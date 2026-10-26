"""
Component upgrades for self-evolution (5.1.1).

Every row of the Components view names an upgrader in its ``upgrade`` field in
components.json, and ``tests/test_upgrader.py`` fails if a row has none, so a
component added later is covered from the day it is listed. A self-evolution
cycle takes the first outdated row that has not been tried at that version and
runs it through one pipeline:

    pending -> test (isolated) -> backup -> apply -> verify -> keep | roll back

Where each kind is tested before it touches the real install:

* ``pip-lock`` / ``pip-dev``  SysPerf's pinned packages. The repository lane in
  codevolve moves the pin and runs the suite in the bubblewrap sandbox.
* ``pip-tool``      pip, Playwright: a throwaway venv in the staging folder.
* ``apt``, ``claude``, ``ubuntu-release``  WSL-side software: the distro is
  exported, imported as a throwaway copy, upgraded there, and SysPerf's suite
  runs in the copy's sandbox. The export is kept as the backup until the real
  upgrade is verified; a failed release upgrade restores it.
* ``python-org``    the interpreter SysPerf runs on: the same release from
  NuGet, unpacked into a staging venv that runs the full Windows suite. The real
  upgrade needs SysPerf stopped, so a detached helper stops the task, upgrades,
  starts it, waits for /api/health and reverts on failure.
* ``pwsh``          PowerShell (MSIX): the release zip runs the real collector
  script, and its output must parse as a sample.
* ``winget``        Git for Windows: PortableGit of that release runs every git
  operation self-evolution uses (worktree, commit, fast-forward, reset --keep).
* ``wsl``           the WSL platform: the signed MSI's Authenticode signature
  is checked first; after the upgrade the sandbox suite must still pass.
* ``playwright-browser``  Chromium: installed beside the old build (Playwright
  pins its build, so nothing is replaced) and launched headless.
* ``windows-update`` / ``vendor-update``  drivers, the BIOS, the Windows build,
  Edge and WebView2 have no package SysPerf can stage. Their own updater, which
  tests and rolls back its packages, is started, and the installed version is
  checked afterwards. SysPerf never flashes firmware itself.
* ``follows``       the WSL kernel ships inside the WSL platform package.

Installers that need administrator rights (WSL, machine-wide Git) raise the
normal Windows UAC prompt; declined or unanswered, the upgrade is reported and
retried on a later cycle. Every side effect goes through ``Host``, so the logic
is tested without touching the machine.
"""
from __future__ import annotations

import hashlib
import json
import platform
import re
import shutil
import time
import zipfile
from dataclasses import dataclass, field
from pathlib import Path
from typing import Callable

TASK = "SysPerf Dashboard"
TEST_DISTRO = "sysperf-upgrade-test"
MIN_FREE_GB = 20


def _key(v: str) -> tuple:
    return tuple(int(x) for x in re.findall(r"\d+", v or ""))


def _is_version(v: str) -> bool:
    return bool(re.fullmatch(r"v?\d+(?:\.\d+)+\S*", (v or "").strip()))


def _q(s: str) -> str:
    """A PowerShell single-quoted literal."""
    return "'" + str(s).replace("'", "''") + "'"


def _sh(s: str) -> str:
    """A POSIX shell single-quoted literal."""
    return "'" + str(s).replace("'", "'\\''") + "'"


def wsl_path(p) -> str:
    p = str(p).replace("\\", "/")
    return f"/mnt/{p[0].lower()}{p[2:]}" if len(p) > 1 and p[1] == ":" else p


def arch() -> str:
    return "arm64" if platform.machine().lower() in ("arm64", "aarch64") else "x64"


