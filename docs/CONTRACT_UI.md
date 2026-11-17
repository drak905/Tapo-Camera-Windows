# Contract: services layer and PyQt6 UI

The UI never calls core modules directly. It talks to `AppController`, which talks to one
`CameraService` per camera. UI tests inject a fake service via `service_factory`.

## `tapo_desktop.services.config_store`
```python
class ConfigStore:                     # %APPDATA%\TapoDesktop\config.json by default
    def __init__(self, path: pathlib.Path | None = None)
    def load(self) -> list[CameraConfig]
    def save(self, cameras: list[CameraConfig]) -> None      # atomic write; no secrets
    def get_setting(self, key: str, default=None); def set_setting(self, key: str, value) -> None
        # settings used: "grid_size" (1|2|3), "minimize_to_tray" (bool), "notifications" (bool)
def default_data_dir() -> pathlib.Path    # %APPDATA%\TapoDesktop (env TAPO_DESKTOP_HOME overrides)
```

## `tapo_desktop.services.camera_service`
```python
class CameraService(abc.ABC):          # blocking API; controller runs calls off the GUI thread
    config: CameraConfig
    def connect(self) -> DeviceInfo
    def start_stream(self, quality: StreamQuality, width: int, height: int,
                     on_frame: Callable[[Frame], None], on_state: Callable[[StreamState, str | None], None]) -> None
    def stop_stream(self) -> None
    def ptz_move(self, dx: int, dy: int) -> None; def ptz_stop(self) -> None
    def get_presets(self) -> list[Preset]; def goto_preset(self, preset_id: str) -> None
    def get_status(self) -> dict        # {"privacy": bool, "day_night": DayNightMode, "led": bool,
                                        #  "motion_detection": bool, "alarm": bool}
    def set_privacy(self, on: bool) -> None; def set_day_night(self, mode: DayNightMode) -> None
    def set_led(self, on: bool) -> None; def set_alarm(self, on: bool) -> None
    def set_motion_detection(self, on: bool) -> None
    def days_with_recordings(self, start: date, end: date) -> list[date]
    def recordings_for_day(self, day: date) -> list[RecordingClip]
    def download_recording(self, clip: RecordingClip, out_path: pathlib.Path,
                           progress: Callable[[float], None] | None = None) -> pathlib.Path
    def start_events(self, on_event: Callable[[MotionEvent], None]) -> None; def stop_events(self) -> None
    def close(self) -> None
class TapoCameraService(CameraService):   # real wiring of core modules
    def __init__(self, config: CameraConfig, vault: CredentialVault, *, limiter: StreamLimiter | None = None,
                 pin_store: PinStore | None = None)
```

## `tapo_desktop.services.controller`
```python
class AppController(QObject):
    # signals
    cameras_changed = pyqtSignal()
    frame_ready = pyqtSignal(str, QImage)             # camera_id, frame (emitted on GUI thread)
    stream_state = pyqtSignal(str, str, str)          # camera_id, StreamState.name, reason ("" if none)
    status_changed = pyqtSignal(str, dict)            # camera_id, status dict (see get_status)
    command_failed = pyqtSignal(str, str, str)        # camera_id, action, message  -> UI rolls back
    motion_event = pyqtSignal(str, object)            # camera_id, MotionEvent
    recordings_ready = pyqtSignal(str, object, list)  # camera_id, date, list[RecordingClip]
    download_progress = pyqtSignal(str, float)        # camera_id, 0..1
    download_finished = pyqtSignal(str, str, str)     # camera_id, path, error ("" = ok)

    def __init__(self, config_store: ConfigStore, vault: CredentialVault,
                 service_factory: Callable[[CameraConfig], CameraService] | None = None, parent=None)
    def cameras(self) -> list[CameraConfig]
    def add_camera(self, config: CameraConfig, cloud: Credentials, camera_account: Credentials) -> None
    def update_camera(self, config: CameraConfig, cloud: Credentials | None = None, camera_account: Credentials | None = None) -> None
    def remove_camera(self, camera_id: str) -> None
    def start_view(self, camera_id: str, quality: StreamQuality, width: int, height: int) -> None
    def stop_view(self, camera_id: str) -> None
    def stop_all(self) -> None                         # stops streams + events; used on quit
    def refresh_status(self, camera_id: str) -> None
    def set_privacy(self, camera_id: str, on: bool) -> None   # optimistic: emits status_changed now,
    def set_day_night(self, camera_id: str, mode: DayNightMode) -> None  # on failure emits command_failed
    def set_led(self, camera_id: str, on: bool) -> None                  # and status_changed with old value
    def set_alarm(self, camera_id: str, on: bool) -> None
    def set_motion_detection(self, camera_id: str, on: bool) -> None
    def ptz_move(self, camera_id: str, dx: int, dy: int) -> None
    def ptz_stop(self, camera_id: str) -> None
    def goto_preset(self, camera_id: str, preset_id: str) -> None
    def load_recordings(self, camera_id: str, day: date) -> None
    def download_recording(self, camera_id: str, clip: RecordingClip, out_path: pathlib.Path) -> None
    def wait_idle(self, timeout_ms: int = 5000) -> bool   # test helper: waits for background jobs
```

