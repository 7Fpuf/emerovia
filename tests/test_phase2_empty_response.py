"""Phase 2 empty-response handling — offline/stub tests only.

Covers the 2026-10-07 tick-1 abort fix (ChatGPT two-change spec):
1. An empty/zero-content model response (content=null, e.g. reasoning
   exhausted the output cap) becomes a recorded no_action technical
   event — billed call + usage recorded, no world action, NO crash.
2. A bounded consecutive-empty-response rule ends the run as a
   technical stop before it can waste the budget.
3. Revised budget enforcement under the resized per-tick output cap
   (1500) and hard cap ($6.00).
4. An abnormal run exit is never reported as "loop_complete".

HARD BOUNDARIES: no live model calls, no inference spend, no
production, no behavioral experiment. Every test here is offline or
uses scripted adapters against a temp-DB world.
"""

from __future__ import annotations

import json
import sys
from pathlib import Path

import pytest

REPO = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(REPO / "tools"))
sys.path.insert(0, str(REPO / "tests"))

import phase2_runner as pr


def make_runner(tmp_path, **cfg_kw):
    cfg = pr.RunConfig(dry_run=True, dry_run_ticks=4,
                       tick_interval_seconds=0, **cfg_kw)
    archive = tmp_path / "archive"
    return pr.Phase2Runner(cfg, pr.StubAdapter(), archive)


def read_events(runner):
    return [json.loads(l) for l in
            runner.log_path.read_text().splitlines() if l.strip()]


class NullContentAdapter(pr.ModelAdapter):
    """Test double replaying the 2026-10-07 abort shape: billed usage
    present, content null (finish_reason=length). Script entries are
    Optional[str]; None = empty response."""
    name = "test-null-content"
    enforces_limits = True

    def __init__(self, script=None, in_tok=915, out_tok=1500):
        self.script = list(script) if script is not None else [None]
        self.i = 0
        self.in_tok = in_tok
        self.out_tok = out_tok

    def complete(self, prompt):
        step = self.script[self.i % len(self.script)]
        self.i += 1
        return step, self.in_tok, self.out_tok


# ------------------------------------------------- 1. empty response path


def test_empty_response_none_is_no_action_not_crash(tmp_path):
    """The exact abort shape through the full tick pipeline: no
    TypeError, billed usage recorded, no world action taken."""
    def fake_transport(url, headers, payload, timeout):
        return {"choices": [{"message": {"content": None,
                                         "finish_reason": "length"}}],
                "usage": {"prompt_tokens": 915, "completion_tokens": 1500}}

    runner = make_runner(tmp_path)
    runner.adapter = pr.PinnedModelAdapter(
        api_url="http://x", api_key="k", transport=fake_transport)
    runner.setup_world()
    try:
        assert runner.tick(runner.agents[0]) == "ok"  # NOT a crash
        assert runner.model_calls == 1
        assert (runner.spend_in, runner.spend_out) == (915, 1500)
        assert runner.agents[0]["actions"] == 0  # no world action
        assert runner.agents[0]["tick_seq"] == 0
        evs = read_events(runner)
        empties = [e for e in evs
                   if e.get("event") == "tick_empty_response"]
        assert len(empties) == 1
        assert empties[0]["action"] == "no_action"
        assert empties[0]["empty_streak"] == 1
        assert empties[0]["in_tokens"] == 915
        assert empties[0]["out_tokens"] == 1500
        budgets = [e for e in evs if e.get("event") == "budget_record"]
        assert budgets and budgets[0]["in_tokens"] == 915
        # no signed-action tick record was produced
        assert not [e for e in evs if e.get("event") == "tick"
                    and "http_status" in e]
    finally:
        runner.logf.close()


def test_empty_response_whitespace_string(tmp_path):
    runner = make_runner(tmp_path)
    runner.adapter = NullContentAdapter(script=["   \n "],
                                        in_tok=100, out_tok=10)
    runner.setup_world()
    try:
        assert runner.tick(runner.agents[0]) == "ok"
        evs = read_events(runner)
        assert any(e.get("event") == "tick_empty_response"
                   for e in evs)
        assert runner.agents[0]["actions"] == 0
    finally:
        runner.logf.close()


def test_billed_out_at_cap_is_not_an_anomaly(tmp_path):
    """billed_out == per_tick_out_cap is legal (only ABOVE the cap is
    a BilledCapAnomaly): a reasoning-exhausted call at exactly the
    cap must flow into the empty-response path, not halt the run."""
    def fake_transport(url, headers, payload, timeout):
        return {"choices": [{"message": {"content": None}}],
                "usage": {"prompt_tokens": 915, "completion_tokens": 1500}}

    a = pr.PinnedModelAdapter(api_url="http://x", api_key="k",
                              transport=fake_transport)
    text, tin, tout = a.complete("hi")
    assert text is None and (tin, tout) == (915, 1500)  # no raise

    def bad_transport(url, headers, payload, timeout):
        return {"choices": [{"message": {"content": None}}],
                "usage": {"prompt_tokens": 915, "completion_tokens": 1501}}

    b = pr.PinnedModelAdapter(api_url="http://x", api_key="k",
                              transport=bad_transport)
    with pytest.raises(pr.BilledCapAnomaly):
        b.complete("hi")


