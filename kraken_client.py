"""
kraken_client.py — the calls WAVE actually needs
================================================
Spot only. No margin. No futures.
Keys from the environment: KRAKEN_API_KEY, KRAKEN_API_SECRET.
"""
from __future__ import annotations

import base64
import hashlib
import hmac
import os
import time
import urllib.parse

import requests

API = "https://api.kraken.com"
PAIR = "XRPUSDC"
ASSET = "XRP"


class KrakenError(RuntimeError):
    pass


class Kraken:
    def __init__(self, key: str | None = None, secret: str | None = None):
        self.key = key or os.environ["KRAKEN_API_KEY"]
        self.secret = secret or os.environ["KRAKEN_API_SECRET"]
        self.s = requests.Session()
        self.s.headers["User-Agent"] = "apex-wave/5.1"

    # ── public ──────────────────────────────────────────────────────────────
    def ticker(self) -> dict:
        r = self.s.get(f"{API}/0/public/Ticker", params={"pair": PAIR}, timeout=10)
        r.raise_for_status()
        body = r.json()
        if body.get("error"):
            raise KrakenError(body["error"])
        row = next(iter(body["result"].values()))
        return {
            "bid": float(row["b"][0]),
            "ask": float(row["a"][0]),
            "last": float(row["c"][0]),
            "mid": (float(row["b"][0]) + float(row["a"][0])) / 2,
        }

    def ohlc_high_since(self, since_unix: int, interval: int = 60) -> float | None:
        """
        D1. Highest high Kraken printed on `interval`-minute bars since `since_unix`.
        Returns None on any failure — the caller must not raise run_high.
        """
        try:
            r = self.s.get(
                f"{API}/0/public/OHLC",
                params={"pair": PAIR, "interval": interval, "since": since_unix},
                timeout=15,
            )
            r.raise_for_status()
            body = r.json()
            if body.get("error"):
                return None
            rows = next(iter(body["result"].values()))
            highs = [float(row[2]) for row in rows if int(row[0]) >= since_unix]
            return max(highs) if highs else None
        except Exception:
            return None

    # ── private ─────────────────────────────────────────────────────────────
    def _sign(self, path: str, data: dict) -> dict:
        postdata = urllib.parse.urlencode(data)
        encoded = (str(data["nonce"]) + postdata).encode()
        message = path.encode() + hashlib.sha256(encoded).digest()
        mac = hmac.new(base64.b64decode(self.secret), message, hashlib.sha512)
        return {
            "API-Key": self.key,
            "API-Sign": base64.b64encode(mac.digest()).decode(),
        }

    def _private(self, path: str, data: dict | None = None) -> dict:
        data = dict(data or {})
        data["nonce"] = str(int(time.time() * 1000))
        headers = self._sign(path, data)
        r = self.s.post(API + path, data=data, headers=headers, timeout=15)
        r.raise_for_status()
        body = r.json()
        if body.get("error"):
            raise KrakenError(body["error"])
        return body["result"]

    def balances(self) -> dict:
        """Total holdings, including what resting orders have reserved."""
        raw = self._private("/0/private/Balance")
        return {k: float(v) for k, v in raw.items()}

    def free_balances(self) -> dict:
        """
        Spendable. BalanceEx returns balance minus hold_trade.
        Falls back to Balance if BalanceEx is unavailable.
        """
        try:
            raw = self._private("/0/private/BalanceEx")
            out = {}
            for k, v in raw.items():
                if isinstance(v, dict):
                    bal = float(v.get("balance", 0))
                    hold = float(v.get("hold_trade", 0))
                    out[k] = bal - hold
                else:
                    out[k] = float(v)
            return out
        except Exception:
            return self.balances()

    def open_orders(self) -> list[dict]:
        raw = self._private("/0/private/OpenOrders")
        out = []
        for oid, o in raw.get("open", {}).items():
            descr = o.get("descr", {})
            out.append({
                "order_id": oid,
                "pair": descr.get("pair", ""),
                "side": descr.get("type", ""),          # buy / sell
                "ordertype": descr.get("ordertype", ""),
                "price": float(descr.get("price") or o.get("price") or 0),
                "volume": float(o.get("vol", 0)),
                "status": o.get("status", ""),
            })
        return out

    def add_order(self, side: str, ordertype: str, volume: float, price: float,
                  post_only: bool = False) -> str:
        data = {
            "pair": PAIR,
            "type": side,
            "ordertype": ordertype,
            "volume": f"{volume:.4f}",
            "price": f"{price:.5f}",
        }
        if post_only:
            data["oflags"] = "post"
        res = self._private("/0/private/AddOrder", data)
        txid = res.get("txid") or []
        if not txid:
            raise KrakenError(f"AddOrder returned no txid: {res}")
        return txid[0]

    def place_maker_buy(self, volume: float, price: float) -> str:
        return self.add_order("buy", "limit", volume, price, post_only=True)

    def place_stop_sell(self, volume: float, trigger: float) -> str:
        """Exchange-side stop. Survives bot downtime."""
        data = {
            "pair": PAIR,
            "type": "sell",
            "ordertype": "stop-loss",
            "volume": f"{volume:.4f}",
            "price": f"{trigger:.5f}",
        }
        res = self._private("/0/private/AddOrder", data)
        txid = res.get("txid") or []
        if not txid:
            raise KrakenError(f"stop-loss returned no txid: {res}")
        return txid[0]

    def cancel(self, oid: str) -> None:
        self._private("/0/private/CancelOrder", {"txid": oid})
