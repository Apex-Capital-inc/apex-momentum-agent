"""
XRP/USDC Adaptive Regime Agent
=================================
v2.6 — Adaptive RSI thresholds per regime (UPTREND: RSI<38, no vol gate | RANGING: RSI<35, vol>=1.0).

Regimes (auto-detected each scan):
  STRONG_DOWNTREND : EMA20 falling AND price < EMA50
                     → bot goes fully defensive, no new long entries
  RANGING          : mixed signals (neither clear uptrend nor downtrend)
                     → standard mean-reversion active (RSI < 35 triggers)
  UPTREND          : EMA20 rising AND price > EMA50
                     → momentum confirmed, hold longer, wider RSI exit

Strategy:
  BUY  (RANGING / UPTREND only):
       RSI(14) < RSI_BUY  AND  vol_ratio >= VOL_RATIO_MIN
  EXIT : RSI > 65 (default) or RSI > 77 (price > EMA×1.03), 3-loop confirmation
  TRAIL: at +1.5% → stop to breakeven, trails 0.5% below high-water mark
  SL   : -1.6% from entry
  COOL : 30-min cooldown after any stop-loss hit

New in v2.5:
  - Regime detection replaces v2.4 three-gate trend_ok check
  - STRONG_DOWNTREND → bot fully defensive, no longs
  - RANGING → mean-reversion allowed even when price is near/below EMA20
  - Short-ready architecture: ALLOW_SHORTS flag + get_short_signal() stub
  - Enhanced trade diary: adds regime + entry_time_utc to every CSV row
  - All v2.4 protections kept: 20% drawdown cap, startup reconciliation, TP heartbeat
"""

import os, time, logging, uuid, json, math, csv
from datetime import datetime, timezone, timedelta
import pandas as pd
from coinbase.rest import RESTClient

# ── Config ────────────────────────────────────────────────────────────────────
SYMBOL             = os.getenv("SYMBOL", "XRP-USDC")
BASE               = SYMBOL.split("-")[0]           # XRP
QUOTE              = SYMBOL.split("-")[1]           # USDC
CAPITAL            = float(os.getenv("TOTAL_CAPITAL_USDC", "90"))
POSITION_PCT       = float(os.getenv("POSITION_PCT", "0.60"))
TP_PCT             = float(os.getenv("TP_PCT", "2.3"))
SL_PCT             = float(os.getenv("SL_PCT", "1.6"))
TRAIL_TRIGGER      = float(os.getenv("TRAIL_TRIGGER_PCT", "1.5"))
TRAIL_DIST         = float(os.getenv("TRAIL_DIST_PCT", "0.5"))
RSI_BUY            = float(os.getenv("RSI_BUY", "35"))
RSI_EXIT           = float(os.getenv("RSI_EXIT", "65"))
EMA_PERIOD         = int(os.getenv("EMA_PERIOD", "20"))
EMA_SLOW_PERIOD    = int(os.getenv("EMA_SLOW_PERIOD", "50"))
EMA_SLOPE_BARS     = int(os.getenv("EMA_SLOPE_BARS", "5"))
RSI_PERIOD         = int(os.getenv("RSI_PERIOD", "14"))
VOL_RATIO_MIN      = float(os.getenv("VOL_RATIO_MIN", "1.0"))
SL_COOLDOWN_SEC    = int(os.getenv("SL_COOLDOWN_SEC", "1800"))
MAX_DRAWDOWN_PCT   = 20.0   # v2.4: hard-capped — not env-configurable
COINBASE_FEE       = float(os.getenv("COINBASE_FEE", "0.004"))
LOOP_SLEEP         = int(os.getenv("LOOP_SLEEP", "30"))
CANDLES_NEEDED     = max(RSI_PERIOD + 10, EMA_SLOW_PERIOD + 10, 60)
TP_HEARTBEAT_LOOPS = 10
RSI_EXIT_CONFIRM   = 3

# v2.6: regime-adaptive RSI thresholds
RSI_BUY_UPTREND    = float(os.getenv("RSI_BUY_UPTREND", "38"))  # looser in confirmed uptrend, no vol gate
# (RSI_BUY = 35 remains the threshold for RANGING, with vol_ratio gate)

# v2.5: short support flag — architecture ready, not yet active on Coinbase spot
ALLOW_SHORTS       = os.getenv("ALLOW_SHORTS", "false").lower() == "true"

# ── Trade diary ───────────────────────────────────────────────────────────────
TRADE_LOG = os.getenv("TRADE_LOG", "/data/trade_log.csv")

TRADE_LOG_HEADER = [
    "timestamp_utc", "side", "price", "qty", "reason",
    "entry_price", "net_pnl_usd", "net_pnl_pct", "portfolio_after",
    "regime", "entry_time_utc",   # v2.5 additions
]

# ── Logging ───────────────────────────────────────────────────────────────────
logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s  %(levelname)-7s %(message)s",
    datefmt="%Y-%m-%d %H:%M:%S",
)
log = logging.getLogger("momentum")

# ── State file ────────────────────────────────────────────────────────────────
STATE_FILE = os.getenv("STATE_FILE", "/data/bot_state.json")
os.makedirs(os.path.dirname(STATE_FILE), exist_ok=True)

# ── Coinbase client ───────────────────────────────────────────────────────────
_api_secret = (
    os.environ.get("COINBASE_API_SECRET")
    or os.environ.get("COINBASE_PRIVATE_KEY")
    or ""
).replace("\\n", "\n")

client = RESTClient(
    api_key=os.environ["COINBASE_API_KEY"],
    api_secret=_api_secret,
)

# ─────────────────────────────────────────────────────────────────────────────
# State persistence
# ─────────────────────────────────────────────────────────────────────────────

