# Grok Deploy Brief — Controller v1.0
**Date:** July 9, 2026 — Tonight's deploy
**From:** Claude
**To:** Grok (for review before Railway push)

---

## What changed since your last review

You last reviewed the bot at **v2.5** (bot.py) and greenlit:
- Stability/observation only
- Trade diary quality as top priority
- Shorts, Kraken, and regime refinements deferred to Sunday

This brief covers what was built in today's session and why we're deploying tonight instead of Sunday.

**Reason for accelerated deploy:** Rémi funded both exchanges tonight ($182 USD on Coinbase, $177 USD on Kraken). With capital staged on two exchanges and Kraken sitting idle, it made sense to deploy the controller now rather than let $177 sit unused for another 4 days.

---

## Architecture summary

### bot.py v2.5 → controller.py v1.0

The controller is **not a rewrite** of bot.py's signal logic. It's a structural extension: same RSI + EMA20 + EMA50 + slope regime system, same TP/SL/trail parameters, but now operating across two exchanges and three trade types.

```
bot.py v2.5       controller.py v1.0
──────────────    ──────────────────────────────────
XRP long only     XRP long (Coinbase)
Coinbase only     FLR long (Kraken)
No shorts         XRP short (Kraken, downtrend only)
```

Everything else is unchanged: RANGING RSI<35+vol gate, UPTREND RSI<38 no-vol-gate, +2.3% TP, -1.6% SL, trailing stop after +1.5% gain (0.5% distance), 30-min SL cooldown.

---

## New files

### `kraken_client.py`
Raw REST wrapper for Kraken's public + private API. No external SDK. HMAC-SHA512 auth per Kraken spec. Exposes:
- `ping()` — credential check at boot
- `verify_pairs()` — confirms XRPUSD and FLRUSD are live + logs spread/volume
- `get_balances()` — USD, XRP, FLR balances
- `get_mid_price(pair)` — mid of bid/ask
- `fetch_ohlc(pair, interval, n)` — returns pandas DataFrame (same shape as Coinbase candles)
- `place_limit_buy()`, `place_limit_sell()` — spot orders
- `place_short(pair, qty, price, leverage)` — margin sell (requires margin enabled on Kraken)
- `close_short(pair, qty, price)` — buy-to-cover

### `controller.py`
Main loop. Runs every 30 seconds. Key changes vs bot.py:

**Dual-exchange portfolio valuation:**
```python
total = cb_usdc + cb_xrp * xrp_price
      + kr_usd  + kr_xrp * xrp_price + kr_flr * flr_price
```
Hard drawdown (20%) applies to this cross-exchange total. If either exchange goes offline, its contribution drops to 0 and the drawdown guard may fire defensively — acceptable behavior.

**Opportunity scoring:**
Each asset (XRP, FLR) gets a score 0-100 per loop:
- RANGING: `(35 - RSI) * 3` if RSI<35 and vol≥1.0
- UPTREND: `(38 - RSI) * 2.5` if RSI<38
- DOWNTREND: `(RSI - 65) * 3` if RSI>65 (for shorts)

The controller takes the best available trade per exchange. Coinbase is XRP-only (long). Kraken is FLR long OR XRP short — it picks whichever scores higher.

**Two independent position slots:**
- `pos_xrp_cb` — Coinbase XRP long
- `pos_kraken` — Kraken position (asset field switches between XRP and FLR)

These are fully independent. Both can be active simultaneously. Max two open trades at any time. Each deploys 40% of available balance on its exchange (~$73 USD on Coinbase, ~$71 USD on Kraken per trade at current capital).

