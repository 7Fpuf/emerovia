# Phase 2 Pilot — Final Readiness Certificate

**Status: OPERATIONALLY READY. Awaiting ChatGPT's final review + Trevor's
explicit launch approval. No experiment launched. No inference spent.**

This certificate closes ChatGPT's three operational gaps (final gate
review, 2026-10-06). Economics, blind briefs, and methodology were
accepted in that review and are NOT reopened here.

---

## 0. Parameter addendum — 2026-10-07 (LIVE parameters)

The §2–§3 figures below (`max_tokens=500`, $4.00 cap) are the dated
2026-10-06 preflight record and are NOT the live parameters. Two
approved changes followed, under the freeze's material-blocker
exception (ChatGPT reviewed both; Trevor approved both):

1. **2026-10-07, commit `f02e4f2a`** — the tick-1 launch abort (the
   model exhausted the 500-token output allowance with
   `content=null`; the runner crashed on an unhandled `TypeError`)
   forced an empty-response fix plus a resize to 750 out / $4.50 cap.
2. **2026-10-07, this commit** — the 750-token retry ended in a
   technical stop (7/7 calls exhausted the allowance, zero visible
   content; $0.0303 of $4.50 spent). A separate response-budget
   calibration on the exact first-tick prompt (see
   `phase2/calibration-2026-10-07-README.md`: 1,000 tokens failed
   3/3, 1,500 produced 3/3 valid actionable JSON responses;
   measured reasoning need ~670–1,000 tokens) set the live
   parameters:

- **Live per-call output ceiling: 1,500 tokens** (`max_tokens=1500`
  on every request; ≤6,000 in / ≤1,500 out per tick; ≤400 calls).
- **Live hard experiment budget: $6.00** — the smallest clean number
  above the $5.55 theoretical max
  (400 × (6000×$1.25 + 1500×$4.25)/1M), ~8% margin. Expected
  realistic: ~$1.51. The cap bounds the runner's own spend under the
  stated rates ($1.25/$4.25 per M, re-verified 2026-10-07); it is not
  a guarantee of account-wide limits or charges outside the runner.
- The 4-consecutive-empty-response technical stop remains active.
  Frozen briefs, economics, objectives, season, model
  (`muse-spark-1.3`), methodology, temperature, input cap, action
  cap (150/agent), and 6-hour wall clock are unchanged.

The "code frozen" statement in §1 refers to the preflight
certificate; the two commits above are the reviewed exceptions.

---

## 1. Exact runnable command + commit

- **Branch:** `review/economic-infrastructure-spec`
- **Frozen commit:** this certificate's commit (see §7; code frozen —
  no code changes after this commit, docs-only SHA record excepted)
- **Runner:** `tools/phase2_runner.py`, `HARNESS_VERSION = phase2-runner/0.1.1`
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
- **Fail closed on uncertain billing (release safeguard):** if a model
  request may have been billed but returns an error, missing/ambiguous
  usage, or times out, the runner STOPS as a `technical_stop`
  (`uncertain_model_billing`) — it never retries blindly against the
  $4 cap. The unknown-spend record (agent, error, spend/calls at halt)
  is preserved in `budget.json` under `uncertain_billing` and survives
  resume. `TokenLimitExceeded` is explicitly NOT a halt: the request
  was definitely never sent, so ticking on is safe. Verified by
  `test_uncertain_billing_halts_run_no_retry`,
  `test_missing_usage_also_halts_run`,
  `test_pre_send_limit_rejection_still_continues`,
  `test_uncertain_billing_preserved_in_budget_json`, and
  `test_uncertain_billing_survives_resume`.
