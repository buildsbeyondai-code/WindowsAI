"""
End-to-end tests with a stubbed Windows collector.

The PowerShell layer is the only part that needs Windows, so it is replaced by a
sample from ``conftest``. Everything above it — sampling, carry-forward, baseline
capture, snapshot assembly, the HTTP API — is exercised on any platform.
"""
from __future__ import annotations

import copy
import json

import pytest

from sysperf.adaptive import AdaptiveScorer
from sysperf.app import create_app
from sysperf.collector import Collector
from sysperf.hardware import build_hardware, ensure_watchlist, load_watchlist


class StubCollector(Collector):
    """A Collector whose only Windows dependency is replaced by a fixture."""

    def __init__(self, *args, sample_data: dict, **kwargs):
        self._sample_data = sample_data
        self.full_calls = 0
        self.fast_calls = 0
        self.fail_next = False
        super().__init__(*args, **kwargs)

    def read_windows(self, full: bool) -> dict:
        if self.fail_next:
            self.fail_next = False
            raise RuntimeError("collector produced no output")
        data = copy.deepcopy(self._sample_data)
        if full:
            self.full_calls += 1
        else:
            self.fast_calls += 1
            # The fast tier does not gather the slow metrics.
            for key in ("startup_apps", "net_remote", "hw_problem_devices",
                        "drv_stale", "services_running", "hostname",
                        "wsl_config_present", "power_plan"):
                data.pop(key, None)
        return data


@pytest.fixture
def collector(tmp_path, cfg, sample):
    return StubCollector(
        cfg, AdaptiveScorer(tmp_path / "adaptive.json", cfg),
        history_file=tmp_path / "history.jsonl",
        baseline_file=tmp_path / "baseline.json",
        sample_data=sample)


def test_first_sample_is_fast_so_the_dashboard_is_not_blank(collector):
    """A full sweep takes tens of seconds; it must not be what a user waits for."""
    assert collector.sample() is True
    assert collector.fast_calls == 1 and collector.full_calls == 0
    assert collector.latest["committed_mb"] == 14000     # data is already usable
    assert collector.baseline == {}                     # but no baseline yet


def test_second_sample_is_full_and_captures_the_baseline(collector):
    collector.sample()
    collector.sample()
    assert collector.full_calls == 1
    assert collector.baseline["metrics"]["committed_mb"] == 14000
    assert collector.baseline_file.exists()


def test_full_sweep_recurs_on_the_configured_cadence(collector):
    collector.cfg["full_every"] = 5
    for _ in range(11):
        collector.sample()
    # samples 1, 5 and 10 are full
    assert collector.full_calls == 3


def test_baseline_is_captured_only_once(collector):
    for _ in range(2):
        collector.sample()
    first = collector.baseline["captured"]
    for _ in range(3):
        collector.sample()
    assert collector.baseline["captured"] == first


def test_recalibrate_replaces_the_baseline(collector, sample):
    collector.sample()
    collector.sample()
    collector._sample_data = dict(sample, committed_mb=5000)
    collector.sample()
    base = collector.recalibrate()
    assert base["metrics"]["committed_mb"] == 5000
    assert base["note"] == "recalibrated by user"


def test_recalibrate_without_data_is_refused(collector):
    with pytest.raises(RuntimeError):
        collector.recalibrate()


def test_slow_metrics_are_carried_forward(collector):
    collector.sample()                       # fast
    collector.sample()                       # full
    collector.sample()                       # fast again: slow keys absent
    assert collector.full_calls == 1 and collector.fast_calls == 2
    assert collector.latest["startup_apps"] == 18
    assert collector.latest["hostname"] == "TESTBOX"


def test_history_is_persisted_and_reloaded(collector, tmp_path, cfg, sample):
    for _ in range(3):
        collector.sample()
    lines = (tmp_path / "history.jsonl").read_text().strip().splitlines()
    assert len(lines) == 3
    assert json.loads(lines[0])["committed_mb"] == 14000

    reborn = StubCollector(cfg, None, history_file=tmp_path / "history.jsonl",
                           baseline_file=tmp_path / "baseline.json",
                           sample_data=sample)
    assert len(reborn.history) == 3


def test_a_failed_sample_is_reported_not_raised(collector):
    collector.fail_next = True
    assert collector.sample() is False
    assert "collector produced no output" in collector.error
    assert collector.sample() is True
    assert collector.error is None


