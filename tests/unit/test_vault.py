"""Tier 1: DPAPI helpers and the credential vault (no plaintext on disk, atomic writes)."""
from __future__ import annotations

import base64
import json
import os
import sys

import pytest
from hypothesis import given, settings
from hypothesis import strategies as st

from tapo_desktop.core.auth.vault import CredentialVault, dpapi_protect, dpapi_unprotect
from tapo_desktop.core.errors import TapoError
from tapo_desktop.models import CredentialKind, Credentials

pytestmark = [pytest.mark.unit, pytest.mark.skipif(sys.platform != "win32", reason="DPAPI is Windows-only")]

SECRET = "Sup3r-Secret-Pässwörd!"
CLOUD = Credentials("admin", SECRET)
CAMACC = Credentials("camuser", "CamAcc0unt-PW#42")


# --------------------------------------------------------------------------- DPAPI
def test_dpapi_roundtrip():
    blob = dpapi_protect(b"hello world")
    assert isinstance(blob, bytes)
    assert b"hello world" not in blob
    assert dpapi_unprotect(blob) == b"hello world"


def test_dpapi_roundtrip_with_entropy():
    blob = dpapi_protect(b"payload", entropy=b"salt-1")
    assert dpapi_unprotect(blob, entropy=b"salt-1") == b"payload"


def test_dpapi_wrong_entropy_raises():
    blob = dpapi_protect(b"payload", entropy=b"salt-1")
    with pytest.raises(TapoError):
        dpapi_unprotect(blob, entropy=b"salt-2")


@pytest.mark.parametrize("blob", [b"", b"garbage", b"\x01\x00\x00\x00" + b"\x00" * 40])
def test_dpapi_unprotect_corrupt_raises(blob):
    with pytest.raises(TapoError):
        dpapi_unprotect(blob)


def test_dpapi_unprotect_bitflip_raises():
    blob = bytearray(dpapi_protect(b"payload"))
    blob[-5] ^= 0xFF
    with pytest.raises(TapoError):
        dpapi_unprotect(bytes(blob))


def test_dpapi_is_nondeterministic():
    assert dpapi_protect(b"x") != dpapi_protect(b"x")


@given(st.binary(max_size=256))
@settings(max_examples=25, deadline=None)
def test_dpapi_roundtrip_property(data):
    assert dpapi_unprotect(dpapi_protect(data)) == data


# --------------------------------------------------------------------------- vault basics
def test_vault_set_get(tmp_path):
    v = CredentialVault(tmp_path / "vault.json")
    v.set("cam1", CredentialKind.CLOUD, CLOUD)
    v.set("cam1", CredentialKind.CAMERA_ACCOUNT, CAMACC)
    assert v.get("cam1", CredentialKind.CLOUD) == CLOUD
    assert v.get("cam1", CredentialKind.CAMERA_ACCOUNT) == CAMACC


def test_vault_missing_returns_none(tmp_path):
    v = CredentialVault(tmp_path / "vault.json")
    assert v.get("nope", CredentialKind.CLOUD) is None
    assert v.camera_ids() == []
    v.set("cam1", CredentialKind.CLOUD, CLOUD)
    assert v.get("cam1", CredentialKind.CAMERA_ACCOUNT) is None


def test_vault_persists_across_instances(tmp_path):
    path = tmp_path / "vault.json"
    CredentialVault(path).set("cam1", CredentialKind.CLOUD, CLOUD)
    v2 = CredentialVault(path)
    assert v2.get("cam1", CredentialKind.CLOUD) == CLOUD
    assert v2.camera_ids() == ["cam1"]


def test_vault_overwrite(tmp_path):
    v = CredentialVault(tmp_path / "vault.json")
    v.set("cam1", CredentialKind.CLOUD, CLOUD)
    v.set("cam1", CredentialKind.CLOUD, Credentials("admin", "new-password"))
    assert v.get("cam1", CredentialKind.CLOUD) == Credentials("admin", "new-password")
    assert CredentialVault(tmp_path / "vault.json").get("cam1", CredentialKind.CLOUD).password == "new-password"


