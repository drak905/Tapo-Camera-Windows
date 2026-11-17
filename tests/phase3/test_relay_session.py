"""RelayMediaSession: the LAN media-session surface over the relay tunnel [spec:remote.relay.stream_over_tunnel,
remote.relay.heartbeat] (mock cloud + mock relay, no ffmpeg)."""
from __future__ import annotations

import threading
import time

import pytest

from tapo_desktop.core.cloud.relay import HEARTBEAT_BODY, HEARTBEAT_INTERVAL_S, RelayError, RelayMediaSession
from tapo_desktop.core.errors import TapoProtocolError
from tapo_desktop.core.media_engine.local_stream import build_preview_request, build_stop_request
from tests.integration._media_helpers import wait_until
from tests.phase3.conftest import DEVICE

pytestmark = [pytest.mark.integration, pytest.mark.timeout(60)]

DEV = DEVICE["deviceId"]


def _session(relay_client, **kw) -> RelayMediaSession:
    kw.setdefault("model", "C210")
    kw.setdefault("timeout", 5)
    return RelayMediaSession(relay_client, DEV, **kw)


def _first_parts(session, n_media: int = 3):
    seq = session.send_json(build_preview_request("VGA"))
    jsons, media = [], []
    for part in session.parts():
        if "json" in part.headers.get("Content-Type", ""):
            jsons.append(part)
        else:
            media.append(part)
        if len(media) >= n_media:
            break
    return seq, jsons, media


def test_heartbeat_constants_match_spec():
    assert HEARTBEAT_INTERVAL_S == 15.0
    assert HEARTBEAT_BODY == b'{"type": "notification", "params": {"event_type": "heartbeat"}}'
    assert len(HEARTBEAT_BODY) == 63


def test_preview_over_tunnel_plaintext_ts(relay_client, mock_relay):
    session = _session(relay_client)
    try:
        session.connect()
        assert session.session_id is None
        seq, jsons, media = _first_parts(session)
        assert seq == 1
        assert session.session_id == "1"
        assert media and all(p.body[:1] == b"\x47" for p in media)  # TS sync byte: not encrypted
        assert all(p.headers.get("X-If-Encrypt") == "0" for p in media)
        first = mock_relay.client_parts[0]
        assert first["body"]["params"]["preview"]["resolutions"] == ["VGA"]  # no inner POST /stream, no digest
        assert mock_relay.previews[0]["slot_resolution"] == "VGA"
        session.send_json(build_stop_request())
        assert wait_until(lambda: mock_relay.stops == 1, 3)
        assert mock_relay.client_parts[-1]["headers"].get("X-Session-Id") == "1"
    finally:
        session.close()
    assert wait_until(lambda: mock_relay.open_tunnels == 0, 3)


def test_ack_uses_session_id(relay_client, mock_relay):
    session = _session(relay_client)
    try:
        session.connect()
        _first_parts(session)
        session.send_ack(25)
        assert wait_until(lambda: mock_relay.acks == ["25"], 3)
    finally:
        session.close()


def test_heartbeat_sent_on_interval(relay_client, mock_relay):
    session = _session(relay_client, heartbeat_interval=0.3)
    try:
        session.connect()
        t0 = time.monotonic()
        _first_parts(session, 1)
        assert wait_until(lambda: len(mock_relay.heartbeats) >= 3, 5)
        assert all(body == HEARTBEAT_BODY for _, body in mock_relay.heartbeats)
        assert mock_relay.heartbeats[0][0] - t0 < 1.5
    finally:
        session.close()
    count = len(mock_relay.heartbeats)
    time.sleep(0.8)
    assert len(mock_relay.heartbeats) == count, "heartbeats continue after close()"
    assert not [t for t in threading.enumerate() if t.name.startswith("relay-heartbeat") and t.is_alive()]


def test_no_heartbeat_before_interval(relay_client, mock_relay):
    session = _session(relay_client)  # default 15 s
    try:
        session.connect()
        _first_parts(session, 2)
        time.sleep(0.5)
        assert mock_relay.heartbeats == []
    finally:
        session.close()


def test_encrypted_relay_part_fails_clearly(relay_client, mock_relay):
    mock_relay.mode = "encrypted"
    session = _session(relay_client)
    try:
        session.connect()
        with pytest.raises(TapoProtocolError, match="encrypted"):
            _first_parts(session)
    finally:
        session.close()


def test_connect_offline_raises_fatal(relay_client, mock_relay):
    mock_relay.mode = "offline"
    session = _session(relay_client)
    with pytest.raises(RelayError) as info:
        session.connect()
    assert info.value.fatal and "offline" in str(info.value).lower()
    session.close()


def test_close_during_connect_aborts(relay_client, mock_relay):
    session = _session(relay_client)
    session.close()  # stop() raced ahead of the worker
    session2 = _session(relay_client)
    session2.connect()
    session2.close()
    session2.close()  # idempotent
    assert wait_until(lambda: mock_relay.open_tunnels == 0, 3)


def test_repr_has_no_secrets(relay_client, mock_relay):
    session = _session(relay_client)
    session.connect()
    try:
        text = repr(session)
        assert DEV in text
        assert mock_relay.slots and not any(token in text for token in mock_relay.slots)
    finally:
        session.close()
