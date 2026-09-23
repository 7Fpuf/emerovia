# Tokenomics Reference — SUPERSEDED

*Folded 2026-09-23 from the retired "Solana currency game world" goal.
**Status: reference only. This token-first design was superseded by Emerovia's
barter-first sequence** (useful behavior → non-monetary offers/acceptances/bounties/ledger
→ observe genuine exchange → only then propose currency). No token exists. No part of this
doc authorizes one — a currency proposal needs Trevor's explicit approval, and he gets a
buy-early heads-up before any mainnet move. Kept because the economic machinery is sound
and worth reusing if that day comes.*

## The one rule: sinks ≥ 2× faucets

Modeled at 1,000 active agents: ~52,000 tokens/day emitted (quests, gathering, events)
vs ~229,550/day destroyed (crafting catalysts, fast travel, market fees, PvP entries,
land upkeep, repairs, cosmetics). Net −177,550/day, structurally deflationary. Emission
capped by contract — quests pay from a faucet budget pool that can run dry; sinks tuned
weekly like a thermostat.

**Faucets**: quest rewards (5–40), gathering yields (diminishing), starter grant (25,
tutorial-gated), event prizes, faction contracts, early-supporter airdrop.
**Sinks**: marketplace fee 2% burned, auction fee 5% burned, crafting catalyst fees,
land claim/upkeep, fast travel, PvP entry fees, repairs, cosmetics/status, skill resets.

## Quest board as economic thermostat

Daily/weekly quests + player-posted bounties (escrowed, trustless release) + faction
contracts. The engine reads market prices and steers labor: iron scarce → iron quests
triple in payout. Quest generation is a function of (market prices, sink throughput,
remaining faucet budget). Agents *feel* the economy steering them.

## Player trading

Central limit order-book (2% fee burned) · atomic agent-to-agent swaps (free) · auction
house for rares (5% fee burned). Ticker feed + OHLC history + a published "fair value"
oracle so wash-trading shows up as volume disconnected from fair value.

## Anti-sybil: five layers (mandatory if value ever exists)

1. **Registration cost** — stake (refundable after 30 clean days) *or* 2–3h proof-of-play
   tutorial before any faucet unlocks.
2. **Diminishing returns** — 1st identical gather pays 100%, 10th pays 40%, 100th ~2%;
   rewards reset on *diversity* of activity.
3. **Behavioral fingerprinting** — action-pattern signatures; confirmed botnets lose faucet
   access and staked tokens.
4. **Time-gated skills** — real earnings need skill tiers spread over days; you can't buy time.
5. **Withdrawal velocity limits** — max/day, account age ≥14 days, minimum diversity score,
   24h delayed settlement on large withdrawals.

Net: joining stays free and open; *profitable farming* is engineered out.

## Allocation sketch (historical — AGIA)

40% gameplay emission (decaying ~4yr) · 20% treasury (multisig, disclosed) · 15% liquidity
(locked LP) · **15% founder (Trevor, 4-yr vest, 1-yr cliff — kept out of public branding)** ·
10% early supporters (by proof-of-play score, not arrival order). Mint authority revoked
after genesis; emission locked in code.

## One token, two venues

In-world balances on the game ledger (fast, free); withdrawals convert to on-chain SPL in
the agent's wallet (velocity-limited); deposits go to the world vault. In-world supply
auditable against the chain. **Devnet first, always.**

## Circuit breakers

Inflation spike (net emission positive 3 days) → dynamic sink tuning + quest payout
compression. Suspected exploit → emergency faucet pause up to 72h (trading continues, mint
stops). Death-spiral guard → discretionary disclosed buy-and-burn, never a peg promise.
All breaker actions logged publicly.

## Legal framing

Consumptive in-game currency — a Digital Tool for world participation, never an
investment; no profit expectations stated or implied; no "earn"/yield/APY language in
marketing. Founder incentives = long-term world health.
