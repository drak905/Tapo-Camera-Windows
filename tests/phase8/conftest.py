"""Phase 8 (Kami Home) fixtures: a mock Kami cloud, a controller on a fake vault, and a network guard."""
from __future__ import annotations

import os
import socket
from dataclasses import dataclass, field

os.environ.setdefault("QT_QPA_PLATFORM", "offscreen")

import pytest  # noqa: E402

from tests.fixtures.mock_kami_cloud import MOCK_APP_KEY, MockKamiCloud  # noqa: E402

LOOPBACK = {"127.0.0.1", "::1", "localhost"}


@pytest.fixture(autouse=True)
def _loopback_only(monkeypatch):
    """No test in this folder may open a socket to anything but loopback (no real Kami server, ever)."""
    real = socket.create_connection

    def guard(address, *args, **kwargs):
        host = address[0]
        if host not in LOOPBACK:
            raise AssertionError(f"test tried to contact a non-loopback host: {host!r}")
        return real(address, *args, **kwargs)

    monkeypatch.setattr(socket, "create_connection", guard)


@pytest.fixture(autouse=True)
def _no_ambient_key(monkeypatch, tmp_path):
    """The vendor key must never come from the developer's own environment or app-data folder in a test."""
    monkeypatch.delenv("TAPO_KAMI_APP_KEY", raising=False)
    monkeypatch.setenv("TAPO_DESKTOP_HOME", str(tmp_path / "home"))


@pytest.fixture
def mock_kami():
    mock = MockKamiCloud().start()
    mock.add_account("owner@example.com", "pw-correct-1")
    mock.devices = []
    try:
        yield mock
    finally:
        mock.stop()


@dataclass
class KamiEnv:
    store: object
    vault: object
    controller: object
    manager: object
    mock: MockKamiCloud
    factory_calls: list = field(default_factory=list)
    signals: dict = field(default_factory=lambda: {"signed_in": [], "failed": [], "signed_out": [], "changed": 0})

    def idle(self, qtbot, ms: int = 5000) -> None:
        assert self.manager.wait_idle(ms), "kami account manager did not become idle"
        qtbot.wait(20)

    def ids(self) -> list[str]:
        return [c.id for c in self.controller.cameras()]

    def kami_ids(self) -> list[str]:
        return [c.id for c in self.controller.cameras() if c.source == "kami"]


def _connect(env: KamiEnv, manager) -> None:
    manager.signed_in.connect(lambda label: env.signals["signed_in"].append(label))
    manager.sign_in_failed.connect(lambda msg: env.signals["failed"].append(msg))
    manager.signed_out.connect(lambda: env.signals["signed_out"].append(True))
    manager.cameras_changed.connect(lambda: env.signals.__setitem__("changed", env.signals["changed"] + 1))


def build_manager(env: KamiEnv, mock: MockKamiCloud, *, routes: dict | None = None, default_factory: bool = False,
                  clock=None):
    from tapo_desktop.core.kami.client import KamiClient
    from tapo_desktop.services.kami_account import KamiAccountManager

    def factory(region: str, country: str):
        env.factory_calls.append((region, country))
        target = (routes or {}).get(region, mock)
        return KamiClient(region, country, app_key=MOCK_APP_KEY, base_url=target.url, timeout=5.0)

    kwargs = {} if clock is None else {"clock": clock}
    manager = KamiAccountManager(env.controller, env.vault, env.store,
                                 client_factory=None if default_factory else factory, **kwargs)
    return manager


@pytest.fixture
def kami_env(tmp_path, qtbot, mock_kami):
    from tapo_desktop.services.config_store import ConfigStore
    from tapo_desktop.services.controller import AppController
    from tests.e2e.fakes import FakeServiceFactory, FakeVault

    store = ConfigStore(tmp_path / "config.json")
    vault = FakeVault()
    service_factory = FakeServiceFactory()
    controller = AppController(store, vault, service_factory=service_factory)
    env = KamiEnv(store=store, vault=vault, controller=controller, manager=None, mock=mock_kami)
    env.manager = build_manager(env, mock_kami)
    _connect(env, env.manager)
    env.service_factory = service_factory
    yield env
    try:
        env.manager.wait_idle(3000)
    except Exception:
        pass
    try:
        controller.stop_all()
    except Exception:
        pass
    service_factory.shutdown()


def rebuild(env: KamiEnv, **kwargs):
    """Replace ``env.manager`` with a fresh one (simulated restart) and hook the signal recorders."""
    env.signals.update({"signed_in": [], "failed": [], "signed_out": [], "changed": 0})
    env.manager = build_manager(env, kwargs.pop("mock", env.mock), **kwargs)
    _connect(env, env.manager)
    return env.manager
