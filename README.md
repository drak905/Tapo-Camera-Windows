# Tapo Desktop for Windows

Unofficial native Windows client for TP-Link Tapo cameras. Sign in with your TP-Link ID like in the
phone app. Video and control go directly to the camera on your network.

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

## Getting started (same as the Tapo phone app)
1. Start Tapo Desktop and **sign in with your TP-Link ID** (the email and password you use in the Tapo app).
   If your account has 2-step verification, enter the code the dialog asks for.
2. Your cameras appear with the names from the Tapo app. The app finds them on your home network
   automatically and starts live video in the grid. You don't need a "Camera account" or RTSP setup.
3. Click a camera to use pan/tilt, presets, privacy mode, night vision, LED, siren, motion detection
   and snapshots, or open **Playback** for SD-card recordings. Double-click a tile for full screen.
4. The account button (top bar) has **Refresh cameras** and **Sign out**. The next start signs you in
   automatically.

Notes
- Live view and control work on the same network as the cameras. Remote viewing through the TP-Link
  cloud (when you are away from home) is not supported yet.
- Cameras on newer firmware (encrypt_type 4, such as the C200 on 1.5.x) use TP-Link's newer login
  (SPAKE2+/TPAP). The app supports it. A wrong password is not retried automatically, so the camera
  won't lock you out.
- Your password is stored with Windows DPAPI (%APPDATA%\TapoDesktop\vault.json), readable only by your
  Windows account. **Add** (by IP) is still available for cameras that aren't in your account.
- If live view or SD downloads fail, check that no local web filter (for example AdGuard) intercepts
  traffic from TapoDesktop.exe / fmpeg.exe. The camera media port 8800 uses plain HTTP.
## Development
Agents and new contributors: start with [AGENTS.md](AGENTS.md) (handover: state, rules, protocol facts, open items).

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
