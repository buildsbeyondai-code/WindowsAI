"""5.1.1 component upgrades: every Components row has an upgrader, each upgrader's
test / apply / verify / roll back path, and the self-evolution host lane.

The machine is never touched: ``Host.run`` is a script of (substring -> result)
rules, downloads come from a dict, and the suite is a stub.
"""
import io
import json
import zipfile
from pathlib import Path

import pytest
from tests.test_codevolve_branches import Box, eng
from tests.test_depupgrade import repo  # noqa: F401 - fixture

import sysperf.codevolve as cv
import sysperf.upgrader as ug

CATALOG = Path(__file__).resolve().parents[1] / "src" / "sysperf" / "components.json"


# ------------------------------------------------------------ the catalog --
def test_every_component_row_names_a_known_upgrader():
    """Today's 29 rows and any added later: no component is left out."""
    rows = json.loads(CATALOG.read_text(encoding="utf-8"))["catalog"]
    assert len(rows) >= 29
    for r in rows:
        spec = r.get("upgrade") or {}
        assert spec.get("via") in ug.REGISTRY, f"{r['key']} has no upgrader"
        if spec["via"] == "apt":
            assert spec.get("package"), r["key"]
        if spec["via"] == "winget":
            assert spec.get("id"), r["key"]
        if spec["via"] == "follows":
            assert spec.get("of") in {x["key"] for x in rows}, r["key"]


def test_every_upgrader_is_labelled():
    for via, u in ug.REGISTRY.items():
        assert u.via == via and u.label


# ----------------------------------------------------------------- harness --
class Run:
    """Scripted host commands: the first rule whose text is in the argv wins."""

    def __init__(self, rules=None, default=(0, "")):
        self.rules = list(rules or [])
        self.default = default
        self.calls = []

    def __call__(self, argv, timeout=600, cwd=None, env=None):
        joined = " ".join(map(str, argv))
        self.calls.append(joined)
        for needle, res in self.rules:
            if needle in joined:
                if isinstance(res, list):          # a sequence of answers
                    out = res.pop(0) if len(res) > 1 else res[0]
                    return out
                return res
        return self.default

    def ran(self, needle):
        return any(needle in c for c in self.calls)


def zbytes(names):
    buf = io.BytesIO()
    with zipfile.ZipFile(buf, "w") as z:
        for n in names:
            z.writestr(n, "x")
    return buf.getvalue()


def host(tmp_path, run=None, files=None, suite=None, **kw):
    files = files or {}

    def get(url):
        for k, v in files.items():
            if k in url:
                return v if isinstance(v, bytes) else json.dumps(v).encode()
        raise OSError(f"no fake for {url}")
    kw.setdefault("sandbox_python", "/home/u/.sysperf-sandbox-venv/bin/python")
    h = ug.Host(run=run or Run(), get=get, staging=tmp_path / "stg", repo=tmp_path / "repo",
                data_dir=tmp_path / "data", win_python="py.exe", venv_dir=tmp_path / "venv",
                distro="Ubuntu", wsl_user="u", suite=suite,
                baseline={"passed": 10, "summary": "10 passed"}, free_gb=lambda p: 100.0, **kw)
    (tmp_path / "data").mkdir(exist_ok=True)
    return h


GREEN = lambda distro=None: {"rc": 0, "passed": 10, "failed": 0, "errors": 0,  # noqa: E731
                             "summary": "10 passed"}
RED = lambda distro=None: {"rc": 1, "passed": 9, "failed": 1, "errors": 0,  # noqa: E731
                           "summary": "1 failed, 9 passed"}


def row(key, cur, new, via, outdated=True, **spec):
    return {"key": key, "name": key.title(), "current": cur, "latest": new,
            "outdated": outdated, "upgrade": {"via": via, **spec}}


# ----------------------------------------------------------------- helpers --
def test_small_helpers(monkeypatch):
    assert ug._key("3.15.2rc1") == (3, 15, 2, 1)
    assert ug._is_version("3.15.2") and ug._is_version("v2.1") and not ug._is_version("apt (x)")
    assert ug._q("it's") == "'it''s'" and ug._sh("it's") == "'it'\\''s'"
    assert ug.wsl_path("C:\\a\\b") == "/mnt/c/a/b" and ug.wsl_path("/x") == "/x"
    monkeypatch.setattr(ug.platform, "machine", lambda: "ARM64")
    assert ug.arch() == "arm64"
    monkeypatch.setattr(ug.platform, "machine", lambda: "AMD64")
    assert ug.arch() == "x64"
    assert ug.title({"key": "git", "from": "1", "to": "2"}) == "3PP upgrade: git 1 → 2"


