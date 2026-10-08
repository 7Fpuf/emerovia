# Response-budget calibration — 2026-10-07

## ⚠️ CALIBRATION ONLY — NOT A PHASE 2 OBSERVATION

The six model calls recorded in `calibration-2026-10-07.jsonl` were a
**non-behavioral engineering calibration**. The model's returned actions
were **NEVER executed** against any world, and these calls are **not**
Phase 2 observations. They exist solely to answer: *how much completion
budget does `muse-spark-1.3` need to produce a valid action JSON on the
exact first-tick pilot prompt?*

## Context

Two behavioral launch attempts (2026-10-07) ended without any agent
action: the model exhausted the full per-call output allowance on
reasoning and returned `content=null` — first at 500 tokens (tick-1
abort, runner crash), then at 750 tokens (technical stop after 4
consecutive empty responses, $0.0303 of $4.50 spent). Both were
classified as technical calibration failures, not behavioral results.

Rather than guess another number and re-run the six-hour experiment,
the calibration tested completion ceilings on the **exact first-tick
prompt** (built verbatim by the reviewed runner's `build_prompt` at
commit `f02e4f2a`, on a fresh isolated temp world, exp-01) with the
same model, temperature (0.7), and 6000-token input cap.

## Method

Ladder, in order: **1000 → 1500 → 2000** completion tokens, up to 3
independent calls per level. Stop rule: first level producing 3
consecutive valid actionable JSON responses without hitting its output
ceiling. 2000 was never tested (stop rule fired at 1500).

## Prompt provenance

- `calibration-2026-10-07-prompt.txt` — the exact prompt sent.
- SHA-256: `bca8976404ca76615d2e136aef896473e16b2267b93dc0f0075a3ad15e53b47c`

## Results (per-call records in `calibration-2026-10-07.jsonl`)

| Call | Ceiling | Billed in/out | Visible content | finish_reason | reasoning_tokens (measured) | JSON valid | Action | Hit ceiling |
|---|---|---|---|---|---|---|---|---|
| 1 | 1000 | 915 / 1000 | no | length | 997 | no | — | yes |
| 2 | 1000 | 915 / 1000 | no | length | 997 | no | — | yes |
| 3 | 1000 | 915 / 1000 | no | length | 960 | no | — | yes |
| 4 | 1500 | 915 / 1043 | yes | stop | 988 | yes | farm_plant | no |
| 5 | 1500 | 915 / 984 | yes | stop | 936 | yes | farm_plant | no |
| 6 | 1500 | 915 / 723 | yes | stop | 669 | yes | move | no |

- Level 1000: 3/3 failed (all truncated, `finish_reason=length`, zero
  visible content).
- Level 1500: 3/3 valid actionable JSON, none hit the ceiling. The
  parsed actions (farm_plant ×2, move ×1) were economically sensible
  first-tick moves but were **never executed** and are not findings.

## Key measurement

The provider exposed `reasoning_tokens` in
`usage.completion_tokens_details` on these calls. Measured reasoning
need on the first-tick prompt: **~670–1000 tokens**; the visible JSON
action is **~50 tokens**. This supersedes the earlier inference ("the
500-token call was probably all reasoning") with a measured split.

## Budget consequence

At pinned rates ($1.25/M in, $4.25/M out, re-verified 2026-10-07):

- Theoretical maximum: 400 × (6000×$1.25 + 1500×$4.25)/1M = **$5.55**
- Expected pilot cost: ~300 calls × ~$0.00504 ≈ **~$1.51**
- Proposed hard cap: **$6.00** (smallest clean number above $5.55,
  ~8% margin)

Calibration spend: **$0.0313** (6 calls).

## What changed (and what did not)

- CHANGED: per-call completion ceiling 750 → **1500**; hard
  experiment budget $4.50 → **$6.00**.
- UNCHANGED: model (`muse-spark-1.3`), frozen blind briefs, economics,
  objectives, season, prompts, temperature, input cap, action cap
  (150/agent), 6-hour wall clock, 4-consecutive-empty technical stop,
  methodology.