@dataclass
class Host:
    """Everything an upgrader may do to the machine, injected."""
    run: Callable                 # run(argv, timeout=, cwd=, env=) -> (rc, text)
    get: Callable                 # get(url) -> bytes
    staging: Path
    repo: Path
    data_dir: Path
    windows: bool = True
    win_python: str = ""          # console python.exe of SysPerf's venv
    venv_dir: Path | None = None
    sandbox_python: str = ""      # Linux path of the WSL sandbox venv's python
    distro: str = ""
    wsl_user: str = ""
    suite: Callable | None = None   # suite(distro=None) -> pytest result dict
    baseline: dict = field(default_factory=dict)
    free_gb: Callable = lambda path: shutil.disk_usage(path).free / 1e9
    clock: Callable = time.time
    sleep: Callable = time.sleep

    # ------------------------------------------------------------ helpers
    def ps(self, script: str, timeout: float = 900):
        return self.run(["powershell.exe", "-NoProfile", "-NonInteractive", "-ExecutionPolicy",
                         "Bypass", "-Command", script], timeout=timeout)

    def wsl(self, cmd: str, distro: str = "", root: bool = False, timeout: float = 900):
        return self.run(["wsl.exe", "-d", distro or self.distro, "-u",
                         "root" if root else self.wsl_user, "--", "sh", "-c", cmd],
                        timeout=timeout)

    def elevated(self, exe: str, args: str, timeout: float = 1800):
        """Run with a UAC prompt; rc 1223 means the prompt was declined."""
        return self.ps(f"try {{ $p = Start-Process -FilePath {_q(exe)} -ArgumentList {_q(args)} "
                       "-Verb RunAs -Wait -PassThru -WindowStyle Hidden; exit $p.ExitCode } "
                       "catch { Write-Output $_.Exception.Message; exit 1223 }", timeout=timeout)

    def download(self, url: str, name: str, sha256: str | None = None) -> Path:
        data = self.get(url)
        if sha256 and hashlib.sha256(data).hexdigest() != sha256.lower():
            raise ValueError(f"{name}: sha256 does not match the published one")
        self.staging.mkdir(parents=True, exist_ok=True)
        p = self.staging / name
        p.write_bytes(data)
        return p

    def github_asset(self, repo: str, pattern: str, tag: str | None = None) -> tuple:
        """(url, release tag) of the asset matching ``pattern`` in a release."""
        path = f"tags/{tag}" if tag else "latest"
        rel = json.loads(self.get(f"https://api.github.com/repos/{repo}/releases/{path}"))
        for a in rel.get("assets") or []:
            if re.fullmatch(pattern, a.get("name", "")):
                return a["browser_download_url"], rel.get("tag_name", "")
        raise ValueError(f"no release asset of {repo} matches {pattern}")

    def suite_ok(self, distro: str = "") -> str | None:
        """None when SysPerf's suite passes as fully as the baseline did."""
        if self.suite is None:
            return None
        t = self.suite(distro=distro or None)
        b = self.baseline or {}
        if t.get("blocked"):
            return "the sandbox could not run: " + str(t.get("summary"))
        if t.get("rc") != 0 or t.get("failed") or t.get("errors") or \
                t.get("passed", 0) < b.get("passed", 0):
            return f"SysPerf's tests: {t.get('summary')} (before: {b.get('summary')})"
        return None

    # ------------------------------------------- throwaway copy of the distro
    def export_distro(self) -> Path:
        if self.free_gb(self.staging) < MIN_FREE_GB:
            raise RuntimeError(f"less than {MIN_FREE_GB} GB free for a test copy of the distro")
        self.staging.mkdir(parents=True, exist_ok=True)
        tar = self.staging / f"{self.distro}-backup.tar"
        rc, out = self.run(["wsl.exe", "--export", self.distro, str(tar)], timeout=3600)
        if rc != 0:
            raise RuntimeError(f"wsl --export failed: {out[-300:]}")
        return tar

    def import_copy(self, tar: Path) -> str:
        self.drop_copy()
        rc, out = self.run(["wsl.exe", "--import", TEST_DISTRO, str(self.staging / TEST_DISTRO),
                            str(tar)], timeout=3600)
        if rc != 0:
            raise RuntimeError(f"wsl --import failed: {out[-300:]}")
        return TEST_DISTRO

    def drop_copy(self) -> None:
        self.run(["wsl.exe", "--unregister", TEST_DISTRO], timeout=600)
        shutil.rmtree(self.staging / TEST_DISTRO, ignore_errors=True)

    def restore_distro(self, tar: Path) -> str | None:
        """Replace the distro with its backup (the last resort of a rollback)."""
        home = self.data_dir / "wsl" / self.distro
        self.run(["wsl.exe", "--unregister", self.distro], timeout=600)
        rc, out = self.run(["wsl.exe", "--import", self.distro, str(home), str(tar)],
                           timeout=3600)
        if rc != 0:
            return f"restoring {self.distro} from {tar} failed: {out[-300:]}"
        self.wsl(f"printf '[user]\\ndefault={self.wsl_user}\\n' >> /etc/wsl.conf", root=True)
        self.run(["wsl.exe", "--set-default", self.distro], timeout=60)
        return None


# ===================================================================== base
class Upgrader:
    via = ""
    label = ""
    repo = False          # handled by codevolve's repository lane (pins in git)
    restart = False       # SysPerf restarts to load the upgrade
    handoff = False       # another updater finishes it; verified on later samples
    windows_only = True
    retry_s = 0           # >0: a hand-off may be repeated after this long

    def pending(self, row: dict, host: Host) -> dict | None:
        cur, new = row.get("current") or "", row.get("latest") or ""
        if row.get("missing") or not row.get("outdated") or not _is_version(new):
            return None
        return {"key": row["key"], "name": row.get("name", row["key"]),
                "from": cur, "to": new, **(row.get("upgrade") or {})}

    def test(self, up: dict, host: Host) -> str | None:
        return None

    def backup(self, up: dict, host: Host) -> dict:
        return {}

    def apply(self, up: dict, host: Host) -> str | None:     # pragma: no cover - abstract
        raise NotImplementedError

    def verify(self, up: dict, host: Host) -> str | None:
        return None

    def rollback(self, up: dict, bk: dict, host: Host) -> str | None:
        return None

    def cleanup(self, up: dict, bk: dict, host: Host) -> None:
        pass


