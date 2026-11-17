"""Self-check of the TPAP mock camera with raw HTTPS (independent of tapo_desktop's TPAP client)."""
from __future__ import annotations

import base64
import http.client
import json
import os
import ssl

import pytest

from tests.fixtures.mock_tapo_server import MockTapoCamera
from tests.fixtures.mock_tapo_server.tpap_server import md5_hex

pytestmark = pytest.mark.integration


def _post(cam, path: str, body: bytes, ctype: str = "application/json") -> tuple[int, str, bytes]:
    ctx = ssl.SSLContext(ssl.PROTOCOL_TLS_CLIENT)
    ctx.check_hostname = False
    ctx.verify_mode = ssl.CERT_NONE
    conn = http.client.HTTPSConnection(cam.host, cam.https_port, timeout=5, context=ctx)
    try:
        conn.request("POST", path, body=body, headers={"Content-Type": ctype})
        r = conn.getresponse()
        return r.status, r.getheader("Content-Type") or "", r.read()
    finally:
        conn.close()


def _login(cam, params: dict) -> dict:
    status, _, data = _post(cam, "/", json.dumps({"method": "login", "params": params}).encode())
    assert status == 200
    return json.loads(data)


@pytest.fixture
def cam():
    c = MockTapoCamera(protocol="tpap", model="C200").start()
    try:
        yield c
    finally:
        c.stop()


def _register_params(**over) -> dict:
    p = {"sub_method": "pake_register", "username": md5_hex("admin"),
         "user_random": base64.b64encode(os.urandom(32)).decode(), "cipher_suites": [1],
         "encryption": ["aes_128_ccm"], "passcode_type": "userpw"}
    p.update(over)
    return p


def test_v3_probe_answers_minus_40211(cam):
    assert _login(cam, {"cnonce": "0123456789ABCDEF", "encrypt_type": "3", "username": "admin"}) == \
        {"error_code": -40211}
    assert cam.v3_probe_count == 1


def test_register_reply_fields(cam):
    r = _login(cam, _register_params())
    assert r["error_code"] == 0
    res = r["result"]
    assert len(base64.b64decode(res["dev_share"])) == 65
    assert len(base64.b64decode(res["dev_random"])) == 32
    assert base64.b64decode(res["dev_salt"])
    assert res["cipher_suites"] == 1 and res["encryption"] == "aes_128_ccm" and res["iterations"] > 0
    assert "extra_crypt" not in res


def test_sha256_extra_camera_sends_extra_crypt():
    c = MockTapoCamera(protocol="tpap", tpap_credential="sha256_extra").start()
    try:
        ec = _login(c, _register_params())["result"]["extra_crypt"]
        assert ec["type"] == "password_shadow" and ec["params"]["passwd_id"] == 5
        assert ec["params"]["passwd_prefix"].startswith("$5$")
    finally:
        c.stop()


def test_register_rejects_wrong_username_and_suites(cam):
    assert _login(cam, _register_params(username="admin"))["error_code"] == -40209
    assert _login(cam, _register_params(cipher_suites=[2]))["error_code"] == -40209


def test_share_without_register_and_garbage_share(cam):
    assert _login(cam, {"sub_method": "pake_share", "user_share": "", "user_confirm": ""})["error_code"] == -40401
    _login(cam, _register_params())
    bad = {"sub_method": "pake_share", "user_share": base64.b64encode(b"\x04" + b"\x01" * 64).decode(),
           "user_confirm": base64.b64encode(b"\x00" * 32).decode()}
    assert _login(cam, bad)["error_code"] == -40209


def test_ds_with_unknown_stok_is_plaintext_minus_40401(cam):
    status, ctype, data = _post(cam, "/stok=deadbeef/ds", b"\x00\x00\x00\x01" + b"\x00" * 32,
                                "application/octet-stream")
    assert status == 200 and ctype.startswith("application/json")
    assert json.loads(data) == {"error_code": -40401}


def test_locked_register_reports_sec_left(cam):
    cam.lock_login(30)
    r = _login(cam, _register_params())
    assert r["error_code"] == -40404 and r["result"]["data"]["sec_left"] > 0
