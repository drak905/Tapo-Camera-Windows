"""MockTapoCamera: one stateful fake camera exposing HTTPS, RTSP, ONVIF and :8800 media surfaces."""
from __future__ import annotations

import threading
import time
from typing import Any
from urllib.parse import quote

from . import media_assets
from .https_server import HttpsServer
from .media_server import MediaServer
from .onvif_server import OnvifServer
from .rtsp_server import RtspServer
from .shadow import DeviceShadow, default_components


class MockTapoCamera:
    """See docs/CONTRACT_FIXTURE.md. All servers bind ``host`` with ephemeral ports by default.

    Extra (non-contract) knobs, all optional:
      ``fixed_nonce`` / ``fixed_start_seq``  make the secure login deterministic (golden vector)
      ``media_digest_algorithm``             "MD5" (default) or "SHA-256" for the :8800 challenge
      ``media_ack_window`` / ``media_ack_timeout`` / ``media_chunk_size`` / ``max_media_sessions``
      ``media_preview_speed``                pacing multiplier of the :8800 live preview (1.0 = real time)
      ``max_failed_logins`` / ``lockout_seconds`` automatic lockout after N bad logins (0 = off)
      ``raw_request_log`` (whole decrypted inner bodies), ``media_request_log``, ``media_acks``,
      ``login_attempts``, ``media_auth_failures``, ``media_parts_sent``, ``media_preview_requests``,
      ``onvif_op_log``; ``drop_media_connections()`` resets every :8800 connection

    TPAP (additive; default cameras are unchanged): ``protocol="tpap"`` makes the HTTPS API speak the
    SPAKE2+ login + AES-128-CCM ``/ds`` channel of newer firmware (v3 step 1 -> -40211), see
    ``tpap_server.py``. ``tpap_credential`` "md5" | "sha256_extra" picks the passcode the camera
    expects. Knobs: ``tpap_iterations``, ``tpap_username`` ("md5" = md5_hex("admin") like the app,
    "admin" = literal), ``tpap_bad_dev_confirm``, ``tpap_refuse_shares`` (refuse the next N correct
    pake_share with -40401), ``tpap_session_expired``. Counters: ``v3_probe_count``,
    ``tpap_register_count``, ``tpap_share_attempts``, ``tpap_ds_seqs``.

    LAT mode (additive, [spec:lat.*]): ``tpap_credential="lat"`` with ``local_token="..."`` emulates a
    camera whose admin credential was moved to the cloud-issued local access token: TPAP accepts only
    the passcode SHA256_HEX_UPPER(LAT); the v3 secure login and the :8800 digest use the raw LAT as the
    password (:8800 encrypt_type 3 -> SHA256_HEX_UPPER(LAT)). ``local_token`` may be changed while
    running (token rotation). The legacy MD5 login keeps using ``cloud_password``.
    """

    def __init__(self, cloud_password: str = "cloudpass", camera_user: str = "camuser",
                 camera_password: str = "campass", secure: bool = True, model: str = "C210",
                 dual_lens: bool = False, max_rtsp_clients: int = 2, https_port: int = 0, rtsp_port: int = 0,
                 onvif_port: int = 0, media_port: int = 0, host: str = "127.0.0.1",
                 protocol: str | None = None, tpap_credential: str = "md5", local_token: str = ""):
        self.cloud_password = cloud_password
        self.camera_user = camera_user
        self.camera_password = camera_password
        self.secure = secure
        if protocol not in (None, "secure", "legacy", "tpap"):
            raise ValueError(f"unknown protocol {protocol!r}")
        if tpap_credential not in ("md5", "sha256_extra", "lat"):
            raise ValueError(f"unknown tpap_credential {tpap_credential!r}")
        if tpap_credential == "lat" and not local_token:
            raise ValueError('tpap_credential="lat" needs a local_token')
        self.protocol = protocol or ("secure" if secure else "legacy")
        self.tpap_credential = tpap_credential
        self.local_token = local_token  # LAT mode: the camera's admin secret is this string
        self.model = model
        self.dual_lens = dual_lens
        self.max_rtsp_clients = max_rtsp_clients
        self.host = host
        self._ports = {"https": https_port, "rtsp": rtsp_port, "onvif": onvif_port, "media": media_port}
        self.state = DeviceShadow(model=model, components=default_components(dual_lens))
        # knobs
        self.fixed_nonce: str | None = None
        self.fixed_start_seq: int | None = None
        self.media_digest_algorithm = "MD5"
        self.media_kex_mode = "md5"           # "md5" (fixed IV) | "hkdf" (C200 fw 1.5.x)
        self.media_corrupt_hmac_every = 0     # HKDF: every Nth part gets a wrong X-Data-Hmac
        self.media_ack_window = 50
        self.media_ack_timeout = 5.0
        self.media_chunk_size = 188 * 32
        self.max_media_sessions = 2
        self.media_preview_speed = 1.0  # live preview pacing multiplier (1.0 = real time)
        self.max_failed_logins = 0
        self.lockout_seconds = 1800.0
        # TPAP knobs / counters (protocol="tpap" only)
        self.tpap_iterations = 1000
        self.tpap_username = "md5"
        self.tpap_bad_dev_confirm = False
        self.tpap_refuse_shares = 0
        self.tpap_session_expired = 3600
        self.v3_probe_count = 0
        self.tpap_register_count = 0
        self.tpap_share_attempts = 0
        self.tpap_ds_seqs: list[int] = []
        # logs / counters
        self._lock = threading.RLock()
        self.request_log: list[dict] = []
        self.raw_request_log: list[Any] = []
        self.media_request_log: list[dict] = []
        self._media_acks: list[int] = []
        self.login_count = 0
        self.login_attempts = 0
        self.media_auth_failures = 0
        self.media_parts_sent = 0
        self.media_preview_requests = 0
        # injections
        self._token_expiry_every = 0
        self._errors: dict[str, list[int]] = {}
        self._http_status: list[int] = []
        self._rtsp_busy = 0
        self._rtsp_corrupt_audio = False
        self._locked_until = 0.0
        self._failures = 0
        self._https: HttpsServer | None = None
        self._rtsp: RtspServer | None = None
        self._onvif: OnvifServer | None = None
        self._media: MediaServer | None = None
        self._started = False

    # -- lifecycle ------------------------------------------------------------------------
    def start(self) -> "MockTapoCamera":
        if self._started:
            return self
        if media_assets.find_ffmpeg():
            media_assets.ensure_all(self.dual_lens)
        try:
            self._https = HttpsServer(self, self.host, self._ports["https"])
            self._rtsp = RtspServer(self, self.host, self._ports["rtsp"])
            self._onvif = OnvifServer(self, self.host, self._ports["onvif"])
            self._media = MediaServer(self, self.host, self._ports["media"])
            for s in (self._https, self._rtsp, self._onvif, self._media):
                s.start()
        except Exception:
            self.stop()
            raise
        self._started = True
        return self

    def stop(self) -> None:
        for s in (self._media, self._onvif, self._rtsp, self._https):
            if s is not None:
                try:
                    s.stop()
                except Exception:  # noqa: BLE001
                    pass
        self._started = False

    def __enter__(self) -> "MockTapoCamera":
        return self.start()

    def __exit__(self, *exc) -> None:
        self.stop()

    # -- addresses ------------------------------------------------------------------------
    @property
    def https_port(self) -> int:
        return self._https.port if self._https else self._ports["https"]

    @property
    def rtsp_port(self) -> int:
        return self._rtsp.port if self._rtsp else self._ports["rtsp"]

    @property
    def onvif_port(self) -> int:
        return self._onvif.port if self._onvif else self._ports["onvif"]

    @property
    def media_port(self) -> int:
        return self._media.port if self._media else self._ports["media"]

    @property
    def cert_fingerprint(self) -> str:
        return self._https.fingerprint if self._https else ""

    def rtsp_url(self, quality: Any = "stream2") -> str:
        q = getattr(quality, "value", quality)
        return (f"rtsp://{quote(self.camera_user, safe='')}:{quote(self.camera_password, safe='')}"
                f"@{self.host}:{self.rtsp_port}/{q}")

    def rotate_certificate(self) -> str:
        assert self._https is not None, "camera not started"
        return self._https.rotate_certificate()

    # -- HTTPS injections -----------------------------------------------------------------
    def expire_sessions(self) -> None:
        if self._https:
            self._https.expire_sessions()

    def inject_token_expiry(self, every_n: int) -> None:
        self._token_expiry_every = max(0, int(every_n))
        if self._https:
            with self._https.lock:
                self._https.ds_request_count = 0

    def inject_error(self, method: str, code: int, times: int = 1) -> None:
        with self._lock:
            self._errors.setdefault(method, []).extend([code] * times)

    def inject_http_status(self, status: int, times: int = 1) -> None:
        with self._lock:
            self._http_status.extend([status] * times)

    def lock_login(self, seconds: float) -> None:
        with self._lock:
            self._locked_until = time.monotonic() + seconds

    # -- RTSP -----------------------------------------------------------------------------
    def inject_rtsp_busy(self, times: int = 1) -> None:
        with self._lock:
            self._rtsp_busy += times

    @property
    def rtsp_active_clients(self) -> int:
        return self._rtsp.playing if self._rtsp else 0

    @property
    def rtsp_reject_count(self) -> int:
        return self._rtsp.reject_count if self._rtsp else 0

    def drop_rtsp_connections(self) -> None:
        if self._rtsp:
            self._rtsp.drop_all()

    def set_rtsp_corrupt_audio(self, enabled: bool) -> None:
        self._rtsp_corrupt_audio = bool(enabled)

    # -- ONVIF ----------------------------------------------------------------------------
    def emit_motion_event(self, active: bool = True) -> None:
        with self.state.lock:
            self.state.last_alarm_time = int(time.time())
            self.state.last_alarm_type = "motion"
        if self._onvif:
            self._onvif.emit(active)

    def ws_discovery_responder(self, probe: bytes | None = None) -> bytes:
        """ProbeMatches for ``probe`` (RelatesTo = its MessageID); b"" if ``probe`` is not a Probe."""
        assert self._onvif is not None, "camera not started"
        return self._onvif.probe_match(probe)

    @property
    def onvif_op_log(self) -> list[str]:
        return self._onvif.op_log if self._onvif else []

    # -- media ----------------------------------------------------------------------------
    @property
    def media_acks(self) -> list[int]:
        return list(self._media_acks)

    @property
    def media_active_sessions(self) -> int:
        return self._media.active if self._media else 0

    def drop_media_connections(self) -> None:
        """Reset every open :8800 connection (TCP RST), like a camera-side network drop."""
        if self._media:
            self._media.drop_all()

    @property
    def device_secret(self) -> str:
        """Password the camera's v3 login and :8800 digest expect: the LAT in LAT mode, else the cloud password."""
        return self.local_token if self.tpap_credential == "lat" else self.cloud_password

    # -- internal hooks used by the servers -----------------------------------------------
    def _count(self, name: str, n: int = 1) -> None:
        with self._lock:
            setattr(self, name, getattr(self, name) + n)

    def _log_request(self, method: Any, params: Any) -> None:
        with self._lock:
            self.request_log.append({"method": method, "params": params})

    def _log_raw(self, inner: Any) -> None:
        with self._lock:
            self.raw_request_log.append(inner)

    def _log_media(self, headers: dict, msg: Any) -> None:
        with self._lock:
            self.media_request_log.append({"headers": dict(headers), "body": msg})

    def _take_injected_error(self, method: Any) -> int | None:
        with self._lock:
            q = self._errors.get(method) if isinstance(method, str) else None
            return q.pop(0) if q else None

    def _take_http_status(self) -> int | None:
        with self._lock:
            return self._http_status.pop(0) if self._http_status else None

    def _take_rtsp_busy(self) -> bool:
        with self._lock:
            if self._rtsp_busy > 0:
                self._rtsp_busy -= 1
                return True
            return False

    def _lock_remaining(self) -> float:
        with self._lock:
            return max(0.0, self._locked_until - time.monotonic())

    def _register_failure(self) -> None:
        with self._lock:
            self._failures += 1
            if self.max_failed_logins and self._failures >= self.max_failed_logins:
                self._locked_until = time.monotonic() + self.lockout_seconds
                self._failures = 0

    def _reset_failures(self) -> None:
        with self._lock:
            self._failures = 0
