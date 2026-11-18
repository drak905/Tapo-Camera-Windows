# AGENTS.md: handover for coding agents (Kiro, Claude Code, Codex, Gemini CLI, ...)

Read this file first. It is the single source of truth for the project's state, how to work on it
safely, and what real hardware taught us. `CLAUDE.md` and `GEMINI.md` only point here.

## 1. What this is
An unofficial native Windows desktop client for TP-Link Tapo cameras that behaves like the Tapo phone app:
- sign in with a TP-Link ID (with MFA), and the cameras appear with their cloud names
- **cameras on the LAN:** found by TDP broadcast and controlled over local HTTPS :443 (secure v3, legacy MD5
  or the new **TPAP / SPAKE2+** login); live view and SD playback/download over the media port :8800
- **cameras elsewhere and shared cameras:** live view, SD playback and downloads through the **TP-Link cloud
  relay**; controls through **cloud passthrough** (IoT `services-sync`)
- 1x1-5x5 grid where every visible tile streams, user-assignable grid slots (menu + drag & drop),
  player window with a 24 h timeline, A-Z / Z-A / account sorting, renaming (on the camera, or a local name
  for shared cameras), tray icon, motion notifications, single instance per user
- Stack: Python 3.12+ (developed on 3.14), PyQt6, `cryptography`; FFmpeg subprocesses (bound to a Windows Job
  Object) decode the video; PyInstaller + Inno Setup build the installer.

Status at handover: all features above work on the owner's real cameras (2x C200 fw 1.5.7 on the LAN,
11x C510W remote, 2 of them shared). About 1444 automated tests: all pass, except for a known
timing-flaky group (section 8).

## 2. Set up a machine
```powershell
powershell -ExecutionPolicy Bypass -File tools/setup_dev.ps1   # venv + deps + checks ffmpeg/Inno Setup
.venv\Scripts\python -m tapo_desktop                            # run from source
```
Prerequisites: Windows 10/11 x64, Python >= 3.12, FFmpeg on PATH (`winget install Gyan.FFmpeg`).
Inno Setup 6 is only needed for `setup.exe` (`winget install JRSoftware.InnoSetup --scope user`).
The setup script installs everything else.

## 3. Everyday commands
| Task | Command |
|---|---|
| Fast tests (about 7 min) | `$env:QT_QPA_PLATFORM='offscreen'; .venv\Scripts\python -m pytest tests -q -m "not soak and not hardware" -p no:cacheprovider --timeout 120` |
| One area | `... -m pytest tests\phase3 -q` (`unit`, `integration`, `e2e`, `phase2`..`phase7`) |
| Spec gate (Gate 1) | `.venv\Scripts\python tools\specs_tool.py verify` (`merge` after adding a spec lane) |
| 30-min soak | `powershell -File tools/run.ps1 test-soak` (`TAPO_SOAK_SECONDS` overrides the length) |
| Live camera tests | set `TAPO_HW_IP`, `TAPO_HW_CLOUD_PASSWORD`, `TAPO_HW_CAM_USER`, `TAPO_HW_CAM_PASSWORD`, then `tools/run.ps1 test-hardware` |
| Smoke vs mocks (with screenshot) | `.venv\Scripts\python tools\smoke_mobile_flow.py` |
| Build the release | `.venv\Scripts\python tools\build_windows.py` -> `release\<timestamp>\TapoDesktop\TapoDesktop.exe` + `TapoDesktop-<ver>-setup.exe` + zip; runs the clean-room audit and a packaged `--selftest` |

Pytest markers: `unit integration chaos soak hardware ui`. UI tests need `QT_QPA_PLATFORM=offscreen`.

