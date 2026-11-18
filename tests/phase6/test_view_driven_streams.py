"""View-driven streaming end to end (CameraGrid -> AppController -> TapoCameraService -> RelayProvider):
every camera in a visible slot streams at the same time, local and remote, for every grid size 1x1..5x5.
The fixed remote cap (``max_remote_streams``, default 4) no longer leaves visible remote tiles on
"Remote - click to view". Fake relay / local supervisors, no network, no ffmpeg."""
from __future__ import annotations

import os

os.environ.setdefault("QT_QPA_PLATFORM", "offscreen")

from dataclasses import dataclass, field  # noqa: E402

import pytest  # noqa: E402

from tapo_desktop.models import CameraConfig, CredentialKind, Credentials, StreamQuality  # noqa: E402
from tapo_desktop.services.camera_service import TapoCameraService  # noqa: E402
from tapo_desktop.services.remote import (  # noqa: E402
    DEFAULT_MAX_REMOTE_STREAMS,
    REMOTE_CLICK_TO_VIEW,
    RelayProvider,
)
from tests.e2e.fakes import FakeServiceFactory, FakeVault  # noqa: E402
from tests.phase2.app_fakes import FakeControlClient, SupervisorFactory  # noqa: E402

pytestmark = [pytest.mark.integration, pytest.mark.timeout(120)]

CLOUD = Credentials("admin", "pw")


def remote_cfg(i: int) -> CameraConfig:
    return CameraConfig(name=f"Away {i}", host="", id=f"REM{i}", source="cloud", device_id=f"REM{i}",
                        model="C210", lan_found=False, online=True, enable_onvif_events=False)


def lan_cfg(i: int) -> CameraConfig:
    return CameraConfig(name=f"Hall {i}", host=f"192.0.2.{i + 10}", id=f"lan{i}", enable_onvif_events=False)


def new_provider() -> tuple[RelayProvider, SupervisorFactory]:
    sups = SupervisorFactory("relay")
    provider = RelayProvider(object(), terminal_uuid="T" * 32, relay_factory=lambda _c: object(),
                             supervisor_factory=sups)
    return provider, sups


@dataclass
class Env:
    controller: object
    store: object
    relay: SupervisorFactory
    provider: RelayProvider
    lan: FakeServiceFactory
    states: list = field(default_factory=list)
    grid: object = None

    def idle(self, qtbot) -> None:
        assert self.controller.wait_idle(5000), "controller did not become idle"
        qtbot.wait(30)

    def relay_streaming(self, relay: SupervisorFactory | None = None) -> set[str]:
        """Remote camera ids with a started, not stopped relay supervisor."""
        return {s.args[1] for s in (relay or self.relay).created if s.started and not s.stopped}

    def lan_streaming(self) -> set[str]:
        return {cid for cid, svc in self.lan.services.items() if svc.stream_args is not None}

    def streaming(self) -> set[str]:
        return self.relay_streaming() | self.lan_streaming()

    def waiting(self) -> list[str]:
        return [cid for cid, _s, reason in self.states if reason == REMOTE_CLICK_TO_VIEW]


@pytest.fixture
def make_env(tmp_path, qtbot):
    from tapo_desktop.services.config_store import ConfigStore
    from tapo_desktop.services.controller import AppController
    from tapo_desktop.ui.grid import CameraGrid

    envs: list[Env] = []

    def _make(cams: list[CameraConfig], *, provider: bool = True, settings: dict | None = None) -> Env:
        store = ConfigStore(tmp_path / f"config{len(envs)}.json")
        store.save(cams)
        for key, value in (settings or {}).items():
            store.set_setting(key, value)
        vault = FakeVault()
        for c in cams:
            vault.set(c.id, CredentialKind.CLOUD, CLOUD)
        lan = FakeServiceFactory(frame_interval=0.25)
        holder: list[AppController] = []

        def factory(cfg: CameraConfig):
            if cfg.source != "cloud":
                return lan(cfg)
            return TapoCameraService(cfg, vault, local_stream_factory=SupervisorFactory("local"),
                                     stream_factory=SupervisorFactory("rtsp"),
                                     control_factory=lambda **_kw: FakeControlClient(),
                                     remote=lambda: holder[0].remote_provider())

        controller = AppController(store, vault, service_factory=factory)
        holder.append(controller)
        prov, relay = new_provider()
        env = Env(controller=controller, store=store, relay=relay, provider=prov, lan=lan)
        controller.stream_state.connect(lambda cid, s, r: env.states.append((cid, s, r)))
        if provider:
            controller.set_remote_provider(prov)
        env.grid = CameraGrid(controller)
        qtbot.addWidget(env.grid)
        envs.append(env)
        return env

    yield _make
    for env in envs:
        env.controller.stop_all()
        env.lan.shutdown()


