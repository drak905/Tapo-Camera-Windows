# Phase 2 lane brief (strict TDD inside the lane)

Work ONLY in your worktree `E:\002.Github\054.Tapo_Camera_Windows\worktrees\p2-<lane>` (branch
`phase2/<lane>`). Read: `docs/CONTRACT_PHASE2.md` (binding), `docs/ARCHITECTURE.md`, the Phase 1
contract docs relevant to you, `src/tapo_desktop/models.py`, `specs/cloud_account_spec.json` and the
spec items named in the contract (search `specs/tapo_protocol_spec.json`), and the existing code you
build on.
Python: `E:\002.Github\054.Tapo_Camera_Windows\.venv\Scripts\python.exe`; run pytest from the
worktree root; `$env:QT_QPA_PLATFORM='offscreen'` for Qt. Runtime deps: stdlib, PyQt6, cryptography.

Process (mandatory order):
1. RED: write the tests for your lane under `tests/phase2/` (plus lane-owned fixtures named in the
   contract), with stubs (signatures + `raise NotImplementedError`) for new modules. Run them, confirm
   they fail for the right reason, and `git commit -m "<lane>: red tests"`.
2. GREEN: implement until your tests pass; commit.
3. REFACTOR + REGRESSION: clean up; then run the WHOLE suite
   `-m "not soak and not hardware"` (about 4 min) and make sure all Phase 1 tests still pass. Never
   modify, skip or weaken existing Phase 1 tests. Fixture changes must be additive (existing behaviour
   unchanged; the fixture self-check must still pass). Commit.
4. Write `artifacts/subagent_reports/<agent_id>.json` (status, tests added, pass/fail, files,
   decisions, open issues) and commit it. Final answer <= 20 lines.
Production quality as in Phase 1: type hints, docstrings, no secrets in logs/reprs, timeouts on all
I/O, threads/sockets/processes always cleaned up.
