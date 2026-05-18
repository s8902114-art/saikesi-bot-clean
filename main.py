#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
賽克斯訊號機器人 v4 — Production-Grade QQE MOD Signal Bot
TG 按鈕確認下單 | K棒收盤觸發 | 40+ 幣種 | 四時框 15m/30m/1H/4H
"""
import sys, io
if hasattr(sys.stdout, "reconfigure"):
    sys.stdout.reconfigure(encoding="utf-8", errors="replace")
else:
    sys.stdout = io.TextIOWrapper(sys.stdout.buffer, encoding="utf-8", errors="replace")

import subprocess
for _pkg in ["requests", "pandas", "numpy", "ccxt", "flask"]:
    try: __import__(_pkg)
    except ImportError: subprocess.check_call([sys.executable, "-m", "pip", "install", _pkg, "-q"])

import argparse, base64, hashlib, hmac, json, math, os, time
from datetime import datetime, timezone, timedelta
from threading import Thread
from time import sleep
from typing import Dict, List, Optional, Tuple

import ccxt
import numpy as np
import pandas as pd
import requests
from flask import Flask

# ══════════════════════════════════════════════════════════════════════════════
#  USER CONFIGURATION
# ══════════════════════════════════════════════════════════════════════════════

COINALYZE_API_KEY = "82087740-b30d-479f-8846-5ffb51540b19"
TG_BOT_TOKEN      = "7642408367:AAG_6HS6BLeHtST2cKjNjaU6Ajpmbe_cj8w"
TG_CHAT_ID        = "8799334828"

OKX_API_KEY    = os.environ.get("OKX_API_KEY", "")
OKX_SECRET     = os.environ.get("OKX_SECRET_KEY", "")
OKX_PASSPHRASE = os.environ.get("OKX_PASSPHRASE", "")
OKX_DEMO       = False

MAX_LEVERAGE     = 100    # 最高槓桿上限（可由 /setmaxlev 修改）
MARGIN_PCT       = 10.0   # 每倉保證金佔可用餘額 %（可由 /setrisk 修改）
SIGNAL_COOLDOWN  = 1800   # 防重複：同幣同時框同方向 30 分鐘內不重發
_LIVE_MODE       = False  # 可由 /setlive /setpaper 切換
_BOT_START_TS    = time.time()
_bot_ref         = None   # TradingBotV3 實例（供 /status 查詢未平倉數）

# ══════════════════════════════════════════════════════════════════════════════
#  FIXED STRATEGY CONSTANTS
# ══════════════════════════════════════════════════════════════════════════════

OKX_BASE  = "https://www.okx.com"
CONA_BASE = "https://api.coinalyze.net/v1"

# inst_id → label（永續合約）
SYMBOLS: Dict[str, str] = {
    "BTC-USDT-SWAP":    "BTC/USDT",
    "ETH-USDT-SWAP":    "ETH/USDT",
    "SOL-USDT-SWAP":    "SOL/USDT",
    "XRP-USDT-SWAP":    "XRP/USDT",
    "BNB-USDT-SWAP":    "BNB/USDT",
    "DOGE-USDT-SWAP":   "DOGE/USDT",
    "ADA-USDT-SWAP":    "ADA/USDT",
    "TRX-USDT-SWAP":    "TRX/USDT",
    "SUI-USDT-SWAP":    "SUI/USDT",
    "LINK-USDT-SWAP":   "LINK/USDT",
    "AVAX-USDT-SWAP":   "AVAX/USDT",
    "TON-USDT-SWAP":    "TON/USDT",
    "HBAR-USDT-SWAP":   "HBAR/USDT",
    "XLM-USDT-SWAP":    "XLM/USDT",
    "BCH-USDT-SWAP":    "BCH/USDT",
    "LTC-USDT-SWAP":    "LTC/USDT",
    "DOT-USDT-SWAP":    "DOT/USDT",
    "UNI-USDT-SWAP":    "UNI/USDT",
    "TAO-USDT-SWAP":    "TAO/USDT",
    "NEAR-USDT-SWAP":   "NEAR/USDT",
    "APT-USDT-SWAP":    "APT/USDT",
    "ARB-USDT-SWAP":    "ARB/USDT",
    "ATOM-USDT-SWAP":   "ATOM/USDT",
    "ETC-USDT-SWAP":    "ETC/USDT",
    "ICP-USDT-SWAP":    "ICP/USDT",
    "AAVE-USDT-SWAP":   "AAVE/USDT",
    "RENDER-USDT-SWAP": "RENDER/USDT",
    "FIL-USDT-SWAP":    "FIL/USDT",
    "ENA-USDT-SWAP":    "ENA/USDT",
    "ALGO-USDT-SWAP":   "ALGO/USDT",
    "WLD-USDT-SWAP":    "WLD/USDT",
    "ONDO-USDT-SWAP":   "ONDO/USDT",
    "JUP-USDT-SWAP":    "JUP/USDT",
    "POL-USDT-SWAP":    "POL/USDT",
    "ZEC-USDT-SWAP":    "ZEC/USDT",
    "DASH-USDT-SWAP":   "DASH/USDT",
    "PENGU-USDT-SWAP":  "PENGU/USDT",
    "MORPHO-USDT-SWAP": "MORPHO/USDT",
    "HYPE-USDT-SWAP":   "HYPE/USDT",
    "SKY-USDT-SWAP":    "SKY/USDT",
}

# label → swap inst_id（自動由 SYMBOLS 反推）
OKX_SWAP: Dict[str, str] = {v: k for k, v in SYMBOLS.items()}

# Coinalyze 現貨 CVD（有資料的幣）
CONA_SPOT: Dict[str, str] = {
    "BTC/USDT":   "BTCUSDT.A",
    "ETH/USDT":   "ETHUSDT.A",
    "SOL/USDT":   "SOLUSDT.A",
    "XRP/USDT":   "XRPUSDT.A",
    "BNB/USDT":   "BNBUSDT.A",
    "DOGE/USDT":  "DOGEUSDT.A",
    "ADA/USDT":   "ADAUSDT.A",
    "TRX/USDT":   "TRXUSDT.A",
    "SUI/USDT":   "SUIUSDT.A",
    "LINK/USDT":  "LINKUSDT.A",
    "AVAX/USDT":  "AVAXUSDT.A",
    "TON/USDT":   "TONUSDT.A",
    "HBAR/USDT":  "HBARUSDT.A",
    "XLM/USDT":   "XLMUSDT.A",
    "BCH/USDT":   "BCHUSDT.A",
    "LTC/USDT":   "LTCUSDT.A",
    "DOT/USDT":   "DOTUSDT.A",
    "UNI/USDT":   "UNIUSDT.A",
    "NEAR/USDT":  "NEARUSDT.A",
    "APT/USDT":   "APTUSDT.A",
    "ARB/USDT":   "ARBUSDT.A",
    "ATOM/USDT":  "ATOMUSDT.A",
    "ETC/USDT":   "ETCUSDT.A",
    "ICP/USDT":   "ICPUSDT.A",
    "AAVE/USDT":  "AAVEUSDT.A",
    "FIL/USDT":   "FILUSDT.A",
    "ENA/USDT":   "ENAUSDT.A",
    "ALGO/USDT":  "ALGOUSDT.A",
    "WLD/USDT":   "WLDUSDT.A",
    "ONDO/USDT":  "ONDOUSDT.A",
}

# Coinalyze 合約 CVD + OI（有資料的幣）
CONA_PERP: Dict[str, str] = {
    "BTC/USDT":   "BTCUSDT_PERP.A",
    "ETH/USDT":   "ETHUSDT_PERP.A",
    "SOL/USDT":   "SOLUSDT_PERP.A",
    "XRP/USDT":   "XRPUSDT_PERP.A",
    "BNB/USDT":   "BNBUSDT_PERP.A",
    "DOGE/USDT":  "DOGEUSDT_PERP.A",
    "ADA/USDT":   "ADAUSDT_PERP.A",
    "TRX/USDT":   "TRXUSDT_PERP.A",
    "SUI/USDT":   "SUIUSDT_PERP.A",
    "LINK/USDT":  "LINKUSDT_PERP.A",
    "AVAX/USDT":  "AVAXUSDT_PERP.A",
    "HBAR/USDT":  "HBARUSDT_PERP.A",
    "XLM/USDT":   "XLMUSDT_PERP.A",
    "BCH/USDT":   "BCHUSDT_PERP.A",
    "LTC/USDT":   "LTCUSDT_PERP.A",
    "DOT/USDT":   "DOTUSDT_PERP.A",
    "UNI/USDT":   "UNIUSDT_PERP.A",
    "NEAR/USDT":  "NEARUSDT_PERP.A",
    "APT/USDT":   "APTUSDT_PERP.A",
    "ARB/USDT":   "ARBUSDT_PERP.A",
    "ATOM/USDT":  "ATOMUSDT_PERP.A",
    "ETC/USDT":   "ETCUSDT_PERP.A",
    "ICP/USDT":   "ICPUSDT_PERP.A",
    "AAVE/USDT":  "AAVEUSDT_PERP.A",
    "FIL/USDT":   "FILUSDT_PERP.A",
    "ENA/USDT":   "ENAUSDT_PERP.A",
    "ALGO/USDT":  "ALGOUSDT_PERP.A",
    "WLD/USDT":   "WLDUSDT_PERP.A",
    "ONDO/USDT":  "ONDOUSDT_PERP.A",
}

BAR_TO_CONA = {"5m": "5min", "15m": "15min", "30m": "30min", "1H": "1hour", "4H": "4hour"}
BAR_SECONDS  = {"5m": 300, "15m": 900, "30m": 1800, "1H": 3600, "4H": 14400}
WARMUP       = 700

TIMEFRAMES = ["15m", "30m", "1H", "4H"]

DEFAULT_TF_PLAN = [
    ("15m", 180, ["long", "short"]),
    ("30m", 300, ["long", "short"]),
    ("1H",  600, ["long", "short"]),
    ("4H",  900, ["long", "short"]),
]

# QQE MOD — 固定參數
QQE_RSI       = 6
QQE_SF        = 5
QQE_FACTOR_P  = 3.0
QQE_FACTOR_S  = 1.61
QQE_THRESHOLD = 3

ADX_THR           = 25
MAX_SL            = 0.12
FUNDING_LONG_MAX  = 0.0001
FUNDING_SHORT_MIN = -0.0001
CVD_WINDOW        = 3
BEAR_MIN_BARS     = 20

MAX_CONSEC_LOSS   = 3
PAUSE_HOURS       = 24

BEST_PARAMS: Dict[str, Dict] = {
    # 日內（15m/30m）: TP1=1.725 TP2多=1.8 TP2空=3.2
    "15m_long":  {"tp1_mult": 1.725, "tp2_intraday_mult": 1.8,  "tp2_swing_mult": 1.8,
                  "sl_atr_buffer": 0.01, "structure_lookback": 28, "exit_mode": "fixed"},
    "15m_short": {"tp1_mult": 2.0,   "tp2_intraday_mult": 3.2,  "tp2_swing_mult": 3.2,
                  "sl_atr_buffer": 0.08, "structure_lookback": 20, "exit_mode": "fixed"},
    "30m_long":  {"tp1_mult": 1.725, "tp2_intraday_mult": 1.8,  "tp2_swing_mult": 1.8,
                  "sl_atr_buffer": 0.05, "structure_lookback": 10, "exit_mode": "fixed"},
    "30m_short": {"tp1_mult": 2.0,   "tp2_intraday_mult": 3.2,  "tp2_swing_mult": 3.2,
                  "sl_atr_buffer": 0.01, "structure_lookback": 10, "exit_mode": "trailing"},
    # 波段（1H/4H）: TP1=1.725 TP2多=2.5 TP2空=4.0
    "1H_long":   {"tp1_mult": 1.725, "tp2_intraday_mult": 2.5,  "tp2_swing_mult": 2.5,
                  "sl_atr_buffer": 0.15, "structure_lookback": 10, "exit_mode": "fixed"},
    "1H_short":  {"tp1_mult": 2.0,   "tp2_intraday_mult": 4.0,  "tp2_swing_mult": 4.0,
                  "sl_atr_buffer": 0.08, "structure_lookback": 20, "exit_mode": "fixed"},
    "4H_long":   {"tp1_mult": 1.725, "tp2_intraday_mult": 2.5,  "tp2_swing_mult": 2.5,
                  "sl_atr_buffer": 0.03, "structure_lookback": 10, "exit_mode": "trailing"},
    "4H_short":  {"tp1_mult": 2.0,   "tp2_intraday_mult": 4.0,  "tp2_swing_mult": 4.0,
                  "sl_atr_buffer": 0.05, "structure_lookback": 30, "exit_mode": "fixed"},
    "5m_long":   {"tp1_mult": 1.725, "tp2_intraday_mult": 1.8,  "tp2_swing_mult": 1.8,
                  "sl_atr_buffer": 0.08, "structure_lookback": 20, "exit_mode": "fixed"},
    "5m_short":  {"tp1_mult": 2.0,   "tp2_intraday_mult": 3.2,  "tp2_swing_mult": 3.2,
                  "sl_atr_buffer": 0.08, "structure_lookback": 20, "exit_mode": "fixed"},
}

def get_params(tf: str, side: str, base_dir: str = ".") -> Dict:
    trade_keys = {"tp1_mult", "tp2_intraday_mult", "tp2_swing_mult",
                  "sl_atr_buffer", "structure_lookback", "exit_mode"}
    for fname in [
        os.path.join(base_dir, f"best_params_{tf.lower()}_{side}.json"),
        os.path.join(base_dir, "final_params_all.json"),
    ]:
        if os.path.exists(fname):
            with open(fname, encoding="utf-8") as f:
                raw = json.load(f)
            data = raw.get(f"{tf}_{side}", raw.get("params", raw))
            p = {k: v for k, v in data.items() if k in trade_keys}
            if len(p) >= 4:
                print(f"  [params] {tf} {side} ← {os.path.basename(fname)}")
                return {**BEST_PARAMS.get(f"{tf}_{side}", {}), **p}
    key = f"{tf}_{side}"
    if key in BEST_PARAMS:
        print(f"  [params] {tf} {side} ← built-in defaults")
        return BEST_PARAMS[key].copy()
    return {"tp1_mult": 1.7, "tp2_intraday_mult": 1.8, "tp2_swing_mult": 2.5,
            "sl_atr_buffer": 0.08, "structure_lookback": 20, "exit_mode": "fixed"}


# ══════════════════════════════════════════════════════════════════════════════
#  STATE
# ══════════════════════════════════════════════════════════════════════════════

pending_orders: Dict[str, Dict] = {}
_tg_offset = 0
_BOT_START_TS = time.time()


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


def tg_with_buttons(text: str, cb_key: str):
    if not TG_BOT_TOKEN or not TG_CHAT_ID:
        return
    kb = {"inline_keyboard": [[
        {"text": "✅ 確認下單", "callback_data": f"confirm_{cb_key}"},
        {"text": "❌ 跳過",     "callback_data": f"skip_{cb_key}"},
    ]]}
    try:
        requests.post(
            f"https://api.telegram.org/bot{TG_BOT_TOKEN}/sendMessage",
            json={"chat_id": TG_CHAT_ID, "text": text,
                  "parse_mode": "HTML", "reply_markup": kb,
                  "disable_web_page_preview": True},
            timeout=10,
        )
    except Exception as e:
        print(f"  [TG] {e}")


def answer_callback(cq_id: str, text: str = ""):
    try:
        requests.post(
            f"https://api.telegram.org/bot{TG_BOT_TOKEN}/answerCallbackQuery",
            json={"callback_query_id": cq_id, "text": text}, timeout=10)
    except Exception:
        pass


def tg_signal(sig: Dict, symbol: str, tf: str, cvd_active: bool):
    side_emoji = "🟢" if sig["side"] == "long" else "🔴"
    dir_s      = "做多" if sig["side"] == "long" else "做空"
    swing_tag  = "📐 波段" if sig["is_swing"] else "⚡ 日內"
    cvd_tag    = "CVD✅" if cvd_active else "CVD⚠️"
    # 台灣時間（UTC+8）顯示
    ts_utc  = datetime.fromisoformat(sig["time"].replace("Z", "+00:00"))
    ts_tw   = ts_utc + timedelta(hours=8)
    ts_str  = ts_tw.strftime("%m/%d %H:%M")
    coin    = symbol.split("/")[0]
    text = (
        f"{side_emoji} <b>{coin} [{tf} {dir_s}]</b>  {swing_tag}  {cvd_tag}\n"
        f"時間：{ts_str} (TST)\n"
        f"入場：<code>{sig['entry']}</code>\n"
        f"━━━━━━━━━━━━\n"
        f"🛑 止損：<code>{sig['sl']}</code>  (風險 {sig['risk_pct']:.2f}%)\n"
        f"🎯 TP1（50%）：<code>{sig['tp1']}</code>  ← 1:{sig['rr1']:.2f}\n"
        f"🎯 TP2（50%）：<code>{sig['tp2']}</code>  ← 1:{sig['rr2']:.2f}\n"
        f"📌 TP1後止損移至：<code>{sig['entry']}</code>\n"
        f"━━━━━━━━━━━━\n"
        f"ATR：{sig['atr']}  出場：{sig['exit_mode']}"
    )
    cb_key = f"{coin}_{tf}_{sig['side']}_{int(time.time())}"
    pending_orders[cb_key] = {
        "symbol":    OKX_SWAP.get(symbol, symbol),
        "direction": sig["side"],
        "entry":     sig["entry"],
        "sl":        sig["sl"],
        "tp1":       sig["tp1"],
        "tp2":       sig["tp2"],
    }
    tg_with_buttons(text, cb_key)


def tg_exit(symbol: str, tf: str, side: str, msg: str):
    emoji = "✅" if "TP" in msg else "🛑" if "SL" in msg else "⏸"
    coin  = symbol.split("/")[0]
    tg(f"{emoji} <b>{coin} [{tf} {side.upper()}] 出場</b>\n{msg}")


def tg_pause(symbol: str, tf: str, side: str, resume: datetime):
    coin = symbol.split("/")[0]
    tg(f"⏸ <b>{coin} [{tf} {side.upper()}] 連虧{MAX_CONSEC_LOSS}單暫停</b>\n"
       f"恢復：{(resume + timedelta(hours=8)).strftime('%m/%d %H:%M')} (TST)")


# ══════════════════════════════════════════════════════════════════════════════
#  OKX REST HELPERS（公開行情）
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

def _okx_pub(path: str, params: dict) -> list:
    try:
        d = requests.get(f"{OKX_BASE}{path}", params=params, timeout=15).json()
        return d.get("data", []) if d.get("code") == "0" else []
    except:
        return []

def fetch_ohlcv(inst_id: str, bar: str, limit: int = WARMUP) -> pd.DataFrame:
    rows = sorted(
        _okx_pub("/api/v5/market/candles",
                 {"instId": inst_id, "bar": bar, "limit": min(limit, 300)}),
        key=lambda x: int(x[0])
    )
    if not rows:
        return pd.DataFrame()
    df = pd.DataFrame(rows, columns=["ts", "open", "high", "low", "close", "vol",
                                     "vC", "vCQ", "confirm"])
    for c in ("open", "high", "low", "close", "vol"):
        df[c] = df[c].astype(float)
    df["ts"] = pd.to_datetime(df["ts"].astype(np.int64), unit="ms", utc=True)
    df.set_index("ts", inplace=True)
    return df.iloc[:-1]  # 去掉未收盤的最後一根

def fetch_funding_now(swap_id: str) -> float:
    rows = _okx_pub("/api/v5/public/funding-rate", {"instId": swap_id})
    if rows:
        return float(rows[0].get("fundingRate", 0))
    return float("nan")


# ══════════════════════════════════════════════════════════════════════════════
#  COINALYZE（CVD + OI）
# ══════════════════════════════════════════════════════════════════════════════

def _cona_get(endpoint: str, params: dict) -> list:
    url = f"{CONA_BASE}/{endpoint}"
    headers = {"api-key": COINALYZE_API_KEY} if COINALYZE_API_KEY else {}
    try:
        d = requests.get(url, params=params, headers=headers, timeout=20).json()
        return d if isinstance(d, list) else d.get("result", d.get("data", []))
    except:
        return []

def fetch_cvd(cona_sym: str, cona_iv: str, from_ms: int, to_ms: int) -> pd.Series:
    if not COINALYZE_API_KEY:
        return pd.Series(dtype=float)
    data = _cona_get("history", {"symbols": cona_sym, "interval": cona_iv,
                                 "from": from_ms // 1000, "to": to_ms // 1000,
                                 "convert_to_usd": "false"})
    rows = []
    for item in data:
        if not isinstance(item, dict):
            continue
        ts = item.get("t", item.get("time", 0))
        v  = float(item.get("v", 0) or 0)
        bv = float(item.get("bv", item.get("buy_volume", v / 2)) or v / 2)
        rows.append((int(ts) * 1000, 2 * bv - v))
    if not rows:
        return pd.Series(dtype=float)
    df_c = pd.DataFrame(rows, columns=["ts", "delta"])
    df_c["ts"] = pd.to_datetime(df_c["ts"].astype(np.int64), unit="ms", utc=True)
    df_c = df_c.set_index("ts").sort_index()
    return df_c["delta"].cumsum()

def fetch_oi(cona_sym: str, cona_iv: str, from_ms: int, to_ms: int) -> pd.Series:
    if not COINALYZE_API_KEY:
        return pd.Series(dtype=float)
    data = _cona_get("open-interest-history",
                     {"symbols": cona_sym, "interval": cona_iv,
                      "from": from_ms // 1000, "to": to_ms // 1000})
    rows = []
    for item in data:
        if not isinstance(item, dict):
            continue
        ts = item.get("t", item.get("time", 0))
        v  = float(item.get("v", item.get("oi", 0)) or 0)
        rows.append((int(ts) * 1000, v))
    if not rows:
        return pd.Series(dtype=float)
    df_o = pd.DataFrame(rows, columns=["ts", "oi"])
    df_o["ts"] = pd.to_datetime(df_o["ts"].astype(np.int64), unit="ms", utc=True)
    return df_o.set_index("ts").sort_index()["oi"]


# ══════════════════════════════════════════════════════════════════════════════
#  OKX 下單（ccxt，TG按鈕確認後執行）
# ══════════════════════════════════════════════════════════════════════════════

def _make_okx_ex():
    ex = ccxt.okx({
        "apiKey":   OKX_API_KEY,
        "secret":   OKX_SECRET,
        "password": OKX_PASSPHRASE,
        "options":  {"defaultType": "swap"},
    })
    if OKX_DEMO:
        ex.set_sandbox_mode(True)
    return ex


def _fetch_okx_balance():
    """抓取 OKX 帳戶餘額，回傳 (可用, 總額) 或 (None, None)"""
    if not OKX_API_KEY:
        return None, None
    try:
        ex = _make_okx_ex()
        bal   = ex.fetch_balance()
        avail = bal["USDT"]["free"]  if "USDT" in bal else 0.0
        total = bal["USDT"]["total"] if "USDT" in bal else 0.0
        return float(avail), float(total)
    except Exception:
        return None, None


def place_okx_order(symbol: str, direction: str, entry: float,
                    sl: float, tp1: float, tp2: float):
    global _LIVE_MODE, MAX_LEVERAGE
    if not _LIVE_MODE:
        tg("📝 Paper 模式：收到下單請求，未實際下單\n"
           "請先發送 /setlive 切換為實盤模式"); return
    try:
        ex = _make_okx_ex()
        ex.load_markets()

        # ── 1. 帳戶可用餘額 ──
        bal   = ex.fetch_balance()
        avail = float(bal["USDT"]["free"])  if "USDT" in bal else 0.0
        if avail <= 0:
            tg("⚠️ USDT 可用餘額不足"); return

        # ── 2. 每倉保證金 = 可用 × MARGIN_PCT% ──
        margin = avail * MARGIN_PCT / 100

        # ── 3. 止損距離% & 建議槓桿 ──
        price       = ex.fetch_ticker(symbol)["last"]
        sl_dist_pct = abs(price - sl) / price * 100
        if sl_dist_pct <= 0:
            tg("⚠️ 止損距離為 0，無法計算槓桿"); return
        sug_lev = max(1, min(int(100 / sl_dist_pct), MAX_LEVERAGE))

        # ── 4. 倉位價值 & 張數 ──
        mkt    = ex.market(symbol)
        prec   = int(mkt.get("precision", {}).get("amount", 0) or 0)
        ct_sz  = float(mkt.get("contractSize", 1) or 1)
        pos_val = margin * sug_lev
        raw     = pos_val / (price * ct_sz)
        amt  = max(1, int(raw))          if prec == 0 else max(round(1/ct_sz, prec), round(raw, prec))
        half = max(1, int(amt // 2))     if prec == 0 else round(amt / 2, prec)

        # ── 5. 設定槓桿 ──
        ex.set_leverage(sug_lev, symbol)

        # ── 6. TG 倉位摘要 ──
        tg(
            f"💰 可用餘額：{avail:.1f} U\n"
            f"📦 每倉保證金：{margin:.1f} U（可用×{MARGIN_PCT}%）\n"
            f"📊 倉位價值：{pos_val:.1f} U\n"
            f"⚡ 建議槓桿：{sug_lev}x\n"
            f"☠️ 最大虧損：{margin:.1f} U（逐倉保證金）"
        )

        # ── 7. 下單 ──
        is_l   = direction == "long"
        es, xs = ("buy", "sell") if is_l else ("sell", "buy")
        eo = ex.create_market_order(symbol, es, amt)
        res = [
            f"✅ 進場\n{symbol} {'做多' if is_l else '做空'} {sug_lev}x\n"
            f"均價:{eo.get('average') or price}  ID:{eo.get('id')}"
        ]
        try:
            o = ex.create_order(symbol, "stop_market", xs, amt, None,
                                {"stopPrice": sl, "reduceOnly": True})
            res.append(f"🛑 SL {sl} ID:{o.get('id')}")
        except Exception as e:
            res.append(f"⚠️ SL失敗:{e}")
        for px, n, lbl in [(tp1, half, "TP1"), (tp2, half, "TP2")]:
            try:
                o = ex.create_limit_order(symbol, xs, n, px, {"reduceOnly": True})
                res.append(f"🎯 {lbl} {px} x{n} ID:{o.get('id')}")
            except Exception as e:
                res.append(f"⚠️ {lbl}失敗:{e}")
        tg("\n".join(res))
    except Exception as e:
        tg(f"⚠️ 下單失敗:{e}")


# ══════════════════════════════════════════════════════════════════════════════
#  TG 按鈕回調輪詢
# ══════════════════════════════════════════════════════════════════════════════

def _handle_tg_command(text: str):
    global _LIVE_MODE, MAX_LEVERAGE, MARGIN_PCT, _BOT_START_TS, _bot_ref
    text = text.strip()

    if text.startswith("/setrisk"):
        parts = text.split()
        if len(parts) >= 2:
            try:
                val = float(parts[1])
                if val <= 0 or val > 100:
                    tg("⚠️ 請輸入 0.1 ~ 100 之間的數字"); return
                MARGIN_PCT = val
                tg(f"✅ 每倉保證金比例已設為 <b>{val}%</b>\n"
                   f"每單保證金 = 可用餘額 × {val}%")
            except ValueError:
                tg("⚠️ 格式錯誤，例：/setrisk 5")
        else:
            tg(f"目前每倉保證金：可用餘額 × <b>{MARGIN_PCT}%</b>\n"
               f"修改請發：/setrisk [數字]（例：/setrisk 5）")

    elif text.startswith("/setmaxlev"):
        parts = text.split()
        if len(parts) >= 2:
            try:
                val = int(float(parts[1]))
                if val < 1 or val > 125:
                    tg("⚠️ 槓桿上限請輸入 1 ~ 125 之間的整數"); return
                MAX_LEVERAGE = val
                tg(f"✅ 最高槓桿上限已設為 <b>{val}x</b>\n"
                   f"建議槓桿 = min(100 ÷ 止損距離%, {val}x)")
            except ValueError:
                tg("⚠️ 格式錯誤，例：/setmaxlev 50")
        else:
            tg(f"目前最高槓桿上限：<b>{MAX_LEVERAGE}x</b>\n修改請發：/setmaxlev [數字]")

    elif text.startswith("/setlive"):
        if not OKX_API_KEY:
            tg("⚠️ 尚未設定 OKX_API_KEY，無法切換實盤"); return
        _LIVE_MODE = True
        tg("🔴 <b>已切換為實盤模式</b>\n點擊 ✅ 確認下單後將直接送出真實委託單")

    elif text.startswith("/setpaper"):
        _LIVE_MODE = False
        tg("📝 <b>已切換為模擬（Paper）模式</b>\n點擊 ✅ 確認下單不會送出真實委託單")

    elif text.startswith("/status"):
        elapsed = int(time.time() - _BOT_START_TS)
        h, m    = elapsed // 3600, (elapsed % 3600) // 60
        mode    = "🔴 實盤" if _LIVE_MODE else "📝 Paper"
        # 帳戶餘額（即時抓取）
        avail, total = _fetch_okx_balance()
        if avail is not None:
            margin_str = f"{avail/10:.1f} U（可用÷10）"
            bal_str    = f"總額：{total:.1f} U  可用：{avail:.1f} U"
        else:
            margin_str = "N/A（需設定 OKX_API_KEY）"
            bal_str    = "N/A"
        # 未平倉數量（paper追蹤）
        open_cnt = 0
        if _bot_ref is not None:
            open_cnt = sum(1 for p in _bot_ref.positions.values() if p and p.open)
        tg(
            f"⚙️ <b>目前狀態</b>\n"
            f"━━━━━━━━━━━━\n"
            f"模式：{mode}\n"
            f"餘額：{bal_str}\n"
            f"每倉保證金：{margin_str}\n"
            f"最高槓桿上限：{MAX_LEVERAGE}x\n"
            f"未平倉（Paper）：{open_cnt} 筆\n"
            f"已運行：{h}h {m}m\n"
            f"監控：{len(SYMBOLS)} 幣 | {len(TIMEFRAMES)} 時框\n"
            f"CVD：{'✅ Coinalyze' if COINALYZE_API_KEY else '⚠️ 無'}"
        )

    elif text.startswith("/help"):
        tg(
            "📖 <b>指令列表</b>\n"
            "━━━━━━━━━━━━\n"
            "/setlive — 切換為實盤下單\n"
            "/setpaper — 切換為模擬（不下單）\n"
            "/setmaxlev [數字] — 設定最高槓桿上限（例：/setmaxlev 50）\n"
            "/status — 顯示模式／餘額／保證金／槓桿上限／未平倉／運行時間\n"
            "/help — 顯示此說明\n"
            "━━━━━━━━━━━━\n"
            "倉位計算邏輯：\n"
            "  每倉保證金 = 可用餘額 ÷ 10\n"
            "  建議槓桿 = min(100 ÷ 止損距離%, 上限)\n"
            "  倉位價值 = 保證金 × 建議槓桿\n"
            "  最大虧損 = 每倉保證金（逐倉模式）"
        )


def poll_tg_callbacks():
    global _tg_offset
    while True:
        try:
            r = requests.get(
                f"https://api.telegram.org/bot{TG_BOT_TOKEN}/getUpdates",
                params={"offset": _tg_offset, "timeout": 30,
                        "allowed_updates": ["callback_query", "message"]},
                timeout=40,
            ).json()
            for upd in r.get("result", []):
                _tg_offset = upd["update_id"] + 1

                # ── 按鈕回調 ──
                cq = upd.get("callback_query")
                if cq:
                    cbd, cq_id = cq.get("data", ""), cq["id"]
                    if cbd.startswith("confirm_"):
                        key   = cbd[8:]
                        order = pending_orders.pop(key, None)
                        if order:
                            answer_callback(cq_id, "下單中...")
                            Thread(
                                target=place_okx_order,
                                args=(order["symbol"], order["direction"],
                                      order.get("entry", 0),
                                      order["sl"], order["tp1"], order["tp2"]),
                                daemon=True,
                            ).start()
                        else:
                            answer_callback(cq_id, "訊號已過期")
                    elif cbd.startswith("skip_"):
                        pending_orders.pop(cbd[5:], None)
                        answer_callback(cq_id, "已跳過")
                        tg("❌ 已跳過")
                    continue

                # ── 文字指令 ──
                msg = upd.get("message", {})
                text = msg.get("text", "")
                if text.startswith("/"):
                    _handle_tg_command(text)

        except Exception as e:
            print(f"[TG輪詢] {e}")
            sleep(5)


# ══════════════════════════════════════════════════════════════════════════════
#  FLASK 健康檢查
# ══════════════════════════════════════════════════════════════════════════════

_app = Flask(__name__)

@_app.route("/")
def _health():
    return f"賽克斯機器人 v4 | {len(SYMBOLS)} 個幣 | running"

def run_web():
    port = int(os.environ.get("PORT", 3000))
    import logging
    log = logging.getLogger("werkzeug")
    log.setLevel(logging.ERROR)
    _app.run(host="0.0.0.0", port=port, debug=False)


# ══════════════════════════════════════════════════════════════════════════════
#  K棒收盤觸發（台灣時間 UTC+8 顯示）
# ══════════════════════════════════════════════════════════════════════════════

def wait_for_next_candle() -> List[str]:
    """等待最近一個 K 棒收盤，回傳剛收盤的時框列表。"""
    now = datetime.now(timezone.utc).timestamp()
    next_closes = {
        tf: (int(now / BAR_SECONDS[tf]) + 1) * BAR_SECONDS[tf] + 5
        for tf in TIMEFRAMES
    }
    earliest = min(next_closes.values())
    # 等待
    while True:
        w = earliest - datetime.now(timezone.utc).timestamp() - 0.5
        if w <= 0:
            break
        sleep(min(w, 30))
    while datetime.now(timezone.utc).timestamp() < earliest:
        sleep(0.05)
    # 回傳在 ±15 秒內收盤的時框
    return [tf for tf, ts in next_closes.items() if abs(ts - earliest) <= 15]


# ══════════════════════════════════════════════════════════════════════════════
#  INDICATORS
# ══════════════════════════════════════════════════════════════════════════════

def precompute_base(df: pd.DataFrame) -> pd.DataFrame:
    df = df.copy()
    for s in (12, 144, 169, 576, 676):
        df[f"e{s}"] = df["close"].ewm(span=s, adjust=False).mean()
    df["sTop"] = df[["e144", "e169"]].max(axis=1)
    df["sBot"] = df[["e144", "e169"]].min(axis=1)
    df["lTop"] = df[["e576", "e676"]].max(axis=1)
    df["lBot"] = df[["e576", "e676"]].min(axis=1)
    hi, lo, cl = df["high"], df["low"], df["close"]
    tr = pd.concat([hi - lo,
                    (hi - cl.shift()).abs(),
                    (lo - cl.shift()).abs()], axis=1).max(axis=1)
    df["atr"]        = tr.ewm(span=14, adjust=False).mean()
    df["channel_ok"] = (df["e144"] - df["e576"]).abs() >= df["atr"] * 2
    return df


def _ws(a: np.ndarray, p: int) -> np.ndarray:
    r = np.zeros(len(a))
    r[p] = a[1:p + 1].sum()
    for i in range(p + 1, len(a)):
        r[i] = r[i - 1] - r[i - 1] / p + a[i]
    return r


def adx_series(df: pd.DataFrame, period: int = 14) -> pd.Series:
    hi, lo, cl = df["high"].values, df["low"].values, df["close"].values
    n = len(hi)
    tr_a, pdm, mdm = np.zeros(n), np.zeros(n), np.zeros(n)
    for i in range(1, n):
        tr_a[i] = max(hi[i] - lo[i], abs(hi[i] - cl[i - 1]), abs(lo[i] - cl[i - 1]))
        u  = hi[i] - hi[i - 1]
        dn = lo[i - 1] - lo[i]
        pdm[i] = u  if u  > dn and u  > 0 else 0
        mdm[i] = dn if dn > u  and dn > 0 else 0
    atr_ = _ws(tr_a, period)
    pdi  = _ws(pdm,  period)
    mdi  = _ws(mdm,  period)
    with np.errstate(divide="ignore", invalid="ignore"):
        p_ = np.where(atr_ > 0, 100 * pdi / atr_, 0)
        m_ = np.where(atr_ > 0, 100 * mdi / atr_, 0)
        dx = np.where(p_ + m_ > 0, 100 * np.abs(p_ - m_) / (p_ + m_), 0)
    return pd.Series(_ws(dx, period), index=df.index)


def add_swing(df: pd.DataFrame) -> pd.DataFrame:
    df = df.copy()
    df["adx"] = adx_series(df, 14)
    agg  = {"open": "first", "high": "max", "low": "min", "close": "last"}
    df4  = df[["open", "high", "low", "close"]].resample("4h").agg(agg).dropna()
    df4["e144_4h"] = df4["close"].ewm(span=144, adjust=False).mean()
    df4["e576_4h"] = df4["close"].ewm(span=576, adjust=False).mean()
    df4["trend4h"] = (df4["e144_4h"] > df4["e576_4h"]).shift(1)
    df4["slope4h"] = (df4["e576_4h"] > df4["e576_4h"].shift(20)).shift(1)
    df["trend4h"]  = df4["trend4h"].reindex(df.index, method="ffill").fillna(False)
    df["slope4h"]  = df4["slope4h"].reindex(df.index, method="ffill").fillna(False)
    df["is_swing"] = df["trend4h"] & df["slope4h"] & (df["adx"] > ADX_THR)
    return df


def calc_qqe(close: pd.Series, rsi_period: int, sf: int,
             factor: float, threshold: float = 3.0):
    delta = close.diff()
    g    = delta.clip(lower=0).ewm(com=rsi_period - 1, adjust=False).mean()
    l    = (-delta.clip(upper=0)).ewm(com=rsi_period - 1, adjust=False).mean()
    rsiMa  = (100 - (100 / (1 + g / l))).ewm(span=sf, adjust=False).mean()
    atrRsi = rsiMa.diff().abs()
    dar    = (atrRsi.ewm(span=rsi_period, adjust=False).mean()
                    .ewm(span=rsi_period, adjust=False).mean() * factor)
    rm, d  = rsiMa.values, dar.values
    n      = len(rm)
    lb = np.zeros(n)
    sb = np.full(n, np.inf)
    tr = np.zeros(n, dtype=np.int8)
    for i in range(1, n):
        if np.isnan(rm[i]) or np.isnan(d[i]) or d[i] <= 0:
            lb[i] = lb[i - 1]; sb[i] = sb[i - 1]; tr[i] = tr[i - 1]; continue
        nlb = rm[i] - d[i]; nsb = rm[i] + d[i]
        lb[i] = (max(lb[i - 1], nlb) if rm[i - 1] > lb[i - 1] and rm[i] > lb[i - 1] else nlb)
        sb[i] = (min(sb[i - 1], nsb) if rm[i - 1] < sb[i - 1] and rm[i] < sb[i - 1] else nsb)
        if   rm[i] > sb[i - 1]: tr[i] = 1
        elif rm[i] < lb[i - 1]: tr[i] = -1
        else: tr[i] = tr[i - 1] if tr[i - 1] != 0 else 1
    prev = np.concatenate([[0], tr[:-1]])
    bull = pd.Series((tr == 1) & (prev != 1) & (rm > 50 + threshold), index=close.index)
    bear = pd.Series((tr == -1) & (prev != -1) & (rm < 50 - threshold), index=close.index)
    bull.iloc[0] = bear.iloc[0] = False
    return bull, bear, tr


def add_dual_qqe(df: pd.DataFrame) -> pd.DataFrame:
    bull1, bear1, tr1 = calc_qqe(df["close"], QQE_RSI, QQE_SF, QQE_FACTOR_P, QQE_THRESHOLD)
    _,     _,     tr2 = calc_qqe(df["close"], QQE_RSI, QQE_SF, QQE_FACTOR_S, QQE_THRESHOLD)
    df = df.copy()
    df["qbull"] = bull1 & (tr2 == 1)
    df["qbear"] = bear1 & (tr2 == -1)
    df["qtr"]   = np.where((tr1 == 1) & (tr2 == 1),   1,
                  np.where((tr1 == -1) & (tr2 == -1), -1, 0)).astype(np.int8)
    return df


def is_bear_market(df_1h: pd.DataFrame, min_bars: int = BEAR_MIN_BARS) -> bool:
    e144 = df_1h["e144"].values
    e576 = df_1h["e576"].values
    count = 0
    for i in range(len(e144) - 1, -1, -1):
        if e144[i] < e576[i]: count += 1
        else: break
    return count >= min_bars


# ══════════════════════════════════════════════════════════════════════════════
#  CVD REVERSAL FILTER
# ══════════════════════════════════════════════════════════════════════════════

def _cvd_turning_up(arr: np.ndarray, i: int, w: int = CVD_WINDOW) -> bool:
    if i < w - 1: return False
    recent = arr[i - w + 1:i + 1]
    if len(recent) < w or np.any(np.isnan(recent)): return False
    return bool(recent[-1] > recent[-2] and recent[-2] <= recent[0])

def _cvd_turning_down(arr: np.ndarray, i: int, w: int = CVD_WINDOW) -> bool:
    if i < w - 1: return False
    recent = arr[i - w + 1:i + 1]
    if len(recent) < w or np.any(np.isnan(recent)): return False
    return bool(recent[-1] < recent[-2] and recent[-2] >= recent[0])

def check_cvd(df: pd.DataFrame, side: str) -> Tuple[Optional[bool], bool]:
    if "cvd_spot" not in df.columns:
        return None, False
    n = len(df); i = n - 1
    cvds = df["cvd_spot"].values
    cvdp = df["cvd_perp"].values if "cvd_perp" in df.columns else np.full(n, np.nan)
    oi   = df["oi"].values       if "oi"       in df.columns else np.full(n, np.nan)
    if np.isnan(cvds[i]) or np.isnan(cvdp[i]) or np.isnan(oi[i]) or i < 1:
        return None, False
    oi_up = oi[i] > oi[i - 1]
    if side == "long":
        ok = _cvd_turning_up(cvds, i) and _cvd_turning_up(cvdp, i) and oi_up
    else:
        ok = _cvd_turning_down(cvds, i) and _cvd_turning_down(cvdp, i) and oi_up
    return ok, True


# ══════════════════════════════════════════════════════════════════════════════
#  PIVOT HELPERS
# ══════════════════════════════════════════════════════════════════════════════

def _piv_low(lo: np.ndarray, idx: int, lb: int, n: int = 2) -> float:
    w   = lo[max(0, idx - lb - 1):idx - 1]
    pts = [w[j] for j in range(n, len(w) - n)
           if all(w[j] < w[j - k] for k in range(1, n + 1))
           and all(w[j] < w[j + k] for k in range(1, n + 1))]
    return pts[-1] if pts else (float(w.min()) if len(w) > 0 else float("nan"))

def _piv_high(hi: np.ndarray, idx: int, lb: int, n: int = 2) -> float:
    w   = hi[max(0, idx - lb - 1):idx - 1]
    pts = [w[j] for j in range(n, len(w) - n)
           if all(w[j] > w[j - k] for k in range(1, n + 1))
           and all(w[j] > w[j + k] for k in range(1, n + 1))]
    return pts[-1] if pts else (float(w.max()) if len(w) > 0 else float("nan"))


# ══════════════════════════════════════════════════════════════════════════════
#  SIGNAL DETECTION
# ══════════════════════════════════════════════════════════════════════════════

def check_signal(df: pd.DataFrame, params: Dict, side: str,
                 funding_now: float = float("nan"),
                 bear_market: bool = False) -> Optional[Dict]:
    i = len(df) - 1
    if i < 10: return None

    cl    = df["close"].values;  hi   = df["high"].values;   lo  = df["low"].values
    e12   = df["e12"].values;    e144 = df["e144"].values;   e576 = df["e576"].values
    sBot  = df["sBot"].values;   sTop = df["sTop"].values
    atr_  = df["atr"].values
    qbull = df["qbull"].values;  qbear = df["qbear"].values
    swing = df["is_swing"].values if "is_swing" in df.columns else np.zeros(len(df), bool)
    ch_ok = df["channel_ok"].values if "channel_ok" in df.columns else np.ones(len(df), bool)

    lb  = int(params["structure_lookback"])
    buf = float(params["sl_atr_buffer"])

    if not ch_ok[i]: return None

    if not math.isnan(funding_now):
        if side == "long"  and funding_now >= FUNDING_LONG_MAX:  return None
        if side == "short" and funding_now <= FUNDING_SHORT_MIN: return None

    if side == "short" and not bear_market:
        return None

    if side == "long":
        if not qbull[i]:               return None
        if not (e144[i] > e576[i]):    return None
        if not (cl[i] > sBot[i]):      return None
        if not (cl[i] > e12[i]):       return None
        base = _piv_low(lo, i, lb)
        if math.isnan(base):           return None
        sl   = base - atr_[i] * buf
        risk = cl[i] - sl
    else:
        if not qbear[i]:               return None
        if not (e144[i] < e576[i]):    return None
        if not (cl[i] < sTop[i]):      return None
        if not (cl[i] < e12[i]):       return None
        base = _piv_high(hi, i, lb)
        if math.isnan(base):           return None
        sl   = base + atr_[i] * buf
        risk = sl - cl[i]

    cvd_result, cvd_active = check_cvd(df, side)
    if cvd_result is False:
        return None

    entry = cl[i]
    if risk <= 0 or (risk / entry) > MAX_SL: return None

    is_swing = bool(swing[i])
    tp2_mult = float(params["tp2_swing_mult"] if is_swing else params["tp2_intraday_mult"])
    tp1_mult = float(params["tp1_mult"])
    exit_mode = str(params["exit_mode"])
    sign     = 1 if side == "long" else -1

    return {
        "side":       side,
        "time":       df.index[i].isoformat(),
        "bar_ts":     df.index[i].isoformat(),
        "entry":      round(entry, 6),
        "sl":         round(sl, 6),
        "tp1":        round(entry + sign * risk * tp1_mult, 6),
        "tp2":        round(entry + sign * risk * tp2_mult, 6),
        "risk_pct":   round(risk / entry * 100, 3),
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
        bar  = df.iloc[-1]
        h, l, c = bar["high"], bar["low"], bar["close"]
        e12  = bar["e12"]; e144 = bar["e144"]; e576 = bar["e576"]
        qtr  = int(df["qtr"].iloc[-1])

        if not self.tp1_hit:
            if (self.side == "long"  and l <= self.sl) or \
               (self.side == "short" and h >= self.sl):
                self.open = False; return f"SL hit @ {self.sl:.4f}"
            if (self.side == "long"  and h >= self.tp1) or \
               (self.side == "short" and l <= self.tp1):
                self.tp1_hit = True; self.sl = self.entry
                ts = df.index[-1].strftime("%H:%M")
                print(f"    [{ts}] [{self.symbol} {self.tf} {self.side}] TP1 → SL=entry")
        else:
            if (self.side == "long"  and l <= self.entry) or \
               (self.side == "short" and h >= self.entry):
                self.open = False; return f"Breakeven stop @ {self.entry:.4f}"
            if self.exit_mode == "fixed":
                if (self.side == "long"  and h >= self.tp2) or \
                   (self.side == "short" and l <= self.tp2):
                    self.open = False; return f"TP2 hit @ {self.tp2:.4f}"
            else:
                rev_q   = (qtr == -1) if self.side == "long" else (qtr == 1)
                rev_e12 = (c < e12)   if self.side == "long" else (c > e12)
                rev_tr  = (e144 < e576) if self.side == "long" else (e144 > e576)
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
        self.tf_plan  = tf_plan
        self.sides    = sides
        self.live     = live
        self.base_dir = base_dir

        self.params: Dict[str, Dict] = {}
        for tf, _, tf_sides in tf_plan:
            for s in tf_sides:
                if s in sides:
                    self.params[f"{tf}_{s}"] = get_params(tf, s, base_dir)

        self.positions:        Dict[str, Optional[PaperPosition]] = {}
        self.consec_loss:      Dict[str, int]                     = {}
        self.pause_until:      Dict[str, Optional[datetime]]      = {}
        self.last_signal_time: Dict[str, float]                   = {}
        self._bear_cache:      Dict[str, bool]                    = {}

        for tf, _, tf_sides in tf_plan:
            for inst_id in SYMBOLS:
                for s in tf_sides:
                    if s in sides:
                        key = f"{inst_id}_{tf}_{s}"
                        self.positions[key]        = None
                        self.consec_loss[key]      = 0
                        self.pause_until[key]      = None
                        self.last_signal_time[key] = 0.0

    def _key(self, inst_id, tf, side): return f"{inst_id}_{tf}_{side}"

    def _is_paused(self, key):
        p = self.pause_until.get(key)
        return p is not None and datetime.now(timezone.utc) < p

    def _record_loss(self, key, tf):
        self.consec_loss[key] += 1
        if self.consec_loss[key] >= MAX_CONSEC_LOSS:
            resume = datetime.now(timezone.utc) + timedelta(hours=PAUSE_HOURS)
            self.pause_until[key]   = resume
            self.consec_loss[key]   = 0
            parts = key.split("_")
            sym   = parts[0] + "-USDT-SWAP"
            label = SYMBOLS.get(sym, sym)
            tg_pause(label, tf, parts[-1], resume)

    def _record_win(self, key):
        self.consec_loss[key] = 0

    def _update_bear_cache(self):
        for inst_id, label in SYMBOLS.items():
            df1h = fetch_ohlcv(inst_id, "1H", 50)
            if df1h.empty or len(df1h) < BEAR_MIN_BARS + 10:
                self._bear_cache[inst_id] = False; continue
            df1h = precompute_base(df1h)
            self._bear_cache[inst_id] = is_bear_market(df1h)
            mode = "🐻 BEAR" if self._bear_cache[inst_id] else "🐂 BULL"
            coin = label.split("/")[0]
            print(f"  {coin}: {mode}")
            sleep(0.2)

    def _print_signal(self, sig, symbol, tf):
        cvd_tag  = "CVD✓" if sig.get("cvd_active") else "CVD—"
        mode_tag = "📐波段" if sig["is_swing"] else "⚡日內"
        w = "─" * 60
        coin = symbol.split("/")[0]
        print(f"\n{w}")
        print(f"  {'🟢 LONG' if sig['side']=='long' else '🔴 SHORT'}  {coin}  [{tf}]  {mode_tag}  {cvd_tag}")
        print(f"  Entry : {sig['entry']}")
        print(f"  SL    : {sig['sl']}  (risk {sig['risk_pct']:.2f}%)")
        print(f"  TP1   : {sig['tp1']}  (R={sig['rr1']:.2f})")
        print(f"  TP2   : {sig['tp2']}  (R={sig['rr2']:.2f})")
        print(f"{w}\n")

    def _scan(self, inst_id, label, tf, side, df, funding_now):
        key = self._key(inst_id, tf, side)
        p   = self.params.get(f"{tf}_{side}", {})

        if self._is_paused(key):
            return

        pos = self.positions[key]

        # 更新持倉狀態
        if pos and pos.open:
            msg = pos.update(df)
            if msg:
                self._print_exit(label, tf, side, msg)
                tg_exit(label, tf, side, msg)
                if "SL" in msg or "Breakeven" in msg:
                    self._record_loss(key, tf)
                else:
                    self._record_win(key)
                self.positions[key] = None
                pos = None

        if pos and pos.open:
            return

        bear = self._bear_cache.get(inst_id, False)
        sig  = check_signal(df, p, side, funding_now=funding_now, bear_market=bear)
        if not sig:
            return

        # 防重複：同幣同時框同方向 30 分鐘內不重複發訊號
        now_ts = time.time()
        if now_ts - self.last_signal_time.get(key, 0) < SIGNAL_COOLDOWN:
            return
        self.last_signal_time[key] = now_ts

        self._print_signal(sig, label, tf)
        tg_signal(sig, label, tf, sig.get("cvd_active", False))
        self.positions[key] = PaperPosition(sig, label, tf)
        self._record_win(key)

    def _print_exit(self, symbol, tf, side, msg):
        ts   = (datetime.now(timezone.utc) + timedelta(hours=8)).strftime("%H:%M:%S")
        coin = symbol.split("/")[0]
        print(f"  [{ts} TST] EXIT {coin} [{tf} {side}]: {msg}")

    def _scan_symbol_tf(self, inst_id, label, tf, tf_sides, all_sides):
        df_raw = fetch_ohlcv(inst_id, tf, WARMUP)
        if df_raw.empty or len(df_raw) < 200:
            return
        df = precompute_base(df_raw)
        df = add_swing(df)
        df = add_dual_qqe(df)

        # 抓 CVD + OI（沒資料的幣自動跳過）
        if COINALYZE_API_KEY:
            cona_iv       = BAR_TO_CONA.get(tf, "1hour")
            now_ms        = int(df.index[-1].timestamp() * 1000)
            look_ms       = BAR_SECONDS.get(tf, 3600) * CVD_WINDOW * 3 * 1000
            from_ms       = now_ms - look_ms
            cona_spot_sym = CONA_SPOT.get(label)
            cona_perp_sym = CONA_PERP.get(label)
            if cona_spot_sym:
                cvd_spot = fetch_cvd(cona_spot_sym, cona_iv, from_ms, now_ms)
                if not cvd_spot.empty:
                    df["cvd_spot"] = cvd_spot.reindex(df.index, method="ffill")
            if cona_perp_sym:
                cvd_perp = fetch_cvd(cona_perp_sym, cona_iv, from_ms, now_ms)
                oi_s     = fetch_oi(cona_perp_sym, cona_iv, from_ms, now_ms)
                if not cvd_perp.empty:
                    df["cvd_perp"] = cvd_perp.reindex(df.index, method="ffill")
                if not oi_s.empty:
                    df["oi"] = oi_s.reindex(df.index, method="ffill")

        funding_now = fetch_funding_now(OKX_SWAP.get(label, ""))

        for side in tf_sides:
            if side in all_sides:
                self._scan(inst_id, label, tf, side, df, funding_now)

    def scan_once(self, due_tfs: Optional[List[str]] = None):
        tfs_to_scan = due_tfs or TIMEFRAMES
        if any(s == "short" for s in self.sides):
            self._update_bear_cache()
        for tf, _, tf_sides in self.tf_plan:
            if tf not in tfs_to_scan:
                continue
            print(f"\n── 掃描 {tf} ──")
            for inst_id, label in SYMBOLS.items():
                self._scan_symbol_tf(inst_id, label, tf, tf_sides, self.sides)
                sleep(0.4)
        print("── 完畢 ──\n")

    def print_banner(self):
        print("\n" + "=" * 64)
        print(f"  賽克斯機器人 v4  |  TFs: {TIMEFRAMES}")
        print(f"  幣種: {len(SYMBOLS)} 個  |  Mode: {'🔴 LIVE' if self.live else '📝 Paper'}  |  MaxLev: {MAX_LEVERAGE}x")
        print(f"  QQE Primary  RSI={QQE_RSI} SF={QQE_SF} F={QQE_FACTOR_P} Thr={QQE_THRESHOLD}")
        print(f"  QQE Secondary RSI={QQE_RSI} SF={QQE_SF} F={QQE_FACTOR_S}")
        print(f"  CVD: {'✅ Coinalyze' if COINALYZE_API_KEY else '⚠️ 無'}")
        print(f"  TG : {'✅' if TG_BOT_TOKEN else '⚠️ off'}  按鈕確認下單")
        print(f"  熊市閘門: EMA144<EMA576 連續{BEAR_MIN_BARS}+根 1H = 允許做空")
        print("=" * 64)
        for tf, _, tf_sides in self.tf_plan:
            for s in tf_sides:
                if s in self.sides:
                    p = self.params.get(f"{tf}_{s}", {})
                    print(f"\n  [{tf} {s}]")
                    for k, v in p.items():
                        print(f"    {k:<28} = {v}")
        print()

    def run(self):
        global _bot_ref
        _bot_ref = self
        self.print_banner()
        print(f"  觸發方式：K棒收盤觸發  |  時區顯示：台灣時間 (UTC+8)")
        print("  Press Ctrl+C to stop\n")

        tg(
            f"✅ 賽克斯機器人 v4 已啟動\n"
            f"━━━━━━━━━━━━━━━━━\n"
            f"監控：{len(SYMBOLS)} 個幣\n"
            f"時框：{' / '.join(TIMEFRAMES)}\n"
            f"CVD：{'✅ Coinalyze' if COINALYZE_API_KEY else '⚠️ 無'}\n"
            f"模式：{'🔴 實盤' if _LIVE_MODE else '📝 Paper（發 /setlive 切換實盤）'}\n"
            f"最高槓桿上限：{MAX_LEVERAGE}x（自動依止損距離計算）\n"
            f"倉位：可用餘額÷10 × 建議槓桿\n"
            f"📖 發送 /help 查看所有指令"
        )

        while True:
            try:
                due = wait_for_next_candle()
                now_tw = (datetime.now(timezone.utc) + timedelta(hours=8)).strftime("%Y-%m-%d %H:%M:%S")
                print(f"[{now_tw} TST] 觸發：{', '.join(due)}")
                self.scan_once(due_tfs=due)
            except KeyboardInterrupt:
                print("\nStopped.")
                tg("🛑 賽克斯機器人已停止。")
                break
            except Exception as e:
                print(f"\n  [ERR] {e}")
                tg(f"⚠️ {e}")
                sleep(60)


# ══════════════════════════════════════════════════════════════════════════════
#  CLI
# ══════════════════════════════════════════════════════════════════════════════

def main():
    ap = argparse.ArgumentParser(description="賽克斯訊號機器人 v4")
    ap.add_argument("--tf",   default="all",
                    help="Timeframe: 15m/30m/1H/4H/all (default: all)")
    ap.add_argument("--side", default="both",
                    choices=["long", "short", "both"])
    ap.add_argument("--live", action="store_true",
                    help="Enable real OKX order execution")
    ap.add_argument("--once", action="store_true",
                    help="Single scan then exit")
    ap.add_argument("--dir",  default=".",
                    help="Directory with param JSON files")
    args = ap.parse_args()

    sides = ["long", "short"] if args.side == "both" else [args.side]

    ALL_TF_POLL = {"15m": 180, "30m": 300, "1H": 600, "4H": 900}
    if args.tf == "all":
        tf_plan = DEFAULT_TF_PLAN
    elif args.tf in ALL_TF_POLL:
        tf_plan = [(args.tf, ALL_TF_POLL[args.tf], sides)]
    else:
        print(f"未知時框 '{args.tf}'，使用預設全部")
        tf_plan = DEFAULT_TF_PLAN

    if args.live and not OKX_API_KEY:
        print("WARNING: --live 需要 OKX_API_KEY，改用 paper 模式。")
        args.live = False

    # 啟動 Flask + TG 輪詢
    Thread(target=run_web, daemon=True).start()
    Thread(target=poll_tg_callbacks, daemon=True).start()

    bot = TradingBotV3(tf_plan=tf_plan, sides=sides,
                       live=args.live, base_dir=args.dir)
    if args.once:
        bot.print_banner()
        bot.scan_once()
    else:
        bot.run()


if __name__ == "__main__":
    main()
