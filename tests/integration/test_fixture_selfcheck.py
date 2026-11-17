"""Self-check of the mock camera's HTTPS control surface + lifecycle (raw clients only).

Independent of ``tapo_desktop``: clients are rebuilt from the spec in tests/fixtures/raw_clients.py.
"""
from __future__ import annotations

import json
import socket
import threading
import time
from datetime import datetime, timedelta, timezone

import pytest

from tests.fixtures.mock_tapo_server import DeviceShadow, MockTapoCamera
from tests.fixtures.raw_clients import LegacyClient, SecureClient, cj, sha256u

pytestmark = pytest.mark.integration

GOLDEN = {
    "password": "test_password", "cnonce": "ABCDEF0123456789", "nonce": "0123456789ABCDEF", "seq": 1234,
    "device_confirm": "AE0E012599836B112471EC21767309CF4A927A9053B8A9FA7917E8AB28F09D010123456789ABCDEFABCDEF0123456789",
    "digest_passwd": "5A379DADD2ED54B76D3308115F0520E539295A1EF5C5149DBE269EB7C23ACE5EABCDEF01234567890123456789ABCDEF",
    "outer": ('{"method":"securePassthrough","params":{"request":"tsPetnY0BwryJNwfMIFkg6TmS7K69/hlPfr/0l4P3q/Ir3YMNXX0fEE1'
              'ASXzOjcW0qoTq3WZqTSc9MSNF2kA5hJJqD5UUkNw54G4WGEg/d0+IDljn04yvBwE8MLzstIcebwChQMmXfM4jJ1hTf6Gj920E5ML9J10B5'
              'ThkTJqhfG+WKfkRzTHXtMuNpHkFjOd"}}'),
    "tag": "1E3CCDD1AA638ACBF781C0BE09D3510F270E61429E7E86FC56F76B636F2C6A38",
}


def _client(cam: MockTapoCamera, **kw) -> SecureClient:
    return SecureClient(cam.host, cam.https_port, cam.cloud_password, **kw)


def _ok(item: dict) -> dict:
    assert item["error_code"] == 0, item
    return item["result"]


# ------------------------------------------------------------------------------------------
# fixtures / lifecycle
# ------------------------------------------------------------------------------------------
def test_fixtures_expose_contract_api(mock_camera, legacy_mock_camera, dual_lens_camera, tmp_data_dir):
    import os

    assert mock_camera.secure and not legacy_mock_camera.secure and dual_lens_camera.dual_lens
    ports = {mock_camera.https_port, mock_camera.rtsp_port, mock_camera.onvif_port, mock_camera.media_port}
    assert len(ports) == 4 and all(p > 0 for p in ports)
    assert mock_camera.host == "127.0.0.1"
    assert isinstance(mock_camera.state, DeviceShadow)
    assert mock_camera.rtsp_url() == f"rtsp://camuser:campass@127.0.0.1:{mock_camera.rtsp_port}/stream2"
    assert mock_camera.rtsp_url(quality="stream1").endswith("/stream1")
    assert len(mock_camera.cert_fingerprint) == 64 and mock_camera.cert_fingerprint == mock_camera.cert_fingerprint.lower()
    assert os.environ["TAPO_DESKTOP_HOME"] == str(tmp_data_dir) and tmp_data_dir.is_dir()
    s = mock_camera.state
    today = datetime.now(timezone.utc).date()
    days = [c.start.date() for c in s.recordings]
    assert days.count(today) == 3 and days.count(today - timedelta(days=1)) == 1
    assert all(c.start.tzinfo is not None for c in s.recordings)
    for name in ("privacy", "led", "inf_type", "night_vision_mode", "motion_enabled", "motion_sensitivity",
                 "alarm_active", "ptz_x", "ptz_y", "presets", "components"):
        assert hasattr(s, name)


def test_clean_shutdown_leaves_no_threads_or_listeners():
    before = {t.ident for t in threading.enumerate()}
    cam = MockTapoCamera()
    with cam:
        ports = [cam.https_port, cam.rtsp_port, cam.onvif_port, cam.media_port]
        c = _client(cam)
        c.login()
        c.multi({"method": "getLedStatus", "params": {"led": {"name": ["config"]}}})  # keep-alive conn open
        held = socket.create_connection((cam.host, cam.rtsp_port))
    t0 = time.monotonic()
    deadline = t0 + 5
    while time.monotonic() < deadline:
        extra = [t for t in threading.enumerate() if t.ident not in before and t.name.startswith("mock-")]
        if not extra:
            break
        time.sleep(0.05)
    assert not extra, [t.name for t in extra]
    import psutil

    conns = psutil.Process().net_connections(kind="tcp")
    leaked = [c for c in conns if c.laddr and c.laddr.port in ports and c.status in ("LISTEN", "ESTABLISHED")]
    assert not leaked, leaked
    held.close()
    c.close()
    cam.stop()  # idempotent


