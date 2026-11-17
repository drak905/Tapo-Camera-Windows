"""AccountManager sign-in: cameras from the cloud list, LAN match, vault + config contents."""
from __future__ import annotations

import os
import re
import sys

os.environ.setdefault("QT_QPA_PLATFORM", "offscreen")

import pytest  # noqa: E402

from tapo_desktop.models import ACCOUNT_VAULT_ID, AccountInfo, CredentialKind, Credentials  # noqa: E402
from tests.phase2._app_helpers import app_env, sign_in  # noqa: E402,F401
from tests.phase2.app_fakes import EMAIL, NICKNAME, PASSWORD, cloud_device, lan_for  # noqa: E402

pytestmark = pytest.mark.ui


def _three_devices():
    d0, d1, d2 = cloud_device(0), cloud_device(1, alias="Garage"), cloud_device(2, online=False)
    lan = [lan_for(d0, "192.168.1.20", http_port=4433), lan_for(d1, "192.168.1.21", by_mac_only=True)]
    return [d0, d1, d2], lan


def test_sign_in_success_upserts_cloud_cameras(app_env, qtbot):
    devices, lan = _three_devices()
    env = app_env(devices=devices, lan=lan)
    refreshed = []
    env.accounts.cameras_refreshed.connect(lambda total, found: refreshed.append((total, found)))
    with qtbot.waitSignal(env.accounts.signed_in, timeout=5000) as blocker:
        env.accounts.sign_in(EMAIL, PASSWORD, True)
    env.idle(qtbot)

    account = blocker.args[0]
    assert isinstance(account, AccountInfo) and account.email == EMAIL and account.nickname == NICKNAME
    assert env.accounts.account() == account
    cams = {c.id: c for c in env.controller.cameras()}
    assert set(cams) == {d.device_id for d in devices}
    c0, c1, c2 = (cams[d.device_id] for d in devices)
    assert (c0.name, c0.host, c0.lan_found, c0.online, c0.model, c0.source) == \
        ("Cloud cam 0", "192.168.1.20", True, True, "C210", "cloud")
    assert c0.device_id == devices[0].device_id and c0.https_port == 4433
    assert c0.mac == "A8-42-A1-00-00-00"
    assert (c1.name, c1.host, c1.lan_found, c1.https_port) == ("Garage", "192.168.1.21", True, 443)  # MAC fallback
    assert (c2.host, c2.lan_found, c2.online) == ("", False, False)
    for d in devices:
        assert env.vault.get(d.device_id, CredentialKind.CLOUD) == Credentials("admin", PASSWORD)
        assert env.vault.get(d.device_id, CredentialKind.CAMERA_ACCOUNT) is None
    assert env.vault.get(ACCOUNT_VAULT_ID, CredentialKind.CLOUD) == Credentials(EMAIL, PASSWORD)
    assert refreshed[-1] == (3, 2)
    assert env.discover.calls >= 1


def test_sign_in_persists_cameras_without_secrets(app_env, qtbot, tmp_path):
    devices, lan = _three_devices()
    env = app_env(devices=devices, lan=lan)
    sign_in(env, qtbot, EMAIL, PASSWORD)
    text = env.store.path.read_text(encoding="utf-8")
    assert PASSWORD not in text
    assert "tok-" not in text  # cloud token never persisted
    for d in devices:
        assert d.device_id in text
    assert env.store.get_setting("account", None)["email"] == EMAIL


@pytest.mark.skipif(sys.platform != "win32", reason="DPAPI vault is Windows-only")
def test_real_vault_never_contains_plaintext_password(app_env, qtbot, tmp_path):
    from tapo_desktop.core.auth.vault import CredentialVault

    devices, lan = _three_devices()
    vault = CredentialVault(tmp_path / "vault.json")
    env = app_env(devices=devices, lan=lan, vault=vault)
    sign_in(env, qtbot, EMAIL, PASSWORD)
    raw = (tmp_path / "vault.json").read_text(encoding="utf-8")
    assert PASSWORD not in raw and EMAIL not in raw
    assert vault.get(ACCOUNT_VAULT_ID, CredentialKind.CLOUD) == Credentials(EMAIL, PASSWORD)
    assert vault.get(devices[0].device_id, CredentialKind.CLOUD) == Credentials("admin", PASSWORD)


