"""Local access token (LAT) fetch from the IoT cloud thing list, against MockTapoCloud only.

[spec:lat.cloud.base_url] getAppServiceUrl, [spec:lat.cloud.headers] ``Authorization: ut|<token>`` +
app headers, [spec:lat.source.thing_list] GET /v2/things paging and ``localAccessToken``.
"""
from __future__ import annotations

import logging

import pytest

from tapo_desktop.core.cloud.client import CloudError, TapoCloudClient
from tests.fixtures.mock_cloud import IOT_SERVICE_ID, make_cloud_device
from tests.fixtures.phase2_fixtures import DEFAULT_EMAIL, DEFAULT_PASSWORD

pytestmark = pytest.mark.integration

TERM = "0123456789ABCDEF0123456789ABCDEF"
LAT1 = "LAT-one-0123456789abcdef"
LAT3 = "LAT-three-fedcba9876543210"


def _client(cloud, **kw) -> TapoCloudClient:
    return TapoCloudClient(terminal_uuid=TERM, base_url=cloud.base_url, timeout=5.0, **kw)


def _signed_in(cloud) -> TapoCloudClient:
    c = _client(cloud)
    c.login(DEFAULT_EMAIL, DEFAULT_PASSWORD)
    return c


def _thing_gets(cloud) -> list[dict]:
    return [r for r in cloud.request_log if r.get("method") == "GET" and r["path"].endswith("/v2/things")]


def _logins(cloud) -> int:
    return len(cloud.calls("/account/login")) + len(cloud.calls("/account/captchaLogin"))


def _id(cloud, i: int) -> str:
    return cloud.devices[i]["deviceId"]


def test_get_service_urls_is_signed_and_carries_the_token(mock_cloud):
    c = _signed_in(mock_cloud)
    urls = c.get_service_urls([IOT_SERVICE_ID])
    assert urls == {IOT_SERVICE_ID: mock_cloud.iot_url}
    (call,) = mock_cloud.calls("/api/v2/common/getAppServiceUrl")
    assert call["path"].startswith("/eu/")             # the account's regional app server
    assert call["signature_ok"] is True
    assert call["query"]["token"] == c.token
    assert call["body"] == {"serviceIds": [IOT_SERVICE_ID]}


def test_local_access_tokens_sends_the_app_headers(mock_cloud):
    mock_cloud.local_tokens = {_id(mock_cloud, 0): LAT1}
    c = _signed_in(mock_cloud)
    tokens = c.local_access_tokens()
    assert tokens == {_id(mock_cloud, 0): LAT1}
    (get,) = _thing_gets(mock_cloud)
    hdr = {k.lower(): v for k, v in get["headers"].items()}
    assert hdr["authorization"] == f"ut|{c.token}"
    assert hdr["app-cid"] == f"app:TP-Link_Tapo_Android:{TERM}"
    assert hdr["x-app-name"] == "TP-Link_Tapo_Android"
    assert hdr["x-app-version"] == "3.22.806"
    assert hdr["x-term-id"] == TERM
    assert hdr["x-strict"] == "0"
    assert "x-authorization" not in hdr                # the IoT API is not HMAC-signed
    q = get["query"]
    assert q["page"] == "0" and q["pageSize"] == "100"
    assert "SMART.IPCAMERA" in q["deviceTypes"].split(",")
    assert q.get("includeKasaShareDevices") == "true"


def test_local_access_tokens_pages_until_total(mock_cloud_factory):
    devices = [make_cloud_device(i, alias=f"Cam {i}") for i in range(1, 4)]
    cloud = mock_cloud_factory(devices=devices,
                               local_tokens={devices[0]["deviceId"]: LAT1, devices[2]["deviceId"]: LAT3})
    cloud.things_max_page_size = 1                     # server caps the page: three pages needed
    c = _signed_in(cloud)
    assert c.local_access_tokens() == {devices[0]["deviceId"]: LAT1, devices[2]["deviceId"]: LAT3}
    assert [g["query"]["page"] for g in _thing_gets(cloud)] == ["0", "1", "2"]


def test_missing_null_and_empty_tokens_are_left_out(mock_cloud_factory):
    devices = [make_cloud_device(i) for i in range(1, 4)]
    cloud = mock_cloud_factory(devices=devices, local_tokens={devices[0]["deviceId"]: "",
                                                              devices[1]["deviceId"]: LAT1})
    c = _signed_in(cloud)
    assert c.local_access_tokens() == {devices[1]["deviceId"]: LAT1}


