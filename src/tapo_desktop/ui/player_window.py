"""Playback window (stub, Phase 5 red)."""
from __future__ import annotations

from PyQt6.QtWidgets import QWidget


class PlaybackTimeline(QWidget):
    def __init__(self, parent: QWidget | None = None):
        raise NotImplementedError


class PlayerWindow(QWidget):
    def __init__(self, controller, camera_id: str, *, day=None, clips=None, start=None, parent=None):
        raise NotImplementedError
