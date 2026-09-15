"""Tests for the Common-Fix engine and its endpoints."""
from __future__ import annotations

import copy
import subprocess

import pytest

from sysperf import fixes as F
from sysperf.app import create_app
from sysperf.collector import Collector
from sysperf.fixes import Fix, FixEngine, build_registry, default_runner


def _row(key, status="met", current=1, target=1):
    return {"key": key, "status": status, "current": current, "target": target}


def _snap(rows, latest=None):
    return {"latest": latest or {"wsl": {"cap_active": True}},
            "scorecard": {"rows": rows}}


DEGRADED = _snap([
    _row("net_remote", "regress", 90, 30), _row("c_free_gb", "partial", 150, 180),
    _row("handles", "regress", 200000, 95000), _row("startup_apps", "partial", 18, 10),
    _row("vmmem_mb", "regress", 9000, 10240), _row("available_mb", "partial", 6000, 12000),
    _row("compression_mb", "regress", 5000, 2750), _row("boot_time_s", "regress", 140, 40),
    _row("wsl_start_s", "regress", 30, 5), _row("explorer_launch_s", "partial", 6, 2),
    _row("outlook_launch_s", "regress", 25, 5), _row("word_launch_s", "partial", 12, 4),
], latest={"wsl": {"cap_active": False}})

HEALTHY = _snap([_row("net_remote"), _row("c_free_gb"), _row("handles"),
                 _row("vmmem_mb"), _row("boot_time_s"), _row("outlook_launch_s")])


# ------------------------------------------------------------- engine ---------
def test_executable_property():
    assert Fix("a", "A", "c", "safe", True, "pwsh", "x", lambda s: "y").executable
    assert not Fix("b", "B", "c", "manual", True, "pwsh", "", lambda s: "y").executable
    assert not Fix("c", "C", "c", "safe", True, "bash", "", lambda s: "y",
                   session_killing=True).executable


def test_all_detectors_fire_on_degraded():
    ids = {d["id"] for d in FixEngine().detect(DEGRADED)}
    assert ids == {f.id for f in build_registry()}


def test_only_dns_when_healthy():
    assert {d["id"] for d in FixEngine().detect(HEALTHY)} == {"flush_dns"}


def test_detector_branches():
    assert F._detect_dns(HEALTHY).startswith("Routine maintenance")
    assert "cap is not active" in F._detect_wsl_boot(
        {"latest": {"wsl": {"cap_active": False}}, "scorecard": {"rows": []}})
    assert F._detect_wsl_boot(
        {"latest": {"wsl": {"cap_active": True}}, "scorecard": {"rows": []}}) is None
    assert F._detect_office(_snap([_row("outlook_launch_s", "regress", 25, 5),
                                   _row("word_launch_s", "partial", 12, 4)]))
    assert F._needs(None) is False
    assert F._latest({"latest": None}) == {}


def test_preview_and_manual_hint():
    prev = {p["id"]: p for p in FixEngine().preview(None, DEGRADED)}
    assert prev["flush_dns"]["would_run"] == "ipconfig /flushdns"
    assert prev["restart_wsl"]["would_run"] is None and prev["restart_wsl"]["manual_hint"]
    assert FixEngine().preview(["flush_dns", "nope"], DEGRADED)[0]["id"] == "flush_dns"


def test_apply_runs_safe_skips_manual():
    calls = []
    eng = FixEngine(runner=lambda sh, cmd: (calls.append((sh, cmd)), (0, "ok"))[1])
    res = {r["id"]: r for r in eng.apply(["flush_dns", "restart_wsl", "trim_startup"], DEGRADED)}
    assert res["flush_dns"]["status"] == "ok"
    assert res["restart_wsl"]["status"] == "skipped"
    assert res["trim_startup"]["status"] == "skipped"
    assert calls == [("pwsh", "ipconfig /flushdns")]


