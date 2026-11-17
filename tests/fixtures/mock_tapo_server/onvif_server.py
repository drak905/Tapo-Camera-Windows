"""ONVIF :2020 (HTTP SOAP 1.2) of the mock camera ([spec:onvif.*]).

Paths: /onvif/device_service, /onvif/service, subscription managers at /onvif/subscription/<id>
(a PullMessages/Renew/Unsubscribe posted to /onvif/service is routed by wsa:To, or to the only
live subscription). Every operation except GetSystemDateAndTime needs a WS-Security
UsernameToken with PasswordDigest = base64(sha1(nonce + created + password)) for the camera
account; otherwise SOAP Fault ter:NotAuthorized (HTTP 400).
"""
from __future__ import annotations

import base64
import binascii
import itertools
import threading
import time
import xml.etree.ElementTree as ET
from collections import deque
from dataclasses import dataclass, field
from datetime import datetime, timedelta, timezone
from http.server import BaseHTTPRequestHandler
from typing import TYPE_CHECKING
from urllib.parse import urlsplit
from xml.sax.saxutils import escape, quoteattr

from . import onvif_xml as X
from ._net import TrackedHTTPServer
from .shadow import PAN_LIMIT, STEP, TILT_LIMIT

if TYPE_CHECKING:
    from .camera import MockTapoCamera

MAX_PULL_TIMEOUT = 60.0


@dataclass
class Subscription:
    id: int
    expires: float                     # time.monotonic() deadline
    queue: deque = field(default_factory=deque)