# ------------------------------------------------------------------------------------------
# secure login [spec:auth.secure.*] + golden vector [spec:auth.golden_vector.v3]
# ------------------------------------------------------------------------------------------
def test_golden_vector_client_is_accepted():
    cam = MockTapoCamera(cloud_password=GOLDEN["password"])
    cam.fixed_nonce, cam.fixed_start_seq = GOLDEN["nonce"], GOLDEN["seq"]
    with cam:
        c = SecureClient(cam.host, cam.https_port, GOLDEN["password"], cnonce=GOLDEN["cnonce"])
        r1 = c.step1()
        assert r1["error_code"] == -40413
        assert r1["result"]["data"]["nonce"] == GOLDEN["nonce"]
        assert r1["result"]["data"]["device_confirm"] == GOLDEN["device_confirm"]
        _, r2 = c.post("/", cj({"method": "login", "params": {"cnonce": GOLDEN["cnonce"], "digest_passwd": GOLDEN["digest_passwd"],
                                                              "encrypt_type": "3", "username": "admin"}}))
        assert r2["error_code"] == 0 and r2["result"]["start_seq"] == GOLDEN["seq"]
        c.nonce, c.stok, c.seq = GOLDEN["nonce"], r2["result"]["stok"], GOLDEN["seq"]
        import hashlib

        fh = sha256u(c.cnonce + sha256u(c.password) + c.nonce)
        c.lsk = hashlib.sha256(("lsk" + c.cnonce + c.nonce + fh).encode()).digest()[:16]
        c.ivb = hashlib.sha256(("ivb" + c.cnonce + c.nonce + fh).encode()).digest()[:16]
        assert c.lsk.hex() == "018fb84dcd026fb84e766fa42627c6df" and c.ivb.hex() == "0681a79416d607616bc1572710f41f6d"
        status, raw = c.post(f"/stok={c.stok}/ds", GOLDEN["outer"], {"seq": "1234", "tapo_tag": GOLDEN["tag"]})
        assert status == 200 and raw["error_code"] == 0
        reply = c.send({}, seq=1235)  # next seq works too (proves seq advanced to 1235)
        assert reply.get("error_code") == -40106  # {} has no method -> inner -40106; outer auth passed
        import base64

        from tests.fixtures.raw_clients import aes_dec

        inner = json.loads(aes_dec(c.lsk, c.ivb, base64.b64decode(raw["result"]["response"])))
        item = inner["result"]["responses"][0]
        assert item["method"] == "getDeviceInfo" and item["error_code"] == 0
        assert item["result"]["device_info"]["basic_info"]["device_model"] == "C210"
        assert cam.request_log[0] == {"method": "getDeviceInfo", "params": {"device_info": {"name": ["basic_info"]}}}
        assert cam.login_count == 1


def test_secure_login_rejects_wrong_password_and_bad_cnonce(mock_camera):
    c = _client(mock_camera)
    r1 = c.step1()
    nonce = r1["result"]["data"]["nonce"]
    # device_confirm proves the camera knows the cloud password; a wrong local guess does not match
    dc = r1["result"]["data"]["device_confirm"]
    assert dc.endswith(nonce + c.cnonce)
    assert dc.startswith(sha256u(c.cnonce + sha256u("cloudpass") + nonce))
    assert not dc.startswith(sha256u(c.cnonce + sha256u("wrong") + nonce))
    r2 = c.step2(nonce, password="wrong")
    assert r2["error_code"] == -40401
    assert mock_camera.login_count == 0
    # nonce consumed: a replayed step 2 gets a fresh challenge
    assert c.step2(nonce)["error_code"] == -40413
    _, bad = c.post("/", cj({"method": "login", "params": {"cnonce": "xyz", "encrypt_type": "3", "username": "admin"}}))
    assert bad["error_code"] == -40210
    c.login()
    assert mock_camera.login_count == 1


