# SysPerf requirements

What SysPerf must do, how well it must do it, and which tests show that it
does. `tests/test_requirements_doc.py` fails if a test named below stops
existing, so this file cannot silently drift from the suite.

## Users and platforms

| | |
|---|---|
| **Primary user** | One person on their own Windows 11 PC who wants it to stay fast and wants to know why when it is not. |
| **Secondary user** | A contributor who runs the suite, reviews AI-proposed patches and ships releases. |
| **Platforms** | Windows 11 x64 (target). WSL 2 and Linux run the core and the suite, but cannot collect Windows metrics. |
| **Delivery** | Scripted install (`install.ps1`), Microsoft Store MSIX (`packaging/msix`), wheel/sdist. |
| **Integrations** | PowerShell 5.1/7, Win32 APIs (ctypes), WSL, OneDrive, Windows toasts. AI self-evolution uses an Anthropic API key or Claude Code, and is opt-in. |

## Features by priority

**Must** have: the product is broken without it. **Should** have: expected,
but it can ship disabled. **Could** have: optional, off by default or opt-in.

| Priority | Feature | Notes |
|---|---|---|
| Must | Measure the PC (memory, CPU, disk, boot/app launch, network, WSL) from real OS sources | No synthetic values, ever |
| Must | Score against a per-machine baseline and show progress | |
| Must | Show freshness, errors and missing permissions instead of stale or invented numbers | |
| Must | Refuse every state change without the CSRF token, and every request not addressed to SysPerf | |
| Must | Survive crashes, hangs and sleep/resume (supervisor) | |
| Should | Reversible fixes with preview before apply | Disabled with `PERFDASH_FIX_ENABLED=0` |
| Should | Autonomous loop: scan, RCA, fix, verify, learn | Defaults to `observe` |
| Should | History (14 days raw, 400 days hourly), trends, notifications | |
| Should | Store build with every system-changing feature opt-in | Store policy 10.2/10.8 |
| Could | Explorer watchdog, tray auto-quit, OneDrive lock policy, App Control | |
| Could | AI self-evolution with review branches and explicit Apply | Needs a key and a source checkout |

Anything not in this table is out of scope until it is added here with
acceptance criteria.

## Acceptance criteria (Given / When / Then)

