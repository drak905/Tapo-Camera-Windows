"""TPAP local login (SPAKE2+ over P-256) and the AES-128-CCM ``/stok=<stok>/ds`` channel.

Newer camera firmware (TDP ``encrypt_type: ["4"]``, ``tpap: {"pake": [2], ...}``; e.g. Tapo C200 hw 5.0
fw 1.5.x) answers the v3 secure login with -40211 and only accepts this flow [spec:tpap.selection]:

1. ``POST /`` ``login`` / ``pake_register`` (user = md5_hex("admin"), 32-byte ``user_random``)
   -> ``dev_salt``, ``dev_random``, ``dev_share``, ``iterations``[, ``extra_crypt``].
2. passcode = md5_hex(cloud password) or SHA256_HEX_UPPER(cloud password), shaped by ``extra_crypt``
   (C200: ``password_shadow`` id 5 = SHA-256-crypt with the camera's ``$5$`` prefix)
   [spec:tpap.credentials.userpw], [spec:tpap.extra_crypt].
3. SPAKE2+ (RFC 9383, P-256, SHA-256, HKDF, HMAC) with w0/w1 = PBKDF2-SHA256(passcode, dev_salt,
   iterations, 80 bytes) split 40/40 mod n and context = SHA256("PAKE V1" + user_random + dev_random)
   [spec:tpap.spake2p.algorithm], [spec:tpap.context]; ``pake_share`` sends shareP + confirmP; the
   camera's ``dev_confirm`` must equal confirmV -> ``stok``, ``start_seq``.
4. ``POST /stok=<stok>/ds`` body ``uint32_be(seq) || AES-128-CCM(ct) || tag16``; key / nonce base are
   HKDF-SHA256(shared_key) with the ``tp-kdf-*-aes128-*`` salts / infos, nonce = base[0:8] +
   uint32_be(seq), first seq = start_seq [spec:tpap.session_cipher], [spec:tpap.ds.framing].

Sources (protocol facts only; this is an independent implementation):
* Tapo Android app (decompiled): ``com/tplink/tls/codec/spake2p/bo/*`` (register / share beans,
  ``Spake2pExtraCryptBean`` shadows), ``tm1/a.java`` (SPAKE2+ transcript / key schedule),
  ``on1/a.java`` (auth context, dev_confirm check), ``com/tplink/security/cipher/SecEncryptor.java``
  and ``SecSessionCipher.java`` (CCM key / nonce), ``libtapocameranetwork/transport/local/j2.java``
  (camera passcode candidates).
* freeKC/tapo-v4-protocol (MIT), PROTOCOL.md sections 4-5: https://github.com/freeKC/tapo-v4-protocol
* RFC 9383 (SPAKE2+) incl. the P-256 M / N points and Appendix C test vectors.

The P-256 arithmetic is plain Python (not constant time). It only handles this client's ephemeral
scalar and values derived from the user's own password, on the user's own LAN.
Secrets (password, passcodes, w0/w1, keys, stok) never appear in logs, messages or ``repr``.
"""
from __future__ import annotations

import base64
import binascii
import hashlib
import hmac
import logging
import os
import secrets
import struct
import time
from dataclasses import dataclass
from typing import Any, Callable

from cryptography.exceptions import InvalidTag
from cryptography.hazmat.primitives import hashes
from cryptography.hazmat.primitives.ciphers.aead import AESCCM
from cryptography.hazmat.primitives.kdf.hkdf import HKDF

from tapo_desktop.core.errors import TapoAuthError, TapoError, TapoLockedError, TapoProtocolError

log = logging.getLogger(__name__)

CONTEXT_TAG = b"PAKE V1"           # Spake2pBean.SPAKE2P_CONTEXT_TAG
CAMERA_USER = "admin"
MAX_ATTEMPTS = 2                   # pake_share attempts per login (password guesses are counted by the camera)
RELOGIN_RETRY_DELAY_S = 2.0        # pause before repeating a refused share with the known-good passcode
CREDENTIAL_VARIANTS = ("md5", "sha256")
LAT_VARIANT = "lat"                # SHA256_HEX_UPPER(local access token) [spec:lat.tpap.passcode_candidates]
DEFAULT_ITERATIONS = 10000         # Spake2pRegisterResult default when the reply omits it
ENCRYPTION = "aes_128_ccm"
CIPHER_SUITE = 1                   # SPAKE2+-P256-SHA256-HKDF-SHA256-HMAC-SHA256

