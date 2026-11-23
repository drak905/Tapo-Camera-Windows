"""Phase 9 (iMou Open Platform, route A) fixtures: a mock iMou cloud, a loopback-only network guard, a clock."""
from __future__ import annotations

import os
import socket

os.environ.setdefault("QT_QPA_PLATFORM", "offscreen")

import pytest  # noqa: E402

from tests.fixtures.mock_imou_cloud import MOCK_APP_ID, MOCK_APP_SECRET, MockImouCloud  # noqa: E402

LOOPBACK = {"127.0.0.1", "::1", "localhost"}


@pytest.fixture(autouse=True)
def _loopback_only(monkeypatch):
    """No test in this folder may open a socket to anything but loopback (the iMou cloud is never contacted)."""
    real = socket.create_connection

    def guard(address, *args, **kwargs):
        host = address[0]
        if host not in LOOPBACK:
            raise AssertionError(f"test tried to contact a non-loopback host: {host!r}")
        return real(address, *args, **kwargs)

    monkeypatch.setattr(socket, "create_connection", guard)


@pytest.fixture(autouse=True)
def _isolated_home(monkeypatch, tmp_path):
    monkeypatch.setenv("TAPO_DESKTOP_HOME", str(tmp_path / "home"))


@pytest.fixture(autouse=True)
def _reset_runtime():
    """The iMou runtime is process-wide state: every test starts and ends without one."""
    try:
        from tapo_desktop.services import imou_camera_service
    except ImportError:  # the service module is built in a later part
        yield
        return
    imou_camera_service.set_runtime(None)
    yield
    imou_camera_service.set_runtime(None)


class Clock:
    """Settable epoch clock."""

    def __init__(self, now: float = 1_700_000_000.0):
        self.now = now

    def __call__(self) -> float:
        return self.now

    def advance(self, seconds: float) -> None:
        self.now += seconds


@pytest.fixture
def clock():
    return Clock()


@pytest.fixture
def cloud(clock):
    return MockImouCloud(clock=clock)


@pytest.fixture
def app_keys():
    return MOCK_APP_ID, MOCK_APP_SECRET


# ---------------------------------------------------------------------------------------- account / UI environment
class SpyVault:
    """In-memory vault (the e2e FakeVault) that remembers every delete."""

    def __init__(self):
        from tests.e2e.fakes import FakeVault

        self._inner = FakeVault()
        self.deleted: list[str] = []

    def set(self, camera_id, kind, creds):
        self._inner.set(camera_id, kind, creds)

    def get(self, camera_id, kind):
        return self._inner.get(camera_id, kind)

    def delete(self, camera_id):
        self.deleted.append(camera_id)
        self._inner.delete(camera_id)

    def camera_ids(self):
        return self._inner.camera_ids()


class ImouEnv:
    def __init__(self, tmp_path, cloud, clock, cameras=()):
        from tapo_desktop.services.config_store import ConfigStore
        from tapo_desktop.services.controller import AppController
        from tests.e2e.fakes import FakeServiceFactory

        self.cloud, self.clock = cloud, clock
        self.store = ConfigStore(tmp_path / "config.json")
        self.store.save(list(cameras))
        self.vault = SpyVault()
        self.tapo = FakeServiceFactory(frame_interval=0.25)
        self.controller = AppController(self.store, self.vault, service_factory=self.tapo)
        self.sup_instances: list = []
        self.manager = None
        self.signals = {"connected": [], "failed": [], "disconnected": 0, "changed": 0}

    def client_factory(self, app_id, secret, data_center, guard, clock):
        from tapo_desktop.core.imou.client import ImouClient

        return ImouClient(app_id, secret, data_center, transport=self.cloud.post, clock=clock, guard=guard)

    def supervisor_factory(self, **kw):
        sup = _FakeSupervisor(**kw)
        self.sup_instances.append(sup)
        return sup

    def build_manager(self):
        from tapo_desktop.services.imou_account import ImouAccountManager

        manager = ImouAccountManager(self.controller, self.vault, self.store, client_factory=self.client_factory,
                                     clock=self.clock, supervisor_factory=self.supervisor_factory,
                                     parent=self.controller)
        manager.connected.connect(lambda label: self.signals["connected"].append(label))
        manager.connect_failed.connect(lambda msg: self.signals["failed"].append(msg))
        manager.disconnected.connect(lambda: self.signals.__setitem__("disconnected", self.signals["disconnected"] + 1))
        manager.cameras_changed.connect(lambda: self.signals.__setitem__("changed", self.signals["changed"] + 1))
        self.manager = manager
        return manager

    def idle(self, qtbot, ms: int = 5000) -> None:
        assert self.manager.wait_idle(ms), "imou account manager did not become idle"
        qtbot.wait(20)

    def ids(self, source: str | None = None) -> list[str]:
        return [c.id for c in self.controller.cameras() if source is None or c.source == source]


class _FakeSupervisor:
    def __init__(self, **kw):
        from tapo_desktop.core.media_engine.supervisor import StreamState

        self.kw, self.started, self.stopped = kw, False, 0
        self._states = StreamState

    def start(self):
        self.started = True

    def stop(self, timeout=3.0):
        self.stopped += 1


@pytest.fixture
def imou_env(tmp_path, qtbot, cloud, clock):
    made: list[ImouEnv] = []

    def make(cameras=()):
        env = ImouEnv(tmp_path, cloud, clock, cameras)
        made.append(env)
        env.build_manager()
        return env

    yield make
    for env in made:
        try:
            env.manager.wait_idle(3000)
        except Exception:
            pass
        try:
            env.controller.stop_all()
        except Exception:
            pass
        env.tapo.shutdown()
