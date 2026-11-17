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
from datetime import date

from PyQt6.QtCore import QDate, QStandardPaths, Qt
from PyQt6.QtWidgets import (
    QDateEdit,
    QFileDialog,
    QHBoxLayout,
    QLabel,
    QListWidget,
    QListWidgetItem,
    QProgressBar,
    QPushButton,
    QVBoxLayout,
    QWidget,
)

from tapo_desktop.models import RecordingClip

#: PlayBackEventType codes [spec:vod.search.*] -> label shown in the row tooltip.
EVENT_LABELS: dict[int, str] = {1: "Continuous", 2: "Motion", 6: "Person", 8: "Vehicle", 9: "Pet"}


def clip_text(clip: RecordingClip) -> str:
    """Row text: local ``HH:MM:SS - HH:MM:SS``."""
    return f"{clip.start.astimezone():%H:%M:%S} - {clip.end.astimezone():%H:%M:%S}"


class PlaybackPanel(QWidget):
    def __init__(self, controller, parent: QWidget | None = None):
        super().__init__(parent)
        self.setObjectName("playbackPanel")
        self._ctrl = controller
        self._camera_id: str | None = None
        self._download_camera: str | None = None

        root = QVBoxLayout(self)
        root.setContentsMargins(6, 6, 6, 6)
        root.setSpacing(6)
        row = QHBoxLayout()
        row.addWidget(QLabel("Day", self))
        self._date = QDateEdit(self)
        self._date.setObjectName("recordingDate")
        self._date.setCalendarPopup(True)
        self._date.setDisplayFormat("yyyy-MM-dd")
        self._date.setDate(QDate.currentDate())
        self._date.setMaximumDate(QDate.currentDate().addDays(1))
        self._date.dateChanged.connect(self._reload)
        row.addWidget(self._date, 1)
        self._refresh = QPushButton("Refresh", self)
        self._refresh.setObjectName("refreshRecordingsButton")
        self._refresh.clicked.connect(self._reload)
        row.addWidget(self._refresh)
        root.addLayout(row)

        self._list = QListWidget(self)
        self._list.setObjectName("recordingsList")
        self._list.itemSelectionChanged.connect(self._update_buttons)
        self._list.itemDoubleClicked.connect(lambda _item: self._download())
        root.addWidget(self._list, 1)

        self._status = QLabel("", self)
        self._status.setObjectName("downloadStatus")
        self._status.setWordWrap(True)
        root.addWidget(self._status)
        self._button = QPushButton("Download clip...", self)
        self._button.setObjectName("downloadButton")
        self._button.clicked.connect(self._download)
        root.addWidget(self._button)
        self._progress = QProgressBar(self)
        self._progress.setObjectName("downloadProgress")
        self._progress.setRange(0, 100)
        self._progress.setValue(0)
        root.addWidget(self._progress)

        controller.recordings_ready.connect(self._on_recordings)
        controller.command_failed.connect(self._on_failed)
        controller.download_progress.connect(self._on_progress)
        controller.download_finished.connect(self._on_finished)
        controller.cameras_changed.connect(self._on_cameras_changed)
        self._update_buttons()

    # ------------------------------------------------------------------ public API
    def camera_id(self) -> str | None:
        return self._camera_id

    def set_camera(self, camera_id: str | None) -> None:
        """Show the recordings of ``camera_id`` for the selected day (None = nothing selected)."""
        self._camera_id = camera_id or None
        self._reload()

    def choose_save_path(self, clip: RecordingClip) -> pathlib.Path | None:
        """Ask for the output file (QFileDialog.getSaveFileName, ``.mp4``). Tests replace this
        method on the instance; the download button MUST go through ``self.choose_save_path``."""
        folder = QStandardPaths.writableLocation(QStandardPaths.StandardLocation.MoviesLocation)
        name = self._camera_name() or "camera"
        safe = "".join(ch if ch.isalnum() or ch in "-_" else "_" for ch in name)
        suggested = pathlib.Path(folder or ".") / f"{safe}_{clip.start.astimezone():%Y%m%d_%H%M%S}.mp4"
        path, _filter = QFileDialog.getSaveFileName(self, "Save recording", str(suggested), "MP4 video (*.mp4)")
        if not path:
            return None
        out = pathlib.Path(path)
        return out if out.suffix.lower() == ".mp4" else out.with_suffix(".mp4")

    # ------------------------------------------------------------------ actions
    def _selected_day(self) -> date:
        return self._date.date().toPyDate()

    def _reload(self) -> None:
        self._list.clear()
        self._update_buttons()
        if self._camera_id is None:
            self._status.setText("")
            return
        self._status.setText("Loading recordings...")
        self._ctrl.load_recordings(self._camera_id, self._selected_day())

    def _download(self) -> None:
        item = self._list.currentItem()
        if self._camera_id is None or item is None or self._download_camera is not None:
            return
        clip = item.data(Qt.ItemDataRole.UserRole)
        out = self.choose_save_path(clip)
        if out is None:
            return
        self._download_camera = self._camera_id
        self._progress.setValue(0)
        self._status.setText(f"Downloading {clip_text(clip)}...")
        self._update_buttons()
        self._ctrl.download_recording(self._camera_id, clip, pathlib.Path(out))

    # ------------------------------------------------------------------ controller signals
    def _on_recordings(self, camera_id: str, day: object, clips: list) -> None:
        if camera_id != self._camera_id or day != self._selected_day():
            return  # stale answer for another camera/day
        self._list.clear()
        for clip in clips:
            item = QListWidgetItem(clip_text(clip))
            item.setData(Qt.ItemDataRole.UserRole, clip)
            label = EVENT_LABELS.get(clip.event_type, "Recording")
            item.setToolTip(f"{label}, {int(clip.duration_seconds)} s")
            self._list.addItem(item)
        if self._download_camera is None:
            self._status.setText(f"{len(clips)} recording(s)" if clips else "No recordings on this day")
        self._update_buttons()

    def _on_failed(self, camera_id: str, action: str, message: str) -> None:
        if camera_id == self._camera_id and action == "load_recordings":
            self._status.setText(f"Could not load recordings: {message}")

    def _on_progress(self, camera_id: str, value: float) -> None:
        if camera_id == self._download_camera:
            self._progress.setValue(max(self._progress.value(), int(round(value * 100))))

    def _on_finished(self, camera_id: str, path: str, error: str) -> None:
        if camera_id != self._download_camera:
            return
        self._download_camera = None
        if error:
            self._status.setText(f"Download failed: {error}")
        else:
            self._progress.setValue(self._progress.maximum())
            self._status.setText(f"Saved to {path}")
        self._update_buttons()

    def _on_cameras_changed(self) -> None:
        if self._camera_id is not None and all(c.id != self._camera_id for c in self._ctrl.cameras()):
            self.set_camera(None)

    # ------------------------------------------------------------------ helpers
    def _camera_name(self) -> str | None:
        return next((c.name for c in self._ctrl.cameras() if c.id == self._camera_id), None)

    def _update_buttons(self) -> None:
        busy = self._download_camera is not None
        self._button.setEnabled(not busy and self._camera_id is not None and self._list.currentItem() is not None)
        self._refresh.setEnabled(self._camera_id is not None)
