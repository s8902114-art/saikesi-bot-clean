# -*- coding: utf-8 -*-
"""NQ「比 Yahoo 更即時」候選的延遲量測（2026-11-27，用戶：「nq沒有更即時的嗎」）——只讀公開端點、不帶金鑰、不下單。
measure() 逐一打每個候選，讀它自己標的「最新成交時間／最新一根／報價時間」，回傳跟本機現在時間的差（秒）。
清單與請求內容全部寫死在這裡（dashboard 的 /rtlag 不吃任何 request 參數）＝不是開放代理。"""
import json, time, urllib.request, urllib.error

UA = {"User-Agent": "Mozilla/5.0", "accept": "application/json"}


def _get(url, body=None, timeout=12):
    h = dict(UA, **({"Content-Type": "application/json"} if body is not None else {}))
    last = None
    for u in ([url, url.replace("query1.", "query2.")] if "query1." in url else [url]):     # Yahoo 偶爾 SSL 握手逾時 → 換另一台主機再試
        try:
            req = urllib.request.Request(u, data=json.dumps(body).encode() if body is not None else None, headers=h)
            with urllib.request.urlopen(req, timeout=timeout) as r:
                return r.status, json.loads(r.read().decode("utf-8", "replace"))
        except urllib.error.HTTPError:
            raise
        except Exception as e:
            last = e
    raise last


def _yahoo(sym):
    def f():
        st, j = _get(f"https://query1.finance.yahoo.com/v8/finance/chart/{sym}?interval=1m&range=1d&includePrePost=true")
        m = j["chart"]["result"][0]["meta"]; ts = j["chart"]["result"][0].get("timestamp") or []
        return {"code": st, "ts": float(m.get("regularMarketTime") or 0), "what": "報價時間 regularMarketTime", "px": m.get("regularMarketPrice"),
                "bar": float(ts[-1]) if ts else None, "declared": m.get("exchangeDataDelayedBy")}
    return f


def _okx(inst):
    def f():
        st, j = _get(f"https://www.okx.com/api/v5/market/trades?instId={inst}&limit=5")
        d = j.get("data") or []
        st2, k = _get(f"https://www.okx.com/api/v5/market/ticker?instId={inst}"); t = (k.get("data") or [{}])[0]
        return {"code": st, "ts": int(d[0]["ts"]) / 1000 if d else None, "what": "最新成交", "px": float(d[0]["px"]) if d else None,
                "tick": int(t.get("ts", 0)) / 1000 or None, "vol24": t.get("volCcy24h")}
    return f


def _binance(sym):
    def f():
        st, d = _get(f"https://www.binance.com/fapi/v1/trades?symbol={sym}&limit=5")
        return {"code": st, "ts": int(d[-1]["time"]) / 1000 if d else None, "what": "最新成交", "px": float(d[-1]["price"]) if d else None}
    return f


def _bitget(sym):
    def f():
        st, j = _get(f"https://api.bitget.com/api/v2/mix/market/fills?symbol={sym}&productType=usdt-futures&limit=5")
        d = j.get("data") or []
        return {"code": st, "ts": max(int(x["ts"]) for x in d) / 1000 if d else None, "what": "最新成交", "px": float(d[0]["price"]) if d else None}
    return f


def _gate(c):
    def f():
        st, d = _get(f"https://api.gateio.ws/api/v4/futures/usdt/trades?contract={c}&limit=5")
        tt = [float(x.get("create_time_ms") or x.get("create_time")) for x in d] if d else []        # Gate 的 create_time_ms 單位是「秒」（帶小數）
        return {"code": st, "ts": max(tt) if tt else None, "what": "最新成交", "px": float(d[0]["price"]) if d else None}
    return f


def _mexc(sym):
    def f():
        st, j = _get(f"https://contract.mexc.com/api/v1/contract/deals/{sym}?limit=5")
        d = j.get("data") or []
        return {"code": st, "ts": max(int(x["t"]) for x in d) / 1000 if d else None, "what": "最新成交", "px": float(d[0]["p"]) if d else None}
    return f


def _bingx(sym):
    def f():
        st, j = _get(f"https://open-api.bingx.com/openApi/swap/v2/quote/trades?symbol={sym}&limit=5")
        d = j.get("data") or []
        return {"code": st, "ts": max(int(x["time"]) for x in d) / 1000 if d else None, "what": "最新成交", "px": float(d[0]["price"]) if d else None}
    return f