## 4. Code map (`src/tapo_desktop`)
| Path | Role |
|---|---|
| `models.py` | dataclasses shared by every layer (`CameraConfig`, `Credentials`, `CloudDevice`, `LanDevice`, ...) |
| `core/errors.py` | the exception hierarchy (`TapoError` ... `StreamLimitError`) |
| `core/auth/crypto.py`, `vault.py` | v3 secure-login crypto; DPAPI credential vault (`%APPDATA%\TapoDesktop\vault.json`) |
| `core/control_client/` | `transport.py` (TOFU-pinned HTTPS), `dtos.py` (request builders/parsers), `client.py` (`ControlApi`, `TapoControlClient`, auth circuit breaker), `tpap.py` (SPAKE2+ P-256, AES-CCM channel) |
| `core/cloud/` | `signing.py` + `client.py` (TP-Link ID, device list, IoT thing list, local access tokens, service URLs), `passthrough.py` (`RemoteControlClient`), `relay.py` (relay slot + TLS tunnel + `SerializedTLSSocket`), `relay_vod.py` (remote playback/download), `_trust.py` (TP-Link root CAs) |
| `core/discovery/` | `tdp.py` (UDP 20002/20004, RSA-OAEP + AES encrypt_info), `matcher.py` (md5(deviceId) or MAC) |
| `core/vod/` | `session.py` (:8800 Digest, MD5 and **HKDF** stream keys, multipart, acks), `downloader.py` |
| `core/media_engine/` | `supervisor.py` (RTSP), `local_stream.py` (:8800 live), `relay_stream.py`, `playback_stream.py`, `_decoder.py` (FFmpeg pipe), `job_object.py`, `limiter.py` |
| `core/onvif_service/` | ONVIF SOAP, WS-Discovery, PTZ, PullPoint events (used only with a camera account) |
| `services/` | `controller.py` (`AppController`, Qt signals, per-camera job queues), `camera_service.py` (routing: local / relay / RTSP / passthrough), `account.py` (sign-in, refresh, LAT, online state, roles), `remote.py` (relay provider + gate), `stream_policy.py`, `view_layout.py` (grid slots), `camera_names.py`, `config_store.py` |
| `ui/` | `main_window.py`, `grid.py`, `video_tile.py`, `controls_panel.py`, `playback_panel.py`, `player_window.py`, `sign_in_dialog.py`, `rename_dialog.py`, `tray.py`, `single_instance.py` |
| `app.py` | entry point: `--selftest`, `--debug`; single-instance guard; silent sign-in |

Persisted settings (`ConfigStore`, `%APPDATA%\TapoDesktop\config.json`; `TAPO_DESKTOP_HOME` overrides the folder):
`account`, `terminal_uuid`, `sign_in_skipped`, `grid_size` (1..5), `grid_slots` (25 ids/null),
`camera_sort`, `max_remote_streams`, `minimize_to_tray`, `notifications`.
Also in that folder: `vault.json` (DPAPI, readable only by that Windows user), `pins.json` (TLS pins), `tapo-desktop.log`.

## 5. Protocol knowledge
- `specs/*_spec.json` hold clean-room facts extracted from the Tapo Android app 3.22.806. Every item has
  JADX file:line evidence or an external source. `specs/tapo_protocol_spec.json` is the merged view
  (lanes listed in `tools/specs_tool.py`). Cite items as `[spec:<id>]` in code.
- Lanes: auth, control, media, vod, cloud, tpap, lat, remote, remote_control, rename.
- `docs/CONTRACT_*.md` are the original interface contracts. Code docstrings are newer and win where they differ.
  `docs/*BRIEF.md` and `artifacts/subagent_reports/*.json` are history (per-lane decisions).

### Facts learned only on real hardware (the APK alone was wrong or silent)
1. C200 fw 1.5.7 advertises `encrypt_type ["4"]`, `tpap {pake:[2]}` and answers the v3 login with **-40211** -> TPAP.
2. Cameras can be in **local-access-token (LAT) mode**: the TP-Link ID password is refused (-40401). The token
   comes from the IoT thing list (`localAccessToken`); the TPAP passcode is `SHA256_HEX_UPPER(LAT)`, and so is
   the :8800 digest secret.
