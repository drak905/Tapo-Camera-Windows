"""Regression: the REAL Kami sign-in reply shape (captured once by the owner, every value replaced by a made-up one).

Real server facts (HTTP 200, ``Content-Type: text/html;charset=UTF-8``, body is JSON):

* ``code`` is a JSON STRING of 5 digits, not a number.
* ``data.userid`` is a JSON NUMBER (6 digits), not a string.
* ``data.register_time`` is a string of 13 digits; ``flag`` is a boolean; empty fields are ``""``.
* ``data`` also has account, name, mobile, userMobileRegion, email, img, token, token_secret, birthday,
  first_name, last_name, user_mobile and openId.

The parser used to read ``code`` only as a number and ``userid`` only as a string, so it rejected this reply.
"""
from __future__ import annotations

import json

import pytest

from tapo_desktop.core.kami.client import KamiClient
from tapo_desktop.core.kami.dtos import (
    KamiAuthError, KamiExtraStepError, KamiProtocolError, KamiServerError, KamiSession, KamiSessionExpired,
    parse_devices, parse_login,
)
from tests.fixtures.mock_kami_cloud import MOCK_APP_KEY, make_device

EMAIL, PASSWORD = "owner@example.com", "pw-correct-1"
TOKEN, SECRET = "0123456789abcdef0123456789abcdef", "fedcba9876543210fedcba9876543210"


def real_shape_reply() -> dict:
    return {"code": "20000", "data": {
        "userid": 100001, "account": "owner@example.com", "name": "Owner", "mobile": "", "userMobileRegion": "",
        "email": "owner@example.com", "img": "", "token": TOKEN, "token_secret": SECRET, "birthday": "",
        "first_name": "Ab", "last_name": "Cdef", "user_mobile": "", "flag": True,
        "openId": "00112233445566778899aabbccddeeff", "register_time": "1700000000000"}}


def test_real_reply_parses_to_a_session():
    session = parse_login(real_shape_reply(), region="eu", country="DE", email=EMAIL)
    assert (session.userid, session.token, session.token_secret) == ("100001", TOKEN, SECRET)
    assert isinstance(session.userid, str)
    assert KamiSession.from_json(session.to_json()) == session


def test_string_error_code_is_an_auth_error_with_the_code():
    with pytest.raises(KamiAuthError) as exc:
        parse_login({"code": "30301", "msg": "rejected"}, region="us", country="US", email=EMAIL)
    assert exc.value.code == 30301 and not isinstance(exc.value, KamiExtraStepError)


def test_string_extra_step_code_is_an_extra_step_error():
    with pytest.raises(KamiExtraStepError):
        parse_login({"code": "20282"}, region="us", country="US", email=EMAIL)


@pytest.mark.parametrize("code", ["", "abc", "2000x", None, 2.5, True, [], {}])
def test_unusable_code_is_still_a_protocol_error(code):
    with pytest.raises(KamiProtocolError):
        parse_login({"code": code, "data": {}}, region="us", country="US", email=EMAIL)


@pytest.mark.parametrize("userid", [0, -5, True, 1.5, "", None, [], {}])
def test_unusable_userid_is_a_protocol_error(userid):
    reply = real_shape_reply()
    reply["data"]["userid"] = userid
    with pytest.raises(KamiProtocolError):
        parse_login(reply, region="us", country="US", email=EMAIL)


def test_device_list_accepts_a_string_code():
    devices = parse_devices({"code": "20000", "data": [make_device("UID-A", "Hall")]}, "eu")
    assert [d.uid for d in devices] == ["UID-A"]
    with pytest.raises(KamiSessionExpired):
        parse_devices({"code": "20202"}, "eu")
    with pytest.raises(KamiServerError):
        parse_devices({"code": "50000"}, "eu")


def test_client_login_against_a_mock_that_answers_like_the_real_server(mock_kami):
    mock_kami.raw_body["/v4/users/login"] = json.dumps(real_shape_reply()).encode("utf-8")
    client = KamiClient("eu", "DE", app_key=MOCK_APP_KEY, base_url=mock_kami.url, timeout=5.0)
    session = client.login(EMAIL, PASSWORD)
    assert session.userid == "100001" and session.token == TOKEN
    assert mock_kami.login_count == 1


def test_a_numeric_userid_is_sent_as_text_in_the_signed_list_call(mock_kami):
    mock_kami.add_account(EMAIL, PASSWORD, userid="100001")
    mock_kami.devices = [make_device("UID-A", "Hall")]
    client = KamiClient("eu", "DE", app_key=MOCK_APP_KEY, base_url=mock_kami.url, timeout=5.0)
    session = client.login(EMAIL, PASSWORD)
    assert [d.uid for d in client.list_devices(session)] == ["UID-A"]
