"""SECURITY.md's mutating-surface table must match the real route table.

Up to 4.1.0 the policy said "exactly four endpoints can change anything" and
named one that no longer existed, while the app had grown 28 POST routes. These
tests make that drift fail the suite: every POST route is listed, every listed
route exists, and every one refuses a request without the CSRF token.
"""
import re
from pathlib import Path

import pytest
from tests.test_app import app

DOC = Path(__file__).resolve().parents[1] / "SECURITY.md"


def _post_routes() -> set:
    return {r.rule for r in app.app.url_map.iter_rules() if "POST" in r.methods}


def _documented() -> set:
    section = DOC.read_text(encoding="utf-8").split("## Mutating surface", 1)[1]
    section = section.split("\n## ", 1)[0]
    return set(re.findall(r"`POST (/api/[^`\s]+)`", section))


def test_every_post_route_is_documented_and_nothing_extra():
    routes, doc = _post_routes(), _documented()
    assert routes, "no POST routes found"
    assert routes - doc == set(), f"undocumented POST routes: {sorted(routes - doc)}"
    assert doc - routes == set(), f"SECURITY.md lists routes that do not exist: {sorted(doc - routes)}"


@pytest.mark.parametrize("rule", sorted(_post_routes()))
def test_every_post_route_requires_the_csrf_token(rule):
    # confirm=true so /api/fix is asked to apply, not just preview.
    r = app.app.test_client().post(rule, json={"confirm": True})
    assert r.status_code == 403, f"{rule} accepted a POST without the token"
    assert "token" in r.get_json()["error"]


def test_supported_versions_name_the_current_minor():
    major_minor = ".".join(app.APP_VERSION.split(".")[:2])
    assert f"| {major_minor}.x | Yes |" in DOC.read_text(encoding="utf-8")
