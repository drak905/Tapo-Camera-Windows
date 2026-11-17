# Contract: control plane (auth, control_client) and ONVIF

## `tapo_desktop.core.auth.crypto`  [spec:auth.*]
All hex digests are UPPERCASE unless noted. Strings are UTF-8.
```python
def md5_upper(s: str) -> str
def sha256_upper(s: str) -> str
def generate_cnonce() -> str                      # 16 uppercase hex chars, from secrets
def expected_device_confirm(password: str, cnonce: str, nonce: str) -> str
def verify_device_confirm(password: str, cnonce: str, nonce: str, device_confirm: str) -> bool  # constant-time compare
def digest_passwd(password: str, cnonce: str, nonce: str) -> str
@dataclass(frozen=True)
class SessionKeys: lsk: bytes; ivb: bytes          # 16 bytes each
def derive_session_keys(password: str, cnonce: str, nonce: str) -> SessionKeys
def compute_tag(password: str, cnonce: str, body: str, seq: int) -> str
class SecureSessionCipher:
    def __init__(self, keys: SessionKeys)
    def encrypt(self, plaintext: str) -> str        # AES-128-CBC/PKCS7, base64 (no newlines)
    def decrypt(self, b64: str) -> str              # raises TapoProtocolError on bad padding/base64
def wrap_secure_passthrough(cipher: SecureSessionCipher, inner: dict) -> str
    # returns exact outer body JSON string {"method":"securePassthrough","params":{"request":"<b64>"}}
```
Golden vector: `[spec:auth.golden_vector.v3]` must reproduce byte-for-byte.

## `tapo_desktop.core.auth.vault`
```python
def dpapi_protect(data: bytes, entropy: bytes | None = None) -> bytes     # CryptProtectData (ctypes)
def dpapi_unprotect(blob: bytes, entropy: bytes | None = None) -> bytes   # raises TapoError on corrupt blob
class CredentialVault:
    def __init__(self, path: pathlib.Path)          # JSON file; secrets stored only as base64 DPAPI blobs
    def set(self, camera_id: str, kind: CredentialKind, creds: Credentials) -> None
    def get(self, camera_id: str, kind: CredentialKind) -> Credentials | None
    def delete(self, camera_id: str) -> None        # removes all kinds for that camera
    def camera_ids(self) -> list[str]
```
Writes are atomic (temp file + `os.replace`). Plaintext passwords never appear in the file.
A corrupt/garbled entry returns `None` from `get` (and does not crash other entries).

## `tapo_desktop.core.control_client.transport`
```python
class PinStore:                                     # TOFU certificate pins
    def __init__(self, path: pathlib.Path | None = None)   # None = in-memory
    def get(self, host: str, port: int) -> str | None       # sha256 hex (lowercase) of DER cert
    def set(self, host: str, port: int, fingerprint: str) -> None
    def forget(self, host: str, port: int) -> None
class HttpsTransport:
    def __init__(self, host: str, port: int = 443, *, pin_store: PinStore | None = None, timeout: float = 10.0)
    def post(self, path: str, body: str, headers: dict[str, str]) -> tuple[int, dict[str, str], str]
        # keep-alive connection reuse; reconnect once on stale socket; raises TapoConnectionError,
        # CertificateMismatchError (pin differs; first contact pins automatically)
    @property
    def fingerprint(self) -> str | None
    def close(self) -> None
```

## `tapo_desktop.core.control_client.dtos`  [spec:envelope.*, control.*, vod.search.*]
Pure functions, no I/O. Each `*_req` returns one request dict `{"method": ..., "params": {...}}`
exactly matching the spec's `params` template; each `parse_*` takes that request's `result` dict.
```python
def multiple_request(requests: list[dict]) -> dict
def get_device_info_req() -> dict;            def parse_device_info(result: dict) -> DeviceInfo
def get_component_list_req() -> dict;         def parse_component_list(result: dict) -> dict[str, int]
def motor_move_req(x: int, y: int) -> dict    # x_coord/y_coord as strings
def motor_step_req(direction: int) -> dict    # singalMove, direction in {0, 90, 180, 270}
def motor_stop_req() -> dict
def get_presets_req() -> dict;                def parse_presets(result: dict) -> list[Preset]
def goto_preset_req(preset_id: str) -> dict
def add_preset_req(name: str) -> dict
def delete_preset_req(preset_id: str) -> dict
def get_privacy_req() -> dict;                def parse_privacy(result: dict) -> bool
def set_privacy_req(enabled: bool) -> dict
def get_day_night_req() -> dict;              def parse_day_night(result: dict) -> DayNightMode
def set_day_night_req(mode: DayNightMode) -> dict
def get_led_req() -> dict;                    def parse_led(result: dict) -> bool
def set_led_req(enabled: bool) -> dict
def manual_alarm_req(start: bool) -> dict
def get_motion_detection_req() -> dict;       def parse_motion_detection(result: dict) -> tuple[bool, int | None]
def set_motion_detection_req(enabled: bool, sensitivity: int | None = None) -> dict   # sensitivity in {20, 60, 80}
def get_sd_status_req() -> dict;              def parse_sd_status(result: dict) -> list[dict]
def search_days_req(start: datetime.date, end: datetime.date) -> dict;  def parse_days(result: dict) -> list[datetime.date]
def search_day_clips_req(day_start_utc: int, day_end_utc: int, start_index: int = 0, end_index: int = 99) -> dict
def parse_day_clips(result: dict) -> list[RecordingClip]
def reboot_req() -> dict
```
Parsers must tolerate missing/extra keys and both flat and module/section nesting where the
spec says so; on unusable data raise `TapoProtocolError` (never `KeyError`/`TypeError`).

