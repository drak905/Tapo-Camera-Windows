"""Lane A (Kami Home account): signing, regions, DTOs, client and KamiAccountManager, all against a local mock.

Nothing here talks to a real Kami server. The mock follows ``specs/kami_auth_spec.json`` and
``specs/kami_cloud_spec.json``; passing these tests proves agreement with the spec, not with the real server.
"""
from __future__ import annotations

import base64
import hashlib
import hmac
import json
import logging
import os
import pathlib
import subprocess
import sys

os.environ.setdefault("QT_QPA_PLATFORM", "offscreen")

import pytest  # noqa: E402

from tapo_desktop.core.errors import TapoConnectionError  # noqa: E402
from tapo_desktop.core.kami import app_key as app_key_mod  # noqa: E402
from tapo_desktop.core.kami.client import KamiClient  # noqa: E402
from tapo_desktop.core.kami.dtos import (  # noqa: E402
    KamiAuthError, KamiConnectionError, KamiExtraStepError, KamiKeyMissingError, KamiProtocolError,
    KamiServerError, KamiSession, KamiSessionExpired, parse_devices, parse_login,
)
from tapo_desktop.core.kami.regions import countries, gateway_url, region_for  # noqa: E402
from tapo_desktop.core.kami.signing import encode_query, password_hash, request_hmac  # noqa: E402
from tapo_desktop.models import (  # noqa: E402
    ACCOUNT_VAULT_ID, KAMI_ACCOUNT_VAULT_ID, CameraConfig, CredentialKind, Credentials,
)
from tapo_desktop.services.config_store import (  # noqa: E402
    SETTING_KAMI_ACCOUNT, SETTING_KAMI_LAST_SIGN_IN_FAILED,
)
from tapo_desktop.services.kami_account import SETTING_KAMI_BLOCKED_UNTIL  # noqa: E402
from tests.e2e.fakes import make_config  # noqa: E402
from tests.fixtures.mock_kami_cloud import (  # noqa: E402
    EXTRA_STEP_CODE, MOCK_APP_KEY, REJECT_CODE, SESSION_INVALID_CODE, MockKamiCloud, make_device,
)
from tests.phase8.conftest import rebuild  # noqa: E402

REPO = pathlib.Path(__file__).resolve().parents[2]
EMAIL, PASSWORD = "owner@example.com", "pw-correct-1"
KEY_FILE = pathlib.Path(r"F:\kami_re\kami_app_key.txt")


def click(env, qtbot, email=EMAIL, password=PASSWORD, country="DE"):
    env.manager.sign_in(email, password, country)
    env.idle(qtbot)


def fake_clock(start=1_000_000.0):
    state = {"t": start}

    def now():
        return state["t"]

    now.state = state
    return now


# ====================================================================== signing [spec:kami.auth.*]
def test_password_hash_is_base64_hmac_sha256():
    want = base64.b64encode(hmac.new(b"k-test", "pa ss+/=é".encode(), hashlib.sha256).digest()).decode()
    assert password_hash("pa ss+/=é", "k-test") == want
    assert "\n" not in password_hash("x" * 500, "k-test")  # NO_WRAP: one line


def test_request_hmac_keeps_order_skips_hmac_and_does_not_url_encode():
    pairs = [("seq", "1"), ("userid", "u 1&x"), ("hmac", "old")]
    message = "seq=1&userid=u 1&x"
    want = base64.b64encode(hmac.new(b"tok&sec", message.encode(), hashlib.sha1).digest()).decode()
    assert request_hmac(pairs, "tok", "sec") == want
    # order matters: the same pairs reversed sign differently
    assert request_hmac(list(reversed(pairs[:2])), "tok", "sec") != request_hmac(pairs[:2], "tok", "sec")


def test_encode_query_percent_encodes_plus_space_and_non_ascii():
    q = encode_query([("a", "x+y z"), ("b", "é@")])
    assert q.startswith("a=x%2By%20z&b=")
    assert "+" not in q and " " not in q


# ====================================================================== regions
def test_countries_table():
    table = countries()
    codes = [c for c, _ in table]
    assert len(codes) == len(set(codes)) and all(len(c) == 2 and c.isupper() for c in codes)
    assert [n for _, n in table] == sorted(n for _, n in table)  # by English name
    assert len(table) > 200
    assert ("DE", "Germany") in table and ("US", "United States") in table


