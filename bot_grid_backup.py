"""
XRP/USDC Floating Grid Scalping Bot — Coinbase Advanced Trade
-------------------------------------------------------------
Engine    : Avellaneda-Stoikov institutional market-making model.
            Reservation price shifts grid center based on inventory.
            Optimal spread sets step size based on vol + liquidity.
            Grid centered on VWAP (stable) not last-trade price (noisy).

Filters   : ALL must be green to trade:
            1. ADX(10) < 25       — market must be ranging, not trending
            2. RSI(14) 35–65      — market must be neutral, not extreme
            3. No vol spike       — current vol < 2× 24h baseline
            4. OB imbalance       — skip heavy one-sided pressure per side

Signals   : XRPL on-chain health adjusts buy size (±10–20%)
            High network activity → 10% larger buys
            Low network activity  → 20% smaller buys

Protection: Hard PRICE_FLOOR — no buy below this level (ever)
            MAX_DRAWDOWN_PCT pause with DRAWDOWN_COOLDOWN auto-restart
            30-min forced GRID_REFRESH_INTERVAL (recenter on VWAP)
            75% recenter threshold on sudden price moves
"""

import os
import time
import math
import logging
import json
import urllib.request
from datetime import datetime, timezone
from dotenv import load_dotenv
from coinbase.rest import RESTClient

# ── Setup ──────────────────────────────────────────────────────────────────────
load_dotenv()
os.makedirs("logs", exist_ok=True)

logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s  %(levelname)s  %(message)s",
    handlers=[
        logging.StreamHandler(),
        logging.FileHandler("logs/bot.log"),
    ],
)
log = logging.getLogger(__name__)

# ── Core config ────────────────────────────────────────────────────────────────
API_KEY        = os.getenv("COINBASE_API_KEY")
PRIVATE_KEY    = os.getenv("COINBASE_PRIVATE_KEY")
SYMBOL         = os.getenv("GRID_SYMBOL", "XRP-USDC")
_GRID_LEVELS_ENV = os.getenv("GRID_LEVELS")   # None = auto-size by capital
GRID_LEVELS      = int(_GRID_LEVELS_ENV) if _GRID_LEVELS_ENV else None  # resolved at runtime
GRID_RANGE_PCT = float(os.getenv("GRID_RANGE_PCT", 12))
TOTAL_CAPITAL  = float(os.getenv("TOTAL_CAPITAL_USDC", 1000.0))
MAX_DRAWDOWN   = float(os.getenv("MAX_DRAWDOWN_PCT", 25)) / 100

POLL_INTERVAL      = 15    # seconds between price checks
RECENTER_THRESHOLD = 0.75  # recenter when price drifts past 75% of grid edge
MIN_ORDER_USDC     = 1.0

# ── Carver volatility targeting ────────────────────────────────────────────────
VOL_LOOKBACK   = int(os.getenv("VOL_LOOKBACK", 6))        # candles (hours) — 6h VWAP tracks current price
VOL_MULTIPLIER = float(os.getenv("VOL_MULTIPLIER", 2.0))
VOL_TARGET     = float(os.getenv("VOL_TARGET", 0.15))     # 15% annual vol target

# ── Geometric grid + fee-aware spacing ────────────────────────────────────────
COINBASE_FEE = float(os.getenv("COINBASE_FEE", 0.004))    # 0.4% maker fee per side
MIN_STEP_PCT = float(os.getenv("MIN_STEP_PCT", 1.5))      # > 2× fee → always profitable
MAX_STEP_PCT = float(os.getenv("MAX_STEP_PCT", 5.0))

# ── Inventory management ───────────────────────────────────────────────────────
INVENTORY_TARGET = float(os.getenv("INVENTORY_TARGET", 0.5))
INVENTORY_SKEW   = float(os.getenv("INVENTORY_SKEW", 0.3))

# ── ADX market filter ──────────────────────────────────────────────────────────
ADX_PERIOD    = int(os.getenv("ADX_PERIOD", 10))
ADX_THRESHOLD = float(os.getenv("ADX_THRESHOLD", 25))
ADX_LOOKBACK  = ADX_PERIOD * 3

# ── RSI filter ─────────────────────────────────────────────────────────────────
# Grid trades only when RSI is in the neutral zone (not extreme).
# RSI < RSI_LOW  = oversold  (strong buys fine, but grid may get trapped on sell side)
# RSI > RSI_HIGH = overbought (strong sells fine, but grid may get trapped on buy side)
RSI_PERIOD = int(os.getenv("RSI_PERIOD", 14))
RSI_LOW    = float(os.getenv("RSI_LOW", 35))
RSI_HIGH   = float(os.getenv("RSI_HIGH", 65))

# ── Hard price floor ───────────────────────────────────────────────────────────
# No buy order will ever be placed below this price.
# No ceiling — we never cap upside. Set to 0 to disable.
PRICE_FLOOR = float(os.getenv("PRICE_FLOOR", 0.50))

# ── Order refresh timer ────────────────────────────────────────────────────────
# Every N seconds, cancel all orders and rebuild grid centered on current VWAP.
# Prevents stale grids from drifting too far from fair value.
GRID_REFRESH_INTERVAL = int(os.getenv("GRID_REFRESH_INTERVAL", 1800))  # 30 min

# ── Avellaneda-Stoikov parameters ──────────────────────────────────────────────
# Institutional market-making model. Adjusts grid center and spread based on
# inventory position and volatility.
#
# γ (gamma): risk aversion coefficient.
#   Higher = more aggressively shifts reservation price when inventory skews.
#   Range: 0.05 (conservative) to 0.5 (aggressive). Default 0.1.
#
# κ (kappa): order arrival rate / market liquidity proxy.
#   Higher = tighter optimal spread (more liquid market).
#   Range: 0.5 (thin) to 3.0 (deep). Default 1.5.
#
# T_HORIZON: normalized time horizon (always 1.0 for rolling session).
AS_GAMMA     = float(os.getenv("AS_GAMMA", 0.1))
AS_KAPPA     = float(os.getenv("AS_KAPPA", 3.0))   # tighter spread = more fills
AS_T_HORIZON = float(os.getenv("AS_T_HORIZON", 1.0))

# ── Scalp mode (always-on when grid is paused) ─────────────────────────────────
# When ADX/RSI conditions are unfavorable for full grid, switch to scalp mode:
# a tight 4-level grid centered on current mid-price, refreshed every 15 min.
# Bot NEVER sits idle — catches small oscillations in any market condition.
# Only true pause: volatility spike or drawdown limit.
SCALP_LEVELS  = int(os.getenv("SCALP_LEVELS", 4))
SCALP_REFRESH = int(os.getenv("SCALP_REFRESH", 900))   # 15 minutes

# ── Volatility spike detection ─────────────────────────────────────────────────
# If current vol > VOL_SPIKE_MULT × rolling baseline → pause immediately.
# Resume when vol drops below VOL_SPIKE_RESUME_MULT × baseline.
VOL_SPIKE_MULT        = float(os.getenv("VOL_SPIKE_MULT", 3.0))
VOL_SPIKE_RESUME_MULT = float(os.getenv("VOL_SPIKE_RESUME_MULT", 1.5))

# ── Drawdown cooldown ──────────────────────────────────────────────────────────
# After hitting max drawdown, bot pauses for this many seconds then auto-restarts.
# Markets recover — a permanent stop means missing the rebound.
DRAWDOWN_COOLDOWN = int(os.getenv("DRAWDOWN_COOLDOWN", 14400))  # 4 hours

