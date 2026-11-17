"""ControlsPanel + PtzPad (docs/CONTRACT_UI.md)."""
from __future__ import annotations

import os

os.environ.setdefault("QT_QPA_PLATFORM", "offscreen")

import pytest  # noqa: E402
from PyQt6.QtCore import Qt  # noqa: E402
from PyQt6.QtWidgets import QAbstractButton, QComboBox  # noqa: E402

from tapo_desktop.models import DayNightMode  # noqa: E402
from tapo_desktop.ui.ptz_pad import PtzPad  # noqa: E402
from tests.e2e.fakes import default_status  # noqa: E402

pytestmark = pytest.mark.ui


def _sign(v):
    return (v > 0) - (v < 0)


def _panel(env, qtbot, camera_id="cam0"):
    from tapo_desktop.ui.controls_panel import ControlsPanel

    panel = ControlsPanel(env.controller)
    qtbot.addWidget(panel)
    panel.resize(400, 400)
    panel.show()
    qtbot.waitExposed(panel)
    panel.set_camera(camera_id)
    qtbot.waitUntil(lambda: bool(env.has_svc(camera_id) and env.svc(camera_id).calls_to("get_status")
                    and env.svc(camera_id).calls_to("get_presets")), timeout=3000)
    env.idle(qtbot)
    return panel


def _btn(panel, name) -> QAbstractButton:
    b = panel.findChild(QAbstractButton, name)
    assert b is not None, name
    assert b.isCheckable() or name.startswith("ptz") or name == "gotoPresetButton", name
    return b


def _combo(panel, name) -> QComboBox:
    c = panel.findChild(QComboBox, name)
    assert c is not None, name
    return c


def test_panel_reflects_status_without_calling_back(make_env, qtbot):
    status = default_status() | {"led": True, "day_night": DayNightMode.ON, "privacy": True}
    env = make_env(1, status=status)
    panel = _panel(env, qtbot)
    assert panel.objectName() == "controlsPanel"
    assert panel.camera_id() == "cam0"
    qtbot.waitUntil(lambda: bool(_btn(panel, "ledToggle").isChecked()), timeout=2000)
    assert _btn(panel, "privacyToggle").isChecked()
    assert _btn(panel, "motionToggle").isChecked()
    assert not _btn(panel, "alarmToggle").isChecked()
    assert _combo(panel, "dayNightCombo").currentData() == DayNightMode.ON
    env.idle(qtbot)
    names = env.svc("cam0").call_names()
    assert not [n for n in names if n.startswith("set_")], names


def test_day_night_combo_items_and_user_change(make_env, qtbot):
    env = make_env(1)
    panel = _panel(env, qtbot)
    combo = _combo(panel, "dayNightCombo")
    assert [combo.itemData(i) for i in range(combo.count())] == [DayNightMode.AUTO, DayNightMode.ON,
                                                                  DayNightMode.OFF]
    assert combo.currentData() == DayNightMode.AUTO
    combo.setFocus()
    qtbot.keyClick(combo, Qt.Key.Key_Down)  # user picks the next item ("on")
    env.idle(qtbot)
    assert [c.args for c in env.svc("cam0").calls_to("set_day_night")] == [(DayNightMode.ON,)]


def test_privacy_toggle_optimistic(make_env, qtbot):
    env = make_env(1)
    panel = _panel(env, qtbot)
    toggle = _btn(panel, "privacyToggle")
    assert not toggle.isChecked()
    toggle.click()
    assert toggle.isChecked()
    env.idle(qtbot)
    assert toggle.isChecked()
    assert [c.args for c in env.svc("cam0").calls_to("set_privacy")] == [(True,)]


def test_privacy_toggle_rolls_back_on_failure(make_env, qtbot):
    env = make_env(1)
    panel = _panel(env, qtbot)
    env.factory.fail("cam0", "set_privacy")
    failures = []
    env.controller.command_failed.connect(lambda *a: failures.append(a))
    toggle = _btn(panel, "privacyToggle")
    toggle.click()
    assert toggle.isChecked()  # optimistic
    qtbot.waitUntil(lambda: bool(failures), timeout=3000)
    qtbot.waitUntil(lambda: bool(not toggle.isChecked()), timeout=3000)
    env.idle(qtbot)
    assert not toggle.isChecked()
    assert failures[0][:2] == ("cam0", "set_privacy")
    assert len(env.svc("cam0").calls_to("set_privacy")) == 1  # rollback does not re-send


@pytest.mark.parametrize("name,method,expected", [("ledToggle", "set_led", True),
                                                  ("alarmToggle", "set_alarm", True),
                                                  ("motionToggle", "set_motion_detection", False)])
def test_other_toggles_forward(make_env, qtbot, name, method, expected):
    env = make_env(1)
    panel = _panel(env, qtbot)
    _btn(panel, name).click()
    env.idle(qtbot)
    assert [c.args for c in env.svc("cam0").calls_to(method)] == [(expected,)]


def test_presets_populate_and_goto(make_env, qtbot):
    env = make_env(1)
    panel = _panel(env, qtbot)
    combo = _combo(panel, "presetCombo")
    qtbot.waitUntil(lambda: bool(combo.count() == 3), timeout=2000)
    assert [combo.itemText(i) for i in range(3)] == ["Door", "Window", "Garden"]
    assert [combo.itemData(i) for i in range(3)] == ["1", "2", "3"]
    combo.setCurrentIndex(2)
    _btn(panel, "gotoPresetButton").click()
    env.idle(qtbot)
    assert [c.args for c in env.svc("cam0").calls_to("goto_preset")] == [("3",)]


@pytest.mark.parametrize("button,signs", [("ptzUp", (0, 1)), ("ptzDown", (0, -1)),
                                          ("ptzLeft", (-1, 0)), ("ptzRight", (1, 0))])
def test_ptz_buttons_hold_to_move(make_env, qtbot, button, signs):
    env = make_env(1)
    panel = _panel(env, qtbot)
    pad = panel.findChild(PtzPad, "ptzPad")
    assert pad is not None
    b = _btn(pad, button)
    qtbot.mousePress(b, Qt.MouseButton.LeftButton)
    env.idle(qtbot)
    moves = env.svc("cam0").calls_to("ptz_move")
    assert len(moves) == 1
    assert tuple(_sign(v) for v in moves[0].args) == signs
    qtbot.mouseRelease(b, Qt.MouseButton.LeftButton)
    env.idle(qtbot)
    assert [n for n in env.svc("cam0").call_names() if n.startswith("ptz")] == ["ptz_move", "ptz_stop"]


def test_ptz_stop_button(make_env, qtbot):
    env = make_env(1)
    panel = _panel(env, qtbot)
    pad = panel.findChild(PtzPad, "ptzPad")
    _btn(pad, "ptzStop").click()
    env.idle(qtbot)
    assert [n for n in env.svc("cam0").call_names() if n.startswith("ptz")] == ["ptz_stop"]


def test_ptz_pad_signals_standalone(qtbot):
    pad = PtzPad()
    qtbot.addWidget(pad)
    pad.show()
    qtbot.waitExposed(pad)
    assert pad.objectName() == "ptzPad"
    left = pad.findChild(QAbstractButton, "ptzLeft")
    with qtbot.waitSignal(pad.move_requested, timeout=1000) as blocker:
        qtbot.mousePress(left, Qt.MouseButton.LeftButton)
    dx, dy = blocker.args
    assert dx < 0 and dy == 0
    with qtbot.waitSignal(pad.stop_requested, timeout=1000):
        qtbot.mouseRelease(left, Qt.MouseButton.LeftButton)
