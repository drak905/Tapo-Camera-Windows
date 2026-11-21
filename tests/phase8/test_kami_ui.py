"""Lane C: Kami sign-in dialog and camera status helpers, tested on a fake account manager.

No network, no real Kami account, no main window. The fake has exactly the interface lane A builds.
"""
from __future__ import annotations

import os

os.environ.setdefault("QT_QPA_PLATFORM", "offscreen")

import pytest  # noqa: E402
from PyQt6.QtCore import QObject, pyqtSignal  # noqa: E402
from PyQt6.QtWidgets import QLabel  # noqa: E402

from tapo_desktop.models import CameraConfig  # noqa: E402
from tapo_desktop.services.camera_service import CameraService  # noqa: E402
from tapo_desktop.services.kami_camera_service import KamiCameraService  # noqa: E402
from tapo_desktop.ui import kami_status  # noqa: E402
from tapo_desktop.ui.kami_sign_in_dialog import KamiSignInDialog  # noqa: E402

COUNTRIES = [("US", "United States"), ("VN", "Vietnam"), ("DE", "Germany")]
SECRET = "hunter2-not-a-real-password"


def _cfg(**kw):
    kw.setdefault("name", "Test camera")
    kw.setdefault("host", "")
    return CameraConfig(**kw)


class FakeKamiManager(QObject):
    """Same surface as lane A's KamiAccountManager; records calls, never touches a network."""

    signed_in = pyqtSignal(str)
    sign_in_failed = pyqtSignal(str)
    signed_out = pyqtSignal()
    cameras_changed = pyqtSignal()

    def __init__(self):
        super().__init__()
        self.calls: list[tuple] = []
        self.blocked: float | None = None
        self.signed = False

    def sign_in(self, email, password, country_code):
        self.calls.append((email, password, country_code))

    def sign_out(self):
        self.calls.append(("sign_out",))

    def restore_session(self):
        self.calls.append(("restore_session",))

    def is_signed_in(self) -> bool:
        return self.signed

    def account_label(self) -> str:
        return "j***@example.com" if self.signed else ""

    def blocked_until(self):
        return self.blocked

    def refresh(self):
        self.calls.append(("refresh",))


class Clock:
    def __init__(self, now=1000.0):
        self.now = now

    def __call__(self):
        return self.now


@pytest.fixture
def manager():
    return FakeKamiManager()


@pytest.fixture
def clock():
    return Clock()


def make_dialog(qtbot, manager, clock, **kw):
    dlg = KamiSignInDialog(manager, COUNTRIES, clock=clock, **kw)
    qtbot.addWidget(dlg)
    return dlg


def fill(dlg, email="owner@example.com", password=SECRET, country="Vietnam"):
    dlg.findChild(QObject, "emailEdit").setText(email)
    dlg.findChild(QObject, "passwordEdit").setText(password)
    dlg.findChild(QObject, "countryCombo").setCurrentText(country)


def button(dlg):
    return dlg.findChild(QObject, "signInButton")


# --------------------------------------------------------------------------- dialog: sign-in
def test_one_click_sends_exactly_one_sign_in(qtbot, manager, clock):
    dlg = make_dialog(qtbot, manager, clock)
    fill(dlg)
    button(dlg).click()
    assert manager.calls == [("owner@example.com", SECRET, "VN")]


def test_repeated_clicks_while_busy_send_nothing_more(qtbot, manager, clock):
    dlg = make_dialog(qtbot, manager, clock)
    fill(dlg)
    button(dlg).click()
    assert not button(dlg).isEnabled()
    button(dlg).click()
    dlg.findChild(QObject, "passwordEdit").returnPressed.emit()
    assert len(manager.calls) == 1


def test_button_enabled_again_after_failure_and_second_click_is_one_more_call(qtbot, manager, clock):
    dlg = make_dialog(qtbot, manager, clock)
    fill(dlg)
    button(dlg).click()
    manager.sign_in_failed.emit("Wrong e-mail or password.")
    assert button(dlg).isEnabled()
    fill(dlg)
    button(dlg).click()
    assert len(manager.calls) == 2


def test_failure_shows_text_and_clears_password(qtbot, manager, clock):
    dlg = make_dialog(qtbot, manager, clock)
    fill(dlg)
    button(dlg).click()
    manager.sign_in_failed.emit("Wrong e-mail or password.")
    err = dlg.findChild(QLabel, "errorLabel")
    assert err.isVisibleTo(dlg)
    assert err.text() == "Wrong e-mail or password."
    assert dlg.findChild(QObject, "passwordEdit").text() == ""
    assert dlg.findChild(QObject, "emailEdit").text() == "owner@example.com"


