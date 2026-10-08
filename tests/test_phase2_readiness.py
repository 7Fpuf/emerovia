"""Phase 2 final operational readiness — offline/stub tests only.

Covers the three ChatGPT-gated operational items:
1. Real pinned-model adapter (stub is dry-run-only; non-dry-run without
   credentials FAILS LOUDLY, never silently falls back).
2. Hard budget enforcement at the API boundary (pre-send limit
   rejection, real billed-usage accounting, $ cap hard stop, action
   cap counts failed signed attempts).
3. Complete observation pipeline + evidence archive (full public world
   info in prompts, snapshots/ledger/budget archived, resume works).

HARD BOUNDARIES: no live model calls, no inference spend, no
production, no behavioral experiment. Every test here is offline or
uses the scripted stub against a temp-DB world.
"""

from __future__ import annotations

import hashlib
import json
import os
import sys
from pathlib import Path

import pytest

REPO = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(REPO / "tools"))
sys.path.insert(0, str(REPO / "tests"))

import phase2_runner as pr


def clear_model_env(monkeypatch):
    monkeypatch.delenv(pr.ENV_API_URL, raising=False)
    monkeypatch.delenv(pr.ENV_API_KEY, raising=False)


def make_runner(tmp_path, **cfg_kw):
    cfg = pr.RunConfig(dry_run=True, dry_run_ticks=4,
                       tick_interval_seconds=0, **cfg_kw)
    archive = tmp_path / "archive"
    return pr.Phase2Runner(cfg, pr.StubAdapter(), archive)


# ---------------------------------------------------------------- 1. adapter


def test_dry_run_selects_stub():
    a = pr.build_adapter(True)
    assert isinstance(a, pr.StubAdapter)
    assert not a.enforces_limits


def test_nondryrun_without_creds_fails_loud(monkeypatch):
    clear_model_env(monkeypatch)
    with pytest.raises(pr.AdapterConfigError) as ei:
        pr.build_adapter(False)
    assert "stub" in str(ei.value).lower()  # names the forbidden fallback


def test_nondryrun_with_creds_builds_real_adapter(monkeypatch):
    monkeypatch.setenv(pr.ENV_API_URL, "https://models.example/v1/chat")
    monkeypatch.setenv(pr.ENV_API_KEY, "test-key")
    a = pr.build_adapter(False)
    assert isinstance(a, pr.PinnedModelAdapter)
    assert a.enforces_limits
    assert a.model == pr.PINNED_MODEL


def test_real_adapter_mocked_transport():
    seen = {}

    def fake_transport(url, headers, payload, timeout):
        seen.update(payload=payload, auth=headers["Authorization"])
        assert payload["max_tokens"] == 1500  # resized 2026-10-07: see
        # RunConfig.per_tick_out_cap (calibration: 1000 failed 3/3,
        # 1500 succeeded 3/3 on the exact first-tick prompt)
        assert payload["model"] == "muse-spark-1.3"
        return {"choices": [{"message": {"content": '{"action":"wait",'
                                                    '"params":{"minutes":1}}'}}],
                "usage": {"prompt_tokens": 1200, "completion_tokens": 42}}

    a = pr.PinnedModelAdapter(api_url="http://x", api_key="k",
                              transport=fake_transport)
    text, tin, tout = a.complete("hello")
    assert (tin, tout) == (1200, 42)  # REAL billed usage, not estimates
    assert json.loads(text)["action"] == "wait"
    assert seen["auth"] == "Bearer k"


def test_real_adapter_rejects_oversize_prompt_before_sending():
    calls = []

    def fake_transport(url, headers, payload, timeout):
        calls.append(1)
        raise AssertionError("must not be called")

    a = pr.PinnedModelAdapter(api_url="http://x", api_key="k",
                              per_tick_in_cap=100,
                              transport=fake_transport)
    with pytest.raises(pr.TokenLimitExceeded):
        a.complete("x" * 10_000)
    assert calls == []  # rejected BEFORE sending: nothing billed


def test_real_adapter_requires_billed_usage():
    def fake_transport(url, headers, payload, timeout):
        return {"choices": [{"message": {"content": "{}"}}]}  # no usage

    a = pr.PinnedModelAdapter(api_url="http://x", api_key="k",
                              transport=fake_transport)
    with pytest.raises(pr.ModelAPIError):
        a.complete("hi")


def test_real_adapter_billed_anomaly():
    def fake_transport(url, headers, payload, timeout):
        return {"choices": [{"message": {"content": "{}"}}],
                "usage": {"prompt_tokens": 10, "completion_tokens": 99999}}

    a = pr.PinnedModelAdapter(api_url="http://x", api_key="k",
                              per_tick_out_cap=500,
                              transport=fake_transport)
    with pytest.raises(pr.BilledCapAnomaly):
        a.complete("hi")


def test_billed_anomaly_halts_run(tmp_path):
    """A billed-usage anomaly is a technical stop, never silent."""
    def fake_transport(url, headers, payload, timeout):
        return {"choices": [{"message": {"content": "{}"}}],
                "usage": {"prompt_tokens": 10, "completion_tokens": 99999}}

    runner = make_runner(tmp_path)
    runner.adapter = pr.PinnedModelAdapter(
        api_url="http://x", api_key="k", transport=fake_transport)
    runner.setup_world()
    try:
        assert runner.tick(runner.agents[0]) == "halt"
        evs = [json.loads(l) for l in
               runner.log_path.read_text().splitlines()]
        assert any(e.get("event") == "technical_stop"
                   and e.get("reason") == "billed_token_anomaly"
                   for e in evs)
    finally:
        runner.logf.close()


def test_main_refuses_nondryrun_without_creds(monkeypatch, tmp_path):
    clear_model_env(monkeypatch)
    rc = pr.main(["--archive", str(tmp_path / "a")])
    assert rc == 2  # loud failure, no silent stub


def test_main_dryrun_wires_stub(monkeypatch, tmp_path):
    got = {}

    class FakeRunner:
        def __init__(self, cfg, adapter, archive_dir, log_name="run.jsonl",
                     resume=False):
            got["adapter"] = adapter
            self.archive_dir = archive_dir
            self._cfg = cfg
            self.model_calls = 0

        def dollars(self):
            return 0.0

        def run(self, max_additional_ticks=None):
            got["ran"] = True

    monkeypatch.setattr(pr, "Phase2Runner", FakeRunner)
    rc = pr.main(["--dry-run", "--ticks", "1",
                  "--archive", str(tmp_path / "a")])
    assert rc == 0
    assert isinstance(got["adapter"], pr.StubAdapter)
    assert got["ran"]


