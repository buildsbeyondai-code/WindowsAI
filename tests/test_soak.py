"""Memory stays flat under sustained polling of the read-only API.

The page polls several routes every few seconds for days. After a warm-up
(first-call caches, imports), Python heap growth over many more rounds must
stay near zero (about 20 KB measured); a 1 KB-per-request leak would show as 1 MB.
tools/soak.py runs the same idea against the real service for hours.
"""
import gc
import tracemalloc

from tests.test_app import app

ROUTES = ("/api/health", "/api/live", "/api/metrics", "/api/fixes", "/api/trends",
          "/api/rca", "/api/learning", "/api/diagnostics", "/api/notifications",
          "/api/store")


def _round(client):
    for r in ROUTES:
        assert client.get(r).status_code == 200, r


def test_polling_does_not_grow_the_heap():
    client = app.app.test_client()
    for _ in range(50):                      # warm-up: caches and lazy imports
        _round(client)
    gc.collect()
    tracemalloc.start()
    try:
        before = tracemalloc.take_snapshot()
        for _ in range(100):                 # 1,000 requests
            _round(client)
        gc.collect()
        after = tracemalloc.take_snapshot()
    finally:
        tracemalloc.stop()
    growth = sum(s.size_diff for s in after.compare_to(before, "filename"))
    assert growth < 256 * 1024, f"heap grew {growth / 1024:.0f} KB over 1,000 requests"


def _soak_tool():
    import importlib.util
    from pathlib import Path
    path = Path(__file__).resolve().parents[1] / "tools" / "soak.py"
    spec = importlib.util.spec_from_file_location("soak_tool", path)
    mod = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(mod)
    return mod


def test_soak_tool_verdicts():
    soak = _soak_tool()
    row = {"rss_mb": 30.0, "sample_age_s": 20, "ok": True}
    flat = [dict(row) for _ in range(30)]
    assert soak.verdict(flat, 50, 600, 300) == []
    leaking = [*flat, {**row, "rss_mb": 95.0}]
    assert "RSS grew 65.0 MB" in soak.verdict(leaking, 50, 600, 300)[0]
    stale = [*flat, {**row, "sample_age_s": 900}, {**row, "ok": False}]
    assert soak.verdict(stale, 50, 600, 300) == ["2 reading(s) stale or not ok"]
