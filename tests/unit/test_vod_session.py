"""Tier 1: MediaSession against an in-process fake of the :8800 endpoint (tests/unit/_media_helpers.py).

[spec:stream.local8800.handshake, stream.local8800.encryption.md5, stream.local8800.framing,
stream.control.envelope]
"""
from __future__ import annotations

import json
import socket
import time

import pytest

from tapo_desktop.core.errors import StreamLimitError, TapoAuthError, TapoConnectionError
from tapo_desktop.core.vod.session import MediaPart, MediaSession
from tests.unit._media_helpers import FakeMediaServer, FakePart, parse_auth_params, parse_client_parts

pytestmark = [pytest.mark.unit, pytest.mark.timeout(15)]

PW = "cloudpass"


def _session(server: FakeMediaServer, password: str = PW, **kw) -> MediaSession:
    return MediaSession("127.0.0.1", password, port=server.port, timeout=kw.pop("timeout", 3.0), **kw)


def test_handshake_requests_follow_spec():
    with FakeMediaServer(PW) as srv:
        s = _session(srv)
        try:
            s.connect()
        finally:
            s.close()
        assert srv.auth_ok
        assert len(srv.requests) == 2
        (line1, h1), (line2, h2) = srv.requests
        for line, h in srv.requests:
            assert line == "POST /stream HTTP/1.1"
            assert h["Content-Type"] == "multipart/mixed; boundary=--client-stream-boundary--"
            assert h["Content-Length"] == "0"
            assert h["X-Key-Exchange"] == "1"
        assert "Authorization" not in h1
        p = parse_auth_params(h2["Authorization"])
        assert p["username"] == "admin"
        assert p["uri"] == "/stream"
        assert p["opaque"] == srv.opaque


@pytest.mark.parametrize(("algorithm", "encrypt_type"), [("MD5", None), ("SHA-256", "3"), (None, None)])
def test_connect_succeeds_for_every_challenge_flavour(algorithm, encrypt_type):
    with FakeMediaServer(PW, algorithm=algorithm, encrypt_type=encrypt_type) as srv:
        with _session(srv) as s:
            s.connect()
        assert srv.auth_ok


def test_wrong_password_raises_auth_error_without_leaking_it():
    with FakeMediaServer(PW) as srv:
        s = _session(srv, password="WrongPassw0rd!")
        with pytest.raises(TapoAuthError) as ei:
            s.connect()
        s.close()
        assert "WrongPassw0rd!" not in str(ei.value)
        assert not srv.auth_ok


def test_busy_503_raises_stream_limit_error():
    with FakeMediaServer(PW, busy=True) as srv:
        s = _session(srv)
        with pytest.raises(StreamLimitError):
            s.connect()
        s.close()


def test_connection_refused_raises_connection_error():
    sock = socket.socket()
    sock.bind(("127.0.0.1", 0))
    port = sock.getsockname()[1]
    sock.close()
    s = MediaSession("127.0.0.1", PW, port=port, timeout=2.0)
    with pytest.raises(TapoConnectionError):
        s.connect()
    s.close()


def test_silent_server_times_out_as_connection_error():
    with FakeMediaServer(PW, silent=True) as srv:
        s = _session(srv, timeout=0.5)
        t0 = time.monotonic()
        with pytest.raises(TapoConnectionError):
            s.connect()
        assert time.monotonic() - t0 < 3.0
        s.close()


