"""The product is called "Tepo Desktop" wherever a user sees its name.

What must NOT change (saved sign-ins and settings live there): the data folder ``%APPDATA%\\TapoDesktop``,
``TAPO_DESKTOP_HOME``, the package ``tapo_desktop``, the single-instance prefix ``TapoDesktop-`` and the
installer AppId. Tests for those stay in tests/unit and tests/phase5 and are not touched.
"""
from __future__ import annotations

import os

os.environ.setdefault("QT_QPA_PLATFORM", "offscreen")

import importlib.util  # noqa: E402
import pathlib  # noqa: E402
import re  # noqa: E402

import pytest  # noqa: E402
from PyQt6.QtWidgets import QApplication, QDialog, QWidget  # noqa: E402

from tests.phase2._app_helpers import app_env, show_main_window, sign_in  # noqa: E402,F401
from tests.phase2.app_fakes import EMAIL, PASSWORD, FakeAccounts, cloud_device  # noqa: E402

REPO = pathlib.Path(__file__).resolve().parents[2]
NEW = "Tepo Desktop"
OLD_APP_ID = "AppId={{6C1E2F0A-4B7D-4E59-9C51-7A3D2B8E5F10}"


# ============================================================================ window and dialog titles
@pytest.mark.ui
def test_main_window_title(app_env, qtbot):
    w = show_main_window(app_env(1), qtbot)
    assert w.windowTitle() == NEW


@pytest.mark.ui
def test_tapo_sign_in_dialog_title(qtbot):
    from tapo_desktop.ui.sign_in_dialog import SignInDialog

    dlg = SignInDialog(FakeAccounts())
    qtbot.addWidget(dlg)
    assert dlg.windowTitle() == "Sign in - Tepo Desktop"


@pytest.mark.ui
def test_kami_sign_in_dialog_title(qtbot):
    from tapo_desktop.ui.kami_sign_in_dialog import KamiSignInDialog
    from tests.phase8.test_kami_ui import COUNTRIES, FakeKamiManager

    dlg = KamiSignInDialog(FakeKamiManager(), COUNTRIES)
    qtbot.addWidget(dlg)
    assert dlg.windowTitle() == "Sign in to Kami Home - Tepo Desktop"


@pytest.mark.ui
def test_imou_dialog_title(qtbot):
    from tapo_desktop.ui.imou_dialog import ImouConnectDialog
    from tests.phase9.test_imou_ui import FakeImouManager

    dlg = ImouConnectDialog(FakeImouManager())
    qtbot.addWidget(dlg)
    assert dlg.windowTitle() == "iMou (Open Platform) - Tepo Desktop"


@pytest.mark.ui
def test_tray_tooltip(app_env, qtbot):
    from tapo_desktop.ui.tray import TrayController

    window = QWidget()
    qtbot.addWidget(window)
    tray = TrayController(window, app_env(1).controller)
    assert tray._icon.toolTip() == NEW


@pytest.mark.ui
def test_snapshot_tooltip_names_the_new_folder(app_env, qtbot):
    from PyQt6.QtWidgets import QAbstractButton

    w = show_main_window(app_env(1), qtbot)
    button = w.findChild(QAbstractButton, "snapshotButton")
    assert button.toolTip().endswith("Pictures\\Tepo Desktop")


def test_snapshot_folder(tmp_path, monkeypatch):
    from tapo_desktop.ui.snapshot import snapshot_dir

    monkeypatch.setenv("USERPROFILE", str(tmp_path))
    assert snapshot_dir() == tmp_path / "Pictures" / NEW


