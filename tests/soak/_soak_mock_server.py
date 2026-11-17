"""Child process for tests/soak/test_endurance_grid.py: hosts N MockTapoCamera instances.

Running the mock cameras out of process keeps the soak's memory/handle measurements about OUR
process only. Protocol (one JSON line per reply on stdout):
    start            -> {"cameras": [{host, https_port, rtsp_port, media_port, rtsp_url, cloud_password}, ...]}
    "mutate <i> <n>" -> applies n DeviceShadow mutations to camera i -> {"ok": n}
    "stats"          -> {"rtsp_active_clients": [...], "rtsp_reject_count": [...]}
    "quit"           -> stops cameras and exits
Usage: python -m tests.soak._soak_mock_server <n_cameras>
"""
from __future__ import annotations

import json
import sys

from tests.fixtures.mock_tapo_server import MockTapoCamera

CLOUD_PASSWORD = "cloudpass"
CAM_USER = "camuser"
CAM_PASSWORD = "campass"


def _mutate(cam, n: int) -> None:
    s = cam.state
    for i in range(n):
        s.led = not s.led
        s.ptz_x = (s.ptz_x + 7) % 360
        s.ptz_y = (s.ptz_y + 3) % 90
        s.motion_sensitivity = (20, 60, 80)[i % 3]
        s.inf_type = ("auto", "on", "off")[i % 3]


def main() -> int:
    n = int(sys.argv[1]) if len(sys.argv) > 1 else 4
    cams = [
        MockTapoCamera(cloud_password=CLOUD_PASSWORD, camera_user=CAM_USER, camera_password=CAM_PASSWORD)
        for _ in range(n)
    ]
    for c in cams:
        c.start()
    try:
        info = [
            {"host": c.host, "https_port": c.https_port, "rtsp_port": c.rtsp_port, "media_port": c.media_port,
             "rtsp_url": c.rtsp_url("stream2"), "cloud_password": CLOUD_PASSWORD}
            for c in cams
        ]
        print(json.dumps({"cameras": info}), flush=True)
        for line in sys.stdin:
            cmd = line.split()
            if not cmd:
                continue
            if cmd[0] == "mutate":
                idx, count = int(cmd[1]), int(cmd[2])
                _mutate(cams[idx], count)
                print(json.dumps({"ok": count}), flush=True)
            elif cmd[0] == "stats":
                print(json.dumps({
                    "rtsp_active_clients": [c.rtsp_active_clients for c in cams],
                    "rtsp_reject_count": [c.rtsp_reject_count for c in cams],
                }), flush=True)
            elif cmd[0] == "quit":
                break
    finally:
        for c in cams:
            c.stop()
    return 0


if __name__ == "__main__":
    sys.exit(main())
