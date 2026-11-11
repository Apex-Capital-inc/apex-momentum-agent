"""
halt.py — WAVE emergency stop
=============================
Two levels. Both refuse to trade. Neither sells the sleeve.

SOFT  no new orders, no cancels, process exits.
      Use when the logic looks wrong but the keys are still yours.

HARD  cancel every open XRPUSDC order, then exit.
      Use when the agent or the keys may be compromised.
      Does NOT market-sell. An attacker must not be able to dump via halt.
"""
from __future__ import annotations

import os
from pathlib import Path

HALT_FILE = Path(os.getenv("WAVE_HALT_FILE", "/data/HALT"))


def requested() -> str | None:
    """
    Returns 'soft', 'hard', or None.
    A file beats the env var. Contents: SOFT or HARD. Empty file = HARD.
    """
    if HALT_FILE.exists():
        text = HALT_FILE.read_text().strip().upper()
        return "soft" if text == "SOFT" else "hard"
    env = os.getenv("WAVE_HALT", "").strip().upper()
    if env in ("SOFT", "HARD"):
        return env.lower()
    if env in ("1", "TRUE", "YES"):
        return "hard"
    return None


def engage(k, st: dict, level: str, log) -> None:
    st["halted"] = True
    st["halt_level"] = level
    log.critical("[HALT] %s — no new orders, sleeve is NOT sold", level.upper())

    if level == "hard":
        try:
            orders = k.open_orders()
        except Exception as e:
            log.error("[HALT] could not list orders: %s", e)
            orders = []
        for o in orders:
            if "XRP" not in o.get("pair", "").upper():
                continue
            log.critical("[HALT] cancel %s %s @ %s id=%s",
                         o["side"], o["volume"], o["price"], o["order_id"])
            try:
                k.cancel(o["order_id"])
            except Exception as e:
                log.error("[HALT] cancel failed %s: %s", o["order_id"], e)
        w = st.get("wave", {})
        for key in ("buy_oid", "add_oid", "sell_oid"):
            w[key] = ""

    # Caller saves state and exits. We do not loop again.