# ── Order book imbalance filter ────────────────────────────────────────────────
# imbalance = (bid_qty − ask_qty) / (bid_qty + ask_qty), range [-1, +1]
# −1 = pure sell pressure → skip placing buys this cycle
# +1 = pure buy pressure  → skip placing sells this cycle
OB_IMBALANCE_THRESHOLD = float(os.getenv("OB_IMBALANCE_THRESHOLD", 0.4))

# ── XRPL on-chain health ───────────────────────────────────────────────────────
# Adjusts buy order sizes based on XRPL network activity.
# High tx/ledger → 10% larger buys. Low → 20% smaller. Fails gracefully.
XRPL_CHECK_ENABLED = os.getenv("XRPL_CHECK_ENABLED", "true").lower() == "true"
XRPL_RPC_URL       = os.getenv("XRPL_RPC_URL", "https://s1.ripple.com:51234")


def send_email(subject: str, body: str):
    """Email removed — all events logged to Railway console instead."""
    log.info(f"EVENT: {subject}")


# ── Coinbase helpers ───────────────────────────────────────────────────────────
def get_client() -> RESTClient:
    return RESTClient(api_key=API_KEY, api_secret=PRIVATE_KEY)


def get_price(client: RESTClient) -> float:
    """Last best bid — fast single-value price check."""
    data = client.get_best_bid_ask(product_ids=[SYMBOL])
    try:
        pricebooks = data.pricebooks if hasattr(data, "pricebooks") else data["pricebooks"]
        bids       = pricebooks[0].bids if hasattr(pricebooks[0], "bids") else pricebooks[0]["bids"]
        price      = bids[0].price if hasattr(bids[0], "price") else bids[0]["price"]
        return float(price)
    except Exception as e:
        log.error(f"get_price parse error: {e}  raw={data}")
        raise


def get_mid_price(client: RESTClient) -> float:
    """Best bid/ask midpoint — more accurate than last trade price."""
    data = client.get_best_bid_ask(product_ids=[SYMBOL])
    try:
        pricebooks = data.pricebooks if hasattr(data, "pricebooks") else data["pricebooks"]
        pb   = pricebooks[0]
        bids = pb.bids if hasattr(pb, "bids") else pb["bids"]
        asks = pb.asks if hasattr(pb, "asks") else pb["asks"]
        best_bid = float(bids[0].price if hasattr(bids[0], "price") else bids[0]["price"])
        best_ask = float(asks[0].price if hasattr(asks[0], "price") else asks[0]["price"])
        return (best_bid + best_ask) / 2
    except Exception as e:
        log.warning(f"get_mid_price fallback to last price: {e}")
        return get_price(client)


def get_vwap(client: RESTClient) -> float | None:
    """
    Volume-Weighted Average Price from last VOL_LOOKBACK hourly candles.
    VWAP is far more stable than last trade price — it's the true
    'fair value' anchor used by institutional traders.
    Returns None on error (caller falls back to mid-price).
    """
    try:
        end   = int(time.time())
        start = end - (VOL_LOOKBACK * 3600)
        response = client.get_candles(
            product_id=SYMBOL,
            start=str(start),
            end=str(end),
            granularity="ONE_HOUR",
        )
        candles = response.candles if hasattr(response, "candles") else response.get("candles", [])
        if len(candles) < 3:
            return None

        total_vol = total_pv = 0.0
        for c in candles:
            high   = float(c.high   if hasattr(c, "high")   else c.get("high",   0))
            low    = float(c.low    if hasattr(c, "low")    else c.get("low",    0))
            close  = float(c.close  if hasattr(c, "close")  else c.get("close",  0))
            volume = float(c.volume if hasattr(c, "volume") else c.get("volume", 0))
            if volume > 0 and high > 0 and low > 0:
                typical    = (high + low + close) / 3
                total_pv  += typical * volume
                total_vol += volume

        if total_vol == 0:
            return None

        vwap = total_pv / total_vol
        log.info(f"VWAP({VOL_LOOKBACK}h): ${vwap:.5f}")
        return vwap
    except Exception as e:
        log.warning(f"VWAP calc error: {e}")
        return None


def get_order_book_imbalance(client: RESTClient) -> float:
    """
    Top-of-book bid/ask quantity imbalance.
    Range [-1, +1]:  -1 = all sell pressure, +1 = all buy pressure, 0 = balanced.
    Used to skip placing orders into a heavily one-sided market.
    """
    try:
        data = client.get_best_bid_ask(product_ids=[SYMBOL])
        pricebooks = data.pricebooks if hasattr(data, "pricebooks") else data["pricebooks"]
        pb   = pricebooks[0]
        bids = pb.bids if hasattr(pb, "bids") else pb["bids"]
        asks = pb.asks if hasattr(pb, "asks") else pb["asks"]

        def _level_qty(lvl) -> float:
            for attr in ("quantity", "size"):
                if hasattr(lvl, attr):
                    v = getattr(lvl, attr)
                    return float(v) if v else 0.0
            return 0.0
        bid_qty = sum(_level_qty(b) for b in bids)
        ask_qty = sum(_level_qty(a) for a in asks)
        total   = bid_qty + ask_qty
        if total == 0:
            return 0.0

        imbalance = (bid_qty - ask_qty) / total
        log.info(f"Order book: bid={bid_qty:.2f}  ask={ask_qty:.2f}  imbalance={imbalance:+.3f}")
        return imbalance
    except Exception as e:
        log.warning(f"Order book imbalance error: {e} — assuming neutral")
        return 0.0


# ── Indicator calculations ─────────────────────────────────────────────────────
def calculate_volatility(client: RESTClient) -> float:
    """
    Carver volatility targeting — XRP daily vol from VOL_LOOKBACK hourly candles.
    Returns daily vol as decimal (0.03 = 3%/day). Falls back on error.
    """
    fallback = GRID_RANGE_PCT / 200
    try:
        end   = int(time.time())
        start = end - (VOL_LOOKBACK * 3600)
        response = client.get_candles(
            product_id=SYMBOL,
            start=str(start),
            end=str(end),
            granularity="ONE_HOUR",
        )
        candles = response.candles if hasattr(response, "candles") else response.get("candles", [])
        if len(candles) < 5:
            log.warning(f"Only {len(candles)} candles — using default vol")
            return fallback

        raw = []
        for c in candles:
            ts    = float(c.start if hasattr(c, "start") else c.get("start", 0))
            close = float(c.close if hasattr(c, "close") else c.get("close", 0))
            raw.append((ts, close))
        raw.sort(key=lambda x: x[0])
        closes = [r[1] for r in raw if r[1] > 0]

        if len(closes) < 5:
            return fallback

        returns    = [math.log(closes[i] / closes[i-1]) for i in range(1, len(closes))]
        mean       = sum(returns) / len(returns)
        variance   = sum((r - mean) ** 2 for r in returns) / len(returns)
        hourly_vol = math.sqrt(variance)
        daily_vol  = hourly_vol * math.sqrt(24)
        annual_vol = daily_vol  * math.sqrt(365)

        log.info(
            f"Volatility — hourly: {hourly_vol*100:.3f}%  "
            f"daily: {daily_vol*100:.2f}%  "
            f"annual: {annual_vol*100:.1f}%"
        )
        return daily_vol

    except Exception as e:
        log.error(f"Volatility calc error: {e} — using fallback")
        return fallback


