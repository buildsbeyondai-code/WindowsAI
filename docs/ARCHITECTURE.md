# SysPerf architecture

One Python process (Flask on waitress) serving one self-contained page. Inside
it, background workers sample the PC, score it, act on it within limits, and
record what happened. A supervisor process keeps it alive. Decisions behind
this shape are in [DECISIONS.md](DECISIONS.md); requirements and targets are
in [REQUIREMENTS.md](REQUIREMENTS.md).

## Layers

```
 Browser / desktop shell (WebView2)          http://127.0.0.1:5056
        │  GET telemetry · POST changes (CSRF token)
        ▼
 ┌─ app.py ───────────────────────────────────────────────────────────┐
 │ hostguard (Host/Origin) → routes → JSON         security headers   │
 └──────────────┬──────────────────────────┬──────────────────────────┘
                │ reads                    │ acts (allowlisted, gated)
 ┌─ measure ────▼─────────────┐   ┌─ act ──▼──────────────────────────┐
 │ collector ◄─ pwsh_session  │   │ fixes · optactions (autonomy)     │
 │   └ collect_win.ps1        │   │ autoimprove (scan→RCA→fix→verify) │
 │ livestats (Win32, ctypes)  │   │ procwatch · trayquit · appctl     │
 │ hardware · browser · netopt│   │ onedrive · session_policy         │
 └──────────────┬─────────────┘   │ evolve (remediation candidates)   │
                │ snapshots       │ codevolve ◄─ llm (opt-in)         │
 ┌─ understand ─▼─────────────┐   └──────────────┬────────────────────┘
 │ scoring · adaptive         │                  │ records
 │ machine_profile · trends   │   ┌─ remember ───▼────────────────────┐
 │ rca · cpuwatch · learning  │   │ historydb (SQLite) · evodb (audit)│
 │ target_review · components │   │ actions · notify · settings_hub   │
 └────────────────────────────┘   └───────────────────────────────────┘
 Operations: supervisor (restart/rollback) · health (probes, error buffer,
 RepeatFilter, footprint) · selftest (in-app gates) · version_check
 Platform: config · platform_paths · background_process · store_mode · desktop
```

Dependencies only point down or across: `config` and `platform_paths` are
leaves, `app` wires everything and owns no logic beyond request handling.

## Modules

| Layer | Module | Responsibility |
|---|---|---|
| Entry | `__main__.py`, `cli.py` | `python -m sysperf`: serve, `--once`, `--report`, `--where`, `--supervise` |
| | `app.py` | Flask routes, CSRF, security headers, worker start-up, waitress |
| | `hostguard.py` | Refuses foreign `Host`, and cross-origin POSTs (DNS rebinding) |
| | `desktop.py` | MSIX/desktop entry: starts the service, opens a WebView2 window |
| | `selfupdate.py` | Store app: closes itself when Windows stages its update, reopens on the new version |
| Measure | `collector.py` | Background sampler, rolling history, snapshot assembly |
| | `pwsh_session.py` | One warm PowerShell process running `collect_win.ps1`; one-shot fallback |
| | `livestats.py` | Real-time headline values straight from Win32 APIs |
| | `hardware.py`, `browser.py`, `netopt.py` | Device health, browser process ownership, Wi-Fi channel analysis |
| Understand | `scoring.py`, `adaptive.py`, `machine_profile.py` | Criteria, personal-best scoring, per-machine baseline and targets |
| | `trends.py`, `rca.py`, `cpuwatch.py`, `learning.py` | Long-term drift, root cause of shortfalls, CPU attribution, learning view |
| | `target_review.py`, `components.py`, `version_check.py` | Unrealistic targets, component upgrade advice, latest versions |
| Act | `fixes.py`, `optactions.py` | Reversible fixes; action registry and autonomy levels |
| | `autoimprove.py`, `evolve.py` | The 5-minute loop; evolution of remediation candidates |
| | `procwatch.py`, `trayquit.py`, `appctl.py` | Explorer watchdog, tray auto-quit, start/stop top apps |
| | `onedrive.py`, `onedrive_control.py`, `session_policy.py` | OneDrive control and lock-state policy |
| | `codevolve.py`, `llm.py` | AI source evolution (sandboxed, review branches, Apply/rollback); model providers, DPAPI key file |
| Remember | `historydb.py`, `evodb.py` | SQLite history (14 d raw, 400 d hourly); evolution/audit store |
| | `actions.py`, `notify.py`, `settings_hub.py` | Human action queue, inbox and toasts, validated settings |
| Operate | `supervisor.py`, `health.py`, `selftest.py` | Restart/rollback; diagnostics and log hygiene; in-app quality gates |
| Platform | `config.py`, `platform_paths.py`, `background_process.py`, `store_mode.py` | Settings resolution, tool discovery, hidden child processes, Store opt-in |

PowerShell scripts in the package: `collect_win.ps1` (the collector),
`standby_broker.ps1` (optional elevated helper), and the
`optimize-*.ps1` / `restart-wsl-and-verify.ps1` / `daily-app-reset.ps1`
remediation helpers.

## Data flow

