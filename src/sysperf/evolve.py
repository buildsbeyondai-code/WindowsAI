#!/usr/bin/env python3
"""
Self-authoring evolution layer (APE) for the auto-improve loop.

The loop in :mod:`autoimprove` is *adaptive*: it learns which of the catalogue's
remediations work and stops choosing the ones that don't. What it could not do is
gain a capability it was not shipped with. Five metrics on this machine
(``cpu_pct``, ``cpu_queue``, ``disk_active_pct``, ``hw_faults``,
``browser_procs``) have no remediation mapped at all, so a shortfall there could
only ever be escalated — for ever, no matter how much evidence accumulated.

This module closes that gap. Every ``EVOLVE_EVERY`` cycles an APE pass reads the
accumulated evidence and produces one of five evolutions:

===========  ====================================================  ============
kind         trigger                                               applied
===========  ====================================================  ============
``mapping``  a fix measurably moved a metric it was not applied    automatically
             for -> that (fix, metric) pair becomes selectable
``recipe``   a metric breached N times with nothing mapped ->      human only
             a vetted recipe is parameterised as a *candidate*
``shard``    a metric accumulated N shortfalls with no rules ->    automatically
             a knowledge shard (rules .md) is generated
``cluster``  verification outcomes reveal which family of fix      automatically
             actually resolves which attributed cause
``proposal`` no recipe can express the change (hardware, policy,   human only
             an unreachable target) -> a structural proposal
===========  ====================================================  ============

Safety invariants
-----------------
These are the reason this module is safe to run unattended, and each one has a
test asserting it:

1. **No command synthesis.** A generated remediation is always a parameterised
   entry from :data:`RECIPE_LIBRARY`. The engine never builds a command string
   from free text, model output, or log content. Evolution selects and
   parameterises; it does not author shell.
2. **Candidates cannot run.** A generated recipe is created with
   ``state="candidate"`` and is not executable. It becomes eligible only after an
   explicit human promotion, and even then it still passes the loop's existing
   risk gate, cooldown and peak-hour rules — promotion widens the choice, it does
   not bypass policy.
3. **Bounded.** Hard caps on recipes, shards, clusters and evolutions per pass,
   so a pathological log cannot grow the state without limit.
4. **Confined writes.** Only ``evolve_state.json`` and files under
   ``evolutions/`` are written. Engine code (``fixes.py``, ``rca.py``,
   ``autoimprove.py``) is never modified.
5. **Reversible.** Every evolution records the files it created and the registry
   keys it added, so :meth:`EvolutionEngine.rollback_last` undoes it.
"""
from __future__ import annotations

import json
import re
import time
from collections import Counter, defaultdict
from pathlib import Path

STATE_VERSION = 1

# APE cadence, in loop cycles. The loop scans every 5 minutes, so 8 cycles is
# roughly 40 minutes — often enough to react within a session, rare enough that a
# single noisy hour cannot churn the registry.
EVOLVE_EVERY = 8

# A metric must breach this many times, with nothing mapped to it, before the
# engine proposes a recipe. Below this it is noise, not a pattern.
RECIPE_THRESHOLD = 8
# Shortfalls needed before a metric earns a generated knowledge shard.
SHARD_THRESHOLD = 8
# A (fix, metric) pair needs this many measured improvements it was not applied
# for before the pairing is trusted enough to widen selection.
MAPPING_THRESHOLD = 2
# A cause -> family association needs this much evidence to be recorded.
CLUSTER_THRESHOLD = 3

MAX_RECIPES = 20
MAX_SHARDS = 20
MAX_PROPOSALS = 20
MAX_EVOLUTIONS_PER_PASS = 5
LOG_KEEP = 200

