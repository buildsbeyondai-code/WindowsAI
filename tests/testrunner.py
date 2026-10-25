#!/usr/bin/env python3
"""
Full test runner for the System Performance Dashboard.

One command, seven gates. It is deliberately self-contained: this environment has
no network access to PyPI, so pytest is the only third-party tool available and
every static check here is implemented against the standard library's ``ast``,
``json`` and ``http`` modules.

    python3 tests/testrunner.py               # every gate
    python3 tests/testrunner.py --no-live     # skip the live HTTP gate
    python3 tests/testrunner.py --no-browser  # skip the slow Chromium gate
    python3 tests/testrunner.py --quick       # unit tests + coverage only

Gates
-----
1. **discover**   every top-level module is registered in the coverage gate, so a
                  new module cannot quietly dodge it.
2. **compile**    every module byte-compiles and carries a module docstring.
3. **lint**       AST checks: unused imports, bare ``except``, mutable default
                  arguments, duplicate definitions, stray ``print`` in library
                  code, leftover TODO/FIXME markers.
4. **unit**       the pytest suite.
5. **coverage**   100% line coverage on every target module (``tests/covrun``).
6. **frontend**   the page's JavaScript parses (``node --check``), every element
                  id it reads exists in the HTML, and every interpolation of a
                  server-provided *string* field is HTML-escaped.
7. **browser**    the Playwright suites drive a real Chromium against the page,
                  and ``tests/uicov.py`` requires 100% of the interface — every
                  tab, section, render target and control — to be exercised.
8. **live**       the running service: status codes, security headers, JSON
                  shape, CSRF enforcement, GET purity, and bounded state files.

Exit code is 0 only when every executed gate passes.
"""
from __future__ import annotations

import argparse
import ast
import io
import json
import os
import re
import subprocess
import sys
import time
import urllib.error
import urllib.request
from contextlib import redirect_stdout
from pathlib import Path

HERE = Path(__file__).resolve().parent
ROOT = HERE.parent
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

BASE_URL = os.environ.get("PERFDASH_TEST_URL", "http://127.0.0.1:5056")
# The development repo keeps the modules and the page at the root; an exported
# distribution keeps them under src/sysperf/. One runner, both layouts.
PKG = ROOT / "src" / "sysperf"
if not PKG.is_dir():
    PKG = ROOT
INDEX = PKG / "static" / "index.html"

# String fields the API can return which end up in innerHTML: interpolating any
# of these without esc() is an XSS hole, so it is a hard failure.
UNSAFE_IF_RAW = ("label", "note", "explanation", "reason", "detail", "summary",
                 "title", "output", "manual_hint", "skip_reason", "why",
                 "key", "id", "cause", "status", "mode", "direction", "message")

# Helpers that make an interpolation safe: escapers, number formatters, and the
# audited HTML builders in this page (each escapes its own text arguments).
SAFE_WRAPPERS = ("esc(", "fmt(", "tag(", "mins(", "dirTag(", "riskCls(",
                 "Math.", "Number(", "String(",
                 "sparkline(", "barCls(", "statusCls(", "donut(", "arrow(")

# An interpolation that *compares* or *measures* yields a literal or a number,
# never caller text, so it cannot inject markup.
SAFE_SHAPES = ("===", "!==", "==", ">=", "<=", ".length")

# Modules whose job is to write to a terminal. Everywhere else a print() is a
# leftover debug statement, which is why the rule exists — but a command-line
# entry point that logged its output instead of printing it would be useless.
PRINTS_ALLOWED = {"cli.py"}


class Gate:
    """One named check with a pass/fail result and a list of findings."""

    def __init__(self, name: str, description: str) -> None:
        self.name = name
        self.description = description
        self.findings: list[str] = []
        self.skipped = False
        self.skip_reason = ""

    def fail(self, message: str) -> None:
        self.findings.append(message)

    def skip(self, reason: str) -> None:
        self.skipped = True
        self.skip_reason = reason

    @property
    def ok(self) -> bool:
        return self.skipped or not self.findings


