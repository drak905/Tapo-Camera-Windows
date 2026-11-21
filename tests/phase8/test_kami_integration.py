"""Integration step: Kami sign-in, camera list and tiles wired into the app, and proof that Tapo is unchanged.

Everything runs against mocks (``tests/fixtures/mock_kami_cloud.py``, fake Tapo services). No real Kami
server, camera or account. Red tests first: the wiring does not exist when this file is committed.
"""
from __future__ import annotations

import importlib.util
import os
import pathlib
import subprocess
import sys

os.environ.setdefault("QT_QPA_PLATFORM", "offscreen")

import pytest  # noqa: E402
from PyQt6.QtCore import QObject, Qt, QTimer, pyqtSignal  # noqa: E402
from PyQt6.QtWidgets import QApplication, QGroupBox, QLineEdit, QPushButton  # noqa: E402

from tapo_desktop.models import CameraConfig, CredentialKind, Credentials, StreamQuality  # noqa: E402
from tapo_desktop.services.config_store import (  # noqa: E402
    SETTING_KAMI_LAST_SIGN_IN_FAILED,
    ConfigStore,
)
from tapo_desktop.services.kami_camera_service import KamiCameraService  # noqa: E402
from tapo_desktop.services.stream_policy import plan_streams  # noqa: E402
from tapo_desktop.ui import kami_status  # noqa: E402
from tests.e2e.fakes import FakeServiceFactory, FakeVault  # noqa: E402
from tests.fixtures.mock_kami_cloud import make_device  # noqa: E402

REPO = pathlib.Path(__file__).resolve().parents[2]
BASELINE = "b11c08e"
UNSUPPORTED = "Live view is not supported for this Kami camera"


def kami_cfg(uid: str = "u1", name: str = "Kami Cam A", online: bool = True) -> CameraConfig:
    return CameraConfig(name=name, host="", id=f"kami:{uid}", source="kami", device_id=uid, model="1",
                        lan_found=False, online=online, enable_onvif_events=False,
                        vendor_data={"uid": uid, "region": "eu", "shared": False})


def lan_cfg(i: int) -> CameraConfig:
    return CameraConfig(name=f"Hall {i}", host=f"192.0.2.{i + 10}", id=f"lan{i}", model="C200",
                        enable_onvif_events=False)


class Env:
    """Controller with fake Tapo services and the real (stub) Kami service; spies on the controller."""

    def __init__(self, tmp_path, cams):
        from tapo_desktop.services.controller import AppController

        self.store = ConfigStore(tmp_path / "config.json")
        self.store.save(cams)
        self.vault = FakeVault()
        for c in cams:
            if c.source != "kami":
                self.vault.set(c.id, CredentialKind.CLOUD, Credentials("admin", "pw"))
        self.tapo = FakeServiceFactory(frame_interval=0.25)
        self.built: list[str] = []
        self.calls: list[tuple] = []

        def factory(cfg):
            self.built.append(cfg.id)
            return KamiCameraService(cfg, self.vault) if cfg.source == "kami" else self.tapo(cfg)

        self.controller = AppController(self.store, self.vault, service_factory=factory)
        for name in ("start_view", "stop_view", "refresh_status", "load_presets", "load_recordings",
                     "ptz_move", "ptz_stop"):
            self._spy(name)

    def _spy(self, name: str) -> None:
        real = getattr(self.controller, name)

        def wrapper(*args, **kwargs):
            self.calls.append((name, *args))
            return real(*args, **kwargs)

        setattr(self.controller, name, wrapper)

    def called(self, name: str) -> list[tuple]:
        return [c for c in self.calls if c[0] == name]

    def close(self) -> None:
        self.controller.stop_all()
        self.tapo.shutdown()


@pytest.fixture
def make_env(tmp_path, qtbot):
    envs: list[Env] = []

    def _make(cams) -> Env:
        env = Env(tmp_path, list(cams))
        envs.append(env)
        return env

    yield _make
    for env in envs:
        env.close()


def make_window(qtbot, env: Env, **kw):
    from tapo_desktop.ui.main_window import MainWindow

    window = MainWindow(env.controller, tray_enabled=False, **kw)
    qtbot.addWidget(window)
    window.resize(1100, 750)
    window.show()
    qtbot.waitExposed(window)
    return window


def rows(window) -> list[tuple[str, str]]:
    lst = window.findChild(QObject, "cameraList")
    return [(lst.item(i).text(), lst.item(i).toolTip()) for i in range(lst.count())]


