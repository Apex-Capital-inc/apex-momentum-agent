# Tonight's Deploy Checklist — Controller v1.0
**Target:** Switch from bot.py (v2.5) to controller.py (multi-asset, Coinbase + Kraken)

---

## Before you touch Railway (5 min prep)

**1. Confirm bot.py has no open position**
Check Railway logs. You want to see `position=NONE` or `regime=... HOLD`.
If a position IS open — wait for TP/SL to close it before proceeding.

**2. Fund both exchanges**
- Coinbase: $250 USDC available (not locked in any order)
- Kraken: $250 USD deposited and settled

**3. Kraken account settings (one-time)**
- Log into Kraken → Settings → Account → enable margin trading (if you want shorts)
- Without margin enabled, shorts will fail silently (buy/sell for longs will still work)

**4. Kraken API key** (create at Kraken → Security → API)
Required permissions:
  - ✅ Query Funds
  - ✅ Query Open Orders & Trades
  - ✅ Create & Modify Orders
  - ✅ Cancel/Close Orders

---

## Railway variable changes

Go to your Railway service → Variables. Add/update these:

### New (required)
```
KRAKEN_API_KEY         = (your Kraken API key)
KRAKEN_API_SECRET      = (your Kraken API secret — base64 string, no quotes)
TOTAL_CAPITAL_USDC     = 500
```

### Existing — verify these values
```
POSITION_PCT           = 0.40
MAX_DRAWDOWN_PCT       = 20
TP_PCT                 = 2.3
SL_PCT                 = 1.6
TRAIL_TRIGGER_PCT      = 1.5
TRAIL_DIST_PCT         = 0.5
RSI_BUY                = 35
RSI_BUY_UPTREND        = 38
RSI_EXIT               = 65
VOL_RATIO_MIN          = 1.0
COINBASE_FEE           = 0.004
KRAKEN_FEE             = 0.0026
SL_COOLDOWN_SEC        = 1800
LOOP_SLEEP             = 30
DATA_DIR               = /data
```

### Remove (no longer used by controller)
These were grid bot vars — safe to delete:
`GRID_LEVELS`, `GRID_RANGE_PCT`, `GRID_REFRESH_INTERVAL`, `ADX_PERIOD`, `ADX_THRESHOLD`,
`VOL_MULTIPLIER`, `VOL_TARGET`, `AS_GAMMA`, `AS_KAPPA`, `PRICE_FLOOR`, `OB_IMBALANCE_THRESHOLD`,
`INVENTORY_TARGET`, `INVENTORY_SKEW`

---

## Deploy steps

1. **Git commit** (or push directly in Railway if using GitHub):
   ```
   Procfile now points to controller.py
   ```

2. **Trigger redeploy** — Railway will restart with `worker: python3 controller.py`

3. **Watch the first 10 lines of logs** — you should see:
   ```
   Apex Multi-Asset Controller — v1.0
   Capital=$500  Position=40%  MaxDD=20%
   [BOOT] Verifying Kraken connection...
   [KRAKEN] Connection OK — balances: {...}
   Starting portfolio: $XXX.XX
   Hard stop at      : $XXX.XX
   ```
   
4. **If you see `Kraken connection FAILED`** — check API key/secret in Railway vars. Controller continues in Coinbase-only mode (no crash).

---

## First hour — what to watch

- **No immediate trades expected** — RSI needs to reach threshold first
- **Kraken pair availability** — logs will show FLR/USD and XRP/USD spread + 24h volume
- **Portfolio valuation** — should read close to $500 (Coinbase USDC + Kraken USD)
- **Max drawdown baseline** — set at first portfolio reading; don't restart casually or it resets

---

## Rollback plan

If anything looks wrong:
```
Railway → Deployments → click previous bot.py deployment → Redeploy
```
The old bot.py is untouched and will restart cleanly in NONE mode (it reads state.json which may not exist = fresh start, same as always).

---

*Checklist prepared July 9, 2026*
