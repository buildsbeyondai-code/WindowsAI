#!/usr/bin/env python3
"""
Human action queue — the work the loop cannot do, with steps and a reason.

The auto-improve loop is explicit about what it skips, but "manual action
required" told the reader nothing actionable: not what to do, not where, not why
automation is barred, and not how to confirm it worked. This module turns every
skip, escalation and pending review into a concrete work item.

Four sources feed the queue:

``fix``         a remediation the loop selected but may not run itself.
``decision``    a chronic gap where the target, the workload or the hardware has
                to change — a judgement, not an action.
``review``      an evolution-generated candidate awaiting human promotion.
``blocked``     a candidate the engine could not complete because no measured
                evidence exists for one of its parameters.

Every item carries a ``why_human`` record with a *specific* cause, not the
catch-all "manual". The distinction matters because it tells the reader what kind
of work it is: fetching admin rights is a two-minute chore, deciding which
start-up programs are essential is not, and revising an unreachable target is a
different conversation entirely.

Steps come from :data:`STEP_RECIPES` — reviewed, per-fix instructions — with an
evidence-built fallback for anything not yet given a recipe. Nothing here
executes: this module only ever reads and formats.
"""
from __future__ import annotations

import json
import time
from pathlib import Path


def _stamp(epoch: float) -> str:
    return time.strftime("%Y-%m-%dT%H:%M:%S+00:00", time.gmtime(epoch))


# --------------------------------------------------------------------------- #
# Why automation is barred                                                    #
# --------------------------------------------------------------------------- #
# Each reason states the mechanism, so the reader can tell a chore from a
# judgement from a policy decision.
WHY = {
    "needs_elevation": (
        "Needs administrator rights",
        "The dashboard service runs unprivileged on purpose — a telemetry "
        "process that can elevate is a far larger risk than the problem it "
        "fixes. There is no way for it to acquire admin rights without a UAC "
        "prompt only you can answer."),
    "session_killing": (
        "Would terminate this dashboard",
        "The action restarts the WSL VM, which hosts the dashboard, its "
        "collector and the sampling history. Run automatically it would kill "
        "the process mid-cycle and lose the measurement it was trying to make."),
    "needs_judgement": (
        "Requires knowing what matters to you",
        "Which items are essential is specific to how you work. The loop has no "
        "way to tell a critical tool from an unnecessary one, and guessing wrong "
        "breaks something you depend on. It is left manual deliberately."),
    "gui_only": (
        "No safe scriptable equivalent",
        "The setting lives behind a GUI or a per-application profile. Editing "
        "the underlying store directly is undocumented and version-specific, so "
        "scripting it risks corrupting the application's configuration."),
    "persistent_config": (
        "Changes configuration that survives reboot",
        "This is not a transient tweak: it rewrites persistent machine "
        "configuration. You should know it happened, because it keeps applying "
        "long after the shortfall that prompted it has gone."),
    "chronic_unreachable": (
        "The target is below what this machine can deliver",
        "Every remediation available has been measured and none closes the gap: "
        "the target sits at or under this hardware's floor for your workload. "
        "The only real options are to revise the target, change the workload, or "
        "change the hardware — all decisions, not actions."),
    "grant_capability": (
        "Only you may grant the loop new capability",
        "The evolution layer proposed this from measured evidence, but a system "
        "that can widen its own permissions has no meaningful limit. Promotion "
        "is the deliberate boundary: it stays inert until you accept it."),
    "no_evidence": (
        "Missing a value nothing has measured",
        "The recipe needs a parameter the loop has no measurement for, and "
        "inventing a plausible value is worse than leaving it blank. Supply the "
        "value, or wait for the evidence to accumulate."),
}

# Where the work happens. Named precisely because "run this command" in the wrong
# shell is the most common way these steps fail.
WHERE = {
    "elevated_pwsh": "Windows PowerShell, opened as Administrator",
    "windows_pwsh": "Windows PowerShell or CMD — on the Windows host, NOT inside WSL",
    "task_manager": "Task Manager → Start-up tab (Ctrl+Shift+Esc)",
    "app_gui": "The application's own options dialog",
    "dashboard": "This dashboard — the Auto-Improve tab",
    "decision": "No terminal — this is a decision to make and record",
}

