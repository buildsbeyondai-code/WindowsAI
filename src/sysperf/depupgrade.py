"""
Third-party (3PP) package upgrades for self-evolution (5.1).

The Components view learns from each vendor's live feed when a package SysPerf
pins has a newer release. This module turns that into an upgrade the evolution
cycle tests and deploys like any other candidate:

* SysPerf's pinned packages: the hash-locked runtime (requirements.lock) and
  the test tools pinned in requirements-dev.txt (5.1.1). A release qualifies
  when it still supports Python 3.10 (the CI floor) and ships a wheel for both
  sides: one the sandbox's Linux interpreter can import (pure, or a manylinux
  build for its version) and one Windows can install without compiling.
* Dependencies come with it (5.4.4). The lock is regenerated with uv exactly as
  CI checks it (``relock``), so a dependency the new release added, or a newer
  one it needs, enters the lock with its hashes; deploying installs it. The
  sandbox gets every wheel of the resolved set for its Linux Python
  (``closure``), unpacked into the worktree, because the sandboxed tests have
  no network. Without uv, a release that needs a package the lock does not
  hold is refused, as before.
* The pin moves in every file that holds it (requirements.txt, the lock with
  the release's published sha256 hashes, requirements-dev.txt, pyproject.toml),
  so the offline lock test still holds and a fresh install gets the same set.
* Deploying installs the pins into SysPerf's venv and into the WSL sandbox
  venv, so later sandbox runs test what is deployed; rolling back resets git
  and installs again, which restores the previous versions.

Software outside SysPerf's own pins (the interpreter, Ubuntu, Git, WSL,
drivers) is upgraded through ``upgrader``.
"""
from __future__ import annotations

import hashlib
import io
import json
import os
import re
import shutil
import sys
import urllib.request
import zipfile
from pathlib import Path, PurePosixPath

from .background_process import run_hidden

PIN = re.compile(r"^([A-Za-z0-9_.-]+)==([^\s;\\]+)", re.M)
# CI's "Lock file matches requirements.txt" check, so a relocked file passes it.
LOCK_ARGS = ["pip", "compile", "--universal", "--generate-hashes", "--python-version", "3.10",
             "--no-header", "--quiet", "requirements.txt", "-o"]
SANDBOX_PLATFORM = "x86_64-unknown-linux-gnu"
PIN_FILES = ("requirements.txt", "requirements.lock", "requirements-dev.txt", "pyproject.toml")
DEV_PIN = re.compile(r"^([A-Za-z0-9_.-]+)==([^\s;\\]+)\s*$", re.M)   # markers: not ours to move
PYTHON_FLOOR = (3, 10)
TITLE = "3PP upgrade: {name} {current} → {latest}"


def norm(name: str) -> str:
    return re.sub(r"[-_.]+", "-", name).lower()


def _key(v: str) -> tuple:
    return tuple(int(x) for x in re.findall(r"\d+", v or ""))


def http_get_bytes(url: str, timeout: float = 30.0) -> bytes:   # pragma: no cover - network
    req = urllib.request.Request(url, headers={"User-Agent": "SysPerf-3pp-upgrade"})
    with urllib.request.urlopen(req, timeout=timeout) as r:
        return r.read()


def locked(repo: Path) -> dict:
    """{normalized name: version} for every package in requirements.lock."""
    try:
        text = (Path(repo) / "requirements.lock").read_text(encoding="utf-8")
    except OSError:
        return {}
    return {norm(n): v for n, v in PIN.findall(text)}


def pinned(repo: Path) -> dict:
    """Every package SysPerf pins: the lock, then requirements-dev.txt."""
    out = dict(locked(repo))
    try:
        text = (Path(repo) / "requirements-dev.txt").read_text(encoding="utf-8")
    except OSError:
        return out
    for n, v in DEV_PIN.findall(text):
        out.setdefault(norm(n), v)
    return out


def title(up: dict) -> str:
    return TITLE.format(**up)


def outdated(repo: Path, latest: dict) -> list:
    """Pinned packages whose live-feed release is newer, in pin order."""
    out = []
    for name, cur in pinned(repo).items():
        new = latest.get(name) or ""
        if new and _key(new) > _key(cur):
            out.append({"name": name, "current": cur, "latest": new})
    return out


def release(name: str, version: str, get) -> dict:
    """PyPI metadata of one release: files with hashes, Python and dependency needs."""
    data = json.loads(get(f"https://pypi.org/pypi/{name}/{version}/json"))
    info = data.get("info") or {}
    files = [{"filename": f["filename"], "url": f["url"], "sha256": f["digests"]["sha256"],
              "packagetype": f.get("packagetype")} for f in data.get("urls") or []
             if not f.get("yanked")]
    return {"name": name, "version": version, "files": files,
            "requires_python": info.get("requires_python") or "",
            "requires_dist": info.get("requires_dist") or []}


