"""Lane-private helpers for the core test suite (lane-tdd-core).

Everything here is computed independently of ``tapo_desktop`` (hashlib + cryptography only) so
the tests cross-check the implementation instead of re-using it.
"""
from __future__ import annotations

import base64
import hashlib
import json
import pathlib
import threading
from typing import Any

from cryptography.hazmat.primitives import padding as sym_padding
from cryptography.hazmat.primitives.ciphers import Cipher, algorithms, modes

ROOT = pathlib.Path(__file__).resolve().parents[2]
SPEC_PATH = ROOT / "specs" / "tapo_protocol_spec.json"

_SPEC_CACHE: dict[str, dict] | None = None


def spec_items() -> dict[str, dict]:
    global _SPEC_CACHE
    if _SPEC_CACHE is None:
        data = json.loads(SPEC_PATH.read_text(encoding="utf-8"))
        _SPEC_CACHE = {it["id"]: it for it in data["items"]}
    return _SPEC_CACHE


def spec_value(item_id: str) -> dict:
    return spec_items()[item_id]["value"]


def golden() -> dict:
    """Golden vector [spec:auth.golden_vector.v3] with short key names."""
    v = spec_value("auth.golden_vector.v3")

    def pick(prefix: str) -> Any:
        for k, val in v.items():
            if k.startswith(prefix):
                return val
        raise KeyError(prefix)

    return {
        **v["inputs"],
        "md5_upper": pick("legacy_md5_upper_password"),
        "sha256_upper": pick("sha256_upper_password"),
        "first_hash": pick("first_hash"),
        "device_confirm": pick("expected_device_confirm"),
        "digest_passwd": pick("digest_passwd"),
        "lsk": bytes.fromhex(pick("lsk_hex")),
        "ivb": bytes.fromhex(pick("ivb_hex")),
        "inner_json": pick("inner_request_json"),
        "ciphertext": pick("ciphertext_b64"),
        "outer_body": pick("outer_body_json"),
        "tag_prefix": pick("tag_prefix"),
        "tapo_tag": pick("tapo_tag"),
        "headers": v["http_headers"],
        "ffs": v["ffs_default_password_example"],
    }


def dumps(obj: Any) -> str:
    return json.dumps(obj, separators=(",", ":"), ensure_ascii=False)


# --------------------------------------------------------------------------- reference crypto
def sha256u(s: str) -> str:
    return hashlib.sha256(s.encode("utf-8")).hexdigest().upper()


def md5u(s: str) -> str:
    return hashlib.md5(s.encode("utf-8")).hexdigest().upper()


def ref_keys(password: str, cnonce: str, nonce: str) -> tuple[bytes, bytes]:
    first = sha256u(cnonce + sha256u(password) + nonce)
    lsk = hashlib.sha256(("lsk" + cnonce + nonce + first).encode()).digest()[:16]
    ivb = hashlib.sha256(("ivb" + cnonce + nonce + first).encode()).digest()[:16]
    return lsk, ivb


def ref_encrypt(lsk: bytes, ivb: bytes, plaintext: bytes) -> str:
    padder = sym_padding.PKCS7(128).padder()
    data = padder.update(plaintext) + padder.finalize()
    enc = Cipher(algorithms.AES(lsk), modes.CBC(ivb)).encryptor()
    return base64.b64encode(enc.update(data) + enc.finalize()).decode("ascii")


def ref_encrypt_raw(lsk: bytes, ivb: bytes, raw_blocks: bytes) -> str:
    """Encrypt pre-padded bytes (no PKCS7) - for crafting bad-padding ciphertexts."""
    enc = Cipher(algorithms.AES(lsk), modes.CBC(ivb)).encryptor()
    return base64.b64encode(enc.update(raw_blocks) + enc.finalize()).decode("ascii")


def ref_decrypt(lsk: bytes, ivb: bytes, b64: str) -> bytes:
    dec = Cipher(algorithms.AES(lsk), modes.CBC(ivb)).decryptor()
    data = dec.update(base64.b64decode(b64)) + dec.finalize()
    unpadder = sym_padding.PKCS7(128).unpadder()
    return unpadder.update(data) + unpadder.finalize()