def test_success_closes_dialog_and_clears_password(qtbot, manager, clock):
    dlg = make_dialog(qtbot, manager, clock)
    dlg.show()
    fill(dlg)
    button(dlg).click()
    with qtbot.waitSignal(dlg.accepted, timeout=1000):
        manager.signed_in.emit("o***@example.com")
    assert not dlg.isVisible()
    assert dlg.findChild(QObject, "passwordEdit").text() == ""


def test_password_is_masked(qtbot, manager, clock):
    from PyQt6.QtWidgets import QLineEdit
    dlg = make_dialog(qtbot, manager, clock)
    assert dlg.findChild(QObject, "passwordEdit").echoMode() == QLineEdit.EchoMode.Password


def test_password_never_appears_in_any_label_or_log(qtbot, manager, clock, caplog):
    caplog.set_level(0)
    dlg = make_dialog(qtbot, manager, clock)
    fill(dlg)
    button(dlg).click()
    manager.sign_in_failed.emit("Wrong e-mail or password.")
    assert SECRET not in caplog.text
    assert all(SECRET not in lab.text() for lab in dlg.findChildren(QLabel))


def test_empty_fields_do_not_call_the_manager(qtbot, manager, clock):
    dlg = make_dialog(qtbot, manager, clock)
    button(dlg).click()
    fill(dlg, email="not-an-email")
    button(dlg).click()
    fill(dlg, password="")
    button(dlg).click()
    assert manager.calls == []
    assert dlg.findChild(QLabel, "errorLabel").isVisibleTo(dlg)
    assert button(dlg).isEnabled()


# --------------------------------------------------------------------------- dialog: blocked
def test_button_disabled_while_blocked_and_shows_seconds(qtbot, manager, clock):
    manager.blocked = clock.now + 42
    dlg = make_dialog(qtbot, manager, clock)
    assert not button(dlg).isEnabled()
    assert "42" in button(dlg).text()
    fill(dlg)
    button(dlg).click()
    assert manager.calls == []


def test_block_counts_down_and_button_returns_when_it_ends(qtbot, manager, clock):
    manager.blocked = clock.now + 10
    dlg = make_dialog(qtbot, manager, clock)
    clock.now += 4
    dlg.refresh_block_state()
    assert not button(dlg).isEnabled()
    assert "6" in button(dlg).text()
    clock.now += 7
    dlg.refresh_block_state()
    assert button(dlg).isEnabled()
    assert button(dlg).text() == "Sign in"


def test_block_in_the_past_means_enabled(qtbot, manager, clock):
    manager.blocked = clock.now - 5
    dlg = make_dialog(qtbot, manager, clock)
    assert button(dlg).isEnabled()


def test_failure_that_starts_a_block_disables_the_button(qtbot, manager, clock):
    dlg = make_dialog(qtbot, manager, clock)
    fill(dlg)
    button(dlg).click()
    manager.blocked = clock.now + 120
    manager.sign_in_failed.emit("Wrong e-mail or password.")
    assert not button(dlg).isEnabled()
    assert "120" in button(dlg).text()


# --------------------------------------------------------------------------- dialog: country and notes
def test_country_picker_is_searchable_and_lists_all_countries(qtbot, manager, clock):
    dlg = make_dialog(qtbot, manager, clock)
    combo = dlg.findChild(QObject, "countryCombo")
    assert combo.isEditable()
    assert [combo.itemText(i) for i in range(combo.count())] == [n for _c, n in COUNTRIES]
    assert combo.completer() is not None


def test_last_country_is_preselected_and_chosen_country_is_reported(qtbot, manager, clock):
    dlg = make_dialog(qtbot, manager, clock, last_country="DE")
    assert dlg.selected_country() == "DE"
    dlg.findChild(QObject, "emailEdit").setText("owner@example.com")
    dlg.findChild(QObject, "passwordEdit").setText(SECRET)
    dlg.findChild(QObject, "countryCombo").setCurrentText("vietnam")
    with qtbot.waitSignal(dlg.country_chosen, timeout=1000) as sig:
        button(dlg).click()
    assert sig.args == ["VN"]
    assert manager.calls[0][2] == "VN"


def test_unknown_country_text_is_refused_without_a_call(qtbot, manager, clock):
    dlg = make_dialog(qtbot, manager, clock)
    fill(dlg, country="Atlantis")
    assert dlg.selected_country() is None
    button(dlg).click()
    assert manager.calls == []
    assert dlg.findChild(QLabel, "errorLabel").isVisibleTo(dlg)


