"""iMou playback supervisor: paced (-re) at a fixed rate, a clean end of the clip is not a failure, one attempt only,
the secret URL never reaches a log. The decoder is a tiny stand-in program (no network, no real ffmpeg needed)."""
from __future__ import annotations

import logging
import sys
import time

import pytest

from tapo_desktop.core.media_engine.supervisor import StreamState
from tests.fixtures.mock_imou_cloud import MOCK_RTSP_QUERY

URL = f"rtsp://203.0.113.7:554/cam/playback?channel=1&{MOCK_RTSP_QUERY}"
SCRIPT = """
import os, sys, time
w, h = int(os.environ["FAKE_W"]), int(os.environ["FAKE_H"])
for _ in range(int(os.environ.get("FAKE_FRAMES", "3"))):
    sys.stdout.buffer.write(bytes(w * h * 3)); sys.stdout.buffer.flush(); time.sleep(float(os.environ.get("FAKE_GAP", "0.05")))
if os.environ.get("FAKE_STDERR"):
    sys.stderr.write(os.environ["FAKE_STDERR"] + "\\n"); sys.stderr.flush()
sys.exit(int(os.environ.get("FAKE_CODE", "0")))
"""


def wait_for(cond, timeout=10.0):
    end = time.monotonic() + timeout
    while time.monotonic() < end:
        if cond():
            return True
        time.sleep(0.02)
    return cond()


class Run:
    """A playback supervisor whose decoder is the stand-in program above."""

    def __init__(self, tmp_path, monkeypatch, *, frames=3, code=0, gap=0.05, stderr="", stall=8.0):
        from tapo_desktop.core.imou.stream import ImouPlaybackSupervisor

        script = tmp_path / "fake_decoder.py"
        script.write_text(SCRIPT, encoding="utf-8")
        for key, value in {"FAKE_W": "64", "FAKE_H": "36", "FAKE_FRAMES": frames, "FAKE_CODE": code, "FAKE_GAP": gap,
                           "FAKE_STDERR": stderr}.items():
            monkeypatch.setenv(key, str(value))
        self.states, self.frames, self.ends = [], [], []

        class Fake(ImouPlaybackSupervisor):
            def _decoder_command(self, exe, target, hwaccel, audio_url):
                return [sys.executable, str(script)]

        self.sup = Fake(URL, 64, 36, on_frame=self.frames.append, on_state=lambda s, r: self.states.append((s.name, r)),
                        on_clip_end=lambda: self.ends.append(time.monotonic()), hwaccel="none", ffmpeg_path="unused",
                        stall_timeout=stall)

    def names(self):
        return [s for s, _r in self.states]


def test_the_playback_command_is_paced_and_fixed_rate_and_keeps_the_real_url_only_in_the_command():
    from tapo_desktop.core.imou.stream import PLAYBACK_FPS, ImouPlaybackSupervisor, ImouStreamSupervisor

    play = ImouPlaybackSupervisor(URL, 640, 360, on_frame=lambda f: None, on_clip_end=lambda: None, hwaccel="none")
    live = ImouStreamSupervisor(URL, "rtsp", 640, 360, on_frame=lambda f: None, hwaccel="none")
    cmd = play._decoder_command("ffmpeg", play._target, None, None)
    assert cmd[cmd.index("-i") + 1] == URL and "-rtsp_transport" in cmd
    assert "-re" in cmd and cmd.index("-re") < cmd.index("-i")
    assert cmd[cmd.index("-r") + 1] == str(PLAYBACK_FPS) == "10"
    assert "-probesize" not in cmd and "-analyzeduration" not in cmd
    assert play._target.url.startswith("imou-play://rtsp/") and URL not in repr(play) and MOCK_RTSP_QUERY not in repr(play)
    live_cmd = live._decoder_command("ffmpeg", live._target, None, None)
    assert "-re" not in live_cmd and "-r" not in live_cmd and live._target.url.startswith("imou-live://")


def test_a_clean_end_of_the_clip_is_not_a_failure(tmp_path, monkeypatch):
    run = Run(tmp_path, monkeypatch, frames=4, code=0)
    run.sup.start()
    assert wait_for(lambda: run.ends), run.states
    assert len(run.frames) == 4 and [f.index for f in run.frames] == [0, 1, 2, 3]
    assert "FAILED" not in run.names() and "RECONNECTING" not in run.names() and run.names()[-1] == "STOPPED"
    time.sleep(0.3)
    assert len(run.ends) == 1  # called once, no second attempt
    run.sup.stop()


def test_ending_before_the_first_frame_is_a_failure(tmp_path, monkeypatch):
    run = Run(tmp_path, monkeypatch, frames=0, code=0)
    run.sup.start()
    assert wait_for(lambda: "FAILED" in run.names()), run.states
    assert run.ends == [] and "RECONNECTING" not in run.names()
    run.sup.stop()


def test_a_crash_after_frames_is_a_failure_with_clean_text(tmp_path, monkeypatch):
    run = Run(tmp_path, monkeypatch, frames=2, code=3, stderr=f"Server returned 404 for {URL}")
    run.sup.start()
    assert wait_for(lambda: "FAILED" in run.names()), run.states
    reason = [r for s, r in run.states if s == "FAILED"][-1]
    assert run.ends == [] and MOCK_RTSP_QUERY not in reason and "rtsp://" not in reason
    assert run.sup.stats()["reconnects"] == 0
    run.sup.stop()


def test_stop_is_fast_and_ends_everything(tmp_path, monkeypatch):
    run = Run(tmp_path, monkeypatch, frames=10_000, gap=0.02)
    run.sup.start()
    assert wait_for(lambda: len(run.frames) >= 2)
    t0 = time.monotonic()
    run.sup.stop()
    assert time.monotonic() - t0 < 3.0 and run.ends == []


def test_no_log_record_holds_the_url_or_its_query(tmp_path, monkeypatch, caplog):
    caplog.set_level(logging.DEBUG)
    run = Run(tmp_path, monkeypatch, frames=2, code=1, stderr=f"open failed {URL}")
    run.sup.start()
    assert wait_for(lambda: "FAILED" in run.names())
    run.sup.stop()
    text = "\n".join(r.getMessage() for r in caplog.records) + repr(run.states)
    assert MOCK_RTSP_QUERY not in text and "203.0.113.7" not in text.replace("imou-play", "")


def test_make_supervisor_builds_the_playback_variant_only_on_request():
    from tapo_desktop.core.imou.stream import ImouPlaybackSupervisor, ImouStreamSupervisor, make_supervisor

    common = dict(url=URL, kind="rtsp", width=64, height=36, hwaccel="none", on_frame=lambda f: None,
                  on_state=lambda s, r: None)
    assert type(make_supervisor(**common)) is ImouStreamSupervisor
    assert isinstance(make_supervisor(**common, playback=True, on_clip_end=lambda: None), ImouPlaybackSupervisor)
