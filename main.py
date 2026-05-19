#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
賽克斯訊號機器人 v4 — Production-Grade QQE MOD Signal Bot
Discord 按鈕確認下單 | K棒收盤觸發 | 40+ 幣種 | 四時框 15m/30m/1H/4H
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
from flask import Flask, request, jsonify

# ══════════════════════════════════════════════════════════════════════════════
#  USER CONFIGURATION
# ══════════════════════════════════════════════════════════════════════════════
#
COINALYZE_API_KEY  = "82087740-b30d-479f-8846-5ffb51540b19"

# Discord 設定
DISCORD_TOKEN = os.environ.get("DISCORD_TOKEN", "")
DISCORD_CHANNEL_ID = os.environ.get("DISCORD_CHANNEL_ID", "1505971611042320616")

# Telegram 保留（可不設定，設了會同時發）
TG_BOT_TOKEN = os.environ.get("TG_BOT_TOKEN", "")
TG_CHAT_ID   = os.environ.get("TG_CHAT_ID", "")

OKX_API_KEY    = os.environ.get("OKX_API_KEY", "")
OKX_SECRET     = os.environ.get("OKX_SECRET_KEY", "")
OKX_PASSPHRASE = os.environ.get("OKX_PASSPHRASE", "")
OKX_DEMO       = False

MAX_LEVERAGE     = 100
MARGIN_PCT       = 10.0
SIGNAL_COOLDOWN  = 1800
_LIVE_MODE       = True if os.environ.get("OKX_API_KEY") else False
_PAUSED          = False
_BOT_START_TS    = time.time()
AUTO_TRADE = {"15m": True, "30m": True, "1H": True, "4H": True}
_bot_ref          = None

# ══════════════════════════════════════════════════════════════════════════════
#  FIXED STRATEGY CONSTANTS
# ══════════════════════════════════════════════════════════════════════════════

OKX_BASE  = "https://www.okx.com"
CONA_BASE = "https://api.coinalyze.net/v1"
DC_BASE   = "https://discord.com/api/v10"

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

OKX_SWAP: Dict[str, str] = {v: k for k, v in SYMBOLS.items()}

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
BEAR_MIN_BARS      = 20

MAX_CONSEC_LOSS   = 3
PAUSE_HOURS       = 24

BEST_PARAMS: Dict[str, Dict] = {
    "15m_long":  {"tp1_mult": 1.725, "tp2_intraday_mult": 1.8,  "tp2_swing_mult": 1.8,
                  "sl_atr_buffer": 0.01, "structure_lookback": 28, "exit_mode": "fixed"},
    "15m_short": {"tp1_mult": 2.0,   "tp2_intraday_mult": 3.2,  "tp2_swing_mult": 3.2,
                  "sl_atr_buffer": 0.08, "structure_lookback": 20, "exit_mode": "fixed"},
    "30m_long":  {"tp1_mult": 1.725, "tp2_intraday_mult": 1.8,  "tp2_swing_mult": 1.8,
                  "sl_atr_buffer": 0.05, "structure_lookback": 10, "exit_mode": "fixed"},
    "30m_short": {"tp1_mult": 2.0,   "tp2_intraday_mult": 3.2,  "tp2_swing_mult": 3.2,
                  "sl_atr_buffer": 0.01, "structure_lookback": 10, "exit_mode": "trailing"},
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


# ══════════════════════════════════════════════════════════════════════════════
#  DISCORD 通知
# ══════════════════════════════════════════════════════════════════════════════

def _dc_headers() -> Dict:
    return {
        "Authorization": f"Bot {DISCORD_TOKEN}",
        "Content-Type": "application/json",
    }

def dc(text: str):
    if not DISCORD_TOKEN or not DISCORD_CHANNEL_ID:
        return
    try:
        requests.post(
            f"{DC_BASE}/channels/{DISCORD_CHANNEL_ID}/messages",
            headers=_dc_headers(),
            json={"content": text},
            timeout=10,
        )
    except Exception as e# Tue May 19 09:13:31 PM UTC 2026