class FakeKamiManager(QObject):
    signed_in = pyqtSignal(str)
    sign_in_failed = pyqtSignal(str)
    signed_out = pyqtSignal()
    cameras_changed = pyqtSignal()

    def __init__(self, signed: bool = False):
        super().__init__()
        self.calls: list[str] = []
        self.signed = signed

    def is_signed_in(self):
        return self.signed

    def account_label(self):
        return "o***@example.com" if self.signed else ""

    def blocked_until(self):
        return None

    def sign_out(self):
        self.calls.append("sign_out")
        self.signed = False
        self.signed_out.emit()

    def restore_session(self):
        self.calls.append("restore_session")

    def sign_in(self, *a):
        self.calls.append("sign_in")


# =============================================================== Tapo unchanged (plan-review finding 12)
def test_plan_streams_for_a_tapo_only_config_matches_the_baseline_golden_values():
    cams = {c.id: c for c in (lan_cfg(0), lan_cfg(1), lan_cfg(2))}
    cams["lan1"].grid_quality = StreamQuality.SD
    cams["lan2"].fullscreen_quality = StreamQuality.HD
    visible = ["lan0", None, "lan1", "lan0", "ghost", "lan2"]
    plan = {k: v.as_args() for k, v in plan_streams(visible, cams).items()}
    assert plan == {"lan0": (cams["lan0"].grid_quality, 640, 360), "lan1": (StreamQuality.SD, 640, 360),
                    "lan2": (cams["lan2"].grid_quality, 640, 360)}
    full = {k: v.as_args() for k, v in plan_streams(visible, cams, "lan2").items()}
    assert full == {"lan2": (StreamQuality.HD, 1280, 720)}
    assert list(plan) == ["lan0", "lan1", "lan2"]  # slot order


def test_tapo_only_grid_and_list_golden(make_env, qtbot):
    env = make_env([lan_cfg(0), lan_cfg(1)])
    window = make_window(qtbot, env)
    assert rows(window) == [("Hall 0", "C200 - 192.0.2.10"), ("Hall 1", "C200 - 192.0.2.11")]
    qtbot.waitUntil(lambda: len(env.called("start_view")) == 2, timeout=3000)
    assert sorted(c[1] for c in env.called("start_view")) == ["lan0", "lan1"]
    assert {c[2:] for c in env.called("start_view")} == {(lan_cfg(0).grid_quality, 640, 360)}


def test_account_menu_keeps_its_tapo_entries_and_only_adds_kami_ones(make_env, qtbot):
    env = make_env([lan_cfg(0)])
    window = make_window(qtbot, env)
    menu = window.findChild(QObject, "accountMenu")
    texts = [a.text() for a in menu.actions()]
    assert texts[:4] == ["Sign in\u2026", "", "Refresh cameras", "Sign out"]
    assert [a.objectName() for a in menu.actions()[4:] if a.objectName()] == ["kamiSignInAction",
                                                                              "kamiSignOutAction"]
    assert window.findChild(QObject, "accountButton").text() == "Sign in"
    sign_in = next(a for a in menu.actions() if a.objectName() == "kamiSignInAction")
    assert not sign_in.isEnabled()  # no Kami manager: disabled, like the Tapo entries without accounts


def test_a_kami_camera_in_the_config_changes_no_tapo_tile_or_stream(make_env, qtbot):
    env = make_env([lan_cfg(0), kami_cfg(), lan_cfg(1)])
    window = make_window(qtbot, env)
    qtbot.waitUntil(lambda: len(env.called("start_view")) >= 2, timeout=3000)
    qtbot.wait(200)
    started = sorted(c[1] for c in env.called("start_view"))
    assert started == ["lan0", "lan1"]
    assert env.called("stop_view") == []
    assert "kami:u1" not in env.built  # no service is even built for the Kami camera by the grid
    tapo_rows = [r for r in rows(window) if r[0].startswith("Hall")]
    assert tapo_rows == [("Hall 0", "C200 - 192.0.2.10"), ("Hall 1", "C200 - 192.0.2.11")]


# =============================================================== tile text and no stream (finding 8)
def test_video_tile_headline_for_the_unsupported_reason():
    from tapo_desktop.ui.video_tile import REASON_HEADLINES

    assert REASON_HEADLINES[kami_status.LIVE_VIEW_UNSUPPORTED_TEXT] == UNSUPPORTED
    assert REASON_HEADLINES["Remote - click to view"] == "Remote - click to view"  # Tapo entry untouched