3. :8800 on that firmware uses **HKDF** Key-Exchange: ikm = `nonce + ":" + SHA256U(secret)`, salt = Key-Exchange
   `salt` as UTF-8, info `stream_hkdf_aes_key` / `stream_hkdf_hmac_key`; IV = `X-Nonce` per part;
   `X-Data-Hmac` = base64 HMAC-SHA256 over the ciphertext (bad parts are dropped).
4. Media replies carry `params.session_id` (the spec says `result`). Without it no acks or stop requests are sent.
5. On Windows, closing a socket with unread incoming data sends a RST and the peer drops the stop request ->
   wait 150 ms after sending the stop (`local_stream._STOP_LINGER_S`).
6. Relay: `/v2/relay/request` needs **`cloudType: 2`** (1 gives a slot, then -1007 "device offline"); the tunnel
   needs the exact header list and order (`core/cloud/relay.py`); media inside is plaintext TS; heartbeat every 15 s.
7. v2 device list `status` reads 0 for online cameras -> online state = IoT thing `status == 1`, or found on the LAN.
8. C510W `searchVideoWithUTC` with `id` can give **-71103**; `player_id` (32 hex) works. The client retries once and remembers the form that worked.
9. Cloud passthrough (`/v1/things/{id}/services-sync`, serviceId `passthrough`) works for owned and shared cameras.
10. Shared cameras (thing `role` 1) are never logged in locally (that would only burn lockout attempts).
11. Relay streams arrive at about 6-7 fps each when about 11 run at once (owner's site uplink); a single one runs at about 17 fps.
12. Hardware (d3d11va) decoding costs about 110 MB per FFmpeg process against about 38 MB in software -> grid tiles decode in software, full screen uses d3d11va.

## 6. Rules (safety first)
- **Camera lockout:** every failed local login counts toward the camera's lockout. Never loop logins or
  credential variants. TPAP makes at most 2 attempts per login, and the auth breaker blocks retries for 120 s
  after a wrong password. Keep it that way.
- **Real devices:** only read-only or no-op actions without the owner's explicit consent (for example, rename
  to the current name). Never move, toggle, format or reboot a real camera on your own. The `tools/diag_*.py`
  scripts follow this. Read a script before running it.
- **Personal data:** never commit or log camera IPs, camera names, Wi-Fi names, account e-mails or ids, device
  ids, tokens or passwords. Use neutral placeholders (`192.168.1.x`, "Home Camera A"). Before every push run
  `git grep -I -n -P "192\.168\.(?!1\.)\d|localAccessToken\x22:\s*\x22[0-9A-Fa-f]{8}"` and review the hits; the unit tests' made-up
  192.168.0.2 is expected (see section 9 for the scrub procedure).
- **Clean room:** never commit APK files, decompiled sources or proprietary assets (`artifacts/apk`,
  `artifacts/jadx_base` and `artifacts/tools` are git-ignored). Spec evidence snippets stay at 2 short lines or less.
  `tools/audit_release.py` must pass for every build.
- **TDD:** write failing tests first, commit them, then implement. Don't weaken existing tests. If a requirement
  changes, edit the test in a commit whose message says so (examples: the 5x5 grid change, the pytest-qt `bool()` fix).
- **Fixtures** (`tests/fixtures/mock_*`): additive changes only. Existing behaviour must stay unchanged by
  default; new behaviour goes behind an option.
- **Files:** UTF-8 **without BOM** (a BOM broke `pyproject.toml` once; Windows PowerShell 5 `Set-Content -Encoding utf8`
  writes one), LF line endings in Python files, `core.autocrlf=false`.

## 7. Environment quirks on Windows
- Web filters such as **AdGuard** intercept plain HTTP from python.exe/ffmpeg.exe, including loopback ->
  :8800 breaks. Tests detect this and skip (`require_loopback_http_stream`). Users must exclude the app.
