"""Tier 1: ONVIF notification parsing and PullPointSubscriber loop with a fake OnvifClient."""
from __future__ import annotations

import datetime as dt
import threading
import time

import pytest
from hypothesis import given, settings
from hypothesis import strategies as st

from tapo_desktop.core.errors import TapoConnectionError
from tapo_desktop.core.onvif_service.events import PullPointSubscriber, parse_notifications
from tapo_desktop.models import MotionEvent
from tests.unit._onvif_samples import (
    MOTION_TOPIC, PULL_MESSAGES_XML, FakeOnvifClient, envelope, notification, pull_messages_body,
)

pytestmark = pytest.mark.unit
UTC = dt.timezone.utc


# --------------------------------------------------------------------------- parse_notifications
def test_parse_pull_messages_motion_true_false():
    events = parse_notifications(PULL_MESSAGES_XML, "10.0.0.5")
    assert len(events) == 2
    a, b = events
    assert isinstance(a, MotionEvent)
    assert (a.camera_host, a.topic, a.active, a.source) == ("10.0.0.5", MOTION_TOPIC, True, "onvif")
    assert a.timestamp == dt.datetime(2025, 1, 3, 10, 0, 1, tzinfo=UTC)
    assert (b.active, b.topic) == (False, MOTION_TOPIC)
    assert b.timestamp == dt.datetime(2025, 1, 3, 10, 0, 4, 250000, tzinfo=UTC)
    assert a.timestamp.tzinfo is not None


def test_parse_single_notification():
    xml = envelope(pull_messages_body(notification(True, "2024-06-01T12:00:00Z"))).encode()
    [ev] = parse_notifications(xml, "cam")
    assert ev.active is True and ev.camera_host == "cam"


def test_parse_state_item_variant():
    xml = envelope(pull_messages_body(notification(True, "2024-06-01T12:00:00Z",
                                                   topic="tns1:VideoSource/MotionAlarm", item="State"))).encode()
    [ev] = parse_notifications(xml, "cam")
    assert ev.active is True and ev.topic == "tns1:VideoSource/MotionAlarm"


def test_parse_empty_pull_response():
    assert parse_notifications(envelope(pull_messages_body()).encode(), "cam") == []


@pytest.mark.parametrize("garbage", [b"", b"garbage", b"<x>", b"\xff\xfe\x00<", b"<a><b/></a>",
                                     envelope("<tev:PullMessagesResponse><wsnt:NotificationMessage/>"
                                              "</tev:PullMessagesResponse>").encode()])
def test_parse_notifications_garbage(garbage):
    assert parse_notifications(garbage, "cam") == []


@given(st.binary(max_size=500))
@settings(max_examples=150, deadline=None)
def test_parse_notifications_never_raises(data):
    assert isinstance(parse_notifications(data, "cam"), list)


@given(utc=st.text(max_size=30), topic=st.text(max_size=30))
@settings(max_examples=100, deadline=None)
def test_parse_notifications_tolerates_odd_fields(utc, topic):
    def esc(s):
        return s.replace("&", "&amp;").replace("<", "&lt;").replace('"', "&quot;")
    xml = envelope(pull_messages_body(notification(True, esc(utc), topic=esc(topic)))).encode("utf-8", "replace")
    assert isinstance(parse_notifications(xml, "cam"), list)


# --------------------------------------------------------------------------- PullPointSubscriber
def _wait(pred, timeout=3.0):
    end = time.monotonic() + timeout
    while time.monotonic() < end:
        if pred():
            return True
        time.sleep(0.02)
    return pred()


