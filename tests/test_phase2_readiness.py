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
        assert payload["max_tokens"] == 500
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
        for section in ("core_state", "open_trade_offers", "recent_chat",
                        "own_structures", "nearby_disclosed_tiles",
                        "recent_filled_trades", "agents_visible",
                        "public_recipes"):
            assert f"[{section}]" in p, section
        assert pr.estimate_tokens(p) <= runner.cfg.per_tick_in_cap
        # winter season info is ordinary public planning info
        assert "winter" in p.lower()
    finally:
        runner.logf.close()


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
