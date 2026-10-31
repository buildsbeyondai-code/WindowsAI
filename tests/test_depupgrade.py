"""5.1 third-party upgrades: the pin rewrite and release checks (depupgrade), and
the self-evolution component lane that tests, retains and deploys them
(codevolve), over the real fixture repository with a scripted sandbox and a
fake PyPI."""
import hashlib
import io
import json
import shutil
import subprocess
import zipfile
from types import SimpleNamespace

import pytest
from tests.test_codevolve import FIXTURE
from tests.test_codevolve_branches import Box, eng

import sysperf.codevolve as cv
import sysperf.depupgrade as dep

H = {c: c * 64 for c in "abcdef"}
LOCK = ("# header\n#   regenerate with uv\n"
        "click==8.5.0 \\\n    --hash=sha256:" + H["a"] + " \\\n    --hash=sha256:" + H["b"]
        + "\n    # via flask\n"
        "flask==3.1.3 \\\n    --hash=sha256:" + H["c"] + " \\\n    --hash=sha256:" + H["d"]
        + "\n    # via -r requirements.txt\n")
REQS = "# pinned\nFlask==3.1.3\n"
DEV = ("-r requirements.txt\npytest==9.1.1\nmarkdown==3.10.3; python_version < '3.11'\n")
TOML = '[project]\ndependencies = [\n    "Flask==3.1.3",\n]\n'


def wheel(names=("fakeflask/__init__.py",)) -> bytes:
    buf = io.BytesIO()
    with zipfile.ZipFile(buf, "w") as z:
        for n in names:
            z.writestr(n, "VERSION = '3.2.0'\n")
    return buf.getvalue()


WHEEL = wheel()


def pypi(version="3.2.0", files=None, requires_python=">=3.9", requires_dist=None):
    files = files if files is not None else [
        {"filename": f"flask-{version}-py3-none-any.whl", "url": "https://files/whl",
         "digests": {"sha256": hashlib.sha256(WHEEL).hexdigest()}, "packagetype": "bdist_wheel"},
        {"filename": f"flask-{version}.tar.gz", "url": "https://files/sdist",
         "digests": {"sha256": H["e"]}, "packagetype": "sdist"},
        {"filename": "old.whl", "url": "x", "digests": {"sha256": H["f"]},
         "packagetype": "bdist_wheel", "yanked": True}]
    return json.dumps({"info": {"requires_python": requires_python,
                                "requires_dist": requires_dist or ["click>=8.1",
                                                                   "pytest; extra == 'dev'"]},
                       "urls": files})


def fake_get(meta=None, blob=WHEEL):
    def get(url):
        if url.startswith("https://pypi.org/"):
            return meta if meta is not None else pypi()
        return blob
    return get


@pytest.fixture
def repo(tmp_path):
    r = tmp_path / "repo"
    shutil.copytree(FIXTURE, r)
    (r / "requirements.lock").write_text(LOCK)
    (r / "requirements.txt").write_text(REQS)
    (r / "pyproject.toml").write_text(TOML)
    (r / "requirements-dev.txt").write_text(DEV)
    for a in (["init", "-q", "-b", "main"], ["add", "-A"],
              ["-c", "user.name=t", "-c", "user.email=t@x", "commit", "-q", "-m", "base"]):
        subprocess.run(["git", "-C", str(r), *a], check=True)
    return r


def head(repo):
    return subprocess.run(["git", "-C", str(repo), "rev-parse", "HEAD"],
                          capture_output=True, text=True).stdout.strip()


UP = {"name": "flask", "current": "3.1.3", "latest": "3.2.0"}


# ------------------------------------------------------------ depupgrade --
def test_locked_pinned_outdated_and_title(tmp_path, repo):
    assert dep.locked(tmp_path) == {} and dep.pinned(tmp_path) == {}
    assert dep.locked(repo) == {"click": "8.5.0", "flask": "3.1.3"}
    # dev pins join; a pin with an environment marker is not ours to move
    assert dep.pinned(repo) == {"click": "8.5.0", "flask": "3.1.3", "pytest": "9.1.1"}
    assert dep.outdated(repo, {"pytest": "9.2.0"}) == [
        {"name": "pytest", "current": "9.1.1", "latest": "9.2.0"}]
    assert dep.norm("Zope_Interface") == "zope-interface"
    assert dep.outdated(repo, {"flask": "3.2.0", "click": "8.5.0"}) == [UP]
    assert dep.outdated(repo, {}) == []
    assert dep.title(UP) == "3PP upgrade: flask 3.1.3 → 3.2.0"