def run_upgrade(u: Upgrader, up: dict, host: Host, log: Callable = lambda m: None) -> dict:
    """The pipeline. Returns {state, stage, reason, ...}; never raises."""
    out = {"key": up["key"], "from": up["from"], "to": up["to"], "via": u.via}
    if u.windows_only and not host.windows:
        return {**out, "state": "rejected", "stage": "plan",
                "reason": f"the {u.via} upgrader runs on a Windows install of SysPerf"}
    bk: dict = {}
    stage = "test"
    try:
        log(f"testing {up['key']} {up['to']} in isolation")
        err = u.test(up, host)
        if err:
            return {**out, "state": "rejected", "stage": "test", "reason": err}
        stage = "backup"
        bk = u.backup(up, host) or {}
        stage = "apply"
        log(f"applying {up['key']} {up['to']}")
        err = u.apply(up, host)
        if err:
            rb = u.rollback(up, bk, host)
            return {**out, "state": "rolled_back" if not rb else "failed", "stage": "apply",
                    "reason": err, "rollback_error": rb}
        if u.handoff:
            return {**out, "state": "handed_off", "stage": "apply",
                    "reason": f"{u.label} started; the version is checked on later samples"}
        if up.get("_deferred"):
            return {**out, "state": "deferred", "stage": "apply", "result_file": up["_deferred"],
                    "reason": "SysPerf restarts to finish this upgrade; the helper verifies "
                              "it and reverts it if SysPerf does not come back healthy"}
        stage = "verify"
        err = u.verify(up, host)
        if err:
            rb = u.rollback(up, bk, host)
            return {**out, "state": "rolled_back" if not rb else "failed", "stage": "verify",
                    "reason": err, "rollback_error": rb}
        return {**out, "state": "applied", "stage": "verify",
                "reason": f"{up['key']} {up['from']} → {up['to']}: tested, applied and verified"}
    except Exception as exc:                # a download, a timeout, a parse error
        rb = u.rollback(up, bk, host) if stage in ("apply", "verify") else None
        return {**out, "state": "failed", "stage": stage, "reason": f"{type(exc).__name__}: {exc}",
                "rollback_error": rb, "retryable": stage in ("test", "backup")}
    finally:
        try:
            u.cleanup(up, bk, host)
        except Exception:       # cleanup must never mask the result
            pass


# ============================================================= repository
class PipLock(Upgrader):
    via, label, repo = "pip-lock", "pinned in requirements.lock (hashes)", True


class PipDev(Upgrader):
    via, label, repo = "pip-dev", "pinned in requirements-dev.txt", True


# ================================================================ WSL side
class _InCopy(Upgrader):
    """Test in a throwaway copy of the distro; keep its export as the backup."""
    label = "tested in a throwaway copy of the WSL distro"

    def run_in(self, up: dict, host: Host, distro: str) -> str | None:  # pragma: no cover
        raise NotImplementedError

    def test(self, up, host):
        up["_tar"] = str(host.export_distro())
        copy = host.import_copy(Path(up["_tar"]))
        try:
            return self.run_in(up, host, copy) or host.suite_ok(copy)
        finally:
            host.drop_copy()

    def backup(self, up, host):
        return {"tar": up.get("_tar")}

    def apply(self, up, host):
        return self.run_in(up, host, host.distro)

    def verify(self, up, host):
        return self.check(up, host) or host.suite_ok()

    def check(self, up, host) -> str | None:     # pragma: no cover - abstract
        raise NotImplementedError

    def cleanup(self, up, bk, host):
        if up.get("_tar"):
            Path(up["_tar"]).unlink(missing_ok=True)


def _venv_fix(host: Host, distro: str) -> str:
    """Shell that rebuilds the sandbox venv if its interpreter stopped working."""
    py = host.sandbox_python
    venv = py.rsplit("/bin/", 1)[0]
    repo = wsl_path(host.repo)
    return (f"{_sh(py)} -c 'import flask, waitress, pytest' 2>/dev/null || "
            f"(python3 -m venv --clear {_sh(venv)} && "
            f"{_sh(venv)}/bin/pip install -q --require-hashes -r {_sh(repo)}/requirements.lock && "
            f"{_sh(venv)}/bin/pip install -q -r {_sh(repo)}/requirements-dev.txt)")