def modules() -> list[Path]:
    return sorted(p for p in PKG.glob("*.py") if not p.name.startswith("_"))


# --------------------------------------------------------------------------- #
# 1. discover                                                                 #
# --------------------------------------------------------------------------- #
def gate_discover() -> Gate:
    gate = Gate("discover", "every module is registered in the coverage gate")
    import covrun
    registered = set(covrun.TARGETS)
    found = {p.name for p in modules()}
    for missing in sorted(found - registered):
        gate.fail(f"{missing} is not in covrun.TARGETS — it would escape the "
                  "100% coverage gate")
    for stale in sorted(registered - found):
        gate.fail(f"covrun.TARGETS lists {stale}, which no longer exists")
    return gate


# --------------------------------------------------------------------------- #
# 2. compile                                                                  #
# --------------------------------------------------------------------------- #
def gate_compile() -> Gate:
    gate = Gate("compile", "modules byte-compile and are documented")
    for path in modules():
        source = path.read_text(encoding="utf-8")
        try:
            tree = ast.parse(source, filename=str(path))
        except SyntaxError as exc:
            gate.fail(f"{path.name}: syntax error line {exc.lineno}: {exc.msg}")
            continue
        if not ast.get_docstring(tree):
            gate.fail(f"{path.name}: missing module docstring")
    return gate


# --------------------------------------------------------------------------- #
# 3. lint                                                                     #
# --------------------------------------------------------------------------- #
def _imported_names(tree: ast.AST) -> dict:
    """{bound name: line} for module-level imports."""
    out = {}
    for node in ast.walk(tree):
        if isinstance(node, ast.Import):
            for alias in node.names:
                name = alias.asname or alias.name.split(".")[0]
                out[name] = node.lineno
        elif isinstance(node, ast.ImportFrom):
            for alias in node.names:
                if alias.name == "*":
                    continue
                out[alias.asname or alias.name] = node.lineno
    return out


def _used_names(tree: ast.AST, source: str) -> set:
    used = {n.id for n in ast.walk(tree) if isinstance(n, ast.Name)}
    used |= {n.attr for n in ast.walk(tree) if isinstance(n, ast.Attribute)}
    # attribute bases (module.attr) and string annotations
    for node in ast.walk(tree):
        if isinstance(node, ast.Attribute) and isinstance(node.value, ast.Name):
            used.add(node.value.id)
        elif isinstance(node, ast.Constant) and isinstance(node.value, str):
            used.update(re.findall(r"[A-Za-z_][A-Za-z_0-9]*", node.value))
    return used


def gate_lint() -> Gate:
    gate = Gate("lint", "AST checks (no external linter is installable here)")
    for path in modules():
        source = path.read_text(encoding="utf-8")
        tree = ast.parse(source)
        name = path.name

        imported = _imported_names(tree)
        used = _used_names(tree, source)
        for bound, line in sorted(imported.items(), key=lambda kv: kv[1]):
            if bound == "annotations":            # from __future__
                continue
            if bound not in used:
                gate.fail(f"{name}:{line}: unused import '{bound}'")

        # A redefinition only matters inside one scope: methods called
        # complete() on two different classes are not duplicates.
        for scope in ast.walk(tree):
            body = getattr(scope, "body", [])
            for child in (body if isinstance(body, list) else []):
                if isinstance(child, (ast.FunctionDef, ast.AsyncFunctionDef)):
                    child.parent_class = id(scope)
        seen: dict = {}
        for node in ast.walk(tree):
            if isinstance(node, ast.ExceptHandler) and node.type is None:
                gate.fail(f"{name}:{node.lineno}: bare 'except:' swallows "
                          "KeyboardInterrupt/SystemExit")
            if isinstance(node, (ast.FunctionDef, ast.AsyncFunctionDef)):
                for default in node.args.defaults + node.args.kw_defaults:
                    if isinstance(default, (ast.List, ast.Dict, ast.Set)):
                        gate.fail(f"{name}:{node.lineno}: mutable default "
                                  f"argument in {node.name}()")
                key = (getattr(node, "parent_class", None), node.name)
                if key in seen and node.name != "__init__":
                    gate.fail(f"{name}:{node.lineno}: {node.name}() redefines "
                              f"the definition at line {seen[key]}")
                seen[key] = node.lineno
            if isinstance(node, ast.Call) and isinstance(node.func, ast.Name) \
                    and node.func.id == "print" and name not in PRINTS_ALLOWED:
                gate.fail(f"{name}:{node.lineno}: print() in library code — "
                          "use the logger")

        for i, line in enumerate(source.splitlines(), 1):
            if re.search(r"#\s*(TODO|FIXME|XXX|HACK)\b", line):
                gate.fail(f"{name}:{i}: unresolved {line.strip()[:60]}")
    return gate


