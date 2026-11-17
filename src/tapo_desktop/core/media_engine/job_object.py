"""Windows Job Object wrapper (ctypes) so ffmpeg children can never outlive the app.

Wave 2 stub: signatures and docstrings only (see docs/CONTRACT_MEDIA.md).
"""
from __future__ import annotations

import subprocess

__all__ = ["JobObject", "default_job"]


class JobObject:
    """A Job Object created with ``JOB_OBJECT_LIMIT_KILL_ON_JOB_CLOSE``."""

    def __init__(self, name: str | None = None):
        """Create the job (optionally named). Raises OSError if the OS call fails."""
        raise NotImplementedError

    def assign(self, proc: subprocess.Popen | int) -> None:
        """Assign a running process (Popen or pid) to the job.

        Raises OSError (e.g. ProcessLookupError) for a process that has exited or does not exist.
        """
        raise NotImplementedError

    def terminate_all(self, exit_code: int = 1) -> None:
        """TerminateJobObject: kill every process in the job with ``exit_code``. The job stays usable."""
        raise NotImplementedError

    def close(self) -> None:
        """Close the job handle; closing the last handle kills every assigned process. Idempotent."""
        raise NotImplementedError

    @property
    def handle(self) -> int:
        """Raw HANDLE value of the job (non-zero while open)."""
        raise NotImplementedError


def default_job() -> JobObject:
    """Process-wide singleton job; its handle is never closed explicitly, so children die with the app."""
    raise NotImplementedError