def test_tag_seq_and_stok_are_enforced(mock_camera):
    c = _client(mock_camera)
    c.login()
    req = {"method": "multipleRequest", "params": {"requests": [{"method": "getLedStatus", "params": {"led": {"name": ["config"]}}}]}}
    s0 = c.seq
    assert c.send(req)["error_code"] == 0
    assert c.send(req, tag="0" * 64)["error_code"] == -40401              # bad tag
    assert c.send(req, seq=s0)["error_code"] == -40401                    # replayed seq
    assert c.send(req, seq=c.seq + 5)["error_code"] == 0                  # gaps are fine (lost requests)
    c.seq += 6
    assert c.send(req, body=cj({"method": "securePassthrough", "params": {"request": "!!notb64"}}))["error_code"] == -40210
    status, r = c.post(f"/stok={c.stok}/ds", c.outer_body(req), {})        # no seq / tapo_tag headers
    assert status == 200 and r["error_code"] == -40401
    status, r = c.post("/stok=deadbeef/ds", c.outer_body(req), {"seq": str(c.seq), "tapo_tag": "x"})
    assert r["error_code"] == -40401
    assert c.send(req)["error_code"] == 0


def test_expire_sessions_and_token_expiry_injection(mock_camera):
    c = _client(mock_camera)
    c.login()
    led = {"method": "getLedStatus", "params": {"led": {"name": ["config"]}}}
    c.multi(led)
    mock_camera.expire_sessions()
    assert c.send({"method": "multipleRequest", "params": {"requests": [led]}})["error_code"] == -40401
    c.login()
    assert mock_camera.login_count == 2
    mock_camera.inject_token_expiry(3)
    codes = []
    for _ in range(3):
        codes.append(c.send({"method": "multipleRequest", "params": {"requests": [led]}})["error_code"])
    assert codes == [0, 0, -40401]
    # the expired stok stays dead until re-login
    assert c.send({"method": "multipleRequest", "params": {"requests": [led]}})["error_code"] == -40401
    mock_camera.inject_token_expiry(0)
    c.login()
    assert c.multi(led)[0]["error_code"] == 0


def test_inject_error_and_http_status(mock_camera):
    c = _client(mock_camera)
    c.login()
    led = {"method": "getLedStatus", "params": {"led": {"name": ["config"]}}}
    priv = {"method": "getLensMaskConfig", "params": {"lens_mask": {"name": ["lens_mask_info"]}}}
    mock_camera.inject_error("getLedStatus", -40401, times=2)
    items = c.multi(led, priv)
    assert [i["error_code"] for i in items] == [-40401, 0]
    assert [i["error_code"] for i in c.multi(led)] == [-40401]
    assert [i["error_code"] for i in c.multi(led)] == [0]
    mock_camera.inject_http_status(500)
    assert c.send({"method": "multipleRequest", "params": {"requests": [led]}})["_status"] == 500
    mock_camera.inject_http_status(401, times=1)
    assert c.send({"method": "multipleRequest", "params": {"requests": [led]}})["_status"] == 401
    assert c.multi(led)[0]["error_code"] == 0


def test_lock_login_returns_40404_then_recovers(mock_camera):
    mock_camera.lock_login(0.6)
    c = _client(mock_camera)
    r = c.step1()
    assert r["error_code"] == -40404 and r["result"]["data"]["sec_left"] >= 1
    time.sleep(0.7)
    c.login()
    assert mock_camera.login_count == 1


def test_certificate_fingerprint_and_rotation(mock_camera):
    c = _client(mock_camera)
    assert c.peer_fingerprint() == mock_camera.cert_fingerprint
    old = mock_camera.cert_fingerprint
    new = mock_camera.rotate_certificate()
    assert new == mock_camera.cert_fingerprint != old
    c2 = _client(mock_camera)
    assert c2.peer_fingerprint() == new
    c.close()
    c2.close()


# ------------------------------------------------------------------------------------------
# legacy camera [spec:auth.legacy.login_request]
# ------------------------------------------------------------------------------------------
def test_legacy_login_and_plain_json(legacy_mock_camera):
    cam = legacy_mock_camera
    sc = SecureClient(cam.host, cam.https_port, cam.cloud_password)
    assert sc.step1()["error_code"] != -40413          # no secure challenge on legacy firmware
    lc = LegacyClient(cam.host, cam.https_port, cam.cloud_password)
    assert lc.login(password="nope")["error_code"] == -40401
    r = lc.login()
    assert r["error_code"] == 0 and r["result"]["stok"]
    out = lc.multi({"method": "getDeviceInfo", "params": {"device_info": {"name": ["basic_info"]}}},
                   {"method": "setLedStatus", "params": {"led": {"config": {"enabled": "off"}}}})
    assert out["error_code"] == 0
    assert out["result"]["responses"][0]["result"]["device_info"]["basic_info"]["device_model"] == "C200"
    assert cam.state.led is False
    cam.expire_sessions()
    assert lc.multi({"method": "getLedStatus", "params": {"led": {"name": ["config"]}}})["error_code"] == -40401
    assert cam.login_count == 1


