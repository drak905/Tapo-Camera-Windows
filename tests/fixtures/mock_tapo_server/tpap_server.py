"""Camera side of the TPAP local login (SPAKE2+ verifier) and the AES-128-CCM ``/ds`` channel.

Active only for ``MockTapoCamera(protocol="tpap")`` ([spec:tpap.*], specs/tpap_spec.json). Deliberately
does NOT import ``tapo_desktop``: the EC arithmetic, KDFs and sha256-crypt below are an independent
re-implementation so the mock stays a reference for the client.

Flow (all on HTTPS :443):
  POST / {"method":"login","params":{"cnonce":..,"encrypt_type":"3",..}} -> {"error_code": -40211}
  POST / {"method":"login","params":{"sub_method":"pake_register", username, user_random,
          cipher_suites:[1], encryption:["aes_128_ccm"], passcode_type:"userpw"}}
       -> dev_salt, dev_random, dev_share (Y), iterations, cipher_suites, encryption[, extra_crypt]
  POST / {"method":"login","params":{"sub_method":"pake_share", user_share (X), user_confirm}}
       -> dev_confirm, stok, start_seq, expired      (wrong password -> -40401)
  POST /stok=<stok>/ds  application/octet-stream  uint32_be(seq) || CCM(ct) || tag16
       -> same layout with the request's seq; any refusal is a plaintext JSON error and ends the
          session (-40401 unknown stok / replayed seq / bad tag, -40209 not a multipleRequest)

Credential modes (``cam.tpap_credential``):
  "md5"           passcode = md5_hex(cloud password), no extra_crypt
  "sha256_extra"  passcode = sha256_crypt(SHA256_HEX_UPPER(cloud password), prefix); the register
                  reply carries extra_crypt {"type":"password_shadow","params":{"passwd_id":5,
                  "passwd_prefix":prefix}} (C200 hw 5.0 style)
  "lat"           passcode = SHA256_HEX_UPPER(cam.local_token) (camera synced to the cloud LAT,
                  [spec:lat.tpap.passcode_candidates]); no extra_crypt
"""
from __future__ import annotations

import base64
import binascii
import hashlib
import hmac
import json
import secrets
import struct
import threading
from dataclasses import dataclass
from typing import TYPE_CHECKING, Any

from cryptography.exceptions import InvalidTag
from cryptography.hazmat.primitives.ciphers.aead import AESCCM

if TYPE_CHECKING:
    from .camera import MockTapoCamera
    from .https_server import HttpsServer

# ---------------------------------------------------------------- P-256 (affine, independent)
_P = 0xFFFFFFFF00000001000000000000000000000000FFFFFFFFFFFFFFFFFFFFFFFF
_A = _P - 3
_B = 0x5AC635D8AA3A93E7B3EBBD55769886BC651D06B0CC53B0F63BCE3C3E27D2604B
_N = 0xFFFFFFFF00000000FFFFFFFFFFFFFFFFBCE6FAADA7179E84F3B9CAC2FC632551
_G = (0x6B17D1F2E12C4247F8BCE6E563A440F277037D812DEB33A0F4A13945D898C296,
      0x4FE342E2FE1A7F9B8EE7EB4A7C0F9E162BCE33576B315ECECBB6406837BF51F5)

def _add(p, q):
    if p is None:
        return q
    if q is None:
        return p
    if p[0] == q[0] and (p[1] + q[1]) % _P == 0:
        return None
    if p == q:
        lam = (3 * p[0] * p[0] + _A) * pow(2 * p[1], -1, _P) % _P
    else:
        lam = (q[1] - p[1]) * pow(q[0] - p[0], -1, _P) % _P
    x = (lam * lam - p[0] - q[0]) % _P
    return x, (lam * (p[0] - x) - p[1]) % _P


def _mul(k: int, p):
    r = None
    k %= _N
    while k:
        if k & 1:
            r = _add(r, p)
        p = _add(p, p)
        k >>= 1
    return r


def _neg(p):
    return None if p is None else (p[0], (-p[1]) % _P)


def _on_curve(p) -> bool:
    return p is not None and 0 <= p[0] < _P and 0 <= p[1] < _P and \
        (p[1] * p[1] - (p[0] ** 3 + _A * p[0] + _B)) % _P == 0