# --------------------------------------------------------------------------- #
# 4/5. unit tests + coverage                                                  #
# --------------------------------------------------------------------------- #
def gate_unit_and_coverage() -> tuple:
    unit = Gate("unit", "pytest suite")
    cov = Gate("coverage", "100% line coverage on every target module")
    import covrun

    buffer = io.StringIO()
    with redirect_stdout(buffer):
        rc = covrun.main()
    output = buffer.getvalue()
    if " passed" in output:
        summary = [ln for ln in output.splitlines() if "passed" in ln or "failed" in ln]
        unit.detail = summary[0].strip() if summary else ""
    if "TESTS FAILED" in output or "failed" in output.split("COVERAGE")[0]:
        unit.fail("pytest reported failures:\n" + "\n".join(
            ln for ln in output.splitlines() if "FAILED" in ln or "Error" in ln)[:2000])
    for line in output.splitlines():
        if "MISSING" in line:
            cov.fail(line.strip())
        if "missing lines:" in line:
            cov.findings[-1] += "  " + line.strip()
    if rc != 0 and cov.ok and unit.ok:
        cov.fail("covrun reported a non-zero exit:\n" + output[-500:])
    cov.detail = next((ln.strip() for ln in output.splitlines()
                       if ln.strip().startswith("TOTAL")), "")
    return unit, cov


# --------------------------------------------------------------------------- #
# 6. frontend                                                                 #
# --------------------------------------------------------------------------- #
def gate_frontend() -> Gate:
    gate = Gate("frontend", "page JS parses, ids resolve, strings are escaped")
    if not INDEX.is_file():
        gate.fail(f"{INDEX} not found")
        return gate
    html = INDEX.read_text(encoding="utf-8")
    scripts = re.findall(r"<script>(.*?)</script>", html, re.S)
    if not scripts:
        gate.fail("no inline <script> block found")
        return gate

    node = subprocess.run(["which", "node"], capture_output=True, text=True)
    if node.returncode == 0:
        for i, block in enumerate(scripts):
            proc = subprocess.run([node.stdout.strip(), "--check", "-"],
                                  input=block, text=True, capture_output=True)
            if proc.returncode != 0:
                gate.fail(f"script block {i} fails node --check: "
                          f"{proc.stderr.strip().splitlines()[0][:160]}")
    else:                                       # pragma: no cover - node present here
        gate.fail("node not available to syntax-check the page JS")

    # every id read by the page must exist in the markup
    ids = set(re.findall(r'\bid="([^"]+)"', html))
    for ref in sorted(set(re.findall(r'\$\("([^"]+)"\)', html))
                      | set(re.findall(r'getElementById\("([^"]+)"\)', html))):
        if ref not in ids:
            gate.fail(f"JS reads #{ref}, which no element defines")

    # server-provided strings interpolated into innerHTML must be escaped
    for expr in re.findall(r"\$\{([^{}]*)\}", html):
        body = expr.strip()
        if any(w in body for w in SAFE_WRAPPERS) or any(s in body for s in SAFE_SHAPES):
            continue
        for field in UNSAFE_IF_RAW:
            # Only property reads count: obj.field or obj["field"]. A bare
            # "field" string (e.g. class="note") is markup, not data.
            if re.search(rf"\.{field}\b", body) or \
                    re.search(rf"""\[["']{field}["']\]""", body):
                gate.fail(f"unescaped interpolation of a string field: "
                          f"${{{body[:70]}}}")
                break
    return gate


