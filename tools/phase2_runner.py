#!/usr/bin/env python3
"""Phase 2 blind-discovery experiment runner (isolated preflight build).

Runs the two experiment agents (exp-01 / exp-02) against a TEMPORARY
Emerovia world database — never production. Each tick: GET world state
-> prompt the model -> parse exactly ONE action -> sign and POST ->
log everything to JSONL.

Boundaries (hard):
- AC_DB_PATH is always a temp file. Production is never touched.
- No real money, no cryptocurrency, no token mechanics.
- Dry-run mode (--dry-run) uses a scripted StubAdapter: zero real
  inference, no behavioral experiment. The behavioral experiment does
  NOT launch without Trevor's explicit approval (protocol §6).

Usage:
    # mechanics dry-run: 4 scripted ticks per agent, tiny budget
    .venv/bin/python tools/phase2_runner.py --dry-run --ticks 4

    # budget-halt verification (halts on the first tick)
    .venv/bin/python tools/phase2_runner.py --dry-run --ticks 4 \\
        --dollar-cap 0.000001

    # action-cap verification
    .venv/bin/python tools/phase2_runner.py --dry-run --ticks 10 \\
        --max-actions 2
"""

from __future__ import annotations

import argparse
import hashlib
import importlib
import json
import os
import sys
import tempfile
import time
from dataclasses import dataclass, field
from pathlib import Path

REPO = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(REPO))
sys.path.insert(0, str(REPO / "tests"))

# Pinned model + rates (standard tier, Meta Model API; verified 2026-10-06
# via public pricing pages: $1.25/M input, $4.25/M output, $0.15/M cached
# input — cached input not used here). Re-verify at launch; the TOKEN
# caps below are the binding constraint, the dollar cap is recomputed
# from whatever rates are current and pre-registered before start.
PINNED_MODEL = "muse-spark-1.3"
RATE_IN_PER_M = 1.25
RATE_OUT_PER_M = 4.25

HARNESS_VERSION = "phase2-runner/0.1.0-preflight"


# --------------------------------------------------------------------------
# Model adapters


class ModelAdapter:
    """Produces one action-decision per tick. Returns
    (response_text, input_tokens, output_tokens)."""

    name = "base"

    def complete(self, prompt: str) -> tuple[str, int, int]:
        raise NotImplementedError


class StubAdapter(ModelAdapter):
    """Scripted, deterministic stand-in for dry-runs. Exercises the full
    tick pipeline (prompt -> parse -> sign/POST -> record) with zero
    real inference and zero behavioral content."""

    name = "stub-scripted"

    # A fixed script per agent: chat (success path), move (may fail on
    # terrain — exercises failure recording), an uncovered trade offer
    # (400 path), then waits.
    SCRIPT = [
        {"action": "chat", "params": {"text": "preflight tick"},
         "reasoning": "dry-run: verify signed chat path"},
        {"action": "move", "params": {"dir": "E"},
         "reasoning": "dry-run: verify signed move path"},
        {"action": "trade_offer",
         "params": {"give": {"flour": 999}, "want": {"iron": 1}},
         "reasoning": "dry-run: verify 400 uncovered-offer recording"},
        {"action": "wait", "params": {"minutes": 1},
         "reasoning": "dry-run: verify wait path"},
    ]

    def __init__(self):
        self.calls = 0

    def complete(self, prompt: str) -> tuple[str, int, int]:
        step = self.SCRIPT[self.calls % len(self.SCRIPT)]
        self.calls += 1
        text = json.dumps(step)
        # deterministic token estimate for the dry run
        return text, len(prompt) // 4, len(text) // 4


# --------------------------------------------------------------------------
# Config


@dataclass
class RunConfig:
    max_actions_per_agent: int = 150
    max_wall_seconds: int = 6 * 3600
    per_tick_in_cap: int = 6000
    per_tick_out_cap: int = 500
    max_model_calls: int = 400
    dollar_cap: float = 4.00  # pre-registered; recomputed at launch
    tick_interval_seconds: int = 120
    wake_check_seconds: int = 900
    dry_run: bool = False
    dry_run_ticks: int = 4


