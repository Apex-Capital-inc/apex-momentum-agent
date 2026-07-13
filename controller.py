"""
Apex Multi-Asset Controller
============================
v1.9 — Risk-Gated Multi-Factor Regime-Rotation Engine with Master Ledger Veto.

Changes vs v1.8:
  - 5-factor opportunity scorer: Trend/Structure (30) + Momentum RSI+MACD (25)
    + Participation vol+POC (20) + Relative Strength (15) + Ledger Stats (10)
  - ATR(14)-based SL/TP: SL = 1.5×ATR, TP = 2.5×ATR (HVN target if closer)
  - Trail activates after +1.5×ATR profit
  - Volume Profile POC/HVN proximity in scoring (Participation factor)
  - MACD (12/26/9) in Momentum factor
  - Market structure detection (HH/HL vs LH/LL) in Trend factor
  - Shorts fire on downtrend CONTINUATION (bearish structure + MACD) — RSI>65
    gate removed (was blocking all shorts in real downtrends)
  - RSI exits gated by profit check: only RSI-exit if net_pnl > 0 after fees
  - RSI exit added for SHORT positions (RSI < 30 = momentum exhausted)
  - pre_trade_risk_check(): 5 hard gates before every entry
  - Ledger profit gate: zone net_pnl ≤ 0 with ≥5 trades → veto entry
  - Relative strength rotation: XRP vs FLR momentum diff as scorer input
  - MIN_ENTRY_SCORE raised to 45 (from 20)
  - CANDLES_NEEDED raised to 100 for Volume Profile accuracy

Architecture:
  Coinbase : XRP long positions (mean-reversion + momentum)
  Kraken   : FLR long positions + XRP/FLR shorts in downtrends (SHORTING_ENABLED=true)

Capital flow:
  USDC is home base. Every decision is measured in USDC profit.
  The controller decides which asset offers the best current opportunity
  and allocates capital accordingly. When nothing passes all gates → stay in USDC.

Regime per asset (independent):
  STRONG_DOWNTREND : EMA20 falling AND price < EMA50 → short candidate (Kraken)
  RANGING          : mixed → mean-reversion long if oversold + structure
  UPTREND          : EMA20 rising AND price > EMA50 → momentum long

Risk:
  MAX_DRAWDOWN_PCT = 20% hard cap across total portfolio
  SL  = 1.5 × ATR(14) from entry (falls back to fixed pct if ATR unavailable)
  TP  = 2.5 × ATR(14) from entry (or nearest HVN if closer)
  Trail: activates after +1.5×ATR profit, locks in at 1.0×ATR distance from peak
"""

import os, time, logging, json, csv, math, uuid
from datetime import datetime, timezone, timedelta
import pandas as pd
import master_ledger as ledger

# ── Exchange clients ──────────────────────────────────────────────────────────
from coinbase.rest import RESTClient
import kraken_client as kraken

# ── Logging ───────────────────────────────────────────────────────────────────
logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s  %(levelname)-7s %(message)s",
    datefmt="%Y-%m-%d %H:%M:%S",
)
log = logging.getLogger("controller")

# ── Config ────────────────────────────────────────────────────────────────────
TOTAL_CAPITAL     = float(os.getenv("TOTAL_CAPITAL_USDC", "500"))
POSITION_PCT      = float(os.getenv("POSITION_PCT", "0.40"))
MAX_DRAWDOWN_PCT  = 20.0
TP_PCT            = float(os.getenv("TP_PCT", "2.3"))           # fallback only (ATR used first)
SL_PCT            = float(os.getenv("SL_PCT", "1.6"))           # fallback only
ATR_PERIOD        = int(os.getenv("ATR_PERIOD", "14"))
ATR_SL_MULT       = float(os.getenv("ATR_SL_MULT", "1.5"))      # SL = 1.5 × ATR
ATR_TP_MULT       = float(os.getenv("ATR_TP_MULT", "2.5"))      # TP = 2.5 × ATR
ATR_TRAIL_MULT    = float(os.getenv("ATR_TRAIL_MULT", "1.5"))   # trail activates after 1.5×ATR profit
TRAIL_DIST        = float(os.getenv("TRAIL_DIST_PCT", "0.5"))   # fallback trail distance %
RSI_BUY_RANGING   = float(os.getenv("RSI_BUY", "35"))
RSI_BUY_UPTREND   = float(os.getenv("RSI_BUY_UPTREND", "42"))
RSI_EXIT          = float(os.getenv("RSI_EXIT", "65"))
RSI_SHORT_EXIT    = float(os.getenv("RSI_SHORT_EXIT", "30"))    # exit shorts when RSI < 30
VOL_RATIO_MIN     = float(os.getenv("VOL_RATIO_MIN", "0.3"))    # liquidity floor
RSI_PERIOD        = int(os.getenv("RSI_PERIOD", "14"))
EMA_PERIOD        = int(os.getenv("EMA_PERIOD", "20"))
EMA_SLOW_PERIOD   = int(os.getenv("EMA_SLOW_PERIOD", "50"))
EMA_SLOPE_BARS    = int(os.getenv("EMA_SLOPE_BARS", "5"))
SL_COOLDOWN_SEC   = int(os.getenv("SL_COOLDOWN_SEC", "1800"))
COINBASE_FEE      = float(os.getenv("COINBASE_FEE", "0.004"))
LOOP_SLEEP        = int(os.getenv("LOOP_SLEEP", "30"))
CANDLES_NEEDED    = max(RSI_PERIOD + 10, EMA_SLOW_PERIOD + 10, 100)
RSI_EXIT_CONFIRM  = 3
TP_HEARTBEAT_LOOPS = 10

SHORT_LEVERAGE    = int(os.getenv("SHORT_LEVERAGE", "2"))
SHORTING_ENABLED  = os.getenv("SHORTING_ENABLED", "false").lower() == "true"
FLR_POSITION_PCT  = float(os.getenv("FLR_POSITION_PCT", "0.28"))
FLR_SL_PCT        = float(os.getenv("FLR_SL_PCT", "2.0"))
SHORT_EMA50_GAP   = float(os.getenv("SHORT_EMA50_GAP", "0.5"))
MIN_ENTRY_SCORE   = float(os.getenv("MIN_ENTRY_SCORE", "45"))   # raised from 20

# ── Paths ─────────────────────────────────────────────────────────────────────
DATA_DIR    = os.getenv("DATA_DIR", "/data")
STATE_FILE  = os.path.join(DATA_DIR, "controller_state.json")
TRADE_LOG   = os.path.join(DATA_DIR, "trade_log.csv")
os.makedirs(DATA_DIR, exist_ok=True)

TRADE_LOG_HEADER = [
    "timestamp_utc", "exchange", "asset", "side", "price", "qty",
    "reason", "entry_price", "exit_time_utc", "net_pnl_usd", "net_pnl_pct",
    "portfolio_after", "regime", "entry_time_utc",
]

# ── Coinbase client ───────────────────────────────────────────────────────────
_api_secret = (
    os.environ.get("COINBASE_API_SECRET")
    or os.environ.get("COINBASE_PRIVATE_KEY")
    or ""
).replace("\\n", "\n")

cb = RESTClient(
    api_key=os.environ["COINBASE_API_KEY"],
    api_secret=_api_secret,
)

# ─────────────────────────────────────────────────────────────────────────────
# Trade diary
# ─────────────────────────────────────────────────────────────────────────────

def log_trade(*, exchange: str, asset: str, side: str, price: float, qty: float,
              reason: str, entry_price: float = 0.0, net_pnl_usd: float = 0.0,
              portfolio_after: float = 0.0, regime: str = "", entry_time_utc: str = "",
              exit_time_utc: str = ""):
    try:
        now_str     = datetime.now(timezone.utc).strftime("%Y-%m-%d %H:%M:%S")
        is_exit     = side in ("SELL", "COVER")
        _exit_time  = exit_time_utc if exit_time_utc else (now_str if is_exit else "")
        file_exists = os.path.exists(TRADE_LOG)
        with open(TRADE_LOG, "a", newline="") as f:
            writer = csv.writer(f)
            if not file_exists:
                writer.writerow(TRADE_LOG_HEADER)
            net_pnl_pct = (net_pnl_usd / (entry_price * qty) * 100) if entry_price and qty else 0.0
            writer.writerow([
                now_str,
                exchange, asset, side,
                round(price, 5), round(qty, 4), reason,
                round(entry_price, 5), _exit_time,
                round(net_pnl_usd, 4), round(net_pnl_pct, 2),
                round(portfolio_after, 2), regime, entry_time_utc,
            ])
        log.info(
            f"[DIARY] {exchange}/{asset} {side} {qty:.4f} @ ${price:.5f} | "
            f"reason={reason}  regime={regime}  pnl=${net_pnl_usd:+.4f}"
        )
    except Exception as e:
        log.warning(f"log_trade error: {e}")


# ─────────────────────────────────────────────────────────────────────────────
# Indicators
# ─────────────────────────────────────────────────────────────────────────────

def compute_rsi(closes: pd.Series, period: int = 14) -> float:
    delta = closes.diff().dropna()
    gain  = delta.clip(lower=0)
    loss  = (-delta).clip(lower=0)
    avg_g = gain.ewm(alpha=1 / period, adjust=False).mean()
    avg_l = loss.ewm(alpha=1 / period, adjust=False).mean()
    rs    = avg_g / avg_l.replace(0, 1e-10)
    return float((100 - 100 / (1 + rs)).iloc[-1])