# ---------------------------------------------------------------- 2. budget


class FixedBilledAdapter(pr.ModelAdapter):
    """Test double: behaves like the real adapter (enforces limits,
    reports fixed BILLED usage)."""
    name = "test-fixed-billed"
    enforces_limits = True

    def __init__(self, in_tok=100, out_tok=10,
                 script=({"action": "wait",
                          "params": {"minutes": 1}},)):
        self.in_tok = in_tok
        self.out_tok = out_tok
        self.script = list(script)
        self.i = 0

    def complete(self, prompt):
        step = self.script[self.i % len(self.script)]
        self.i += 1
        return json.dumps(step), self.in_tok, self.out_tok


def read_events(runner):
    return [json.loads(l) for l in
            runner.log_path.read_text().splitlines() if l.strip()]


def test_real_usage_accounted_at_face_value_not_clipped(tmp_path):
    """Billed usage is accounted exactly — never min()'d down."""
    runner = make_runner(tmp_path, dollar_cap=100.0)
    runner.adapter = FixedBilledAdapter(in_tok=5900, out_tok=499)
    runner.setup_world()
    try:
        assert runner.tick(runner.agents[0]) == "ok"
        assert runner.spend_in == 5900
        assert runner.spend_out == 499
        evs = read_events(runner)
        b = [e for e in evs if e.get("event") == "budget_record"]
        assert b and b[0]["in_tokens"] == 5900
    finally:
        runner.logf.close()


def test_budget_hard_stop_post_call(tmp_path):
    """Defense in depth: even if the pre-call check passed, billed
    spend past the cap halts immediately as a technical stop."""
    runner = make_runner(tmp_path, dollar_cap=0.000001)
    runner.adapter = FixedBilledAdapter(in_tok=100, out_tok=10)
    runner.setup_world()
    try:
        # Bypass the pre-call gate to exercise the post-call layer.
        runner.check_budget_before_call = lambda: True
        assert runner.tick(runner.agents[0]) == "halt"
        evs = read_events(runner)
        assert any(e.get("event") == "technical_stop"
                   and e.get("reason") == "budget_cap_exceeded"
                   for e in evs)
    finally:
        runner.logf.close()


def test_action_cap_counts_failed_signed_attempts(tmp_path):
    """400s (and 409s/transport errors) are signed attempts: they count
    toward the 150-action cap. Unknown actions and waits do not.

    (The scripted stub is shared across agents, so the fixed script
    interleaves: exp-01 hits the 400 path, exp-02 does not.)"""
    runner = make_runner(tmp_path)
    runner.cfg.dry_run_ticks = 6
    runner.run()
    got = {a["name"]: a["actions"] for a in runner.agents}
    assert got == {"exp-01": 3, "exp-02": 2}, got
    evs = read_events(runner)
    bad = [e for e in evs if e.get("http_status") == 400]
    assert bad, "expected a recorded 400 uncovered-offer tick"
    assert all("tick_seq" in e for e in bad)
    # tick_seq contiguous per agent over signed attempts
    seqs = {}
    for e in evs:
        if e.get("event") == "tick" and "tick_seq" in e:
            seqs.setdefault(e["agent"], []).append(e["tick_seq"])
    for agent, s in seqs.items():
        assert s == list(range(1, len(s) + 1)), (agent, s)


def test_unknown_action_not_counted(tmp_path):
    runner = make_runner(tmp_path)
    runner.adapter = FixedBilledAdapter(
        script=({"action": "nope_not_real", "params": {}},))
    runner.setup_world()
    try:
        assert runner.tick(runner.agents[0]) == "ok"
        assert runner.agents[0]["actions"] == 0
        evs = read_events(runner)
        assert any(e.get("http_status") == "rejected" for e in evs)
    finally:
        runner.logf.close()


# ---------------------------------------------------------------- 3. pipeline


def test_prompt_carries_full_observation_pipeline(tmp_path):
    runner = make_runner(tmp_path)
    runner.setup_world()
    try:
        p = runner.build_prompt(runner.agents[0])
        for section in ("core_state", "recent_actions", "open_trade_offers",
                        "recent_chat", "own_structures", "own_discoveries",
                        "nearby_disclosed_tiles", "recent_filled_trades",
                        "agents_visible", "public_recipes"):
            assert f"[{section}]" in p, section
        assert pr.estimate_tokens(p) <= runner.cfg.per_tick_in_cap
        # winter season info is ordinary public planning info
        assert "winter" in p.lower()
        # CONTENT, not just headings (2026-10-08: the farmer's
        # own_structures rendered [] for the whole pilot because the
        # runner filtered on owner_pubkey / read slots — fields the
        # endpoint never returns).
        agent = runner.agents[0]
        structs = json.loads(_section(p, "own_structures"))
        farms = [s for s in structs if s.get("kind") == "farm"]
        assert len(farms) == 1, structs
        farm = farms[0]
        assert farm["name"] == f"{agent['name']} farm"
        assert farm["id"] == agent["farm_id"]
        plots = farm["plots"]
        assert len(plots) == 4
        assert {pl["slot"] for pl in plots} == {0, 1, 2, 3}
        assert all(pl["state"] == "empty" for pl in plots), plots
        # the agent's own furnace is visible too
        kinds = {s.get("kind") for s in structs}
        assert "furnace" in kinds, kinds
    finally:
        runner.logf.close()


def _section(prompt: str, name: str) -> str:
    """Extract one [name] section's body from a built prompt."""
    marker = f"[{name}]\n"
    start = prompt.index(marker) + len(marker)
    nxt = prompt.find("\n[", start)
    return prompt[start:] if nxt == -1 else prompt[start:nxt]


# ---------------------------------------------------------------- 5.
# observation interface (harness 0.2.0 / prompt variant obs-interface-v1)


