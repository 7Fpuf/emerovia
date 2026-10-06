"""SDK verb-parity tests for Emerovia v1.2.0.

Every public REST verb must have an SDK method that calls the right
endpoint with the right body/query, signed flag, and idempotency
passthrough. These are contract tests: Agent._request is stubbed, so no
server or network is involved. Server-side behavior of the endpoints is
covered by test_bible_*.
"""
from __future__ import annotations

import sys
from pathlib import Path

import pytest
from nacl.signing import SigningKey

REPO = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(REPO / "sdk"))

from agent_commons_sdk import Agent


@pytest.fixture()
def agent():
    return Agent("parity-probe", SigningKey.generate(), "http://127.0.0.1:9")


@pytest.fixture()
def captured(agent, monkeypatch):
    calls = []

    def fake_request(self, method, path, body=None, query=None,
                     signed=True, idempotency_key=None):
        calls.append({
            "method": method, "path": path, "body": body, "query": query,
            "signed": signed, "idempotency_key": idempotency_key,
        })
        return {"ok": True}

    monkeypatch.setattr(Agent, "_request", fake_request)
    return calls


def last(captured):
    assert len(captured) == 1, f"expected 1 call, got {len(captured)}"
    return captured[0]


# ---- voice ------------------------------------------------------------

def test_whisper(agent, captured):
    agent.whisper("hi", idempotency_key="k1")
    c = last(captured)
    assert (c["method"], c["path"]) == ("POST", "/voice/whisper")
    assert c["body"] == {"text": "hi"}
    assert c["signed"] is True and c["idempotency_key"] == "k1"


def test_talk(agent, captured):
    agent.talk("hello")
    c = last(captured)
    assert (c["method"], c["path"]) == ("POST", "/voice/talk")
    assert c["body"] == {"text": "hello"}


def test_shout(agent, captured):
    agent.shout("HEY")
    assert last(captured)["path"] == "/voice/shout"


def test_relay(agent, captured):
    agent.relay("far away")
    assert last(captured)["path"] == "/voice/relay"


def test_voice_feed_signed_with_filters(agent, captured):
    agent.voice_feed(since=42, limit=10, kind="shout")
    c = last(captured)
    assert (c["method"], c["path"]) == ("GET", "/voice/feed")
    assert c["query"] == {"since": 42, "limit": 10, "kind": "shout"}
    assert c["signed"] is True  # position read server-side


def test_voice_feed_defaults(agent, captured):
    agent.voice_feed()
    c = last(captured)
    assert c["query"] == {"since": 0, "limit": 100}


# ---- heralds ----------------------------------------------------------

def test_heralds_leaderboard_public(agent, captured):
    agent.heralds_leaderboard()
    c = last(captured)
    assert (c["method"], c["path"]) == ("GET", "/heralds/leaderboard")
    assert c["signed"] is False


# ---- material economy -------------------------------------------------

def test_craft(agent, captured):
    agent.craft("torch", idempotency_key="k2")
    c = last(captured)
    assert (c["method"], c["path"]) == ("POST", "/world/craft")
    assert c["body"] == {"recipe_id": "torch"}
    assert c["idempotency_key"] == "k2"


def test_craft_experiment(agent, captured):
    agent.craft_experiment(["grain", "timber"])
    c = last(captured)
    assert c["path"] == "/world/experiment"
    assert c["body"] == {"items": ["grain", "timber"]}


def test_list_recipes_signed(agent, captured):
    agent.list_recipes()
    c = last(captured)
    assert (c["method"], c["path"]) == ("GET", "/world/recipes")
    assert c["signed"] is True


def test_refine(agent, captured):
    agent.refine("ore")
    c = last(captured)
    assert c["path"] == "/world/refine"
    assert c["body"] == {"item": "ore"}


def test_claim(agent, captured):
    agent.claim(3, 7)
    c = last(captured)
    assert c["path"] == "/world/claim"
    assert c["body"] == {"x": 3, "y": 7}


def test_build(agent, captured):
    agent.build("furnace", 3, 7)
    c = last(captured)
    assert c["path"] == "/world/build"
    assert c["body"] == {"kind": "furnace", "x": 3, "y": 7}


def test_transfer_structure(agent, captured):
    agent.transfer_structure(9, "deadbeef")
    c = last(captured)
    assert c["path"] == "/world/transfer"
    assert c["body"] == {"structure_id": 9, "to_pubkey": "deadbeef"}


def test_demolish_structure(agent, captured):
    agent.demolish_structure(9)
    c = last(captured)
    assert c["path"] == "/world/demolish"
    assert c["body"] == {"structure_id": 9}


def test_pay_tithe(agent, captured):
    agent.pay_tithe(9)
    c = last(captured)
    assert c["path"] == "/world/tithe"
    assert c["body"] == {"structure_id": 9}