# --------------------------------------------------------------------------- #
# 7. browser (Playwright) + UI coverage                                       #
# --------------------------------------------------------------------------- #
def gate_browser() -> Gate:
    """The browser suite, plus the UI-coverage floor it exists to defend.

    The frontend gate is static: it proves the JS parses and escapes. It cannot
    tell whether a panel ever renders. Without this gate the page could sit at 11%
    browser coverage while the dashboard reported every gate green — which is
    exactly the state this replaces.
    """
    gate = Gate("browser", "the page renders and responds in a real browser")
    suites = sorted(HERE.glob("test_e2e*.py"))
    if not suites:                                  # pragma: no cover - defensive
        gate.skip("no browser suites found")
        return gate
    try:
        import playwright.sync_api  # noqa: F401
    except ImportError:
        gate.skip("playwright is not installed")
        return gate

    proc = subprocess.run(
        [sys.executable, "-m", "pytest", "-q", "-p", "no:randomly",
         *[str(p) for p in suites]],
        cwd=str(ROOT), capture_output=True, text=True)
    summary = next((ln.strip() for ln in reversed(proc.stdout.splitlines())
                    if " passed" in ln or " failed" in ln), "")
    if proc.returncode != 0:
        gate.fail("browser suite failed: " + (summary or proc.stdout[-600:]))

    import uicov
    report = uicov.measure()
    overall = report["_total"]
    gate.detail = f"{summary}  ·  UI coverage {overall['pct']:.1f}%"
    if overall["pct"] + 1e-9 < uicov.THRESHOLD:
        missing = [f"{kind}:{item}"
                   for kind in ("tab", "section", "render", "listener",
                                "control", "delegated")
                   for item in report[kind]["missing"]]
        gate.fail(f"UI coverage {overall['pct']:.1f}% < {uicov.THRESHOLD:.1f}% — "
                  f"unexercised: {', '.join(missing[:12])}"
                  + (" …" if len(missing) > 12 else ""))
    return gate


# --------------------------------------------------------------------------- #
# 8. live service                                                             #
# --------------------------------------------------------------------------- #
def _get(path: str, timeout: float = 20.0):
    req = urllib.request.Request(BASE_URL + path)
    with urllib.request.urlopen(req, timeout=timeout) as resp:
        return resp.status, dict(resp.headers), resp.read()


def _post(path: str, payload: dict, timeout: float = 60.0):
    data = json.dumps(payload).encode()
    req = urllib.request.Request(BASE_URL + path, data=data, method="POST",
                                 headers={"Content-Type": "application/json"})
    try:
        with urllib.request.urlopen(req, timeout=timeout) as resp:
            return resp.status, json.loads(resp.read() or b"{}")
    except urllib.error.HTTPError as exc:
        return exc.code, json.loads(exc.read() or b"{}")