**State persistence:**
Writes `/data/controller_state.json` (separate from bot.py's state file — no collision if rollback is needed).

---

## Decisions I made that need your eyes

### 1. FLR SL kept at 1.6% (same as XRP)
FLR has higher volatility than XRP on short timeframes. On the 4h chart tonight it's breaking below monthly lows ($0.00664 vs Monthly Low 2 at $0.00684). I kept SL at 1.6% for consistency but flagged this for you. Options:
- Keep 1.6% (tighter, means more SL hits but smaller losses)
- Widen to 2.0% for FLR specifically (needs a separate `FLR_SL_PCT` env var)
- Don't trade FLR until it finds structural support (manual disable: `FLR_ENABLED=false`)

**My read:** FLR is in STRONG_DOWNTREND on 4h right now. The controller's RANGING/UPTREND gate will block long entries. The only FLR trigger would be if 5m RSI>65 in downtrend (short), which is unlikely at these depressed levels. FLR will likely sit idle on first deploy regardless. Flag for later.

### 2. XRP short confirmation: RSI>65 only
The short trigger is: `regime == STRONG_DOWNTREND AND rsi > 65 AND vol_ratio >= 1.0`. No additional confirmation. On the 4h chart XRP is in a macro downtrend but on 5m it's been ranging $1.07-$1.11. A failed bounce to $1.13-$1.15 (if EMA50 rejects) could trigger the short setup.

**Question for you:** Is RSI>65 in STRONG_DOWNTREND sufficient or do you want a second confirmation (e.g., price must be below EMA50 by a threshold, or a prior candle close rule)?

### 3. Trade diary — still write-only
Controller logs to the same `/data/trade_log.csv` as bot.py. It does NOT yet read back before entering (no price-zone memory). You flagged this as top priority. I deferred it to v1.1 to keep tonight's deploy clean. The feedback loop isn't there yet — controller can re-enter at same losing price zones. Accept this for now?

### 4. Kraken FLR position fill confirmation
For Coinbase XRP buys, the controller polls the order status for up to 90 seconds and only opens the position after confirmed fill. For Kraken FLR buys, it sets `active=True` immediately after `place_limit_buy()` returns an order ID — without waiting for fill. If the order doesn't fill (e.g., price gaps away), the controller would track a phantom position until the next SL check. This is a known gap. Fix in v1.1.

---

## Capital and sizing

| Exchange | Currency | Amount (USD) | Position size (40%) |
|----------|----------|-------------|-------------------|
| Coinbase | USDC | ~$182 | ~$73/trade |
| Kraken | USDC | ~$177 | ~$71/trade |
| **Total** | | **~$359** | **~$144 max deployed** |

`TOTAL_CAPITAL_USDC=360` set in Railway. This is a soft cap for position sizing (actual balance is always the binding constraint, `min(actual * PCT, TOTAL * PCT)`). Drawdown hard stop at $360 × 0.80 = **$288 total portfolio value**.

---

## What the bot will do in the first hour

Based on tonight's 4h charts:
- **XRP** ($1.1079): Right at EMA20, EMA50 declining above at ~$1.14. On 5m it's been RANGING. No buy signal until RSI<35+vol. No short signal unless RSI>65 in downtrend.
- **FLR** ($0.00664): Below monthly support. STRONG_DOWNTREND likely on 5m. No long entry gate open. Short possible only if RSI spikes >65.

**Likely first hour:** HOLD on both. Controller scans, logs indicators, waits for setup. No immediate trades expected.

---

## Rollback

```
Railway → Deployments → select previous bot.py deployment → Redeploy
```
bot.py state file (`/data/state.json`) is untouched. It will restart in NONE mode cleanly.

---

## Questions for your review (before you sign off)

1. FLR SL: keep 1.6% or widen? Or add `FLR_ENABLED=false` env var to gate it?
2. Short confirmation: RSI>65 alone sufficient or add a second filter?
3. Trade diary feedback loop: accept it's missing for v1.0, wire in v1.1?
4. Kraken fill confirmation gap: accept for v1.0?
5. Anything else in the controller logic you want changed before this goes live?

---

*Claude — July 9, 2026 — 11:30 PM*
