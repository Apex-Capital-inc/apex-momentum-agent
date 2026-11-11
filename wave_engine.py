"""
wave_engine.py — pure functions for APEX WAVE v5.1
==================================================
No I/O. Decimal arithmetic. Constants are locked.

Sell only after a new top, at run_high × 0.89.
Buy 2/3 at previous_top × 0.78. Add 1/3 at previous_top × 0.70.
$110 absolute floor. Max 3 down-cycles. Door E off.
"""
from __future__ import annotations

from decimal import Decimal, ROUND_DOWN, ROUND_UP, getcontext

getcontext().prec = 28

# ── locked ──────────────────────────────────────────────────────────────────
SELL_MULT = Decimal("0.89")       # −11% from the new top
BUY22_MULT = Decimal("0.78")      # −22% from previous_top
ADD30_MULT = Decimal("0.70")      # −30% from previous_top
DEPLOY_PCT = Decimal("0.6667")    # 2/3 of spendable
RESERVE_USD_MIN = Decimal("110")  # absolute, never a percent
MAX_DOWN_CYCLES = 3
DOOR_E = False
FEE = Decimal("0.0040")           # maker
TAKER_FEE = Decimal("0.0080")
PX_DP = Decimal("0.00001")        # 5 decimal places
QTY_DP = Decimal("0.0001")        # 4 decimal places
DUST_XRP = Decimal("0.01")


def D(x) -> Decimal:
    if isinstance(x, Decimal):
        return x
    return Decimal(str(x))


def _px(value: Decimal, rounding) -> Decimal:
    return value.quantize(PX_DP, rounding=rounding)


def _qty(value: Decimal) -> Decimal:
    return value.quantize(QTY_DP, rounding=ROUND_DOWN)


def buy22_price(previous_top) -> Decimal:
    """Rebuy level. Rounded DOWN so the bid never posts above its level."""
    return _px(D(previous_top) * BUY22_MULT, ROUND_DOWN)


def add30_price(previous_top) -> Decimal:
    """Lower-low door. Rounded DOWN."""
    return _px(D(previous_top) * ADD30_MULT, ROUND_DOWN)


def sell_price(run_high) -> Decimal:
    """Flatten level once a new top exists. Rounded UP so it never posts below."""
    return _px(D(run_high) * SELL_MULT, ROUND_UP)


def is_new_top(run_high, previous_top) -> bool:
    """Strict greater-than. Equal does not arm."""
    return D(run_high) > D(previous_top)


def spendable(cash) -> Decimal:
    """Cash above the absolute floor. Never negative."""
    return max(Decimal("0"), D(cash) - RESERVE_USD_MIN)


def buy22_usd(cash) -> Decimal:
    """Two-thirds of spendable. The rest waits for the −30% door."""
    return (spendable(cash) * DEPLOY_PCT).quantize(Decimal("0.0001"), rounding=ROUND_DOWN)


def add30_usd(cash) -> Decimal:
    """Everything above the floor, after the −22% fill has reserved its own cash."""
    return spendable(cash)


def qty_for(usd, price) -> Decimal:
    """
    Coins a maker buy of `usd` can take at `price`.
    Gross + maker fee must fit inside usd.
    """
    px = D(price)
    if px <= 0 or D(usd) <= 0:
        return Decimal("0")
    return _qty(D(usd) / (px * (Decimal("1") + FEE)))


def round_trip_be(entry) -> Decimal:
    """Price that nets zero after a maker entry and a taker exit."""
    return _px(D(entry) * (Decimal("1") + FEE) / (Decimal("1") - TAKER_FEE), ROUND_UP)


def update_run_high(current, sample, ohlc_high=None) -> Decimal:
    """
    D1. run_high is the max of what we have already seen, the 60s sample,
    and Kraken's own interval high. A failed OHLC read must pass None —
    never invent a high.
    """
    best = D(current or 0)
    best = max(best, D(sample or 0))
    if ohlc_high is not None:
        best = max(best, D(ohlc_high))
    return best


def brake_engaged(down_cycles) -> bool:
    return int(down_cycles) >= MAX_DOWN_CYCLES


def classify(er10, band_pct, net_pct) -> str:
    """
    Observer only. Default UNCLEAR.
    RANGE only on a tight, directionless tape.
    TREND only when efficiency and net and band all agree.
    """
    er = D(er10)
    band = D(band_pct)
    net = abs(D(net_pct))
    if er < Decimal("0.15") and band > net * 2:
        return "RANGE"
    if er > Decimal("0.45") and net > Decimal("5") and band > Decimal("8"):
        return "TREND"
    return "UNCLEAR"
