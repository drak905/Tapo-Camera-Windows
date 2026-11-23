"""iMou route A, core: signing, stream-URL decryption, data centers, the strict client, errors, the call guard."""
from __future__ import annotations

import json
import logging
import pathlib
import re
import uuid

import pytest

from tests.fixtures.mock_imou_cloud import (
    MOCK_APP_ID, MOCK_APP_SECRET, MOCK_HLS_QUERY, MOCK_RTSP_QUERY, MockImouCloud, encrypt_stream_url, make_device,
)

SRC = pathlib.Path(__file__).resolve().parents[2] / "src" / "tapo_desktop"

DOC_TIME, DOC_NONCE = 1706511734, "f5a1ae2d-c09c-4d39-a744-83a5c2c653c2"
DOC_SECRET, DOC_SIGN = "test123456789test123456789", "xjhCQBoJ9hRDsCjyDcHjtDNzRZ3ZJezcawsfWeiaoxU="

EXPECTED_PATHS = frozenset({
    "/openapi/accessToken", "/openapi/listDeviceDetailsByPage", "/openapi/createVideoDownloadStreamUrl",
    "/openapi/bindDeviceLive", "/openapi/liveList", "/openapi/unbindLive",
    "/openapi/v2/device.service.invoke",  # SD playback: the one service id record.file.list is checked separately
})
EXPECTED_PUBLIC_METHODS = {"access_token", "list_devices", "create_rtsp_url", "bind_live", "find_live", "unbind_live",
                           "list_local_records", "create_playback_url"}


def make_client(cloud, clock, *, dc="sg", guard=None, secret=MOCK_APP_SECRET, app_id=MOCK_APP_ID):
    from tapo_desktop.core.imou.client import ImouClient

    return ImouClient(app_id, secret, dc, transport=cloud.post, clock=clock, guard=guard,
                      nonce_factory=lambda: str(uuid.uuid4()))


# ------------------------------------------------------------------------------------------ signing
def test_sign_matches_the_public_doc_vector():
    from tapo_desktop.core.imou import signing

    assert signing.sign(DOC_SECRET, DOC_TIME, DOC_NONCE) == DOC_SIGN


def test_password_key_is_lowercase_hex_sha256_of_the_secret():
    from tapo_desktop.core.imou import signing

    key = signing.password_key(DOC_SECRET)
    assert re.fullmatch(r"[0-9a-f]{64}", key)


def test_request_body_shape_and_secret_never_inside():
    from tapo_desktop.core.imou import signing

    body = signing.build_request("lcdAPPID", DOC_SECRET, {"token": "At_x", "pageSize": 50}, time_s=DOC_TIME,
                                 nonce=DOC_NONCE, request_id="req-1")
    assert body == {"system": {"ver": "1.0", "appId": "lcdAPPID", "sign": DOC_SIGN, "time": DOC_TIME,
                               "nonce": DOC_NONCE}, "id": "req-1", "params": {"token": "At_x", "pageSize": 50}}
    assert DOC_SECRET not in signing.encode(body).decode("utf-8")
    assert b" " not in signing.encode(body).replace(b"At_x", b"")  # compact JSON


# ------------------------------------------------------------------------------------------ decrypt
def test_decrypt_stream_url_round_trip_with_independent_encryption():
    from tapo_desktop.core.imou.crypto import decrypt_stream_url

    url = "rtsp://203.0.113.7:554/live?token=abc&x=1"
    assert decrypt_stream_url(encrypt_stream_url(url, "At_token_1"), "At_token_1") == url


@pytest.mark.parametrize("bad", ["", "AAAA", "not base64 !!", "A" * 40])
def test_decrypt_rejects_garbage(bad):
    from tapo_desktop.core.imou.crypto import decrypt_stream_url
    from tapo_desktop.core.imou.errors import ImouProtocolError

    with pytest.raises(ImouProtocolError):
        decrypt_stream_url(bad, "At_token_1")