# One-sentence, jargon-free statement of exactly what the person must do, keyed by
# the item's ``why_code``. The detailed steps/why already exist; this is the
# "in plain words, what do you want from me?" line shown at the top of each card.
SIMPLE_ASK = {
    "needs_elevation": (
        "Run the short command in the steps once, in an Administrator "
        "PowerShell — then click “I did this”."),
    "session_killing": (
        "When it suits you, run the one restart command in the steps "
        "(it closes this dashboard briefly) — then click “I did this”."),
    "needs_judgement": (
        "Look at the list in the steps and switch off the ones you don’t "
        "need — then click “I did this”. Nothing is changed for you."),
    "gui_only": (
        "Change the one setting shown in the steps inside the app itself — "
        "then click “I did this”."),
    "persistent_config": (
        "Apply the one change in the steps (it sticks after reboot) — then "
        "click “I did this”."),
    "chronic_unreachable": (
        "No command to run. Just decide: is this goal realistic for your PC? "
        "If it can never be reached, ask me to lower the goal. If it can, "
        "close a few apps or accept it — then click “I did this”."),
    "grant_capability": (
        "No command to run. The system wants your yes/no to add a new "
        "automatic fix. Approve it (safe and reversible) or reject it using "
        "the Promote / Reject buttons in the evolution card below."),
    "no_evidence": (
        "Nothing to do yet — the system is still collecting a measurement. "
        "Leave it; it completes itself."),
}


def _simple_ask(why_code: str) -> str:
    """Plain-words 'what is needed from you' for a card, or a safe default."""
    return SIMPLE_ASK.get(why_code,
                          "Follow the steps below, then click “I did this”.")