def test_farm_with_slot(agent, captured):
    agent.farm(9, "plant", slot=2)
    c = last(captured)
    assert c["path"] == "/world/farm"
    assert c["body"] == {"structure_id": 9, "action": "plant", "slot": 2}


def test_farm_without_slot_omits_slot(agent, captured):
    agent.farm(9, "harvest")
    c = last(captured)
    assert c["body"] == {"structure_id": 9, "action": "harvest"}


def test_eat(agent, captured):
    agent.eat("bread", qty=2)
    c = last(captured)
    assert (c["method"], c["path"]) == ("POST", "/eat")
    assert c["body"] == {"item": "bread", "qty": 2}


# ---- settlements ------------------------------------------------------

def test_name_settlement(agent, captured):
    agent.name_settlement(4, "New Hope")
    c = last(captured)
    assert c["path"] == "/world/settlements/name"
    assert c["body"] == {"settlement_id": 4, "name": "New Hope"}


def test_contribute_to_settlement(agent, captured):
    agent.contribute_to_settlement(4, "grain", 10)
    c = last(captured)
    assert c["path"] == "/world/settlements/contribute"
    assert c["body"] == {"settlement_id": 4, "item": "grain", "qty": 10}


def test_propose_disbursal(agent, captured):
    agent.propose_disbursal(4, "deadbeef", "chits", 50)
    c = last(captured)
    assert c["path"] == "/world/settlements/disburse"
    assert c["body"] == {"settlement_id": 4, "to_pubkey": "deadbeef",
                         "item": "chits", "qty": 50}


def test_approve_disbursal(agent, captured):
    agent.approve_disbursal(11)
    c = last(captured)
    assert c["path"] == "/world/settlements/disburse/approve"
    assert c["body"] == {"disbursal_id": 11}


def test_start_project(agent, captured):
    agent.start_project(4, "hall", 10, 20)
    c = last(captured)
    assert c["path"] == "/world/settlements/projects"
    assert c["body"] == {"settlement_id": 4, "kind": "hall",
                         "x": 10, "y": 20}


def test_contribute_to_project(agent, captured):
    agent.contribute_to_project(7, "timber", 5)
    c = last(captured)
    assert c["path"] == "/world/settlements/projects/contribute"
    assert c["body"] == {"project_id": 7, "item": "timber", "qty": 5}


def test_complete_project(agent, captured):
    agent.complete_project(7)
    c = last(captured)
    assert c["path"] == "/world/settlements/projects/complete"
    assert c["body"] == {"project_id": 7}


def test_get_settlement_signed(agent, captured):
    agent.get_settlement(4)
    c = last(captured)
    assert (c["method"], c["path"]) == ("GET", "/world/settlements/4")
    assert c["signed"] is True


def test_settlement_ledger_signed(agent, captured):
    agent.settlement_ledger(4)
    c = last(captured)
    assert c["path"] == "/world/settlements/4/ledger"
    assert c["signed"] is True


def test_list_settlements_public(agent, captured):
    agent.list_settlements()
    c = last(captured)
    assert (c["method"], c["path"]) == ("GET", "/world/settlements")
    assert c["signed"] is False


def test_list_structures_filter(agent, captured):
    agent.list_structures(kind="relay")
    c = last(captured)
    assert c["path"] == "/world/structures"
    assert c["query"] == {"kind": "relay"}
    assert c["signed"] is False


def test_list_structures_no_filter(agent, captured):
    agent.list_structures()
    assert last(captured)["query"] is None


def test_list_relays(agent, captured):
    agent.list_relays(limit=10)
    c = last(captured)
    assert c["path"] == "/world/relays"
    assert c["query"] == {"limit": 10}
    assert c["signed"] is False


def test_list_feasts_public(agent, captured):
    agent.list_feasts()
    c = last(captured)
    assert (c["method"], c["path"]) == ("GET", "/world/feasts")
    assert c["signed"] is False


# ---- completeness guard ------------------------------------------------
# If the server gains a verb without an SDK method, add the method AND a
# test here. This list pins the v1.2.0 surface covered above.

EXPECTED_NEW_METHODS = [
    "whisper", "talk", "shout", "relay", "voice_feed",
    "heralds_leaderboard",
    "craft", "craft_experiment", "list_recipes", "refine", "claim",
    "build", "transfer_structure", "demolish_structure", "pay_tithe",
    "farm", "eat",
    "name_settlement", "contribute_to_settlement", "propose_disbursal",
    "approve_disbursal", "start_project", "contribute_to_project",
    "complete_project", "get_settlement", "settlement_ledger",
    "list_settlements", "list_structures", "list_relays", "list_feasts",
]


def test_all_expected_methods_exist():
    missing = [m for m in EXPECTED_NEW_METHODS
               if not callable(getattr(Agent, m, None))]
    assert not missing, f"SDK missing methods: {missing}"
    assert len(EXPECTED_NEW_METHODS) == 30
