"""
Third-party (3PP) package upgrades for self-evolution (5.1).

The Components view learns from each vendor's live feed when a package SysPerf
pins has a newer release. This module turns that into an upgrade the evolution
cycle tests and deploys like any other candidate:

* Only SysPerf's own hash-locked runtime packages (requirements.lock) are
  upgraded, and only to a release that still supports Python 3.10 (the CI
  floor), needs no package the lock does not already hold, and ships a
  pure-Python wheel. A pure wheel can be unpacked into the worktree for the
  sandboxed tests, which have no network, and replaced under a running service.
* The pin moves in requirements.txt, requirements.lock (with the release's
  published sha256 hashes) and pyproject.toml together, so the offline lock
  test still holds and a fresh install gets the same versions.
* Deploying runs ``pip install --require-hashes -r requirements.lock``; rolling
  back resets git and runs it again, which reinstalls the previous versions.

Everything else that is outdated (the Python interpreter, Ubuntu, Git, WSL,
drivers) belongs to the PC rather than to SysPerf. Those rows keep their
upgrade path in the Components view and are left to their owner.
"""
from __future__ import annotations

import hashlib
import io
import json
import re
import sys
import urllib.request
import zipfile
from pathlib import Path, PurePosixPath

from .background_process import run_hidden

PIN = re.compile(r"^([A-Za-z0-9_.-]+)==([^\s;\\]+)", re.M)
PIN_FILES = ("requirements.txt", "requirements.lock", "pyproject.toml")
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


def title(up: dict) -> str:
    return TITLE.format(**up)


def outdated(repo: Path, latest: dict) -> list:
    """Locked packages whose live-feed release is newer, in lock order."""
    out = []
    for name, cur in locked(repo).items():
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


def pure_wheel(rel: dict) -> dict | None:
    return next((f for f in rel["files"] if f["packagetype"] == "bdist_wheel"
                 and f["filename"].endswith("-py3-none-any.whl")), None)


def unsuitable(rel: dict, lock: dict) -> str | None:
    """Why this release cannot be upgraded to automatically (None = it can)."""
    if not rel["files"]:
        return f"{rel['name']} {rel['version']} has no published files"
    if not _floor_ok(rel["requires_python"]):
        return (f"{rel['name']} {rel['version']} requires Python {rel['requires_python']}, "
                "and SysPerf still supports 3.10")
    if pure_wheel(rel) is None:
        return (f"{rel['name']} {rel['version']} has no pure-Python wheel; upgrade it by "
                "regenerating requirements.lock")
    for req in rel["requires_dist"]:
        if "extra ==" in req:
            continue
        dep = norm(re.match(r"\s*([A-Za-z0-9_.-]+)", req).group(1))
        if dep not in lock:
            return (f"{rel['name']} {rel['version']} needs {dep}, which requirements.lock "
                    "does not hold")
    return None


def fetch_wheel(rel: dict, get) -> bytes:
    f = pure_wheel(rel)
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


def pip_sync(repo: Path, run=run_hidden, timeout: float = 600) -> str | None:
    """Install exactly what requirements.lock pins; the error text, or None."""
    try:
        r = run([_python(), "-m", "pip", "install", "--disable-pip-version-check",
                 "--no-input", "--no-deps", "--require-hashes", "-r",
                 str(Path(repo) / "requirements.lock")],
                capture_output=True, text=True, timeout=timeout)
    except Exception as exc:
        return f"{type(exc).__name__}: {exc}"
    if r.returncode != 0:
        return ((r.stderr or "") + (r.stdout or "")).strip()[-400:] or f"pip exited {r.returncode}"
    return None


def overview(repo: Path | None, latest: dict, candidates: list) -> list:
    """Each locked package with its live latest and its upgrade attempt, if any."""
    if not repo:
        return []
    by_title = {}
    for c in candidates:                  # newest first: keep the latest attempt
        by_title.setdefault(c.get("title"), c)
    out = []
    for name, cur in locked(repo).items():
        new = latest.get(name) or ""
        row = {"name": name, "current": cur, "latest": new or None,
               "outdated": bool(new) and _key(new) > _key(cur)}
        c = by_title.get(title({"name": name, "current": cur, "latest": new})) if new else None
        if c:
            row.update(candidate=c["id"], decision=c.get("decision"),
                       review_state=c.get("review_state"), reason=c.get("reason"))
        out.append(row)
    return out
