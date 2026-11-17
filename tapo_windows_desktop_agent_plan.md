# Autonomous Agent CLI Execution Plan: Native Windows TP-Link Tapo Camera Desktop Client via REA Static Analysis, Multi-Subagent Parallelism & Strict TDD

## 1. Executive Summary & Architectural Strategy

This master execution plan instructs an autonomous Agent CLI (Claude Code, Kiro CLI, or Antigravity) on Windows x64 to reverse-engineer the feature set, data transfer objects (DTOs), and local device communication schemas of the official Android [TP-Link Tapo APK (`com.tplink.iot`)](https://apkpure.com/vn/tp-link-tapo/com.tplink.iot/download) using [REA (Reverse Engineer Anything)](https://github.com/morluto/rea), and subsequently build a rock-solid, low-latency native Windows desktop application.

To maximize build velocity and guarantee long-term runtime stability, this plan enforces three foundational engineering pillars:

1. **Dev-Time Only REA Isolation**: [REA (`morluto/rea`)](https://github.com/morluto/rea) is restricted strictly to **Dev-Time R&D and Specification Extraction**. The agent inspects the APK offline to extract JSON-RPC command payloads, authentication state machines, UI capability matrices, and audio/video codec parameters into a clean-room `tapo_protocol_spec.json` contract. The runtime Windows application is built against those verified specifications alongside documented local interoperability standards ([TP-Link RTSP/ONVIF Specification](https://www.tp-link.com/us/support/faq/2680/), [JurajNyiri/pytapo](https://github.com/JurajNyiri/pytapo), and [displaynone/tapo-camera-client](https://github.com/displaynone/tapo-camera-client)).
2. **DAG-Based Multi-Subagent Parallel Execution**: Instead of sequential single-threaded execution, the Main Orchestrator Agent spawns specialized parallel subagents across isolated Git worktrees (`git worktree`) across three concurrent waves: **Wave 1 (Parallel REA Static Analysis)**, **Wave 2 (Parallel TDD Red-Phase & Mock Fixture Synthesis)**, and **Wave 3 (Parallel Green/Refactor Subsystem Implementation)**.
3. **Strict Test-Driven Development (Red-Green-Refactor) & Chaos Resilience**: Zero production code may be written before a failing automated test (`Red`) exists. Beyond unit tests, the pipeline enforces cryptographic golden-master parity, stateful network fault injection (Wi-Fi drops, expired `stok` tokens, camera busy `-40404` locks), and process handle/memory soak testing so the compiled Windows app runs 24/7 without freezes or memory leaks.

| Architectural Layer | Selected Technology / Protocol | Primary Role & Grounded Evidence |
| :--- | :--- | :--- |
| **Static Analysis & R&D Engine** | [REA (`rea-agents` v6.9+)](https://github.com/morluto/rea) + Headless JADX + Apktool + Ghidra | Decompiles DEX classes, extracts Android resources, and inspects JNI `.so` libraries locally via MCP ([REA Documentation](https://github.com/morluto/rea)). |
| **Target Artifact** | [TP-Link Tapo APK (`com.tplink.iot`)](https://apkpure.com/vn/tp-link-tapo/com.tplink.iot/download) | Source reference for camera control DTOs, PTZ velocity curves, SD card playback indexing, and smart detection toggles ([APKPure Repository](https://apkpure.com/vn/tp-link-tapo/com.tplink.iot/download)). |
| **Video & Audio Streaming Plane** | Local RTSP (TCP Port `554`) + FFmpeg / WebRTC | Hardware-accelerated H.264/H.265 video and PCM ALAW (`alaw`) audio decoding across `/stream1` (HD), `/stream2` (SD), and `/stream6` (telephoto) ([TP-Link RTSP/ONVIF Guide](https://www.tp-link.com/us/support/faq/2680/)). |
| **Telemetry & PTZ Event Plane** | Local ONVIF Profile S (TCP Port `2020`) | Standardized pan-tilt-zoom vectors and real-time motion detection binary sensor events ([Tapo ONVIF FAQ](https://www.tapo.com/en/faqs/tapo-camera-onvif-and-rtsp-common-questions-answered)). |
| **Extended Device Control Plane** | Local HTTPS API (TCP Port `443`) | Encrypted local `stok` token session handling IR night vision modes, privacy mask, LED status, presets, and SD card recording downloads ([JurajNyiri/pytapo](https://github.com/JurajNyiri/pytapo)). |
| **Windows Native Runtime Stack** | Tauri v2 (Rust Backend) + React/TypeScript UI (or Python 3.12 + PyQt6) | Zero-bloat native Windows executable with system tray background monitoring, multi-camera grid view, and hardware D3D11VA decoding ([displaynone/tapo-camera-client](https://github.com/displaynone/tapo-camera-client)). |
| **TDD & Quality Assurance Stack** | Rust `cargo test` + `proptest` / Vitest + Playwright + Local Chaos Proxy | Enforces Red-Green-Refactor cycles, deterministic packet drop recovery, and `<180 MB` RAM ceiling across multi-camera streams ([HomeAssistant-Tapo-Control](https://github.com/jurajnyiri/homeassistant-tapo-control)). |

## 2. Multi-Subagent Parallel Execution Topology (DAG Orchestration)

To cut end-to-end R&D and build time by 60–70%, the Main Orchestrator Agent must never analyze classes or implement independent subsystems sequentially in its main context loop. Instead, the Orchestrator delegates work across **4 concurrent subagent lanes** separated by strict **Synchronization Barriers** and isolated **Git Worktrees** (`C:\Projects\TapoWindowsClient\worktrees\<agent_name>`).

```
                     [Main Orchestrator Agent: Phase 1 & 2 Bootstrap]
                       Unpacks APK, runs `rea doctor`, initializes Git
                                             |
       +-------------------------------------+-------------------------------------+
       | WAVE 1: PARALLEL REA STATIC ANALYSIS (4 Concurrent Read-Only Subagents)   |
       v                         v                         v                       v
+-----------------+     +------------------+     +------------------+     +-----------------+
| Subagent-RE-Auth|     |Subagent-RE-Ctrl  |     |Subagent-RE-Media |     | Subagent-RE-VOD |
| Traces stok,    |     |Decompiles JSON-  |     |Extracts RTSP URIs|     |Traces SD card   |
| cnonce, klap,   |     |RPC multipleReq,  |     |/stream1..7, JNI  |     |UTC search & MP4 |
| tpap, TLS pins  |     |PTZ, NightVision  |     |libtpstream, alaw |     |segment download |
+--------+--------+     +--------+---------+     +--------+---------+     +--------+--------+
         |                       |                        |                        |
         +-----------------------+-----------+------------+------------------------+
                                             |
                  [SYNCHRONIZATION BARRIER 1: Spec Merge & Gate 1 Audit]
                   Produces immutable `specs/tapo_protocol_spec.json`
                                             |
       +-------------------------------------+-------------------------------------+
       | WAVE 2: PARALLEL TDD RED-PHASE & MOCK FIXTURES (4 Isolated Worktrees)     |
       v                         v                         v                       v
+-----------------+     +------------------+     +------------------+     +-----------------+
|Subagent-Fixture |     |Subagent-TDD-Core |     |Subagent-TDD-Media|     |Subagent-TDD-UI  |
|Builds Stateful  |     |Writes FAILING    |     |Writes FAILING    |     |Writes FAILING   |
|Mock Tapo Server |     |(Red) crypto, auth|     |(Red) RTSP, FFmpeg|     |(Red) Grid, PTZ, |
|(:8443/8554/8020)|     |& JSON-RPC tests  |     |watchdog & audio  |     |& Timeline UI    |
|& Chaos Proxy    |     |                  |     |recovery tests    |     |Playwright tests |
+--------+--------+     +--------+---------+     +--------+---------+     +--------+--------+
         |                       |                        |                        |
         +-----------------------+-----------+------------+------------------------+
                                             |
                 [SYNCHRONIZATION BARRIER 2: Verify All Tests Fail (Red)]
                                             |
       +-------------------------------------+-------------------------------------+
       | WAVE 3: PARALLEL TDD GREEN/REFACTOR IMPLEMENTATION (4 Worktrees)          |
       v                         v                         v                       v
+-----------------+     +------------------+     +------------------+     +-----------------+
|Subagent-Impl-   |     |Subagent-Impl-    |     |Subagent-Impl-    |     |Subagent-Impl-UI |
|Control          |     |Media             |     |ONVIF             |     |Builds React/    |
|Implements DPAPI |     |Implements FFmpeg |     |Implements Port   |     |PyQt6 UI, Tray,  |
|Vault & HTTPS    |     |D3D11VA supervisor|     |2020 WS-Discovery |     |& IPC bindings   |
|:443 Client      |     |& alaw transcoder |     |& PullPoint events|     |until UI Green   |
+--------+--------+     +--------+---------+     +--------+---------+     +--------+--------+
         |                       |                        |                        |
         +-----------------------+-----------+------------+------------------------+
                                             |
          [SYNCHRONIZATION BARRIER 3: Worktree Merge, Chaos Soak & Packaging]
                       Executes Gates 2, 3, and 4 -> Final `.msi`/`.exe`
```

### Subagent Isolation & Anti-Collision Rules

| Subagent Governance Rule | Enforcement Mechanism | Operational Benefit |
| :--- | :--- | :--- |
| **Read-Only REA Concurrency (Wave 1)** | All 4 Wave 1 subagents query [REA (`morluto/rea`)](https://github.com/morluto/rea) against the unpacked APK in read-only mode and write exclusively to their own designated file (`specs/auth_handshake_spec.json`, `specs/camera_control_dtos.json`, `specs/stream_profiles.json`, `specs/sd_vod_playback_spec.json`). | Zero file lock contention while decompiling DEX bytecode and JNI `.so` libraries in parallel. |
| **Git Worktree Isolation (Waves 2 & 3)** | Before spawning Wave 2 or Wave 3 coding subagents, the Orchestrator creates dedicated worktrees (`git worktree add .\worktrees\lane-control -b feat/control`, etc.). | Each subagent compiles, runs tests, and refactors in its own filesystem tree without overwriting sibling modules. |
| **Immutable Test Lock in Wave 3** | Wave 3 implementation subagents are forbidden from deleting, skipping (`#[ignore]` / `.skip`), or weakening assertions in test files authored during Wave 2. | Guarantees genuine TDD compliance and prevents agents from gaming test suites to force a pass. |
| **Structured Handoff Manifests** | Every subagent terminates by writing a compact JSON summary (`artifacts/subagent_reports/<agent_id>.json`) listing verified evidence, test counts, and unresolved edge cases. | Keeps the Main Orchestrator context window clean and prevents context bloat ([REA MCP Schema Guide](https://github.com/morluto/rea)). |

## 3. Phase 1 & 2: Windows Host Bootstrap & APK Integrity Audit

The Main Orchestrator Agent first provisions the Windows x64 host environment and prepares the unpacked [TP-Link Tapo APK (`com.tplink.iot`)](https://apkpure.com/vn/tp-link-tapo/com.tplink.iot/download) before fanning out subagents. Per the [REA Installation Requirements](https://github.com/morluto/rea/blob/main/docs/getting-started/installation.md), Android APK and resource analysis on Windows x64 require a patched Node.js runtime, OpenJDK 21, headless JADX, Apktool, and Ghidra.

### Step 3.1: Automated Host Provisioning & Workspace Initialization

```powershell
# 1. Verify Node.js runtime version (REA requires Node.js >=22.23.4, >=24.22.1, or >=26.12.1)
node --version
npm --version

# 2. Install OpenJDK 21, JADX, Apktool, FFmpeg, and Git via Winget if missing
winget install --id Microsoft.OpenJDK.21 -e --accept-source-agreements --accept-package-agreements
winget install --id skylot.jadx -e --accept-source-agreements --accept-package-agreements
winget install --id iBotPeaches.Apktool -e --accept-source-agreements --accept-package-agreements
winget install --id Gyan.FFmpeg -e --accept-source-agreements --accept-package-agreements

# 3. Create workspace directory structure and initialize Git for subagent worktrees
$WorkspaceRoot = "C:\Projects\TapoWindowsClient"
$Dirs = @(
    "$WorkspaceRoot\artifacts\apk",
    "$WorkspaceRoot\artifacts\rea_evidence",
    "$WorkspaceRoot\artifacts\subagent_reports",
    "$WorkspaceRoot\specs",
    "$WorkspaceRoot\src\core\auth",
    "$WorkspaceRoot\src\core\control_client",
    "$WorkspaceRoot\src\core\media_engine",
    "$WorkspaceRoot\src\core\onvif_service",
    "$WorkspaceRoot\src\ui",
    "$WorkspaceRoot\tests\fixtures\mock_tapo_server",
    "$WorkspaceRoot\tests\unit",
    "$WorkspaceRoot\tests\integration",
    "$WorkspaceRoot\tests\soak",
    "$WorkspaceRoot\worktrees"
)
foreach ($d in $Dirs) { New-Item -ItemType Directory -Force -Path $d | Out-Null }
Set-Location $WorkspaceRoot
git init

# 4. Install and register REA CLI & MCP Server with compact schema profiles
npm install --global rea-agents@latest
npx rea-agents@latest setup
$env:REA_MCP_INPUT_SCHEMA_PROFILE = "compact"
$env:REA_MCP_OUTPUT_SCHEMA_PROFILE = "compact"
rea doctor
```

### Step 3.2: APK/XAPK Unpacking & Signature Verification

Download the [TP-Link Tapo APK (`com.tplink.iot`)](https://apkpure.com/vn/tp-link-tapo/com.tplink.iot/download) into `C:\Projects\TapoWindowsClient\artifacts\apk\` and unpack any split `.xapk` bundle so all 4 Wave 1 subagents can inspect `base.apk` and `config.arm64_v8a.apk` simultaneously:

```powershell
Get-FileHash "C:\Projects\TapoWindowsClient\artifacts\apk\*" -Algorithm SHA256 |
  ConvertTo-Json | Out-File "C:\Projects\TapoWindowsClient\artifacts\rea_evidence\artifact_hashes.json"

if (Test-Path "C:\Projects\TapoWindowsClient\artifacts\apk\com.tplink.iot.xapk") {
    Copy-Item "C:\Projects\TapoWindowsClient\artifacts\apk\com.tplink.iot.xapk" "C:\Projects\TapoWindowsClient\artifacts\apk\tapo_bundle.zip"
    Expand-Archive "C:\Projects\TapoWindowsClient\artifacts\apk\tapo_bundle.zip" -DestinationPath "C:\Projects\TapoWindowsClient\artifacts\apk\unpacked" -Force
}

rea inspect-apk-signature "C:\Projects\TapoWindowsClient\artifacts\apk\unpacked\com.tplink.iot.apk" --json > "C:\Projects\TapoWindowsClient\artifacts\rea_evidence\apk_signature.json"
```

## 4. Phase 3 (Wave 1): Parallel REA Static Analysis Subagents

The Main Orchestrator spawns **4 concurrent REA investigation subagents** in a single parallel batch turn. Each subagent uses [REA's MCP tools](https://github.com/morluto/rea) (`open_binary`, `search_strings`, `search_procedures`, `find_xrefs_to_name`, `get_call_graph`, `procedure_pseudo_code`, and JADX/Apktool bridges) to extract its assigned domain contract.

| Subagent ID | Assigned APK Domain | REA Tool & Clue Sequence | Output Contract File |
| :--- | :--- | :--- | :--- |
| **`Subagent-RE-Auth`** | Local Authentication, Session Handshake & Crypto | JADX Bridge: `search_strings` (`"stok"`, `"cnonce"`, `"snonce"`, `"digest_passwd"`, `"start_seq"`, `"klap"`, `"tpap"`) -> `find_xrefs_to_name` -> `procedure_pseudo_code` | `specs/auth_handshake_spec.json` ([JurajNyiri/pytapo](https://github.com/JurajNyiri/pytapo)) |
| **`Subagent-RE-Control`** | HTTPS `:443` JSON-RPC Command Catalog (`multipleRequest`) | JADX Bridge: `search_procedures` (`"set_lens_mask_info"`, `"setDayNightMode"`, `"motorMove"`, `"get_preset_config"`, `"setAlarmConfig"`) -> `batch_decompile` | `specs/camera_control_dtos.json` ([HomeAssistant-Tapo-Control](https://github.com/jurajnyiri/homeassistant-tapo-control)) |
| **`Subagent-RE-Media`** | RTSP `:554` Dual-Lens Routing, JNI Audio & Two-Way Talkback | JADX + Apktool + Ghidra JNI (`libtpstream.so`): `search_strings` (`"stream1"`, `"stream2"`, `"stream6"`, `"stream7"`, `"pcm_alaw"`, `"talkback"`) | `specs/stream_profiles.json` ([TP-Link RTSP/ONVIF Guide](https://www.tp-link.com/us/support/faq/2680/)) |
| **`Subagent-RE-VOD`** | ONVIF `:2020` Events & SD Card VOD Playback Downloader | JADX Call Graph: `search_strings` (`"search_video_with_utc"`, `"searchVideoOfDay"`, `"PullPointSubscription"`, `"2020"`) -> `procedure_callees` | `specs/sd_vod_playback_spec.json` ([Tapo ONVIF FAQ](https://www.tapo.com/en/faqs/tapo-camera-onvif-and-rtsp-common-questions-answered)) |

### Synchronization Barrier 1: Unified Protocol Schema Synthesis

Once all 4 Wave 1 subagents complete, the Main Orchestrator validates their outputs and merges them into `specs/tapo_protocol_spec.json`, capturing:
- Exact cryptographic state machines for both legacy MD5 `stok` login and modern SHA-256 AES-CBC/GCM local sessions (`cnonce`, `snonce`, `start_seq`, `seq` increment rules, and `-40401` token expiration codes) ([JurajNyiri/pytapo](https://github.com/JurajNyiri/pytapo)).
- Exact RTSP URI mappings for single-lens (`/stream1` HD, `/stream2` SD) and dual-lens telephoto cameras (`/stream6` HD, `/stream7` SD) ([TP-Link RTSP/ONVIF Guide](https://www.tp-link.com/us/support/faq/2680/)).
- Hardware concurrency constraints: maximum 2 simultaneous live streams per camera and mandatory check for the 3-way hardware exclusion between SD card recording, Tapo Care, and NVR/RTSP/ONVIF ([Tapo RTSP & ONVIF FAQ](https://www.tapo.com/en/faqs/tapo-camera-onvif-and-rtsp-common-questions-answered)).

## 5. Phase 4 (Waves 2 & 3): Strict TDD Mechanism & Parallel Clean-Room Implementation

To guarantee production-grade stability on Windows, development follows a strict **Red-Green-Refactor Test-Driven Development (TDD)** lifecycle executed across parallel subagent worktrees.

### 5.1: The 4-Tier TDD & Stability Assurance Pyramid

```
                       /\
                      /  \     Tier 4: Live Hardware LAN & E2E UI Acceptance
                     /----\    (Physical Camera RTSP :554, ONVIF :2020, HTTPS :443)
                    /      \
                   /--------\   Tier 3: Multi-Hour Endurance, Soak & Handle Guard
                  /          \  (30-min accelerated 2x2 grid soak, <180MB RAM, 0 leaks)
                 /------------\
                /              \ Tier 2: Stateful Mock Camera & Chaos Fault Injection
               /----------------\ (Wi-Fi drop recovery <3s, -40401 re-auth, RTP corruption)
              /                  \
             /--------------------\ Tier 1: Golden-Master Crypto & Contract Unit Tests
            +----------------------+ (100% deterministic DTO & AES-CBC/GCM vector parity)
```

| TDD Tier | Target Failure Modes Prevented | Mandatory Automated Tests Written in Wave 2 (`Red` Phase) | Stability Acceptance Criteria (`Green` & `Refactor` Phase) |
| :--- | :--- | :--- | :--- |
| **Tier 1: Golden-Master Contract & Crypto Unit Tests** | Malformed JSON-RPC payloads, wrong AES IV/tag calculation, sequence number desync, DPAPI credential corruption. | Property-based (`proptest` / `hypothesis`) and golden-vector tests verifying `cnonce`/`snonce` derivation, AES-128-CBC/GCM payload encryption, `seq` auto-increment, and all `multipleRequest` DTO serializations against `specs/tapo_protocol_spec.json` ([JurajNyiri/pytapo](https://github.com/JurajNyiri/pytapo)). | 100% pass rate; 100% branch coverage on authentication, cipher, and DTO serializer modules; zero panics (`unwrap()`) on malformed JSON inputs. |
| **Tier 2: Stateful Mock Server & Chaos Fault Injection** | App freeze on Wi-Fi drop, unhandled `-40401` session timeout, camera lockup from >2 concurrent streams (`-40404`), zombie FFmpeg processes. | Integration tests driven against `mock_tapo_server` with an inline **Chaos TCP Proxy** that injects: (a) mid-stream TCP RST / 5s blackhole, (b) forced HTTP `401` / Tapo `-40401` expired `stok` every 50 requests, (c) `-40404` busy responses when opening a 3rd stream, and (d) truncated RTSP `pcm_alaw` audio packets ([HomeAssistant-Tapo-Control](https://github.com/jurajnyiri/homeassistant-tapo-control)). | Automatic RTSP stream reconnection within `<3.0 seconds` of network recovery; transparent background `stok` token renewal without dropping UI state; strict semaphore blocking any 3rd concurrent stream to the same camera IP. |
| **Tier 3: Endurance Soak & Resource Leak Guard** | Memory bloat over hours of viewing, Windows GDI/file handle exhaustion, UI frame drops during 4-camera grid decoding. | Automated soak harness (`tests/soak/test_endurance_grid.spec`) streaming 4 concurrent mock cameras in a `2x2` `/stream2` grid for 30 minutes while firing 500 rapid PTZ `motorMove` and night-vision toggle commands ([TP-Link RTSP/ONVIF Guide](https://www.tp-link.com/us/support/faq/2680/)). | Process private working set remains `<180 MB` with `<2%` memory growth slope over the last 20 minutes; zero orphaned `ffmpeg` child processes upon window close or crash signal. |
| **Tier 4: Live Hardware LAN & E2E UI Tests** | Real-world TLS self-signed cert rejection, D3D11VA GPU incompatibility, audio sync drift, unresponsive PTZ joystick. | Playwright / WebDriver E2E suite testing the packaged desktop UI against a real Tapo camera on LAN (`443`, `554`, `2020`): verifies live video canvas rendering (`>=15 FPS`), PTZ preset movement, privacy mask toggle, and SD card VOD clip download ([Tapo ONVIF FAQ](https://www.tapo.com/en/faqs/tapo-camera-onvif-and-rtsp-common-questions-answered)). | End-to-end glass-to-glass RTSP latency `<350 ms` on LAN; verified MP4 download checksum from SD card; Windows System Tray toast notification delivered within `<1.5 seconds` of ONVIF motion event. |

### 5.2: Wave 2 — Parallel TDD Red-Phase & Mock Fixture Subagents

Before any application logic is written, the Main Orchestrator creates 4 Git worktrees and dispatches **4 Wave 2 Subagents in parallel**:

1. **`Subagent-Fixture` (`worktrees/lane-fixture`)**:
   - Builds the self-contained **Stateful Mock Tapo Camera Server & Chaos Proxy** (`tests/fixtures/mock_tapo_server`):
     - **HTTPS `:8443`**: Implements full `stok` challenge-response, AES-CBC/GCM encryption validation, `seq` tracking, stateful device shadow (stores current LED, privacy mask, night vision, and PTZ `x`/`y` coordinates), and fault-injection hooks (`inject_token_expiry`, `inject_busy_40404`).
     - **RTSP `:8554`**: Streams synthetic H.264 video + 8 kHz mono `pcm_alaw` audio over `/stream1`, `/stream2`, and `/stream6`, enforcing a hard cap of 2 simultaneous RTSP client connections per simulated camera ([HomeAssistant-Tapo-Control FAQ](https://github.com/jurajnyiri/homeassistant-tapo-control)).
     - **ONVIF `:8020`**: Serves WS-Discovery probes, `GetCapabilities`, `ContinuousMove`, and synthetic `PullPointSubscription` motion event XML bursts ([Tapo ONVIF FAQ](https://www.tapo.com/en/faqs/tapo-camera-onvif-and-rtsp-common-questions-answered)).
2. **`Subagent-TDD-Core` (`worktrees/lane-tdd-core`)**:
   - Writes comprehensive **failing (`Red`) unit, property-based, and chaos contract tests** (`tests/unit/test_auth_crypto.rs`, `tests/integration/test_control_chaos.rs`) covering Windows DPAPI credential storage, dual-credential fallback (Local Camera Account vs. TP-Link Cloud password hash for SD card VOD), `multipleRequest` batching, and automatic `-40401` token refresh.
3. **`Subagent-TDD-Media` (`worktrees/lane-tdd-media`)**:
   - Writes **failing (`Red`) media & resilience tests** (`tests/integration/test_media_supervisor.rs`, `tests/soak/test_endurance_grid.rs`) asserting D3D11VA hardware decoder initialization, `/stream1` (fullscreen) to `/stream2` (grid) dynamic switching, `pcm_alaw` to PCM/Opus audio transcoding, TCP drop auto-reconnect (`<3s`), and zero zombie FFmpeg child processes on parent kill (via Windows Job Objects `CreateJobObjectW` + `JOB_OBJECT_LIMIT_KILL_ON_JOB_CLOSE`).
4. **`Subagent-TDD-UI` (`worktrees/lane-tdd-ui`)**:
   - Writes **failing (`Red`) frontend component & Playwright E2E tests** (`tests/e2e/test_desktop_ui.spec.ts`) covering the `1x1`/`2x2`/`3x3` camera grid, virtual PTZ joystick & `WASD` keyboard bindings, optimistic UI rollback when the camera is offline, and the SD card UTC timeline VOD player.

### 5.3: Wave 3 — Parallel TDD Green/Refactor Implementation Subagents

Once Synchronization Barrier 2 confirms that the Mock Tapo Camera Server is live and all Wave 2 tests fail (`Red`) as expected, the Main Orchestrator dispatches **4 Wave 3 Implementation Subagents in parallel** across isolated worktrees to turn their respective test suites `Green` and `Refactor` for performance:

1. **`Subagent-Impl-Control` (`worktrees/lane-impl-control`)**:
   - Implements `src/core/auth` (Windows DPAPI / `wincred` vault) and `src/core/control_client` (async HTTPS `:443` client with TOFU certificate fingerprint pinning, AES-CBC/GCM session cipher, request coalescing, and per-camera concurrency semaphores) ([JurajNyiri/pytapo](https://github.com/JurajNyiri/pytapo)).
2. **`Subagent-Impl-Media` (`worktrees/lane-impl-media`)**:
   - Implements `src/core/media_engine` using an embedded FFmpeg / `go2rtc` worker pool bound to a **Windows Job Object** (`JOB_OBJECT_LIMIT_KILL_ON_JOB_CLOSE`) so child processes can never leak if the app crashes.
   - Configures `-hwaccel d3d11va -rtsp_transport tcp -fflags nobuffer -flags low_delay`, dynamic stream profile switching (`/stream1`, `/stream2`, `/stream6`, `/stream7`), and an exponential-backoff circuit breaker for network drops ([TP-Link RTSP/ONVIF Guide](https://www.tp-link.com/us/support/faq/2680/)).
3. **`Subagent-Impl-ONVIF` (`worktrees/lane-impl-onvif`)**:
   - Implements `src/core/onvif_service` supporting UDP multicast WS-Discovery, manual subnet scan fallback on port `2020`, continuous PTZ velocity vectors, and `PullPointSubscription` motion listeners hooked into native Windows Toast notifications ([Tapo ONVIF FAQ](https://www.tapo.com/en/faqs/tapo-camera-onvif-and-rtsp-common-questions-answered)).
4. **`Subagent-Impl-UI` (`worktrees/lane-impl-ui`)**:
   - Implements the responsive native Windows UI (`src/ui`) with a multi-camera grid (`1x1`, `2x2`, `3x3`), low-latency video canvas, PTZ control overlay, quick toggles (Privacy Mask, IR Night Vision `auto`/`on`/`off`, Siren, LED, Two-Way Talkback mic), SD card timeline VOD explorer, and System Tray background minimization ([displaynone/tapo-camera-client](https://github.com/displaynone/tapo-camera-client)).

## 6. Phase 5: 4-Gate Closed-Loop Verification & Self-Healing Pipeline

After merging all Wave 3 worktrees at Synchronization Barrier 3, the Main Orchestrator runs the **4-Gate Closed-Loop Verification System**. No milestone is marked complete until its gate passes 100%.

| Verification Gate | Automated Test Command / Check | Pass Criteria | Self-Healing Fallback Action |
| :--- | :--- | :--- | :--- |
| **Gate 1: Schema & Evidence Parity** | `npm run verify:specs` | 100% of DTO fields in `specs/tapo_protocol_spec.json` map to verified JADX/Ghidra evidence from [REA (`morluto/rea`)](https://github.com/morluto/rea) across all 4 Wave 1 subagent reports. | Spawn targeted REA repair subagent using `procedure_pseudo_code` and `xrefs` to resolve any obfuscated R8/ProGuard `@SerializedName` tags. |
| **Gate 2: TDD Unit, Contract & Chaos Suite** | `npm run test:tdd-all` (Tier 1 + Tier 2 Chaos against `mock_tapo_server`) | 100% pass across crypto vectors, `-40401` token expiry recovery, `-40404` 2-stream concurrency guard, and `<3s` RTSP reconnect after simulated Wi-Fi drop. | Inspect failing test trace, isolate the offending subsystem worktree, and run an automated Red-Green repair loop until deterministic parity with [JurajNyiri/pytapo](https://github.com/JurajNyiri/pytapo) is achieved. |
| **Gate 3: Endurance Soak & Live Hardware Smoke Test** | `npm run test:soak && npm run test:hardware -- --ip <CAMERA_LAN_IP>` | 30-minute `2x2` grid soak holds RAM `<180 MB` with 0 leaked handles; physical LAN camera streams `/stream1` at `>=15 FPS` via D3D11VA and executes PTZ/Night-Vision toggles. | If RAM grows, profile frame buffer disposal in `media_engine`; if live RTSP fails, verify port `554`/`2020`, fallback to `/stream2`, and check the 2-of-3 SD/TapoCare/NVR hardware rule ([Tapo RTSP & ONVIF FAQ](https://www.tapo.com/en/faqs/tapo-camera-onvif-and-rtsp-common-questions-answered)). |
| **Gate 4: Windows Packaging & Clean-Room Audit** | `npm run build:windows && rea audit-release-exposure ./dist` | Produces standalone Windows `.msi`/`.exe`; zero bundled `rea`, JADX, or proprietary APK assets; Windows Job Object kills all FFmpeg workers within `<200ms` of app exit. | Strip dev dependencies, verify Windows Job Object handle attachment, and re-run `rea audit-release-exposure` ([REA Documentation](https://github.com/morluto/rea)). |

## 7. Phase 6: Master Agent CLI Prompt (Copy-Paste Execution Block)

Copy and paste the following master prompt directly into your Windows Agent CLI (Claude Code, Kiro CLI, or Antigravity) inside `C:\Projects\TapoWindowsClient`:

```text
ROLE & OBJECTIVE:
You are the Lead Orchestrator Agent operating on Windows x64. Your objective is to analyze the official TP-Link Tapo Android APK (`com.tplink.iot`) in `./artifacts/apk/` using the registered `rea` (Reverse Engineer Anything) MCP server (`rea-agents`), extract a clean-room protocol specification into `./specs/tapo_protocol_spec.json`, and build a rock-solid native Windows desktop client for TP-Link Tapo cameras using parallel subagents and strict Test-Driven Development (TDD).

MANDATORY MULTI-SUBAGENT PARALLELISM RULES:
1. NEVER process independent reverse-engineering targets or coding subsystems sequentially in your main loop. Always spawn parallel subagents (`Task` / subagent workers) in concurrent waves.
2. Wave 1 (Parallel REA Static Analysis): Spawn 4 read-only subagents concurrently against `./artifacts/apk/unpacked/`:
   - `Subagent-RE-Auth`: Trace `stok`, `cnonce`, `snonce`, `klap`, `tpap`, and AES-CBC/GCM session handshakes -> write `./specs/auth_handshake_spec.json`.
   - `Subagent-RE-Control`: Decompile HTTPS `:443` `multipleRequest` DTOs (`motorMove`, `set_lens_mask_info`, `setDayNightMode`, presets, siren, LED) -> write `./specs/camera_control_dtos.json`.
   - `Subagent-RE-Media`: Extract RTSP `:554` URI patterns (`/stream1`, `/stream2`, `/stream6`, `/stream7`), JNI `libtpstream.so` audio (`pcm_alaw`), and two-way talkback framing -> write `./specs/stream_profiles.json`.
   - `Subagent-RE-VOD`: Trace ONVIF `:2020` `PullPointSubscription` events and SD card UTC recording search/download (`searchVideoOfDay`, `search_video_with_utc`) -> write `./specs/sd_vod_playback_spec.json`.
3. Wait at Synchronization Barrier 1 until all 4 Wave 1 subagents finish, then merge their outputs into `./specs/tapo_protocol_spec.json` and run Gate 1 validation.
4. Use isolated Git worktrees (`git worktree add ./worktrees/<lane>`) for all Wave 2 and Wave 3 coding subagents so parallel workers never collide on filesystem writes.

MANDATORY TEST-DRIVEN DEVELOPMENT (RED-GREEN-REFACTOR) & STABILITY RULES:
1. ZERO PRODUCTION CODE BEFORE FAILING TESTS (RED PHASE): In Wave 2, spawn 4 parallel subagents (`Subagent-Fixture`, `Subagent-TDD-Core`, `Subagent-TDD-Media`, `Subagent-TDD-UI`) to build the Stateful Mock Tapo Server (`:8443` HTTPS, `:8554` RTSP, `:8020` ONVIF + Chaos TCP Proxy) and author the complete failing test suite across 4 stability tiers:
   - Tier 1 (Unit/Property): Golden-master AES-CBC/GCM encryption vectors, `cnonce`/`snonce` hashing, DPAPI vault, and DTO serialization parity.
   - Tier 2 (Chaos Integration): Simulated Wi-Fi drops (TCP RST mid-stream requiring <3s auto-reconnect), forced `-40401` `stok` token expiration recovery, `-40404` 2-stream hardware concurrency semaphore enforcement, and corrupted `pcm_alaw` audio frame recovery.
   - Tier 3 (Endurance Soak & Leak Guard): 30-minute accelerated 2x2 grid soak test + 500 PTZ/toggle state changes asserting <180 MB RAM ceiling and Windows Job Object (`JOB_OBJECT_LIMIT_KILL_ON_JOB_CLOSE`) prevention of zombie FFmpeg processes.
   - Tier 4 (E2E UI & Live LAN Smoke): Playwright E2E UI tests for 1x1/2x2/3x3 grid switching, PTZ joystick/WASD controls, optimistic UI rollback on offline camera, and SD card VOD download.
2. Verify at Synchronization Barrier 2 that `mock_tapo_server` passes self-check and all Wave 2 tests fail (`Red`) as expected.
3. GREEN & REFACTOR PHASE (WAVE 3): Spawn 4 parallel implementation subagents (`Subagent-Impl-Control`, `Subagent-Impl-Media`, `Subagent-Impl-ONVIF`, `Subagent-Impl-UI`) in isolated worktrees. Wave 3 subagents are strictly forbidden from deleting or weakening Wave 2 tests; they must implement clean-room production code until 100% of Tier 1–4 tests pass (`Green`) and resource metrics meet Refactor targets.
4. Synchronize and merge worktrees at Barrier 3, execute the 4-Gate Closed-Loop Verification Pipeline (including `rea audit-release-exposure`), write checkpoints to `./specs/agent_checkpoint.json` after each barrier, and output the standalone Windows installer.

Begin immediately with Phase 1 & 2 host check, then launch Wave 1 (4 parallel REA subagents).
```