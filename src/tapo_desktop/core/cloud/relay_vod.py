"""Remote SD-card playback / download through the TP-Link cloud relay (stub)."""
from __future__ import annotations

import pathlib
import threading
from collections.abc import Callable
from typing import Any

from tapo_desktop.models import RecordingClip


def build_playback_request(clip: RecordingClip, *, client_id: int = 1, channel: int = 0) -> dict:
    raise NotImplementedError


class RemoteRecordingDownloader:
    def __init__(self, relay: Any, device_id: str, **kwargs: Any):
        raise NotImplementedError

    def download(self, clip: RecordingClip, out_path: pathlib.Path, *,
                 progress: Callable[[float], None] | None = None,
                 cancel: threading.Event | None = None) -> pathlib.Path:
        raise NotImplementedError