# --------------------------------------------------------------------------- #
# Reviewed, per-fix step recipes                                              #
# --------------------------------------------------------------------------- #
# Keyed by fix id. ``steps`` are imperative and ordered; ``verify`` says how to
# confirm it worked from this dashboard, which is the part usually missing from a
# manual hint.
STEP_RECIPES = {
    "clear_standby": {
        "why": "needs_elevation",
        "where": "elevated_pwsh",
        "effort": "5 min, one-off download",
        "steps": [
            "Download Sysinternals RAMMap from "
            "https://learn.microsoft.com/sysinternals/downloads/rammap "
            "and unblock the zip (Properties → Unblock) before extracting.",
            "Open Windows PowerShell as Administrator (Win+X → "
            "'Terminal (Admin)' / 'PowerShell (Admin)').",
            "Run: RAMMap.exe -Et",
            "The window does not need to stay open — '-Et' empties the standby "
            "list and exits.",
        ],
        "verify": "Watch 'Available physical' and 'Memory Compression' on the "
                  "Details tab. The next sample (within 20 s) should show "
                  "available memory up and the compressed set down.",
        "expected": "Standby (cached) pages are released back to the free list. "
                    "The effect is immediate but temporary — Windows refills the "
                    "cache as you work, which is normal.",
    },
    "trim_startup": {
        "why": "needs_judgement",
        "where": "task_manager",
        "effort": "10 min once",
        "steps": [
            "Open Task Manager (Ctrl+Shift+Esc) → 'Start-up apps'.",
            "Sort by 'Start-up impact' and read down the High and Medium rows.",
            "For each one, decide whether you need it running from boot or "
            "only when you actually open it. Updaters, vendor tray helpers, "
            "cloud-sync clients for drives you rarely touch and chat clients are "
            "the usual wins.",
            "Right-click → Disable. Do NOT disable security agents, VPN "
            "clients, device drivers or anything your employer requires.",
            "Sign out and back in, so the next boot sample reflects the change.",
        ],
        "verify": "The 'Startup programs (enabled)' row on the Details tab "
                  "should fall to your new count on the next boot sample, and "
                  "'Boot time' should improve over the following few days.",
        "expected": "Fewer processes contend for CPU and disk during sign-in, "
                    "which shortens boot and lowers idle CPU afterwards.",
    },
    "restart_wsl": {
        "why": "session_killing",
        "where": "windows_pwsh",
        "effort": "2 min, plus reopening your WSL work",
        "steps": [
            "Save and close anything running inside WSL — this terminates the "
            "VM, including this dashboard and its collector.",
            "Open Windows PowerShell or CMD on the Windows host. This will not "
            "work from a shell inside WSL.",
            "Run: wsl --shutdown",
            "Wait about 10 seconds, then reopen your WSL terminal. The "
            "dashboard service restarts automatically with it.",
        ],
        "verify": "Once the dashboard is reachable again, 'vmmemWSL' on the "
                  "Details tab should start from a low value and grow only to "
                  "the ceiling set in .wslconfig.",
        "expected": "The WSL VM releases all memory back to Windows and "
                    "restarts under the current .wslconfig limits. History and "
                    "learned state survive — both are on disk.",
    },
    "enable_sysmain": {
        "why": "needs_elevation",
        "where": "elevated_pwsh",
        "effort": "2 min",
        "steps": [
            "Open Windows PowerShell as Administrator.",
            "Run: Set-Service SysMain -StartupType Automatic",
            "Run: Start-Service SysMain",
            "Confirm with: Get-Service SysMain  (Status should be Running)",
        ],
        "verify": "Application launch rows on the Details tab (Explorer, "
                  "Outlook, Word) improve gradually — SysMain needs a few days "
                  "of use to learn your pattern before pre-caching helps.",
        "expected": "Windows pre-loads frequently used applications into "
                    "standby memory, cutting cold-start time. It uses spare RAM "
                    "and yields it on demand.",
        "caveat": "If this machine has a slow spinning disk, SysMain helps most. "
                  "On a fast NVMe SSD the gain is small; some IT policies "
                  "disable it deliberately, so check before re-enabling.",
    },
    "trim_office_addins": {
        "why": "gui_only",
        "where": "app_gui",
        "effort": "10 min once",
        "steps": [
            "Open Outlook → File → Options → Add-ins.",
            "At the bottom, set 'Manage' to 'COM Add-ins' and click Go…",
            "Untick add-ins you do not use. Legacy archiving, CRM and "
            "meeting-recorder plugins are the usual offenders.",
            "Repeat in Word (File → Options → Add-ins).",
            "Restart both applications fully, then time a cold start.",
        ],
        "verify": "'Outlook launch' and 'Word launch' on the Details tab should "
                  "improve on the next measured cold start.",
        "expected": "Each COM add-in is loaded synchronously during start-up, so "
                    "removing unused ones cuts launch time directly.",
        "caveat": "Some add-ins are mandated by your employer for compliance or "
                  "archiving. If in doubt, leave it enabled.",
    },
    "empty_recycle_bin": {
        "why": "needs_judgement",
        "where": "elevated_pwsh",
        "effort": "1 min",
        "steps": [
            "Check the Recycle Bin for anything you still want — this is NOT "
            "reversible.",
            "Open Windows PowerShell (elevation is not required for your own "
            "profile).",
            "Run: Clear-RecycleBin -Force",
        ],
        "verify": "'C: free' on the Details tab should rise on the next sample.",
        "expected": "Deleted files are released permanently. The loop will not "
                    "do this unattended because it cannot be undone.",
    },
}


def _fallback_steps(title: str, hint: str, shell: str, where: str) -> dict:
    """Steps for a remediation with no reviewed recipe yet.

    Built only from what is known — the fix's own hint and shell — so the item is
    still actionable without inventing procedure it cannot vouch for.
    """
    steps = [f"Open {WHERE[where]}."]
    if hint:
        steps.append(hint)
    else:
        steps.append(f"Carry out '{title}' manually. No reviewed step-by-step "
                     "recipe exists for this one yet.")
    steps.append("Return here after the next scan (within 5 minutes) to see the "
                 "measured effect.")
    return {"steps": steps,
            "verify": "Watch the affected row on the Details tab over the next "
                      "few samples.",
            "expected": "The loop records the metric before and after, so the "
                        "real effect appears in 'Learned fix effectiveness'."}


