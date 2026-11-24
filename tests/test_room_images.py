"""Room images: synthetic game state and mocked provider only; no paid calls."""
import asyncio
import importlib.util
import os
from pathlib import Path
import shutil
import sys
import tempfile
import unittest
from unittest.mock import patch

ROOT = Path(__file__).resolve().parents[1]
TEST_TMPDIR = os.environ.get("LUMINARIA_TEST_TMPDIR")


def temporary_directory(**kwargs):
    return tempfile.TemporaryDirectory(dir=TEST_TMPDIR, **kwargs)


async def request(app, method, path, body=b"", query=b""):
    messages = []
    received = False

    async def receive():
        nonlocal received
        if not received:
            received = True
            return {"type": "http.request", "body": body, "more_body": False}
        await asyncio.sleep(3600)

    async def send(message):
        messages.append(message)

    scope = {"type": "http", "asgi": {"version": "3.0"},
             "http_version": "1.1", "method": method, "scheme": "http",
             "path": path, "raw_path": path.encode(), "query_string": query,
             "headers": [(b"content-type", b"application/json")],
             "client": ("127.0.0.1", 1234), "server": ("test", 80)}
    await app(scope, receive, send)
    start = next(m for m in messages if m["type"] == "http.response.start")
    body = b"".join(m.get("body", b"") for m in messages
                    if m["type"] == "http.response.body")
    return start["status"], dict(start["headers"]), body


