"""
Wave Bot — XRP/USDC on Kraken
Apex Wave Agent v1.0 — Kraken-only
"""
import os, time, logging, json, math, csv
from datetime import datetime, timezone, timedelta
import pandas as pd
import kraken_client as kraken

PAIR               = "XRPUSDC"
CAPITAL            = float(os.getenv("TOTAL_CAPITAL_USDC", "483"))
POSITION_PCT       = float(os.getenv("POSITION_PCT", "0.40"))
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
MAX_DRAWDOWN_PCT   = 20.0
KRAKEN_FEE         = float(os.getenv("KRAKEN_FEE", "0.0026"))
LOOP_SLEEP         = int(os.getenv("LOOP_SLEEP", "30"))
CANDLES_NEEDED     = max(RSI_PERIOD + 10, EMA_SLOW_PERIOD + 10, 60)
RSI_EXIT_CONFIRM   = 3
RSI_BUY_UPTREND    = float(os.getenv("RSI_BUY_UPTREND", "38"))
DATA_DIR           = os.getenv("DATA_DIR", "/data")
STATE_FILE         = os.path.join(DATA_DIR, "wave_state.json")
TRADE_LOG          = os.path.join(DATA_DIR, "wave_trade_log.csv")
TRADE_LOG_HEADER   = ["timestamp_utc","side","price","qty","reason","entry_price","net_pnl_usd","net_pnl_pct","portfolio_after","regime","entry_time_utc"]

logging.basicConfig(level=logging.INFO, format="%(asctime)s  %(levelname)-7s %(message)s", datefmt="%Y-%m-%d %H:%M:%S")
log = logging.getLogger("wave")

def save_state(**kwargs):
    try:
        os.makedirs(DATA_DIR, exist_ok=True)
        with open(STATE_FILE, "w") as f:
            json.dump({**kwargs, "saved_at": time.time()}, f, indent=2)
    except Exception as e:
        log.warning(f"save_state error: {e}")

def load_state():
    try:
        if not os.path.exists(STATE_FILE): return None
        with open(STATE_FILE) as f: s = json.load(f)
        log.info(f"[STATE] Loaded in_position={s.get('in_position')} xrp={s.get('xrp_position',0):.4f}")
        return s
    except Exception as e:
        log.warning(f"load_state error: {e}"); return None

def clear_state():
    try:
        if os.path.exists(STATE_FILE): os.remove(STATE_FILE)
    except: pass

def log_trade(*, side, price, qty, reason, entry_price=0.0, net_pnl_usd=0.0, portfolio_after=0.0, regime="", entry_time_utc=""):
    try:
        os.makedirs(DATA_DIR, exist_ok=True)
        file_exists = os.path.exists(TRADE_LOG)
        with open(TRADE_LOG, "a", newline="") as f:
            w = csv.writer(f)
            if not file_exists: w.writerow(TRADE_LOG_HEADER)
            pct = (net_pnl_usd / (entry_price * qty) * 100) if entry_price and qty else 0.0
            w.writerow([datetime.now(timezone.utc).strftime("%Y-%m-%d %H:%M:%S"),
                        side, round(price,4), round(qty,4), reason, round(entry_price,4),
                        round(net_pnl_usd,4), round(pct,2), round(portfolio_after,2), regime, entry_time_utc])
        log.info(f"[DIARY] {side} {qty:.4f} XRP @ ${price:.4f} reason={reason} pnl=${net_pnl_usd:+.4f}")
    except Exception as e:
        log.warning(f"log_trade error: {e}")

def compute_rsi(prices, period):
    delta = prices.diff().dropna()
    gain = delta.clip(lower=0).rolling(period).mean()
    loss = (-delta.clip(upper=0)).rolling(period).mean()
    rs = gain / loss.replace(0, float("nan"))
    rsi = 100 - (100 / (1 + rs))
    return float(rsi.iloc[-1]) if not rsi.empty else 50.0

def compute_ema(prices, period):
    return prices.ewm(span=period, adjust=False).mean()

def detect_regime(df):
    close = df["close"]
    ema20 = compute_ema(close, EMA_PERIOD)
    ema50 = compute_ema(close, EMA_SLOW_PERIOD)
    price = close.iloc[-1]
    slope = ema20.iloc[-1] - ema20.iloc[-EMA_SLOPE_BARS]
    if slope < 0 and price < ema50.iloc[-1]: return "STRONG_DOWNTREND"
    if slope > 0 and price > ema20.iloc[-1]: return "UPTREND"
    return "RANGING"

def vol_ratio(df):
    vol = df["volume"]
    if len(vol) < 20: return 1.0
    avg = vol.iloc[-20:].mean()
    return float(vol.iloc[-1] / avg) if avg > 0 else 1.0

