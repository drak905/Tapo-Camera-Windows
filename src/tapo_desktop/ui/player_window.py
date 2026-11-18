"""Playback window: SD-card recordings streamed inside the app, like the playback screen of the Tapo
phone app (Phase 5).

:class:`PlayerWindow` - non-modal, resizable top-level window, one per camera, objectName
``playerWindow_<camera_id>``. Children: ``playbackVideo`` (frames letterboxed, aspect ratio kept),
``playbackTimeline`` (:class:`PlaybackTimeline`: the local 24 h day with the recorded segments and a
moving cursor; click or drag-and-release seeks), ``playPauseButton``, ``speedCombo`` (1x/2x/4x/8x; item
data "1/1".."8/1"), ``prevClipButton`` / ``nextClipButton``, ``snapshotButton`` (PNG of the current
frame into ``snapshot_folder`` or the default snapshot folder), ``downloadClipButton`` (the clip at the
cursor; path from ``self.choose_save_path(clip)``), ``playbackTimeLabel`` (local ``HH:MM:SS`` of the
position), ``playbackDate`` (changing it stops playback, reloads the day's clips and starts at the first
one) and ``playbackStatus``. Closing the window stops the playback session (``closed`` is emitted).

Everything goes through the AppController: ``start_playback`` / ``pause_playback`` / ``resume_playback``
/ ``seek_playback`` / ``set_playback_speed`` / ``stop_playback`` / ``load_recordings`` /
``download_recording`` and its ``playback_frame`` / ``playback_state`` / ``playback_position`` /
``recordings_ready`` / ``download_*`` signals.
"""
from __future__ import annotations

import pathlib
from datetime import date, datetime, timedelta

from PyQt6.QtCore import QDate, QPointF, QRectF, QSize, QStandardPaths, Qt, pyqtSignal
from PyQt6.QtGui import QCloseEvent, QColor, QFont, QImage, QMouseEvent, QPainter, QPaintEvent, QPen
from PyQt6.QtWidgets import (
    QComboBox,
    QDateEdit,
    QFileDialog,
    QHBoxLayout,
    QLabel,
    QPushButton,
    QSizePolicy,
    QVBoxLayout,
    QWidget,
)

from tapo_desktop.models import RecordingClip

__all__ = ["PlaybackTimeline", "PlayerWindow", "SPEED_ITEMS", "local_day_range"]

#: (label, scale) of the speed selector.
SPEED_ITEMS: tuple[tuple[str, str], ...] = (("1x", "1/1"), ("2x", "2/1"), ("4x", "4/1"), ("8x", "8/1"))
END_OF_RECORDINGS = "end of recordings"  # PlaybackStreamSupervisor's STOPPED reason
_ACTIVE_STATES = {"IDLE", "CONNECTING", "PLAYING", "RECONNECTING"}
_RESTART_PREV_S = 3.0  # "previous" within this many seconds of a clip start goes to the clip before


def local_day_range(day: date) -> tuple[float, float]:
    """Epoch seconds of local midnight of ``day`` and of the next day (23 or 25 h on DST days)."""
    start = datetime(day.year, day.month, day.day).astimezone()
    nxt = day + timedelta(days=1)
    end = datetime(nxt.year, nxt.month, nxt.day).astimezone()
    return start.timestamp(), end.timestamp()