def compute_atr(df: pd.DataFrame, period: int = 14) -> float:
    """Average True Range: max(H-L, |H-Cprev|, |L-Cprev|)."""
    if len(df) < period + 1:
        return 0.0
    tr = pd.DataFrame(index=df.index)
    tr["hl"] = df["high"] - df["low"]
    tr["hc"] = (df["high"] - df["close"].shift(1)).abs()
    tr["lc"] = (df["low"]  - df["close"].shift(1)).abs()
    tr["tr"] = tr[["hl", "hc", "lc"]].max(axis=1)
    return float(tr["tr"].ewm(alpha=1 / period, adjust=False).mean().iloc[-1])


def compute_macd(closes: pd.Series, fast: int = 12, slow: int = 26, signal: int = 9) -> dict:
    """MACD line, signal line, histogram, and direction flag."""
    if len(closes) < slow + signal:
        return {"macd": 0.0, "signal": 0.0, "histogram": 0.0, "bullish": False}
    ema_fast    = closes.ewm(span=fast, adjust=False).mean()
    ema_slow    = closes.ewm(span=slow, adjust=False).mean()
    macd_line   = ema_fast - ema_slow
    signal_line = macd_line.ewm(span=signal, adjust=False).mean()
    histogram   = macd_line - signal_line
    return {
        "macd":      float(macd_line.iloc[-1]),
        "signal":    float(signal_line.iloc[-1]),
        "histogram": float(histogram.iloc[-1]),
        "bullish":   float(macd_line.iloc[-1]) > float(signal_line.iloc[-1]),
    }


def compute_volume_profile(df: pd.DataFrame, bins: int = 20) -> dict:
    """
    Approximate Volume Profile using price bins weighted by volume.
    Returns Point of Control (highest volume price) and High Volume Nodes.
    """
    if df.empty or len(df) < 5:
        return {"poc": 0.0, "hvn_list": []}
    prices   = (df["high"] + df["low"]) / 2
    low_p    = prices.min()
    high_p   = prices.max()
    if high_p <= low_p:
        return {"poc": float(prices.iloc[-1]), "hvn_list": []}
    bin_size = (high_p - low_p) / bins
    vp: dict = {}
    for _, row in df.iterrows():
        mid = (row["high"] + row["low"]) / 2
        b   = min(int((mid - low_p) / bin_size), bins - 1)
        vp[b] = vp.get(b, 0.0) + float(row["volume"])
    if not vp:
        return {"poc": float(prices.iloc[-1]), "hvn_list": []}
    poc_bin   = max(vp, key=lambda k: vp[k])
    poc_price = low_p + (poc_bin + 0.5) * bin_size
    max_vol   = vp[poc_bin]
    hvn_list  = sorted([low_p + (b + 0.5) * bin_size
                        for b, v in vp.items() if v >= 0.70 * max_vol])
    return {"poc": float(poc_price), "hvn_list": hvn_list}


def detect_market_structure(df: pd.DataFrame, lookback: int = 12) -> dict:
    """
    Detect HH/HL (BULLISH) vs LH/LL (BEARISH) structure from recent pivot points.
    Uses simple 3-bar pivot: bar higher/lower than both neighbours.
    """
    if len(df) < lookback + 2:
        return {"structure": "NEUTRAL", "swing_high": 0.0, "swing_low": 0.0}
    recent = df.tail(lookback + 2).reset_index(drop=True)
    highs  = recent["high"].values
    lows   = recent["low"].values
    swing_highs = [highs[i] for i in range(1, len(highs) - 1)
                   if highs[i] > highs[i - 1] and highs[i] > highs[i + 1]]
    swing_lows  = [lows[i] for i in range(1, len(lows) - 1)
                   if lows[i] < lows[i - 1] and lows[i] < lows[i + 1]]
    struct = "NEUTRAL"
    if len(swing_highs) >= 2 and len(swing_lows) >= 2:
        hh = swing_highs[-1] > swing_highs[-2]
        hl = swing_lows[-1]  > swing_lows[-2]
        lh = swing_highs[-1] < swing_highs[-2]
        ll = swing_lows[-1]  < swing_lows[-2]
        if hh and hl:
            struct = "BULLISH"
        elif lh and ll:
            struct = "BEARISH"
    last_high = swing_highs[-1] if swing_highs else float(df["high"].iloc[-1])
    last_low  = swing_lows[-1]  if swing_lows  else float(df["low"].iloc[-1])
    return {"structure": struct, "swing_high": last_high, "swing_low": last_low}


def compute_indicators(df: pd.DataFrame) -> dict:
    closes    = df["close"]
    volumes   = df["volume"]
    rsi       = compute_rsi(closes, RSI_PERIOD)

    ema_series = closes.ewm(span=EMA_PERIOD, adjust=False).mean()
    ema        = float(ema_series.iloc[-1])
    slope_idx  = max(0, len(ema_series) - 1 - EMA_SLOPE_BARS)
    ema_prev   = float(ema_series.iloc[slope_idx])
    ema50      = float(closes.ewm(span=EMA_SLOW_PERIOD, adjust=False).mean().iloc[-1])

    price     = float(closes.iloc[-1])
    vol_avg   = float(volumes.iloc[-20:].mean()) if len(volumes) >= 20 else float(volumes.mean())
    vol_ratio = float(volumes.iloc[-1]) / vol_avg if vol_avg > 0 else 1.0

    atr  = compute_atr(df, ATR_PERIOD)
    macd = compute_macd(closes)
    vp   = compute_volume_profile(df)
    ms   = detect_market_structure(df)

    return {
        "rsi":       rsi,
        "ema":       ema,
        "ema_prev":  ema_prev,
        "ema50":     ema50,
        "price":     price,
        "vol_ratio": vol_ratio,
        "atr":       atr,
        "macd":      macd,
        "vp_poc":    vp["poc"],
        "vp_hvn":    vp["hvn_list"],
        "structure": ms["structure"],
        "swing_high": ms["swing_high"],
        "swing_low":  ms["swing_low"],
    }


def detect_regime(ind: dict) -> str:
    ema20_rising = ind["ema"] > ind["ema_prev"]
    above_ema50  = ind["price"] > ind["ema50"]
    if not ema20_rising and not above_ema50:
        return "STRONG_DOWNTREND"
    elif ema20_rising and above_ema50:
        return "UPTREND"
    return "RANGING"


# ─────────────────────────────────────────────────────────────────────────────
# 5-Factor Opportunity Scorer (v1.9)
# ─────────────────────────────────────────────────────────────────────────────

def opportunity_score(ind: dict, regime: str, ledger_stats: dict = None,
                      relative_strength: float = 0.0) -> float:
    """
    5-Factor composite score 0-100. Entry only if score >= MIN_ENTRY_SCORE (45).

    Factor 1 — Trend / Structure    30 pts  EMA alignment + HH/HL vs LH/LL
    Factor 2 — Momentum             25 pts  RSI proximity + MACD direction
    Factor 3 — Participation        20 pts  relative volume + POC proximity
    Factor 4 — Relative Strength    15 pts  this asset vs peer momentum
    Factor 5 — Ledger Stats         10 pts  zone+regime WR and avg PnL

    STRONG_DOWNTREND → score SHORT setup (bearish continuation signals)
    RANGING          → score LONG mean-reversion (oversold bounce)
    UPTREND          → score LONG momentum (pullback in trend)
    """
    score = 0.0
    macd  = ind.get("macd", {})

    # ── Factor 1: Trend / Structure (30 pts) ─────────────────────────────────
    if regime == "STRONG_DOWNTREND":
        # Bearish structure = max points; NEUTRAL = partial
        if ind.get("structure") == "BEARISH":
            score += 15.0
        elif ind.get("structure") == "NEUTRAL":
            score += 5.0
        if ind["ema"] < ind["ema50"]:      # EMA20 below EMA50
            score += 8.0
        if ind["ema"] < ind["ema_prev"]:   # EMA20 still falling
            score += 7.0

    elif regime == "UPTREND":
        if ind.get("structure") == "BULLISH":
            score += 15.0
        elif ind.get("structure") == "NEUTRAL":
            score += 5.0
        if ind["ema"] > ind["ema50"]:
            score += 8.0
        if ind["ema"] > ind["ema_prev"]:
            score += 7.0

    elif regime == "RANGING":
        # Mean-reversion: below EMA50 is the core signal
        if ind["price"] < ind["ema50"]:
            pct = (ind["ema50"] - ind["price"]) / ind["ema50"] * 100
            score += min(18.0, pct * 3.0)   # 6% below EMA50 → full 18pts
        if ind["ema"] > ind["ema_prev"]:     # EMA20 turning up = confirmation
            score += 12.0

    # ── Factor 2: Momentum — RSI + MACD (25 pts) ─────────────────────────────
    if regime == "STRONG_DOWNTREND":
        # SHORT momentum: RSI above midpoint (not deeply oversold) = downtrend continuation
        # RSI 45-55: moderate; 55-65: strong; 65+: overbought bounce = ideal short
        rsi_pts = 0.0
        if ind["rsi"] > 45:
            rsi_pts = min(15.0, (ind["rsi"] - 45) * 0.5)
        score += rsi_pts
        # MACD bearish = continuation confirmed
        if macd and not macd.get("bullish", True):
            score += 7.0
        if macd and macd.get("histogram", 0) < 0:
            score += 3.0

    elif regime == "UPTREND":
        # LONG momentum: RSI pullback to 35-50 = ideal re-entry in uptrend
        rsi_pts = max(0.0, min(15.0, (55 - ind["rsi"]) * 0.75)) if ind["rsi"] < 55 else 0.0
        score += rsi_pts
        if macd and macd.get("bullish", False):
            score += 7.0
        if macd and macd.get("histogram", 0) > 0:
            score += 3.0

    elif regime == "RANGING":
        # LONG mean-reversion: deeply oversold RSI = max points
        rsi_pts = max(0.0, min(20.0, (48 - ind["rsi"]) * 0.8)) if ind["rsi"] < 48 else 0.0
        score += rsi_pts
        # MACD starting to turn bullish = early confirmation
        if macd and macd.get("bullish", False):
            score += 5.0

    # ── Factor 3: Participation — Volume + POC Proximity (20 pts) ────────────
    # Volume relative to average (0-10 pts)
    vol_pts = min(10.0, max(0.0, (ind["vol_ratio"] - 0.6) * 7.0))
    score  += vol_pts

    # POC proximity: price near Point of Control = high liquidity zone (0-10 pts)
    poc = ind.get("vp_poc", 0.0)
    if poc > 0 and ind["price"] > 0:
        poc_dist_pct = abs(ind["price"] - poc) / poc * 100
        poc_pts = max(0.0, 10.0 - poc_dist_pct * 1.5)   # within 0.5%→10pts, >6.7%→0pts
        score  += poc_pts

    # ── Factor 4: Relative Strength vs Peer (15 pts) ─────────────────────────
    # relative_strength: positive = this asset leads in the correct direction
    # Mapped -50→0 … 0→7.5 … +50→15
    rs_pts = max(0.0, min(15.0, (relative_strength + 50.0) * 0.15))
    score += rs_pts

    # ── Factor 5: Ledger Stats — Zone + Regime WR (10 pts) ───────────────────
    if ledger_stats and ledger_stats.get("trades", 0) >= 3:
        wr      = ledger_stats.get("win_rate", 50)    # 0-100
        avg_pnl = ledger_stats.get("avg_pnl", 0.0)
        if wr >= 60 and avg_pnl > 0:
            score += 10.0
        elif wr >= 50 and avg_pnl > 0:
            score += 6.0
        elif wr >= 40:
            score += 2.0
        # WR < 40 or avg_pnl <= 0: 0 ledger pts (veto handled in pre_trade_risk_check)
    else:
        # No history yet: neutral bonus
        score += 5.0

    return round(min(100.0, max(0.0, score)), 1)