def test_decrypt_with_the_wrong_token_fails_and_does_not_leak_the_token():
    from tapo_desktop.core.imou.crypto import decrypt_stream_url
    from tapo_desktop.core.imou.errors import ImouProtocolError

    blob = encrypt_stream_url("rtsp://203.0.113.7/x", "At_right")
    with pytest.raises(ImouProtocolError) as err:
        decrypt_stream_url(blob, "At_wrong_secret_token")
    assert "At_wrong_secret_token" not in str(err.value)


# ------------------------------------------------------------------------------------------ data centers
def test_data_centers_are_a_fixed_list():
    from tapo_desktop.core.imou import regions

    assert set(regions.DATA_CENTERS) == {"sg", "fk", "or"}
    assert regions.base_url("sg") == "https://openapi-sg.easy4ip.com"
    assert regions.base_url("fk") == "https://openapi-fk.easy4ip.com"
    assert regions.base_url("or") == "https://openapi-or.easy4ip.com"


@pytest.mark.parametrize("bad", ["", "cn", "SG ", "evil.example", "sg.evil.example", None, "http://x"])
def test_unknown_data_center_is_refused(bad):
    from tapo_desktop.core.imou import regions

    with pytest.raises(ValueError):
        regions.base_url(bad)


@pytest.mark.parametrize("url,ok", [
    ("https://openapi-sg.easy4ip.com/openapi/accessToken", True),
    ("https://openapi-sg.easy4ip.com:443/openapi/accessToken", True),
    ("http://openapi-sg.easy4ip.com/openapi/accessToken", False),
    ("https://openapi-sg.easy4ip.com.evil.example/openapi/accessToken", False),
    ("https://evil.example/openapi/accessToken", False),
    ("https://user@openapi-sg.easy4ip.com/openapi/accessToken", False),
    ("https://openapi.lechange.cn/openapi/accessToken", False),
])
def test_allowed_url(url, ok):
    from tapo_desktop.core.imou import regions

    assert regions.is_allowed_url(url) is ok


def test_default_transport_refuses_any_other_host_before_opening_a_socket(monkeypatch):
    from tapo_desktop.core.imou.client import UrllibTransport
    from tapo_desktop.core.imou.errors import ImouNotAllowedError

    def boom(*a, **k):  # pragma: no cover - must not be reached
        raise AssertionError("a network call was attempted")

    monkeypatch.setattr("urllib.request.urlopen", boom)
    with pytest.raises(ImouNotAllowedError):
        UrllibTransport().post("https://evil.example/openapi/accessToken", b"{}", 5.0)


def test_default_transport_posts_json_utf8_with_a_timeout(monkeypatch):
    from tapo_desktop.core.imou.client import UrllibTransport

    seen = {}

    class Resp:
        status = 200

        def __enter__(self):
            return self

        def __exit__(self, *a):
            return False

        def read(self):
            return b'{"result":{"code":"0"}}'

        def getcode(self):
            return 200

    def fake_open(request, timeout=None):
        seen.update(url=request.full_url, method=request.get_method(), data=request.data, timeout=timeout,
                    ctype=request.get_header("Content-type"))
        return Resp()

    monkeypatch.setattr("urllib.request.urlopen", fake_open)
    status, data = UrllibTransport().post("https://openapi-sg.easy4ip.com/openapi/accessToken", b'{"a":1}', 12.0)
    assert (status, data) == (200, b'{"result":{"code":"0"}}')
    assert seen == {"url": "https://openapi-sg.easy4ip.com/openapi/accessToken", "method": "POST",
                    "data": b'{"a":1}', "timeout": 12.0, "ctype": "application/json;charset=UTF-8"}


# ------------------------------------------------------------------------------------------ client: token + list
def test_unknown_data_center_cannot_build_a_client(cloud, clock):
    with pytest.raises(ValueError):
        make_client(cloud, clock, dc="evil")


