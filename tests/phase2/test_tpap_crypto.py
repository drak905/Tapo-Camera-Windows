"""TPAP crypto primitives: SPAKE2+ (RFC 9383 vector), KDFs, credential shaping, CCM channel.

[spec:tpap.spake2p.algorithm], [spec:tpap.spake2p.points], [spec:tpap.context],
[spec:tpap.credentials.userpw], [spec:tpap.extra_crypt], [spec:tpap.session_cipher], [spec:tpap.ds.framing]
"""
from __future__ import annotations

import base64
import hashlib
import hmac
import struct

import pytest
from cryptography.hazmat.primitives.ciphers.aead import AESCCM

from tapo_desktop.core.control_client import tpap
from tapo_desktop.core.errors import TapoProtocolError

pytestmark = pytest.mark.unit

# ---- RFC 9383 Appendix C, SPAKE2+-P256-SHA256-HKDF-SHA256-HMAC-SHA256 ------------------------------
RFC_CONTEXT = b"SPAKE2+-P256-SHA256-HKDF-SHA256-HMAC-SHA256 Test Vectors"
RFC = {k: bytes.fromhex(v) for k, v in {
    "w0": "bb8e1bbcf3c48f62c08db243652ae55d3e5586053fca77102994f23ad95491b3",
    "w1": "7e945f34d78785b8a3ef44d0df5a1a97d6b3b460409a345ca7830387a74b1dba",
    "x": "d1232c8e8693d02368976c174e2088851b8365d0d79a9eee709c6a05a2fad539",
    "shareP": "04ef3bd051bf78a2234ec0df197f7828060fe9856503579bb1733009042c15c0c1de127727f418b5966afadfdd95a6e459"
              "1d171056b333dab97a79c7193e341727",
    "shareV": "04c0f65da0d11927bdf5d560c69e1d7d939a05b0e88291887d679fcadea75810fb5cc1ca7494db39e82ff2f50665255d76"
              "173e09986ab46742c798a9a68437b048",
    "Z": "04bbfce7dd7f277819c8da21544afb7964705569bdf12fb92aa388059408d50091a0c5f1d3127f56813b5337f9e4e67e2ca63311"
         "7a4fbd559946ab474356c41839",
    "V": "0458bf27c6bca011c9ce1930e8984a797a3419797b936629a5a937cf2f11c8b9514b82b993da8a46e664f23db7c01edc87faa530"
         "db01c2ee405230b18997f16b68",
    "K_main": "4c59e1ccf2cfb961aa31bd9434478a1089b56cd11542f53d3576fb6c2a438a29",
    "K_confirmP": "871ae3f7b78445e34438fb284504240239031c39d80ac23eb5ab9be5ad6db58a",
    "K_confirmV": "ccd53c7c1fa37b64a462b40db8be101cedcf838950162902054e644b400f1680",
    "confirmP": "926cc713504b9b4d76c9162ded04b5493e89109f6d89462cd33adc46fda27527",
    "confirmV": "9747bcc4f8fe9f63defee53ac9b07876d907d55047e6ff2def2e7529089d3e68",
    "K_shared": "0c5f8ccd1413423a54f6c1fb26ff01534a87f893779c6e68666d772bfd91f3e7",
}.items()}


def _i(b: bytes) -> int:
    return int.from_bytes(b, "big")


def test_rfc9383_vector_prover_side():
    prover = tpap.Spake2pProver(_i(RFC["w0"]), _i(RFC["w1"]), RFC_CONTEXT, id_prover=b"client",
                                id_verifier=b"server", x=_i(RFC["x"]))
    assert prover.share == RFC["shareP"]
    keys = prover.finish(RFC["shareV"])
    assert keys.share_p == RFC["shareP"] and keys.share_v == RFC["shareV"]
    assert keys.transcript.startswith(struct.pack("<Q", len(RFC_CONTEXT)) + RFC_CONTEXT)
    assert RFC["Z"] in keys.transcript and RFC["V"] in keys.transcript
    assert keys.transcript.endswith(struct.pack("<Q", 32) + RFC["w0"])
    assert keys.k_main == RFC["K_main"] == hashlib.sha256(keys.transcript).digest()
    assert keys.k_confirm_p == RFC["K_confirmP"]
    assert keys.k_confirm_v == RFC["K_confirmV"]
    assert keys.confirm_p == RFC["confirmP"]      # what the client sends as user_confirm
    assert keys.confirm_v == RFC["confirmV"]      # what dev_confirm must equal
    assert keys.shared_key == RFC["K_shared"]


def test_keys_repr_hides_secrets():
    prover = tpap.Spake2pProver(_i(RFC["w0"]), _i(RFC["w1"]), RFC_CONTEXT, x=_i(RFC["x"]))
    keys = prover.finish(RFC["shareV"])
    text = repr(keys) + repr(prover)
    for secret in (RFC["w0"].hex(), keys.shared_key.hex(), keys.k_main.hex()):
        assert secret not in text


