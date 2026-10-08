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
- WALL_CLOCK_SECONDS wall-clock limit on the whole check.
- early stop on independently observed adaptation, defined
  BEHAVIORALLY from the action record — never from model self-report.
  The ONLY outcome that counts as adaptation: a planting trigger
  (successful plant of slot S, or a 400 occupied-slot rejection on
  slot S), followed by the agent independently selecting and
  SUCCESSFULLY planting a DIFFERENT slot. A chat, move, wait, or any
  other action after planting is recorded as INCONCLUSIVE — it does
  not demonstrate learning from the farm's state. A successful
  harvest is never a planting trigger.

Wait handling: in live mode the controller never busy-spins while the
agent waits — it sleeps in bounded polls (dry-run mode skips waits
deterministically, as the runner does). The 10-call and $1 caps are
preserved exactly.

Live entry point: run_live_check() constructs the controller with the
PINNED real model adapter. There is deliberately no adapter parameter,
so no scripted adapter can be substituted into a live invocation; and
a non-dry-run controller REFUSES any adapter that is not the pinned
real one. Missing credentials fail loudly via AdapterConfigError
before any world setup or spend.

Offline only unless run_live_check is explicitly invoked: scripted
adapters, zero spend. Exact prompt/response / action / result records
(hashes, no key material) are kept exactly like the main runner, and
the archive verifies.

