"""Remote (off-LAN) live view wiring: the relay provider of the signed-in account and the remote stream gate.

* :class:`RelayProvider` - owned by :class:`~tapo_desktop.services.account.AccountManager` (one per
  manager); attached to the signed-in cloud client and handed to the controller
  (``AppController.set_remote_provider``), which gives every :class:`TapoCameraService` a getter for it.
  It builds the :class:`~tapo_desktop.core.cloud.relay.RelayClient` lazily and creates
  :class:`~tapo_desktop.core.media_engine.relay_stream.RelayStreamSupervisor` instances with the
  device type / ``cloudType`` from the cloud device list.
* :class:`RemoteStreamGate` - at most ``max_remote_streams`` (setting, default 4) relay streams at once,
  so a 3x3 grid of remote cameras does not open 9 relay sessions. Extra tiles show
  :data:`REMOTE_CLICK_TO_VIEW`; clicking one takes the slot of the remote stream that has been running
  longest.

Only live view goes through the relay; controls, playback and events stay LAN-only
(:data:`NETWORK_ONLY`).
"""
from __future__ import annotations

import logging
import threading
from collections import OrderedDict
from collections.abc import Callable, Iterable
from typing import Any

from tapo_desktop.models import CameraConfig, CloudDevice

log = logging.getLogger(__name__)

#: Camera-list status of a cloud camera that is online but not on this LAN (viewable through the relay).
REMOTE = "Remote"
#: Stream state reason (FAILED) of a remote tile waiting for a free relay slot.
REMOTE_CLICK_TO_VIEW = "Remote - click to view"
#: Error text for controls / playback / events of a camera that is only reachable through the relay.
NETWORK_ONLY = "Available on the camera's network only"
#: Stream failure reason for cloud cameras the cloud reports offline.
CAMERA_OFFLINE = "Camera is offline"
SETTING_MAX_REMOTE_STREAMS = "max_remote_streams"
DEFAULT_MAX_REMOTE_STREAMS = 4
MAX_REMOTE_STREAMS_LIMIT = 16


def max_remote_streams(value: Any) -> int:
    """Validated ``max_remote_streams`` setting (1..16); anything else -> the default 4."""
    if isinstance(value, bool) or not isinstance(value, int) or not 1 <= value <= MAX_REMOTE_STREAMS_LIMIT:
        return DEFAULT_MAX_REMOTE_STREAMS
    return value


class RemoteStreamGate:
    """Thread-safe counting gate for remote streams, keyed by camera id (in acquisition order).

    ``acquire(key, on_evict)``: True if ``key`` holds (or now holds) a slot. When full, ``preempt=True``
    evicts the oldest holder: it is removed first, then its ``on_evict()`` runs (outside the lock, on the
    caller's thread) so it can stop its stream.
    """

    def __init__(self, capacity: int = DEFAULT_MAX_REMOTE_STREAMS):
        if isinstance(capacity, bool) or not isinstance(capacity, int) or capacity < 1:
            raise ValueError("capacity must be a positive int")
        self._capacity = capacity
        self._lock = threading.Lock()
        self._holders: OrderedDict[str, Callable[[], None]] = OrderedDict()

    def __repr__(self) -> str:
        return f"RemoteStreamGate({len(self._holders)}/{self._capacity})"

    @property
    def capacity(self) -> int:
        return self._capacity

    def set_capacity(self, capacity: int) -> None:
        """Change the limit (already running streams keep their slot)."""
        if isinstance(capacity, bool) or not isinstance(capacity, int) or capacity < 1:
            raise ValueError("capacity must be a positive int")
        with self._lock:
            self._capacity = capacity

    def acquire(self, key: str, on_evict: Callable[[], None], *, preempt: bool = False) -> bool:
        evicted: Callable[[], None] | None = None
        with self._lock:
            if key in self._holders:
                return True  # already holds a slot (its first on_evict stays registered)
            if len(self._holders) >= self._capacity:
                if not preempt:
                    return False
                victim, evicted = self._holders.popitem(last=False)
                log.info("remote stream limit (%d): %s gives its slot to %s", self._capacity, victim, key)
            self._holders[key] = on_evict
        if evicted is not None:
            try:
                evicted()
            except Exception:
                log.exception("stopping an evicted remote stream failed")
        return True

    def release(self, key: str) -> None:
        """Give the slot of ``key`` back (no-op if it holds none)."""
        with self._lock:
            self._holders.pop(key, None)

    def holds(self, key: str) -> bool:
        with self._lock:
            return key in self._holders

    def holders(self) -> list[str]:
        """Keys holding a slot, oldest first."""
        with self._lock:
            return list(self._holders)