def test_one_token_call_serves_many_calls(cloud, clock):
    client = make_client(cloud, clock)
    client.list_devices()
    client.list_devices()
    client.create_rtsp_url("MOCKSERIAL0001", "0", 1)
    assert cloud.count("accessToken") == 1


def test_every_call_goes_to_the_chosen_data_center_host(cloud, clock):
    client = make_client(cloud, clock, dc="fk")
    client.list_devices()
    assert {c.url.split("/openapi/")[0] for c in cloud.calls} == {"https://openapi-fk.easy4ip.com"}


def test_token_travels_inside_params_and_the_secret_nowhere(cloud, clock):
    client = make_client(cloud, clock)
    client.list_devices()
    listing = [c for c in cloud.calls if c.path == "listDeviceDetailsByPage"][0]
    assert listing.params["token"].startswith("At_")
    assert all(MOCK_APP_SECRET not in json.dumps(c.params) + json.dumps(c.system) for c in cloud.calls)


def test_wrong_secret_gives_one_auth_error_and_no_retry(cloud, clock):
    from tapo_desktop.core.imou.errors import ImouAuthError

    client = make_client(cloud, clock, secret="wrong-secret")
    with pytest.raises(ImouAuthError) as err:
        client.list_devices()
    assert "App ID" in str(err.value) and "secret" in str(err.value)
    assert cloud.paths() == ["accessToken"]


def test_list_devices_pages_and_maps_online_state(cloud, clock):
    cloud.devices = [make_device(f"MOCKSERIAL{i:04d}", f"Cam {i}") for i in range(55)]
    cloud.devices[3]["deviceStatus"] = "offline"
    cloud.devices[4]["deviceStatus"] = "sleep"
    client = make_client(cloud, clock)
    devices = client.list_devices()
    assert len(devices) == 55
    assert cloud.count("listDeviceDetailsByPage") == 2
    assert [p.params["pageSize"] for p in cloud.calls if p.path == "listDeviceDetailsByPage"] == [50, 50]
    assert devices[0].serial == "MOCKSERIAL0000" and devices[0].name == "Cam 0" and devices[0].online
    assert devices[0].channel == "0" and devices[0].model == "IPC-MOCK"
    assert not devices[3].online and not devices[4].online
    assert devices[4].status == "sleep"


def test_list_devices_stops_at_a_short_page(cloud, clock):
    client = make_client(cloud, clock)
    assert [d.serial for d in client.list_devices()] == ["MOCKSERIAL0001", "MOCKSERIAL0002"]
    assert cloud.count("listDeviceDetailsByPage") == 1


def test_list_devices_has_a_page_cap(cloud, clock):
    cloud.devices = [make_device(f"MOCKSERIAL{i:04d}") for i in range(500)]
    client = make_client(cloud, clock)
    assert len(client.list_devices()) <= 200
    assert cloud.count("listDeviceDetailsByPage") <= 4


# ------------------------------------------------------------------------------------------ client: live
def test_create_rtsp_url_decrypts_and_sends_the_documented_params(cloud, clock):
    client = make_client(cloud, clock)
    url = client.create_rtsp_url("MOCKSERIAL0001", "0", 1)
    assert url.startswith("rtsp://203.0.113.7:554/") and MOCK_RTSP_QUERY in url
    call = [c for c in cloud.calls if c.path == "createVideoDownloadStreamUrl"][0]
    assert call.params == {"token": cloud.last_issued_token(), "deviceId": "MOCKSERIAL0001", "channelId": "0",
                           "businessType": "real", "streamType": "1"}


def test_expired_token_is_refreshed_once_and_the_call_retried_once(cloud, clock):
    client = make_client(cloud, clock)
    client.list_devices()
    cloud.expire_tokens()
    assert len(client.list_devices()) == 2
    assert cloud.count("accessToken") == 2
    assert cloud.count("listDeviceDetailsByPage") == 3  # ok, TK1002, ok


