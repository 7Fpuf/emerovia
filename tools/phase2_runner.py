#!/usr/bin/env python3
"""Phase 2 blind-discovery experiment runner (0.1.0).

Runs the two experiment agents (exp-01 / exp-02) against a TEMPORARY
Emerovia world database — never production. Each tick: GET world state
-> prompt the model -> parse exactly ONE action -> sign and POST ->
log everything to a signed evidence archive.

Boundaries (hard):
- AC_DB_PATH is always a temp file. Production is never touched.
- No real money, no cryptocurrency, no token mechanics.
- Dry-run mode (--dry-run) uses a scripted StubAdapter: zero real
  inference, no behavioral experiment. A NON-dry-run invocation
  constructs the pinned-model API adapter; if it cannot be built
  (missing endpoint/credentials) the run FAILS LOUDLY at startup —
  the stub is NEVER used as a silent fallback.
- The behavioral experiment does NOT launch without Trevor's explicit
  approval (protocol §6).

Usage:
    # mechanics dry-run: 4 scripted ticks per agent, tiny budget
    .venv/bin/python tools/phase2_runner.py --dry-run --ticks 4

    # budget-halt verification (halts on the first tick)
    .venv/bin/python tools/phase2_runner.py --dry-run --ticks 4 \
        --dollar-cap 0.000001

    # action-cap verification
    .venv/bin/python tools/phase2_runner.py --dry-run --ticks 10 \
        --max-actions 2

    # verify an evidence archive (offline, no model, no world needed)
    .venv/bin/python tools/phase2_runner.py --verify-archive <ARCHIVE_DIR>

    # resume a run from its archive (restores world DB + runner state)
    .venv/bin/python tools/phase2_runner.py --resume <ARCHIVE_DIR> \
        --dry-run --ticks 8

    # REAL (behavioral) run — requires PHASE2_MODEL_API_URL and
    # PHASE2_MODEL_API_KEY in the environment, plus Trevor's approval.
    # Refuses to start without them; never falls back to the stub.
    PHASE2_MODEL_API_URL=... PHASE2_MODEL_API_KEY=... \
        .venv/bin/python tools/phase2_runner.py --archive <DIR>
"""

from __future__ import annotations

import argparse
import hashlib
import importlib
import json
import os
import shutil
import sys
import tempfile
import time
import urllib.error
import urllib.request
from dataclasses import dataclass, field
from pathlib import Path

REPO = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(REPO))
sys.path.insert(0, str(REPO / "tests"))

# Pinned model + rates (standard tier, Meta Model API; verified 2026-10-06
# and re-verified 2026-10-07 via public pricing pages: $1.25/M input,
# $4.25/M output, $0.15/M cached input — cached input not used here).
# Re-verify at launch; the TOKEN caps below are the binding constraint,
# the dollar cap is recomputed from whatever rates are current and
# pre-registered before start.
PINNED_MODEL = "muse-spark-1.3"
RATE_IN_PER_M = 1.25
RATE_OUT_PER_M = 4.25

HARNESS_VERSION = "phase2-runner/0.1.1"

ENV_API_URL = "PHASE2_MODEL_API_URL"
ENV_API_KEY = "PHASE2_MODEL_API_KEY"


# --------------------------------------------------------------------------
# Errors


class AdapterConfigError(RuntimeError):
    """The real model adapter cannot be constructed (missing endpoint /
    credentials). Raised LOUDLY at startup; never silently downgraded."""


class TokenLimitExceeded(RuntimeError):
    """A model request was REJECTED BEFORE SENDING because its estimated
    token count exceeded the per-tick cap. No API call was made, no
    tokens were billed, no spend was recorded."""


class BilledCapAnomaly(RuntimeError):
    """The model API reported BILLED token usage above the per-tick caps.
    The budget math can no longer be trusted: the run halts as a
    technical stop. This must never happen silently."""


class ModelAPIError(RuntimeError):
    """Transport- or API-level failure talking to the model endpoint.

    Billing status is UNKNOWN: the request may have been received and
    billed before the error/timeout occurred. The runner treats this as
    a fail-closed condition (technical stop), never a retry."""


class UncertainBillingHalt(RuntimeError):
    """Internal marker: the run stopped because a model-API failure left
    billing uncertain. Recorded as a technical_stop with reason
    'uncertain_model_billing' plus an unknown-spend record in
    budget.json."""


# --------------------------------------------------------------------------
# Token estimation (heuristic; the API's BILLED usage is authoritative)


