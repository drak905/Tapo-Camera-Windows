"""TapoCloudClient against MockTapoCloud (never the real cloud) [spec:cloud.*]."""
from __future__ import annotations

import base64
import http.client
import json
import logging
import socket
import threading
import time

import pytest

from tapo_desktop.core.cloud import client as client_mod
from tapo_desktop.core.cloud.client import (CloudAuthError, CloudError, CloudMfaRequired, TapoCloudClient,
                                            decode_alias, make_ssl_context)
from tapo_desktop.core.cloud.signing import sign_headers
from tapo_desktop.core.errors import TapoConnectionError
from tapo_desktop.models import AccountInfo, CloudDevice
from tests.fixtures.mock_cloud import make_cloud_device
from tests.fixtures.phase2_fixtures import DEFAULT_EMAIL, DEFAULT_PASSWORD

pytestmark = pytest.mark.integration

TERM = "0123456789ABCDEF0123456789ABCDEF"


def _client(cloud, **kw) -> TapoCloudClient:
    return TapoCloudClient(terminal_uuid=kw.pop("terminal_uuid", TERM), base_url=cloud.base_url,
                           timeout=kw.pop("timeout", 5.0), **kw)


# --------------------------------------------------------------------------- the mock itself
def _raw_post(cloud, path: str, body: bytes, headers: dict[str, str]) -> tuple[int, dict]:
    conn = http.client.HTTPConnection("127.0.0.1", cloud.port, timeout=5)
    try:
        conn.request("POST", path + f"?appName=TP-Link_Tapo_Android&termID={TERM}", body=body,
                     headers={"Content-Type": "application/json;charset=UTF-8", **headers})
        resp = conn.getresponse()
        return resp.status, json.loads(resp.read())
    finally:
        conn.close()


def test_mock_verifies_content_md5_and_signature(mock_cloud):
    path = "/api/v2/account/getAccountStatusAndUrl"
    body = json.dumps({"appType": "TP-Link_Tapo_Android", "cloudUserName": DEFAULT_EMAIL}).encode()
    assert _raw_post(mock_cloud, path, body, {})[0] == 401
    good = sign_headers(body, path)
    assert _raw_post(mock_cloud, path, body, {**good, "Content-MD5": "AAAAAAAAAAAAAAAAAAAAAA=="})[0] == 401
    bad_sig = dict(good)
    bad_sig["X-Authorization"] = good["X-Authorization"][:-4] + "0000"
    assert _raw_post(mock_cloud, path, body, bad_sig)[0] == 401
    wrong_path = sign_headers(body, "/api/v2/account/login")
    assert _raw_post(mock_cloud, path, body, wrong_path)[0] == 401
    status, payload = _raw_post(mock_cloud, path, body, good)
    assert status == 200 and payload["errorCode"] == 0
    assert payload["result"]["appServerUrlV2"] == mock_cloud.regional_url


# --------------------------------------------------------------------------- login
def test_login_happy_path(mock_cloud):
    c = _client(mock_cloud)
    assert c.token is None and c.account is None
    info = c.login(DEFAULT_EMAIL, DEFAULT_PASSWORD)
    assert info == AccountInfo(email=DEFAULT_EMAIL, nickname="Tester", account_id="12345678",
                               region_url=mock_cloud.regional_url)
    assert c.account == info
    assert c.token and c.token in mock_cloud.valid_tokens()

    log = mock_cloud.request_log
    assert [r["path"] for r in log] == ["/api/v2/account/getAccountStatusAndUrl", "/eu/api/v2/account/login"]
    assert all(r["signature_ok"] for r in log)
    assert log[0]["body"] == {"appType": "TP-Link_Tapo_Android", "cloudUserName": DEFAULT_EMAIL}
    body = log[1]["body"]
    assert body["appType"] == "TP-Link_Tapo_Android"
    assert body["appVersion"] == "3.22.806"
    assert body["cloudUserName"] == DEFAULT_EMAIL and body["cloudPassword"] == DEFAULT_PASSWORD
    assert body["terminalUUID"] == TERM and body["terminalMeta"] == "1"
    assert body["refreshTokenNeeded"] is False and body["platform"] and body["terminalName"]
    q = log[1]["query"]
    assert q["appName"] == "TP-Link_Tapo_Android" and q["appVer"] == "3.22.806" and q["termID"] == TERM
    assert {"netType", "ospf", "brand", "locale"} <= set(q)
    assert "token" not in q and "token" not in log[0]["query"]
    assert log[1]["headers"]["Content-Type"].startswith("application/json")