# --------------------------------------------------------------------------- #
# Vetted recipe library                                                       #
# --------------------------------------------------------------------------- #
# The complete set of actions evolution is allowed to propose. Each entry is a
# fixed, reviewed recipe: evolution chooses among them and fills in the declared
# parameters from measured evidence. Adding a capability here is a deliberate
# human act — which is exactly the point.
#
# ``risk`` follows fixes.py: safe | caution | manual. Recipes that change
# persistent machine configuration are deliberately 'manual' even when they are
# reversible, because the user should see them before they happen.
RECIPE_LIBRARY = {
    "cap_cpu_offenders": {
        "title": "Lower the priority of a sustained CPU consumer",
        "family": "cpu",
        "metrics": ("cpu_pct", "cpu_queue"),
        "risk": "caution",
        "reversible": True,
        "shell": "pwsh",
        "params": ("process",),
        "rationale": "A process that holds a consistent share of CPU across many "
                     "intervals is a scheduling problem, not a spike. Dropping its "
                     "priority frees the run queue without killing the user's work.",
        "manual_hint": "Get-Process {process} | ForEach-Object "
                       "{{ $_.PriorityClass = 'BelowNormal' }}",
    },
    "trim_browser_procs": {
        "title": "Enable browser process consolidation",
        "family": "browser",
        "metrics": ("browser_procs", "committed_app_mb"),
        "risk": "manual",
        "reversible": True,
        "shell": "pwsh",
        "params": (),
        "rationale": "Browser process count is driven by per-site isolation "
                     "policy. Consolidation is a policy change the user should "
                     "make knowingly, because it trades isolation for memory.",
        "manual_hint": "Set the browser's process-per-site policy, or reduce the "
                       "number of open profiles and pinned tabs.",
    },
    "throttle_disk_indexer": {
        "title": "Throttle the search indexer while disk is saturated",
        "family": "disk",
        "metrics": ("disk_active_pct",),
        "risk": "caution",
        "reversible": True,
        "shell": "pwsh",
        "params": (),
        "rationale": "Sustained disk-active time with no user I/O is usually the "
                     "indexer or a background scan. Throttling it is reversible "
                     "and does not lose data.",
        "manual_hint": "Stop-Service WSearch   # re-enable with Start-Service WSearch",
    },
    "report_device_faults": {
        "title": "Report Device-Manager faults for review",
        "family": "hardware",
        "metrics": ("hw_faults",),
        "risk": "manual",
        "reversible": True,
        "shell": "pwsh",
        "params": (),
        "rationale": "A device fault cannot be cleared by the dashboard. The "
                     "useful automated act is to surface exactly which devices "
                     "are faulted so a human can act once.",
        "manual_hint": "Get-PnpDevice | Where-Object Status -ne 'OK'",
    },
    "cap_wsl_memory": {
        "title": "Cap WSL memory in .wslconfig",
        "family": "wsl",
        "metrics": ("vmmem_mb", "available_mb"),
        "risk": "manual",
        "reversible": True,
        "shell": "pwsh",
        "params": ("limit_gb",),
        "rationale": "vmmemWSL grows to its configured ceiling. Lowering the "
                     "ceiling is persistent configuration, so it needs a restart "
                     "and the user's knowledge.",
        "manual_hint": "Set memory={limit_gb}GB in %USERPROFILE%\\.wslconfig, "
                       "then wsl --shutdown",
    },
}

# Attributed cause -> the recipe families that have historically resolved it.
# This is the *seed*; the cluster evolution rewrites it from measured outcomes.
SEED_CLUSTERS = {
    "usage_driven": ("cpu", "browser"),
    "long_term_regression": ("wsl", "disk"),
    "steady_shortfall": ("wsl", "hardware"),
    "transient_spike": ("cpu",),
}

# Causes that no recipe can address: the target is below the machine's floor, so
# the honest output is a structural proposal, not an action.
STRUCTURAL_CAUSES = ("chronic_unreachable",)


def _slug(text: str) -> str:
    """A filesystem- and id-safe token. Bounds length so a long metric label
    cannot produce an unbounded filename."""
    return re.sub(r"[^a-z0-9_]+", "_", str(text).lower()).strip("_")[:48]


def _iso(stamp: float | None = None) -> str:
    return time.strftime("%Y-%m-%dT%H:%M:%S+00:00",
                         time.gmtime(time.time() if stamp is None else stamp))


