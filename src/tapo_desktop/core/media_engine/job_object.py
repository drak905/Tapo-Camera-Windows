"""Windows Job Object wrapper (ctypes) so ffmpeg children can never outlive the app.

The job is created with ``JOB_OBJECT_LIMIT_KILL_ON_JOB_CLOSE``: when the last handle to it is
closed (explicitly, or by the kernel when this process dies however abruptly) every process in
the job is terminated.
"""
from __future__ import annotations

import ctypes
import logging
import subprocess
import sys
import threading
from ctypes import wintypes

__all__ = ["JobObject", "default_job"]

logger = logging.getLogger(__name__)

_JOB_OBJECT_EXTENDED_LIMIT_INFORMATION_CLASS = 9
_JOB_OBJECT_LIMIT_KILL_ON_JOB_CLOSE = 0x00002000
_PROCESS_TERMINATE = 0x0001
_PROCESS_SET_QUOTA = 0x0100
_PROCESS_QUERY_LIMITED_INFORMATION = 0x1000
_STILL_ACTIVE = 259


class _IoCounters(ctypes.Structure):
    _fields_ = [
        ("ReadOperationCount", ctypes.c_ulonglong),
        ("WriteOperationCount", ctypes.c_ulonglong),
        ("OtherOperationCount", ctypes.c_ulonglong),
        ("ReadTransferCount", ctypes.c_ulonglong),
        ("WriteTransferCount", ctypes.c_ulonglong),
        ("OtherTransferCount", ctypes.c_ulonglong),
    ]


class _BasicLimitInformation(ctypes.Structure):
    _fields_ = [
        ("PerProcessUserTimeLimit", ctypes.c_int64),
        ("PerJobUserTimeLimit", ctypes.c_int64),
        ("LimitFlags", wintypes.DWORD),
        ("MinimumWorkingSetSize", ctypes.c_size_t),
        ("MaximumWorkingSetSize", ctypes.c_size_t),
        ("ActiveProcessLimit", wintypes.DWORD),
        ("Affinity", ctypes.c_size_t),
        ("PriorityClass", wintypes.DWORD),
        ("SchedulingClass", wintypes.DWORD),
    ]


class _ExtendedLimitInformation(ctypes.Structure):
    _fields_ = [
        ("BasicLimitInformation", _BasicLimitInformation),
        ("IoInfo", _IoCounters),
        ("ProcessMemoryLimit", ctypes.c_size_t),
        ("JobMemoryLimit", ctypes.c_size_t),
        ("PeakProcessMemoryUsed", ctypes.c_size_t),
        ("PeakJobMemoryUsed", ctypes.c_size_t),
    ]


_kernel32 = None
_kernel32_lock = threading.Lock()


def _k32():
    """Load kernel32 with typed prototypes (once). Raises OSError off Windows."""
    global _kernel32
    if sys.platform != "win32":
        raise OSError("Job Objects are only available on Windows")
    with _kernel32_lock:
        if _kernel32 is None:
            k32 = ctypes.WinDLL("kernel32", use_last_error=True)
            k32.CreateJobObjectW.restype = wintypes.HANDLE
            k32.CreateJobObjectW.argtypes = [ctypes.c_void_p, wintypes.LPCWSTR]
            k32.SetInformationJobObject.restype = wintypes.BOOL
            k32.SetInformationJobObject.argtypes = [wintypes.HANDLE, ctypes.c_int, ctypes.c_void_p, wintypes.DWORD]
            k32.AssignProcessToJobObject.restype = wintypes.BOOL
            k32.AssignProcessToJobObject.argtypes = [wintypes.HANDLE, wintypes.HANDLE]
            k32.TerminateJobObject.restype = wintypes.BOOL
            k32.TerminateJobObject.argtypes = [wintypes.HANDLE, wintypes.UINT]
            k32.OpenProcess.restype = wintypes.HANDLE
            k32.OpenProcess.argtypes = [wintypes.DWORD, wintypes.BOOL, wintypes.DWORD]
            k32.GetExitCodeProcess.restype = wintypes.BOOL
            k32.GetExitCodeProcess.argtypes = [wintypes.HANDLE, ctypes.POINTER(wintypes.DWORD)]
            k32.CloseHandle.restype = wintypes.BOOL
            k32.CloseHandle.argtypes = [wintypes.HANDLE]
            _kernel32 = k32
        return _kernel32


