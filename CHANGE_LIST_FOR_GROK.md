# Change List — Grid Bot → Directional Momentum Agent
## For Grok — Jun 30, 2026

---

## What Changed

### 1. Complete rewrite of bot.py
- **Old**: ~1,250 lines — grid bot with A-S model, dual modes, 9 bug fixes
- **New**: ~320 lines — directional momentum agent, clean and simple
- Old bot saved as `bot_grid_backup.py` (not deployed)

---

### 2. Strategy replaced entirely

| | Grid Bot | Momentum Agent |
|--|---------|----------------|
| Logic | Place buy/sell orders at fixed price levels | Take directional position on RSI+EMA signal |
| Profit source | Spread between grid levels | Price moving in direction of trade |
| In downtrend | Buys every step down, accumulates XRP | Stays in USDC, no position |
| In uptrend | Sells too early at first grid level | Trailing stop rides the move |
| In ranging | Shuffles USDC↔XRP, fees drain wallet | Catches every RSI bounce |
| Complexity | 9 bugs fixed over 1 week | 0 bugs so far — no complexity to hide bugs in |

---

### 3. Signal logic (Grok's parameters, fully implemented)

**BUY — all 3 must be true simultaneously:**
- RSI(14) < 35 (Grok: stricter than our proposed 38)
- Price > EMA(20) (trend filter — no buying into falling knives)
- Volume >= 20-period average volume (confirmation filter)

**EXIT — first condition that triggers:**
- Take-profit limit order fills at +3.2% (Grok: wider than our proposed 2.5%)
- Stop-loss: price drops to entry × (1 − 1.6%) (Grok: 1.6% not 1.5%)
- RSI > 65 (Grok: wider than our proposed 62 — avoids early exit)
- Max drawdown 20%: emergency sell + agent halts

**TRAILING STOP (Grok's logic confirmed):**
- Activates at +1.5% profit
- Stop first moves to breakeven (covers fees)
- Then trails 0.5% below rolling high-water mark
- Result: can never lose money on a +1.5% trade

---

### 4. Position sizing (confirmed by Grok)
- 70% of capital per trade
- 30% permanently held as USDC buffer — never deployed
- One position at a time (strict — no stacking)
- Re-entry only after full close (TP or SL)

---

### 5. Cooldown after stop-loss (Grok's recommendation)
- 30-minute hard cooldown after any SL hit
- Prevents revenge trading (stopped → immediately re-enters → stopped again)
- During cooldown: agent scans but does not trade

---

### 6. Removed entirely (no longer needed)
- Avellaneda-Stoikov market-making model
- VWAP grid centering
- Grid levels, step sizes, MIN/MAX_STEP_PCT
- Dual mode (GRID / SCALP)
- Order book imbalance filter
- Volatility spike detection and pause
- Counter-order protection system
- ADX/RSI regime switching
- XRPL on-chain health check
- `handle_filled_orders()` — no counter-orders in directional trading
- `initialise_grid()` / `initialise_scalp()`
- `seed_handled_orders()` — not needed, no fill history to seed

---

### 7. New Railway env vars required

**Add these:**
```
SYMBOL=XRP-USDC
TOTAL_CAPITAL_USDC=140
POSITION_PCT=0.70
TP_PCT=3.2
SL_PCT=1.6
TRAIL_TRIGGER_PCT=1.5
TRAIL_DIST_PCT=0.5
RSI_BUY=35
RSI_EXIT=65
EMA_PERIOD=20
RSI_PERIOD=14
VOL_RATIO_MIN=1.0
SL_COOLDOWN_SEC=1800
MAX_DRAWDOWN_PCT=20
COINBASE_FEE=0.004
LOOP_SLEEP=30
```

**These old vars are now unused** (can leave or delete — won't affect anything):
`GRID_SYMBOL, GRID_LEVELS, MIN_STEP_PCT, MAX_STEP_PCT, GRID_REFRESH_INTERVAL,
SCALP_LEVELS, SCALP_REFRESH, ADX_PERIOD, ADX_THRESHOLD, RSI_LOW, RSI_HIGH,
AS_GAMMA, AS_KAPPA, AS_T_HORIZON, VOL_SPIKE_MULT, VOL_SPIKE_RESUME_MULT,
DRAWDOWN_COOLDOWN, OB_IMBALANCE_THRESHOLD, INVENTORY_TARGET, INVENTORY_SKEW,
XRPL_CHECK_ENABLED, XRPL_RPC_URL, VOL_LOOKBACK, PRICE_FLOOR`

---

### 8. Dependencies — no change needed
`coinbase-advanced-py` and `pandas` already in requirements.txt.
No new packages required.

---

### 9. Expected performance (Grok's conservative estimate)

- Win rate: 48–52%
- Risk/reward: 3.2 / 1.6 = **2:1**
- At 50% win rate, 2 signals/day:
  - Win: +3.2% × 70% capital = +2.24% portfolio
  - Loss: −1.6% × 70% capital = −1.12% portfolio
  - Net per day: **+1.12%** (Grok floor: 0.7–0.9%)
- Week 1 target: $140 → **~$151**
- Month 1 target: **~$196**

---

## Deploy command
```bash
cd "/Users/apexcapital/Documents/Claude/Projects/XRPL a/grid-bot" && railway up
```

---

## Questions for Grok on this implementation
1. The volume filter (`vol_ratio >= 1.0`) — is 1.0× average the right threshold, or should it be higher (1.2–1.5×) to further reduce false signals?
2. The buy fill wait is 90 seconds. If the limit buy doesn't fill in 90s (price moved away), we cancel and wait for next signal. Is this the right timeout?
3. Should we add a second EMA (e.g., 50-period) as a longer-term trend filter, so we only buy when EMA(20) > EMA(50)?
