#!/usr/bin/env python3

# LuminariaSUD - a deterministic dungeon crawler with a web/API shell.
# Copyright (C) 2026 Apollo (FridayAndCo)
#
# This program is free software: you can redistribute it and/or modify
# it under the terms of the GNU General Public License as published by
# the Free Software Foundation, either version 3 of the License, or
# (at your option) any later version.
#
# This program is distributed in the hope that it will be useful,
# but WITHOUT ANY WARRANTY; without even the implied warranty of
# MERCHANTABILITY or FITNESS FOR A PARTICULAR PURPOSE.  See the
# GNU General Public License for more details.
#
# You should have received a copy of the GNU General Public License
# along with this program.  If not, see <https://www.gnu.org/licenses/>.
"""api.py - Phase 1 web API for LuminariaSUD.

Wraps the existing engine + game logic in a single-threaded "actor": one Game
instance is driven by one worker thread through Game.process_command(). A web
(or any HTTP) client talks JSON to FastAPI endpoints and never touches stdin.

Because the game's combat/conversation loops block on input(), the worker can
pause mid-command waiting on a prompt; the client streams new lines via
/events?cursor=N and answers the prompt via /answer - so one logical command
may span many HTTP round-trips without deadlocking.

Run:  python3 api.py            (http://127.0.0.1:8090)
      python3 api.py --port 9000 --no-narrate
"""

import argparse
import copy
import os
import threading

import io_layer
import ui
import luminaria as L
from room_images import RoomImageCache
try:
    from fastapi import FastAPI, HTTPException
    from fastapi.responses import HTMLResponse, JSONResponse, Response
    from pydantic import BaseModel
except ImportError as _e:  # friendly hint instead of a raw traceback
    import sys
    sys.stderr.write(
        "\n[LuminariaSUD] The web server needs a few Python packages that\n"
        "aren't installed yet ({}).\n\n"
        "    pip install -r requirements.txt\n\n"
        "Tip: terminal mode needs none of these - just run\n"
        "    python3 luminaria.py\n\n".format(_e)
    )
    sys.exit(1)

BASE = os.path.dirname(os.path.abspath(__file__))
STATIC = os.path.join(BASE, "static")
ROOM_IMAGES = RoomImageCache(
    os.path.join(BASE, "state", "room_images.sqlite3"),
    os.environ.get("NANOGPT_API_KEY", ""),
)

# ---- shared game state (the actor) ----
CFG = L.load_cfg()
GAME = L.Game(CFG)
QIO = io_layer.QueueIO()
ui.set_sink(QIO)
_notice = GAME.narration_notice()
if _notice:
    QIO.emit("warn", _notice)

_lock = threading.RLock()        # guards GAME mutation + snapshot reads
_gate = threading.Lock()         # guards the busy flag only (never held during a prompt)
_busy = False
_last_snapshot = None


def _set_busy(v):
    global _busy
    _busy = v


def _snapshot_safe():
    """Snapshot only when the worker isn't mid-command (avoids torn reads)."""
    global _last_snapshot
    if _lock.acquire(blocking=False):
        try:
            _last_snapshot = GAME.snapshot()
        finally:
            _lock.release()
    return _last_snapshot


# ---- worker thread ----
def _worker():
    global _busy
    while True:
        text = _jobs.get()
        _set_busy(True)
        try:
            with _lock:
                GAME.process_command(text)
        except Exception as e:  # never let the actor die on a bad command
            QIO.emit("bad", f"(api error: {e})")
        finally:
            _set_busy(False)
        _jobs.task_done()


import queue  # noqa: E402
_jobs = queue.Queue()
_thread = threading.Thread(target=_worker, daemon=True)
_thread.start()


# ---- models ----
class Cmd(BaseModel):
    text: str = ""


class Ans(BaseModel):
    text: str = ""


# ---- app ----
app = FastAPI(title="LuminariaSUD API", version="0.1")


@app.get("/", response_class=HTMLResponse)
def index(legacy: bool = False):
    # The Phase 1 test harness stays reachable at /?legacy=1.
    fname = "legacy.html" if legacy and os.path.exists(os.path.join(STATIC, "legacy.html")) else "index.html"
    path = os.path.join(STATIC, fname)
    if os.path.exists(path):
        with open(path, encoding="utf-8") as f:
            return f.read()
    return "<h1>LuminariaSUD API</h1><p>See /docs. POST /api/command {text}.</p>"


