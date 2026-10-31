"""Full-coverage tests for fixes.py."""
import subprocess

import sysperf.fixes as fixes
from sysperf.fixes import (
    Fix,
    FixEngine,
    _detect_dns,
    _detect_wsl,
    _detect_wsl_boot,
    build_registry,
    default_runner,
)


# ------------------------------------------------------------- Fix dataclass --
def test_fix_executable_property():
    safe = Fix("a", "A", "c", "safe", True, "pwsh", "cmd", lambda s: "x")
    caution = Fix("b", "B", "c", "caution", True, "pwsh", "cmd", lambda s: "x")
    manual = Fix("c", "C", "c", "manual", True, "pwsh", "", lambda s: "x")
    killer = Fix("d", "D", "c", "safe", True, "bash", "", lambda s: "x",
                 session_killing=True)
    assert safe.executable and caution.executable
    assert not manual.executable and not killer.executable


# ---------------------------------------------------------------- detectors ---
def test_all_detectors_fire_on_degraded(snapshot_degraded):
    ids = {d["id"] for d in FixEngine().detect(snapshot_degraded)}
    # every registered fix should be applicable in the fully-degraded snapshot
    assert ids == {f.id for f in build_registry()}


def test_dns_maintenance_default(snapshot_healthy):
    # DNS flush is always offered (routine maintenance) even when healthy
    assert _detect_dns(snapshot_healthy).startswith("Routine maintenance")


def test_detectors_quiet_when_healthy(snapshot_healthy):
    detected = {d["id"] for d in FixEngine().detect(snapshot_healthy)}
    # only the always-on DNS maintenance fix should appear
    assert detected == {"flush_dns"}


def test_detect_wsl_prefers_cap_message():
    snap = {"latest": {"wsl": {"cap_active": False}}, "scorecard": {"rows": []}}
    assert "cap is written but NOT active" in _detect_wsl(snap)


def test_detect_wsl_regress_branch():
    snap = {"latest": {"wsl": {"cap_active": True}},
            "scorecard": {"rows": [{"key": "vmmem_mb", "status": "regress",
                                    "current": 9000, "target": 10240}]}}
    assert "not falling" in _detect_wsl(snap)


def test_detect_wsl_none():
    snap = {"latest": {"wsl": {"cap_active": True}}, "scorecard": {"rows": []}}
    assert _detect_wsl(snap) is None


def test_detect_wsl_boot_start_time_branch():
    snap = {"latest": {"wsl": {"cap_active": True}},
            "scorecard": {"rows": [{"key": "wsl_start_s", "status": "regress",
                                    "current": 30, "target": 5}]}}
    assert "cold-start" in _detect_wsl_boot(snap)


def test_detect_wsl_boot_none():
    snap = {"latest": {"wsl": {"cap_active": True}}, "scorecard": {"rows": []}}
    assert _detect_wsl_boot(snap) is None


def test_detect_wsl_boot_cap_not_active():
    # wsl_start_s not degraded, but the cap is inactive -> the cap-message branch
    snap = {"latest": {"wsl": {"cap_active": False}}, "scorecard": {"rows": []}}
    assert "cap is not active" in _detect_wsl_boot(snap)


def test_detect_office_names_and_worst():
    snap = {"latest": {}, "scorecard": {"rows": [
        {"key": "outlook_launch_s", "status": "regress", "current": 25, "target": 5},
        {"key": "word_launch_s", "status": "partial", "current": 12, "target": 4}]}}
    reason = fixes._detect_office(snap)
    assert "Outlook" in reason and "Word" in reason and "25" in reason


def test_detect_helpers_with_missing_rows():
    empty = {"latest": {}, "scorecard": {"rows": []}}
    assert fixes._detect_temp(empty) is None
    assert fixes._detect_recycle(empty) is None
    assert fixes._detect_explorer(empty) is None
    assert fixes._detect_startup(empty) is None
    assert fixes._detect_standby(empty) is None
    assert fixes._detect_sysmain(empty) is None
    assert fixes._detect_office(empty) is None


def test_needs_none_row():
    assert fixes._needs(None) is False


def test_latest_missing_returns_empty():
    assert fixes._latest({"latest": None}) == {}


