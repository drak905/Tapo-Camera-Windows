"""Shared helpers for lane P2-app tests (the cloud lane owns tests/phase2/conftest.py).

``app_env`` is a pytest fixture factory: import it into a test module with
``from tests.phase2._app_helpers import app_env  # noqa: F401``.
"""
from __future__ import annotations

import os

os.environ.setdefault("QT_QPA_PLATFORM", "offscreen")

from dataclasses import dataclass, field  # noqa: E402

import pytest  # noqa: E402

from tapo_desktop.models import CameraConfig, CredentialKind  # noqa: E402
from tests.e2e.fakes import CAMERA, CLOUD, FakeServiceFactory, FakeVault, make_config  # noqa: E402
from tests.phase2.app_fakes import FakeCloud, FakeDiscover  # noqa: E402


@dataclass
class AppEnv:
    store: object
    vault: object
    factory: FakeServiceFactory
    controller: object
    accounts: object
    cloud: FakeCloud
    discover: FakeDiscover
    windows: list = field(default_factory=list)

    def new_manager(self, *, cloud: FakeCloud | None = None, discover: FakeDiscover | None = None):
        """A second AccountManager on the same controller (e.g. simulating a restart)."""
        from tapo_desktop.services.account import AccountManager

        return AccountManager(self.controller, cloud_factory=cloud or self.cloud, discover=discover or self.discover)

    def idle(self, qtbot, ms: int = 5000) -> None:
        assert self.accounts.wait_idle(ms), "account manager did not become idle"
        assert self.controller.wait_idle(ms), "controller did not become idle"
        qtbot.wait(20)

    def ids(self) -> list[str]:
        return [c.id for c in self.controller.cameras()]


@pytest.fixture
def app_env(tmp_path, qtbot):
    """Factory: ``app_env(manual=0, devices=[...], lan=[...], vault=None) -> AppEnv``."""
    from tapo_desktop.services.account import AccountManager
    from tapo_desktop.services.config_store import ConfigStore
    from tapo_desktop.services.controller import AppController

    envs: list[AppEnv] = []

    def _make(manual: int = 0, *, devices=None, lan=None, vault=None, cameras: list[CameraConfig] | None = None,
              store_path=None, cloud: FakeCloud | None = None, **service_kwargs) -> AppEnv:
        store = ConfigStore(store_path or tmp_path / "config.json")
        vault = vault if vault is not None else FakeVault()
        if cameras is None:
            cameras = [make_config(i) for i in range(manual)]
            store.save(cameras)
            for c in cameras:
                vault.set(c.id, CredentialKind.CLOUD, CLOUD)
                vault.set(c.id, CredentialKind.CAMERA_ACCOUNT, CAMERA)
        factory = FakeServiceFactory(**service_kwargs)
        controller = AppController(store, vault, service_factory=factory)
        cloud = cloud or FakeCloud(devices=list(devices or []))
        discover = FakeDiscover(list(lan or []))
        accounts = AccountManager(controller, cloud_factory=cloud, discover=discover)
        env = AppEnv(store=store, vault=vault, factory=factory, controller=controller, accounts=accounts,
                     cloud=cloud, discover=discover)
        envs.append(env)
        return env

    yield _make

    for env in envs:
        for w in env.windows:
            try:
                w.hide()
            except Exception:
                pass
        try:
            env.accounts.wait_idle(3000)
        except Exception:
            pass
        try:
            env.controller.stop_all()
        except Exception:
            pass
        env.factory.shutdown()


def show_main_window(env: AppEnv, qtbot, *, size=(1100, 750)):
    from tapo_desktop.ui.main_window import MainWindow

    w = MainWindow(env.controller, tray_enabled=False, accounts=env.accounts)
    qtbot.addWidget(w)
    env.windows.append(w)
    w.resize(*size)
    w.show()
    qtbot.waitExposed(w)
    return w


def sign_in(env: AppEnv, qtbot, email: str, password: str, remember: bool = True) -> None:
    """Sign in through the manager and wait until the result has been applied."""
    with qtbot.waitSignal(env.accounts.signed_in, timeout=5000):
        env.accounts.sign_in(email, password, remember)
    env.idle(qtbot)