@pytest.mark.parametrize("code,region", [("US", "us"), ("CA", "us"), ("BR", "us"), ("DE", "eu"), ("GB", "eu"),
                                         ("ZA", "eu"), ("SG", "sea"), ("JP", "sea"), ("AU", "sea"),
                                         ("VN", "sea"), ("CN", "cn"), ("de", "eu")])
def test_region_for(code, region):
    assert region_for(code) == region


def test_region_for_unknown_country_raises():
    with pytest.raises(ValueError):
        region_for("ZZ")
    with pytest.raises(ValueError):
        region_for("")


def test_region_counts_are_plausible():
    by_region: dict[str, int] = {}
    for code, _ in countries():
        by_region[region_for(code)] = by_region.get(region_for(code), 0) + 1
    assert by_region["us"] >= 45 and by_region["eu"] >= 90 and by_region["sea"] >= 60 and by_region["cn"] == 1


def test_gateway_urls():
    assert gateway_url("us") == "https://gw-us.xiaoyi.com"
    assert gateway_url("eu") == "https://gw-eu.xiaoyi.com"
    assert gateway_url("sea") == "https://gw-sg.xiaoyi.com"
    assert gateway_url("cn") == "https://api.xiaoyi.com"
    with pytest.raises(ValueError):
        gateway_url("mars")


# ====================================================================== app key
def test_app_key_env_wins_over_file(monkeypatch, tmp_path):
    (tmp_path / "home").mkdir()
    (tmp_path / "home" / "kami_app_key.txt").write_text("file-key\n", encoding="utf-8")
    assert app_key_mod.load_app_key() == "file-key"
    monkeypatch.setenv("TAPO_KAMI_APP_KEY", "  env-key ")
    assert app_key_mod.load_app_key() == "env-key"


def test_app_key_missing_or_empty_is_none(monkeypatch, tmp_path):
    assert app_key_mod.load_app_key() is None
    (tmp_path / "home").mkdir()
    (tmp_path / "home" / "kami_app_key.txt").write_text("  \n", encoding="utf-8")
    assert app_key_mod.load_app_key() is None
    monkeypatch.setenv("TAPO_KAMI_APP_KEY", "   ")
    assert app_key_mod.load_app_key() is None


@pytest.mark.skipif(not KEY_FILE.exists(), reason="the real key file F:\\kami_re\\kami_app_key.txt is not present")
def test_real_vendor_key_is_in_no_tracked_file():
    key = KEY_FILE.read_text(encoding="utf-8").strip()
    assert len(key) >= 16
    tracked = subprocess.run(["git", "ls-files", "-z"], cwd=REPO, capture_output=True, check=True).stdout
    needle = key.encode("utf-8")
    hits = []
    for rel in filter(None, tracked.split(b"\0")):
        path = REPO / rel.decode("utf-8")
        if path.is_file() and needle in path.read_bytes():
            hits.append(rel.decode("utf-8"))
    assert hits == []


# ====================================================================== DTOs
def test_parse_devices_fields_shared_offline_and_skips_entries_without_uid():
    reply = {"code": 20000, "data": [
        make_device("UID-A", "Hall", model="7", online=True),
        make_device("UID-B", "Garden", online=False),
        make_device("UID-C", "Friend's cam", share=True),
        {"name": "no uid"},
        make_device("UID-D", ""),
        "not a dict",
    ]}
    devs = parse_devices(reply, "eu")
    assert [d.uid for d in devs] == ["UID-A", "UID-B", "UID-C", "UID-D"]
    a, b, c, d = devs
    assert (a.name, a.model, a.online, a.shared, a.region) == ("Hall", "7", True, False, "eu")
    assert b.online is False
    assert c.shared is True
    assert d.name == "UID-D"  # no name: the uid is shown


def test_parse_devices_empty_and_bad_shapes():
    assert parse_devices({"code": 20000, "data": []}, "us") == []
    for bad in ({"code": 20000}, {"code": 20000, "data": {"x": 1}}, {"code": 20000, "data": "x"}):
        with pytest.raises(KamiProtocolError):
            parse_devices(bad, "us")


def test_parse_devices_result_codes():
    with pytest.raises(KamiSessionExpired):
        parse_devices({"code": SESSION_INVALID_CODE}, "us")
    with pytest.raises(KamiServerError):
        parse_devices({"code": 50000}, "us")


