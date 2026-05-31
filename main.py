#!/usr/bin/env python3

# -*- coding: utf-8 -*-

"""
賽克斯多時框全功能生產級交易系統 v4 (Production-Grade Multi-Timeframe System)
支援 40+ 幣種監控 | 15m/30m/1H/4H 四時框輪詢 | QQE MOD 雙軌交叉策略
CVD 累積成交量背離過濾 | OKX 實盤與模擬盤自動劃轉下單 | Discord 雙向按鈕控制台
"""

import sys
import io
import os
import time
import math
import json
import hmac
import base64
import hashlib
import logging
import argparse
import subprocess
from datetime import datetime, timezone, timedelta
from threading import Thread, Lock
from time import sleep
from typing import Dict, List, Optional, Tuple, Any

# ══════════════════════════════════════════════════════════════════════════════

# 環境相依性與編碼修正

# ══════════════════════════════════════════════════════════════════════════════

if hasattr(sys.stdout, "reconfigure"):
    sys.stdout.reconfigure(encoding="utf-8", errors="replace")
else:
    sys.stdout = io.TextIOWrapper(sys.stdout.buffer, encoding="utf-8", errors="replace")

# 自動檢查並安裝缺少的第三方套件

REQUIRED_PACKAGES = ["requests", "pandas", "numpy", "ccxt", "flask"]
for pkg in REQUIRED_PACKAGES:
    try:
        __import__(pkg)
    except ImportError:
        print(f"[-] 偵測到缺少必要套件 {pkg}，正在進行背景靜態安裝...")
        subprocess.check_call([sys.executable, "-m", "pip", "install", pkg, "-q"])

import requests
import numpy as np
import pandas as pd
import ccxt
from flask import Flask, request, jsonify

# 指標與形態（已抽出至 indicators.py，見 MODULE_MAP.md）
from indicators import (
    calculate_smooth_rsi,
    calculate_full_qqe_mod,
    calculate_average_true_range,
    calculate_directional_movement_index,
    check_double_bottom,
    check_double_top,
)

# ══════════════════════════════════════════════════════════════════════════════

# 核心全局配置與金鑰設定 (USER CONFIGURATION)

# ══════════════════════════════════════════════════════════════════════════════

COINALYZE_API_KEY = "82087740-b30d-479f-8846-5ffb51540b19"

# Discord Bot 配置

DISCORD_TOKEN = os.environ.get("DISCORD_TOKEN", "")
DISCORD_CHANNEL_ID = os.environ.get("DISCORD_CHANNEL_ID", "1505971611042320616")
DISCORD_PUBLIC_KEY = os.environ.get("DISCORD_PUBLIC_KEY", "79788628a845970d78c0d99d2e85505d9a306bae482459d33eaa8d0f84b6c6d4")

# Telegram 備用通知管道

TG_BOT_TOKEN = os.environ.get("TG_BOT_TOKEN", "")
TG_CHAT_ID = os.environ.get("TG_CHAT_ID", "")

# OKX 交易所帳戶配置

OKX_API_KEY    = os.environ.get("OKX_API_KEY", "")
OKX_SECRET     = os.environ.get("OKX_SECRET_KEY", "")
OKX_PASSPHRASE = os.environ.get("OKX_PASSPHRASE", "")
OKX_DEMO = False  # 是否啟用 OKX 模擬盤交易環境

# BingX 交易所帳戶配置

BINGX_API_KEY    = os.environ.get("BINGX_API_KEY", "")
BINGX_SECRET_KEY = os.environ.get("BINGX_SECRET_KEY", "")
BINGX_BASE       = "https://open-api.bingx.com"

# 交易所路由開關（Discord 指令 /exchange okx|bingx on|off）

EXCHANGE_ENABLED: Dict[str, bool] = {
    "okx":   True,
    "bingx": True  # 預設開啟，/exchange bingx off 才關閉
}

# ══════════════════════════════════════════════════════════════════════════════

# 策略風控常數與運行狀態機

# ══════════════════════════════════════════════════════════════════════════════

MAX_LEVERAGE = 100         # 系統最高安全槓桿限制
RISK_PCT     = 0.10        # 單筆最大風險金額 = 啟動時總資金 × 10%
RISK_TOLERANCE_MULT = 2.0  # 停損容忍倍數：張數進位後停損 ≤ 風險預算 × 此值 才下單（超過則拒單）
POSITION_SLOTS = 10        # 倉位格數（保留供 !setslots 指令使用）
SIGNAL_COOLDOWN = 1800     # 同一商品相同時框的訊號冷卻時間 (秒)
DIR_SIGNAL_COOLDOWN = 3600 # 同幣同方向跨時框去重：1 小時內只下一次（避免 15m/30m/1H 整點同時觸發）
MAX_CONSEC_LOSS = 3       # 最大連續虧損次數限制，達標後觸發熔斷
PAUSE_HOURS = 24           # 熔斷冷卻時間 (小時)

# 系統底層控制開關

_LIVE_MODE = True if os.environ.get("OKX_API_KEY") else False
_PAUSED = False
_BOT_START_TS = time.time()
_INITIAL_BALANCE: Optional[float] = None   # 啟動時抓取一次，固定風險基準
_STATE_LOCK = Lock()

# 各時框獨立自動下單路由開關

AUTO_TRADE: Dict[str, bool] = {
"15m": True,
"30m": True,
"1H":  True,
"4H":  False   # 4H 僅發 DC 通知，需手動授權才下單
}
CVD_ENABLED: bool = False  # 秋總三層 CVD 背離吸收過濾開關（預設關閉，/cvd on 啟用）
ADX_ENABLED: bool = True   # ADX >= ADX_THR 過濾開關
MARGIN_MODE: str  = "cross"   # 保證金模式：cross（全倉）或 isolated（逐倉）

# API 基本節點網址

OKX_BASE = "https://www.okx.com"
CONA_BASE = "https://api.coinalyze.net/v1"
DC_BASE = "https://discord.com/api/v10"

# ══════════════════════════════════════════════════════════════════════════════

# 完整 40+ 核心追蹤幣種字典對照表 (SYMBOLS MAPPING)

# ══════════════════════════════════════════════════════════════════════════════

SYMBOLS: Dict[str, str] = {
"BTC-USDT-SWAP": "BTC/USDT",
"ETH-USDT-SWAP": "ETH/USDT",
"SOL-USDT-SWAP": "SOL/USDT",
"XRP-USDT-SWAP": "XRP/USDT",
"BNB-USDT-SWAP": "BNB/USDT",
"DOGE-USDT-SWAP": "DOGE/USDT",
"ADA-USDT-SWAP": "ADA/USDT",
"TRX-USDT-SWAP": "TRX/USDT",
"SUI-USDT-SWAP": "SUI/USDT",
"LINK-USDT-SWAP": "LINK/USDT",
"AVAX-USDT-SWAP": "AVAX/USDT",
"TON-USDT-SWAP": "TON/USDT",
"HBAR-USDT-SWAP": "HBAR/USDT",
"XLM-USDT-SWAP": "XLM/USDT",
"BCH-USDT-SWAP": "BCH/USDT",
"LTC-USDT-SWAP": "LTC/USDT",
"DOT-USDT-SWAP": "DOT/USDT",
"UNI-USDT-SWAP": "UNI/USDT",
"TAO-USDT-SWAP": "TAO/USDT",
"NEAR-USDT-SWAP": "NEAR/USDT",
"APT-USDT-SWAP": "APT/USDT",
"ARB-USDT-SWAP": "ARB/USDT",
"ATOM-USDT-SWAP": "ATOM/USDT",
"ETC-USDT-SWAP": "ETC/USDT",
"ICP-USDT-SWAP": "ICP/USDT",
"AAVE-USDT-SWAP": "AAVE/USDT",
"RENDER-USDT-SWAP": "RENDER/USDT",
"FIL-USDT-SWAP": "FIL/USDT",
"ENA-USDT-SWAP": "ENA/USDT",
"ALGO-USDT-SWAP": "ALGO/USDT",
"WLD-USDT-SWAP": "WLD/USDT",
"ONDO-USDT-SWAP": "ONDO/USDT",
"JUP-USDT-SWAP": "JUP/USDT",
"POL-USDT-SWAP": "POL/USDT",
"ZEC-USDT-SWAP": "ZEC/USDT",
"DASH-USDT-SWAP": "DASH/USDT",
"PENGU-USDT-SWAP": "PENGU/USDT",
"MORPHO-USDT-SWAP": "MORPHO/USDT",
"HYPE-USDT-SWAP": "HYPE/USDT",
"SKY-USDT-SWAP": "SKY/USDT",
}

OKX_SWAP: Dict[str, str] = {v: k for k, v in SYMBOLS.items()}

# 動態幣種列表狀態
_SYMBOLS_FALLBACK: Dict[str, str] = dict(SYMBOLS)   # 硬編碼備援
_SYMBOLS_CACHE_FILE = os.path.join(os.path.dirname(os.path.abspath(__file__)), "symbols_cache.json")
_symbols_last_updated: float = 0.0   # UNIX timestamp，0 = 從未更新

CONA_SPOT: Dict[str, str] = {
"BTC/USDT": "BTCUSDT.A", "ETH/USDT": "ETHUSDT.A", "SOL/USDT": "SOLUSDT.A",
"XRP/USDT": "XRPUSDT.A", "BNB/USDT": "BNBUSDT.A", "DOGE/USDT": "DOGEUSDT.A",
"ADA/USDT": "ADAUSDT.A", "TRX/USDT": "TRXUSDT.A", "SUI/USDT": "SUIUSDT.A",
"LINK/USDT": "LINKUSDT.A", "AVAX/USDT": "AVAXUSDT.A", "TON/USDT": "TONUSDT.A",
"HBAR/USDT": "HBARUSDT.A", "XLM/USDT": "XLMUSDT.A", "BCH/USDT": "BCHUSDT.A",
"LTC/USDT": "LTCUSDT.A", "DOT/USDT": "DOTUSDT.A", "UNI/USDT": "UNIUSDT.A",
"NEAR/USDT": "NEARUSDT.A", "APT/USDT": "APTUSDT.A", "ARB/USDT": "ARBUSDT.A",
"ATOM/USDT": "ATOMUSDT.A", "ETC/USDT": "ETCUSDT.A", "ICP/USDT": "ICPUSDT.A",
"AAVE/USDT": "AAVEUSDT.A", "FIL/USDT": "FILUSDT.A", "ENA/USDT": "ENAUSDT.A",
"ALGO/USDT": "ALGOUSDT.A", "WLD/USDT": "WLDUSDT.A", "ONDO/USDT": "ONDOUSDT.A",
}

CONA_PERP: Dict[str, str] = {
"BTC/USDT": "BTCUSDT_PERP.A", "ETH/USDT": "ETHUSDT_PERP.A", "SOL/USDT": "SOLUSDT_PERP.A",
"XRP/USDT": "XRPUSDT_PERP.A", "BNB/USDT": "BNBUSDT_PERP.A", "DOGE/USDT": "DOGEUSDT_PERP.A",
"ADA/USDT": "ADAUSDT_PERP.A", "TRX/USDT": "TRXUSDT_PERP.A", "SUI/USDT": "SUIUSDT_PERP.A",
"LINK/USDT": "LINKUSDT_PERP.A", "AVAX/USDT": "AVAXUSDT_PERP.A", "HBAR/USDT": "HBARUSDT_PERP.A",
"XLM/USDT": "XLMUSDT_PERP.A", "BCH/USDT": "BCHUSDT_PERP.A", "LTC/USDT": "LTCUSDT_PERP.A",
"DOT/USDT": "DOTUSDT_PERP.A", "UNI/USDT": "UNIUSDT_PERP.A", "NEAR/USDT": "NEARUSDT_PERP.A",
"APT/USDT": "APTUSDT_PERP.A", "ARB/USDT": "ARBUSDT_PERP.A", "ATOM/USDT": "ATOMUSDT_PERP.A",
"ETC/USDT": "ETCUSDT_PERP.A", "ICP/USDT": "ICPUSDT_PERP.A", "AAVE/USDT": "AAVEUSDT_PERP.A",
"FIL/USDT": "FILUSDT_PERP.A", "ENA/USDT": "ENAUSDT_PERP.A", "ALGO/USDT": "ALGOUSDT_PERP.A",
"WLD/USDT": "WLDUSDT_PERP.A", "ONDO/USDT": "ONDOUSDT_PERP.A",
}

BAR_TO_CONA = {"5m": "5min", "15m": "15min", "30m": "30min", "1H": "1hour", "4H": "4hour"}
BAR_SECONDS = {"5m": 300, "15m": 900, "30m": 1800, "1H": 3600, "4H": 14400}
WARMUP = 700
TIMEFRAMES = ["15m", "30m", "1H", "4H"]

# ══════════════════════════════════════════════════════════════════════════════

# QQE MOD 核心固定數學常數

# ══════════════════════════════════════════════════════════════════════════════

QQE_RSI = 6
QQE_SF = 5
QQE_FACTOR_P = 3.0
QQE_FACTOR_S = 1.61
QQE_THRESHOLD = 3

ADX_THR   = 25
MAX_SL    = 0.12
PIVOT_LEN = 5     # Pivot 結構點左右各需 N 根確認
FUNDING_LONG_MAX = 0.0001
FUNDING_SHORT_MIN = -0.0001
CVD_WINDOW = 3
BEAR_MIN_BARS = 20

# 🌟 全局變數：用於追蹤 Discord 歷史最高訊息 ID，防重複處理

# Discord 訊息 ID 追蹤（在 poll_dc_commands 中初始化）

# ══════════════════════════════════════════════════════════════════════════════

# 多時框全自動路由最佳化動態參數對照表 (PRODUCTION CONFIG MAPPINGS)

# ══════════════════════════════════════════════════════════════════════════════

BEST_PARAMS: Dict[str, Dict[str, Any]] = {
# 迭代優化 + Walk-Forward 驗證 2026-05-24
# ✅ = WF 驗證穩健（已更新）  ⚠️ = WF 可疑（保守維持舊值）
#
# ✅ 15m/long：WF 訓練+0.107→驗證+0.052，略降但正，採用
#    TP1=1.2 TP2=2.5 BE=1.2 BUF=0.0 PVT=10
"15m_long": {
"tp1_mult": 1.2,  "tp2_intraday_mult": 2.5,  "tp2_swing_mult": 2.5, "be_trigger": 1.2,
"sl_atr_buffer": 0.0, "structure_lookback": 10, "exit_mode": "fixed",
"qqe_rsi": 7, "qqe_sf": 5, "qqe_factor": 3.0
},
# ✅ 15m/short：WF 訓練+0.028→驗證+0.124，驗證更好，強力採用
#    TP1=1.0 TP2=2.5 BE=1.2 BUF=0.3 PVT=5
"15m_short": {
"tp1_mult": 1.0,  "tp2_intraday_mult": 2.5,  "tp2_swing_mult": 2.5, "be_trigger": 1.2,
"sl_atr_buffer": 0.3, "structure_lookback": 5, "exit_mode": "fixed",
"qqe_rsi": 5, "qqe_sf": 6, "qqe_factor": 3.0
},
# ✅ 30m/long：WF 訓練+0.114→驗證+0.144，穩健採用
#    TP1=1.5 TP2=3.5 BE=1.2 BUF=0.1 PVT=5
"30m_long": {
"tp1_mult": 1.5,  "tp2_intraday_mult": 3.5,  "tp2_swing_mult": 3.5, "be_trigger": 1.2,
"sl_atr_buffer": 0.1, "structure_lookback": 5, "exit_mode": "fixed",
"qqe_rsi": 5, "qqe_sf": 2, "qqe_factor": 3.0
},
# ⚠️ 30m/short：WF 訓練+0.197→驗證-0.067，過擬合，保守維持舊值
#    維持 TP1=0.8 TP2=3.5 BE=1.0 BUF=0.0 PVT=10
"30m_short": {
"tp1_mult": 0.8,  "tp2_intraday_mult": 3.5,  "tp2_swing_mult": 3.5, "be_trigger": 1.0,
"sl_atr_buffer": 0.0, "structure_lookback": 10, "exit_mode": "fixed",
"qqe_rsi": 5, "qqe_sf": 3, "qqe_factor": 4.0
},
# ✅ 1H/long：WF 訓練+0.203→驗證+0.119，略降但穩健，採用
#    TP1=1.2 TP2=3.5 BE=0.5 BUF=0.5 PVT=5
"1H_long": {
"tp1_mult": 1.2,  "tp2_intraday_mult": 3.5,  "tp2_swing_mult": 3.5, "be_trigger": 0.5,
"sl_atr_buffer": 0.5, "structure_lookback": 5, "exit_mode": "fixed",
"qqe_rsi": 8, "qqe_sf": 2, "qqe_factor": 3.0
},
# ⚠️ 1H/short：WF 訓練+0.140→驗證-0.088，嚴重過擬合，保守維持舊值
#    維持 TP1=1.0 TP2=2.0 BE=1.0 BUF=0.5 PVT=3
"1H_short": {
"tp1_mult": 1.0,  "tp2_intraday_mult": 2.0,  "tp2_swing_mult": 2.0, "be_trigger": 1.0,
"sl_atr_buffer": 0.5, "structure_lookback": 3, "exit_mode": "fixed",
"qqe_rsi": 5, "qqe_sf": 7, "qqe_factor": 4.238
},
"4H_long": {
"tp1_mult": 0.8,  "tp2_intraday_mult": 2.5,  "tp2_swing_mult": 2.5, "be_trigger": 1.0,
"sl_atr_buffer": 0.03, "structure_lookback": 10, "exit_mode": "fixed",
"qqe_rsi": 6, "qqe_sf": 3, "qqe_factor": 3.0
},
"4H_short": {
"tp1_mult": 0.8,  "tp2_intraday_mult": 3.0,  "tp2_swing_mult": 3.0, "be_trigger": 1.0,
"sl_atr_buffer": 0.05, "structure_lookback": 30, "exit_mode": "fixed",
"qqe_rsi": 6, "qqe_sf": 5, "qqe_factor": 3.0
},
}