# --------------------------------------------------------------------------
# Runner


class Phase2Runner:
    def __init__(self, config: RunConfig, adapter: ModelAdapter,
                 log_path: Path):
        self.cfg = config
        self.adapter = adapter
        self.log_path = log_path
        self.logf = open(log_path, "w")
        self.start_ts = time.time()
        self.spend_in = 0
        self.spend_out = 0
        self.model_calls = 0
        # test-module helpers (single source of truth for mechanics)
        import test_econ_validation_phase1 as T
        self.T = T
        self.client = None
        self.db_path = None
        self.agents = []  # dicts: name, key, pk, brief, furnace_xy, farm_id

    # -- logging ------------------------------------------------------
    def log(self, record: dict):
        record = {"ts": time.time(), **record}
        self.logf.write(json.dumps(record) + "\n")
        self.logf.flush()

    def dollars(self) -> float:
        return (self.spend_in / 1e6 * RATE_IN_PER_M
                + self.spend_out / 1e6 * RATE_OUT_PER_M)

    # -- world setup --------------------------------------------------
    def setup_world(self):
        T = self.T
        tmp = tempfile.NamedTemporaryFile(suffix=".db", delete=False)
        tmp.close()
        self.db_path = tmp.name
        os.environ["AC_DB_PATH"] = self.db_path
        os.environ["AC_OPERATOR_PUBKEY"] = T.pubkey_hex(T.make_key())
        import server.app as appmod
        importlib.reload(appmod)
        for k in ("gather", "refine", "move", "build", "claim", "farm",
                  "plant", "harvest", "trade_offer", "trade_accept", "eat",
                  "craft", "chat"):
            appmod.RATE_LIMITS[k] = (10000, 60)
        from fastapi.testclient import TestClient
        self.client = TestClient(appmod.app)
        # Winter pinned: genesis 45d -> (45//14)%4 = 3 -> winter.
        T.set_genesis_days_ago(self.db_path, 45)
        briefs = {
            "exp-01": (REPO / "phase2" / "brief_exp_01.md").read_text(),
            "exp-02": (REPO / "phase2" / "brief_exp_02.md").read_text(),
        }
        specs = [("exp-01", ("plow",)), ("exp-02", ("ore_bounty",))]
        for name, advantages in specs:
            key = T.make_key()
            T.register(self.client, name, key)
            T.spawn(self.client, key)
            pk = T.pubkey_hex(key)
            # Endowments per protocol §3.2 (documented, fixed).
            for tool in ("crude_axe", "crude_pick", "crude_sickle"):
                T.grant_tool(self.db_path, pk, tool, durability=300)
            for adv in advantages:
                T.grant_tool(self.db_path, pk, adv, durability=300)
            st = T.me(self.client, key)
            x, y = st["x"], st["y"]
            T.signed_request(self.client, key, "POST", "/world/claim",
                             {"x": x, "y": y})
            T.set_inventory(self.db_path, pk,
                            {"stone": 4, "clay": 2, "timber": 2})
            r = T.signed_request(self.client, key, "POST", "/world/build",
                                 {"kind": "furnace", "x": x, "y": y,
                                  "name": f"{name} furnace"})
            furnace_id = r["id"]
            conn = T.db(self.db_path)
            try:
                adj = conn.execute(
                    "SELECT x, y FROM world_tiles WHERE terrain != 'ocean'"
                    " AND (ABS(x - ?) + ABS(y - ?)) = 1"
                    " AND NOT EXISTS (SELECT 1 FROM structures s"
                    " WHERE s.x = world_tiles.x AND s.y = world_tiles.y)"
                    " LIMIT 1", (x, y)).fetchone()
                assert adj is not None, "no adjacent free tile for farm"
                fx, fy = adj["x"], adj["y"]
            finally:
                conn.close()
            T.signed_request(self.client, key, "POST", "/world/claim",
                             {"x": fx, "y": fy})
            T.set_inventory(self.db_path, pk, {"timber": 2, "grain": 2})
            r = T.signed_request(self.client, key, "POST", "/world/build",
                                 {"kind": "farm", "x": fx, "y": fy,
                                  "name": f"{name} farm"})
            self.agents.append({
                "name": name, "key": key, "pk": pk,
                "brief": briefs[name], "furnace_xy": (x, y),
                "farm_id": r["id"], "actions": 0,
                "wait_until": 0, "next_wake": 0,
            })
        self.log({
            "event": "run_start",
            "harness": HARNESS_VERSION,
            "model": PINNED_MODEL,
            "model_adapter": self.adapter.name,
            "brief_sha256": {
                a["name"]: hashlib.sha256(
                    a["brief"].encode()).hexdigest()
                for a in self.agents},
            "dollar_cap": self.cfg.dollar_cap,
            "token_caps": {"per_tick_in": self.cfg.per_tick_in_cap,
                           "per_tick_out": self.cfg.per_tick_out_cap,
                           "max_calls": self.cfg.max_model_calls},
            "action_cap_per_agent": self.cfg.max_actions_per_agent,
            "wall_cap_seconds": self.cfg.max_wall_seconds,
            "dry_run": self.cfg.dry_run,
        })

    # -- tick ---------------------------------------------------------
    def build_prompt(self, agent: dict) -> str:
        T = self.T
        me = T.me(self.client, agent["key"])
        inv = T.inventory_of(self.db_path, agent["pk"])
        others = self.client.get("/world/agents").json()
        offers = self.client.get("/trade/offers").json()
        chat = self.client.get("/chat",
                               params={"room": "general",
                                       "limit": 5,
                                       "order": "desc"}).json()
        msgs = chat["messages"] if isinstance(chat, dict) else chat
        state = {
            "tick_wall_s": round(time.time() - self.start_ts, 1),
            "ap": me.get("ap"), "pos": [me.get("x"), me.get("y")],
            "inventory": inv,
            "agents_visible": [a.get("agent_name") for a in others],
            "open_offers": [
                {"id": o["id"], "maker": o["maker_name"],
                 "give": o["give"], "want": o["want"]} for o in offers],
            "recent_chat": [
                {"by": m.get("agent_name"), "text": m.get("text")[:160]}
                for m in msgs],
            "actions_so_far": agent["actions"],
        }
        return (agent["brief"]
                + "\n\n--- CURRENT STATE (tick; respond with exactly one "
                  "JSON action object) ---\n"
                + json.dumps(state, indent=1)
                + '\nValid actions: move {"dir":"N|S|E|W"}, gather '
                  '{"resource":"..."}, farm_plant/farm_harvest {"slot":0-3}, '
                  'refine {"item":"flour|iron"}, chat {"text":"..."}, '
                  'trade_offer {"give":{},"want":{}}, trade_accept '
                  '{"offer_id":N}, wait {"minutes":N}. '
                  'Schema: {"action":"...","params":{...},"reasoning":"..."}')

    def check_budget_before_call(self) -> bool:
        """True if another model call is allowed; else log halt."""
        if self.model_calls >= self.cfg.max_model_calls:
            self.log({"event": "technical_stop",
                      "reason": "max_model_calls_reached",
                      "model_calls": self.model_calls})
            return False
        # worst-case next-call cost must fit under the cap
        worst = (self.cfg.per_tick_in_cap / 1e6 * RATE_IN_PER_M
                 + self.cfg.per_tick_out_cap / 1e6 * RATE_OUT_PER_M)
        if self.dollars() + worst > self.cfg.dollar_cap:
            self.log({"event": "technical_stop",
                      "reason": "budget_cap_reached",
                      "spend_dollars": round(self.dollars(), 4),
                      "dollar_cap": self.cfg.dollar_cap})
            return False
        return True

    ACTION_ROUTES = {
        "move": ("POST", "/world/move", lambda p: {"dir": p["dir"]}),
        "gather": ("POST", "/world/gather",
                   lambda p: {"resource": p["resource"]}),
        "farm_plant": ("POST", "/world/farm",
                       lambda p, a: {"structure_id": a["farm_id"],
                                     "action": "plant",
                                     "slot": p["slot"]}),
        "farm_harvest": ("POST", "/world/farm",
                         lambda p, a: {"structure_id": a["farm_id"],
                                       "action": "harvest",
                                       "slot": p["slot"]}),
        "refine": ("POST", "/world/refine", lambda p: {"item": p["item"]}),
        "chat": ("POST", "/chat", lambda p: {"text": p["text"]}),
        "trade_offer": ("POST", "/trade/offers",
                        lambda p: {"give": p["give"], "want": p["want"]}),
        "trade_accept": ("POST", None, lambda p, a: p["offer_id"]),
    }

    def execute_action(self, agent: dict, action: dict) -> dict:
        """Sign and POST one parsed action. Returns the tick record."""
        T = self.T
        name, key = agent["name"], agent["key"]
        act = action.get("action")
        params = action.get("params", {}) or {}
        reasoning = action.get("reasoning", "")
        rec = {"event": "tick", "agent": name, "action": act,
               "params": params, "stated_reasoning": reasoning[:500]}
        ap_before = T.me(self.client, key)["ap"]
        inv_before = T.inventory_of(self.db_path, agent["pk"])
        if act == "wait":
            mins = min(int(params.get("minutes", 1)), 120)
            agent["wait_until"] = time.time() + mins * 60
            agent["next_wake"] = time.time() + self.cfg.wake_check_seconds
            rec.update({"http_status": "n/a(wait)",
                        "note": f"waiting {mins}m: no model calls until "
                                f"wake (dry-run: no sleep)"})
            if self.cfg.dry_run:
                agent["wait_until"] = 0  # dry-run: don't actually wait
        elif act in self.ACTION_ROUTES:
            method, path, mk = self.ACTION_ROUTES[act]
            try:
                if act == "trade_accept":
                    oid = mk(params, agent)
                    path = f"/trade/offers/{oid}/accept"
                    payload = {}
                else:
                    payload = mk(params, agent) \
                        if mk.__code__.co_argcount == 2 else mk(params)
                body = json.dumps(payload, separators=(",", ":"))
                headers = T.sign(key, method, path, body)
                r = self.client.request(
                    method, path, content=body.encode(),
                    headers={**headers,
                             "Content-Type": "application/json"})
                rec["http_status"] = r.status_code
                rec["response"] = r.text[:300]
                if r.status_code in (200, 201):
                    agent["actions"] += 1
            except Exception as e:  # transport-level failure, logged
                rec["http_status"] = "transport_error"
                rec["response"] = f"{type(e).__name__}: {e}"[:300]
        else:
            rec["http_status"] = "rejected"
            rec["response"] = f"unknown action: {act}"
        rec["ap_delta"] = round(T.me(self.client, key)["ap"] - ap_before, 2)
        inv_after = T.inventory_of(self.db_path, agent["pk"])
        rec["inventory_delta"] = {
            k: inv_after.get(k, 0) - inv_before.get(k, 0)
            for k in set(inv_before) | set(inv_after)
            if inv_after.get(k, 0) != inv_before.get(k, 0)}
        return rec

    def tick(self, agent: dict) -> str:
        """One agent tick. Returns 'ok', 'cap', or 'halt'."""
        if agent["actions"] >= self.cfg.max_actions_per_agent:
            self.log({"event": "agent_done", "agent": agent["name"],
                      "reason": "action_cap_reached",
                      "actions": agent["actions"]})
            return "cap"
        if time.time() - self.start_ts > self.cfg.max_wall_seconds:
            self.log({"event": "technical_stop",
                      "reason": "wall_clock_cap_reached"})
            return "halt"
        now = time.time()
        if now < agent.get("wait_until", 0):
            # Waiting: no model call. Wake-check every 15 min to
            # re-read state (logged, no inference).
            if now >= agent.get("next_wake", 0):
                agent["next_wake"] = now + self.cfg.wake_check_seconds
                T = self.T
                self.log({"event": "wake_check", "agent": agent["name"],
                          "ap": T.me(self.client, agent["key"])["ap"],
                          "inventory": T.inventory_of(
                              self.db_path, agent["pk"])})
            return "ok"
        if not self.check_budget_before_call():
            return "halt"
        prompt = self.build_prompt(agent)
        try:
            text, tin, tout = self.adapter.complete(prompt)
        except Exception as e:
            self.log({"event": "tick_error", "agent": agent["name"],
                      "error": f"{type(e).__name__}: {e}"[:200]})
            return "ok"
        self.model_calls += 1
        # per-tick token caps (hard)
        tin_c = min(tin, self.cfg.per_tick_in_cap)
        tout_c = min(tout, self.cfg.per_tick_out_cap)
        self.spend_in += tin_c
        self.spend_out += tout_c
        if tin > self.cfg.per_tick_in_cap or tout > self.cfg.per_tick_out_cap:
            self.log({"event": "tick_truncated", "agent": agent["name"],
                      "in": tin, "out": tout})
        try:
            action = json.loads(text)
            assert isinstance(action, dict) and "action" in action
        except (json.JSONDecodeError, AssertionError):
            self.log({"event": "tick_parse_failure",
                      "agent": agent["name"],
                      "raw": text[:300],
                      "in_tokens": tin_c, "out_tokens": tout_c})
            return "ok"
        rec = self.execute_action(agent, action)
        rec.update({"in_tokens": tin_c, "out_tokens": tout_c,
                    "spend_dollars": round(self.dollars(), 4),
                    "model_calls": self.model_calls})
        self.log(rec)
        return "ok"

    def run(self):
        self.setup_world()
        tick_budget = (self.cfg.dry_run_ticks
                       if self.cfg.dry_run else 10 ** 9)
        ticks = 0
        try:
            while ticks < tick_budget:
                all_capped = True
                for agent in self.agents:
                    st = self.tick(agent)
                    if st == "halt":
                        self.log({"event": "run_end",
                                  "reason": "halt",
                                  "ticks": ticks,
                                  "spend_dollars": round(self.dollars(), 4)})
                        return
                    if st == "ok":
                        all_capped = False
                    ticks += 1
                    if not self.cfg.dry_run:
                        # ~tick_interval per agent: split across agents.
                        time.sleep(self.cfg.tick_interval_seconds
                                   / max(1, len(self.agents)))
                if all_capped:
                    break
        finally:
            self.log({"event": "run_end", "reason": "loop_complete",
                      "ticks": ticks,
                      "spend_dollars": round(self.dollars(), 4),
                      "model_calls": self.model_calls,
                      "actions": {a["name"]: a["actions"]
                                  for a in self.agents}})
            self.logf.close()


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--dry-run", action="store_true")
    ap.add_argument("--ticks", type=int, default=4)
    ap.add_argument("--max-actions", type=int, default=150)
    ap.add_argument("--dollar-cap", type=float, default=4.00)
    ap.add_argument("--log", type=str, default=None)
    args = ap.parse_args()

    cfg = RunConfig(dry_run=args.dry_run, dry_run_ticks=args.ticks,
                    max_actions_per_agent=args.max_actions,
                    dollar_cap=args.dollar_cap,
                    tick_interval_seconds=0 if args.dry_run else 120)
    log_path = (Path(args.log) if args.log else
                Path(tempfile.mkdtemp(prefix="phase2-")) / "run.jsonl")
    print(f"[phase2-runner] log -> {log_path}", flush=True)
    runner = Phase2Runner(cfg, StubAdapter(), log_path)
    runner.run()
    print(f"[phase2-runner] done. spend=${runner.dollars():.4f} "
          f"calls={runner.model_calls}", flush=True)


if __name__ == "__main__":
    main()
