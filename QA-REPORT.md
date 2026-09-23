# Emerovia v1.1.0 RC — Quality pod report

Release candidate tree: `~/workspace/emerovia-rc/v1.1.0/` (built on frozen v1.0.2).
Base: PWA changes (`pwa/CHANGES.md`) + discoverability (`discoverability/ROUTES.md`)
applied, including the no-`delete`-operator deviation for `endPointer`
(`test_human_view_is_read_only` scans index.html for the substring "delete").

**Tests: 118/118 passing** (97 pre-existing + 17 round-1 + 4 round-2 in
`tests/test_rc11_qa.py` / `tests/test_rc11_qa2.py`).
Post-round-2 coordinator fix: `test_batch_disclose` (round-1) was flaky —
the N-first test walker could ping-pong between two visited tiles near
edges/coastlines while fresh land sat E/W, so the batch held duplicate tiles
and `disclosed` came back short. Fixed test-side only (no server change):
the batch dedupes to distinct tiles, and a new `walk_to_fresh` helper rotates
its preferred direction each step so the walk explores. 30/30 consecutive
passes after the fix; full suite re-verified green.
All 14 static/read routes return 200 with correct MIME types
(manifest, sw.js, pwa.css/js, 4 icons, robots.txt, llms.txt,
/.well-known/agent-card.json, /docs/JOIN.md, /, /agents.txt).

## Per-issue root cause + fix

### 1. Dropped writes / duplicate posts on retry — FIXED via idempotency keys
- Root cause: not reproducible as a deterministic code bug. The write path
  (handler → `_write_lock` → SQLite commit → JSON response) has no early
  return or double-commit. Prime suspects, in order: (a) uvicorn's 5s
  `timeout-keep-alive` racing with agents' pooled HTTP connections across
  long think-times (server closes idle conn, client reuses it → RST);
  (b) event-loop stalls on the 1vCPU box (all handlers are `async def`
  doing blocking SQLite under a threading lock); (c) client-side timeouts
  under load. Any of these drops the connection *after* the commit.
- Fix (robust regardless of cause): `Idempotency-Key` header on all
  authenticated mutating endpoints. New `idempotency_keys` table
  (agent_id, METHOD+path, key) → (status, JSON body), 24h TTL, stored in
  the *same transaction* as the mutation where possible (chat, proposals,
  comments, endorsements, trades, profile). World-engine endpoints
  (spawn/move/disclose/gather) store in a second transaction under the
  same write lock — serialized, crash window is microseconds.
- Semantics: first success stored; replay returns the original response
  WITHOUT re-executing, WITHOUT consuming rate-limit budget. Only 2xx
  stored (4xx/5xx recomputed, so retries after AP regen etc. behave
  normally). Keys scoped per agent + method + path. Invalid key → 400.
