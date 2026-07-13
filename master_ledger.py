"""
Master Ledger
=============
Single source of truth for ALL trades across ALL agents (Coinbase + Kraken).

One row per trade. Each trade has a lifecycle:
  OPEN   → written when position opens (entry_price, qty, regime, etc.)
  CLOSED → updated when position closes (exit_price, reason, P&L, cumulative)

Both agents read from and write to this ledger.
The controller queries it before entering to avoid repeating losing setups.

File: /data/master_ledger.csv   (persists on Railway Volume)

Columns:
  trade_id         unique ID (hex)
  exchange         coinbase / kraken
  asset            XRP / FLR
  direction        LONG / SHORT
  entry_time_utc   ISO timestamp
  entry_price      float
  entry_regime     RANGING / UPTREND / STRONG_DOWNTREND
  qty              float
  exit_time_utc    ISO timestamp or ""
  exit_price       float or 0.0
  exit_reason      TAKE-PROFIT / STOP-LOSS / RSI-EXIT / OPEN
  gross_pnl_usd    float (before fees)
  fees_usd         float
  net_pnl_usd      float (after fees)
  net_pnl_pct      float (% of deployed capital)
  cumulative_pnl   float (running total since inception)
  status           OPEN / CLOSED
"""

import os, csv, uuid, logging
from datetime import datetime, timezone
from typing import Optional

log = logging.getLogger("ledger")

DATA_DIR  = os.getenv("DATA_DIR", "/data")
LEDGER    = os.path.join(DATA_DIR, "master_ledger.csv")

COLUMNS = [
    "trade_id", "exchange", "asset", "direction",
    "entry_time_utc", "entry_price", "entry_regime", "qty",
    "exit_time_utc", "exit_price", "exit_reason",
    "gross_pnl_usd", "fees_usd", "net_pnl_usd", "net_pnl_pct",
    "cumulative_pnl", "status",
]


# ─────────────────────────────────────────────────────────────────────────────
# Internal helpers
# ─────────────────────────────────────────────────────────────────────────────

def _ensure_file():
    os.makedirs(DATA_DIR, exist_ok=True)
    if not os.path.exists(LEDGER):
        with open(LEDGER, "w", newline="") as f:
            csv.writer(f).writerow(COLUMNS)


def _read_all() -> list[dict]:
    _ensure_file()
    with open(LEDGER, newline="") as f:
        return list(csv.DictReader(f))


def _write_all(rows: list[dict]):
    _ensure_file()
    with open(LEDGER, "w", newline="") as f:
        w = csv.DictWriter(f, fieldnames=COLUMNS)
        w.writeheader()
        w.writerows(rows)


def _now() -> str:
    return datetime.now(timezone.utc).strftime("%Y-%m-%d %H:%M:%S")


def _running_pnl(rows: list[dict]) -> float:
    total = 0.0
    for r in rows:
        if r["status"] == "CLOSED":
            try:
                total += float(r["net_pnl_usd"] or 0)
            except ValueError:
                pass
    return round(total, 4)


# ─────────────────────────────────────────────────────────────────────────────
# Write operations
# ─────────────────────────────────────────────────────────────────────────────

def open_trade(
    exchange: str,
    asset: str,
    direction: str,
    entry_price: float,
    qty: float,
    regime: str,
) -> str:
    """
    Record a new open position. Returns trade_id.
    Call this the moment the entry order is confirmed filled.
    """
    _ensure_file()
    trade_id = uuid.uuid4().hex[:12]
    row = {
        "trade_id":       trade_id,
        "exchange":       exchange,
        "asset":          asset,
        "direction":      direction,
        "entry_time_utc": _now(),
        "entry_price":    round(entry_price, 6),
        "entry_regime":   regime,
        "qty":            round(qty, 4),
        "exit_time_utc":  "",
        "exit_price":     "",
        "exit_reason":    "OPEN",
        "gross_pnl_usd":  "",
        "fees_usd":       "",
        "net_pnl_usd":    "",
        "net_pnl_pct":    "",
        "cumulative_pnl": "",
        "status":         "OPEN",
    }
    with open(LEDGER, "a", newline="") as f:
        csv.DictWriter(f, fieldnames=COLUMNS).writerow(row)

    log.info(
        f"[LEDGER] OPEN  {trade_id}  {exchange}/{asset}/{direction} "
        f"@ ${entry_price:.5f}  qty={qty:.4f}  regime={regime}"
    )
    return trade_id