def test_points_m_n_and_encoding_round_trip():
    m = tpap.decode_point(bytes.fromhex(tpap.M_POINT_HEX))
    n = tpap.decode_point(bytes.fromhex(tpap.N_POINT_HEX))
    assert m != n
    for p in (m, n):
        enc = tpap.encode_point(p)
        assert len(enc) == 65 and enc[0] == 4
        assert tpap.decode_point(enc) == p


@pytest.mark.parametrize("bad", [b"", b"\x04" + b"\x00" * 64, b"\x04" + b"\x01" * 64, b"\x05" + b"\x00" * 32,
                                 b"\x02" + b"\xff" * 32, RFC["shareV"][:-1]],
                         ids=["empty", "zero", "off_curve", "bad_prefix", "x_ge_p", "truncated"])
def test_invalid_points_rejected(bad):
    with pytest.raises(ValueError):
        tpap.decode_point(bad)


def test_peer_share_off_curve_rejected():
    prover = tpap.Spake2pProver(5, 7, b"ctx", x=11)
    with pytest.raises(ValueError):
        prover.finish(b"\x04" + b"\x01" * 64)


def test_random_x_shares_differ():
    a = tpap.Spake2pProver(5, 7, b"ctx").share
    b = tpap.Spake2pProver(5, 7, b"ctx").share
    assert a != b and len(a) == 65


# ---- KDFs --------------------------------------------------------------------------------------------
def test_derive_w0_w1_is_pbkdf2_sha256_80_bytes_mod_n():
    n = 0xFFFFFFFF00000000FFFFFFFFFFFFFFFFBCE6FAADA7179E84F3B9CAC2FC632551
    salt = bytes(range(16))
    dk = hashlib.pbkdf2_hmac("sha256", b"0123abcd", salt, 1000, 80)
    assert tpap.derive_w0_w1("0123abcd", salt, 1000) == (_i(dk[:40]) % n, _i(dk[40:]) % n)


def test_tpap_context_is_sha256_of_tag_and_randoms():
    ur, dr = b"\x01" * 32, b"\x02" * 32
    assert tpap.CONTEXT_TAG == b"PAKE V1"
    assert tpap.tpap_context(ur, dr) == hashlib.sha256(b"PAKE V1" + ur + dr).digest()


def _hkdf(ikm: bytes, salt: bytes, info: bytes, n: int) -> bytes:  # RFC 5869, independent
    prk = hmac.new(salt, ikm, hashlib.sha256).digest()
    out, t, i = b"", b"", 1
    while len(out) < n:
        t = hmac.new(prk, t + info + bytes([i]), hashlib.sha256).digest()
        out += t
        i += 1
    return out[:n]


SHARED = bytes(range(32))
GOLD_KEY = _hkdf(SHARED, b"tp-kdf-salt-aes128-key", b"tp-kdf-info-aes128-key", 32)[:16]
GOLD_NONCE = _hkdf(SHARED, b"tp-kdf-salt-aes128-iv", b"tp-kdf-info-aes128-iv", 32)[:12]


def test_cipher_golden_vector_and_framing():
    c = tpap.TpapCipher(SHARED)
    plain = b'{"method":"multipleRequest","params":{"requests":[]}}'
    seq = 0x01020304
    nonce = GOLD_NONCE[:8] + struct.pack(">I", seq)
    expected_ct = AESCCM(GOLD_KEY, tag_length=16).encrypt(nonce, plain, None)
    assert c.encrypt(plain, seq) == expected_ct
    body = c.seal(plain, seq)
    assert body == struct.pack(">I", seq) + expected_ct
    assert len(body) == 4 + len(plain) + 16
    assert c.open(body) == (seq, plain)
    assert c.decrypt(expected_ct, seq) == plain


def test_cipher_nonce_depends_on_seq_and_tamper_detected():
    c = tpap.TpapCipher(SHARED)
    a, b = c.seal(b"same", 10), c.seal(b"same", 11)
    assert a[4:] != b[4:]
    with pytest.raises(TapoProtocolError):
        c.decrypt(a[4:], 11)                        # wrong seq -> wrong nonce
    bad = bytearray(a)
    bad[-1] ^= 1
    with pytest.raises(TapoProtocolError):
        c.open(bytes(bad))
    with pytest.raises(TapoProtocolError):
        c.open(b"\x00\x00\x00\x01short")


def test_cipher_rejects_unknown_encryption_and_hides_key():
    with pytest.raises(TapoProtocolError):
        tpap.TpapCipher(SHARED, "chacha20_poly1305")
    assert GOLD_KEY.hex() not in repr(tpap.TpapCipher(SHARED))