def test_login_follows_region_redirect(mock_cloud_factory):
    cloud = mock_cloud_factory(region_redirect=True)
    c = _client(cloud)
    info = c.login(DEFAULT_EMAIL, DEFAULT_PASSWORD)
    assert info.region_url == cloud.regional_url
    paths = [r["path"] for r in cloud.request_log]
    assert paths == ["/api/v2/account/getAccountStatusAndUrl", "/api/v2/account/login",
                     "/eu/api/v2/account/login"]
    assert len(c.list_cameras()) == 2  # later calls go to the regional URL


@pytest.mark.parametrize("email,password,code", [
    (DEFAULT_EMAIL, "wrong-password", -20601),
    ("nobody@example.com", DEFAULT_PASSWORD, -20600),
])
def test_wrong_credentials_raise_cloud_auth_error(mock_cloud, email, password, code):
    c = _client(mock_cloud)
    with pytest.raises(CloudAuthError) as ei:
        c.login(email, password)
    assert ei.value.code == code
    assert str(ei.value) and password not in str(ei.value)
    assert c.token is None and c.account is None


def test_locked_account_raises_cloud_auth_error(mock_cloud):
    mock_cloud.locked.add(DEFAULT_EMAIL)
    with pytest.raises(CloudAuthError) as ei:
        _client(mock_cloud).login(DEFAULT_EMAIL, DEFAULT_PASSWORD)
    assert ei.value.code == -20661
    assert "lock" in str(ei.value).lower() and "30" in str(ei.value)


def test_cloud_auth_error_is_a_cloud_error():
    assert issubclass(CloudAuthError, CloudError) and issubclass(CloudMfaRequired, CloudError)


# --------------------------------------------------------------------------- MFA
def test_mfa_required_then_complete(mock_cloud_factory):
    cloud = mock_cloud_factory(mfa=True, mfa_types=[2, 3, 4])
    c = _client(cloud)
    with pytest.raises(CloudMfaRequired) as ei:
        c.login(DEFAULT_EMAIL, DEFAULT_PASSWORD)
    assert ei.value.process_id and ei.value.mfa_types == [2, 3, 4]
    assert c.token is None

    c.request_mfa_code(2)
    assert cloud.sent_codes == [(2, DEFAULT_EMAIL)]
    send = cloud.calls("/getEmailVC4TerminalMFA")[-1]
    assert send["path"] == "/eu/api/v2/account/getEmailVC4TerminalMFA"
    assert send["body"]["cloudPassword"] == DEFAULT_PASSWORD and send["body"]["terminalUUID"] == TERM
    c.request_mfa_code(4)
    assert cloud.sent_codes[-1] == (4, DEFAULT_EMAIL)
    n = len(cloud.request_log)
    c.request_mfa_code(3)  # TOTP: nothing to send
    assert len(cloud.request_log) == n

    with pytest.raises(CloudAuthError):
        c.complete_mfa("000000", 2)
    info = c.complete_mfa(cloud.mfa_code, 2)
    assert info.email == DEFAULT_EMAIL and info.region_url == cloud.regional_url
    assert c.token in cloud.valid_tokens()
    check = cloud.calls("/checkMFACodeAndLogin")[-1]["body"]
    assert check["MFAType"] == 2 and check["terminalBindEnabled"] is True
    assert check["MFAProcessId"] == ei.value.process_id and check["code"] == cloud.mfa_code

    # the bound terminal signs in later without MFA (same as the phone app)
    again = _client(cloud).login(DEFAULT_EMAIL, DEFAULT_PASSWORD)
    assert again.email == DEFAULT_EMAIL


def test_mfa_calls_without_pending_mfa_raise(mock_cloud):
    c = _client(mock_cloud)
    with pytest.raises(CloudError):
        c.request_mfa_code(2)
    with pytest.raises(CloudError):
        c.complete_mfa("123456", 2)


