"""Remote (off-LAN) live view wiring (stub, red phase)."""
from __future__ import annotations

from collections.abc import Callable
from typing import Any

REMOTE = "Remote"
REMOTE_CLICK_TO_VIEW = "Remote - click to view"
NETWORK_ONLY = "Available on the camera's network only"
CAMERA_OFFLINE = "Camera is offline"
SETTING_MAX_REMOTE_STREAMS = "max_remote_streams"
DEFAULT_MAX_REMOTE_STREAMS = 4


class RemoteStreamGate:
    def __init__(self, capacity: int = DEFAULT_MAX_REMOTE_STREAMS):
        raise NotImplementedError

    def acquire(self, key: str, on_evict: Callable[[], None], *, preempt: bool = False) -> bool:
        raise NotImplementedError

    def release(self, key: str) -> None:
        raise NotImplementedError

    def holders(self) -> list[str]:
        raise NotImplementedError


class RelayProvider:
    def __init__(self, cloud_client: Any = None, *, terminal_uuid: str | None = None,
                 max_streams: int = DEFAULT_MAX_REMOTE_STREAMS, relay_factory: Callable[[Any], Any] | None = None,
                 supervisor_factory: Callable[..., Any] | None = None):
        raise NotImplementedError
