#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
Trading Bot v3 — Production-Grade QQE MOD Signal Bot

Optimised parameters (final_params_all.json):
  1H  Long : TP1=1.3x  TP2i=2.0x  TP2s=4.0x  SL=0.15×ATR  LB=10  fixed
  15m Long : TP1=1.5x  TP2i=2.2x  TP2s=2.5x  SL=0.01×ATR  LB=28  fixed

Active filters:
  • QQE MOD dual confirmation  (Primary F=3 + Secondary F=1.61, Thr=3)
  • Channel entanglement guard  |EMA144-EMA576| >= ATR×2
  • Funding Rate gate           Long<0.01% / Short>-0.01%
  • Bear-market gate for shorts EMA144 < EMA576 for 20+ consecutive 1H bars
  • CVD 3-layer (Coinalyze)    enabled when COINALYZE_API_KEY is set
  • Swing / Intraday auto-detect (4H trend + slope + ADX>25)
  • Trailing stop               TP1 hit → SL moves to entry (breakeven)
  • Consecutive-loss pause      3 losses → 24-hour trading pause per symbol/side
  • Anti-duplicate signal guard same bar cannot re-fire for same symbol/tf/side

Usage:
  python trading_bot_v3.py                   # paper mode, 1H+15m, both sides
  python trading_bot_v3.py --tf 1H           # 1H timeframe only
  python trading_bot_v3.py --tf 15m          # 15m timeframe only
  python trading_bot_v3.py --side long       # long only
  python trading_bot_v3.py --live            # real OKX orders (requires API creds)
  python trading_bot_v3.py --once            # single scan then exit
  python trading_bot_v3.py --dir /path       # custom param JSON directory
