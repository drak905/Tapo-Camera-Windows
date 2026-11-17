# Wave 3 brief (TDD Green + Refactor)

You work ONLY inside your git worktree `E:\002.Github\054.Tapo_Camera_Windows\worktrees\<lane>`
(branch `wave3/<lane>`). Never touch the main checkout or other worktrees.

Read first (in your worktree): `docs/ARCHITECTURE.md`, your contract file(s) `docs/CONTRACT_*.md`,
`src/tapo_desktop/models.py`, `src/tapo_desktop/core/errors.py`, the stubs you must fill, the Wave 2
tests that target your modules, the Wave 2 reports in `artifacts/subagent_reports/` (they record
contract decisions the tests encode), and the relevant items of `specs/tapo_protocol_spec.json`.

Python: `E:\002.Github\054.Tapo_Camera_Windows\.venv\Scripts\python.exe` (3.14). Run pytest from the
worktree root, e.g. `& E:\002.Github\054.Tapo_Camera_Windows\.venv\Scripts\python.exe -m pytest tests/unit/test_x.py -q`.
For UI tests set `$env:QT_QPA_PLATFORM='offscreen'`. FFmpeg/ffprobe 8.1 on PATH. Runtime code may
use only stdlib, PyQt6 and cryptography (plus ctypes for Win32).

Rules
1. IMMUTABLE TESTS: do not modify, delete, skip, xfail or weaken anything under `tests/`
   (including fixtures and conftest). If you are convinced a test is wrong (contradicts the spec or
   is impossible), leave it failing, and explain precisely in your report with evidence.
2. Edit only the source paths your lane owns (docs/ARCHITECTURE.md ownership table). You may add
   private modules inside your own package directories. Keep the public names from the contract and
   the stubs (the Wave 2 lanes added some names; those are part of the contract now).
3. Production quality: type hints, docstrings for public API, no bare `except:`, no secrets in
   logs/reprs/exception text, use `logging.getLogger(__name__)`, threads are daemon + joinable,
   sockets/processes always closed (context managers / finally), timeouts on all network I/O.
4. Green, then Refactor: get your lane's tests passing, then clean up (dedupe, simplify, keep
   functions small) and re-run. Run your lane's whole test set at least twice to catch flakiness.
5. Known environment issue: AdGuard on this PC intercepts plain HTTP from python.exe over TCP,
   including loopback, which breaks the :8800 media protocol after the HTTP 200. Tests that skip via
   `require_loopback_http_stream` are expected skips. If a test fails ONLY because of this, prove it
   (e.g. the same flow passes over the fixture's in-memory socketpair path, or a raw-socket repro) and
   list it in the report as `env_blocked`.
6. When done: commit in your worktree. Write `artifacts/subagent_reports/<agent_id>.json`:
   `{"agent_id","status","tests_targeted","tests_passed","tests_failed","tests_skipped",
   "failing_tests":[{"id","reason"}],"env_blocked":[...],"files":[...],"notes":[...]}` and commit it.
7. Final answer: <= 20 lines.
