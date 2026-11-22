"""Single camera video tile (contract: docs/CONTRACT_UI.md).

objectName ``videoTile_<camera_id>``. Paints the latest frame aspect-fit (letterboxed) and an
overlay headline for non-playing states. ``status_text()`` returns exactly the overlay headline:

* ``CONNECTING`` -> ``"Connecting..."``
* ``RECONNECTING`` -> ``"Reconnecting..."``
* ``FAILED`` / ``STOPPED`` -> ``"Offline"``
* ``PLAYING`` / ``IDLE`` -> ``""`` (overlay hidden; the frame is fully visible)
* Phase 3: ``FAILED`` with reason ``"Remote - click to view"`` (remote stream waiting for a relay
  slot) -> that text instead of ``"Offline"``.

The reason string (if any) may be shown as a second line / tooltip, never inside ``status_text()``.
Single click emits ``clicked(camera_id)``; double click emits ``double_clicked(camera_id)``.

Phase 5 (grid slots):

* Overlay and name fonts shrink with the tile (:meth:`VideoTile.overlay_font_sizes`) so 5x5 grids stay
  readable; the name label is elided to the tile width (:meth:`VideoTile.display_name`).
* Drag and drop: the payload is the camera id in MIME type :data:`CAMERA_MIME`
  (:func:`camera_mime_data` / :func:`camera_id_from_mime`). With :meth:`set_drag_enabled` a tile can
  be dragged; with ``setAcceptDrops(True)`` a tile (or an :class:`EmptySlotTile`) emits
  ``camera_dropped(camera_id)`` when a camera is dropped on it. The grid decides what that means.
* :class:`EmptySlotTile` (objectName ``emptySlot``) is a grid position without a camera: it shows
  "Empty" and a hint and never streams.
"""
from __future__ import annotations

from PyQt6.QtCore import QMimeData, QPoint, QRect, QRectF, QSize, Qt, pyqtSignal
from PyQt6.QtGui import (
    QColor,
    QDrag,
    QDragEnterEvent,
    QDragMoveEvent,
    QDropEvent,
    QFont,
    QFontMetrics,
    QImage,
    QMouseEvent,
    QPainter,
    QPaintEvent,
    QPen,
)
from PyQt6.QtWidgets import QApplication, QSizePolicy, QWidget

from tapo_desktop.services.remote import REMOTE_CLICK_TO_VIEW
from tapo_desktop.ui import _theme, imou_status
from tapo_desktop.ui.kami_status import LIVE_VIEW_UNSUPPORTED_TEXT

OVERLAY_TEXT: dict[str, str] = {
    "IDLE": "",
    "CONNECTING": "Connecting...",
    "PLAYING": "",
    "RECONNECTING": "Reconnecting...",
    "STOPPED": "Offline",
    "FAILED": "Offline",
}
#: FAILED reasons that replace the "Offline" headline (Phase 3: remote tile waiting for a relay slot; Kami:
#: a camera whose live view is not supported).
REASON_HEADLINES: dict[str, str] = {REMOTE_CLICK_TO_VIEW: REMOTE_CLICK_TO_VIEW,
                                    LIVE_VIEW_UNSUPPORTED_TEXT: LIVE_VIEW_UNSUPPORTED_TEXT}

#: MIME type of a dragged camera (payload: the camera id, UTF-8).
CAMERA_MIME = "application/x-tapo-camera-id"
EMPTY_TEXT = "Empty"
EMPTY_HINT = "Right-click or drag a camera here"

_MAX_ID_BYTES = 256
_BACKGROUND = QColor(10, 11, 13)
_EMPTY_BACKGROUND = QColor(18, 20, 23)
_EMPTY_BORDER = QColor(44, 48, 54)
_OVERLAY = QColor(0, 0, 0, 150)
_LABEL_BG = QColor(0, 0, 0, 140)
_TEXT = QColor(235, 237, 240)
_MUTED = QColor(160, 165, 172)
_BORDER = 2
#: Minimum tile size: small enough for 5x5 next to both docks in a 1100 px wide window.
MIN_TILE_SIZE = QSize(96, 54)


