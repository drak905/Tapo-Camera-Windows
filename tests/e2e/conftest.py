"""Fixtures for the services/UI e2e tests (owned by lane-tdd-ui; NOT the top-level conftest).

``make_env(...)`` builds ConfigStore (tmp file) + FakeVault + FakeServiceFactory + AppController.
Construction happens inside the test body, so unimplemented stubs make the test FAIL rather than
ERROR. Everything created is torn down even if the controller is not implemented yet.
"""
from __future__ import annotations

import os

os.environ.setdefault("QT_QPA_PLATFORM", "offscreen")

import threading  # noqa: E402
from dataclasses import dataclass, field  # noqa: E402

import pytest  # noqa: E402

from tapo_desktop.models import CameraConfig, CredentialKind  # noqa: E402
from tests.e2e.fakes import CAMERA, CLOUD, FakeCameraService, FakeServiceFactory, FakeVault, make_config  # noqa: E402


@dataclass
class UiEnv:
    store: object
    vault: FakeVault
    factory: FakeServiceFactory
    controller: object
    cameras: list[CameraConfig]
    gui_thread: int = field(default_factory=threading.get_ident)
    windows: list = field(default_factory=list)

    def svc(self, camera_id: str) -> FakeCameraService:
        return self.factory.services[camera_id]

    def has_svc(self, camera_id: str) -> bool:
        return camera_id in self.factory.services

    def idle(self, qtbot, ms: int = 5000) -> None:
        """Wait for controller background jobs, then let queued signals reach the GUI thread."""
        assert self.controller.wait_idle(ms), "controller did not become idle"
        qtbot.wait(30)


@pytest.fixture
def make_env(tmp_path, qtbot):
    from tapo_desktop.services.config_store import ConfigStore
    from tapo_desktop.services.controller import AppController

    factories: list[FakeServiceFactory] = []
    envs: list[UiEnv] = []

    def _make(n_cameras: int = 1, *, cameras: list[CameraConfig] | None = None,
              settings: dict | None = None, **service_kwargs) -> UiEnv:
        factory = FakeServiceFactory(**service_kwargs)
        factories.append(factory)
        store = ConfigStore(tmp_path / "config.json")
        cams = list(cameras) if cameras is not None else [make_config(i) for i in range(n_cameras)]
        store.save(cams)
        for key, value in (settings or {}).items():
            store.set_setting(key, value)
        vault = FakeVault()
        for c in cams:
            vault.set(c.id, CredentialKind.CLOUD, CLOUD)
            vault.set(c.id, CredentialKind.CAMERA_ACCOUNT, CAMERA)
        controller = AppController(store, vault, service_factory=factory)
        env = UiEnv(store=store, vault=vault, factory=factory, controller=controller, cameras=cams)
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
            env.controller.stop_all()
        except Exception:
            pass
    for f in factories:
        f.shutdown()


@pytest.fixture
def no_app_quit(monkeypatch):
    """Record QApplication.quit() instead of quitting the shared test QApplication."""
    from PyQt6.QtWidgets import QApplication

    calls: list[int] = []
    monkeypatch.setattr(QApplication, "quit", staticmethod(lambda: calls.append(1)))
    return calls
