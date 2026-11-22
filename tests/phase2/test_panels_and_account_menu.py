"""MainWindow: hide/show of the camera list and the controls panel (View menu, shortcuts, saved choice),
and the account menu grouped in a Tapo section followed by a Kami section."""
from __future__ import annotations

import json
import os

os.environ.setdefault("QT_QPA_PLATFORM", "offscreen")

import pytest  # noqa: E402
from PyQt6.QtCore import QObject, Qt, pyqtSignal  # noqa: E402
from PyQt6.QtGui import QAction, QKeySequence  # noqa: E402
from PyQt6.QtWidgets import QDockWidget, QMenu, QToolButton  # noqa: E402

from tapo_desktop.services.config_store import ConfigStore  # noqa: E402
from tests.phase2._app_helpers import app_env, show_main_window, sign_in  # noqa: E402,F401
from tests.phase2.app_fakes import EMAIL, PASSWORD, cloud_device, lan_for  # noqa: E402

pytestmark = pytest.mark.ui

LIST_KEY = "show_camera_list"
CONTROLS_KEY = "show_controls_panel"


class FakeKami(QObject):
    """Same surface the main window uses from KamiAccountManager; never touches a network."""

    signed_in = pyqtSignal(str)
    sign_in_failed = pyqtSignal(str)
    signed_out = pyqtSignal()

    def __init__(self, signed: bool = False):
        super().__init__()
        self.signed = signed
        self.calls: list[str] = []

    def is_signed_in(self) -> bool:
        return self.signed

    def account_label(self) -> str:
        return "j***@example.com" if self.signed else ""

    def sign_out(self) -> None:
        self.calls.append("sign_out")
        self.signed = False
        self.signed_out.emit()


def _window(env, qtbot, **kw):
    from tapo_desktop.ui.main_window import MainWindow

    w = MainWindow(env.controller, tray_enabled=False, **kw)
    qtbot.addWidget(w)
    env.windows.append(w)
    w.resize(1100, 750)
    w.show()
    qtbot.waitExposed(w)
    return w


def _dock(w, name) -> QDockWidget:
    dock = w.findChild(QDockWidget, name)
    assert dock is not None
    return dock


def _action(w, name) -> QAction:
    action = w.findChild(QAction, name)
    assert action is not None, name
    return action


def _calls(env) -> dict[str, int]:
    names = ("start_stream", "stop_stream")
    return {n: sum(len(s.calls_to(n)) for s in env.factory.created) for n in names}


# ================================================================== side panels
def test_default_both_panels_shown_and_actions_checked(app_env, qtbot):
    env = app_env(1)
    w = _window(env, qtbot)
    assert w.camera_list_visible() and w.controls_panel_visible()
    assert _dock(w, "cameraDock").isVisible() and _dock(w, "sideDock").isVisible()
    assert _action(w, "toggleCameraListAction").isChecked()
    assert _action(w, "toggleControlsPanelAction").isChecked()


def test_view_menu_has_both_checkable_entries_and_distinct_shortcuts(app_env, qtbot):
    env = app_env(1)
    w = _window(env, qtbot)
    menu = w.menuBar().findChild(QMenu, "viewMenu")
    assert menu is not None and menu.title().replace("&", "") == "View"
    lst, ctl = _action(w, "toggleCameraListAction"), _action(w, "toggleControlsPanelAction")
    assert lst in menu.actions() and ctl in menu.actions()
    assert lst.isCheckable() and ctl.isCheckable()
    assert lst.shortcut() == QKeySequence("Ctrl+B") and ctl.shortcut() == QKeySequence("Ctrl+J")
    assert lst.shortcut() != ctl.shortcut()
    used = {a.shortcut().toString() for a in w.findChildren(QAction) if a not in (lst, ctl)}
    assert "Ctrl+B" not in used and "Ctrl+J" not in used