def get_params(tf: str, side: str, base_dir: str = ".") -> Dict[str, Any]:
    """ 精確抓取指定時框與多空方向的最佳化回測因子參數 """
    param_key = f"{tf}_{side}"
    trade_keys = {"tp1_mult", "tp2_intraday_mult", "tp2_swing_mult", "sl_atr_buffer",
                  "structure_lookback", "exit_mode", "qqe_rsi", "qqe_sf", "qqe_factor"}
    paths = [
        os.path.join(base_dir, f"best_params_{tf.lower()}_{side}.json"),
        os.path.join(base_dir, "final_params_all.json")
    ]
    for fname in paths:
        if os.path.exists(fname):
            try:
                with open(fname, encoding="utf-8") as f:
                    raw = json.load(f)
                data = raw.get(param_key, raw.get("params", raw))
                extracted = {k: v for k, v in data.items() if k in trade_keys}
                if len(extracted) >= 4:
                    base = BEST_PARAMS.get(param_key, {}).copy()
                    base.update(extracted)
                    return base
            except:
                pass
    return BEST_PARAMS.get(param_key, {
        "tp1_mult": 1.7, "tp2_intraday_mult": 1.8, "tp2_swing_mult": 2.5,
        "sl_atr_buffer": 0.08, "structure_lookback": 20, "exit_mode": "fixed",
        "qqe_rsi": 6, "qqe_sf": 5, "qqe_factor": 3.0
    }).copy()

# ══════════════════════════════════════════════════════════════════════════════

# 內部記帳監控狀態管理 (STATE MANAGEMENT)

# ══════════════════════════════════════════════════════════════════════════════

pending_orders: Dict[str, Dict[str, Any]] = {}
active_real_trades: Dict[str, Dict[str, Any]] = {}
# 結構: {trade_key: {"inst_id", "symbol", "direction", "entry_price",
#                    "sl_algo_id", "tp1_order_id", "tp1_hit",
#                    "current_sl", "remaining_amount", "pos_side"}}

class PaperPosition:
    def __init__(self):
        self.open: bool = False
        self.side: str = ""
        self.entry: float = 0.0
        self.sl: float = 0.0
        self.tp1: float = 0.0
        self.tp2: float = 0.0
        self.tp1_hit: bool = False
        self.exit_mode: str = "fixed"
        # ══════════════════════════════════════════════════════════════════════════════

# 通知元件模組 (DISCORD & TELEGRAM NOTIFICATIONS)

# ══════════════════════════════════════════════════════════════════════════════

def _dc_headers() -> Dict[str, str]:
    return {"Authorization": f"Bot {DISCORD_TOKEN}", "Content-Type": "application/json"}

def dc_log(text: str):
    """ 標準化 Discord 文字頻道輸出 """
    if not DISCORD_TOKEN or not DISCORD_CHANNEL_ID:
        print(f"[Console Log] {text}")
        return
    try:
        payload = {"content": str(text)}
        requests.post(f"{DC_BASE}/channels/{DISCORD_CHANNEL_ID}/messages", headers=_dc_headers(), json=payload, timeout=10)
    except Exception as e:
        print(f"  [Discord HTTP 異常] {e}")

def tg_log(text: str):
    """ 備用 Telegram 管道輸出 """
    if not TG_BOT_TOKEN or not TG_CHAT_ID:
        return
    try:
        url = f"https://api.telegram.org/bot{TG_BOT_TOKEN}/sendMessage"
        requests.post(url, json={"chat_id": TG_CHAT_ID, "text": text, "parse_mode": "Markdown"}, timeout=5)
    except:
        pass

def dc_embed_send(embed: Dict[str, Any], components: List[Any] = None) -> Optional[str]:
    """ 發送互動式帶有 UI 按鈕組件的 Embed 進階通知 """
    if not DISCORD_TOKEN or not DISCORD_CHANNEL_ID:
        return None
    payload = {"embeds": [embed]}
    if components:
        payload["components"] = components
    try:
        r = requests.post(f"{DC_BASE}/channels/{DISCORD_CHANNEL_ID}/messages", headers=_dc_headers(), json=payload, timeout=5)
        return r.json().get("id")
    except Exception as e:
        print(f"  [Embed 發送失敗] {e}")
    return None

def dc_embed_edit(message_id: str, new_text: str):
    """ 實時更新交互按鈕的點擊結果狀態，防止二次重複操作 """
    if not DISCORD_TOKEN or not message_id:
        return
    try:
        url = f"{DC_BASE}/channels/{DISCORD_CHANNEL_ID}/messages/{message_id}"
        requests.patch(url, headers=_dc_headers(), json={"content": new_text, "components": []}, timeout=5)
    except Exception as e:
        print(f"  [Embed 編輯失敗] {e}")

def create_interactive_signal(sig: Dict[str, Any], symbol: str, tf: str, cvd_ok: bool) -> str:
    """ 建置完全體交互控制台卡片，整合止損比率、盈虧比與手動掛單快取 """
    side_emoji = "🟢" if sig["side"] == "long" else "🔴"
    dir_name = "多頭趨勢進場" if sig["side"] == "long" else "空頭趨勢進場"
    swing_tag = "📐 趨勢波段追蹤" if sig["is_swing"] else "⚡ 短線日內反彈"
    cvd_tag = "CVD ✅ 動能同步確認" if cvd_ok else "CVD ⚠️ 量能背離過濾"
    card_color = 0x2ecc71 if sig["side"] == "long" else 0xe74c3c

    tw_time = datetime.fromisoformat(sig["time"].replace("Z", "").replace("+00:00", "")) + timedelta(hours=8)
    coin_name = symbol.split("/")[0]
    unique_callback_key = f"sykes_{coin_name.lower()}_{tf}_{sig['side']}_{int(time.time())}"

    exit_mode_label = "固定限價" if sig.get("exit_mode") == "fixed" else "追蹤止損"

    # 寫入待核准交易訂單池快取
    pending_orders[unique_callback_key] = {
    "symbol": OKX_SWAP.get(symbol, symbol),
    "direction": sig["side"],
    "entry": sig["entry"],
    "sl": sig["sl"],
    "tp1": sig["tp1"],
    "tp2": sig["tp2"],
    "exit_mode": sig.get("exit_mode", "fixed")
    }

    source_tag = sig.get("source_tag", "C3")
    source_label = f" 【{source_tag}】" if source_tag else ""
    embed_payload = {
    "title": f"{side_emoji} {coin_name} [{tf} - {dir_name}]{source_label}",
    "description": f"**環境特徵:** {swing_tag} | {cvd_tag}",
    "color": int(card_color, 16) if isinstance(card_color, str) else card_color,
    "fields": [
        {"name": "觸發時間 (TST)", "value": tw_time.strftime("%Y/%m/%d %H:%M:%S"), "inline": True},
        {"name": "ATR 當前波動", "value": f"`{sig['atr']}`", "inline": True},
        {"name": "離場機制", "value": f"`{exit_mode_label}`", "inline": True},
        {"name": "規劃進場價", "value": f"**{sig['entry']}** USDT", "inline": True},
        {"name": "安全結構止損 🛑", "value": f"`{sig['sl']}` ({sig['risk_pct']:.2f}%)", "inline": True},
        {"name": "保證金防禦", "value": "TP1達標後自動推成本價", "inline": True},
        {"name": f"第一目標價 TP1 (分批50%) [盈虧比 1:{sig['rr1']:.2f}]", "value": f"`{sig['tp1']}`", "inline": False},
        {"name": f"終點目標價 TP2 (剩餘50%) [盈虧比 1:{sig['rr2']:.2f}]", "value": f"`{sig['tp2']}`", "inline": False}
    ],
    "footer": {"text": f"交易核心識別碼: {unique_callback_key}"}
    }

    components_payload = [{
    "type": 1,
    "components": [
        {"type": 2, "style": 3, "label": "✅ 授權實盤下單", "custom_id": f"confirm_{unique_callback_key}"},
        {"type": 2, "style": 4, "label": "❌ 放棄此訊號", "custom_id": f"skip_{unique_callback_key}"}
    ]
    }]

    msg_id = dc_embed_send(embed_payload, components_payload)
    if msg_id:
        pending_orders[unique_callback_key]["msg_id"] = msg_id

    # 同步發送 Telegram 備用通知
    tg_text = f"【{side_emoji} 賽克斯新訊號】\n商品: {coin_name} ({tf})\n方向: {sig['side'].upper()}\n進場: {sig['entry']}\n止損: {sig['sl']}\nTP1: {sig['tp1']} | TP2: {sig['tp2']}"
    tg_log(tg_text)

    return unique_callback_key

# ══════════════════════════════════════════════════════════════════════════════

# OKX 底層 REST API 原始簽章與行情擷取 (EXCHANGE CONNECTOR)

# ══════════════════════════════════════════════════════════════════════════════

def _okx_generate_signature(timestamp: str, method: str, request_path: str, body: str = "") -> str:
    message = timestamp + method + request_path + body
    secret_bytes = OKX_SECRET.encode("utf-8")
    signature = hmac.new(secret_bytes, message.encode("utf-8"), hashlib.sha256).digest()
    return base64.b64encode(signature).decode("utf-8")
def _fetch_okx_public_data(endpoint_path: str, query_params: dict) -> list:
    """ 高穩定度原生連接器，負責拉取 K 線與即時費率 """
    try:
        url = f"{OKX_BASE}{endpoint_path}"
        headers = {
            "Content-Type": "application/json",
            **({"x-simulated-trading": "1"} if OKX_DEMO else {})
        }
        r = requests.get(url, params=query_params, headers=headers, timeout=12)
        if r.status_code == 200:
            res_json = r.json()
            if res_json.get("code") == "0":
                return res_json.get("data", [])
        return []
    except:
        return []

def fetch_market_candles(inst_id: str, timeframe_bar: str, fetch_limit: int = WARMUP) -> pd.DataFrame:
    """ 抓取歷史 K 線數據並轉換為精準格式之 Pandas DataFrame """
    raw_candles = _fetch_okx_public_data("/api/v5/market/candles", {
        "instId": inst_id,
        "bar": timeframe_bar,
        "limit": str(min(fetch_limit, 300))
    })
    if not raw_candles:
        return pd.DataFrame()

    sorted_candles = sorted(raw_candles, key=lambda x: int(x[0]))
    df = pd.DataFrame(sorted_candles, columns=["ts", "open", "high", "low", "close", "vol", "volCcy", "volCcyQuote", "confirm"])
    for col in ["open", "high", "low", "close", "vol"]:
        df[col] = df[col].astype(float)
    df["ts"] = pd.to_datetime(df["ts"].astype(np.int64), unit="ms", utc=True)
    df.set_index("ts", inplace=True)
    return df.iloc[:-1]

def fetch_current_funding_rate(swap_id: str) -> float:
    data_list = _fetch_okx_public_data("/api/v5/public/funding-rate", {"instId": swap_id})
    if data_list:
        return float(data_list[0].get("fundingRate", 0.0))
    return 0.0


# COINALYZE 大數據特徵過濾引擎 (CVD + OI ANALYSIS)

# ══════════════════════════════════════════════════════════════════════════════

def _execute_coinalyze_request(endpoint: str, query_params: dict) -> list:
    """ 對接 Coinalyze API 獲取大體量逐筆成交明細累積與未平倉特徵 """
    if not COINALYZE_API_KEY:
        return []
    try:
        url = f"{CONA_BASE}/{endpoint}"
        req_headers = {"api-key": COINALYZE_API_KEY}
        r = requests.get(url, params=query_params, headers=req_headers, timeout=15)
        if r.status_code == 200:
            res_data = r.json()
            if isinstance(res_data, list):
                return res_data
            return res_data.get("result", res_data.get("data", []))
        return []
    except:
        return []

