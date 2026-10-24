# SysPerf 4.0 — AI self-evolution, autonomy levels and security architecture

This guide covers what is new in 4.0: the three autonomy levels, the runtime AI
integration, source-code self-evolution, and the security boundaries around
them. Everything else in the User Manual still applies.

## 1. Autonomy levels

| Level | What happens | Default |
|---|---|---|
| `off` | No autonomous behaviour. Monitoring and the dashboard still run. | |
| `observe` | Monitor, diagnose, recommend. Nothing on the machine or in the code changes. | **yes** (new installs) |
| `safe_auto` | The loop may run actions that are **low-risk, reversible and individually enabled by you**. | |
| `self_evolve` | `safe_auto`, plus source-code self-evolution (patches generated and validated in isolation, kept for your review). | |

Set it on the **AI Self-Evolution** tab, or `POST /api/autonomy {"level": …}`.

**Upgrading from 3.x.** If you had chosen Auto-Improve mode *auto*, 4.0 starts in
`safe_auto` with only the three actions it classifies as low-risk *and*
reversible enabled (`flush_dns`, `trim_working_sets`, `clear_standby`). Actions
3.x ran unattended that delete data (`clear_temp`) now need your confirmation.
The migration is written to the audit log.

### The action registry

Each remediation is an `ActionSpec` (`optactions.py`) with identifier,
description, preconditions, risk, reversibility, required permission, expected
benefit, execution method, validation method and rollback method. Execution
always goes through an existing, reviewed handler in `fixes.py`; no command
string is ever built from model output.

Only `risk = low` **and** `reversible = true` actions can be enabled for
unattended runs. Everything else is "confirmation only". Some things are refused
outright, whatever the level or setting: anything touching Defender/antivirus,
the firewall, Windows Update, event logging or auditing, backup/VSS, BitLocker,
UAC, recursive deletes, `diskpart`/`bcdedit`, `reg delete` under `HKLM\SYSTEM`,
or stopping `csrss`/`lsass`/`winlogon`/`svchost`/`smss`/`wininit`
(`optactions.FORBIDDEN`).

### Measured effectiveness

Every unattended run is recorded with its metric value *before* it ran. After a
10-minute settle time the current value is recorded and the run is judged
`improved`, `no_change` (under 3 % change — noise) or `worse`. The
effectiveness table only shows numbers that come from these recorded pairs. If
nothing has been measured it says so.

## 2. Runtime AI integration

Configure it on the tab, or in `<data dir>/evolution_settings.json`:

```json
{
  "llm": {
    "provider": "anthropic",
    "model": "<an id returned by 'List models'>",
    "timeout_s": 60,
    "daily_token_budget": 200000,
    "daily_cost_budget_usd": 2.0,
    "prices": { "<model id>": [<USD per 1M input tokens>, <USD per 1M output tokens>] }
  }
}
```

* **Providers.**
  * `anthropic`: the Messages API over HTTPS, stdlib only.
  * `kiro_cli`: a local `kiro-cli` in headless mode, with **no tools trusted**.
  * `none`: offline (the default).
* **Model id.** No model is built in. **List models** calls the provider's
  `GET /v1/models`, so you choose from what your account can actually use.
* **API key.** Taken from `SYSPERF_ANTHROPIC_API_KEY` or `ANTHROPIC_API_KEY`, or
  stored with **Store key**. That writes `<data dir>/secrets/anthropic.key` with
  mode 0600 inside a 0700 directory, and a key file that other users can read is
  refused. The key is never returned by any API or written to the database. A
  logging filter replaces anything shaped like a key with `sk-ant-***`.
* **Resilience.**
  * Timeouts are per request.
  * Up to 4 retries with exponential backoff and full jitter (capped at 30 s).
  * HTTP 429 honours `retry-after`; 5xx/529 and network errors are retried;
    401/403/400/404 are not.
  * There is a 2 s minimum gap between calls.
  * A daily token and cost budget acts as a hard stop.
* **Cost.** Estimated only from the `prices` you configure. Prices change, so
  none are built in; without one, cost shows as *unknown*.
* **Output validation.** Model replies are untrusted. A JSON object is
  extracted and validated against a strict schema (unknown keys and oversize
  fields are rejected) before anything else reads it.
* **Offline.** With no provider, a dead network or bad credentials, monitoring,
  scoring, RCA and rule-based recommendations run exactly as before. The tab
  shows why evolution is idle. AI errors are recorded and never propagate into
  the monitoring threads.

## 3. Source-code self-evolution

Self-evolution changes **code**, not thresholds or recipes. One cycle:

```
evidence (sandboxed baseline tests + benchmarks, AST findings)
  -> LLM plan + patch (JSON find/replace edits, schema-validated)
  -> pre-checks: paths, protection, eligibility, exact single match
  -> fresh git worktree of the approved repository (main checkout untouched)
  -> static analysis (parse) + security scan (no new subprocess/socket/eval/... ,
     no security-sensitive strings)
  -> sandboxed pytest: all pass, collected count >= baseline
  -> sandboxed benchmark: baseline vs candidate, N repetitions, median + MAD
  -> decision -> commit on sysperf-evolve/<id>  (never merged)
  -> human review: Approve records consent and shows the merge command;
                   Reject deletes the branch
```

**Configure it:**

1. Make your SysPerf source a git checkout:
   `git init && git add -A && git commit -m base`.
2. Add an `evo_bench.py` that defines `BENCHMARKS = {name: zero-arg callable}`
   for the code you want made faster. Without benchmarks a change cannot be
   measured, so it is never retained.
3. On the tab, set *Repository*, tick *Evolution enabled*, set the level to
   `self_evolve`, and configure an AI provider.