def test_host_commands_downloads_and_releases(tmp_path):
    run = Run([("Start-Process", (0, ""))])
    h = host(tmp_path, run, files={
        "f.bin": b"data",
        "releases/latest": {"tag_name": "v2", "assets": [{"name": "a.zip",
                                                          "browser_download_url": "u/a"}]},
        "releases/tags/v1": {"tag_name": "v1", "assets": []}})
    h.ps("x")
    h.wsl("echo", root=True)
    h.elevated("msiexec.exe", "/i x")
    assert "'-NoProfile'" not in run.calls[0] and "-Command x" in run.calls[0]
    assert run.calls[1] == "wsl.exe -d Ubuntu -u root -- sh -c echo"
    assert "-Verb RunAs" in run.calls[2]
    import hashlib
    p = h.download("https://x/f.bin", "f.bin", hashlib.sha256(b"data").hexdigest())
    assert p.read_bytes() == b"data"
    with pytest.raises(ValueError, match="sha256"):
        h.download("https://x/f.bin", "f.bin", "00")
    assert h.github_asset("o/r", r"a\.zip") == ("u/a", "v2")
    with pytest.raises(ValueError, match="no release asset"):
        h.github_asset("o/r", "b", tag="v1")


def test_suite_ok_compares_with_the_baseline(tmp_path):
    h = host(tmp_path)
    assert h.suite_ok() is None                                  # no suite wired
    h.suite = GREEN
    assert h.suite_ok() is None
    h.suite = RED
    assert "1 failed" in h.suite_ok("copy")
    h.suite = lambda distro=None: {"blocked": True, "summary": "no bwrap"}
    assert "could not run" in h.suite_ok()


def test_distro_copy_export_import_restore(tmp_path):
    run = Run([("--export", [(1, "disk"), (0, "")]), ("--import", [(1, "bad"), (0, "")])])
    h = host(tmp_path, run)
    with pytest.raises(RuntimeError, match="export failed"):
        h.export_distro()
    tar = h.export_distro()
    assert tar.name == "Ubuntu-backup.tar"
    with pytest.raises(RuntimeError, match="import failed"):
        h.import_copy(tar)
    assert h.import_copy(tar) == ug.TEST_DISTRO
    assert run.ran("--unregister sysperf-upgrade-test")
    h.free_gb = lambda p: 1.0
    with pytest.raises(RuntimeError, match="GB free"):
        h.export_distro()
    run = Run([("--import", [(1, "x"), (0, "")])])
    h = host(tmp_path, run)
    assert "failed" in h.restore_distro(tar)
    assert h.restore_distro(tar) is None
    assert run.ran("default=u") and run.ran("--set-default Ubuntu")


# ---------------------------------------------------------------- pipeline --
class Scripted(ug.Upgrader):
    via, label = "scripted", "scripted"

    def __init__(self, **res):
        self.res = res
        self.calls = []

    def _r(self, name, default=None):
        self.calls.append(name)
        v = self.res.get(name, default)
        if isinstance(v, Exception):
            raise v
        return v

    def test(self, up, host):
        return self._r("test")

    def backup(self, up, host):
        return self._r("backup", {"b": 1})

    def apply(self, up, host):
        return self._r("apply")

    def verify(self, up, host):
        return self._r("verify")

    def rollback(self, up, bk, host):
        return self._r("rollback")

    def cleanup(self, up, bk, host):
        self._r("cleanup")


UP = {"key": "x", "from": "1", "to": "2"}


def test_pipeline_outcomes(tmp_path):
    h = host(tmp_path)
    assert ug.run_upgrade(Scripted(), dict(UP), h)["state"] == "applied"
    r = ug.run_upgrade(Scripted(test="broken"), dict(UP), h)
    assert (r["state"], r["stage"], r["reason"]) == ("rejected", "test", "broken")
    r = ug.run_upgrade(Scripted(apply="no"), dict(UP), h)
    assert r["state"] == "rolled_back" and r["stage"] == "apply"
    r = ug.run_upgrade(Scripted(apply="no", rollback="stuck"), dict(UP), h)
    assert r["state"] == "failed" and r["rollback_error"] == "stuck"
    r = ug.run_upgrade(Scripted(verify="bad"), dict(UP), h)
    assert r["state"] == "rolled_back" and r["stage"] == "verify"
    r = ug.run_upgrade(Scripted(verify="bad", rollback="stuck"), dict(UP), h)
    assert r["state"] == "failed"
    s = Scripted(test=OSError("offline"))
    r = ug.run_upgrade(s, dict(UP), h)
    assert r["state"] == "failed" and r["retryable"] and "rollback" not in s.calls
    s = Scripted(apply=OSError("gone"), cleanup=OSError("ignored"))
    r = ug.run_upgrade(s, dict(UP), h)
    assert r["state"] == "failed" and not r["retryable"] and "rollback" in s.calls
    s = Scripted()
    s.handoff = True
    assert ug.run_upgrade(s, dict(UP), h)["state"] == "handed_off"
    s = Scripted()
    s.apply = lambda up, host: up.update(_deferred="r.json")
    r = ug.run_upgrade(s, dict(UP), h)
    assert r["state"] == "deferred" and r["result_file"] == "r.json"
    h.windows = False
    assert ug.run_upgrade(Scripted(), dict(UP), h)["stage"] == "plan"