def test_subscriber_delivers_events_and_uses_subscription_path():
    fake = FakeOnvifClient()
    got: list[MotionEvent] = []
    lock = threading.Lock()

    def on_event(ev):
        with lock:
            got.append(ev)

    sub = PullPointSubscriber(fake, on_event, pull_timeout_s=1, retry_backoff_s=0.05)
    sub.start()
    try:
        assert sub.running
        fake.pull_queue.append(notification(True, "2025-01-03T10:00:01Z"))
        assert _wait(lambda: len(got) >= 1), f"no event delivered; ops={fake.ops()}"
        fake.pull_queue.append(notification(False, "2025-01-03T10:00:02Z"))
        assert _wait(lambda: len(got) >= 2)
        assert [e.active for e in got[:2]] == [True, False]
        assert got[0].camera_host == "10.0.0.5"
        assert "/onvif/subscription?id=42" in fake.paths("PullMessages")
    finally:
        sub.stop()
    assert not sub.running
    assert "Unsubscribe" in fake.ops()


def test_subscriber_pull_messages_body():
    fake = FakeOnvifClient()
    sub = PullPointSubscriber(fake, lambda e: None, pull_timeout_s=4, message_limit=7, retry_backoff_s=0.05)
    sub.start()
    try:
        assert _wait(lambda: fake.bodies("PullMessages"))
        body = fake.bodies("PullMessages")[0]
        assert "PT4S" in body
        assert ">7<" in body.replace(" ", "")
    finally:
        sub.stop()


def test_subscriber_resubscribes_after_failures():
    fake = FakeOnvifClient()
    fake.fail_create = 2
    got = []
    sub = PullPointSubscriber(fake, got.append, pull_timeout_s=1, retry_backoff_s=0.05)
    sub.start()
    try:
        assert _wait(lambda: fake.ops().count("CreatePullPointSubscription") >= 3)
        fake.pull_queue.append(notification(True, "2025-01-03T10:00:01Z"))
        assert _wait(lambda: len(got) >= 1)
    finally:
        sub.stop()


def test_subscriber_resubscribes_when_pull_fails():
    fake = FakeOnvifClient()
    got = []
    sub = PullPointSubscriber(fake, got.append, pull_timeout_s=1, retry_backoff_s=0.05)
    sub.start()
    try:
        assert _wait(lambda: fake.bodies("PullMessages"))
        fake.raise_on_pull = TapoConnectionError("reset")
        before = fake.ops().count("CreatePullPointSubscription")
        assert _wait(lambda: fake.ops().count("CreatePullPointSubscription") > before)
        fake.raise_on_pull = None
        fake.pull_queue.append(notification(True, "2025-01-03T10:00:01Z"))
        assert _wait(lambda: len(got) >= 1)
    finally:
        sub.stop()


def test_subscriber_callback_exception_does_not_kill_loop():
    fake = FakeOnvifClient()
    got = []

    def bad(ev):
        got.append(ev)
        raise RuntimeError("consumer bug")

    sub = PullPointSubscriber(fake, bad, pull_timeout_s=1, retry_backoff_s=0.05)
    sub.start()
    try:
        fake.pull_queue.append(notification(True, "2025-01-03T10:00:01Z"))
        assert _wait(lambda: len(got) >= 1)
        fake.pull_queue.append(notification(False, "2025-01-03T10:00:02Z"))
        assert _wait(lambda: len(got) >= 2)
        assert sub.running
    finally:
        sub.stop()


def test_subscriber_stop_is_prompt_and_idempotent():
    fake = FakeOnvifClient()
    fake.pull_delay = 0.5
    sub = PullPointSubscriber(fake, lambda e: None, pull_timeout_s=1, retry_backoff_s=0.05)
    sub.start()
    assert _wait(lambda: fake.bodies("PullMessages"))
    t0 = time.monotonic()
    sub.stop(timeout=3.0)
    assert time.monotonic() - t0 < 3.0
    assert not sub.running
    sub.stop()


def test_subscriber_thread_is_daemon():
    fake = FakeOnvifClient()
    before = set(threading.enumerate())
    sub = PullPointSubscriber(fake, lambda e: None, pull_timeout_s=1, retry_backoff_s=0.05)
    sub.start()
    try:
        new = [t for t in threading.enumerate() if t not in before]
        assert new and all(t.daemon for t in new)
    finally:
        sub.stop()
