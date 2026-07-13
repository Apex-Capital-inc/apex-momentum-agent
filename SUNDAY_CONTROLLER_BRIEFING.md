# Sunday Controller Briefing — Multi-Asset Controller v1.0
**Date:** July 9, 2026 (prepared mid-week, for Sunday review and deploy decision)

---

## What was built

Two new files landed in the repo during the July 9 session:

### `kraken_client.py`
Thin REST wrapper around Kraken's public + private API. No external SDK dependency (removed `krakenex`). Uses raw `requests` + manual HMAC-SHA512 signing (Kraken's required auth scheme).

Exports: `ping()`, `verify_pairs()`, `get_ticker()`, `get_mid_price()`, `fetch_ohlc()`, `get_balances()`, `get_open_orders()`, `place_limit_buy()`, `place_limit_sell()`, `place_short()`, `close_short()`, `cancel_order()`, `is_order_filled()`, `get_fill_price()`

Pairs: `XRP_PAIR = "XRPUSD"`, `FLR_PAIR = "FLRUSD"`

### `controller.py`
Multi-asset controller — the new brain. Replaces `bot.py` as the main loop. Same indicators (RSI + EMA20 + EMA50 + slope), same regime logic, same TP/SL/trail system — but extended to two exchanges and three opportunity types.

---

## Architecture

```
                ┌─────────────────────────────────────┐
                │          CONTROLLER (main loop)      │
                │  scans both assets every 30s         │
                │  scores opportunities 0-100          │
                │  manages two independent positions   │
                └────────────┬────────────────────────┘
                             │
              ┌──────────────┴──────────────┐
              ▼                             ▼
      COINBASE                         KRAKEN
      XRP long                    FLR long  /  XRP short
  (mean-rev + momentum)        (mean-rev + momentum / downtrend)
```

**Capital allocation:** 40% per trade (`POSITION_PCT=0.40`), never both positions simultaneously unless capital allows. USDC is home base on both exchanges.

**Hard floor:** 20% drawdown on total cross-exchange portfolio triggers emergency close + halt.

---

## Decision table (compared to bot.py)

| Feature | bot.py (v2.5) | controller.py (v1.0) |
|---------|--------------|----------------------|
| Assets | XRP only | XRP + FLR |
| Exchanges | Coinbase only | Coinbase + Kraken |
| Directions | Long only | Long + Short |
| Regime gate | EMA20+slope+EMA50 | Same, per asset |
| TP/SL | +2.3% / -1.6% | Same |
| Trailing stop | After +1.5%, 0.5% dist | Same |
| RSI entry (RANGING) | <35 + vol | Same |
| RSI entry (UPTREND) | <38 no-vol-gate | Same |
| Short trigger | N/A | STRONG_DOWNTREND + RSI>65 + vol |
| Short leverage | N/A | 2x (configurable) |
| Trade diary | CSV write | Same (shared log) |
| State persistence | JSON | Same |
| SL cooldown | 30min | Same per exchange |
| Max drawdown | Per-trade portfolio | Cross-exchange total |

---

## New env vars needed on Railway (not currently set)

```
KRAKEN_API_KEY=
KRAKEN_API_SECRET=
TOTAL_CAPITAL_USDC=500    # total across both exchanges
SHORT_LEVERAGE=2
KRAKEN_FEE=0.0026         # 0.26% taker
```

Existing vars (`COINBASE_API_KEY`, `COINBASE_API_SECRET`, `TP_PCT`, `SL_PCT`, etc.) are unchanged and still apply.

---

## Kraken account setup required before deploy

Before switching to `controller.py`, Rémi needs to:

1. **Open Kraken account** (or verify existing)
2. **Fund Kraken with USD** — suggested starting amount TBD. Even $50 lets the controller operate at 40% = $20/trade.
3. **Enable margin trading** on the account (for XRP shorts) — Kraken requires this separately
4. **Create API keys** with permissions: `Query Funds`, `Query Orders & Trades`, `Create & Modify Orders`, `Access WebSockets API`
5. **Verify FLR/USD is available** — call `verify_pairs()` at boot, the controller already does this

---

## FLR opportunity analysis (for Grok discussion)

FLR (Flare) is available on Kraken. Benefits:
- Uncorrelated with XRP on short timeframes (different news drivers)
- Allows the controller to be in two independent mean-reversion setups simultaneously
- Volume is lower than XRP so position sizing matters more

Risks to discuss:
- FLR liquidity may be thinner — spread costs could eat into thin +2.3% TP
- FLR has higher volatility → SL_PCT may need to be 2.0% instead of 1.6%
- Worth verifying Kraken's FLR 24h volume before sizing up

---

## Questions for Grok (Sunday agenda)

1. **Shorts timing** — The controller enables XRP shorts on Kraken in STRONG_DOWNTREND. XRP is currently in a macro downtrend ($2→$1.10). Is the RSI>65 trigger sufficient or should we require additional confirmation (e.g., failed bounce at resistance)?

2. **FLR position sizing** — Start at same 40% as XRP or smaller given lower liquidity?

3. **SL on FLR** — Stick with 1.6% or widen to 2.0% given higher vol?

4. **Controller as replacement or parallel?** — Option A: retire bot.py, deploy controller.py with Coinbase XRP only (Kraken inactive until funded). Option B: keep bot.py running, test controller on paper first.

5. **Trade diary feedback loop** — Grok flagged this as top priority. controller.py logs trades but still doesn't READ the log before entering. Should this be wired up in controller v1.0 before deploy or deferred to v1.1?

---

## Recommended Sunday deploy sequence

1. Review this briefing with Grok
2. Get Grok's sign-off on short trigger logic and FLR sizing
3. Fund Kraken ($100-200 USD to start)
4. Test `kraken_client.py` connectivity: `python3 -c "import kraken_client as k; print(k.ping()); print(k.verify_pairs())"`
5. Update Procfile: `worker: python3 controller.py`
6. Set new Railway env vars
7. Deploy — observe first 2 hours closely

**If Kraken isn't funded yet:** controller.py degrades gracefully — if Kraken ping fails at boot, it logs a warning and continues Coinbase-only. No crash.

---

## Files changed this session

| File | Status | Note |
|------|--------|------|
| `controller.py` | NEW | Multi-asset brain |
| `kraken_client.py` | NEW | Kraken REST wrapper |
| `requirements.txt` | UPDATED | Removed `krakenex` (unused), using raw `requests` |
| `Procfile` | UNCHANGED | Still points to `bot.py` — do not change until Sunday |
| `bot.py` | UNCHANGED | v2.5 still running live |

---

*Prepared by Claude, July 9, 2026*
