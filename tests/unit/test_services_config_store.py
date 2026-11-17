"""ConfigStore / default_data_dir (contract: docs/CONTRACT_UI.md, services.config_store)."""
from __future__ import annotations

import json
import os
import pathlib
import tempfile

import pytest
from hypothesis import given, settings
from hypothesis import strategies as st

from tapo_desktop.models import CameraConfig, StreamQuality
from tapo_desktop.services import config_store as cs_mod
from tapo_desktop.services.config_store import ConfigStore, default_data_dir

pytestmark = pytest.mark.unit


def _cams() -> list[CameraConfig]:
    return [
        CameraConfig(name="Front door", host="192.168.1.20", id="a1"),
        CameraConfig(name="Garage", host="fe80::1", id="b2", https_port=8443, rtsp_port=8554,
                     onvif_port=2021, media_port=8801, dual_lens=True,
                     grid_quality=StreamQuality.TELE_SD, fullscreen_quality=StreamQuality.TELE_HD,
                     enable_onvif_events=False),
    ]


def test_round_trip_preserves_every_field_and_order(tmp_path):
    path = tmp_path / "config.json"
    ConfigStore(path).save(_cams())
    loaded = ConfigStore(path).load()
    assert loaded == _cams()
    assert [c.id for c in loaded] == ["a1", "b2"]
    assert isinstance(loaded[1].grid_quality, StreamQuality)
    assert loaded[1].grid_quality is StreamQuality.TELE_SD


def test_file_format_versioned_json_enums_by_value(tmp_path):
    path = tmp_path / "config.json"
    store = ConfigStore(path)
    assert store.path == path.absolute()
    store.save(_cams())
    doc = json.loads(path.read_text(encoding="utf-8"))
    assert doc["version"] == 1
    assert [c["id"] for c in doc["cameras"]] == ["a1", "b2"]
    assert doc["cameras"][1]["grid_quality"] == "stream7"
    assert doc["cameras"][1]["dual_lens"] is True


def test_load_missing_or_corrupt_file_returns_empty(tmp_path):
    assert ConfigStore(tmp_path / "nope" / "config.json").load() == []
    bad = tmp_path / "bad.json"
    bad.write_text("{not json", encoding="utf-8")
    assert ConfigStore(bad).load() == []


def test_save_creates_parent_directory(tmp_path):
    path = tmp_path / "deep" / "er" / "config.json"
    ConfigStore(path).save(_cams()[:1])
    assert path.is_file()
    assert ConfigStore(path).load() == _cams()[:1]


def test_no_secrets_in_file(tmp_path):
    path = tmp_path / "config.json"
    store = ConfigStore(path)
    store.save(_cams())
    store.set_setting("grid_size", 3)
    text = path.read_text(encoding="utf-8").lower()
    assert "password" not in text
    assert "passwd" not in text
    assert "secret" not in text


def test_save_is_atomic_via_temp_file_and_os_replace(tmp_path, monkeypatch):
    path = tmp_path / "config.json"
    store = ConfigStore(path)
    store.save(_cams()[:1])
    seen: list[tuple[pathlib.Path, pathlib.Path]] = []
    real_replace = os.replace

    def spy(src, dst, *a, **kw):
        seen.append((pathlib.Path(src), pathlib.Path(dst)))
        return real_replace(src, dst, *a, **kw)

    monkeypatch.setattr(os, "replace", spy)
    store.save(_cams())
    assert seen, "save() must write a temp file and os.replace() it over the target"
    src, dst = seen[-1]
    assert dst.resolve() == path.resolve()
    assert src.resolve().parent == path.resolve().parent
    assert src.resolve() != path.resolve()
    assert sorted(p.name for p in tmp_path.iterdir()) == ["config.json"], "temp file left behind"


def test_failed_replace_leaves_previous_file_intact(tmp_path, monkeypatch):
    path = tmp_path / "config.json"
    store = ConfigStore(path)
    store.save(_cams()[:1])
    before = path.read_bytes()

    def boom(*a, **kw):
        raise OSError("disk full")

    monkeypatch.setattr(os, "replace", boom)
    with pytest.raises(OSError):
        store.save(_cams())
    monkeypatch.undo()
    assert path.read_bytes() == before
    assert ConfigStore(path).load() == _cams()[:1]


def test_settings_defaults_and_explicit_default(tmp_path):
    store = ConfigStore(tmp_path / "config.json")
    assert cs_mod.DEFAULT_SETTINGS == {"grid_size": 2, "minimize_to_tray": True, "notifications": True}
    assert store.get_setting("grid_size") == 2
    assert store.get_setting("minimize_to_tray") is True
    assert store.get_setting("notifications") is True
    assert store.get_setting("unknown") is None
    assert store.get_setting("unknown", 42) == 42


def test_settings_persist_and_coexist_with_cameras(tmp_path):
    path = tmp_path / "config.json"
    store = ConfigStore(path)
    store.save(_cams())
    store.set_setting("grid_size", 3)
    store.set_setting("minimize_to_tray", False)
    store.save(_cams()[:1])  # save preserves settings
    again = ConfigStore(path)
    assert again.get_setting("grid_size") == 3
    assert again.get_setting("minimize_to_tray") is False
    assert again.get_setting("grid_size", 1) == 3  # persisted value beats the explicit default
    assert again.load() == _cams()[:1]  # set_setting preserved cameras
    again.set_setting("notifications", False)
    assert ConfigStore(path).load() == _cams()[:1]
    assert ConfigStore(path).get_setting("notifications") is False


def test_default_data_dir_env_override(tmp_path, monkeypatch):
    monkeypatch.setenv("TAPO_DESKTOP_HOME", str(tmp_path / "home"))
    assert default_data_dir() == tmp_path / "home"
    store = ConfigStore()
    assert store.path == (tmp_path / "home" / "config.json").absolute()
    store.save(_cams()[:1])
    assert (tmp_path / "home" / "config.json").is_file()


def test_default_data_dir_appdata(tmp_path, monkeypatch):
    monkeypatch.delenv("TAPO_DESKTOP_HOME", raising=False)
    monkeypatch.setenv("APPDATA", str(tmp_path / "Roaming"))
    assert default_data_dir() == tmp_path / "Roaming" / "TapoDesktop"
    monkeypatch.setenv("TAPO_DESKTOP_HOME", "")  # empty = not set
    assert default_data_dir() == tmp_path / "Roaming" / "TapoDesktop"


_names = st.text(st.characters(blacklist_categories=("Cs",)), min_size=0, max_size=40)


@settings(max_examples=30, deadline=None)
@given(name=_names, port=st.integers(1, 65535), dual=st.booleans(),
       q=st.sampled_from(list(StreamQuality)))
def test_round_trip_property(name, port, dual, q):
    cam = CameraConfig(name=name, host="10.0.0.5", id="x", rtsp_port=port, dual_lens=dual, grid_quality=q)
    with tempfile.TemporaryDirectory() as d:
        path = pathlib.Path(d) / "config.json"
        ConfigStore(path).save([cam])
        assert ConfigStore(path).load() == [cam]