def test_vault_camera_ids(tmp_path):
    v = CredentialVault(tmp_path / "vault.json")
    v.set("b", CredentialKind.CLOUD, CLOUD)
    v.set("a", CredentialKind.CAMERA_ACCOUNT, CAMACC)
    v.set("a", CredentialKind.CLOUD, CLOUD)
    assert sorted(v.camera_ids()) == ["a", "b"]


def test_vault_delete_removes_all_kinds(tmp_path):
    path = tmp_path / "vault.json"
    v = CredentialVault(path)
    v.set("cam1", CredentialKind.CLOUD, CLOUD)
    v.set("cam1", CredentialKind.CAMERA_ACCOUNT, CAMACC)
    v.set("cam2", CredentialKind.CLOUD, CLOUD)
    v.delete("cam1")
    assert v.get("cam1", CredentialKind.CLOUD) is None
    assert v.get("cam1", CredentialKind.CAMERA_ACCOUNT) is None
    assert v.camera_ids() == ["cam2"]
    v2 = CredentialVault(path)
    assert v2.get("cam1", CredentialKind.CLOUD) is None
    assert v2.get("cam2", CredentialKind.CLOUD) == CLOUD
    assert "cam1" not in path.read_text(encoding="utf-8")


def test_vault_delete_unknown_is_noop(tmp_path):
    v = CredentialVault(tmp_path / "vault.json")
    v.delete("ghost")
    assert v.camera_ids() == []


def test_vault_creates_parent_dirs(tmp_path):
    path = tmp_path / "deep" / "er" / "vault.json"
    CredentialVault(path).set("cam1", CredentialKind.CLOUD, CLOUD)
    assert path.exists()


def test_vault_unicode_password(tmp_path):
    creds = Credentials("admin", "密码-пароль-🔑")
    v = CredentialVault(tmp_path / "vault.json")
    v.set("cam1", CredentialKind.CLOUD, creds)
    assert CredentialVault(tmp_path / "vault.json").get("cam1", CredentialKind.CLOUD) == creds


# --------------------------------------------------------------------------- no plaintext
def _forbidden_encodings(secret: str) -> list[bytes]:
    raw = secret.encode("utf-8")
    return [
        raw,
        secret.encode("utf-16-le"),
        base64.b64encode(raw),
        base64.urlsafe_b64encode(raw),
        raw.hex().encode(), raw.hex().upper().encode(),
        json.dumps(secret).encode("ascii"),            # \u-escaped form
    ]


def test_vault_file_never_contains_plaintext(tmp_path):
    path = tmp_path / "vault.json"
    v = CredentialVault(path)
    v.set("cam1", CredentialKind.CLOUD, CLOUD)
    v.set("cam1", CredentialKind.CAMERA_ACCOUNT, CAMACC)
    data = path.read_bytes()
    for secret in (CLOUD.password, CAMACC.password):
        for enc in _forbidden_encodings(secret):
            assert enc not in data, f"secret leaked in encoding {enc!r}"
    json.loads(data.decode("utf-8"))  # still a JSON file


def test_vault_dir_has_no_plaintext_leftovers(tmp_path):
    path = tmp_path / "vault.json"
    v = CredentialVault(path)
    for i in range(5):
        v.set(f"cam{i}", CredentialKind.CLOUD, CLOUD)
    files = list(tmp_path.iterdir())
    assert path in files
    leftovers = [f for f in files if f != path and ("tmp" in f.name.lower() or "temp" in f.name.lower())]
    assert leftovers == [], f"temp files left behind: {leftovers}"
    for f in files:
        assert CLOUD.password.encode() not in f.read_bytes()


def test_vault_secret_blobs_are_dpapi(tmp_path):
    """Every long base64 string in the file must decrypt with DPAPI (secrets only as DPAPI blobs)."""
    path = tmp_path / "vault.json"
    CredentialVault(path).set("cam1", CredentialKind.CLOUD, CLOUD)
    blobs = []

    def walk(o):
        if isinstance(o, dict):
            for val in o.values():
                walk(val)
        elif isinstance(o, list):
            for val in o:
                walk(val)
        elif isinstance(o, str) and len(o) >= 40:
            try:
                blobs.append(base64.b64decode(o, validate=True))
            except ValueError:
                pass

    walk(json.loads(path.read_text(encoding="utf-8")))
    assert blobs, "expected at least one base64 DPAPI blob in the vault file"
    decrypted = []
    for b in blobs:
        try:
            decrypted.append(dpapi_unprotect(b))
        except TapoError:
            pass
    assert decrypted, "no blob in the vault file is a DPAPI blob"