def calculate_adx(client: RESTClient) -> float | None:
    """
    ADX market filter — Wilder's smoothing, ADX_PERIOD hourly candles.
    ADX < threshold  → ranging → grid active.
    ADX >= threshold → trending → grid pauses.
    Returns None if insufficient data (safe default: don't block).
    """
    try:
        end   = int(time.time())
        start = end - (ADX_LOOKBACK * 3600)
        response = client.get_candles(
            product_id=SYMBOL,
            start=str(start),
            end=str(end),
            granularity="ONE_HOUR",
        )
        candles = response.candles if hasattr(response, "candles") else response.get("candles", [])

        raw = []
        for c in candles:
            ts    = float(c.start if hasattr(c, "start") else c.get("start", 0))
            high  = float(c.high  if hasattr(c, "high")  else c.get("high",  0))
            low   = float(c.low   if hasattr(c, "low")   else c.get("low",   0))
            close = float(c.close if hasattr(c, "close") else c.get("close", 0))
            if high > 0 and low > 0 and close > 0:
                raw.append((ts, high, low, close))
        raw.sort(key=lambda x: x[0])

        if len(raw) < ADX_PERIOD * 2 + 1:
            log.warning(f"ADX: only {len(raw)} candles, need {ADX_PERIOD * 2 + 1}")
            return None

        trs, pdms, mdms = [], [], []
        for i in range(1, len(raw)):
            _, h, l, _                     = raw[i]
            _, ph, pl, pc                  = raw[i - 1]
            tr  = max(h - l, abs(h - pc), abs(l - pc))
            up  = h - ph
            dn  = pl - l
            trs.append(tr)
            pdms.append(up if (up > dn and up > 0) else 0.0)
            mdms.append(dn if (dn > up and dn > 0) else 0.0)

        n     = ADX_PERIOD
        s_tr  = sum(trs[:n])
        s_pdm = sum(pdms[:n])
        s_mdm = sum(mdms[:n])

        dx_values = []
        for i in range(n, len(trs)):
            s_tr  = s_tr  - s_tr  / n + trs[i]
            s_pdm = s_pdm - s_pdm / n + pdms[i]
            s_mdm = s_mdm - s_mdm / n + mdms[i]
            pdi   = 100.0 * s_pdm / s_tr if s_tr > 0 else 0.0
            mdi   = 100.0 * s_mdm / s_tr if s_tr > 0 else 0.0
            dx    = 100.0 * abs(pdi - mdi) / (pdi + mdi) if (pdi + mdi) > 0 else 0.0
            dx_values.append(dx)

        if len(dx_values) < n:
            return None

        adx = sum(dx_values[:n]) / n
        for dx in dx_values[n:]:
            adx = (adx * (n - 1) + dx) / n

        status = "TRENDING — grid paused" if adx >= ADX_THRESHOLD else "ranging  — grid active"
        log.info(f"ADX({n}): {adx:.1f}  [{status}]")
        return adx

    except Exception as e:
        log.error(f"ADX calculation error: {e} — treating as ranging")
        return None


def calculate_rsi(client: RESTClient) -> float | None:
    """
    RSI(14) from hourly candles — Wilder's smoothing.
    RSI_LOW–RSI_HIGH = neutral zone → ideal for grid trading.
    RSI below RSI_LOW = oversold. RSI above RSI_HIGH = overbought.
    Both extremes trigger a grid pause (risky for bidirectional grid).
    Returns None if insufficient data.
    """
    try:
        lookback = RSI_PERIOD * 3
        end   = int(time.time())
        start = end - (lookback * 3600)
        response = client.get_candles(
            product_id=SYMBOL,
            start=str(start),
            end=str(end),
            granularity="ONE_HOUR",
        )
        candles = response.candles if hasattr(response, "candles") else response.get("candles", [])

        raw = []
        for c in candles:
            ts    = float(c.start if hasattr(c, "start") else c.get("start", 0))
            close = float(c.close if hasattr(c, "close") else c.get("close", 0))
            if close > 0:
                raw.append((ts, close))
        raw.sort(key=lambda x: x[0])
        closes = [r[1] for r in raw]

        if len(closes) < RSI_PERIOD + 1:
            log.warning(f"RSI: not enough candles ({len(closes)})")
            return None

        changes  = [closes[i] - closes[i-1] for i in range(1, len(closes))]
        gains    = [max(c, 0) for c in changes]
        losses   = [abs(min(c, 0)) for c in changes]

        # Seed with first RSI_PERIOD bars
        avg_gain = sum(gains[:RSI_PERIOD])  / RSI_PERIOD
        avg_loss = sum(losses[:RSI_PERIOD]) / RSI_PERIOD

        # Wilder's smoothing
        for i in range(RSI_PERIOD, len(changes)):
            avg_gain = (avg_gain * (RSI_PERIOD - 1) + gains[i])  / RSI_PERIOD
            avg_loss = (avg_loss * (RSI_PERIOD - 1) + losses[i]) / RSI_PERIOD

        rsi = 100.0 if avg_loss == 0 else 100.0 - (100.0 / (1.0 + avg_gain / avg_loss))

        zone = (
            "OVERBOUGHT — grid paused" if rsi > RSI_HIGH else
            "OVERSOLD   — grid paused" if rsi < RSI_LOW  else
            "neutral    — grid active"
        )
        log.info(f"RSI({RSI_PERIOD}): {rsi:.1f}  [{zone}]")
        return rsi

    except Exception as e:
        log.error(f"RSI calculation error: {e}")
        return None


def get_xrpl_health() -> dict:
    """
    XRPL on-chain health check via public JSON-RPC (no extra dependencies).
    Queries server_info for validated ledger transaction rate.

    Returns dict:
      signal: +1 (high activity / bullish), 0 (normal), -1 (sluggish / cautious)
      healthy: True if ledger is recent (< 30s old)
      tx_per_ledger: raw transaction count

    Fails silently — a network error returns neutral signal (0).
    The signal adjusts buy order sizes but never blocks trading entirely.
    """
    if not XRPL_CHECK_ENABLED:
        return {"healthy": True, "signal": 0, "tx_per_ledger": None}

    try:
        payload = json.dumps({"method": "server_info", "params": [{}]}).encode()
        req     = urllib.request.Request(
            XRPL_RPC_URL,
            data=payload,
            headers={"Content-Type": "application/json"},
            method="POST",
        )
        with urllib.request.urlopen(req, timeout=5) as resp:
            data = json.loads(resp.read())

        info             = data.get("result", {}).get("info", {})
        validated_ledger = info.get("validated_ledger", {})
        tx_per_ledger    = validated_ledger.get("txn_count", None)
        age_seconds      = validated_ledger.get("age", 999)
        healthy          = age_seconds < 30

        # XRP ledger closes ~4s; typical 50–200 tx/ledger
        if tx_per_ledger is None:
            signal = 0
        elif tx_per_ledger > 150:
            signal = 1    # high activity → buy more
        elif tx_per_ledger < 30:
            signal = -1   # low activity  → buy less
        else:
            signal = 0

        signal_map = {1: "HIGH/bullish +10%", 0: "normal", -1: "LOW/cautious -20%"}
        log.info(
            f"XRPL: {'OK' if healthy else 'STALE'}  "
            f"tx/ledger={tx_per_ledger}  age={age_seconds}s  "
            f"signal={signal_map[signal]}"
        )
        return {"healthy": healthy, "signal": signal, "tx_per_ledger": tx_per_ledger}

    except Exception as e:
        log.warning(f"XRPL health check failed: {e} — neutral signal")
        return {"healthy": True, "signal": 0, "tx_per_ledger": None}