def camera_mime_data(camera_id: str) -> QMimeData:
    """Drag payload carrying ``camera_id``."""
    mime = QMimeData()
    mime.setData(CAMERA_MIME, camera_id.encode("utf-8"))
    return mime


def camera_id_from_mime(mime: QMimeData | None) -> str | None:
    """Camera id of a :func:`camera_mime_data` payload, or None for anything else / malformed data."""
    if mime is None or not mime.hasFormat(CAMERA_MIME):
        return None
    raw = bytes(mime.data(CAMERA_MIME))
    if not raw or len(raw) > _MAX_ID_BYTES:
        return None
    try:
        text = raw.decode("utf-8")
    except UnicodeDecodeError:
        return None
    return text if text.isprintable() and text.strip() else None


def _font_sizes(width: int, height: int) -> tuple[int, int, int]:
    """(headline, detail, label) point sizes for a tile of ``width`` x ``height`` pixels."""
    if width >= 360 and height >= 220:
        return 14, 9, 9
    if width >= 220 and height >= 130:
        return 11, 8, 8
    return 9, 7, 7


class _SlotWidget(QWidget):
    """Common base of grid positions: drop target for :data:`CAMERA_MIME` payloads."""

    camera_dropped = pyqtSignal(str)

    def __init__(self, parent: QWidget | None = None):
        super().__init__(parent)
        self._drop_hover = False
        self.setSizePolicy(QSizePolicy.Policy.Expanding, QSizePolicy.Policy.Expanding)
        self.setMinimumSize(MIN_TILE_SIZE)

    def overlay_font_sizes(self) -> tuple[int, int, int]:
        """(headline, detail, name label) point sizes used at the current widget size."""
        return _font_sizes(self.width(), self.height())

    def _font(self, size: int, bold: bool = False) -> QFont:
        font = QFont(self.font())
        font.setPointSize(size)
        font.setBold(bold)
        return font

    # ------------------------------------------------------------------ drop target
    def dragEnterEvent(self, event: QDragEnterEvent) -> None:
        if camera_id_from_mime(event.mimeData()) is None:
            event.ignore()
            return
        event.setDropAction(Qt.DropAction.MoveAction)
        event.accept()
        self._set_drop_hover(True)

    def dragMoveEvent(self, event: QDragMoveEvent) -> None:
        if camera_id_from_mime(event.mimeData()) is None:
            event.ignore()
            return
        event.setDropAction(Qt.DropAction.MoveAction)
        event.accept()

    def dragLeaveEvent(self, event) -> None:
        self._set_drop_hover(False)
        super().dragLeaveEvent(event)

    def dropEvent(self, event: QDropEvent) -> None:
        self._set_drop_hover(False)
        camera_id = camera_id_from_mime(event.mimeData())
        if camera_id is None:
            event.ignore()
            return
        event.setDropAction(Qt.DropAction.MoveAction)
        event.accept()
        self.camera_dropped.emit(camera_id)

    def _set_drop_hover(self, on: bool) -> None:
        if on != self._drop_hover:
            self._drop_hover = on
            self.update()

    def _paint_frame_border(self, painter: QPainter, color: QColor, style=Qt.PenStyle.SolidLine) -> None:
        pen = QPen(color)
        pen.setWidth(_BORDER)
        pen.setStyle(style)
        painter.setPen(pen)
        painter.setBrush(Qt.BrushStyle.NoBrush)
        painter.drawRect(self.rect().adjusted(1, 1, -1, -1))