def test_tolerant_parsing_of_odd_thing_lists(mock_cloud, monkeypatch):
    c = _signed_in(mock_cloud)
    weird = {"data": [None, 5, "x", {"thingName": 7, "localAccessToken": "t"}, {"localAccessToken": "t"},
                      {"thingName": "A", "localAccessToken": 12}, {"thingName": "B", "localAccessToken": "tok-B"},
                      {"thingName": "C", "localAccessToken": None}],
             "total": "not-a-number"}
    monkeypatch.setattr(mock_cloud, "_handle_iot_get", lambda path, q, h: (200, weird))
    assert c.local_access_tokens() == {"B": "tok-B"}
    monkeypatch.setattr(mock_cloud, "_handle_iot_get", lambda path, q, h: (200, {"data": "nope"}))
    assert c.local_access_tokens() == {}


def test_non_json_thing_list_is_a_cloud_error(mock_cloud, monkeypatch):
    c = _signed_in(mock_cloud)
    monkeypatch.setattr(mock_cloud, "_handle_iot_get", lambda path, q, h: (500, ["not", "an", "object"]))
    with pytest.raises(CloudError):
        c.local_access_tokens()


def test_expired_token_relogs_in_once_and_retries(mock_cloud):
    mock_cloud.local_tokens = {_id(mock_cloud, 0): LAT1}
    c = _signed_in(mock_cloud)
    assert c.local_access_tokens() == {_id(mock_cloud, 0): LAT1}
    old = c.token
    before = _logins(mock_cloud)
    mock_cloud.expire_tokens()
    assert c.local_access_tokens() == {_id(mock_cloud, 0): LAT1}
    assert _logins(mock_cloud) == before + 1
    assert c.token and c.token != old
    statuses = [g["headers"].get("Authorization") for g in _thing_gets(mock_cloud)]
    assert statuses[-1] == f"ut|{c.token}"


def test_expired_token_during_service_url_lookup_relogs_in(mock_cloud):
    mock_cloud.local_tokens = {_id(mock_cloud, 1): LAT3}
    c = _signed_in(mock_cloud)
    before = _logins(mock_cloud)
    mock_cloud.expire_tokens()
    assert c.local_access_tokens() == {_id(mock_cloud, 1): LAT3}
    assert _logins(mock_cloud) == before + 1


def test_persistent_rejection_raises_after_one_relogin(mock_cloud, monkeypatch):
    c = _signed_in(mock_cloud)
    c.get_service_urls([IOT_SERVICE_ID])
    before = _logins(mock_cloud)
    monkeypatch.setattr(mock_cloud, "_handle_iot_get",
                        lambda path, q, h: (401, {"code": -20651, "message": "Token expired"}))
    with pytest.raises(CloudError):
        c.local_access_tokens()
    assert _logins(mock_cloud) == before + 1


def test_not_signed_in_raises(mock_cloud):
    with pytest.raises(CloudError):
        _client(mock_cloud).local_access_tokens()
    with pytest.raises(CloudError):
        _client(mock_cloud).get_service_urls([IOT_SERVICE_ID])


def test_tokens_never_logged_or_in_repr(mock_cloud, caplog):
    mock_cloud.local_tokens = {_id(mock_cloud, 0): LAT1}
    caplog.set_level(logging.DEBUG)
    c = _signed_in(mock_cloud)
    mock_cloud.expire_tokens()
    c.local_access_tokens()
    text = caplog.text + repr(c) + str(c)
    assert LAT1 not in text
    assert c.token not in text
    assert DEFAULT_PASSWORD not in text


def test_https_bootstrap_refuses_a_plain_http_iot_server(monkeypatch):
    c = TapoCloudClient(terminal_uuid=TERM, base_url="https://n-wap.i.tplinkcloud.com")
    c._TapoCloudClient__token = "tok"                   # pretend to be signed in (no network)
    c._region = "https://euw1-app-server.example"
    monkeypatch.setattr(c, "get_service_urls", lambda ids: {IOT_SERVICE_ID: "http://evil.example"})
    with pytest.raises(CloudError):
        c.local_access_tokens()
