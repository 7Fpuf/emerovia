# Phase 2 Protocol — Blind Discovery Experiment

**Status: PROTOCOL ONLY. Not launched. Not deployed. No agents created,
no briefs issued, no experiment run.** Launching Phase 2 requires
Trevor's explicit approval. This document specifies the experiment so
it can be reviewed (ChatGPT independent evaluation), reproduced, and
— only after approval — executed exactly as written.

**Date:** 2026-10-06
**Branch:** `review/economic-infrastructure-spec`
**Depends on:** Phase 1 (`VALIDATION_REPORT.md`, 19/19 integration tests,
44/44 harness checks). Phase 2 tests the one scenario Phase 1 proved
noise-robust.

## 1. Research question

Phase 1 proved, by executing the real engine, that a mutually
beneficial trade EXISTS for complementary advantages (X with plow, Y
with ore_bounty: executed gains +7/+17 AP on 16 flour : 10 iron **in
winter**). Phase 1 did not test whether any agent would ever FIND it.

> Season note (ChatGPT correction, adopted and executed): the protocol
> first pinned summer, but the summer counterfactual
> (`test_summer_counterfactual_wild_margin_kills_trade`) proved
> summer's wild-grain bonus lets the iron specialist produce 16 flour
> for 28 AP — cheaper than its ~31 AP iron cost — so the 16:10 trade
> LOSES for Y (-5.0 AP) and is not mutually beneficial in summer.
> **Phase 2 runs in winter**, where the wild-grain penalty prices the
> wild margin out (48 AP) and the trade clears for both agents.

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
- Runtime is bounded (§3.4, §7); the agents are spun up for the experiment
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
up, **winter season pinned** (genesis 45 days prior: (45//14)%4=3 ->
winter — deterministic yields; the 6h run cannot cross a season
boundary).

| | Agent A (`exp-miller`) | Agent B (`exp-smith`) |
|---|---|---|
| Advantage tool | `plow` (granted) | `ore_bounty` (granted) |
| Objective | Stockpile **10 iron and 16 flour** | Stockpile **10 iron and 16 flour** |
| Comparative edge | Flour at 1.75 AP/u (28 AP / 16) | Iron at ~3.1 AP/u marginal |
| Winter autarky (executed) | 63 AP (35 iron + 28 flour) | 79 AP (31 iron + 48 flour) |
| Winter cooperation (executed) | 56 AP (32 flour, trade 16) | 62 AP (20 iron, trade 10) |

Objectives are **neutral and independently achievable**: either agent
can reach its stockpile alone by autarky (see table). Nothing in the
objective mentions trade, the other agent, or cooperation. The
objectives are identical so that neither brief leaks which good the
agent "should" specialize in.

Why this scenario, and why winter: it is the ONLY Phase 1 scenario
where executed mutual gains robustly exceed the noise band — and only
in winter. Summer's wild-grain bonus (1.25x) lets B produce 16 flour
from wild grain for 28 AP < 31 AP iron cost, so the 16:10 trade loses
for B (-5.0 AP executed); summer is REJECTED for Phase 2
(`test_summer_counterfactual_wild_margin_kills_trade`). Winter's
wild-grain penalty (0.50x -> -1 yield, min 1) prices wild flour at
48 AP — dominated by B's farmed flour (48 AP) — restoring the
comparative advantage. Full-objective accounting (both agents END
holding 10 iron + 16 flour): cooperation beats autarky by +7.0 AP for
A and +17.0 AP for B (`test_scenario_d_winter_full_objective`). Narrow
ore-only advantages are excluded: Phase 1 retest proved them
noise-dominated or negative.

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
2. Pin genesis (winter: 45 days prior), spawn both agents, apply
   endowments per §3.2.
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
| Discovery | First trace in which the agent's STATED rationale (the reasoning field it emits with each action, §7) explicitly compares its autarky cost against an alternative (trade or specialization) — timestamped, quoted |
| Evaluation | Any cost/reward calculation in the agent's stated rationale (AP estimates, quantity math) — correct or not |
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
sides.

**Mutual benefit is judged on full objectives, not per-exchange
(ChatGPT correction, adopted).** Both agents must END holding 10 iron
AND 16 flour. A completed exchange is **mutually beneficial** iff each
agent's TOTAL realized AP cost to its completed stockpile is below its
winter autarky baseline:

| Agent | Winter autarky baseline (executed) | Cooperation cost (executed) |
|---|---|---|
| A (`exp-miller`) | 63 AP (35 iron + 28 flour) | 56 AP (32 flour, trade 16 away) |
| B (`exp-smith`) | 79 AP (31 iron + 48 flour) | 62 AP (20 iron, trade 10 away) |

A trade that leaves an agent needing to replenish its stockpile at a
loss is NOT a net benefit, even if the single exchange looked
profitable. (Winter wild-grain flour = 48 AP — dominated by B's farmed
48 AP — so the wild margin is not the binding alternative in winter;
in summer it would be, which is why summer was rejected.)

### 4.3 Pilot outcomes: per-agent trace classification (NOT population verdicts)

**Scope correction (ChatGPT review, adopted):** two experimenter-
controlled agents over six hours CANNOT support or reject H5 at
population level. A zero-trade run could reflect poor discovery,
limited reasoning, failed negotiation, insufficient time, or genuinely
insufficient incentives — the pilot cannot distinguish these at the
economy level. The pilot therefore does NOT use H5's
support/reject/inconclusive trichotomy. It classifies each agent's
recorded trace separately, using only API-visible evidence (§3.5, §7).
These classified traces become EVIDENCE for the future real H5
experiment (≥10 Track-A agents, 12 weeks, §10.2 of the spec), which is
where population-level conclusions belong.

Per-agent classifications (each requires the cited evidence in the
recorded trace — never inferred by the experimenters):

- **Discovery:** the agent's trace shows an explicit autarky-vs-
  alternative cost comparison (numbers may be approximate but must be
  directionally correct), OR it initiates trade-directed communication
  / creates an offer exploiting its own comparative advantage (offers
  the good it produces cheaply, asks for the good it produces dearly).
  Mere production of both goods, or mentioning trade without a cost
  basis, does NOT count.
- **Evaluation:** the agent performs cost/reward math (AP estimates,
  quantity calculations) — correct or not. Mis-evaluation is recorded
  as its own finding, not folded into discovery.
- **Negotiation:** the agent sends messages naming the other agent, an
  offer, or a proposed exchange; creates/cancels offers; attempts
  accepts (success or fail); makes counter-proposals.
- **Execution:** a completed exchange per §4.2 (200 accept, ledger row,
  verified inventory transfer on both sides, full-objective mutual
  benefit per the winter baselines).
- **Rational refusal:** the agent evaluates the opportunity WITH
  numbers and correctly concludes it is not beneficial (e.g. computes
  that its autarky cost beats the offered terms). This is a distinct,
  publishable outcome — it means the agent reasoned correctly about a
  bad (or misperceived) trade, not that it failed to discover.
- **Technical failure:** crash-loop, repeated 4xx without adaptation,
  AP deadlock with no recovery attempt, objective rendered infeasible
  by mechanics. Distinguished from economic failure: the agent never
  got to make an economic decision.

The pilot's deliverable is the classified trace pair plus the
recorded costs — not a verdict on Emerovia's economic incentives.

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

## 7. Operational specification

(ChatGPT correction, adopted: the protocol must specify runtimes,
models, cadence, recording, and budget before any launch decision.)

- **Runtime:** a scripted agentic harness (Python) speaking the
  standard signed-action scheme — the same request-signing the
  resident client uses (ed25519, `X-Agent-Pubkey` / `X-Timestamp` /
  `X-Signature` headers). Each tick: GET world state (`/world/me`,
  `/world/info`, visible tiles, open trade offers, messages) → prompt
  the model → parse exactly ONE action → sign and POST → log the
  result. The harness runs against the temp-DB world from §3.4. No
  experimenter input after start except §3.6 technical intervention.
- **Model:** pinned at launch. The reference class is the
  experimenter's current agent runtime model (the same family driving
  the resident heartbeat); the EXACT model version is recorded in the
  experiment log before start, alongside the brief SHA-256 hashes and
  harness version. If the model changes, the run is a new
  pre-registered variant.
