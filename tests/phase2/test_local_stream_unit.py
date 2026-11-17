"""Tier 1: pure parts of the :8800 live view (request builders, ffmpeg args, argument checks)."""
from __future__ import annotations

import pytest

from tapo_desktop.core.media_engine.ffmpeg import build_ffmpeg_args
from tapo_desktop.core.media_engine.local_stream import (
    LocalStreamSupervisor,
    build_preview_request,
    build_stop_request,
)
from tapo_desktop.core.media_engine.supervisor import StreamState, StreamSupervisor

pytestmark = pytest.mark.unit


def _noop(*_a):
    pass


def test_preview_request_matches_spec():
    """[spec:stream.control.preview] {type:request, params:{method:get, preview:{channels, resolutions, audio}}}."""
    assert build_preview_request("HD") == {
        "type": "request",
        "params": {"method": "get", "preview": {"channels": [0], "resolutions": ["HD"], "audio": ["default"]}},
    }
    req = build_preview_request("VGA", channel=1)
    assert req["params"]["preview"]["channels"] == [1]
    assert req["params"]["preview"]["resolutions"] == ["VGA"]


def test_preview_request_rejects_unknown_resolution():
    with pytest.raises(ValueError):
        build_preview_request("4K")


def test_stop_request_matches_spec():
    assert build_stop_request() == {"type": "request", "params": {"method": "do", "stop": "null"}}


def test_ffmpeg_args_for_piped_mpegts():
    args = build_ffmpeg_args("pipe:0", 640, 360, hwaccel=None, input_format="mpegts")
    i = args.index("-i")
    assert args[i + 1] == "pipe:0"
    assert args[args.index("-f")] == "-f" and args[args.index("-f") + 1] == "mpegts"
    assert args.index("-f") < i
    assert "-rtsp_transport" not in args and "-timeout" not in args
    assert "-probesize" in args and args[-1] == "pipe:1"
    assert "scale=640:360" in args[args.index("-vf") + 1]
    assert args[args.index("-fps_mode") + 1] == "passthrough"


def test_ffmpeg_args_default_unchanged():
    rtsp = build_ffmpeg_args("rtsp://u:p@h:554/stream1", 640, 360, hwaccel=None)
    assert "-rtsp_transport" in rtsp and rtsp[rtsp.index("-i") + 1].startswith("rtsp://")
    assert "mpegts" not in rtsp


@pytest.mark.parametrize("kw", [{"resolution": "4K"}, {"width": 0}, {"height": -1}, {"stall_timeout": 0},
                                {"backoff_initial": 2.0, "backoff_max": 1.0}, {"port": 0}, {"host": ""}])
def test_constructor_validates(kw):
    args = {"host": "192.168.1.20", "password": "pw", "width": 640, "height": 360}
    args.update({k: v for k, v in kw.items() if k in args})
    extra = {k: v for k, v in kw.items() if k not in args}
    with pytest.raises(ValueError):
        LocalStreamSupervisor(args["host"], args["password"], args["width"], args["height"], on_frame=_noop, **extra)


def test_initial_state_repr_and_surface():
    sup = LocalStreamSupervisor("192.168.1.20", "s3cret-pw", 640, 360, resolution="HD", on_frame=_noop)
    assert sup.state is StreamState.IDLE
    assert sup.pid is None
    assert "s3cret-pw" not in repr(sup) and "192.168.1.20" in repr(sup)
    stats = sup.stats()
    assert stats["frames"] == 0 and stats["reconnects"] == 0 and stats["fps"] == 0.0
    for name in ("start", "stop", "switch", "stats"):
        assert callable(getattr(sup, name))
    assert isinstance(sup, StreamSupervisor) or hasattr(sup, "state")
    sup.stop()  # stop before start is a no-op that never raises


def test_switch_validates():
    sup = LocalStreamSupervisor("192.168.1.20", "pw", on_frame=_noop)
    with pytest.raises(ValueError):
        sup.switch("4K", 640, 360)
    with pytest.raises(ValueError):
        sup.switch("HD", 0, 720)
