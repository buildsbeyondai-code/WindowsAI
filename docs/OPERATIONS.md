# Operating SysPerf

What "working" means, how to tell, what to do when it is not, and what past
incidents taught.

## Service objectives

SysPerf runs on one PC, so these are personal objectives, not contracts.
`tools/soak.py` measures them over any period; `/api/diagnostics` shows them
live.

| Objective | Target | Signal |
|---|---|---|
| Fresh data | A good sample under 10 min old, 99% of the time | `/api/health` `last_sample_age_s`, probe `collector` |
| Available | The dashboard answers within 2 s, 99.5% of the time while signed in | supervisor health checks; `supervisor_log.jsonl` |
| Loop alive | An auto-improve cycle at least every 25 min | probe `auto-improve` (stale after 1,500 s) |
| Quiet | No new ERROR in the last hour on a healthy PC | `/api/diagnostics` `health.errors_1h` |
| Light | Under 1% CPU and 150 MB RSS for SysPerf itself | `/api/diagnostics` `footprint` |

## Where to look

| Question | Look at |
|---|---|
| Is it up and sampling? | `GET /api/health` (`ok`, `last_sample_age_s`, `error`) |
| What went wrong recently? | Diagnostics tab, or `GET /api/diagnostics`: errors, workers, probes, supervisor log |
| Full history | `sysperf-runtime.log` in the install folder (rotated), `supervisor_log.jsonl` in the data folder |
| What did it change on the PC? | Home → What SysPerf did today; `autoimprove_log.jsonl`; `trayquit_log.jsonl` |
| Is the code healthy? | Self-test tab (`POST /api/selftest/run`), or `python -m pytest -rs` in the checkout |

## Incident response

1. **Contain.** If SysPerf is changing things you did not expect:
   - Set Autonomy to **Observe** (AI Self-Evolution tab). That stops the loop's
     unattended fixes.
   - The Explorer watchdog and tray auto-quit have their own switches on the
     App Control tab, and OneDrive control is on the Auto-Improve tab. In the
     Store build all of them are also on Home → Optional features.
   - The global kill switch is `PERFDASH_FIX_ENABLED=0`, then restart. Every
     part that can change the PC then only reports.
2. **Look.** Read Diagnostics, then the runtime log around the first error.
   Note the version (`sysperf --version`) and the time.
3. **Roll back** if a deployed AI patch is involved: AI Self-Evolution →
   **Roll back**, or `POST /api/codevolve/rollback`. The supervisor does this
   by itself when a patched service never becomes healthy.
4. **Restart.** Use `POST /api/restart` (when supervised), or end the
   sign-in task and start it again.
5. **Record.** Add an entry below: what happened, the root cause, the fix,
   and the regression test that now guards it.

Security problems go through SECURITY.md, not this log.

## Root-cause records

### 2026-10-24 · Auto-improve hook failed every five minutes, unnoticed

- **Impact.** For an hour the loop's post-cycle hook raised `TypeError` on
  every cycle. The dashboard showed nothing.
- **Root cause.** The service had no view of its own errors. They only went to
  a log file nobody reads.
- **Fix.** `health.py`: ErrorBuffer, worker liveness and freshness probes,
  `/api/diagnostics`, and notifications on service problems (4.1.0).
- **Guard.** `tests/test_health.py`, `tests/test_app_api.py::test_cycle_hook_notifies_breach_and_applied_fixes`.

### 2026-10-24 · Auto-improve deadlocked itself

- **Impact.** The loop stopped cycling.
- **Root cause.** A non-reentrant lock was taken again on the same thread.
- **Fix.** `RLock` (commit eb865b6).
- **Guard.** `tests/test_autoimprove.py`.

### 2026-10-25 · Tray auto-quit failed every 15 s (36 tracebacks)

- **Impact.** Tray auto-quit did nothing, and the runtime log filled with
  identical `ctypes.ArgumentError: expected LP_PMC instance` traces.
