"""The daily component-catalog refresh: every probe, the raise-only rule, suffix
preservation, the Ubuntu series change, atomic writes and the script entry point.
No process or network is touched — run_hidden and urlopen are stubbed."""
import io
import json
import logging
from types import SimpleNamespace

import pytest

import sysperf.refresh_components as rc


@pytest.fixture
def catalog(tmp_path, monkeypatch):
    monkeypatch.setattr(rc, "BASE", tmp_path)
    monkeypatch.setattr(rc, "COMPONENTS_FILE", tmp_path / "components.json")
    monkeypatch.setattr(rc, "LOG_FILE", tmp_path / "refresh.log")
    rc._dist_cache.clear()
    data = {"assessment_date": "2026-01-01", "catalog": [
        {"key": "ubuntu", "latest": "24.04", "channel": "apt"},
        {"key": "node", "latest": "22.20.0 (major upgrade - confirm first)"},
        {"key": "npm", "latest": "10.0.0"},
        {"key": "powershell", "latest": "7.5.0"},
        {"key": "git", "latest": "2.43.0"},
        {"key": "nginx", "latest": "1.99.0"},
        {"key": "gunicorn", "latest": "23.0.0"},
        {"key": "windows", "latest": "26100"},
        {"key": "bios", "compare": "date", "latest": "2026-04-22"},
        {"key": "python3", "latest": "3.12.0"}]}
    (tmp_path / "components.json").write_text(json.dumps(data))
    return tmp_path


def _apt(policy):
    def run(argv, **kw):
        if argv[0] == "do-release-upgrade":
            return SimpleNamespace(stdout="", stderr="New release '26.04 LTS' available.")
        return SimpleNamespace(stdout=policy.get(argv[-1], ""), stderr="")
    return run


class _Resp(io.BytesIO):
    def __enter__(self):
        return self

    def __exit__(self, *a):
        return False


def _urls(table):
    def urlopen(url, timeout=None):
        u = url if isinstance(url, str) else url.full_url
        for k, v in table.items():
            if k in u:
                if isinstance(v, Exception):
                    raise v
                return _Resp(json.dumps(v).encode())
        raise OSError("unexpected url " + u)
    return urlopen


def test_refresh_raises_only_newer_targets_and_keeps_notes(catalog, monkeypatch):
    monkeypatch.setattr(rc, "run_hidden", _apt({
        "git": "  Installed: 1:2.43.0-1\n  Candidate: 1:2.45.1-1ubuntu1\n",
        "nginx": "  Candidate: 1.24.0-2\n",                     # older: no downgrade
        "python3": "  Candidate: (none)\n"}))
    monkeypatch.setattr(rc.urllib.request, "urlopen", _urls({
        "nodejs.org": [{"version": "v23.1.0", "npm": "10.9.0"},
                       {"version": "v22.21.0", "npm": "10.9.4"}],
        "PowerShell": {"tag_name": "v7.6.8"},
        "pypi.org": {"info": {"version": "23.0.0"}}}))
    assert rc.refresh() == 0
    out = {e["key"]: e for e in json.loads((catalog / "components.json").read_text())["catalog"]}
    assert out["git"]["latest"] == "2.45.1" and out["nginx"]["latest"] == "1.99.0"
    assert out["node"]["latest"] == "22.21.0 (major upgrade - confirm first)"
    assert out["npm"]["latest"] == "10.9.4" and out["powershell"]["latest"] == "7.6.8"
    assert out["ubuntu"]["latest"] == "26.04" and "LTS" in out["ubuntu"]["channel"]
    assert out["windows"]["latest"] == "26100" and out["python3"]["latest"] == "3.12.0"
    doc = json.loads((catalog / "components.json").read_text())
    assert doc["assessment_date"] != "2026-01-01"


def test_every_probe_failure_leaves_values_alone(catalog, monkeypatch):
    def boom(*a, **k):
        raise OSError("offline")
    monkeypatch.setattr(rc, "run_hidden", boom)
    monkeypatch.setattr(rc.urllib.request, "urlopen", boom)
    before = json.loads((catalog / "components.json").read_text())["catalog"]
    assert rc.refresh() == 0
    after = json.loads((catalog / "components.json").read_text())["catalog"]
    assert before == after


def test_probe_edge_cases(monkeypatch):
    rc._dist_cache.clear()
    monkeypatch.setattr(rc.urllib.request, "urlopen", _urls({
        "nodejs.org": [{"version": "v20.1.0"}]}))
    assert rc._node_latest() == "" and rc._npm_latest() == ""      # no pinned-major release
    monkeypatch.setattr(rc, "run_hidden", lambda *a, **k: SimpleNamespace(
        stdout="no candidate line\n", stderr=""))
    assert rc._apt_candidate("x") == ""
    monkeypatch.setattr(rc, "run_hidden", lambda *a, **k: SimpleNamespace(stdout=None,
                                                                          stderr=None))
    assert rc._ubuntu_latest_lts() == ""
    assert rc._cmp("", "1.0") is None and rc._cmp("1.2", "1.2.0") == 0
    assert rc._preserve_suffix("", "1.2") == "1.2" and rc._ver(None) == ""


def test_unreadable_catalog_and_failed_write(catalog, monkeypatch):
    (catalog / "components.json").write_text("{broken")
    assert rc.refresh() == 1
    (catalog / "components.json").write_text(json.dumps({"catalog": []}))

    def boom(*a, **k):
        raise OSError("disk full")
    monkeypatch.setattr(rc.os, "replace", boom)
    assert rc.refresh() == 1
    assert not list(catalog.glob(".components.*.tmp"))             # temp file cleaned up


def test_failed_write_with_vanished_temp_file(catalog, monkeypatch):
    def boom(*a, **k):
        raise OSError("disk full")
    monkeypatch.setattr(rc.os, "replace", boom)
    monkeypatch.setattr(rc.os, "unlink", boom)
    assert rc.refresh() == 1


def test_main_configures_logging_and_runs(catalog, monkeypatch):
    seen = {}
    monkeypatch.setattr(rc.logging, "basicConfig", lambda **kw: seen.update(kw))
    monkeypatch.setattr(rc, "refresh", lambda: 0)
    assert rc.main() == 0
    assert seen["level"] == logging.INFO
    for h in seen["handlers"]:
        h.close()


def test_running_as_a_script(catalog, monkeypatch):
    import runpy
    import sys
    monkeypatch.setenv("SYSPERF_COMPONENTS_DIR", str(catalog))
    monkeypatch.setattr(logging, "basicConfig", lambda **kw: [h.close() for h in kw["handlers"]])
    monkeypatch.setattr("urllib.request.urlopen", lambda *a, **k: (_ for _ in ()).throw(
        OSError("offline")))
    monkeypatch.setattr("subprocess.run", lambda *a, **k: (_ for _ in ()).throw(OSError("x")))
    sys.modules.pop("sysperf.refresh_components_main", None)
    with pytest.raises(SystemExit) as e:
        runpy.run_module("sysperf.refresh_components", run_name="__main__")
    assert e.value.code == 0
