"""Credential vault: per-camera secrets protected with Windows DPAPI (CryptProtectData via ctypes)."""
from __future__ import annotations

import pathlib

from tapo_desktop.models import CredentialKind, Credentials


def dpapi_protect(data: bytes, entropy: bytes | None = None) -> bytes:
    """Encrypt ``data`` for the current Windows user (CryptProtectData). Returns the opaque blob."""
    raise NotImplementedError


def dpapi_unprotect(blob: bytes, entropy: bytes | None = None) -> bytes:
    """Decrypt a DPAPI blob (CryptUnprotectData). Raises TapoError on a corrupt blob or wrong entropy."""
    raise NotImplementedError


class CredentialVault:
    """JSON file vault. Secrets are stored only as base64 DPAPI blobs; plaintext never touches disk.

    Writes are atomic (temp file in the same directory + ``os.replace``). A corrupt/garbled entry
    makes :meth:`get` return ``None`` for that entry without affecting other entries.
    """

    def __init__(self, path: pathlib.Path):
        raise NotImplementedError

    def set(self, camera_id: str, kind: CredentialKind, creds: Credentials) -> None:
        """Store (or replace) the credentials of ``kind`` for ``camera_id``."""
        raise NotImplementedError

    def get(self, camera_id: str, kind: CredentialKind) -> Credentials | None:
        """Return stored credentials, or ``None`` if absent or unreadable."""
        raise NotImplementedError

    def delete(self, camera_id: str) -> None:
        """Remove all credential kinds for ``camera_id`` (no error if absent)."""
        raise NotImplementedError

    def camera_ids(self) -> list[str]:
        """Camera ids that have at least one stored credential."""
        raise NotImplementedError
