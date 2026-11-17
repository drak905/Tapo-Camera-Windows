"""Tier 1: secure-login crypto primitives against [spec:auth.golden_vector.v3]."""
from __future__ import annotations

import base64
import json
import re

import pytest
from hypothesis import given, settings
from hypothesis import strategies as st

from tapo_desktop.core.auth import crypto
from tapo_desktop.core.auth.crypto import SecureSessionCipher, SessionKeys
from tapo_desktop.core.errors import TapoProtocolError
from tests.unit._core_helpers import (
    golden, md5u, ref_decrypt, ref_encrypt, ref_encrypt_raw, ref_keys, ref_tag, sha256u,
)

pytestmark = pytest.mark.unit
G = golden()


# --------------------------------------------------------------------------- hashes
def test_md5_upper_golden():
    assert crypto.md5_upper(G["password"]) == G["md5_upper"]


def test_sha256_upper_golden():
    assert crypto.sha256_upper(G["password"]) == G["sha256_upper"]


def test_md5_ffs_default_password():
    ffs = G["ffs"]
    assert crypto.md5_upper(ffs["deviceId"]).lower() == ffs["password = md5(deviceId) lowercase hex"]


def test_hashes_are_utf8_and_uppercase():
    s = "pässwörd-密码"
    assert crypto.sha256_upper(s) == sha256u(s)
    assert crypto.md5_upper(s) == md5u(s)
    assert crypto.sha256_upper(s) == crypto.sha256_upper(s).upper()


# --------------------------------------------------------------------------- cnonce
def test_cnonce_format():
    for _ in range(50):
        c = crypto.generate_cnonce()
        assert isinstance(c, str)
        assert re.fullmatch(r"[0-9A-F]{16}", c), c


def test_cnonce_is_random():
    values = {crypto.generate_cnonce() for _ in range(200)}
    assert len(values) == 200


# --------------------------------------------------------------------------- device_confirm / digest
def test_expected_device_confirm_golden():
    assert crypto.expected_device_confirm(G["password"], G["cnonce"], G["nonce"]) == G["device_confirm"]


def test_device_confirm_structure():
    dc = crypto.expected_device_confirm(G["password"], G["cnonce"], G["nonce"])
    assert dc == G["first_hash"] + G["nonce"] + G["cnonce"]
    assert len(dc) == 64 + 16 + 16


def test_verify_device_confirm_true():
    assert crypto.verify_device_confirm(G["password"], G["cnonce"], G["nonce"], G["device_confirm"]) is True


@pytest.mark.parametrize("bad", [
    "",
    "0" * 96,
    G["device_confirm"][:-1],
    G["device_confirm"] + "0",
    G["first_hash"],
    "1" + G["device_confirm"][1:],
])
def test_verify_device_confirm_false(bad):
    assert crypto.verify_device_confirm(G["password"], G["cnonce"], G["nonce"], bad) is False


def test_verify_device_confirm_wrong_password():
    assert crypto.verify_device_confirm("other_password", G["cnonce"], G["nonce"], G["device_confirm"]) is False


def test_verify_device_confirm_uses_constant_time_compare(monkeypatch):
    import hmac
    import secrets

    calls = []
    real = hmac.compare_digest

    def spy(a, b):
        calls.append((a, b))
        return real(a, b)

    # cover `hmac.compare_digest`, `secrets.compare_digest` and `from hmac import compare_digest`
    monkeypatch.setattr(hmac, "compare_digest", spy)
    monkeypatch.setattr(secrets, "compare_digest", spy)
    if hasattr(crypto, "compare_digest"):
        monkeypatch.setattr(crypto, "compare_digest", spy)
    assert crypto.verify_device_confirm(G["password"], G["cnonce"], G["nonce"], G["device_confirm"])
    assert calls, "verify_device_confirm must use hmac.compare_digest"


def test_digest_passwd_golden():
    assert crypto.digest_passwd(G["password"], G["cnonce"], G["nonce"]) == G["digest_passwd"]


@given(pw=st.text(max_size=40), cn=st.from_regex(r"[0-9A-F]{16}", fullmatch=True),
       nn=st.from_regex(r"[0-9A-F]{16}", fullmatch=True))
@settings(max_examples=50, deadline=None)
def test_device_confirm_and_digest_property(pw, cn, nn):
    assert crypto.expected_device_confirm(pw, cn, nn) == sha256u(cn + sha256u(pw) + nn) + nn + cn
    assert crypto.digest_passwd(pw, cn, nn) == sha256u(sha256u(pw) + cn + nn) + cn + nn
    assert crypto.verify_device_confirm(pw, cn, nn, crypto.expected_device_confirm(pw, cn, nn))


# --------------------------------------------------------------------------- session keys
def test_derive_session_keys_golden():
    keys = crypto.derive_session_keys(G["password"], G["cnonce"], G["nonce"])
    assert isinstance(keys, SessionKeys)
    assert keys.lsk == G["lsk"]
    assert keys.ivb == G["ivb"]
    assert len(keys.lsk) == 16 and len(keys.ivb) == 16


@given(pw=st.text(max_size=30), cn=st.from_regex(r"[0-9A-F]{16}", fullmatch=True))
@settings(max_examples=30, deadline=None)
def test_derive_session_keys_property(pw, cn):
    keys = crypto.derive_session_keys(pw, cn, "FEDCBA9876543210")
    assert (keys.lsk, keys.ivb) == ref_keys(pw, cn, "FEDCBA9876543210")


# --------------------------------------------------------------------------- tag
def test_compute_tag_golden():
    tag = crypto.compute_tag(G["password"], G["cnonce"], G["outer_body"], G["seq"])
    assert tag == G["tapo_tag"]
    assert tag == G["headers"]["tapo_tag"]