class PlaybackTimeline(QWidget):
    """The local day as a horizontal bar: recorded segments, hour ticks, the playback cursor.

    A click or a drag (seek on release) emits ``seek_requested(utc_seconds)``; while dragging the cursor
    follows the mouse and ``scrubbing(utc_seconds)`` is emitted.
    """

    seek_requested = pyqtSignal(float)
    scrubbing = pyqtSignal(float)

    _MARGIN = 10
    _BAR_COLOR = QColor(45, 49, 58)
    _SEGMENT_COLOR = QColor(58, 160, 255)
    _CURSOR_COLOR = QColor(255, 196, 0)

    def __init__(self, parent: QWidget | None = None):
        super().__init__(parent)
        self.setObjectName("playbackTimeline")
        self.setMinimumHeight(46)
        self.setSizePolicy(QSizePolicy.Policy.Expanding, QSizePolicy.Policy.Fixed)
        self.setCursor(Qt.CursorShape.PointingHandCursor)
        self.setToolTip("Click or drag to jump to a time")
        self._start, self._end = local_day_range(date.today())
        self._segments: list[tuple[float, float]] = []
        self._position: float | None = None
        self._drag: float | None = None

    # ------------------------------------------------------------------ model
    def set_day(self, day: date) -> None:
        self._start, self._end = local_day_range(day)
        self.update()

    def day_range(self) -> tuple[float, float]:
        return self._start, self._end

    def set_segments(self, segments: list[tuple[float, float]]) -> None:
        self._segments = sorted((float(s), float(e)) for s, e in segments)
        self.update()

    def segments(self) -> list[tuple[float, float]]:
        return list(self._segments)

    def set_position(self, utc: float | None) -> None:
        self._position = None if utc is None else float(utc)
        self.update()

    def position(self) -> float | None:
        return self._position

    def margin(self) -> int:
        return self._MARGIN

    def x_for(self, utc: float) -> float:
        """Widget x of recording time ``utc`` (clamped to the bar)."""
        span = max(1.0, self._end - self._start)
        frac = min(1.0, max(0.0, (float(utc) - self._start) / span))
        return self._MARGIN + frac * max(1, self.width() - 2 * self._MARGIN)

    def utc_at(self, x: float) -> float:
        """Recording time under widget x (clamped to the day)."""
        width = max(1, self.width() - 2 * self._MARGIN)
        frac = min(1.0, max(0.0, (float(x) - self._MARGIN) / width))
        return self._start + frac * (self._end - self._start)

    def sizeHint(self) -> QSize:
        return QSize(720, 52)

    # ------------------------------------------------------------------ mouse
    def mousePressEvent(self, event: QMouseEvent) -> None:
        if event.button() == Qt.MouseButton.LeftButton:
            self._drag = self.utc_at(event.position().x())
            self.scrubbing.emit(self._drag)
            self.update()
        super().mousePressEvent(event)

    def mouseMoveEvent(self, event: QMouseEvent) -> None:
        if self._drag is not None:
            self._drag = self.utc_at(event.position().x())
            self.scrubbing.emit(self._drag)
            self.update()
        super().mouseMoveEvent(event)

    def mouseReleaseEvent(self, event: QMouseEvent) -> None:
        if event.button() == Qt.MouseButton.LeftButton and self._drag is not None:
            utc = self.utc_at(event.position().x())
            self._drag = None
            self._position = utc
            self.update()
            self.seek_requested.emit(utc)
        super().mouseReleaseEvent(event)

    # ------------------------------------------------------------------ paint
    def paintEvent(self, event: QPaintEvent) -> None:
        painter = QPainter(self)
        painter.setRenderHint(QPainter.RenderHint.Antialiasing, False)
        h = self.height()
        bar = QRectF(self._MARGIN, 6, max(1, self.width() - 2 * self._MARGIN), h - 24)
        painter.fillRect(bar, self._BAR_COLOR)
        for start, end in self._segments:
            if end < self._start or start > self._end:
                continue
            x0, x1 = self.x_for(start), self.x_for(end)
            painter.fillRect(QRectF(x0, bar.top(), max(2.0, x1 - x0), bar.height()), self._SEGMENT_COLOR)
        painter.setPen(QPen(QColor(150, 155, 165)))
        font = QFont(painter.font())
        font.setPointSizeF(max(6.5, font.pointSizeF() - 1.5))
        painter.setFont(font)
        hours = int(round((self._end - self._start) / 3600))
        for hour in range(0, hours + 1, 3):
            x = self.x_for(self._start + hour * 3600)
            painter.drawLine(QPointF(x, bar.bottom()), QPointF(x, bar.bottom() + 4))
            painter.drawText(QRectF(x - 20, bar.bottom() + 4, 40, 14), Qt.AlignmentFlag.AlignCenter, f"{hour:02d}:00")
        cursor = self._drag if self._drag is not None else self._position
        if cursor is not None and self._start <= cursor <= self._end:
            x = self.x_for(cursor)
            painter.setPen(QPen(self._CURSOR_COLOR, 2))
            painter.drawLine(QPointF(x, 2), QPointF(x, bar.bottom() + 2))
        painter.end()