class VideoTile(_SlotWidget):
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
        self._drag_enabled = False
        self._press_pos: QPoint | None = None
        self.setObjectName(f"videoTile_{camera_id}")
        self.setAttribute(Qt.WidgetAttribute.WA_OpaquePaintEvent, True)
        self.setFocusPolicy(Qt.FocusPolicy.ClickFocus)

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

    def display_name(self) -> str:
        """The name as painted in the label: elided to the current tile width."""
        if not self._name:
            return ""
        metrics = QFontMetrics(self._font(self.overlay_font_sizes()[2]))
        return metrics.elidedText(self._name, Qt.TextElideMode.ElideRight, max(16, self.width() - 24))

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
        if self._state == "FAILED" and self._reason in REASON_HEADLINES:
            return REASON_HEADLINES[self._reason]
        if self._state == "FAILED":  # iMou texts ("click to start" / "click to retry"); None for every other reason
            headline = imou_status.headline_for(self._reason)
            if headline is not None:
                return headline
        return OVERLAY_TEXT[self._state]

    def waiting_for_click(self) -> bool:
        """True while the tile is a remote stream waiting for a free relay slot (click to start it)."""
        return self._state == "FAILED" and self._reason == REMOTE_CLICK_TO_VIEW

    def set_selected(self, selected: bool) -> None:
        if selected != self._selected:
            self._selected = selected
            self.update()

    def is_selected(self) -> bool:
        return self._selected

    def set_drag_enabled(self, enabled: bool) -> None:
        """Allow dragging this tile (payload: :meth:`drag_mime_data`)."""
        self._drag_enabled = bool(enabled)

    def drag_mime_data(self) -> QMimeData:
        """Payload of a drag started on this tile."""
        return camera_mime_data(self._camera_id)

    def sizeHint(self) -> QSize:
        return QSize(480, 270)

    # ------------------------------------------------------------------ events
    def mousePressEvent(self, event: QMouseEvent) -> None:
        if event.button() == Qt.MouseButton.LeftButton:
            self._press_pos = event.position().toPoint()
            self.clicked.emit(self._camera_id)
        super().mousePressEvent(event)

    def mouseReleaseEvent(self, event: QMouseEvent) -> None:
        self._press_pos = None
        super().mouseReleaseEvent(event)

    def mouseMoveEvent(self, event: QMouseEvent) -> None:
        start = self._press_pos
        if (self._drag_enabled and start is not None and event.buttons() & Qt.MouseButton.LeftButton
                and (event.position().toPoint() - start).manhattanLength() >= QApplication.startDragDistance()):
            self._press_pos = None
            self._start_drag()
            return
        super().mouseMoveEvent(event)

    def mouseDoubleClickEvent(self, event: QMouseEvent) -> None:
        if event.button() == Qt.MouseButton.LeftButton:
            self.double_clicked.emit(self._camera_id)
        super().mouseDoubleClickEvent(event)

    def _start_drag(self) -> None:
        drag = QDrag(self)
        drag.setMimeData(self.drag_mime_data())
        thumb = self.grab()
        if not thumb.isNull():
            drag.setPixmap(thumb.scaledToWidth(min(160, max(1, thumb.width())),
                                               Qt.TransformationMode.SmoothTransformation))
        drag.exec(Qt.DropAction.MoveAction)

    def paintEvent(self, event: QPaintEvent) -> None:
        painter = QPainter(self)
        try:
            headline_pt, detail_pt, _label_pt = self.overlay_font_sizes()
            painter.fillRect(self.rect(), _BACKGROUND)
            if self._image is not None and not self._image.isNull():
                painter.setRenderHint(QPainter.RenderHint.SmoothPixmapTransform, True)
                painter.drawImage(self._fit_rect(self._image.width(), self._image.height()), self._image)
            headline = self.status_text()
            if headline:
                self._paint_overlay(painter, headline, headline_pt, detail_pt)
            elif self._image is None:
                self._paint_center(painter, self.display_name() or self._camera_id, _MUTED, detail_pt + 2)
            self._paint_name(painter)
            if self._drop_hover:
                self._paint_frame_border(painter, _theme.ACCENT, Qt.PenStyle.DashLine)
            elif self._selected:
                self._paint_frame_border(painter, _theme.ACCENT)
        finally:
            painter.end()

    # ------------------------------------------------------------------ painting helpers
    def _fit_rect(self, img_w: int, img_h: int) -> QRectF:
        """Largest rect with the image's aspect ratio centred in the widget (letterbox)."""
        w, h = self.width(), self.height()
        scale = min(w / img_w, h / img_h)
        fw, fh = img_w * scale, img_h * scale
        return QRectF((w - fw) / 2, (h - fh) / 2, fw, fh)

    def _paint_overlay(self, painter: QPainter, headline: str, headline_pt: int, detail_pt: int) -> None:
        painter.fillRect(self.rect(), _OVERLAY)
        self._paint_center(painter, headline, _TEXT, headline_pt, bold=True)
        if self._reason and self._reason != headline:
            painter.setFont(self._font(detail_pt))
            painter.setPen(_MUTED)
            top = self.height() // 2 + QFontMetrics(self._font(headline_pt, True)).height() // 2 + 2
            rect = self.rect().adjusted(6, top, -6, -6)
            if rect.height() <= 0:
                return
            flags = (Qt.AlignmentFlag.AlignHCenter.value | Qt.AlignmentFlag.AlignTop.value
                     | Qt.TextFlag.TextWordWrap.value)
            painter.drawText(rect, int(flags), self._reason)

    def _paint_center(self, painter: QPainter, text: str, color: QColor, size: int, bold: bool = False) -> None:
        painter.setFont(self._font(size, bold))
        painter.setPen(color)
        flags = Qt.AlignmentFlag.AlignCenter.value | Qt.TextFlag.TextWordWrap.value
        painter.drawText(self.rect().adjusted(4, 0, -4, 0), int(flags), text)

    def _paint_name(self, painter: QPainter) -> None:
        text = self.display_name()
        if not text:
            return
        painter.setFont(self._font(self.overlay_font_sizes()[2]))
        metrics = painter.fontMetrics()
        pad = 4 if self.height() < 130 else 6
        box = QRect(pad, self.height() - metrics.height() - pad - 4, metrics.horizontalAdvance(text) + 12,
                    metrics.height() + 4)
        painter.fillRect(box, _LABEL_BG)
        painter.setPen(_TEXT)
        painter.drawText(box, Qt.AlignmentFlag.AlignCenter, text)


