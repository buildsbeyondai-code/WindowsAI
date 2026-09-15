# Contributing to SysPerf

Thanks for considering it. This document covers what SysPerf will and will not
accept, and how to get a change merged.

## The one rule that is not negotiable

**SysPerf does not write to the system.**

Every collector call is a query. The advisor produces text. A pull request that
disables a service, edits the registry, deletes files, changes a power plan or
"just" toggles a setting will be declined regardless of how useful the change is.

This is not caution for its own sake. It is the property that makes the tool
auditable: a user can read `collect_win.ps1`, confirm it only reads, and trust the
rest. Silent system modification is exactly why Windows "optimiser" tools have the
reputation they do.

The single permitted write path is SysPerf's own data directory: `baseline.json`,
`history.jsonl`, `adaptive_state.json`, `sysperf.config.json`,
`hw_watchlist.json`. Nothing else, ever.

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
powershell -NoProfile -ExecutionPolicy Bypass -File src\sysperf\scripts\collect_win.ps1 -Full
powershell -ExecutionPolicy Bypass -File run.ps1 -Report
```

## Adding a metric

1. Emit it from `collect_win.ps1`. Fast tier for cheap counters; inside the
   `if ($Full)` block for anything that enumerates. Wrap anything that might not
   exist so a missing class returns null rather than failing the sample.
2. If it is carried between full samples, add the key to `CARRY_FORWARD_KEYS`.
3. If it should appear on a chart, add it to `SERIES_KEYS`.
4. To make it a scored criterion, add it to `CRITERIA_SPEC` in `profile.py` and
   derive a target in `derive_targets`. Set `requires` so the row disappears on
   machines that do not report it.
5. Add it to the fixture in `tests/conftest.py` and assert the behaviour.

## Adding an advisor rule

```python
@rule
def _my_rule(latest: dict, sc: dict, cfg: dict, prof: dict):
    value = _num(latest.get("my_metric"))
    if not value or value < threshold:
        return None                     # stay silent unless there is something to say
    return _rec(
        "category.short-id",
        "One-line title, no jargon",
        "medium", "category",
        f"What was measured: {value}.",
        "Why it matters, in terms of what the user experiences.",
        ["The first concrete step.", "The second."],
        command="Optional command for the user to run themselves",
        needs_admin=False,
        impact="What improves if they do it.",
    )
```

Rules must be silent when there is nothing to report — a dashboard of
unconditional advice trains people to ignore it. Never call a threshold you
invented "recommended" without saying whose recommendation it is.

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
- the output of `sysperf --once` if it is a collection problem — set
  `"anonymize": true` first if it contains application names you would rather not
  share.

## Feature requests that will be declined

- Anything that changes system state (see the top of this document).
- "Registry tweaks" and service-disabling lists. They are machine-specific,
  frequently harmful, and reverted by policy on managed devices.
- Cloud upload, telemetry, or accounts.
- Third-party JavaScript in the dashboard. The charts are hand-written SVG so the
  page works offline and has nothing to keep patched.

## License

Contributions are accepted under the [Apache License 2.0](LICENSE). By opening a
pull request you confirm you have the right to contribute the code under that
license.
