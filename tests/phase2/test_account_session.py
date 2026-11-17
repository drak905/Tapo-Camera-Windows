"""AccountManager: restore on start (online/offline), refresh, sign out."""
from __future__ import annotations

import os

os.environ.setdefault("QT_QPA_PLATFORM", "offscreen")

import pytest  # noqa: E402

from tapo_desktop.core.errors import TapoConnectionError  # noqa: E402
from tapo_desktop.models import ACCOUNT_VAULT_ID, CredentialKind, Credentials, StreamQuality  # noqa: E402
from tests.phase2._app_helpers import app_env, sign_in  # noqa: E402,F401
from tests.phase2.app_fakes import EMAIL, PASSWORD, FakeDiscover, cloud_device, lan_for  # noqa: E402

pytestmark = pytest.mark.ui


def _restart(env, qtbot, *, cloud=None, discover=None):
    """Simulate the next app start: a fresh controller + manager on the same store and vault."""
    from tapo_desktop.services.account import AccountManager
    from tapo_desktop.services.controller import AppController

    controller = AppController(env.store, env.vault, service_factory=env.factory)
    manager = AccountManager(controller, cloud_factory=cloud or env.cloud, discover=discover or env.discover)
    return controller, manager


def test_restore_session_without_account_returns_false(app_env, qtbot):
    env = app_env(1)
    assert env.accounts.restore_session() is False
    env.idle(qtbot)
    assert env.cloud.calls == []
    assert env.ids() == ["cam0"]
    assert env.accounts.account() is None


def test_restore_session_signs_in_silently(app_env, qtbot):
    dev = cloud_device(0)
    env = app_env(devices=[dev], lan=[lan_for(dev, "192.168.1.40")])
    sign_in(env, qtbot, EMAIL, PASSWORD)
    env.cloud.calls.clear()
    env.discover.devices = [lan_for(dev, "192.168.1.41")]

    controller, manager = _restart(env, qtbot)
    try:
        assert [c.id for c in controller.cameras()] == [dev.device_id]  # cached list shows at once
        with qtbot.waitSignal(manager.signed_in, timeout=5000) as blocker:
            assert manager.restore_session() is True
        manager.wait_idle(5000)
        assert blocker.args[0].email == EMAIL
        assert [(c.name, c.args) for c in env.cloud.calls][:1] == [("login", (EMAIL,))]
        assert "list_cameras" in env.cloud.call_names()
        assert controller.camera(dev.device_id).host == "192.168.1.41"
    finally:
        controller.stop_all()


def test_restore_offline_keeps_cached_cameras_and_runs_discovery(app_env, qtbot):
    d0, d1 = cloud_device(0), cloud_device(1)
    env = app_env(devices=[d0, d1], lan=[lan_for(d0, "192.168.1.50"), lan_for(d1, "192.168.1.51")])
    sign_in(env, qtbot, EMAIL, PASSWORD)
    env.cloud.offline = True
    discover = FakeDiscover([lan_for(d0, "192.168.1.60")])  # d1 not answering this time

    controller, manager = _restart(env, qtbot, discover=discover)
    refreshed = []
    manager.cameras_refreshed.connect(lambda t, f: refreshed.append((t, f)))
    try:
        with qtbot.waitSignal(manager.signed_in, timeout=5000) as blocker:
            assert manager.restore_session() is True
        manager.wait_idle(5000)
        controller.wait_idle(5000)
        qtbot.wait(20)
        assert blocker.args[0].email == EMAIL
        assert discover.calls >= 1
        assert [c.id for c in controller.cameras()] == [d0.device_id, d1.device_id]
        c0, c1 = controller.camera(d0.device_id), controller.camera(d1.device_id)
        assert (c0.host, c0.lan_found) == ("192.168.1.60", True)
        assert (c1.host, c1.lan_found) == ("", False)
        assert c1.name == "Cloud cam 1"
        assert refreshed[-1] == (2, 1)
        assert env.vault.get(ACCOUNT_VAULT_ID, CredentialKind.CLOUD) == Credentials(EMAIL, PASSWORD)
    finally:
        controller.stop_all()


def test_restore_with_changed_password_reports_failure_and_keeps_cameras(app_env, qtbot):
    dev = cloud_device(0)
    env = app_env(devices=[dev], lan=[lan_for(dev, "192.168.1.70")])
    sign_in(env, qtbot, EMAIL, PASSWORD)
    env.cloud.accounts[EMAIL] = "changed-elsewhere"
    controller, manager = _restart(env, qtbot)
    try:
        with qtbot.waitSignal(manager.sign_in_failed, timeout=5000) as blocker:
            assert manager.restore_session() is True
        manager.wait_idle(5000)
        assert blocker.args[0]
        assert manager.account() is None
        assert [c.id for c in controller.cameras()] == [dev.device_id]
    finally:
        controller.stop_all()