def test_each_panel_hides_and_shows_on_its_own_and_grid_gets_the_width(app_env, qtbot):
    env = app_env(1)
    w = _window(env, qtbot)
    grid = w.centralWidget()
    base = grid.width()
    _action(w, "toggleCameraListAction").trigger()
    qtbot.waitUntil(lambda: grid.width() > base, timeout=3000)
    assert not _dock(w, "cameraDock").isVisible() and _dock(w, "sideDock").isVisible()
    assert not w.camera_list_visible() and w.controls_panel_visible()
    one_hidden = grid.width()
    _action(w, "toggleControlsPanelAction").trigger()
    qtbot.waitUntil(lambda: grid.width() > one_hidden, timeout=3000)
    assert not _dock(w, "sideDock").isVisible() and not _dock(w, "cameraDock").isVisible()
    _action(w, "toggleCameraListAction").trigger()
    qtbot.waitUntil(lambda: _dock(w, "cameraDock").isVisible(), timeout=3000)
    assert not _dock(w, "sideDock").isVisible()
    _action(w, "toggleControlsPanelAction").trigger()
    qtbot.waitUntil(lambda: _dock(w, "sideDock").isVisible(), timeout=3000)
    assert _action(w, "toggleCameraListAction").isChecked() and _action(w, "toggleControlsPanelAction").isChecked()


def test_toggle_twice_is_back_to_shown(app_env, qtbot):
    env = app_env(1)
    w = _window(env, qtbot)
    for _ in range(2):
        _action(w, "toggleControlsPanelAction").trigger()
    assert w.controls_panel_visible() and _dock(w, "sideDock").isVisible()
    assert _action(w, "toggleControlsPanelAction").isChecked()


def test_public_setters_keep_actions_in_step(app_env, qtbot):
    env = app_env(1)
    w = _window(env, qtbot)
    w.set_camera_list_visible(False)
    w.set_controls_panel_visible(False)
    assert not _action(w, "toggleCameraListAction").isChecked()
    assert not _action(w, "toggleControlsPanelAction").isChecked()
    w.set_camera_list_visible(False)  # same value again: no change
    assert not w.camera_list_visible()
    w.set_camera_list_visible(True)
    assert _action(w, "toggleCameraListAction").isChecked() and _dock(w, "cameraDock").isVisible()


def test_shortcuts_toggle_when_the_window_is_active(app_env, qtbot):
    env = app_env(1)
    w = _window(env, qtbot)
    w.activateWindow()
    qtbot.keyClick(w, Qt.Key.Key_B, Qt.KeyboardModifier.ControlModifier)
    qtbot.keyClick(w, Qt.Key.Key_J, Qt.KeyboardModifier.ControlModifier)
    assert not w.camera_list_visible() and not w.controls_panel_visible()


def test_choice_is_saved_in_config_and_used_by_the_next_window(app_env, qtbot, tmp_path):
    env = app_env(1)
    w = _window(env, qtbot)
    w.set_camera_list_visible(False)
    saved = json.loads((tmp_path / "config.json").read_text(encoding="utf-8"))
    assert saved["settings"][LIST_KEY] is False and saved["settings"].get(CONTROLS_KEY, True) is True
    w.set_controls_panel_visible(False)
    w.hide()
    w2 = _window(env, qtbot)  # "restart": a new window reads the saved choice
    assert not w2.camera_list_visible() and not w2.controls_panel_visible()
    assert not _dock(w2, "cameraDock").isVisible() and not _dock(w2, "sideDock").isVisible()
    assert not _action(w2, "toggleCameraListAction").isChecked()
    assert ConfigStore(tmp_path / "config.json").get_setting(CONTROLS_KEY) is False
    w2.set_camera_list_visible(True)
    assert ConfigStore(tmp_path / "config.json").get_setting(LIST_KEY) is True


def test_old_config_without_the_keys_shows_both_and_writes_nothing(app_env, qtbot, tmp_path):
    env = app_env(1)
    before = (tmp_path / "config.json").read_text(encoding="utf-8")
    w = _window(env, qtbot)
    assert w.camera_list_visible() and w.controls_panel_visible()
    after = json.loads((tmp_path / "config.json").read_text(encoding="utf-8"))
    assert LIST_KEY not in after.get("settings", {}) and CONTROLS_KEY not in after.get("settings", {})
    assert json.loads(before).get("cameras") == after.get("cameras")


@pytest.mark.parametrize("junk", ["no", 0, None, [], {"a": 1}])
def test_a_damaged_value_means_shown(app_env, qtbot, junk):
    env = app_env(1)
    env.store.set_setting(LIST_KEY, junk)
    env.store.set_setting(CONTROLS_KEY, junk)
    w = _window(env, qtbot)
    assert w.camera_list_visible() and w.controls_panel_visible()


