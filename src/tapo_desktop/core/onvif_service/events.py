"""ONVIF Events: PullPoint subscription for motion notifications [spec:onvif.events_pullpoint]."""
from __future__ import annotations

import logging
import threading
import time
import xml.etree.ElementTree as ET
from dataclasses import dataclass
from datetime import datetime, timezone
from typing import Callable
from xml.sax.saxutils import escape

from tapo_desktop.core.errors import TapoConnectionError
from tapo_desktop.core.onvif_service import _xml as X
from tapo_desktop.core.onvif_service.soap import OnvifClient, service_path
from tapo_desktop.models import MotionEvent

logger = logging.getLogger(__name__)

_ACTIVE_ITEMS = ("IsMotion", "State")
_TRUE = frozenset({"true", "1"})
_FALSE = frozenset({"false", "0"})
_SUBSCRIPTION_TTL_S = 60.0
_MAX_BACKOFF_S = 30.0
_UNSUBSCRIBE_TIMEOUT_S = 2.0
_PULL_ACTION = "http://www.onvif.org/ver10/events/wsdl/PullPointSubscription/PullMessagesRequest"
_RENEW_ACTION = "http://docs.oasis-open.org/wsn/bw-2/SubscriptionManager/RenewRequest"
_UNSUBSCRIBE_ACTION = "http://docs.oasis-open.org/wsn/bw-2/SubscriptionManager/UnsubscribeRequest"


# ----------------------------------------------------------------------------------- parsing
def _simple_items(parent: ET.Element | None) -> dict[str, str]:
    if parent is None:
        return {}
    return {el.get("Name", ""): el.get("Value", "") for el in X.iter_local(parent, "SimpleItem") if el.get("Name")}


def _parse_message(note: ET.Element, camera_host: str) -> MotionEvent | None:
    """One NotificationMessage -> MotionEvent, or None if it carries no motion state."""
    outer = X.child_local(note, "Message")          # wsnt:Message
    if outer is None:
        return None
    inner = X.child_local(outer, "Message")         # tt:Message (UtcTime, Source, Data)
    message = inner if inner is not None else outer
    data = _simple_items(X.child_local(message, "Data"))
    raw = next((data[k] for k in _ACTIVE_ITEMS if k in data), None)
    value = (raw or "").strip().lower()
    if value not in _TRUE and value not in _FALSE:
        return None
    timestamp = X.parse_utc(message.get("UtcTime", ""))
    if timestamp is None:
        logger.debug("notification without a usable UtcTime; using local time")
        timestamp = datetime.now(timezone.utc)
    extra = {"source": _simple_items(X.child_local(message, "Source")), "data": data,
             "property_operation": message.get("PropertyOperation", "")}
    return MotionEvent(camera_host=camera_host, topic=X.text_local(note, "Topic"), active=value in _TRUE,
                       timestamp=timestamp, source="onvif", data=extra)


def parse_notifications(xml_bytes: bytes, camera_host: str) -> list[MotionEvent]:
    """Parse a PullMessagesResponse (or Notify) into motion events.

    Topic is the NotificationMessage Topic text; ``active`` from the SimpleItem ``IsMotion``
    (also accepts ``State``); timestamp from Message ``UtcTime`` (UTC-aware). Tolerant: ``[]`` on garbage.
    """
    root = X.safe_parse(xml_bytes)
    return _events_from(root, camera_host) if root is not None else []


def _events_from(root: ET.Element, camera_host: str) -> list[MotionEvent]:
    events: list[MotionEvent] = []
    for note in X.iter_local(root, "NotificationMessage"):
        ev = _parse_message(note, camera_host)
        if ev is not None:
            events.append(ev)
    return events


# ----------------------------------------------------------------------------------- subscriber
@dataclass
class _Subscription:
    path: str          # path (+ query) of the SubscriptionReference address
    address: str       # full address as returned by the camera (sent as wsa:To)
    renew_at: float    # time.monotonic() deadline for the next Renew


def _remaining_ttl(resp: ET.Element, default: float) -> float:
    """TerminationTime - CurrentTime of a response (camera clock), else ``default``."""
    current = X.parse_utc(X.text_local(resp, "CurrentTime"))
    termination = X.parse_utc(X.text_local(resp, "TerminationTime"))
    if current is None or termination is None:
        return default
    return max(0.0, (termination - current).total_seconds())


