"""-71103 on ``searchVideoWithUTC`` (real C510W finding): the LAN client retries once with ``player_id``
and remembers the form that worked; cameras that accept ``"id": 1`` see no change."""
from __future__ import annotations

import datetime as dt

import pytest

from tapo_desktop.core.control_client import dtos
from tapo_desktop.core.control_client.client import TapoControlClient
from tapo_desktop.core.errors import TapoDeviceError

pytestmark = [pytest.mark.integration, pytest.mark.timeout(60)]


def _client(cam, **kw) -> TapoControlClient:
    from tapo_desktop.core.control_client.transport import PinStore

    return TapoControlClient(cam.host, cam.cloud_password, port=cam.https_port, pin_store=PinStore(), timeout=5, **kw)


def _forms(cam) -> list[str]:
    out = []
    for r in cam.request_log:
        if r["method"] == "searchVideoWithUTC":
            inner = r["params"]["playback"]["search_video_with_utc"]
            out.append("player_id" if "player_id" in inner else "id")
    return out


def test_dtos_player_id_form():
    built = dtos.search_day_clips_req(100, 200, 0, 49, player_id="ab" * 16)
    inner = built["params"]["playback"]["search_video_with_utc"]
    assert "id" not in inner and inner["player_id"] == "ab" * 16
    assert inner["channel"] == 0 and (inner["start_time"], inner["end_time"]) == (100, 200)
    assert (inner["start_index"], inner["end_index"]) == (0, 49)
    legacy = dtos.search_day_clips_req(100, 200)["params"]["playback"]["search_video_with_utc"]
    assert legacy["id"] == 1 and "player_id" not in legacy


def test_default_camera_keeps_the_id_form(mock_camera):
    client = _client(mock_camera)
    try:
        today = dt.datetime.now(dt.timezone.utc).date()
        assert client.recordings_for_day(today, tz=dt.timezone.utc)
        assert _forms(mock_camera) == ["id"]
    finally:
        client.close()


def test_minus_71103_retries_with_player_id_and_remembers(mock_camera):
    mock_camera.clip_search_requires_player_id = True
    client = _client(mock_camera, player_id="0123456789abcdef0123456789abcdef")
    try:
        today = dt.datetime.now(dt.timezone.utc).date()
        clips = client.recordings_for_day(today, tz=dt.timezone.utc)
        expected = [c for c in mock_camera.state.recordings if c.start.date() == today]
        assert len(clips) == len(expected) > 0
        assert _forms(mock_camera) == ["id", "player_id"]
        last = [r for r in mock_camera.request_log if r["method"] == "searchVideoWithUTC"][-1]
        assert last["params"]["playback"]["search_video_with_utc"]["player_id"] == "0123456789abcdef0123456789abcdef"
        client.recordings_for_day(today - dt.timedelta(days=1), tz=dt.timezone.utc)
        assert _forms(mock_camera) == ["id", "player_id", "player_id"]
    finally:
        client.close()


def test_default_player_id_is_stable_32_hex(mock_camera):
    mock_camera.clip_search_requires_player_id = True
    ids = []
    for _ in range(2):
        client = _client(mock_camera)
        try:
            client.recordings_for_day(dt.date.today())
        finally:
            client.close()
        last = [r for r in mock_camera.request_log if r["method"] == "searchVideoWithUTC"][-1]
        ids.append(last["params"]["playback"]["search_video_with_utc"]["player_id"])
    assert ids[0] == ids[1] and len(ids[0]) == 32 and int(ids[0], 16) >= 0


def test_both_forms_refused_raises(mock_camera):
    mock_camera.inject_error("searchVideoWithUTC", -71103, times=2)
    client = _client(mock_camera)
    try:
        with pytest.raises(TapoDeviceError) as err:
            client.recordings_for_day(dt.date.today())
        assert err.value.code == -71103
        assert _forms(mock_camera) == ["id", "player_id"]
    finally:
        client.close()
