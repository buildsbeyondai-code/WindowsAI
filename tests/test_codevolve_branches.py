"""Every decision branch of codevolve, driven by a scripted sandbox and model over
the real fixture repository (no isolation backend needed)."""
import json
import subprocess
import sys
import threading
from pathlib import Path
from types import SimpleNamespace

import pytest
from tests.test_codevolve import FIXTURE, GOOD, Fixed

import sysperf.codevolve as cv
import sysperf.evodb as evodb
import sysperf.llm as llm


@pytest.fixture
def repo(tmp_path):
    r = tmp_path / "repo"
    import shutil
    shutil.copytree(FIXTURE, r)
    for a in (["init", "-q", "-b", "main"], ["add", "-A"],
              ["-c", "user.name=t", "-c", "user.email=t@x", "commit", "-q", "-m", "base"]):
        subprocess.run(["git", "-C", str(r), *a], check=True)
    return r


class Box:
    """Scripted sandbox: tests -> self.tests (or a list, one per call); bench -> self.bench."""
    backend, why_none = "fake", None

    def __init__(self, tests="4 passed in 0.1s", rc=0, bench=None, profile=None):
        self.tests, self.rc = tests, rc
        self.bench = bench if bench is not None else {"f": [0.01] * 5}
        self.profile = profile

    def run(self, wt, cmd, timeout_s=None):
        joined = " ".join(map(str, cmd))
        if "pytest" in joined:
            t = self.tests.pop(0) if isinstance(self.tests, list) else self.tests
            rc = self.rc.pop(0) if isinstance(self.rc, list) else self.rc
            return {"rc": rc, "stdout": t, "stderr": "err tail"}
        if "_sysperf_profile_runner" in joined:
            return {"stdout": self.profile or ""}
        b = self.bench.pop(0) if isinstance(self.bench, list) else self.bench
        if b is None:
            return {"stdout": "", "stderr": "bench crashed"}
        return {"stdout": "BENCH_JSON=" + json.dumps(b)}


def eng(tmp_path, repo, provider=None, box=None, **settings):
    db = evodb.EvoDB(tmp_path / "evo.db")
    sf = tmp_path / "settings.json"
    sf.write_text(json.dumps({"evolution": {"enabled": True, "repo": str(repo),
                                            "bench_reps": 5, "min_evidence": 1, **settings}}))
    client = llm.LLMClient(provider or Fixed(GOOD), min_interval_s=0) if provider is not False \
        else None
    return cv.CodeEvolution(db, client, sf, tmp_path / "wt", sandbox=box or Box()), db


# ------------------------------------------------------------- sandbox --
def _which(found):
    return lambda name: name if name in found else None


def test_detect_wsl_bwrap_on_windows(monkeypatch):
    calls = []

    def run(argv, **kw):
        calls.append(argv)
        if argv[-1] == "true":
            return SimpleNamespace(returncode=0, stdout="")
        return SimpleNamespace(returncode=0, stdout="noise\n/home/u/.venv/bin/python\n")
    monkeypatch.setattr(cv.os, "name", "nt")
    monkeypatch.setattr(cv.shutil, "which", _which({"wsl.exe"}))
    monkeypatch.setattr(cv, "run_hidden", run)
    s = cv.Sandbox()
    monkeypatch.setattr(cv.os, "name", "posix")         # pathlib needs the real OS below
    assert s.backend == "wsl-bwrap" and s.wsl_python == "/home/u/.venv/bin/python"
    argv = s.argv(Path("D:/work/wt"), [sys.executable, "-m", "pytest"])
    assert argv[:3] == ["wsl.exe", "-e", "bwrap"]
    assert cv.Sandbox._wsl_path("D:\\work\\wt") == "/mnt/d/work/wt"
    assert "/home/u/.venv/bin/python" in argv and "prlimit" in argv
    assert cv.Sandbox._wsl_path("relative/p") == "relative/p"


@pytest.mark.parametrize("probe_rc,stdout,first_rc", [(1, "", 0), (0, "", 0), (0, "x", 1)])
def test_detect_wsl_without_a_usable_venv_falls_through(monkeypatch, probe_rc, stdout,
                                                         first_rc):
    def run(argv, **kw):
        if argv[-1] == "true":
            return SimpleNamespace(returncode=first_rc, stdout="")
        return SimpleNamespace(returncode=probe_rc, stdout=stdout)
    monkeypatch.setattr(cv.os, "name", "nt")
    monkeypatch.setattr(cv.shutil, "which", _which({"wsl.exe"}))
    monkeypatch.setattr(cv, "run_hidden", run)
    backend = cv.Sandbox().backend
    monkeypatch.setattr(cv.os, "name", "posix")
    assert backend == "none"