# --------------------------------------- 2. consecutive-empty bound


def test_consecutive_empty_responses_halt_at_bound(tmp_path):
    runner = make_runner(tmp_path)  # max_empty_streak=4 default
    # The 4-empty safeguard stays active under the 1500/$6.00 config.
    assert runner.cfg.per_tick_out_cap == 1500
    assert runner.cfg.dollar_cap == 6.00
    assert runner.cfg.max_empty_streak == 4
    runner.adapter = NullContentAdapter()  # always None
    runner.setup_world()
    try:
        assert runner.tick(runner.agents[0]) == "ok"   # streak 1
        assert runner.tick(runner.agents[0]) == "ok"   # streak 2
        assert runner.tick(runner.agents[0]) == "ok"   # streak 3
        assert runner.tick(runner.agents[0]) == "halt"  # streak 4
        assert runner.model_calls == 4  # all four calls were billed
        evs = read_events(runner)
        stops = [e for e in evs
                 if e.get("event") == "technical_stop"
                 and e.get("reason") == "consecutive_empty_responses"]
        assert len(stops) == 1
        assert stops[0]["empty_streak"] == 4
        assert runner.agents[0]["actions"] == 0
    finally:
        runner.logf.close()


def test_empty_streak_resets_on_content(tmp_path):
    valid = json.dumps({"action": "wait", "params": {"minutes": 1}})
    runner = make_runner(tmp_path)
    runner.adapter = NullContentAdapter(
        script=[None, None, valid, None, None, None])
    runner.setup_world()
    try:
        assert runner.tick(runner.agents[0]) == "ok"  # streak 1
        assert runner.tick(runner.agents[0]) == "ok"  # streak 2
        assert runner.tick(runner.agents[0]) == "ok"  # content: reset
        assert runner.tick(runner.agents[0]) == "ok"  # streak 1
        assert runner.tick(runner.agents[0]) == "ok"  # streak 2
        assert runner.tick(runner.agents[0]) == "ok"  # streak 3, no halt
        evs = read_events(runner)
        assert not [e for e in evs
                    if e.get("reason") == "consecutive_empty_responses"]
    finally:
        runner.logf.close()


def test_custom_empty_streak_bound(tmp_path):
    runner = make_runner(tmp_path, max_empty_streak=2)
    runner.adapter = NullContentAdapter()
    runner.setup_world()
    try:
        assert runner.tick(runner.agents[0]) == "ok"
        assert runner.tick(runner.agents[0]) == "halt"
    finally:
        runner.logf.close()


# --------------------------------------- 3. revised budget enforcement


def test_budget_precheck_uses_resized_caps(tmp_path):
    runner = make_runner(tmp_path)  # dollar_cap=6.00 default
    assert runner.cfg.dollar_cap == 6.00
    assert runner.cfg.per_tick_out_cap == 1500
    worst = 6000 / 1e6 * 1.25 + 1500 / 1e6 * 4.25
    assert worst == pytest.approx(0.013875)  # worst-case next call
    assert 400 * worst == pytest.approx(5.55)  # < $6.00 hard cap
    assert runner.check_budget_before_call() is True
    # Push spend so the worst-case next call no longer fits.
    target = 6.00 - worst + 0.001
    runner.spend_out = int(target / 4.25 * 1e6)
    assert runner.check_budget_before_call() is False
    evs = read_events(runner)
    assert any(e.get("event") == "technical_stop"
               and e.get("reason") == "budget_cap_reached" for e in evs)
    runner.logf.close()


def test_budget_hard_stop_post_call_at_600(tmp_path):
    """Defense in depth under the $6.00 cap: even if the pre-call
    check passed, billed spend past the cap halts immediately as a
    technical stop, with the cap recorded as 6.00."""
    runner = make_runner(tmp_path)  # dollar_cap=6.00 default
    runner.adapter = NullContentAdapter(in_tok=6000, out_tok=1500)
    runner.setup_world()
    try:
        # Bypass the pre-call gate to exercise the post-call layer.
        runner.check_budget_before_call = lambda: True
        # Pre-seed spend so one maxed-out call breaches $6.00.
        runner.spend_out = int(5.99 / 4.25 * 1e6)
        assert runner.tick(runner.agents[0]) == "halt"
        evs = read_events(runner)
        stops = [e for e in evs
                 if e.get("event") == "technical_stop"
                 and e.get("reason") == "budget_cap_exceeded"]
        assert len(stops) == 1
        assert stops[0]["dollar_cap"] == 6.00
    finally:
        runner.logf.close()


