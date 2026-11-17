"""RelayClient: CIPC host discovery, /v2/relay/request slot request and the TLS relay tunnel
[spec:remote.relay.cipc_host.discovery, remote.relay.cipc.*, remote.relay.tunnel.*] against the mock
cloud + mock relay (no ffmpeg, no real network)."""
from __future__ import annotations

import re

import pytest

from tapo_desktop.core.cloud.client import CloudError
from tapo_desktop.core.cloud.relay import (
    DEFAULT_CIPC_HOST,
    MAX_REDIRECTS,
    RelayClient,
    RelayError,
    RelaySlot,
    cloud_type_for,
)
from tapo_desktop.core.errors import TapoConnectionError, TapoError
from tests.fixtures.mock_relay import EXPECTED_HEADERS
from tests.phase3.conftest import DEFAULT_EMAIL, DEVICE, TERMINAL

pytestmark = [pytest.mark.integration, pytest.mark.timeout(60)]

DEV = DEVICE["deviceId"]
NO_COOKIE = [h for h in EXPECTED_HEADERS if h != "Cookie"]


def _relay_calls(cloud) -> list[dict]:
    return cloud.calls("/v2/relay/request")


# ---------------------------------------------------------------------------------- slot request
def test_slot_request_headers_body_and_cipc_discovery(relay_client, relay_cloud, signed_in_cloud, mock_relay):
    slot = relay_client.request_slot(DEV, "SMART.IPCAMERA", "C210", "VGA")
    discovery = [c for c in relay_cloud.calls("/getAppServiceUrl") if "cipc.api.cloud" in c["body"]["serviceIds"]]
    assert discovery and discovery[0]["signature_ok"] is True
    call = _relay_calls(relay_cloud)[-1]
    hdr = call["headers"]
    assert call["path"] == "/cipc/v2/relay/request"
    assert hdr["Authorization"] == signed_in_cloud.token  # raw account token, no "ut|"
    assert hdr["User-Agent"] == "Tapo_APP/C210/3.22.806_Android/Android 14"
    assert re.fullmatch(r"[A-Za-z0-9]{10}", hdr["X-Request-Signature"])
    assert (hdr["X-Client-Id"], hdr["X-Source"], hdr["X-Brand"], hdr["X-Ca-Type"]) == \
        (TERMINAL, "tapo-app", "TPLINK", "cloud-self")
    assert hdr["Content-Type"].startswith("application/json")
    body = call["body"]
    assert body == {"deviceId": DEV, "dataTimeoutCount": 0, "cloudType": 2, "deviceType": "SMART.IPCAMERA",
                    "playerId": TERMINAL, "rootCaVer": "1", "preConnection": 0, "resolution": "VGA",
                    "streamType": 0, "customParams": "{}", "trackId": body["trackId"]}
    assert re.fullmatch(r"[0-9a-f-]{36}", body["trackId"])
    assert isinstance(slot, RelaySlot)
    assert (slot.relay_host, slot.relay_port, slot.relay_ip) == ("localhost", mock_relay.port, "127.0.0.1")
    assert slot.relay_path.startswith(f"/relayservice?deviceid={DEV}&type=video&resolution=VGA")
    assert (slot.device_id, slot.resolution, slot.track_id) == (DEV, "VGA", body["trackId"])
    assert slot.token in mock_relay.slots
    assert slot.token not in repr(slot) and slot.token not in str(slot)


def test_cipc_host_is_cached(relay_client, relay_cloud):
    relay_client.request_slot(DEV, model="C210")
    relay_client.request_slot(DEV, model="C210", resolution="HD")
    lookups = [c for c in relay_cloud.calls("/getAppServiceUrl") if "cipc.api.cloud" in c["body"]["serviceIds"]]
    assert len(lookups) == 1
    assert _relay_calls(relay_cloud)[-1]["body"]["resolution"] == "HD"


def test_cloud_type_1_only_when_asked(relay_client, relay_cloud):
    relay_client.request_slot(DEV, model="C210", cloud_type=1)
    assert _relay_calls(relay_cloud)[-1]["body"]["cloudType"] == 1


@pytest.mark.parametrize("raw,expected", [
    ({}, 2), (None, 2), ({"isSupportIoTCloud": True}, 2), ({"isSupportIoTCloud": False}, 1),
    ({"is_support_iot_cloud": False}, 1), ({"isSupportIoTCloud": None}, 2),
])
def test_cloud_type_for_device_list_entry(raw, expected):
    assert cloud_type_for(raw) == expected