def estimate_tokens(text: str) -> int:
    """~4 chars/token heuristic for pre-send limit checks. Conservative
    for English prose; the billed usage reported by the API is what
    gets accounted."""
    return max(1, len(text) // 4)


# --------------------------------------------------------------------------
# Model adapters


class ModelAdapter:
    """Produces one action-decision per tick. Returns
    (response_text, input_tokens, output_tokens).

    `enforces_limits`: True for adapters that enforce per-tick token
    caps at the API boundary and report REAL billed usage. False for
    the synthetic dry-run stub (estimates only).
    """

    name = "base"
    enforces_limits = False

    def complete(self, prompt: str) -> tuple[str, int, int]:
        raise NotImplementedError


class StubAdapter(ModelAdapter):
    """Scripted, deterministic stand-in for dry-runs ONLY. Exercises the
    full tick pipeline (prompt -> parse -> sign/POST -> record) with
    zero real inference and zero behavioral content. Must never be
    used for a non-dry-run invocation (see build_adapter)."""

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


def default_transport(api_url: str, headers: dict, payload: dict,
                      timeout_s: int) -> dict:
    """Real HTTP transport for the pinned-model API. Returns the parsed
    JSON response. Any failure raises ModelAPIError."""
    req = urllib.request.Request(
        api_url, data=json.dumps(payload).encode("utf-8"),
        headers={**headers, "Content-Type": "application/json"},
        method="POST")
    try:
        with urllib.request.urlopen(req, timeout=timeout_s) as resp:
            return json.loads(resp.read().decode("utf-8"))
    except urllib.error.HTTPError as e:
        body = e.read().decode("utf-8", errors="replace")[:500]
        raise ModelAPIError(f"model API HTTP {e.code}: {body}")
    except (urllib.error.URLError, TimeoutError, OSError) as e:
        raise ModelAPIError(f"model API transport failure: {e}")


class PinnedModelAdapter(ModelAdapter):
    """The REAL adapter for the pinned model (muse-spark-1.3).

    Assumed API shape (documented, to re-verify at launch): an
    OpenAI-compatible chat-completions endpoint —
    POST {api_url} with {"model", "messages", "max_tokens",
    "temperature"}; response {"choices":[{"message":{"content": ...}}],
    "usage":{"prompt_tokens": N, "completion_tokens": M}}.
    If the operator's endpoint uses a different shape, ONLY this
    class's request/response mapping changes — the enforcement and
    accounting layers above it do not.

    Enforcement (hard, at the API boundary):
    - Prompt estimated > per_tick_in_cap -> TokenLimitExceeded BEFORE
      sending. Nothing billed, nothing recorded as spend.
    - max_tokens = per_tick_out_cap is set on the request (API-side
      enforcement of the output cap).
    - Billed usage comes from the response's `usage` block — real
      billed tokens, never estimates. Missing usage -> ModelAPIError.
    - Billed usage above either per-tick cap -> BilledCapAnomaly
      (the run must halt; the budget math is untrustworthy).
    - Any transport/API failure -> ModelAPIError. Billing is then
      UNKNOWN (a timeout or 5xx may have been processed server-side),
      so the runner FAILS CLOSED (technical stop, unknown-spend
      recorded) rather than retrying blindly.

    Defense in depth beyond this class (launch-handoff operator step):
    in the provider's billing console, put a hard spend limit / budget
    alert at or below the experiment cap on a DEDICATED experiment API
    key before launch, and never reuse a general-purpose key for the
    pilot. The runner's in-code $4.50 cap is the primary safeguard; the
    provider-side limit is the backstop. Console steps are provider-
    specific — verify them at launch, do not assume a UI path."""

    name = "pinned-muse-spark-1.3-api"
    enforces_limits = True

    def __init__(self, api_url: str | None = None,
                 api_key: str | None = None,
                 model: str = PINNED_MODEL,
                 per_tick_in_cap: int = 6000,
                 # 750: resized from 500 after the 2026-10-07 tick-1 abort,
                 # where muse-spark-1.3 burned the full 500-token output
                 # budget on reasoning (content=null,
                 # finish_reason=length). 500 observed reasoning burn +
                 # 250 headroom for the tiny JSON action. Keep in sync
                 # with RunConfig.per_tick_out_cap (main() wires them).
                 per_tick_out_cap: int = 750,
                 temperature: float = 0.7,
                 timeout_s: int = 120,
                 transport=None):
        url = api_url or os.environ.get(ENV_API_URL)
        key = api_key or os.environ.get(ENV_API_KEY)
        missing = []
        if not url:
            missing.append(ENV_API_URL)
        if not key:
            missing.append(ENV_API_KEY)
        if missing:
            raise AdapterConfigError(
                "PinnedModelAdapter: missing " + ", ".join(missing) + ". "
                "A non-dry-run Phase 2 invocation requires the pinned-model "
                "API endpoint and credentials in the launch environment "
                f"({ENV_API_URL} and {ENV_API_KEY}). The runner REFUSES to "
                "fall back to the scripted stub: fix the environment and "
                "retry. The stub is dry-run-only by design.")
        self.api_url = url
        self.api_key = key
        self.model = model
        self.per_tick_in_cap = per_tick_in_cap
        self.per_tick_out_cap = per_tick_out_cap
        self.temperature = temperature
        self.timeout_s = timeout_s
        self.transport = transport or default_transport

    def complete(self, prompt: str) -> tuple[str, int, int]:
        est = estimate_tokens(prompt)
        if est > self.per_tick_in_cap:
            raise TokenLimitExceeded(
                f"prompt estimated ~{est} tokens exceeds per-tick input "
                f"cap {self.per_tick_in_cap}: request REJECTED before "
                "sending (no API call, no billed usage)")
        payload = {
            "model": self.model,
            "messages": [{"role": "user", "content": prompt}],
            "max_tokens": self.per_tick_out_cap,
            "temperature": self.temperature,
        }
        try:
            resp = self.transport(
                self.api_url,
                {"Authorization": f"Bearer {self.api_key}"},
                payload, self.timeout_s)
        except ModelAPIError:
            raise
        except Exception as e:  # a custom transport's own failure mode
            raise ModelAPIError(
                f"model transport failure: {type(e).__name__}: {e}")
        try:
            text = resp["choices"][0]["message"]["content"]
            usage = resp.get("usage") or {}
            billed_in = usage.get("prompt_tokens")
            billed_out = usage.get("completion_tokens")
        except (KeyError, IndexError, TypeError) as e:
            raise ModelAPIError(f"unparseable model response: {e}")
        if billed_in is None or billed_out is None:
            raise ModelAPIError(
                "model response carried no billed token usage "
                "(`usage.prompt_tokens` / `usage.completion_tokens`); "
                "spend cannot be accounted — refusing to continue")
        if billed_in > self.per_tick_in_cap or \
                billed_out > self.per_tick_out_cap:
            raise BilledCapAnomaly(
                f"billed usage in={billed_in} out={billed_out} exceeded "
                f"per-tick caps ({self.per_tick_in_cap}/"
                f"{self.per_tick_out_cap}); budget math untrustworthy")
        return text, int(billed_in), int(billed_out)


def build_adapter(dry_run: bool,
                  cfg: RunConfig | None = None) -> ModelAdapter:
    """Adapter factory. dry-run -> scripted stub. Anything else -> the
    REAL pinned-model adapter, which raises AdapterConfigError loudly
    if it cannot be constructed. There is NO silent stub fallback.

    The adapter's token caps are wired from the run config so the
    API-side max_tokens can never drift from the budget math's
    per-tick caps (the 2026-10-07 abort was a cap the config knew
    but the wire request enforced differently — now impossible)."""
    if dry_run:
        return StubAdapter()
    if cfg is None:
        return PinnedModelAdapter()
    return PinnedModelAdapter(
        per_tick_in_cap=cfg.per_tick_in_cap,
        per_tick_out_cap=cfg.per_tick_out_cap,
        temperature=cfg.model_temperature)


# Phase 2 stockpile objectives (protocol §3): both agents must END
# holding at least these amounts. Verified from the world DB, never
# from stated claims.
OBJECTIVE_IRON = 10
OBJECTIVE_FLOUR = 16


# --------------------------------------------------------------------------
# Config


@dataclass
class RunConfig:
    max_actions_per_agent: int = 150
    max_wall_seconds: int = 6 * 3600
    per_tick_in_cap: int = 6000
    # 750 = 500 observed reasoning burn on the 2026-10-07 tick-1 abort
    # (content=null, finish_reason=length) + 250 headroom for the tiny
    # JSON action. Do NOT shrink this to save money without review: a
    # smaller cap truncates the model's reasoning, not its bill.
    per_tick_out_cap: int = 750
    max_model_calls: int = 400
    # Hard experiment budget: $4.50 is the smallest clean number above
    # the $4.275 worst case (400 x (6000x$1.25 + 750x$4.25)/1M).
    dollar_cap: float = 4.50
    # Consecutive empty model responses (content=null, e.g. reasoning
    # exhausted the output cap) before the run ends as a technical
    # stop. 4 bounds the waste at ~$0.04 while tolerating one unlucky
    # tick; 4-in-a-row signals a systematic problem, not noise.
    max_empty_streak: int = 4
    tick_interval_seconds: int = 120
    wake_check_seconds: int = 900
    model_temperature: float = 0.7  # pre-registered model-call parameter
    dry_run: bool = False
    dry_run_ticks: int = 4


# --------------------------------------------------------------------------
# Runner


def sha256_file(path: Path) -> str:
    h = hashlib.sha256()
    with open(path, "rb") as f:
        for chunk in iter(lambda: f.read(65536), b""):
            h.update(chunk)
    return h.hexdigest()


class Phase2Runner:
    def __init__(self, config: RunConfig, adapter: ModelAdapter,
                 archive_dir: Path, log_name: str = "run.jsonl",
                 resume: bool = False):
        self.cfg = config
        self.adapter = adapter
        self.archive_dir = Path(archive_dir)
        self.archive_dir.mkdir(parents=True, exist_ok=True)
        self.log_path = self.archive_dir / log_name
        self.logf = open(self.log_path, "a" if resume else "w")
        self.start_ts = time.time()
        self.ticks = 0
        self.spend_in = 0
        self.spend_out = 0
        self.model_calls = 0
        self.budget_calls: list[dict] = []
        # Fail-closed billing records: each entry is an event where a
        # model-API failure left spend UNKNOWN (may have been billed).
        # Preserved in budget.json; the run stops at the first one.
        self.uncertain_billing: list[dict] = []
        # test-module helpers (single source of truth for mechanics)
        import test_econ_validation_phase1 as T
        self.T = T
        self.client = None
        self.db_path = None
        self.agents = []  # dicts: name, key, pk, brief, furnace_xy, farm_id
        self._world_ready = False

    # -- logging ------------------------------------------------------
    def log(self, record: dict):
        record = {"ts": time.time(), **record}
        self.logf.write(json.dumps(record) + "\n")
        self.logf.flush()

    def dollars(self) -> float:
        return (self.spend_in / 1e6 * RATE_IN_PER_M
                + self.spend_out / 1e6 * RATE_OUT_PER_M)

    def objectives_complete(self) -> dict | None:
        """Check the Phase 2 stockpile objectives against the
        AUTHORITATIVE world DB: every agent holds >= OBJECTIVE_IRON
        iron AND >= OBJECTIVE_FLOUR flour. Returns the per-agent
        inventory evidence, or None if any agent is short. Never
        trusts stated claims — only verified world state."""
        if not self.agents or not self.db_path:
            return None
        evidence = {}
        for a in self.agents:
            inv = self.T.inventory_of(self.db_path, a["pk"])
            iron = inv.get("iron", 0)
            flour = inv.get("flour", 0)
            evidence[a["name"]] = {"iron": iron, "flour": flour}
            if iron < OBJECTIVE_IRON or flour < OBJECTIVE_FLOUR:
                return None
        return evidence

    def log_objectives_complete(self, evidence: dict):
        self.log({"event": "run_objectives_complete",
                  "inventories": evidence,
                  "objectives": {"iron": OBJECTIVE_IRON,
                                 "flour": OBJECTIVE_FLOUR},
                  "ticks": self.ticks,
                  "model_calls": self.model_calls,
                  "spend_dollars": round(self.dollars(), 4)})

    # -- world setup --------------------------------------------------
    def _attach_client(self):
        """(Re)attach the FastAPI test client to self.db_path with the
        experiment's lifted rate limits. Used by setup and resume."""
        T = self.T
        os.environ["AC_DB_PATH"] = self.db_path
        import server.app as appmod
        importlib.reload(appmod)
        for k in ("gather", "refine", "move", "build", "claim", "farm",
                  "plant", "harvest", "trade_offer", "trade_accept", "eat",
                  "craft", "chat"):
            appmod.RATE_LIMITS[k] = (10000, 60)
        from fastapi.testclient import TestClient
        self.client = TestClient(appmod.app)

    def setup_world(self):
        T = self.T
        tmp = tempfile.NamedTemporaryFile(suffix=".db", delete=False)
        tmp.close()
        self.db_path = tmp.name
        os.environ["AC_OPERATOR_PUBKEY"] = T.pubkey_hex(T.make_key())
        self._attach_client()
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
                "farm_id": r["id"], "actions": 0, "tick_seq": 0,
                "wait_until": 0, "next_wake": 0, "empty_streak": 0,
            })
        self.log({
            "event": "run_start",
            "harness": HARNESS_VERSION,
            "model": PINNED_MODEL,
            "model_adapter": self.adapter.name,
            "adapter_enforces_limits": self.adapter.enforces_limits,
            "model_temperature": self.cfg.model_temperature,
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
        self._write_snapshot("start")
        self._world_ready = True

    # -- observation pipeline ------------------------------------------
    def _signed_get(self, key, path: str, params: dict | None = None):
        """Signed GET against the test client (body is empty; the
        signature covers ts/method/path/empty-body per server auth)."""
        T = self.T
        headers = T.sign(key, "GET", path, "")
        r = self.client.request("GET", path, params=params or {},
                                headers=headers)
        r.raise_for_status()
        return r.json()

    def _fit_sections(self, sections: list[tuple[str, str]],
                      budget_tokens: int) -> tuple[str, list[str]]:
        """Assemble prompt sections in priority order under a token
        budget. Returns (text, dropped_section_names)."""
        kept, dropped, used = [], [], 0
        for name, text in sections:
            cost = estimate_tokens(text)
            if used + cost <= budget_tokens:
                kept.append((name, text))
                used += cost
            else:
                dropped.append(name)
        body = "\n".join(f"[{name}]\n{text}" for name, text in kept)
        return body, dropped

    def build_prompt(self, agent: dict) -> str:
        """Full ordinary public world information per protocol §7:
        /world/me, /world/info (season/day), disclosed map window,
        own structures incl. farm slot states, public recipes, agent
        list, open trade offers, recent ledger, recent chat.
        Navigational/operational info only — zero strategy hints."""
        T = self.T
        key = agent["key"]
        me = T.me(self.client, key)
        inv = T.inventory_of(self.db_path, agent["pk"])
        info = self.client.get("/world/info").json()
        seasons = info.get("seasons", {})
        cur = seasons.get("season")
        mults = (seasons.get("multipliers", {}).get("grain", {})
                 if isinstance(seasons.get("multipliers"), dict) else {})
        # Disclosed (public) map window around the agent — navigation.
        pmap = self.client.get("/world/map").json()
        mx, my = me.get("x"), me.get("y")
        nearby = sorted(
            ({"x": t["x"], "y": t["y"], "terrain": t["terrain"]}
             for t in pmap.get("tiles", [])
             if abs(t["x"] - mx) + abs(t["y"] - my) <= 6),
            key=lambda t: abs(t["x"] - mx) + abs(t["y"] - my))[:80]
        # Public structure census, own structures only (farm slot
        # growth stages are public per /world/structures).
        structs = self.client.get("/world/structures").json()
        own_structs = [
            {k: s.get(k) for k in
             ("id", "kind", "x", "y", "name", "slots", "growth", "stage")}
            for s in (structs if isinstance(structs, list) else [])
            if s.get("owner_pubkey") == agent["pk"]]
        # Public recipe book (signed read; public info).
        try:
            recipes = self._signed_get(key, "/world/recipes")
        except Exception:
            recipes = {"error": "recipes unavailable"}
        if isinstance(recipes, dict):
            recipes = recipes.get("recipes", recipes)
        if isinstance(recipes, list):
            recipes = recipes[:12]
        others = self.client.get("/world/agents").json()
        offers = self.client.get("/trade/offers").json()
        ledger = self.client.get("/trade/ledger",
                                 params={"limit": 5}).json()
        chat = self.client.get("/chat",
                               params={"room": "general", "limit": 8,
                                       "order": "desc"}).json()
        msgs = chat["messages"] if isinstance(chat, dict) else chat

        core = {
            "tick_wall_s": round(time.time() - self.start_ts, 1),
            "season": cur,
            "day": seasons.get("days_since_genesis"),
            "season_grain_multiplier_now": mults.get(cur) if cur else None,
            "ap": me.get("ap"), "pos": [mx, my],
            "inventory": inv,
            "chits": me.get("chits"),
            "actions_so_far": agent["actions"],
        }
        sections = [
            ("core_state", json.dumps(core)),
            ("open_trade_offers", json.dumps([
                {"id": o["id"], "maker": o.get("maker_name"),
                 "give": o.get("give"), "want": o.get("want")}
                for o in (offers if isinstance(offers, list) else [])])),
            ("recent_chat", json.dumps([
                {"by": m.get("agent_name"), "text": str(m.get("text"))[:160]}
                for m in (msgs if isinstance(msgs, list) else [])])),
            ("own_structures", json.dumps(own_structs)),
            ("nearby_disclosed_tiles", json.dumps(nearby)),
            ("recent_filled_trades", json.dumps(ledger)),
            ("agents_visible", json.dumps(
                [a.get("agent_name") for a in
                 (others if isinstance(others, list) else [])])),
            ("public_recipes", json.dumps(recipes)[:4000]),
        ]
        # State budget: per-tick cap minus the brief minus margin.
        budget = (self.cfg.per_tick_in_cap
                  - estimate_tokens(agent["brief"]) - 600)
        body, dropped = self._fit_sections(sections, max(1200, budget))
        note = ("\n[note: omitted for prompt size: "
                + ", ".join(dropped) + "]") if dropped else ""
        return (agent["brief"]
                + "\n\n--- CURRENT STATE (tick; respond with exactly one "
                  "JSON action object) ---\n"
                + body + note
                + '\nValid actions: move {"dir":"N|S|E|W"}, gather '
                  '{"resource":"..."}, farm_plant/farm_harvest {"slot":0-3}, '
                  'refine {"item":"flour|iron"}, chat {"text":"..."}, '
                  'trade_offer {"give":{},"want":{}}, trade_accept '
                  '{"offer_id":N}, wait {"minutes":N}. '
                  'Schema: {"action":"...","params":{...},"reasoning":"..."}')

    # -- snapshots + archive -------------------------------------------
    def snapshot_world(self) -> dict:
        """Full evidence snapshot of the experiment world."""
        T = self.T
        agents = []
        for a in self.agents:
            me = T.me(self.client, a["key"])
            agents.append({
                "name": a["name"], "pubkey": a["pk"],
                "x": me.get("x"), "y": me.get("y"),
                "ap": me.get("ap"), "chits": me.get("chits"),
                "inventory": T.inventory_of(self.db_path, a["pk"]),
                "signed_actions": a["actions"],
                "tick_seq": a["tick_seq"]})
        info = self.client.get("/world/info").json()
        return {
            "ts": time.time(),
            "harness": HARNESS_VERSION,
            "seasons": info.get("seasons"),
            "agents_in_world": info.get("agents_in_world"),
            "agents": agents,
            "structures": self.client.get("/world/structures").json(),
            "open_offers": self.client.get("/trade/offers").json(),
            "ledger": self.client.get(
                "/trade/ledger", params={"limit": 100}).json(),
        }

    def _write_snapshot(self, which: str) -> dict:
        snap = self.snapshot_world()
        path = self.archive_dir / "snapshots" / f"{which}.json"
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text(json.dumps(snap, indent=1))
        self.log({"event": f"snapshot_{which}",
                  "path": f"snapshots/{which}.json",
                  "sha256": sha256_file(path),
                  "agents": len(snap["agents"]),
                  "ledger_rows": len(snap["ledger"])})
        return snap

    def _write_json(self, name: str, obj: dict) -> Path:
        path = self.archive_dir / name
        path.write_text(json.dumps(obj, indent=1))
        return path

    def finalize_archive(self, reason: str):
        """Write the complete evidence bundle: end snapshot, full
        ledger, budget records, world DB copy, runner state, agent
        keys, and the manifest with SHA-256 of every file."""
        snap_end = self._write_snapshot("end")
        self._write_json("ledger.json", {
            "ts": time.time(),
            "rows": self.client.get(
                "/trade/ledger", params={"limit": 100}).json()})
        budget = {
            "ts": time.time(),
            "rates_per_m": {"in": RATE_IN_PER_M, "out": RATE_OUT_PER_M},
            "dollar_cap": self.cfg.dollar_cap,
            "calls": self.budget_calls,
            "totals": {
                "model_calls": self.model_calls,
                "in_tokens": self.spend_in,
                "out_tokens": self.spend_out,
                "spend_dollars": round(self.dollars(), 4)},
            # Fail-closed billing records: model-API failures whose
            # spend could not be determined. Each one halted the run;
            # preserved here so no possible charge goes unrecorded.
            "uncertain_billing": {
                "count": len(self.uncertain_billing),
                "events": self.uncertain_billing},
        }
        self._write_json("budget.json", budget)
        shutil.copy2(self.db_path, self.archive_dir / "world_end.db")
        self._write_json("runner_state.json", {
            "ts": time.time(),
            "ticks": self.ticks,
            "spend_in": self.spend_in,
            "spend_out": self.spend_out,
            "model_calls": self.model_calls,
            "agents": [{
                "name": a["name"], "actions": a["actions"],
                "tick_seq": a["tick_seq"]} for a in self.agents]})
        self._write_json("agent_keys.json", {
            a["name"]: {
                "pubkey": a["pk"],
                # Experiment-only temp keys (isolated world); needed
                # for archive resume.
                "privkey_hex": a["key"].encode().hex(),
                "brief_sha256": hashlib.sha256(
                    a["brief"].encode()).hexdigest()}
            for a in self.agents})
        manifest = {
            "harness": HARNESS_VERSION,
            "model": PINNED_MODEL,
            "model_adapter": self.adapter.name,
            "run_reason": reason,
            "created_ts": time.time(),
            "brief_sha256": {
                a["name"]: hashlib.sha256(a["brief"].encode()).hexdigest()
                for a in self.agents},
            "config": {
                "dollar_cap": self.cfg.dollar_cap,
                "per_tick_in_cap": self.cfg.per_tick_in_cap,
                "per_tick_out_cap": self.cfg.per_tick_out_cap,
                "max_model_calls": self.cfg.max_model_calls,
                "max_actions_per_agent": self.cfg.max_actions_per_agent,
                "max_wall_seconds": self.cfg.max_wall_seconds,
                "model_temperature": self.cfg.model_temperature,
                "dry_run": self.cfg.dry_run},
            "files": {},
        }
        # run_end is logged FIRST, then the log file is CLOSED: no
        # further appends are possible, so run.jsonl's hash is stable
        # and it joins the manifest like every other evidence file.
        # The action log is the experiment's central evidence record —
        # verify_archive checks it against this hash.
        self.log({"event": "run_end", "reason": reason,
                  "ticks": self.ticks,
                  "spend_dollars": round(self.dollars(), 4),
                  "model_calls": self.model_calls,
                  "actions": {a["name"]: a["actions"] for a in self.agents}})
        self.logf.flush()
        self.logf.close()
        for p in sorted(self.archive_dir.rglob("*")):
            if p.is_file() and p.name != "manifest.json":
                manifest["files"][p.relative_to(self.archive_dir).as_posix()] = \
                    sha256_file(p)
        self._write_json("manifest.json", manifest)

    # -- budget ---------------------------------------------------------
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

    # -- actions ----------------------------------------------------------
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
        """Sign and POST one parsed action. Returns the tick record.

        The per-agent action cap counts every SIGNED ATTEMPT — 200s,
        400s, 409s, and transport errors alike (a transport error may
        have executed server-side, so it counts conservatively).
        Unknown actions are never sent and do not count; `wait` is not
        a signed action and does not count."""
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
            agent["tick_seq"] += 1
            rec["tick_seq"] = agent["tick_seq"]
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
            except Exception as e:  # transport-level failure, logged
                rec["http_status"] = "transport_error"
                rec["response"] = f"{type(e).__name__}: {e}"[:300]
            agent["actions"] += 1  # every signed attempt counts
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
        except TokenLimitExceeded as e:
            # Rejected BEFORE sending: no call made, nothing billed.
            self.log({"event": "tick_rejected", "agent": agent["name"],
                      "reason": "per_tick_in_cap",
                      "detail": str(e)[:200]})
            return "ok"
        except BilledCapAnomaly as e:
            self.log({"event": "technical_stop",
                      "reason": "billed_token_anomaly",
                      "detail": str(e)[:200]})
            return "halt"
        except ModelAPIError as e:
            # FAIL CLOSED: billing is unknown — the request may have
            # been processed and charged before the error/timeout.
            # Retrying blindly could breach the $4 cap, so the run
            # STOPS. The unknown-spend record is preserved in
            # budget.json (see finalize_archive). This is never an
            # economic finding.
            self.uncertain_billing.append({
                "ts": time.time(), "agent": agent["name"],
                "error": f"{type(e).__name__}: {e}"[:300],
                "spend_dollars_at_halt": round(self.dollars(), 4),
                "model_calls_at_halt": self.model_calls})
            self.log({"event": "technical_stop",
                      "reason": "uncertain_model_billing",
                      "agent": agent["name"],
                      "detail": str(e)[:200]})
            return "halt"
        except Exception as e:  # non-API adapter errors: transient
            self.log({"event": "tick_error", "agent": agent["name"],
                      "error": f"{type(e).__name__}: {e}"[:200]})
            return "ok"
        self.model_calls += 1
        if self.adapter.enforces_limits:
            # Real adapter: billed usage is authoritative and caps were
            # enforced at the API boundary. Accounted at face value —
            # NEVER clipped.
            pass
        else:
            # Stub (dry-run only): synthetic estimates, clipped as
            # before, with the truncation logged.
            tin_c = min(tin, self.cfg.per_tick_in_cap)
            tout_c = min(tout, self.cfg.per_tick_out_cap)
            if tin > self.cfg.per_tick_in_cap or \
                    tout > self.cfg.per_tick_out_cap:
                self.log({"event": "tick_truncated",
                          "agent": agent["name"],
                          "in": tin, "out": tout})
            tin, tout = tin_c, tout_c
        self.spend_in += tin
        self.spend_out += tout
        call_rec = {"event": "budget_record", "call": self.model_calls,
                    "agent": agent["name"], "in_tokens": tin,
                    "out_tokens": tout,
                    "spend_dollars": round(self.dollars(), 4),
                    "dollar_cap": self.cfg.dollar_cap}
        self.budget_calls.append(call_rec)
        self.log(call_rec)
        if self.dollars() > self.cfg.dollar_cap:
            # Hard stop: a single call's billed usage pushed past the
            # cap. Halt immediately; never an economic finding.
            self.log({"event": "technical_stop",
                      "reason": "budget_cap_exceeded",
                      "spend_dollars": round(self.dollars(), 4),
                      "dollar_cap": self.cfg.dollar_cap})
            return "halt"
        if text is None or (isinstance(text, str) and not text.strip()):
            # Empty model response (e.g. reasoning exhausted the
            # output cap: content=null, finish_reason=length). The
            # billed call and its usage are already recorded above;
            # this is a survivable no_action technical event — NEVER
            # a crash. No world action is produced.
            agent["empty_streak"] = agent.get("empty_streak", 0) + 1
            streak = agent["empty_streak"]
            self.log({"event": "tick_empty_response",
                      "agent": agent["name"],
                      "action": "no_action",
                      "reason": "empty_model_content",
                      "empty_streak": streak,
                      "max_empty_streak": self.cfg.max_empty_streak,
                      "in_tokens": tin, "out_tokens": tout,
                      "spend_dollars": round(self.dollars(), 4)})
            if streak >= self.cfg.max_empty_streak:
                self.log({"event": "technical_stop",
                          "reason": "consecutive_empty_responses",
                          "agent": agent["name"],
                          "empty_streak": streak})
                return "halt"
            return "ok"
        agent["empty_streak"] = 0  # any content resets the streak
        try:
            action = json.loads(text)
            assert isinstance(action, dict) and "action" in action
        except (json.JSONDecodeError, AssertionError):
            self.log({"event": "tick_parse_failure",
                      "agent": agent["name"],
                      "raw": text[:300],
                      "in_tokens": tin, "out_tokens": tout})
            return "ok"
        rec = self.execute_action(agent, action)
        rec.update({"in_tokens": tin, "out_tokens": tout,
                    "spend_dollars": round(self.dollars(), 4),
                    "model_calls": self.model_calls})
        self.log(rec)
        return "ok"

    def run(self, max_additional_ticks: int | None = None):
        if not self._world_ready:
            self.setup_world()
        tick_budget = (self.cfg.dry_run_ticks
                       if self.cfg.dry_run else 10 ** 9)
        if max_additional_ticks is not None:
            tick_budget = self.ticks + max_additional_ticks
        reason = "loop_complete"
        try:
            while self.ticks < tick_budget:
                all_capped = True
                for agent in self.agents:
                    # Objective completion: stop spending inference the
                    # moment both stockpiles are verified complete.
                    done = self.objectives_complete()
                    if done is not None:
                        self.log_objectives_complete(done)
                        reason = "objectives_complete"
                        return
                    st = self.tick(agent)
                    if st == "halt":
                        reason = "halt"
                        return
                    if st == "ok":
                        all_capped = False
                    self.ticks += 1
                    if self.ticks >= tick_budget:
                        break
                    if not self.cfg.dry_run:
                        # ~tick_interval per agent: split across agents.
                        time.sleep(self.cfg.tick_interval_seconds
                                   / max(1, len(self.agents)))
                if all_capped:
                    reason = "all_agents_capped"
                    break
        except Exception as e:  # noqa: BLE001 - harness must record this
            # An abnormal exit (e.g. the 2026-10-07 tick-1 TypeError on
            # null model content) must NEVER be reported as
            # "loop_complete": the evidence log must say what happened.
            # Re-raised after recording so the operator still sees a
            # nonzero exit; the archive is finalized in `finally`.
            self.log({"event": "run_exception",
                      "error": f"{type(e).__name__}: {e}"[:300]})
            reason = "run_exception"
            raise
        finally:
            self.finalize_archive(reason)

    # -- resume ------------------------------------------------------------
    @classmethod
    def resume(cls, archive_dir: Path, config: RunConfig,
               adapter: ModelAdapter) -> "Phase2Runner":
        """Restore a run from its evidence archive and continue it.

        Verifies the archive first (brief SHAs must match the frozen
        briefs — any change voids the resume), restores the world DB
        copy, runner spend/calls, and agent keys/actions, then returns
        a live runner. Waits are reset (resume starts ticking)."""
        from nacl.signing import SigningKey
        archive_dir = Path(archive_dir)
        report = verify_archive(archive_dir)
        if not report["ok"]:
            raise RuntimeError(
                "archive verification failed; refusing resume: "
                + "; ".join(report["errors"]))
        manifest = json.loads(
            (archive_dir / "manifest.json").read_text())
        # Frozen briefs must be unchanged.
        for name, sha in manifest["brief_sha256"].items():
            brief_file = REPO / "phase2" / f"brief_{name}.md".replace(
                "-", "_")
            actual = sha256_file(brief_file)
            if actual != sha:
                raise RuntimeError(
                    f"brief for {name} changed since the archived run "
                    f"({actual[:12]} != {sha[:12]}); resume voided")
        tmp = tempfile.NamedTemporaryFile(suffix=".db", delete=False)
        tmp.close()
        shutil.copy2(archive_dir / "world_end.db", tmp.name)
        runner = cls(config, adapter, archive_dir, resume=True)
        runner.db_path = tmp.name
        import test_econ_validation_phase1 as T
        os.environ["AC_OPERATOR_PUBKEY"] = T.pubkey_hex(T.make_key())
        runner._attach_client()
        state = json.loads(
            (archive_dir / "runner_state.json").read_text())
        keys = json.loads((archive_dir / "agent_keys.json").read_text())
        briefs = {
            "exp-01": (REPO / "phase2" / "brief_exp_01.md").read_text(),
            "exp-02": (REPO / "phase2" / "brief_exp_02.md").read_text(),
        }
        runner.ticks = state["ticks"]
        runner.spend_in = state["spend_in"]
        runner.spend_out = state["spend_out"]
        runner.model_calls = state["model_calls"]
        # Rebuild the per-call budget ledger and the fail-closed
        # billing records from the archived artifacts so the final
        # budget.json reconciles with restored totals and no
        # unknown-spend record is lost across a resume.
        for line in (archive_dir / "run.jsonl").read_text().splitlines():
            line = line.strip()
            if not line:
                continue
            try:
                ev = json.loads(line)
            except json.JSONDecodeError:
                continue
            if ev.get("event") == "budget_record":
                runner.budget_calls.append(ev)
        try:
            _bj = json.loads(
                (archive_dir / "budget.json").read_text())
            runner.uncertain_billing = list(
                _bj.get("uncertain_billing", {}).get("events", []))
        except (OSError, json.JSONDecodeError):
            runner.uncertain_billing = []
        runner._world_ready = True
        for astate in state["agents"]:
            name = astate["name"]
            runner.agents.append({
                "name": name,
                "key": SigningKey(bytes.fromhex(
                    keys[name]["privkey_hex"])),
                "pk": keys[name]["pubkey"],
                "brief": briefs[name],
                "furnace_xy": (0, 0), "farm_id": None,
                "actions": astate["actions"],
                "tick_seq": astate["tick_seq"],
                "wait_until": 0, "next_wake": 0, "empty_streak": 0,
            })
        # Re-resolve farm ids from the restored world (public census).
        structs = runner.client.get("/world/structures").json()
        for a in runner.agents:
            farms = [s for s in structs
                     if s.get("kind") == "farm"
                     and s.get("owner_pubkey") == a["pk"]]
            a["farm_id"] = farms[0]["id"] if farms else None
        runner.log({"event": "run_resumed",
                    "harness": HARNESS_VERSION,
                    "model_adapter": adapter.name,
                    "restored_ticks": runner.ticks,
                    "restored_spend_dollars": round(runner.dollars(), 4),
                    "restored_actions": {a["name"]: a["actions"]
                                         for a in runner.agents}})
        return runner


# --------------------------------------------------------------------------
# Archive verification (offline)


def verify_archive(archive_dir: Path) -> dict:
    """Offline integrity check of an evidence archive. Returns
    {"ok": bool, "checks": [...], "errors": [...]}. No model, no world
    needed (except opening world_end.db read-only)."""
    archive_dir = Path(archive_dir)
    checks, errors = [], []

    def check(name: str, cond: bool, detail: str = ""):
        checks.append({"name": name, "ok": bool(cond), "detail": detail})
        if not cond:
            errors.append(f"{name}: {detail}")

    manifest_p = archive_dir / "manifest.json"
    check("manifest_present", manifest_p.exists())
    manifest = {}
    if manifest_p.exists():
        try:
            manifest = json.loads(manifest_p.read_text())
            check("manifest_parses", True)
        except json.JSONDecodeError as e:
            check("manifest_parses", False, str(e))
    for rel, sha in manifest.get("files", {}).items():
        p = archive_dir / rel
        check(f"file:{rel}", p.exists()
              and sha256_file(p) == sha,
              "missing or hash mismatch" if not (
                  p.exists() and sha256_file(p) == sha) else "")

    log_p = archive_dir / "run.jsonl"
    events = []
    bad_lines = 0
    if log_p.exists():
        for line in log_p.read_text().splitlines():
            line = line.strip()
            if line:
                try:
                    events.append(json.loads(line))
                except json.JSONDecodeError:
                    bad_lines += 1
    check("log_present", log_p.exists())
    check("log_lines_parse", log_p.exists() and bad_lines == 0,
          f"{bad_lines} unparsable lines" if bad_lines else "")
    starts = [e for e in events if e.get("event") == "run_start"]
    ends = [e for e in events if e.get("event") == "run_end"]
    check("single_run_start", len(starts) == 1, f"found {len(starts)}")
    check("run_end_present", len(ends) >= 1)
    check("log_ends_with_run_end",
          bool(events) and events[-1].get("event") == "run_end",
          events[-1].get("event") if events else "empty log")
    if starts:
        rs = starts[0]
        for f in ("harness", "model", "brief_sha256", "dollar_cap",
                  "token_caps", "action_cap_per_agent"):
            check(f"run_start_has_{f}", f in rs)
    # tick_seq contiguity per agent over signed-action records
    seqs: dict[str, list[int]] = {}
    for e in events:
        if e.get("event") == "tick" and "tick_seq" in e:
            seqs.setdefault(e["agent"], []).append(e["tick_seq"])
    for agent, s in seqs.items():
        check(f"tick_seq_contiguous:{agent}",
              s == list(range(1, len(s) + 1)), f"got {s[:8]}...")
    # budget records reconcile with budget.json and run_end spend
    budget_p = archive_dir / "budget.json"
    if budget_p.exists():
        b = json.loads(budget_p.read_text())
        calls = b.get("calls", [])
        tin = sum(c["in_tokens"] for c in calls)
        tout = sum(c["out_tokens"] for c in calls)
        tot = b.get("totals", {})
        check("budget_sums_match", tin == tot.get("in_tokens")
              and tout == tot.get("out_tokens")
              and len(calls) == tot.get("model_calls"),
              f"calls={len(calls)} in={tin} out={tout}")
        expect_d = round(tin / 1e6 * RATE_IN_PER_M
                         + tout / 1e6 * RATE_OUT_PER_M, 4)
        check("budget_dollars_recompute",
              abs(expect_d - tot.get("spend_dollars", -1)) < 1e-9,
              f"recomputed {expect_d}")
        ub = b.get("uncertain_billing")
        check("budget_has_uncertain_billing_block",
              isinstance(ub, dict) and isinstance(ub.get("events"), list)
              and ub.get("count") == len(ub["events"]),
              "missing or malformed uncertain_billing block")
    else:
        check("budget_json_present", False)
    for which in ("start", "end"):
        sp = archive_dir / "snapshots" / f"{which}.json"
        check(f"snapshot_{which}_present", sp.exists())
        if sp.exists():
            try:
                s = json.loads(sp.read_text())
                check(f"snapshot_{which}_agents",
                      len(s.get("agents", [])) == 2)
            except json.JSONDecodeError as e:
                check(f"snapshot_{which}_parses", False, str(e))
    check("ledger_json_present",
          (archive_dir / "ledger.json").exists())
    # world_end.db opens and matches the end snapshot inventories
    db_p = archive_dir / "world_end.db"
    end_p = archive_dir / "snapshots" / "end.json"
    if db_p.exists() and end_p.exists():
        import sqlite3
        try:
            snap = json.loads(end_p.read_text())
            conn = sqlite3.connect(f"file:{db_p}?mode=ro", uri=True)
            conn.row_factory = sqlite3.Row
            try:
                ok = True
                for a in snap.get("agents", []):
                    rows = conn.execute(
                        "SELECT resource, qty FROM inventories "
                        "WHERE agent_pubkey = ?", (a["pubkey"],)).fetchall()
                    inv = {r["resource"]: r["qty"] for r in rows}
                    if inv != a["inventory"]:
                        ok = False
                check("world_end_db_matches_snapshot", ok)
            finally:
                conn.close()
        except Exception as e:
            check("world_end_db_readable", False, str(e)[:200])
    else:
        check("world_end_db_present", db_p.exists())
    return {"ok": not errors, "checks": checks, "errors": errors}


# --------------------------------------------------------------------------
# CLI


def main(argv: list[str] | None = None) -> int:
    ap = argparse.ArgumentParser(
        description="Phase 2 blind-discovery experiment runner")
    ap.add_argument("--dry-run", action="store_true",
                    help="scripted stub adapter, zero real inference")
    ap.add_argument("--ticks", type=int, default=4,
                    help="dry-run tick budget (ignored for real runs)")
    ap.add_argument("--max-actions", type=int, default=150)
    ap.add_argument("--dollar-cap", type=float, default=4.50)
    ap.add_argument("--archive", type=str, default=None,
                    help="evidence archive directory "
                         "(default: fresh temp dir)")
    ap.add_argument("--log", type=str, default=None,
                    help="legacy: JSONL log path; the archive is written "
                         "next to it")
    ap.add_argument("--resume", type=str, default=None,
                    help="resume a run from an evidence archive directory")
    ap.add_argument("--verify-archive", type=str, default=None,
                    help="offline integrity check of an archive; then exit")
    args = ap.parse_args(argv)

    if args.verify_archive:
        report = verify_archive(Path(args.verify_archive))
        print(json.dumps(report, indent=1))
        return 0 if report["ok"] else 1

    cfg = RunConfig(dry_run=args.dry_run, dry_run_ticks=args.ticks,
                    max_actions_per_agent=args.max_actions,
                    dollar_cap=args.dollar_cap,
                    tick_interval_seconds=0 if args.dry_run else 120)
    try:
        adapter = build_adapter(args.dry_run, cfg)
    except AdapterConfigError as e:
        print(f"[phase2-runner] FATAL: {e}", file=sys.stderr)
        print("[phase2-runner] refusing to start: a non-dry-run "
              "invocation requires the pinned-model API adapter. The "
              "scripted stub is dry-run-only and will NOT be used as a "
              "silent fallback.", file=sys.stderr)
        return 2

    if args.log and args.archive:
        print("[phase2-runner] --log and --archive are mutually exclusive",
              file=sys.stderr)
        return 2
    if args.log:
        log_path = Path(args.log)
        archive_dir = log_path.parent
        log_name = log_path.name
    else:
        archive_dir = (Path(args.archive) if args.archive
                       else Path(tempfile.mkdtemp(prefix="phase2-")))
        log_name = "run.jsonl"

    if args.resume:
        runner = Phase2Runner.resume(Path(args.resume), cfg, adapter)
        remaining = (cfg.dry_run_ticks - runner.ticks
                     if cfg.dry_run else None)
        print(f"[phase2-runner] resumed archive -> {runner.archive_dir} "
              f"(ticks done: {runner.ticks})", flush=True)
        runner.run(max_additional_ticks=remaining)
    else:
        runner = Phase2Runner(cfg, adapter, archive_dir, log_name=log_name)
        print(f"[phase2-runner] archive -> {runner.archive_dir} "
              f"adapter={adapter.name}", flush=True)
        runner.run()
    print(f"[phase2-runner] done. spend=${runner.dollars():.4f} "
          f"calls={runner.model_calls} archive={runner.archive_dir}",
          flush=True)
    return 0


if __name__ == "__main__":
    sys.exit(main())
