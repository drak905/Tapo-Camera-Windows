"""READ-ONLY probe of remote control through the TP-Link cloud [spec:remote.client_recipe.control]:
sends only "get"/"search" commands via IoT services-sync passthrough to remote cameras (owned + shared).
Changes nothing. Prints no tokens. Usage: python tools/diag_remote_ctl.py"""
from __future__ import annotations

import json
import sys
from datetime import date, timedelta
from pathlib import Path
from urllib.parse import urlsplit

sys.path.insert(0, str(Path(__file__).resolve().parent.parent / "src"))
from tapo_desktop.core.auth.vault import CredentialVault  # noqa: E402
from tapo_desktop.core.cloud import client as C  # noqa: E402
from tapo_desktop.models import ACCOUNT_VAULT_ID, CredentialKind  # noqa: E402
from tapo_desktop.services.config_store import ConfigStore, default_data_dir  # noqa: E402

READS = [
    {"method": "getDeviceInfo", "params": {"device_info": {"name": ["basic_info"]}}},
    {"method": "getLensMaskConfig", "params": {"lens_mask": {"name": ["lens_mask_info"]}}},
    {"method": "getLedStatus", "params": {"led": {"name": ["config"]}}},
]


def main() -> int:
    data = default_data_dir()
    acct = CredentialVault(data / "vault.json").get(ACCOUNT_VAULT_ID, CredentialKind.CLOUD)
    cl = C.TapoCloudClient(terminal_uuid=ConfigStore(data / "config.json").get_setting("terminal_uuid"))
    cl.login(acct.username, acct.password)
    things: list[dict] = []
    orig = C.TapoCloudClient._iot_json

    def spy(status, raw):
        body = orig(status, raw)
        things.extend(x for x in body.get("data", []) if isinstance(x, dict))
        return body

    C.TapoCloudClient._iot_json = staticmethod(spy)
    cl.local_access_tokens()
    C.TapoCloudClient._iot_json = staticmethod(orig)
    online = cl.thing_online
    picks = []
    for role in (0, 1):
        for t in things:
            name = C.decode_alias(t.get("nickname") or "")
            if t.get("role") == role and online.get(t["thingName"]) and "Phòng" not in name and "CameraB" not in name:
                picks.append((t, name))
                break
    today = date.today()
    out = []
    for t, name in picks:
        base = urlsplit(t.get("appServerUrlV2") or t.get("appServerUrl"))
        reads = READS + [{"method": "searchDateWithVideo", "params": {"playback": {"search_year_utility": {
            "channel": [0], "start_date": (today - timedelta(days=7)).strftime("%Y%m%d"),
            "end_date": today.strftime("%Y%m%d")}}}}]
        body = {"serviceId": "passthrough", "inputParams": {"requestData": {
            "method": "multipleRequest", "params": {"requests": reads}}}}
        path = f"{base.path.rstrip('/')}/v1/things/{t['thingName']}/services-sync"
        status, raw = cl._http_request("POST", base.scheme, base.hostname, base.port, path,
                                       json.dumps(body, separators=(",", ":")).encode(), {
                                           **cl._iot_headers(), "Content-Type": "application/json;charset=UTF-8"},
                                       "/services-sync")
        try:
            reply = json.loads(raw or b"{}")
        except ValueError:
            reply = {"raw": raw[:200].decode("latin-1")}
        op = reply.get("outputParams") or (reply.get("result") or {}).get("outputParams") or {}
        rd = op.get("responseData", op)
        responses = ((rd or {}).get("result") or {}).get("responses") or []
        summary = []
        for r in responses:
            res = r.get("result") or {}
            brief = {"method": r.get("method"), "error_code": r.get("error_code")}
            if r.get("method") == "getDeviceInfo":
                bi = (res.get("device_info") or {}).get("basic_info") or {}
                brief["model"] = bi.get("device_model")
            elif r.get("method") == "getLensMaskConfig":
                brief["privacy"] = ((res.get("lens_mask") or {}).get("lens_mask_info") or {}).get("enabled")
            elif r.get("method") == "searchDateWithVideo":
                brief["days"] = len((res.get("playback") or {}).get("search_results") or [])
            summary.append(brief)
        out.append({"camera": name, "role": t.get("role"), "shareType": t.get("shareType"), "http": status,
                    "top_keys": sorted(reply), "reply_error": reply.get("error_code", reply.get("errorCode")),
                    "inner_error": (rd or {}).get("error_code"), "responses": summary})
    print(json.dumps(out, indent=2, ensure_ascii=False))
    return 0


if __name__ == "__main__":
    sys.exit(main())
