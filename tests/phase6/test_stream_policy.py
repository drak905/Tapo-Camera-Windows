"""Pure view-driven streaming policy (services/stream_policy.py): which cameras stream, at which quality /
decode size, with which decoder. No Qt, no network."""
from __future__ import annotations

import pytest

from tapo_desktop.models import CameraConfig, StreamQuality
from tapo_desktop.services.stream_policy import (
    GRID_DECODE_SIZE,
    HARDWARE_DECODER,
    HD_DECODE_SIZE,
    SOFTWARE_DECODER,
    StreamRequest,
    decoder_for,
    plan_streams,
    reserved_relay_streams,
    view_size,
)

pytestmark = pytest.mark.unit

SD, HD = StreamQuality.SD, StreamQuality.HD


def _cfgs(n: int, **kw) -> dict[str, CameraConfig]:
    cams = [CameraConfig(name=f"Cam {i}", host=f"10.0.0.{i + 1}", id=f"cam{i}", **kw) for i in range(n)]
    return {c.id: c for c in cams}


# ------------------------------------------------------------------ decode size + decoder
@pytest.mark.parametrize("quality,size", [
    (StreamQuality.SD, (640, 360)), (StreamQuality.TELE_SD, (640, 360)),
    (StreamQuality.HD, (1280, 720)), (StreamQuality.TELE_HD, (1280, 720)),
])
def test_view_size_per_quality(quality, size):
    assert view_size(quality) == size
    assert view_size(quality.value) == size  # plain string values are accepted too


def test_decode_size_constants():
    assert GRID_DECODE_SIZE == (640, 360) and HD_DECODE_SIZE == (1280, 720)


def test_grid_reexports_the_policy_view_size():
    import os

    os.environ.setdefault("QT_QPA_PLATFORM", "offscreen")
    from tapo_desktop.ui import grid

    assert grid.view_size is view_size


@pytest.mark.parametrize("quality", [StreamQuality.SD, StreamQuality.TELE_SD])
def test_small_tiles_use_software_decoding(quality):
    """Measured: d3d11va costs ~110 MB per ffmpeg vs ~38 MB in software for VGA tiles."""
    assert decoder_for(quality) == SOFTWARE_DECODER == "none"


@pytest.mark.parametrize("quality", [StreamQuality.HD, StreamQuality.TELE_HD])
def test_hd_streams_try_hardware_decoding(quality):
    assert decoder_for(quality) == HARDWARE_DECODER == "auto"


def test_decoder_rejects_unknown_quality():
    with pytest.raises(ValueError):
        decoder_for("stream9")


# ------------------------------------------------------------------ plan_streams
def test_request_args():
    req = StreamRequest(SD, 640, 360)
    assert req.as_args() == (SD, 640, 360)


@pytest.mark.parametrize("n", [1, 2, 3, 4, 5])
def test_every_camera_in_a_full_grid_streams(n):
    cfgs = _cfgs(25)
    visible = list(cfgs)[: n * n]
    plan = plan_streams(visible, cfgs)
    assert list(plan) == visible  # slot order
    assert len(plan) == n * n
    assert all(r == StreamRequest(SD, 640, 360) for r in plan.values())


def test_empty_unknown_and_repeated_slots_do_not_stream():
    cfgs = _cfgs(3)
    plan = plan_streams(["cam2", None, "ghost", "cam0", "cam2", None], cfgs)
    assert list(plan) == ["cam2", "cam0"]


def test_per_camera_grid_quality_is_honoured():
    cfgs = _cfgs(2)
    cfgs["cam1"] = CameraConfig(name="Tele", host="10.0.0.9", id="cam1", grid_quality=StreamQuality.TELE_HD)
    plan = plan_streams(["cam0", "cam1"], cfgs)
    assert plan["cam0"] == StreamRequest(SD, 640, 360)
    assert plan["cam1"] == StreamRequest(StreamQuality.TELE_HD, 1280, 720)


def test_fullscreen_streams_only_that_camera_in_fullscreen_quality():
    cfgs = _cfgs(9)
    plan = plan_streams(list(cfgs), cfgs, fullscreen="cam4")
    assert plan == {"cam4": StreamRequest(HD, 1280, 720)}


def test_fullscreen_camera_outside_the_view_is_ignored():
    cfgs = _cfgs(9)
    assert plan_streams(["cam0", "cam1"], cfgs, fullscreen="cam7") == {
        "cam0": StreamRequest(SD, 640, 360), "cam1": StreamRequest(SD, 640, 360)}
    assert plan_streams(["cam0"], cfgs, fullscreen="ghost") == {"cam0": StreamRequest(SD, 640, 360)}


def test_empty_view_streams_nothing():
    assert plan_streams([], _cfgs(2)) == {}
    assert plan_streams([None, None], _cfgs(2)) == {}


# ------------------------------------------------------------------ relay reservations
def test_every_viewed_camera_is_reserved_a_relay_slot():
    views = {"a": (SD, 640, 360), "b": None, "c": (HD, 1280, 720)}
    assert reserved_relay_streams(views) == frozenset({"a", "c"})
    assert reserved_relay_streams({}) == frozenset()
