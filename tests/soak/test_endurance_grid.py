"""Tier 3 soak: 2x2 grid of stream2 supervisors on 4 mock cameras + 500 rapid control commands.

Duration: env TAPO_SOAK_SECONDS (default 120; the release gate runs 1800).
Mock cameras run in a child process (tests/soak/_soak_mock_server.py) so that every number below
describes OUR process, as the app would run it.

Budgets
* Python process private bytes: peak < 180 MB; linear growth over the last 2/3 of the run < 2 %.
* ffmpeg children are measured separately. Cap: < 256 MB private bytes per child. Justification:
  measured on the dev box (FFmpeg 8.1, 640x360 H.264 -> rgb24 rawvideo): ~128 MB private with
  d3d11va (D3D11 runtime + driver UMD + surface pool), ~48 MB in software; 256 MB = 2x the worst
  observed. Their summed growth over the last 2/3 must stay < 5 % (decoder/pipe leak detector).
* After stop: zero ffmpeg children, handle count back within max(64, 10 %) of the pre-grid baseline.
"""
from __future__ import annotations

import gc
import json
import os
import statistics
import subprocess
import sys
import threading
import time
from pathlib import Path

import psutil
import pytest

from tapo_desktop.core.media_engine.limiter import StreamLimiter
from tapo_desktop.core.media_engine.supervisor import StreamState, StreamSupervisor

DURATION = float(os.environ.get("TAPO_SOAK_SECONDS", "120"))
SAMPLE_EVERY = 5.0
N_CAMERAS = 4
N_COMMANDS = 500
PY_PEAK_MB = 180.0
PY_GROWTH_PCT = 2.0
FFMPEG_CHILD_PEAK_MB = 256.0
FFMPEG_GROWTH_PCT = 5.0
MIN_FPS = 12.0  # mock paces ~15 fps
CONTROL_MODE = os.environ.get("TAPO_SOAK_CONTROL", "auto")  # auto | client | state

ROOT = Path(__file__).resolve().parents[2]
MB = 1024 * 1024

pytestmark = [pytest.mark.soak, pytest.mark.timeout(int(DURATION + 240))]


def _private(p: psutil.Process) -> int:
    m = p.memory_info()
    return getattr(m, "private", 0) or m.rss


def _ffmpeg_children() -> list[psutil.Process]:
    out = []
    for c in psutil.Process().children(recursive=True):
        try:
            if "ffmpeg" in c.name().lower():
                out.append(c)
        except psutil.NoSuchProcess:
            pass
    return out


def growth_pct(samples: list[tuple[float, float]]) -> float:
    """Linear growth across the window relative to the fitted start value, in percent.

    Uses the Theil-Sen estimator (median of pairwise slopes): a steady leak shows up in full while
    isolated allocator spikes (one 4-frame burst of 691 KB rgb24 buffers) do not flip the verdict.
    """
    n = len(samples)
    assert n >= 3, "not enough samples for a slope"
    slopes = sorted(
        (samples[j][1] - samples[i][1]) / (samples[j][0] - samples[i][0])
        for i in range(n) for j in range(i + 1, n) if samples[j][0] > samples[i][0]
    )
    slope = statistics.median(slopes)
    t0, t1 = samples[0][0], samples[-1][0]
    start = statistics.median(y - slope * (t - t0) for t, y in samples)
    return 100.0 * slope * (t1 - t0) / start


class _Counter:
    def __init__(self):
        self.frames = 0
        self.bad = 0
        self.states: list[tuple[float, StreamState]] = []

    def on_frame(self, f):
        self.frames += 1
        if len(f.data) != 640 * 360 * 3:
            self.bad += 1

    def on_state(self, s, _reason):
        self.states.append((time.monotonic(), s))


