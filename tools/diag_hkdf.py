"""Offline check of the :8800 HKDF key derivation against a real camera packet: the HMAC header
proves which derivation is right. One media session is opened; no extra login attempts.
Usage: python tools/diag_hkdf.py <ip>"""
from __future__ import annotations

import base64
import binascii
import hashlib
import hmac
import json
import sys
from pathlib import Path

from cryptography.hazmat.primitives import hashes
from cryptography.hazmat.primitives.ciphers import Cipher, algorithms, modes
from cryptography.hazmat.primitives.kdf.hkdf import HKDF

sys.path.insert(0, str(Path(__file__).resolve().parent.parent / "src"))
from tapo_desktop.core.auth.vault import CredentialVault  # noqa: E402
from tapo_desktop.core.cloud.client import TapoCloudClient  # noqa: E402
from tapo_desktop.core.discovery.tdp import discover  # noqa: E402
from tapo_desktop.core.vod import session as S  # noqa: E402
from tapo_desktop.models import ACCOUNT_VAULT_ID, CredentialKind  # noqa: E402
from tapo_desktop.services.config_store import ConfigStore, default_data_dir  # noqa: E402


def hkdf(ikm: bytes, salt: bytes, info: bytes) -> bytes:
    return HKDF(algorithm=hashes.SHA256(), length=16, salt=salt, info=info).derive(ikm)


def main(ip: str) -> int:
    data = default_data_dir()
    acct = CredentialVault(data / "vault.json").get(ACCOUNT_VAULT_ID, CredentialKind.CLOUD)
    cloud = TapoCloudClient(terminal_uuid=ConfigStore(data / "config.json").get_setting("terminal_uuid"))
    cloud.login(acct.username, acct.password)
    tokens = cloud.local_access_tokens()
    hit = {d.ip: d for d in discover(timeout=3)}[ip]
    dev = next(d for d in cloud.list_cameras()
               if hashlib.md5(d.device_id.encode()).hexdigest() == hit.device_id_md5.lower())
    lat = tokens[dev.device_id]
    kex: dict = {}
    orig = S.MediaSession._setup_encryption

    def spy(self, response, challenge):
        kex.update(S.parse_key_exchange(response.headers.get("key-exchange", "")))
        return orig(self, response, challenge)

    S.MediaSession._setup_encryption = spy
    S.decrypt_part = lambda body, key, iv: body
    sess = S.MediaSession(ip, acct.password, local_token=lat, timeout=8)
    part = None
    try:
        sess.connect()
        sess.send_json({"type": "request", "params": {"method": "get", "preview": {
            "channels": [0], "resolutions": ["VGA"], "audio": ["default"]}}})
        for p in sess.parts():
            if p.headers.get("X-If-Encrypt") == "1":
                part = p
                break
    finally:
        sess.close()
    body = part.body
    want = part.headers["X-Data-Hmac"]
    iv = binascii.unhexlify(part.headers["X-Nonce"])
    secrets = {"SHA256U(LAT)": S.transform_password(lat, "3"), "LAT": lat,
               "SHA256U(pwd)": S.transform_password(acct.password, "3"), "MD5U(LAT)": S.transform_password(lat, None)}
    salts = {"utf8": kex["salt"].encode(), "hex": binascii.unhexlify(kex["salt"]) if len(kex["salt"]) % 2 == 0 else b""}
    found = []
    for sname, secret in secrets.items():
        for ikm_form in ("nonce:secret", "secret", "secret:nonce"):
            ikm = {"nonce:secret": f"{kex['nonce']}:{secret}", "secret": secret,
                   "secret:nonce": f"{secret}:{kex['nonce']}"}[ikm_form].encode()
            for saltname, salt in salts.items():
                mk = hkdf(ikm, salt, b"stream_hkdf_hmac_key")
                for mac_over in ("ciphertext",):
                    got = base64.b64encode(hmac.new(mk, body, hashlib.sha256).digest()).decode()
                    if hmac.compare_digest(got, want):
                        key = hkdf(ikm, salt, b"stream_hkdf_aes_key")
                        dec = Cipher(algorithms.AES(key), modes.CBC(iv)).decryptor()
                        plain = dec.update(body) + dec.finalize()
                        found.append({"secret": sname, "ikm": ikm_form, "salt": saltname,
                                      "pad_byte": plain[-1], "ts_sync_ok": plain[0] == 0x47})
    print(json.dumps({"kex_fields": sorted(kex), "body_len": len(body), "matches": found}, indent=2))
    return 0 if found else 1


if __name__ == "__main__":
    sys.exit(main(sys.argv[1]))