def save_state(*, in_position, entry_price, xrp_position, tp_order_id, tp_price,
               stop_price, trailing_active, high_since_entry,
               sl_cooldown_until, start_val, day_start_val, current_day,
               resumed=False, entry_time_utc="", entry_regime=""):
    """Persist bot state to disk — survives in-container crashes and restarts."""
    state = {
        "in_position":       in_position,
        "entry_price":       entry_price,
        "xrp_position":      xrp_position,
        "tp_order_id":       tp_order_id,
        "tp_price":          tp_price,
        "stop_price":        stop_price,
        "trailing_active":   trailing_active,
        "high_since_entry":  high_since_entry,
        "sl_cooldown_until": (sl_cooldown_until.isoformat()
                              if isinstance(sl_cooldown_until, datetime)
                              else str(sl_cooldown_until)),
        "start_val":         start_val,
        "day_start_val":     day_start_val,
        "current_day":       current_day,
        "resumed":           resumed,
        "entry_time_utc":    entry_time_utc,   # v2.5
        "entry_regime":      entry_regime,      # v2.5
        "saved_at":          time.time(),
    }
    try:
        with open(STATE_FILE, "w") as f:
            json.dump(state, f, indent=2)
    except Exception as e:
        log.warning(f"save_state error: {e}")


def load_state() -> dict | None:
    """Load persisted state from disk. Returns None if missing or corrupt."""
    try:
        if not os.path.exists(STATE_FILE):
            return None
        with open(STATE_FILE) as f:
            s = json.load(f)
        age_min = (time.time() - s.get("saved_at", 0)) / 60
        log.info(
            f"[STATE] Loaded (age={age_min:.1f}min) | "
            f"in_position={s.get('in_position')}  "
            f"xrp={s.get('xrp_position', 0):.4f}  "
            f"entry=${s.get('entry_price', 0):.4f}  "
            f"tp_oid={str(s.get('tp_order_id', ''))[:12]}…  "
            f"regime={s.get('entry_regime', 'unknown')}"
        )
        return s
    except Exception as e:
        log.warning(f"load_state error: {e}")
        return None


def clear_state():
    """Delete state file after a clean position close."""
    try:
        if os.path.exists(STATE_FILE):
            os.remove(STATE_FILE)
            log.info("[STATE] State file cleared")
    except Exception as e:
        log.warning(f"clear_state error: {e}")


# ─────────────────────────────────────────────────────────────────────────────
# Trade diary  (v2.5 — adds regime + entry_time_utc)
# ─────────────────────────────────────────────────────────────────────────────

def log_trade(*, side: str, price: float, qty: float, reason: str,
              entry_price: float = 0.0, net_pnl_usd: float = 0.0,
              portfolio_after: float = 0.0, regime: str = "", entry_time_utc: str = ""):
    """
    Append one row to /data/trade_log.csv.
    side          : 'BUY' or 'SELL'
    reason        : 'ENTRY' | 'TAKE-PROFIT' | 'STOP-LOSS' | 'RSI-EXIT' | 'MAX-DRAWDOWN'
    regime        : 'RANGING' | 'UPTREND' | 'STRONG_DOWNTREND'
    entry_time_utc: ISO UTC timestamp of position entry (meaningful on SELL rows)
    """
    try:
        os.makedirs(os.path.dirname(TRADE_LOG), exist_ok=True)
        file_exists = os.path.exists(TRADE_LOG)
        with open(TRADE_LOG, "a", newline="") as f:
            writer = csv.writer(f)
            if not file_exists:
                writer.writerow(TRADE_LOG_HEADER)
            net_pnl_pct = (net_pnl_usd / (entry_price * qty) * 100) if entry_price and qty else 0.0
            writer.writerow([
                datetime.now(timezone.utc).strftime("%Y-%m-%d %H:%M:%S"),
                side, round(price, 4), round(qty, 4), reason,
                round(entry_price, 4), round(net_pnl_usd, 4),
                round(net_pnl_pct, 2), round(portfolio_after, 2),
                regime, entry_time_utc,
            ])
        log.info(
            f"[DIARY] {side} {qty:.4f} XRP @ ${price:.4f} | "
            f"reason={reason}  regime={regime}  pnl=${net_pnl_usd:+.4f} ({net_pnl_pct:+.2f}%)"
        )
    except Exception as e:
        log.warning(f"log_trade error: {e}")


# ─────────────────────────────────────────────────────────────────────────────
# Candle / indicator helpers
# ─────────────────────────────────────────────────────────────────────────────

def fetch_candles(n: int = 60) -> pd.DataFrame:
    """Fetch last n × 5-min candles, sorted oldest→newest."""
    end   = int(time.time())
    start = end - n * 5 * 60
    try:
        resp    = client.get_candles(
            product_id=SYMBOL, start=str(start), end=str(end), granularity="FIVE_MINUTE"
        )
        candles = resp.candles
        if not candles:
            return pd.DataFrame()
        df = pd.DataFrame([{
            "ts":     int(c.start),
            "open":   float(c.open),
            "high":   float(c.high),
            "low":    float(c.low),
            "close":  float(c.close),
            "volume": float(c.volume),
        } for c in candles])
        df.sort_values("ts", inplace=True)
        df.reset_index(drop=True, inplace=True)
        return df
    except Exception as e:
        log.warning(f"fetch_candles error: {e}")
        return pd.DataFrame()


def compute_rsi(closes: pd.Series, period: int = 14) -> float:
    """Wilder's smoothed RSI."""
    delta = closes.diff().dropna()
    gain  = delta.clip(lower=0)
    loss  = (-delta).clip(lower=0)
    avg_g = gain.ewm(alpha=1 / period, adjust=False).mean()
    avg_l = loss.ewm(alpha=1 / period, adjust=False).mean()
    rs    = avg_g / avg_l.replace(0, 1e-10)
    return float((100 - 100 / (1 + rs)).iloc[-1])


def compute_indicators(df: pd.DataFrame) -> dict:
    closes    = df["close"]
    volumes   = df["volume"]
    rsi       = compute_rsi(closes, RSI_PERIOD)

    # EMA20 + slope
    ema_series  = closes.ewm(span=EMA_PERIOD, adjust=False).mean()
    ema         = float(ema_series.iloc[-1])
    slope_idx   = max(0, len(ema_series) - 1 - EMA_SLOPE_BARS)
    ema_prev    = float(ema_series.iloc[slope_idx])

    # EMA50 (regime gate)
    ema50 = float(closes.ewm(span=EMA_SLOW_PERIOD, adjust=False).mean().iloc[-1])

    price     = float(closes.iloc[-1])
    vol_avg   = float(volumes.iloc[-20:].mean()) if len(volumes) >= 20 else float(volumes.mean())
    vol_ratio = float(volumes.iloc[-1]) / vol_avg if vol_avg > 0 else 1.0
    return {
        "rsi": rsi, "ema": ema, "ema_prev": ema_prev,
        "ema50": ema50, "price": price, "vol_ratio": vol_ratio
    }