def close_trade(
    trade_id: str,
    exit_price: float,
    exit_reason: str,
    fee_rate: float,        # e.g. 0.004 for Coinbase, 0.0026 for Kraken
) -> float:
    """
    Mark a trade as CLOSED. Computes gross P&L, fees, net P&L, updates cumulative.
    Returns net_pnl_usd.
    """
    rows = _read_all()
    cum  = _running_pnl(rows)

    net_pnl = 0.0
    found   = False
    for row in rows:
        if row["trade_id"] == trade_id and row["status"] == "OPEN":
            found = True
            entry = float(row["entry_price"])
            qty   = float(row["qty"])
            dirn  = row["direction"]

            if dirn == "LONG":
                gross = (exit_price - entry) * qty
            else:  # SHORT
                gross = (entry - exit_price) * qty

            fees    = (entry + exit_price) * qty * fee_rate
            net_pnl = round(gross - fees, 4)
            pct     = round(net_pnl / (entry * qty) * 100, 2) if entry and qty else 0.0
            cum     = round(cum + net_pnl, 4)

            row.update({
                "exit_time_utc":  _now(),
                "exit_price":     round(exit_price, 6),
                "exit_reason":    exit_reason,
                "gross_pnl_usd":  round(gross, 4),
                "fees_usd":       round(fees, 4),
                "net_pnl_usd":    net_pnl,
                "net_pnl_pct":    pct,
                "cumulative_pnl": cum,
                "status":         "CLOSED",
            })
            break

    if not found:
        log.warning(f"[LEDGER] close_trade: trade_id {trade_id} not found or already closed")
        return 0.0

    _write_all(rows)
    log.info(
        f"[LEDGER] CLOSE {trade_id}  exit=${exit_price:.5f}  "
        f"reason={exit_reason}  net=${net_pnl:+.4f}  cumulative=${cum:+.4f}"
    )
    return net_pnl


# ─────────────────────────────────────────────────────────────────────────────
# Read operations (queried before each entry decision)
# ─────────────────────────────────────────────────────────────────────────────

def get_open_trade(exchange: str, asset: str) -> Optional[dict]:
    """
    Returns the open trade row for this exchange+asset, or None.
    Used at boot to detect orphaned positions.
    """
    for row in _read_all():
        if row["status"] == "OPEN" and row["exchange"] == exchange and row["asset"] == asset:
            return row
    return None


def running_pnl() -> float:
    """Total net P&L across all closed trades since inception."""
    return _running_pnl(_read_all())


def recent_trades(asset: str = None, n: int = 20) -> list[dict]:
    """
    Last N closed trades, optionally filtered by asset.
    Most recent first.
    """
    rows = [r for r in _read_all() if r["status"] == "CLOSED"]
    if asset:
        rows = [r for r in rows if r["asset"] == asset]
    return rows[-n:][::-1]


def zone_risk(asset: str, price: float, radius_pct: float = 1.5) -> dict:
    """
    Look at all closed trades for this asset where entry_price was within
    radius_pct% of the current price. Return win/loss counts and net P&L.

    Used before entering: if zone has 2+ losses and no wins → skip or reduce size.

    Returns: {"trades": int, "wins": int, "losses": int, "net_pnl": float}
    """
    lo = price * (1 - radius_pct / 100)
    hi = price * (1 + radius_pct / 100)
    trades = wins = losses = 0
    net    = 0.0
    for row in _read_all():
        if row["status"] != "CLOSED" or row["asset"] != asset:
            continue
        try:
            ep  = float(row["entry_price"])
            pnl = float(row["net_pnl_usd"] or 0)
        except ValueError:
            continue
        if lo <= ep <= hi:
            trades += 1
            net    += pnl
            if pnl >= 0:
                wins   += 1
            else:
                losses += 1
    return {"trades": trades, "wins": wins, "losses": losses, "net_pnl": round(net, 4)}


def regime_stats(asset: str, regime: str) -> dict:
    """
    Win rate and average P&L for this asset+regime combination.
    Used to weight opportunity scores: good regime history → higher confidence.

    Returns: {"trades": int, "wins": int, "win_rate": float, "avg_pnl": float}
    """
    rows  = [r for r in _read_all()
             if r["status"] == "CLOSED" and r["asset"] == asset and r["entry_regime"] == regime]
    if not rows:
        return {"trades": 0, "wins": 0, "win_rate": 0.0, "avg_pnl": 0.0}
    wins     = sum(1 for r in rows if float(r["net_pnl_usd"] or 0) >= 0)
    total_pnl = sum(float(r["net_pnl_usd"] or 0) for r in rows)
    return {
        "trades":   len(rows),
        "wins":     wins,
        "win_rate": round(wins / len(rows) * 100, 1),
        "avg_pnl":  round(total_pnl / len(rows), 4),
    }


def current_streak() -> dict:
    """
    Current consecutive win or loss streak across ALL assets.
    Returns: {"count": int, "type": "WIN" / "LOSS" / "NONE"}
    """
    rows = [r for r in _read_all() if r["status"] == "CLOSED"]
    if not rows:
        return {"count": 0, "type": "NONE"}
    streak = 0
    kind   = None
    for row in reversed(rows):
        try:
            pnl = float(row["net_pnl_usd"] or 0)
        except ValueError:
            break
        this = "WIN" if pnl >= 0 else "LOSS"
        if kind is None:
            kind = this
        if this != kind:
            break
        streak += 1
    return {"count": streak, "type": kind or "NONE"}


def summary() -> dict:
    """
    Quick portfolio summary for boot log and heartbeat.
    """
    rows   = _read_all()
    closed = [r for r in rows if r["status"] == "CLOSED"]
    open_  = [r for r in rows if r["status"] == "OPEN"]
    wins   = sum(1 for r in closed if float(r.get("net_pnl_usd") or 0) >= 0)
    cum    = _running_pnl(rows)
    return {
        "total_trades":  len(closed),
        "open_trades":   len(open_),
        "wins":          wins,
        "losses":        len(closed) - wins,
        "win_rate":      round(wins / len(closed) * 100, 1) if closed else 0.0,
        "cumulative_pnl": cum,
        "streak":        current_streak(),
    }
