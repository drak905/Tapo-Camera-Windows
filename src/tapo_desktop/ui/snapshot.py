"""Save a camera frame as PNG. STUB (red phase)."""
from __future__ import annotations

import pathlib


def snapshot_dir() -> pathlib.Path:
    raise NotImplementedError


def save_snapshot(image, camera_name: str, *, now=None) -> pathlib.Path:
    raise NotImplementedError
