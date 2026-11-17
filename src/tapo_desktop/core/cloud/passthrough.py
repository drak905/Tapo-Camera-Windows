"""Remote camera control through the TP-Link IoT cloud passthrough [spec:remote.control.*] (stub)."""
from __future__ import annotations

from typing import Any


class CloudPassthrough:
    def __init__(self, cloud_client: Any, *, timeout: float = 30.0):
        raise NotImplementedError

    def multiple_request(self, thing_name: str, app_server_url: str, reqs: list[dict]) -> list[dict]:
        raise NotImplementedError


class RemoteControlClient:
    def __init__(self, passthrough: CloudPassthrough, thing_name: str, app_server_url: str, *,
                 player_id: str | None = None):
        raise NotImplementedError
