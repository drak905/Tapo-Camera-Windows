"""TCP reachability probe run before each ffmpeg spawn (private to the supervisor).

ffmpeg's own connect on Windows only gives up at its socket timeout (WSAPoll does not report a
refused connect), so probing in Python turns a 5 s hang into the OS's ~2 s refusal and avoids
spawning ffmpeg while the camera is down.
"""
from __future__ import annotations

import errno
import select
import socket
import threading
import time
from urllib.parse import urlsplit

__all__ = ["probe_address", "tcp_reachable"]

_POLL_S = 0.1
_DEFAULT_PORTS = {"rtsp": 554, "rtsps": 322}
_CONNECT_IN_PROGRESS = {errno.EINPROGRESS, errno.EWOULDBLOCK, errno.EALREADY, 10035, 10036, 10037}
_ERRNO_TEXT = {10061: "connection refused", 111: "connection refused", 10060: "connect timed out",
               10065: "no route to host", 10051: "network unreachable"}


def probe_address(url: str) -> tuple[str, int] | None:
    """(host, port) worth a TCP reachability probe before spawning ffmpeg, or None."""
    try:
        parts = urlsplit(url)
        port = parts.port or _DEFAULT_PORTS.get(parts.scheme.lower())
    except ValueError:
        return None
    if not parts.hostname or not port or parts.scheme.lower() not in _DEFAULT_PORTS:
        return None
    return parts.hostname, port


def tcp_reachable(host: str, port: int, timeout: float, abort: threading.Event) -> str | None:
    """Non-blocking TCP connect that ``abort`` can interrupt. Returns None or a failure reason."""
    try:
        infos = socket.getaddrinfo(host, port, type=socket.SOCK_STREAM)
    except OSError as exc:
        return f"cannot resolve camera address ({exc.strerror or type(exc).__name__})"
    family, socktype, proto, _, address = infos[0]
    deadline = time.monotonic() + timeout
    with socket.socket(family, socktype, proto) as sock:
        sock.setblocking(False)
        err = sock.connect_ex(address)
        if err == 0:
            return None
        if err not in _CONNECT_IN_PROGRESS:
            return f"camera unreachable ({_errno_text(err)})"
        while not abort.is_set():
            remaining = deadline - time.monotonic()
            if remaining <= 0:
                return "camera unreachable (connect timed out)"
            _, writable, failed = select.select([], [sock], [sock], min(_POLL_S, remaining))
            if writable or failed:
                err = sock.getsockopt(socket.SOL_SOCKET, socket.SO_ERROR)
                if err == 0:
                    return None
                return f"camera unreachable ({_errno_text(err)})"
    return "stopped"


def _errno_text(err: int) -> str:
    return _ERRNO_TEXT.get(err, f"socket error {err}")