def test_base_pending_and_defaults(tmp_path):
    u, h = ug.Follows(), host(tmp_path)
    base = ug.Upgrader()
    assert base.pending(row("x", "1.0", "2.0", "y"), h)["to"] == "2.0"
    assert base.pending(dict(row("x", "1.0", "2.0", "y"), missing=True), h) is None
    assert base.pending(row("x", "1.0", "2.0", "y", outdated=False), h) is None
    assert base.pending(row("x", "1.0", "manufacturer's latest", "y"), h) is None
    assert base.test(UP, h) is None and base.backup(UP, h) == {}
    assert base.verify(UP, h) is None and base.rollback(UP, {}, h) is None
    base.cleanup(UP, {}, h)
    assert u.pending(row("kernel", "1", "2", "follows"), h) is None


# ------------------------------------------------------------------- WSL --
def apt_run(installed="1.0", cand="1.1", rules=()):
    pol = f"python3:\n  Installed: {installed}\n  Candidate: {cand}\n"
    return Run([*rules, ("apt-cache policy", (0, pol))])


def test_apt_pending_reads_apt_policy(tmp_path):
    a = ug.Apt()
    r = row("python3", "3.12.3", "3.12.3", "apt", outdated=False, package="python3")
    up = a.pending(r, host(tmp_path, apt_run()))
    assert (up["package"], up["from"], up["to"]) == ("python3", "1.0", "1.1")
    assert a.pending(r, host(tmp_path, apt_run("1.1", "1.1"))) is None
    assert a.pending(r, host(tmp_path, apt_run("(none)", "1.1"))) is None
    assert a.pending(r, host(tmp_path, Run())) is None             # no policy output
    assert a.pending(dict(r, missing=True), host(tmp_path, apt_run())) is None
    h = host(tmp_path, apt_run())
    h.windows = False
    assert a.pending(r, h) is None


def test_apt_tests_in_a_copy_then_applies_and_verifies(tmp_path):
    a = ug.Apt()
    up = {"key": "git", "package": "git", "from": "1.0", "to": "1.1"}
    run = apt_run("1.1", "1.1")
    h = host(tmp_path, run, suite=GREEN)
    r = ug.run_upgrade(a, dict(up), h)
    assert r["state"] == "applied", r
    assert run.ran("wsl.exe -d sysperf-upgrade-test -u root") and run.ran("--only-upgrade")
    # the export backup is removed once verified
    assert not list((tmp_path / "stg").glob("*.tar"))


def test_apt_failures_and_rollback(tmp_path):
    a = ug.Apt()
    up = {"key": "git", "package": "git", "from": "1.0", "to": "1.1"}
    h = host(tmp_path, apt_run(rules=[("--only-upgrade", (100, "E: broken"))]), suite=GREEN)
    assert "apt-get failed" in ug.run_upgrade(a, dict(up), h)["reason"]
    h = host(tmp_path, apt_run("1.0", "1.1"), suite=GREEN)        # still old after apply
    r = ug.run_upgrade(a, dict(up), h)
    assert r["state"] == "rolled_back" and "is 1.0, not 1.1" in r["reason"]
    # downgrade fails: fall back to restoring the export, or report that there is none
    run = apt_run("1.0", "1.1", rules=[("--allow-downgrades", (100, "gone"))])
    h = host(tmp_path, run)
    assert a.rollback(up, {"tar": str(tmp_path / "t.tar")}, h) is None and run.ran("--import Ubuntu")
    assert "no backup" in a.rollback(up, {}, h)


def test_copy_test_fails_on_red_suite(tmp_path):
    h = host(tmp_path, apt_run(), suite=RED)
    up = {"key": "git", "package": "git", "from": "1.0", "to": "1.1"}
    r = ug.run_upgrade(ug.Apt(), dict(up), h)
    assert r["state"] == "rejected" and "1 failed" in r["reason"]


def test_ubuntu_release_upgrade(tmp_path):
    u = ug.UbuntuRelease()
    up = u.pending(row("ubuntu", "24.04", "26.04.1", "ubuntu-release"), host(tmp_path))
    assert up["release"] is True
    assert u.pending(row("ubuntu", "24.04.2", "24.04.3", "ubuntu-release"),
                     host(tmp_path))["release"] is False
    assert u.pending(row("ubuntu", "24.04", "26.04", "x", outdated=False), host(tmp_path)) is None
    run = Run([("os-release", (0, 'VERSION="26.04.1 LTS (Resolute)"'))])
    h = host(tmp_path, run, suite=GREEN)
    assert ug.run_upgrade(u, dict(up), h)["state"] == "applied"
    assert run.ran("do-release-upgrade") and run.ran("python3 -m venv --clear")
    point = dict(up, release=False)
    assert u.run_in(point, h, "d") is None and run.calls[-2].count("do-release-upgrade") == 0
    h = host(tmp_path, Run([("dist-upgrade", (1, "held"))]))
    assert "upgrade failed" in u.run_in(up, h, "d")
    h = host(tmp_path, Run([("venv --clear", (1, "no pip"))]))
    assert "sandbox venv" in u.run_in(up, h, "d")
    h = host(tmp_path, Run([("os-release", (0, 'VERSION="24.04.3 LTS"'))]))
    assert "not 26.04.1" in u.check(up, h)
    assert u.rollback(up, {"tar": "t.tar"}, h) is None and u.rollback(up, {}, h) == "no backup to restore"