def test_parse_login_and_session_repr_hides_secrets():
    reply = {"code": 20000, "data": {"userid": "u-9", "token": "TOKEN-VALUE", "token_secret": "SECRET-VALUE",
                                     "name": "Owner A"}}
    session = parse_login(reply, region="eu", country="DE", email="owner@example.com")
    assert (session.userid, session.token, session.token_secret) == ("u-9", "TOKEN-VALUE", "SECRET-VALUE")
    assert "TOKEN-VALUE" not in repr(session) and "SECRET-VALUE" not in repr(session)
    assert KamiSession.from_json(session.to_json()) == session


@pytest.mark.parametrize("data", [None, {}, {"userid": "u"}, {"userid": "u", "token": "t"},
                                  {"token": "t", "token_secret": "s"}])
def test_parse_login_missing_fields_is_protocol_error(data):
    reply = {"code": 20000}
    if data is not None:
        reply["data"] = data
    with pytest.raises(KamiProtocolError):
        parse_login(reply, region="us", country="US", email="a@example.com")


def test_parse_login_error_codes():
    with pytest.raises(KamiAuthError) as exc:
        parse_login({"code": REJECT_CODE}, region="us", country="US", email="a@example.com")
    assert exc.value.code == REJECT_CODE and not isinstance(exc.value, KamiExtraStepError)
    with pytest.raises(KamiExtraStepError):
        parse_login({"code": EXTRA_STEP_CODE}, region="us", country="US", email="a@example.com")
    with pytest.raises(KamiExtraStepError):
        parse_login({"code": 20281}, region="us", country="US", email="a@example.com")


def test_from_json_rejects_garbage():
    for text in ("", "not json", "[]", json.dumps({"userid": "u"})):
        with pytest.raises(KamiProtocolError):
            KamiSession.from_json(text)


# ====================================================================== client against the mock
def make_client(mock, region="eu", country="DE", key=MOCK_APP_KEY):
    return KamiClient(region, country, app_key=key, base_url=mock.url, timeout=5.0)


def test_client_login_and_list(mock_kami):
    mock_kami.devices = [make_device("UID-A", "Hall"), make_device("UID-B", "Garden", online=False)]
    client = make_client(mock_kami)
    session = client.login(EMAIL, PASSWORD)
    assert session.userid == "u-100" and session.token and session.token_secret
    devices = client.list_devices(session)
    assert [(d.uid, d.online) for d in devices] == [("UID-A", True), ("UID-B", False)]
    assert (mock_kami.login_count, mock_kami.list_count) == (1, 1)


def test_client_login_request_shape(mock_kami):
    make_client(mock_kami, country="DE").login(EMAIL, PASSWORD)
    req = mock_kami.requests[0]
    assert req.path == "/v4/users/login"
    assert req.params["account"] == EMAIL and req.params["seq"] == "1"
    assert req.params["password"] == password_hash(PASSWORD, MOCK_APP_KEY) != PASSWORD
    assert {"dev_name", "dev_type", "dev_os_version"} <= set(req.params)
    h = req.headers
    assert h["x-kamihome-apptype"] == "ANDROID" and h["x-kamihome-packagetype"] == "RELEASE"
    assert h["x-xiaoyi-appcountrycode"] == "DE" and h["x-xiaoyi-appversion"]
    assert h["user-agent"] and "python" not in h["user-agent"].lower()


def test_client_china_login_uses_email_param(mock_kami):
    make_client(mock_kami, region="cn", country="CN").login(EMAIL, PASSWORD)
    params = mock_kami.requests[0].params
    assert params["email"] == EMAIL and "account" not in params


def test_client_list_request_is_signed_in_order(mock_kami):
    client = make_client(mock_kami)
    session = client.login(EMAIL, PASSWORD)
    client.list_devices(session)
    req = mock_kami.requests[1]
    assert [k for k, _ in req.pairs] == ["seq", "userid", "hmac"]
    assert req.params["hmac"] == request_hmac([("seq", "1"), ("userid", session.userid)], session.token,
                                              session.token_secret)


def test_client_wrong_password_one_request(mock_kami):
    with pytest.raises(KamiAuthError) as exc:
        make_client(mock_kami).login(EMAIL, "wrong")
    assert exc.value.code == REJECT_CODE and mock_kami.login_count == 1


def test_client_unknown_account_and_wrong_region_look_the_same(mock_kami):
    with pytest.raises(KamiAuthError):
        make_client(mock_kami).login("nobody@example.com", PASSWORD)


def test_client_social_only_account_gets_extra_step_error(mock_kami):
    mock_kami.add_account("social@example.com", "x", extra_step_code=EXTRA_STEP_CODE)
    with pytest.raises(KamiExtraStepError):
        make_client(mock_kami).login("social@example.com", "x")


