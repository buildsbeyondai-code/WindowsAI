"""Full-coverage tests for the lock-state OneDrive policy.

The policy reconciles (level-triggered). Two tests here changed meaning when the
latch bug was fixed on 2026-09-28:

* ``test_no_action_between_transitions`` asserted that a non-transition tick is
  always a no-op. That *was* the bug: it let Windows' OneDrive autostart win
  permanently once the policy had latched. Reconciliation is now expected, and
  manual control is asserted separately via :meth:`note_manual`.
* ``test_status`` gained the ``reconcile`` / ``manual_hold_s`` fields the UI
  needs to show why the policy is or is not acting.
"""
from sysperf.session_policy import SessionOneDrivePolicy


class FakeClock:
    """Monotonic clock under test control."""

    def __init__(self, t=1000.0):
        self.t = t

    def __call__(self):
        return self.t

    def advance(self, secs):
        self.t += secs
        return self.t


def _policy(latest_seq, **kw):
    """Policy whose get_latest() walks a sequence of samples, recording actions."""
    state = {"i": 0}
    actions = []

    def get_latest():
        i = min(state["i"], len(latest_seq) - 1)
        state["i"] += 1
        return latest_seq[i]

    def apply_fn(runner, action):
        actions.append(action)
        return {"action": action, "status": "ok"}

    p = SessionOneDrivePolicy(runner=lambda s, c: (0, ""), get_latest=get_latest,
                              apply_fn=apply_fn, **kw)
    p.actions = actions
    return p


# --------------------------------------------------------------- desired state --
def test_desired_action_matrix():
    da = SessionOneDrivePolicy.desired_action
    assert da(True, False) == "start"     # locked, not running -> start
    assert da(True, None) == "start"
    assert da(True, True) is None         # locked, already running -> nothing
    assert da(False, True) == "stop"      # unlocked, running -> stop
    assert da(False, None) == "stop"
    assert da(False, False) is None       # unlocked, already stopped -> nothing


def test_disabled_or_readonly_does_nothing():
    p = _policy([{"session_locked": True, "onedrive_running": False}],
                enabled=False, fix_enabled=True)
    assert p.tick() is None and p.actions == []
    p2 = _policy([{"session_locked": True, "onedrive_running": False}],
                 enabled=True, fix_enabled=False)
    assert p2.tick() is None and p2.actions == []


def test_lock_state_unknown_is_noop():
    p = _policy([{"onedrive_running": True}])   # no session_locked field
    assert p.tick() is None and p.actions == []
    assert p.last_locked is None


def test_first_tick_locked_starts_onedrive():
    p = _policy([{"session_locked": True, "onedrive_running": False}])
    out = p.tick()
    assert out == {"action": "start", "status": "ok"}
    assert p.actions == ["start"] and p.last_locked is True


def test_unlock_transition_stops_onedrive():
    p = _policy([
        {"session_locked": True, "onedrive_running": False},   # start
        {"session_locked": False, "onedrive_running": True},   # stop
    ])
    assert p.tick()["action"] == "start"
    assert p.tick()["action"] == "stop"
    assert p.actions == ["start", "stop"]


def test_transition_with_desired_state_already_held_is_noop():
    # first observation is unlocked and OneDrive already stopped -> nothing to do
    p = _policy([{"session_locked": False, "onedrive_running": False}])
    assert p.tick() is None
    assert p.last_locked is False and p.actions == []


# ------------------------------------------------------------- reconciliation --
def test_reconciles_drift_without_a_transition():
    """The regression this fix exists for.

    Unlocked with OneDrive stopped (desired state holds), then something external
    starts it — Windows logon autostart did exactly this at 04:25 on 2026-09-28.
    The lock state never changes, so an edge-triggered policy would never act.
    """
    p = _policy([
        {"session_locked": False, "onedrive_running": False},  # settled, no-op
        {"session_locked": False, "onedrive_running": True},   # drift!
        {"session_locked": False, "onedrive_running": True},   # still drifted
    ])
    assert p.tick() is None                     # nothing to correct yet
    assert p.tick()["action"] == "stop"         # drift corrected
    assert p.tick()["action"] == "stop"         # keeps correcting while wrong
    assert p.actions == ["stop", "stop"]