def test_toggling_during_a_running_stream_starts_and_stops_nothing(app_env, qtbot):
    env = app_env(2)
    w = _window(env, qtbot)
    qtbot.waitUntil(lambda: _calls(env)["start_stream"] >= 2, timeout=5000)
    env.idle(qtbot)
    starts, stops = [], []
    ctrl = env.controller
    real_start, real_stop = ctrl.start_view, ctrl.stop_view
    ctrl.start_view = lambda *a, **k: (starts.append(a), real_start(*a, **k))[1]
    ctrl.stop_view = lambda *a, **k: (stops.append(a), real_stop(*a, **k))[1]
    try:
        before = _calls(env)
        for name in ("toggleCameraListAction", "toggleControlsPanelAction", "toggleCameraListAction",
                     "toggleControlsPanelAction"):
            _action(w, name).trigger()
            qtbot.wait(50)
        w.set_camera_list_visible(False)
        w.set_controls_panel_visible(False)
        qtbot.wait(300)
        env.idle(qtbot)
        assert _calls(env) == before
        assert starts == [] and stops == []
    finally:
        ctrl.start_view, ctrl.stop_view = real_start, real_stop


def test_hidden_panels_do_not_break_selection_or_the_list(app_env, qtbot):
    env = app_env(3)
    w = _window(env, qtbot)
    w.set_camera_list_visible(False)
    w.set_controls_panel_visible(False)
    assert w.selected_camera_id() is not None
    w.set_grid_size(3)
    assert w.grid_size() == 3


# ================================================================== account menu
def _menu(w) -> QMenu:
    btn = w.findChild(QToolButton, "accountButton")
    assert btn is not None and btn.menu() is not None
    return btn.menu()


def _layout(menu: QMenu) -> list[tuple[str, str]]:
    """[(kind, text)] in menu order: kind is 'header', 'sep' or 'action'."""
    out = []
    for a in menu.actions():
        text = a.text().replace("&", "")
        if a.isSeparator():
            out.append(("header" if text else "sep", text))
        else:
            out.append(("action", text))
    return out


TAPO_ENTRIES = ["Sign in\u2026", "Refresh cameras", "Sign out"]
KAMI_ENTRIES = ["Kami account\u2026", "Sign out of Kami"]


def test_menu_is_a_tapo_section_then_a_kami_section(app_env, qtbot):
    env = app_env(1)
    w = _window(env, qtbot, accounts=env.accounts, kami_accounts=FakeKami())
    items = [i for i in _layout(_menu(w)) if i[0] != "sep"]
    assert items == [("header", "Tapo")] + [("action", t) for t in TAPO_ENTRIES] \
        + [("header", "Kami")] + [("action", t) for t in KAMI_ENTRIES]


def test_headers_are_not_clickable_actions(app_env, qtbot):
    env = app_env(1)
    w = _window(env, qtbot, accounts=env.accounts, kami_accounts=FakeKami())
    menu = _menu(w)
    headers = [a for a in menu.actions() if a.isSeparator() and a.text()]
    assert [a.text() for a in headers] == ["Tapo", "Kami"]
    # a section header is a separator: the menu never activates it, and it has no check box or shortcut
    assert all(a.isSeparator() and not a.isCheckable() and a.shortcut().isEmpty() for a in headers)
    # no real (clickable) action is called "Tapo" or "Kami"
    assert not [a for a in menu.actions() if not a.isSeparator() and a.text() in ("Tapo", "Kami")]


def test_kami_section_comes_after_every_tapo_entry(app_env, qtbot):
    env = app_env(1)
    w = _window(env, qtbot, accounts=env.accounts, kami_accounts=FakeKami())
    kinds = [t for k, t in _layout(_menu(w)) if k != "sep"]
    assert max(kinds.index(t) for t in TAPO_ENTRIES) < kinds.index("Kami") < min(kinds.index(t) for t in KAMI_ENTRIES)