def test_release_reads_hashes_and_skips_yanked_files():
    rel = dep.release("flask", "3.2.0", fake_get())
    assert [f["filename"] for f in rel["files"]] == ["flask-3.2.0-py3-none-any.whl",
                                                     "flask-3.2.0.tar.gz"]
    assert rel["requires_python"] == ">=3.9" and rel["requires_dist"][0] == "click>=8.1"
    bare = dep.release("x", "1", lambda url: json.dumps({}))
    assert bare["files"] == [] and bare["requires_python"] == "" and bare["requires_dist"] == []


@pytest.mark.parametrize("spec,ok", [("", True), (">=3.9", True), (">=3.10", True),
                                     (">=3.11", False), (">3.9", True), (">3.10", False),
                                     ("==3.10.*", True), ("==3.12", False), ("~=3.12", False),
                                     ("<4,>=3.8", True)])
def test_python_floor(spec, ok):
    assert dep._floor_ok(spec) is ok


def test_unsuitable_explains_every_refusal():
    lock = {"click": "8.5.0", "flask": "3.1.3"}
    get = fake_get()
    assert dep.unsuitable(dep.release("flask", "3.2.0", get), lock) is None
    none = dep.release("flask", "3.2.0", fake_get(pypi(files=[])))
    assert "no published files" in dep.unsuitable(none, lock)
    py = dep.release("flask", "3.2.0", fake_get(pypi(requires_python=">=3.11")))
    assert "still supports 3.10" in dep.unsuitable(py, lock)
    sdist = [{"filename": "flask-3.2.0.tar.gz", "url": "u", "digests": {"sha256": H["e"]},
              "packagetype": "sdist"}]
    assert "no wheel the sandbox's Python 3.12" in dep.unsuitable(
        dep.release("flask", "3.2.0", fake_get(pypi(files=sdist))), lock)
    linux_only = [whl("markupsafe-3.1-cp312-cp312-manylinux_2_17_x86_64.whl")]
    assert "no Windows wheel" in dep.unsuitable(
        dep.release("flask", "3.2.0", fake_get(pypi(files=linux_only))), lock)
    new_dep = dep.release("flask", "3.2.0", fake_get(pypi(requires_dist=["Blinker>=1.9"])))
    assert "needs blinker" in dep.unsuitable(new_dep, lock)
    # 5.4.3 live: a dev tool is not hash-locked, so pip resolves its dependencies
    # ("pytest 9.2.0 needs colorama" had refused every pytest upgrade)
    assert dep.unsuitable(dict(new_dep, name="pytest"), lock) is None


def whl(name, sha=None):
    return {"filename": name, "url": "https://files/" + name, "packagetype": "bdist_wheel",
            "digests": {"sha256": sha or hashlib.sha256(WHEEL).hexdigest()}}


def test_test_wheel_picks_one_the_sandbox_can_import():
    rel = {"files": [whl("m-1-cp311-cp311-manylinux_2_17_x86_64.whl"),
                     whl("m-1-cp312-cp312-manylinux_2_17_x86_64.whl"),
                     whl("m-1-cp312-cp312-win_amd64.whl")]}
    assert dep.test_wheel(rel, "312")["filename"].startswith("m-1-cp312-cp312-manylinux")
    abi3 = {"files": [whl("m-1-cp38-abi3-manylinux_2_17_x86_64.whl"),
                      whl("m-1-cp313-abi3-musllinux_1_1_aarch64.whl")]}
    assert dep.test_wheel(abi3, "312")["filename"].startswith("m-1-cp38-abi3")
    assert dep.test_wheel({"files": [whl("m-1-cp313-abi3-manylinux_2_17_x86_64.whl")]},
                          "312") is None
    assert dep.test_wheel({"files": [whl("m-1-cp311-cp311-manylinux_2_17_x86_64.whl")]},
                          "312") is None
    assert dep._windows_ok(rel) and not dep._windows_ok(abi3)