# ── Avellaneda-Stoikov engine ──────────────────────────────────────────────────
def calculate_as_quotes(
    mid_price: float,
    daily_vol: float,
    inventory_ratio: float,
) -> tuple[float, float]:
    """
    Avellaneda-Stoikov (2008) institutional market-making model.

    RESERVATION PRICE — shifts grid center based on inventory:
      r = mid − q · γ · σ² · (T−t)
      q ∈ [−1, +1]:  positive = too much XRP → shift DOWN (sell more)
                     negative = too much USDC → shift UP  (buy more)

    OPTIMAL SPREAD — sets step size from vol + liquidity:
      δ = γ · σ² · (T−t)  +  (2/γ) · ln(1 + γ/κ)
      γ = risk aversion (how hard we push back on inventory imbalance)
      κ = order arrival rate (higher = more liquid = tighter spread)

    Returns (reservation_price, spread_pct_of_mid).
    Step size = spread_pct / 2 (spread is bid+ask total).
    """
    # Normalize inventory → q ∈ [−1, +1]
    q = (inventory_ratio - INVENTORY_TARGET) * 2
    q = max(-1.0, min(1.0, q))

    sigma_sq = daily_vol ** 2

    # Reservation price
    r = mid_price - q * AS_GAMMA * sigma_sq * AS_T_HORIZON
    r = max(r, PRICE_FLOOR * 1.02) if PRICE_FLOOR > 0 else r  # floor guard

    # Optimal spread
    inventory_term = AS_GAMMA * sigma_sq * AS_T_HORIZON
    liquidity_term = (2.0 / AS_GAMMA) * math.log(1.0 + AS_GAMMA / AS_KAPPA) if AS_KAPPA > 0 and AS_GAMMA > 0 else 0.01
    delta          = inventory_term + liquidity_term

    spread_pct = (delta / mid_price) * 100

    log.info(
        f"A-S: mid=${mid_price:.5f}  q={q:+.3f}  "
        f"reservation=${r:.5f}  spread={spread_pct:.2f}%"
    )
    return r, spread_pct


# ── Portfolio helpers ──────────────────────────────────────────────────────────
def get_balances(client: RESTClient) -> tuple[float, float]:
    """Returns (usdc_available, xrp_available) — available balance only."""
    response = client.get_accounts()
    accounts = response.accounts if hasattr(response, "accounts") else response.get("accounts", [])
    usdc = xrp = 0.0
    for acct in accounts:
        currency = acct.currency if hasattr(acct, "currency") else acct.get("currency", "")
        bal      = acct.available_balance if hasattr(acct, "available_balance") else acct.get("available_balance", {})
        value    = float(bal.value if hasattr(bal, "value") else bal.get("value", 0))
        if currency == "USDC":
            usdc += value
        elif currency == "XRP":
            xrp  += value
    return usdc, xrp


def get_portfolio_value(client: RESTClient, price: float) -> float:
    """Total portfolio in USDC (available + held in open orders)."""
    response = client.get_accounts()
    accounts = response.accounts if hasattr(response, "accounts") else response.get("accounts", [])
    usdc = xrp = 0.0
    for acct in accounts:
        currency  = acct.currency if hasattr(acct, "currency") else acct.get("currency", "")
        avail_bal = acct.available_balance if hasattr(acct, "available_balance") else acct.get("available_balance", {})
        hold_bal  = acct.hold if hasattr(acct, "hold") else acct.get("hold", {})
        avail     = float(avail_bal.value if hasattr(avail_bal, "value") else avail_bal.get("value", 0))
        hold      = float(hold_bal.value  if hasattr(hold_bal,  "value") else hold_bal.get("value",  0))
        total     = avail + hold
        if currency == "USDC":
            usdc += total
        elif currency == "XRP":
            xrp  += total
    return usdc + (xrp * price)


# ── Counter-order protection ───────────────────────────────────────────────────
# Tracks order IDs placed as counter-orders by handle_filled_orders.
# Protected from cancellation during grid refreshes for GRID_REFRESH_INTERVAL*2
# seconds — giving the second leg of each trade a full cycle to fill.
# After expiry they are cleaned up automatically on the next refresh.
_counter_order_ids: dict = {}   # {order_id: placed_at_timestamp}


def _active_counter_ids() -> set:
    """Return counter-order IDs still within their protection window.
    Protected for exactly one refresh cycle — enough to survive the next
    cancel_all without being wiped, but not so long capital gets locked up."""
    cutoff = time.time() - GRID_REFRESH_INTERVAL
    return {oid for oid, ts in _counter_order_ids.items() if ts > cutoff}


def _cleanup_counter_ids():
    """Prune expired counter-order IDs from tracking dict."""
    cutoff = time.time() - GRID_REFRESH_INTERVAL
    expired = [oid for oid, ts in list(_counter_order_ids.items()) if ts <= cutoff]
    for oid in expired:
        _counter_order_ids.pop(oid, None)
    if expired:
        log.info(f"Counter-order tracker: expired {len(expired)} old ID(s)")


def cancel_all_open_orders(client: RESTClient, protect_ids: set = None):
    try:
        response = client.list_orders(product_id=SYMBOL, order_status="OPEN")
        orders   = response.orders if hasattr(response, "orders") else response.get("orders", [])
        ids = [
            (o.order_id if hasattr(o, "order_id") else o.get("order_id"))
            for o in orders
        ]
        ids = [i for i in ids if i]

        if protect_ids:
            protected = [i for i in ids if i in protect_ids]
            ids       = [i for i in ids if i not in protect_ids]
            if protected:
                log.info(f"Keeping {len(protected)} counter-order(s) alive — skipping cancel")

        if ids:
            client.cancel_orders(order_ids=ids)
            log.info(f"Cancelled {len(ids)} open orders")
    except Exception as e:
        log.error(f"Cancel orders error: {e}")


# ── Grid builder ───────────────────────────────────────────────────────────────
def build_grid(
    center_price: float,
    daily_vol: float = None,
    inventory_ratio: float = 0.5,
):
    """
    Builds a geometric grid using the Avellaneda-Stoikov engine.

    Flow:
      1. A-S reservation price → shifts grid center from VWAP
         based on how imbalanced our inventory is.
      2. A-S optimal spread → sets step size
         based on current volatility and market liquidity.
      3. Fee-aware minimum step → guarantees every trade is profitable.
      4. Carver vol targeting → sets USDC per slot.
      5. Geometric levels built around reservation price.

    Returns (levels, step_pct, low, high, usdc_per_slot).
    """
    if center_price <= 0:
        raise ValueError(f"Invalid center price: {center_price}")

    # Dynamic level sizing: ~$10 USDC per slot, clamped to 6–12 levels
    resolved_levels = GRID_LEVELS or max(6, min(12, int(TOTAL_CAPITAL / 10)))
    half = resolved_levels // 2

    if daily_vol and daily_vol > 0:
        reservation_price, spread_pct = calculate_as_quotes(
            mid_price=center_price,
            daily_vol=daily_vol,
            inventory_ratio=inventory_ratio,
        )
        step_pct = spread_pct / 2   # total spread = 2 × step
    else:
        reservation_price = center_price
        step_pct          = GRID_RANGE_PCT / half

    # Enforce fee-aware profitability floor (1.5× safety margin over round-trip fee)
    min_profitable = COINBASE_FEE * 2 * 100 * 1.5
    step_pct = max(step_pct, min_profitable, MIN_STEP_PCT)
    step_pct = min(step_pct, MAX_STEP_PCT)

    # Geometric levels around reservation price
    multiplier = 1 + step_pct / 100
    levels = sorted(set([
        round(reservation_price * (multiplier ** i), 4)
        for i in range(-half, half + 1)
    ]))
    low  = levels[0]
    high = levels[-1]

    # Carver position sizing
    if daily_vol and daily_vol > 0:
        daily_vol_safe = max(daily_vol, 0.001)
        vol_scalar     = VOL_TARGET / (daily_vol_safe * math.sqrt(365) * math.sqrt(resolved_levels))
        usdc_per       = round(min(TOTAL_CAPITAL / resolved_levels, TOTAL_CAPITAL * vol_scalar), 2)
        usdc_per       = max(usdc_per, MIN_ORDER_USDC)
    else:
        usdc_per = round(TOTAL_CAPITAL / resolved_levels, 2)

    net_profit_pct = step_pct - (COINBASE_FEE * 2 * 100)
    log.info(
        f"Grid — VWAP/mid=${center_price:.5f}  reservation=${reservation_price:.5f}  "
        f"step={step_pct:.2f}%  net/trade={net_profit_pct:.2f}%  "
        f"range=${low:.4f}–${high:.4f}  usdc/slot=${usdc_per:.2f}"
    )
    return levels, step_pct, low, high, usdc_per


