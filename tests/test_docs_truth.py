"""User-facing docs may only describe things the code has.

By 4.1 the README and manual still documented features from 2.x/3.x that no
longer existed: a ``--recalibrate`` flag and route, an ``anonymize`` option,
an off-loopback token, and modules that had been renamed. Every CLI flag,
API route, module, and environment variable the docs name must exist; retired
features may not be mentioned at all.
"""
import re
from pathlib import Path

import pytest
from tests.test_app import app

from sysperf import cli

ROOT = Path(__file__).resolve().parents[1]
DOCS = ["README.md", "docs/USER_MANUAL.md", "START-HERE.txt", "CONTRIBUTING.md",
        "docs/AI_EVOLUTION.md", "docs/ARCHITECTURE.md", "docs/OPERATIONS.md",
        "docs/REQUIREMENTS.md", "docs/DECISIONS.md", "docs/FEATURE_AUDIT.md",
        "docs/RELEASE_CHECKLIST.md", "SECURITY.md", "CLAUDE.md", "packaging/msix/README.md"]

# Features that were removed; naming them in docs misleads readers.
RETIRED = {
    "--recalibrate": "baseline reset is: delete baseline.json and restart",
    "/api/recalibrate": "no such route",
    "anonymize": "never implemented in 4.x",
    "X-SysPerf-Token": "there is no off-loopback token",
    "SYSPERF_TOKEN": "there is no off-loopback token",
    "/api/advice": "no such route",
    "/api/profile": "no such route",
    "/api/config": "settings are /api/settings",
    "auth_token": "there is no off-loopback token",
    "full_every": "fixed at every 5th sample",
    "max_samples": "fixed at 240 samples (4 h)",
    "history_max_bytes": "history is SQLite with history.raw_days",
    "score_smooth_window": "not a setting",
    "regress_band_pct": "not a setting",
    "advisor.": "there is no advisor block",
}


def _text(name):
    return (ROOT / name).read_text(encoding="utf-8")


def _routes():
    return {re.sub(r"<[^>]+>", "<id>", r.rule) for r in app.app.url_map.iter_rules()}


@pytest.mark.parametrize("doc", DOCS)
def test_doc_names_no_retired_feature(doc):
    text = _text(doc)
    found = [f"{k} ({why})" for k, why in RETIRED.items() if k in text]
    assert found == []


@pytest.mark.parametrize("doc", DOCS)
def test_doc_cli_flags_exist(doc):
    flags = {o for a in cli.build_parser()._actions for o in a.option_strings}
    used = set(re.findall(r"sysperf(?:\.exe)?((?:\s+--[a-z][a-z-]+(?:\s+\S+)?)+)", _text(doc)))
    named = {f for chunk in used for f in re.findall(r"--[a-z][a-z-]+", chunk)}
    assert named - flags == set()


@pytest.mark.parametrize("doc", DOCS)
def test_doc_api_routes_exist(doc):
    routes = _routes()
    named = {re.sub(r"<[^>]+>|\{[^}]+\}", "<id>", m.rstrip(".,)"))
             for m in re.findall(r"(/api/[A-Za-z0-9_/<>{}.-]*[A-Za-z0-9_>}])", _text(doc))}
    assert named - routes == set()


@pytest.mark.parametrize("doc", DOCS)
def test_doc_modules_exist(doc):
    files = {p.name for p in ROOT.rglob("*.py") if ".git" not in p.parts and "build" not in p.parts}
    named = set(re.findall(r"`(?:[\w/]+/)?([a-z_]+\.py)`", _text(doc)))
    named |= set(re.findall(r"^[\s│├└─]*([a-z_]+\.py)\s", _text(doc), re.M))
    assert named - files == set()


@pytest.mark.parametrize("doc", DOCS)
def test_doc_environment_variables_exist(doc):
    source = "\n".join(p.read_text(encoding="utf-8")
                       for p in (ROOT / "src" / "sysperf").rglob("*.py"))
    named = set(re.findall(r"\b((?:SYSPERF|PERFDASH)_[A-Z_]+)\b", _text(doc)))
    assert {v for v in named if f'"{v}"' not in source} == set()