def _floor_ok(spec: str) -> bool:
    """False when ``requires_python`` excludes the CI floor (e.g. '>=3.11')."""
    for op, ver in re.findall(r"(>=|>|==|~=)\s*(\d+(?:\.\d+)*)", spec or ""):
        want = _key(ver)[:2]
        if op in (">=", "~=") and want > PYTHON_FLOOR:
            return False
        if op == ">" and want >= PYTHON_FLOOR:
            return False
        if op == "==" and want[:2] != PYTHON_FLOOR:
            return False
    return True


def _wheels(rel: dict) -> list:
    return [f for f in rel["files"] if f["packagetype"] == "bdist_wheel"]


def test_wheel(rel: dict, pyver: str = "312") -> dict | None:
    """The wheel the sandbox's Linux CPython ``pyver`` (e.g. "312") can import."""
    ws = _wheels(rel)
    pure = next((f for f in ws if f["filename"].endswith("-py3-none-any.whl")), None)
    if pure:
        return pure
    x86 = [f for f in ws if re.search(r"manylinux[^-]*x86_64\.whl$", f["filename"])]
    exact = next((f for f in x86 if f"-cp{pyver}-cp{pyver}-" in f["filename"]), None)
    if exact:
        return exact
    for f in x86:                                       # cp38-abi3 runs on any newer 3.x
        m = re.search(r"-cp3(\d+)-abi3-", f["filename"])
        if m and int(m.group(1)) <= int(pyver[1:]):
            return f
    return None


def _windows_ok(rel: dict) -> bool:
    return any(f["filename"].endswith(("-none-any.whl", "win_amd64.whl", "win_arm64.whl"))
               for f in _wheels(rel))


def uv_cmd(find=None) -> list | None:
    """``[python, -m, uv]`` when uv is installed next to SysPerf, else None."""
    import importlib.util
    find = find or importlib.util.find_spec
    return [_python(), "-m", "uv"] if find("uv") else None


def relock(wt: Path, uv: list, run=run_hidden, timeout: float = 600) -> str | None:
    """Regenerate requirements.lock from requirements.txt; the error text, or None.

    The same command CI diffs the lock against, so the result passes that check
    and carries every dependency the pinned releases need, with hashes.
    """
    lock = Path(wt) / "requirements.lock"
    old = lock.read_text(encoding="utf-8") if lock.is_file() else ""
    header = "".join(ln for ln in old.splitlines(True)[:20] if ln.startswith("#"))
    fresh = Path(wt) / "requirements.lock.new"
    try:
        r = run([*uv, *LOCK_ARGS, str(fresh)], cwd=str(wt), capture_output=True, text=True,
                timeout=timeout)
    except Exception as exc:
        return f"uv: {type(exc).__name__}: {exc}"
    if r.returncode != 0 or not fresh.is_file():
        return f"uv could not resolve the dependencies: {(r.stderr or r.stdout).strip()[-400:]}"
    lock.write_text(header + fresh.read_text(encoding="utf-8"), encoding="utf-8")
    fresh.unlink()
    return None


def closure(wt: Path, uv: list, extra: str = "", pyver: str = "312", run=run_hidden,
            timeout: float = 600) -> dict | str:
    """{name: version} the sandbox's Linux Python needs; the error text on failure.

    requirements.txt, plus ``extra`` (a dev tool's new pin), resolved for the
    sandbox's platform, so a dependency only Windows needs (colorama) is not
    fetched and one Linux needs is.
    """
    src = Path(wt) / "requirements.sandbox.in"
    req = (Path(wt) / "requirements.txt").read_text(encoding="utf-8")
    src.write_text(req + ("\n" + extra + "\n" if extra else ""), encoding="utf-8")
    try:
        r = run([*uv, "pip", "compile", "--python-version", f"{pyver[0]}.{pyver[1:]}",
                 "--python-platform", SANDBOX_PLATFORM, "--no-header", "--quiet",
                 "--no-annotate", str(src)], cwd=str(wt), capture_output=True, text=True,
                timeout=timeout)
    except Exception as exc:
        return f"uv: {type(exc).__name__}: {exc}"
    finally:
        src.unlink(missing_ok=True)
    if r.returncode != 0:
        return f"uv could not resolve the dependencies: {(r.stderr or r.stdout).strip()[-400:]}"
    return {norm(n): v for n, v in PIN.findall(r.stdout)}


def unsuitable(rel: dict, lock: dict, pyver: str = "312", resolver: bool = False) -> str | None:
    """Why this release cannot be upgraded to automatically (None = it can)."""
    if not rel["files"]:
        return f"{rel['name']} {rel['version']} has no published files"
    if not _floor_ok(rel["requires_python"]):
        return (f"{rel['name']} {rel['version']} requires Python {rel['requires_python']}, "
                "and SysPerf still supports 3.10")
    if test_wheel(rel, pyver) is None:
        return (f"{rel['name']} {rel['version']} has no wheel the sandbox's Python "
                f"{pyver[0]}.{pyver[1:]} can import")
    if not _windows_ok(rel):
        return f"{rel['name']} {rel['version']} has no Windows wheel; pip would have to compile it"
    # A hash-locked package needs every dependency hash-locked too (pip's
    # --require-hashes refuses anything else). A dev tool (pytest) is not in the
    # lock: pip resolves its dependencies, so this check refused every one of
    # them (5.4.3 live: "pytest 9.2.0 needs colorama").
    # With a resolver the lock is regenerated, dependencies and all.
    for req in rel["requires_dist"] if norm(rel["name"]) in lock and not resolver else []:
        if "extra ==" in req:
            continue
        dep = norm(re.match(r"\s*([A-Za-z0-9_.-]+)", req).group(1))
        if dep not in lock:
            return (f"{rel['name']} {rel['version']} needs {dep}, which requirements.lock "
                    "does not hold")
    return None