# --------------------------------------------------------------------------- atomic writes
def test_vault_write_uses_os_replace(tmp_path, monkeypatch):
    path = tmp_path / "vault.json"
    v = CredentialVault(path)
    calls = []
    real = os.replace

    def spy(src, dst, *a, **kw):
        calls.append((os.fspath(src), os.fspath(dst)))
        return real(src, dst, *a, **kw)

    monkeypatch.setattr(os, "replace", spy)
    v.set("cam1", CredentialKind.CLOUD, CLOUD)
    assert calls, "vault writes must go through os.replace"
    src, dst = calls[-1]
    assert os.path.samefile(dst, path)
    assert os.path.dirname(os.path.abspath(src)) == os.path.dirname(os.path.abspath(path))
    assert not os.path.exists(src)


def test_vault_failed_write_keeps_previous_file(tmp_path, monkeypatch):
    path = tmp_path / "vault.json"
    v = CredentialVault(path)
    v.set("cam1", CredentialKind.CLOUD, CLOUD)
    before = path.read_bytes()

    def boom(*_a, **_kw):
        raise OSError("disk full (simulated)")

    monkeypatch.setattr(os, "replace", boom)
    with pytest.raises(Exception):
        v.set("cam2", CredentialKind.CLOUD, CAMACC)
    monkeypatch.undo()
    assert path.read_bytes() == before
    v2 = CredentialVault(path)
    assert v2.get("cam1", CredentialKind.CLOUD) == CLOUD
    assert v2.get("cam2", CredentialKind.CLOUD) is None


# --------------------------------------------------------------------------- corruption tolerance
def _corrupt_subtree(o, target: str, mode: str, inside: bool = False):
    """Replace every long base64 string that belongs to camera ``target``."""
    if isinstance(o, dict):
        mine = inside or target in o.values()
        out = {}
        for k, val in o.items():
            out[k] = _corrupt_subtree(val, target, mode, mine or k == target)
        return out
    if isinstance(o, list):
        return [_corrupt_subtree(val, target, mode, inside) for val in o]
    if isinstance(o, str) and inside and len(o) >= 40:
        if mode == "not_base64":
            return "!!! this is not base64 !!!"
        raw = bytearray(base64.b64decode(o))
        raw[len(raw) // 2] ^= 0xFF
        return base64.b64encode(bytes(raw)).decode()
    return o


@pytest.mark.parametrize("mode", ["not_base64", "bitflip"])
def test_vault_corrupt_entry_returns_none(tmp_path, mode):
    path = tmp_path / "vault.json"
    v = CredentialVault(path)
    v.set("camA", CredentialKind.CLOUD, CLOUD)
    v.set("camB", CredentialKind.CLOUD, CLOUD)
    v.set("camB", CredentialKind.CAMERA_ACCOUNT, CAMACC)
    doc = json.loads(path.read_text(encoding="utf-8"))
    corrupted = _corrupt_subtree(doc, "camA", mode)
    assert corrupted != doc, "test could not locate camA's blob in the vault file"
    path.write_text(json.dumps(corrupted), encoding="utf-8")

    v2 = CredentialVault(path)
    assert v2.get("camA", CredentialKind.CLOUD) is None
    assert v2.get("camB", CredentialKind.CLOUD) == CLOUD
    assert v2.get("camB", CredentialKind.CAMERA_ACCOUNT) == CAMACC


def test_vault_corrupt_file_does_not_crash(tmp_path):
    path = tmp_path / "vault.json"
    path.write_bytes(b"\x00\xffnot json at all{{{")
    v = CredentialVault(path)
    assert v.get("cam1", CredentialKind.CLOUD) is None
    v.set("cam1", CredentialKind.CLOUD, CLOUD)
    assert CredentialVault(path).get("cam1", CredentialKind.CLOUD) == CLOUD