def test_client_http_500_and_malformed_json(mock_kami):
    mock_kami.http_status["/v4/users/login"] = 500
    with pytest.raises(KamiServerError):
        make_client(mock_kami).login(EMAIL, PASSWORD)
    mock_kami.http_status.clear()
    mock_kami.raw_body["/v4/users/login"] = b"{not json"
    with pytest.raises(KamiProtocolError):
        make_client(mock_kami).login(EMAIL, PASSWORD)
    mock_kami.raw_body["/v4/users/login"] = b"[1, 2]"
    with pytest.raises(KamiProtocolError):
        make_client(mock_kami).login(EMAIL, PASSWORD)
    mock_kami.raw_body["/v4/users/login"] = b""
    with pytest.raises(KamiProtocolError):
        make_client(mock_kami).login(EMAIL, PASSWORD)


def test_client_unreachable_server_is_connection_error(mock_kami):
    client = make_client(mock_kami)
    mock_kami.stop()
    with pytest.raises(KamiConnectionError) as exc:
        client.login(EMAIL, PASSWORD)
    assert isinstance(exc.value, TapoConnectionError)
    assert "127.0.0.1" not in str(exc.value) and "/v4/" not in str(exc.value)


def test_client_without_key_raises_before_any_request(mock_kami):
    with pytest.raises(KamiKeyMissingError):
        make_client(mock_kami, key=None).login(EMAIL, PASSWORD)
    assert mock_kami.requests == []


def test_client_stale_token_is_session_expired(mock_kami):
    client = make_client(mock_kami)
    session = client.login(EMAIL, PASSWORD)
    mock_kami.expire_sessions()
    with pytest.raises(KamiSessionExpired):
        client.list_devices(session)
    mock_kami.http_status["/v5/devices/list"] = 401
    with pytest.raises(KamiSessionExpired):
        client.list_devices(session)


# ====================================================================== manager: sign-in
def test_sign_in_creates_kami_cameras_with_online_flags(kami_env, qtbot):
    env = kami_env
    env.mock.devices = [make_device("UID-A", "Hall", model="7"), make_device("UID-B", "Garden", online=False),
                        make_device("UID-C", "Friend's cam", share=True)]
    assert not env.manager.is_signed_in()
    click(env, qtbot)
    assert env.signals["failed"] == []
    assert env.signals["signed_in"] == ["o***@example.com"]
    assert env.manager.is_signed_in() and env.manager.account_label() == "o***@example.com"
    cams = {c.id: c for c in env.controller.cameras()}
    assert set(cams) == {"kami:UID-A", "kami:UID-B", "kami:UID-C"}
    a, b, c = cams["kami:UID-A"], cams["kami:UID-B"], cams["kami:UID-C"]
    assert (a.source, a.host, a.lan_found, a.name, a.model, a.device_id) == ("kami", "", False, "Hall", "7", "UID-A")
    assert (a.online, b.online, c.online) == (True, False, True)
    assert a.vendor_data["uid"] == "UID-A" and a.vendor_data["region"] == "eu" and a.vendor_data["model_code"] == "7"
    assert a.vendor_data["shared"] is False and c.vendor_data["shared"] is True
    assert env.signals["changed"] >= 1
    assert env.factory_calls == [("eu", "DE")]


def test_sign_in_stores_token_in_vault_not_password_and_nothing_secret_in_config(kami_env, qtbot):
    env = kami_env
    env.mock.devices = [make_device("UID-A", "Hall")]
    click(env, qtbot)
    entry = env.vault.get(KAMI_ACCOUNT_VAULT_ID, CredentialKind.KAMI_ACCOUNT)
    assert entry is not None and entry.username == EMAIL
    blob = json.loads(entry.password)
    token, secret = env.mock.last_session()
    assert (blob["token"], blob["token_secret"], blob["userid"]) == (token, secret, "u-100")
    assert PASSWORD not in entry.password
    text = env.store.path.read_text(encoding="utf-8")
    for secret_text in (PASSWORD, token, secret, "ENCRYPTED-CAMERA-PASSWORD", EMAIL, "u-100", "Home WiFi A",
                        "192.168.1.50", "AA:BB:CC:00:00:01"):
        assert secret_text not in text
    assert env.store.get_setting(SETTING_KAMI_ACCOUNT, None)["label"] == "o***@example.com"
    assert env.store.get_setting(SETTING_KAMI_LAST_SIGN_IN_FAILED, False) is False
    # no TP-Link style credentials were created for the Kami camera
    assert env.vault.get("kami:UID-A", CredentialKind.CLOUD) is None
    assert env.vault.get(ACCOUNT_VAULT_ID, CredentialKind.CLOUD) is None


