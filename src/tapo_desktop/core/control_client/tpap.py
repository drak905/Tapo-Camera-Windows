"""TPAP local login (SPAKE2+ over P-256) and the AES-128-CCM ``/stok=<stok>/ds`` channel.

RED-phase stub: signatures only.
"""
from __future__ import annotations

import os
from dataclasses import dataclass
from typing import Any, Callable

from tapo_desktop.core.errors import TapoError

CONTEXT_TAG = b"PAKE V1"
CAMERA_USER = "admin"
MAX_ATTEMPTS = 2
RELOGIN_RETRY_DELAY_S = 2.0
CREDENTIAL_VARIANTS = ("md5", "sha256")


def decode_point(data: bytes) -> tuple[int, int]:
    raise NotImplementedError


def encode_point(point: tuple[int, int]) -> bytes:
    raise NotImplementedError


M_POINT_HEX = "02886e2f97ace46e55ba9dd7242579f2993b64e16ef3dcab95afd497333d8fa12f"
N_POINT_HEX = "03d8bbd6c639c62937b04d997f38c3770719c629d7014d49a24b4f98baa1292b49"


def derive_w0_w1(credential: str, salt: bytes, iterations: int) -> tuple[int, int]:
    raise NotImplementedError


def tpap_context(user_random: bytes, dev_random: bytes) -> bytes:
    raise NotImplementedError


@dataclass(frozen=True, repr=False)
class Spake2pKeys:
    share_p: bytes
    share_v: bytes
    transcript: bytes
    k_main: bytes
    k_confirm_p: bytes
    k_confirm_v: bytes
    confirm_p: bytes
    confirm_v: bytes
    shared_key: bytes


class Spake2pProver:
    def __init__(self, w0: int, w1: int, context: bytes, *, id_prover: bytes = b"", id_verifier: bytes = b"",
                 x: int | None = None):
        raise NotImplementedError

    @property
    def share(self) -> bytes:
        raise NotImplementedError

    def finish(self, peer_share: bytes) -> Spake2pKeys:
        raise NotImplementedError


def md5_hex(text: str) -> str:
    raise NotImplementedError


def sha256_hex_upper(text: str) -> str:
    raise NotImplementedError


def md5_crypt(key: str, prefix: str) -> str | None:
    raise NotImplementedError


def sha256_crypt(key: str, prefix: str) -> str:
    raise NotImplementedError


def apply_extra_crypt(credential: str, extra_crypt: Any, *, mac: str = "") -> str:
    raise NotImplementedError


def credential_for(variant: str, password: str) -> str:
    raise NotImplementedError


def camera_username(user_hash_type: int = 0, user: str = CAMERA_USER) -> str:
    raise NotImplementedError


class TpapCipher:
    def __init__(self, shared_key: bytes, encryption: str = "aes_128_ccm"):
        raise NotImplementedError

    def encrypt(self, plaintext: bytes, seq: int) -> bytes:
        raise NotImplementedError

    def decrypt(self, data: bytes, seq: int) -> bytes:
        raise NotImplementedError

    def seal(self, plaintext: bytes, seq: int) -> bytes:
        raise NotImplementedError

    def open(self, body: bytes) -> tuple[int, bytes]:
        raise NotImplementedError


class TpapRejected(TapoError):
    def __init__(self, code: int | None, stage: str):
        super().__init__(f"TPAP {stage} rejected (error {code})")
        self.code = code
        self.stage = stage


@dataclass(repr=False)
class TpapSession:
    stok: str
    start_seq: int
    cipher: TpapCipher
    variant: str
    expires_in: int | None = None


def tpap_requested(tpap: dict | None, encrypt_type: Any = None) -> bool:
    if isinstance(tpap, dict) and tpap.get("pake"):
        return True
    types = [encrypt_type] if isinstance(encrypt_type, (str, int)) else list(encrypt_type or [])
    return "4" in [str(t) for t in types]


class TpapAuthenticator:
    def __init__(self, post_login: Callable[[dict], dict], password: str, *, tpap: dict | None = None,
                 rng: Callable[[int], bytes] = os.urandom, sleep: Callable[[float], None] | None = None):
        raise NotImplementedError

    @property
    def good_variant(self) -> str | None:
        raise NotImplementedError

    def login(self) -> TpapSession:
        raise NotImplementedError
