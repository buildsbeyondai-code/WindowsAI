"""Benchmarks for source self-evolution. PROTECTED: candidates may not edit it.

codevolve runs these inside the sandbox (read-only tree, no network, HOME=/tmp)
for the baseline and for each candidate, and only retains a candidate that is
measurably faster without regressing any of them. Inputs are synthetic but
shaped like the collector's real history (MAX_SAMPLES samples, one per minute,
every CRITERIA key present) and fully deterministic, so a change in timing is a
change in the code, not in the data.
"""
import os
import sys
import tempfile
from pathlib import Path

sys.path.insert(0, os.path.join(os.path.dirname(os.path.abspath(__file__)), "src"))

from sysperf import appctl, config, scoring, trends    # noqa: E402
from sysperf.adaptive import AdaptiveScorer             # noqa: E402

_TMP = Path(tempfile.mkdtemp(prefix="evo_bench_"))
_KEYS = [c[0] for c in config.CRITERIA]


def _sample(i: int) -> dict:
    s = {k: float(100 + (i * 37 + j * 11) % 400) for j, k in enumerate(_KEYS)}
    s.update(_t=1_790_000_000.0 + i * 60.0, vmmem_mb=3000.0 + (i * 13) % 900,
             _iso=f"2026-10-{1 + i // 1440 % 28:02d}T{i // 60 % 24:02d}:{i % 60:02d}:00",
             session_locked=(i % 17 == 0),
             ncpu=8, cpu_procs=[{"n": f"app{j}", "pid": 1000 + j, "ws_mb": 50 + j,
                                 "cpu_s": i * (j % 9) * 12.0,
                                 "exe": f"C:\\Apps\\app{j}.exe" if j % 3 else None}
                                for j in range(60)])
    return s


_HIST = [_sample(i) for i in range(config.MAX_SAMPLES)]
_LONG = [_sample(i) for i in range(config.MAX_SAMPLES * 12)]     # two days of samples
_LATEST = _HIST[-1]
_SCORER = AdaptiveScorer(_TMP / "adaptive.json")
_SCORER.seed(_HIST)
_CARD = scoring.build_scorecard(_LATEST, _HIST, _SCORER)


def _scorecard():
    for _ in range(200):
        scoring.build_scorecard(_LATEST, _HIST, _SCORER)


def _vmmem_trend():
    for _ in range(50):
        scoring.vmmem_trend(_LONG)


def _smoothed_current():
    for k in _KEYS:
        for _ in range(60):
            scoring._smoothed_current(_HIST, k, _LATEST[k])


def _trend_ingest_summary():
    st = trends.TrendStore(_TMP / "absent" / "trends.json")
    for s in _LONG:
        st.ingest(s, _CARD, persist=False)
    st.summary(_LATEST)


def _top_apps():
    for _ in range(15):
        appctl.top_apps(_HIST)


# Keys name the code each benchmark exercises; the planner is told to target it.
BENCHMARKS = {
    "scoring.build_scorecard": _scorecard,
    "scoring.vmmem_trend": _vmmem_trend,
    "scoring._smoothed_current": _smoothed_current,
    "trends.TrendStore.ingest+summary": _trend_ingest_summary,
    "appctl.top_apps (cpuwatch.consistent_cpu, last_exe)": _top_apps,
}