# ─────────────────────────────────────────────────────────────────────────────
# Regime detection  (v2.5)
# ─────────────────────────────────────────────────────────────────────────────

def detect_regime(ind: dict) -> str:
    """
    Classify current market regime from indicators.

    STRONG_DOWNTREND : EMA20 falling AND price < EMA50
                       → bot goes fully defensive, no new long entries.
                       Both conditions must be true — avoids blocking on
                       temporary price dips while EMA is still rising.

    UPTREND          : EMA20 rising AND price > EMA50
                       → momentum confirmed above medium-term trend.
                       Hold longer, wider RSI exit threshold.

    RANGING          : all other combinations (mixed signals)
                       → standard mean-reversion active.
                       Allows RSI < 35 buys even when price is near/below
                       EMA20, as long as EMA is not in confirmed decline.

    Key improvement over v2.4 trend_ok:
    v2.4 required price > EMA20 AND rising AND price > EMA50 to buy.
    This blocked entries even in RANGING when price dipped below EMA20
    on the 5m chart (the RSI < 35 paradox).
    v2.5 only blocks in a confirmed STRONG_DOWNTREND.
    """
    ema20_rising = ind["ema"] > ind["ema_prev"]
    above_ema50  = ind["price"] > ind["ema50"]

    if not ema20_rising and not above_ema50:
        return "STRONG_DOWNTREND"
    elif ema20_rising and above_ema50:
        return "UPTREND"
    else:
        return "RANGING"


# ─────────────────────────────────────────────────────────────────────────────
# Signal functions  (v2.5 — long/short architecture)
# ─────────────────────────────────────────────────────────────────────────────

def get_long_signal(ind: dict, regime: str) -> str:
    """
    Long entry/exit signal — v2.6 adaptive thresholds per regime.

    STRONG_DOWNTREND : blocked entirely, no longs.
    UPTREND          : RSI < 38, no vol_ratio gate.
                       Confirmed trend makes vol gate redundant — we want
                       to buy momentum pullbacks, not wait for volume spikes.
    RANGING          : RSI < 35 AND vol_ratio >= 1.0 (strict mean-reversion).
                       Tighter entry required — no trend confirmation behind us.

    EXIT : RSI > RSI_EXIT (dynamic threshold + confirmation handled in main loop)
    """
    if regime == "STRONG_DOWNTREND":
        return "HOLD"

    if regime == "UPTREND":
        if ind["rsi"] < RSI_BUY_UPTREND:
            return "BUY"
    else:  # RANGING
        if ind["rsi"] < RSI_BUY and ind["vol_ratio"] >= VOL_RATIO_MIN:
            return "BUY"

    if ind["rsi"] > RSI_EXIT:
        return "EXIT"
    return "HOLD"


def get_short_signal(ind: dict, regime: str) -> str:
    """
    Short entry signal — architecture stub. NOT YET ACTIVE.

    Future implementation:
      - Requires ALLOW_SHORTS=true in env
      - STRONG_DOWNTREND only
      - Entry: RSI > 65 (overbought in downtrend) + vol spike
      - Exit:  RSI < 35 or price recovers above EMA20
      - Needs short execution support (futures/derivatives or margin)

    Will be expanded when exchange support is confirmed.
    """
    return "HOLD"


# ─────────────────────────────────────────────────────────────────────────────
# Market data helpers
# ─────────────────────────────────────────────────────────────────────────────

def get_mid_price() -> float:
    try:
        book = client.get_best_bid_ask(product_ids=[SYMBOL])
        bid  = float(book.pricebooks[0].bids[0].price)
        ask  = float(book.pricebooks[0].asks[0].price)
        return (bid + ask) / 2
    except Exception as e:
        log.warning(f"get_mid_price error: {e}")
        return 0.0


def get_balances() -> tuple[float, float]:
    """Returns (usdc_available, xrp_available)."""
    usdc = xrp = 0.0
    try:
        for acct in client.get_accounts().accounts:
            bal = acct.available_balance
            val = bal["value"] if isinstance(bal, dict) else bal.value
            if acct.currency == QUOTE:
                usdc = float(val)
            elif acct.currency == BASE:
                xrp  = float(val)
    except Exception as e:
        log.warning(f"get_balances error: {e}")
    return usdc, xrp


def _floor4(qty: float) -> float:
    """Floor quantity to 4 decimal places — never sell more than available balance."""
    return math.floor(qty * 10000) / 10000


def _safe_get(obj, key, default=None):
    """Get field from either dict or object."""
    if isinstance(obj, dict):
        return obj.get(key, default)
    return getattr(obj, key, default)


def get_portfolio_value(price: float) -> float:
    """Total portfolio in USDC — includes XRP locked in open sell orders."""
    usdc, xrp = get_balances()
    try:
        resp   = client.list_orders(product_id=SYMBOL, order_status=["OPEN"])
        orders = _safe_get(resp, "orders") or []
        for o in orders:
            if _safe_get(o, "side") == "SELL":
                sz = _safe_get(o, "base_size") or _safe_get(o, "leaves_quantity")
                if sz:
                    if isinstance(sz, dict):
                        sz = sz.get("value", 0)
                    try:
                        xrp += float(sz)
                    except (ValueError, TypeError):
                        pass
    except Exception:
        pass
    return usdc + xrp * price


# ─────────────────────────────────────────────────────────────────────────────
# Order helpers
# ─────────────────────────────────────────────────────────────────────────────

def _extract_order_id(resp) -> str:
    """Extract order_id from SDK response — handles dict and object at every level."""
    if isinstance(resp, dict):
        return (resp.get("order_id")
                or resp.get("success_response", {}).get("order_id", "")
                or "")
    sr = getattr(resp, "success_response", None)
    if sr is not None:
        oid = sr.get("order_id") if isinstance(sr, dict) else getattr(sr, "order_id", None)
        if oid:
            return oid
    return getattr(resp, "order_id", "") or ""


