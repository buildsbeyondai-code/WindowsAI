"""Full-coverage tests for browser.py."""
from sysperf.browser import browser_breakdown


def test_breakdown_full():
    latest = {
        "wv2_hosts": {"ShellExperienceHost": 10, "OneDrive": 5, "SearchHost": 4,
                      "ms-teams": 3, "OUTLOOK": 2, "SomeOther": 6},
        "edge_procs": 14, "edge_main": 1, "edge_utility": 7,
        "edge_renderer": 9, "edge_extension": 6, "edge_ext_mb": 552 * 1024 * 1024,
        "browser_procs": 71,
    }
    b = browser_breakdown(latest)
    assert b["mandatory_total"] == 19          # 10+5+4
    assert b["inuse_total"] == 5               # 3+2
    assert b["other_total"] == 6
    assert b["hard_floor"] == 30
    assert b["edge_split"]["extension"] == 6
    assert set(b["host_labels"]) >= {"ShellExperienceHost", "ms-teams"}


def test_breakdown_empty_defaults():
    b = browser_breakdown({})
    assert b["mandatory_total"] == 0 and b["current_total"] == 0
    assert b["edge_total"] == 0
    # edge_floor uses defaults main=1, utility=7 -> +3+2
    assert b["realistic_floor"] == 13