# ─────────────────────────────────────────────────────────────────────────────
# ATR-Based SL/TP
# ─────────────────────────────────────────────────────────────────────────────

def compute_atr_sl_tp(entry_price: float, direction: str, ind: dict,
                       fallback_sl_pct: float, fallback_tp_pct: float,
                       decimals: int = 5) -> tuple[float, float]:
    """
    ATR-based SL/TP.
      LONG  : SL = entry - ATR_SL_MULT × ATR    TP = entry + ATR_TP_MULT × ATR
      SHORT : SL = entry + ATR_SL_MULT × ATR    TP = entry - ATR_TP_MULT × ATR
    Also targets nearest HVN if it lands between entry and ATR TP.
    Falls back to fixed pct when ATR is unavailable.
    """
    atr = ind.get("atr", 0.0)
    fmt = lambda v: round(v, decimals)

    if atr <= 0 or entry_price <= 0:
        if direction == "LONG":
            return fmt(entry_price * (1 - fallback_sl_pct / 100)), \
                   fmt(entry_price * (1 + fallback_tp_pct / 100))
        else:
            return fmt(entry_price * (1 + fallback_sl_pct / 100)), \
                   fmt(entry_price * (1 - fallback_tp_pct / 100))

    if direction == "LONG":
        sl = fmt(entry_price - ATR_SL_MULT * atr)
        tp = fmt(entry_price + ATR_TP_MULT * atr)
        # If a HVN sits between entry+0.5% and ATR TP, target it instead
        hvn_list = ind.get("vp_hvn", [])
        above_hvn = [h for h in hvn_list if h > entry_price * 1.005]
        if above_hvn:
            nearest = above_hvn[0]
            if nearest < tp:
                tp = fmt(nearest)
    else:
        sl = fmt(entry_price + ATR_SL_MULT * atr)
        tp = fmt(entry_price - ATR_TP_MULT * atr)
        hvn_list = ind.get("vp_hvn", [])
        below_hvn = [h for h in hvn_list if h < entry_price * 0.995]
        if below_hvn:
            nearest = below_hvn[-1]
            if nearest > tp:
                tp = fmt(nearest)

    return sl, tp


# ─────────────────────────────────────────────────────────────────────────────
# Pre-Trade Risk Gate (5 hard checks)
# ─────────────────────────────────────────────────────────────────────────────

def pre_trade_risk_check(asset: str, price: float, deploy: float,
                          available: float, port_val: float, ind: dict,
                          ledger_stats: dict) -> tuple[bool, str]:
    """
    5 hard gates that must ALL pass before any entry is opened.
    Returns (ok: bool, reason_if_blocked: str).
    """
    # 1. Position size vs available balance
    if deploy > available * 0.95:
        return False, f"insufficient balance: need ${deploy:.2f} avail=${available:.2f}"

    # 2. Portfolio concentration: single position ≤ 60% of portfolio
    if port_val > 0 and deploy > port_val * 0.60:
        return False, f"concentration limit: ${deploy:.2f} > 60% of ${port_val:.2f}"

    # 3. Liquidity floor: ultra-thin market = skip
    if ind.get("vol_ratio", 1.0) < VOL_RATIO_MIN:
        return False, f"liquidity too thin: vol_ratio={ind['vol_ratio']:.2f} < {VOL_RATIO_MIN}"

    # 4. Ledger zone veto: ≥3 losses / 0 wins near this price
    zr = ledger.zone_risk(asset, price)
    if zr.get("losses", 0) >= 3 and zr.get("wins", 0) == 0:
        return False, f"zone veto: {zr['losses']}L 0W near ${price:.5f}"
    # Extended zone veto: ≥5 trades with net PnL ≤ 0
    if zr.get("trades", 0) >= 5 and zr.get("net_pnl", 0) <= 0:
        return False, f"zone PnL veto: net=${zr['net_pnl']:+.4f} over {zr['trades']} trades in zone"

    # 5. Regime PnL veto: ≥5 trades, WR < 30%, avg PnL < 0
    if ledger_stats and ledger_stats.get("trades", 0) >= 5:
        if ledger_stats.get("avg_pnl", 0) < 0 and ledger_stats.get("win_rate", 50) < 30:
            return False, (f"regime veto: WR={ledger_stats['win_rate']}% "
                           f"avgPnL=${ledger_stats['avg_pnl']:+.4f}")

    return True, ""


# ─────────────────────────────────────────────────────────────────────────────
# Ledger Profit Gate (exit veto)
# ─────────────────────────────────────────────────────────────────────────────

def ledger_profit_gate(entry_price: float, exit_price: float, qty: float,
                        direction: str, fee: float) -> tuple[bool, float]:
    """
    Calculate net P&L after fees and determine if exit is profitable.
    Called before RSI exits — we never exit at a loss unless SL/emergency.
    Returns (is_profitable: bool, net_pnl_usd: float).
    """
    if entry_price <= 0 or qty <= 0:
        return False, 0.0
    if direction == "LONG":
        gross = (exit_price - entry_price) * qty
    else:
        gross = (entry_price - exit_price) * qty
    total_fees = (entry_price + exit_price) * qty * fee
    net_pnl    = gross - total_fees
    return net_pnl > 0, round(net_pnl, 4)


# ─────────────────────────────────────────────────────────────────────────────
# Coinbase helpers (XRP long)
# ─────────────────────────────────────────────────────────────────────────────

def _safe_get(obj, key, default=None):
    if isinstance(obj, dict):
        return obj.get(key, default)
    return getattr(obj, key, default)


def _floor4(qty: float) -> float:
    return math.floor(qty * 10000) / 10000


def _extract_order_id(resp) -> str:
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


def cb_get_mid_price(symbol: str = "XRP-USDC") -> float:
    try:
        book = cb.get_best_bid_ask(product_ids=[symbol])
        bid  = float(book.pricebooks[0].bids[0].price)
        ask  = float(book.pricebooks[0].asks[0].price)
        return (bid + ask) / 2
    except Exception as e:
        log.warning(f"cb_get_mid_price error: {e}")
        return 0.0


def cb_get_balances() -> tuple[float, float]:
    """Returns (usdc_total, xrp_total) — includes funds held in open orders."""
    usdc = xrp = 0.0
    try:
        for acct in cb.get_accounts().accounts:
            def _v(b) -> float:
                if b is None:
                    return 0.0
                try:
                    return float(b["value"] if isinstance(b, dict) else b.value)
                except Exception:
                    return 0.0
            total_val = _v(acct.available_balance) + _v(acct.hold)
            if acct.currency == "USDC":
                usdc = total_val
            elif acct.currency == "XRP":
                xrp  = total_val
    except Exception as e:
        log.warning(f"cb_get_balances error: {e}")
    return usdc, xrp


def cb_fetch_candles(symbol: str = "XRP-USDC", n: int = 70) -> pd.DataFrame:
    end   = int(time.time())
    start = end - n * 5 * 60
    try:
        resp    = cb.get_candles(product_id=symbol, start=str(start), end=str(end), granularity="FIVE_MINUTE")
        candles = resp.candles
        if not candles:
            return pd.DataFrame()
        df = pd.DataFrame([{
            "ts": int(c.start), "open": float(c.open), "high": float(c.high),
            "low": float(c.low), "close": float(c.close), "volume": float(c.volume),
        } for c in candles])
        df.sort_values("ts", inplace=True)
        df.reset_index(drop=True, inplace=True)
        return df
    except Exception as e:
        log.warning(f"cb_fetch_candles error: {e}")
        return pd.DataFrame()


