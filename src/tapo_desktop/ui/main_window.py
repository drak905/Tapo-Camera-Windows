"""Main window (contract: docs/CONTRACT_UI.md).

objectNames: ``cameraList`` (QListWidget, one row per camera in ``controller.cameras()`` order,
text = camera name, ``Qt.UserRole`` data = camera id), ``addCameraButton``, ``removeCameraButton``,
``layout1x1Button``, ``layout2x2Button``, ``layout3x3Button``, ``cameraGrid``, ``controlsPanel``,
``playbackPanel``.

* Selection: selecting a row in ``cameraList`` selects that camera in the grid and vice versa; the
  selected camera is given to the controls/playback panels (``set_camera``).
* Grid size is read from setting ``"grid_size"`` at start and persisted by ``set_grid_size``
  (``ValueError`` for anything but 1, 2, 3). The layout buttons call ``set_grid_size``.
* ``addCameraButton`` runs ``CameraDialog(self).exec()``; on Accepted it calls
  ``controller.add_camera(*dialog.result_values())``. ``removeCameraButton`` asks
  ``QMessageBox.question(...)`` and on Yes calls ``controller.remove_camera(selected_id)``.
  ``cameras_changed`` refreshes ``cameraList`` and the grid.
* Keyboard (handled in the window's ``keyPressEvent``/``keyReleaseEvent``, auto-repeat ignored):
  W/Up -> ``ptz_move(id, 0, +d)``, S/Down -> ``(0, -d)``, A/Left -> ``(-d, 0)``, D/Right -> ``(+d, 0)``
  for the selected camera (d > 0); releasing the key -> ``ptz_stop(id)``. No selection -> no call.
* ``closeEvent``: if setting ``"minimize_to_tray"`` is True the event is ignored and the window is
  hidden (streams keep running); otherwise ``quit()``. ``quit()`` calls ``controller.stop_all()``,
  hides the tray icon and calls ``QApplication.quit()``.
"""
from __future__ import annotations

from PyQt6.QtWidgets import QMainWindow


class MainWindow(QMainWindow):
    def __init__(self, controller, tray_enabled: bool = True):
        super().__init__()
        raise NotImplementedError

    @property
    def tray(self):
        """The TrayController, or None when ``tray_enabled`` is False."""
        raise NotImplementedError

    def grid_size(self) -> int:
        raise NotImplementedError

    def set_grid_size(self, n: int) -> None:
        raise NotImplementedError

    def quit(self) -> None:
        raise NotImplementedError