def test_build_adapter_wires_config_caps(monkeypatch):
    monkeypatch.setenv(pr.ENV_API_URL, "https://models.example/v1/chat")
    monkeypatch.setenv(pr.ENV_API_KEY, "test-key")
    seen = {}

    def fake_transport(url, headers, payload, timeout):
        seen["max_tokens"] = payload["max_tokens"]
        return {"choices": [{"message": {"content": "{}"}}],
                "usage": {"prompt_tokens": 10, "completion_tokens": 5}}

    cfg = pr.RunConfig()  # resized defaults: 1500 out, $6.00 cap
    a = pr.build_adapter(False, cfg)
    assert isinstance(a, pr.PinnedModelAdapter)
    assert a.per_tick_out_cap == 1500
    a.transport = fake_transport
    a.complete("hi")
    assert seen["max_tokens"] == 1500  # wire cap == config cap

    cfg2 = pr.RunConfig(per_tick_out_cap=1234)
    a2 = pr.build_adapter(False, cfg2)
    assert a2.per_tick_out_cap == 1234  # custom caps propagate

    a3 = pr.build_adapter(False)  # legacy call shape still works
    assert a3.per_tick_out_cap == 1500


# --------------------------------------- 4. honest run_end reasons


def test_run_exception_reason_not_loop_complete(tmp_path, monkeypatch):
    runner = make_runner(tmp_path)
    runner.setup_world()

    def boom(agent):
        raise RuntimeError("simulated tick crash")

    monkeypatch.setattr(runner, "tick", boom)
    with pytest.raises(RuntimeError):
        runner.run()  # re-raised after recording; archive finalized
    evs = read_events(runner)
    assert any(e.get("event") == "run_exception" for e in evs)
    ends = [e for e in evs if e.get("event") == "run_end"]
    assert ends and ends[-1]["reason"] == "run_exception"
    assert ends[-1]["reason"] != "loop_complete"


def test_setup_world_survives_spawn_on_claimed_tile(tmp_path):
    """Regression: spawn() avoids agent-occupied tiles but not
    claimed-yet-unoccupied ones. If the second agent lands on the
    first agent's claimed farm tile, setup must relocate it and claim
    a free tile instead of crashing with 'tile already claimed'.

    (Root cause of the intermittent
    test_consecutive_empty_responses_halt_at_bound failure: a ~0.1%
    SystemRandom spawn collision during setup_world, unrelated to the
    empty-response safeguard itself.)
    """
    import test_econ_validation_phase1 as T

    runner = make_runner(tmp_path)
    runner.setup_world(agent_names=("exp-01",))
    try:
        # exp-01's claimed farm tile (claimed, unoccupied -> in spawn pool)
        fx, fy = runner.agents[0]["farm_xy"] \
            if "farm_xy" in runner.agents[0] else (None, None)
        if fx is None:
            conn = T.db(runner.db_path)
            try:
                row = conn.execute(
                    "SELECT x, y FROM structures WHERE kind = 'farm'"
                    " LIMIT 1").fetchone()
                fx, fy = row["x"], row["y"]
            finally:
                conn.close()
        # register + spawn exp-02, then force the rare collision by
        # placing it on exp-01's claimed farm tile
        key2 = T.make_key()
        T.register(runner.client, "exp-02", key2)
        T.spawn(runner.client, key2)
        pk2 = T.pubkey_hex(key2)
        conn = T.db(runner.db_path)
        try:
            conn.execute(
                "UPDATE agent_world SET x = ?, y = ? WHERE agent_id ="
                " (SELECT id FROM agents WHERE pubkey = ?)",
                (fx, fy, pk2))
            conn.commit()
        finally:
            conn.close()
        # must relocate + claim, not raise
        x, y = runner._claim_spawn_tile(T, key2, pk2, fx, fy, "exp-02")
        assert (x, y) != (fx, fy), "should have relocated off the tile"
        conn = T.db(runner.db_path)
        try:
            owner = conn.execute(
                "SELECT owner_pubkey FROM claims WHERE x = ? AND y = ?",
                (x, y)).fetchone()
            pos = conn.execute(
                "SELECT x, y FROM agent_world WHERE agent_id ="
                " (SELECT id FROM agents WHERE pubkey = ?)",
                (pk2,)).fetchone()
        finally:
            conn.close()
        assert owner is not None and owner["owner_pubkey"] == pk2
        assert (pos["x"], pos["y"]) == (x, y)
    finally:
        runner.logf.close()