def needs_recenter(price: float, grid_low: float, grid_high: float) -> bool:
    """True if price has drifted past the recenter threshold."""
    if grid_low <= 0 or grid_high <= 0:
        return True
    grid_range = grid_high - grid_low
    lower_edge = grid_low  + grid_range * (1 - RECENTER_THRESHOLD)
    upper_edge = grid_high - grid_range * (1 - RECENTER_THRESHOLD)
    return price < lower_edge or price > upper_edge


# ── Order placement ────────────────────────────────────────────────────────────
def _extract_order_id(response) -> str | None:
    if hasattr(response, "success_response"):
        sr = response.success_response
        return sr.order_id if hasattr(sr, "order_id") else None
    if hasattr(response, "order_id"):
        return response.order_id
    if isinstance(response, dict):
        return response.get("order_id") or response.get("success_response", {}).get("order_id")
    return None


def place_buy(
    client: RESTClient,
    price: float,
    usdc: float,
    xrpl_signal: int = 0,
) -> str | None:
    """
    Place a limit buy. Hard price floor enforced — no buy below PRICE_FLOOR.
    XRPL signal adjusts size: +1 → 10% more, -1 → 20% less.
    """
    if PRICE_FLOOR > 0 and price < PRICE_FLOOR:
        log.info(f"BUY blocked @ ${price:.5f} — below floor ${PRICE_FLOOR:.5f}")
        return None

    # XRPL signal size adjustment
    if xrpl_signal == 1:
        usdc = round(usdc * 1.10, 2)
    elif xrpl_signal == -1:
        usdc = round(usdc * 0.80, 2)

    if usdc < MIN_ORDER_USDC:
        return None

    qty = round(usdc / price, 6)
    try:
        o   = client.limit_order_gtc_buy(
            client_order_id=f"grid-buy-{price:.5f}-{int(time.time())}",
            product_id=SYMBOL,
            base_size=str(qty),
            limit_price=str(price),
        )
        oid     = _extract_order_id(o)
        success = getattr(o, "success", None) or (isinstance(o, dict) and o.get("success"))
        if oid or success:
            actual_id = oid or "pending"
            log.info(f"BUY  {qty:.4f} XRP @ ${price:.5f}  [{actual_id}]")
            return oid or actual_id
        else:
            log.error(f"Buy order FAILED @ ${price:.5f}: {o}")
        return None
    except Exception as e:
        log.error(f"Buy failed @ ${price:.5f}: {e}")
        return None


def place_sell(client: RESTClient, price: float, qty: float) -> str | None:
    try:
        o   = client.limit_order_gtc_sell(
            client_order_id=f"grid-sell-{price:.5f}-{int(time.time())}",
            product_id=SYMBOL,
            base_size=str(round(qty, 6)),
            limit_price=str(price),
        )
        oid     = _extract_order_id(o)
        success = getattr(o, "success", None) or (isinstance(o, dict) and o.get("success"))
        if oid or success:
            actual_id = oid or "pending"
            log.info(f"SELL {qty:.4f} XRP @ ${price:.5f}  [{actual_id}]")
            return oid or actual_id
        else:
            log.error(f"Sell order FAILED @ ${price:.5f}: {o}")
        return None
    except Exception as e:
        log.error(f"Sell failed @ ${price:.5f}: {e}")
        return None


# ── Grid initialisation ────────────────────────────────────────────────────────
def initialise_grid(
    client: RESTClient,
    price: float,
    daily_vol: float = None,
    xrpl_signal: int = 0,
):
    """
    Cancel all orders and lay a fresh bidirectional grid.
    Uses VWAP as center (falls back to mid-price).
    Applies A-S model, OB imbalance filter, inventory skew.
    """
    # VWAP as center anchor
    vwap   = get_vwap(client)
    center = vwap if vwap else get_mid_price(client)
    log.info(f"Grid center: VWAP=${center:.5f}  current=${price:.5f}")

    # Get balances for inventory ratio
    usdc_bal, xrp_bal = get_balances(client)
    xrp_value  = xrp_bal * price
    total_val  = usdc_bal + xrp_value
    xrp_ratio  = (xrp_value / total_val) if total_val > 0 else 0.5

    levels, step, low, high, usdc_per = build_grid(
        center_price=center,
        daily_vol=daily_vol,
        inventory_ratio=xrp_ratio,
    )

    cancel_all_open_orders(client, protect_ids=_active_counter_ids())
    _cleanup_counter_ids()
    time.sleep(5)   # wait for Coinbase to release holds before reading balances

    # Re-fetch after cancellation (balances freed from holds)
    usdc_bal, xrp_bal = get_balances(client)
    xrp_value  = xrp_bal * price
    total_val  = usdc_bal + xrp_value
    xrp_ratio  = (xrp_value / total_val) if total_val > 0 else 0.5
    log.info(f"Balances: ${usdc_bal:.2f} USDC  |  {xrp_bal:.4f} XRP (${xrp_value:.2f})")

    # Inventory skew multipliers
    imbalance      = xrp_ratio - INVENTORY_TARGET
    skew           = max(-INVENTORY_SKEW, min(INVENTORY_SKEW, imbalance))
    buy_size_mult  = 1.0 - skew
    sell_size_mult = 1.0 + skew
    log.info(
        f"Inventory: XRP {xrp_ratio*100:.1f}% / USDC {(1-xrp_ratio)*100:.1f}%  "
        f"skew={skew:+.2f}  buy×{buy_size_mult:.2f}  sell×{sell_size_mult:.2f}"
    )

    # Order book imbalance — soft filter: reduce size at moderate pressure, skip only at extreme
    ob_imbalance = get_order_book_imbalance(client)
    if ob_imbalance < -0.7:
        ob_buy_mult = 0.0
        log.warning(f"OB imbalance {ob_imbalance:+.3f} — extreme sell pressure, skipping buys")
    elif ob_imbalance < -OB_IMBALANCE_THRESHOLD:
        ob_buy_mult = 0.5
        log.warning(f"OB imbalance {ob_imbalance:+.3f} — sell pressure, 50% buy size")
    else:
        ob_buy_mult = 1.0

    if ob_imbalance > 0.7:
        ob_sell_mult = 0.0
        log.warning(f"OB imbalance {ob_imbalance:+.3f} — extreme buy pressure, skipping sells")
    elif ob_imbalance > OB_IMBALANCE_THRESHOLD:
        ob_sell_mult = 0.5
        log.warning(f"OB imbalance {ob_imbalance:+.3f} — buy pressure, 50% sell size")
    else:
        ob_sell_mult = 1.0

    buy_levels  = [l for l in levels if l < price]
    sell_levels = [l for l in levels if l > price]

    buy_count = 0
    buy_usdc  = round(usdc_per * buy_size_mult * ob_buy_mult, 2)
    if ob_buy_mult > 0:
        # Place closest levels first — highest buy price = nearest to current price.
        # This ensures the most likely-to-fill orders get funded when capital is tight.
        for lvl in reversed(buy_levels):
            if usdc_bal >= buy_usdc and buy_usdc >= MIN_ORDER_USDC:
                if place_buy(client, lvl, buy_usdc, xrpl_signal=xrpl_signal):
                    usdc_bal -= buy_usdc
                    buy_count += 1

    sell_count = 0
    if xrp_bal > 0 and sell_levels and ob_sell_mult > 0:
        xrp_per_slot = round((xrp_bal / len(sell_levels)) * sell_size_mult * ob_sell_mult, 6)
        xrp_per_slot = min(xrp_per_slot, xrp_bal / len(sell_levels) * 1.5)
        # Place closest sells first too (lowest sell price = nearest to current price)
        for lvl in sell_levels:
            if xrp_per_slot >= 0.0001 and xrp_bal >= xrp_per_slot:
                place_sell(client, lvl, xrp_per_slot)
                xrp_bal -= xrp_per_slot
                sell_count += 1

    net_profit_pct = step - (COINBASE_FEE * 2 * 100)
    log.info(
        f"Grid ready: {buy_count} buys | {sell_count} sells  "
        f"floor=${PRICE_FLOOR:.4f}  OB={ob_imbalance:+.3f}(×{ob_buy_mult:.1f}buy/×{ob_sell_mult:.1f}sell)  XRPL={xrpl_signal:+d}"
    )
    send_email(
        subject="[Grid Bot] Grid (re)built",
        body=(
            f"A-S grid — VWAP=${center:.5f}  price=${price:.5f}\n"
            f"Step      : {step:.2f}%  (net/trade: +{net_profit_pct:.2f}%)\n"
            f"Range     : ${low:.4f} – ${high:.4f}\n"
            f"Floor     : ${PRICE_FLOOR:.4f}\n"
            f"Buys      : {buy_count} @ ${buy_usdc:.2f} USDC each\n"
            f"Sells     : {sell_count}  skew={skew:+.2f}\n"
            f"OB imbal  : {ob_imbalance:+.3f}\n"
            f"XRPL sig  : {xrpl_signal:+d}\n"
            f"XRP ratio : {xrp_ratio*100:.1f}%\n"
            f"Time      : {datetime.now(timezone.utc).isoformat()}"
        ),
    )
    return levels, step, low, high, usdc_per