1. **Sample.** Every `sample_seconds` (60 s), `collector` asks
   `pwsh_session` to run `collect_win.ps1`. Every fifth sample is a full one.
   The JSON result is stored in `historydb` and kept in memory (240 samples,
   4 hours).
2. **Score.** `scoring` and `adaptive` turn the latest sample and history
   into the scorecard, against `machine_profile`'s baseline.
3. **Serve.** Routes read the snapshot (cached for `SNAPSHOT_TTL`). `livestats`
   gives second-fresh headline values without a process spawn.
4. **Improve.** Every 5 minutes `autoimprove` scans, runs `rca` when the score
   is under the threshold, applies allow-listed fixes allowed by the autonomy
   level, then verifies the measured effect on later samples and learns from it.
5. **Evolve (opt-in).** `codevolve` asks the model for a patch, tests and
   benchmarks it in a sandbox, and keeps a winner on a review branch. Only an
   explicit Apply fast-forwards the checkout and restarts the service.
6. **Upgrade components and their dependencies.** Each cycle first takes one
   outdated Components row (`upgrader.REGISTRY` by its `upgrade.via`).
   SysPerf's own Python packages go through `depupgrade`:

   ```
   newer release on PyPI ──► pin moves (requirements.txt / -dev / pyproject)
        │                        │
        │                        ▼
        │        uv pip compile, as CI checks the lock  ──► requirements.lock
        │        (new or newer dependencies come in          (every package hashed)
        │         with their hashes)
        ▼
   uv resolves the same set for the sandbox's Linux Python ──► every wheel
   unpacked into the worktree ──► full suite in the sandbox ──► retained
        │
        ▼
   apply: pip install --require-hashes (lock) + dev requirements, into
   SysPerf's venv and the sandbox venv ──► restart ──► verify, else roll back
   ```

   A package the resolver adds to the lock becomes a Components row with a live
   PyPI feed (`components.catalog`, `version_check.SOURCES`), so it is tracked
   and upgraded like the rest. Software outside the pins (Python, Ubuntu, WSL,
   Edge, drivers) uses its own upgrader, tested in isolation and rolled back on
   failure. A component bundled in another (the WSL kernel) waits for its
   carrier. An upgrade rejected before its tests passed is retried after the
   next commit; one that failed after applying waits for a newer release.

## Trust boundaries

| Boundary | Threat | Control |
|---|---|---|
| Browser ↔ service | Other web pages (CSRF, DNS rebinding) | Host/Origin guard, per-process token on every POST, CSP, `X-Frame-Options` |
| Service ↔ OS | Command injection, unsafe changes | Fixed script paths, no shell; allow-listed actions; reversible-only autonomy; forbidden categories |
| Service ↔ model provider | Prompt injection, hostile patches | Repository content marked as data; schema validation; protected files; security scan of diffs; sandboxed execution; human Apply |
| Service ↔ disk | Secret disclosure | DPAPI-encrypted key file (Windows) or `0600`; keys redacted from every log record |
| Service ↔ internet | Data leakage | Only version feeds and the configured model provider; SSID/BSSID masked in `/api/metrics` |

## State

All state is in the data directory (`sysperf --where`), never the install
directory.

| File | Holds |
|---|---|
| `history.db` | Raw samples (14 days) and hourly rollups (400 days) |
| `sysperf.db` | Evolution candidates, measured effectiveness, hash-chained audit log |
| `baseline.json` | The machine's starting point |
| `adaptive_state.json`, `trends_daily.json` | Personal bests; daily and hourly rollups |
| `autoimprove_state.json`, `autoimprove_log.jsonl` | Loop mode, cooldowns, learned effectiveness; one line per cycle |
| `autonomy.json`, `actions_state.json` | Autonomy level and enabled actions; human action queue |
| `evolution_settings.json`, `evolutions/` | Self-evolution settings and artefacts |
| `notifications.json`, `trayquit_state.json`, `onedrive_control.json` | Inbox, tray consent, OneDrive switch |
| `supervisor_log.jsonl`, `pending_apply.json` | Restarts; a deployed patch awaiting health confirmation |
| `secrets/anthropic.key` | Model API key (DPAPI or `0600`) |

## Failure modes

| Failure | Detected by | Response |
|---|---|---|
| PowerShell hangs or dies | `pwsh_session` deadline (120 s) | Kill, one-shot fallback for that sample, fresh session next time |
| Collector stops producing samples | health probe (stale after 600 s); supervisor | `/api/health` `ok: false`; supervisor restarts the service |
| Service crashes or stops answering | supervisor health checks | Restart with back-off |
| Applied patch never becomes healthy | supervisor, `pending_apply.json` | `git reset --keep` to the backup tag, restart |
| Worker raises repeatedly | ErrorBuffer, RepeatFilter | Shown on Diagnostics; the log keeps 3 per 10 minutes plus a count |
| Corrupt state or database | load functions | Corrupt rows skipped; a corrupt `sysperf.db` is quarantined, not fatal |
| No sandbox for generated code | `codevolve` | Validation blocked; candidates are never executed |
| Sleep/resume | supervisor | Fresh grace period, no false restart |