def test_a_second_token_failure_is_not_retried_again(cloud, clock):
    from tapo_desktop.core.imou.errors import ImouTokenExpiredError

    client = make_client(cloud, clock)
    client.list_devices()
    cloud.fail("listDeviceDetailsByPage", "TK1002", times=None)
    with pytest.raises(ImouTokenExpiredError):
        client.list_devices()
    assert cloud.count("accessToken") == 2
    assert cloud.count("listDeviceDetailsByPage") == 3  # ok, TK1002, TK1002


def test_offline_camera_is_one_call_and_no_retry(cloud, clock):
    from tapo_desktop.core.imou.errors import ImouOfflineError

    cloud.devices[0]["deviceStatus"] = "offline"
    client = make_client(cloud, clock)
    client.list_devices()
    with pytest.raises(ImouOfflineError) as err:
        client.create_rtsp_url("MOCKSERIAL0001", "0", 1)
    assert "offline" in str(err.value).lower()
    assert cloud.count("createVideoDownloadStreamUrl") == 1


def test_a_dropped_connection_is_an_error_without_retry(cloud, clock):
    from tapo_desktop.core.imou.errors import ImouConnectionError

    client = make_client(cloud, clock)
    cloud.drop_connection = True
    with pytest.raises(ImouConnectionError):
        client.list_devices()
    assert cloud.calls == []


def test_a_malformed_reply_is_a_protocol_error(clock):
    from tapo_desktop.core.imou.client import ImouClient
    from tapo_desktop.core.imou.errors import ImouProtocolError

    client = ImouClient(MOCK_APP_ID, MOCK_APP_SECRET, "sg", transport=lambda u, b, t: (200, b"<html>"), clock=clock)
    with pytest.raises(ImouProtocolError):
        client.access_token()


def test_http_error_status_is_a_connection_error(clock):
    from tapo_desktop.core.imou.client import ImouClient
    from tapo_desktop.core.imou.errors import ImouConnectionError

    client = ImouClient(MOCK_APP_ID, MOCK_APP_SECRET, "sg", transport=lambda u, b, t: (502, b""), clock=clock)
    with pytest.raises(ImouConnectionError):
        client.access_token()


def test_bind_live_find_and_unbind(cloud, clock):
    client = make_client(cloud, clock)
    live = client.bind_live("MOCKSERIAL0001", "0", 1)
    assert live.live_token and live.hls.startswith("https://") and MOCK_HLS_QUERY in live.hls
    assert cloud.live_count() == 1
    found = client.find_live("MOCKSERIAL0001", "0")
    assert found is not None and found.live_token == live.live_token and found.hls == live.hls
    assert client.find_live("MOCKSERIAL0002", "0") is None
    client.unbind_live(live.live_token)
    assert cloud.live_count() == 0


def test_bind_live_twice_reports_the_existing_live_error(cloud, clock):
    from tapo_desktop.core.imou.errors import ImouLiveExistsError

    client = make_client(cloud, clock)
    client.bind_live("MOCKSERIAL0001", "0", 1)
    with pytest.raises(ImouLiveExistsError):
        client.bind_live("MOCKSERIAL0001", "0", 1)


def test_bind_live_params(cloud, clock):
    client = make_client(cloud, clock)
    client.bind_live("MOCKSERIAL0001", "0", 0)
    call = [c for c in cloud.calls if c.path == "bindDeviceLive"][0]
    assert call.params == {"token": cloud.last_issued_token(), "deviceId": "MOCKSERIAL0001", "channelId": "0",
                           "streamId": 0}