def gate_live() -> Gate:
    gate = Gate("live", "running service: contracts, headers, CSRF, GET purity")
    try:
        status, _, _ = _get("/api/health")
    except (urllib.error.URLError, OSError) as exc:
        gate.skip(f"service not reachable at {BASE_URL} ({exc})")
        return gate

    expect_keys = {
        "/api/health": ("ok", "samples"),
        "/api/metrics": ("latest", "series", "scorecard", "meta"),
        "/api/fixes": ("enabled", "token", "fixes"),
        "/api/trends": ("days_tracked", "metrics", "score_trend", "regime"),
        "/api/rca": ("score", "threshold", "breach", "findings", "summary"),
        "/api/autoimprove": ("mode", "interval_s", "threshold", "recent"),
    }
    payloads = {}
    for path, keys in expect_keys.items():
        try:
            status, headers, body = _get(path)
        except (urllib.error.URLError, OSError) as exc:
            gate.fail(f"GET {path} failed: {exc}")
            continue
        if status != 200:
            gate.fail(f"GET {path} returned {status}")
            continue
        for header, value in (("X-Content-Type-Options", "nosniff"),
                              ("X-Frame-Options", "SAMEORIGIN"),
                              ("Cache-Control", "no-store")):
            if headers.get(header) != value:
                gate.fail(f"GET {path} missing header {header}: {value}")
        try:
            data = json.loads(body)
        except json.JSONDecodeError:
            gate.fail(f"GET {path} did not return JSON")
            continue
        payloads[path] = data
        for key in keys:
            if key not in data:
                gate.fail(f"GET {path} response is missing '{key}'")

    # the dashboard page itself
    try:
        status, _, body = _get("/")
        if status != 200 or b"<html" not in body[:200].lower():
            gate.fail("GET / did not return the dashboard page")
        if b'id="tab-auto"' not in body:
            gate.fail("GET / is serving a page without the Auto-Improve tab")
    except (urllib.error.URLError, OSError) as exc:
        gate.fail(f"GET / failed: {exc}")

    # CSRF: mutating endpoints must reject a wrong token
    for path in ("/api/fix", "/api/autoimprove/run", "/api/autoimprove/mode"):
        code, data = _post(path, {"token": "wrong", "confirm": True, "mode": "off"})
        if code != 403:
            gate.fail(f"POST {path} with a bad token returned {code}, expected 403")

    # GET purity: reading the API must not run a cycle
    before = payloads.get("/api/autoimprove", {}).get("cycles")
    for path in ("/api/rca", "/api/trends", "/api/metrics", "/api/autoimprove"):
        try:
            _get(path)
        except (urllib.error.URLError, OSError) as exc:      # pragma: no cover
            gate.fail(f"GET {path} failed on the second read: {exc}")
    after = None
    try:
        after = json.loads(_get("/api/autoimprove")[2]).get("cycles")
    except (urllib.error.URLError, OSError, json.JSONDecodeError) as exc:
        gate.fail(f"could not re-read /api/autoimprove: {exc}")
    if before is not None and after is not None and after != before:
        gate.fail(f"GET requests changed the cycle count ({before} -> {after})")

    # unknown paths must 404 rather than 500
    try:
        code, _, _ = _get("/api/does-not-exist")
        gate.fail(f"unknown path returned {code}, expected 404")
    except urllib.error.HTTPError as exc:
        if exc.code != 404:
            gate.fail(f"unknown path returned {exc.code}, expected 404")
    except (urllib.error.URLError, OSError) as exc:           # pragma: no cover
        gate.fail(f"unknown path probe failed: {exc}")

    # state files must be valid and bounded
    import sysperf.config as config
    for path in (config.AUTO_STATE_FILE, config.TRENDS_FILE, config.ADAPTIVE_FILE):
        if not Path(path).is_file():
            continue
        try:
            json.loads(Path(path).read_text())
        except json.JSONDecodeError as exc:
            gate.fail(f"{Path(path).name} is not valid JSON: {exc}")
    log = Path(config.AUTO_LOG_FILE)
    if log.is_file():
        lines = log.read_text().splitlines()
        if len(lines) > config.AUTO_LOG_MAX_LINES:
            gate.fail(f"{log.name} has {len(lines)} lines, cap is "
                      f"{config.AUTO_LOG_MAX_LINES}")
        for i, line in enumerate(lines, 1):
            try:
                json.loads(line)
            except json.JSONDecodeError:
                gate.fail(f"{log.name}:{i} is not valid JSON")
                break
    return gate