# ── Scalp mode initialiser ────────────────────────────────────────────────────
def initialise_scalp(client: RESTClient, price: float, xrpl_signal: int = 0):
    """
    Scalp mode — tight SCALP_LEVELS grid centered on current mid-price.
    Used instead of pausing when ADX/RSI conditions are unfavorable.

    No VWAP, no A-S shift — centered directly on current price so the
    grid is always within reach of the market. Fee-safe minimum step.
    Refreshed every SCALP_REFRESH seconds (default 15 min).

    The bot is ALWAYS earning. Even in a downtrend XRP oscillates 0.5–2%
    within each candle — scalp mode catches those bounces.
    """
    mid  = get_mid_price(client)
    step = max(MIN_STEP_PCT, COINBASE_FEE * 2 * 100 * 1.5)
    multiplier = 1 + step / 100
    half = SCALP_LEVELS // 2

    buy_levels  = sorted([round(mid / (multiplier ** i), 4) for i in range(1, half + 1)])
    sell_levels = sorted([round(mid * (multiplier ** i), 4) for i in range(1, half + 1)])
    all_levels  = sorted(buy_levels + sell_levels)
    low  = buy_levels[0]  if buy_levels  else mid
    high = sell_levels[-1] if sell_levels else mid

    cancel_all_open_orders(client, protect_ids=_active_counter_ids())
    _cleanup_counter_ids()
    time.sleep(5)   # wait for Coinbase to release holds before reading balances

    usdc_bal, xrp_bal = get_balances(client)
    xrp_value = xrp_bal * price
    total_val  = usdc_bal + xrp_value
    log.info(
        f"SCALP mode — mid=${mid:.5f}  step={step:.2f}%  "
        f"buys:{[f'${l:.4f}' for l in buy_levels]}  "
        f"sells:{[f'${l:.4f}' for l in sell_levels]}"
    )

    # Size: split capital across scalp levels — never over-commit
    usdc_per = min(usdc_bal / max(half, 1), TOTAL_CAPITAL / SCALP_LEVELS)
    usdc_per = max(round(usdc_per, 2), MIN_ORDER_USDC)

    buy_count = 0
    for lvl in buy_levels:
        if usdc_bal >= usdc_per and usdc_per >= MIN_ORDER_USDC:
            if place_buy(client, lvl, usdc_per, xrpl_signal=xrpl_signal):
                usdc_bal -= usdc_per
                buy_count += 1

    sell_count = 0
    if xrp_bal > 0 and sell_levels:
        xrp_per = round(xrp_bal / len(sell_levels), 6)
        for lvl in sell_levels:
            if xrp_per >= 0.0001 and xrp_bal >= xrp_per:
                place_sell(client, lvl, xrp_per)
                xrp_bal -= xrp_per
                sell_count += 1

    net_pct = step - (COINBASE_FEE * 2 * 100)
    log.info(
        f"SCALP grid ready: {buy_count} buys | {sell_count} sells  "
        f"step={step:.2f}%  net/trade={net_pct:.2f}%  "
        f"range=${low:.4f}–${high:.4f}  XRPL={xrpl_signal:+d}"
    )
    return all_levels, step, low, high, usdc_per


# ── Filled order handler ───────────────────────────────────────────────────────
_handled_order_ids: set = set()
_fills_seeded: bool = False


def seed_handled_orders(client: RESTClient):
    """
    On first boot, mark all existing FILLED orders as already handled.
    Prevents duplicate counter-orders after a redeploy.
    """
    global _fills_seeded
    if _fills_seeded:
        return
    try:
        response = client.list_orders(product_id=SYMBOL, order_status="FILLED")
        orders   = response.orders if hasattr(response, "orders") else response.get("orders", [])
        for order in orders:
            oid = order.order_id if hasattr(order, "order_id") else order.get("order_id", "")
            if oid:
                _handled_order_ids.add(oid)
        log.info(f"Seeded {len(_handled_order_ids)} historical fills — won't re-process them")
    except Exception as e:
        log.error(f"seed_handled_orders error: {e}")
    _fills_seeded = True