def fetch_wheel(rel: dict, get, pyver: str = "312") -> bytes:
    f = test_wheel(rel, pyver)
    data = get(f["url"])
    if hashlib.sha256(data).hexdigest() != f["sha256"]:
        raise ValueError(f"{f['filename']}: sha256 does not match PyPI's")
    return data


def unpack_wheel(data: bytes, dest: Path) -> None:
    """Extract a wheel; refuses any member that would land outside ``dest``."""
    with zipfile.ZipFile(io.BytesIO(data)) as z:
        for n in z.namelist():
            p = PurePosixPath(n)
            if p.is_absolute() or ".." in p.parts or ":" in n:
                raise ValueError(f"unsafe path in wheel: {n}")
        z.extractall(dest)


def _name_re(name: str) -> str:
    """A package name in any spelling pip accepts (Flask, MarkupSafe, zope_interface)."""
    return "[-_.]".join(map(re.escape, re.split(r"[-_.]", name)))


def _sub_pin(text: str, name: str, old: str, new: str) -> str:
    pat = re.compile(r"(?<![A-Za-z0-9_.-])(" + _name_re(name) + r")=="
                     + re.escape(old) + r"(?![0-9A-Za-z.])", re.I)
    return pat.sub(lambda m: f"{m.group(1)}=={new}", text)


def rewrite(text: str, up: dict, hashes: list) -> str:
    """Move one pin to its new version; in the lock also swap its hash lines."""
    crlf = "\r\n" in text
    t = text.replace("\r\n", "\n")
    t = _sub_pin(t, up["name"], up["current"], up["latest"])
    head = re.compile(r"(?im)^(" + _name_re(up["name"]) + "==" + re.escape(up["latest"])
                      + r" \\\n)((?:[ \t]+--hash=sha256:[0-9a-f]{64}(?: \\)?\n)+)")
    lines = "".join(f"    --hash=sha256:{h}" + (" \\\n" if i < len(hashes) - 1 else "\n")
                    for i, h in enumerate(sorted(hashes)))
    t = head.sub(lambda m: m.group(1) + lines, t, count=1)
    return t.replace("\n", "\r\n") if crlf else t


def apply_pin(wt: Path, up: dict, rel: dict) -> list:
    """Rewrite every pin file in the worktree; returns the files that changed."""
    hashes = [f["sha256"] for f in rel["files"]]
    changed = []
    for rel_path in PIN_FILES:
        p = Path(wt) / rel_path
        if not p.is_file():
            continue
        old = p.read_bytes().decode("utf-8")
        new = rewrite(old, up, hashes)
        if new != old:
            p.write_bytes(new.encode("utf-8"))
            changed.append(rel_path)
    return changed


def _python() -> str:
    """The console interpreter next to ``sys.executable`` (pip under pythonw is silent)."""
    exe = Path(sys.executable)
    if exe.name.lower() == "pythonw.exe" and (exe.parent / "python.exe").is_file():
        return str(exe.parent / "python.exe")
    return sys.executable


SANDBOX_PY = "~/.sysperf-sandbox-venv/bin/python"


def _wsl_path(p) -> str:
    p = str(p).replace("\\", "/")
    return f"/mnt/{p[0].lower()}{p[2:]}" if len(p) > 1 and p[1] == ":" else p


def pip_sync(repo: Path, run=run_hidden, timeout: float = 600, dev: bool = False,
             wsl: bool | None = None) -> str | None:
    """Install exactly what SysPerf pins; the error text, or None.

    Into SysPerf's own venv and, on Windows, into the WSL sandbox venv too, so
    the next sandboxed test run exercises the versions that are deployed.
    """
    if wsl is None:
        wsl = os.name == "nt" and shutil.which("wsl.exe") is not None
    targets = [([_python()], lambda p: str(p))]
    if wsl:
        # `wsl.exe --` runs through the login shell, which expands the ~.
        targets.append((["wsl.exe", "--", SANDBOX_PY], _wsl_path))
    for py, path in targets:
        steps = [["--no-deps", "--require-hashes", "-r", path(Path(repo) / "requirements.lock")]]
        if dev:
            steps.append(["-r", path(Path(repo) / "requirements-dev.txt")])
        for args in steps:
            try:
                r = run([*py, "-m", "pip", "install", "-q", "--disable-pip-version-check",
                         "--no-input", *args], capture_output=True, text=True, timeout=timeout)
            except Exception as exc:
                return f"{type(exc).__name__}: {exc}"
            if r.returncode != 0:
                return (((r.stderr or "") + (r.stdout or "")).strip()[-400:]
                        or f"pip exited {r.returncode}")
    return None