class EvolutionEngine:
    """Generates new capability for the auto-improve loop from its own evidence.

    ``learning_view`` supplies the derived evidence (strategies, knowledge sets,
    cycle history). ``remediation_map`` is the catalogue's metric -> fix-id map,
    read only. ``state_file`` and ``evolutions_dir`` are the *only* paths written.
    """

    def __init__(self, learning_view, remediation_map, state_file,
                 evolutions_dir, evolve_every: int = EVOLVE_EVERY,
                 now=time.time, context=None, on_promote=None,
                 auto_adopt: bool = False) -> None:
        self.view = learning_view
        self.remediation_map = dict(remediation_map or {})
        self.state_file = Path(state_file)
        self.evolutions_dir = Path(evolutions_dir)
        self.evolve_every = int(evolve_every)
        self.now = now
        # When True the engine decides each candidate itself from evidence (see
        # auto_adjudicate) instead of asking the user to Promote/Reject. The user
        # never reviews self-authored proposals.
        self.auto_adopt = bool(auto_adopt)
        # Optional callable returning live evidence the loop's state does not
        # carry (currently the consistent-CPU table). Without it a recipe whose
        # parameter can only come from that table would be generated permanently
        # incomplete, and therefore permanently unpromotable.
        self.context = context
        # Called with promoted_fixes() after a promotion, so the host can register
        # them with the FixEngine without this module importing it.
        self.on_promote = on_promote
        self.state = self._default_state()
        self._load()
        if self.auto_adopt:
            self.auto_adjudicate()

    def auto_adjudicate(self) -> list:
        """Decide every pending candidate from evidence instead of asking a human.

        A self-generated recipe is ADOPTED only when it fills a genuine gap — the
        metric has no mapped remediation at all — and the recipe is reversible and
        low-risk (safe/caution) and complete. Anything redundant (the metric
        already has a fix) or needing human judgement (risk=manual) is HELD, so it
        never appears as a task for the user. Adoption still never makes a recipe
        self-executing (Invariant 2): a promoted recipe is a documented
        recommendation the loop surfaces and skips.
        """
        if not self.auto_adopt:
            return []
        decided = []
        for cid, r in list(self.state["recipes"].items()):
            if r.get("state") != "candidate":
                continue
            metric = r.get("metric")
            fills_gap = not self.remediation_map.get(metric)
            low_risk = r.get("risk") in ("safe", "caution")
            adopt = (fills_gap and bool(r.get("reversible")) and low_risk
                     and not r.get("missing_params"))
            if adopt:
                self.promote(cid)
                decided.append({"id": cid, "metric": metric, "decision": "adopted"})
            else:
                reason = ("redundant — the metric already has a mapped fix"
                          if not fills_gap else
                          "needs human judgement" if not low_risk else
                          "not reversible / incomplete")
                r["state"] = "held"
                r["executable"] = False
                r["held_reason"] = reason
                decided.append({"id": cid, "metric": metric,
                                "decision": "held", "reason": reason})
        if decided:
            self._save()
        return decided

    # ----------------------------------------------------------------- state
    @staticmethod
    def _default_state() -> dict:
        return {
            "version": STATE_VERSION,
            "generation": 0,
            "cycles_seen": 0,
            "passes": 0,
            "last_pass_at": None,
            "last_pass_cycle": 0,
            "recipes": {},        # recipe id -> candidate/promoted record
            "shards": {},         # metric key -> generated shard record
            "mappings": {},       # metric key -> [fix id, ...] learned pairings
            "clusters": {},       # cause -> [family, ...]
            "proposals": {},      # proposal id -> record
            "log": [],            # newest-last evolution audit entries
        }

    def _load(self) -> None:
        try:
            data = json.loads(self.state_file.read_text(encoding="utf-8"))
        except (OSError, ValueError):
            return
        if isinstance(data, dict):
            for key, value in data.items():
                if key in self.state and isinstance(value, type(self.state[key])):
                    self.state[key] = value

    def _save(self) -> None:
        """Atomic write: the API reads this file, a torn write would read as a
        corrupt state and silently reset the generation counter."""
        self.state_file.parent.mkdir(parents=True, exist_ok=True)
        tmp = self.state_file.with_suffix(self.state_file.suffix + ".tmp")
        tmp.write_text(json.dumps(self.state, indent=1), encoding="utf-8")
        tmp.replace(self.state_file)

    def _log(self, kind: str, summary: str, files: list | None = None,
             registry: list | None = None) -> dict:
        entry = {
            "at": _iso(self.now()),
            "generation": self.state["generation"],
            "kind": kind,
            "summary": summary,
            "files": [str(f) for f in (files or [])],
            "registry": list(registry or []),
            "reversible": True,
        }
        self.state["log"].append(entry)
        del self.state["log"][:-LOG_KEEP]
        return entry

    # ------------------------------------------------------------------ tick
    def tick(self, cycles: int | None = None) -> bool:
        """Advance the cycle counter. True when an APE pass is due.

        Called once per loop cycle. Cheap by design: the expensive derivation only
        happens on the cycle that actually triggers a pass.
        """
        self.state["cycles_seen"] = (int(cycles) if cycles is not None
                                     else self.state["cycles_seen"] + 1)
        due = (self.state["cycles_seen"] - self.state["last_pass_cycle"]) \
            >= self.evolve_every
        self._save()
        return due

    # -------------------------------------------------------------- evidence
    def _evidence(self) -> dict:
        """Everything a pass reasons over, derived once."""
        cycles = self.view._cycles()
        state = self.view._state()
        strategies = self.view.strategies(state)
        knowledge = self.view.knowledge_sets(state, strategies)

        # shortfalls and causes per metric, from the RCA findings in each cycle
        shortfalls = Counter()
        causes = defaultdict(Counter)
        labels = {}
        for rec in cycles:
            for finding in rec.get("findings") or []:
                if not isinstance(finding, dict):
                    continue
                key = str(finding.get("key") or "")
                if not key:
                    continue
                shortfalls[key] += 1
                causes[key][str(finding.get("cause") or "")] += 1
                labels.setdefault(key, str(finding.get("label") or key))

        # measured collateral: a verification lists the actions applied and every
        # metric that moved. A metric that improved but was not what the fix was
        # mapped for is the raw material for a mapping evolution.
        collateral = Counter()
        cause_outcomes = defaultdict(Counter)
        for rec in cycles:
            ver = rec.get("verification")
            if not (isinstance(ver, dict) and ver.get("status") == "verified"):
                continue
            actions = [str(a) for a in (ver.get("actions") or [])]
            verdicts = ver.get("verdicts")
            verdicts = verdicts if isinstance(verdicts, dict) else {}
            top = max((f for f in (rec.get("findings") or [])
                       if isinstance(f, dict)),
                      key=lambda f: f.get("contribution_pct") or 0, default=None)
            cause = str((top or {}).get("cause") or "")
            for metric, verdict in verdicts.items():
                if verdict != "improved":
                    continue
                for fix_id in actions:
                    if fix_id not in self.remediation_map.get(metric, ()):
                        collateral[(fix_id, metric)] += 1
                    family = self._family_of(fix_id)
                    if cause and family:
                        cause_outcomes[cause][family] += 1
        return {
            "cycles": cycles,
            "state": state,
            "context": self._context(),
            "strategies": strategies,
            "knowledge": knowledge,
            "shortfalls": shortfalls,
            "causes": causes,
            "labels": labels,
            "collateral": collateral,
            "cause_outcomes": cause_outcomes,
        }

    def _context(self) -> dict:
        """Live extra evidence, or an empty dict when unavailable."""
        if self.context is None:
            return {}
        try:
            extra = self.context()
        except Exception:                                # pragma: no cover
            return {}
        return extra if isinstance(extra, dict) else {}

    def _family_of(self, fix_id: str) -> str:
        """Recipe family a catalogue fix belongs to, for cluster learning."""
        recipe = self.state["recipes"].get(fix_id)
        if recipe:
            return str(recipe.get("family") or "")
        for name, spec in RECIPE_LIBRARY.items():
            if name == fix_id:
                return spec["family"]
        return {
            "flush_dns": "network", "clear_temp": "storage",
            "empty_recycle_bin": "storage", "restart_explorer": "shell",
            "clear_standby": "memory", "trim_startup": "startup",
            "restart_wsl": "wsl", "optimize_wsl_boot": "wsl",
            "enable_sysmain": "startup", "trim_office_addins": "startup",
        }.get(fix_id, "")

    # ------------------------------------------------------------------- pass
    def run_pass(self, force: bool = False) -> dict:
        """One APE pass. Returns the evolutions applied and why others were not.

        ``force`` runs regardless of cadence; the scheduled path passes False and
        relies on :meth:`tick`.
        """
        if not force and (self.state["cycles_seen"]
                          - self.state["last_pass_cycle"]) < self.evolve_every:
            return {"ran": False, "reason": "not due",
                    "cycles_until_due": self.evolve_every - (
                        self.state["cycles_seen"] - self.state["last_pass_cycle"]),
                    "evolutions": []}

        ev = self._evidence()
        applied: list = []
        skipped: list = []
        budget = MAX_EVOLUTIONS_PER_PASS

        for step in (self._evolve_mappings, self._evolve_clusters,
                     self._evolve_recipes, self._evolve_shards,
                     self._evolve_proposals):
            if budget <= 0:
                skipped.append({"kind": step.__name__.replace("_evolve_", ""),
                                "reason": "evolution budget for this pass spent"})
                continue
            produced, notes = step(ev, budget)
            applied.extend(produced)
            skipped.extend(notes)
            budget -= len(produced)

        if applied:
            self.state["generation"] += 1
        self.state["passes"] += 1
        self.state["last_pass_at"] = _iso(self.now())
        self.state["last_pass_cycle"] = self.state["cycles_seen"]
        self._save()
        if self.auto_adopt:          # decide any new candidates from evidence now
            self.auto_adjudicate()
        return {"ran": True, "generation": self.state["generation"],
                "evolutions": applied, "skipped": skipped,
                "budget_used": MAX_EVOLUTIONS_PER_PASS - budget}

    # ------------------------------------------------------- 1. mappings ----
    def _evolve_mappings(self, ev: dict, budget: int) -> tuple:
        """Promote a measured collateral improvement into a usable pairing."""
        applied, skipped = [], []
        for (fix_id, metric), count in ev["collateral"].most_common():
            if len(applied) >= budget:
                break
            if count < MAPPING_THRESHOLD:
                skipped.append({"kind": "mapping", "id": f"{fix_id}->{metric}",
                                "reason": f"only {count} measured improvement(s), "
                                          f"needs {MAPPING_THRESHOLD}"})
                continue
            known = self.state["mappings"].setdefault(metric, [])
            if fix_id in known:
                continue
            known.append(fix_id)
            entry = self._log(
                "mapping",
                f"{fix_id} measurably improved {metric} "
                f"{count}x without being mapped to it — {metric} can now select it",
                registry=[f"mappings/{metric}/{fix_id}"])
            applied.append({**entry, "fix": fix_id, "metric": metric,
                            "evidence_count": count})
        return applied, skipped

    # -------------------------------------------------------- 2. clusters ---
    def _evolve_clusters(self, ev: dict, budget: int) -> tuple:
        """Rewrite cause -> recipe-family associations from measured outcomes."""
        applied, skipped = [], []
        for cause, families in ev["cause_outcomes"].items():
            if len(applied) >= budget:
                break
            ranked = [f for f, n in families.most_common()
                      if n >= CLUSTER_THRESHOLD]
            if not ranked:
                skipped.append({"kind": "cluster", "id": cause,
                                "reason": f"no family reached {CLUSTER_THRESHOLD} "
                                          f"measured resolutions"})
                continue
            if self.state["clusters"].get(cause) == ranked:
                continue
            before = self.state["clusters"].get(cause)
            self.state["clusters"][cause] = ranked
            entry = self._log(
                "cluster",
                f"cause '{cause}' is best resolved by the "
                f"{', '.join(ranked)} family (was: {', '.join(before or ['seed'])})",
                registry=[f"clusters/{cause}"])
            applied.append({**entry, "cause": cause, "families": ranked})
        return applied, skipped

    # --------------------------------------------------------- 3. recipes ---
    def _candidate_recipe(self, metric: str, cause: str) -> str | None:
        """Pick a library recipe for an unmapped metric, preferring the family the
        cluster evolution has learned for this cause."""
        matches = [name for name, spec in RECIPE_LIBRARY.items()
                   if metric in spec["metrics"]]
        if not matches:
            return None
        preferred = self.state["clusters"].get(cause) \
            or list(SEED_CLUSTERS.get(cause, ()))
        matches.sort(key=lambda n: (
            preferred.index(RECIPE_LIBRARY[n]["family"])
            if RECIPE_LIBRARY[n]["family"] in preferred else len(preferred), n))
        return matches[0]

    def _params_for(self, recipe: str, metric: str, ev: dict) -> dict:
        """Fill a recipe's declared parameters from measured evidence only.

        A parameter with no evidence stays absent, and the candidate records that
        it is incomplete — better than inventing a plausible-looking value.
        """
        spec = RECIPE_LIBRARY[recipe]
        params: dict = {}
        for name in spec["params"]:
            if name == "process":
                consumers = ev["context"].get("cpu_consumers")
                consumers = consumers if isinstance(consumers, dict) else {}
                top = next((c for c in (consumers.get("consumers") or [])
                            if isinstance(c, dict) and c.get("consistent")), None)
                if top and top.get("name"):
                    params[name] = str(top["name"])
            elif name == "limit_gb":
                target = ev["context"].get("targets", {}).get(metric)
                if isinstance(target, (int, float)) and target > 0:
                    params[name] = round(target / 1024.0, 1)
        return params

    def _evolve_recipes(self, ev: dict, budget: int) -> tuple:
        """Propose a vetted recipe for a metric that has nothing mapped."""
        applied, skipped = [], []
        unmapped = {k["key"]: k for k in ev["knowledge"]
                    if k["state"] == "unmapped"}
        for metric, count in ev["shortfalls"].most_common():
            if len(applied) >= budget:
                break
            if metric not in unmapped:
                continue
            if count < RECIPE_THRESHOLD:
                skipped.append({"kind": "recipe", "id": metric,
                                "reason": f"{count} shortfall(s), needs "
                                          f"{RECIPE_THRESHOLD}"})
                continue
            cause = (ev["causes"][metric].most_common(1) or [("", 0)])[0][0]
            if cause in STRUCTURAL_CAUSES:
                skipped.append({"kind": "recipe", "id": metric,
                                "reason": f"cause '{cause}' is structural — "
                                          f"handled as a proposal, not a recipe"})
                continue
            recipe = self._candidate_recipe(metric, cause)
            if recipe is None:
                skipped.append({"kind": "recipe", "id": metric,
                                "reason": "no vetted recipe in the library "
                                          "addresses this metric"})
                continue
            candidate_id = f"evolved_{_slug(recipe)}_{_slug(metric)}"
            if candidate_id in self.state["recipes"]:
                continue
            if len(self.state["recipes"]) >= MAX_RECIPES:
                skipped.append({"kind": "recipe", "id": metric,
                                "reason": f"recipe cap reached ({MAX_RECIPES})"})
                break
            spec = RECIPE_LIBRARY[recipe]
            params = self._params_for(recipe, metric, ev)
            missing = [p for p in spec["params"] if p not in params]
            self.state["recipes"][candidate_id] = {
                "id": candidate_id,
                "recipe": recipe,
                "title": spec["title"],
                "family": spec["family"],
                "metric": metric,
                "label": ev["labels"].get(metric, metric),
                "risk": spec["risk"],
                "reversible": spec["reversible"],
                "shell": spec["shell"],
                "rationale": spec["rationale"],
                "manual_hint": spec["manual_hint"],
                "params": params,
                "missing_params": missing,
                "cause": cause,
                "evidence_count": count,
                # Invariant 2: a generated recipe is never executable until a
                # human promotes it, and promotion is a separate authenticated act.
                "state": "candidate",
                "executable": False,
                "created": _iso(self.now()),
                "promoted_at": None,
                "generation": self.state["generation"] + 1,
            }
            entry = self._log(
                "recipe",
                f"{ev['labels'].get(metric, metric)} fell short {count}x with no "
                f"remediation mapped — proposing the vetted '{spec['title']}' "
                f"recipe as a candidate for review",
                registry=[f"recipes/{candidate_id}"])
            applied.append({**entry, "candidate": candidate_id, "metric": metric,
                            "recipe": recipe, "needs_promotion": True})
        return applied, skipped

    # ---------------------------------------------------------- 4. shards ---
    def _shard_text(self, metric: str, ev: dict) -> str:
        """A rules document generated from this metric's accumulated evidence."""
        label = ev["labels"].get(metric, metric)
        causes = ev["causes"][metric].most_common()
        row = next((k for k in ev["knowledge"] if k["key"] == metric), {})
        tried = [s for s in ev["strategies"] if s["metric"] == metric]
        lines = [
            f"# {label} — learned rules",
            "",
            f"Generated by the evolution layer at {_iso(self.now())} from "
            f"{ev['shortfalls'][metric]} observed shortfalls. Evidence only — "
            "nothing here was authored by hand.",
            "",
            "## What the evidence says",
            "",
        ]
        for cause, count in causes:
            lines.append(f"- Attributed to `{cause or 'unclassified'}` "
                         f"{count} time(s).")
        if row:
            lines += [
                f"- Long-term direction: **{row.get('direction') or 'unknown'}**"
                + (f" at {row['pct_per_day']}%/day"
                   if row.get("pct_per_day") is not None else "") + ".",
                f"- Met target on {row.get('met_days')} of the retained days.",
                f"- Chronic (target at or below this machine's floor): "
                f"**{'yes' if row.get('chronic') else 'no'}**.",
            ]
        lines += ["", "## What has been tried", ""]
        # Mapped-but-never-run and measured are different facts. Reporting only
        # the measured ones made an earlier version claim "no remediation was
        # mapped" for metrics that are in fact mapped but had never been selected.
        mapped = [m for m in row.get("mapped", []) if m not in
                  {s["id"] for s in tried}]
        if tried:
            for strategy in tried:
                lines.append(
                    f"- `{strategy['id']}` — {strategy['attempts']} attempt(s), "
                    f"measured effect "
                    f"{'n/a' if strategy['effect_pct'] is None else str(strategy['effect_pct']) + '%'}"
                    f", verdict **{strategy['status']}**.")
        for fix_id in mapped:
            lines.append(f"- `{fix_id}` — mapped to this metric but never "
                         "selected yet, so it has no measured effect.")
        if not tried and not mapped:
            lines.append("- Nothing. No remediation is mapped to this metric, "
                         "which is why this shard exists.")
        lines += [
            "",
            "## Rules derived",
            "",
            f"- MUST NOT treat a `{metric}` shortfall as actionable while its "
            "dominant cause is `chronic_unreachable` — raise a structural "
            "proposal instead.",
        ]
        if causes and causes[0][0] == "transient_spike":
            lines.append(f"- SHOULD wait one cycle before acting on `{metric}`: "
                         "its dominant cause is a transient spike, which usually "
                         "clears without intervention.")
        if row.get("direction") == "degrading":
            lines.append(f"- SHOULD raise a preventive flag for `{metric}` while "
                         "it still meets target, because the daily typical is "
                         "drifting the wrong way.")
        for strategy in tried:
            if strategy["status"] == "suppressed":
                lines.append(f"- MUST NOT choose `{strategy['id']}` for "
                             f"`{metric}`: {strategy['attempts']} measured "
                             "attempts produced no gain.")
        return "\n".join(lines) + "\n"

    def _evolve_shards(self, ev: dict, budget: int) -> tuple:
        applied, skipped = [], []
        for metric, count in ev["shortfalls"].most_common():
            if len(applied) >= budget:
                break
            if count < SHARD_THRESHOLD:
                skipped.append({"kind": "shard", "id": metric,
                                "reason": f"{count} shortfall(s), needs "
                                          f"{SHARD_THRESHOLD}"})
                continue
            if metric in self.state["shards"]:
                continue
            if len(self.state["shards"]) >= MAX_SHARDS:
                skipped.append({"kind": "shard", "id": metric,
                                "reason": f"shard cap reached ({MAX_SHARDS})"})
                break
            self.evolutions_dir.mkdir(parents=True, exist_ok=True)
            path = self.evolutions_dir / f"{_slug(metric)}-rules.md"
            text = self._shard_text(metric, ev)
            path.write_text(text, encoding="utf-8")
            self.state["shards"][metric] = {
                "metric": metric,
                "label": ev["labels"].get(metric, metric),
                "file": path.name,
                "rules": sum(1 for ln in text.splitlines()
                             if ln.startswith("- MUST") or ln.startswith("- SHOULD")),
                "shortfalls": count,
                "created": _iso(self.now()),
                "generation": self.state["generation"] + 1,
            }
            entry = self._log(
                "shard",
                f"{ev['labels'].get(metric, metric)} accumulated {count} "
                f"shortfalls with no rules — generated {path.name}",
                files=[path.name], registry=[f"shards/{metric}"])
            applied.append({**entry, "metric": metric, "file": path.name})
        return applied, skipped

    # -------------------------------------------------------- 5. proposals --
    def _evolve_proposals(self, ev: dict, budget: int) -> tuple:
        """Raise a structural proposal where no action can help."""
        applied, skipped = [], []
        for metric, count in ev["shortfalls"].most_common():
            if len(applied) >= budget:
                break
            dominant = (ev["causes"][metric].most_common(1) or [("", 0)])[0][0]
            if dominant not in STRUCTURAL_CAUSES:
                continue
            proposal_id = f"structural_{_slug(metric)}"
            if proposal_id in self.state["proposals"]:
                continue
            if len(self.state["proposals"]) >= MAX_PROPOSALS:
                skipped.append({"kind": "proposal", "id": metric,
                                "reason": f"proposal cap reached ({MAX_PROPOSALS})"})
                break
            row = next((k for k in ev["knowledge"] if k["key"] == metric), {})
            label = ev["labels"].get(metric, metric)
            self.state["proposals"][proposal_id] = {
                "id": proposal_id,
                "metric": metric,
                "label": label,
                "kind": "structural",
                "detail": (
                    f"{label} was attributed to '{dominant}' in {count} of its "
                    f"shortfalls: the target sits at or below what this hardware "
                    f"and workload can deliver. No remediation in the library can "
                    f"close it. The options are to revise the target, change the "
                    f"workload, or change the hardware."),
                "evidence": {"shortfalls": count, "dominant_cause": dominant,
                             "direction": row.get("direction"),
                             "met_days": row.get("met_days"),
                             "chronic": bool(row.get("chronic"))},
                "created": _iso(self.now()),
                "state": "open",
                "generation": self.state["generation"] + 1,
            }
            entry = self._log(
                "proposal",
                f"{label} is chronically unreachable ({count} shortfalls) — "
                f"raised a structural proposal for a human",
                registry=[f"proposals/{proposal_id}"])
            applied.append({**entry, "proposal": proposal_id, "metric": metric,
                            "needs_human": True})
        return applied, skipped

    # ------------------------------------------------------------- promotion
    # ------------------------------------------------------------- promotion
    def promoted_fixes(self) -> list:
        """Promoted recipes as Fix-shaped records, for registration with the
        FixEngine so RCA and the planner can see them.

        ``risk`` is forced to ``manual`` and ``command`` is empty. This is
        deliberate and is the honest consequence of invariant 1: the library
        supplies a reviewed *instruction* (``manual_hint``), not an executable
        command. A generated remediation therefore surfaces as a recommendation
        with the exact command to run, and the planner skips it with "manual
        action required" — rather than silently executing an empty command and
        reporting a success it did not achieve.

        Giving a recipe a real command is a deliberate human edit to
        RECIPE_LIBRARY, at which point it stops being generated capability and
        becomes shipped capability.
        """
        out = []
        for record in self.state["recipes"].values():
            if record.get("state") != "promoted":
                continue
            out.append({
                "id": record["id"],
                "title": record["title"],
                "category": record["family"],
                "risk": "manual",
                "reversible": bool(record["reversible"]),
                "shell": record["shell"],
                "command": "",
                "metric": record["metric"],
                "manual_hint": self._hint(record),
                "origin": "evolved",
            })
        return out

    @staticmethod
    def _hint(record: dict) -> str:
        """The library hint with the candidate's measured parameters filled in."""
        try:
            return record["manual_hint"].format(**(record.get("params") or {}))
        except (KeyError, IndexError, ValueError):
            return record["manual_hint"]

    def promote(self, candidate_id: str) -> dict:
        """Make a candidate recipe available to the loop's planner.

        Promotion is the single point at which generated capability becomes
        usable. It does not bypass policy: the recipe keeps its declared risk, so
        the loop's own risk gate, cooldown and peak-hour rules still decide
        whether it ever actually runs.
        """
        record = self.state["recipes"].get(candidate_id)
        if record is None:
            return {"promoted": False, "reason": "no such candidate"}
        if record["state"] == "promoted":
            return {"promoted": False, "reason": "already promoted",
                    "recipe": record}
        if record["missing_params"]:
            return {"promoted": False,
                    "reason": "candidate is incomplete: no measured value for "
                              + ", ".join(record["missing_params"]),
                    "recipe": record}
        record["state"] = "promoted"
        record["promoted_at"] = _iso(self.now())
        # Not self-executing. A promoted recipe carries a reviewed instruction,
        # not a command, so it is registered as 'manual': the loop will surface it
        # and skip it, never run it. See promoted_fixes() for why.
        record["executable"] = False
        self.state["mappings"].setdefault(record["metric"], [])
        if candidate_id not in self.state["mappings"][record["metric"]]:
            self.state["mappings"][record["metric"]].append(candidate_id)
        self.state["generation"] += 1
        entry = self._log(
            "promotion",
            f"human promoted '{record['title']}' for {record['label']} — "
            f"surfaced to RCA as a manual recommendation with the exact command",
            registry=[f"recipes/{candidate_id}"])
        self._save()
        if self.on_promote is not None:
            try:
                self.on_promote(self.promoted_fixes())
            except Exception:                            # pragma: no cover
                pass
        return {"promoted": True, "recipe": record, "log": entry}

    def reject(self, candidate_id: str) -> dict:
        """Mark a candidate as rejected so a later pass does not re-propose it."""
        record = self.state["recipes"].get(candidate_id)
        if record is None:
            return {"rejected": False, "reason": "no such candidate"}
        record["state"] = "rejected"
        record["executable"] = False
        self._log("rejection",
                  f"human rejected '{record['title']}' for {record['label']}",
                  registry=[f"recipes/{candidate_id}"])
        self._save()
        return {"rejected": True, "recipe": record}

    # -------------------------------------------------------------- rollback
    def rollback_last(self) -> dict:
        """Undo the most recent evolution: delete its files and registry keys."""
        if not self.state["log"]:
            return {"rolled_back": False, "reason": "nothing to roll back"}
        entry = self.state["log"][-1]
        removed_files, removed_keys = [], []
        for name in entry.get("files") or []:
            path = self.evolutions_dir / Path(name).name
            try:
                path.unlink()
                removed_files.append(path.name)
            except OSError:
                pass
        for key in entry.get("registry") or []:
            parts = key.split("/")
            bucket = self.state.get(parts[0])
            if not isinstance(bucket, dict) or len(parts) < 2:
                continue
            if len(parts) == 2:
                if bucket.pop(parts[1], None) is not None:
                    removed_keys.append(key)
            else:                       # mappings/<metric>/<fix id>
                inner = bucket.get(parts[1])
                if isinstance(inner, list) and parts[2] in inner:
                    inner.remove(parts[2])
                    removed_keys.append(key)
        self.state["log"].pop()
        self.state["generation"] = max(0, self.state["generation"] - 1)
        self._log("rollback",
                  f"reverted the {entry['kind']} evolution: {entry['summary']}")
        self._save()
        return {"rolled_back": True, "reverted": entry,
                "files_removed": removed_files, "keys_removed": removed_keys}

    # -------------------------------------------------------------- reporting
    def evolved_map(self) -> dict:
        """metric -> promoted/learned remediation ids, for the planner and the
        knowledge-set coverage calculation. Candidates are excluded: an unpromoted
        proposal is not coverage."""
        out: dict = {}
        for metric, ids in self.state["mappings"].items():
            usable = [i for i in ids
                      if self.state["recipes"].get(i, {}).get("state",
                                                              "promoted")
                      == "promoted"]
            if usable:
                out[metric] = usable
        return out

    def status(self) -> dict:
        recipes = list(self.state["recipes"].values())
        by_state = Counter(r["state"] for r in recipes)
        clusters = {**{c: list(f) for c, f in SEED_CLUSTERS.items()},
                    **self.state["clusters"]}
        due_in = max(0, self.evolve_every - (self.state["cycles_seen"]
                                             - self.state["last_pass_cycle"]))
        return {
            "generation": self.state["generation"],
            "passes": self.state["passes"],
            "cycles_seen": self.state["cycles_seen"],
            "evolve_every": self.evolve_every,
            "cycles_until_pass": due_in,
            "last_pass_at": self.state["last_pass_at"],
            "kpis": {
                "generation": self.state["generation"],
                "candidates": by_state.get("candidate", 0),
                "promoted": by_state.get("promoted", 0),
                "shards": len(self.state["shards"]),
                "mappings": sum(len(v) for v in self.state["mappings"].values()),
                "proposals": sum(1 for p in self.state["proposals"].values()
                                 if p.get("state") == "open"),
            },
            "by_kind": dict(Counter(e["kind"] for e in self.state["log"])),
            "recipes": sorted(recipes, key=lambda r: (r["state"], r["id"])),
            "shards": sorted(self.state["shards"].values(),
                             key=lambda s: -s["shortfalls"]),
            "mappings": {k: list(v) for k, v in self.state["mappings"].items()},
            "clusters": clusters,
            "proposals": sorted(self.state["proposals"].values(),
                                key=lambda p: p["id"]),
            "log": list(reversed(self.state["log"]))[:60],
            "library": sorted(
                [{"id": name, "title": spec["title"], "family": spec["family"],
                  "risk": spec["risk"], "metrics": list(spec["metrics"]),
                  "params": list(spec["params"]),
                  "rationale": spec["rationale"]}
                 for name, spec in RECIPE_LIBRARY.items()],
                key=lambda r: r["id"]),
            "caps": {"recipes": MAX_RECIPES, "shards": MAX_SHARDS,
                     "proposals": MAX_PROPOSALS,
                     "per_pass": MAX_EVOLUTIONS_PER_PASS},
            "thresholds": {"recipe": RECIPE_THRESHOLD, "shard": SHARD_THRESHOLD,
                           "mapping": MAPPING_THRESHOLD,
                           "cluster": CLUSTER_THRESHOLD},
            "evolutions_dir": str(self.evolutions_dir),
        }
