# Tapo Desktop for Windows

Unofficial native Windows client for TP-Link Tapo cameras on your local network. It talks
directly to the camera; no cloud relay is used.

| Feature | Camera interface |
|---|---|
| Live view, 1x1 / 2x2 / 3x3 grid, double-click a tile for full-screen HD | RTSP :554 (`/stream2` in the grid, `/stream1` full screen; `/stream6`/`/stream7` for dual-lens) |
| Pan/tilt pad, W/A/S/D or arrow keys, presets | HTTPS :443 (`motorMove`, `singalMove`, presets) |
| Privacy mode, night vision (auto/IR on/off), status LED, siren, motion detection | HTTPS :443 |
| SD-card recordings: days with video, clip list, download to MP4 | HTTPS :443 search + media port :8800 |
| Motion notifications (Windows toast via the tray icon) | ONVIF :2020 PullPoint events |
| Runs in the system tray, auto-reconnects after Wi-Fi drops | |

## Install
Run `dist\TapoDesktop-0.1.0-setup.exe` (per-user install, no admin rights), or unzip
`dist\TapoDesktop-0.1.0-win64.zip` and run `TapoDesktop\TapoDesktop.exe`. FFmpeg is bundled.

## Camera setup
1. In the Tapo phone app: camera > Settings > Advanced settings > **Camera account**: create a user
   name and password. RTSP and ONVIF use this account.
2. In Tapo Desktop: **Add**, then enter the camera IP, your **TP-Link cloud account password** (used by the
   camera's local HTTPS control API with user `admin`), and the camera account from step 1.
3. Give the camera a fixed IP (DHCP reservation in the router).

Passwords are stored with Windows DPAPI (`%APPDATA%\TapoDesktop\vault.json`; readable only by your
Windows account). The camera's TLS certificate is pinned on first contact. If the certificate later changes,
the app refuses to connect.

Firmware limits to keep in mind: a camera serves at most 2 RTSP streams at once (the app enforces this per camera).
Some features exclude each other on the camera (SD recording, Tapo Care and NVR/RTSP/ONVIF), so check that RTSP/ONVIF works if streams fail.

## Development
```powershell
python -m venv .venv; .venv\Scripts\pip install -e .[dev]
.venv\Scripts\python -m tapo_desktop            # run from source
powershell -File tools/run.ps1 verify-specs     # Gate 1: protocol spec evidence check
powershell -File tools/run.ps1 test-tdd         # Gate 2: unit + mock-camera + chaos + UI tests
powershell -File tools/run.ps1 test-soak        # Gate 3: 30-min 4-camera soak (TAPO_SOAK_SECONDS)
powershell -File tools/run.ps1 test-hardware -Ip 192.168.x.y   # Gate 3: live camera (see tests/hardware)
powershell -File tools/run.ps1 smoke            # real app stack vs 2 mock cameras + screenshot
powershell -File tools/run.ps1 build-windows    # Gate 4: PyInstaller + Inno Setup + audit + selftest
```
The npm aliases in `package.json` (`npm run test:tdd-all`, and so on) call the same tasks.

Layout: `src/tapo_desktop/core` (auth, control_client, media_engine, vod, onvif_service),
`services` (controller and camera service), `ui` (PyQt6). `tests/fixtures/mock_tapo_server` is a stateful
mock camera (HTTPS, RTSP, ONVIF, :8800) with a chaos TCP proxy. The interface contracts are in `docs/`.
The protocol spec is in `specs/tapo_protocol_spec.json`; it was extracted from the Tapo Android app
3.22.806 with JADX/REA. Every item cites its evidence. The release build contains no APK material
(`tools/audit_release.py`).