def test_relogin_once_on_token_invalid(relay_client, relay_cloud, signed_in_cloud):
    logins = len(relay_cloud.calls("/account/login"))
    old = signed_in_cloud.token
    relay_client.cipc_base()  # resolve the host while the token is still valid
    relay_cloud.expire_tokens()
    slot = relay_client.request_slot(DEV, model="C210")
    assert slot.token
    calls = _relay_calls(relay_cloud)
    assert calls[-2]["headers"]["Authorization"] == old
    assert calls[-1]["headers"]["Authorization"] == signed_in_cloud.token != old
    assert len(relay_cloud.calls("/account/login")) + len(relay_cloud.calls("/account/captchaLogin")) == logins + 1


def test_token_invalid_and_relogin_fails(relay_client, relay_cloud):
    relay_client.cipc_base()
    relay_cloud.expire_tokens()
    relay_cloud.accounts[DEFAULT_EMAIL] = "password-changed-elsewhere"
    with pytest.raises(CloudError):
        relay_client.request_slot(DEV, model="C210")
    assert len(_relay_calls(relay_cloud)) == 1  # no retry loop


def test_slot_refused_device_offline(relay_client, relay_cloud):
    relay_cloud.relay_errors[DEV] = -1007
    with pytest.raises(RelayError) as info:
        relay_client.request_slot(DEV, model="C210")
    assert info.value.code == -1007 and info.value.fatal
    assert "camera is offline" in str(info.value).lower()


def test_slot_refused_other_error_is_not_fatal(relay_client, relay_cloud):
    relay_cloud.relay_errors[DEV] = -1014
    with pytest.raises(RelayError) as info:
        relay_client.request_slot(DEV, model="C210")
    assert info.value.code == -1014 and not info.value.fatal


@pytest.mark.parametrize("listed,expected", [
    ("", f"https://{DEFAULT_CIPC_HOST}"),
    ("use1-cipc-api.i.tplinkcloud.com", "https://use1-cipc-api.i.tplinkcloud.com"),
    ("https://euw1-cipc-api.i.tplinkcloud.com/", "https://euw1-cipc-api.i.tplinkcloud.com"),
])
def test_cipc_base_fallback_and_scheme(relay_client, relay_cloud, listed, expected):
    relay_cloud.cipc_service_url = listed
    assert relay_client.cipc_base() == expected


def test_insecure_cipc_url_rejected_for_https_account(signed_in_cloud, relay_cloud, monkeypatch):
    relay_cloud.cipc_service_url = "http://evil.example.com"
    monkeypatch.setattr(signed_in_cloud, "base_url", "https://n-wap.i.tplinkcloud.com")
    client = RelayClient(signed_in_cloud, terminal_uuid=TERMINAL, timeout=5)
    with pytest.raises(CloudError):
        client.cipc_base()


def test_not_signed_in():
    from tapo_desktop.core.cloud.client import TapoCloudClient

    client = RelayClient(TapoCloudClient(terminal_uuid=TERMINAL, base_url="http://127.0.0.1:9"), timeout=2)
    with pytest.raises(TapoError):
        client.request_slot(DEV)


# ---------------------------------------------------------------------------------- tunnel
def test_tunnel_request_bytes_exact_order(relay_client):
    slot = RelaySlot(device_id=DEV, relay_host="relay.example.com", relay_port=443,
                     relay_path=f"/relayservice?deviceid={DEV}&type=video&resolution=VGA", relay_ip="10.0.0.1",
                     track_id="11111111-2222-3333-4444-555555555555", resolution="VGA", token="TOKEN123",
                     elb_cookie="AWSELB=xyz")
    raw = relay_client.tunnel_request(slot, redirect_times=1, arrive_latency_ms=42)
    assert raw.endswith(b"\r\n\r\n") and raw.count(b"\r\n\r\n") == 1
    lines = raw.decode("latin-1").split("\r\n")[:-2]
    assert lines[0] == f"POST /relayservice?deviceid={DEV}&type=video&resolution=VGA&retryTime=0 HTTP/1.1"
    assert [tuple(line.split(": ", 1)) for line in lines[1:]] == [
        ("User-Agent", "Client=TP-Link_Tapo_Android Android 3.22.806/1.3"), ("Keep-Relay", "3600"),
        ("Accept", "*/*"), ("Content-Type", "multipart/mixed;boundary=--client-stream-boundary--"),
        ("Host", "relay.example.com"), ("Content-Length", "9223372036854775807"), ("X-token", "TOKEN123"),
        ("X-Client-Model", "Windows"), ("X-Client-UUID", TERMINAL),
        ("X-Track-Id", "11111111-2222-3333-4444-555555555555"), ("X-Redirect-Times", "1"),
        ("X-Pull-Mode", "2"), ("Cookie", "AWSELB=xyz"), ("X-Version", "2.0"), ("X-Arrive-Latency", "42")]
    no_cookie = RelaySlot(**{**slot.__dict__, "elb_cookie": ""})
    names = [line.split(":")[0] for line in relay_client.tunnel_request(no_cookie).decode().split("\r\n")[1:-2]]
    assert names == NO_COOKIE