def test_detect_docker_and_errors(monkeypatch, tmp_path):
    def boom(*a, **k):
        raise OSError("no exec")
    monkeypatch.setattr(cv, "run_hidden", boom)
    monkeypatch.setattr(cv.os, "name", "posix")
    monkeypatch.setattr(cv.shutil, "which", _which({"bwrap", "docker"}))
    s = cv.Sandbox(docker_image="img:1")
    assert s.backend == "docker"
    argv = s.argv(tmp_path, ["python", "x"])
    assert argv[:2] == ["docker", "run"] and "--network" in argv and argv[-3:] == [
        "img:1", "python", "x"]
    monkeypatch.setattr(cv.os, "name", "nt")
    monkeypatch.setattr(cv.shutil, "which", _which({"wsl.exe"}))
    backend = cv.Sandbox().backend
    monkeypatch.setattr(cv.os, "name", "posix")
    assert backend == "none"
    none = cv.Sandbox(backend="none")
    with pytest.raises(RuntimeError):
        none.argv(tmp_path, ["x"])


def test_run_times_out(monkeypatch, tmp_path):
    s = cv.Sandbox(backend="docker", docker_image="i")

    def slow(*a, **k):
        raise subprocess.TimeoutExpired("x", 1)
    monkeypatch.setattr(cv, "run_hidden", slow)
    r = s.run(tmp_path, ["x"], timeout_s=1)
    assert r["rc"] == 124 and r["timeout"]


def test_git_error_is_raised(tmp_path):
    with pytest.raises(RuntimeError, match="git rev-parse"):
        cv._git(tmp_path, "rev-parse", "HEAD")


# ---------------------------------------------------- analysis & settings --
def test_static_findings_cover_syntax_and_bare_except(tmp_path, repo):
    (repo / "broken.py").write_text("def x(:\n")
    (repo / "bare.py").write_text("try:\n    pass\nexcept:\n    pass\n")
    a = cv.SourceAnalyzer(repo, ["*.py"])
    kinds = {f["kind"] for f in a.static_findings(["broken.py", "bare.py"])}
    assert kinds == {"syntax", "bare_except"}


def test_settings_load_validate_and_persist(tmp_path, repo):
    e, db = eng(tmp_path, repo)
    (tmp_path / "settings.json").write_text("{broken")
    assert e._load_settings()["enabled"] is False            # defaults on corrupt file
    for bad in ({"interval_s": 10}, {"max_per_day": 99}, {"bench_reps": 1},
                {"eligible": ["../x"]}, {"repo": str(tmp_path)}, {"noise_k": 1},
                {"enabled": "yes"}, {"max_per_day": True}):
        with pytest.raises(ValueError):
            e.update_settings(bad)
    out = e.update_settings({"repo": str(repo), "eligible": ["*.py"], "interval_s": 600})
    assert out["interval_s"] == 600 and out["repo"] == str(repo.resolve())
    assert json.loads((tmp_path / "settings.json").read_text())["evolution"]["interval_s"] == 600


def test_scheduling_and_gates(tmp_path, repo):
    clock = [1000.0]
    e, db = eng(tmp_path, repo)
    e.clock = lambda: clock[0]
    assert e.next_due() == 1000.0                           # never ran: due now
    e.settings["enabled"] = False
    assert e.next_due() is None
    e.settings["enabled"] = True
    e.settings["repo"] = None
    assert "no approved repository" in e.gate("manual", "self_evolve")
    e.settings["repo"] = str(repo)
    e._state = "testing"
    assert "already testing" in e.gate("manual", "self_evolve")
    e._state = "idle"
    db.start_cycle("c1", "x")
    db.update_cycle("c1", state="failed", error="timeout (retryable): x")
    db.conn.execute("UPDATE cycles SET started=?", (clock[0],))
    db.conn.commit()
    assert e.next_due() == clock[0] + e.settings["cooldown_s"]
    assert e.gate("event", "self_evolve") == "event trigger inside cooldown"
    e.llm = None
    assert "no LLM provider" in e.gate("manual", "self_evolve")


