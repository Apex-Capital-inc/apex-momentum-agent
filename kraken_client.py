"""
Kraken REST API Client
=======================
Wrapper for the apex-momentum-agent multi-asset controller.
Handles: balances, market data (ticker + OHLC), limit orders, margin shorts.

Pairs used:
  XRP/USDC  — long/short on Kraken (account holds USDC, not fiat USD)
  FLR/USDC  — long on Kraken (FLR not available on Coinbase)

Authentication: KRAKEN_API_KEY + KRAKEN_API_SECRET env vars.
"""

import os, time, logging, hashlib, hmac, base64, urllib.parse
import requests
import pandas as pd

log = logging.getLogger("kraken")

# ── Config ────────────────────────────────────────────────────────────────────
KRAKEN_API_URL    = "https://api.kraken.com"
KRAKEN_API_KEY    = os.environ.get("KRAKEN_API_KEY", "")
KRAKEN_API_SECRET = os.environ.get("KRAKEN_API_SECRET", "")
KRAKEN_FEE        = float(os.getenv("KRAKEN_FEE", "0.0026"))  # 0.26% taker fee

# Pair names on Kraken
XRP_PAIR      = "XRPUSDC"   # ✅ XRP/USDC — confirmed live, orders funded from USDC balance
FLR_PAIR      = "FLRUSD"    # ✅ FLR/USD  — confirmed live, controller auto-converts USDC→USD as needed
FLR_DATA_PAIR = "FLRUSD"    # same pair for OHLC / ticker data
FLR_XRP_PAIR  = "FLRXRP"    # ⚠️  not yet available on Kraken REST API

# ── Auth ──────────────────────────────────────────────────────────────────────

def _kraken_signature(urlpath: str, data: dict, secret: str) -> str:
    """Generate Kraken API signature (HMAC-SHA512)."""
    postdata  = urllib.parse.urlencode(data)
    encoded   = (str(data["nonce"]) + postdata).encode()
    message   = urlpath.encode() + hashlib.sha256(encoded).digest()
    mac       = hmac.new(base64.b64decode(secret), message, hashlib.sha512)
    return base64.b64encode(mac.digest()).decode()


def _nonce() -> str:
    return str(int(time.time() * 1000))


# ── HTTP helpers ──────────────────────────────────────────────────────────────

def _public_get(path: str, params: dict = None) -> dict:
    """Public REST call — no auth required."""
    try:
        resp = requests.get(
            f"{KRAKEN_API_URL}{path}",
            params=params or {},
            timeout=10,
        )
        resp.raise_for_status()
        data = resp.json()
        if data.get("error"):
            log.warning(f"Kraken public error {path}: {data['error']}")
        return data.get("result", {})
    except Exception as e:
        log.warning(f"Kraken public GET {path} error: {e}")
        return {}


def _private_post(path: str, data: dict) -> dict:
    """Private REST call — requires API key + signature."""
    if not KRAKEN_API_KEY or not KRAKEN_API_SECRET:
        log.error("Kraken API credentials not set")
        return {}
    try:
        data["nonce"] = _nonce()
        sig = _kraken_signature(path, data, KRAKEN_API_SECRET)
        headers = {
            "API-Key":  KRAKEN_API_KEY,
            "API-Sign": sig,
        }
        resp = requests.post(
            f"{KRAKEN_API_URL}{path}",
            data=data,
            headers=headers,
            timeout=10,
        )
        resp.raise_for_status()
        result = resp.json()
        if result.get("error"):
            log.warning(f"Kraken private error {path}: {result['error']}")
            return {}
        return result.get("result", {})
    except Exception as e:
        log.warning(f"Kraken private POST {path} error: {e}")
        return {}


# ── Market data ───────────────────────────────────────────────────────────────