| ID | Given | When | Then | Verified by |
|---|---|---|---|---|
| R1 | the collector is running on Windows | a sample completes | every headline value comes from an OS counter or API and matches it | `tests/test_collector.py::test_sample_success_full`, `tests/test_livestats.py::test_reads_memory_and_cpu_delta`, `tests/test_livestats_win.py::test_live_values_match_an_independent_os_reading`, the CI collector smoke test |
| R2 | collection fails or stops | the page or `/api/health` is read | it reports `ok: false` or a stale age, never the last good value as if it were current | `tests/test_app.py::test_health`, `tests/test_health.py::test_monitor_verdicts` |
| R3 | a baseline exists | the scorecard is built | each row shows baseline, current, target, status and its unit | `tests/test_scoring.py::test_build_scorecard_full` |
| R4 | a fix is detected | the user previews it | nothing changes; with `confirm` and the token it is applied and reported | `tests/test_app.py::test_fix_preview`, `tests/test_app.py::test_fix_apply_ok`, `tests/test_app.py::test_fix_apply_bad_token` |
| R5 | any POST route | it is called without the token | it answers 403 and changes nothing | `tests/test_security_doc.py::test_every_post_route_requires_the_csrf_token` |
| R6 | a web page rebinds its hostname to 127.0.0.1 | it requests the token or POSTs | the request is refused (403) | `tests/test_hostguard.py::test_rebound_host_cannot_read_the_csrf_token`, `tests/test_hostguard.py::test_rebound_host_cannot_post` |
| R7 | the service crashes, hangs or stops sampling | the supervisor notices | it restarts the service with back-off | `tests/test_supervisor.py::test_failed_health_checks_restart_after_threshold`, `tests/test_supervisor.py::test_stalled_sampling_is_treated_as_unhealthy` |
| R8 | an applied AI patch never becomes healthy | the supervisor gives up waiting | it rolls back to the tagged previous commit | `tests/test_supervisor.py::test_repeated_crashes_after_a_patch_roll_it_back` |
| R9 | self-evolution produces a passing candidate | the cycle ends | the candidate waits on a review branch; main is unchanged until Apply | `tests/test_codevolve.py::test_good_patch_is_retained_on_a_review_branch_never_main`, `tests/test_codevolve.py::test_apply_refuses_dirty_tree_moved_main_and_wrong_states` |
| R10 | no sandbox (bubblewrap/container) is available | a candidate is ready to test | it is never executed | `tests/test_codevolve.py::test_no_isolation_blocks_execution` |
| R11 | the Store build starts for the first time | Home is shown | every system-changing feature is off and listed with how to undo it | `tests/test_store_mode.py::test_describe_lists_every_feature_with_its_state` |
| R12 | the autonomy level is `observe` (the default) | the loop runs | no action runs unattended | `tests/test_optactions_evodb.py::test_default_is_observe_and_nothing_runs_unattended` |
| R13 | a tray app the user has not decided on | its window has been closed past the grace period | SysPerf asks once and quits it only after consent | `tests/test_trayquit.py::test_undecided_app_waits_for_consent_and_asks_once` |
| R14 | the API key is saved on Windows | the key file is read by another user or on another OS | it cannot be decrypted, so it is ignored | `tests/test_llm.py::test_unreadable_dpapi_key_file_is_ignored`, `tests/test_llm_win.py::test_real_dpapi_round_trip_and_file_holds_no_key` |
| R15 | the dashboard is opened | each tab is clicked in a real browser | every view renders, with loading, empty and error states | `tests/test_e2e_ui.py::test_every_tab_opens_its_view`, `tests/test_e2e_v41.py::test_home_with_nothing_to_do` |
| R17 | a package in requirements.lock has a newer pure-wheel release | a self-evolution cycle runs | the pin moves in all three pin files, the suite runs against it in the sandbox and a green upgrade is applied and verified; a red one is rejected and main is unchanged | `tests/test_depupgrade.py::test_green_upgrade_is_retained_applied_and_restarted`, `tests/test_depupgrade.py::test_red_tests_reject_the_upgrade` |
| R18 | an applied upgrade's packages fail to install | Apply runs pip | main returns to the previous commit and the previous packages are reinstalled | `tests/test_depupgrade.py::test_apply_rolls_back_when_the_packages_do_not_install` |
| R19 | a component is added to components.json | the suite runs | it fails unless the row names a known upgrader | `tests/test_upgrader.py::test_every_component_row_names_a_known_upgrader` |
| R20 | a host component's isolated test fails, or its post-upgrade check fails | the upgrade runs | the real install is left untouched, or rolled back from the backup | `tests/test_upgrader.py::test_pipeline_outcomes`, `tests/test_upgrader.py::test_apt_failures_and_rollback` |
| R21 | a PC has no AI provider, no WSL sandbox and no source checkout | a Windows-side component is outdated | it is upgraded on its own from Safe-auto, or with Upgrade now at any level but Off, and rows that need the sandbox say so | `tests/test_upgrader.py::test_upgrades_run_without_a_model_sandbox_or_checkout`, `tests/test_e2e_v41.py::test_upgrades_work_without_ai_and_upgrade_now_starts_one` |
| R16 | a worker fails repeatedly | it logs the same error | the log keeps the first three per 10 minutes and reports the rest | `tests/test_health.py::test_repeat_filter_lets_a_burst_through_then_reports_the_rest` |

## Non-functional targets

Measured values are from the live service on the reference laptop (8 logical
CPUs, 7.8 GB RAM) on 2026-10-25, unless stated otherwise.

| Area | Target | Measured | How it is measured |
|---|---|---|---|
| Freshness | Last good sample under 10 min old, 99% of the time | 13 s | `/api/health` `last_sample_age_s`; health probe `stale_after_s` 600 |
| API latency | Read endpoints under 250 ms at p95 | 4–94 ms | `curl -w %{time_total}` over all GET routes |
| Own CPU | Under 1% average | 0.06% | `/api/diagnostics` `footprint.avg_cpu_pct` |
| Own memory | Under 150 MB RSS | 27.9 MB after 22 min; see the soak test for growth | `/api/diagnostics` `footprint.rss_mb`; `tools/soak.py` |
| Sample cost | Fast sample under 5 s | 3.9 s | `/api/diagnostics` `collector.last_duration_s` |
| Recovery | Restart within 60 s of a crash | covered by tests | supervisor tests |
| Storage | History DB under 100 MB | 20 MB (14 d raw + rollups) | `/api/diagnostics` `history.bytes` |
| Accessibility | WCAG 2.1 AA contrast; every control reachable by keyboard and labelled | contrast 6.4:1 or better | contrast check in `tests/test_accessibility.py` |
| Security | No known vulnerable runtime dependency at release | 0 (pip-audit) | CI `supply-chain` job |
