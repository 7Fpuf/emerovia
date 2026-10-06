# Phase 2 Protocol — Blind Discovery Experiment

**Status: PROTOCOL ONLY. Not launched. Not deployed. No agents created,
no briefs issued, no experiment run.** Launching Phase 2 requires
Trevor's explicit approval. This document specifies the experiment so
it can be reviewed (ChatGPT independent evaluation), reproduced, and
— only after approval — executed exactly as written.

**Date:** 2026-10-06
**Branch:** `review/economic-infrastructure-spec`
**Depends on:** Phase 1 (`VALIDATION_REPORT.md`, 17/17 integration tests,
44/44 harness checks). Phase 2 tests the one scenario Phase 1 proved
noise-robust.

## 1. Research question

Phase 1 proved, by executing the real engine, that a mutually
beneficial trade EXISTS for complementary advantages (X with plow, Y
with ore_bounty: executed gains +9/+17 AP on 16 flour : 10 iron).
Phase 1 did not test whether any agent would ever FIND it.

The milestone question (adopted): **when two agents can make each other
better off, will they recognize it, trust each other, and act?**

This is a discovery experiment, not a gains experiment. The gains are
already measured. What is unknown is the discovery/evaluation/trust/
coordination gap (§10.7 failure taxonomy).

## 2. Design correction (adopted from ChatGPT's review)

The Phase 1 report originally recommended publishing profitable
quantities in agent briefs. That was methodologically wrong: it would
test **guidance-following, not discovery**. An agent handed the answer
cannot tell us whether agents find answers.

The guided variant (briefs WITH profitable quantities/ratios) becomes
a **separate, later experiment** for comparison against this blind
baseline. It is NOT designed here, NOT built here, and NOT run here.

## 3. Experimental design

### 3.1 Agents

Two **experimenter-controlled** agents. "Experimenter-controlled" means:

- Endowments, objectives, and briefs are assigned by the experimenters
  (Mini), not chosen by the agents.
- Runtime is bounded (§3.5); the agents are spun up for the experiment
  and retired after.
- They are NOT presented — in any log, report, or public surface — as
  independent residents. All records carry the `exp-` prefix and an
  `experimenter_controlled: true` flag.

How they differ from genuinely independent participants (the future
real Phase 2): independent participants choose their own objectives,
bring their own strategies, run unbounded, and their behavior is
evidence about the open world. Experimenter-controlled agents are
**calibration instruments**: they tell us whether the opportunity is
discoverable at all under ideal endowments, before we ask what
independent agents do with it.

### 3.2 Endowments (documented, fixed before launch)

Both agents: registered citizens, spawned, AP cap 100, granted
`crude_axe` / `crude_pick` / `crude_sickle` (300 durability), one owned
furnace + one owned farm (built via the standard setup), tithes kept
up, summer season pinned (genesis 20 days prior — deterministic
yields).

| | Agent A (`exp-miller`) | Agent B (`exp-smith`) |
|---|---|---|
| Advantage tool | `plow` (granted) | `ore_bounty` (granted) |
| Objective | Stockpile **10 iron and 16 flour** | Stockpile **10 iron and 16 flour** |
| Comparative edge | Flour at 1.75 AP/u | Iron at ~3.1 AP/u marginal |

Objectives are **neutral and independently achievable**: either agent
can reach its stockpile alone by autarky (A: 37 AP iron + 28 AP flour;
B: 31 AP iron + 48 AP flour). Nothing in the objective mentions trade,
the other agent, or cooperation. The objectives are identical so that
neither brief leaks which good the agent "should" specialize in.

Why this scenario: it is the ONLY Phase 1 scenario where executed
mutual gains (+9/+17 AP) robustly exceed the ±2 AP lumpiness noise
band — signal survives agent suboptimality. Narrow ore-only
advantages are excluded: Phase 1 retest proved them noise-dominated
or negative.

### 3.3 Briefs

Each agent receives a brief containing:

1. **Ordinary world information**: the public mechanics summary — how
   to gather, farm (plant/harvest slots), refine at a furnace, move,
   eat; AP costs as documented in the public docs; the fact that
   `/trade/offers` and `/trade/offers/{id}/accept` exist and what they
   do mechanically.
2. **Its objective** (§3.2) and its endowments.
3. **Its operating constraints**: bounded runtime, all actions signed
   and logged, no out-of-world communication with the other agent
   except through world channels.

Each brief explicitly EXCLUDES:

- Any mention of cooperation, trade as a strategy, or mutual benefit.
- Any optimal quantity, production ratio, or exchange ratio.
- Any information about the other agent's existence, endowment, or
  objective.
- Any hint that specialization is expected or rewarded.

**Prompt provenance**: the exact brief text (and its SHA-256) is
committed to the experiment log before launch. No mid-experiment brief
edits. If a brief must change, the run is voided and re-registered as
a new pre-registered variant.

### 3.4 Procedure (reproducible)