class _VideoView(QWidget):
    """Paints the latest playback frame aspect-fit on black, or a short message."""

    def __init__(self, parent: QWidget | None = None):
        super().__init__(parent)
        self.setObjectName("playbackVideo")
        self.setMinimumSize(320, 180)
        self.setSizePolicy(QSizePolicy.Policy.Expanding, QSizePolicy.Policy.Expanding)
        self._image: QImage | None = None
        self._message = "Loading..."

    def set_image(self, image: QImage | None) -> None:
        self._image = image
        self.update()

    def set_message(self, text: str) -> None:
        self._message = text
        self.update()

    def fit_rect(self, img_w: int, img_h: int) -> QRectF:
        """Largest rect with the image's aspect ratio centred in the widget (letterbox)."""
        scale = min(self.width() / max(1, img_w), self.height() / max(1, img_h))
        w, h = img_w * scale, img_h * scale
        return QRectF((self.width() - w) / 2, (self.height() - h) / 2, w, h)

    def sizeHint(self) -> QSize:
        return QSize(960, 540)

    def paintEvent(self, event: QPaintEvent) -> None:
        painter = QPainter(self)
        painter.fillRect(self.rect(), QColor(0, 0, 0))
        image = self._image
        if image is not None and not image.isNull():
            painter.setRenderHint(QPainter.RenderHint.SmoothPixmapTransform, True)
            painter.drawImage(self.fit_rect(image.width(), image.height()), image)
        if self._message:
            painter.setPen(QColor(235, 235, 235))
            painter.drawText(self.rect(), Qt.AlignmentFlag.AlignCenter, self._message)
        painter.end()