def cb_place_limit_buy(usdc_amount: float, price: float, symbol: str = "XRP-USDC") -> tuple[str, float, float]:
    buy_price = round(price * 1.0005, 4)
    xrp_qty   = round((usdc_amount / buy_price) * (1 - COINBASE_FEE), 4)
    if xrp_qty < 1.0:
        log.warning(f"CB buy qty too small ({xrp_qty})")
        return "", 0.0, 0.0
    cid = f"ctrl-buy-{uuid.uuid4().hex[:8]}"
    try:
        resp = cb.limit_order_gtc_buy(client_order_id=cid, product_id=symbol,
                                       base_size=str(xrp_qty), limit_price=str(buy_price))
        oid = _extract_order_id(resp)
        log.info(f"[CB] BUY {xrp_qty} XRP @ ${buy_price} | id={oid}")
        return oid, buy_price, xrp_qty
    except Exception as e:
        log.error(f"cb_place_limit_buy error: {e}")
        return "", 0.0, 0.0


def cb_place_tp(xrp_qty: float, tp_price: float, symbol: str = "XRP-USDC") -> str:
    cid = f"ctrl-tp-{uuid.uuid4().hex[:8]}"
    try:
        resp = cb.limit_order_gtc_sell(client_order_id=cid, product_id=symbol,
                                        base_size=str(_floor4(xrp_qty)),
                                        limit_price=str(round(tp_price, 4)))
        oid = _extract_order_id(resp)
        if oid:
            log.info(f"[CB] TP {xrp_qty} XRP @ ${tp_price:.4f} | id={oid}")
        return oid
    except Exception as e:
        log.error(f"cb_place_tp error: {e}")
        return ""


def cb_emergency_sell(xrp_qty: float, price: float, symbol: str = "XRP-USDC") -> bool:
    sl_price = round(price * 0.995, 4)
    xrp_qty  = _floor4(xrp_qty)
    cid      = f"ctrl-sl-{uuid.uuid4().hex[:8]}"
    try:
        resp = cb.limit_order_gtc_sell(client_order_id=cid, product_id=symbol,
                                        base_size=str(xrp_qty), limit_price=str(sl_price))
        oid = _extract_order_id(resp)
        log.info(f"[CB] SL sell {xrp_qty} XRP @ ${sl_price} | id={oid}")
        for _ in range(30):
            time.sleep(1)
            try:
                resp = cb.get_order(order_id=oid)
                o = _safe_get(resp, "order") or resp
                if _safe_get(o, "status") in ("FILLED", "DONE"):
                    return True
            except Exception:
                pass
        return True
    except Exception as e:
        log.error(f"cb_emergency_sell error: {e}")
        return False


def cb_is_order_filled(order_id: str) -> bool:
    if not order_id:
        return False
    try:
        resp = cb.get_order(order_id=order_id)
        o = _safe_get(resp, "order") or resp
        return _safe_get(o, "status") in ("FILLED", "DONE")
    except Exception:
        return False


def cb_get_fill_price(order_id: str, fallback: float) -> float:
    try:
        resp = cb.get_order(order_id=order_id)
        o = _safe_get(resp, "order") or resp
        avg = _safe_get(o, "average_filled_price")
        return float(avg) if avg else fallback
    except Exception:
        return fallback


# ─────────────────────────────────────────────────────────────────────────────
# State persistence
# ─────────────────────────────────────────────────────────────────────────────

def _empty_position(asset: str, exchange: str) -> dict:
    return {
        "active":           False,
        "asset":            asset,
        "exchange":         exchange,
        "direction":        "LONG",
        "entry_price":      0.0,
        "qty":              0.0,
        "tp_order_id":      "",
        "tp_price":         0.0,
        "stop_price":       0.0,
        "high_since_entry": 0.0,
        "trailing_active":  False,
        "entry_time_utc":   "",
        "entry_regime":     "",
        "resumed":          False,
        "entry_atr":        0.0,   # ATR at entry, for trail calculations
    }


STATE_FILE_BACKUP = "controller_state_backup.json"

def save_state(state: dict):
    if not state:
        state = {}
    state["saved_at"] = time.time()
    for path in [STATE_FILE, STATE_FILE_BACKUP]:
        try:
            d = os.path.dirname(path)
            if d:
                os.makedirs(d, exist_ok=True)
            with open(path, "w") as f:
                json.dump(state, f, indent=2)
                f.flush()
                os.fsync(f.fileno())
        except Exception as e:
            log.warning(f"save_state to {path}: {e}")


def load_state() -> dict | None:
    for path in [STATE_FILE, STATE_FILE_BACKUP]:
        try:
            if not os.path.exists(path):
                continue
            with open(path) as f:
                s = json.load(f)
            if not s:
                continue
            age_min = (time.time() - s.get("saved_at", 0)) / 60
            log.info(f"[STATE] Loaded from {path} (age={age_min:.1f}min)")
            return s
        except Exception as e:
            log.warning(f"load_state from {path}: {e}")
    log.warning("[STATE] No saved state found — starting fresh")
    return None


# ─────────────────────────────────────────────────────────────────────────────
# Portfolio valuation
# ─────────────────────────────────────────────────────────────────────────────

def total_portfolio_value(pos_cb: dict, pos_kr: dict, prices: dict) -> float:
    """
    True portfolio value including assets locked in TP/SL orders.
    CB: cb_get_balances() returns total (available + hold).
    KR: Kraken Balance is available-only; add tracked position value explicitly.
    """
    cb_usdc, cb_xrp = cb_get_balances()
    kr_bals = kraken.get_balances()

    total  = cb_usdc
    total += cb_xrp * prices.get("XRP", 0)
    total += kr_bals.get("USD",  0)
    total += kr_bals.get("USDC", 0)
    total += kr_bals.get("XRP",  0) * prices.get("XRP", 0)
    total += kr_bals.get("FLR",  0) * prices.get("FLR", 0)

    # KR LONG position: asset locked in TP sell order not in available balance
    if pos_kr.get("active") and pos_kr.get("direction") == "LONG" and pos_kr.get("qty", 0) > 0:
        asset = pos_kr.get("asset", "XRP")
        total += pos_kr["qty"] * prices.get(asset, 0)

    return total


def kr_ensure_usd(amount_needed: float, kr_bals: dict) -> bool:
    usd_avail  = kr_bals.get("USD",  0.0)
    usdc_avail = kr_bals.get("USDC", 0.0)
    if usd_avail >= amount_needed:
        return True
    shortfall   = amount_needed - usd_avail
    convert_amt = round(shortfall * 1.02, 2)
    if usdc_avail < convert_amt:
        log.warning(
            f"[KRAKEN] Insufficient funds for FLR trade: need ${amount_needed:.2f} USD, "
            f"have ${usd_avail:.2f} USD + ${usdc_avail:.2f} USDC"
        )
        return False
    log.info(f"[KRAKEN] Auto-converting {convert_amt:.2f} USDC → USD for FLR trade")
    ok = kraken.convert_funds("USDC", "ZUSD", convert_amt)
    if ok:
        time.sleep(2)
    return ok


def kr_reconvert_to_usdc(kr_bals: dict):
    usd_avail = kr_bals.get("USD", 0.0)
    if usd_avail >= 1.0:
        convert_amt = round(usd_avail * 0.999, 2)
        log.info(f"[KRAKEN] Reconverting ${convert_amt:.2f} USD → USDC after FLR close")
        kraken.convert_funds("ZUSD", "USDC", convert_amt)


# ─────────────────────────────────────────────────────────────────────────────
# Main controller loop
# ─────────────────────────────────────────────────────────────────────────────