# ============================================================================ application name
@pytest.fixture
def patched_main(qapp, tmp_path, monkeypatch):
    """app.main() in-process: no event loop, no network, no sign-in dialog; returns (app module, dialogs, home)."""
    from tapo_desktop import app
    from tapo_desktop.services import account
    from tapo_desktop.ui import _theme, sign_in_dialog

    home = tmp_path / "home"
    monkeypatch.setenv("TAPO_DESKTOP_HOME", str(home))
    monkeypatch.setattr(QApplication, "exec", lambda self=None: 0)
    monkeypatch.setattr(_theme, "apply_theme", lambda _app: None)
    monkeypatch.setattr(app, "_configure_logging", lambda *_a: None)
    dialogs: list = []

    class RecordingDialog:
        def __init__(self, accounts, parent=None):
            dialogs.append(self)

        def exec(self):
            return QDialog.DialogCode.Rejected

        def deleteLater(self):
            pass

    monkeypatch.setattr(sign_in_dialog, "SignInDialog", RecordingDialog)
    monkeypatch.setattr(account, "_default_discover", lambda: [])
    quit_flag = qapp.quitOnLastWindowClosed()
    before = set(QApplication.topLevelWidgets())
    yield app, dialogs, home
    for w in set(QApplication.topLevelWidgets()) - before:
        w.hide()
        w.deleteLater()
    qapp.setQuitOnLastWindowClosed(quit_flag)
    qapp.processEvents()


@pytest.mark.ui
def test_application_name(patched_main):
    app, _dialogs, _home = patched_main
    assert app.main(["--selftest"]) == 0
    assert QApplication.applicationName() == NEW


# ============================================================================ invariant: saved state still loads
@pytest.mark.ui
def test_existing_data_folder_still_loads_without_a_sign_in_prompt(patched_main, app_env, qtbot, monkeypatch):
    """A config + vault written under the (unchanged) data folder: same cameras, same account, no dialog,
    and the window that comes up carries the new name."""
    from tapo_desktop.core.auth.vault import CredentialVault
    from tapo_desktop.services import account
    from tapo_desktop.services.config_store import ConfigStore

    app, dialogs, home = patched_main
    home.mkdir(parents=True)
    dev = cloud_device(0)
    env = app_env(devices=[dev], store_path=home / "config.json", vault=CredentialVault(home / "vault.json"))
    sign_in(env, qtbot, EMAIL, PASSWORD)  # writes the account + cached camera list, as an old build did
    env.controller.stop_all()
    seen = []
    monkeypatch.setattr(account, "_default_cloud_factory", lambda terminal: env.cloud(terminal))
    original = account.AccountManager.restore_session
    monkeypatch.setattr(account.AccountManager, "restore_session",
                        lambda self: seen.append(original(self)) or seen[-1])
    assert app.main([]) == 0
    assert seen == [True]
    assert dialogs == []  # no sign-in prompt
    windows = [w for w in QApplication.topLevelWidgets() if w.windowTitle() == NEW]
    assert windows, "the main window with the new title was not created"
    assert ConfigStore(home / "config.json").get_setting("sign_in_skipped", False) is not True


# ============================================================================ server-visible names
def test_terminal_name():
    from tapo_desktop.core.cloud.client import TERMINAL_NAME

    assert TERMINAL_NAME == "Tepo Desktop (Windows PC)"


def test_kami_login_names():
    from tapo_desktop.core.kami import client as kami

    pairs = dict(kami.KamiClient("eu", "DE", app_key="not-a-real-key")._login_pairs("a@example.com", "pw"))
    assert pairs["dev_name"] == NEW
    assert kami.USER_AGENT == "TepoDesktop-Kami/0.1"


# ============================================================================ build and installer
def _load(path: pathlib.Path, name: str):
    spec = importlib.util.spec_from_file_location(name, path)
    mod = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(mod)
    return mod


def test_build_app_name():
    assert _load(REPO / "tools" / "build_windows.py", "_build_windows_for_test").APP == "TepoDesktop"


def _iss() -> str:
    return (REPO / "tools" / "installer.iss").read_text(encoding="utf-8")


def _section(name: str) -> list[str]:
    lines, inside = [], False
    for line in _iss().splitlines():
        if line.strip().startswith("["):
            inside = line.strip() == f"[{name}]"
            continue
        if inside and line.strip() and not line.strip().startswith(";"):
            lines.append(line.strip())
    return lines


def test_installer_keeps_the_app_id_so_it_is_an_upgrade():
    assert OLD_APP_ID in _section("Setup")


