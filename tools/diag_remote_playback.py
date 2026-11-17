"""VIEW-ONLY probe of remote SD-card playback [spec:remote.client_recipe.playback] on one remote
camera: list today's/yesterday's clips via cloud passthrough, open a relay playback slot (streamType 1),
play ~15 s of one clip, count TS bytes, stop. Changes nothing. Prints no tokens.
Usage: python tools/diag_remote_playback.py [name-substring]"""
from __future__ import annotations

import json
import sys
import time
import uuid
from datetime import datetime, timedelta, timezone
from pathlib import Path
from urllib.parse import urlsplit

sys.path.insert(0, str(Path(__file__).resolve().parent.parent / "src"))
from tapo_desktop.core.auth.vault import CredentialVault  # noqa: E402
from tapo_desktop.core.cloud import client as C  # noqa: E402
from tapo_desktop.core.cloud.relay import RelayClient  # noqa: E402
from tapo_desktop.core.vod.session import MultipartReader, build_ack_part, encode_client_part  # noqa: E402
from tapo_desktop.models import ACCOUNT_VAULT_ID, CredentialKind  # noqa: E402
from tapo_desktop.services.config_store import ConfigStore, default_data_dir  # noqa: E402


def passthrough(cl, thing: dict, reqs: list[dict]) -> list[dict]:
    base = urlsplit(thing.get("appServerUrlV2") or thing.get("appServerUrl"))
    body = {"serviceId": "passthrough", "inputParams": {"requestData": {
        "method": "multipleRequest", "params": {"requests": reqs}}}}
    status, raw = cl._http_request("POST", base.scheme, base.hostname, base.port,
                                   f"{base.path.rstrip('/')}/v1/things/{thing['thingName']}/services-sync",
                                   json.dumps(body, separators=(",", ":")).encode(),
                                   {**cl._iot_headers(), "Content-Type": "application/json;charset=UTF-8"}, "/sync")
    op = json.loads(raw).get("outputParams") or {}
    rd = op.get("responseData", op)
    return ((rd or {}).get("result") or {}).get("responses") or []


def main(pattern: str) -> int:
    data = default_data_dir()
    acct = CredentialVault(data / "vault.json").get(ACCOUNT_VAULT_ID, CredentialKind.CLOUD)
    term = ConfigStore(data / "config.json").get_setting("terminal_uuid")
    cl = C.TapoCloudClient(terminal_uuid=term)
    cl.login(acct.username, acct.password)
    things: list[dict] = []
    orig = C.TapoCloudClient._iot_json
    C.TapoCloudClient._iot_json = staticmethod(lambda s, r: (lambda b: (things.extend(
        x for x in b.get("data", []) if isinstance(x, dict)), b)[1])(orig(s, r)))
    cl.local_access_tokens()
    C.TapoCloudClient._iot_json = staticmethod(orig)
    thing = next(t for t in things if pattern.lower() in C.decode_alias(t.get("nickname") or "").lower()
                 and cl.thing_online.get(t["thingName"]))
    out: dict = {"camera": C.decode_alias(thing["nickname"])}
    clips = []
    for back in (0, 1):
        day = (datetime.now().astimezone() - timedelta(days=back)).replace(hour=0, minute=0, second=0, microsecond=0)
        start, end = int(day.timestamp()), int(day.timestamp()) + 86399
        for key, val in (("id", 1), ("player_id", uuid.uuid4().hex)):
            res = passthrough(cl, thing, [{"method": "searchVideoWithUTC", "params": {"playback": {
                "search_video_with_utc": {"channel": 0, "start_time": start, "end_time": end,
                                          "start_index": 0, "end_index": 49, key: val}}}}])
            r = res[0] if res else {}
            out.setdefault("search", []).append({"day_back": back, "key": key, "error_code": r.get("error_code")})
            items = ((r.get("result") or {}).get("playback") or {}).get("search_video_results") or []
            for it in items:
                v = next(iter(it.values())) if isinstance(it, dict) and it else {}
                if isinstance(v, dict) and v.get("startTime"):
                    clips.append((int(v["startTime"]), int(v["endTime"])))
            if r.get("error_code") == 0:
                break
        if clips:
            break
    out["clips_found"] = len(clips)
    if not clips:
        print(json.dumps(out, indent=2, ensure_ascii=False))
        return 1
    st, en = clips[len(clips) // 2]
    relay = RelayClient(cl, terminal_uuid=term)
    orig_post = relay._post_json

    def post_playback(base, path, body, headers):
        body = {k: v for k, v in body.items() if k not in ("resolution", "preConnection")}
        body.update({"streamType": 1, "channelId": "0", "customParams": "{}"})
        return orig_post(base, path, body, headers)

    relay._post_json = post_playback
    slot = relay.request_slot(thing["thingName"], "SMART.IPCAMERA", thing.get("model", ""), "VGA")
    out["relay_path_type"] = [q for q in urlsplit("https://" + slot.relay_path if not slot.relay_path.startswith("/")
                                                  else "https://x" + slot.relay_path).query.split("&") if q.startswith("type=")]
    sock = relay.open_tunnel(slot)
    sock.settimeout(20)
    msg = {"type": "request", "seq": 1, "params": {"method": "get", "playback": {
        "client_id": 1, "channels": [0], "scale": "1/1", "start_time": str(st), "end_time": str(en),
        "event_type": [1, 2]}}}
    sock.sendall(encode_client_part(json.dumps(msg, separators=(",", ":")).encode()))
    reader, session_id, ts_bytes, media, notes, t0 = MultipartReader(), None, 0, 0, [], time.monotonic()
    try:
        while time.monotonic() - t0 < 15:
            chunk = sock.recv(65536)
            if not chunk:
                notes.append("tunnel closed")
                break
            for p in reader.feed(chunk):
                ctype = p.headers.get("Content-Type", "")
                if "json" in ctype:
                    j = json.loads(p.body or b"{}")
                    sid = (j.get("params") or {}).get("session_id") or p.headers.get("X-Session-Id")
                    session_id = session_id or sid
                    notes.append(json.dumps(j)[:160])
                elif "mp2t" in ctype:
                    media += 1
                    ts_bytes += len(p.body)
                    if media % 25 == 0 and session_id:
                        sock.sendall(build_ack_part(int(p.headers.get("X-Data-Sequence", media)), str(session_id)))
    finally:
        try:
            sock.sendall(encode_client_part(json.dumps({"type": "request", "seq": 2, "params": {
                "method": "do", "stop": "null"}}, separators=(",", ":")).encode()))
        finally:
            sock.close()
    out.update({"clip_seconds": en - st, "media_parts": media, "ts_bytes": ts_bytes, "json_parts": notes[:4],
                "first_ts_ok": media > 0})
    print(json.dumps(out, indent=2, ensure_ascii=False))
    return 0 if media else 1


if __name__ == "__main__":
    sys.exit(main(sys.argv[1] if len(sys.argv) > 1 else "Remote Camera 1"))
