"""Persistent, non-secret application configuration (contract: docs/CONTRACT_UI.md).

File layout (JSON, UTF-8)::

    {"version": 1, "cameras": [<CameraConfig as dict, enums stored by value>], "settings": {...}}

Secrets (passwords) are NEVER written here; they live in the DPAPI CredentialVault.
"""
from __future__ import annotations

import dataclasses
import enum
import json
import logging
import os
import pathlib
import tempfile
import threading
from typing import Any

from tapo_desktop.models import CameraConfig, StreamQuality

log = logging.getLogger(__name__)

#: Default values returned by :meth:`ConfigStore.get_setting` when the caller passes no default
#: and the key was never set.
DEFAULT_SETTINGS: dict = {"grid_size": 2, "minimize_to_tray": True, "notifications": True}

FILE_VERSION = 1
_APP_DIR_NAME = "TapoDesktop"
_CAMERA_FIELDS = {f.name: f for f in dataclasses.fields(CameraConfig)}
_ENUM_FIELDS: dict[str, type[enum.Enum]] = {"grid_quality": StreamQuality, "fullscreen_quality": StreamQuality}
_MISSING = object()


def default_data_dir() -> pathlib.Path:
    """Return the per-user data directory.

    ``%TAPO_DESKTOP_HOME%`` if that env var is set (non-empty), else ``%APPDATA%\\TapoDesktop``.
    The directory is not required to exist (callers create it on first write).
    """
    home = os.environ.get("TAPO_DESKTOP_HOME", "").strip()
    if home:
        return pathlib.Path(home)
    appdata = os.environ.get("APPDATA", "").strip()
    base = pathlib.Path(appdata) if appdata else pathlib.Path.home() / "AppData" / "Roaming"
    return base / _APP_DIR_NAME


def _camera_to_dict(cam: CameraConfig) -> dict[str, Any]:
    out: dict[str, Any] = {}
    for name in _CAMERA_FIELDS:
        value = getattr(cam, name)
        out[name] = value.value if isinstance(value, enum.Enum) else value
    return out


def _camera_from_dict(raw: dict[str, Any]) -> CameraConfig:
    kwargs: dict[str, Any] = {}
    for name, value in raw.items():
        if name not in _CAMERA_FIELDS:
            continue  # tolerate keys written by newer versions
        enum_type = _ENUM_FIELDS.get(name)
        kwargs[name] = enum_type(value) if enum_type is not None else value
    return CameraConfig(**kwargs)


class ConfigStore:
    """JSON config file; ``path`` defaults to ``default_data_dir() / "config.json"``.

    * ``load()`` on a missing or corrupt file returns ``[]`` (never raises for bad JSON).
    * ``save()`` and ``set_setting()`` are atomic: write a temp file in the same directory, then
      ``os.replace`` it over the target. If anything fails, the previous file is left intact.
    * ``save()`` preserves settings; ``set_setting()`` preserves cameras.
    * Settings used: ``"grid_size"`` (1|2|3), ``"minimize_to_tray"`` (bool), ``"notifications"`` (bool).

    The file is re-read on every call, so several instances on the same path stay consistent.
    Methods are thread-safe within one instance.
    """

    def __init__(self, path: pathlib.Path | None = None):
        target = pathlib.Path(path) if path is not None else default_data_dir() / "config.json"
        self._path = target.absolute()
        self._lock = threading.RLock()

    def __repr__(self) -> str:
        return f"ConfigStore(path={str(self._path)!r})"

    @property
    def path(self) -> pathlib.Path:
        """Absolute path of the JSON file."""
        return self._path

    # ------------------------------------------------------------------ public API
    def load(self) -> list[CameraConfig]:
        """Return the persisted cameras (in saved order); ``[]`` if file missing/corrupt."""
        with self._lock:
            cameras: list[CameraConfig] = []
            for raw in self._read()["cameras"]:
                try:
                    cameras.append(_camera_from_dict(raw))
                except (TypeError, ValueError, AttributeError) as exc:
                    log.warning("skipping unreadable camera entry in %s: %s", self._path, exc)
            return cameras

    def save(self, cameras: list[CameraConfig]) -> None:
        """Atomically persist ``cameras`` (no secrets)."""
        with self._lock:
            doc = self._read()
            doc["cameras"] = [_camera_to_dict(c) for c in cameras]
            self._write(doc)

    def get_setting(self, key: str, default=_MISSING):
        """Return a persisted setting; if unset return ``default`` when given, else
        ``DEFAULT_SETTINGS.get(key)``."""
        with self._lock:
            settings = self._read()["settings"]
        if key in settings:
            return settings[key]
        return DEFAULT_SETTINGS.get(key) if default is _MISSING else default

    def set_setting(self, key: str, value) -> None:
        """Atomically persist one JSON-serialisable setting."""
        with self._lock:
            doc = self._read()
            doc["settings"][key] = value
            self._write(doc)

    # ------------------------------------------------------------------ internals
    def _read(self) -> dict[str, Any]:
        """Return the document normalised to ``{"version", "cameras": list, "settings": dict}``."""
        doc: dict[str, Any] = {"version": FILE_VERSION, "cameras": [], "settings": {}}
        try:
            raw = json.loads(self._path.read_text(encoding="utf-8"))
        except FileNotFoundError:
            return doc
        except (OSError, ValueError) as exc:  # JSONDecodeError and UnicodeDecodeError are ValueErrors
            log.warning("config file %s is unreadable, starting empty: %s", self._path, exc)
            return doc
        if not isinstance(raw, dict):
            log.warning("config file %s has an unexpected layout, starting empty", self._path)
            return doc
        cameras = raw.get("cameras")
        settings = raw.get("settings")
        doc["cameras"] = [c for c in cameras if isinstance(c, dict)] if isinstance(cameras, list) else []
        doc["settings"] = dict(settings) if isinstance(settings, dict) else {}
        return doc

    def _write(self, doc: dict[str, Any]) -> None:
        doc = {"version": FILE_VERSION, "cameras": doc["cameras"], "settings": doc["settings"]}
        data = json.dumps(doc, ensure_ascii=False, indent=2)
        self._path.parent.mkdir(parents=True, exist_ok=True)
        fd, tmp_name = tempfile.mkstemp(prefix=f".{self._path.name}.", suffix=".tmp", dir=self._path.parent)
        tmp = pathlib.Path(tmp_name)
        try:
            with os.fdopen(fd, "w", encoding="utf-8", newline="\n") as fh:
                fh.write(data)
                fh.flush()
                os.fsync(fh.fileno())
            os.replace(tmp, self._path)
        except BaseException:
            tmp.unlink(missing_ok=True)
            raise
