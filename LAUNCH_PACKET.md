# Phase 2 Launch Packet — blind discovery pilot (PREFLIGHT, NOT LAUNCHED)

**Status: preflight complete. Awaiting ChatGPT review round + Trevor's
explicit launch approval (protocol §6). No experiment has been
launched. There is no standing authorization to run.**

This packet is the single finishing line ChatGPT asked for: corrected
economics, runner revision, frozen briefs, pinned model, budget,
duration, stop conditions, and the authorization gate. Nothing in it
authorizes a launch.

---

## 1. Corrected economics (all numbers EXECUTED on the real engine)

Winter scenario (genesis pinned 45d → `(45//14)%4=3` → winter;
deterministic yields; the 6h run cannot cross a season boundary).
Both agents must END holding **10 iron + 16 flour** (full-objective
accounting — a trade that leaves an agent replenishing at a loss is
not a benefit).

| | Agent A (`exp-01`, plow) | Agent B (`exp-02`, ore_bounty) |
|---|---|---|
| Winter autarky (executed) | 63 AP (35 iron + 28 flour) | 71 AP (31 iron + 40 flour) |
| Winter cooperation (executed) | 56 AP (32 flour, trade 16 away) | 62 AP (20 iron, trade 10 away) |
| **Full-objective gain** | **+7.0 AP** | **+9.0 AP** |

Baselines are each agent's **cheapest feasible alternative**
(four-gate review, Item 1, adopted):

- B's 16-flour: 6 individual farm slots → 18 grain → 8 refines =
  **40.0 AP** (2 grain retained; `test_winter_cheapest_flour_autarky_b`).
  A 5-slot + 1 winter wild-gather variant costs 38 AP + real round-trip
  travel (44.0 AP executed) — it only beats 40 AP if wild grain is
  within 1 tile, inside the ±4 band. The 48 AP full-cycle figure was
  the same artificial restriction corrected in summer: withdrawn.
- A's 16-flour (28 AP) is already slot-optimal (4 slots = exactly 16
  grain, one cycle); A's iron has no alternative method.

**Uncertainty: ±4 AP** (worldgen tile-stock variance; 10 measured runs:
A +7.0 every run, B +7 to +11). Both gains exceed the band. The
advantage is real but thinner than the earlier +17 claim — exactly
ChatGPT's correction, adopted.

Why winter, not summer: summer's wild-grain bonus (1.25×) makes B's
cheapest flour 28 AP against ~31 AP iron — typical gain −3 AP, inside
the band: no robust mutual-gain claim survives summer
(`test_summer_counterfactual_wild_margin_kills_trade`). Winter prices
the wild margin out (48 AP).

## 2. Runner revision + dry-run results

- **Runner:** `tools/phase2_runner.py`,
  `HARNESS_VERSION = phase2-runner/0.1.0-preflight`.
- **Loop:** GET state → prompt pinned model → parse exactly ONE action
  → ed25519-sign + POST → JSONL log (timestamp, state, token counts,
  action, HTTP status, AP/inventory deltas, stated reasoning). Temp-DB
  world only; no experimenter input after start except §3.6 technical
  intervention.
- **Wait semantics:** `wait` sets a wake timestamp — zero model calls
  until then, 15-minute wake-checks (state re-read, logged, no
  inference). Farm cycles are 2h real-time; the 6h window fits the
  agents' production plans.
- **Dry-run verified** (scripted stub adapter, zero real inference):
  1. Full tick pipeline with 201/200/400 paths recorded;
  2. `technical_stop: budget_cap_reached` with 0 model calls at a
     $0.000001 cap;
  3. `agent_done: action_cap_reached` at exactly 2/2 with
     `--max-actions 2`;
  4. `run_start` commits harness version, model, brief SHAs, dollar
     cap, token caps before any tick.
- **Remaining before a real run:** swap the stub adapter for the
  pinned-model API adapter; recompute the dollar cap from
  then-current published rates.

## 3. Frozen briefs (SHA-256, blind)

- `phase2/brief_exp_01.md`
  `ddafa4ab3d1f8219487d07c870a93077b5721b2260b14de3cf153d8607e49843`
- `phase2/brief_exp_02.md`
  `d0bf6dc30d317939cb943a71d12fef0ca30e2f4e725882dbedd5bc1267bdd2e1`

Blindness: neutral public identifiers `exp-01`/`exp-02` (the
role-leaking `exp-miller`/`exp-smith` names are removed everywhere
public). Briefs contain ordinary mechanics + objective + own
endowments + operating constraints; they EXCLUDE cooperation, trade
strategy, quantities/ratios, the other agent, and specialization
hints. Mutual discoverability via ordinary channels (agent list →
chat → trade offers) with zero researcher hints is mechanically
verified (`test_phase2_mutual_discovery_no_hints`). Any brief change
voids the run.

## 4. Pinned model

**`muse-spark-1.3`** (standard tier, Meta Model API). Published rates
at preflight (2026-10-06): **$1.25 / 1M input, $4.25 / 1M output**.
The contributor tier ($0.10/$0.20) is explicitly NOT used — it trades
experiment data for the discount. Any model change = new
pre-registered variant.

## 5. Budget, duration, stop conditions

- **Token caps (binding):** ≤6,000 in / ≤500 out per tick; ≤400 model
  calls total → ≤2.4M in + ≤200k out.
- **Dollar cap (pre-registered): $4.00** — covers the $3.85 maximum
  theoretical spend at pinned rates (2.4×$1.25 + 0.2×$4.25).
  Expected realistic: ~1M tokens ≈ **~$1.85**. Recomputed at launch
  from then-current rates; token caps bind regardless.
- **Duration:** 6 wall-clock hours max; 150 signed actions/agent max;
  tick cadence ~2 min/agent.
- **Stop conditions:** budget cap (worst-case next-call cost checked
  BEFORE every call), action cap, wall-clock cap — all logged as
  technical stops, never as economic findings. No spend without the
  pre-registered cap.

## 6. What the pilot can and cannot conclude

Per-agent trace classification only (discovery / evaluation /
negotiation / execution / rational refusal / technical failure),
each with cited API-visible evidence — never inferred. A completed
exchange counts iff each agent's TOTAL realized AP to its completed
stockpile beats its winter autarky baseline (63 / 71). **Population
H5 verdicts are OUT of scope**: two agents over six hours cannot
support or reject them; classified traces become evidence for the
future real experiment. Agents may choose self-sufficiency or
discover unanticipated trades — legitimate outcomes, not failures.

## 7. Authorization gate

1. ✅ Phase 1 corrections (three rounds) — executed, 479/479 tests,
   44/44 harness checks.
2. ✅ Final preflight (this packet) — runner built, dry-run verified.
3. ⬜ ChatGPT independent review of this packet.
4. ⬜ **Trevor's explicit approval** — the single decision: run or don't.
5. ⬜ Only then: execute exactly per protocol, publish run log +
   outcome.

**No step 5 without step 4. Steps 3–4 are not done. The pilot is ON
HOLD.**
