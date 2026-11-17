"""Shared data models (contract owned by the orchestrator; see docs/ARCHITECTURE.md)."""
from __future__ import annotations

import enum
import uuid
from dataclasses import dataclass, field
from datetime import datetime


class StreamQuality(str, enum.Enum):
    """RTSP stream paths served by Tapo firmware on port 554."""

    HD = "stream1"
    SD = "stream2"
    TELE_HD = "stream6"  # second lens (dual-lens / telephoto models)
    TELE_SD = "stream7"


class DayNightMode(str, enum.Enum):
    """Values of image.common.inf_type."""

    AUTO = "auto"
    ON = "on"  # forced IR night vision
    OFF = "off"  # forced day mode


class CredentialKind(str, enum.Enum):
    CLOUD = "cloud"  # TP-Link cloud account password; HTTPS :443 control + :8800 media (user "admin")
    CAMERA_ACCOUNT = "camera_account"  # local camera account; RTSP :554 + ONVIF :2020


@dataclass(frozen=True)
class Credentials:
    username: str
    password: str

    def __repr__(self) -> str:  # never leak secrets into logs
        return f"Credentials(username={self.username!r}, password='***')"


@dataclass
class CameraConfig:
    """Non-secret, persisted camera settings. Secrets live in the DPAPI vault."""

    name: str
    host: str
    id: str = field(default_factory=lambda: uuid.uuid4().hex)
    https_port: int = 443
    rtsp_port: int = 554
    onvif_port: int = 2020
    media_port: int = 8800
    dual_lens: bool = False
    grid_quality: StreamQuality = StreamQuality.SD
    fullscreen_quality: StreamQuality = StreamQuality.HD
    enable_onvif_events: bool = True
    # Phase 2 (mobile parity): cameras imported from the TP-Link cloud account
    source: str = "manual"  # "manual" (added by IP) | "cloud" (from the signed-in account)
    device_id: str = ""  # cloud deviceId (stable identity; cloud cameras use it as `id`)
    mac: str = ""  # "AA-BB-CC-DD-EE-FF" style as reported by cloud/TDP, normalised upper-case
    model: str = ""  # e.g. "C210"
    online: bool = True  # cloud status (status != 0); refreshed on sign-in / refresh
    lan_found: bool = True  # host was located on the LAN (TDP or manual); False -> host is stale/empty
    live_transport: str = "auto"  # "auto": RTSP if a camera account exists, else local :8800 | "local" | "rtsp"


@dataclass(frozen=True)
class AccountInfo:
    """Signed-in TP-Link ID (secret password lives in the vault under camera_id ACCOUNT_VAULT_ID)."""

    email: str
    nickname: str = ""
    account_id: str = ""
    region_url: str = ""


ACCOUNT_VAULT_ID = "__tplink_account__"


@dataclass(frozen=True)
class CloudDevice:
    device_id: str
    mac: str
    model: str
    alias: str
    device_type: str = "SMART.IPCAMERA"
    online: bool = True
    fw_version: str = ""
    hw_version: str = ""
    role: int = 0  # 0 = owner, otherwise shared
    raw: dict = field(default_factory=dict, compare=False, repr=False)


@dataclass(frozen=True)
class LanDevice:
    ip: str
    mac: str
    model: str = ""
    device_type: str = ""
    device_id_md5: str = ""  # lowercase hex md5(cloud deviceId) as broadcast by TDP
    http_port: int | None = None  # local HTTPS control port from encrypt_info (None -> 443)
    source: str = "tdp"  # "tdp" | "onvif"
    raw: dict = field(default_factory=dict, compare=False, repr=False)


@dataclass(frozen=True)
class DeviceInfo:
    model: str
    alias: str
    firmware: str
    hardware: str = ""
    mac: str = ""
    device_id: str = ""
    raw: dict = field(default_factory=dict, compare=False, repr=False)


@dataclass(frozen=True)
class Preset:
    id: str
    name: str
    pan: float = 0.0
    tilt: float = 0.0
    read_only: bool = False


@dataclass(frozen=True)
class RecordingClip:
    start: datetime  # timezone-aware UTC
    end: datetime  # timezone-aware UTC
    event_type: int = 0  # PlayBackEventType code (1 timing, 2 motion, 6 person, 8 vehicle, 9 pet ...)

    @property
    def duration_seconds(self) -> float:
        return (self.end - self.start).total_seconds()


@dataclass(frozen=True)
class MotionEvent:
    camera_host: str
    topic: str  # e.g. "tns1:RuleEngine/CellMotionDetector/Motion"
    active: bool
    timestamp: datetime
    source: str = "onvif"
    data: dict = field(default_factory=dict, compare=False)
