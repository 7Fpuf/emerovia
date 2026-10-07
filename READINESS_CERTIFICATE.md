# Phase 2 Pilot — Final Readiness Certificate

**Status: OPERATIONALLY READY. Awaiting ChatGPT's final review + Trevor's
explicit launch approval. No experiment launched. No inference spent.**

This certificate closes ChatGPT's three operational gaps (final gate
review, 2026-10-06). Economics, blind briefs, and methodology were
accepted in that review and are NOT reopened here.

---

## 1. Exact runnable command + commit

- **Branch:** `review/economic-infrastructure-spec`
- **Commit:** `3d40c0f6` + this certificate's commit (see GitHub)
- **Runner:** `tools/phase2_runner.py`, `HARNESS_VERSION = phase2-runner/0.1.0`
- **Dry-run (zero inference):**
  `PHASE2_MODEL_API_URL` / `PHASE2_MODEL_API_KEY` NOT required.
  ```
  .venv/bin/python tools/phase2_runner.py --dry-run --ticks 4 --archive <DIR>
  ```
- **Real (behavioral) run — requires Trevor's approval AND:**
  ```
  PHASE2_MODEL_API_URL=<operator endpoint> PHASE2_MODEL_API_KEY=<key> \
    .venv/bin/python tools/phase2_runner.py --archive <DIR>
  ```
  Without both env vars the run **exits 2 at startup with a loud
  error** (`AdapterConfigError`); the scripted stub is dry-run-only
  and is NEVER used as a silent fallback.
- **Archive verify (offline):**
  `.venv/bin/python tools/phase2_runner.py --verify-archive <DIR>`
- **Resume:** `.venv/bin/python tools/phase2_runner.py --resume <DIR> [--dry-run]`

## 2. Real model adapter — VERIFIED

- `PinnedModelAdapter` (name `pinned-muse-spark-1.3-api`) implements
  the pinned model against an OpenAI-compatible chat-completions
  endpoint; endpoint + key come from `PHASE2_MODEL_API_URL` /
  `PHASE2_MODEL_API_KEY` (operator launch config).
- `build_adapter(dry_run)`: dry-run → `StubAdapter`; anything else →
  real adapter or loud `AdapterConfigError`. No silent fallback —
  verified by `test_nondryrun_without_creds_fails_loud` and
  `test_main_refuses_nondryrun_without_creds` (exit 2).
- Request mapping verified against a mocked transport:
  `max_tokens=500` set on every request, model `muse-spark-1.3`,
  Bearer auth, response text + **billed** `usage` parsed.
  No live calls were made in any test.

## 3. Budget enforcement — VERIFIED (hard, at the API boundary)

- **Pre-send rejection:** prompt estimated > 6,000 tokens →
  `TokenLimitExceeded` BEFORE sending. No API call, nothing billed,
  logged as `tick_rejected`. Verified with a mocked transport that
  asserts it is never called.
- **Real billed usage:** accounted at face value from the response's
  `usage` block — never estimated, never clipped. Missing usage →
  `ModelAPIError` (refuse to continue). Billed usage above either
  per-tick cap → `BilledCapAnomaly` → `technical_stop`, run halts.
- **$4.00 hard stop:** worst-case next-call cost checked BEFORE every
  call (`budget_cap_reached` halt); billed spend re-checked AFTER
  every call (`budget_cap_exceeded` halt). Both logged as technical
  stops, never as economic findings.
- **Action cap counts failed signed attempts:** every signed POST —
  200, 400, 409, transport errors — increments the per-agent counter
  (transport errors count conservatively: may have executed).
  Unknown actions (never sent) and `wait` do not count. Per-agent
  `tick_seq` is contiguous over signed attempts (evidence chain).
- Pinned rates $1.25/$4.25 per M (standard tier); pre-registered cap
  $4.00 covers the $3.85 theoretical max (≤400 calls, ≤6k/≤500
  per tick). Token caps bind regardless of rates.

## 4. Logging, observation pipeline, recovery — VERIFIED

- **Observation pipeline** (protocol §7, "ordinary public world
  information"): every prompt carries `/world/me` + inventory,
  `/world/info` (season/day/multipliers), disclosed map window
  (radius 6), own structures incl. farm slot states, public recipe
  book, agent list, open trade offers, recent filled trades, recent
  chat — navigational/operational info only, zero strategy hints.
  Sections are priority-fitted under the 6k token cap (core state
  never dropped; dropped sections named in the prompt).
- **Evidence archive** per run (`--archive DIR`): `run.jsonl`
  (every tick: timestamp, state, token counts, action, HTTP status,
  AP/inventory deltas, stated reasoning; plus `budget_record`,
  `wake_check`, `snapshot_*`, `run_start`/`run_resumed`/`run_end`
  provenance), `snapshots/start.json` + `snapshots/end.json` (full
  world state), `ledger.json` (all filled trades), `budget.json`
  (per-call records + totals + cap), `world_end.db` (world copy),
  `runner_state.json`, `agent_keys.json` (experiment-only temp keys),
  `manifest.json` (SHA-256 of every file + brief SHAs + config).
- **Recovery:** `--verify-archive` checks manifest hashes, log
  integrity (single `run_start`, ends with `run_end`, contiguous
  `tick_seq`), budget reconciliation (per-call sums = totals =
  recomputed dollars), snapshots, and `world_end.db` matching the
  end snapshot. `--resume` restores the world DB, spend, calls,
  actions, and keys (briefs re-verified frozen; any change voids the
  resume), then continues ticking. Full dry-run → verify → resume →
  verify cycle passes in tests.

## 5. Final test results

- New: `tests/test_phase2_readiness.py` — **19/19 pass** (offline +
  stub only; zero inference spend).
- Phase 1 econ suite: included in the full run below.
- Cost-model harness `tools/validate-cost-model.py`: **44/44**.
- Full regression suite: **498/498 pass** (479 prior + 19 new).

## 6. Outstanding risks

1. **Endpoint shape.** The real adapter assumes an OpenAI-compatible
   chat-completions response shape. If the operator's Meta Model API
   endpoint differs, only `PinnedModelAdapter`'s request/response
   mapping needs a bounded change — enforcement and accounting layers
   are unaffected. Re-verify with one mocked call at launch config.
2. **Rates at launch.** Dollar cap must be recomputed from
   then-current published rates before start (token caps bind
   regardless). Pre-registered value: $4.00.
3. **Worldgen variance.** ±4 AP bands cover measured variance (10
   runs); rare excursions remain theoretically possible — the pilot
   classifies traces, it does not re-prove the economics.
4. **Stub/model behavior gap.** Dry-runs prove mechanics, not agent
   behavior. The behavioral experiment is exactly what the pilot is
   for — after approval.

## 7. Authorization gate (unchanged)

1. ✅ Phase 1 corrections (three rounds) — 479/479 tests.
2. ✅ Final preflight + operational readiness (this certificate) —
   498/498 tests, 44/44 harness.
3. ⬜ ChatGPT independent review of this certificate.
4. ⬜ **Trevor's explicit approval** — the single decision: run or don't.
5. ⬜ Only then: execute exactly per protocol, publish run log + outcome.

**No step 5 without step 4. The pilot is ON HOLD.**
