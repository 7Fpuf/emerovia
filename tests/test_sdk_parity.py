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


# ---- policy engine v1 -------------------------------------------------

def test_set_citizen_card(agent, captured):
    card = {"identity": "emerovia:" + "ab" * 32, "card_version": "citizen-card/v0"}
    agent.set_citizen_card(card)
    c = last(captured)
    assert (c["method"], c["path"]) == ("POST", "/citizens/card")
    assert c["body"]["card"] == card
    assert len(c["body"]["signature"]) == 128
    assert c["signed"] is True


def test_get_citizen_card(agent, captured):
    agent.get_citizen_card("nova")
    c = last(captured)
    assert (c["method"], c["path"]) == ("GET", "/citizens/nova/card")
    assert c["signed"] is False


def test_get_mandate(agent, captured):
    agent.get_mandate("nova")
    assert last(captured)["path"] == "/citizens/nova/mandate"


def test_attach_mandate(agent, captured):
    mandate = {"citizen": "emerovia:" + "ab" * 32}
    agent.attach_mandate(mandate, "ff" * 64)
    c = last(captured)
    assert (c["method"], c["path"]) == ("POST", "/citizens/mandate")
    assert c["body"] == {"mandate": mandate, "signature": "ff" * 64}


def test_citizen_timeline(agent, captured):
    agent.citizen_timeline("nova", limit=10)
    c = last(captured)
    assert (c["method"], c["path"]) == ("GET", "/citizens/nova/timeline")
    assert c["query"] == {"limit": 10}
    assert c["signed"] is False


def test_issue_lease(agent, captured):
    lease = {"lease_id": "L-1", "issuer": "emerovia:" + "ab" * 32}
    agent.issue_lease(lease)
    c = last(captured)
    assert (c["method"], c["path"]) == ("POST", "/leases/issue")
    assert c["body"]["lease"] == lease
    assert len(c["body"]["signature"]) == 128


def test_revoke_lease(agent, captured):
    agent.revoke_lease("L-1")
    c = last(captured)
    assert (c["method"], c["path"]) == ("POST", "/leases/L-1/revoke")


def test_list_leases(agent, captured):
    agent.list_leases(citizen="emerovia:" + "ab" * 32, capability="world.move")
    c = last(captured)
    assert (c["method"], c["path"]) == ("GET", "/leases")
    assert c["query"]["citizen"] == "emerovia:" + "ab" * 32
    assert c["signed"] is False


def test_get_lease(agent, captured):
    agent.get_lease("L-1")
    assert last(captured)["path"] == "/leases/L-1"


def test_list_capabilities(agent, captured):
    agent.list_capabilities()
    c = last(captured)
    assert (c["method"], c["path"]) == ("GET", "/capabilities")
    assert c["signed"] is False


def test_get_capability(agent, captured):
    agent.get_capability("world.move")
    assert last(captured)["path"] == "/capabilities/world.move"


def test_policy_check(agent, captured):
    agent.policy_check("world.move", amount_bytes=10)
    c = last(captured)
    assert (c["method"], c["path"]) == ("POST", "/policy/check")
    assert c["body"] == {"capability": "world.move", "amount_bytes": 10}
    assert c["signed"] is True


def test_policy_denials(agent, captured):
    agent.policy_denials(limit=5)
    c = last(captured)
    assert (c["method"], c["path"]) == ("GET", "/policy/denials")
    assert c["signed"] is False


def test_policy_authority(agent, captured):
    agent.policy_authority()
    assert last(captured)["path"] == "/policy/authority"


def test_mind_write(agent, captured):
    agent.mind_write("I trust Atlas", kind="fact", tags=["people"],
                     idempotency_key="k9")
    c = last(captured)
    assert (c["method"], c["path"]) == ("POST", "/mind/entries")
    assert c["body"] == {"kind": "fact", "text": "I trust Atlas", "tags": ["people"]}
    assert c["signed"] is True and c["idempotency_key"] == "k9"


def test_mind_list(agent, captured):
    agent.mind_list(kind="episode", q="Atlas", limit=10)
    c = last(captured)
    assert (c["method"], c["path"]) == ("GET", "/mind/entries")
    assert c["query"] == {"limit": 10, "kind": "episode", "q": "Atlas"}


def test_mind_update(agent, captured):
    agent.mind_update(7, "new text")
    c = last(captured)
    assert (c["method"], c["path"]) == ("PUT", "/mind/entries/7")
    assert c["body"] == {"text": "new text"}


def test_mind_delete(agent, captured):
    agent.mind_delete(7)
    assert last(captured)["path"] == "/mind/entries/7"


def test_sdk_lease_envelope_matches_server_verification(agent):
    """The SDK's envelope signing must verify under the server's
    verifier — this is the trust-critical interop."""
    import sys as _sys
    _sys.path.insert(0, str(REPO))
    from server import authority, leases as lease_registry
    lease = {
        "lease_id": "L-xyz",
        "issuer": "emerovia:" + agent.pubkey,
        "citizen": "emerovia:" + "cd" * 32,
        "capability": "object.furnace.use",
        "scope": {"max_uses": 5},
        "budget": None,
        "location": None,
        "expiration": "2030-01-01T00:00:00+00:00",
        "delegation_depth": 0,
        "revocation": {"notice": "immediate"},
        "parent_lease_id": None,
        "issued_at": "2026-10-06T00:00:00+00:00",
        "extra_ignored": True,
    }
    sig = agent._sign_document(agent._lease_envelope(lease))
    assert authority.verify_document_signature(
        agent.pubkey, lease_registry.envelope_for(lease), sig
    )