def get_ticker(pair: str) -> dict:
    """
    Returns ticker info for a pair.
    Keys: bid, ask, mid, last, volume_24h
    """
    result = _public_get("/0/public/Ticker", {"pair": pair})
    if not result:
        return {}
    try:
        key  = list(result.keys())[0]
        tick = result[key]
        bid  = float(tick["b"][0])
        ask  = float(tick["a"][0])
        last = float(tick["c"][0])
        vol  = float(tick["v"][1])   # 24h volume
        return {
            "bid": bid,
            "ask": ask,
            "mid": round((bid + ask) / 2, 6),
            "last": last,
            "volume_24h": vol,
        }
    except Exception as e:
        log.warning(f"get_ticker {pair} parse error: {e}")
        return {}


def get_mid_price(pair: str) -> float:
    """Returns mid price for pair, 0.0 on failure."""
    t = get_ticker(pair)
    return t.get("mid", 0.0)


def fetch_ohlc(pair: str, interval: int = 5, n: int = 70) -> pd.DataFrame:
    """
    Fetch OHLC candles from Kraken.
    interval : candle size in minutes (1, 5, 15, 30, 60, 240)
    n        : number of candles needed (Kraken returns up to 720)
    Returns DataFrame sorted oldest→newest with columns:
      ts, open, high, low, close, volume
    """
    since = int(time.time()) - n * interval * 60
    result = _public_get("/0/public/OHLC", {"pair": pair, "interval": interval, "since": since})
    if not result:
        return pd.DataFrame()
    try:
        key  = list(k for k in result.keys() if k != "last")[0]
        rows = result[key]
        df = pd.DataFrame(rows, columns=[
            "ts", "open", "high", "low", "close", "vwap", "volume", "count"
        ])
        df = df.astype({
            "ts": int, "open": float, "high": float,
            "low": float, "close": float, "volume": float,
        })
        df.sort_values("ts", inplace=True)
        df.reset_index(drop=True, inplace=True)
        return df[["ts", "open", "high", "low", "close", "volume"]]
    except Exception as e:
        log.warning(f"fetch_ohlc {pair} parse error: {e}")
        return pd.DataFrame()


# ── Account ───────────────────────────────────────────────────────────────────

_balance_cache: dict = {}   # last known good balances — returned on API error / rate limit

def get_balances() -> dict:
    """
    Returns dict of available balances.
    Keys:
      USD   — fiat USD (Kraken internal: ZUSD) — used for FLR/USD trades
      USDC  — stablecoin USDC                  — used for XRP/USDC trades
      XRP, FLR
    Both USD and USDC count as capital; controller auto-converts between them as needed.

    On API error or rate-limit, returns the last known good balance instead of zeros.
    This prevents false max-drawdown triggers when Kraken is temporarily unreachable.
    """
    global _balance_cache
    result = _private_post("/0/private/Balance", {})
    if not result:
        if _balance_cache:
            log.warning("[KRAKEN] Balance API failed — returning cached balances to avoid false drawdown")
            return _balance_cache.copy()
        return {"USD": 0.0, "USDC": 0.0, "XRP": 0.0, "FLR": 0.0}
    balances = {}
    for k, v in result.items():
        clean = k.lstrip("XZ")   # ZUSD → USD, XXRP → XRP, USDC stays USDC
        balances[clean] = float(v)
    fresh = {
        "USD":  balances.get("USD",  0.0),   # fiat (ZUSD)
        "USDC": balances.get("USDC", 0.0),   # stablecoin
        "XRP":  balances.get("XRP",  0.0),
        "FLR":  balances.get("FLR",  0.0),
    }
    _balance_cache = fresh.copy()   # update cache on every successful call
    return fresh


