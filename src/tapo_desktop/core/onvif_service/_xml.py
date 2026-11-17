"""Private XML helpers shared by the ONVIF modules (namespaces, tolerant lookup, durations, times).

Payload elements are matched by *local name* inside the namespace-aware ElementTree so the
parsers accept the ONVIF schema variants cameras use (e.g. ver10/ver20 PTZ, SOAP 1.1/1.2,
WS-Addressing 2004/2005). Envelope-level structure is checked against the exact namespaces.
"""
from __future__ import annotations

import math
import re
import xml.etree.ElementTree as ET
from collections.abc import Iterator
from datetime import datetime, timezone
from urllib.parse import urlsplit

SOAP12 = "http://www.w3.org/2003/05/soap-envelope"
SOAP11 = "http://schemas.xmlsoap.org/soap/envelope/"
SOAP_NAMESPACES = (SOAP12, SOAP11)
WSSE = "http://docs.oasis-open.org/wss/2004/01/oasis-200401-wss-wssecurity-secext-1.0.xsd"
WSU = "http://docs.oasis-open.org/wss/2004/01/oasis-200401-wss-wssecurity-utility-1.0.xsd"
TDS = "http://www.onvif.org/ver10/device/wsdl"
TRT = "http://www.onvif.org/ver10/media/wsdl"
TPTZ = "http://www.onvif.org/ver20/ptz/wsdl"
TEV = "http://www.onvif.org/ver10/events/wsdl"
TT = "http://www.onvif.org/ver10/schema"
WSNT = "http://docs.oasis-open.org/wsn/b-2"
WSA = "http://www.w3.org/2005/08/addressing"
WSA_2004 = "http://schemas.xmlsoap.org/ws/2004/08/addressing"
DISCOVERY_2005 = "http://schemas.xmlsoap.org/ws/2005/04/discovery"
DISCOVERY_2009 = "http://docs.oasis-open.org/ws-dd/ns/discovery/2009/01"
DN = "http://www.onvif.org/ver10/network/wsdl"

DEFAULT_SERVICE_PATH = "/onvif/service"
DEVICE_SERVICE_PATH = "/onvif/device_service"

_OP_RE = re.compile(r"<\s*(?:[\w.-]+:)?([A-Za-z_][\w.-]*)")


def local(tag: object) -> str:
    """Local part of a ``{namespace}name`` tag (``""`` for comments / processing instructions)."""
    return tag.rsplit("}", 1)[-1] if isinstance(tag, str) else ""


def namespace(tag: object) -> str:
    """Namespace URI of a ``{namespace}name`` tag (``""`` if none)."""
    return tag[1:].split("}", 1)[0] if isinstance(tag, str) and tag.startswith("{") else ""


def iter_local(root: ET.Element, name: str) -> Iterator[ET.Element]:
    """All descendants (and ``root`` itself) whose local name is ``name``, in document order."""
    return (el for el in root.iter() if local(el.tag) == name)


def find_local(root: ET.Element, name: str) -> ET.Element | None:
    """First element (depth-first, including ``root``) with local name ``name``."""
    return next(iter_local(root, name), None)


def child_local(parent: ET.Element, name: str) -> ET.Element | None:
    """First *direct* child of ``parent`` with local name ``name``."""
    return next((c for c in parent if local(c.tag) == name), None)


def text_local(root: ET.Element, name: str, default: str = "") -> str:
    """Stripped text of the first element named ``name`` (``default`` if absent or empty)."""
    el = find_local(root, name)
    text = (el.text or "").strip() if el is not None else ""
    return text or default


def safe_parse(data: bytes | str) -> ET.Element | None:
    """Parse untrusted XML; ``None`` on any error. DTDs are refused (entity-expansion hardening)."""
    if not data:
        return None
    raw = data.encode("utf-8", "replace") if isinstance(data, str) else data
    if b"<!DOCTYPE" in raw or b"<!ENTITY" in raw:
        return None
    try:
        return ET.fromstring(raw)
    except (ET.ParseError, ValueError, TypeError, UnicodeError):
        return None


def soap_body(root: ET.Element) -> ET.Element | None:
    """The SOAP Body of an Envelope ``root`` (SOAP 1.2 or 1.1), else ``None``."""
    if local(root.tag) != "Envelope" or namespace(root.tag) not in SOAP_NAMESPACES:
        return None
    return next((c for c in root if local(c.tag) == "Body" and namespace(c.tag) in SOAP_NAMESPACES), None)


def operation_name(body_xml: str) -> str:
    """Local name of the first element in a request body fragment (for logs / error messages)."""
    m = _OP_RE.search(body_xml or "")
    return m.group(1) if m else ""


def url_path(url: str, default: str = DEFAULT_SERVICE_PATH) -> str:
    """Path (+ query) of an absolute URL; ``default`` if the URL has no usable path."""
    try:
        parts = urlsplit((url or "").strip())
    except ValueError:
        return default
    path = parts.path or ""
    if not path.startswith("/"):
        return default
    return f"{path}?{parts.query}" if parts.query else path


def fmt_number(value: float, digits: int = 6) -> str:
    """Compact decimal for xs:float / xs:duration values (``0.5``, ``-1``, ``2.5``); NaN -> ``0``."""
    if not math.isfinite(value):
        return "0"
    text = f"{value:.{digits}f}".rstrip("0").rstrip(".")
    return "0" if text in ("", "-0") else text


def fmt_duration(seconds: float) -> str:
    """xs:duration in seconds, e.g. ``PT5S`` / ``PT2.5S`` (negative values become ``PT0S``)."""
    return f"PT{fmt_number(max(0.0, seconds), 3)}S"


_DURATION_RE = re.compile(r"^P(?:(\d+)D)?(?:T(?:(\d+)H)?(?:(\d+)M)?(?:(\d+(?:\.\d+)?)S)?)?$")


def parse_duration(text: str) -> float | None:
    """Seconds of a (day-time) xs:duration like ``PT1M30S``; ``None`` if unparsable."""
    m = _DURATION_RE.match((text or "").strip())
    if not m or not any(m.groups()):
        return None
    d, h, mi, s = m.groups()
    return int(d or 0) * 86400 + int(h or 0) * 3600 + int(mi or 0) * 60 + float(s or 0)


def parse_utc(text: str) -> datetime | None:
    """xs:dateTime -> timezone-aware UTC datetime (naive values are taken as UTC); ``None`` if bad."""
    value = (text or "").strip()
    if not value:
        return None
    if value[-1:] in ("Z", "z"):
        value = value[:-1] + "+00:00"
    try:
        ts = datetime.fromisoformat(value)
    except (ValueError, OverflowError):
        return None
    if ts.tzinfo is None:
        return ts.replace(tzinfo=timezone.utc)
    try:
        return ts.astimezone(timezone.utc)
    except (ValueError, OverflowError):
        return None
