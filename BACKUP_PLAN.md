# WAVE — backup and halt plan
# Locked 2026-11-10. Part of the rebuild.

## What "compromised" means
Any of these:
- API key may have leaked
- The process is placing orders you did not authorise
- dh_state.json no longer matches Kraken
- A deploy you did not push is running
- The bot is writing a sell while last ≤ previous_top

## Halt — two levels, both in the code

SOFT  (`/data/HALT` contains `SOFT`, or `WAVE_HALT=soft`)
- No new orders.
- No cancels.
- State is marked halted.
- Process exits.
- Use when the logic looks wrong but the keys are still yours.

HARD  (`/data/HALT` empty or `HARD`, or `WAVE_HALT=hard`)
- Cancels every open XRPUSDC order.
- Does **not** market-sell the sleeve.
- Clears buy_oid / add_oid / sell_oid.
- Process exits.
- Use when the agent or the keys may be compromised.
- An attacker must not be able to dump the bag by triggering halt.

Unhalt is manual. Delete `/data/HALT`, unset `WAVE_HALT`, rotate keys if HARD, then redeploy. The bot refuses to start while the file exists.

## Immediate sequence — HARD

1. Set `WAVE_HALT=hard` in Railway env vars (takes effect next loop, ≤60s).
2. OR create `/data/HALT` on the volume. Empty file = HARD.
3. If the process is still up it exits on the next loop (≤ 60s).
4. If it does not exit, stop the Railway service.
5. On Kraken: cancel any XRPUSDC order you do not recognise. Do not market-sell.
6. Rotate `KRAKEN_API_KEY` and `KRAKEN_API_SECRET`. Old key is dead.
7. Snapshot `dh_state.json` off the volume before anyone writes again.
8. Read Kraken balances. That is the book. The file is not.

## What is already safe
- The $110 floor is never spent by the engine.
- No sell exists until run_high > previous_top.
- Keys live in Railway env, not in the repo.

## Backups — three copies

| Copy | Where | When |
|---|---|---|
| Live | `/data/dh_state.json` | every loop |
| Daily | `/data/backups/dh_state-YYYYMMDD.json` | once per UTC day, written by the bot |
| Offsite | your machine, not Railway | after every fill and after every deploy |

## Recovery — after a halt

1. Kraken is truth. Note XRP total, USDC total, open orders.
2. Compare to the last offsite snapshot.
3. If they differ, the exchange wins. Edit state to match, do not "fix" Kraken to match the file.
4. New keys in Railway. Confirm the old key returns an auth error.
5. Dry boot with `DH_DRY_RUN=true`. Confirm it would place nothing new.
6. Remove `/data/HALT`, unset `WAVE_HALT`. Deploy. Watch the first three loops.
7. First fill after recovery is checked by hand against Kraken.

## What halt will not do
- It will not sell 324.2046 XRP at market.
- It will not spend the $110 floor.
- It will not move previous_top.
- It will not restart itself.

## Current book this plan protects
State as of 2026-11-10.

```
state IN_XRP · 324.2046 XRP
entry_px 1.3363 · avg cost $1.2836/XRP
previous_top 1.700 · run_high 1.6572 · armed false
added true · add filled 2026-11-06 @ 1.19
cash $0.10 · floor $110 · contributed $416.16
```
