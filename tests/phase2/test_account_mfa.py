"""AccountManager two-step verification (MFA) flow."""
from __future__ import annotations

import os

os.environ.setdefault("QT_QPA_PLATFORM", "offscreen")

import pytest  # noqa: E402

from tapo_desktop.models import ACCOUNT_VAULT_ID, CredentialKind, Credentials  # noqa: E402
from tests.phase2._app_helpers import app_env  # noqa: E402,F401
from tests.phase2.app_fakes import EMAIL, MFA_CODE, PASSWORD, cloud_device, lan_for  # noqa: E402

pytestmark = pytest.mark.ui


def test_mfa_flow_request_code_wrong_code_then_success(app_env, qtbot):
    dev = cloud_device(0)
    env = app_env(devices=[dev], lan=[lan_for(dev, "192.168.1.30")])
    env.cloud.mfa_types = [2, 4]
    signed = []
    env.accounts.signed_in.connect(signed.append)

    with qtbot.waitSignal(env.accounts.mfa_required, timeout=5000) as blocker:
        env.accounts.sign_in(EMAIL, PASSWORD, True)
    env.idle(qtbot)
    assert list(blocker.args[0]) == [2, 4]
    assert signed == [] and env.accounts.account() is None
    assert env.vault.camera_ids() == [] and env.controller.cameras() == []

    env.accounts.request_mfa_code(2)
    env.idle(qtbot)
    assert ("request_mfa_code", (2,)) in [(c.name, c.args) for c in env.cloud.calls]

    with qtbot.waitSignal(env.accounts.sign_in_failed, timeout=5000) as failed:
        env.accounts.submit_mfa("000000", 2)
    env.idle(qtbot)
    assert failed.args[0] and "code" in failed.args[0].lower()
    assert signed == []

    with qtbot.waitSignal(env.accounts.signed_in, timeout=5000):
        env.accounts.submit_mfa(MFA_CODE, 2)
    env.idle(qtbot)
    assert env.accounts.account().email == EMAIL
    assert [c.id for c in env.controller.cameras()] == [dev.device_id]
    assert env.controller.camera(dev.device_id).host == "192.168.1.30"
    assert env.vault.get(ACCOUNT_VAULT_ID, CredentialKind.CLOUD) == Credentials(EMAIL, PASSWORD)
    assert env.vault.get(dev.device_id, CredentialKind.CLOUD) == Credentials("admin", PASSWORD)
    # the MFA calls ran on the same client (same MFA process) that started the login
    clients = {c.terminal_uuid for c in env.cloud.calls}
    assert len(clients) == 1


def test_submit_mfa_without_pending_login_fails_cleanly(app_env, qtbot):
    env = app_env()
    with qtbot.waitSignal(env.accounts.sign_in_failed, timeout=5000) as blocker:
        env.accounts.submit_mfa(MFA_CODE, 2)
    assert blocker.args[0]
    assert env.cloud.calls == []


def test_sign_out_during_mfa_discards_pending_login(app_env, qtbot):
    env = app_env()
    env.cloud.mfa_types = [3]
    with qtbot.waitSignal(env.accounts.mfa_required, timeout=5000):
        env.accounts.sign_in(EMAIL, PASSWORD, True)
    env.accounts.sign_out()
    with qtbot.waitSignal(env.accounts.sign_in_failed, timeout=5000):
        env.accounts.submit_mfa(MFA_CODE, 3)
    env.idle(qtbot)
    assert env.accounts.account() is None
    assert env.vault.camera_ids() == []
