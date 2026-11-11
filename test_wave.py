"""Unit checks for the locked arithmetic. Run before any deploy."""
from decimal import Decimal
import wave_engine as we

def check(name, cond):
    print(("PASS " if cond else "FAIL ") + name)
    if not cond:
        raise SystemExit(1)

# previous_top 1.70 — the live seed
check("buy22", we.buy22_price(1.7) == Decimal("1.32600"))
check("add30", we.add30_price(1.7) == Decimal("1.19000"))
check("sell at 1.70", we.sell_price(1.7) == Decimal("1.51300"))
check("not a new top", we.is_new_top(1.65452, 1.7) is False)
check("is a new top", we.is_new_top(1.70001, 1.7) is True)
check("equal is not a top", we.is_new_top(1.7, 1.7) is False)

# the live sleeve
check("qty at 1.3363", we.qty_for("278.40", "1.3363") > 0)

# floor
check("spendable under floor", we.spendable(100) == 0)
check("spendable above", we.spendable(139.0081) == Decimal("29.0081"))

# D1 — a failed OHLC read must not raise the high
check("ohlc none keeps sample",
      we.update_run_high(1.60, 1.61, None) == Decimal("1.61"))
check("ohlc raises",
      we.update_run_high(1.60, 1.61, 1.6577) == Decimal("1.6577"))

# brake
check("brake at 3", we.brake_engaged(3) is True)
check("brake under", we.brake_engaged(1) is False)

# observer default
check("unclear", we.classify(0.3, 10, 2) == "UNCLEAR")
check("trend", we.classify(0.62, 29, 15) == "TREND")

print("all locked checks passed")