def test_installer_names():
    setup = _section("Setup")
    for line in ("AppName=Tepo Desktop", "AppPublisher=Tepo Desktop (unofficial)",
                 "DefaultDirName={localappdata}\\Programs\\Tepo Desktop", "DefaultGroupName=Tepo Desktop",
                 "OutputBaseFilename=TepoDesktop-{#AppVersion}-setup", "UninstallDisplayIcon={app}\\TepoDesktop.exe"):
        assert line in setup, line
    icons = "\n".join(_section("Icons"))
    assert '"{group}\\Tepo Desktop"' in icons and '"{group}\\Uninstall Tepo Desktop"' in icons
    assert '"{userdesktop}\\Tepo Desktop"' in icons
    assert "Launch Tepo Desktop" in "\n".join(_section("Run"))
    assert "TepoDesktop.exe" in "\n".join(_section("Run"))


def test_installer_upgraders_keep_their_install_folder_and_group():
    text = _iss()
    assert not re.search(r"^\s*UsePreviousAppDir\s*=\s*no", text, re.M | re.I)
    assert not re.search(r"^\s*UsePreviousGroup\s*=\s*no", text, re.M | re.I)


def test_installer_removes_the_old_exe_and_shortcuts():
    deletes = "\n".join(_section("InstallDelete"))
    for item in ("{app}\\TapoDesktop.exe", "{group}\\Tapo Desktop.lnk", "{group}\\Uninstall Tapo Desktop.lnk",
                 "{userdesktop}\\Tapo Desktop.lnk"):
        assert item in deletes, item


def test_installer_autostart_value_moves_to_the_new_name():
    registry = _section("Registry")
    new = [r for r in registry if 'ValueName: "TepoDesktop"' in r]
    old = [r for r in registry if 'ValueName: "TapoDesktop"' in r]
    assert len(new) == 1 and "Tasks: autostart" in new[0] and "uninsdeletevalue" in new[0]
    assert '{app}\\TepoDesktop.exe' in new[0]
    assert len(old) == 1 and "deletevalue" in old[0] and "Tasks:" not in old[0] and "ValueType: none" in old[0]


# ============================================================================ guard: the old name stays out of src/ and tools/
#: (file relative to the repo, text that may appear on a line): kept on purpose, see AGENTS.md section 1.
ALLOWED = [
    ("src/tapo_desktop/app.py", 'setOrganizationName("TapoDesktop")'),
    ("src/tapo_desktop/core/kami/app_key.py", "TapoDesktop"),
    ("src/tapo_desktop/services/config_store.py", "TapoDesktop"),
    ("src/tapo_desktop/ui/single_instance.py", '"TapoDesktop-"'),
    ("tools/diag_all_streams.py", '"TapoDesktop"'),
    ("tools/installer.iss", "{app}\\TapoDesktop.exe"),  # old files removed on upgrade
    ("tools/installer.iss", "Tapo Desktop.lnk"),
    ("tools/installer.iss", 'ValueName: "TapoDesktop"'),
    ("tools/installer.iss", "old name"),  # comments that explain the lines above
]
OLD_NAME = re.compile(r"Tapo Desktop|TapoDesktop")


def test_old_product_name_only_where_it_is_kept_on_purpose():
    bad = []
    for base in ("src", "tools"):
        for path in sorted((REPO / base).rglob("*")):
            if not path.is_file() or "egg-info" in path.parts or "__pycache__" in path.parts:
                continue
            if path.suffix not in {".py", ".iss", ".ps1", ".md", ".txt", ".json", ".toml", ".cmd", ".bat"}:
                continue
            rel = path.relative_to(REPO).as_posix()
            for number, line in enumerate(path.read_text(encoding="utf-8", errors="replace").splitlines(), 1):
                if OLD_NAME.search(line) and not any(rel == f and frag in line for f, frag in ALLOWED):
                    bad.append(f"{rel}:{number}: {line.strip()}")
    assert not bad, "\n".join(bad)
