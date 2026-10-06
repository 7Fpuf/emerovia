# Economic Validation Report — Phase 1

**Date:** 2026-10-06
**Branch:** `review/economic-infrastructure-spec`
**Scope:** Integration tests executing the REAL Emerovia world engine
(`server/world.py`, `server/app.py` at commit b16d726) against temporary
SQLite databases. No production data, no deploys, no crypto, no fake
volume.

## Results summary

| Suite | Tests | Result |
|---|---|---|
| New: `tests/test_econ_validation_phase1.py` | 17 | **17 pass** |
| Existing suite (regression) | 458 | **458 pass** |
| Cost-model harness `tools/validate-cost-model.py` | 44 checks | **all pass** |

Every number below was MEASURED by running the real engine, not derived
from constants.

## Corrections applied 2026-10-06 (ChatGPT independent review)

Three test issues, all adopted and retested; one Phase 2 design
correction, adopted as protocol (see `PHASE2_PROTOCOL.md`).

1. **Per-slot harvest retest.** Phase 1's helper harvested full 4-slot
   farm cycles, artificially restricting producible quantities. Code
   verification (`server/world.py` `farm()`: per-slot `slot` 0–3 with
   independent slot state machines; `refine()`: one discrete 2:2 batch
   per call; surplus retained in inventory) confirmed ChatGPT's claim:
   per-slot plant/harvest and partial refining are real mechanics.
   Retest: 14 flour IS producible — 5 slots across 2 cycles → 15 grain
   → 7 refines → 14 flour + 1 retained grain, 34 AP executed
   (2.43 AP/u). New test `test_scenario_c_partial_harvest_14_flour`.
2. **Delivery travel removed from trade profitability.** The trade API
   settles globally (`accept()` swaps inventories in one DB transaction;
   no position check) — counterparties never travel to each other. The
   earlier "delivery erases narrow gains / gain > 2 × distance ×
   move_cost" framing was a v2.1 delivery-vs-access regression and is
   removed. Travel is acquisition/facility-access only. Retest
   `test_variant_travel_cost_bound` executes a profitable trade between
   agents tens of tiles apart and confirms gains identical to the
   co-located baseline.
3. **Negative tests assert explicit HTTP failures.** Double-accept now
   asserts 409 + `detail: "offer is filled"`; uncovered offer asserts
   400 + `detail: "maker does not hold give-items"` — no broad
   AssertionError catches.

## The four scenarios

**(a) Identical agents — zero-sum, confirmed.** X gives 12 flour (28 AP)
for Y's 10 iron (37 AP autarky): X +9, Y −9, sum = 0. Trade executes
correctly through `/trade/offers` → `/trade/offers/{id}/accept` with
ledger row; no cooperation gain exists, as predicted.

**(b) Farming advantage (X has plow) — mutual gains, confirmed.**
X makes 16 flour for 28 AP (vs 37.0 iron autarky → +9.0). Y makes 10
iron for 37 AP (vs 48.0 flour autarky → +11.0). Both gain; trade
settles.

**(c) Ore advantage only (Y has ore_bounty) — window closed by margin
competition, not lumpiness (RETESTED).** The continuous model predicts
a rational window F ∈ (13.0, 15.2). ChatGPT's correction verified: 14
flour IS producible via per-slot harvests (34 AP executed). But
producible ≠ mutually beneficial. At 14 flour : 10 iron, X gains
37 − 34 = +3.0 while Y loses: Y's cheapest flour autarky is the WILD
margin (2.0 AP/u, 28 AP for 14 flour), so Y's gain is 28 − 31 = −3.0.
The binding constraint is the wild-grain margin available to BOTH
agents, which competes away Y's bounty advantage — the continuous
model omits it. Falling back to wild grain for X: 16 flour (32 AP)
for 10 iron (31 AP) gives X +5.0, Y +1.0 — Y's gain stays inside the
±2 AP gather-lumpiness noise band, so the narrow ore advantage is
NOISE-DOMINATED and the model cannot reliably predict the sign of
Y's gain. Verdict refined, not reversed: ChatGPT was right about the
mechanic; the economics still don't clear.

**(d) Complementary advantages (X plow, Y ore_bounty) — mutual gains,
confirmed.** X: 16 flour for 28 AP (+9.0 vs 37.0 autarky). Y: 10 iron
for 31 AP (+17.0 vs 48.0 autarky). Executed gains EXCEED the model's
continuous predictions (7.4/6.9) because farm lumpiness raises both
autarky baselines. Direction confirmed; magnitudes are lumpy.

## Biggest model-vs-reality discrepancies

