"""Tier 1: OnvifPtz request bodies (velocity clamping, profile selection) via a fake OnvifClient."""
from __future__ import annotations

import re

import pytest
from hypothesis import given, settings
from hypothesis import strategies as st

from tapo_desktop.core.onvif_service.ptz import OnvifPtz
from tapo_desktop.models import Preset
from tests.unit._onvif_samples import FakeOnvifClient, tag_attrs, tag_text

pytestmark = pytest.mark.unit


def _move_body(fake: FakeOnvifClient) -> str:
    bodies = fake.bodies("ContinuousMove")
    assert bodies, f"no ContinuousMove sent; ops={fake.ops()}"
    return bodies[-1]


def _velocity(body: str) -> tuple[float, float, float | None]:
    pt = tag_attrs(body, "PanTilt")
    assert pt is not None, body
    zoom = tag_attrs(body, "Zoom")
    return float(pt["x"]), float(pt["y"]), (float(zoom["x"]) if zoom and "x" in zoom else None)


def test_continuous_move_in_range():
    fake = FakeOnvifClient()
    OnvifPtz(fake).continuous_move(0.5, -0.25, 0.1)
    body = _move_body(fake)
    assert _velocity(body) == (0.5, -0.25, 0.1)
    assert tag_text(body, "ProfileToken") == "profile_1"


@pytest.mark.parametrize("pan,tilt,zoom,expected", [
    (2.0, -3.0, 0.5, (1.0, -1.0, 0.5)),
    (-1.5, 1.0001, 9.0, (-1.0, 1.0, 1.0)),
    (1.0, -1.0, -7.0, (1.0, -1.0, -1.0)),
])
def test_continuous_move_clamps(pan, tilt, zoom, expected):
    fake = FakeOnvifClient()
    OnvifPtz(fake).continuous_move(pan, tilt, zoom)
    assert _velocity(_move_body(fake)) == expected


@given(pan=st.floats(-1e6, 1e6), tilt=st.floats(-1e6, 1e6), zoom=st.floats(-1e6, 1e6))
@settings(max_examples=60, deadline=None)
def test_continuous_move_clamp_property(pan, tilt, zoom):
    fake = FakeOnvifClient()
    OnvifPtz(fake, profile_token="p").continuous_move(pan, tilt, zoom)
    x, y, z = _velocity(_move_body(fake))
    clamp = lambda v: max(-1.0, min(1.0, v))  # noqa: E731
    assert x == pytest.approx(clamp(pan), abs=5e-3)
    assert y == pytest.approx(clamp(tilt), abs=5e-3)
    assert z is None or z == pytest.approx(clamp(zoom), abs=5e-3)


def test_continuous_move_timeout_duration():
    fake = FakeOnvifClient()
    OnvifPtz(fake).continuous_move(0.3, 0.0, timeout_s=2.5)
    timeout = tag_text(_move_body(fake), "Timeout")
    assert timeout is not None
    m = re.fullmatch(r"PT(\d+(?:\.\d+)?)S", timeout)
    assert m and float(m.group(1)) == pytest.approx(2.5)


def test_continuous_move_without_timeout_has_no_timeout_element():
    fake = FakeOnvifClient()
    OnvifPtz(fake).continuous_move(0.3, 0.0)
    assert tag_text(_move_body(fake), "Timeout") is None


def test_explicit_profile_token_is_used():
    fake = FakeOnvifClient()
    OnvifPtz(fake, profile_token="profile_2").continuous_move(0.1, 0.1)
    assert tag_text(_move_body(fake), "ProfileToken") == "profile_2"


def test_stop_body():
    fake = FakeOnvifClient()
    OnvifPtz(fake).stop()
    bodies = fake.bodies("Stop")
    assert bodies, f"no Stop sent; ops={fake.ops()}"
    assert tag_text(bodies[-1], "ProfileToken") == "profile_1"
    pan = tag_text(bodies[-1], "PanTilt")
    assert pan is None or pan.lower() == "true"


def test_get_presets():
    fake = FakeOnvifClient()
    presets = OnvifPtz(fake).get_presets()
    assert [(p.id, p.name) for p in presets] == [("1", "Door"), ("2", "Gate")]
    assert all(isinstance(p, Preset) for p in presets)
    assert tag_text(fake.bodies("GetPresets")[-1], "ProfileToken") == "profile_1"


def test_goto_preset_body():
    fake = FakeOnvifClient()
    OnvifPtz(fake).goto_preset("2")
    body = fake.bodies("GotoPreset")[-1]
    assert tag_text(body, "PresetToken") == "2"
    assert tag_text(body, "ProfileToken") == "profile_1"