def test_kami_tile_shows_the_fixed_text_and_never_requests_a_stream(make_env, qtbot):
    env = make_env([kami_cfg(), lan_cfg(0)])
    window = make_window(qtbot, env)
    window.set_grid_size(3)
    grid = window.findChild(QObject, "cameraGrid")
    tile = grid.tile("kami:u1")
    assert tile.status_text() == UNSUPPORTED
    assert not tile.waiting_for_click()
    # click, double click (fullscreen), select, menu "Full screen": still no stream request
    grid.select_camera("kami:u1")
    grid.toggle_fullscreen("kami:u1")
    grid.toggle_fullscreen("kami:u1")
    qtbot.wait(200)
    assert [c for c in env.called("start_view") if c[1] == "kami:u1"] == []
    assert tile.status_text() == UNSUPPORTED
    assert "kami:u1" not in env.built


def test_a_kami_camera_added_later_gets_the_text_without_a_stream(make_env, qtbot):
    env = make_env([lan_cfg(0)])
    window = make_window(qtbot, env)
    env.controller.upsert_cameras([kami_cfg("late")], cloud=None)
    grid = window.findChild(QObject, "cameraGrid")
    qtbot.waitUntil(lambda: grid.tile("kami:late") is not None, timeout=3000)
    assert grid.tile("kami:late").status_text() == UNSUPPORTED
    assert [c for c in env.called("start_view") if c[1] == "kami:late"] == []


def test_capability_service_for_kami_only():
    assert kami_status.capability_service(kami_cfg()) is KamiCameraService
    assert kami_status.capability_service(lan_cfg(0)) is None
    assert kami_status.control_enabled(kami_status.capability_service(lan_cfg(0)), "ptz") is True
    assert kami_status.control_enabled(kami_status.capability_service(kami_cfg()), "live_view") is False


# =============================================================== camera list (finding 7 UI parts)
def test_kami_rows_have_their_own_tooltip_and_status(make_env, qtbot):
    env = make_env([kami_cfg("a", "Kami Cam A", True), kami_cfg("b", "Kami Cam B", False)])
    window = make_window(qtbot, env)
    got = dict(rows(window))
    on_text, off_text = "Kami Cam A", "Kami Cam B (Offline)"
    assert on_text in got and off_text in got
    assert got[on_text].splitlines()[0].endswith(kami_status.LIST_TOOLTIP + ", Online")
    assert got[off_text].splitlines()[0].endswith(kami_status.LIST_TOOLTIP + ", Offline")
    assert "Not on this network" not in " ".join(got) and "No address" not in " ".join(got.values())


def test_camera_row_status_of_tapo_is_unchanged():
    from tapo_desktop.ui.main_window import camera_row_status

    assert camera_row_status(lan_cfg(0)) == ""
    off = lan_cfg(1)
    off.online = False
    assert camera_row_status(off) == "Offline"
    cloud = CameraConfig(name="R", host="", id="R1", source="cloud", online=True, lan_found=False)
    assert camera_row_status(cloud) == "Not on this network"
    assert camera_row_status(cloud, remote=True) == "Remote"
    assert camera_row_status(kami_cfg(online=True)) == ""
    assert camera_row_status(kami_cfg(online=False)) == "Offline"


# =============================================================== controls and playback panels
def test_kami_selection_disables_controls_and_calls_no_service(make_env, qtbot):
    env = make_env([lan_cfg(0), kami_cfg()])
    window = make_window(qtbot, env)
    panel = window.findChild(QObject, "controlsPanel")
    playback = window.findChild(QObject, "playbackPanel")
    window._select("lan0", force=True)
    qtbot.wait(100)
    assert [c[1] for c in env.called("refresh_status")] == ["lan0"]
    assert all(g.isEnabled() for g in panel.findChildren(QGroupBox))
    assert [c[1] for c in env.called("load_recordings")] == ["lan0"]
    env.calls.clear()
    window._select("kami:u1", force=True)
    qtbot.wait(100)
    assert env.called("refresh_status") == [] and env.called("load_presets") == []
    assert env.called("load_recordings") == []
    assert not any(g.isEnabled() for g in panel.findChildren(QGroupBox))
    assert not playback.findChild(QObject, "refreshRecordingsButton").isEnabled()
    assert not playback.findChild(QObject, "playButton").isEnabled()
    assert not playback.findChild(QObject, "downloadButton").isEnabled()
    assert "not supported" in playback.findChild(QObject, "downloadStatus").text().lower()
    # back to a Tapo camera: everything enabled again
    window._select("lan0", force=True)
    qtbot.wait(100)
    assert all(g.isEnabled() for g in panel.findChildren(QGroupBox))
    assert playback.findChild(QObject, "refreshRecordingsButton").isEnabled()


