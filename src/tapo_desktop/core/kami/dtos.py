"""Kami Home reply parsers, session record and errors (no network, no vendor secret).

Shapes come from the app's parsers, not from real replies [spec:kami.auth.login_response, kami.cloud.device_list,
kami.cloud.device_fields]. Per-camera passwords and P2P parameters in the device list are ignored on purpose.
"""
from __future__ import annotations

import json
from dataclasses import dataclass, field
from typing import Any

from tapo_desktop.core.errors import TapoConnectionError, TapoError
from tapo_desktop.core.kami.models import KamiDevice

CODE_OK = 20000
#: The app logs the user out on this code of any signed call [spec:kami.auth.session_check] (e9/d.java: code 20202).
CODE_SESSION_INVALID = 20202
#: Handed to a separate step by the app (social / extra verification); meaning unproven [spec:kami.auth.result_codes].
CODES_EXTRA_STEP = (20281, 20282)


class KamiError(TapoError):
    """Base class of every Kami lane error."""


class KamiConnectionError(KamiError, TapoConnectionError):
    """The Kami server could not be reached (refused, timeout, TLS)."""


class KamiServerError(KamiError):
    """HTTP status other than 200, or a result code that is not a sign-in problem."""

    def __init__(self, message: str, status: int = 0, code: int | None = None):
        super().__init__(message)
        self.status = status
        self.code = code


class KamiProtocolError(KamiError):
    """The reply is not the JSON shape the spec describes."""


class KamiAuthError(KamiError):
    """The server refused the sign-in (wrong password, unknown account, wrong region: all look the same)."""

    def __init__(self, code: int):
        super().__init__(f"sign-in refused (code {code})")
        self.code = code


class KamiExtraStepError(KamiAuthError):
    """The server asks for a step this client cannot do (social-only account or extra verification)."""


class KamiSessionExpired(KamiError):
    """The stored token is no longer valid."""


class KamiKeyMissingError(KamiError):
    """The vendor password key is not installed (see ``core/kami/app_key.py``)."""


@dataclass(frozen=True)
class KamiSession:
    """A signed-in Kami session. ``token`` and ``token_secret`` are secrets: they live in the vault only."""

    userid: str
    token: str
    token_secret: str
    email: str = field(default="", compare=False, repr=False)
    region: str = ""
    country: str = ""

    def __repr__(self) -> str:
        return f"KamiSession(region={self.region!r}, token='***')"

    def to_json(self) -> str:
        return json.dumps({"userid": self.userid, "token": self.token, "token_secret": self.token_secret,
                           "region": self.region, "country": self.country}, separators=(",", ":"))

    @classmethod
    def from_json(cls, text: str, email: str = "") -> "KamiSession":
        try:
            raw = json.loads(text)
            fields = {k: raw[k] for k in ("userid", "token", "token_secret")}
        except (ValueError, KeyError, TypeError):
            raise KamiProtocolError("stored Kami session is unreadable") from None
        if not all(isinstance(v, str) and v for v in fields.values()):
            raise KamiProtocolError("stored Kami session is unreadable")
        region, country = raw.get("region", ""), raw.get("country", "")
        return cls(**fields, email=email, region=region if isinstance(region, str) else "",
                   country=country if isinstance(country, str) else "")


def _code(reply: dict[str, Any]) -> int | None:
    code = reply.get("code")
    return code if isinstance(code, int) and not isinstance(code, bool) else None


def parse_login(reply: dict[str, Any], *, region: str, country: str, email: str) -> KamiSession:
    """Turn a ``/v4/users/login`` reply into a session, or raise the matching :class:`KamiError`."""
    code = _code(reply)
    if code in CODES_EXTRA_STEP:
        raise KamiExtraStepError(code)
    if code != CODE_OK:
        if code is None:
            raise KamiProtocolError("sign-in reply has no result code")
        raise KamiAuthError(code)
    data = reply.get("data")
    if not isinstance(data, dict):
        raise KamiProtocolError("sign-in reply has no data")
    fields = {k: data.get(k) for k in ("userid", "token", "token_secret")}
    if not all(isinstance(v, str) and v for v in fields.values()):
        raise KamiProtocolError("sign-in reply lacks userid, token or token_secret")
    return KamiSession(**fields, email=email, region=region, country=country)


def _flag(value: Any) -> bool:
    if isinstance(value, str):
        return value.strip().lower() in ("1", "true")
    return bool(value)


def parse_devices(reply: dict[str, Any], region: str) -> list[KamiDevice]:
    """Turn a ``/v5/devices/list`` reply into devices (owned and shared). Entries without a uid are skipped."""
    code = _code(reply)
    if code == CODE_SESSION_INVALID:
        raise KamiSessionExpired("the Kami session is no longer valid")
    if code != CODE_OK:
        raise KamiServerError(f"device list refused (code {code})", code=code)
    data = reply.get("data")
    if not isinstance(data, list):
        raise KamiProtocolError("device list reply has no data array")
    devices: list[KamiDevice] = []
    for item in data:
        if not isinstance(item, dict):
            continue
        uid = item.get("uid")
        if not isinstance(uid, str) or not uid.strip():
            continue
        name = item.get("name")
        model = item.get("model")
        devices.append(KamiDevice(
            uid=uid, name=name.strip() if isinstance(name, str) and name.strip() else uid,
            model=str(model) if model is not None else "", online=_flag(item.get("online")),
            shared=_flag(item.get("share")), region=region))
    return devices