_LOCK_CODES = frozenset({-40404, -40408})
_BAD_USERNAME = -40209
_NEEDS_V3 = -40413                 # register answered with result.data.encrypt_type ["3"]

# ---------------------------------------------------------------- P-256
_P = 0xFFFFFFFF00000001000000000000000000000000FFFFFFFFFFFFFFFFFFFFFFFF
_A = _P - 3
_B = 0x5AC635D8AA3A93E7B3EBBD55769886BC651D06B0CC53B0F63BCE3C3E27D2604B
_N = 0xFFFFFFFF00000000FFFFFFFFFFFFFFFFBCE6FAADA7179E84F3B9CAC2FC632551
_G = (0x6B17D1F2E12C4247F8BCE6E563A440F277037D812DEB33A0F4A13945D898C296,
      0x4FE342E2FE1A7F9B8EE7EB4A7C0F9E162BCE33576B315ECECBB6406837BF51F5)

Point = tuple[int, int]


def _on_curve(p: Point) -> bool:
    x, y = p
    return 0 <= x < _P and 0 <= y < _P and (y * y - (x * x * x + _A * x + _B)) % _P == 0


def _add(p: Point | None, q: Point | None) -> Point | None:
    """Affine point addition; ``None`` is the point at infinity."""
    if p is None:
        return q
    if q is None:
        return p
    if p[0] == q[0]:
        if (p[1] + q[1]) % _P == 0:
            return None
        lam = (3 * p[0] * p[0] + _A) * pow(2 * p[1], -1, _P) % _P
    else:
        lam = (q[1] - p[1]) * pow(q[0] - p[0], -1, _P) % _P
    x = (lam * lam - p[0] - q[0]) % _P
    return x, (lam * (p[0] - x) - p[1]) % _P


def _mul(k: int, p: Point | None) -> Point | None:
    """Scalar multiplication (double-and-add)."""
    result: Point | None = None
    k %= _N
    addend = p
    while k and addend is not None:
        if k & 1:
            result = _add(result, addend)
        addend = _add(addend, addend)
        k >>= 1
    return result


def _neg(p: Point) -> Point:
    return p[0], (-p[1]) % _P


