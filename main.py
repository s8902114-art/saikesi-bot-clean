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
import daily_report   # 每日00:00(UTC)復盤(record_entry進場記;daily_tick主迴圈發)
import ccxt
from flask import Flask, request, jsonify

# 指標與形態（已抽出至 indicators.py，見 MODULE_MAP.md）
from indicators import (
    calculate_smooth_rsi,
    calculate_full_qqe_mod,
    calculate_average_true_range,
    calculate_directional_movement_index,
    calculate_macd,
    macd_difslope_ok,
    ladder_fib_lines,
    near_ladder,
    check_double_bottom,
    check_double_top,
)

# ── 逐筆 tFlow 確認(2026-06-12 升級 1H MACD 進場)──────────────────────────────
# 回測:1H MACD空+帶量+tFlow 驗+0.459、1H MACD多 驗+0.465(勝聚合CVD +0.378/+0.353)。
# tFlow = 該『已完成』1H bar 的 taker 淨流(sign=-1 if isBuyerMaker else +1)。
# 僅 BTC/ETH/SOL(有逐筆對齊回測);其他幣回 None → 只靠帶量。Binance fapi 公開免auth。
# (內聯於 main.py:push.sh 只推 main.py,不可用外部模組。語意對齊 trading-backtest/tflow_live.py)
_TFLOW_COINS = {"BTCUSDT", "ETHUSDT", "SOLUSDT"}
# 輕量端點:一次回傳整小時 taker 買量/賣量,buyVol-sellVol 號 = 逐筆 sq_sum 號(2026-06-12 三幣實測號全一致、值近同)。
# 取代分頁 aggTrades(~10s)→ 一個 call、瞬間。bot 本來就用 takerlongshortRatio(_fetch_binance_ls_taker)。
_TFLOW_BASE = "https://fapi.binance.com/futures/data/takerlongshortRatio"
def _tflow_last_hour(symbol):
    """回傳 (net=buyVol-sellVol, ok)。ok=False 表示沒對齊到完成小時列 → 退回帶量。"""
    now = int(time.time() * 1000); hour = 3600_000
    start = (now // hour) * hour - hour                     # 最近一根已完成 1H 的起點
    r = requests.get(_TFLOW_BASE, params={"symbol": symbol, "period": "1h", "limit": 6}, timeout=10).json()
    if not isinstance(r, list): return 0.0, False
    for x in r:
        if int(x["timestamp"]) == start:
            bv = float(x["buyVol"]); sv = float(x["sellVol"])
            if bv + sv <= 0: return 0.0, False
            return bv - sv, True
    return 0.0, False
def tflow_confirm(symbol, direction):
    """進場確認閘。空:net<0;多:net>0。非3幣/無資料/失敗回 (None,...) → 交給帶量。"""
    if symbol not in _TFLOW_COINS: return None, "non-tape coin"
    try:
        net, ok = _tflow_last_hour(symbol)
    except Exception as e:
        return None, f"tflow err {e}"
    if not ok: return None, "tflow no-data"
    res = (net < 0) if direction == "short" else (net > 0)
    return res, f"tFlow net={net:+.1f}"

# ── 15m tFlow 滾動z(2026-06-16 新增 15m MACD空用)──────────────────────────────
# period=5m 抓近9h → 滾動12桶(=1h)淨流 + 該序列z-score。對齊回測 _macd_15m_improve.py
# (5分桶 buyVol-sellVol ≈ 逐筆 sq_sum、滾動12、z over ~96)。比 period=1h 時鐘桶能做「零延遲滾動」。
# 回測(3幣WF):15m MACD空 +tFlow|z|>1.0 +swing_full(pivot) 訓+0.148/驗+0.254/MDD24%(訓驗同向=穩)。
def tflow_z(symbol):
    """回傳 (net_1h, z, ok)。僅3幣;非3幣/資料不足回 ok=False → 該15m空不放行。"""
    try:
        r = requests.get(_TFLOW_BASE, params={"symbol": symbol, "period": "5m", "limit": 108}, timeout=10).json()
    except Exception:
        return 0.0, 0.0, False
    if not isinstance(r, list) or len(r) < 60:
        return 0.0, 0.0, False
    r = sorted(r, key=lambda x: int(x["timestamp"]))            # 由舊到新
    net5 = [float(x["buyVol"]) - float(x["sellVol"]) for x in r]
    roll = [sum(net5[i-11:i+1]) for i in range(11, len(net5))]  # 滾動12桶=1h淨流
    if len(roll) < 12:
        return 0.0, 0.0, False
    cur = roll[-1]; base = roll[:-1]
    mu = sum(base) / len(base)
    sd = (sum((x - mu) ** 2 for x in base) / len(base)) ** 0.5
    if sd <= 0:
        return cur, 0.0, True
    return cur, (cur - mu) / sd, True

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
RISK_PCT     = 0.05        # 單筆最大風險 = 基準10U × 5%(ladder基準10U下27U帳戶實際~1.85%/筆=已保守;用戶確認改回5成長快)
RISK_TOLERANCE_MULT = 2.0  # 停損容忍倍數：張數進位後停損 ≤ 風險預算 × 此值 才下單（超過則拒單）
OKX_MIN_MMR       = 350.0  # OKX 開倉前維持保證金率門檻(%)：預估加新倉後 < 此值就跳過（!setmmr 可調）
BINGX_MAX_RISK_RATE = 0.70 # BingX 開倉前帳戶風險率上限：預估加新倉後 > 此值就跳過（!setbingxrisk 可調）
# ── 分段複利下注（壓 MDD；回測：每+50U → 37倍/MDD50% vs 純複利MDD96%）──
LADDER_BASE_USDT = 10.0    # 初始下注基準（單筆風險 = 此值 × RISK_PCT 起跳）
LADDER_STEP_USDT = 50.0    # 每多賺此金額，單筆風險才加一級（/setladder 可調）
POSITION_SLOTS = 10        # 倉位格數（保留供 !setslots 指令使用）
SIGNAL_COOLDOWN = 1800     # 同一商品相同時框的訊號冷卻時間 (秒)
DIR_SIGNAL_COOLDOWN = 3600 # 同幣同方向跨時框去重：1 小時內只下一次（避免 15m/30m/1H 整點同時觸發）
MAX_CONSEC_LOSS = 3       # 最大連續虧損次數限制，達標後觸發熔斷
PAUSE_HOURS = 24           # 熔斷冷卻時間 (小時)
DAILY_STOP_ENABLED = True  # 每日虧損熔斷:當日從日初錢包跌破X%→停開新倉到隔日UTC(擋齊漲血洗的災難日肥尾)
DAILY_LOSS_PCT = 0.30      # 每日最大虧損(錢包%):-30%=只有齊漲血洗的災難日才觸發,正常小虧連發(讓跑書呼吸)不打斷。Discord !dailystop 可調
DIR_BALANCE_ENABLED = True # 方向平衡:防整本全做空/全做多→一個反彈全清。主導方向比另一方多 MAX_DIR_SKEW 倉時擋該方向新倉
MAX_DIR_SKEW = 15          # 每所方向偏斜上限:主導方向比另一方多15個幣才擋(放鬆=只防荒謬集中,熊市正常空單不binding)。Discord !dirskew 可調(0=關)
CONC_RISK_ENABLED = True   # 風險預算:同向倉堆太多時,新倉風險遞減(不擋單=不丟edge,但總曝險有界=squeeze那天虧得小)
CONC_FREE = 10             # 每所同向「全倉」名額;超過後新倉 ×(CONC_FREE/倉數),總同向曝險≈CONC_FREE R
CONC_FLOOR = 0.3           # 風險遞減下限(不會縮到太小)。Discord !concrisk N 可調(0=關)

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
# ✅ 15m/long：WF 訓練+0.107→驗證+0.052；BE 延後至 1.5R（出場優化 WF 驗證 +0.079 vs 原 +0.047）
#    TP1=1.2 TP2=2.5 BE=1.5 BUF=0.0 PVT=10
"15m_long": {
"tp1_mult": 1.2,  "tp2_intraday_mult": 2.5,  "tp2_swing_mult": 2.5, "be_trigger": 1.5,
"sl_atr_buffer": 0.0, "structure_lookback": 10, "exit_mode": "fixed",
"qqe_rsi": 7, "qqe_sf": 5, "qqe_factor": 3.0
},
# ✅ 15m/short：WF 訓練+0.028→驗證+0.124；BE 延後至 1.5R（出場優化）
#    TP1=1.0 TP2=2.5 BE=1.5 BUF=0.3 PVT=5
"15m_short": {
"tp1_mult": 1.0,  "tp2_intraday_mult": 2.5,  "tp2_swing_mult": 2.5, "be_trigger": 1.5,
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
# 1H/short：TP1=1.5（2026-06-12 升級:MACD空 swing_tp=TP1.5半倉+剩半轉折移SL,WF驗+0.459）
#    C3+階梯空走 swing_full 不掛TP→不受此值影響;僅 MACD空 與 罕見1H雙頂固定R 用到。BE=1.5。
"1H_short": {
"tp1_mult": 1.5,  "tp2_intraday_mult": 2.0,  "tp2_swing_mult": 2.0, "be_trigger": 1.5,
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

# ── active_real_trades 持久化（解決重啟/redeploy 後追蹤丟失）────────────────
# Railway 每次 redeploy 會重啟程式，純記憶體的 active_real_trades 會清空，
# 導致已開倉的保本/移動止損追蹤停擺。存成 json，啟動時讀回。
# 注意：BingX 的 headers 含 API 金鑰，不落地；讀回時用全域 key 重建。
_TRADES_FILE = os.path.join(os.path.dirname(os.path.abspath(__file__)), "active_trades.json")

def save_active_trades():
    """將 active_real_trades 存成 json（排除 headers 等不可序列化/敏感欄位）"""
    try:
        dump = {}
        for k, v in active_real_trades.items():
            dump[k] = {kk: vv for kk, vv in v.items() if kk != "headers"}
        with open(_TRADES_FILE, "w", encoding="utf-8") as f:
            json.dump(dump, f, ensure_ascii=False, default=str)
    except Exception as e:
        print(f"[Persist] 存檔 active_trades 失敗: {e}")

def load_active_trades():
    """啟動時讀回 active_real_trades；BingX 條目補回 headers"""
    if not os.path.exists(_TRADES_FILE):
        return
    try:
        with open(_TRADES_FILE, encoding="utf-8") as f:
            data = json.load(f)
        for k, v in data.items():
            if v.get("exchange") == "bingx":
                v["headers"] = {"X-BX-APIKEY": BINGX_API_KEY}
            active_real_trades[k] = v
        if active_real_trades:
            print(f"[Persist] 已讀回 {len(active_real_trades)} 筆追蹤中倉位")
            dc_log(f"♻️ 重啟後已還原 {len(active_real_trades)} 筆倉位追蹤（保本/移動止損續行）")
    except Exception as e:
        print(f"[Persist] 讀回 active_trades 失敗: {e}")

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

def _entry_reason(source_tag: str, side: str, tf: str, dh_boost: float) -> str:
    """把訊號來源組成一句白話進場原因。"""
    s = source_tag or ""
    trend = "多頭" if side == "long" else "空頭"
    bits = []
    if "C3" in s:
        bits.append("回踩維加斯通道後 QQE 轉" + ("強" if side == "long" else "弱"))
    if "雙頂+RSI" in s:   bits.append("雙頂 + RSI 跌破50 共振")
    elif "雙底+RSI" in s: bits.append("雙底 + RSI 穿50 共振")
    elif "雙頂" in s:     bits.append("M頭型態")
    elif "雙底" in s:     bits.append("W底型態")
    if "MACD" in s:       bits.append("MACD 動能 + 4H 趨勢同向")
    if "數據獵手空" in s:  bits.append("大級別2B假突破 + CVD頂背離 + OI升 + 散戶爆多")
    if "箱突破空" in s:    bits.append("跌破盤整箱底 + 帶量 + CVD↓ + OI升(順勢)")
    if "維加斯大通道空" in s: bits.append("回測維加斯大通道被擋 + 1H空方共振 + CVD↓ + 散戶爆多(fade)")
    if "主力建" in s:      bits.append("12h窄幅壓縮 + 帶量突破 + OI升建倉 + 4H順向(主力建倉噴出)")
    if tf == "1H" and side == "short":
        bits.append("靠階梯壓力位")
    if dh_boost and dh_boost > 1.0:
        bits.append(f"CVD 吸收確認(加碼×{dh_boost})")
    return f"{trend}趨勢｜" + "、".join(bits) if bits else f"{trend}趨勢"


def create_interactive_signal(sig: Dict[str, Any], symbol: str, tf: str, cvd_ok: bool) -> str:
    """ 精簡訊號卡：標題 + 進場原因 + 進場/止損 + TP1/TP2 + 授權按鈕 """
    side_emoji = "🟢" if sig["side"] == "long" else "🔴"
    dir_name   = "多" if sig["side"] == "long" else "空"
    card_color = 0x2ecc71 if sig["side"] == "long" else 0xe74c3c
    coin_name  = symbol.split("/")[0]
    unique_callback_key = f"sykes_{coin_name.lower()}_{tf}_{sig['side']}_{int(time.time())}"

    pending_orders[unique_callback_key] = {
        "symbol": OKX_SWAP.get(symbol, symbol), "direction": sig["side"],
        "entry": sig["entry"], "sl": sig["sl"], "tp1": sig["tp1"], "tp2": sig["tp2"],
        "exit_mode": sig.get("exit_mode", "fixed"),
    }

    reason = _entry_reason(sig.get("source_tag", ""), sig["side"], tf, sig.get("dh_boost", 1.0))
    try:
        _judge_brief = judge_coin(coin_name, sig["side"], brief=True)
    except Exception:
        _judge_brief = None
    embed_payload = {
        "title": f"{side_emoji} {coin_name} · {tf} {dir_name}",
        "description": f"**進場原因:** {reason}" + (f"\n**順籌碼:** {_judge_brief}" if _judge_brief else ""),
        "color": card_color,
        "fields": [
            {"name": "進場", "value": f"**{sig['entry']}**", "inline": True},
            {"name": "止損", "value": f"`{sig['sl']}` ({sig['risk_pct']:.2f}%)", "inline": True},
            {"name": "保本", "value": "達標自動推成本價", "inline": True},
            {"name": f"TP1 (50% · 1:{sig['rr1']:.1f})", "value": f"`{sig['tp1']}`", "inline": True},
            {"name": f"TP2 (50% · 1:{sig['rr2']:.1f})", "value": f"`{sig['tp2']}`", "inline": True},
        ],
        "footer": {"text": unique_callback_key},
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
    # Coinalyze 改版:'history' 端點已404,改 'ohlcv-history'(回 v總量/bv買量),分組格式
    raw_data = _execute_coinalyze_request("ohlcv-history", {
    "symbols": cona_symbol,
    "interval": cona_interval,
    "from": str(start_timestamp // 1000),
    "to": str(end_timestamp // 1000)
    })
    if not raw_data:
        return pd.Series(dtype=float)

    records = []
    for grp in raw_data:
        if not isinstance(grp, dict):
            continue
        for node in grp.get("history", []):
            t_sec = node.get("t", 0)
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

    # Coinalyze 改版:回 [{symbol, history:[{t,o,h,l,c}]}] 分組格式,OI=history[].c(收盤OI)
    records = []
    for grp in raw_data:
        if isinstance(grp, dict):
            for h in grp.get("history", []):
                records.append((int(h.get("t", 0)) * 1000, float(h.get("c", 0.0) or 0.0)))
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

_daily_stop = {"day": None, "start": None, "active": False, "last": 0.0}
def _daily_stop_active() -> bool:
    """每日虧損熔斷:當日從日初錢包跌破 DAILY_LOSS_PCT → True(停開新倉)。隔日UTC自動解。
    只擋新倉,不碰既有倉管理(在 scan 開頭 return,check_trailing 照常跑)。失敗/非live 回 False 不擋。"""
    if not DAILY_STOP_ENABLED or not _LIVE_MODE:
        return False
    import datetime as _dt
    now = time.time()
    today = _dt.datetime.now(_dt.timezone.utc).date().isoformat()
    if _daily_stop["day"] != today:   # 新的一天→重置
        _daily_stop.update({"day": today, "start": None, "active": False, "last": 0.0})
    if _daily_stop["active"]:
        return True
    if now - _daily_stop["last"] < 60:   # 快取60s,少打API
        return False
    _daily_stop["last"] = now
    try:
        bd = _initialize_ccxt_client().fetch_balance()
        w = None
        for _ccy in ((bd.get("info", {}).get("data") or [{}])[0].get("details") or []):
            if _ccy.get("ccy") == "USDT":
                w = float(_ccy.get("cashBal") or _ccy.get("availBal") or 0.0); break
        if w is None:
            w = float((bd.get("USDT") or {}).get("total") or 0.0)
        if w <= 0:
            return False
        if _daily_stop["start"] is None:
            _daily_stop["start"] = w; return False
        dd = (w - _daily_stop["start"]) / _daily_stop["start"]
        if dd <= -DAILY_LOSS_PCT:
            _daily_stop["active"] = True
            dc_log(f"🛑 **每日虧損熔斷**:今日從 {_daily_stop['start']:.2f}U → {w:.2f}U ({dd:+.0%}),"
                   f"停開新倉至隔日UTC。既有倉照常管理。")
            return True
    except Exception as _dse:
        print(f"[DailyStop] 檢查失敗(不擋): {_dse}")
    return False


def _dir_skew_block(new_dir: str, exch: str) -> bool:
    """方向平衡:某交易所該方向已過度集中(主導 - 另一方向 >= MAX_DIR_SKEW)→擋該所該方向新倉。
    防單一帳戶全做空(或全做多)被一反彈一次清光。★個別算:每所數自己的實際持倉(兩所獨立、且不是每幣兩所都開)。回 True=擋。"""
    if not DIR_BALANCE_ENABLED or MAX_DIR_SKEW <= 0:
        return False
    longs  = sum(1 for t in active_real_trades.values()
                 if t.get("exchange") == exch and t.get("direction") == "long")
    shorts = sum(1 for t in active_real_trades.values()
                 if t.get("exchange") == exch and t.get("direction") == "short")
    if new_dir == "short" and (shorts - longs) >= MAX_DIR_SKEW:
        return True
    if new_dir == "long" and (longs - shorts) >= MAX_DIR_SKEW:
        return True
    return False


def _concentration_mult(new_dir: str, exch: str) -> float:
    """風險預算:某所同向倉已堆 n 個,新倉風險 ×(CONC_FREE/n)(n>CONC_FREE時遞減)。
    不擋單(edge不丟),但讓總同向曝險≈CONC_FREE R,squeeze那天虧得小。回 1.0~CONC_FLOOR。"""
    if not CONC_RISK_ENABLED or CONC_FREE <= 0:
        return 1.0
    n = sum(1 for t in active_real_trades.values()
            if t.get("exchange") == exch and t.get("direction") == new_dir)
    if n < CONC_FREE:
        return 1.0
    return max(CONC_FLOOR, CONC_FREE / float(n + 1))


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

def _okx_cancel_all_algos(inst_id: str) -> int:
    """取消該 instId 所有 pending conditional algo 單(TP/SL)。回傳取消數。
    用於解 51088「同全倉位只能有一張 TP/SL」→ 清掉舊單再重掛。"""
    now=datetime.now(timezone.utc); ts=now.strftime("%Y-%m-%dT%H:%M:%S.")+f"{now.microsecond//1000:03d}Z"
    path=f"/api/v5/trade/orders-algo-pending?ordType=conditional&instId={inst_id}"
    sig=_okx_generate_signature(ts,"GET",path,"")
    headers={"OK-ACCESS-KEY":OKX_API_KEY,"OK-ACCESS-SIGN":sig,"OK-ACCESS-TIMESTAMP":ts,
             "OK-ACCESS-PASSPHRASE":OKX_PASSPHRASE,"Content-Type":"application/json"}
    if OKX_DEMO: headers["x-simulated-trading"]="1"
    cancelled=0
    try:
        r=requests.get(f"{OKX_BASE}{path}",headers=headers,timeout=10).json()
        for d in (r.get("data") or []):
            aid=d.get("algoId")
            if aid and _cancel_okx_algo_order(inst_id, aid): cancelled+=1
    except Exception as e:
        print(f"[Algo] 取消全部algo失敗 {inst_id}: {e}")
    return cancelled

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
                              tf_id: str = "15m", position_scale: float = 1.0,
                              pyramid_eligible: bool = False,
                              exit_strategy: str = "", allow_stack: bool = False) -> None:
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
        if available_usdt <= 0:   # 2026-06-27 移除人為餘額門檻(用戶要求):只要風險值內+真margin夠就開,實際保證金把關在後面(可用USDT<需要margin才擋)。只防真0餘額
            dc_log(f"⚠️ **實盤交易中斷**: 帳戶可用餘額為 0")
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
        wallet_now = wallet_usdt if wallet_usdt > 0 else (available_usdt if available_usdt > 0 else total_usdt)
        # ── 分段複利下注 + 小資金自動縮（壓低 MDD，且基準不超過實際餘額）──────
        # 階梯基準：每多賺 LADDER_STEP_USDT 升一級（回測：每+50U → 37倍/MDD50%）。
        # 但若實際餘額 < 階梯基準（如帳戶5U卻設基準10U），改用實際餘額當基準，
        # 讓每單風險貼近實際資金（5U×10%=0.5U），避免小帳戶撐不起、保證金卡滿。
        level = max(0, int((wallet_now - LADDER_BASE_USDT) // LADDER_STEP_USDT))
        ladder_base = LADDER_BASE_USDT * (1 + level)   # 階梯基準
        eff_base = min(ladder_base, wallet_now)        # 取較小：小資金用實際餘額
        risk_usdt = eff_base * RISK_PCT
        base_funds = wallet_now                     # 錢包餘額（顯示/參考用）

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

        # ── 鐵律：每單觸止損 = risk_usdt，不論動用多少保證金 ────────────────────
        # 已移除原本的「保證金上限(max_margin = base×RISK_PCT)」——它會在止損近時
        # 把倉位縮小，導致觸損虧損 < risk_usdt，破壞「每單精準虧 risk_usdt」鐵律。
        # 倉位一律 = risk_usdt ÷ 止損距離%，保證金該多少就多少（不夾）。
        # 保證金不足由後面的「可用USDT檢查」乾淨跳過，不在此處縮倉。

        # ── 倉位縮放（position_scale）：<1.0 縮倉(弱訊號) / >1.0 加碼(CVD吸收C方案)──
        if position_scale != 1.0:
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
                    if projected_mmr < OKX_MIN_MMR:
                        dc_log(f"⚠️ OKX 跳過 [{symbol_id}]：維持保證金率不足"
                               f"（預估 {projected_mmr:.1f}% < {OKX_MIN_MMR:.0f}%）")
                        return
            except Exception as risk_check_err:
                print(f"[RiskCheck] OKX 維持保證金率檢查失敗: {risk_check_err}")

        # ── 下單前可用 USDT 檢查（事前乾淨跳過，避免 51008 Insufficient margin 崩潰）──
        # 重新抓即時可用餘額（函數開頭那次已過時：其他幣同時下單會佔用保證金），
        # 並要求 可用 >= 需要保證金 × 1.05（留 5% 緩衝給手續費/精度/滑點）。
        try:
            _bal_now = ex.fetch_balance()
            avail_now = float(_bal_now.get("USDT", {}).get("free", 0.0))
        except Exception:
            avail_now = available_usdt   # 抓失敗退回開頭那次
        need_margin = allocated_margin * 1.05
        if avail_now < need_margin:
            dc_log(f"⚠️ OKX 跳過 [{symbol_id}]：可用USDT {avail_now:.2f} 不足，需要 {need_margin:.2f}"
                   f"（保證金 {allocated_margin:.2f} ×1.05 緩衝）")
            return

        # ── ★強平保護：強平價若落在停損之前，這筆不下（保住高槓桿、只擋自殺單）──────
        # 全倉強平≈逆向虧損把可動用權益吃光。可承受逆向幅度(%) ≈
        #   (可用USDT + 本倉保證金) ÷ 倉位價值。
        # 若 停損距離% ≥ 估算強平距離% × 0.85（留緩衝）→ 價格會在碰停損前先強平，
        # 全倉模式下會連帶清掉帳戶其他倉(含手動倉) → 直接跳過不下。
        # 高槓桿照舊；只有「停損太遠相對於當前權益緩衝」的危險單會被擋。
        est_liq_dist = (avail_now + allocated_margin) / position_value if position_value > 0 else 0.0
        if sl_distance_pct >= est_liq_dist * 0.85:
            dc_log(f"⚠️ OKX 跳過 [{symbol_id}]：強平價會在停損前觸發，為保護全倉帳戶不下單"
                   f"（停損距 {sl_distance_pct*100:.2f}% ≥ 估強平距 {est_liq_dist*100:.2f}%×0.85；"
                   f"可用 {avail_now:.2f}U／倉位 {position_value:.2f}U）")
            return

        # ── 不限倉數：只要保證金夠 + 風險值內就下（倉數上限已移除）──────────────
        # 把關交給：可用USDT檢查、維持保證金率(OKX_MIN_MMR)、worst_loss、同向去重。
        positions_raw = ex.fetch_positions()   # 仍需取得持倉供下方防同向加倉判斷

        # ── 防同幣同向重複加倉（避免訊號反覆觸發把單倉越疊越大）──────────────
        # 原本只檢查總倉數，沒擋「同幣同向已有倉」→ 同一幣每隔冷卻期就再加一筆，
        # 名義/保證金累積成大倉，且止損仍按單筆算 → 實際觸損遠超預算。
        # ★bugfix 2026-07-06:舊比對 _p["symbol"]==symbol_id 是死代碼——ccxt回統一格式"SOL/USDT:USDT",
        #   symbol_id是instId"SOL-USDT-SWAP",永遠不相等→6/1上線以來一次沒擋過(7/1-7/4 SOL被同訊號連加3筆
        #   把均價從75.4墊到79.5,MFE5.7R只實現+0.77)。改成兩邊都正規化成"SOL/USDT"比對。
        #   趨勢延續的加碼交給_mai_add_on_swing(轉折加碼:守3上限/遞減0.5x/SL跟轉折線,已驗證),不靠盲目再進場。
        #   allow_stack=True(同訊號平行hf_1r倉,刻意雙倉)豁免,與BingX行為對齊。
        _base_sym = SYMBOLS.get(symbol_id, symbol_id).split(":")[0]   # → "SOL/USDT"
        if not allow_stack:
            for _p in positions_raw:
                _psym = (_p.get("symbol") or "").split(":")[0]
                if (_psym == _base_sym
                        and _p.get("side") == trade_side
                        and abs(float(_p.get("contracts") or 0)) > 0):
                    dc_log(f"⚠️ OKX 跳過 [{_base_sym}]：已有 {trade_side} 倉，不重複加倉(趨勢延續由轉折加碼接手)")
                    return

        # 設槓桿：OKX 需帶 mgnMode；全倉(cross)不可帶 posSide，逐倉(isolated)才需要。
        # 若沒設成功，OKX 會用預設低槓桿算保證金 → position_value 大時爆 51008。
        _lev_ok = False
        try:
            if MARGIN_MODE == "cross":
                ex.set_leverage(calculated_leverage, symbol_id, params={"mgnMode": "cross"})
            else:
                ex.set_leverage(calculated_leverage, symbol_id, params={"mgnMode": "isolated", "posSide": trade_side})
            _lev_ok = True
        except Exception as _lev_err:
            # 重試：不帶 posSide（部分情況 posSide 會被拒）
            try:
                ex.set_leverage(calculated_leverage, symbol_id, params={"mgnMode": MARGIN_MODE})
                _lev_ok = True
            except Exception as _lev_err2:
                dc_log(f"⚠️ OKX [{symbol_id}] 設槓桿失敗（{calculated_leverage}x）：{_lev_err2}；"
                       f"為避免用預設低槓桿爆保證金(51008)，跳過此單")
                return   # 槓桿沒設成功就別硬下，否則必爆 51008

        is_buy       = (trade_side == "long")
        entry_action = "buy"  if is_buy else "sell"
        exit_action  = "sell" if is_buy else "buy"
        inst_id      = OKX_SWAP.get(symbol_id, symbol_id)

        # ── OKX 永續一律用「張數」下單（棄用 tgtCcy，那對 SWAP 不可靠）──────────
        # 真因：amount=position_value + tgtCcy=quote_ccy 時，OKX 把 16.16 當成「16.16 張」，
        #       NEAR ctVal=10 → 16.16×價×10 = 380U 名義被提前強平。
        # 正解：張數 = 名義 ÷ (價 × ctVal)，再用 ccxt amount_to_precision 依該幣精度取整。
        #       （NEAR 精度0.1 → 0.7張；SKY 精度1 → 整張。自動適配，不寫死 int）
        mkt    = ex.market(symbol_id)
        ct_val = float(mkt.get("contractSize", 1.0) or 1.0)
        contract_notional = current_market_price * ct_val          # 1 張名義價值(USDT)
        raw_contracts = position_value / contract_notional         # 應下張數(可能小數)
        try:
            qty_str = ex.amount_to_precision(symbol_id, raw_contracts)  # 依該幣精度取整
            total_contracts = float(qty_str)
        except Exception:
            total_contracts = raw_contracts
        # 最小下單量檢查：不足最小張數則拒單（不硬進位放大，守住風險）
        _min_amt = float(((mkt.get("limits") or {}).get("amount") or {}).get("min") or 0)
        if total_contracts <= 0 or (_min_amt > 0 and total_contracts < _min_amt):
            dc_log(f"⚠️ OKX 跳過 [{symbol_id}]：應下 {raw_contracts:.3f} 張 < 最小 {_min_amt} 張"
                   f"（本金不足以承接此幣最小單位）")
            return
        # 風控：實際張數的停損虧損不得超過風險預算 × RISK_TOLERANCE_MULT
        worst_loss = total_contracts * contract_notional * sl_distance_pct
        if worst_loss > risk_usdt * RISK_TOLERANCE_MULT:
            dc_log(f"⚠️ OKX 跳過 [{symbol_id}]：預估停損虧損 {worst_loss:.2f}U "
                   f"> 風險預算 {risk_usdt:.2f}U × {RISK_TOLERANCE_MULT}，拒絕超額下單")
            return

        _scale_tag = (f" · ⚡CVD加碼×{position_scale}" if position_scale > 1.0
                      else (f" · 🔻縮倉×{position_scale}" if position_scale < 1.0 else ""))
        execution_report = [
            f"🚀 OKX {symbol_id} {'多' if is_buy else '空'} 下單成功{_scale_tag}",
            f"{calculated_leverage}x · {total_contracts}張 · 風險 {risk_usdt:.2f}U ({RISK_PCT*100:.0f}%)",
        ]

        entry_order = ex.create_market_order(
            symbol=symbol_id,
            side=entry_action,
            amount=total_contracts,     # 張數（已依精度取整）
            params={"posSide": trade_side, "tdMode": MARGIN_MODE}
        )

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

        # ── 價格精度化（修低價幣如 SHIB 的 str(0.00001)='1e-05' 科學記號被 OKX 拒絕）──
        # 一律用 ccxt price_to_precision 轉成符合該幣 tick 的字串，不用 Python str()。
        def _px(p):
            try:
                return ex.price_to_precision(symbol_id, p)
            except Exception:
                return format(float(p), "f")   # 備援：固定小數，避免科學記號
        sl_px_str  = _px(stop_loss)
        tp1_px_str = _px(tp1)
        tp2_px_str = _px(tp2)

        # 止損：OKX algo slTriggerPx 條件單（closeFraction=1 平全倉，無需指定張數）
        sl_side = "sell" if trade_side == "long" else "buy"
        sl_pos  = trade_side
        def _do_place_sl():
            res = _place_okx_algo_sl(inst_id=inst_id, side=sl_side, amount="0",
                                     sl_trigger_px=sl_px_str, pos_side=sl_pos)
            return res, (res.get("data") or [{}])[0].get("algoId")
        try:
            sl_result, sl_algo_id = _do_place_sl()
            if not sl_algo_id:
                # 51088：同全倉位已有TP/SL → 清掉舊algo單再重掛一次(避免裸倉)
                _scode = str((sl_result.get("data") or [{}])[0].get("sCode") or "")
                if _scode == "51088":
                    n_cxl = _okx_cancel_all_algos(inst_id)
                    time.sleep(0.3)
                    sl_result, sl_algo_id = _do_place_sl()
                    if sl_algo_id:
                        execution_report.append(f"♻️ 清掉{n_cxl}張舊TP/SL後重掛止損成功")
            if sl_algo_id:
                execution_report.append(f"🛑 止損 `{stop_loss}`")
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

        # ── TP 分批（一律張數，依精度拆半；無法拆半則 TP1 全出）─────────────
        tp1_qty = 0.0; tp2_qty = 0.0
        try:
            tp1_qty = float(ex.amount_to_precision(symbol_id, total_contracts * 0.5))
        except Exception:
            tp1_qty = round(total_contracts * 0.5, 4)
        tp2_qty = round(total_contracts - tp1_qty, 8)
        _min_amt2 = float(((mkt.get("limits") or {}).get("amount") or {}).get("min") or 0)

        if exit_strategy in ("line_full", "swing_full", "line_add"):
            # ── 整倉趨勢跟蹤：不掛任何TP，整倉持有。SL 已掛(closeFraction=1平全倉,硬底兜底)。
            #    line_full=切線突破(DH空已改line_add/30m C3多); swing_full=轉折移SL(1H MACD空);
            #    line_add=整倉切線+N型轉折遞減加碼(DH空驗+0.890/RA3.46)。
            _tag = {"line_full": "切線突破", "swing_full": "轉折移SL",
                    "line_add": "切線突破+轉折加碼"}.get(exit_strategy, "切線")
            execution_report.append(f"📈 整倉出場(不掛TP,{_tag};SL兜底)")
        elif exit_strategy in ("box_trend", "hf_1r"):
            # ── 整倉單一TP:box_trend=4R讓跑/達1R保本;hf_1r=高頻固定1R全平/達0.5R保本(不讓跑)。
            #    R掃描甜蜜點4R(EV+0.234/賺賠2.8);讓趨勢跑,crypto切線被反彈洗故不用切線。
            try:
                tp1_order = ex.create_order(
                    symbol=symbol_id, type="limit", side=exit_action,
                    amount=total_contracts, price=tp1_px_str,
                    params={"posSide": trade_side, "tdMode": MARGIN_MODE, "reduceOnly": True})
                tp1_order_id = tp1_order.get("id")
                _ttag = "高頻1R全平,達0.5R保本" if exit_strategy == "hf_1r" else "4R讓跑,達1R保本"
                execution_report.append(f"🎯 整倉TP `{tp1_px_str}`({_ttag})")
            except Exception as tp1e:
                execution_report.append(f"⚠️ 整倉TP委託失敗: {tp1e}")
        elif exit_strategy in ("tp_line", "swing_tp", "swing_tp_1h"):
            # ── TP1落袋半 + 剩半趨勢跟蹤：只掛 TP1(半倉)。
            #    tp_line=剩半切線; swing_tp=剩半轉折移SL(1H W底多); swing_tp_1h=參1H轉折(15m MACD多)。
            if tp1_qty > 0 and tp2_qty >= (_min_amt2 or 0) and tp2_qty > 0:
                try:
                    tp1_order = ex.create_order(
                        symbol=symbol_id, type="limit", side=exit_action,
                        amount=tp1_qty, price=tp1_px_str,
                        params={"posSide": trade_side, "tdMode": MARGIN_MODE, "reduceOnly": True})
                    tp1_order_id = tp1_order.get("id")
                    _tag = "沿切線" if exit_strategy == "tp_line" else "轉折移SL"
                    execution_report.append(f"🎯 TP1 `{tp1_px_str}`(剩半{_tag})")
                except Exception as tp1e:
                    execution_report.append(f"⚠️ TP1委託失敗: {tp1e}")
            else:
                # 倉小不拆半 → 退回整倉趨勢跟蹤(不掛TP)
                execution_report.append("📈 倉小不拆半→改整倉(不掛TP)")
        elif tp1_qty > 0 and tp2_qty >= (_min_amt2 or 0) and tp2_qty > 0:
            # 固定R：可拆半 TP1 / TP2 各一半
            try:
                tp1_order = ex.create_order(
                    symbol=symbol_id, type="limit", side=exit_action,
                    amount=tp1_qty, price=tp1_px_str,
                    params={"posSide": trade_side, "tdMode": MARGIN_MODE, "reduceOnly": True})
                tp1_order_id = tp1_order.get("id")
                execution_report.append(f"🎯 TP1 `{tp1_px_str}` / TP2 `{tp2_px_str}`")
            except Exception as tp1e:
                execution_report.append(f"⚠️ TP1委託失敗: {tp1e}")
            try:
                ex.create_order(
                    symbol=symbol_id, type="limit", side=exit_action,
                    amount=tp2_qty, price=tp2_px_str,
                    params={"posSide": trade_side, "tdMode": MARGIN_MODE, "reduceOnly": True})
            except Exception as tp2e:
                execution_report.append(f"⚠️ TP2委託失敗: {tp2e}")
        else:
            # 固定R：太小無法拆半 → TP1 全出、不設 TP2
            try:
                tp1_order = ex.create_order(
                    symbol=symbol_id, type="limit", side=exit_action,
                    amount=total_contracts, price=tp1_px_str,
                    params={"posSide": trade_side, "tdMode": MARGIN_MODE, "reduceOnly": True})
                tp1_order_id = tp1_order.get("id")
                execution_report.append(f"🎯 TP1全出 `{tp1_px_str}`(倉位小不拆半)")
            except Exception as tp1e:
                execution_report.append(f"⚠️ TP1委託失敗: {tp1e}")

        # ── 加入追蹤池（解決 OKX 倉位先前完全沒被 check_trailing_stops 管理的問題）──
        # 只有成功掛上止損(sl_algo_id)才追蹤；否則倉位狀態不明，不納入。
        if sl_algo_id:
            # 剩餘量：整倉(line_full/swing_full/line_add/box_trend)=全倉；其他=TP1出一半後剩的半倉
            if exit_strategy in ("line_full", "swing_full", "line_add", "box_trend", "hf_1r"):
                remaining_amt = str(total_contracts)
            else:
                remaining_amt = str(tp2_qty if tp2_qty > 0 else total_contracts)
            # key 含 exit_strategy + 毫秒:高頻平行倉與讓跑倉同秒下單不撞號(各跑各的)
            okx_tkey = f"okx_{inst_id}_{trade_side}_{exit_strategy or 'fixR'}_{int(time.time()*1000)}"
            active_real_trades[okx_tkey] = {
                "exchange":         "okx",
                "inst_id":          inst_id,
                "symbol":           symbol_id,
                "direction":        trade_side,
                "entry_price":      str(executed_average_price),
                "sl_algo_id":       sl_algo_id,
                "tp1_order_id":     tp1_order_id,
                "tp1_hit":          False,
                "current_sl":       stop_loss,
                "remaining_amount": remaining_amt,
                "pos_side":         trade_side,
                "risk_dist":        abs(executed_average_price - stop_loss),
                "tf_id":            tf_id,
                # 金字塔基礎張數 = 未含CVD加碼的基礎單位(total_contracts已×position_scale,
                # 故除回去)。讓加碼只加1個基礎單位,不疊CVD的×1.5,使MDD與回測(48%)一致。
                "init_contracts":   round(total_contracts / max(position_scale, 1e-9), 8),
                "pyramid_added":    False,             # 是否已 +1R 加碼過
                "pyramid_eligible": pyramid_eligible,  # 僅驗證過的多單(C3/W底)可加碼
                "exit_strategy":    exit_strategy,     # ""固定R/line_full切線/swing_*移SL/line_add加碼
                "entry_ts":         int(time.time()),  # 開倉時戳(切線/移SL只看進場後的K)
                "full_contracts":   str(total_contracts),  # 整倉張數(市價平用)
                "add_count":        0,                 # line_add:已N型轉折加碼次數(守3)
                "add_swings_n":     0,                 # line_add:已處理的順勢轉折數(避免同轉折重複加)
            }
            save_active_trades()   # 持久化
            execution_report.append("📋 已納入保本追蹤")

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
    elif method == "DELETE":
        return requests.delete(url, headers=headers, timeout=timeout)
    else:
        return requests.post(url, headers=headers, timeout=timeout)

def execute_bingx_trade_pipeline(symbol_id: str, trade_side: str, entry_price: float,
                                  stop_loss: float, tp1: float, tp2: float,
                                  exit_mode: str = "fixed", tf_id: str = "15m",
                                  position_scale: float = 1.0, exit_strategy: str = "",
                                  allow_stack: bool = False) -> None:
    """
    BingX 永續合約下單
    position_scale：倉位縮放係數（1.0=正常，0.5=半倉，由 dynamic_sl_tp 傳入）
    exit_strategy：與OKX一致(line_full/line_add/swing_full=不掛TP整倉; tp_line/swing_tp/
                   swing_tp_1h=掛TP1半倉; ""=固定R)。趨勢跟蹤出場由check_trailing BingX段處理。
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

        # 分段複利下注 + 小資金自動縮（與 OKX 一致）：基準不超過實際餘額
        _level = max(0, int((wallet_usdt - LADDER_BASE_USDT) // LADDER_STEP_USDT))
        _ladder_base = LADDER_BASE_USDT * (1 + _level)
        _eff_base = min(_ladder_base, wallet_usdt)     # 小資金用實際餘額
        risk_usdt = _eff_base * RISK_PCT * position_scale
        sl_dist_pct = abs(entry_price - stop_loss) / entry_price
        if sl_dist_pct <= 0.0001:
            dc_log("⚠️ BingX 止損距離過小，跳過下單")
            return

        # ── 防同幣同向重複加倉（與 OKX 一致;allow_stack=同訊號平行hf_1r倉豁免)──────
        _ps = "LONG" if trade_side == "long" else "SHORT"
        if allow_stack:
            _ps = None   # 跳過下方查倉去重
        try:
            if _ps is not None:
                pos_q = _bingx_request("GET", "/openApi/swap/v2/user/positions",
                                       {"symbol": bingx_symbol}, headers).json()
                for _pp in (pos_q.get("data") or []):
                    if _pp.get("positionSide") == _ps and abs(float(_pp.get("positionAmt") or 0)) > 0:
                        dc_log(f"⚠️ BingX 跳過 [{bingx_symbol}]：已有 {trade_side} 倉，不重複加倉")
                        return
        except Exception as _pos_err:
            print(f"[BingX] 查持倉失敗（不阻擋下單）: {_pos_err}")

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
            # 2026-06-13:全倉+風險制下注→升槓桿不改倉位/風險,只少鎖保證金(全倉下強平反而更遠)。
            #   直接用 BingX 幣種實際上限(很多幣>150,黃金500x),不再壓到 MAX_LEVERAGE。
            #   coin_max_lev 本身即 BingX 該幣風險上限(隨波動率設),沿用安全。上限1000防API異常。
            leverage = max(1, min(coin_max_lev, 1000))
        else:
            # 逐倉模式：維持原本動態槓桿邏輯
            leverage = max(1, min(int(50.0 / (sl_dist_pct * 100.0)), MAX_LEVERAGE))

        # 保證金 = 倉位價值 ÷ 槓桿（全倉用最大槓桿後此值即實際新倉保證金）
        # 鐵律：倉位 = risk_usdt ÷ 止損距離%，不夾保證金上限（與 OKX 一致），
        # 確保每單觸止損精準 = risk_usdt。保證金不足由下方「可用 < margin」檢查跳過。
        margin = position_value / leverage

        if avail_usdt < margin:
            dc_log(f"⚠️ BingX 保證金不足：可用 {avail_usdt:.2f}，需要 {margin:.2f}")
            return

        # ── BingX 風險率保護（帳戶級）────────────────────────────────────────
        # BingX 風險率 = 已用保證金 / 帳戶淨值，越高越危險，接近 100% 會爆倉。
        # BINGX_MAX_RISK_RATE 全域可調(!setbingxrisk)，預設 70%。
        equity        = float(bal.get("equity") or total_usdt)
        used_margin   = equity - avail_usdt
        projected_used = used_margin + margin
        projected_risk_rate = projected_used / equity if equity > 0 else 1.0
        if projected_risk_rate > BINGX_MAX_RISK_RATE:
            dc_log(f"⚠️ BingX 跳過 [{symbol_id}]：風險率預估 {projected_risk_rate:.0%} > {BINGX_MAX_RISK_RATE:.0%}"
                   f"（已用 {used_margin:.2f} + 新倉 {margin:.2f} / 淨值 {equity:.2f}）")
            return

        # ── BingX 強平守門員（逐筆，與 OKX 一致）────────────────────────────
        # 估算強平距離 = (可用保證金 + 本倉保證金) / 倉位名義，若停損距 ≥ 強平距×0.85
        # 代表強平會在止損前觸發（全倉模式連帶清掉其他倉）→ 直接跳過。
        est_liq_dist_bx = (avail_usdt + margin) / position_value if position_value > 0 else 0.0
        if sl_dist_pct >= est_liq_dist_bx * 0.85:
            dc_log(f"⚠️ BingX 跳過 [{symbol_id}]：強平估算觸發在止損前，保護帳戶不下單"
                   f"（停損距 {sl_dist_pct*100:.2f}% ≥ 估強平距 {est_liq_dist_bx*100:.2f}%×0.85；"
                   f"可用 {avail_usdt:.2f}U／倉位 {position_value:.2f}U）")
            return

        # ★先設保證金模式=全倉(CROSSED)。marginType 是獨立端點,之前誤塞在 /trade/leverage 裡被
        #   BingX 忽略→實際一直用合約預設逐倉(ISOLATED)。此為「BingX 下到逐倉」根因修正(2026-06-16)。
        #   已有持倉的幣 BingX 會拒改(無妨,既有倉不轉);新倉/無倉幣會正確設成全倉。
        try:
            _bingx_request("POST", "/openApi/swap/v2/trade/marginType", {
                "symbol": bingx_symbol, "marginType": "CROSSED"
            }, headers)
        except Exception as _mt_e:
            print(f"[BingX] {bingx_symbol} 設全倉失敗(可能已有持倉,既有倉不轉): {_mt_e}", flush=True)

        # 設定槓桿
        _bingx_request("POST", "/openApi/swap/v2/trade/leverage", {
            "symbol": bingx_symbol,
            "side": "LONG" if trade_side == "long" else "SHORT",
            "leverage": str(leverage),
        }, headers)

        # 計算張數
        qty = round(position_value / entry_price, 4)
        side_str = "BUY" if trade_side == "long" else "SELL"
        pos_side = "LONG" if trade_side == "long" else "SHORT"
        exit_side = "SELL" if trade_side == "long" else "BUY"

        # ★ 先驗證 BingX 是否支援此合約(很多幣 BingX 沒有,如 TON-USDT 報109425)→ 靜默跳過
        # 不刷 Discord(OKX已下單,BingX缺該幣是常態),只記 Railway log。
        price_check = _bingx_request("GET", "/openApi/swap/v2/quote/price", {
            "symbol": bingx_symbol
        }, headers).json()
        if price_check.get("code", 0) != 0 or not (price_check.get("data") or {}).get("price"):
            print(f"[BingX] {bingx_symbol} 無此合約(BingX不支援該幣)，跳過 BingX 下單", flush=True)
            return
        # BingX stopPrice 必須：做多 < 當前價；做空 > 當前價
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

        # ── TP 掛單依 exit_strategy（與OKX一致）──────────────────────────────
        half_qty = round(actual_qty / 2, 4)
        bingx_tp1_order_id = ""
        if exit_strategy in ("line_full", "line_add", "swing_full"):
            # 整倉趨勢跟蹤：不掛TP,整倉持有,由check_trailing切線/移SL出場(SL兜底)
            pass
        elif exit_strategy in ("box_trend", "hf_1r"):
            # 整倉單一TP:box_trend=4R/達1R保本;hf_1r=高頻固定1R全平/達0.5R保本。TP全倉掛交易所自動成交。
            tp1_r = _bingx_request("POST", "/openApi/swap/v2/trade/order", {
                "symbol": bingx_symbol, "side": exit_side, "positionSide": pos_side,
                "type": "TAKE_PROFIT_MARKET", "stopPrice": str(round(tp1, 5)),
                "quantity": str(round(actual_qty, 4)), "workingType": "MARK_PRICE"
            }, headers)
            bingx_tp1_order_id = tp1_r.json().get("data", {}).get("order", {}).get("orderId", "")
        elif exit_strategy in ("tp_line", "swing_tp", "swing_tp_1h"):
            # TP1落袋半 + 剩半趨勢跟蹤：只掛TP1半倉
            tp1_r = _bingx_request("POST", "/openApi/swap/v2/trade/order", {
                "symbol": bingx_symbol, "side": exit_side, "positionSide": pos_side,
                "type": "TAKE_PROFIT_MARKET", "stopPrice": str(round(tp1, 5)),
                "quantity": str(half_qty), "workingType": "MARK_PRICE"
            }, headers)
            bingx_tp1_order_id = tp1_r.json().get("data", {}).get("order", {}).get("orderId", "")
        else:
            # 固定R：TP1半倉 + (fixed)TP2半倉
            tp1_r = _bingx_request("POST", "/openApi/swap/v2/trade/order", {
                "symbol": bingx_symbol, "side": exit_side, "positionSide": pos_side,
                "type": "TAKE_PROFIT_MARKET", "stopPrice": str(round(tp1, 5)),
                "quantity": str(half_qty), "workingType": "MARK_PRICE"
            }, headers)
            bingx_tp1_order_id = tp1_r.json().get("data", {}).get("order", {}).get("orderId", "")
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
        # 剩餘量：整倉趨勢跟蹤=全倉；其他=半倉(TP1出後剩的)
        _rem_qty = round(actual_qty, 4) if exit_strategy in ("line_full","line_add","swing_full","box_trend","hf_1r") else half_qty
        # 加碼基礎量(line_add)：未疊CVD加碼的基礎單位
        _base_qty = round(actual_qty / max(position_scale, 1e-9), 4)
        # key 含 exit_strategy + 毫秒:高頻平行倉與讓跑倉不撞號(各跑各的)
        trade_key = f"bingx_{bingx_symbol}_{trade_side}_{exit_strategy or 'fixR'}_{int(time.time()*1000)}"
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
            "remaining_qty":    str(_rem_qty),
            "full_qty":         str(round(actual_qty, 4)),  # 全倉量(TP1前提前保本用)
            "pos_side":         pos_side,
            "exit_side":        exit_side,
            "headers":          headers,
            "risk_dist":        abs(float(entry_price) - stop_loss),
            "tf_id":            tf_id,
            "exit_strategy":    exit_strategy,
            "entry_ts":         int(time.time()),
            "init_qty":         _base_qty,    # line_add 加碼基礎量
            "add_count":        0,
            "add_swings_n":     0,
        }
        save_active_trades()   # 持久化：新倉立即存檔，重啟可還原
        dc_log(f"📋 BingX 倉位已加入保本追蹤：{bingx_symbol} {trade_side} SL={stop_loss} qty={actual_qty:.4f}")

    except Exception as e:
        dc_log(f"❌ **BingX 下單失敗**: {e}")


def _mai_line_breakout(ex, trade) -> bool:
    """
    麥門切線出場（PDF 正版）：連「下降的頭」(空)/「上升的腳」(多)成切線，
    價格「實體收盤突破切線」→ 市價平剩餘倉。回傳 True=已平倉。
    頭/腳轉折確認(收盤即確認，對齊回測 _mai_line_v2.py)：
      空頭頭(假突破): hi[f]>hi[f-1] AND cl[f]<lo[f-1]
      多頭腳(破底翻): lo[f]<lo[f-1] AND cl[f]>hi[f-1]
    只看「進場後」的 K（entry_ts 之後），與回測從進場點累積轉折一致。
    """
    try:
        inst_id   = trade["inst_id"]
        symbol    = trade["symbol"]
        direction = trade["direction"]
        tf        = trade.get("tf_id", "15m")
        name      = symbol.split("/")[0]

        df = fetch_market_candles(inst_id, tf, fetch_limit=120)
        if df.empty or len(df) < 6:
            return False
        # 只保留進場後的 K（不夠就用全部，避免剛開倉立刻判斷）
        ets = trade.get("entry_ts")
        if ets:
            try:
                cutoff = pd.Timestamp(int(ets), unit="s", tz="UTC")
                sub = df[df.index >= cutoff]
                # 只看進場/接管後K線。不足6根→這輪不動作(等積累,不回退全120避免取進場前資料)
                if len(sub) < 6:
                    return False
                df = sub
            except Exception:
                pass
        hi = df["high"].values; lo = df["low"].values; cl = df["close"].values
        n = len(df)

        # 頭/腳轉折，只收「更低的頭(空)/更高的腳(多)」=順趨勢序列
        swings = []
        for f in range(1, n):
            if direction == "short":
                if hi[f] > hi[f-1] and cl[f] < lo[f-1]:
                    if not swings or hi[f] < swings[-1][1]:
                        swings.append((f, hi[f]))
            else:
                if lo[f] < lo[f-1] and cl[f] > hi[f-1]:
                    if not swings or lo[f] > swings[-1][1]:
                        swings.append((f, lo[f]))
        if len(swings) < 2:
            return False
        (a, pa), (b, pb) = swings[-2], swings[-1]
        if b <= a:
            return False
        proj = pb + (pb - pa) / (b - a) * ((n - 1) - b)   # 切線投影到當前根
        broke = (cl[-1] > proj) if direction == "short" else (cl[-1] < proj)
        if not broke:
            return False

        # 實體收盤突破切線 → 市價平剩餘倉
        rem = float(trade.get("remaining_amount", 0) or 0)
        if rem <= 0:
            return False
        exit_side = "buy" if direction == "short" else "sell"
        ex.create_market_order(
            symbol=symbol, side=exit_side, amount=rem,
            params={"posSide": direction, "tdMode": MARGIN_MODE, "reduceOnly": True})
        _cancel_okx_algo_order(inst_id, trade.get("sl_algo_id"))
        msg = (f"📐 {name} 麥門切線突破（{'空' if direction=='short' else '多'}），"
               f"市價平剩餘 {rem} 張")
        dc_log(msg); tg_log(msg)
        print(f"[MaiLine] {name} proj={proj:.6f} close={cl[-1]:.6f} → 平倉")
        return True
    except Exception as e:
        print(f"[MaiLine] {trade.get('symbol')} 切線出場失敗: {e}")
        return False


def _n_shape_turn(hi, lo, cl, direction):
    """N字型轉折點(三波,收盤突破確認)——用戶定義。
    做多:波1漲(高H)→波2回調(低L)→波3「收盤>波1高H」則N成型,該回調低L為轉折點。
         連續N的回調低構成轉折序列,回傳最有利(最高)的確認回調低。
    做空反之:波1跌(低L)→波2反彈(高H)→波3「收盤<波1低L」成型,反彈高H為轉折,回傳最低的確認反彈高。
    無成型則回傳 None。"""
    n = len(cl)
    if n < 3:
        return None
    best = None
    if direction == "long":
        phase = "up"; H = hi[0]; L = None
        for i in range(1, n):
            if phase == "up":
                if hi[i] > H: H = hi[i]              # 更新波1高
                if cl[i] < lo[i-1]:                  # 收盤破前低 → 回調開始(進波2)
                    phase = "down"; L = lo[i]
            else:                                     # 波2下跌
                if lo[i] < L: L = lo[i]              # 更新波2低
                if cl[i] > H:                        # 波3收盤突破波1高 → N成型
                    best = L if best is None else max(best, L)
                    phase = "up"; H = hi[i]          # 波3變新波1
    else:  # short
        phase = "down"; L = lo[0]; H = None
        for i in range(1, n):
            if phase == "down":
                if lo[i] < L: L = lo[i]              # 更新波1低
                if cl[i] > hi[i-1]:                  # 收盤過前高 → 反彈開始(進波2)
                    phase = "up"; H = hi[i]
            else:                                     # 波2反彈
                if hi[i] > H: H = hi[i]              # 更新波2高
                if cl[i] < L:                        # 波3收盤跌破波1低 → 成型
                    best = H if best is None else min(best, H)
                    phase = "down"; L = lo[i]
    return best


def _swing_trail_update_sl(ex, trade, ref_tf=None) -> bool:
    """
    移動停利（切線PDF p11「用最新出現的高/低點修改保利點」）：
    用最新轉折點(多頭最新「腳VV」/空頭最新「頭AA」)移動 SL，只往有利方向。
    出場靠交易所 SL algo 觸發(價格碰移動後SL自動平倉)。回傳 True=有更新SL。
    ref_tf：移SL參考的時框(預設用倉位時框)。15m策略傳 ref_tf="1H" 用大級別轉折避雜訊。
    WF:1H W底多 swing_tp驗+0.165、1H MACD空 swing_full驗+0.251、15m MACD多參1H移SL驗+0.142。
    """
    try:
        inst_id   = trade["inst_id"]
        symbol    = trade["symbol"]
        direction = trade["direction"]
        tf        = ref_tf or trade.get("tf_id", "1H")
        if tf in ("adopted", "", None): tf = "1H"   # 接管倉改用1H避雜訊
        name      = symbol.split("/")[0]

        df = fetch_market_candles(inst_id, tf, fetch_limit=120)
        if df.empty or len(df) < 6:
            return False
        ets = trade.get("entry_ts")
        if ets:
            try:
                cutoff = pd.Timestamp(int(ets), unit="s", tz="UTC")
                sub = df[df.index >= cutoff]
                # 只看進場/接管後K線。不足6根→這輪不動作(等積累,不回退全120避免取進場前資料)
                if len(sub) < 6:
                    return False
                df = sub
            except Exception:
                pass
        hi = df["high"].values; lo = df["low"].values; cl = df["close"].values
        n = len(df)

        # 移SL用「pivot 擺盪點」(前後2根局部極值)。回測:pivot 勝 N字型(N MDD暴增58~71%)→回退。
        # ★bugfix 2026-06-21:先濾「市價合法側」再挑最緊pivot。舊版先挑全窗最極端pivot,噴後整理時
        #   它落在市價錯側→整個更新被否決凍住(該噴後跟漲的整理段不跟,AXS案例根因)。
        PV = 2
        cur_sl = float(trade.get("current_sl", 0) or 0)
        cur_px = float(cl[-1])
        last_swing = None
        for j in range(PV, n - PV):
            if direction == "long":
                if lo[j] == lo[j-PV:j+PV+1].min() and lo[j] < cur_px:   # 只取市價下方(合法側)
                    if last_swing is None or lo[j] > last_swing: last_swing = lo[j]
            else:
                if hi[j] == hi[j-PV:j+PV+1].max() and hi[j] > cur_px:   # 只取市價上方(合法側)
                    if last_swing is None or hi[j] < last_swing: last_swing = hi[j]
        if last_swing is None:
            print(f"[OKX-trail] {name} 合法側無pivot,不移(px={cur_px})", flush=True)
            return False
        print(f"[OKX-trail] {name} {direction} pivot={last_swing} cur_sl={cur_sl} px={cur_px} ets={trade.get('entry_ts')}", flush=True)
        # 只往有利方向移（多頭往上、空頭往下）
        if direction == "long"  and last_swing <= cur_sl:
            print(f"[OKX-trail] {name} pivot≤cur_sl 不更優,不移", flush=True); return False
        if direction == "short" and last_swing >= cur_sl:
            print(f"[OKX-trail] {name} pivot≥cur_sl 不更優,不移", flush=True); return False
        # 合法側保護:新SL須在市價保護側,否則OKX拒單。★放在cancel前:否則先取消舊SL再
        # 掛無效新SL→裸倉。
        if direction == "short" and last_swing <= cur_px:
            print(f"[OKX-trail] {name} pivot≤市價 錯側,不移", flush=True); return False
        if direction == "long"  and last_swing >= cur_px:
            print(f"[OKX-trail] {name} pivot≥市價 錯側,不移", flush=True); return False

        # ★2026-07-06 改 place-before-cancel(與BingX _bingx_replace_sl對齊):舊版先取消舊SL再掛新,
        #   掛失敗→裸倉且不回復。改為先掛新SL,51088(已有algo)才取消舊再掛一次;最終失敗保留舊SL不裸倉。
        exit_side = "sell" if direction == "long" else "buy"
        try: sl_px = ex.price_to_precision(symbol, last_swing)
        except Exception: sl_px = format(last_swing, "f")
        _old_id = trade.get("sl_algo_id")
        def _place_trail():
            return _place_okx_algo_sl(
                inst_id=inst_id, side=exit_side,
                amount=trade["remaining_amount"], sl_trigger_px=sl_px, pos_side=direction)
        res = _place_trail()
        nid = (res.get("data") or [{}])[0].get("algoId")
        if not nid:
            _sc = str((res.get("data") or [{}])[0].get("sCode") or "")
            if _sc == "51088":                      # 同倉位已有TP/SL → 先撤舊再掛(此時短暫無SL,立即補掛)
                _cancel_okx_algo_order(inst_id, _old_id); _old_id = None
                time.sleep(0.3)
                res = _place_trail()
                nid = (res.get("data") or [{}])[0].get("algoId")
        if nid:
            if _old_id: _cancel_okx_algo_order(inst_id, _old_id)
            trade["sl_algo_id"] = nid
            trade["current_sl"] = last_swing
            msg = f"📐 {name} 轉折移動停損 → {last_swing}"
            dc_log(msg)
            print(f"[SwingTrail] {name} SL→{last_swing}")
            return True
        print(f"[SwingTrail] {name} 掛新SL失敗(保留舊SL,不裸倉) resp={res}", flush=True)
        return False
    except Exception as e:
        print(f"[SwingTrail] {trade.get('symbol')} 移SL失敗: {e}")
        return False


def _is_rev_top(o, h, l, c, po, ph, pl, pc):
    """空頭反轉K(加碼點,任一):看跌吞噬 / 流星長上影(打勾) / 假突破收破前低。"""
    return ((pc > po and c < o and c <= po and o >= pc) or
            (abs(c-o) > 0 and (h - max(o, c)) >= abs(c-o) * 2) or
            (h > ph and c < pl))
def _is_rev_bot(o, h, l, c, po, ph, pl, pc):
    """多頭反轉K(加碼點,任一):看漲吞噬 / 錘子長下影 / 破底翻收過前高。"""
    return ((pc < po and c > o and c >= po and o <= pc) or
            (abs(c-o) > 0 and (min(o, c) - l) >= abs(c-o) * 2) or
            (l < pl and c > ph))

MAI_ADD_MAX = 3   # N型轉折加碼上限(守加碼3,加碼.pdf)
def _mai_add_on_swing(ex, trade) -> bool:
    """
    N型轉折順勢加碼（加碼.pdf）：整倉切線基礎上，每出現一個新的順勢轉折
    (空頭更低的頭AA / 多頭更高的腳VV) 加 0.5×首單(遞減金字塔)，守加碼3。
    加碼走強平守門員(合併部位強平在停損前→不加,防爆倉)。
    SL 用 closeFraction=1 觸發平全倉,加碼後自動覆蓋,不需重掛。整組由切線突破出場。
    WF:DH空遞減加碼驗+0.890/MDD26%/RA3.46(>無加碼3.15)。回傳 True=有加碼。
    """
    try:
        symbol = trade["symbol"]; inst_id = trade["inst_id"]; direction = trade["direction"]
        tf = trade.get("tf_id", "15m"); name = symbol.split("/")[0]
        add_count = int(trade.get("add_count", 0))
        if add_count >= MAI_ADD_MAX:
            return False
        base_ct = float(trade.get("init_contracts") or 0)
        if base_ct <= 0:
            return False

        # 進場後順勢轉折序列(頭/腳)
        df = fetch_market_candles(inst_id, tf, fetch_limit=120)
        if df.empty or len(df) < 6:
            return False
        ets = trade.get("entry_ts")
        if ets:
            try:
                cutoff = pd.Timestamp(int(ets), unit="s", tz="UTC")
                sub = df[df.index >= cutoff]
                # 只看進場/接管後K線。不足6根→這輪不動作(等積累,不回退全120避免取進場前資料)
                if len(sub) < 6:
                    return False
                df = sub
            except Exception:
                pass
        op = df["open"].values; hi = df["high"].values; lo = df["low"].values; cl = df["close"].values; n = len(df)
        # 加碼點=多型態反轉K(吞噬OR上影OR假突破),只收順勢更低頭/更高腳。
        # 回測:多型態加碼 EV+1.177 > 單一假突破+0.722(加碼要多抓轉折機會,與切線「鈍」相反)。
        swings = []
        for f in range(1, n):
            if direction == "short":
                if _is_rev_top(op[f],hi[f],lo[f],cl[f],op[f-1],hi[f-1],lo[f-1],cl[f-1]):
                    if not swings or hi[f] < swings[-1]: swings.append(hi[f])
            else:
                if _is_rev_bot(op[f],hi[f],lo[f],cl[f],op[f-1],hi[f-1],lo[f-1],cl[f-1]):
                    if not swings or lo[f] > swings[-1]: swings.append(lo[f])
        cur_n = len(swings)
        if cur_n <= int(trade.get("add_swings_n", 0)):
            return False   # 無新順勢轉折，不加

        # 加碼張數 = 0.5×首單(遞減)
        try: add_amt = float(ex.amount_to_precision(symbol, base_ct * 0.5))
        except Exception: add_amt = base_ct * 0.5
        if add_amt <= 0:
            trade["add_swings_n"] = cur_n; return False

        cur = float(ex.fetch_ticker(symbol).get("last") or 0)
        if cur <= 0: return False
        mkt = ex.market(symbol); ct_val = float(mkt.get("contractSize", 1.0) or 1.0)
        # 強平守門員(合併部位)：估強平距 < 停損距 → 不加(防爆倉)
        try:
            cur_ct = 0.0
            for _p in ex.fetch_positions([symbol]):
                if _p.get("symbol") == symbol and _p.get("side") == direction:
                    cur_ct = abs(float(_p.get("contracts") or 0)); break
            if cur_ct <= 0: cur_ct = float(trade.get("remaining_amount") or base_ct)
        except Exception:
            cur_ct = base_ct
        comb_val = (cur_ct + add_amt) * cur * ct_val
        try: avail = float(ex.fetch_balance().get("USDT", {}).get("free", 0.0))
        except Exception: avail = 0.0
        try: lev = int(float(((mkt.get("limits", {}) or {}).get("leverage", {}) or {}).get("max") or MAX_LEVERAGE))
        except Exception: lev = MAX_LEVERAGE
        comb_margin = comb_val / max(1, min(lev, MAX_LEVERAGE))
        slv = float(trade.get("current_sl") or 0)
        sl_dist_pct = abs(slv - cur) / cur if cur else 1.0
        est_liq = (avail + comb_margin) / comb_val if comb_val else 0.0
        if sl_dist_pct >= est_liq * PYRAMID_LIQ_BUF:
            dc_log(f"⚠️ 轉折加碼跳過 [{name}]：加碼後強平會在停損前，防爆倉不加")
            trade["add_swings_n"] = cur_n; return False

        # 市價加碼(同方向加倉,OKX 自動併倉更新均價)
        add_action = "buy" if direction == "long" else "sell"
        ex.create_market_order(symbol=symbol, side=add_action, amount=add_amt,
                               params={"posSide": direction, "tdMode": MARGIN_MODE})
        # 更新追蹤：remaining 累加(切線出場平全部)、add_count、已處理轉折數
        new_rem = float(trade.get("remaining_amount") or 0) + add_amt
        trade["remaining_amount"] = str(new_rem)
        trade["add_count"] = add_count + 1
        trade["add_swings_n"] = cur_n
        msg = (f"📈 {name} N型轉折加碼#{add_count+1}（{'空' if direction=='short' else '多'}）"
               f"+{add_amt}張(遞減半單,守{MAI_ADD_MAX})；SL closeFraction=1 覆蓋全倉,整組切線出")
        dc_log(msg)
        print(f"[MaiAdd] {name} add#{add_count+1} +{add_amt}張")
        return True
    except Exception as e:
        print(f"[MaiAdd] {trade.get('symbol')} 加碼失敗: {e}")
        return False


# ══ BingX 趨勢跟蹤(切線/移SL/加碼) — 與OKX對齊,BingX用OKX公開K偵測轉折(跨所近似)══════
def _bingx_swings(symbol_ccxt, tf, entry_ts, direction):
    """用 OKX 公開K(進場後)偵測順勢轉折(空頭頭AA/多頭腳VV)。回傳 (swings[(f,price)], df)。"""
    inst_okx = OKX_SWAP.get(symbol_ccxt, symbol_ccxt)
    df = fetch_market_candles(inst_okx, tf, fetch_limit=120)
    if df.empty or len(df) < 6:
        return [], df
    if entry_ts:
        try:
            cutoff = pd.Timestamp(int(entry_ts), unit="s", tz="UTC")
            sub = df[df.index >= cutoff]
            # ★bugfix 2026-06-18:進場後不足6根→回傳空(不畫切線),對齊OKX _mai_line_breakout。
            #   舊版會fall through用「進場前」120根舊轉折畫切線,害剛開倉<3h的單被誤砍(SOL 30m多案例)。
            if len(sub) < 6:
                return [], df
            df = sub
        except Exception:
            pass
    hi = df["high"].values; lo = df["low"].values; cl = df["close"].values; n = len(df)
    sw = []
    for f in range(1, n):
        if direction == "short":
            if hi[f] > hi[f-1] and cl[f] < lo[f-1]:
                if not sw or hi[f] < sw[-1][1]: sw.append((f, hi[f]))
        else:
            if lo[f] < lo[f-1] and cl[f] > hi[f-1]:
                if not sw or lo[f] > sw[-1][1]: sw.append((f, lo[f]))
    return sw, df

def _bingx_replace_sl(trade, sl_price, qty):
    """BingX 換止損:★place-before-cancel★——先挂新止損、確認成功,才取消舊止損。
    絕不先清空再挂(舊版先清→挂失敗就裸倉,而且會把用戶手動止損清掉留下沒保護)。
    closePosition 整倉平避免 110424(帶量止損名義超可用)。
    109420(position not exist):此 positionSide 無倉位→自動試另一持倉模式(BOTH↔LONG/SHORT)。
    回傳新orderId,或 None(=沒換成,舊/手動止損原樣保留,絕不裸倉)。"""
    sym = trade["inst_id"]; hdr = trade["headers"]; pos = trade["pos_side"]
    # 1) 先記下現有止損 orderId(稍後新單確認成功才清),查失敗也照樣嘗試挂新(不因查單失敗而不保護)
    old_oids = []
    try:
        oo = _bingx_request("GET", "/openApi/swap/v2/trade/openOrders", {"symbol": sym}, hdr).json()
        _ords = oo.get("data") or {}
        if isinstance(_ords, dict): _ords = _ords.get("orders") or []
        old_oids = [o.get("orderId") for o in _ords
                    if str(o.get("type", "")).upper() in ("STOP_MARKET", "STOP")]
    except Exception as _e:
        print(f"[BingX-SL] {sym} 查舊單失敗(仍嘗試挂新): {_e}", flush=True)
    # 2) ★先挂新止損(closePosition 整倉)。失敗→保留舊/手動止損,return None(不裸倉)。
    def _post_sl(ps, use_qty=True):
        # ★bugfix 2026-06-20:帶 quantity(BCH等需要,否則109400「quantity or stopPrice is must」→移SL一直失敗)。
        #   110424(名義超可用)時改 closePosition 整倉平 fallback。
        _p = {"symbol": sym, "side": trade["exit_side"], "positionSide": ps,
              "type": "STOP_MARKET", "stopPrice": format(float(sl_price), "f"),
              "workingType": "MARK_PRICE"}
        if use_qty and qty and float(qty) > 0:
            _p["quantity"] = str(qty)
        else:
            _p["closePosition"] = "true"
        return _bingx_request("POST", "/openApi/swap/v2/trade/order", _p, hdr).json()
    r = _post_sl(pos)
    if r.get("code", 0) == 110424:   # 名義超可用→改整倉 closePosition
        r = _post_sl(pos, use_qty=False)
    if r.get("code", 0) == 109420:   # 此 positionSide 查無倉位→持倉模式不符,試另一種
        alt = "BOTH" if pos in ("LONG", "SHORT") else ("SHORT" if trade["direction"] == "short" else "LONG")
        r_alt = _post_sl(alt)
        if r_alt.get("code", 0) == 0:
            print(f"[BingX-SL] {sym} positionSide {pos}→{alt} 修正成功", flush=True)
            trade["pos_side"] = alt   # 記住正確模式,下次直接用
            r = r_alt
    if r.get("code", 0) != 0:
        print(f"[BingX-SL] {sym} 挂新止損失敗(保留舊止損,不裸倉) resp={r}", flush=True)
        return None
    new_id = r.get("data", {}).get("order", {}).get("orderId", "")
    # 3) 新止損已成功 → 逐個 DELETE 取消舊的(留下新單)。清不掉不致命(dedup 下輪再清,不裸倉)。
    n_cxl = 0
    for oid in old_oids:
        if not oid or str(oid) == str(new_id): continue
        try:
            cj = _bingx_request("DELETE", "/openApi/swap/v2/trade/order",
                                {"symbol": sym, "orderId": oid}, hdr).json()
            if cj.get("code", -1) == 0: n_cxl += 1
        except Exception: pass
    print(f"[BingX-SL] {sym} 換止損成功→{sl_price}(清舊{n_cxl}/{len(old_oids)})", flush=True)
    return new_id

def _px_for_bingx(ex, trade):
    """BingX 倉位取現價:先試 OKX 報價(跨所近似),OKX 沒這幣(如 H/TAO)→用 BingX 自己的報價。
    修:H/TAO 等 BingX 獨有幣,OKX 無 market→ex.fetch_ticker 報錯→保本/移SL失效。"""
    try:
        p = float(ex.fetch_ticker(trade["symbol"]).get("last") or 0)   # OKX 跨所報價
        if p > 0: return p
    except Exception: pass
    try:
        r = _bingx_request("GET", "/openApi/swap/v2/quote/price",
                           {"symbol": trade["inst_id"]}, trade["headers"]).json()
        return float((r.get("data") or {}).get("price") or 0)
    except Exception:
        return 0.0


# ── 山寨多單 OI降早出(2026-06-18,預設關)──────────────────────────────────────
# 用戶COAI/WLD虧損的出場端解:山寨多單獲利中,若OI開始降(主力出貨)+價在跌→在崩盤吐回前先跑。
# 參數對齊回測 _oi_exit_alt.py:獲利中(cl>entry) 且 oi[-1]<oi[-4](降3根) 且 cl[-1]<cl[-2]。
# WF山寨 +0.385→+0.445/勝67%/MDD↓(主流上害,故限非主流)。全程guard,任何失敗回False不影響原移SL/平倉。
OI_EARLY_EXIT_ENABLED = True     # 山寨多單OI降早出啟用(用戶決定一次上;guard完整、限非主流、參數對齊回測)
HF_1R_ENABLED = True             # 高頻固定1R平行層:現役3格(1H C3空/1H MACD空/15m MACD多)訊號成立時多開一筆固定1R/0.5R保本獨立倉,各跑各的
HF_SHORT_ENABLED = False         # ★2026-07-06 關閉高頻層15m/30m MACD空:_bt_hf15_regime.py重測主流3幣23Q4~24Q3四期,
                                 #   live閘(4H e200斜率)EV-0.020/換4H e50閘-0.004/雙閘+0.002=全在零附近,無edge;
                                 #   當初+0.103/勝59%是2024Q2單期(該期我重測+0.097吻合,其他期全負)。7/3、7/5 BTC空實盤雙停損即此層。多單(swing_full讓跑)不受影響
HF_MAJORS_ONLY = True            # ★2026-06-21 瘦身止血:HF MACD層限主流(BTC/ETH/SOL)。我測15m MACD固定1R全層負勝50%,山寨裸MACD是訊號爆量+流血主因;限主流砍~80%量、止血、對齊「動能限主流」。設False回全市值

def _oi_drop_exit_long(trade) -> bool:
    """山寨多單OI降早出:獲利中+OI降3根+價在跌→True(該平)。失敗一律False。"""
    try:
        if not OI_EARLY_EXIT_ENABLED: return False
        if trade.get("direction") != "long": return False
        symbol_item = trade.get("symbol", "")
        if symbol_item.split("/")[0] in ("BTC", "ETH", "SOL"): return False   # 主流上害,只山寨
        tf_id = trade.get("tf_id", "1H")
        if tf_id in ("adopted", "", None): tf_id = "1H"
        bar = BAR_TO_CONA.get(tf_id); cona = CONA_PERP.get(symbol_item)
        if not bar or not cona: return False
        entry = float(trade["entry_price"])
        inst_okx = OKX_SWAP.get(symbol_item, symbol_item)
        df = fetch_market_candles(inst_okx, tf_id, fetch_limit=10)
        if df.empty or len(df) < 3: return False
        cl = df["close"].values
        if cl[-1] <= entry: return False           # 只在獲利中
        if not (cl[-1] < cl[-2]): return False     # 價在跌
        end_ts = int(time.time() * 1000); start_ts = end_ts - BAR_SECONDS[tf_id] * 12 * 1000
        oi = fetch_open_interest_series(cona, bar, start_ts, end_ts)
        if len(oi) < 4: return False
        return bool(oi.iloc[-1] < oi.iloc[-4])      # OI降3根=主力出貨
    except Exception:
        return False


def _bingx_line_breakout(trade) -> bool:
    """BingX 麥門切線突破→市價平剩餘。回傳 True=已平。"""
    try:
        direction = trade["direction"]; name = trade["symbol"].split("/")[0]
        sw, df = _bingx_swings(trade["symbol"], trade.get("tf_id","15m"), trade.get("entry_ts"), direction)
        if df.empty or len(sw) < 2: return False
        cl = df["close"].values; n = len(df)
        (a, pa), (b, pb) = sw[-2], sw[-1]
        if b <= a: return False
        proj = pb + (pb - pa) / (b - a) * ((n - 1) - b)
        broke = (cl[-1] > proj) if direction == "short" else (cl[-1] < proj)
        if not broke: return False
        rem = float(trade.get("remaining_qty", 0) or 0)
        if rem <= 0: return False
        res = _bingx_request("POST", "/openApi/swap/v2/trade/order", {
            "symbol": trade["inst_id"], "side": trade["exit_side"], "positionSide": trade["pos_side"],
            "type": "MARKET", "quantity": str(rem)}, trade["headers"]).json()
        if res.get("code", 0) == 0:
            try:
                _bingx_request("POST", "/openApi/swap/v2/trade/cancelOrder",
                               {"symbol": trade["inst_id"], "orderId": trade.get("sl_order_id")}, trade["headers"])
            except Exception: pass
            dc_log(f"📐 BingX {name} 麥門切線突破，市價平剩餘 {rem}")
            return True
        return False
    except Exception as e:
        print(f"[BingX MaiLine] {trade.get('symbol')} 切線出場失敗: {e}")
        return False

def _bingx_swing_trail(trade, ref_tf=None) -> bool:
    """BingX 用 pivot 擺盪點移SL(只往有利方向;非單根吞噬,回測pivot全面勝)。回傳 True=有更新。"""
    try:
        direction = trade["direction"]; name = trade["symbol"].split("/")[0]
        tf = ref_tf or trade.get("tf_id", "1H")
        if tf in ("adopted", "", None): tf = "1H"   # 接管倉用1H避雜訊(與OKX對齊;否則拿"adopted"當時框抓K失敗)
        inst_okx = OKX_SWAP.get(trade["symbol"], trade["symbol"])
        df = fetch_market_candles(inst_okx, tf, fetch_limit=120)
        if df.empty or len(df) < 6: return False
        ets = trade.get("entry_ts")
        if ets:
            try:
                cutoff = pd.Timestamp(int(ets), unit="s", tz="UTC")
                subdf = df[df.index >= cutoff]
                # 只看進場/接管後K線。不足6根→不移(等積累,不回退全120避免取進場前pivot被合法側擋)
                if len(subdf) < 6: return False
                df = subdf
            except Exception: pass
        hi = df["high"].values; lo = df["low"].values; n = len(df)
        # pivot 擺盪點(前後2根局部極值)。回測勝 N字型(N MDD暴增)→回退。
        # ★bugfix 2026-06-21:與OKX對齊——先濾市價合法側再挑最緊pivot(舊版先挑全窗最極端,
        #   噴後整理時落在錯側→整段更新被否決凍住=BingX該跟不跟根因)。並補診斷log(BingX原本一個都沒有)。
        cur_px = float(df["close"].iloc[-1])
        cur_sl = float(trade.get("current_sl", 0) or 0)
        PV = 2
        last = None
        for j in range(PV, n - PV):
            if direction == "long":
                if lo[j] == lo[j-PV:j+PV+1].min() and lo[j] < cur_px:
                    if last is None or lo[j] > last: last = lo[j]
            else:
                if hi[j] == hi[j-PV:j+PV+1].max() and hi[j] > cur_px:
                    if last is None or hi[j] < last: last = hi[j]
        if last is None:
            print(f"[BingX-Trail] {name} 合法側無pivot,不移(px={cur_px} sl={cur_sl})", flush=True); return False
        if direction == "long"  and last <= cur_sl:
            print(f"[BingX-Trail] {name} pivot{last}≤sl{cur_sl} 不更優,不移", flush=True); return False
        if direction == "short" and last >= cur_sl:
            print(f"[BingX-Trail] {name} pivot{last}≥sl{cur_sl} 不更優,不移", flush=True); return False
        rem = float(trade.get("remaining_qty", 0) or 0)
        if rem <= 0: return False
        nid = _bingx_replace_sl(trade, last, rem)
        if nid is not None:
            trade["sl_order_id"] = nid; trade["current_sl"] = last
            dc_log(f"📐 BingX {name} 轉折移動停損 → {last}")
            print(f"[BingX-Trail] {name} 移SL→{last}", flush=True)
            return True
        return False
    except Exception as e:
        print(f"[BingX SwingTrail] {trade.get('symbol')} 移SL失敗: {e}")
        return False

def _bingx_add_on_swing(trade) -> bool:
    """BingX N型轉折遞減加碼(守3)。市價加倉 + 重掛SL覆蓋新總量。回傳 True=有加碼。"""
    try:
        direction = trade["direction"]; name = trade["symbol"].split("/")[0]
        add_count = int(trade.get("add_count", 0))
        if add_count >= MAI_ADD_MAX: return False
        base = float(trade.get("init_qty") or 0)
        if base <= 0: return False
        # 加碼點=多型態反轉K(自己算,OKX公開K;切線仍用_bingx_swings單一吞噬)
        inst_okx = OKX_SWAP.get(trade["symbol"], trade["symbol"])
        df = fetch_market_candles(inst_okx, trade.get("tf_id","15m"), fetch_limit=120)
        if df.empty or len(df) < 6: return False
        ets = trade.get("entry_ts")
        if ets:
            try:
                cutoff = pd.Timestamp(int(ets), unit="s", tz="UTC")
                subdf = df[df.index >= cutoff]
                if len(subdf) >= 6: df = subdf
            except Exception: pass
        op=df["open"].values; hi=df["high"].values; lo=df["low"].values; cl=df["close"].values; n=len(df)
        sw=[]
        for f in range(1, n):
            if direction == "short":
                if _is_rev_top(op[f],hi[f],lo[f],cl[f],op[f-1],hi[f-1],lo[f-1],cl[f-1]):
                    if not sw or hi[f] < sw[-1]: sw.append(hi[f])
            else:
                if _is_rev_bot(op[f],hi[f],lo[f],cl[f],op[f-1],hi[f-1],lo[f-1],cl[f-1]):
                    if not sw or lo[f] > sw[-1]: sw.append(lo[f])
        cur_n = len(sw)
        if cur_n <= int(trade.get("add_swings_n", 0)): return False
        add_qty = round(base * 0.5, 4)
        if add_qty <= 0:
            trade["add_swings_n"] = cur_n; return False
        add_side = "BUY" if direction == "long" else "SELL"
        r = _bingx_request("POST", "/openApi/swap/v2/trade/order", {
            "symbol": trade["inst_id"], "side": add_side, "positionSide": trade["pos_side"],
            "type": "MARKET", "quantity": str(add_qty)}, trade["headers"]).json()
        if r.get("code", 0) != 0:
            trade["add_swings_n"] = cur_n; return False
        new_rem = float(trade.get("remaining_qty") or 0) + add_qty
        trade["remaining_qty"] = str(new_rem)
        nid = _bingx_replace_sl(trade, float(trade.get("current_sl") or 0), new_rem)
        if nid is not None: trade["sl_order_id"] = nid
        trade["add_count"] = add_count + 1; trade["add_swings_n"] = cur_n
        dc_log(f"📈 BingX {name} N型轉折加碼#{add_count+1} +{add_qty}張(遞減半單,守{MAI_ADD_MAX})")
        return True
    except Exception as e:
        print(f"[BingX MaiAdd] {trade.get('symbol')} 加碼失敗: {e}")
        return False


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
        # ★★致命bug修復:此段用 OKX client 查持倉,只能處理 OKX 倉位。
        # 沒有此過濾→BingX 倉位被 OKX fetch_positions 查不到→誤判已平倉→pop移除→
        # 永遠到不了下方 BingX 段→BingX 移SL/保本從來沒運作過。BingX 由下方專段處理。
        if trade.get("exchange") != "okx":
            continue
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
                # ★防孤兒倉(2026-06-18):單次查無可能是API瞬斷/symbol格式不符(SOL案例),
                #   需連2輪查無才移除,避免把還開著的倉踢出追蹤變孤兒(失管)。
                _miss = int(trade.get("_pos_miss", 0)) + 1
                if _miss < 2:
                    trade["_pos_miss"] = _miss; save_active_trades(); continue
                print(f"[Trailing] {name} 倉位已關閉(連{_miss}輪查無)，移除追蹤")
                active_real_trades.pop(trade_key, None); save_active_trades()
                continue
            if trade.get("_pos_miss"):
                trade["_pos_miss"] = 0   # 查到倉=重置誤判計數

            # ── CME缺口單 300h超時平倉(回測同款:逾300根1H未觸SL/TP→收盤價出) ──
            if (trade.get("exit_strategy") == "cme_gap"
                    and time.time() - int(trade.get("entry_ts", 0)) > CME_GAP_TIMEOUT_H * 3600):
                try:
                    ex.create_market_order(symbol=symbol,
                        side=("sell" if direction == "long" else "buy"),
                        amount=float(trade.get("remaining_amount", 0) or 0),
                        params={"posSide": direction, "tdMode": MARGIN_MODE, "reduceOnly": True})
                    _cancel_okx_algo_order(inst_id, trade.get("sl_algo_id"))
                    dc_log(f"⏰ {name} CME缺口單300h超時,市價平倉")
                except Exception as _cte:
                    print(f"[CME-Gap] {name} 超時平倉失敗: {_cte}")
                active_real_trades.pop(trade_key, None); save_active_trades(); continue

            # ── 山寨多單 OI降早出(OI_EARLY_EXIT_ENABLED):主力出貨即跑,救COAI式吐回 ──
            if _oi_drop_exit_long(trade):
                try:
                    ex.create_market_order(symbol=symbol, side="sell",
                        amount=float(trade.get("remaining_amount", 0) or 0),
                        params={"posSide": direction, "tdMode": MARGIN_MODE, "reduceOnly": True})
                    _cancel_okx_algo_order(inst_id, trade.get("sl_algo_id"))
                    dc_log(f"📉 {name} OI降早出(主力出貨),市價平倉")
                except Exception as _oie:
                    print(f"[OI-Exit] {name} 平倉失敗: {_oie}")
                active_real_trades.pop(trade_key, None); save_active_trades(); continue

            # ── 金字塔加碼：驗證過的多單(C3/W底)達 +1R 且未加過 → 加一單位 ──────────
            if (PYRAMID_ENABLED and trade.get("exchange") == "okx" and direction == "long"
                    and trade.get("pyramid_eligible", False)
                    and not trade.get("pyramid_added", False)
                    and not trade.get("tp1_hit", False)):
                try:
                    entry_p = float(trade["entry_price"]); risk_d = float(trade.get("risk_dist", 0) or 0)
                    if risk_d > 0:
                        cur_p = float(ex.fetch_ticker(symbol).get("last") or 0)
                        if cur_p >= entry_p + risk_d:   # 達 +1R
                            _okx_pyramid_add(ex, trade)
                            save_active_trades()
                except Exception as _pe:
                    print(f"[Pyramid] {name} 加碼判斷失敗: {_pe}")

            # ── 整倉麥門切線(line_full)：DH空 / 30m C3多 ─────────────────────
            # 不掛TP,整倉沿切線跑,「實體收盤突破切線」→市價平全倉;SL已掛硬底兜底。
            # 不走 TP1/保本邏輯(整倉跟趨勢,WF:DH+0.629/30m+0.582,去top3仍正)。
            if trade.get("exit_strategy") == "line_full":
                if _mai_line_breakout(ex, trade):
                    active_real_trades.pop(trade_key, None)
                    save_active_trades()
                continue

            # ── 整倉切線 + N型轉折加碼(line_add)：DH空。切線突破→平全部;否則順勢轉折處加碼。
            #    WF:DH空遞減加碼驗+0.890/RA3.46(>無加碼3.15)。
            if trade.get("exit_strategy") == "line_add":
                if _mai_line_breakout(ex, trade):     # 切線突破→平全部(含加碼倉)
                    active_real_trades.pop(trade_key, None)
                    save_active_trades()
                    continue
                if _mai_add_on_swing(ex, trade):      # 順勢轉折→加碼
                    save_active_trades()
                continue

            # ── 整倉轉折移SL(swing_full)：1H MACD空。不掛TP,整倉,用最新轉折移SL,
            #    出場靠交易所SL algo觸發。WF:1H MACD空驗+0.251/RA0.83。
            if trade.get("exit_strategy") == "swing_full":
                # 接管倉達1R保本兜底(與BingX一致):達浮盈1R且SL還在虧損側→先移SL保本,
                # 之後 N 字型移SL 繼續鎖利。be_better 防止把已鎖利的SL拉回保本。
                # ★LETRUN_BE_ENABLED=False(2026-06-10):此兜底害讓跑策略,預設關,直接走純pivot移SL。
                if LETRUN_BE_ENABLED and trade.get("tf_id") == "adopted" and not trade.get("tp1_hit"):
                    try:
                        cur = float(ex.fetch_ticker(symbol).get("last") or 0)
                        entry = float(trade["entry_price"]); rd = float(trade.get("risk_dist", 0) or 0)
                        if cur > 0 and rd > 0:
                            fpnl = (entry - cur) if direction == "short" else (cur - entry)
                            fee_buf = entry * 0.001
                            be_price = entry - fee_buf if direction == "short" else entry + fee_buf
                            sl_now = float(trade.get("current_sl") or 0)
                            be_better = (be_price < sl_now) if direction == "short" else (be_price > sl_now)
                            print(f"[OKX-BE] {name} {direction} 浮盈{fpnl/rd:+.2f}R sl={sl_now} be={be_price} 可保本={be_better}", flush=True)
                            if fpnl >= rd * 1.0 and be_better:
                                exit_side = "sell" if direction == "long" else "buy"
                                try: _bx = ex.price_to_precision(symbol, be_price)
                                except Exception: _bx = format(be_price, "f")
                                _cancel_okx_algo_order(inst_id, trade["sl_algo_id"])
                                def _place_be_sf():
                                    r = _place_okx_algo_sl(inst_id=inst_id, side=exit_side, amount="0",
                                                           sl_trigger_px=_bx, pos_side=direction)
                                    return r, (r.get("data") or [{}])[0].get("algoId")
                                res, nid = _place_be_sf()
                                if not nid:
                                    _sc = str((res.get("data") or [{}])[0].get("sCode") or "")
                                    if _sc == "51088":
                                        _okx_cancel_all_algos(inst_id); time.sleep(0.3)
                                        res, nid = _place_be_sf()
                                if nid:
                                    trade["sl_algo_id"] = nid; trade["current_sl"] = be_price
                                    trade["tp1_hit"] = True
                                    dc_log(f"🔒 {name} 接管倉達1R,止損移保本 {be_price}")
                                    print(f"[OKX-BE] {name} 達1R保本→{be_price}", flush=True)
                    except Exception as _be:
                        print(f"[OKX-BE] {name} 保本判斷失敗: {_be}", flush=True)
                # ── 讓跑倉轉折加碼(2026-06-21,多空通用):順勢轉折處加碼,加後 place-before-cancel 重掛SL覆蓋全倉 ──
                if PYRAMID_LETRUN_ENABLED and trade.get("tf_id") != "adopted":
                    try:
                        _old_sl_id = trade.get("sl_algo_id")
                        if _mai_add_on_swing(ex, trade):   # 加碼(多空皆可,含強平守門員),更新 remaining_amount
                            _csl = float(trade.get("current_sl") or 0)
                            if _csl > 0:
                                try: _slpx = ex.price_to_precision(symbol, _csl)
                                except Exception: _slpx = format(_csl, "f")
                                # 先掛覆蓋全倉的新SL(平倉方向:多sell/空buy),確認成功才取消舊SL(不裸倉)
                                _r = _place_okx_algo_sl(inst_id=inst_id,
                                                        side=("sell" if direction == "long" else "buy"),
                                                        amount=trade["remaining_amount"],
                                                        sl_trigger_px=_slpx, pos_side=direction)
                                _nid = (_r.get("data") or [{}])[0].get("algoId")
                                if _nid:
                                    trade["sl_algo_id"] = _nid
                                    _cancel_okx_algo_order(inst_id, _old_sl_id)
                                else:
                                    print(f"[LetRunAdd] {name} 加碼後重掛SL失敗,保留舊SL(不裸倉)", flush=True)
                            save_active_trades()
                    except Exception as _lra:
                        print(f"[LetRunAdd] {name} 讓跑加碼失敗: {_lra}", flush=True)
                if _swing_trail_update_sl(ex, trade):
                    save_active_trades()
                continue

            # ── 箱突破空(box_trend)：整倉4R TP掛在交易所,這裡只做「達1R浮盈→移SL保本」(一次)
            #    防假突破拉回。TP(4R)成交由交易所自動平,下輪偵測倉位消失移除。
            if trade.get("exit_strategy") in ("box_trend", "hf_1r"):
                _is_hf = trade.get("exit_strategy") == "hf_1r"   # 高頻固定1R:0.5R保本;TP@1R掛交易所自動全平
                _be_trig = 0.5 if _is_hf else 1.0
                _be_active = False if _is_hf else LETRUN_BE_ENABLED  # ★hf_1r拿掉保本(2026-06-18):純固定1R,TP@1R/SL@-1R掛交易所,勝率~57%(去BE驗證更高)
                if _be_active and not trade.get("tp1_hit"):       # 借 tp1_hit 當「已保本」旗標
                    try:
                        cur = float(ex.fetch_ticker(symbol).get("last") or 0)
                        entry = float(trade["entry_price"]); rd = float(trade.get("risk_dist", 0) or 0)
                        if cur > 0 and rd > 0:
                            fpnl = (entry - cur) if direction == "short" else (cur - entry)
                            if fpnl >= rd * _be_trig:    # hf=0.5R / box=1R → 移SL到保本
                                fee_buf = entry * 0.001
                                be_price = entry - fee_buf if direction == "short" else entry + fee_buf
                                exit_side = "sell" if direction == "long" else "buy"
                                try: _bx = ex.price_to_precision(symbol, be_price)
                                except Exception: _bx = format(be_price, "f")
                                _cancel_okx_algo_order(inst_id, trade["sl_algo_id"])
                                def _place_bt():
                                    r = _place_okx_algo_sl(inst_id=inst_id, side=exit_side, amount="0",
                                                           sl_trigger_px=_bx, pos_side=direction)
                                    return r, (r.get("data") or [{}])[0].get("algoId")
                                res, nid = _place_bt()
                                if not nid:
                                    _sc = str((res.get("data") or [{}])[0].get("sCode") or "")
                                    if _sc == "51088":
                                        _okx_cancel_all_algos(inst_id); time.sleep(0.3)
                                        res, nid = _place_bt()
                                if nid:
                                    trade["sl_algo_id"] = nid; trade["current_sl"] = be_price
                                    trade["tp1_hit"] = True
                                    dc_log(f"🔒 {name} {'高頻達0.5R' if _is_hf else '箱突破空達1R'},止損移保本 {be_price}")
                                else:
                                    try:
                                        try: _osl = ex.price_to_precision(symbol, trade["current_sl"])
                                        except Exception: _osl = format(float(trade["current_sl"]), "f")
                                        rr = _place_okx_algo_sl(inst_id=inst_id, side=exit_side, amount="0",
                                                                sl_trigger_px=_osl, pos_side=direction)
                                        if (rr.get("data") or [{}])[0].get("algoId"):
                                            trade["sl_algo_id"]=(rr.get("data") or [{}])[0].get("algoId")
                                    except Exception: pass
                                    dc_log(f"⚠️ {name} 箱突破空移保本失敗,已重掛原止損,請手動確認")
                    except Exception as _be:
                        print(f"[BoxTrend] {name} 保本判斷失敗: {_be}")
                continue

            if not trade["tp1_hit"]:
                # 安全查 TP1 狀態：無單號或查詢失敗 → 視為未成交，改走浮盈保本
                # （修：原本 fetch_order(None) 會丟例外→整筆被跳過→保本永遠不動）
                tp1_status = None
                if trade.get("tp1_order_id"):
                    try:
                        tp1_status = (ex.fetch_order(trade["tp1_order_id"], symbol) or {}).get("status")
                    except Exception as _tpe:
                        print(f"[Trailing] {name} 查TP1失敗(改走浮盈保本): {_tpe}")
                if tp1_status in ("closed", "filled"):
                    # TP1 成交 → 移止損到保本價(含手續費)
                    entry    = float(trade["entry_price"])
                    fee_buf  = entry * 0.001   # taker 雙邊 0.1%
                    be_price = entry + fee_buf if direction == "long" else entry - fee_buf
                    try: be_px = ex.price_to_precision(symbol, be_price)
                    except Exception: be_px = format(be_price, "f")
                    exit_side = "sell" if direction == "long" else "buy"
                    # 取消舊止損後掛保本(closeFraction=1全倉)
                    _cancel_okx_algo_order(inst_id, trade["sl_algo_id"])
                    def _place_be():
                        r = _place_okx_algo_sl(inst_id=inst_id, side=exit_side, amount="0",
                                               sl_trigger_px=be_px, pos_side=direction)
                        return r, (r.get("data") or [{}])[0].get("algoId")
                    sl_result, new_algo_id = _place_be()
                    if not new_algo_id:
                        # 51088「同全倉位已有TP/SL」→ 清掉所有algo再重掛(防舊止損取消後裸倉)
                        _sc = str((sl_result.get("data") or [{}])[0].get("sCode") or "")
                        if _sc == "51088":
                            _okx_cancel_all_algos(inst_id); time.sleep(0.3)
                            sl_result, new_algo_id = _place_be()
                    if new_algo_id:
                        trade["sl_algo_id"] = new_algo_id
                        trade["current_sl"] = be_price
                        trade["tp1_hit"]    = True
                        msg = f"✅ TP1 已成交，止損移至保本價 {be_price}（含手續費）\n幣種：{name}"
                        dc_log(msg); tg_log(msg)
                        print(f"[Trailing] {name} TP1成交，SL移至保本價 {be_price}")
                    else:
                        # 移保本失敗 → 重掛原止損避免裸倉 + 警告(不發假保本通知)
                        try:
                            try: _osl = ex.price_to_precision(symbol, trade["current_sl"])
                            except Exception: _osl = format(float(trade["current_sl"]), "f")
                            rr = _place_okx_algo_sl(inst_id=inst_id, side=exit_side, amount="0",
                                                    sl_trigger_px=_osl, pos_side=direction)
                            rid = (rr.get("data") or [{}])[0].get("algoId")
                            if rid: trade["sl_algo_id"] = rid
                        except Exception as _re:
                            print(f"[Trailing] {name} 重掛原止損失敗: {_re}")
                        dc_log(f"⚠️ {name} TP1成交但移保本失敗，已嘗試重掛原止損 {trade['current_sl']}，請手動確認")
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
                    # 保本價含手續費：多單掛 entry+fee、空單掛 entry-fee（之前誤掛在raw entry沒扣費）
                    be_price = entry + fee_buffer if direction == "long" else entry - fee_buffer
                    # 已保本(SL已達/優於保本價:多頭SL>=be、空頭SL<=be)→設tp1_hit進入pivot移SL。
                    # ⚠️ elif：_be_done時跳過浮盈保本，否則SL被倒退回保本價(已追蹤更佳的SL被覆蓋)。
                    _be_done = (float(trade["current_sl"]) >= be_price) if direction == "long" else (float(trade["current_sl"]) <= be_price)
                    if _be_done:
                        trade["tp1_hit"] = True
                    elif float_pnl >= breakeven_trigger and trade["current_sl"] != be_price:
                        exit_side = "sell" if direction == "long" else "buy"
                        try: _be_px = ex.price_to_precision(symbol, be_price)
                        except Exception: _be_px = format(be_price, "f")
                        _cancel_okx_algo_order(inst_id, trade["sl_algo_id"])
                        def _place_be2():
                            r = _place_okx_algo_sl(inst_id=inst_id, side=exit_side, amount="0",
                                                   sl_trigger_px=_be_px, pos_side=direction)
                            return r, (r.get("data") or [{}])[0].get("algoId")
                        sl_result, new_algo_id = _place_be2()
                        if not new_algo_id:
                            _sc = str((sl_result.get("data") or [{}])[0].get("sCode") or "")
                            if _sc == "51088":
                                _okx_cancel_all_algos(inst_id); time.sleep(0.3)
                                sl_result, new_algo_id = _place_be2()
                        if new_algo_id:
                            trade["sl_algo_id"] = new_algo_id
                            trade["current_sl"] = be_price
                            trade["tp1_hit"] = True   # 保本後進入移SL階段(下輪走 else 的 pivot 移SL 繼續鎖利)
                            msg = f"🔒 {name} 浮盈達{be_trigger_mult}R，止損移至保本價 {be_price}（含手續費）"
                            dc_log(msg); print(f"[Trailing] {msg}")
                        else:
                            try:
                                try: _osl = ex.price_to_precision(symbol, trade["current_sl"])
                                except Exception: _osl = format(float(trade["current_sl"]), "f")
                                rr = _place_okx_algo_sl(inst_id=inst_id, side=exit_side, amount="0",
                                                        sl_trigger_px=_osl, pos_side=direction)
                                rid = (rr.get("data") or [{}])[0].get("algoId")
                                if rid: trade["sl_algo_id"] = rid
                            except Exception: pass
                            dc_log(f"⚠️ {name} 浮盈保本掛載失敗，已嘗試重掛原止損，請手動確認")

            else:
                # TP1 已成交
                # ── tp_line：TP1落袋半,剩半沿麥門切線跑,實體收盤破線市價平剩餘 ──
                if trade.get("exit_strategy") == "tp_line":
                    if _mai_line_breakout(ex, trade):
                        active_real_trades.pop(trade_key, None)
                        save_active_trades()
                    continue

                # ── swing_tp / swing_tp_1h：TP1落袋半,剩半用最新轉折移SL,出場靠交易所algo ──
                #    swing_tp(1H W底多,參自身1H轉折,驗+0.165);
                #    swing_tp_1h(15m MACD多,參1H轉折避雜訊,驗+0.142/RA0.36)。
                if trade.get("exit_strategy") in ("swing_tp", "swing_tp_1h"):
                    _ref = "1H" if trade.get("exit_strategy") == "swing_tp_1h" else None
                    if _swing_trail_update_sl(ex, trade, ref_tf=_ref):
                        save_active_trades()
                    continue

                # 其他(固定R剩半 / 接管倉es=None)：保本後用 pivot 擺盪點移SL繼續鎖利
                # 使用 _swing_trail_update_sl：entry_ts 過濾進場後K線(修 bug：舊版用全120根
                # 含進場前K，空頭進場前的低local high < 當前價，OKX拒單→SL永卡在保本價)
                if _swing_trail_update_sl(ex, trade):
                    save_active_trades()

        except Exception as e:
            print(f"[Trailing] {name} 處理失敗: {e}")

    # ── BingX 保本追蹤 ──────────────────────────────────────────────────────
    # ★先抓一次實時持倉,移除已平倉的追蹤——否則已關閉的倉位每15m仍嘗試挂止損→
    #   BingX 回 109420 "position not exist" 每輪刷屏(用戶看到的一堆錯誤)。
    #   只移除「記憶體追蹤」,不碰交易所任何掛單(已平倉本就無單;誤判最多=停止管理,倉位仍有交易所SL)。
    _bingx_live_syms = None
    for _tk in list(active_real_trades.keys()):
        _t = active_real_trades[_tk]
        if _t.get("exchange") == "bingx" and _t.get("headers"):
            try:
                _pr = _bingx_request("GET", "/openApi/swap/v2/user/positions", {}, _t["headers"]).json()
                _bingx_live_syms = {p.get("symbol") for p in (_pr.get("data") or [])
                                    if abs(float(p.get("positionAmt") or 0)) > 0}
            except Exception as _le:
                print(f"[BingX] 取實時持倉失敗(本輪不清理): {_le}", flush=True)
            break
    if _bingx_live_syms is not None:
        for _tk in list(active_real_trades.keys()):
            _t = active_real_trades[_tk]
            if _t.get("exchange") != "bingx": continue
            if _t.get("inst_id") not in _bingx_live_syms:
                print(f"[BingX] {_t.get('inst_id')} 倉位已平→移除追蹤(不再嘗試挂止損)", flush=True)
                active_real_trades.pop(_tk, None)
        save_active_trades()

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

            # ── CME缺口單 300h超時平倉(與OKX段對齊) ──
            if (trade.get("exit_strategy") == "cme_gap"
                    and time.time() - int(trade.get("entry_ts", 0)) > CME_GAP_TIMEOUT_H * 3600):
                try:
                    _rem = float(trade.get("remaining_qty", 0) or 0)
                    if _rem > 0:
                        _r = _bingx_request("POST", "/openApi/swap/v2/trade/order", {
                            "symbol": bingx_symbol, "side": exit_side, "positionSide": pos_side,
                            "type": "MARKET", "quantity": str(_rem)}, headers).json()
                        if _r.get("code", 0) == 0:
                            try:
                                _bingx_request("POST", "/openApi/swap/v2/trade/cancelOrder",
                                               {"symbol": bingx_symbol, "orderId": sl_order_id}, headers)
                            except Exception: pass
                            dc_log(f"⏰ BingX {bingx_symbol} CME缺口單300h超時,市價平倉")
                            active_real_trades.pop(trade_key, None); save_active_trades(); continue
                except Exception as _cte:
                    print(f"[BingX CME-Gap] {trade_key} 超時平倉失敗: {_cte}")

            # ── 山寨多單 OI降早出(預設關,與OKX對齊):主力出貨即跑,救COAI式吐回 ──
            if _oi_drop_exit_long(trade):
                try:
                    _rem = float(trade.get("remaining_qty", 0) or 0)
                    if _rem > 0:
                        _r = _bingx_request("POST", "/openApi/swap/v2/trade/order", {
                            "symbol": bingx_symbol, "side": exit_side, "positionSide": pos_side,
                            "type": "MARKET", "quantity": str(_rem)}, headers).json()
                        if _r.get("code", 0) == 0:
                            try:
                                _bingx_request("POST", "/openApi/swap/v2/trade/cancelOrder",
                                               {"symbol": bingx_symbol, "orderId": sl_order_id}, headers)
                            except Exception: pass
                            dc_log(f"📉 BingX {bingx_symbol} OI降早出(主力出貨),市價平倉")
                            active_real_trades.pop(trade_key, None); save_active_trades(); continue
                except Exception as _oie:
                    print(f"[BingX OI-Exit] {trade_key} 失敗: {_oie}")

            # ── BingX 趨勢跟蹤出場(與OKX對齊;切線/移SL/加碼,用OKX公開K偵測轉折)──────
            _es = trade.get("exit_strategy", "")
            # 箱突破空:整倉4R TP掛在交易所,這裡只做達1R保本(一次)。TP成交自動平。
            if _es in ("box_trend", "hf_1r"):
                _is_hf = _es == "hf_1r"             # 高頻固定1R:0.5R保本;TP@1R掛交易所自動全平
                _be_trig = 0.5 if _is_hf else 1.0
                _be_active = False if _is_hf else LETRUN_BE_ENABLED  # ★hf_1r拿掉保本(2026-06-18):純固定1R,TP@1R/SL@-1R掛交易所,勝率~57%(去BE驗證更高)
                if _be_active and not trade.get("tp1_hit"):
                    try:
                        cur=_px_for_bingx(ex, trade)
                        rd=float(trade.get("risk_dist",0) or 0)
                        if cur>0 and rd>0:
                            fpnl=(entry-cur) if direction=="short" else (cur-entry)
                            if fpnl>=rd*_be_trig:   # hf=0.5R / box=1R→移SL保本(取消舊+重掛STOP_MARKET)
                                nid=_bingx_replace_sl(trade, be_price, remaining)
                                if nid is not None:
                                    trade["sl_order_id"]=nid; trade["current_sl"]=be_price
                                    trade["tp1_hit"]=True
                                    dc_log(f"🔒 BingX {bingx_symbol} {'高頻達0.5R' if _is_hf else '箱突破空達1R'},止損移保本 {be_price}")
                    except Exception as _bbe:
                        print(f"[BingX BoxTrend] {trade_key} 保本失敗: {_bbe}")
                continue
            if _es in ("line_full", "line_add"):
                if _bingx_line_breakout(trade):
                    active_real_trades.pop(trade_key, None)
                elif _es == "line_add":
                    _bingx_add_on_swing(trade)
                continue
            if _es == "swing_full":
                # 接管倉(tf_id=adopted)加「達1R保本」兜底:不依賴K線,達浮盈立即移SL到保本,
                # 之後再交給pivot移SL。正常swing_full(1H MACD空等)不加,保持回測純移SL。
                # 同時印每倉浮盈診斷,看清19倉是賺是虧、該不該動。
                # ★LETRUN_BE_ENABLED=False(2026-06-10):兜底害讓跑,預設關,直接走純pivot移SL。
                if LETRUN_BE_ENABLED and trade.get("tf_id") == "adopted" and not trade.get("tp1_hit"):
                    try:
                        cur = _px_for_bingx(ex, trade)
                        rd  = float(trade.get("risk_dist", 0) or 0)
                        if cur > 0 and rd > 0:
                            fpnl_r = ((entry - cur) if direction == "short" else (cur - entry)) / rd
                            sl_now = float(trade.get("current_sl") or 0)
                            be_better = (be_price < sl_now) if direction == "short" else (be_price > sl_now)
                            print(f"[BingX-BE] {bingx_symbol} {direction} 浮盈{fpnl_r:+.2f}R "
                                  f"sl={sl_now} be={be_price} 可保本={be_better}", flush=True)
                            if fpnl_r >= 1.0 and be_better:
                                nid = _bingx_replace_sl(trade, be_price, remaining)
                                if nid is not None:
                                    trade["sl_order_id"] = nid; trade["current_sl"] = be_price
                                    trade["tp1_hit"] = True
                                    dc_log(f"🔒 BingX {bingx_symbol} 接管倉達1R,止損移保本 {be_price}")
                                    print(f"[BingX-BE] {bingx_symbol} 達1R保本→{be_price}", flush=True)
                    except Exception as _be:
                        print(f"[BingX-BE] {trade_key} 保本判斷失敗: {_be}", flush=True)
                # ── 讓跑倉轉折加碼(2026-06-21,多空通用):_bingx_add_on_swing 自帶 place-before-cancel 重掛SL覆蓋全倉 ──
                if PYRAMID_LETRUN_ENABLED and trade.get("tf_id") != "adopted":
                    try: _bingx_add_on_swing(trade)
                    except Exception as _bla: print(f"[BingX LetRunAdd] {bingx_symbol} 加碼失敗: {_bla}", flush=True)
                moved = _bingx_swing_trail(trade)
                # 即使沒移SL(SL已在最優位,pivot不優於它),也清理多餘止損單。
                # 解決:之前累積的殘留止損單,因現在不觸發移SL/保本→_bingx_replace_sl不被呼叫→舊單清不掉。
                if not moved:
                    try:
                        _oo = _bingx_request("GET", "/openApi/swap/v2/trade/openOrders",
                                             {"symbol": bingx_symbol}, headers).json()
                        _ords = _oo.get("data") or {}
                        if isinstance(_ords, dict): _ords = _ords.get("orders") or []
                        _stops = [o for o in _ords
                                  if str(o.get("type", "")).upper() in ("STOP_MARKET", "STOP")
                                  and o.get("positionSide") == pos_side]
                        if len(_stops) > 1:
                            _csl = float(trade.get("current_sl") or 0)
                            if _csl > 0:
                                nid = _bingx_replace_sl(trade, _csl, remaining)  # 清全部,按current_sl重掛1個
                                if nid: trade["sl_order_id"] = nid
                                print(f"[BingX-Dedup] {bingx_symbol} 清理{len(_stops)}個止損→1個@{_csl}", flush=True)
                    except Exception as _de:
                        print(f"[BingX-Dedup] {bingx_symbol} 清理失敗: {_de}", flush=True)
                continue
            if _es in ("swing_tp", "swing_tp_1h", "tp_line"):
                if not trade.get("tp1_hit"):
                    # 查TP1成交→重掛半倉SL於保本,進入趨勢跟蹤
                    try:
                        _t = _bingx_request("GET", "/openApi/swap/v2/trade/order",
                                            {"symbol": bingx_symbol, "orderId": trade["tp1_order_id"]},
                                            headers).json().get("data", {}).get("order", {})
                        if _t.get("status") in ("FILLED", "filled"):
                            nid = _bingx_replace_sl(trade, be_price, remaining)
                            if nid is not None:
                                trade["sl_order_id"] = nid; trade["current_sl"] = be_price
                            trade["tp1_hit"] = True
                            dc_log(f"✅ BingX {bingx_symbol} TP1成交,剩半進入趨勢跟蹤(保本起跳)")
                    except Exception as _te:
                        print(f"[BingX Trend] {trade_key} 查TP1失敗: {_te}")
                else:
                    if _es == "tp_line":
                        if _bingx_line_breakout(trade):
                            active_real_trades.pop(trade_key, None)
                    else:
                        _bingx_swing_trail(trade, ref_tf=("1H" if _es == "swing_tp_1h" else None))
                continue

            if trade["tp1_hit"]:
                # 固定R:TP1成交保本後，用 pivot 擺盪點移SL繼續鎖利（與 OKX else 分支對齊）
                # 原本直接 continue → 保本後 SL 永遠停在進場價，不跟漲/跌。
                _bingx_swing_trail(trade)
                continue

            # ── 浮盈提前保本(與OKX對齊)：達 be_trigger×R+fee → 全倉SL移保本價 ──
            # 用 OKX 報價當參考(同資產跨所價格近似);失敗則跳過,退回TP1成交後保本。
            try:
                risk_b = float(trade.get("risk_dist", 0) or 0)
                if risk_b > 0 and trade.get("current_sl") != be_price:
                    cur_b = _px_for_bingx(ex, trade)
                    tf_kb = f"{trade.get('tf_id','15m')}_{direction}"
                    be_mb = BEST_PARAMS.get(tf_kb, {}).get("be_trigger", 1.0)
                    trig_b = risk_b * be_mb + entry * 0.001
                    fpnl_b = (cur_b - entry) if direction == "long" else (entry - cur_b)
                    if cur_b > 0 and fpnl_b >= trig_b:
                        # 統一走 _bingx_replace_sl(DELETE撤單 + closePosition,避免110424/暴增)
                        nid = _bingx_replace_sl(trade, be_price, trade.get("full_qty", remaining))
                        if nid is not None:
                            trade["sl_order_id"] = nid
                            trade["current_sl"]  = be_price
                            dc_log(f"🔒 BingX {bingx_symbol} 浮盈達{be_mb}R，止損移至保本價 {be_price}（含手續費）")
            except Exception as _eb:
                print(f"[BingX Trailing] {trade_key} 提前保本判斷失敗: {_eb}")

            # 查詢 TP1 是否成交
            tp1_r = _bingx_request("GET", "/openApi/swap/v2/trade/order", {
                "symbol": bingx_symbol, "orderId": trade["tp1_order_id"]
            }, headers)
            tp1_data = tp1_r.json().get("data", {}).get("order", {})
            if tp1_data.get("status") in ("FILLED", "filled"):
                # TP1 成交 → 統一走 _bingx_replace_sl(DELETE撤單 + closePosition整倉,
                # 避免 POST cancelOrder 刪不掉暴增 + 帶量止損 110424)
                new_sl_id = _bingx_replace_sl(trade, be_price, remaining)
                if new_sl_id is not None:
                    trade["sl_order_id"] = new_sl_id
                    trade["current_sl"]  = be_price
                    trade["tp1_hit"]     = True
                    msg = f"✅ BingX TP1 成交，止損移至保本價 {be_price}（含手續費）\n幣種：{bingx_symbol}"
                    dc_log(msg)
                    print(f"[BingX Trailing] {msg}")
                else:
                    print(f"[BingX Trailing] {bingx_symbol} TP1保本掛載失敗(見[BingX-SL]log)", flush=True)

        except Exception as be_err:
            print(f"[BingX Trailing] {trade_key} 處理失敗: {be_err}")

    # 每輪追蹤後存檔：tp1_hit/current_sl/sl_order_id 變動、倉位移除 都會反映
    save_active_trades()

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


# ── 數據獵手 CVD 過濾（WF 驗證版，與上面三層吸收不同）───────────────────────────
DH_CVD_ENABLED = True   # 開關：15m 多 CVD吸收加碼（C方案）
BOOST_MULT     = 1.0    # ★2026-06-15 加碼總開關(止血關閉):1.0=不加碼;改回1.5重新啟用所有×1.5加碼
DH_BOOST_MULT  = BOOST_MULT    # CVD吸收確認時的下注加碼倍數(回測C×1.5;現由 BOOST_MULT 總控)
# ★2026-06-15 市值幣集合:讓跑類(swing_full/line_full)市值幣維持讓跑,山寨改半倉2.5R落袋(噴到頂鎖利防吐回,如COAI)
MAJOR_COINS = {"BTC/USDT","ETH/USDT","SOL/USDT","BNB/USDT","XRP/USDT","DOGE/USDT","ADA/USDT",
               "AVAX/USDT","LINK/USDT","DOT/USDT","TRX/USDT","BCH/USDT","LTC/USDT","TON/USDT","SUI/USDT"}
ALT_LOCK_R     = 2.5    # 山寨讓跑單達此R先落袋半倉+移BE(防COAI式噴到頂吐回)

# ── 金字塔加碼（+1R 加單，僅多單）──────────────────────────────────────────────
# 回測(WF)金字塔翻倍成長但MDD大增;OKX同方向會合併成一個部位,故實作=「+1R加大部位
# + 停損上移到原進場價」,行為與回測(兩獨立單)不完全相同。安全設計:
#   1) 加碼前走強平守門員(合併部位若強平在停損前→不加)
#   2) 加碼後停損=原進場價→觸損時原單保本+加碼單虧1R,合併最大虧≈1單位(有界,不爆倉)
#   3) 每筆只加一次  4) 預設關閉,review+觀察後再開
PYRAMID_ENABLED = False   # 2026-06-13 關閉:橫盤=純風險放大;改用突破訊號position_scale×1.5集中下注
PYRAMID_LIQ_BUF = 0.85    # 強平守門員緩衝(同下單管線)
# ── 讓跑倉(swing_full)轉折加碼開關(2026-06-21,session驗證:轉折點加碼放大讓跑贏家,下檔仍束底倉) ──
#   多空通用讓跑倉:在順勢轉折(多=更高腳/空=更低頭)處用 _mai_add_on_swing 加碼(遞減半單/守3/強平守門員)。
#   加後重掛SL覆蓋全倉(OKX place-before-cancel防裸倉;BingX _bingx_add_on_swing自帶安全重掛)。
#   排除接管倉(tf_id=adopted)。市價版先上;觸發單(轉折線等回踩,不卡保證金)為下一步升級。
PYRAMID_LETRUN_ENABLED = True
# ── 讓跑類策略(swing_full接管倉 / box_trend)的「達1R保本兜底」開關 ───────────────
# 2026-06-10 含費WF證實:1R保本兜底對讓跑策略是災難(DH +0.142→-0.118、1H C3空砍頭)。
# 它當初只是接管倉的未驗證OK繃(commit 82def47,本就「正常swing_full不加」),卻因每次redeploy
# 全倉被adopt而蓋住全部。關掉=還原回測驗證過的純pivot移SL讓跑(pivot trail本身已護回撤)。
# 註:固定R策略(15m/1H C3 的 TP1保本/浮盈保本)不在此旗標內,維持現狀待另測。
LETRUN_BE_ENABLED = False
def _okx_pyramid_add(ex, trade) -> bool:
    """對已 +1R 的多單加碼一個單位(=原始張數),停損上移到原進場價。
    走強平守門員;不安全則跳過。回傳是否成功加碼。"""
    try:
        symbol = trade["symbol"]; inst_id = trade["inst_id"]
        entry  = float(trade["entry_price"]); init_ct = float(trade.get("init_contracts") or 0)
        if init_ct <= 0: return False
        tk = ex.fetch_ticker(symbol); cur = float(tk.get("last") or 0)
        if cur <= 0: return False
        mkt = ex.market(symbol); ct_val = float(mkt.get("contractSize", 1.0) or 1.0)
        # 加碼張數 = 基礎單位(未疊CVD加碼),依該幣精度取整
        try: add_amt = float(ex.amount_to_precision(symbol, init_ct))
        except Exception: add_amt = init_ct
        if add_amt <= 0:
            dc_log(f"⚠️ 金字塔跳過 [{symbol}]：加碼張數取整後為0"); trade["pyramid_added"]=True; return False
        # 實際當前部位張數(含CVD加碼) + 加碼 = 合併名義(守門員用真實部位才保守)
        try:
            cur_ct = 0.0
            for _p in ex.fetch_positions([symbol]):
                if _p.get("symbol")==symbol and _p.get("side")==trade["direction"]:
                    cur_ct = abs(float(_p.get("contracts") or 0)); break
            if cur_ct <= 0: cur_ct = init_ct
        except Exception:
            cur_ct = init_ct
        combined_val = (cur_ct + add_amt) * cur * ct_val
        try:
            bal = ex.fetch_balance(); avail = float(bal.get("USDT", {}).get("free", 0.0))
        except Exception:
            avail = 0.0
        try:
            lev = int(float(((mkt.get("limits", {}) or {}).get("leverage", {}) or {}).get("max") or MAX_LEVERAGE))
        except Exception:
            lev = MAX_LEVERAGE
        comb_margin = combined_val / max(1, min(lev, MAX_LEVERAGE))
        # 強平守門員:停損距=(cur-entry)/cur;估強平距=(avail+合併保證金)/合併名義
        sl_dist_pct = abs(cur - entry) / cur if cur else 1.0
        est_liq = (avail + comb_margin) / combined_val if combined_val else 0.0
        if sl_dist_pct >= est_liq * PYRAMID_LIQ_BUF:
            dc_log(f"⚠️ 金字塔跳過 [{symbol}]：加碼後強平價會在停損(原進場{entry})前，為防爆倉不加")
            return False
        # 下加碼市價單(增加部位)
        add_action = "buy" if trade["direction"] == "long" else "sell"
        ex.create_market_order(symbol=symbol, side=add_action, amount=add_amt,
                               params={"posSide": trade["direction"], "tdMode": MARGIN_MODE})
        # 停損上移到原進場價(closeFraction=1 平合併全倉)
        _cancel_okx_algo_order(inst_id, trade["sl_algo_id"])
        try: sl_px = ex.price_to_precision(symbol, entry)
        except Exception: sl_px = format(entry, "f")
        sl_side = "sell" if trade["direction"] == "long" else "buy"
        res = _place_okx_algo_sl(inst_id=inst_id, side=sl_side, amount="0",
                                 sl_trigger_px=sl_px, pos_side=trade["direction"])
        new_id = (res.get("data") or [{}])[0].get("algoId")
        if new_id:
            trade["sl_algo_id"] = new_id; trade["current_sl"] = entry
        trade["pyramid_added"] = True
        dc_log(f"📈 金字塔加碼成功 [{symbol}]：+{add_amt}張(基礎單位,達+1R)，停損上移至原進場價 {entry}")
        return True
    except Exception as e:
        dc_log(f"❌ 金字塔加碼失敗 [{trade.get('symbol')}]：{e}")
        trade["pyramid_added"] = True   # 失敗也標記，避免反覆重試
        return False
def _dh_cvd_ok(symbol_item: str, okx_bar_fmt: str, tf_id: str, direction: str) -> Tuple[bool, str]:
    """
    數據獵手合約 CVD 過濾（只用合約 perp CVD，與三層吸收不同）：
      15m 多：CVD[-1] < CVD[-4]（近3根淨賣壓，C3已確認反彈=被動買方吸收）
              WF 驗證 EV +0.073→+0.187（backtest_15m_sop.py）
      1H 空：CVD[-1] < max(CVD[-4:-1])（頂背離=主動買盤力竭），疊在階梯壓力上
              WF 驗證 1H空 +0.208→+0.287（_short_1h_dh.py）
    其他時框/方向不過濾。資料不足時拒絕（保守）。
    """
    if not ((tf_id == "15m" and direction == "long") or (tf_id == "1H" and direction == "short")):
        return True, "非DH適用時框/方向"
    cona_perp = CONA_PERP.get(symbol_item)
    if not cona_perp:
        return True, "無Coinalyze合約數據，略過DH-CVD"
    end_ts   = int(time.time() * 1000)
    start_ts = end_ts - (BAR_SECONDS[tf_id] * 30 * 1000)   # 近~30根
    cvd = calculate_cumulative_volume_delta(cona_perp, okx_bar_fmt, start_ts, end_ts)
    if len(cvd) < 4:
        return False, "DH-CVD數據不足"
    if tf_id == "15m":   # long 吸收
        ok = bool(cvd.iloc[-1] < cvd.iloc[-4])
        return (ok, "合約CVD吸收(近3根↓)確認" if ok else "合約CVD未吸收(非DH多)")
    else:                # 1H short 頂背離
        prior_max = max(cvd.iloc[-2], cvd.iloc[-3], cvd.iloc[-4])
        ok = bool(cvd.iloc[-1] < prior_max)
        return (ok, "合約CVD頂背離確認" if ok else "合約CVD無頂背離(非DH空)")


# ── 數據獵手做空 + ls_ratio/taker_ratio（OKX rubik 公開端點，快取5分；原幣安 fapi 被雲端IP封鎖）──
C3_15M_LONG_ENABLED = False      # 2026-07-01暫關:忠實複刻重測EV-0.078(n=566)且逐期惡化,原宣稱+0.133是裸訊號跟CVD/ls加碼子集混在一起,先關
C3_30M_LONG_ENABLED = False      # 2026-07-01暫關:補完Binance Vision主流資料後首測 n=82 EV-0.128(負),與舊「主流驗+0.173」矛盾,先關到查清楚
RESON_ENABLED = False            # 2026-07-01暫關:忠實複刻重測 雙底多EV-0.082(n=288)/雙頂空EV+0.010幾乎打平(n=279),原宣稱+0.062/+0.187樣本太小(n=8/15)不可信,先關
DH_SHORT_ENABLED = False         # 2026-07-01暫關:今日重測只驗證吞噬空+MACD空(忠實複刻),DH空未今日驗證,先關到驗完
# 15m 數據獵手做空(2B+CVD頂背離+OI升+ls>=2.5+taker>1.0)
DH_SHORT_MAJOR   = 96            # 大級別2B回看(96根/1天)
_LS_TAKER_CACHE: Dict[str, Any] = {}   # "coin|period" -> (ts, ls, taker)
_LS_FAIL = {"streak": 0, "skip_until": 0.0}   # OKX rubik 熔斷:連續失敗就停打一段,避免log洪水+timeout拖慢掃描

def _okx_rubik_period(period: str) -> str:
    """把策略時框映到 OKX rubik 支援的 period(僅 5m / 1H / 1D)。"""
    p = period.lower()
    if p in ("5m", "15m", "30m"): return "5m"
    if p in ("1d", "1day"):       return "1D"
    return "1H"

def _fetch_ls_taker(symbol_item: str, period: str = "15m"):
    """OKX 多空人數比 ls + 主動買賣比 taker。快取5分鐘。回傳(ls,taker)或(None,None)。
    來源改用 OKX rubik(long-short-account-ratio + taker-volume)取代被雲端IP地理封鎖的幣安 fapi。
    ls=最新一根多空帳戶比;taker=最近3根 buyVol/sellVol(近似幣安單期 buySellRatio)。
    熔斷:連續8次失敗→停打30分鐘,不再洪水log也不卡timeout。"""
    coin = symbol_item.split("/")[0].upper()   # BTC/USDT -> BTC
    pk = _okx_rubik_period(period)
    key = f"{coin}|{pk}"
    now = time.time()
    c = _LS_TAKER_CACHE.get(key)
    if c and now - c[0] < 300:
        return c[1], c[2]
    if now < _LS_FAIL["skip_until"]:    # 熔斷中:直接放棄,不打HTTP不log(掃描不卡)
        return None, None
    try:
        ls_d = _fetch_okx_public_data("/api/v5/rubik/stat/contracts/long-short-account-ratio",
                                      {"ccy": coin, "period": pk})            # [[ts, ratio], ...] 新→舊
        tk_d = _fetch_okx_public_data("/api/v5/rubik/stat/taker-volume",
                                      {"ccy": coin, "instType": "CONTRACTS", "period": pk})  # [[ts, sellVol, buyVol], ...]
        if not ls_d or not tk_d:
            raise ValueError("OKX rubik 空資料")
        ls = float(ls_d[0][1])                       # data[0]=最新
        k = min(3, len(tk_d))                        # 聚合最近3根穩定 taker(5m×3≈15m)
        buy  = sum(float(r[2]) for r in tk_d[:k])
        sell = sum(float(r[1]) for r in tk_d[:k])
        if sell <= 0:
            raise ValueError("sellVol=0")
        taker = buy / sell
        _LS_TAKER_CACHE[key] = (now, ls, taker)
        _LS_FAIL["streak"] = 0
        return ls, taker
    except Exception as e:
        _LS_FAIL["streak"] += 1
        if _LS_FAIL["streak"] >= 8:
            _LS_FAIL["skip_until"] = now + 1800
            print(f"[LS/Taker] OKX rubik 連續失敗→熔斷30分;DH空/維加斯/逆勢多本段噤聲: {str(e)[:50]}")
        return None, None

# 舊名相容別名(歷史呼叫點仍用 _fetch_binance_ls_taker;來源已改 OKX)
_fetch_binance_ls_taker = _fetch_ls_taker


def _check_dh_short(symbol_item: str, okx_bar_fmt: str, df: pd.DataFrame) -> Tuple[bool, str]:
    """數據獵手做空(15m,WF驗證+0.153)：大級別2B(戳破96根高收回)+CVD頂背離+OI升6根+ls>=2.5+taker>1.0"""
    try:
        hi = df["high"].values; cl = df["close"].values
        if len(hi) < DH_SHORT_MAJOR + 4: return False, ""
        prior_high = hi[-(DH_SHORT_MAJOR+1):-1].max()   # 過去96根(不含當根)
        if not (hi[-1] > prior_high and cl[-1] < prior_high): return False, ""   # 2B假突破收回
        cona_perp = CONA_PERP.get(symbol_item)
        if not cona_perp: return False, ""
        end_ts = int(time.time()*1000); start_ts = end_ts - (BAR_SECONDS["15m"]*40*1000)
        cvd = calculate_cumulative_volume_delta(cona_perp, okx_bar_fmt, start_ts, end_ts)
        oi  = fetch_open_interest_series(cona_perp, okx_bar_fmt, start_ts, end_ts)
        if len(cvd) < 4 or len(oi) < 7: return False, ""
        if not (cvd.iloc[-1] < max(cvd.iloc[-2], cvd.iloc[-3], cvd.iloc[-4])): return False, ""  # CVD頂背離
        if not (oi.iloc[-1] > oi.iloc[-7]): return False, ""                                       # OI升6根
        ls, taker = _fetch_binance_ls_taker(symbol_item)
        if ls is None or ls < 2.5: return False, ""
        if taker is None or taker <= 1.0: return False, ""
        return True, f"ls{ls:.1f}+taker{taker:.2f}"
    except Exception as e:
        print(f"[DH-Short] {symbol_item} 失敗: {e}")
        return False, ""


# ── 箱突破做空(15m)：破窄箱底+帶量+CVD↓+OI升(WF +0.193,出場1.5R/3R)──────────────
VEGAS_SHORT_ENABLED = False  # 2026-07-01忠實複刻重測確認關閉:7期間n=70,EV-0.229(PF0.65),5/7期負,補資料後更負,維持關
BOX_SHORT_ENABLED = True     # 2026-07-01忠實複刻重測轉正式:7期間n=2228,EV+0.124,23Q4~24Q1負(早期)、24Q2起連續5期同號正(18個月),非雜訊,開啟
# 15m 維加斯大通道 fade 做空(2026-06-13,WF驗+0.182/MDD16%/各年不虧)
def _check_box_short(symbol_item: str, okx_bar_fmt: str, df: pd.DataFrame) -> Tuple[bool, str]:
    """箱突破做空：96根窄箱(range<8%)收盤跌破箱底 + 帶量1.5x + CVD↓ + OI升(3根)。只用現成資料。"""
    try:
        hi = df["high"].values; lo = df["low"].values; cl = df["close"].values
        vol = df["vol"].values if "vol" in df.columns else None
        if len(cl) < 100 or vol is None: return False, ""
        bh = hi[-97:-1].max(); bl = lo[-97:-1].min()
        if bl <= 0 or (bh-bl)/bl > 0.08: return False, ""        # 箱要夠窄=盤整
        if not (cl[-1] < bl and cl[-2] >= bl): return False, ""  # 收盤首根跌破箱底
        va = float(np.mean(vol[-21:-1]))
        if not (va > 0 and vol[-1] > 1.5*va): return False, ""    # 帶量突破
        cona = CONA_PERP.get(symbol_item)
        if not cona: return False, ""
        end_ts = int(time.time()*1000); start_ts = end_ts - (BAR_SECONDS["15m"]*40*1000)
        cvd = calculate_cumulative_volume_delta(cona, okx_bar_fmt, start_ts, end_ts)
        oi  = fetch_open_interest_series(cona, okx_bar_fmt, start_ts, end_ts)
        if len(cvd) < 2 or len(oi) < 4: return False, ""
        if not (cvd.iloc[-1] < cvd.iloc[-2]): return False, ""    # CVD↓(賣方主導)
        if not (oi.iloc[-1] > oi.iloc[-4]): return False, ""      # OI升(新空進場)
        return True, "破窄箱底+帶量+CVD↓+OI升"
    except Exception as e:
        print(f"[Box-Short] {symbol_item} 失敗: {e}")
        return False, ""


ENGULF_SHORT_ENABLED = True   # 山寨看跌吞噬空(1H,2026-06-24 WF):放量吞噬+價<EMA100下跌regime。★2026-07-01忠實複刻重測(真main.py邏輯/7個不重疊期間23Q4~25H2/n=550):EV+0.176,7/7期全正,PF1.29,轉正式(拿掉觀察標籤)。限非主流山寨,純價量不碰OI/CVD,固定2R,SL近高。
def _check_engulf_short(symbol_item: str, df: pd.DataFrame) -> Tuple[bool, str]:
    """山寨看跌吞噬空(1H):①陰線吞噬前陽線實體 ②量>1.3×近24均量 ③收盤<EMA100(下跌regime) ④在近12根高附近(空頂部)。
    純價量,3個獨立時期樣本外複製超額vs隨機+0.15。注意:df已去掉未收盤當根,[-1]=最新已收盤。"""
    try:
        op = df["open"].values; hi = df["high"].values; cl = df["close"].values
        vol = df["vol"].values if "vol" in df.columns else None
        if vol is None or len(cl) < 130: return False, ""
        ema100 = pd.Series(cl).ewm(span=100, adjust=False).mean().values
        if not (cl[-1] < ema100[-1]): return False, ""                              # 下跌regime
        if not (hi[-1] >= hi[-13:-1].max() * 0.997): return False, ""               # 在近12根高附近(頂)
        if not ((cl[-1] < op[-1]) and (cl[-2] > op[-2]) and (op[-1] >= cl[-2]) and (cl[-1] <= op[-2])):
            return False, ""                                                         # 看跌吞噬(陰吞前陽實體)
        va = float(np.mean(vol[-25:-1]))
        if not (va > 0 and vol[-1] > 1.3 * va): return False, ""                     # 放量
        return True, "看跌吞噬+放量+價<EMA100"
    except Exception as e:
        print(f"[Engulf-Short] {symbol_item} 失敗: {e}")
        return False, ""


OI_SQUEEZE_ENABLED = False  # 2026-07-01暫關:今日忠實複刻重測樣本太小(n=14,3段期間1/3/10筆),EV+0.407不可信,先關到樣本夠大再開
# 主力建倉壓縮突破(1H,2026-06-13):12h壓縮<3%+帶量突破+OI升+4H regime,讓跑
def _check_oi_squeeze(symbol_item: str, okx_bar_fmt: str, df: pd.DataFrame, okx_swap_symbol: str):
    """主力建倉壓縮突破(1H):12h窄幅壓縮<3% + 帶量突破range(噴出) + 12h OI升>5%(建倉) + 4H regime順向。
    coiled spring:壓得越緊彈越大。回 'long'/'short'/None。WF驗+0.309/賺賠3.1/MDD6%/各年正(讓跑出場)。"""
    try:
        hi = df["high"].values; lo = df["low"].values; cl = df["close"].values; vol = df["vol"].values
        if len(cl) < 25: return None
        rh = float(hi[-13:-1].max()); rl = float(lo[-13:-1].min())
        if rl <= 0 or (rh - rl) / rl > 0.03: return None              # 12h壓縮<3%(coiled spring)
        side = "long" if cl[-1] > rh else ("short" if cl[-1] < rl else None)
        if side is None: return None
        va = float(np.mean(vol[-21:-1]))
        if not (va > 0 and vol[-1] > 1.5 * va): return None           # 帶量突破=噴出
        cona = CONA_PERP.get(symbol_item)
        if not cona: return None
        _e = int(time.time() * 1000); _s = _e - (BAR_SECONDS["1H"] * 16 * 1000)
        oi = fetch_open_interest_series(cona, okx_bar_fmt, _s, _e)
        if len(oi) < 13 or oi.iloc[-13] <= 0: return None
        if (oi.iloc[-1] - oi.iloc[-13]) / oi.iloc[-13] < 0.05: return None   # 12h OI升>5%(主力建倉)
        # CVD確認方向(防假突破:真突破帶主動流,假突破沒)。多需CVD↑、空需CVD↓。WF:勝率43%→50%、+0.309→+0.395。
        _ce = int(time.time() * 1000); _cs = _ce - (BAR_SECONDS["1H"] * 6 * 1000)
        cvd = calculate_cumulative_volume_delta(cona, okx_bar_fmt, _cs, _ce)
        if len(cvd) >= 4:
            if side == "long" and not (cvd.iloc[-1] > cvd.iloc[-4]): return None
            if side == "short" and not (cvd.iloc[-1] < cvd.iloc[-4]): return None
        d4 = fetch_market_candles(okx_swap_symbol, "4H")
        if d4.empty or len(d4) < 60: return None
        e50 = d4["close"].ewm(span=50, adjust=False).mean()
        up4h = e50.iloc[-1] > e50.iloc[-2]
        if (side == "long" and not up4h) or (side == "short" and up4h): return None   # 4H regime順向
        return side
    except Exception as e:
        print(f"[OI-Squeeze] {symbol_item} 失敗: {e}")
        return None

CONV_BREAKOUT_ENABLED = False  # 2026-07-01暫關:今日忠實複刻7期WF(5期有OI資料)EV+0.077/勝38%/PF1.21/4/5期正,
# 但24Q4牛市+25H1本地資料缺OI欄位完全沒測到(最該驗證牛市表現的兩期是黑箱)→用力回答顧問團一致:先不上,
# 待牛市段資料補齊再重評。見council-report-20260701c.html。
# 主流收斂突破+OI升 1H做多(2026-06-21 session WF:T1主流訓+0.19/驗+0.17;限BTC/ETH/SOL)
CONV_MAJORS = ("BTC/USDT", "ETH/USDT", "SOL/USDT")

C3_1H_SHORT_OIV2_ENABLED = True  # 2026-07-01上線:1H C3空+v2 OI評分確認層(anomaly卡逆推公式,結構分級±4~40非固定±24)
# 7期忠實複刻WF(3期24Q4牛/25H1/25H2,C3空需576/676根慢速EMA故只用連續夠長期間避開單季warmup汙染regime):
# 裸基底EV+0.027/勝52%/PF1.10(3/3正)→疊確認EV+0.125/勝54%/PF1.46(訊號砍半,25H2 n=9太薄轉負但基底健康)。
# 用力回答顧問團一致建議:風險等於原策略子集(濾網非新增風險源),上線觀察規則=連續20張成交單對照+0.125/54%,不達標即關。
def _check_c3short_oiv2_confirm(symbol_item, okx_bar_fmt, df, current_close):
    """v2 OI評分確認(結構分級±4~40,OI×價格象限,3%為weak/strong門檻):score<=-8才放行做空。
    見project_dh_anomaly_score.md,忠實對齊_bt_c3short_oiv2_faithful.py的oi_score_v2()。"""
    try:
        cona = CONA_PERP.get(symbol_item)
        if not cona: return True  # 查無OI映射(如非主流幣)→放行,避免OI缺失擋掉全部訊號
        _e = int(time.time() * 1000); _s = _e - (BAR_SECONDS["1H"] * 4 * 1000)
        oi = fetch_open_interest_series(cona, okx_bar_fmt, _s, _e)
        if len(oi) < 2 or oi.iloc[-2] <= 0: return True  # 資料不足→放行(同上,避免誤擋)
        oi_chg1 = (float(oi.iloc[-1]) / float(oi.iloc[-2]) - 1.0) * 100.0
        prev_close = float(df["close"].iloc[-2])
        chg1 = (current_close / prev_close - 1.0) * 100.0 if prev_close > 0 else 0.0
        strong = abs(oi_chg1) >= 3.0
        ramp = max(0.0, min(1.0, (abs(oi_chg1) - 3.0) / 10.0))
        oi_up = oi_chg1 > 0; price_up = chg1 > 0
        if oi_up and not price_up:      s = -((24 + ramp*16) if strong else 12)   # 主動做空
        elif (not oi_up) and not price_up: s = -((8 + ramp*8) if strong else 4)   # 多頭出場(弱空)
        elif oi_up and price_up:        s = (24 + ramp*16) if strong else 12      # 主動做多(逆向,擋)
        else:                           s = (8 + ramp*8) if strong else 4         # 空頭出場(逆向,擋)
        return s <= -8
    except Exception as e:
        print(f"[C3空OIv2] {symbol_item} 判斷失敗(放行): {e}")
        return True
SHORT_POC_GATE_ENABLED = True   # 籌碼支撐閘(2026-06-21 session WF:在POC下方才空,砍掉「支撐上方做空被彈」流血空單,訓-0.025→-0.010)
LONG_POC_GATE_ENABLED  = True   # 籌碼壓力閘(對稱):收盤要站上POC才做多,擋「追進壓力被打回」(POC主流做多+0.069→+0.095)
def _vp_poc(df, W=120, nb=50):
    """Volume Profile:近W根成交量分布,回 (POC, VAH, VAL) 價值區70%上下緣。資料不足回None。"""
    try:
        cl = df["close"].values; vol = df["vol"].values
        if len(cl) < 30: return None
        a = max(0, len(cl) - W); px = cl[a:]; vv = vol[a:]
        lo_, hi_ = float(px.min()), float(px.max())
        if hi_ <= lo_: return None
        edges = np.linspace(lo_, hi_, nb + 1); idx = np.clip(np.digitize(px, edges) - 1, 0, nb - 1)
        vh = np.zeros(nb)
        for k, v in zip(idx, vv): vh[k] += v
        centers = (edges[:-1] + edges[1:]) / 2; poc = float(centers[int(vh.argmax())])
        order = np.argsort(vh)[::-1]; tot = float(vh.sum()); acc = 0.0; sel = []
        for k in order:
            sel.append(int(k)); acc += vh[k]
            if acc >= 0.7 * tot: break
        selc = centers[sorted(sel)]
        return poc, float(selc.max()), float(selc.min())
    except Exception:
        return None

def _check_conv_breakout(symbol_item, okx_bar_fmt, df, okx_swap_symbol):
    """收斂突破+OI升 1H做多(限主流):結構式收斂(高點降低+低點墊高)+收盤破近5高(不過度延展)+價在EMA50上(順勢)
    +OI升(建倉)。session WF:T1主流訓+0.19/驗+0.17,進得比OI_SQUEEZE便宜(延展0.74)。讓跑swing_full(吃轉折加碼)。
    ★山寨無效(技術/數據皆驗證負)故限主流。回 'long'/None。"""
    try:
        if symbol_item not in CONV_MAJORS: return None
        hi = df["high"].values; lo = df["low"].values; cl = df["close"].values
        if len(cl) < 60: return None
        i = len(cl) - 1; W = 20; a = i - W; m = i - W // 2
        ema50 = float(df["close"].ewm(span=50, adjust=False).mean().iloc[-1])
        if cl[-1] <= ema50: return None                                              # 順勢:價在EMA50上
        if not (hi[m:i+1].max() <= hi[a:m].max() and lo[m:i+1].min() >= lo[a:m].min()): return None  # 收斂
        rng = (hi[a:i+1].max() - lo[a:i+1].min()) / cl[-1]
        if not (0.01 < rng < 0.18): return None                                      # 範圍別太寬(非趨勢中段)
        rh5 = float(hi[i-5:i].max())
        if not (cl[-1] > rh5 and cl[-1] <= rh5 * 1.03): return None                  # 收盤破近高,不過度延展(治追)
        cona = CONA_PERP.get(symbol_item)
        if not cona: return None
        _e = int(time.time() * 1000); _s = _e - (BAR_SECONDS["1H"] * 8 * 1000)
        oi = fetch_open_interest_series(cona, okx_bar_fmt, _s, _e)
        if len(oi) < 4 or oi.iloc[-4] <= 0: return None
        if oi.iloc[-1] <= oi.iloc[-4]: return None                                   # OI升(建倉)
        return "long"
    except Exception as e:
        print(f"[ConvBreak] {symbol_item} 失敗: {e}")
        return None


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
        self.hf_last_bar:   Dict[str, int]   = {}   # 高頻層K棒去重(key=symbol_tf_dir),同根不重複下hf倉
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
        if _daily_stop_active():   # 每日虧損熔斷:只擋新倉,既有倉由 check_trailing 照常管理
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

    # 2b. ── 高頻固定1R 獨立偵測(各跑各的,不靠主訊號):15m/30m MACD空多帶量,全市值,純固定1R無保本 ──
        #   驗證(live停損_find_pivot,含費WF,拆T1/T2/T3全正):15m空+0.103/勝59% 30m空+0.097 15m多+0.070
        #   30m多+0.032,均勝55-59%(~6成)。SL=live同款pivot+ATR緩衝;TP=進場±1R掛交易所;0.5R保本已拿掉。
        if HF_1R_ENABLED and tf_id in ("15m", "30m") and AUTO_TRADE.get(tf_id) and bar_ts != 0 \
           and (not HF_MAJORS_ONLY or symbol_item in CONV_MAJORS):   # ★瘦身:HF限主流(砍山寨裸MACD爆量+流血)
            try:
                _hf4h = fetch_market_candles(okx_swap_symbol, "4H")
                if not _hf4h.empty and len(_hf4h) > 200:
                    _e2 = _hf4h["close"].ewm(span=200, adjust=False).mean()
                    _up4 = _e2.iloc[-1] > _e2.iloc[-2]
                    _hd, _ha, _hh = calculate_macd(df["close"])
                    _gold = _hd.iloc[-2] <= _ha.iloc[-2] and _hd.iloc[-1] > _ha.iloc[-1]
                    _dead = _hd.iloc[-2] >= _ha.iloc[-2] and _hd.iloc[-1] < _ha.iloc[-1]
                    _vv = df["vol"].values
                    _vavg = float(np.mean(_vv[-21:-1])) if len(_vv) >= 21 else 0.0
                    _vok = _vavg > 0 and _vv[-1] > 1.5 * _vavg
                    _hfd = None
                    if _vok and _up4 and _gold and macd_difslope_ok(_hd, "long"): _hfd = "long"
                    elif HF_SHORT_ENABLED and _vok and (not _up4) and _dead and macd_difslope_ok(_hd, "short"): _hfd = "short"
                    if _hfd:
                        _hk = f"{symbol_item}_{tf_id}_{_hfd}_hf"
                        if self.hf_last_bar.get(_hk) != bar_ts:   # 每根K只下一次,防輪詢重複洗單
                            _hp = BEST_PARAMS.get(f"{tf_id}_{_hfd}", {})
                            _hsl = (_find_pivot_low(df, _hp.get("structure_lookback", 5), _hp.get("sl_atr_buffer", 0.0))
                                    if _hfd == "long" else
                                    _find_pivot_high(df, _hp.get("structure_lookback", 5), _hp.get("sl_atr_buffer", 0.0)))
                            _hrisk = abs(current_close - _hsl)
                            if _hsl and _hrisk > 0 and 0.001 < _hrisk / current_close <= MAX_SL:
                                _htp = round(current_close + _hrisk, 6) if _hfd == "long" else round(current_close - _hrisk, 6)
                                # ★空→hf_1r固定1R(快進快出,15m空+0.103/勝59%);多→swing_full讓跑(平滑曲線+對沖,
                                #   WF驗證段含費拆層全正15m+0.107/賺賠2,固定1R多沒用報酬/MDD1.3故改讓跑)。
                                _hf_es = "hf_1r" if _hfd == "short" else "swing_full"
                                _tag = "固定1R" if _hfd == "short" else "讓跑"
                                self.hf_last_bar[_hk] = bar_ts
                                try:
                                    if EXCHANGE_ENABLED.get("okx", True) and not _dir_skew_block(_hfd, "okx"):
                                        execute_okx_trade_pipeline(okx_swap_symbol, _hfd, current_close,
                                            _hsl, _htp, _htp, "fixed", tf_id,
                                            position_scale=_concentration_mult(_hfd, "okx"),
                                            pyramid_eligible=False, exit_strategy=_hf_es)
                                    if EXCHANGE_ENABLED.get("bingx", True) and not _dir_skew_block(_hfd, "bingx"):
                                        execute_bingx_trade_pipeline(symbol_item, _hfd, current_close,
                                            _hsl, _htp, _htp, "fixed", tf_id,
                                            position_scale=_concentration_mult(_hfd, "bingx"), exit_strategy=_hf_es)
                                    dc_log(f"⚡ 高頻層({tf_id} MACD{_hfd}帶量·{_tag}):{symbol_item} 進場`{current_close}` SL`{round(_hsl,6)}`")
                                except Exception as _he:
                                    print(f"[HF] {symbol_item} {tf_id} 平行倉失敗: {_he}")
            except Exception as _hfe:
                print(f"[HF-1R-detect] {symbol_item} {tf_id} 偵測失敗: {_hfe}")

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

        # ── 數據獵手 CVD 加碼（C方案）：15m 多單 CVD吸收確認 → 下注 ×DH_BOOST_MULT ──
        # 不過濾(保留全部15m多的頻率=複利引擎)，只把資金往高品質的CVD確認單傾斜。
        # 回測(全策略×階梯下注)：C×1.5 成長>不過濾基準、MDD還略低，優於硬性過濾(A)。
        # dh_boost 一律先設1.0(每個tf都會經過此行)，只有15m多且CVD確認才放大。
        dh_boost = 1.0
        if DH_CVD_ENABLED and is_long and tf_id == "15m":
            try:
                _dh_ok, _dh_r = _dh_cvd_ok(symbol_item, okx_bar_fmt, "15m", "long")
                _ls_l, _ = _fetch_binance_ls_taker(symbol_item)   # ls<1.0=散戶淨空=逆勢多(WF+0.138)
                if _dh_ok or (_ls_l is not None and _ls_l < 1.0):
                    dh_boost = DH_BOOST_MULT   # CVD吸收 或 散戶淨空 → 加碼下注
            except Exception as _dh_err:
                print(f"[DH-CVD] {symbol_item} 15m多加碼判斷失敗: {_dh_err}")

        # ── 數據獵手做空(15m)：2B+CVD頂背離+OI升6根+ls>=2.5+taker>1.0(WF驗證+0.153)──
        is_dh_short = False; _dh_short_r = ""
        if DH_SHORT_ENABLED and tf_id == "15m":
            try:
                is_dh_short, _dh_short_r = _check_dh_short(symbol_item, okx_bar_fmt, df)
            except Exception as _dse:
                print(f"[DH-Short] {symbol_item} 判斷失敗: {_dse}")
            # 2026-06-13 regime閘:DH是熊市空單(按年:2022 EV+7.3/+72.9R,2024-25牛市轉負-30R)。
            #   只在 4H EMA200 下彎(下跌趨勢)才放行,牛市自動噤聲。加碼保留(熊市加碼+72.9>純跑+44.6)。
            if is_dh_short:
                try:
                    _d4dh = fetch_market_candles(okx_swap_symbol, "4H")
                    if not _d4dh.empty and len(_d4dh) > 200:
                        _e2dh = _d4dh["close"].ewm(span=200, adjust=False).mean()
                        if _e2dh.iloc[-1] >= _e2dh.iloc[-2]:   # 沒下彎 → 非熊 → 取消
                            is_dh_short = False
                            print(f"[DH-Short] {symbol_item} 4H非下跌趨勢,regime閘擋下")
                except Exception as _dhr:
                    print(f"[DH-Short] {symbol_item} regime閘失敗(放行): {_dhr}")

        # ── 箱突破做空(15m)：破窄箱底+帶量+CVD↓+OI升(WF+0.193,出場1.5R/3R)──
        is_box_short = False
        if BOX_SHORT_ENABLED and tf_id == "15m":
            try:
                is_box_short, _ = _check_box_short(symbol_item, okx_bar_fmt, df)
            except Exception as _bse:
                print(f"[Box-Short] {symbol_item} 判斷失敗: {_bse}")
            # ICT Killzone 過濾：突破=流動性獵取,只在機構活躍時段做(UTC換算亞洲/倫敦/紐約)。
            # 回測WF(箱頂止損):全時段 驗+0.175 → killzone過濾 驗+0.273,MDD 48%→26%。
            if is_box_short:
                _kzh = datetime.now(timezone.utc).hour
                if not ((0 <= _kzh < 4) or (6 <= _kzh < 9) or (12 <= _kzh < 15) or (18 <= _kzh < 19)):
                    is_box_short = False

        # ── 維加斯大通道 fade 做空(15m,2026-06-13)：價由下回測大通道(e576/676)被當壓力擋下
        #   + 1H空方共振 + CVD↓ + ls≥2(擠多頭軋空)。WF驗+0.182/勝53%/MDD16%/各年不虧,牛市也正。
        #   = 第一支「牛市也能賺」的15m逆勢空(靠1H共振蓋過上漂)。出場固定R(預設"")。
        is_vegas_short = False; _veg_r = ""
        if VEGAS_SHORT_ENABLED and tf_id == "15m":
            try:
                _bd = float(min(ema576.iloc[-1], ema676.iloc[-1]))   # 大通道下緣
                _prev_below = float(df["close"].iloc[-2]) < _bd        # 前一根在大通道下(由下往上)
                _retest = abs(current_close - _bd) <= 0.5 * current_atr  # 回測下緣±0.5ATR
                _rejected = current_close < _bd and current_close < float(df["open"].iloc[-1])  # 被擋回+收黑
                if _prev_below and _retest and _rejected:
                    _ls_v, _ = _fetch_binance_ls_taker(symbol_item)   # ls≥2(便宜,快取)先擋
                    if _ls_v is not None and _ls_v >= 2.0:
                        _cona_v = CONA_PERP.get(symbol_item)
                        if _cona_v:
                            _e = int(time.time()*1000); _s = _e - (BAR_SECONDS["15m"]*40*1000)
                            _cvd_v = calculate_cumulative_volume_delta(_cona_v, okx_bar_fmt, _s, _e)
                            if len(_cvd_v) >= 4 and _cvd_v.iloc[-1] < _cvd_v.iloc[-4]:   # CVD↓
                                _d1v = fetch_market_candles(okx_swap_symbol, "1H")
                                if not _d1v.empty and len(_d1v) > 600:
                                    _e144v = _d1v["close"].ewm(span=144, adjust=False).mean()
                                    _e576v = _d1v["close"].ewm(span=576, adjust=False).mean()
                                    if _e144v.iloc[-1] < _e576v.iloc[-1] and _d1v["close"].iloc[-1] < _e144v.iloc[-1]:
                                        is_vegas_short = True; _veg_r = f"ls{_ls_v:.1f}+1H空+CVD↓"
                                        print(f"[Vegas空] {symbol_item} 回測大通道被擋 {_veg_r}")
            except Exception as _vse:
                print(f"[Vegas-Short] {symbol_item} 判斷失敗: {_vse}")

        # ── 山寨看跌吞噬空(1H,2026-06-24)：放量吞噬+價<EMA100下跌regime。限非主流山寨,純價量。
        #   3時期樣本外複製(超額vs隨機+0.15一致),牛市靠EMA100閘自動噤聲不流血。固定2R,SL近高。
        is_engulf_short = False
        if ENGULF_SHORT_ENABLED and tf_id == "1H" and symbol_item not in MAJOR_COINS:
            try:
                is_engulf_short, _ = _check_engulf_short(symbol_item, df)
            except Exception as _ese:
                print(f"[Engulf-Short] {symbol_item} 判斷失敗: {_ese}")

        # ── 1H 空單階梯壓力過濾（WF 驗證：靠壓力位才做空, EV +0.182→+0.313）──────
        # 只作用於 1H 空單（15m 多單回測顯示階梯過濾有害，不套用）。
        # C3 空訊號成立後，要求進場價在某條階梯 Fibo 線 ±0.5×ATR 內才放行。
        if is_short and tf_id == "1H":
            try:
                _lad = ladder_fib_lines(df)
                if not near_ladder(current_close, _lad, float(current_atr), tol=0.5):
                    is_short = False   # 不靠壓力位 → 取消這筆空單
            except Exception as _lad_err:
                print(f"[Ladder] {symbol_item} 階梯過濾失敗: {_lad_err}")
            # 註：曾試「1H空再加CVD頂背離」，但全期指標顯示 EV 不變(+0.141)、累積R反降
            # (17.6→14.7R)，WF的+0.287是n=40小樣本假象 → 不加，階梯壓力本身才是edge。

            # ── v2 OI評分確認層(2026-07-01):階梯壓力過關後,再要求OI結構同向做空(score<=-8)。
            if is_short and C3_1H_SHORT_OIV2_ENABLED:
                if not _check_c3short_oiv2_confirm(symbol_item, okx_bar_fmt, df, current_close):
                    is_short = False
                    print(f"[C3空OIv2] {symbol_item} OI結構不同向做空,擋(v2確認未過)")

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

        # ── 突破閘(2026-06-13,回看24根=用戶指正96太長→進場太晚成本爛):動量策略(MACD/W底)要求
        #   價格已穿過前24根S/R才進(空:跌破前24低;多:突破前24高)。回看96→24:1H空+0.567→+0.655、
        #   1H多+0.218→+0.410、15m多/W底持平但訊號更多。更早觸發=更好成本+更多訊號。
        #   突破訊號 position_scale×1.5(少而重)。回踩/反轉(C3/DH/共振)不適用,不套。
        _N_BRK = 24
        _hN = df["high"].values; _lN = df["low"].values
        # ★防盤整假突破(2026-06-15,用戶觀察「大多盤整→突破假突破虧」):ADX<20=盤整,不做突破。
        #   WF:1H MACD多 +0.640→+0.825、賺賠2.2→3.2、砍45%盤整爛單。ADX>25太嚴。治-46%主因。
        _adx_trend = (current_adx >= 20)   # NaN→False→盤整期不突破(安全)
        _brk_up = _adx_trend and len(_hN) >= _N_BRK+1 and current_close > float(_hN[-(_N_BRK+1):-1].max())   # 多:突破前24高+有趨勢
        _brk_dn = _adx_trend and len(_lN) >= _N_BRK+1 and current_close < float(_lN[-(_N_BRK+1):-1].min())   # 空:跌破前24低+有趨勢
        if tf_id == "1H" and _adx_trend and not _brk_dn:   # 1H空額外:跌破維加斯大通道也算(訊號×3、WF驗+0.39,補1H空量)
            try: _brk_dn = current_close < float(min(ema576.iloc[-1], ema676.iloc[-1]))
            except Exception: pass

        # ── 雙底(W底)第二套訊號（OR 邏輯，獨立觸發）──────────────────────
        # 回測結論（backtest_wm_variants.py）：
        #   W底做多：1H +0.265、15m +0.068（C現狀版穩健）→ 僅 1H 啟用，與 WF 一致
        #   M頭做空：四版兩時框幾乎全賠 → 單獨關閉
        # 故：雙底僅 1H 做多；雙頂(M頭)單獨做空已停用。
        # ★2026-06-16 W底限主流幣:17幣實測,W底 edge 只在 BTC/ETH/SOL 成立(訓+0.25/驗+0.21),
        #   山寨全負(訓-0.11/驗-0.03/MDD71%,gap≤6%也救不活訓-0.14)→這是WLD式山寨W底虧損的根因。
        #   MACD多/空 edge 在山寨成立(故不限),唯W底型態在山寨無效→只在主流做。
        if tf_id == "1H" and symbol_item in ("BTC/USDT", "ETH/USDT", "SOL/USDT"):
            is_double_bottom = check_double_bottom(df, tf_id) and _brk_up   # +突破閘
            # ★W底 gap≤6%(2026-06-16):底(止損)到進場>6%=山寨追太高(WLD式 0.4271→0.50)→撤W底。
            #   回測(3幣WF):+0.363→+0.375、MDD 11%→9%,主流幣均僅3.8%、擋掉WLD式極端。早撤=讓下游
            #   combined_long/exit_strategy 全一致(避免算完risk才撤造成exit_strategy殘留)。
            if is_double_bottom:
                _wb_sl = _find_pivot_low(df, p_l["structure_lookback"], p_l.get("sl_atr_buffer", 0.0))
                if _wb_sl < current_close and (current_close - _wb_sl) / current_close > 0.06:
                    is_double_bottom = False
                    if _dbg: print(f"[W底gap] {symbol_item} 底到進場{(current_close-_wb_sl)/current_close:.1%}>6%→撤W底", flush=True)
            if is_double_bottom: dh_boost = BOOST_MULT                             # 突破高品質→×1.5
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
        if tf_id == "15m" and RESON_ENABLED:
            _rsi = calculate_smooth_rsi(df["close"], 14)
            _rsi_now  = _rsi.iloc[-1]
            _rsi_prev = _rsi.iloc[-2]
            rsi_up50   = _rsi_prev < 50 and _rsi_now >= 50   # 上穿50
            rsi_down50 = _rsi_prev >= 50 and _rsi_now < 50   # 下穿50
            if rsi_up50 and check_double_bottom(df, tf_id):
                is_reson_long = True
            if rsi_down50 and check_double_top(df, tf_id):
                is_reson_short = True

        # ── MACD 多週期動能濾波（WF 驗證穩健）─────────────────────────────────
        # backtest_macd_wf.py：
        #   15m 多 + 快線斜率：訓練+0.102→驗證+0.104（n=1523）✅ 最強
        #   1H  空 + 快線斜率：訓練+0.105→驗證+0.084（n=378）✅ 補1H空單
        # 規則：4H EMA200 斜率定向 + 當時框 MACD(12,26,9) 交叉 + 快線DIF斜率加速(非收腳)
        is_macd_long = False
        is_macd_short = False
        if tf_id in ("15m", "1H"):
            try:
                df4h_macd = fetch_market_candles(okx_swap_symbol, "4H")
                if not df4h_macd.empty and len(df4h_macd) > 200:
                    e200 = df4h_macd["close"].ewm(span=200, adjust=False).mean()
                    trend_up_4h = e200.iloc[-1] > e200.iloc[-2]   # 4H EMA200 斜率
                    dif, dea, _hist = calculate_macd(df["close"])
                    gold = dif.iloc[-2] <= dea.iloc[-2] and dif.iloc[-1] > dea.iloc[-1]
                    dead = dif.iloc[-2] >= dea.iloc[-2] and dif.iloc[-1] < dea.iloc[-1]
                    # 15m 多 升級:加帶量(2026-06-12)。裸進場太鬆=驗-0.022/MDD92%(線上舊狀);
                    #   +帶量→驗+0.168/勝58%/MDD33%,砍73%雜訊單。tFlow對15m多無增益故不加(便宜上)。
                    if tf_id == "15m" and trend_up_4h and gold and macd_difslope_ok(dif, "long") and _brk_up:
                        _vol15 = df["vol"].values
                        _va15 = float(np.mean(_vol15[-21:-1])) if len(_vol15) >= 21 else 0.0
                        if _va15 > 0 and _vol15[-1] > 1.5 * _va15:
                            is_macd_long = True; dh_boost = BOOST_MULT            # 帶量+突破→×1.5
                            print(f"[MACD多15m] {symbol_item} 帶量+突破✓")
                    # ★15m MACD空(2026-06-16 新增,限BTC/ETH/SOL):4H↓+死叉+difslope+帶量+逐筆tFlow滾動z。
                    #   回測3幣WF:+tFlow|z|>1.0 訓+0.148/驗+0.254/MDD24%(訓驗同向=穩)。出場swing_full(pivot,見3593+段)。
                    #   只3幣(tflow_z限定)=天然避開無逐筆數據的山寨;不套突破閘(對齊回測,15m空無突破)。
                    if tf_id == "15m" and (not trend_up_4h) and dead and macd_difslope_ok(dif, "short"):
                        _bn15 = symbol_item.replace("/", "")
                        if _bn15 in _TFLOW_COINS:
                            _vol15s = df["vol"].values
                            _va15s = float(np.mean(_vol15s[-21:-1])) if len(_vol15s) >= 21 else 0.0
                            if _va15s > 0 and _vol15s[-1] > 1.5 * _va15s:
                                _net15, _z15, _zok15 = tflow_z(_bn15)
                                if _zok15 and _net15 < 0 and abs(_z15) >= 1.0:
                                    is_macd_short = True; dh_boost = BOOST_MULT
                                    print(f"[MACD空15m] {symbol_item} 帶量+tFlow空(net={_net15:+.0f} z={_z15:+.2f})✓")
                    if tf_id == "1H":
                        # 帶量(全幣)+逐筆tFlow+突破閘。WF:1H空+0.33→+0.52、1H多+0.28→+0.51,MDD→6%。
                        _vol = df["vol"].values
                        _va = float(np.mean(_vol[-21:-1])) if len(_vol) >= 21 else 0.0
                        _vol_ok = _va > 0 and _vol[-1] > 1.5 * _va
                        _bn_sym = symbol_item.replace("/", "")     # BTC/USDT → BTCUSDT
                        # 避地板:離10日低(240根)<1ATR=貼地板易被彈→不空(已跌破日低=真突破則放行)。WF驗+0.567→+0.631。
                        _lo1h = df["low"].values
                        _dlow = float(_lo1h[-240:].min()) if len(_lo1h) >= 240 else float(_lo1h.min())
                        _floor_ok = (current_close < _dlow) or ((current_close - _dlow) >= 1.0 * current_atr)
                        if _vol_ok and _brk_dn and _floor_ok and (not trend_up_4h) and dead and macd_difslope_ok(dif, "short"):
                            _tfok, _tfr = tflow_confirm(_bn_sym, "short")
                            if _tfok is not False:    # None(非3幣/thin/失敗)=放行,只靠帶量+突破
                                is_macd_short = True; dh_boost = BOOST_MULT
                                print(f"[MACD空] {symbol_item} 帶量+突破✓ {_tfr}")
                        if _vol_ok and _brk_up and trend_up_4h and gold and macd_difslope_ok(dif, "long"):
                            # ★延伸濾≤4ATR(2026-06-18):進場離e144>4ATR=噴過頭不追,治COAI/WLD/NEAR追頂。
                            #   WF:1H MACD多 +0.681→+0.905、山寨+0.385→+0.822/MDD3%。只套1H MACD多,不套W底/反轉/15m。
                            _e144 = float(df["close"].ewm(span=144, adjust=False).mean().iloc[-1])
                            _ext_ok = current_atr <= 0 or (current_close - _e144) / current_atr <= 4.0
                            _tfok, _tfr = tflow_confirm(_bn_sym, "long")
                            if _ext_ok and _tfok is not False:
                                is_macd_long = True; dh_boost = BOOST_MULT
                                print(f"[MACD多] {symbol_item} 帶量+突破✓ {_tfr}")
                            elif not _ext_ok:
                                print(f"[MACD多] {symbol_item} 噴過頭>4ATR,延伸濾擋(治追頂)")
            except Exception as _macd_err:
                print(f"[MACD] {symbol_item} {tf_id} 計算失敗: {_macd_err}")

        # ── C3 1H/多 停用：回測 EV −0.024 負期望（backtest_c3_bias.py）──────────
        # 1H 的多單改由雙底(W底, +0.265) 觸發；C3 做多僅保留 15m（+0.133）。
        # 1H 的 C3 做空（+0.073）與 15m C3 多空不受影響。
        if tf_id == "1H":
            is_long = False
        # ── C3 15m/多 暫關(2026-07-01):忠實複刻今日重測(真Binance Vision資料24山寨幣/6期間/含費)
        #   結果EV-0.078(n=566),且逐期惡化(23Q4+0.345→24Q3-0.403→25H2-0.342)。
        #   舊文件宣稱+0.133~+0.207是CVD/ls加碼子集的數字,裸C3本身今日測出是負的→先關到查清楚。
        if tf_id == "15m" and not C3_15M_LONG_ENABLED:
            is_long = False
        # ★2026-06-16 30m C3多 限主流:17幣實測,30m C3多 山寨訓-0.03/驗+0.005=無edge(主流驗+0.173)。
        #   C3順勢型態在山寨無效(同W底),只在 BTC/ETH/SOL 做。MACD/1H C3空 在山寨成立故不限。
        if tf_id == "30m" and is_long and symbol_item not in ("BTC/USDT", "ETH/USDT", "SOL/USDT"):
            is_long = False
        # ★30m C3多(限主流) 暫關(2026-07-01):過去缺BTC/ETH/SOL資料測不了,今晚補完Binance Vision
        #   資料後首次測出 n=82 EV=-0.128(負),跟舊「主流驗+0.173」矛盾,先關到查清楚哪個對。
        if tf_id == "30m" and is_long and not C3_30M_LONG_ENABLED:
            is_long = False

        # ── 30m/short 停用：分區回測顯示只在 2022 慢熊有效(+0.195)，
        #    牛市 −0.088、2025 急跌 −0.199(勝率0%)，全期 −0.023 負期望 → 停用。
        #    30m/long(WF驗證 +0.093 穩健) 保留。空單交給 15m 雙頂共振/C3空。
        if tf_id == "30m":
            is_short = False

        # 砍 15m C3空：含費 -0.057 純拖累(高頻堆相關性)。SNR/SMC 過濾層 WF 救不了
        # (HTF溢價過濾訓練可救但驗證崩=crypto逆勢空結構性逆風)。保留 1H C3空(階梯壓力)。
        if tf_id == "15m" and is_short:
            is_short = False

        # ── 主力建倉壓縮突破(1H,2026-06-13):12h壓縮<3%+帶量突破+OI升+4H regime,讓跑。雙向。
        is_oisq_long = False; is_oisq_short = False
        if OI_SQUEEZE_ENABLED and tf_id == "1H":
            try:
                _sq = _check_oi_squeeze(symbol_item, okx_bar_fmt, df, okx_swap_symbol)
                if _sq == "long":  is_oisq_long = True;  dh_boost = BOOST_MULT; print(f"[主力建多] {symbol_item} 壓縮突破噴出(×1.5)")
                elif _sq == "short": is_oisq_short = True; dh_boost = BOOST_MULT; print(f"[主力建空] {symbol_item} 壓縮突破噴出(×1.5)")
            except Exception as _sqe:
                print(f"[OI-Squeeze] {symbol_item} 判斷失敗: {_sqe}")

        # ── 收斂突破+OI升 1H做多(限主流,2026-06-21):結構式收斂+收盤破近高+順勢+OI升,讓跑(吃轉折加碼) ──
        is_conv_long = False
        if CONV_BREAKOUT_ENABLED and tf_id == "1H":
            try:
                if _check_conv_breakout(symbol_item, okx_bar_fmt, df, okx_swap_symbol) == "long":
                    is_conv_long = True; print(f"[收斂突破多] {symbol_item} 收斂+OI升(限主流,讓跑)")
            except Exception as _cbe:
                print(f"[ConvBreak] {symbol_item} 判斷失敗: {_cbe}")

        # ★2026-06-15 空單regime閘(實盤診斷:6/7-13空169筆-29;6/8空51筆0%勝-17.8;但6/10跌日空76%勝+11.7)。
        #   crypto空=regime依賴(只在下跌賺,上漲日狂賠)。用「價在4H EMA50之下」=靈敏(單日下殺即跌破,抓6/10;
        #   持續上漲時價在EMA50上,擋6/8)。比EMA200斜率快=不會太晚、又不擋掉單日下殺的好空。非DH積極空單適用。
        if (is_box_short or is_vegas_short or is_macd_short or is_oisq_short or is_engulf_short or (is_short and tf_id == "1H")):
            try:
                _d4s = fetch_market_candles(okx_swap_symbol, "4H")
                if not _d4s.empty and len(_d4s) > 60:
                    _e50s = _d4s["close"].ewm(span=50, adjust=False).mean()
                    _4h_dn = float(_d4s["close"].iloc[-1]) < float(_e50s.iloc[-1])   # 價在4H EMA50之下=下行
                    if not _4h_dn:
                        print(f"[空regime閘] {symbol_item} 4H在EMA50之上(非下行)→擋積極空單")
                        is_box_short = is_vegas_short = is_macd_short = is_oisq_short = is_engulf_short = False
                        if tf_id == "1H": is_short = False
            except Exception as _r4e:
                print(f"[空regime閘] {symbol_item} 失敗(放行): {_r4e}")

        # ── 籌碼支撐閘(2026-06-21):不在POC(籌碼支撐)上方做空,除非脫離(收盤<POC)。砍流血空單(空在支撐被彈) ──
        if SHORT_POC_GATE_ENABLED and (is_short or is_double_top or is_reson_short or is_macd_short
                                        or is_dh_short or is_box_short or is_vegas_short or is_oisq_short):
            try:
                _vp = _vp_poc(df)
                if _vp and current_close >= _vp[0]:   # 收盤仍在POC上方=籌碼支撐沒破→擋空
                    is_short = is_double_top = is_reson_short = is_macd_short = False
                    is_dh_short = is_box_short = is_vegas_short = is_oisq_short = False
                    print(f"[籌碼支撐閘] {symbol_item} 收盤在POC上方,擋空(防空在支撐被彈)")
            except Exception as _pge:
                print(f"[POC-Gate] {symbol_item} 失敗(放行): {_pge}")

        # ── 擺動低支撐防呆閘(2026-06-27 實盤教訓:ASTER空在0.58上方7.8%/FIL空在0.67上方9.6%被軋)──
        #   價在近期大支撐(200根擺動低)上方「反彈危險區(1.5~12%,沒跌破=支撐還守)」→擋空。
        #   POC閘用成交量重心,抓不到swing low這種大支撐;這個補上。跌破支撐(<1.5%)=breakdown放行。
        if (is_short or is_double_top or is_reson_short or is_macd_short or is_dh_short
                or is_box_short or is_vegas_short or is_oisq_short or is_engulf_short):
            try:
                _lows = df["low"].values; _lb = min(200, len(_lows))
                _sup = float(_lows[-_lb:].min())
                if _sup > 0:
                    _g = (current_close - _sup) / current_close
                    # ★2026-07-06 拿掉「黑K拒絕」豁免:C3空觸發根必為黑K→豁免形同虛設,7/1-7/6實盤被停損空單
                    #   6/8筆進場在危險區、停損後24h平均反噴3R+(WLFI×2/BICO/GRAM/CC)。
                    #   回測(_bt_c3short_supguard.py):C3空危險區子集EV-0.05/PF0.80,擋掉後EV+0.125→+0.403/PF1.46→2.24。
                    #   吞噬空例外保留(_bt_engulf_supguard.py:危險區恰是吞噬edge所在,+0.212 vs 圈外+0.063),不在封鎖名單。
                    if 0.015 < _g < 0.12:   # 貼大支撐1.5~12% → 擋空(吞噬空豁免)
                        print(f"[支撐防呆] {symbol_item} 收盤 {current_close:.6g} 在大支撐 {_sup:.6g} 上方 {_g*100:.1f}%(危險區),擋空(吞噬空豁免)")
                        is_short = is_double_top = is_reson_short = is_macd_short = False
                        is_dh_short = is_box_short = is_vegas_short = is_oisq_short = False
            except Exception as _sge:
                print(f"[支撐防呆] {symbol_item} 失敗(放行): {_sge}")

        # ── 籌碼壓力閘(2026-06-21,對稱空單版):不在POC(籌碼壓力)下方追多,除非站上(收盤>POC)。擋追進壓力被打回 ──
        if LONG_POC_GATE_ENABLED and (is_long or is_double_bottom or is_reson_long or is_macd_long or is_oisq_long or is_conv_long):
            try:
                _vpl = _vp_poc(df)
                if _vpl and current_close <= _vpl[0]:   # 收盤仍在POC下方=籌碼壓力沒突破→擋多
                    is_long = is_double_bottom = is_reson_long = is_macd_long = is_oisq_long = is_conv_long = False
                    print(f"[籌碼壓力閘] {symbol_item} 收盤在POC下方,擋多(防追進壓力被打回)")
            except Exception as _pgl:
                print(f"[POC-Gate-L] {symbol_item} 失敗(放行): {_pgl}")

        # 合併：C3 或 雙底 或 共振 或 MACD 任一成立即可觸發
        combined_long  = is_long  or is_double_bottom or is_reson_long  or is_macd_long or is_oisq_long or is_conv_long
        combined_short = is_short or is_double_top   or is_reson_short or is_macd_short or is_dh_short or is_box_short or is_vegas_short or is_oisq_short or is_engulf_short

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
            if is_macd_long:     _signal_source.append("MACD動能")
            if is_oisq_long:     _signal_source.append("主力建多")
            if is_conv_long:     _signal_source.append("收斂突破多")
        else:
            _signal_source = []
            if is_short:         _signal_source.append("C3")
            if is_double_top:    _signal_source.append("雙頂")
            if is_reson_short:   _signal_source.append("雙頂+RSI共振")
            if is_macd_short:    _signal_source.append("MACD動能")
            if is_dh_short:      _signal_source.append("數據獵手空")
            if is_box_short:     _signal_source.append("箱突破空")
            if is_vegas_short:   _signal_source.append("維加斯大通道空")
            if is_oisq_short:    _signal_source.append("主力建空")
            if is_engulf_short:  _signal_source.append("吞噬空")
        signal_source_tag = "+".join(_signal_source)

        # ── 出場策略分派（麥門切線/移動停利/加碼 PDF 正版，WF+離群終檢，2026-06-03）──────
        #   line_full  = 整倉切線(不掛TP,收盤破切線市價平全倉)
        #   line_add   = 整倉切線 + N型轉折遞減加碼(守3) ← 加碼.pdf
        #   swing_full = 整倉用最新轉折移SL(不掛TP) ← 1H移動停利
        #   tp_line    = TP1落袋半 + 剩半切線
        #   swing_tp   = TP1落袋半 + 剩半用最新轉折移SL ← 1H移動停利
        #   swing_tp_1h= TP1落袋半 + 剩半參1H轉折移SL(15m進場避雜訊)
        #   ""         = 固定R(現役 TP1/TP2)
        # 對齊 _mai_line_v2 / _mai_trail_1h / _mai_add / _mai_mtf_trail：
        #   DH空(加碼驗+0.890/RA3.46) → line_add; 30m C3多(驗+0.582) → line_full
        #   1H W底多(驗+0.165) → swing_tp; 1H MACD空(驗+0.251) → swing_full
        #   1H C3空+階梯(驗+0.263/MDD10%/RA2.54) → swing_full
        #   15m MACD多(參1H轉折驗+0.142/RA0.36) → swing_tp_1h
        #   箱突破空/15m C3多 → 固定R(切線/移SL未變好)
        exit_strategy = ""
        if is_dh_short:
            exit_strategy = "line_full"                                  # DH空：整倉切線讓跑(2026-06-13關加碼:
            #   按年顯示加碼只在強熊好(2022),震盪/牛市害它(2024純跑+0.32 vs 加碼-0.02)。切線出場不變,只去加碼。
        elif tf_id == "30m" and direction == "long" and is_long:
            exit_strategy = "line_full"                                  # 30m C3多：整倉切線
        elif tf_id == "1H" and direction == "long" and is_double_bottom:
            exit_strategy = "swing_tp"                                   # 1H W底多：TP1+轉折移SL
        elif tf_id == "1H" and direction == "short" and is_macd_short:
            exit_strategy = "line_full"                                  # ★1H MACD空→麥門切線整倉出場(2026-06-16):
            #   3幣WF驗證段:切線+0.795/MDD9% >> 現役pivot移SL +0.390、N字型+0.649、固定2R+0.655。
            #   crypto上漂=空單肉短,切線收盤破線即平=全場最強空單出場。山寨經3709行2.5R落袋規則銀行化。
        elif tf_id == "1H" and direction == "long" and is_macd_long:
            exit_strategy = "swing_full"                                 # 1H MACD多(新增):整倉轉折移SL讓跑(驗+0.605>TP1.5+0.465,順勢抱)
        elif tf_id == "1H" and direction == "short" and is_short:
            exit_strategy = "swing_full"                                 # 1H C3空+階梯：整倉pivot移SL(驗+0.263/MDD10%)
        elif tf_id == "15m" and direction == "long" and is_macd_long:
            exit_strategy = "swing_tp_1h"                                # 15m MACD多：TP1+參1H轉折移SL
        elif tf_id == "15m" and direction == "short" and is_macd_short:
            exit_strategy = "swing_full"                                 # ★15m MACD空(新)：整倉pivot移SL(保守版,回測訓+0.148/驗+0.254/MDD24%)
        elif is_box_short:
            exit_strategy = "box_trend"                                  # 箱突破空：1R保本+4R整倉大TP(讓趨勢跑)
        elif is_oisq_long or is_oisq_short:
            exit_strategy = "swing_full"                                 # OI壓縮突破：整倉轉折移SL讓跑(抓噴出尾,驗+0.309/賺賠3.1/MDD6%)
        elif is_conv_long:
            exit_strategy = "swing_full"                                 # 收斂突破多(限主流)：整倉轉折移SL讓跑(吃轉折加碼,session驗+0.17/加碼+0.8~1.0)

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
        # 箱突破空專屬出場 4R整倉大TP(讓趨勢跑;R掃描甜蜜點4R,EV+0.234/賺賠2.8最佳)
        # box_trend:單一全倉TP at 4R + 達1R保本(check_trailing處理);切線在crypto箱突破被反彈洗,不用。
        if direction == "short" and is_box_short:
            p = {**p, "tp1_mult": 4.0, "tp2_intraday_mult": 4.0, "tp2_swing_mult": 4.0}
        # 吞噬空：固定 2R 單一目標(回測 2R > 1.5R 早收;高點被拒的空要讓它跑到2R)
        if direction == "short" and is_engulf_short and not is_box_short:
            p = {**p, "tp1_mult": 2.0, "tp2_intraday_mult": 2.0, "tp2_swing_mult": 2.0}

        # 止損距離下限：太近=結構低點無效→倉位被放超大+一根K秒進秒損 → 寧可不下單
        MIN_SL_PCT = 0.006   # 0.6%
        if direction == "long":
            calculated_sl = _find_pivot_low(df, p["structure_lookback"], p.get("sl_atr_buffer", 0.0))
            risk_pct = abs(current_close - calculated_sl) / current_close
            # 結構低點在現價之上(無效) 或 止損過近(<MIN_SL) → 跳過(不用0.5%硬下=秒進秒損)
            if calculated_sl >= current_close or risk_pct < MIN_SL_PCT:
                if _dbg: print(f"[SL] {symbol_item} 多 止損無效/過近({risk_pct:.3%}<{MIN_SL_PCT:.1%})→跳過", flush=True)
                return
            if risk_pct > MAX_SL:
                calculated_sl = current_close * (1.0 - MAX_SL)
                risk_pct = MAX_SL
            is_swing   = self._get_4h_swing_flag(okx_swap_symbol, df, tf_id)
            tp2_mult   = p["tp2_swing_mult"] if is_swing else p["tp2_intraday_mult"]
            risk_dist  = current_close - calculated_sl
            tp1_target = current_close + risk_dist * p["tp1_mult"]
            tp2_target = current_close + risk_dist * tp2_mult
        else:
            if is_engulf_short:
                calculated_sl = round(float(df["high"].values[-4:].max()) + 0.15 * current_atr, 8)   # 吞噬空:近4根高+0.15ATR(對齊回測)
            else:
                calculated_sl = _find_pivot_high(df, p["structure_lookback"], p.get("sl_atr_buffer", 0.0))
            risk_pct = abs(calculated_sl - current_close) / current_close
            # 結構高點在現價之下(無效) 或 止損過近(<MIN_SL) → 跳過
            if calculated_sl <= current_close or risk_pct < MIN_SL_PCT:
                if _dbg: print(f"[SL] {symbol_item} 空 止損無效/過近({risk_pct:.3%}<{MIN_SL_PCT:.1%})→跳過", flush=True)
                return
            if risk_pct > MAX_SL:
                calculated_sl = current_close * (1.0 + MAX_SL)
                risk_pct = MAX_SL
            is_swing   = self._get_4h_swing_flag(okx_swap_symbol, df, tf_id)
            tp2_mult   = p["tp2_swing_mult"] if is_swing else p["tp2_intraday_mult"]
            risk_dist  = calculated_sl - current_close
            tp1_target = current_close - risk_dist * p["tp1_mult"]
            tp2_target = current_close - risk_dist * tp2_mult

        # 箱突破空：止損改用「整個箱頂 bh」(SNR 結構止損),非局部 swing high(_find_pivot_high)。
        # 回測(含費WF):swing high -0.089 → 箱頂止損 +0.137,勝率22%→41%,MDD 99%→48%(配killzone再到26%)。
        # 箱頂與回測 box_sigs 的 hi[i-96:i].max() 對齊(df 倒數96根不含當根)。出場維持 4R。
        if direction == "short" and is_box_short:
            _bh_box = float(df["high"].values[-97:-1].max())
            calculated_sl = round(_bh_box + _get_tick_size(df), 8)
            risk_pct = (calculated_sl - current_close) / current_close
            if risk_pct < MIN_SL_PCT or risk_pct > MAX_SL:
                if _dbg: print(f"[Box-SL] {symbol_item} 箱頂止損超範圍({risk_pct:.3%})→跳過", flush=True)
                return
            risk_dist  = calculated_sl - current_close
            tp1_target = current_close - risk_dist * 4.0
            tp2_target = current_close - risk_dist * 4.0

        # 維加斯空：止損放「大通道上緣 e576/676 + 0.3ATR」(對齊回測),固定R出場(TP1.0/TP2.5)。
        if direction == "short" and is_vegas_short and not is_box_short:
            _bu_v = float(max(ema576.iloc[-1], ema676.iloc[-1]))
            calculated_sl = round(_bu_v + 0.3 * current_atr, 8)
            risk_pct = (calculated_sl - current_close) / current_close
            if risk_pct < MIN_SL_PCT or risk_pct > MAX_SL:
                if _dbg: print(f"[Vegas-SL] {symbol_item} 大通道止損超範圍({risk_pct:.3%})→跳過", flush=True)
                return
            risk_dist  = calculated_sl - current_close
            tp1_target = current_close - risk_dist * p["tp1_mult"]          # 固定R 1.0
            tp2_target = current_close - risk_dist * p["tp2_intraday_mult"] # 2.5

        # OI壓縮突破:止損放「12h range 對邊 ± 0.3ATR」(對齊回測)。讓跑出場(swing_full)不掛固定TP。
        if is_oisq_long or is_oisq_short:
            _rh_sq = float(df["high"].values[-13:-1].max()); _rl_sq = float(df["low"].values[-13:-1].min())
            if is_oisq_long:  calculated_sl = round(_rl_sq - 0.3 * current_atr, 8)
            else:             calculated_sl = round(_rh_sq + 0.3 * current_atr, 8)
            risk_pct = abs(calculated_sl - current_close) / current_close
            if risk_pct < MIN_SL_PCT or risk_pct > MAX_SL:
                if _dbg: print(f"[OISq-SL] {symbol_item} range止損超範圍({risk_pct:.3%})→跳過", flush=True)
                return
            risk_dist  = abs(current_close - calculated_sl)
            tp1_target = current_close + (risk_dist if is_oisq_long else -risk_dist) * 1.5  # 讓跑不掛固定TP,此值僅供顯示
            tp2_target = current_close + (risk_dist if is_oisq_long else -risk_dist) * 3.0

        # ★山寨讓跑改半倉2.5R落袋(2026-06-15,COAI教訓:山寨噴到頂用swing_full一路抱會吐回)。
        #   市值幣維持讓跑(不會這樣噴崩);山寨(非MAJOR)讓跑類→swing_tp 半倉2.5R落袋+BE+剩半trail。多空通用。
        if symbol_item not in MAJOR_COINS and exit_strategy in ("swing_full", "line_full"):
            exit_strategy = "swing_tp"
            _rd = abs(current_close - calculated_sl)
            if direction == "long":
                tp1_target = current_close + _rd * ALT_LOCK_R
                tp2_target = current_close + _rd * (ALT_LOCK_R + 2.0)
            else:
                tp1_target = current_close - _rd * ALT_LOCK_R
                tp2_target = current_close - _rd * (ALT_LOCK_R + 2.0)

        risk_delta = abs(current_close - calculated_sl) or 1e-9
        rr1 = abs(tp1_target - current_close) / risk_delta
        rr2 = abs(tp2_target - current_close) / risk_delta

        signal_payload = {
            "side": direction, "entry": current_close, "sl": round(calculated_sl, 5),
            "tp1": round(tp1_target, 5), "tp2": round(tp2_target, 5), "atr": round(current_atr, 4),
            "risk_pct": risk_pct * 100.0, "rr1": rr1, "rr2": rr2, "is_swing": is_swing,
            "exit_mode": p["exit_mode"], "time": datetime.now(timezone.utc).isoformat(),
            "source_tag": signal_source_tag, "dh_boost": dh_boost,
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

        # 金字塔資格：僅驗證過的多單(C3 15m/30m、1H W底)。排除15m雙底共振(n小且加碼變差)、MACD。
        # 趨勢跟蹤出場(切線/移SL/轉折加碼)不走金字塔:整倉/移SL/已自帶N型加碼,金字塔會衝突。
        _pyr_elig = (direction == "long" and ("C3" in _signal_source or "雙底" in _signal_source)
                     and exit_strategy not in ("line_full", "line_add", "swing_full",
                                               "swing_tp", "swing_tp_1h"))

        if AUTO_TRADE.get(tf_id):
            try:
                daily_report.record_entry(symbol_item, tf_id, direction, signal_source_tag or exit_strategy)
            except Exception:
                pass
            if EXCHANGE_ENABLED.get("okx", True):
                if _dir_skew_block(direction, "okx"):
                    dc_log(f"⚖️ 方向平衡:OKX {direction} 已過度集中,跳過OKX這單(防一面倒)")
                else:
                    execute_okx_trade_pipeline(
                        okx_swap_symbol, direction, current_close,
                        signal_payload["sl"], signal_payload["tp1"], signal_payload["tp2"],
                        p["exit_mode"], tf_id,
                        position_scale=dh_boost * _concentration_mult(direction, "okx"),
                        pyramid_eligible=_pyr_elig,
                        exit_strategy=exit_strategy,
                    )
            if EXCHANGE_ENABLED.get("bingx", True):
                if _dir_skew_block(direction, "bingx"):
                    dc_log(f"⚖️ 方向平衡:BingX {direction} 已過度集中,跳過BingX這單(防一面倒)")
                else:
                    execute_bingx_trade_pipeline(
                        symbol_item, direction, current_close,
                        signal_payload["sl"], signal_payload["tp1"], signal_payload["tp2"],
                        p["exit_mode"], tf_id,
                        position_scale=dh_boost * _concentration_mult(direction, "bingx"),
                        exit_strategy=exit_strategy,
                    )

            # ── 高頻固定1R 平行層(各跑各的,觸發就都開):現役3格訊號成立→多開一筆hf_1r獨立倉 ──
            #   1H C3空(is_short) / 1H MACD空(is_macd_short) / 15m MACD多(is_macd_long)。
            #   固定1R全平 + 0.5R保本 + 不讓跑(高頻快累積本金)。與讓跑倉同訊號各開一筆,不去重。
            #   只留 1H(C3空/MACD空,全市值);15m/30m MACD多空由獨立偵測區塊處理(見scan早段hf區塊)。
            _hf_cell = (tf_id == "1H" and direction == "short" and (is_short or is_macd_short))
            if HF_1R_ENABLED and _hf_cell:
                _hf_sl = signal_payload["sl"]
                _hf_risk = abs(current_close - _hf_sl)
                if _hf_risk > 0:
                    _hf_tp = round(current_close + _hf_risk, 6) if direction == "long" \
                             else round(current_close - _hf_risk, 6)
                    try:
                        if EXCHANGE_ENABLED.get("okx", True):
                            execute_okx_trade_pipeline(
                                okx_swap_symbol, direction, current_close,
                                _hf_sl, _hf_tp, _hf_tp, p["exit_mode"], tf_id,
                                position_scale=1.0, pyramid_eligible=False, exit_strategy="hf_1r",
                                allow_stack=True)   # 同訊號平行倉=刻意雙倉,豁免同向去重
                        if EXCHANGE_ENABLED.get("bingx", True):
                            execute_bingx_trade_pipeline(
                                symbol_item, direction, current_close,
                                _hf_sl, _hf_tp, _hf_tp, p["exit_mode"], tf_id,
                                position_scale=1.0, exit_strategy="hf_1r",
                                allow_stack=True)
                        dc_log(f"⚡ 高頻固定1R平行倉:{symbol_item} {tf_id} {direction} TP@1R=`{_hf_tp}`")
                    except Exception as _hfe:
                        print(f"[HF-1R] {symbol_item} 平行倉失敗: {_hfe}")
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

_CONA_FUT_MAP: Dict[str, list] = {}
def _cona_future_map() -> Dict[str, list]:
    """Coinalyze 幣→各所USDT永續symbols(future-markets建一次,快取整session)。"""
    global _CONA_FUT_MAP
    if _CONA_FUT_MAP:
        return _CONA_FUT_MAP
    try:
        mkts = _execute_coinalyze_request("future-markets", {})
        m: Dict[str, list] = {}
        for x in mkts:
            if (x.get("quote_asset") == "USDT" and x.get("is_perpetual") and x.get("has_ohlcv_data")):
                m.setdefault(x.get("base_asset"), []).append(x.get("symbol"))
        if m:
            _CONA_FUT_MAP = m
    except Exception:
        pass
    return _CONA_FUT_MAP

def _cona_agg(coin: str, cona_int: str):
    """跨所聚合 OI+CVD 方向(Coinalyze,bot自己key,聚合~9所)。回 (oi_up, oi_pct, cvd_up, n_ex)。"""
    syms = _cona_future_map().get(coin, [])[:12]
    if not syms:
        return None, 0.0, None, 0
    sstr = ",".join(syms); end = int(time.time()); start = end - 3600 * 24
    oi_up = None; oi_pct = 0.0; cvd_up = None
    try:
        oid = _execute_coinalyze_request("open-interest-history", {"symbols": sstr, "interval": cona_int, "from": start, "to": end})
        agg = {}
        for grp in oid:
            for h in grp.get("history", []):
                agg[h["t"]] = agg.get(h["t"], 0.0) + float(h["c"])   # 各所OI收盤加總
        ts = sorted(agg)
        if len(ts) >= 2:
            k = min(6, len(ts) - 1); oi_now = agg[ts[-1]]; oi_then = agg[ts[-1 - k]]
            if oi_then > 0: oi_up = oi_now > oi_then; oi_pct = (oi_now / oi_then - 1) * 100
    except Exception: pass
    try:
        ohl = _execute_coinalyze_request("ohlcv-history", {"symbols": sstr, "interval": cona_int, "from": start, "to": end})
        agg = {}
        for grp in ohl:
            for h in grp.get("history", []):
                agg[h["t"]] = agg.get(h["t"], 0.0) + (2 * float(h.get("bv", 0)) - float(h.get("v", 0)))  # 各所(買-賣)加總
        ts = sorted(agg)
        if len(ts) >= 2:
            k = min(6, len(ts)); cvd_up = sum(agg[t] for t in ts[-k:]) > 0   # 近k根聚合淨delta
    except Exception: pass
    return oi_up, oi_pct, cvd_up, len(syms)

def _quick_rank_score(inst_id, tf="1H", btc_chg=0.0):
    """輕量評分(只抓K線,不打OI/CVD API,供 !top 快速排名)。回 (norm, 象限標籤, price) 或 None。
    評分=動能(chg1/chg24/相對BTC)+量價CVD代理象限;近似 judge_coin 但不含真OI(快)。"""
    try:
        df = fetch_market_candles(inst_id, tf, 60)
        if df is None or len(df) < 30: return None
        cl = df["close"].values; price = float(cl[-1])
        chg1 = (cl[-1]/cl[-2]-1)*100 if len(cl) >= 2 else 0.0
        n24 = min(24, len(cl)-1); chg24 = (cl[-1]/cl[-1-n24]-1)*100
        rs = chg24 - btc_chg
        hi = df["high"].values; lo = df["low"].values; vol = df["vol"].values
        den = np.where(hi == lo, 1.0, hi-lo); bpos = np.where(hi == lo, 0.0, (cl-lo)/den*2-1)
        cp = np.cumsum(bpos*vol); kk = min(6, len(cp)-1); cvd_up = bool(cp[-1] > cp[-1-kk])
        s = max(-8.0, min(8.0, chg1/1.2)) + max(-5.0, min(5.0, chg24/6.0)) + max(-8.0, min(8.0, rs/3.5))
        if   cvd_up and chg1 > 0:   s += 12; lab = "多頭建倉"
        elif (not cvd_up) and chg1 < 0: s -= 12; lab = "空頭建倉"
        elif cvd_up and chg1 <= 0:  s += 4;  lab = "回補弱多"
        else:                       s -= 4;  lab = "出場弱空"
        norm = int(max(-10, min(10, round(s/7))))
        return (norm, lab, price)
    except Exception:
        return None

def judge_coin(coin_raw, side_hint=None, brief=False, tf="1H"):
    """裸打「幣」或「幣 多/空 [時框]」→ 仿數據獵手:市場結構象限(OI×CVD)+評分(±10)+方向轉折+適合多/空+建議SL/TP。
    用 bot 自己的 OI/CVD/funding/價格資料,即時、唯讀、不下單、零外部訊號源。
    支援時框 5m/15m/30m/1H/4H(預設1H)。brief=True 回精簡一行(訊號卡掛載用,不含SL/TP)。"""
    try:
        coin = coin_raw.strip().upper()
        inst_id = f"{coin}-USDT-SWAP"
        if inst_id not in SYMBOLS:
            return None
        symbol_item = SYMBOLS[inst_id]
        tfmap = {"5":"5m","5m":"5m","15":"15m","15m":"15m","30":"30m","30m":"30m",
                 "1h":"1H","60":"1H","1":"1H","2h":"2H","4h":"4H","4":"4H"}
        tf = tfmap.get(str(tf).strip().lower(), "1H")
        tf_min = {"5m":5,"15m":15,"30m":30,"1H":60,"2H":120,"4H":240}[tf]
        bars24 = max(2, round(24*60 / tf_min))
        df = fetch_market_candles(inst_id, tf, min(300, bars24 + 8))
        if df is None or len(df) < min(bars24 + 2, 25):
            return f"⚠️ {coin} {tf} 資料不足"
        cl = df["close"]; price = float(cl.iloc[-1]); b24 = min(bars24, len(cl) - 1)
        chg1  = (cl.iloc[-1] / cl.iloc[-2]     - 1) * 100
        chg24 = (cl.iloc[-1] / cl.iloc[-1-b24] - 1) * 100
        try:
            btc = fetch_market_candles("BTC-USDT-SWAP", tf, min(300, bars24 + 8))["close"]
            btc24 = (btc.iloc[-1] / btc.iloc[-1-min(b24, len(btc)-1)] - 1) * 100
        except Exception:
            btc24 = 0.0
        rs = chg24 - btc24
        # 方向轉折(近3根斜率 vs 前3根)
        flip = ""
        if len(cl) >= 5:
            r_now = float(cl.iloc[-1] - cl.iloc[-3]); r_prev = float(cl.iloc[-3] - cl.iloc[-5])
            if r_prev < 0 and r_now > 0:   flip = " 🔄剛轉多"
            elif r_prev > 0 and r_now < 0: flip = " 🔄剛轉空"
        # OI+CVD:① Coinalyze 跨所聚合(~9所,bot自己key) → ② OKX真OI/OKX+幣安taker → ③ 量代理
        oi_up = None; oi_pct = 0.0; oi_src = "無源"; cvd_up = None; cvd_src = "—"
        try:
            _ai, _ap, _ac, _nex = _cona_agg(coin, BAR_TO_CONA.get(tf, "1hour"))
            if _ai is not None: oi_up = _ai; oi_pct = _ap; oi_src = f"聚合{_nex}所"
            if _ac is not None: cvd_up = _ac; cvd_src = f"聚合{_nex}所"
        except Exception: pass
        if oi_up is None:   # OI 退路:OKX rubik
            try:
                _oid = _fetch_okx_public_data("/api/v5/rubik/stat/contracts/open-interest-volume",
                                              {"ccy": coin, "period": ("5m" if tf == "5m" else "1H")})
                if _oid and len(_oid) >= 2:
                    _k = min(6, len(_oid) - 1); _now = float(_oid[0][1]); _then = float(_oid[_k][1])
                    if _then > 0: oi_up = bool(_now > _then); oi_pct = (_now / _then - 1) * 100; oi_src = "OKX"
            except Exception: pass
        if cvd_up is None:   # CVD 退路:OKX+幣安 taker
            _votes = []
            try:
                _tkr = _fetch_okx_public_data("/api/v5/rubik/stat/taker-volume",
                                              {"ccy": coin, "instType": "CONTRACTS", "period": ("5m" if tf == "5m" else "1H")})
                if _tkr and len(_tkr) >= 3:
                    _k = min(6, len(_tkr)); _votes.append(1 if sum(float(r[2]) - float(r[1]) for r in _tkr[:_k]) > 0 else -1)
            except Exception: pass
            try:
                _bp = tf.lower() if tf.lower() in ("5m","15m","30m","1h","2h","4h") else "1h"
                _ls, _tkb = _fetch_binance_ls_taker(symbol_item, _bp)
                if _tkb is not None: _votes.append(1 if _tkb > 1 else -1)
            except Exception: pass
            if _votes and sum(_votes) != 0:
                cvd_up = sum(_votes) > 0; cvd_src = "OKX+幣安"
        if cvd_up is None:   # CVD 最終退路:OHLCV 量代理
            clv = cl.values; hiv = df["high"].values; lov = df["low"].values; volv = df["vol"].values
            _den = np.where(hiv == lov, 1.0, hiv - lov); bpos = np.where(hiv == lov, 0.0, (clv - lov) / _den * 2 - 1)
            _cp = np.cumsum(bpos * volv); _kk = min(6, len(_cp) - 1)
            cvd_up = bool(_cp[-1] > _cp[-1 - _kk]) if len(_cp) > _kk else None
            cvd_src = "量代理"
        try: fr = fetch_current_funding_rate(inst_id) or 0.0
        except Exception: fr = 0.0
        # 市場結構象限:有真OI用OI×CVD(主力建倉,2026-06-23逆推,C3空L2確認已驗證用這套);無真OI退「價格動能×CVD」近似(標OI估)
        struct_label = "（資料不足）"; struct_score = 0
        _oi_dir = oi_up if oi_up is not None else (chg1 > 0)
        if cvd_up is not None:
            if   _oi_dir and cvd_up:         struct_label = "🟢多頭建倉(主動做多)"; struct_score =  24
            elif _oi_dir and not cvd_up:     struct_label = "🔴空頭建倉(主動做空)"; struct_score = -24
            elif (not _oi_dir) and cvd_up:   struct_label = "🟢空頭平倉(回補,弱多)"; struct_score =   8
            else:                            struct_label = "🔴多頭平倉(出場,弱空)"; struct_score =  -8
            if oi_up is None:                struct_label += "·OI估"
        # ── v2 OI結構(OI×價格,2026-07-01anomaly卡逆推,C3空L1確認已上線用這套)──
        # 跟上面OI×CVD是「不同象限系統」,不互相取代:上面是籌碼×資金流向,這裡是籌碼×價格。並列顯示不混用。
        oiv2_label = None; oiv2_score = 0
        if oi_up is not None and abs(oi_pct) > 0:
            _strong = abs(oi_pct) >= 3.0
            _ramp = max(0.0, min(1.0, (abs(oi_pct) - 3.0) / 10.0))
            _price_up = chg1 > 0
            if oi_up and _price_up:        oiv2_label = "OI↑價↑(主動做多)"; oiv2_score = (24+_ramp*16) if _strong else 12
            elif oi_up and not _price_up:  oiv2_label = "OI↑價↓(主動做空)"; oiv2_score = -((24+_ramp*16) if _strong else 12)
            elif (not oi_up) and _price_up:oiv2_label = "OI↓價↑(空頭出場)"; oiv2_score = (8+_ramp*8) if _strong else 4
            else:                          oiv2_label = "OI↓價↓(多頭出場)"; oiv2_score = -((8+_ramp*8) if _strong else 4)
        # 評分(仿數據獵手,正規化~±10)
        s  = struct_score
        s += max(-8.0, min(8.0, chg1 / 1.2))
        s += max(-5.0, min(5.0, chg24 / 6.0))
        s += max(-8.0, min(8.0, rs / 3.5))
        s += max(-4.0, min(4.0, -fr * 1000))
        norm = int(max(-10, min(10, round(s / 7))))
        verdict = "🟢 適合做多" if norm >= 5 else "🔴 適合做空" if norm <= -5 else "🟡 中性觀望"
        align = ""
        if side_hint:
            w = "long" if side_hint in ("多","long","l","做多") else "short" if side_hint in ("空","short","s","做空") else None
            if w == "long":  align = " ✅順" if norm >= 5 else " ⚠️逆籌碼,別追" if norm <= -3 else " ➖訊號弱"
            if w == "short": align = " ✅順" if norm <= -5 else " ⚠️逆籌碼,別追" if norm >=  3 else " ➖訊號弱"
        _oiv2_txt = f" · v2:{oiv2_label}({oiv2_score:+.0f})" if oiv2_label else ""
        if brief:
            return f"{struct_label} · 評分 `{norm:+d}/10`{align}{flip}{_oiv2_txt}".strip()
        cvd_txt = "升" if cvd_up else ("降" if cvd_up is not None else "?")
        # ATR(14,1H) + 近20根擺動高低 → 建議停損停利(SL=結構或至少1ATR;TP=2~3ATR)
        hi = df["high"].values; lo = df["low"].values; clv = cl.values
        _tr = np.maximum(hi[1:] - lo[1:], np.maximum(np.abs(hi[1:] - clv[:-1]), np.abs(lo[1:] - clv[:-1])))
        atr = float(pd.Series(_tr).ewm(alpha=1/14, adjust=False).mean().iloc[-1]) if len(_tr) else 0.0
        sw_lo = float(lo[-20:].min()); sw_hi = float(hi[-20:].max())
        d = None
        if side_hint:
            d = "long" if side_hint in ("多","long","l","做多") else "short" if side_hint in ("空","short","s","做空") else None
        if d is None:
            d = "long" if norm >= 5 else "short" if norm <= -5 else ("long" if (norm > 0 or chg1 > 0) else "short")
        plan = ""
        if d and atr > 0:
            if d == "long":
                sl = min(sw_lo - 0.3*atr, price - atr); r = price - sl
                tp1 = price + 2*atr; tp2 = price + 3*atr
            else:
                sl = max(sw_hi + 0.3*atr, price + atr); r = sl - price
                tp1 = price - 2*atr; tp2 = price - 3*atr
            rr1 = (abs(tp1 - price) / r) if r > 0 else 0
            plan = (f"\n📐 建議({'多' if d=='long' else '空'}): 進場 `${price:,.6g}`  停損 `${sl:,.6g}` "
                    f"(`{abs(price-sl)/price*100:.1f}%` / {r/atr:.1f}ATR)\n"
                    f"　TP1 `${tp1:,.6g}` (2ATR · RR{rr1:.1f})　TP2 `${tp2:,.6g}` (3ATR)")
        # ── 進場5點檢查:把分數變成可執行的進場判斷(位置/觸發/停損/賺賠/regime)──
        chk = ""
        if d and atr > 0:
            _cl = cl.values; _op = df["open"].values
            _e50 = pd.Series(_cl).ewm(span=50, adjust=False).mean().values
            _pts = []
            if d == "long":
                _pts.append(("位置近支撐", (price - sw_lo) <= 2.0*atr))
                _pts.append(("觸發(收紅創高)", _cl[-1] > _op[-1] and _cl[-1] > _cl[-2]))
            else:
                _pts.append(("位置近壓力", (sw_hi - price) <= 2.0*atr))
                _pts.append(("觸發(收黑破低)", _cl[-1] < _op[-1] and _cl[-1] < _cl[-2]))
            _pts.append(("停損合理(0.6-12%)", 0.006 <= abs(price-sl)/price <= 0.12))
            _pts.append(("賺賠比≥1.8", rr1 >= 1.8))
            _pts.append(("順勢regime", (price > _e50[-1]) if d == "long" else (price < _e50[-1])))
            _npass = sum(1 for _, ok in _pts if ok)
            _vd = "✅ 可考慮進場" if _npass >= 4 else ("⚠️ 再等訊號" if _npass == 3 else "❌ 別碰")
            _dlab = "🟢做多" if d == "long" else "🔴做空"
            chk = f"\n🎯 **{_dlab}進場檢查 {_npass}/5 → {_vd}**  _(查另一方向打 `{coin} 多` 或 `{coin} 空`)_\n　" + "　".join(f"{'✅' if ok else '❌'}{nm}" for nm, ok in _pts)
            if d == "long":
                chk += "\n　_(山寨多 edge 薄:務必小注+嚴守停損,5點少一個就放掉)_"
        _oiv2_line = f"\nv2結構(OI×價格): {oiv2_label} `{oiv2_score:+.0f}`" if oiv2_label else ""
        return (f"📊 **{coin}** ${price:,.6g}  {verdict}  **{norm:+d}/10**{align}{flip}  _({tf} 級別)_\n"
                f"市場結構: {struct_label}  (OI {oi_pct:+.1f}%[{oi_src}] / CVD {cvd_txt}[{cvd_src}]){_oiv2_line}\n"
                f"動能 {tf} `{chg1:+.1f}%`  24H `{chg24:+.1f}%`  相對強弱vsBTC `{rs:+.1f}%`  資費 `{fr*100:+.3f}%`"
                f"{plan}{chk}")
    except Exception as e:
        return f"⚠️ 判斷失敗: {e}"


def poll_dc_commands():
    """ 輪詢 Discord 頻道訊息，處理 ! / / 指令 """
    global _PAUSED, _LIVE_MODE, _dc_last_msg_id, POSITION_SLOTS, RISK_PCT, LADDER_STEP_USDT, LADDER_BASE_USDT, OKX_MIN_MMR, BINGX_MAX_RISK_RATE, DAILY_LOSS_PCT, DAILY_STOP_ENABLED, MAX_DIR_SKEW, DIR_BALANCE_ENABLED, CONC_FREE, CONC_RISK_ENABLED
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
                        if is_bot:
                            continue
                        if not (content.startswith("!") or content.startswith("/")):
                            # 裸打「幣」或「幣 多/空 [時框]」→ 順籌碼即時判斷,只認已知幣防誤觸
                            _w = content.split()
                            if 1 <= len(_w) <= 3 and _w[0].isascii() and _w[0].isalpha() \
                               and f"{_w[0].upper()}-USDT-SWAP" in SYMBOLS:
                                _side = None; _tf = "1H"
                                for _t in _w[1:]:
                                    _tl = _t.lower()
                                    if _tl in ("多","空","long","short","l","s","做多","做空"): _side = _t
                                    elif _tl in ("5m","15m","30m","1h","4h","2h","5","15","30","60","4"): _tf = _t
                                try:
                                    _res = judge_coin(_w[0], _side, tf=_tf)
                                    dc_log(_res if _res else f"⚠️ {_w[0].upper()} 無判斷結果(可能非SYMBOLS幣)")
                                except Exception as _je:
                                    dc_log(f"⚠️ 判斷錯誤: {type(_je).__name__}: {_je}")
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
                                "__即時判斷__\n"
                                "**`幣` / `幣 空/多 [時框]`** - 順籌碼即時判斷(象限+評分±10+方向轉折+SL/TP)，如 `ADA`、`ADA 空 15m`(時框5m/15m/30m/1H/4H，預設1H)\n"
                                "**`!top` / `!top 15m`** - 掃全幣，列當前最適合做多/做空各前3名(評分排序)\n"
                                "__模式 / 掃描__\n"
                                "`!status` 系統狀態 · `!setlive`/`!setpaper` 實盤/模擬 · `!pause`/`!resume` 暫停/恢復掃描\n"
                                "__風控__\n"
                                "`!risk [%]` 每倉風險%（如 `!risk 5`）\n"
                                "`!dailystop [%]` 當日虧此%停開新倉至隔日UTC（`!dailystop 0`=關閉立即恢復；既有倉照管）\n"
                                "`!dirskew [n]` 方向平衡：主導方向多 n 倉就擋該向新倉（0=關）\n"
                                "`!concrisk [n]` 風險預算：同向超 n 倉後新倉風險遞減（0=關）\n"
                                "`!setladder [U]` 分段複利：每多賺 U 升一級風險\n"
                                "`!setmmr [%]` OKX 維持保證金率門檻，低於不開新倉\n"
                                "`!setbingxrisk [%]` BingX 帳戶風險率上限，超過不開新倉\n"
                                "__過濾 / 開關__\n"
                                "`/cvd on|off` · `/adx on|off` · `/trade [15m|30m|1h|4h|all] on|off` · `/margin isolated|cross` · `/exchange okx|bingx on|off`\n"
                            )

                        # ── top：掃全幣評分，列做多/做空各前3名 ──────────────
                        elif cmd == "top":
                            _ttf = "1H"
                            for _t in parts[1:]:
                                if _t in ("5m","15m","30m","1h","4h","2h"): _ttf = _t
                            dc_log(f"⏳ 掃描評分中({_ttf})…約20-40秒")
                            try:
                                _bdf = fetch_market_candles("BTC-USDT-SWAP", _ttf, 60)
                                _bc = ((_bdf["close"].values[-1]/_bdf["close"].values[-min(24,len(_bdf)-1)]-1)*100) if (_bdf is not None and len(_bdf) > 24) else 0.0
                                _rows = []
                                for _sk in list(SYMBOLS.keys())[:60]:
                                    _qr = _quick_rank_score(_sk, _ttf, _bc)
                                    if _qr: _rows.append((_sk.replace("-USDT-SWAP",""), _qr[0], _qr[1], _qr[2]))
                                    time.sleep(0.04)
                                if not _rows:
                                    dc_log("⚠️ top:無資料")
                                else:
                                    _lg = sorted(_rows, key=lambda x: -x[1])[:3]
                                    _sh = sorted(_rows, key=lambda x:  x[1])[:3]
                                    _msg = f"🏆 **當前評分排名** ({_ttf}，掃 {len(_rows)} 幣)\n🟢 **適合做多 前3**\n"
                                    for c,n,l,p in _lg: _msg += f"　`{n:+d}/10` **{c}** {l} ${p:,.6g}\n"
                                    _msg += "🔴 **適合做空 前3**\n"
                                    for c,n,l,p in _sh: _msg += f"　`{n:+d}/10` **{c}** {l} ${p:,.6g}\n"
                                    _msg += "_(輕量動能評分排名；個別幣詳細順籌碼+SL/TP 請打 `幣`，如 `ADA`)_"
                                    dc_log(_msg)
                            except Exception as _te:
                                dc_log(f"⚠️ top 掃描錯誤: {type(_te).__name__}: {_te}")

                        # ── 幣順籌碼判斷（!幣 / /幣 也可，如 !ADA 空 15m）────
                        elif f"{cmd.upper()}-USDT-SWAP" in SYMBOLS:
                            _side = None; _tf = "1H"
                            for _t in parts[1:]:
                                if _t in ("多","空","long","short","l","s","做多","做空"): _side = _t
                                elif _t in ("5m","15m","30m","1h","4h","2h","5","15","30","60","4"): _tf = _t
                            try:
                                _res = judge_coin(cmd, _side, tf=_tf)
                                dc_log(_res if _res else f"⚠️ {cmd.upper()} 無判斷結果")
                            except Exception as _je:
                                dc_log(f"⚠️ 判斷錯誤: {type(_je).__name__}: {_je}")

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

                        # ── dailystop：每日虧損熔斷%（虧到此%停新倉到隔日UTC；0=關閉）──
                        elif cmd == "dailystop":
                            if len(parts) >= 2 and parts[1].replace("%", "").replace(".", "").isdigit():
                                v = float(parts[1].replace("%", ""))
                                if v <= 0:
                                    DAILY_STOP_ENABLED = False
                                    dc_log("⚙️ 每日虧損熔斷已**關閉**")
                                else:
                                    DAILY_STOP_ENABLED = True
                                    DAILY_LOSS_PCT = round(v / 100, 4) if v > 1 else round(v, 4)
                                    _daily_stop["active"] = False
                                    dc_log(f"⚙️ 每日虧損熔斷: 當日虧到 `-{DAILY_LOSS_PCT*100:.0f}%` 停開新倉(隔日UTC解,既有倉照管)")
                            else:
                                dc_log("⚠️ 用法: `!dailystop 20`(虧20%停) / `!dailystop 0`(關閉)")

                        # ── dirskew：方向平衡偏斜上限(主導方向比另一方多N倉就擋;0=關)──
                        elif cmd == "dirskew":
                            if len(parts) >= 2 and parts[1].replace(".", "").isdigit():
                                v = int(float(parts[1]))
                                if v <= 0:
                                    DIR_BALANCE_ENABLED = False
                                    dc_log("⚙️ 方向平衡已**關閉**(可全做空/全做多)")
                                else:
                                    DIR_BALANCE_ENABLED = True; MAX_DIR_SKEW = v
                                    dc_log(f"⚙️ 方向平衡: 主導方向比另一方多 `{v}` 倉(兩所合計)就擋該方向新倉")
                            else:
                                dc_log("⚠️ 用法: `!dirskew 12`(偏斜上限) / `!dirskew 0`(關閉)")

                        # ── concrisk：風險預算(同向倉超N個後新倉風險遞減;0=關)──
                        elif cmd == "concrisk":
                            if len(parts) >= 2 and parts[1].replace(".", "").isdigit():
                                v = int(float(parts[1]))
                                if v <= 0:
                                    CONC_RISK_ENABLED = False
                                    dc_log("⚙️ 風險預算已**關閉**(同向倉不遞減風險)")
                                else:
                                    CONC_RISK_ENABLED = True; CONC_FREE = v
                                    dc_log(f"⚙️ 風險預算: 每所同向超過 `{v}` 倉後,新倉風險×({v}/倉數)遞減(總同向曝險≈{v}R)")
                            else:
                                dc_log("⚠️ 用法: `!concrisk 6`(全倉名額) / `!concrisk 0`(關閉)")

                        # ── setladder：分段複利級距（每多賺 N U 才把單筆風險加一級）──
                        elif cmd == "setladder":
                            if len(parts) >= 2 and parts[1].replace(".", "").isdigit():
                                LADDER_STEP_USDT = float(parts[1])
                                base_unit = LADDER_BASE_USDT * RISK_PCT
                                dc_log(f"⚙️ 分段複利級距已更新：每多賺 `{LADDER_STEP_USDT:.0f}U` 升一級\n"
                                       f"   初始單筆風險 `{base_unit:.2f}U`（基準{LADDER_BASE_USDT:.0f}U × {RISK_PCT*100:.0f}%）\n"
                                       f"   越小越接近純複利(高成長高MDD)、越大越接近固定(穩但慢)")
                            else:
                                dc_log("⚠️ 用法: `!setladder 50`（每多賺50U才把單筆風險加一級）")

                        # ── setmmr：OKX 維持保證金率門檻(%)，預估加新倉後低於此值就跳過 ──
                        elif cmd == "setmmr":
                            if len(parts) >= 2 and parts[1].replace(".", "").isdigit():
                                OKX_MIN_MMR = float(parts[1])
                                dc_log(f"⚙️ OKX 維持保證金率門檻已更新：`{OKX_MIN_MMR:.0f}%`\n"
                                       f"   開倉前預估加新倉後維持率 < {OKX_MIN_MMR:.0f}% 就跳過。\n"
                                       f"   越高越保守(留多餘保證金)、越低越積極(易爆倉風險升)")
                            else:
                                dc_log("⚠️ 用法: `!setmmr 350`（OKX 維持保證金率門檻%，低於此值不開新倉）")

                        # ── setbingxrisk：BingX 帳戶風險率上限，預估加新倉後超過就跳過 ──
                        elif cmd == "setbingxrisk":
                            if len(parts) >= 2 and parts[1].replace(".", "").isdigit():
                                val = float(parts[1])
                                if 0 < val <= 100:
                                    BINGX_MAX_RISK_RATE = val / 100.0 if val > 1 else val
                                    dc_log(f"⚙️ BingX 風險率上限已更新：`{BINGX_MAX_RISK_RATE:.0%}`\n"
                                           f"   開倉前預估加新倉後風險率 > {BINGX_MAX_RISK_RATE:.0%} 就跳過。\n"
                                           f"   越低越保守（預設70%；BingX 另有逐筆強平守門員與OKX對齊）")
                                else:
                                    dc_log("⚠️ 數值需在 1~100 之間（如 `!setbingxrisk 60`=60%）")
                            else:
                                dc_log("⚠️ 用法: `!setbingxrisk 70`（BingX 帳戶風險率上限%，超過就不開新倉）")

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

TOP_MOVERS_N        = 25            # 漲幅前N + 跌幅前N(共~50)加入掃描
MIN_MOVER_VOLCCY    = 10_000_000.0  # 流動性門檻:24h成交額(volCcy24h)需 ≥ 此值,濾掉插針山寨
def _fetch_okx_top_movers(top_n: int = TOP_MOVERS_N, min_volccy: float = MIN_MOVER_VOLCCY) -> list:
    """OKX 24h 漲幅前N + 跌幅前N(USDT永續,配流動性門檻)。
    漲幅榜→動量/突破多單廣度;跌幅榜→breakdown空/box 候選。回傳 inst_id 列表。"""
    try:
        r = requests.get("https://www.okx.com/api/v5/market/tickers",
                         params={"instType": "SWAP"}, timeout=15)
        if r.status_code != 200:
            print(f"[SYMBOLS] OKX tickers HTTP {r.status_code}", flush=True); return []
        movers = []
        for t in r.json().get("data", []):
            inst = t.get("instId", "")
            if not inst.endswith("-USDT-SWAP"):
                continue
            try:
                last = float(t["last"]); op = float(t["open24h"]); vc = float(t.get("volCcy24h", 0) or 0)
            except (KeyError, ValueError, TypeError):
                continue
            if op <= 0 or vc < min_volccy:        # 流動性門檻
                continue
            movers.append((inst, (last - op) / op))
        if not movers:
            return []
        movers.sort(key=lambda x: x[1], reverse=True)
        gainers = [m[0] for m in movers[:top_n]]
        losers  = [m[0] for m in movers[-top_n:]]
        print(f"[SYMBOLS] OKX 漲跌幅榜:漲{len(gainers)}+跌{len(losers)}(流動性≥{min_volccy/1e6:.0f}M)", flush=True)
        return gainers + losers
    except Exception as e:
        print(f"[SYMBOLS] OKX 漲跌幅榜抓取失敗: {e}", flush=True)
        return []

def build_dynamic_symbols() -> bool:
    """
    重建 SYMBOLS + OKX_SWAP：
      CoinGecko 市值前100（排除穩定幣）× OKX 永續  +  OKX 每日漲跌幅前25(配流動性門檻)
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

    # 加每日漲跌幅榜(配流動性門檻)→ 擴廣度,波動在哪訊號在哪(2026-06-13)
    _n_before = len(new_symbols)
    for inst_id in _fetch_okx_top_movers():
        if inst_id in okx_swaps and inst_id not in new_symbols:
            coin = inst_id.split("-")[0]
            if coin not in STABLECOINS:
                new_symbols[inst_id] = f"{coin}/USDT"
    if len(new_symbols) > _n_before:
        print(f"[SYMBOLS] 漲跌幅榜額外加入 {len(new_symbols)-_n_before} 幣 → 共 {len(new_symbols)}", flush=True)

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

def _okx_fetch_algo_sl(inst_id: str):
    """讀該 instId 第一個 conditional 止損 algo 單，回傳 (algoId, slTriggerPx)；無則 (None,None)。"""
    now=datetime.now(timezone.utc); ts=now.strftime("%Y-%m-%dT%H:%M:%S.")+f"{now.microsecond//1000:03d}Z"
    path=f"/api/v5/trade/orders-algo-pending?ordType=conditional&instId={inst_id}"
    sig=_okx_generate_signature(ts,"GET",path,"")
    headers={"OK-ACCESS-KEY":OKX_API_KEY,"OK-ACCESS-SIGN":sig,"OK-ACCESS-TIMESTAMP":ts,
             "OK-ACCESS-PASSPHRASE":OKX_PASSPHRASE,"Content-Type":"application/json"}
    if OKX_DEMO: headers["x-simulated-trading"]="1"
    try:
        r=requests.get(f"{OKX_BASE}{path}",headers=headers,timeout=10).json()
        for d in (r.get("data") or []):
            t=d.get("slTriggerPx")
            if t and float(t)>0: return d.get("algoId"), float(t)
    except Exception as e:
        print(f"[Adopt] 讀algo失敗 {inst_id}: {e}")
    return None,None


def adopt_untracked_okx_positions():
    """啟動時把未追蹤的 OKX 倉位納入保本追蹤：讀既有止損推算R→達1R自動移保本。
    讀不到止損則只發通知、不亂下單(避免重複止損/亂猜)。採用倉位不做金字塔。"""
    if not _LIVE_MODE: return
    try:
        ex=_initialize_ccxt_client(); ex.load_markets(); positions=ex.fetch_positions()
    except Exception as e:
        print(f"[Adopt] 取持倉失敗: {e}"); return
    tracked={(t.get("symbol"),t.get("direction")) for t in active_real_trades.values()}
    adopted=0
    for p in positions:
        try:
            ct=abs(float(p.get("contracts") or 0))
            if ct<=0: continue
            sym=p.get("symbol"); side=p.get("side")
            if not sym or side not in ("long","short"): continue
            if (sym,side) in tracked: continue
            entry=float(p.get("entryPrice") or 0) or float((p.get("info") or {}).get("avgPx") or 0)
            if entry<=0: continue
            inst_id=(p.get("info") or {}).get("instId") or OKX_SWAP.get(sym, sym)
            sl_id, sl_trig=_okx_fetch_algo_sl(inst_id)
            if not sl_trig:
                dc_log(f"⚠️ 發現未追蹤倉位 {sym} {side}(讀不到止損)→ bot不自動接管，請手動設止損/保本")
                continue
            risk=abs(entry-sl_trig)
            if risk<=0: continue
            # 接管倉統一走 swing_full(整倉,與 BingX 一致)——不補TP。
            # 用戶要:接管動作 = 達1R保本 + N字型移動停利(check_trailing swing_full 分支處理)。
            # 原「有TP→固定R(等TP1才移SL)」改掉,改成主動的整倉移SL。
            tp1_id=None
            inferred_es  = "swing_full"
            inferred_rem = str(ct)
            tkey=f"okx_adopt_{inst_id}_{side}_{int(time.time())}"
            active_real_trades[tkey]={
                "exchange":"okx","inst_id":inst_id,"symbol":sym,"direction":side,
                "entry_price":str(entry),"sl_algo_id":sl_id,"tp1_order_id":tp1_id,
                "tp1_hit":False,"current_sl":sl_trig,"remaining_amount":inferred_rem,
                "pos_side":side,"risk_dist":risk,"tf_id":"adopted",
                "init_contracts":ct,"pyramid_added":True,"pyramid_eligible":False,
                "exit_strategy":inferred_es,
                "entry_ts":int(time.time()) - 24*3600,  # ★往前24h(2026-06-20):redeploy重撿會重設entry_ts,6h只6根K→trail找不到pivot;24h給夠pivot(錯側由合法側檢查擋)
            }
            adopted+=1
            dc_log(f"📥 已接管未追蹤倉位 {sym} {side}(進場{entry}、止損{sl_trig})→ swing_full 達1R保本+N字型移SL")
        except Exception as ie:
            print(f"[Adopt] {p.get('symbol')} 失敗: {ie}")
    # 診斷:dump 每個 OKX 接管倉的 es/sl/tp1,看 swing_full vs 固定R 分布(進Railway logs)
    print(f"[OKX Adopt] 接管 {adopted} 個 OKX 倉位", flush=True)
    for _k,_v in active_real_trades.items():
        if _v.get("exchange")=="okx":
            print(f"[OKX倉] {_v.get('symbol')} {_v.get('direction')} "
                  f"es={_v.get('exit_strategy') or '固定R'} sl={_v.get('current_sl')} "
                  f"tp1={_v.get('tp1_hit')}", flush=True)
    if adopted: save_active_trades()


def adopt_untracked_bingx_positions():
    """啟動時把未追蹤的 BingX 倉位納入保本追蹤（與 OKX adopt 對齊）。
    BingX 沒有 closeFraction，需要記錄具體 qty；SL 用 STOP_MARKET 訂單追蹤。"""
    if not _LIVE_MODE or not BINGX_API_KEY or not BINGX_SECRET_KEY:
        return
    try:
        headers = {"X-BX-APIKEY": BINGX_API_KEY}
        r = _bingx_request("GET", "/openApi/swap/v2/user/positions", {}, headers)
        positions = (r.json().get("data") or [])
    except Exception as e:
        print(f"[BingX Adopt] 取持倉失敗: {e}"); return

    tracked = {(t.get("symbol"), t.get("direction")) for t in active_real_trades.values()
               if t.get("exchange") == "bingx"}
    adopted = 0; skipped_mode = 0
    for p in positions:
        try:
            amt = float(p.get("positionAmt") or 0)
            qty = abs(amt)
            if qty <= 0: continue
            pos_side_raw = p.get("positionSide", "")
            # 支援單向持倉模式(positionSide=BOTH)：方向由 positionAmt 正負判斷，SL掛單仍用BOTH
            if pos_side_raw == "BOTH":
                direction = "long" if amt > 0 else "short"
            elif pos_side_raw in ("LONG", "SHORT"):
                direction = "long" if pos_side_raw == "LONG" else "short"
            else:
                skipped_mode += 1; continue
            bx_sym = p.get("symbol", "")
            if not bx_sym: continue
            ccxt_sym = bx_sym.replace("-", "/")
            if (ccxt_sym, direction) in tracked: continue
            entry = float(p.get("avgPrice") or p.get("entryPrice") or 0)
            if entry <= 0: continue

            # 抓開放訂單，找 SL(STOP_MARKET) 和 TP
            orders_r = _bingx_request("GET", "/openApi/swap/v2/trade/openOrders",
                                      {"symbol": bx_sym}, headers)
            orders = (orders_r.json().get("data") or {})
            if isinstance(orders, dict): orders = orders.get("orders") or []

            sl_order_id = None; sl_trig = None; tp1_order_id = None; has_tp = False
            for o in (orders or []):
                o_type = str(o.get("type", "")).upper()
                o_pos  = o.get("positionSide", "")
                o_stop = float(o.get("stopPrice") or 0)
                o_id   = str(o.get("orderId") or "")
                if o_pos != pos_side_raw: continue
                if o_type in ("STOP_MARKET", "STOP") and o_stop > 0 and not sl_order_id:
                    sl_order_id = o_id; sl_trig = o_stop
                if o_type in ("TAKE_PROFIT_MARKET", "TAKE_PROFIT", "LIMIT") and o_stop > 0:
                    has_tp = True
                    if not tp1_order_id: tp1_order_id = o_id

            if not sl_trig:
                dc_log(f"⚠️ BingX 發現未追蹤倉位 {bx_sym} {direction}(讀不到止損)→ 請手動設止損")
                continue
            risk = abs(entry - sl_trig)
            if risk <= 0: continue

            fee_buf  = entry * 0.001
            be_price = (entry + fee_buf) if direction == "long" else (entry - fee_buf)
            exit_side = "SELL" if direction == "long" else "BUY"
            # 根據有無 TP 推算 exit_strategy 和 remaining_qty（與 OKX adopt 邏輯一致）
            inferred_es  = "swing_full" if not has_tp else ""
            inferred_rem = str(round(qty, 4)) if not has_tp else str(round(qty * 0.5, 4))

            tkey = f"bingx_{bx_sym}_{direction}_{int(time.time())}"
            active_real_trades[tkey] = {
                "exchange":      "bingx",
                "inst_id":       bx_sym,
                "symbol":        ccxt_sym,
                "direction":     direction,
                "entry_price":   str(entry),
                "sl_order_id":   sl_order_id,
                "tp1_order_id":  tp1_order_id,
                "tp1_hit":       False,
                "current_sl":    sl_trig,
                "be_price":      be_price,
                "remaining_qty": inferred_rem,
                "full_qty":      str(round(qty, 4)),
                "pos_side":      pos_side_raw,
                "exit_side":     exit_side,
                "headers":       headers,
                "risk_dist":     risk,
                "tf_id":         "adopted",
                "exit_strategy": inferred_es,
                "entry_ts":      int(time.time()) - 24*3600,  # ★往前24h(2026-06-20):被redeploy重撿時entry_ts會重設,6h只給6根K→pivot湊不齊trail不動(BingX全卡原停損bug根因)。24h給24根K,trail找得到pivot;錯側進場前pivot由合法側檢查擋掉=安全
                "init_qty":      str(round(qty, 4)),
                "add_count":     0,
                "add_swings_n":  0,
            }
            adopted += 1
            _es_lbl = "swing_full整倉追蹤" if inferred_es == "swing_full" else "固定R半倉"
            dc_log(f"📥 BingX 已接管未追蹤倉位 {bx_sym} {direction}(進場{entry}、止損{sl_trig})→ {_es_lbl}")
        except Exception as ie:
            print(f"[BingX Adopt] {p.get('symbol','?')} 失敗: {ie}")
    # 診斷:同時 print(進Railway stdout/Deploy Logs) + dc_log(進Discord)
    # dc_log 成功發Discord時不print,故Railway Deploy Logs只能靠print看到adopt行為。
    _diag = (f"BingX adopt 掃描 {len(positions)} 個持倉 → 接管 {adopted} 個"
             + (f"、跳過模式不符 {skipped_mode} 個" if skipped_mode else ""))
    print(f"[BingX Adopt] {_diag}", flush=True)
    for _k, _v in active_real_trades.items():
        if _v.get("exchange") == "bingx":
            print(f"[BingX倉] {_v.get('symbol')} {_v.get('direction')} "
                  f"es={_v.get('exit_strategy') or '固定R'} sl={_v.get('current_sl')} "
                  f"tp1={_v.get('tp1_hit')}", flush=True)
    dc_log(f"ℹ️ {_diag}")
    if adopted: save_active_trades()


# ══════════════════════════════════════════════════════════════════════════════
# ★CME週末缺口策略(2026-07-04,麥門缺口框架移植):主流三幣專屬
#   合成CME缺口 = 週五21:00 UTC收盤價 vs 週日23:00 UTC開盤價,|缺口|>1.5%才武裝
#   補滿(1H收盤K觸及缺口遠端)→下一根順「補的方向」市價進場
#   SL=補滿前12根1H極值±0.05%, TP=2R整倉, 300h超時市價平倉
#   回測(_maimen_results.txt):BTC+0.306/ETH+0.155/SOL+0.340,7期WF 7/7正,EV+0.268,PF1.49,
#   12組參數擾動全正;山寨14幣EV-0.007=無效(CME錨定效應,主流限定)
#   風險=每週總預算 CME_GAP_WEEKLY_RISK 攤給當週武裝幣數(三幣同週末82%同向=同一注)
CME_GAP_ENABLED     = True
CME_GAP_COINS       = ["BTC/USDT", "ETH/USDT", "SOL/USDT"]
CME_GAP_MIN_PCT     = 0.015      # 缺口門檻1.5%(回測:>1.5%桶才有edge)
CME_GAP_WEEKLY_RISK = 0.02       # 每週總風險預算2%(用戶2026-07-04拍板),攤給觸發幣
CME_GAP_TP_R        = 2.0
CME_GAP_SL_WIN      = 12         # SL=補滿前12根1H極值
CME_GAP_FILL_WIN_H  = 336        # 缺口有效期2週
CME_GAP_TIMEOUT_H   = 300        # 進場後300h未觸SL/TP→市價平倉(回測同款)
CME_GAP_STATE_FILE  = "cme_gap_state.json"
_cme_state: Dict[str, Any] = {}

def _cme_load_state():
    global _cme_state
    try:
        with open(CME_GAP_STATE_FILE, "r") as f:
            _cme_state = json.load(f)
    except Exception:
        _cme_state = {}

def _cme_save_state():
    try:
        with open(CME_GAP_STATE_FILE, "w") as f:
            json.dump(_cme_state, f)
    except Exception as e:
        print(f"[CME-Gap] 存檔失敗: {e}")

def _cme_week_anchor(now: datetime) -> datetime:
    """最近一個已過去的週五21:00 UTC"""
    d = now
    while d.weekday() != 4:   # 4=Friday
        d -= timedelta(days=1)
    anchor = d.replace(hour=21, minute=0, second=0, microsecond=0)
    if anchor > now:
        anchor -= timedelta(days=7)
    return anchor

def _cme_gap_poll():
    """主迴圈每輪呼叫:武裝週末缺口→偵測補滿→進場。全部只看已收1H K(fetch_market_candles已去未收K)。"""
    global _cme_state
    if not CME_GAP_ENABLED:
        return
    now = datetime.now(timezone.utc)
    anchor = _cme_week_anchor(now)
    reopen = anchor + timedelta(hours=50)   # 週日23:00 UTC
    wk_key = anchor.strftime("%Y-%m-%d")

    # ── 1) 武裝本週缺口(週日23:00後,一次性) ─────────────────────────
    if now >= reopen + timedelta(hours=1) and _cme_state.get("week") != wk_key:
        gaps = {}
        for coin in CME_GAP_COINS:
            inst = OKX_SWAP.get(coin)
            if not inst:
                continue
            try:
                df = fetch_market_candles(inst, "1H")
                if df.empty:
                    continue
                fri_bar = df[df.index == (anchor - timedelta(hours=1))]   # 20:00開盤那根的收盤=21:00收盤價
                sun_bar = df[df.index == reopen]
                if fri_bar.empty or sun_bar.empty:
                    continue
                c = float(fri_bar["close"].iloc[0]); o = float(sun_bar["open"].iloc[0])
                pct = abs(o - c) / c
                if pct >= CME_GAP_MIN_PCT:
                    gaps[coin] = {"lo": min(o, c), "hi": max(o, c),
                                  "dir": 1 if o > c else -1, "pct": round(pct * 100, 2),
                                  "filled": False, "traded": False}
            except Exception as e:
                print(f"[CME-Gap] {coin} 武裝失敗: {e}")
        _cme_state = {"week": wk_key, "reopen": reopen.isoformat(), "gaps": gaps, "n_armed": len(gaps)}
        _cme_save_state()
        if gaps:
            msg = " / ".join(f"{k.split('/')[0]} {v['pct']}%{'↑' if v['dir']==1 else '↓'}" for k, v in gaps.items())
            dc_log(f"🕳️ **CME週末缺口武裝** ({wk_key}週): {msg} · 每幣風險{CME_GAP_WEEKLY_RISK/max(len(gaps),1)*100:.1f}% · 等補滿順向進場")

    # ── 2) 偵測補滿→進場 ────────────────────────────────────────────
    if _cme_state.get("week") != wk_key or not _cme_state.get("gaps"):
        return
    try:
        reopen_dt = datetime.fromisoformat(_cme_state["reopen"])
    except Exception:
        return
    if now > reopen_dt + timedelta(hours=CME_GAP_FILL_WIN_H):
        return   # 過期缺口不再追
    n_armed = int(_cme_state.get("n_armed", 1)) or 1
    for coin, g in _cme_state["gaps"].items():
        if g.get("filled") or g.get("traded"):
            continue
        inst = OKX_SWAP.get(coin)
        if not inst:
            continue
        try:
            df = fetch_market_candles(inst, "1H")
            if df.empty:
                continue
            recent = df[df.index >= reopen_dt]
            if recent.empty:
                continue
            fill_i = None
            his = recent["high"].values; los = recent["low"].values
            for i in range(len(recent)):
                if (g["dir"] == 1 and los[i] <= g["lo"]) or (g["dir"] == -1 and his[i] >= g["hi"]):
                    fill_i = i
                    break
            if fill_i is None:
                continue
            g["filled"] = True
            # 只在「補滿K是最近2根已收K」時進場(bot當機錯過就放棄,不追陳舊訊號)
            if fill_i < len(recent) - 2:
                g["traded"] = False
                _cme_save_state()
                print(f"[CME-Gap] {coin} 補滿但訊號陳舊(第{fill_i}/{len(recent)}根),放棄")
                continue
            side = "short" if g["dir"] == 1 else "long"   # 順補的方向
            fill_pos = df.index.get_indexer([recent.index[fill_i]])[0]
            w0 = max(0, fill_pos - CME_GAP_SL_WIN + 1)
            if side == "long":
                sl = float(df["low"].iloc[w0:fill_pos + 1].min()) * 0.9995
            else:
                sl = float(df["high"].iloc[w0:fill_pos + 1].max()) * 1.0005
            entry = float(df["close"].iloc[-1])
            risk = (entry - sl) if side == "long" else (sl - entry)
            if risk <= 0 or risk / entry > 0.12 or risk / entry < 0.002:
                g["traded"] = True; _cme_save_state()
                print(f"[CME-Gap] {coin} 風險距離不合格({risk/entry:.3%}),跳過")
                continue
            tp = entry + risk * CME_GAP_TP_R * (1 if side == "long" else -1)
            scale = (CME_GAP_WEEKLY_RISK / n_armed) / max(RISK_PCT, 1e-9)
            g["traded"] = True
            _cme_save_state()
            try:
                if EXCHANGE_ENABLED.get("okx", True) and not _dir_skew_block(side, "okx"):
                    execute_okx_trade_pipeline(inst, side, entry, sl, tp, tp, "fixed", "1H",
                                               position_scale=scale, pyramid_eligible=False,
                                               exit_strategy="cme_gap")
                if EXCHANGE_ENABLED.get("bingx", True) and not _dir_skew_block(side, "bingx"):
                    execute_bingx_trade_pipeline(coin, side, entry, sl, tp, tp, "fixed", "1H",
                                                 position_scale=scale, exit_strategy="cme_gap")
                dc_log(f"🕳️ **CME缺口補滿→順向進場** {coin.split('/')[0]} {'做多' if side=='long' else '做空'}"
                       f" 進場`{entry}` SL`{round(sl,6)}` TP2R`{round(tp,6)}` 風險{CME_GAP_WEEKLY_RISK/n_armed*100:.1f}%·300h超時")
            except Exception as te:
                print(f"[CME-Gap] {coin} 下單失敗: {te}")
        except Exception as e:
            print(f"[CME-Gap] {coin} 補滿偵測失敗: {e}")

def main_polling_loop():
    """ 交易中樞核心守護進程主迴圈 """
    global _PAUSED, _bot_ref, _INITIAL_BALANCE
    # 啟動時永遠重新抓幣單（Railway 容器重啟後快取消失，需重新抓）
    build_dynamic_symbols()
    # 還原重啟前的倉位追蹤（保本/移動止損續行，解決 redeploy 後追蹤丟失）
    load_active_trades()
    _cme_load_state()   # CME週末缺口狀態(redeploy不丟武裝中的缺口)
    # 接管現有未追蹤的 OKX 倉位（手動開的/重啟前丟失的）→ 讀既有止損納入自動保本
    adopt_untracked_okx_positions()
    # 接管現有未追蹤的 BingX 倉位（與 OKX adopt 對齊，解決 redeploy 後 BingX 追蹤全失）
    adopt_untracked_bingx_positions()
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

            # CME週末缺口:武裝/補滿偵測/進場(便宜,只打3幣K線API)
            try:
                _cme_gap_poll()
            except Exception as _ce:
                print(f"[CME-Gap] 輪詢例外: {_ce}", flush=True)

            # 每日 00:00(UTC) 復盤發 Discord(daily_tick 內部每日去重,僅 00:xx 時段建 client)
            try:
                if datetime.now(timezone.utc).hour == 0:
                    _rep = daily_report.daily_tick(_initialize_ccxt_client())
                    if _rep:
                        dc_log(_rep)
            except Exception as _re:
                print(f"[DailyReport] 失敗: {_re}", flush=True)

            # 每日自動更新幣種列表（1天 = 86400秒)：market cap 變動小,但漲跌幅榜需日更才有意義
            if time.time() - _symbols_last_updated > 86400:
                print("[SYMBOLS] 距上次更新超過1天，自動重新抓取(含漲跌幅榜)...", flush=True)
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