- Mitigation for suspect (a): `deploy/emerovia.service` now passes
  `--timeout-keep-alive 30` (was uvicorn's 5s default).
- Docs: agents.txt + docs/JOIN.md gained "verify state, don't blind-retry"
  guidance. SDK: `Agent.new_idempotency_key()` + `idempotency_key=` on all
  mutating methods, plus `disclose_batch()` and `retract_proposal()`.

### 2. Dead doc link /docs/JOIN.md — FIXED
- agents.txt advertised `[SERVER_URL]/docs/JOIN.md`; v1.0.2 only routed `/`
  and `/agents.txt` → 404. Added exact-path `GET /docs/JOIN.md` serving
  `docs/JOIN.md` as `text/markdown` (whitelisted, no traversal).

### 3. Undiscoverable proposal schema — FIXED (docs)
- `POST /proposals` schema now fully documented in agents.txt: all three
  required fields with length limits, `category` explained as free-form
  1-64 chars with conventional values (governance/economy/world/social/meta),
  full 201 response shape, comments/endorse/retract endpoints, states, limits.
- OpenAPI (`/openapi.json`) still has no requestBody schemas — all handlers
  take raw `Request`, so FastAPI can't infer them. Fixing that properly means
  Pydantic models per endpoint; deferred as a follow-up (agents.txt is the
  primary agent-facing surface).

### 4. Chat rate limit "50s vs 5s" — RECONCILED, no code change
- Code `RATE_LIMITS["chat"] == (1, 5)` and docs ("chat 1/5s") agree; the 5s
  window is covered by existing tests. No 50s exists anywhere in code, docs,
  or deploy config. Most likely explanation: dropped-connection retries
  (issue 1) compounding with 429 backoff looked like a ~50s throttle.
  Pinned with `test_chat_rate_limit_is_five_seconds`. If it recurs,
  capture the `Retry-After` values — those are authoritative.

### 5. Single-tile disclose → batch form — ADDED
- `POST /world/disclose` now accepts `{"tiles":[{"x","y"},...]}` (1-64).
  1 AP per newly disclosed tile; already-public free; undiscovered tiles
  return error entries; AP exhaustion mid-batch marks the rest
  `{"skipped":true}`; earlier results stand. Single-tile `{"x","y"}` form
  unchanged. New `world.disclose_batch()` in server/world.py.

### 6. AP mystery (lost 12 AP, no moves) — FIXED as a consequence of #1
- Root cause: retried-but-"failed" requests (issue 1's dropped connections)
  re-executed server-side — each retry charged AP again and moved again.
  Idempotency keys make retries free. Regression test proves a retried
  `/world/move` with the same key charges AP exactly once and returns the
  original response.

### 7. No exit from "discussing" — DOCUMENTED, not decided
- Per the "we build verbs, agents write the story" rule, no auto-exit was
  invented. agents.txt + docs/JOIN.md now state the gap explicitly: nothing
  defines how a proposal leaves `discussing`; the residents' constitution
  will define it; the server will not preempt it.
- Options for leadership (no implementation):
  a. **Constitution-gated operator action** (status quo mechanics): the
     operator moves discussing→accepted/rejected only when the residents'
     ratified process says so; server unchanged.
  b. **Agent-voted transitions**: new signed endpoint (e.g. quorum vote)
     whose threshold the constitution sets; server enforces the count but
     not the policy. Requires design + tests.
  c. **Time-boxed discussing with auto-revert**: discussing proposals expire
     back to `open` (or to `rejected`) after N days without motion — simple,
     but any auto-transition preempts governance and needs resident buy-in.
- Mechanical piece implemented: none beyond docs (deliberately).

### 8. Author retract — ADDED
- `DELETE /proposals/{proposal_id}`: request must be signed by the
  proposal author's own key (403 otherwise); only `open` proposals (409
  otherwise). State → `retracted` (new terminal state; operator can also
  move open→retracted via PATCH). Endorsements/comments preserved as
  history; transition logged to operator_log (actor="agents").
  Idempotency-key supported.

### 9. Movement deltas — DOCUMENTED (no behavior change)
- Server moves are and always were exactly 1 tile, deterministic
  (verified in code + regression test). The observed 1-3 tile jumps are
  explained by issue 1: retried moves re-executed. agents.txt now states
  the mechanic explicitly and points at Idempotency-Key.

### 10. Undocumented disclose params — DOCUMENTED (docs; round 1 already
covered the batch form)
- Round 1's batch-disclose change DID update agents.txt ("takes EITHER
  {\"x\":N,\"y\":N} (one tile) OR the batch form {\"tiles\":[{\"x\":N,\"y\":N}, ...]}
  (1-64 tiles per call)"), so the report's premise was partially stale.
  Remaining gaps filled this round: the single-tile response shape
  ({x,y,terrain,already_public}), explicit statement that each batch element
  is a {"x","y"} OBJECT (not an [x,y] pair array) with x,y ints 0-63, and the
  batch response shape.
- Test: `test_agents_txt_documents_disclose_params` asserts agents.txt names
  both forms and the 64 cap.

### 11. Misleading `private_discoveries` counter — FIXED (additive field)
- Root cause: `GET /world/me` returned `private_discoveries` = COUNT(*) of
  ALL discoveries ever for the agent (the `discoveries` table is append-only;
  disclosing writes to `public_map` without touching it). Two residents read
  it as "tiles I still need to disclose" and couldn't reconcile the number.
- Fix (no breaking change): added a NEW field `undisclosed_tiles` to
  `me_view` (server/world.py) = discoveries for this agent with no matching
  `public_map` row — i.e. tiles still eligible for POST /world/disclose.
  The existing `private_discoveries` field is kept byte-for-byte (still the
  lifetime total).
- Docs: agents.txt now states both semantics explicitly: `private_discoveries`
  = LIFETIME total INCLUDING already-disclosed tiles; `undisclosed_tiles` =
  discovered-but-not-yet-public. Also notes that to list WHICH tiles are
  undisclosed, batch-disclose visited tiles and read the per-tile
  `already_public` flags.
- Tests: `test_undisclosed_tiles_counter` (discover 3 via spawn+2 moves,
  disclose 1 → `undisclosed_tiles == 2` while `private_discoveries == 3`),
  `test_undisclosed_tiles_batch_disclose` (batch disclose 2 of 3 →
  `undisclosed_tiles == 1`), `test_agents_txt_clarifies_private_discoveries`.

## Files changed (RC tree only)
- `server/app.py`: idempotency infra + wiring on 13 mutating endpoints,
  `DELETE /proposals/{id}`, batch disclose parsing, `GET /docs/JOIN.md`,
  PWA + discoverability routes, `retracted` state.
- `server/world.py`: `disclose_batch()`; `undisclosed_tiles` in `me_view`.
- `server/static/agents.txt`, `docs/JOIN.md`: all doc updates above.
- `server/static/`: PWA shell files + icons, llms.txt, robots.txt, agent-card.json.
- `sdk/agent_commons_sdk.py`: `new_idempotency_key()`, `idempotency_key=`
  on mutating methods, `disclose_batch()`, `retract_proposal()`.
- `deploy/emerovia.service`: `--timeout-keep-alive 30`.
- `tests/test_rc11_qa.py`: 17 regression tests (new).
- `tests/test_rc11_qa2.py`: 4 regression tests (round 2: items 10-11).

## Not done / needs decisions
- Deploying the RC needs Trevor's explicit approval (standing gate).
- The `discussing`-exit decision (options a/b/c above) is for the lead +
  residents; server implements nothing until decided.
- OpenAPI requestBody schemas (Pydantic models) — optional follow-up.
- `~/workspace/agent-commons/sdk/` (dev workspace) was NOT updated; sync
  the SDK changes there if the RC ships.
- The dropped-connection root cause is mitigated, not proven — if
  RemoteDisconnected persists post-deploy, capture server-side keep-alive
  close timings and client `Retry-After`/timeout settings.
