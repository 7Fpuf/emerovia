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
| New: `tests/test_econ_validation_phase1.py` | 16 | **16 pass** |
| Existing suite (regression) | 458 | **458 pass** |
| Cost-model harness `tools/validate-cost-model.py` | 41 checks | **all pass** |

Every number below was MEASURED by running the real engine, not derived
from constants.

## The four scenarios

**(a) Identical agents — zero-sum, confirmed.** X gives 12 flour (28 AP)
for Y's 10 iron (37 AP autarky): X +9, Y −9, sum = 0. Trade executes
correctly through `/trade/offers` → `/trade/offers/{id}/accept` with
ledger row; no cooperation gain exists, as predicted.

**(b) Farming advantage (X has plow) — mutual gains, confirmed.**
X makes 16 flour for 28 AP (vs 37.0 iron autarky → +9.0). Y makes 10
iron for 37 AP (vs 48.0 flour autarky → +11.0). Both gain; trade
settles.

**(c) Ore advantage only (Y has ore_bounty) — MODEL FAILURE.**
The continuous model predicts a rational window F ∈ (13.0, 15.2). **No
cycle-exact flour quantity exists in this window** (farm cycles yield
exactly 12 or 16). The window is infeasible under discrete mechanics.
Falling back to wild grain (2.0 AP/u, depleting): measured X +3.0,
Y −1.0. Y's predicted +1.0 gain is smaller than gather-lumpiness noise
(±2 AP) — **the narrow ore advantage is noise-dominated**. The model
cannot reliably predict the sign of Y's gain.

**(d) Complementary advantages (X plow, Y ore_bounty) — mutual gains,
confirmed.** X: 16 flour for 28 AP (+9.0 vs 37.0 autarky). Y: 10 iron
for 31 AP (+17.0 vs 48.0 autarky). Executed gains EXCEED the model's
continuous predictions (7.4/6.9) because farm lumpiness raises both
autarky baselines. Direction confirmed; magnitudes are lumpy.

## Biggest model-vs-reality discrepancies

1. **Farm-cycle lumpiness (systematic).** The v2.3 model uses continuous
   per-unit costs (1.75/2.33 AP/u). Real farm cycles are quanta: 12
   grain/cycle (no plow), 16/cycle (plow). Producing 16 flour without a
   plow costs 3.0 AP/u (2 cycles, 8 grain surplus), not 2.33. The
   model's rational windows can be empty of feasible quantities.

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

7. **Travel is 1 AP/tile (land), 2 AP (mountain).** A 10-tile delivery
   costs 10–20 AP — enough to erase narrow gains. Cooperation requires
   gain > 2 × distance × move_cost.

8. **Winter doubles wild grain cost** (4.0 vs 2.0 AP/u); farmed grain
   is season-independent. Confirmed temporal advantage for farmers.

## Harness extensions

Six new checks in `tools/validate-cost-model.py` pin the discrete
constraints: farm cycle quanta (12/16), window feasibility (16 in
(13.0, 20.2); none in (13.0, 15.2)), lumpiness noise band (2 AP), and
F=12 non-mutuality in the ore-only case. All 41 checks green.

## Failure taxonomy (where agents would fail)

- **Discovery:** Agents must find cycle-exact quantities; the model's
  windows don't tell them which F values are feasible.
- **Evaluation:** Narrow gains (< 2 AP) are indistinguishable from
  noise; agents need confidence intervals, not point estimates.
- **Trust:** No-escrow offers mean the maker's inventory can change
  between offer and accept; accept re-verifies, but agents must handle
  acceptance failures.
- **Coordination:** Distance costs and AP constraints require planning;
  a trade that's profitable on paper can fail if the agents can't meet
  or can't afford the AP.

## Unresolved questions

- Wild resource regrowth rate (1/7 days) vs depletion: long-run
  sustainability of the wild-flour margin untested.
- Multi-hop trades and chit-denominated pricing not exercised.
- Agent behavioral question: will real agents discover the
  cycle-exact quantities, or satisfice on lumpy production?

## Phase 2 recommendation

Run the first genuine independent-agent experiment with **complementary
advantages only** (plow vs ore_bounty): it is the one scenario where
executed mutual gains (+9/+17 AP) robustly exceed lumpiness noise, so
signal will survive agent suboptimality. Endow two independent agents
with the respective tools, publish the cycle-exact feasible quantities
(12/16 flour) in their briefs to solve discovery, and measure whether
they find the trade without prompting — the discovery gap, not the
gains, is the real unknown. Do NOT test narrow ore-only advantages in
Phase 2: scenario C proved the gains are noise-dominated and would
yield uninterpretable results.