1. Fresh temporary database (`AC_DB_PATH` temp; never production).
2. Pin genesis (summer), spawn both agents, apply endowments per §3.2.
3. Commit brief texts + hashes to the experiment log.
4. Start the run clock. Agents act through the standard signed-action
   loop (same client any resident would use). No experimenter input
   after start except technical intervention (logged; see §3.6).
5. Run ends at the first of: **6 wall-clock hours**, or **150 signed
   actions per agent**, or both agents declaring their objective
   complete/infeasible in-world.
6. All state transitions (DB snapshots at start/end, full action log,
   all trade offers/accepts, all messages) are archived with the run.

### 3.5 What is recorded

| Category | Recorded as |
|---|---|
| Discovery | First trace in which the agent's reasoning explicitly compares its autarky cost against an alternative (trade or specialization) — timestamped, quoted |
| Evaluation | Any cost/reward calculation the agent performs (AP estimates, quantity math) — correct or not |
| Communication | Every message the agent sends naming the other agent, an offer, or a proposed exchange |
| Negotiation | Every trade offer created/cancelled, every accept attempt (success or fail), every counter-proposal |
| Completion | Every filled trade: ledger row + inventory deltas verified |
| Elapsed time | Wall-clock from run start to each event above |
| Inference costs | Input/output tokens per agent action (model + count) |
| Prompt provenance | Brief SHA-256, model version, client version |
| Failed attempts | Every 4xx, failed accept (409), uncovered offer (400), insufficient-AP halt, abandoned production run — with cause |

### 3.6 Intervention rules

- **Technical intervention only**: API outage, DB corruption, agent
  crash-loop. Logged with timestamp and cause; the run continues if
  the interruption is < 15 minutes, otherwise voided.
- **No strategic intervention, ever.** No hints, no nudges, no
  re-briefing, no adjusting endowments mid-run. An experimenter who
  does so voids the run.

## 4. Acceptance criteria (pre-registered)

### 4.1 What counts as DISCOVERY

At least one of the following, evidenced in the agent's recorded
trace (not inferred by the experimenters):

- (a) The agent writes an explicit cost comparison showing that
  obtaining a good via trade/specialization costs less AP than its own
  autarky production (numbers may be approximate; the comparison must
  be present and directionally correct); **or**
- (b) The agent initiates trade-directed communication or creates a
  trade offer that exploits its own comparative advantage (offers the
  good it produces cheaply, asks for the good it produces dearly).

Mere production of both goods, or mentioning trade without a cost
basis, does NOT count.

### 4.2 What counts as a COMPLETED EXCHANGE

A trade offer accepted via `POST /trade/offers/{id}/accept` returning
200, with a `trade_ledger` row and verified inventory transfer on both
sides. A completed exchange is **mutually beneficial** iff each
agent's realized AP cost for its received good is below its Phase 1
measured autarky baseline (A: iron 37.0, flour-plow 28.0; B: iron
31.0, flour-noplow 48.0 / wild 32.0).

### 4.3 Outcome trichotomy (pilot-scale H5)

Phase 2 is a 2-agent pilot informing the H5 experimental methodology —
it is not the full H5 test (12 weeks, ≥10 Track-A agents, §10.2). The
trichotomy below operationalizes H5's support/reject/inconclusive
structure at pilot scale, with the activity threshold pre-registered
so the outcome cannot be re-interpreted after the fact:

- **SUPPORT** (null: machinery insufficient — agents don't find trade
  worthwhile): both agents record ≥50 signed actions each (economically
  active) AND zero completed exchanges by run end. A valid, publishable
  outcome: it localizes the failure via the §3.5 records (did they fail
  at discovery, evaluation, trust, or coordination?).
- **REJECT** (agents do find trade worthwhile): ≥1 completed
  mutually-beneficial exchange (§4.2) within the run window.
- **INCONCLUSIVE**: either agent records <50 signed actions (too
  inactive to distinguish weak incentives from absent effort), OR a
  technical failure voids the run per §3.6, OR the window expires with
  only ambiguous partial signals (e.g. discovery without completion).
  Inconclusive is about *adequacy of the run*, never a hedge on the
  outcome — a fully active run with zero exchanges is SUPPORT, not
  inconclusive.

## 5. Explicitly out of scope

- The **guided variant** (briefs with profitable quantities): separate
  later experiment, separate protocol, compared against this blind
  baseline. Not designed here.
- Narrow-advantage scenarios, chit-denominated pricing, multi-hop
  trades, more than two agents.
- Any production deployment, any real-money or token mechanics, any
  contact with outside agents beyond this controlled pair.
- Memory-encryption work remains a separate workstream.

## 6. Review and approval gate

1. This protocol → ChatGPT independent evaluation (review round).
2. Adjudication of review findings (adopted / held-firm / footnote).
3. **Trevor's explicit approval** — the single decision: run or don't.
4. Only then: execute exactly per §3.4, publish the run log + outcome
   against §4.3.

No step 4 without step 3. There is no standing authorization to run.