class Apt(_InCopy):
    via = "apt"
    label = "apt in WSL, tested in a throwaway copy of the distro"

    def policy(self, host: Host, pkg: str, distro: str = "") -> tuple:
        rc, out = host.wsl(f"apt-get update -qq >/dev/null 2>&1; apt-cache policy {_sh(pkg)}",
                           distro=distro, root=True, timeout=600)
        inst = re.search(r"Installed:\s*(\S+)", out)
        cand = re.search(r"Candidate:\s*(\S+)", out)
        return (inst.group(1) if inst else ""), (cand.group(1) if cand else "")

    def pending(self, row, host):
        if row.get("missing") or not host.windows:
            return None
        pkg = (row.get("upgrade") or {}).get("package") or row["key"]
        inst, cand = self.policy(host, pkg)
        if not inst or inst == "(none)" or not cand or cand == "(none)" or inst == cand:
            return None
        return {"key": row["key"], "name": row.get("name", row["key"]), "package": pkg,
                "from": inst, "to": cand, "via": self.via}

    def run_in(self, up, host, distro):
        rc, out = host.wsl("DEBIAN_FRONTEND=noninteractive apt-get install -y -q --only-upgrade "
                           f"{_sh(up['package'])}={_sh(up['to'])}", distro=distro, root=True,
                           timeout=1800)
        return None if rc == 0 else f"apt-get failed: {out[-300:]}"

    def check(self, up, host):
        inst, _ = self.policy(host, up["package"])
        return None if inst == up["to"] else f"{up['package']} is {inst}, not {up['to']}"

    def rollback(self, up, bk, host):
        rc, out = host.wsl("DEBIAN_FRONTEND=noninteractive apt-get install -y -q "
                           f"--allow-downgrades {_sh(up['package'])}={_sh(up['from'])}",
                           root=True, timeout=1800)
        if rc == 0:
            return None
        return host.restore_distro(Path(bk["tar"])) if bk.get("tar") else \
            f"downgrade failed and there is no backup: {out[-200:]}"


class UbuntuRelease(_InCopy):
    via = "ubuntu-release"
    label = "release upgrade, tested in a throwaway copy; the export is the backup"

    def pending(self, row, host):
        up = super().pending(row, host)
        if up:
            up["release"] = _key(up["to"])[:2] > _key(up["from"])[:2]
        return up

    def run_in(self, up, host, distro):
        if up["release"]:
            cmd = ("sed -i 's/^Prompt=.*/Prompt=lts/' /etc/update-manager/release-upgrades; "
                   "DEBIAN_FRONTEND=noninteractive apt-get update -qq && "
                   "DEBIAN_FRONTEND=noninteractive apt-get -y -q dist-upgrade && "
                   "do-release-upgrade -f DistUpgradeViewNonInteractive")
        else:
            cmd = ("DEBIAN_FRONTEND=noninteractive apt-get update -qq && "
                   "DEBIAN_FRONTEND=noninteractive apt-get -y -q dist-upgrade")
        rc, out = host.wsl(cmd, distro=distro, root=True, timeout=4 * 3600)
        if rc != 0:
            return f"the upgrade failed: {out[-300:]}"
        rc, out = host.wsl(_venv_fix(host, distro), distro=distro, timeout=1800)
        return None if rc == 0 else f"rebuilding the sandbox venv failed: {out[-300:]}"

    def check(self, up, host):
        rc, out = host.wsl("grep '^VERSION=' /etc/os-release")
        got = re.search(r"(\d+(?:\.\d+)+)", out)
        same = bool(got) and _key(got.group(1))[:2] == _key(up["to"])[:2]
        return None if same else f"the distro reports {out.strip()[:80]}, not {up['to']}"

    def rollback(self, up, bk, host):
        return host.restore_distro(Path(bk["tar"])) if bk.get("tar") else "no backup to restore"


class Claude(_InCopy):
    via = "claude"
    label = "claude install <version>, tested in a throwaway copy of the distro"

    def run_in(self, up, host, distro):
        rc, out = host.wsl(f"$HOME/.local/bin/claude install {_sh(up['to'])} && "
                           "$HOME/.local/bin/claude --version", distro=distro, timeout=1800)
        return None if rc == 0 and up["to"] in out else f"claude install failed: {out[-300:]}"

    def check(self, up, host):
        rc, out = host.wsl("$HOME/.local/bin/claude --version")
        return None if up["to"] in out else f"claude reports {out.strip()[:80]}"

    def rollback(self, up, bk, host):
        rc, out = host.wsl(f"$HOME/.local/bin/claude install {_sh(up['from'])}", timeout=1800)
        return None if rc == 0 else f"reinstalling claude {up['from']} failed: {out[-200:]}"


