"""Lane-private ONVIF sample payloads and fakes for lane-tdd-core unit tests."""
from __future__ import annotations

import base64
import hashlib
import re
import threading
import xml.etree.ElementTree as ET
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer

SOAP12 = "http://www.w3.org/2003/05/soap-envelope"
WSSE = "http://docs.oasis-open.org/wss/2004/01/oasis-200401-wss-wssecurity-secext-1.0.xsd"
WSU = "http://docs.oasis-open.org/wss/2004/01/oasis-200401-wss-wssecurity-utility-1.0.xsd"
TDS = "http://www.onvif.org/ver10/device/wsdl"
TRT = "http://www.onvif.org/ver10/media/wsdl"
TPTZ = "http://www.onvif.org/ver20/ptz/wsdl"
TEV = "http://www.onvif.org/ver10/events/wsdl"
TT = "http://www.onvif.org/ver10/schema"
WSNT = "http://docs.oasis-open.org/wsn/b-2"
WSA = "http://www.w3.org/2005/08/addressing"
WSA_DISC = "http://schemas.xmlsoap.org/ws/2004/08/addressing"
DISC = "http://schemas.xmlsoap.org/ws/2005/04/discovery"
MOTION_TOPIC = "tns1:RuleEngine/CellMotionDetector/Motion"


def envelope(body: str) -> str:
    return (f'<?xml version="1.0" encoding="UTF-8"?>'
            f'<env:Envelope xmlns:env="{SOAP12}" xmlns:tds="{TDS}" xmlns:trt="{TRT}" xmlns:tptz="{TPTZ}" '
            f'xmlns:tev="{TEV}" xmlns:tt="{TT}" xmlns:wsnt="{WSNT}" xmlns:wsa="{WSA}" '
            f'xmlns:tns1="http://www.onvif.org/ver10/topics" xmlns:ter="http://www.onvif.org/ver10/error">'
            f'<env:Header/><env:Body>{body}</env:Body></env:Envelope>')


DEVICE_INFO_BODY = ("<tds:GetDeviceInformationResponse><tds:Manufacturer>tp-link</tds:Manufacturer>"
                    "<tds:Model>C210</tds:Model><tds:FirmwareVersion>1.3.9 Build 231019</tds:FirmwareVersion>"
                    "<tds:SerialNumber>2b9a7c11</tds:SerialNumber><tds:HardwareId>2.0</tds:HardwareId>"
                    "</tds:GetDeviceInformationResponse>")


def capabilities_body(base: str) -> str:
    return ("<tds:GetCapabilitiesResponse><tds:Capabilities>"
            f"<tt:Device><tt:XAddr>{base}/onvif/device_service</tt:XAddr></tt:Device>"
            f"<tt:Events><tt:XAddr>{base}/onvif/service</tt:XAddr>"
            "<tt:WSSubscriptionPolicySupport>true</tt:WSSubscriptionPolicySupport>"
            "<tt:WSPullPointSupport>true</tt:WSPullPointSupport></tt:Events>"
            f"<tt:Media><tt:XAddr>{base}/onvif/service</tt:XAddr></tt:Media>"
            f"<tt:PTZ><tt:XAddr>{base}/onvif/service</tt:XAddr></tt:PTZ>"
            "</tds:Capabilities></tds:GetCapabilitiesResponse>")


PROFILES_BODY = ('<trt:GetProfilesResponse>'
                 '<trt:Profiles token="profile_1" fixed="true"><tt:Name>mainStream</tt:Name></trt:Profiles>'
                 '<trt:Profiles token="profile_2" fixed="true"><tt:Name>minorStream</tt:Name></trt:Profiles>'
                 '</trt:GetProfilesResponse>')

PRESETS_BODY = ('<tptz:GetPresetsResponse>'
                '<tptz:Preset token="1"><tt:Name>Door</tt:Name></tptz:Preset>'
                '<tptz:Preset token="2"><tt:Name>Gate</tt:Name></tptz:Preset>'
                '</tptz:GetPresetsResponse>')

FAULT_BODY = ('<env:Fault><env:Code><env:Value>env:Sender</env:Value><env:Subcode>'
              '<env:Value>ter:NotAuthorized</env:Value></env:Subcode></env:Code>'
              '<env:Reason><env:Text xml:lang="en">Sender not Authorized</env:Text></env:Reason></env:Fault>')


def notification(active: bool, utc: str, topic: str = MOTION_TOPIC, item: str = "IsMotion") -> str:
    return ('<wsnt:NotificationMessage>'
            f'<wsnt:Topic Dialect="http://www.onvif.org/ver10/tev/topicExpression/ConcreteSet">{topic}</wsnt:Topic>'
            f'<wsnt:Message><tt:Message UtcTime="{utc}" PropertyOperation="Changed">'
            '<tt:Source><tt:SimpleItem Name="VideoSourceConfigurationToken" Value="vsconf"/>'
            '<tt:SimpleItem Name="Rule" Value="MyMotionDetectorRule"/></tt:Source>'
            f'<tt:Data><tt:SimpleItem Name="{item}" Value="{"true" if active else "false"}"/></tt:Data>'
            '</tt:Message></wsnt:Message></wsnt:NotificationMessage>')


