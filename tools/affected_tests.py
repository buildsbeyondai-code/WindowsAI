"""Run only the tests a change can affect (the inner edit loop).

The full suite (~1,300 tests, three platforms, eight gates) is for the release
boundary: the pre-push hook (tools/hooks/pre-push) and the Store checklist run
it. While editing, this picks the tests that can see the change:

* a changed test file runs itself;
* a changed ``src/sysperf/<mod>.py`` runs every test that imports ``<mod>``,
  every test named ``test_<mod>*.py``, and every test whose source names the
  file or the module (docs and coverage checks reference modules by name);
* a changed non-Python file (``index.html``, ``*.ps1``, ``*.json``, ``*.md``,
  ``pyproject.toml``) runs every test whose source names it; ``index.html``
  also runs the UI and accessibility tests;
* a change to ``conftest.py``, fixtures or the test runner selects everything.

Usage (from the repo root, with the project's Python):

    python tools/affected_tests.py               # uncommitted changes vs HEAD
    python tools/affected_tests.py --base main   # everything since main
    python tools/affected_tests.py --list        # print the selection only
    python tools/affected_tests.py -- -x -k foo  # extra pytest arguments

Exit code is pytest's, or 0 when nothing selectable changed.
"""
from __future__ import annotations

import argparse
import ast
import os
import shutil
import subprocess
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
TESTS = ROOT / "tests"
PKG = ROOT / "src" / "sysperf"
EVERYTHING = {"tests/conftest.py", "tests/covrun.py", "tests/testrunner.py",
              "tests/uicov.py", "pyproject.toml"}
UI_TESTS = ("test_e2e_", "test_accessibility")


def git(*args: str) -> str:
    # From WSL, Linux git sees every file of this Windows checkout as modified
    # (CRLF); Git for Windows does not.
    win = Path("/mnt/c/Program Files/Git/cmd/git.exe")
    exe = str(win) if str(ROOT).startswith("/mnt/") and win.exists() else (
        shutil.which("git") or r"C:\Program Files\Git\cmd\git.exe")
    root = subprocess.run(["wslpath", "-w", str(ROOT)], capture_output=True, text=True,
                          check=False).stdout.strip() if exe == str(win) else str(ROOT)
    out = subprocess.run([exe, "-C", root, *args], capture_output=True, text=True,
                         check=False)
    return out.stdout


def changed(base: str | None) -> list[str]:
    names = set(git("diff", "-z", "--name-only", base or "HEAD").split("\0"))
    names |= set(git("ls-files", "-z", "--others", "--exclude-standard").split("\0"))
    return sorted(n.replace("\\", "/") for n in names if n)


def imported_modules(src: str) -> set[str]:
    """sysperf modules a test imports, by any import form."""
    mods = set()
    try:
        tree = ast.parse(src)
    except SyntaxError:
        return mods
    for node in ast.walk(tree):
        if isinstance(node, ast.Import):
            for a in node.names:
                if a.name.startswith("sysperf."):
                    mods.add(a.name.split(".")[1])
        elif isinstance(node, ast.ImportFrom) and node.module:
            if node.module == "sysperf":
                mods |= {a.name for a in node.names}
            elif node.module.startswith("sysperf."):
                mods.add(node.module.split(".")[1])
    return mods


def select(files: list[str]) -> tuple[list[str], bool]:
    """(test files to run, whether the whole suite is needed)."""
    tests = {p.name: p.read_text(encoding="utf-8", errors="replace")
             for p in sorted(TESTS.glob("test_*.py"))}
    imports = {name: imported_modules(src) for name, src in tests.items()}
    picked: set[str] = set()
    for f in files:
        p = Path(f)
        if f in EVERYTHING or f.startswith("tests/fixtures/"):
            return sorted(tests), True
        if f.startswith("tests/") and p.name in tests:
            picked.add(p.name)
            continue
        if f.startswith("src/sysperf/") and p.suffix == ".py":
            mod = p.stem
            for name, src in tests.items():
                if (mod in imports[name] or name.startswith(f"test_{mod}")
                        or p.name in src or f"sysperf.{mod}" in src):
                    picked.add(name)
            continue
        if p.name == "index.html":
            picked |= {n for n in tests if n.startswith(UI_TESTS)}
        for name, src in tests.items():         # anything that names the file
            if p.name in src:
                picked.add(name)
    return sorted(picked), False


def main(argv: list[str]) -> int:
    ap = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    ap.add_argument("--base", help="compare against this ref instead of HEAD")
    ap.add_argument("--list", action="store_true", help="print the selection, run nothing")
    ap.add_argument("pytest_args", nargs="*", help="passed through to pytest (after --)")
    a = ap.parse_args(argv)
    files = changed(a.base)
    picked, whole = select(files)
    print(f"changed: {', '.join(files) or 'nothing'}")
    print(f"selected {'ALL' if whole else len(picked)} test file(s)"
          + ("" if whole else f": {' '.join(picked)}"))
    if a.list or not picked:
        return 0
    py_changed = [f for f in files if f.endswith(".py") and (ROOT / f).exists()]
    if py_changed:
        rc = subprocess.run([sys.executable, "-m", "ruff", "check", *py_changed],
                            cwd=ROOT, check=False).returncode
        if rc:
            return rc
    targets = ["tests"] if whole else [f"tests/{n}" for n in picked]
    extra = list(a.pytest_args)
    if os.name == "nt" and not any(x.startswith("--basetemp") for x in extra):
        # %TEMP%\pytest-of-<user> can carry an ACL the user cannot open.
        extra.append(f"--basetemp={Path(os.environ['LOCALAPPDATA']) / 'SysPerf' / 'pt-tmp'}")
    return subprocess.run([sys.executable, "-m", "pytest", "-p", "no:cacheprovider",
                           *targets, *extra], cwd=ROOT, check=False).returncode


if __name__ == "__main__":
    sys.exit(main(sys.argv[1:]))