def test_notify_and_loop(tmp_path, repo):
    e, db = eng(tmp_path, repo)
    runs, stop = [], threading.Event()
    e.gate = lambda trig, lvl: None
    e.run_cycle = lambda trig, lvl, detail=None: (runs.append((trig, detail)), stop.set())
    e.notify("selftest red")
    e.loop(lambda: "self_evolve", tick_s=0.01, stop=stop)
    assert runs == [("event", "selftest red")] and e._pending_event is None


def test_run_cycle_gate_lock_and_internal_error(tmp_path, repo):
    e, db = eng(tmp_path, repo)
    assert e.run_cycle("manual", "observe")["started"] is False
    e._lock.acquire()
    try:
        assert e.run_cycle("manual", "self_evolve")["reason"] == "a cycle is already running"
    finally:
        e._lock.release()

    def boom(cid):
        raise RuntimeError("disk gone")
    e._cycle = boom
    r = e.run_cycle("manual", "self_evolve")
    assert r["state"] == "failed" and db.cycles()[0]["explanation"].startswith("internal error")


# ------------------------------------------------------------ the cycle --
def test_no_eligible_files_is_skipped(tmp_path, repo):
    e, db = eng(tmp_path, repo, eligible=["*.nothing"])
    assert e.run_cycle("manual", "self_evolve")["state"] == "skipped"


def test_red_baseline_is_refused_and_names_the_failing_test(tmp_path, repo):
    out = "FAILED tests/test_x.py::test_slow - AssertionError: boom\n1 failed, 3 passed"
    e, db = eng(tmp_path, repo, box=Box(tests=out, rc=1))
    r = e.run_cycle("manual", "self_evolve")
    assert r["state"] == "skipped" and "refusing to evolve" in r["explanation"]
    assert "failing: tests/test_x.py::test_slow" in r["explanation"]


def test_crashing_test_run_counts_as_an_error(tmp_path, repo):
    e, db = eng(tmp_path, repo, box=Box(tests="", rc=2))
    r = e.run_cycle("manual", "self_evolve")
    assert "not green" in r["explanation"] and "err tail" in r["explanation"]


def test_insufficient_evidence_is_skipped(tmp_path, repo):
    e, db = eng(tmp_path, repo, box=Box(bench={}), min_evidence=50)
    assert "insufficient evidence" in e.run_cycle("manual", "self_evolve")["explanation"]


def test_model_declines_is_completed(tmp_path, repo):
    p = Fixed({"title": "none", "rationale": "already fast", "edits": [], "no_change": True})
    e, db = eng(tmp_path, repo, provider=p)
    r = e.run_cycle("manual", "self_evolve")
    assert r["state"] == "completed" and "already fast" in r["explanation"]


def test_noop_patch_and_foreign_diff_are_rejected(tmp_path, repo, monkeypatch):
    same = {"title": "x", "rationale": "x", "edits": [
        {"path": "dedupe.py", "find": "    seen = []\n", "replace": "    seen = []\n"}]}
    e, db = eng(tmp_path, repo, provider=Fixed(same))
    assert "no diff" in e.run_cycle("manual", "self_evolve")["explanation"]
    e2, db2 = eng(tmp_path / "b" if (tmp_path / "b").mkdir() is None else tmp_path, repo)
    real = cv._git

    def git(path, *args, **kw):
        out = real(path, *args, **kw)
        return out + "tests/test_dedupe.py\n" if args[:2] == ("diff", "--name-only") else out
    monkeypatch.setattr(cv, "_git", git)
    assert "protected/ineligible" in e2.run_cycle("manual", "self_evolve")["explanation"]


def test_edit_checks_and_security_counts():
    e = cv.CodeEvolution.__new__(cv.CodeEvolution)
    probs, engine = e._check_edits([{"path": "other.py", "find": "", "replace": "x"},
                                    {"path": "src/x/evolve.py", "find": "a", "replace": "b"}],
                                   ["src/x/evolve.py"])
    assert "other.py is not an eligible file" in probs and "empty 'find' for other.py" in probs
    assert engine is True
    assert cv.CodeEvolution._security_counts("def (:") == {}
    assert cv.CodeEvolution._security_counts("from subprocess import run\n") == {
        "import subprocess": 1}


def test_candidate_test_regression_and_bench_failures(tmp_path, repo):
    e, db = eng(tmp_path, repo, box=Box(tests=["4 passed", "3 passed, 1 failed"], rc=[0, 1]))
    assert "tests:" in e.run_cycle("manual", "self_evolve")["explanation"]
    # candidate benchmark crashes mid A/B
    good = {"f": [0.02] * 5}
    box = Box(bench=[good, good, None])
    e, db = eng(tmp_path / "c" if (tmp_path / "c").mkdir() is None else tmp_path, repo, box=box)
    assert "candidate benchmark failed" in e.run_cycle("manual", "self_evolve")["explanation"]