class PlayerWindow(QWidget):
    """Playback screen of one camera (see the module docstring). ``clips`` = the clips of ``day`` (when
    None they are loaded); playback starts at ``start`` (default: the first clip)."""

    closed = pyqtSignal(str)  # camera_id, after the window was closed (playback stopped)

    def __init__(self, controller, camera_id: str, *, day: date | None = None,
                 clips: list[RecordingClip] | None = None, start: datetime | float | None = None,
                 parent: QWidget | None = None):
        super().__init__(parent)
        self.setWindowFlag(Qt.WindowType.Window, True)
        self.setObjectName(f"playerWindow_{camera_id}")
        self.setAttribute(Qt.WidgetAttribute.WA_DeleteOnClose, False)
        self._ctrl = controller
        self._camera_id = camera_id
        self._day = day or date.today()
        self._clips: list[RecordingClip] = []
        self._pending_start: float | None = None  # where to start once the day's clips arrived
        self._awaiting_clips = False
        self._active = False  # a playback session was requested and has not ended
        self._paused = False
        self._state = "IDLE"
        self._position: float | None = None
        self._image: QImage | None = None
        self._download_busy = False
        self.snapshot_folder: pathlib.Path | None = None  # None = the default snapshot folder
        self._build()
        self.setWindowTitle(f"Playback - {self._camera_name()}")
        self.resize(960, 680)
        controller.playback_frame.connect(self._on_frame)
        controller.playback_state.connect(self._on_state)
        controller.playback_position.connect(self._on_position)
        controller.recordings_ready.connect(self._on_recordings)
        controller.command_failed.connect(self._on_command_failed)
        controller.download_progress.connect(self._on_download_progress)
        controller.download_finished.connect(self._on_download_finished)
        controller.cameras_changed.connect(self._on_cameras_changed)
        first = _epoch(start) if start is not None else None
        if clips is None:
            self._load_day(first)
        else:
            self._set_clips(clips)
            target = first if first is not None else (self._clips[0].start.timestamp() if self._clips else None)
            if target is not None:
                self._play_at(target)
            else:
                self._set_status("No recordings on this day")
        self._update_controls()

    # ------------------------------------------------------------------ construction
    def _build(self) -> None:
        root = QVBoxLayout(self)
        root.setContentsMargins(8, 8, 8, 8)
        root.setSpacing(6)
        top = QHBoxLayout()
        self._title = QLabel(self._camera_name(), self)
        self._title.setObjectName("playbackCameraName")
        title_font = QFont(self._title.font())
        title_font.setBold(True)
        self._title.setFont(title_font)
        top.addWidget(self._title)
        top.addSpacing(12)
        top.addWidget(QLabel("Day", self))
        self._date = QDateEdit(self)
        self._date.setObjectName("playbackDate")
        self._date.setCalendarPopup(True)
        self._date.setDisplayFormat("yyyy-MM-dd")
        self._date.setMaximumDate(QDate.currentDate().addDays(1))
        self._date.setDate(QDate(self._day.year, self._day.month, self._day.day))
        self._date.dateChanged.connect(self._on_date_changed)
        top.addWidget(self._date)
        top.addSpacing(12)
        self._status = QLabel("", self)
        self._status.setObjectName("playbackStatus")
        self._status.setWordWrap(True)
        top.addWidget(self._status, 1)
        root.addLayout(top)

        self._video = _VideoView(self)
        root.addWidget(self._video, 1)
        self._timeline = PlaybackTimeline(self)
        self._timeline.set_day(self._day)
        self._timeline.seek_requested.connect(self._play_at)
        self._timeline.scrubbing.connect(self._show_time)
        root.addWidget(self._timeline)

        row = QHBoxLayout()
        self._prev = self._button("Previous clip", "prevClipButton", self._prev_clip, row)
        self._play = self._button("Play", "playPauseButton", self._toggle_play, row)
        self._next = self._button("Next clip", "nextClipButton", self._next_clip, row)
        self._speed = QComboBox(self)
        self._speed.setObjectName("speedCombo")
        self._speed.setToolTip("Playback speed")
        for label, scale in SPEED_ITEMS:
            self._speed.addItem(label, scale)
        self._speed.currentIndexChanged.connect(self._on_speed)
        row.addWidget(self._speed)
        row.addSpacing(12)
        self._time = QLabel("--:--:--", self)
        self._time.setObjectName("playbackTimeLabel")
        time_font = QFont(self._time.font())
        time_font.setPointSizeF(time_font.pointSizeF() + 2)
        time_font.setBold(True)
        self._time.setFont(time_font)
        row.addWidget(self._time)
        row.addStretch(1)
        self._snap = self._button("Snapshot", "snapshotButton", self._snapshot, row)
        self._download = self._button("Download clip...", "downloadClipButton", self._download_clip, row)
        root.addLayout(row)

    def _button(self, text: str, name: str, slot, row: QHBoxLayout) -> QPushButton:
        button = QPushButton(text, self)
        button.setObjectName(name)
        button.clicked.connect(slot)
        row.addWidget(button)
        return button

    # ------------------------------------------------------------------ public API
    def camera_id(self) -> str:
        return self._camera_id

    def day(self) -> date:
        return self._day

    def clips(self) -> list[RecordingClip]:
        return list(self._clips)

    def position(self) -> float | None:
        """Recording time (UTC epoch s) currently shown, None before the first report."""
        return self._position

    def current_image(self) -> QImage | None:
        return self._image

    def playback_state(self) -> str:
        """Last StreamState name reported for this window's playback ("IDLE" before)."""
        return self._state

    def is_paused(self) -> bool:
        return self._paused

    def play_clip(self, clip: RecordingClip, clips: list[RecordingClip] | None = None,
                  day: date | None = None) -> None:
        """Jump to ``clip`` (switching to ``day`` / ``clips`` when given) and play."""
        if day is not None and day != self._day:
            self._day = day
            self._date.blockSignals(True)
            self._date.setDate(QDate(day.year, day.month, day.day))
            self._date.blockSignals(False)
            self._timeline.set_day(day)
            if self._active:
                self._ctrl.stop_playback(self._camera_id)
                self._active = False
        if clips is not None:
            self._set_clips(clips)
        self._play_at(clip.start.timestamp())

    def choose_save_path(self, clip: RecordingClip) -> pathlib.Path | None:
        """Ask for the MP4 path of ``clip`` (tests replace this on the instance)."""
        folder = QStandardPaths.writableLocation(QStandardPaths.StandardLocation.MoviesLocation)
        safe = "".join(ch if ch.isalnum() or ch in "-_" else "_" for ch in self._camera_name())
        suggested = pathlib.Path(folder or ".") / f"{safe}_{clip.start.astimezone():%Y%m%d_%H%M%S}.mp4"
        path, _filter = QFileDialog.getSaveFileName(self, "Save recording", str(suggested), "MP4 video (*.mp4)")
        if not path:
            return None
        out = pathlib.Path(path)
        return out if out.suffix.lower() == ".mp4" else out.with_suffix(".mp4")

    # ------------------------------------------------------------------ playback actions
    def _day_end(self) -> float:
        return self._timeline.day_range()[1]

    def _play_at(self, utc: float) -> None:
        """Seek the running session to ``utc``, or start a new one there."""
        utc = float(utc)
        self._position = utc
        self._timeline.set_position(utc)
        self._show_time(utc)
        if self._active:
            self._ctrl.seek_playback(self._camera_id, utc)
        else:
            self._ctrl.start_playback(self._camera_id, utc, max(self._day_end(), utc + 1), clips=self._clips)
            self._active = True
            self._state = "CONNECTING"
            scale = self._speed.currentData()
            if scale and scale != "1/1":
                self._ctrl.set_playback_speed(self._camera_id, scale)
            self._set_status("Loading...")
        self._paused = False
        self._update_controls()

    def _toggle_play(self) -> None:
        if self._active and not self._paused:
            self._ctrl.pause_playback(self._camera_id)
            self._paused = True
        elif self._active:
            self._ctrl.resume_playback(self._camera_id)
            self._paused = False
        else:
            target = self._position
            if target is None or not self._inside_any_clip(target):
                target = self._clips[0].start.timestamp() if self._clips else None
            if target is None:
                self._set_status("No recordings on this day")
                return
            self._play_at(target)
            return
        self._update_controls()

    def _on_speed(self, _index: int) -> None:
        scale = self._speed.currentData()
        if scale and self._active:
            self._ctrl.set_playback_speed(self._camera_id, scale)

    def _clip_index(self, utc: float) -> int | None:
        """Index of the clip containing ``utc`` (1 s slack), else of the last clip starting before it."""
        before = None
        for i, clip in enumerate(self._clips):
            start, end = clip.start.timestamp(), clip.end.timestamp()
            if start - 1 <= utc <= end + 1:
                return i
            if start <= utc:
                before = i
        return before

    def _inside_any_clip(self, utc: float) -> bool:
        return any(c.start.timestamp() - 1 <= utc <= c.end.timestamp() + 1 for c in self._clips)

    def _next_clip(self) -> None:
        if not self._clips:
            return
        pos = self._position if self._position is not None else self._clips[0].start.timestamp() - 1
        i = self._clip_index(pos)
        floor = self._clips[i].start.timestamp() if i is not None else pos
        nxt = next((c for c in self._clips if c.start.timestamp() > floor + 0.5), None)
        if nxt is None:
            self._set_status("No later recordings on this day")
            return
        self._play_at(nxt.start.timestamp())

    def _prev_clip(self) -> None:
        if not self._clips:
            return
        pos = self._position if self._position is not None else self._clips[0].start.timestamp()
        i = self._clip_index(pos)
        if i is None:
            target = self._clips[0]
        elif pos - self._clips[i].start.timestamp() > _RESTART_PREV_S or i == 0:
            target = self._clips[i]
        else:
            target = self._clips[i - 1]
        self._play_at(target.start.timestamp())

    def _snapshot(self) -> None:
        from tapo_desktop.ui.snapshot import save_snapshot

        if self._image is None:
            self._set_status("No picture to save yet")
            return
        stamp = datetime.fromtimestamp(self._position) if self._position is not None else None
        try:
            path = save_snapshot(self._image, self._camera_name(), now=stamp, folder=self.snapshot_folder)
        except (OSError, ValueError) as exc:
            self._set_status(f"Could not save the snapshot: {exc}")
            return
        self._set_status(f"Snapshot saved to {path}")

    def _download_clip(self) -> None:
        if self._download_busy or not self._clips:
            return
        pos = self._position if self._position is not None else self._clips[0].start.timestamp()
        i = self._clip_index(pos)
        clip = self._clips[i if i is not None else 0]
        out = self.choose_save_path(clip)
        if out is None:
            return
        self._download_busy = True
        self._update_controls()
        self._set_status(f"Downloading {clip.start.astimezone():%H:%M:%S}...")
        self._ctrl.download_recording(self._camera_id, clip, pathlib.Path(out))

    # ------------------------------------------------------------------ day / clips
    def _set_clips(self, clips: list[RecordingClip]) -> None:
        self._clips = sorted(clips, key=lambda c: c.start)
        self._timeline.set_segments([(c.start.timestamp(), c.end.timestamp()) for c in self._clips])
        self._update_controls()

    def _load_day(self, start: float | None = None) -> None:
        self._pending_start = start
        self._awaiting_clips = True
        self._set_clips([])
        self._set_status("Loading recordings...")
        self._ctrl.load_recordings(self._camera_id, self._day)

    def _on_date_changed(self, qdate: QDate) -> None:
        day = qdate.toPyDate()
        if day == self._day:
            return
        self._day = day
        if self._active:
            self._ctrl.stop_playback(self._camera_id)
            self._active = False
        self._paused = False
        self._image = None
        self._video.set_image(None)
        self._position = None
        self._timeline.set_day(day)
        self._timeline.set_position(None)
        self._time.setText("--:--:--")
        self._load_day()

    def _on_recordings(self, camera_id: str, day: object, clips: list) -> None:
        if camera_id != self._camera_id or day != self._day:
            return
        self._set_clips(list(clips))
        if not self._awaiting_clips:
            return
        self._awaiting_clips = False
        start, self._pending_start = self._pending_start, None
        if not self._clips:
            self._set_status("No recordings on this day")
            self._video.set_message("No recordings on this day")
            self._update_controls()
            return
        self._play_at(start if start is not None else self._clips[0].start.timestamp())

    def _on_command_failed(self, camera_id: str, action: str, message: str) -> None:
        if camera_id != self._camera_id:
            return
        if action == "load_recordings" and self._awaiting_clips:
            self._awaiting_clips = False
            self._set_status(f"Could not load recordings: {message}")
        elif action.endswith("_playback"):
            self._set_status(message)

    # ------------------------------------------------------------------ controller signals
    def _on_frame(self, camera_id: str, image: QImage) -> None:
        if camera_id != self._camera_id or not self._active:
            return
        self._image = image
        self._video.set_message("")
        self._video.set_image(image)

    def _on_state(self, camera_id: str, state: str, reason: str) -> None:
        if camera_id != self._camera_id:
            return
        self._state = state
        if state in ("CONNECTING", "IDLE"):
            self._set_status("Loading...")
        elif state == "PLAYING":
            self._set_status("")
        elif state == "RECONNECTING":
            self._set_status(f"Reconnecting... ({reason})" if reason else "Reconnecting...")
        elif state == "FAILED":
            self._active = self._paused = False
            text = reason or "Playback failed"
            self._set_status(text)
            self._video.set_message(text)
        elif state == "STOPPED":
            self._active = self._paused = False
            if reason == END_OF_RECORDINGS:
                self._set_status("End of recordings")
        self._update_controls()

    def _on_position(self, camera_id: str, utc: float) -> None:
        if camera_id != self._camera_id or not self._active:
            return
        self._position = float(utc)
        self._timeline.set_position(self._position)
        self._show_time(self._position)

    def _on_download_progress(self, camera_id: str, value: float) -> None:
        if camera_id == self._camera_id and self._download_busy:
            self._set_status(f"Downloading... {int(round(value * 100))}%")

    def _on_download_finished(self, camera_id: str, path: str, error: str) -> None:
        if camera_id != self._camera_id or not self._download_busy:
            return
        self._download_busy = False
        self._set_status(f"Download failed: {error}" if error else f"Saved to {path}")
        self._update_controls()

    # ------------------------------------------------------------------ helpers
    def _on_cameras_changed(self) -> None:
        """Phase 7: a rename shows at once in the title bar and the header."""
        name = self._camera_name()
        self.setWindowTitle(f"Playback - {name}")
        self._title.setText(name)

    def _camera_name(self) -> str:
        cfg = self._ctrl.camera(self._camera_id) if hasattr(self._ctrl, "camera") else None
        return cfg.name if cfg is not None else self._camera_id

    def _show_time(self, utc: float) -> None:
        self._time.setText(datetime.fromtimestamp(float(utc)).strftime("%H:%M:%S"))

    def _set_status(self, text: str) -> None:
        self._status.setText(text)

    def _update_controls(self) -> None:
        playing = self._active and not self._paused
        self._play.setText("Pause" if playing else "Play")
        has_clips = bool(self._clips)
        self._play.setEnabled(has_clips or self._active)
        self._prev.setEnabled(has_clips)
        self._next.setEnabled(has_clips)
        self._download.setEnabled(has_clips and not self._download_busy)

    def closeEvent(self, event: QCloseEvent) -> None:
        """Closing the window ends the playback session."""
        if self._active:
            self._active = False
        self._ctrl.stop_playback(self._camera_id)
        super().closeEvent(event)
        self.closed.emit(self._camera_id)


def _epoch(value: datetime | float) -> float:
    return value.timestamp() if isinstance(value, datetime) else float(value)
