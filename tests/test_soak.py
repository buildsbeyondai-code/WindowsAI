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


def _rows(hours=24, every=300, **over):
    return [{"t": 1_000_000 + k * every, "rss_mb": 30.0, "sample_age_s": 20, "ok": True,
             "error": "", **over} for k in range(int(hours * 3600 / every) + 1)]


def test_a_complete_flat_day_passes():
    v = _soak_tool().verdict(_rows(), 50, 600, 300, hours=24)
    assert v["state"] == "pass" and v["problems"] == [] and v["gaps_in_evidence"] == []
    c = v["coverage"]
    assert (c["readings"], c["complete"], c["expected"], c["gaps"]) == (289, 289, 289, 0)
    assert c["covered_s"] == 24 * 3600 and c["rss_base_mb"] == c["rss_end_mb"] == 30.0


def test_measured_problems_fail():
    soak = _soak_tool()
    rows = _rows()
    for r in rows[-20:]:
        r["rss_mb"] = 95.0                                  # last hour leaks
    rows[100]["sample_age_s"], rows[101]["ok"] = 900, False
    v = soak.verdict(rows, 50, 600, 300, hours=24)
    assert v["state"] == "fail"
    assert v["problems"] == ["1 reading(s) with data older than 600 s",
                             "1 reading(s) where health was not ok",
                             "RSS grew 65.0 MB (median 30.0 in the first hour, 95.0 in the last)"]


def test_missing_measurements_do_not_pass():
    # 5.5.0 audit F5: two rows missing RSS, age and health produced zero problems
    soak = _soak_tool()
    blank = [{"t": 0, "rss_mb": None, "sample_age_s": None, "ok": None},
             {"t": 300, "rss_mb": None, "sample_age_s": None, "ok": None}]
    v = soak.verdict(blank, 50, 600, 300, hours=24)
    assert v["state"] == "inconclusive" and v["problems"] == []
    assert v["gaps_in_evidence"][0].startswith("memory growth not judged: 0 RSS")
    assert "covered 5.0 min of 24.00 h" in v["gaps_in_evidence"]
    assert "0 complete reading(s) of 289 expected" in v["gaps_in_evidence"]
    assert "no readings" in soak.verdict([], 50, 600, 300)["gaps_in_evidence"]
    assert soak.verdict([{"t": "x"}], 50, 600, 0)["coverage"]["readings"] == 0


def test_gaps_short_runs_and_failed_readings_are_inconclusive():
    soak = _soak_tool()
    rows = _rows()
    del rows[100:130]                                       # 2.5 h with no readings
    rows[5].update(rss_mb=None, error="URLError: refused")
    v = soak.verdict(rows, 50, 600, 300, hours=24)
    assert v["state"] == "inconclusive"
    assert "1 gap(s) between readings, 155 min in total" in v["gaps_in_evidence"]
    assert "258 complete reading(s) of 289 expected" in v["gaps_in_evidence"]
    assert "1 reading(s) could not be taken" in v["gaps_in_evidence"]
    short = soak.verdict(_rows(hours=2), 50, 600, 300, hours=24)
    assert short["state"] == "inconclusive" and "covered 2.00 h of 24.00 h" in short["gaps_in_evidence"]
    assert soak.verdict(_rows(hours=0.5), 50, 600, 300)["gaps_in_evidence"] == [
        "memory growth not judged: 7 RSS reading(s) in the warm-up hour and 0 after it "
        "(need 2 and 1)"]


def test_a_restart_makes_the_soak_inconclusive():
    soak = _soak_tool()
    rows = _rows()
    for k, r in enumerate(rows):
        r["uptime_s"] = (k if k < 150 else k - 150) * 300 + 10     # restarted at reading 150
    v = soak.verdict(rows, 50, 600, 300, hours=24)
    assert v["state"] == "inconclusive" and v["coverage"]["restarts"] == 1
    assert v["gaps_in_evidence"] == ["the service restarted 1 time(s): memory growth across "
                                     "a restart cannot be judged"]
    for k, r in enumerate(rows):
        r["uptime_s"] = k * 300
    assert soak.verdict(rows, 50, 600, 300, hours=24)["state"] == "pass"


def test_a_failed_request_is_recorded_not_fatal():
    soak = _soak_tool()

    def down(base, path):
        raise OSError("connection refused")
    row = soak.sample("http://x", down)
    assert row["rss_mb"] is None and row["error"] == "OSError: connection refused"
    good = soak.sample("http://x", lambda b, p: {"footprint": {"rss_mb": 41, "uptime_s": 9},
                                                  "ok": True, "last_sample_age_s": 12,
                                                  "health": {}})
    assert good["uptime_s"] == 9
    assert (good["rss_mb"], good["sample_age_s"], good["ok"], good["error"]) == (41, 12, True, "")


def test_main_writes_csv_and_verdict_and_exits_by_state(tmp_path, capsys):
    import json
    soak = _soak_tool()
    now = [1_000_000.0]
    out = tmp_path / "s.csv"

    def sleep(s):
        now[0] += s + 0.4                        # each reading takes a little time
    fetch = lambda b, p: {"footprint": {"rss_mb": 30}, "ok": True, "last_sample_age_s": 5}  # noqa: E731
    rc = soak.main(["--hours", "2", "--every", "300", "--out", str(out)], fetch,
                   clock=lambda: now[0], sleep=sleep)
    printed = capsys.readouterr().out
    assert rc == 0 and "PASS" in printed
    assert len(out.read_text().splitlines()) == 1 + 25     # header + readings at 0..120 min
    assert json.loads((tmp_path / "s.csv.verdict.json").read_text())["state"] == "pass"

    def down(b, p):
        raise OSError("refused")
    rc = soak.main(["--hours", "0", "--out", str(out)], down, clock=lambda: now[0], sleep=sleep)
    printed = capsys.readouterr().out
    assert rc == 2 and "INCONCLUSIVE" in printed and "- evidence:" in printed
    fetch_bad = lambda b, p: {"footprint": {"rss_mb": 30}, "ok": False, "last_sample_age_s": 5}  # noqa: E731
    rc = soak.main(["--hours", "0", "--out", str(out)], fetch_bad, clock=lambda: now[0], sleep=sleep)
    assert rc == 1 and "- FAIL: 1 reading(s) where health was not ok" in capsys.readouterr().out