def place_limit_buy(usdc_amount: float, price: float) -> tuple[str, float, float]:
    """Aggressive limit buy slightly above mid. Returns (order_id, limit_price, xrp_qty)."""
    buy_price = round(price * 1.0005, 4)
    xrp_qty   = round((usdc_amount / buy_price) * (1 - COINBASE_FEE), 4)
    if xrp_qty < 1.0:
        log.warning(f"Buy qty too small ({xrp_qty} XRP) — skipping")
        return "", 0.0, 0.0
    cid = f"mom-buy-{uuid.uuid4().hex[:8]}"
    try:
        resp = client.limit_order_gtc_buy(
            client_order_id=cid, product_id=SYMBOL,
            base_size=str(xrp_qty), limit_price=str(buy_price),
        )
        oid = _extract_order_id(resp)
        log.info(f"BUY  placed: {xrp_qty} XRP @ ${buy_price} | id={oid}")
        return oid, buy_price, xrp_qty
    except Exception as e:
        log.error(f"place_limit_buy error: {e}")
        return "", 0.0, 0.0


def place_limit_sell_tp(xrp_qty: float, tp_price: float) -> str:
    """Place take-profit limit sell. Returns order_id."""
    cid = f"mom-tp-{uuid.uuid4().hex[:8]}"
    try:
        resp = client.limit_order_gtc_sell(
            client_order_id=cid, product_id=SYMBOL,
            base_size=str(_floor4(xrp_qty)), limit_price=str(round(tp_price, 4)),
        )
        oid = _extract_order_id(resp)
        if oid:
            log.info(f"TP   placed: {xrp_qty} XRP @ ${tp_price:.4f} | id={oid}")
        else:
            log.error(
                f"TP   API call returned but order_id empty. "
                f"resp_type={type(resp).__name__}  "
                f"preview={str(resp)[:200]}"
            )
        return oid
    except Exception as e:
        log.error(f"place_limit_sell_tp error: {e}")
        return ""


def cancel_order(order_id: str) -> None:
    if not order_id:
        return
    try:
        client.cancel_orders(order_ids=[order_id])
        log.info(f"Order cancelled: {order_id}")
    except Exception as e:
        log.warning(f"cancel_order {order_id}: {e}")


def emergency_sell(xrp_qty: float) -> bool:
    """Aggressive limit sell for SL execution — 0.5% below mid to ensure fill."""
    price    = get_mid_price()
    sl_price = round(price * 0.995, 4)
    xrp_qty  = _floor4(xrp_qty)
    cid      = f"mom-sl-{uuid.uuid4().hex[:8]}"
    try:
        resp = client.limit_order_gtc_sell(
            client_order_id=cid, product_id=SYMBOL,
            base_size=str(xrp_qty), limit_price=str(sl_price),
        )
        oid = _extract_order_id(resp)
        log.info(f"SL   sell: {xrp_qty} XRP @ ${sl_price} | id={oid}")
        for _ in range(30):
            time.sleep(1)
            try:
                resp = client.get_order(order_id=oid)
                o    = _safe_get(resp, "order") or resp
                if _safe_get(o, "status") in ("FILLED", "DONE"):
                    log.info("SL fill confirmed ✓")
                    return True
            except Exception:
                pass
        log.warning("SL sell not confirmed in 30s — likely still filling")
        return True
    except Exception as e:
        log.error(f"emergency_sell error: {e}")
        return False


def is_order_filled(order_id: str) -> bool:
    if not order_id:
        return False
    try:
        resp = client.get_order(order_id=order_id)
        o    = _safe_get(resp, "order") or resp
        return _safe_get(o, "status") in ("FILLED", "DONE")
    except Exception:
        return False


def get_fill_price(order_id: str, fallback: float) -> float:
    try:
        resp = client.get_order(order_id=order_id)
        o    = _safe_get(resp, "order") or resp
        avg  = _safe_get(o, "average_filled_price")
        return float(avg) if avg else fallback
    except Exception:
        return fallback


def verify_tp_order(tp_order_id: str, xrp_qty: float, tp_price: float) -> str:
    """
    Heartbeat: verify TP order is still OPEN on Coinbase.
    - If tp_order_id is empty: scan for orphaned open sell, adopt it; else place new TP.
    - If tp_order_id is set: confirm it exists; re-place if missing.
    Returns the current (possibly new) order_id.
    """
    if xrp_qty <= 0:
        return tp_order_id
    try:
        resp   = client.list_orders(product_id=SYMBOL, order_status=["OPEN"])
        orders = _safe_get(resp, "orders") or []

        for o in orders:
            oid = _safe_get(o, "order_id", "")
            if _safe_get(o, "side") == "SELL":
                if tp_order_id and oid == tp_order_id:
                    return tp_order_id
                if not tp_order_id and oid:
                    log.warning(f"[TP-HEARTBEAT] Adopting orphaned sell order: {oid}")
                    return oid

        reason = "no id captured on placement" if not tp_order_id else "order missing from exchange"
        log.warning(f"[TP-HEARTBEAT] No open sell order ({reason}) — placing TP at ${tp_price:.4f}")
        new_oid = place_limit_sell_tp(xrp_qty, tp_price)
        if new_oid:
            log.info(f"[TP-HEARTBEAT] TP placed: {new_oid}")
            return new_oid
        log.error("[TP-HEARTBEAT] Failed to place TP order — XRP unprotected!")
    except Exception as e:
        log.warning(f"verify_tp_order error: {e}")
    return tp_order_id


