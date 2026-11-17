# Wave 2 brief (TDD Red phase)

You work ONLY inside your git worktree `E:\002.Github\054.Tapo_Camera_Windows\worktrees\<lane>`
(branch `wave2/<lane>`). Never touch the main checkout or other worktrees.

Read first (in your worktree): `docs/ARCHITECTURE.md`, the contract file(s) for your lane
(`docs/CONTRACT_*.md`), `src/tapo_desktop/models.py`, `src/tapo_desktop/core/errors.py`, and the
relevant items of `specs/tapo_protocol_spec.json` (search it with python/json or rg; it is large).

Python: `E:\002.Github\054.Tapo_Camera_Windows\.venv\Scripts\python.exe` (3.14; PyQt6, cryptography,
pytest, pytest-qt, pytest-timeout, hypothesis, psutil installed). Do not pip install anything else
without strong need; if you do, say so in your report. FFmpeg/ffprobe 8.1 are on PATH.
Run tests from the worktree root: `& E:\002.Github\054.Tapo_Camera_Windows\.venv\Scripts\python.exe -m pytest <paths> -q`.

Rules
1. Test-writing lanes (core, media, ui): write the failing test suite + interface stubs. Stubs =
   exact public signatures from the contract, docstrings, bodies `raise NotImplementedError`. No
   production logic in stubs (constants/enums/dataclasses from the contract are fine).
   Tests must import cleanly (collection succeeds) and then FAIL (NotImplementedError / assertion),
   except trivially-passing ones you should avoid. Confirm with pytest that collection works and
   the tests are red; report counts.
2. Tests that need the mock camera use the fixtures named in `docs/CONTRACT_FIXTURE.md`
   (`mock_camera`, `legacy_mock_camera`, `dual_lens_camera`, `chaos_proxy`, `ffmpeg_path`,
   `tmp_data_dir`). The fixture lane builds them in parallel, so in your worktree those tests will
   error with "fixture not found"; that is expected. Write them exactly against the contract API.
3. Make tests deterministic, fast (unit < 1 s each; integration < 20 s each) and specific: assert
   exact payloads, exact golden vectors from the spec, state transitions, timings with tolerance.
   Mark tests with the pytest markers (unit, integration, chaos, soak, hardware, ui).
   Use hypothesis for property tests where natural (round-trips, parsers never raising non-Tapo errors).
4. Only create/modify files your lane owns (ownership table in docs/ARCHITECTURE.md). If you
   need a shared helper, put it in your own test file or a lane-private helper module
   (e.g. `tests/unit/_core_helpers.py` for core).
5. When done: `git add -A; git commit -m "<lane>: ..."` in your worktree (user already configured).
   Write `artifacts/subagent_reports/<agent_id>.json` inside your worktree:
   `{"agent_id","status","tests_total","tests_red","tests_passing_unexpectedly","files":[...],
   "notes":[...],"contract_questions":[...]}` and commit it too.
6. Final answer: <= 20 lines (what you built, counts, anything the orchestrator must know,
   e.g. contract ambiguities you resolved and how).
