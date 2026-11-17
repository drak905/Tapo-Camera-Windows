"""One controlled TPAP login to ONE real camera using only the cloud local access token
(exactly one pake_share; the account password is not offered). Then reads device info and
starts a 10 s :8800 live view. Usage: python tools/diag_camera_login.py <camera-ip>"""
from __future__ import annotations

import hashlib
import json
import sys
import threading
import time
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent / "src"))
from tapo_desktop.core.auth.vault import CredentialVault  # noqa: E402
from tapo_desktop.core.cloud.client import TapoCloudClient  # noqa: E402
from tapo_desktop.core.control_client.client import TapoControlClient  # noqa: E402
from tapo_desktop.core.discovery.tdp import discover  # noqa: E402
from tapo_desktop.core.media_engine.local_stream import LocalStreamSupervisor  # noqa: E402
from tapo_desktop.core.media_engine.supervisor import StreamState  # noqa: E402
from tapo_desktop.models import ACCOUNT_VAULT_ID, CredentialKind  # noqa: E402
from tapo_desktop.services.config_store import ConfigStore, default_data_dir  # noqa: E402


def main(ip: str) -> int:
    data = default_data_dir()
    vault = CredentialVault(data / "vault.json")
    acct = vault.get(ACCOUNT_VAULT_ID, CredentialKind.CLOUD)
    cloud = TapoCloudClient(terminal_uuid=ConfigStore(data / "config.json").get_setting("terminal_uuid"))
    cloud.login(acct.username, acct.password)
    tokens = cloud.local_access_tokens()
    lan = {d.ip: d for d in discover(timeout=3)}
    hit = lan[ip]
    dev = next(d for d in cloud.list_cameras()
               if hashlib.md5(d.device_id.encode()).hexdigest() == hit.device_id_md5.lower())
    lat = tokens[dev.device_id]
    res: dict = {"camera": dev.alias, "ip": ip}
    tpap = (hit.raw.get("result", hit.raw) or {}).get("tpap")
    c = TapoControlClient(ip, "", tpap=tpap, encrypt_type=["4"], local_token=lat, timeout=10)
    try:
        info = c.get_device_info()
        res["login"] = f"ok ({c.protocol})"
        res["device"] = {"model": info.model, "alias": info.alias, "fw": info.firmware}
        res["privacy"] = c.get_privacy()
    except Exception as exc:  # report precisely, never retry
        res["login"] = f"FAILED {type(exc).__name__}: {exc}"
        print(json.dumps(res, indent=2, ensure_ascii=False))
        return 1
    finally:
        c.close()

    frames: list[float] = []
    states: list[str] = []
    playing = threading.Event()

    def on_state(state, reason):
        states.append(f"{state.name}{': ' + reason if reason else ''}")
        if state is StreamState.PLAYING:
            playing.set()

    sup = LocalStreamSupervisor(ip, acct.password, 640, 360, resolution="VGA", local_token=lat,
                                on_frame=lambda f: frames.append(f.timestamp), on_state=on_state,
                                max_retries=1)
    sup.start()
    try:
        playing.wait(15)
        time.sleep(10)
    finally:
        sup.stop()
    res["live_view"] = {"states": states[:6], "frames": len(frames),
                        "fps": round((len(frames) - 1) / (frames[-1] - frames[0]), 1) if len(frames) > 2 else 0,
                        "decoder": sup.stats().get("decoder")}
    print(json.dumps(res, indent=2, ensure_ascii=False))
    return 0 if frames else 1


if __name__ == "__main__":
    sys.exit(main(sys.argv[1]))