def test_planted_slot_visible_as_growing_in_next_prompt(tmp_path):
    """The milestone primitive: after a successful plant, the NEXT
    prompt shows that slot as occupied/growing — the agent can see
    the consequence of its own action."""
    runner = make_runner(tmp_path)
    runner.adapter = FixedBilledAdapter(
        script=({"action": "farm_plant", "params": {"slot": 0},
                 "reasoning": "test: plant slot 0"},))
    runner.setup_world()
    try:
        agent = runner.agents[0]
        assert runner.tick(agent) == "ok"
        evs = read_events(runner)
        tick = [e for e in evs if e.get("event") == "tick"
                and e.get("action") == "farm_plant"][-1]
        assert tick["http_status"] == 200, tick
        p = runner.build_prompt(agent)
        structs = json.loads(_section(p, "own_structures"))
        farm = [s for s in structs if s.get("kind") == "farm"][0]
        states = {pl["slot"]: pl["state"] for pl in farm["plots"]}
        assert states[0] == "growing", states
        assert all(states[i] == "empty" for i in (1, 2, 3)), states
        # the successful plant is also in the action history
        hist = json.loads(_section(p, "recent_actions"))
        assert hist[-1]["action"] == "farm_plant"
        assert hist[-1]["http_status"] == 200
    finally:
        runner.logf.close()


def test_rejected_plant_error_visible_in_next_prompt(tmp_path):
    """The 2026-10-08 failure mode: eight 400 rejections the agent
    never saw. Now the error detail reaches the next prompt."""
    runner = make_runner(tmp_path)
    runner.adapter = FixedBilledAdapter(
        script=({"action": "farm_plant", "params": {"slot": 0},
                 "reasoning": "test: plant slot 0"},))
    runner.setup_world()
    try:
        agent = runner.agents[0]
        assert runner.tick(agent) == "ok"   # plant succeeds
        assert runner.tick(agent) == "ok"   # same slot -> 400
        evs = read_events(runner)
        rej = [e for e in evs if e.get("event") == "tick"
               and e.get("http_status") == 400]
        assert rej, "expected a 400 rejection"
        assert "growing, not empty" in rej[-1]["response"]
        p = runner.build_prompt(agent)
        hist = json.loads(_section(p, "recent_actions"))
        assert len(hist) == 2
        assert hist[0]["http_status"] == 200
        last = hist[-1]
        assert last["action"] == "farm_plant"
        assert last["params"] == {"slot": 0}
        assert last["http_status"] == 400
        assert "growing, not empty" in last["response"]
        # and the prompt's world state agrees: slot 0 is growing
        structs = json.loads(_section(p, "own_structures"))
        farm = [s for s in structs if s.get("kind") == "farm"][0]
        states = {pl["slot"]: pl["state"] for pl in farm["plots"]}
        assert states[0] == "growing"
    finally:
        runner.logf.close()


def test_action_history_bounded(tmp_path):
    """recent_actions keeps only the last RECENT_ACTIONS_KEPT entries."""
    runner = make_runner(tmp_path)
    runner.adapter = FixedBilledAdapter(
        script=({"action": "chat", "params": {"text": "t"},
                 "reasoning": "test"},))
    runner.setup_world()
    try:
        agent = runner.agents[0]
        for _ in range(pr.RECENT_ACTIONS_KEPT + 3):
            assert runner.tick(agent) == "ok"
        assert len(agent["recent_actions"]) == pr.RECENT_ACTIONS_KEPT
        p = runner.build_prompt(agent)
        hist = json.loads(_section(p, "recent_actions"))
        assert len(hist) == pr.RECENT_ACTIONS_KEPT
    finally:
        runner.logf.close()


def test_movement_and_discoveries_visible_across_prompts(tmp_path):
    """Movement results and visited tiles are observable across
    consecutive prompts: the agent can see where it went."""
    runner = make_runner(tmp_path)
    runner.adapter = FixedBilledAdapter(script=[
        {"action": "move", "params": {"dir": d}, "reasoning": "test"}
        for d in ("E", "S", "W", "N")])
    runner.setup_world()
    try:
        agent = runner.agents[0]
        p0 = runner.build_prompt(agent)
        d0 = json.loads(_section(p0, "own_discoveries"))
        for _ in range(4):
            assert runner.tick(agent) == "ok"
        evs = read_events(runner)
        moves = [e for e in evs if e.get("event") == "tick"
                 and e.get("action") == "move"]
        assert moves, "no moves recorded"
        ok_moves = [m for m in moves if m["http_status"] == 200]
        assert ok_moves, "no successful move to observe"
        p1 = runner.build_prompt(agent)
        hist = json.loads(_section(p1, "recent_actions"))
        assert len(hist) == 4
        d1 = json.loads(_section(p1, "own_discoveries"))
        assert len(d1) >= len(d0)
        seen = {(d["x"], d["y"]) for d in d1}
        for m in ok_moves:
            assert tuple(m["pos_after"]) in seen, (m, seen)
            entry = [h for h in hist
                     if h.get("tick_seq") == m["tick_seq"]][0]
            assert tuple(entry["pos_after"]) == tuple(m["pos_after"])
    finally:
        runner.logf.close()


def test_discoveries_endpoint_is_private_per_agent(tmp_path):
    """GET /world/discoveries: signed read, returns ONLY the caller's
    own tiles. No parameter can reach another agent's history."""
    runner = make_runner(tmp_path)
    runner.setup_world()
    try:
        T = runner.T
        a, b = runner.agents
        # Spawn records the spawn tile, so snapshot b's tiles first.
        db_before = {(d["x"], d["y"])
                     for d in runner._signed_get(b["key"],
                                                  "/world/discoveries")}
        da_before = {(d["x"], d["y"])
                     for d in runner._signed_get(a["key"],
                                                  "/world/discoveries")}
        moved = False
        for d in ("E", "S", "W", "N"):
            try:
                T.signed_request(runner.client, a["key"], "POST",
                                 "/world/move", {"dir": d})
                moved = True
                break
            except AssertionError:
                continue
        assert moved, "no legal move found from spawn"
        da = runner._signed_get(a["key"], "/world/discoveries")
        assert isinstance(da, list) and len(da) >= 1, da
        assert all(set(d) >= {"x", "y", "terrain", "discovered_at"}
                   for d in da)
        new_a = {(d["x"], d["y"]) for d in da} - da_before
        db = runner._signed_get(b["key"], "/world/discoveries")
        # b sees only its own tiles: none of a's NEW tiles leak, and
        # b's set is unchanged by a's movement.
        assert new_a, "a's move recorded no new tile"
        assert not (new_a & {(d["x"], d["y"]) for d in db})
        assert {(d["x"], d["y"]) for d in db} == db_before
        # unsigned read is refused
        r = runner.client.get("/world/discoveries")
        assert r.status_code == 401, r.status_code
        # limit is honored and bounded
        few = runner._signed_get(a["key"], "/world/discoveries",
                                 {"limit": 1})
        assert len(few) == 1
    finally:
        runner.logf.close()


