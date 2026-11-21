"""R2-0 seam tests: Kami data model and factory seam, and proof that Tapo behaviour is unchanged.

Plan review finding 12 items 1-4 and 6, plus finding 7 (Tapo code paths a Kami camera must never reach).
"""
from __future__ import annotations

import ast
import dataclasses
import json
import os
import pathlib

os.environ.setdefault("QT_QPA_PLATFORM", "offscreen")

import pytest  # noqa: E402

from tapo_desktop.core.errors import TapoError  # noqa: E402
from tapo_desktop.models import (  # noqa: E402
    ACCOUNT_VAULT_ID, CameraConfig, CredentialKind, Credentials, StreamQuality,
)
from tapo_desktop.services.config_store import DEFAULT_SETTINGS, ConfigStore  # noqa: E402
from tests.e2e.fakes import FakeVault, make_config  # noqa: E402
from tests.phase2._app_helpers import app_env, sign_in  # noqa: E402,F401
from tests.phase2.app_fakes import EMAIL, PASSWORD, cloud_device, lan_for  # noqa: E402

SRC = pathlib.Path(__file__).resolve().parents[2] / "src" / "tapo_desktop"

#: ``CameraConfig`` as of b11c08e: field name -> default (``None`` = default_factory). R2-0 adds only vendor_data.
LEGACY_CAMERA_DEFAULTS = {
    "name": "<required>", "host": "<required>", "id": None, "https_port": 443, "rtsp_port": 554,
    "onvif_port": 2020, "media_port": 8800, "dual_lens": False, "grid_quality": StreamQuality.SD,
    "fullscreen_quality": StreamQuality.HD, "enable_onvif_events": True, "source": "manual", "device_id": "",
    "mac": "", "model": "", "online": True, "lan_found": True, "live_transport": "auto", "control_protocol": "",
    "role": 0, "share_type": "", "display_name": "", "pending_name": "", "pending_since": 0.0, "cloud_alias": "",
}


def _kami_config(uid: str = "u1", **overrides) -> CameraConfig:
    kwargs = dict(name="Kami Cam A", host="", id=f"kami:{uid}", source="kami", device_id=uid, model="Kami Indoor",
                  online=True, lan_found=False, vendor_data={"uid": uid, "region": "eu"})
    kwargs.update(overrides)
    return CameraConfig(**kwargs)


# ====================================================================== 1. config round trip
def _legacy_doc() -> dict:
    cams = []
    for i, source in enumerate(("manual", "cloud")):
        given = {"name": f"Home Camera {i}", "host": f"192.168.1.{10 + i}", "id": f"cam{i}", "source": source}
        cams.append({n: given.get(n, d.value if isinstance(d, StreamQuality) else d)  # same key order as the writer
                     for n, d in LEGACY_CAMERA_DEFAULTS.items()})
    return {"version": 1, "cameras": cams, "settings": {"grid_size": 3, "account": {"email": "owner@example.com"}}}


def test_legacy_config_loads_and_saves_back_unchanged(tmp_path):
    path = tmp_path / "config.json"
    text = json.dumps(_legacy_doc(), ensure_ascii=False, indent=2)
    path.write_text(text, encoding="utf-8", newline="\n")
    store = ConfigStore(path)
    cams = store.load()
    assert [c.id for c in cams] == ["cam0", "cam1"]
    assert all(c.vendor_data == {} for c in cams)
    store.save(cams)
    assert path.read_text(encoding="utf-8") == text  # byte for byte: no new key for Tapo cameras


def test_kami_camera_round_trips_and_tapo_only_loader_ignores_unknown_keys(tmp_path):
    store = ConfigStore(tmp_path / "config.json")
    store.save([make_config(0), _kami_config()])
    loaded = store.load()
    assert loaded[1].vendor_data == {"uid": "u1", "region": "eu"} and loaded[1].source == "kami"
    doc = json.loads(store.path.read_text(encoding="utf-8"))
    doc["cameras"][0]["future_key"] = 1  # a key from a newer build
    store.path.write_text(json.dumps(doc), encoding="utf-8")
    assert [c.id for c in store.load()] == ["cam0", "kami:u1"]


