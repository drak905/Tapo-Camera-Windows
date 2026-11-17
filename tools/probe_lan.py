"""Credential-free probe of real Tapo cameras on the LAN.

* TDP discovery (UDP 20002/20004)
* HTTPS :443 secure-login step 1 only (cnonce -> -40413 challenge with nonce/device_confirm); no password is sent
* :8800 POST /stream without auth -> 401 Digest challenge (algorithm / encrypt_type); no password is sent
Nothing is changed on the cameras and no failed-login counter is incremented.
"""
from __future__ import annotations

import http.client
import json
import socket
import ssl
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent / "src"))
from tapo_desktop.core.auth.crypto import generate_cnonce  # noqa: E402
from tapo_desktop.core.discovery.tdp import discover  # noqa: E402


def https_step1(ip: str, port: int) -> dict:
    ctx = ssl.create_default_context()
    ctx.check_hostname = False
    ctx.verify_mode = ssl.CERT_NONE
    body = json.dumps({"method": "login", "params": {"cnonce": generate_cnonce(), "encrypt_type": "3",
                                                     "username": "admin"}})
    c = http.client.HTTPSConnection(ip, port, timeout=6, context=ctx)
    try:
        c.request("POST", "/", body, {"Content-Type": "application/json", "User-Agent": "Tapo CameraClient Android",
                                      "requestByApp": "true", "Referer": f"https://{ip}:{port}"})
        r = c.getresponse()
        data = json.loads(r.read() or b"{}")
        res = data.get("result", {}).get("data", {})
        return {"http": r.status, "error_code": data.get("error_code"), "has_nonce": bool(res.get("nonce")),
                "has_device_confirm": bool(res.get("device_confirm")), "code": res.get("code"),
                "encrypt_type": res.get("encrypt_type"), "tls": c.sock.version() if c.sock else None}
    finally:
        c.close()


def media_challenge(ip: str, port: int = 8800) -> dict:
    s = socket.create_connection((ip, port), timeout=6)
    try:
        req = (f"POST /stream HTTP/1.1\r\nContent-Type: multipart/mixed; boundary=--client-stream-boundary--\r\n"
               f"Host: {ip}:{port}\r\nConnection: Keep-Alive\r\nContent-Length: 0\r\nX-Key-Exchange: 1\r\n\r\n")
        s.sendall(req.encode())
        buf = b""
        while b"\r\n\r\n" not in buf and len(buf) < 8192:
            chunk = s.recv(4096)
            if not chunk:
                break
            buf += chunk
        head = buf.split(b"\r\n\r\n", 1)[0].decode("latin-1").split("\r\n")
        hdrs = {h.split(":", 1)[0].lower(): h.split(":", 1)[1].strip() for h in head[1:] if ":" in h}
        www = hdrs.get("www-authenticate", "")
        keys = {kv.split("=", 1)[0].strip(): kv.split("=", 1)[1].strip('" ') for kv in www[7:].split(",") if "=" in kv}
        keys.pop("nonce", None)
        keys.pop("opaque", None)
        return {"status_line": head[0], "digest_params": keys}
    finally:
        s.close()


def main() -> int:
    devs = discover(timeout=3)
    out = []
    for d in devs:
        row = {"ip": d.ip, "model": d.model, "type": d.device_type, "http_port": d.http_port,
               "md5_id_known": bool(d.device_id_md5)}
        for name, fn in (("https_step1", lambda: https_step1(d.ip, d.http_port or 443)),
                         ("media_8800", lambda: media_challenge(d.ip))):
            try:
                row[name] = fn()
            except Exception as exc:  # report, don't crash
                row[name] = {"error": f"{type(exc).__name__}: {exc}"}
        out.append(row)
    print(json.dumps(out, indent=2))
    return 0


if __name__ == "__main__":
    sys.exit(main())