# ---- credentials -------------------------------------------------------------------------------------
def test_hash_helpers():
    assert tpap.md5_hex("cloudpass") == hashlib.md5(b"cloudpass").hexdigest()
    assert tpap.sha256_hex_upper("cloudpass") == hashlib.sha256(b"cloudpass").hexdigest().upper()
    assert tpap.credential_for("md5", "pw") == tpap.md5_hex("pw")
    assert tpap.credential_for("sha256", "pw") == tpap.sha256_hex_upper("pw")
    with pytest.raises(ValueError):
        tpap.credential_for("plain", "pw")


def test_camera_username_hashing():
    assert tpap.camera_username() == "21232f297a57a5a743894a0e4a801fc3"  # md5_hex("admin")
    assert tpap.camera_username(1) == hashlib.sha256(b"admin").hexdigest().upper()


def test_sha256_crypt_drepper_vectors():
    assert tpap.sha256_crypt("Hello world!", "$5$saltstring") == \
        "$5$saltstring$5B8vYYiY.CVt1RlTTf8KbXBH3hsxY/GNooZaBBGWEc5"
    assert tpap.sha256_crypt("Hello world!", "$5$rounds=10000$saltstringsaltstring") == \
        "$5$rounds=10000$saltstringsaltst$3xv.VbSHBb41AL9AvLeujZkZRBAwqFMz2.opqey6IcA"


def test_md5_crypt_vector_and_prefix_check():
    assert tpap.md5_crypt("password", "$1$3azHgidD$") == "$1$3azHgidD$SrJPt7B.9rekpmwJwtON31"
    assert tpap.md5_crypt("password", "$5$nope$") is None


def test_apply_extra_crypt_variants():
    cred = tpap.md5_hex("pw")
    assert tpap.apply_extra_crypt(cred, None) == cred
    assert tpap.apply_extra_crypt(cred, {}) == cred
    assert tpap.apply_extra_crypt(cred, {"type": "unknown", "params": {}}) == cred
    shadow5 = {"type": "password_shadow", "params": {"passwd_id": 5, "passwd_prefix": "$5$abcdefgh$"}}
    assert tpap.apply_extra_crypt(cred, shadow5) == tpap.sha256_crypt(cred, "$5$abcdefgh$")
    shadow1 = {"type": "PASSWORD_SHADOW", "params": {"passwd_id": 1, "passwd_prefix": "$1$abcdefgh$"}}
    assert tpap.apply_extra_crypt(cred, shadow1) == tpap.md5_crypt(cred, "$1$abcdefgh$")
    bad1 = {"type": "password_shadow", "params": {"passwd_id": 1, "passwd_prefix": "nope"}}
    assert tpap.apply_extra_crypt(cred, bad1) == cred          # app falls back to the raw passcode
    shadow2 = {"type": "password_shadow", "params": {"passwd_id": 2}}
    assert tpap.apply_extra_crypt(cred, shadow2) == hashlib.sha1(cred.encode()).hexdigest()
    shadow3 = {"type": "password_shadow", "params": {"passwd_id": 3}}
    assert tpap.apply_extra_crypt(cred, shadow3, mac="AABBCCDDEEFF") == cred  # needs a username: unchanged
    salt = base64.b64encode(b"NaCl").decode()
    sha0 = {"type": "password_sha_with_salt", "params": {"sha_name": 0, "sha_salt": salt}}
    assert tpap.apply_extra_crypt(cred, sha0) == hashlib.sha256(("admin" + "NaCl" + cred).encode()).hexdigest()
    sha_default = {"type": "password_sha_with_salt", "params": {"sha_salt": salt}}
    assert tpap.apply_extra_crypt(cred, sha_default) == hashlib.sha256(("user" + "NaCl" + cred).encode()).hexdigest()


def test_apply_extra_crypt_authkey():
    # authKey(cred, tmpkey, dict): out[i] = dict[(c ^ k) % len(dict)], 187 pads the shorter string
    ec = {"type": "password_authkey", "params": {"authkey_tmpkey": "ab", "authkey_dictionary": "0123456789"}}
    cred = "xyz"
    exp = "".join("0123456789"[(ord(c) ^ k) % 10] for c, k in zip(cred, [ord("a"), ord("b"), 187]))
    assert tpap.apply_extra_crypt(cred, ec) == exp
    ec_no_dict = {"type": "password_authkey", "params": {"authkey_tmpkey": "ab"}}
    assert tpap.apply_extra_crypt(cred, ec_no_dict) == cred


def test_tpap_requested_hints():
    assert tpap.tpap_requested({"pake": [2], "port": 443})
    assert tpap.tpap_requested(None, ["4"])
    assert tpap.tpap_requested(None, "4")
    assert not tpap.tpap_requested(None, ["3"])
    assert not tpap.tpap_requested({"pake": []}, None)
    assert not tpap.tpap_requested(None, None)
