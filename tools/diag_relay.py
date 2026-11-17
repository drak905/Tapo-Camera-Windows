"""View-only feasibility probe of the TP-Link cloud relay for ONE remote camera [spec:remote.*]:
relay slot request, TLS tunnel, preview request, read a few parts (headers only + body sizes),
stop. Changes nothing on the camera. Prints no tokens. Usage: python tools/diag_relay.py [name-substring]"""
from __future__ import annotations

import http.client
import json
import random
import socket
import ssl
import string
import sys
import time
import uuid
from pathlib import Path
from urllib.parse import urlsplit

sys.path.insert(0, str(Path(__file__).resolve().parent.parent / "src"))
from tapo_desktop.core.auth.vault import CredentialVault  # noqa: E402
from tapo_desktop.core.cloud.client import TapoCloudClient, make_ssl_context  # noqa: E402
from tapo_desktop.core.vod.session import MultipartReader, encode_client_part  # noqa: E402
from tapo_desktop.models import ACCOUNT_VAULT_ID, CredentialKind  # noqa: E402
from tapo_desktop.services.config_store import ConfigStore, default_data_dir  # noqa: E402


def main(pattern: str) -> int:
    data = default_data_dir()
    acct = CredentialVault(data / "vault.json").get(ACCOUNT_VAULT_ID, CredentialKind.CLOUD)
    term = ConfigStore(data / "config.json").get_setting("terminal_uuid")
    cloud = TapoCloudClient(terminal_uuid=term)
    cloud.login(acct.username, acct.password)
    cams = cloud.list_cameras()
    online = (cloud.local_access_tokens(), cloud.thing_online)[1]
    cam = next(c for c in cams if pattern.lower() in c.alias.lower() and online.get(c.device_id))
    out: dict = {"camera": cam.alias, "model": cam.model}
    cipc = cloud.get_service_urls(["cipc.api.cloud"]).get("cipc.api.cloud") or "aps1-cipc-api.i.tplinkcloud.com"
    host = urlsplit(cipc if "//" in cipc else "https://" + cipc).hostname
    body = {"deviceId": cam.device_id, "dataTimeoutCount": 0,
            "cloudType": 2 if cam.raw.get("isSupportIoTCloud") or cam.raw.get("is_support_iot_cloud") else 1,
            "deviceType": cam.device_type, "playerId": term, "rootCaVer": "1", "preConnection": 0,
            "resolution": "VGA", "streamType": 0, "customParams": "{}", "trackId": str(uuid.uuid4())}
    ctx = make_ssl_context(True)
    for cloud_type in (2,):
        body["cloudType"] = cloud_type
        conn = http.client.HTTPSConnection(host, 443, timeout=15, context=ctx)
        conn.request("POST", "/v2/relay/request", json.dumps(body, separators=(",", ":")), {
            "Authorization": cloud.token, "Content-Type": "application/json; charset=UTF-8",
            "User-Agent": f"Tapo_APP/{cam.model}/3.22.806_Android/Android 14",
            "X-Request-Signature": "".join(random.choices(string.ascii_letters + string.digits, k=10)),
            "X-Client-Id": term, "X-Source": "tapo-app", "X-Brand": "TPLINK", "X-Ca-Type": "cloud-self"})
        r = conn.getresponse()
        reply = json.loads(r.read() or b"{}")
        conn.close()
        res = reply.get("result") or {}
        out[f"relay_request_cloudType{cloud_type}"] = {"http": r.status, "errorCode": reply.get("errorCode"),
                                                        "msg": reply.get("msg"), "result_fields": sorted(res)}
        if res.get("relayUrl"):
            break
    if not res.get("relayUrl"):
        print(json.dumps(out, indent=2, ensure_ascii=False))
        return 1
    url = urlsplit(res["relayUrl"] if "//" in res["relayUrl"] else "https://" + res["relayUrl"])
    target = f"{url.path}?{url.query}&retryTime=0"
    sock = socket.create_connection((res.get("relayIp") or url.hostname, url.port or 443), timeout=15)
    tls = ctx.wrap_socket(sock, server_hostname=url.hostname)
    hdrs = [("User-Agent", "Client=TP-Link_Tapo_Android Android 3.22.806/1.3"), ("Keep-Relay", "3600"),
            ("Accept", "*/*"), ("Content-Type", "multipart/mixed;boundary=--client-stream-boundary--"),
            ("Host", url.hostname), ("Content-Length", "9223372036854775807"), ("X-token", res["relayToken"]),
            ("X-Client-Model", "Windows"), ("X-Client-UUID", term), ("X-Track-Id", body["trackId"]),
            ("X-Redirect-Times", "0"), ("X-Pull-Mode", "2")]
    if res.get("elbCookie"):
        hdrs.append(("Cookie", res["elbCookie"]))
    hdrs += [("X-Version", "2.0"), ("X-Arrive-Latency", "20")]
    head = f"POST {target} HTTP/1.1\r\n" + "".join(f"{k}: {v}\r\n" for k, v in hdrs)
    out["relay_target_shape"] = target.split("?")[0] + "?" + "&".join(q.split("=")[0] for q in target.split("?", 1)[1].split("&"))
    tls.sendall((head + "\r\n").encode())
    buf = b""
    while b"\r\n\r\n" not in buf:
        chunk = tls.recv(4096)
        if not chunk:
            break
        buf += chunk
    status_line, _, rest = buf.partition(b"\r\n")
    hdr_blob, _, body_rest = rest.partition(b"\r\n\r\n")
    out["tunnel_status"] = status_line.decode("latin-1")
    out["tunnel_headers"] = sorted(h.split(b":")[0].decode().lower() for h in hdr_blob.split(b"\r\n") if b":" in h)
    if b" 200" not in status_line:
        out["tunnel_body"] = body_rest[:300].decode("latin-1", "replace")
        print(json.dumps(out, indent=2, ensure_ascii=False))
        return 1
    preview = {"type": "request", "seq": 1, "params": {"method": "get", "preview": {
        "channels": [0], "resolutions": ["VGA"], "audio": ["default"]}}}
    tls.sendall(encode_client_part(json.dumps(preview, separators=(",", ":")).encode()))
    reader = MultipartReader()
    parts, t0 = [], time.monotonic()
    data_in = body_rest
    while len(parts) < 30 and time.monotonic() - t0 < 20:
        for p in reader.feed(data_in):
            parts.append(p)
        data_in = tls.recv(65536)
        if not data_in:
            break
    media = [p for p in parts if "mp2t" in p.headers.get("Content-Type", "")]
    out["parts"] = len(parts)
    out["first_json"] = [p.body[:200].decode("utf-8", "replace") for p in parts if "json" in p.headers.get("Content-Type", "")][:2]
    out["media_parts"] = len(media)
    if media:
        m = media[0]
        out["media_headers"] = sorted(m.headers)
        out["media_encrypted"] = m.headers.get("X-If-Encrypt")
        out["ts_sync_byte_ok"] = m.body[:1] == b"\x47"
        out["media_bytes"] = sum(len(p.body) for p in media)
    stop = {"type": "request", "seq": 2, "params": {"method": "do", "stop": "null"}}
    try:
        tls.sendall(encode_client_part(json.dumps(stop, separators=(",", ":")).encode()))
    finally:
        tls.close()
    print(json.dumps(out, indent=2, ensure_ascii=False))
    return 0 if media else 1


if __name__ == "__main__":
    sys.exit(main(sys.argv[1] if len(sys.argv) > 1 else "Remote"))
