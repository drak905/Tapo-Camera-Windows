"""Diagnostic (cloud only, no camera contact): does the TP-Link cloud issue a local access token
for the cameras found on this LAN? Uses the signed-in account stored in the app's DPAPI vault.
Prints no secrets (only yes/no and token length)."""
from __future__ import annotations

import hashlib
import json
import os
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent / "src"))
from tapo_desktop.core.auth.vault import CredentialVault  # noqa: E402
from tapo_desktop.core.cloud.client import TapoCloudClient  # noqa: E402
from tapo_desktop.core.discovery.tdp import discover  # noqa: E402
from tapo_desktop.models import ACCOUNT_VAULT_ID, CredentialKind  # noqa: E402
from tapo_desktop.services.config_store import ConfigStore, default_data_dir  # noqa: E402


def main() -> int:
    data = default_data_dir()
    store = ConfigStore(data / "config.json")
    vault = CredentialVault(data / "vault.json")
    acct = vault.get(ACCOUNT_VAULT_ID, CredentialKind.CLOUD)
    if acct is None:
        print("no stored account (sign in with 'Keep me signed in' first)")
        return 2
    uuid = store.get_setting("terminal_uuid")
    cloud = TapoCloudClient(terminal_uuid=uuid)
    cloud.login(acct.username, acct.password)
    devices = cloud.list_cameras()
    tokens = cloud.local_access_tokens()
    lan = discover(timeout=3)
    lan_md5 = {d.device_id_md5.lower(): d for d in lan if d.device_id_md5}
    out = {"cloud_cameras": len(devices), "cloud_tokens": len(tokens), "lan_cameras": []}
    for dev in devices:
        hit = lan_md5.get(hashlib.md5(dev.device_id.encode()).hexdigest())
        if hit is None:
            continue
        tok = tokens.get(dev.device_id)
        out["lan_cameras"].append({"name": dev.alias, "ip": hit.ip, "model": dev.model,
                                   "role": dev.role, "has_local_token": tok is not None,
                                   "token_len": len(tok) if tok else 0,
                                   "tdp_user_hash_type": (hit.raw.get("result", hit.raw) or {}).get("tpap", {}).get("user_hash_type")})
    print(json.dumps(out, indent=2, ensure_ascii=False))
    return 0


if __name__ == "__main__":
    sys.exit(main())
