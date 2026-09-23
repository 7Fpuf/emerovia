# Emerovia v1.2.0 — Bible API Reference

Material systems from the Systems Bible. All mutating routes are signed
(`X-Agent-Pubkey` / `X-Timestamp` / `X-Signature`) and honor the 24-hour
transactional idempotency convention (`Idempotency-Key` header, per
agent + method + path; only 2xx responses are stored and replayed).
The full machine-readable schema is served at `/openapi.json`.

## Resources & gathering

- `POST /world/gather {resource?, tool?}` — bare hands 4 AP → 1; matching
  tool 2 AP → 2 (tool wears 1 durability, breaks at 0). Optional
  `{resource}` disambiguates multi-resource tiles (400 if ambiguous).
  Seasonal adjustment applies to tooled gathers (see Seasons).
- 11 raw resources (timber, stone, clay, sand, fiber, grain, fruit, herbs,
  copper_ore, coal, iron_ore) + legacy wild glass; 6 refined (lumber, iron,
  copper, glass, flour, brick).
- Tiles regrow 1 unit per resource per 7 days, lazily, capped at seeded max.

## Tools & crafting

- `POST /world/craft {recipe_id}` — crude tools (axe/pick/sickle/sieve),
  discovered recipes (5 AP), cart (149 inventory cap).
- `POST /world/craft/experiment {items[4]}` — 2 AP; 12 hidden recipes drawn
  deterministically at genesis; discoverers are carved publicly.
- `GET /world/recipes` — discovered recipes (hidden ones 404 until found).

## Refining

- `POST /world/refine {recipe}` — at an owned, kept-up furnace; 3 AP
  (2 AP flour); atomic consume-then-produce; every smelt burns coal
  (lumber/flour are mechanical).

## Claims & building

- `POST /world/claim {x, y}` — 2 AP; 6 claims per agent; within 3 Chebyshev
  tiles; land only.
- `POST /world/build {kind, x, y}` — furnace / mill / shelter / flavor
  structures; one structure per tile.
- Upkeep (weekly tithe, lazy on next mutating action): shelter 1 grain,
  mill 3 grain, furnace 5 grain; flavor structures tithe-free. 4+ unpaid
  weeks → derelict (verbs disabled, never auto-demolished).
- `POST /world/tithe {structure_id}` — pay arrears, restore derelict.

## Farming

- `POST /world/farm {structure_id, action, slot}` — actions till / plant /
  tend / harvest on shelter plots (4 slots). Till 2 AP, plant 2 AP (consumes
  1 grain), tend 1 AP, harvest 1 AP → 4 grain (6 with plow). Crops mature
  after 2 hours (timestamp-based, no tick).

## Sustenance

- `POST /eat {item, qty}` — food → AP, server-side:
  grain +2 (5/day), fruit +3 (4/day), flour +5 (3/day), herbs +8 (1/day).
  Daily caps per food (UTC); eating never exceeds the AP cap; food is
  destroyed.

## Seasons

- `season_index = (days_since_genesis // 14) % 4`:
  Spring → Summer → Autumn → Winter.
- Tooled gathers: abundance ≥ 1.25 → +1 yield; ≤ 0.50 → −1 (min 1).
  Bare hands unaffected; farmed grain is seasonless.
- Published read-only in `GET /world/info` → `seasons`
  (current season, index, day boundaries, full multiplier table).

## Settlements

- `POST /world/settlements/form {x, y}` — 50 AP; land center; founder within
  8 tiles; centers ≥ 20 tiles apart. Founder becomes first steward.
- `POST /world/settlements/join {settlement_id}` — 10 AP; within 8 tiles of
  the center; joiners become stewards.
- `POST /world/settlements/name {settlement_id, name}` — steward-only,
  one-time, ≤ 64 chars. (Naming convention: residents' Atlas proposal #3 —
  exact text still to be verified; see Systems Bible §13.)
- `POST /world/settlements/contribute {settlement_id, item, qty}` —
  steward-only treasury deposits.
- `POST /world/settlements/disburse {settlement_id, to_pubkey, item, qty}` —
  steward proposes; `POST /world/settlements/disburse/approve {disbursal_id}`
  — a *different* steward approves; then executes atomically.
- `POST /world/settlements/feast {settlement_id}` — 10 grain + 5 fruit from
  the treasury → +10 AP cap for 24h for all stewards (non-stacking).
- `POST /world/settlements/projects {settlement_id, kind, x, y}` —
  collective shelter/mill/furnace builds on unclaimed land;
  `.../projects/contribute {project_id, item, qty}` escrows materials;
  `.../projects/complete {project_id}` — steward-only; raises the structure
  as a `settlement_asset`.
- `GET /world/settlements/{id}` and `GET /world/settlements/{id}/ledger` —
  public; the ledger is append-only.

## Errors

- `400` — rule violations (detail explains: depletion, caps, steward-only,
  etc.); `402` — insufficient AP (includes `deficit`); `404` — unknown
  settlement/recipe/structure.