def test_camera_config_existing_fields_and_defaults_unchanged():
    fields = {f.name: f for f in dataclasses.fields(CameraConfig)}
    assert list(fields)[:-1] == list(LEGACY_CAMERA_DEFAULTS)  # same names, same order; one new field at the end
    assert list(fields)[-1] == "vendor_data"
    for name, expected in LEGACY_CAMERA_DEFAULTS.items():
        f = fields[name]
        if expected == "<required>":
            assert f.default is dataclasses.MISSING and f.default_factory is dataclasses.MISSING
        elif expected is None:
            assert f.default_factory is not dataclasses.MISSING
        else:
            assert f.default == expected and type(f.default) is type(expected), name
    assert fields["vendor_data"].default_factory is dict
    a, b = CameraConfig(name="x", host="h"), CameraConfig(name="x", host="h")
    assert a.vendor_data == {} and a.vendor_data is not b.vendor_data


def test_existing_settings_defaults_unchanged():
    assert DEFAULT_SETTINGS == {"grid_size": 2, "minimize_to_tray": True, "notifications": True}


def test_kami_setting_keys_exist_and_do_not_clash_with_tapo_keys():
    from tapo_desktop.services import config_store as cs

    assert cs.SETTING_KAMI_ACCOUNT == "kami_account"
    assert cs.SETTING_KAMI_LAST_SIGN_IN_FAILED == "kami_last_sign_in_failed"
    assert {"account", "sign_in_skipped", "terminal_uuid"}.isdisjoint({cs.SETTING_KAMI_ACCOUNT,
                                                                        cs.SETTING_KAMI_LAST_SIGN_IN_FAILED})


# ====================================================================== 2. KamiDevice and ids
def test_kami_device_is_frozen_without_secrets():
    from tapo_desktop.core.kami.models import KamiDevice

    assert [f.name for f in dataclasses.fields(KamiDevice)] == ["uid", "name", "model", "online", "shared", "region"]
    dev = KamiDevice(uid="u1", name="Kami Cam A", model="Kami Indoor", online=True, shared=False, region="eu")
    with pytest.raises(dataclasses.FrozenInstanceError):
        dev.name = "x"  # type: ignore[misc]
    assert not any(w in f.name for f in dataclasses.fields(KamiDevice) for w in ("token", "secret", "password", "key"))


def test_kami_camera_ids_are_prefixed_and_cannot_collide_with_tapo_ids():
    from tapo_desktop.core.kami.models import KAMI_ID_PREFIX, KamiDevice, kami_camera_id

    assert KAMI_ID_PREFIX == "kami:"
    assert kami_camera_id("u1") == "kami:u1" == KamiDevice(uid="u1").camera_id
    tapo_ids = ["8022" + "0" * 36, CameraConfig(name="a", host="h").id, "cam0"]  # deviceId, uuid4 hex, test id
    for tid in tapo_ids:
        assert ":" not in tid
        assert kami_camera_id(tid) != tid and kami_camera_id(tid).startswith("kami:")
    assert kami_camera_id("a") != kami_camera_id("b")
    for bad in ("", "   "):
        with pytest.raises(ValueError):
            kami_camera_id(bad)


# ====================================================================== 3. vault kind
def test_kami_vault_kind_is_separate_from_cloud():
    from tapo_desktop.models import KAMI_ACCOUNT_VAULT_ID

    assert CredentialKind.KAMI_ACCOUNT.value == "kami_account"
    assert CredentialKind.KAMI_ACCOUNT is not CredentialKind.CLOUD
    assert {k.name: k.value for k in CredentialKind if k.name != "KAMI_ACCOUNT"} == {
        "CLOUD": "cloud", "CAMERA_ACCOUNT": "camera_account", "LOCAL_TOKEN": "local_token"}
    assert KAMI_ACCOUNT_VAULT_ID == "__kami_account__" and KAMI_ACCOUNT_VAULT_ID != ACCOUNT_VAULT_ID


