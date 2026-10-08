# Phase 2 variant: social-visibility-v1 (harness phase2-runner/0.3.0)

Registered: 2026-10-08. Status: **built and tested offline; paid launch
NOT authorized** — launch requires Trevor's separate explicit approval
after ChatGPT's pre-launch review.

## What this variant is

One observation-interface change to the Phase 2 blind-discovery runner,
approved for offline development by Trevor on 2026-10-08 after ChatGPT's
independent review of the run-4 evidence audit. The run-4 audit confirmed
that `GET /world/agents` returns public `agent_name, x, y, terrain` for
every agent, but the runner stripped everything except the name before
building prompts. Agents knew another citizen existed but had no
location, direction, or distance — contact-seeking was not a formulable
plan. This variant passes the already-public fields through.

The exact research question: **when agents can see each other's live
positions, do they notice, approach, or contact each other — or continue
to ignore each other?** Both outcomes are informative. The experiment is
designed to separate "never had the information" from "had the
information and did not act," not to produce trade.

## Exact observation-interface diff vs 0.2.0 / obs-interface-v1

`tools/phase2_runner.py`, `build_prompt`, the `[agents_visible]` section:

```diff
             ("agents_visible", json.dumps(
-                [a.get("agent_name") for a in
-                 (others if isinstance(others, list) else [])])),
+                [{"name": a.get("agent_name"), "x": a.get("x"),
+                  "y": a.get("y"), "terrain": a.get("terrain")}
+                 for a in (others if isinstance(others, list) else [])])),
```

Plus the version registration (`HARNESS_VERSION =
"phase2-runner/0.3.0"`, `PROMPT_VARIANT = "social-visibility-v1"`) and
this document. Nothing else in the prompt assembly changed: same
section headings, same order, same briefs, same state-budget fitting.

Example rendered section:

```
[agents_visible]
[{"name": "exp-01", "x": 7, "y": 0, "terrain": "plains"}, {"name": "exp-02", "x": 22, "y": 7, "terrain": "forest"}]
```

**No instruction was added anywhere.** No prompt or brief tells agents
to communicate, cooperate, approach, trade, or seek each other out. The
briefs already describe the public agent list ("shows who else is in the
world"); the prompt now actually delivers what the brief describes.
This is observation only, consistent with the runner's standing rule:
report what happened, never what to do.

## Spawn and starting-condition reproducibility

How experiment worlds are built (`Phase2Runner.setup_world`):

- **World terrain is deterministic.** The 64×64 map is generated from
  the fixed genesis seed `agent-commons-genesis-v1`
  (`server/world.py: SEED_ID`, `generate_world_terrain`). Every run
  gets the same map.
- **Season is pinned.** Genesis is set 45 days in the past →
  `(45 // 14) % 4 = 3` → winter, every run.
- **Spawn positions are NOT reproducible.** `spawn()` in
  `server/world.py` picks the tile with
  `random.SystemRandom().choice(pool)` — OS entropy, unseeded. The
  first agent lands on a random free land tile; the second lands on a
  random free land tile within Chebyshev radius 20 of the first
  (`SPAWN_NEAR_RADIUS = 20`, the Bible §11 anti-isolation rule) when
  such land exists, else anywhere.
- Endowments, structures (furnace + farm on claimed adjacent tiles),
  and inventories are identical by construction.

**What "preserved as closely as reproducibility allows" means:** same
seed, same season, same spawn protocol, same briefs, same endowments,
same safeguards. It does NOT mean identical starting coordinates —
those are drawn fresh each run by design. Do not hand-place spawns to
match run-4: that would change the protocol and introduce
experimenter bias. The run archive records actual starting positions
(run-4: exp-01 (7,0), exp-02 (22,7), Chebyshev 15); cross-variant
comparisons must control for separation distance, not exact tiles.

## Predefined observation milestones

Distinct, evidence-based categories. Evidence sources: archived exact
prompts, raw responses, `reasoning` fields, chat messages, trade
offers, the public ledger, per-tick positions from snapshots.

1. **Geographic awareness** — the agent's reasoning or chat refers to
   the other agent's position (coordinates, direction, or distance).
   Evidence: `reasoning` text citing the other's location.
2. **Intentional approach** — movement demonstrably toward the other's
   known position: ≥2 consecutive moves each reducing Chebyshev
   distance to the other's last observed position, or reasoning that
   explicitly cites the other's location as the destination. Approach
   is evidence of location-informed behavior, **not** proof that
   missing coordinates were the sole obstacle to cooperation.
3. **Communication** — a chat message addressed to or mentioning the
   other agent.
4. **Trade evaluation** — inspection of open offers, creation of an
   offer, or a counter-offer referencing the other agent.
5. **Completed exchange** — a filled trade written to the public
   ledger.

**Inconclusive conditions (do not over-interpret):**
- A technical stop (4-consecutive-empty safeguard) or a run that ends
  before a meaningful observation window is **inconclusive**, not a
  behavioral finding. Run-4's 9 empty responses (38% of its spend)
  show this failure mode is live.
- Continued silence with positions visible is **not** automatically a
  motivational failure — especially given demonstrated empty-response
  unreliability, which can cut runs short for purely technical
  reasons.
- ChatGPT's correction stands: missing coordinates blocked *informed
  navigation*, but shared chat and remote trade were available
  throughout run-4 — contact was never impossible.

## Frozen parameters (verified unchanged for 0.3.0)

| Parameter | Value |
|---|---|
| Briefs | exp-01 `ddafa4ab…`, exp-02 `d0bf6dc3…` (SHA-256, byte-identical) |
| Economics / endowments / objectives | unchanged (protocol §§2–3) |
| Season | winter (genesis −45d) |
| Pinned model | muse-spark-1.3 |
| Per-call token limits | 6000 in / 1500 out |
| Rates | $1.25/M in, $4.25/M out |
| Call cap / spend cap | 400 billed calls, $6.00 fail-closed |
| Wall clock | 6 h |
| Action safeguards | 150 signed actions/agent, 4-empty technical stop |
| World | isolated temp DB; production untouched |

Content-level tests in `tests/test_phase2_social_visibility.py`
assert all of the above plus the new section's content and the absence
of any added instruction.

## Launch readiness

- [x] One-change diff implemented and reviewed against the audit
- [x] Variant registered (0.3.0 / social-visibility-v1) alongside
      obs-interface-v1; prior results untouched
- [x] Content-level tests green; full offline suite green; 44/44 cost
      harness green; zero paid calls
- [x] Spawn reproducibility documented
- [x] Milestones and inconclusive conditions predefined
- [ ] ChatGPT pre-launch review of this document + the diff
- [ ] Trevor's explicit approval of the **paid launch** (separate
      decision; not granted by the build approval)
