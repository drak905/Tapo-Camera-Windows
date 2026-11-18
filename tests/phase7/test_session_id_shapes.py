"""Real Tapo cameras and the TP-Link relay answer a media request with
``{"type":"response","seq":1,"params":{"error_code":0,"session_id":"7"}}`` (seen live on a C200 and a
C510W), not ``result``. The session id must be learned from either shape, or from X-Session-Id;
without it no flow-control acks and no stop request are sent."""
from __future__ import annotations

import json

import pytest

from tapo_desktop.core.vod.session import MediaPart, MediaSession

pytestmark = pytest.mark.unit


def _session() -> MediaSession:
    return MediaSession("127.0.0.1", "pw", port=1)


def _json_part(obj: dict, **headers: str) -> MediaPart:
    return MediaPart({"Content-Type": "application/json", **headers}, json.dumps(obj).encode())


@pytest.mark.parametrize("reply", [
    {"type": "response", "seq": 1, "params": {"error_code": 0, "session_id": "7"}},
    {"type": "response", "seq": 1, "result": {"error_code": 0, "session_id": "7"}},
    {"type": "response", "seq": 1, "params": {"error_code": 0, "session_id": 7}},
])
def test_session_id_from_response_body(reply):
    s = _session()
    s._decode(_json_part(reply))
    assert s.session_id == "7"


def test_session_id_from_header_when_body_has_none():
    s = _session()
    s._decode(_json_part({"type": "notification", "params": {"event_type": "channel_motor_status"}},
                         **{"X-Session-Id": "12"}))
    assert s.session_id == "12"


def test_error_reply_does_not_set_session_id():
    s = _session()
    s._decode(_json_part({"type": "response", "seq": 1, "params": {"error_code": -52407}}))
    assert s.session_id is None