def convert_funds(from_asset: str, to_asset: str, volume: float) -> bool:
    """
    Convert between currencies using Kraken's ConvertFunds endpoint.
    Examples:
      convert_funds("USDC", "ZUSD", 50.0)  — USDC → fiat USD before FLR buy
      convert_funds("ZUSD", "USDC", 50.0)  — fiat USD → USDC after FLR sell
    Returns True on success.
    """
    data = {
        "from":   from_asset,
        "to":     to_asset,
        "volume": str(round(volume, 6)),
    }
    result = _private_post("/0/private/ConvertFunds", data)
    if result:
        log.info(f"[KRAKEN] Converted {volume:.4f} {from_asset} → {to_asset}")
        return True
    log.warning(f"[KRAKEN] Conversion {from_asset}→{to_asset} {volume:.4f} failed")
    return False


def get_open_orders() -> list:
    """Returns list of open orders with standardized fields."""
    result = _private_post("/0/private/OpenOrders", {})
    if not result:
        return []
    orders = []
    for oid, o in result.get("open", {}).items():
        desc = o.get("descr", {})
        orders.append({
            "order_id":  oid,
            "pair":      desc.get("pair", ""),
            "side":      desc.get("type", ""),        # buy / sell
            "type":      desc.get("ordertype", ""),   # limit / market
            "price":     float(desc.get("price", 0)),
            "volume":    float(o.get("vol", 0)),
            "filled":    float(o.get("vol_exec", 0)),
            "status":    o.get("status", ""),
            "leverage":  desc.get("leverage", "none"),
        })
    return orders


# ── Order placement ───────────────────────────────────────────────────────────

def place_limit_buy(pair: str, volume: float, price: float) -> str:
    """
    Place a limit buy order. Returns order_id or '' on failure.
    volume : base currency (XRP or FLR)
    price  : limit price in USD
    """
    data = {
        "pair":      pair,
        "type":      "buy",
        "ordertype": "limit",
        "price":     str(round(price, 5)),
        "volume":    str(round(volume, 4)),
    }
    result = _private_post("/0/private/AddOrder", data)
    txids = result.get("txid", [])
    if txids:
        oid = txids[0]
        log.info(f"[KRAKEN] BUY {volume:.4f} {pair} @ ${price:.5f} | id={oid}")
        return oid
    log.error(f"[KRAKEN] place_limit_buy failed: {result}")
    return ""


def place_limit_sell(pair: str, volume: float, price: float) -> str:
    """
    Place a limit sell order (spot). Returns order_id or '' on failure.
    """
    data = {
        "pair":      pair,
        "type":      "sell",
        "ordertype": "limit",
        "price":     str(round(price, 5)),
        "volume":    str(round(volume, 4)),
    }
    result = _private_post("/0/private/AddOrder", data)
    txids = result.get("txid", [])
    if txids:
        oid = txids[0]
        log.info(f"[KRAKEN] SELL {volume:.4f} {pair} @ ${price:.5f} | id={oid}")
        return oid
    log.error(f"[KRAKEN] place_limit_sell failed: {result}")
    return ""


def place_short(pair: str, volume: float, price: float, leverage: int = 2) -> str:
    """
    Open a short position via Kraken margin trading.
    Sells borrowed asset, profits when price falls.
    leverage : 2x default (Kraken supports 2x-5x depending on pair)
    Returns order_id or '' on failure.
    """
    data = {
        "pair":      pair,
        "type":      "sell",
        "ordertype": "limit",
        "price":     str(round(price, 5)),
        "volume":    str(round(volume, 4)),
        "leverage":  str(leverage),
    }
    result = _private_post("/0/private/AddOrder", data)
    txids = result.get("txid", [])
    if txids:
        oid = txids[0]
        log.info(f"[KRAKEN] SHORT {volume:.4f} {pair} @ ${price:.5f} x{leverage} | id={oid}")
        return oid
    log.error(f"[KRAKEN] place_short failed: {result}")
    return ""


def close_short(pair: str, volume: float, price: float) -> str:
    """
    Close a short position (buy to cover).
    Returns order_id or '' on failure.
    """
    data = {
        "pair":      pair,
        "type":      "buy",
        "ordertype": "limit",
        "price":     str(round(price, 5)),
        "volume":    str(round(volume, 4)),
        "leverage":  "2",
    }
    result = _private_post("/0/private/AddOrder", data)
    txids = result.get("txid", [])
    if txids:
        oid = txids[0]
        log.info(f"[KRAKEN] COVER {volume:.4f} {pair} @ ${price:.5f} | id={oid}")
        return oid
    log.error(f"[KRAKEN] close_short failed: {result}")
    return ""