No trade hints, no cooperation guidance, no strategy anywhere.
"""

from __future__ import annotations

import time
from pathlib import Path

import phase2_runner as pr

# Usability-check envelope (pre-registered, ChatGPT-reviewed).
MAX_BILLED_CALLS = 10
DOLLAR_CAP = 1.00
WALL_CLOCK_SECONDS = 30 * 60
FARMER_PROFILE = "exp-01"

# Seconds between wake polls while the agent waits (live mode only).
WAIT_POLL_SECONDS = 30.0


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


def _summarize(entry: dict) -> str:
    """One-line human-readable summary for reviewer evidence."""
    e = _ev(entry)
    return (f"{e['action']} {e['params']} -> {e['http_status']} "
            f"{str(e['response'])[:80]!r} (tick {e['tick_seq']})")


def _is_trigger(entry: dict) -> tuple[bool, object]:
    """A planting trigger: a successful farm_plant on slot s (200), or
    a 400 occupied-slot rejection on slot s. Returns (is_trigger, slot).

    farm_harvest is NEVER a trigger — a successful harvest says
    nothing about whether the agent observed an occupied slot."""
    if entry.get("action") != "farm_plant":
        return False, None
    slot = (entry.get("params") or {}).get("slot")
    status = entry.get("http_status")
    response = str(entry.get("response") or "")
    planted = status == 200
    rejected_occupied = (status == 400
                         and ("not empty" in response
                              or "growing" in response))
    return (planted or rejected_occupied), slot


def _most_recent_trigger(history: list[dict], before: int) -> int | None:
    """Index of the most recent planting trigger before position
    `before`, or None."""
    for i in range(before - 1, -1, -1):
        is_trig, _ = _is_trigger(history[i])
        if is_trig:
            return i
    return None


def detect_adaptation(history: list[dict]) -> dict | None:
    """Scan the action record for STRONG evidence of adaptation.

    The only outcome that counts: a planting trigger (successful plant
    of slot s, or a 400 occupied-slot rejection on slot s), followed
    by the agent independently selecting and SUCCESSFULLY planting a
    DIFFERENT slot (a 200 on a different slot proves it was empty —
    planting an occupied slot returns 400). Empty/parse-failure ticks
    are not decisions and are skipped. Returns an evidence dict with
    reviewer-readable trigger/adapted summaries, or None.
    """
    for j, later in enumerate(history):
        if later.get("action") != "farm_plant":
            continue  # only a successful replant can be strong evidence
        if not later.get("tick_seq"):
            continue  # not a signed decision
        trigger_idx = _most_recent_trigger(history, j)
        if trigger_idx is None:
            continue
        trig = history[trigger_idx]
        _, slot = _is_trigger(trig)
        later_slot = (later.get("params") or {}).get("slot")
        if later_slot == slot:
            continue  # same slot again: the non-adaptive pattern
        if later.get("http_status") != 200:
            continue  # different slot but the plant failed: no evidence
        return {"mode": "adaptation_observed",
                "trigger": _ev(trig),
                "adapted_action": _ev(later),
                "trigger_summary": _summarize(trig),
                "adapted_summary": _summarize(later),
                "note": ("strong evidence: after observing slot "
                         f"{slot} occupied/planted, the agent "
                         f"independently planted different slot "
                         f"{later_slot} successfully")}
    return None


def detect_inconclusive(history: list[dict],
                        reported: set) -> list[dict]:
    """Find post-trigger behavioral changes that are NOT adaptation.

    A chat, move, wait, or any other non-farming action after a
    planting trigger is recorded as INCONCLUSIVE: it is a different
    action, but it does not demonstrate learning from the farm's
    state. `reported` holds tick_seqs already logged, so each event is
    returned once. Narrow and explicit by design."""
    out = []
    for j, later in enumerate(history):
        action = later.get("action")
        if not action or action in ("no_action", "farm_plant"):
            continue
        ts = later.get("tick_seq")
        if ts is None or ts in reported:
            continue
        trigger_idx = _most_recent_trigger(history, j)
        if trigger_idx is None:
            continue
        trig = history[trigger_idx]
        reported.add(ts)
        out.append({"mode": "inconclusive",
                    "trigger": _ev(trig),
                    "action": _ev(later),
                    "trigger_summary": _summarize(trig),
                    "action_summary": _summarize(later),
                    "note": ("behavioral change after a planting "
                             "trigger, but not evidence of learning "
                             "from the farm's state")})
    return out


class UsabilityController:
    """Runs the one-farmer usability check to a hard stop.

    Reuses the Phase2Runner's observation interface, budget machinery,
    and evidence archive; adds only the adaptation early-stop, wait
    handling without busy-spinning, and a wall-clock limit. The
    economic pilot path is untouched.
    """

    def __init__(self, archive_dir: str | Path,
                 adapter: pr.ModelAdapter,
                 max_billed_calls: int = MAX_BILLED_CALLS,
                 dollar_cap: float = DOLLAR_CAP,
                 wall_clock_seconds: float = WALL_CLOCK_SECONDS,
                 dry_run: bool = True):
        if not dry_run and not (
                isinstance(adapter, pr.PinnedModelAdapter)
                and adapter.enforces_limits):
            # No silent substitution: a live (non-dry-run) invocation
            # requires the pinned REAL model adapter. Scripted and test
            # adapters are dry-run-only by design.
            raise pr.AdapterConfigError(
                "UsabilityController: dry_run=False requires the pinned "
                "real model adapter (PinnedModelAdapter); refusing a "
                f"scripted/test adapter ({type(adapter).__name__}). "
                "Use run_live_check() for live invocations.")
        cfg = pr.RunConfig(
            dry_run=dry_run,
            tick_interval_seconds=0,
            max_model_calls=max_billed_calls,
            dollar_cap=dollar_cap,
            max_wall_seconds=int(wall_clock_seconds),
            # Same response budget as the calibrated pilot: the check
            # measures observation, not a cheaper model call.
            per_tick_out_cap=1500,
            max_empty_streak=4,
        )
        self.runner = pr.Phase2Runner(cfg, adapter, Path(archive_dir))
        self.history: list[dict] = []  # full action history (unbounded)
        self._seen_tick_seq: set = set()
        self._inconclusive_reported: set = set()
        self._wait_poll_seconds = WAIT_POLL_SECONDS
        self._sleeper = time.sleep  # replaceable in tests
        self._start_ts: float | None = None
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
            "wall_clock_seconds": self.runner.cfg.max_wall_seconds,
            "dry_run": self.runner.cfg.dry_run,
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

    def _wait_pause_seconds(self, calls_before: int) -> float:
        """Seconds to pause before the next tick, or 0.

        In dry-run mode waits are skipped deterministically (the
        runner sets wait_until=0), so this is always 0 there and tests
        never sleep. In live mode, when a tick consumed no model call
        and the agent is still waiting, pause for the bounded poll
        interval instead of busy-spinning."""
        if self.runner.cfg.dry_run:
            return 0.0
        if self.runner.model_calls != calls_before:
            return 0.0  # a model call was consumed; not a wait tick
        remaining = self.agent.get("wait_until", 0) - time.time()
        if remaining <= 0:
            return 0.0
        return min(remaining, self._wait_poll_seconds)

    def _log_inconclusive(self):
        for ev in detect_inconclusive(self.history,
                                      self._inconclusive_reported):
            self.runner.log({"event": "usability_inconclusive",
                             "ts": time.time(), **ev})

    def run(self) -> str:
        """Run to a hard stop. Returns the stop reason."""
        self.setup()
        self._start_ts = time.time()
        reason = "loop_complete"
        try:
            while True:
                # Wall-clock limit on the whole check (recorded).
                if time.time() - self._start_ts > \
                        self.runner.cfg.max_wall_seconds:
                    reason = "wall_clock"
                    self.runner.log({
                        "event": "usability_stop",
                        "reason": reason,
                        "model_calls": self.runner.model_calls,
                        "spend_dollars":
                            round(self.runner.dollars(), 4),
                    })
                    break
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
                calls_before = self.runner.model_calls
                st = self.runner.tick(self.agent)
                self._sync_history()
                if st == "halt":
                    # Runner halted: empty-streak, post-call $ cap,
                    # or uncertain billing. Reason is in the log.
                    reason = "runner_halt"
                    break
                # No busy-spin: if the tick consumed no model call and
                # the agent is waiting, pause instead of looping.
                pause = self._wait_pause_seconds(calls_before)
                if pause > 0:
                    self._sleeper(pause)
                    continue
                self._log_inconclusive()
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


def run_live_check(archive_dir: str | Path, *,
                   max_billed_calls: int = MAX_BILLED_CALLS,
                   dollar_cap: float = DOLLAR_CAP,
                   wall_clock_seconds: float = WALL_CLOCK_SECONDS) -> str:
    """EXPLICIT live entry point for the one-agent usability check.

    Constructs the controller with the PINNED real model adapter.
    There is deliberately NO adapter parameter: no scripted or test
    adapter can be substituted into a live invocation through this
    path. Raises AdapterConfigError immediately — before any world
    setup or spend — if PHASE2_MODEL_API_URL / PHASE2_MODEL_API_KEY
    are missing. There is no silent fallback to the scripted stub.

    Do not invoke without explicit approval: this spends real money
    (bounded by the $1.00 hard cap and 10 billed calls).
    """
    adapter = pr.PinnedModelAdapter()  # loud failure if env missing
    ctl = UsabilityController(
        archive_dir, adapter,
        max_billed_calls=max_billed_calls,
        dollar_cap=dollar_cap,
        wall_clock_seconds=wall_clock_seconds,
        dry_run=False)
    return ctl.run()
