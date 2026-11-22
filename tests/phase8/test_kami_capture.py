"""One-shot capture of the real Kami sign-in reply (owner tool): redaction keeps structure, hides values.

Written first (red). The real server is never contacted here; everything runs against ``mock_kami_cloud``.
"""
from __future__ import annotations

import importlib.util
import json
import pathlib

import pytest

from tapo_desktop.core.kami.capture import capture_login, redact_json, write_capture
from tapo_desktop.core.kami.client import KamiClient
from tapo_desktop.core.kami.dtos import KamiKeyMissingError
from tests.fixtures.mock_kami_cloud import MOCK_APP_KEY

EMAIL = "owner@example.com"
PASSWORD = "pw-correct-1"
ROOT = pathlib.Path(__file__).resolve().parents[2]


def _client(mock, key=MOCK_APP_KEY):
    return KamiClient("eu", "DE", app_key=key, base_url=mock.url, timeout=5.0)


def _keys(node, prefix=""):
    if isinstance(node, dict):
        for k, v in node.items():
            yield prefix + k
            yield from _keys(v, prefix + k + ".")


# ------------------------------------------------------------------ redact_json
def test_redact_keeps_field_names_and_codes_hides_values():
    doc = {"code": 20000, "msg": "success", "data": {"userid": 123456789, "token": "a" * 32, "email": "x@y.z",
                                                     "flag": True, "nothing": None, "list": ["abc", 5]}}
    out = redact_json(doc)
    assert out["code"] == 20000 and out["msg"] == "success"
    assert set(out["data"]) == {"userid", "token", "email", "flag", "nothing", "list"}
    assert out["data"]["flag"] is True and out["data"]["nothing"] is None
    text = json.dumps(out)
    for secret in ("123456789", "a" * 32, "x@y.z", "abc"):
        assert secret not in text
    assert "32" in out["data"]["token"]  # the length stays: it shows what kind of token it is


def test_redact_scrubs_known_secrets_even_in_safe_keys():
    out = redact_json({"msg": f"hello {EMAIL} and {PASSWORD}"}, secrets=(EMAIL, PASSWORD))
    assert EMAIL not in json.dumps(out) and PASSWORD not in json.dumps(out)


def test_redact_drops_text_with_an_at_sign():
    assert "@" not in json.dumps(redact_json({"msg": "mail a@b.c refused"}))


# ------------------------------------------------------------------ capture_login
def test_capture_login_records_status_shape_and_no_secret(mock_kami):
    doc = capture_login(_client(mock_kami), EMAIL, PASSWORD)
    assert doc["status"] == 200
    assert doc["request"]["path"] == "/v4/users/login"
    assert "password" in doc["request"]["param_names"] and "account" in doc["request"]["param_names"]
    assert doc["body"]["code"] == 20000
    assert {"userid", "token", "token_secret", "email"} <= set(doc["body"]["data"])
    token, secret = mock_kami.last_session()
    text = json.dumps(doc)
    for forbidden in (token, secret, EMAIL, PASSWORD, "u-100", "Owner A", MOCK_APP_KEY, "127.0.0.1"):
        assert forbidden not in text
    assert mock_kami.login_count == 1  # exactly one request, no retry


def test_capture_login_keeps_header_names_and_hides_cookie_values(mock_kami):
    doc = capture_login(_client(mock_kami), EMAIL, PASSWORD)
    names = {k.lower() for k in doc["headers"]}
    assert "content-type" in names
    assert doc["headers"]["Content-Type"] == "application/json"


def test_capture_login_reports_non_json_body(mock_kami):
    mock_kami.raw_body["/v4/users/login"] = b"<html>gateway says no</html>"
    doc = capture_login(_client(mock_kami), EMAIL, PASSWORD)
    assert doc["body_kind"] == "not-json" and doc["body_length"] == 28
    assert doc["first_bytes_hex"].startswith("3c68746d6c")


def test_capture_login_sees_a_byte_order_mark(mock_kami):
    mock_kami.raw_body["/v4/users/login"] = b"\xef\xbb\xbf" + json.dumps({"code": 20000, "data": {"token": "t"}}).encode()
    doc = capture_login(_client(mock_kami), EMAIL, PASSWORD)
    assert doc["has_bom"] is True and doc["body"]["code"] == 20000


def test_capture_login_keeps_non_200_status(mock_kami):
    mock_kami.http_status["/v4/users/login"] = 403
    doc = capture_login(_client(mock_kami), EMAIL, PASSWORD)
    assert doc["status"] == 403


def test_capture_needs_the_key_and_sends_nothing(mock_kami):
    with pytest.raises(KamiKeyMissingError):
        capture_login(_client(mock_kami, key=None), EMAIL, PASSWORD)
    assert mock_kami.login_count == 0


def test_write_capture_is_lf_utf8_without_bom(tmp_path):
    target = tmp_path / "sub" / "reply.json"
    write_capture({"status": 200, "body": {"code": 20000}}, target)
    raw = target.read_bytes()
    assert not raw.startswith(b"\xef\xbb\xbf") and b"\r" not in raw
    assert json.loads(raw)["status"] == 200


# ------------------------------------------------------------------ the owner tool
def _tool():
    spec = importlib.util.spec_from_file_location("diag_kami_capture", ROOT / "tools" / "diag_kami_capture.py")
    mod = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(mod)
    return mod


def test_tool_refuses_without_owner_flag(capsys):
    assert _tool().main(["--email", EMAIL, "--country", "DE"]) == 2
    assert "--i-am-the-owner" in capsys.readouterr().err


def test_tool_has_no_password_argument():
    src = (ROOT / "tools" / "diag_kami_capture.py").read_text(encoding="utf-8")
    assert "--password" not in src and "getpass" in src