def test_keyboard_ptz_is_ignored_for_a_kami_camera(make_env, qtbot):
    from PyQt6.QtGui import QKeyEvent

    env = make_env([kami_cfg(), lan_cfg(0)])
    window = make_window(qtbot, env)
    window._select("kami:u1", force=True)
    ev = QKeyEvent(QKeyEvent.Type.KeyPress, Qt.Key.Key_W, Qt.KeyboardModifier.NoModifier)
    window.keyPressEvent(ev)
    window.keyReleaseEvent(QKeyEvent(QKeyEvent.Type.KeyRelease, Qt.Key.Key_W, Qt.KeyboardModifier.NoModifier))
    assert env.called("ptz_move") == []
    window._select("lan0", force=True)
    window.keyPressEvent(QKeyEvent(QKeyEvent.Type.KeyPress, Qt.Key.Key_W, Qt.KeyboardModifier.NoModifier))
    assert [c[1] for c in env.called("ptz_move")] == ["lan0"]
    window.keyReleaseEvent(QKeyEvent(QKeyEvent.Type.KeyRelease, Qt.Key.Key_W, Qt.KeyboardModifier.NoModifier))


# =============================================================== main window: Kami account entries
def kami_actions(window):
    menu = window.findChild(QObject, "accountMenu")
    by = {a.objectName(): a for a in menu.actions() if a.objectName()}
    return by["kamiSignInAction"], by["kamiSignOutAction"]


def test_kami_actions_follow_the_signed_in_state(make_env, qtbot):
    env = make_env([lan_cfg(0)])
    manager = FakeKamiManager()
    window = make_window(qtbot, env, kami_accounts=manager)
    sign_in, sign_out = kami_actions(window)
    assert sign_in.text() == "Kami account\u2026"
    assert sign_out.text() == "Sign out of Kami"
    assert sign_in.isEnabled() and not sign_out.isEnabled()
    manager.signed = True
    manager.signed_in.emit("o***@example.com")
    assert not sign_in.isEnabled() and sign_out.isEnabled()
    sign_out.trigger()
    assert manager.calls == ["sign_out"]
    assert sign_in.isEnabled() and not sign_out.isEnabled()
    assert window.kami_accounts is manager


def test_sign_in_failed_text_reaches_the_status_bar_when_no_dialog_is_open(make_env, qtbot):
    env = make_env([lan_cfg(0)])
    manager = FakeKamiManager()
    window = make_window(qtbot, env, kami_accounts=manager)
    manager.sign_in_failed.emit("Your Kami session has ended. Sign in again.")
    assert "Your Kami session has ended" in window.statusBar().currentMessage()


def test_dialog_gets_the_manager_the_country_list_and_the_saved_country(make_env, qtbot, monkeypatch):
    from tapo_desktop.core.kami import regions
    from tapo_desktop.ui import main_window as mw

    env = make_env([lan_cfg(0)])
    env.store.set_setting(kami_status.SETTING_KAMI_LAST_COUNTRY, "VN")
    manager = FakeKamiManager()
    seen: dict = {}

    class FakeDialog(QObject):
        country_chosen = pyqtSignal(str)

        def __init__(self, mgr, countries=None, parent=None, *, last_country=None, **kw):
            super().__init__()
            seen.update(mgr=mgr, countries=countries, parent=parent, last=last_country)
            seen["dialog"] = self

        def exec(self):
            self.country_chosen.emit("DE")
            return 1

        def deleteLater(self):
            pass

    monkeypatch.setattr(mw.kami_sign_in_dialog, "KamiSignInDialog", FakeDialog)
    window = make_window(qtbot, env, kami_accounts=manager)
    kami_actions(window)[0].trigger()
    assert seen["mgr"] is manager and seen["parent"] is window
    assert seen["countries"] == regions.countries()
    assert seen["last"] == "VN"
    assert env.store.get_setting(kami_status.SETTING_KAMI_LAST_COUNTRY) == "DE"


