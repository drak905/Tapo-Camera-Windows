"""Remote live view through the TP-Link cloud relay [spec:remote.relay.*] (stub, red phase)."""
from __future__ import annotations

import ssl
import threading
from dataclasses import dataclass, field
from typing import Any

from tapo_desktop.core.cloud.client import CloudError
from tapo_desktop.core.vod.session import MediaSession

__all__ = ["CIPC_SERVICE_ID", "DEFAULT_CIPC_HOST", "HEARTBEAT_BODY", "HEARTBEAT_INTERVAL_S", "MAX_REDIRECTS",
           "MSG_CAMERA_OFFLINE", "RelayClient", "RelayError", "RelayMediaSession", "RelaySlot", "cloud_type_for"]

CIPC_SERVICE_ID = "cipc.api.cloud"
DEFAULT_CIPC_HOST = "aps1-cipc-api.i.tplinkcloud.com"
HEARTBEAT_INTERVAL_S = 15.0
HEARTBEAT_BODY = b'{"type": "notification", "params": {"event_type": "heartbeat"}}'
MAX_REDIRECTS = 3
MSG_CAMERA_OFFLINE = "Camera is offline (the TP-Link cloud relay cannot reach it)."


class RelayError(CloudError):
    def __init__(self, message: str, code: int | str = "", *, fatal: bool = False, result: dict | None = None):
        super().__init__(message, code, result)
        self.fatal = fatal


def cloud_type_for(raw: dict | None) -> int:
    raise NotImplementedError


@dataclass(frozen=True)
class RelaySlot:
    device_id: str
    relay_host: str
    relay_port: int
    relay_path: str
    relay_ip: str
    track_id: str
    resolution: str
    token: str = field(repr=False)
    elb_cookie: str = field(default="", repr=False)


class RelayClient:
    def __init__(self, cloud_client: Any, *, terminal_uuid: str | None = None, timeout: float = 15.0,
                 ssl_context: ssl.SSLContext | None = None, app_version: str = "3.22.806",
                 client_model: str = "Windows"):
        raise NotImplementedError

    def cipc_base(self) -> str:
        raise NotImplementedError

    def request_slot(self, device_id: str, device_type: str = "SMART.IPCAMERA", model: str = "",
                     resolution: str = "VGA", *, cloud_type: int = 2) -> RelaySlot:
        raise NotImplementedError

    def tunnel_request(self, slot: RelaySlot, redirect_times: int = 0, arrive_latency_ms: int = 0) -> bytes:
        raise NotImplementedError

    def open_tunnel(self, slot: RelaySlot) -> ssl.SSLSocket:
        raise NotImplementedError


class RelayMediaSession(MediaSession):
    def __init__(self, relay: RelayClient, device_id: str, *, device_type: str = "SMART.IPCAMERA",
                 model: str = "", resolution: str = "VGA", cloud_type: int = 2, timeout: float = 10.0,
                 heartbeat_interval: float = HEARTBEAT_INTERVAL_S):
        super().__init__(device_id, "", port=443, timeout=timeout)
        self._hb_stop = threading.Event()

    def connect(self) -> None:
        raise NotImplementedError