def cancel_order(order_id: str) -> bool:
    """Cancel an open order. Returns True on success."""
    result = _private_post("/0/private/CancelOrder", {"txid": order_id})
    if result.get("count", 0) > 0:
        log.info(f"[KRAKEN] Cancelled order {order_id}")
        return True
    log.warning(f"[KRAKEN] Cancel failed for {order_id}: {result}")
    return False


def is_order_filled(order_id: str) -> bool:
    """Check if a specific order is fully filled."""
    result = _private_post("/0/private/QueryOrders", {"txid": order_id, "trades": "false"})
    if not result:
        return False
    o = result.get(order_id, {})
    return o.get("status") in ("closed", "canceled") and float(o.get("vol_exec", 0)) > 0


def get_fill_price(order_id: str, fallback: float) -> float:
    """Get average fill price of an order."""
    result = _private_post("/0/private/QueryOrders", {"txid": order_id, "trades": "false"})
    if not result:
        return fallback
    o = result.get(order_id, {})
    avg = o.get("price")
    return float(avg) if avg and float(avg) > 0 else fallback


# ── Connectivity check ────────────────────────────────────────────────────────

def ping() -> bool:
    """Verify Kraken API is reachable and credentials work."""
    try:
        result = _private_post("/0/private/Balance", {})
        if result is not None:
            log.info(f"[KRAKEN] Connection OK — balances: {result}")
            return True
    except Exception as e:
        log.error(f"[KRAKEN] Ping failed: {e}")
    return False


def verify_pairs() -> dict:
    """
    Check which of our required pairs are available and liquid.
    Separates DATA availability (for scoring) from TRADE availability (for orders).
    """
    pairs_info = {}

    # XRP: single pair for both data and trading
    t = get_ticker(XRP_PAIR)
    if t:
        pairs_info[XRP_PAIR] = {
            "available": True,
            "mid":        t["mid"],
            "spread_pct": round((t["ask"] - t["bid"]) / t["mid"] * 100, 4),
            "volume_24h": t["volume_24h"],
        }
        log.info(
            f"[KRAKEN] {XRP_PAIR}: mid=${t['mid']:.5f}  "
            f"spread={pairs_info[XRP_PAIR]['spread_pct']:.3f}%  "
            f"vol24h={t['volume_24h']:.0f}"
        )
    else:
        pairs_info[XRP_PAIR] = {"available": False}
        log.warning(f"[KRAKEN] {XRP_PAIR}: NOT available or no data")

    # FLR: FLRUSD is both the data pair and the trade pair.
    # Controller auto-converts USDC↔USD as needed before/after FLR trades.
    t_flr = get_ticker(FLR_PAIR)   # FLR_PAIR = FLR_DATA_PAIR = "FLRUSD"
    if t_flr:
        pairs_info[FLR_PAIR] = {
            "available": True,
            "mid":        t_flr["mid"],
            "spread_pct": round((t_flr["ask"] - t_flr["bid"]) / t_flr["mid"] * 100, 4),
            "volume_24h": t_flr["volume_24h"],
        }
        log.info(
            f"[KRAKEN] {FLR_PAIR} (data+trade): mid=${t_flr['mid']:.6f}  "
            f"spread={pairs_info[FLR_PAIR]['spread_pct']:.3f}%  "
            f"vol24h={t_flr['volume_24h']:.0f}  "
            f"[USDC↔USD auto-conversion enabled]"
        )
    else:
        pairs_info[FLR_PAIR] = {"available": False}
        log.warning(f"[KRAKEN] {FLR_PAIR}: NOT available — FLR disabled")

    return pairs_info
