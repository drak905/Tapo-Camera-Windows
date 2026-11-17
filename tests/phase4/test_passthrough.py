"""Cloud passthrough (IoT ``services-sync``) envelope, headers, reply shapes, error mapping and silent
re-login, against the mock cloud [spec:remote.control.iot_passthrough.*]."""
from __future__ import annotations

import json

import pytest

from tapo_desktop.core.control_client import dtos
from tapo_desktop.core.errors import TapoConnectionError, TapoDeviceError, TapoProtocolError
from tapo_desktop.models import ThingInfo
from tests.phase4.conftest import DEFAULT_EMAIL, DEVICE_ID, OFFLINE_ID, TERMINAL

pytestmark = [pytest.mark.integration, pytest.mark.timeout(60)]


def _sync_calls(cloud):
    return [r for r in cloud.request_log if r["path"].endswith("/services-sync")]


def test_thing_records_are_kept(signed_in_cloud, relay_cloud):
    assert signed_in_cloud.things == {}
    signed_in_cloud.local_access_tokens()
    thing = signed_in_cloud.things[DEVICE_ID]
    assert isinstance(thing, ThingInfo)
    assert thing.thing_name == DEVICE_ID
    assert thing.app_server_url == relay_cloud.regional_url  # appServerUrlV2
    assert thing.role == 0 and thing.online is True
    assert signed_in_cloud.things[OFFLINE_ID].online is False


def test_thing_url_falls_back_to_app_server_url(signed_in_cloud, relay_cloud, monkeypatch):
    orig = relay_cloud._thing

    def thing(d):
        out = orig(d)
        out["appServerUrlV2"] = ""
        out["appServerUrl"] = relay_cloud.regional_url + "/v1only"
        return out

    monkeypatch.setattr(relay_cloud, "_thing", thing)
    signed_in_cloud.local_access_tokens()
    assert signed_in_cloud.things[DEVICE_ID].app_server_url == relay_cloud.regional_url + "/v1only"


def test_envelope_headers_and_items(passthrough, relay_cloud, remote_camera, signed_in_cloud):
    reqs = [dtos.get_device_info_req(), dtos.get_led_req()]
    items = passthrough.multiple_request(DEVICE_ID, relay_cloud.regional_url, reqs)
    assert [i["method"] for i in items] == ["getDeviceInfo", "getLedStatus"]
    assert all(i["error_code"] == 0 for i in items)
    assert items[0]["result"]["device_info"]["basic_info"]["device_model"] == "C210"
    call = _sync_calls(relay_cloud)[-1]
    assert call["path"] == f"/eu/v1/things/{DEVICE_ID}/services-sync"
    assert call["body"] == {"serviceId": "passthrough", "inputParams": {"requestData": {
        "method": "multipleRequest", "params": {"requests": reqs}}}}
    hdr = {k.lower(): v for k, v in call["headers"].items()}
    assert hdr["authorization"] == f"ut|{signed_in_cloud.token}"
    assert hdr["app-cid"] == f"app:TP-Link_Tapo_Android:{TERMINAL}"
    assert hdr["content-type"].startswith("application/json")
    assert "x-authorization" not in hdr  # IoT path is not HMAC-signed
    assert [r["method"] for r in remote_camera.request_log] == ["getDeviceInfo", "getLedStatus"]


def test_direct_output_params_shape(passthrough, relay_cloud, remote_camera):
    relay_cloud.passthrough_shape = "direct"
    items = passthrough.multiple_request(DEVICE_ID, relay_cloud.regional_url, [dtos.get_privacy_req()])
    assert items == [{"method": "getLensMaskConfig", "result": {"lens_mask": {"lens_mask_info": {"enabled": "off"}}},
                      "error_code": 0}]


def test_per_item_errors_are_kept(passthrough, relay_cloud, remote_camera):
    remote_camera.inject_error("getLedStatus", -40106)
    items = passthrough.multiple_request(DEVICE_ID, relay_cloud.regional_url,
                                         [dtos.get_privacy_req(), dtos.get_led_req()])
    assert [i["error_code"] for i in items] == [0, -40106]


@pytest.mark.parametrize("thing,setup,exc,text", [
    (OFFLINE_ID, None, TapoConnectionError, "Camera is offline"),
    (DEVICE_ID, None, TapoConnectionError, "Camera did not respond"),  # no camera behind the cloud
    (DEVICE_ID, (200, -20571), TapoConnectionError, "Camera is offline"),
    (DEVICE_ID, (200, -20002), TapoConnectionError, "Camera did not respond"),
    (DEVICE_ID, (429, 15000), TapoConnectionError, "busy"),
    (DEVICE_ID, (400, 12345), TapoDeviceError, "12345"),
])
def test_cloud_errors_have_user_messages(passthrough, relay_cloud, thing, setup, exc, text):
    if setup is not None:
        relay_cloud.passthrough_errors[thing] = setup
    with pytest.raises(exc, match=text):
        passthrough.multiple_request(thing, relay_cloud.regional_url, [dtos.get_led_req()])