def test_one_click_sends_one_sign_in_through_the_real_manager_and_dialog(kami_env, qtbot):
    from tapo_desktop.ui.main_window import MainWindow

    env = kami_env
    env.mock.add_account("pc@example.com", "pw-correct-2", userid="u-200")
    env.mock.devices = [make_device("cam1", "Porch"), make_device("cam2", "Garden", online=False)]
    window = MainWindow(env.controller, tray_enabled=False, kami_accounts=env.manager)
    qtbot.addWidget(window)
    window.show()
    driven = {"clicks": 0}

    def drive():
        dlg = QApplication.activeModalWidget()
        assert dlg is not None and dlg.objectName() == "kamiSignInDialog"
        dlg.findChild(QLineEdit, "emailEdit").setText("pc@example.com")
        dlg.findChild(QLineEdit, "passwordEdit").setText("pw-correct-2")
        dlg.findChild(QObject, "countryCombo").setCurrentText("Germany")
        button = dlg.findChild(QPushButton, "signInButton")
        button.click()
        driven["clicks"] += 1
        button.click()  # a second click while the call runs must send nothing

    QTimer.singleShot(300, drive)
    QTimer.singleShot(15000, lambda: QApplication.activeModalWidget() and QApplication.activeModalWidget().reject())
    assert window.show_kami_sign_in() is True
    env.idle(qtbot)
    assert env.mock.login_count == 1
    assert env.store.get_setting(kami_status.SETTING_KAMI_LAST_COUNTRY) == "DE"
    assert sorted(env.kami_ids()) == ["kami:cam1", "kami:cam2"]
    names = [r[0] for r in rows(window)]
    assert "Porch" in names and "Garden (Offline)" in names
    grid = window.findChild(QObject, "cameraGrid")
    assert grid.tile("kami:cam1").status_text() == UNSUPPORTED
    assert not kami_actions(window)[0].isEnabled() and kami_actions(window)[1].isEnabled()


# =============================================================== app wiring
def test_build_creates_the_kami_manager_beside_the_tapo_one(tmp_path, monkeypatch, qtbot):
    from tapo_desktop import app as app_mod
    from tapo_desktop.services.kami_account import KamiAccountManager

    monkeypatch.setenv("TAPO_DESKTOP_HOME", str(tmp_path))
    qapp = QApplication.instance()
    window, controller, accounts = app_mod._build(qapp, ConfigStore(tmp_path / "config.json"), tmp_path,
                                                  interactive=False)
    try:
        assert isinstance(window.kami_accounts, KamiAccountManager)
        assert window.accounts is accounts
        assert kami_actions(window)[0].isEnabled()
    finally:
        window.deleteLater()
        controller.stop_all()


def test_start_account_restores_the_kami_session_once(tmp_path):
    from tapo_desktop import app as app_mod

    class W:
        kami_accounts = FakeKamiManager()

    class TapoAccounts:
        config_store = ConfigStore(tmp_path / "c.json")

        def restore_session(self):
            return True

    app_mod._start_account(W, TapoAccounts())
    assert W.kami_accounts.calls == ["restore_session"]


def test_a_broken_kami_restore_does_not_stop_the_app(tmp_path):
    from tapo_desktop import app as app_mod

    class Boom(FakeKamiManager):
        def restore_session(self):
            raise RuntimeError("vault broken")

    class W:
        kami_accounts = Boom()

    class TapoAccounts:
        config_store = ConfigStore(tmp_path / "c.json")
        called = 0

        def restore_session(self):
            TapoAccounts.called += 1
            return True

    app_mod._start_account(W, TapoAccounts())
    assert TapoAccounts.called == 1


def test_restore_after_a_failed_attempt_sends_nothing(kami_env, qtbot):
    from tapo_desktop import app as app_mod
    from tapo_desktop.models import KAMI_ACCOUNT_VAULT_ID

    env = kami_env
    env.vault.set(KAMI_ACCOUNT_VAULT_ID, CredentialKind.KAMI_ACCOUNT, Credentials(
        "pc@example.com",
        '{"userid": "u-200", "token": "t", "token_secret": "s", "region": "eu", "country": "DE"}'))
    env.store.set_setting(SETTING_KAMI_LAST_SIGN_IN_FAILED, True)

    class W:
        kami_accounts = env.manager

    class TapoAccounts:
        config_store = env.store

        def restore_session(self):
            return True

    app_mod._start_account(W, TapoAccounts())
    env.idle(qtbot)
    assert env.mock.login_count == 0 and env.mock.list_count == 0
    assert env.factory_calls == []