# ============================================================ Windows side
class PipTool(Upgrader):
    via = "pip-tool"
    label = "pip install, tested in a throwaway venv"

    def _pythons(self, host):
        out = [[host.win_python]]
        if host.sandbox_python:
            out.append(["wsl.exe", "-d", host.distro, "-u", host.wsl_user, "--",
                        host.sandbox_python])
        return out

    def _install(self, host, pkg, ver) -> str | None:
        for py in self._pythons(host):
            rc, out = host.run([*py, "-m", "pip", "install", "-q", "--disable-pip-version-check",
                                f"{pkg}=={ver}"], timeout=1800)
            if rc != 0:
                return f"pip install {pkg}=={ver} failed: {out[-300:]}"
            if pkg == "playwright":
                rc, out = host.run([*py, "-m", "playwright", "install", "chromium"], timeout=3600)
                if rc != 0:
                    return f"playwright install chromium failed: {out[-300:]}"
        return None

    def _smoke(self, host, py: list, up) -> str | None:
        mod = ["-m", "pip", "--version"] if up["package"] == "pip" else \
            ["-m", up["package"], "--version"]
        rc, out = host.run([*py, *mod], timeout=300)
        return None if rc == 0 and up["to"] in out else \
            f"{up['package']} --version reports {out.strip()[-120:]}"

    def pending(self, row, host):
        up = super().pending(row, host)
        if up:
            up.setdefault("package", row["key"])
        return up

    def test(self, up, host):
        venv = host.staging / "venv-test"
        shutil.rmtree(venv, ignore_errors=True)
        rc, out = host.run([host.win_python, "-m", "venv", str(venv)], timeout=600)
        if rc != 0:
            return f"could not create a test venv: {out[-200:]}"
        py = str(venv / ("Scripts/python.exe" if host.windows else "bin/python"))
        rc, out = host.run([py, "-m", "pip", "install", "-q", f"{up['package']}=={up['to']}"],
                           timeout=1800)
        if rc != 0:
            return f"pip install into the test venv failed: {out[-300:]}"
        return self._smoke(host, [py], up)

    def apply(self, up, host):
        return self._install(host, up["package"], up["to"])

    def verify(self, up, host):
        return self._smoke(host, [host.win_python], up) or host.suite_ok()

    def rollback(self, up, bk, host):
        return self._install(host, up["package"], up["from"])

    def cleanup(self, up, bk, host):
        shutil.rmtree(host.staging / "venv-test", ignore_errors=True)


class PlaywrightBrowser(Upgrader):
    via = "playwright-browser"
    label = "playwright install chromium (beside the old build)"
    SMOKE = ("from playwright.sync_api import sync_playwright\n"
             "with sync_playwright() as p:\n    p.chromium.launch().close()\nprint('ok')")

    def pending(self, row, host):
        if row.get("missing") or not host.sandbox_python:
            return None
        rc, out = host.wsl(f"{_sh(host.sandbox_python)} -m playwright install --dry-run chromium")
        want = re.search(r"chromium-(\d+)", out)
        have = _key(row.get("current") or "")
        if not want or (have and have[-1] == int(want.group(1))):
            return None
        return {"key": row["key"], "name": row.get("name", row["key"]),
                "from": row.get("current") or "none", "to": "build " + want.group(1),
                "via": self.via}

    def apply(self, up, host):
        rc, out = host.wsl(f"{_sh(host.sandbox_python)} -m playwright install chromium",
                           timeout=3600)
        return None if rc == 0 else f"playwright install chromium failed: {out[-300:]}"

    def verify(self, up, host):
        rc, out = host.wsl(f"{_sh(host.sandbox_python)} -c {_sh(self.SMOKE)}", timeout=300)
        return None if rc == 0 and "ok" in out else f"Chromium did not launch: {out[-300:]}"


class PythonOrg(Upgrader):
    via = "python-org"
    label = "python.org installer; staged from NuGet and run against the full suite"
    restart = True

    def test(self, up, host):
        z = host.download(f"https://www.nuget.org/api/v2/package/python/{up['to']}",
                          f"python-{up['to']}.nupkg")
        root = host.staging / f"python-{up['to']}"
        shutil.rmtree(root, ignore_errors=True)
        with zipfile.ZipFile(z) as f:
            f.extractall(root)
        venv = host.staging / "pyvenv-test"
        steps = [[str(root / "tools" / "python.exe"), "-m", "venv", "--clear", str(venv)],
                 [str(venv / "Scripts" / "python.exe"), "-m", "pip", "install", "-q",
                  "--require-hashes", "-r", str(host.repo / "requirements.lock")],
                 [str(venv / "Scripts" / "python.exe"), "-m", "pip", "install", "-q",
                  "-r", str(host.repo / "requirements-dev.txt")],
                 [str(venv / "Scripts" / "python.exe"), "-m", "pytest", "-q", "-p",
                  "no:cacheprovider", "-o", "addopts=", "--basetemp",
                  str(host.staging / "pytest-tmp"), "tests"]]
        for argv in steps:
            rc, out = host.run(argv, timeout=3600, cwd=str(host.repo),
                               env={"PYTHONPATH": str(host.repo / "src")})
            if rc != 0:
                return f"{Path(argv[0]).name} {' '.join(argv[1:4])}: {out.strip()[-300:]}"
        return None

    def apply(self, up, host):
        minor = _key(up["to"])[:2] != _key(up["from"])[:2]
        a = "amd64" if arch() == "x64" else "arm64"
        new = host.download(f"https://www.python.org/ftp/python/{up['to']}/"
                            f"python-{up['to']}-{a}.exe", f"python-{up['to']}-{a}.exe")
        old = host.download(f"https://www.python.org/ftp/python/{up['from']}/"
                            f"python-{up['from']}-{a}.exe", f"python-{up['from']}-{a}.exe")
        result = host.data_dir / "upgrade_result.json"
        result.unlink(missing_ok=True)
        script = host.data_dir / "python_upgrade.ps1"
        script.write_text(HELPER.format(
            task=_q(TASK), new=_q(new), old=_q(old), venv=_q(host.venv_dir),
            repo=_q(host.repo), result=_q(result), minor="$true" if minor else "$false",
            to=_q(up["to"]), key=_q(up["key"]), frm=_q(up["from"])), encoding="utf-8")
        rc, out = host.ps(f"Start-Process powershell.exe -WindowStyle Hidden -ArgumentList "
                          f"'-NoProfile -ExecutionPolicy Bypass -File \"{script}\"'", timeout=60)
        if rc != 0:
            return f"could not start the upgrade helper: {out[-200:]}"
        up["_deferred"] = str(result)
        return None