# ------------------------------------------------------------------------------------------
# multipleRequest dispatcher on the DeviceShadow [spec:control.*]
# ------------------------------------------------------------------------------------------
def test_device_info_components_and_unknown_method(mock_camera):
    c = _client(mock_camera)
    c.login()
    info, comps, unknown = c.multi(
        {"method": "getDeviceInfo", "params": {"device_info": {"name": ["basic_info"]}}},
        {"method": "getAppComponentList", "params": {"app_component": {"name": ["app_component_list"]}}},
        {"method": "getNoSuchThing", "params": {}})
    bi = _ok(info)["device_info"]["basic_info"]
    assert bi["device_model"] == "C210" and bi["sw_version"] and bi["mac"] and bi["device_alias"]
    lst = _ok(comps)["app_component"]["app_component_list"]
    assert {"name": "ptz", "version": 1} in lst and {"name": "lensMask", "version": 2} in lst
    assert unknown == {"method": "getNoSuchThing", "result": {}, "error_code": -40106}
    # a non-multipleRequest inner request is answered directly
    single = c.send({"method": "getLedStatus", "params": {"led": {"name": ["config"]}}})
    assert single["error_code"] == 0 and single["result"] == {"led": {"config": {"enabled": "on"}}}
    assert c.send({"method": "nope"})["error_code"] == -40106


def test_privacy_led_daynight_alarm_motion_roundtrip(mock_camera):
    c = _client(mock_camera)
    c.login()
    s = mock_camera.state
    items = c.multi(
        {"method": "setLensMaskConfig", "params": {"lens_mask": {"lens_mask_info": {"enabled": "on"}}}},
        {"method": "setLedStatus", "params": {"led": {"config": {"enabled": "off"}}}},
        {"method": "setDayNightModeConfig", "params": {"image": {"common": {"inf_type": "on"}}}},
        {"method": "setNightVisionModeConfig", "params": {"image": {"switch": {"night_vision_mode": "md_night_vision"}}}},
        {"method": "doManualAlarm", "params": {"msg_alarm": {"manual_alarm": {"action": "start"}}}},
        {"method": "setDetectionConfig", "params": {"motion_detection": {"motion_det": {"enabled": "off", "digital_sensitivity": "80"}}}})
    assert [i["error_code"] for i in items] == [0] * 6
    assert (s.privacy, s.led, s.inf_type, s.night_vision_mode, s.alarm_active, s.motion_enabled, s.motion_sensitivity) == \
        (True, False, "on", "md_night_vision", True, False, 80)
    g = c.multi(
        {"method": "getLensMaskConfig", "params": {"lens_mask": {"name": ["lens_mask_info"]}}},
        {"method": "getLedStatus", "params": {"led": {"name": ["config"]}}},
        {"method": "getDayNightModeConfig", "params": {"image": {"name": "common"}}},
        {"method": "getDetectionConfig", "params": {"motion_detection": {"name": ["motion_det"], "table": ["region_info"]}}})
    assert _ok(g[0]) == {"lens_mask": {"lens_mask_info": {"enabled": "on"}}}
    assert _ok(g[1]) == {"led": {"config": {"enabled": "off"}}}
    assert _ok(g[2])["image"]["common"]["inf_type"] == "on"
    md = _ok(g[3])["motion_detection"]
    assert md["motion_det"]["enabled"] == "off" and md["motion_det"]["digital_sensitivity"] == "80" and "region_info" in md
    # tests can mutate the shadow directly and see it through the API
    s.privacy = False
    assert _ok(c.multi({"method": "getLensMaskConfig", "params": {"lens_mask": {"name": ["lens_mask_info"]}}})[0]) == \
        {"lens_mask": {"lens_mask_info": {"enabled": "off"}}}
    bad = c.multi({"method": "setLedStatus", "params": {"led": {"config": {"enabled": "maybe"}}}},
                  {"method": "setDayNightModeConfig", "params": {"image": {"common": {}}}},
                  {"method": "getLedStatus", "params": {"led": {"name": ["wrong"]}}})
    assert [b["error_code"] for b in bad] == [-40210, -40211, -40211]
    assert s.led is False
    logged = [e["method"] for e in mock_camera.request_log]
    assert logged[:6] == ["setLensMaskConfig", "setLedStatus", "setDayNightModeConfig", "setNightVisionModeConfig",
                          "doManualAlarm", "setDetectionConfig"]
    assert mock_camera.request_log[0]["params"] == {"lens_mask": {"lens_mask_info": {"enabled": "on"}}}