def resume_position_from_balance(price: float) -> dict | None:
    """
    Safety net for redeploys: no state file but XRP found in wallet.
    Reconstructs position using current price as synthetic entry.
    """
    _, xrp_bal = get_balances()
    if xrp_bal * price < 5.0:
        return None

    tp_price_r   = round(price * (1 + TP_PCT / 100), 4)
    stop_price_r = round(price * (1 - SL_PCT / 100), 4)

    existing_tp_oid   = ""
    existing_tp_price = 0.0
    try:
        resp   = client.list_orders(product_id=SYMBOL, order_status=["OPEN"])
        orders = _safe_get(resp, "orders") or []
        for o in orders:
            if _safe_get(o, "side") == "SELL":
                existing_tp_oid = _safe_get(o, "order_id", "")
                cfg = _safe_get(o, "order_configuration") or {}
                lgc = (cfg.get("limit_limit_gtc") if isinstance(cfg, dict)
                       else getattr(cfg, "limit_limit_gtc", None)) or {}
                lp  = (lgc.get("limit_price") if isinstance(lgc, dict)
                       else getattr(lgc, "limit_price", None))
                existing_tp_price = float(lp) if lp else 0.0
                log.info(
                    f"[RESUME] Found existing sell order {existing_tp_oid[:12]}… "
                    f"@ ${existing_tp_price:.4f} — adopting it"
                )
                break
    except Exception as e:
        log.warning(f"[RESUME] Could not scan existing orders: {e}")

    tp_price_r = existing_tp_price if existing_tp_price > 0 else round(price * (1 + TP_PCT / 100), 4)

    log.warning("=" * 60)
    log.warning(f"[RESUME] No state file — {xrp_bal:.4f} XRP @ ${price:.4f} detected")
    log.warning(f"[RESUME] Synthetic entry: SL=${stop_price_r:.4f}  TP=${tp_price_r:.4f}")
    log.warning("[RESUME] Risk/reward based on current price, NOT original entry.")
    log.warning("=" * 60)

    if existing_tp_oid:
        tp_oid = existing_tp_oid
    else:
        tp_oid = place_limit_sell_tp(_floor4(xrp_bal), tp_price_r)
        if not tp_oid:
            log.error("[RESUME] Could not place TP order — XRP is unprotected!")

    return {
        "in_position":      True,
        "entry_price":      price,
        "xrp_position":     xrp_bal,
        "tp_order_id":      tp_oid,
        "tp_price":         tp_price_r,
        "stop_price":       stop_price_r,
        "trailing_active":  False,
        "high_since_entry": price,
        "resumed":          True,
        "entry_time_utc":   datetime.now(timezone.utc).isoformat(),
        "entry_regime":     "UNKNOWN",
    }


# ─────────────────────────────────────────────────────────────────────────────
# Main
# ─────────────────────────────────────────────────────────────────────────────

