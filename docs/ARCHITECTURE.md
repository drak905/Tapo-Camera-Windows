# Tapo Desktop for Windows: Architecture & Interface Contract

This file is the binding contract between the Wave 2 (Red: tests) and Wave 3 (Green:
implementation) lanes. Tests are written against the public names below; implementations must
provide exactly these names. Protocol facts come from `specs/tapo_protocol_spec.json` (ids quoted
as `[spec:<id>]`). Read the spec items before writing code or tests.

## Stack
- Python 3.14 (`.venv\Scripts\python.exe`), PyQt6 6.12 (UI), `cryptography` (AES), stdlib
  `http.client`/`ssl`/`socket`/`xml.etree` (no other runtime deps), FFmpeg 8.1 subprocess for
  RTSP decode (`ffmpeg` on PATH, override with env `TAPO_FFMPEG`).
- Tests: pytest 9, pytest-qt, pytest-timeout, hypothesis, psutil. Run: `.venv\Scripts\python.exe -m pytest`.
- Package root: `src/tapo_desktop` (import name `tapo_desktop`). `pyproject.toml` sets
  `pythonpath = ["src", "."]` so tests can `import tapo_desktop` and `import tests.fixtures...`.
- Pytest markers: `unit`, `integration`, `chaos`, `soak`, `hardware`, `ui`. Hardware tests skip
  unless env `TAPO_HW_IP` is set (plus `TAPO_HW_CLOUD_PASSWORD`, `TAPO_HW_CAM_USER`, `TAPO_HW_CAM_PASSWORD`).
- Default per-test timeout 60 s (pytest-timeout). Integration tests must use ephemeral ports (port 0).
- JSON on the wire: `json.dumps(obj, separators=(",", ":"), ensure_ascii=False)` (compact, Gson-like).

## Ownership (one lane writes each path; never edit another lane's files)
| Path | Wave 2 (tests + stubs) | Wave 3 (impl) |
|---|---|---|
| `src/tapo_desktop/models.py`, `core/errors.py`, `pyproject.toml`, `docs/` | orchestrator | orchestrator |
| `tests/fixtures/**`, `tests/conftest.py`, `tests/integration/test_fixture_*.py` | lane-fixture | (frozen) |
| `src/tapo_desktop/core/auth/**`, `core/control_client/**` | lane-tdd-core (stubs) | lane-impl-control |
| `src/tapo_desktop/core/onvif_service/**` | lane-tdd-core (stubs) | lane-impl-onvif |
| `src/tapo_desktop/core/media_engine/**`, `core/vod/**` | lane-tdd-media (stubs) | lane-impl-media |
| `src/tapo_desktop/services/**`, `ui/**`, `app.py`, `__main__.py` | lane-tdd-ui (stubs) | lane-impl-ui |
| `tests/unit/test_auth_*`, `test_vault*`, `test_dtos*`, `test_control_*`, `test_onvif_*`; `tests/integration/test_control_*`, `test_onvif_*` | lane-tdd-core | frozen |
| `tests/unit/test_media_*`, `test_vod_*`; `tests/integration/test_media_*`, `test_vod_*`; `tests/soak/**` | lane-tdd-media | frozen |
| `tests/e2e/**`, `tests/unit/test_ui_*`, `tests/unit/test_services_*` | lane-tdd-ui | frozen |

Stubs written in Wave 2 contain the exact public signatures and docstrings with bodies
`raise NotImplementedError`. Wave 3 replaces bodies; it may add private helpers/modules inside its
own directories, but must not delete, skip, xfail or weaken any Wave 2 test.

Shared models (already present): `tapo_desktop.models` = `StreamQuality`, `DayNightMode`,
`CredentialKind`, `Credentials`, `CameraConfig`, `DeviceInfo`, `Preset`, `RecordingClip`,
`MotionEvent`. Errors: `tapo_desktop.core.errors` = `TapoError`, `TapoConnectionError`,
`CertificateMismatchError`, `TapoAuthError`, `TapoLockedError`, `TapoProtocolError`,
`TapoDeviceError(code, method)`, `TapoSessionExpired`, `StreamLimitError`.

See `docs/CONTRACT_CORE.md`, `docs/CONTRACT_MEDIA.md`, `docs/CONTRACT_UI.md`, `docs/CONTRACT_FIXTURE.md`.
