#!/usr/bin/env python3
"""
UI coverage gate — how much of the page the browser tests actually exercise.

``covrun.py`` proves every Python line runs. Nothing made the equivalent claim
about the page, so the browser suite could sit at 11% of the interface while the
dashboard reported "all gates green". This closes that asymmetry.

The measured surface is everything a user can see change or interact with:

``render``     an element id the page writes into (``$("x").innerHTML``). If no
               test asserts on it, nothing proves it ever renders.
``listener``   an id with an attached event handler.
``control``    a static ``<button>``, ``<select>`` or ``<input>`` with an id.
``tab``        a top-level view.
``section``    a collapsible detail section (``data-sect``).
``delegated``  a class used for event delegation on generated rows, where the
               handler cannot be found by id.

An item counts as covered when the browser suite names it — by id, by
``data-sect`` value, or by class. That is a deliberately simple rule: it proves a
test *reaches* the element, not that every assertion is meaningful. It is a floor,
not a ceiling, and it is the same bargain line coverage makes.

    python3 tests/uicov.py            # report, exit non-zero below the threshold
    python3 tests/uicov.py --list     # list every uncovered item
"""
from __future__ import annotations

import argparse
import re
import sys
from pathlib import Path

HERE = Path(__file__).resolve().parent
ROOT = HERE.parent
# Repo keeps the page at <root>/static; an export at <root>/src/sysperf/static.
_PKG = ROOT / "src" / "sysperf"
PKG = _PKG if _PKG.is_dir() else ROOT
INDEX = PKG / "static" / "index.html"
# Every browser suite counts, so coverage can be grown in a new module without
# bloating an existing one.
SUITES = sorted(HERE.glob("test_e2e*.py"))

# Required coverage. 100% is the target and the default: an element nothing
# touches is an element nothing protects.
THRESHOLD = 100.0

# Ids that cannot be exercised from the browser suite, each with the reason.
# Kept explicit and small — an exclusion is a hole in the gate, so it has to be
# argued for rather than assumed.
EXCLUDED = {
    # The print/export stylesheet hides these; they exist only for @media print.
    # Nothing here.
}


def _is_static_id(value: str) -> bool:
    """False for ids built inside a template string, which are not real elements."""
    return not any(ch in value for ch in "${}+\"'\\")


def surface(html: str) -> dict:
    """Every measurable element, grouped by kind."""
    render = {i for i in re.findall(
        r'\$\("([^"]+)"\)\.(?:innerHTML|textContent|innerText)', html)
        if _is_static_id(i)}
    # Also count ids written through a local alias, e.g. const m = $("x"); m.innerHTML
    aliased = re.findall(r'=\s*\$\("([^"]+)"\)\s*[,;]', html)
    render |= {i for i in aliased if _is_static_id(i)}
    listener = {i for i in re.findall(r'\$\("([^"]+)"\)\.addEventListener', html)
                if _is_static_id(i)}
    control = {i for i in re.findall(
        r'<(?:button|select|input)\b[^>]*\bid="([^"]+)"', html)
        if _is_static_id(i)}
    tab = set(re.findall(r'<button class="tab" id="([^"]+)"', html))
    section = set(re.findall(r'data-sect="([^"]+)"', html))
    delegated = set(re.findall(r'closest\("\.([a-z-]+)"\)', html))
    groups = {"render": render, "listener": listener, "control": control,
              "tab": tab, "section": section, "delegated": delegated}
    return {k: {i for i in v if i not in EXCLUDED} for k, v in groups.items()}


def covered(items: set, suite: str) -> set:
    """Items the browser suite names."""
    return {i for i in items if i in suite}


def measure(html: str | None = None, suite: str | None = None) -> dict:
    html = INDEX.read_text(encoding="utf-8") if html is None else html
    if suite is None:
        suite = "\n".join(p.read_text(encoding="utf-8") for p in SUITES)
    groups = surface(html)
    report, total, hit = {}, 0, 0
    for kind, items in groups.items():
        found = covered(items, suite)
        report[kind] = {"total": len(items), "covered": len(found),
                        "missing": sorted(items - found)}
        total += len(items)
        hit += len(found)
    report["_total"] = {"total": total, "covered": hit,
                        "pct": round(100.0 * hit / total, 1) if total else 100.0}
    return report


def main(argv=None) -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--list", action="store_true",
                        help="list every uncovered item")
    parser.add_argument("--threshold", type=float, default=THRESHOLD)
    args = parser.parse_args(argv)

    report = measure()
    print(f"{'KIND':<12}{'TOTAL':>7}{'COVERED':>9}{'PCT':>7}")
    for kind in ("tab", "section", "render", "listener", "control", "delegated"):
        row = report[kind]
        pct = 100.0 * row["covered"] / row["total"] if row["total"] else 100.0
        print(f"  {kind:<10}{row['total']:>7}{row['covered']:>9}{pct:>6.1f}%")
    overall = report["_total"]
    print(f"  {'TOTAL':<10}{overall['total']:>7}{overall['covered']:>9}"
          f"{overall['pct']:>6.1f}%")

    if args.list:
        for kind in ("tab", "section", "render", "listener", "control",
                     "delegated"):
            for item in report[kind]["missing"]:
                print(f"    MISSING {kind}: {item}")

    if overall["pct"] + 1e-9 < args.threshold:
        print(f"\nUI COVERAGE BELOW THRESHOLD: {overall['pct']:.1f}% < "
              f"{args.threshold:.1f}%")
        return 1
    print(f"\nUI coverage {overall['pct']:.1f}% meets the "
          f"{args.threshold:.1f}% threshold.")
    return 0


if __name__ == "__main__":
    sys.exit(main())