def test_tunnel_opens_with_exact_headers(relay_client, mock_relay):
    slot = relay_client.request_slot(DEV, model="C210")
    sock = relay_client.open_tunnel(slot)
    try:
        entry = mock_relay.tunnels[-1]
        assert entry["status"] == "200"
        assert [n for n, _ in entry["headers"]] == NO_COOKIE
        assert dict(entry["headers"])["X-token"] == slot.token
        assert entry["line"].endswith("&retryTime=0 HTTP/1.1")
        assert sock.server_hostname == "localhost"
    finally:
        sock.close()


def test_tunnel_sends_elb_cookie(relay_client, mock_relay):
    mock_relay.elb_cookie = "AWSALB=cookie-value"
    slot = relay_client.request_slot(DEV, model="C210")
    assert slot.elb_cookie == "AWSALB=cookie-value"
    relay_client.open_tunnel(slot).close()
    entry = mock_relay.tunnels[-1]
    assert entry["status"] == "200" and dict(entry["headers"])["Cookie"] == "AWSALB=cookie-value"
    assert [n for n, _ in entry["headers"]] == list(EXPECTED_HEADERS)


def test_tunnel_follows_304_redirects(relay_client, mock_relay):
    mock_relay.redirects = 2
    slot = relay_client.request_slot(DEV, model="C210")
    relay_client.open_tunnel(slot).close()
    assert [t["status"] for t in mock_relay.tunnels] == ["304", "304", "200"]
    assert [dict(t["headers"])["X-Redirect-Times"] for t in mock_relay.tunnels] == ["0", "1", "2"]
    tokens = [dict(t["headers"])["X-token"] for t in mock_relay.tunnels]
    assert len(set(tokens)) == 3  # every redirect hands out a new token


def test_tunnel_gives_up_after_too_many_redirects(relay_client, mock_relay):
    mock_relay.redirects = 50
    slot = relay_client.request_slot(DEV, model="C210")
    with pytest.raises(RelayError) as info:
        relay_client.open_tunnel(slot)
    assert "redirect" in str(info.value).lower()
    assert len(mock_relay.tunnels) == MAX_REDIRECTS + 1


def test_tunnel_device_offline_with_cloud_type_1(relay_client, mock_relay):
    """Real-world finding: cloudType 1 gets a slot but the tunnel answers 500 -1007."""
    slot = relay_client.request_slot(DEV, model="C210", cloud_type=1)
    with pytest.raises(RelayError) as info:
        relay_client.open_tunnel(slot)
    assert info.value.code == -1007 and info.value.fatal
    assert "camera is offline" in str(info.value).lower()
    assert slot.token not in str(info.value)


def test_tunnel_illegal_request_when_header_missing(relay_client, mock_relay, monkeypatch):
    original = RelayClient.tunnel_request

    def without_version(self, slot, redirect_times=0, arrive_latency_ms=0):
        raw = original(self, slot, redirect_times, arrive_latency_ms)
        return raw.replace(b"X-Version: 2.0\r\n", b"")

    monkeypatch.setattr(RelayClient, "tunnel_request", without_version)
    slot = relay_client.request_slot(DEV, model="C210")
    with pytest.raises(RelayError) as info:
        relay_client.open_tunnel(slot)
    assert info.value.code == 3 and "illegal_request" in str(info.value)
    assert mock_relay.tunnels[-1]["status"].startswith("400")


def test_tunnel_dropped_connection(relay_client, mock_relay):
    mock_relay.mode = "drop"
    slot = relay_client.request_slot(DEV, model="C210")
    with pytest.raises(TapoConnectionError) as info:
        relay_client.open_tunnel(slot)
    assert not getattr(info.value, "fatal", False)


def test_tunnel_rejects_untrusted_certificate(signed_in_cloud, relay_cloud):
    client = RelayClient(signed_in_cloud, terminal_uuid=TERMINAL, timeout=5)  # TP-Link roots + system only
    slot = client.request_slot(DEV, model="C210")
    with pytest.raises(TapoConnectionError):
        client.open_tunnel(slot)