def test_default_countries_is_empty_and_never_calls_manager(qtbot, manager, clock):
    dlg = KamiSignInDialog(manager, clock=clock)
    qtbot.addWidget(dlg)
    assert dlg.findChild(QObject, "countryCombo").count() == 0
    fill(dlg, country="Vietnam")
    button(dlg).click()
    assert manager.calls == []


def test_notes_are_present_in_plain_language(qtbot, manager, clock):
    dlg = make_dialog(qtbot, manager, clock)
    text = " ".join(lab.text() for lab in dlg.findChildren(QLabel))
    assert "Unofficial. Kami cameras show in the list; live view is not supported yet." in text
    assert "sign you out of the Kami app" in text
    assert "Google or Apple" in text and "password" in text


# --------------------------------------------------------------------------- status helpers
TAPO = CameraService  # no supports_* flags on the base class


class _TapoLike:
    """A service without any capability flag, like TapoCameraService."""


def test_tapo_service_has_every_control_enabled():
    svc = _TapoLike()
    for control in kami_status.CONTROLS:
        assert kami_status.control_enabled(svc, control) is True


def test_real_tapo_base_class_has_no_flags():
    for control in kami_status.CONTROLS:
        assert kami_status.control_enabled(TAPO, control) is True


def test_kami_service_disables_live_view_ptz_playback_rename():
    svc = KamiCameraService(_cfg(id="kami:u1", name="Kami A", source="kami"))
    for control in ("live_view", "ptz", "playback", "rename", "controls", "events"):
        assert kami_status.control_enabled(svc, control) is False


def test_flipping_a_flag_enables_just_that_control():
    class Later(KamiCameraService):
        supports_live_view = True

    svc = Later(_cfg(id="kami:u1", name="Kami A", source="kami"))
    assert kami_status.control_enabled(svc, "live_view") is True
    assert kami_status.control_enabled(svc, "ptz") is False


def test_unknown_control_name_is_an_error():
    with pytest.raises(ValueError):
        kami_status.control_enabled(_TapoLike(), "teleport")


def test_overlay_text_is_the_fixed_plain_sentence():
    assert kami_status.LIVE_VIEW_UNSUPPORTED_TEXT == "Live view is not supported for this Kami camera"
    svc = KamiCameraService(_cfg(id="kami:u1", source="kami"))
    assert kami_status.live_view_overlay_text(svc) == kami_status.LIVE_VIEW_UNSUPPORTED_TEXT


def test_overlay_text_is_static_no_spinner_no_retry():
    text = kami_status.LIVE_VIEW_UNSUPPORTED_TEXT.lower()
    for bad in ("...", "\u2026", "connecting", "retry", "reconnect", "try again", "loading"):
        assert bad not in text
    svc = KamiCameraService(_cfg(id="kami:u1", source="kami"))
    assert kami_status.live_view_overlay_text(svc) == kami_status.live_view_overlay_text(svc)


def test_no_overlay_for_tapo_or_when_live_view_is_supported():
    assert kami_status.live_view_overlay_text(_TapoLike()) == ""

    class Later(KamiCameraService):
        supports_live_view = True

    assert kami_status.live_view_overlay_text(Later(_cfg(id="kami:u1", source="kami"))) == ""


def test_online_label():
    assert kami_status.online_label(True) == "Online"
    assert kami_status.online_label(False) == "Offline"


def test_list_tooltip_only_for_kami():
    assert kami_status.list_tooltip(_cfg(id="kami:u1", source="kami")) == "Kami (unofficial)"
    assert kami_status.list_tooltip(_cfg(id="abc", source="cloud")) == ""
    assert kami_status.list_tooltip(_cfg(id="192.168.1.5", source="manual")) == ""


def test_is_kami():
    assert kami_status.is_kami(_cfg(id="kami:u1", source="kami"))
    assert not kami_status.is_kami(_cfg(id="abc", source="cloud"))


def test_new_files_import_no_tapo_core_or_vendor_modules():
    import ast
    import pathlib

    import tapo_desktop.ui.kami_sign_in_dialog as d
    import tapo_desktop.ui.kami_status as s

    for mod in (d, s):
        tree = ast.parse(pathlib.Path(mod.__file__).read_text(encoding="utf-8"))
        names = []
        for node in ast.walk(tree):
            if isinstance(node, ast.Import):
                names += [a.name for a in node.names]
            elif isinstance(node, ast.ImportFrom):
                names.append(node.module or "")
        assert not [n for n in names if n.startswith(("tapo_desktop.core", "tapo_desktop.services.account"))], names