class OnvifServer:
    def __init__(self, cam: "MockTapoCamera", host: str, port: int):
        self.cam = cam
        self.server = TrackedHTTPServer((host, port), _Handler, name="mock-onvif")
        self.server.onvif = self  # type: ignore[attr-defined]
        self.port = self.server.server_address[1]
        self.cond = threading.Condition()
        self.subs: dict[int, Subscription] = {}
        self.pending: list[tuple[bool, datetime]] = []   # emitted before any subscription existed
        self._ids = itertools.count(1)
        self.op_log: list[str] = []

    def start(self) -> None:
        self.server.start()

    def stop(self) -> None:
        with self.cond:
            self.cond.notify_all()
        self.server.stop()

    @property
    def base(self) -> str:
        return f"http://{self.cam.host}:{self.port}"

    # -- events ---------------------------------------------------------------------------
    def emit(self, active: bool) -> None:
        ev = (active, datetime.now(timezone.utc))
        with self.cond:
            live = [s for s in self.subs.values() if s.expires > time.monotonic()]
            if live:
                for s in live:
                    s.queue.append(ev)
            else:
                self.pending.append(ev)
            self.cond.notify_all()

    def _sub_for(self, path: str, root: ET.Element) -> Subscription | None:
        sid = None
        if path.startswith("/onvif/subscription/"):
            sid = path.rsplit("/", 1)[-1]
        else:
            to = X.text_local(root, "To")
            if "/onvif/subscription/" in to:
                sid = to.rsplit("/", 1)[-1]
        with self.cond:
            if sid is not None:
                try:
                    return self.subs.get(int(sid))
                except ValueError:
                    return None
            return next(iter(self.subs.values())) if len(self.subs) == 1 else None

    # -- auth -----------------------------------------------------------------------------
    def authorized(self, root: ET.Element) -> bool:
        token = X.find_local(root, "UsernameToken")
        if token is None:
            return False
        user = X.text_local(token, "Username")
        pw_el = X.find_local(token, "Password")
        nonce_b64 = X.text_local(token, "Nonce")
        created = X.text_local(token, "Created")
        if pw_el is None or user != self.cam.camera_user or not created:
            return False
        if "PasswordDigest" not in (pw_el.get("Type") or ""):
            return False
        try:
            nonce = base64.b64decode(nonce_b64, validate=True)
        except (binascii.Error, ValueError):
            return False
        expected = X.password_digest(nonce, created, self.cam.camera_password)
        return (pw_el.text or "").strip() == expected

    # -- operations -----------------------------------------------------------------------
    def handle(self, path: str, data: bytes) -> tuple[int, bytes]:
        try:
            root = ET.fromstring(data)
        except ET.ParseError:
            return 400, X.fault("ter:WellFormed", "XML not well formed")
        body = X.find_local(root, "Body")
        op_el = next(iter(body), None) if body is not None else None
        if op_el is None:
            return 400, X.fault("ter:WellFormed", "empty SOAP body")
        op = X.local(op_el.tag)
        self.op_log.append(op)
        if op == "GetSystemDateAndTime":
            now = datetime.now(timezone.utc)
            return 200, X.envelope(
                f"<tds:GetSystemDateAndTimeResponse><tds:SystemDateAndTime><tt:DateTimeType>NTP</tt:DateTimeType>"
                f"<tt:DaylightSavings>false</tt:DaylightSavings><tt:UTCDateTime><tt:Time><tt:Hour>{now.hour}</tt:Hour>"
                f"<tt:Minute>{now.minute}</tt:Minute><tt:Second>{now.second}</tt:Second></tt:Time><tt:Date>"
                f"<tt:Year>{now.year}</tt:Year><tt:Month>{now.month}</tt:Month><tt:Day>{now.day}</tt:Day></tt:Date>"
                f"</tt:UTCDateTime></tds:SystemDateAndTime></tds:GetSystemDateAndTimeResponse>")
        if not self.authorized(root):
            return 400, X.fault("ter:NotAuthorized", "Sender not Authorized")
        fn = getattr(self, f"op_{op}", None)
        if fn is None:
            return 400, X.fault("ter:ActionNotSupported", f"{op} not supported")
        return fn(op_el, path, root)

    def op_GetDeviceInformation(self, el, path, root):
        s = self.cam.state
        return 200, X.envelope(
            f"<tds:GetDeviceInformationResponse><tds:Manufacturer>tp-link</tds:Manufacturer>"
            f"<tds:Model>{escape(s.model)}</tds:Model><tds:FirmwareVersion>{escape(s.firmware)}</tds:FirmwareVersion>"
            f"<tds:SerialNumber>{escape(s.mac.replace('-', '').lower())}</tds:SerialNumber>"
            f"<tds:HardwareId>{escape(s.hardware)}</tds:HardwareId></tds:GetDeviceInformationResponse>")

    def op_GetCapabilities(self, el, path, root):
        svc = f"{self.base}/onvif/service"
        return 200, X.envelope(
            f"<tds:GetCapabilitiesResponse><tds:Capabilities>"
            f"<tt:Device><tt:XAddr>{self.base}/onvif/device_service</tt:XAddr></tt:Device>"
            f"<tt:Events><tt:XAddr>{svc}</tt:XAddr><tt:WSSubscriptionPolicySupport>true</tt:WSSubscriptionPolicySupport>"
            f"<tt:WSPullPointSupport>true</tt:WSPullPointSupport></tt:Events>"
            f"<tt:Imaging><tt:XAddr>{svc}</tt:XAddr></tt:Imaging>"
            f"<tt:Media><tt:XAddr>{svc}</tt:XAddr><tt:StreamingCapabilities><tt:RTP_RTSP_TCP>true</tt:RTP_RTSP_TCP>"
            f"</tt:StreamingCapabilities></tt:Media>"
            f"<tt:PTZ><tt:XAddr>{svc}</tt:XAddr></tt:PTZ></tds:Capabilities></tds:GetCapabilitiesResponse>")

    def op_GetProfiles(self, el, path, root):
        profiles = [("profile_1", "mainStream", 1280, 720), ("profile_2", "minorStream", 640, 360)]
        items = "".join(
            f'<trt:Profiles token="{t}" fixed="true"><tt:Name>{n}</tt:Name>'
            f'<tt:VideoEncoderConfiguration token="vec_{t}"><tt:Name>{n}</tt:Name><tt:Encoding>H264</tt:Encoding>'
            f"<tt:Resolution><tt:Width>{w}</tt:Width><tt:Height>{h}</tt:Height></tt:Resolution>"
            f"</tt:VideoEncoderConfiguration>"
            f'<tt:PTZConfiguration token="PTZConfig"><tt:Name>PTZConfig</tt:Name><tt:NodeToken>PTZNode</tt:NodeToken>'
            f"</tt:PTZConfiguration></trt:Profiles>" for t, n, w, h in profiles)
        return 200, X.envelope(f"<trt:GetProfilesResponse>{items}</trt:GetProfilesResponse>")

    def op_ContinuousMove(self, el, path, root):
        pt = X.find_local(el, "PanTilt")
        if pt is None:
            return 400, X.fault("ter:InvalidArgVal", "Velocity/PanTilt missing")
        try:
            x, y = float(pt.get("x", "0")), float(pt.get("y", "0"))
        except ValueError:
            return 400, X.fault("ter:InvalidArgVal", "bad velocity")
        x, y = max(-1.0, min(1.0, x)), max(-1.0, min(1.0, y))
        s = self.cam.state
        with s.lock:
            s.clamp_move(int(round(x * STEP)), int(round(y * STEP)))
            s.ptz_moving = True
        return 200, X.envelope("<tptz:ContinuousMoveResponse/>")

    def op_Stop(self, el, path, root):
        self.cam.state.ptz_moving = False
        return 200, X.envelope("<tptz:StopResponse/>")

    def op_GetPresets(self, el, path, root):
        s = self.cam.state
        with s.lock:
            items = "".join(
                f'<tptz:Preset token={quoteattr(pid)}><tt:Name>{escape(p["name"])}</tt:Name><tt:PTZPosition>'
                f'<tt:PanTilt x="{p["x"] / PAN_LIMIT:.6f}" y="{p["y"] / TILT_LIMIT:.6f}"/></tt:PTZPosition></tptz:Preset>'
                for pid, p in sorted(s.presets.items(), key=lambda kv: (len(kv[0]), kv[0])))
        return 200, X.envelope(f"<tptz:GetPresetsResponse>{items}</tptz:GetPresetsResponse>")

    def op_GotoPreset(self, el, path, root):
        token = X.text_local(el, "PresetToken")
        s = self.cam.state
        with s.lock:
            p = s.presets.get(token)
            if p is None:
                return 400, X.fault("ter:InvalidArgVal", "NoToken")
            s.ptz_x, s.ptz_y = p["x"], p["y"]
        return 200, X.envelope("<tptz:GotoPresetResponse/>")

    def _times(self, sub: Subscription) -> str:
        now = datetime.now(timezone.utc)
        term = now + timedelta(seconds=max(0.0, sub.expires - time.monotonic()))
        return f"<wsnt:CurrentTime>{X.iso(now)}</wsnt:CurrentTime><wsnt:TerminationTime>{X.iso(term)}</wsnt:TerminationTime>"

    def op_CreatePullPointSubscription(self, el, path, root):
        ttl = X.parse_duration(X.text_local(el, "InitialTerminationTime"), 60.0)
        sub = Subscription(next(self._ids), time.monotonic() + ttl)
        with self.cond:
            self.subs[sub.id] = sub
            sub.queue.extend(self.pending)
            self.pending.clear()
        addr = f"{self.base}/onvif/subscription/{sub.id}"
        return 200, X.envelope(
            f"<tev:CreatePullPointSubscriptionResponse><tev:SubscriptionReference><wsa:Address>{addr}</wsa:Address>"
            f"</tev:SubscriptionReference>{self._times(sub)}</tev:CreatePullPointSubscriptionResponse>")

    def _live_sub(self, path, root) -> Subscription | None:
        sub = self._sub_for(path, root)
        if sub is None or sub.expires <= time.monotonic():
            return None
        return sub

    def op_PullMessages(self, el, path, root):
        sub = self._live_sub(path, root)
        if sub is None:
            return 400, X.fault("ter:InvalidArgVal", "unknown or expired subscription")
        timeout = min(MAX_PULL_TIMEOUT, X.parse_duration(X.text_local(el, "Timeout"), 5.0))
        try:
            limit = max(1, int(X.text_local(el, "MessageLimit", "10")))
        except ValueError:
            limit = 10
        deadline = time.monotonic() + timeout
        stopping = self.server.stopping
        with self.cond:
            while not sub.queue and not stopping.is_set():
                left = deadline - time.monotonic()
                if left <= 0:
                    break
                self.cond.wait(min(left, 0.1))
            msgs = [sub.queue.popleft() for _ in range(min(limit, len(sub.queue)))]
        notes = "".join(X.notification(X.MOTION_TOPIC, a, t) for a, t in msgs)
        return 200, X.envelope(f"<tev:PullMessagesResponse>{self._times(sub)}{notes}</tev:PullMessagesResponse>")

    def op_Renew(self, el, path, root):
        sub = self._live_sub(path, root)
        if sub is None:
            return 400, X.fault("ter:InvalidArgVal", "unknown or expired subscription")
        ttl = X.parse_duration(X.text_local(el, "TerminationTime"), 60.0)
        sub.expires = time.monotonic() + ttl
        return 200, X.envelope(f"<wsnt:RenewResponse>{self._times(sub)}</wsnt:RenewResponse>")

    def op_Unsubscribe(self, el, path, root):
        sub = self._sub_for(path, root)
        if sub is None:
            return 400, X.fault("ter:InvalidArgVal", "unknown subscription")
        with self.cond:
            self.subs.pop(sub.id, None)
        return 200, X.envelope("<wsnt:UnsubscribeResponse/>")

    # -- WS-Discovery ---------------------------------------------------------------------
    def probe_match(self, probe: bytes | None) -> bytes:
        relates = "uuid:00000000-0000-0000-0000-000000000000"
        if probe:
            try:
                root = ET.fromstring(probe)
            except ET.ParseError:
                return b""
            if X.find_local(root, "Probe") is None:
                return b""
            relates = X.text_local(root, "MessageID", relates)
        s = self.cam.state
        uid = f"uuid:3fa1fe68-b915-4053-a3e1-{s.mac.replace('-', '').lower()}"
        scopes = (f"onvif://www.onvif.org/name/{s.model} onvif://www.onvif.org/hardware/{s.model} "
                  "onvif://www.onvif.org/Profile/Streaming onvif://www.onvif.org/type/NetworkVideoTransmitter")
        return (
            '<?xml version="1.0" encoding="UTF-8"?>'
            '<SOAP-ENV:Envelope xmlns:SOAP-ENV="http://www.w3.org/2003/05/soap-envelope" '
            'xmlns:wsa="http://schemas.xmlsoap.org/ws/2004/08/addressing" '
            'xmlns:d="http://schemas.xmlsoap.org/ws/2005/04/discovery" '
            'xmlns:dn="http://www.onvif.org/ver10/network/wsdl">'
            f"<SOAP-ENV:Header><wsa:MessageID>{uid}-reply</wsa:MessageID><wsa:RelatesTo>{escape(relates)}</wsa:RelatesTo>"
            "<wsa:To>http://schemas.xmlsoap.org/ws/2004/08/addressing/role/anonymous</wsa:To>"
            "<wsa:Action>http://schemas.xmlsoap.org/ws/2005/04/discovery/ProbeMatches</wsa:Action></SOAP-ENV:Header>"
            f"<SOAP-ENV:Body><d:ProbeMatches><d:ProbeMatch><wsa:EndpointReference><wsa:Address>{uid}</wsa:Address>"
            "</wsa:EndpointReference><d:Types>dn:NetworkVideoTransmitter</d:Types>"
            f"<d:Scopes>{scopes}</d:Scopes><d:XAddrs>{self.base}/onvif/device_service</d:XAddrs>"
            "<d:MetadataVersion>1</d:MetadataVersion></d:ProbeMatch></d:ProbeMatches></SOAP-ENV:Body>"
            "</SOAP-ENV:Envelope>").encode("utf-8")


class _Handler(BaseHTTPRequestHandler):
    protocol_version = "HTTP/1.1"
    server_version = "gSOAP/2.8"
    sys_version = ""
    timeout = 5.0  # idle keep-alive; stop() closes live connections actively

    def log_message(self, format, *args):  # noqa: A002
        pass

    def do_POST(self):  # noqa: N802
        onvif: OnvifServer = self.server.onvif  # type: ignore[attr-defined]
        try:
            n = int(self.headers.get("Content-Length") or 0)
        except ValueError:
            n = 0
        data = self.rfile.read(n) if n > 0 else b""
        path = urlsplit(self.path).path
        if not (path in ("/onvif/device_service", "/onvif/service") or path.startswith("/onvif/subscription/")):
            status, payload = 404, X.fault("ter:ActionNotSupported", "unknown service path")
        else:
            status, payload = onvif.handle(path, data)
        self.send_response(status)
        self.send_header("Content-Type", "application/soap+xml; charset=utf-8")
        self.send_header("Content-Length", str(len(payload)))
        self.end_headers()
        self.wfile.write(payload)
        self.wfile.flush()
