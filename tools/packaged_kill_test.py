"""Gate 4 check: packaged TepoDesktop.exe streams from a mock camera; after a hard kill
(TerminateProcess, no cleanup code runs) every ffmpeg child must be gone within 200 ms
thanks to the Windows Job Object (KILL_ON_JOB_CLOSE)."""
from __future__ import annotations

import json
import os
import subprocess
import sys
import tempfile
import time
from pathlib import Path

import psutil

ROOT = Path(__file__).resolve().parent.parent
sys.path[:0] = [str(ROOT / "src"), str(ROOT)]
from tapo_desktop.core.auth.vault import CredentialVault  # noqa: E402
from tapo_desktop.models import CameraConfig, CredentialKind, Credentials  # noqa: E402
from tapo_desktop.services.config_store import ConfigStore  # noqa: E402
from tests.fixtures.mock_tapo_server import MockTapoCamera  # noqa: E402

EXE = ROOT / "release" / "TepoDesktop" / "TepoDesktop.exe"


def main() -> int:
    home = Path(tempfile.mkdtemp(prefix="tapo_pkg_"))
    cams = [MockTapoCamera(cloud_password="cloudpass", camera_user="camuser", camera_password="campass")
            for _ in range(2)]
    for c in cams:
        c.start()
    proc = None
    try:
        store = ConfigStore(home / "config.json")
        vault = CredentialVault(home / "vault.json")
        cfgs = []
        for i, c in enumerate(cams):
            cfg = CameraConfig(name=f"Mock {i + 1}", host=c.host, https_port=c.https_port,
                               rtsp_port=c.rtsp_port, onvif_port=c.onvif_port, media_port=c.media_port)
            vault.set(cfg.id, CredentialKind.CLOUD, Credentials("admin", "cloudpass"))
            vault.set(cfg.id, CredentialKind.CAMERA_ACCOUNT, Credentials("camuser", "campass"))
            cfgs.append(cfg)
        store.save(cfgs)
        store.set_setting("grid_size", 2)
        env = dict(os.environ, TAPO_DESKTOP_HOME=str(home), QT_QPA_PLATFORM="offscreen")
        proc = subprocess.Popen([str(EXE)], env=env)
        p = psutil.Process(proc.pid)
        deadline = time.monotonic() + 30
        ff: list[psutil.Process] = []
        while time.monotonic() < deadline:
            ff = [c for c in p.children(recursive=True) if c.name().lower() == "ffmpeg.exe"]
            if len(ff) >= 2 and all(c.rtsp_active_clients for c in cams):
                break
            time.sleep(0.25)
        streaming = len(ff) >= 2 and all(c.rtsp_active_clients for c in cams)
        rss_mb = p.memory_full_info().private / 2**20
        time.sleep(3)
        t0 = time.perf_counter()
        proc.kill()  # TerminateProcess: no Python cleanup in the app
        proc.wait(5)
        gone, alive = psutil.wait_procs(ff, timeout=2)
        elapsed_ms = (time.perf_counter() - t0) * 1000
        res = {"ffmpeg_children": len(ff), "streaming_from_mock": streaming,
               "app_private_mb_while_streaming": round(rss_mb, 1),
               "ffmpeg_alive_after_kill": len(alive), "all_ffmpeg_gone_ms": round(elapsed_ms, 1)}
        res["ok"] = streaming and not alive and elapsed_ms < 200
        print(json.dumps(res, indent=2))
        return 0 if res["ok"] else 1
    finally:
        if proc and proc.poll() is None:
            proc.kill()
        for c in cams:
            c.stop()


if __name__ == "__main__":
    sys.exit(main())
