# Contributing to SysPerf

Thanks for considering it. This document covers what SysPerf will and will not
accept, and how to get a change merged.

## The rules that are not negotiable

1. **No fake data.** Every number on the page comes from a real OS source, and
   says when it is stale, unavailable or still being measured. Tests use
   fixtures; the shipped page never does.
2. **Changes to the PC are listed, reversible and gated.** A remediation must be
   registered in `optactions.py` with its risk, reversibility and rollback, so
   autonomy levels govern it. It must be shown in the app with how to undo it, be
   off in the Store build, and stop when `fixes.enabled` is false. Changes to
   Defender, the firewall, Windows Update, UAC, BitLocker, backups, recursive
   deletes or critical processes are refused at every level and will not be
   merged.
3. **Every change is tested and documented.** The suite and its 100% coverage
   gate must stay green. `SECURITY.md`, `docs/REQUIREMENTS.md`, the example
   config and the manual are checked against the code by tests, so update them
   in the same pull request.

The collector itself, `collect_win.ps1`, only queries. Keep it that way, so a
reader can audit it in one sitting.

## Design principles

Changes are judged against these:

1. **No machine is special.** No hardcoded baseline, target, hostname, vendor or
   app-specific tuning. Derive from measured capacity, or expose it as
   configuration.
2. **Honest about uncertainty.** A metric that cannot be evaluated yet reports
   `measuring` and stays out of the score. Never assume good.
3. **Score the sustained state.** Counts breathe. If a new metric fluctuates, it
   needs smoothing, and the window needs a reason.
4. **Recommendations are actionable.** A rule must say what was measured, why it
   matters, the concrete step, whether it needs administrator rights, and whether
   it is reversible. "Your memory is high" is not a recommendation.
5. **Portable by default.** Locale-independent APIs, no assumed drive letters, no
   assumed English display names, no assumed WSL, no assumed battery.
6. **Comments explain *why*.** The code says what it does. A comment that repeats
   it is noise; a comment explaining a non-obvious threshold or a guard against a
   specific failure mode is what stops the next person from "simplifying" it back
   into a bug.

## Getting set up

```bash
git clone <your fork>
cd sysperf
python3 -m venv .venv
.venv/bin/pip install -r requirements-dev.txt

PYTHONPATH=src .venv/bin/python -m pytest          # 96 tests
PYTHONPATH=src .venv/bin/python -m ruff check src tests
```

The test suite stubs the PowerShell layer, so it runs on Linux and macOS as well
as Windows. Verify anything touching `collect_win.ps1` on a real Windows machine:

```powershell
powershell -NoProfile -ExecutionPolicy Bypass -File src\sysperf\collect_win.ps1 -Full
powershell -ExecutionPolicy Bypass -File run.ps1 -Report
```

## Adding a metric

1. Emit it from `collect_win.ps1`. Fast tier for cheap counters; inside the
   `if ($Full)` block for anything that enumerates. Wrap anything that might not
   exist so a missing class returns null rather than failing the sample.
2. If it is carried between full samples, add the key to `CARRY_FORWARD_KEYS`.
3. If it should appear on a chart, add it to `SERIES_KEYS`.
4. To make it a scored criterion, add a row to `CRITERIA` in `config.py` and
   derive its target in `machine_profile.targets_for`. A row the machine does not
   report shows as *measuring*, never as a made-up value.
5. Add it to the fixture in `tests/conftest.py` and assert the behaviour.

## Adding a fix

1. Add a detector and a remediation to `fixes.py`. The detector must be quiet
   when there is nothing to fix, because a dashboard of unconditional advice
   trains people to ignore it.
2. Register it in `optactions.py` with its risk (`safe`, `caution` or
   `manual`), whether it is reversible, its rollback, and the metric that
   proves it worked.
3. Add tests for detection, preview, apply, and the measured before/after
   verdict.

## Pull requests

- One logical change per PR. Split refactors from behaviour changes.
- Tests for new behaviour, and a test that reproduces any bug you fix.
- `ruff check` clean and `pytest` green.
- Update `README.md` if you change behaviour a user would notice, and
  `CHANGELOG.md` under an `## [Unreleased]` heading.
- Explain *why* in the description, not just what. If you changed a threshold,
  say what measurement led you to that number.

### Commit messages

```
scoring: derive the trend gap threshold from the sample interval

A fixed 120 s gap meant any configured cadence above 40 s re-anchored the
window on every sample, so the trend never computed. The threshold is now
three times the observed median interval.
```

Imperative subject under 72 characters, prefixed with the area. Body explains the
reasoning.

## Reporting bugs

Include:

- `sysperf --version` and your Windows build (`winver`);
- how it was installed (installer, `run.ps1`, `pip`, WSL);
- what you expected and what happened;
- the output of `sysperf --once` if it is a collection problem. It contains
  process and device names, so read it before you share it.

## Feature requests that will be declined

- System changes that are not reversible, not opt-in, or not shown in the app with
  how to undo them (see the top of this document).
- "Registry tweaks" and service-disabling lists. They are machine-specific,
  frequently harmful, and reverted by policy on managed devices.
- Cloud upload, telemetry, or accounts.
- Third-party JavaScript in the dashboard. The charts are hand-written SVG so the
  page works offline and has nothing to keep patched.

## License

Contributions are accepted under the [Apache License 2.0](LICENSE). By opening a
pull request you confirm you have the right to contribute the code under that
license.
