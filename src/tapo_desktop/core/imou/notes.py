"""Short notes about the last recording list of a camera (for the panel's status line): a truncated list, two clocks.

Kept in memory by camera id; `set` with an empty text clears. Never holds anything secret.
"""
from __future__ import annotations

_notes: dict[str, str] = {}


def set(camera_id: str, text: str) -> None:
    if text:
        _notes[camera_id] = text
    else:
        _notes.pop(camera_id, None)


def get(camera_id: str) -> str:
    return _notes.get(camera_id, "")