def main():
    log.info("=" * 70)
    log.info("Apex Multi-Asset Controller — v1.9")
    log.info(f"Capital=${TOTAL_CAPITAL}  MaxDD={MAX_DRAWDOWN_PCT}%  MIN_SCORE={MIN_ENTRY_SCORE}")
    log.info(f"ATR: period={ATR_PERIOD}  SL_mult={ATR_SL_MULT}×  TP_mult={ATR_TP_MULT}×  trail={ATR_TRAIL_MULT}×")
    log.info(f"XRP: position={POSITION_PCT*100:.0f}%  FLR: position={FLR_POSITION_PCT*100:.0f}%")
    log.info(f"SHORTS: {'ENABLED (x' + str(SHORT_LEVERAGE) + ') — structure+MACD trigger' if SHORTING_ENABLED else 'DISABLED'}")
    log.info(f"Scorer: Trend(30)+Momentum(25)+Participation(20)+RS(15)+Ledger(10) | threshold={MIN_ENTRY_SCORE}")
    log.info(f"Candles: {CANDLES_NEEDED} | Profit gate: RSI exits only when net_pnl > 0 after fees")
    log.info("=" * 70)

    s = ledger.summary()
    log.info(
        f"[LEDGER] Lifetime: {s['total_trades']} trades  "
        f"W={s['wins']} L={s['losses']} WR={s['win_rate']}%  "
        f"cumPnL=${s['cumulative_pnl']:+.4f}  "
        f"streak={s['streak']['count']}x{s['streak']['type']}"
    )

    log.info("[BOOT] Verifying Kraken connection...")
    kraken_ok = kraken.ping()
    if not kraken_ok:
        log.error("[BOOT] Kraken connection FAILED — check API keys. Continuing with Coinbase only.")

    log.info("[BOOT] Verifying available pairs on Kraken...")
    pair_info = kraken.verify_pairs()
    flr_trading_ok = pair_info.get(kraken.FLR_PAIR, {}).get("available", False)
    if flr_trading_ok:
        log.info(f"[BOOT] FLR trading ENABLED — {kraken.FLR_PAIR} (USDC↔USD auto-conversion active)")
    else:
        log.warning(f"[BOOT] FLR trading DISABLED — {kraken.FLR_PAIR} not available")

    pos_xrp_cb = _empty_position("XRP", "coinbase")
    pos_kraken  = _empty_position("XRP", "kraken")

    sl_cooldown = {
        "coinbase": datetime.now(timezone.utc),
        "kraken":   datetime.now(timezone.utc),
    }
    asset_cooldown = {
        "XRP": datetime.now(timezone.utc),
        "FLR": datetime.now(timezone.utc),
    }

    saved = load_state()
    if saved:
        pos_xrp_cb = saved.get("pos_xrp_cb", pos_xrp_cb)
        pos_kraken  = saved.get("pos_kraken",  pos_kraken)

        if pos_xrp_cb.get("active") and pos_xrp_cb.get("tp_order_id"):
            try:
                if cb_is_order_filled(pos_xrp_cb["tp_order_id"]):
                    fill_p = cb_get_fill_price(pos_xrp_cb["tp_order_id"], pos_xrp_cb.get("tp_price", 0))
                    ledger.close_trade(pos_xrp_cb.get("trade_id", ""), fill_p, "TAKE-PROFIT-OFFLINE", COINBASE_FEE)
                    log.info(f"[STATE] CB TP filled while offline @ ${fill_p:.4f} — position cleared")
                    pos_xrp_cb = _empty_position("XRP", "coinbase")
                else:
                    resp   = cb.get_order(order_id=pos_xrp_cb["tp_order_id"])
                    o      = _safe_get(resp, "order") or resp
                    status = _safe_get(o, "status") or ""
                    if status in ("CANCELLED", "EXPIRED", ""):
                        log.warning(f"[STATE] CB TP order no longer active ({status}) — clearing CB position")
                        pos_xrp_cb = _empty_position("XRP", "coinbase")
                    else:
                        log.info(
                            f"[STATE] CB XRP LONG VERIFIED — {pos_xrp_cb['qty']:.4f} XRP @ "
                            f"${pos_xrp_cb['entry_price']:.4f}  TP=${pos_xrp_cb.get('tp_price', 0):.4f}"
                        )
            except Exception as e:
                log.warning(f"[STATE] CB position verification error: {e} — keeping loaded state")

        if pos_kraken.get("active") and pos_kraken.get("tp_order_id"):
            try:
                if kraken.is_order_filled(pos_kraken["tp_order_id"]):
                    fill_p = kraken.get_fill_price(pos_kraken["tp_order_id"], pos_kraken.get("tp_price", 0))
                    ledger.close_trade(pos_kraken.get("trade_id", ""), fill_p, "TAKE-PROFIT-OFFLINE", kraken.KRAKEN_FEE)
                    log.info(f"[STATE] KR TP filled while offline @ ${fill_p:.5f} — position cleared")
                    pos_kraken = _empty_position("XRP", "kraken")
                else:
                    log.info(
                        f"[STATE] KR {pos_kraken['asset']} {pos_kraken['direction']} VERIFIED — "
                        f"{pos_kraken['qty']:.4f} @ ${pos_kraken['entry_price']:.5f}  "
                        f"TP=${pos_kraken.get('tp_price', 0):.5f}"
                    )
            except Exception as e:
                log.warning(f"[STATE] KR position verification error: {e} — keeping loaded state")

        for exch in sl_cooldown:
            try:
                sl_cooldown[exch] = datetime.fromisoformat(
                    saved.get(f"sl_cooldown_{exch}", datetime.now(timezone.utc).isoformat())
                )
            except Exception:
                pass
        for ast in asset_cooldown:
            try:
                asset_cooldown[ast] = datetime.fromisoformat(
                    saved.get(f"asset_cooldown_{ast}", datetime.now(timezone.utc).isoformat())
                )
            except Exception:
                pass

    xrp_price = cb_get_mid_price("XRP-USDC")
    flr_price = kraken.get_mid_price(kraken.FLR_DATA_PAIR) if kraken_ok else 0.0
    prices    = {"XRP": xrp_price, "FLR": flr_price}

    start_val = total_portfolio_value(pos_xrp_cb, pos_kraken, prices)
    for _ in range(6):
        if start_val > 0:
            break
        time.sleep(10)
        xrp_price = cb_get_mid_price("XRP-USDC")
        prices["XRP"] = xrp_price
        start_val = total_portfolio_value(pos_xrp_cb, pos_kraken, prices)

    log.info(f"Starting portfolio: ${start_val:.2f}")
    log.info(f"Hard stop at      : ${start_val * (1 - MAX_DRAWDOWN_PCT/100):.2f}")

    loop_count     = 0
    rsi_exit_count = {"coinbase": 0, "kraken": 0}
    current_day    = datetime.now(timezone.utc).strftime("%Y-%m-%d")
    day_start_val  = start_val

    # ─────────────────────────────────────────────────────────────────────────
    # Main loop
    # ─────────────────────────────────────────────────────────────────────────
    while True:
        try:
            now        = datetime.now(timezone.utc)
            loop_count += 1

            xrp_price = cb_get_mid_price("XRP-USDC")
            flr_price = kraken.get_mid_price(kraken.FLR_DATA_PAIR) if kraken_ok else 0.0
            prices    = {"XRP": xrp_price, "FLR": flr_price}

            if xrp_price <= 0:
                time.sleep(LOOP_SLEEP)
                continue

            today = now.strftime("%Y-%m-%d")
            if today != current_day:
                eod_val   = total_portfolio_value(pos_xrp_cb, pos_kraken, prices)
                daily_pnl = eod_val - day_start_val
                pct       = daily_pnl / day_start_val * 100 if day_start_val > 0 else 0
                log.info(f"DAILY P&L [{current_day}]: ${daily_pnl:+.4f} ({pct:+.2f}%)")
                current_day   = today
                day_start_val = eod_val

            port_val = total_portfolio_value(pos_xrp_cb, pos_kraken, prices)
            drawdown = (start_val - port_val) / start_val * 100 if start_val > 0 else 0.0
            if drawdown >= MAX_DRAWDOWN_PCT:
                log.error(f"MAX DRAWDOWN {drawdown:.1f}% — HALTING ALL POSITIONS")
                if pos_xrp_cb["active"] and pos_xrp_cb["qty"] > 0:
                    cb_emergency_sell(pos_xrp_cb["qty"], xrp_price)
                if pos_kraken["active"] and pos_kraken["qty"] > 0:
                    if pos_kraken.get("tp_order_id"):
                        kraken.cancel_order(pos_kraken["tp_order_id"])
                        time.sleep(2)
                    p = xrp_price if pos_kraken["asset"] == "XRP" else flr_price
                    if pos_kraken["direction"] == "LONG":
                        kraken.place_limit_sell(
                            kraken.XRP_PAIR if pos_kraken["asset"] == "XRP" else kraken.FLR_PAIR,
                            pos_kraken["qty"], round(p * 0.995, 5)
                        )
                    else:
                        kraken.close_short(
                            kraken.XRP_PAIR if pos_kraken["asset"] == "XRP" else kraken.FLR_PAIR,
                            pos_kraken["qty"], round(p * 1.005, 5)
                        )
                save_state({})
                log.error("Controller halted. Restart after reviewing conditions.")
                break

            # ── Candles + indicators ──────────────────────────────────────────
            df_xrp = cb_fetch_candles("XRP-USDC", CANDLES_NEEDED)
            df_flr = (kraken.fetch_ohlc(kraken.FLR_DATA_PAIR, 5, CANDLES_NEEDED)
                      if kraken_ok and flr_price > 0 else pd.DataFrame())

            ind_xrp    = compute_indicators(df_xrp) if not df_xrp.empty else None
            ind_flr    = compute_indicators(df_flr) if not df_flr.empty else None

            regime_xrp = detect_regime(ind_xrp) if ind_xrp else "UNKNOWN"
            regime_flr = detect_regime(ind_flr) if ind_flr else "UNKNOWN"

            rs_xrp = ledger.regime_stats("XRP", regime_xrp) if regime_xrp != "UNKNOWN" else {}
            rs_flr = ledger.regime_stats("FLR", regime_flr) if regime_flr != "UNKNOWN" else {}

            # ── Relative Strength: XRP vs FLR momentum differential ───────────
            # Positive = this asset has stronger directional momentum
            xrp_rsi = ind_xrp["rsi"] if ind_xrp else 50.0
            flr_rsi = ind_flr["rsi"] if ind_flr else 50.0
            # For LONGS: higher RSI = leading up = positive RS
            # For SHORTS: lower RSI = leading down = positive RS (pass inverted)
            if regime_xrp in ("RANGING", "UPTREND"):
                rel_str_xrp = xrp_rsi - flr_rsi      # xrp stronger up = positive
            else:
                rel_str_xrp = flr_rsi - xrp_rsi      # xrp weaker (lower RSI in DT) = positive for shorting

            if regime_flr in ("RANGING", "UPTREND"):
                rel_str_flr = flr_rsi - xrp_rsi
            else:
                rel_str_flr = xrp_rsi - flr_rsi

            score_xrp = opportunity_score(ind_xrp, regime_xrp, rs_xrp, rel_str_xrp) if ind_xrp else 0.0
            score_flr = opportunity_score(ind_flr, regime_flr, rs_flr, rel_str_flr) if ind_flr else 0.0

            # ── Heartbeat ─────────────────────────────────────────────────────
            log.info(
                f"Portfolio=${port_val:.2f}  drawdown={drawdown:.2f}%  "
                f"XRP=${xrp_price:.4f}  FLR=${flr_price:.5f}"
            )
            if ind_xrp:
                log.info(
                    f"[XRP] RSI={ind_xrp['rsi']:.1f}  "
                    f"EMA20={ind_xrp['ema']:.4f}  EMA50={ind_xrp['ema50']:.4f}  "
                    f"slope={'↑' if ind_xrp['ema'] > ind_xrp['ema_prev'] else '↓'}  "
                    f"ATR={ind_xrp['atr']:.4f}  struct={ind_xrp['structure']}  "
                    f"MACD={'↑' if ind_xrp['macd'].get('bullish') else '↓'}  "
                    f"POC={ind_xrp['vp_poc']:.4f}  regime={regime_xrp}  score={score_xrp:.1f}"
                )
            if ind_flr:
                log.info(
                    f"[FLR] RSI={ind_flr['rsi']:.1f}  "
                    f"EMA20={ind_flr['ema']:.5f}  EMA50={ind_flr['ema50']:.5f}  "
                    f"slope={'↑' if ind_flr['ema'] > ind_flr['ema_prev'] else '↓'}  "
                    f"ATR={ind_flr['atr']:.6f}  struct={ind_flr['structure']}  "
                    f"MACD={'↑' if ind_flr['macd'].get('bullish') else '↓'}  "
                    f"POC={ind_flr['vp_poc']:.5f}  regime={regime_flr}  score={score_flr:.1f}"
                )
            if score_xrp > 0 or score_flr > 0:
                leader = "XRP" if score_xrp >= score_flr else "FLR"
                log.info(
                    f"[RANK] XRP={score_xrp:.1f}(RS={rel_str_xrp:+.1f})  "
                    f"FLR={score_flr:.1f}(RS={rel_str_flr:+.1f})  → leader={leader}"
                )

            # =================================================================
            # MANAGE OPEN POSITIONS
            # =================================================================

            # ── Coinbase XRP long ─────────────────────────────────────────────
            if pos_xrp_cb["active"]:
                p       = pos_xrp_cb
                pnl_pct = (xrp_price - p["entry_price"]) / p["entry_price"] * 100

                if p["tp_order_id"] and cb_is_order_filled(p["tp_order_id"]):
                    actual_tp = cb_get_fill_price(p["tp_order_id"], p["tp_price"])
                    net_pnl   = ledger.close_trade(p.get("trade_id", ""), actual_tp, "TAKE-PROFIT", COINBASE_FEE)
                    log.info(f"[CB/XRP] TAKE PROFIT @ ${actual_tp:.4f} | pnl=${net_pnl:+.4f}  cumPnL=${ledger.running_pnl():+.4f}")
                    log_trade(exchange="coinbase", asset="XRP", side="SELL", price=actual_tp,
                              qty=p["qty"], reason="TAKE-PROFIT", entry_price=p["entry_price"],
                              net_pnl_usd=net_pnl, portfolio_after=port_val + net_pnl,
                              regime=p["entry_regime"], entry_time_utc=p["entry_time_utc"])
                    pos_xrp_cb = _empty_position("XRP", "coinbase")
                    rsi_exit_count["coinbase"] = 0

                else:
                    # ATR-based trailing: activate when pnl >= ATR_TRAIL_MULT × entry ATR
                    entry_atr = p.get("entry_atr", 0.0)
                    p["high_since_entry"] = max(p["high_since_entry"], xrp_price)

                    if entry_atr > 0:
                        trail_trigger_price = p["entry_price"] + ATR_TRAIL_MULT * entry_atr
                        if xrp_price >= trail_trigger_price:
                            p["trailing_active"] = True
                            trail_stop = p["high_since_entry"] - entry_atr  # 1×ATR below peak
                            breakeven  = p["entry_price"] * (1 + COINBASE_FEE * 2 * 1.2)
                            new_stop   = max(trail_stop, breakeven)
                            if new_stop > p["stop_price"]:
                                p["stop_price"] = new_stop
                    else:
                        # Fallback: pct-based trail
                        pnl_pct_trigger = float(os.getenv("TRAIL_TRIGGER_PCT", "1.5"))
                        if pnl_pct >= pnl_pct_trigger:
                            p["trailing_active"] = True
                            breakeven  = p["entry_price"] * (1 + COINBASE_FEE * 2 * 1.2)
                            trail_stop = p["high_since_entry"] * (1 - TRAIL_DIST / 100)
                            new_stop   = max(trail_stop, breakeven)
                            if new_stop > p["stop_price"]:
                                p["stop_price"] = new_stop

                    # RSI exit — PROFIT GATE: only if net_pnl > 0 after fees
                    exit_by_rsi = False
                    if ind_xrp:
                        rsi_exit_threshold = RSI_EXIT if xrp_price < ind_xrp["ema"] * 1.03 else RSI_EXIT + 12
                        if ind_xrp["rsi"] > rsi_exit_threshold:
                            rsi_exit_count["coinbase"] += 1
                        else:
                            rsi_exit_count["coinbase"] = 0
                        if rsi_exit_count["coinbase"] >= RSI_EXIT_CONFIRM:
                            # Check profit gate — only exit RSI if we're profitable after fees
                            profitable, net_check = ledger_profit_gate(
                                p["entry_price"], xrp_price, p["qty"], "LONG", COINBASE_FEE
                            )
                            if profitable:
                                exit_by_rsi = True
                            else:
                                log.info(
                                    f"[CB/XRP] RSI exit blocked by profit gate: "
                                    f"net_pnl=${net_check:.4f} — holding for TP"
                                )
                                rsi_exit_count["coinbase"] = 0  # reset, don't keep spamming

                    hit_sl = p["stop_price"] > 0 and xrp_price <= p["stop_price"]
                    if hit_sl or exit_by_rsi:
                        reason = "STOP-LOSS" if hit_sl else "RSI-EXIT"
                        log.info(f"[CB/XRP] {reason} | price=${xrp_price:.4f}  entry=${p['entry_price']:.4f}  pnl={pnl_pct:+.2f}%")
                        if p["tp_order_id"]:
                            try:
                                cb.cancel_orders(order_ids=[p["tp_order_id"]])
                            except Exception:
                                pass
                        time.sleep(2)
                        sold = cb_emergency_sell(p["qty"], xrp_price)
                        if sold:
                            exit_price = cb_get_mid_price("XRP-USDC") or xrp_price
                            net_pnl    = ledger.close_trade(p.get("trade_id", ""), exit_price, reason, COINBASE_FEE)
                            log_trade(exchange="coinbase", asset="XRP", side="SELL", price=exit_price,
                                      qty=p["qty"], reason=reason, entry_price=p["entry_price"],
                                      net_pnl_usd=net_pnl, portfolio_after=port_val + net_pnl,
                                      regime=p["entry_regime"], entry_time_utc=p["entry_time_utc"])
                            log.info(f"[CB/XRP] {reason} pnl=${net_pnl:+.4f}  cumPnL=${ledger.running_pnl():+.4f}")
                            if hit_sl:
                                sl_cooldown["coinbase"] = now + timedelta(seconds=SL_COOLDOWN_SEC)
                                asset_cooldown["XRP"]   = now + timedelta(seconds=SL_COOLDOWN_SEC)
                                log.info(f"[COORD] XRP cooldown set — Kraken skips XRP for {SL_COOLDOWN_SEC//60}min")
                            pos_xrp_cb = _empty_position("XRP", "coinbase")
                            rsi_exit_count["coinbase"] = 0

                    log.info(
                        f"[CB/XRP] pnl={pnl_pct:+.2f}%  stop=${p['stop_price']:.4f}  "
                        f"trail={'ON' if p['trailing_active'] else 'OFF'}  "
                        f"ATR={p.get('entry_atr', 0):.4f}"
                    )

            # ── Kraken position ───────────────────────────────────────────────
            if pos_kraken["active"]:
                p          = pos_kraken
                asset      = p["asset"]
                pair       = kraken.XRP_PAIR if asset == "XRP" else kraken.FLR_PAIR
                curr_price = xrp_price if asset == "XRP" else flr_price
                entry_atr  = p.get("entry_atr", 0.0)
                ind_curr   = ind_xrp if asset == "XRP" else ind_flr

                if curr_price <= 0:
                    pass
                elif p["direction"] == "LONG":
                    pnl_pct = (curr_price - p["entry_price"]) / p["entry_price"] * 100
                    p["high_since_entry"] = max(p["high_since_entry"], curr_price)

                    # TP order fill check
                    if p["tp_order_id"] and kraken.is_order_filled(p["tp_order_id"]):
                        fill_p  = kraken.get_fill_price(p["tp_order_id"], p["tp_price"])
                        net_pnl = ledger.close_trade(p.get("trade_id", ""), fill_p, "TAKE-PROFIT", kraken.KRAKEN_FEE)
                        log.info(f"[KR/{asset}] TAKE-PROFIT (order) @ ${fill_p:.5f} | pnl=${net_pnl:+.4f}  cumPnL=${ledger.running_pnl():+.4f}")
                        log_trade(exchange="kraken", asset=asset, side="SELL", price=fill_p,
                                  qty=p["qty"], reason="TAKE-PROFIT", entry_price=p["entry_price"],
                                  net_pnl_usd=net_pnl, portfolio_after=port_val + net_pnl,
                                  regime=p["entry_regime"], entry_time_utc=p["entry_time_utc"])
                        if asset == "FLR":
                            kr_reconvert_to_usdc(kraken.get_balances())
                        pos_kraken = _empty_position("XRP", "kraken")
                        rsi_exit_count["kraken"] = 0
                    else:
                        # ATR-based trailing
                        if entry_atr > 0:
                            trail_trigger_price = p["entry_price"] + ATR_TRAIL_MULT * entry_atr
                            if curr_price >= trail_trigger_price:
                                p["trailing_active"] = True
                                trail_stop = p["high_since_entry"] - entry_atr
                                if trail_stop > p["stop_price"]:
                                    p["stop_price"] = trail_stop
                        else:
                            if pnl_pct >= float(os.getenv("TRAIL_TRIGGER_PCT", "1.5")):
                                p["trailing_active"] = True
                                trail_stop = p["high_since_entry"] * (1 - TRAIL_DIST / 100)
                                if trail_stop > p["stop_price"]:
                                    p["stop_price"] = trail_stop

                        hit_sl  = p["stop_price"] > 0 and curr_price <= p["stop_price"]
                        hit_tp  = (not p["tp_order_id"]) and curr_price >= p["tp_price"]

                        # RSI exit with profit gate
                        exit_by_rsi = False
                        if ind_curr:
                            rsi_thr = RSI_EXIT if curr_price < ind_curr["ema"] * 1.03 else RSI_EXIT + 12
                            if ind_curr["rsi"] > rsi_thr:
                                rsi_exit_count["kraken"] += 1
                            else:
                                rsi_exit_count["kraken"] = 0
                            if rsi_exit_count["kraken"] >= RSI_EXIT_CONFIRM:
                                profitable, net_check = ledger_profit_gate(
                                    p["entry_price"], curr_price, p["qty"], "LONG", kraken.KRAKEN_FEE
                                )
                                if profitable:
                                    exit_by_rsi = True
                                else:
                                    log.info(
                                        f"[KR/{asset}] RSI exit blocked by profit gate: "
                                        f"net_pnl=${net_check:.4f} — holding"
                                    )
                                    rsi_exit_count["kraken"] = 0

                        if hit_sl or hit_tp or exit_by_rsi:
                            reason = "TAKE-PROFIT" if hit_tp else ("STOP-LOSS" if hit_sl else "RSI-EXIT")
                            if p["tp_order_id"] and (hit_sl or exit_by_rsi):
                                kraken.cancel_order(p["tp_order_id"])
                            close_price = round(curr_price * (0.995 if hit_sl else 1.0005), 5)
                            kraken.place_limit_sell(pair, p["qty"], close_price)
                            net_pnl = ledger.close_trade(p.get("trade_id", ""), curr_price, reason, kraken.KRAKEN_FEE)
                            log.info(f"[KR/{asset}] {reason} @ ${curr_price:.5f} | pnl=${net_pnl:+.4f}  cumPnL=${ledger.running_pnl():+.4f}")
                            log_trade(exchange="kraken", asset=asset, side="SELL", price=curr_price,
                                      qty=p["qty"], reason=reason, entry_price=p["entry_price"],
                                      net_pnl_usd=net_pnl, portfolio_after=port_val + net_pnl,
                                      regime=p["entry_regime"], entry_time_utc=p["entry_time_utc"])
                            if hit_sl:
                                sl_cooldown["kraken"]  = now + timedelta(seconds=SL_COOLDOWN_SEC)
                                asset_cooldown[asset]  = now + timedelta(seconds=SL_COOLDOWN_SEC)
                                log.info(f"[COORD] {asset} cooldown set — both agents skip {asset} for {SL_COOLDOWN_SEC//60}min")
                            if asset == "FLR":
                                time.sleep(3)
                                kr_reconvert_to_usdc(kraken.get_balances())
                            pos_kraken = _empty_position("XRP", "kraken")
                            rsi_exit_count["kraken"] = 0

                elif p["direction"] == "SHORT":
                    pnl_pct = (p["entry_price"] - curr_price) / p["entry_price"] * 100
                    hit_sl  = curr_price >= p["stop_price"] if p["stop_price"] > 0 else False
                    hit_tp  = curr_price <= p["tp_price"]   if p["tp_price"]   > 0 else False

                    # RSI exit for shorts: RSI < RSI_SHORT_EXIT = oversold = momentum exhausted
                    exit_by_rsi = False
                    if ind_curr:
                        if ind_curr["rsi"] < RSI_SHORT_EXIT:
                            rsi_exit_count["kraken"] += 1
                        else:
                            rsi_exit_count["kraken"] = 0
                        if rsi_exit_count["kraken"] >= RSI_EXIT_CONFIRM:
                            profitable, net_check = ledger_profit_gate(
                                p["entry_price"], curr_price, p["qty"], "SHORT", kraken.KRAKEN_FEE
                            )
                            if profitable:
                                exit_by_rsi = True
                            else:
                                log.info(
                                    f"[KR/{asset} SHORT] RSI exit blocked by profit gate: "
                                    f"net_pnl=${net_check:.4f} — holding"
                                )
                                rsi_exit_count["kraken"] = 0

                    if hit_sl or hit_tp or exit_by_rsi:
                        reason      = "TAKE-PROFIT" if hit_tp else ("STOP-LOSS" if hit_sl else "RSI-EXIT")
                        cover_price = round(curr_price * (1.001 if not hit_sl else 1.005), 5)
                        kraken.close_short(pair, p["qty"], cover_price)
                        net_pnl = ledger.close_trade(p.get("trade_id", ""), curr_price, reason, kraken.KRAKEN_FEE)
                        log.info(f"[KR/{asset} SHORT] {reason} pnl=${net_pnl:+.4f}  cumPnL=${ledger.running_pnl():+.4f}")
                        log_trade(exchange="kraken", asset=asset, side="COVER", price=curr_price,
                                  qty=p["qty"], reason=reason, entry_price=p["entry_price"],
                                  net_pnl_usd=net_pnl, portfolio_after=port_val + net_pnl,
                                  regime=p["entry_regime"], entry_time_utc=p["entry_time_utc"])
                        if hit_sl:
                            sl_cooldown["kraken"]  = now + timedelta(seconds=SL_COOLDOWN_SEC)
                            asset_cooldown[asset]  = now + timedelta(seconds=SL_COOLDOWN_SEC)
                            log.info(f"[COORD] {asset} cooldown set (SHORT SL) — {SL_COOLDOWN_SEC//60}min")
                        pos_kraken = _empty_position("XRP", "kraken")
                        rsi_exit_count["kraken"] = 0

                if pos_kraken.get("active"):
                    log.info(f"[KR/{p['asset']}/{p['direction']}] pnl={pnl_pct:+.2f}%  stop=${p['stop_price']:.5f}  trail={'ON' if p['trailing_active'] else 'OFF'}")

            # =================================================================
            # SCAN FOR NEW ENTRIES
            # =================================================================

            # ── Coinbase XRP long entry ───────────────────────────────────────
            if not pos_xrp_cb["active"] and now >= sl_cooldown["coinbase"] and now >= asset_cooldown["XRP"]:
                should_buy_xrp = (
                    ind_xrp is not None and
                    score_xrp >= MIN_ENTRY_SCORE and
                    regime_xrp in ("RANGING", "UPTREND")
                )
                if should_buy_xrp:
                    cb_usdc, cb_xrp = cb_get_balances()
                    deploy = round(min(cb_usdc * POSITION_PCT, TOTAL_CAPITAL * POSITION_PCT), 2)
                    ok, block_reason = pre_trade_risk_check(
                        "XRP", xrp_price, deploy, cb_usdc, port_val, ind_xrp, rs_xrp
                    )
                    if not ok:
                        log.info(f"[CB/XRP] Entry blocked by risk gate: {block_reason}")
                        should_buy_xrp = False

                if should_buy_xrp:
                    cb_usdc, _ = cb_get_balances()
                    deploy = round(min(cb_usdc * POSITION_PCT, TOTAL_CAPITAL * POSITION_PCT), 2)
                    if deploy >= 5.0:
                        log.info(f"[CB/XRP] BUY | score={score_xrp:.1f}  regime={regime_xrp}  RSI={ind_xrp['rsi']:.1f}  ATR={ind_xrp['atr']:.4f}  struct={ind_xrp['structure']}  deploy=${deploy:.2f}")
                        oid, buy_price, qty = cb_place_limit_buy(deploy, xrp_price)
                        if oid:
                            filled = False
                            for _ in range(90):
                                time.sleep(1)
                                if cb_is_order_filled(oid):
                                    filled = True
                                    break
                            if filled:
                                entry_price = cb_get_fill_price(oid, buy_price)
                                stop_price, tp_price = compute_atr_sl_tp(
                                    entry_price, "LONG", ind_xrp, SL_PCT, TP_PCT, decimals=4
                                )
                                tp_oid   = cb_place_tp(qty, tp_price)
                                trade_id = ledger.open_trade("coinbase", "XRP", "LONG", entry_price, qty, regime_xrp)
                                pos_xrp_cb = {
                                    "active": True, "asset": "XRP", "exchange": "coinbase",
                                    "direction": "LONG", "entry_price": entry_price, "qty": qty,
                                    "tp_order_id": tp_oid, "tp_price": tp_price,
                                    "stop_price": stop_price, "high_since_entry": entry_price,
                                    "trailing_active": False, "entry_time_utc": now.isoformat(),
                                    "entry_regime": regime_xrp, "resumed": False,
                                    "trade_id": trade_id, "entry_atr": ind_xrp.get("atr", 0.0),
                                }
                                log.info(f"[CB/XRP] Open @ ${entry_price:.4f} | TP=${tp_price:.4f}  SL=${stop_price:.4f}  ATR={ind_xrp['atr']:.4f}  tid={trade_id}")
                                log_trade(exchange="coinbase", asset="XRP", side="BUY",
                                          price=entry_price, qty=qty, reason="ENTRY",
                                          entry_price=entry_price, portfolio_after=port_val,
                                          regime=regime_xrp, entry_time_utc=now.isoformat())
                            else:
                                log.warning("[CB/XRP] Buy not filled in 90s — cancelling")
                                try:
                                    cb.cancel_orders(order_ids=[oid])
                                except Exception:
                                    pass

            # ── Kraken: unified opportunity ranker ────────────────────────────
            if not pos_kraken["active"] and kraken_ok and now >= sl_cooldown["kraken"]:
                kr_bals  = kraken.get_balances()
                kr_usdc  = kr_bals.get("USDC", 0.0)
                kr_usd   = kr_bals.get("USD",  0.0)
                kr_total = kr_usdc + kr_usd

                candidates = []  # (name, score, asset, direction, price, deploy, ind)

                if kr_total >= 5.0:
                    deploy_xrp = round(min(kr_usdc  * POSITION_PCT,     TOTAL_CAPITAL * POSITION_PCT), 2)
                    deploy_flr = round(min(kr_total  * FLR_POSITION_PCT, TOTAL_CAPITAL * FLR_POSITION_PCT), 2)

                    # XRP LONG
                    if (ind_xrp and xrp_price > 0 and score_xrp >= MIN_ENTRY_SCORE
                            and regime_xrp in ("RANGING", "UPTREND")
                            and now >= asset_cooldown["XRP"]):
                        candidates.append(("XRP_LONG",  score_xrp, "XRP", "LONG",  xrp_price, deploy_xrp, ind_xrp, SL_PCT))
                    elif now < asset_cooldown["XRP"] and score_xrp >= MIN_ENTRY_SCORE:
                        rem = int((asset_cooldown["XRP"] - now).total_seconds() / 60)
                        log.info(f"[COORD] XRP cooldown ({rem}min left) — skipping XRP_LONG")

                    # FLR LONG
                    if (flr_trading_ok and ind_flr and flr_price > 0 and score_flr >= MIN_ENTRY_SCORE
                            and regime_flr in ("RANGING", "UPTREND")
                            and now >= asset_cooldown["FLR"]):
                        candidates.append(("FLR_LONG",  score_flr, "FLR", "LONG",  flr_price, deploy_flr, ind_flr, FLR_SL_PCT))
                    elif now < asset_cooldown["FLR"] and score_flr >= MIN_ENTRY_SCORE:
                        rem = int((asset_cooldown["FLR"] - now).total_seconds() / 60)
                        log.info(f"[COORD] FLR cooldown ({rem}min left) — skipping FLR_LONG")

                    if SHORTING_ENABLED:
                        # XRP SHORT — STRONG_DOWNTREND + bearish continuation signals
                        # Gate: structure BEARISH or NEUTRAL, MACD bearish, score >= MIN_ENTRY_SCORE
                        # Note: RSI>65 gate REMOVED — downtrend continuation ≠ overbought bounce
                        xrp_short_ok = (
                            ind_xrp and xrp_price > 0 and
                            regime_xrp == "STRONG_DOWNTREND" and
                            score_xrp >= MIN_ENTRY_SCORE and
                            (not ind_xrp["macd"].get("bullish", True)) and   # MACD bearish
                            ind_xrp.get("structure") in ("BEARISH", "NEUTRAL") and
                            now >= asset_cooldown["XRP"]
                        )
                        if xrp_short_ok:
                            candidates.append(("XRP_SHORT", score_xrp, "XRP", "SHORT", xrp_price, deploy_xrp, ind_xrp, SL_PCT))
                        elif ind_xrp and regime_xrp == "STRONG_DOWNTREND" and ind_xrp["macd"].get("bullish", True):
                            log.info(f"[SHORT] XRP short skipped — MACD bullish (no confirmation)")

                        # FLR SHORT — same logic
                        flr_short_ok = (
                            flr_trading_ok and ind_flr and flr_price > 0 and
                            regime_flr == "STRONG_DOWNTREND" and
                            score_flr >= MIN_ENTRY_SCORE and
                            (not ind_flr["macd"].get("bullish", True)) and
                            ind_flr.get("structure") in ("BEARISH", "NEUTRAL") and
                            now >= asset_cooldown["FLR"]
                        )
                        if flr_short_ok:
                            candidates.append(("FLR_SHORT", score_flr, "FLR", "SHORT", flr_price, deploy_flr, ind_flr, FLR_SL_PCT))
                        elif ind_flr and regime_flr == "STRONG_DOWNTREND" and ind_flr["macd"].get("bullish", True):
                            log.info(f"[SHORT] FLR short skipped — MACD bullish")

                if candidates:
                    candidates.sort(key=lambda x: x[1], reverse=True)
                    best_name, best_score, best_asset, best_dir, best_price, best_deploy, best_ind, best_sl = candidates[0]
                    others = [(c[0], round(c[1], 1)) for c in candidates[1:]]
                    log.info(f"[KR] Opportunity rank: {best_name}={best_score:.1f}" +
                             (f"  | also: {others}" if others else ""))

                    # Pre-trade risk gate
                    avail_for_trade = kr_usdc if best_asset == "XRP" else kr_total
                    ok, block_reason = pre_trade_risk_check(
                        best_asset, best_price, best_deploy, avail_for_trade, port_val, best_ind,
                        rs_xrp if best_asset == "XRP" else rs_flr
                    )
                    if not ok:
                        log.info(f"[KR] {best_name} blocked by risk gate: {block_reason}")
                        candidates = []

                if candidates:
                    best_name, best_score, best_asset, best_dir, best_price, best_deploy, best_ind, best_sl = candidates[0]
                    pair      = kraken.XRP_PAIR if best_asset == "XRP" else kraken.FLR_PAIR
                    asset_reg = regime_xrp if best_asset == "XRP" else regime_flr
                    qty       = round((best_deploy / best_price) * (1 - kraken.KRAKEN_FEE), 4)

                    if best_asset == "FLR" and best_deploy >= 5.0:
                        if not kr_ensure_usd(best_deploy, kr_bals):
                            log.warning(f"[KR] FLR trade skipped — USDC→USD conversion failed")
                            qty = 0

                    if qty >= 1.0 and best_deploy >= 5.0:
                        if best_dir == "LONG":
                            raw_entry = round(best_price * 1.001, 5)
                            stop_price, tp_price = compute_atr_sl_tp(
                                raw_entry, "LONG", best_ind, best_sl, TP_PCT
                            )
                            oid      = kraken.place_limit_buy(pair, qty, raw_entry)
                            side_log = "BUY"
                        else:
                            raw_entry = round(best_price * 0.999, 5)
                            stop_price, tp_price = compute_atr_sl_tp(
                                raw_entry, "SHORT", best_ind, best_sl, TP_PCT
                            )
                            oid      = kraken.place_short(pair, qty, raw_entry, SHORT_LEVERAGE)
                            side_log = "SHORT"

                        if oid:
                            filled = False
                            log.info(f"[KR/{best_asset}/{best_dir}] Order {oid} placed — polling fill (up to 90s)...")
                            for _ in range(18):   # 18 × 5s = 90s — stays within Kraken rate limits
                                time.sleep(5)
                                if kraken.is_order_filled(oid):
                                    filled = True
                                    break
                            if not filled:
                                log.warning(f"[KR/{best_asset}/{best_dir}] Not filled in 90s — cancelling")
                                kraken.cancel_order(oid)
                            else:
                                entry_price = kraken.get_fill_price(oid, raw_entry)
                                # Recalculate using confirmed fill price
                                stop_price, tp_price = compute_atr_sl_tp(
                                    entry_price, best_dir, best_ind, best_sl, TP_PCT
                                )
                                tp_oid = ""
                                if best_dir == "LONG":
                                    tp_oid = kraken.place_limit_sell(pair, qty, tp_price)
                                    if not tp_oid:
                                        log.warning(f"[KR/{best_asset}] TP order failed — price-based exit only")
                                # Shorts: TP managed via price check (cover order)

                                trade_id   = ledger.open_trade("kraken", best_asset, best_dir, entry_price, qty, asset_reg)
                                pos_kraken = {
                                    "active": True, "asset": best_asset, "exchange": "kraken",
                                    "direction": best_dir, "entry_price": entry_price, "qty": qty,
                                    "tp_order_id": tp_oid, "tp_price": tp_price, "stop_price": stop_price,
                                    "high_since_entry": entry_price, "trailing_active": False,
                                    "entry_time_utc": now.isoformat(), "entry_regime": asset_reg,
                                    "resumed": False, "trade_id": trade_id,
                                    "entry_atr": best_ind.get("atr", 0.0),
                                }
                                log.info(
                                    f"[KR/{best_asset}/{best_dir}] Fill @ ${entry_price:.5f} | "
                                    f"TP=${tp_price:.5f}  SL=${stop_price:.5f}  "
                                    f"ATR={best_ind.get('atr', 0):.5f}  "
                                    f"POC={best_ind.get('vp_poc', 0):.5f}  "
                                    f"score={best_score:.1f}  tid={trade_id}"
                                )
                                log_trade(exchange="kraken", asset=best_asset, side=side_log,
                                          price=entry_price, qty=qty,
                                          reason=f"ENTRY-{best_dir}",
                                          entry_price=entry_price, portfolio_after=port_val,
                                          regime=asset_reg, entry_time_utc=now.isoformat())
                else:
                    if kr_total >= 5.0:
                        log.info(
                            f"[KR] No opportunity — "
                            f"kr_usdc=${kr_usdc:.2f}  "
                            f"XRP={regime_xrp}/score={score_xrp:.1f}  "
                            f"FLR={regime_flr}/score={score_flr:.1f}"
                        )

            # ── Save state every loop ─────────────────────────────────────────
            save_state({
                "pos_xrp_cb":           pos_xrp_cb,
                "pos_kraken":            pos_kraken,
                "sl_cooldown_coinbase":  sl_cooldown["coinbase"].isoformat(),
                "sl_cooldown_kraken":    sl_cooldown["kraken"].isoformat(),
                "asset_cooldown_XRP":    asset_cooldown["XRP"].isoformat(),
                "asset_cooldown_FLR":    asset_cooldown["FLR"].isoformat(),
                "start_val":             start_val,
            })

        except KeyboardInterrupt:
            log.info("Controller stopped by user")
            break
        except Exception as e:
            log.error(f"Controller loop error: {e}", exc_info=True)

        time.sleep(LOOP_SLEEP)


if __name__ == "__main__":
    main()