# --------------------------------------------------------------------------- device list
def test_list_cameras_pages_and_filters(mock_cloud_factory):
    devices = [make_cloud_device(i) for i in range(1, 46)]
    devices += [make_cloud_device(100, deviceType="SMART.TAPOPLUG", deviceModel="P110"),
                make_cloud_device(101, deviceType="SMART.TAPOHUB", deviceModel="H200"),
                make_cloud_device(102, deviceType="SMART.TAPODOORBELL", deviceModel="D230")]
    cloud = mock_cloud_factory(devices=devices)
    c = _client(cloud)
    c.login(DEFAULT_EMAIL, DEFAULT_PASSWORD)
    cams = c.list_cameras()
    assert len(cams) == 46  # 45 cameras + the doorbell; plug and hub are not cameras
    assert {d.device_type for d in cams} == {"SMART.IPCAMERA", "SMART.TAPODOORBELL"}
    assert len({d.device_id for d in cams}) == 46
    pages = cloud.calls("/getDeviceListByPage")
    assert [p["body"]["index"] for p in pages] == [0, 20, 40]
    assert all(p["body"]["limit"] == 20 for p in pages)
    assert all("SMART.IPCAMERA" in p["body"]["deviceTypeList"] for p in pages)
    assert all(p["query"]["token"] == c.token and p["signature_ok"] for p in pages)


def test_list_cameras_maps_fields(mock_cloud):
    c = _client(mock_cloud)
    c.login(DEFAULT_EMAIL, DEFAULT_PASSWORD)
    cams = {d.alias: d for d in c.list_cameras()}
    assert set(cams) == {"Front Door", "Garden"}
    front = cams["Front Door"]
    assert isinstance(front, CloudDevice)
    assert front.device_id == make_cloud_device(1)["deviceId"]
    assert front.mac == "A0-B1-C2-00-00-01"
    assert front.model == "C210" and front.online is True and front.role == 0
    assert front.fw_version.startswith("1.3.11") and front.hw_version == "2.0"
    assert front.raw["deviceId"] == front.device_id
    assert cams["Garden"].online is False and cams["Garden"].model == "C320WS"


def test_alias_decoding_plain_and_base64(mock_cloud_factory):
    b64 = base64.b64encode("Phòng khách".encode()).decode()
    cloud = mock_cloud_factory(devices=[make_cloud_device(1, alias="Front Door"), make_cloud_device(2, alias=b64),
                                        make_cloud_device(3, alias="Cam1"),
                                        make_cloud_device(4, alias="", deviceName="C200")])
    c = _client(cloud)
    c.login(DEFAULT_EMAIL, DEFAULT_PASSWORD)
    aliases = sorted(d.alias for d in c.list_cameras())
    assert aliases == sorted(["Front Door", "Phòng khách", "Cam1", "C200"])


@pytest.mark.parametrize("raw,expected", [
    ("Front Door", "Front Door"),       # spaces -> not Base64
    ("Cam1", "Cam1"),                   # valid Base64 alphabet but decodes to invalid UTF-8
    ("Home", "Home"),
    (base64.b64encode(b"Living room").decode(), "Living room"),
    (base64.b64encode("Cổng trước".encode()).decode(), "Cổng trước"),
    (base64.b64encode(b"\x01\x02ctl").decode(), base64.b64encode(b"\x01\x02ctl").decode()),  # unprintable
    ("", ""),
    ("Garage2024", "Garage2024"),
])
def test_decode_alias(raw, expected):
    assert decode_alias(raw) == expected


def test_list_cameras_requires_login(mock_cloud):
    with pytest.raises(CloudError):
        _client(mock_cloud).list_cameras()


def test_token_expiry_silent_relogin_once(mock_cloud):
    c = _client(mock_cloud)
    c.login(DEFAULT_EMAIL, DEFAULT_PASSWORD)
    old = c.token
    mock_cloud.expire_tokens()
    cams = c.list_cameras()
    assert len(cams) == 2
    assert c.token != old and c.token in mock_cloud.valid_tokens()
    relogins = mock_cloud.calls("/captchaLogin")
    assert len(relogins) == 1
    assert relogins[0]["path"] == "/eu/api/v2/account/captchaLogin"
    assert relogins[0]["body"]["cloudPassword"] == DEFAULT_PASSWORD


def test_token_expiry_relogin_failure_is_not_retried_forever(mock_cloud):
    c = _client(mock_cloud)
    c.login(DEFAULT_EMAIL, DEFAULT_PASSWORD)
    mock_cloud.expire_tokens()
    mock_cloud.accounts[DEFAULT_EMAIL] = "changed-on-another-device"
    with pytest.raises(CloudAuthError):
        c.list_cameras()
    assert len(mock_cloud.calls("/captchaLogin")) == 1


def test_http_error_with_bad_signature_is_cloud_error(mock_cloud, monkeypatch):
    real = client_mod.sign_headers
    monkeypatch.setattr(client_mod, "sign_headers",
                        lambda body, path, **kw: real(body, path, secret_key="0" * 32))
    with pytest.raises(CloudError) as ei:
        _client(mock_cloud).login(DEFAULT_EMAIL, DEFAULT_PASSWORD)
    assert not isinstance(ei.value, CloudAuthError)
    assert ei.value.code == -1