# ------------------------------------------------------------------------------------------ errors and the guard
@pytest.mark.parametrize("code", ["FL1002", "FL1004", "FL1005", "OP1013", "OP1014"])
def test_quota_codes_stop_all_calls_until_the_next_utc_day(cloud, clock, code):
    from tapo_desktop.core.imou.errors import ImouBlockedError, ImouQuotaError
    from tapo_desktop.core.imou.guard import ImouGuard, next_utc_midnight

    guard = ImouGuard(clock=clock)
    client = make_client(cloud, clock, guard=guard)
    client.list_devices()
    cloud.fail("createVideoDownloadStreamUrl", code)
    with pytest.raises(ImouQuotaError) as err:
        client.create_rtsp_url("MOCKSERIAL0001", "0", 1)
    assert "free quota used" in str(err.value).lower()
    calls_before = len(cloud.calls)
    for action in (client.list_devices, lambda: client.create_rtsp_url("MOCKSERIAL0001", "0", 1),
                   lambda: client.bind_live("MOCKSERIAL0001", "0", 1), client.access_token):
        with pytest.raises(ImouBlockedError) as blocked:
            action()
        assert "free quota used" in str(blocked.value).lower()
    assert len(cloud.calls) == calls_before
    until = next_utc_midnight(clock())
    assert guard.blocked()[0] == until
    clock.now = until + 1
    assert len(client.list_devices()) == 2


def test_next_utc_midnight():
    from tapo_desktop.core.imou.guard import next_utc_midnight

    assert next_utc_midnight(1_700_000_000.0) == 1_700_006_400.0  # 2023-11-14 22:13:20 UTC -> 2023-11-15 00:00
    assert next_utc_midnight(1_700_006_400.0) == 1_700_006_400.0 + 86_400  # exactly midnight: the next one


def test_signature_error_stops_without_retry_and_blocks_for_two_minutes(cloud, clock):
    from tapo_desktop.core.imou.errors import ImouAuthError, ImouBlockedError
    from tapo_desktop.core.imou.guard import ImouGuard

    guard = ImouGuard(clock=clock)
    client = make_client(cloud, clock, guard=guard, secret="wrong-secret")
    with pytest.raises(ImouAuthError):
        client.list_devices()
    assert cloud.paths() == ["accessToken"]
    with pytest.raises(ImouBlockedError) as blocked:
        client.list_devices()
    assert "App ID" in str(blocked.value)
    assert cloud.paths() == ["accessToken"]
    clock.advance(121)
    with pytest.raises(ImouAuthError):  # one new attempt after the block
        client.list_devices()
    assert cloud.paths() == ["accessToken", "accessToken"]


@pytest.mark.parametrize("code,msg", [("LV1008", "Real-name authentication is required (mock vendor text)"),
                                      ("OP1009", "Insufficient permissions (mock vendor text)")])
def test_vendor_text_is_shown_for_real_name_and_permission_errors(cloud, clock, code, msg):
    from tapo_desktop.core.imou.errors import ImouVendorError

    client = make_client(cloud, clock)
    client.list_devices()
    cloud.fail("createVideoDownloadStreamUrl", code, msg)
    with pytest.raises(ImouVendorError) as err:
        client.create_rtsp_url("MOCKSERIAL0001", "0", 1)
    assert msg in str(err.value) and code in str(err.value)
    assert str(err.value).startswith("iMou: ")


def test_guard_state_survives_a_restart_through_the_save_callback(cloud, clock):
    from tapo_desktop.core.imou.errors import ImouQuotaError
    from tapo_desktop.core.imou.guard import ImouGuard

    saved = {}
    guard = ImouGuard(clock=clock, load=lambda: saved.get("v"), save=lambda v: saved.__setitem__("v", v))
    client = make_client(cloud, clock, guard=guard)
    client.list_devices()
    cloud.fail("createVideoDownloadStreamUrl", "FL1004")
    with pytest.raises(ImouQuotaError):
        client.create_rtsp_url("MOCKSERIAL0001", "0", 1)
    again = ImouGuard(clock=clock, load=lambda: saved.get("v"), save=lambda v: saved.__setitem__("v", v))
    assert again.blocked() is not None
    again.clear()
    assert again.blocked() is None and ImouGuard(clock=clock, load=lambda: saved.get("v")).blocked() is None