class _MockProcess:
    def __init__(self):
        env = dict(os.environ)
        env["PYTHONPATH"] = os.pathsep.join([str(ROOT / "src"), str(ROOT)])
        self.proc = subprocess.Popen(
            [sys.executable, "-m", "tests.soak._soak_mock_server", str(N_CAMERAS)],
            cwd=str(ROOT), env=env, stdin=subprocess.PIPE, stdout=subprocess.PIPE, text=True, bufsize=1,
        )
        line = self.proc.stdout.readline()
        if not line:
            raise RuntimeError(f"mock camera process failed to start (rc={self.proc.wait(10)})")
        self.cameras = json.loads(line)["cameras"]
        self._lock = threading.Lock()

    def call(self, cmd: str) -> dict:
        with self._lock:
            self.proc.stdin.write(cmd + "\n")
            self.proc.stdin.flush()
            return json.loads(self.proc.stdout.readline())

    def close(self):
        try:
            self.proc.stdin.write("quit\n")
            self.proc.stdin.flush()
            self.proc.wait(15)
        except Exception:
            self.proc.kill()
            self.proc.wait(5)


def _make_control(cameras):
    """Return (mode, list_of_command_callables). Media part never depends on this."""
    if CONTROL_MODE in ("auto", "client"):
        try:
            from tapo_desktop.core.control_client.client import TapoControlClient
            from tapo_desktop.core.control_client.transport import PinStore

            clients = [TapoControlClient(c["host"], c["cloud_password"], port=c["https_port"], pin_store=PinStore())
                       for c in cameras]
            for cl in clients:
                cl.login()
            return "client", clients
        except (ImportError, NotImplementedError):
            if CONTROL_MODE == "client":
                raise
    return "state", None