def ref_tag(password: str, cnonce: str, body: str, seq: int) -> str:
    return sha256u(sha256u(sha256u(password) + cnonce) + body + str(seq))


# --------------------------------------------------------------------------- fake camera transport
def _lower(headers: dict[str, str]) -> dict[str, str]:
    return {k.lower(): v for k, v in (headers or {}).items()}


class FakeCameraTransport:
    """In-process stand-in for ``HttpsTransport`` that behaves like a strict Tapo camera.

    Implements ``post(path, body, headers) -> (status, headers, text)``, ``fingerprint`` and ``close``.
    secure=True: encrypt_type "3" login (-40413 challenge), securePassthrough with seq/tapo_tag check.
    secure=False: legacy MD5 login, plaintext JSON on /stok=<stok>/ds.
    """

    def __init__(self, password: str, *, secure: bool = True, nonce: str = "0123456789ABCDEF",
                 start_seq: int = 100, stok: str = "FAKESTOK0001"):
        self.password = password
        self.secure = secure
        self.nonce = nonce
        self.start_seq = start_seq
        self.stok_base = stok
        self.stok: str | None = None
        self.lock = threading.Lock()
        self.posts: list[tuple[str, str, dict[str, str]]] = []
        self.inner_requests: list[dict] = []
        self.login_count = 0
        self.expected_seq: int | None = None
        self.cnonce: str | None = None
        self.keys: tuple[bytes, bytes] | None = None
        self.seq_errors = 0
        self.tag_errors = 0
        self.expire_next = 0           # next N data requests -> -40401 (top level)
        self.always_expired = False
        self.http_status_next: list[int] = []
        self.item_errors: dict[str, list[int]] = {}
        self.lock_code: int | None = None   # login -> this error code (e.g. -40404)
        self.clips: list[tuple[int, int, str]] = []
        self.days: list[str] = []
        self.closed = False
        self.fingerprint = "00" * 32

    # ---- transport API
    def close(self) -> None:
        self.closed = True

    def post(self, path: str, body: str, headers: dict[str, str]) -> tuple[int, dict[str, str], str]:
        with self.lock:
            self.posts.append((path, body, dict(headers or {})))
            if self.http_status_next:
                return self.http_status_next.pop(0), {}, ""
            if path in ("/", ""):
                return 200, {}, dumps(self._login(json.loads(body)))
            if self.stok is None or path != f"/stok={self.stok}/ds":
                return 200, {}, dumps({"error_code": -40401})
            if self.always_expired or self.expire_next > 0:
                if self.expire_next > 0:
                    self.expire_next -= 1
                self.stok = None
                return 200, {}, dumps({"error_code": -40401})
            if not self.secure:
                return 200, {}, dumps(self._dispatch(json.loads(body)))
            return self._secure(body, headers)

    # ---- login
    def _login(self, req: dict) -> dict:
        params = req.get("params") or {}
        if req.get("method") != "login":
            return {"error_code": -40106}
        if self.lock_code is not None:
            return {"error_code": self.lock_code}
        if self.secure:
            if params.get("encrypt_type") != "3" or "cnonce" not in params:
                return {"error_code": -40413, "result": {"data": {"code": -40413, "encrypt_type": ["3"]}}}
            cnonce = params["cnonce"]
            if "digest_passwd" not in params:
                self.cnonce = cnonce
                confirm = sha256u(cnonce + sha256u(self.password) + self.nonce) + self.nonce + cnonce
                return {"error_code": -40413, "result": {"data": {
                    "code": -40413, "encrypt_type": ["3"], "key": "unused-key",
                    "nonce": self.nonce, "device_confirm": confirm}}}
            expected = sha256u(sha256u(self.password) + cnonce + self.nonce) + cnonce + self.nonce
            if cnonce != self.cnonce or params["digest_passwd"] != expected:
                return {"error_code": -40401, "result": {"data": {"code": -40401}}}
            self.login_count += 1
            self.stok = f"{self.stok_base}{self.login_count}"
            self.keys = ref_keys(self.password, cnonce, self.nonce)
            self.expected_seq = self.start_seq
            return {"error_code": 0, "result": {"stok": self.stok, "start_seq": self.start_seq, "user_group": "root"}}
        # legacy
        if params.get("hashed") is True and params.get("password") == md5u(self.password) \
                and params.get("username") == "admin" and "cnonce" not in params:
            self.login_count += 1
            self.stok = f"{self.stok_base}{self.login_count}"
            return {"error_code": 0, "result": {"stok": self.stok, "user_group": "root"}}
        return {"error_code": -40401, "result": {"data": {"code": -40401}}}

    # ---- secure data path
    def _secure(self, body: str, headers: dict[str, str]) -> tuple[int, dict[str, str], str]:
        h = _lower(headers)
        assert self.keys is not None and self.cnonce is not None
        try:
            seq = int(h.get("seq", ""))
        except ValueError:
            self.seq_errors += 1
            return 200, {}, dumps({"error_code": -40401})
        if seq != self.expected_seq:
            self.seq_errors += 1
            return 200, {}, dumps({"error_code": -40401})
        if h.get("tapo_tag") != ref_tag(self.password, self.cnonce, body, seq):
            self.tag_errors += 1
            return 200, {}, dumps({"error_code": -40401})
        self.expected_seq = seq + 1
        outer = json.loads(body)
        if outer.get("method") != "securePassthrough":
            return 200, {}, dumps({"error_code": -40210})
        lsk, ivb = self.keys
        inner = json.loads(ref_decrypt(lsk, ivb, outer["params"]["request"]).decode("utf-8"))
        resp = self._dispatch(inner)
        enc = ref_encrypt(lsk, ivb, dumps(resp).encode("utf-8"))
        return 200, {}, dumps({"error_code": 0, "result": {"response": enc}})

    # ---- command dispatch
    def _dispatch(self, inner: dict) -> dict:
        if inner.get("method") != "multipleRequest":
            return {"error_code": -40210}
        responses = []
        for item in inner["params"]["requests"]:
            self.inner_requests.append(item)
            method = item.get("method")
            errs = self.item_errors.get(method)
            if errs:
                responses.append({"method": method, "result": {}, "error_code": errs.pop(0)})
                continue
            responses.append({"method": method, "result": self._result(method, item.get("params") or {}),
                              "error_code": 0})
        return {"error_code": 0, "result": {"responses": responses}}

    def _result(self, method: str, params: dict) -> dict:
        if method == "getDeviceInfo":
            return {"device_info": {"basic_info": {
                "device_model": "C210", "device_alias": "Front door", "sw_version": "1.3.9 Build 231019",
                "hw_version": "2.0", "mac": "AA-BB-CC-DD-EE-FF", "dev_id": "0123456789ABCDEF",
                "device_type": "SMART.IPCAMERA"}}}
        if method == "getLedStatus":
            return {"led": {"config": {"enabled": "on"}}}
        if method == "searchDateWithVideo":
            return {"playback": {"search_results": [
                {f"search_results_{i + 1}": {"date": d}} for i, d in enumerate(self.days)]}}
        if method == "searchVideoWithUTC":
            p = params["playback"]["search_video_with_utc"]
            lo, hi = int(p["start_time"]), int(p["end_time"])
            matching = [c for c in self.clips if lo <= c[0] <= hi]
            si, ei = int(p["start_index"]), int(p["end_index"])
            page = matching[si:ei + 1]
            return {"playback": {
                "search_video_results": [
                    {f"search_video_results_{si + i + 1}": {"startTime": s, "endTime": e,
                                                             "vedio_type": t, "video_type": t}}
                    for i, (s, e, t) in enumerate(page)],
                "to_be_continued": 1 if len(matching) > ei + 1 else 0}}
        return {}

    # ---- inspection helpers
    def data_headers(self) -> list[dict[str, str]]:
        return [_lower(h) for p, _b, h in self.posts if p.startswith("/stok=")]

    def login_bodies(self) -> list[dict]:
        return [json.loads(b) for p, b, _h in self.posts if p in ("/", "")]