def test_ptz_and_presets(mock_camera):
    c = _client(mock_camera)
    c.login()
    s = mock_camera.state
    r = c.multi({"method": "motorMove", "params": {"motor": {"move": {"x_coord": "10", "y_coord": "-5"}}}})
    assert r[0]["error_code"] == 0 and (s.ptz_x, s.ptz_y) == (10, -5)
    r = c.multi({"method": "motorMove", "params": {"motor": {"move": {"x_coord": 10, "y_coord": 0}}}})
    assert r[0]["error_code"] == -40210                      # ints instead of integer strings
    r = c.multi({"method": "singalMove", "params": {"motor": {"movestep": {"direction": "90"}}}},
                {"method": "stopMove", "params": {"motor": {"stop": "null"}}},
                {"method": "singalMove", "params": {"motor": {"movestep": {"direction": "45"}}}})
    assert [i["error_code"] for i in r] == [0, 0, -40210] and (s.ptz_x, s.ptz_y) == (10, 5)
    s.ptz_x = 180
    r = c.multi({"method": "motorMove", "params": {"motor": {"move": {"x_coord": "10", "y_coord": "0"}}}})
    assert r[0]["error_code"] == -64304                      # boundary
    pr = _ok(c.multi({"method": "getPresetConfig", "params": {"preset": {"name": ["preset"]}}})[0])["preset"]["preset"]
    assert pr["id"] == ["1", "2"] and pr["name"] == ["Door", "Gate"] and len(pr["position_pan"]) == 2
    r = c.multi({"method": "addMotorPostion", "params": {"preset": {"set_preset": {"name": "Yard", "save_ptz": "1"}}}},
                {"method": "motorMoveToPreset", "params": {"preset": {"goto_preset": {"id": "1"}}}})
    assert [i["error_code"] for i in r] == [0, 0]
    assert s.presets["3"] == {"name": "Yard", "x": 180, "y": 5, "read_only": False} and (s.ptz_x, s.ptz_y) == (20, 0)
    r = c.multi({"method": "deletePreset", "params": {"preset": {"remove_preset": {"id": ["1"]}}}},
                {"method": "motorMoveToPreset", "params": {"preset": {"goto_preset": {"id": "1"}}}})
    assert [i["error_code"] for i in r] == [0, -40210] and "1" not in s.presets
    for i in range(10):
        last = c.multi({"method": "addMotorPostion", "params": {"preset": {"set_preset": {"name": f"p{i}", "save_ptz": "1"}}}})
    assert last[0]["error_code"] == -64306 and len(s.presets) == 8


def test_sd_status_and_reboot(mock_camera):
    c = _client(mock_camera)
    c.login()
    sd = _ok(c.multi({"method": "getSdCardStatus", "params": {"harddisk_manage": {"table": ["hd_info"]}}})[0])
    info = sd["harddisk_manage"]["hd_info"][0]["hd_info_1"]
    assert info["status"] == "normal" and info["total_space"]
    assert c.multi({"method": "rebootDevice", "params": {"system": {"reboot": "null"}}})[0]["error_code"] == 0
    assert mock_camera.state.reboot_count == 1
    led = {"method": "multipleRequest", "params": {"requests": [{"method": "getLedStatus", "params": {"led": {"name": ["config"]}}}]}}
    assert c.send(led)["error_code"] == -40401          # rebooted: sessions gone


