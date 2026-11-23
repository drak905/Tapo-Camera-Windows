"""SD-card playback (contract: docs/CONTRACT_UI.md). objectName ``playbackPanel``.

Children: ``recordingDate`` (QDateEdit; changing it, or ``set_camera``, calls
``controller.load_recordings(id, date)``), ``recordingsList`` (one row per clip, text
``"HH:MM:SS - HH:MM:SS"`` of ``clip.start``/``clip.end`` converted to LOCAL time
(``dt.astimezone()``), rows in the order received; ``Qt.UserRole`` data = the RecordingClip),
``downloadButton`` (downloads the selected row: path from ``self.choose_save_path(clip)``;
``None`` = cancelled) and ``downloadProgress`` (QProgressBar 0..100; reaches its maximum when
``download_finished`` reports success).

Phase 5: ``playButton`` (or double-clicking a clip row) opens the camera's
:class:`~tapo_desktop.ui.player_window.PlayerWindow` at that clip (one window per camera: an open one is
raised and jumps to the clip); ``player_window(camera_id)`` returns the open window.
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
from tapo_desktop.ui.kami_status import control_enabled
from tapo_desktop.ui.player_window import PlayerWindow
from tapo_desktop.ui.vendor_status import capability_service, playback_profile, recordings_unsupported_text

PLAYBACK_UNSUPPORTED_TEXT = "Recordings are not supported for this Kami camera"

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
        self._from_cache = False  # a list shown from the profile's cache must not renew the cache
        self._players: dict[str, PlayerWindow] = {}  # open playback windows, one per camera

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
        self._date.dateChanged.connect(self._on_day_changed)
        row.addWidget(self._date, 1)
        self._refresh = QPushButton("Refresh", self)
        self._refresh.setObjectName("refreshRecordingsButton")
        self._refresh.clicked.connect(self._on_refresh_clicked)
        row.addWidget(self._refresh)
        root.addLayout(row)

        self._list = QListWidget(self)
        self._list.setObjectName("recordingsList")
        self._list.itemSelectionChanged.connect(self._update_buttons)
        self._list.itemDoubleClicked.connect(lambda _item: self._play())
        root.addWidget(self._list, 1)

        self._status = QLabel("", self)
        self._status.setObjectName("downloadStatus")
        self._status.setWordWrap(True)
        root.addWidget(self._status)
        self._play_button = QPushButton("Play", self)
        self._play_button.setObjectName("playButton")
        self._play_button.setToolTip("Watch the selected recording (double-click a clip does the same)")
        self._play_button.clicked.connect(self._play)
        root.addWidget(self._play_button)
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
    def player_window(self, camera_id: str) -> PlayerWindow | None:
        """The open playback window of ``camera_id``, or None."""
        return self._players.get(camera_id)

    def _play(self) -> None:
        """Open (or raise) the camera's PlayerWindow at the selected clip."""
        item = self._list.currentItem()
        if self._camera_id is None or item is None:
            return
        clip = item.data(Qt.ItemDataRole.UserRole)
        clips = [self._list.item(i).data(Qt.ItemDataRole.UserRole) for i in range(self._list.count())]
        day = self._selected_day()
        window = self._players.get(self._camera_id)
        if window is None:
            window = PlayerWindow(self._ctrl, self._camera_id, day=day, clips=clips, start=clip.start)
            window.closed.connect(self._player_closed)
            self._players[self._camera_id] = window
        else:
            window.play_clip(clip, clips, day)
        window.show()
        window.raise_()
        window.activateWindow()

    def _player_closed(self, camera_id: str) -> None:
        self._players.pop(camera_id, None)

    def _selected_day(self) -> date:
        return self._date.date().toPyDate()

    def _on_day_changed(self, *_args) -> None:
        self._reload()

    def _on_refresh_clicked(self, *_args) -> None:
        self._reload(refresh=True)

    def _reload(self, refresh: bool = False) -> None:
        self._list.clear()
        self._update_buttons()
        if self._camera_id is None:
            self._status.setText("")
            return
        if not self._playback_supported():
            cfg = next((c for c in self._ctrl.cameras() if c.id == self._camera_id), None)
            self._status.setText(recordings_unsupported_text(cfg, PLAYBACK_UNSUPPORTED_TEXT))
            return
        profile = self._profile(self._camera_id)
        if profile is not None and profile.on_demand_list and self._profile_idle(profile, refresh):
            return
        self._status.setText("Loading recordings...")
        self._ctrl.load_recordings(self._camera_id, self._selected_day())

    def _profile(self, camera_id: str | None):
        """The playback profile of a camera that is read on demand (iMou), else None (Tapo and Kami: unchanged)."""
        return playback_profile(next((c for c in self._ctrl.cameras() if c.id == camera_id), None))

    def _profile_idle(self, profile, refresh: bool) -> bool:
        """True = the cached answer, or the hint, is shown instead of asking the camera (only Refresh asks)."""
        if refresh:
            profile.cache.forget(self._camera_id)
            return False
        day = self._selected_day()
        hit = profile.cache.get(self._camera_id, day)
        if hit is None:
            self._status.setText(profile.hint_text)
        elif hit.error:
            self._status.setText(f"Could not load recordings: {hit.error}")
        else:
            self._from_cache = True
            try:
                self._on_recordings(self._camera_id, day, hit.clips)
            finally:
                self._from_cache = False
        return True

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
        profile = self._profile(camera_id)
        if profile is not None and not self._from_cache:
            profile.cache.put_clips(camera_id, day, clips)  # type: ignore[arg-type]
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
            note = profile.note(camera_id) if profile is not None else ""
            if note and clips:
                self._status.setText(f"{self._status.text()}. {note}")
        self._update_buttons()

    def _on_failed(self, camera_id: str, action: str, message: str) -> None:
        if camera_id == self._camera_id and action == "load_recordings":
            profile = self._profile(camera_id)
            if profile is not None and profile.is_no_card(message):
                profile.cache.put_no_card(camera_id, message)  # remembered until Refresh
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

    def _playback_supported(self) -> bool:
        """False for a camera whose service has no recordings (Kami); Tapo cameras are always supported."""
        cfg = next((c for c in self._ctrl.cameras() if c.id == self._camera_id), None)
        return control_enabled(capability_service(cfg), "playback")

    def _update_buttons(self) -> None:
        busy = self._download_camera is not None
        selected = self._camera_id is not None and self._list.currentItem() is not None
        profile = self._profile(self._camera_id)
        allow_download = profile is None or profile.allow_download
        self._button.setEnabled(not busy and selected and allow_download)
        self._button.setToolTip("" if allow_download else profile.no_download_text)
        self._play_button.setEnabled(selected)
        self._refresh.setEnabled(self._camera_id is not None and self._playback_supported())
