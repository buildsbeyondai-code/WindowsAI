"""Full-coverage tests for version_check.py (live latest versions)."""
import json

from sysperf import version_check as vc

FEEDS = {
    "https://api.github.com/repos/microsoft/WSL/releases/latest": {"tag_name": "3.0.3"},
    "https://api.github.com/repos/git-for-windows/git/releases/latest":
        {"tag_name": "v2.56.0.windows.1"},
    "https://pypi.org/pypi/flask/json": {"info": {"version": "3.1.3"}},
    "https://registry.npmjs.org/@anthropic-ai/claude-code/latest": {"version": "2.1.310"},
    "https://endoflife.date/api/python.json": [{"cycle": "3.15", "latest": "3.15.1"},
                                               {"cycle": "3.14", "latest": "3.14.6"}],
    "https://edgeupdates.microsoft.com/api/products": [
        {"Product": "Beta", "Releases": [{"Platform": "Windows", "Architecture": "x64",
                                          "ProductVersion": "157.0.1.1"}]},
        {"Product": "Stable", "Releases": [
            {"Platform": "Windows", "Architecture": "x64", "ProductVersion": "156.0.4314.42"},
            {"Platform": "Windows", "Architecture": "x64", "ProductVersion": "155.0.9.9"},
            {"Platform": "MacOS", "Architecture": "universal", "ProductVersion": "160.0.0.0"}]}],
}
META_LTS = ("Dist: jammy\nVersion: 22.04.5 LTS\nSupported: 1\n\n"
            "Dist: noble\nVersion: 24.04.3 LTS\nSupported: 1\n\n"
            "Dist: resolute\nVersion: 26.04.1 LTS\nSupported: 1\n\n"
            "Dist: next\nVersion: 28.04 LTS\nSupported: 0\n")


def _get(url):
    if url.endswith("meta-release-lts"):
        return META_LTS
    if url not in FEEDS:
        raise OSError(f"offline: {url}")
    return json.dumps(FEEDS[url])


def test_each_feed_parser_reads_its_real_response_shape():
    assert vc.github("microsoft/WSL")(_get) == "3.0.3"
    assert vc.github("git-for-windows/git")(_get) == "2.56.0"
    assert vc.pypi("flask")(_get) == "3.1.3"
    assert vc.npm("@anthropic-ai/claude-code")(_get) == "2.1.310"
    assert vc.python_release(_get) == "3.15.1"
    assert vc.ubuntu_lts(_get) == "26.04.1"              # newest *supported* LTS
    assert vc.edge_stable(_get) == "156.0.4314.42"       # Stable, Windows x64 only
    assert set(vc.SOURCES) >= {"wsl", "powershell", "ubuntu", "edge", "webview2", "python"}


def test_refresh_keeps_previous_answers_when_a_feed_fails(tmp_path):
    t = [1000.0]
    calls = []
    edge = lambda get: calls.append("edge") or vc.edge_stable(get)   # noqa: E731
    lv = vc.LatestVersions(tmp_path / "l.json", get=_get, clock=lambda: t[0], sources={
        "wsl": vc.github("microsoft/WSL"), "edge": edge, "webview2": edge,
        "powershell": vc.github("PowerShell/PowerShell"), "empty": lambda get: ""})
    assert lv.due()
    (tmp_path / "l.json").write_text(json.dumps({"latest": {"powershell": "7.6.7"}}))
    d = lv.refresh()
    assert d["latest"] == {"wsl": "3.0.3", "edge": "156.0.4314.42",
                           "webview2": "156.0.4314.42", "powershell": "7.6.7"}
    assert "powershell" in d["errors"] and calls == ["edge"]     # shared feed asked once
    assert not lv.due()
    t[0] += vc.REFRESH_S
    assert lv.due()
    assert not vc.LatestVersions(tmp_path / "l.json", enabled=False).due()


def test_load_tolerates_missing_or_bad_files(tmp_path):
    lv = vc.LatestVersions(tmp_path / "l.json")
    assert lv.load() == {}
    (tmp_path / "l.json").write_text("[1]")
    assert lv.load() == {}
    (tmp_path / "l.json").write_text("{bad")
    assert lv.load() == {}