def test_vod_search_days_and_clips(mock_camera):
    c = _client(mock_camera)
    c.login()
    s = mock_camera.state
    today = datetime.now(timezone.utc).date()
    yday = today - timedelta(days=1)
    r = _ok(c.multi({"method": "searchDateWithVideo", "params": {"playback": {"search_year_utility": {
        "channel": [0], "start_date": (today - timedelta(days=40)).strftime("%Y%m%d"), "end_date": today.strftime("%Y%m%d")}}}})[0])
    dates = [list(x.values())[0]["date"] for x in r["playback"]["search_results"]]
    assert dates == [yday.strftime("%Y%m%d"), today.strftime("%Y%m%d")]
    day0 = int(datetime(today.year, today.month, today.day, tzinfo=timezone.utc).timestamp())
    r = _ok(c.multi({"method": "searchVideoWithUTC", "params": {"playback": {"search_video_with_utc": {
        "id": 1, "channel": 0, "start_time": day0, "end_time": day0 + 86399, "start_index": 0, "end_index": 99}}}})[0])
    clips = [list(x.values())[0] for x in r["playback"]["search_video_results"]]
    assert len(clips) == 3 and r["playback"]["to_be_continued"] == 0
    todays = sorted((x for x in s.recordings if x.start.date() == today), key=lambda x: x.start)
    assert [(k["startTime"], k["endTime"]) for k in clips] == [(int(x.start.timestamp()), int(x.end.timestamp())) for x in todays]
    assert clips[0]["vedio_type"] == str(todays[0].event_type)
    r = _ok(c.multi({"method": "searchVideoOfDay", "params": {"playback": {"search_video_utility": {
        "id": 1, "channel": 0, "date": yday.strftime("%Y%m%d"), "start_index": 0, "end_index": 99}}}})[0])
    assert len(r["playback"]["search_video_results"]) == 1
    # paging: 150 extra clips on one day
    RecordingClip = type(s.recordings[0])  # tapo_desktop.models.RecordingClip, via the shadow
    base = datetime(today.year, today.month, today.day, 5, tzinfo=timezone.utc)
    s.recordings.extend(RecordingClip(base + timedelta(minutes=i), base + timedelta(minutes=i, seconds=30), 2) for i in range(150))
    q = {"id": 1, "channel": 0, "start_time": day0, "end_time": day0 + 86399, "start_index": 0, "end_index": 99}
    p1 = _ok(c.multi({"method": "searchVideoWithUTC", "params": {"playback": {"search_video_with_utc": q}}})[0])["playback"]
    q.update(start_index=100, end_index=199)
    p2 = _ok(c.multi({"method": "searchVideoWithUTC", "params": {"playback": {"search_video_with_utc": q}}})[0])["playback"]
    assert (len(p1["search_video_results"]), p1["to_be_continued"]) == (100, 1)
    assert (len(p2["search_video_results"]), p2["to_be_continued"]) == (53, 0)
    bad = c.multi({"method": "searchVideoWithUTC", "params": {"playback": {"search_video_with_utc": {"start_time": "x", "end_time": 1}}}})
    assert bad[0]["error_code"] == -40210


# ------------------------------------------------------------------------------------------
# ONVIF [spec:onvif.*]
# ------------------------------------------------------------------------------------------
import xml.etree.ElementTree as ET  # noqa: E402

from tests.fixtures.raw_clients import soap  # noqa: E402


def _find(root, name):
    return [e for e in root.iter() if e.tag.rsplit("}", 1)[-1] == name]


def _onvif(cam, body, path="/onvif/device_service", auth=True, **kw):
    user, pw = (cam.camera_user, cam.camera_password) if auth else (None, None)
    status, data = soap(cam.host, cam.onvif_port, body, user, pw, path=path, **kw)
    return status, ET.fromstring(data)


def test_onvif_auth_and_device_queries(mock_camera):
    cam = mock_camera
    st, root = _onvif(cam, "<tds:GetDeviceInformation/>", auth=False)
    assert st == 400 and _find(root, "Fault") and "NotAuthorized" in ET.tostring(root).decode()
    st, data = soap(cam.host, cam.onvif_port, "<tds:GetDeviceInformation/>", cam.camera_user, "wrong")
    assert st == 400 and b"NotAuthorized" in data
    st, root = _onvif(cam, "<tds:GetSystemDateAndTime/>", auth=False)  # allowed without auth
    assert st == 200 and _find(root, "UTCDateTime")
    st, root = _onvif(cam, "<tds:GetDeviceInformation/>")
    assert st == 200
    assert _find(root, "Model")[0].text == "C210" and _find(root, "Manufacturer")[0].text
    assert _find(root, "FirmwareVersion")[0].text and _find(root, "SerialNumber")[0].text and _find(root, "HardwareId")
    st, root = _onvif(cam, '<tds:GetCapabilities><tds:Category>All</tds:Category></tds:GetCapabilities>')
    xaddrs = [e.text for e in _find(root, "XAddr")]
    assert st == 200 and all(x.startswith(f"http://127.0.0.1:{cam.onvif_port}/onvif/") for x in xaddrs) and len(xaddrs) >= 4
    st, root = _onvif(cam, "<trt:GetProfiles/>", path="/onvif/service")
    profiles = _find(root, "Profiles")
    assert st == 200 and [p.get("token") for p in profiles] == ["profile_1", "profile_2"]
    st, root = _onvif(cam, "<tds:NoSuchOp/>")
    assert st == 400 and b"ActionNotSupported" in ET.tostring(root)


