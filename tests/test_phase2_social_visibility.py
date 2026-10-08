"""Phase 2 social-visibility-v1 (harness 0.3.0) — content-level tests.

Covers the single observation-interface change vs obs-interface-v1:
[agents_visible] now passes through the already-public agent directory
fields (name, live x/y, terrain) instead of names only.

HARD BOUNDARIES: no live model calls, no inference spend, no
production, no behavioral experiment. Every test is offline and uses
the scripted stub against a temp-DB world.
"""

from __future__ import annotations

import hashlib
import json
import sys
from pathlib import Path

import pytest

REPO = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(REPO / "tools"))
sys.path.insert(0, str(REPO / "tests"))

import phase2_runner as pr
import test_econ_validation_phase1 as T

FROZEN_BRIEF_SHAS = {
    "exp-01": "ddafa4ab3d1f8219487d07c870a93077b5721b2260b14de3cf153d8607e49843",
    "exp-02": "d0bf6dc30d317939cb943a71d12fef0ca30e2f4e725882dbedd5bc1267bdd2e1",
}

EXPECTED_SECTIONS = (
    "core_state", "recent_actions", "open_trade_offers", "recent_chat",
    "own_structures", "own_discoveries", "nearby_disclosed_tiles",
    "recent_filled_trades", "agents_visible", "public_recipes",
)


def make_runner(tmp_path, **cfg_kw):
    cfg = pr.RunConfig(dry_run=True, dry_run_ticks=4,
                       tick_interval_seconds=0, **cfg_kw)
    archive = tmp_path / "archive"
    return pr.Phase2Runner(cfg, pr.StubAdapter(), archive)


def _section(prompt: str, name: str) -> str:
    marker = f"[{name}]\n"
    start = prompt.index(marker) + len(marker)
    nxt = prompt.find("\n[", start)
    return prompt[start:] if nxt == -1 else prompt[start:nxt]


def _setup_two_agents(tmp_path):
    runner = make_runner(tmp_path)
    runner.setup_world()
    assert len(runner.agents) == 2
    return runner


def test_variant_is_registered():
    assert pr.HARNESS_VERSION == "phase2-runner/0.3.0"
    assert pr.PROMPT_VARIANT == "social-visibility-v1"


def test_section_headings_unchanged(tmp_path):
    """The 0.2.0 section list is intact — only agents_visible's content
    changed, nothing else in the prompt assembly."""
    runner = _setup_two_agents(tmp_path)
    try:
        p = runner.build_prompt(runner.agents[0])
        found = []
        for section in EXPECTED_SECTIONS:
            assert f"[{section}]" in p, section
            found.append(p.index(f"[{section}]"))
        assert found == sorted(found), "section order changed"
    finally:
        runner.logf.close()


def test_agents_visible_carries_name_position_terrain(tmp_path):
    """The other agent's entry carries name, live x/y, and terrain —
    matching the server's public /world/agents record exactly."""
    runner = _setup_two_agents(tmp_path)
    try:
        me, other = runner.agents[0], runner.agents[1]
        p = runner.build_prompt(me)
        entries = json.loads(_section(p, "agents_visible"))
        assert isinstance(entries, list) and len(entries) >= 2, entries
        by_name = {e["name"]: e for e in entries}
        assert other["name"] in by_name, by_name.keys()
        entry = by_name[other["name"]]
        assert set(entry.keys()) == {"name", "x", "y", "terrain"}, entry
        # cross-check against the server's own public directory
        live = {a["agent_name"]: a
                for a in runner.client.get("/world/agents").json()}
        want = live[other["name"]]
        assert entry["x"] == want["x"], (entry, want)
        assert entry["y"] == want["y"], (entry, want)
        assert entry["terrain"] == want["terrain"], (entry, want)
    finally:
        runner.logf.close()


def test_agents_visible_coordinates_are_live(tmp_path):
    """After the other agent moves, the next prompt shows its NEW
    position — the coordinates are live observations, not stale."""
    runner = _setup_two_agents(tmp_path)
    try:
        me, other = runner.agents[0], runner.agents[1]
        before = T.me(runner.client, other["key"])
        moved = None
        for d in ("N", "S", "E", "W"):
            r = T.signed_request(runner.client, other["key"], "POST",
                                 "/world/move",
                                 {"dir": d, "reasoning": "test move"})
            if r.get("x") is not None and (r["x"], r["y"]) != (
                    before["x"], before["y"]):
                moved = r
                break
        assert moved is not None, "no valid test move found"
        p = runner.build_prompt(me)
        entries = json.loads(_section(p, "agents_visible"))
        entry = {e["name"]: e for e in entries}[other["name"]]
        assert (entry["x"], entry["y"]) == (moved["x"], moved["y"]), entry
    finally:
        runner.logf.close()