def test_claude_code(tmp_path):
    c = ug.Claude()
    up = {"key": "claude-code", "from": "2.1.310", "to": "2.1.320"}
    h = host(tmp_path, Run([("claude", (0, "2.1.320 (Claude Code)"))]))
    assert c.run_in(up, h, "d") is None and c.check(up, h) is None
    h = host(tmp_path, Run([("claude", (1, "network"))]))
    assert "claude install failed" in c.run_in(up, h, "d")
    assert "claude reports" in c.check(up, h)
    assert "reinstalling claude" in c.rollback(up, {}, h)
    assert c.rollback(up, {}, host(tmp_path)) is None


# --------------------------------------------------------------- Windows --
def test_pip_tool(tmp_path):
    p = ug.PipTool()
    up = p.pending(row("pip", "26.2.2", "26.3", "pip-tool"), host(tmp_path))
    assert up["package"] == "pip"
    assert p.pending(row("pip", "1", "1", "pip-tool", outdated=False), host(tmp_path)) is None
    run = Run([("--version", (0, "pip 26.3 from x"))])
    h = host(tmp_path, run, suite=GREEN)
    assert ug.run_upgrade(p, dict(up), h)["state"] == "applied"
    assert run.ran("wsl.exe -d Ubuntu -u u -- /home/u/.sysperf-sandbox-venv/bin/python -m pip")
    h = host(tmp_path, Run([("-m venv", (1, "no venv"))]))
    assert "test venv" in p.test(up, h)
    h = host(tmp_path, Run([("pip install -q pip==", (1, "no such"))]))
    assert "into the test venv failed" in p.test(up, h)
    h = host(tmp_path, Run([("--version", (0, "pip 26.2.2"))]))
    assert "reports" in p.test(up, h)
    h = host(tmp_path, Run([("--disable-pip-version-check", (1, "denied"))]))
    assert "failed" in p.apply(up, h) and "pip==26.2.2 failed" in p.rollback(up, {}, h)
    pw = dict(up, package="playwright", key="playwright")
    h = host(tmp_path, Run([("install chromium", (1, "cdn down"))]), sandbox_python="")
    h.sandbox_python = ""
    assert "playwright install chromium failed" in p.apply(pw, h)
    run = Run([("--version", (0, "Version 26.3"))])
    h = host(tmp_path, run)
    assert p._smoke(h, ["py"], pw) is None and run.ran("-m playwright --version")


def test_playwright_browser(tmp_path):
    b = ug.PlaywrightBrowser()
    r = row("chromium", "build 1248", "the build matching Playwright", "playwright-browser")
    h = host(tmp_path, Run([("--dry-run", (0, "browser: chromium-1250 at /x/chromium-1250"))]))
    up = b.pending(r, h)
    assert (up["from"], up["to"]) == ("build 1248", "build 1250")
    h = host(tmp_path, Run([("--dry-run", (0, "chromium-1248"))]))
    assert b.pending(r, h) is None
    assert b.pending(r, host(tmp_path)) is None                       # no dry-run output
    assert b.pending(dict(r, missing=True), h) is None
    h.sandbox_python = ""
    assert b.pending(r, h) is None
    h = host(tmp_path, Run([("-c", (0, "ok"))]))
    assert b.apply(up, h) is None and b.verify(up, h) is None
    h = host(tmp_path, Run([("install chromium", (1, "x")), ("-c", (1, "crash"))]))
    assert "failed" in b.apply(up, h) and "did not launch" in b.verify(up, h)


def test_python_org_stages_then_hands_the_swap_to_a_helper(tmp_path):
    p = ug.PythonOrg()
    nupkg = zbytes(["tools/python.exe"])
    files = {"nuget.org": nupkg, "python-3.15.2-": b"new", "python-3.15.1-": b"old"}
    run = Run()
    h = host(tmp_path, run, files=files)
    up = {"key": "python", "from": "3.15.1", "to": "3.15.2"}
    r = ug.run_upgrade(p, dict(up), h)
    assert r["state"] == "deferred", r
    assert run.ran("-m pytest") and run.ran("--require-hashes")
    script = (tmp_path / "data" / "python_upgrade.ps1").read_text(encoding="utf-8")
    assert "Stop-ScheduledTask -TaskName 'SysPerf Dashboard'" in script
    assert "if ($false)" in script and "Healthy" in script
    minor = dict(up, to="3.16.0")
    files["python-3.16.0-"] = b"newer"
    assert p.apply(minor, h) is None
    assert "if ($true)" in (tmp_path / "data" / "python_upgrade.ps1").read_text(encoding="utf-8")
    h = host(tmp_path, Run([("-m pytest", (1, "3 failed"))]), files=files)
    assert "3 failed" in p.test(up, h)
    h = host(tmp_path, Run([("Start-Process powershell.exe", (1, "blocked"))]), files=files)
    assert "upgrade helper" in p.apply(up, h)