def test_real_vault_keeps_kami_and_tapo_account_secrets_apart(tmp_path):
    from tapo_desktop.core.auth.vault import CredentialVault
    from tapo_desktop.models import KAMI_ACCOUNT_VAULT_ID

    vault = CredentialVault(tmp_path / "vault.json")
    vault.set(ACCOUNT_VAULT_ID, CredentialKind.CLOUD, Credentials("tapo@example.com", "tapo-pw"))
    vault.set(KAMI_ACCOUNT_VAULT_ID, CredentialKind.KAMI_ACCOUNT, Credentials("kami@example.com", "kami-pw"))
    assert vault.get(KAMI_ACCOUNT_VAULT_ID, CredentialKind.CLOUD) is None
    assert vault.get(ACCOUNT_VAULT_ID, CredentialKind.KAMI_ACCOUNT) is None
    vault.delete(ACCOUNT_VAULT_ID)  # Tapo sign-out
    assert vault.get(KAMI_ACCOUNT_VAULT_ID, CredentialKind.KAMI_ACCOUNT) == Credentials("kami@example.com", "kami-pw")
    assert vault.get(ACCOUNT_VAULT_ID, CredentialKind.CLOUD) is None


# ====================================================================== 4. service factory seam
@pytest.mark.ui
@pytest.mark.parametrize("source", ["manual", "cloud"])
def test_default_factory_gives_tapo_service_for_manual_and_cloud(tmp_path, qtbot, source):
    from tapo_desktop.services.camera_service import TapoCameraService
    from tapo_desktop.services.controller import AppController

    ctrl = AppController(ConfigStore(tmp_path / "config.json"), FakeVault())
    svc = ctrl._default_factory(make_config(0, source=source))
    assert type(svc) is TapoCameraService


@pytest.mark.ui
def test_default_factory_gives_kami_service_only_for_kami(tmp_path, qtbot):
    from tapo_desktop.services.camera_service import CameraService, TapoCameraService
    from tapo_desktop.services.controller import AppController
    from tapo_desktop.services.kami_camera_service import KamiCameraService

    ctrl = AppController(ConfigStore(tmp_path / "config.json"), FakeVault())
    svc = ctrl._default_factory(_kami_config())
    assert type(svc) is KamiCameraService and isinstance(svc, CameraService)
    assert not isinstance(svc, TapoCameraService)
    assert ctrl._limiter is None and ctrl._pin_store is None  # none of the Tapo media/control wiring was built


# ====================================================================== 5. Tapo sign-in/out leave Kami alone
@pytest.mark.ui
def test_tapo_sign_in_refresh_and_sign_out_leave_kami_camera_and_vault_entry_alone(app_env, tmp_path, qtbot):
    from tapo_desktop.models import KAMI_ACCOUNT_VAULT_ID

    kami = _kami_config(mac="A8-42-A1-00-00-00")  # same MAC as the Tapo camera below: TDP matching must not touch it
    path = tmp_path / "cfg" / "config.json"
    ConfigStore(path).save([kami])
    dev = cloud_device(0)
    env = app_env(cameras=[], store_path=path, devices=[dev], lan=[lan_for(dev, "192.168.1.40")])
    kami_secret = Credentials("kami@example.com", "kami-pw")
    env.vault.set(KAMI_ACCOUNT_VAULT_ID, CredentialKind.KAMI_ACCOUNT, kami_secret)
    env.vault.set(kami.id, CredentialKind.CAMERA_ACCOUNT, Credentials("dev", "dev-secret"))

    def untouched() -> None:
        assert env.controller.camera(kami.id) == kami
        assert env.vault.get(KAMI_ACCOUNT_VAULT_ID, CredentialKind.KAMI_ACCOUNT) == kami_secret
        assert env.vault.get(kami.id, CredentialKind.CAMERA_ACCOUNT) == Credentials("dev", "dev-secret")
        assert env.factory.created_for(kami.id) == []  # no service (so no Tapo call) was ever built for it

    sign_in(env, qtbot, EMAIL, PASSWORD)
    assert [c.id for c in env.controller.cameras() if c.source == "cloud"] == [dev.device_id]
    untouched()
    env.cloud.devices = []  # the Tapo list shrinks: refresh prunes Tapo cameras only
    env.accounts.refresh_cameras()
    env.idle(qtbot)
    assert [c.id for c in env.controller.cameras()] == [kami.id]
    untouched()
    env.accounts.sign_out()
    env.idle(qtbot)
    assert [c.id for c in env.controller.cameras()] == [kami.id]
    untouched()
    assert ConfigStore(path).load() == [kami]