class PullPointSubscriber:
    """Background PullPoint loop delivering MotionEvents to ``on_event``.

    The PullMessages requests go through ``client`` (its host/port) using the path of the
    subscription reference address. ``on_event`` runs on the subscriber thread; exceptions it
    raises are logged and do not stop the loop.
    """

    def __init__(self, client: OnvifClient, on_event: Callable[[MotionEvent], None], *,
                 pull_timeout_s: int = 5, message_limit: int = 10, retry_backoff_s: float = 1.0):
        if pull_timeout_s <= 0 or message_limit <= 0 or retry_backoff_s <= 0:
            raise ValueError("pull_timeout_s, message_limit and retry_backoff_s must be positive")
        self._client = client
        self._on_event = on_event
        self._pull_timeout_s = pull_timeout_s
        self._message_limit = int(message_limit)
        self._retry_backoff_s = float(retry_backoff_s)
        self._net_timeout = float(getattr(client, "timeout", 5.0))
        self._host = str(getattr(client, "host", ""))
        self._stop = threading.Event()
        self._thread: threading.Thread | None = None
        self._lock = threading.Lock()

    # ------------------------------------------------------------------------------- lifecycle
    def start(self) -> None:
        """Start a daemon thread: CreatePullPointSubscription, PullMessages loop, Renew before
        expiry, re-subscribe with backoff on any failure."""
        with self._lock:
            if self._thread is not None and self._thread.is_alive():
                return
            self._stop.clear()
            self._thread = threading.Thread(target=self._run, name=f"onvif-pullpoint-{self._host}", daemon=True)
            self._thread.start()

    def stop(self, timeout: float = 3.0) -> None:
        """Unsubscribe best-effort and join the thread."""
        self._stop.set()
        with self._lock:
            thread = self._thread
        if thread is None or thread is threading.current_thread():
            return
        thread.join(timeout)
        if thread.is_alive():
            logger.warning("PullPoint subscriber for %s did not stop within %.1f s", self._host, timeout)

    @property
    def running(self) -> bool:
        thread = self._thread
        return thread is not None and thread.is_alive() and not self._stop.is_set()

    # ------------------------------------------------------------------------------- worker
    def _run(self) -> None:
        sub: _Subscription | None = None
        backoff = self._retry_backoff_s
        try:
            while not self._stop.is_set():
                try:
                    if sub is None:
                        sub = self._subscribe()
                        backoff = self._retry_backoff_s
                    if time.monotonic() >= sub.renew_at:
                        self._renew(sub)
                    self._pull(sub)
                except Exception as exc:  # noqa: BLE001 - any failure means "re-subscribe"
                    if self._stop.is_set():
                        break
                    logger.warning("ONVIF PullPoint on %s failed (%s: %s); re-subscribing in %.2f s",
                                   self._host, type(exc).__name__, exc, backoff)
                    if sub is not None and not isinstance(exc, TapoConnectionError):
                        self._unsubscribe(sub)
                    sub = None
                    self._stop.wait(backoff)
                    backoff = min(backoff * 2, max(_MAX_BACKOFF_S, self._retry_backoff_s))
        finally:
            if sub is not None:
                self._unsubscribe(sub)
            logger.debug("PullPoint subscriber for %s stopped", self._host)

    def _call(self, path: str, body: str, *, action: str = "", to: str = "",
              wait_s: float | None = None, cancellable: bool = True) -> ET.Element:
        """client.call with WS-Addressing headers, long-poll timeout and stop-cancellation when the
        client is a real OnvifClient; duck-typed clients get the plain ``call(path, body)``."""
        if not isinstance(self._client, OnvifClient):
            return self._client.call(path, body)
        header = ""
        if action:
            header = f'<wsa:Action xmlns:wsa="{X.WSA}">{action}</wsa:Action>'
            if to:
                header += f'<wsa:To xmlns:wsa="{X.WSA}">{escape(to)}</wsa:To>'
        return self._client.call(path, body, header_xml=header, timeout=wait_s,
                                 cancel=self._stop if cancellable else None)

    def _schedule_renew(self, sub: _Subscription, ttl: float) -> None:
        margin = max(10.0, self._pull_timeout_s + self._net_timeout + 2.0)
        sub.renew_at = time.monotonic() + max(0.0, ttl - min(margin, ttl / 2))

    def _subscribe(self) -> _Subscription:
        path = service_path(self._client, "events")
        resp = self._call(path, f'<tev:CreatePullPointSubscription xmlns:tev="{X.TEV}">'
                                f"<tev:InitialTerminationTime>{X.fmt_duration(_SUBSCRIPTION_TTL_S)}"
                                "</tev:InitialTerminationTime></tev:CreatePullPointSubscription>")
        ref = X.find_local(resp, "SubscriptionReference")
        address = X.text_local(ref, "Address") if ref is not None else ""
        sub = _Subscription(path=X.url_path(address, default=path), address=address, renew_at=0.0)
        self._schedule_renew(sub, _remaining_ttl(resp, _SUBSCRIPTION_TTL_S))
        logger.info("ONVIF PullPoint subscription created on %s", self._host)
        return sub

    def _renew(self, sub: _Subscription) -> None:
        resp = self._call(sub.path, f'<wsnt:Renew xmlns:wsnt="{X.WSNT}"><wsnt:TerminationTime>'
                                    f"{X.fmt_duration(_SUBSCRIPTION_TTL_S)}</wsnt:TerminationTime></wsnt:Renew>",
                          action=_RENEW_ACTION, to=sub.address)
        self._schedule_renew(sub, _remaining_ttl(resp, _SUBSCRIPTION_TTL_S))

    def _pull(self, sub: _Subscription) -> None:
        body = (f'<tev:PullMessages xmlns:tev="{X.TEV}"><tev:Timeout>{X.fmt_duration(self._pull_timeout_s)}'
                f"</tev:Timeout><tev:MessageLimit>{self._message_limit}</tev:MessageLimit></tev:PullMessages>")
        resp = self._call(sub.path, body, action=_PULL_ACTION, to=sub.address,
                          wait_s=self._pull_timeout_s + self._net_timeout)
        if X.find_local(resp, "TerminationTime") is not None:
            self._schedule_renew(sub, _remaining_ttl(resp, _SUBSCRIPTION_TTL_S))
        for ev in _events_from(resp, self._host):
            self._deliver(ev)

    def _deliver(self, ev: MotionEvent) -> None:
        try:
            self._on_event(ev)
        except Exception:  # noqa: BLE001 - a consumer bug must not kill the loop
            logger.exception("on_event callback raised for %s", self._host)

    def _unsubscribe(self, sub: _Subscription) -> None:
        """Best-effort Unsubscribe (short timeout, never raises)."""
        try:
            self._call(sub.path, f'<wsnt:Unsubscribe xmlns:wsnt="{X.WSNT}"/>', action=_UNSUBSCRIBE_ACTION,
                       to=sub.address, wait_s=min(self._net_timeout, _UNSUBSCRIBE_TIMEOUT_S), cancellable=False)
        except Exception as exc:  # noqa: BLE001 - best effort
            logger.debug("Unsubscribe on %s failed: %s", self._host, exc)