def test_sign_in_with_empty_camera_list(kami_env, qtbot):
    click(kami_env, qtbot)
    assert kami_env.manager.is_signed_in() and kami_env.kami_ids() == []
    assert kami_env.signals["signed_in"] == ["o***@example.com"]


def test_sign_in_keeps_tapo_cameras_and_orders_them_first(kami_env, qtbot):
    env = kami_env
    env.controller.upsert_cameras([make_config(1, source="cloud", device_id="d1")], cloud=Credentials("admin", "p"))
    env.mock.devices = [make_device("UID-A", "Hall")]
    click(env, qtbot)
    assert env.ids() == ["cam1", "kami:UID-A"]


def test_sign_in_wrong_region_gives_one_request_and_no_cameras(kami_env, qtbot):
    env = kami_env
    other = MockKamiCloud().start()  # the "us" gateway knows nobody
    try:
        rebuild(env, routes={"us": other})
        click(env, qtbot, country="US")
        assert other.login_count == 1 and env.mock.requests == []
        assert env.signals["failed"] and not env.manager.is_signed_in() and env.kami_ids() == []
        assert env.factory_calls[-1] == ("us", "US")
    finally:
        other.stop()


def test_sign_in_social_only_account_explains_and_does_not_block(kami_env, qtbot):
    env = kami_env
    env.mock.add_account("social@example.com", "x", extra_step_code=EXTRA_STEP_CODE)
    click(env, qtbot, email="social@example.com", password="x")
    assert env.mock.login_count == 1 and not env.manager.is_signed_in()
    (message,) = env.signals["failed"]
    assert "Kami app" in message and "password" in message.lower()
    assert env.manager.blocked_until() is None  # not a wrong password: no 120 s block
    assert env.store.get_setting(SETTING_KAMI_LAST_SIGN_IN_FAILED, False) is True


def test_sign_in_server_error_and_malformed_json(kami_env, qtbot):
    env = kami_env
    env.mock.http_status["/v4/users/login"] = 500
    click(env, qtbot)
    assert env.mock.login_count == 1 and len(env.signals["failed"]) == 1 and not env.manager.is_signed_in()
    assert env.manager.blocked_until() is None
    env.mock.http_status.clear()
    env.mock.raw_body["/v4/users/login"] = b"{broken"
    click(env, qtbot)
    assert env.mock.login_count == 2 and len(env.signals["failed"]) == 2
    assert env.store.get_setting(SETTING_KAMI_LAST_SIGN_IN_FAILED, False) is True


def test_sign_in_server_unreachable(kami_env, qtbot):
    env = kami_env
    env.mock.stop()
    click(env, qtbot)
    (message,) = env.signals["failed"]
    assert "reach" in message.lower() and len(env.factory_calls) == 1
    assert env.manager.blocked_until() is None


def test_sign_in_missing_app_key_fails_with_clear_message_and_no_network(kami_env, qtbot, monkeypatch):
    env = kami_env
    rebuild(env, default_factory=True)  # real factory: reads TAPO_KAMI_APP_KEY / kami_app_key.txt (none here)
    click(env, qtbot)
    (message,) = env.signals["failed"]
    assert "kami_app_key.txt" in message and "TAPO_KAMI_APP_KEY" in message
    assert env.mock.requests == [] and not env.manager.is_signed_in()
    assert env.manager.blocked_until() is None


def test_sign_in_rejects_empty_input_and_unknown_country_without_a_request(kami_env, qtbot):
    env = kami_env
    click(env, qtbot, email="  ")
    click(env, qtbot, password="")
    click(env, qtbot, country="ZZ")
    assert len(env.signals["failed"]) == 3 and env.mock.requests == [] and env.factory_calls == []