# Runs detached, because the interpreter cannot be replaced while SysPerf runs
# on it. Patch release: upgrade in place. New minor: install side by side,
# build a new venv and swap it in. Either way SysPerf must answer /api/health
# within 5 minutes or the change is reverted.
HELPER = r"""$ErrorActionPreference = 'Continue'
function Done($state, $reason) {{
  @{{ key = {key}; from = {frm}; to = {to}; state = $state; reason = $reason; at = [DateTimeOffset]::UtcNow.ToUnixTimeSeconds() }} |
    ConvertTo-Json | Set-Content -Encoding UTF8 {result}
}}
function Healthy {{
  for ($i = 0; $i -lt 60; $i++) {{
    Start-Sleep 5
    try {{ $h = Invoke-RestMethod http://127.0.0.1:5056/api/health -TimeoutSec 5; if ($h.status) {{ return $true }} }} catch {{}}
  }}
  return $false
}}
$venv = {venv}; $next = "$venv-next"; $prev = "$venv-prev"
Stop-ScheduledTask -TaskName {task}; Start-Sleep 8
Get-Process pythonw, python -ErrorAction SilentlyContinue | Where-Object {{ $_.Path -like "$venv*" }} | Stop-Process -Force
$p = Start-Process {new} -ArgumentList '/quiet InstallAllUsers=0 PrependPath=0 Include_launcher=0 Include_test=0 Shortcuts=0' -Wait -PassThru
if ($p.ExitCode -ne 0) {{ Start-ScheduledTask -TaskName {task}; Done 'rolled_back' "installer exit $($p.ExitCode)"; exit }}
if ({minor}) {{
  $py = (Get-ChildItem "$env:LOCALAPPDATA\Programs\Python" -Directory | Sort-Object Name | Select-Object -Last 1).FullName + '\python.exe'
  Remove-Item -Recurse -Force $next -ErrorAction SilentlyContinue
  & $py -m venv $next
  & "$next\Scripts\python.exe" -m pip install -q --require-hashes -r (Join-Path {repo} 'requirements.lock')
  & "$next\Scripts\python.exe" -m pip install -q --no-deps -e {repo}
  Remove-Item -Recurse -Force $prev -ErrorAction SilentlyContinue
  Rename-Item $venv $prev; Rename-Item $next $venv
}} else {{
  & "$venv\Scripts\python.exe" -m venv --upgrade $venv
}}
Start-ScheduledTask -TaskName {task}
if (Healthy) {{ Done 'applied' 'tested, applied and verified: SysPerf is healthy on the new interpreter'; exit }}
Stop-ScheduledTask -TaskName {task}; Start-Sleep 8
if ({minor}) {{
  Remove-Item -Recurse -Force $venv -ErrorAction SilentlyContinue; Rename-Item $prev $venv
}} else {{
  Start-Process {new} -ArgumentList '/uninstall /quiet' -Wait
  Start-Process {old} -ArgumentList '/quiet InstallAllUsers=0 PrependPath=0 Include_launcher=0 Include_test=0 Shortcuts=0' -Wait
  & "$venv\Scripts\python.exe" -m venv --upgrade $venv
}}
Start-ScheduledTask -TaskName {task}
Done 'rolled_back' 'SysPerf did not become healthy on the new interpreter; reverted'
"""


