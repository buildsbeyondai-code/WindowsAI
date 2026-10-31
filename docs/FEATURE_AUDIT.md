# Feature audit matrix

Every user-facing value and control, the real thing behind it, how it is
tested, and the evidence (UAISES §4). States are PASS, FAIL or UNVERIFIED.
A state is PASS only with evidence from a real run on Windows, not from
reading the code. Audited 2026-10-25 against 5.0.0 on the reference laptop.

## Values shown

| Feature | Claimed function | Real provider | Test | Evidence | State |
|---|---|---|---|---|---|
| Live CPU % | Share of all cores busy right now | `GetSystemTimes` (livestats) | `test_livestats.py`, `test_livestats_win.py` | 7.2–7.8% vs `\Processor(_Total)` 3.0% (sampled seconds apart; the counter is instantaneous) | PASS |
| Available / total RAM | Free and total physical memory | `GetPerformanceInfo` | `test_livestats_win.py` | 1,700 vs 1,653 MB; 7,836 = 7,836 MB (CIM) | PASS |
| Commit charge / limit | Committed and limit | `GetPerformanceInfo` | `test_livestats_win.py` | 11,758 vs 11,823 MB; 14,597 = 14,597 MB (`\Memory` counters) | PASS |
| Processes, uptime | Running processes; time since boot | `GetPerformanceInfo`, `GetTickCount64` | `test_livestats_win.py` | 262 = 262; 15.67 = 15.67 h (CIM) | PASS |
| Scorecard rows | Current vs this PC's baseline and target | `collect_win.ps1` via `pwsh_session`; `machine_profile` | `test_scoring.py`, `test_collector.py`, CI collector smoke | 970 stored samples; baseline captured 2026-10-24 12:03 | PASS |
| Data freshness | How old the last good sample is | collector `last_ok_at` | `test_health.py`, `test_app.py::test_health` | `last_sample_age_s` 13 s; probe stale after 600 s | PASS |
| Service health | Workers alive, errors, own footprint | `health.py` | `test_health.py`, `test_soak.py` | 7 workers alive, 0 errors/h, 0.06% CPU, 27.9 MB | PASS |
| History chart | One metric over hours to months | SQLite `historydb` | `test_historydb.py`, `test_e2e_v41.py::test_history_chart_follows_metric_and_range` | 20 MB DB, 14 d raw + rollups | PASS |
| Components | Installed vs newest public version | Local probes + public release feeds | `test_components.py`, `test_version_check.py` | Feeds read daily; results in `components_latest.json` | PASS |
| Self-test gates | The project's own quality gates | `tests/testrunner.py` | `test_selftest.py` | Shows `unknown` until run; never a fake pass | PASS |

## Controls

