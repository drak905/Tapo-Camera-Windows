"""AccountManager + AppController: per-camera local access tokens (vault ``LOCAL_TOKEN``).

After sign-in / restore / refresh the manager calls ``local_access_tokens()``; a failure must not
break sign-in. Tokens are stored as ``("admin", <LAT>)``, rotated (service restarted), removed when
the cloud no longer returns one, kept when the fetch failed, and dropped on sign-out.
"""
from __future__ import annotations

import logging
import os

os.environ.setdefault("QT_QPA_PLATFORM", "offscreen")

import pytest  # noqa: E402

from tapo_desktop.core.errors import TapoConnectionError  # noqa: E402
from tapo_desktop.models import CredentialKind, Credentials, StreamQuality  # noqa: E402
from tests.phase2._app_helpers import app_env, sign_in  # noqa: E402,F401
from tests.phase2.app_fakes import (  # noqa: E402
    EMAIL, PASSWORD, CloudError, FakeCloud, FakeCloudClient, FakeDiscover, cloud_device, lan_for)

pytestmark = pytest.mark.ui

LAT0 = "LAT-zero-0123456789"
LAT0_NEW = "LAT-zero-rotated-9876"
LAT1 = "LAT-one-abcdef"


class LatCloudClient(FakeCloudClient):
    def local_access_tokens(self) -> dict[str, str]:
        cloud: LatCloud = self._cloud  # type: ignore[assignment]
        cloud.record("local_access_tokens", (), self._uuid)
        self._check_online()
        if self._token is None:
            raise CloudError("not signed in", -20651)
        if cloud.fail_tokens is not None:
            raise cloud.fail_tokens
        return dict(cloud.tokens)


class LatCloud(FakeCloud):
    def __init__(self, *a, tokens: dict[str, str] | None = None, **kw):
        super().__init__(*a, **kw)
        self.tokens: dict[str, str] = dict(tokens or {})
        self.fail_tokens: Exception | None = None

    def __call__(self, terminal_uuid: str) -> LatCloudClient:
        with self._lock:
            self.uuids.append(terminal_uuid)
            client = LatCloudClient(self, terminal_uuid)
            self.clients.append(client)
            return client


D0, D1 = cloud_device(0), cloud_device(1)


def _env(app_env, **kw):
    cloud = LatCloud(devices=[D0, D1], tokens=kw.pop("tokens", {D0.device_id: LAT0}))
    return app_env(cloud=cloud, lan=[lan_for(D0, "192.168.1.40"), lan_for(D1, "192.168.1.41")], **kw)


def _lat(env, dev) -> str | None:
    c = env.vault.get(dev.device_id, CredentialKind.LOCAL_TOKEN)
    return c.password if c is not None else None


def test_sign_in_stores_lat_per_camera(app_env, qtbot):
    env = _env(app_env)
    sign_in(env, qtbot, EMAIL, PASSWORD)
    assert env.vault.get(D0.device_id, CredentialKind.LOCAL_TOKEN) == Credentials("admin", LAT0)
    assert _lat(env, D1) is None
    assert env.vault.get(D0.device_id, CredentialKind.CLOUD) == Credentials("admin", PASSWORD)
    assert "local_access_tokens" in env.cloud.call_names()
    assert env.controller.uses_local_token(D0.device_id) is True
    assert env.controller.uses_local_token(D1.device_id) is False
    assert env.controller.uses_local_token("unknown") is False


def test_refresh_rotates_and_removes_lat_and_restarts_only_changed_cameras(app_env, qtbot):
    env = _env(app_env, tokens={D0.device_id: LAT0, D1.device_id: LAT1})
    sign_in(env, qtbot, EMAIL, PASSWORD)
    env.controller.start_view(D0.device_id, StreamQuality.SD, 320, 180)
    env.controller.start_view(D1.device_id, StreamQuality.SD, 320, 180)
    env.idle(qtbot)
    svc0, svc1 = env.factory.services[D0.device_id], env.factory.services[D1.device_id]

    env.cloud.tokens = {D0.device_id: LAT0_NEW}       # D0 rotated, D1 rolled back (no LAT)
    with qtbot.waitSignal(env.accounts.cameras_refreshed, timeout=5000):
        env.accounts.refresh_cameras()
    env.idle(qtbot)
    qtbot.waitUntil(lambda: svc0.closed and svc1.closed, timeout=5000)
    assert _lat(env, D0) == LAT0_NEW
    assert _lat(env, D1) is None
    assert env.vault.get(D1.device_id, CredentialKind.CLOUD) == Credentials("admin", PASSWORD)
    qtbot.waitUntil(lambda: env.factory.services[D0.device_id] is not svc0, timeout=5000)

    # unchanged tokens -> no restart
    svc0b = env.factory.services[D0.device_id]
    with qtbot.waitSignal(env.accounts.cameras_refreshed, timeout=5000):
        env.accounts.refresh_cameras()
    env.idle(qtbot)
    assert not svc0b.closed
    assert env.factory.services[D0.device_id] is svc0b


