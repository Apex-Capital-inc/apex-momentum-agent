# Apex Momentum Agent — Full Grok Briefing
## Date: June 30, 2026 — End of Day 1

---

## The Mission

Rémi runs a 24/7 trading bot on Railway (cloud). Capital: ~$88 USDC (started at $140, grid bot eroded it).
Goal: grow the wallet consistently using a directional momentum strategy.
Constraint: fully unattended — no manual intervention.

---

## What We Replaced and Why

The original bot was a **grid bot** on XRP-USDC. After 1 week:
- Net result: $140 → ~$135 (fees eating every cycle)
- Root cause: XRP daily range (~4%) was smaller than the grid step (5%), so sell orders never filled
- Structural flaw: grid bots market-make. On $140 capital with 0.4% fees each side, you need massive volume to profit. It was a fee machine, not a profit machine.

**Decision**: scrap the grid bot entirely. Build a directional momentum agent.

---

## The New Bot — Apex Momentum Agent v2.0

**Strategy**: RSI + volume oversold bounce plays. One position at a time.

**Signal logic:**
- BUY: RSI(14) < 35 AND vol_ratio ≥ 0.3
- EXIT: TP +3.2% (limit order placed at entry) OR SL -1.6% (bot monitors price) OR RSI > 65

**Risk management:**
- Position size: 70% of capital per trade
- 30% permanent USDC buffer
- Trailing stop: activates at +1.5% profit → floor at breakeven → trails 0.5% below high-water mark
- SL cooldown: 30 min after any stop-loss hit (no revenge trading)
- Hard stop: bot halts if total drawdown ≥ 20%

**Parameters (Grok validated in previous session):**
| Parameter | Value |
|-----------|-------|
| TP | +3.2% |
| SL | -1.6% |
| RSI buy | < 35 |
| RSI exit | > 65 |
| EMA period | 20 |
| Candle size | 5 min |
| Vol ratio min | 0.3 |
| Cooldown | 30 min |
| Max drawdown | 20% |

---

## Full Bug History — Day 1 (June 30, 2026)

This was a brutal day of SDK bugs. Every single one is now fixed. Here's the complete log:

### Bug 1: Coinbase API Key PEM format
**Error**: `Could not deserialize key data. InvalidByte. ASN.1 parsing error`
**Cause**: The Coinbase Ed25519 private key from the JSON file contains real newlines. Railway stores env vars as single-line strings. Pasting the key via copy-paste corrupted the newlines.
**Fix**: Bot code converts literal `\n` → real newlines at startup:
```python
_api_secret = os.environ.get("COINBASE_API_SECRET").replace("\\n", "\n")
```
Also wrote `fix_railway_keys.py` — reads the JSON file directly and sets Railway vars via CLI, bypassing copy-paste entirely.

### Bug 2: `get_balances` — dict vs object
**Error**: `'dict' object has no attribute 'value'`
**Cause**: `coinbase-advanced-py` SDK returns `available_balance` as a **dict**, not an object. Code was calling `.value` on it.
**Fix**:
```python
bal = acct.available_balance
val = bal["value"] if isinstance(bal, dict) else bal.value
```

### Bug 3: Division by zero on startup
**Error**: `ZeroDivisionError: float division by zero` at `drawdown = (start_val - port_val) / start_val * 100`
**Cause**: When Bug 2 caused `get_balances` to fail, `get_portfolio_value` returned $0. `start_val = 0` → division by zero.
**Fix**: Startup retries 6× until `start_val > 0`. Drawdown guarded with `if start_val > 0 else 0.0`.

### Bug 4: `place_limit_buy` order_id extraction
**Error**: `'dict' object has no attribute 'order_id'`
**Cause**: SDK `limit_order_gtc_buy` returns response where `success_response` is a **dict**, not an object. Code tried `resp.success_response.order_id`.
**Fix**: Universal helper function:
```python
def _extract_order_id(resp) -> str:
    if isinstance(resp, dict):
        return resp.get("order_id") or resp.get("success_response", {}).get("order_id", "")
    sr = getattr(resp, "success_response", None)
    if sr is not None:
        return sr.get("order_id") if isinstance(sr, dict) else getattr(sr, "order_id", "")
    return getattr(resp, "order_id", "") or ""
```

### Bug 5: Repeated buying (critical)
**Cause**: Because Bug 4 returned empty order_id, `buy_oid` was "". The code did `if not buy_oid: continue` — SKIPPED setting `in_position = True`. Next loop, RSI still oversold, bot placed ANOTHER buy. This repeated 5× — spent ~$120 buying XRP with no TP orders protecting any of it.
**Fix 1**: The `_extract_order_id` fix above (order_id now correctly returned).
**Fix 2**: Safety guard added — if XRP balance > $5 already held, skip the buy:
```python
if xrp_bal * price > 5.0:
    log.warning("Skipping BUY — already holding XRP with no tracked position")
    continue
```

