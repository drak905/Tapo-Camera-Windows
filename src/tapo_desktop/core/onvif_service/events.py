"""ONVIF Events: PullPoint subscription for motion notifications [spec:onvif.events_pullpoint]."""
from __future__ import annotations

from typing import Callable

from tapo_desktop.core.onvif_service.soap import OnvifClient
from tapo_desktop.models import MotionEvent


def parse_notifications(xml_bytes: bytes, camera_host: str) -> list[MotionEvent]:
    """Parse a PullMessagesResponse (or Notify) into motion events.

    Topic is the NotificationMessage Topic text; ``active`` from the SimpleItem ``IsMotion``
    (also accepts ``State``); timestamp from Message ``UtcTime`` (UTC-aware). Tolerant: ``[]`` on garbage.
    """
    raise NotImplementedError


class PullPointSubscriber:
    """Background PullPoint loop delivering MotionEvents to ``on_event``.

    The PullMessages requests go through ``client`` (its host/port) using the path of the
    subscription reference address.
    """

    def __init__(self, client: OnvifClient, on_event: Callable[[MotionEvent], None], *,
                 pull_timeout_s: int = 5, message_limit: int = 10, retry_backoff_s: float = 1.0):
        raise NotImplementedError

    def start(self) -> None:
        """Start a daemon thread: CreatePullPointSubscription, PullMessages loop, Renew before
        expiry, re-subscribe with backoff on any failure."""
        raise NotImplementedError

    def stop(self, timeout: float = 3.0) -> None:
        """Unsubscribe best-effort and join the thread."""
        raise NotImplementedError

    @property
    def running(self) -> bool:
        raise NotImplementedError