1. **Farm production is slot-granular, not cycle-granular
   (refined 2026-10-06).** The v2.3 model used continuous per-unit costs
   (1.75/2.33 AP/u) and Phase 1 first treated farm cycles as 12/16-grain
   quanta. Retest: slots are independent (3 grain/slot, 4 with plow),
   so flour is producible in any even quantity 2·floor(3n/2) for n
   slots — e.g. 14 flour at 2.43 AP/u. Lumpiness still bites (odd
   quantities waste grain; the per-unit rate never beats the
   continuous ideal), but the scenario-C window was closed by the
   wild-grain margin (2.0 AP/u, available to both agents), not by
   lumpiness alone.

2. **Gather lumpiness (+5%).** 10 iron costs 37 AP executed vs 35.4
   continuous — the final partial gather (min(2, stock)) adds overhead.

3. **Narrow advantages are noise-dominated.** Any predicted gain < 2 AP
   is unreliable; tile-stock randomness exceeds it. (Scenario C.)

4. **No escrow at offer creation.** `POST /trade/offers` moves nothing;
   goods remain spendable until accept. Double-accept fails (offer
   filled), uncovered offers rejected at creation.

5. **Inventory cap 99/item** (149 with cart) bounds bulk trades; the
   model assumes unbounded inventories.

6. **AP is stop-and-wait.** 5 AP allows exactly 1 iron refine (3 AP);
   regen is 1/min. Production is AP-constrained, not free-flowing.

7. **Travel is 1 AP/tile (land), 2 AP (mountain) — acquisition
   only (corrected 2026-10-06).** Travel costs apply to reaching
   resource tiles and standing on facilities. Trade settles globally
   via the API: a profitable trade executed between agents tens of
   tiles apart fills with gains identical to the co-located baseline
   (+9/+17). Distance is NOT a term in trade profitability — the
   earlier delivery-cost bound was a v2.1 regression and is removed.

8. **Winter doubles wild grain cost** (4.0 vs 2.0 AP/u); farmed grain
   is season-independent. Confirmed temporal advantage for farmers.

## Harness extensions

Seven checks in `tools/validate-cost-model.py` pin the discrete
constraints: farm cycle quanta (12/16), per-slot producibility (14 in
the ore-only window), lumpiness noise band (2 AP), F=12 non-mutuality,
and F=14 executed non-mutuality (X +3.0, Y −3.0 via the wild margin).
All 44 checks green.

## Failure taxonomy (where agents would fail)

- **Discovery:** Agents must find producible quantities and evaluate
  them against autarky; the model's windows don't tell them which F
  values are mutually beneficial (the wild margin competes).
- **Evaluation:** Narrow gains (< 2 AP) are indistinguishable from
  noise; agents need confidence intervals, not point estimates.
- **Trust:** No-escrow offers mean the maker's inventory can change
  between offer and accept; accept re-verifies, but agents must handle
  acceptance failures.
- **Coordination:** AP constraints require planning — a trade that's
  profitable on paper fails if an agent can't afford the production AP.
  (Distance is not a coordination cost: settlement is global.)

## Unresolved questions

- Wild resource regrowth rate (1/7 days) vs depletion: long-run
  sustainability of the wild-flour margin untested.
- Multi-hop trades and chit-denominated pricing not exercised.
- Agent behavioral question: will real agents discover the
  cycle-exact quantities, or satisfice on lumpy production?

## Phase 2: blind discovery protocol (NOT launched)

The earlier recommendation — publish profitable quantities in agent
briefs — was **methodologically wrong** (ChatGPT's design correction,
adopted): it would test guidance-following, not discovery. The guided
variant is deferred to a SEPARATE later experiment for comparison; it
is not built here.

Phase 2 is specified as a **blind discovery experiment** in
`PHASE2_PROTOCOL.md` (protocol only — no launch, no deploy; Trevor's
explicit approval required before any run):

- Two experimenter-controlled, clearly labeled agents with neutral,
  independently achievable economic objectives and different starting
  capabilities (plow vs ore_bounty — the complementary-advantage
  scenario, the only one with noise-robust executed gains of +9/+17).
- Briefs contain ordinary world information + existing trading tools.
  They do NOT mention cooperation, optimal quantities, or exchange
  ratios.
- Records: discovery, cost evaluation, communication, negotiation,
  completed exchanges, elapsed time, inference costs, prompt
  provenance, failed attempts.
- Pre-registered acceptance criteria: what counts as discovery, what
  counts as a completed exchange, and support/reject/inconclusive
  conditions mapped to H5's trichotomy.

Do NOT test narrow ore-only advantages in Phase 2: the retest
confirmed the gains are noise-dominated or negative and would yield
uninterpretable results.