def test_garbage_in_the_saved_guard_state_is_ignored(clock):
    from tapo_desktop.core.imou.guard import ImouGuard

    for junk in (None, "x", 5, {"until": "soon"}, {"until": float("nan")}, {"reason": "quota"}):
        assert ImouGuard(clock=clock, load=lambda junk=junk: junk).blocked() is None


# ------------------------------------------------------------------------------------------ strict allowlist
def test_allowed_paths_are_exactly_the_documented_set():
    from tapo_desktop.core.imou.client import ALLOWED_PATHS

    assert ALLOWED_PATHS == EXPECTED_PATHS


def test_client_public_methods_are_exactly_the_allowlisted_ones():
    from tapo_desktop.core.imou.client import ImouClient

    public = {n for n in dir(ImouClient) if not n.startswith("_") and callable(getattr(ImouClient, n))}
    assert public == EXPECTED_PUBLIC_METHODS


@pytest.mark.parametrize("path", ["/openapi/unBindDevice", "/openapi/bindDevice", "/openapi/restartDevice",
                                  "/openapi/modifyDeviceName", "/openapi/v2/device.name.set",
                                  "/openapi/controlMovePTZ", "/openapi/setDeviceSnap", "/openapi/upgradeDevice",
                                  "/openapi/wifiConfig", "/openapi/accessToken/../x", "openapi/accessToken", ""])
def test_any_other_path_is_refused_and_nothing_is_sent(cloud, clock, path):
    from tapo_desktop.core.imou.errors import ImouNotAllowedError

    client = make_client(cloud, clock)
    with pytest.raises(ImouNotAllowedError):
        client._call(path, {})
    assert cloud.calls == []


def test_source_of_the_iMou_core_names_no_write_or_device_changing_call():
    forbidden = ("unBindDevice", "bindDevice\"", "restartDevice", "reboot", "modifyDevice", "setDeviceSnap",
                 "controlMove", "controlPTZ", "wifi", "upgradeDevice", "device.name.set", "device.password",
                 "purchase", "createSubAccount", "modifyLivePlan", "formatDisk", "setMessageCallback")
    for path in (SRC / "core" / "imou").glob("*.py"):
        text = path.read_text(encoding="utf-8")
        for word in forbidden:
            assert word.lower() not in text.lower(), f"{path.name} mentions {word!r}"
    paths = {m for p in (SRC / "core" / "imou").glob("*.py")
             for m in re.findall(r"/openapi/[A-Za-z0-9_./]+", p.read_text(encoding="utf-8"))}
    assert paths <= EXPECTED_PATHS


# ------------------------------------------------------------------------------------------ nothing secret in logs
def test_no_secret_in_any_log_record_on_success_and_error_paths(cloud, clock, caplog):
    caplog.set_level(logging.DEBUG)
    client = make_client(cloud, clock)
    client.list_devices()
    url = client.create_rtsp_url("MOCKSERIAL0001", "0", 1)
    live = client.bind_live("MOCKSERIAL0001", "0", 1)
    client.unbind_live(live.live_token)
    cloud.fail("createVideoDownloadStreamUrl", "OP1009", f"denied for {url}")
    with pytest.raises(Exception):
        client.create_rtsp_url("MOCKSERIAL0001", "0", 1)
    cloud.fail("listDeviceDetailsByPage", "SN1001")
    with pytest.raises(Exception):
        client.list_devices()
    text = "\n".join(r.getMessage() + " " + str(r.exc_info and r.exc_info[1]) for r in caplog.records)
    assert MOCK_APP_SECRET not in text
    assert MOCK_RTSP_QUERY not in text and MOCK_HLS_QUERY not in text
    assert cloud.last_issued_token() not in text