def test_token_fetch_failure_does_not_break_sign_in_and_keeps_cached_lat(app_env, qtbot, caplog):
    env = _env(app_env)
    sign_in(env, qtbot, EMAIL, PASSWORD)
    assert _lat(env, D0) == LAT0
    env.cloud.fail_tokens = CloudError("IoT cloud down", 500)
    caplog.set_level(logging.WARNING)
    with qtbot.waitSignal(env.accounts.cameras_refreshed, timeout=5000):
        env.accounts.refresh_cameras()
    env.idle(qtbot)
    assert _lat(env, D0) == LAT0                       # unknown is not "no token"
    assert any("local access token" in r.getMessage() for r in caplog.records if r.levelno >= logging.WARNING)
    assert LAT0 not in caplog.text

    env2 = _env(app_env, store_path=env.store._path.parent / "second.json")
    env2.cloud.fail_tokens = TapoConnectionError("down")
    sign_in(env2, qtbot, EMAIL, PASSWORD)              # still signs in
    assert [c.id for c in env2.controller.cameras()] == [D0.device_id, D1.device_id]
    assert _lat(env2, D0) is None


def test_client_without_token_api_still_signs_in(app_env, qtbot):
    env = app_env(devices=[D0], lan=[lan_for(D0, "192.168.1.40")])    # plain FakeCloud: no LAT API
    sign_in(env, qtbot, EMAIL, PASSWORD)
    assert [c.id for c in env.controller.cameras()] == [D0.device_id]
    assert _lat(env, D0) is None


def test_sign_out_removes_lat(app_env, qtbot):
    env = _env(app_env)
    sign_in(env, qtbot, EMAIL, PASSWORD)
    assert _lat(env, D0) == LAT0
    env.accounts.sign_out()
    env.idle(qtbot)
    assert _lat(env, D0) is None
    assert env.vault.camera_ids() == []


def test_restore_session_refreshes_lat_and_offline_keeps_it(app_env, qtbot):
    from tapo_desktop.services.account import AccountManager
    from tapo_desktop.services.controller import AppController

    env = _env(app_env)
    sign_in(env, qtbot, EMAIL, PASSWORD)
    env.cloud.tokens = {D0.device_id: LAT0_NEW}
    controller = AppController(env.store, env.vault, service_factory=env.factory)
    manager = AccountManager(controller, cloud_factory=env.cloud, discover=env.discover)
    try:
        with qtbot.waitSignal(manager.signed_in, timeout=5000):
            assert manager.restore_session() is True
        manager.wait_idle(5000)
        assert _lat(env, D0) == LAT0_NEW
    finally:
        controller.stop_all()

    env.cloud.offline = True
    controller = AppController(env.store, env.vault, service_factory=env.factory)
    manager = AccountManager(controller, cloud_factory=env.cloud, discover=FakeDiscover([lan_for(D0, "192.168.1.40")]))
    try:
        with qtbot.waitSignal(manager.signed_in, timeout=5000):
            assert manager.restore_session() is True
        manager.wait_idle(5000)
        assert _lat(env, D0) == LAT0_NEW
    finally:
        controller.stop_all()


def test_lat_kind_delete_keeps_other_kinds(tmp_path):
    from tapo_desktop.core.auth.vault import CredentialVault

    v = CredentialVault(tmp_path / "vault.json")
    v.set("cam", CredentialKind.CLOUD, Credentials("admin", "pw"))
    v.set("cam", CredentialKind.LOCAL_TOKEN, Credentials("admin", LAT0))
    assert v.get("cam", CredentialKind.LOCAL_TOKEN) == Credentials("admin", LAT0)
    assert LAT0 not in (tmp_path / "vault.json").read_text()
    v.delete_kind("cam", CredentialKind.LOCAL_TOKEN)
    v.delete_kind("cam", CredentialKind.LOCAL_TOKEN)     # idempotent
    v.delete_kind("other", CredentialKind.CLOUD)
    assert v.get("cam", CredentialKind.LOCAL_TOKEN) is None
    assert v.get("cam", CredentialKind.CLOUD) == Credentials("admin", "pw")
    v.delete_kind("cam", CredentialKind.CLOUD)
    assert v.camera_ids() == []


def test_camera_row_tooltip_says_whether_a_lat_is_used(app_env, qtbot):
    from PyQt6.QtCore import Qt
    from PyQt6.QtWidgets import QListWidget

    from tests.phase2._app_helpers import show_main_window

    env = _env(app_env)
    sign_in(env, qtbot, EMAIL, PASSWORD)
    w = show_main_window(env, qtbot)
    lst = w.findChild(QListWidget, "cameraList")
    tips = {lst.item(i).data(Qt.ItemDataRole.UserRole): lst.item(i).toolTip() for i in range(lst.count())}
    assert "using local access token: yes" in tips[D0.device_id]
    assert "using local access token: no" in tips[D1.device_id]
    assert all(LAT0 not in t for t in tips.values())