# ====================================================================== manager: breaker
def test_wrong_password_blocks_for_120_seconds_and_blocked_click_sends_nothing(kami_env, qtbot):
    env = kami_env
    clock = fake_clock()
    rebuild(env, clock=clock)
    click(env, qtbot, password="wrong")
    assert env.mock.login_count == 1 and len(env.signals["failed"]) == 1
    assert env.manager.blocked_until() == pytest.approx(clock() + 120)
    assert env.store.get_setting(SETTING_KAMI_LAST_SIGN_IN_FAILED, False) is True
    assert env.store.get_setting(SETTING_KAMI_BLOCKED_UNTIL, None) == pytest.approx(clock() + 120)
    clock.state["t"] += 60
    click(env, qtbot)  # even the right password: blocked
    assert env.mock.login_count == 1 and len(env.signals["failed"]) == 2
    assert "60" in env.signals["failed"][1] or "minute" in env.signals["failed"][1].lower()
    clock.state["t"] += 61
    assert env.manager.blocked_until() is None
    click(env, qtbot)
    assert env.mock.login_count == 2 and env.manager.is_signed_in()
    assert env.store.get_setting(SETTING_KAMI_LAST_SIGN_IN_FAILED, False) is False
    assert env.store.get_setting(SETTING_KAMI_BLOCKED_UNTIL, None) is None


def test_breaker_survives_a_restart(kami_env, qtbot):
    env = kami_env
    clock = fake_clock()
    rebuild(env, clock=clock)
    click(env, qtbot, password="wrong")
    until = env.manager.blocked_until()
    clock.state["t"] += 10
    rebuild(env, clock=clock)
    assert env.manager.blocked_until() == until
    click(env, qtbot)
    assert env.mock.login_count == 1 and env.signals["failed"]


def test_second_click_while_one_is_running_sends_no_second_request(kami_env, qtbot):
    env = kami_env
    env.mock.login_delay = 0.4
    env.manager.sign_in(EMAIL, PASSWORD, "DE")
    env.manager.sign_in(EMAIL, PASSWORD, "DE")
    env.idle(qtbot)
    assert env.mock.login_count == 1
    assert len(env.signals["failed"]) == 1 and env.manager.is_signed_in()


# ====================================================================== manager: restore / refresh / sign-out
def test_restore_session_uses_the_stored_token_and_never_sends_a_password(kami_env, qtbot):
    env = kami_env
    env.mock.devices = [make_device("UID-A", "Hall")]
    click(env, qtbot)
    assert env.mock.login_count == 1
    env.mock.devices = [make_device("UID-A", "Hall"), make_device("UID-N", "New")]
    manager = rebuild(env)
    assert not manager.is_signed_in()
    manager.restore_session()
    env.idle(qtbot)
    assert env.mock.login_count == 1 and env.mock.list_count == 2
    assert manager.is_signed_in() and env.signals["signed_in"] == ["o***@example.com"]
    assert env.kami_ids() == ["kami:UID-A", "kami:UID-N"]


def test_restore_session_without_a_stored_session_does_nothing(kami_env, qtbot):
    kami_env.manager.restore_session()
    kami_env.idle(qtbot)
    assert kami_env.mock.requests == [] and kami_env.signals["signed_in"] == [] and kami_env.signals["failed"] == []


def test_restore_session_is_skipped_after_a_failed_sign_in(kami_env, qtbot):
    env = kami_env
    click(env, qtbot)
    env.store.set_setting(SETTING_KAMI_LAST_SIGN_IN_FAILED, True)  # a later manual attempt failed
    count = len(env.mock.requests)
    manager = rebuild(env)
    manager.restore_session()
    env.idle(qtbot)
    assert len(env.mock.requests) == count and not manager.is_signed_in()
    assert env.signals["signed_in"] == [] and env.signals["failed"] == []


def test_restore_with_an_expired_token_asks_for_a_manual_sign_in_and_sends_no_password(kami_env, qtbot):
    env = kami_env
    env.mock.devices = [make_device("UID-A", "Hall")]
    click(env, qtbot)
    env.mock.expire_sessions()
    manager = rebuild(env)
    manager.restore_session()
    env.idle(qtbot)
    assert env.mock.login_count == 1  # no silent re-login
    assert len(env.signals["failed"]) == 1 and not manager.is_signed_in()
    assert env.store.get_setting(SETTING_KAMI_LAST_SIGN_IN_FAILED, False) is True
    assert env.vault.get(KAMI_ACCOUNT_VAULT_ID, CredentialKind.KAMI_ACCOUNT) is None  # dead token dropped
    # a second restore (next start) now does nothing at all
    count = len(env.mock.requests)
    manager = rebuild(env)
    manager.restore_session()
    env.idle(qtbot)
    assert len(env.mock.requests) == count


