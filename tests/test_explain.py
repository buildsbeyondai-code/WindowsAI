"""Full-coverage tests for explain.py (explainable, measured recommendations)."""
from sysperf.explain import RISK_CLASS, expected, explain, risk_class

FIND = [{"key": "compression_mb", "label": "Memory Compression", "cause": "long_term_regression",
         "confidence": 0.85, "contribution_pct": 45.4, "scored_typical": 745.0, "target": 392,
         "explanation": "daily typical is trending the wrong way",
         "remediations": ["clear_standby", "recipes/x"]},
        {"key": "cpu_pct", "label": "CPU", "confidence": 0.5, "contribution_pct": 20.0,
         "remediations": ["background_cpu_hog", "unknown_fix"]}]
FIXES = {"clear_standby": {"title": "Clear standby", "risk": "caution", "reversible": True},
         "recipes/x": {"title": "Promoted", "risk": "manual", "reversible": False, "evolved": True},
         "background_cpu_hog": {"title": "Background the hog", "risk": "safe", "reversible": True}}
LEDGER = {"clear_standby": {"compression_mb": {"ema": 0.123, "n": 4, "wins": 3, "losses": 1}},
          "background_cpu_hog": {"cpu_pct": {"ema": 0.0, "n": 2}}}


def test_every_recommendation_answers_the_seven_questions_from_measured_data():
    recs = explain(FIND, FIXES, LEDGER, settle_s=600)
    first = recs[0]
    assert [r["key"] for r in recs] == ["compression_mb", "cpu_pct"]      # 45.4x0.85 > 20x0.5
    assert first["what"].startswith("Memory Compression: daily typical")
    assert first["evidence"] == {"typical": 745.0, "target": 392, "cause": "long_term_regression",
                                 "share_of_gap_pct": 45.4, "confidence": 0.85}
    cs, rx = first["fixes"]
    assert cs["risk"] == "Moderate" and cs["reversible"] is True
    assert cs["expected"]["text"] == ("12.3% better (recency-weighted, 4 run(s): 3 better, 1 worse)"
                                    " (recorded before 5.5.2: may include other fixes run at the same time)")
    assert rx["risk"] == "Experimental" and rx["expected"]["measured"] is False
    assert "10 min" in first["measure"] and first["priority"] == 38.59
    cpu = recs[1]
    assert cpu["what"] == "CPU: outside its target"
    assert cpu["fixes"][0]["expected"]["text"].startswith("no measurable change (recency-weighted, 2 run(s)")
    assert cpu["fixes"][1] == {"id": "unknown_fix", "title": "unknown_fix", "risk": "Advanced",
                               "reversible": False,
                               "expected": {"measured": False, "text": "not measured on this PC yet"}}
    assert explain(None, {}, {}, 0) == []


def test_risk_classes_and_worse_results_are_reported_as_measured():
    assert RISK_CLASS == {"safe": "Safe", "caution": "Moderate", "manual": "Advanced"}
    assert risk_class("safe") == "Safe" and risk_class(None) == "Advanced"
    assert risk_class("safe", evolved=True) == "Experimental"
    worse = expected({"f": {"k": {"ema": -0.05, "n": 1}}}, "f", "k")
    assert worse["text"].startswith("5.0% worse (recency-weighted, 1 run(s): 0 better, 0 worse)") and worse["losses"] == 0


def test_an_effect_is_stated_with_its_values_age_and_attribution():
    day = 86400
    e = {"ema": -0.1, "n": 1, "losses": 1, "v": 2, "last_before": 50, "last_after": 55,
         "at": 10 * day}
    r = expected({"f": {"cpu_pct": e}}, "f", "cpu_pct", now=10 * day + 60)
    assert r["text"] == ("10.0% worse (recency-weighted, 1 run(s): 0 better, 1 worse); "
                         "last run 50 → 55, measured today; only 1 run(s), not yet reliable")
    assert r["uncertain"] is True and r["stale"] is False
    assert r["legacy"] is False and r["measured_at"] == 10 * day and "group_runs" not in r
    assert ", measured 1 day ago" in expected({"f": {"k": e}}, "f", "k", now=11.5 * day)["text"]
    big = dict(e, last_before=1234.5, last_after=0.5, group_n=2)
    r = expected({"f": {"k": big}}, "f", "k", now=13 * day)
    assert "last run 1,234 → 0.5, measured 3 days ago;" in r["text"] and r["group_runs"] == 2
    old = expected({"f": {"k": dict(e, n=5)}}, "f", "k", now=50 * day)
    assert old["text"].endswith("measured 40 days ago, may be out of date")
    assert old["stale"] is True and old["uncertain"] is False
    assert "measured" not in expected({"f": {"k": e}}, "f", "k")["text"].split(";")[1]
    assert "?" in expected({"f": {"k": dict(e, last_before="x")}}, "f", "k")["text"]


def test_a_change_seen_only_with_other_fixes_is_not_claimed():
    r = expected({"f": {"k": {"n": 0, "ema": None, "group_n": 3}}}, "f", "k")
    assert r == {"measured": False, "group_only": True, "group_runs": 3,
                 "text": "no effect of its own measured yet: seen only together with "
                         "other fixes (3 batch(es))"}
