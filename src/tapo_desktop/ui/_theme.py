"""Dark theme, palette and the generated application icon (private to the UI package)."""
from __future__ import annotations

from PyQt6.QtCore import QPointF, QRectF, Qt
from PyQt6.QtGui import QBrush, QColor, QIcon, QPainter, QPainterPath, QPalette, QPixmap
from PyQt6.QtWidgets import QApplication

ACCENT = QColor(0, 150, 214)
_WINDOW = QColor(30, 32, 36)
_BASE = QColor(22, 24, 27)
_ALT = QColor(38, 41, 46)
_TEXT = QColor(225, 228, 232)
_DISABLED = QColor(110, 115, 122)

STYLESHEET = """
QMainWindow, QDialog { background: #1e2024; }
QDockWidget { color: #c8ccd2; font-weight: 600; }
QDockWidget::title { background: #25282d; padding: 6px 8px; }
QToolBar { background: #25282d; border: none; spacing: 4px; padding: 4px; }
QStatusBar { background: #25282d; color: #a0a5ac; }
QListWidget { background: #16181b; border: 1px solid #2c3036; border-radius: 4px; padding: 2px; }
QListWidget::item { padding: 6px 8px; border-radius: 3px; }
QListWidget::item:selected { background: #0096d6; color: white; }
QListWidget::item:hover:!selected { background: #2a2e34; }
QPushButton, QToolButton {
    background: #2c3036; color: #e1e4e8; border: 1px solid #3a3f46; border-radius: 4px;
    padding: 5px 10px;
}
QPushButton:hover, QToolButton:hover { background: #353a41; }
QPushButton:pressed, QToolButton:pressed { background: #0b6f9c; }
QPushButton:checked, QToolButton:checked { background: #0096d6; border-color: #0096d6; color: white; }
QPushButton:disabled, QToolButton:disabled { color: #6e737a; background: #24272b; }
QPushButton#ptzStop { border-radius: 18px; }
QLineEdit, QComboBox, QDateEdit {
    background: #16181b; color: #e1e4e8; border: 1px solid #3a3f46; border-radius: 4px; padding: 4px 6px;
}
QLineEdit:focus, QComboBox:focus, QDateEdit:focus { border-color: #0096d6; }
QGroupBox { border: 1px solid #2c3036; border-radius: 6px; margin-top: 14px; padding: 8px 6px 6px 6px; }
QGroupBox::title { subcontrol-origin: margin; left: 8px; padding: 0 4px; color: #a0a5ac; }
QProgressBar { background: #16181b; border: 1px solid #3a3f46; border-radius: 4px; text-align: center;
               color: #e1e4e8; height: 16px; }
QProgressBar::chunk { background: #0096d6; border-radius: 3px; }
QTabWidget::pane { border: 1px solid #2c3036; border-radius: 4px; top: -1px; }
QTabBar::tab { background: #25282d; color: #a0a5ac; padding: 6px 14px; border-top-left-radius: 4px;
               border-top-right-radius: 4px; }
QTabBar::tab:selected { background: #2c3036; color: #e1e4e8; }
QLabel#errorLabel { color: #ff6b6b; }
QLabel#hintLabel, QLabel#downloadStatus { color: #a0a5ac; }
QWidget#cameraGrid { background: #0a0b0d; }
QWidget#emptySlot { background: #121417; border: 1px dashed #2c3036; }
"""


def apply_theme(app: QApplication) -> None:
    """Fusion style + dark palette + stylesheet for the whole application."""
    app.setStyle("Fusion")
    pal = QPalette()
    for role, color in (
        (QPalette.ColorRole.Window, _WINDOW), (QPalette.ColorRole.WindowText, _TEXT),
        (QPalette.ColorRole.Base, _BASE), (QPalette.ColorRole.AlternateBase, _ALT),
        (QPalette.ColorRole.Text, _TEXT), (QPalette.ColorRole.Button, _ALT),
        (QPalette.ColorRole.ButtonText, _TEXT), (QPalette.ColorRole.Highlight, ACCENT),
        (QPalette.ColorRole.HighlightedText, QColor("white")), (QPalette.ColorRole.ToolTipBase, _ALT),
        (QPalette.ColorRole.ToolTipText, _TEXT), (QPalette.ColorRole.PlaceholderText, _DISABLED),
    ):
        pal.setColor(role, color)
    for role in (QPalette.ColorRole.Text, QPalette.ColorRole.ButtonText, QPalette.ColorRole.WindowText):
        pal.setColor(QPalette.ColorGroup.Disabled, role, _DISABLED)
    app.setPalette(pal)
    app.setStyleSheet(STYLESHEET)


def app_icon() -> QIcon:
    """A generated camera glyph (no binary asset needed), in several sizes."""
    icon = QIcon()
    for size in (16, 24, 32, 48, 64, 128, 256):
        icon.addPixmap(_icon_pixmap(size))
    return icon


def _icon_pixmap(size: int) -> QPixmap:
    pm = QPixmap(size, size)
    pm.fill(Qt.GlobalColor.transparent)
    p = QPainter(pm)
    try:
        p.setRenderHint(QPainter.RenderHint.Antialiasing, True)
        s = float(size)
        p.setPen(Qt.PenStyle.NoPen)
        p.setBrush(QBrush(ACCENT))
        p.drawRoundedRect(QRectF(0, 0, s, s), s * 0.22, s * 0.22)
        # camera dome body
        p.setBrush(QBrush(QColor(245, 247, 250)))
        body = QPainterPath()
        body.addRoundedRect(QRectF(s * 0.18, s * 0.22, s * 0.64, s * 0.56), s * 0.28, s * 0.28)
        p.drawPath(body)
        # lens
        p.setBrush(QBrush(QColor(25, 28, 33)))
        p.drawEllipse(QPointF(s * 0.5, s * 0.5), s * 0.17, s * 0.17)
        p.setBrush(QBrush(ACCENT.lighter(140)))
        p.drawEllipse(QPointF(s * 0.46, s * 0.46), s * 0.05, s * 0.05)
    finally:
        p.end()
    return pm