def _why_for(fix, recipe: dict | None) -> str:
    """Classify why a fix cannot be automated, from its declared flags."""
    if recipe and recipe.get("why"):
        return recipe["why"]
    if getattr(fix, "session_killing", False):
        return "session_killing"
    hint = (getattr(fix, "manual_hint", "") or "").lower()
    if "elevat" in hint or "as admin" in hint or "administrator" in hint:
        return "needs_elevation"
    if "task manager" in hint or "options" in hint or "->" in hint:
        return "gui_only"
    if getattr(fix, "persistent", False):
        return "persistent_config"
    return "needs_judgement"


def _where_for(fix, recipe: dict | None) -> str:
    if recipe and recipe.get("where"):
        return recipe["where"]
    if getattr(fix, "session_killing", False):
        return "windows_pwsh"
    return "elevated_pwsh"


class ActionQueue:
    """Assembles the human work queue from the loop, RCA and the evolution layer.

    Read-only with respect to the loop. The one thing it does own is the *status*
    of each item, persisted in ``state_file``, so "I have done this" survives a
    restart and the same task is never presented twice.

    Identity and de-duplication
    ---------------------------
    Every item has a stable ``key`` of the form ``<kind>:<target>``. The queue is
    assembled into a dict keyed by it, so a remediation that is skipped for two
    different metrics in the same cycle, or one that appears in both ``planned``
    and ``skipped``, yields exactly one item. The first source to claim a key
    wins, and :meth:`report` asserts the keys are unique.

    Status
    ------
    ``open``   the work is still outstanding.
    ``closed`` with ``closed_by`` saying which of the two ways it closed:
               ``human``    — you took the action and recorded it. The item reads
                              "action taken", with when and what you noted.
               ``evidence`` — it resolved itself (the candidate was promoted, the
                              escalation cleared, the metric came back on target).

    A manual close is recorded together with the evidence counter at that moment.
    If the same trigger recurs afterwards the item reopens by itself and is
    flagged ``recurred`` — closing a task must not hide it coming back.
    """

    def __init__(self, improver, fix_engine, evolution=None,
                 state_file=None, now=time.time,
                 surface_evolution: bool = True) -> None:
        self.improver = improver
        self.fixes = fix_engine
        self.evolution = evolution
        self.state_file = Path(state_file) if state_file else None
        self.now = now
        # When False, self-authored evolution proposals are NOT shown as items in
        # the human queue — the engine decides them itself (auto_adopt). They stay
        # visible only in the Self-authoring evolution section for power users.
        self.surface_evolution = bool(surface_evolution)
        self.state = {}
        self._rca = {}
        self._load()

    # ---------------------------------------------------------------- status
    def _load(self) -> None:
        if self.state_file is None:
            return
        try:
            data = json.loads(self.state_file.read_text(encoding="utf-8"))
        except (OSError, ValueError):
            return
        if isinstance(data, dict) and isinstance(data.get("items"), dict):
            self.state = data["items"]

    def _save(self) -> None:
        if self.state_file is None:
            return
        self.state_file.parent.mkdir(parents=True, exist_ok=True)
        tmp = self.state_file.with_suffix(self.state_file.suffix + ".tmp")
        tmp.write_text(json.dumps({"items": self.state}, indent=1),
                       encoding="utf-8")
        tmp.replace(self.state_file)

    def set_status(self, key: str, status: str, note: str = "") -> dict:
        """Mark an item done or reopen it. Returns the stored record."""
        if status not in ("open", "closed"):
            raise ValueError("status must be 'open' or 'closed'")
        # Assemble in the current analysis context and put it back: reporting
        # with an empty RCA would read different numbers than the caller sees.
        saved = self._rca
        try:
            live = {item["key"]: item for item in self._assemble()}
        finally:
            self._rca = saved
        if key not in live:
            return {"ok": False, "reason": "no such action item", "key": key}
        current = int(live[key].get("recur_count") or 0)
        if status == "open":
            self.state.pop(key, None)
            self._save()
            return {"ok": True, "key": key, "status": "open"}
        self.state[key] = {
            "status": "closed",
            "closed_by": "human",
            "at": _stamp(self.now()),
            "note": str(note or "")[:400],
            # The recurrence counter at the moment of closing. If it grows later
            # the trigger has been re-raised and the item reopens on its own.
            "recur_count_at_close": current,
        }
        self._save()
        return {"ok": True, "key": key, **self.state[key]}

    # ------------------------------------------------------------------ parts
    def _fix_items(self, state: dict, report: dict) -> list:
        """Remediations this cycle selected but is not permitted to run."""
        last = state.get("last") or {}
        shares = {}
        for finding in (report.get("findings") or []):
            if isinstance(finding, dict) and finding.get("key"):
                shares[finding["key"]] = finding
        items = []
        for decision in (last.get("skipped") or []) + (last.get("planned") or []):
            if not isinstance(decision, dict) or decision.get("eligible"):
                continue
            fix_id = str(decision.get("id") or "")
            if not fix_id:
                continue
            fix = self.fixes.get(fix_id)
            # Only genuinely human-blocked work belongs here. A fix waiting out a
            # cooldown, deferred past peak hours or suppressed as ineffective is
            # the loop managing itself, not a task for the reader.
            if fix is None or fix.executable:
                continue
            recipe = STEP_RECIPES.get(fix_id)
            why = _why_for(fix, recipe)
            where = _where_for(fix, recipe)
            metric = str(decision.get("for") or "")
            finding = shares.get(metric) or {}
            detail = recipe or _fallback_steps(
                fix.title, fix.manual_hint or "", fix.shell, where)
            items.append({
                # One item per remediation, not per (remediation, metric): the
                # work is the same however many rows it would help.
                "key": f"fix:{fix_id}",
                "id": fix_id,
                "kind": "fix",
                "title": fix.title,
                "metric": metric,
                "metric_label": str(finding.get("label") or metric),
                "gap_share_pct": finding.get("contribution_pct"),
                "cause": str(finding.get("cause") or ""),
                "why_code": why,
                "why_headline": WHY[why][0],
                "why_detail": WHY[why][1],
                "simple_ask": _simple_ask(why),
                "where": WHERE[where],
                "steps": list(detail["steps"]),
                "verify": detail["verify"],
                "expected": detail["expected"],
                "caveat": detail.get("caveat", ""),
                "effort": detail.get("effort", "a few minutes"),
                "command": (fix.command or "").strip(),
                "reversible": bool(fix.reversible),
                "persistent": bool(getattr(fix, "persistent", False)),
                "evidence": str(decision.get("why") or ""),
                # No monotonic recurrence signal exists for a fix: its share of
                # the gap moves every cycle. A fix item closes when the metric
                # resolves or the skip stops being reported, so recurrence is
                # tracked by presence, not by a counter.
                "recur_count": 0,
                "resolved": str(finding.get("status") or "") == "met"
                            or metric not in shares,
                "has_recipe": recipe is not None,
            })
        return items

    def _decision_items(self, state: dict) -> list:
        """Chronic gaps escalated by the loop: a target/workload/hardware call."""
        items = []
        for proposal in state.get("proposals") or []:
            if not isinstance(proposal, dict):
                continue
            evidence = proposal.get("evidence") or {}
            metric = str(proposal.get("key") or "")
            label = str(evidence.get("label") or metric)
            target = evidence.get("target")
            best = evidence.get("min") if evidence.get("higher_is_better") \
                else evidence.get("max")
            items.append({
                "key": f"decision:{metric}",
                "id": f"decision_{metric}",
                "kind": "decision",
                "title": f"Decide what to do about {label}",
                "metric": metric,
                "metric_label": label,
                "gap_share_pct": None,
                "cause": "chronic_unreachable",
                "why_code": "chronic_unreachable",
                "why_headline": WHY["chronic_unreachable"][0],
                "why_detail": WHY["chronic_unreachable"][1],
                "simple_ask": _simple_ask("chronic_unreachable"),
                "where": WHERE["decision"],
                "steps": [
                    f"Read the evidence: over {evidence.get('window_days')} days "
                    f"{label} met its target on only "
                    f"{evidence.get('met_days')} of them, drifting "
                    f"{evidence.get('pct_per_day')}%/day "
                    f"({evidence.get('direction')}).",
                    f"Compare the target ({target}) with what this machine "
                    f"actually achieves (best observed: {best}, mean: "
                    f"{evidence.get('mean')}). If the target was never reachable, "
                    f"the row is measuring the wrong thing.",
                    "Pick one: (a) revise the target to something this hardware "
                    "can hold, (b) change the workload that drives it, or "
                    "(c) change the hardware.",
                    "If you revise the target, edit CRITERIA in config.py and "
                    "restart the service so the scorecard reflects the new goal.",
                ],
                "verify": "After a target change the row should move out of "
                          "permanent shortfall, and this escalation stops "
                          "recurring on later cycles.",
                "expected": "The score stops being dragged down by a goal that "
                            "was never achievable, so the remaining shortfalls "
                            "are the ones actually worth acting on.",
                "caveat": "Do not simply lower the target to make the score look "
                          "better. The point is to make the number honest.",
                "effort": "a conversation, not a command",
                "command": "",
                "reversible": True,
                "persistent": True,
                "evidence": str(proposal.get("detail") or ""),
                # The loop counts how many cycles re-raised this escalation, which
                # is exactly the recurrence signal a closed item should reopen on.
                "recur_count": int(proposal.get("seen") or 0),
                "resolved": False,
                "seen": int(proposal.get("seen") or 0),
                "has_recipe": True,
            })
        return items

    def _evolution_items(self) -> list:
        """Generated candidates awaiting promotion, or blocked for lack of data."""
        if self.evolution is None:
            return []
        items = []
        for record in self.evolution.status().get("recipes") or []:
            state = record.get("state")
            # A promoted or rejected candidate is decided: it is reported as a
            # closed item once (so the queue shows it was handled) and never as
            # outstanding work.
            decided = state != "candidate"
            blocked = list(record.get("missing_params") or [])
            why = "no_evidence" if blocked else "grant_capability"
            hint = record.get("manual_hint") or ""
            try:
                hint = hint.format(**(record.get("params") or {}))
            except (KeyError, IndexError, ValueError):
                pass
            if blocked:
                steps = [
                    f"The '{record.get('title')}' recipe needs a value for "
                    f"{', '.join(blocked)}, which nothing has measured yet.",
                    "Either leave it — the loop fills the parameter in "
                    "automatically once the evidence appears — or reject the "
                    "candidate if the recipe is wrong for this metric.",
                ]
                verify = ("The Promote button enables itself as soon as the "
                          "parameter has a measured value.")
                expected = ("Nothing changes until then. An incomplete candidate "
                            "is inert.")
            else:
                steps = [
                    f"Read the rationale: {record.get('rationale')}",
                    f"Check the proposed instruction is right for this machine: "
                    f"{hint}",
                    "Press Promote in the Self-authoring evolution card below to "
                    "accept it, or Reject to stop it being proposed again.",
                    "Promoting does NOT run it. The recipe is registered as a "
                    "manual recommendation, so it appears in the root-cause "
                    "analysis with the exact command and the loop skips it.",
                ]
                verify = ("After promoting, force a scan: the recipe appears as a "
                          "remediation for this metric in Root-cause analysis, "
                          "marked 'manual action required'.")
                expected = ("The metric stops being uncoverable. It gains a "
                            "documented, evidence-derived remediation you can run "
                            "yourself.")
            items.append({
                "key": f"review:{record.get('id')}",
                "id": record.get("id"),
                "kind": "blocked" if blocked and not decided else "review",
                "title": f"Review the proposed '{record.get('title')}'",
                "metric": str(record.get("metric") or ""),
                "metric_label": str(record.get("label") or record.get("metric")),
                "gap_share_pct": None,
                "cause": str(record.get("cause") or ""),
                "why_code": why,
                "why_headline": WHY[why][0],
                "why_detail": WHY[why][1],
                "simple_ask": _simple_ask(why),
                "where": WHERE["dashboard"],
                "steps": steps,
                "verify": verify,
                "expected": expected,
                "caveat": "",
                "effort": "2 min to read and decide",
                "command": hint,
                "reversible": True,
                "persistent": False,
                "evidence": (f"proposed after {record.get('evidence_count')} "
                             f"shortfalls with no remediation mapped"),
                # A candidate is decided by promotion or rejection, which sets
                # resolved; it is never re-proposed, so there is nothing to recur.
                "recur_count": 0,
                "resolved": decided,
                "resolution": (f"{state} on "
                               f"{str(record.get('promoted_at') or '')[:19]}"
                               if decided else ""),
                "has_recipe": True,
            })
        return items

    # ------------------------------------------------------------------ report
    def _assemble(self) -> list:
        """Every candidate item, de-duplicated by key. First source wins."""
        try:
            state = self.improver.status()
        except Exception:                                # pragma: no cover
            state = {}
        report = self._rca or {}
        unique = {}
        evo_items = self._evolution_items() if self.surface_evolution else []
        for item in (self._fix_items(state, report) + self._decision_items(state)
                     + evo_items):
            unique.setdefault(item["key"], item)
        return list(unique.values())

    def report(self, rca_report: dict | None = None) -> dict:
        """The full queue, highest-impact first, each item open or closed."""
        self._rca = rca_report or {}
        items = self._assemble()

        for item in items:
            stored = self.state.get(item["key"]) or {}
            closed_by_hand = stored.get("status") == "closed"
            recurred = bool(
                closed_by_hand
                and item["recur_count"] > int(stored.get("recur_count_at_close") or 0))
            if closed_by_hand and not recurred:
                # You acted. Reported as such even if the evidence has since
                # caught up, because who closed it is the more useful fact.
                item["status"] = "closed"
                item["closed_by"] = "human"
                item["closed_reason"] = "action taken"
                item["closed_at"] = stored.get("at", "")
            elif item["resolved"]:
                item["status"] = "closed"
                item["closed_by"] = "evidence"
                item["closed_reason"] = "resolved automatically"
                item["closed_at"] = ""
            else:
                item["status"] = "open"
                item["closed_by"] = ""
                item["closed_reason"] = ""
                item["closed_at"] = ""
            item["note"] = str(stored.get("note") or "")
            item["recurred"] = recurred
            item.setdefault("resolution", "")

        # Open work first, then by impact. A closed item stays visible so the
        # reader can see what has been handled, but never above outstanding work.
        rank = {"fix": 0, "decision": 1, "review": 2, "blocked": 3}
        items.sort(key=lambda i: (i["status"] == "closed",
                                  rank.get(i["kind"], 9),
                                  -(i["gap_share_pct"] or 0), i["key"]))
        for position, item in enumerate(
                (i for i in items if i["status"] == "open"), start=1):
            item["priority"] = position
        for item in items:
            item.setdefault("priority", None)

        open_items = [i for i in items if i["status"] == "open"]
        share = sum(i["gap_share_pct"] or 0 for i in open_items
                    if i["kind"] == "fix")
        keys = [i["key"] for i in items]
        return {
            "items": items,
            "count": len(items),
            "open": len(open_items),
            "closed": len(items) - len(open_items),
            "recurred": sum(1 for i in items if i["recurred"]),
            "action_taken": sum(1 for i in items if i["closed_by"] == "human"),
            "unique_keys": len(set(keys)) == len(keys),
            "by_kind": {kind: sum(1 for i in open_items if i["kind"] == kind)
                        for kind in ("fix", "decision", "review", "blocked")},
            "by_reason": {code: sum(1 for i in open_items
                                    if i["why_code"] == code)
                          for code in sorted({i["why_code"] for i in open_items})},
            "blocked_gap_share_pct": round(share, 1) if share else 0.0,
            "reasons": {code: {"headline": head, "detail": detail}
                        for code, (head, detail) in WHY.items()},
        }