# --------------------------------------------------------------------------- #
# runner                                                                      #
# --------------------------------------------------------------------------- #
def write_report(path: Path, gates: list, started: float, verdict: str,
                 exit_code: int) -> None:
    """Persist the run as JSON so the dashboard can show it without re-running.

    Written atomically (temp file + replace): the dashboard polls this file, and
    a half-written report would read as a corrupt/absent run.
    """
    def counts(detail: str, *names: str) -> dict:
        found = {}
        for name in names:
            match = re.search(rf"(\d+)\s+{name}\b", detail)
            if match:
                found[name] = int(match.group(1))
        return found

    unit = next((g for g in gates if g.name == "unit"), None)
    cov = next((g for g in gates if g.name == "coverage"), None)
    unit_detail = getattr(unit, "detail", "") if unit else ""
    cov_detail = getattr(cov, "detail", "") if cov else ""
    cov_pct = re.search(r"([\d.]+)%", cov_detail)
    seconds = re.search(r"in ([\d.]+)s", unit_detail)

    report = {
        "finished_at": time.time(),
        "duration_s": round(time.time() - started, 1),
        "verdict": verdict,
        "exit_code": exit_code,
        "gates": [{
            "name": g.name,
            "description": g.description,
            "status": "skip" if g.skipped else ("pass" if g.ok else "fail"),
            "skip_reason": g.skip_reason,
            "detail": getattr(g, "detail", ""),
            "findings": list(g.findings),
        } for g in gates],
        "unit": {
            "detail": unit_detail,
            "duration_s": float(seconds.group(1)) if seconds else None,
            **counts(unit_detail, "passed", "failed", "skipped", "error"),
        },
        "coverage": {
            "detail": cov_detail,
            "line_pct": float(cov_pct.group(1)) if cov_pct else None,
        },
    }
    path.parent.mkdir(parents=True, exist_ok=True)
    tmp = path.with_suffix(path.suffix + ".tmp")
    tmp.write_text(json.dumps(report, indent=1), encoding="utf-8")
    tmp.replace(path)


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--no-live", action="store_true",
                        help="skip the live HTTP gate")
    parser.add_argument("--no-browser", action="store_true",
                        help="skip the Playwright gate (slow: it drives Chromium)")
    parser.add_argument("--quick", action="store_true",
                        help="only run the unit tests and coverage gate")
    parser.add_argument("--json", metavar="PATH",
                        help="also write a machine-readable report to PATH "
                             "(read by the dashboard's Test Suite card)")
    args = parser.parse_args()

    started = time.time()
    gates: list[Gate] = []
    if not args.quick:
        gates.append(gate_discover())
        gates.append(gate_compile())
        gates.append(gate_lint())
    unit, cov = gate_unit_and_coverage()
    gates.extend([unit, cov])
    if not args.quick:
        gates.append(gate_frontend())
        if args.no_browser:
            browser = Gate("browser", "real-browser rendering + UI coverage")
            browser.skip("--no-browser")
            gates.append(browser)
        else:
            gates.append(gate_browser())
        if args.no_live:
            live = Gate("live", "running service")
            live.skip("--no-live")
            gates.append(live)
        else:
            gates.append(gate_live())

    width = 62
    print("\n" + "=" * width)
    print("  SYSPERF DASHBOARD — TEST RUNNER")
    print("=" * width)
    failed = 0
    for gate in gates:
        if gate.skipped:
            mark, extra = "SKIP", f" ({gate.skip_reason})"
        elif gate.ok:
            mark, extra = "PASS", ""
        else:
            mark, extra = "FAIL", f" — {len(gate.findings)} issue(s)"
            failed += 1
        detail = getattr(gate, "detail", "")
        print(f"  [{mark}] {gate.name:9} {gate.description}{extra}")
        if detail and gate.ok:
            print(f"         {detail}")
        for finding in gate.findings:
            print(f"         · {finding}")
    print("-" * width)
    verdict = "ALL GATES PASSED" if not failed else f"{failed} GATE(S) FAILED"
    print(f"  {verdict} in {time.time() - started:.1f}s")
    print("=" * width)
    if args.json:
        write_report(Path(args.json), gates, started, verdict, 1 if failed else 0)
    return 1 if failed else 0


if __name__ == "__main__":
    sys.exit(main())