## `tapo_desktop.core.control_client.client`
```python
class TapoControlClient:
    def __init__(self, host: str, password: str, *, username: str = "admin", port: int = 443,
                 transport: HttpsTransport | None = None, pin_store: PinStore | None = None,
                 timeout: float = 10.0)
    def login(self) -> None
        # secure (encrypt_type "3") if step-1 returns -40413 with nonce, else legacy MD5 login.
        # bad device_confirm / bad password -> TapoAuthError; -40404 / lock codes -> TapoLockedError
    @property
    def is_secure(self) -> bool
    @property
    def logged_in(self) -> bool
    def request(self, req: dict) -> dict             # single request via multipleRequest; returns its result
    def multiple_request(self, reqs: list[dict]) -> list[dict]   # [{"method","result","error_code"}]
        # Auto-login on first use. On -40401 (top-level or per-item) or HTTP 401: re-login once, retry once.
        # Thread-safe (internal lock); seq increments per secure request.
        # Non-zero item error_code -> TapoDeviceError(code, method) from request(); multiple_request returns raw items.
    # convenience wrappers (all use dtos):
    def get_device_info(self) -> DeviceInfo
    def get_components(self) -> dict[str, int]
    def move(self, x: int, y: int) -> None
    def step(self, direction: int) -> None
    def stop_move(self) -> None
    def get_presets(self) -> list[Preset]
    def goto_preset(self, preset_id: str) -> None
    def add_preset(self, name: str) -> None
    def delete_preset(self, preset_id: str) -> None
    def get_privacy(self) -> bool;      def set_privacy(self, enabled: bool) -> None
    def get_day_night(self) -> DayNightMode; def set_day_night(self, mode: DayNightMode) -> None
    def get_led(self) -> bool;          def set_led(self, enabled: bool) -> None
    def set_alarm(self, active: bool) -> None
    def get_motion_detection(self) -> tuple[bool, int | None]
    def set_motion_detection(self, enabled: bool, sensitivity: int | None = None) -> None
    def get_sd_status(self) -> list[dict]
    def days_with_recordings(self, start: date, end: date) -> list[date]
    def recordings_for_day(self, day: date, tz: datetime.tzinfo | None = None) -> list[RecordingClip]  # pages until done
    def reboot(self) -> None
    def close(self) -> None
```
Credentials must never appear in logs, exception messages or `repr`.

## `tapo_desktop.core.onvif_service`  (ONVIF on port 2020; external spec: ONVIF Core/PTZ/Events)
`soap.py`
```python
def wsse_header(username: str, password: str, *, created: str | None = None, nonce: bytes | None = None) -> str
    # UsernameToken PasswordDigest = base64(sha1(nonce + created + password))
def build_envelope(body_xml: str, header_xml: str = "") -> str
class OnvifClient:
    def __init__(self, host: str, username: str, password: str, *, port: int = 2020, timeout: float = 5.0)
    def call(self, path: str, body_xml: str) -> xml.etree.ElementTree.Element   # SOAP Body child; SOAP Fault -> TapoDeviceError(code=-1)
    def get_device_information(self) -> dict          # Manufacturer, Model, FirmwareVersion, SerialNumber, HardwareId
    def get_capabilities(self) -> dict[str, str]       # {"ptz": url, "events": url, "media": url, ...}
    def get_profiles(self) -> list[dict]               # [{"token", "name"}]
```
`discovery.py`
```python
@dataclass(frozen=True)
class DiscoveredDevice: host: str; xaddrs: tuple[str, ...]; scopes: tuple[str, ...]; name: str = ""
def build_probe(message_id: str | None = None) -> bytes
def parse_probe_match(data: bytes) -> list[DiscoveredDevice]     # tolerant of garbage: returns []
def discover(timeout: float = 3.0, *, multicast_addr: str = "239.255.255.250", port: int = 3702) -> list[DiscoveredDevice]
def scan_subnet(cidr: str, port: int = 2020, timeout: float = 0.3, max_workers: int = 64) -> list[str]
```
`ptz.py`
```python
class OnvifPtz:
    def __init__(self, client: OnvifClient, profile_token: str | None = None)  # None -> first profile
    def continuous_move(self, pan: float, tilt: float, zoom: float = 0.0, timeout_s: float | None = None) -> None  # values clamped to [-1, 1]
    def stop(self) -> None
    def get_presets(self) -> list[Preset]
    def goto_preset(self, token: str) -> None
```
`events.py`
```python
def parse_notifications(xml_bytes: bytes, camera_host: str) -> list[MotionEvent]   # tolerant: [] on garbage
class PullPointSubscriber:
    def __init__(self, client: OnvifClient, on_event: Callable[[MotionEvent], None], *,
                 pull_timeout_s: int = 5, message_limit: int = 10, retry_backoff_s: float = 1.0)
    def start(self) -> None      # background daemon thread: CreatePullPointSubscription, PullMessages loop,
                                 # Renew before expiry, re-subscribe with backoff on any failure
    def stop(self, timeout: float = 3.0) -> None   # unsubscribes best-effort; thread joined
    @property
    def running(self) -> bool
```