# ====================================================================== 6. import direction
_TAPO_ONLY = ("tapo_desktop.core.control_client", "tapo_desktop.core.cloud", "tapo_desktop.core.media_engine",
              "tapo_desktop.core.vod", "tapo_desktop.core.onvif_service", "tapo_desktop.core.discovery",
              "tapo_desktop.core.auth.crypto", "tapo_desktop.services.account", "tapo_desktop.services.remote",
              "tapo_desktop.services.controller", "tapo_desktop.ui")


def _imports(path: pathlib.Path) -> list[tuple[int, str]]:
    """(line, dotted name) of every import in ``path`` (``from a import b`` yields ``a`` and ``a.b``)."""
    out: list[tuple[int, str]] = []
    for node in ast.walk(ast.parse(path.read_text(encoding="utf-8"))):
        if isinstance(node, ast.Import):
            out += [(node.lineno, a.name) for a in node.names]
        elif isinstance(node, ast.ImportFrom) and node.module and node.level == 0:
            out.append((node.lineno, node.module))
            out += [(node.lineno, f"{node.module}.{a.name}") for a in node.names]
    return out


def _is_kami_file(path: pathlib.Path) -> bool:
    rel = path.relative_to(SRC).as_posix()
    return rel.startswith("core/kami/") or pathlib.PurePosixPath(rel).name.startswith("kami_") or \
        rel.startswith("ui/kami_")


def _under(name: str, prefixes) -> bool:
    return any(name == p or name.startswith(p + ".") for p in prefixes)


def test_tapo_modules_do_not_import_kami_except_the_one_factory_seam():
    hits = []
    for path in SRC.rglob("*.py"):
        if _is_kami_file(path):
            continue
        for line, name in _imports(path):
            if _under(name, ("tapo_desktop.core.kami",)) or name.startswith("tapo_desktop.services.kami_") \
                    or name.startswith("tapo_desktop.ui.kami_"):
                hits.append((path.relative_to(SRC).as_posix(), line, name))
    assert {(f, n) for f, _l, n in hits} <= {("services/controller.py", "tapo_desktop.services.kami_camera_service"),
                                              ("services/controller.py",
                                               "tapo_desktop.services.kami_camera_service.KamiCameraService")}, hits
    assert len({l for f, l, _n in hits}) <= 1, hits  # one import statement only


def test_kami_modules_do_not_import_tapo_core_or_the_tapo_camera_service():
    kami_files = [p for p in SRC.rglob("*.py") if _is_kami_file(p)]
    assert any(p.name == "models.py" for p in kami_files) and any(p.name == "kami_camera_service.py" for p in kami_files)
    hits = []
    for path in kami_files:
        for line, name in _imports(path):
            if _under(name, _TAPO_ONLY) or name == "tapo_desktop.services.camera_service.TapoCameraService":
                hits.append((path.relative_to(SRC).as_posix(), line, name))
    assert hits == []