def test_endurance_grid(ffmpeg_path, record_property):
    me = psutil.Process()
    mock = _MockProcess()
    sups: list[StreamSupervisor] = []
    counters: list[_Counter] = []
    control_errors: list[str] = []
    commands_done = 0
    clients = None
    try:
        mode, clients = _make_control(mock.cameras)
        record_property("control_mode", mode)
        gc.collect()
        time.sleep(1.0)
        base_handles = me.num_handles()
        base_private = _private(me)

        limiter = StreamLimiter()
        for cam in mock.cameras:
            c = _Counter()
            sup = StreamSupervisor(cam["rtsp_url"], 640, 360, on_frame=c.on_frame, on_state=c.on_state,
                                   ffmpeg_path=ffmpeg_path, limiter=limiter, limiter_key=cam["host"] + str(cam["rtsp_port"]))
            counters.append(c)
            sups.append(sup)
            sup.start()
        deadline = time.monotonic() + 20
        while time.monotonic() < deadline and not all(s.state is StreamState.PLAYING for s in sups):
            time.sleep(0.1)
        assert all(s.state is StreamState.PLAYING for s in sups), [s.state for s in sups]
        t_start = time.monotonic()

        # ---- control load: 500 commands in 10 bursts across the first 90 % of the run
        def control_worker():
            nonlocal commands_done
            bursts = 10
            per_burst = N_COMMANDS // bursts
            for b in range(bursts):
                target = t_start + (DURATION * 0.9) * b / bursts
                time.sleep(max(0.0, target - time.monotonic()))
                for i in range(per_burst):
                    idx = (b * per_burst + i) % N_CAMERAS
                    try:
                        if mode == "client":
                            cl = clients[idx]
                            op = i % 4
                            if op == 0:
                                cl.set_led(i % 8 < 4)
                            elif op == 1:
                                cl.get_led()
                            elif op == 2:
                                cl.move(5 if i % 2 else -5, 0)
                            else:
                                cl.get_motion_detection()
                        else:
                            mock.call(f"mutate {idx} 1")
                        commands_done += 1
                    except Exception as exc:  # recorded and asserted at the end
                        control_errors.append(f"{type(exc).__name__}: {exc}")

        ctl = threading.Thread(target=control_worker, daemon=True)
        ctl.start()

        # ---- sampling
        samples = []  # (t, py_floor, ffmpeg_total, ffmpeg_max_child, handles, frames, n_ffmpeg, py_peak)
        stalls: list[str] = []
        prev_frames = [c.frames for c in counters]
        while time.monotonic() - t_start < DURATION:
            time.sleep(SAMPLE_EVERY)
            t = time.monotonic() - t_start
            kids = _ffmpeg_children()
            kid_priv = []
            for k in kids:
                try:
                    kid_priv.append(_private(k))
                except psutil.NoSuchProcess:
                    pass
            # 5 quick reads: the floor (min) tracks retained memory, the max feeds the peak budget
            reads = []
            for _ in range(5):
                reads.append(_private(me))
                time.sleep(0.1)
            frames = [c.frames for c in counters]
            for i, (a, b) in enumerate(zip(prev_frames, frames)):
                if b <= a:
                    stalls.append(f"stream {i} produced no frame in sample ending t={t:.0f}s")
            prev_frames = frames
            samples.append((t, min(reads), sum(kid_priv), max(kid_priv or [0]), me.num_handles(), frames,
                            len(kids), max(reads)))
        ctl.join(timeout=60)
        elapsed = time.monotonic() - t_start
        tail = samples[len(samples) // 3:]  # last 2/3 of the run
        py_growth = growth_pct([(s[0], s[1]) for s in tail])
        ff_growth = growth_pct([(s[0], s[2]) for s in tail])

        # ---- report
        report = {
            "duration_s": round(elapsed, 1),
            "control_mode": mode,
            "commands_done": commands_done,
            "py_private_floor_mb": [round(s[1] / MB, 1) for s in samples],
            "py_private_peak_mb": [round(s[7] / MB, 1) for s in samples],
            "py_growth_pct_last_2_3": round(py_growth, 3),
            "ffmpeg_total_private_mb": [round(s[2] / MB, 1) for s in samples],
            "ffmpeg_max_child_private_mb": [round(s[3] / MB, 1) for s in samples],
            "ffmpeg_growth_pct_last_2_3": round(ff_growth, 3),
            "handles": [s[4] for s in samples],
            "base_handles": base_handles,
            "base_private_mb": round(base_private / MB, 1),
            "stats": [s.stats() for s in sups],
        }
        print("SOAK REPORT " + json.dumps(report))
        for k, v in report.items():
            record_property(k, json.dumps(v))

        # ---- media assertions
        assert not stalls, stalls[:5]
        assert all(s.state is StreamState.PLAYING for s in sups), [s.state for s in sups]
        assert all(c.bad == 0 for c in counters)
        for i, s in enumerate(sups):
            st = s.stats()
            assert st["reconnects"] == 0, f"stream {i} reconnected without any fault: {st}"
            assert counters[i].frames / elapsed >= MIN_FPS, f"stream {i}: {counters[i].frames / elapsed:.1f} fps"
        assert all(s[6] == N_CAMERAS for s in samples), "ffmpeg child count changed during the run"

        py_peak = max(s[7] for s in samples) / MB
        assert py_peak < PY_PEAK_MB, f"python private bytes peak {py_peak:.1f} MB"
        assert py_growth < PY_GROWTH_PCT, f"python memory grows {py_growth:.2f} % over last 2/3"

        ff_peak = max(s[3] for s in samples) / MB
        assert ff_peak < FFMPEG_CHILD_PEAK_MB, f"ffmpeg child private peak {ff_peak:.1f} MB"
        assert ff_growth < FFMPEG_GROWTH_PCT, f"ffmpeg memory grows {ff_growth:.2f} % over last 2/3"

        # ---- teardown assertions
        for s in sups:
            t0 = time.monotonic()
            s.stop()
            assert time.monotonic() - t0 < 3.0
        sups_stopped = list(sups)
        sups.clear()
        deadline = time.monotonic() + 2.0
        while _ffmpeg_children() and time.monotonic() < deadline:
            time.sleep(0.05)
        assert _ffmpeg_children() == [], "ffmpeg children left after stop()"
        assert all(s.state is StreamState.STOPPED for s in sups_stopped)
        if clients:
            for cl in clients:
                cl.close()
            clients = None
        gc.collect()
        time.sleep(1.0)
        handles = me.num_handles()
        allowed = base_handles + max(64, int(base_handles * 0.10))
        assert handles <= allowed, f"handles {handles} vs baseline {base_handles} (allowed {allowed})"

        # ---- control-plane assertions (after media so media failures are reported first)
        assert not control_errors, control_errors[:5]
        assert commands_done == N_COMMANDS
    finally:
        for s in sups:
            try:
                s.stop()
            except Exception:
                pass
        if clients:
            for cl in clients:
                try:
                    cl.close()
                except Exception:
                    pass
        mock.close()