def main():
    log.info("=" * 60)
    log.info("  Wave Bot v1.0 — XRP/USDC on Kraken")
    log.info(f"  CAPITAL={CAPITAL} PCT={POSITION_PCT} TP={TP_PCT}% SL={SL_PCT}%")
    log.info("=" * 60)
    if not kraken.ping():
        log.error("Kraken API unreachable — check credentials"); return

    in_position=False; entry_price=0.0; xrp_position=0.0; stop_price=0.0
    trailing_active=False; high_since_entry=0.0; rsi_exit_count=0
    sl_cooldown_until=datetime.now(timezone.utc)
    entry_time_utc=""; entry_regime=""; start_val=None; day_start_val=None; current_day=None

    saved = load_state()
    if saved:
        in_position=saved.get("in_position",False); entry_price=saved.get("entry_price",0.0)
        xrp_position=saved.get("xrp_position",0.0); stop_price=saved.get("stop_price",0.0)
        trailing_active=saved.get("trailing_active",False); high_since_entry=saved.get("high_since_entry",0.0)
        entry_time_utc=saved.get("entry_time_utc",""); entry_regime=saved.get("entry_regime","")
        try: sl_cooldown_until=datetime.fromisoformat(saved.get("sl_cooldown_until","")).replace(tzinfo=timezone.utc)
        except: pass
        start_val=saved.get("start_val"); day_start_val=saved.get("day_start_val"); current_day=saved.get("current_day")

    loop_count = 0
    while True:
        try:
            loop_count += 1
            now = datetime.now(timezone.utc)
            today = now.date().isoformat()
            df = kraken.fetch_ohlc(PAIR, interval=5, n=CANDLES_NEEDED+10)
            if df is None or len(df) < CANDLES_NEEDED:
                log.warning(f"Not enough candles, sleeping..."); time.sleep(LOOP_SLEEP); continue

            price  = float(df["close"].iloc[-1])
            rsi    = compute_rsi(df["close"], RSI_PERIOD)
            regime = detect_regime(df)
            vr     = vol_ratio(df)
            ema20  = compute_ema(df["close"], EMA_PERIOD).iloc[-1]

            balances = kraken.get_balances()
            usdc_bal = float(balances.get("USDC", 0))
            port_val = usdc_bal + (xrp_position * price if in_position else 0)

            if start_val is None: start_val = port_val; log.info(f"[INIT] start_val=${start_val:.2f}")
            if current_day != today: day_start_val=port_val; current_day=today; log.info(f"[DAY] {today} start=${day_start_val:.2f}")

            drawdown = (start_val - port_val) / start_val * 100 if start_val else 0
            if drawdown >= MAX_DRAWDOWN_PCT:
                log.warning(f"[HALT] Drawdown {drawdown:.1f}% >= {MAX_DRAWDOWN_PCT}%")
                time.sleep(LOOP_SLEEP * 2); continue

            log.info(f"[{loop_count}] ${price:.4f} RSI={rsi:.1f} {regime} vol={vr:.2f} pos={'YES' if in_position else 'NO'} ${port_val:.2f}")

            if in_position and xrp_position > 0:
                high_since_entry = max(high_since_entry, price)
                gain_pct = (price - entry_price) / entry_price * 100
                if not trailing_active and gain_pct >= TRAIL_TRIGGER:
                    trailing_active = True; stop_price = max(stop_price, entry_price)
                    log.info(f"[TRAIL] Activated, stop=${stop_price:.4f}")
                if trailing_active:
                    new_stop = high_since_entry * (1 - TRAIL_DIST / 100)
                    if new_stop > stop_price: stop_price = new_stop; log.info(f"[TRAIL] stop=${stop_price:.4f}")

                if price <= stop_price:
                    log.info(f"[SL] Triggered @ ${price:.4f}")
                    sp = kraken.get_mid_price(PAIR)
                    oid = kraken.place_limit_sell(PAIR, xrp_position, sp * 0.999)
                    time.sleep(5); fill = kraken.get_fill_price(oid, sp)
                    pnl = (fill - entry_price) * xrp_position - fill * xrp_position * KRAKEN_FEE
                    log_trade(side="SELL", price=fill, qty=xrp_position, reason="STOP-LOSS",
                              entry_price=entry_price, net_pnl_usd=pnl, portfolio_after=port_val+pnl,
                              regime=regime, entry_time_utc=entry_time_utc)
                    in_position=False; xrp_position=0.0; entry_price=0.0
                    trailing_active=False; high_since_entry=0.0; rsi_exit_count=0
                    sl_cooldown_until = now + timedelta(seconds=SL_COOLDOWN_SEC)
                    clear_state(); time.sleep(LOOP_SLEEP); continue

                tp_price = entry_price * (1 + TP_PCT / 100)
                if price >= tp_price:
                    log.info(f"[TP] Take-profit @ ${price:.4f}")
                    sp = kraken.get_mid_price(PAIR)
                    oid = kraken.place_limit_sell(PAIR, xrp_position, sp * 0.999)
                    time.sleep(5); fill = kraken.get_fill_price(oid, sp)
                    pnl = (fill - entry_price) * xrp_position - fill * xrp_position * KRAKEN_FEE
                    log_trade(side="SELL", price=fill, qty=xrp_position, reason="TAKE-PROFIT",
                              entry_price=entry_price, net_pnl_usd=pnl, portfolio_after=port_val+pnl,
                              regime=regime, entry_time_utc=entry_time_utc)
                    in_position=False; xrp_position=0.0; entry_price=0.0
                    trailing_active=False; high_since_entry=0.0; rsi_exit_count=0
                    clear_state(); time.sleep(LOOP_SLEEP); continue

                rsi_thresh = 77 if price > ema20 * 1.03 else RSI_EXIT
                if rsi >= rsi_thresh:
                    rsi_exit_count += 1
                    if rsi_exit_count >= RSI_EXIT_CONFIRM:
                        sp = kraken.get_mid_price(PAIR)
                        oid = kraken.place_limit_sell(PAIR, xrp_position, sp * 0.999)
                        time.sleep(5); fill = kraken.get_fill_price(oid, sp)
                        pnl = (fill - entry_price) * xrp_position - fill * xrp_position * KRAKEN_FEE
                        log_trade(side="SELL", price=fill, qty=xrp_position, reason="RSI-EXIT",
                                  entry_price=entry_price, net_pnl_usd=pnl, portfolio_after=port_val+pnl,
                                  regime=regime, entry_time_utc=entry_time_utc)
                        in_position=False; xrp_position=0.0; entry_price=0.0
                        trailing_active=False; high_since_entry=0.0; rsi_exit_count=0
                        clear_state(); time.sleep(LOOP_SLEEP); continue
                else:
                    rsi_exit_count = 0

            elif not in_position:
                if regime == "STRONG_DOWNTREND":
                    log.info("[SKIP] STRONG_DOWNTREND")
                elif now < sl_cooldown_until:
                    log.info(f"[COOL] {int((sl_cooldown_until-now).total_seconds()//60)}min remaining")
                else:
                    rsi_th = RSI_BUY_UPTREND if regime == "UPTREND" else RSI_BUY
                    vol_ok = (vr >= VOL_RATIO_MIN) if regime == "RANGING" else True
                    if rsi < rsi_th and vol_ok:
                        alloc = min(usdc_bal, CAPITAL) * POSITION_PCT
                        if alloc < 5:
                            log.warning(f"[SKIP] Low balance: {usdc_bal:.2f}")
                        else:
                            bp = kraken.get_mid_price(PAIR)
                            xrp_qty = math.floor((alloc / bp) * 10000) / 10000
                            log.info(f"[BUY] RSI={rsi:.1f} {regime} vr={vr:.2f} qty={xrp_qty:.4f} @ ${bp:.4f}")
                            oid = kraken.place_limit_buy(PAIR, xrp_qty, bp * 1.001)
                            time.sleep(8); fill = kraken.get_fill_price(oid, bp)
                            if fill > 0:
                                in_position=True; entry_price=fill; xrp_position=xrp_qty
                                stop_price=fill*(1-SL_PCT/100); trailing_active=False
                                high_since_entry=fill; entry_time_utc=now.isoformat(); entry_regime=regime
                                log_trade(side="BUY", price=fill, qty=xrp_qty, reason="ENTRY",
                                          entry_price=fill, portfolio_after=port_val, regime=regime, entry_time_utc=entry_time_utc)
                                log.info(f"[ENTRY] ${fill:.4f} stop=${stop_price:.4f} tp=${fill*(1+TP_PCT/100):.4f}")
                            else:
                                kraken.cancel_order(oid)

            save_state(in_position=in_position, entry_price=entry_price, xrp_position=xrp_position,
                       stop_price=stop_price, trailing_active=trailing_active, high_since_entry=high_since_entry,
                       sl_cooldown_until=sl_cooldown_until.isoformat(), entry_time_utc=entry_time_utc,
                       entry_regime=entry_regime, start_val=start_val, day_start_val=day_start_val, current_day=current_day)

        except KeyboardInterrupt:
            log.info("Wave Bot stopped"); break
        except Exception as e:
            log.error(f"Loop error: {e}", exc_info=True)
        time.sleep(LOOP_SLEEP)

if __name__ == "__main__":
    main()