# ====================================================================== 7. paths a Kami camera must never reach
def test_kami_service_is_a_stub_with_capability_flags_and_clear_errors():
    from datetime import date, datetime, timezone

    from tapo_desktop.models import DayNightMode, RecordingClip
    from tapo_desktop.services.kami_camera_service import KamiCameraService

    svc = KamiCameraService(_kami_config(), FakeVault())
    assert [getattr(svc, n) for n in ("supports_live_view", "supports_ptz", "supports_controls",
                                      "supports_playback", "supports_events", "supports_rename")] == [False] * 6
    info = svc.connect()  # no network
    assert info.model == "Kami Indoor"
    now = datetime.now(timezone.utc)
    calls = [
        lambda: svc.start_stream(StreamQuality.SD, 64, 36, lambda f: None, lambda s, r: None),
        lambda: svc.ptz_move(1, 0), svc.ptz_stop, svc.get_presets, lambda: svc.goto_preset("1"), svc.get_status,
        lambda: svc.set_privacy(True), lambda: svc.set_day_night(DayNightMode.ON), lambda: svc.set_led(True),
        lambda: svc.set_alarm(True), lambda: svc.set_motion_detection(True),
        lambda: svc.days_with_recordings(date(2026, 1, 1), date(2026, 1, 2)),
        lambda: svc.recordings_for_day(date(2026, 1, 1)),
        lambda: svc.download_recording(RecordingClip(now, now), pathlib.Path("x.mp4")),
        lambda: svc.start_playback(0.0, 1.0, 64, 36, lambda f: None, lambda s, r: None, lambda p: None),
        lambda: svc.rename("New name"),
    ]
    for call in calls:
        with pytest.raises(TapoError, match="Kami"):
            call()
    svc.stop_stream(), svc.stop_events(), svc.stop_playback(), svc.close()  # stopping is always safe


def test_kami_service_never_starts_motion_events():
    from tapo_desktop.services.kami_camera_service import KamiCameraService

    got = []
    svc = KamiCameraService(_kami_config(enable_onvif_events=True), FakeVault())
    svc.connect()
    svc.start_events(got.append)  # the controller calls this when enable_onvif_events is True
    svc.stop_events()
    assert got == []


def test_kami_camera_is_never_cloud_routed_or_renamed_on_camera():
    from tapo_desktop.services.camera_names import renames_on_camera
    from tapo_desktop.services.remote import cloud_routed

    for cfg in (_kami_config(), _kami_config(online=False, role=1)):
        assert cloud_routed(cfg) is False
        assert renames_on_camera(cfg) is False


@pytest.mark.ui
def test_controller_renames_kami_camera_locally_without_building_a_service(app_env, tmp_path, qtbot):
    kami = _kami_config()
    path = tmp_path / "cfg" / "config.json"
    ConfigStore(path).save([kami])
    env = app_env(cameras=[], store_path=path)
    assert env.controller.rename_camera(kami.id, "Porch") is False  # False = local name only
    env.idle(qtbot)
    cfg = env.controller.camera(kami.id)
    assert (cfg.name, cfg.display_name, cfg.pending_name, cfg.vendor_data) == ("Porch", "", "", kami.vendor_data)
    assert env.factory.created_for(kami.id) == []
    assert ConfigStore(path).load()[0].name == "Porch"


@pytest.mark.ui
def test_live_view_of_kami_camera_reports_a_clear_failure_through_the_default_factory(tmp_path, qtbot):
    from tapo_desktop.services.controller import AppController

    kami = _kami_config()
    store = ConfigStore(tmp_path / "config.json")
    store.save([kami])
    ctrl = AppController(store, FakeVault())
    try:
        with qtbot.waitSignal(ctrl.stream_state, timeout=5000) as blocker:
            ctrl.start_view(kami.id, StreamQuality.SD, 64, 36)
        assert blocker.args[0] == kami.id and blocker.args[1] == "FAILED" and "Kami" in blocker.args[2]
    finally:
        ctrl.stop_all()