def pull_messages_body(*notifications: str) -> str:
    return ("<tev:PullMessagesResponse><tev:CurrentTime>2025-01-03T10:00:05Z</tev:CurrentTime>"
            "<tev:TerminationTime>2025-01-03T10:01:05Z</tev:TerminationTime>"
            + "".join(notifications) + "</tev:PullMessagesResponse>")


PULL_MESSAGES_XML = envelope(pull_messages_body(
    notification(True, "2025-01-03T10:00:01Z"),
    notification(False, "2025-01-03T10:00:04.250Z"),
)).encode("utf-8")


def create_pullpoint_body(address: str) -> str:
    return ("<tev:CreatePullPointSubscriptionResponse><tev:SubscriptionReference>"
            f"<wsa:Address>{address}</wsa:Address></tev:SubscriptionReference>"
            "<wsnt:CurrentTime>2025-01-03T10:00:00Z</wsnt:CurrentTime>"
            "<wsnt:TerminationTime>2025-01-03T10:01:00Z</wsnt:TerminationTime>"
            "</tev:CreatePullPointSubscriptionResponse>")


def probe_matches(matches: list[tuple[str, str, str]], relates_to: str = "uuid:probe-1") -> bytes:
    """matches: (xaddrs, scopes, endpoint uuid)."""
    body = "".join(
        f"<d:ProbeMatch><wsa:EndpointReference><wsa:Address>urn:uuid:{uid}</wsa:Address></wsa:EndpointReference>"
        f"<d:Types>dn:NetworkVideoTransmitter</d:Types><d:Scopes>{scopes}</d:Scopes>"
        f"<d:XAddrs>{xaddrs}</d:XAddrs><d:MetadataVersion>1</d:MetadataVersion></d:ProbeMatch>"
        for xaddrs, scopes, uid in matches)
    return (f'<?xml version="1.0" encoding="UTF-8"?>'
            f'<SOAP-ENV:Envelope xmlns:SOAP-ENV="{SOAP12}" xmlns:wsa="{WSA_DISC}" xmlns:d="{DISC}" '
            f'xmlns:dn="http://www.onvif.org/ver10/network/wsdl">'
            f'<SOAP-ENV:Header><wsa:MessageID>uuid:reply-1</wsa:MessageID><wsa:RelatesTo>{relates_to}</wsa:RelatesTo>'
            f'<wsa:To>http://schemas.xmlsoap.org/ws/2004/08/addressing/role/anonymous</wsa:To>'
            f'<wsa:Action>http://schemas.xmlsoap.org/ws/2005/04/discovery/ProbeMatches</wsa:Action></SOAP-ENV:Header>'
            f'<SOAP-ENV:Body><d:ProbeMatches>{body}</d:ProbeMatches></SOAP-ENV:Body></SOAP-ENV:Envelope>'
            ).encode("utf-8")


def body_child(xml_text: str) -> ET.Element:
    root = ET.fromstring(xml_text)
    body = root.find(f"{{{SOAP12}}}Body")
    assert body is not None
    return list(body)[0]


def first_op(body_xml: str) -> str:
    m = re.search(r"<\s*(?:[\w.-]+:)?([A-Za-z]+)", body_xml)
    return m.group(1) if m else ""


def tag_attrs(body_xml: str, local: str) -> dict[str, str] | None:
    m = re.search(rf"<\s*(?:[\w.-]+:)?{local}\b([^>]*)>", body_xml)
    if not m:
        return None
    return dict(re.findall(r"""([\w:.-]+)\s*=\s*["']([^"']*)["']""", m.group(1)))


def tag_text(body_xml: str, local: str) -> str | None:
    m = re.search(rf"<\s*(?:[\w.-]+:)?{local}\b[^>]*>([^<]*)<", body_xml)
    return m.group(1).strip() if m else None


def wsse_digest(nonce: bytes, created: str, password: str) -> str:
    return base64.b64encode(hashlib.sha1(nonce + created.encode() + password.encode()).digest()).decode()


