# Phase 2 contract: mobile-app parity (sign in with TP-Link ID, cameras appear, live view over :8800)

Target user flow (same as the Tapo phone app on home Wi-Fi):
1. First start shows **Sign in** (TP-Link ID email + password, "Keep me signed in"). MFA code
   prompt if the account requires it. Region redirect handled silently.
2. After sign-in the camera list fills from the cloud device list (name = alias, model,
   online/offline). The app finds each camera on the LAN with TDP broadcast (UDP 20002), matches
   by md5(deviceId) (fallback MAC), and connects locally with user `admin` + the TP-Link ID password.
   No camera account / RTSP setup is required. Cameras not found on the LAN show "Not on this network".
3. Grid tiles start live video automatically over the local media port (:8800 `/stream` preview,
   like the phone app). RTSP is used only if the user configured a camera account and chose it.
4. Controls, playback, notifications as in Phase 1. Account menu: Refresh cameras, Sign out
   (removes the account secret and cloud cameras; manual cameras stay). Snapshot button saves a PNG.
5. Next start: signed in silently from the vault (cloud re-login; if offline, use the cached camera
   list and LAN discovery).

Spec: `specs/cloud_account_spec.json` (ids `cloud.*`, `tdp.*`, `lan.*`), merged into
`specs/tapo_protocol_spec.json`. Shared models in `tapo_desktop.models`: `AccountInfo`,
`ACCOUNT_VAULT_ID`, `CloudDevice`, `LanDevice`, new `CameraConfig` fields (`source`, `device_id`,
`mac`, `model`, `online`, `lan_found`, `live_transport`).

## Lane P2-cloud: `tapo_desktop.core.cloud` and `tapo_desktop.core.discovery`
`core/cloud/signing.py`
```python
ACCESS_KEY: str; SECRET_KEY: str              # production pair from spec cloud.signing.keys
def content_md5(body: bytes) -> str          # base64(md5(body or b"{}"))
def sign_headers(body: bytes, path: str, *, nonce: str | None = None,
                 access_key: str = ACCESS_KEY, secret_key: str = SECRET_KEY) -> dict[str, str]
    # {"Content-MD5": ..., "X-Authorization": "Timestamp=9999999999, Nonce=..., AccessKey=..., Signature=..."}
```
`core/cloud/client.py`
```python
class CloudError(TapoError): code: int | str
class CloudAuthError(CloudError)              # wrong email/password (-20601/-20615), locked (-20661)
class CloudMfaRequired(CloudError): process_id: str; mfa_types: list[int]
class TapoCloudClient:
    def __init__(self, *, terminal_uuid: str, base_url: str = "https://n-wap.i.tplinkcloud.com",
                 timeout: float = 15.0, app_version: str = "3.22.806", verify_tls: bool = True)
    def login(self, email: str, password: str) -> AccountInfo
        # getAccountStatusAndUrl -> appServerUrlV2 -> /api/v2/account/login; follows -20212 region redirect;
        # raises CloudMfaRequired / CloudAuthError / CloudError / TapoConnectionError
    def request_mfa_code(self, mfa_type: int) -> None          # 2 email, 4 SMS (1 push, 3 TOTP need no send)
    def complete_mfa(self, code: str, mfa_type: int) -> AccountInfo   # checkMFACodeAndLogin, terminalBindEnabled true
    def list_cameras(self) -> list[CloudDevice]  # getDeviceListByPage, all pages, camera-like types only;
                                                 # -20651 token expired -> silent re-login once with stored creds
    @property
    def token(self) -> str | None
    @property
    def account(self) -> AccountInfo | None
```
Never log password/token; `repr` must not include them.

`core/discovery/tdp.py`
```python
TDP_PORT = 20002
def build_probe(public_key_pem: str, serial: int) -> bytes      # 16-byte header + JSON, CRC32 per spec
def parse_response(data: bytes, private_key) -> LanDevice | None # tolerant: None on garbage / CRC error
def discover(timeout: float = 3.0, *, targets: list[tuple[str, int]] | None = None,
             attempts: int = 3, interval: float = 0.3) -> list[LanDevice]
    # targets None -> 255.255.255.255:20002 + each local IPv4 interface's directed broadcast; dedupe by mac
```
`core/discovery/matcher.py`
```python
def normalise_mac(mac: str) -> str                               # "AA-BB-CC-DD-EE-FF"
def match_devices(cloud: list[CloudDevice], lan: list[LanDevice]) -> dict[str, LanDevice]   # deviceId -> LanDevice
    # md5(deviceId).lower() == lan.device_id_md5, else MAC equality
```
Fixtures (lane-owned, new files only): `tests/fixtures/mock_cloud.py` (`MockTapoCloud(accounts={email: password}, devices=[...], mfa=False, region_redirect=False)` HTTP server on 127.0.0.1:0 that VERIFIES Content-MD5 + X-Authorization signature, `.base_url`, `.expire_tokens()`, `.request_log`) and `tests/fixtures/mock_tdp.py` (`MockTdpResponder(devices=[dict], port=0)` UDP responder implementing the spec reply incl. RSA-OAEP/AES encrypt_info; `.port`). pytest fixtures for them go in `tests/fixtures/phase2_fixtures.py` and are registered via `pytest_plugins` in a NEW `tests/phase2/conftest.py`; Phase 2 tests live under `tests/phase2/`.