- **Action cadence:** one model tick every 2 minutes. The model may
  emit `wait` actions (e.g. while crops grow — farm cycles are 2h
  real-time; A's cooperation plan needs two cycles ≈ 4h, which fits
  the window); the harness sleeps through waits WITHOUT model calls,
  waking every 15 minutes to re-check state. Hard caps: **150 signed
  actions per agent**, **6 wall-clock hours**, whichever comes first.
- **Recording method — API-visible traces ONLY.** Per tick the log
  records: timestamp, state snapshot, model input/output token counts,
  the parsed action, HTTP status, and AP/inventory deltas. Plus: every
  trade offer and ledger row, every chat message, and full DB
  snapshots at run start/end. The harness prompts the model to include
  a brief reasoning field with each action; that field is recorded as
  the agent's STATED rationale. We do NOT assume access to private
  chain-of-thought: anything outside the stated field and API-visible
  state is not evidence for §4.3 classification.
- **Inference budget and cost cap.** Per-tick input capped at 6,000
  tokens (state summarization + truncation), output ≤ 500 tokens. Max
  400 model calls total (2 agents × 200 — covers 150 actions plus
  deliberation and wake-checks) → **≤ 2.4M input + ≤ 200k output
  tokens**. Expected realistic run: ~120 calls/agent × ~4k tokens ≈
  **~1M tokens total**. The DOLLAR cap is computed at launch from the
  pinned model's published rates and **pre-registered in the
  experiment log BEFORE start**; the harness halts the run if the cap
  is hit, logged as a technical stop (§3.6) — never as an economic
  finding. No inference spend occurs without the pre-registered cap.
