"""
wave_harvester.py — APEX WAVE v5.1 main loop
============================================
One sleeve. One venue. No second strategy.

States
  BUY_WORKING   a −22% maker bid is resting
  IN_XRP        sleeve is held; sell does not exist until a new top
  IN_CASH       flattened; next bid is previous_top × 0.78

The $110 floor is never spent.
The −30% add is placed only after the −22% fill.
A sell flattens the whole sleeve, add included.
"""
from __future__ import annotations

import json
import logging
import os
import time
from decimal import Decimal
from pathlib import Path

import wave_engine as we
from kraken_client import Kraken, KrakenError, ASSET

log = logging.getLogger("wave")
logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)s %(message)s")

STATE_PATH = Path(os.getenv("WAVE_STATE", "/data/dh_state.json"))
LOOP_SEC = int(os.getenv("DH_LOOP_SEC", "60"))
DRY_RUN = os.getenv("DH_DRY_RUN", "false").lower() == "true"


def load() -> dict:
    if not STATE_PATH.exists():
        seed_json = os.getenv("WAVE_SEED", "")
        if seed_json:
            log.info("[BOOT] no state file — seeding from WAVE_SEED env var")
            STATE_PATH.parent.mkdir(parents=True, exist_ok=True)
            STATE_PATH.write_text(seed_json)
        else:
            raise SystemExit(f"no state at {STATE_PATH} and no WAVE_SEED — refuse to invent a book")
    return json.loads(STATE_PATH.read_text())


def save(st: dict) -> None:
    tmp = STATE_PATH.with_suffix(".json.tmp")
    tmp.write_text(json.dumps(st, indent=2))
    tmp.replace(STATE_PATH)


def xrp_of(balances: dict) -> float:
    for key in ("XXRP", "XRP"):
        if key in balances:
            return float(balances[key])
    return 0.0


def usdc_of(balances: dict) -> float:
    for key in ("USDC", "ZUSD"):
        if key in balances:
            return float(balances[key])
    return 0.0


def keep(order: dict, w: dict) -> bool:
    """
    Boot keep-set. Match pair + side + price, not oid alone.
    A hand-placed order the book has never seen still survives
    if its price is the add or the buy we expect.
    """
    side = order["side"]
    px = Decimal(str(order["price"]))
    if side == "buy" and w.get("add_px") and abs(px - Decimal(str(w["add_px"]))) < Decimal("0.00002"):
        return True
    if side == "buy" and w.get("buy_px") and abs(px - Decimal(str(w["buy_px"]))) < Decimal("0.00002"):
        return True
    if side == "sell" and w.get("sell_px") and abs(px - Decimal(str(w["sell_px"]))) < Decimal("0.00002"):
        return True
    return False


def boot(k: Kraken, st: dict) -> None:
    w = st["wave"]
    log.info("APEX WAVE v5.1  state=%s  qty=%s  entry=%s  prev=%s  run_high=%s",
             w["state"], w["qty"], w["entry_px"], w["previous_top"], w["run_high"])
    if DRY_RUN:
        return
    try:
        orders = k.open_orders()
    except KrakenError as e:
        log.error("[BOOT] open orders failed: %s — cleanup deferred", e)
        return
    for o in orders:
        if "XRP" not in o["pair"].upper():
            continue
        if keep(o, w):
            log.info("[BOOT] KEEPING %s %s @ %s id=%s", o["side"], o["volume"], o["price"], o["order_id"])
            continue
        log.warning("[BOOT] orphan %s %s @ %s — cancelling %s",
                    o["side"], o["volume"], o["price"], o["order_id"])
        try:
            k.cancel(o["order_id"])
        except KrakenError as e:
            log.error("[BOOT] cancel failed: %s", e)


def place_buy22(k: Kraken, st: dict) -> None:
    w = st["wave"]
    if we.brake_engaged(w["down_cycles"]):
        log.info("[BRAKE] %s/%s down-cycles — no new −22%% buy", w["down_cycles"], we.MAX_DOWN_CYCLES)
        return
    px = we.buy22_price(w["previous_top"])
    usd = we.buy22_usd(st["cash"])
    qty = we.qty_for(usd, px)
    if qty <= 0:
        log.info("[BUY22] nothing to deploy above the $110 floor")
        return
    log.info("[BUY22] %s XRP @ %s  (prev %s × 0.78)", qty, px, w["previous_top"])
    if DRY_RUN:
        return
    oid = k.place_maker_buy(float(qty), float(px))
    w.update(state="BUY_WORKING", buy_oid=oid, buy_px=float(px), buy_qty=float(qty))
    save(st)