def ids(prefix: str, n: int) -> set[str]:
    return {f"{prefix}{i}" for i in range(n)}


def test_3x3_grid_of_remote_cameras_streams_all_nine(make_env, qtbot):
    env = make_env([remote_cfg(i) for i in range(9)])
    env.grid.set_grid_size(3)
    env.idle(qtbot)
    assert env.relay_streaming() == ids("REM", 9)
    assert env.waiting() == []
    assert env.provider.gate.capacity == DEFAULT_MAX_REMOTE_STREAMS == 4  # the old cap no longer limits the view
    assert env.provider.gate.reserved() == ids("REM", 9)
    assert sorted(env.provider.gate.holders()) == sorted(ids("REM", 9))


@pytest.mark.parametrize("n", [1, 2, 3, 4, 5])
def test_every_grid_size_streams_every_visible_camera(make_env, qtbot, n):
    env = make_env([remote_cfg(i) for i in range(25)])
    env.grid.set_grid_size(n)
    env.idle(qtbot)
    visible = {cid for cid in env.grid.visible_slots() if cid}
    assert len(visible) == n * n
    assert env.relay_streaming() == visible
    assert env.waiting() == []
    for sup in env.relay.created:
        if sup.args[1] in visible and not sup.stopped:
            assert sup.args[2:4] == (640, 360) and sup.kwargs["resolution"] == "VGA"
            assert sup.kwargs["hwaccel"] == "none"  # small tiles decode in software


def test_mixed_local_and_remote_grid_streams_everything(make_env, qtbot):
    cams = [lan_cfg(0), remote_cfg(0), lan_cfg(1)] + [remote_cfg(i) for i in range(1, 7)]
    env = make_env(cams)
    env.grid.set_grid_size(3)
    env.idle(qtbot)
    assert env.lan_streaming() == {"lan0", "lan1"}
    assert env.relay_streaming() == ids("REM", 7)
    assert env.waiting() == []


def test_growing_and_shrinking_the_grid_follows_the_view(make_env, qtbot):
    env = make_env([remote_cfg(i) for i in range(16)])
    env.grid.set_grid_size(2)
    env.idle(qtbot)
    assert env.relay_streaming() == ids("REM", 4)
    env.grid.set_grid_size(4)
    env.idle(qtbot)
    assert env.relay_streaming() == ids("REM", 16) and env.waiting() == []
    env.grid.set_grid_size(3)
    env.idle(qtbot)
    assert env.relay_streaming() == ids("REM", 9)
    assert env.provider.gate.reserved() == ids("REM", 9)
    assert sorted(env.provider.gate.holders()) == sorted(ids("REM", 9))


def test_replacing_a_camera_in_a_full_grid_does_not_wait_for_a_click(make_env, qtbot):
    """The new camera's start may run before the old camera's stop released its relay slot: its
    reservation lets it in anyway."""
    env = make_env([remote_cfg(i) for i in range(10)])
    env.grid.set_grid_size(3)
    env.idle(qtbot)
    for _ in range(3):
        env.grid.place_camera(0, "REM9")
        env.idle(qtbot)
        assert "REM9" in env.relay_streaming() and "REM0" not in env.relay_streaming()
        env.grid.place_camera(0, "REM0")
        env.idle(qtbot)
        assert "REM0" in env.relay_streaming() and "REM9" not in env.relay_streaming()
    assert env.waiting() == []
    assert len(env.relay_streaming()) == 9