def test_tag_prefix_matches_spec():
    # tag_prefix = SHA256U(SHA256U(pwd)+cnonce): cross-check the spec vector with hashlib
    assert sha256u(crypto.sha256_upper(G["password"]) + G["cnonce"]) == G["tag_prefix"]


def test_compute_tag_depends_on_seq_and_body():
    base = crypto.compute_tag(G["password"], G["cnonce"], G["outer_body"], G["seq"])
    assert crypto.compute_tag(G["password"], G["cnonce"], G["outer_body"], G["seq"] + 1) != base
    assert crypto.compute_tag(G["password"], G["cnonce"], G["outer_body"] + " ", G["seq"]) != base
    assert crypto.compute_tag(G["password"], G["cnonce"], "{}", 7) == ref_tag(G["password"], G["cnonce"], "{}", 7)


# --------------------------------------------------------------------------- cipher
def _cipher() -> SecureSessionCipher:
    return SecureSessionCipher(SessionKeys(lsk=G["lsk"], ivb=G["ivb"]))


def test_encrypt_golden():
    ct = _cipher().encrypt(G["inner_json"])
    assert ct == G["ciphertext"]
    assert "\n" not in ct and "\r" not in ct


def test_decrypt_golden():
    assert _cipher().decrypt(G["ciphertext"]) == G["inner_json"]


def test_encrypt_matches_reference_for_long_text():
    text = "x" * 1000 + "ü"
    assert _cipher().encrypt(text) == ref_encrypt(G["lsk"], G["ivb"], text.encode("utf-8"))


@given(st.text(max_size=500))
@settings(max_examples=100, deadline=None)
def test_encrypt_decrypt_roundtrip(text):
    c = _cipher()
    ct = c.encrypt(text)
    assert base64.b64decode(ct, validate=True)
    assert len(base64.b64decode(ct)) % 16 == 0
    assert c.decrypt(ct) == text
    assert ref_decrypt(G["lsk"], G["ivb"], ct).decode("utf-8") == text


@given(key=st.binary(min_size=16, max_size=16), iv=st.binary(min_size=16, max_size=16), text=st.text(max_size=100))
@settings(max_examples=30, deadline=None)
def test_roundtrip_any_keys(key, iv, text):
    c = SecureSessionCipher(SessionKeys(lsk=key, ivb=iv))
    assert c.decrypt(c.encrypt(text)) == text


@pytest.mark.parametrize("garbage", [
    "",                                   # empty
    "not base64 !!!",                    # invalid alphabet
    "QUJD",                               # valid base64, 3 bytes (not a block multiple)
    base64.b64encode(b"0123456789abcde").decode(),   # 15 bytes
    ref_encrypt_raw(G["lsk"], G["ivb"], b"A" * 15 + b"\x00"),          # pad byte 0
    ref_encrypt_raw(G["lsk"], G["ivb"], b"A" * 14 + b"\x05\x05"),      # inconsistent padding
    ref_encrypt_raw(G["lsk"], G["ivb"], b"A" * 15 + b"\x11"),          # pad > block size
    ref_encrypt(G["lsk"], G["ivb"], b"\xff\xfe\xfd"),                  # valid padding, invalid UTF-8
])
def test_decrypt_garbage_raises_protocol_error(garbage):
    with pytest.raises(TapoProtocolError):
        _cipher().decrypt(garbage)


@given(st.text(max_size=200))
@settings(max_examples=150, deadline=None)
def test_decrypt_never_raises_non_tapo_errors(s):
    try:
        out = _cipher().decrypt(s)
    except TapoProtocolError:
        return
    assert isinstance(out, str)


@given(st.binary(min_size=16, max_size=64).filter(lambda b: len(b) % 16 == 0))
@settings(max_examples=100, deadline=None)
def test_decrypt_random_blocks_only_protocol_error(raw):
    try:
        out = _cipher().decrypt(base64.b64encode(raw).decode())
    except TapoProtocolError:
        return
    assert isinstance(out, str)


# --------------------------------------------------------------------------- outer envelope
def test_wrap_secure_passthrough_golden():
    inner = json.loads(G["inner_json"])
    body = crypto.wrap_secure_passthrough(_cipher(), inner)
    assert body == G["outer_body"]


def test_wrap_secure_passthrough_compact_and_unescaped():
    inner = {"method": "multipleRequest", "params": {"requests": [{"method": "setX", "params": {"a": "ü/<>"}}]}}
    body = crypto.wrap_secure_passthrough(_cipher(), inner)
    outer = json.loads(body)
    assert list(outer) == ["method", "params"]
    assert outer["method"] == "securePassthrough"
    assert list(outer["params"]) == ["request"]
    assert ": " not in body and ", " not in body
    plain = ref_decrypt(G["lsk"], G["ivb"], outer["params"]["request"]).decode("utf-8")
    assert plain == json.dumps(inner, separators=(",", ":"), ensure_ascii=False)
    assert "\\u00fc" not in plain and "\\/" not in plain


def test_full_golden_pipeline():
    """Derive keys from the inputs and reproduce the whole golden request byte-for-byte."""
    keys = crypto.derive_session_keys(G["password"], G["cnonce"], G["nonce"])
    body = crypto.wrap_secure_passthrough(SecureSessionCipher(keys), json.loads(G["inner_json"]))
    assert body == G["outer_body"]
    assert crypto.compute_tag(G["password"], G["cnonce"], body, G["seq"]) == G["headers"]["tapo_tag"]
    assert str(G["seq"]) == G["headers"]["seq"]