def handle_filled_orders(
    client: RESTClient,
    levels: list,
    step_pct: float,
    usdc_per: float,
    xrpl_signal: int = 0,
):
    """
    For each NEW fill, place the counter-order one grid step away.

    Counter-price is calculated arithmetically from fill_price and step_pct —
    NOT from the current levels list index. This makes fill handling correct
    even after a recenter or mode switch that changes the levels list.

      BUY  filled @ P → SELL @ P × (1 + step_pct/100)   [one step up]
      SELL filled @ P → BUY  @ P / (1 + step_pct/100)   [one step down]

    Minimum step enforced so counter-orders are always fee-profitable.
    """
    try:
        response = client.list_orders(product_id=SYMBOL, order_status="FILLED")
        orders   = response.orders if hasattr(response, "orders") else response.get("orders", [])
        for order in orders:
            oid = order.order_id if hasattr(order, "order_id") else order.get("order_id", "")
            if oid in _handled_order_ids:
                continue

            cid        = order.client_order_id if hasattr(order, "client_order_id") else order.get("client_order_id", "")
            side       = order.side if hasattr(order, "side") else order.get("side", "")
            fill_price = float(order.average_filled_price if hasattr(order, "average_filled_price") else order.get("average_filled_price", 0))
            fill_qty   = float(order.filled_size if hasattr(order, "filled_size") else order.get("filled_size", 0))

            if fill_price <= 0 or fill_qty <= 0:
                _handled_order_ids.add(oid)
                continue

            # Use max of current step and fee-safe minimum — always profitable
            effective_step = max(step_pct, MIN_STEP_PCT, COINBASE_FEE * 2 * 100 * 1.5)
            multiplier     = 1 + effective_step / 100

            _handled_order_ids.add(oid)
            _counter_order_ids.pop(oid, None)   # Remove if this was a tracked counter-order

            if side == "BUY" and "grid-buy" in cid:
                sell_price  = round(fill_price * multiplier, 4)
                est_profit  = (sell_price - fill_price) * fill_qty - (fill_price + sell_price) * fill_qty * COINBASE_FEE
                counter_oid = place_sell(client, sell_price, fill_qty)
                if counter_oid:
                    _counter_order_ids[counter_oid] = time.time()   # protect from next refresh
                log.info(
                    f"FILL BUY  {fill_qty:.4f} XRP @ ${fill_price:.5f} → "
                    f"SELL @ ${sell_price:.5f}  est. net profit: +${est_profit:.4f} USDC"
                    + (f"  [counter protected]" if counter_oid else "")
                )

            elif side == "SELL" and "grid-sell" in cid:
                buy_price   = round(fill_price / multiplier, 4)
                est_profit  = (fill_price - buy_price) * fill_qty - (fill_price + buy_price) * fill_qty * COINBASE_FEE
                counter_oid = place_buy(client, buy_price, usdc_per, xrpl_signal=xrpl_signal)
                if counter_oid:
                    _counter_order_ids[counter_oid] = time.time()   # protect from next refresh
                log.info(
                    f"FILL SELL {fill_qty:.4f} XRP @ ${fill_price:.5f} → "
                    f"BUY @ ${buy_price:.5f}  est. net profit: +${est_profit:.4f} USDC"
                    + (f"  [counter protected]" if counter_oid else "")
                )

    except Exception as e:
        log.error(f"handle_filled_orders error: {e}")


# ── Drawdown guard ─────────────────────────────────────────────────────────────
def check_drawdown(client: RESTClient, price: float, start_val: float) -> bool:
    """
    Returns True if trading should continue.
    Returns False if drawdown limit is hit — caller initiates DRAWDOWN_COOLDOWN.
    After cooldown, the main loop automatically restarts.
    """
    if start_val <= 0:
        log.warning("Starting portfolio value is $0 — deposit USDC before running live.")
        return True
    current  = get_portfolio_value(client, price)
    drawdown = (start_val - current) / start_val
    log.info(f"Portfolio: ${current:.2f}  Drawdown: {drawdown*100:.1f}%  Start: ${start_val:.2f}")

    if drawdown >= MAX_DRAWDOWN:
        log.warning(
            f"DRAWDOWN LIMIT — start=${start_val:.2f}  current=${current:.2f}  "
            f"drawdown={drawdown*100:.1f}%  "
            f"pausing {DRAWDOWN_COOLDOWN//3600}h then restarting"
        )
        cancel_all_open_orders(client)
        send_email(
            subject="DRAWDOWN LIMIT — BOT COOLING DOWN",
            body=(
                f"Drawdown {drawdown*100:.1f}% hit {MAX_DRAWDOWN*100:.0f}% limit.\n"
                f"All orders cancelled. Bot pauses {DRAWDOWN_COOLDOWN//3600}h then restarts.\n"
                f"Start: ${start_val:.2f}  Current: ${current:.2f}"
            ),
        )
        return False
    return True


# ── Main ───────────────────────────────────────────────────────────────────────
VOL_RECALC_INTERVAL = 3600   # hourly recalc of vol + ADX + RSI