class Pwsh(Upgrader):
    via = "pwsh"
    label = "PowerShell MSIX; the release zip runs the real collector first"
    restart = True

    def _probe(self, host, exe: str) -> str | None:
        from .collector import _parse_probe
        script = Path(__file__).with_name("collect_win.ps1")
        rc, out = host.run([exe, "-NoProfile", "-ExecutionPolicy", "Bypass", "-File",
                            str(script)], timeout=300)
        try:
            _parse_probe(out)
        except ValueError:
            return f"the collector does not run under {exe}: {out.strip()[-200:]}"
        return None

    def _msix(self, host, ver):
        return host.download(f"https://github.com/PowerShell/PowerShell/releases/download/"
                             f"v{ver}/PowerShell-{ver}-win-{arch()}.msix",
                             f"PowerShell-{ver}.msix")

    def test(self, up, host):
        z = host.download(f"https://github.com/PowerShell/PowerShell/releases/download/"
                          f"v{up['to']}/PowerShell-{up['to']}-win-{arch()}.zip",
                          f"PowerShell-{up['to']}.zip")
        root = host.staging / f"pwsh-{up['to']}"
        shutil.rmtree(root, ignore_errors=True)
        with zipfile.ZipFile(z) as f:
            f.extractall(root)
        return self._probe(host, str(root / "pwsh.exe"))

    def apply(self, up, host):
        rc, out = host.ps(f"Add-AppxPackage -Path {_q(self._msix(host, up['to']))}")
        return None if rc == 0 else f"Add-AppxPackage failed: {out[-300:]}"

    def verify(self, up, host):
        rc, out = host.run(["pwsh.exe", "-NoProfile", "-Command",
                            "$PSVersionTable.PSVersion.ToString()"], timeout=120)
        if up["to"] not in out:
            return f"pwsh reports {out.strip()[:60]}"
        return self._probe(host, "pwsh.exe")

    def rollback(self, up, bk, host):
        rc, out = host.ps(f"Add-AppxPackage -Path {_q(self._msix(host, up['from']))} "
                          "-ForceUpdateFromAnyVersion")
        return None if rc == 0 else f"reinstalling PowerShell {up['from']} failed: {out[-200:]}"

    def cleanup(self, up, bk, host):
        shutil.rmtree(host.staging / f"pwsh-{up['to']}", ignore_errors=True)


# Every git operation self-evolution performs, in the order a deploy uses them.
GIT_ID = ["-c", "user.name=t", "-c", "user.email=t@x"]
GIT_SMOKE = (["init", "-q", "-b", "main", "r"],
             ["-C", "r", *GIT_ID, "commit", "-q", "--allow-empty", "-m", "a"],
             ["-C", "r", "worktree", "add", "-q", "--detach", "../w", "HEAD"],
             ["-C", "w", "checkout", "-q", "-b", "b"],
             ["-C", "w", *GIT_ID, "commit", "-q", "--allow-empty", "-m", "b"],
             ["-C", "r", "merge", "-q", "--ff-only", "b"],
             ["-C", "r", "tag", "-f", "t", "HEAD~1"],
             ["-C", "r", "reset", "-q", "--keep", "t"],
             ["-C", "r", "worktree", "remove", "--force", "../w"])


class Winget(Upgrader):
    via = "winget"
    label = "winget; the release's PortableGit runs every git operation first"

    def _smoke(self, host, git: str) -> str | None:
        work = host.staging / "git-smoke"
        shutil.rmtree(work, ignore_errors=True)
        work.mkdir(parents=True)
        for argv in GIT_SMOKE:
            rc, out = host.run([git, *argv], timeout=120, cwd=str(work))
            if rc != 0:
                return f"git {argv[2] if argv[0] == '-C' else argv[0]} failed: {out[-200:]}"
        return None

    def test(self, up, host):
        url, tag = host.github_asset("git-for-windows/git", r"PortableGit-[\d.]+-64-bit\.7z\.exe")
        if up["to"] not in tag:
            return f"the newest Git for Windows release is {tag}, not {up['to']}"
        exe = host.download(url, "PortableGit.7z.exe")
        root = host.staging / "PortableGit"
        shutil.rmtree(root, ignore_errors=True)
        rc, out = host.run([str(exe), f"-o{root}", "-y"], timeout=1200)
        if rc != 0:
            return f"PortableGit did not unpack: {out[-200:]}"
        return self._smoke(host, str(root / "cmd" / "git.exe"))

    def backup(self, up, host):
        rc, out = host.run(["winget", "list", "--id", up["id"], "--exact",
                            "--accept-source-agreements", "--disable-interactivity"], timeout=300)
        m = re.search(re.escape(up["id"]) + r"\s+(\d+(?:\.\d+)+)", out)
        return {"version": m.group(1) if m else ""}

    def apply(self, up, host):
        rc, out = host.run(["winget", "upgrade", "--id", up["id"], "--exact", "--silent",
                            "--accept-package-agreements", "--accept-source-agreements",
                            "--disable-interactivity"], timeout=3600)
        return None if rc == 0 else f"winget upgrade failed (rc {rc}): {out[-300:]}"

    def verify(self, up, host):
        rc, out = host.run(["git", "--version"], timeout=60)
        return (None if up["to"] in out else f"git reports {out.strip()[:60]}") or \
            self._smoke(host, "git")

    def rollback(self, up, bk, host):
        if not bk.get("version"):
            return "the previous version is unknown"
        rc, out = host.run(["winget", "install", "--id", up["id"], "--exact", "--version",
                            bk["version"], "--force", "--silent", "--accept-package-agreements",
                            "--accept-source-agreements", "--disable-interactivity"],
                           timeout=3600)
        return None if rc == 0 else f"reinstalling {bk['version']} failed: {out[-200:]}"

    def cleanup(self, up, bk, host):
        shutil.rmtree(host.staging / "PortableGit", ignore_errors=True)
        shutil.rmtree(host.staging / "git-smoke", ignore_errors=True)