# ------------------------------------------------------------------- preview --
def test_preview_default_selects_applicable(snapshot_degraded):
    eng = FixEngine()
    prev = eng.preview(None, snapshot_degraded)
    by_id = {p["id"]: p for p in prev}
    assert by_id["flush_dns"]["would_run"] == "ipconfig /flushdns"
    assert by_id["flush_dns"]["shell"] == "pwsh"
    # manual fix -> would_run is None, hint present
    assert by_id["restart_wsl"]["would_run"] is None
    assert by_id["restart_wsl"]["manual_hint"]


def test_preview_specific_ids(snapshot_degraded):
    prev = FixEngine().preview(["flush_dns", "bogus_id"], snapshot_degraded)
    assert [p["id"] for p in prev] == ["flush_dns"]        # unknown id dropped


# --------------------------------------------------------------------- apply --
def test_apply_runs_safe_and_skips_manual(snapshot_degraded):
    calls = []

    def fake_runner(shell, command):
        calls.append((shell, command))
        return 0, "done"

    eng = FixEngine(runner=fake_runner)
    results = eng.apply(["flush_dns", "restart_wsl", "trim_startup"], snapshot_degraded)
    by_id = {r["id"]: r for r in results}
    assert by_id["flush_dns"]["status"] == "ok"
    assert by_id["restart_wsl"]["status"] == "skipped"      # session-killing
    assert by_id["trim_startup"]["status"] == "skipped"     # manual
    assert calls == [("pwsh", "ipconfig /flushdns")]        # only the safe one ran


def test_apply_reports_error_returncode(snapshot_degraded):
    eng = FixEngine(runner=lambda s, c: (2, "boom"))
    r = eng.apply(["flush_dns"], snapshot_degraded)[0]
    assert r["status"] == "error" and r["returncode"] == 2


def test_apply_default_all_applicable(snapshot_degraded):
    eng = FixEngine(runner=lambda s, c: (0, ""))
    results = eng.apply(None, snapshot_degraded)
    # includes both executed and skipped entries
    assert any(r["status"] == "ok" for r in results)
    assert any(r["status"] == "skipped" for r in results)


# ---------------------------------------------------------------- runner ------
def test_default_runner_pwsh(monkeypatch):
    seen = {}

    def fake_run(argv, **kw):
        seen["argv"] = argv
        class R:
            returncode = 0
            stdout = "out"
            stderr = ""
        return R()

    monkeypatch.setattr(subprocess, "run", fake_run)
    rc, out = default_runner("pwsh", "Get-Date")
    assert rc == 0 and out == "out"
    assert seen["argv"][-2:] == ["-Command", "Get-Date"]


def test_default_runner_bash(monkeypatch):
    def fake_run(argv, **kw):
        assert argv[0] == "/bin/bash"
        class R:
            returncode = 0
            stdout = ""
            stderr = "warn"
        return R()

    monkeypatch.setattr(subprocess, "run", fake_run)
    rc, out = default_runner("bash", "true")
    assert rc == 0 and out == "warn"


def test_default_runner_exception(monkeypatch):
    def boom(*a, **k):
        raise subprocess.TimeoutExpired("cmd", 1)

    monkeypatch.setattr(subprocess, "run", boom)
    rc, out = default_runner("pwsh", "x")
    assert rc == 1 and "TimeoutExpired" in out


# ------------------------------------------------------ runtime registration ---
def test_register_adds_a_fix_at_runtime_and_is_idempotent():
    """Used by the evolution layer to surface a human-promoted remediation."""
    engine = FixEngine(runner=lambda shell, cmd: (0, "ok"))
    before = len(engine.registry)
    extra = fixes.Fix(id="evolved_x", title="Evolved action", category="cpu",
                      risk="manual", reversible=True, shell="pwsh", command="",
                      detect=lambda snap: "because",
                      manual_hint="Do the thing")
    assert engine.register(extra) is True
    assert len(engine.registry) == before + 1
    assert engine.get("evolved_x") is extra
    assert engine.register(extra) is False          # idempotent
    assert len(engine.registry) == before + 1
    found = {d["id"] for d in engine.detect({"scorecard": {"rows": []}})}
    assert "evolved_x" in found


# ------------------------------------ unprivileged CPU / memory remediations ---
# Added in 2.9.1 because cpu_pct, cpu_queue and disk_active_pct had no remediation
# at all: they drove ~150 of 500 cycles' breaches with nothing the loop could try.
def _snap(**rows):
    return {"scorecard": {"rows": [{"key": k, "label": k, "status": v}
                                   for k, v in rows.items()]},
            "latest": {}}