# --------------------------------------------------------------------------- network failures
def test_connection_refused_raises_tapo_connection_error():
    s = socket.socket()
    s.bind(("127.0.0.1", 0))
    port = s.getsockname()[1]
    s.close()
    c = TapoCloudClient(terminal_uuid=TERM, base_url=f"http://127.0.0.1:{port}", timeout=2.0)
    with pytest.raises(TapoConnectionError):
        c.login(DEFAULT_EMAIL, DEFAULT_PASSWORD)


def test_timeout_raises_tapo_connection_error():
    srv = socket.socket()
    srv.bind(("127.0.0.1", 0))
    srv.listen(5)
    accepted: list[socket.socket] = []
    stop = threading.Event()

    def accept_and_hang():
        srv.settimeout(0.2)
        while not stop.is_set():
            try:
                accepted.append(srv.accept()[0])
            except OSError:
                pass

    t = threading.Thread(target=accept_and_hang, daemon=True)
    t.start()
    try:
        c = TapoCloudClient(terminal_uuid=TERM, base_url=f"http://127.0.0.1:{srv.getsockname()[1]}", timeout=0.5)
        t0 = time.monotonic()
        with pytest.raises(TapoConnectionError):
            c.login(DEFAULT_EMAIL, DEFAULT_PASSWORD)
        assert time.monotonic() - t0 < 3.0
    finally:
        stop.set()
        t.join(2)
        for a in accepted:
            a.close()
        srv.close()


def test_malformed_response_is_cloud_error():
    srv = socket.socket()
    srv.bind(("127.0.0.1", 0))
    srv.listen(5)

    def serve():
        srv.settimeout(5)
        try:
            conn, _ = srv.accept()
            buf = b""
            while b"\r\n\r\n" not in buf:
                buf += conn.recv(65536)
            head, _, rest = buf.partition(b"\r\n\r\n")
            length = int(next(line.split(b":")[1] for line in head.split(b"\r\n")
                              if line.lower().startswith(b"content-length")))
            while len(rest) < length:
                rest += conn.recv(65536)
            conn.sendall(b"HTTP/1.1 200 OK\r\nContent-Type: text/html\r\nContent-Length: 9\r\n"
                         b"Connection: close\r\n\r\n<html>!</")
            conn.close()
        except OSError:
            pass

    t = threading.Thread(target=serve, daemon=True)
    t.start()
    try:
        c = TapoCloudClient(terminal_uuid=TERM, base_url=f"http://127.0.0.1:{srv.getsockname()[1]}", timeout=2)
        with pytest.raises(CloudError):
            c.login(DEFAULT_EMAIL, DEFAULT_PASSWORD)
    finally:
        t.join(6)
        srv.close()


# --------------------------------------------------------------------------- secrets
def test_secrets_never_in_repr_or_logs(mock_cloud, caplog):
    caplog.set_level(logging.DEBUG)
    c = _client(mock_cloud)
    c.login(DEFAULT_EMAIL, DEFAULT_PASSWORD)
    token = c.token
    mock_cloud.expire_tokens()
    c.list_cameras()
    with pytest.raises(CloudAuthError) as ei:
        _client(mock_cloud).login(DEFAULT_EMAIL, "Wr0ng-Secret-Value")
    text = caplog.text + repr(c) + str(c.__dict__.keys())
    for secret in (DEFAULT_PASSWORD, token, c.token, "Wr0ng-Secret-Value"):
        assert secret not in text
        assert secret not in repr(c) and secret not in str(c)
    assert "Wr0ng-Secret-Value" not in str(ei.value) and "Wr0ng-Secret-Value" not in repr(ei.value)
    assert any("cloud" in r.name for r in caplog.records)  # the client does log (without secrets)


# --------------------------------------------------------------------------- TLS
def test_tls_context_trusts_tplink_cloud_roots():
    ctx = make_ssl_context(True)
    subjects = {str(dict(x[0] for x in ca["subject"]).get("commonName")) for ca in ctx.get_ca_certs()}
    assert {"TP-Link Cloud Root CA", "tp-link-CA"} <= subjects
    import ssl
    assert ctx.verify_mode == ssl.CERT_REQUIRED and ctx.check_hostname
    insecure = make_ssl_context(False)
    assert insecure.verify_mode == ssl.CERT_NONE