def place_add30(k: Kraken, st: dict) -> None:
    w = st["wave"]
    if w.get("added") or w.get("add_oid"):
        return
    px = we.add30_price(w["previous_top"])
    usd = we.add30_usd(st["cash"])
    qty = we.qty_for(usd, px)
    if qty <= 0:
        log.info("[ADD30] no cash above the floor")
        return
    log.info("[ADD30] %s XRP @ %s  (prev %s × 0.70)", qty, px, w["previous_top"])
    if DRY_RUN:
        return
    oid = k.place_maker_buy(float(qty), float(px))
    w.update(add_oid=oid, add_px=float(px), add_qty=float(qty))
    save(st)


def place_sell(k: Kraken, st: dict) -> None:
    w = st["wave"]
    px = we.sell_price(w["run_high"])
    qty = Decimal(str(w["qty"]))
    log.info("[SELL] flatten %s XRP @ %s  (run_high %s × 0.89)", qty, px, w["run_high"])
    if DRY_RUN:
        return
    oid = k.place_stop_sell(float(qty), float(px))
    w.update(armed=True, sell_px=float(px), sell_oid=oid)
    save(st)


def on_buy_fill(st: dict, fill_px: float, fill_qty: float) -> None:
    w = st["wave"]
    w.update(
        state="IN_XRP",
        entry_px=fill_px,
        qty=fill_qty,
        cost=round(fill_qty * fill_px, 8),
        run_high=fill_px,
        armed=False,
        sell_px=0.0,
        buy_oid="",
        down_cycles=int(w.get("down_cycles", 0)) + 1,
    )
    log.info("[FILL] −22%%  %s @ %s  down_cycles=%s  no sell until > %s",
             fill_qty, fill_px, w["down_cycles"], w["previous_top"])


def on_add_fill(st: dict, fill_px: float, fill_qty: float) -> None:
    w = st["wave"]
    # One sleeve. The add joins the bag; run_high is not reset.
    new_qty = Decimal(str(w["qty"])) + Decimal(str(fill_qty))
    new_cost = Decimal(str(w["cost"])) + Decimal(str(fill_qty)) * Decimal(str(fill_px))
    w.update(qty=float(new_qty), cost=float(new_cost), added=True, add_oid="")
    log.info("[FILL] −30%% add  %s @ %s  sleeve now %s", fill_qty, fill_px, new_qty)


def on_sell_fill(st: dict, fill_px: float) -> None:
    w = st["wave"]
    new_top = float(w["run_high"])
    w.update(
        state="IN_CASH",
        previous_top=new_top,
        run_high=None,
        armed=False,
        qty=0.0,
        cost=0.0,
        entry_px=0.0,
        sell_px=0.0,
        sell_oid="",
        added=False,
        add_oid="",
        down_cycles=0,
    )
    log.info("[FILL] flatten @ %s  new previous_top=%s  next buy at %s",
             fill_px, new_top, we.buy22_price(new_top))


def loop_once(k: Kraken, st: dict) -> None:
    w = st["wave"]
    t = k.ticker()
    sample = t["mid"]

    ohlc_high = None
    if w.get("entry_utc"):
        ohlc_high = k.ohlc_high_since(int(w["entry_utc"]))
        if ohlc_high is None:
            log.warning("[OHLC-MISS] keeping sample — run_high not raised")

    if w["state"] == "IN_XRP":
        prev = w.get("run_high") or w["entry_px"]
        w["run_high"] = float(we.update_run_high(prev, sample, ohlc_high))
        w["run_high_src"] = "ohlc" if ohlc_high else "sample"

        if not w.get("armed") and we.is_new_top(w["run_high"], w["previous_top"]):
            log.info("[ARM] run_high %s > previous_top %s", w["run_high"], w["previous_top"])
            place_sell(k, st)

        if not w.get("add_oid") and not w.get("added"):
            place_add30(k, st)

    elif w["state"] == "IN_CASH":
        if not w.get("buy_oid"):
            place_buy22(k, st)

    log.info("[HB] %s spot=%.5f run_high=%s prev=%s armed=%s qty=%s",
             w["state"], sample, w.get("run_high"), w["previous_top"],
             w.get("armed"), w.get("qty"))
    save(st)


def main() -> None:
    st = load()
    k = Kraken()
    boot(k, st)
    while True:
        try:
            loop_once(k, st)
        except Exception:
            log.exception("[LOOP] error — state untouched this pass")
        time.sleep(LOOP_SEC)


if __name__ == "__main__":
    main()
