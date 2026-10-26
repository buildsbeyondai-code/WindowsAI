"""
Success-criteria scoring and the vmmemWSL trend estimator.

Pure functions: they take a `latest` sample and `history` list and return the
scorecard payload. The adaptive scorer is injected into build_scorecard so this
module has no global state.
"""
from __future__ import annotations

from . import config as _cfg
from .config import (
    CARRY_FORWARD_KEYS,
    CRITERIA,  # noqa: F401 - re-exported; tests patch scoring.CRITERIA
    MIN_TREND_SAMPLES,
    MIN_TREND_SPAN_S,
    SCORE_GAP_S,
    SCORE_SMOOTH_WINDOW,
    SCORE_WARMUP_SAMPLES,
    SMOOTH_WINDOW_OVERRIDES,
    STATIC_CRITERIA,
    TREND_GAP_S,
    TREND_WINDOW_S,
)


def _session_slice(history: list[dict], gap_s: float = SCORE_GAP_S,
                   tail: int | None = None) -> list[dict]:
    """The trailing run of `history` that belongs to the CURRENT session.

    A gap longer than `gap_s` between consecutive samples means the collector was
    not running — a reboot, a resume from sleep, or a Flask restart — so the
    samples before it describe a different machine state. Anchoring at the newest
    such gap stops the scoring median from mixing two sessions, which otherwise
    makes the scorecard grade the previous session for a whole window after every
    restart. Mirrors the re-anchoring vmmem_trend() already performs.

    ``tail`` limits the search to the last ``tail`` samples (the result is then
    at most that long); callers that only need the newest few samples use it.
    Scans newest-first: the first gap found is the newest one.
    """
    n = len(history)
    lo = 1 if tail is None else max(1, n - tail + 1)
    nxt = history[-1].get("_t") if n else None
    for i in range(n - 1, lo - 1, -1):
        t0 = history[i - 1].get("_t")
        if t0 is not None and nxt is not None and nxt - t0 > gap_s:
            return history[i:]
        nxt = t0
    return history[lo - 1:]


def session_sample_count(history: list[dict]) -> int:
    """How many trailing samples belong to the current session."""
    return len(_session_slice(history))


def _smoothed_current(history: list[dict], key: str, latest_val,
                      window: int = SCORE_SMOOTH_WINDOW):
    """Median of the last `window` non-null current-session samples for `key`.

    Scoring a naturally-fluctuating count metric (browser/handle/process counts)
    on the single latest reading makes the score jitter and collapse on every
    upward breath. The median of a short recent window reflects the *sustained*
    state without hiding a real, persistent regression (a genuine shift moves the
    median within one window). Falls back to the latest value when there is not
    enough history or smoothing is disabled (window <= 1).

    Only samples from the current session are eligible — see _session_slice.
    """
    if not window or window <= 1:
        return latest_val
    scoped = _session_slice(history, tail=window)[-window:]
    # Score the WORKING session. While the machine is locked, OneDrive syncs on
    # purpose (the lock policy starts it) and Windows runs idle maintenance;
    # counting those samples penalised the user for the dashboard's own design.
    # Locked samples are used only when there is nothing else to go on.
    active = [s for s in scoped if not s.get("session_locked")]
    vals = [s.get(key) for s in (active or scoped) if s.get(key) is not None]
    if not vals:
        return latest_val
    vals.sort()
    n = len(vals)
    mid = n // 2
    return vals[mid] if n % 2 else (vals[mid - 1] + vals[mid]) / 2.0


def progress_pct(baseline: float, current: float, target: float) -> float:
    """Fraction of the baseline->target journey completed, clamped to 0..100."""
    if baseline == target:
        return 100.0
    return max(0.0, min(100.0, (current - baseline) / (target - baseline) * 100.0))