def test_fetch_wheel_verifies_the_hash_and_unpack_refuses_escapes(tmp_path):
    rel = dep.release("flask", "3.2.0", fake_get())
    assert dep.fetch_wheel(rel, fake_get()) == WHEEL
    with pytest.raises(ValueError, match="sha256"):
        dep.fetch_wheel(rel, fake_get(blob=b"tampered"))
    dep.unpack_wheel(WHEEL, tmp_path / "d")
    assert (tmp_path / "d" / "fakeflask" / "__init__.py").is_file()
    for bad in ("../evil.py", "/abs.py", "C:/x.py"):
        with pytest.raises(ValueError, match="unsafe"):
            dep.unpack_wheel(wheel((bad,)), tmp_path / "e")


def test_rewrite_moves_every_spelling_of_the_pin_and_swaps_lock_hashes():
    hashes = [H["f"], H["e"]]
    lock = dep.rewrite(LOCK.replace("\n", "\r\n"), UP, hashes)
    assert "\r\n" in lock and "\n" not in lock.replace("\r\n", "")
    lock = lock.replace("\r\n", "\n")
    assert ("flask==3.2.0 \\\n    --hash=sha256:" + H["e"] + " \\\n    --hash=sha256:"
            + H["f"] + "\n    # via -r requirements.txt") in lock
    assert H["c"] not in lock and H["a"] in lock and "click==8.5.0" in lock
    assert dep.rewrite(REQS, UP, hashes) == "# pinned\nFlask==3.2.0\n"
    assert '"Flask==3.2.0"' in dep.rewrite(TOML, UP, hashes)
    # a longer version that merely starts with the old one is left alone
    assert dep.rewrite("flask==3.1.30\n", UP, hashes) == "flask==3.1.30\n"


def test_apply_pin_reports_changed_files(tmp_path):
    (tmp_path / "requirements.txt").write_text(REQS)
    (tmp_path / "requirements.lock").write_text("click==8.5.0 \\\n    --hash=sha256:" + H["a"] + "\n")
    assert dep.apply_pin(tmp_path, UP, {"files": [{"sha256": H["e"]}]}) == ["requirements.txt"]
    (tmp_path / "requirements-dev.txt").write_text(DEV)
    up = {"name": "pytest", "current": "9.1.1", "latest": "9.2.0"}
    assert dep.apply_pin(tmp_path, up, {"files": []}) == ["requirements-dev.txt"]
    assert "pytest==9.2.0" in (tmp_path / "requirements-dev.txt").read_text()


def test_python_prefers_the_console_interpreter(tmp_path, monkeypatch):
    (tmp_path / "pythonw.exe").write_text("")
    monkeypatch.setattr(dep.sys, "executable", str(tmp_path / "pythonw.exe"))
    assert dep._python() == str(tmp_path / "pythonw.exe")       # no python.exe beside it
    (tmp_path / "python.exe").write_text("")
    assert dep._python() == str(tmp_path / "python.exe")
    monkeypatch.setattr(dep.sys, "executable", "/usr/bin/python3")
    assert dep._python() == "/usr/bin/python3"


def test_pip_sync_installs_the_pins_into_both_venvs(tmp_path, monkeypatch):
    seen = []

    def run(argv, **kw):
        seen.append(argv)
        return SimpleNamespace(returncode=0, stdout="", stderr="")
    assert dep.pip_sync(tmp_path, run=run, wsl=False) is None
    assert len(seen) == 1 and "--require-hashes" in seen[0]
    assert seen[0][-1] == str(tmp_path / "requirements.lock")
    seen.clear()
    assert dep.pip_sync("C:\\app", run=run, wsl=True, dev=True) is None
    assert len(seen) == 4
    assert seen[2][:3] == ["wsl.exe", "--", dep.SANDBOX_PY]
    assert seen[2][-1] == "/mnt/c/app/requirements.lock"
    assert seen[3][-1] == "/mnt/c/app/requirements-dev.txt"
    monkeypatch.setattr(dep.shutil, "which", lambda name: None)
    seen.clear()
    assert dep.pip_sync(tmp_path, run=run) is None and len(seen) == 1   # autodetect: no WSL
    fail = lambda argv, **kw: SimpleNamespace(returncode=1, stdout="", stderr="no match")  # noqa: E731
    assert dep.pip_sync(tmp_path, run=fail, wsl=False) == "no match"
    silent = lambda argv, **kw: SimpleNamespace(returncode=2, stdout=None, stderr=None)  # noqa: E731
    assert dep.pip_sync(tmp_path, run=silent, wsl=False) == "pip exited 2"

    def boom(argv, **kw):
        raise OSError("no pip")
    assert dep.pip_sync(tmp_path, run=boom, wsl=False) == "OSError: no pip"