def test_signed_out_state_each_section_offers_its_sign_in(app_env, qtbot):
    env = app_env(1)
    w = _window(env, qtbot, accounts=env.accounts, kami_accounts=FakeKami())
    acts = {a.text().replace("&", ""): a for a in _menu(w).actions() if not a.isSeparator()}
    assert acts["Sign in\u2026"].isEnabled() and acts["Kami account\u2026"].isEnabled()
    assert not acts["Sign out"].isEnabled() and not acts["Refresh cameras"].isEnabled()
    assert not acts["Sign out of Kami"].isEnabled()


def test_without_a_kami_manager_the_kami_section_is_still_there_but_inert(app_env, qtbot):
    env = app_env(1)
    w = _window(env, qtbot, accounts=env.accounts)
    items = [i for i in _layout(_menu(w)) if i[0] != "sep"]
    assert ("header", "Kami") in items
    acts = {a.text().replace("&", ""): a for a in _menu(w).actions() if not a.isSeparator()}
    assert not acts["Kami account\u2026"].isEnabled() and not acts["Sign out of Kami"].isEnabled()


def test_without_any_manager_both_headers_exist_and_nothing_is_enabled(app_env, qtbot):
    env = app_env(1)
    w = _window(env, qtbot)
    items = [i for i in _layout(_menu(w)) if i[0] != "sep"]
    assert [t for k, t in items if k == "header"] == ["Tapo", "Kami"]
    assert all(not a.isEnabled() for a in _menu(w).actions() if not a.isSeparator())


def test_tapo_signed_in_only_the_tapo_entries_change(app_env, qtbot):
    d0 = cloud_device(0)
    env = app_env(0, devices=[d0], lan=[lan_for(d0, "192.168.1.20")])
    w = _window(env, qtbot, accounts=env.accounts, kami_accounts=FakeKami())
    sign_in(env, qtbot, EMAIL, PASSWORD)
    acts = {a.text().replace("&", ""): a for a in _menu(w).actions() if not a.isSeparator()}
    assert not acts["Sign in\u2026"].isEnabled()
    assert acts["Sign out"].isEnabled() and acts["Refresh cameras"].isEnabled()
    assert acts["Kami account\u2026"].isEnabled() and not acts["Sign out of Kami"].isEnabled()


def test_kami_signed_in_only_the_kami_entries_change(app_env, qtbot):
    env = app_env(1)
    kami = FakeKami(signed=True)
    w = _window(env, qtbot, accounts=env.accounts, kami_accounts=kami)
    acts = {a.text().replace("&", ""): a for a in _menu(w).actions() if not a.isSeparator()}
    assert not acts["Kami account\u2026"].isEnabled() and acts["Sign out of Kami"].isEnabled()
    assert acts["Sign in\u2026"].isEnabled() and not acts["Sign out"].isEnabled()


def test_every_old_action_still_runs(app_env, qtbot, monkeypatch):
    d0 = cloud_device(0)
    env = app_env(0, devices=[d0], lan=[lan_for(d0, "192.168.1.20")])
    kami = FakeKami(signed=True)
    w = _window(env, qtbot, accounts=env.accounts, kami_accounts=kami)
    opened = []
    monkeypatch.setattr(w, "show_kami_sign_in", lambda: opened.append("kami") or False)
    monkeypatch.setattr(w, "show_sign_in", lambda *a, **k: opened.append("tapo") or False)
    acts = {a.text().replace("&", ""): a for a in _menu(w).actions() if not a.isSeparator()}
    kami.signed = False
    w._sync_kami()
    acts["Kami account\u2026"].trigger()
    acts["Sign in\u2026"].trigger()
    assert opened == ["kami", "tapo"]
    kami.signed = True
    w._sync_kami()
    acts["Sign out of Kami"].trigger()
    assert kami.calls == ["sign_out"]
    sign_in(env, qtbot, EMAIL, PASSWORD)
    n = env.discover.calls
    acts["Refresh cameras"].trigger()
    env.idle(qtbot)
    assert env.discover.calls == n + 1
    acts["Sign out"].trigger()
    env.idle(qtbot)
    assert w.accounts.account() is None


def test_object_names_of_the_actions_are_kept(app_env, qtbot):
    env = app_env(1)
    w = _window(env, qtbot, accounts=env.accounts, kami_accounts=FakeKami())
    assert w.findChild(QAction, "kamiSignInAction") is not None
    assert w.findChild(QAction, "kamiSignOutAction") is not None
    assert w.findChild(QMenu, "accountMenu") is not None