def _hl(coin):
    def f():
        now = int(time.time() * 1000)
        st, tr = _get("https://api.hyperliquid.xyz/info", {"type": "recentTrades", "coin": coin})          # 只取時間、價格（回應裡的錢包地址不留）
        st2, d = _get("https://api.hyperliquid.xyz/info", {"type": "candleSnapshot", "req": {"coin": coin, "interval": "1m", "startTime": now - 600_000, "endTime": now}})
        t = max(tr, key=lambda x: x["time"]) if tr else None
        return {"code": st, "ts": int(t["time"]) / 1000 if t else None, "what": "最新成交", "px": float(t["px"]) if t else None,
                "n10": sum(int(x.get("n", 0)) for x in d), "v10": round(sum(float(x.get("v", 0)) for x in d), 4)}
    return f


def _tv():
    st, j = _get("https://scanner.tradingview.com/futures/scan", {"symbols": {"tickers": ["CME_MINI:NQ1!"]}, "columns": ["close", "update_mode"]})
    d = (j.get("data") or [{}])[0].get("d") or [None, None]
    return {"code": st, "ts": None, "what": "沒有時間欄位，看它自己標的模式", "px": d[0], "declared": d[1]}


def _pyth():
    st, j = _get("https://hermes.pyth.network/v2/price_feeds?query=QQQ&asset_type=equity")
    fid = next((x["id"] for x in j if (x.get("attributes") or {}).get("symbol") == "Equity.US.QQQ/USD"), None)
    st2, k = _get(f"https://hermes.pyth.network/v2/updates/price/latest?ids[]={fid}&parsed=true")
    p = (k.get("parsed") or [{}])[0].get("price") or {}
    return {"code": st2, "ts": float(p.get("publish_time") or 0) or None, "what": "報價發布時間", "px": int(p.get("price", 0)) * 10 ** int(p.get("expo", 0)) if p else None}


def _cme():
    st, _ = _get("https://www.cmegroup.com/CmeWS/mvc/Quotes/Future/146/G")
    return {"code": st, "ts": None, "what": "—"}


CANDS = [
    ("Yahoo NQ=F（現在用的）", "NQ 本身", _yahoo("NQ%3DF")),
    ("TradingView scanner NQ1!", "NQ 本身", _tv),
    ("CME 官網報價", "NQ 本身", _cme),
    ("Hyperliquid xyz:XYZ100", "替身：追那斯達克 100 的永續", _hl("xyz:XYZ100")),
    ("Binance QQQUSDT", "替身：QQQ ETF 永續（×41.10）", _binance("QQQUSDT")),
    ("OKX QQQ-USDT-SWAP", "替身：QQQ ETF 永續", _okx("QQQ-USDT-SWAP")),
    ("MEXC NAS100_USDT", "替身：那斯達克 100 永續", _mexc("NAS100_USDT")),
    ("OKX US100-USDT-SWAP", "替身：那斯達克 100 永續（價位≈NQ）", _okx("US100-USDT-SWAP")),
    ("Bitget NDX100USDT", "替身：那斯達克 100 永續", _bitget("NDX100USDT")),
    ("Gate NAS100_USDT", "替身：那斯達克 100 永續", _gate("NAS100_USDT")),
    ("BingX NASDAQ100", "替身：那斯達克 100 永續", _bingx("NCSINASDAQ1002USD-USDT")),
    ("Yahoo ^NDX", "替身：現貨指數（沒有量）", _yahoo("%5ENDX")),
    ("Yahoo QQQ", "替身：QQQ ETF", _yahoo("QQQ")),
    ("Pyth QQQ", "替身：QQQ 報價（沒有量）", _pyth),
]


def measure():
    out = []
    for name, kind, fn in CANDS:
        row = {"name": name, "kind": kind}
        t0 = time.time()
        try:
            row.update(fn())
        except urllib.error.HTTPError as e:
            row["code"] = e.code
        except Exception as e:
            row["err"] = f"{type(e).__name__}: {e}"[:120]
        row["now"] = time.time(); row["rt"] = round(row["now"] - t0, 2)
        if row.get("ts"):
            row["lag"] = round(row["now"] - row["ts"], 1)
        out.append(row)
    return out


if __name__ == "__main__":
    import sys
    sys.stdout.reconfigure(encoding="utf-8")
    for r in measure():
        print(r)