"""
import sys, io
if hasattr(sys.stdout, "reconfigure"):
    sys.stdout.reconfigure(encoding="utf-8", errors="replace")
else:
    sys.stdout = io.TextIOWrapper(sys.stdout.buffer, encoding="utf-8", errors="replace")

import subprocess
for _pkg in ["requests", "pandas", "numpy"]:
    try: __import__(_pkg)
    except ImportError: subprocess.check_call([sys.executable, "-m", "pip", "install", _pkg, "-q"])

import argparse, base64, hashlib, hmac, json, math, os, time
from datetime import datetime, timezone, timedelta
from typing import Dict, List, Optional, Tuple

import numpy as np
import pandas as pd
import requests

# ══════════════════════════════════════════════════════════════════════════════
#  USER CONFIGURATION — edit this section
# ══════════════════════════════════════════════════════════════════════════════

# ── Coinalyze (CVD + OI) ─────────────────────────────────────────────────────
COINALYZE_API_KEY = "82087740-b30d-479f-8846-5ffb51540b19"

# ── Telegram alerts ───────────────────────────────────────────────────────────
TG_BOT_TOKEN = "7642408367:AAG_6HS6BLeHtST2cKjNjaU6Ajpmbe_cj8w"
TG_CHAT_ID   = "8799334828"

# ── OKX live trading (only needed with --live) ────────────────────────────────
OKX_API_KEY    = "d96ecfe5-5336-4f5f-b25f-bd3ac9603786"
OKX_SECRET     = "359300E99DD8870F8990CC698BC4F491"
OKX_PASSPHRASE = "Small5017714@"
OKX_DEMO       = True            # True = OKX paper-trading endpoint

# Risk management (live mode)
RISK_PCT       = 0.01            # 1% of account per trade
LEVERAGE       = 10              # cross-margin leverage

# OKX perpetual contract face-value (ctVal, in base asset)
CONTRACT_SIZE: Dict[str, float] = {
    "BTC-USDT": 0.01,
    "ETH-USDT": 0.1,
    "SOL-USDT": 1.0,
}

# ── Timeframes to monitor ─────────────────────────────────────────────────────
# Each entry: (bar_str, poll_seconds, side_list)
DEFAULT_TF_PLAN = [
    ("1H",  600,  ["long", "short"]),
    ("15m", 180,  ["long", "short"]),
]

# ══════════════════════════════════════════════════════════════════════════════
#  FIXED STRATEGY CONSTANTS
# ══════════════════════════════════════════════════════════════════════════════

OKX_BASE          = "https://www.okx.com"
OKX_DEMO_BASE     = "https://www.okx.com"          # OKX uses same host, demo via header
CONA_BASE         = "https://api.coinalyze.net/v1"

SYMBOLS: Dict[str, str] = {
    "BTC-USDT": "BTC/USDT",
    "ETH-USDT": "ETH/USDT",
    "SOL-USDT": "SOL/USDT",
}
OKX_SWAP: Dict[str, str] = {
    "BTC/USDT": "BTC-USDT-SWAP",
    "ETH/USDT": "ETH-USDT-SWAP",
    "SOL/USDT": "SOL-USDT-SWAP",
}
CONA_SPOT: Dict[str, str] = {
    "BTC/USDT": "BTCUSDT.A",
    "ETH/USDT": "ETHUSDT.A",
    "SOL/USDT": "SOLUSDT.A",
}
CONA_PERP: Dict[str, str] = {
    "BTC/USDT": "BTCUSDT_PERP.A",
    "ETH/USDT": "ETHUSDT_PERP.A",
    "SOL/USDT": "SOLUSDT_PERP.A",
}
BAR_TO_CONA = {"5m":"5min","15m":"15min","30m":"30min","1H":"1hour","4H":"4hour"}
BAR_SECONDS  = {"5m":300,"15m":900,"30m":1800,"1H":3600,"4H":14400}
WARMUP       = 700

# QQE MOD — fixed, not optimised
QQE_RSI       = 6
QQE_SF        = 5
QQE_FACTOR_P  = 3.0
QQE_FACTOR_S  = 1.61
QQE_THRESHOLD = 3

ADX_THR           = 25
MAX_SL            = 0.12
FUNDING_LONG_MAX  = 0.0001    #  0.01%
FUNDING_SHORT_MIN = -0.0001   # -0.01%
CVD_WINDOW        = 3
BEAR_MIN_BARS     = 20        # consecutive 1H bars of e144 < e576 → bear market

MAX_CONSEC_LOSS   = 3
PAUSE_HOURS       = 24

# ── Hard-coded best params (from final_params_all.json) ───────────────────────
BEST_PARAMS: Dict[str, Dict] = {
    "1H_long":  {"tp1_mult":1.3, "tp2_intraday_mult":2.0, "tp2_swing_mult":4.0,
                 "sl_atr_buffer":0.15, "structure_lookback":10, "exit_mode":"fixed"},
    "1H_short": {"tp1_mult":1.7, "tp2_intraday_mult":1.8, "tp2_swing_mult":2.5,
                 "sl_atr_buffer":0.08, "structure_lookback":20, "exit_mode":"fixed"},
    "15m_long": {"tp1_mult":1.5, "tp2_intraday_mult":2.2, "tp2_swing_mult":2.5,
                 "sl_atr_buffer":0.01, "structure_lookback":28, "exit_mode":"fixed"},
    "15m_short":{"tp1_mult":1.7, "tp2_intraday_mult":1.8, "tp2_swing_mult":2.5,
                 "sl_atr_buffer":0.08, "structure_lookback":20, "exit_mode":"fixed"},
    "30m_long": {"tp1_mult":1.7, "tp2_intraday_mult":2.2, "tp2_swing_mult":2.5,
                 "sl_atr_buffer":0.05, "structure_lookback":10, "exit_mode":"fixed"},
    "30m_short":{"tp1_mult":1.15,"tp2_intraday_mult":1.35,"tp2_swing_mult":2.25,
                 "sl_atr_buffer":0.01, "structure_lookback":10, "exit_mode":"trailing"},
    "5m_long":  {"tp1_mult":1.7, "tp2_intraday_mult":1.8, "tp2_swing_mult":2.5,
                 "sl_atr_buffer":0.08, "structure_lookback":20, "exit_mode":"fixed"},
    "5m_short": {"tp1_mult":1.7, "tp2_intraday_mult":1.8, "tp2_swing_mult":2.5,
                 "sl_atr_buffer":0.08, "structure_lookback":20, "exit_mode":"fixed"},
    "4H_long":  {"tp1_mult":1.3, "tp2_intraday_mult":1.5, "tp2_swing_mult":3.0,
                 "sl_atr_buffer":0.03, "structure_lookback":10, "exit_mode":"trailing"},
    "4H_short": {"tp1_mult":1.5, "tp2_intraday_mult":1.8, "tp2_swing_mult":4.0,
                 "sl_atr_buffer":0.05, "structure_lookback":30, "exit_mode":"fixed"},
}

def get_params(tf: str, side: str, base_dir: str = ".") -> Dict:
    """Load from best_params_<tf>_<side>.json or final_params_all.json, else use built-in."""
    trade_keys = {"tp1_mult","tp2_intraday_mult","tp2_swing_mult",
                  "sl_atr_buffer","structure_lookback","exit_mode"}
    for fname in [
        os.path.join(base_dir, f"best_params_{tf.lower()}_{side}.json"),
        os.path.join(base_dir, "final_params_all.json"),
    ]:
        if os.path.exists(fname):
            with open(fname, encoding="utf-8") as f:
                raw = json.load(f)
            # final_params_all.json uses tf_side keys
            data = raw.get(f"{tf}_{side}", raw.get("params", raw))
            p = {k: v for k, v in data.items() if k in trade_keys}
            if len(p) >= 4:
                print(f"  [params] {tf} {side} ← {os.path.basename(fname)}")
                return {**BEST_PARAMS.get(f"{tf}_{side}", {}), **p}
    key = f"{tf}_{side}"
    if key in BEST_PARAMS:
        print(f"  [params] {tf} {side} ← built-in defaults")
        return BEST_PARAMS[key].copy()
    return {"tp1_mult":1.7,"tp2_intraday_mult":1.8,"tp2_swing_mult":2.5,
            "sl_atr_buffer":0.08,"structure_lookback":20,"exit_mode":"fixed"}


# ══════════════════════════════════════════════════════════════════════════════
#  TELEGRAM
# ══════════════════════════════════════════════════════════════════════════════

def tg(text: str):
    if not TG_BOT_TOKEN or not TG_CHAT_ID:
        return
    try:
        requests.post(
            f"https://api.telegram.org/bot{TG_BOT_TOKEN}/sendMessage",
            json={"chat_id": TG_CHAT_ID, "text": text,
                  "parse_mode": "HTML", "disable_web_page_preview": True},
            timeout=10,
        )
    except Exception as e:
        print(f"  [TG] {e}")


def tg_signal(sig: Dict, symbol: str, tf: str, cvd_active: bool):
    side_emoji = "🟢" if sig["side"] == "long" else "🔴"
    swing_tag  = "📐 波段" if sig["is_swing"] else "⚡ 日內"
    cvd_tag    = "✅ CVD確認" if cvd_active else "⚠️ CVD無資料"
    text = (
        f"{side_emoji} <b>{symbol}  [{tf} {sig['side'].upper()}]</b>\n"
        f"時間: {sig['time'][:19]} UTC\n"
        f"進場: <code>{sig['entry']}</code>\n"
        f"停損: <code>{sig['sl']}</code>  (風險 {sig['risk_pct']:.2f}%)\n"
        f"TP1 : <code>{sig['tp1']}</code>  (R={sig['rr1']:.2f})\n"
        f"TP2 : <code>{sig['tp2']}</code>  (R={sig['rr2']:.2f})\n"
        f"ATR : {sig['atr']}\n"
        f"出場: {sig['exit_mode']}  {swing_tag}\n"
        f"{cvd_tag}"
    )
    tg(text)


def tg_exit(symbol: str, tf: str, side: str, msg: str):
    emoji = "✅" if "TP" in msg else "🛑" if "SL" in msg else "⏸"
    tg(f"{emoji} <b>{symbol} [{tf} {side.upper()}] 出場</b>\n{msg}")


def tg_pause(symbol: str, tf: str, side: str, resume: datetime):
    tg(f"⏸ <b>{symbol} [{tf} {side.upper()}] 連虧{MAX_CONSEC_LOSS}單暫停</b>\n"
       f"恢復時間: {resume.strftime('%m/%d %H:%M')} UTC")


# ══════════════════════════════════════════════════════════════════════════════
#  OKX API HELPERS
# ══════════════════════════════════════════════════════════════════════════════

def _okx_ts() -> str:
    return datetime.utcnow().strftime("%Y-%m-%dT%H:%M:%S.%f")[:-3] + "Z"

def _okx_sign(ts: str, method: str, path: str, body: str = "") -> str:
    msg = ts + method + path + body
    sig = hmac.new(OKX_SECRET.encode(), msg.encode(), hashlib.sha256).digest()
    return base64.b64encode(sig).decode()

def _okx_headers(method: str, path: str, body: str = "") -> Dict:
    ts = _okx_ts()
    return {
        "OK-ACCESS-KEY":        OKX_API_KEY,
        "OK-ACCESS-SIGN":       _okx_sign(ts, method, path, body),
        "OK-ACCESS-TIMESTAMP":  ts,
        "OK-ACCESS-PASSPHRASE": OKX_PASSPHRASE,
        "Content-Type":         "application/json",
        **({"x-simulated-trading": "1"} if OKX_DEMO else {}),
    }

def okx_get_balance() -> float:
    """Returns total USDT equity."""
    path = "/api/v5/account/balance?ccy=USDT"
    try:
        r = requests.get(OKX_BASE + path, headers=_okx_headers("GET", path), timeout=10).json()
        details = r.get("data", [{}])[0].get("details", [])
        for d in details:
            if d.get("ccy") == "USDT":
                return float(d.get("eq", 0))
    except Exception as e:
        print(f"  [OKX balance] {e}")
    return 0.0

def okx_place_order(inst_id: str, side: str, sz: str, sl_px: str) -> Dict:
    """
    Place market order + attach SL.
    side: "buy" (long) or "sell" (short)
    sz: number of contracts (string)
    sl_px: stop-loss trigger price (string)
    """
    path = "/api/v5/trade/order"
    pos_side = "long" if side == "buy" else "short"
    body = json.dumps({
        "instId":     inst_id,
        "tdMode":     "cross",
        "side":       side,
        "posSide":    pos_side,
        "ordType":    "market",
        "sz":         sz,
        "slTriggerPx": sl_px,
        "slOrdPx":    "-1",      # market SL
        "tpTriggerPxType": "last",
        "slTriggerPxType": "last",
    })
    try:
        r = requests.post(
            OKX_BASE + path,
            headers=_okx_headers("POST", path, body),
            data=body, timeout=10
        ).json()
        return r
    except Exception as e:
        return {"error": str(e)}

def calc_contracts(symbol: str, entry: float, sl: float, balance_usdt: float) -> int:
    """Calculate contract quantity based on account risk."""
    ct  = CONTRACT_SIZE.get(symbol, 0.01)
    risk_per_contract = abs(entry - sl) * ct   # USDT risk per contract
    if risk_per_contract <= 0:
        return 1
    risk_usdt = balance_usdt * RISK_PCT
    return max(1, round(risk_usdt / risk_per_contract))

def okx_close_position(inst_id: str, pos_side: str) -> Dict:
    """Close an open position at market."""
    path = "/api/v5/trade/close-position"
    body = json.dumps({"instId": inst_id, "mgnMode": "cross", "posSide": pos_side})
    try:
        r = requests.post(
            OKX_BASE + path,
            headers=_okx_headers("POST", path, body),
            data=body, timeout=10
        ).json()
        return r
    except Exception as e:
        return {"error": str(e)}


# ══════════════════════════════════════════════════════════════════════════════
#  DATA FETCHING
# ══════════════════════════════════════════════════════════════════════════════

def _okx_pub(path: str, params: dict) -> list:
    try:
        d = requests.get(f"https://www.okx.com{path}", params=params, timeout=15).json()
        return d.get("data", []) if d.get("code") == "0" else []
    except: return []

def fetch_ohlcv(inst_id: str, bar: str, limit: int = WARMUP) -> pd.DataFrame:
    rows = sorted(
        _okx_pub("/api/v5/market/candles", {"instId": inst_id, "bar": bar, "limit": min(limit, 300)}),
        key=lambda x: int(x[0])
    )
    if not rows: return pd.DataFrame()
    df = pd.DataFrame(rows, columns=["ts","open","high","low","close","vol","vC","vCQ","confirm"])
    for c in ("open","high","low","close","vol"): df[c] = df[c].astype(float)
    df["ts"] = pd.to_datetime(df["ts"].astype(np.int64), unit="ms", utc=True)
    df.set_index("ts", inplace=True)
    return df.iloc[:-1]   # drop unclosed bar

def fetch_funding_now(swap_id: str) -> float:
    rows = _okx_pub("/api/v5/public/funding-rate", {"instId": swap_id})
    if rows: return float(rows[0].get("fundingRate", 0))
    return float("nan")

def _cona_get(endpoint: str, params: dict) -> list:
    url = f"{CONA_BASE}/{endpoint}"
    headers = {"api-key": COINALYZE_API_KEY} if COINALYZE_API_KEY else {}
    try:
        d = requests.get(url, params=params, headers=headers, timeout=20).json()
        return d if isinstance(d, list) else d.get("result", d.get("data", []))
    except: return []

def fetch_cvd(cona_sym: str, cona_iv: str, from_ms: int, to_ms: int) -> pd.Series:
    if not COINALYZE_API_KEY:
        return pd.Series(dtype=float)
    data = _cona_get("history", {"symbols": cona_sym, "interval": cona_iv,
                                  "from": from_ms//1000, "to": to_ms//1000,
                                  "convert_to_usd": "false"})
    rows = []
    for item in data:
        if not isinstance(item, dict): continue
        ts = item.get("t", item.get("time", 0))
        v  = float(item.get("v", 0) or 0)
        bv = float(item.get("bv", item.get("buy_volume", v/2)) or v/2)
        rows.append((int(ts)*1000, 2*bv - v))
    if not rows: return pd.Series(dtype=float)
    df_c = pd.DataFrame(rows, columns=["ts","delta"])
    df_c["ts"] = pd.to_datetime(df_c["ts"].astype(np.int64), unit="ms", utc=True)
    df_c = df_c.set_index("ts").sort_index()
    return df_c["delta"].cumsum()

def fetch_oi(cona_sym: str, cona_iv: str, from_ms: int, to_ms: int) -> pd.Series:
    if not COINALYZE_API_KEY:
        return pd.Series(dtype=float)
    data = _cona_get("open-interest-history", {"symbols": cona_sym, "interval": cona_iv,
                                                "from": from_ms//1000, "to": to_ms//1000})
    rows = []
    for item in data:
        if not isinstance(item, dict): continue
        ts = item.get("t", item.get("time", 0))
        v  = float(item.get("v", item.get("oi", 0)) or 0)
        rows.append((int(ts)*1000, v))
    if not rows: return pd.Series(dtype=float)
    df_o = pd.DataFrame(rows, columns=["ts","oi"])
    df_o["ts"] = pd.to_datetime(df_o["ts"].astype(np.int64), unit="ms", utc=True)
    return df_o.set_index("ts").sort_index()["oi"]


# ══════════════════════════════════════════════════════════════════════════════
#  INDICATORS
# ══════════════════════════════════════════════════════════════════════════════

def precompute_base(df: pd.DataFrame) -> pd.DataFrame:
    df = df.copy()
    for s in (12, 144, 169, 576, 676):
        df[f"e{s}"] = df["close"].ewm(span=s, adjust=False).mean()
    df["sTop"] = df[["e144","e169"]].max(axis=1)
    df["sBot"] = df[["e144","e169"]].min(axis=1)
    df["lTop"] = df[["e576","e676"]].max(axis=1)
    df["lBot"] = df[["e576","e676"]].min(axis=1)
    hi, lo, cl = df["high"], df["low"], df["close"]
    tr = pd.concat([hi-lo, (hi-cl.shift()).abs(), (lo-cl.shift()).abs()], axis=1).max(axis=1)
    df["atr"]        = tr.ewm(span=14, adjust=False).mean()
    df["channel_ok"] = (df["e144"] - df["e576"]).abs() >= df["atr"] * 2
    return df

def _ws(a: np.ndarray, p: int) -> np.ndarray:
    r = np.zeros(len(a)); r[p] = a[1:p+1].sum()
    for i in range(p+1, len(a)): r[i] = r[i-1] - r[i-1]/p + a[i]
    return r

def adx_series(df: pd.DataFrame, period: int = 14) -> pd.Series:
    hi, lo, cl = df["high"].values, df["low"].values, df["close"].values
    n = len(hi); tr_a, pdm, mdm = np.zeros(n), np.zeros(n), np.zeros(n)
    for i in range(1, n):
        tr_a[i] = max(hi[i]-lo[i], abs(hi[i]-cl[i-1]), abs(lo[i]-cl[i-1]))
        u = hi[i]-hi[i-1]; dn = lo[i-1]-lo[i]
        pdm[i] = u if u > dn and u > 0 else 0
        mdm[i] = dn if dn > u and dn > 0 else 0
    atr_ = _ws(tr_a, period); pdi = _ws(pdm, period); mdi = _ws(mdm, period)
    with np.errstate(divide="ignore", invalid="ignore"):
        p_ = np.where(atr_>0, 100*pdi/atr_, 0); m_ = np.where(atr_>0, 100*mdi/atr_, 0)
        dx = np.where(p_+m_>0, 100*np.abs(p_-m_)/(p_+m_), 0)
    return pd.Series(_ws(dx, period), index=df.index)

def add_swing(df: pd.DataFrame) -> pd.DataFrame:
    df = df.copy()
    df["adx"] = adx_series(df, 14)
    agg = {"open":"first","high":"max","low":"min","close":"last"}
    df4 = df[["open","high","low","close"]].resample("4h").agg(agg).dropna()
    df4["e144_4h"] = df4["close"].ewm(span=144, adjust=False).mean()
    df4["e576_4h"] = df4["close"].ewm(span=576, adjust=False).mean()
    df4["trend4h"] = (df4["e144_4h"] > df4["e576_4h"]).shift(1)
    df4["slope4h"] = (df4["e576_4h"] > df4["e576_4h"].shift(20)).shift(1)
    df["trend4h"] = df4["trend4h"].reindex(df.index, method="ffill").fillna(False)
    df["slope4h"] = df4["slope4h"].reindex(df.index, method="ffill").fillna(False)
    df["is_swing"] = df["trend4h"] & df["slope4h"] & (df["adx"] > ADX_THR)
    return df

def calc_qqe(close: pd.Series, rsi_period: int, sf: int,
             factor: float, threshold: float = 3.0):
    delta = close.diff()
    g = delta.clip(lower=0).ewm(com=rsi_period-1, adjust=False).mean()
    l = (-delta.clip(upper=0)).ewm(com=rsi_period-1, adjust=False).mean()
    rsiMa = (100-(100/(1+g/l))).ewm(span=sf, adjust=False).mean()
    atrRsi = rsiMa.diff().abs()
    dar = (atrRsi.ewm(span=rsi_period, adjust=False).mean()
                 .ewm(span=rsi_period, adjust=False).mean() * factor)
    rm, d = rsiMa.values, dar.values; n = len(rm)
    lb = np.zeros(n); sb = np.full(n, np.inf); tr = np.zeros(n, dtype=np.int8)
    for i in range(1, n):
        if np.isnan(rm[i]) or np.isnan(d[i]) or d[i] <= 0:
            lb[i]=lb[i-1]; sb[i]=sb[i-1]; tr[i]=tr[i-1]; continue
        nlb = rm[i]-d[i]; nsb = rm[i]+d[i]
        lb[i] = (max(lb[i-1],nlb) if rm[i-1]>lb[i-1] and rm[i]>lb[i-1] else nlb)
        sb[i] = (min(sb[i-1],nsb) if rm[i-1]<sb[i-1] and rm[i]<sb[i-1] else nsb)
        if   rm[i] > sb[i-1]: tr[i] = 1
        elif rm[i] < lb[i-1]: tr[i] = -1
        else: tr[i] = tr[i-1] if tr[i-1] != 0 else 1
    prev = np.concatenate([[0], tr[:-1]])
    bull = pd.Series((tr==1)&(prev!=1)&(rm>50+threshold), index=close.index)
    bear = pd.Series((tr==-1)&(prev!=-1)&(rm<50-threshold), index=close.index)
    bull.iloc[0] = bear.iloc[0] = False
    return bull, bear, tr

def add_dual_qqe(df: pd.DataFrame) -> pd.DataFrame:
    bull1, bear1, tr1 = calc_qqe(df["close"], QQE_RSI, QQE_SF, QQE_FACTOR_P, QQE_THRESHOLD)
    _,     _,     tr2 = calc_qqe(df["close"], QQE_RSI, QQE_SF, QQE_FACTOR_S, QQE_THRESHOLD)
    df = df.copy()
    df["qbull"] = bull1 & (tr2 == 1)
    df["qbear"] = bear1 & (tr2 == -1)
    df["qtr"]   = np.where((tr1==1)&(tr2==1), 1,
                   np.where((tr1==-1)&(tr2==-1), -1, 0)).astype(np.int8)
    return df

def is_bear_market(df_1h: pd.DataFrame, min_bars: int = BEAR_MIN_BARS) -> bool:
    """
    Returns True when EMA144 has been below EMA576 for at least min_bars
    consecutive closed 1H bars — indicating a confirmed downtrend.
    """
    e144 = df_1h["e144"].values
    e576 = df_1h["e576"].values
    count = 0
    for i in range(len(e144)-1, -1, -1):
        if e144[i] < e576[i]: count += 1
        else: break
    return count >= min_bars


# ══════════════════════════════════════════════════════════════════════════════
#  CVD REVERSAL FILTER
# ══════════════════════════════════════════════════════════════════════════════

def _cvd_turning_up(arr: np.ndarray, i: int, w: int = CVD_WINDOW) -> bool:
    if i < w-1: return False
    recent = arr[i-w+1:i+1]
    if len(recent) < w or np.any(np.isnan(recent)): return False
    return bool(recent[-1] > recent[-2] and recent[-2] <= recent[0])

def _cvd_turning_down(arr: np.ndarray, i: int, w: int = CVD_WINDOW) -> bool:
    if i < w-1: return False
    recent = arr[i-w+1:i+1]
    if len(recent) < w or np.any(np.isnan(recent)): return False
    return bool(recent[-1] < recent[-2] and recent[-2] >= recent[0])

def check_cvd(df: pd.DataFrame, side: str) -> Tuple[Optional[bool], bool]:
    """
    Returns (result, cvd_was_active).
    result=None → data unavailable → caller skips CVD filter.
    result=False → data present, filter failed.
    result=True → passed.
    cvd_was_active → whether Coinalyze data was present.
    """
    if "cvd_spot" not in df.columns:
        return None, False
    n = len(df); i = n-1
    cvds = df["cvd_spot"].values
    cvdp = df["cvd_perp"].values if "cvd_perp" in df.columns else np.full(n, np.nan)
    oi   = df["oi"].values       if "oi"       in df.columns else np.full(n, np.nan)
    if np.isnan(cvds[i]) or np.isnan(cvdp[i]) or np.isnan(oi[i]) or i < 1:
        return None, False
    oi_up = oi[i] > oi[i-1]
    if side == "long":
        ok = _cvd_turning_up(cvds,i) and _cvd_turning_up(cvdp,i) and oi_up
    else:
        ok = _cvd_turning_down(cvds,i) and _cvd_turning_down(cvdp,i) and oi_up
    return ok, True


# ══════════════════════════════════════════════════════════════════════════════
#  PIVOT HELPERS
# ══════════════════════════════════════════════════════════════════════════════

def _piv_low(lo: np.ndarray, idx: int, lb: int, n: int = 2) -> float:
    w = lo[max(0, idx-lb-1):idx-1]
    pts = [w[j] for j in range(n, len(w)-n)
           if all(w[j]<w[j-k] for k in range(1,n+1)) and all(w[j]<w[j+k] for k in range(1,n+1))]
    return pts[-1] if pts else (float(w.min()) if len(w) > 0 else float("nan"))

def _piv_high(hi: np.ndarray, idx: int, lb: int, n: int = 2) -> float:
    w = hi[max(0, idx-lb-1):idx-1]
    pts = [w[j] for j in range(n, len(w)-n)
           if all(w[j]>w[j-k] for k in range(1,n+1)) and all(w[j]>w[j+k] for k in range(1,n+1))]
    return pts[-1] if pts else (float(w.max()) if len(w) > 0 else float("nan"))


# ══════════════════════════════════════════════════════════════════════════════
#  SIGNAL DETECTION
# ══════════════════════════════════════════════════════════════════════════════

def check_signal(df: pd.DataFrame, params: Dict, side: str,
                 funding_now: float = float("nan"),
                 bear_market: bool = False) -> Optional[Dict]:
    """
    Returns signal dict or None.
    df must have QQE + channel_ok + swing + (optionally) CVD columns.
    """
    i = len(df) - 1
    if i < 10: return None

    cl    = df["close"].values;   hi   = df["high"].values;   lo  = df["low"].values
    e12   = df["e12"].values;     e144 = df["e144"].values;   e576 = df["e576"].values
    sBot  = df["sBot"].values;    sTop = df["sTop"].values
    atr_  = df["atr"].values
    qbull = df["qbull"].values;   qbear = df["qbear"].values
    swing = df["is_swing"].values if "is_swing" in df.columns else np.zeros(len(df), bool)
    ch_ok = df["channel_ok"].values if "channel_ok" in df.columns else np.ones(len(df), bool)

    lb  = int(params["structure_lookback"])
    buf = float(params["sl_atr_buffer"])

    # ── Channel entanglement guard ────────────────────────────────────────────
    if not ch_ok[i]: return None

    # ── Funding rate gate ─────────────────────────────────────────────────────
    if not math.isnan(funding_now):
        if side == "long"  and funding_now >= FUNDING_LONG_MAX:  return None
        if side == "short" and funding_now <= FUNDING_SHORT_MIN: return None

    # ── Bear-market gate for shorts ───────────────────────────────────────────
    if side == "short" and not bear_market:
        return None

    # ── QQE dual + trend zone ─────────────────────────────────────────────────
    if side == "long":
        if not qbull[i]: return None
        if not (e144[i] > e576[i]): return None      # medium-term uptrend
        if not (cl[i] > sBot[i]):   return None      # above EMA144/169 floor
        if not (cl[i] > e12[i]):    return None      # above fast EMA
        base = _piv_low(lo, i, lb)
        if math.isnan(base): return None
        sl   = base - atr_[i] * buf
        risk = cl[i] - sl
    else:
        if not qbear[i]: return None
        if not (e144[i] < e576[i]): return None      # medium-term downtrend
        if not (cl[i] < sTop[i]):   return None      # below EMA144/169 ceiling
        if not (cl[i] < e12[i]):    return None      # below fast EMA
        base = _piv_high(hi, i, lb)
        if math.isnan(base): return None
        sl   = base + atr_[i] * buf
        risk = sl - cl[i]

    # ── CVD three-layer (skip if data unavailable) ────────────────────────────
    cvd_result, cvd_active = check_cvd(df, side)
    if cvd_result is False:   # data present but filter failed
        return None

    entry = cl[i]
    if risk <= 0 or (risk / entry) > MAX_SL: return None

    is_swing  = bool(swing[i])
    tp2_mult  = float(params["tp2_swing_mult"] if is_swing else params["tp2_intraday_mult"])
    tp1_mult  = float(params["tp1_mult"])
    exit_mode = str(params["exit_mode"])
    sign      = 1 if side == "long" else -1

    return {
        "side":       side,
        "time":       df.index[i].isoformat(),
        "bar_ts":     df.index[i].isoformat(),
        "entry":      round(entry, 6),
        "sl":         round(sl, 6),
        "tp1":        round(entry + sign*risk*tp1_mult, 6),
        "tp2":        round(entry + sign*risk*tp2_mult, 6),
        "risk_pct":   round(risk/entry*100, 3),
        "rr1":        round(tp1_mult, 3),
        "rr2":        round(tp2_mult, 3),
        "exit_mode":  exit_mode,
        "is_swing":   is_swing,
        "atr":        round(float(atr_[i]), 6),
        "cvd_active": cvd_active,
    }


# ══════════════════════════════════════════════════════════════════════════════
#  PAPER POSITION TRACKER
# ══════════════════════════════════════════════════════════════════════════════

class PaperPosition:
    def __init__(self, signal: Dict, symbol: str, tf: str):
        self.symbol    = symbol; self.tf    = tf
        self.side      = signal["side"]
        self.entry     = signal["entry"]
        self.sl        = signal["sl"]
        self.tp1       = signal["tp1"]
        self.tp2       = signal["tp2"]
        self.exit_mode = signal["exit_mode"]
        self.tp1_hit   = False
        self.open      = True

    def update(self, df: pd.DataFrame) -> Optional[str]:
        if not self.open: return None
        bar = df.iloc[-1]
        h, l, c = bar["high"], bar["low"], bar["close"]
        e12  = bar["e12"]; e144 = bar["e144"]; e576 = bar["e576"]
        qtr  = int(df["qtr"].iloc[-1])

        if not self.tp1_hit:
            if (self.side=="long" and l<=self.sl) or (self.side=="short" and h>=self.sl):
                self.open = False; return f"SL hit @ {self.sl:.4f}"
            if (self.side=="long" and h>=self.tp1) or (self.side=="short" and l<=self.tp1):
                self.tp1_hit = True; self.sl = self.entry
                ts = df.index[-1].strftime("%H:%M")
                print(f"    [{ts}] [{self.symbol} {self.tf} {self.side}] TP1 → SL=entry @ {self.entry:.4f}")
        else:
            if (self.side=="long" and l<=self.entry) or (self.side=="short" and h>=self.entry):
                self.open = False; return f"Breakeven stop @ {self.entry:.4f}"
            if self.exit_mode == "fixed":
                if (self.side=="long" and h>=self.tp2) or (self.side=="short" and l<=self.tp2):
                    self.open = False; return f"TP2 hit @ {self.tp2:.4f}"
            else:
                rev_q   = (qtr==-1) if self.side=="long" else (qtr==1)
                rev_e12 = (c<e12)   if self.side=="long" else (c>e12)
                rev_tr  = (e144<e576) if self.side=="long" else (e144>e576)
                if rev_q or rev_e12 or rev_tr:
                    reason = "QQE rev" if rev_q else "EMA12" if rev_e12 else "EMA144/576"
                    self.open = False; return f"Trail exit ({reason}) @ {c:.4f}"
        return None


# ══════════════════════════════════════════════════════════════════════════════
#  CORE BOT
# ══════════════════════════════════════════════════════════════════════════════

class TradingBotV3:
    def __init__(self, tf_plan: List[Tuple], sides: List[str],
                 live: bool = False, base_dir: str = "."):
        self.tf_plan  = tf_plan    # [(bar_str, poll_sec, sides), ...]
        self.sides    = sides
        self.live     = live
        self.base_dir = base_dir

        # Load params for each (tf, side)
        self.params: Dict[str, Dict] = {}
        for tf, _, tf_sides in tf_plan:
            for s in tf_sides:
                if s in sides:
                    self.params[f"{tf}_{s}"] = get_params(tf, s, base_dir)

        # Paper positions: key = f"{inst_id}_{tf}_{side}"
        self.positions: Dict[str, Optional[PaperPosition]] = {}
        # Live order IDs: key = f"{inst_id}_{tf}_{side}"
        self.live_orders: Dict[str, str] = {}
        # Consecutive loss counters and pause timestamps
        self.consec_loss: Dict[str, int] = {}
        self.pause_until: Dict[str, Optional[datetime]] = {}
        # Anti-duplicate: store bar timestamp of last signal per key
        self.last_signal_bar: Dict[str, str] = {}
        # Cached 1H bear-market flag per symbol (updated each full scan)
        self._bear_cache: Dict[str, bool] = {}

        for tf, _, tf_sides in tf_plan:
            for inst_id in SYMBOLS:
                for s in tf_sides:
                    if s in sides:
                        key = f"{inst_id}_{tf}_{s}"
                        self.positions[key]      = None
                        self.consec_loss[key]    = 0
                        self.pause_until[key]    = None
                        self.last_signal_bar[key]= ""

    # ── helpers ───────────────────────────────────────────────────────────────
    def _key(self, inst_id: str, tf: str, side: str) -> str:
        return f"{inst_id}_{tf}_{side}"

    def _is_paused(self, key: str) -> bool:
        p = self.pause_until.get(key)
        return p is not None and datetime.now(timezone.utc) < p

    def _record_loss(self, key: str, tf: str):
        self.consec_loss[key] += 1
        if self.consec_loss[key] >= MAX_CONSEC_LOSS:
            resume = datetime.now(timezone.utc) + timedelta(hours=PAUSE_HOURS)
            self.pause_until[key] = resume
            self.consec_loss[key] = 0
            parts = key.split("_")
            sym, side = parts[0]+"_"+parts[1], parts[-1]
            print(f"  ⏸ {sym} [{tf} {side}] {MAX_CONSEC_LOSS} consecutive losses → paused until {resume:%H:%M} UTC")
            tg_pause(sym, tf, side, resume)

    def _record_win(self, key: str):
        self.consec_loss[key] = 0

    # ── update 1H bear-market cache ───────────────────────────────────────────
    def _update_bear_cache(self):
        for inst_id, label in SYMBOLS.items():
            df1h = fetch_ohlcv(inst_id, "1H", 50)
            if df1h.empty or len(df1h) < BEAR_MIN_BARS + 10:
                self._bear_cache[inst_id] = False
                continue
            df1h = precompute_base(df1h)
            self._bear_cache[inst_id] = is_bear_market(df1h)
            mode = "🐻 BEAR" if self._bear_cache[inst_id] else "🐂 BULL"
            print(f"  {label}: {mode} market (EMA144<EMA576 for {BEAR_MIN_BARS}+ bars = {self._bear_cache[inst_id]})")
            time.sleep(0.2)

    # ── print helpers ─────────────────────────────────────────────────────────
    def _print_signal(self, sig: Dict, symbol: str, tf: str):
        cvd_tag = "CVD✓" if sig.get("cvd_active") else "CVD—"
        mode_tag = "📐波段" if sig["is_swing"] else "⚡日內"
        w = "─" * 60
        print(f"\n{w}")
        print(f"  {'🟢 LONG' if sig['side']=='long' else '🔴 SHORT'}  {symbol}  [{tf}]  {mode_tag}  {cvd_tag}")
        print(f"  Time  : {sig['time'][:19]} UTC")
        print(f"  Entry : {sig['entry']}")
        print(f"  SL    : {sig['sl']}  (risk {sig['risk_pct']:.2f}%)")
        print(f"  TP1   : {sig['tp1']}  (R={sig['rr1']:.2f})")
        print(f"  TP2   : {sig['tp2']}  (R={sig['rr2']:.2f})")
        print(f"  ATR   : {sig['atr']}  exit={sig['exit_mode']}")
        print(f"{w}\n")

    def _print_exit(self, symbol: str, tf: str, side: str, msg: str):
        ts = datetime.now(timezone.utc).strftime("%H:%M:%S")
        print(f"  [{ts}] EXIT {symbol} [{tf} {side}]: {msg}")

    # ── single symbol × tf × side scan ───────────────────────────────────────
    def _scan(self, inst_id: str, label: str, tf: str, side: str,
              df: pd.DataFrame, funding_now: float):
        key = self._key(inst_id, tf, side)
        p   = self.params.get(f"{tf}_{side}", {})

        # Pause check
        if self._is_paused(key):
            rem = int((self.pause_until[key] - datetime.now(timezone.utc)).total_seconds() // 60)
            if rem % 30 == 0:  # print reminder every 30 min
                print(f"  ⏸ {label} [{tf} {side}] paused ({rem}min left)")
            return

        pos = self.positions[key]

        # ── update existing position ──────────────────────────────────────────
        if pos and pos.open:
            msg = pos.update(df)
            if msg:
                self._print_exit(label, tf, side, msg)
                tg_exit(label, tf, side, msg)
                if "SL" in msg or "Breakeven" in msg:
                    self._record_loss(key, tf)
                else:
                    self._record_win(key)
                if self.live:
                    ps = "long" if side == "long" else "short"
                    okx_close_position(OKX_SWAP[label] if label in OKX_SWAP else "", ps)
                self.positions[key] = None
                pos = None

        # ── check for new entry ───────────────────────────────────────────────
        if pos and pos.open:
            return

        bear = self._bear_cache.get(inst_id, False)
        sig  = check_signal(df, p, side, funding_now=funding_now, bear_market=bear)
        if not sig:
            return

        # Anti-duplicate: skip if same bar already fired
        if self.last_signal_bar.get(key) == sig["bar_ts"]:
            return
        self.last_signal_bar[key] = sig["bar_ts"]

        self._print_signal(sig, label, tf)
        tg_signal(sig, label, tf, sig.get("cvd_active", False))
        self.positions[key] = PaperPosition(sig, label, tf)
        self._record_win(key)  # entering a new trade resets counter

        # ── live order ────────────────────────────────────────────────────────
        if self.live and OKX_API_KEY:
            swap_id = OKX_SWAP.get(label, "")
            bal     = okx_get_balance()
            sz      = calc_contracts(inst_id, sig["entry"], sig["sl"], bal)
            okx_side = "buy" if side == "long" else "sell"
            resp = okx_place_order(swap_id, okx_side, str(sz), str(sig["sl"]))
            if resp.get("code") == "0":
                oid = resp.get("data",[{}])[0].get("ordId","?")
                print(f"  [OKX] Order placed  id={oid}  sz={sz}  balance={bal:.0f} USDT")
            else:
                print(f"  [OKX] Order error: {resp}")

    # ── full symbol scan (builds df once, used by all tf×side) ───────────────
    def _scan_symbol_tf(self, inst_id: str, label: str, tf: str,
                        tf_sides: List[str], all_sides: List[str]):
        df_raw = fetch_ohlcv(inst_id, tf, WARMUP)
        if df_raw.empty or len(df_raw) < 200:
            return
        df = precompute_base(df_raw)
        df = add_swing(df)
        df = add_dual_qqe(df)

        # Fetch CVD + OI (last few bars)
        if COINALYZE_API_KEY:
            cona_iv  = BAR_TO_CONA.get(tf, "1hour")
            now_ms   = int(df.index[-1].timestamp() * 1000)
            look_ms  = BAR_SECONDS.get(tf, 3600) * CVD_WINDOW * 3 * 1000
            from_ms  = now_ms - look_ms

            cvd_spot = fetch_cvd(CONA_SPOT[label], cona_iv, from_ms, now_ms)
            cvd_perp = fetch_cvd(CONA_PERP[label], cona_iv, from_ms, now_ms)
            oi_s     = fetch_oi(CONA_PERP[label], cona_iv, from_ms, now_ms)
            if not cvd_spot.empty:
                df["cvd_spot"] = cvd_spot.reindex(df.index, method="ffill")
            if not cvd_perp.empty:
                df["cvd_perp"] = cvd_perp.reindex(df.index, method="ffill")
            if not oi_s.empty:
                df["oi"] = oi_s.reindex(df.index, method="ffill")

        # Funding rate (single call per symbol)
        funding_now = fetch_funding_now(OKX_SWAP.get(label, ""))

        for side in tf_sides:
            if side in all_sides:
                self._scan(inst_id, label, tf, side, df, funding_now)

    # ── public interface ──────────────────────────────────────────────────────
    def scan_once(self):
        """Scan all symbols × timeframes × sides once."""
        # Update bear-market cache first (uses 1H data regardless of tf_plan)
        if any(s == "short" for s in self.sides):
            self._update_bear_cache()

        for tf, _, tf_sides in self.tf_plan:
            for inst_id, label in SYMBOLS.items():
                self._scan_symbol_tf(inst_id, label, tf, tf_sides, self.sides)
                time.sleep(0.3)

    def print_banner(self):
        print("\n" + "="*64)
        print(f"  Trading Bot v3  |  TFs: {[tf for tf,_,_ in self.tf_plan]}")
        print(f"  Sides: {self.sides}  |  Mode: {'🔴 LIVE' if self.live else '📝 PAPER'}")
        print(f"  QQE Primary  RSI={QQE_RSI} SF={QQE_SF} F={QQE_FACTOR_P} Thr={QQE_THRESHOLD}")
        print(f"  QQE Secondary RSI={QQE_RSI} SF={QQE_SF} F={QQE_FACTOR_S}")
        print(f"  CVD: {'✅ enabled (Coinalyze)' if COINALYZE_API_KEY else '⚠️ skipped (no API key)'}")
        print(f"  Telegram: {'✅' if TG_BOT_TOKEN else '⚠️ off'}")
        print(f"  Bear gate: EMA144<EMA576 for {BEAR_MIN_BARS}+ bars (1H) → short enabled")
        print("="*64)
        for tf, _, tf_sides in self.tf_plan:
            for s in tf_sides:
                if s in self.sides:
                    p = self.params.get(f"{tf}_{s}", {})
                    print(f"\n  [{tf} {s}]")
                    for k, v in p.items():
                        print(f"    {k:<28} = {v}")
        print()

    def run(self):
        self.print_banner()
        min_poll = min(p for _, p, _ in self.tf_plan)
        print(f"  Poll interval: {min_poll}s  |  Symbols: {list(SYMBOLS.values())}")
        print("  Press Ctrl+C to stop\n")

        tg(f"🤖 <b>Trading Bot v3 started</b>\n"
           f"TFs: {[tf for tf,_,_ in self.tf_plan]}\n"
           f"Mode: {'LIVE' if self.live else 'PAPER'}\n"
           f"CVD: {'ON' if COINALYZE_API_KEY else 'OFF'}")

        while True:
            now = datetime.now(timezone.utc).strftime("%Y-%m-%d %H:%M:%S UTC")
            print(f"[{now}] Scanning ...", end="  ", flush=True)
            try:
                self.scan_once()
            except KeyboardInterrupt:
                print("\nStopped.")
                tg("🛑 Trading Bot v3 stopped.")
                break
            except Exception as e:
                print(f"\n  [ERR] {e}")
            print("done")
            time.sleep(min_poll)


# ══════════════════════════════════════════════════════════════════════════════
#  CLI
# ══════════════════════════════════════════════════════════════════════════════

def main():
    ap = argparse.ArgumentParser(
        description="Trading Bot v3 — QQE MOD dual-confirmation full-filter bot",
        formatter_class=argparse.RawDescriptionHelpFormatter,
        epilog=__doc__,
    )
    ap.add_argument("--tf",   default="all",
                    help="Timeframe: 5m/15m/30m/1H/4H/all (default: all = 15m+1H)")
    ap.add_argument("--side", default="both",
                    choices=["long","short","both"],
                    help="Trade side (default: both)")
    ap.add_argument("--live", action="store_true",
                    help="Enable real OKX order execution")
    ap.add_argument("--once", action="store_true",
                    help="Single scan then exit")
    ap.add_argument("--dir",  default=".",
                    help="Directory with param JSON files (default: .)")
    args = ap.parse_args()

    sides = ["long","short"] if args.side == "both" else [args.side]

    # Build TF plan
    ALL_TF_POLL = {"5m":60,"15m":180,"30m":300,"1H":600,"4H":900}
    if args.tf == "all":
        tf_plan = [(tf, poll, sides) for tf, poll in [("15m",180),("1H",600)]]
    elif args.tf in ALL_TF_POLL:
        tf_plan = [(args.tf, ALL_TF_POLL[args.tf], sides)]
    else:
        print(f"Unknown --tf '{args.tf}', using 1H"); tf_plan = [("1H",600,sides)]

    if args.live and not OKX_API_KEY:
        print("WARNING: --live requires OKX_API_KEY / OKX_SECRET / OKX_PASSPHRASE in config.")
        print("Falling back to paper mode.")
        args.live = False

    bot = TradingBotV3(tf_plan=tf_plan, sides=sides,
                       live=args.live, base_dir=args.dir)
    if args.once:
        bot.print_banner()
        bot.scan_once()
    else:
        bot.run()


if __name__ == "__main__":
    main()
