"""One-agent usability-check controller (offline demonstration).

Isolated from the economic pilot path: proves a single farmer agent can
OBSERVE and ADAPT — plant a slot, see that it is occupied, and choose
differently next — using only accurate observations, never strategy.

Hard bounds (all enforced, all tested):
- exactly one agent (exp-01 farmer profile)
- at most MAX_BILLED_CALLS billed model calls. This is the key
  distinction: empty/non-action responses are BILLED, so the limit
  counts billed calls (runner.model_calls), not signed actions.
- DOLLAR_CAP hard cap via the runner's fail-closed budget machinery
  (worst-case pre-call gate + post-call hard stop).
- early stop on independently observed adaptation, defined
  BEHAVIORALLY from the action record — never from model self-report:
  after a successful plant of slot s, or a 400 occupied-slot rejection
  on slot s, the agent's next farming action targets a different slot,
  or it takes a different action.

Offline only: scripted adapters, zero spend. Exact prompt/response /
action / result records (hashes, no key material) are kept exactly like
the main runner, and the archive verifies.

No trade hints, no cooperation guidance, no strategy anywhere.
"""

from __future__ import annotations

import time
from pathlib import Path

import phase2_runner as pr

# Usability-check envelope (pre-registered, ChatGPT-reviewed).
MAX_BILLED_CALLS = 10
DOLLAR_CAP = 1.00
FARMER_PROFILE = "exp-01"

FARMING_ACTIONS = ("farm_plant", "farm_harvest")


def _ev(entry: dict) -> dict:
    """Trim an action-history entry to its observational fields."""
    return {
        "tick_seq": entry.get("tick_seq"),
        "action": entry.get("action"),
        "params": entry.get("params"),
        "http_status": entry.get("http_status"),
        "response": entry.get("response"),
        "ap_delta": entry.get("ap_delta"),
        "inventory_delta": entry.get("inventory_delta"),
        "pos_after": entry.get("pos_after"),
    }


def _is_trigger(entry: dict) -> tuple[bool, object]:
    """A farming trigger: a successful plant on slot s, or a 400
    occupied-slot rejection on slot s. Returns (is_trigger, slot)."""
    if entry.get("action") not in FARMING_ACTIONS:
        return False, None
    slot = (entry.get("params") or {}).get("slot")
    status = entry.get("http_status")
    response = str(entry.get("response") or "")
    planted = status == 200
    rejected_occupied = (status == 400
                         and ("not empty" in response
                              or "growing" in response))
    return (planted or rejected_occupied), slot


def detect_adaptation(history: list[dict]) -> dict | None:
    """Scan the action record for observed adaptation.

    Defined behaviorally: after a farming trigger (successful plant of
    slot s, or a 400 occupied-slot rejection on slot s), the agent's
    next farming action targets a different slot, or it takes a
    different action. The trigger cited is the most recent one before
    the adapted action, so the evidence reads as "saw X, chose
    differently". Empty/parse-failure ticks are not decisions and are
    skipped. Returns an evidence dict, or None.
    """
    for j, later in enumerate(history):
        action = later.get("action")
        if not action or action == "no_action":
            continue  # not a decision
        # Most recent trigger before this action.
        trigger_idx = None
        for i in range(j - 1, -1, -1):
            is_trig, _ = _is_trigger(history[i])
            if is_trig:
                trigger_idx = i
                break
        if trigger_idx is None:
            continue
        trig = history[trigger_idx]
        _, slot = _is_trigger(trig)
        if action in FARMING_ACTIONS:
            later_slot = (later.get("params") or {}).get("slot")
            if later_slot != slot:
                return {"mode": "different_slot",
                        "trigger": _ev(trig),
                        "adapted_action": _ev(later)}
            # same slot again: not adaptation yet; keep watching
        else:
            return {"mode": "different_action",
                    "trigger": _ev(trig),
                    "adapted_action": _ev(later)}
    return None


class UsabilityController:
    """Runs the one-farmer usability check to a hard stop.

    Reuses the Phase2Runner's observation interface, budget machinery,
    and evidence archive; adds only the adaptation early-stop. The
    economic pilot path is untouched.
    """

    def __init__(self, archive_dir: str | Path,
                 adapter: pr.ModelAdapter,
                 max_billed_calls: int = MAX_BILLED_CALLS,
                 dollar_cap: float = DOLLAR_CAP):
        cfg = pr.RunConfig(
            dry_run=True,
            tick_interval_seconds=0,
            max_model_calls=max_billed_calls,
            dollar_cap=dollar_cap,
            # Same response budget as the calibrated pilot: the check
            # measures observation, not a cheaper model call.
            per_tick_out_cap=1500,
            max_empty_streak=4,
        )
        self.runner = pr.Phase2Runner(cfg, adapter, Path(archive_dir))
        self.history: list[dict] = []  # full action history (unbounded)
        self._seen_tick_seq: set = set()
        self.stop_reason: str | None = None
        self.adaptation_evidence: dict | None = None

    def setup(self):
        self.runner.setup_world(agent_names=(FARMER_PROFILE,))
        assert len(self.runner.agents) == 1, self.runner.agents
        assert self.runner.agents[0]["name"] == FARMER_PROFILE
        self.agent = self.runner.agents[0]
        self.runner.log({
            "event": "usability_check_start",
            "harness": pr.HARNESS_VERSION,
            "prompt_variant": pr.PROMPT_VARIANT,
            "agent": FARMER_PROFILE,
            "max_billed_calls": self.runner.cfg.max_model_calls,
            "dollar_cap": self.runner.cfg.dollar_cap,
        })

    def _sync_history(self):
        """Fold newly recorded actions into the full local history.

        The prompt carries only a bounded window (RECENT_ACTIONS_KEPT);
        the controller keeps everything, keyed by tick_seq."""
        for e in self.agent.get("recent_actions", []):
            ts = e.get("tick_seq")
            if ts is not None and ts not in self._seen_tick_seq:
                self._seen_tick_seq.add(ts)
                self.history.append(e)

    def run(self) -> str:
        """Run to a hard stop. Returns the stop reason."""
        self.setup()
        reason = "loop_complete"
        try:
            while True:
                # Billed-call limit AND $ cap, worst-case pre-call gate.
                if not self.runner.check_budget_before_call():
                    reason = "call_budget"
                    self.runner.log({
                        "event": "usability_stop",
                        "reason": reason,
                        "model_calls": self.runner.model_calls,
                        "spend_dollars":
                            round(self.runner.dollars(), 4),
                    })
                    break
                st = self.runner.tick(self.agent)
                self._sync_history()
                if st == "halt":
                    # Runner halted: empty-streak, post-call $ cap,
                    # or uncertain billing. Reason is in the log.
                    reason = "runner_halt"
                    break
                evidence = detect_adaptation(self.history)
                if evidence is not None:
                    self.adaptation_evidence = evidence
                    self.runner.log({
                        "event": "usability_adaptation_observed",
                        "ts": time.time(),
                        "model_calls": self.runner.model_calls,
                        "spend_dollars":
                            round(self.runner.dollars(), 4),
                        "evidence": evidence,
                    })
                    reason = "adaptation_observed"
                    break
        finally:
            self.runner.finalize_archive(reason)
        self.stop_reason = reason
        return reason
