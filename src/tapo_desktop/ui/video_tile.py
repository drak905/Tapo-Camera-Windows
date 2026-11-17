"""Single camera video tile (contract: docs/CONTRACT_UI.md).

objectName ``videoTile_<camera_id>``. Paints the latest frame aspect-fit (letterboxed) and an
overlay headline for non-playing states. ``status_text()`` returns exactly the overlay headline:

* ``CONNECTING`` -> ``"Connecting..."``
* ``RECONNECTING`` -> ``"Reconnecting..."``
* ``FAILED`` / ``STOPPED`` -> ``"Offline"``
* ``PLAYING`` / ``IDLE`` -> ``""`` (overlay hidden; the frame is fully visible)

The reason string (if any) may be shown as a second line / tooltip, never inside ``status_text()``.
Single click emits ``clicked(camera_id)``; double click emits ``double_clicked(camera_id)``.
"""
from __future__ import annotations

from PyQt6.QtCore import QRect, QRectF, QSize, Qt, pyqtSignal
from PyQt6.QtGui import QColor, QFont, QImage, QMouseEvent, QPainter, QPaintEvent, QPen
from PyQt6.QtWidgets import QSizePolicy, QWidget

from tapo_desktop.ui import _theme

OVERLAY_TEXT: dict[str, str] = {
    "IDLE": "",
    "CONNECTING": "Connecting...",
    "PLAYING": "",
    "RECONNECTING": "Reconnecting...",
    "STOPPED": "Offline",
    "FAILED": "Offline",
}

_BACKGROUND = QColor(10, 11, 13)
_OVERLAY = QColor(0, 0, 0, 150)
_LABEL_BG = QColor(0, 0, 0, 140)
_TEXT = QColor(235, 237, 240)
_MUTED = QColor(160, 165, 172)
_BORDER = 2


class VideoTile(QWidget):
    clicked = pyqtSignal(str)
    double_clicked = pyqtSignal(str)

    def __init__(self, camera_id: str, name: str = "", parent: QWidget | None = None):
        super().__init__(parent)
        self._camera_id = camera_id
        self._name = name
        self._image: QImage | None = None
        self._state = "IDLE"
        self._reason = ""
        self._selected = False
        self.setObjectName(f"videoTile_{camera_id}")
        self.setAttribute(Qt.WidgetAttribute.WA_OpaquePaintEvent, True)
        self.setFocusPolicy(Qt.FocusPolicy.ClickFocus)
        self.setSizePolicy(QSizePolicy.Policy.Expanding, QSizePolicy.Policy.Expanding)
        self.setMinimumSize(160, 90)

    # ------------------------------------------------------------------ public API
    @property
    def camera_id(self) -> str:
        return self._camera_id

    def name(self) -> str:
        return self._name

    def set_name(self, name: str) -> None:
        if name != self._name:
            self._name = name
            self.update()

    def set_frame(self, image: QImage) -> None:
        """Show ``image`` (repaint scheduled)."""
        self._image = image
        self.update()

    def current_image(self) -> QImage | None:
        """The last image passed to ``set_frame`` (lane addition; None before the first frame)."""
        return self._image

    def set_stream_state(self, state_name: str, reason: str = "") -> None:
        """Update the overlay from a ``StreamState.name`` (see module docstring)."""
        self._state = state_name if state_name in OVERLAY_TEXT else "FAILED"
        self._reason = reason or ""
        self.setToolTip(self._reason)
        self.update()

    def stream_state(self) -> str:
        return self._state

    def status_text(self) -> str:
        return OVERLAY_TEXT[self._state]

    def set_selected(self, selected: bool) -> None:
        if selected != self._selected:
            self._selected = selected
            self.update()

    def is_selected(self) -> bool:
        return self._selected

    def sizeHint(self) -> QSize:
        return QSize(480, 270)

    # ------------------------------------------------------------------ events
    def mousePressEvent(self, event: QMouseEvent) -> None:
        if event.button() == Qt.MouseButton.LeftButton:
            self.clicked.emit(self._camera_id)
        super().mousePressEvent(event)

    def mouseDoubleClickEvent(self, event: QMouseEvent) -> None:
        if event.button() == Qt.MouseButton.LeftButton:
            self.double_clicked.emit(self._camera_id)
        super().mouseDoubleClickEvent(event)

    def paintEvent(self, event: QPaintEvent) -> None:
        painter = QPainter(self)
        try:
            painter.fillRect(self.rect(), _BACKGROUND)
            if self._image is not None and not self._image.isNull():
                painter.setRenderHint(QPainter.RenderHint.SmoothPixmapTransform, True)
                painter.drawImage(self._fit_rect(self._image.width(), self._image.height()), self._image)
            headline = self.status_text()
            if headline:
                self._paint_overlay(painter, headline)
            elif self._image is None:
                self._paint_center(painter, self._name or self._camera_id, _MUTED, 11)
            self._paint_name(painter)
            if self._selected:
                pen = QPen(_theme.ACCENT)
                pen.setWidth(_BORDER)
                painter.setPen(pen)
                painter.setBrush(Qt.BrushStyle.NoBrush)
                painter.drawRect(self.rect().adjusted(1, 1, -1, -1))
        finally:
            painter.end()

    # ------------------------------------------------------------------ painting helpers
    def _fit_rect(self, img_w: int, img_h: int) -> QRectF:
        """Largest rect with the image's aspect ratio centred in the widget (letterbox)."""
        w, h = self.width(), self.height()
        scale = min(w / img_w, h / img_h)
        fw, fh = img_w * scale, img_h * scale
        return QRectF((w - fw) / 2, (h - fh) / 2, fw, fh)

    def _paint_overlay(self, painter: QPainter, headline: str) -> None:
        painter.fillRect(self.rect(), _OVERLAY)
        self._paint_center(painter, headline, _TEXT, 14, bold=True)
        if self._reason:
            font = QFont(self.font())
            font.setPointSize(9)
            painter.setFont(font)
            painter.setPen(_MUTED)
            rect = self.rect().adjusted(8, self.height() // 2 + 14, -8, -8)
            flags = (Qt.AlignmentFlag.AlignHCenter.value | Qt.AlignmentFlag.AlignTop.value
                     | Qt.TextFlag.TextWordWrap.value)
            painter.drawText(rect, int(flags), self._reason)

    def _paint_center(self, painter: QPainter, text: str, color: QColor, size: int, bold: bool = False) -> None:
        font = QFont(self.font())
        font.setPointSize(size)
        font.setBold(bold)
        painter.setFont(font)
        painter.setPen(color)
        painter.drawText(self.rect(), Qt.AlignmentFlag.AlignCenter, text)

    def _paint_name(self, painter: QPainter) -> None:
        if not self._name:
            return
        font = QFont(self.font())
        font.setPointSize(9)
        painter.setFont(font)
        metrics = painter.fontMetrics()
        text = metrics.elidedText(self._name, Qt.TextElideMode.ElideRight, max(40, self.width() - 24))
        box = QRect(6, self.height() - metrics.height() - 10, metrics.horizontalAdvance(text) + 12,
                    metrics.height() + 4)
        painter.fillRect(box, _LABEL_BG)
        painter.setPen(_TEXT)
        painter.drawText(box, Qt.AlignmentFlag.AlignCenter, text)