def test_selftest_with_a_kami_camera_in_the_config(tmp_path, monkeypatch):
    from tapo_desktop import app as app_mod

    monkeypatch.setenv("TAPO_DESKTOP_HOME", str(tmp_path))
    ConfigStore(tmp_path / "config.json").save([kami_cfg(), lan_cfg(0)])
    assert app_mod.main(["--selftest"]) == 0


# =============================================================== release audit and .gitignore
def load_audit():
    spec = importlib.util.spec_from_file_location("audit_release_t", REPO / "tools" / "audit_release.py")
    module = importlib.util.module_from_spec(spec)
    sys.modules[spec.name] = module
    spec.loader.exec_module(module)
    return module


@pytest.mark.parametrize("name", ["Kami_Home.apkm", "sub/x.APKM", "kami_app_key.txt"])
def test_audit_forbids_kami_file_names(tmp_path, name):
    audit = load_audit()
    path = tmp_path / name
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_bytes(b"x")
    assert not audit.audit(tmp_path)["passed"]


@pytest.mark.parametrize("pattern", [b"com.yitechnology.x", b"import com.xiaoyi.y", b"libs/com.tutk.IOTC",
                                     b"com.p2p.pppp_api.Z", b"com.tnp.sdk"])
def test_audit_forbids_kami_package_bytes(tmp_path, pattern):
    audit = load_audit()
    (tmp_path / "mod.pyc").write_bytes(b"\x00\x01" + pattern + b"\x02")
    report = audit.audit(tmp_path)
    assert not report["passed"]


def test_audit_catches_the_vendor_key_without_printing_it(tmp_path, monkeypatch):
    audit = load_audit()
    key = "k3y-FOR-TEST-ONLY-0123456789ab"
    keyfile = tmp_path / "key.txt"
    keyfile.write_text(key + "\n", encoding="utf-8")
    monkeypatch.setenv("KAMI_APP_KEY_FILE", str(keyfile))
    release = tmp_path / "rel"
    release.mkdir()
    (release / "data.bin").write_bytes(b"abc" + key.encode() + b"def")
    report = audit.audit(release)
    assert not report["passed"]
    assert key not in " ".join(report["problems"])
    (release / "data.bin").write_bytes(b"clean")
    assert audit.audit(release)["passed"]


def test_audit_still_passes_a_clean_release(tmp_path):
    audit = load_audit()
    (tmp_path / "TapoDesktop.exe").write_bytes(b"MZ nothing special here")
    assert audit.audit(tmp_path)["passed"]


def test_gitignore_has_the_kami_lines():
    lines = (REPO / ".gitignore").read_text(encoding="utf-8").splitlines()
    assert "*.apkm" in lines and "kami_app_key.txt" in lines
    assert "artifacts/apk/" in lines  # old lines kept


# =============================================================== byte-identical Tapo core
PROTECTED = ["src/tapo_desktop/core/control_client", "src/tapo_desktop/core/cloud",
             "src/tapo_desktop/core/media_engine", "src/tapo_desktop/core/vod",
             "src/tapo_desktop/core/auth/crypto.py", "src/tapo_desktop/services/account.py",
             "src/tapo_desktop/services/remote.py", "src/tapo_desktop/services/stream_policy.py"]


def test_protected_tapo_files_are_byte_identical_to_the_baseline():
    try:
        have = subprocess.run(["git", "cat-file", "-e", f"{BASELINE}^{{commit}}"], cwd=REPO,
                              capture_output=True).returncode == 0
    except OSError:
        have = False
    if not have:
        pytest.skip("baseline commit not available")
    diff = subprocess.run(["git", "diff", "--stat", BASELINE, "--", *PROTECTED], cwd=REPO,
                          capture_output=True, text=True)
    assert diff.stdout.strip() == "", diff.stdout


def test_new_text_files_are_lf_and_have_no_bom():
    for rel in ("tests/phase8/test_kami_integration.py", "tools/audit_release.py",
                "src/tapo_desktop/app.py", "src/tapo_desktop/ui/main_window.py", "src/tapo_desktop/ui/grid.py",
                "src/tapo_desktop/ui/kami_status.py", "src/tapo_desktop/ui/controls_panel.py",
                "src/tapo_desktop/ui/playback_panel.py", "src/tapo_desktop/ui/video_tile.py"):
        data = (REPO / rel).read_bytes()
        assert not data.startswith(b"\xef\xbb\xbf"), rel
        assert b"\r\n" not in data, rel
