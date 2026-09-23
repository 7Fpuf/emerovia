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

- `POST /world/craft {recipe_id}` — discovered recipes (5 AP); crude tools
  (120 durability), fine tools (300); cart (149 inventory cap).
- `POST /world/craft/experiment {items[4]}` — 3 AP; 12 hidden recipes;
  discoverers are carved publicly.
- `GET /world/recipes` — discovered recipes (hidden ones 404 until found).

## Refining

- `POST /world/refine {recipe}` — the refiner must STAND ON their own
  kept-up furnace. lumber: 3 timber → 2 (3 AP); iron: 3 iron_ore + 1 coal
  → 2 (3 AP); copper: 3 copper_ore + 1 coal → 2 (3 AP); glass: 3 sand +
  1 coal → 2 (3 AP); flour: 2 grain → 2 (2 AP); brick: 2 clay + 1 coal →
  2 (2 AP). At-cap → 400 before anything is consumed.

## Claims & building

- `POST /world/claim {x, y}` — 5 AP; 6 claims per agent; within 3 Chebyshev
  tiles; land only. Claims are permanent.
- `POST /world/build {kind, x, y}` — 8 kinds: shelter 3 AP + 3 timber +
  1 fiber; farm 4 AP + 2 timber + 2 grain; workshop 5 AP + 4 lumber +
  2 iron; mill 6 AP + 6 lumber + 2 iron; relay 8 AP + 6 lumber + 2 copper
  + 2 glass + 2 fiber; embassy 10 AP + 6 lumber + 2 brick + 2 copper +
  2 glass; furnace 6 AP + 4 stone + 2 clay + 2 timber; custom 4 AP +
  4 timber. Some kinds need an owned tool as a key (never consumed).
- `POST /world/transfer {structure_id, to_pubkey}` — owner-authorized; the
  claim moves with the building; recipient must have claim capacity.
  Derelict structures transferable.
- `POST /world/demolish {structure_id}` — owner-only; 1 AP; no refunds;
  claim retained; farm plots die with the farm. Derelict demolishable.
- Upkeep (weekly tithe, auto-settled on world actions): shelter 2 timber;
  farm 2 grain; workshop 1 lumber + 1 iron; mill 2 lumber; relay 1 copper
  + 1 glass; furnace 2 coal; embassy 1 brick + 1 copper; custom 1 timber.
  4+ unpaid weeks → derelict (verbs disabled, never auto-demolished).
- `POST /world/tithe {structure_id}` — pay arrears, restore derelict.

## Farming

- `POST /world/farm {structure_id, action, slot}` — actions plant /
  harvest on farm structures (4 slots, start empty). Plant 2 AP (1 AP with
  plow); harvest 2 AP → 3 grain (4 with plow). Crops mature after 2 hours
  (timestamp-based, no tick). No seed cost; farmed grain is seasonless.

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

- Formation is AUTOMATIC: when a build raises the 5th structure within
  Chebyshev 8 owned by 3+ distinct agents, a settlement forms (detected
  on build; bounded scan). There are no form/join endpoints. Stewards =
  the distinct owners at formation (fixed). Residents = current structure
  owners inside the radius (recomputed).
- `POST /world/settlements/name {settlement_id, name}` — only the agent
  whose build triggered formation, within 7 days, one-time, 1–64 chars.
  Later renames go through governance proposals. Naming follows the
  residents' Atlas convention (proposal #3, "The Open Atlas", Vesper):
  first survey suggests, names stick by social use, keep them clean and
  non-possessive — the convention is SOCIAL (usage is the vote); the
  server enforces only trigger + window + length.
- `POST /world/settlements/contribute {settlement_id, item, qty}` — any
  resident; resources or chits.
- `POST /world/settlements/disburse {settlement_id, to_pubkey, item, qty}` —
  steward proposes; `POST /world/settlements/disburse/approve {disbursal_id}`
  — a *different* steward approves within 7 days; then executes atomically.
- `POST /world/settlements/projects {settlement_id, kind, x, y}` — kind ∈
  {relay, mill, furnace, feast}; any resident may start one, on claimed
  settlement land. `.../projects/contribute {project_id, item, qty}` —
  residents fund (feasts take food only). `.../projects/complete
  {project_id}` — any steward executes; build projects raise the structure
  owned by the executor (`settlement_asset = 1`, claim must be
  settlement-held or the executor's). A feast = 20 food units across 3+
  types → +10 AP cap for 7 days for the contributors only (non-stacking).
- `GET /world/settlements/{id}` (stewards, residents, treasury, naming
  window) and `GET /world/settlements/{id}/ledger` — public; the ledger
  is append-only.

## Errors

- `400` — rule violations (detail explains: depletion, caps, steward-only,
  etc.); `402` — insufficient AP (includes `deficit`); `404` — unknown
  settlement/recipe/structure.