- **Root cause.** `health.py` set `argtypes` on the shared psapi
  `GetProcessMemoryInfo` function object. `trayquit.py` passed its own
  structure type, so its calls were rejected.
- **Fix.** Private `WINFUNCTYPE` prototypes in `health.py` (commit d15a9af).
  5.0.0 added `RepeatFilter` so a repeating failure cannot flood the log, and
  the DPAPI wrapper uses a private `WinDLL` for the same reason.
- **Guard.** `tests/test_trayquit_win.py::test_health_memory_read_does_not_break_trayquit`,
  `tests/test_health.py::test_repeat_filter_lets_a_burst_through_then_reports_the_rest`.

### 2026-10-25 · Self-evolution blocked all day by a red sandbox baseline

- **Impact.** Every scheduled cycle since 09:26 refused to run: "baseline test
  suite is not green", with two CRLF tests failing. That is the correct
  refusal, but it meant no evolution at all.
- **Root cause.** The test helper rewrote the fixture to CRLF and committed it.
  On a Windows checkout the file is CRLF already, so there was nothing to
  commit and `git commit` failed. Developer machines hid this, because their
  git configuration normalises line endings; the sandbox clears that
  configuration.
- **Fix.** `--allow-empty` in the helper (5.0.0). The full suite now passes inside
  the real `wsl-bwrap` sandbox: 1,169 passed.
- **Guard.** The same two tests, now passing in the sandbox. The release
  checklist runs the suite inside the evolution sandbox as well as directly.

### 2026-10-25 · Memory grew with every Diagnostics poll (Windows)

- **Impact.** About 1 KB of heap per `/api/diagnostics` call on Windows, which
  the page polls, so memory crept up over days. Tray-quit rebuilt its structure
  types every 15 s in the same way.
- **Root cause.** `health._win_rss_mb()` defined a ctypes `Structure` and a
  `POINTER` prototype on every call. ctypes caches each new class's pointer type
  forever.
- **Fix.** Build the types once (`health._win_mem_api`, `trayquit.WinApi.__init__`,
  a single DPAPI wrapper) in 5.0.0.
- **Found by.** `tests/test_soak.py`, which passed on Linux (20 KB) and failed on
  Windows (1,039 KB over 1,000 requests). Before 5.0.0 the suite had never been
  measured for leaks on Windows.
- **Guard.** `tests/test_soak.py`, `tests/test_trayquit_win.py::test_repeated_reads_do_not_grow_the_heap`.

### 2026-10-25 · DNS rebinding could reach the mutating API

- **Impact.** None observed. A UAISES audit found it: the server accepted any
  `Host`, and `GET /api/fixes` returns the CSRF token. A malicious page could
  therefore rebind to 127.0.0.1 and call any POST route.
- **Root cause.** The threat model treated "loopback plus token" as enough,
  and never considered a page that becomes same-origin.
- **Fix.** `hostguard.py`: Host allowlist and Origin check, plus a CSP (5.0.0).
- **Guard.** `tests/test_hostguard.py`, `tests/test_security_doc.py`.

### 2026-10-25 · SECURITY.md described a different product

- **Impact.** The security policy said there were four mutating endpoints
  (there were 28) and described features that did not exist.
- **Root cause.** Nothing tied the document to the code.
- **Fix.** Rewritten from the route table (5.0.0).
- **Guard.** `tests/test_security_doc.py` fails on any mismatch.

## Rollback of a release

The scripted install runs from a git checkout:

```powershell
cd "$env:LOCALAPPDATA\SysPerf\app"
git log --oneline -5                  # find the last good commit or tag
git switch --detach <commit>          # or: git reset --keep <commit> on your branch
```

Then restart the sign-in task. The data directory is not touched by code
changes, and older versions read newer state files except where the
CHANGELOG says otherwise. For the Store build, reinstall the previous
`.msix`, or uninstall and reinstall from the Store. Data in
`%LOCALAPPDATA%\Packages\...` is kept unless you uninstall.
