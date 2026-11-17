"""Save a camera frame as PNG under ``%USERPROFILE%\\Pictures\\Tapo Desktop`` (contract:
docs/CONTRACT_PHASE2.md, lane P2-app)."""
from __future__ import annotations

import os
import pathlib
import re
from datetime import datetime

from PyQt6.QtGui import QImage

_FOLDER = "Tapo Desktop"
_UNSAFE = re.compile(r'[<>:"/\\|?*\x00-\x1f]')
_RESERVED = {"CON", "PRN", "AUX", "NUL", *(f"COM{i}" for i in range(1, 10)), *(f"LPT{i}" for i in range(1, 10))}


def snapshot_dir() -> pathlib.Path:
    """``%USERPROFILE%\\Pictures\\Tapo Desktop`` (home directory if USERPROFILE is unset)."""
    profile = os.environ.get("USERPROFILE", "").strip()
    base = pathlib.Path(profile) if profile else pathlib.Path.home()
    return base / "Pictures" / _FOLDER


def safe_file_stem(name: str) -> str:
    """``name`` usable as a Windows file name (unsafe characters -> ``_``)."""
    stem = _UNSAFE.sub("_", name or "").strip().rstrip(" ._")
    if not stem or stem.upper() in _RESERVED:
        stem = f"camera_{stem}" if stem else "camera"
    return stem[:80]


def save_snapshot(image: QImage, camera_name: str, *, now: datetime | None = None,
                  folder: pathlib.Path | None = None) -> pathlib.Path:
    """Write ``image`` as ``<name>_<YYYYmmdd_HHMMSS>.png`` (``_2``, ``_3``... if taken twice in the
    same second) and return the path. Raises OSError if the file cannot be written."""
    if image is None or image.isNull():
        raise ValueError("no image to save")
    folder = folder or snapshot_dir()
    folder.mkdir(parents=True, exist_ok=True)
    stem = f"{safe_file_stem(camera_name)}_{(now or datetime.now()).strftime('%Y%m%d_%H%M%S')}"
    path = folder / f"{stem}.png"
    counter = 2
    while path.exists():
        path = folder / f"{stem}_{counter}.png"
        counter += 1
    if not image.save(str(path), "PNG"):
        raise OSError(f"could not write {path}")
    return path