def test_pwsh(tmp_path):
    w = ug.Pwsh()
    files = {".zip": zbytes(["pwsh.exe"]), ".msix": b"msix"}
    run = Run([("-File", (0, '{"cpu": 1}')), ("PSVersion", (0, "7.6.10"))])
    h = host(tmp_path, run, files=files)
    up = {"key": "powershell", "from": "7.6.9", "to": "7.6.10"}
    assert ug.run_upgrade(w, dict(up), h)["state"] == "applied"
    assert run.ran("Add-AppxPackage")
    h = host(tmp_path, Run([("-File", (0, "error"))]), files=files)
    assert "does not run" in w.test(up, h)
    h = host(tmp_path, Run([("Add-AppxPackage", (1, "denied")), ("PSVersion", (0, "7.6.9"))]),
             files=files)
    assert "Add-AppxPackage failed" in w.apply(up, h)
    assert "pwsh reports" in w.verify(up, h)
    assert "reinstalling PowerShell" in w.rollback(up, {}, h)
    assert w.rollback(up, {}, host(tmp_path, files=files)) is None


def test_winget_git(tmp_path):
    g = ug.Winget()
    rel = {"tag_name": "v2.57.0.windows.1",
           "assets": [{"name": "PortableGit-2.57.0-64-bit.7z.exe", "browser_download_url": "u/pg"}]}
    files = {"releases/latest": rel, "u/pg": b"exe"}
    run = Run([("winget list", (0, "Git  Git.Git  2.56.0.3  2.57.0 winget")),
               ("--version", (0, "git version 2.57.0.windows.1"))])
    h = host(tmp_path, run, files=files)
    up = {"key": "git-windows", "id": "Git.Git", "from": "2.56.0", "to": "2.57.0"}
    assert ug.run_upgrade(g, dict(up), h)["state"] == "applied"
    assert run.ran("worktree add") and run.ran("reset -q --keep") and run.ran("winget upgrade")
    assert g.backup(up, h) == {"version": "2.56.0.3"}
    assert g.backup(up, host(tmp_path)) == {"version": ""}
    assert "newest Git for Windows release" in g.test(dict(up, to="2.58.0"), h)
    h = host(tmp_path, Run([("-y", (2, "corrupt"))]), files=files)
    assert "did not unpack" in g.test(up, h)
    h = host(tmp_path, Run([("init -q", (1, "no"))]), files=files)
    assert "git init failed" in g._smoke(h, "git")
    h = host(tmp_path, Run([("merge -q", (1, "diverged"))]), files=files)
    assert "git merge failed" in g._smoke(h, "git")
    h = host(tmp_path, Run([("winget", (5, "blocked")), ("--version", (0, "git version 2.56"))]))
    assert "winget upgrade failed" in g.apply(up, h)
    assert "git reports" in g.verify(up, h)
    assert "unknown" in g.rollback(up, {}, h)
    assert "reinstalling" in g.rollback(up, {"version": "2.56.0.3"}, h)
    assert g.rollback(up, {"version": "2.56.0.3"}, host(tmp_path)) is None


def test_wsl_platform(tmp_path):
    w = ug.Wsl()
    rel = lambda v: {"tag_name": v, "assets": [  # noqa: E731
        {"name": f"wsl.{v}.0.x64.msi", "browser_download_url": f"u/wsl-{v}"}]}
    files = {"tags/3.1.0": rel("3.1.0"), "tags/3.0.3": rel("3.0.3"),
             "u/wsl-3.1.0": b"m1", "u/wsl-3.0.3": b"m0"}
    run = Run([("Get-AuthenticodeSignature", (0, "signed")), ("RunAs", (0, "")),
               ("--version", (0, "WSL version: 3.1.0.0"))])
    h = host(tmp_path, run, files=files, suite=GREEN)
    up = {"key": "wsl", "from": "3.0.3", "to": "3.1.0"}
    assert ug.run_upgrade(w, dict(up), h)["state"] == "applied"
    h = host(tmp_path, Run([("Get-AuthenticodeSignature", (0, "NotSigned"))]), files=files)
    with pytest.raises(ValueError, match="not validly signed"):
        w.test(dict(up), h)
    up["_msi"] = "x.msi"
    h = host(tmp_path, Run([("RunAs", (1223, "declined"))]))
    assert "UAC" in w.apply(up, h)
    h = host(tmp_path, Run([("Get-AuthenticodeSignature", (0, "signed")), ("RunAs", (1603, "e")),
                            ("--version", (0, "WSL version: 3.0.3.0"))]), files=files)
    assert "msiexec exit 1603" in w.apply(up, h)
    assert "does not report" in w.verify(up, h)
    assert "reinstalling WSL" in w.rollback(up, {}, h)