class Wsl(Upgrader):
    via = "wsl"
    label = "signed WSL MSI (UAC); the sandbox suite must pass afterwards"

    def _msi(self, host, ver) -> Path:
        url, _ = host.github_asset("microsoft/WSL", rf"wsl\.{re.escape(ver)}(\.0)?\.{arch()}\.msi",
                                   tag=ver)
        p = host.download(url, f"wsl-{ver}.msi")
        rc, out = host.ps(f"$s = Get-AuthenticodeSignature {_q(p)}; "
                          "if ($s.Status -eq 'Valid' -and $s.SignerCertificate.Subject -match "
                          "'Microsoft Corporation') { 'signed' } else { $s.Status }")
        if "signed" not in out:
            raise ValueError(f"{p.name} is not validly signed by Microsoft ({out.strip()[:60]})")
        return p

    def test(self, up, host):
        up["_msi"] = str(self._msi(host, up["to"]))
        return None

    def apply(self, up, host):
        rc, out = host.elevated("msiexec.exe", f'/i "{up["_msi"]}" /quiet /norestart')
        if rc == 1223:
            return "the administrator (UAC) prompt was declined or not answered"
        return None if rc in (0, 3010) else f"msiexec exit {rc}: {out[-200:]}"

    def verify(self, up, host):
        rc, out = host.run(["wsl.exe", "--version"], timeout=120)
        return (None if up["to"] in out.replace("\x00", "") else "wsl --version does not "
                f"report {up['to']}") or host.suite_ok()

    def rollback(self, up, bk, host):
        old = self._msi(host, up["from"])
        host.elevated("msiexec.exe", f'/x "{up["_msi"]}" /quiet /norestart')
        rc, out = host.elevated("msiexec.exe", f'/i "{old}" /quiet /norestart')
        return None if rc in (0, 3010) else f"reinstalling WSL {up['from']} failed (rc {rc})"


# ================================================================ hand-off
class WindowsUpdate(Upgrader):
    via = "windows-update"
    label = "Windows Update scan (drivers arrive as optional updates)"
    handoff = True
    retry_s = 30 * 86400

    def pending(self, row, host):
        if row.get("missing") or not row.get("outdated"):
            return None
        return {"key": row["key"], "name": row.get("name", row["key"]),
                "from": row.get("current") or "", "to": row.get("latest") or "",
                "via": self.via}

    def apply(self, up, host):
        rc, out = host.run(["UsoClient.exe", "StartInteractiveScan"], timeout=120)
        return None if rc == 0 else f"UsoClient exit {rc}: {out[-200:]}"


class VendorUpdate(WindowsUpdate):
    via = "vendor-update"
    label = "Microsoft Edge Update"

    def apply(self, up, host):
        rc, out = host.ps(
            "$t = Get-ScheduledTask -TaskName 'MicrosoftEdgeUpdateTaskMachineUA*' "
            "-ErrorAction SilentlyContinue | Select-Object -First 1; "
            "if ($t) { Start-ScheduledTask -InputObject $t; 'started' } else { "
            "$e = \"${env:ProgramFiles(x86)}\\Microsoft\\EdgeUpdate\\MicrosoftEdgeUpdate.exe\"; "
            "Start-Process $e -ArgumentList '/ua /installsource scheduler'; 'started' }")
        return None if rc == 0 else f"Edge Update did not start: {out[-200:]}"


class Follows(Upgrader):
    """Ships inside another component; upgraded with it, never on its own."""
    via = "follows"
    label = "upgraded with another component"
    windows_only = False

    def pending(self, row, host):
        return None


REGISTRY = {u.via: u() for u in (PipLock, PipDev, PipTool, PlaywrightBrowser, Apt,
                                  UbuntuRelease, Claude, PythonOrg, Pwsh, Winget, Wsl,
                                  WindowsUpdate, VendorUpdate, Follows)}


def title(up: dict) -> str:
    """The attempt's identity: one try per (component, from, to)."""
    return f"3PP upgrade: {up['key']} {up['from']} → {up['to']}"
