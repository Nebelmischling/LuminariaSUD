"""Server-only, durable room art; no network or filesystem work on import.

NANOGPT_API_KEY is supplied by api.py, never by clients. A room's first claim
is committed before contacting the provider; failed and abandoned claims are
never retried automatically (including after restart). One generation per
cache instance, no queue, and a lifetime budget of 128 claims bound spend and
storage. Failed generations also trip a durable five-minute global cooldown.
Abandoned pending claims stop occupying the shared slot after two minutes,
but remain negative results for that room. Only PNG/JPEG/WebP bytes are served.

The cache lives under state/ and is independent of game saves/reset. Removing
it while the server is stopped deliberately resets both images and the paid
attempt budget. There are no provider retries, URL downloads or redirects.
"""
import base64
from contextlib import closing
from dataclasses import dataclass
import hashlib
import json
from pathlib import Path
import sqlite3
import threading
import time
import urllib.request


MAX_RESPONSE_BYTES = 12 * 1024 * 1024
MAX_IMAGE_BYTES = 8 * 1024 * 1024


class _NoRedirect(urllib.request.HTTPRedirectHandler):
    def redirect_request(self, req, fp, code, msg, headers, newurl):
        return None


class NanoGPTGenerator:
    def __init__(self, api_key):
        self.api_key = api_key

    def __call__(self, prompt):
        payload = {"model": "z-image-turbo", "prompt": prompt,
                   "size": "1280x720", "n": 1, "response_format": "b64_json"}
        req = urllib.request.Request(
            "https://nano-gpt.com/api/v1/images/generations",
            data=json.dumps(payload).encode(), method="POST",
            headers={"Authorization": "Bearer " + self.api_key,
                     "Content-Type": "application/json", "Accept": "application/json"})
        with urllib.request.build_opener(_NoRedirect()).open(req, timeout=45) as response:
            raw = response.read(MAX_RESPONSE_BYTES + 1)
        if len(raw) > MAX_RESPONSE_BYTES:
            raise ValueError("Image response is too large")
        result = json.loads(raw)
        items = result.get("data") if isinstance(result, dict) else None
        if not isinstance(items, list) or len(items) != 1 or not isinstance(items[0], dict):
            raise ValueError("Expected exactly one image")
        encoded = items[0].get("b64_json")
        if not isinstance(encoded, str) or not encoded:
            raise ValueError("Expected inline base64 image")
        data = base64.b64decode(encoded, validate=True)
        _media_type(data)
        return data


def _media_type(data):
    # Raster signatures only; never pass HTML or active SVG through inline.
    if isinstance(data, bytes) and 0 < len(data) <= MAX_IMAGE_BYTES:
        if data.startswith(b"\x89PNG\r\n\x1a\n"):
            return "image/png"
        if data.startswith(b"\xff\xd8\xff"):
            return "image/jpeg"
        if data.startswith(b"RIFF") and data[8:12] == b"WEBP":
            return "image/webp"
    raise ValueError("Expected a PNG, JPEG or WebP image")


@dataclass(frozen=True)
class Image:
    data: bytes
    media_type: str


def _scene(room_id, room):
    # Deliberately exclude changing loot, encounters, clock, prose and inventory.
    fields = ("name", "type", "description", "visual_tags", "theme", "atmosphere",
              "stairs_up", "stairs_down")
    scene = {key: room[key] for key in fields if key in room}
    scene["room_id"] = room_id
    canonical = json.dumps(scene, sort_keys=True, ensure_ascii=True)
    identity = {k: v for k, v in scene.items() if k not in ("stairs_up", "stairs_down")}
    identity["visual_tags"] = [tag for tag in room.get("visual_tags", [])
                               if tag not in ("stone stairs leading up", "stone stairs leading down")]
    key = hashlib.sha256(("room-art-v1:" + json.dumps(identity, sort_keys=True)).encode()).hexdigest()
    prompt = ("Fantasy dungeon crawler environment illustration, atmospheric cinematic "
              "wide composition, painterly detail. Depict only this location, no text, "
              "no UI, no captions. Scene data: " + canonical)
    return key, prompt


class RoomImageCache:
    def __init__(self, path, api_key, *, generator=None, max_entries=128):
        self.path = Path(path)
        self.api_key = api_key.strip()
        self.generator = generator if generator is not None else NanoGPTGenerator(self.api_key)
        self.max_entries = max_entries
        self._generation_gate = threading.Lock()

    def get(self, room_id, room):
        if not self.api_key or not self._generation_gate.acquire(blocking=False):
            return None
        try:
            return self._get(room_id, room)
        except (OSError, sqlite3.Error, ValueError, TypeError):
            # Fail closed: without a durable claim there must be no paid call.
            return None
        finally:
            self._generation_gate.release()

    def _get(self, room_id, room):
        key, prompt = _scene(room_id, room)
        self.path.parent.mkdir(parents=True, exist_ok=True)
        with closing(sqlite3.connect(self.path, isolation_level=None)) as db:
            db.execute("CREATE TABLE IF NOT EXISTS images "
                       "(key TEXT PRIMARY KEY, status TEXT, data BLOB, media_type TEXT, started REAL)")
            db.execute("CREATE TABLE IF NOT EXISTS limits "
                       "(name TEXT PRIMARY KEY, value REAL NOT NULL)")
            db.execute("BEGIN IMMEDIATE")
            row = db.execute("SELECT status, data, media_type FROM images WHERE key=?", (key,)).fetchone()
            if row:
                db.commit()
                return Image(row[1], row[2]) if row[0] == "ready" else None
            cooldown = db.execute("SELECT value FROM limits WHERE name='next_attempt'").fetchone()
            count = db.execute("SELECT COUNT(*) FROM images").fetchone()[0]
            # Abandoned claims remain negative cache entries, but after two
            # minutes no longer occupy the global generation slot.
            active = db.execute("SELECT COUNT(*) FROM images WHERE status='pending' AND started>?",
                                (time.time() - 120,)).fetchone()[0]
            if active or count >= self.max_entries or (cooldown and cooldown[0] > time.time()):
                db.commit()
                return None
            db.execute("INSERT INTO images (key, status, started) VALUES (?, 'pending', ?)", (key, time.time()))
            db.commit()
            # No transaction (and no game lock) spans the remote operation.
            try:
                data = self.generator(prompt)
                image = Image(data, _media_type(data))
            except Exception:
                # Persist a negative result before allowing any further work.
                db.execute("BEGIN IMMEDIATE")
                db.execute("UPDATE images SET status='failed' WHERE key=?", (key,))
                db.execute("INSERT OR REPLACE INTO limits VALUES ('next_attempt', ?)",
                           (time.time() + 300,))
                db.commit()
                return None
            db.execute("UPDATE images SET status='ready', data=?, media_type=? WHERE key=?",
                       (image.data, image.media_type, key))
            return image
