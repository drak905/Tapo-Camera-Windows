"""Tier 1: Windows Job Object (KILL_ON_JOB_CLOSE) so ffmpeg can never become a zombie."""
from __future__ import annotations

import ctypes
import os
import subprocess
import sys
import time
from ctypes import wintypes
from pathlib import Path

import psutil
import pytest

from tapo_desktop.core.media_engine.job_object import JobObject, default_job

pytestmark = [pytest.mark.unit, pytest.mark.skipif(sys.platform != "win32", reason="Windows Job Objects")]

ROOT = Path(__file__).resolve().parents[2]
SLEEPER = [sys.executable, "-c", "import time; time.sleep(60)"]
_CREATE_NO_WINDOW = 0x08000000


def _spawn() -> subprocess.Popen:
    return subprocess.Popen(SLEEPER, creationflags=_CREATE_NO_WINDOW)


def _is_in_job(pid: int, job_handle: int) -> bool:
    k32 = ctypes.WinDLL("kernel32", use_last_error=True)
    k32.OpenProcess.restype = wintypes.HANDLE
    k32.OpenProcess.argtypes = [wintypes.DWORD, wintypes.BOOL, wintypes.DWORD]
    k32.IsProcessInJob.argtypes = [wintypes.HANDLE, wintypes.HANDLE, ctypes.POINTER(wintypes.BOOL)]
    k32.CloseHandle.argtypes = [wintypes.HANDLE]
    h = k32.OpenProcess(0x1000, False, pid)  # PROCESS_QUERY_LIMITED_INFORMATION
    assert h, ctypes.get_last_error()
    try:
        result = wintypes.BOOL()
        assert k32.IsProcessInJob(h, wintypes.HANDLE(job_handle), ctypes.byref(result))
        return bool(result.value)
    finally:
        k32.CloseHandle(h)


@pytest.fixture
def procs():
    spawned: list[subprocess.Popen] = []
    yield spawned
    for p in spawned:
        if p.poll() is None:
            p.kill()
            p.wait(5)


def test_handle_is_nonzero_int():
    job = JobObject()
    try:
        assert isinstance(job.handle, int)
        assert job.handle != 0
    finally:
        job.close()


def test_named_job():
    job = JobObject(name=f"tapo-test-job-{os.getpid()}-{time.monotonic_ns()}")
    try:
        assert job.handle != 0
    finally:
        job.close()


def test_assign_puts_process_in_job(procs):
    job = JobObject()
    p = _spawn()
    procs.append(p)
    job.assign(p)
    assert _is_in_job(p.pid, job.handle)
    job.close()


def test_assign_by_pid(procs):
    job = JobObject()
    p = _spawn()
    procs.append(p)
    job.assign(p.pid)
    assert _is_in_job(p.pid, job.handle)
    job.close()
    assert p.wait(timeout=2) is not None


def test_close_kills_child_within_200ms(procs):
    job = JobObject()
    p = _spawn()
    procs.append(p)
    job.assign(p)
    time.sleep(0.2)
    assert p.poll() is None
    t0 = time.monotonic()
    job.close()
    try:
        p.wait(timeout=0.2)
    except subprocess.TimeoutExpired:
        pytest.fail("child survived job close for > 200 ms")
    assert time.monotonic() - t0 < 0.25
    assert not psutil.pid_exists(p.pid) or psutil.Process(p.pid).status() == psutil.STATUS_ZOMBIE


def test_close_kills_all_children(procs):
    job = JobObject()
    kids = [_spawn() for _ in range(3)]
    procs.extend(kids)
    for k in kids:
        job.assign(k)
    job.close()
    for k in kids:
        k.wait(timeout=1)
        assert k.returncode is not None


def test_close_is_idempotent(procs):
    job = JobObject()
    job.close()
    job.close()


def test_terminate_all_uses_exit_code_and_job_stays_usable(procs):
    job = JobObject()
    kids = [_spawn(), _spawn()]
    procs.extend(kids)
    for k in kids:
        job.assign(k)
    job.terminate_all(exit_code=7)
    for k in kids:
        assert k.wait(timeout=1) == 7
    again = _spawn()
    procs.append(again)
    job.assign(again)
    assert _is_in_job(again.pid, job.handle)
    job.close()
    assert again.wait(timeout=1) is not None


def test_terminate_all_on_empty_job_is_noop():
    job = JobObject()
    try:
        job.terminate_all()
    finally:
        job.close()


def test_assign_exited_popen_raises_oserror():
    job = JobObject()
    try:
        p = subprocess.Popen([sys.executable, "-c", "pass"], creationflags=_CREATE_NO_WINDOW)
        p.wait(10)
        with pytest.raises(OSError):
            job.assign(p)
    finally:
        job.close()


def test_assign_nonexistent_pid_raises_oserror():
    job = JobObject()
    try:
        pid = 4_000_000 - 4  # Windows PIDs are multiples of 4, this one is not in use
        while psutil.pid_exists(pid):
            pid -= 4
        with pytest.raises(OSError):
            job.assign(pid)
    finally:
        job.close()


def test_unassigned_process_survives_job_close(procs):
    job = JobObject()
    p = _spawn()
    procs.append(p)
    job.close()
    time.sleep(0.1)
    assert p.poll() is None


def test_default_job_is_singleton():
    a = default_job()
    b = default_job()
    assert a is b
    assert a.handle != 0


def test_default_job_children_die_when_parent_process_exits():
    script = (
        "import os, subprocess, sys\n"
        "from tapo_desktop.core.media_engine.job_object import default_job\n"
        "job = default_job()\n"
        "p = subprocess.Popen([sys.executable, '-c', 'import time; time.sleep(60)'], creationflags=0x08000000)\n"
        "try:\n"
        "    job.assign(p)\n"
        "except BaseException:\n"
        "    p.kill()\n"
        "    raise\n"
        "print(p.pid, flush=True)\n"
        "os._exit(0)\n"  # abrupt: no atexit / finalizers, only the kernel closing handles
    )
    env = dict(os.environ)
    env["PYTHONPATH"] = os.pathsep.join([str(ROOT / "src"), str(ROOT)])
    out = subprocess.run(
        [sys.executable, "-c", script], capture_output=True, text=True, timeout=30, env=env,
        creationflags=_CREATE_NO_WINDOW,
    )
    assert out.returncode == 0, out.stderr
    grandchild = int(out.stdout.strip().splitlines()[-1])
    deadline = time.monotonic() + 2.0
    while time.monotonic() < deadline and psutil.pid_exists(grandchild):
        try:
            if psutil.Process(grandchild).status() == psutil.STATUS_ZOMBIE:
                break
        except psutil.NoSuchProcess:
            break
        time.sleep(0.02)
    alive = psutil.pid_exists(grandchild)
    if alive:  # don't leak it whatever happens
        try:
            psutil.Process(grandchild).kill()
        except psutil.NoSuchProcess:
            alive = False
    assert not alive, "grandchild outlived the parent: default_job() is not kill-on-close"