class RoomImageAPITests(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        # Engine creates/loads saves on import: import a source-only copy.
        cls.fixture = temporary_directory(prefix="luminaria-image-api-")
        cls.addClassCleanup(cls.fixture.cleanup)
        names = ("engine", "luminaria", "tools", "ui", "io_layer", "llm")
        previous = {name: sys.modules.pop(name) for name in names if name in sys.modules}

        def restore_modules():
            for name in names:
                sys.modules.pop(name, None)
            sys.modules.update(previous)

        cls.addClassCleanup(restore_modules)
        root = Path(cls.fixture.name)
        for source in ROOT.glob("*.py"):
            shutil.copy2(source, root / source.name)
        shutil.copytree(ROOT / "data", root / "data")
        (root / "config.json").write_text('{"narrate": false, "api_url": "http://xxx.invalid/v1"}', encoding="utf-8")
        sys.path.insert(0, str(root))
        try:
            spec = importlib.util.spec_from_file_location("image_test_api", root / "api.py")
            assert spec is not None and spec.loader is not None
            cls.api = importlib.util.module_from_spec(spec)
            with patch.dict(os.environ, {"NANOGPT_API_KEY": ""}):
                spec.loader.exec_module(cls.api)
        finally:
            sys.path.remove(str(root))

    def call(self, method="POST", path="/api/room-image", **kwargs):
        return asyncio.run(request(self.api.app, method, path, **kwargs))

    def test_image_comes_only_from_current_actor_room_not_request_body(self):
        import room_images
        calls = []
        png = b"\x89PNG\r\n\x1a\nfixture"

        def generate(prompt):
            calls.append(prompt)
            return png

        with temporary_directory() as directory:
            cache = room_images.RoomImageCache(Path(directory) / "images.sqlite3", "test-only", generator=generate)
            with patch.object(self.api, "ROOM_IMAGES", cache, create=True):
                status, headers, body = self.call(
                    body=b'{"room_id":"99_99_-99","x":99,"prompt":"INJECTED SCENE"}',
                    query=b"room_id=99_99_-99")
                self.assertEqual(status, 200)
                self.assertEqual(body, png)
                self.assertEqual(headers[b"content-type"], b"image/png")
                self.assertEqual(headers[b"content-disposition"], b"inline")
                self.assertEqual(headers[b"cache-control"], b"no-store")
                pos = self.api.GAME.eng.player.data["position"]
                room_id = f"{pos['x']}_{pos['y']}_{pos['z']}"
                self.assertEqual(headers[b"x-room-id"].decode(), room_id)
                self.assertEqual(self.call()[2], png)
        self.assertEqual(len(calls), 1)
        self.assertIn(self.api.GAME._room()["name"], calls[0])
        self.assertNotIn("INJECTED SCENE", calls[0])
        self.assertNotIn("99_99_-99", calls[0])

    def test_slow_generation_does_not_block_state_or_game_commands(self):
        import json
        import threading
        import room_images
        started, release = threading.Event(), threading.Event()
        old_pos = dict(self.api.GAME.eng.player.data["position"])
        old_id = f"{old_pos['x']}_{old_pos['y']}_{old_pos['z']}"
        direction = next(iter(self.api.GAME._room()["exits"]))

        def generate(_):
            started.set()
            release.wait(4)
            return b"\x89PNG\r\n\x1a\nfixture"

        async def exercise():
            task = asyncio.create_task(request(self.api.app, "POST", "/api/room-image"))
            try:
                self.assertTrue(await asyncio.to_thread(started.wait, 2))
                state = await asyncio.wait_for(request(self.api.app, "GET", "/api/state"), 1)
                self.assertEqual(state[0], 200)
                command = await asyncio.wait_for(request(self.api.app, "POST", "/api/command",
                                                        json.dumps({"text": direction}).encode()), 1)
                self.assertEqual(command[0], 200)
                await asyncio.wait_for(asyncio.to_thread(self.api._jobs.join), 2)
                self.assertNotEqual(self.api.GAME.eng.player.data["position"], old_pos)
                duplicate = await asyncio.wait_for(request(self.api.app, "POST", "/api/room-image"), 1)
                self.assertEqual(duplicate[0], 204)
            finally:
                release.set()
            image = await asyncio.wait_for(task, 2)
            self.assertEqual(image[0], 200)
            self.assertEqual(image[1][b"x-room-id"].decode(), old_id)

        with temporary_directory() as directory:
            cache = room_images.RoomImageCache(Path(directory) / "images.sqlite3", "test-only", generator=generate)
            try:
                with patch.object(self.api, "ROOM_IMAGES", cache):
                    asyncio.run(exercise())
            finally:
                with self.api._lock:
                    self.api.GAME.eng.player.data["position"] = old_pos

    def test_busy_or_unloaded_room_never_calls_provider_or_generates_floor(self):
        from unittest.mock import Mock
        cache = Mock()
        with patch.object(self.api, "ROOM_IMAGES", cache):
            with patch.object(self.api, "_busy", True):
                self.assertEqual(self.call()[0], 204)
            with patch.dict(self.api.GAME.eng.player.data, {"position": {"x": 99, "y": 99, "z": -99}}), \
                 patch.object(self.api.GAME.eng.world, "get_room") as get_room:
                self.assertEqual(self.call()[0], 204)
                get_room.assert_not_called()
        cache.get.assert_not_called()

    def test_cache_exception_is_an_empty_204_not_a_game_error(self):
        from unittest.mock import Mock
        cache = Mock()
        cache.get.side_effect = OSError("disk unavailable")
        with patch.object(self.api, "ROOM_IMAGES", cache):
            status, _, body = self.call()
        self.assertEqual(status, 204)
        self.assertEqual(body, b"")
        self.assertEqual(self.call(method="GET")[0], 405)

    def test_disabled_images_return_empty_204(self):
        status, headers, body = self.call()
        self.assertEqual(status, 204)
        self.assertEqual(body, b"")
        self.assertNotIn(b"application/json", headers.get(b"content-type", b""))


class RoomImageCacheTests(unittest.TestCase):
    def test_concurrent_request_is_deduplicated_without_waiting(self):
        import room_images
        from concurrent.futures import ThreadPoolExecutor
        import threading
        started, release = threading.Event(), threading.Event()
        calls = []

        def generate(prompt):
            calls.append(prompt)
            started.set()
            release.wait(3)
            return b"\x89PNG\r\n\x1a\nfixture"

        with temporary_directory() as directory:
            path = Path(directory) / "images.sqlite3"
            first = room_images.RoomImageCache(path, "test-only", generator=generate)
            second = room_images.RoomImageCache(path, "test-only", generator=generate)
            with ThreadPoolExecutor(max_workers=2) as pool:
                pending = pool.submit(first.get, "0_0_0", {"name": "Square"})
                self.assertTrue(started.wait(2))
                duplicate = pool.submit(second.get, "0_0_0", {"name": "Square"})
                try:
                    self.assertIsNone(duplicate.result(timeout=1))
                finally:
                    release.set()
                self.assertIsNotNone(pending.result(timeout=2))
            self.assertEqual(len(calls), 1)

    def test_provider_failure_is_durable_and_trips_global_cooldown(self):
        import room_images
        calls = []

        def fail(prompt):
            calls.append(prompt)
            raise OSError("provider unavailable")

        with temporary_directory() as directory:
            path = Path(directory) / "images.sqlite3"
            cache = room_images.RoomImageCache(path, "test-only", generator=fail)
            self.assertIsNone(cache.get("0_0_0", {"name": "Square"}))
            for _ in range(15):
                self.assertIsNone(cache.get("0_0_0", {"name": "Square"}))
            restored = room_images.RoomImageCache(path, "test-only", generator=fail)
            self.assertIsNone(restored.get("0_0_0", {"name": "Square"}))
            self.assertIsNone(restored.get("1_0_0", {"name": "Shop"}))
            self.assertEqual(len(calls), 1)

    def test_generation_budget_is_durable_and_cached_hits_still_work(self):
        import room_images
        calls = []

        def generate(prompt):
            calls.append(prompt)
            return b"\x89PNG\r\n\x1a\nfixture"

        with temporary_directory() as directory:
            path = Path(directory) / "images.sqlite3"
            cache = room_images.RoomImageCache(path, "test-only", generator=generate, max_entries=2)
            first = cache.get("0_0_0", {"name": "Square"})
            self.assertIsNotNone(cache.get("1_0_0", {"name": "Shop"}))
            self.assertIsNone(cache.get("2_0_0", {"name": "Inn"}))
            restored = room_images.RoomImageCache(path, "test-only", generator=generate, max_entries=2)
            self.assertIsNone(restored.get("3_0_0", {"name": "Gate"}))
            self.assertEqual(restored.get("0_0_0", {"name": "Square"}), first)
            self.assertEqual(len(calls), 2)

    def test_only_one_distinct_room_is_generated_at_a_time_without_queue(self):
        import room_images
        from concurrent.futures import ThreadPoolExecutor
        import threading
        started, release = threading.Event(), threading.Event()
        calls = []

        def generate(prompt):
            calls.append(prompt)
            started.set()
            release.wait(3)
            return b"\x89PNG\r\n\x1a\nfixture"

        with temporary_directory() as directory:
            cache = room_images.RoomImageCache(Path(directory) / "images.sqlite3", "test-only", generator=generate)
            with ThreadPoolExecutor(max_workers=2) as pool:
                pending = pool.submit(cache.get, "0_0_0", {"name": "Square"})
                self.assertTrue(started.wait(2))
                other = pool.submit(cache.get, "1_0_0", {"name": "Shop"})
                try:
                    self.assertIsNone(other.result(timeout=1))
                finally:
                    release.set()
                self.assertIsNotNone(pending.result(timeout=2))
            self.assertIsNotNone(cache.get("1_0_0", {"name": "Shop"}))
            self.assertEqual(len(calls), 2)

    def test_cache_uses_actual_raster_media_type_and_rejects_non_images(self):
        import room_images
        samples = [(b"\xff\xd8\xfffixture", "image/jpeg"),
                   (b"RIFF0000WEBPfixture", "image/webp"), (b"<html>not art</html>", None)]
        for data, expected in samples:
            with self.subTest(media_type=expected), temporary_directory() as directory:
                cache = room_images.RoomImageCache(Path(directory) / "images.sqlite3", "test-only", generator=lambda _: data)
                result = cache.get("0_0_0", {"name": "Square"})
                if expected:
                    assert result is not None
                    self.assertEqual(result.media_type, expected)
                else:
                    self.assertIsNone(result)

    def test_gameplay_changes_and_discovered_stairs_do_not_reroll_art(self):
        import room_images
        from unittest.mock import Mock
        generate = Mock(return_value=b"\x89PNG\r\n\x1a\nfixture")
        room = {"name": "Hall", "type": "Combat", "visual_tags": ["stone"], "encounter": "Goblin"}
        with temporary_directory() as directory:
            cache = room_images.RoomImageCache(Path(directory) / "images.sqlite3", "test-only", generator=generate)
            first = cache.get("0_0_-1", room)
            changed = dict(room, encounter="None", corpse={"searched": True},
                           dropped_items=[{"name": "Sword"}], stairs_up=True,
                           visual_tags=["stone", "stone stairs leading up"])
            self.assertEqual(cache.get("0_0_-1", changed), first)
            self.assertEqual(generate.call_count, 1)

    def test_abandoned_pending_room_is_not_retried_or_a_permanent_global_block(self):
        import room_images
        from unittest.mock import Mock
        with temporary_directory() as directory:
            path = Path(directory) / "images.sqlite3"
            cache = room_images.RoomImageCache(path, "test-only", generator=Mock(side_effect=KeyboardInterrupt))
            with patch.object(room_images.time, "time", return_value=100):
                with self.assertRaises(KeyboardInterrupt):
                    cache.get("0_0_0", {"name": "Square"})
            generate = Mock(return_value=b"\x89PNG\r\n\x1a\nfixture")
            restored = room_images.RoomImageCache(path, "test-only", generator=generate)
            with patch.object(room_images.time, "time", return_value=1000):
                self.assertIsNone(restored.get("0_0_0", {"name": "Square"}))
                self.assertIsNotNone(restored.get("1_0_0", {"name": "Shop"}))
            self.assertEqual(generate.call_count, 1)

    def test_broken_cache_fails_closed_without_a_provider_call(self):
        import room_images
        from unittest.mock import Mock
        with temporary_directory() as directory:
            broken = Path(directory) / "not-a-database"
            broken.write_bytes(b"corrupt cache")
            generate = Mock(return_value=b"\x89PNG\r\n\x1a\nfixture")
            for path in (broken, broken / "images.sqlite3"):
                with self.subTest(path=path):
                    cache = room_images.RoomImageCache(path, "test-only", generator=generate)
                    self.assertIsNone(cache.get("0_0_0", {"name": "Square"}))
            generate.assert_not_called()

    def test_oversized_image_is_not_cached_or_returned(self):
        import room_images
        data = b"\x89PNG\r\n\x1a\n" + b"x" * 100
        with temporary_directory() as directory, \
             patch.object(room_images, "MAX_IMAGE_BYTES", 64, create=True):
            cache = room_images.RoomImageCache(Path(directory) / "images.sqlite3", "test-only", generator=lambda _: data)
            self.assertIsNone(cache.get("0_0_0", {"name": "Square"}))

    def test_missing_key_never_calls_provider_or_creates_cache(self):
        import room_images
        from unittest.mock import Mock
        with temporary_directory() as directory:
            path = Path(directory) / "new" / "images.sqlite3"
            generate = Mock()
            cache = room_images.RoomImageCache(path, "  ", generator=generate)
            self.assertIsNone(cache.get("0_0_0", {"name": "Square"}))
            self.assertFalse(path.parent.exists())
            generate.assert_not_called()

    def test_image_is_cached_durably_across_instances(self):
        import room_images
        png = b"\x89PNG\r\n\x1a\n" + b"fixture-image"
        calls = []

        def generate(prompt):
            calls.append(prompt)
            return png

        with temporary_directory() as directory:
            path = Path(directory) / "images.sqlite3"
            cache = room_images.RoomImageCache(path, "test-only", generator=generate)
            first = cache.get("-1_1_0", {"name": "Temple", "visual_tags": ["marble"]})
            assert first is not None
            self.assertEqual(first.data, png)
            self.assertEqual(first.media_type, "image/png")
            restored = room_images.RoomImageCache(path, "test-only", generator=generate)
            self.assertEqual(restored.get("-1_1_0", {"name": "Temple", "visual_tags": ["marble"]}), first)
            self.assertEqual(len(calls), 1)
            self.assertIn("Temple", calls[0])
            self.assertIn("marble", calls[0])


class NanoGPTProviderTests(unittest.TestCase):
    def test_provider_response_reads_are_bounded(self):
        import urllib.request
        import room_images
        from unittest.mock import MagicMock
        response = MagicMock()
        response.__enter__.return_value = response
        response.read.return_value = b" " * 129
        opener = MagicMock()
        opener.open.return_value = response
        with patch.object(room_images, "MAX_RESPONSE_BYTES", 128, create=True), \
             patch.object(urllib.request, "build_opener", return_value=opener):
            with self.assertRaises(ValueError):
                room_images.NanoGPTGenerator("test-only")("scene")
        response.read.assert_called_once_with(129)

    def test_redirects_are_not_followed_with_server_credentials(self):
        import http.client
        import io
        import urllib.request
        import room_images
        handler = room_images._NoRedirect()
        self.assertIsNone(handler.redirect_request(urllib.request.Request("https://nano-gpt.com"),
                                                  io.BytesIO(), 302, "Found", http.client.HTTPMessage(),
                                                  "https://other.invalid"))

    def test_rejects_invalid_provider_payloads_and_non_images(self):
        import base64
        import io
        import json
        import urllib.request
        import room_images
        from unittest.mock import MagicMock
        invalid = [b'not json', b'{}', b'{"data":[]}',
                   json.dumps({"data": [{"url": "http://untrusted.invalid/image"}]}).encode(),
                   json.dumps({"data": [{"b64_json": "!!!!"}]}).encode(),
                   json.dumps({"data": [{"b64_json": base64.b64encode(b"<svg>bad</svg>").decode()}]}).encode(),
                   json.dumps({"data": [{"b64_json": "AA=="}, {"b64_json": "AA=="}]}).encode()]
        for body in invalid:
            with self.subTest(body=body):
                opener = MagicMock()
                opener.open.return_value = io.BytesIO(body)
                with patch.object(urllib.request, "build_opener", return_value=opener):
                    with self.assertRaises(ValueError):
                        room_images.NanoGPTGenerator("test-only")("scene")

    def test_fixed_provider_contract_and_bearer_auth(self):
        import base64
        import io
        import json
        import urllib.request
        import room_images
        from unittest.mock import MagicMock
        png = b"\x89PNG\r\n\x1a\nfixture"
        response = io.BytesIO(json.dumps({"data": [{"b64_json": base64.b64encode(png).decode()}]}).encode())
        opener = MagicMock()
        opener.open.return_value = response
        with patch.object(urllib.request, "build_opener", return_value=opener) as build:
            result = room_images.NanoGPTGenerator("server-only-secret")("Temple scene")
        self.assertEqual(result, png)
        req = opener.open.call_args.args[0]
        self.assertEqual(req.full_url, "https://nano-gpt.com/api/v1/images/generations")
        self.assertEqual(req.method, "POST")
        self.assertEqual(req.get_header("Authorization"), "Bearer server-only-secret")
        self.assertEqual(json.loads(req.data), {"model": "z-image-turbo", "prompt": "Temple scene",
                                             "size": "1280x720", "n": 1, "response_format": "b64_json"})
        self.assertEqual(opener.open.call_args.kwargs["timeout"], 45)
        self.assertTrue(build.called)


if __name__ == "__main__":
    unittest.main()
