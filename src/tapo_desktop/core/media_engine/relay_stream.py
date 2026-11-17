"""Remote live view through the TP-Link cloud relay [spec:remote.relay.*].

:class:`RelayStreamSupervisor` is a :class:`~tapo_desktop.core.media_engine.local_stream.LocalStreamSupervisor`
whose media session per attempt is a :class:`~tapo_desktop.core.cloud.relay.RelayMediaSession` (fresh
relay slot + TLS tunnel + heartbeat) instead of a direct :8800 connection. Everything else - preview
request, pump, acks, ffmpeg decoding, reconnect/backoff, stall watchdog, lease, stats, ``switch`` - is
the LAN code path. A relay refusal that retrying cannot fix (e.g. -1007 "Camera is offline") goes to
FAILED at once; drops and other errors reconnect with a new slot.
"""
from __future__ import annotations

from collections.abc import Callable
from typing import Any

from tapo_desktop.core.cloud.relay import HEARTBEAT_INTERVAL_S, RelayClient, RelayMediaSession
from tapo_desktop.core.media_engine.job_object import JobObject
from tapo_desktop.core.media_engine.limiter import StreamLimiter
from tapo_desktop.core.media_engine.local_stream import LocalStreamSupervisor
from tapo_desktop.core.media_engine.supervisor import Frame, StreamState

__all__ = ["RelayStreamSupervisor"]


class RelayStreamSupervisor(LocalStreamSupervisor):
    """Live view of one cloud camera through the relay; same public surface as LocalStreamSupervisor.

    ``device_id`` is the cloud deviceId (also the limiter key and the label in logs/thread names);
    ``device_type`` / ``model`` / ``cloud_type`` go into the slot request; ``resolution`` ``"VGA"`` / ``"HD"``
    is both the slot's and the preview's resolution (``switch`` requests a new slot).
    """

    def __init__(
        self,
        relay: RelayClient | Any,
        device_id: str,
        width: int = 640,
        height: int = 360,
        *,
        device_type: str = "SMART.IPCAMERA",
        model: str = "",
        cloud_type: int = 2,
        resolution: str = "VGA",
        channel: int = 0,
        heartbeat_interval: float = HEARTBEAT_INTERVAL_S,
        on_frame: Callable[[Frame], None],
        on_state: Callable[[StreamState, str | None], None] | None = None,
        on_audio: Callable[[bytes], None] | None = None,
        ffmpeg_path: str | None = None,
        hwaccel: str = "auto",
        limiter: StreamLimiter | None = None,
        limiter_key: str | None = None,
        job: JobObject | None = None,
        backoff_initial: float = 0.5,
        backoff_max: float = 10.0,
        stall_timeout: float = 5.0,
        max_retries: int | None = None,
    ):
        if heartbeat_interval <= 0:
            raise ValueError("heartbeat_interval must be positive")
        self._relay = relay
        self._device_type = device_type
        self._model = model
        self._cloud_type = int(cloud_type)
        self._heartbeat_interval = float(heartbeat_interval)
        super().__init__(
            device_id, "", width, height, resolution=resolution, channel=channel, on_frame=on_frame,
            on_state=on_state, on_audio=on_audio, ffmpeg_path=ffmpeg_path, hwaccel=hwaccel, limiter=limiter,
            limiter_key=limiter_key or device_id, job=job, backoff_initial=backoff_initial,
            backoff_max=backoff_max, stall_timeout=stall_timeout, max_retries=max_retries,
            session_factory=self._relay_session,
        )

    def _pseudo_url(self, resolution: str) -> str:
        """Secret-free identifier used for logs and the thread name."""
        return f"tapo-relay://{self._host}/{self._channel}/{resolution}"

    def __repr__(self) -> str:
        return f"RelayStreamSupervisor({self._host!r}, resolution={self._target.resolution}, state={self.state.name})"

    def _relay_session(self, resolution: str) -> RelayMediaSession:
        """Fresh relay session (new slot + tunnel) for one attempt."""
        return RelayMediaSession(self._relay, self._host, device_type=self._device_type, model=self._model,
                                 resolution=resolution, cloud_type=self._cloud_type, timeout=self._io_timeout,
                                 heartbeat_interval=self._heartbeat_interval)