def test_onvif_ptz_and_presets(mock_camera):
    cam = mock_camera
    st, _ = _onvif(cam, '<tptz:ContinuousMove><tptz:ProfileToken>profile_1</tptz:ProfileToken><tptz:Velocity>'
                        '<tt:PanTilt x="0.5" y="-1.0"/></tptz:Velocity></tptz:ContinuousMove>', path="/onvif/service")
    assert st == 200 and (cam.state.ptz_x, cam.state.ptz_y) == (5, -10) and cam.state.ptz_moving
    st, _ = _onvif(cam, "<tptz:Stop><tptz:ProfileToken>profile_1</tptz:ProfileToken></tptz:Stop>", path="/onvif/service")
    assert st == 200 and not cam.state.ptz_moving
    st, root = _onvif(cam, "<tptz:GetPresets><tptz:ProfileToken>profile_1</tptz:ProfileToken></tptz:GetPresets>",
                      path="/onvif/service")
    presets = _find(root, "Preset")
    assert st == 200 and [p.get("token") for p in presets] == ["1", "2"]
    assert [_find(p, "Name")[0].text for p in presets] == ["Door", "Gate"]
    st, _ = _onvif(cam, "<tptz:GotoPreset><tptz:ProfileToken>profile_1</tptz:ProfileToken><tptz:PresetToken>2"
                        "</tptz:PresetToken></tptz:GotoPreset>", path="/onvif/service")
    assert st == 200 and (cam.state.ptz_x, cam.state.ptz_y) == (-60, 10)
    st, _ = _onvif(cam, "<tptz:GotoPreset><tptz:ProfileToken>profile_1</tptz:ProfileToken><tptz:PresetToken>99"
                        "</tptz:PresetToken></tptz:GotoPreset>", path="/onvif/service")
    assert st == 400


def test_onvif_pullpoint_motion_events(mock_camera):
    cam = mock_camera
    st, root = _onvif(cam, "<tev:CreatePullPointSubscription><tev:InitialTerminationTime>PT60S</tev:InitialTerminationTime>"
                           "</tev:CreatePullPointSubscription>", path="/onvif/service")
    assert st == 200
    addr = _find(root, "Address")[0].text
    assert addr.startswith(f"http://127.0.0.1:{cam.onvif_port}/onvif/subscription/")
    sub_path = addr.split(str(cam.onvif_port), 1)[1]
    assert _find(root, "TerminationTime") and _find(root, "CurrentTime")
    # long-poll with nothing queued returns empty after ~Timeout
    t0 = time.monotonic()
    st, root = _onvif(cam, "<tev:PullMessages><tev:Timeout>PT0.6S</tev:Timeout><tev:MessageLimit>10</tev:MessageLimit>"
                           "</tev:PullMessages>", path=sub_path)
    took = time.monotonic() - t0
    assert st == 200 and not _find(root, "NotificationMessage") and 0.5 <= took < 2.0
    # an event emitted during a long-poll wakes it up promptly
    threading.Timer(0.3, cam.emit_motion_event, args=(True,)).start()
    t0 = time.monotonic()
    st, root = _onvif(cam, "<tev:PullMessages><tev:Timeout>PT5S</tev:Timeout><tev:MessageLimit>10</tev:MessageLimit>"
                           "</tev:PullMessages>", path=sub_path)
    assert st == 200 and time.monotonic() - t0 < 2.0
    msgs = _find(root, "NotificationMessage")
    assert len(msgs) == 1
    assert _find(msgs[0], "Topic")[0].text == "tns1:RuleEngine/CellMotionDetector/Motion"
    item = [e for e in _find(msgs[0], "SimpleItem") if e.get("Name") == "IsMotion"][0]
    assert item.get("Value") == "true"
    cam.emit_motion_event(False)
    cam.emit_motion_event(True)
    st, root = _onvif(cam, "<tev:PullMessages><tev:Timeout>PT1S</tev:Timeout><tev:MessageLimit>1</tev:MessageLimit>"
                           "</tev:PullMessages>", path=sub_path)
    vals = [e.get("Value") for e in _find(root, "SimpleItem") if e.get("Name") == "IsMotion"]
    assert vals == ["false"]                         # MessageLimit honoured, FIFO order
    st, root = _onvif(cam, "<tev:PullMessages><tev:Timeout>PT1S</tev:Timeout><tev:MessageLimit>10</tev:MessageLimit>"
                           "</tev:PullMessages>", path=sub_path)
    assert [e.get("Value") for e in _find(root, "SimpleItem") if e.get("Name") == "IsMotion"] == ["true"]
    st, _ = _onvif(cam, "<wsnt:Renew><wsnt:TerminationTime>PT120S</wsnt:TerminationTime></wsnt:Renew>", path=sub_path)
    assert st == 200
    st, _ = _onvif(cam, "<wsnt:Unsubscribe/>", path=sub_path)
    assert st == 200
    st, _ = _onvif(cam, "<tev:PullMessages><tev:Timeout>PT1S</tev:Timeout><tev:MessageLimit>1</tev:MessageLimit>"
                        "</tev:PullMessages>", path=sub_path)
    assert st == 400