def test_handoffs(tmp_path):
    wu, vu = ug.WindowsUpdate(), ug.VendorUpdate()
    r = row("bios", "F.26", "manufacturer's latest", "windows-update")
    up = wu.pending(r, host(tmp_path))
    assert up["to"] == "manufacturer's latest"
    assert wu.pending(dict(r, outdated=False), host(tmp_path)) is None
    run = Run()
    h = host(tmp_path, run)
    assert ug.run_upgrade(wu, dict(up), h)["state"] == "handed_off" and run.ran("UsoClient")
    assert ug.run_upgrade(vu, dict(up), h)["state"] == "handed_off" and run.ran("EdgeUpdate")
    h = host(tmp_path, Run(default=(1, "denied")))
    assert "UsoClient exit 1" in wu.apply(up, h) and "did not start" in vu.apply(up, h)


# ------------------------------------------------------ codevolve host lane --
class Probe(ug.Upgrader):
    via, label = "probe", "probe upgrader"
    windows_only = False

    def __init__(self, result=None, raises=None, restart=False, retry_s=0):
        self.result, self.raises = result, raises
        self.restart, self.retry_s = restart, retry_s

    def pending(self, row, host):
        if self.raises:
            raise self.raises
        return super().pending(row, host)

    def apply(self, up, host):
        return None


@pytest.fixture
def probe(monkeypatch):
    def install(u):
        monkeypatch.setitem(ug.REGISTRY, "probe", u)
        return u
    return install


def hostlane(tmp_path, repo_dir, rows):
    e, db = eng(tmp_path, repo_dir, box=Box())
    e.components_fn = lambda: rows
    e.run = Run([("WSL_DISTRO_NAME", (0, "id=Ubuntu|u"))])
    return e, db


def test_host_upgrade_is_recorded_applied_and_restarts(tmp_path, repo, probe, monkeypatch):  # noqa: F811
    probe(Probe(restart=True))
    e, db = hostlane(tmp_path, repo, [row("tool", "1.0", "2.0", "probe")])
    restarts = []
    e.on_applied = lambda: restarts.append(1)
    res = e.run_cycle("manual", "self_evolve")
    assert res["state"] == "completed" and "applied" in res["explanation"]
    c = db.candidates()[0]
    assert c["title"] == "3PP upgrade: tool 1.0 → 2.0" and c["review_state"] == "applied"
    assert c["decision"] == "retained" and c["tests_ok"] == 1 and restarts == [1]
    events = [a["event"] for a in db.audit_tail(limit=10)]
    assert "upgrade.applied" in events and "upgrade.step" in events
    rows = e.status("self_evolve")["components"]["rows"]
    assert rows[0]["label"] == "probe upgrader" and rows[0]["review_state"] == "applied"
    # tried once: the next cycle has nothing to do and, with a model, runs the code lane
    assert e._component_lane("x", repo, "HEAD", {}, repo) is None


def test_host_upgrade_outcomes(tmp_path, repo, probe, monkeypatch):  # noqa: F811
    p = probe(Probe())
    e, db = hostlane(tmp_path, repo, [row("tool", "1.0", "2.0", "probe")])
    results = iter([{"state": "failed", "stage": "test", "reason": "offline", "retryable": True},
                    {"state": "rejected", "stage": "test", "reason": "red"},
                    {"state": "rolled_back", "stage": "verify", "reason": "slow"},
                    {"state": "failed", "stage": "apply", "reason": "stuck"}])
    monkeypatch.setattr(ug, "run_upgrade", lambda u, up, host, log: next(results))
    r = e._host_upgrade("c1", p, {"key": "tool", "name": "t", "from": "1", "to": "2"},
                        e._host(repo, {}))
    assert r["state"] == "failed" and db.candidates() == []           # retried later
    for cid, want in (("c2", ("rejected", "none", 0)), ("c3", ("rejected", "rolled_back", 1)),
                      ("c4", ("rejected", "none", 1))):
        db.start_cycle(cid, "manual")
        e._host_upgrade(cid, p, {"key": "tool", "name": "t", "from": "1", "to": cid},
                        e._host(repo, {}))
        c = db.candidate(f"c-{cid}")
        assert (c["decision"], c["review_state"], c["tests_ok"]) == want


def test_handoff_is_repeated_once_per_period(tmp_path, repo, probe):  # noqa: F811
    u = probe(Probe(retry_s=86400))
    u.handoff = True
    e, db = hostlane(tmp_path, repo, [row("bios", "F.26", "2.0", "probe")])
    e.clock = lambda: 86400 * 5 + 10
    e.run_cycle("manual", "self_evolve")
    c = db.candidates()[0]
    assert c["title"].endswith("(#5)") and c["review_state"] == "handed_off"
    assert e._component_lane("x", repo, "HEAD", {}, repo) is None
    e.clock = lambda: 86400 * 6 + 10
    assert e._upgrade_title(u, {"key": "bios", "from": "F.26", "to": "2.0"}).endswith("(#6)")


