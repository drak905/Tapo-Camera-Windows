"""SD-card playback (contract: docs/CONTRACT_UI.md). objectName ``playbackPanel``.

Children: ``recordingDate`` (QDateEdit; changing it, or ``set_camera``, calls
``controller.load_recordings(id, date)``), ``recordingsList`` (one row per clip, text
``"HH:MM:SS - HH:MM:SS"`` of ``clip.start``/``clip.end`` converted to LOCAL time
(``dt.astimezone()``), rows in the order received; ``Qt.UserRole`` data = the RecordingClip),
``downloadButton`` (downloads the selected row: path from ``self.choose_save_path(clip)``;
``None`` = cancelled) and ``downloadProgress`` (QProgressBar 0..100; reaches its maximum when
``download_finished`` reports success).
"""
from __future__ import annotations

import pathlib

from PyQt6.QtWidgets import QWidget

from tapo_desktop.models import RecordingClip


class PlaybackPanel(QWidget):
    def __init__(self, controller, parent: QWidget | None = None):
        super().__init__(parent)
        raise NotImplementedError

    def camera_id(self) -> str | None:
        raise NotImplementedError

    def set_camera(self, camera_id: str | None) -> None:
        raise NotImplementedError

    def choose_save_path(self, clip: RecordingClip) -> pathlib.Path | None:
        """Ask for the output file (QFileDialog.getSaveFileName, ``.mp4``). Tests replace this
        method on the instance; the download button MUST go through ``self.choose_save_path``."""
        raise NotImplementedError