def test_fullscreen_streams_one_camera_then_the_grid_again(make_env, qtbot):
    env = make_env([remote_cfg(i) for i in range(9)])
    env.grid.set_grid_size(3)
    env.idle(qtbot)
    env.grid.toggle_fullscreen("REM4")
    env.idle(qtbot)
    assert env.relay_streaming() == {"REM4"}
    assert env.provider.gate.reserved() == {"REM4"}
    sup = next(s for s in env.relay.created if s.args[1] == "REM4" and not s.stopped)
    assert sup.switches and sup.switches[-1][0] == "HD"  # switched in place to the HD preview
    env.grid.toggle_fullscreen("REM4")
    env.idle(qtbot)
    assert env.relay_streaming() == ids("REM", 9) and env.waiting() == []


def test_sign_in_after_the_grid_is_shown_streams_all_remote_tiles(make_env, qtbot):
    env = make_env([remote_cfg(i) for i in range(9)], provider=False)
    env.grid.set_grid_size(3)
    env.idle(qtbot)
    assert env.relay_streaming() == set()
    env.controller.set_remote_provider(env.provider)
    env.idle(qtbot)
    assert env.relay_streaming() == ids("REM", 9)
    assert env.provider.gate.reserved() == ids("REM", 9)
    assert env.waiting() == []


def test_sign_out_clears_the_reservations_of_the_old_provider(make_env, qtbot):
    env = make_env([remote_cfg(i) for i in range(4)])
    env.grid.set_grid_size(2)
    env.idle(qtbot)
    env.controller.set_remote_provider(None)
    assert env.provider.gate.reserved() == frozenset()
    other, other_relay = new_provider()
    env.controller.set_remote_provider(other)
    env.idle(qtbot)
    assert other.gate.reserved() == ids("REM", 4)
    assert env.relay_streaming(other_relay) == ids("REM", 4)


def test_explicit_max_remote_streams_setting_does_not_limit_the_view(make_env, qtbot):
    env = make_env([remote_cfg(i) for i in range(9)], settings={"max_remote_streams": 2})
    env.provider.gate.set_capacity(2)  # what AccountManager does with the setting at sign-in
    env.grid.set_grid_size(3)
    env.idle(qtbot)
    assert env.relay_streaming() == ids("REM", 9) and env.waiting() == []


def test_removed_camera_and_stop_all_drop_reservations(make_env, qtbot):
    env = make_env([remote_cfg(i) for i in range(4)])
    env.grid.set_grid_size(2)
    env.idle(qtbot)
    env.controller.remove_camera("REM1")
    env.idle(qtbot)
    assert "REM1" not in env.provider.gate.reserved()
    env.controller.stop_all()
    assert env.provider.gate.reserved() == frozenset()
    assert env.provider.gate.holders() == []


def test_controller_without_a_view_keeps_the_phase3_gate(make_env, qtbot):
    """Nothing viewed: no reservations, the configured capacity applies as before."""
    env = make_env([remote_cfg(i) for i in range(2)])
    env.grid.set_grid_size(1)
    env.grid.clear_slot(0)
    env.idle(qtbot)
    assert env.provider.gate.reserved() == frozenset()
    assert env.provider.gate.capacity == DEFAULT_MAX_REMOTE_STREAMS


def test_grid_requests_carry_the_policy_quality(make_env, qtbot):
    env = make_env([remote_cfg(i) for i in range(2)])
    env.grid.set_grid_size(2)
    env.idle(qtbot)
    created = [s for s in env.relay.created if not s.stopped]
    assert {s.args[1] for s in created} == {"REM0", "REM1"}
    assert all(s.kwargs["resolution"] == "VGA" for s in created)
    assert StreamQuality.SD.value == "stream2"
