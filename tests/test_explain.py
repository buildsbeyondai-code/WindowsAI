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
    assert cs["expected"]["text"] == "12.3% better on average over 4 measured run(s)"
    assert rx["risk"] == "Experimental" and rx["expected"]["measured"] is False
    assert "10 min" in first["measure"] and first["priority"] == 38.59
    cpu = recs[1]
    assert cpu["what"] == "CPU: outside its target"
    assert cpu["fixes"][0]["expected"]["text"] == "no measurable change over 2 run(s)"
    assert cpu["fixes"][1] == {"id": "unknown_fix", "title": "unknown_fix", "risk": "Advanced",
                               "reversible": False,
                               "expected": {"measured": False, "text": "not measured on this PC yet"}}
    assert explain(None, {}, {}, 0) == []


def test_risk_classes_and_worse_results_are_reported_as_measured():
    assert RISK_CLASS == {"safe": "Safe", "caution": "Moderate", "manual": "Advanced"}
    assert risk_class("safe") == "Safe" and risk_class(None) == "Advanced"
    assert risk_class("safe", evolved=True) == "Experimental"
    worse = expected({"f": {"k": {"ema": -0.05, "n": 1}}}, "f", "k")
    assert worse["text"] == "5.0% worse on average over 1 measured run(s)" and worse["losses"] == 0