# ---------------------------------------------------------- codevolve lane --
ROWS = [{"key": "flask", "name": "Flask", "current": "3.1.3", "latest": "3.2.0",
         "outdated": True, "upgrade": {"via": "pip-lock"}}]


def lane(tmp_path, repo, box=None, rows=None, get=None, **settings):
    e, db = eng(tmp_path, repo, box=box, **settings)
    e.components_fn = lambda: ROWS if rows is None else rows
    e.http_get = get or fake_get()
    e.uv_fn = lambda: None                    # the resolver tests below opt in
    return e, db


@pytest.fixture
def synced(monkeypatch):
    calls = []
    monkeypatch.setattr(dep, "pip_sync", lambda repo, dev=False: calls.append(str(repo)))
    return calls


def test_green_upgrade_is_retained_applied_and_restarted(tmp_path, repo, synced):
    box = Box()
    seen = []
    run = box.run
    box.run = lambda wt, cmd, timeout_s=None: (seen.append(cmd), run(wt, cmd, timeout_s))[1]
    e, db = lane(tmp_path, repo, box=box)
    restarts = []
    e.on_applied = lambda: restarts.append(1)
    before = head(repo)
    res = e.run_cycle("manual", "self_evolve")
    assert res["state"] == "awaiting_approval", res
    c = db.candidates()[0]
    assert c["title"] == dep.title(UP) and c["decision"] == "retained"
    assert json.loads(c["files"]) == ["requirements.txt", "requirements.lock", "pyproject.toml"]
    # the candidate ran with the unpacked wheel first on sys.path
    assert "sys.path.insert(0, '_deps')" in " ".join(seen[-1])
    # applied straight away: main moved, the lock was installed, restart requested
    assert head(repo) != before and "flask==3.2.0" in (repo / "requirements.lock").read_text()
    assert synced == [str(repo)] and restarts == [1]
    assert e.pending_apply()["id"] == c["id"]
    st = e.status("self_evolve")["components"]
    assert st["enabled"] and st["rows"][0]["review_state"] == "approved"   # verifying
    assert st["rows"][0]["label"].startswith("pinned in requirements.lock")
    # the same upgrade is never attempted twice; the code lane runs instead
    e.confirm_apply(True)
    assert e._component_lane("x", repo, head(repo), {}, repo) is None


def test_a_skipped_auto_apply_leaves_the_upgrade_for_review(tmp_path, repo, synced):
    e, db = lane(tmp_path, repo)

    def refuse(cid, actor="user"):
        raise ValueError("the repository has uncommitted changes")
    e.apply = refuse
    e.run_cycle("manual", "self_evolve")
    assert db.candidates()[0]["review_state"] == "pending"
    assert "candidate.auto_apply_skipped" in [a["event"] for a in db.audit_tail(limit=10)]


def test_lane_is_off_without_rows_setting_or_work(tmp_path, repo):
    e, db = lane(tmp_path, repo, auto_upgrade_deps=False)
    assert e._component_lane("x", repo, head(repo), {}, repo) is None
    (tmp_path / "b").mkdir()
    current = [dict(ROWS[0], latest="3.1.3", outdated=False),
               {"key": "docker", "missing": True, "upgrade": {"via": "apt"}},
               {"key": "mystery", "upgrade": {}},
               {"key": "kernel", "upgrade": {"via": "follows", "of": "wsl"}}]
    e, db = lane(tmp_path / "b", repo, rows=current)
    assert e._component_lane("x", repo, head(repo), {}, repo) is None
    rows = e.status("self_evolve")["components"]["rows"]
    assert [r["label"] for r in rows][2] == "no upgrader"
    e.components_fn = None
    assert e._component_lane("x", repo, head(repo), {}, repo) is None
    assert e.status("self_evolve")["components"] == {"enabled": False, "rows": []}