def test_no_directive_text_added(tmp_path):
    """The runner added observation data only: no instruction to
    communicate, approach, cooperate, or trade appears anywhere in the
    runner-generated prompt (the frozen briefs' own mechanics text is
    pre-existing and untouched)."""
    runner = _setup_two_agents(tmp_path)
    try:
        agent = runner.agents[0]
        p = runner.build_prompt(agent)
        assert p.startswith(agent["brief"]), "brief must lead the prompt"
        generated = p[len(agent["brief"]):]
        for phrase in ("you should approach", "seek out the other",
                       "cooperate with", "contact the other",
                       "trade with the other", "go find",
                       "move toward the other", "initiate contact"):
            assert phrase not in generated.lower(), phrase
        # the agents_visible entries are pure data records
        entries = json.loads(_section(p, "agents_visible"))
        for e in entries:
            assert set(e.keys()) == {"name", "x", "y", "terrain"}
            assert isinstance(e["name"], str)
            assert isinstance(e["x"], int) and isinstance(e["y"], int)
            assert isinstance(e["terrain"], str)
    finally:
        runner.logf.close()


def test_briefs_match_frozen_shas():
    """Agent briefs are byte-identical to the frozen run-4 briefs."""
    for name, fname, want in (
        ("exp-01", "brief_exp_01.md", FROZEN_BRIEF_SHAS["exp-01"]),
        ("exp-02", "brief_exp_02.md", FROZEN_BRIEF_SHAS["exp-02"]),
    ):
        path = REPO / "phase2" / fname
        h = hashlib.sha256(path.read_bytes()).hexdigest()
        assert h == want, (name, h)


def test_frozen_parameters_unchanged():
    """Economics-adjacent runner parameters are exactly the reviewed
    values — the variant changed observations only."""
    cfg = pr.RunConfig()
    assert cfg.per_tick_in_cap == 6000
    assert cfg.per_tick_out_cap == 1500
    assert cfg.max_model_calls == 400
    assert cfg.dollar_cap == 6.00
    assert cfg.max_empty_streak == 4
    assert cfg.max_actions_per_agent == 150
    assert cfg.tick_interval_seconds == 120
    assert pr.PINNED_MODEL == "muse-spark-1.3"
    assert pr.RATE_IN_PER_M == 1.25
    assert pr.RATE_OUT_PER_M == 4.25
    assert pr.RECENT_ACTIONS_KEPT == 5
    assert pr.DISCOVERIES_KEPT == 60


def test_prompt_still_within_token_cap(tmp_path):
    """The larger agents_visible section does not blow the per-tick
    input budget."""
    runner = _setup_two_agents(tmp_path)
    try:
        p = runner.build_prompt(runner.agents[0])
        assert pr.estimate_tokens(p) <= runner.cfg.per_tick_in_cap
    finally:
        runner.logf.close()


def test_both_agents_receive_each_others_fields(tmp_path):
    """Both directions: each agent's prompt carries the other's name,
    live x/y, and terrain — the builder is symmetric."""
    runner = _setup_two_agents(tmp_path)
    try:
        a0, a1 = runner.agents[0], runner.agents[1]
        for me, other in ((a0, a1), (a1, a0)):
            p = runner.build_prompt(me)
            entries = json.loads(_section(p, "agents_visible"))
            by_name = {e["name"]: e for e in entries}
            assert other["name"] in by_name, by_name.keys()
            entry = by_name[other["name"]]
            assert set(entry.keys()) == {"name", "x", "y", "terrain"}, entry
    finally:
        runner.logf.close()


def test_agents_visible_under_budget_pressure(tmp_path):
    """With a tight per-tick token cap, [agents_visible] is either kept
    with full name/x/y/terrain fields or explicitly named in the
    omitted-for-size note — never silently dropped."""
    runner = make_runner(tmp_path, per_tick_in_cap=1500)
    runner.setup_world()
    try:
        me, other = runner.agents[0], runner.agents[1]
        p = runner.build_prompt(me)
        if "[agents_visible]\n" in p:
            entries = json.loads(_section(p, "agents_visible"))
            entry = {e["name"]: e for e in entries}[other["name"]]
            assert set(entry.keys()) == {"name", "x", "y", "terrain"}, entry
        else:
            assert "agents_visible" in p, p[-400:]
    finally:
        runner.logf.close()