| Feature | Claimed function | Real side effect | Test | Evidence | State |
|---|---|---|---|---|---|
| Fix: Flush DNS | Clears the resolver cache | `Clear-DnsClientCache` | `test_fixes.py`, `test_app.py::test_fix_apply_ok` | Ran 3 times on this PC (last 2026-10-24 15:24 UTC). Measured effect on `net_remote`: none (ledger EMA −0.04), and the loop records that | PASS |
| Fix: trim working sets, clear standby | Frees idle memory | `EmptyWorkingSet`; standby broker | `test_fixes.py` | Implemented and tested with fakes. Standby clear needs the elevated broker, which is not running here | UNVERIFIED |
| Tray auto-quit | Quits allowed tray apps after their window closes | `TerminateProcess` (WinApi) | `test_trayquit.py`, `test_trayquit_win.py` (real child process) | WhatsApp freed 104 and 96 MB; ChatGPT freed 1,564 MB (`trayquit_log.jsonl`) | PASS |
| Explorer watchdog | Restarts Explorer after sustained high CPU | Explorer restart | `test_procwatch.py` | Enabled, 0 actions: Explorer at 0.02% never breached 25% | UNVERIFIED (no live trigger) |
| App Control start/stop | Starts or stops a top-CPU app | Process start/stop | `test_appctl.py` | Tested with fakes and allowlist checks; not exercised live by this audit | UNVERIFIED |
| OneDrive start/stop | Starts or stops OneDrive | OneDrive process | `test_onedrive.py`, `test_onedrive_control.py` | Tested with fakes; not exercised live by this audit | UNVERIFIED |
| Autonomy level, loop mode | Decides what may run unattended | Persisted policy | `test_optactions_evodb.py`, `test_autoimprove.py` | Live `/api/autonomy` and `/api/store` match the cycle log | PASS |
| Optional features (Home) | Turns each system-changing feature on or off | Each owner's state file | `test_store_mode.py`, `test_e2e_v41.py` | Live `/api/store` lists 5 features with undo text | PASS |
| Settings save/reset/import/export | Validated settings, all or nothing | `sysperf.config.json` | `test_settings_hub.py`, `test_e2e_v41.py::test_settings_form_saves_and_offers_a_restart` | Real browser (Playwright) | PASS |
| Notifications | Inbox and Windows toasts | Toast via PowerShell | `test_notify.py`, `test_e2e_v41.py::test_bell_shows_unread_lists_items_and_marks_them_read` | Inbox live (4.4 KB); toast display not checked by eye | PASS |
| Run self-test | Runs the gates in the background | `tests/testrunner.py` | `test_selftest.py`, `test_app.py::test_selftest_run_starts_the_suite` | Live run on 5.0.0: 8/8 gates, 9,339/9,339 lines, 418 s (it had never produced a result on Windows before 5.0.0) | PASS |
| AI self-evolution: run / apply / rollback | Proposes, deploys and reverts code patches | git branches, supervisor | `test_codevolve.py` (real sandbox), `test_supervisor.py` | Sandbox tests run with bubblewrap on Linux; no candidate has been applied on this PC | UNVERIFIED (no live apply) |
| Restart | Restarts under the supervisor | Supervisor relaunch | `test_supervisor.py`, `test_app_api.py::test_apply_rollback_restart_routes` | Supervisor log shows handled `restart_requested`/`started` pairs | PASS |
| Maintenance scan (5.3) | Protection, cleanup, registry, security, crashes, services, tasks, app updates | `maintenance.ps1` (read-only) | `test_maintenance.py`, `test_e2e_maintenance.py` | Ran on this PC 2026-10-30 in 30 s: Edge 677 MB, Chrome 1,540 MB, Firefox 118 MB of cache; 0 registry orphans; Defender and all firewall profiles on; 4 unexpected shutdowns in 30 days; 10+ winget updates listed. No restore point is recorded (no System Restore events) | PASS |
| Automatic restore points (5.4) | A restore point before the registry fix, app updates and component upgrades, and weekly | `restore_point.ps1` scheduled task (elevated, set up after one UAC prompt) | `test_restorepoint.py`, `test_e2e_maintenance.py` | Set up live 2026-10-31 after one UAC prompt: task registered, System Restore on for C:, newest restore point read back (under 24 h old, so later changes reported `recent`). An unanswered set-up prompt timed out after 10 min and the updates went ahead without one, as designed | PASS |
| Automatic app updates (5.4) | Listed apps update themselves after each scan; machine-wide ones after one prompt | winget (`maintenance.py`) | `test_maintenance.py`, `test_e2e_maintenance.py` | Live 2026-10-30/31: Zoom, Firefox, Notepad++, VLC, Outlook and Windows Terminal updated; HP Connection Optimizer and both VC++ runtimes updated together after one UAC prompt (all rc 0); ChatGPT held as waiting while open (0x80073D02), then updated to 26.1027.3384.0 once closed; MiKTeX refused by its manifest (0x8A150114), tagged own updater. Before the fix, unattended machine-wide installers hung 30 min each on an unseen UAC prompt | PASS |
| Fix: registry orphans (5.3) | Exports, then removes, the user's start-up and uninstall entries for missing programs | `reg export`, `Remove-ItemProperty` | `test_fixes.py`, `test_maintenance.py` | 2026-10-30: a test Run value pointing to a missing exe was removed, its `.reg` backup written (1,620 bytes), and `reg import` restored it exactly; the test value was then deleted | PASS |
| Fix: clear browser caches; app update (5.3) | Empties closed browsers' caches; updates one app with winget | `Remove-Item` on cache folders; `winget upgrade --id` | `test_maintenance.py`, `test_e2e_maintenance.py` | Not run on this PC: both change the owner's machine and need their go-ahead | UNVERIFIED |
| Every POST control | Refused without the CSRF token or from another origin | — | `test_security_doc.py`, `test_hostguard.py` | Live 5.0.0: foreign Host → 403, cross-origin POST → 403, no token → 403 | PASS |

UNVERIFIED rows are implemented and covered by automated tests, but their real
side effect was not triggered on this PC during the audit. Each needs either a
live trigger (Explorer at 25% CPU), elevation (standby broker), or a deliberate
change the owner has to approve (stopping OneDrive or an app, applying a patch).