def test_parts_are_decrypted_in_order():
    ts1 = b"\x47" + bytes(range(187))
    ts2 = b"\x47" + b"\xaa" * 187 * 2
    notif = json.dumps({"type": "notification", "params": {"event_type": "stream_status", "status": "finished"}}).encode()
    parts = [
        FakePart({"Content-Type": "application/json"}, b'{"type":"response","seq":1,"result":{"error_code":0,"session_id":"9"}}'),
        FakePart({"Content-Type": "video/mp2t", "X-Data-Sequence": "1", "X-Data-PTS": "0"}, ts1, encrypt=True),
        FakePart({"Content-Type": "video/mp2t", "X-Data-Sequence": "2", "X-Data-PTS": "3600"}, ts2, encrypt=False),
        FakePart({"Content-Type": "application/json"}, notif),
    ]
    with FakeMediaServer(PW, parts=parts) as srv:
        with _session(srv) as s:
            s.connect()
            got = list(s.parts())
    assert all(isinstance(p, MediaPart) for p in got)
    assert [p.body for p in got] == [parts[0].body, ts1, ts2, notif]
    assert got[1].headers["X-If-Encrypt"] == "1"
    assert got[1].headers["X-Data-Sequence"] == "1"
    assert got[2].headers["Content-Type"] == "video/mp2t"


def test_sha256_flavour_decrypts_with_sha256_transformed_password():
    body = b"\x47" * 188 * 4
    with FakeMediaServer(PW, algorithm="SHA-256", encrypt_type="3",
                         parts=[FakePart({"Content-Type": "video/mp2t"}, body, encrypt=True)]) as srv:
        with _session(srv) as s:
            s.connect()
            assert [p.body for p in s.parts()] == [body]


def test_key_exchange_username_none_uses_default_password():
    body = b"\x47" * 188
    with FakeMediaServer(PW, ke_username="none",
                         parts=[FakePart({"Content-Type": "video/mp2t"}, body, encrypt=True)]) as srv:
        with _session(srv) as s:
            s.connect()
            assert [p.body for p in s.parts()] == [body]


def test_send_json_adds_incrementing_seq_and_frames_parts():
    with FakeMediaServer(PW, linger=3.0) as srv:
        with _session(srv) as s:
            s.connect()
            msg = {"type": "request", "params": {"method": "get", "download": {"client_id": 1}}}
            assert s.send_json(msg) == 1
            assert s.send_json({"type": "request", "params": {"method": "do", "stop": "download"}}) == 2
            assert s.send_json({"type": "request", "seq": 42, "params": {"method": "do", "play": {}}}) == 42
            assert s.send_json(
                {"type": "notification", "params": {"event_type": "stream_sequence"}},
                headers={"X-Data-Received": "25", "X-Session-Id": "9"},
            ) is not None
            assert srv.wait_client_bytes(b"X-Data-Received: 25")
    sent = parse_client_parts(bytes(srv.client_bytes))
    assert len(sent) == 4
    bodies = [json.loads(b) for _, b in sent]
    assert bodies[0] == {"type": "request", "seq": 1, "params": {"method": "get", "download": {"client_id": 1}}}
    assert bodies[1]["seq"] == 2 and bodies[1]["params"] == {"method": "do", "stop": "download"}
    assert bodies[2]["seq"] == 42
    for headers, body in sent:
        assert headers["Content-Type"] == "application/json"
        assert int(headers["Content-Length"]) == len(body)
        assert b'": ' not in body and b'", "' not in body  # compact JSON on the wire
    ack_headers = sent[3][0]
    assert ack_headers["X-Data-Received"] == "25"
    assert ack_headers["X-Session-Id"] == "9"
    assert bodies[3]["params"] == {"event_type": "stream_sequence"}


def test_send_json_does_not_mutate_caller_dict():
    with FakeMediaServer(PW, linger=1.0) as srv:
        with _session(srv) as s:
            s.connect()
            msg = {"type": "request", "params": {"method": "get"}}
            s.send_json(msg)
            assert "seq" not in msg


def test_parts_end_when_server_closes_and_close_is_idempotent():
    with FakeMediaServer(PW, parts=[FakePart({"Content-Type": "application/json"}, b"{}")], linger=0.1) as srv:
        s = _session(srv)
        s.connect()
        t0 = time.monotonic()
        assert [p.body for p in s.parts()] == [b"{}"]
        assert time.monotonic() - t0 < 3.0
        s.close()
        s.close()
