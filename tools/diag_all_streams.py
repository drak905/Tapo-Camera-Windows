"""View-only capacity test: every camera in a 4x4 grid streams at once (remote cap lifted) on a COPY
of the user's settings. Measures per-camera fps, start time, app + ffmpeg memory and CPU.
Usage: python tools/diag_all_streams.py [decoder: policy|none|hw]  (policy = the app default since Phase 6:
software for grid tiles, hardware for HD; none / hw force software / d3d11va for every stream)"""
import json
import os
import shutil
import sys
import tempfile
import time
from pathlib import Path

import psutil

real = Path(os.environ["APPDATA"]) / "TapoDesktop"
tmp = Path(tempfile.mkdtemp(prefix="tapo_cap_"))
for f in ("config.json", "vault.json", "pins.json"):
    if (real / f).exists():
        shutil.copy2(real / f, tmp / f)
os.environ["TAPO_DESKTOP_HOME"] = str(tmp)
os.environ.setdefault("QT_QPA_PLATFORM", "offscreen")
sys.path.insert(0, str(Path("src").resolve()))

from PyQt6.QtWidgets import QApplication  # noqa: E402

from tapo_desktop.core.auth.vault import CredentialVault  # noqa: E402
from tapo_desktop.services.account import AccountManager  # noqa: E402
from tapo_desktop.services.config_store import ConfigStore  # noqa: E402
from tapo_desktop.services.controller import AppController  # noqa: E402
from tapo_desktop.ui.main_window import MainWindow  # noqa: E402

HW = sys.argv[1] if len(sys.argv) > 1 else "policy"
if HW in ("none", "hw"):
    import tapo_desktop.core.media_engine.supervisor as sup_mod
    orig = sup_mod.StreamSupervisor.__init__

    def init(self, *a, **k):
        k["hwaccel"] = None if HW == "none" else "auto"
        orig(self, *a, **k)
    sup_mod.StreamSupervisor.__init__ = init


def pump(app, cond, t):
    end = time.monotonic() + t
    while time.monotonic() < end:
        app.processEvents()
        if cond():
            return True
        time.sleep(0.02)
    return False


app = QApplication(["cap"])
store = ConfigStore(tmp / "config.json")
store.set_setting("max_remote_streams", 16)
store.set_setting("grid_size", 4)
ctl = AppController(store, CredentialVault(tmp / "vault.json"))
acct = AccountManager(ctl)
done = []
acct.cameras_refreshed.connect(lambda *a: done.append(1))
first: dict[str, float] = {}
frames: dict[str, int] = {}
states: dict[str, str] = {}
t0 = time.monotonic()


def on_frame(cid, img):
    frames[cid] = frames.get(cid, 0) + 1
    first.setdefault(cid, time.monotonic() - t0)


ctl.frame_ready.connect(on_frame)
ctl.stream_state.connect(lambda c, s, r: states.__setitem__(c, s + (f": {r[:60]}" if r else "")))
win = MainWindow(ctl, tray_enabled=False, accounts=acct)
win.resize(1600, 950)
win.show()
acct.restore_session()
pump(app, lambda: bool(done), 40)
online = [c for c in ctl.cameras() if c.online]
pump(app, lambda: len(first) >= len(online), 60)
me = psutil.Process()
me.cpu_percent()
for p in me.children(recursive=True):
    p.cpu_percent()
f0 = dict(frames)
pump(app, lambda: False, 20)
kids = [p for p in me.children(recursive=True) if p.name().lower() == "ffmpeg.exe"]
fps = {c.name: round((frames.get(c.id, 0) - f0.get(c.id, 0)) / 20, 1) for c in online}
res = {
    "hwaccel": HW, "cameras_online": len(online), "streaming": sum(1 for v in fps.values() if v > 5),
    "fps": fps, "first_frame_s": {c.name: round(first[c.id], 1) for c in online if c.id in first},
    "not_streaming": {c.name: states.get(c.id) for c in online if fps.get(c.name, 0) <= 5},
    "app_private_mb": round(me.memory_full_info().private / 2**20),
    "ffmpeg_count": len(kids),
    "ffmpeg_private_mb_total": round(sum(p.memory_full_info().private for p in kids) / 2**20),
    "app_cpu_pct": me.cpu_percent(), "ffmpeg_cpu_pct_total": round(sum(p.cpu_percent() for p in kids)),
    "cpu_count": psutil.cpu_count(),
}
ctl.stop_all()
print(json.dumps(res, indent=1, ensure_ascii=False))
shutil.rmtree(tmp, ignore_errors=True)