def test_call_records_persisted_with_hashes_and_verified(tmp_path):
    """Every model call's exact prompt, raw response, parsed action
    and result are persisted with SHA-256 and join the verified
    archive. No key material in the records."""
    rc = pr.main(["--dry-run", "--ticks", "2",
                  "--archive", str(tmp_path / "a")])
    assert rc == 0
    arch = tmp_path / "a"
    calls = sorted((arch / "calls").glob("call_*.json"))
    assert len(calls) == 2, [p.name for p in calls]
    for p in calls:
        r = json.loads(p.read_text())
        assert r["prompt"] and r["raw_response"]
        assert (hashlib.sha256(r["prompt"].encode()).hexdigest()
                == r["prompt_sha256"])
        assert (hashlib.sha256((r["raw_response"] or "").encode()).hexdigest()
                == r["raw_response_sha256"])
        assert r["prompt_variant"] == pr.PROMPT_VARIANT
        assert r["harness"] == pr.HARNESS_VERSION
        assert r["parsed_action"] is not None
        assert r["action_result"] is not None
        blob = json.dumps(r).lower()
        assert "privkey" not in blob and "private" not in blob
    manifest = json.loads((arch / "manifest.json").read_text())
    assert manifest["prompt_variant"] == pr.PROMPT_VARIANT
    assert any(n.startswith("calls/") for n in manifest["files"])
    report = pr.verify_archive(arch)
    assert report["ok"], report["errors"]


def test_resume_restores_farm_id_and_action_history(tmp_path):
    """Resume re-resolves the farm (regression: the old owner_pubkey
    filter left farm_id None) and rebuilds the bounded action
    history from the archived tick records."""
    arch = tmp_path / "a"
    assert pr.main(["--dry-run", "--ticks", "4",
                    "--archive", str(arch)]) == 0
    resumed = pr.Phase2Runner.resume(
        arch, pr.RunConfig(dry_run=True), pr.StubAdapter())
    try:
        for a in resumed.agents:
            assert a["farm_id"] is not None, a["name"]
            assert len(a["recent_actions"]) > 0, a["name"]
            entry = a["recent_actions"][-1]
            assert entry["http_status"] in (200, 400, "rejected",
                                            "n/a(wait)"), entry
        # the resumed run's prompts carry the restored memory
        p = resumed.build_prompt(resumed.agents[0])
        hist = json.loads(_section(p, "recent_actions"))
        assert len(hist) > 0
        structs = json.loads(_section(p, "own_structures"))
        assert any(s.get("kind") == "farm" for s in structs)
    finally:
        resumed.logf.close()


def test_prompt_truncation_under_cap(tmp_path):
    runner = make_runner(tmp_path)
    runner.setup_world()
    try:
        T = runner.T
        key = runner.agents[0]["key"]
        for i in range(600):
            T.signed_request(runner.client, key, "POST", "/chat",
                             {"text": f"flood message number {i} " + "x" * 40},
                             expect=201)
        p = runner.build_prompt(runner.agents[0])
        assert pr.estimate_tokens(p) <= runner.cfg.per_tick_in_cap
        assert "[core_state]" in p  # core state never dropped
    finally:
        runner.logf.close()


def test_archive_bundle_complete_and_verifiable(tmp_path):
    rc = pr.main(["--dry-run", "--ticks", "2",
                  "--archive", str(tmp_path / "a")])
    assert rc == 0
    arch = tmp_path / "a"
    for name in ("run.jsonl", "budget.json", "ledger.json",
                 "runner_state.json", "agent_keys.json", "manifest.json",
                 "world_end.db", "snapshots/start.json",
                 "snapshots/end.json"):
        assert (arch / name).exists(), name
    report = pr.verify_archive(arch)
    assert report["ok"], report["errors"]
    # budget.json reconciles with the log
    b = json.loads((arch / "budget.json").read_text())
    assert b["totals"]["model_calls"] == 2
    assert b["totals"]["spend_dollars"] > 0
    # both agents present in the end snapshot
    snap = json.loads((arch / "snapshots" / "end.json").read_text())
    assert {a["name"] for a in snap["agents"]} == {"exp-01", "exp-02"}
    assert len(snap["ledger"]) >= 0


def test_resume_continues_from_archive(tmp_path):
    arch = tmp_path / "a"
    assert pr.main(["--dry-run", "--ticks", "2",
                    "--archive", str(arch)]) == 0
    assert pr.main(["--resume", str(arch), "--dry-run",
                    "--ticks", "4"]) == 0
    evs = [json.loads(l) for l in (arch / "run.jsonl").read_text()
           .splitlines() if l.strip()]
    assert sum(1 for e in evs if e.get("event") == "run_start") == 1
    assert any(e.get("event") == "run_resumed" for e in evs)
    seqs = {}
    for e in evs:
        if e.get("event") == "tick" and "tick_seq" in e:
            seqs.setdefault(e["agent"], []).append(e["tick_seq"])
    assert seqs, "no signed attempts recorded"
    for agent, s in seqs.items():
        assert s == list(range(1, len(s) + 1)), (agent, s)
    report = pr.verify_archive(arch)
    assert report["ok"], report["errors"]


def test_resume_voided_on_brief_change(tmp_path, monkeypatch):
    arch = tmp_path / "a"
    assert pr.main(["--dry-run", "--ticks", "2",
                    "--archive", str(arch)]) == 0
    real_sha = pr.sha256_file

    def fake_sha(p):
        if "/brief_exp_" in str(p).replace("\\", "/"):
            return "0" * 64
        return real_sha(p)

    monkeypatch.setattr(pr, "sha256_file", fake_sha)
    with pytest.raises(RuntimeError, match="resume voided"):
        pr.Phase2Runner.resume(arch, pr.RunConfig(dry_run=True),
                               pr.StubAdapter())