def test_onvif_subscription_expiry_and_event_buffering(mock_camera):
    cam = mock_camera
    cam.emit_motion_event(True)                      # before any subscription: buffered for the first one
    st, root = _onvif(cam, "<tev:CreatePullPointSubscription><tev:InitialTerminationTime>PT1S</tev:InitialTerminationTime>"
                           "</tev:CreatePullPointSubscription>", path="/onvif/service")
    # PullMessages posted to /onvif/service is routed to the only subscription
    st, root = _onvif(cam, "<tev:PullMessages><tev:Timeout>PT1S</tev:Timeout><tev:MessageLimit>5</tev:MessageLimit>"
                           "</tev:PullMessages>", path="/onvif/service")
    assert st == 200 and len(_find(root, "NotificationMessage")) == 1
    time.sleep(1.1)
    st, _ = _onvif(cam, "<tev:PullMessages><tev:Timeout>PT1S</tev:Timeout><tev:MessageLimit>5</tev:MessageLimit>"
                        "</tev:PullMessages>", path="/onvif/service")
    assert st == 400                                 # expired without Renew


def test_ws_discovery_responder(mock_camera):
    probe = (b'<?xml version="1.0"?><s:Envelope xmlns:s="http://www.w3.org/2003/05/soap-envelope" '
             b'xmlns:a="http://schemas.xmlsoap.org/ws/2004/08/addressing" xmlns:d="http://schemas.xmlsoap.org/ws/2005/04/discovery" '
             b'xmlns:dn="http://www.onvif.org/ver10/network/wsdl"><s:Header><a:MessageID>uuid:1234</a:MessageID></s:Header>'
             b"<s:Body><d:Probe><d:Types>dn:NetworkVideoTransmitter</d:Types></d:Probe></s:Body></s:Envelope>")
    out = mock_camera.ws_discovery_responder(probe)
    root = ET.fromstring(out)
    assert _find(root, "RelatesTo")[0].text == "uuid:1234"
    assert _find(root, "XAddrs")[0].text == f"http://127.0.0.1:{mock_camera.onvif_port}/onvif/device_service"
    assert "NetworkVideoTransmitter" in _find(root, "Types")[0].text
    assert mock_camera.ws_discovery_responder(b"garbage<") == b""
    assert ET.fromstring(mock_camera.ws_discovery_responder()) is not None


# ------------------------------------------------------------------------------------------
# robustness: the dispatcher answers any params with an error code, never an exception
# ------------------------------------------------------------------------------------------
from hypothesis import given, settings  # noqa: E402
from hypothesis import strategies as st  # noqa: E402

from tests.fixtures.mock_tapo_server.dispatcher import HANDLERS, dispatch  # noqa: E402

_json = st.recursive(st.none() | st.booleans() | st.integers(-10**6, 10**6) | st.text(max_size=8),
                     lambda kids: st.lists(kids, max_size=3) | st.dictionaries(st.text(max_size=12), kids, max_size=3),
                     max_leaves=12)
_known_keys = st.sampled_from(["device_info", "motor", "preset", "lens_mask", "image", "led", "msg_alarm",
                               "motion_detection", "harddisk_manage", "playback", "system", "app_component"])


@settings(max_examples=300, deadline=None)
@given(method=st.sampled_from(sorted(HANDLERS)) | st.text(max_size=10),
       params=_json | st.dictionaries(_known_keys, _json | st.dictionaries(st.text(max_size=16), _json, max_size=3),
                                      max_size=2))
def test_dispatcher_never_raises(method, params):
    shadow = DeviceShadow()
    code, result = dispatch(shadow, method, params)
    assert isinstance(code, int) and isinstance(result, dict)
    if method not in HANDLERS:
        assert code == -40106
