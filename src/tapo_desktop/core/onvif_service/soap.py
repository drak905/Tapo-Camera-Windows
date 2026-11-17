"""Minimal SOAP 1.2 / WS-Security client for the camera's ONVIF service (port 2020) [spec:onvif.port]."""
from __future__ import annotations

import xml.etree.ElementTree


def wsse_header(username: str, password: str, *, created: str | None = None, nonce: bytes | None = None) -> str:
    """WS-Security UsernameToken header XML.

    PasswordDigest = base64(sha1(nonce + created + password)); ``created`` defaults to the current UTC
    time (``YYYY-MM-DDTHH:MM:SSZ``), ``nonce`` to 16 random bytes. Namespaces are declared on the
    header element itself so the fragment is self-contained.
    """
    raise NotImplementedError


def build_envelope(body_xml: str, header_xml: str = "") -> str:
    """SOAP 1.2 Envelope (``http://www.w3.org/2003/05/soap-envelope``) with Header and Body."""
    raise NotImplementedError


class OnvifClient:
    """Authenticated ONVIF SOAP client (camera account credentials)."""

    def __init__(self, host: str, username: str, password: str, *, port: int = 2020, timeout: float = 5.0):
        raise NotImplementedError

    def call(self, path: str, body_xml: str) -> xml.etree.ElementTree.Element:
        """POST a SOAP request to ``http://host:port{path}`` and return the first child of Body.

        SOAP Fault -> TapoDeviceError(code=-1); network failure -> TapoConnectionError;
        unparsable response -> TapoProtocolError.
        """
        raise NotImplementedError

    def get_device_information(self) -> dict:
        """Manufacturer, Model, FirmwareVersion, SerialNumber, HardwareId."""
        raise NotImplementedError

    def get_capabilities(self) -> dict[str, str]:
        """Service XAddrs by lowercase category: ``{"ptz": url, "events": url, "media": url, ...}``."""
        raise NotImplementedError

    def get_profiles(self) -> list[dict]:
        """Media profiles ``[{"token", "name"}]``."""
        raise NotImplementedError