def score_vmmem(current: float, cap: float, trend: float,
                span: float, n: int) -> tuple:
    """
    vmmemWSL is a compound CEILING criterion: '<= cap AND falling'.
    The generic baseline->target formula does not apply: the cap (10,240 MB) sits
    far above the baseline (5,627 MB), so dropping below baseline would score
    negative. What matters is being under the cap and not growing.

    Returns (progress|None, status, note).
    """
    if current > cap:
        return 0.0, "regress", f"above the {cap:,.0f} MB cap"
    if n < MIN_TREND_SAMPLES or span < MIN_TREND_SPAN_S:
        need = max(0, int((MIN_TREND_SPAN_S - span) / 60) + 1)
        return None, "measuring", (
            f"under cap; slope needs ~{need} more min of samples "
            f"(have {n} over {span / 60:.0f} min)")
    mins = f"over the last {span / 60:.0f} min"
    util = (current / cap) if cap else 0.0
    if trend <= 0:
        return 100.0, "met", (
            f"under cap and {'falling' if trend < 0 else 'flat'} "
            f"({trend:+,.0f} MB/h {mins})")
    # Under the cap but rising: judge the risk by how much runway remains, not by
    # the mere existence of a positive slope. A slow rise from a low working set is
    # normal churn (working sets breathe), not a leak - only penalise when the cap
    # is genuinely threatened (near it, or projected to reach it soon).
    room = cap - current
    hrs_to_cap = (room / trend) if trend > 0 else 1e9
    if hrs_to_cap >= 12 and util < 0.5:
        return 100.0, "met", (
            f"under cap; rising only {trend:+,.0f} MB/h {mins} but ~{hrs_to_cap:.0f} h "
            f"of headroom at {util * 100:.0f}% of cap - not a leak")
    if hrs_to_cap >= 3:
        prog = max(50.0, min(95.0, hrs_to_cap / 12 * 100))
        return prog, "partial", (
            f"under cap, rising {trend:+,.0f} MB/h {mins}; ~{hrs_to_cap:.0f} h to cap")
    prog = max(0.0, hrs_to_cap / 3 * 40)
    return prog, "regress", (f"under cap but RISING {trend:+,.0f} MB/h {mins} - "
                             f"~{hrs_to_cap:.1f} h to cap, autoMemoryReclaim not in effect")


def vmmem_trend(history: list[dict]) -> tuple:
    """
    Least-squares slope of vmmemWSL in MB/hour.

    Guards:
      * Only the last TREND_WINDOW_S of samples are used.
      * A minimum elapsed span is required (working-set trims otherwise
        extrapolate to nonsense rates).
      * The window is re-baselined at a genuine WSL restart / working-set reset
        (collector gap, or a collapse from a high steady state) so the
        post-shutdown recovery ramp does not create a phantom "rising" slope.
        It is NOT re-anchored merely because vmmem is low — with effective
        autoMemoryReclaim the working set normally sits well below the cap.

    Returns (slope_mb_per_h, span_seconds, n_points).
    """
    newest = None
    for s in reversed(history):
        if s.get("vmmem_mb"):
            nt = s.get("_t")
            if nt:
                newest = nt
                break
    if newest is None:
        return 0.0, 0.0, 0
    win = TREND_WINDOW_S
    pts = [(t, v) for s in history
           if (v := s.get("vmmem_mb")) and (t := s.get("_t")) and newest - t <= win]

    # Read at call time: the cap is sized to this machine's RAM once captured.
    collapse_from = _cfg.VMMEM_CAP_MB * 0.45
    anchor = 0
    if pts:
        gap_s = TREND_GAP_S
        pt, pv = pts[0]
        for i in range(1, len(pts)):
            ct, cv = pts[i]
            if ct - pt > gap_s or (pv >= collapse_from and cv < pv * 0.5):
                anchor = i
            pt, pv = ct, cv
    if anchor:
        pts = pts[anchor:]

    n = len(pts)
    if n < 2:
        return 0.0, 0.0, n
    span = pts[-1][0] - pts[0][0]
    if n < MIN_TREND_SAMPLES or span < MIN_TREND_SPAN_S:
        return 0.0, span, n
    t0 = pts[0][0]
    xs = [(t - t0) / 3600.0 for t, _ in pts]
    ys = [v for _, v in pts]
    mx, my = sum(xs) / n, sum(ys) / n
    den = sum([(x - mx) ** 2 for x in xs])
    if den == 0:                       # pragma: no cover - unreachable: span>=600s implies distinct x
        return 0.0, span, n
    slope = sum([(x - mx) * (y - my) for x, y in zip(xs, ys)]) / den
    return slope, span, n