def test_wrong_password_reports_and_stores_nothing(app_env, qtbot):
    devices, lan = _three_devices()
    env = app_env(devices=devices, lan=lan)
    signed = []
    env.accounts.signed_in.connect(signed.append)
    with qtbot.waitSignal(env.accounts.sign_in_failed, timeout=5000) as blocker:
        env.accounts.sign_in(EMAIL, "wrong-password", True)
    env.idle(qtbot)
    message = blocker.args[0]
    assert message and "password" in message.lower()
    assert "wrong-password" not in message
    assert signed == [] and env.accounts.account() is None
    assert env.controller.cameras() == []
    assert env.vault.camera_ids() == []
    assert env.store.get_setting("account", None) is None
    assert "list_cameras" not in env.cloud.call_names()


def test_locked_account_message(app_env, qtbot):
    env = app_env()
    env.cloud.locked = True
    with qtbot.waitSignal(env.accounts.sign_in_failed, timeout=5000) as blocker:
        env.accounts.sign_in(EMAIL, PASSWORD, True)
    assert "locked" in blocker.args[0].lower()


def test_cloud_unreachable_message(app_env, qtbot):
    env = app_env()
    env.cloud.offline = True
    with qtbot.waitSignal(env.accounts.sign_in_failed, timeout=5000) as blocker:
        env.accounts.sign_in(EMAIL, PASSWORD, True)
    env.idle(qtbot)
    assert "internet" in blocker.args[0].lower() or "reach" in blocker.args[0].lower()
    assert env.vault.camera_ids() == []


def test_manual_cameras_are_kept_on_sign_in(app_env, qtbot):
    devices, lan = _three_devices()
    env = app_env(2, devices=devices, lan=lan)
    sign_in(env, qtbot, EMAIL, PASSWORD)
    ids = env.ids()
    assert ids[:2] == ["cam0", "cam1"]
    assert len(ids) == 5
    manual = env.controller.camera("cam0")
    assert manual.source == "manual" and manual.host == "192.0.2.10"


def test_remember_false_keeps_no_account_secret(app_env, qtbot):
    devices, lan = _three_devices()
    env = app_env(1, devices=devices, lan=lan)
    sign_in(env, qtbot, EMAIL, PASSWORD, remember=False)
    assert env.vault.get(ACCOUNT_VAULT_ID, CredentialKind.CLOUD) is None
    assert env.store.get_setting("account", None) is None
    assert len(env.ids()) == 4  # the session works: cameras are there with their credentials
    assert env.vault.get(devices[0].device_id, CredentialKind.CLOUD) == Credentials("admin", PASSWORD)
    # next start: nothing to restore -> the not-kept session's cloud cameras are dropped
    second = env.new_manager()
    assert second.restore_session() is False
    env.idle(qtbot)
    assert env.ids() == ["cam0"]
    assert env.vault.camera_ids() == ["cam0"]


def test_terminal_uuid_generated_once_and_persisted(app_env, qtbot):
    env = app_env()
    sign_in(env, qtbot, EMAIL, PASSWORD)
    uuid = env.store.get_setting("terminal_uuid", None)
    assert isinstance(uuid, str) and re.fullmatch(r"[0-9A-F]{32}", uuid)
    assert env.cloud.uuids and set(env.cloud.uuids) == {uuid}
    second = env.new_manager()
    with qtbot.waitSignal(second.signed_in, timeout=5000):
        second.sign_in(EMAIL, PASSWORD, True)
    second.wait_idle(5000)
    assert set(env.cloud.uuids) == {uuid}
    assert env.store.get_setting("terminal_uuid", None) == uuid
