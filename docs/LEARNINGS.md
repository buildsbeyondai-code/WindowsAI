# Learnings: the fast path for changes

Read this before a change. It records what cost hours before, so the next
change does not pay for it again. Add a line when something surprises you.

## The edit loop (minutes, not hours)

| Step | Command | When |
|---|---|---|
| Tests the change can affect | `python tools/affected_tests.py` | after every edit |
| Same, against a branch base | `python tools/affected_tests.py --base main` | before asking for review |
| Full suite + lint | `python -m pytest -rs` and `python -m ruff check src tests` | the pre-push hook does it |
| The eight gates (100% coverage) | `python tests/testrunner.py` | before a push / release |
| Suite inside the evolution sandbox | `python tools/sandbox_suite.py` (the pre-push hook runs it) | before a push |
| Linux + Python matrix | the CI matrix on push (ubuntu, windows, py3.10+) | never by hand |
| MSIX build and install | `docs/RELEASE_CHECKLIST.md` | Store submissions only |

`affected_tests.py` selects by import, by file name (`test_<module>*.py`) and
by mention (docs and coverage tests name the files they check). A change to
`conftest.py`, `covrun.py`, `testrunner.py`, `uicov.py`, fixtures or
`pyproject.toml` selects everything. It is a fast filter, not the gate: the
full suite still runs before anything reaches GitHub (`tools/hooks/pre-push`,
enabled with `git config core.hooksPath tools/hooks`).

## Why the full suite is slow (measured 31 Oct 2026)

The Windows suite took 608 s. About half of it is `test_codevolve.py`: each
sandboxed test runs a real evolution cycle (worktree, fixture suite inside
bubblewrap, benchmark), 6–30 s each. `test_profile_evidence_points_at_the_hot_function`
is load-sensitive: it can fail while another suite or an upgrade runs at the
same time, and passes alone. Do not run two suites at once on this PC.
The sandbox tests skip themselves (25 skips instead of 2) when the WSL probe
fails, e.g. while an Ubuntu upgrade loads WSL; a skip count far above 2 means
rerun them, not that they passed.

## Where a change goes

| To change | Edit | Tests that cover it |
|---|---|---|
| A Components row | `components.json` (+ `upgrade.via`) | `test_components.py`, `test_upgrader.py` |
| How a component upgrades | `upgrader.py` (one class per `via`) | `test_upgrader.py` |
| When self-evolution runs | `codevolve.py` `next_due` / `gate` / `upgrade_gate` | `test_codevolve*.py` |
| A dashboard card | `static/index.html` (search the `$("id")` render function) | `test_e2e_*.py`, `test_accessibility.py` |
| A new POST route | `app.py` + SECURITY.md table | `test_security_doc.py`, `test_app_api.py` |
| A setting | `config.py` + `sysperf.config.example.json` + docs | `test_config_example.py`, `test_docs_truth.py` |
| A new module | add to `tests/covrun.py` `TARGETS` | the coverage gate |

## Self-evolution throughput (5.4.1)

- A cycle takes 2–9 minutes. The waiting is the cost: cycles start every
  `interval_s` (6 h) and the component lane makes one upgrade per cycle, so
  7 outdated components took ~42 h. Since 5.4.1 a cycle that upgraded a
  component schedules the next after `cooldown_s` (30 min), and component
  cycles no longer use up `max_per_day` (that limit is for the model's work).
- Standalone upgrades (Windows Update, Edge, WSL, winget) all run in one
  `upgrade_pass` every 6 h, so they are not queued one by one.
- Edge and WebView2 "handed off" means Edge Update downloaded the build; the
  version moves only when Edge restarts. Drivers depend on Windows Update
  offering one.
- The Ubuntu LTS upgrade exports the distro (~4 GB), imports a test copy and
  upgrades it before touching the real one: expect an hour or more, during
  which the cycle lock blocks other cycles.
- A `follows` row (the WSL kernel) is only an upgrade when its carrier (WSL)
  is outdated; otherwise it reads *waiting for WSL release*.
- CI does not run the suite *inside* bubblewrap, and that is what every
  evolution cycle's baseline is. Its `/tmp` is a small tmpfs: a test needing
  real free disk passed on Windows, Linux and CI and still went red there,
  stopping all cycles (5.4.2). Hence `tools/sandbox_suite.py` in pre-push.
- Do not edit the live checkout while an upgrade cycle is testing: some
  upgraders (Python) run the suite in the checkout itself, and a mid-edit tree
  rejects a good upgrade. Check `GET /api/codevolve` `state` first. A
  suite-rejected upgrade is retried after the next commit (5.4.2).
- A rule written for one kind of package must check that it applies: the
  hash-lock dependency check also ran for dev tools and refused every pytest
  upgrade (fixed in 5.4.4). If an upgrade keeps getting rejected with the same
  reason, test the rule, not just the package.
- Nothing runs while the PC sleeps (a 2.5-day gap on 28–30 Oct 2026 was sleep).
- The Components "Assessment" date is the latest live version check (daily);
  `catalog_date` is when the hand-reviewed fallbacks were last edited.

## Microsoft Store

- Every update failed once with 0x80073D02 while the window was open: Windows
  will not swap a running package, and the Store's first try does not force it.
  `selfupdate.py` closes the window on the *staging* event (~10 s before the
  swap) and relaunches through WMI, which runs outside the package. A process
  started by the app itself (cmd, powershell) belongs to the package and would
  block the swap again.
- The fix only helps updates *from* a version that has it.
- Analytics API: `/v1.0/my/analytics/appacquisitions`, `funnel`; it rate-limits
  (429) at about one call per few seconds. Client: `%LOCALAPPDATA%\SysPerf\store-tools`.

## Environment traps (WSL driving the Windows install)

- The checkout is `C:\Users\<user>\AppData\Local\SysPerf\app`; the venv is the
  sibling `..\venv` (not `app\venv`). Run tests with `..\venv\Scripts\python.exe`.
- WSL's `git` sees every file as modified (CRLF). Use
  `/mnt/c/Program Files/Git/cmd/git.exe`; `affected_tests.py` does this itself.
- WSL's `curl` cannot reach 127.0.0.1:5056; use `curl.exe` from Windows.
  POSTs need the token from `GET /api/fixes` and Host `127.0.0.1`.
- `%TEMP%\pytest-of-<user>` can have an unreadable ACL: pass `--basetemp`
  (`affected_tests.py` adds `%LOCALAPPDATA%\SysPerf\pt-tmp` on Windows).
- Source files here are CRLF. `sed 's/...$/.../'` silently matches nothing
  (the `$` sits before `\r`); a missed import then broke /api/home live. Edit
  with Python or the Edit tool, and run the affected tests *before* restarting
  the live service, never after.
- If `/api/restart` cannot be reached (a broken build answers 500 on
  /api/fixes), restart with `Stop-ScheduledTask` / `Start-ScheduledTask
  'SysPerf Dashboard'`.
- PowerShell from bash: put the script in a `.ps1` file and run it with
  `-File`; backticks and `$env:` inside `-Command` strings get mangled by bash.
- `sysperf.db` cannot be opened from WSL while the service runs (disk I/O
  error); copy `sysperf.db` and `sysperf.db-wal` first, or use the API.
- Restart the live service through `POST /api/restart` (the supervisor), not
  by killing pythonw.