def main():
    log.info("=" * 65)
    log.info("XRP/USDC Grid Bot — A-S Engine + VWAP + ADX + RSI + XRPL")
    _dyn_levels = GRID_LEVELS or max(6, min(12, int(TOTAL_CAPITAL / 10)))
    log.info(f"Capital: ${TOTAL_CAPITAL}  Grid levels: {_dyn_levels} ({'fixed' if GRID_LEVELS else 'dynamic'})  Scalp levels: {SCALP_LEVELS}  Floor: ${PRICE_FLOOR}")
    log.info(f"ADX≤{ADX_THRESHOLD}  RSI {RSI_LOW}–{RSI_HIGH}  Vol spike: {VOL_SPIKE_MULT}×  VWAP: {VOL_LOOKBACK}h")
    log.info(f"Grid refresh: {GRID_REFRESH_INTERVAL//60}min  Scalp refresh: {SCALP_REFRESH//60}min  A-S γ={AS_GAMMA} κ={AS_KAPPA}")
    log.info(f"Mode: always-on (GRID=ranging, SCALP=trending, pause=vol spike only)")
    log.info("=" * 65)

    client = get_client()
    price  = get_price(client)

    # ── Initial indicators ─────────────────────────────────────────────────────
    daily_vol    = calculate_volatility(client)
    baseline_vol = daily_vol                   # rolling 24h baseline for spike detection
    adx          = calculate_adx(client)
    rsi          = calculate_rsi(client)
    xrpl_health  = get_xrpl_health()
    xrpl_signal  = xrpl_health.get("signal", 0)
    last_vol_ts  = time.time()

    start_val = get_portfolio_value(client, price)
    log.info(f"Starting portfolio: ${start_val:.2f} USDC")

    seed_handled_orders(client)

    # ── State ──────────────────────────────────────────────────────────────────
    scalp_mode           = False   # True = trending market → use tight scalp grid
    vol_spike_paused     = False   # True = vol spike → only true pause
    last_refresh_ts      = time.time()
    drawdown_cooldown_until = 0.0

    # ── Daily P&L tracking ─────────────────────────────────────────────────────
    current_day   = datetime.now(timezone.utc).strftime("%Y-%m-%d")
    day_start_val = start_val

    # Determine initial mode
    adx_bad = adx is not None and adx >= ADX_THRESHOLD
    rsi_bad = rsi is not None and not (RSI_LOW <= rsi <= RSI_HIGH)

    if adx_bad or rsi_bad:
        reasons = []
        if adx_bad: reasons.append(f"ADX={adx:.1f}")
        if rsi_bad: reasons.append(f"RSI={rsi:.1f}")
        log.info(f"Trending market at startup ({', '.join(reasons)}) — starting in SCALP mode")
        scalp_mode = True
        levels, step, grid_low, grid_high, usdc_per = initialise_scalp(
            client, price, xrpl_signal=xrpl_signal
        )
    else:
        levels, step, grid_low, grid_high, usdc_per = initialise_grid(
            client, price, daily_vol, xrpl_signal=xrpl_signal
        )

    send_email(
        subject="[Grid Bot] Started",
        body=(
            f"A-S Grid Bot started.\n"
            f"Price   : ${price:.5f}\n"
            f"Vol/day : {daily_vol*100:.2f}%\n"
            f"ADX({ADX_PERIOD})  : {f'{adx:.1f}' if adx is not None else 'n/a'}  (pause >= {ADX_THRESHOLD})\n"
            f"RSI({RSI_PERIOD})  : {f'{rsi:.1f}' if rsi is not None else 'n/a'}  (pause outside {RSI_LOW}-{RSI_HIGH})\n"
            f"XRPL   : signal={xrpl_signal:+d}\n"
            f"Capital : ${TOTAL_CAPITAL}  Levels: {_dyn_levels}\n"
            f"Floor   : ${PRICE_FLOOR}\n"
            f"Started : {datetime.now(timezone.utc).isoformat()}"
        ),
    )

    # ── Main loop ──────────────────────────────────────────────────────────────
    while True:
        try:
            now   = time.time()
            price = get_price(client)

            # ── Daily P&L rollover ────────────────────────────────────────────
            today = datetime.now(timezone.utc).strftime("%Y-%m-%d")
            if today != current_day:
                eod_val   = get_portfolio_value(client, price)
                daily_pnl = eod_val - day_start_val
                log.info(
                    f"DAILY P&L [{current_day}]: ${daily_pnl:+.4f} USDC  "
                    f"({daily_pnl / day_start_val * 100:+.2f}%)  "
                    f"start=${day_start_val:.2f}  end=${eod_val:.2f}"
                )
                current_day   = today
                day_start_val = eod_val

            # ── Drawdown cooldown ─────────────────────────────────────────────
            if now < drawdown_cooldown_until:
                remaining = int(drawdown_cooldown_until - now)
                log.info(f"Drawdown cooldown: {remaining//60}min remaining — monitoring only")
                time.sleep(min(POLL_INTERVAL * 4, remaining + 1))
                continue

            # ── Hourly recalculation ──────────────────────────────────────────
            if now - last_vol_ts >= VOL_RECALC_INTERVAL:
                new_vol     = calculate_volatility(client)
                adx         = calculate_adx(client)
                rsi         = calculate_rsi(client)
                xrpl_health = get_xrpl_health()
                xrpl_signal = xrpl_health.get("signal", 0)
                last_vol_ts = now

                if new_vol and new_vol > 0:
                    # ── Volatility spike detection ─────────────────────────────
                    if baseline_vol and baseline_vol > 0:
                        spike_ratio = new_vol / baseline_vol
                        if spike_ratio >= VOL_SPIKE_MULT and not vol_spike_paused:
                            vol_spike_paused = True
                            log.warning(
                                f"VOL SPIKE: {new_vol*100:.2f}% vs baseline {baseline_vol*100:.2f}% "
                                f"({spike_ratio:.1f}×) — pausing grid"
                            )
                            cancel_all_open_orders(client)
                            send_email(
                                subject=f"[Grid Bot] PAUSED — Vol spike {spike_ratio:.1f}×",
                                body=f"Vol spiked {spike_ratio:.1f}× above baseline. Grid paused. Resumes when vol < {VOL_SPIKE_RESUME_MULT}× baseline."
                            )
                        elif vol_spike_paused and spike_ratio < VOL_SPIKE_RESUME_MULT:
                            vol_spike_paused = False
                            log.info(f"Vol spike cleared ({spike_ratio:.1f}×) — ready to resume grid")
                        # Faster-decay baseline (60/40 EMA): catches up to real vol quicker
                        # Cuts post-spike lockout time roughly in half vs 80/20
                        baseline_vol = 0.6 * baseline_vol + 0.4 * new_vol
                    daily_vol = new_vol

                # ── ADX / RSI mode transitions ────────────────────────────────
                adx_bad = adx is not None and adx >= ADX_THRESHOLD
                rsi_bad = rsi is not None and not (RSI_LOW <= rsi <= RSI_HIGH)

                if (adx_bad or rsi_bad) and not scalp_mode and not vol_spike_paused:
                    # Market turned trending → switch to scalp mode (don't pause)
                    scalp_mode = True
                    reasons = []
                    if adx_bad: reasons.append(f"ADX={adx:.1f}")
                    if rsi_bad: reasons.append(f"RSI={rsi:.1f}")
                    log.info(f"Trending ({', '.join(reasons)}) → switching to SCALP mode")
                    send_email(
                        subject=f"[Grid Bot] SCALP mode — {', '.join(reasons)}",
                        body=f"Trending market. Switching to tight scalp grid to keep earning."
                    )
                    levels, step, grid_low, grid_high, usdc_per = initialise_scalp(
                        client, price, xrpl_signal=xrpl_signal
                    )
                    last_refresh_ts = now

                elif scalp_mode and not adx_bad and not rsi_bad and not vol_spike_paused:
                    # Market returned to range → switch back to full grid
                    scalp_mode = False
                    adx_s = f"{adx:.1f}" if adx is not None else "n/a"
                    rsi_s = f"{rsi:.1f}" if rsi is not None else "n/a"
                    log.info(f"Ranging (ADX={adx_s} RSI={rsi_s}) → switching to full GRID mode")
                    send_email(subject="[Grid Bot] GRID mode restored", body="Market ranging again. Full A-S grid reactivated.")
                    levels, step, grid_low, grid_high, usdc_per = initialise_grid(
                        client, price, daily_vol, xrpl_signal=xrpl_signal
                    )
                    last_refresh_ts = now

            # ── Only true pause: volatility spike ────────────────────────────
            if vol_spike_paused:
                log.info(f"VOL SPIKE pause — monitoring only")
                time.sleep(POLL_INTERVAL)
                continue
            # scalp_mode = still trading, just with tight grid — no skip

            # ── Drawdown check ────────────────────────────────────────────────
            if not check_drawdown(client, price, start_val):
                drawdown_cooldown_until = now + DRAWDOWN_COOLDOWN
                levels = []
                log.info(f"Drawdown cooldown started — resumes in {DRAWDOWN_COOLDOWN//3600}h")
                continue

            # ── Timed refresh (15 min scalp / 30 min grid) ───────────────────
            current_refresh = SCALP_REFRESH if scalp_mode else GRID_REFRESH_INTERVAL
            mode_label      = "SCALP" if scalp_mode else "GRID"
            if now - last_refresh_ts >= current_refresh:
                log.info(f"{current_refresh//60}-min {mode_label} refresh")
                # Process fills BEFORE cancelling — never lose a fill cycle on refresh
                if levels:
                    handle_filled_orders(client, levels, step, usdc_per, xrpl_signal=xrpl_signal)
                if scalp_mode:
                    levels, step, grid_low, grid_high, usdc_per = initialise_scalp(
                        client, price, xrpl_signal=xrpl_signal
                    )
                else:
                    levels, step, grid_low, grid_high, usdc_per = initialise_grid(
                        client, price, daily_vol, xrpl_signal=xrpl_signal
                    )
                last_refresh_ts = now

            # ── Recenter if price drifted past threshold ──────────────────────
            elif needs_recenter(price, grid_low, grid_high):
                log.info(f"Price ${price:.5f} outside threshold — recentering [{mode_label}]")
                # Process fills BEFORE cancelling — never lose a fill cycle on recenter
                if levels:
                    handle_filled_orders(client, levels, step, usdc_per, xrpl_signal=xrpl_signal)
                if scalp_mode:
                    levels, step, grid_low, grid_high, usdc_per = initialise_scalp(
                        client, price, xrpl_signal=xrpl_signal
                    )
                else:
                    levels, step, grid_low, grid_high, usdc_per = initialise_grid(
                        client, price, daily_vol, xrpl_signal=xrpl_signal
                    )
                last_refresh_ts = now

            # ── Normal fill processing ────────────────────────────────────────
            else:
                if levels:
                    handle_filled_orders(client, levels, step, usdc_per, xrpl_signal=xrpl_signal)

            time.sleep(POLL_INTERVAL)

        except KeyboardInterrupt:
            log.info("Stopped by user.")
            cancel_all_open_orders(client)
            log.info("All orders cancelled.")
            break
        except Exception as e:
            log.error(f"Loop error: {e}")
            time.sleep(POLL_INTERVAL)


if __name__ == "__main__":
    main()