class EmptySlotTile(_SlotWidget):
    """Grid position without a camera (objectName ``emptySlot``); never streams."""

    def __init__(self, index: int, parent: QWidget | None = None):
        super().__init__(parent)
        self._index = index
        self.setObjectName("emptySlot")
        self.setAttribute(Qt.WidgetAttribute.WA_OpaquePaintEvent, True)
        self.setToolTip(EMPTY_HINT)

    @property
    def slot_index(self) -> int:
        """Position of this tile in the slot list (0-based, row-major)."""
        return self._index

    def status_text(self) -> str:
        return EMPTY_TEXT

    def hint_text(self) -> str:
        return EMPTY_HINT

    def sizeHint(self) -> QSize:
        return QSize(480, 270)

    def paintEvent(self, event: QPaintEvent) -> None:
        painter = QPainter(self)
        try:
            headline_pt, detail_pt, _ = self.overlay_font_sizes()
            painter.fillRect(self.rect(), _EMPTY_BACKGROUND)
            border = _theme.ACCENT if self._drop_hover else _EMPTY_BORDER
            self._paint_frame_border(painter, border, Qt.PenStyle.DashLine)
            head = self._font(headline_pt, bold=True)
            head_h = QFontMetrics(head).height()
            mid = self.height() // 2
            painter.setFont(head)
            painter.setPen(_MUTED)
            painter.drawText(QRect(4, mid - head_h, self.width() - 8, head_h), Qt.AlignmentFlag.AlignCenter,
                             EMPTY_TEXT)
            painter.setFont(self._font(detail_pt))
            flags = (Qt.AlignmentFlag.AlignHCenter.value | Qt.AlignmentFlag.AlignTop.value
                     | Qt.TextFlag.TextWordWrap.value)
            painter.drawText(QRect(6, mid + 2, self.width() - 12, max(0, self.height() - mid - 6)), int(flags),
                             EMPTY_HINT)
        finally:
            painter.end()