def test_unsuitable_release_is_rejected_once(tmp_path, repo):
    e, db = lane(tmp_path, repo, get=fake_get(pypi(requires_python=">=3.12")))
    res = e.run_cycle("manual", "self_evolve")
    c = db.candidates()[0]
    assert c["decision"] == "rejected" and "still supports 3.10" in c["reason"]
    assert "rejected" in res["explanation"]


def test_unreachable_pypi_fails_the_cycle_as_retryable(tmp_path, repo):
    def down(url):
        raise OSError("offline")
    e, db = lane(tmp_path, repo, get=down)
    res = e.run_cycle("manual", "self_evolve")
    assert res["state"] == "failed" and "offline" in res["explanation"]
    assert "retryable" in db.cycles()[0]["error"]


def test_red_tests_reject_the_upgrade(tmp_path, repo):
    box = Box(tests=["4 passed in 0.1s", "1 failed, 3 passed in 0.1s"], rc=[0, 1])
    e, db = lane(tmp_path, repo, box=box)
    e.run_cycle("manual", "self_evolve")
    c = db.candidates()[0]
    assert c["decision"] == "rejected" and c["reason"].startswith("tests: 1 failed")
    assert "flask==3.1.3" in (repo / "requirements.lock").read_text()


def test_apply_rolls_back_when_the_packages_do_not_install(tmp_path, repo, monkeypatch):
    calls = []
    monkeypatch.setattr(dep, "pip_sync", lambda repo, dev=False: calls.append(1) or (
        "no matching distribution" if len(calls) == 1 else "still offline"))
    e, db = lane(tmp_path, repo)
    e.apply = lambda cid, actor="user": None            # retain only
    e.run_cycle("manual", "self_evolve")
    del e.apply
    before = head(repo)
    cand = db.candidates()[0]
    with pytest.raises(RuntimeError, match="no matching distribution"):
        e.apply(cand["id"])
    # git is back where it was, and the old lock was reinstalled (that failure is logged)
    assert head(repo) == before and len(calls) == 2 and e.pending_apply() is None


def test_pins_changed(repo):
    h = head(repo)
    assert not cv.pins_changed(repo, h, h)
    (repo / "requirements.lock").write_text(LOCK + "\n")
    (repo / "requirements-dev.txt").write_text(DEV + "\n")
    subprocess.run(["git", "-C", str(repo), "-c", "user.name=t", "-c", "user.email=t@x",
                    "commit", "-qam", "lock"], check=True)
    assert cv.pins_changed(repo, h, head(repo)) == {"requirements.lock", "requirements-dev.txt"}


# ------------------------------------------------- dependencies come along --
def test_uv_cmd_only_when_uv_is_installed():
    assert dep.uv_cmd(find=lambda name: None) is None
    assert dep.uv_cmd(find=lambda name: object())[1:] == ["-m", "uv"]


def test_unsuitable_leaves_new_dependencies_to_the_resolver():
    lock = {"click": "8.5.0", "flask": "3.1.3"}
    new_dep = dep.release("flask", "3.2.0", fake_get(pypi(requires_dist=["Blinker>=1.9"])))
    assert "needs blinker" in dep.unsuitable(new_dep, lock)
    assert dep.unsuitable(new_dep, lock, resolver=True) is None