def test_restore_while_offline_keeps_the_cached_cameras(kami_env, qtbot):
    env = kami_env
    env.mock.devices = [make_device("UID-A", "Hall")]
    click(env, qtbot)
    env.mock.stop()
    manager = rebuild(env)
    manager.restore_session()
    env.idle(qtbot)
    assert manager.is_signed_in() and env.kami_ids() == ["kami:UID-A"]
    assert env.signals["signed_in"] == ["o***@example.com"] and env.signals["failed"] == []
    assert env.store.get_setting(SETTING_KAMI_LAST_SIGN_IN_FAILED, False) is False


def test_refresh_adds_removes_updates_and_keeps_a_local_rename(kami_env, qtbot):
    env = kami_env
    env.mock.devices = [make_device("UID-A", "Hall"), make_device("UID-B", "Garden")]
    click(env, qtbot)
    env.controller.rename_camera("kami:UID-A", "My hall")  # local name (Kami cannot rename yet)
    env.mock.devices = [make_device("UID-A", "Hall (server)", online=False), make_device("UID-N", "New cam"),
                        make_device("UID-S", "Shared", share=True)]
    before = env.signals["changed"]
    env.manager.refresh()
    env.idle(qtbot)
    cams = {c.id: c for c in env.controller.cameras()}
    assert set(cams) == {"kami:UID-A", "kami:UID-N", "kami:UID-S"}
    assert cams["kami:UID-A"].name == "My hall" and cams["kami:UID-A"].online is False
    assert cams["kami:UID-S"].vendor_data["shared"] is True
    assert env.signals["changed"] > before
    # a server-side rename shows up while no local rename exists
    env.mock.devices = [make_device("UID-N", "New cam v2")]
    env.manager.refresh()
    env.idle(qtbot)
    assert env.controller.camera("kami:UID-N").name == "New cam v2"


def test_refresh_when_signed_out_does_nothing(kami_env, qtbot):
    kami_env.manager.refresh()
    kami_env.idle(qtbot)
    assert kami_env.mock.requests == [] and kami_env.signals["failed"] == []


def test_refresh_with_expired_token_reports_failure_and_never_logs_in_again(kami_env, qtbot):
    env = kami_env
    env.mock.devices = [make_device("UID-A", "Hall")]
    click(env, qtbot)
    env.mock.expire_sessions()
    env.manager.refresh()
    env.idle(qtbot)
    assert env.mock.login_count == 1 and len(env.signals["failed"]) == 1
    assert not env.manager.is_signed_in() and env.kami_ids() == ["kami:UID-A"]


def test_refresh_when_the_server_is_down_keeps_everything(kami_env, qtbot):
    env = kami_env
    env.mock.devices = [make_device("UID-A", "Hall")]
    click(env, qtbot)
    env.mock.stop()
    env.manager.refresh()
    env.idle(qtbot)
    assert env.manager.is_signed_in() and env.kami_ids() == ["kami:UID-A"] and env.signals["failed"] == []


def test_sign_out_removes_only_kami_state_and_leaves_tapo_untouched(kami_env, qtbot):
    env = kami_env
    tapo_cloud = make_config(1, source="cloud", device_id="d1")
    manual = make_config(2)
    env.controller.upsert_cameras([tapo_cloud], cloud=Credentials("admin", "tapo-pass"))
    env.controller.upsert_cameras([manual])
    env.vault.set(ACCOUNT_VAULT_ID, CredentialKind.CLOUD, Credentials("tapo@example.com", "tapo-account-pass"))
    env.vault.set("cam2", CredentialKind.CAMERA_ACCOUNT, Credentials("u", "p"))
    env.store.set_setting("account", {"email": "tapo@example.com"})
    tapo_before = [(c.id, c.name, c.host, c.source) for c in env.controller.cameras()]
    vault_before = {cid: env.vault.get(cid, CredentialKind.CLOUD) for cid in ("cam1", ACCOUNT_VAULT_ID)}

    env.mock.devices = [make_device("UID-A", "Hall")]
    click(env, qtbot)
    assert env.kami_ids() == ["kami:UID-A"]
    env.manager.sign_out()
    assert env.signals["signed_out"] == [True]
    assert not env.manager.is_signed_in() and env.manager.account_label() == ""
    assert env.kami_ids() == []
    assert [(c.id, c.name, c.host, c.source) for c in env.controller.cameras()] == tapo_before
    assert {cid: env.vault.get(cid, CredentialKind.CLOUD) for cid in ("cam1", ACCOUNT_VAULT_ID)} == vault_before
    assert env.vault.get("cam2", CredentialKind.CAMERA_ACCOUNT) == Credentials("u", "p")
    assert env.vault.get(KAMI_ACCOUNT_VAULT_ID, CredentialKind.KAMI_ACCOUNT) is None
    assert env.vault.camera_ids().count("kami:UID-A") == 0
    assert env.store.get_setting("account", None) == {"email": "tapo@example.com"}
    assert env.store.get_setting(SETTING_KAMI_ACCOUNT, None) is None
    assert [c.id for c in env.store.load()] == ["cam1", "cam2"]