def test_a_broken_probe_does_not_stop_the_lane(tmp_path, repo, probe):  # noqa: F811
    probe(Probe(raises=OSError("wsl down")))
    e, db = hostlane(tmp_path, repo, [row("tool", "1.0", "2.0", "probe")])
    assert e._component_lane("x", repo, "HEAD", {}, repo) is None


def test_no_model_still_runs_the_component_lane(tmp_path, repo, probe):  # noqa: F811
    probe(Probe())
    e, db = eng(tmp_path, repo, provider=False, box=Box())
    assert "no LLM provider" in e.gate("manual", "self_evolve")
    e.components_fn = lambda: [row("tool", "1.0", "2.0", "probe")]
    e.run = Run()
    assert e.gate("manual", "self_evolve") is None
    assert "applied" in e.run_cycle("manual", "self_evolve")["explanation"]
    res = e.run_cycle("manual", "self_evolve")          # nothing left, no model
    assert res["state"] == "skipped" and "needs a model" in res["explanation"]


def test_only_windows_side_upgraders_are_standalone():
    assert sorted(v for v, u in ug.REGISTRY.items() if u.standalone) == [
        "pwsh", "vendor-update", "windows-update", "winget", "wsl"]


def test_upgrades_run_without_a_model_sandbox_or_checkout(tmp_path, repo, probe):  # noqa: F811
    """A PC with no AI provider, no WSL sandbox and no source checkout still upgrades
    its Windows-side components: unattended from Safe-auto, or with Upgrade now."""
    u = probe(Probe(restart=True))
    u.standalone = True
    e, db = eng(tmp_path, repo, provider=False, box=cv.Sandbox(backend="none"))
    e.settings["repo"] = None                              # no source checkout
    e.run = Run()
    assert e.upgrade_gate("safe_auto") == "auto-upgrade is off"      # no Components rows
    e.components_fn = lambda: [row("tool", "1.0", "2.0", "probe")]
    assert e.upgrade_gate("off", manual=True) == "autonomy level is 'off'"
    assert "press Upgrade now" in e.upgrade_gate("observe")
    assert e.upgrade_gate("observe", manual=True) is None
    restarts = []
    e.on_applied = lambda: restarts.append(1)
    res = e.upgrade_pass("safe_auto")
    assert res["started"] and res["state"] == "completed" and restarts == [1]
    c = db.candidates()[0]
    assert c["title"] == "3PP upgrade: tool 1.0 → 2.0" and c["review_state"] == "applied"
    assert "next upgrade check in" in e.upgrade_gate("safe_auto")
    assert e.upgrade_pass("observe", "manual") == {
        "started": False, "reason": "every component is current or already tried"}
    st = e.status("safe_auto")
    assert st["upgrades"] == {"gate": e.upgrade_gate("safe_auto"), "manual": None}
    assert st["components"]["rows"][0]["standalone"] is True


def test_one_pass_upgrades_every_outdated_component(tmp_path, repo, probe):  # noqa: F811
    """5.4: a pass reaches the latest of every standalone component, not one per
    cycle; the evolution cycle's lane leaves them to it."""
    u = probe(Probe())
    u.standalone = True
    e, db = eng(tmp_path, repo, provider=False, box=Box())
    e.run = Run()
    e.components_fn = lambda: [row("a", "1.0", "2.0", "probe"), row("b", "3.0", "4.0", "probe")]
    assert e._component_lane("x", repo, "HEAD", {}, repo) is None      # left to the pass
    seen = []
    e.before_change = lambda why: seen.append(why) or {"state": "recent", "message": "ok"}
    res = e.upgrade_pass("safe_auto")
    assert [x["state"] for x in res["upgrades"]] == ["completed", "completed"]
    assert sorted(c["title"] for c in db.candidates()) == [
        "3PP upgrade: a 1.0 → 2.0", "3PP upgrade: b 3.0 → 4.0"]
    assert seen == ["upgrading A", "upgrading B"]
    assert any(a["event"] == "upgrade.restore_point" for a in db.audit_tail(limit=30))
    # one that needs SysPerf to restart ends the pass; the rest go next time
    u.restart = True
    e.components_fn = lambda: [row("c", "1.0", "2.0", "probe"), row("d", "1.0", "2.0", "probe")]
    e.on_applied = lambda: None
    assert len(e.upgrade_pass("observe", "manual")["upgrades"]) == 1