def test_relock_regenerates_the_lock_like_ci_and_keeps_its_header(tmp_path):
    (tmp_path / "requirements.lock").write_text(LOCK)
    seen = []

    def run(argv, cwd=None, **kw):
        seen.append(argv)
        (tmp_path / "requirements.lock.new").write_text("colorama==0.4.6 ; sys_platform == 'win32'\n")
        return SimpleNamespace(returncode=0, stdout="", stderr="")
    assert dep.relock(tmp_path, ["uv"], run=run) is None
    out = (tmp_path / "requirements.lock").read_text()
    assert out.startswith("# header\n#   regenerate with uv\n") and "colorama==0.4.6" in out
    assert seen[0][1:4] == ["pip", "compile", "--universal"] and "--generate-hashes" in seen[0]
    assert not (tmp_path / "requirements.lock.new").exists()

    def fail(argv, **kw):
        return SimpleNamespace(returncode=1, stdout="", stderr="No solution found")
    assert "No solution found" in dep.relock(tmp_path, ["uv"], run=fail)

    def boom(argv, **kw):
        raise OSError("no uv")
    assert dep.relock(tmp_path / "empty", ["uv"], run=boom) == "uv: OSError: no uv"


def test_closure_resolves_for_the_sandbox_platform(tmp_path):
    (tmp_path / "requirements.txt").write_text(REQS)
    seen = []

    def run(argv, cwd=None, **kw):
        seen.append((argv, (tmp_path / "requirements.sandbox.in").read_text()))
        return SimpleNamespace(returncode=0, stderr="",
                               stdout="flask==3.2.0\niniconfig==2.1.0\npytest==9.2.0\n")
    got = dep.closure(tmp_path, ["uv"], "pytest==9.2.0", "312", run=run)
    assert got == {"flask": "3.2.0", "iniconfig": "2.1.0", "pytest": "9.2.0"}
    argv, src = seen[0]
    assert "x86_64-unknown-linux-gnu" in argv and "3.12" in argv and "pytest==9.2.0" in src
    assert not (tmp_path / "requirements.sandbox.in").exists()
    assert "boom" in dep.closure(tmp_path, ["uv"], run=lambda a, **k: SimpleNamespace(
        returncode=2, stdout="", stderr="boom"))

    def boom(argv, **kw):
        raise OSError("no uv")
    assert dep.closure(tmp_path, ["uv"], run=boom) == "uv: OSError: no uv"


def test_upgrade_with_a_new_dependency_relocks_and_tests_the_whole_set(tmp_path, repo, synced,
                                                                        monkeypatch):
    # 5.4.3 live: "pytest 9.2.0 needs colorama" refused the upgrade; dependencies
    # are now resolved, hash-locked and unpacked for the sandbox
    calls = []

    def relock(wt, uv):
        calls.append(("relock", uv))
        (wt / "requirements.lock").write_text(LOCK.replace("3.1.3", "3.2.0")
                                              + "blinker==1.9.0 \\\n    --hash=sha256:" + H["e"] + "\n")
    monkeypatch.setattr(dep, "relock", relock)
    monkeypatch.setattr(dep, "closure", lambda wt, uv, extra, pyver: calls.append(
        ("closure", extra)) or {"flask": "3.2.0", "blinker": "1.9.0"})
    e, db = lane(tmp_path, repo, get=fake_get(pypi(requires_dist=["Blinker>=1.9"])))
    e.uv_fn = lambda: ["uv"]
    res = e.run_cycle("manual", "self_evolve")
    assert res["state"] == "awaiting_approval", res
    assert calls == [("relock", ["uv"]), ("closure", "")]
    c = db.candidates()[0]
    assert "requirements.lock" in json.loads(c["files"]) and c["decision"] == "retained"
    assert "blinker==1.9.0" in (repo / "requirements.lock").read_text()


def test_a_dev_tool_resolves_with_its_new_pin_and_an_unresolvable_set_is_rejected(
        tmp_path, repo, monkeypatch):
    extras = []
    monkeypatch.setattr(dep, "closure", lambda wt, uv, extra, pyver: extras.append(extra)
                        or "uv could not resolve the dependencies: conflict")
    rows = [{"key": "pytest", "name": "pytest", "current": "9.1.1", "latest": "9.2.0",
             "outdated": True, "upgrade": {"via": "pip-dev"}}]
    e, db = lane(tmp_path, repo, rows=rows)
    e.uv_fn = lambda: ["uv"]
    e.run_cycle("manual", "self_evolve")
    c = db.candidates()[0]
    assert extras == ["pytest==9.2.0"]
    assert c["decision"] == "rejected" and "could not resolve" in c["reason"]