# ---------------------------------------------------------------- 4. release safeguards (ChatGPT final review)


def boom_transport(url, headers, payload, timeout):
    """Test double transport: the request may have been billed before
    this timeout. Billing is unknowable from here."""
    raise TimeoutError("read timed out after request was sent")


def test_uncertain_billing_halts_run_no_retry(tmp_path):
    """Fail closed: an API failure with unknown billing stops the run
    as a technical stop — no blind retries that could breach the cap."""
    runner = make_runner(tmp_path)
    runner.adapter = pr.PinnedModelAdapter(
        api_url="http://x", api_key="k", transport=boom_transport)
    runner.setup_world()
    try:
        assert runner.tick(runner.agents[0]) == "halt"
        assert runner.model_calls == 0  # nothing accounted, nothing retried
        evs = read_events(runner)
        assert any(e.get("event") == "technical_stop"
                   and e.get("reason") == "uncertain_model_billing"
                   and e.get("agent") == "exp-01"
                   for e in evs), "missing fail-closed technical stop"
    finally:
        runner.logf.close()


def test_missing_usage_also_halts_run(tmp_path):
    """A 200 with no billed usage is the same fail-closed condition:
    spend cannot be accounted, so the run stops."""
    def no_usage(url, headers, payload, timeout):
        return {"choices": [{"message": {"content": "{}"}}]}

    runner = make_runner(tmp_path)
    runner.adapter = pr.PinnedModelAdapter(
        api_url="http://x", api_key="k", transport=no_usage)
    runner.setup_world()
    try:
        assert runner.tick(runner.agents[0]) == "halt"
        evs = read_events(runner)
        assert any(e.get("event") == "technical_stop"
                   and e.get("reason") == "uncertain_model_billing"
                   for e in evs)
    finally:
        runner.logf.close()


def test_pre_send_limit_rejection_still_continues(tmp_path):
    """TokenLimitExceeded means the request was definitely NOT sent:
    nothing was billed, so ticking on is safe (not a billing halt)."""
    def must_not_send(url, headers, payload, timeout):
        raise AssertionError("transport must not be called")

    runner = make_runner(tmp_path)
    runner.adapter = pr.PinnedModelAdapter(
        api_url="http://x", api_key="k", per_tick_in_cap=1,
        transport=must_not_send)
    runner.setup_world()
    try:
        assert runner.tick(runner.agents[0]) == "ok"
        evs = read_events(runner)
        assert any(e.get("event") == "tick_rejected"
                   and e.get("reason") == "per_tick_in_cap" for e in evs)
        assert not any(e.get("event") == "technical_stop" for e in evs)
    finally:
        runner.logf.close()


def test_uncertain_billing_preserved_in_budget_json(tmp_path):
    """The unknown-spend record survives into the finalized
    budget.json, and the archive still verifies."""
    runner = make_runner(tmp_path)
    runner.adapter = pr.PinnedModelAdapter(
        api_url="http://x", api_key="k", transport=boom_transport)
    runner.setup_world()
    runner.run(max_additional_ticks=10)
    b = json.loads((runner.archive_dir / "budget.json").read_text())
    ub = b["uncertain_billing"]
    assert ub["count"] == 1, ub
    ev = ub["events"][0]
    assert ev["agent"] == "exp-01"
    assert "spend_dollars_at_halt" in ev and "model_calls_at_halt" in ev
    report = pr.verify_archive(runner.archive_dir)
    assert report["ok"], report["errors"]


def test_uncertain_billing_survives_resume(tmp_path):
    """A resumed run keeps the unknown-spend record from the earlier
    halt (no spend silently dropped across the resume boundary)."""
    arch = tmp_path / "a"
    cfg = pr.RunConfig(dry_run=True, dry_run_ticks=4,
                       tick_interval_seconds=0)
    runner = pr.Phase2Runner(
        cfg,
        pr.PinnedModelAdapter(api_url="http://x", api_key="k",
                              transport=boom_transport),
        arch)
    runner.setup_world()
    runner.run(max_additional_ticks=10)
    b = json.loads((arch / "budget.json").read_text())
    assert b["uncertain_billing"]["count"] == 1
    resumed = pr.Phase2Runner.resume(
        arch, pr.RunConfig(dry_run=True), pr.StubAdapter())
    try:
        assert len(resumed.uncertain_billing) == 1
        assert resumed.uncertain_billing[0]["agent"] == "exp-01"
    finally:
        resumed.logf.close()


def test_run_jsonl_hash_in_manifest_and_verified(tmp_path):
    rc = pr.main(["--dry-run", "--ticks", "2",
                  "--archive", str(tmp_path / "a")])
    assert rc == 0
    arch = tmp_path / "a"
    manifest = json.loads((arch / "manifest.json").read_text())
    assert "run.jsonl" in manifest["files"], "action log not hashed"
    assert (manifest["files"]["run.jsonl"]
            == pr.sha256_file(arch / "run.jsonl"))
    report = pr.verify_archive(arch)
    assert report["ok"], report["errors"]


def test_tampered_run_jsonl_fails_verification(tmp_path):
    rc = pr.main(["--dry-run", "--ticks", "2",
                  "--archive", str(tmp_path / "a")])
    assert rc == 0
    arch = tmp_path / "a"
    with (arch / "run.jsonl").open("a") as f:
        f.write('{"event":"forged","agent":"exp-01"}\n')
    report = pr.verify_archive(arch)
    assert not report["ok"]
    assert any("run.jsonl" in e for e in report["errors"]), \
        report["errors"]


def test_objectives_complete_stops_run_without_spend(tmp_path):
    """Both stockpiles verified complete -> record completion and
    stop. Zero further inference is spent."""
    runner = make_runner(tmp_path)
    runner.setup_world()
    try:
        T = runner.T
        for a in runner.agents:
            T.set_inventory(runner.db_path, a["pk"],
                            {"iron": 10, "flour": 16})
        runner.run(max_additional_ticks=50)
        assert runner.ticks == 0
        assert runner.model_calls == 0
        evs = read_events(runner)
        done = [e for e in evs
                if e.get("event") == "run_objectives_complete"]
        assert done, "completion not recorded"
        assert done[0]["inventories"] == {
            "exp-01": {"iron": 10, "flour": 16},
            "exp-02": {"iron": 10, "flour": 16}}
        ends = [e for e in evs
                if e.get("event") == "run_end"
                and e.get("reason") == "objectives_complete"]
        assert ends, "run_end reason wrong"
        report = pr.verify_archive(runner.archive_dir)
        assert report["ok"], report["errors"]
    finally:
        runner.logf.close()