def _decode(b: bytes):
    if len(b) == 65 and b[0] == 4:
        p = (int.from_bytes(b[1:33], "big"), int.from_bytes(b[33:], "big"))
    elif len(b) == 33 and b[0] in (2, 3):
        x = int.from_bytes(b[1:], "big")
        y = pow((x ** 3 + _A * x + _B) % _P, (_P + 1) // 4, _P)
        if y & 1 != b[0] & 1:
            y = _P - y
        p = (x, y)
    else:
        raise ValueError("bad point encoding")
    if not _on_curve(p):
        raise ValueError("point not on P-256")
    return p


def _encode(p) -> bytes:
    assert p is not None
    return b"\x04" + p[0].to_bytes(32, "big") + p[1].to_bytes(32, "big")


_M = _decode(bytes.fromhex("02886e2f97ace46e55ba9dd7242579f2993b64e16ef3dcab95afd497333d8fa12f"))
_NPT = _decode(bytes.fromhex("03d8bbd6c639c62937b04d997f38c3770719c629d7014d49a24b4f98baa1292b49"))


# ---------------------------------------------------------------- KDFs / credentials
def _hkdf(ikm: bytes, salt: bytes | None, info: bytes, length: int) -> bytes:
    prk = hmac.new(salt if salt else b"\x00" * 32, ikm, hashlib.sha256).digest()
    out, block, i = b"", b"", 1
    while len(out) < length:
        block = hmac.new(prk, block + info + bytes([i]), hashlib.sha256).digest()
        out += block
        i += 1
    return out[:length]


def md5_hex(s: str) -> str:
    return hashlib.md5(s.encode("utf-8")).hexdigest()


def sha256_hex_upper(s: str) -> str:
    return hashlib.sha256(s.encode("utf-8")).hexdigest().upper()


_C64 = "./0123456789ABCDEFGHIJKLMNOPQRSTUVWXYZabcdefghijklmnopqrstuvwxyz"


def sha256_crypt(key: str, prefix: str) -> str:
    """Drepper SHA-crypt "$5$" (default 5000 rounds), salt <= 16 chars taken from ``prefix``."""
    rest = prefix[3:] if prefix.startswith("$5$") else prefix
    rounds, custom = 5000, False
    if rest.startswith("rounds=") and "$" in rest:
        r, rest = rest.split("$", 1)
        rounds, custom = max(1000, min(999_999_999, int(r[7:]))), True
    salt = rest.split("$", 1)[0][:16]
    k, s = key.encode(), salt.encode()
    b = hashlib.sha256(k + s + k).digest()
    ctx = hashlib.sha256(k + s)
    n = len(k)
    while n > 32:
        ctx.update(b)
        n -= 32
    ctx.update(b[:n])
    n = len(k)
    while n:
        ctx.update(b if n & 1 else k)
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

    def b64(v: int, n: int) -> str:
        out = ""
        for _ in range(n):
            out += _C64[v & 63]
            v >>= 6
        return out

    order = [(0, 10, 20), (21, 1, 11), (12, 22, 2), (3, 13, 23), (24, 4, 14),
             (15, 25, 5), (6, 16, 26), (27, 7, 17), (18, 28, 8), (9, 19, 29)]
    enc = "".join(b64((c[x] << 16) | (c[y] << 8) | c[z], 4) for x, y, z in order) + b64((c[31] << 8) | c[30], 3)
    return "$5$" + (f"rounds={rounds}$" if custom else "") + salt + "$" + enc


def _lenpre(*parts: bytes) -> bytes:
    return b"".join(struct.pack("<Q", len(p)) + p for p in parts)


def _b64d(value: Any) -> bytes:
    if not isinstance(value, str):
        raise ValueError("not a base64 string")
    try:
        return base64.b64decode(value, validate=True)
    except (binascii.Error, ValueError) as exc:
        raise ValueError("bad base64") from exc


def _b64e(b: bytes) -> str:
    return base64.b64encode(b).decode("ascii")


# ---------------------------------------------------------------- server state
@dataclass
class _Pending:
    user_random: bytes
    dev_random: bytes
    w0: int
    w1: int
    y: int
    share_v: bytes


@dataclass
class TpapSession:
    stok: str
    key: bytes
    nonce: bytes
    next_seq: int

    def nonce_for(self, seq: int) -> bytes:
        return self.nonce[:8] + struct.pack(">I", seq & 0xFFFFFFFF)


class TpapEndpoint:
    """TPAP login + /ds handler bound to one MockTapoCamera / HttpsServer."""

    def __init__(self, cam: "MockTapoCamera", api: "HttpsServer"):
        self.cam = cam
        self.api = api
        self.lock = threading.Lock()
        self.pending: _Pending | None = None
        self.sessions: dict[str, TpapSession] = {}
        self.prefix = "$5$" + "".join(secrets.choice(_C64[2:]) for _ in range(16)) + "$"

    # -- helpers -----------------------------------------------------------------------
    def expire(self) -> None:
        with self.lock:
            self.sessions.clear()

    def passcode(self) -> str:
        pw = self.cam.cloud_password
        if self.cam.tpap_credential == "lat":
            return sha256_hex_upper(self.cam.local_token)
        if self.cam.tpap_credential == "sha256_extra":
            return sha256_crypt(sha256_hex_upper(pw), self.prefix)
        return md5_hex(pw)

    def expected_username(self) -> str:
        return "admin" if self.cam.tpap_username == "admin" else md5_hex("admin")

    def _locked(self) -> dict | None:
        left = self.cam._lock_remaining()
        if left > 0:
            return {"error_code": -40404, "result": {"data": {"code": -40404, "sec_left": int(left) + 1}}}
        return None

    # -- login -------------------------------------------------------------------------
    def handle_login(self, params: Any) -> dict:
        cam = self.cam
        cam._count("login_attempts")
        if not isinstance(params, dict):
            return {"error_code": -40210}
        sub = params.get("sub_method")
        if sub == "pake_register":
            return self._register(params)
        if sub == "pake_share":
            return self._share(params)
        if sub is None and ("encrypt_type" in params or "password" in params or "cnonce" in params):
            cam._count("v3_probe_count")
            return {"error_code": -40211}  # what TPAP-only firmware answers to the v3 / legacy login
        return {"error_code": -40209}

    def _register(self, params: dict) -> dict:
        cam = self.cam
        cam._count("tpap_register_count")
        locked = self._locked()
        if locked:
            return locked
        if params.get("username") != self.expected_username():
            return {"error_code": -40209}
        suites = params.get("cipher_suites")
        enc = params.get("encryption", ["aes_128_ccm"])
        if (params.get("passcode_type") != "userpw" or not isinstance(suites, list) or 1 not in suites
                or not isinstance(enc, list) or "aes_128_ccm" not in enc):
            return {"error_code": -40209}
        try:
            user_random = _b64d(params.get("user_random"))
        except ValueError:
            return {"error_code": -40209}
        if not 16 <= len(user_random) <= 64:
            return {"error_code": -40209}
        salt = secrets.token_bytes(16)
        dev_random = secrets.token_bytes(32)
        iterations = int(cam.tpap_iterations)
        dk = hashlib.pbkdf2_hmac("sha256", self.passcode().encode("utf-8"), salt, iterations, 80)
        w0 = int.from_bytes(dk[:40], "big") % _N
        w1 = int.from_bytes(dk[40:], "big") % _N
        y = secrets.randbelow(_N - 1) + 1
        share_v = _encode(_add(_mul(y, _G), _mul(w0, _NPT)))
        with self.lock:
            self.pending = _Pending(user_random, dev_random, w0, w1, y, share_v)
        result: dict[str, Any] = {"dev_salt": _b64e(salt), "dev_random": _b64e(dev_random), "dev_share": _b64e(share_v),
                                  "cipher_suites": 1, "iterations": iterations, "encryption": "aes_128_ccm"}
        if cam.tpap_credential == "sha256_extra":
            result["extra_crypt"] = {"type": "password_shadow",
                                     "params": {"passwd_id": 5, "passwd_prefix": self.prefix}}
        return {"error_code": 0, "result": result}

    def _share(self, params: dict) -> dict:
        cam = self.cam
        cam._count("tpap_share_attempts")
        locked = self._locked()
        if locked:
            return locked
        with self.lock:
            pend, self.pending = self.pending, None
        if pend is None:
            return {"error_code": -40401}
        try:
            share_p = _b64d(params.get("user_share"))
            user_confirm = _b64d(params.get("user_confirm"))
            xpt = _decode(share_p)
        except ValueError:
            return {"error_code": -40209}
        share_p = _encode(xpt)
        h = _add(xpt, _neg(_mul(pend.w0, _M)))
        z = _mul(pend.y, h)
        v = _mul(pend.y, _mul(pend.w1, _G))
        if z is None or v is None:
            return {"error_code": -40401}
        context = hashlib.sha256(b"PAKE V1" + pend.user_random + pend.dev_random).digest()
        tt = _lenpre(context, b"", b"", _encode(_M), _encode(_NPT), share_p, pend.share_v, _encode(z), _encode(v),
                     pend.w0.to_bytes(32, "big"))
        k_main = hashlib.sha256(tt).digest()
        conf = _hkdf(k_main, None, b"ConfirmationKeys", 64)
        kc_a, kc_b = conf[:32], conf[32:]
        shared = _hkdf(k_main, None, b"SharedKey", 32)
        if not hmac.compare_digest(user_confirm, hmac.new(kc_a, pend.share_v, hashlib.sha256).digest()):
            cam._register_failure()
            return {"error_code": -40401}
        with cam._lock:
            refuse = cam.tpap_refuse_shares > 0
            if refuse:
                cam.tpap_refuse_shares -= 1
        if refuse:  # C200 quirk: a correct share right after a session ended is refused once
            return {"error_code": -40401}
        dev_confirm = hmac.new(kc_b, share_p, hashlib.sha256).digest()
        if cam.tpap_bad_dev_confirm:
            dev_confirm = bytes(b ^ 0x5A for b in dev_confirm)
        key = _hkdf(shared, b"tp-kdf-salt-aes128-key", b"tp-kdf-info-aes128-key", 32)[:16]
        nonce = _hkdf(shared, b"tp-kdf-salt-aes128-iv", b"tp-kdf-info-aes128-iv", 32)[:12]
        stok = secrets.token_hex(16)
        start_seq = cam.fixed_start_seq if cam.fixed_start_seq is not None else secrets.randbelow(9000) + 100
        with self.lock:
            self.sessions[stok] = TpapSession(stok, key, nonce, start_seq)
        cam._count("login_count")
        cam._reset_failures()
        return {"error_code": 0, "result": {"dev_confirm": _b64e(dev_confirm), "stok": stok, "start_seq": start_seq,
                                            "expired": int(cam.tpap_session_expired)}}

    # -- data --------------------------------------------------------------------------
    def _kill(self, stok: str) -> None:
        with self.lock:
            self.sessions.pop(stok, None)

    def handle_ds(self, stok: str, raw: bytes) -> tuple[str, bytes]:
        """-> (content_type, body). Plaintext JSON for refusals, binary for answers."""
        cam = self.cam

        def refuse(code: int) -> tuple[str, bytes]:
            self._kill(stok)
            return "application/json; charset=UTF-8", json.dumps({"error_code": code}).encode("utf-8")

        with self.lock:
            sess = self.sessions.get(stok)
        if sess is None:
            return refuse(-40401)
        if self.api._expiry_fires():
            return refuse(-40401)
        if len(raw) < 4 + 16:
            return refuse(-40401)
        seq = struct.unpack(">I", raw[:4])[0]
        with cam._lock:
            cam.tpap_ds_seqs.append(seq)
        if seq < sess.next_seq:
            return refuse(-40401)
        try:
            plain = AESCCM(sess.key, tag_length=16).decrypt(sess.nonce_for(seq), raw[4:], None)
        except InvalidTag:
            return refuse(-40401)
        sess.next_seq = seq + 1
        try:
            inner = json.loads(plain.decode("utf-8"))
        except (UnicodeDecodeError, ValueError):
            return refuse(-40209)
        if not isinstance(inner, dict) or inner.get("method") != "multipleRequest":
            return refuse(-40209)
        reply = self.api.handle_inner(inner)
        data = json.dumps(reply, separators=(",", ":")).encode("utf-8")
        body = struct.pack(">I", seq) + AESCCM(sess.key, tag_length=16).encrypt(sess.nonce_for(seq), data, None)
        return "application/octet-stream", body