def _row_note(key: str, cur, latest: dict) -> str | None:
    """Human-readable supplementary note for specific criteria."""
    if key == "startup_apps" and latest.get("startup_apps_total") is not None:
        return (f"{cur} enabled of {latest['startup_apps_total']} registered "
                "auto-start entries (disabled entries excluded)")
    if key == "net_remote" and latest.get("net_established") is not None:
        raw = latest.get("net_remote_raw")
        hosts = latest.get("net_remote_hosts")
        extra = ""
        if latest.get("net_remote_infra") is not None:
            extra += (f"; {latest['net_remote_infra']} OS/sync infrastructure "
                      "(OneDrive, Windows Update, Defender, VPN) excluded")
        if raw is not None:
            extra += f"; {raw} instantaneous (transient <8s bursts excluded)"
        if hosts is not None:
            extra += f"; {hosts} distinct remote hosts"
        return (f"{cur} sustained remote of {latest['net_established']} established "
                f"({latest.get('net_loopback', 0)} loopback IPC excluded){extra}; "
                "target sized from this machine's own starting point")
    if key == "committed_app_mb":
        floor = latest.get("committed_system_mb")
        ctr = latest.get("committed_mb")
        if floor is not None and ctr is not None:
            return (f"{cur:,} MB actionable app commit ({floor:,} MB OS/security/WSL floor "
                    f"excluded; {ctr:,} MB raw commit charge counter)")
        return None
    if key == "handles":
        tot = latest.get("handles_total")
        sysh = latest.get("handles_system")
        if tot is not None and sysh is not None:
            return (f"{cur:,} actionable app handles ({sysh:,} OS/kernel/security-agent "
                    f"floor excluded as non-actionable; {tot:,} total system-wide)")
        return None
    if key == "hw_faults":
        pd = latest.get("hw_problem_devices") or 0
        ud = latest.get("hw_disks_unhealthy") or 0
        return ("all devices report OK; no unhealthy disks" if cur == 0
                else f"{pd} device(s) with error codes, {ud} unhealthy disk(s)")
    return None


# How far past its target a row may drift before it scores 0%, as a fraction of
# the target. Replaces a cliff where one unit over a target scored the same 0%
# as being ten times over it.
SLIP_ZERO_FRACTION = 0.5


def slipped_progress(scored: float, target: float, hib: bool) -> float:
    """Graded score for a row that has drifted past a target it once met."""
    if not target:
        return 0.0                        # a zero target (faults) has no band
    span = abs(target) * SLIP_ZERO_FRACTION
    over = (target - scored) if hib else (scored - target)
    return max(0.0, min(100.0, 100.0 * (1 - over / span)))