def test_objectives_incomplete_keeps_running(tmp_path):
    """One agent short on flour -> the run proceeds; the stop only
    fires on fully verified stockpiles."""
    runner = make_runner(tmp_path)
    runner.setup_world()
    try:
        T = runner.T
        T.set_inventory(runner.db_path, runner.agents[0]["pk"],
                        {"iron": 10, "flour": 16})
        T.set_inventory(runner.db_path, runner.agents[1]["pk"],
                        {"iron": 10, "flour": 15})
        assert runner.objectives_complete() is None
        runner.run(max_additional_ticks=2)
        assert runner.ticks > 0
        evs = read_events(runner)
        assert not any(e.get("event") == "run_objectives_complete"
                       for e in evs)
    finally:
        runner.logf.close()


def test_objectives_not_trusted_from_claims(tmp_path):
    """Stated claims don't count: objectives_complete reads the
    authoritative world DB, and an empty world is never 'complete'."""
    runner = make_runner(tmp_path)
    assert runner.objectives_complete() is None  # no agents/world yet


def test_null_content_call_record_has_no_action(tmp_path):
    """A null-content (empty) model call still gets an exact evidence
    record: prompt hashed, raw response null, no_action result."""

    class NullAdapter(pr.ModelAdapter):
        name = "test-null"
        enforces_limits = True

        def complete(self, prompt):
            return None, 915, 1500

    runner = make_runner(tmp_path)
    runner.adapter = NullAdapter()
    runner.setup_world()
    try:
        assert runner.tick(runner.agents[0]) == "ok"
        recs = sorted((runner.archive_dir / "calls").glob("call_*.json"))
        assert len(recs) == 1
        r = json.loads(recs[0].read_text())
        assert r["raw_response"] is None
        assert (hashlib.sha256(b"").hexdigest()
                == r["raw_response_sha256"])
        assert r["parsed_action"] is None
        assert r["action_result"]["reason"] == "empty_model_content"
    finally:
        runner.logf.close()


# ---------------------------------------------------------------- 9. finishing items (ChatGPT review)


def test_discoveries_newest_first_beyond_500(tmp_path):
    """Regression (ChatGPT review): with >500 discovered tiles the
    endpoint must return the NEWEST first (ORDER BY discovered_at
    DESC), so the runner's most-recent selection never loses fresh
    tiles. Privacy: another agent's tiles never leak."""
    import datetime
    runner = make_runner(tmp_path)
    runner.setup_world()
    try:
        T = runner.T
        conn = T.db(runner.db_path)
        try:
            aid = conn.execute(
                "SELECT id FROM agents WHERE name='exp-01'").fetchone()["id"]
            bid = conn.execute(
                "SELECT id FROM agents WHERE name='exp-02'").fetchone()["id"]
            base = datetime.datetime.now(datetime.timezone.utc)
            for i in range(520):
                ts = (base + datetime.timedelta(seconds=i)).isoformat()
                conn.execute(
                    "INSERT OR IGNORE INTO discoveries "
                    "(agent_id, x, y, terrain, discovered_at) "
                    "VALUES (?, ?, ?, ?, ?)",
                    (aid, 1000 + i, 0, "plains", ts))
            for i in range(5):
                ts = (base + datetime.timedelta(seconds=i)).isoformat()
                conn.execute(
                    "INSERT OR IGNORE INTO discoveries "
                    "(agent_id, x, y, terrain, discovered_at) "
                    "VALUES (?, ?, ?, ?, ?)",
                    (bid, 2000 + i, 0, "desert", ts))
            conn.commit()
        finally:
            conn.close()
        a, b = runner.agents
        da = runner._signed_get(a["key"], "/world/discoveries")
        assert isinstance(da, list) and len(da) == 500, len(da)
        # newest first: last-inserted tile leads, oldest of the
        # returned window trails
        assert da[0]["x"] == 1000 + 519, da[0]
        assert da[-1]["x"] == 1000 + 20, da[-1]
        assert all(da[i]["discovered_at"] >= da[i + 1]["discovered_at"]
                   for i in range(len(da) - 1))
        # none of b's tiles leak into a's read
        assert not any(d["x"] >= 2000 for d in da)
        db = runner._signed_get(b["key"], "/world/discoveries")
        db_xy = {(d["x"], d["y"]) for d in db}
        assert {(2000 + i, 0) for i in range(5)} <= db_xy, db_xy
        # and still none of a's tiles leak into b's read
        assert not any(d["x"] >= 1000 and d["x"] < 2000 for d in db)
        # the prompt's own_discoveries section carries the newest
        p = runner.build_prompt(a)
        own = json.loads(_section(p, "own_discoveries"))
        assert own and own[0]["x"] == 1000 + 519, own[0]
    finally:
        runner.logf.close()


def test_usability_controller_stops_on_adaptation(tmp_path):
    """Scripted: plant slot 0 (200), plant slot 0 again (400
    occupied), plant slot 1 (200). The controller must stop early
    with adaptation_observed and behavioral evidence — the pilot's
    failure mode, then the fix. Offline, zero spend."""
    import phase2_usability as pu
    script = [
        {"action": "farm_plant", "params": {"slot": 0},
         "reasoning": "t"},
        {"action": "farm_plant", "params": {"slot": 0},
         "reasoning": "t"},
        {"action": "farm_plant", "params": {"slot": 1},
         "reasoning": "t"},
    ]
    ctl = pu.UsabilityController(tmp_path / "u1",
                                 FixedBilledAdapter(script=script))
    try:
        reason = ctl.run()
        assert reason == "adaptation_observed", reason
        assert ctl.runner.model_calls == 3, ctl.runner.model_calls
        ev = ctl.adaptation_evidence
        assert ev["mode"] == "adaptation_observed", ev
        assert ev["trigger"]["http_status"] == 400, ev
        assert "not empty" in str(ev["trigger"]["response"]), ev
        assert ev["trigger"]["params"] == {"slot": 0}, ev
        assert ev["adapted_action"]["params"] == {"slot": 1}, ev
        assert ev["adapted_action"]["http_status"] == 200, ev
        evs = read_events(ctl.runner)
        assert any(e.get("event") == "usability_adaptation_observed"
                   for e in evs)
        # exact evidence records kept, hashed, no key material
        recs = sorted((ctl.runner.archive_dir / "calls").glob("call_*.json"))
        assert len(recs) == 3, len(recs)
        r0 = json.loads(recs[0].read_text())
        assert r0["prompt"] and r0["prompt_sha256"]
        assert r0["raw_response"] and r0["raw_response_sha256"]
        blob = json.dumps(r0)
        assert "BEGIN" not in blob and "PRIVATE KEY" not in blob
    finally:
        ctl.runner.logf.close()