def test_apply_error_and_default_all():
    assert FixEngine(runner=lambda s, c: (2, "boom")).apply(["flush_dns"], DEGRADED)[0]["status"] == "error"
    res = FixEngine(runner=lambda s, c: (0, "")).apply(None, DEGRADED)
    assert any(r["status"] == "ok" for r in res) and any(r["status"] == "skipped" for r in res)


# ------------------------------------------------------------- runner ---------
def test_default_runner_pwsh(monkeypatch):
    monkeypatch.setattr("sysperf.platform_win.powershell_exe", lambda: "pwsh")
    seen = {}

    def fake_run(argv, **kw):
        seen["argv"] = argv
        return type("R", (), {"returncode": 0, "stdout": "out", "stderr": ""})()
    monkeypatch.setattr(F.subprocess, "run", fake_run)
    rc, out = default_runner("pwsh", "Get-Date")
    assert rc == 0 and out == "out" and seen["argv"][-2] == "-Command"
    assert "& { Get-Date }" in seen["argv"][-1]
    assert "$ErrorActionPreference = 'Stop'" in seen["argv"][-1]
    assert "exit $LASTEXITCODE" in seen["argv"][-1]


def test_default_runner_bash(monkeypatch):
    monkeypatch.setattr(F.subprocess, "run",
                        lambda argv, **kw: type("R", (), {"returncode": 0, "stdout": "", "stderr": "w"})())
    assert default_runner("bash", "true") == (0, "w")


def test_default_runner_exception(monkeypatch):
    def boom(*a, **k):
        raise subprocess.TimeoutExpired("c", 1)
    monkeypatch.setattr(F.subprocess, "run", boom)
    rc, out = default_runner("pwsh", "x")
    monkeypatch.setattr("sysperf.platform_win.powershell_exe", lambda: "pwsh")
    rc, out = default_runner("pwsh", "x")
    assert rc == 1 and "TimeoutExpired" in out


# ------------------------------------------------------------- endpoints ------
class _Stub(Collector):
    def __init__(self, *a, sample_data, **k):
        self._s = sample_data
        super().__init__(*a, **k)

    def read_windows(self, full):
        return copy.deepcopy(self._s)


@pytest.fixture
def fix_client(cfg, sample, tmp_path):
    c = _Stub(cfg, None, history_file=tmp_path / "h.jsonl",
              baseline_file=tmp_path / "b.json", sample_data=sample)
    c.sample()
    c.sample()
    return cfg, create_app(cfg, c).test_client()


def test_api_fixes_and_preview(fix_client):
    _cfg, cl = fix_client
    d = cl.get("/api/fixes").get_json()
    assert d["enabled"] is False and d["token"] and isinstance(d["fixes"], list)
    p = cl.post("/api/fix", json={"confirm": False}).get_json()
    assert p["mode"] == "preview"


def test_api_fix_apply_disabled_is_blocked(fix_client):
    _cfg, cl = fix_client
    tok = cl.get("/api/fixes").get_json()["token"]
    r = cl.post("/api/fix", json={"confirm": True, "token": tok})
    assert r.status_code == 403 and "disabled" in r.get_json()["error"]


def test_api_fix_apply_enabled_manual_is_skipped(cfg, sample, tmp_path):
    cfg["fixes"] = {"enabled": True}
    c = _Stub(cfg, None, history_file=tmp_path / "h.jsonl",
              baseline_file=tmp_path / "b.json", sample_data=sample)
    c.sample()
    c.sample()
    cl = create_app(cfg, c).test_client()
    tok = cl.get("/api/fixes").get_json()["token"]
    # bad token first
    assert cl.post("/api/fix", json={"confirm": True, "token": "x",
                                     "ids": ["trim_startup"]}).status_code == 403
    # manual fix -> executed path but skipped (no real command runs)
    out = cl.post("/api/fix", json={"confirm": True, "token": tok,
                                    "ids": ["restart_wsl"]}).get_json()
    assert out["mode"] == "apply" and out["results"][0]["status"] == "skipped"