**Scheduling.** The cycle runs on its own thread, independent of the 5-minute
monitoring loop:

* The default interval is 6 h, with at most 4 cycles a day.
* An event trigger exists (`notify()`), subject to a 30-minute cooldown.
* *Run evolution cycle now* starts one manually.
* Cycles do not start while another is running, when the daily limit or budget
  is reached, or when there is no provider or repository. The reason is always
  shown.
* A cycle that finds too little evidence ends as *skipped* and says so; it does
  not invent work.

**Acceptance (enforced in code, not editable through the API):**

* Every test passes, and the collected test count does not drop.
* At least one benchmark is faster by **≥ 10 %** *and* by more than
  3 × (MAD_base + MAD_cand)/median. That is the noise bound.
* No benchmark regresses by more than 5 % beyond noise.
* **Engine modules** (`evolve.py`, `autoimprove.py`, `fixes.py`, `selftest.py`,
  `actions.py`) need a ≥ 20 % win and always need approval.
* **Protected — any edit rejects the candidate:**
  * tests, `conftest.py` and `evo_bench.py`;
  * the evolution, LLM, store and action-policy modules (`codevolve.py`,
    `llm.py`, `evodb.py`, `optactions.py`);
  * CI, packaging, `container/` and `scripts/`;
  * all `*.ps1`, `*.json`, `*.toml`, `*.sh`, `*.md` files, and `static/`.

## 4. Security architecture

| Threat | Control | Where |
|---|---|---|
| Model output used as commands | Output is data: schema-validated JSON edits; actions only via allowlisted handlers | `llm.validate`, `optactions` |
| Prompt injection via source files | Repository text is delimited and declared *data*; obeying an injected instruction still hits the protected-path and security gates | `codevolve.SYSTEM_PROMPT`, tests |
| Path traversal / writing outside repo | Relative paths only, no `..`, absolute paths, backslashes or `~`; file must be tracked and eligible | `codevolve._rel_ok`, `_check_edits` |
| Moving the goalposts | Tests, benchmarks, policy and evolution code are protected; thresholds not API-editable; test count may not drop | `PROTECTED`, `EDITABLE` |
| Executing untrusted code | **bubblewrap**: all namespaces unshared (no network), read-only root, `/home /root /mnt /media /run /var /srv /opt` hidden, env cleared, rlimits (2 GB AS, 64 MB files), timeout. Or **docker** `--network none --read-only --cap-drop ALL --security-opt no-new-privileges`, pids/memory/cpu limits | `codevolve.Sandbox` |
| No isolation available | Executable validation is **blocked**; the cycle stops before the LLM call and reports why | `Sandbox(backend="none")` |
| Self-granted permissions | Evolution cannot edit `optactions.py`, cannot enable non-low-risk actions, cannot approve its own candidates | `PROTECTED`, `ActionPolicy`, `review()` |
| Production deployment | Candidates live on branches; the service never loads them; merging is a human git step | `ReleaseManager` (commit), `review()` |
| Tampering with history | SHA-256 hash-chained audit log; `verify_audit()` pinpoints the first altered row | `evodb.audit` |
| Interrupted cycles | On start, cycles without an end are marked *interrupted*; stale worktrees removed | `evodb.recover`, `_cleanup_worktrees` |
| Corrupt database | Quarantined as `sysperf.corrupt-<ts>`, a fresh store created; monitoring unaffected | `EvoDB._open` |
| CSRF on controls | Every POST requires the per-process token | `app._csrf` |

**Known limits, stated plainly:**

* **The audit chain is tamper-*evident*, not tamper-proof.** Anyone who can
  write the database can rebuild the whole chain.
* **bubblewrap is not a VM.** It shares the host kernel, so a kernel exploit
  would escape it. Use the docker backend, or a dedicated VM, if that matters
  to you.
* **Windows has no built-in backend.** On native Windows, self-evolution runs
  in "static only" mode unless `evolution.docker_image` points at a working
  Docker install.
* **The container deployment (`container/`) has no bubblewrap.** Evolution
  there is static-only unless configured otherwise.
* **The `kiro_cli` provider cannot report token counts**, so its cost is
  unknown.

## 5. Install and start (Windows)

1. Unpack the release. Then run
   `powershell -ExecutionPolicy Bypass -File install.ps1`, or the graphical
   `install-wizard.ps1`.
2. Open `http://127.0.0.1:5056/`. The new tab is **AI Self-Evolution**.
3. Validate on your machine:
   `powershell -ExecutionPolicy Bypass -File tools\validate-windows.ps1`.
   It checks:
   * the OS build and privilege level, PowerShell and Python;
   * that the collector returns CPU, memory, disk and process keys;
   * platform resolution, background start, the first sample and API latency;
   * that the default autonomy level is `observe`, that it works offline, and
     that shutdown is clean;
   * that the Scheduled Tasks API is present, then runs the test suite.

From source: `python -m sysperf --port 5056 --open`.

## 6. Reproducing the evolution demonstration

```bash
python3 tools/evolution_demo.py --provider kiro_cli --out evidence/
python3 tools/evolution_demo.py --provider anthropic --model <id> --out evidence/
```

The demo copies `tests/fixtures/evo_demo` into a throwaway repository. That
fixture contains `unique_in_order`, which is O(n²) on purpose.

1. It runs one cycle with the real LLM.
2. It runs four adversarial cycles whose *model output* is fixed text: a syntax
   error, a behaviour change, an edit to a protected test, and an injected
   `subprocess` call. Everything after the model — sandbox, tests, benchmark,
   decision — runs for real.

Each cycle record, diff and audit-chain check is written to `--out`.