def test_upgrade_pass_skips_what_it_cannot_run_and_survives_errors(tmp_path, repo, probe,  # noqa: F811
                                                                   monkeypatch):
    p = probe(Probe(raises=OSError("down")))
    p.standalone = True
    e, db = eng(tmp_path, repo, provider=False, box=Box())
    e.run = Run()
    rows = [row("tool", "1.0", "2.0", "probe"), dict(row("gone", "1", "2", "probe"), missing=True),
            row("flask", "1", "2", "pip-lock"), row("odd", "1", "2", "no-such-upgrader")]
    e.components_fn = lambda: rows
    assert e.upgrade_pass("observe", "manual")["reason"].startswith("every component")
    assert e.upgrade_gate("self_evolve").startswith("next upgrade check")  # any auto level
    e._lock.acquire()
    assert e.upgrade_pass("observe", "manual")["reason"] == "a cycle is already running"
    e._lock.release()
    p.raises = None

    def boom(*a):
        raise RuntimeError("boom")
    monkeypatch.setattr(e, "_host_upgrade", boom)
    res = e.upgrade_pass("observe", "manual")
    assert res["started"] and res["state"] == "failed" and res["error"] == "boom"
    assert db.cycles(limit=1)[0]["state"] == "failed"
    e.components_fn = lambda: 1 / 0
    assert e.upgrade_pass("observe", "manual") == {
        "started": False, "cycle": None, "state": "failed", "error": "division by zero"}


def test_host_context_and_wsl_identity(tmp_path, repo, monkeypatch):  # noqa: F811
    e, db = hostlane(tmp_path, repo, [])
    e.sandbox = cv.Sandbox(backend="none")
    monkeypatch.setattr(cv, "OS_NAME", "posix")
    assert e._wsl_identity() == ("", "")                 # not Windows
    monkeypatch.setattr(cv, "OS_NAME", "nt")
    e._wsl_id = None
    assert e._wsl_identity() == ("Ubuntu", "u")
    seen = []
    e._tests = lambda wt, path=None, sandbox=None: seen.append(sandbox) or {"rc": 0}
    h = e._host(repo, {"passed": 1})
    assert h.distro == "Ubuntu" and h.windows
    h.suite()
    h.suite(distro="copy")
    assert seen[0] is e.sandbox and (seen[1].distro, seen[1].user) == ("copy", "u")


def test_sandbox_in_distro_targets_that_distro():
    box = cv.Sandbox(backend="wsl-bwrap")
    box.wsl_python = "/home/u/.venv/bin/python"
    argv = box.in_distro("copy", "u").argv(Path("C:/w"), ["python"])
    assert argv[:6] == ["wsl.exe", "-d", "copy", "-u", "u", "-e"]
    assert box.argv(Path("C:/w"), ["python"])[:2] == ["wsl.exe", "-e"]


def test_sandbox_pyver(tmp_path, repo):  # noqa: F811
    e, db = hostlane(tmp_path, repo, [])
    assert e._sandbox_pyver() == "312"                  # Box answers no version
    e.sandbox.run = lambda wt, cmd, timeout_s=None: {"stdout": "PYV=313\n"}
    assert e._sandbox_pyver() == "313"


def test_deferred_interpreter_upgrade_is_reconciled(tmp_path, repo):  # noqa: F811
    e, db = hostlane(tmp_path, repo, [])
    e._reconcile_upgrades()                              # no result file: nothing happens
    for cid, state in (("d1", "applied"), ("d2", "rolled_back")):
        db.start_cycle(cid, "x")
        db.add_candidate({"id": f"c-{cid}", "cycle_id": cid, "created": 1.0,
                          "title": f"3PP upgrade: python 3.15.1 → {cid}", "decision": "retained",
                          "review_state": "verifying"})
        (e.state_dir / "upgrade_result.json").write_text(json.dumps(
            {"key": "python", "from": "3.15.1", "to": cid, "state": state, "reason": "r"}),
            encoding="utf-8-sig")
        e.status("self_evolve")
        assert db.candidate(f"c-{cid}")["review_state"] == state
        assert not (e.state_dir / "upgrade_result.json").exists()


def test_run_text_reports_rc_output_timeouts_and_missing_programs(tmp_path):
    import sys
    rc, out = cv.run_text([sys.executable, "-c", "print('hi')"], env={"X": "1"})
    assert rc == 0 and "hi" in out
    assert cv.run_text([sys.executable, "-c", "import time; time.sleep(5)"], timeout=0.5)[0] == 124
    assert cv.run_text([str(tmp_path / "nope.exe")])[0] == 127



def test_the_first_export_measures_a_staging_folder_that_does_not_exist_yet(tmp_path):
    # 5.4 live: shutil.disk_usage raised WinError 3 on the missing staging folder
    h = host(tmp_path, Run([("--export", (0, ""))]))

    def free_gb(path):          # like shutil.disk_usage on Windows, without needing
        if not path.exists():   # 20 GB free here (the sandbox's /tmp is a small tmpfs)
            raise FileNotFoundError(f"[WinError 3] {path}")
        return 100.0
    h.free_gb = free_gb
    assert not h.staging.exists()
    assert h.export_distro().name == "Ubuntu-backup.tar"