# --------------------------------------------------------------------------- canned SOAP server
class CannedOnvifServer:
    """Tiny HTTP server answering ONVIF SOAP calls with canned payloads; validates WS-Security."""

    def __init__(self, username: str = "camuser", password: str = "campass"):
        self.username = username
        self.password = password
        self.requests: list[tuple[str, dict[str, str], str]] = []
        self.garbage = False
        outer = self

        class Handler(BaseHTTPRequestHandler):
            def log_message(self, *a):  # quiet
                pass

            def do_POST(self):  # noqa: N802
                n = int(self.headers.get("Content-Length") or 0)
                body = self.rfile.read(n).decode("utf-8", "replace")
                outer.requests.append((self.path, dict(self.headers), body))
                status, payload = outer.answer(body)
                data = payload.encode("utf-8")
                self.send_response(status)
                self.send_header("Content-Type", "application/soap+xml; charset=utf-8")
                self.send_header("Content-Length", str(len(data)))
                self.end_headers()
                self.wfile.write(data)

        self.httpd = ThreadingHTTPServer(("127.0.0.1", 0), Handler)
        self.httpd.daemon_threads = True
        self.port = self.httpd.server_address[1]
        self.thread = threading.Thread(target=self.httpd.serve_forever, daemon=True)

    @property
    def base(self) -> str:
        return f"http://127.0.0.1:{self.port}"

    def authorized(self, body: str) -> bool:
        user = tag_text(body, "Username")
        pw = tag_text(body, "Password")
        nonce = tag_text(body, "Nonce")
        created = tag_text(body, "Created")
        if not (user and pw and nonce and created):
            return False
        try:
            raw_nonce = base64.b64decode(nonce)
        except ValueError:
            return False
        return user == self.username and pw == wsse_digest(raw_nonce, created, self.password)

    def answer(self, body: str) -> tuple[int, str]:
        if self.garbage:
            return 200, "this is not xml <<<"
        if not self.authorized(body):
            return 400, envelope(FAULT_BODY)
        if "GetDeviceInformation" in body:
            return 200, envelope(DEVICE_INFO_BODY)
        if "GetCapabilities" in body:
            return 200, envelope(capabilities_body(self.base))
        if "GetProfiles" in body:
            return 200, envelope(PROFILES_BODY)
        return 400, envelope(FAULT_BODY.replace("NotAuthorized", "ActionNotSupported"))

    def __enter__(self):
        self.thread.start()
        return self

    def __exit__(self, *exc):
        self.httpd.shutdown()
        self.httpd.server_close()
        self.thread.join(2)


# --------------------------------------------------------------------------- fake OnvifClient
class FakeOnvifClient:
    """Records calls; answers by operation name. Duck-types ``OnvifClient``."""

    def __init__(self, host: str = "10.0.0.5", profiles=(("profile_1", "mainStream"), ("profile_2", "minor"))):
        self.host = host
        self.port = 2020
        self.username = "camuser"
        self.calls: list[tuple[str, str]] = []
        self.profiles = list(profiles)
        self.lock = threading.Lock()
        self.fail_create = 0
        self.pull_queue: list[str] = []
        self.pull_delay = 0.05
        self.subscription_address = "http://10.0.0.5:2020/onvif/subscription?id=42"
        self.raise_on_pull: Exception | None = None

    def get_profiles(self) -> list[dict]:
        return [{"token": t, "name": n} for t, n in self.profiles]

    def get_capabilities(self) -> dict[str, str]:
        base = f"http://{self.host}:{self.port}"
        return {"device": base + "/onvif/device_service", "ptz": base + "/onvif/service",
                "events": base + "/onvif/service", "media": base + "/onvif/service"}

    def get_device_information(self) -> dict:
        return {"Manufacturer": "tp-link", "Model": "C210", "FirmwareVersion": "1", "SerialNumber": "s",
                "HardwareId": "2.0"}

    def ops(self) -> list[str]:
        with self.lock:
            return [first_op(b) for _p, b in self.calls]

    def bodies(self, op: str) -> list[str]:
        with self.lock:
            return [b for _p, b in self.calls if first_op(b) == op]

    def paths(self, op: str) -> list[str]:
        with self.lock:
            return [p for p, b in self.calls if first_op(b) == op]

    def call(self, path: str, body_xml: str) -> ET.Element:
        import time

        from tapo_desktop.core.errors import TapoConnectionError

        with self.lock:
            self.calls.append((path, body_xml))
        op = first_op(body_xml)
        if op == "GetProfiles":
            prof = "".join(f'<trt:Profiles token="{t}"><tt:Name>{n}</tt:Name></trt:Profiles>' for t, n in self.profiles)
            return body_child(envelope(f"<trt:GetProfilesResponse>{prof}</trt:GetProfilesResponse>"))
        if op == "GetPresets":
            return body_child(envelope(PRESETS_BODY))
        if op == "CreatePullPointSubscription":
            with self.lock:
                if self.fail_create > 0:
                    self.fail_create -= 1
                    raise TapoConnectionError("simulated network failure")
            return body_child(envelope(create_pullpoint_body(self.subscription_address)))
        if op == "PullMessages":
            if self.raise_on_pull is not None:
                raise self.raise_on_pull
            time.sleep(self.pull_delay)
            with self.lock:
                notes = self.pull_queue[:]
                self.pull_queue.clear()
            return body_child(envelope(pull_messages_body(*notes)))
        return body_child(envelope(f"<tptz:{op}Response/>" if op else "<tptz:EmptyResponse/>"))
