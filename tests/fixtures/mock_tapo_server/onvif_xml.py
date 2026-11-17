"""SOAP 1.2 / ONVIF XML helpers for the mock ONVIF service."""
from __future__ import annotations

import base64
import hashlib
import re
import xml.etree.ElementTree as ET
from datetime import datetime, timezone
from xml.sax.saxutils import escape

NS = {
    "env": "http://www.w3.org/2003/05/soap-envelope",
    "tds": "http://www.onvif.org/ver10/device/wsdl",
    "trt": "http://www.onvif.org/ver10/media/wsdl",
    "tptz": "http://www.onvif.org/ver20/ptz/wsdl",
    "tev": "http://www.onvif.org/ver10/events/wsdl",
    "tt": "http://www.onvif.org/ver10/schema",
    "wsnt": "http://docs.oasis-open.org/wsn/b-2",
    "wsa": "http://www.w3.org/2005/08/addressing",
    "tns1": "http://www.onvif.org/ver10/topics",
    "ter": "http://www.onvif.org/ver10/error",
    "wsse": "http://docs.oasis-open.org/wss/2004/01/oasis-200401-wss-wssecurity-secext-1.0.xsd",
    "wsu": "http://docs.oasis-open.org/wss/2004/01/oasis-200401-wss-wssecurity-utility-1.0.xsd",
    "d": "http://schemas.xmlsoap.org/ws/2005/04/discovery",
    "dn": "http://www.onvif.org/ver10/network/wsdl",
    "wsa04": "http://schemas.xmlsoap.org/ws/2004/08/addressing",
}
MOTION_TOPIC = "tns1:RuleEngine/CellMotionDetector/Motion"
_NS_DECL = " ".join(f'xmlns:{k}="{v}"' for k, v in NS.items() if k not in ("d", "dn", "wsa04"))


def local(tag: str) -> str:
    return tag.rsplit("}", 1)[-1]


def find_local(root: ET.Element, name: str) -> ET.Element | None:
    for el in root.iter():
        if local(el.tag) == name:
            return el
    return None


def text_local(root: ET.Element, name: str, default: str = "") -> str:
    el = find_local(root, name)
    return (el.text or "").strip() if el is not None and el.text else default


def iso(dt: datetime) -> str:
    return dt.astimezone(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")


def envelope(body: str, header: str = "") -> bytes:
    hdr = f"<env:Header>{header}</env:Header>" if header else ""
    return (f'<?xml version="1.0" encoding="UTF-8"?><env:Envelope {_NS_DECL}>{hdr}<env:Body>{body}</env:Body>'
            f"</env:Envelope>").encode("utf-8")


def fault(subcode: str, reason: str, code: str = "env:Sender") -> bytes:
    return envelope(f"<env:Fault><env:Code><env:Value>{code}</env:Value><env:Subcode><env:Value>{subcode}"
                    f"</env:Value></env:Subcode></env:Code><env:Reason><env:Text xml:lang=\"en\">{escape(reason)}"
                    f"</env:Text></env:Reason></env:Fault>")


def password_digest(nonce: bytes, created: str, password: str) -> str:
    return base64.b64encode(hashlib.sha1(nonce + created.encode("utf-8") + password.encode("utf-8")).digest()).decode()


_DUR_RE = re.compile(r"^P(?:(\d+)D)?(?:T(?:(\d+)H)?(?:(\d+)M)?(?:(\d+(?:\.\d+)?)S)?)?$")


def parse_duration(s: str, default: float) -> float:
    m = _DUR_RE.match((s or "").strip())
    if not m or not any(m.groups()):
        return default
    d, h, mi, sec = m.groups()
    return int(d or 0) * 86400 + int(h or 0) * 3600 + int(mi or 0) * 60 + float(sec or 0)


def notification(topic: str, active: bool, when: datetime, prop: str = "Changed") -> str:
    v = "true" if active else "false"
    return (f'<wsnt:NotificationMessage><wsnt:Topic Dialect="http://www.onvif.org/ver10/tev/topicExpression/'
            f'ConcreteSet">{topic}</wsnt:Topic><wsnt:Message><tt:Message UtcTime="{iso(when)}" '
            f'PropertyOperation="{prop}"><tt:Source><tt:SimpleItem Name="VideoSourceConfigurationToken" '
            f'Value="vsconf"/><tt:SimpleItem Name="VideoAnalyticsConfigurationToken" Value="VideoAnalyticsToken"/>'
            f'<tt:SimpleItem Name="Rule" Value="MyMotionDetectorRule"/></tt:Source><tt:Data>'
            f'<tt:SimpleItem Name="IsMotion" Value="{v}"/></tt:Data></tt:Message></wsnt:Message>'
            f"</wsnt:NotificationMessage>")
