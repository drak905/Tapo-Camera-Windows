"""Credential vault: per-camera secrets protected with Windows DPAPI (CryptProtectData via ctypes)."""
from __future__ import annotations

import base64
import binascii
import ctypes
import json
import logging
import pathlib
import sys
import threading
from ctypes import wintypes

from tapo_desktop.core.auth._atomic import atomic_write_text, read_json_object
from tapo_desktop.core.errors import TapoError
from tapo_desktop.models import CredentialKind, Credentials

log = logging.getLogger(__name__)

_CRYPTPROTECT_UI_FORBIDDEN = 0x1
_FORMAT_VERSION = 1


class _DataBlob(ctypes.Structure):
    _fields_ = [("cbData", wintypes.DWORD), ("pbData", ctypes.POINTER(ctypes.c_char))]


_api: tuple | None = None
_api_lock = threading.Lock()


def _dpapi():
    """Lazily bind CryptProtectData / CryptUnprotectData / LocalFree (Windows only)."""
    global _api
    if _api is None:
        if sys.platform != "win32":
            raise TapoError("DPAPI is only available on Windows")
        with _api_lock:
            if _api is None:
                crypt32 = ctypes.WinDLL("crypt32", use_last_error=True)
                kernel32 = ctypes.WinDLL("kernel32", use_last_error=True)
                blob_p = ctypes.POINTER(_DataBlob)
                protect = crypt32.CryptProtectData
                protect.argtypes = [blob_p, wintypes.LPCWSTR, blob_p, ctypes.c_void_p, ctypes.c_void_p,
                                    wintypes.DWORD, blob_p]
                protect.restype = wintypes.BOOL
                unprotect = crypt32.CryptUnprotectData
                unprotect.argtypes = [blob_p, ctypes.POINTER(wintypes.LPWSTR), blob_p, ctypes.c_void_p,
                                      ctypes.c_void_p, wintypes.DWORD, blob_p]
                unprotect.restype = wintypes.BOOL
                local_free = kernel32.LocalFree
                local_free.argtypes = [ctypes.c_void_p]
                local_free.restype = ctypes.c_void_p
                _api = (protect, unprotect, local_free)
    return _api


def _in_blob(data: bytes) -> tuple[_DataBlob, ctypes.Array]:
    buf = ctypes.create_string_buffer(data, len(data))  # caller keeps ``buf`` alive
    return _DataBlob(len(data), ctypes.cast(buf, ctypes.POINTER(ctypes.c_char))), buf


def _call(fn_index: int, data: bytes, entropy: bytes | None, what: str) -> bytes:
    api = _dpapi()
    fn, local_free = api[fn_index], api[2]
    data_in, _keep1 = _in_blob(data)
    ent_ptr = None
    if entropy is not None:
        ent, _keep2 = _in_blob(entropy)
        ent_ptr = ctypes.byref(ent)
    out = _DataBlob()
    ok = fn(ctypes.byref(data_in), None, ent_ptr, None, None, _CRYPTPROTECT_UI_FORBIDDEN, ctypes.byref(out))
    if not ok:
        raise TapoError(f"DPAPI {what} failed (Windows error {ctypes.get_last_error()})")
    try:
        return ctypes.string_at(out.pbData, out.cbData)
    finally:
        local_free(ctypes.cast(out.pbData, ctypes.c_void_p))


def dpapi_protect(data: bytes, entropy: bytes | None = None) -> bytes:
    """Encrypt ``data`` for the current Windows user (CryptProtectData). Returns the opaque blob."""
    return _call(0, bytes(data), entropy, "protect")


def dpapi_unprotect(blob: bytes, entropy: bytes | None = None) -> bytes:
    """Decrypt a DPAPI blob (CryptUnprotectData). Raises TapoError on a corrupt blob or wrong entropy."""
    return _call(1, bytes(blob), entropy, "unprotect")


class CredentialVault:
    """JSON file vault. Secrets are stored only as base64 DPAPI blobs; plaintext never touches disk.

    Writes are atomic (temp file in the same directory + ``os.replace``). A corrupt/garbled entry
    makes :meth:`get` return ``None`` for that entry without affecting other entries.

    File layout: ``{"version": 1, "cameras": {camera_id: {kind: "<base64 DPAPI blob>"}}}``. Each
    blob decrypts to JSON ``{camera_id, kind, username, password}``; the embedded id/kind are checked
    on read so a blob copied to another entry is rejected. The file is re-read on every call, so
    several vault instances on the same path stay consistent.
    """

    def __init__(self, path: pathlib.Path):
        self._path = pathlib.Path(path)
        self._lock = threading.Lock()

    def __repr__(self) -> str:
        return f"CredentialVault(path={str(self._path)!r})"

    # ------------------------------------------------------------------ file
    def _load(self) -> dict[str, dict[str, str]]:
        cams = read_json_object(self._path).get("cameras")
        if not isinstance(cams, dict):
            return {}
        return {cid: {k: v for k, v in kinds.items() if isinstance(v, str)}
                for cid, kinds in cams.items() if isinstance(cid, str) and isinstance(kinds, dict)}

    def _save(self, cams: dict[str, dict[str, str]]) -> None:
        doc = {"version": _FORMAT_VERSION, "cameras": {cid: kinds for cid, kinds in cams.items() if kinds}}
        atomic_write_text(self._path, json.dumps(doc, indent=2, sort_keys=True))

    # ------------------------------------------------------------------ API
    def set(self, camera_id: str, kind: CredentialKind, creds: Credentials) -> None:
        """Store (or replace) the credentials of ``kind`` for ``camera_id``."""
        kind = CredentialKind(kind)
        payload = json.dumps({"camera_id": camera_id, "kind": kind.value,
                              "username": creds.username, "password": creds.password})
        blob = base64.b64encode(dpapi_protect(payload.encode("utf-8"))).decode("ascii")
        with self._lock:
            cams = self._load()
            cams.setdefault(camera_id, {})[kind.value] = blob
            self._save(cams)

    def get(self, camera_id: str, kind: CredentialKind) -> Credentials | None:
        """Return stored credentials, or ``None`` if absent or unreadable."""
        kind = CredentialKind(kind)
        with self._lock:
            blob = self._load().get(camera_id, {}).get(kind.value)
        if blob is None:
            return None
        try:
            data = json.loads(dpapi_unprotect(base64.b64decode(blob, validate=True)).decode("utf-8"))
            if data["camera_id"] != camera_id or data["kind"] != kind.value:
                raise ValueError("entry does not belong to this camera/kind")
            username, password = data["username"], data["password"]
            if not isinstance(username, str) or not isinstance(password, str):
                raise ValueError("bad field types")
        except (TapoError, binascii.Error, ValueError, KeyError, TypeError) as exc:
            log.warning("vault entry %s/%s is unreadable (%s); ignoring it", camera_id, kind.value,
                        type(exc).__name__)
            return None
        return Credentials(username, password)

    def delete(self, camera_id: str) -> None:
        """Remove all credential kinds for ``camera_id`` (no error if absent)."""
        with self._lock:
            cams = self._load()
            if cams.pop(camera_id, None) is not None:
                self._save(cams)

    def camera_ids(self) -> list[str]:
        """Camera ids that have at least one stored credential."""
        with self._lock:
            return [cid for cid, kinds in self._load().items() if kinds]