def test_usability_controller_chat_after_trigger_is_inconclusive(tmp_path):
    """Tightened definition: an unrelated chat after a planting
    trigger is recorded as INCONCLUSIVE — it does not demonstrate
    learning from the farm's state. The run must NOT stop early;
    it continues to the billed-call budget."""
    import phase2_usability as pu
    script = [
        {"action": "farm_plant", "params": {"slot": 0},
         "reasoning": "t"},
        {"action": "farm_plant", "params": {"slot": 0},
         "reasoning": "t"},
        {"action": "chat", "params": {"text": "hello"}, "reasoning": "t"},
    ]
    ctl = pu.UsabilityController(tmp_path / "u2",
                                 FixedBilledAdapter(script=script))
    try:
        reason = ctl.run()
        assert reason == "call_budget", reason
        assert ctl.runner.model_calls == 10, ctl.runner.model_calls
        assert ctl.adaptation_evidence is None
        evs = read_events(ctl.runner)
        assert not any(e.get("event") == "usability_adaptation_observed"
                       for e in evs)
        inc = [e for e in evs
               if e.get("event") == "usability_inconclusive"]
        assert inc, "chat after trigger was not recorded inconclusive"
        assert inc[0]["mode"] == "inconclusive", inc[0]
        assert inc[0]["trigger"]["params"] == {"slot": 0}, inc[0]
        assert inc[0]["action"]["action"] == "chat", inc[0]
        assert "trigger_summary" in inc[0] and "action_summary" in inc[0]
    finally:
        ctl.runner.logf.close()


def test_detect_adaptation_harvest_never_trigger():
    """A successful harvest must never be treated as a planting
    trigger: harvest 200, then plant a different slot 200, is not
    adaptation under the tightened definition."""
    import phase2_usability as pu
    history = [
        {"tick_seq": 1, "action": "farm_harvest",
         "params": {"slot": 0}, "http_status": 200,
         "response": '{"action":"harvest"}'},
        {"tick_seq": 2, "action": "farm_plant",
         "params": {"slot": 1}, "http_status": 200,
         "response": '{"action":"plant"}'},
    ]
    assert pu.detect_adaptation(history) is None
    # harvest is not a trigger even on its own
    is_trig, _ = pu._is_trigger(history[0])
    assert not is_trig


def test_detect_adaptation_failed_replant_not_adaptation():
    """Planting a different slot after a trigger only counts when the
    plant SUCCEEDS (200 proves the slot was empty). A 400 on the new
    slot is not evidence of learning."""
    import phase2_usability as pu
    history = [
        {"tick_seq": 1, "action": "farm_plant",
         "params": {"slot": 0}, "http_status": 200,
         "response": '{"action":"plant"}'},
        {"tick_seq": 2, "action": "farm_plant",
         "params": {"slot": 0}, "http_status": 400,
         "response": '{"detail":"slot 0 is growing, not empty"}'},
        {"tick_seq": 3, "action": "farm_plant",
         "params": {"slot": 1}, "http_status": 400,
         "response": '{"detail":"slot 1 is growing, not empty"}'},
    ]
    assert pu.detect_adaptation(history) is None


def test_detect_adaptation_same_slot_replant_not_adaptation():
    """Re-planting the same slot after a rejection is the pilot's
    failure mode continuing — not adaptation."""
    import phase2_usability as pu
    history = [
        {"tick_seq": 1, "action": "farm_plant",
         "params": {"slot": 0}, "http_status": 200,
         "response": '{"action":"plant"}'},
        {"tick_seq": 2, "action": "farm_plant",
         "params": {"slot": 0}, "http_status": 400,
         "response": '{"detail":"slot 0 is growing, not empty"}'},
        {"tick_seq": 3, "action": "farm_plant",
         "params": {"slot": 0}, "http_status": 400,
         "response": '{"detail":"slot 0 is growing, not empty"}'},
    ]
    assert pu.detect_adaptation(history) is None


def test_detect_adaptation_strong_path_evidence_strings():
    """The strong path carries reviewer-readable trigger/adapted
    summaries naming the exact slot and outcome."""
    import phase2_usability as pu
    history = [
        {"tick_seq": 5, "action": "farm_plant",
         "params": {"slot": 0}, "http_status": 400,
         "response": '{"detail":"slot 0 is growing, not empty"}'},
        {"tick_seq": 7, "action": "farm_plant",
         "params": {"slot": 2}, "http_status": 200,
         "response": '{"action":"plant","state":"growing"}'},
    ]
    ev = pu.detect_adaptation(history)
    assert ev is not None and ev["mode"] == "adaptation_observed"
    assert "slot 0" in ev["trigger_summary"] and "400" in ev["trigger_summary"]
    assert "'slot': 2" in ev["adapted_summary"] and "200" in ev["adapted_summary"]
    assert ev["trigger"]["params"] == {"slot": 0}
    assert ev["adapted_action"]["params"] == {"slot": 2}


def test_usability_controller_stops_at_ten_billed_calls(tmp_path):
    """No adaptation: the agent re-plants slot 0 forever (pilot
    failure mode). The controller must stop at EXACTLY 10 billed
    calls — empty responses would count too, since they are billed."""
    import phase2_usability as pu
    script = [{"action": "farm_plant", "params": {"slot": 0},
               "reasoning": "t"}]
    ctl = pu.UsabilityController(tmp_path / "u3",
                                 FixedBilledAdapter(script=script))
    try:
        reason = ctl.run()
        assert reason == "call_budget", reason
        assert ctl.runner.model_calls == 10, ctl.runner.model_calls
        assert ctl.adaptation_evidence is None
        evs = read_events(ctl.runner)
        assert any(e.get("event") == "technical_stop"
                   and e.get("reason") == "max_model_calls_reached"
                   for e in evs)
        assert not any(e.get("event") == "usability_adaptation_observed"
                       for e in evs)
    finally:
        ctl.runner.logf.close()


