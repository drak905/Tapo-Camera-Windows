"""VIEW-ONLY real-device check of SD-card playback streaming through the app classes (AccountManager +
AppController + TapoCameraService + PlaybackStreamSupervisor / RelayPlaybackStreamSupervisor).

Restores the stored TP-Link ID session exactly like the app (cloud login, device list, local access
tokens, TDP), then for the LAN camera (default 192.168.1.10, :8800 with its local access token) and
the remote camera (default name contains "Remote Camera 1", cloud relay): lists today's recordings
(yesterday's when today has none), plays from a clip in the middle of the list for ~6 s, pauses 2 s,
resumes 3 s, seeks to the next clip for ~4 s and stops. Reports frames, the position rate (recording
seconds per wall second), whether the pause froze the picture, where the seek landed and the states.
Nothing on the cameras is changed; no tokens, passwords or images are written.

Usage: python tools/diag_playback_real.py [lan_ip] [remote_name_substring]
"""
from __future__ import annotations

import json
import os
import sys
import time
from datetime import date, timedelta
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
sys.path[:0] = [str(ROOT / "src")]
os.environ.setdefault("QT_QPA_PLATFORM", "offscreen")

from PyQt6.QtWidgets import QApplication  # noqa: E402

from tapo_desktop.core.auth.vault import CredentialVault  # noqa: E402
from tapo_desktop.services.account import AccountManager  # noqa: E402
from tapo_desktop.services.config_store import ConfigStore, default_data_dir  # noqa: E402
from tapo_desktop.services.controller import AppController  # noqa: E402
from tapo_desktop.ui.player_window import local_day_range  # noqa: E402


def pump(app, cond, timeout):
    end = time.monotonic() + timeout
    while time.monotonic() < end:
        app.processEvents()
        if cond():
            return True
        time.sleep(0.02)
    return bool(cond())


def wait(app, seconds):
    pump(app, lambda: False, seconds)


class Probe:
    def __init__(self, ctl, cam_id):
        self.cam_id = cam_id
        self.frames: list[float] = []
        self.sizes: set = set()
        self.states: list[str] = []
        self.positions: list[tuple[float, float]] = []
        ctl.playback_frame.connect(self._frame)
        ctl.playback_state.connect(self._state)
        ctl.playback_position.connect(self._position)

    def _frame(self, cid, img):
        if cid == self.cam_id:
            self.frames.append(time.monotonic())
            self.sizes.add((img.width(), img.height()))

    def _state(self, cid, s, r):
        if cid == self.cam_id:
            self.states.append(s + (f": {r}" if r else ""))

    def _position(self, cid, u):
        if cid == self.cam_id:
            self.positions.append((time.monotonic(), u))

    def frames_between(self, t0, t1):
        return sum(1 for t in self.frames if t0 <= t < t1)

    def pos_at(self, t):
        before = [u for ts, u in self.positions if ts <= t]
        return before[-1] if before else None


def clips_for(app, ctl, cam_id):
    got: dict = {}
    ctl.recordings_ready.connect(lambda cid, day, lst: got.__setitem__((cid, day), lst))
    for day in (date.today(), date.today() - timedelta(days=1)):
        ctl.load_recordings(cam_id, day)
        pump(app, lambda: (cam_id, day) in got, 45)
        clips = got.get((cam_id, day)) or []
        if clips:
            return day, sorted(clips, key=lambda c: c.start)
    return None, []


def check(app, ctl, cam) -> dict:
    out: dict = {"camera": cam.name, "transport": "relay" if (not cam.lan_found or cam.role) else "local"}
    day, clips = clips_for(app, ctl, cam.id)
    out["day"], out["clips"] = str(day), len(clips)
    if not clips:
        return out
    i = max(0, len(clips) // 2 - 1)
    clip, nxt = clips[i], clips[min(i + 1, len(clips) - 1)]
    probe = Probe(ctl, cam.id)
    t_start = time.monotonic()
    ctl.start_playback(cam.id, clip.start, local_day_range(day)[1], clips=clips, width=640, height=360)
    ok = pump(app, lambda: bool(probe.frames), 25)
    out["first_frame_s"] = round(probe.frames[0] - t_start, 2) if ok else None
    if not ok:
        out["states"] = probe.states[:6]
        ctl.stop_playback(cam.id)
        return out
    t0 = time.monotonic()
    wait(app, 6.0)
    t1 = time.monotonic()
    p0, p1 = probe.pos_at(t0), probe.pos_at(t1)
    out["play_6s"] = {"frames": probe.frames_between(t0, t1),
                      "position_rate": round((p1 - p0) / (t1 - t0), 2) if p0 and p1 else None,
                      "offset_into_clip_s": round(p1 - clip.start.timestamp(), 1) if p1 else None}
    ctl.pause_playback(cam.id)
    wait(app, 0.6)
    t2 = time.monotonic()
    wait(app, 2.0)
    t3 = time.monotonic()
    out["paused_2s"] = {"frames": probe.frames_between(t2, t3),
                        "position_moved_s": round((probe.pos_at(t3) or 0) - (probe.pos_at(t2) or 0), 2)}
    ctl.resume_playback(cam.id)
    wait(app, 3.0)
    t4 = time.monotonic()
    out["resumed_3s_frames"] = probe.frames_between(t3, t4)
    target = nxt.start.timestamp() if nxt is not clip else clip.start.timestamp() + 5
    ctl.seek_playback(cam.id, target)
    wait(app, 5.0)
    t5 = time.monotonic()
    p5 = probe.pos_at(t5)
    out["seek"] = {"target_offset_from_clip_start_s": round(target - clip.start.timestamp(), 1),
                   "landed_minus_target_s": round(p5 - target, 1) if p5 else None,
                   "frames_after_seek": probe.frames_between(t4 + 1.5, t5)}
    t6 = time.monotonic()
    ctl.stop_playback(cam.id)
    ctl.wait_idle(5000)
    out["stop_s"] = round(time.monotonic() - t6, 2)
    out["frame_sizes"] = sorted(probe.sizes)
    out["states"] = probe.states[:10]
    return out


def main(lan_ip: str, remote_name: str) -> int:
    app = QApplication(["tapo-playback-check"])
    data = default_data_dir()
    ctl = AppController(ConfigStore(data / "config.json"), CredentialVault(data / "vault.json"))
    acct = AccountManager(ctl)
    refreshed: list = []
    acct.cameras_refreshed.connect(lambda *a: refreshed.append(a))
    restored = acct.restore_session()
    pump(app, lambda: bool(refreshed), 45)
    cams = ctl.cameras()
    lan = next((c for c in cams if c.host == lan_ip and c.lan_found), None)
    remote = next((c for c in cams if remote_name.lower() in c.name.lower() and c.online), None)
    results = {"restored": restored, "lan_found": lan is not None, "remote_found": remote is not None, "checks": []}
    try:
        for cam in (lan, remote):
            if cam is not None:
                results["checks"].append(check(app, ctl, cam))
    finally:
        ctl.stop_all()
    print(json.dumps(results, indent=2, ensure_ascii=False, default=str))
    ok = results["checks"] and all(c.get("play_6s", {}).get("frames") for c in results["checks"])
    return 0 if ok else 1


if __name__ == "__main__":
    args = sys.argv[1:]
    sys.exit(main(args[0] if args else "192.168.1.10", args[1] if len(args) > 1 else "Remote Camera 1"))