def _last_error(what: str) -> OSError:
    code = ctypes.get_last_error()
    err = ctypes.WinError(code)
    err.strerror = f"{what}: {err.strerror}"
    return err


class JobObject:
    """A Job Object created with ``JOB_OBJECT_LIMIT_KILL_ON_JOB_CLOSE``."""

    def __init__(self, name: str | None = None):
        """Create the job (optionally named). Raises OSError if the OS call fails."""
        k32 = _k32()
        self._lock = threading.Lock()
        handle = k32.CreateJobObjectW(None, name)
        if not handle:
            raise _last_error("CreateJobObjectW")
        info = _ExtendedLimitInformation()
        info.BasicLimitInformation.LimitFlags = _JOB_OBJECT_LIMIT_KILL_ON_JOB_CLOSE
        if not k32.SetInformationJobObject(
            handle, _JOB_OBJECT_EXTENDED_LIMIT_INFORMATION_CLASS, ctypes.byref(info), ctypes.sizeof(info)
        ):
            err = _last_error("SetInformationJobObject")
            k32.CloseHandle(handle)
            raise err
        self._handle: int | None = int(handle)

    def assign(self, proc: subprocess.Popen | int) -> None:
        """Assign a running process (Popen or pid) to the job.

        Raises OSError (e.g. ProcessLookupError) for a process that has exited or does not exist.
        """
        if isinstance(proc, subprocess.Popen):
            if proc.poll() is not None:
                raise ProcessLookupError(f"process {proc.pid} has already exited")
            pid = proc.pid
        else:
            pid = int(proc)
        k32 = _k32()
        with self._lock:
            job = self._handle
            if not job:
                raise OSError("job object is closed")
            access = _PROCESS_SET_QUOTA | _PROCESS_TERMINATE | _PROCESS_QUERY_LIMITED_INFORMATION
            hproc = k32.OpenProcess(access, False, pid)
            if not hproc:
                raise ProcessLookupError(f"cannot open process {pid}: WinError {ctypes.get_last_error()}")
            try:
                code = wintypes.DWORD()
                if k32.GetExitCodeProcess(hproc, ctypes.byref(code)) and code.value != _STILL_ACTIVE:
                    raise ProcessLookupError(f"process {pid} has already exited")
                if not k32.AssignProcessToJobObject(job, hproc):
                    raise _last_error(f"AssignProcessToJobObject(pid={pid})")
            finally:
                k32.CloseHandle(hproc)

    def terminate_all(self, exit_code: int = 1) -> None:
        """TerminateJobObject: kill every process in the job with ``exit_code``. The job stays usable."""
        with self._lock:
            job = self._handle
            if not job:
                raise OSError("job object is closed")
            if not _k32().TerminateJobObject(job, exit_code):
                raise _last_error("TerminateJobObject")

    def close(self) -> None:
        """Close the job handle; closing the last handle kills every assigned process. Idempotent."""
        with self._lock:
            job, self._handle = self._handle, None
        if job:
            _k32().CloseHandle(job)

    @property
    def handle(self) -> int:
        """Raw HANDLE value of the job (non-zero while open)."""
        return self._handle or 0

    def __enter__(self) -> "JobObject":
        return self

    def __exit__(self, exc_type, exc, tb) -> None:
        self.close()

    def __repr__(self) -> str:
        return f"JobObject(handle={self.handle:#x})"


_default_job: JobObject | None = None
_default_lock = threading.Lock()


def default_job() -> JobObject:
    """Process-wide singleton job; its handle is never closed explicitly, so children die with the app."""
    global _default_job
    with _default_lock:
        if _default_job is None:
            _default_job = JobObject()
            logger.debug("created process-wide job object %r", _default_job)
        return _default_job