def test_reconcile_disabled_restores_edge_only_behaviour():
    p = _policy([
        {"session_locked": False, "onedrive_running": False},
        {"session_locked": False, "onedrive_running": True},   # drift ignored
    ], reconcile=False)
    assert p.tick() is None
    assert p.tick() is None
    assert p.actions == []


def test_no_action_between_transitions_when_state_is_correct():
    """A settled, correct state is still a no-op — reconciling is not thrashing."""
    p = _policy([
        {"session_locked": True, "onedrive_running": False},   # start
        {"session_locked": True, "onedrive_running": True},    # now correct
        {"session_locked": True, "onedrive_running": True},    # still correct
    ])
    assert p.tick()["action"] == "start"
    assert p.tick() is None
    assert p.tick() is None
    assert p.actions == ["start"]


# ------------------------------------------------------------- manual override --
def test_manual_hold_suspends_reconciliation_then_expires():
    clock = FakeClock()
    p = _policy([{"session_locked": False, "onedrive_running": True}],
                clock=clock, manual_grace_s=600)
    p.last_locked = False                       # already observed: no transition
    assert p.note_manual("start") == 1600.0
    assert p.manual_hold_s() == 600
    assert p.tick() is None                     # honours the click
    clock.advance(599)
    assert p.tick() is None                     # still inside the window
    clock.advance(2)                            # window elapsed
    assert p.manual_hold_s() == 0
    assert p.tick()["action"] == "stop"         # drift correction resumes
    assert p.actions == ["stop"]


def test_note_manual_without_action_argument():
    clock = FakeClock()
    p = _policy([{"session_locked": False, "onedrive_running": False}],
                clock=clock, manual_grace_s=60)
    assert p.note_manual() == 1060.0


def test_lock_transition_overrides_a_manual_hold():
    """Locking the machine is an unambiguous intent signal — it clears the hold."""
    clock = FakeClock()
    p = _policy([{"session_locked": True, "onedrive_running": False}],
                clock=clock, manual_grace_s=3600)
    p.last_locked = False                       # so this tick IS a transition
    p.note_manual("stop")
    assert p.manual_hold_s() == 3600
    assert p.tick()["action"] == "start"        # transition wins
    assert p.manual_hold_s() == 0               # hold cleared


# ------------------------------------------------------------ staleness guard --
def test_stale_seeded_sample_is_ignored():
    """collector._load_history() replays the last pre-restart sample as `latest`.

    Latching on it is what set last_locked from 14.7-hour-old data at 04:20 on
    2026-09-28, so the policy must wait for a sample from its own session.
    """
    clock = FakeClock(t=1000.0)
    p = _policy([
        {"session_locked": False, "onedrive_running": True, "_t": 500.0},   # stale
        {"session_locked": False, "onedrive_running": True, "_t": 1010.0},  # fresh
    ], clock=clock)
    assert p.tick() is None                     # ignored
    assert p.last_locked is None                # and did NOT latch
    assert p.tick()["action"] == "stop"         # acts on the fresh sample
    assert p.last_locked is False


def test_sample_without_timestamp_is_treated_as_current():
    """Synthetic/legacy samples carry no _t; they must not be silently discarded."""
    p = _policy([{"session_locked": False, "onedrive_running": True}])
    assert p.tick()["action"] == "stop"


def test_sample_exactly_at_start_time_is_current():
    clock = FakeClock(t=1000.0)
    p = _policy([{"session_locked": False, "onedrive_running": True, "_t": 1000.0}],
                clock=clock)
    assert p.tick()["action"] == "stop"


# -------------------------------------------------------------------- status --
def test_status():
    clock = FakeClock()
    p = _policy([{"session_locked": True, "onedrive_running": True}],
                enabled=True, fix_enabled=True, clock=clock, manual_grace_s=120)
    assert p.status() == {"enabled": True, "last_locked": None,
                          "control_disabled": False,
                          "reconcile": True, "manual_hold_s": 0}
    p.tick()
    assert p.status()["last_locked"] is True
    p.note_manual("stop")
    assert p.status()["manual_hold_s"] == 120


def test_status_reflects_reconcile_flag():
    p = _policy([{"session_locked": True, "onedrive_running": True}],
                reconcile=False)
    assert p.status()["reconcile"] is False
