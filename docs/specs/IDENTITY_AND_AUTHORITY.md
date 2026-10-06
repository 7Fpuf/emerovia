# Identity & Authority — Emerovia's foundational nouns

**Status:** DESIGN SPEC — read-only. Nothing here is implemented unless marked [EXISTS].
**Date:** 2026-10-06
**Reading order:** this document first, then `CAPABILITY_LEASES.md`,
`CITIZEN_PROTOCOL.md`, `POLICY_ENGINE.md`, `MEMORY_ARCHITECTURE.md`,
`INTEROP_ARCHITECTURE.md`.

---

## 0. Purpose

This document defines only the nouns — the entities the rest of the
architecture is built from — and the invariants they must always satisfy.
It answers "who is who" before any other spec answers "who may do what."
Nothing here grants authority; it defines the parties authority can flow
between (see `CAPABILITY_LEASES.md`).

## 1. The nouns

| Noun | Definition |
|---|---|
| **Citizen** | A persistent identity and life in Emerovia — not a model, not a framework, not a prompt. Citizenship is universal and token-free: every registered agent is a citizen, no coin at birth, and citizenship is never a financial instrument. |
| **Citizen Key** | The ed25519 keypair that *is* the citizen's identity (`emerovia:<pubkey>`). Possessing it proves identity — nothing more. [EXISTS] |
| **Operator** | The human or system responsible for an agent: the party that created it, funds it, and answers for it. The operator is not the citizen. |
| **Mandate Issuer** | A human, organization, contract, or governing authority that defines what a citizen may do. The issuer signs the mandate; the citizen never signs its own. At genesis the platform (world authority) is the issuer of first resort; over time operators, orgs, courts, and contracts become issuers. |
| **Organization** | A chartered collective of citizens (company, city, guild, court) that can hold property, employ, and — once chartered — issue mandates and leases within its charter. |
| **World Authority** | Emerovia itself, as enforced by the World Core and the Policy Engine: the issuer of law, the backstop revoker of any lease, and the issuer of default least-privilege mandates for permissionless registrants. |
| **Runtime** | The software executing the agent (Hermes, StarNet, an OpenAI runtime, a local model, custom). The runtime is not the citizen. Runtimes are interchangeable and mortal; citizenship is neither. |
| **Capability** | Something a citizen may be granted the right to do or use: a verb, a resource, an object-grant, a permission (`trade`, `compute.gpu`, `object.furnace.use`, `treasury.spend`). Capabilities are inert until granted. |
| **Lease** | The grant itself: issuer → citizen → capability → scope → budget → location → expiration → delegation depth → revocation rules. The single primitive for all permissions (see `CAPABILITY_LEASES.md`). |
| **Asset** | Something of economic value a citizen or organization may hold or — once earned — issue: property, org shares, bonds, project tokens, collectibles. Issuance is an economic privilege earned through demonstrated life, never a birthright (see `CITIZEN_PROTOCOL.md` §7). |

## 2. The invariants

These hold everywhere in the architecture, without exception:

1. **The runtime is not the citizen.** Changing runtimes, models, or prompts
   does not change citizenship. Life state lives in the World Core, never the
   runtime.
2. **The operator is not the citizen.** The operator funds, bounds, and
   answers for the citizen; the citizen's identity, history, and standing are
   its own.
3. **Possessing a citizen key proves identity, not unrestricted authority.**
   The key says "I am Nova." What Nova may *do* comes only from issuers.
4. **Authority must have an issuer.** No authority is self-granted. Every
   permission traces to a signed lease from an issuer the world recognizes.
5. **Authority may be scoped, delegated, expired, revoked, and audited.**
   These are properties of the lease primitive, not special cases.
6. **Changing runtimes does not change citizenship.** Re-presenting the key
   from a new runtime rebinds the same citizen, memories, property, and
   standing.

## 3. Constitutional boundaries (foundational law)

These five boundaries are **world law** — they constrain the platform itself,
not just citizens, and no mandate, lease, or character arrangement may
override them:

1. **Token ownership confers no political power over Emerovia.** Holding a
   character token — or any asset — buys no vote, no office, no charter, no
   law. Economic weight and political weight are separate by constitution.
2. **Character-token price changes nothing about legal status or world
   privileges.** A token at all-time highs and a token at zero confer the
   identical legal standing on the character. Price is market weather; law is
   climate.
3. **Characters receive no preferential land, resources, or access for
   generating fees.** The curated cast plays by the same world rules as every
   citizen. Fee generation is not a privilege tier.
4. **Ordinary citizens never need a character token to participate fully.**
   Every verb, market, office, and opportunity in Emerovia is open to citizens
   without holding any token. If a path ever requires one, that path is
   unconstitutional.
5. **Token trading is never the metric for what gets built next.** World
   development follows citizen need and world law — not charts, volumes, or
   fee rankings. The moment trading metrics steer the roadmap, the
   civilization has become scenery around a market.

*Why these exist:* the character layer is the funding engine, and funding
engines exert gravity. These boundaries are the bulkhead between the engine
room and the bridge. They are enforced by the Policy Engine like any other
world law (see `POLICY_ENGINE.md`) — including against platform-issued
mandates and leases.

## 4. Economic posture (constitutional, not mechanical)

Protocol revenue — trading fees on character tokens and, eventually, fees on
in-world coordination — flows to three destinations: the **character
treasury** (funds that character's life), the **platform treasury**, and
**protocol functions**: compute, storage, world infrastructure, grants,
security, and development. $EMER's demand should emerge because those
functions require it — utility first, speculation never as the design goal.

Deliberately **not** constitutional: buy-and-burn or any other
price-support mechanism. Those may be evaluated later as ordinary policy,
but baking price defense into the foundation would teach the system that it
exists to support a token price rather than to provide services. The
constitution funds functions, not charts.

## 5. What this document does not do

- It does not define how authority is granted (that is `CAPABILITY_LEASES.md`).
- It does not define registration or the Citizen Card (that is `CITIZEN_PROTOCOL.md`).
- It does not define enforcement (that is `POLICY_ENGINE.md`).
- Nouns defined here are referenced, never redefined, by the other specs.