## `tapo_desktop.ui`  (PyQt6 widgets; every interactive widget has a stable `objectName`)
- `ui.main_window.MainWindow(controller: AppController, tray_enabled: bool = True)`:
  objectNames: `cameraList` (QListWidget), `addCameraButton`, `removeCameraButton`, `layout1x1Button`,
  `layout2x2Button`, `layout3x3Button`, `cameraGrid`, `controlsPanel`, `playbackPanel`.
  Keyboard: W/A/S/D (and arrows) pan/tilt the selected camera (press = move, release = stop).
  `grid_size() -> int`, `set_grid_size(n: int)` (1, 2, 3; persisted via ConfigStore setting "grid_size").
  Closing the window minimizes to tray when "minimize_to_tray" is True; `quit()` stops everything.
- `ui.grid.CameraGrid` (objectName `cameraGrid`): `tiles() -> list[VideoTile]`, `set_grid_size(n)`,
  `selected_camera_id() -> str | None`; double-click a tile = single-camera fullscreen view
  (requests `fullscreen_quality`), double-click again = back to grid (`grid_quality`).
- `ui.video_tile.VideoTile`: objectName `videoTile_<camera_id>`; `set_frame(QImage)`, `status_text() -> str`,
  `camera_id`, signals `clicked(str)`, `double_clicked(str)`; shows "Offline"/"Reconnecting..." overlays.
- `ui.controls_panel.ControlsPanel` (objectName `controlsPanel`): `privacyToggle`, `ledToggle`,
  `alarmToggle`, `motionToggle` (checkable QPushButton/QCheckBox), `dayNightCombo` (QComboBox items
  auto/on/off), `ptzPad` (`ui.ptz_pad.PtzPad` with buttons `ptzUp`, `ptzDown`, `ptzLeft`, `ptzRight`,
  `ptzStop`), `presetCombo`, `gotoPresetButton`. Reflects `status_changed`; rolls back on `command_failed`.
- `ui.playback_panel.PlaybackPanel` (objectName `playbackPanel`): `recordingDate` (QDateEdit),
  `recordingsList` (QListWidget, one row per clip "HH:MM:SS - HH:MM:SS"), `downloadButton`, `downloadProgress`.
- `ui.camera_dialog.CameraDialog`: fields `nameEdit`, `hostEdit`, `cloudPasswordEdit`, `cameraUserEdit`,
  `cameraPasswordEdit`, `dualLensCheck`; `result_values() -> tuple[CameraConfig, Credentials, Credentials]`.
- `ui.tray.TrayController(window, controller)`: QSystemTrayIcon; `notify(title, message)`;
  motion events -> toast (if "notifications" setting True), max one toast per camera per 10 s.
- `tapo_desktop.app.main(argv: list[str] | None = None) -> int` and `python -m tapo_desktop`.