## Lane P2-live: local :8800 live view
`core/media_engine/local_stream.py`
```python
class LocalStreamSupervisor:   # same public surface as StreamSupervisor (state, pid, stats, start, stop, switch)
    def __init__(self, host: str, password: str, width: int = 640, height: int = 360, *,
                 resolution: str = "VGA",            # "HD" | "VGA" (preview request resolutions)
                 port: int = 8800, channel: int = 0,
                 on_frame, on_state=None, on_audio=None, ffmpeg_path=None, hwaccel="auto",
                 limiter=None, limiter_key=None, job=None,
                 backoff_initial=0.5, backoff_max=10.0, stall_timeout=5.0, max_retries=None)
    def switch(self, resolution: str, width: int, height: int) -> None
```
Behaviour: opens `MediaSession` (core.vod.session), sends the preview request
[spec:stream.control.preview], acks per [spec:vod.stream.flow_control_ack] when required, feeds the
decrypted MPEG-TS parts into an ffmpeg decoder (`-f mpegts -i pipe:0` or a localhost relay) that
outputs rgb24 frames exactly like StreamSupervisor; reconnect with backoff on any failure/stall;
first frame after recovery < 3 s; no leaked ffmpeg (job object); stop() < 3 s; StreamLimitError /
-52407 -> FAILED with reason containing "limit"; wrong password -> FAILED reason containing "auth".
Fixture extension (allowed, additive only): the mock :8800 server must answer preview requests by
streaming the cached TS clip in a loop in real time (encrypted per spec) until the client closes or
sends stop; obey `drop_media_connections()` (new) for chaos.

## Lane P2-app: account flow in services + UI
`services/account.py`
```python
class AccountManager(QObject):
    signed_in = pyqtSignal(object)            # AccountInfo
    signed_out = pyqtSignal()
    mfa_required = pyqtSignal(object)         # list[int] mfa types
    sign_in_failed = pyqtSignal(str)          # user-facing message
    cameras_refreshed = pyqtSignal(int, int)  # total cloud cameras, found on LAN
    def __init__(self, controller: AppController, *, cloud_factory=None, discover=None, parent=None)
        # cloud_factory(terminal_uuid) -> TapoCloudClient-like; discover() -> list[LanDevice]
    def account(self) -> AccountInfo | None
    def sign_in(self, email: str, password: str, remember: bool = True) -> None   # async (worker)
    def submit_mfa(self, code: str, mfa_type: int) -> None
    def request_mfa_code(self, mfa_type: int) -> None
    def restore_session(self) -> bool       # on startup: vault has account -> silent sign-in (async); False if none
    def refresh_cameras(self) -> None        # cloud list + TDP discovery -> upsert cloud CameraConfigs
                                             # (id = deviceId, name = alias, host = LAN ip or "", lan_found,
                                             # online, model, mac); vault CLOUD creds = ("admin", account password)
    def sign_out(self) -> None               # removes account secret + cloud cameras (streams stopped)
    def wait_idle(self, timeout_ms: int = 5000) -> bool
```
Terminal UUID: generated once (32 upper hex) and persisted via ConfigStore setting "terminal_uuid".
`TapoCameraService.start_stream`: `live_transport` "auto" -> `LocalStreamSupervisor` (resolution HD
for fullscreen_quality HD, VGA for SD) unless a CAMERA_ACCOUNT credential exists and transport is
"rtsp"; cameras with `lan_found=False` or empty host -> on_state(FAILED, "Not on this network").
ONVIF events need a camera account; without one, poll `getLastAlarmInfo` every 5 s
[spec:events.last_alarm_info] for motion notifications.
UI: `ui/sign_in_dialog.py` `SignInDialog` (objectNames `emailEdit`, `passwordEdit`, `rememberCheck`,
`signInButton`, `errorLabel`, `mfaCodeEdit`, `mfaSendButton`, `mfaVerifyButton`; inline errors, no
modal boxes). MainWindow: account button `accountButton` with menu actions "Refresh cameras",
"Sign out"; camera list rows show `name` + status ("Offline" / "Not on this network");
`snapshotButton` in ControlsPanel saves the selected tile's current frame as PNG to
`%USERPROFILE%\Pictures\Tapo Desktop\<name>_<timestamp>.png`. `app.main` shows SignInDialog when no
account is stored (skippable with "Use without account" -> manual cameras only).