- Build output folders can become **read-only (ACL) after the app ran from them**. That is why each build goes to
  `release/<timestamp>/` (the old `dist/` is locked; it needs admin rights to delete).
- Connecting to a closed loopback port takes about 2 s on Windows. Use timeouts of 3 s or more in such tests.
- Python's faulthandler may print "Windows fatal exception: access violation" when a chaos test resets an SSL
  socket mid-send. It is harmless: tests continue and pass.
- `rg` (ripgrep) is much faster than PowerShell `Select-String` on the decompiled tree.

## 8. Known issues and open items
- **Timing-flaky tests under load:** `tests/phase3/test_relay_stream.py::test_stop_is_fast_and_clean`,
  `test_relay_ui.py::test_end_to_end_remote_stream_through_real_wiring`, and rarely one in `phase2` or
  `integration/test_media_supervisor.py`. A single file always passes when rerun. Partly caused by the
  mock relay (one TLS socket used by two threads); see the would-block handling in `tests/fixtures/mock_relay.py`.
- **Not verified on hardware:** playback across clip boundaries, end of recordings and 2x/4x/8x speed; real cloud
  error replies for offline cameras; how long the cloud takes to show a rename; which commands a shared user may send.
- **Not built:** two-way talk (spec `audio.talkback.*`, `remote.talkback.summary`, relay `type=dtspk`), audio in
  live view and downloads, a stream quality settings UI (HD/SD per grid size, decoder choice), staggered relay
  starts (8-10 s to the first frame when 11 start at once), a license file, CI, code signing.
- `stop()` cannot interrupt a relay slot request or tunnel connect that is already in progress (up to 15 s).
- `docs/CONTRACT_UI.md` and `docs/ARCHITECTURE.md` predate phases 2-7 (slots, playback window, sorting, rename).

## 9. Material that is local only (not in this repo)
| What | Where on the owner's PC | How to recreate |
|---|---|---|
| Tapo XAPK and unpacked APKs | `artifacts/apk/` | download `com.tplink.iot` 3.22.806, unzip the `.xapk` |
| Decompiled Java (~60k files) | `artifacts/jadx_base/sources/` | `jadx --no-res --show-bad-code -j 8 -d artifacts/jadx_base artifacts/apk/unpacked/com.tplink.iot.apk` (JADX 1.5.6, about 14 GB heap) |
| JADX / Apktool | `artifacts/tools/` | GitHub releases skylot/jadx, iBotPeaches/Apktool |
| Built installers | `release/` | `tools/build_windows.py` |
| App data, vault, log | `%APPDATA%\TapoDesktop\` | the user signs in again (DPAPI blobs only work for that Windows user) |
| Unscrubbed git history | `..\054.Tapo_Camera_Windows_backup\original-history.bundle` | contains personal data: **never push it** |

The public repo history was scrubbed with `git filter-repo --replace-text`. Do the same if personal data
ever gets committed, then force-push only with the owner's approval.

## 10. Phase history (one line each)
1. Spec extraction (4 lanes), mock camera with chaos proxy, TDD for control/media/ONVIF/UI, Windows packaging.
2. Mobile parity: TP-Link ID sign-in, cloud device list, TDP discovery, TPAP, :8800 live view.
2b. Real-camera fixes: local access tokens, HKDF media keys, online state.
3. Remote live view through the cloud relay (`SerializedTLSSocket` for heartbeats).
4. Remote controls and recordings through cloud passthrough + relay VOD; shared cameras; -71103 fallback.
5. 4x4/5x5 grids, assignable grid slots, player window with timeline; single instance.
6. View-driven streaming: every visible tile streams; software decoding for SD tiles.
7. A-Z sorting and renaming; session_id shape fix; stop linger.
Checkpoints: `specs/agent_checkpoint.json`.