def test_inner_error_code_raises_device_error(passthrough, relay_cloud, monkeypatch):
    def reply(thing, body, headers):
        return 200, {"outputParams": {"responseData": {"error_code": -40210, "result": {}}}}

    monkeypatch.setattr(relay_cloud, "_handle_services_sync", reply)
    with pytest.raises(TapoDeviceError) as err:
        passthrough.multiple_request(DEVICE_ID, relay_cloud.regional_url, [dtos.get_led_req()])
    assert err.value.code == -40210


@pytest.mark.parametrize("payload", [{}, {"outputParams": None}, {"outputParams": {"responseData": {"error_code": 0}}}])
def test_malformed_reply_is_a_protocol_error(passthrough, relay_cloud, monkeypatch, payload):
    monkeypatch.setattr(relay_cloud, "_handle_services_sync", lambda *a: (200, payload))
    with pytest.raises(TapoProtocolError):
        passthrough.multiple_request(DEVICE_ID, relay_cloud.regional_url, [dtos.get_led_req()])


def test_expired_token_relogs_in_once(passthrough, relay_cloud, remote_camera):
    logins = len(relay_cloud.calls("/account/login"))
    relay_cloud.expire_tokens()
    items = passthrough.multiple_request(DEVICE_ID, relay_cloud.regional_url, [dtos.get_led_req()])
    assert items[0]["error_code"] == 0
    assert len(relay_cloud.calls("/account/login")) == logins + 1
    statuses = [r for r in _sync_calls(relay_cloud)]
    assert len(statuses) == 2  # refused once, then accepted with the new token


def test_token_rejected_twice_gives_up(passthrough, relay_cloud, monkeypatch):
    monkeypatch.setattr(relay_cloud, "_handle_services_sync",
                        lambda *a: (401, {"code": -20651, "message": "Token expired"}))
    with pytest.raises(TapoConnectionError, match="sign in again"):
        passthrough.multiple_request(DEVICE_ID, relay_cloud.regional_url, [dtos.get_led_req()])
    assert len(relay_cloud.calls("/account/login")) == 2  # first sign-in + one silent re-login


@pytest.mark.parametrize("url", ["ftp://127.0.0.1/eu", "", "not a url"])
def test_bad_thing_url_is_refused(passthrough, url):
    with pytest.raises(TapoProtocolError):
        passthrough.multiple_request(DEVICE_ID, url, [dtos.get_led_req()])


def test_unreachable_cloud_is_a_connection_error(signed_in_cloud):
    from tapo_desktop.core.cloud.passthrough import CloudPassthrough

    with pytest.raises(TapoConnectionError):
        CloudPassthrough(signed_in_cloud, timeout=2).multiple_request(
            DEVICE_ID, "http://127.0.0.1:9/eu", [dtos.get_led_req()])


def test_no_secrets_in_reprs(passthrough, remote_client, signed_in_cloud):
    token = signed_in_cloud.token
    for obj in (passthrough, remote_client):
        assert token not in repr(obj) and token not in str(obj)


def test_thing_name_is_path_quoted(passthrough, relay_cloud, monkeypatch):
    seen = []
    monkeypatch.setattr(relay_cloud, "_handle_services_sync", lambda thing, body, headers: (
        seen.append(thing) or (200, {"outputParams": {"responseData": {"error_code": 0, "result": {"responses": [
            {"method": "getLedStatus", "result": {}, "error_code": 0}]}}}})))
    items = passthrough.multiple_request("A/B?x", relay_cloud.regional_url, [dtos.get_led_req()])
    assert [i["method"] for i in items] == ["getLedStatus"]
    assert seen == ["A/B?x"]  # one path segment on the wire (quoted), no query injection


def test_empty_batch_sends_nothing(passthrough, relay_cloud):
    assert passthrough.multiple_request(DEVICE_ID, relay_cloud.regional_url, []) == []
    assert _sync_calls(relay_cloud) == []


def test_requests_are_json_objects_not_strings(passthrough, relay_cloud, remote_camera):
    passthrough.multiple_request(DEVICE_ID, relay_cloud.regional_url, [dtos.set_led_req(False)])
    body = _sync_calls(relay_cloud)[-1]["body"]
    assert isinstance(body["inputParams"]["requestData"], dict)
    json.dumps(body)
    assert remote_camera.state.led is False
    assert DEFAULT_EMAIL  # imported for symmetry with the other phase tests
