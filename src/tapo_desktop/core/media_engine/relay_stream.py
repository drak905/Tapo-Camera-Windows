"""Remote live view over the TP-Link cloud relay (stub, red phase)."""
from __future__ import annotations

from typing import Any

from tapo_desktop.core.media_engine.local_stream import LocalStreamSupervisor

__all__ = ["RelayStreamSupervisor"]


class RelayStreamSupervisor(LocalStreamSupervisor):
    def __init__(self, relay: Any, device_id: str, width: int = 640, height: int = 360, **kwargs: Any):
        raise NotImplementedError