def test_cpu_hog_detector_fires_on_any_of_its_three_metrics():
    for key in ("cpu_pct", "cpu_queue", "disk_active_pct"):
        reason = fixes._detect_cpu_hog(_snap(**{key: "regress"}))
        assert reason and "background scheduling priority" in reason, key
        assert key in reason


def test_cpu_hog_detector_is_quiet_when_all_three_are_met():
    assert fixes._detect_cpu_hog(_snap(cpu_pct="met", cpu_queue="met",
                                       disk_active_pct="met")) is None
    assert fixes._detect_cpu_hog(_snap()) is None


def test_working_set_detector_fires_on_memory_pressure():
    for key in ("committed_app_mb", "available_mb", "compression_mb"):
        reason = fixes._detect_working_set(_snap(**{key: "partial"}))
        assert reason and "working" in reason, key


def test_working_set_detector_is_quiet_when_memory_is_fine():
    assert fixes._detect_working_set(_snap(committed_app_mb="met",
                                           available_mb="met",
                                           compression_mb="met")) is None


def test_both_remediations_are_registered_and_runnable_unattended():
    engine = FixEngine(runner=lambda shell, cmd: (0, "ok"))
    for fid in ("background_cpu_hog", "trim_working_sets"):
        fix = engine.get(fid)
        assert fix is not None, fid
        assert fix.executable, f"{fid} must be choosable by the loop"
        assert fix.risk == "caution", fid          # user-visible, so not 'safe'
        assert fix.reversible and not fix.session_killing, fid
        assert not fix.persistent, fid
        assert fix.manual_hint, f"{fid} must say how to undo it"


def test_the_denylist_protects_the_processes_that_matter():
    """A dry run of the selection logic picked `pwsh` — the process taking the
    measurement — and offered to trim `Memory Compression` and `vmmemWSL`, the VM
    this service runs inside. appctl.CRITICAL alone does not cover that, because it
    answers "may I stop this?", not "may I deprioritise or trim this?"."""
    import sysperf.appctl as appctl
    deny = fixes._DENY_PS.lower()
    for name in ("pwsh", "powershell", "memory compression", "vmmemwsl",
                 "conhost", "python3"):
        assert f"'{name}'" in deny, name
    # the shared list is included wholesale, so it cannot drift out of sync
    for name in sorted(appctl.CRITICAL):
        assert f"'{name}'" in deny, name
    assert appctl.CRITICAL <= (appctl.CRITICAL | fixes._EXTRA_DENY)


def test_the_cpu_remediation_excludes_its_own_process_tree():
    """A name list alone would be defeated by a renamed or repackaged shell."""
    command = FixEngine().get("background_cpu_hog").command
    assert "$self = @($PID)" in command
    assert "ParentProcessId" in command
    assert "$self -notcontains $_.Id" in command


def test_the_cpu_remediation_measures_over_an_interval():
    """Process.CPU is total seconds since start, so without differencing over an
    interval a long-lived idle process would always win."""
    command = FixEngine().get("background_cpu_hog").command
    assert "Start-Sleep" in command and "$_.CPU - $a[$_.Id]" in command
    assert "0x00100000" in command      # PROCESS_MODE_BACKGROUND_BEGIN
    assert "$delta -lt 0.5" in command  # a floor, so noise is not acted on


def test_the_trim_remediation_only_touches_large_working_sets():
    command = FixEngine().get("trim_working_sets").command
    assert "200MB" in command and "EmptyWorkingSet" in command
    assert "Select-Object -First 5" in command    # bounded


def test_a_restore_point_comes_first_for_fixes_that_ask():
    """5.4: the registry fix asks FixEngine.before_change for a restore point."""
    ran, seen = [], []
    eng = FixEngine(runner=lambda sh, cmd: (ran.append(cmd), (0, "done"))[1])
    eng.before_change = lambda why: seen.append((why, len(ran))) or {"state": "created"}
    res = eng.apply(["fix_registry_orphans", "flush_dns"], {})
    assert seen == [("back up and remove your start-up and uninstall entries for missing "
                     "programs", 0)]
    assert res[0]["restore_point"] == {"state": "created"} and "restore_point" not in res[1]
    eng.before_change = None
    assert "restore_point" not in eng.apply(["fix_registry_orphans"], {})[0]