@app.get("/api/state")
def state():
    return JSONResponse({
        "snapshot": _snapshot_safe(),
        "busy": _busy,
        "awaiting_prompt": QIO.awaiting_prompt,
        "prompt": QIO._prompt_text if QIO.awaiting_prompt else None,
    })


@app.get("/api/events")
def events(cursor: int = 0):
    evs, new_cursor = QIO.poll(cursor)
    return JSONResponse({
        "events": evs,
        "cursor": new_cursor,
        "busy": _busy,
        "awaiting_prompt": QIO.awaiting_prompt,
        "prompt": QIO._prompt_text if QIO.awaiting_prompt else None,
        # refresh panels once the command has finished
        "snapshot": None if _busy else _snapshot_safe(),
    })


@app.post("/api/room-image")
def room_image():
    """Same shared actor as /state and /command; never accept a client scene.

    The POST waits for its image, not the game actor. Concurrent or unavailable
    requests get an empty 204. X-Room-Id lets clients discard late room art.
    """
    headers = {"Cache-Control": "no-store", "X-Content-Type-Options": "nosniff"}
    if not _lock.acquire(blocking=False):
        return Response(status_code=204, headers=headers)
    try:
        if _busy:
            return Response(status_code=204, headers=headers)
        pos = GAME.eng.player.data["position"]
        room_id = f"{pos['x']}_{pos['y']}_{pos['z']}"
        headers["X-Room-Id"] = room_id
        world = GAME.eng.world
        # Read existing rooms only: get_room() can generate/save a whole floor.
        room = copy.deepcopy(world.town_rooms.get(room_id) or world.rooms.get(room_id))
    finally:
        _lock.release()
    if not room:
        return Response(status_code=204, headers=headers)
    try:
        image = ROOM_IMAGES.get(room_id, room)
    except Exception:
        # Optional artwork must never turn a game request into an error.
        image = None
    if image is None:
        return Response(status_code=204, headers=headers)
    headers["Content-Disposition"] = "inline"
    return Response(content=image.data, media_type=image.media_type, headers=headers)


@app.post("/api/command")
def command(cmd: Cmd):
    text = (cmd.text or "").strip()
    if not text:
        raise HTTPException(400, "empty command")
    with _gate:
        if _busy:
            raise HTTPException(409, "busy - a command is already running")
        _set_busy(True)
    _jobs.put(text)
    return JSONResponse({"started": True, "text": text})


@app.post("/api/answer")
def answer(ans: Ans):
    if not QIO.awaiting_prompt:
        raise HTTPException(409, "no prompt is waiting")
    QIO.answer_prompt((ans.text or "").strip())
    return JSONResponse({"answered": True})


@app.post("/api/reset")
def reset():
    """Abandon the run and reset to defaults (bypasses the CLI confirm prompt)."""
    with _gate:
        if _busy:
            raise HTTPException(409, "busy")
        _set_busy(True)
    try:
        with _lock:
            GAME.eng.reset_game()
            GAME.insight = GAME.insight_max
            GAME.last_narration = None
            QIO.emit("say", "The world dissolves and reknits itself at the Temple of Radiance.")
            snap = GAME.snapshot()
    finally:
        _set_busy(False)
    return JSONResponse({"reset": True, "snapshot": snap})


def main():
    ap = argparse.ArgumentParser(description="LuminariaSUD web API (Phase 1)")
    ap.add_argument("--host", default="127.0.0.1")
    ap.add_argument("--port", type=int, default=8090)
    ap.add_argument("--narrate", dest="narrate", action="store_true", default=None,
                    help="force LLM prose ON (default: from config.json - ON)")
    ap.add_argument("--no-narrate", dest="narrate", action="store_false",
                    help="force LLM prose OFF")
    args = ap.parse_args()
    if args.narrate is not None:
        GAME.narrate_on = args.narrate
    import uvicorn
    uvicorn.run(app, host=args.host, port=args.port, log_level="info")


if __name__ == "__main__":
    main()
