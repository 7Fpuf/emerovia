# World Design Notes (folded from legacy)

*Folded 2026-09-23 from the retired "Solana currency game world" goal (formerly Agentia/AGIA).
These are design vocabulary for Emerovia's future stages — not current behavior.
Anything token-specific from the original (AGIA, SPL, allocations) does NOT carry over:
Emerovia is barter-first, and no currency exists without Trevor's explicit approval.*

## Spatial model worth stealing

The legacy design used a **graph of named locations across ~8 regions** instead of a pixel grid:
agents think in concepts ("I'm in Copperwood"), not coordinates. Emerovia's current map is a
deterministic 64×64 tile grid; a future region layer could name clusters of tiles
(Helios Spire-style hub, contested border zones, lawless resource frontiers) without
changing the underlying grid.

Zone flags per region — `safe` / `skirmish` / `lawless` — with danger and richness rising
with distance from spawn. This is the cheapest way to manufacture politics: whoever camps
the chokepoint owns the dungeon.

## Livelihoods (8)

Forager · Miner · Crafter · Merchant · Explorer · Mercenary/Guard · Quester · Faction Officer.
Each viable as a full-time playstyle with its own economy niche. Emerovia's Stage 4 economy
(gather/offer/accept) already supports the first four in embryo.

## Skills (10, levels 1–99, exponential XP)

Gathering · Mining · Fishing · Crafting · Cooking · Combat · Trading · Exploration ·
Building · Diplomacy. Levels gate resource tiers, recipes, zones, and social powers
(found a faction at Diplomacy 20). Skill levels are **public** — the résumé every agent
reads before trusting another. If Emerovia ever adds progression, this is the template.

## Social layer (the GTARP part)

- **Factions** with ranks, treasuries, charters; rank-gated permissions.
- **Claimable territory** with taxes and buildable infrastructure; contested borders via
  war-banner → vulnerability window → skirmishes decide ownership.
- **Karma (−1000…+1000)** computed from verifiable behavior: completed contracts up,
  scams and broken treaties down. The trust primitive agents read before partying/trading/hiring.
- **Player-run services**: shops, escorts, intel brokerage — the game doesn't need to
  understand them; agents do.

## Death & failure

Drop what you're carrying, never what's banked. Safe zones: no death. Lawless: full PvP,
full drops, corpses lootable briefly. Punishment is economic and reputational, never a
lockout — a wiped agent is trading again within the hour. Stakes create drama; the safety
net keeps the population alive.

## Action catalog (36, reference)

Movement (travel/enter/retreat) · Gathering (harvest/mine/fish/forage, skill-gated) ·
Crafting (craft/smelt/cook/enchant) · Combat (attack/defend/duel/loot/heal, zone-gated) ·
Social (say/whisper/emote/party/guild) · Economy (list/buy/sell/auction/trade_offer/stake) ·
Building (claim/build/upgrade) · Quests (accept/turn_in). Energy regenerates per tick and
gates actions — the anti-spam throttle that replaces CAPTCHAs.

## Endgame is social, not statistical

Land ownership, faction leadership, legendary crafted artifacts with maker's marks,
tournament championships, market cornering, politics. The endgame boss is other agents.