def test_refresh_updates_changed_ip_and_marks_missing(app_env, qtbot):
    d0, d1 = cloud_device(0), cloud_device(1)
    env = app_env(devices=[d0, d1], lan=[lan_for(d0, "192.168.1.80"), lan_for(d1, "192.168.1.81")])
    sign_in(env, qtbot, EMAIL, PASSWORD)
    env.discover.devices = [lan_for(d0, "192.168.1.99")]
    with qtbot.waitSignal(env.accounts.cameras_refreshed, timeout=5000) as blocker:
        env.accounts.refresh_cameras()
    env.idle(qtbot)
    assert blocker.args == [2, 1]
    c0, c1 = env.controller.camera(d0.device_id), env.controller.camera(d1.device_id)
    assert (c0.host, c0.lan_found) == ("192.168.1.99", True)
    assert (c1.host, c1.lan_found) == ("", False)


def test_refresh_adds_new_and_drops_removed_cloud_cameras(app_env, qtbot):
    d0, d1, d2 = cloud_device(0), cloud_device(1), cloud_device(2)
    env = app_env(1, devices=[d0, d1], lan=[lan_for(d0, "192.168.1.10")])
    sign_in(env, qtbot, EMAIL, PASSWORD)
    env.cloud.devices = [d0, d2]
    env.accounts.refresh_cameras()
    env.idle(qtbot)
    assert env.ids() == ["cam0", d0.device_id, d2.device_id]
    assert env.vault.get(d1.device_id, CredentialKind.CLOUD) is None
    assert env.vault.get(d2.device_id, CredentialKind.CLOUD) == Credentials("admin", PASSWORD)


def test_refresh_restarts_stream_when_ip_changes(app_env, qtbot):
    dev = cloud_device(0)
    env = app_env(devices=[dev], lan=[lan_for(dev, "192.168.1.11")])
    sign_in(env, qtbot, EMAIL, PASSWORD)
    env.controller.start_view(dev.device_id, StreamQuality.SD, 64, 36)
    qtbot.waitUntil(lambda: bool(dev.device_id in env.factory.services
                                 and env.factory.services[dev.device_id].frames_sent > 0), timeout=3000)
    first = env.factory.services[dev.device_id]
    env.discover.devices = [lan_for(dev, "192.168.1.12")]
    env.accounts.refresh_cameras()
    env.idle(qtbot)
    qtbot.waitUntil(lambda: bool(len(env.factory.created_for(dev.device_id)) == 2), timeout=3000)
    assert first.closed
    second = env.factory.services[dev.device_id]
    assert second.config.host == "192.168.1.12"
    qtbot.waitUntil(lambda: bool(second.frames_sent > 0), timeout=3000)


def test_sign_out_removes_account_and_cloud_cameras_only(app_env, qtbot):
    d0, d1 = cloud_device(0), cloud_device(1)
    env = app_env(1, devices=[d0, d1], lan=[lan_for(d0, "192.168.1.90"), lan_for(d1, "192.168.1.91")])
    sign_in(env, qtbot, EMAIL, PASSWORD)
    env.controller.start_view(d0.device_id, StreamQuality.SD, 64, 36)
    env.controller.start_view("cam0", StreamQuality.SD, 64, 36)
    qtbot.waitUntil(lambda: bool(d0.device_id in env.factory.services
                                 and env.factory.services[d0.device_id].frames_sent > 0), timeout=3000)
    cloud_svc = env.factory.services[d0.device_id]

    with qtbot.waitSignal(env.accounts.signed_out, timeout=5000):
        env.accounts.sign_out()
    env.idle(qtbot)
    assert env.ids() == ["cam0"]
    assert cloud_svc.calls_to("stop_stream") and cloud_svc.closed
    assert env.vault.get(ACCOUNT_VAULT_ID, CredentialKind.CLOUD) is None
    assert env.vault.camera_ids() == ["cam0"]
    assert env.store.get_setting("account", None) is None
    assert env.accounts.account() is None
    assert not env.factory.services["cam0"].closed  # the manual camera keeps streaming
    # a later restore finds nothing
    assert env.new_manager().restore_session() is False


def test_discovery_failure_is_not_fatal(app_env, qtbot):
    dev = cloud_device(0)
    env = app_env(devices=[dev])
    env.discover.error = OSError("no network interface")
    sign_in(env, qtbot, EMAIL, PASSWORD)
    cam = env.controller.camera(dev.device_id)
    assert (cam.host, cam.lan_found) == ("", False)


def test_cloud_list_failure_after_login_keeps_cached_cameras(app_env, qtbot):
    dev = cloud_device(0)
    env = app_env(devices=[dev], lan=[lan_for(dev, "192.168.1.15")])
    sign_in(env, qtbot, EMAIL, PASSWORD)
    env.cloud.fail_list = TapoConnectionError("timeout")
    env.discover.devices = [lan_for(dev, "192.168.1.16")]
    with qtbot.waitSignal(env.accounts.cameras_refreshed, timeout=5000):
        env.accounts.refresh_cameras()
    env.idle(qtbot)
    assert env.ids() == [dev.device_id]
    assert env.controller.camera(dev.device_id).host == "192.168.1.16"
