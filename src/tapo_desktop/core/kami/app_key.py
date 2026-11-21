"""Run-time loader of the Kami password HMAC key [spec:kami.auth.password_hash].

The key is a vendor secret that sits as a constant in the Kami app. It is deliberately NOT part of this repository.
Lookup order: environment ``TAPO_KAMI_APP_KEY``, then the file ``kami_app_key.txt`` in the app data folder
(``%TAPO_DESKTOP_HOME%`` or ``%APPDATA%\\TapoDesktop``). Nothing here logs the value.
"""
from __future__ import annotations

import os
import pathlib

ENV_VAR = "TAPO_KAMI_APP_KEY"
KEY_FILE_NAME = "kami_app_key.txt"


def data_dir() -> pathlib.Path:
    """Same folder rule as ``services.config_store.default_data_dir`` (kept here so core never imports services)."""
    home = os.environ.get("TAPO_DESKTOP_HOME", "").strip()
    if home:
        return pathlib.Path(home)
    appdata = os.environ.get("APPDATA", "").strip()
    return (pathlib.Path(appdata) if appdata else pathlib.Path.home() / "AppData" / "Roaming") / "TapoDesktop"


def key_file_path() -> pathlib.Path:
    return data_dir() / KEY_FILE_NAME


def load_app_key() -> str | None:
    """The key, or ``None`` when neither the environment variable nor the file holds one."""
    value = os.environ.get(ENV_VAR, "").strip()
    if value:
        return value
    try:
        text = key_file_path().read_text(encoding="utf-8-sig").strip()
    except (OSError, UnicodeDecodeError):
        return None
    return text or None