def test_snapshot_shape(collector):
    collector.sample()
    collector.sample()                       # baseline needs the full sweep
    snap = collector.snapshot()
    for key in ("latest", "series", "scorecard", "browser", "hardware", "advice",
                "profile", "baseline", "meta"):
        assert key in snap, f"snapshot missing {key}"
    assert snap["meta"]["criteria_count"] > 0
    assert snap["scorecard"]["rows"]
    assert snap["series"]["t"]


def test_snapshot_is_cached_but_reports_fresh_server_time(collector):
    collector.sample()
    collector.sample()
    first = collector.snapshot()
    second = collector.snapshot()
    assert second["meta"]["samples"] == first["meta"]["samples"]
    assert second["meta"]["server_time"] >= first["meta"]["server_time"]


def test_snapshot_is_json_serialisable(collector):
    collector.sample()
    collector.sample()
    json.dumps(collector.snapshot())        # would raise on a non-serialisable value


def test_anonymize_replaces_names_but_keeps_the_shape(collector):
    collector.cfg["anonymize"] = True
    collector.sample()
    collector.sample()
    snap = collector.snapshot()
    assert snap["profile"]["hostname"] != "TESTBOX"
    assert snap["profile"]["hostname"].startswith("app-")
    assert all(n.startswith("app-") for n in snap["latest"]["wv2_hosts"])
    assert snap["latest"]["committed_mb"] == 14000      # numbers untouched


def test_watchlist_template_is_created_then_respected(tmp_path):
    path = tmp_path / "hw_watchlist.json"
    ensure_watchlist(path)
    assert path.exists()
    path.write_text('[{"component":"mine","status":"open"}]', encoding="utf-8")
    ensure_watchlist(path)                   # must not overwrite
    assert load_watchlist(path)[0]["component"] == "mine"


def test_watchlist_open_issue_count(tmp_path, sample):
    path = tmp_path / "wl.json"
    path.write_text(json.dumps([{"component": "a", "status": "open"},
                                {"component": "b", "status": "resolved"}]),
                    encoding="utf-8")
    assert build_hardware(sample, path)["open_issues"] == 1


def test_battery_health_percentage(sample):
    assert build_hardware(sample)["battery_health_pct"] == pytest.approx(58.5, abs=0.1)


# --------------------------------------------------------------------- HTTP ---

@pytest.fixture
def client(cfg, collector):
    collector.sample()
    collector.sample()                       # fast, then full: baseline captured
    app = create_app(cfg, collector)
    app.config.update(TESTING=True)
    return app.test_client()


def test_index_serves_the_dashboard(client):
    resp = client.get("/")
    assert resp.status_code == 200
    assert b"SysPerf" in resp.data


def test_api_endpoints_answer(client):
    for path in ("/api/metrics", "/api/health", "/api/advice", "/api/profile",
                 "/api/config"):
        resp = client.get(path)
        assert resp.status_code == 200, path
        assert resp.is_json, path


def test_health_reports_readiness(client):
    body = client.get("/api/health").get_json()
    assert body["ok"] is True
    assert body["baseline_captured"] is True
    assert body["samples"] >= 1


def test_config_endpoint_never_leaks_the_token(client):
    assert "auth_token" not in client.get("/api/config").get_json()


def test_security_headers_are_present(client):
    headers = client.get("/api/health").headers
    assert headers["X-Content-Type-Options"] == "nosniff"
    assert headers["X-Frame-Options"] == "SAMEORIGIN"
    assert headers["Cache-Control"] == "no-store"
    assert "default-src 'self'" in headers["Content-Security-Policy"]


def test_unknown_route_is_a_json_404(client):
    resp = client.get("/nope")
    assert resp.status_code == 404 and resp.is_json


def test_recalibrate_endpoint(client):
    token = client.get("/api/fixes").get_json()["token"]
    body = client.post("/api/recalibrate", headers={"X-SysPerf-CSRF": token}).get_json()
    assert body["ok"] is True
    assert body["baseline"]["note"] == "recalibrated by user"


def test_token_auth_gates_every_route(cfg, collector):
    cfg["host"] = "0.0.0.0"
    cfg["auth_token"] = "s3cret-token-value"
    collector.sample()
    collector.sample()
    client = create_app(cfg, collector).test_client()
    assert client.get("/api/health").status_code == 401
    assert client.get("/api/health?token=wrong").status_code == 401
    assert client.get("/api/health?token=s3cret-token-value").status_code == 200
    assert client.get("/api/health",
                      headers={"X-SysPerf-Token": "s3cret-token-value"}).status_code == 200
