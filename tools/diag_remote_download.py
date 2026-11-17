"""VIEW-ONLY probe of the remote SD-card download path [spec:remote.relay.playback_download.*] on one
remote camera, with the production code: finds a short clip of today/yesterday through the cloud
passthrough (RemoteControlClient, -71103 fallback included), then downloads it with
RemoteRecordingDownloader (download slot streamType 3 first, playback stream fallback) to a file in
%TEMP%. Changes nothing on the camera. Prints no tokens.
Usage: python tools/diag_remote_download.py [name-substring] [max-clip-seconds]"""
from __future__ import annotations

import json
import logging
import sys
import tempfile
import time
from datetime import date, timedelta
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent / "src"))
from tapo_desktop.core.auth.vault import CredentialVault  # noqa: E402
from tapo_desktop.core.cloud import client as C  # noqa: E402
from tapo_desktop.core.cloud.passthrough import CloudPassthrough, RemoteControlClient  # noqa: E402
from tapo_desktop.core.cloud.relay import RelayClient  # noqa: E402
from tapo_desktop.core.cloud.relay_vod import RemoteRecordingDownloader  # noqa: E402
from tapo_desktop.models import ACCOUNT_VAULT_ID, CredentialKind  # noqa: E402
from tapo_desktop.services.config_store import ConfigStore, default_data_dir  # noqa: E402


def main(pattern: str, max_seconds: int) -> int:
    logging.basicConfig(level=logging.INFO, format="%(levelname)s %(name)s: %(message)s")
    data = default_data_dir()
    acct = CredentialVault(data / "vault.json").get(ACCOUNT_VAULT_ID, CredentialKind.CLOUD)
    term = ConfigStore(data / "config.json").get_setting("terminal_uuid")
    cloud = C.TapoCloudClient(terminal_uuid=term)
    cloud.login(acct.username, acct.password)
    devices = {d.device_id: d for d in cloud.list_cameras()}
    cloud.local_access_tokens()
    online = cloud.thing_online
    pick = next((d for d in devices.values() if pattern.lower() in d.alias.lower() and online.get(d.device_id)), None)
    if pick is None:
        print(json.dumps({"error": f"no online camera matching {pattern!r}"}))
        return 1
    thing = cloud.things[pick.device_id]
    remote = RemoteControlClient(CloudPassthrough(cloud), thing.thing_name, thing.app_server_url,
                                 player_id=str(term).lower())
    out: dict = {"camera": pick.alias, "model": pick.model, "role": thing.role}
    clips = []
    for back in (0, 1):
        clips = [c for c in remote.recordings_for_day(date.today() - timedelta(days=back))
                 if 0 < c.duration_seconds <= max_seconds]
        if clips:
            break
    out["clip_search_form"] = remote._clip_search_form
    if not clips:
        out["error"] = f"no clip <= {max_seconds} s today/yesterday"
        print(json.dumps(out, indent=2, ensure_ascii=False))
        return 1
    clip = min(clips, key=lambda c: c.duration_seconds)
    target = Path(tempfile.gettempdir()) / f"tapo_remote_{pick.device_id[-6:]}_{int(clip.start.timestamp())}.mp4"
    relay = RelayClient(cloud, terminal_uuid=term)
    dl = RemoteRecordingDownloader(relay, pick.device_id, model=pick.model)
    started = time.monotonic()
    try:
        dl.download(clip, target)
        out.update({"ok": True, "mode": dl.last_mode, "bytes": target.stat().st_size, "file": str(target)})
    except Exception as exc:  # report, never re-raise with secrets (core errors carry none)
        out.update({"ok": False, "mode": dl.last_mode, "error": f"{type(exc).__name__}: {exc}"})
    out.update({"clip_seconds": clip.duration_seconds, "elapsed_s": round(time.monotonic() - started, 1)})
    print(json.dumps(out, indent=2, ensure_ascii=False))
    return 0 if out.get("ok") else 1


if __name__ == "__main__":
    sys.exit(main(sys.argv[1] if len(sys.argv) > 1 else "Remote Camera 1", int(sys.argv[2]) if len(sys.argv) > 2 else 90))
