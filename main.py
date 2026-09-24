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
from concurrent.futures import ThreadPoolExecutor
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
import dashboard      # 私人儀表板(唯讀,掛在既有 Flask 上;沒設 DASH_TOKEN 就整個不存在)
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
# ★2026-09-14 OKX 與 BingX 同一個幣名字不同 → 原本直接 "MON/USDT"→"MON-USDT" 找不到就判「BingX不支援」靜默跳過。
#   用戶指正「bingx有mon好嗎」:BingX 叫 MONAD-USDT。對照 OKX 464 個 USDT 永續 vs BingX 1216 合約,
#   名字不同的候選 16 個,**用兩邊即時價格驗證**(比值 1.000±0.1% 才算同一個幣):
#   ✅同幣同價位 6 個(下表);❌不同幣 5 個(LIGHT≠LIGHTER、MET≠METIS、NET≠NETNET、PENG≠PENGU、RAM≠RAMSES,絕不能對應)。
#   ⚠️ 1000BONK/1000PEPE/1000SHIB/10000SATS 價格單位差 1000~10000 倍,下單價/停損/追蹤都要換算,**刻意不放**(另案);
#   XAU→XAUT 是商品,幣池已排除。只收「價格單位相同」的,因為倉位追蹤用 OKX 報價(trade["symbol"])算停損。
BINGX_SYMBOL_ALIAS = {"MON": "MONAD", "AEON": "AEONBSC", "EDGE": "EDGEX", "GRAM": "GRAMTON",
                      "NEIRO": "NEIROCTO", "TRUMP": "TRUMPSOL"}
BINGX_SYMBOL_ALIAS_REV = {v: k for k, v in BINGX_SYMBOL_ALIAS.items()}


def _to_bingx_symbol(symbol_id: str) -> str:
    """OKX/ccxt 幣名(MON/USDT) → BingX 合約名(MONAD-USDT)"""
    base, _, quote = symbol_id.replace("-", "/").partition("/")
    return f"{BINGX_SYMBOL_ALIAS.get(base, base)}-{quote or 'USDT'}"


def _from_bingx_symbol(bx_sym: str) -> str:
    """BingX 合約名(MONAD-USDT) → OKX/ccxt 幣名(MON/USDT),接管判斷要跟追蹤池同一格式"""
    base, _, quote = bx_sym.partition("-")
    return f"{BINGX_SYMBOL_ALIAS_REV.get(base, base)}/{quote or 'USDT'}"

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
_PAUSED = False  # ★2026-08-02 恢復,但只開「有用的」5支(容錯≥11且一致性過關):
# MACD多(1H)18.3🟢 / OISQ空17.8🟢 / CME缺口~14🟢 / C3階梯空13.5🟢(n=49小) / OISQ多11.3🟡
# ⚠️2026-08-26訂正:C3階梯空那個13.5是**3期n=49**的數字;跑滿7期的裸基準其實是容錯-1.5🔴。
#   已把進場改成官方的三步驟序列(見 short_C1/C2/C3 註解)→ 7期 n=129/勝52.7%/容錯11.6🟡/正期4-5。
# 已關(容錯不足或未審):吞噬空4.5🔴、箱突破空8.2🟡4期正、HF_1R平行層(未審且會加倍C3空倉位)、
#   1H MACD空8-10🔴、15m MACD多0.2🔴、BPR(頻率10倍bug)、其餘既有停用項。
# 預估頻率約1.7張/天(對照全開7.6張/天=降78%,手續費同步降),兩週約可收24筆=足以判斷live勝率是否回到45-50%。
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
# ★2026-09-15 快取改存 Railway volume(/data):程式目錄每次 redeploy 被清成 repo 版(5月的 47 幣舊檔),
#   剛好又碰上 CoinGecko 429 → 整個掃描池卡在 47 幣(原本 165)。repo 那份只在 volume 沒檔時當初始值讀。
_SYMBOLS_CACHE_REPO = os.path.join(os.path.dirname(os.path.abspath(__file__)), "symbols_cache.json")
_SYMBOLS_CACHE_FILE = os.path.join(os.environ.get("PERSIST_DIR") or ("/data" if os.path.isdir("/data") else os.path.dirname(os.path.abspath(__file__))),
                                   "symbols_cache.json")
_symbols_last_updated: float = 0.0   # UNIX timestamp，0 = 從未更新(CoinGecko市值前100,慢變動,維持24h週期)
_top100_base_symbols: Dict[str, str] = {}  # ★2026-07-07:市值前100的純淨底池(不含漲跌幅榜疊加),
                                            # 讓輕量刷新能「整批換掉」漲跌幅榜疊加部分而非只增不減(防清單無限膨脹)
_movers_last_updated: float = 0.0    # ★2026-07-07:漲跌幅榜獨立計時,原本綁在24h全量更新裡太慢
                                      # (幣中途暴衝完全落幕才輪到刷新=整段行情看不到,不是訊號漏抓是幣種根本沒進掃描清單)
                                      # 漲跌幅榜只打1支OKX輕量API(跟現役K線/OI查詢同等級),獨立用MOVERS_REFRESH_SEC刷新
                                      # 不必re-hit CoinGecko(該API有速率限制,市值排名本就慢變動不需要常刷)
MOVERS_REFRESH_SEC = 3600             # 漲跌幅榜每1小時刷新(對齊1H K棒週期,新增負擔=每小時多1支OKX ticker呼叫,可忽略)

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
# ★2026-09-10 補模組層 MIN_SL_PCT:原本只在 scan_and_process_market 內部有一個**局部變數**
#   (`MIN_SL_PCT = 0.006` 縮排在函數裡),其他函數引用它會執行期 NameError。
#   `_chk_names.py` 抓不到這種(它只檢查「名字有沒有在檔案裡定義過」,不檢查作用域)——
#   跟 2026-09-04 `_crypto_mv` 害漲跌幅榜死一整天是同一個坑。值與 _bt_lib_faithful 一致。
MIN_SL_PCT = 0.006   # 0.6%:停損太近=結構無效→倉位放超大+一根K秒進秒損
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
# ★★2026-09-14 修:容器檔案系統**每次 redeploy 都清空**(Railway 原本沒掛 volume),存在程式目錄的
#   active_trades.json 跟著消失 → 每次部署所有持倉被當「未追蹤」重新接管、出場一律被改成 swing_full
#   (0904/0911 都記過、一直沒修;09-14 BOR 的 BCH/SKY/AEON/CC/BICO 全被改寫)。
#   → 改存到 Railway volume(掛在 /data);本機/沒掛時退回程式目錄。PERSIST_DIR 環境變數可覆寫。
_PERSIST_DIR = os.environ.get("PERSIST_DIR") or ("/data" if os.path.isdir("/data") else os.path.dirname(os.path.abspath(__file__)))
_TRADES_FILE = os.path.join(_PERSIST_DIR, "active_trades.json")
_RISK_STATE_FILE = os.path.join(_PERSIST_DIR, "strategy_risk_state.json")   # BOR/4JD 熔斷計數(redeploy 不歸零)
# ★「交易所掛好 SL/TP 就不碰」的出場型:BOR 固定1R、S4H 固定2.5R、接管時認不出原策略的倉(adopt_hold)。
#   不移保本、不移SL、不加碼;只在倉位消失時移除追蹤(BOR 另做熔斷計數)。
#   ★2026-09-15 S4H 移出(用戶:「保住本金為主」)→ 改走 box_trend/fourjd_2r 那段「只做一次保本」,浮盈 S4H_BE_R 移保本。
#   BOR 維持不保本(用戶:「停利1R的就不用保本了」)。
_HANDS_OFF_ES = ("bor_1r", "engulf_1r", "fourjd_1r", "adopt_hold")


def save_risk_state():
    """熔斷計數落地(CLAUDE.md 第11條:觀察條款寫成代碼;redeploy 歸零 = 熔斷形同虛設)"""
    try:
        with open(_RISK_STATE_FILE, "w", encoding="utf-8") as f:
            json.dump({"bor": globals().get("_BOR_RISK"), "fourjd": globals().get("_FOURJD_RISK")}, f)
    except Exception as e:
        print(f"[Persist] 存檔熔斷計數失敗: {e}")


def load_risk_state():
    try:
        if not os.path.exists(_RISK_STATE_FILE): return
        with open(_RISK_STATE_FILE, encoding="utf-8") as f:
            d = json.load(f)
        for key, g in (("bor", "_BOR_RISK"), ("fourjd", "_FOURJD_RISK")):
            if isinstance(d.get(key), dict) and isinstance(globals().get(g), dict):
                globals()[g].update({k: d[key][k] for k in ("consec_sl", "halted") if k in d[key]})
        print(f"[Persist] 熔斷計數已讀回 BOR={globals().get('_BOR_RISK')} 4JD={globals().get('_FOURJD_RISK')}", flush=True)
    except Exception as e:
        print(f"[Persist] 讀回熔斷計數失敗: {e}")

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
    if "BPR" in s:         bits.append("BPR失衡區重合回測中軸(固定1.5R)")
    # ★2026-09-11 修策略名誤標(對帳時抓到):
    #   ①S4H 的來源標籤「S4H做空(4h吞噬+…)」含「吞噬」→ 被印成吞噬空(XPL 4H 那張卡)
    #   ②4JD 的「4J減速跌破空」含「4J」→ 被印成 4J結構回踩
    #   ③VLONG「V成型吸收多」/ LL→LH 沒有對應 → 卡片只印「多頭趨勢」,08-31起12筆VLONG全被歸成「未知」
    #   一律改成比對**完整標籤前綴**,不再用會互相包含的短字。
    if "吞噬空" in s:      bits.append("山寨看跌吞噬 + 放量 + EMA100下跌regime + 流動性≥10萬U")
    if "S4H做空" in s:     bits.append("S4H:4h看跌吞噬 + 趨勢線123 + 斐波0.382~0.618 + LL(固定2.5R)")
    if "突破回踩空" in s:   bits.append("突破回踩:4h收盤跌破前低 → 反彈回測前低(壓力支撐互換) → 看跌吞噬才進(固定1R)")
    if "4J減速跌破空" in s: bits.append("4J減速跌破:2h位階 + 1H減速磨上去 + 跌破盤整低(2R + 0.8R保本)")
    if "V成型吸收多" in s:  bits.append("V成型吸收:低點墊高 + 合約CVD低點降低(2.5R)")
    if "LL→LH反彈空" in s:  bits.append("LL→LH 反彈斐波吞噬空")
    # ★2026-08-27:4J 上線當天漏了這條 → 第一筆live單(ONE 15m多)的訊號卡只印「多頭趨勢」,
    #   沒有策略名 → 之後對帳會把它算到別人頭上(CLAUDE.md 第12條踩過的坑:BPR空61筆被標成C3空)。
    #   帶上階梯(4H→30m / 2H→15m),否則兩階分不開。
    if "4J結構回踩" in s:
        _rg = ""
        if "(" in s and ")" in s:
            _seg = s[s.find("4J結構回踩"):]
            if "(" in _seg and ")" in _seg:
                _rg = _seg[_seg.find("(")+1:_seg.find(")")]
        bits.append(("4J" + (f"({_rg})" if _rg else "")
                     + ":高時框結構實體突破→回踩該位階守住(TP1 1R半平+保本/TP2 3R)"))
    # ★2026-07-18修:原本對「所有1H空單」無條件加「靠階梯壓力位」→BPR空/MACD空的卡都被標成階梯,
    #   14天61筆BPR空全被誤讀成C3空,歸因差點錯人。階梯字樣只有C3空才真的有這個閘。
    if tf == "1H" and side == "short" and "C3" in s:
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
    # 儀表板：訊號卡發一張就記一筆(策略名用訊號卡同一個 source_tag,避免顯示層跟策略對不上)
    try:
        dashboard.sig(symbol, tf, sig["side"], sig.get("source_tag", "") or "—",
                      float(sig["entry"]), float(sig["sl"]))
    except Exception:
        pass
    # ★2026-09-01 修:順籌碼分數是「**進場當下 1H 的快照**」,不是即時值。
    #   用戶回報 LA 字卡寫 +8、之後用 `幣` 指令查卻是負的 → 同一個 judge_coin,
    #   差別只是**算的時間點不同**(字卡=進場當下 / 指令=你查的當下),分數本來就會變。
    #   先前字卡沒標時間與時框,看起來像兩個系統打架 → 補上快照標記,對帳才不會歸因錯人。
    try:
        _judge_brief = judge_coin(coin_name, sig["side"], brief=True)
        if _judge_brief:
            _judge_brief = (f"{_judge_brief}　_(1H·進場當下快照 "
                            f"{datetime.now(timezone.utc).strftime('%m/%d %H:%M')}UTC"
                            f";之後用 `{coin_name}` 查為即時值,會不同)_")
    except Exception:
        _judge_brief = None
    # ★數據面板(用戶2026-09-03:要像數據獵手字卡那樣一排一排攤開,排版整齊)
    _data_panel = ""
    try:
        _v = _VLONG_LAST.get(symbol)
        if _v:
            _cd = _v.get("cvd_delta"); _oip = _v.get("oi_pct")
            _ls = _tk = None
            try:
                _ls, _tk = _fetch_ls_taker(symbol, "15m")
            except Exception:
                pass
            _rw = ["", "━━━━━━━━━━━━━━━━━━",
                   f"`型態　　`V成型吸收 · 低點墊高 **+{_v['up_pct']:.2f}%**",
                   f"`結構　　`擺動 {_v['swing']:.1f}%　間隔 {_v['gap_bars'] * 15} 分鐘",
                   f"`低點　　`{_v['low1']:.6g} → **{_v['low2']:.6g}**",
                   f"`合約CVD`{'　賣壓被吸收 ✅' if (_cd is not None and _cd < 0) else '　—'}"
                   + (f"（{_cd:+,.0f}）" if _cd is not None else "")]
            if _oip is not None:
                _rw.append(f"`持倉1h　`{_oip:+.2f}%　{'增倉 📈' if _oip > 0 else '減倉 📉'}")
            if _ls:
                _rw.append(f"`多空比　`多方 {_ls * 100 / (1 + _ls):.0f}%")
            if _tk:
                _rw.append(f"`主動買賣`{_tk:.2f}　{'買方主導' if _tk > 1 else '賣方主導'}")
            _rw.append("━━━━━━━━━━━━━━━━━━")
            _data_panel = chr(10).join(_rw)
    except Exception as _dpe:
        print(f"[V-Long] 數據面板組裝失敗: {_dpe}", flush=True)
    embed_payload = {
        "title": f"{side_emoji} {coin_name} · {tf} {dir_name}",
        "description": f"**進場原因:** {reason}"
                       + ((chr(10) + f"**順籌碼:** {_judge_brief}") if _judge_brief else "")
                       + _data_panel,
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
    # ★2026-09-11 加 volCcyQuote(=USDT成交額)。OKX 的 "vol" 是**合約張數**,不是幣數;
    #   張數×收盤價 ≠ 成交額,誤差倍數=合約面值ctVal(BTC 0.01→高估100倍、WLFI 10→低估10倍、PEPE 1e7)。
    #   S4H/4JD 的流動性閘原本用 vol×close,尺度跟回測(幣安 vol=幣數)對不上。
    for col in ["open", "high", "low", "close", "vol", "volCcyQuote"]:
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
                              exit_strategy: str = "", allow_stack: bool = False,
                              timestop_h: int = 0, be_pct: float = 0.0) -> None:
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
        elif exit_strategy in ("box_trend", "hf_1r", "fourjd_2r"):
            # ── 整倉單一TP:box_trend=4R讓跑/達1R保本;hf_1r=高頻固定1R全平/達0.5R保本(不讓跑);
            #    fourjd_2r=4J減速跌破空 固定2R全平/達0.8R保本。
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
            if exit_strategy in ("line_full", "swing_full", "line_add", "box_trend", "hf_1r", "fourjd_2r"):
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
                "ts_h":             int(timestop_h or 0),   # ★2026-08-02 本策略專屬時間停損(0=用型態預設)
                "be_pct":           float(be_pct or 0.0),  # ★浮盈達進場價的N%就移保本(0=不用;目前只有4J)
                "be_done":          False,
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
                                  allow_stack: bool = False,
                                timestop_h: int = 0) -> None:
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
        # 轉換幣種格式：BTC/USDT → BTC-USDT(★名字不同的幣走 BINGX_SYMBOL_ALIAS,如 MON→MONAD)
        bingx_symbol = _to_bingx_symbol(symbol_id)

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
        elif exit_strategy in ("box_trend", "hf_1r", "fourjd_2r"):
            # 整倉單一TP:box_trend=4R/達1R保本;hf_1r=1R全平/0.5R保本;fourjd_2r=2R全平/0.8R保本。TP全倉掛交易所自動成交。
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
        _rem_qty = round(actual_qty, 4) if exit_strategy in ("line_full","line_add","swing_full","box_trend","hf_1r","fourjd_2r") else half_qty
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
            "ts_h":             int(timestop_h or 0),   # ★2026-08-02 本策略專屬時間停損(0=用型態預設)
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

        # ★2026-07-19 浮盈門檻(用戶指正「浮盈之後才叫移動停利」+14天實單65%的移動發生在虧損側):
        #   進場後最大有利波動 < 1R 前不啟動移動停利,SL停在原結構停損。
        #   回測驗證(_bt_trail_gate.py,7期):OISQ空+0.315→+0.378/OISQ多+0.103→+0.127/
        #   MACD空+0.160→+0.168(正期4→5)/MACD多+0.347→+0.312(微降),3/4改善=免費甚至加分。
        _entry_px = float(trade.get("entry_price") or 0)
        _risk_d = float(trade.get("risk_dist") or 0)
        if _entry_px > 0 and _risk_d > 0:
            _mfe = (float(hi.max()) - _entry_px) if direction == "long" else (_entry_px - float(lo.min()))
            if _mfe < 1.0 * _risk_d:
                return False   # 浮盈不足1R,不移(留在結構停損,避免虧損中被雜訊pivot掃出)

        # 移SL用「pivot 擺盪點」(前後2根局部極值)。回測:pivot 勝 N字型(N MDD暴增58~71%)→回退。
        # ★bugfix 2026-06-21:先濾「市價合法側」再挑最緊pivot。舊版先挑全窗最極端pivot,噴後整理時
        #   它落在市價錯側→整個更新被否決凍住(該噴後跟漲的整理段不跟,AXS案例根因)。
        # ★2026-07-19 加pivot緩衝(舊版裸pivot比回測緊)。同日LINK插針案例後加深0.2→0.4ATR:
        #   回測_bt_trail_buffer.py掃0.2/0.4/0.6全無差(EV±0.005),live pivot(PV=2)比回測(7根窗)淺,加深免費避獵殺區。
        PV = 2
        _tr = np.maximum(hi[1:] - lo[1:], np.maximum(np.abs(hi[1:] - cl[:-1]), np.abs(lo[1:] - cl[:-1])))
        _atr_now = float(pd.Series(_tr).ewm(alpha=1/14, adjust=False).mean().iloc[-1]) if len(_tr) >= 5 else 0.0
        cur_sl = float(trade.get("current_sl", 0) or 0)
        cur_px = float(cl[-1])
        last_swing = None
        for j in range(PV, n - PV):
            if direction == "long":
                _pv = lo[j] - 0.4 * _atr_now
                if lo[j] == lo[j-PV:j+PV+1].min() and _pv < cur_px:     # 只取市價下方(合法側,含緩衝)
                    if last_swing is None or _pv > last_swing: last_swing = _pv
            else:
                _pv = hi[j] + 0.4 * _atr_now
                if hi[j] == hi[j-PV:j+PV+1].max() and _pv > cur_px:     # 只取市價上方(合法側,含緩衝)
                    if last_swing is None or _pv < last_swing: last_swing = _pv
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
    # 0) ★2026-07-19 BingX端錯側防呆(用戶指出「BingX點位跟OKX不同」):pivot/保本價全是用OKX K線算的,
    #    兩所價差(小幣可觀)可能讓新SL在BingX已越過現價→STOP_MARKET掛上即觸發=倉被莫名市價平掉。
    #    掛單前用BingX自己的最新價驗證保護側,錯側→跳過本輪(舊SL保留,下輪pivot更新自然重試)。
    try:
        _pq = _bingx_request("GET", "/openApi/swap/v2/quote/price", {"symbol": sym}, hdr).json()
        _bx_px = float((_pq.get("data") or {}).get("price") or 0)
        if _bx_px > 0:
            _slp = float(sl_price)
            if trade.get("direction") == "long" and _slp >= _bx_px:
                print(f"[BingX-SL] {sym} 新SL {_slp} ≥ BingX現價 {_bx_px}(OKX/BingX價差錯側),跳過本輪不移", flush=True)
                return None
            if trade.get("direction") == "short" and _slp <= _bx_px:
                print(f"[BingX-SL] {sym} 新SL {_slp} ≤ BingX現價 {_bx_px}(OKX/BingX價差錯側),跳過本輪不移", flush=True)
                return None
    except Exception as _pxe:
        print(f"[BingX-SL] {sym} 查BingX現價失敗(照原流程掛): {_pxe}", flush=True)
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
HF_1R_ENABLED = False            # ★2026-08-02暫停:此層會在C3空/MACD空/15m MACD多訊號成立時**額外**開一筆
# 固定1R平行倉→等於把那些策略的倉位數與手續費加倍,而它從未進入本輪容錯檢視。清乾淨才能量到單純的策略績效。原註:# 高頻固定1R平行層:現役3格(1H C3空/1H MACD空/15m MACD多)訊號成立時多開一筆固定1R/0.5R保本獨立倉,各跑各的
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
        cl_v = df["close"].values
        # ★2026-07-19 與OKX對齊:浮盈<1R不啟動移動停利(見_mai_move_sl_on_swing同日註解+_bt_trail_gate.py)
        _entry_px = float(trade.get("entry_price") or 0)
        _risk_d = float(trade.get("risk_dist") or 0)
        if _entry_px > 0 and _risk_d > 0:
            _mfe = (float(hi.max()) - _entry_px) if direction == "long" else (_entry_px - float(lo.min()))
            if _mfe < 1.0 * _risk_d:
                return False
        # pivot 擺盪點(前後2根局部極值)。回測勝 N字型(N MDD暴增)→回退。
        # ★bugfix 2026-06-21:與OKX對齊——先濾市價合法側再挑最緊pivot(舊版先挑全窗最極端,
        #   噴後整理時落在錯側→整段更新被否決凍住=BingX該跟不跟根因)。並補診斷log(BingX原本一個都沒有)。
        # ★2026-07-19 pivot緩衝0.4ATR(與OKX一致;LINK插針案例後加深,回測0.2/0.4/0.6無差=免費)。
        cur_px = float(df["close"].iloc[-1])
        cur_sl = float(trade.get("current_sl", 0) or 0)
        PV = 2
        _tr = np.maximum(hi[1:] - lo[1:], np.maximum(np.abs(hi[1:] - cl_v[:-1]), np.abs(lo[1:] - cl_v[:-1])))
        _atr_now = float(pd.Series(_tr).ewm(alpha=1/14, adjust=False).mean().iloc[-1]) if len(_tr) >= 5 else 0.0
        last = None
        for j in range(PV, n - PV):
            if direction == "long":
                _pv = lo[j] - 0.4 * _atr_now
                if lo[j] == lo[j-PV:j+PV+1].min() and _pv < cur_px:
                    if last is None or _pv > last: last = _pv
            else:
                _pv = hi[j] + 0.4 * _atr_now
                if hi[j] == hi[j-PV:j+PV+1].max() and _pv > cur_px:
                    if last is None or _pv < last: last = _pv
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

    # ★2026-08-01 持倉存在性改「每輪全查一次」(原本逐倉 fetch_positions([symbol]) 在迴圈內打N次API,
    #   OKX倉位端點限流(10次/2秒)→瞬斷回空→連2輪即誤判「倉位已關閉」把還開著的倉踢出追蹤池
    #   →trail/保本/時間停損全部失效(13天實證:31筆持倉>24h但時停只觸發2次、保本0次)。
    #   全查=1次API,既省限流又避免誤刪;查詢失敗時本輪完全不做移除判定(_pos_ok=False)。
    _pos_set = set(); _pos_ok = False; _pos_n = 0   # ★_pos_n=真實倉數(每倉會放2個entry進_pos_set)
    # ★★2026-09-19 新增 _pos_sz:同一次 API 順手記下**交易所實際張數**(不多打任何請求)。
    #   動機(ZEN 裸倉事故):時間停損/OI早出 平倉用的是 `remaining_amount`,但這個值在
    #   「TP1出一半」型態下只有半倉(且 redeploy 後由推斷重建,更容易失準)→ 平不乾淨,
    #   卻照樣 `_cancel_okx_algo_order` 撤停損 + 從追蹤池移除 → **剩下的量變成無停損又無人管的裸倉**。
    _pos_sz: Dict[Any, float] = {}
    try:
        for _p in ex.fetch_positions():
            if abs(float(_p.get("contracts") or 0)) > 0 and _p.get("side"):
                _sym = _p.get("symbol"); _sd = _p.get("side")
                _pos_n += 1
                _pos_set.add((_sym, _sd))
                _pos_sz[(_sym, _sd)] = abs(float(_p.get("contracts") or 0))
                try:
                    _iid = (_p.get("info") or {}).get("instId")
                    if _iid: _pos_sz[(_iid, _sd)] = abs(float(_p.get("contracts") or 0))
                except Exception:
                    pass
                # ★★2026-08-27 致命bug修復:ccxt fetch_positions 回的是**統一格式**('ONE/USDT:USDT'),
                #   但追蹤池記的 trade["symbol"] 是 execute_okx_trade_pipeline 收到的 **instId**
                #   ('ONE-USDT-SWAP',來源 OKX_SWAP)。兩者**永遠比不上** →
                #   `(symbol,direction) in _pos_set` 恆為 False → 每個OKX倉開倉後3輪就被踢出追蹤池 →
                #   **保本/移動停損/時間停損/TP1成交後移保本 全部從來沒運作過**(不只4J,是所有策略)。
                #   這是 2026-08-01「持倉存在性改每輪全查」重構引入的(本意是修誤刪,結果引入格式不匹配)。
                #   症狀在 log 裡自相矛盾:「追蹤池 OKX=1 交易所實際持倉=1 查詢OK=True」下一行卻
                #   「ONE-USDT-SWAP 倉位已關閉(連3輪查無),移除追蹤」。
                #   修法:兩種格式都放進集合,下方比對再加 inst_id 備援。
                try:
                    _mid = (ex.market(_sym) or {}).get("id")
                    if _mid: _pos_set.add((_mid, _sd))
                except Exception:
                    pass
        _pos_ok = True
    except Exception as _fpe:
        print(f"[Trailing] 全倉查詢失敗(本輪不做移除判定): {_fpe}", flush=True)
    _okx_n = sum(1 for t in active_real_trades.values() if t.get("exchange") == "okx")
    # ★2026-09-10:原本印 len(_pos_set),但每個倉會放 2 個 entry(symbol+instId 兩種格式,見上方
    #   0827 修復說明)→ 5個倉印成10,害我自己誤判「9個倉沒被追蹤」。改印真實倉數。
    #   ★另注意:這裡的「實際持倉」含**手動單**,追蹤池只收 bot 單,兩者本來就不會相等。
    print(f"[Trailing] 追蹤池 OKX={_okx_n} 交易所實際持倉={_pos_n}(含手動單) 查詢OK={_pos_ok}", flush=True)

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
            # 確認倉位是否仍存在(用本輪全查結果,不再逐倉打API)
            if not _pos_ok:
                continue                      # 全查失敗→本輪跳過此倉的管理與移除判定(不誤刪)
            has_pos = ((symbol, direction) in _pos_set
                       or (inst_id, direction) in _pos_set)   # ★兩種格式都比(見上方修復說明)
            if not has_pos:
                # ★防孤兒倉(2026-06-18→2026-08-01加嚴):連3輪查無才移除。
                #   舊版連2輪+逐倉查限流瞬斷=大量誤刪→倉位失管跑到80h沒人砍(13天實證)。
                _miss = int(trade.get("_pos_miss", 0)) + 1
                if _miss < 3:
                    trade["_pos_miss"] = _miss; save_active_trades(); continue
                print(f"[Trailing] {name} 倉位已關閉(連{_miss}輪查無)，移除追蹤")
                # ★4J減速跌破空 熔斷計數(CLAUDE.md第11條:觀察條款要寫成代碼)。
                #   判定精確:TP=2R > 保本觸發0.8R,所以「賺的單必定先經過0.8R」→ tp1_hit=True;
                #   從未觸發保本就消失 = 只能是吃滿停損(本策略 _strat_ts_h=-1 不設時停)。
                if trade.get("exit_strategy") == "fourjd_1r":
                    # ★★2026-09-16 判準換掉:TP 改 1R 全平且**不保本**後,原本的
                    #   `tp1_hit=False ⇒ 吃滿停損` 會把**贏單也算成連虧**(TP1=TP2 同價,一次全平後
                    #   剩餘量歸零 →「TP1成交→移保本」那段的 if new_algo_id: 不成立)。
                    #   這正是 2026-09-13 在 BOR 上記過的坑,改用同一套幾何判準:
                    #   只做空、TP 在進場價下方 1R、SL 在上方 → 現價 ≥ 進場價 ⇒ 停損側出場。
                    #   抓價失敗一律不計數(寧可漏算也不要誤觸熔斷),判定值印進 log 供事後對帳。
                    try:
                        _fjd_ep = float(trade.get("entry_price") or 0)
                        _fjd_cp = float(ex.fetch_ticker(symbol).get("last") or 0)
                        if _fjd_ep > 0 and _fjd_cp > 0:
                            _fjd_is_sl = bool(_fjd_cp >= _fjd_ep)
                            print(f"[4JD] {name} 出場判定 現價{_fjd_cp:.6g} vs 進場{_fjd_ep:.6g}"
                                  f" → {'吃滿停損' if _fjd_is_sl else '獲利出場'}"
                                  f" (連虧{_FOURJD_RISK['consec_sl']}/{FOURJD_MAX_CONSEC_SL})", flush=True)
                            _fourjd_record_result(_fjd_is_sl)
                    except Exception as _fre:
                        print(f"[4JD] 熔斷計數失敗(本筆不計數): {_fre}")
                elif trade.get("exit_strategy") == "bor_1r":
                    # ★突破回踩空 熔斷計數(2026-09-13)。
                    # ★★不能照抄 4JD 的 tp1_hit 判準:BOR **沒有保本**且 TP1=TP2 都在 1R,
                    #   倉位一次全平後剩餘量歸零 → 上面「TP1成交→移保本」那段的 `if new_algo_id:`
                    #   不會成立 → **賺錢出場也是 tp1_hit=False**,照抄會把贏單算成連續吃滿停損。
                    # ★也不能用 algo pending 判「停損單還在不在」:倉位關閉時系統本來就會撤停損殘單
                    #   (本檔 _cancel_okx_algo_order 共14處呼叫,2440/2465/2477 就在這一帶),
                    #   「不在 pending」無法區分是被觸發還是被我們自己撤掉。
                    # ★用幾何判準:BOR 只做空,TP 在進場價**下方** 1R、SL 在**上方** 1R,
                    #   兩者對稱夾住進場價 → 現價 ≥ 進場價 ⇒ 停損側出場;現價 < 進場價 ⇒ 獲利側出場。
                    # ★誤判邊界:倉位關閉後到抓價之間(數秒)價格剛好穿越進場價會判錯;
                    #   抓價失敗一律**不計數**(寧可漏算也不要誤觸熔斷)。判定值印進 log 供事後對帳。
                    try:
                        _bor_ep = float(trade.get("entry_price") or 0)
                        _bor_cp = float(ex.fetch_ticker(symbol).get("last") or 0)
                        if _bor_ep > 0 and _bor_cp > 0:
                            _bor_is_sl = bool(_bor_cp >= _bor_ep)
                            print(f"[BOR] {name} 出場判定 現價{_bor_cp:.6g} vs 進場{_bor_ep:.6g}"
                                  f" → {'吃滿停損' if _bor_is_sl else '獲利出場'}"
                                  f" (連虧{_BOR_RISK['consec_sl']}/{BOR_MAX_CONSEC_SL})", flush=True)
                            _bor_record_result(_bor_is_sl)
                    except Exception as _bre:
                        print(f"[BOR] 熔斷計數失敗(本筆不計數): {_bre}")
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

            # ── ★全域時間停損(2026-07-19,改善持單體感):超過GLOBAL_TIMESTOP_H小時未觸TP/SL→市價平 ──
            #   cme_gap豁免(自有300h超時)。ts_open=真實開倉時間(接管倉的entry_ts被回撥24h不能用,故另存ts_open)。
            # ts_open優先;無ts_open時只有「非接管倉」能用entry_ts(接管倉entry_ts被回撥24h,舊json無ts_open→跳過不誤平)
            _ts_open = int(trade.get("ts_open") or 0)
            if not _ts_open and trade.get("tf_id") != "adopted":
                _ts_open = int(trade.get("entry_ts", 0) or 0)
            _tsh = _timestop_hours(trade)
            # ★2026-08-03 用戶要求「手動單不要有持倉時數限制」:接管倉必須帶bot_verified(=接管時用broker tag驗過是bot開的)
            #   才准被時間停損平掉。7/19手動倉保護上線「之前」被接管、殘留在active_trades.json的舊資料沒有此旗標→一律不碰。
            _adopted_unverified = (trade.get("tf_id") == "adopted" and not trade.get("bot_verified"))
            if _adopted_unverified and _ts_open > 0 and time.time() - _ts_open > _tsh * 3600:
                print(f"[TimeStop] {name} 接管倉未驗證來源(可能是手動倉)→不套時間停損", flush=True)
            if (not _adopted_unverified and trade.get("exit_strategy") != "cme_gap" and _ts_open > 0
                    and time.time() - _ts_open > _tsh * 3600):
                # ★2026-09-19 用**交易所實際張數**平,不用 remaining_amount(ZEN 裸倉事故:只平了半倉)
                _amt_ts = _pos_sz.get((symbol, direction)) or _pos_sz.get((inst_id, direction)) \
                    or float(trade.get("remaining_amount", 0) or 0)
                try:
                    ex.create_market_order(symbol=symbol,
                        side=("sell" if direction == "long" else "buy"),
                        amount=_amt_ts,
                        params={"posSide": direction, "tdMode": MARGIN_MODE, "reduceOnly": True})
                    _cancel_okx_algo_order(inst_id, trade.get("sl_algo_id"))
                    dc_log(f"⏰ {name} 開倉滿{_tsh}h未到目標,市價平倉(時間停損) 平{_amt_ts}張")
                except Exception as _tse:
                    # ★平倉失敗就**不准**撤停損、也不准移出追蹤池(否則變裸倉)
                    print(f"[TimeStop] {name} 平倉失敗(保留停損與追蹤,不移除): {_tse}", flush=True)
                    continue
                active_real_trades.pop(trade_key, None); save_active_trades(); continue

            # ── 山寨多單 OI降早出(OI_EARLY_EXIT_ENABLED):主力出貨即跑,救COAI式吐回 ──
            if _oi_drop_exit_long(trade):
                # ★2026-09-19 同 ZEN 事故修法:用交易所實際張數,且平倉失敗不撤停損不移除追蹤
                _amt_oi = _pos_sz.get((symbol, direction)) or _pos_sz.get((inst_id, direction)) \
                    or float(trade.get("remaining_amount", 0) or 0)
                try:
                    ex.create_market_order(symbol=symbol, side="sell",
                        amount=_amt_oi,
                        params={"posSide": direction, "tdMode": MARGIN_MODE, "reduceOnly": True})
                    _cancel_okx_algo_order(inst_id, trade.get("sl_algo_id"))
                    dc_log(f"📉 {name} OI降早出(主力出貨),市價平倉 平{_amt_oi}張")
                except Exception as _oie:
                    print(f"[OI-Exit] {name} 平倉失敗(保留停損與追蹤,不移除): {_oie}", flush=True)
                    continue
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

            # ── ★早期保本(be_pct):浮盈達「進場價的 N%」就把SL移到保本 ─────────────
            # 2026-08-27 用戶指定「實盤1%的時候要保本」。這跟本專案舊紀錄衝突
            # (memory/project_exit_finding「達1R保本兜底重創讓跑策略」、CLAUDE.md「保本延後至1.5R」),
            # 所以實測了才上:4J兩階合併 n=926,10期(含2022深熊/2023橫盤):
            #   現行(TP1成交才移保本)  勝率74.1% EV+0.545 容錯25.1 143R/年 最大回撤-8.73R
            #   ★+1%保本(用戶版)      勝率58.3% EV+0.490 容錯**31.5**(最高) 129R/年 最大回撤**-5.99R**
            #   +0.5%保本            勝率46.7% EV+0.404 容錯28.8 106R/年 -5.68R(掃太兇,連虧16)
            #   +1.5%保本            勝率63.8% EV+0.502 容錯29.8 132R/年 -6.82R
            # → EV只掉10%,但**回撤降31%、容錯升到最高**。以「每單位回撤換到的年報酬」看
            #   16.4 → 21.5(+31%)。舊紀錄的「保本太早有害」是對**讓跑型**策略,
            #   對這種「TP1半平+固定TP2」的緊停損策略不成立。腳本 _bt_4j_be.py。
            # 只對有帶 be_pct 的倉生效(目前=4J),其他策略行為不變。
            _bep = float(trade.get("be_pct") or 0)
            if _bep > 0 and not trade.get("be_done") and not trade.get("tp1_hit"):
                try:
                    _ep = float(trade["entry_price"])
                    _cp = float(ex.fetch_ticker(symbol).get("last") or 0)
                    if _cp > 0 and _ep > 0:
                        _gain = (_cp - _ep)/_ep if direction == "long" else (_ep - _cp)/_ep
                        if _gain >= _bep:
                            _fb = _ep * 0.001                       # 保本價含往返手續費
                            _be = _ep + _fb if direction == "long" else _ep - _fb
                            _slnow = float(trade.get("current_sl") or 0)
                            _better = (_be > _slnow) if direction == "long" else (_be < _slnow)
                            if _better:
                                try: _bx = ex.price_to_precision(symbol, _be)
                                except Exception: _bx = format(_be, "f")
                                _exs = "sell" if direction == "long" else "buy"
                                # place-before-cancel:先掛新SL成功才取消舊的(不裸倉,對齊0706那次修法)
                                _r = _place_okx_algo_sl(inst_id=inst_id, side=_exs, amount="0",
                                                        sl_trigger_px=_bx, pos_side=direction)
                                _nid = (_r.get("data") or [{}])[0].get("algoId")
                                if not _nid and str((_r.get("data") or [{}])[0].get("sCode") or "") == "51088":
                                    _okx_cancel_all_algos(inst_id); time.sleep(0.3)
                                    _r = _place_okx_algo_sl(inst_id=inst_id, side=_exs, amount="0",
                                                            sl_trigger_px=_bx, pos_side=direction)
                                    _nid = (_r.get("data") or [{}])[0].get("algoId")
                                if _nid:
                                    _old = trade.get("sl_algo_id")
                                    trade["sl_algo_id"] = _nid; trade["current_sl"] = _be
                                    trade["be_done"] = True
                                    if _old and _old != _nid: _cancel_okx_algo_order(inst_id, _old)
                                    save_active_trades()
                                    dc_log(f"🔒 {name} 浮盈{_gain*100:.2f}%(≥{_bep*100:.0f}%),止損移保本 {_be}")
                                    print(f"[EarlyBE] {name} {direction} 浮盈{_gain*100:.2f}%→保本{_be}", flush=True)
                except Exception as _bee:
                    print(f"[EarlyBE] {name} 保本判斷失敗: {_bee}", flush=True)

            # ── ★2026-09-14 「交易所掛好就不碰」型:BOR(固定1R)/S4H(固定2.5R)/認不出策略的接管倉 ──
            #   原本走到最下方預設固定R分支 → 浮盈 be_trigger(4H_short=1.0R)移保本、之後還 pivot 移SL
            #   → S4H 的回測規格「固定2.5R全平、不保本、不移SL」live 從沒被執行過;BOR 同理。
            #   SL/TP 都已掛在交易所,這裡只負責上方的「倉位消失→移除追蹤(+BOR熔斷計數)」。
            if trade.get("exit_strategy") in _HANDS_OFF_ES:
                continue

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
            if trade.get("exit_strategy") in ("box_trend", "hf_1r", "fourjd_2r", "s4h_fixed"):
                _es_be = trade.get("exit_strategy")
                _is_hf = _es_be == "hf_1r"      # 高頻固定1R:0.5R保本;TP@1R掛交易所自動全平
                _is_fjd = _es_be == "fourjd_2r"  # ★4J減速跌破空:0.8R保本(回測 吃滿停損51.9%→30.5%,容錯12.5→13.8)
                _is_s4h = _es_be == "s4h_fixed"  # ★2026-09-15 S4H:1.5R保本(用戶「保住本金為主」,見 S4H_BE_R)
                _be_trig = 0.5 if _is_hf else (FOURJD_BE_R if _is_fjd else (S4H_BE_R if _is_s4h else 1.0))
                # ★hf_1r拿掉保本(2026-06-18):純固定1R,TP@1R/SL@-1R掛交易所,勝率~57%(去BE驗證更高)
                # ★fourjd_2r 的保本是**回測規格的一部分**(逐根重跑驗過,不是MFE事後估算),不受 LETRUN_BE_ENABLED 影響
                _be_active = True if (_is_fjd or _is_s4h) else (False if _is_hf else LETRUN_BE_ENABLED)
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
                                    dc_log(f"🔒 {name} {'高頻達0.5R' if _is_hf else ('4J減速跌破空達' + str(FOURJD_BE_R) + 'R' if _is_fjd else ('S4H達' + str(S4H_BE_R) + 'R' if _is_s4h else '箱突破空達1R'))},止損移保本 {be_price}")
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

            # ── ★全域時間停損(2026-07-19,與OKX段對齊):超過GLOBAL_TIMESTOP_H小時未觸TP/SL→市價平 ──
            _ts_open_bx = int(trade.get("ts_open") or 0)
            if not _ts_open_bx and trade.get("tf_id") != "adopted":
                _ts_open_bx = int(trade.get("entry_ts", 0) or 0)
            _tsh_bx = _timestop_hours(trade)
            if trade.get("tf_id") == "adopted" and not trade.get("bot_verified"):
                _ts_open_bx = 0   # ★2026-08-03 未驗證來源的接管倉不套時停(手動倉保護,與OKX一致)
            if (trade.get("exit_strategy") != "cme_gap" and _ts_open_bx > 0
                    and time.time() - _ts_open_bx > _tsh_bx * 3600):
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
                            dc_log(f"⏰ BingX {bingx_symbol} 開倉滿{_tsh_bx}h未到目標,市價平倉(時間停損)")
                            active_real_trades.pop(trade_key, None); save_active_trades(); continue
                except Exception as _tse:
                    print(f"[BingX TimeStop] {trade_key} 平倉失敗: {_tse}")

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
            if _es in _HANDS_OFF_ES:      # ★2026-09-14 BOR/認不出的接管倉:交易所SL/TP已掛,不保本不移SL(對齊OKX);S4H 09-15 起改走保本段
                continue
            # 箱突破空:整倉4R TP掛在交易所,這裡只做達1R保本(一次)。TP成交自動平。
            if _es in ("box_trend", "hf_1r", "fourjd_2r", "s4h_fixed"):
                _is_hf = _es == "hf_1r"             # 高頻固定1R:0.5R保本;TP@1R掛交易所自動全平
                _is_fjd = _es == "fourjd_2r"
                _is_s4h = _es == "s4h_fixed"        # ★2026-09-15 S4H 1.5R保本(對齊OKX)
                _be_trig = 0.5 if _is_hf else (FOURJD_BE_R if _is_fjd else (S4H_BE_R if _is_s4h else 1.0))
                # ★2026-09-14 修:BingX 的 4JD 原本走 LETRUN_BE_ENABLED(=False)→**從不移保本**,
                #   但 0.8R 保本是 4JD 回測規格本體(OKX 端已是 _be_active=True),兩所對齊。
                _be_active = True if (_is_fjd or _is_s4h) else (False if _is_hf else LETRUN_BE_ENABLED)  # ★hf_1r拿掉保本(2026-06-18):純固定1R,TP@1R/SL@-1R掛交易所,勝率~57%(去BE驗證更高)
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
                                    dc_log(f"🔒 BingX {bingx_symbol} {'高頻達0.5R' if _is_hf else ('4J減速跌破空達' + str(FOURJD_BE_R) + 'R' if _is_fjd else ('S4H達' + str(S4H_BE_R) + 'R' if _is_s4h else '箱突破空達1R'))},止損移保本 {be_price}")
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
MACD_SHORT_1H_ENABLED = False    # ★2026-08-01關閉:結構性太薄,出場怎麼調都救不了。
# 用戶問「這樣的勝率R蓋得過嗎」逼出的容錯分析(容錯=實際勝率-兩平勝率,live勝率一向低回測8-10點):
#   TP2.0R/無時停 容錯10.1點(最佳,EV+0.257但最長連虧15) / TP2.5R/無 9.6點 / TP1.5R/無 9.4點
#   / TP2.5R+24h 8.8點 / TP2.5R+12h(當時現行) 8.0點(最差,EV被砍60%)
#   → **所有配置容錯都只有8~10點,live衰減直接吃光**。對照OISQ空17.8點/OISQ多13.2點=健康。
# 13天真R實證也一致:MACD -0.122R(n=17,最長連虧7)。非bug,是edge本身薄。
# 重開前置條件:進場側找到能把容錯推到≥13點的濾網(出場微調已窮盡,別再試);腳本_bt_winrate_config.py。
# ※只關1H MACD空;15m MACD空(限3幣+tFlow,未在此輪測試範圍)不受影響。
MACD_LONG_1H_ENABLED = True      # 2026-07-07重測(真4H重採樣,含現役延伸濾<=4ATR):n=54,EV+0.315,PF2.42,5/7期正
                                 # (23Q4/24Q1/24Q3/24Q4牛/25H2正,只24Q2/25H1負)——樣本不算大但一致性夠,維持開。
                                 # ★訂正:先前一度誤植成"3/7期正"與15m那版混記,已用同腳本重算確認是5/7,更正。
MACD_LONG_15M_ENABLED = False    # ★2026-08-02關閉:用新指標(容錯空間)審出零edge。
# 忠實複刻live規格(ADX>=30/4H向上/金叉/difslope/帶量1.5x/突破24根高;出場swing_tp_1h=TP1半倉@1.2R+剩半1H轉折移SL/24h時停):
#   ADX30(live現行) n=513 勝率49.1% EV**+0.004** 賺賠1.04 **容錯0.2點🔴** 連虧9 正期3/7
#   對照:ADX20舊版 n=1036 EV-0.017/容錯-1.0🔴/2期正;ADX35 n=349 EV+0.040/容錯2.5🔴/3期正;12h時停版 EV+0.022/容錯1.6🔴
#   → 每個變體的EV都在雜訊範圍(|EV|<0.05)、容錯≤2.5點(=零安全邊際)、一致性最多3/7。
# ※與2026-07-07記載「ADX30讓一致性3/7→5/7」有出入,差異來自出場建模(本次忠實模擬swing_tp_1h的TP1半倉+1H轉折移SL);
#   但兩次都同意基礎版只有3/7=弱。若日後要重開,**前置條件:進場側找到能把容錯推到≥10點的濾網**(調ADX/時停已窮盡:
#   ADX 20/30/35 × 時停12/24/無 共測5組全部🔴)。
# ★2026-07-07原註記(保留供日後優化參考):原版n=932只3/7期正(24Q2/24Q4牛明顯負)。
                                 # 逐一排查原因——①主流vs山寨分段測:兩邊問題一樣重,不是山寨拖累(假說推翻)
                                 # ②延伸濾<=2.5ATR:轉6/7期正但樣本崩到n=75、多數期2-15筆,跟RESON同風險,不敢用
                                 # ③延伸濾+ADX疊加:兩濾網互相打架,樣本剩個位數且全負,更差,已排除
                                 # ④★單獨拉高ADX門檻(現有共用突破閘只要求>=20太鬆,盤整也能過)到>=30:
                                 #   n=462(每期30-101筆,樣本足夠),EV+0.098,一致性3/7→5/7,唯24Q2/24Q4牛仍負
                                 #   但沒有更差。這是目前最好、樣本又夠大的版本,採用。
MACD_LONG_15M_ADX_MIN = 30       # 15m MACD多專屬ADX門檻(比共用_adx_trend的20嚴,只套這支不影響W底/MACD空/1H多)
C3_30M_LONG_ENABLED = False      # 2026-07-01暫關:補完Binance Vision主流資料後首測 n=82 EV-0.128(負),與舊「主流驗+0.173」矛盾,先關到查清楚
RESON_ENABLED = False            # 2026-07-01暫關:忠實複刻重測 雙底多EV-0.082(n=288)/雙頂空EV+0.010幾乎打平(n=279),原宣稱+0.062/+0.187樣本太小(n=8/15)不可信,先關
DH_SHORT_ENABLED = False         # ★2026-08-05 正式判死(非暫關)。腳本:_bt_dhshort_audit / _diag_dhshort_sl /
# _bt_dhshort_iterate2 / _bt_dhshort_iterate3 / _bt_dhshort_majors / _bt_dhshort_fee_sens。
# ①本策略的停損定義結構性失效:進場條件是2B(當根戳破96根新高後收回),當根右側缺5根故不可能是已確認pivot,
#   _find_pivot_high 找到的是較舊較低的高 → 停損中位落在進場K棒自己的上影線頂端(pos=0.97,53.4%在影線內,n=3983)。
#   後果:live忠實複刻7期,303筆訊號被「停損無效→跳過」擋到只剩17筆,其中16筆停損,EV-0.986。
# ②把停損改成插針上緣+0.5ATR救回樣本(n=580)後,掃10種出場仍無一過關:
#   麥門切線 勝27.8%/EV+0.283/容錯5.7;固定2.5R 勝36.4%/+0.161/容錯4.7;固定1.0R 勝55.9%/+0.020/容錯1.0;
#   固定0.5R 勝69.5%但EV-0.055。★沒有任何配置容錯>=8。
# ③分層補測(避免只用小幣判死):主流BTC/ETH/SOL即使ls門檻全放寬仍只有n=18(4H下跌regime閘在2023Q4-2025的主流幣
#   幾乎不成立,非ls門檻排除,主流2B訊號有1882筆/22%過ls2.5);大市值n=23-25且EV-0.23~-0.32為負。只有小山寨有樣本。
# ④小山寨最佳高勝率配置(ls>=2.0/固定1.0R)n=371/勝57.7%/EV+0.057,但停損距中位僅1.05%→成本敏感度致命:
#   往返成本0.10%→0.15%,EV+0.057→+0.009(歸零),正期5/6→2/6。小幣15m空實際滑價本就>=0.05%。
# ⑤歷史宣稱「WF驗+0.199」查無產出腳本(規則:查不到腳本=該數字不存在);同期真實存在的 _dh_short_full.txt
#   顯示17幣完整DH堆疊 訓-0.112/驗-0.189,主流/大市值/小山寨三層全負,與本次結論同向。
# 【重開前置條件(三項全達成才准重測,否則列入勿重測清單)】
#   (a) 15m取得**真實perp CVD**多幣資料(本次CVD層是taker ratio反推的代理,可能低估) ——這是唯一未被排除的翻案理由;
#   (b) 停損改用「進場K棒插針上緣+buffer」(舊pivot定義在2B型態上邏輯自相矛盾,重測不准再用);
#   (c) 拿到小山寨15m空的**實際成交滑價**統計,證明往返總成本<0.13%。
# ★注意:本策略「打敗隨機15m空基準率-0.38」為真(+0.17~0.35),訊號有料;砍它的理由是「有料但蓋不過成本」,
#   不是「沒訊號」。若未來成本結構改變(maker掛單成交/手續費等級提升),(c)可能成立。
# 原註:2026-07-01暫關:今日重測只驗證吞噬空+MACD空(忠實複刻),DH空未今日驗證,先關到驗完
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
BOX_SHORT_ENABLED = False    # ★2026-08-02暫停:容錯8.2🟡且僅4/7期正(全策略最低一致性)。
# 大支撐閘(0802上線)尚未在live驗證過。重開條件:前5支驗證完 + 支撐閘實際擋單效果確認。原註:# 2026-07-01忠實複刻重測轉正式:7期間n=2228,EV+0.124,23Q4~24Q1負(早期)、24Q2起連續5期同號正(18個月),非雜訊,開啟
# 15m 維加斯大通道 fade 做空(2026-06-13,WF驗+0.182/MDD16%/各年不虧)
def _multitouch_support_dist(inst_id: str, entry_px: float, tol: float = 0.015,
                             pv: int = 2, need: int = 2, want_bars: int = 1920):
    """★2026-08-01 大支撐距離(用戶指控「一直空在4H/日線大支撐上方」的驗證版實作)。
    定義=近20天15m擺動低(前後各2根,已確認)群聚:彼此<1.5%且≥2次觸及=「被測試過的大支撐」,
    取進場價下方最近的群心,回傳 (進場價-支撐)/進場價。抓不到足夠資料→回None(呼叫端放行不擋)。
    OKX單次上限300根→用 history-candles 的 after 分頁往回抓(只在訊號已通過其他濾網時才呼叫,頻率低)。
    ★回測依據(_bt_box_decisive衍生,7期n=1875):此定義下「支撐上方1.5~12%」的箱突破空
      EV=-0.186/勝率35%(對照其他+0.099/47%)→擋掉後EV+0.132→+0.154,只砍7%訊號、總利潤+22R。
      ※註:改用4H擺動低的單次API版本被測掉(擋到的681筆EV仍+0.080=擋掉好單,總利潤反而少),故採本版。"""
    try:
        lows = []; after = None
        for _ in range(8):                        # 8×300=2400根上限
            q = {"instId": inst_id, "bar": "15m", "limit": "300"}
            if after: q["after"] = after
            rows = _fetch_okx_public_data("/api/v5/market/history-candles", q)
            if not rows: break
            for r in rows:
                lows.append((int(r[0]), float(r[3])))   # ts, low
            after = str(rows[-1][0])
            if len(lows) >= want_bars: break
        if len(lows) < 400: return None
        lows.sort(key=lambda x: x[0])
        arr = np.array([v for _, v in lows][-want_bars:])
        sw = [arr[j] for j in range(pv, len(arr)-pv) if arr[j] == arr[j-pv:j+pv+1].min()]
        sw = [v for v in sw if v < entry_px]
        if len(sw) < need: return None
        for v in sorted(sw, reverse=True):
            grp = [x for x in sw if abs(x-v)/v <= tol]
            if len(grp) >= need:
                return (entry_px - float(np.mean(grp))) / entry_px
        return None
    except Exception as e:
        print(f"[大支撐] {inst_id} 計算失敗(放行): {e}")
        return None

BOX_SUPPORT_GATE = True   # 箱突破空:大支撐危險區(上方1.5~12%)擋空。見_multitouch_support_dist註解回測依據。
BOX_DECISIVE_ATR = 0.15  # ★2026-07-19 果斷破底margin(用戶指正:現行cl<bl接受任意幅度收破→6筆實單3筆假突破,
# 破底幅0.05~0.59%多為noise)。用戶定義=針尖(wick極值)定箱✓+實體要突破+很明顯。回測_bt_box_decisive.py 7期:
# margin 0→0.15 EV+0.124→+0.132、維持5/7正、砍16%最爛noise單;0.3更肥但掉4/7。取0.15=更好EV+同一致性+少churn。

def _check_box_short(symbol_item: str, okx_bar_fmt: str, df: pd.DataFrame) -> Tuple[bool, str]:
    """箱突破做空：96根窄箱(range<8%)收盤果斷跌破箱底(>0.15ATR) + 帶量1.5x + CVD↓ + OI升(3根)。只用現成資料。"""
    try:
        hi = df["high"].values; lo = df["low"].values; cl = df["close"].values
        vol = df["vol"].values if "vol" in df.columns else None
        if len(cl) < 100 or vol is None: return False, ""
        # ATR(14) 供果斷破底門檻用
        _tr = np.maximum(hi[1:]-lo[1:], np.maximum(np.abs(hi[1:]-cl[:-1]), np.abs(lo[1:]-cl[:-1])))
        _atr = float(pd.Series(_tr).ewm(alpha=1/14, adjust=False).mean().iloc[-1]) if len(_tr) >= 15 else 0.0
        bh = hi[-97:-1].max(); bl = lo[-97:-1].min()
        if bl <= 0 or (bh-bl)/bl > 0.08: return False, ""        # 箱要夠窄=盤整
        # ★收盤要「果斷」跌破箱底針尖(實體破,幅度>0.15ATR),不接受針尖被戳一下的marginal假突破
        if not (cl[-1] < bl - BOX_DECISIVE_ATR*_atr and cl[-2] >= bl): return False, ""
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
        # ★2026-08-01 大支撐危險區閘(用戶反覆指出「一直空在4H/日線大支撐上方會虧」,7期回測證實對箱突破空成立):
        #   放在所有濾網之後才算(分頁抓K線較貴,此時訊號已極少)。抓不到資料→放行不擋。
        if BOX_SUPPORT_GATE:
            _g = _multitouch_support_dist(OKX_SWAP.get(symbol_item, symbol_item), float(cl[-1]))
            if _g is not None and 0.015 < _g <= 0.12:
                print(f"[箱突破-大支撐] {symbol_item} 收盤在多次測試支撐上方 {_g*100:.1f}%(危險區),擋空")
                return False, ""
        return True, "破窄箱底+帶量+CVD↓+OI升"
    except Exception as e:
        print(f"[Box-Short] {symbol_item} 失敗: {e}")
        return False, ""


# ★★★2026-09-16 深夜 出場改版(用戶:「不降低單量的情況 增加勝率」+「停損率高那就是進場不對」)
# 先驗用戶那句話:對 1650 筆**吃滿停損**的單算「停損前最大有利幅度(MFE)」——
#   吞噬空 2026:先賺過>0.5R 才被打掉的佔 **53%**、從沒賺過(<0.2R)只有 23%,
#   而且**被停損後 48h 內價格照樣走到原 1R 目標的佔 44%** → 主因是**停損太緊被影線掃**,不是進場看錯方向。
#   (4JD 相反:掃損後只有 15% 會回到 1R、停損後中位 −0.11R,它才是比較接近「進場不對」。)
# 所以解法是**放寬停損 + 縮短停利**:n 一筆不砍,把「先賺過又被打回」那群轉成贏單。
# 掃描 停損×(1.0~2.2) × 停利(1~3R) × 保本(無/0.5/0.8/1/1.5R) 共124組,**只用訓練段選、用勝率選**,
# 且把 live 的 MAX_SL=12% 封頂模擬進去(腳本 _bt_exit_capped.py / _an_exit_sweep.py):
#   吞噬空 現行讓跑 訓+1.096/勝62% 驗+0.699/47% 新幣+0.532/49% **2026 −0.119/勝36%**
#          SL×2.2/TP1R 訓+0.536/**勝78%** 驗+0.239/**63%** 新幣+0.149/**59%** **2026 +0.063/勝54%/總R+10.6**
#   → 四層勝率全升、2026 由負轉正,撞 MAX_SL 上限的只有 8%。
# ★代價講明:放棄長尾(舊期別總R 砍半,最大一筆曾有 +13R)。這是「勝率 vs 總R」的取捨,用戶要勝率。
# ★停利 1R 不加保本(用戶規則),故 exit_strategy 走 engulf_1r ∈ _HANDS_OFF_ES。
ENGULF_SL_MULT = 2.2     # 停損距離 = 原規格(近4根高+0.15ATR)距離 × 2.2
ENGULF_TP_R    = 1.0     # 停利 1R 全平

ENGULF_MIN_BODY = 0.70   # ★★★2026-09-06 進場品質閘:吞噬K實體佔全棒幅比例 ≥70%
# ★2026-09-15 訂正:下表的 EV/勝率/吃滿停損是**固定2R出場**算的(今天同規則重跑 fix2:實體≥0.70 n=373 +0.525,各層對得上)。
#   live 出場 09-05 起是 swing_full,同一批進場 = n=373 EV+0.740(訓+0.284 驗+0.933 新+0.444 22+1.096)。
#   2026 真樣本外(swing_full):63幣 −0.006 / live幣池 −0.21(_bt_holdout_livepool.py)。腳本 _chk_engulf_gap.py。
# ★用戶定調(這句是轉折):「重點就不是改我的風險或熔斷,**是你的勝率,進場不對就是輸**」
#   我當天先花時間在風控層(位階閘/回撤深度閘/1R保本/熔斷)——**全部失敗或幫倒忙**:
#     位階≥50%閘     訓練+0.234 → 驗證 −0.044   (選樣偏差,砍84%樣本)
#     回撤深度0.3-0.5 訓練+0.548 → 驗證 −0.020   (同上)
#     1R保本(逐根)    EV+0.219 → **+0.177**、總R +289→+233 (救回輸單但把贏單提早掃掉;
#                    ★我一度用MFE近似算成 +0.403/容錯14.7,是**高估**,逐根重跑才抓到)
#     ★最長連虧在每一種**出場**改法下都是 **51**,完全不動 → 那不是出場問題。
# ★換成**進場**條件(實體佔比)後,四個門檻×四個驗收層**全部單調**:
#   ┌ 實體≥ ┬ 樣本 ┬ 訓練 ┬ 驗證 ┬ 新幣 ┬ 2022 ┬ 合計EV ┬ 勝率 ┬ 吃滿停損 ┬ 連虧 ┬ 單期最差回撤 ┐
#   │ 無    │100% │+0.328│+0.106│+0.141│+0.323│ +0.219 │ 43% │  57.0%  │ 51  │  −42.9R    │
#   │ 0.5   │ 62% │+0.474│+0.163│+0.281│+0.343│ +0.298 │ 45% │  54.1%  │ 47  │  −35.5R    │
#   │ 0.6   │ 46% │+0.449│+0.193│+0.246│+0.509│ +0.339 │ 47% │  53.2%  │ 45  │  −32.2R    │
#   │★0.7  │ 28% │+0.471│+0.431│+0.388│+0.750│ +0.514 │ 53% │  47.0%  │ 27  │  **−19.0R**│
#   └───────┴─────┴──────┴──────┴──────┴──────┴────────┴─────┴─────────┴─────┴────────────┘
#   ★單期最差回撤 −42.9R → −19.0R:這是我改風控/出場/保本都動不了的數字,換進場條件砍一半。
#   ★不是選樣偏差:偏差的特徵是「訓練漂亮、驗證死」(當天已踩兩次),這個是**四層一起變好+四門檻單調**。
# 驗收:11/12期正(唯一負的22H1 n=16,低於n<20線)｜成本0.30%仍 EV+0.430/容錯14.6
#   去尾1% +0.515、去尾5% +0.521(≈平均+0.514,**尾部完全不依賴**)｜block bootstrap CI[+0.314,+0.720]
#   同回撤預算下可放大2.26x → 等效總 +432R vs 無門檻 +289R(**+50%**)｜頻率 0.27筆/天
#   帳戶換算:風險1%/筆 → 單期最差 = 帳戶 **−19.0%**(無門檻是 −42.9%)
# ★不是波動代理:停損距只從2.06%→2.71%(+31%),EV卻 +0.088→+0.514(6倍)。
# ★已測**不要加**:斐波∩支撐壓力區匯流加權(疊在實體條件上樣本外只 +1.0%,且傷訓練段
#   +0.4713→+0.2938)——它跟實體佔比抓同一件事,實體更直接,少一個活動零件。
ENGULF_SHORT_ENABLED = True   # ★2026-09-06 重開(加進場品質閘,證據見上表)
# ★漏斗儀表(2026-09-06):S4H/V-Long/4J 都有,吞噬空原本沒有。新加的實體閘若擋過頭,
#   Discord 什麼都不會說(記憶教訓:「訊號默默消失時 log 是唯一現形處」)→ 補上逐層計數。
_ENGULF_DIAG = {"呼叫":0, "K棒不足":0, "非下跌regime":0, "不在近12根高":0,
                "非吞噬":0, "量不足":0, "實體不足":0, "成立":0}
# ── 以下為 2026-09-05 關閉時的原因,保留當歷史:當時只有「無門檻」版,回撤確實不可行 ──
# 原註:★★★2026-09-05 當天上線後**又關掉**——因為我漏測了最重要的東西:回撤。
# 用戶:「保住本金為主,我不要因為你這樣導致資金大回撤,尤其都會比你說的回撤還要多,
#        就像之前所有上線的策略,永遠讓我的資金越來越少」→ 補測後證實他是對的:
#   ┌ 版本 ─────────────┬ EV ────┬ 容錯 ┬ **最大回撤** ┬ **最長連虧** ┬ 吃滿停損 ┐
#   │ 固定2R(原本)        │ +0.224 │  7.5 │ **−82.7R**   │ **46筆**     │ 57.0%    │
#   │ swing_full(我推的)  │ +0.350 │ 10.7 │ **−87.6R**   │ **61筆**     │ 59.5%    │
#   │ swing_full+1R保本   │ +0.336 │ 11.0 │   −69.7R     │   61筆       │ 63.7%    │
#   └────────────────────┴────────┴──────┴──────────────┴──────────────┴──────────┘
#   單期最差回撤:swing_full −45.4R(25H2,連虧44筆) / 固定2R −43.8R(連虧42筆)。
# ★換算到 30U 帳戶:就算風險降到 **1%**,單期 −45.4R = **−45% 帳戶**;2% = −90%;現行10% = 死好幾次。
# ★★方法論教訓(比這個策略本身重要):我全程只報 EV/容錯/正期,**從沒報過最大回撤與最長連虧**,
#   還拿「容錯」當穩健度在講——**容錯不是回撤**。用戶說「回撤都比你說的多」,我沒有反駁的立場,
#   因為我根本沒說過回撤。★**任何策略上線前必須報:最大回撤(R)、最長連虧筆數、吃滿停損%,
#   並換算成「以用戶當下資金與風險%,這個回撤等於帳戶的百分之幾」。**
# ★注意:固定2R 版的回撤幾乎一樣差(−82.7R/46筆) → 這不是 swing_full 造成的,
#   是**這個策略本身**的性質。而且很可能**其他已上線策略也有同樣未測的回撤**,待逐一補測。
# 原重開理由與驗收數字(仍有效,只是回撤讓它在當前資金規模不可行):
#   唯一 live 沒衰減(live真R+0.130≈回測+0.106)、12期全正、2.12筆/天;
#   換 swing_full 後 容錯7.5→10.7、成本0.25%後+0.153→+0.278、四層皆正、重疊率vs S4H 0.9%。
#   ★資金規模夠大(能承受單期 −45R)時可重新考慮;或先找到能把回撤壓到 −15R 以內的改法。
# 原註(2026-08-02暫停時):容錯僅4.5🔴(全策略最薄),且它一支佔live訊號量44%
# 重開理由(用戶要空單:「BTC一個回檔一堆打停損,不見任何空的訊號」;實測現役只剩3個空策略且都不抓順勢陰跌):
#   它是**唯一 live 沒衰減**的策略(live真R+0.130 ≈ 回測+0.106),12期全正、訓驗同號、2.12筆/天,
#   當初停用的兩個理由(容錯4.5太薄、佔訊號量44%手續費貴)靠**換出場**同時解決,且**一筆訊號都沒砍**。
# ★關鍵:改善來自「同一批單換出場」,不是「挑掉一批單」——換出場不涉及選擇,
#   所以訓驗差異不可能是 cherry-picking(對照:量門檻2x 也能到容錯10.7,但砍掉58%樣本且訓練段掉到4.9,不採用)。
# 忠實驗收(_bt_engulf_liveexit.py:**逐根重放 live 既有 _swing_trail_update_sl 機制**,
#   非我自己另寫一套 —— PV=2 pivot + 0.4ATR緩衝 + 浮盈1R才啟動 + 只往有利方向 + 合法側):
#   n=1335 EV+0.224→**+0.350** 容錯 7.5→**10.7** 正期9/12 成本0.25%後 +0.153→**+0.278**
#   四層皆正:訓+7.0 / 驗+8.1 / 未測新幣+8.5 / 2022外樣本+15.9   重疊率 vs S4H做空 = **0.9%**
#   鄰域9格全過(PV 2/3/4、ATR緩衝 0~0.6、浮盈門檻1R/1.5R,容錯9.6~11.1)=平台不是孤峰。
#   ★全部用 live 現行預設,不動任何共用參數(浮盈門檻改1.5R更好=容錯11.1/正期11/12,
#     但那是 OISQ/MACD空 共用的,不為單一策略動它)。
# ★不設時間停損(_strat_ts_h=-1):回測就是不設;swing_full 預設會吃到讓跑型24h時停,那不是我驗過的規格。
# 原註(2026-08-02暫停時):容錯僅4.5🔴(全策略最薄),且它一支佔live訊號量44%
# (3.38張/天)=手續費主要來源。/打架(GPT)也點名:「執行單純所以live不衰減」的豁免理由不成立(吞噬依賴收盤確認,
# 最怕收盤延遲/跳空),應是首批停用而非例外。★但它live真R+0.130確實≈回測+0.106(唯一不衰減的策略),
# 重開條件:前5支驗證完(live勝率回到45-50%)後,單獨放它回來再觀察20筆。原註:# 山寨看跌吞噬空(1H,2026-06-24 WF):放量吞噬+價<EMA100下跌regime。★2026-07-01忠實複刻重測(真main.py邏輯/7個不重疊期間23Q4~25H2/n=550):EV+0.176,7/7期全正,PF1.29,轉正式(拿掉觀察標籤)。限非主流山寨,純價量不碰OI/CVD,固定2R,SL近高。
ENGULF_MIN_LIQ = 100_000.0   # ★2026-09-11 近96根1H USDT成交額中位下限 —— **補回 0906 回測規格裡本來就有、live 漏抄的閘**

# ★★★2026-09-16 深夜 大盤波動閘(用戶:「再搞搞 不然空單這樣行嗎」)──────────────────────────
# 起因:吞噬空在 2026(到09-15,live幣池97) 每筆 −0.119/勝36%,而且**比配對隨機還差**
#   (超額 −0.241,按天 block bootstrap P(超額≥0)=3.5%)→ 不是大環境逆風,是進場在那個環境下反轉。
# 先排除的方向(都做過同等力度、都沒過):
#   ①進場K品質閘(7特徵×6門檻×2方向 + 兩兩組合):訓練段選到的在 2026 全失效。
#   ★真因診斷:**2026 與舊期別的進場K品質分布幾乎一模一樣**(上影0.10/0.09、實體0.78/0.80、
#     量倍1.82/1.89、位階28.8/27.5、ATR1.27/1.23、前24h漲幅−0.58/−0.86)→ 訊號沒變差,
#     所以加進場閘本來就救不了。變的是「在什麼環境下觸發」。
# 這一刀 = BTC 近96根15m 的 (high−low) 平均 ÷ 現價,即大盤 24h 平均真實區間%。
#   機制:大盤在恐慌高波動時做空 = 空在殺盤尾段,被反彈打掉。跟用戶「不要空在大支撐」同一件事。
# 驗收(只用訓練段22H1/22H2/23H1選門檻,2026完全不參與挑選;腳本 _chk_btcvol.py):
#   吞噬空 訓練 +1.096→**+1.794**(勝62→71%) 驗證 +0.699→**+1.172** 新幣 +0.532→**+0.730**
#          **2026 −0.119→+0.050**(勝36→38%) 留存 50~68%;**逐期 9/9 全正**
# ★一般性檢查(關鍵,避免曲線擬合):同一刀套到另外三支空單 —— S4H 2026 +0.372→**+0.81**(同向),
#   4JD −0.216→−0.167(仍負,不套)、BOR −0.02→−0.12(**變差,不套**)。
#   同時淘汰了「BTC 7天跌幅」那半:全體空單單獨看是**反向**的(≥−2% −0.048 vs <−2% +0.009)=曲線擬合。
# ★2026 閘後 CI[−0.271,+0.460]、P(EV>0)=58% —— 只到「從明確虧損變成打平」,不是證明會賺,照實記。
ENGULF_BTCVOL_GATE = True
ENGULF_BTCVOL_MAX  = 0.30    # BTC 近96根15m 平均(high−low)÷現價 的上限%
_BTCVOL_CACHE: Dict[int, float] = {}


def _btc_atr24_pct() -> float:
    """BTC 近96根已收盤15m 的 (high−low) 平均 ÷ 最新收盤 ×100。抓不到回 nan(呼叫端放行,同其他閘)。"""
    key = int(time.time() // 300)
    if key in _BTCVOL_CACHE: return _BTCVOL_CACHE[key]
    v = float("nan")
    try:
        b = fetch_market_candles("BTC-USDT-SWAP", "15m", 300)
        if b is not None and len(b) >= 96:
            h = b["high"].values[-96:].astype(float); l = b["low"].values[-96:].astype(float)
            c = float(b["close"].values[-1])
            if c > 0: v = float(np.mean(h - l) / c * 100)
    except Exception as ex:
        print(f"[BTC波動] 失敗(放行): {ex}", flush=True)
    if len(_BTCVOL_CACHE) > 32: _BTCVOL_CACHE.clear()
    _BTCVOL_CACHE[key] = v
    return v
# 證據(_sim_engulf_exits.py + 濾網拆解,2026 live幣池重放 A窗05-01~09-11 / B窗01-10~03-10,固定2R):
#   無此閘(=live) n=436 EV−0.049 / 有此閘 n=227 EV+0.081(A+0.038 B+0.159)
#   **被擋掉的 n=209 EV−0.190 CI[−0.362,−0.005]**,兩窗皆負。
#   ★先前用 vol×close 算會得到「兩窗方向相反」—— vol 是合約張數,那是單位錯。一律用 volCcyQuote。
#   ★誠實記錄:補閘後剩下的 +0.081 CI 仍跨0;幣安老幣同邏輯 5~9月為 −0.094。此閘只證明「擋掉的是爛單」,
#     不證明「剩下的在2026有edge」。
def _check_engulf_short(symbol_item: str, df: pd.DataFrame) -> Tuple[bool, str]:
    """山寨看跌吞噬空(1H):①陰線吞噬前陽線實體 ②量>1.3×近24均量 ③收盤<EMA100(下跌regime) ④在近12根高附近(空頂部)。
    純價量,3個獨立時期樣本外複製超額vs隨機+0.15。注意:df已去掉未收盤當根,[-1]=最新已收盤。"""
    try:
        _ENGULF_DIAG["呼叫"] += 1
        op = df["open"].values; hi = df["high"].values; cl = df["close"].values
        lo = df["low"].values
        vol = df["vol"].values if "vol" in df.columns else None
        if vol is None or len(cl) < 130:
            _ENGULF_DIAG["K棒不足"] += 1; return False, ""
        ema100 = pd.Series(cl).ewm(span=100, adjust=False).mean().values
        if not (cl[-1] < ema100[-1]):
            _ENGULF_DIAG["非下跌regime"] += 1; return False, ""                     # 下跌regime
        if not (hi[-1] >= hi[-13:-1].max() * 0.997):
            _ENGULF_DIAG["不在近12根高"] += 1; return False, ""                      # 在近12根高附近(頂)
        if not ((cl[-1] < op[-1]) and (cl[-2] > op[-2]) and (op[-1] >= cl[-2]) and (cl[-1] <= op[-2])):
            _ENGULF_DIAG["非吞噬"] += 1; return False, ""                            # 看跌吞噬(陰吞前陽實體)
        va = float(np.mean(vol[-25:-1]))
        if not (va > 0 and vol[-1] > 1.3 * va):
            _ENGULF_DIAG["量不足"] += 1; return False, ""                            # 放量
        # ★流動性閘(USDT尺度,補回回測規格):近96根已收盤1H的 USDT 成交額中位 ≥ ENGULF_MIN_LIQ
        if "volCcyQuote" in df.columns:
            if float(np.median(df["volCcyQuote"].values[-96:])) < ENGULF_MIN_LIQ:
                _ENGULF_DIAG["流動性"] = _ENGULF_DIAG.get("流動性", 0) + 1; return False, ""
        # ★★進場品質閘(2026-09-06):吞噬K的**實體佔全棒幅比例** ≥ ENGULF_MIN_BODY
        #   用戶:「重點就不是改我的風險或熔斷,是你的勝率,進場不對就是輸」——他是對的。
        #   實體大 = 那根陰線是實打實收下來的,不是上下影線一堆的假動作。
        _rng = float(hi[-1]) - float(lo[-1])
        if _rng <= 0:
            _ENGULF_DIAG["實體不足"] += 1; return False, ""
        _body = abs(float(cl[-1]) - float(op[-1])) / _rng
        if _body < ENGULF_MIN_BODY:
            _ENGULF_DIAG["實體不足"] += 1; return False, ""
        # ★★2026-09-16 大盤波動閘(說明見 ENGULF_BTCVOL_GATE 常數區):恐慌高波動時不空
        _bv = float("nan")
        if ENGULF_BTCVOL_GATE:
            _bv = _btc_atr24_pct()
            if _bv == _bv and _bv > ENGULF_BTCVOL_MAX:
                _ENGULF_DIAG["大盤波動擋"] = _ENGULF_DIAG.get("大盤波動擋", 0) + 1
                print(f"[Engulf-Short] {symbol_item} 擋:BTC 24h平均區間 {_bv:.2f}%"
                      f"(>{ENGULF_BTCVOL_MAX}%)=恐慌殺盤,空單易被反彈打掉", flush=True)
                return False, ""
        _ENGULF_DIAG["成立"] += 1
        _bvt = f"+大盤波動{_bv:.2f}%" if _bv == _bv else ""
        return True, f"看跌吞噬+放量+價<EMA100+實體{_body:.0%}{_bvt}"
    except Exception as e:
        print(f"[Engulf-Short] {symbol_item} 失敗: {e}")
        return False, ""


# ── ★V成型吸收做多(15m,2026-08-31上線) ───────────────────────────────────────
# 用戶定調:「做多不會是在跌的時候進。價格低點墊高的過程叫V,在**吸收的那個V成型時**進場。」
# 官方原文對得上:「多單止損之後,賣單往下賣但賣不下去,等於被吸收了,然後反轉」= V轉。
# 規格(腳本 _bt_vlong.py,12期雙驗收):
#   15m｜ZigZag 擺動 5%/5.5%/6% 任一成立(參數取聯集,不卡死單一值)｜兩個低點間隔 ≤2h~4h
#   V成型 = 第二個低點 > 第一個低點；吸收 = 合約CVD 第二個低點 < 第一個低點
#   停損 = 第二個低點(吸收低點)×0.999｜TP 2.5R
# 驗收:聯集 n=983 EV+0.517 容錯15.5 10/12期正;六個單格容錯17.9~24.6(每格獨立達標)
#   未測新幣 +0.359｜2022真獨立外樣本 +0.385｜隨機同方向做多基準 -0.077 → **超額 +0.879**
#   成本0.10/0.15/0.20% → 容錯24.0/23.7/23.3(不敏感)｜頻率≈2筆/天｜持有中位6.2h
#   重疊率 vs 現役1H MACD多 = **0.0%**(全新獨立訊號源,不需去重)｜強制平倉僅5%
# ★已測過且**有害**,不要再加:日線EMA200/4H EMA200 regime閘(新幣驗收 +0.359→+0.02~-0.058)、
#   位置層疊加、加分項越多、麥門切線/分批保本出場(皆不如固定TP)。
# ★★已知弱點:**22H2深熊(LUNA/FTX)EV -0.583(n=37)** —— 做多在深熊會賠。
#   不在策略層加regime(實測有害),改依賴既有 DAILY_STOP_ENABLED 每日熔斷。
#   ★重開/關閉條件:若連續兩個月實單EV<0 或 觸發每日熔斷≥3次 → 關閉並回頭查是否進入深熊。
VLONG_ENABLED = True    # ★2026-08-31 上線(12期雙驗收全過,詳見上方註解)
VLONG_SWINGS = (0.05, 0.055, 0.06)   # ZigZag 擺動門檻(任一成立即可)
VLONG_MAX_GAP = 16                    # 兩個低點最大間隔(根15m) = 4h
VLONG_MIN_LIQ = 0.0                   # ★2026-09-04 當天稍晚**撤回**(用戶:「看他們做的是怎樣 就依照那條件」)
#   撤回理由:這道閘是**我自己**從回測百分位推的,**不是官方規則**。實查官方行為:
#   當晚 91 筆數據異動警報裡,噴最兇的 APR(+47.1%)/CHIP(+15.9%)/DGAI(+14.7%) 的 15m 成交額
#   只有 44K/11K/7K;白總影片示範自己交易的 CAP,15m 成交額也才約 37K → **他們照做**。
#   而且當晚實測「噴≥5%」的比例在每個流動性桶都是 12~14%(<50K 13% / 50-100K 14% /
#   100-500K 14% / >500K 12%),**低流動性並沒有比較差**,<50K 那桶勝率反而最高(46%)。
#   原本裝閘的依據只是「回測 <100K 樣本 n=16 未驗證」——那是「沒測過」,不是「證明差」。
#   設 0 = 停用;若日後要重開,需先補足低流動性樣本的回測證據,不可再憑百分位拍門檻。
_VLONG_MIN_LIQ_OLD = 100_000.0        # (舊值留存)
#   用戶回報 CIEN(43K/根)、BSB(40K/根) 這種流動性也在下單。實測回測樣本的成交額分布:
#   p1=77K / p5=200K / p50=3.28M → **CIEN 落在第0.2百分位**,比回測中位低76倍。
#   分桶EV:<100K n=16(未驗證) / 100-200K +1.190 / 200K-1M +0.684 / 1-5M +0.740 / >5M +0.098。
#   加 >=100K 閘:保留98%樣本、EV +0.519→+0.515(幾乎零成本),但擋掉完全沒被驗證過的極低流動性。
#   ★注意 >5M 那桶 EV 只有 +0.098 —— edge 在中小流動性,不是越大越好,故不設上限。
VLONG_MIN_UP  = 0.02                  # ★2026-09-04 修:低點墊高**至少2%**(對齊回測 _bt_vlong.run(min_up=0.02))
#   上線時漏了這道閘 → live 只檢查 p2>p1。實測 5 張訊號卡有 **3 張(60%)** 墊高<2%
#   (CAP 1.34%×2、CIEN 1.61%),都是回測規格會拒絕的單 → live 跑的不是被驗證過的策略。
# ★★2026-09-05 反追漲閘(用戶實盤觀察:「要嘛進在很高的位置」;9/4 六筆V成型全停損,
#   進場位階中位89%、每筆都在該幣已噴+10~47%(24h)之後才進)。
#   門檻只用**訓練段TR**選、凍結後才看其他層(_bt_vlong_gate_oos.py),四層全部改善:
#   全期 +0.508/14.5→+0.778/22.3、驗證 22.1→26.0、新幣 9.5→16.2、2022 13.3→18.1;保留樣本51%。
VLONG_MAX_POS_PCT   = 70.0   # ★主閘:進場價在前24h區間的百分位上限。>70%=買在區間上緣→不進。
# ★★2026-09-05 用戶指正兩點,兩點都被數據證實,故把「漲幅閘」改回關閉、只留位階閘:
#   ①「追漲不是說不行,你要追對位置」→ 在 位階≤70% 之下追漲**是可以的**:
#       漲幅≤0 +0.778/22.3 | 漲0~10% +0.348/9.9 | 漲10~25% +0.508/14.5
#       (漲>25% 是 −0.150 但 n=20 <25,依自訂規則只算線索,不拿來設閘)
#   ②「擋掉這些,他噴出去你又能進到嗎」→ 會漏。漲幅閘擋掉的485筆 EV 仍有 +0.230、
#       貢獻 +112R,其中 **173 筆是 R≥2 大贏單 = 全部大贏單的 40%**。
#   真正有分辨力的變數是**位階**不是漲幅。兩者對照(全12期):
#       漲幅≤0+位階≤70: 留51% EV+0.778 容錯22.3 總R+387 大贏單保住60% 正期9/12 驗證2/3
#       **只用位階≤70**: 留63% EV+0.686 容錯19.6 **總R+424 大贏單保住70% 正期10/12 驗證3/3**
#   ★換掉的依據**只用訓練段TR**即成立(總R +158 vs +130、大贏單 79% vs 60%、容錯26.4仍遠超門檻),
#     沒有偷看留出層(避免 0828「用全期挑配置」的錯)。
#   ★9/4 實際那6筆有5筆位階在74~93%,光位階閘就擋掉(只有TRIA位階51%會放行)。
VLONG_MAX_RUNUP_PCT = 999.0  # 漲幅閘=關閉(999)。保留變數與log輸出供觀察,不參與擋單。

# ★★2026-09-18 新增「近7天漲幅閘」(與上面的 24h `_chg24` 是**不同的軸**,24h 那道已測過被位階閘取代)
#   起因:用戶「要嘛大跌才在空 要嘛漲很多了才在多」「你進場不對 就算停損給你拉10%也是損」。
#   診斷(_an_zz_2026.py):2026 的 V成型訊號與舊期別**特徵分布完全不同**——
#     近7天漲 舊期別中位 −4.0% vs 2026 **+16.4%**;對BTC強度 −2.3% vs **+12.0%**;頻率 0.34→2.58筆/天(7.6倍)。
#     ＝2026 的訊號是在「已經噴完」的地方發的,這就是 live 追高的機械根源。
#   忠實 live 規格(TP2.5R+1.5R保本+位階≤70)實測(_bt_vlong_7d.py,四層):
#     無閘      訓+0.315/容錯9.8 驗+0.751/23.7 新幣+0.596/18.2 **2026 −0.012/−0.4 總R−6.5**
#     **<0%**   訓+0.312/9.8    驗+0.909/28.7 新幣+0.809/24.8 **2026 +0.030/+0.9 總R+7.6**
#     <−10%    訓+0.388/12.2   驗+1.042/32.4 新幣+0.838/25.7  2026 +0.078/+2.4 總R+16.0
#   ★選 0% 而不是 −10%:0 不是掃出來的參數(滾動前推每季重選門檻從沒選到 −10,選 −20/−30),
#     不掛參數風險;且用戶 13 筆真實進場有 **12 筆** 近7天漲<0(唯一例外 ZEC +6.99%)。
#   ★代價(照 0905 用戶教訓,容錯與總R一起看):驗證段總R **+266.5→+177.3(−33%)**、
#     R≥2 大贏單只保住 **61%**、2026 留樣率 45%。這與 24h 漲幅閘被否決的理由同類,
#     但差別是:24h 那道**四層沒有全改善**,這道是四層 EV/容錯全改善且把 2026 從負轉正。
#   ★驗收:逐根重放 177/177=100%、live 300根視窗 100% 重現(7天值改用 **1H K線** 取得,見 _vlong_7d_runup);
#     滾動前推(門檻選擇計入樣本外)n=327 EV+0.539 勝52%;按日 block bootstrap 全期 CI[+0.082,+0.699] P=99.4%,
#     **2026 CI[−0.064,+0.374] P=91.7%(跨0,不顯著)**;成本 0.10→0.25% 只從 +0.430 掉到 +0.399。
#   ★已知弱點(寫在這裡供事後對帳):它是**事件型**策略——去掉最好3天 EV +0.430→+0.142、去5天 +0.071。
VLONG_MAX_7D_RUNUP = 0.0     # 近7天(168根1H)漲幅 > 此值 → 擋。取不到值=未知→放行(寧可漏擋不誤擋)。

# ★★★2026-09-16 群聚閘 + BTC 反彈閘(用戶:「把2V弄到可以用為止」)──────────────────────────
# 2026 回測 V成型 −0.23R/勝24%,一路追到底才找到原因:**V成型是「全市場爆倉後集體反彈」的事件策略**。
#   全部 3252 筆(原12期 + 2025/2026 幣安全市場 + 已下架幣)前5天貢獻 100% 總R(2025-02-03 +550R、2025-10-10 +450R);
#   平常日零星的 V 勝率 31%、每筆≈0R —— 17 種出場、OI三種量法、秋總錨點、1H/4H/日線支撐、V尺寸、擴大幣池全部救不起來。
# 即時判斷(只用進場當下以前):
#   ①群聚:前 4h 內掃描池裡「別的幣」也出過 V成型(過完其他閘)的數量,換算到 163 幣池 ≥ 3
#      原12期 +1.07/勝76%、2025補的900多個幣(含已下架) +0.97~+1.62,<3 個 ≈0
#   ②BTC 已從 24h 最低點反彈 ≥3%(崩盤殺完了,不是還在殺)—— 崩盤中途出現的 V 會被下一段再殺(2024-04-12/08-05/12-09)
#   合併驗收(含 8 次從沒參與挑選的崩盤月 _bt_crash_events.py):n=1418 每筆+1.03R 勝73% 吃滿停損24%
#     事件日 22 天 賺17 虧5,拿掉最賺2天仍 +0.71R;群聚但 BTC 未反彈 n=387 +0.00R
#     反彈門檻 2/3/4/5% = +0.83/+1.03/+0.93/+0.89(平台,不是單點);腳本 _chk_crowd_btc.py
# ★代價(照實):平常日幾乎不出單,崩盤反彈時一次出很多;2026 前9個月只有 08-22 一次群聚,且BTC未反彈→會被擋。
# ★被群聚/BTC閘擋掉的 V 照樣記進群聚名單(回測的群聚就是算所有過完其他閘的 V)。
#
# ★★★2026-09-16 深夜 訂正:群聚從「硬擋」改成「加碼」,BTC 門檻 3%→1%,另加選幣層(下方 VLONG_ANOM_*)
#   起因:拉了數據獵手後台 121 筆真實訊號獨立驗證(_chk_dhx_verify.py)—— 他們 8~9 月做多 +0.19~+0.28R/筆,
#   我方同期 433 筆進場卡與他們 118 筆訊號「同幣同向±24h」只重疊 1/118,而他們賺的正是「底背離吸收做多」
#   (n=26 +0.388 勝73%)=**跟我們 VLONG 同一套策略**。所以問題不是沒有這個策略,是我們把它閘到不出單。
#   量化(腳本 _cmp_final_gates.py,同一批訊號同出場):
#     舊12期 TP2.5R   無新閘 n447 +0.717 勝50% 總R+320.7
#                     群聚≥3且BTC≥3%(前版) n291(65%) +0.964 勝57% 總R+280.5   ← 每筆最好但總R最低
#                     異常12h且BTC≥1%(現版) n375(84%) +0.856 勝54% 總R+320.8   ← 勝率↑、總R不減、少砍18%
#     2026 live幣池97 前版只留 17%(0.18筆/天=五天半一單)、現版留 48%(0.51筆/天),每筆 −0.103→−0.102
#   ★群聚閘本身沒有錯(它在 8 個崩盤月 n=1418 驗過),但①舊12期幣池只有 25~39 幣,換算倍率 5.4
#     → 那段回測裡它**全部放行**(447/447),所以 +0.664 那個數字跟它無關;②2026 沒有大崩盤 → 它幾乎不出單。
#     結論:它選的是「崩盤反彈日」,該當**加碼**用,不該當唯一的進場許可。
VLONG_CROWD_GATE      = True
VLONG_CROWD_MIN_163   = 3.0      # 換算到 163 幣池的「別的幣出V」數量下限;實際門檻 = 3 × 目前掃描池幣數 / 163
VLONG_CROWD_WIN_SEC   = 4 * 3600
VLONG_CROWD_MODE      = "boost"  # "boost"=群聚只加碼(現行) / "gate"=群聚不足就擋(2026-09-16 白天的舊行為)
VLONG_CROWD_BOOST     = 1.5      # 群聚成立且 BTC 反彈≥VLONG_CROWD_BTC_REB → 下注×1.5(舊12期那格 +0.964/勝57%)
VLONG_CROWD_BTC_REB   = 3.0      # 加碼才要求的 BTC 反彈門檻(原硬擋值)
VLONG_BTC_REBOUND_MIN = 1.0      # 硬擋門檻放寬:BTC 收盤/近96根15m最低點−1 ≥ 1%
                                 #   舊12期 ≥1% 保留90% +0.773 勝51%(≥3% 只保留65%);2026 ≥3% 只保留17%且EV更差
_VLONG_CROWD: Dict[str, float] = {}   # symbol -> 最近一次出 V(過完其他閘)的訊號K起始 epoch
_BTC_REB_CACHE: Dict[int, float] = {}

# ★★★2026-09-16 深夜 選幣層:成交量異常 或 OI 異常(用戶:「他們OI儀表板選幣就成交量或OI異常阿」)
#   數據獵手的訊號 content 寫著 source=volume_top100;用戶定調選幣層=量/OI異常。自己重建後測到**對照組**:
#   舊12期 TP2.5R:進場前12h內有量或OI異常 n412(92%) +0.814 勝52% 總R+335.5(比無閘的+320.7還高)
#                 **完全沒異常的那 8%  n35  −0.423 勝17% 吃滿停損83%**  ← 這就是要擋的那群
#   逐期 10/12 期改善、無一期變差;2026 中性(−0.046→−0.047,不傷)。腳本 _bt_absorb_anom_live.py
#   ★視窗深度已對齊 live:量用 72h 基準(296根≤fetch上限300)、OI 用 rubik 翻4頁=100h(實測無缺口)
#     7天窗 vs 72h窗 結果幾乎相同(+0.288 vs +0.290)→ 不是「凍結規格 live 執行不了」那種坑
VLONG_ANOM_GATE    = True
VLONG_ANOM_VOLX    = 3.0    # 量異常:近4根(1h)量 / 前288根(72h)同長度量的中位 ≥ 3.0x
VLONG_ANOM_VOL_BASE= 288
VLONG_ANOM_LOOK_H  = 12     # 異常發生在進場前 12h 內就算數(警報是選幣層,不是進場訊號)
VLONG_ANOM_OI_PAGES= 4      # rubik open-interest-history 15m 每頁100筆,4頁=100h
VLONG_ANOM_OI_Q    = 0.95   # OI異常:|近1h OI變化%| > 自身近96h 的 p95
_VLONG_OI_CACHE: Dict[str, tuple] = {}   # instId -> (抓取epoch, pd.Series(oi, 15m index))


def _btc_rebound_24h(end_ts: pd.Timestamp) -> float:
    """BTC 永續截至 end_ts(含)已收盤 15m:收盤 / 近96根最低點 − 1(%)。抓不到回 nan(呼叫端放行,同其他閘)。"""
    key = int(end_ts.timestamp())
    if key in _BTC_REB_CACHE: return _BTC_REB_CACHE[key]
    try:
        b = fetch_market_candles("BTC-USDT-SWAP", "15m", 300)
        b = b[b.index + pd.Timedelta(minutes=15) <= end_ts]
        v = float((b["close"].values[-1] / b["low"].values[-96:].min() - 1) * 100) if len(b) >= 96 else float("nan")
    except Exception as ex:
        print(f"[BTC反彈] 失敗(放行): {ex}"); v = float("nan")
    if len(_BTC_REB_CACHE) > 64: _BTC_REB_CACHE.clear()
    _BTC_REB_CACHE[key] = v
    return v


def _vlong_crowd_count(symbol_item: str, now_ts: float) -> int:
    """記下本幣這次出V,回傳前 4h 內「別的幣」出過 V 的數量(並清掉過期紀錄)。"""
    for k in [k for k, t in _VLONG_CROWD.items() if now_ts - t > VLONG_CROWD_WIN_SEC]:
        _VLONG_CROWD.pop(k, None)
    n = sum(1 for k, t in _VLONG_CROWD.items() if k != symbol_item and t <= now_ts)
    _VLONG_CROWD[symbol_item] = now_ts
    return n


def _okx_oi_hist_15m(inst_id: str, pages: int = VLONG_ANOM_OI_PAGES) -> Optional[pd.Series]:
    """OKX rubik open-interest-history(15m)翻頁取回約 pages×100 根的 OI(oiUsd)。
    ★一頁只回 100 筆(=25h),要 96h 的 p95 基準就必須帶 end 往回翻(實測 4 頁=100h、無缺口)。
    ★同 _okx_contract_cvd_15m 的模式:呼叫端只在「價格已成立 V」之後才打,不會每幣每輪洗量。
    抓不到回 None → 呼叫端當「OI 未知」處理(不擋、也不當通過)。"""
    ck = _VLONG_OI_CACHE.get(inst_id)
    if ck and (time.time() - ck[0]) < 600:
        return ck[1]
    rows: List[list] = []
    end = None
    try:
        for _ in range(max(1, pages)):
            params = {"instId": inst_id, "period": "15m"}
            if end is not None:
                params["end"] = str(end)
            d = _fetch_okx_public_data("/api/v5/rubik/stat/contracts/open-interest-history", params)
            if not d:
                break
            rows += d
            end = min(int(r[0]) for r in d) - 1
            time.sleep(0.35)
    except Exception as ex:
        print(f"[V-Long OI] {inst_id} 抓取失敗: {ex}", flush=True)
    if len(rows) < 120:          # 不足 30h 就算不出 96h 的 p95 → 當未知
        return None
    try:
        s = pd.Series({pd.to_datetime(int(r[0]), unit="ms", utc=True): float(r[3]) for r in rows})
        s = s[~s.index.duplicated()].sort_index()
    except Exception:
        return None
    if len(_VLONG_OI_CACHE) > 200:
        _VLONG_OI_CACHE.clear()
    _VLONG_OI_CACHE[inst_id] = (time.time(), s)
    return s


def _vlong_anomaly_ok(inst_id: str, df: pd.DataFrame) -> Tuple[bool, str]:
    """★選幣層:進場前 VLONG_ANOM_LOOK_H 小時內,該幣出現過「成交量異常」或「OI 異常」。
      量異常 = 近4根(1h)量 / 前 VLONG_ANOM_VOL_BASE 根同長度量的中位 ≥ VLONG_ANOM_VOLX
               (用比值,所以 OKX 的 vol 是合約張數也不影響 —— 同一合約 ctVal 會約掉)
      OI異常 = |近1h OI 變化%| > 自身近96h 同指標的 p95
    回傳 (通過, 說明)。兩邊都算不出來 → 放行(同其他閘的慣例:資料不足不擋單)。"""
    look = int(VLONG_ANOM_LOOK_H * 4)
    hits: List[str] = []
    unknown = 0
    # ── ① 成交量異常(用現成 K 線,不打 API)
    try:
        v = pd.Series(df["vol"].values.astype(float))
        base_n = min(int(VLONG_ANOM_VOL_BASE), max(0, len(v) - 8))
        if base_n >= 96:
            v4 = v.rolling(4).sum()
            volx = (v4 / v4.shift(4).rolling(base_n).median()).values
            seg = volx[-(look + 1):]
            seg = seg[np.isfinite(seg)]
            if len(seg):
                mx = float(np.nanmax(seg))
                if mx >= VLONG_ANOM_VOLX:
                    hits.append(f"量異{mx:.1f}x")
            else:
                unknown += 1
        else:
            unknown += 1
    except Exception:
        unknown += 1
    if hits:                      # 量已異常就不必再打 OI 的 API
        return True, "選幣:" + "+".join(hits)
    # ── ② OI 異常(rubik 翻頁)
    try:
        s = _okx_oi_hist_15m(inst_id)
        if s is None or len(s) < 120:
            unknown += 1
        else:
            end_ts = df.index[-1] + pd.Timedelta(minutes=15)
            s = s[s.index <= end_ts]
            ch = (s.pct_change(4).abs() * 100).dropna()
            if len(ch) < 100:
                unknown += 1
            else:
                q = float(ch.iloc[-384:].quantile(VLONG_ANOM_OI_Q))
                recent = ch.iloc[-(look + 1):]
                if len(recent) and float(recent.max()) > q > 0:
                    hits.append(f"OI異{float(recent.max()):.2f}%>p95{q:.2f}%")
    except Exception as ex:
        print(f"[V-Long 選幣] {inst_id} OI 異常判斷失敗(當未知): {ex}", flush=True)
        unknown += 1
    if hits:
        return True, "選幣:" + "+".join(hits)
    if unknown >= 2:              # 兩個來源都算不出 → 放行,不要用資料不足去殺訊號
        return True, "選幣:資料不足(放行)"
    return False, "選幣:12h內無量/OI異常"

# ★★2026-09-06 真假吸收閘(用戶逼出來的:「有大量進場有吸收,後面才會帶動市場價格」)────────
# 原本 VLONG 的「吸收」只有一行 `cvd[低2]<cvd[低1]`,沒有幅度也沒有量 → CVD 跌 1 單位也算吸收。
# 用戶講的機制:「很多人市價賣,主力用**限價掛單**吸收 → CVD降但價格下不去 → 才形成第二個V」。
# 拆成四件事對照現況:①第二腳較高=有(墊高≥2%) ②賣壓較重=有但無幅度 ③價格下不去=就是① ④**大量=沒有**
# 兩個門檻都用**該幣自己近96根**當基準(絕對值不可跨幣比):
#   VOLX = 兩低點間平均量 / 近96根中位量
#   CVDX = CVD下降量 / (近96根「逐根CVD變化絕對值」的中位數 × 窗口長度)
# 12期凍結驗收(訓練段選門檻,選完才看其他三層):
#   ┌ 配置 ─────────┬ 樣本 ┬ 訓練 ┬ 驗證 ┬ 新幣 ┬ 2022 ┬ 合計EV ┬ 勝率 ┬ 吃滿停損 ┬ 單期最差 ┐
#   │ 現行(無此閘)    │100% │+0.749│+0.929│+0.451│+0.466│ +0.625 │ 49% │  50.7%  │ -16.5R │
#   │ 只加量≥1.2     │ 89% │+0.817│+0.991│+0.551│+0.507│ +0.709 │ 52% │  48.0%  │ -18.4R │
#   │★量≥1.2+賣壓≥1 │ 41% │+1.157│+1.322│+0.746│+0.801│ +1.039 │ 63% │  36.5%  │ **-8.8R** │
#   └────────────────┴─────┴──────┴──────┴──────┴──────┴────────┴─────┴─────────┴────────┘
#   ★四層全部改善(不是選樣偏差的形狀)。同回撤預算下總報酬 25.2 → **32.2**(唯一勝率與風險調整後
#   報酬同時變好的改法;當天試過的位階閘/回撤深度閘/1R保本/gap上限全都做不到)。
# ★量的單軸:0.8-1.2x **EV-0.133 勝25%**(唯一負EV那群) / 2-4x +0.461 / 4x以上 +1.044 勝63%
# ★賣壓單軸:0-0.5倍 +0.275 勝38% / 2倍以上 +1.194 勝68%
# ★已測**不要加**:
#   ①OI:在「賣壓重」條件下 OI升+1.070 vs OI降+1.037 = **無差別**。機制上空單回補也是買、也吸收
#     賣壓、也推價格,但那會讓OI下降 → 兩條路都成立,所以方向分不出好壞。窗口改成「中間高點→低點2」
#     後邊際才有 +0.178,但疊上量+賣壓後總樣本只剩 35 筆,不夠。
#   ②吸收段時長(中間高點→低點2):疊上去 2022 層只剩 n=10 測不了,風險調整後 32.2→23.1,變差。
#   ③間隔上限:**加了量+賣壓後間隔就不爛了**(120-180分 +0.416→+0.835 勝60%)——被擋掉的391筆裡
#     258筆正是間隔120-255分那群,「離很遠的2V」爛的原因就是沒量沒賣壓,不是兩個問題。
#   ④墊高上限:加閘後每一桶都正(2-5% +0.515 / 5-10% +1.123 / 20%+ +1.741),不必設上限。
VLONG_ABS_MIN_VOLX = 1.2     # ①大量進場:兩低點間平均量 ≥ 1.2 × 該幣近96根中位量
VLONG_ABS_MIN_CVDX = 1.0     # ②賣壓夠重:CVD下降 ≥ 1.0 × 該幣近96根常態CVD波動
VLONG_TP_R = 2.5
VLONG_CVD_PAGES = 6          # ★2026-09-16 OKX rubik taker-volume 一頁只回 72 筆(6h),翻 6 頁 = 36h(吸收強度基準要 ≈24h)


def _vlong_zigzag_lows(hi, lo, pct):
    """ZigZag 低點(以回撤幅度定義,無左右N根確認)。回傳 [(idx, price, 確認idx)]。無未來函數。"""
    n = len(hi); L = []
    if n < 3: return L
    up = True; ex_i = 0; ex_p = hi[0]
    for i in range(1, n):
        if up:
            if hi[i] > ex_p: ex_i, ex_p = i, hi[i]
            elif ex_p > 0 and (ex_p - lo[i]) / ex_p >= pct:
                up = False; ex_i, ex_p = i, lo[i]
        else:
            if lo[i] < ex_p: ex_i, ex_p = i, lo[i]
            elif ex_p > 0 and (hi[i] - ex_p) / ex_p >= pct:
                L.append((ex_i, float(ex_p), i)); up = True; ex_i, ex_p = i, hi[i]
    return L


_VLONG_LAST: Dict[str, dict] = {}   # symbol -> 最近一次V成型明細(供訊號卡數據面板)
_VLONG_DIAG = {"呼叫": 0, "K棒不足": 0, "無CVD": 0, "CVD不足": 0, "無V成型": 0,
               "追漲擋": 0, "7天追漲擋": 0, "位階擋": 0, "量不足": 0, "賣壓不足": 0, "群聚不足": 0, "BTC未反彈": 0,
               "無異常": 0, "群聚加碼": 0, "觸發": 0}


_VLONG_7D_CACHE: Dict[str, Any] = {}   # instId -> (取得時間, 近7天漲幅% 或 None)


def _vlong_7d_runup(inst_id: str) -> Optional[float]:
    """近7天漲幅%(=現價 vs 168 小時前收盤)。
    ★為什麼用 1H 不用 15m:live 的 `fetch_market_candles` 受 OKX /market/candles 限制**上限300根**,
      15m 300根只有 3.1 天,拿不到 7 天;1H 只需 169 根(7天),遠在上限內、不必 history-candles 分頁。
      回測用 15m 的 cl[c2-672],時間戳與 1H 的 c[-169] 相同(同一時刻的最後成交價),定義一致。
    ★呼叫位置:放在位階閘**之後**(那時候候選已被濾到極少),每輪只會多打個位數的 API。
    抓不到 → 回 None,呼叫端當「未知」放行。"""
    ck = _VLONG_7D_CACHE.get(inst_id)
    if ck and (time.time() - ck[0]) < 1800:
        return ck[1]
    val = None
    try:
        _d1h = fetch_market_candles(inst_id, "1H", 200)
        if _d1h is not None and len(_d1h) >= 169:
            _c = _d1h["close"].values.astype(float)
            if float(_c[-169]) > 0:
                val = (float(_c[-1]) - float(_c[-169])) / float(_c[-169]) * 100.0
    except Exception as _e7d:
        print(f"[V-Long] {inst_id} 近7天漲幅取得失敗(放行): {_e7d}", flush=True)
    if len(_VLONG_7D_CACHE) > 300:
        _VLONG_7D_CACHE.clear()
    _VLONG_7D_CACHE[inst_id] = (time.time(), val)
    return val


def _okx_contract_cvd_15m(okx_swap_symbol: str, idx) -> "pd.Series":
    """★用 OKX rubik taker-volume(5m,全幣種都有) 聚合成 15m 合約CVD。
    取代 Coinalyze:CONA_PERP 只有 29 幣、live 掃描池 199 幣 → 85% 的幣拿不到 CVD,
    所有數據策略在那些幣上靜默 return(這就是「數據單都沒觸發」的根因)。
    OKX 回傳格式 [ts, sellVol, buyVol];一次約 576 根 5m ≈ 48 小時。
    ★與回測一致:回測的合約CVD也是用 taker buy/sell 推算,不是 Coinalyze。"""
    ccy = okx_swap_symbol.split("-")[0]
    # ★★2026-09-16 OKX 改版:一次只回 72 筆(6h),不再是 576 筆(48h)→ live log 每次都「CVD不足」,V成型等於全瞎。
    #   帶 end 往回翻頁可拿回完整歷史(實測 8 頁=48h、無缺口、間隔0.5秒不被限流)。
    #   吸收強度的基準要低點2之前 97 根15m(≈24h)→ 翻 VLONG_CVD_PAGES 頁(36h)。
    rows = []; _end = None
    for _pg in range(VLONG_CVD_PAGES):
        _q = {"ccy": ccy, "instType": "CONTRACTS", "period": "5m"}
        if _end is not None: _q["end"] = str(_end)
        _r = _fetch_okx_public_data("/api/v5/rubik/stat/taker-volume", _q)
        if not _r: break
        rows += _r; _end = int(_r[-1][0]) - 1
        if _pg < VLONG_CVD_PAGES - 1: time.sleep(0.35)
    if not rows or len(rows) < 60:
        return pd.Series(dtype=float)
    recs = []
    for r in rows:
        try:
            recs.append((int(r[0]), float(r[2]) - float(r[1])))   # buy - sell
        except Exception:
            continue
    if len(recs) < 60:
        return pd.Series(dtype=float)
    ser = pd.Series({pd.to_datetime(t, unit="ms", utc=True): d for t, d in recs}).sort_index()
    d15 = ser.resample("15min").sum()          # 5m → 15m
    cvd = d15.cumsum()
    return cvd.reindex(idx, method="ffill")


def _okx_spot_cvd_15m(okx_swap_symbol: str, idx) -> "pd.Series":
    """★現貨 CVD（15m）。用 OKX rubik taker-volume 的 **instType=SPOT**。

    ★我一度以為「現貨 CVD 沒有來源」而把官方的 `spot_cvd_i1/i2` 留空 —— 那是錯的。
      2026-09-24 實測：`instType=SPOT` 一次回 **576 筆 5m（48 小時）**，
      比 CONTRACTS 的 72 筆（6 小時）還多，**不用翻頁**。
    官方「數據訊號」每筆都同時帶 `fut_cvd_i1/i2` 與 `spot_cvd_i1/i2`，
    兩者分開看才是完整的背離判定（合約與現貨可能不同向，那正是資訊所在）。
    """
    ccy = okx_swap_symbol.split("-")[0]
    rows = _fetch_okx_public_data("/api/v5/rubik/stat/taker-volume",
                                  {"ccy": ccy, "instType": "SPOT", "period": "5m"})
    if not rows or len(rows) < 60:
        return pd.Series(dtype=float)
    recs = []
    for r in rows:
        try:
            recs.append((int(r[0]), float(r[2]) - float(r[1])))   # buy - sell
        except Exception:
            continue
    if len(recs) < 60:
        return pd.Series(dtype=float)
    ser = pd.Series({pd.to_datetime(t, unit="ms", utc=True): d for t, d in recs}).sort_index()
    cvd = ser.resample("15min").sum().cumsum()
    return cvd.reindex(idx, method="ffill")


_VLONG_KL_CACHE: Dict[str, pd.DataFrame] = {}   # inst -> 加深後的15m K線
_VLONG_DEEP_BUDGET = {"round": 0, "used": 0}   # 每輪最多深抓幾個幣(避免拖慢掃描)
VLONG_DEEP_BARS = 900          # 目標深度(900根15m ≈ 9.4天)
VLONG_DEEP_PER_ROUND = 8       # 每輪最多深抓幾個新幣


def _vlong_deep_candles(inst_id: str, df_recent: pd.DataFrame) -> pd.DataFrame:
    """★把 15m K線加深到 ~900 根(用 history-candles 分頁),快取後增量更新。
    為什麼要這樣:live 只有 fetch_market_candles 的 300 根(75小時),
    5% 擺動的 ZigZag 在這麼短的窗內樞紐嚴重不足 —— 實測**只保留 23% 的訊號**
    (同一批資料:完整歷史 22 個訊號 → 只給 300 根窗剩 5 個)。
    回測驗證的 EV 是在完整歷史上算的,live 抓不到就等於規格沒被執行。
    ★成本控制:首次每幣分頁抓(每輪最多 VLONG_DEEP_PER_ROUND 個幣),
      之後只用既有的 df_recent 增量合併,不增加任何請求。"""
    cur = _VLONG_KL_CACHE.get(inst_id)
    if cur is None:
        if _VLONG_DEEP_BUDGET["used"] >= VLONG_DEEP_PER_ROUND:
            return df_recent                      # 本輪配額用完,先用淺的
        _VLONG_DEEP_BUDGET["used"] += 1
        rows = []; after = None
        for _ in range(max(1, VLONG_DEEP_BARS // 100)):
            q = {"instId": inst_id, "bar": "15m", "limit": "100"}
            if after: q["after"] = after
            d = _fetch_okx_public_data("/api/v5/market/history-candles", q)
            if not d: break
            rows += d; after = d[-1][0]
        if rows:
            try:
                k = pd.DataFrame(rows, columns=["ts","open","high","low","close",
                                                "vol","volCcy","volCcyQuote","confirm"])
                k = k[k["confirm"] == "1"]
                idx = pd.to_datetime(k["ts"].astype("int64"), unit="ms", utc=True)
                cur = pd.DataFrame({c: k[c].astype(float).values
                                    for c in ("open","high","low","close","vol")}, index=idx)
                cur = cur.sort_index()
            except Exception as e:
                print(f"[V-Long] {inst_id} 深抓解析失敗: {e}"); cur = None
    if cur is None: return df_recent
    try:
        keep = [c for c in ("open","high","low","close","vol") if c in df_recent.columns]
        merged = pd.concat([cur, df_recent[keep]])
        merged = merged[~merged.index.duplicated(keep="last")].sort_index().tail(VLONG_DEEP_BARS)
        _VLONG_KL_CACHE[inst_id] = merged
        return merged
    except Exception:
        return df_recent


def _check_vlong(symbol_item: str, okx_bar_fmt: str, df: pd.DataFrame,
                 okx_swap_symbol: str = ""):
    """V成型吸收做多。回傳 (是否成立, 原因, 停損價)。df 為已去掉未收盤當根的 15m。"""
    try:
        _VLONG_DIAG["呼叫"] += 1
        # ★2026-09-03 撤回「K線加深」:當初診斷說 300 根只保留 23% 訊號,那是**測試方法的假象**
        #   —— 我用 range(W,n,4) 抽樣,23%≈1/4 正好是步長造成的。改 step=1(等同 live 每15分鐘
        #   逐根掃)重測,**保留率 100%**,300 根本來就夠、一根訊號都不會漏。
        #   故不再深抓,省下每幣 9 個 API 請求與一個會在 redeploy 歸零的快取。
        hi = df["high"].values; lo = df["low"].values
        n = len(hi)
        if n < 200:
            _VLONG_DIAG["K棒不足"] += 1; return False, "", 0.0
        # ★2026-09-16 改成「價格先成立 V 型才抓 CVD」(下方迴圈內):CVD 現在要向 OKX 翻 6 頁,
        #   每幣每 15 分鐘都抓會被限流;V 型價格條件本來就很少成立。判斷結果與先抓完全相同(CVD 只在 V 成立後才用到)。
        cv = None
        # ★2026-09-01 修(用戶回報 ZRO 有吸收卻沒抓到):
        #   原本把價格截斷到 CVD 的 48 小時範圍才跑 ZigZag → 5% 擺動在 48h 內通常只形成
        #   **1 個**低點樞紐(不足 2 個),幾乎永遠判不出 V。回測有幾個月歷史故樞紐充足,
        #   這就是「無V成型 147/150」與 ZRO 漏抓的真正原因 —— 是我的實作,不是策略。
        #   正解:ZigZag 用**完整價格歷史**(不受 CVD 48h 限制),只要求那兩個樞紐點的 CVD 有值。
        #   V 成型要求間隔≤4h 且進場在最新根 → 兩個樞紐必然落在最近數小時,一定在 48h 內。
        for pct in VLONG_SWINGS:
            L = _vlong_zigzag_lows(hi, lo, pct)
            if len(L) < 2: continue
            (j1, p1, _c1), (j2, p2, c2) = L[-2], L[-1]
            if c2 != n - 1: continue                       # ★只在「V剛成型確認」那根進場
            if j2 - j1 > VLONG_MAX_GAP: continue           # 兩個低點間隔上限
            if not (p2 > p1): continue                     # ★V成型:低點墊高
            if (p2 - p1) / p1 < VLONG_MIN_UP: continue     # ★2026-09-04 補回測的 min_up=2% 門檻
            try:                                           # ★流動性閘:近96根15m成交額中位數
                _turn = (df["vol"].values.astype(float) * df["close"].values.astype(float))[-96:]
                _liq = float(np.median(_turn)) if len(_turn) else 0.0
            except Exception:
                _liq = 0.0
            if _liq < VLONG_MIN_LIQ:
                print(f"[V-Long] {symbol_item} 流動性不足(15m成交額中位 {_liq:,.0f} < "
                      f"{VLONG_MIN_LIQ:,.0f})→跳過", flush=True)
                continue
            if cv is None:
                cvd = _okx_contract_cvd_15m(okx_swap_symbol or symbol_item.replace("/", "-") + "-SWAP", df.index)
                if cvd is None or len(cvd) == 0:
                    _VLONG_DIAG["無CVD"] += 1; return False, "無合約CVD來源", 0.0
                cv = cvd.values.astype(float)
                if np.isfinite(cv).sum() < 60:
                    _VLONG_DIAG["CVD不足"] += 1; return False, "合約CVD數據不足", 0.0
            if not (np.isfinite(cv[j1]) and np.isfinite(cv[j2])):
                continue                                   # 樞紐落在CVD涵蓋範圍外 → 跳過這個擺動
            if not (float(cv[j2]) < float(cv[j1])): continue   # ★吸收:合約CVD低點降低
            # ★★2026-09-06 真假吸收閘(用戶:「有大量進場有吸收後面才會帶動市場價格」)──────
            #   原本的吸收判斷只有上面那一行 `cv[低2]<cv[低1]`,**沒有幅度、也沒有量**——
            #   CVD 只要跌 1 單位就算「賣壓被吸收✅」。用戶點出機制:「很多人市價賣,主力用
            #   限價掛單吸收 → CVD降但價格下不去 → 才形成第二個V」,關鍵是**要有大量**。
            #   兩個都用該幣自己近96根當基準(不跨幣比絕對值)。詳見 VLONG_ABS_* 常數註解。
            try:
                # ★基準必須取在**低點2 之前** 96 根(不含低點2、也不是陣列尾端)——
                #   對齊回測的 rolling(96).median().shift(1) 在 j2 的取值。
                #   (2026-09-06 移植對拍抓到:原本寫 _vv[-96:] 取到進場那根,245/245 全不一致)
                _vv = df["vol"].values.astype(float)
                _vnorm = float(np.median(_vv[j2 - 96:j2])) if j2 >= 96 else 0.0
                _cvd_step = np.abs(np.diff(cv[j2 - 97:j2])) if j2 >= 97 else np.array([])
                _cnorm = float(np.median(_cvd_step)) if len(_cvd_step) else 0.0
                _w = int(j2 - j1) + 1
                _volx = (float(np.mean(_vv[j1:j2 + 1])) / _vnorm) if _vnorm > 0 else 0.0
                _cvdx = ((float(cv[j1]) - float(cv[j2])) / (_cnorm * _w)) if _cnorm > 0 else 0.0
            except Exception as _abe:
                print(f"[V-Long] {symbol_item} 吸收強度計算失敗: {_abe}", flush=True)
                _volx = _cvdx = 0.0
            if _volx < VLONG_ABS_MIN_VOLX:
                _VLONG_DIAG["量不足"] += 1; continue        # ①沒有大量進場 → 不是吸收
            if _cvdx < VLONG_ABS_MIN_CVDX:
                _VLONG_DIAG["賣壓不足"] += 1; continue      # ②賣壓太輕 → 沒東西可吸
            # ★★2026-09-05 反追漲閘(用戶實盤觀察逼出來的:「要嘛進在很高的位置」)──────────────
            #   9/4 那天 6 筆 V成型全部停損,進場位階中位 **89%**,而且每一筆都是在該幣
            #   已經噴了 +10~47%(24h) 之後才進 —— 上漲途中本來就一路「低點墊高」,
            #   V成型條件照樣成立 → 它變成**在拋物線頂部追多**,不是設計中的 V 形反轉。
            #   我 0904 只測過「進場前24h**跌幅**」那一側(跌>20% EV+0.896 最好),
            #   **從沒測過漲幅那一側**。補測(_bt_vlong_runup.py, n=982)分桶單調:
            #     跌>20% +0.896/25.7 | 跌10~20% +0.670 | 漲0~5% +0.418 | 漲5~10% +0.259/7.4
            #     | **漲10~20% +0.058/容錯1.6/勝31%** | 漲20~40% +0.219 | 漲>40% +0.248
            #   位階同樣單調:0~50% +0.840/24.0 → 70~85% +0.193/5.5 → >85% +0.215/6.1
            #   ★★但用戶隨即指正兩點,兩點都被數據推翻了我的第一版(漲幅≤0%),故**只留位階閘**:
            #     ①「追漲不是說不行,你要追對位置」→ 位階≤70% 之下追漲是可以的:
            #        漲幅≤0 +0.778/22.3 | 漲0~10% +0.348/9.9 | 漲10~25% +0.508/14.5
            #     ②「擋掉這些,他噴出去你又能進到嗎」→ 會漏:漲幅閘擋掉的485筆 EV 仍有+0.230,
            #        其中 **173 筆 R≥2 大贏單 = 全部大贏單的 40%**。
            #   最終(只用訓練段TR即可判定,未看留出層):**只用 位階≤70%**
            #     全期 +0.508/14.5 → **+0.686/19.6**,總R +498→+424(漲幅版只有+387),
            #     大贏單保住 70%(漲幅版 60%),正期 10/12、驗證段 3/3(漲幅版 9/12、2/3),保留63%。
            #   ★9/4 那 6 筆有 5 筆位階在 74~93%,光位階閘就擋掉(只有 TRIA 位階51% 會放行)。
            #   ★_chg24 只計算不擋單(VLONG_MAX_RUNUP_PCT=999),留在 log 供觀察。
            _cla = df["close"].values.astype(float)
            _c96 = _cla[-97] if len(_cla) >= 97 else _cla[0]
            _chg24 = ((float(_cla[-1]) - float(_c96)) / float(_c96) * 100.0
                      if float(_c96) > 0 else 0.0)
            if _chg24 > VLONG_MAX_RUNUP_PCT:
                _VLONG_DIAG["追漲擋"] += 1
                print(f"[V-Long] {symbol_item} 進場前24h已漲 {_chg24:+.1f}% "
                      f"(>{VLONG_MAX_RUNUP_PCT:g}%)→擋(反追漲閘)", flush=True)
                continue
            _rl = float(np.min(lo[-97:])); _rh = float(np.max(hi[-97:]))
            _pos = ((float(df["close"].iloc[-1]) - _rl) / (_rh - _rl) * 100.0) if _rh > _rl else 0.0
            if _pos > VLONG_MAX_POS_PCT:
                _VLONG_DIAG["位階擋"] += 1
                print(f"[V-Long] {symbol_item} 進場位階 {_pos:.0f}% "
                      f"(>{VLONG_MAX_POS_PCT:g}%,買在區間上緣)→擋", flush=True)
                continue
            # ★2026-09-18 近7天漲幅閘(說明與實測數字見 VLONG_MAX_7D_RUNUP 常數區)
            #   放在位階閘之後 = 只對已通過其餘所有閘的候選打這支 API。
            _chg7d = _vlong_7d_runup(okx_swap_symbol or symbol_item)
            if _chg7d is not None and _chg7d > VLONG_MAX_7D_RUNUP:
                _VLONG_DIAG["7天追漲擋"] += 1
                print(f"[V-Long] {symbol_item} 近7天已漲 {_chg7d:+.1f}% "
                      f"(>{VLONG_MAX_7D_RUNUP:g}%)→擋(7天反追漲閘)", flush=True)
                continue
            # 判定值印進 log 供事後對帳(觀察條款寫成代碼)
            print(f"[V-Long] {symbol_item} 通過閘:位階{_pos:.0f}% 24h{_chg24:+.1f}% "
                  f"7天{(f'{_chg7d:+.1f}%' if _chg7d is not None else '未知')}", flush=True)
            sl = float(p2) * 0.999
            if sl >= float(df["close"].iloc[-1]): continue
            _crowd_txt = ""
            _vlong_boost = 1.0
            if VLONG_CROWD_GATE:    # ★2026-09-16 深夜改版:群聚→加碼、BTC→放寬硬擋(說明見常數區)
                _now = float(df.index[-1].timestamp())
                _n_other = _vlong_crowd_count(symbol_item, _now)   # 仍要呼叫:它同時負責把本幣記進群聚名單
                _need = VLONG_CROWD_MIN_163 * len(SYMBOLS) / 163.0
                if VLONG_CROWD_MODE == "gate" and _n_other < _need:
                    _VLONG_DIAG["群聚不足"] += 1
                    print(f"[V-Long] {symbol_item} V成型成立但前4h只有{_n_other}個別的幣出V(需{_need:.1f})→不是崩盤反彈,不進", flush=True)
                    continue
                _reb = _btc_rebound_24h(df.index[-1] + pd.Timedelta(minutes=15))
                if _reb == _reb and _reb < VLONG_BTC_REBOUND_MIN:
                    _VLONG_DIAG["BTC未反彈"] += 1
                    print(f"[V-Long] {symbol_item} BTC 只從24h低點彈{_reb:.1f}%(<{VLONG_BTC_REBOUND_MIN}%)→還在殺,不進", flush=True)
                    continue
                if _n_other >= _need and (_reb != _reb or _reb >= VLONG_CROWD_BTC_REB):
                    _vlong_boost = VLONG_CROWD_BOOST      # 崩盤反彈日=舊12期那格 +0.964/勝57% → 加碼
                    _VLONG_DIAG["群聚加碼"] += 1
                    _crowd_txt = f"/群聚{_n_other}幣×{VLONG_CROWD_BOOST:g}/BTC反彈{_reb:.1f}%"
                else:
                    _crowd_txt = f"/BTC反彈{_reb:.1f}%" if _reb == _reb else ""
            if VLONG_ANOM_GATE:     # ★選幣層:12h 內要有量或OI異常(沒異常那群 −0.423R/勝17%)
                _anom_ok, _anom_txt = _vlong_anomaly_ok(okx_swap_symbol or symbol_item, df)
                if not _anom_ok:
                    _VLONG_DIAG["無異常"] += 1
                    print(f"[V-Long] {symbol_item} V成型成立但{_anom_txt}→不是主力在動的幣,不進", flush=True)
                    continue
                _crowd_txt += f"/{_anom_txt}"
            _VLONG_DIAG["觸發"] += 1
            try:
                # ★2026-09-16 訂正:live 的 15m df **沒有 oi 欄**(fetch_market_candles 只回 OHLCV)
                #   → 這裡以前恆為 None,訊號卡的 OI 欄一直是空的。改用選幣層剛抓好的 rubik 序列。
                _oiv = None
                if "oi" in df.columns:
                    _oiv = pd.Series(df["oi"].values.astype(float)).ffill().bfill().values
                else:
                    _ck = _VLONG_OI_CACHE.get(okx_swap_symbol or symbol_item)
                    if _ck and _ck[1] is not None and len(_ck[1]) > 5:
                        _oiv = _ck[1][_ck[1].index <= df.index[-1] + pd.Timedelta(minutes=15)].values
                _oip = ((_oiv[-1] / _oiv[-5] - 1) * 100
                        if _oiv is not None and len(_oiv) > 5 and _oiv[-5] > 0 else None)
                _VLONG_LAST[symbol_item] = {
                    "swing": pct * 100, "gap_bars": int(j2 - j1),
                    "low1": float(p1), "low2": float(p2),
                    "up_pct": (float(p2) / float(p1) - 1) * 100,
                    "cvd_delta": float(cv[j2]) - float(cv[j1]), "oi_pct": _oip,
                    "boost": float(_vlong_boost),   # ★群聚日加碼倍數,呼叫端讀這個餵 dh_boost
                }
            except Exception:
                pass
            return True, f"V成型吸收多(擺動{pct*100:g}%/間隔{j2-j1}根/低點{p1:.6g}→{p2:.6g}{_crowd_txt})", sl
        _VLONG_DIAG["無V成型"] += 1
        return False, "", 0.0
    except Exception as e:
        print(f"[V-Long] {symbol_item} 判斷失敗: {e}")
        return False, "", 0.0


# ══════════════════════════════════════════════════════════════════════
# ★LL→LH 反彈斐波吞噬 做空（15m，2026-09-04 上線）
# 用戶口述結構：「多方是 LH HH，那空方就是 LL LH，空方就是在反彈過程中找機會做空」
# 12期凍結驗收（只用訓練段4期在36組網格選參數，凍結後才看其他層）：
#   訓練 n=547 EV+0.401 容錯11.7 正期4/4  ／ 驗證 n=616 EV+0.382 容錯11.1 正期3/3
#   未測新幣 n=659 EV+0.329 容錯9.5 正期2/2 ／ 2022外樣本 n=1010 EV+0.236 容錯6.8 正期2/3
#   加每日上限5筆後：n=2549 EV+0.306 容錯8.9 **正期12/12** 新幣+0.303 2022+0.263
# 打架(ChatGPT+Gemini)四個攻擊點，三個用數據排除、一個成立已修：
#   ①收盤進 vs 下一根開盤進 = +0.313 vs +0.312（不靠收盤價成交）→ 排除
#   ②因果稽核:進場根−L2確認根 最小=1（永遠在確認之後）→ 無未來函數
#   ③右截尾:期末未平倉僅1.66%，排除後 +0.305 → 排除
#   ④跨幣叢集:同日訊號 max18、最差日−18.8R → **成立**，加每日上限5筆後最差日 −5.3R
# ★不設時間停損:被砍掉的長單原本 EV+0.65~+0.77（是獲利來源），設1天上限 EV 崩到 +0.040。
# 成本敏感度:往返0.25% 仍 +0.261（停損中位2.75% 夠寬）。重疊率 vs 現役吞噬空 = 2.0%。
LLH_SHORT_ENABLED = False   # ★2026-09-04 上線前一刻關閉:逐根重放 live 邏輯後 EV 崩掉8成。
#   原回測 EV+0.306/容錯8.9/正期12/12 是**枚舉每個L2往後掃**得到的 —— 它會拿「已被更新的舊結構」
#   進場,live 只看當下最新的低點,做不到。live/回測逐點對拍只重現 52.3%。
#   改成逐根重放 live 邏輯的回測(_bt_llh_live.py):
#     全期 n=2466 EV+0.063 容錯1.8 正期9/12 ／ 驗證+0.017 ／ 2022+0.036(正期1/3)
#     停損中位2.17%(較窄)→ 往返成本0.20% 只剩+0.013、0.25% 轉負 −0.012
#   ★重開前置條件:①找到 live 可執行、且逐點對拍100%的規格 ②在該規格下容錯≥8 且成本0.25%仍為正
#   ★教訓:參數/結構驗完之後,**必須先做 live 逐根重放對拍再談上線**,不能只做「訊號數對得上」。
LLH_SWING       = 0.02      # ZigZag 擺動（2%>3%>4%>5%>6%>8% 單調，2% 最佳）
LLH_FIB_LO      = 0.55      # 反彈打折帶（0.382~0.5 < 0.5~0.618 < 0.55~0.7 單調）
LLH_FIB_HI      = 0.70
LLH_MAX_WAIT    = 576       # 反彈等待上限（根15m）= 6天。6h→192h 單調上升，144h 起容錯>8
LLH_TP_R        = 2.5
LLH_COOLDOWN_H  = 384       # 同幣冷卻(小時)=16天。把頻率從 47 筆/天壓到 3.7 筆/天
LLH_DAILY_CAP   = 5         # ★每日新倉上限(叢集風控)。EV 只掉0.006，最差日 −18.8R→−5.3R
_LLH_LAST_TS: Dict[str, float] = {}      # symbol -> 上次觸發的 epoch 秒（冷卻用）
_LLH_DAY = {"day": "", "count": 0}       # 每日新倉計數
_LLH_DIAG = {"呼叫": 0, "K棒不足": 0, "樞紐不足": 0, "非LL": 0, "反彈過H1": 0,
             "不在斐波帶": 0, "非吞噬": 0, "冷卻中": 0, "日上限": 0, "觸發": 0}


def _zigzag_hl(hi, lo, pct):
    """ZigZag 高低點（回撤幅度定義，無左右N根確認）。
    回傳依**確認時間**排序的 [(確認idx, 樞紐idx, 價格, 'H'/'L')]。無未來函數。"""
    n = len(hi); ev = []
    if n < 3: return ev
    up = True; ex_i = 0; ex_p = hi[0]
    for i in range(1, n):
        if up:
            if hi[i] > ex_p: ex_i, ex_p = i, hi[i]
            elif ex_p > 0 and (ex_p - lo[i]) / ex_p >= pct:
                ev.append((i, ex_i, float(ex_p), "H")); up = False; ex_i, ex_p = i, lo[i]
        else:
            if lo[i] < ex_p: ex_i, ex_p = i, lo[i]
            elif ex_p > 0 and (hi[i] - ex_p) / ex_p >= pct:
                ev.append((i, ex_i, float(ex_p), "L")); up = True; ex_i, ex_p = i, hi[i]
    return ev


def _check_llh_short(symbol_item: str, df: pd.DataFrame):
    """LL→LH 反彈斐波吞噬 做空。回傳 (是否成立, 原因, 停損價)。
    df = 已去掉未收盤當根的 15m。判定一律在最新已收盤根（n-1）。"""
    try:
        _LLH_DIAG["呼叫"] += 1
        op = df["open"].values; hi = df["high"].values
        lo = df["low"].values; cl = df["close"].values
        n = len(cl)
        if n < 200:
            _LLH_DIAG["K棒不足"] += 1; return False, "", 0.0
        i = n - 1                                   # 最新已收盤根
        # ── ④ 先檢查最便宜的條件:看跌吞噬 ──
        if not (cl[i] < op[i] and cl[i-1] > op[i-1]
                and op[i] >= cl[i-1] and cl[i] <= op[i-1]):
            _LLH_DIAG["非吞噬"] += 1; return False, "", 0.0
        ev = [e for e in _zigzag_hl(hi, lo, LLH_SWING) if e[0] <= i]   # 只用已確認樞紐
        if len(ev) < 3:
            _LLH_DIAG["樞紐不足"] += 1; return False, "", 0.0
        # 需要序列 ... L1, H1, L2（L2 = 最近一個已確認的**低點**）
        # ★2026-09-04 對拍修正:原本寫 ev[-1] 必須是 L,但反彈到斐波帶時那個反彈高點
        #   常常已被確認成新的 H → ev[-1] 變 H,結構判定整個失效。
        #   live 重放只重現回測訊號的 42.7%,漏斗顯示「非LL」擋掉45%。
        #   正解:往回找**最後一個 L**當 L2（若之後又出現更低的新 L,自然會換成那個=新的setup）。
        _li = None
        for _k in range(len(ev) - 1, 1, -1):
            if ev[_k][3] == "L": _li = _k; break
        if _li is None or _li < 2:
            _LLH_DIAG["非LL"] += 1; return False, "", 0.0
        cL2, jL2, pL2, _ = ev[_li]
        cH1, jH1, pH1, tH1 = ev[_li - 1]
        cL1, jL1, pL1, tL1 = ev[_li - 2]
        if tH1 != "H" or tL1 != "L":
            _LLH_DIAG["非LL"] += 1; return False, "", 0.0
        if not (pL2 < pL1):                          # ★① LL:低點降低
            _LLH_DIAG["非LL"] += 1; return False, "", 0.0
        if i - cL2 > LLH_MAX_WAIT:                   # ★② 反彈等待上限 6 天
            _LLH_DIAG["反彈過H1"] += 1; return False, "", 0.0
        if float(np.max(hi[cL2 + 1:i + 1])) > pH1:   # 反彈超過前高 → 結構破壞
            _LLH_DIAG["反彈過H1"] += 1; return False, "", 0.0
        rng = pH1 - pL2
        if rng <= 0:
            _LLH_DIAG["非LL"] += 1; return False, "", 0.0
        zl = pL2 + rng * LLH_FIB_LO; zh = pL2 + rng * LLH_FIB_HI
        if not (hi[i] >= zl and lo[i] <= zh):        # ★③ 反彈進入斐波 0.55~0.7
            _LLH_DIAG["不在斐波帶"] += 1; return False, "", 0.0
        # ── 冷卻 / 每日上限 ──
        _now = time.time()
        if _now - _LLH_LAST_TS.get(symbol_item, 0.0) < LLH_COOLDOWN_H * 3600:
            _LLH_DIAG["冷卻中"] += 1; return False, "", 0.0
        _today = datetime.now(timezone.utc).strftime("%Y-%m-%d")
        if _LLH_DAY["day"] != _today:
            _LLH_DAY["day"] = _today; _LLH_DAY["count"] = 0
        if _LLH_DAY["count"] >= LLH_DAILY_CAP:
            _LLH_DIAG["日上限"] += 1; return False, "", 0.0
        sl = float(pH1) * 1.001                      # 停損 = 前一個高點 H1
        if sl <= float(cl[i]):
            return False, "", 0.0
        _LLH_LAST_TS[symbol_item] = _now
        _LLH_DAY["count"] += 1
        _LLH_DIAG["觸發"] += 1
        return True, (f"LL→LH反彈空(低點{pL1:.6g}→{pL2:.6g}/等待{i-cL2}根/"
                      f"斐波{LLH_FIB_LO:g}~{LLH_FIB_HI:g}/前高{pH1:.6g})"), sl
    except Exception as e:
        print(f"[LLH-Short] {symbol_item} 判斷失敗: {e}")
        return False, "", 0.0


# ══════════════════════════════════════════════════════════════════════════════
# ★S4H 做空(2026-09-04 上線):4h 看跌吞噬 + 趨勢線123 + 斐波反彈打折 + LL 下降結構
# ──────────────────────────────────────────────────────────────────────────────
# 【為什麼是這組】用戶要做空。把 4J模式/LL→LH/吸收頂背離/數據警報/現役吞噬空放在
#   同一批資料、同一出場、同一驗收下比(_bt_short_league.py),只有這組通過全部關卡。
# 【凍結規格】_bt_s4h_final.py (use_LL=True, use_D2=False) —— 逐項照抄,不准自己補係數:
#   ① 該根 4h **看跌吞噬**(陰線吞掉前一根陽線實體)
#   ② 近 6 根(24h)內成立過**趨勢線123**(上升支撐線被實體跌破 → 回調上不去 → 收黑K = 2)
#   ③ 該根觸及「最近一段已確認下跌腿」的**斐波 0.382~0.618 反彈帶**
#   ④ **LL**:最近兩個已確認 ZigZag 低點是降低的
#   進場 = 下一根 4h 開盤(即本根收盤當下);停損 = 最近一個在進場價上方的已確認樞紐高 ×1.001
#   TP = 2.5R 全平,**不設時間停損**;同幣冷卻 2 根(8h);每日新倉上限 5;
#   流動性 = 近 24 根 4h 成交額中位 ≥ 400,000 USDT
# 【驗收數字】n=391 EV+0.361 容錯10.9 勝41% 正期9/12
#   訓練TR 6.6 / 驗證VE 7.6 / 未測新幣 13.9 / 2022外樣本 12.9 —— 四層全正
#   成本0.25% → EV+0.334/容錯10.1;期別 block bootstrap 95%CI [+0.078,+0.570](下界>0)
#   頻率 0.54 筆/天;重疊率 vs 現役 OI壓縮突破空 = 0.5%(非重複下注)
#   ★bar內順序(打架 ChatGPT #1):4h OHLC 分不出同根先觸SL還是TP,改用 **15m 重建**先後
#     → EV+0.352 vs 原 +0.361,無實質差異(停損中位 6.27% 夠寬),該反駁排除。
# 【已知弱點 —— 觀察期要盯,不要假裝沒有】
#   ①獲利集中:41個月裡獲利最多的 3 個月貢獻 71%;79 幣裡前 10 幣貢獻 71%
#   ②尚未做:資金費逐筆計入、真實深度/價差滑價、date-cluster bootstrap、組合層總曝險上限
#   ③「時間平移檢定」第一版我寫錯了(只平移開始檢查SL/TP的時點、沒平移進場價),結論作廢待重做
# 【★★live 資料深度是硬需求,不是優化】
#   fetch_market_candles 受 OKX /market/candles 限制,上限 300 根。
#   實測 _chk_s4h_win300.py(同一批 238 筆回測訊號,把資料截斷成 live 看得到的視窗重算):
#     300根 → 只重現 **77.7%**(53 筆因 TrendLines(max_age=600) 畫不出線而消失)
#     500根 → 81.9%   /   **1000根 → 99.2%**
#   → 必須用 history-candles 分頁把 4H 加深到 ~1000 根,否則等於凍結規格沒被執行。
#   (同族教訓:CLAUDE.md「生命週期沒複刻」;LL→LH 就是死在 live 重放只剩 52.3%。)
# ══════════════════════════════════════════════════════════════════════════════
S4H_SHORT_ENABLED  = True
S4H_SWING          = 0.04        # ZigZag 擺動
S4H_FIB_LO         = 0.382
S4H_FIB_HI         = 0.618
S4H_FIB_MAXAGE     = 60          # 波段太舊就不畫斐波(根)
S4H_123_WIN        = 6           # 123 回看窗(根 4h = 24h)
S4H_TP_R           = 2.5
S4H_BE_R           = 1.5         # ★2026-09-15 用戶「保住本金為主」:浮盈 1.5R → 停損移保本(含費),TP 仍 2.5R 全平
#   同一批 627 筆逐根重跑(_bt_be_bor_s4h.py,先停損→再停利→最後才移保本):
#     不保本 吃滿停損61% 每筆+0.272R 總+170R / ★1.5R保本 吃滿停損**50%** 每筆+0.277R 總+174R
#     1.0R保本 42% +0.230 +144R / 0.8R保本 38% +0.197 +124R
#   1.5R = 虧到本金的單少1/6、總R不掉;2026 live幣池 +0.41→+0.46。
S4H_COOLDOWN_BARS  = 2           # 同幣冷卻(根 4h)
S4H_DAILY_CAP      = 5           # 每日新倉上限(叢集風控)
S4H_MIN_LIQ        = 400_000.0   # 近24根 4h 成交額中位下限(USDT)
S4H_TL_PV          = 5           # 趨勢線樞紐左右確認根數
S4H_TL_TOL         = 0.003
S4H_TL_TOUCH       = 3           # 官方「三個點確立效果更好」
S4H_TL_WAIT        = 96
S4H_TL_MAXAGE      = 600
S4H_TL_KEEPN       = 6
S4H_DEEP_BARS      = 1000        # ★見上方:300根只重現77.7%,1000根99.2%
S4H_DEEP_PER_ROUND = 6           # 每輪最多深抓幾個新幣(控API用量)
_S4H_KL_CACHE: Dict[str, pd.DataFrame] = {}
_S4H_DEEP_BUDGET   = {"used": 0}
_S4H_LAST_BAR: Dict[str, int] = {}          # inst -> 上次觸發的 4h bar epoch(冷卻)
_S4H_DAY = {"day": "", "count": 0}          # 每日新倉計數
_S4H_DIAG = {"呼叫":0, "K棒不足":0, "非吞噬":0, "流動性":0, "無123":0, "不在斐波":0,
             "非LL":0, "冷卻":0, "每日上限":0, "停損無效":0, "急漲擋":0, "成立":0}

# ══════════════════════════════════════════════════════════════════════════════
# ★2026-09-15 進場品質閘(S4H 急漲閘 / 4JD 位置閘 + BTC 急漲閘)
# ══════════════════════════════════════════════════════════════════════════════
# 用戶:「會輸的就去找原因找方法改善,提升勝率,不能砍到沒單,多空都不能啞巴」。
# 選法事先寫死(_night_select3.py / _night_combo.py):每個候選只用訓練段(23Q4~24Q3)挑,
#   通過 = 驗證/新幣/2022/2026(63幣)/2026(live幣池96幣) 五段 ≥4 段每筆R變好 + 整體勝率不降
#   + 留單≥60% + BTC 日線多頭/空頭時各留≥50%(不會轉多轉空就啞巴)。
# S4H 急漲閘(用戶做空核心「先看怎麼漲上來的:一口氣急漲不空第一個頭」,feedback_approach_1a_2a):
#   進場前 48 根已收盤 1H 內最高點 t,往前 72 根內最低點 s = 這段上漲;途中拉回≥3%再創新高算一個A。
#   A≤1 且 漲幅≥12% → 不空。n=627:留92% 勝39→40% 每筆+0.272→+0.300R 總R+170→+174
#   五段 4/5(驗+0.26→+0.33 新+0.49→+0.53 22+0.42→+0.38❌ 26−0.11→−0.06 26L+0.41→+0.45)
# 4JD 位置閘:最近30根已收盤 4H 高低區間裡,進場價位置<15% → 不空(空在這波最底,live 輸單圖共同點)
# 4JD BTC 閘:BTC 近24h 漲>+1% → 不空(09-14 HYPE/VIRTUAL 等同一根4H 在 BTC 反彈時一起被掃)
#   兩道合併 n=533:留65% 勝27→30% 每筆+0.151→+0.243R 總R+81→+84 五段 5/5
#   (驗+0.63→+0.70 新+0.26→+0.50 22+0.38→+0.55 26+0.06→+0.07 26L−0.20→−0.19)
#   ★已知:4JD 在 2026 live 幣池本身每筆 −0.20R,這兩道閘救不回,另查。
# ★對拍:_chk_night_gates_port.py 用 exec 抽這段函式,在回測資料上逐筆重算特徵,須與回測 0 不一致。
# ★被擋的訊號照樣佔冷卻/每日上限(回測是在已成交的單上事後過濾)。
S4H_SPIKE_GATE     = True
S4H_SPIKE_MAX_A    = 1
S4H_SPIKE_RISE     = 12.0        # %
S4H_SPIKE_PULL     = 0.03        # 拉回≥3%才算一個A
FOURJD_POS_GATE    = True
FOURJD_POS_N       = 30          # 根 4H
FOURJD_POS_MIN     = 15.0        # %
FOURJD_BTC_GATE    = True
FOURJD_BTC_MAX     = 1.0         # BTC 24h 漲幅上限 %

# ★★★2026-09-16 深夜 4JD 進場品質閘(用戶:「再搞搞 不然空單這樣行嗎」)──────────────────────
# 4JD 在 2026(到09-15,live幣池97) 每筆 −0.216/勝17%,**比配對隨機還差**(超額 −0.205,P(超額≥0)=7.6%)。
# ★先說清楚:**既有的 FOURJD_POS_GATE(4H/N30/≥15%) 在 2026 實測是白擋的** ——
#   被它擋掉的 EV −0.193、留下的 −0.204,兩邊一樣爛,只砍單量不改品質(腳本 _an_shorts_gated.py)。
#   留著是因為被擋那批確實略差,但它救不了策略。
# 這兩道是另外選出來的(只用訓練段22H1/22H2/23H1、用勝率選,2026完全不參與挑選;
#   並要求驗證段與新幣層都要比各自無閘基準改善才進候選 → 592/330 個候選裡取訓練段勝率第一)：
#   ①進場位階 = 近96根已收盤1H 區間裡進場收盤的位置% ≥ 30(太貼近期低點=空在殺完的底部)
#   ②進場K(1H)實體佔全棒幅 ≥ 0.50
# 驗收:訓練 +0.383→**+0.663**(勝34→48.7%) 驗證 +0.302→**+0.433** 新幣 +0.211→**+0.310**
#      **2026 −0.216→+0.035**(勝17→22%) 留存26%;逐期 5/5 全正。腳本 _an_short_env2.py
# ★誠實標註:2026 CI 跨 0(P(EV>0)≈56%)、留存只有26% → 這是「從明確虧損變成打平」,不是證明會賺。
# ★BTC 波動閘(吞噬空那道)對 4JD **無效**(2026 −0.216→−0.167 仍負),所以沒套上來。
FOURJD_Q_GATE      = True
FOURJD_Q_POS_N     = 96          # 根 1H
FOURJD_Q_POS_MIN   = 30.0        # %
FOURJD_Q_BODY_MIN  = 0.50        # 進場1H K棒實體佔全棒幅


def _gate_count_As(h, l, s, e, pull=0.03):
    """起漲低點 s → 高點 e 之間的 A 數:高點之後先拉回≥pull、再被創新高,那個高點算一個A(同 _lib_1a2a.count_As)。"""
    n = 0; hi = h[s]; pulled = False
    for i in range(s + 1, e + 1):
        if h[i] > hi:
            if pulled: n += 1
            hi = h[i]; pulled = False
        elif l[i] <= hi * (1 - pull):
            pulled = True
    return n


def _gate_rise_leg(h, l, W=48, B=72, pull=0.03):
    """h/l = 已收盤 1H(最後一根=決策時點前最後一根)。回傳 (A數, 漲幅%)。"""
    k = len(h)
    a = max(0, k - W); t = a + int(np.argmax(h[a:k]))
    b = max(0, t - B); s = b + int(np.argmin(l[b:t + 1]))
    if t <= s: return 0, 0.0
    return int(_gate_count_As(h, l, s, t, pull)), float((h[t] / l[s] - 1) * 100)


def _gate_range_pos_short(h4, l4, e, N=30):
    """最近 N 根已收盤 4H 高低區間(含進場價)裡,進場價的位置%(0=最低點)。"""
    hh = h4[-N:]; ll = l4[-N:]
    hi = max(float(np.max(hh)), e); lo = min(float(np.min(ll)), e)
    return float((e - lo) / (hi - lo) * 100) if hi > lo else float("nan")


_BTC24_CACHE: Dict[int, float] = {}


def _btc_24h_change(end_ts: pd.Timestamp) -> float:
    """BTC 永續:截至 end_ts(含)已收盤 15m 的收盤 vs 96 根前。抓不到回 nan(呼叫端放行)。"""
    key = int(end_ts.timestamp())
    if key in _BTC24_CACHE: return _BTC24_CACHE[key]
    try:
        b = fetch_market_candles("BTC-USDT-SWAP", "15m", 300)
        b = b[b.index + pd.Timedelta(minutes=15) <= end_ts]
        v = float((b["close"].values[-1] / b["close"].values[-97] - 1) * 100) if len(b) >= 97 else float("nan")
    except Exception as ex:
        print(f"[BTC24h] 失敗(放行): {ex}"); v = float("nan")
    if len(_BTC24_CACHE) > 64: _BTC24_CACHE.clear()
    _BTC24_CACHE[key] = v
    return v


def _s4h_pivots(hi, lo, pv):
    """左右各 pv 根確認的樞紐（_lib_trendline.pivots 移植）。回傳 (高,低)，元素 (idx, 價, 可用起點)。"""
    n = len(hi); H = []; L = []
    for j in range(pv, n - pv):
        if hi[j] > hi[j-pv:j].max() and hi[j] >= hi[j+1:j+pv+1].max(): H.append((j, float(hi[j]), j+pv))
        if lo[j] < lo[j-pv:j].min() and lo[j] <= lo[j+1:j+pv+1].min(): L.append((j, float(lo[j]), j+pv))
    return H, L


class _S4HTrendLines:
    """趨勢線（_lib_trendline.TrendLines 逐行移植，係數一個都不准改）。
    up 線 = 連兩個低點(後者較高)=支撐；觸及必須是**分離事件**(先離開 tol 再回來才 +1)。"""
    def __init__(self, hi, lo, pv, tol, min_touch, max_age, keep_n):
        self.hi = hi; self.lo = lo; self.pv = pv; self.tol = tol
        self.min_touch = min_touch; self.max_age = max_age; self.keep_n = keep_n
        self.H, self.L = _s4h_pivots(hi, lo, pv)
        self.lines = []; self._ph = 0; self._pl = 0; self._lastH = []; self._lastL = []

    def _add(self, kind, a, b):
        (i1, p1, _), (i2, p2, _) = a, b
        if i2 <= i1: return
        if kind == "up"   and not (p2 > p1): return
        if kind == "down" and not (p2 < p1): return
        self.lines.append(dict(kind=kind, i1=i1, p1=p1, i2=i2, p2=p2,
                               slope=(p2-p1)/(i2-i1), touch=2,
                               born=max(a[2], b[2]), near=False))

    def value(self, ln, i):
        return ln["p1"] + ln["slope"] * (i - ln["i1"])

    def update(self, i):
        while self._ph < len(self.H) and self.H[self._ph][2] <= i:
            p = self.H[self._ph]
            for q in self._lastH[-3:]: self._add("down", q, p)   # ★向右尋找:新樞紐只跟更早的連
            self._lastH.append(p); self._ph += 1
        while self._pl < len(self.L) and self.L[self._pl][2] <= i:
            p = self.L[self._pl]
            for q in self._lastL[-3:]: self._add("up", q, p)
            self._lastL.append(p); self._pl += 1
        keep = []
        for ln in self.lines:
            if i - ln["born"] > self.max_age: continue
            v = self.value(ln, i)
            if v <= 0: continue
            px = self.lo[i] if ln["kind"] == "up" else self.hi[i]
            near = abs(px - v) / v <= self.tol
            if near and not ln["near"]: ln["touch"] += 1
            ln["near"] = near
            keep.append(ln)
        keep.sort(key=lambda x: (-x["touch"], -x["born"]))
        self.lines = keep[:self.keep_n]

    def active(self, i, kind):
        out = []
        for ln in self.lines:
            if ln["kind"] != kind or ln["touch"] < self.min_touch: continue
            if i <= ln["i2"]: continue
            out.append((ln, self.value(ln, i)))
        return out


def _s4h_scan_123_short(hi, lo, cl, op):
    """_lib_123.scan_123(side='short', entry_at='2') 移植。回傳成立的進場 idx 集合。
    1 = 上升支撐線被**實體(收盤)**跌破(官方明說不要進在1,可能假跌破)
    2 = 回調之後不再創新高、且收了黑K → 這才是進場點"""
    n = len(cl)
    tl = _S4HTrendLines(hi, lo, S4H_TL_PV, S4H_TL_TOL, S4H_TL_TOUCH, S4H_TL_MAXAGE, S4H_TL_KEEPN)
    out = set(); st = None
    for i in range(S4H_TL_PV + 2, n):
        tl.update(i)
        if st is None:
            for ln, v in tl.active(i, "up"):
                if cl[i] < v and cl[i-1] >= tl.value(ln, i-1):        # 1 成立
                    a = max(0, i - S4H_TL_PV * 4)
                    st = dict(i1=i, ref=float(hi[a:i+1].max()))
                    break
            continue
        if i - st["i1"] > S4H_TL_WAIT: st = None; continue
        if float(hi[i]) > st["ref"]: st = None; continue              # 創新高=假跌破,作廢
        if cl[i] < op[i] and float(hi[i]) < st["ref"]:                # 2 成立
            out.add(i); st = None
    return out


def _s4h_fib_hit(hi, lo, ev, i) -> bool:
    """該根是否觸及「最近一段已確認下跌腿」的 0.382~0.618 反彈帶。
    ev = _zigzag_hl 的 [(確認idx, 樞紐idx, 價, 'H'/'L')]，依確認時間排序。"""
    legs = [e for e in ev if e[0] <= i]
    if len(legs) < 2: return False
    (c2, j2, p2, t2) = legs[-1]; (c1, j1, p1, t1) = legs[-2]
    if t1 == t2 or i - c2 > S4H_FIB_MAXAGE: return False
    if not (t1 == "H" and t2 == "L"): return False        # 必須是下跌腿(高→低)
    rng = p1 - p2
    if rng <= 0: return False
    zl, zh = p2 + rng * S4H_FIB_LO, p2 + rng * S4H_FIB_HI
    return bool(lo[i] <= zh and hi[i] >= zl)              # ★bool():numpy.bool_ 不是 bool


def _s4h_struct_sl(ev, i, e):
    """停損 = 最近一個在進場價上方(至少 MIN_SL_PCT)的**已確認**樞紐高 ×1.001。"""
    Hs = [x for x in ev if x[0] <= i and x[3] == "H"]
    for (c, j, p, t) in reversed(Hs):
        if p > e * (1 + MIN_SL_PCT): return float(p) * 1.001
    return None


def _s4h_deep_candles(inst_id: str, df_recent: pd.DataFrame) -> pd.DataFrame:
    """把 4H K線加深到 ~1000 根(history-candles 分頁),首次抓完就快取、之後只增量合併。
    ★不是優化,是規格能否被執行的前提 —— 見本區塊開頭的 300/500/1000 根重現率實測。"""
    cur = _S4H_KL_CACHE.get(inst_id)
    if cur is None:
        if _S4H_DEEP_BUDGET["used"] >= S4H_DEEP_PER_ROUND:
            return df_recent                      # 本輪配額用完,先用淺的(條件會自然不成立)
        _S4H_DEEP_BUDGET["used"] += 1
        rows = []; after = None
        for _ in range(max(1, S4H_DEEP_BARS // 100)):
            q = {"instId": inst_id, "bar": "4H", "limit": "100"}
            if after: q["after"] = after
            d = _fetch_okx_public_data("/api/v5/market/history-candles", q)
            if not d: break
            rows += d; after = d[-1][0]
        if rows:
            try:
                k = pd.DataFrame(rows, columns=["ts", "open", "high", "low", "close",
                                                "vol", "volCcy", "volCcyQuote", "confirm"])
                k = k[k["confirm"] == "1"]
                idx = pd.to_datetime(k["ts"].astype("int64"), unit="ms", utc=True)
                cur = pd.DataFrame({c: k[c].astype(float).values
                                    for c in ("open", "high", "low", "close", "vol", "volCcyQuote")}, index=idx)
                cur = cur.sort_index()
            except Exception as e:
                print(f"[S4H] {inst_id} 深抓解析失敗: {e}"); cur = None
    if cur is None: return df_recent
    try:
        keep = [c for c in ("open", "high", "low", "close", "vol", "volCcyQuote") if c in df_recent.columns]
        merged = pd.concat([cur, df_recent[keep]])
        merged = merged[~merged.index.duplicated(keep="last")].sort_index().tail(S4H_DEEP_BARS)
        _S4H_KL_CACHE[inst_id] = merged
        return merged
    except Exception:
        return df_recent


def _check_s4h_short(symbol_item: str, okx_swap_symbol: str):
    """S4H 做空。回傳 (是否成立, 原因, 停損價)。**自己抓 4H**,不吃外面傳進來的時框 df。"""
    if not S4H_SHORT_ENABLED: return False, "", 0.0
    try:
        _S4H_DIAG["呼叫"] += 1
        d4 = fetch_market_candles(okx_swap_symbol, "4H", 300)
        if d4 is None or d4.empty or len(d4) < 60:
            _S4H_DIAG["K棒不足"] += 1; return False, "", 0.0
        df = _s4h_deep_candles(okx_swap_symbol, d4)
        hi = df["high"].values; lo = df["low"].values
        cl = df["close"].values; op = df["open"].values; vol = df["vol"].values
        n = len(cl)
        if n < 120:
            _S4H_DIAG["K棒不足"] += 1; return False, "", 0.0
        i = n - 1                                       # 最新**已收盤** 4h 根
        # ── ① 看跌吞噬(最便宜的條件先擋,省掉後面的趨勢線計算) ──
        if not (cl[i] < op[i] and cl[i-1] > op[i-1] and op[i] >= cl[i-1] and cl[i] <= op[i-1]):
            _S4H_DIAG["非吞噬"] += 1; return False, "", 0.0
        _sl_i = max(0, i - 23)
        # ★2026-09-11 改用 volCcyQuote(USDT成交額)。原本 vol×close 的 vol 是 OKX **合約張數**,
        #   尺度隨合約面值錯 0.01~1e7 倍,跟回測(幣安 vol=幣數)對不上。
        #   重放 2026-05~09 live幣池(_replay_win_ct.py):張數版 n=78 EV+0.058 → USDT版 n=121 EV+0.148;
        #   被舊閘誤放的11筆 EV−0.705、被誤擋的54筆 EV+0.101。1~3月窗兩版相當(+0.81/+0.79)。
        _turn = (df["volCcyQuote"].values if "volCcyQuote" in df.columns else vol * cl)
        if float(np.median(_turn[_sl_i:i+1])) < S4H_MIN_LIQ:
            _S4H_DIAG["流動性"] += 1; return False, "", 0.0
        # ── ② 趨勢線 123（近 6 根 = 24h 內成立過） ──
        z123 = _s4h_scan_123_short(hi, lo, cl, op)
        if not any(i - S4H_123_WIN <= k <= i for k in z123):
            _S4H_DIAG["無123"] += 1; return False, "", 0.0
        ev = _zigzag_hl(hi, lo, S4H_SWING)              # 只含已確認樞紐,無未來函數
        # ── ③ 斐波反彈打折帶 ──
        if not _s4h_fib_hit(hi, lo, ev, i):
            _S4H_DIAG["不在斐波"] += 1; return False, "", 0.0
        # ── ④ LL:最近兩個已確認低點降低 ──
        Ls = [e for e in ev if e[0] <= i and e[3] == "L"]
        if len(Ls) < 2 or not (Ls[-1][2] < Ls[-2][2]):
            _S4H_DIAG["非LL"] += 1; return False, "", 0.0
        # ── 冷卻 / 每日上限 ──
        _bar_ts = int(df.index[i].timestamp())
        if _bar_ts - _S4H_LAST_BAR.get(okx_swap_symbol, 0) < S4H_COOLDOWN_BARS * 14400:
            _S4H_DIAG["冷卻"] += 1; return False, "", 0.0
        _today = datetime.now(timezone.utc).strftime("%Y-%m-%d")
        if _S4H_DAY["day"] != _today: _S4H_DAY.update(day=_today, count=0)
        if _S4H_DAY["count"] >= S4H_DAILY_CAP:
            _S4H_DIAG["每日上限"] += 1; return False, "", 0.0
        # ── 停損 = 最近一個在進場價上方的已確認樞紐高 ──
        e = float(cl[i])                                # 進場≈本根收盤(=下一根4h開盤)
        sl = _s4h_struct_sl(ev, i, e)
        if sl is None or sl <= e:
            _S4H_DIAG["停損無效"] += 1; return False, "", 0.0
        d = (sl - e) / e
        if d < MIN_SL_PCT or d > MAX_SL:
            _S4H_DIAG["停損無效"] += 1; return False, "", 0.0
        _S4H_LAST_BAR[okx_swap_symbol] = _bar_ts
        _S4H_DAY["count"] += 1
        if S4H_SPIKE_GATE:                              # ★2026-09-15 急漲閘(說明見常數區)
            _end = df.index[i] + pd.Timedelta(hours=4)
            d1 = fetch_market_candles(okx_swap_symbol, "1H", 300)
            if d1 is not None and not d1.empty:
                d1 = d1[d1.index + pd.Timedelta(hours=1) <= _end]
                if len(d1) >= 130:
                    _nA, _rise = _gate_rise_leg(d1["high"].values.astype(float), d1["low"].values.astype(float),
                                                pull=S4H_SPIKE_PULL)
                    if _nA <= S4H_SPIKE_MAX_A and _rise >= S4H_SPIKE_RISE:
                        _S4H_DIAG["急漲擋"] += 1
                        print(f"[S4H-Short] {symbol_item} 擋:一口氣急漲上來(A={_nA} 漲{_rise:.1f}%),不空第一個頭", flush=True)
                        return False, "", 0.0
        _S4H_DIAG["成立"] += 1
        return True, f"4h吞噬+趨勢線123+斐波{S4H_FIB_LO}~{S4H_FIB_HI}+LL結構", float(sl)
    except Exception as ex:
        print(f"[S4H-Short] {symbol_item} 判斷失敗: {ex}")
        return False, "", 0.0


# ══════════════════════════════════════════════════════════════════════════════
# ★★4J減速跌破做空 (FOURJD, 2026-09-10 上線) —— 位階2h / 判定與進場1H
# ══════════════════════════════════════════════════════════════════════════════
# 來源:逐張看 4J 直播畫格(`_vid/ajffl_Oh8N4` 等)讀出來的,不是逐字稿推論。
#   s025/s156 兩條到同一條壓力的路徑圖 → 軸1「怎麼到位階的」:一口氣衝到(加速) vs 爬樓梯(減速)
#   s034 他自己 12.189% 吃滿停損那筆 →「前面是從低點一路往上幹,**就算看到影線也不太能去做空**」
#   s142「9月1號…這裡吞沒K嘛,下跌,放空,這裡也成功」(那次是減速上來 → 成功)
#   s141「這裡如果沒有做到空也沒關係,因為**這裡的低點是被跌破的**」→ 右側扳機
#   WatI7fVRovw 04:21/05:36 他的即時決策:「我在觀察他這裡的低點有沒有被跌破」→「低點是沒有(破)」→ 不做空
#   用戶看輸單圖指出「**你輸的那些大多是盤整**」→ 實測 ADX<15 那桶是唯一負的(EV−0.084/吃滿44.4%)
# ── 驗收(全部逐根 live 邏輯,含費0.1%往返) ─────────────────────────────────────
#   ★★滾動前推(**參數挑選本身也納入檢驗**:每段只用它之前的資料挑參數,再跑下一段):
#     樣本外 n=107 EV+0.554 容錯23.9 贏41.1%/保本35.5%/吃滿23.4% 正期7/8
#     bootstrap 95%CI [+0.326,+0.790];參數收斂穩定(9次選擇:2h pv6 8/9、TP2.0 9/9、
#     保本0.8 8/9、量≥2.0 8/9、ADX閘 8/9)——ADX 是被演算法自己選中的,不是事後加的。
#   逐根重放對拍 100%(678/678,多出0);live視窗深度:2h只給120根仍100%、1H給300根ADX誤差0.0000
#   bar內順序(15m重跑出場路徑):結論不同僅0.3%,EV差+0.006 → 1H OHLC 假設無害
#   成本+0.25% 仍 EV+0.264;與現役1H吞噬空重疊率 1.4%(非重複下注);頻率≈1.3筆/天
#   打架(ChatGPT)五條反駁:③樣本獨立性(改每日投組+移動區塊bootstrap 下界仍+0.137)、
#     ④事實問題(23Q4其實 n=0,訓練段實際只有3期)、⑤執行模型(15m已驗)→ 已查;
#     ①多重比較 ②外樣本已看過 → 由滾動前推正面回應。
# ── 已知弱點(不粉飾) ──────────────────────────────────────────────────────────
#   訓練段(23Q4~24Q3)永遠是最弱的一層;24Q1 一直是負的;2022熊市最肥、近期較普通。
FOURJD_SHORT_ENABLED = True
FOURJD_PVH        = 6        # 2h 樞紐左右確認根數(滾動前推 8/9 次選中)
FOURJD_ZW         = 0.004    # 位階帶寬:樞紐高下方 0.4% 為區間帶
FOURJD_TOL        = 0.006    # 觸及容差
FOURJD_MAXWAIT    = 200      # 位階誕生後 200 根 1H 內沒被觸及就丟棄
FOURJD_CW         = 36       # 觸及位階後,盤整窗上限 36 根 1H
FOURJD_EFF_MAX    = 0.15     # 減速:觸及前最後24根的路徑效率(淨位移/路徑長)上限
FOURJD_RISE_MAX   = 0.5      # 減速:那24根的漲幅上限(%)
FOURJD_STEPS_MIN  = 11       # 減速:那24根裡的下跌根數下限(爬樓梯)
FOURJD_TOUCH_MIN  = 2        # ★位階要真的成立:盤整期間觸及位階次數(官方「兩次成立」)
FOURJD_VOLX_MIN   = 2.0      # 進場K成交量 / 近96根中位量
FOURJD_ADX_MIN    = 15.0     # ★用戶看圖抓到的:ADX<15(盤整)那桶是唯一負的
# ★★★2026-09-16 深夜 出場改版(同 ENGULF_SL_MULT 那段的掃描,只用訓練段選、用勝率選,含 MAX_SL 封頂模擬)
#   4JD 現行(TP2R+0.8R保本) 訓+0.386/勝35% 驗+0.333/33% 新幣+0.280/30% **2026 −0.211/勝18%**
#        SL×1.3/TP1R      訓+0.362/**勝69%** 驗+0.230/**63%** 新幣**+0.323**/**66%** **2026 −0.108/勝46%**
#   逐期 10/10 期勝率全部上升(17→47%、32→72%、30→65%、21→75%、7→50%…),n 一筆沒少。
#   ★選 ×1.3 不選 ×1.5:數字幾乎一樣但撞 MAX_SL=12% 上限的比例 15%→7%,回測與 live 落差更小。
#   ★2026 仍是負的(−0.108, bootstrap P(EV>0)=25%),只是從 −0.211 改善一半 —— 別當成它會賺。
FOURJD_SL_MULT    = 1.3      # 停損距離 = 原規格(盤整區最高點×1.0015)距離 × 1.3
FOURJD_TP_R       = 1.0      # ★停利 1R 全平(原 2.0;改版理由見上)
FOURJD_BE_R       = 0.8      # (保留但不再使用:TP=1R 依用戶規則不加保本,exit_strategy 走 fourjd_1r)
FOURJD_BUF        = 0.0015   # 停損 buffer
FOURJD_MIN_LIQ    = 100_000.0  # 近96根成交額中位
FOURJD_COOLDOWN_BARS = 6     # 同幣冷卻 6 根 1H
FOURJD_LOOKBACK   = 210      # 需要的 1H 根數下限(減速24+盤整36+ADX暖機+流動性96)
# ★熔斷(CLAUDE.md 第11條:觀察條款必須寫成代碼,不能只寫註解 —— BPR 就是栽在這)
FOURJD_MAX_CONSEC_SL = 8     # 回測最長連續吃滿停損 = 4 筆;連續 8 筆吃滿 → 自動停用等人工複查
_FOURJD_DIAG = {"呼叫": 0, "K棒不足": 0, "無位階": 0, "未觸及": 0, "非跌破": 0,
                "減速不合": 0, "觸及不足": 0, "量不足": 0, "ADX盤整": 0,
                "流動性": 0, "冷卻": 0, "停損無效": 0, "熔斷": 0, "位置擋": 0, "BTC漲擋": 0, "成立": 0}
_FOURJD_LAST_BAR: Dict[str, int] = {}
_FOURJD_RISK = {"consec_sl": 0, "halted": False}


def _fourjd_adx(h, l, c, n: int = 14):
    """ADX(14)。EWM alpha=1/14;實測給 300 根 1H 與完整歷史誤差 0.0000、門檻不翻轉。"""
    up = pd.Series(h).diff(); dn = -pd.Series(l).diff()
    pdm = np.where((up > dn) & (up > 0), up, 0.0)
    ndm = np.where((dn > up) & (dn > 0), dn, 0.0)
    tr = pd.concat([pd.Series(h - l),
                    (pd.Series(h) - pd.Series(c).shift()).abs(),
                    (pd.Series(l) - pd.Series(c).shift()).abs()], axis=1).max(axis=1)
    atr = tr.ewm(alpha=1 / n, adjust=False).mean()
    pdi = 100 * pd.Series(pdm).ewm(alpha=1 / n, adjust=False).mean() / atr.replace(0, np.nan)
    ndi = 100 * pd.Series(ndm).ewm(alpha=1 / n, adjust=False).mean() / atr.replace(0, np.nan)
    dx = 100 * (pdi - ndi).abs() / (pdi + ndi).replace(0, np.nan)
    return dx.ewm(alpha=1 / n, adjust=False).mean().values


def _fourjd_signal(d1: pd.DataFrame, d2: pd.DataFrame):
    """逐根重放狀態機,只回報「最後一根 1H 是否觸發」。
    ★不跨輪保存狀態:每次用當下視窗整個重建 —— 已實測截成 live 視窗重算 266/266=100%,
      這樣重啟/redeploy 都不會遺失狀態(對比 _oi_history 每次部署歸零的坑)。
    回傳 (成立?, 原因字串, 停損價) 或 (False, 診斷鍵, 0.0)。"""
    hi = d1["high"].values; lo = d1["low"].values
    cl = d1["close"].values; vol = d1["vol"].values
    n = len(cl)
    if n < FOURJD_LOOKBACK:
        return False, "K棒不足", 0.0
    H = d2["high"].values; m = len(H)
    if m < FOURJD_PVH * 2 + 4:
        return False, "K棒不足", 0.0
    # 2h→1H 對齊:每根 1H 對到「最後一根**已收盤**的 2h」(用收盤時間比,不用 ffill)
    k = np.searchsorted((d2.index + pd.Timedelta("2h")).values,
                        (d1.index + pd.Timedelta("1h")).values, side="right") - 1
    AD = _fourjd_adx(hi, lo, cl)
    # ★2026-09-11 成交額改用 volCcyQuote(USDT)。vol 是 OKX 合約張數,vol×close 尺度隨合約面值錯 0.01~1e7 倍。
    #   ★誠實記錄:這刀對 4JD 不是純改善 —— 重放 2026-05~09 張數版 n=61 EV−0.029 → USDT版 n=89 EV−0.144
    #   (多出的34筆 −0.270);1~3月窗 n=20 +0.870 → n=29 +0.762(多出的9筆 +0.535)。兩窗方向相反。
    #   仍改的理由:回測驗證的門檻是 USDT 尺度,張數版是**從沒被驗證過的規格**。
    _turn = d1["volCcyQuote"].values if "volCcyQuote" in d1.columns else vol * cl
    med = pd.Series(_turn).rolling(96).median().values
    vmed = pd.Series(vol).rolling(96).median().shift(1).values
    tgt = n - 1                                   # 只關心最後一根(已收盤)
    act: Dict[int, dict] = {}; seen = set(); last_fire = -10 ** 9
    fail = "未觸及"
    # ★★視窗起點之前就**已確認**的 2h 樞紐,也必須納入 —— 否則 live 只掃 kk-2..kk 三根,
    #   會永久漏掉「視窗開始前確認的位階」。移植對拍實測:不補這段 248/264(漏16筆),補了 264/264。
    #   born 用該樞紐**真正的確認時刻**換算成相對 1H 索引(可為負=視窗外),MAXWAIT 過期才不失真。
    _i0 = FOURJD_PVH * 2
    _kk0 = int(k[_i0]) if _i0 < len(k) else -1
    if _kk0 >= 0:
        _t0 = d1.index[0]
        for j in range(FOURJD_PVH, min(_kk0 - FOURJD_PVH + 1, m - FOURJD_PVH)):
            if j in seen: continue
            if not (H[j] >= H[j - FOURJD_PVH:j].max() and H[j] >= H[j + 1:j + FOURJD_PVH + 1].max()):
                continue
            seen.add(j); top = float(H[j])
            _conf = d2.index[j + FOURJD_PVH] + pd.Timedelta("2h")   # 該樞紐被確認的時刻
            _born = int((_conf - _t0).total_seconds() // 3600)      # 可為負(視窗外確認)
            act[j] = {"top": top, "bot": top * (1 - FOURJD_ZW), "born": _born, "t": None}
    for i in range(FOURJD_PVH * 2, n - 1 + 1):
        kk = int(k[i]) if i < len(k) else -1
        if kk < 0: continue
        # 新確認的 2h 樞紐高(j 要到 j+PVH 那根收盤才看得見 → 無未來函數)
        for q in range(max(FOURJD_PVH, kk - 2), kk + 1):
            j = q - FOURJD_PVH
            if j - FOURJD_PVH < 0 or j + FOURJD_PVH >= m or j in seen: continue
            if not (H[j] >= H[j - FOURJD_PVH:j].max() and H[j] >= H[j + 1:j + FOURJD_PVH + 1].max()):
                continue
            seen.add(j); top = float(H[j])
            act[j] = {"top": top, "bot": top * (1 - FOURJD_ZW), "born": i, "t": None}
        for j1 in list(act.keys()):
            st = act[j1]
            if i - st["born"] > FOURJD_MAXWAIT and st["t"] is None:
                del act[j1]; continue
            if st["t"] is None:
                if float(hi[i]) >= st["bot"] * (1 - FOURJD_TOL): st["t"] = i
                else: continue
            t = st["t"]
            if i - t > FOURJD_CW: del act[j1]; continue
            clo = float(np.min(lo[t:i])) if i > t else float(lo[t])
            # 扳機:收盤跌破盤整區低點(前一根還沒破)
            if not ((i > t + 3) and float(cl[i]) < clo and float(cl[i - 1]) >= clo):
                continue
            a = max(0, t - 24); seg = cl[a:t + 1]
            path = float(np.sum(np.abs(np.diff(seg)))); net = float(seg[-1] - seg[0])
            eff = net / path if path > 0 else float("nan")
            rise = ((float(seg[-1]) - float(seg[0])) / float(seg[0]) * 100
                    if float(seg[0]) > 0 else float("nan"))
            steps = int(np.sum(np.diff(seg) < 0))
            ntouch = int(np.sum(hi[t:i + 1] >= st["bot"]))
            vx = (float(vol[i]) / float(vmed[i])) if (vmed[i] == vmed[i] and vmed[i] > 0) else 0.0
            adxv = float(AD[i]) if AD[i] == AD[i] else 0.0
            zhi = float(np.max(hi[t:i + 1]))
            del act[j1]
            if i != tgt:                       # 不是最後一根:只吃掉冷卻位,不回報
                if not (eff == eff and rise == rise): continue
                if (eff < FOURJD_EFF_MAX and rise < FOURJD_RISE_MAX
                        and steps >= FOURJD_STEPS_MIN and ntouch >= FOURJD_TOUCH_MIN
                        and vx >= FOURJD_VOLX_MIN and adxv >= FOURJD_ADX_MIN
                        and i - last_fire >= FOURJD_COOLDOWN_BARS
                        and med[i] == med[i] and med[i] >= FOURJD_MIN_LIQ):
                    last_fire = i
                continue
            # ── 最後一根:逐層檢查 ──
            # ★★同一根 K 可能有**多個位階**同時觸發扳機(移植對拍實測:AXSUSDT 那根有兩個,
            #   字典順序先撞到的那個減速不合、第二個才是成立的)。所以不合格一律 continue
            #   去看下一個位階,**不准提前 return** —— 回測版用的就是 continue。
            #   寫成 return 會漏掉 16/264 筆(93.9%),這是我自己引入、回測沒有的結構。
            if not (eff == eff and rise == rise): fail = "減速不合"; continue
            if not (eff < FOURJD_EFF_MAX and rise < FOURJD_RISE_MAX
                    and steps >= FOURJD_STEPS_MIN):
                fail = "減速不合"; continue
            if ntouch < FOURJD_TOUCH_MIN: fail = "觸及不足"; continue
            if vx < FOURJD_VOLX_MIN: fail = "量不足"; continue
            if adxv < FOURJD_ADX_MIN: fail = "ADX盤整"; continue
            if not (med[i] == med[i] and med[i] >= FOURJD_MIN_LIQ):
                fail = "流動性"; continue
            if i - last_fire < FOURJD_COOLDOWN_BARS: fail = "冷卻"; continue
            e = float(cl[i]); sl = zhi * (1 + FOURJD_BUF)
            if sl <= e: fail = "停損無效"; continue
            dd = (sl - e) / e
            if dd < MIN_SL_PCT or dd > MAX_SL: fail = "停損無效"; continue
            return True, (f"2h位階減速跌破|效率{eff:.2f} 24根漲{rise:+.2f}% 回檔{steps}根 "
                          f"觸及{ntouch}次 量{vx:.1f}x ADX{adxv:.0f}"), float(sl)
        if i == tgt: break
    return False, fail, 0.0


def _check_fourjd_short(symbol_item: str, okx_swap_symbol: str):
    """4J減速跌破做空。回傳 (是否成立, 原因, 停損價)。★自己抓 1H+2H,不吃外面傳進來的 df。"""
    if not FOURJD_SHORT_ENABLED: return False, "", 0.0
    if _FOURJD_RISK["halted"]:
        _FOURJD_DIAG["熔斷"] += 1; return False, "", 0.0
    try:
        _FOURJD_DIAG["呼叫"] += 1
        d1 = fetch_market_candles(okx_swap_symbol, "1H", 300)
        if d1 is None or d1.empty or len(d1) < FOURJD_LOOKBACK:
            _FOURJD_DIAG["K棒不足"] += 1; return False, "", 0.0
        d2 = fetch_market_candles(okx_swap_symbol, "2H", 300)
        if d2 is None or d2.empty or len(d2) < FOURJD_PVH * 2 + 4:
            _FOURJD_DIAG["K棒不足"] += 1; return False, "", 0.0
        ok, why, sl = _fourjd_signal(d1, d2)
        if not ok:
            _FOURJD_DIAG[why if why in _FOURJD_DIAG else "無位階"] += 1
            return False, "", 0.0
        _bar_ts = int(d1.index[-1].timestamp())
        if _bar_ts - _FOURJD_LAST_BAR.get(okx_swap_symbol, 0) < FOURJD_COOLDOWN_BARS * 3600:
            _FOURJD_DIAG["冷卻"] += 1; return False, "", 0.0
        _FOURJD_LAST_BAR[okx_swap_symbol] = _bar_ts
        _end = d1.index[-1] + pd.Timedelta(hours=1)     # ★2026-09-15 位置閘 + BTC 急漲閘(說明見 S4H 常數區)
        if FOURJD_POS_GATE:
            d4 = fetch_market_candles(okx_swap_symbol, "4H", 60)
            if d4 is not None and not d4.empty:
                d4 = d4[d4.index + pd.Timedelta(hours=4) <= _end]
                if len(d4) >= FOURJD_POS_N:
                    _pos = _gate_range_pos_short(d4["high"].values.astype(float), d4["low"].values.astype(float),
                                                 float(d1["close"].values[-1]), FOURJD_POS_N)
                    if _pos == _pos and _pos < FOURJD_POS_MIN:
                        _FOURJD_DIAG["位置擋"] += 1
                        print(f"[4JD-Short] {symbol_item} 擋:在最近{FOURJD_POS_N}根4H的最底部(位置{_pos:.0f}%)", flush=True)
                        return False, "", 0.0
        if FOURJD_BTC_GATE:
            _b24 = _btc_24h_change(_end)
            if _b24 == _b24 and _b24 > FOURJD_BTC_MAX:
                _FOURJD_DIAG["BTC漲擋"] += 1
                print(f"[4JD-Short] {symbol_item} 擋:BTC 24h 漲 {_b24:+.2f}%", flush=True)
                return False, "", 0.0
        if FOURJD_Q_GATE:      # ★2026-09-16 進場品質閘(說明見 FOURJD_Q_GATE 常數區)
            _h1 = d1["high"].values.astype(float); _l1 = d1["low"].values.astype(float)
            _c1 = d1["close"].values.astype(float); _o1 = d1["open"].values.astype(float)
            if len(_c1) >= FOURJD_Q_POS_N:
                _w = slice(-FOURJD_Q_POS_N, None)
                _rr = float(_h1[_w].max() - _l1[_w].min())
                _qp = float((_c1[-1] - _l1[_w].min()) / _rr * 100) if _rr > 0 else 50.0
                if _qp < FOURJD_Q_POS_MIN:
                    _FOURJD_DIAG["位階低擋"] = _FOURJD_DIAG.get("位階低擋", 0) + 1
                    print(f"[4JD-Short] {symbol_item} 擋:進場在近{FOURJD_Q_POS_N}根1H區間的 {_qp:.0f}%"
                          f"(<{FOURJD_Q_POS_MIN:g}%)=空在殺完的底部", flush=True)
                    return False, "", 0.0
            _rg1 = float(_h1[-1] - _l1[-1])
            _bd1 = abs(float(_c1[-1]) - float(_o1[-1])) / _rg1 if _rg1 > 0 else 0.0
            if _bd1 < FOURJD_Q_BODY_MIN:
                _FOURJD_DIAG["實體不足擋"] = _FOURJD_DIAG.get("實體不足擋", 0) + 1
                print(f"[4JD-Short] {symbol_item} 擋:進場1H實體只有 {_bd1:.0%}"
                      f"(<{FOURJD_Q_BODY_MIN:.0%})", flush=True)
                return False, "", 0.0
        _FOURJD_DIAG["成立"] += 1
        return True, why, float(sl)
    except Exception as ex:
        print(f"[4JD-Short] {symbol_item} 判斷失敗: {ex}")
        return False, "", 0.0


def _fourjd_record_result(is_full_stop: bool):
    """★熔斷計數:連續吃滿停損達 FOURJD_MAX_CONSEC_SL → 自動停用(寫成代碼,不是註解)。"""
    if is_full_stop:
        _FOURJD_RISK["consec_sl"] += 1
        if _FOURJD_RISK["consec_sl"] >= FOURJD_MAX_CONSEC_SL and not _FOURJD_RISK["halted"]:
            _FOURJD_RISK["halted"] = True
            dc_log(f"🛑 **4J減速跌破空 自動熔斷**:連續吃滿停損 {_FOURJD_RISK['consec_sl']} 筆"
                   f"(回測最長 4 筆,門檻 {FOURJD_MAX_CONSEC_SL})。已停止開新倉,需人工複查後重開。")
    else:
        _FOURJD_RISK["consec_sl"] = 0
    save_risk_state()


# ══════════════════════════════════════════════════════════════════════════════
# ★★突破回踩做空 (BOR = Breakout-Retest, 2026-09-13 上線) —— 4h 判定與進場
# ══════════════════════════════════════════════════════════════════════════════
# 來源:用戶 2026-09-13 定調「追價不是不行 但不能追在進了就等停損那種／
#   **進單要嘛支撐進要嘛突破回測進**」。V成型做多已經是「支撐進」(停損=第二個低點=支撐),
#   「跌破前低 → 反彈回測前低(支撐壓力互換) → 才進」在系統裡完全沒有。
# 規格對齊已固化的官方/講者規格,沒有自己發明的係數:
#   4J「實體K突破針尖後等回踩支撐或壓力」→ 跌破用**收盤**不是影線;
#   0831「停損=回踩前的前一個低點」(做空鏡像=反彈段最高點);
#   0830 bug③「突破偵測要用**前一根**算的位階」→ 前低用 i-LOOK-SKIP : i-SKIP 算。
# ── 驗收(12期四層,逐根重放,含費0.1%往返,腳本 _bt_bo_retest.py) ──────────────────
#   ★凍結流程(0828教訓:用全期挑會挑錯):只用訓練段(23Q4~24Q3)**勝率**選 TP
#     (用戶第一訴求是勝率)→ 選到 TP1.0R(訓練勝58%),凍結後樣本外
#     **驗證+0.142 / 新幣+0.196 / 2022+0.232 三層全正**。
#     (改用訓練段EV選會選到TP3.0R → 驗證段−0.005 掛掉,那才是選樣偏差)
#   合計 n=724 勝**61%** EV+0.191 容錯9.6 中位**+0.96R** 吃滿停損39% 正期9/11 頻率≈3.5筆/週
#   block bootstrap(按天) 95%CI [+0.076,+0.300] P(EV>0)=99.9%
#   去尾1%平均+0.196(**不是樂透結構**)、前5大單只佔總R 4%、打亂順序3000次 MDD中位10.0%/p95 14.9%
#   成本敏感度:往返再+0.20% 仍 EV+0.144(停損距中位4.23%,不是薄停損)
#   時框階梯(單調,對上手冊「支撐壓力型時框越大越好」):15m+0.017 / 1h+0.011 / 2h+0.017 / **4h+0.220**
#   live視窗深度:截成300根4h重算 **161/161 = 100%** 重現(門檻100%)
#   重疊率 vs 1H吞噬空 **1.9%**(非重複下注);回測84幣 / live掃79幣 = 涵蓋率約100%
# ── 已知弱點(不粉飾) ──────────────────────────────────────────────────────────
#   22H2熊 EV−0.092(n=77,真弱期);24Q1 −0.191(n=12,低於自訂 n<20 線=只算線索);最長連虧 14 筆。
#   ★做多側**同等力度測過全負**(TP1.0/1.5/2.5 × tol/wait/look/give 共7配置,
#     EV−0.058~−0.140、2022層每格都負) → 本策略**只做空**,不硬掛負EV做多湊「多空都要有」。
#   ★「回踩深度≥N%」當進場閘**不加**:勝率52→56% 但 block bootstrap CI 每檔都含0
#     (下界−0.007~−0.030),且頻率 3.5→1.6 筆/週 = 拿頻率換帳面勝率。
BOR_SHORT_ENABLED = True
BOR_LOOK        = 96      # 前低回看根數(4h)
BOR_SKIP        = 8       # 前低排除最近幾根(確保「前低」是真的舊結構,不是剛形成的)
BOR_WAIT        = 24      # 跌破後等回測的上限根數,超過就作廢
BOR_TOL         = 0.004   # 回測觸及容差(反彈高點碰到 前低×(1-tol) 即算觸及)
BOR_GIVE        = 0.01    # 反彈收盤超過 前低×(1+give) = 跌破失敗,放棄這個結構
BOR_SL_BUF      = 0.001   # 停損 buffer
BOR_MAX_SL_PCT  = 8.0     # ★停損距上限:回測樣本最大 7.99%,而 live 的 MAX_SL=12% 會放行
                          #   我**沒測過**的 8~12% 區間 → 策略自己設 8%(否則凍結規格沒被執行)
BOR_TP_R        = 1.0     # TP 1R 全平(訓練段勝率選出來的;2.5R/3R 的EV更高但勝率掉到38/34%、中位−1.02R)
BOR_COOLDOWN_BARS = 4     # 同幣冷卻 4 根 4h(對齊回測 driver 的 COOLDOWN["4h"]=4)
BOR_DAILY_CAP   = 5       # 每日新倉上限(叢集風控:回測一天最多13筆、≥5筆的有43天)
# ★熔斷(CLAUDE.md 第11條:觀察條款必須寫成代碼 —— BPR 就是栽在只寫註解)
BOR_MAX_CONSEC_SL = 20    # 回測最長連虧 14 筆 → 連續 20 筆吃滿停損自動停用等人工複查
# ★★貼支撐閘(2026-09-14,用戶:「這兩天空單空在支撐區,空了就下不去」)
#   live 09-13~14 BOR 9 筆有 5 筆進場時下方 <0.25R 就有支撐區(回測只 16%),停損的 HYPE/VIRTUAL 都在其中。
#   支撐區=官方進階班灰區定義:影線(高/低樞紐 k=3)多次停在同一水平(±1% 合併、≥2 次觸及),回看 600 根 4H。
#   回測 724 筆(`_bt_bor_support.py`):下方 0~0.25R 有支撐 n=115 勝50% EV−0.037(唯一負桶);無支撐 n=382 勝65% EV+0.284。
#   凍結流程(`_bt_bor_support_wf.py`,只用訓練段勝率選 tol×門檻,且總R不得低於無閘)→ 選中 tol=1%、<0.25R:
#     全體 n 724→605 勝 60.9→63.6% EV +0.191→+0.244 總R +138.1→+147.6;驗/新/22 三層勝率都升(22層總R −1.6)
#     日 block bootstrap:EV差 CI[+0.013,+0.093]、勝率差 CI[+0.7,+4.7]pt 皆>0;總R差 CI 跨 0(P>0=73%)
#     滾動前推(嚴格「總R不降」才選):樣本外 勝 59.4→60.6% EV +0.162→+0.186 總R +69.7→+75.3
#   ★回看深度:live 只給 300 根時效果縮水(EV+0.211) → 用 S4H 同一份 1000 根深抓快取(沒抓到就用 300 根,log 會印)。
#   ★被擋的訊號照樣佔冷卻(回測是先過冷卻才套閘),否則 live 會比回測多出單。
BOR_SUP_GATE    = True
BOR_SUP_MIN_R   = 0.25    # 下方最近支撐區上緣離進場 < 0.25R → 不空
BOR_SUP_TOL     = 0.01
BOR_SUP_K       = 3
BOR_SUP_TOUCH   = 2
BOR_SUP_LOOK    = 600
_BOR_DIAG = {"呼叫":0, "K棒不足":0, "無訊號":0, "停損無效":0, "停損過寬":0,
             "冷卻":0, "每日上限":0, "貼支撐":0, "上影擋":0, "熔斷":0, "成立":0}
# ★2026-09-15 吞噬K上影線閘:扳機那根 4h 看跌吞噬的上影線 ≥ 全幅 20% → 不空
#   選法 = 滾動前推(_night_wf.py,家族與門檻都在迴圈內挑):只用 2026 以前全部資料挑出這條,
#   2026 真樣本外(從沒參與挑選)n=924:留下594筆 勝53% +0.037R / 擋掉330筆 勝46% −0.119R
#     63幣池 留+0.053 擋−0.156、live幣池 留+0.024 擋−0.090(兩池各自成立)
#     留下−擋掉 差 +0.153 95%CI[−0.009,+0.314] P(>0)=96.5%;BTC多頭/空頭時各留63/64%
#   12期(挑選時看過):驗+0.14→+0.26 新+0.18→+0.27 22+0.23→+0.27
#   live 真實單(沒參與挑選):已停損 6 筆 BOR 有 5 筆會被擋(AEON .24/ZAMA .51/DOS .25/HYPE .43/VIRTUAL .23),BICO .19 差一點
#   ★已知弱點:前推逐段挑出的閘不穩定(1/7塊),採用依據是「最終版在2026兩池都成立」,不是前推整體通過。
BOR_UPWICK_GATE = True
BOR_UPWICK_MAX  = 0.20


def _gate_upper_wick(o, h, l, c):
    """上影線佔全幅比例(0~1);全幅為0回0。"""
    rg = float(h) - float(l)
    return float((float(h) - max(float(o), float(c))) / rg) if rg > 0 else 0.0


def _support_below_R(hi, lo, i, entry, sl, k=3, tol=0.01, L=600, min_touch=2):
    """進場當下「下方最近支撐區」離進場幾 R(手抄自回測 `_lib_support_below.support_below`,已對拍)。
    只用 ≤ i 已確認的樞紐(j ≤ i−k)。無支撐區 → (nan, 0)。"""
    a = max(k, i - L); lv = []
    for j in range(a, i - k + 1):
        w0, w1 = j - k, j + k + 1
        if w0 < 0: continue
        if hi[j] == hi[w0:w1].max() and hi[j] > hi[w0:j].max(): lv.append(hi[j])
        if lo[j] == lo[w0:w1].min() and lo[j] < lo[w0:j].min(): lv.append(lo[j])
    lv = sorted(lv); zones = []
    for p in lv:
        if zones and (p - zones[-1]["ref"]) / zones[-1]["ref"] <= tol:
            zones[-1]["hi"] = p; zones[-1]["n"] += 1
        else:
            zones.append({"ref": p, "hi": p, "n": 1})
    risk = sl - entry
    sup = [z for z in zones if z["n"] >= min_touch and z["hi"] < entry]
    if not sup or risk <= 0: return float("nan"), 0
    z = max(sup, key=lambda z: z["hi"])
    return float((entry - z["hi"]) / risk), int(z["n"])
_BOR_LAST_BAR: Dict[str, int] = {}
_BOR_DAY = {"day": "", "count": 0}
_BOR_RISK = {"consec_sl": 0, "halted": False}


def _bo_retest_signal(df: pd.DataFrame):
    """★逐根重放狀態機(手抄自回測 `_bt_bo_retest.signals`,side=short/trig=engulf/sl_mode=retest_low)。
    回傳 [(i, 進場價, 停損價, 前低PH, 反彈高度%)]。
    df 必須是**已收盤**的 4h —— `fetch_market_candles` 結尾是 `iloc[:-1]`,已去掉未完成那根。
    ★對拍:`_chk_bo_port.py` 拿這支跟回測 lib 在同一批 pkl 上逐根比對(S4H 標準:0 不一致才准推)。
    狀態機:IDLE --收盤跌破前低--> BROKE --反彈觸及前低--> RETEST --看跌吞噬--> 進場
    """
    hi = df["high"].values.astype(float); lo = df["low"].values.astype(float)
    op = df["open"].values.astype(float); cl = df["close"].values.astype(float)
    n = len(cl); out = []
    st = "IDLE"; PH = 0.0; t0 = -1; r_i = -1; done = None; done_i = -10 ** 9
    for i in range(BOR_LOOK + BOR_SKIP + 2, n):
        lvl = float(np.min(lo[i - BOR_LOOK - BOR_SKIP:i - BOR_SKIP]))   # ★用前一根為止的資料算
        if st == "IDLE":
            # ★同一個跌破只做一次;done 只保留 BOR_WAIT*4 根就淘汰 ——
            #   不淘汰會讓「回測全窗」與「live 最近300根」的狀態不同(實測會漏 1/94 筆訊號)
            if done is not None and i - done_i <= BOR_WAIT * 4 and lvl >= done * 0.995:
                continue
            if cl[i] < lvl:
                st = "BROKE"; PH = lvl; t0 = i; r_i = -1
            continue
        if i - t0 > BOR_WAIT:
            st = "IDLE"; done = PH; done_i = i; continue      # 等不到回測 → 作廢
        if st == "BROKE":
            if hi[i] >= PH * (1 - BOR_TOL):
                st = "RETEST"; r_i = i                        # ★記回測起始根,停損從這裡起算
            continue
        if cl[i] > PH * (1 + BOR_GIVE):
            st = "IDLE"; done = PH; done_i = i; continue      # 反彈太high=跌破失敗
        if not (cl[i] < op[i] and cl[i] < lo[i - 1] and op[i] >= cl[i - 1]):
            continue                                          # 等看跌吞噬
        e = float(cl[i]); rhi = float(np.max(hi[r_i:i + 1]))
        sl = rhi * (1 + BOR_SL_BUF)                           # 停損=回測段最高點
        st = "IDLE"; done = PH; done_i = i
        if sl > e and (sl - e) / e * 100 <= BOR_MAX_SL_PCT:
            out.append((i, e, sl, PH, (rhi - PH) / PH * 100))
    return out


def _check_bor_short(symbol_item: str, okx_swap_symbol: str):
    """突破回踩做空。回傳 (是否成立, 原因, 停損價)。★自己抓 4H,不吃外面傳進來的 df。"""
    if not BOR_SHORT_ENABLED: return False, "", 0.0
    if _BOR_RISK["halted"]:
        _BOR_DIAG["熔斷"] += 1; return False, "", 0.0
    try:
        _BOR_DIAG["呼叫"] += 1
        df = fetch_market_candles(okx_swap_symbol, "4H", 300)
        if df is None or df.empty or len(df) < BOR_LOOK + BOR_SKIP + BOR_WAIT + 4:
            _BOR_DIAG["K棒不足"] += 1; return False, "", 0.0
        n = len(df)
        cur = [s for s in _bo_retest_signal(df) if s[0] == n - 1]   # ★只在最新已收盤根成立時進場
        if not cur:
            _BOR_DIAG["無訊號"] += 1; return False, "", 0.0
        i, e, sl, PH, deep = cur[0]
        d = (sl - e) / e
        if d < MIN_SL_PCT:
            _BOR_DIAG["停損無效"] += 1; return False, "", 0.0
        if d * 100 > BOR_MAX_SL_PCT or d > MAX_SL:
            _BOR_DIAG["停損過寬"] += 1; return False, "", 0.0
        _bar_ts = int(df.index[i].timestamp())
        if _bar_ts - _BOR_LAST_BAR.get(okx_swap_symbol, 0) < BOR_COOLDOWN_BARS * 14400:
            _BOR_DIAG["冷卻"] += 1; return False, "", 0.0
        _today = datetime.now(timezone.utc).strftime("%Y-%m-%d")
        if _BOR_DAY["day"] != _today: _BOR_DAY.update(day=_today, count=0)
        if _BOR_DAY["count"] >= BOR_DAILY_CAP:
            _BOR_DIAG["每日上限"] += 1; return False, "", 0.0
        _BOR_LAST_BAR[okx_swap_symbol] = _bar_ts          # ★先佔冷卻(被貼支撐閘擋掉也算,對齊回測)
        if BOR_SUP_GATE:
            _deep = _s4h_deep_candles(okx_swap_symbol, df)
            try:
                _j = int(_deep.index.get_loc(df.index[i]))
            except Exception:
                _deep, _j = df, i                            # 對不上就用淺的 300 根
            _sr, _sn = _support_below_R(_deep["high"].values.astype(float), _deep["low"].values.astype(float),
                                        _j, float(e), float(sl), k=BOR_SUP_K, tol=BOR_SUP_TOL,
                                        L=BOR_SUP_LOOK, min_touch=BOR_SUP_TOUCH)
            if _sr == _sr and _sr < BOR_SUP_MIN_R:
                _BOR_DIAG["貼支撐"] += 1
                print(f"[BOR-Short] {symbol_item} 擋:下方 {_sr:.2f}R 就有支撐區(觸{_sn}次,回看{min(_j, BOR_SUP_LOOK)}根)", flush=True)
                return False, "", 0.0
        if BOR_UPWICK_GATE:                              # ★2026-09-15 吞噬K上影線閘(說明見常數區)
            _uw = _gate_upper_wick(df["open"].values[i], df["high"].values[i], df["low"].values[i], df["close"].values[i])
            if _uw >= BOR_UPWICK_MAX:
                _BOR_DIAG["上影擋"] += 1
                print(f"[BOR-Short] {symbol_item} 擋:吞噬K上影線佔{_uw:.2f}(≥{BOR_UPWICK_MAX})", flush=True)
                return False, "", 0.0
        _BOR_DAY["count"] += 1
        _BOR_DIAG["成立"] += 1
        return True, f"突破回踩空(跌破前低{PH:.6g}→反彈回測+{deep:.2f}%→看跌吞噬)", float(sl)
    except Exception as ex:
        print(f"[BOR-Short] {symbol_item} 判斷失敗: {ex}")
        return False, "", 0.0


def _bor_record_result(is_full_stop: bool):
    """★熔斷計數:連續吃滿停損達 BOR_MAX_CONSEC_SL → 自動停用(寫成代碼,不是註解)。"""
    if is_full_stop:
        _BOR_RISK["consec_sl"] += 1
        if _BOR_RISK["consec_sl"] >= BOR_MAX_CONSEC_SL and not _BOR_RISK["halted"]:
            _BOR_RISK["halted"] = True
            dc_log(f"🛑 **突破回踩空 自動熔斷**:連續吃滿停損 {_BOR_RISK['consec_sl']} 筆"
                   f"(回測最長 14 筆,門檻 {BOR_MAX_CONSEC_SL})。已停止開新倉,需人工複查後重開。")
    else:
        _BOR_RISK["consec_sl"] = 0
    save_risk_state()


MTF_BIAS_GATE_ENABLED = True   # ★⚠️2026-08-26 重大訂正:本閘 2026-08-05 上線時的回測依據有**未來函數**。
# 舊腳本(_bt_mtf_bias_concept.py / _bt_mtf_gate_on_strats.py) 用 pandas resample 後 reindex(method="ffill"),
# 而 resample 的索引是K棒的**起始**時間 → 當天的每個小時就已經用到當天日線的最終收盤。
# 修正對齊(只用已收盤那根)後重驗(_diag_mtf_lookahead.py, 19,434筆/7期):
#   概念層 「0個對齊47.3% → 2個都對齊56.6%」(+9.3pt) 縮水成 「49.3% → 50.3%」(**+1.0pt**)。
#   策略層(_bt_oisq_gate_final.py,live忠實C版=現價 vs 已收盤K的EMA20):
#     OISQ多 裸訊號n=370/勝47.6%/容錯11.0 → 加閘 n=298/勝45.3%/容錯8.0 = **變差**。
#     被日線閘擋掉的69筆:勝58.0%/EV+0.386/容錯24.9;留下的301筆:勝45.2%/EV+0.165/容錯8.5
#     → 對多單它擋掉的是**好單**。
#     OISQ空 裸n=138/勝54.3%/容錯18.9 → 加日線閘 n=120/勝56.7%/容錯22.4 = 有幫助(保留)。
#     4H那層只擋1%訊號=冗餘,已拿掉。
# → 現行:**只對空單、只用日線**。多單不再套。
# ★來源說明:用戶的Bisancos「紅綠燈Pro/3+1」指標原始碼受保護=黑盒,本實作是通用概念版,非該指標複刻。
MTF_BIAS_SHORT_ONLY = True     # 2026-08-26:多單套這個閘容錯從11.0掉到8.0(擋掉的69筆勝58%比留下的301筆45%好),只留給空單
MTF_BIAS_DAILY_ONLY = True     # 2026-08-26:4H那層只擋1%訊號=冗餘(擋<10%視為沒作用)
def _mtf_bias_ok(okx_swap_symbol: str, direction: str) -> bool:
    """日線與4H偏見是否都與進場方向同向。偏見=該時框收盤在EMA20之上(多)/之下(空)。
    抓不到資料→回True(放行不擋,與其他濾網一致的保守處理)。"""
    if not MTF_BIAS_GATE_ENABLED: return True
    if MTF_BIAS_SHORT_ONLY and direction != "short": return True   # ★多單不套(實測有害,見上方註解)
    try:
        want = 1 if direction == "long" else -1
        _bars = ("1D",) if MTF_BIAS_DAILY_ONLY else ("1D", "4H")
        for _bar in _bars:
            _d = fetch_market_candles(okx_swap_symbol, _bar, 60)
            if _d is None or _d.empty or len(_d) < 25: return True     # 資料不足→放行
            _c = _d["close"]
            _e = _c.ewm(span=20, adjust=False).mean()
            _b = 1 if float(_c.iloc[-1]) > float(_e.iloc[-1]) else -1
            if _b != want:
                print(f"[偏見閘] {okx_swap_symbol} {direction} 與{_bar}偏見不同向,擋單")
                return False
        return True
    except Exception as _e:
        print(f"[偏見閘] {okx_swap_symbol} 計算失敗(放行): {_e}")
        return True

# ── 4J 結構回踩(2026-08-27 上線,半倉觀察) ─────────────────────────────────
# 出處:YouTube 頻道 4J(SMC/市場結構路線),逐字稿在 _live_transcripts/4J*,規格 _ELITE_COURSE_SPEC.md 13-11/13-12
#   結構定義(0825):「什麼叫結構出現?就是這裡**高點不斷的抬高**…後面有**一根實體K是一個很明顯的突破**」
#   進場(0825):「那要怎麼樣去進場?**就是回踩嘛**…價格跌回來前面高點,這就是**壓力支撐的轉換位**」
#   出場(0825):「頂的部分你可以設**一比一**」
#   盤整不做(0825):「這裡就是**盤整局面**…你在這裡做單是**沒有任何意義的**,而且很容易被洗出去」
#   ★分工(0818):「**大時間級別是代表方向**…我們要看短時間級別,原因是因為我們要**找點位**、找**進場時機**」
#     → 結構與關鍵位在 4H,進場時機在 1H。這一條是關鍵:同一批 setup 用4H收盤進場只有 EV+0.069/容錯3.7,
#       降到1H進場變 EV+0.316/容錯16.4(配對檢定,停損距 6.51%→3.57%,R幾乎翻倍)。
#
# 回測依據(_bt_4j_multiscale.py「B 4H→1H」,10個不重疊期間 2022H1~2025H2,含費0.1%):
#   回測路徑(PV3) n=197 勝率72.1% EV+0.407 PF2.44 容錯20.6🟢 最長連虧5 **10/10期為正** 約56筆/年
#   ★**live路徑實測**(_diag_4j_liveR3.py,同資料跑 live 的程式邏輯,PV3+同setup去重):
#     n=167 勝率71.9% EV+0.410 PF2.45 容錯20.8🟢 最長連虧6 **10/10期正** 訓+0.54/驗+0.30
#     (PV8 舊版是 n=111/+0.295/容錯15.2/連虧4;PV3 換來更多訊號與更高EV,代價是連虧4→6)
#     ★**上線預期用這組數字,不是回測那組**(差異來自 live 是逐根1H判定,進場時辰與回測差幾小時;
#       同資料對帳一致率70.8%、筆數130 vs 125=1.04x,頻率沒問題)
#   ★樣本外壓力段(2022深熊/2023橫盤,完全沒參與參數選擇):22H1深熊 EV+0.829、23H1橫盤 +0.272
#   ★打架查核(ChatGPT+Gemini)全部通過:未來函數0/120違規、bootstrap三抽法5%分位>+0.17、
#     留一期最壞+0.267/留一幣最壞+0.282、K棒內悲觀=樂觀(無同根雙觸)、改下一根開盤進場EV不變、
#     往返成本拉到0.30%仍容錯12.7🟢。與現役策略重疊率僅1.7%(獨立訊號源)。
#   ★已知弱點(所以只給半倉):①ADX>=25 是看到23H1橫盤EV=0.000後才加的,壓力段已被用掉→
#     需要2026年全新holdout;②每幣一年約只有1次訊號(最多的AAVE才9筆/4年)→幣種層級無法下結論,
#     故**不限制幣種**(限幣等於關掉策略);③23Q4-25H2那組幣有存活偏差。
FOURJ_ENABLED       = False  # ★★2026-08-27 當日關閉:**整套回測是未來函數**,實際為負期望。
                             # 打架(ChatGPT)點名「E 要等結構K收盤才知道它『觸位且收回』,但 G 允許在
                             # 該根尚未收完的進場TF K進場 = 用未來才知道的條件回頭挑交易」。實測(_bt_4j_lookahead.py):
                             #   進場窗 k0 從「突破K收盤」改成「**回踩K收盤**」(live 唯一做得到的):
                             #   4H→30m  +0.570/容錯43.2/10-10期 → **-0.229/容錯-13.2/2-5期**,吃滿停損 9.1%→**50.0%**
                             #   2H→15m  +0.560/容錯38.9/10-10期 → **-0.247/容錯-13.6/1-10期**,吃滿停損12.1%→**52.2%**
                             #   (無結構濾網版同樣崩:+0.595→-0.136 / +0.590→-0.172)
                             # → 我2026-08-27報出的**所有** 4J 數字都是未來函數灌水,全部作廢。
                             # 這也解釋了為什麼 live 幾乎不出訊號:live 只能在回踩K收盤後才知道,
                             # 而那時回測定義的進場窗(到 f收盤+1根)已經幾乎過完。
                             # ★重開前置條件:①先有一個**因果版**(只用當下已知資訊)通過7期WF且訓驗同號
                             #   ②該版本要能在真實OKX資料上跑出與回測一致的頻率 ③再走 /結論 + /打架。
                             # ★另:打架也判定「這不是複刻4J的方法」——PV3/24根/回踩0.5/首次突破/冷卻/
                             #   出場(TP1半平+3R+1%保本+時停)/雙時框對 全是我自加,他只講了 BOS→回踩→1:1。
FOURJ_PV            = 3      # ★2026-08-27 當天改 8→3。用戶指正「日內交易也沒那麼少」——0818那集標題
                             #   就叫「最實用的**日內**交易策略」,一年35筆不合理。根因是 PV=8 在4H上
                             #   =「左右各8根=32小時的擺動」,根本不是日內尺度。PV 先前只在「4H結構→4H進場」
                             #   那個(錯的)版本掃過,低階從來沒掃。同一份10期資料實測:
                             #     PV3 n=197 勝率72.1% EV+0.407 容錯20.6 **10/10期正** 56筆/年
                             #     PV5 n=142 66.2% +0.300 15.3 6/8期   40筆/年
                             #     PV8 n=125 68.0% +0.357 18.8 7/7期   35筆/年
                             #   PV3 訊號多60%、EV與容錯都更高、且**十期沒有一期為負**。
                             #   另在獨立的15m資料(7期)網格上,PV3 在每一個時框對都是最好或並列最好 → 非單一格。
# ★2026-08-27 用戶指正「我們從來沒在用atr」——查證屬實:**4J 的13支逐字稿裡 "ATR" 出現 0 次**。
#   他說的是影片01「你的止損就是放在**前面的高點**」,沒有緩衝、沒有ATR。
#   下面三個ATR係數原本都是**我自己加的**,實測(10期,含2022深熊/2023橫盤,含費)全部都在傷害策略:
#     4H→30m 現行(.30/.35/.20) n=313 勝率55.3% EV+0.518 容錯32.8 連虧7 46.0R/年 回撤-5.38R
#            ★全去ATR(0/0/0)  n=528 勝率58.7% EV+0.576 容錯40.1 連虧6 86.4R/年 回撤**-2.87R**
#     2H→15m 現行            n=641 勝率57.4% EV+0.425 容錯26.5 連虧10 77.2R/年 回撤-6.35R
#            ★全去ATR        n=1103 勝率67.3% EV+0.577 容錯38.3 連虧9 **180.7R/年** 回撤-6.24R
#   訊號多70%、勝率更高、EV更高、容錯更高、總R翻倍、回撤更小 → 全部歸零,照他說的做。
#   腳本 _bt_4j_noatr.py。★教訓:把口述規則「量化成ATR倍數」不是中立的,是在改策略。
FOURJ_EXT_ATR       = 0.0    # 突破 = 順向實體K**收盤穿越**,不加任何緩衝
FOURJ_BUF_S         = 0.0    # 回踩 = 價格**觸及**位階,不加容差
FOURJ_BUF_E         = 0.0    # 進場 = 進場TF**觸及**位階,不加容差
FOURJ_RETR_MAX      = 0.50   # 回踩深度上限(突破腿的斐波;官方反覆強調先看0.5)
FOURJ_WAIT          = 24     # 突破後最多等24根4H找回踩
FOURJ_ADX_MIN       = 0.0    # ★2026-08-27 停用(25→0)。這個閘是 PV8 時代為了救「23H1橫盤 EV=0.000」
                             #   才事後加的;改成 PV3 之後那個問題本來就不見了(PV3 在23H1橫盤是
                             #   +0.185/容錯10.0🟡,新兩階更是 +0.370/+0.431 全🟢)。
                             #   PV3 下實測(10期):不過濾 n=364 EV+0.345 容錯17.6 10/10期 103筆/年;
                             #   ADX>=20 n=252 +0.413 容錯21.0 72筆/年;ADX>=25 n=197 +0.407 20.6 56筆/年。
                             #   **總期望值 不過濾最高**(35.5 vs 29.7 vs 22.8 R/年),且容錯仍🟢、期數全正。
                             #   移除一個事後加的參數 = 少一個研究者自由度。保留常數以便隨時回開。
                             # ★用戶提議的「改用支撐壓力區間定義盤整」已測,**不採用**:
                             #   把區間參數調到大級別(箱體寬中位1.6→4.0ATR)後,箱體寬當閘擋掉70~84%訊號
                             #   但EV/容錯全面下降(+0.345/17.6 → +0.217~+0.262/11.1~13.5);
                             #   而「實體突破區間」永遠0筆——與本策略**定義互斥**(突破後回踩進場時,
                             #   價格必然貼著位階,不可能同時收在區間遠側之外)。詳見 _bt_4j_zonechop.py。
FOURJ_SL_LOOKBACK   = 6      # 停損 = 進場TF近6根的**前低/前高**(官方原話,不加ATR緩衝)
# ★出場(2026-08-27 重測後改版)。用戶問「停利怎麼抓的」→ 發現我在 PV8→PV3、換兩階之後
#   **從來沒重測過出場**。10期(含2022深熊/2023橫盤)實測,兩階合併:
#     固定1:1(原本)        EV+0.411 容錯20.6 連虧7  108R/年 最大回撤 -7.6R
#     ★1R半平+保本+3R      EV+0.545 容錯25.1 連虧7  143R/年 最大回撤 -8.7R  ← 採用
#     1R保本後緊跟移動停利   EV+0.731 容錯30.8 連虧7  192R/年 最大回撤 -9.7R  ← 更好但要新寫出場引擎
#     live現成的swing_full EV+0.856 容錯24.3 連虧**16** 225R/年 最大回撤**-18.5R** ← 不採用
#       (它要等pivot確認(f-3且近6根極值)才移損,跟太鬆吃回吐 → 連虧與回撤都翻倍)
#     固定3R              EV+0.718 容錯18.9 連虧16 189R/年 最大回撤 -19.7R ← 不採用
#   官方雖說「一比一」,但他自己也留口:0825「都可以」、影片01「想做1:2、1:3 記得**分批止盈**」。
#   TP1=1R 平一半並自動移保本(bot既有機制),TP2=3R。
FOURJ_TP1_R         = 1.0    # TP1:1R 平50% + 止損移保本(官方的「一比一」保留成第一目標)
FOURJ_TP2_R         = 3.0    # TP2:剩下半倉跑到3R
FOURJ_BE_PCT        = 0.01   # ★2026-08-27 用戶指定「實盤1%的時候要保本」:浮盈達進場價的1%
                             #   就把停損移到保本(含手續費)。實測(兩階合併n=926,10期含2022深熊/橫盤):
                             #     現行(TP1成交才移) 勝率74.1% EV+0.545 容錯25.1 143R/年 回撤-8.73R
                             #     ★+1%保本         勝率58.3% EV+0.490 容錯**31.5**(最高) 129R/年 回撤**-5.99R**
                             #     +0.5%保本        勝率46.7% EV+0.404 容錯28.8 106R/年 -5.68R(掃太兇,連虧16)
                             #     +1.5%保本        勝率63.8% EV+0.502 容錯29.8 132R/年 -6.82R
                             #   EV只掉10%但回撤降31%、容錯升到最高;每單位回撤換到的年報酬 16.4→21.5(+31%)。
                             #   ★這與舊紀錄「保本太早有害」不衝突:那條是對**讓跑型**策略,
                             #     對本策略(TP1半平+固定TP2、緊結構停損)不成立。腳本 _bt_4j_be.py。
FOURJ_OBS_SCALE     = 1.0    # ★2026-08-27 用戶決定全倉(問了兩次,第二次是在保本價修正後)。
                             #   決策當下的數字(兩階合併 n=1631,10期含2022深熊/2023橫盤,含費0.1%,
                             #   保本價已對齊live的+0.1%手續費緩衝):
                             #     EV+0.563 容錯40.5 賺63.2%(平均+1.09R)/保本25.6%(0R)/吃滿停損11.2%
                             #     463筆/年、總期望261R/年、最大回撤-5.59R、單日最差-4.77R
                             #   換算帳戶:半倉(2.5%/筆) 回撤-14.0%/單日-11.9%
                             #           **全倉(5%/筆) 回撤-28.0%/單日-23.9%/單週-22.1%**
                             #   ★我提出過的保留意見(用戶已知並決定照做):
                             #     ①上面只算4J自己,未含 OISQ/MACD/C3/CME 同時曝險
                             #     ②回測不含滑價;保本價就在前一根K的插針範圍內,live被掃機率高於回測
                             #     ③**每日熔斷 DAILY_LOSS_PCT=-30%,擋不住 -23.9% 那種單日**
                             #     ④同日最多20筆訊號;風險預算閘(CONC_FREE=10)把同向總曝險壓在約10R
                             #       = 全倉時帳戶50%,那是結構上限
                             #   ★觀察條款:累計30筆live成交後人工對帳(用EV/容錯/連虧/結果分桶,**不要用勝率**,
                             #     見下方 tf 判定處的註解)。若實際回撤超過 -28% 或吃滿停損比例 >20%(回測11.2%)
                             #     → 先降回 0.5 再查。
# ★三階分形階梯的時框對照(2026-08-27)。★★這行曾在 dcf9cad 被我用「字串區間取代」誤刪,
#   造成 live 每個幣每個時框都丟 NameError(整個bot停擺約1小時,靠 railway logs 才發現)。
#   語法檢查(ast.parse)過得了,因為 NameError 是執行期錯誤 → 之後改 main.py 一律加跑 _chk_names.py。
#   每階規格完全相同,只換時框對;同結構TF的不同進場TF重疊73~93%,故一個結構TF只配一個進場TF。
#   實測(15m資料7期,PV3,不過濾):4H→30m 容錯28.9/96筆年 | 2H→15m 容錯16.3/183筆年
FOURJ_LADDER = {"30m": "4H", "15m": "2H"}
_FOURJ_CACHE: Dict[str, Any] = {}     # f"{instId}|{structBar}" -> (ts, setup_or_None)
# ★同一個 setup 只進一次(2026-08-27 對帳發現):live 是逐根1H判定,同一個回踩位階會被
#   重複觸發;回測是「每個setup最多進一次」。實測差異很大——
#   live無去重 n=130 EV+0.241 容錯12.4 最長連虧6 訓練段-0.03(翻負);
#   live加去重 n=111 EV+0.295 容錯15.2 最長連虧4 訓練段+0.04。→ 必須去重。
_FOURJ_FIRED: Dict[str, float] = {}   # f"{instId}|{dir}|{level}" -> 觸發時間
# ★2026-08-27 儀表:用戶質疑「訊號量還是不太正常,況且4j不用數據」→ 不再用推的,直接量。
#   每輪掃描回報:被評估幾個幣 / 幾個有有效setup / 幾個真的觸發進場。
#   若「評估數」遠小於幣池 → 是掃描沒跑到,不是策略稀有;
#   若「有setup」很多但「觸發」很少 → 卡在進場條件或下游閘。
_FOURJ_STAT: Dict[str, int] = {"eval": 0, "setup": 0, "fire": 0, "nodata": 0}


def _fourj_adx(h, l, c, n=14):
    up = np.diff(h, prepend=h[0]); dn = -np.diff(l, prepend=l[0])
    pdm = np.where((up > dn) & (up > 0), up, 0.0)
    ndm = np.where((dn > up) & (dn > 0), dn, 0.0)
    pc = np.concatenate([[c[0]], c[:-1]])
    tr = np.maximum(h - l, np.maximum(np.abs(h - pc), np.abs(l - pc)))
    _e = lambda x: pd.Series(x).ewm(alpha=1/n, adjust=False).mean().values
    at = _e(tr)
    pdi = 100*_e(pdm)/np.where(at > 0, at, np.nan)
    ndi = 100*_e(ndm)/np.where(at > 0, at, np.nan)
    dx = 100*np.abs(pdi-ndi)/np.where((pdi+ndi) > 0, pdi+ndi, np.nan)
    return _e(np.nan_to_num(dx))


# ★★2026-08-27 回退(58ad2a1 已撤銷)。我曾為了讓 [4J儀表] 的「有setup」不再是0,
#   把「**結構K要觸到位階、又收回位階上方**」這個回踩確認整個拿掉,改成「進場TF觸到就判」。
#   ★那是我自己加的規則,沒有任何回測支撐。丟回同一份10期資料實測(_bt_4j_liverule.py):
#     4H→30m A原規則 n= 528 賺57.0%/吃滿停損 9.1% EV+0.570 容錯43.2 連虧 6 10/10期 回撤  -2.7R
#            B我改的 n= 941 賺32.6%/吃滿停損**29.6%** EV**-0.001** 容錯-0.1 連虧15 5/10期 回撤 **-31.6R**
#     2H→15m A原規則 n=1103 賺66.2%/吃滿停損12.1% EV+0.560 容錯38.9 連虧 9 10/10期 回撤  -6.0R
#            B我改的 n=1935 賺36.2%/吃滿停損**37.3%** EV**-0.083** 容錯-5.7 連虧16 2/10期 回撤**-179.5R**
#   → 「結構K收回確認」是這策略的命門,拿掉即歸零。**已回退為原規則(下方)。**
#   ★★同時訂正我的誤判:「[4J儀表] 有setup 長時間是0」在原規則下**是正常的**——
#     進場窗只有「回踩K收盤後約1根結構K」,窗口本來就窄(回測 4H→30m 才150筆/年)。
#     我把「稀有」誤判成「壞掉」,然後為了修一個不存在的bug去改規則。
def _fourj_setup(okx_swap_symbol: str, struct_bar: str = "4H"):
    """回傳目前**還在進場窗內**的 4H setup:(direction, level) 或 None。
    ★只用**已收盤**的4H K(API 的 iloc[-1] 是未完成K,必須丟掉,見 CLAUDE.md 時框對齊陷阱)。"""
    try:
        _ck = f"{okx_swap_symbol}|{struct_bar}"
        # ★快取TTL要綁結構TF,不能一律900秒(2026-08-27 改成雙階後發現):15m進場每15分鐘評估一次,
        #   TTL=900秒等於每根15m都重抓一次2H K線 → 每小時多 4×幣數 支API呼叫,~100幣就是400+/小時,
        #   再加30m階約200+/小時。OKX限流被打到會**靜默影響其他策略**(不會報錯,只是抓不到資料放行)。
        #   結構K本來2~4小時才換一根,TTL取「結構K長度的一半」就夠新,呼叫量降到1/2~1/4。
        _ttl = 1800 if struct_bar == "2H" else 3600
        _c = _FOURJ_CACHE.get(_ck)
        if _c and time.time() - _c[0] < _ttl:
            return _c[1]
        d = fetch_market_candles(okx_swap_symbol, struct_bar, 300)
        if d is None or d.empty or len(d) < 120:
            _FOURJ_STAT["nodata"] += 1      # ★結構K抓不到/不足 → 這幣本輪等於沒被判定
            _FOURJ_CACHE[_ck] = (time.time(), None); return None
        # ★★2026-08-27 修:這裡原本又寫了一次 d = d.iloc[:-1]。但 `fetch_market_candles` 的
        #   最後一行就是 `return df.iloc[:-1]`(它自己已經丟掉未完成K),於是**一共丟了兩根**——
        #   未完成那根 + 最新那根**已收盤**的。setup 要求回踩落在最後兩根結構K內(f >= n-2),
        #   少一根等於永遠在看 2~4 小時前的舊資料 → 幾乎不可能有 setup 落在窗內。
        #   症狀:[4J儀表] 連續三輪「評估188 結構K不足0 有setup0」,而 OKX 實資料模擬說
        #   setup出現率應有 1.11%(188幣約2個)。是加了儀表才量出來的。
        #   → 不要再丟。fetch_market_candles 回來的 iloc[-1] 已經是**最後一根已收盤**的K。
        hi = d["high"].values.astype(float); lo = d["low"].values.astype(float)
        cl = d["close"].values.astype(float); op = d["open"].values.astype(float)
        n = len(hi)
        pc = np.concatenate([[cl[0]], cl[:-1]])
        tr = np.maximum(hi-lo, np.maximum(np.abs(hi-pc), np.abs(lo-pc)))
        atr = pd.Series(tr).ewm(alpha=1/14, adjust=False).mean().values
        adxv = _fourj_adx(hi, lo, cl)
        pv = FOURJ_PV
        ph = []; pl = []
        for j in range(pv, n-pv):
            if (all(hi[j] > hi[j-k] for k in range(1, pv+1)) and
                    all(hi[j] >= hi[j+k] for k in range(1, pv+1))): ph.append(j)
            if (all(lo[j] < lo[j-k] for k in range(1, pv+1)) and
                    all(lo[j] <= lo[j+k] for k in range(1, pv+1))): pl.append(j)
        # 往回找最近一個「突破→回踩」且**還在進場窗內**的 setup。
        # 進場窗與回測一致:回踩那根4H收盤後再給1根4H(即回踩根 f,窗到 f+1 這根4H結束)。
        for i in range(n-2, max(pv, n-2-FOURJ_WAIT*2)-1, -1):
            a = atr[i]
            if not (a > 0) or not (adxv[i] >= FOURJ_ADX_MIN): continue
            H = [j for j in ph if j <= i-pv]; L = [j for j in pl if j <= i-pv]
            if len(H) < 2 or len(L) < 2: continue
            for d_ in ("long", "short"):
                if d_ == "long":
                    lvl = float(hi[H[-1]])
                    if not (cl[i] > lvl + FOURJ_EXT_ATR*a and cl[i] > op[i] and cl[i-1] <= lvl):
                        continue
                    if not (hi[H[-1]] > hi[H[-2]] and lo[L[-1]] > lo[L[-2]]): continue
                else:
                    lvl = float(lo[L[-1]])
                    if not (cl[i] < lvl - FOURJ_EXT_ATR*a and cl[i] < op[i] and cl[i-1] >= lvl):
                        continue
                    if not (lo[L[-1]] < lo[L[-2]] and hi[H[-1]] < hi[H[-2]]): continue
                f = None
                for g in range(i+1, min(i+FOURJ_WAIT, n)):
                    if d_ == "long":
                        if cl[g] < lvl - FOURJ_BUF_S*atr[g]: break
                        if lo[g] <= lvl + FOURJ_BUF_S*atr[g] and cl[g] > lvl: f = g; break
                    else:
                        if cl[g] > lvl + FOURJ_BUF_S*atr[g]: break
                        if hi[g] >= lvl - FOURJ_BUF_S*atr[g] and cl[g] < lvl: f = g; break
                if f is None: continue
                if f < n-2: continue                       # 進場窗已過(回踩根 + 1根4H)
                if d_ == "long":
                    leg = float(hi[i:f+1].max()) - lvl
                    retr = (float(hi[i:f+1].max()) - cl[f])/leg if leg > 0 else 9.9
                else:
                    leg = lvl - float(lo[i:f+1].min())
                    retr = (cl[f] - float(lo[i:f+1].min()))/leg if leg > 0 else 9.9
                if retr > FOURJ_RETR_MAX: continue
                out = (d_, lvl)
                _FOURJ_CACHE[_ck] = (time.time(), out)
                return out
        _FOURJ_CACHE[_ck] = (time.time(), None)
        return None
    except Exception as _e:
        print(f"[4J] {okx_swap_symbol}/{struct_bar} setup 計算失敗: {_e}", flush=True)
        return None


def _check_4j(symbol_item: str, okx_swap_symbol: str, df: pd.DataFrame, tf_id: str = "1H"):
    """進場時機:價格觸及該結構TF位階(±0.35x進場TF ATR) 且**順向收盤** → 回 (方向, 位階)。
    結構TF 由 FOURJ_LADDER[tf_id] 決定(1H進場配4H結構 / 30m配4H / 15m配2H)。"""
    if not FOURJ_ENABLED: return None
    _sb = FOURJ_LADDER.get(tf_id)
    if not _sb: return None
    try:
        _FOURJ_STAT["eval"] += 1
        st = _fourj_setup(okx_swap_symbol, _sb)
        if st: _FOURJ_STAT["setup"] += 1
        if not st: return None
        d_, lvl = st
        _fk = f"{okx_swap_symbol}|{_sb}|{d_}|{lvl:.10g}"
        _now = time.time()
        for _k in [k for k, v in _FOURJ_FIRED.items() if _now - v > 14*86400]:
            _FOURJ_FIRED.pop(_k, None)                 # 清14天前的紀錄,避免無限膨脹
        if _fk in _FOURJ_FIRED: return None            # ★這個 setup 已經進過,不再進
        hi = df["high"].values; lo = df["low"].values
        cl = df["close"].values; op = df["open"].values
        if len(cl) < 20: return None
        a1 = float(df["atr"].iloc[-1]) if "atr" in df.columns else float(
            pd.Series(np.maximum(hi-lo, np.maximum(np.abs(hi-np.concatenate([[cl[0]], cl[:-1]])),
                      np.abs(lo-np.concatenate([[cl[0]], cl[:-1]]))))).ewm(alpha=1/14,
                      adjust=False).mean().iloc[-1])
        if not (a1 > 0): return None
        if d_ == "long":
            if lo[-1] <= lvl + FOURJ_BUF_E*a1 and cl[-1] > lvl and cl[-1] > op[-1]:
                _FOURJ_FIRED[_fk] = _now; _FOURJ_STAT["fire"] += 1
                return ("long", lvl)
        else:
            if hi[-1] >= lvl - FOURJ_BUF_E*a1 and cl[-1] < lvl and cl[-1] < op[-1]:
                _FOURJ_FIRED[_fk] = _now; _FOURJ_STAT["fire"] += 1
                return ("short", lvl)
        return None
    except Exception as _e:
        print(f"[4J] {symbol_item} 判斷失敗: {_e}", flush=True)
        return None


# ── 資費過熱閘(2026-08-26 上線) ─────────────────────────────────────────────
FUNDING_GATE_ENABLED = True
FUNDING_GATE_PCTL    = 0.75    # 資費 > 該幣過去30天資費的p75 → 擋單(多空皆擋)
FUNDING_GATE_DAYS    = 30
_FUNDING_HIST_CACHE: Dict[str, Any] = {}   # instId -> (ts, overheated_or_None, pctl)

def _funding_overheated(inst_id: str):
    """資費是否高於它**自己的常態帶**(過去30天資費的p75)。回 True/False;資料不足回 None(放行)。

    ★用戶指正(2026-08-26):「資費要看常態處在哪個區間,超過這個區間的才叫資費正或資費負」。
      實測確認(_diag_funding_band.py,27幣/2年):資費中位是**正的**(+0.0082%/8h),負資費只佔20.8%時間
      → 用「>0 = 資費正」會把八成時間都標成正,等於沒有分類。必須用相對常態帶。

    ★回測依據(_bt_oisq_gate_final.py / _bt_fgate_vs_vol.py,7期含費,幣安資費史):
      OISQ多(不套偏見閘) 容錯11.0→13.8🟢 / 勝47.6→50.5%;擋掉20%訊號
      OISQ空(套日線偏見閘) 容錯22.1→27.0🟢 / 勝56.3→59.8%
      12組參數(窗長60/90/120 × 帶p10-90/p20-80/p25-75/p30-70)全部改善=非單一參數依賴
      被擋那批:多單勝36.0%(留下50.5%)、空單勝38.5%(留下58.0%),且總R只有+4.24/−1.39=沒砍到尾部
      訓練/驗證兩段都改善(多 10.1→14.7 / 13.1→15.1;空 17.5→25.3 / 30.2→28.9)
      拔掉最大5筆盈利仍站得住(多11.3/空22.6);bootstrap 5000次 P(EV>0)=100%
      ★不是波動率代理:被擋單的ATR百分位 多0.425 vs 留下0.364、空0.288 vs 留下0.413(**方向相反**),
        量能百分位幾乎一樣(0.933/0.936);純波動率閘只到13.3/24.6,資費閘到15.0/26.8且疊加還能再進步。
      ★這推翻了原影片邏輯(影片:資費正=多頭擁擠=該做空);實測是過熱時**多空都爛**=「這幣現在別碰」。

    ★★已知弱點(必須live驗證):回測用**幣安**資費史,live只能用**OKX**(幣安fapi在Railway被地理封鎖)。
      實測(_dl_okx_funding.py,近3個月17幣,OKX史料上限):兩所資費相關0.66、「過熱」分類只有37%重疊。
      → 本閘屬「概念移植」而非直接移植。每次擋單都印百分位方便事後對帳(觀察條款寫成代碼)。
      重查條件:累積30筆被擋訊號後,用 _rec_analyze 對帳被擋那批是否真的較差;若無差異則關掉本閘。
      另:OKX 部分合約是4H結算(非8H),故用**時間窗(30天)**而非固定筆數取帶。
    """
    if not FUNDING_GATE_ENABLED:
        return None
    try:
        _c = _FUNDING_HIST_CACHE.get(inst_id)
        if _c and time.time() - _c[0] < 3600:
            return _c[1]
        rows = _fetch_okx_public_data("/api/v5/public/funding-rate-history",
                                      {"instId": inst_id, "limit": "100"})
        recs = []
        for r in (rows or []):
            try:
                recs.append((int(r.get("fundingTime") or 0), float(r.get("fundingRate"))))
            except Exception:
                continue
        recs.sort()
        if len(recs) < 20:
            _FUNDING_HIST_CACHE[inst_id] = (time.time(), None, None)
            return None
        cur_ts, cur = recs[-1]
        cutoff = cur_ts - FUNDING_GATE_DAYS * 86400 * 1000
        hist = [v for (t, v) in recs[:-1] if t >= cutoff]      # 常態帶只用過去,不含當筆
        if len(hist) < 20:
            _FUNDING_HIST_CACHE[inst_id] = (time.time(), None, None)
            return None
        _h = np.asarray(hist, dtype=float)
        # ★2026-08-26 實測修正:OKX 對多數山寨的資費**釘在 0.01%/8h 上限**(幣安是連續變動的,
        #   回測沒暴露這問題)。嚴格 cur > p75 在「p75 也等於上限」時永遠不成立 →
        #   實測15幣有7幣百分位=100 卻全部放行,擋單率只有7%(回測預期~20%)。
        #   改用 midrank(並列取中位排名),並要求分布有離散度(全部釘在同一值時無從判斷→放行)。
        if float(_h.max() - _h.min()) <= 0:
            _FUNDING_HIST_CACHE[inst_id] = (time.time(), None, None)
            return None
        pctl = float(((_h < cur).mean() + (_h <= cur).mean()) / 2.0 * 100)
        hot = bool(pctl >= FUNDING_GATE_PCTL * 100)
        _FUNDING_HIST_CACHE[inst_id] = (time.time(), hot, pctl)
        if hot:
            print(f"[資費閘] {inst_id} 現資費{cur*100:.4f}%/結算 落在30天分布的第{pctl:.0f}百分位 "
                  f"(門檻{FUNDING_GATE_PCTL*100:.0f}) → 過熱擋單", flush=True)
        return hot
    except Exception as _fe:
        print(f"[資費閘] {inst_id} 計算失敗(放行): {_fe}", flush=True)
        return None


# ── 反向 S/R 區間閘(2026-08-26 上線) ────────────────────────────────────────
SR_ZONE_GATE_ENABLED = True
SR_ZONE_PV        = 3      # 樞紐左右確認根數(1H)
SR_ZONE_TURN_ATR  = 1.5    # 官方「曾經有明顯轉折(爆漲或暴跌)」:離開樞紐後 conf 根內走幅 >= 1.5×ATR
SR_ZONE_CONF      = 8      # 轉折確認窗(根)
SR_ZONE_MERGE_ATR = 0.6    # 群聚容差:錨點距離 <= 0.6×ATR 視為同一區
SR_ZONE_MIN_TOUCH = 2      # 官方「兩次成立,三次確認」→ 最低2次
SR_ZONE_BUF_ATR   = 0.3    # 區間外緣容差
SR_ZONE_BARS      = 900    # 1H 回看根數(約37天)
# ★2026-08-26 生命週期(對齊回測 ZoneState 的 keep/max_age;沒有這兩個 live 會留下900根內的
#   **所有**區間→區間過密→「停損側最近的同向區間」永遠貼著價格→止損位置閘實測擋單率0%
#   (回測27%/22%)。回測預期擋單率 vs live實際對不上 = 實作有bug,不是策略問題。)
SR_ZONE_KEEP      = 12     # 最多保留幾個區(依最近觸及排序)
SR_ZONE_MAX_AGE   = 1200   # 區間存活根數(超過未被觸及就淘汰)
_SR_ZONE_CACHE: Dict[str, Any] = {}   # instId -> (ts, zones)

def _sr_zones_1h(inst_id: str):
    """依官方講義定義建 1H 支撐/壓力**區間**(不是一條線)。回 list[dict(kind,lo,hi,touches)]。

    ★來源:菁英交易學院「傳統技術分析 步驟2 支撐與壓力」講義(規格抄錄在
      trading-backtest/_ELITE_COURSE_SPEC.md 第二節),原文要點:
        「支撐與壓力不是一條線,而是一個『區域』」
        畫法:找**明顯影線** → 觸及越多次越好 → **兩次成立,三次確認** → 時間週期越大越有效
        有效性:**曾經有明顯轉折(爆漲或暴跌)** 且 **測試過不只一次**
        用矩形畫區間的理由:「初學者常畫一條超精準的線,結果價格突破就誤認為可以交易,
        但事實上他遇到了假突破」→ 區間是為了容錯
    實作:①用**影線極值**(high/low)當錨點 ②樞紐左右各 PV 根確認 ③離開樞紐後 CONF 根內
         走幅 >= TURN_ATR×ATR 才算「明顯轉折」 ④錨點距離 <= MERGE_ATR×ATR 併成同一區
         ⑤區間 = [最低錨點, 最高錨點],touches = 併入的錨點數
    """
    try:
        _c = _SR_ZONE_CACHE.get(inst_id)
        if _c and time.time() - _c[0] < 1800:
            return _c[1]
        rows = []
        after = None
        for _ in range(4):                      # 4×300=1200根上限
            q = {"instId": inst_id, "bar": "1H", "limit": "300"}
            if after: q["after"] = after
            r = _fetch_okx_public_data("/api/v5/market/history-candles", q)
            if not r: break
            rows += r
            after = r[-1][0]
            if len(rows) >= SR_ZONE_BARS: break
        if len(rows) < 300:
            _SR_ZONE_CACHE[inst_id] = (time.time(), None); return None
        arr = sorted(({"ts": int(x[0]), "o": float(x[1]), "h": float(x[2]),
                       "l": float(x[3]), "c": float(x[4])}
                      for x in rows), key=lambda z: z["ts"])
        hi = np.array([x["h"] for x in arr]); lo = np.array([x["l"] for x in arr])
        cl = np.array([x["c"] for x in arr]); op = np.array([x["o"] for x in arr])
        bt = np.maximum(op, cl); bb = np.minimum(op, cl); n = len(arr)
        pc = np.concatenate([[cl[0]], cl[:-1]])
        tr = np.maximum(hi - lo, np.maximum(np.abs(hi - pc), np.abs(lo - pc)))
        atr = pd.Series(tr).ewm(alpha=1/14, adjust=False).mean().values
        pv, cf = SR_ZONE_PV, SR_ZONE_CONF
        zones = []
        def _add(price, kind, a, j):
            # ★2026-08-26 依官方「繪製五大重點」第④點修正:**盡量不要畫到實體**。
            #   校正原話(傳統技術分析_2):「我們前面壓力抓的範圍是**影線**…支撐也可以套用這個邏輯,
            #   **把區間拉大畫到影線的範圍**」。原版用單一價格當錨點,區間會橫跨實體(等於畫錯)。
            #   新版:錨點=該K棒的影線段(壓力=[實體上緣,最高價];支撐=[最低價,實體下緣]);
            #   合併後 壓力區下緣取「所有錨點實體上緣的最大值」→ 保證不吃進任何錨點K的實體。
            #   回測(_bt_zone_on_live.py,7期):OISQ多 容錯14.1→16.9(6/7期正)、OISQ空 25.1→27.0(4/4)。
            w_lo, w_hi = (bt[j], hi[j]) if kind == "res" else (lo[j], bb[j])
            w_lo = float(w_lo); w_hi = float(w_hi)   # ★numpy.float64 會讓下游比較回 numpy.bool_
            if w_hi <= w_lo: w_lo = w_hi = price
            tol = SR_ZONE_MERGE_ATR * a
            for z in zones:
                if z["kind"] == kind and (w_hi >= z["lo"] - tol) and (w_lo <= z["hi"] + tol):
                    z["wlo"].append(w_lo); z["whi"].append(w_hi)
                    if kind == "res":
                        z["lo"] = max(z["wlo"]); z["hi"] = max(z["whi"])
                    else:
                        z["lo"] = min(z["wlo"]); z["hi"] = min(z["whi"])
                    if z["hi"] < z["lo"]: z["lo"], z["hi"] = z["hi"], z["lo"]
                    z["touches"] += 1; z["last_j"] = j; return
            zones.append({"kind": kind, "lo": w_lo, "hi": w_hi, "touches": 1,
                          "wlo": [w_lo], "whi": [w_hi], "last_j": j})
        for j in range(pv, n - pv - cf):
            a = atr[j]
            if a > 0:
                e = min(j + cf, n - 1)
                if (all(hi[j] > hi[j-k] for k in range(1, pv+1)) and
                        all(hi[j] >= hi[j+k] for k in range(1, pv+1)) and
                        (hi[j] - lo[j+1:e+1].min()) >= SR_ZONE_TURN_ATR * a):
                    _add(float(hi[j]), "res", a, j)     # ★錨點=上影線段(不畫到實體)
                if (all(lo[j] < lo[j-k] for k in range(1, pv+1)) and
                        all(lo[j] <= lo[j+k] for k in range(1, pv+1)) and
                        (hi[j+1:e+1].max() - lo[j]) >= SR_ZONE_TURN_ATR * a):
                    _add(float(lo[j]), "sup", a, j)
            # ★逐根淘汰(與回測 ZoneState.update 同結構):先汰舊、再依最近觸及留前 KEEP 個。
            #   淘汰在 min_touches 過濾**之前**,與回測一致(1觸的區也佔名額)。
            if zones:
                zones = [z for z in zones if j - z["last_j"] <= SR_ZONE_MAX_AGE]
                zones.sort(key=lambda z: -z["last_j"])
                del zones[SR_ZONE_KEEP:]
        zones = [z for z in zones if z["touches"] >= SR_ZONE_MIN_TOUCH]
        # 支壓互換(官方第8頁):價已站到另一側 → 角色翻轉
        last = float(cl[-1]); atr_now = float(atr[-1])
        for z in zones:
            if z["kind"] == "sup" and last < z["lo"]: z["kind"] = "res"
            elif z["kind"] == "res" and last > z["hi"]: z["kind"] = "sup"
        out = {"zones": zones, "atr": atr_now, "last": last}
        _SR_ZONE_CACHE[inst_id] = (time.time(), out)
        return out
    except Exception as _ze:
        print(f"[S/R區間] {inst_id} 計算失敗(放行): {_ze}", flush=True)
        return None

SR_SL_POS_GATE_ENABLED = True   # ★2026-08-26 止損位置閘(見 _sr_sl_position_ok)

def _sr_sl_position_ok(inst_id: str, direction: str, entry: float, sl: float):
    """★止損不得卡在「停損側最近的同向區間」內部或另一側。回 True/False/None(無資料→放行)。

    出處(菁英交易學院 直播 0825,作者本人檢討學員單):
      「下面就是一個**支撐位**,結果他把**止損守在這個支撐位上方**,那怎麼合理?…
        這邊明確他是一根針兩個針三根針然後假跌破…**要設止損應該是設下面這邊**」
      → 多單止損必須在最近支撐區的**下緣之下**;空單止損必須在最近壓力區的**上緣之上**。
        止損落在區間裡=把停損放在「本來就會被插針測試」的位置,等於送分給掃損。

    回測依據(_bt_zone_room.py,7期,含費,**增量檢定**:基準已含現行反向S/R區間閘):
      OISQ多 n197→143(再擋27%) 容錯 16.9→**19.6** EV+0.320→+0.349 正期 6/7 不變
      OISQ空 n 63→ 49(再擋22%) 容錯 27.0→**28.7** EV+0.539→+0.551 正期 4/4 不變
      逐期:空單7期中5期改善;多單4改善3變差(24Q2 +0.344→+0.055 是最差的一期)→
      所以這層是「容錯/勝率」的改善(多單勝率51.8→53.1、空單65.1→67.3),不是EV暴增,別過度期待。
    ★同時測過但**不採用**的「上方空間閘」(room_R>=1.0,直播0813「上方空間很大」):
      與本閘和反向區間閘重疊52%,多單只換到容錯+1.0pt卻再擋48%訊號,不划算;
      空單看似+6.5pt但n只剩34筆/7期,樣本不足以支撐。留待live累積樣本後重評。
    """
    if not SR_SL_POS_GATE_ENABLED: return None
    d = _sr_zones_1h(inst_id)
    if not d or not d.get("zones"): return None
    px = d["last"]
    want = "sup" if direction == "long" else "res"
    near = None
    for z in d["zones"]:
        if z["kind"] != want: continue
        if direction == "long" and z["hi"] < px:
            if near is None or z["hi"] > near["hi"]: near = z
        elif direction == "short" and z["lo"] > px:
            if near is None or z["lo"] < near["lo"]: near = z
    if near is None: return None                    # 停損側沒有同向區間 → 無從違規
    # ★bool() 不可省:區間邊界若是 numpy.float64,比較會回 numpy.bool_,
    #   呼叫端的 `is False` **永遠不成立** → 閘門形同不存在(2026-08-26 用真實OKX資料驗觸發率抓到)。
    return bool(sl < near["lo"]) if direction == "long" else bool(sl > near["hi"])


def _sr_reverse_zone_hit(inst_id: str, direction: str):
    """進場價是不是正撞在**反向區間**上(做多撞壓力區 / 做空撞支撐區)→ 該擋。
    回 True=撞到該擋 / False=沒撞 / None=資料不足(放行)。

    ★回測依據(_bt_zone_on_live.py + _bt_zone_sens.py,7期含費,疊在 live 現況之上):
      OISQ多 容錯 14.1 → 16.8(勝50.0→49.1%,EV+0.258→+0.348,擋45%,正期6/7不變,訓+0.29/驗+0.39)
      OISQ空 容錯 25.1 → 28.0(勝59.0→62.7%,EV+0.498→+0.540,擋41%,訓+0.53/驗+0.55)
      **參數敏感度:turn_atr(1.0/1.5/2.0)×pv(2/3/4)×觸及(2/3)×容差(0/0.3/0.6) 共30組全部改善**,
      訓練/驗證兩段皆正,獲利集中度不惡化(多 10%→15%)=非曲線擬合。
      取網格正中間 turn1.5/pv3/觸及2/容差0.3 上線(不挑最好看的那組)。
    ★這條就是用戶反覆抱怨「一直空在4H和日線大支撐,這種單一直虧,講幾百次了」的代碼化解法。
    """
    if not SR_ZONE_GATE_ENABLED: return None
    d = _sr_zones_1h(inst_id)
    if not d or not d.get("zones"): return None
    want = "res" if direction == "long" else "sup"
    px = d["last"]; buf = SR_ZONE_BUF_ATR * d["atr"]
    for z in d["zones"]:
        if z["kind"] != want: continue
        if (z["lo"] - buf) <= px <= (z["hi"] + buf):
            print(f"[S/R區間] {inst_id} {direction} 撞到反向{'壓力' if want=='res' else '支撐'}區 "
                  f"[{z['lo']:.6g}~{z['hi']:.6g}] 觸及{z['touches']}次 → 擋單", flush=True)
            return True
    return False


OI_SQUEEZE_ENABLED = True   # ★2026-07-07迭代重開(多空雙向,見_check_oi_squeeze開頭+上方門檻常數註解):
                            # 原全市值多空合測n=14太小不可信;拆開4象限(多空×主流山寨)找到各自鬆門檻版本
                            # 空:OI2%/vol2.0x n=125/EV+0.222/PF1.80/5-7期正;多:OI1.5%/vol2.25x n=377/EV+0.103/PF1.41/6-7期正
                            # 兩邊都掃過12組鄰近參數確認穩定。比照C3空OIv2上線慣例:
                            # 觀察前20-30張真實成交,對照預期EV不達標即關。
# 主力建倉壓縮突破(1H,2026-06-13):12h壓縮<3%+帶量突破+OI升+4H regime,讓跑
# ★2026-07-07訂正:先前只給空單做12組(OI×vol)鄰近參數網格、多單只草草試4次就判死刑不公平。
# 補測多單同一套網格後發現多單其實也有能用版本,兩邊都開,各自獨立門檻:
#   空單: OI>=2% + 帶量>=2.0x → n=125/EV+0.222/PF1.80/5-7期正
#   多單: OI>=1.5% + 帶量>=2.25x → n=377/EV+0.103/PF1.41/6-7期正(比空單一致性還好),每期n=32-121不算薄
#   兩邊都掃過鄰近12組參數確認非單一運氣格。
OI_SQUEEZE_OI_RISE_MIN_SHORT = 0.02
OI_SQUEEZE_VOL_MULT_SHORT = 2.0
OI_SQUEEZE_OI_RISE_MIN_LONG = 0.015
OI_SQUEEZE_VOL_MULT_LONG = 2.25

def _check_oi_squeeze(symbol_item: str, okx_bar_fmt: str, df: pd.DataFrame, okx_swap_symbol: str):
    """主力建倉壓縮突破(1H):12h窄幅壓縮<3% + 帶量突破range(噴出) + 12h OI升(建倉) + 4H regime順向。
    coiled spring:壓得越緊彈越大。回 'long'/'short'/None。
    ★2026-07-07:多空各自迭代出獨立門檻(見上方常數),空單OI2%/vol2.0x、多單OI1.5%/vol2.25x,
    都用12組鄰近參數網格驗證過穩定非單一運氣格,詳見_bt_oisq_iterate*.py。"""
    try:
        hi = df["high"].values; lo = df["low"].values; cl = df["close"].values; vol = df["vol"].values
        if len(cl) < 25: return None
        rh = float(hi[-13:-1].max()); rl = float(lo[-13:-1].min())
        if rl <= 0 or (rh - rl) / rl > 0.03: return None              # 12h壓縮<3%(coiled spring)
        side = "long" if cl[-1] > rh else ("short" if cl[-1] < rl else None)
        if side is None: return None
        va = float(np.mean(vol[-21:-1]))
        _vol_mult = OI_SQUEEZE_VOL_MULT_SHORT if side == "short" else OI_SQUEEZE_VOL_MULT_LONG
        if not (va > 0 and vol[-1] > _vol_mult * va): return None     # 帶量突破=噴出
        cona = CONA_PERP.get(symbol_item)
        if not cona: return None
        _e = int(time.time() * 1000); _s = _e - (BAR_SECONDS["1H"] * 16 * 1000)
        oi = fetch_open_interest_series(cona, okx_bar_fmt, _s, _e)
        if len(oi) < 13 or oi.iloc[-13] <= 0: return None
        _oi_min = OI_SQUEEZE_OI_RISE_MIN_SHORT if side == "short" else OI_SQUEEZE_OI_RISE_MIN_LONG
        if (oi.iloc[-1] - oi.iloc[-13]) / oi.iloc[-13] < _oi_min: return None   # 12h OI升(建倉)
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


# ── BPR(Balanced Price Range,失衡區重合) 1H續勢(2026-07-08,Bisancos SMC課程挖掘) ──────────
# 上漲FVG(3根K缺口)與下跌FVG重疊區,價格回測中軸,順「較晚形成那個FVG」的方向進場,SL=zone遠端。
# 忠實對齊 _bt_fvg_all.py find_fvgs/find_bpr_zones/simulate_trade_C,疊限主流幣+4H EMA200 regime對齊
# (_bt_freq_reduce_fvg_bpr.py驗證:不加regime只有頻率17.5筆/天,加了regime連EV都變好非只是砍量)。
# WF驗證(major+regime,train23q4-24q3/verify bull-oos-alt同號):merged EV+0.200/勝60.7%/PF1.44/
# n=1811/7期正,頻率2.4筆/天(15幣)。跟現役OI_SQUEEZE訊號重疊率僅0.9%,非重複下注同一批單。
# 出場固定1.5R(非trail,trail版本在SNR平行測試證實有尾端假象風險,BPR保守用回測驗證過的固定R版本)。
# ★live風險:regime(4H EMA200斜率)在盤整期可能live/backtest有落差,尚未有live樣本驗證。
#   上線先給觀察倉位+熔斷:前20-30筆勝率<45%或連續虧損>8筆(回測最長記錄)即應關閉重查。
BPR_ENABLED = False  # ★2026-07-19關閉:14天真實對帳=bot最大流血源(空61筆勝率32.8%虧-6.1U+多30筆-0.6U);
# live頻率22.7筆/天=上線壓測宣稱2.4的10倍,主因_check_bpr zone成交後不消耗、價格離開再回來同zone重複觸發
# (回測simulate_trade_C每zone只進一次);且live勝率32.8% vs 出場照規格重演48.6%=執行落差未查明。
# 觀察期條款(連虧>8筆暫停重查)已觸發。重開前置條件:①加traded-zone消耗 ②查為何BPR固定1.5R單會放好幾天
# ③重演腳本_live_replay_bpr.py驗證頻率回到~2-3筆/天。
BPR_MIN_GAP_PCT = 0.0005     # zone最小寬度(相對價格),濾雜訊微缺口,同回測MIN_GAP_PCT
BPR_MAX_ZONE_AGE = 200       # FVG/BPR zone存活多少根K線內等待回測,同回測MAX_ZONE_AGE
BPR_MAX_DIST = 30            # 上下FVG視為「重疊」的最大K線距離,同回測BPR_MAX_DIST
BPR_OBSERVATION_SCALE = 0.5  # ★觀察倉期倉位縮放(全新策略無live驗證,先半倉;達標後改回1.0,見上方進場區塊註解)


def _bpr_find_fvgs(hi, lo, cl, n):
    """忠實對齊 _bt_fvg_all.py find_fvgs():回傳list of dict(i2,top,bot,mid,direction,violated,viol_bar)。"""
    fvgs = []
    for i2 in range(2, n):
        i1 = i2 - 2
        if hi[i1] < lo[i2]:
            top, bot = lo[i2], hi[i1]
            gap_pct = (top - bot) / cl[i1] if cl[i1] > 0 else 0
            if gap_pct >= BPR_MIN_GAP_PCT:
                fvgs.append(dict(i2=i2, top=top, bot=bot, mid=(top+bot)/2,
                                  direction='up', violated=False, viol_bar=None))
        if lo[i1] > hi[i2]:
            top, bot = lo[i1], hi[i2]
            gap_pct = (top - bot) / cl[i1] if cl[i1] > 0 else 0
            if gap_pct >= BPR_MIN_GAP_PCT:
                fvgs.append(dict(i2=i2, top=top, bot=bot, mid=(top+bot)/2,
                                  direction='down', violated=False, viol_bar=None))
    fvgs.sort(key=lambda z: z['i2'])
    return fvgs


def _bpr_mark_violations(fvgs, hi, lo, cl, n):
    """同回測mark_violations():zone收盤穿越遠端即標記失效,避免對已作廢的zone觸發進場。"""
    for z in fvgs:
        start = z['i2'] + 1
        end = min(start + BPR_MAX_ZONE_AGE, n)
        for f in range(start, end):
            if z['direction'] == 'up':
                if cl[f] < z['bot']:
                    z['violated'] = True; z['viol_bar'] = f; break
            else:
                if cl[f] > z['top']:
                    z['violated'] = True; z['viol_bar'] = f; break


def _bpr_find_zones(fvgs):
    """同回測find_bpr_zones():上FVG與下FVG重疊區,方向取「較晚形成」那個FVG的方向。"""
    bprs = []
    ups = [z for z in fvgs if z['direction'] == 'up']
    downs = [z for z in fvgs if z['direction'] == 'down']
    for u in ups:
        for d in downs:
            if abs(u['i2'] - d['i2']) > BPR_MAX_DIST:
                continue
            lo_ov = max(u['bot'], d['bot']); hi_ov = min(u['top'], d['top'])
            if hi_ov <= lo_ov:
                continue
            later = u if u['i2'] > d['i2'] else d
            mid = (lo_ov + hi_ov) / 2
            start_bar = max(u['i2'], d['i2']) + 1
            bprs.append(dict(top=hi_ov, bot=lo_ov, mid=mid, direction=later['direction'],
                              start_bar=start_bar, violated=False, viol_bar=None))
    return bprs


def _check_bpr(symbol_item: str, okx_bar_fmt: str, df: pd.DataFrame, okx_swap_symbol: str):
    """BPR續勢(1H,限主流+4H EMA200 regime對齊)。回傳 (direction, zone_bot, zone_top) 或 None。
    ★live簡化:回測用intrabar觸及zone中點的精確價成交,live用「該根K線收盤價」近似成交
    (跟回測理論進場價可能有微小落差,屬已知的live/backtest fidelity gap,靠live訊號log+週回放比對追蹤)。"""
    try:
        if symbol_item not in MAJOR_COINS: return None
        hi = df["high"].values; lo = df["low"].values; cl = df["close"].values
        n = len(cl)
        if n < 60: return None
        a = max(0, n - 260)
        hi_w = hi[a:]; lo_w = lo[a:]; cl_w = cl[a:]; nw = len(cl_w)
        fvgs = _bpr_find_fvgs(hi_w, lo_w, cl_w, nw)
        if not fvgs: return None
        _bpr_mark_violations(fvgs, hi_w, lo_w, cl_w, nw)
        bprs = _bpr_find_zones(fvgs)
        if not bprs: return None
        cur = nw - 1
        hit = None
        for z in bprs:
            if z['start_bar'] > cur: continue
            if cur - z['start_bar'] > BPR_MAX_ZONE_AGE: continue
            if z['violated'] and z['viol_bar'] is not None and z['viol_bar'] <= cur: continue
            if z['direction'] == 'up':
                if lo_w[cur] <= z['mid'] and cl_w[cur] > z['bot']:
                    if cur > z['start_bar'] and lo_w[cur-1] <= z['mid']:
                        continue  # 非首次觸及,避免同zone重複進場
                    hit = ('long', z['bot'], z['top']); break
            else:
                if hi_w[cur] >= z['mid'] and cl_w[cur] < z['top']:
                    if cur > z['start_bar'] and hi_w[cur-1] >= z['mid']:
                        continue
                    hit = ('short', z['bot'], z['top']); break
        if hit is None: return None
        direction = hit[0]
        d4 = fetch_market_candles(okx_swap_symbol, "4H")
        if d4.empty or len(d4) < 200: return None
        e200 = d4["close"].ewm(span=200, adjust=False).mean()
        trend_up_4h_bpr = e200.iloc[-1] > e200.iloc[-2]
        if (direction == "long" and not trend_up_4h_bpr) or (direction == "short" and trend_up_4h_bpr):
            return None                                                          # regime不順向,不進
        print(f"[BPR] {symbol_item} 失衡區重合回測中軸({direction}) regime_up4h={trend_up_4h_bpr} "
              f"zone=[{hit[1]:.5g},{hit[2]:.5g}]")
        return hit
    except Exception as e:
        print(f"[BPR] {symbol_item} 判斷失敗: {e}")
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

        # ── 儀表板被動快照：只記「上面已經算完的值」，不多打一次 API、不多算一次指標。
        #    包在 try 裡且 dashboard.put 自己也不拋例外 → 儀表板壞掉不可能影響交易。
        try:
            _st_sc, _st_lb = 0, ""
            if tf_id == "1H":
                # ★官方評分的「結構分」（±22）。只在 1H 算：字卡的 OI／象限脈絡就是 1H，
                #   而且這裡的 df 是掃描本來就抓好的，不多打 API、不多抓一次 K 線。
                _st_sc, _st_lb = _struct_score(
                    df["high"].values[-200:], df["low"].values[-200:],
                    [float(x) for x in df["close"].values[-200:]], current_close)
            dashboard.put(symbol_item, tf_id,
                          px=float(current_close),
                          atrp=float(current_atr) / float(current_close) if current_close else None,
                          adx=float(current_adx),
                          vg=("大通道上" if current_close > large_top else
                              "大通道內" if current_close >= large_bot else "大通道下"),
                          trend="bear" if bear_trend else "bull",
                          **({"struct": _st_sc, "struct_label": _st_lb} if tf_id == "1H" else {}))
        except Exception:
            pass

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

        # ★2026-08-26 改成官方的「三步驟**依序**」(原本是三條件同一根K同時成立)
        # 依據:菁英交易學院「賽克斯指標 步驟2」影片逐字稿(規格見 trading-backtest/_ELITE_COURSE_SPEC.md 10-1)
        #   官方原話(做空):「第一步我們要先等待價格**上來測試這個小通道,沒有出現突破的動作**;
        #   第二步就要**跌破我們黃色這條過濾線**;第三步呢,就是要 QQE 由藍柱轉為紅柱」
        #   → 是**跨多根K的先後順序**,不是同一根全中。我原本寫成同根,等於在測另一個東西。
        # 回測(_bt_c3_official_fix.py / _bt_c3_seq_sens.py,7期含費,live忠實出場=swing_full+12h時停):
        #   同根(舊)      n=78  勝38.5% EV-0.019 PF0.94 容錯-1.5🔴 連虧9 正期2/5 訓-0.20/驗+0.07
        #   序列跨度6(新) n=129 勝52.7% EV+0.135 PF1.59 容錯11.6🟡 連虧6 正期4/5 訓+0.00/驗+0.22
        #   ★穩健性:跨度3/4/6/8/12 全部大幅改善(容錯10.6~13.0)、訓驗兩段皆非負 → 非曲線擬合,
        #     取網格中間值6上線(不挑最好看的4)。
        #   ★同時測過但**不採用**:觸發改QQE MOD柱色轉紅(容錯只有5.0~8.8、連虧26-33,比rsiMa差)、
        #     止損改「測試通道那根的高點」(容錯-4.3,更差)、加均線纏繞閘(11.6→8.2,更差)。
        # ⚠️同時訂正:live 舊註解說 C3階梯空「容錯13.5🟢」是**3期n=49**的數字;
        #   跑滿7期的裸基準其實是 容錯-1.5🔴。這支策略原本的依據比註解寫的薄很多。
        C3_SEQ_WIN = 6      # 序列最大跨度(根)
        _cs = df["close"].values; _hs = df["high"].values
        _e12 = ema12.values; _e144 = ema144.values; _e169 = ema169.values
        _iN = len(_cs) - 1
        # 第三步(當根):rsiMa 由 >=50 穿越到 <50
        short_C3 = (rsi_ma_s.iloc[-2] >= 50 and rsi_ma_s.iloc[-1] < 50)
        # 第二步:往回找「目前這段連續收在過濾線下」的起點 = 跌破過濾線那根
        _brk = None
        for _f in range(_iN, max(0, _iN - C3_SEQ_WIN) - 1, -1):
            if _cs[_f] < _e12[_f]:
                _brk = _f
            else:
                break
        # 第一步:在跌破過濾線之前,要有一根「上來測試小通道但沒有突破」
        _test_i = None
        if _brk is not None:
            for _f in range(_brk, max(0, _brk - C3_SEQ_WIN) - 1, -1):
                _st = max(_e144[_f], _e169[_f])
                if _hs[_f] > _st and _cs[_f] < _st:
                    _test_i = _f
                    break
        short_C1 = (_test_i is not None)          # 測試小通道未突破(可發生在前幾根)
        short_C2 = (_brk is not None)             # 已跌破過濾線並維持在其下
        short_adx_ok = (not ADX_ENABLED or current_adx >= ADX_THR)
        short_fund_ok = (funding_rate is None or funding_rate >= FUNDING_SHORT_MIN)
        is_short = (bear_trend and short_C1 and short_C2 and short_C3 and
                    short_adx_ok and short_fund_ok)

        # ★2026-08-26 資費過熱閘也套到 1H C3空(原本只串在 OISQ)。
        # 回測(_bt_gates_on_c3.py,7期含費,對象=新版三步驟序列C3空,live忠實出場):
        #   基準 n=129/勝52.7%/EV+0.135/容錯11.6🟡/正期4-5/訓+0.00 驗+0.22
        #   +資費過熱閘 n=118(只擋9%)/勝53.4%/EV+0.161/容錯**14.0🟢**/正期**4/4**/訓+0.03 驗+0.24
        # ★同時測過但**不加**:
        #   日線偏見閘 → 擋 **0%**(C3空本來就要求 bear_trend,日線偏見必然同向)= 冗餘
        #   反向S/R區間閘 → 擋53%但容錯 11.6→11.4 幾乎沒變(C3空已有階梯Fibo壓力位+POC籌碼支撐閘
        #     兩層在管位置,第三層重複);三閘全上 n只剩55、正期2/3,樣本太薄不值得
        if is_short and tf_id == "1H" and _funding_overheated(okx_swap_symbol) is True:
            print(f"[資費閘] {symbol_item} C3空 資費過熱,擋單")
            is_short = False

        # ── 數據獵手 CVD 加碼（C方案）：15m 多單 CVD吸收確認 → 下注 ×DH_BOOST_MULT ──
        # 不過濾(保留全部15m多的頻率=複利引擎)，只把資金往高品質的CVD確認單傾斜。
        # 回測(全策略×階梯下注)：C×1.5 成長>不過濾基準、MDD還略低，優於硬性過濾(A)。
        # dh_boost 一律先設1.0(每個tf都會經過此行)，只有15m多且CVD確認才放大。
        dh_boost = 1.0
        # ── ★V成型吸收做多(15m,2026-08-31)：獨立訊號源,不覆寫既有 is_long ──
        is_vlong = False; _vlong_sl = 0.0; _vlong_r = ""
        if VLONG_ENABLED and tf_id == "15m":
            try:
                is_vlong, _vlong_r, _vlong_sl = _check_vlong(symbol_item, okx_bar_fmt, df,
                                                             okx_swap_symbol)
                if _VLONG_DIAG["呼叫"] % 189 == 1:
                    _VLONG_DEEP_BUDGET["used"] = 0          # 每輪重置深抓配額
                    _S4H_DEEP_BUDGET["used"] = 0            # ★S4H 4H深抓配額同步每輪重置
                if _VLONG_DIAG["呼叫"] % 50 == 0:
                    print(f"[V-Long儀表] {_VLONG_DIAG} 深快取{len(_VLONG_KL_CACHE)}幣", flush=True)
                if is_vlong:
                    # ★2026-09-16 深夜:群聚(崩盤反彈日)改成加碼而非硬擋 → 這裡把倍數接到下注
                    _vb = float(_VLONG_LAST.get(symbol_item, {}).get("boost", 1.0) or 1.0)
                    if _vb > 1.0: dh_boost = max(dh_boost, _vb)
                    print(f"[V-Long] {symbol_item} {_vlong_r} sl={_vlong_sl:.6g} 下注×{dh_boost:g}", flush=True)
            except Exception as _vle:
                print(f"[V-Long] {symbol_item} 判斷失敗: {_vle}")

        if DH_CVD_ENABLED and is_long and tf_id == "15m":
            try:
                _dh_ok, _dh_r = _dh_cvd_ok(symbol_item, okx_bar_fmt, "15m", "long")
                _ls_l, _ = _fetch_binance_ls_taker(symbol_item)   # ls<1.0=散戶淨空=逆勢多(WF+0.138)
                if _dh_ok or (_ls_l is not None and _ls_l < 1.0):
                    dh_boost = DH_BOOST_MULT   # CVD吸收 或 散戶淨空 → 加碼下注
            except Exception as _dh_err:
                print(f"[DH-CVD] {symbol_item} 15m多加碼判斷失敗: {_dh_err}")

        # ── ★LL→LH 反彈斐波吞噬做空(15m,2026-09-04)：獨立訊號源,不覆寫既有 is_short ──
        is_llh_short = False; _llh_sl = 0.0; _llh_r = ""
        if LLH_SHORT_ENABLED and tf_id == "15m":
            try:
                is_llh_short, _llh_r, _llh_sl = _check_llh_short(symbol_item, df)
                if _LLH_DIAG["呼叫"] % 50 == 0:
                    print(f"[LLH-Short儀表] {_LLH_DIAG} 今日{_LLH_DAY['count']}/{LLH_DAILY_CAP}", flush=True)
                if is_llh_short:
                    print(f"[LLH-Short] {symbol_item} {_llh_r} sl={_llh_sl:.6g}", flush=True)
            except Exception as _lle:
                print(f"[LLH-Short] {symbol_item} 判斷失敗: {_lle}")

        # ── ★S4H 做空(4h,2026-09-04上線)：獨立訊號源,不覆寫既有 is_short ──
        #   4H 在 AUTO_TRADE 是 False(僅通知),本策略在下方下單閘用「只有它觸發」的專屬旁路放行,
        #   不會順帶把其他 4H 訊號一起開成自動下單。
        is_s4h_short = False; _s4h_sl = 0.0; _s4h_r = ""
        if S4H_SHORT_ENABLED and tf_id == "4H":
            try:
                is_s4h_short, _s4h_r, _s4h_sl = _check_s4h_short(symbol_item, okx_swap_symbol)
                if _S4H_DIAG["呼叫"] % 50 == 0:
                    print(f"[S4H-Short儀表] {_S4H_DIAG} 今日{_S4H_DAY['count']}/{S4H_DAILY_CAP} "
                          f"深快取{len(_S4H_KL_CACHE)}幣", flush=True)
                if is_s4h_short:
                    print(f"[S4H-Short] {symbol_item} {_s4h_r} sl={_s4h_sl:.6g}", flush=True)
            except Exception as _s4e:
                print(f"[S4H-Short] {symbol_item} 判斷失敗: {_s4e}")

        # ── ★突破回踩做空(4h,2026-09-13上線)：獨立訊號源,不覆寫既有 is_short ──
        #   跟 S4H 一樣是 4h 進場,而 4H 的 AUTO_TRADE 是 False(僅通知)→
        #   靠下方 `_bor_only` 專屬旁路放行,不會順帶把其他 4H 訊號開成自動下單。
        is_bor_short = False; _bor_sl = 0.0; _bor_r = ""
        if BOR_SHORT_ENABLED and tf_id == "4H":
            try:
                is_bor_short, _bor_r, _bor_sl = _check_bor_short(symbol_item, okx_swap_symbol)
                if _BOR_DIAG["呼叫"] % 50 == 0:
                    print(f"[BOR-Short儀表] {_BOR_DIAG} 今日{_BOR_DAY['count']}/{BOR_DAILY_CAP} "
                          f"連虧{_BOR_RISK['consec_sl']}/{BOR_MAX_CONSEC_SL}"
                          f"{' 🛑已熔斷' if _BOR_RISK['halted'] else ''}", flush=True)
                if is_bor_short:
                    print(f"[BOR-Short] {symbol_item} {_bor_r} sl={_bor_sl:.6g}", flush=True)
            except Exception as _boe:
                print(f"[BOR-Short] {symbol_item} 判斷失敗: {_boe}")

        # ── ★4J減速跌破做空(1H,2026-09-10上線)：位階2h/判定與進場1H,獨立訊號源 ──
        #   自己抓 1H+2H(不吃外面的 df),整段狀態機每輪重建(不跨輪保存,redeploy不會歸零)。
        #   移植對拍 264/264 一致、停損價0不一致、4032根負樣本0假陽性(_chk_4jd_port.py)。
        is_fourjd_short = False; _fjd_sl = 0.0; _fjd_r = ""
        if FOURJD_SHORT_ENABLED and tf_id == "1H":
            try:
                is_fourjd_short, _fjd_r, _fjd_sl = _check_fourjd_short(symbol_item, okx_swap_symbol)
                if _FOURJD_DIAG["呼叫"] % 200 == 0:
                    print(f"[4JD-Short儀表] {_FOURJD_DIAG} 連虧{_FOURJD_RISK['consec_sl']}"
                          f"/{FOURJD_MAX_CONSEC_SL}{' 🛑已熔斷' if _FOURJD_RISK['halted'] else ''}",
                          flush=True)
                if is_fourjd_short:
                    print(f"[4JD-Short] {symbol_item} {_fjd_r} sl={_fjd_sl:.6g}", flush=True)
            except Exception as _fje:
                print(f"[4JD-Short] {symbol_item} 判斷失敗: {_fje}")

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
                is_engulf_short, _er = _check_engulf_short(symbol_item, df)
                if _ENGULF_DIAG["呼叫"] % 200 == 0:
                    print(f"[Engulf-Short儀表] {_ENGULF_DIAG}", flush=True)
                if is_engulf_short:
                    print(f"[Engulf-Short] {symbol_item} {_er}", flush=True)
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
            print(f"  C1={short_C1}  (序列:測試小通道未突破的那根 index={_test_i})", flush=True)
            print(f"  C2={short_C2}  (序列:跌破過濾線起點 index={_brk}, EMA12={ema12.iloc[-1]:.6f})", flush=True)
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
                    if MACD_LONG_15M_ENABLED and tf_id == "15m" and trend_up_4h and gold and macd_difslope_ok(dif, "long") and _brk_up \
                       and current_adx >= MACD_LONG_15M_ADX_MIN:
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
                        if MACD_SHORT_1H_ENABLED and _vol_ok and _brk_dn and _floor_ok and (not trend_up_4h) and dead and macd_difslope_ok(dif, "short"):
                            _tfok, _tfr = tflow_confirm(_bn_sym, "short")
                            if _tfok is not False:    # None(非3幣/thin/失敗)=放行,只靠帶量+突破
                                is_macd_short = True; dh_boost = BOOST_MULT
                                print(f"[MACD空] {symbol_item} 帶量+突破✓ {_tfr}")
                        if MACD_LONG_1H_ENABLED and _vol_ok and _brk_up and trend_up_4h and gold and macd_difslope_ok(dif, "long"):
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
                # ★2026-08-05 多時框偏見對齊確認層(見 _mtf_bias_ok 註解的回測依據):
                #   ⚠️2026-08-26訂正:舊數字(空62.2%/容錯30.7)是未來函數造的。修正後只對空單有效(18.9→22.4),
                #   多單反而被害(11.0→8.0)→已改成 short-only + daily-only。
                if _sq in ("long", "short") and not _mtf_bias_ok(okx_swap_symbol, _sq):
                    _sq = None
                # ★2026-08-26 資費過熱閘(回測依據見 _funding_overheated 註解)。多空皆擋。
                if _sq in ("long", "short") and _funding_overheated(okx_swap_symbol) is True:
                    print(f"[資費閘] {symbol_item} {_sq} 資費過熱,擋單")
                    _sq = None
                # ★2026-08-26 反向S/R區間閘(見 _sr_reverse_zone_hit 註解的回測依據+官方定義出處)
                if _sq in ("long", "short") and _sr_reverse_zone_hit(okx_swap_symbol, _sq) is True:
                    _sq = None
                # ★2026-08-26 止損位置閘(見 _sr_sl_position_ok)。用**下方實際會下的那個SL**去判,
                #   公式與 is_oisq_long/short 區塊(range對邊±0.3ATR、2.5ATR上限)逐字對齊。
                if _sq in ("long", "short"):
                    _rh_g = float(df["high"].values[-13:-1].max())
                    _rl_g = float(df["low"].values[-13:-1].min())
                    if _sq == "long":
                        _sl_g = max(_rl_g - 0.3 * current_atr, current_close - 2.5 * current_atr)
                    else:
                        _sl_g = min(_rh_g + 0.3 * current_atr, current_close + 2.5 * current_atr)
                    if _sr_sl_position_ok(okx_swap_symbol, _sq, current_close, _sl_g) is False:
                        print(f"[止損位置閘] {symbol_item} {_sq} 止損卡在同向區間內,擋單")
                        _sq = None
                if _sq == "long":  is_oisq_long = True;  dh_boost = BOOST_MULT; print(f"[主力建多] {symbol_item} 壓縮突破噴出(×1.5)")
                elif _sq == "short": is_oisq_short = True; dh_boost = BOOST_MULT; print(f"[主力建空] {symbol_item} 壓縮突破噴出(×1.5)")
            except Exception as _sqe:
                print(f"[OI-Squeeze] {symbol_item} 判斷失敗: {_sqe}")

        # ── BPR失衡區重合續勢(1H,限主流+regime,2026-07-08):見_check_bpr註解。固定1.5R出場 ──
        is_bpr_long = False; is_bpr_short = False; _bpr_zone = None
        if BPR_ENABLED and tf_id == "1H":
            try:
                _bpr_hit = _check_bpr(symbol_item, okx_bar_fmt, df, okx_swap_symbol)
                if _bpr_hit:
                    _bpr_zone = _bpr_hit
                    if _bpr_hit[0] == "long":  is_bpr_long = True;  print(f"[BPR多] {symbol_item} 失衡區重合回測(固定1.5R)")
                    else:                      is_bpr_short = True; print(f"[BPR空] {symbol_item} 失衡區重合回測(固定1.5R)")
                    # ★觀察倉期(2026-07-08上線):全新策略無live樣本,先半倉觀察。
                    #   累計30筆live成交後人工核對是否符合回測(勝率55-65%/EV+0.03~+0.34),達標再拿掉0.5x。
                    dh_boost = BPR_OBSERVATION_SCALE
            except Exception as _bpre:
                print(f"[BPR] {symbol_item} 判斷失敗: {_bpre}")

        # ── 4J 結構回踩(1H進場,2026-08-27):4H定結構與關鍵位 → 1H抓進場時機。固定1:1 ──
        is_4j_long = False; is_4j_short = False; _4j_level = None
        if FOURJ_ENABLED and tf_id in FOURJ_LADDER:
            try:
                _4j = _check_4j(symbol_item, okx_swap_symbol, df, tf_id)
                if _4j:
                    _4j_level = _4j[1]
                    _sbar = FOURJ_LADDER[tf_id]
                    if _4j[0] == "long":  is_4j_long = True;  print(f"[4J多] {symbol_item} {_sbar}結構突破後回踩→{tf_id}進場(固定1:1)")
                    else:                 is_4j_short = True; print(f"[4J空] {symbol_item} {_sbar}結構跌破後回踩→{tf_id}進場(固定1:1)")
                    # ★半倉觀察(2026-08-27上線):全新策略無live樣本。累計30筆live成交後人工對帳
                    #   ★對帳要對的數字(現行規格:全去ATR + TP1=1R半平/TP2=3R + 浮盈1%保本):
                    #     4H→30m n=528 EV+0.576 容錯40.1 連虧6 | 2H→15m n=1103 EV+0.577 容錯38.3 連虧9
                    #   ★**不要用「勝率」對帳,會被誤導**:1%保本讓很多單以「只虧手續費」出場,
                    #     那些不是虧損單但也不算贏。正確的分桶(現行規格實測):
                    #       4H→30m 賺58.7% / 保本出場32.2% / **吃滿停損只有9.1%**  平均賺+1.17R 平均虧-1.09R
                    #       2H→15m 賺67.3% / 保本出場20.6% / **吃滿停損只有12.1%** 平均賺+1.07R 平均虧-1.11R
                    #     (我2026-08-27曾對外報「勝率82%/70%」,那是**還沒加1%保本、固定1:1**版本的數字,
                    #      同一批訊號改用1:1確實是79%;規格換了勝率的定義基準就變了,已訂正。)
                    #   達標再拿掉 0.5x。
                    dh_boost = FOURJ_OBS_SCALE
            except Exception as _e4:
                print(f"[4J] {symbol_item} 判斷失敗: {_e4}")

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
        # ★★2026-09-11 把**吞噬空**從本閘移除(其餘策略不動)。證據 `_chk_engulf_regime4h.py`:
        #   同一批 live 觸發規格的訊號(n=376),本閘擋掉 80 筆(21.3%),而**被擋掉的反而是好單**——
        #     無閘 n=376 EV+0.515 容錯17.2 總+193R
        #     有閘 n=296 EV+0.468 容錯15.7 總+138R   ← 總R少28%
        #     被擋的80筆 EV+0.688 容錯22.9 勝57% 正期6/6
        #   四層裡三層變差(訓練16.3→9.9 / 驗證14.1→12.6 / 新幣12.0→9.9)。
        #   想得通:吞噬空要的是「破前12根高之後的看跌吞噬」=**相對高位**,4H還在EMA50上正是這種位置;
        #   它自己已經有 close<EMA100 這個回測驗過的 regime 條件,再疊慢速4H EMA50 是重複且有害。
        #   ★本閘是 2026-06-15 憑**實盤169筆全部空單合計**的診斷加的,不是吞噬空專屬回測;
        #     而我 0906 重做吞噬空(實體≥0.70)時漏了 grep main.py 核對防呆閘(CLAUDE.md 心法2),
        #     導致 live 跑的規格 ≠ 我驗過的規格。用戶 2026-09-11 問「今天都沒什麼單」才翻出來。
        #   ★弱點誠實記錄:被擋那80筆在訓練段/驗證段各自 n<25,單獨不算鐵證;
        #     依據是主表四層對照(376 vs 296,樣本足)。
        if (is_box_short or is_vegas_short or is_macd_short or is_oisq_short or (is_short and tf_id == "1H")):
            try:
                _d4s = fetch_market_candles(okx_swap_symbol, "4H")
                if not _d4s.empty and len(_d4s) > 60:
                    _e50s = _d4s["close"].ewm(span=50, adjust=False).mean()
                    _4h_dn = float(_d4s["close"].iloc[-1]) < float(_e50s.iloc[-1])   # 價在4H EMA50之下=下行
                    if not _4h_dn:
                        print(f"[空regime閘] {symbol_item} 4H在EMA50之上(非下行)→擋積極空單(吞噬空已豁免)")
                        is_box_short = is_vegas_short = is_macd_short = is_oisq_short = False
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
        combined_long  = is_long  or is_double_bottom or is_reson_long  or is_macd_long or is_oisq_long or is_conv_long or is_bpr_long or is_4j_long or is_vlong
        combined_short = is_short or is_double_top   or is_reson_short or is_macd_short or is_dh_short or is_box_short or is_vegas_short or is_oisq_short or is_engulf_short or is_bpr_short or is_4j_short or is_llh_short or is_s4h_short or is_fourjd_short or is_bor_short

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
            if is_vlong:         _signal_source.append("V成型吸收多")
            if is_long:          _signal_source.append("C3")
            if is_double_bottom: _signal_source.append("雙底")
            if is_reson_long:    _signal_source.append("雙底+RSI共振")
            if is_macd_long:     _signal_source.append("MACD動能")
            if is_oisq_long:     _signal_source.append("主力建多")
            if is_conv_long:     _signal_source.append("收斂突破多")
            if is_bpr_long:      _signal_source.append("BPR失衡區重合")
            if is_4j_long:       _signal_source.append(f"4J結構回踩多({FOURJ_LADDER.get(tf_id,'?')}→{tf_id})")
        else:
            _signal_source = []
            if is_short:         _signal_source.append("C3")
            if is_double_top:    _signal_source.append("雙頂")
            if is_reson_short:   _signal_source.append("雙頂+RSI共振")
            if is_macd_short:    _signal_source.append("MACD動能")
            if is_dh_short:      _signal_source.append("數據獵手空")
            if is_llh_short:     _signal_source.append("LL→LH反彈空")
            if is_s4h_short:     _signal_source.append("S4H做空(4h吞噬+123+斐波+LL)")
            if is_bor_short:     _signal_source.append("突破回踩空(4h跌破前低+回測+吞噬)")
            if is_fourjd_short:  _signal_source.append("4J減速跌破空(2h位階+1H減速+跌破盤整低)")
            if is_box_short:     _signal_source.append("箱突破空")
            if is_vegas_short:   _signal_source.append("維加斯大通道空")
            if is_oisq_short:    _signal_source.append("主力建空")
            if is_engulf_short:  _signal_source.append("吞噬空")
            if is_bpr_short:     _signal_source.append("BPR失衡區重合")
            if is_4j_short:      _signal_source.append(f"4J結構回踩空({FOURJ_LADDER.get(tf_id,'?')}→{tf_id})")
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
        _strat_ts_h = 0          # ★2026-08-02 策略專屬時間停損(0=用型態預設:讓跑24h/固定R 12h)
        if is_fourjd_short:
            # ★4J減速跌破空:整倉 TP 2R + 浮盈0.8R移保本(見 execute_*_pipeline 的 fourjd_2r 分支)。
            #   回測:無保本 吃滿停損51.9% → 0.8R保本 30.5%,容錯 12.5→13.8。
            exit_strategy = "fourjd_1r"   # ★2026-09-16 TP改1R不保本→交給交易所掛單,不再走保本分支
            _strat_ts_h = -1         # ★不設時間停損:回測沒設(進場後最多走400根)
        elif is_bor_short:
            # ★BOR:固定 1R 全平(TP override 見下方 SL/TP 區塊)。
            #   標記 "bor_1r" **不在**任何出場族 tuple 裡(box_trend/hf_1r/fourjd_2r/line_*/swing_*)
            #   → 出場行為等同 ""(分批 TP1/TP2,但兩段同價 = 等效全平),與 S4H 一致;
            #   取這個名字只為了倉位消失時能識別是哪個策略(熔斷計數要用)。
            exit_strategy = "bor_1r"
            _strat_ts_h = -1         # ★不設時間停損 —— 回測就是不設
        elif is_s4h_short:
            exit_strategy = "s4h_fixed"   # S4H:固定 2.5R 全平(TP override 見下方 SL/TP 區塊)
            # ★2026-09-14 原本是 "" → 追蹤迴圈走預設固定R分支,浮盈 1R(4H_short be_trigger)就移保本
            #   再 pivot 移SL,回測規格「固定2.5R全平」live 沒被執行。改專屬標記,交易所掛好就不碰(_HANDS_OFF_ES)。
            _strat_ts_h = -1         # ★不設時間停損 —— 回測就是不設,設了 12h 預設就不是同一個規格
        elif is_engulf_short:
            # ★2026-09-05 吞噬空重開,出場從固定2R改成 swing_full(整倉讓跑,不掛TP)。
            #   容錯 7.5→10.7、成本0.25%後 +0.153→+0.278,四層皆正,訊號量一筆沒砍(2.12筆/天)。
            #   驗收方式=**逐根重放 live 既有的 _swing_trail_update_sl**,不是我另寫一套(見上方 ENGULF_SHORT_ENABLED 註解)。
            exit_strategy = "engulf_1r"   # ★2026-09-16 讓跑改成 寬停損+TP1R(見 ENGULF_SL_MULT 註解)
            _strat_ts_h = -1         # ★不設時停(回測沒設)
        elif is_dh_short:
            exit_strategy = "line_full"                                  # DH空：整倉切線讓跑(2026-06-13關加碼:
            #   按年顯示加碼只在強熊好(2022),震盪/牛市害它(2024純跑+0.32 vs 加碼-0.02)。切線出場不變,只去加碼。
        elif tf_id == "30m" and direction == "long" and is_long:
            exit_strategy = "line_full"                                  # 30m C3多：整倉切線
        elif tf_id == "1H" and direction == "long" and is_double_bottom:
            exit_strategy = "swing_tp"                                   # 1H W底多：TP1+轉折移SL
        elif tf_id == "1H" and direction == "short" and is_macd_short:
            exit_strategy = ""                                           # ★2026-07-19 1H MACD空改固定2.5R全平(用戶選保守版)。
            #   舊「麥門切線」的+0.795是3幣小樣本;全市值7期忠實重測:切線只+0.222/4期正,
            #   固定2.5R=+0.265/5期正(EV+一致性都勝)。tp override見下方SL/TP區塊(比照吞噬空單一目標)。
        elif tf_id == "1H" and direction == "long" and is_macd_long:
            exit_strategy = "swing_full"                                 # 1H MACD多(新增):整倉轉折移SL讓跑(驗+0.605>TP1.5+0.465,順勢抱)
            # ★★★2026-09-19 24 → -1(不設時間停損)。**這是 09-06「關掉時間停損」那次的漏改**:
            #   當時我只把 GLOBAL_TIMESTOP_H / LETRUN_TIMESTOP_H 改成 10**6,沒發現策略專屬的
            #   `_strat_ts_h` 在 `_timestop_hours()` 裡**優先度高於常數**(`if _h > 0: return _h`)
            #   → MACD多 的 24h 時停一直活著。實際事故:ZEN 09-17 16:06 開倉 → 09-18 16:15
            #   「⏰ ZEN-USDT-SWAP 開倉滿24h未到目標,市價平倉(時間停損)」,用戶當場抓到。
            #   而且它只平了 remaining_amount(32張)、撤掉停損、把倉從追蹤池移除 →
            #   **剩下 32 張變成無停損又無人管的裸倉**,用戶 16:32 自己出掉。
            # ★數據也支持不設:同一批訊號 無時停 EV+0.309/容錯18.9 > 24h +0.271/18.3(見上方 09-06 註解)。
            # ★用戶硬性規則:不要時間停損、保本要留。
            _strat_ts_h = -1
        elif tf_id == "1H" and direction == "short" and is_short:
            exit_strategy = "swing_full"                                 # 1H C3空+階梯：整倉pivot移SL(驗+0.263/MDD10%)
            # ★★2026-09-19 12 → -1(不設)。與 MACD多 同一個漏改(見上)。
            #   原註的 12h vs 24h 比較 **從來沒有跟「無時停」比過**,而 n=49 我自己標了「樣本小視為線索」;
            #   09-06 的 live 實測(持倉時長分桶)證明時停砍的正好是贏單(>25h 那桶均 +6.46U = 被砍那批的5.6倍)。
            #   用戶硬性規則:不要時間停損。要重開必須先補「無時停」對照組。
            _strat_ts_h = -1    # 原:12(C3空專屬,已停用)
        elif tf_id == "15m" and direction == "long" and is_macd_long:
            exit_strategy = "swing_tp_1h"                                # 15m MACD多：TP1+參1H轉折移SL
        elif tf_id == "15m" and direction == "short" and is_macd_short:
            exit_strategy = "swing_full"                                 # ★15m MACD空(新)：整倉pivot移SL(保守版,回測訓+0.148/驗+0.254/MDD24%)
        elif is_box_short:
            exit_strategy = "box_trend"                                  # 箱突破空：1R保本+4R整倉大TP(讓趨勢跑)
        elif is_oisq_long:
            exit_strategy = "swing_full"                                 # OI壓縮突破多：整倉轉折移SL讓跑(抓噴出尾,驗+0.309/賺賠3.1/MDD6%)
            # ★OISQ空2026-07-19改固定2.5R全平(不進此分支,exit_strategy=""):全市值7期讓跑+0.392/5期→固定2.5R+0.506/6期
        elif is_conv_long:
            exit_strategy = "swing_full"                                 # 收斂突破多(限主流)：整倉轉折移SL讓跑(吃轉折加碼,session驗+0.17/加碼+0.8~1.0)
        elif is_bpr_long or is_bpr_short:
            exit_strategy = ""                                           # BPR:固定1.5R(對齊回測,見下方SL區塊p override)
        elif is_4j_long or is_4j_short:
            exit_strategy = ""                                           # 4J:TP1=1R半平+保本 / TP2=3R(見下方SL區塊)
            # ★2026-08-27 專屬時間停損。用戶:「4J的不套用12小時停損」——而且數據支持:
            #   全域預設12h會砍掉EV約5%、總R約10%(12h vs 回測值):
            #     4H→30m 12h EV+0.546/78.1R年 → 96h EV+0.576/86.4R年
            #     2H→15m 12h EV+0.554/171.8R年 → 48h EV+0.577/180.7R年
            #   (12h 的回撤反而略小 -2.52R/-5.15R vs -2.87R/-6.24R,但總報酬差更多)
            #   設成**回測用的值**,讓 live 與我報出的期望值同規格。腳本 _bt_4j_ts.py。
            _strat_ts_h = 96 if tf_id == "30m" else 48

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
        # ★1H MACD空：固定 2.5R 單一目標close-all(2026-07-19,用戶選保守版)。全市值7期回測固定2.5R+0.265/5期正>切線+0.222/4期正。
        #   OISQ空的tp在下方OISQ SL區塊直接設(它有自己的range止損);此處只管MACD空(走預設SL/TP路徑)。
        if direction == "short" and tf_id == "1H" and is_macd_short and not is_box_short and not is_engulf_short and not is_oisq_short:
            p = {**p, "tp1_mult": 2.5, "tp2_intraday_mult": 2.5, "tp2_swing_mult": 2.5}
        # BPR：固定 1.5R 單一目標(對齊回測simulate_trade_C的exit_fixed_r(...,1.5,1.5,999),非trail)
        if is_bpr_long or is_bpr_short:
            p = {**p, "tp1_mult": 1.5, "tp2_intraday_mult": 1.5, "tp2_swing_mult": 1.5}
        if is_4j_long or is_4j_short:
            # ★2026-08-27 出場改版:官方是「一比一」,但用戶問「停利怎麼抓的」逼出重測 →
            #   1:1 是**最差**的出場(EV+0.411/容錯20.6)。改成 TP1=1R半平+保本、TP2=3R:
            #   兩階合併 EV+0.545 容錯25.1 連虧7 143R/年(1:1是108R/年)。詳見 FOURJ_TP_* 註解。
            p = {**p, "tp1_mult": FOURJ_TP1_R, "tp2_intraday_mult": FOURJ_TP2_R,
                 "tp2_swing_mult": FOURJ_TP2_R}

        # 止損距離下限：太近=結構低點無效→倉位被放超大+一根K秒進秒損 → 寧可不下單
        MIN_SL_PCT = 0.006   # 0.6%
        if direction == "long":
            if is_vlong and _vlong_sl > 0:
                calculated_sl = round(float(_vlong_sl), 8)   # ★V成型:停損=第二個低點(吸收低點)
            else:
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
            if is_vlong:
                tp1_target = tp2_target = current_close + risk_dist * VLONG_TP_R   # 2.5R全平,對齊回測
            else:
                tp1_target = current_close + risk_dist * p["tp1_mult"]
                tp2_target = current_close + risk_dist * tp2_mult
        else:
            if is_fourjd_short and _fjd_sl > 0:
                # ★4J減速跌破:停損=盤整區最高點×1.0015,★2026-09-16 起距離再 ×FOURJD_SL_MULT(見常數註解)
                calculated_sl = round(current_close + (float(_fjd_sl) - current_close) * FOURJD_SL_MULT, 8)
            elif is_bor_short and _bor_sl > 0:
                calculated_sl = round(float(_bor_sl), 8)   # ★BOR:停損=回測段最高點×1.001(對齊回測 sl_mode="retest_low" 的做空鏡像)
            elif is_s4h_short and _s4h_sl > 0:
                calculated_sl = round(float(_s4h_sl), 8)   # ★S4H:停損=最近一個在進場價上方的已確認樞紐高×1.001(對齊回測 struct_sl)
            elif is_llh_short and _llh_sl > 0:
                calculated_sl = round(float(_llh_sl), 8)   # ★LL→LH:停損=前一個高點H1(對齊回測)
            elif is_engulf_short:
                # 吞噬空:近4根高+0.15ATR,★2026-09-16 起距離再 ×ENGULF_SL_MULT(見常數註解)
                _eng_sl0 = float(df["high"].values[-4:].max()) + 0.15 * current_atr
                calculated_sl = round(current_close + (_eng_sl0 - current_close) * ENGULF_SL_MULT, 8)
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
            if is_fourjd_short and _fjd_sl > 0:
                # ★4J減速跌破:★2026-09-16 起 TP 1R 全平(原2R),不保本 —— 見 FOURJD_SL_MULT 常數註解
                tp1_target = tp2_target = current_close - risk_dist * FOURJD_TP_R
            elif is_bor_short and _bor_sl > 0:
                # ★BOR:1R 全平,對齊回測(不分批、**不設時間停損**)
                tp1_target = tp2_target = current_close - risk_dist * BOR_TP_R
            elif is_engulf_short:
                # ★吞噬空:2026-09-16 起 TP 1R 全平(原 swing_full 讓跑),不保本 —— 見 ENGULF_SL_MULT 常數註解
                tp1_target = tp2_target = current_close - risk_dist * ENGULF_TP_R
            elif is_s4h_short and _s4h_sl > 0:
                # ★S4H:2.5R 全平,對齊回測(不分批、**不設時間停損**)
                tp1_target = tp2_target = current_close - risk_dist * S4H_TP_R
            elif is_llh_short and _llh_sl > 0:
                # ★LL→LH:2.5R 全平,對齊回測(不分批、不設時間停損 —— 被砍掉的長單原本EV+0.65~+0.77)
                tp1_target = tp2_target = current_close - risk_dist * LLH_TP_R
            else:
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

        # OI壓縮突破:止損放「12h range 對邊 ± 0.3ATR」(對齊回測)。
        #   多=讓跑(swing_full)不掛固定TP;★空=固定2.5R全平(2026-07-19,用戶保守版,全市值7期+0.506/6期正)。
        if is_oisq_long or is_oisq_short:
            _rh_sq = float(df["high"].values[-13:-1].max()); _rl_sq = float(df["low"].values[-13:-1].min())
            if is_oisq_long:  calculated_sl = round(_rl_sq - 0.3 * current_atr, 8)
            else:             calculated_sl = round(_rh_sq + 0.3 * current_atr, 8)
            # ★2026-08-02 停損上限2.5ATR(用戶問「怎樣才抓得到真正的噴出段」逼出的發現):
            #   OISQ的range對邊停損中位數寬達3.4-3.8ATR(一般結構停損才1.5-2.5)→就算真的噴3ATR也只值0.8-0.9R,
            #   「噴出抓到了但R沒收好」。7期回測收緊到2.5ATR上限:多EV+0.154→+0.221(+43%)/空+0.277→+0.345(+25%),
            #   容錯幾乎不變(13.2→12.6 / 17.8→17.7)、正期不變(6/7 / 5/7)、逐期幾乎全面改善。
            #   (更緊的2.0ATR多單容錯掉到10.1、1.5ATR崩到4.9=過度收緊會被雜訊掃,2.5是甜蜜點)
            if is_oisq_long:  calculated_sl = max(calculated_sl, current_close - 2.5 * current_atr)
            else:             calculated_sl = min(calculated_sl, current_close + 2.5 * current_atr)
            risk_pct = abs(calculated_sl - current_close) / current_close
            if risk_pct < MIN_SL_PCT or risk_pct > MAX_SL:
                if _dbg: print(f"[OISq-SL] {symbol_item} range止損超範圍({risk_pct:.3%})→跳過", flush=True)
                return
            risk_dist  = abs(current_close - calculated_sl)
            if is_oisq_long:
                tp1_target = current_close + risk_dist * 1.5   # 多:讓跑不掛固定TP,此值僅供顯示
                tp2_target = current_close + risk_dist * 3.0
            else:
                tp1_target = current_close - risk_dist * 2.5   # ★空:固定2.5R單一目標全平
                tp2_target = current_close - risk_dist * 2.5

        # BPR:止損放zone遠端(bot多/top空),距離不足0.6%則外推(對齊回測apply_sl_floor,非直接跳過)。固定1.5R單一目標。
        if is_bpr_long or is_bpr_short:
            _b_bot, _b_top = _bpr_zone[1], _bpr_zone[2]
            if is_bpr_long:
                calculated_sl = _b_bot
                _floor_sl = current_close * (1.0 - MIN_SL_PCT)
                if calculated_sl > _floor_sl: calculated_sl = _floor_sl
            else:
                calculated_sl = _b_top
                _floor_sl = current_close * (1.0 + MIN_SL_PCT)
                if calculated_sl < _floor_sl: calculated_sl = _floor_sl
            risk_pct = abs(current_close - calculated_sl) / current_close
            if calculated_sl == current_close or risk_pct > MAX_SL:
                if _dbg: print(f"[BPR-SL] {symbol_item} 止損無效/超範圍({risk_pct:.3%})→跳過", flush=True)
                return
            risk_dist  = abs(current_close - calculated_sl)
            tp1_target = current_close + (risk_dist if is_bpr_long else -risk_dist) * 1.5
            tp2_target = tp1_target                          # 固定單一目標(比照吞噬空/箱突破模式,非TP1/TP2分批)

        # 4J:停損=**1H 前低/前高**(近6根)±0.2ATR,對齊回測 _bt_4j_multiscale.py
        #   (官方影片01原話「你的止損就是放在**前面的高點**」;他講的1~2%停損綁在他自己的時框,
        #    不可跨時框搬——回測實測固定1.5%套到4H級結構是災難 -0.20,故用結構停損)
        if is_4j_long or is_4j_short:
            _lb = FOURJ_SL_LOOKBACK
            if is_4j_long:
                calculated_sl = float(df["low"].values[-_lb:].min())      # ★純前低,不加ATR緩衝
                _floor_sl = current_close * (1.0 - MIN_SL_PCT)
                if calculated_sl > _floor_sl: calculated_sl = _floor_sl
            else:
                calculated_sl = float(df["high"].values[-_lb:].max())     # ★純前高
                _floor_sl = current_close * (1.0 + MIN_SL_PCT)
                if calculated_sl < _floor_sl: calculated_sl = _floor_sl
            risk_pct = abs(current_close - calculated_sl) / current_close
            if calculated_sl == current_close or risk_pct > MAX_SL:
                if _dbg: print(f"[4J-SL] {symbol_item} 止損無效/超範圍({risk_pct:.3%})→跳過", flush=True)
                return
            risk_dist  = abs(current_close - calculated_sl)
            _sgn = 1.0 if is_4j_long else -1.0
            tp1_target = current_close + _sgn * risk_dist * FOURJ_TP1_R   # 1R 平一半→自動移保本
            tp2_target = current_close + _sgn * risk_dist * FOURJ_TP2_R   # 剩半倉跑3R

        # ★山寨讓跑改半倉2.5R落袋(2026-06-15,COAI教訓:山寨噴到頂用swing_full一路抱會吐回)。
        #   市值幣維持讓跑(不會這樣噴崩);山寨(非MAJOR)讓跑類→swing_tp 半倉2.5R落袋+BE+剩半trail。多空通用。
        # ★2026-09-14 吞噬空豁免:它的出場規格就是 swing_full(0905 逐根重放 live 既有 _swing_trail_update_sl 驗收),
        #   而吞噬空**只做山寨** → 這段覆寫等於把驗過的規格整個換成 半倉TP+1.5R保本+剩半trail(0911 對帳抓到)。
        if symbol_item not in MAJOR_COINS and exit_strategy in ("swing_full", "line_full") and not is_engulf_short:
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

        # ★S4H 專屬旁路(2026-09-04):4H 在 AUTO_TRADE 是 False(僅通知需手動授權),
        #   但 S4H 的凍結規格本來就是 4h 進場。這裡**只**在「S4H 是唯一觸發來源」時放行,
        #   絕不因此把其他 4H 訊號(C3/雙頂/MACD…)一起開成自動下單。
        _s4h_only = bool(is_s4h_short and _signal_source == ["S4H做空(4h吞噬+123+斐波+LL)"])
        # ★BOR 專屬旁路(2026-09-13):同 S4H —— 4H 在 AUTO_TRADE 是 False,但 BOR 的凍結規格
        #   本來就是 4h 進場。只在「BOR 是唯一觸發來源」時放行。
        _bor_only = bool(is_bor_short and _signal_source == ["突破回踩空(4h跌破前低+回測+吞噬)"])
        if AUTO_TRADE.get(tf_id) or _s4h_only or _bor_only:
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
                        exit_strategy=exit_strategy, timestop_h=_strat_ts_h,
                        # ★4J:浮盈達進場價1%就移保本(用戶指定,見 FOURJ_BE_PCT 註解的回測依據)
                        be_pct=(FOURJ_BE_PCT if (is_4j_long or is_4j_short) else 0.0),
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
                        exit_strategy=exit_strategy, timestop_h=_strat_ts_h,
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

# 私人儀表板：/d/<DASH_TOKEN> 看頁面、/d/<DASH_TOKEN>/api 拿 JSON。
# 傳 globals() 進去 → 儀表板讀到的永遠是當下的 SYMBOLS/_oi_history/active_real_trades/各 *_DIAG，
# 不會複製一份出來走味（顯示層要跟邏輯同步，見 CLAUDE.md 第12條）。
# 沒設 DASH_TOKEN(或長度<16) → 兩個路由一律回 404，等於這個功能不存在。
try:
    dashboard.register(app, globals())
except Exception as _dash_reg_err:
    print(f"[DASH] 儀表板路由註冊失敗(不影響交易): {_dash_reg_err}", flush=True)

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
        # ★2026-07-18修:原norm=round(s/7)照抄judge_coin的除數,但這裡滿分只±33(judge結構±24+資費±4滿分~±49)
        #   → 分數永遠壓在±5內、跟`幣`指令不同尺度;且!top用整數分排序→大量同分,前3名變成掃描順序決定。
        #   改按本函數滿分±33歸一到±10,並回傳raw分s給!top排序用。
        norm = int(max(-10, min(10, round(s * 10 / 33))))
        return (norm, lab, price, s)
    except Exception:
        return None

def judge_coin(coin_raw, side_hint=None, brief=False, tf="1H"):
    """裸打「幣」或「幣 多/空 [時框]」→ 仿數據獵手:市場結構象限(OI×CVD)+評分(±10)+方向轉折+適合多/空+建議SL/TP。
    用 bot 自己的 OI/CVD/funding/價格資料,即時、唯讀、不下單、零外部訊號源。
    支援時框 5m/15m/30m/1H/4H(預設1H)。brief=True 回精簡一行(訊號卡掛載用,不含SL/TP)。"""
    try:
        coin = coin_raw.strip().upper()
        inst_id = f"{coin}-USDT-SWAP"
        if not _is_known_okx_coin(coin):
            return None
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
                _ls, _tkb = _fetch_binance_ls_taker(f"{coin}/USDT", _bp)
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
        # ★2026-08-02修(用戶:「評分似乎沒看過有大於3或小於負3的」——實測130張卡:實際範圍只有-4~+6、
        #   |分數|≥5僅4.6%、**≤-5從沒出現過=「適合做空」這個判定在程式裡永遠不可能觸發**)。
        #   根因:÷7是照抄舊尺度,但分數被結構項(±24)主導→24/7=3.4,其他動能項很少同時到頂。
        #   改按真實滿分49(struct24+chg1的8+chg24的5+rs的8+funding的4)歸一到±10,兩端才對稱可達。
        norm = int(max(-10, min(10, round(s * 10.0 / 49.0))))
        # ★措辭從「適合做多/做空」(預測性)改為「結構偏多/偏空」(描述性):此分數已三次獨立回測證實
        #   對未來報酬零預測力(2026-06-10評分App/07-18≥3閘/08-02連續分數相關係數僅+0.04~0.05,
        #   且對空單呈反向)。它是市場結構儀表板,不是進場依據——別照它下單。
        verdict = "🟢 結構偏多" if norm >= 5 else "🔴 結構偏空" if norm <= -5 else "🟡 結構中性"
        align = ""
        if side_hint:
            w = "long" if side_hint in ("多","long","l","做多") else "short" if side_hint in ("空","short","s","做空") else None
            # ★2026-08-02:原本寫「⚠️逆籌碼,別追」是**沒有根據且對空單講反**的建議——
            #   實測(_bt判斷分數vs結果,OISQ 7期):空單分數+1(看多)那桶EV最好(+0.638)>分數-4那桶(+0.188),
            #   相關係數僅+0.04。照舊文案「分數≥3做空是逆籌碼別追」會擋掉最好的一批空單。改純描述、不給建議。
            if w == "long":  align = " (結構同向)" if norm >= 5 else " (結構逆向)" if norm <= -5 else " (結構中性)"
            if w == "short": align = " (結構同向)" if norm <= -5 else " (結構逆向)" if norm >= 5 else " (結構中性)"
        # ★2026-07-08(用戶反映跟v2結構疊在一起「結論不同、很亂」):brief/完整輸出都只顯示主結構這一套,
        #   v2結構(oiv2_label/oiv2_score)只保留內部變數不再顯示,避免同畫面出現兩套可能互相矛盾的結構判讀。
        # ATR(14,1H) + 近20根擺動高低 → 建議停損停利(SL=結構或至少1ATR;TP=2~3ATR)
        hi = df["high"].values; lo = df["low"].values; clv = cl.values
        _tr = np.maximum(hi[1:] - lo[1:], np.maximum(np.abs(hi[1:] - clv[:-1]), np.abs(lo[1:] - clv[:-1])))
        atr = float(pd.Series(_tr).ewm(alpha=1/14, adjust=False).mean().iloc[-1]) if len(_tr) else 0.0
        sw_lo = float(lo[-20:].min()); sw_hi = float(hi[-20:].max())
        # ★接刀警示(2026-07-08,數據獵手LAB案例逼出):近期若已重挫,OI/CVD再怎麼看多都可能是假反彈,慎防追多
        # ★2026-07-18修:crash_warn原本定義在brief return之後→brief=True必NameError→
        #   訊號卡「順籌碼」列自7/8起全變「⚠️判斷失敗: name 'crash_warn' is not defined」。移到brief前。
        _look_hi = float(hi[-min(len(hi), bars24*3):].max())
        _drawdown = (_look_hi - price) / _look_hi if _look_hi > 0 else 0.0
        crash_warn = f"\n⚠️ 近期已重挫 `{_drawdown:.0%}`(距高點),OI/CVD偏多也可能是接刀假象,慎防追多" if _drawdown >= 0.40 else ""
        if brief:
            return f"{struct_label} · 評分 `{norm:+d}/10`{align}{flip}{crash_warn}".strip()
        cvd_txt = "升" if cvd_up else ("降" if cvd_up is not None else "?")
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
            plan = (f"📐 {'多' if d=='long' else '空'} 進場 `${price:,.6g}`  停損 `${sl:,.6g}` "
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
            chk = f"🎯 **{_dlab}進場檢查 {_npass}/5 → {_vd}**\n　" + "　".join(f"{'✅' if ok else '❌'}{nm}" for nm, ok in _pts)

        # ★2026-07-08(用戶反映跟main.py舊版「重疊很多東西又結論不同」很亂):
        #   改成單一結論卡片格式(仿數據獵手字卡但只有一套結構、不重複顯示矛盾的第二套),
        #   分隔線隔開「結構/動能」「進場檢查」「結論」三塊,結論永遠放最後一行。
        # ★2026-07-09(用戶指出「做多regime不太行、做空又說不好,到底什麼才算好」):
        #   之前不管檢查幾分,一律加一句該方向「歷史偏弱」警語,導致結論永遠像在唱衰,失去意義。
        #   改成:只有在5點檢查本身也偏弱(≤3/5)時才補強調嚴設停損;檢查4-5/5過關就不再額外唱衰。
        #   山寨多的警語也改成只在非主流幣才顯示(原始發現就是限定山寨,主流多不適用這句)。
        _dword = "多" if d == "long" else "空" if d == "short" else None
        if d and atr > 0:
            if _npass >= 4:
                concl = f"💡結論:偏{_dword}且條件到位。新倉可考慮；已有{_dword}單→結構還撐得住,可續抱。"
            elif _npass == 3:
                concl = f"💡結論:偏{_dword}但訊號不夠齊。新倉先別急；已有{_dword}單→結構沒轉壞但盯緊點。"
                if d == "short": concl += "\n　_(crypto空單歷史結構偏弱,嚴設停損)_"
                elif f"{coin}/USDT" not in MAJOR_COINS: concl += "\n　_(山寨多edge薄,務必小注+嚴守停損)_"
            else:
                concl = f"💡結論:條件不支持做{_dword}。新倉別追；已有{_dword}單→數據偏弱/中性,是否減倉自行評估(非平倉指令)。"
                if d == "short": concl += "\n　_(crypto空單歷史結構偏弱,嚴設停損)_"
                elif f"{coin}/USDT" not in MAJOR_COINS: concl += "\n　_(山寨多edge薄,務必小注+嚴守停損)_"
        else:
            concl = "💡結論:訊號中性、方向不明確,新倉觀望即可。"
        _sep = "\n──────────\n"
        _plan_line = f"（新倉參考）{plan.strip()}\n" if plan else ""
        return (f"📊 **{coin}** ${price:,.6g}  {verdict}  **{norm:+d}/10**{align}{flip}  _({tf} 級別)_{_sep}"
                f"籌碼結構: {struct_label}  (OI {oi_pct:+.1f}% · CVD{cvd_txt})\n"
                f"動能: {tf}`{chg1:+.1f}%` 24H`{chg24:+.1f}%` vsBTC`{rs:+.1f}%` 資費`{fr*100:+.3f}%`"
                f"{_sep}{_plan_line}{chk}{_sep}{concl}{crash_warn}")
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
                            # ★2026-07-08:支援一則訊息多行多幣(一行一支),逐行各自判斷(原本整則>3詞就整包丟棄靜默無回應)
                            for _line in content.splitlines():
                                _w = _line.split()
                                if 1 <= len(_w) <= 3 and _w[0].isascii() and _w[0].isalpha() \
                                   and _is_known_okx_coin(_w[0]):
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
                                    if _qr: _rows.append((_sk.replace("-USDT-SWAP",""), _qr[0], _qr[1], _qr[2], _qr[3]))
                                    time.sleep(0.04)
                                if not _rows:
                                    dc_log("⚠️ top:無資料")
                                else:
                                    # ★2026-07-18修:排序改用raw分(x[4]),不用四捨五入整數分(同分一堆時前3名=掃描順序)
                                    _lg = sorted(_rows, key=lambda x: -x[4])[:3]
                                    _sh = sorted(_rows, key=lambda x:  x[4])[:3]
                                    _msg = f"🏆 **當前評分排名** ({_ttf}，掃 {len(_rows)} 幣)\n🟢 **適合做多 前3**\n"
                                    for c,n,l,p,_ in _lg: _msg += f"　`{n:+d}/10` **{c}** {l} ${p:,.6g}\n"
                                    _msg += "🔴 **適合做空 前3**\n"
                                    for c,n,l,p,_ in _sh: _msg += f"　`{n:+d}/10` **{c}** {l} ${p:,.6g}\n"
                                    _msg += "_(輕量動能評分排名；個別幣詳細順籌碼+SL/TP 請打 `幣`，如 `ADA`)_"
                                    dc_log(_msg)
                            except Exception as _te:
                                dc_log(f"⚠️ top 掃描錯誤: {type(_te).__name__}: {_te}")

                        # ── 幣順籌碼判斷（!幣 / /幣 也可，如 !ADA 空 15m）────
                        elif _is_known_okx_coin(cmd):
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

# ★2026-07-08:!幣判斷指令原本只認 SYMBOLS(bot動態掃描的~40-70幣),數據獵手上像LAB/TRIA
#   這種OKX真實存在但沒被主動掃描的幣打進DC完全沒反應(靜默忽略,無錯誤訊息)。
#   改認全OKX USDT永續合約集合(~400檔),不影響下單邏輯只影響!幣查詢的辨識範圍。
_ALL_OKX_SWAP_IDS: set = set()
_all_okx_swap_last_refresh: float = 0.0
_ALL_OKX_SWAP_REFRESH_SEC = 6 * 3600

def _is_known_okx_coin(coin: str) -> bool:
    global _ALL_OKX_SWAP_IDS, _all_okx_swap_last_refresh
    if time.time() - _all_okx_swap_last_refresh > _ALL_OKX_SWAP_REFRESH_SEC or not _ALL_OKX_SWAP_IDS:
        _s = _fetch_okx_swap_set()
        if _s:
            _ALL_OKX_SWAP_IDS = _s; _all_okx_swap_last_refresh = time.time()
    return f"{coin.upper()}-USDT-SWAP" in _ALL_OKX_SWAP_IDS

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
            _rows = r.json()
            # ★2026-09-24 儀表板:同一份回應本來就含 market_cap,原本只取 symbol 就丟掉。
            #   官方「OI／市值」= 衍生品槓桿相對幣種規模,是風險/擁擠度指標(不是買賣訊號)。零額外 API。
            for _c in _rows:
                try:
                    _mc = float(_c.get("market_cap") or 0)
                    if _mc > 0:
                        _MCAP[_c["symbol"].upper()] = _mc
                except (TypeError, ValueError):
                    pass
            return [c["symbol"].upper() for c in _rows]
        print(f"[SYMBOLS] CoinGecko HTTP {r.status_code}", flush=True)
    except Exception as e:
        print(f"[SYMBOLS] CoinGecko 抓取失敗: {e}", flush=True)
    return []

TOP_MOVERS_N        = 25            # 漲幅前N + 跌幅前N(共~50)加入掃描
MIN_MOVER_VOLCCY    = 1_000_000.0   # ★2026-09-04 對齊官方實測下限(見 LIQ_POOL_MIN_VOLCCY);單位已修為USDT
# ★2026-08-27 流動性底池(用戶:「幣池要開多一點嗎 流動性差的除外」「裸k基本上所有商品適用」)
#   原本底池只有「CoinGecko市值前100 ∩ OKX永續」= 實測54幣,加漲跌幅榜/OI榜後約94~98幣。
#   ★發現的洞:我的回測只涵蓋47幣 → 策略其實正在約50個**從沒回測過**的幣上下單。
#   補測(新抓49幣,純加密貨幣 ∩ OKX有永續 ∩ 幣安24h成交額>=10M ∩ 原本沒資料,25H1+25H2):
#     4H→30m 原47幣 EV+0.452/容錯36.9 → **新幣 EV+0.603/容錯47.8**
#     2H→15m 原47幣 EV+0.558/容錯38.9 → **新幣 EV+0.605/容錯43.6**
#     按成交額分三層,**連最低那層都成立**(4H→30m低1/3 +0.406/31.2;2H→15m低1/3 +0.659/44.8)
#   → 裸K確實跨幣種通用,而且新幣不比原有幣差。**但只驗證到 24h成交額 10M,更低沒測**,
#     故門檻設 10M(與 MIN_MOVER_VOLCCY 同值),不再往下放。
#   實測:OKX USDT永續共438個,>=10M 有 166 個 → 底池從54擴到約166,總池約 170~180。
LIQ_POOL_ENABLED     = True
LIQ_POOL_MIN_VOLCCY  = 1_000_000.0    # ★2026-09-04 **對齊官方實測下限**(用戶:「你要看他們選的幣最低條件」)
#   拉 1000 筆警報卡的「24H量」欄位(那是他們自己量的)分布:
#     **最小值 1001K、p1=1003K、低於1M的警報佔 0%** → 最小的五筆(ARX/CRO/POPCAT/DATA/SPACE)全是1001K
#     = **一刀切在 $1,000,000**,不是自然分布。其餘 p10=1.9M / p25=4.6M / 中位13.3M / p75=54M。
#   我先前猜的 3M 會擋掉他們 15% 的警報 → 改用他們的實際門檻 1M。
#   (舊註:「OI儀表板本來就小幣為主,因為要噴的話相對需要的資金少」
#   因為要噴的話相對需要的資金少」)。修正單位bug後 10M 咬得比以前重很多(幣池168→81幣)。
#   實測 OI儀表板近9天警報的 164 幣日成交額分布:p10=0.6M / p25=1.9M / **中位5.2M** / p75=16.5M
#     → **66% 的警報幣低於10M、48% 低於5M**,10M 等於把三分之二的目標幣種擋掉。
#   我方 VLONG 流動性分桶也指向同一結論(15m成交額中位):
#     100-200K(≈日10-19M) EV**+1.190** / 200K-1M +0.684 / 1-5M +0.740 / **>5M(日>480M) 只有+0.098**
#     → edge 在中小流動性,大幣幾乎沒有。最好那桶正好卡在10M/天的邊界上。
#   ★誠實標記:回測只驗證到日成交額10M,**3~10M 這段沒有回測證據**(資料集本身就是流動性篩過的)。
#     下調是依據「官方實際在做的標的分布」,不是回測。要驗證需另抓低流動性幣種的歷史資料。
#   1M → 幣池約238幣(OKX USDT永續共453),與官方掃描池同一量級。★掃描成本上升約50%,注意Railway用量。

def _okx_crypto_symbols() -> set:
    """★2026-09-04 只留**加密貨幣**永續(用戶:「股票只會在賽克斯,因為沒有數據」)。
    OKX instruments 的 `instCategory`:1=加密(278) / 3=股票(167,NVDA/TSLA/SNDK/TQQQ) / 4=商品(8,XAU/CL)。
    我方策略(VLONG/C3/OI壓縮/DH)全部吃 OI、CVD、資費 —— 股票/商品代幣**沒有這些資料**,
    而且有週末休市與跳空,違反 CLAUDE.md 記的「crypto 24/7 無跳空」前提(我們的停損模型以此為基礎)。
    門檻降到1M後池內多了33支這類標的,故加此濾網。抓失敗時回空集合=不過濾(保守不誤殺)。"""
    try:
        r = requests.get("https://www.okx.com/api/v5/public/instruments",
                         params={"instType": "SWAP"}, timeout=15)
        if r.status_code != 200: return set()
        out = set()
        for x in r.json().get("data", []):
            iid = x.get("instId", "")
            if iid.endswith("-USDT-SWAP") and str(x.get("instCategory", "")) == "1":
                out.add(iid.replace("-USDT-SWAP", ""))
        print(f"[SYMBOLS] 加密貨幣永續 {len(out)} 支(instCategory=1,已排除股票/商品)", flush=True)
        return out
    except Exception as e:
        print(f"[SYMBOLS] instCategory 抓取失敗({e}),本輪不過濾", flush=True)
        return set()


def _fetch_okx_liquid_pool(min_volccy: float = LIQ_POOL_MIN_VOLCCY) -> list:
    """OKX 全部 USDT 永續中,24h成交額 >= 門檻 的 inst_id 列表(免API KEY,一支輕量API)。"""
    if not LIQ_POOL_ENABLED:
        return []
    try:
        r = requests.get("https://www.okx.com/api/v5/market/tickers",
                         params={"instType": "SWAP"}, timeout=15)
        if r.status_code != 200:
            print(f"[SYMBOLS] 流動性底池 HTTP {r.status_code}", flush=True); return []
        out = []
        _crypto_only = _okx_crypto_symbols()
        for t in r.json().get("data", []):
            inst = t.get("instId", "")
            if not inst.endswith("-USDT-SWAP"):
                continue
            if _crypto_only and inst.replace("-USDT-SWAP","") not in _crypto_only:
                continue                      # ★只留加密貨幣
            try:
                # ★2026-09-04 修:OKX 永續的 volCcy24h 單位是**幣的數量**,不是USDT。
                #   原本直接拿它跟 10M 比 → 這道閘方向是**反的**:偏好便宜幣(幣數多)、排斥貴幣。
                #   實測:BTC volCcy24h=0.117M(實際9,522M USDT)會被擋;
                #        BSB volCcy24h=36.1M(實際只有4M USDT)卻通過 → BSB 就是這樣進到掃描池的。
                #   全市場對照:現行通過168幣,乘上價格後只有82幣 →
                #        **130幣被錯誤放進來**(有些一天只有0.2M USDT)、**44幣被錯誤擋掉**(含BTC/ETH/ZEC/HYPE)。
                if float(t.get("volCcy24h", 0) or 0) * float(t.get("last", 0) or 0) >= min_volccy:
                    out.append(inst)
            except (ValueError, TypeError):
                continue
        print(f"[SYMBOLS] 流動性底池:{len(out)} 幣(24h成交額≥{min_volccy/1e6:.0f}M USDT)", flush=True)
        return out
    except Exception as e:
        print(f"[SYMBOLS] 流動性底池抓取失敗: {e}", flush=True)
        return []

# ══ 儀表板資料取樣（唯一寫入點，見 CLAUDE.md「同一個欄名、多個來源」的坑）═══════════
_TICKER_SNAP: Dict[str, dict] = {}   # instId -> {last, chg24h, volccy_usd, ts}
_PX_HISTORY: Dict[str, list] = {}    # instId -> [(ts, last), ...]  與 _oi_history 同節奏，四象限才同窗
_MCAP: Dict[str, float] = {}         # COIN -> 市值USD（CoinGecko 那支本來就回傳，原本被丟掉）
_DASH_SAMPLE = {"ts": 0.0, "n": 0}
# ★取樣間隔 = 300 秒。**不可以等於最短的窗長**：原本 900 秒時，15m 窗要求基準點落在
#   「正好 15 分鐘前 ± 容差」，但取樣點的相位是任意的 → 線上實測 15m 窗**恆為 0 幣**
#   (30m 窗 300 幣正常)。官方有 15m/30m/1H 三檔，按鈕在那裡卻永遠空的 = 壞掉的 UI。
#   成本：兩支公開端點各 288 次/天，相對現役 K 線查詢(每輪約 200 幣)可忽略。
DASH_SAMPLE_SEC = 300
DASH_SAVE_EVERY = 1                 # ★每一輪取樣都落地（原本 3 輪＝15 分鐘一次）
# 為什麼改成每輪：redeploy 會丟掉「上次存檔之後」的所有取樣 → 歷史出現一個洞。
# 洞落在哪裡，哪個窗就算不出來：2026-09-24 連推六次之後實測
#   0.25h 有 281 筆、2h/4h/12h 有，**只有 0.5h 和 1h 是 0** —— 洞正好在 30~60 分鐘前。
# 而 1H 正是官方排名與評分用的窗，等於最重要的那一格最容易被自己的部署打死。
# 成本：476 幣約 1MB 的 JSON，每 5 分鐘原子寫一次到 /data，可忽略。
_DASH_HIST_FILE = os.path.join(_PERSIST_DIR, "dash_hist.json")


def _dash_hist_save() -> None:
    """★把 OI／價格取樣落地到 Railway volume（/data），redeploy 不歸零。
    不存檔的話每次部署 1H 窗都要重等一小時、12H 窗等於永遠等不到
    （部署頻率比 12 小時高）。時間戳取整數、數值取 6 位有效數字以縮小檔案。"""
    try:
        keep_from = time.time() - (OI_MOVERS_WINDOW_H + 1) * 3600
        def _pack(d):
            out = {}
            for k, h in list(d.items()):   # ★snapshot:幣安取樣在背景執行緒改這些 dict
                # 9 位有效數字:OI 的 1H 變化常常只有 1~3%,存成 6 位(1000052→1000050)
                # 的量化誤差雖小,但沒必要拿精度換那一點檔案大小(實測 476 幣約 1MB)。
                pts = [[int(t), float(f"{v:.9g}")] for (t, v) in h if t >= keep_from]
                if pts:
                    out[k] = pts
            return out
        tmp = _DASH_HIST_FILE + ".tmp"
        # ★2026-09-24 加存 bn（幣安 OI）：先前幣安被 451 擋著、這裡沒東西可存所以沒寫，
        #   改走 www.binance.com 之後有資料了就必須一起落地 —— 不然每次 redeploy
        #   幣安那一腳都要重等一小時，OI 變化% 會在「OKX 單腳」與「雙所平均」之間跳。
        with open(tmp, "w", encoding="utf-8") as f:
            json.dump({"v": 4, "ts": int(time.time()),
                       "oi": _pack(_oi_history), "px": _pack(_PX_HISTORY),
                       "bn": _pack(_BN_HISTORY),
                       # ★CVD/資費/多空比也要落地：它們是逐幣抓的（幣安沒有批量端點），
                       #   redeploy 後要一整輪才補得回來。而**沒有 CVD 的幣會掉進
                       #   scoreBreakdown 的粗略分支**（只看 OI×價格），短線反彈時
                       #   一堆幣被標成 `OI↑價↑ +12` → 分數整片偏多（用戶 2026-09-24
                       #   回報「他們也不會一堆什麼主力建多啊」，當下實測 CVD 0/282）。
                       "ex": {k: v for k, v in _BN_EXTRA.items()},
                       # ★掃描快照也存：它是掃描迴圈每根 K 收盤順手記的，純記憶體 →
                       #   redeploy 後「幣種」那頁整個空白、要等下一根 15m 收盤才有東西
                       #   （用戶 2026-09-24 回報「空的」）。每列都有 ts，讀回來會照實顯示幾分鐘前。
                       "scan": dashboard.snapshot()},
                      f, separators=(",", ":"))
        os.replace(tmp, _DASH_HIST_FILE)      # 原子替換,避免寫到一半被重啟砍成半截檔
    except Exception as e:
        print(f"[DASH] 取樣落地失敗(不影響交易): {e}", flush=True)


def _dash_hist_load() -> None:
    """啟動時讀回取樣歷史。太舊的點在這裡就濾掉（bot 停機期間的空窗不該被當成連續資料）。"""
    global _PX_HISTORY
    try:
        if not os.path.exists(_DASH_HIST_FILE):
            print("[DASH] 無歷史存檔,從零開始累積", flush=True)
            return
        with open(_DASH_HIST_FILE, encoding="utf-8") as f:
            d = json.load(f)
        keep_from = time.time() - (OI_MOVERS_WINDOW_H + 1) * 3600
        def _unpack(src):
            out = {}
            for k, pts in (src or {}).items():
                arr = [(float(t), float(v)) for t, v in pts if float(t) >= keep_from]
                if len(arr) >= 1:
                    out[k] = arr
            return out
        _oi_history.update(_unpack(d.get("oi")))
        _PX_HISTORY = _unpack(d.get("px"))
        _BN_HISTORY.update(_unpack(d.get("bn")))   # v1 舊檔沒這個鍵 → 空 dict，相容
        dashboard.restore(d.get("scan"))           # v2 以前沒有 scan → restore 自己會忽略
        try:                                       # v3 以前沒有 ex → 讀不到就算了
            for _k, _v in (d.get("ex") or {}).items():
                if isinstance(_v, dict):
                    _BN_EXTRA[_k] = dict(_v)
        except Exception:
            pass
        _depth = 0
        for _k, _h in _oi_history.items():
            if _h:
                _depth = max(_depth, int((time.time() - _h[0][0]) / 60))
        print(f"[DASH] 讀回取樣歷史:OI {len(_oi_history)} 幣 / 價 {len(_PX_HISTORY)} 幣 / "
              f"幣安OI {len(_BN_HISTORY)} 幣 / 補值 {len(_BN_EXTRA)} 幣,"
              f"最深 {_depth} 分鐘(存檔於 {int(time.time() - d.get('ts', 0)) // 60} 分鐘前)", flush=True)
    except Exception as e:
        print(f"[DASH] 讀回取樣歷史失敗(從零開始): {e}", flush=True)


_BN_HISTORY: Dict[str, list] = {}       # 幣安 OI（張數）instId -> [(ts, oi)]
_BN_STATE = {"ok": None, "fail": 0, "n": 0, "host": 0, "busy": False, "ms": 0}
# ★Cloudflare Worker 代理（繞開 Railway 出口 IP 被幣安地理封鎖 HTTP 451）。
#   設了才啟用；沒設就走原本的直連（會 451 然後自動停用）。
#   格式：DASH_BN_PROXY=https://xxx.workers.dev   DASH_BN_PROXY_KEY=<Worker 裡的 SECRET>
#   Worker 程式碼：trading-backtest/_cf_worker_binance_proxy.js
DASH_BN_PROXY = os.environ.get("DASH_BN_PROXY", "").rstrip("/")
DASH_BN_PROXY_KEY = os.environ.get("DASH_BN_PROXY_KEY", "")


def _bn_get(path: str, params: dict, timeout: int = 8):
    """打幣安公開端點：有設代理就走代理，沒有就直連。回 requests.Response 或 None。"""
    try:
        if DASH_BN_PROXY and DASH_BN_PROXY_KEY:
            q = dict(params or {})
            q["path"] = path
            q["k"] = DASH_BN_PROXY_KEY
            return requests.get(DASH_BN_PROXY, params=q, timeout=timeout)
        return requests.get(_BN_HOSTS[_BN_STATE["host"]] + path, params=params, timeout=timeout)
    except Exception:
        return None
# ★★★2026-09-24 破解：幣安的地理封鎖是**按網域**的，不是按 IP 一封到底。
#   Railway 出口實測（/d/<token>/egress 探針）：
#     fapi.binance.com  → 451 restricted location
#     api.binance.com   → 451
#     www.binance.com   → **200**，同一份 /fapi/v1/* 與 /futures/data/* 路徑照回真實資料
#   www 走的是網站主 CDN，跟 API 網域不同邊緣節點、不同封鎖名單。
#   已驗：7 個我們要的端點在 www 前置下全部 200（單幣OI/24h行情/資金費/OI歷史/
#   多空帳戶比/taker買賣比/合約資訊）；不需要瀏覽器 UA；回應帶 x-mbx-used-weight-1m
#   = 同一套限流基礎設施（上限 2400/分，我們 60 幣÷5 分鐘毫無壓力）。
#   ★教訓：先前我只試了 fapi 家族就下「幣安封到底了、只能開 VM」的結論 —— 測得不夠。
#     「某網域被封」不可外推成「這家全封」，要逐網域實測。
_BN_HOSTS = ["https://www.binance.com", "https://fapi.binance.com",
             "https://fapi1.binance.com", "https://fapi2.binance.com",
             "https://fapi3.binance.com", "https://fapi4.binance.com"]
BN_WORKERS = 6                          # 幣安逐幣端點的併發數（沒有批量版本）
DASH_BN_TOP_N = 120                     # 補幣安**OI**的幣數上限（一半取自 |OKX 變化| 榜、一半取自成交額榜）
# ★120 而不是 60：移到背景執行緒之後不再佔用交易主迴圈，成本只剩幣安限流，
#   而 120 幣／5 分鐘 = 24 權重/分，上限是 2400/分 —— 用掉 1%。
#   實測 60 幣時 281 個合約只有 46 個拿得到雙所平均（其餘只有 OKX 單腳）。


def _bn_oi_sample(now_s: float, keep_from: float) -> None:
    """★補幣安 OI，讓 OI 變化% 能跟官方一樣取「OKX 與幣安的算術平均」。

    幣安**沒有全市場 OI 的批量端點**（`/fapi/v1/openInterest` 一次一個幣），官方也是逐幣打。
    權重 1／次，每分鐘上限 2400，所以 60 幣 ÷ 5 分鐘完全在限額內。
    ★Railway 的 IP 曾被幣安地理封鎖（HTTP 451，開機連通性檢查就會印）。
      這裡連續失敗 3 次就整個停用，不會每輪重試洗 log；成功則自動恢復使用。
    存的是**張數**不是 USD —— 算變化% 不需要換算（合約面值不變時比值相同）。
    """
    global _BN_HISTORY
    if _BN_STATE["fail"] >= 3:
        return
    try:
        # 挑要補的幣：兩份名單取聯集，因為它們回答的是兩個不同的問題——
        #   ①|OKX 變化| 大的 = 「這輪最可能出現在排名表上的」
        #   ②24h 成交額大的 = 官方選幣層就是 `volume_top100`，要對齊就得把這群固定補上
        # 只用①的話，成交量大但這一刻沒在動的幣永遠拿不到幣安腳，
        # 於是同一個幣的 OI 變化% 會在「雙所平均」與「OKX 單腳」之間來回跳（定義不穩）。
        scored = []
        for inst, h in _oi_history.items():
            if len(h) >= 2 and h[0][1] > 0:
                scored.append((abs(h[-1][1] - h[0][1]) / h[0][1], h[-1][1], inst))
        scored.sort(reverse=True)
        by_move = [x[2] for x in scored[:DASH_BN_TOP_N // 2]]
        by_vol = [k for _v, k in sorted(
            (((v.get("volccy_usd") or 0), k) for k, v in _TICKER_SNAP.items()),
            reverse=True)[:DASH_BN_TOP_N // 2]]
        picks, _seen = [], set()
        for inst in by_move + by_vol:
            if inst in _oi_history and inst not in _seen:
                _seen.add(inst)
                picks.append(inst)
        ok = 0
        for inst in picks:
            sym = inst.replace("-USDT-SWAP", "") + "USDT"
            try:
                r = _bn_get("/fapi/v1/openInterest", {"symbol": sym}, timeout=6)
                if r is None:
                    continue
                if r.status_code == 451:
                    # 換下一個備援網域再試；全部試完才算真的不通
                    if _BN_STATE["host"] + 1 < len(_BN_HOSTS):
                        _BN_STATE["host"] += 1
                        print(f"[DASH] 幣安 451 → 改試備援網域 "
                              f"{_BN_HOSTS[_BN_STATE['host']]}", flush=True)
                        return
                    _BN_STATE["fail"] += 1
                    print(f"[DASH] 幣安 OI:{len(_BN_HOSTS)} 個網域全部被地理封鎖(451) → "
                          f"停用，OI 變化只用 OKX（第 {_BN_STATE['fail']}/3 次）", flush=True)
                    return
                if r.status_code != 200:
                    continue
                v = float(r.json().get("openInterest", 0) or 0)
                if v <= 0:
                    continue
                hh = _BN_HISTORY.setdefault(inst, [])
                hh.append((now_s, v))
                _BN_HISTORY[inst] = [(t, x) for (t, x) in hh if t >= keep_from] or [(now_s, v)]
                ok += 1
            except Exception:
                continue
            time.sleep(0.05)            # 節流：60 幣約 3 秒，不影響取樣週期
        _BN_STATE["ok"] = ok > 0
        _BN_STATE["n"] = ok
        if ok == 0:
            _BN_STATE["fail"] += 1
        else:
            _BN_STATE["fail"] = 0
        for k in list(_BN_HISTORY.keys()):
            if k not in _oi_history:
                del _BN_HISTORY[k]
        _bn_extra_sample(picks)
    except Exception as e:
        _BN_STATE["fail"] += 1
        print(f"[DASH] 幣安 OI 取樣失敗({_BN_STATE['fail']}/3): {e}", flush=True)


_BN_EXTRA: Dict[str, dict] = {}   # instId -> {"funding","long_pct","ls","cvd_ratio","ts"}
_BN_FUND_HIST: Dict[str, list] = {}   # instId -> [(ts, funding)]，算資費常態帶用
BN_FUND_KEEP_H = 48                   # 留 48 小時（官方 baseline 要 n≥24 才算數）


def _bn_fund_push(inst: str, fr: float, now_s: float) -> None:
    """累積資費樣本，給官方 `getFundingContext` 的 median／MAD 常態帶用。

    ★官方 `fundingBaselineMap` 是他們後端算好的；我們沒有那個端點（`/api/funding-baseline`
      只回 `{ts,data}`），所以自己累積。官方判準是 **n≥24 才 hasBase**，
      不足時退回固定門檻（general 0.01 / extreme 0.025）—— 那條 fallback 路徑我們照抄，
      所以「還沒累積夠」不會算錯，只會比較不靈敏。
    """
    try:
        h = _BN_FUND_HIST.setdefault(inst, [])
        h.append((now_s, float(fr)))
        cut = now_s - BN_FUND_KEEP_H * 3600
        _BN_FUND_HIST[inst] = [(t, v) for (t, v) in h if t >= cut] or [(now_s, float(fr))]
    except Exception:
        pass


def _struct_score(highs_a, lows_a, closes_a, cur_price):
    """★官方 `_analyzeStructure` + `_detectSwings` 的忠實移植（±22 分那一項）。

    原始碼是 2026-09-24 從他們前端 `String(window._analyzeStructure)` 直接取下來的
    （存檔 trading-backtest/_DHX_SCORE_SRC.js、規格 _DHX_SCORE_0924_SPEC.md），
    不是逆推。★兩個容易抄錯的點：
      ①`_detectSwings` 的 lb=2，判定用 **>= / <=** 反向淘汰 → 等價於「左右各 2 根嚴格極值」。
        寫成 `== max(...)` 在平盤上會讓每根都算樞紐（CLAUDE.md 記過這個坑）。
      ②EMA 的起手是**前 period 根的簡單平均**，不是第一根收盤。
    回 (score, label)。資料不足一律 (0, "")，不亂猜。
    """
    try:
        n = len(closes_a)
        if n < 10:
            return 0, ""
        lb = 2
        hi_pts, lo_pts = [], []
        for i in range(lb, n - lb):
            h, l = float(highs_a[i]), float(lows_a[i])
            is_h = is_l = True
            for j in range(i - lb, i + lb + 1):
                if j == i:
                    continue
                if float(highs_a[j]) >= h:
                    is_h = False
                if float(lows_a[j]) <= l:
                    is_l = False
            if is_h:
                hi_pts.append(h)
            if is_l:
                lo_pts.append(l)
        if len(hi_pts) < 2 or len(lo_pts) < 2:
            return 0, ""
        h2, h1 = hi_pts[-1], hi_pts[-2]
        l2, l1 = lo_pts[-1], lo_pts[-2]
        HH, HL = h2 > h1, l2 > l1
        LH, LL = h2 < h1, l2 < l1
        if (HH and LL) or (LH and HL):
            return 0, "盤整結構"

        def _ema(period):
            if n < period:
                return None
            k = 2.0 / (period + 1)
            v = sum(closes_a[:period]) / period
            for i in range(period, n):
                v = closes_a[i] * k + v * (1 - k)
            return v

        e20, e50 = _ema(20), _ema(50)
        cur = float(cur_price)
        macro_up = e20 is not None and e50 is not None and cur > e20 and e20 > e50
        macro_dn = e20 is not None and e50 is not None and cur < e20 and e20 < e50
        if macro_dn and LH and LL:
            return (22, "CHoCH↑") if cur > h2 else (-15, "下跌結構")
        if macro_up and HH and HL:
            return (-22, "CHoCH↓") if cur < l2 else (15, "上漲結構")
        if HH and HL:
            return (-22, "CHoCH↓") if cur < l2 else (15, "上漲結構")
        if LH and LL:
            return (22, "CHoCH↑") if cur > h2 else (-15, "下跌結構")
        return 0, ""
    except Exception:
        return 0, ""


_MCAP_STATE = {"ts": 0.0, "n": 0, "src": "", "err": "", "cg": 0, "cp": 0}
_MCAP_SRC: Dict[str, str] = {}        # COIN -> 這個市值是哪家給的（兩家口徑不同，要能查）
MCAP_REFRESH_SEC = 6 * 3600           # 成功後 6 小時再刷（幣價會動，7 天太久）
MCAP_RETRY_SEC = 20 * 60              # ★失敗後 20 分鐘就重試，不要等 6 小時


def _mcap_refresh(now_s: float) -> None:
    """補全市值 —— 「OI／市值」那一欄要有它才算得出來。

    ★原本 `_MCAP` 只在 `build_dynamic_symbols` 裡順手撈 CoinGecko **前 100 名**，
      而且那支**本身就常被 429**（線上實測 log：`[SYMBOLS] CoinGecko HTTP 429`
      → 連幣種列表都退回 OKX 流動性底池）。結果儀表板 476 個合約的 OI／市值
      幾乎全是「—」（用戶 2026-09-24 問「是沒有的嗎」）。

    ★★**兩個來源的市值不可以混著用而不標記**（CLAUDE.md：「同一個欄名、多個來源」）。
      實測 361 個共同幣：比值中位數 1.000（多數完全一致），但 **24% 的幣差超過 20%**，
      而且 CoinPaprika 系統性偏低（p10 = 0.44）—— 例：ZRO CoinPaprika 164M vs
      CoinGecko 527M（0.31 倍），差在流通量口徑（ZRO 有持續解鎖）。
      官方 `oi-cache` 的 `market_cap` 站在 **CoinGecko 那一側**（ZRO 549M）。
      → 所以 **CoinGecko 優先**（對得上官方），CoinPaprika 只補它沒有的長尾。

    ★成本：CoinPaprika `/v1/tickers?limit=2000` 一支就回 ~1900 個幣、免金鑰、實測 0.8s；
      CoinGecko 要 250 一頁打 4 次。兩者加起來 5 支 × 6 小時 = 一天 20 支，可忽略。
      兩邊都是市值降序、`symbol` 會重複（CoinPaprika 2000 筆有 108 個重複），
      「先到的不覆蓋」＝ 取市值最大的那個，才對得到交易所上的主流標的。
    ★失敗一定要印出來：先前寫成 `if status != 200: break` 又不記錄，
      429 就成了**沉默失敗**，線上看起來只是「這欄一直沒有值」。
    """
    # ★冷卻要看「**CoinGecko** 拿到沒」，不是「有沒有拿到任何東西」。
    #   CoinGecko 在 Railway 幾乎必 429，但那是共用出口 IP 的速率限制、不是永久封鎖，
    #   所以只要還沒拿到它，就每 20 分鐘再試一次 —— 哪一輪通了就覆蓋掉 CoinPaprika 的值。
    #   （原本寫成「CoinPaprika 成功就冷卻 6 小時」＝ 等於直接放棄對齊官方口徑。）
    gap = MCAP_REFRESH_SEC if _MCAP_STATE.get("cg") else MCAP_RETRY_SEC
    if now_s - _MCAP_STATE["ts"] < gap:
        return
    _MCAP_STATE["ts"] = now_s
    tail, main_, err = {}, {}, ""

    # ① 長尾：CoinPaprika，一支呼叫
    try:
        r = requests.get("https://api.coinpaprika.com/v1/tickers",
                         params={"limit": 2000},
                         headers={"User-Agent": "Mozilla/5.0",
                                  "Accept": "application/json"}, timeout=25)
        if r.status_code == 200:
            for c in r.json() or []:
                try:
                    sym = str(c.get("symbol") or "").upper()
                    mc = float(((c.get("quotes") or {}).get("USD") or {}).get("market_cap") or 0)
                    if sym and mc > 0 and sym not in tail:
                        tail[sym] = mc
                except (TypeError, ValueError, AttributeError):
                    continue
        else:
            err = f"coinpaprika HTTP {r.status_code}"
    except Exception as e:
        err = f"coinpaprika {type(e).__name__}: {e}"

    # ② 主來源：CoinGecko（對齊官方口徑）。雲端常 429，拿得到多少算多少。
    try:
        for page in (1, 2, 3, 4):
            r = requests.get(
                "https://api.coingecko.com/api/v3/coins/markets",
                params={"vs_currency": "usd", "order": "market_cap_desc",
                        "per_page": 250, "page": page, "sparkline": "false"},
                headers={"User-Agent": "Mozilla/5.0",
                         "Accept": "application/json"}, timeout=20)
            if r.status_code != 200:
                err += (" / " if err else "") + f"coingecko p{page} HTTP {r.status_code}"
                break
            rows = r.json() or []
            if not rows:
                break
            for c in rows:
                try:
                    sym = str(c.get("symbol") or "").upper()
                    mc = float(c.get("market_cap") or 0)
                    if sym and mc > 0 and sym not in main_:
                        main_[sym] = mc
                except (TypeError, ValueError):
                    continue
            time.sleep(2.0)
    except Exception as e:
        err += (" / " if err else "") + f"coingecko {type(e).__name__}: {e}"

    merged = dict(tail)
    merged.update(main_)                # ★CoinGecko 覆蓋 CoinPaprika（口徑對齊官方）
    if merged:
        _MCAP.update(merged)
        _MCAP_SRC.update({k: ("coingecko" if k in main_ else "coinpaprika") for k in merged})
        _MCAP_STATE.update({"n": len(_MCAP), "cg": len(main_), "cp": len(tail),
                            "src": "coingecko" if main_ else "coinpaprika", "err": err})
        print(f"[DASH] 市值刷新:CoinGecko {len(main_)} + CoinPaprika 補 "
              f"{len(merged) - len(main_)} → 累計 {len(_MCAP)}"
              + (f"（部分失敗: {err}）" if err else ""), flush=True)
    else:
        _MCAP_STATE["err"] = err or "無資料"
        print(f"[DASH] 市值刷新失敗(不影響交易,{MCAP_RETRY_SEC // 60} 分鐘後重試): "
              f"{_MCAP_STATE['err']}", flush=True)


def _bn_fund_base(inst: str):
    """回 (median, mad, n)。n<24 時呼叫端要當成 hasBase=False（照官方）。"""
    h = _BN_FUND_HIST.get(inst) or []
    vals = sorted(v for _t, v in h)
    n = len(vals)
    if n == 0:
        return 0.0, 0.0, 0
    med = vals[n // 2] if n % 2 else (vals[n // 2 - 1] + vals[n // 2]) / 2.0
    dev = sorted(abs(v - med) for v in vals)
    mad = dev[n // 2] if n % 2 else (dev[n // 2 - 1] + dev[n // 2]) / 2.0
    return med, mad, n


def _bn_extra_sample(picks: list) -> None:
    """補**資費／多空帳戶比／合約 CVD** —— 官方評分公式要用它們，字卡也要顯示。
    跟 OI 一樣跑在背景執行緒裡，網頁端零 API。

    ★★2026-09-24 與官方逐幣對帳後大改：原本只補 `picks`(120 幣)，
      於是 281 個合約裡只有 97 個拿得到 CVD。**沒有 CVD 的幣會掉進
      `scoreBreakdown` 的粗略分支（只看 OI×價格）**，而那條分支在短線反彈時
      把一堆幣標成 `OI↑價↑ +12` → 我的分數 68% 為正、官方同時刻只有 21%
      （官方 185/281 走 CVD 分支，我只有 97）。CVD 是 ±40 的主分，不能只補一部分。
    ★所以 CVD 與多空比改成**覆蓋整個追蹤池**，並用小執行緒池並行
      （逐幣端點沒有批量版本；序列跑 281×2 支 × 0.35s ≈ 200 秒，會吃掉整個取樣週期）。
    ★用量：資費 1 支(全市場) + CVD 281 + 多空比 281 ≈ 563 支／5 分鐘 ≈ 113 權重/分，
      幣安上限 2400/分 → 約 5%。併發只開 `BN_WORKERS` 條，不打爆對方也不被限流。
    ★資費是**幣安**的，不是 OKX。這裡的用途是複刻數據獵手的評分（他們用幣安／CoinGlass），
      字卡會標來源；不要拿它當 OKX 部位的資費依據（CLAUDE.md 記過兩所符號不一致 36.3%）。
    """
    try:
        # ① 資費：不帶 symbol 一次回整個市場（實測 910 支、0.33s、權重 10）
        r = _bn_get("/fapi/v1/premiumIndex", {}, timeout=12)
        fmap = {}
        if r is not None and r.status_code == 200:
            for x in r.json() or []:
                try:
                    fmap[x["symbol"]] = float(x.get("lastFundingRate") or 0)
                except (KeyError, TypeError, ValueError):
                    continue
        now_s = time.time()

        # ② 逐幣的兩支：整個追蹤池都補，不再只補 picks
        targets = [i for i in _oi_history.keys()]
        if not targets:
            targets = list(picks or [])

        def _one(inst):
            sym = inst.replace("-USDT-SWAP", "") + "USDT"
            out = {}
            try:
                # cvd_ratio：定義已對回官方（`binance_taker_ratio`）——
                # 12 根 5m(=1H) 的 (buyVol−sellVol)/(buyVol+sellVol)×100。
                rc = _bn_get("/futures/data/takerlongshortRatio",
                             {"symbol": sym, "period": "5m", "limit": 12}, timeout=8)
                if rc is not None and rc.status_code == 200:
                    arr = rc.json()
                    if isinstance(arr, list) and arr:
                        bv = sum(float(x.get("buyVol") or 0) for x in arr)
                        sv = sum(float(x.get("sellVol") or 0) for x in arr)
                        if bv + sv > 0:
                            out["cvd_ratio"] = (bv - sv) / (bv + sv) * 100.0
            except Exception:
                pass
            try:
                # 官方 scoreBreakdown 用的是 longPct（多方**帳戶佔比 %**，門檻 40/45/55/60），
                # 不是 longShortRatio（那是倍數）。
                rr = _bn_get("/futures/data/globalLongShortAccountRatio",
                             {"symbol": sym, "period": "5m", "limit": 1}, timeout=8)
                if rr is not None and rr.status_code == 200:
                    arr = rr.json()
                    if isinstance(arr, list) and arr:
                        out["long_pct"] = float(arr[-1].get("longAccount") or 0) * 100 or None
                        out["ls"] = float(arr[-1].get("longShortRatio") or 0) or None
            except Exception:
                pass
            return inst, out

        got = 0
        with ThreadPoolExecutor(max_workers=BN_WORKERS) as ex:
            for inst, out in ex.map(_one, targets):
                rec = _BN_EXTRA.get(inst) or {}
                sym = inst.replace("-USDT-SWAP", "") + "USDT"
                if sym in fmap:
                    rec["funding"] = fmap[sym]
                    _bn_fund_push(inst, fmap[sym], now_s)
                rec.update(out)
                if rec:
                    rec["ts"] = now_s
                    _BN_EXTRA[inst] = rec
                    got += 1
        for k in list(_BN_EXTRA.keys()):
            if k not in _oi_history:
                del _BN_EXTRA[k]
        _BN_STATE["extra"] = got
        _BN_STATE["cvd"] = sum(1 for v in _BN_EXTRA.values() if v.get("cvd_ratio") is not None)
        _BN_STATE["fund_all"] = len(fmap)
        print(f"[DASH] 幣安補值:{got} 幣(CVD {_BN_STATE['cvd']} / 資費源 {len(fmap)})", flush=True)
    except Exception as e:
        print(f"[DASH] 幣安 資費/多空比/CVD 取樣失敗(不影響交易): {e}", flush=True)


_DHX_SIG = {}                    # 數據訊號結果：inst -> dict（給儀表板顯示）
_DHX_STATE = {"ts": 0.0, "i": 0, "n": 0}
DHX_SCAN_SEC = 900               # 15 分鐘掃一次（官方全部訊號都是 15m 時框）
DHX_SCAN_BATCH = 8               # 每輪只掃 |OI 變化| 最大的 N 幣（CVD 要逐幣翻頁，很貴）


def _dhx_scan(force: bool = False) -> None:
    """★複刻數據獵手「數據訊號」的三個家族（規格 trading-backtest/_DHX_DATASIG_0924_SPEC.md，
    來自他們 `/api/signals?type=data_hunter` 的 158 筆**原始欄位**，不是逆推猜的）。

    官方 `rule_version` 把結構寫在名字裡：
      `TRAP_CONFIRMED_PIVOT_I1_CLOSE_RECLAIM_NATIVE_BB_CVD`
         樞紐 i1 → 假突破(breakout_extreme) → **收盤收回 i1 收盤價** 才 CONFIRMED
      `ABSORPTION_I2_FORMATION_FULL_WICK_NATIVE_BB_CVD`
         價格創新低(i2<i1) 但 **CVD 沒跟著創新低** → 賣壓被吸收 → 做多（反之做空）
      `EXHAUSTION_I2_FORMATION_FULL_WICK_NATIVE_BB_CVD`
         價格創新高(i2>i1) 但 **CVD 沒跟著創新高** → 買盤衰竭 → 做空（反之做多）
    共通：全部 **15m**；停損放 i2 的**完整影線**外緣（官方 `sl_source: i2_full_wick_low/high`）。

    ★CVD 用 bot 既有的 `_okx_contract_cvd_15m`（OKX rubik，翻頁拿 36h）。
      官方另有**現貨 CVD**（`spot_cvd_i1/i2`），我沒有來源 → 欄位給 None，不假裝有。
    只顯示、不下單；全程 try/except。
    """
    global _DHX_SIG
    if not force and time.time() - _DHX_STATE["ts"] < DHX_SCAN_SEC:
        return
    _DHX_STATE["ts"] = time.time()
    try:
        # ★選幣層照官方：`source: "volume_top100"`（63/63 筆全部都是）＝**24h 成交額前 100**。
        #   原本用「|OI 變化| 最大」是我自己選的，不是他們的做法。
        #   前 100 每輪只掃得動 DHX_SCAN_BATCH 個（CVD 要逐幣打），所以用游標輪替掃完。
        pool = sorted(((v.get("volccy_usd") or 0), k) for k, v in (_TICKER_SNAP or {}).items())
        pool = [k for _, k in pool[::-1][:100]]
        if not pool:
            return
        _i = int(_DHX_STATE.get("i", 0)) % len(pool)
        picks = [pool[(_i + d) % len(pool)] for d in range(DHX_SCAN_BATCH)]
        _DHX_STATE["i"] = (_i + DHX_SCAN_BATCH) % len(pool)
        found = {}
        for inst in picks:
            try:
                df = fetch_market_candles(inst, "15m")
                if df.empty or len(df) < 80:
                    continue
                hi = df["high"].values; lo = df["low"].values
                cl = df["close"].values; n = len(cl)
                # CVD 先抓（ABSORPTION/EXHAUSTION 的判定需要它，不能等掃完才補）
                cv = sv = None
                try:
                    _c = _okx_contract_cvd_15m(inst, df.index)
                    if _c is not None and len(_c) >= 40:
                        cv = _c.values.astype(float)
                except Exception:
                    cv = None
                try:
                    _s = _okx_spot_cvd_15m(inst, df.index)   # ★現貨 CVD（instType=SPOT，48h）
                    if _s is not None and len(_s) >= 40:
                        sv = _s.values.astype(float)
                except Exception:
                    sv = None
                _ts = [int(x.timestamp()) for x in df.index]
                r = (_dhx_trap(inst, hi, lo, cl, n, cv, sv, _ts)
                     or _dhx_absorb(inst, hi, lo, cl, n, cv, sv, _ts)
                     or _dhx_exhaust(inst, hi, lo, cl, n, cv, sv, _ts))
                if r:
                    found[inst] = r
            except Exception:
                continue
        _DHX_SIG = found
        _DHX_STATE["n"] = len(found)
        if found:
            print("[DHX] 數據訊號 %d 筆: %s" % (len(found),
                  [x["inst"].replace("-USDT-SWAP", "") + ":" + x["kind"] for x in found.values()]),
                  flush=True)
    except Exception as e:
        print(f"[DHX] 數據訊號掃描失敗(不影響交易): {e}", flush=True)


def _dhx_pivot(hi, lo, n, side, look=46, conf=3, skip=8):
    """往回找樞紐（左右各 conf 根確認）。side='low' 找樞紐低、'high' 找樞紐高。

    ★`skip`：**跳過最近 skip 根**才開始找。沒有這個會出大事 ——
      假突破的那一根（i2）本身就是個樞紐低/高，不跳過就會被當成 i1，
      接著在「i1 之後」找 i2 自然找不到，整個型態永遠不觸發。
      （2026-09-24 自己寫的測試抓到，三個家族全都不觸發就是這個原因。）
    """
    for k in range(n - conf - skip, n - look, -1):
        if k < conf + 1:
            break
        # ★要**嚴格**極值：用 `== min(...)` 在平盤/平台上會讓「每一根都算樞紐」，
        #   於是永遠回傳最靠近的那根，後面的型態判定全部落空
        #   （2026-09-24 測試抓到：三個家族都不觸發就是這個）。
        #   定義與 bot 既有的 `_find_pivot_low` 一致：左右各 conf 根都要嚴格比它高/低。
        if side == "low" and all(lo[j] > lo[k] for j in range(k - conf, k + conf + 1) if j != k):
            return k
        if side == "high" and all(hi[j] < hi[k] for j in range(k - conf, k + conf + 1) if j != k):
            return k
    return None


def _dhx_pack(inst, kind, bias, i1, i2, back, cl, sl, n, extra=None):
    entry = float(cl[-1]); risk = abs(entry - sl)
    if risk <= 0 or risk / entry > 0.08 or risk / entry < 0.001:
        return None
    # ★★停損必須在「會虧錢」的那一側：做多 sl < 進場、做空 sl > 進場。
    #   2026-09-24 用戶抓到 F 這筆：吸收做空但 sl 0.003673 < entry 0.003703 ——
    #   價格早就漲過 pivot2 的高點，那個停損等於無效（設在獲利方向）。
    #   官方 63 筆**停損方向錯的是 0 筆**，所以這確定是我漏掉的檢查。
    #   同型坑 memory 記過：live 的 `calculate_sl <= close → 跳過`。
    if bias == "LONG" and sl >= entry:
        return None
    if bias == "SHORT" and sl <= entry:
        return None
    d = 1 if bias == "LONG" else -1
    r = {"inst": inst, "kind": kind, "bias": bias, "tf": "15m", "level": "CONFIRMED",
         "i1_close": float(cl[i1]), "breakout_extreme": float(sl), "close_back": float(cl[back]),
         "entry": entry, "sl": float(sl), "sl_dist_pct": round(risk / entry * 100, 3),
         "sl_source": "i2_full_wick_" + ("low" if d > 0 else "high"),
         "tp1": round(entry + d * risk, 8), "tp2": round(entry + d * risk * 1.5, 8),
         "tp3": round(entry + d * risk * 2.0, 8),
         "i1_i2_dist": int(i2 - i1), "bars_since": int(n - 1 - back),
         "fut_cvd_i1": None, "fut_cvd_i2": None,
         "spot_cvd_i1": None, "spot_cvd_i2": None,   # ★官方有現貨CVD，我沒有來源
         "ts": time.time()}
    if extra:
        r.update(extra)
    return r




def _dhx_cvd(cv, sv, i1, i2):
    """把兩個錨點的合約／現貨 CVD 包成官方那四個欄位。拿不到就留 None。"""
    o = {}
    try:
        if cv is not None and len(cv) > max(i1, i2):
            o["fut_cvd_i1"] = float(cv[i1]); o["fut_cvd_i2"] = float(cv[i2])
    except Exception:
        pass
    try:
        if sv is not None and len(sv) > max(i1, i2):
            o["spot_cvd_i1"] = float(sv[i1]); o["spot_cvd_i2"] = float(sv[i2])
    except Exception:
        pass
    return o


def _dhx_oi_delta(inst, t1, t2):
    """兩個錨點之間的 OI 變化%（官方 `oi_delta_pct` / `oi_pivot1`,`oi_pivot2`）。
    用 `_oi_history`（5 分鐘取樣）線性內插取值；取不到回 None（不猜）。"""
    h = _oi_history.get(inst)
    if not h or len(h) < 2:
        return None

    def _val(ts):
        prev = None
        for t, v in h:
            if t <= ts:
                prev = (t, v)
            elif prev is None:
                return None
            else:
                if t - prev[0] > 3600:
                    return None
                f = (ts - prev[0]) / (t - prev[0])
                return prev[1] + (v - prev[1]) * f
        return h[-1][1] if prev is not None else None

    a1, a2 = _val(t1), _val(t2)
    if not a1 or not a2 or a1 <= 0:
        return None
    return (a2 - a1) / a1 * 100.0


def _dhx_trap(inst, hi, lo, cl, n, cv=None, sv=None, ts=None):
    """TRAP：樞紐 → 假突破 → 收盤收回 i1 收盤價 → **再過數據層**。

    ★數據層是官方的硬條件（400 筆實測 100% 一致，見 _DHX_DATASIG_V2_0924.md）：
        SHORT_TRAP(做多)：合約 CVD **降** + 現貨 CVD **升**   （實測 0/60、59/60）
        LONG_TRAP (做空)：合約 CVD **升** + 現貨 CVD **降**   （實測 36/36、0/36）
      另外兩類的 OI 變化中位都是 **正的**（+1.50% / +1.37%）→ 要求 OI 升。
    ★第一版我只做價格型態、沒有數據層，等於發一堆他們根本不會發的訊號。
    """
    for side in ("short_trap", "long_trap"):
        i1 = _dhx_pivot(hi, lo, n, "low" if side == "short_trap" else "high")
        if i1 is None:
            continue
        i2 = None
        for k in range(i1 + 1, n):
            if side == "short_trap" and lo[k] < lo[i1]:
                i2 = k
            elif side == "long_trap" and hi[k] > hi[i1]:
                i2 = k
        if i2 is None or i2 >= n - 1:
            continue
        back = None
        for k in range(i2 + 1, n):
            if side == "short_trap" and cl[k] > cl[i1]:
                back = k; break
            if side == "long_trap" and cl[k] < cl[i1]:
                back = k; break
        if back is None or (n - 1 - back) > 4:
            continue
        # ── 數據層（官方硬條件）：沒有 CVD 就不發，不再「只看型態」
        if cv is None or sv is None or len(cv) <= i2 or len(sv) <= i2:
            continue
        fut_up = float(cv[i2]) > float(cv[i1])
        spot_up = float(sv[i2]) > float(sv[i1])
        if side == "short_trap" and not ((not fut_up) and spot_up):
            continue                     # 做多要：合約降 + 現貨升
        if side == "long_trap" and not (fut_up and (not spot_up)):
            continue                     # 做空要：合約升 + 現貨降
        oi_d = _dhx_oi_delta(inst, ts[i1], ts[i2]) if ts is not None else None
        if oi_d is not None and oi_d <= 0:
            continue                     # OI 要升（官方中位 +1.4~1.5%）
        sl = float(lo[i2]) if side == "short_trap" else float(hi[i2])
        ex = _dhx_cvd(cv, sv, i1, i2)
        ex["oi_delta_pct"] = None if oi_d is None else round(oi_d, 3)
        ex["entry_source"] = "trigger_market"
        r = _dhx_pack(inst, "SHORT_TRAP" if side == "short_trap" else "LONG_TRAP",
                      "LONG" if side == "short_trap" else "SHORT", i1, i2, back, cl, sl, n, ex)
        if r:
            return r
    return None


def _dhx_exhaust(inst, hi, lo, cl, n, cv=None, sv=None, ts=None):
    """衰竭背離（exhaustion）—— **多空各一邊**。

    ★官方 `cvd_signal` 原話（2026-09-24 從他們 API 直接抓到）：
        做多「底衰竭：賣方**砸破前低**但 CVD **未創新低**，空方力竭」
        做空「頂衰竭：買方**突破前高**但 CVD **未創新高**，多方力竭」
    ★與吸收的差別只有一個：**價格有沒有破前低／前高**
        吸收＝沒破（低點抬高）＋ CVD 創新低
        衰竭＝**破了** ＋ CVD **沒有**創新低
      我第一版把「價創新低但 CVD 沒跟著低」命名成 ABSORPTION —— 那其實就是**衰竭**，名字錯置。
    """
    if cv is None:
        return None
    for side in ("long", "short"):
        want_low = (side == "long")
        p1 = _dhx_pivot(hi, lo, n, "low" if want_low else "high", look=45, conf=3, skip=12)
        if p1 is None:
            continue
        p2 = None
        for k in range(n - 4, p1 + 3, -1):
            if want_low:
                # ★破前低（與吸收相反）
                if lo[k] < lo[p1] and all(lo[j] > lo[k] for j in range(k - 2, k + 3) if j != k):
                    p2 = k; break
            else:
                if hi[k] > hi[p1] and all(hi[j] < hi[k] for j in range(k - 2, k + 3) if j != k):
                    p2 = k; break
        if p2 is None or len(cv) <= p2:
            continue
        c1, c2 = float(cv[p1]), float(cv[p2])
        if want_low and not (c2 > c1):
            continue                     # 做多要：CVD **未創新低**（空方力竭）
        if (not want_low) and not (c2 < c1):
            continue                     # 做空要：CVD **未創新高**（多方力竭）
        # 確認：p2 之後有一根往回收
        back = None
        for k in range(p2 + 1, n):
            if want_low and cl[k] > cl[p2] and cl[k] > (hi[k] + lo[k]) / 2:
                back = k; break
            if (not want_low) and cl[k] < cl[p2] and cl[k] < (hi[k] + lo[k]) / 2:
                back = k; break
        if back is None or (n - 1 - back) > 4:
            continue
        oi_d = _dhx_oi_delta(inst, ts[p1], ts[p2]) if ts is not None else None
        sl = float(lo[p2]) if want_low else float(hi[p2])
        ex = _dhx_cvd(cv, sv, p1, p2)
        ex["oi_delta_pct"] = None if oi_d is None else round(oi_d, 3)
        ex["sl_source"] = "i2_full_wick_" + ("low" if want_low else "high")
        ex["entry_source"] = "confirm_close"
        ex["pivot1_price"] = float(lo[p1] if want_low else hi[p1])
        ex["pivot2_price"] = sl
        r = _dhx_pack(inst, "EXHAUSTION", "LONG" if want_low else "SHORT",
                      p1, p2, back, cl, sl, n, ex)
        if r:
            r["sl_source"] = ex["sl_source"]
            return r
    return None


def _dhx_absorb(inst, hi, lo, cl, n, cv=None, sv=None, ts=None):
    """吸收背離（absorption）—— **多空各一邊**。

    ★官方 `cvd_signal` 原話：「底背離吸收：**賣方砸盤但價格未破前低**，買方限價單吸收賣壓」
      實際資料 `pivot1_price 8.821 → pivot2_price 10.331` = **低點抬高**，不是創新低。
      我第一版寫成「價格創新低但 CVD 沒跟著低」→ **方向完全相反**，已改正。
    ★這個定義與我自己 memory `project_0830_absorb_long` 早就驗過的一致：
      兩個樞紐低點**抬高** + **CVD 樞紐低點降低** + OI 上升。
    ★停損 = **pivot2 的價格**（官方 `sl_price` 就等於 `pivot2_price`，
      `sl_source: post_i2_structure_low/high` 也是這個意思），不是完整影線。
    """
    if cv is None:
        return None
    for side in ("long", "short"):
        want_low = (side == "long")
        p1 = _dhx_pivot(hi, lo, n, "low" if want_low else "high", look=45, conf=3, skip=14)
        if p1 is None:
            continue
        p2 = None
        for k in range(n - 4, p1 + 3, -1):
            if want_low:
                # 低點**抬高**（不是創新低），且是嚴格樞紐低
                if lo[k] > lo[p1] and all(lo[j] > lo[k] for j in range(k - 2, k + 3) if j != k):
                    p2 = k; break
            else:
                if hi[k] < hi[p1] and all(hi[j] < hi[k] for j in range(k - 2, k + 3) if j != k):
                    p2 = k; break
        if p2 is None or len(cv) <= p2:
            continue
        c1, c2 = float(cv[p1]), float(cv[p2])
        if want_low and not (c2 < c1):
            continue                     # 做多要：CVD 樞紐低點**降低**（賣方砸盤）
        if (not want_low) and not (c2 > c1):
            continue                     # 做空鏡像：CVD 樞紐高點升高
        # ★OI 只記錄、**不過濾**：官方吸收/衰竭的 oi_pivot1→oi_pivot2 中位是
        #   185.1M → 184.7M（**略降**），OI 上升是 TRAP 才有的條件。
        oi_d = _dhx_oi_delta(inst, ts[p1], ts[p2]) if ts is not None else None
        sl = float(lo[p2]) if want_low else float(hi[p2])   # ★停損＝pivot2 價格
        ex = _dhx_cvd(cv, sv, p1, p2)
        ex["oi_delta_pct"] = None if oi_d is None else round(oi_d, 3)
        ex["sl_source"] = "post_i2_structure_" + ("low" if want_low else "high")
        ex["entry_source"] = "confirm_close"
        ex["pivot1_price"] = float(lo[p1] if want_low else hi[p1])
        ex["pivot2_price"] = sl
        r = _dhx_pack(inst, "ABSORPTION", "LONG" if want_low else "SHORT",
                      p1, p2, n - 1, cl, sl, n, ex)
        if r:
            r["sl_source"] = ex["sl_source"]
            return r
    return None


def _dash_sampler_loop() -> None:
    """★★取樣自己一條執行緒，不再搭交易主迴圈的便車（2026-09-24 用戶問「1H 為什麼沒資料」查出來的）。

    原本 `_oi_sample_tick()` 是在 `main_polling_loop` 裡呼叫的，而那個迴圈開頭是
    `synchronise_and_wait_next_candle()` —— 它會**卡住等下一根 15 分 K 收盤**。
    所以 `DASH_SAMPLE_SEC=300` 根本沒生效，**實際取樣間隔是 15 分鐘**。
    而內插容差 `max(DASH_SAMPLE_SEC*4, 1800)` = 30 分鐘 = **只有兩個間隔的餘裕**：
    redeploy 時錯過一兩次取樣就破表 → 歷史開洞 → 起點落在洞裡的那個窗整個算不出來。
    實測（連推九次之後）洞在 **49~79 分鐘前**，正好吃掉 1H 窗（官方排名與評分用的就是它）。

    ★落地（`_dash_hist_save`）救得了「已經取到的點」，救不了「根本沒去取」的那幾次 ——
      這是我先前說「資料會記著所以不怕重推」漏掉的那一半。
    ★改成獨立執行緒後：間隔真的是 `DASH_SAMPLE_SEC`(5 分鐘)，而且**不再受交易迴圈阻塞**，
      重啟只會損失開機那一小段（約 1~2 分鐘），遠小於 30 分鐘容差。
    """
    while True:
        try:
            time.sleep(DASH_SAMPLE_SEC)
            # 自己就是節拍器，不必再靠 DASH_SAMPLE_SEC 節流
            _oi_sample_tick(force=True)
        except Exception as e:
            print(f"[DASH] 取樣執行緒例外(不影響交易): {e}", flush=True)
            time.sleep(30)


_WHALE: Dict[str, dict] = {}     # 巨鯨雷達：inst -> 資金注入候選事件
WHALE_OBS_SEC = 900              # ★官方原話：「觀察 **15 分鐘** 後…判斷方向」
WHALE_VALID_H = 6                # 事件留多久（官方沒公布，這是我設的）
WHALE_MAX = 40


def _whale_scan(now_s: float) -> None:
    """★巨鯨雷達 —— 跟「視覺篩選器」是**兩個東西**，這是用戶 2026-09-24 指正我的。

    同一個分頁（他們前端 `data-target-tab="visual"`）在主選單叫「巨鯨雷達」、
    在引導模式選單叫「視覺篩選器」，但頁面裡裝的是**兩套不同門檻的產物**：
      ·「持倉 × 價格象限圖」＝ 瀏覽/篩選工具，OI ≥1%、|價格| ≤5%，
        官方明說「**象限只描述持倉與價格，不直接判定多空**」→ 這是「篩選器」那半。
      ·「資金注入候選」＝ **警報產品**，1H OI ≥4%、|價格| ≤3%，**會產生卡片與通知**，
        而且有後續流程 → 這是「雷達」那半。
    決定性證據是他們自己的契約字串：`batch_title: "巨鯨雷達｜資金注入候選 {count} 個"`
    —— 巨鯨雷達是產品名，資金注入候選是它的產出。

    官方流程（原話）：「先找出 1H 資金注入候選；**觀察 15 分鐘後**，
    以 **OI 保留、相對 BTC 強弱與 CVD** 判斷方向。15m／30m 僅觀察變化，
    不另產生卡片或通知。」方向標籤 `direction_labels`：
    bull=偏多／bear=偏空／pending=觀察中／none=方向未成立。

    ★誠實標記：**三個判斷因子與 15 分鐘、4%/3% 門檻都是官方的**，
      但「三個因子怎麼合成一個方向」官方沒公布（server-side），
      下面的合成規則（各記 ±1、總分 ≥2 偏多 / ≤−2 偏空 / 其餘方向未成立）**是我訂的**。

    ★用 `dashboard._at` 做內插，不另寫一份 —— OI 變化的算法只能有一個實作
      （memory 記過「OI 公式逐行抄、改一邊要改兩邊」的教訓）。
    """
    try:
        gap = max(DASH_SAMPLE_SEC * 4, 1800.0)
        t1 = now_s - 3600.0

        def _chg1(hist):
            if not hist or len(hist) < 2:
                return None
            b = dashboard._at(hist, t1, gap)
            if not b or b[1] <= 0:
                return None
            return (hist[-1][1] - b[1]) / b[1]

        btc_px = _chg1(_PX_HISTORY.get("BTC-USDT-SWAP"))
        for inst, h in list(_oi_history.items()):
            oi1 = _chg1(h)
            px1 = _chg1(_PX_HISTORY.get(inst))
            if oi1 is None or px1 is None:
                continue
            ev = _WHALE.get(inst)
            if oi1 >= 0.04 and abs(px1) <= 0.03:          # 官方固定門檻
                if not ev:
                    if len(_WHALE) >= WHALE_MAX:
                        continue
                    ev = {"inst": inst, "first_ts": now_s, "oi0": oi1, "px0": px1,
                          "oi_at": h[-1][1], "dir": "pending", "judged_ts": 0.0,
                          "note": ""}
                    _WHALE[inst] = ev
                ev["last_ts"] = now_s
                ev["oi"] = oi1
                ev["px"] = px1
            if not ev:
                continue
            # 觀察滿 15 分鐘 → 判方向（只判一次，之後不再變，跟異常警報的狀態機同慣例）
            if ev["dir"] == "pending" and now_s - ev["first_ts"] >= WHALE_OBS_SEC:
                score, why = 0, []
                # ① OI 保留：當初那筆增量還在不在（張數沒有掉回去）
                keep = h[-1][1] >= ev["oi_at"] * 0.98
                score += 1 if keep else -1
                why.append("OI保留✓" if keep else "OI已回吐")
                # ② 相對 BTC 強弱（1H）
                if btc_px is not None and px1 is not None:
                    rel = px1 - btc_px
                    score += 1 if rel > 0 else -1
                    why.append(f"對BTC {rel * 100:+.2f}%")
                # ③ CVD 方向（背景取樣好的幣安 taker 比）
                cvd = (_BN_EXTRA.get(inst) or {}).get("cvd_ratio")
                if cvd is not None:
                    score += 1 if cvd > 0 else -1
                    why.append(f"CVD {cvd:+.1f}%")
                ev["dir"] = "bull" if score >= 2 else ("bear" if score <= -2 else "none")
                ev["score"] = score
                ev["note"] = "・".join(why)
                ev["judged_ts"] = now_s
        cut = now_s - WHALE_VALID_H * 3600
        for k in [k for k, v in _WHALE.items() if v.get("last_ts", 0) < cut]:
            del _WHALE[k]
    except Exception as e:
        print(f"[WHALE] 掃描例外(不影響交易): {e}", flush=True)


_ANOM = {}                       # 異常警報事件池：coin -> 事件 dict
ANOM_PX_TH = 3.0                 # ★官方門檻：price_15m / price_5m 都是 ≥3.00%（1000 筆實測最小 3.001）
ANOM_VALID_H = 6                 # 事件有效期（官方有 event_valid_until，實際值沒抓到，這是我設的）
ANOM_MAX = 40
ANOM_CVD_BATCH = 6               # 每輪最多對幾個已觸發的幣打 CVD（逐幣翻頁，很貴）
_ANOM_CVD = {"n": 0}


def _anom_scan(now_s: float) -> None:
    """★複刻「異常警報」的觸發 + 事件狀態機（規格 trading-backtest/_DHX_ALERT_0924_SPEC.md）。

    官方觸發器與門檻（1000 筆實測）：`price_15m` ≥3.00%（700筆）、`price_5m` ≥3.00%（192筆）、
    `oi_cross`（81筆，定義沒抓到）。★統計還顯示：純價格觸發有 **73~78% 停在「異動觀察」**，
    只有 `oi_cross` 幾乎都會定出方向（4% 停在觀察）—— 所以價格警報本來就多半只是觀察。

    狀態機（官方 `event_lifecycle_status` / `bias_label`）：
      RADAR(異動觀察) → CONFIRMED(偏多/偏空確認) → WEAKENING(轉弱) / INVALIDATED(失效)
    ★官方用 **CVD 當方向主軸**（偏多確認 CVD +4.83、偏空確認 −8.24、觀察中 ≈0），
      我全市場沒有 CVD → **方向判定用 OI 保留 + 相對 BTC 強弱**（官方那句話的另外兩項），
      並在畫面標明「沒有 CVD，方向判定與官方不同」，不假裝一致。

    資料全部來自既有的 5 分鐘取樣（`_PX_HISTORY` / `_oi_history`），零額外 API。
    """
    try:
        px_all = _PX_HISTORY
        if not px_all:
            return
        # BTC 當基準（官方「相對 BTC 強弱」）
        def _chg(hist, secs):
            if not hist or len(hist) < 2:
                return None
            base = None
            tgt = now_s - secs
            prev = None
            for t, v in hist:
                if t <= tgt:
                    prev = (t, v)
                elif prev is not None:
                    f = (tgt - prev[0]) / (t - prev[0])
                    base = prev[1] + (v - prev[1]) * f
                    break
                else:
                    return None
            if base is None or base <= 0:
                return None
            return (hist[-1][1] - base) / base * 100.0

        btc15 = _chg(px_all.get("BTC-USDT-SWAP"), 900) or 0.0
        _ANOM_CVD["n"] = 0        # 每輪重置 CVD 取用額度
        for inst, ph in list(px_all.items()):
            coin = inst.replace("-USDT-SWAP", "")
            p15 = _chg(ph, 900)
            p5 = _chg(ph, 300)
            if p15 is None:
                continue
            trig = None
            if abs(p15) >= ANOM_PX_TH:
                trig = "price_15m"
            elif p5 is not None and abs(p5) >= ANOM_PX_TH:
                trig = "price_5m"
            ev = _ANOM.get(coin)
            if trig and not ev:
                _ANOM[coin] = ev = {
                    "coin": coin, "inst": inst, "trigger_type": trig,
                    "first_ts": now_s, "last_ts": now_s, "trigger_count": 1,
                    "trigger_price": ph[-1][1], "init_dir": "bull" if p15 > 0 else "bear",
                    "status": "RADAR", "bias_label": "異動觀察",
                    "confirmed_at": None, "confirmed_price": None, "confirmed_dir": "",
                    "mfe_pct": 0.0, "giveback_pct": 0.0,
                }
            elif trig and ev:
                ev["trigger_count"] += 1
                ev["last_ts"] = now_s
                ev["trigger_type"] = trig
            if not ev:
                continue
            # ── 狀態機：用 OI 保留 + 相對 BTC 強弱（官方第三項 CVD 我沒有）
            last = ph[-1][1]
            oi15 = None
            oh = _oi_history.get(inst)
            if oh:
                oi15 = _chg(oh, 900)
            rel = (p15 or 0.0) - btc15          # 相對 BTC 強弱
            up = ev["init_dir"] == "bull"
            oi_hold = (oi15 is not None and oi15 > 0)
            strong = (rel > 0) if up else (rel < 0)
            # ★官方原話是三項：「以 **OI 保留**、**相對 BTC 強弱** 與 **CVD** 判斷方向」。
            #   前兩項用取樣資料就有；CVD 要逐幣打 rubik，所以只對**已觸發**的幣打
            #   （一輪最多 ANOM_CVD_BATCH 個），這正是全市場拿不到、但警報數量少就負擔得起的做法。
            #   官方統計：偏多確認 CVD 中位 +4.83、偏空確認 −8.24、異動觀察 ≈0 → CVD 是方向主軸。
            if ev.get("cvd_dir") is None and _ANOM_CVD["n"] < ANOM_CVD_BATCH:
                _ANOM_CVD["n"] += 1
                try:
                    _df = fetch_market_candles(inst, "15m")
                    _cv = _okx_contract_cvd_15m(inst, _df.index) if not _df.empty else None
                    if _cv is not None and len(_cv) >= 8:
                        _v = _cv.values.astype(float)
                        _d = float(_v[-1] - _v[-5])        # 近 5 根(≈1h)的 CVD 變化
                        ev["cvd_delta"] = _d
                        ev["cvd_dir"] = 1 if _d > 0 else (-1 if _d < 0 else 0)
                except Exception:
                    ev["cvd_dir"] = 0                       # 拿不到就當方向不明，不亂猜
            cvd_ok = (ev.get("cvd_dir") is not None and
                      (ev["cvd_dir"] > 0 if up else ev["cvd_dir"] < 0))
            if ev["status"] == "RADAR":
                if oi_hold and strong and cvd_ok and (now_s - ev["first_ts"]) >= 900:
                    ev["status"] = "CONFIRMED"
                    ev["bias_label"] = "偏多確認" if up else "偏空確認"
                    ev["confirmed_at"] = now_s
                    ev["confirmed_price"] = last
                    ev["confirmed_dir"] = "bull" if up else "bear"
            elif ev["status"] == "CONFIRMED":
                ref = ev["confirmed_price"] or last
                move = (last - ref) / ref * 100.0 * (1 if up else -1)
                ev["mfe_pct"] = max(ev.get("mfe_pct") or 0.0, move)
                ev["giveback_pct"] = round((ev["mfe_pct"] - move), 3)
                if move <= -2.0:
                    ev["status"] = "INVALIDATED"
                    ev["bias_label"] = "偏多失效" if up else "偏空失效"
                elif (ev.get("cvd_dir") is not None and not cvd_ok) or not oi_hold or not strong:
                    # ★官方統計：OI 還正、但 CVD 翻到反向 → 降級「轉弱」（偏多轉弱 CVD 中位 −1.80）
                    ev["status"] = "WEAKENING"
                    ev["bias_label"] = "偏多轉弱" if up else "偏空轉弱"
            ev["p15"] = round(p15, 3) if p15 is not None else None
            ev["p5"] = round(p5, 3) if p5 is not None else None
            ev["oi15"] = round(oi15, 3) if oi15 is not None else None
            ev["rel_btc"] = round(rel, 3)
            ev["last_price"] = last
        # 過期清理
        for c in list(_ANOM.keys()):
            if now_s - _ANOM[c]["last_ts"] > ANOM_VALID_H * 3600:
                del _ANOM[c]
        if len(_ANOM) > ANOM_MAX:
            for c in sorted(_ANOM, key=lambda x: _ANOM[x]["last_ts"])[:len(_ANOM) - ANOM_MAX]:
                del _ANOM[c]
    except Exception as e:
        print(f"[ANOM] 異常警報掃描失敗(不影響交易): {e}", flush=True)


def _oi_sample_tick(force: bool = False) -> bool:
    """★儀表板的 OI／價格取樣（**_oi_history / _TICKER_SNAP / _PX_HISTORY 的唯一寫入點**）。
    兩支公開端點，各一次涵蓋全市場約 400 個合約：
      public/open-interest → _oi_history        market/tickers → _TICKER_SNAP + _PX_HISTORY
    5 分鐘一次 = 288 次/天，跟現役 K 線查詢比可忽略。失敗一律吞掉，不影響交易。
    ★節拍器是 `_dash_sampler_loop`（獨立執行緒），而且**只有它一個呼叫者**
      （啟動那次是一次性的，且在起執行緒之前就跑完）——這裡是三個歷史 dict 的
      唯一寫入點，不可以有兩個節拍器同時進來寫出重複時間戳。"""
    global _TICKER_SNAP
    if not force and time.time() - _DASH_SAMPLE["ts"] < DASH_SAMPLE_SEC:
        return False
    _DASH_SAMPLE["ts"] = time.time()
    now_s = time.time()
    # ★只留加密貨幣：不過濾的話美股代幣(TSEM/SQQQ/GTLB/NMR…)會混進 OI 排名 ——
    #   2026-09-24 與官方同時刻對帳時抓到，官方清單裡一個都沒有（他們有獨立的美股分頁）。
    #   同型事故 memory 記過一次（SNXX instCategory=3 經 OI 增長榜進池、吞噬空真的開了空單）。
    #   另排除穩定幣（官方 `_whaleRadarIsEligibleCoin` 也有這張清單）。
    try:
        _crypto_ok = _okx_crypto_symbols()
    except Exception:
        _crypto_ok = set()
    _STABLE_EX = {"USDT", "USDC", "DAI", "BUSD", "FDUSD", "TUSD", "USDS", "USDE", "USDD",
                  "USDY", "USDG", "USDF", "USDTB", "RLUSD", "PYUSD", "BFUSD", "USDB",
                  "FRAX", "LUSD", "SUSD", "GUSD", "USD0"}

    def _dash_ok(inst_id):
        coin = inst_id.replace("-USDT-SWAP", "")
        if coin in _STABLE_EX:
            return False
        return (not _crypto_ok) or (coin in _crypto_ok)   # 抓不到清單就不過濾（與其他來源一致）
    keep_from = now_s - (OI_MOVERS_WINDOW_H + 1) * 3600      # 留到比最大窗多 1 小時就夠
    # ① OI
    try:
        r = requests.get("https://www.okx.com/api/v5/public/open-interest",
                         params={"instType": "SWAP"}, timeout=15)
        if r.status_code == 200:
            for row in r.json().get("data", []):
                inst = row.get("instId", "")
                if not inst.endswith("-USDT-SWAP") or not _dash_ok(inst):
                    continue
                try:
                    oi_usd = float(row.get("oiUsd", 0) or 0)
                except (TypeError, ValueError):
                    continue
                if oi_usd <= 0:
                    continue
                h = _oi_history.setdefault(inst, [])
                h.append((now_s, oi_usd))
                _oi_history[inst] = [(t, v) for (t, v) in h if t >= keep_from] or [(now_s, oi_usd)]
    except Exception as e:
        print(f"[DASH] OI 取樣失敗: {e}", flush=True)
    # ② 價格（存在流動性門檻之前，才能跟 _oi_history 全市場對得起來）
    try:
        r = requests.get("https://www.okx.com/api/v5/market/tickers",
                         params={"instType": "SWAP"}, timeout=15)
        if r.status_code == 200:
            snap = {}
            for t in r.json().get("data", []):
                inst = t.get("instId", "")
                if not inst.endswith("-USDT-SWAP") or not _dash_ok(inst):
                    continue
                try:
                    last = float(t["last"]); op = float(t["open24h"]); vc = float(t.get("volCcy24h", 0) or 0)
                except (KeyError, TypeError, ValueError):
                    continue
                if last <= 0 or op <= 0:
                    continue
                snap[inst] = {"last": last, "chg24h": (last - op) / op,
                              "volccy_usd": vc * last, "ts": now_s}
                h = _PX_HISTORY.setdefault(inst, [])
                h.append((now_s, last))
                _PX_HISTORY[inst] = [(_t, _v) for (_t, _v) in h if _t >= keep_from] or [(now_s, last)]
            if snap:
                _TICKER_SNAP = snap                       # 整批換掉，下架幣自然消失
                for k in list(_PX_HISTORY.keys()):
                    if k not in snap:
                        del _PX_HISTORY[k]
    except Exception as e:
        print(f"[DASH] tickers 取樣失敗: {e}", flush=True)
    # ★★2026-09-24 改成背景執行緒：幣安沒被擋之前這裡 451 一次就 return，成本≈0；
    #   改走 www.binance.com 之後會真的逐幣打 60 支（幣安沒有全市場 OI 的批量端點），
    #   同步跑等於每 5 分鐘把**交易主迴圈**卡住十幾秒 —— 掃描時機不能被儀表板拖。
    #   `_BN_STATE["busy"]` 保證同時只有一個在跑（上一輪沒跑完就跳過這輪，不堆執行緒）。
    if not _BN_STATE.get("busy"):
        def _bn_bg(_ns=now_s, _kf=keep_from):
            _t0 = time.time()
            try:
                _bn_oi_sample(_ns, _kf)
                _mcap_refresh(_ns)      # 順便刷市值（自己有 6 小時節流）
            finally:
                _BN_STATE["ms"] = int((time.time() - _t0) * 1000)
                _BN_STATE["busy"] = False
        _BN_STATE["busy"] = True
        Thread(target=_bn_bg, daemon=True).start()
    try:
        _whale_scan(now_s)              # 巨鯨雷達：資金注入候選 + 15 分鐘觀察狀態機
    except Exception as _we:
        print(f"[WHALE] 例外(不影響交易): {_we}", flush=True)
    try:
        _anom_scan(now_s)               # 異常警報（觸發+狀態機，零額外 API）
    except Exception as _ae:
        print(f"[ANOM] 例外(不影響交易): {_ae}", flush=True)
    try:
        _dhx_scan()                     # 數據訊號(TRAP/ABSORPTION/EXHAUSTION)
    except Exception as _de:
        print(f"[DHX] 掃描例外(不影響交易): {_de}", flush=True)
    _DASH_SAMPLE["n"] = len(_TICKER_SNAP)
    _DASH_SAMPLE["i"] = int(_DASH_SAMPLE.get("i", 0)) + 1
    if force or _DASH_SAMPLE["i"] % DASH_SAVE_EVERY == 0:
        _dash_hist_save()      # 每輪落地,redeploy 只損失開機那一小段
    return True


def _fetch_okx_top_movers(top_n: int = TOP_MOVERS_N, min_volccy: float = MIN_MOVER_VOLCCY) -> list:
    """OKX 24h 漲幅前N + 跌幅前N(USDT永續,配流動性門檻;★2026-09-04只留加密貨幣)。
    漲幅榜→動量/突破多單廣度;跌幅榜→breakdown空/box 候選。回傳 inst_id 列表。"""
    try:
        # ★2026-09-04 修 NameError:原本 _crypto_mv 被寫在 _fetch_okx_liquid_pool 裡(局部變數),
        #   這裡引用它 → 執行期 `name '_crypto_mv' is not defined`,漲跌幅榜整個掃描來源從 d2c169a 起是死的。
        #   `ast.parse` 和 _chk_names.py 都抓不到(跨函數作用域,不是「沒定義過這個名字」)。
        _crypto_mv = _okx_crypto_symbols()
        r = requests.get("https://www.okx.com/api/v5/market/tickers",
                         params={"instType": "SWAP"}, timeout=15)
        if r.status_code != 200:
            print(f"[SYMBOLS] OKX tickers HTTP {r.status_code}", flush=True); return []
        movers = []
        for t in r.json().get("data", []):
            inst = t.get("instId", "")
            if not inst.endswith("-USDT-SWAP"):
                continue
            if _crypto_mv and inst.replace("-USDT-SWAP", "") not in _crypto_mv:
                continue                      # ★2026-09-04 只留加密貨幣(instCategory=1)
            try:
                last = float(t["last"]); op = float(t["open24h"]); vc = float(t.get("volCcy24h", 0) or 0)
            except (KeyError, ValueError, TypeError):
                continue
            if op <= 0 or vc * last < min_volccy:  # ★2026-09-04 修:volCcy24h是幣數不是USDT,要乘價
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

# ★2026-07-07:OI異常增長榜(用戶指出:主力安靜建倉時價格不大動,不會進漲跌幅榜,
#   OI壓縮突破策略要抓的正是這種「還沒噴」的幣→漲跌幅榜結構性抓不到,需要獨立用OI變化篩選)
OI_MOVERS_N = 20                # OI增幅前N名加入掃描
OI_MOVERS_WINDOW_H = 12         # 對齊_check_oi_squeeze的12h壓縮窗
_oi_history: Dict[str, list] = {}   # instId -> [(ts, oiUsd), ...] 只留約OI_MOVERS_WINDOW_H+1小時,記憶體內即可(不需存檔)

OI_BOOT_MIN_USD  = 1_000_000.0  # 只補 oiUsd≥此值的合約(實測 460→216支);與底池流動性門檻同級
OI_BOOT_SLEEP    = 0.25         # 每支之間節流(實測單支約0.32s → 合計約1.7次/秒,不去撞 rubik 限流)
OI_BOOT_MAX_MISS = 10           # 累計失敗達此數就整個放棄(寧可退回逐小時累積,也不要害 ls/taker 熔斷)
_OI_BOOT = {"started": False, "done": False, "n": 0, "ts": 0.0}

def _oi_bootstrap_history(window_h: int = OI_MOVERS_WINDOW_H) -> None:
    """★2026-09-10:啟動時把 _oi_history 一次補回 window_h 小時,治「每次 redeploy 都啞12小時」。
    病因:_oi_history 是純記憶體,而餵它的 refresh_top_movers_only 每小時才跑一次
      → 要 12 個點(12小時)才算得出增幅 → 每推一次 code,OI增長榜就死12小時。
      (寫檔救不了:`railway volume list` = No volumes,redeploy 就是全新容器。)
    解法:OKX rubik `/rubik/stat/contracts/open-interest-history`(instId版,period=1H)
      實測回 100 筆 = 99 小時,遠超需要的 12h。
    ★單位對帳(實測,不是推測):該端點欄位 [3] 就是 oiUsd,與 /public/open-interest 的 oiUsd
      同單位同量級(BTC 2,118,019,488 vs 2,117,618,198,差 0.02% 只是幾秒時間差)
      → 可以直接填進 _oi_history,不需換算。
    ★在背景 daemon thread 跑(實測 0.32 秒/支 × 約460支 ≈ 150秒),完全不擋主流程與掃描。
    """
    if _OI_BOOT["started"]: return
    _OI_BOOT["started"] = True
    def _run():
        t0 = time.time(); filled = 0; miss = 0
        # ★★用戶指正(2026-09-10):「你之前就有停打導致都沒訊號過」——
        #   `_fetch_ls_taker` 的 rubik 熔斷(連續失敗8次→停打30分)害過 DH空/維加斯/逆勢多整段噤聲。
        #   所以這裡監看的是**受害者的狀態**,不是我自己的失敗數:
        #   ①開跑前:ls/taker 已在熔斷 → 直接不跑,別火上加油
        #   ②跑的過程:只要 ls/taker 的失敗計數開始上升 → 立刻中止,把 rubik 額度讓回去
        if time.time() < _LS_FAIL.get("skip_until", 0):
            print("[OI-Boot] ls/taker 正在熔斷中,本次不補歷史(避免加重限流)", flush=True)
            _OI_BOOT["done"] = True; return
        _ls_base = _LS_FAIL.get("streak", 0)
        try:
            r = requests.get("https://www.okx.com/api/v5/public/open-interest",
                             params={"instType": "SWAP"}, timeout=15)
            # ★只補 oiUsd≥1M 的(實測 460支→216支,69秒)。理由有二:
            #   ①OI 低於 1M 的幣本來就不該進掃描池(VLONG 教訓:CIEN 43K/根落在回測第0.2百分位)
            #   ②少打一半 API = 少一半撞 OKX rubik 限流的機會(見下方節流說明)
            insts = [x.get("instId", "") for x in r.json().get("data", [])
                     if x.get("instId", "").endswith("-USDT-SWAP")
                     and float(x.get("oiUsd") or 0) >= OI_BOOT_MIN_USD]
        except Exception as e:
            print(f"[OI-Boot] 取合約列表失敗,放棄補歷史(退回逐小時累積): {e}", flush=True)
            _OI_BOOT["done"] = True; return
        cutoff_ms = (time.time() - (window_h + 1) * 3600) * 1000
        for inst in insts:
            # ★受害者監看:ls/taker 一開始失敗就讓路(它的訊號比 OI 增長榜重要得多)
            if (_LS_FAIL.get("streak", 0) > _ls_base + 1
                    or time.time() < _LS_FAIL.get("skip_until", 0)):
                print(f"[OI-Boot] ⚠️ 偵測到 ls/taker 開始失敗(streak={_LS_FAIL.get('streak')}),"
                      f"立刻中止補歷史把 rubik 額度讓回去(已填 {filled} 幣)", flush=True)
                break
            try:
                d = _fetch_okx_public_data("/api/v5/rubik/stat/contracts/open-interest-history",
                                           {"instId": inst, "period": "1H"})
                # ★★節流:_fetch_ls_taker 有自己的熔斷(rubik 連續失敗8次→停打30分,
                #   DH空/維加斯/逆勢多全部噤聲)。本函數若把 IP 打到限流就會連累它,
                #   所以刻意壓到約 1.7 次/秒,而且失敗時退讓更久、連續失敗就整個放棄。
                if not d:
                    miss += 1
                    if miss >= OI_BOOT_MAX_MISS:
                        print(f"[OI-Boot] ⚠️ 連續/累計失敗 {miss} 次(可能限流),放棄補歷史保護 ls/taker",
                              flush=True)
                        break
                    time.sleep(1.5); continue
                miss = 0
                pts = []
                for row in d:                       # [ts, oi, oiCcy, oiUsd],新→舊
                    try:
                        ts_ms = float(row[0]); v = float(row[3])
                    except (ValueError, TypeError, IndexError):
                        continue
                    if ts_ms < cutoff_ms or v <= 0: continue
                    pts.append((ts_ms / 1000.0, v))
                if len(pts) >= 2:
                    pts.sort(key=lambda x: x[0])    # 轉成舊→新(與逐小時 append 的順序一致)
                    _oi_history[inst] = pts
                    filled += 1
                time.sleep(OI_BOOT_SLEEP)
            except Exception:
                time.sleep(OI_BOOT_SLEEP); continue
        _OI_BOOT.update(done=True, n=filled, ts=time.time())
        print(f"[OI-Boot] ✅ 已補回 {filled}/{len(insts)} 幣的 {window_h}h OI 歷史,"
              f"耗時 {time.time()-t0:.0f}s → OI增長榜不必再等12小時", flush=True)
    Thread(target=_run, name="oi-bootstrap", daemon=True).start()
    print(f"[OI-Boot] 背景補 {window_h}h OI 歷史中(約150秒,不擋掃描)...", flush=True)


def _fetch_okx_oi_movers(top_n: int = OI_MOVERS_N, window_h: int = OI_MOVERS_WINDOW_H) -> list:
    """OKX全市場USDT永續OI批量查詢(一次API涵蓋~400個合約,跟漲跌幅榜同等級便宜),
    用內建歷史(_oi_history)算過去window_h小時OI%增幅,回傳增幅最大的top_n個inst_id。
    第一次呼叫(歷史不足window_h)回傳[](還沒有基準點可比,下一輪才有資料)。"""
    global _oi_history
    _oi_bootstrap_history(window_h)   # ★內部有旗標,只會真的跑一次;背景補歷史不擋這裡
    try:
        r = requests.get("https://www.okx.com/api/v5/public/open-interest",
                         params={"instType": "SWAP"}, timeout=15)
        if r.status_code != 200:
            print(f"[SYMBOLS] OKX OI榜 HTTP {r.status_code}", flush=True); return []
        now = time.time()
        cutoff = now - window_h * 3600
        seen = set()
        for row in r.json().get("data", []):
            inst = row.get("instId", "")
            if not inst.endswith("-USDT-SWAP"):
                continue
            try:
                oi_usd = float(row.get("oiUsd", 0) or 0)
            except (ValueError, TypeError):
                continue
            if oi_usd <= 0:
                continue
            seen.add(inst)
            hist = _oi_history.setdefault(inst, [])
            hist.append((now, oi_usd))
            _oi_history[inst] = [(t, v) for (t, v) in hist if t >= cutoff - 3600] or [(now, oi_usd)]
        # 清掉本輪沒回傳的舊inst(下架/資料異常),避免_oi_history無限增長
        for inst in list(_oi_history.keys()):
            if inst not in seen:
                del _oi_history[inst]
        gains = []
        for inst, hist in _oi_history.items():
            if len(hist) < 2:
                continue
            oldest_t, oldest_v = hist[0]
            _, latest_v = hist[-1]
            if oldest_t > cutoff + 3600 or oldest_v <= 0:   # 歷史還不夠window_h小時,基準點太新不可信
                continue
            pct = (latest_v - oldest_v) / oldest_v
            gains.append((inst, pct))
        if not gains:
            _bs = ("補歷史尚未跑完(背景約150秒)" if not _OI_BOOT["done"]
                   else f"補歷史已完成但只填到{_OI_BOOT['n']}幣,仍在逐小時累積")
            print(f"[SYMBOLS] OI榜:無結果 —— {_bs}(需{window_h}h,追蹤{len(_oi_history)}幣)", flush=True)
            return []
        # ★2026-09-11 補「只留加密貨幣」濾網(底池/漲跌幅榜 09-04 就有,這個來源漏了)。
        #   實際事故:SNXX(instCategory=3 股票代幣)經 OI增長榜進池,吞噬空在 09-11 14:09 真的開了空單。
        #   以前每次 redeploy OI榜要空轉12h才有結果所以少見;5039d43 啟動補歷史後立刻生效,漏洞被放大。
        #   抓失敗回空集合 = 不過濾(與另外兩個來源一致)。
        _crypto_oi = _okx_crypto_symbols()
        if _crypto_oi:
            _n0 = len(gains)
            gains = [g for g in gains if g[0].replace("-USDT-SWAP", "") in _crypto_oi]
            if len(gains) < _n0:
                print(f"[SYMBOLS] OI增長榜:排除 {_n0 - len(gains)} 支非加密(股票/商品)", flush=True)
        gains.sort(key=lambda x: x[1], reverse=True)
        top = [g[0] for g in gains[:top_n]]
        print(f"[SYMBOLS] OKX OI增長榜:前{len(top)}名(window={window_h}h,追蹤{len(_oi_history)}幣)", flush=True)
        return top
    except Exception as e:
        print(f"[SYMBOLS] OKX OI榜抓取失敗: {e}", flush=True)
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
            _cf = _SYMBOLS_CACHE_FILE if os.path.exists(_SYMBOLS_CACHE_FILE) else _SYMBOLS_CACHE_REPO
            if os.path.exists(_cf):
                cached = json.load(open(_cf, encoding="utf-8"))
                SYMBOLS = cached["symbols"]
                OKX_SWAP = {v: k for k, v in SYMBOLS.items()}
                _symbols_last_updated = cached.get("updated", 1.0)
                print(f"[SYMBOLS] 快取載入：{len(SYMBOLS)} 個幣種({_cf})", flush=True)
        except Exception as e:
            print(f"[SYMBOLS] 快取讀取失敗: {e}", flush=True)

    print("[SYMBOLS] 向 CoinGecko 抓取市值前100...", flush=True)
    top100 = _fetch_coingecko_top100()
    _cg_failed = not top100
    if _cg_failed:
        # ★2026-09-15 原本直接 return False「維持現有列表」→ 啟動時現有列表=舊快取 47 幣,整天卡住。
        #   改成照樣用 OKX 自己的流動性底池 + 漲跌幅榜組池(底池本身 160+ 幣,已涵蓋大部分市值前100),1 小時後再試 CoinGecko。
        print("[SYMBOLS] ⚠️ CoinGecko 失敗 → 改用 OKX 流動性底池+漲跌幅榜組池,1小時後重試 CoinGecko", flush=True)
        top100 = []

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

    # ★加流動性底池(2026-08-27):OKX全市場 24h成交額>=10M 的永續(見 _fetch_okx_liquid_pool 註解的回測依據)
    _n_liq = len(new_symbols)
    for inst_id in _fetch_okx_liquid_pool():
        if inst_id in okx_swaps and inst_id not in new_symbols:
            coin = inst_id.split("-")[0]
            if coin not in STABLECOINS:
                new_symbols[inst_id] = f"{coin}/USDT"
    if len(new_symbols) > _n_liq:
        print(f"[SYMBOLS] 流動性底池額外加入 {len(new_symbols)-_n_liq} 幣 → 共 {len(new_symbols)}", flush=True)
    global _top100_base_symbols
    _top100_base_symbols = dict(new_symbols)   # ★底池改成「市值前100 + 流動性池」,供輕量刷新整批重組用

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
    _symbols_last_updated = time.time() - (86400 - 3600 if _cg_failed else 0)   # CoinGecko 失敗 → 1 小時後重試全量

    # 儲存快取
    try:
        json.dump({"updated": _symbols_last_updated, "symbols": SYMBOLS},
                  open(_SYMBOLS_CACHE_FILE, "w", encoding="utf-8"), indent=2, ensure_ascii=False)
    except Exception as e:
        print(f"[SYMBOLS] 快取寫入失敗: {e}", flush=True)

    msg = f"🔄 幣種列表已動態更新：市值前100 × OKX 永續，共 **{len(SYMBOLS)}** 個"
    print(f"[SYMBOLS] ✅ {msg}", flush=True)
    dc_log(msg)
    global _movers_last_updated
    _movers_last_updated = time.time()   # 全量更新已含最新漲跌幅榜,重置獨立計時避免立刻又觸發輕量刷新
    return True

def refresh_top_movers_only() -> bool:
    """★2026-07-07:漲跌幅榜+OI增長榜 輕量獨立刷新(只打OKX輕量API,不碰CoinGecko/OKX合約列表)。
    每次都是「市值前100底池(_top100_base_symbols) + 本輪漲跌幅榜 + 本輪OI增長榜」整批重組,
    不是只增不減——否則清單會逐小時膨脹,scan負擔跟著漲(用戶要求railway用量別爆炸)。
    目的:
    ①漲跌幅是24h滾動即時排名,原本綁在24h全量更新週期太慢——幣中途暴衝完就落幕,整段時間都不在掃描清單裡。
    ②★用戶指出:主力安靜建倉時價格不大動,結構性不會進漲跌幅榜——OI壓縮突破要抓的正是這種「還沒噴」的幣,
      漲跌幅榜這個篩選機制天生抓不到,需要獨立用OI增幅篩(_fetch_okx_oi_movers)補上這塊掃描盲區。"""
    global SYMBOLS, OKX_SWAP, _movers_last_updated
    if not _top100_base_symbols:
        return False   # 還沒跑過一次全量更新,沒有底池可組,交給下次全量更新處理
    try:
        okx_swaps = _fetch_okx_swap_set()
        if not okx_swaps:
            return False   # 保守:抓不到合約列表就沿用舊列表,不動SYMBOLS
        STABLECOINS = {"USDT", "USDC", "BUSD", "DAI", "TUSD", "FDUSD", "USDD", "USDP"}
        merged: Dict[str, str] = dict(_top100_base_symbols)   # 底池固定不變
        n_base = len(merged)
        n_price_added = 0; n_oi_added = 0
        for inst_id in _fetch_okx_top_movers():
            if inst_id in okx_swaps and inst_id not in merged:
                coin = inst_id.split("-")[0]
                if coin not in STABLECOINS:
                    merged[inst_id] = f"{coin}/USDT"; n_price_added += 1
        for inst_id in _fetch_okx_oi_movers():
            if inst_id in okx_swaps and inst_id not in merged:
                coin = inst_id.split("-")[0]
                if coin not in STABLECOINS:
                    merged[inst_id] = f"{coin}/USDT"; n_oi_added += 1
        SYMBOLS = merged
        OKX_SWAP = {v: k for k, v in SYMBOLS.items()}
        _movers_last_updated = time.time()
        print(f"[SYMBOLS] 輕量刷新:底池{n_base}+漲跌幅{n_price_added}+OI增長{n_oi_added},共{len(SYMBOLS)}個(整批重組,非累加)", flush=True)
        return True
    except Exception as e:
        print(f"[SYMBOLS] 輕量刷新失敗(沿用舊列表): {e}", flush=True)
        return False

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


def _infer_adopted_exit(ex, sym, side, entry, sl_trig, ct):
    """★2026-09-14 接管倉出場推斷:用交易所上仍掛著的 TP 限價單(reduceOnly)推回原策略出場規格。
    回傳 (exit_strategy, tp1_order_id, remaining_amount, 理由) 或 None(查單失敗→呼叫端退回舊行為)。
    規則(對應 execute_okx_trade_pipeline 各分支掛 TP 的方式):
      沒有 TP 單                          → swing_full(讓跑類本來就不掛 TP)
      停損已在獲利側(保本/移SL過)         → adopt_hold(原始風險距不可知,不碰)
      兩張 TP 同價、R≈1(做空)             → bor_1r(BOR 固定1R;保留熔斷計數)
      兩張 TP 同價、R≈2.5                 → s4h_fixed(S4H 固定2.5R)
      兩張 TP 不同價                       → ""(傳統固定R:TP1 成交後保本)
      一張 TP 全倉、R≈2(做空)             → fourjd_2r(4JD 2R + 0.8R 保本)
      一張 TP 全倉、R≈4                   → box_trend
      一張 TP 半倉                         → swing_tp(TP1 半倉 + 剩半轉折移SL)
      其餘                                 → adopt_hold"""
    close_side = "buy" if side == "short" else "sell"
    oo = ex.fetch_open_orders(sym)
    tps = []
    for o in oo or []:
        info = o.get("info") or {}
        if str(info.get("reduceOnly")).lower() != "true" and not o.get("reduceOnly"): continue
        if o.get("side") != close_side: continue
        if info.get("posSide") and info.get("posSide") != side: continue
        if (o.get("type") or info.get("ordType")) not in ("limit", "post_only"): continue
        px = float(o.get("price") or info.get("px") or 0); amt = float(o.get("remaining") or o.get("amount") or info.get("sz") or 0)
        if px > 0 and amt > 0: tps.append((px, amt, o.get("id")))
    if not tps:
        return ("swing_full", None, str(ct), "交易所無TP單")
    loss_side_ok = (sl_trig > entry) if side == "short" else (sl_trig < entry)
    if not loss_side_ok:
        return ("adopt_hold", None, str(ct), "停損已在獲利側,原始風險不可知")
    risk0 = abs(entry - sl_trig)
    tps.sort(key=lambda t: abs(t[0] - entry))
    Rs = [abs(t[0] - entry) / risk0 for t in tps]
    tot = sum(t[1] for t in tps)
    same_px = (max(t[0] for t in tps) - min(t[0] for t in tps)) / entry <= 0.003
    # ★R 用「成交均價」算,但 TP 是用「訊號K收盤」算的 → 滑價會把 R 推偏(實測 BOR 的 BICO 1.35、CC 0.78),
    #   區間要放寬;各類別區間互不重疊。目前開著的「兩張同價TP」策略只有 BOR(1R)/S4H(2.5R)/CME缺口(2R,限BTC/ETH/SOL)。
    _is_cme = (sym.split(":")[0] in globals().get("CME_GAP_COINS", []))
    if len(tps) >= 2 and same_px:
        R = Rs[0]
        if _is_cme and 1.6 <= R <= 2.4:
            return ("cme_gap", tps[0][2], str(ct), f"CME幣兩張TP同價 R={R:.2f}")
        if side == "short" and 0.6 <= R <= 1.5:
            return ("bor_1r", tps[0][2], str(ct), f"兩張TP同價 R={R:.2f}")
        if side == "short" and 2.0 <= R <= 3.0:
            return ("s4h_fixed", tps[0][2], str(ct), f"兩張TP同價 R={R:.2f}")
        return ("adopt_hold", None, str(ct), f"兩張TP同價但 R={R:.2f} 對不上已知策略")
    if len(tps) >= 2:
        return ("", tps[0][2], str(round(ct - tps[0][1], 8)), f"兩張TP不同價 R={Rs[0]:.2f}/{Rs[-1]:.2f}")
    R = Rs[0]; frac = tot / ct if ct else 0
    if frac >= 0.9:
        if side == "short" and 1.6 <= R <= 2.5:
            return ("fourjd_2r", tps[0][2], str(ct), f"單張全倉TP R={R:.2f}")
        if _is_cme and side == "long" and 1.6 <= R <= 2.4:     # CME缺口倉太小拆不了半→單張全倉TP
            return ("cme_gap", tps[0][2], str(ct), f"CME幣單張全倉TP R={R:.2f}")
        if 3.5 <= R <= 4.5:
            return ("box_trend", tps[0][2], str(ct), f"單張全倉TP R={R:.2f}")
        return ("adopt_hold", None, str(ct), f"單張全倉TP R={R:.2f} 對不上已知策略")
    if 0.3 <= frac <= 0.7:
        return ("swing_tp", tps[0][2], str(round(ct - tps[0][1], 8)), f"單張半倉TP R={R:.2f}")
    return ("adopt_hold", None, str(ct), f"TP量比例{frac:.2f}對不上已知策略")


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
            entry=float(p.get("entryPrice") or 0) or float((p.get("info") or {}).get("avgPx") or 0)
            if entry<=0: continue
            inst_id=(p.get("info") or {}).get("instId") or OKX_SWAP.get(sym, sym)
            # ★★2026-09-04 修(跨系統識別碼格式不統一,第二現場):
            #   bot 自己開倉寫進追蹤池的是 **instId**("ARB-USDT-SWAP", 見 1350 行 "symbol": symbol_id),
            #   但 ccxt fetch_positions() 回的是**統一符號**("ARB/USDT:USDT") → 舊寫法 (sym,side) in tracked
            #   對 bot 自家倉**恆為 False** → 每次啟動都把自家倉當外來倉「接管」一次。
            #   後果不只是重複訊息:接管會把 exit_strategy **強制覆寫成 swing_full**(見下方 inferred_es),
            #   原本設計的固定R + TP1 1.2R出半倉 + TP2 2.5R **被靜默改成整倉讓跑**,
            #   且 ts_open 重新計時(時間停損歸零)、pyramid_eligible 被關掉。
            #   證據:2026-09-04 06:04/06:06/06:11 三次重啟,ARB/EDGE/APR/KMNO 每次都被重新接管
            #   (同期手動倉 ONDO/KITE 被 broker-tag 防呆正確擋下,故此 bug 只影響 bot 自己的倉)。
            #   同族教訓見 CLAUDE.md「跨系統識別碼要先統一格式」(2026-08-27 追蹤池100%誤刪事故)。
            if (sym,side) in tracked or (inst_id,side) in tracked: continue
            # ★2026-07-19 手動倉不接管(用戶LINK案例:手動8.4張多單被接管→移動停利貼太近被插針掃出)。
            #   判定:近期該inst同方向「開倉單」(非reduceOnly)是否帶ccxt broker tag(6b9ad766b55dBCDE)=bot下的。
            #   全無bot tag=手動倉→只通知、絕不接管。判定失敗也保守不接管(寧漏勿越權)。
            # ★★2026-08-03 修正致命邏輯錯誤(用戶WLD手動多單被接管並時停平掉的根因):
            #   舊版問「這個幣**歷史上**bot有沒有開過同方向倉」,archive端點回3個月資料→bot六七月做過WLD多
            #   →命中→把用戶8/2手動開的WLD多當成bot倉接管。**正確問法是「**這一筆倉**是不是bot開的」**。
            #   修法:用倉位建立時間(info.cTime)當錨,只採計「本倉建立之後」的開倉單;並改用7天端點(orders-history)
            #   為主(涵蓋當前倉的生命週期且不含陳年舊單),archive僅在7天端點無資料時備援。
            _pos_ct = 0
            try: _pos_ct = int((p.get("info") or {}).get("cTime") or 0)
            except Exception: _pos_ct = 0
            try:
                _oh = ex.private_get_trade_orders_history({"instType": "SWAP", "instId": inst_id, "limit": "100"})
                _rows = _oh.get("data") or []
                if not _rows:
                    _oh = ex.private_get_trade_orders_history_archive({"instType": "SWAP", "instId": inst_id, "limit": "100"})
                    _rows = _oh.get("data") or []
                _bot_opened = False
                for _o in _rows:
                    if _o.get("state") != "filled": continue
                    if str(_o.get("reduceOnly")) == "true": continue
                    if _o.get("posSide") and _o.get("posSide") != side: continue
                    # ★只採計本倉建立之後(容60秒誤差)的開倉單;早於本倉=別筆舊倉的單,與本倉無關
                    if _pos_ct and int(_o.get("cTime") or 0) < _pos_ct - 60000: continue
                    _is_open = (side == "long" and _o.get("side") == "buy") or (side == "short" and _o.get("side") == "sell")
                    if _is_open and str(_o.get("tag") or "").startswith("6b9ad766b55d"):
                        _bot_opened = True; break
                if not _bot_opened:
                    dc_log(f"🙅 發現手動倉位 {sym} {side}(開倉單無bot標記)→ 不接管、不碰它。停損/停利請自行管理")
                    continue
            except Exception as _own_err:
                print(f"[Adopt] {sym} 開倉來源判定失敗(保守:不接管): {_own_err}", flush=True)
                dc_log(f"⚠️ 未追蹤倉位 {sym} {side} 無法判定開倉來源→ 不接管(保守),請自行確認")
                continue
            sl_id, sl_trig=_okx_fetch_algo_sl(inst_id)
            if not sl_trig:
                dc_log(f"⚠️ 發現未追蹤倉位 {sym} {side}(讀不到止損)→ bot不自動接管，請手動設止損/保本")
                continue
            risk=abs(entry-sl_trig)
            if risk<=0: continue
            # ★2026-07-19 風險距離下限=1×ATR(1H):接管時既有SL可能貼極近(LINK案例0.08%),
            #   使「達1R保本」「浮盈1R才trail」的1R變假、門檻秒過。
            try:
                _dfa = fetch_market_candles(inst_id, "1H", 20)
                if _dfa is not None and not _dfa.empty and len(_dfa) >= 15:
                    _clA = _dfa["close"]
                    _trA = pd.concat([_dfa["high"]-_dfa["low"], (_dfa["high"]-_clA.shift()).abs(),
                                      (_dfa["low"]-_clA.shift()).abs()], axis=1).max(axis=1)
                    _atrA = float(_trA.ewm(alpha=1/14, adjust=False).mean().iloc[-1])
                    if _atrA > 0: risk = max(risk, _atrA)
            except Exception as _atr_err:
                print(f"[Adopt] {sym} ATR下限計算失敗(用原risk): {_atr_err}", flush=True)
            # ★★2026-09-14 改:不再一律 swing_full。先看交易所上掛著的 TP 限價單推回原策略的出場規格
            #   (追蹤紀錄已改存 volume,這裡只是紀錄仍遺失時的保險)。認不出來→adopt_hold 不碰。
            tp1_id=None
            inferred_es  = "swing_full"
            inferred_rem = str(ct)
            try:
                _inf = _infer_adopted_exit(ex, sym, side, entry, sl_trig, ct)
            except Exception as _ie:
                _inf = None
                print(f"[Adopt] {sym} 出場推斷失敗(退回舊行為 swing_full): {_ie}", flush=True)
            if _inf:
                inferred_es, tp1_id, inferred_rem, _why = _inf
                if inferred_es != "swing_full":
                    risk = abs(entry - sl_trig)      # 固定R類用原始停損距(ATR下限只給讓跑類)
                print(f"[Adopt] {sym} {side} 出場推斷 → {inferred_es}({_why})", flush=True)
            tkey=f"okx_adopt_{inst_id}_{side}_{int(time.time())}"
            active_real_trades[tkey]={
                "exchange":"okx","inst_id":inst_id,"symbol":sym,"direction":side,
                "entry_price":str(entry),"sl_algo_id":sl_id,"tp1_order_id":tp1_id,
                "tp1_hit":False,"current_sl":sl_trig,"remaining_amount":inferred_rem,
                "pos_side":side,"risk_dist":risk,"tf_id":"adopted",
                "init_contracts":ct,"pyramid_added":True,"pyramid_eligible":False,
                "exit_strategy":inferred_es,
                "entry_ts":int(time.time()) - 24*3600,  # ★往前24h(2026-06-20):redeploy重撿會重設entry_ts,6h只6根K→trail找不到pivot;24h給夠pivot(錯側由合法側檢查擋)
                "ts_open":int(time.time()),  # ★2026-07-19真實接管時間(給時間停損用;entry_ts被回撥24h不能拿來算時停)
                "bot_verified":True,   # ★2026-08-03 已用broker tag驗證=bot自己開的倉,才准被時間停損碰
            }
            adopted+=1
            _es_txt = {"swing_full": "swing_full 轉折移SL", "bor_1r": "BOR 固定1R(不動)", "s4h_fixed": "S4H 2.5R+1.5R保本",
                       "fourjd_2r": "4JD 2R+0.8R保本", "box_trend": "箱突破 4R", "swing_tp": "TP1半倉+剩半轉折移SL",
                       "": "固定R TP1/TP2", "cme_gap": "CME缺口 2R+300h超時",
                       "adopt_hold": "認不出原策略→交易所SL/TP不動"}.get(inferred_es, inferred_es)
            dc_log(f"📥 已接管未追蹤倉位 {sym} {side}(進場{entry}、止損{sl_trig})→ {_es_txt}")
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
            ccxt_sym = _from_bingx_symbol(bx_sym)   # ★MONAD-USDT→MON/USDT,否則 bot 自己開的倉會被當成「未追蹤」
            if (ccxt_sym, direction) in tracked: continue
            entry = float(p.get("avgPrice") or p.get("entryPrice") or 0)
            if entry <= 0: continue
            # ★2026-07-19 BingX下單不帶broker tag,無法驗證開倉來源→比照OKX手動倉保護原則,
            #   一律不自動接管(寧漏勿越權;bot自己的倉靠active_trades.json持久化,redeploy不會丟)。
            dc_log(f"🙅 BingX 發現未追蹤倉位 {bx_sym} {direction}→ 無法驗證是否bot開倉,不接管、不碰它")
            continue

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
                "ts_open":       int(time.time()),  # ★2026-07-19真實接管時間(時間停損用)
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
CME_GAP_STATE_FILE  = os.path.join(_PERSIST_DIR, "cme_gap_state.json")   # ★09-14 改存 volume(武裝中的缺口 redeploy 不丟)

# ★2026-07-19 全域時間停損(用戶要求改善持單體感):任何倉開超過此時數還沒觸TP/SL→市價平。
# 動機:真實持倉分佈=贏單中位0h(秒收)但輸單拖3.8h、3筆抱>3天(最長85h)→「贏的秒跑輸的拖著看紅盤」體感最差。
# 回測(_bt_fixed_tp時停掃描,固定2.5R空):24h時停→平均持倉68/45h砍到20/18h、勝率44→50%、EV降但2策略仍5/7正。
# 用戶選24h(帳戶小無所謂,體感優先)。cme_gap有自己的300h超時故豁免。
GLOBAL_TIMESTOP_H   = 10 ** 6   # ★★★2026-09-06 **關閉時間停損**(用戶:「而且贏單也被你的時間停損砍掉」「關掉啊」)
# 這是推翻 2026-08-01 那次決定(當時是用戶要求、為了勝率與體感)。推翻依據=**live 實際成交**,不是回測:
#   ┌ 持倉時長 ─────────┬ n ─┬ 勝率 ┬ 均損益 ─┬ 總計 ────┐
#   │ <2h                │162 │ 21% │  −0.89U │ −143.6U │  ← 輸單很快就解決
#   │ 2-6h               │116 │ 21% │  −4.21U │ −488.1U │
#   │ 6-11.5h            │ 45 │ 76% │  +1.50U │  +67.3U │
#   │ ★11.5-13h(12h時停) │ 46 │ 63% │  +1.15U │  +53.0U │  ← **63%是贏的,卻只賺到零頭**
#   │ ★>25h(活過時停)    │ 38 │ 58% │  +6.46U │ +245.7U │  ← **是被砍那批的 5.6 倍**
#   └────────────────────┴────┴─────┴─────────┴─────────┘
#   ★輸單在6小時內就解決(<2h與2-6h勝率都只有21%),贏單需要更久 → **時間停損砍的正好是贏單**。
# 這也是 live 賺賠比塌掉的主因之一:bot 實測 **均賺+2.09U / 均賠−13.86U = 賺賠比 0.15**
#   (同期用戶手動是 2.30)。「均賺2.09」正是贏單被時停砍在小賺的直接後果。
# 回測也同向(2026-09-06 吞噬空,同一批訊號只換時停):
#   12h EV+0.091/容錯4.1 → 24h +0.183/7.0 → 48h +0.209/7.4 → **無時停 +0.224/7.5**
# ★當初 08-01 的依據是「時停提高勝率」(44.2%→50.2%),那是真的;但**勝率提高的代價是把贏單砍成零頭**,
#   當時只量了勝率沒量賺賠比,所以看不到這件事。兩邊證據(live實測+回測)現在都指向關閉。
# ★機制保留:cme_gap 仍走自己的300h;個別策略仍可用 ts_h 指定(負值=不設,正值=自訂)。
# 原註(2026-08-01):24→12h(用戶:「不要那種靠少數單贏的,等到來帳戶都沒錢了」)。
# 時間停損是提高勝率的真正槓桿(把卡住不動、慢慢走向停損的單提前收在小賺小賠),且不犧牲容錯空間:
#   回測(_bt_winrate_config.py,7期) TP2.5R勝率: 無時停44.2% → 24h 49.1% → 12h 50.2%;
#   OISQ空 45.7%→52.1%→54.3%; OISQ多 39.0%→53.1%→55.4%(6/7期正)。EV略降(空+0.145→+0.102)但仍正。
# ★為何不改用小TP(0.5R)衝勝率:兩平勝率66.7% vs 回測72.5%=容錯僅5.8點,而live勝率一向比回測低8-10點
#   →實盤會直接翻負(看起來一直在贏、帳戶卻一直縮)。TP維持2.5R(容錯15.6點)靠時停提勝率才是穩的做法。
# ★★2026-08-02 訂正:12h**只適用固定R型**。全策略檢視發現讓跑型吃不消(我原本全域套用=沒分策略驗證的錯):
#   MACD多(swing_full讓跑,用真實出場模型重測): 無時停EV+0.309/容錯18.9 → 24h +0.271/18.3 → **12h +0.134/12.1**
#     =12h砍掉57%EV、容錯掉6.2點,只換到+3.5點勝率 → 不划算,改24h。
#   OISQ多(讓跑+2.5ATR上限): 24h +0.215/容錯11.3 ≈ 12h +0.183/11.1(12h勝率高2.4點) → 兩者接近,統一走24h。
#   固定R型(OISQ空/吞噬空/箱突破空)維持12h(已驗證:OISQ空容錯17.8🟢)。
LETRUN_TIMESTOP_H   = 10 ** 6   # ★2026-09-06 一併關閉(理由同上)。讓跑型本來就靠尾部獲利,
# 24h 時停等於把「讓跑」的前提拿掉。原註:讓跑型專用(見上方訂正說明)
_LETRUN_ES = ("swing_full", "line_full", "line_add", "swing_tp", "swing_tp_1h", "tp_line")

def _timestop_hours(trade) -> int:
    """依出場型態決定時間停損時數:讓跑型24h/固定R型12h(cme_gap另有300h,不走此函數)。"""
    _h = int(trade.get("ts_h") or 0)
    # ★2026-09-04 負值 = 該策略**明確不設**時間停損(S4H 的回測就是不設時停,設了就不是同一個規格)。
    #   不能用 0 表示,0 在此函數是「用型態預設」,而呼叫端的 `now-ts_open > _tsh*3600` 遇到 0 會**立刻平倉**。
    if _h < 0: return 10 ** 6
    if _h > 0: return _h                     # 策略專屬(下單時寫入)優先
    return LETRUN_TIMESTOP_H if trade.get("exit_strategy") in _LETRUN_ES else GLOBAL_TIMESTOP_H
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
    load_risk_state()   # ★BOR/4JD 熔斷計數(存在 volume,redeploy 不歸零)
    print(f"[Persist] 存檔目錄 {_PERSIST_DIR}", flush=True)
    _cme_load_state()   # CME週末缺口狀態(redeploy不丟武裝中的缺口)
    # 接管現有未追蹤的 OKX 倉位（手動開的/重啟前丟失的）→ 讀既有止損納入自動保本
    adopt_untracked_okx_positions()
    # 接管現有未追蹤的 BingX 倉位（與 OKX adopt 對齊，解決 redeploy 後 BingX 追蹤全失）
    adopt_untracked_bingx_positions()
    n_sym = len(SYMBOLS)

    # ★2026-09-24 儀表板:啟動就先取樣一次。主迴圈的取樣點在 synchronise_and_wait_next_candle
    #   **之後**,所以 redeploy 完最多要等 15 分鐘才有第一個點,儀表板整段時間是空的。
    try:
        _dash_hist_load()      # ★先讀回存檔,再取樣 → redeploy 後 1H/12H 窗接得上,不用重等
        _oi_sample_tick(force=True)
        print(f"[DASH] 啟動取樣完成:報價 {len(_TICKER_SNAP)} 幣 / OI 追蹤 {len(_oi_history)} 幣", flush=True)
        Thread(target=_dash_sampler_loop, daemon=True).start()
    except Exception as _ise:
        print(f"[DASH] 啟動取樣失敗(不影響交易): {_ise}", flush=True)

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

            # ★2026-08-01 出場管理必須先跑,且不受 !pause 影響。
            #   舊版 `if _PAUSED: continue` 擺在前面→暫停時連「既有倉的移動停損/保本/時間停損」都停擺,
            #   等於裸奔只剩原始SL(與熔斷訊息宣稱的「既有倉照常管理」不符)。!pause 語意=停開新倉,不是停管理。
            check_trailing_stops_for_real()

            if _PAUSED:
                continue

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

            # 每日自動更新幣種列表（1天 = 86400秒)：CoinGecko市值前100慢變動,含合約列表全量重建
            if time.time() - _symbols_last_updated > 86400:
                print("[SYMBOLS] 距上次全量更新超過1天，自動重新抓取(市值前100+漲跌幅榜)...", flush=True)
                build_dynamic_symbols()
            # ★2026-07-07:漲跌幅榜獨立輕量刷新(MOVERS_REFRESH_SEC=1H),不等24h全量週期
            #   只打1支OKX ticker API,不碰CoinGecko,負擔可忽略;治「幣中途暴衝完落幕整段沒進掃描池」
            elif time.time() - _movers_last_updated > MOVERS_REFRESH_SEC:
                refresh_top_movers_only()

            # ★2026-09-24 儀表板:OI/價格取樣拉到 15 分鐘一次(原本綁在 1H 的 movers 刷新裡)。
            #   數據獵手的 OI 異動排名看的是 **1H 變化**,取樣 1H 一點的話 1H 窗只有兩點=沒有解析度。
            #   成本:open-interest + tickers 各一支(每支一次涵蓋全市場約400合約),96次/天,可忽略。
            # ★2026-09-24 取樣已搬到 `_dash_sampler_loop` 獨立執行緒。
            #   留在這裡的話節拍會被 `synchronise_and_wait_next_candle` 綁成 15 分鐘
            #   （那正是 1H 窗開洞的原因，說明見該函數的 docstring）。這裡不再呼叫。

            # ★★2026-08-27 致命縮排bug修復:原本 `for symbol_item` 迴圈**沒有包在 `for tf` 裡面**
            #   (兩個 for 同一層縮排),導致 `for tf` 只印字,真正的掃描只跑**一次**、且用最後一個 tf。
            #   實際後果(synchronise_and_wait_next_candle 回傳剛收盤的時框):
            #     :15/:45 → ["15m"]              → 掃 15m ✓
            #     :30     → ["15m","30m"]        → 只掃 30m,**15m 被跳過**
            #     :00     → ["15m","30m","1H"]   → 只掃 1H,**15m 與 30m 都被跳過**
            #   = 15m 與 30m 各損失**一半**的掃描機會。4J 兩階(2H→15m / 4H→30m)剛好全掛在這兩個時框上,
            #   這是「訊號量不正常地少」的主因之一(用戶質疑「4j不用數據」逼出來的查證)。
            for tf in active_tfs_to_run:
                print(f"[{datetime.now().strftime('%H:%M:%S')}] ⏳ 核心排程觸發：啟動時框 {tf} 全商品指標矩陣掃描...")
                _FOURJ_STAT.update({"eval": 0, "setup": 0, "fire": 0, "nodata": 0})
                for symbol_item in list(SYMBOLS.values()):
                    try:
                        _bot_ref.scan_and_process_market(symbol_item, tf)
                        sleep(0.25)
                    except Exception as loop_exception:
                        print(f"  ❌ 商品 {symbol_item} 於時框 [{tf}] 處理時發生系統例外: {loop_exception}")
                if tf in FOURJ_LADDER:
                    print(f"[4J儀表] {tf}: 幣池{len(SYMBOLS)} 評估{_FOURJ_STAT['eval']} "
                          f"結構K不足{_FOURJ_STAT['nodata']} 有setup{_FOURJ_STAT['setup']} "
                          f"觸發{_FOURJ_STAT['fire']}", flush=True)

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
    # ★★2026-09-24 這個檢查原本打 fapi.binance.com 永遠 451,結論被寫成「幣安封到底」。
    #   實測(/d/<token>/egress 探針)發現封鎖是**按網域**的:fapi/api 被封,**www.binance.com 通**,
    #   同一份 /fapi/v1/* 路徑照回真實資料。所以這裡改打 _BN_HOSTS[0](=www)。
    #   回測的合約CVD是用**幣安 taker** 推的,live 用 OKX rubik 代替,
    #   實測兩者12h窗方向一致率只有 79%(約1/5時候相反) → 幣安既然可用,CVD 源應回頭對齊(待驗)。
    for _bh in _BN_HOSTS[:3]:
        try:
            _bt = requests.get(_bh + "/fapi/v1/klines",
                               params={"symbol": "BTCUSDT", "interval": "15m", "limit": 3}, timeout=8)
            print(f"[連通性] 幣安 {_bh} klines → HTTP {_bt.status_code} "
                  f"{'✅可用' if _bt.status_code == 200 else '❌'} "
                  f"{_bt.text[:100] if _bt.status_code != 200 else ''}", flush=True)
            if _bt.status_code == 200:
                break
        except Exception as _be:
            print(f"[連通性] 幣安 {_bh} ❌ {type(_be).__name__}: {str(_be)[:100]}", flush=True)
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