def build_scorecard(latest: dict, history: list[dict], scorer,
                    target_overrides: dict | None = None,
                    live_targets: dict | None = None) -> dict:
    """Assemble the full scorecard payload, including adaptive enrichment.

    ``target_overrides`` (key -> target) lets the automatic target-realism layer
    (target_review.py) substitute an honest, telemetry-derived goal for one the
    machine has proven it can never reach. CRITERIA stays the plan default; an
    override only ever replaces the goal for a single row and is surfaced via the
    row's ``plan_target`` / ``target_source`` fields.
    """
    rows, progs = [], []
    # Live targets (recomputed from the current sample, e.g. the browser floor of
    # whatever is open now) take precedence over a static captured number.
    live = {k: v for k, v in (live_targets or {}).items() if v is not None}
    overrides = {**(target_overrides or {}), **live}
    if latest.get("hw_problem_devices") is not None:
        latest["hw_faults"] = ((latest.get("hw_problem_devices") or 0)
                               + (latest.get("hw_disks_unhealthy") or 0))
    trend, span, npts = vmmem_trend(history)

    # Read through the module so this machine's per-machine CRITERIA (and a
    # reloaded config) are always the ones scored.
    for key, label, base, plan_target, hib, unit in _cfg.CRITERIA:
        cur = latest.get(key)
        if cur is None:
            continue
        # An auto-decided honest goal (if any) replaces the plan goal for scoring.
        target = overrides.get(key, plan_target)

        if key in _cfg.PROFILE_PENDING:
            # No baseline for THIS machine yet: show the reading, grade nothing.
            rows.append({
                "key": key, "label": label, "baseline": None, "current": cur,
                "scored_typical": cur, "target": None, "unit": unit,
                "higher_is_better": hib, "plan_target": None,
                "target_source": "plan", "change_pct": None, "progress": None,
                "room": None, "status": "measuring",
                "note": "capturing this machine's baseline (first few samples)",
                "shortfall": 0,
            })
            continue

        if key == "vmmem_mb":
            # vmmem has its own trend-aware scorer that already handles noise.
            change = (cur - base) / base * 100.0 if base else 0.0
            prog, status, note = score_vmmem(cur, target, trend, span, npts)
            met = status == "met"
            scored = cur
        else:
            # Score the SUSTAINED state (rolling median), not the latest noisy
            # sample, so momentary breathing does not collapse the score.
            # Precedence:
            #  1) an explicit per-metric override window (used for bursty,
            #     sparsely-sampled carry-forward metrics like net_remote that
            #     need a long window spanning many refreshes);
            #  2) other carry-forward/step metrics -> raw latest (a short median
            #     would only return a stale prior step);
            #  3) everything else -> default short-window median.
            ovr = SMOOTH_WINDOW_OVERRIDES.get(key)
            if ovr is not None:
                scored = _smoothed_current(history, key, cur, ovr)
            elif key in CARRY_FORWARD_KEYS:
                scored = cur
            else:
                scored = _smoothed_current(history, key, cur)
            change = (scored - base) / base * 100.0 if base else 0.0
            prog = progress_pct(base, scored, target)
            met = (scored >= target) if hib else (scored <= target)
            if met:
                # A row inside its target is done, even when this machine started
                # better than the goal (progress_pct would read that as ~0%).
                prog = 100.0
            elif key in live or ((base >= target) if hib else (base <= target)):
                # Past the target, and either the target is live (the captured
                # baseline described a different set of open apps, so a journey
                # from it means nothing) or this machine started on the good side
                # of it (progress_pct's journey would run backwards and clamp to
                # 100%). Grade by how far past the target it is.
                prog = slipped_progress(scored, target, hib)
            worsened = (change < 0) if hib else (change > 0)
            if key in live and not met:
                worsened = prog == 0.0
            status = "met" if met else ("regress" if worsened else "partial")
            note = _row_note(key, cur, latest)

        rows.append({
            "key": key, "label": label, "baseline": base, "current": cur,
            "scored_typical": round(scored, 1),
            "target": target, "unit": unit, "higher_is_better": hib,
            "plan_target": plan_target,
            "target_source": ("live" if key in live
                              else "auto" if key in overrides else "plan"),
            "change_pct": round(change, 1),
            "progress": None if prog is None else round(prog, 1),
            "room": None if prog is None else round(100 - prog, 1),
            "status": status, "note": note,
            "shortfall": 0 if met else round(abs(scored - target), 1),
        })
        if prog is not None:
            progs.append(prog)

    # STATIC_CRITERIA (Intel-boot) is informational and deliberately excluded.
    overall = sum(progs) / len(progs) if progs else 0.0
    session_n = session_sample_count(history)

    for r in rows:
        if r["status"] == "measuring" and r["baseline"] is None:
            continue
        a = scorer.enrich(r["key"], r["current"], r["baseline"],
                          r["target"], r["higher_is_better"])
        if a is not None:
            r["adaptive"] = a

    return {
        "rows": rows,
        "static": STATIC_CRITERIA,
        "overall_improvement": round(overall, 1),
        "overall_room": round(100 - overall, 1),
        "adaptive": scorer.summary(rows),
        "scored": len(progs),
        "total_criteria": len(rows),
        "vmmem_trend": round(trend, 1),
        "vmmem_trend_span_min": round(span / 60, 1),
        "vmmem_trend_points": npts,
        # Scoring provenance: how many samples of the CURRENT session the medians
        # were drawn from. Below SCORE_WARMUP_SAMPLES the score is honest but thin
        # (post-reboot), so the UI labels it as stabilising instead of settled.
        "session_samples": session_n,
        "smooth_window": SCORE_SMOOTH_WINDOW,
        "warming_up": session_n < SCORE_WARMUP_SAMPLES,
        "counts": {
            "met": sum(1 for r in rows if r["status"] == "met"),
            "partial": sum(1 for r in rows if r["status"] == "partial"),
            "regress": sum(1 for r in rows if r["status"] == "regress"),
            "measuring": sum(1 for r in rows if r["status"] == "measuring"),
        },
    }
