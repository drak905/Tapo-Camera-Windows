# Wave 1 common brief (read-only REA static analysis)

Project root: `E:\002.Github\054.Tapo_Camera_Windows`
Goal: extract a clean-room protocol specification from the official TP-Link Tapo Android app
(`com.tplink.iot` 3.22.806) so a native Windows client for Tapo IP cameras can be written
against the LOCAL LAN interfaces of the camera (HTTPS :443 JSON API, RTSP :554, ONVIF :2020,
local media port :8800). This is interoperability research for the owner's own cameras.

## Inputs (read-only, never modify)
- Decompiled Java (JADX 1.5.6, no resources): `artifacts\jadx_base\sources\` (~60k files, 515 MB).
  The camera-local code lives mainly in `com\tplink\libtapocameranetwork\` (model\Method.java,
  model\Section.java, model\Module.java, model\SecureAccount.java, model\LoginResult.java,
  util\SecureUtils__SecureUtilsKt.java, net\, transport\, protocol\, business\).
  Also relevant: `com\tplink\libtpstreamclientmanager`, `libtplivemedia`, `libtpvodmedia`,
  `libtpvideodownloadmedia`, `libtpstreamconnectionsocket`, `libtpcommonstream`,
  `libtapotpap`, `libtapocameraapi`, `libtapocameraui`.
- Base APK: `artifacts\apk\unpacked\com.tplink.iot.apk`; arm64 split: `artifacts\apk\unpacked\config.arm64_v8a.apk`.
- Selected native libs (arm64): `artifacts\apk\native_arm64\*.so`.
- Tools: ripgrep `rg` (FAST - always use it, never PowerShell Select-String over the tree),
  REA CLI `rea` 6.3.0 (`rea inspect-android-class <apk> <fqcn>`, `rea inspect-android-method <apk> <fqcn> <method> [--overload-index N]`,
  `rea inspect-android-package <apk>`, `rea inspect-artifact <file>`, `rea decompile`, `rea function`, Ghidra 12.1.4 backend is healthy;
  run `rea <cmd> --help` for options, use `--format json` and `--token-limit` to keep output small).
  JADX CLI if needed: `artifacts\tools\jadx\bin\jadx.bat`. Apktool: `java -jar artifacts\tools\apktool.jar`.

## Rules
1. Read-only on all inputs. Write ONLY your two output files (named in your task).
2. Clean-room: record FACTS (method names, JSON field names, value enums, algorithms, constants,
   ports, URL paths, header names, error codes, state machines). Do NOT paste proprietary code
   blocks; evidence snippets must be <= 2 short lines.
3. Every spec item needs evidence: `{"source":"jadx","file":"<path relative to sources>","line":N,"snippet":"..."}`
   and/or `{"source":"rea","command":"rea ...","summary":"..."}`. Use at least a few REA commands
   (e.g. `rea inspect-android-class` / `rea inspect-android-method` on the key classes) so the
   evidence trail includes REA output. If something comes from public knowledge (pytapo etc.)
   rather than the APK, mark `"confidence":"external"`; APK-confirmed = `"verified"`;
   reasoned from partial evidence = `"inferred"`.
4. Be efficient: target finishing within ~30-40 minutes of work. Prefer depth on what a
   desktop client needs over exhaustive coverage of every cloud/IoT feature.
5. Output spec JSON shape (top level):
```json
{
  "spec_id": "<id>",
  "apk": {"package": "com.tplink.iot", "version_name": "3.22.806", "version_code": "7276"},
  "summary": "...",
  "items": [
    {"id": "dotted.unique.id", "kind": "<algorithm|request|response|enum|constant|endpoint|error_code|state_machine|header|stream>",
     "description": "...", "value": { ... free-form structured facts, exact JSON payload templates ... },
     "evidence": [ ... ], "confidence": "verified|inferred|external"}
  ],
  "unresolved": [ {"topic": "...", "detail": "..."} ]
}
```
6. Report file `artifacts\subagent_reports\<agent_id>.json`:
```json
{"agent_id":"...","status":"ok|partial|failed","spec_file":"specs/...","items_total":N,
 "items_verified":N,"rea_commands_run":["..."],"key_findings":["..."],"unresolved":["..."]}
```
7. Validate your JSON parses (`python -c "import json;json.load(open(r'...',encoding='utf-8'))"`).
8. Final answer to the orchestrator: <= 25 lines summarising key findings and file paths.