def main():
    log.info("=" * 70)
    log.info("XRP/USDC Adaptive Regime Agent — v2.6")
    log.info(f"TP=+{TP_PCT}%  SL=-{SL_PCT}%  RSI_BUY<{RSI_BUY}  RSI_EXIT>{RSI_EXIT}")
    log.info(f"EMA({EMA_PERIOD}) slope/{EMA_SLOPE_BARS}bars  EMA({EMA_SLOW_PERIOD}) regime gate")
    log.info(f"RSI({RSI_PERIOD})  Position={POSITION_PCT*100:.0f}%  Cooldown={SL_COOLDOWN_SEC//60}min")
    log.info(f"Regimes: STRONG_DOWNTREND=block | RANGING=RSI<{RSI_BUY}+vol | UPTREND=RSI<{RSI_BUY_UPTREND} no-vol-gate")
    log.info(f"Shorts: {'ENABLED (stub — not active)' if ALLOW_SHORTS else 'disabled'}")
    log.info(f"MaxDD={MAX_DRAWDOWN_PCT}% (hard)  Capital=${CAPITAL}")
    log.info(f"Trade diary: {TRADE_LOG}")
    log.info("=" * 70)

    # ── Default state ─────────────────────────────────────────────────────────
    in_position       = False
    entry_price       = 0.0
    xrp_position      = 0.0
    tp_order_id       = ""
    tp_price          = 0.0
    stop_price        = 0.0
    trailing_active   = False
    high_since_entry  = 0.0
    sl_cooldown_until = datetime.now(timezone.utc)
    resumed           = False
    loop_count        = 0
    rsi_exit_count    = 0
    entry_time_utc    = ""    # v2.5: when position was opened
    entry_regime      = ""    # v2.5: regime at entry

    # ── Startup price + portfolio ─────────────────────────────────────────────
    price     = get_mid_price()
    start_val = get_portfolio_value(price)
    for _attempt in range(6):
        if start_val > 0:
            break
        log.warning("Portfolio returned $0 — retrying in 10s…")
        time.sleep(10)
        price     = get_mid_price()
        start_val = get_portfolio_value(price)

    current_day   = datetime.now(timezone.utc).strftime("%Y-%m-%d")
    day_start_val = start_val

    # ── Load persisted state ──────────────────────────────────────────────────
    saved = load_state()
    if saved:
        in_position      = saved.get("in_position", False)
        entry_price      = float(saved.get("entry_price", 0.0))
        xrp_position     = float(saved.get("xrp_position", 0.0))
        tp_order_id      = saved.get("tp_order_id", "")
        tp_price         = float(saved.get("tp_price", 0.0))
        stop_price       = float(saved.get("stop_price", 0.0))
        trailing_active  = bool(saved.get("trailing_active", False))
        high_since_entry = float(saved.get("high_since_entry", 0.0))
        start_val        = float(saved.get("start_val", start_val))
        day_start_val    = float(saved.get("day_start_val", day_start_val))
        current_day      = saved.get("current_day", current_day)
        resumed          = bool(saved.get("resumed", False))
        entry_time_utc   = saved.get("entry_time_utc", "")    # v2.5
        entry_regime     = saved.get("entry_regime", "")      # v2.5
        try:
            sl_cooldown_until = datetime.fromisoformat(saved["sl_cooldown_until"])
        except Exception:
            sl_cooldown_until = datetime.now(timezone.utc)

        # Sanity check: state says in_position but XRP is gone
        if in_position:
            _, xrp_bal = get_balances()
            xrp_in_orders = 0.0
            try:
                resp   = client.list_orders(product_id=SYMBOL, order_status=["OPEN"])
                orders = _safe_get(resp, "orders") or []
                for o in orders:
                    if _safe_get(o, "side") == "SELL":
                        sz = _safe_get(o, "base_size") or _safe_get(o, "leaves_quantity")
                        if sz:
                            try:
                                xrp_in_orders += float(
                                    sz if not isinstance(sz, dict) else sz.get("value", 0)
                                )
                            except (ValueError, TypeError):
                                pass
            except Exception as e:
                log.warning(f"[STATE] Could not scan open orders for sanity check: {e}")

            total_xrp = xrp_bal + xrp_in_orders
            if total_xrp * price < 5.0:
                log.warning(
                    "[STATE] State says in_position=True but no XRP found "
                    "(wallet + open orders) — position closed while offline. Resetting."
                )
                in_position = False
                xrp_position = 0.0
                entry_price = tp_price = stop_price = high_since_entry = 0.0
                tp_order_id = ""
                trailing_active = False
                entry_time_utc = ""
                entry_regime = ""
                clear_state()
            else:
                log.info(
                    f"[STATE] Resumed position: {xrp_position:.4f} XRP "
                    f"entry=${entry_price:.4f}  TP=${tp_price:.4f}  SL=${stop_price:.4f}  "
                    f"regime={entry_regime}"
                    + ("  [SYNTHETIC ENTRY]" if resumed else "")
                )
                # Reconcile TP order against Coinbase reality
                if tp_order_id:
                    try:
                        if is_order_filled(tp_order_id):
                            actual_tp = get_fill_price(tp_order_id, tp_price)
                            net_pnl   = (
                                (actual_tp - entry_price) * xrp_position
                                - (entry_price + actual_tp) * xrp_position * COINBASE_FEE
                            )
                            log.info(
                                f"[RECONCILE] ✅ TP filled while offline @ ${actual_tp:.4f} "
                                f"| net P&L≈${net_pnl:+.2f} — closing position"
                            )
                            log_trade(
                                side="SELL", price=actual_tp, qty=xrp_position,
                                reason="TAKE-PROFIT", entry_price=entry_price,
                                net_pnl_usd=net_pnl,
                                portfolio_after=get_portfolio_value(actual_tp),
                                regime=entry_regime, entry_time_utc=entry_time_utc,
                            )
                            in_position = False; xrp_position = 0.0
                            tp_order_id = ""; tp_price = 0.0; stop_price = 0.0
                            trailing_active = False; high_since_entry = 0.0
                            resumed = False; entry_time_utc = ""; entry_regime = ""
                            clear_state()
                        else:
                            resp   = client.get_order(order_id=tp_order_id)
                            o      = _safe_get(resp, "order") or resp
                            status = _safe_get(o, "status", "OPEN")
                            if status not in ("OPEN", "PENDING", "QUEUED"):
                                log.warning(
                                    f"[RECONCILE] TP order status={status} "
                                    f"— re-placing TP at ${tp_price:.4f}"
                                )
                                tp_order_id = place_limit_sell_tp(
                                    _floor4(xrp_position), tp_price
                                )
                            else:
                                log.info("[RECONCILE] TP order confirmed OPEN on Coinbase ✓")
                    except Exception as e:
                        log.warning(f"[RECONCILE] TP check error: {e}")

    elif price > 0:
        # No state file — check for orphaned XRP
        resumed_data = resume_position_from_balance(price)
        if resumed_data:
            in_position      = resumed_data["in_position"]
            entry_price      = resumed_data["entry_price"]
            xrp_position     = resumed_data["xrp_position"]
            tp_order_id      = resumed_data["tp_order_id"]
            tp_price         = resumed_data["tp_price"]
            stop_price       = resumed_data["stop_price"]
            trailing_active  = resumed_data["trailing_active"]
            high_since_entry = resumed_data["high_since_entry"]
            resumed          = resumed_data["resumed"]
            entry_time_utc   = resumed_data.get("entry_time_utc", "")
            entry_regime     = resumed_data.get("entry_regime", "UNKNOWN")
            save_state(
                in_position=in_position, entry_price=entry_price,
                xrp_position=xrp_position, tp_order_id=tp_order_id,
                tp_price=tp_price, stop_price=stop_price,
                trailing_active=trailing_active, high_since_entry=high_since_entry,
                sl_cooldown_until=sl_cooldown_until, start_val=start_val,
                day_start_val=day_start_val, current_day=current_day, resumed=resumed,
                entry_time_utc=entry_time_utc, entry_regime=entry_regime,
            )

    log.info(f"Starting portfolio : ${start_val:.2f}")
    log.info(f"Hard stop at       : ${start_val * (1 - MAX_DRAWDOWN_PCT/100):.2f}  "
             f"(drawdown {MAX_DRAWDOWN_PCT}%)")

    # ─────────────────────────────────────────────────────────────────────────
    # Main loop
    # ─────────────────────────────────────────────────────────────────────────
    while True:
        try:
            now        = datetime.now(timezone.utc)
            price      = get_mid_price()
            loop_count += 1

            if price <= 0:
                time.sleep(LOOP_SLEEP)
                continue

            # ── Daily P&L rollover ────────────────────────────────────────────
            today = now.strftime("%Y-%m-%d")
            if today != current_day:
                eod_val   = get_portfolio_value(price)
                daily_pnl = eod_val - day_start_val
                pct       = (daily_pnl / day_start_val * 100) if day_start_val > 0 else 0.0
                log.info(
                    f"DAILY P&L [{current_day}]: ${daily_pnl:+.4f} ({pct:+.2f}%)  "
                    f"start=${day_start_val:.2f}  end=${eod_val:.2f}"
                )
                current_day   = today
                day_start_val = eod_val

            # ── Max drawdown guard ────────────────────────────────────────────
            if in_position and xrp_position > 0:
                usdc_free, _ = get_balances()
                port_val = usdc_free + xrp_position * price
            else:
                port_val = get_portfolio_value(price)
            drawdown = (start_val - port_val) / start_val * 100 if start_val > 0 else 0.0

            if drawdown >= MAX_DRAWDOWN_PCT:
                log.error(f"MAX DRAWDOWN {drawdown:.1f}% hit — halting agent")
                if tp_order_id:
                    cancel_order(tp_order_id)
                if in_position and xrp_position > 0:
                    emergency_sell(xrp_position)
                    net_pnl = (
                        (price - entry_price) * xrp_position
                        - (entry_price + price) * xrp_position * COINBASE_FEE
                    )
                    log_trade(
                        side="SELL", price=price, qty=xrp_position,
                        reason="MAX-DRAWDOWN", entry_price=entry_price,
                        net_pnl_usd=net_pnl, portfolio_after=port_val + net_pnl,
                        regime=entry_regime, entry_time_utc=entry_time_utc,
                    )
                clear_state()
                log.error("Agent halted. Restart manually after reviewing conditions.")
                break

            # ── Heartbeat log ─────────────────────────────────────────────────
            pos_label = "NONE"
            if in_position:
                pos_label = f"LONG/{entry_regime}" + (" [RESUMED]" if resumed else "")
            log.info(
                f"Portfolio=${port_val:.2f}  price=${price:.4f}  "
                f"position={pos_label}  drawdown={drawdown:.2f}%"
            )

            # ═════════════════════════════════════════════════════════════════
            # IN POSITION — manage open trade
            # ═════════════════════════════════════════════════════════════════
            if in_position:
                pnl_pct          = (price - entry_price) / entry_price * 100
                high_since_entry = max(high_since_entry, price)

                # ── TP heartbeat ──────────────────────────────────────────────
                if loop_count % TP_HEARTBEAT_LOOPS == 0:
                    new_oid = verify_tp_order(tp_order_id, xrp_position, tp_price)
                    if new_oid != tp_order_id:
                        tp_order_id = new_oid
                        save_state(
                            in_position=in_position, entry_price=entry_price,
                            xrp_position=xrp_position, tp_order_id=tp_order_id,
                            tp_price=tp_price, stop_price=stop_price,
                            trailing_active=trailing_active, high_since_entry=high_since_entry,
                            sl_cooldown_until=sl_cooldown_until, start_val=start_val,
                            day_start_val=day_start_val, current_day=current_day,
                            resumed=resumed, entry_time_utc=entry_time_utc,
                            entry_regime=entry_regime,
                        )

                # ── Check if TP limit order filled on exchange ────────────────
                if tp_order_id and is_order_filled(tp_order_id):
                    actual_tp = get_fill_price(tp_order_id, tp_price)
                    net_pnl   = (
                        (actual_tp - entry_price) * xrp_position
                        - (entry_price + actual_tp) * xrp_position * COINBASE_FEE
                    )
                    log.info(
                        f"✅ TAKE PROFIT @ ${actual_tp:.4f} | "
                        f"entry=${entry_price:.4f} | net P&L≈${net_pnl:+.2f} | "
                        f"regime={entry_regime}"
                    )
                    log_trade(
                        side="SELL", price=actual_tp, qty=xrp_position,
                        reason="TAKE-PROFIT", entry_price=entry_price,
                        net_pnl_usd=net_pnl,
                        portfolio_after=get_portfolio_value(actual_tp),
                        regime=entry_regime, entry_time_utc=entry_time_utc,
                    )
                    in_position = False; xrp_position = 0.0
                    tp_order_id = ""; tp_price = 0.0; stop_price = 0.0
                    trailing_active = False; high_since_entry = 0.0
                    resumed = False; entry_time_utc = ""; entry_regime = ""
                    clear_state()
                    time.sleep(LOOP_SLEEP)
                    continue

                # ── Update trailing stop ──────────────────────────────────────
                if pnl_pct >= TRAIL_TRIGGER:
                    trailing_active = True
                    breakeven       = entry_price * (1 + COINBASE_FEE * 2 * 1.2)
                    trail_stop      = high_since_entry * (1 - TRAIL_DIST / 100)
                    new_stop        = max(trail_stop, breakeven)
                    if new_stop > stop_price:
                        stop_price = new_stop
                        log.info(
                            f"Trail updated → ${stop_price:.4f}  "
                            f"(pnl={pnl_pct:+.2f}%  high=${high_since_entry:.4f})"
                        )
                        save_state(
                            in_position=in_position, entry_price=entry_price,
                            xrp_position=xrp_position, tp_order_id=tp_order_id,
                            tp_price=tp_price, stop_price=stop_price,
                            trailing_active=trailing_active, high_since_entry=high_since_entry,
                            sl_cooldown_until=sl_cooldown_until, start_val=start_val,
                            day_start_val=day_start_val, current_day=current_day,
                            resumed=resumed, entry_time_utc=entry_time_utc,
                            entry_regime=entry_regime,
                        )

                # ── RSI exit signal (dynamic threshold + confirmation) ────────
                exit_by_rsi = False
                try:
                    df = fetch_candles(CANDLES_NEEDED)
                    if not df.empty:
                        ind = compute_indicators(df)
                        rsi_exit_threshold = (RSI_EXIT if ind["price"] < ind["ema"] * 1.03
                                              else RSI_EXIT + 12)
                        if ind["rsi"] > rsi_exit_threshold:
                            rsi_exit_count += 1
                        else:
                            rsi_exit_count = 0
                        exit_by_rsi = rsi_exit_count >= RSI_EXIT_CONFIRM
                        log.info(
                            f"[LONG/{entry_regime}] pnl={pnl_pct:+.2f}%  RSI={ind['rsi']:.1f}  "
                            f"threshold={rsi_exit_threshold:.0f}  "
                            f"confirm={rsi_exit_count}/{RSI_EXIT_CONFIRM}  "
                            f"stop=${stop_price:.4f}  trail={'ON' if trailing_active else 'OFF'}"
                        )
                except Exception as e:
                    log.warning(f"Indicator error in position: {e}")

                # ── Trigger exit (SL or RSI overbought) ──────────────────────
                hit_sl = stop_price > 0 and price <= stop_price
                if hit_sl or exit_by_rsi:
                    reason = "STOP-LOSS" if hit_sl else "RSI OVERBOUGHT EXIT"
                    log.info(
                        f"🛑 {reason} | price=${price:.4f}  "
                        f"entry=${entry_price:.4f}  pnl={pnl_pct:+.2f}%"
                    )
                    cancel_order(tp_order_id)
                    tp_order_id = ""
                    time.sleep(2)

                    sell_success = False
                    for _attempt in range(3):
                        _, xrp_avail = get_balances()
                        sell_qty = _floor4(min(xrp_position, xrp_avail))
                        if sell_qty < 1.0:
                            log.warning(
                                f"[EXIT] XRP avail too low ({sell_qty:.4f}) "
                                f"— attempt {_attempt + 1}/3"
                            )
                            time.sleep(5)
                            continue
                        sell_success = emergency_sell(sell_qty)
                        if sell_success:
                            break
                        log.warning(
                            f"[EXIT] Sell attempt {_attempt + 1}/3 failed — retrying in 5s"
                        )
                        time.sleep(5)

                    if sell_success:
                        log.info("[EXIT] Sell confirmed — closing position")
                        exit_price = get_mid_price() or price
                        net_pnl    = (
                            (exit_price - entry_price) * xrp_position
                            - (entry_price + exit_price) * xrp_position * COINBASE_FEE
                        )
                        exit_reason = "STOP-LOSS" if hit_sl else "RSI-EXIT"
                        log_trade(
                            side="SELL", price=exit_price, qty=xrp_position,
                            reason=exit_reason, entry_price=entry_price,
                            net_pnl_usd=net_pnl,
                            portfolio_after=get_portfolio_value(exit_price),
                            regime=entry_regime, entry_time_utc=entry_time_utc,
                        )
                        in_position = False; xrp_position = 0.0
                        tp_price = 0.0; stop_price = 0.0
                        trailing_active = False; high_since_entry = 0.0
                        resumed = False; rsi_exit_count = 0
                        entry_time_utc = ""; entry_regime = ""
                        if hit_sl:
                            sl_cooldown_until = now + timedelta(seconds=SL_COOLDOWN_SEC)
                            log.info(
                                f"SL cooldown active until "
                                f"{sl_cooldown_until.strftime('%H:%M UTC')}"
                            )
                        clear_state()
                    else:
                        log.error(
                            "[EXIT] ⚠️ Sell FAILED after 3 attempts — "
                            "keeping position state, will retry next loop"
                        )
                        save_state(
                            in_position=in_position, entry_price=entry_price,
                            xrp_position=xrp_position, tp_order_id=tp_order_id,
                            tp_price=tp_price, stop_price=stop_price,
                            trailing_active=trailing_active, high_since_entry=high_since_entry,
                            sl_cooldown_until=sl_cooldown_until, start_val=start_val,
                            day_start_val=day_start_val, current_day=current_day,
                            resumed=resumed, entry_time_utc=entry_time_utc,
                            entry_regime=entry_regime,
                        )

            # ═════════════════════════════════════════════════════════════════
            # NO POSITION — scan for entry
            # ═════════════════════════════════════════════════════════════════
            else:
                if now < sl_cooldown_until:
                    remaining = int((sl_cooldown_until - now).total_seconds() // 60)
                    log.info(f"[COOLDOWN] {remaining}min remaining | price=${price:.4f}")
                    time.sleep(LOOP_SLEEP)
                    continue

                df = fetch_candles(CANDLES_NEEDED)
                if df.empty:
                    time.sleep(LOOP_SLEEP)
                    continue

                ind    = compute_indicators(df)
                regime = detect_regime(ind)
                sig    = get_long_signal(ind, regime)

                log.info(
                    f"[SCAN] price=${price:.4f}  RSI={ind['rsi']:.1f}  "
                    f"EMA20={ind['ema']:.4f}  EMA50={ind['ema50']:.4f}  "
                    f"slope={'↑' if ind['ema'] > ind['ema_prev'] else '↓'}  "
                    f"regime={regime}  "
                    f"vol={ind['vol_ratio']:.2f}  → {sig}"
                )

                if sig == "BUY":
                    usdc, xrp_bal = get_balances()

                    if xrp_bal * price > 5.0:
                        log.warning(
                            f"Skipping BUY — already holding {xrp_bal:.2f} XRP "
                            f"(${xrp_bal * price:.2f}) with no tracked position"
                        )
                        time.sleep(LOOP_SLEEP)
                        continue

                    deploy = round(min(usdc * POSITION_PCT, CAPITAL * POSITION_PCT), 2)
                    if deploy < 5.0:
                        log.warning(f"Insufficient USDC: ${usdc:.2f} available")
                        time.sleep(LOOP_SLEEP)
                        continue

                    log.info(
                        f"🟢 BUY SIGNAL | RSI={ind['rsi']:.1f}  "
                        f"regime={regime}  deploying ${deploy:.2f}"
                    )
                    buy_oid, buy_price_placed, qty = place_limit_buy(deploy, price)
                    if not buy_oid:
                        time.sleep(LOOP_SLEEP)
                        continue

                    # Wait up to 90s for buy fill
                    filled = False
                    for _ in range(90):
                        time.sleep(1)
                        if is_order_filled(buy_oid):
                            filled = True
                            break
                    if not filled:
                        log.warning("Buy not filled in 90s — cancelling")
                        cancel_order(buy_oid)
                        time.sleep(LOOP_SLEEP)
                        continue

                    entry_price      = get_fill_price(buy_oid, buy_price_placed)
                    xrp_position     = qty
                    high_since_entry = entry_price
                    stop_price       = round(entry_price * (1 - SL_PCT / 100), 4)
                    tp_price         = round(entry_price * (1 + TP_PCT / 100), 4)
                    in_position      = True
                    trailing_active  = False
                    resumed          = False
                    entry_time_utc   = now.isoformat()   # v2.5: record entry time
                    entry_regime     = regime             # v2.5: record entry regime

                    tp_order_id = place_limit_sell_tp(qty, tp_price)
                    log.info(
                        f"Position open: {qty} XRP @ ${entry_price:.4f} | "
                        f"TP=${tp_price:.4f} (+{TP_PCT}%) | SL=${stop_price:.4f} (-{SL_PCT}%) | "
                        f"regime={entry_regime}"
                    )
                    log_trade(
                        side="BUY", price=entry_price, qty=qty,
                        reason="ENTRY", entry_price=entry_price,
                        portfolio_after=get_portfolio_value(entry_price),
                        regime=entry_regime, entry_time_utc=entry_time_utc,
                    )
                    save_state(
                        in_position=in_position, entry_price=entry_price,
                        xrp_position=xrp_position, tp_order_id=tp_order_id,
                        tp_price=tp_price, stop_price=stop_price,
                        trailing_active=trailing_active, high_since_entry=high_since_entry,
                        sl_cooldown_until=sl_cooldown_until, start_val=start_val,
                        day_start_val=day_start_val, current_day=current_day, resumed=False,
                        entry_time_utc=entry_time_utc, entry_regime=entry_regime,
                    )

        except KeyboardInterrupt:
            log.info("Agent stopped by user")
            break
        except Exception as e:
            log.error(f"Main loop error: {e}", exc_info=True)

        time.sleep(LOOP_SLEEP)


if __name__ == "__main__":
    main()