def _default_relay_factory(cloud_client: Any) -> Any:
    from tapo_desktop.core.cloud.relay import RelayClient

    return RelayClient(cloud_client, terminal_uuid=getattr(cloud_client, "terminal_uuid", None))


def _default_supervisor_factory(*args: Any, **kwargs: Any) -> Any:
    from tapo_desktop.core.media_engine.relay_stream import RelayStreamSupervisor

    return RelayStreamSupervisor(*args, **kwargs)


class RelayProvider:
    """Remote live view for the cameras of one signed-in TP-Link ID (thread-safe).

    ``relay_factory(cloud_client) -> RelayClient`` and ``supervisor_factory(relay, device_id, width,
    height, **kwargs) -> supervisor`` replace the core classes (tests).
    """

    def __init__(self, cloud_client: Any = None, *, terminal_uuid: str | None = None,
                 max_streams: int = DEFAULT_MAX_REMOTE_STREAMS, relay_factory: Callable[[Any], Any] | None = None,
                 supervisor_factory: Callable[..., Any] | None = None):
        self._lock = threading.Lock()
        self._cloud = cloud_client
        self._terminal_uuid = terminal_uuid
        self._relay_factory = relay_factory or _default_relay_factory
        self._supervisor_factory = supervisor_factory or _default_supervisor_factory
        self._relay: Any = None
        self._devices: dict[str, CloudDevice] = {}
        self.gate = RemoteStreamGate(max_streams)

    def __repr__(self) -> str:
        return f"RelayProvider(available={self.available}, gate={self.gate!r})"

    @property
    def available(self) -> bool:
        """True while a cloud client is attached (signed in)."""
        return self._cloud is not None

    def attach(self, cloud_client: Any) -> None:
        """Use ``cloud_client`` (a new sign-in); the relay client is rebuilt on next use."""
        with self._lock:
            if cloud_client is not self._cloud:
                self._cloud, self._relay = cloud_client, None

    def detach(self) -> None:
        """Signed out: no new remote streams (running ones are stopped by their services)."""
        with self._lock:
            self._cloud, self._relay = None, None

    def update_devices(self, devices: Iterable[CloudDevice]) -> None:
        """Remember the cloud device list entries (device type, IoT-cloud flag) by deviceId."""
        with self._lock:
            self._devices = {d.device_id: d for d in devices if getattr(d, "device_id", "")}

    def relay_client(self) -> Any:
        """The RelayClient of the attached account (created on first use)."""
        with self._lock:
            if self._cloud is None:
                from tapo_desktop.core.errors import TapoConnectionError

                raise TapoConnectionError("Not signed in to a TP-Link ID.")
            if self._relay is None:
                self._relay = self._relay_factory(self._cloud)
            return self._relay

    def stream_params(self, config: CameraConfig) -> dict[str, Any]:
        """``device_type`` / ``model`` / ``cloud_type`` for the slot request of ``config``."""
        from tapo_desktop.core.cloud.relay import cloud_type_for

        device_id = config.device_id or config.id
        with self._lock:
            dev = self._devices.get(device_id)
        device_type = (dev.device_type if dev is not None and dev.device_type else "") or "SMART.IPCAMERA"
        return {"device_type": device_type, "model": config.model or (dev.model if dev is not None else ""),
                "cloud_type": cloud_type_for(dev.raw if dev is not None else None)}

    def create_stream(self, config: CameraConfig, width: int, height: int, *, resolution: str, channel: int = 0,
                      **callbacks: Any) -> Any:
        """A (not started) relay stream supervisor for ``config``; ``callbacks`` are the supervisor
        keywords of the service (``on_frame``, ``on_state``, ``limiter``, ``limiter_key`` ...)."""
        relay = self.relay_client()
        device_id = config.device_id or config.id
        return self._supervisor_factory(relay, device_id, width, height, resolution=resolution, channel=channel,
                                        **self.stream_params(config), **callbacks)
