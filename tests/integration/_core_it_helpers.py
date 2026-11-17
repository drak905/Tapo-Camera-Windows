"""Lane-private helpers for lane-tdd-core integration tests (mock camera from lane-fixture)."""
from __future__ import annotations

import time

from tapo_desktop.core.control_client.client import TapoControlClient
from tapo_desktop.core.control_client.transport import HttpsTransport, PinStore

# Defaults documented in docs/CONTRACT_FIXTURE.md (MockTapoCamera constructor)
DEFAULT_CLOUD = "cloudpass"
DEFAULT_CAM_USER = "camuser"
DEFAULT_CAM_PASS = "campass"


def cloud_password(cam) -> str:
    return getattr(cam, "cloud_password", DEFAULT_CLOUD)


def camera_user(cam) -> str:
    return getattr(cam, "camera_user", DEFAULT_CAM_USER)


def camera_password(cam) -> str:
    return getattr(cam, "camera_password", DEFAULT_CAM_PASS)


def make_client(cam, *, host: str | None = None, port: int | None = None, password: str | None = None,
                pin_store: PinStore | None = None, transport=None, timeout: float = 5.0) -> TapoControlClient:
    return TapoControlClient(host or cam.host, password if password is not None else cloud_password(cam),
                             port=port or cam.https_port, transport=transport,
                             pin_store=pin_store if pin_store is not None else PinStore(), timeout=timeout)


class RecordingTransport(HttpsTransport):
    """HttpsTransport that records (path, headers) of every POST."""

    def __init__(self, *a, **kw):
        super().__init__(*a, **kw)
        self.sent: list[tuple[str, dict[str, str]]] = []

    def post(self, path, body, headers):
        self.sent.append((path, {k.lower(): v for k, v in (headers or {}).items()}))
        return super().post(path, body, headers)


def wait_until(pred, timeout: float = 3.0, interval: float = 0.02) -> bool:
    end = time.monotonic() + timeout
    while time.monotonic() < end:
        if pred():
            return True
        time.sleep(interval)
    return bool(pred())


def methods(cam) -> list[str]:
    """Flattened method names from cam.request_log (expands multipleRequest wrappers if logged)."""
    out: list[str] = []
    for r in list(cam.request_log):
        if r.get("method") == "multipleRequest":
            out.extend(i.get("method") for i in ((r.get("params") or {}).get("requests") or []))
        else:
            out.append(r.get("method"))
    return out


def logged_requests(cam, method: str) -> list[dict]:
    out: list[dict] = []
    for r in list(cam.request_log):
        items = ((r.get("params") or {}).get("requests") or []) if r.get("method") == "multipleRequest" else [r]
        out.extend(i for i in items if i.get("method") == method)
    return out