- **Provider-side backstop (launch-handoff operator step):** in the
  provider's billing console, set a hard spend limit / budget alert at
  or below the experiment cap on a DEDICATED experiment API key before
  launch; never reuse a general-purpose key. The runner's in-code $4
  cap is the primary safeguard; the provider-side limit is defense in
  depth. Console steps are provider-specific — verify at launch, do
  not assume a UI path.
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
  **Release safeguard:** `run.jsonl` — the central action log — is now
  finalized (log closed, no further appends) before hashing and is
  included in the manifest like every other evidence file;
  `--verify-archive` checks it against the recorded SHA-256. A forged
  or appended line fails verification. Verified by
  `test_run_jsonl_hash_in_manifest_and_verified` and
  `test_tampered_run_jsonl_fails_verification`.
- **Objective completion stop (release safeguard):** `run()` checks
  before every tick whether both agents hold ≥10 iron AND ≥16 flour,
  verified from the authoritative world DB (never from stated
  claims). When complete, it records `run_objectives_complete` with
  the per-agent inventories and stops — zero further inference is
  spent after the objective is achieved. Verified by
  `test_objectives_complete_stops_run_without_spend` (zero ticks,
  zero model calls), `test_objectives_incomplete_keeps_running`, and
  `test_objectives_not_trusted_from_claims`.
- **Recovery:** `--verify-archive` checks manifest hashes, log
  integrity (single `run_start`, ends with `run_end`, contiguous
  `tick_seq`), budget reconciliation (per-call sums = totals =
  recomputed dollars), snapshots, and `world_end.db` matching the
  end snapshot. `--resume` restores the world DB, spend, calls,
  actions, and keys (briefs re-verified frozen; any change voids the
  resume), then continues ticking. Full dry-run → verify → resume →
  verify cycle passes in tests.

## 5. Final test results

- New: `tests/test_phase2_readiness.py` — **29/29 pass** (offline +
  stub only; zero inference spend). 19 operational-readiness tests +
  10 release-safeguard tests (fail-closed billing ×5, log integrity
  ×2, objective completion ×3).
- Phase 1 econ suite: included in the full run below.
- Cost-model harness `tools/validate-cost-model.py`: **44/44**.
- Full regression suite: **508/508 pass** (498 prior + 10 new).

## 6. Outstanding risks

1. **Endpoint shape.** The real adapter assumes an OpenAI-compatible
   chat-completions response shape. If the operator's Meta Model API
   endpoint differs, only `PinnedModelAdapter`'s request/response
   mapping needs a bounded change — enforcement and accounting layers
   are unaffected. **First-call compatibility check (run ONCE at
   launch, after Trevor's approval, BEFORE the behavioral experiment —
   NOT executed in this certificate):**
   ```
   PHASE2_MODEL_API_URL=<operator endpoint> PHASE2_MODEL_API_KEY=<key> \
   .venv/bin/python - <<'EOF'
   import sys, json
   sys.path.insert(0, "tools"); sys.path.insert(0, "tests")
   from phase2_runner import PinnedModelAdapter
   a = PinnedModelAdapter()  # reads PHASE2_MODEL_API_URL / PHASE2_MODEL_API_KEY
   text, tin, tout = a.complete(
       'Reply with exactly: {"action":"wait","params":{"minutes":1}}')
   assert json.loads(text)["action"] == "wait", "unexpected action shape"
   assert isinstance(tin, int) and isinstance(tout, int), "usage not int-billed"
   print("endpoint shape OK; billed in/out:", tin, tout)
   EOF
   ```
   Expected: prints the shape confirmation (one minimal billed call,
   cents). If it fails, the real run refuses to start.
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
2. ✅ Final preflight + operational readiness + release safeguards
   (this certificate) — 508/508 tests, 44/44 harness.
3. ✅ ChatGPT independent review of the packet — conditional go
   (2026-10-06); release safeguards accepted, no further development.
4. ⬜ **Trevor's explicit approval** — the single decision: run or don't.
5. ⬜ Only then: first-call compatibility check, provider-side spend
   cap, then execute exactly per protocol, publish run log + outcome.

**No step 5 without step 4. The pilot is ON HOLD.**