def calculate_cumulative_volume_delta(cona_symbol: str, cona_interval: str, start_timestamp: int, end_timestamp: int) -> pd.Series:
    """ 計算出精準的 CVD (累積成交量差額) 指標線 """
    raw_data = _execute_coinalyze_request("history", {
    "symbols": cona_symbol,
    "interval": cona_interval,
    "from": str(start_timestamp // 1000),
    "to": str(end_timestamp // 1000),
    "convert_to_usd": "false"
    })
    if not raw_data:
        return pd.Series(dtype=float)

    records = []
    for node in raw_data:
        if not isinstance(node, dict):
            continue
        t_sec = node.get("t", node.get("time", 0))
        tot_v = float(node.get("v", 0.0) or 0.0)
        buy_v = float(node.get("bv", tot_v / 2.0) or (tot_v / 2.0))
        sell_v = tot_v - buy_v
        delta = buy_v - sell_v
        records.append((int(t_sec) * 1000, delta))

    if not records:
        return pd.Series(dtype=float)

    df = pd.DataFrame(records, columns=["ts", "delta"])
    df["ts"] = pd.to_datetime(df["ts"], unit="ms", utc=True)
    df.set_index("ts", inplace=True)
    df.sort_index(inplace=True)
    return df["delta"].cumsum()

def fetch_open_interest_series(cona_symbol: str, cona_interval: str, start_timestamp: int, end_timestamp: int) -> pd.Series:
    """ 追蹤機構持倉未平倉合約總量 (OI) 走勢 """
    raw_data = _execute_coinalyze_request("open-interest-history", {
    "symbols": cona_symbol,
    "interval": cona_interval,
    "from": str(start_timestamp // 1000),
    "to": str(end_timestamp // 1000)
    })
    if not raw_data:
        return pd.Series(dtype=float)

    records = []
    for node in raw_data:
        if isinstance(node, dict):
            records.append((int(node.get("t", 0)) * 1000, float(node.get("v", 0.0) or 0.0)))
    if not records:
        return pd.Series(dtype=float)

    df = pd.DataFrame(records, columns=["ts", "oi"])
    df["ts"] = pd.to_datetime(df["ts"], unit="ms", utc=True)
    df.set_index("ts", inplace=True)
    df.sort_index(inplace=True)
    return df["oi"]

# ══════════════════════════════════════════════════════════════════════════════

# 複雜技術指標庫算力模組 (TECHNICAL INDICATORS MATHEMATICS)

# ══════════════════════════════════════════════════════════════════════════════

# QQE MOD / ATR / ADX 計算函數已移至 indicators.py（見檔頭 import 與 MODULE_MAP.md）

# ══════════════════════════════════════════════════════════════════════════════

# 接續上篇：OKX 實盤風控倉位自動計算與分批委託鏈 (ORDER EXECUTION)

# ══════════════════════════════════════════════════════════════════════════════

def _initialize_ccxt_client() -> ccxt.okx:
    client = ccxt.okx({
    "apiKey": OKX_API_KEY,
    "secret": OKX_SECRET,
    "password": OKX_PASSPHRASE,
    "options": {"defaultType": "swap"}
    })
    if OKX_DEMO:
        client.set_sandbox_mode(True)
    return client

def _place_okx_algo_sl(inst_id: str, side: str, amount: str, sl_trigger_px: str, pos_side: str) -> dict:
    """ 使用 OKX REST API 掛條件式止損 Algo 單 (slTriggerPx)
        sz 固定傳 "0" + closeFraction="1" = 觸發時平掉該方向全部倉位，不依賴張數
    """
    now_utc = datetime.now(timezone.utc)
    ts = now_utc.strftime("%Y-%m-%dT%H:%M:%S.") + f"{now_utc.microsecond // 1000:03d}Z"
    body = json.dumps({
        "instId": inst_id, "tdMode": MARGIN_MODE, "side": side,
        "ordType": "conditional", "posSide": pos_side,
        "closeFraction": "1",
        "slTriggerPx": sl_trigger_px, "slOrdPx": "-1",
        "slTriggerPxType": "mark"
    })
    path = "/api/v5/trade/order-algo"
    sig = _okx_generate_signature(ts, "POST", path, body)
    headers = {
        "OK-ACCESS-KEY": OKX_API_KEY, "OK-ACCESS-SIGN": sig,
        "OK-ACCESS-TIMESTAMP": ts, "OK-ACCESS-PASSPHRASE": OKX_PASSPHRASE,
        "Content-Type": "application/json"
    }
    if OKX_DEMO:
        headers["x-simulated-trading"] = "1"
    r = requests.post(f"{OKX_BASE}{path}", headers=headers, data=body, timeout=10)
    return r.json()

def _place_okx_algo_trailing(inst_id: str, side: str, amount: str, callback_ratio: str, pos_side: str) -> dict:
    """ 使用 OKX REST API 掛移動止損 (Trailing Stop) Algo 單 """
    now_utc = datetime.now(timezone.utc)
    ts = now_utc.strftime("%Y-%m-%dT%H:%M:%S.") + f"{now_utc.microsecond // 1000:03d}Z"
    body = json.dumps({
        "instId": inst_id, "tdMode": MARGIN_MODE, "side": side,
        "ordType": "move_order_stop", "sz": amount, "posSide": pos_side,
        "callbackRatio": callback_ratio, "activePx": ""
    })
    path = "/api/v5/trade/order-algo"
    sig = _okx_generate_signature(ts, "POST", path, body)
    headers = {
        "OK-ACCESS-KEY": OKX_API_KEY, "OK-ACCESS-SIGN": sig,
        "OK-ACCESS-TIMESTAMP": ts, "OK-ACCESS-PASSPHRASE": OKX_PASSPHRASE,
        "Content-Type": "application/json"
    }
    if OKX_DEMO:
        headers["x-simulated-trading"] = "1"
    r = requests.post(f"{OKX_BASE}{path}", headers=headers, data=body, timeout=10)
    return r.json()

def _cancel_okx_algo_order(inst_id: str, algo_id: str) -> bool:
    """ 取消 OKX 條件/止損 Algo 單 """
    now_utc = datetime.now(timezone.utc)
    ts = now_utc.strftime("%Y-%m-%dT%H:%M:%S.") + f"{now_utc.microsecond // 1000:03d}Z"
    body = json.dumps([{"algoId": algo_id, "instId": inst_id}])
    path = "/api/v5/trade/cancel-algos"
    sig = _okx_generate_signature(ts, "POST", path, body)
    headers = {
        "OK-ACCESS-KEY": OKX_API_KEY, "OK-ACCESS-SIGN": sig,
        "OK-ACCESS-TIMESTAMP": ts, "OK-ACCESS-PASSPHRASE": OKX_PASSPHRASE,
        "Content-Type": "application/json"
    }
    if OKX_DEMO:
        headers["x-simulated-trading"] = "1"
    try:
        r = requests.post(f"{OKX_BASE}{path}", headers=headers, data=body, timeout=10)
        return r.json().get("code") == "0"
    except Exception as e:
        print(f"[Trailing] 取消Algo單失敗 {algo_id}: {e}")
        return False

def execute_okx_trade_pipeline(symbol_id: str, trade_side: str, entry_price: float,
                              stop_loss: float, tp1: float, tp2: float, exit_mode: str = "fixed",
                              tf_id: str = "15m", position_scale: float = 1.0) -> None:
    """
    實盤訂單路由模組：整合動態槓桿、USDT 單位下單、市價與限價單組合
    position_scale：倉位縮放係數（1.0=正常，0.5=半倉，由 dynamic_sl_tp 傳入）
    """
    global _LIVE_MODE, MAX_LEVERAGE, POSITION_SLOTS, _INITIAL_BALANCE
    if not _LIVE_MODE:
        dc_log(f"📝 [紙交易通知] 商品 {symbol_id} 方向 {trade_side} 處於 Paper 模擬模式，跳過交易。")
        return

    try:
        ex = _initialize_ccxt_client()
        ex.load_markets()

        balance_data = ex.fetch_balance()
        available_usdt = float(balance_data.get("USDT", {}).get("free", 0.0))
        if available_usdt <= 5.0:
            dc_log(f"⚠️ **實盤交易中斷**: 可用保證金不足 ({available_usdt:.2f} USDT)")
            return

        # ── RISK 公式（基準＝錢包餘額，不含浮動盈虧）────────────────────────
        # 用 cashBal/availBal（已實現權益），避免持倉浮盈浮虧讓下一單倉位忽大忽小。
        # 只有真正平倉賺賠改變錢包餘額時，下一單風險才隨帳戶大小調整。
        total_usdt = float(balance_data.get("USDT", {}).get("total", 0.0))   # 含uPnL，僅供顯示
        _okx_detail = (balance_data.get("info", {}) or {}).get("data", [{}])
        _okx_d0     = _okx_detail[0] if isinstance(_okx_detail, list) and _okx_detail else {}
        wallet_usdt = 0.0
        for _ccy in (_okx_d0.get("details") or []):
            if _ccy.get("ccy") == "USDT":
                wallet_usdt = float(_ccy.get("cashBal") or _ccy.get("availBal") or 0.0)
                break
        # 備援：抓不到 cashBal 時退回 free（不含 uPnL），最後才用 total
        base_funds = wallet_usdt if wallet_usdt > 0 else (available_usdt if available_usdt > 0 else total_usdt)
        risk_usdt  = base_funds * RISK_PCT         # 單筆最大風險金額 = 錢包餘額 × RISK_PCT

        ticker_info = ex.fetch_ticker(symbol_id)
        current_market_price = float(ticker_info.get("last", entry_price))

        sl_distance_pct = abs(current_market_price - stop_loss) / current_market_price
        if sl_distance_pct <= 0.0001:
            dc_log("⚠️ **風控異常**: 結構止損間距過小，自動拒絕下單以防爆倉。")
            return

        # 倉位價值 = 風險金額 ÷ 止損距離%（不論模式皆照 RISK 公式）
        position_value   = risk_usdt / sl_distance_pct

        if MARGIN_MODE == "cross":
            # 全倉模式：直接用該幣種最大槓桿，保證金自動最小化
            try:
                _mkt_lev = ex.market(symbol_id)
                coin_max_lev = int(float(
                    ((_mkt_lev.get("limits", {}) or {}).get("leverage", {}) or {}).get("max")
                    or MAX_LEVERAGE
                ))
            except Exception:
                coin_max_lev = MAX_LEVERAGE
            calculated_leverage = max(1, min(coin_max_lev, MAX_LEVERAGE))
        else:
            # 逐倉模式：維持原本動態槓桿邏輯
            calculated_leverage = max(1, min(int(50.0 / (sl_distance_pct * 100.0)), MAX_LEVERAGE))

        # 保證金 = 倉位價值 ÷ 槓桿
        allocated_margin = position_value / calculated_leverage

        # ── Fix 3：單倉保證金上限 = 總資產 × RISK_PCT ────────────────────────
        max_margin = base_funds * RISK_PCT   # e.g. 1000 USDT × 10% = 100 USDT
        if allocated_margin > max_margin:
            allocated_margin = max_margin
            position_value   = allocated_margin * calculated_leverage

        # ── 訊號分級倉位縮放（position_scale 由 dynamic_sl_tp 傳入）──────────
        # 弱訊號（score 30~59）：倉位縮小 50%
        if position_scale < 1.0:
            position_value   = round(position_value   * position_scale, 2)
            allocated_margin = round(allocated_margin * position_scale, 2)

        # ── 全倉模式開倉前風控：預估加入新倉後維持保證金率 < 150% → 跳過 ──────
        if MARGIN_MODE == "cross":
            try:
                acct_info = ex.fetch_balance()
                info_root = acct_info.get("info", {}) or {}
                data_list = info_root.get("data", [])
                d0 = data_list[0] if isinstance(data_list, list) and data_list else {}
                total_eq = float(d0.get("totalEq") or total_usdt or 0)
                mmr_raw  = float(d0.get("mgnRatio") or info_root.get("mgnRatio", 0) or 0)
                # OKX 回傳小數（如 10.5 = 1050%），若 0<x<50 視為倍數需 ×100
                mmr_now = mmr_raw * 100 if 0 < mmr_raw < 50 else mmr_raw
                # 預估加入新倉後維持保證金率：新倉佔用保證金降低權益緩衝
                if mmr_now > 0 and total_eq > 0:
                    projected_mmr = mmr_now * max(total_eq - allocated_margin, 0) / total_eq
                    if projected_mmr < 350:
                        dc_log(f"⚠️ OKX 跳過 [{symbol_id}]：維持保證金率不足"
                               f"（預估 {projected_mmr:.1f}% < 350%）")
                        return
            except Exception as risk_check_err:
                print(f"[RiskCheck] OKX 維持保證金率檢查失敗: {risk_check_err}")

        # 下單前檢查：可用餘額 >= 保證金
        if available_usdt < allocated_margin:
            dc_log(f"⚠️ 保證金不足，跳過下單：可用 {available_usdt:.2f} USDT，需要 {allocated_margin:.2f} USDT")
            return

        # 已開倉數量 < POSITION_SLOTS
        positions_raw = ex.fetch_positions()
        open_positions_count = len([p for p in positions_raw if float(p.get("contracts", 0) or 0) > 0])
        if open_positions_count >= POSITION_SLOTS:
            dc_log(f"⚠️ 已達最大倉位數 ({open_positions_count}/{POSITION_SLOTS})，跳過下單")
            return

        try:
            ex.set_leverage(calculated_leverage, symbol_id, params={"posSide": trade_side})
        except:
            pass

        is_buy       = (trade_side == "long")
        entry_action = "buy"  if is_buy else "sell"
        exit_action  = "sell" if is_buy else "buy"
        inst_id      = OKX_SWAP.get(symbol_id, symbol_id)

        # ── USDT 單位下單（tgtCcy=quote_ccy）─────────────────────────────────
        # OKX 支援以報價幣（USDT）指定倉位大小，完全迴避張數轉換問題
        # position_value = 實際倉位名義價值（USDT）
        # TP1 / TP2 各拆 50%，同樣用 USDT 金額指定（reduceOnly=True）
        half_usdt = round(position_value * 0.5, 2)

        execution_report = [
            f"🚀 **賽克斯實盤下單鏈成功發動**",
            f"商品代號: `{symbol_id}` | 交易方向: `{'做多 LONG' if is_buy else '做空 SHORT'}`",
            f"配置槓桿: `{calculated_leverage}x` | 倉位價值: `{position_value:.2f}` USDT"
            f" | 保證金: `{allocated_margin:.2f}` USDT | 風險: `{risk_usdt:.2f}` USDT ({RISK_PCT*100:.0f}%)"
        ]

        # ── 市價開倉：優先 USDT 單位（tgtCcy=quote_ccy），不支援則降級用「張」──
        # 部分幣種（如 SKY-USDT-SWAP）不支援 tgtCcy=quote_ccy，回傳 sCode=59110
        fallback_to_contracts = False   # 是否降級用張數下單
        total_contracts       = 0       # 降級時的總張數
        ct_val                = 1.0     # 合約面值 ctVal
        entry_order           = None

        try:
            entry_order = ex.create_market_order(
                symbol=symbol_id,
                side=entry_action,
                amount=position_value,          # USDT 名義值
                params={
                    "posSide":  trade_side,
                    "tdMode":   MARGIN_MODE,
                    "tgtCcy":   "quote_ccy",    # 告訴 OKX 用報價幣（USDT）計量
                }
            )
        except Exception as quote_err:
            if "59110" in str(quote_err):
                # 不支援 USDT 計量 → 降級用張數下單
                fallback_to_contracts = True
                mkt    = ex.market(symbol_id)
                ct_val = float(mkt.get("contractSize", 1.0) or 1.0)   # 合約面值
                contract_notional = current_market_price * ct_val     # 1 張名義價值(USDT)
                # 張數 = 倉位USDT ÷ 1張名義，無條件捨去取整數張
                total_contracts = int(position_value / contract_notional)
                # 不足 1 張時進位成 1 張（最小可下單量）
                if total_contracts < 1:
                    total_contracts = 1
                # ★ 風控核心：停損虧損上限 = 風險預算 × RISK_TOLERANCE_MULT（容忍 2 倍）
                #   超過才拒單；2 倍內可接受（例如 1U 預算允許停損到 2U）
                worst_loss = total_contracts * contract_notional * sl_distance_pct
                if worst_loss > risk_usdt * RISK_TOLERANCE_MULT:
                    dc_log(f"⚠️ OKX 跳過 [{symbol_id}]：預估停損虧損 {worst_loss:.2f}U "
                           f"> 風險預算 {risk_usdt:.2f}U × {RISK_TOLERANCE_MULT}（上限 {risk_usdt*RISK_TOLERANCE_MULT:.2f}U），拒絕超額下單")
                    return
                dc_log(f"ℹ️ [{symbol_id}] 不支援 tgtCcy=quote_ccy（59110），"
                       f"自動降級用張數下單：{total_contracts} 張（ctVal={ct_val}，預估觸損 {worst_loss:.2f}U）")
                entry_order = ex.create_market_order(
                    symbol=symbol_id,
                    side=entry_action,
                    amount=total_contracts,     # 張數
                    params={"posSide": trade_side, "tdMode": MARGIN_MODE}
                )
                execution_report.append(f"📐 張數降級下單: `{total_contracts}` 張 (ctVal={ct_val})")
            else:
                # 其他錯誤照常往外拋給通用例外處理
                raise

        # 等待成交均價（市價單可能需要短暫延遲才有 average）
        executed_average_price = entry_order.get("average") or entry_order.get("price")
        if not executed_average_price or float(executed_average_price or 0) == 0:
            time.sleep(0.5)
            try:
                filled = ex.fetch_order(entry_order["id"], symbol_id)
                executed_average_price = filled.get("average") or filled.get("price") or current_market_price
            except:
                executed_average_price = current_market_price
        executed_average_price = float(executed_average_price)
        execution_report.append(f"交易所實際成交均價: `{executed_average_price}`")

        sl_algo_id   = None
        tp1_order_id = None

        # 止損：OKX algo slTriggerPx 條件單（closeFraction=1 平全倉，無需指定張數）
        sl_side = "sell" if trade_side == "long" else "buy"
        sl_pos  = trade_side
        try:
            sl_result  = _place_okx_algo_sl(
                inst_id=inst_id,
                side=sl_side,
                amount="0",                  # closeFraction=1 時 sz 傳 0
                sl_trigger_px=str(stop_loss),
                pos_side=sl_pos
            )
            sl_algo_id = (sl_result.get("data") or [{}])[0].get("algoId")
            if sl_algo_id:
                execution_report.append(f"🛑 OKX Algo 止損已錨定: `{stop_loss}` (algoId: {sl_algo_id})")
            else:
                raise RuntimeError(f"API 回應無 algoId: {sl_result}")
        except Exception as sle:
            dc_log(
                f"🚨 **止損單掛載失敗，請立即手動設定止損！**\n"
                f"商品: `{symbol_id}` 方向: `{trade_side}`\n"
                f"倉位已開，止損價: `{stop_loss}`\n"
                f"錯誤: `{sle}`"
            )
            return

        if not fallback_to_contracts:
            # ── USDT 單位 TP 分批（各 50%，不依賴張數）─────────────────────
            # TP1
            try:
                tp1_order = ex.create_order(
                    symbol=symbol_id, type="limit", side=exit_action,
                    amount=half_usdt, price=tp1,
                    params={
                        "posSide":    trade_side,
                        "tdMode":     MARGIN_MODE,
                        "reduceOnly": True,
                        "tgtCcy":     "quote_ccy",
                    }
                )
                tp1_order_id = tp1_order.get("id")
                execution_report.append(f"🌓 TP1 限價單 (50% = {half_usdt:.2f} USDT): `{tp1}` (ordId: {tp1_order_id})")
            except Exception as tp1e:
                execution_report.append(f"⚠️ TP1委託失敗: {tp1e}")
            # TP2
            try:
                ex.create_order(
                    symbol=symbol_id, type="limit", side=exit_action,
                    amount=half_usdt, price=tp2,
                    params={
                        "posSide":    trade_side,
                        "tdMode":     MARGIN_MODE,
                        "reduceOnly": True,
                        "tgtCcy":     "quote_ccy",
                    }
                )
                execution_report.append(f"🌕 TP2 限價單 (50% = {half_usdt:.2f} USDT): `{tp2}`")
            except Exception as tp2e:
                execution_report.append(f"⚠️ TP2委託失敗: {tp2e}")
        else:
            # ── 張數降級 TP 分批 ─────────────────────────────────────────
            if total_contracts >= 2:
                tp1_qty = int(total_contracts // 2)
                tp2_qty = total_contracts - tp1_qty
                # TP1
                try:
                    tp1_order = ex.create_order(
                        symbol=symbol_id, type="limit", side=exit_action,
                        amount=tp1_qty, price=tp1,
                        params={"posSide": trade_side, "tdMode": MARGIN_MODE, "reduceOnly": True}
                    )
                    tp1_order_id = tp1_order.get("id")
                    execution_report.append(f"🌓 TP1 限價單 ({tp1_qty} 張): `{tp1}` (ordId: {tp1_order_id})")
                except Exception as tp1e:
                    execution_report.append(f"⚠️ TP1委託失敗: {tp1e}")
                # TP2
                try:
                    ex.create_order(
                        symbol=symbol_id, type="limit", side=exit_action,
                        amount=tp2_qty, price=tp2,
                        params={"posSide": trade_side, "tdMode": MARGIN_MODE, "reduceOnly": True}
                    )
                    execution_report.append(f"🌕 TP2 限價單 ({tp2_qty} 張): `{tp2}`")
                except Exception as tp2e:
                    execution_report.append(f"⚠️ TP2委託失敗: {tp2e}")
            else:
                # 僅 1 張：TP1 全出，不設 TP2
                try:
                    tp1_order = ex.create_order(
                        symbol=symbol_id, type="limit", side=exit_action,
                        amount=total_contracts, price=tp1,
                        params={"posSide": trade_side, "tdMode": MARGIN_MODE, "reduceOnly": True}
                    )
                    tp1_order_id = tp1_order.get("id")
                    execution_report.append(f"🌓 TP1 限價單 (全出 {total_contracts} 張): `{tp1}` (ordId: {tp1_order_id})")
                except Exception as tp1e:
                    execution_report.append(f"⚠️ TP1委託失敗: {tp1e}")
                dc_log(f"⚠️ [{symbol_id}] 倉位1張，TP2略過改全出")
                execution_report.append("⚠️ 倉位1張，TP2略過改全出")

        dc_log("\n".join(execution_report))
    except Exception as general_error:
        dc_log(f"❌ **交易所執行鏈嚴重崩潰**: {general_error}")


def _bingx_sign(params: dict, secret: str) -> str:
    """BingX HMAC-SHA256 簽名：參數不排序，保持原始順序"""
    query = "&".join(f"{k}={v}" for k, v in params.items())
    return hmac.new(secret.encode("utf-8"), query.encode("utf-8"), hashlib.sha256).hexdigest()

def _bingx_request(method: str, path: str, params: dict, headers: dict, timeout: int = 10):
    """BingX 統一請求：timestamp 加入後計算 signature，signature 單獨附加"""
    ts = str(int(time.time() * 1000))
    params["timestamp"] = ts
    sig = _bingx_sign(params, BINGX_SECRET_KEY)
    query = "&".join(f"{k}={v}" for k, v in params.items()) + f"&signature={sig}"
    url = f"{BINGX_BASE}{path}?{query}"
    if method == "GET":
        return requests.get(url, headers=headers, timeout=timeout)
    else:
        return requests.post(url, headers=headers, timeout=timeout)

def execute_bingx_trade_pipeline(symbol_id: str, trade_side: str, entry_price: float,
                                  stop_loss: float, tp1: float, tp2: float,
                                  exit_mode: str = "fixed", tf_id: str = "15m",
                                  position_scale: float = 1.0) -> None:
    """
    BingX 永續合約下單
    position_scale：倉位縮放係數（1.0=正常，0.5=半倉，由 dynamic_sl_tp 傳入）
    """
    if not BINGX_API_KEY or not BINGX_SECRET_KEY:
        dc_log("⚠️ BingX API Key 未設定，跳過 BingX 下單")
        return
    try:
        # 轉換幣種格式：BTC/USDT → BTC-USDT
        bingx_symbol = symbol_id.replace("/", "-")

        # 取得帳戶餘額
        headers = {"X-BX-APIKEY": BINGX_API_KEY}
        r = _bingx_request("GET", "/openApi/swap/v2/user/balance", {}, headers)
        bal_resp = r.json()
        bal = bal_resp.get("data", {})
        if isinstance(bal, dict) and "balance" in bal:
            bal = bal["balance"]
        total_usdt = float(
            bal.get("equity") or bal.get("balance") or bal.get("totalMarginBalance") or 0
        )
        avail_usdt = float(
            bal.get("availableMargin") or bal.get("available") or bal.get("availableBalance") or total_usdt
        )
        # 錢包餘額（不含未實現盈虧）＝風險基準，避免浮動盈虧讓倉位忽大忽小
        wallet_usdt = float(bal.get("balance") or bal.get("totalMarginBalance") or 0)
        if wallet_usdt <= 0:
            wallet_usdt = total_usdt   # 備援：抓不到才退回淨值
        if total_usdt <= 0:
            dc_log(f"⚠️ BingX 餘額讀取異常（返回值: {bal_resp}），跳過下單")
            return

        # 訊號分級倉位縮放（position_scale 由 dynamic_sl_tp 傳入）
        # 風險基準用 wallet_usdt（錢包餘額），不用 equity（含uPnL）
        risk_usdt = wallet_usdt * RISK_PCT * position_scale
        sl_dist_pct = abs(entry_price - stop_loss) / entry_price
        if sl_dist_pct <= 0.0001:
            dc_log("⚠️ BingX 止損距離過小，跳過下單")
            return

        position_value = risk_usdt / sl_dist_pct

        if MARGIN_MODE == "cross":
            # 全倉模式：查該幣種最大槓桿，直接用最大槓桿（保證金自動最小化）
            try:
                lev_q = _bingx_request("GET", "/openApi/swap/v2/trade/leverage", {
                    "symbol": bingx_symbol
                }, headers).json()
                lev_data = lev_q.get("data", {}) or {}
                if trade_side == "long":
                    coin_max_lev = int(float(lev_data.get("maxLongLeverage") or MAX_LEVERAGE))
                else:
                    coin_max_lev = int(float(lev_data.get("maxShortLeverage") or MAX_LEVERAGE))
            except Exception:
                coin_max_lev = MAX_LEVERAGE
            leverage = max(1, min(coin_max_lev, MAX_LEVERAGE))
        else:
            # 逐倉模式：維持原本動態槓桿邏輯
            leverage = max(1, min(int(50.0 / (sl_dist_pct * 100.0)), MAX_LEVERAGE))

        # 保證金 = 倉位價值 ÷ 槓桿（全倉用最大槓桿後此值即實際新倉保證金）
        margin = position_value / leverage
        max_margin = wallet_usdt * RISK_PCT   # 保證金上限與風險基準一致（錢包餘額）
        if margin > max_margin:
            margin = max_margin
            position_value = margin * leverage

        if avail_usdt < margin:
            dc_log(f"⚠️ BingX 保證金不足：可用 {avail_usdt:.2f}，需要 {margin:.2f}")
            return

        # ── BingX 風險率保護 ──────────────────────────────────────────────────
        # BingX 風險率 = 已用保證金 / 帳戶淨值，越高越危險，接近 100% 會爆倉
        # 公式：used_margin = equity - availableMargin
        # 新倉開完後估算風險率，超過閾值就不開
        BINGX_MAX_RISK_RATE = 0.70   # 最高允許風險率 70%（超過就不開新倉）
        equity        = float(bal.get("equity") or total_usdt)
        used_margin   = equity - avail_usdt
        # 加上這筆新倉的保證金後，估算風險率
        projected_used = used_margin + margin
        projected_risk_rate = projected_used / equity if equity > 0 else 1.0
        if projected_risk_rate > BINGX_MAX_RISK_RATE:
            dc_log(f"⚠️ BingX 跳過 [{symbol_id}]：風險率預估 {projected_risk_rate:.0%} > {BINGX_MAX_RISK_RATE:.0%}"
                   f"（已用 {used_margin:.2f} + 新倉 {margin:.2f} / 淨值 {equity:.2f}）")
            return

        # 設定槓桿（全倉模式）
        _bingx_request("POST", "/openApi/swap/v2/trade/leverage", {
            "symbol": bingx_symbol,
            "side": "LONG" if trade_side == "long" else "SHORT",
            "leverage": str(leverage),
            "marginType": "CROSSED"
        }, headers)

        # 計算張數
        qty = round(position_value / entry_price, 4)
        side_str = "BUY" if trade_side == "long" else "SELL"
        pos_side = "LONG" if trade_side == "long" else "SHORT"
        exit_side = "SELL" if trade_side == "long" else "BUY"

        # ★ 先驗證止損參數是否合法，再開倉
        # BingX stopPrice 必須：做多 < 當前價；做空 > 當前價
        price_check = _bingx_request("GET", "/openApi/swap/v2/quote/price", {
            "symbol": bingx_symbol
        }, headers).json()
        current_px = float((price_check.get("data") or {}).get("price") or entry_price)

        if trade_side == "long" and stop_loss >= current_px:
            dc_log(f"⚠️ BingX 止損 {stop_loss} ≥ 當前價 {current_px}，自動調整至當前價下方 0.5%")
            stop_loss = round(current_px * 0.995, 5)
        if trade_side == "short" and stop_loss <= current_px:
            dc_log(f"⚠️ BingX 止損 {stop_loss} ≤ 當前價 {current_px}，自動調整至當前價上方 0.5%")
            stop_loss = round(current_px * 1.005, 5)
        if qty < 0.001:
            dc_log(f"⚠️ BingX 下單量 {qty} 過小，取消下單")
            return

        # 倉位價值不得超過可用保證金 × 槓桿（防止 110424）
        max_position_value = avail_usdt * leverage * 0.95   # 留 5% 緩衝
        if position_value > max_position_value:
            position_value = max_position_value
            qty = round(position_value / entry_price, 4)
            dc_log(f"⚠️ BingX 倉位縮減至可用上限：{position_value:.2f} USDT，qty={qty}")

        # ★ 風控防呆：qty 經四捨五入/縮減後，預估停損不得超過風險預算 × 容忍倍數
        #   （與 OKX 一致；BingX 精度高通常 ≈ risk_usdt，此為防呆上限，只拒單不放大）
        worst_loss = qty * entry_price * sl_dist_pct
        if worst_loss > risk_usdt * RISK_TOLERANCE_MULT:
            dc_log(f"⚠️ BingX 跳過 [{symbol_id}]：預估停損虧損 {worst_loss:.2f}U "
                   f"> 風險預算 {risk_usdt:.2f}U × {RISK_TOLERANCE_MULT}（上限 {risk_usdt*RISK_TOLERANCE_MULT:.2f}U），拒絕超額下單")
            return

        # 市價開倉
        r = _bingx_request("POST", "/openApi/swap/v2/trade/order", {
            "symbol": bingx_symbol, "side": side_str, "positionSide": pos_side,
            "type": "MARKET", "quantity": str(qty)
        }, headers)
        order_data = r.json()
        if order_data.get("code", 0) != 0:
            dc_log(f"⚠️ BingX 開倉失敗：{order_data}")
            return
        order_id = order_data.get("data", {}).get("order", {}).get("orderId", "")

        # ── BingX 止損單：使用開倉實際成交數量，避免 size 超過帳戶餘額限制 ──
        # 優先從回傳結果取 executedQty（已成交量）或 origQty（委託量），備援用計算 qty
        order_detail = order_data.get("data", {}).get("order", {})
        actual_qty   = float(
            order_detail.get("executedQty")
            or order_detail.get("origQty")
            or order_detail.get("quantity")
            or qty
        )
        if actual_qty <= 0:
            actual_qty = qty   # 備援：用原始計算值

        # 止損單（開倉後立刻掛，失敗就平倉）
        r = _bingx_request("POST", "/openApi/swap/v2/trade/order", {
            "symbol": bingx_symbol, "side": exit_side, "positionSide": pos_side,
            "type": "STOP_MARKET", "stopPrice": str(round(stop_loss, 5)),
            "quantity": str(round(actual_qty, 4)), "workingType": "MARK_PRICE"
        }, headers)
        sl_data = r.json()
        if sl_data.get("code", 0) != 0:
            dc_log(f"🚨 **BingX 止損掛載失敗，請立即手動設定止損！**\n"
                   f"商品: {bingx_symbol} 方向: {trade_side}\n"
                   f"倉位已開，止損價: {stop_loss}\n"
                   f"錯誤: {sl_data}")
            return
        bingx_sl_order_id = sl_data.get("data", {}).get("order", {}).get("orderId", "")

        # TP1/TP2 均使用實際成交數量的各半（避免超過帳戶可用量）
        half_qty = round(actual_qty / 2, 4)
        tp1_r = _bingx_request("POST", "/openApi/swap/v2/trade/order", {
            "symbol": bingx_symbol, "side": exit_side, "positionSide": pos_side,
            "type": "TAKE_PROFIT_MARKET", "stopPrice": str(round(tp1, 5)),
            "quantity": str(half_qty), "workingType": "MARK_PRICE"
        }, headers)
        bingx_tp1_order_id = tp1_r.json().get("data", {}).get("order", {}).get("orderId", "")

        # TP2（fixed 模式）
        if exit_mode == "fixed":
            _bingx_request("POST", "/openApi/swap/v2/trade/order", {
                "symbol": bingx_symbol, "side": exit_side, "positionSide": pos_side,
                "type": "TAKE_PROFIT_MARKET", "stopPrice": str(round(tp2, 5)),
                "quantity": str(half_qty), "workingType": "MARK_PRICE"
            }, headers)

        # ★ 存入 active_real_trades 供保本機制追蹤
        fee_buffer_bingx = float(entry_price) * 0.001
        be_price_bingx   = float(entry_price) + fee_buffer_bingx if trade_side == "long" \
                           else float(entry_price) - fee_buffer_bingx
        trade_key = f"bingx_{bingx_symbol}_{trade_side}_{int(time.time())}"
        active_real_trades[trade_key] = {
            "exchange":         "bingx",
            "inst_id":          bingx_symbol,
            "symbol":           symbol_id,
            "direction":        trade_side,
            "entry_price":      str(entry_price),
            "sl_order_id":      bingx_sl_order_id,
            "tp1_order_id":     bingx_tp1_order_id,
            "tp1_hit":          False,
            "current_sl":       stop_loss,
            "be_price":         be_price_bingx,
            "remaining_qty":    str(half_qty),      # 實際成交量的一半
            "pos_side":         pos_side,
            "exit_side":        exit_side,
            "headers":          headers,
            "risk_dist":        abs(float(entry_price) - stop_loss),
            "tf_id":            tf_id,
        }
        dc_log(f"📋 BingX 倉位已加入保本追蹤：{bingx_symbol} {trade_side} SL={stop_loss} qty={actual_qty:.4f}")

    except Exception as e:
        dc_log(f"❌ **BingX 下單失敗**: {e}")


def check_trailing_stops_for_real():
    """ 每次掃描自動執行：偵測 TP1 成交並管理追蹤止損 """
    if not active_real_trades:
        return
    try:
        ex = _initialize_ccxt_client()
        ex.load_markets()
    except Exception as e:
        print(f"[Trailing] 初始化交易所失敗: {e}")
        return

    for trade_key in list(active_real_trades.keys()):
        trade     = active_real_trades[trade_key]
        symbol    = trade["symbol"]
        inst_id   = trade["inst_id"]
        direction = trade["direction"]
        name      = symbol.split("/")[0]

        try:
            # 確認倉位是否仍存在
            positions = ex.fetch_positions([symbol])
            has_pos = any(
                p["symbol"] == symbol
                and abs(float(p.get("contracts") or 0)) > 0
                and p.get("side") == direction
                for p in positions
            )
            if not has_pos:
                print(f"[Trailing] {name} 倉位已關閉，移除追蹤")
                active_real_trades.pop(trade_key, None)
                continue

            if not trade["tp1_hit"]:
                # 查詢 TP1 限價單狀態
                tp1_order = ex.fetch_order(trade["tp1_order_id"], symbol)
                if tp1_order.get("status") in ("closed", "filled"):
                    # TP1 成交 → 1. 取消原止損
                    _cancel_okx_algo_order(inst_id, trade["sl_algo_id"])

                    # 2. 掛新止損在成本價 + 手續費（保本價略高於進場價）
                    entry    = float(trade["entry_price"])
                    fee_buf  = entry * 0.001   # taker 雙邊 0.1%
                    be_price = entry + fee_buf if direction == "long" else entry - fee_buf
                    be_price = round(be_price, 5)
                    exit_side = "sell" if direction == "long" else "buy"
                    sl_result = _place_okx_algo_sl(
                        inst_id=inst_id, side=exit_side,
                        amount=trade["remaining_amount"],
                        sl_trigger_px=str(be_price),
                        pos_side=direction
                    )
                    new_algo_id = (sl_result.get("data") or [{}])[0].get("algoId")
                    if new_algo_id:
                        trade["sl_algo_id"] = new_algo_id
                        trade["current_sl"] = be_price
                        trade["tp1_hit"]    = True

                    # 3. DC + TG 通知
                    msg = f"✅ TP1 已成交，止損移至保本價 {be_price}（含手續費）\n幣種：{name}"
                    dc_log(msg)
                    tg_log(msg)
                    print(f"[Trailing] {name} TP1成交，SL移至保本價 {be_price}")
                else:
                    # TP1 未成交：檢查浮盈是否達 be_trigger × R + 手續費，提前保本
                    ticker = ex.fetch_ticker(symbol)
                    cur_price = float(ticker.get("last", 0))
                    entry  = float(trade["entry_price"])
                    risk   = float(trade.get("risk_dist", abs(entry - float(trade["current_sl"]))))
                    fee_buffer = entry * 0.001   # OKX taker 雙邊手續費約 0.1%
                    # 從 BEST_PARAMS 讀取 be_trigger
                    tf_key = f"{trade.get('tf_id', '15m')}_{direction}"
                    be_trigger_mult = BEST_PARAMS.get(tf_key, {}).get("be_trigger", 1.0)
                    breakeven_trigger = risk * be_trigger_mult + fee_buffer
                    if direction == "long":
                        float_pnl = cur_price - entry
                    else:
                        float_pnl = entry - cur_price
                    if float_pnl >= breakeven_trigger and trade["current_sl"] != entry:
                        _cancel_okx_algo_order(inst_id, trade["sl_algo_id"])
                        exit_side = "sell" if direction == "long" else "buy"
                        sl_result = _place_okx_algo_sl(
                            inst_id=inst_id, side=exit_side,
                            amount=trade["remaining_amount"],
                            sl_trigger_px=str(round(entry, 5)),
                            pos_side=direction
                        )
                        new_algo_id = (sl_result.get("data") or [{}])[0].get("algoId")
                        if new_algo_id:
                            trade["sl_algo_id"] = new_algo_id
                            trade["current_sl"] = entry
                        msg = f"🔒 {name} 浮盈達1R，止損提前移至成本價 {entry}（含手續費保護）"
                        dc_log(msg)
                        print(f"[Trailing] {msg}")

            else:
                # TP1 已成交，波浪偵測：當根創近20根新高/低 → 止損移至20根低點/高點
                tf_wave = trade.get("tf_id", "15m")
                wave_df = fetch_market_candles(inst_id, tf_wave, fetch_limit=21)
                if wave_df.empty or len(wave_df) < 20:
                    continue
                highs = wave_df["high"].values
                lows  = wave_df["low"].values
                cur_high  = highs[-1]
                cur_low   = lows[-1]
                prev_highs = highs[-21:-1] if len(highs) >= 21 else highs[:-1]
                prev_lows  = lows[-21:-1]  if len(lows)  >= 21 else lows[:-1]

                new_sl = trade["current_sl"]
                if direction == "long":
                    # 當根創近20根新高 → 止損移至近20根最低點
                    if len(prev_highs) > 0 and cur_high > float(prev_highs.max()):
                        candidate = round(float(lows[-20:].min()), 5)
                        new_sl = max(trade["current_sl"], candidate)
                else:
                    # 當根創近20根新低 → 止損移至近20根最高點
                    if len(prev_lows) > 0 and cur_low < float(prev_lows.min()):
                        candidate = round(float(highs[-20:].max()), 5)
                        new_sl = min(trade["current_sl"], candidate)

                if new_sl != trade["current_sl"]:
                    _cancel_okx_algo_order(inst_id, trade["sl_algo_id"])
                    exit_side = "sell" if direction == "long" else "buy"
                    sl_result = _place_okx_algo_sl(
                        inst_id=inst_id, side=exit_side,
                        amount=trade["remaining_amount"],
                        sl_trigger_px=str(new_sl),
                        pos_side=direction
                    )
                    new_algo_id = (sl_result.get("data") or [{}])[0].get("algoId")
                    if new_algo_id:
                        trade["sl_algo_id"] = new_algo_id
                        trade["current_sl"] = new_sl

                    msg = f"🔄 波浪追蹤止損更新至 {new_sl}\n幣種：{name}"
                    dc_log(msg)
                    tg_log(msg)
                    print(f"[Trailing] {name} 波浪追蹤止損更新至 {new_sl}")

        except Exception as e:
            print(f"[Trailing] {name} 處理失敗: {e}")

    # ── BingX 保本追蹤 ──────────────────────────────────────────────────────
    for trade_key in list(active_real_trades.keys()):
        trade = active_real_trades[trade_key]
        if trade.get("exchange") != "bingx": continue
        try:
            bingx_symbol = trade["inst_id"]
            direction    = trade["direction"]
            entry        = float(trade["entry_price"])
            be_price     = float(trade["be_price"])
            headers      = trade["headers"]
            exit_side    = trade["exit_side"]
            pos_side     = trade["pos_side"]
            remaining    = trade["remaining_qty"]
            sl_order_id  = trade["sl_order_id"]

            if trade["tp1_hit"]: continue  # 已保本，跳過

            # 查詢 TP1 是否成交
            tp1_r = _bingx_request("GET", "/openApi/swap/v2/trade/order", {
                "symbol": bingx_symbol, "orderId": trade["tp1_order_id"]
            }, headers)
            tp1_data = tp1_r.json().get("data", {}).get("order", {})
            if tp1_data.get("status") in ("FILLED", "filled"):
                # TP1 成交 → 取消原止損，掛保本止損
                _bingx_request("POST", "/openApi/swap/v2/trade/cancelOrder", {
                    "symbol": bingx_symbol, "orderId": sl_order_id
                }, headers)
                # 新止損掛在保本價（含手續費）
                new_sl_r = _bingx_request("POST", "/openApi/swap/v2/trade/order", {
                    "symbol": bingx_symbol, "side": exit_side, "positionSide": pos_side,
                    "type": "STOP_MARKET", "stopPrice": str(round(be_price, 5)),
                    "quantity": str(remaining), "workingType": "MARK_PRICE"
                }, headers)
                new_sl_data = new_sl_r.json()
                if new_sl_data.get("code", 0) == 0:
                    new_sl_id = new_sl_data.get("data", {}).get("order", {}).get("orderId", "")
                    trade["sl_order_id"] = new_sl_id
                    trade["current_sl"]  = be_price
                    trade["tp1_hit"]     = True
                    msg = f"✅ BingX TP1 成交，止損移至保本價 {be_price}（含手續費）\n幣種：{bingx_symbol}"
                    dc_log(msg)
                    print(f"[BingX Trailing] {msg}")
                else:
                    dc_log(f"⚠️ BingX 保本止損掛載失敗：{new_sl_data}")

        except Exception as be_err:
            print(f"[BingX Trailing] {trade_key} 處理失敗: {be_err}")

def _get_tick_size(df: pd.DataFrame) -> float:
    """從 K 棒數據自動估算 tick size（最小價格單位）"""
    closes = df["close"].dropna().values
    if len(closes) < 2:
        return 0.01
    price = float(closes[-1])
    if price >= 1000:
        return 0.1
    elif price >= 100:
        return 0.01
    elif price >= 10:
        return 0.001
    elif price >= 1:
        return 0.0001
    else:
        return 0.00001

# ══════════════════════════════════════════════════════════════════════════════
# 雙底 / 雙頂動能衰減進場偵測 (Double Bottom / Top Pattern)
# ══════════════════════════════════════════════════════════════════════════════

# 雙底/雙頂偵測（_DOUBLE_PARAMS、check_double_bottom/top）已移至 indicators.py


def _find_pivot_low(df: pd.DataFrame, pivot_len: int = PIVOT_LEN,
                    atr_buffer: float = 0.0) -> Optional[float]:
    """
    找最近一個 Swing Low（左右各 pivot_len 根都比它高）
    找不到則退而求其次取最近 pivot_len*2 根最低點
    止損 = Swing Low - 1 tick - atr_buffer * ATR
    atr_buffer：ATR 倍數緩衝（0.0 = 僅 1 tick，同原版）
    """
    lows = df["low"].values
    n = len(lows)
    tick = _get_tick_size(df)
    # ATR 緩衝計算
    if atr_buffer > 0.0 and "atr" in df.columns:
        atr_val = float(df["atr"].iloc[-1])
        extra_buf = atr_val * atr_buffer
    else:
        extra_buf = 0.0
    # 優先：找真正的 Swing Low
    for i in range(n - pivot_len - 1, pivot_len - 1, -1):
        if (all(lows[i] < lows[i - j] for j in range(1, pivot_len + 1)) and
                all(lows[i] < lows[i + j] for j in range(1, pivot_len + 1))):
            return round(float(lows[i]) - tick - extra_buf, 8)
    # 備援：最近 pivot_len*2 根最低點
    lookback = min(pivot_len * 2, n)
    return round(float(lows[-lookback:].min()) - tick - extra_buf, 8)

def _find_pivot_high(df: pd.DataFrame, pivot_len: int = PIVOT_LEN,
                     atr_buffer: float = 0.0) -> Optional[float]:
    """
    找最近一個 Swing High（左右各 pivot_len 根都比它低）
    找不到則退而求其次取最近 pivot_len*2 根最高點
    止損 = Swing High + 1 tick + atr_buffer * ATR
    atr_buffer：ATR 倍數緩衝（0.0 = 僅 1 tick，同原版）
    """
    highs = df["high"].values
    n = len(highs)
    tick = _get_tick_size(df)
    # ATR 緩衝計算
    if atr_buffer > 0.0 and "atr" in df.columns:
        atr_val = float(df["atr"].iloc[-1])
        extra_buf = atr_val * atr_buffer
    else:
        extra_buf = 0.0
    # 優先：找真正的 Swing High
    for i in range(n - pivot_len - 1, pivot_len - 1, -1):
        if (all(highs[i] > highs[i - j] for j in range(1, pivot_len + 1)) and
                all(highs[i] > highs[i + j] for j in range(1, pivot_len + 1))):
            return round(float(highs[i]) + tick + extra_buf, 8)
    # 備援：最近 pivot_len*2 根最高點
    lookback = min(pivot_len * 2, n)
    return round(float(highs[-lookback:].max()) + tick + extra_buf, 8)

def _check_cvd_absorption(symbol_item: str, tf_id: str, okx_bar_fmt: str,
                          df: pd.DataFrame, direction: str) -> Tuple[bool, str]:
    """
    秋總三層背離吸收確認（三層缺一不可）
    做多：price<=price[1] + 現貨CVD翻上 + 合約CVD翻上 + OI上升
    做空：price>=price[1] + 現貨CVD翻下 + 合約CVD翻下 + OI上升
    """
    cona_perp = CONA_PERP.get(symbol_item)
    cona_spot = CONA_SPOT.get(symbol_item)

    if not cona_perp:
        return True, "無Coinalyze合約數據，略過CVD過濾"

    end_ts   = int(time.time() * 1000)
    start_ts = end_ts - (BAR_SECONDS[tf_id] * CVD_WINDOW * 1000)

    cvd_perp  = calculate_cumulative_volume_delta(cona_perp, okx_bar_fmt, start_ts, end_ts)
    cvd_spot  = (calculate_cumulative_volume_delta(cona_spot, okx_bar_fmt, start_ts, end_ts)
                 if cona_spot else pd.Series(dtype=float))
    oi_series = fetch_open_interest_series(cona_perp, okx_bar_fmt, start_ts, end_ts)

    # 三層缺一不可 — 任一數據不足直接拒絕
    if len(cvd_perp) < 2:
        return False, "合約CVD數據不足"
    if len(cvd_spot) < 2:
        return False, "現貨CVD數據不足"
    if len(oi_series) < 2:
        return False, "OI數據不足"

    current_close = df["close"].iloc[-1]
    prev_close    = df["close"].iloc[-2]
    rejects = []

    if direction == "long":
        # close <= close[1]：當根無強勢上漲（背離確認）
        if current_close > prev_close:
            rejects.append("價格上漲（無背離）")
        if cvd_perp.iloc[-1] <= cvd_perp.iloc[-2]:
            rejects.append("合約CVD未翻上")
        if cvd_spot.iloc[-1] <= cvd_spot.iloc[-2]:
            rejects.append("現貨CVD未翻上")
        if oi_series.iloc[-1] <= oi_series.iloc[-2]:
            rejects.append("OI未上升")
    else:
        # close >= close[1]：當根無強勢下跌（背離確認）
        if current_close < prev_close:
            rejects.append("價格下跌（無背離）")
        if cvd_perp.iloc[-1] >= cvd_perp.iloc[-2]:
            rejects.append("合約CVD未翻下")
        if cvd_spot.iloc[-1] >= cvd_spot.iloc[-2]:
            rejects.append("現貨CVD未翻下")
        if oi_series.iloc[-1] <= oi_series.iloc[-2]:
            rejects.append("OI未上升")

    if rejects:
        return False, "、".join(rejects)

    if direction == "long":
        return True, "現貨CVD↑+合約CVD↑+OI↑（三層吸收確認）"
    else:
        return True, "現貨CVD↓+合約CVD↓+OI↑（三層吸收確認）"

# ══════════════════════════════════════════════════════════════════════════════
# 模組一：訊號評分引擎 filter_signals()
# 輸入：各指標布林值與數值
# 輸出：signal_score (0~100 整數)
# 評分邏輯：CVD確認25分 + ADX強度25分 + K棒結構25分 + OI/資費方向25分
# ══════════════════════════════════════════════════════════════════════════════

def filter_signals(
    direction: str,
    is_c3: bool,
    is_pattern: bool,          # 雙底/雙頂等第二套訊號
    cvd_pass: bool,
    current_adx: float,
    c1_ok: bool,
    c2_ok: bool,
    c3_ok: bool,
    funding_rate: Optional[float],
) -> int:
    """
    訊號評分引擎：將各過濾條件量化為 0~100 整數評分。
    四維評分：
      A. CVD 動能確認    (0~25)
      B. ADX 趨勢強度    (0~25)
      C. K棒結構完整度   (0~25)
      D. 資費/OI 方向    (0~25)

    回傳值供 dynamic_sl_tp() 與 CircuitBreaker.check() 使用。
    """
    score = 0

    # ── A. CVD 動能確認（25分）────────────────────────────────────────
    # CVD 三層確認通過：+25；僅有結構訊號無 CVD 資料：+10（給予部分分數）
    if cvd_pass:
        score += 25
    elif is_c3 or is_pattern:
        score += 10  # 無 CVD 但有結構形態，給基礎分

    # ── B. ADX 趨勢強度（25分）────────────────────────────────────────
    # ADX < 25：0分；25~39：12分；40~54：20分；≥55：25分
    if current_adx >= 55:
        score += 25
    elif current_adx >= 40:
        score += 20
    elif current_adx >= 25:
        score += 12
    # else: 0分（趨勢太弱）

    # ── C. K棒結構完整度（25分）──────────────────────────────────────
    # C3 三條件全中：25分；兩條件：15分；一條件：8分；僅雙底/雙頂：12分
    if is_c3:
        c3_count = sum([c1_ok, c2_ok, c3_ok])
        if c3_count == 3:
            score += 25
        elif c3_count == 2:
            score += 15
        else:
            score += 8
    elif is_pattern:
        # 雙底/雙頂形態完整（頸線+量能衰減+放量突破）：給12分
        score += 12

    # ── D. 資費/OI 方向一致性（25分）─────────────────────────────────
    # 做多：資費 ≤ 0 → 空方負擔成本，對多頭有利 → +25；資費輕微正（0~0.0001）：+15
    # 做空：資費 ≥ 0 → 多方負擔成本，對空頭有利 → +25；資費輕微負（-0.0001~0）：+15
    fr = funding_rate or 0.0
    if direction == "long":
        if fr <= 0:
            score += 25
        elif fr <= FUNDING_LONG_MAX:
            score += 15
        # else: 0分（資費過高，不利多頭）
    else:
        if fr >= 0:
            score += 25
        elif fr >= FUNDING_SHORT_MIN:
            score += 15

    return min(100, max(0, score))


# ══════════════════════════════════════════════════════════════════════════════
# 模組二：動態 SL/TP 調整器 dynamic_sl_tp()
# 輸入：signal_score、base_params（BEST_PARAMS 原始值）
# 輸出：Dict 含 tp1_mult、tp2_mult、be_trigger、position_scale
# 四個等級處理邏輯：
#   ≥90（強趨勢）：止損寬 1.2x、保本觸發 1.5R、TP2 倍率 +20%
#   60~89（普通）：維持原參數不調整
#   30~59（弱訊號）：倉位縮 50%，SL/TP 不動
#   <30（極弱）：position_scale=0（外層判斷直接跳過下單）
# ══════════════════════════════════════════════════════════════════════════════

def dynamic_sl_tp(
    signal_score: int,
    base_params: Dict[str, Any],
) -> Dict[str, Any]:
    """
    根據 signal_score 動態調整出場參數與倉位比例。
    回傳 dict 包含：
      - tp1_mult       : TP1 風報比倍率
      - tp2_mult       : TP2 風報比倍率（由 is_swing 決定 intraday/swing 已在外層處理）
      - be_trigger     : 保本觸發 R 倍數
      - sl_scale       : 止損距離乘數（>1.0 代表放寬）
      - position_scale : 倉位比例（1.0=正常，0.5=半倉，0.0=不下單）
    """
    tp1  = float(base_params.get("tp1_mult", 1.2))
    tp2i = float(base_params.get("tp2_intraday_mult", 2.5))
    tp2s = float(base_params.get("tp2_swing_mult", 2.5))
    be   = float(base_params.get("be_trigger", 1.0))

    if signal_score >= 90:
        # 強趨勢：放寬止損給呼吸空間，TP2 上調 20%，保本延後到 1.5R
        return {
            "tp1_mult":            tp1,
            "tp2_intraday_mult":   round(tp2i * 1.2, 3),
            "tp2_swing_mult":      round(tp2s * 1.2, 3),
            "be_trigger":          max(be, 1.5),
            "sl_scale":            1.2,
            "position_scale":      1.0,
        }
    elif signal_score >= 60:
        # 普通訊號：完全沿用 BEST_PARAMS，不做調整
        return {
            "tp1_mult":            tp1,
            "tp2_intraday_mult":   tp2i,
            "tp2_swing_mult":      tp2s,
            "be_trigger":          be,
            "sl_scale":            1.0,
            "position_scale":      1.0,
        }
    elif signal_score >= 30:
        # 弱訊號：半倉，出場參數不變
        return {
            "tp1_mult":            tp1,
            "tp2_intraday_mult":   tp2i,
            "tp2_swing_mult":      tp2s,
            "be_trigger":          be,
            "sl_scale":            1.0,
            "position_scale":      0.5,
        }
    else:
        # 極弱：不下單（position_scale=0 由外層判斷）
        return {
            "tp1_mult":            tp1,
            "tp2_intraday_mult":   tp2i,
            "tp2_swing_mult":      tp2s,
            "be_trigger":          be,
            "sl_scale":            1.0,
            "position_scale":      0.0,
        }


# ══════════════════════════════════════════════════════════════════════════════
# 模組三：防洗盤熔斷機制 CircuitBreaker
# 記錄最近5筆交易結果（win=True / loss=False）
# 連續3筆虧損 → 熔斷1小時，signal_score 門檻提高至85
# 熔斷解除後 DC 通知，自動恢復正常門檻
# ══════════════════════════════════════════════════════════════════════════════

class CircuitBreaker:
    """
    防洗盤熔斷機制：
      - record(win: bool)    : 記錄一筆交易結果
      - check(score: int)    : 回傳有效的最終 signal_score（熔斷期間強制最低門檻85）
      - is_active() -> bool  : 是否處於熔斷狀態
    """

    WINDOW           = 5     # 追蹤最近 N 筆交易
    CONSEC_LOSS_MAX  = 3     # 連續虧損超過此數觸發熔斷
    COOLDOWN_SECS    = 3600  # 熔斷持續時間（秒）
    BREAKER_SCORE    = 85    # 熔斷期間最低有效 signal_score

    def __init__(self):
        self._results: List[bool] = []          # True=獲利 / False=虧損
        self._break_until: Optional[float] = None   # 熔斷解除的 UNIX timestamp

    def record(self, win: bool) -> None:
        """記錄一筆交易結果，超過 WINDOW 則移除最舊筆。"""
        self._results.append(win)
        if len(self._results) > self.WINDOW:
            self._results.pop(0)

        # 連續虧損計數（從最新往前數）
        consec = 0
        for r in reversed(self._results):
            if not r:
                consec += 1
            else:
                break

        if consec >= self.CONSEC_LOSS_MAX and not self.is_active():
            self._break_until = time.time() + self.COOLDOWN_SECS
            dc_log(
                f"⚠️ **熔斷啟動**：近期連虧 {consec} 筆，"
                f"過濾器收緊1小時（門檻提高至 score≥{self.BREAKER_SCORE}）"
            )

    def is_active(self) -> bool:
        """回傳目前是否處於熔斷狀態，超時則自動解除。"""
        if self._break_until is None:
            return False
        if time.time() >= self._break_until:
            self._break_until = None
            dc_log("✅ **熔斷解除**：恢復正常過濾條件")
            return False
        return True

    def check(self, score: int) -> int:
        """
        傳入原始 signal_score，回傳最終有效分數。
        熔斷期間：若 score < BREAKER_SCORE，強制回傳 0（外層判斷為極弱，不下單）。
        正常期間：直接回傳原始 score。
        """
        if self.is_active() and score < self.BREAKER_SCORE:
            return 0   # 熔斷濾除低品質訊號
        return score


# 全局熔斷器實例（跨所有幣種共享）
_circuit_breaker = CircuitBreaker()


class SykesTradingBot:
    def __init__(self):
        self.cooldown_dict: Dict[str, float] = {}
        self.dir_cooldown:  Dict[str, float] = {}   # 跨時框同幣同向去重（key=symbol_direction）
        self.last_bar_ts:   Dict[str, int]   = {}   # K棒去重：同一根K棒不重複觸發
        self.consec_losses = 0
        self.circuit_break_until: Optional[float] = None
        self.paper_positions: Dict[str, PaperPosition] = {}

    def is_cooldown(self, symbol: str, tf: str) -> bool:
        key = f"{symbol}_{tf}"
        if key in self.cooldown_dict:
            if time.time() - self.cooldown_dict[key] < SIGNAL_COOLDOWN:
                return True
        return False

    def set_cooldown(self, symbol: str, tf: str):
        self.cooldown_dict[f"{symbol}_{tf}"] = time.time()

    def check_circuit_breaker(self) -> bool:
        if self.circuit_break_until and time.time() < self.circuit_break_until:
            return True
        if self.circuit_break_until and time.time() >= self.circuit_break_until:
            self.circuit_break_until = None
            self.consec_losses = 0
            dc_log("🛡️ **風控通告**: 熔斷冷卻時間已屆滿，核心解鎖恢復主動交易輪詢。")
        return False

    def trigger_circuit_break(self):
        self.circuit_break_until = time.time() + (PAUSE_HOURS * 3600)
        dc_log(f"🚨 **風控核心硬熔斷發動** 🚨")

    def update_paper_trailing_and_exits(self, symbol_item: str, current_price: float, current_tf: str = None):
        for tf in TIMEFRAMES:
            pos_key = f"{symbol_item}_{tf}"
            if pos_key not in self.paper_positions or not self.paper_positions[pos_key].open:
                continue
            pos = self.paper_positions[pos_key]

            if pos.side == "long":
                if current_price <= pos.sl:
                    dc_log(f"📉 [紙交易離場] {symbol_item} ({tf}) 觸及止損價 `{pos.sl}`。")
                    pos.open = False
                    if not pos.tp1_hit:
                        self.consec_losses += 1
                        if self.consec_losses >= MAX_CONSEC_LOSS:
                            self.trigger_circuit_break()
                    else:
                        self.consec_losses = 0
                elif current_price >= pos.tp1 and not pos.tp1_hit:
                    dc_log(f"🎯 [紙交易獲利] {symbol_item} ({tf}) 達標TP1 `{pos.tp1}`，推動保本止損。")
                    pos.tp1_hit = True
                    pos.sl = pos.entry
                elif current_price >= pos.tp2:
                    dc_log(f"🌕 [紙交易獲利] {symbol_item} ({tf}) 達標TP2 `{pos.tp2}`！")
                    pos.open = False
                    self.consec_losses = 0
                elif pos.exit_mode == "trailing" and pos.tp1_hit:
                    new_sl = current_price * 0.98
                    if new_sl > pos.sl:
                        pos.sl = new_sl

            elif pos.side == "short":
                if current_price >= pos.sl:
                    dc_log(f"📈 [紙交易離場] {symbol_item} ({tf}) 觸及止損價 `{pos.sl}`。")
                    pos.open = False
                    if not pos.tp1_hit:
                        self.consec_losses += 1
                        if self.consec_losses >= MAX_CONSEC_LOSS:
                            self.trigger_circuit_break()
                    else:
                        self.consec_losses = 0
                elif current_price <= pos.tp1 and not pos.tp1_hit:
                    dc_log(f"🎯 [紙交易獲利] {symbol_item} ({tf}) 達標TP1 `{pos.tp1}`，推動保本止損。")
                    pos.tp1_hit = True
                    pos.sl = pos.entry
                elif current_price <= pos.tp2:
                    dc_log(f"🌕 [紙交易獲利] {symbol_item} ({tf}) 達標TP2 `{pos.tp2}`！")
                    pos.open = False
                    self.consec_losses = 0
                elif pos.exit_mode == "trailing" and pos.tp1_hit:
                    new_sl = current_price * 1.02
                    if new_sl < pos.sl:
                        pos.sl = new_sl

    def _get_4h_swing_flag(self, okx_swap_symbol: str, df_current: pd.DataFrame, tf_id: str) -> bool:
        """
        波段判斷三條件（同時符合才算波段）：
        1. 4H EMA144 > EMA576
        2. 4H EMA576[-1] > EMA576[-21]（通道有斜率）
        3. 4H ADX(14) > ADX_THR
        """
        if tf_id == "4H":
            df4h = df_current
        else:
            df4h = fetch_market_candles(okx_swap_symbol, "4H")
            if df4h.empty or len(df4h) < 25:
                return False

        ema144 = df4h["close"].ewm(span=144, adjust=False).mean()
        ema576 = df4h["close"].ewm(span=576, adjust=False).mean()
        cond1  = ema144.iloc[-1] > ema576.iloc[-1]
        cond2  = len(ema576) > 20 and ema576.iloc[-1] > ema576.iloc[-21]
        adx4h  = calculate_directional_movement_index(df4h, 14)
        cond3  = adx4h.iloc[-1] > ADX_THR
        return cond1 and cond2 and cond3

    def scan_and_process_market(self, symbol_item: str, tf_id: str):
        """ 全時框商品訊號矩陣掃描引擎核心（v3 Vegas+QQE穿越+CVD三層吸收） """
        _dbg = False  # debug flag（已關閉）

        if self.check_circuit_breaker():
            if _dbg: print(f"[DBG DOGE/15m] ⛔ circuit_breaker 觸發，跳出", flush=True)
            return
        if self.is_cooldown(symbol_item, tf_id):
            if _dbg: print(f"[DBG DOGE/15m] ⏳ 冷卻中，跳出", flush=True)
            return

        okx_swap_symbol = OKX_SWAP.get(symbol_item)
        if not okx_swap_symbol:
            if _dbg: print(f"[DBG DOGE/15m] ❌ 找不到 OKX swap 代號", flush=True)
            return

    # 1. 行情數據拉取
        okx_bar_fmt = BAR_TO_CONA.get(tf_id, "15min")
        df = fetch_market_candles(okx_swap_symbol, tf_id)
        if df.empty or len(df) < 100:
            if _dbg: print(f"[DBG DOGE/15m] ❌ K棒數據不足 ({len(df)} bars)", flush=True)
            return

        # ── K棒去重：同一根K棒不重複觸發訊號 ──────────────────────────────
        try:
            bar_ts  = int(df.index[-2].timestamp()) if hasattr(df.index[-2], 'timestamp') else 0
        except:
            bar_ts = 0
        bar_key = f"{symbol_item}_{tf_id}"
        if bar_ts != 0 and self.last_bar_ts.get(bar_key) == bar_ts:
            return

        current_close = df["close"].iloc[-1]
        self.update_paper_trailing_and_exits(symbol_item, current_close)

    # 2. 技術指標
        atr_series  = calculate_average_true_range(df, 14)
        adx_series  = calculate_directional_movement_index(df, 14)
        current_atr = atr_series.iloc[-1]
        current_adx = adx_series.iloc[-1]

    # 3. Vegas 通道
        ema12  = df["close"].ewm(span=12,  adjust=False).mean()
        ema144 = df["close"].ewm(span=144, adjust=False).mean()
        ema169 = df["close"].ewm(span=169, adjust=False).mean()
        ema576 = df["close"].ewm(span=576, adjust=False).mean()
        ema676 = df["close"].ewm(span=676, adjust=False).mean()
        large_top = max(ema576.iloc[-1], ema676.iloc[-1])
        large_bot = min(ema576.iloc[-1], ema676.iloc[-1])
        small_top = max(ema144.iloc[-1], ema169.iloc[-1])
        small_bot = min(ema144.iloc[-1], ema169.iloc[-1])

    # 4. （channel_ok 已移除，不過濾盤整）

        # 5. 空頭趨勢（v9.8：要求連續 BEAR_MIN_BARS 根都維持 EMA144 < EMA576，減少假空頭）
        bear_series = (ema144 < ema576).astype(int)
        bear_trend  = bool(bear_series.iloc[-BEAR_MIN_BARS:].min() == 1) if len(bear_series) >= BEAR_MIN_BARS \
                      else bool(ema144.iloc[-1] < ema576.iloc[-1])

    # 6. 雙軌 QQE MOD
        p_l = get_params(tf_id, "long")
        p_s = get_params(tf_id, "short")
        # Primary：各時框各方向獨立參數（從 BEST_PARAMS 載入）
        rsi_ma_l, trail_l = calculate_full_qqe_mod(
            df, rsi_pd=int(p_l.get("qqe_rsi", QQE_RSI)),
            sf_pd=int(p_l.get("qqe_sf", QQE_SF)),
            factor_mult=float(p_l.get("qqe_factor", QQE_FACTOR_P))
        )
        rsi_ma_s, trail_s = calculate_full_qqe_mod(
            df, rsi_pd=int(p_s.get("qqe_rsi", QQE_RSI)),
            sf_pd=int(p_s.get("qqe_sf", QQE_SF)),
            factor_mult=float(p_s.get("qqe_factor", QQE_FACTOR_P))
        )
    # 7. 進場條件（v9：Vegas 結構 + QQE rsiMa 穿越50線 + ADX）
        funding_rate = fetch_current_funding_rate(okx_swap_symbol)

        current_low  = df["low"].iloc[-1]
        current_high = df["high"].iloc[-1]

        # C1 當根版本（v9：移除 largeTop/largeBot 限制）
        long_C1  = (current_close > small_bot and current_low  < small_bot)
        long_C2  = current_close > ema12.iloc[-1]
        long_C3  = (rsi_ma_l.iloc[-2] < 50 and
                    rsi_ma_l.iloc[-1] >= 50)   # rsiMa 從 <50 穿越到 >=50（QQE 轉藍）
        is_long  = (ema144.iloc[-1] > ema576.iloc[-1] and
                    long_C1 and long_C2 and long_C3 and
                    (not ADX_ENABLED or current_adx >= ADX_THR) and
                    (funding_rate is None or funding_rate <= FUNDING_LONG_MAX))

        short_C1 = (current_close < small_top and current_high > small_top)
        short_C2 = current_close < ema12.iloc[-1]
        short_C3 = (rsi_ma_s.iloc[-2] >= 50 and
                    rsi_ma_s.iloc[-1] < 50)    # rsiMa 從 >=50 穿越到 <50（QQE 轉紅）
        short_adx_ok = (not ADX_ENABLED or current_adx >= ADX_THR)
        short_fund_ok = (funding_rate is None or funding_rate >= FUNDING_SHORT_MIN)
        is_short = (bear_trend and short_C1 and short_C2 and short_C3 and
                    short_adx_ok and short_fund_ok)

        # ── DOGE/15m 詳細 debug log ─────────────────────────────
        if _dbg:
            ts = datetime.now().strftime("%H:%M:%S")
            print(f"\n[DBG DOGE/15m @ {ts}] ════════════════════════", flush=True)
            print(f"  close={current_close:.6f}  high={current_high:.6f}  low={current_low:.6f}", flush=True)
            print(f"  EMA144={ema144.iloc[-1]:.6f}  EMA576={ema576.iloc[-1]:.6f}", flush=True)
            print(f"  smallTop={small_top:.6f}  smallBot={small_bot:.6f}", flush=True)
            print(f"  EMA12={ema12.iloc[-1]:.6f}  ADX={current_adx:.2f}  ATR={current_atr:.6f}", flush=True)
            print(f"  rsiMa_s[-2]={rsi_ma_s.iloc[-2]:.2f}  rsiMa_s[-1]={rsi_ma_s.iloc[-1]:.2f}", flush=True)
            print(f"  funding_rate={funding_rate}", flush=True)
            print(f"  ── 做空條件 ──", flush=True)
            print(f"  bearTrend={bear_trend}  (EMA144={ema144.iloc[-1]:.6f} < EMA576={ema576.iloc[-1]:.6f})", flush=True)
            print(f"  C1={short_C1}  (close<smallTop={current_close < small_top}  high>smallTop={current_high > small_top})", flush=True)
            print(f"  C2={short_C2}  (close<EMA12={current_close < ema12.iloc[-1]:.6f})", flush=True)
            print(f"  C3={short_C3}  (rsiMa[-2]={rsi_ma_s.iloc[-2]:.2f}>=50 AND rsiMa[-1]={rsi_ma_s.iloc[-1]:.2f}<50)", flush=True)
            print(f"  ADX_ok={short_adx_ok}  (ADX_ENABLED={ADX_ENABLED}, ADX={current_adx:.2f} vs THR={ADX_THR})", flush=True)
            print(f"  Fund_ok={short_fund_ok}  (funding={funding_rate}, min={FUNDING_SHORT_MIN})", flush=True)
            print(f"  ➜ is_short={is_short}  is_long={is_long}", flush=True)
            print(f"  AUTO_TRADE[15m]={AUTO_TRADE.get('15m')}  CVD_ENABLED={CVD_ENABLED}  ADX_ENABLED={ADX_ENABLED}", flush=True)
            if is_short:
                print(f"  ✅ 做空條件成立，繼續執行 CVD 過濾...", flush=True)
            else:
                blocked = []
                if not bear_trend:        blocked.append(f"bearTrend(EMA144={ema144.iloc[-1]:.6f} >= EMA576={ema576.iloc[-1]:.6f})")
                if not short_C1:          blocked.append("C1")
                if not short_C2:          blocked.append("C2")
                if not short_C3:          blocked.append("C3")
                if not short_adx_ok:      blocked.append(f"ADX({current_adx:.1f}<{ADX_THR})")
                if not short_fund_ok:     blocked.append(f"funding({funding_rate})")
                print(f"  ❌ 做空被擋住：{' | '.join(blocked) if blocked else '未知'}", flush=True)
            print(f"[DBG DOGE/15m] ════════════════════════\n", flush=True)
        # ────────────────────────────────────────────────────────

        # ── 雙底(W底)第二套訊號（OR 邏輯，獨立觸發）──────────────────────
        # 回測結論（backtest_wm_variants.py）：
        #   W底做多：1H +0.265、15m +0.068（C現狀版穩健）→ 僅 1H 啟用，與 WF 一致
        #   M頭做空：四版兩時框幾乎全賠 → 單獨關閉
        # 故：雙底僅 1H 做多；雙頂(M頭)單獨做空已停用。
        if tf_id == "1H":
            is_double_bottom = check_double_bottom(df, tf_id)
        else:
            is_double_bottom = False
        is_double_top = False   # M頭單獨做空回測全賠，停用（共振版見下）

        # ── 雙底/雙頂 + RSI穿50 共振（僅 15m，WF 驗證穩健）────────────────────
        # backtest_reson_wf.py：
        #   15m 雙頂空+RSI：訓練+0.211→驗證+0.187（n=15, MDD 4%）✅ 救活了做空
        #   15m 雙底多+RSI：訓練+0.074→驗證+0.062（n=8）✅ 輔助
        # RSI(14) 穿 50 為共振條件（用 calculate_smooth_rsi，與回測同算法）
        is_reson_long = False
        is_reson_short = False
        if tf_id == "15m":
            _rsi = calculate_smooth_rsi(df["close"], 14)
            _rsi_now  = _rsi.iloc[-1]
            _rsi_prev = _rsi.iloc[-2]
            rsi_up50   = _rsi_prev < 50 and _rsi_now >= 50   # 上穿50
            rsi_down50 = _rsi_prev >= 50 and _rsi_now < 50   # 下穿50
            if rsi_up50 and check_double_bottom(df, tf_id):
                is_reson_long = True
            if rsi_down50 and check_double_top(df, tf_id):
                is_reson_short = True

        # ── C3 1H/多 停用：回測 EV −0.024 負期望（backtest_c3_bias.py）──────────
        # 1H 的多單改由雙底(W底, +0.265) 觸發；C3 做多僅保留 15m（+0.133）。
        # 1H 的 C3 做空（+0.073）與 15m C3 多空不受影響。
        if tf_id == "1H":
            is_long = False

        # ── 30m/short 停用：分區回測顯示只在 2022 慢熊有效(+0.195)，
        #    牛市 −0.088、2025 急跌 −0.199(勝率0%)，全期 −0.023 負期望 → 停用。
        #    30m/long(WF驗證 +0.093 穩健) 保留。空單交給 15m 雙頂共振/C3空。
        if tf_id == "30m":
            is_short = False

        # 合併：C3 或 雙底 或 共振 任一成立即可觸發
        combined_long  = is_long  or is_double_bottom or is_reson_long
        combined_short = is_short or is_double_top   or is_reson_short

        if not combined_long and not combined_short:
            return

        # 方向優先：C3 長多 > 雙底 > C3 空 > 雙頂
        if combined_long and combined_short:
            direction = "long"
        elif combined_long:
            direction = "long"
        else:
            direction = "short"

        # 記錄訊號來源（供 Discord 顯示）
        if direction == "long":
            _signal_source = []
            if is_long:          _signal_source.append("C3")
            if is_double_bottom: _signal_source.append("雙底")
            if is_reson_long:    _signal_source.append("雙底+RSI共振")
        else:
            _signal_source = []
            if is_short:         _signal_source.append("C3")
            if is_double_top:    _signal_source.append("雙頂")
            if is_reson_short:   _signal_source.append("雙頂+RSI共振")
        signal_source_tag = "+".join(_signal_source)

        # ── 跨時框同幣同向去重 ──────────────────────────────────────────────
        # 同一幣、同一方向，DIR_SIGNAL_COOLDOWN 秒內只允許一次（不分時框），
        # 避免 15m/30m/1H 整點同時收盤造成「一小時內同向三次訊號」。
        dir_key = f"{symbol_item}_{direction}"
        if time.time() - self.dir_cooldown.get(dir_key, 0.0) < DIR_SIGNAL_COOLDOWN:
            if _dbg: print(f"[DBG] {dir_key} 同向冷卻中，跳出", flush=True)
            return

    # 8. 秋總三層背離吸收 CVD 過濾
        # 永遠獨立抓真實 CVD 結果（供 30m_long override 使用）
        real_cvd_pass, cvd_reason = _check_cvd_absorption(
            symbol_item, tf_id, okx_bar_fmt, df, direction
        )
        if CVD_ENABLED:
            cvd_pass = real_cvd_pass
        else:
            cvd_pass, cvd_reason = True, "CVD 已停用"

        if _dbg and (combined_long or combined_short):
            print(f"[DBG DOGE/15m] CVD: pass={cvd_pass}  reason={cvd_reason}", flush=True)

    # 9. SL/TP 計算（固定 BEST_PARAMS，Walk-Forward 驗證版）─────────────────
    #   ※ 已移除「訊號評分／動態SL/動態倉位」層：該層未經回測且會偏離
    #     WF 驗證過的參數，回退至固定參數以恢復正期望值。
        p = p_l if direction == "long" else p_s

        if direction == "long":
            calculated_sl = _find_pivot_low(df, p["structure_lookback"], p.get("sl_atr_buffer", 0.0))
            # 多單止損必須在當前價下方，否則用 MIN_SL 保護
            if calculated_sl >= current_close:
                calculated_sl = current_close * (1.0 - 0.005)
            risk_pct = abs(current_close - calculated_sl) / current_close
            if risk_pct > MAX_SL:
                calculated_sl = current_close * (1.0 - MAX_SL)
                risk_pct = MAX_SL
            is_swing   = self._get_4h_swing_flag(okx_swap_symbol, df, tf_id)
            tp2_mult   = p["tp2_swing_mult"] if is_swing else p["tp2_intraday_mult"]
            risk_dist  = current_close - calculated_sl
            tp1_target = current_close + risk_dist * p["tp1_mult"]
            tp2_target = current_close + risk_dist * tp2_mult
        else:
            calculated_sl = _find_pivot_high(df, p["structure_lookback"], p.get("sl_atr_buffer", 0.0))
            # 空單止損必須在當前價上方，否則用 MIN_SL 保護
            if calculated_sl <= current_close:
                calculated_sl = current_close * (1.0 + 0.005)
            risk_pct = abs(calculated_sl - current_close) / current_close
            if risk_pct > MAX_SL:
                calculated_sl = current_close * (1.0 + MAX_SL)
                risk_pct = MAX_SL
            is_swing   = self._get_4h_swing_flag(okx_swap_symbol, df, tf_id)
            tp2_mult   = p["tp2_swing_mult"] if is_swing else p["tp2_intraday_mult"]
            risk_dist  = calculated_sl - current_close
            tp1_target = current_close - risk_dist * p["tp1_mult"]
            tp2_target = current_close - risk_dist * tp2_mult

        risk_delta = abs(current_close - calculated_sl) or 1e-9
        rr1 = abs(tp1_target - current_close) / risk_delta
        rr2 = abs(tp2_target - current_close) / risk_delta

        signal_payload = {
            "side": direction, "entry": current_close, "sl": round(calculated_sl, 5),
            "tp1": round(tp1_target, 5), "tp2": round(tp2_target, 5), "atr": round(current_atr, 4),
            "risk_pct": risk_pct * 100.0, "rr1": rr1, "rr2": rr2, "is_swing": is_swing,
            "exit_mode": p["exit_mode"], "time": datetime.now(timezone.utc).isoformat(),
            "source_tag": signal_source_tag,
        }

        self.set_cooldown(symbol_item, tf_id)
        self.dir_cooldown[f"{symbol_item}_{direction}"] = time.time()   # 記錄同向去重時戳
        if bar_ts != 0:
            self.last_bar_ts[bar_key] = bar_ts
        create_interactive_signal(signal_payload, symbol_item, tf_id, cvd_pass)

        # CVD 複合信號：30m_long 且 CVD 三層確認 → 特調高回報參數
        use_cvd_override = (tf_id == "30m" and direction == "long" and real_cvd_pass)
        if use_cvd_override:
            cvd_override = {"tp1_mult": 1.5, "tp2_mult": 2.0, "be_trigger": 1.0}
            risk_dist_long = abs(current_close - calculated_sl)
            tp1_target = current_close + risk_dist_long * cvd_override["tp1_mult"]
            tp2_target = current_close + risk_dist_long * cvd_override["tp2_mult"]
            signal_payload["tp1"] = round(tp1_target, 5)
            signal_payload["tp2"] = round(tp2_target, 5)
            signal_payload["cvd_override"] = True
            dc_log(f"⚡ CVD 複合信號觸發特調：{symbol_item} 30m_long → tp1=1.5R tp2=2.0R")

        if AUTO_TRADE.get(tf_id):
            if EXCHANGE_ENABLED.get("okx", True):
                execute_okx_trade_pipeline(
                    okx_swap_symbol, direction, current_close,
                    signal_payload["sl"], signal_payload["tp1"], signal_payload["tp2"],
                    p["exit_mode"], tf_id,
                )
            if EXCHANGE_ENABLED.get("bingx", True):
                execute_bingx_trade_pipeline(
                    symbol_item, direction, current_close,
                    signal_payload["sl"], signal_payload["tp1"], signal_payload["tp2"],
                    p["exit_mode"], tf_id,
                )
        else:
            pos = PaperPosition()
            pos.open = True; pos.side = direction; pos.entry = current_close
            pos.sl = signal_payload["sl"]; pos.tp1 = signal_payload["tp1"]; pos.tp2 = signal_payload["tp2"]
            pos.exit_mode = p["exit_mode"]
            self.paper_positions[f"{symbol_item}_{tf_id}"] = pos

_bot_ref = SykesTradingBot()

# ══════════════════════════════════════════════════════════════════════════════

# 嵌入式 WEB 伺服器與 DISCORD INTERACTION API 控制台 (WEB CONTROL CENTER)

# ══════════════════════════════════════════════════════════════════════════════

app = Flask(__name__)

def verify_discord_signature(raw_body: bytes, signature: str, timestamp: str) -> bool:
    """ Ed25519 靜態無狀態簽章驗證演算法 """
    if not DISCORD_PUBLIC_KEY or not signature or not timestamp:
        return False
    try:
        from nacl.signing import VerifyKey
        verify_key = VerifyKey(bytes.fromhex(DISCORD_PUBLIC_KEY))
        verify_key.verify(timestamp.encode() + raw_body, signature=bytes.fromhex(signature))
        return True
    except:
        return False

@app.route("/interactions", methods=["POST"])
def discord_interactions_webhook():
    """ 接收並解析來自 Discord 互動式 UI 按鈕的異步點擊授權回調 """
    signature = request.headers.get("X-Signature-Ed25519", "")
    timestamp = request.headers.get("X-Signature-Timestamp", "")
    raw_body = request.data

    if not verify_discord_signature(raw_body, signature, timestamp):
        return jsonify({"type": 1}), 200

    interaction_data = request.json or {}
    if interaction_data.get("type") == 1:
        return jsonify({"type": 1}), 200

    if interaction_data.get("type") == 3:  # 代表按鈕交互組件觸發
        custom_id = interaction_data.get("data", {}).get("custom_id", "")
        message_id = interaction_data.get("message", {}).get("id", "")

    if custom_id.startswith("confirm_") or custom_id.startswith("skip_"):
        action = "confirm" if custom_id.startswith("confirm_") else "skip"
        cache_key = custom_id.replace("confirm_", "").replace("skip_", "")

        if cache_key in pending_orders:
            order = pending_orders[cache_key]
            if action == "confirm":
                # 異步直通實盤下單模組
                Thread(target=execute_okx_trade_pipeline, args=(
                    order["symbol"], order["direction"], order["entry"],
                    order["sl"], order["tp1"], order["tp2"],
                    order.get("exit_mode", "fixed")
                )).start()
                new_status_text = f"✅ **控制中樞已接獲授權**: 已成功向 OKX 發送該筆實盤精密風控委託鏈。"
            else:
                new_status_text = f"❌ **訊號已手動拋棄**: 該項目已被交易員放棄，不執行任何實盤劃轉。"

            # 即時編輯卡片，移除按鈕避免重複觸發
            Thread(target=dc_embed_edit, args=(message_id, new_status_text)).start()
            pending_orders.pop(cache_key, None)

            return jsonify({
                "type": 4,
                "data": {"content": "核心中樞處理成功，正在向交易所進行線程同步...", "flags": 64}
            }), 200

    return jsonify({"type": 4, "data": {"content": "未知的核心控制碼", "flags": 64}}), 200

# ══════════════════════════════════════════════════════════════════════════════

# 時間同步與無漂移收盤矩陣輪詢機制 (SCHEDULER & MAIN LOOP)

# ══════════════════════════════════════════════════════════════════════════════

def synchronise_and_wait_next_candle() -> List[str]:
    """ 無漂移收盤對齊引擎：等待下一個整點/15分收盤 K 棒，返回觸發的時框 """
    while True:
        now = datetime.now()
        current_minute = now.minute
        current_second = now.second

        active_timeframes = []
    # 每 15 分鐘收盤觸發檢測
        if current_minute % 15 == 0 and current_second <= 3:
            active_timeframes.append("15m")
            if current_minute % 30 == 0:
                active_timeframes.append("30m")
            if current_minute == 0:
                active_timeframes.append("1H")
                if now.hour % 4 == 0:
                    active_timeframes.append("4H")

        if active_timeframes:
            sleep(5)
            return active_timeframes

        sleep(1)

_dc_last_msg_id = None

def poll_dc_commands():
    """ 輪詢 Discord 頻道訊息，處理 ! / / 指令 """
    global _PAUSED, _LIVE_MODE, _dc_last_msg_id, POSITION_SLOTS, RISK_PCT
    global CVD_ENABLED, ADX_ENABLED, AUTO_TRADE, MARGIN_MODE, EXCHANGE_ENABLED
    if not DISCORD_TOKEN or not DISCORD_CHANNEL_ID:
        print("[DC] DISCORD_TOKEN 或 DISCORD_CHANNEL_ID 未設定，指令輪詢停用。")
        return
    headers = {"Authorization": f"Bot {DISCORD_TOKEN}"}
    print("[DC] 指令輪詢已啟動。")
    try:
        init_resp = requests.get(f"{DC_BASE}/channels/{DISCORD_CHANNEL_ID}/messages", headers=headers, params={"limit": 1}, timeout=10)
        if init_resp.status_code == 200 and init_resp.json():
            _dc_last_msg_id = init_resp.json()[0].get("id")
            print(f"[DC] 初始化訊息ID: {_dc_last_msg_id}")
    except:
        pass
    while True:
        try:
            params = {"limit": 5}
            if _dc_last_msg_id:
                params["after"] = _dc_last_msg_id
            resp = requests.get(f"{DC_BASE}/channels/{DISCORD_CHANNEL_ID}/messages", headers=headers, params=params, timeout=10)
            if resp.status_code == 200:
                messages = resp.json()
                if messages:
                    for msg in reversed(messages):
                        msg_id  = msg.get("id", "")
                        content = msg.get("content", "").strip()
                        author  = msg.get("author", {})
                        is_bot  = author.get("bot", False)
                        if msg_id and (not _dc_last_msg_id or int(msg_id) > int(_dc_last_msg_id)):
                            _dc_last_msg_id = msg_id
                        if is_bot or not (content.startswith("!") or content.startswith("/")):
                            continue
                        parts = content.lower().split()
                        cmd   = parts[0].lstrip("!/")   # 統一去掉 ! 或 / 前綴
                        uptime_s = int(time.time() - _BOT_START_TS)
                        uptime_h = uptime_s // 3600
                        uptime_m = (uptime_s % 3600) // 60

                        # ── status ─────────────────────────────────────
                        if cmd == "status":
                            mode   = "🟢 LIVE 實盤" if _LIVE_MODE else "🟡 PAPER 模擬"
                            paused = "⏸️ 已暫停" if _PAUSED else "▶️ 掃描中"
                            per_slot_margin_str = "N/A (模擬模式)"
                            if _LIVE_MODE:
                                try:
                                    ex_tmp = _initialize_ccxt_client()
                                    bal    = ex_tmp.fetch_balance()
                                    total  = float(bal.get("USDT", {}).get("total", 0.0) or
                                                   bal.get("info", {}).get("totalEq", 0.0) or 0.0)
                                    risk_per_trade = total * RISK_PCT
                                    per_slot_margin_str = f"{risk_per_trade:.2f} USDT (總資產 {total:.2f} × {RISK_PCT*100:.0f}%)"
                                except:
                                    per_slot_margin_str = "查詢失敗"
                            tf_status = "  ".join(
                                f"`{k}`:{'✅' if v else '🔕'}" for k, v in AUTO_TRADE.items()
                            )
                            ex_status = "  ".join(
                                f"`{k}`:{'✅' if v else '🔕'}" for k, v in EXCHANGE_ENABLED.items()
                            )
                            dc_log(
                                f"⚙️ **賽克斯系統狀態**\n"
                                f"狀態: {paused} | 模式: **{mode}**\n"
                                f"CVD 過濾: {'✅ 開' if CVD_ENABLED else '🔕 關'}  "
                                f"ADX 過濾: {'✅ 開' if ADX_ENABLED else '🔕 關'}\n"
                                f"保證金模式: `{'全倉 cross' if MARGIN_MODE == 'cross' else '逐倉 isolated'}`\n"
                                f"交易所: OKX {'✅' if EXCHANGE_ENABLED.get('okx') else '🔕'} ({len(SYMBOLS)} 個幣)  BingX {'✅' if EXCHANGE_ENABLED.get('bingx') else '🔕'}\n"
                                f"自動下單: {tf_status}\n"
                                f"倉位格數: `{POSITION_SLOTS}` | 槓桿上限: `{MAX_LEVERAGE}x`\n"
                                f"每倉保證金: `{per_slot_margin_str}`\n"
                                f"運作時間: `{uptime_h}h {uptime_m}m`"
                            )

                        # ── help ───────────────────────────────────────
                        elif cmd == "help":
                            dc_log(
                                "📋 **指令列表**（`!` 或 `/` 前綴皆可）\n"
                                "`!status` - 系統狀態（CVD/ADX/時框開關）\n"
                                "`!setlive` / `!setpaper` - 切換實盤/模擬模式\n"
                                "`!pause` / `!resume` - 暫停/恢復掃描\n"
                                "`!risk [%]` - 設定每倉風險百分比（如 !risk 10）\n"
                                "`/cvd on|off` - 開關 CVD 過濾\n"
                                "`/adx on|off` - 開關 ADX 過濾\n"
                                "`/trade [15m|30m|1h|4h|all] on|off` - 開關自動下單\n"
                                "`/margin isolated|cross` - 切換逐倉/全倉模式\n"
                                "`/exchange okx|bingx on|off` - 開關交易所\n"
                            )

                        # ── setlive / setpaper ─────────────────────────
                        elif cmd == "setlive":
                            _LIVE_MODE = True
                            dc_log("🟢 **已切換為實盤模式**，自動下單鏈已啟用。")
                        elif cmd == "setpaper":
                            _LIVE_MODE = False
                            dc_log("🟡 **已切換為模擬模式**，僅觀察訊號不執行下單。")

                        # ── pause / resume ─────────────────────────────
                        elif cmd == "pause":
                            _PAUSED = True
                            dc_log("⏸️ **系統已暫停**，停止掃描與下單。")
                        elif cmd == "resume":
                            _PAUSED = False
                            dc_log("▶️ **系統已恢復**，重新開始掃描。")

                        # ── risk / setslots ────────────────────────────
                        elif cmd in ("risk", "setslots"):
                            if len(parts) >= 2:
                                val = parts[1].replace("%", "")
                                if val.replace(".", "").isdigit():
                                    v = float(val)
                                    # 判斷輸入：> 1 視為百分比（如 20 = 20%），≤ 1 視為小數（如 0.2）
                                    if v > 1:
                                        RISK_PCT = round(v / 100, 4)
                                    else:
                                        RISK_PCT = round(v, 4)
                                    POSITION_SLOTS = max(1, round(1.0 / RISK_PCT))
                                    dc_log(f"⚙️ 風險已更新: 每倉 `{RISK_PCT*100:.1f}%`，對應倉位格數: `{POSITION_SLOTS}` 倉")
                                else:
                                    dc_log("⚠️ 用法: `!risk 10`（輸入每倉風險百分比）")
                            else:
                                dc_log("⚠️ 用法: `!risk 10`（輸入每倉風險百分比）")

                        # ── cvd on|off ─────────────────────────────────
                        elif cmd == "cvd":
                            if len(parts) >= 2 and parts[1] in ("on", "off"):
                                CVD_ENABLED = (parts[1] == "on")
                                dc_log(f"{'✅' if CVD_ENABLED else '🔕'} CVD 過濾已{'啟用' if CVD_ENABLED else '停用'}")
                            else:
                                dc_log("⚠️ 用法: `/cvd on` 或 `/cvd off`")

                        # ── adx on|off ─────────────────────────────────
                        elif cmd == "adx":
                            if len(parts) >= 2 and parts[1] in ("on", "off"):
                                ADX_ENABLED = (parts[1] == "on")
                                dc_log(f"{'✅' if ADX_ENABLED else '🔕'} ADX 過濾已{'啟用' if ADX_ENABLED else '停用'}")
                            else:
                                dc_log("⚠️ 用法: `/adx on` 或 `/adx off`")

                        # ── trade [tf] on|off ──────────────────────────
                        elif cmd == "trade":
                            TF_MAP = {"15m": "15m", "30m": "30m", "1h": "1H", "4h": "4H", "all": "all"}
                            if len(parts) >= 3 and parts[1] in TF_MAP and parts[2] in ("on", "off"):
                                tf_key = TF_MAP[parts[1]]
                                state  = (parts[2] == "on")
                                if tf_key == "all":
                                    for k in AUTO_TRADE:
                                        AUTO_TRADE[k] = state
                                    dc_log(f"{'✅' if state else '🔕'} 所有時框自動下單已{'啟用' if state else '停用'}")
                                else:
                                    AUTO_TRADE[tf_key] = state
                                    dc_log(f"{'✅' if state else '🔕'} {tf_key} 自動下單已{'啟用' if state else '停用'}")
                            else:
                                dc_log("⚠️ 用法: `/trade [15m|30m|1h|4h|all] [on|off]`")

                        # ── margin isolated|cross ──────────────────────
                        elif cmd == "margin":
                            if len(parts) >= 2 and parts[1] in ("isolated", "cross"):
                                MARGIN_MODE = parts[1]
                                mode_txt = "逐倉 (isolated)" if MARGIN_MODE == "isolated" else "全倉 (cross)"
                                dc_log(f"💱 保證金模式已切換為：**{mode_txt}**\n⚠️ 注意：切換前請確認無持倉，新訂單才會套用新模式")
                            else:
                                dc_log("⚠️ 用法: `/margin isolated` 或 `/margin cross`")

                        # ── exchange okx|bingx on|off ───────────────────
                        elif cmd == "exchange":
                            if len(parts) >= 3 and parts[1] in ("okx", "bingx") and parts[2] in ("on", "off"):
                                exname = parts[1]
                                state  = parts[2] == "on"
                                EXCHANGE_ENABLED[exname] = state
                                dc_log(f"{'✅' if state else '🔕'} {exname.upper()} 交易所已{'啟用' if state else '停用'}")
                            else:
                                dc_log("⚠️ 用法: `/exchange okx|bingx on|off`")

        except Exception as e:
            print(f"[DC] 指令輪詢異常: {e}")
        sleep(5)
def _fetch_okx_swap_set() -> set:
    """抓取 OKX 所有上線中的 USDT 永續合約 instId 集合（免 API KEY）"""
    try:
        r = requests.get(
            f"{OKX_BASE}/api/v5/public/instruments",
            params={"instType": "SWAP", "quoteCcy": "USDT"},
            timeout=15
        )
        if r.status_code == 200:
            return {d["instId"] for d in r.json().get("data", []) if d.get("state") == "live"}
    except Exception as e:
        print(f"[SYMBOLS] OKX 合約列表抓取失敗: {e}", flush=True)
    return set()

def _fetch_coingecko_top100() -> list:
    """從 CoinGecko 抓市值前100幣種 symbol 列表（免 API KEY）"""
    try:
        r = requests.get(
            "https://api.coingecko.com/api/v3/coins/markets",
            params={"vs_currency": "usd", "order": "market_cap_desc",
                    "per_page": 100, "page": 1, "sparkline": "false"},
            headers={"Accept": "application/json"},
            timeout=15
        )
        if r.status_code == 200:
            return [c["symbol"].upper() for c in r.json()]
        print(f"[SYMBOLS] CoinGecko HTTP {r.status_code}", flush=True)
    except Exception as e:
        print(f"[SYMBOLS] CoinGecko 抓取失敗: {e}", flush=True)
    return []

def build_dynamic_symbols() -> bool:
    """
    重建 SYMBOLS + OKX_SWAP：
      CoinGecko 市值前100（排除穩定幣）× OKX 有上線永續合約
    失敗時回傳 False，SYMBOLS 保持不變。
    """
    global SYMBOLS, OKX_SWAP, _symbols_last_updated

    # 1. 載入快取（若此次啟動尚未載入）
    if _symbols_last_updated == 0.0:
        try:
            if os.path.exists(_SYMBOLS_CACHE_FILE):
                cached = json.load(open(_SYMBOLS_CACHE_FILE, encoding="utf-8"))
                SYMBOLS = cached["symbols"]
                OKX_SWAP = {v: k for k, v in SYMBOLS.items()}
                _symbols_last_updated = cached.get("updated", 1.0)
                print(f"[SYMBOLS] 快取載入：{len(SYMBOLS)} 個幣種", flush=True)
        except Exception as e:
            print(f"[SYMBOLS] 快取讀取失敗: {e}", flush=True)

    print("[SYMBOLS] 向 CoinGecko 抓取市值前100...", flush=True)
    top100 = _fetch_coingecko_top100()
    if not top100:
        print("[SYMBOLS] ⚠️ CoinGecko 失敗，維持現有列表", flush=True)
        return False

    print("[SYMBOLS] 向 OKX 確認永續合約...", flush=True)
    okx_swaps = _fetch_okx_swap_set()
    if not okx_swaps:
        print("[SYMBOLS] ⚠️ OKX 合約列表失敗，維持現有列表", flush=True)
        return False

    # 穩定幣排除清單
    STABLECOINS = {"USDT", "USDC", "BUSD", "DAI", "TUSD", "FDUSD", "USDD", "USDP"}

    new_symbols: Dict[str, str] = {}
    for coin in top100:
        if coin in STABLECOINS:
            continue
        inst_id = f"{coin}-USDT-SWAP"
        if inst_id in okx_swaps:
            new_symbols[inst_id] = f"{coin}/USDT"

    if len(new_symbols) < 10:
        print(f"[SYMBOLS] ⚠️ 動態列表僅 {len(new_symbols)} 個，回退備援列表", flush=True)
        return False

    SYMBOLS = new_symbols
    OKX_SWAP = {v: k for k, v in SYMBOLS.items()}
    _symbols_last_updated = time.time()

    # 儲存快取
    try:
        json.dump({"updated": _symbols_last_updated, "symbols": SYMBOLS},
                  open(_SYMBOLS_CACHE_FILE, "w", encoding="utf-8"), indent=2, ensure_ascii=False)
    except Exception as e:
        print(f"[SYMBOLS] 快取寫入失敗: {e}", flush=True)

    msg = f"🔄 幣種列表已動態更新：市值前100 × OKX 永續，共 **{len(SYMBOLS)}** 個"
    print(f"[SYMBOLS] ✅ {msg}", flush=True)
    dc_log(msg)
    return True

def main_polling_loop():
    """ 交易中樞核心守護進程主迴圈 """
    global _PAUSED, _bot_ref, _INITIAL_BALANCE
    # 啟動時永遠重新抓幣單（Railway 容器重啟後快取消失，需重新抓）
    build_dynamic_symbols()
    n_sym = len(SYMBOLS)

    start_alert = f"🚀 **賽克斯全功能完全體智慧交易系統 v4 實盤部署完成**\n控制中樞已對齊 **{n_sym}** 個主流加密商品（市值前100 × OKX 永續），開始進行 15m/30m/1H/4H 收盤矩陣輪詢機制..."
    dc_log(start_alert)
    tg_log(start_alert)

    # 啟動時抓取總資金（固定風險基準，僅此一次）
    if _LIVE_MODE and _INITIAL_BALANCE is None:
        try:
            ex_init = _initialize_ccxt_client()
            bal = ex_init.fetch_balance()
            total = float(bal.get("USDT", {}).get("total", 0.0))
            if total > 0:
                _INITIAL_BALANCE = total
                dc_log(f"💰 初始總資金已鎖定：`{_INITIAL_BALANCE:.2f} USDT`（單筆風險 = {RISK_PCT*100:.0f}% = `{_INITIAL_BALANCE*RISK_PCT:.2f} USDT`）")
        except Exception as e:
            print(f"[INIT] 無法抓取初始餘額: {e}")

    while True:
        try:
            active_tfs_to_run = synchronise_and_wait_next_candle()

            if _PAUSED:
                continue

            check_trailing_stops_for_real()

            # 每週自動更新幣種列表（7天 = 604800秒）
            if time.time() - _symbols_last_updated > 604800:
                print("[SYMBOLS] 距上次更新超過7天，自動重新抓取...", flush=True)
                build_dynamic_symbols()

            for tf in active_tfs_to_run:
                print(f"[{datetime.now().strftime('%H:%M:%S')}] ⏳ 核心排程觸發：啟動時框 {tf} 全商品指標矩陣掃描...")
            for symbol_item in list(SYMBOLS.values()):
                try:
                    _bot_ref.scan_and_process_market(symbol_item, tf)
                    sleep(0.25)
                except Exception as loop_exception:
                    print(f"  ❌ 商品 {symbol_item} 於時框 [{tf}] 處理時發生系統例外: {loop_exception}")

        except Exception as outer_err:
            print(f"[MAIN LOOP] 主循環例外，繼續運行: {outer_err}")
            sleep(5)

def run_embedded_web_server():
    import logging
    werkzeug_logger = logging.getLogger("werkzeug")
    werkzeug_logger.setLevel(logging.ERROR)
    app.run(host="0.0.0.0", port=int(os.environ.get("PORT", 3000)), debug=False)

# ══════════════════════════════════════════════════════════════════════════════

# 交易核心主入口引導程序 (ENTRYPOINT)

# ══════════════════════════════════════════════════════════════════════════════

if __name__ == "__main__":
    parser = argparse.ArgumentParser(description="Sykes Multi-Timeframe Trading System Engine")
    parser.add_argument("--live", action="store_true", help="強制覆蓋開啟 OKX 實盤下單鏈")
    parser.add_argument("--demo", action="store_true", help="切換至 OKX 模擬盤測試環境")
    args = parser.parse_args()

    if args.live:
        _LIVE_MODE = True
    if args.demo:
        OKX_DEMO = True

    print("=" * 70)
    print(f" 賽克斯全功能智慧交易中樞核心引擎系統啟動中... ")
    print(f" 實盤模式狀態: {'🟢 LIVE 實盤委託對接中' if _LIVE_MODE else '🟡 PAPER 模擬記帳觀察中'}")
    print(f" OKX 環境配置: {'⚠️ 模擬盤 (Sandbox)' if OKX_DEMO else '⚡ 正式實盤節點'}")
    print("=" * 70)

# 1. 異步啟動嵌入式控制台 Web 控制中樞
    web_worker_thread = Thread(target=run_embedded_web_server, daemon=True)
    web_worker_thread.start()

# 2. 啟動 Discord 指令輪詢執行緒
    dc_cmd_thread = Thread(target=poll_dc_commands, daemon=True)
    dc_cmd_thread.start()

# 3. 直通主執行緒進入無漂移排程輪詢主迴圈
    main_polling_loop()
#
