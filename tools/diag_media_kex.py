"""Show the :8800 Key-Exchange parameters (names, and values for non-secret fields) and the first
media part headers of a real camera, using the cloud LAT. Usage: python tools/diag_media_kex.py <ip>"""
from __future__ import annotations

import hashlib
import json
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent / "src"))
from tapo_desktop.core.auth.vault import CredentialVault  # noqa: E402
from tapo_desktop.core.cloud.client import TapoCloudClient  # noqa: E402
from tapo_desktop.core.discovery.tdp import discover  # noqa: E402
from tapo_desktop.core.vod import session as S  # noqa: E402
from tapo_desktop.models import ACCOUNT_VAULT_ID, CredentialKind  # noqa: E402
from tapo_desktop.services.config_store import ConfigStore, default_data_dir  # noqa: E402

SAFE = {"username", "cipher", "padding", "algorithm", "encrypt_type", "qop", "realm"}


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

    captured: dict = {}
    orig = S.MediaSession._setup_encryption

    def spy(self, response, challenge):
        kex = S.parse_key_exchange(response.headers.get("key-exchange", ""))
        captured["challenge"] = {k: (v if k in SAFE else f"<{len(v)} chars>") for k, v in challenge.items()}
        captured["key_exchange"] = {k: (v if k in SAFE else f"<{len(v)} chars>") for k, v in kex.items()}
        captured["response_headers"] = sorted(response.headers)
        captured["secret_used"] = "LAT" if self._secret == lat else "password"
        return orig(self, response, challenge)

    S.MediaSession._setup_encryption = spy
    sess = S.MediaSession(ip, acct.password, local_token=lat, timeout=8)
    try:
        sess.connect()
        sess.send_json({"type": "request", "params": {"method": "get", "preview": {
            "channels": [0], "resolutions": ["VGA"], "audio": ["default"]}}})
        raw_parts = []
        orig_decrypt = S.decrypt_part
        S.decrypt_part = lambda body, key, iv: body  # capture raw
        try:
            for i, part in enumerate(sess.parts()):
                raw_parts.append({"headers": {k: (v if k.lower() not in ("x-nonce", "x-data-hmac") else f"<{len(v)} chars>")
                                              for k, v in part.headers.items()},
                                  "body_len": len(part.body), "body_len_mod16": len(part.body) % 16})
                if i >= 4:
                    break
        finally:
            S.decrypt_part = orig_decrypt
        captured["first_parts"] = raw_parts
    finally:
        sess.close()
    print(json.dumps(captured, indent=2))
    return 0


if __name__ == "__main__":
    sys.exit(main(sys.argv[1]))