### Bug 6: `get_portfolio_value` — fake 70% drawdown (critical)
**Error**: `MAX DRAWDOWN 70.9% hit — cancelling position and stopping` — immediately after opening a position.
**Cause**: After buying XRP, the XRP moves from available balance into the TP limit order (locked). `get_balances()` returns 0 XRP available. The `get_portfolio_value` function tried to count XRP from open sell orders but `base_size` on order objects is also a dict — exception caught silently — so XRP from the TP order wasn't counted. Result: portfolio looked like only $27 (USDC buffer) → fake 70% drawdown → bot halted and emergency-sold the position.
**Fix 1**: Universal `_safe_get` helper for all SDK object access:
```python
def _safe_get(obj, key, default=None):
    if isinstance(obj, dict):
        return obj.get(key, default)
    return getattr(obj, key, default)
```
**Fix 2** (more reliable): When `in_position`, calculate portfolio from known state instead of querying API:
```python
if in_position and xrp_position > 0:
    usdc_free, _ = get_balances()
    port_val = usdc_free + xrp_position * price
else:
    port_val = get_portfolio_value(price)
```
This avoids any API timing issues entirely.

### Bug 7: `is_order_filled` and `get_fill_price` — same dict issue
All order query functions updated to use `_safe_get` for consistent dict/object handling.

### Bug 8: EMA filter blocking all entries
**Problem**: Original signal required `price > EMA(20)`. XRP was in a downtrend all day — price consistently below EMA. Combined with RSI < 35 + volume condition, the three conditions NEVER all aligned simultaneously:
- When RSI < 35 → price below EMA → blocked
- When volume high → RSI already recovered above 35 → blocked
**Fix**: Removed EMA from BUY condition. Strategy is now a pure oversold bounce: RSI < 35 + vol_ratio ≥ 0.3. EMA still computed and logged for info but doesn't gate entries.

### Bug 9: VOL_RATIO_MIN too strict
**Problem**: With `VOL_RATIO_MIN = 1.0`, RSI hit 23-28 multiple times overnight but volume was always 0.04-0.73 during those dips. Bot never fired.
**Fix**: Lowered progressively: 1.0 → 0.5 → 0.3 via Railway env var.

---

## Current Status (8:40 PM, June 30)

**Bot**: ACTIVE, holding a LONG position
**Portfolio**: ~$87 USDC equivalent
**Position**: ~59 XRP bought around $1.034
**TP target**: $1.0675 (+3.2%) — limit order open on Coinbase
**SL**: $1.0176 (-1.6%) — monitored by bot every 30s
**Drawdown**: ~0.5% (correctly calculated, no more false halts)
**Trail**: OFF (not yet at +1.5% trigger)

The bot has been holding this position for several hours with no crashes. First stable position since launch.

---

## Financial Summary — Day 1

| Event | P&L |
|-------|-----|
| Grid bot week (starting point) | -$5 |
| Repeated buys Bug 5 (5 unprotected buys → manual sells) | -$3 approx |
| False drawdown Bug 6 (bought $1.034, emergency sold $1.034) | ~$0 (breakeven - fees) |
| Current open position | TBD (waiting for TP at $1.0675) |
| **Net capital today** | **~$87** |

The losses are entirely from bugs, not from bad trades. The strategy itself hasn't had a clean run yet until this current position.

---

## What's Working Now

1. ✅ API authentication — stable
2. ✅ Balance reading — dict handling fixed
3. ✅ Order placement — order_id correctly extracted
4. ✅ TP limit order — placed correctly after buy
5. ✅ Position tracking — `in_position` correctly set
6. ✅ Portfolio valuation — no more fake drawdown
7. ✅ Repeated buy guard — XRP balance check prevents doubles
8. ✅ Bot holds position for hours without crashing

---

## Questions for Grok

### 1. Signal parameters
Given that XRP has been ranging $1.03–$1.07 with low volume during North American night hours, is RSI < 35 + vol_ratio ≥ 0.3 a good signal combination? Or would you tighten/loosen?

### 2. EMA filter removal
We removed `price > EMA(20)` from the BUY condition because it was preventing all entries during the overnight downtrend. Was this the right call? Does buying oversold RSI regardless of trend direction make sense for XRP on 5-min candles?

### 3. TP target realism
TP is at +3.2%. Looking at today's chart, XRP ranged from $1.030 to $1.077 — a 4.5% intraday range. Is +3.2% achievable in a typical XRP session, or should we tighten to +2% for more frequent fills?

### 4. Current open position
Entry ~$1.034, TP $1.0675, SL $1.0176. XRP currently $1.034 (flat). Chart shows a bearish structure on 15m. Should the bot hold or would you recommend a tighter exit here?

### 5. Capital recovery
We're at $87 from $140 start (losses from grid bot week + day-1 bugs). What's the realistic timeline to recover to $100, then $140, given this strategy? What win rate and trade frequency should we expect?

### 6. One thing we're missing
From your perspective — what's the single biggest risk or flaw in this setup that we haven't addressed?

---

## Files

| File | Purpose |
|------|---------|
| `bot.py` | Main agent — live on Railway (v2.0, all bugs fixed) |
| `requirements.txt` | `coinbase-advanced-py`, `pandas`, `python-dotenv`, `cryptography` |
| `fix_railway_keys.py` | Reads Coinbase JSON → sets Railway vars automatically |
| `bot_grid_backup.py` | Old grid bot — archived |
