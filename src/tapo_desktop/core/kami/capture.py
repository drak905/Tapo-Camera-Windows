"""One-shot, redacted capture of the Kami sign-in reply (owner tool; see ``tools/diag_kami_capture.py``).

Sends exactly one sign-in request and returns the HTTP status, reply headers and JSON body with every VALUE hidden
and every field NAME, type, length and result code kept. Nothing from the request (URL, host, e-mail, password,
password hash) is put into the result except the query parameter names. The output is for debugging the parser only.
"""
from __future__ import annotations

import json
import re
from collections.abc import Iterable
from pathlib import Path
from typing import Any

from tapo_desktop.core.kami.client import KamiClient
from tapo_desktop.core.kami.signing import password_hash

#: Keys whose short text value is kept (result messages). A value with an "@" or a known secret is hidden anyway.
_TEXT_KEYS = frozenset({"msg", "message", "desc", "description", "error", "errmsg", "errmessage", "status", "result",
                        "type", "state", "reason"})
#: Keys whose number is kept (result codes and flags).
_NUMBER_KEYS = frozenset({"code", "status", "result", "ret", "errcode", "error_code", "errorcode", "flag", "state"})
#: Reply headers whose value is kept. Every other header value is replaced by its length.
_HEADER_KEEP = frozenset({"content-type", "content-length", "content-encoding", "transfer-encoding", "connection",
                          "cache-control", "vary", "server", "date", "keep-alive", "access-control-allow-origin"})
_LONG_TOKEN = re.compile(r"[A-Za-z0-9+/=_.\-]{16,}")


def _scrub(text: str, secrets: Iterable[str]) -> str:
    for secret in secrets:
        if secret:
            text = text.replace(secret, "<secret>")
    return text


def _kind(text: str) -> str:
    if text.isdigit():
        return "digits"
    if re.fullmatch(r"[0-9a-fA-F]+", text):
        return "hex"
    if text.isalnum():
        return "alnum"
    return "text"


def redact_json(value: Any, secrets: Iterable[str] = (), key: str = "") -> Any:
    """Copy of ``value`` with names and structure kept and values hidden (see module docstring)."""
    secrets = tuple(secrets)
    if isinstance(value, dict):
        return {_scrub(str(k), secrets): redact_json(v, secrets, str(k).lower()) for k, v in value.items()}
    if isinstance(value, list):
        return [redact_json(v, secrets) for v in value]
    if isinstance(value, bool) or value is None:
        return value
    if isinstance(value, (int, float)):
        return value if key in _NUMBER_KEYS else f"<number {len(str(value))} chars>"
    if isinstance(value, str):
        clean = _scrub(value, secrets)
        if key in _TEXT_KEYS and "@" not in clean and "<secret>" not in clean and len(clean) <= 80:
            return clean
        return f"<str {len(value)} {_kind(value)}>"
    return f"<{type(value).__name__}>"


def _redact_headers(headers: list[tuple[str, str]]) -> dict[str, str]:
    out: dict[str, str] = {}
    for name, value in headers:
        shown = value if name.lower() in _HEADER_KEEP else f"<redacted {len(value)} chars>"
        out[name] = f"{out[name]} | {shown}" if name in out else shown
    return out


def capture_login(client: KamiClient, email: str, password: str) -> dict[str, Any]:
    """Sign in once and describe the reply. Raises ``KamiKeyMissingError`` (no request) or ``KamiConnectionError``."""
    pairs, status, headers, body = client.login_raw(email, password)
    app_key = getattr(client, "_app_key", None) or ""
    secrets = [email, password, password_hash(password, app_key), email.split("@")[0]]
    has_bom = body.startswith(b"\xef\xbb\xbf")
    doc: dict[str, Any] = {
        "request": {"path": "/v4/users/login", "param_names": [k for k, _ in pairs]},
        "status": status, "headers": _redact_headers(headers), "body_length": len(body),
        "first_bytes_hex": body[:8].hex(), "has_bom": has_bom,
    }
    try:
        parsed = json.loads(body[3:].decode("utf-8") if has_bom else body.decode("utf-8"))
    except ValueError:
        preview = _LONG_TOKEN.sub("<token>", _scrub(body[:200].decode("utf-8", "replace"), secrets))
        doc.update(body_kind="not-json", body_preview=preview)
    else:
        doc.update(body_kind="json", body=redact_json(parsed, secrets))
    return doc


def write_capture(doc: dict[str, Any], path: str | Path) -> Path:
    """Write ``doc`` as UTF-8 JSON, LF, no BOM; parent folders are created."""
    target = Path(path)
    target.parent.mkdir(parents=True, exist_ok=True)
    target.write_bytes((json.dumps(doc, indent=2, ensure_ascii=True) + "\n").encode("utf-8"))
    return target