def test_usability_controller_dollar_cap_trips(tmp_path):
    """The $1.00 hard cap trips via the fail-closed machinery even
    when the billed-call limit is not yet reached."""
    import phase2_usability as pu
    script = [{"action": "wait", "params": {"minutes": 1},
               "reasoning": "t"}]
    ctl = pu.UsabilityController(
        tmp_path / "u4",
        FixedBilledAdapter(script=script, in_tok=100000, out_tok=100000))
    try:
        reason = ctl.run()
        assert reason == "runner_halt", reason
        assert ctl.runner.model_calls <= 10, ctl.runner.model_calls
        assert ctl.runner.dollars() > 1.00, ctl.runner.dollars()
        evs = read_events(ctl.runner)
        assert any(e.get("event") == "technical_stop"
                   and e.get("reason") == "budget_cap_exceeded"
                   for e in evs)
    finally:
        ctl.runner.logf.close()


def test_usability_wait_pause_never_busy_spins(tmp_path):
    """While the agent waits (live mode), the controller pauses in a
    bounded poll instead of looping. Dry-run never sleeps."""
    import phase2_usability as pu
    import time as _time
    ctl = pu.UsabilityController(tmp_path / "uw",
                                 FixedBilledAdapter())
    try:
        ctl.setup()
        # dry-run: waits are skipped deterministically -> always 0
        assert ctl._wait_pause_seconds(0) == 0.0
        # live mode, agent waiting 10 minutes out: bounded poll
        ctl.runner.cfg.dry_run = False
        ctl.agent["wait_until"] = _time.time() + 600
        assert ctl._wait_pause_seconds(0) == pu.WAIT_POLL_SECONDS
        # live mode, wait almost over: pauses only the remainder
        ctl.agent["wait_until"] = _time.time() + 5
        assert ctl._wait_pause_seconds(0) == pytest.approx(5, abs=1)
        # a consumed model call means this was not a wait tick
        ctl.runner.model_calls = 1
        assert ctl._wait_pause_seconds(0) == 0.0
        ctl.runner.model_calls = 0
        # wait already elapsed: no pause
        ctl.agent["wait_until"] = _time.time() - 1
        assert ctl._wait_pause_seconds(0) == 0.0
    finally:
        ctl.runner.logf.close()


def test_usability_wait_uses_sleeper_not_spin(tmp_path):
    """The run loop actually calls the (replaceable) sleeper while
    waiting instead of spinning on tick()."""
    import phase2_usability as pu
    import time as _time
    script = [{"action": "wait", "params": {"minutes": 30},
               "reasoning": "t"},
              {"action": "farm_plant", "params": {"slot": 0},
               "reasoning": "t"}]
    ctl = pu.UsabilityController(tmp_path / "uw2",
                                 FixedBilledAdapter(script=script))
    try:
        ctl.setup()
        # force live-mode wait semantics without sleeping for real
        ctl.runner.cfg.dry_run = False
        sleeps = []
        ctl._sleeper = lambda s: sleeps.append(s)
        ctl._wait_poll_seconds = 0.01
        # one manual tick: wait action sets wait_until in the future
        assert ctl.runner.tick(ctl.agent) == "ok"
        assert ctl.agent["wait_until"] > _time.time()
        calls_before = ctl.runner.model_calls
        pause = ctl._wait_pause_seconds(calls_before)
        assert pause == pytest.approx(0.01, abs=0.005)
        ctl._sleeper(pause)
        assert sleeps and all(s <= 0.01 + 1e-6 for s in sleeps)
    finally:
        ctl.runner.logf.close()


def test_usability_wall_clock_limit_trips(tmp_path):
    """A zero wall-clock budget stops the check immediately with a
    recorded reason — bounding even pathological no-call loops."""
    import phase2_usability as pu
    ctl = pu.UsabilityController(tmp_path / "uwc",
                                 FixedBilledAdapter(),
                                 wall_clock_seconds=0)
    try:
        reason = ctl.run()
        assert reason == "wall_clock", reason
        assert ctl.runner.model_calls == 0, ctl.runner.model_calls
        evs = read_events(ctl.runner)
        assert any(e.get("event") == "usability_stop"
                   and e.get("reason") == "wall_clock"
                   for e in evs)
        start = [e for e in evs
                 if e.get("event") == "usability_check_start"][0]
        assert start["wall_clock_seconds"] == 0
    finally:
        ctl.runner.logf.close()


def test_usability_live_path_fails_loudly_without_credentials(monkeypatch,
                                                              tmp_path):
    """Invoking the live entry point without model credentials raises
    AdapterConfigError BEFORE any world setup or spend — no silent
    fallback, no $0-spend ambiguity."""
    import phase2_usability as pu
    clear_model_env(monkeypatch)
    with pytest.raises(pr.AdapterConfigError) as ei:
        pu.run_live_check(tmp_path / "live")
    assert "PHASE2_MODEL_API_URL" in str(ei.value)
    # nothing was set up: no archive, no spend, no calls
    assert not (tmp_path / "live").exists()


def test_usability_live_path_rejects_scripted_adapter(tmp_path):
    """A non-dry-run controller REFUSES scripted/test adapters: there
    is no code path by which one can silently replace the real model
    in a live invocation."""
    import phase2_usability as pu
    with pytest.raises(pr.AdapterConfigError) as ei:
        pu.UsabilityController(tmp_path / "ul",
                               FixedBilledAdapter(),
                               dry_run=False)
    assert "scripted" in str(ei.value).lower()
    with pytest.raises(pr.AdapterConfigError):
        pu.UsabilityController(tmp_path / "ul2",
                               pr.StubAdapter(),
                               dry_run=False)


def test_usability_live_entry_takes_no_adapter():
    """run_live_check has no adapter parameter by design — injection
    is impossible at the signature level."""
    import inspect
    import phase2_usability as pu
    params = inspect.signature(pu.run_live_check).parameters
    assert "adapter" not in params, params
    assert set(params) >= {"archive_dir", "max_billed_calls",
                           "dollar_cap", "wall_clock_seconds"}
