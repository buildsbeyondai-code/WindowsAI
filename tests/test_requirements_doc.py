"""docs/REQUIREMENTS.md must only cite tests that exist.

The traceability table is worth something only while every ``file::test`` it
names is real; a renamed or deleted test fails here instead of leaving a
requirement silently unverified.
"""
import ast
import re
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
REF = re.compile(r"`(tests/[\w/]+\.py)::(\w+)`")


def _functions(path: Path) -> set:
    tree = ast.parse(path.read_text(encoding="utf-8"))
    return {n.name for n in ast.walk(tree) if isinstance(n, ast.FunctionDef)}


def test_every_cited_test_exists():
    refs = REF.findall((ROOT / "docs" / "REQUIREMENTS.md").read_text(encoding="utf-8"))
    assert len(refs) >= 16, "the traceability table lost its references"
    missing = [f"{f}::{t}" for f, t in refs
               if not (ROOT / f).is_file() or t not in _functions(ROOT / f)]
    assert missing == []


def test_every_acceptance_criterion_cites_a_test():
    text = (ROOT / "docs" / "REQUIREMENTS.md").read_text(encoding="utf-8")
    rows = [ln for ln in text.splitlines() if re.match(r"\| R\d+ \|", ln)]
    assert rows
    for row in rows:
        assert REF.search(row), f"no test cited for {row.split('|')[1].strip()}"
