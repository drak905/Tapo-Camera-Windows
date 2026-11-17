"""Persistent, non-secret application configuration (contract: docs/CONTRACT_UI.md).

File layout (JSON, UTF-8)::

    {"version": 1, "cameras": [<CameraConfig as dict, enums stored by value>], "settings": {...}}

Secrets (passwords) are NEVER written here; they live in the DPAPI CredentialVault.
"""
from __future__ import annotations

import pathlib

from tapo_desktop.models import CameraConfig

#: Default values returned by :meth:`ConfigStore.get_setting` when the caller passes no default
#: and the key was never set.
DEFAULT_SETTINGS: dict = {"grid_size": 2, "minimize_to_tray": True, "notifications": True}


def default_data_dir() -> pathlib.Path:
    """Return the per-user data directory.

    ``%TAPO_DESKTOP_HOME%`` if that env var is set (non-empty), else ``%APPDATA%\\TapoDesktop``.
    The directory is not required to exist (callers create it on first write).
    """
    raise NotImplementedError


class ConfigStore:
    """JSON config file; ``path`` defaults to ``default_data_dir() / "config.json"``.

    * ``load()`` on a missing or corrupt file returns ``[]`` (never raises for bad JSON).
    * ``save()`` and ``set_setting()`` are atomic: write a temp file in the same directory, then
      ``os.replace`` it over the target. If anything fails, the previous file is left intact.
    * ``save()`` preserves settings; ``set_setting()`` preserves cameras.
    * Settings used: ``"grid_size"`` (1|2|3), ``"minimize_to_tray"`` (bool), ``"notifications"`` (bool).
    """

    def __init__(self, path: pathlib.Path | None = None):
        raise NotImplementedError

    @property
    def path(self) -> pathlib.Path:
        """Absolute path of the JSON file."""
        raise NotImplementedError

    def load(self) -> list[CameraConfig]:
        """Return the persisted cameras (in saved order); ``[]`` if file missing/corrupt."""
        raise NotImplementedError

    def save(self, cameras: list[CameraConfig]) -> None:
        """Atomically persist ``cameras`` (no secrets)."""
        raise NotImplementedError

    def get_setting(self, key: str, default=None):
        """Return a persisted setting; if unset return ``default`` when given, else
        ``DEFAULT_SETTINGS.get(key)``."""
        raise NotImplementedError

    def set_setting(self, key: str, value) -> None:
        """Atomically persist one JSON-serialisable setting."""
        raise NotImplementedError