def decode_point(data: bytes) -> tuple[int, int]:
    """SEC1 decode (uncompressed 65 bytes or compressed 33 bytes); ValueError unless on P-256."""
    if not isinstance(data, (bytes, bytearray)):
        raise ValueError("point must be bytes")
    data = bytes(data)
    if len(data) == 65 and data[0] == 4:
        p = (int.from_bytes(data[1:33], "big"), int.from_bytes(data[33:], "big"))
    elif len(data) == 33 and data[0] in (2, 3):
        x = int.from_bytes(data[1:], "big")
        if x >= _P:
            raise ValueError("point x coordinate out of range")
        rhs = (x * x * x + _A * x + _B) % _P
        y = pow(rhs, (_P + 1) // 4, _P)
        if y * y % _P != rhs:
            raise ValueError("no point on P-256 with this x")
        if y & 1 != data[0] & 1:
            y = _P - y
        p = (x, y)
    else:
        raise ValueError("bad point encoding")
    if not _on_curve(p):
        raise ValueError("point is not on P-256")
    return p


def encode_point(point: tuple[int, int]) -> bytes:
    """SEC1 uncompressed encoding (65 bytes)."""
    return b"\x04" + point[0].to_bytes(32, "big") + point[1].to_bytes(32, "big")


# RFC 9383 section 4 / Spake2pCipherSuiteType: fixed P-256 points M and N (compressed SEC1).
M_POINT_HEX = "02886e2f97ace46e55ba9dd7242579f2993b64e16ef3dcab95afd497333d8fa12f"
N_POINT_HEX = "03d8bbd6c639c62937b04d997f38c3770719c629d7014d49a24b4f98baa1292b49"
_M = decode_point(bytes.fromhex(M_POINT_HEX))
_NPT = decode_point(bytes.fromhex(N_POINT_HEX))


# ---------------------------------------------------------------- KDFs
def _hkdf(ikm: bytes, salt: bytes | None, info: bytes, length: int) -> bytes:
    return HKDF(algorithm=hashes.SHA256(), length=length, salt=salt, info=info).derive(ikm)


def derive_w0_w1(credential: str, salt: bytes, iterations: int) -> tuple[int, int]:
    """PBKDF2-HMAC-SHA256(credential, salt, iterations, 80 bytes) -> (w0, w1), each 40 bytes mod n."""
    if iterations <= 0:
        raise ValueError("iterations must be positive")
    dk = hashlib.pbkdf2_hmac("sha256", credential.encode("utf-8"), salt, int(iterations), 80)
    return int.from_bytes(dk[:40], "big") % _N, int.from_bytes(dk[40:], "big") % _N


def tpap_context(user_random: bytes, dev_random: bytes) -> bytes:
    """SPAKE2+ context: SHA256("PAKE V1" || user_random || dev_random)."""
    return hashlib.sha256(CONTEXT_TAG + user_random + dev_random).digest()


def _lenpre(*parts: bytes) -> bytes:
    return b"".join(struct.pack("<Q", len(p)) + p for p in parts)


# ---------------------------------------------------------------- SPAKE2+ prover
@dataclass(frozen=True, repr=False)
class Spake2pKeys:
    """Result of one SPAKE2+ exchange (all secret; repr intentionally hidden)."""

    share_p: bytes
    share_v: bytes
    transcript: bytes
    k_main: bytes
    k_confirm_p: bytes
    k_confirm_v: bytes
    confirm_p: bytes
    confirm_v: bytes
    shared_key: bytes

    def __repr__(self) -> str:
        return "Spake2pKeys(<hidden>)"


class Spake2pProver:
    """RFC 9383 prover (the client). ``x`` is only for test vectors; normally it is random."""

    def __init__(self, w0: int, w1: int, context: bytes, *, id_prover: bytes = b"", id_verifier: bytes = b"",
                 x: int | None = None):
        self._w0 = int(w0) % _N
        self._w1 = int(w1) % _N
        self._context = bytes(context)
        self._id_p = bytes(id_prover)
        self._id_v = bytes(id_verifier)
        self._x = int(x) % _N if x is not None else secrets.randbelow(_N - 1) + 1
        if self._x == 0:
            raise ValueError("x must be non-zero")
        point = _add(_mul(self._x, _G), _mul(self._w0, _M))
        if point is None:  # pragma: no cover - probability ~2^-256
            raise ValueError("degenerate share")
        self._share = encode_point(point)

    def __repr__(self) -> str:
        return "Spake2pProver(<hidden>)"

    @property
    def share(self) -> bytes:
        """shareP = x*G + w0*M (uncompressed)."""
        return self._share

    def finish(self, peer_share: bytes) -> Spake2pKeys:
        """Process the verifier's shareV; ValueError if it is not a valid P-256 point."""
        y_pt = decode_point(peer_share)
        share_v = encode_point(y_pt)
        w0_n = _mul(self._w0, _NPT)
        base = _add(y_pt, _neg(w0_n) if w0_n is not None else None)  # Y - w0*N
        if base is None:
            raise ValueError("peer share cancels out")
        z = _mul(self._x, base)
        v = _mul(self._w1, base)
        if z is None or v is None:
            raise ValueError("degenerate SPAKE2+ result")
        tt = _lenpre(self._context, self._id_p, self._id_v, encode_point(_M), encode_point(_NPT),
                     self._share, share_v, encode_point(z), encode_point(v), self._w0.to_bytes(32, "big"))
        k_main = hashlib.sha256(tt).digest()
        conf = _hkdf(k_main, None, b"ConfirmationKeys", 64)
        kc_p, kc_v = conf[:32], conf[32:]
        return Spake2pKeys(
            share_p=self._share, share_v=share_v, transcript=tt, k_main=k_main, k_confirm_p=kc_p,
            k_confirm_v=kc_v, confirm_p=hmac.new(kc_p, share_v, hashlib.sha256).digest(),
            confirm_v=hmac.new(kc_v, self._share, hashlib.sha256).digest(),
            shared_key=_hkdf(k_main, None, b"SharedKey", 32))


# ---------------------------------------------------------------- credentials
def md5_hex(text: str) -> str:
    """Lowercase hex MD5 of the UTF-8 text (the app's ``wc1.a.h``)."""
    return hashlib.md5(text.encode("utf-8")).hexdigest()


def sha256_hex_upper(text: str) -> str:
    """Uppercase hex SHA-256 of the UTF-8 text (the app's ``libtapocameranetwork.util.k.h``)."""
    return hashlib.sha256(text.encode("utf-8")).hexdigest().upper()


_C64 = "./0123456789ABCDEFGHIJKLMNOPQRSTUVWXYZabcdefghijklmnopqrstuvwxyz"


def _b64_24(b2: int, b1: int, b0: int, n: int) -> str:
    v = (b2 << 16) | (b1 << 8) | b0
    out = []
    for _ in range(n):
        out.append(_C64[v & 63])
        v >>= 6
    return "".join(out)


def md5_crypt(key: str, prefix: str) -> str | None:
    """Unix MD5-crypt (``$1$``); ``None`` if ``prefix`` is not ``$1$...`` (``md5Shadow``)."""
    if len(key) > 30000 or not prefix.startswith("$1$"):
        return None
    k = key.encode("utf-8")
    rest = prefix[3:]
    end = rest.find("$")
    salt = rest[:min(8, end if end >= 0 else len(rest))].encode("utf-8")
    magic_salt = b"$1$" + salt
    alt = hashlib.md5(k + salt + k).digest()
    ctx = hashlib.md5(k + magic_salt)
    n = len(k)
    while n > 16:
        ctx.update(alt)
        n -= 16
    ctx.update(alt[:n])
    n = len(k)
    while n:
        ctx.update(b"\x00" if n & 1 else k[:1])
        n >>= 1
    final = ctx.digest()
    for i in range(1000):
        h = hashlib.md5(k if i & 1 else final)
        if i % 3:
            h.update(salt)
        if i % 7:
            h.update(k)
        h.update(final if i & 1 else k)
        final = h.digest()
    f = final
    enc = "".join(_b64_24(f[a], f[b], f[c], 4) for a, b, c in ((0, 6, 12), (1, 7, 13), (2, 8, 14), (3, 9, 15),
                                                                  (4, 10, 5)))
    enc += _b64_24(0, 0, f[11], 2)
    return magic_salt.decode("utf-8") + "$" + enc


def sha256_crypt(key: str, prefix: str) -> str:
    """Unix SHA-256-crypt (``$5$``, Drepper), salt <= 16 chars and ``rounds=`` taken from ``prefix``."""
    rest = prefix[3:] if prefix.startswith("$5$") else prefix
    rounds, custom = 5000, False
    if rest.startswith("rounds="):
        parts = rest.split("$")
        if len(parts) > 1:
            rounds = max(1000, min(int(parts[0][7:]), 999_999_999))
            rest, custom = parts[1], True
    end = rest.find("$")
    salt_s = rest[:min(end if end > 0 else len(rest), 16)]
    k, s = key.encode("utf-8"), salt_s.encode("utf-8")
    alt = hashlib.sha256(k + s + k).digest()
    ctx = hashlib.sha256(k + s)
    n = len(k)
    while n > 32:
        ctx.update(alt)
        n -= 32
    ctx.update(alt[:n])
    n = len(k)
    while n:
        ctx.update(alt if n & 1 else k)
        n >>= 1
    a = ctx.digest()
    dp = hashlib.sha256(k * len(k)).digest()
    p = (dp * (len(k) // 32 + 1))[:len(k)]
    ds = hashlib.sha256(s * (16 + a[0])).digest()
    sb = (ds * (len(s) // 32 + 1))[:len(s)]
    c = a
    for i in range(rounds):
        h = hashlib.sha256(p if i & 1 else c)
        if i % 3:
            h.update(sb)
        if i % 7:
            h.update(p)
        h.update(c if i & 1 else p)
        c = h.digest()
    order = ((0, 10, 20), (21, 1, 11), (12, 22, 2), (3, 13, 23), (24, 4, 14),
             (15, 25, 5), (6, 16, 26), (27, 7, 17), (18, 28, 8), (9, 19, 29))
    enc = "".join(_b64_24(c[x], c[y], c[z], 4) for x, y, z in order) + _b64_24(0, c[31], c[30], 3)
    return "$5$" + (f"rounds={rounds}$" if custom else "") + salt_s + "$" + enc


def _authkey(cred: str, tmpkey: str, dictionary: str) -> str:
    """``password_authkey``: out[i] = dict[(c ^ k) % len(dict)]; 187 pads the shorter string."""
    out = []
    for i in range(max(len(tmpkey), len(cred))):
        c = ord(cred[i]) if i < len(cred) else 187
        k = ord(tmpkey[i]) if i < len(tmpkey) else 187
        out.append(dictionary[(c ^ k) % len(dictionary)])
    return "".join(out)


def apply_extra_crypt(credential: str, extra_crypt: Any, *, mac: str = "") -> str:
    """Shape the passcode as the register reply's ``extra_crypt`` asks (``Spake2pExtraCryptBean``).

    Cameras register without a user name, so ``passwd_id`` 3 (needs one) and every unknown or
    malformed variant leave the passcode unchanged, exactly like the app.
    """
    if not isinstance(extra_crypt, dict):
        return credential
    kind = extra_crypt.get("type")
    params = extra_crypt.get("params")
    if not isinstance(kind, str) or not isinstance(params, dict):
        return credential
    kind = kind.lower()
    try:
        if kind == "password_shadow":
            pid = params.get("passwd_id")
            prefix = params.get("passwd_prefix")
            if pid == 1 and isinstance(prefix, str):
                shadow = md5_crypt(credential, prefix)
                return shadow if shadow is not None else credential
            if pid == 2:
                return hashlib.sha1(credential.encode("utf-8")).hexdigest()
            if pid == 5 and isinstance(prefix, str):
                return sha256_crypt(credential, prefix)
            return credential  # id 3 needs a user name (cameras send none); others unknown
        if kind == "password_authkey":
            tmpkey, dictionary = params.get("authkey_tmpkey"), params.get("authkey_dictionary")
            if isinstance(tmpkey, str) and isinstance(dictionary, str) and dictionary:
                return _authkey(credential, tmpkey, dictionary)
            return credential
        if kind == "password_sha_with_salt":
            salt = params.get("sha_salt")
            if not isinstance(salt, str):
                return credential
            name = "admin" if params.get("sha_name", -1) == 0 else "user"
            raw = base64.b64decode(salt).decode("utf-8", errors="replace")
            return hashlib.sha256((name + raw + credential).encode("utf-8")).hexdigest()
    except (ValueError, binascii.Error):
        log.debug("ignoring malformed extra_crypt parameters (%s)", kind)
    return credential


def credential_for(variant: str, password: str) -> str:
    """Passcode candidate: ``"md5"`` -> md5_hex(password), ``"sha256"`` -> SHA256_HEX_UPPER(password)."""
    if variant == "md5":
        return md5_hex(password)
    if variant == "sha256":
        return sha256_hex_upper(password)
    raise ValueError(f"unknown credential variant {variant!r}")


def camera_username(user_hash_type: int = 0, user: str = CAMERA_USER) -> str:
    """Register user name: md5_hex(user), or SHA256_HEX_UPPER(user) when TDP ``user_hash_type`` is 1."""
    return sha256_hex_upper(user) if user_hash_type == 1 else md5_hex(user)


# ---------------------------------------------------------------- /ds channel
class TpapCipher:
    """AES-128-CCM session cipher (``SecSessionCipher``): 16-byte tag, no AAD, 12-byte nonce."""

    def __init__(self, shared_key: bytes, encryption: str = ENCRYPTION):
        if encryption != ENCRYPTION:
            raise TapoProtocolError(f"unsupported TPAP session encryption {encryption!r}")
        key = _hkdf(shared_key, b"tp-kdf-salt-aes128-key", b"tp-kdf-info-aes128-key", 32)[:16]
        self._nonce = _hkdf(shared_key, b"tp-kdf-salt-aes128-iv", b"tp-kdf-info-aes128-iv", 32)[:12]
        self._aead = AESCCM(key, tag_length=16)

    def __repr__(self) -> str:
        return "TpapCipher(aes_128_ccm)"

    def _nonce_for(self, seq: int) -> bytes:
        return self._nonce[:8] + struct.pack(">I", seq & 0xFFFFFFFF)

    def encrypt(self, plaintext: bytes, seq: int) -> bytes:
        """Ciphertext || 16-byte tag for request number ``seq``."""
        return self._aead.encrypt(self._nonce_for(seq), plaintext, None)

    def decrypt(self, data: bytes, seq: int) -> bytes:
        """Inverse of :meth:`encrypt`; TapoProtocolError on a short body or tag failure."""
        if len(data) < 16:
            raise TapoProtocolError("TPAP payload is shorter than the CCM tag")
        try:
            return self._aead.decrypt(self._nonce_for(seq), data, None)
        except InvalidTag:
            raise TapoProtocolError("TPAP payload failed authentication") from None

    def seal(self, plaintext: bytes, seq: int) -> bytes:
        """``/ds`` request body: uint32_be(seq) || ciphertext || tag."""
        return struct.pack(">I", seq & 0xFFFFFFFF) + self.encrypt(plaintext, seq)

    def open(self, body: bytes) -> tuple[int, bytes]:
        """Parse a ``/ds`` reply body -> ``(seq, plaintext)`` (nonce built from the body's own seq)."""
        if len(body) < 4 + 16:
            raise TapoProtocolError("TPAP reply is too short")
        seq = struct.unpack(">I", body[:4])[0]
        return seq, self.decrypt(body[4:], seq)


class TpapRejected(TapoError):
    """The camera refused a TPAP login step with an error code that is not a credential failure."""

    def __init__(self, code: int | None, stage: str):
        super().__init__(f"TPAP {stage} rejected (error {code})")
        self.code = code
        self.stage = stage


class TpapNotSupported(TpapRejected):
    """``pake_register`` answered -40413 with ``encrypt_type ['3']``: use the v3 secure login instead."""


@dataclass(repr=False)
class TpapSession:
    """An established TPAP session (holds the stok and keys; repr hidden)."""

    stok: str
    start_seq: int
    cipher: TpapCipher
    variant: str
    expires_in: int | None = None

    def __repr__(self) -> str:
        return f"TpapSession(variant={self.variant!r}, expires_in={self.expires_in})"


def tpap_requested(tpap: dict | None, encrypt_type: Any = None) -> bool:
    """True if TDP hints say the camera wants TPAP (``tpap.pake`` non-empty or encrypt_type "4")."""
    if isinstance(tpap, dict) and tpap.get("pake"):
        return True
    types = [encrypt_type] if isinstance(encrypt_type, (str, int)) else list(encrypt_type or [])
    return "4" in [str(t) for t in types]


def _code(resp: Any) -> int:
    if not isinstance(resp, dict):
        raise TapoProtocolError("TPAP login reply is not a JSON object")
    code = resp.get("error_code", 0)
    if isinstance(code, str):
        try:
            code = int(code)
        except ValueError:
            pass
    if not isinstance(code, int) or isinstance(code, bool):
        raise TapoProtocolError("TPAP login reply has a non-integer error_code")
    return code


def _result(resp: dict) -> dict:
    res = resp.get("result")
    return res if isinstance(res, dict) else {}


def _b64field(res: dict, name: str) -> bytes:
    value = res.get(name)
    if not isinstance(value, str) or not value:
        raise TapoProtocolError(f"TPAP reply has no {name}")
    try:
        return base64.b64decode(value, validate=True)
    except (binascii.Error, ValueError):
        raise TapoProtocolError(f"TPAP reply field {name} is not base64") from None


def _int_field(res: dict, name: str, default: int | None = None) -> int | None:
    value = res.get(name, default)
    if value is None:
        return None
    if isinstance(value, str):
        try:
            value = int(value)
        except ValueError:
            pass
    if not isinstance(value, int) or isinstance(value, bool):
        raise TapoProtocolError(f"TPAP reply field {name} is not an integer")
    return value


def _raise_if_locked(code: int, resp: dict) -> None:
    if code in _LOCK_CODES:
        data = _result(resp).get("data")
        left = data.get("sec_left") if isinstance(data, dict) else None
        suffix = f"; retry in {left} s" if isinstance(left, int) and not isinstance(left, bool) else ""
        raise TapoLockedError(f"camera is blocking logins after too many failures (error {code}){suffix}")


@dataclass(repr=False)
class _Register:
    user_random: bytes
    dev_random: bytes
    dev_salt: bytes
    dev_share: bytes
    iterations: int
    encryption: str
    extra_crypt: Any


class TpapAuthenticator:
    """Runs the bounded TPAP login for one camera and remembers what worked.

    ``post_login(params)`` must POST ``{"method": "login", "params": params}`` to ``/`` and return the
    parsed JSON reply. At most :data:`MAX_ATTEMPTS` ``pake_share`` (password) attempts are made per
    :meth:`login`. Order: md5 first (the app's first candidate) unless the register reply carries
    ``extra_crypt`` (C200 style: SHA256 + shadow) - then sha256 first. With a cloud-issued local
    access token (``local_token``) SHA256_HEX_UPPER(LAT) is offered FIRST and the best password
    variant second [spec:lat.tpap.passcode_candidates], still within MAX_ATTEMPTS. After a success only the
    known-good variant is used; a refusal of it is retried once after :data:`RELOGIN_RETRY_DELAY_S`
    (C200 refuses the first share after a session ended). Lockout codes stop at once.
    """

    def __init__(self, post_login: Callable[[dict], dict], password: str, *, tpap: dict | None = None,
                 rng: Callable[[int], bytes] = os.urandom, sleep: Callable[[float], None] | None = None,
                 mac: str = "", local_token: str | None = None):
        self._post = post_login
        self._password = password
        self._local_token = local_token or None
        self._rng = rng
        self._sleep = sleep if sleep is not None else time.sleep
        self._mac = mac
        hash_type = tpap.get("user_hash_type", 0) if isinstance(tpap, dict) else 0
        self._usernames = [camera_username(1 if hash_type == 1 else 0)]
        if CAMERA_USER not in self._usernames:
            self._usernames.append(CAMERA_USER)  # some firmware wants the literal user name
        self._good_variant: str | None = None

    def __repr__(self) -> str:
        return f"TpapAuthenticator(good_variant={self._good_variant!r})"

    @property
    def good_variant(self) -> str | None:
        """Credential variant of the last successful login (``"lat"`` / ``"md5"`` / ``"sha256"``) or ``None``."""
        return self._good_variant

    # -- steps -------------------------------------------------------------------------
    def _register(self) -> _Register:
        while True:
            user_random = self._rng(32)
            username = self._usernames[0]
            resp = self._post({"sub_method": "pake_register", "username": username,
                               "user_random": base64.b64encode(user_random).decode("ascii"),
                               "cipher_suites": [CIPHER_SUITE], "encryption": [ENCRYPTION],
                               "passcode_type": "userpw"})
            code = _code(resp)
            _raise_if_locked(code, resp)
            if code == _BAD_USERNAME and len(self._usernames) > 1:
                log.debug("camera rejected the hashed TPAP user name; trying the literal one")
                self._usernames.pop(0)  # remembered for later logins too
                continue
            if code == _BAD_USERNAME:
                raise TapoAuthError("camera rejected the TPAP user name (error -40209)")
            if code == _NEEDS_V3:
                raise TpapNotSupported(code, "pake_register")
            if code != 0:
                raise TpapRejected(code, "pake_register")
            res = _result(resp)
            suite = _int_field(res, "cipher_suites", CIPHER_SUITE)
            if suite != CIPHER_SUITE:
                raise TapoProtocolError(f"camera chose unsupported TPAP cipher suite {suite}")
            iterations = _int_field(res, "iterations", DEFAULT_ITERATIONS) or DEFAULT_ITERATIONS
            if not 0 < iterations <= 10_000_000:
                raise TapoProtocolError("TPAP iteration count out of range")
            encryption = res.get("encryption") or ENCRYPTION
            if not isinstance(encryption, str):
                raise TapoProtocolError("TPAP reply has a bad encryption field")
            return _Register(user_random, _b64field(res, "dev_random"), _b64field(res, "dev_salt"),
                             _b64field(res, "dev_share"), iterations, encryption, res.get("extra_crypt"))

    def _passcode(self, variant: str) -> str:
        """Raw passcode of ``variant``: ``"lat"`` -> SHA256_HEX_UPPER(LAT) [spec:lat.tpap.passcode_candidates]."""
        if variant == LAT_VARIANT:
            assert self._local_token is not None
            return sha256_hex_upper(self._local_token)
        return credential_for(variant, self._password)

    def _share(self, reg: _Register, variant: str) -> TpapSession | int:
        """One password attempt. Returns the session, or the camera's error code on refusal."""
        passcode = apply_extra_crypt(self._passcode(variant), reg.extra_crypt, mac=self._mac)
        w0, w1 = derive_w0_w1(passcode, reg.dev_salt, reg.iterations)
        prover = Spake2pProver(w0, w1, tpap_context(reg.user_random, reg.dev_random))
        try:
            keys = prover.finish(reg.dev_share)
        except ValueError:
            raise TapoProtocolError("camera sent an invalid SPAKE2+ share") from None
        cipher = TpapCipher(keys.shared_key, reg.encryption)  # validate before spending an attempt
        resp = self._post({"sub_method": "pake_share",
                           "user_share": base64.b64encode(keys.share_p).decode("ascii"),
                           "user_confirm": base64.b64encode(keys.confirm_p).decode("ascii")})
        code = _code(resp)
        _raise_if_locked(code, resp)
        if code != 0:
            return code
        res = _result(resp)
        if not hmac.compare_digest(_b64field(res, "dev_confirm"), keys.confirm_v):
            raise TapoAuthError("camera could not prove it knows the password (TPAP dev_confirm mismatch)")
        stok = res.get("stok")
        if not isinstance(stok, str) or not stok:
            raise TapoProtocolError("TPAP login reply has no stok")
        start_seq = _int_field(res, "start_seq")
        if start_seq is None:
            raise TapoProtocolError("TPAP login reply has no start_seq")
        return TpapSession(stok=stok, start_seq=start_seq, cipher=cipher, variant=variant,
                           expires_in=_int_field(res, "expired"))

    # -- login -------------------------------------------------------------------------
    def login(self) -> TpapSession:
        """Bounded login; TapoAuthError / TapoLockedError / TpapRejected / TapoProtocolError."""
        if not self._password and not self._local_token:
            raise TapoAuthError("no password for the TPAP login (sign in with the TP-Link ID)")
        known = self._good_variant
        last_code: int | None = None
        tried: list[str] = []
        for attempt in range(MAX_ATTEMPTS):
            if attempt and known is not None:
                self._sleep(RELOGIN_RETRY_DELAY_S)
            if known is None and not [v for v in self._order(None) if v not in tried]:
                break  # LAT-only login: nothing left to offer (the order only grows with extra_crypt)
            reg = self._register()
            if known is not None:
                variant = known
            else:
                variant = next((v for v in self._order(reg.extra_crypt) if v not in tried), "")
                if not variant:
                    break
            tried.append(variant)
            result = self._share(reg, variant)
            if isinstance(result, TpapSession):
                self._good_variant = variant
                log.debug("TPAP login succeeded (variant %s, attempt %d)", variant, attempt + 1)
                return result
            last_code = result
            log.debug("TPAP pake_share refused (error %s, variant %s)", result, variant)
        self._good_variant = None  # e.g. the password changed: the next login tries both variants again
        if self._local_token:
            raise TapoAuthError("camera rejected the local access token and the TP-Link ID password "
                                f"(TPAP, error {last_code})")
        raise TapoAuthError(f"camera rejected the TP-Link ID password (TPAP, error {last_code})")

    def _order(self, extra_crypt: Any) -> list[str]:
        """Candidate passcodes, best first: the LAT (if any), then the password variants.

        With ``extra_crypt`` (C200 style) SHA-256 is the better password guess, else md5 (the app's
        first candidate). Without a password only the LAT is offered.
        """
        order = [LAT_VARIANT] if self._local_token else []
        if self._password:
            order += ["sha256", "md5"] if extra_crypt else list(CREDENTIAL_VARIANTS)
        return order