def test_bench_ab_baseline_failure_and_missing_bench(tmp_path):
    e = cv.CodeEvolution.__new__(cv.CodeEvolution)
    e.settings = dict(cv.CodeEvolution.DEFAULTS)
    e.sandbox = Box(bench=[None])
    wt = tmp_path / "wt"
    wt.mkdir()
    (wt / "evo_bench.py").write_text("BENCHMARKS = {}")
    base, cand = e._bench_ab(wt, wt)
    assert "_error" in base and base is cand
    assert e._bench(tmp_path) == {}                          # no evo_bench.py


def test_compare_error_paths():
    e = cv.CodeEvolution.__new__(cv.CodeEvolution)
    e.settings = dict(cv.CodeEvolution.DEFAULTS)
    assert "candidate benchmark failed" in e.compare({"f": {}}, {"_error": "boom"}, False)["reason"]
    b = {"median": 1.0, "mad": 0.0}
    assert "missing from candidate" in e.compare({"f": b}, {"g": b}, False)["reason"]


def test_existing_worktree_is_replaced(tmp_path, repo):
    e, db = eng(tmp_path, repo)
    sha = cv._git(repo, "rev-parse", "HEAD").strip()
    wt = e._worktree(repo, sha, "dup")
    (wt / "marker").write_text("x")
    wt2 = e._worktree(repo, sha, "dup")
    assert not (wt2 / "marker").exists()
    e._drop_worktree(repo, wt2)


def test_review_errors(tmp_path, repo):
    e, db = eng(tmp_path, repo)
    with pytest.raises(ValueError, match="only retained"):
        e.review("missing", "approve")
    db.start_cycle("c1", "x")
    db.add_candidate({"id": "k", "cycle_id": "c1", "created": 1, "decision": "retained",
                      "review_state": "pending", "branch": "b", "commit_sha": "s"})
    with pytest.raises(ValueError, match="approve or reject"):
        e.review("k", "maybe")


def test_prompt_skips_files_over_the_budget(tmp_path):
    (tmp_path / "small.py").write_text("x = 1\n")
    (tmp_path / "huge.py").write_text("y = 2\n" * 20000)            # > 60 kB
    e = cv.CodeEvolution.__new__(cv.CodeEvolution)
    p = e._prompt(tmp_path, ["huge.py", "small.py"], [{"file": "huge.py", "kind": "k"}])
    assert '<file path="small.py">' in p and '<file path="huge.py">' not in p


def _crlf_repo(repo):
    """Turn the fixture checkout into what Git for Windows produces (CRLF)."""
    src = (repo / "dedupe.py").read_bytes().replace(b"\r\n", b"\n").replace(b"\n", b"\r\n")
    (repo / "dedupe.py").write_bytes(src)
    subprocess.run(["git", "-C", str(repo), "-c", "user.name=t", "-c", "user.email=t@x",
                    "-c", "core.autocrlf=false", "commit", "-qam", "crlf"], check=True)


def test_noop_edit_on_a_crlf_checkout_leaves_no_diff(tmp_path, repo):
    _crlf_repo(repo)
    same = {"title": "x", "rationale": "x", "edits": [
        {"path": "dedupe.py", "find": "    seen = []\n", "replace": "    seen = []\n"}]}
    e, db = eng(tmp_path, repo, provider=Fixed(same))
    assert "no diff" in e.run_cycle("manual", "self_evolve")["explanation"]


def test_real_edit_on_a_crlf_checkout_changes_only_its_lines(tmp_path, repo):
    _crlf_repo(repo)
    e, db = eng(tmp_path, repo, box=Box(bench={"f": [0.02] * 5}))   # no gain: rejected, diff kept
    e.run_cycle("manual", "self_evolve")
    diff = db.candidate(db.candidates()[0]["id"])["diff"]
    changed = [ln for ln in diff.splitlines() if ln[:1] in "+-" and ln[:3] not in ("+++", "---")]
    assert len(changed) == 4 and any("seen = set()" in ln for ln in changed), changed


def test_match_eol():
    assert cv._match_eol("a\nb\n", True) == "a\r\nb\r\n"
    assert cv._match_eol("a\r\nb\n", False) == "a\nb\n"
