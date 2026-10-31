# SysPerf: instructions for coding agents

Self-improving Windows 11 performance dashboard. Python 3.10+, Flask + waitress,
one self-contained page (`src/sysperf/static/index.html`), PowerShell collector.
The live install often runs from this checkout (editable install), so a change
here is a change to the running app after a restart.

Start with `docs/LEARNINGS.md` (the fast edit loop, where each change goes,
environment traps). Read only what the task needs:

- Structure, data flow, trust boundaries: `docs/ARCHITECTURE.md`
- Why things are the way they are: `docs/DECISIONS.md`
- Acceptance criteria and the tests that prove them: `docs/REQUIREMENTS.md`
- Current scope, status and next steps: `docs/PROJECT_STATE.md` (keep it current)
- Release gates: `docs/RELEASE_CHECKLIST.md`; incidents: `docs/OPERATIONS.md`
- Every user-facing value and control, with its evidence: `docs/FEATURE_AUDIT.md`
- The engineering playbook this project follows: `docs/UNIVERSAL_AI_SOFTWARE_ENGINEERING_STANDARD.md`.
  Read only the sections the task needs.

## Commands

```bash
python tools/affected_tests.py  # after each edit: only the tests the change can affect
python -m pytest -rs            # full suite (pre-push hook); Windows-only tests skip elsewhere
python -m ruff check src tests  # must be clean
python tests/testrunner.py      # the eight in-app gates, incl. 100% line coverage
```

## Rules

- Never show a value that is not measured. Missing data reads as
  *measuring*, *stale* or *unavailable*.
- Every state change is a POST that uses `_csrf()`. Add any new POST route to
  SECURITY.md's table, or `tests/test_security_doc.py` fails.
- A new module goes in `tests/covrun.py` `TARGETS` and needs 100% line coverage.
- Anything that changes the PC is registered in `optactions.py`, is reversible,
  and is shown in the app with how to undo it (Store build: Home → Optional features).
- Docs and the example config are checked against the code
  (`test_docs_truth.py`, `test_config_example.py`, `test_requirements_doc.py`).
  Update them in the same change.
- Run the full suite, the gates and the other platforms only before a push or a
  Store build, not after every edit; `tools/hooks/pre-push` enforces the full suite.
- Do not claim a check passed without running it; say UNVERIFIED instead.
