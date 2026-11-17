"""Evidence for the env_blocked :8800 tests (Subagent-Impl-Media, Wave 3). Not a test; run manually:

    .venv\\Scripts\\python.exe artifacts/subagent_reports/Subagent-Impl-Media_adguard_proof.py

1. Raw-socket repro with no project code: bytes sent after an HTTP 200 on a loopback TCP socket never
   reach the server (``tests.fixtures.raw_clients.loopback_http_stream_passthrough``).
2. The four failing tests are executed UNCHANGED, but ``socket.create_connection`` (used only by
   MediaSession) returns one end of the fixture's in-memory socket pair, whose other end is served by
   the very same server handler (mock camera ``_media._handle`` / ``FakeMediaServer._serve``).
"""
from __future__ import annotations

import pathlib
import shutil
import socket
import sys
import tempfile
import threading
import traceback

ROOT = pathlib.Path(__file__).resolve().parents[2]
sys.path[:0] = [str(ROOT / "src"), str(ROOT)]

from tests.fixtures.mock_tapo_server import MockTapoCamera  # noqa: E402
from tests.fixtures.raw_clients import loopback_http_stream_passthrough, mem_socketpair  # noqa: E402
from tests.integration import test_vod_download as integ  # noqa: E402
from tests.unit import _media_helpers  # noqa: E402
from tests.unit import test_vod_session as unit  # noqa: E402

_real_create_connection = socket.create_connection
_servers: dict[int, object] = {}  # port -> handler(server_end)


def _mem_create_connection(address, timeout=None, *args, **kwargs):
    handler = _servers.get(address[1])
    if handler is None:
        return _real_create_connection(address, timeout, *args, **kwargs)
    client, server = mem_socketpair()
    client.settimeout(timeout)
    threading.Thread(target=handler, args=(server,), daemon=True).start()
    return client


def _run(name, fn):
    try:
        fn()
        print(f"PASS {name}")
        return True
    except Exception:
        print(f"FAIL {name}\n{traceback.format_exc()}")
        return False


def main() -> int:
    print("raw loopback HTTP stream passthrough:", loopback_http_stream_passthrough())
    socket.create_connection = _mem_create_connection
    ok = True
    ffmpeg = shutil.which("ffmpeg")

    # unit test: FakeMediaServer served over the in-memory pair
    orig_enter = _media_helpers.FakeMediaServer.__enter__

    def enter(self):
        srv = orig_enter(self)

        def serve(server_end):
            server_end.settimeout(0.05)
            srv._serve(server_end)

        _servers[srv.port] = serve
        return srv

    _media_helpers.FakeMediaServer.__enter__ = enter
    ok &= _run("tests/unit/test_vod_session.py::test_send_json_adds_incrementing_seq_and_frames_parts",
               unit.test_send_json_adds_incrementing_seq_and_frames_parts)

    # integration tests: the real mock camera handler
    for name in ("test_media_session_download_handshake", "test_download_seeded_clip_to_valid_mp4",
                 "test_download_every_seeded_clip"):
        cam = MockTapoCamera(secure=True)
        cam.start()
        try:
            def serve(server_end, cam=cam):
                server_end.settimeout(0.5)
                cam._media._handle(server_end, ("mem", 0))

            _servers[cam.media_port] = serve
            fn = getattr(integ, name)
            with tempfile.TemporaryDirectory() as tmp:
                if name == "test_media_session_download_handshake":
                    ok &= _run(f"tests/integration/test_vod_download.py::{name}", lambda: fn(cam))
                else:
                    ok &= _run(f"tests/integration/test_vod_download.py::{name}",
                               lambda: fn(cam, ffmpeg, pathlib.Path(tmp)))
        finally:
            cam.stop()
    return 0 if ok else 1


if __name__ == "__main__":
    raise SystemExit(main())