def test_sign_out_keeps_the_breaker(kami_env, qtbot):
    env = kami_env
    rebuild(env, clock=fake_clock())
    click(env, qtbot, password="wrong")
    until = env.manager.blocked_until()
    env.manager.sign_out()
    assert env.manager.blocked_until() == until


def test_late_result_after_sign_out_cannot_sign_back_in(kami_env, qtbot):
    env = kami_env
    env.mock.login_delay = 0.4
    env.mock.devices = [make_device("UID-A", "Hall")]
    env.manager.sign_in(EMAIL, PASSWORD, "DE")
    env.manager.sign_out()
    env.idle(qtbot)
    assert not env.manager.is_signed_in() and env.kami_ids() == []
    assert env.vault.get(KAMI_ACCOUNT_VAULT_ID, CredentialKind.KAMI_ACCOUNT) is None


# ====================================================================== safety: logs and files
def test_logs_contain_no_url_password_token_or_camera_password(kami_env, qtbot, caplog):
    env = kami_env
    caplog.set_level(logging.DEBUG)
    env.mock.devices = [make_device("UID-A", "Hall")]
    click(env, qtbot)
    token, secret = env.mock.last_session()
    env.manager.refresh()
    env.idle(qtbot)
    click(env, qtbot, password="wrong-password-xyz")  # already signed in again: still must not leak
    env.mock.http_status["/v5/devices/list"] = 500
    env.manager.refresh()
    env.idle(qtbot)
    env.manager.sign_out()
    click(env, qtbot, password="wrong-password-xyz")
    forbidden = [PASSWORD, "wrong-password-xyz", token, secret, MOCK_APP_KEY, "ENCRYPTED-CAMERA-PASSWORD",
                 env.mock.url, "127.0.0.1", "/v4/users/login", "/v5/devices/list", "password=", "hmac=",
                 password_hash(PASSWORD, MOCK_APP_KEY), EMAIL]
    text = caplog.text
    for item in forbidden:
        assert item not in text, f"log leaks {item[:8]!r}"
    assert caplog.records  # the capture really ran


def test_kami_lane_imports_no_tapo_network_code():
    import ast

    banned = ("tapo_desktop.core.control_client", "tapo_desktop.core.cloud", "tapo_desktop.core.media_engine",
              "tapo_desktop.core.vod", "tapo_desktop.core.discovery", "tapo_desktop.services.account")
    files = list((REPO / "src" / "tapo_desktop" / "core" / "kami").glob("*.py")) + [
        REPO / "src" / "tapo_desktop" / "services" / "kami_account.py"]
    for path in files:
        tree = ast.parse(path.read_text(encoding="utf-8"))
        for node in ast.walk(tree):
            names = ([a.name for a in node.names] if isinstance(node, ast.Import)
                     else [node.module or ""] if isinstance(node, ast.ImportFrom) else [])
            for name in names:
                assert not name.startswith(banned), f"{path.name} imports {name}"


def test_source_files_are_utf8_lf_without_bom():
    paths = list((REPO / "src" / "tapo_desktop" / "core" / "kami").glob("*.py")) + [
        REPO / "src" / "tapo_desktop" / "services" / "kami_account.py", REPO / "tools" / "diag_kami_list.py",
        REPO / "tests" / "fixtures" / "mock_kami_cloud.py", pathlib.Path(__file__)]
    for path in paths:
        raw = path.read_bytes()
        assert not raw.startswith(b"\xef\xbb\xbf") and b"\r\n" not in raw, path.name
        raw.decode("utf-8")


# ====================================================================== diag tool
def test_diag_tool_refuses_without_the_owner_flag():
    result = subprocess.run([sys.executable, str(REPO / "tools" / "diag_kami_list.py")], capture_output=True,
                            text=True, timeout=60, cwd=REPO, env={**os.environ, "PYTHONPATH": str(REPO / "src")})
    assert result.returncode != 0
    assert "--i-am-the-owner" in result.stdout + result.stderr


def test_diag_tool_has_no_password_argument():
    text = (REPO / "tools" / "diag_kami_list.py").read_text(encoding="utf-8")
    assert "--password" not in text and "getpass" in text
