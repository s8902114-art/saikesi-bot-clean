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

OKX_API_KEY = os.environ.get("OKX_API_KEY", "")
OKX_SECRET = os.environ.get("OKX_SECRET_KEY", "359300E99DD8870F8990CC698BC4F491")
OKX_PASSPHRASE = os.environ.get("OKX_PASSPHRASE", "Small5017714@")
OKX_DEMO = False  # 是否啟用 OKX 模擬盤交易環境

# ══════════════════════════════════════════════════════════════════════════════

# 策略風控常數與運行狀態機

# ══════════════════════════════════════════════════════════════════════════════

MAX_LEVERAGE = 100         # 系統最高安全槓桿限制
RISK_PCT     = 0.10        # 單筆最大風險金額 = 啟動時總資金 × 10%
POSITION_SLOTS = 10        # 倉位格數（保留供 !setslots 指令使用）
SIGNAL_COOLDOWN = 1800     # 同一商品商品相同時框的訊號冷卻時間 (秒)
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
CVD_ENABLED: bool = True   # 秋總三層 CVD 背離吸收過濾開關
ADX_ENABLED: bool = True   # ADX >= ADX_THR 過濾開關

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
PIVOT_LEN = 2     # Pivot 結構點左右各需 N 根確認
FUNDING_LONG_MAX = 0.0001
FUNDING_SHORT_MIN = -0.0001
CVD_WINDOW = 3
BEAR_MIN_BARS = 20

# 🌟 全局變數：用於追蹤 Discord 歷史最高訊息 ID，防重複處理

_dc_last_msg_id = "0"

# ══════════════════════════════════════════════════════════════════════════════

# 多時框全自動路由最佳化動態參數對照表 (PRODUCTION CONFIG MAPPINGS)

# ══════════════════════════════════════════════════════════════════════════════

BEST_PARAMS: Dict[str, Dict[str, Any]] = {
# 手動設定 2026-05-22；C3 改為 rsiMa 穿越 50 線版本
"15m_long": {
"tp1_mult": 1.725,  "tp2_intraday_mult": 1.8,  "tp2_swing_mult": 1.8,
"sl_atr_buffer": 0.08, "structure_lookback": 17, "exit_mode": "trailing",
"qqe_rsi": 7, "qqe_sf": 5, "qqe_factor": 3.0
},
"15m_short": {
"tp1_mult": 2.0,    "tp2_intraday_mult": 3.2,  "tp2_swing_mult": 3.2,
"sl_atr_buffer": 0.03, "structure_lookback": 20, "exit_mode": "fixed",
"qqe_rsi": 5, "qqe_sf": 6, "qqe_factor": 3.0
},
"30m_long": {
"tp1_mult": 1.725,  "tp2_intraday_mult": 1.8,  "tp2_swing_mult": 1.8,
"sl_atr_buffer": 0.05, "structure_lookback": 10, "exit_mode": "fixed",
"qqe_rsi": 5, "qqe_sf": 2, "qqe_factor": 3.0
},
"30m_short": {
"tp1_mult": 2.0,    "tp2_intraday_mult": 3.2,  "tp2_swing_mult": 3.2,
"sl_atr_buffer": 0.01, "structure_lookback": 10, "exit_mode": "trailing",
"qqe_rsi": 5, "qqe_sf": 3, "qqe_factor": 4.0
},
"1H_long": {
"tp1_mult": 1.725,  "tp2_intraday_mult": 2.5,  "tp2_swing_mult": 2.5,
"sl_atr_buffer": 0.15, "structure_lookback": 10, "exit_mode": "fixed",
"qqe_rsi": 8, "qqe_sf": 2, "qqe_factor": 3.0
},
"1H_short": {
"tp1_mult": 2.0,    "tp2_intraday_mult": 4.0,  "tp2_swing_mult": 4.0,
"sl_atr_buffer": 0.08, "structure_lookback": 20, "exit_mode": "fixed",
"qqe_rsi": 5, "qqe_sf": 7, "qqe_factor": 4.238
},
"4H_long": {
"tp1_mult": 1.725,  "tp2_intraday_mult": 2.5,  "tp2_swing_mult": 2.5,
"sl_atr_buffer": 0.03, "structure_lookback": 10, "exit_mode": "trailing",
"qqe_rsi": 6, "qqe_sf": 3, "qqe_factor": 3.0
},
"4H_short": {
"tp1_mult": 2.0,    "tp2_intraday_mult": 4.0,  "tp2_swing_mult": 4.0,
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

    embed_payload = {
    "title": f"{side_emoji} {coin_name} [{tf} - {dir_name}]",
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

def calculate_smooth_rsi(series_src: pd.Series, rolling_period: int) -> pd.Series:
    """ 計算非線性標準化相對強弱指標線 """
    price_delta = series_src.diff()
    up_trends = price_delta.clip(lower=0.0)
    down_trends = -price_delta.clip(upper=0.0)
    mean_up = up_trends.ewm(com=rolling_period - 1, adjust=False).mean()
    mean_down = down_trends.ewm(com=rolling_period - 1, adjust=False).mean()
    rs_value = mean_up / mean_down.replace(0.0, 1e-9)
    return 100.0 - (100.0 / (1.0 + rs_value))

def calculate_full_qqe_mod(data_df: pd.DataFrame, rsi_pd: int = 6, sf_pd: int = 5, factor_mult: float = 4.236) -> Tuple[pd.Series, pd.Series]:
    """ 完全體 QQE MOD 動態移動區間軌道演算演算法 """
    src_close = data_df["close"]
    rsi_series = calculate_smooth_rsi(src_close, rsi_pd)
    rsi_smoothed_ma = rsi_series.ewm(span=sf_pd, adjust=False).mean()
    absolute_rsi_delta = rsi_smoothed_ma.diff().abs()
    smoothed_atr_rsi = absolute_rsi_delta.ewm(span=2 * rsi_pd - 1, adjust=False).mean()
    dar_trailing_band = smoothed_atr_rsi.ewm(span=2 * rsi_pd - 1, adjust=False).mean() * factor_mult

    trailing_line_value = float('nan')
    trailing_buffer_list = []
    for idx in range(len(rsi_smoothed_ma)):
        current_ma_val = rsi_smoothed_ma.iloc[idx]
        dar_val = dar_trailing_band.iloc[idx]
        if idx == 0 or (current_ma_val != current_ma_val) or (dar_val != dar_val):
            # NaN guard: keep previous or initialize
            if trailing_line_value != trailing_line_value:
                trailing_line_value = current_ma_val if (current_ma_val == current_ma_val) else 50.0
        else:
            previous_trailing_value = trailing_line_value
            if previous_trailing_value != previous_trailing_value:
                previous_trailing_value = current_ma_val
            if current_ma_val < previous_trailing_value:
                # Bear state: trail ratchets down (can only decrease)
                trailing_line_value = min(previous_trailing_value, current_ma_val + dar_val)
            else:
                # Bull state: trail ratchets up (can only increase)
                trailing_line_value = max(previous_trailing_value, current_ma_val - dar_val)
        trailing_buffer_list.append(trailing_line_value)
    return rsi_smoothed_ma, pd.Series(trailing_buffer_list, index=data_df.index)

def calculate_average_true_range(data_df: pd.DataFrame, atr_period: int = 14) -> pd.Series:
    """ 計算真實波動幅度均值 (ATR) """
    high_prices = data_df["high"]
    low_prices = data_df["low"]
    previous_closes = data_df["close"].shift(1)
    tr1 = high_prices - low_prices
    tr2 = (high_prices - previous_closes).abs()
    tr3 = (low_prices - previous_closes).abs()
    true_range = pd.concat([tr1, tr2, tr3], axis=1).max(axis=1)
    return true_range.ewm(alpha=1.0 / atr_period, adjust=False).mean()

def calculate_directional_movement_index(data_df: pd.DataFrame, adx_period: int = 14) -> pd.Series:
    """ 動向指標 (DMI/ADX) 趨勢強度過濾器 """
    high_diff = data_df["high"].diff()
    low_diff = -data_df["low"].diff()
    plus_dm = np.where((high_diff > low_diff) & (high_diff > 0), high_diff, 0.0)
    minus_dm = np.where((low_diff > high_diff) & (low_diff > 0), low_diff, 0.0)

    atr_series = calculate_average_true_range(data_df, adx_period).replace(0.0, 1e-9)
    plus_di = 100.0 * pd.Series(plus_dm, index=data_df.index).ewm(alpha=1.0 / adx_period, adjust=False).mean() / atr_series
    minus_di = 100.0 * pd.Series(minus_dm, index=data_df.index).ewm(alpha=1.0 / adx_period, adjust=False).mean() / atr_series

    dx_value = 100.0 * (plus_di - minus_di).abs() / (plus_di + minus_di).replace(0.0, 1e-9)
    return dx_value.ewm(alpha=1.0 / adx_period, adjust=False).mean()

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
    """ 使用 OKX REST API 掛條件式止損 Algo 單 (slTriggerPx) """
    now_utc = datetime.now(timezone.utc)
    ts = now_utc.strftime("%Y-%m-%dT%H:%M:%S.") + f"{now_utc.microsecond // 1000:03d}Z"
    body = json.dumps({
        "instId": inst_id, "tdMode": "isolated", "side": side,
        "ordType": "conditional", "sz": amount, "posSide": pos_side,
        "slTriggerPx": sl_trigger_px, "slOrdPx": "-1"
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
        "instId": inst_id, "tdMode": "isolated", "side": side,
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
                              tf_id: str = "15m") -> None:
    """ 實盤訂單路由模組：整合動態槓桿、精密合約張數轉換、市價與限價單組合 """
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

        # ── RISK 公式（動態基準：每次下單重新抓當前總資產含未實現盈虧）─────────
        total_usdt = float(balance_data.get("USDT", {}).get("total", 0.0))
        base_funds = total_usdt if total_usdt > 0 else available_usdt
        risk_usdt  = base_funds * RISK_PCT         # 單筆最大風險金額 = 當前總資產 × RISK_PCT

        ticker_info = ex.fetch_ticker(symbol_id)
        current_market_price = float(ticker_info.get("last", entry_price))

        sl_distance_pct = abs(current_market_price - stop_loss) / current_market_price
        if sl_distance_pct <= 0.0001:
            dc_log("⚠️ **風控異常**: 結構止損間距過小，自動拒絕下單以防爆倉。")
            return

        # 槓桿 = min(50 ÷ 止損距離%, MAX_LEVERAGE)
        calculated_leverage = max(1, min(int(50.0 / (sl_distance_pct * 100.0)), MAX_LEVERAGE))

        # 倉位價值 = 風險金額 ÷ 止損距離%；保證金 = 倉位價值 ÷ 槓桿
        position_value  = risk_usdt / sl_distance_pct
        allocated_margin = position_value / calculated_leverage

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

        market_structure = ex.market(symbol_id)
        amount_precision = int(market_structure.get("precision", {}).get("amount", 0))
        contract_size = float(market_structure.get("contractSize", 1.0) or 1.0)

        raw_order_amount = position_value / (current_market_price * contract_size)

        if amount_precision == 0:
            final_order_amount = max(1, int(raw_order_amount))
            split_half_amount = max(1, int(final_order_amount // 2))
        else:
            final_order_amount = max(round(1.0 / contract_size, amount_precision), round(raw_order_amount, amount_precision))
            split_half_amount = round(final_order_amount / 2.0, amount_precision)

        try:
            ex.set_leverage(calculated_leverage, symbol_id, params={"posSide": trade_side})
        except:
            pass

        is_buy = (trade_side == "long")
        entry_action = "buy" if is_buy else "sell"
        exit_action = "sell" if is_buy else "buy"
        inst_id = OKX_SWAP.get(symbol_id, symbol_id)

        execution_report = [
            f"🚀 **賽克斯實盤下單鏈成功發動**",
            f"商品代號: `{symbol_id}` | 交易方向: `{'做多 LONG' if is_buy else '做空 SHORT'}`",
            f"配置槓桿: `{calculated_leverage}x` | 下單張數: `{final_order_amount}` | 保證金: `{allocated_margin:.2f}` USDT | 風險: `{risk_usdt:.2f}` USDT ({RISK_PCT*100:.0f}%)"
        ]

        entry_order = ex.create_market_order(
            symbol=symbol_id,
            side=entry_action,
            amount=final_order_amount,
            params={"posSide": trade_side, "tdMode": "isolated"}
        )
        executed_average_price = entry_order.get("average", current_market_price)
        execution_report.append(f"交易所實際成交均價: `{executed_average_price}`")

        sl_algo_id   = None
        tp1_order_id = None

        # 止損：OKX algo slTriggerPx 條件單
        # posSide 必須與倉位方向一致：做多 side=sell/posSide=long，做空 side=buy/posSide=short
        try:
            sl_result = _place_okx_algo_sl(
                inst_id=inst_id,
                side="sell" if trade_side == "long" else "buy",
                amount=str(final_order_amount), sl_trigger_px=str(stop_loss),
                pos_side=trade_side   # "long" or "short"
            )
            sl_algo_id = (sl_result.get("data") or [{}])[0].get("algoId")
            execution_report.append(f"🛑 OKX Algo 止損已錨定: `{stop_loss}` (algoId: {sl_algo_id})")
        except Exception as sle:
            execution_report.append(f"⚠️ 止損單掛載失敗: {sle}")

        # TP1：固定限價（50%）
        try:
            tp1_order = ex.create_order(
                symbol=symbol_id, type="limit", side=exit_action,
                amount=split_half_amount, price=tp1,
                params={"posSide": trade_side, "tdMode": "isolated", "reduceOnly": True}
            )
            tp1_order_id = tp1_order.get("id")
            execution_report.append(f"🌓 TP1 固定限價單掛置 (50%): `{tp1}` (ordId: {tp1_order_id})")
        except Exception as tp1e:
            execution_report.append(f"⚠️ TP1委託失敗: {tp1e}")

        remainder_amount = final_order_amount - split_half_amount
        if amount_precision == 0:
            remainder_amount = int(remainder_amount)
        else:
            remainder_amount = round(remainder_amount, amount_precision)

        # TP2 最小下單量檢查
        _min_amt = float(((market_structure.get("limits") or {}).get("amount") or {}).get("min") or 0)
        if _min_amt == 0:
            _min_amt = 1.0 / float(contract_size) if float(contract_size) > 0 else 1.0
        if remainder_amount <= 0 or remainder_amount < _min_amt:
            dc_log(f"⚠️ TP2 跳過：剩餘張數 {remainder_amount} 小於最小下單量 {_min_amt}")
        else:
            # TP2：exit_mode=fixed → 限價；exit_mode=trailing → 追蹤止損 Algo，不掛限價
            if exit_mode == "trailing":
                try:
                    _place_okx_algo_trailing(
                        inst_id=inst_id, side=exit_action,
                        amount=str(remainder_amount), callback_ratio="0.02",
                        pos_side=trade_side
                    )
                    execution_report.append(f"🌕 TP2 追蹤止損 Algo 已掛置 (50%) 回撤率: 2%")
                except Exception as tp2e:
                    execution_report.append(f"⚠️ TP2 追蹤止損委託失敗: {tp2e}")
            else:
                try:
                    ex.create_order(
                        symbol=symbol_id, type="limit", side=exit_action,
                        amount=remainder_amount, price=tp2,
                        params={"posSide": trade_side, "tdMode": "isolated", "reduceOnly": True}
                    )
                    execution_report.append(f"🌕 TP2 固定限價單掛置 (50%): `{tp2}`")
                except Exception as tp2e:
                    execution_report.append(f"⚠️ TP2委託失敗: {tp2e}")

        # exit_mode=trailing：寫入實盤追蹤止損狀態機
        if exit_mode == "trailing" and sl_algo_id and tp1_order_id:
            trade_key = f"{inst_id}_{trade_side}_{int(time.time())}"
            active_real_trades[trade_key] = {
                "inst_id":          inst_id,
                "symbol":           symbol_id,
                "direction":        trade_side,
                "entry_price":      executed_average_price,
                "sl_algo_id":       sl_algo_id,
                "tp1_order_id":     tp1_order_id,
                "tp1_hit":          False,
                "current_sl":       stop_loss,
                "remaining_amount": str(remainder_amount),
                "pos_side":         trade_side,
                "tf_id":            tf_id,
            }
            execution_report.append(f"📊 追蹤止損狀態機已啟動 (key: {trade_key})")

        dc_log("\n".join(execution_report))
    except Exception as general_error:
        dc_log(f"❌ **交易所執行鏈嚴重崩潰**: {general_error}")


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

                    # 2. 掛新止損在成本價（break-even）
                    be_price  = trade["entry_price"]
                    exit_side = "sell" if direction == "long" else "buy"
                    sl_result = _place_okx_algo_sl(
                        inst_id=inst_id, side=exit_side,
                        amount=trade["remaining_amount"],
                        sl_trigger_px=str(round(be_price, 5)),
                        pos_side=direction
                    )
                    new_algo_id = (sl_result.get("data") or [{}])[0].get("algoId")
                    if new_algo_id:
                        trade["sl_algo_id"] = new_algo_id
                        trade["current_sl"] = be_price
                        trade["tp1_hit"]    = True

                    # 3. DC + TG 通知
                    msg = f"✅ TP1 已成交，止損移至成本價 {be_price}\n幣種：{name}"
                    dc_log(msg)
                    tg_log(msg)
                    print(f"[Trailing] {name} TP1成交，SL移至成本價 {be_price}")

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

def _find_pivot_low(df: pd.DataFrame, pivot_len: int = PIVOT_LEN) -> Optional[float]:
    """找最近一個已確認的 pivot low（左右各 pivot_len 根都比它高）"""
    lows = df["low"].values
    n = len(lows)
    for i in range(n - pivot_len - 1, pivot_len - 1, -1):
        if (all(lows[i] < lows[i - j] for j in range(1, pivot_len + 1)) and
                all(lows[i] < lows[i + j] for j in range(1, pivot_len + 1))):
            return float(lows[i])
    return None

def _find_pivot_high(df: pd.DataFrame, pivot_len: int = PIVOT_LEN) -> Optional[float]:
    """找最近一個已確認的 pivot high（左右各 pivot_len 根都比它低）"""
    highs = df["high"].values
    n = len(highs)
    for i in range(n - pivot_len - 1, pivot_len - 1, -1):
        if (all(highs[i] > highs[i - j] for j in range(1, pivot_len + 1)) and
                all(highs[i] > highs[i + j] for j in range(1, pivot_len + 1))):
            return float(highs[i])
    return None

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

class SykesTradingBot:
    def __init__(self):
        self.cooldown_dict: Dict[str, float] = {}
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
        if self.check_circuit_breaker():
            return
        if self.is_cooldown(symbol_item, tf_id):
            return

        okx_swap_symbol = OKX_SWAP.get(symbol_item)
        if not okx_swap_symbol:
            return

    # 1. 行情數據拉取
        okx_bar_fmt = BAR_TO_CONA.get(tf_id, "15min")
        df = fetch_market_candles(okx_swap_symbol, tf_id)
        if df.empty or len(df) < 100:
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

    # 5. 空頭趨勢（連續 >= 20 根 EMA144 < EMA576）
        bear_trend = (ema144.iloc[-20:] < ema576.iloc[-20:]).all()

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
        is_short = (bear_trend and
                    short_C1 and short_C2 and short_C3 and
                    (not ADX_ENABLED or current_adx >= ADX_THR) and
                    (funding_rate is None or funding_rate >= FUNDING_SHORT_MIN))

        if not is_long and not is_short:
            return

        direction = "long" if is_long else "short"

    # 8. 秋總三層背離吸收 CVD 過濾
        if CVD_ENABLED:
            cvd_pass, cvd_reason = _check_cvd_absorption(
                symbol_item, tf_id, okx_bar_fmt, df, direction
            )
        else:
            cvd_pass, cvd_reason = True, "CVD 已停用"

    # 9. SL/TP 計算（SL 改用 Pivot 結構點，備援為近5根極值）
        p = p_l if is_long else p_s

        if is_long:
            pivot_sl      = _find_pivot_low(df, PIVOT_LEN)
            calculated_sl = pivot_sl if pivot_sl is not None else float(df["low"].iloc[-5:].min())
            risk_pct      = abs(current_close - calculated_sl) / current_close
            if risk_pct > MAX_SL:
                calculated_sl = current_close * (1.0 - MAX_SL)
                risk_pct = MAX_SL
            is_swing   = self._get_4h_swing_flag(okx_swap_symbol, df, tf_id)
            tp2_mult   = p["tp2_swing_mult"] if is_swing else p["tp2_intraday_mult"]
            tp1_target = current_close + current_atr * p["tp1_mult"]
            tp2_target = current_close + current_atr * tp2_mult
        else:
            pivot_sl      = _find_pivot_high(df, PIVOT_LEN)
            calculated_sl = pivot_sl if pivot_sl is not None else float(df["high"].iloc[-5:].max())
            risk_pct      = abs(calculated_sl - current_close) / current_close
            if risk_pct > MAX_SL:
                calculated_sl = current_close * (1.0 + MAX_SL)
                risk_pct = MAX_SL
            is_swing   = self._get_4h_swing_flag(okx_swap_symbol, df, tf_id)
            tp2_mult   = p["tp2_swing_mult"] if is_swing else p["tp2_intraday_mult"]
            tp1_target = current_close - current_atr * p["tp1_mult"]
            tp2_target = current_close - current_atr * tp2_mult

        risk_delta = abs(current_close - calculated_sl) or 1e-9
        rr1 = abs(tp1_target - current_close) / risk_delta
        rr2 = abs(tp2_target - current_close) / risk_delta

        signal_payload = {
            "side": direction, "entry": current_close, "sl": round(calculated_sl, 5),
            "tp1": round(tp1_target, 5), "tp2": round(tp2_target, 5), "atr": round(current_atr, 4),
            "risk_pct": risk_pct * 100.0, "rr1": rr1, "rr2": rr2, "is_swing": is_swing,
            "exit_mode": p["exit_mode"], "time": datetime.now(timezone.utc).isoformat()
        }

        self.set_cooldown(symbol_item, tf_id)
        create_interactive_signal(signal_payload, symbol_item, tf_id, cvd_pass)

        if AUTO_TRADE.get(tf_id) and cvd_pass:
            execute_okx_trade_pipeline(
                okx_swap_symbol, direction, current_close,
                signal_payload["sl"], signal_payload["tp1"], signal_payload["tp2"], p["exit_mode"], tf_id
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
    global _PAUSED, _LIVE_MODE, _dc_last_msg_id, POSITION_SLOTS
    global CVD_ENABLED, ADX_ENABLED, AUTO_TRADE
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
                                    avail  = float(bal.get("USDT", {}).get("free", 0.0))
                                    per_slot = avail / POSITION_SLOTS
                                    per_slot_margin_str = f"{per_slot:.2f} USDT (餘額 {avail:.2f} ÷ {POSITION_SLOTS} 倉)"
                                except:
                                    per_slot_margin_str = "查詢失敗"
                            tf_status = "  ".join(
                                f"`{k}`:{'✅' if v else '🔕'}" for k, v in AUTO_TRADE.items()
                            )
                            dc_log(
                                f"⚙️ **賽克斯系統狀態**\n"
                                f"狀態: {paused} | 模式: **{mode}**\n"
                                f"CVD 過濾: {'✅ 開' if CVD_ENABLED else '🔕 關'}  "
                                f"ADX 過濾: {'✅ 開' if ADX_ENABLED else '🔕 關'}\n"
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
                                "`!setslots [數字]` - 設定倉位格數\n"
                                "`/cvd on|off` - 開關 CVD 過濾\n"
                                "`/adx on|off` - 開關 ADX 過濾\n"
                                "`/trade [15m|30m|1h|4h|all] on|off` - 開關自動下單\n"
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

                        # ── setslots ───────────────────────────────────
                        elif cmd == "setslots":
                            if len(parts) >= 2 and parts[1].isdigit() and int(parts[1]) >= 1:
                                POSITION_SLOTS = int(parts[1])
                                dc_log(f"⚙️ 倉位格數已更新: `{POSITION_SLOTS}` 倉")
                            else:
                                dc_log("⚠️ 用法: `!setslots [正整數]`，例如 `!setslots 10`")

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

        except Exception as e:
            print(f"[DC] 指令輪詢異常: {e}")
        sleep(5)
def main_polling_loop():
    """ 交易中樞核心守護進程主迴圈 """
    global _PAUSED, _bot_ref, _INITIAL_BALANCE
    start_alert = "🚀 **賽克斯全功能完全體智慧交易系統 v4 實盤部署完成**\n控制中樞已成功對齊 40+ 主流加密商品，開始進行 15m/30m/1H/4H 收盤矩陣輪詢機制..."
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
        active_tfs_to_run = synchronise_and_wait_next_candle()

        if _PAUSED:
            continue

        check_trailing_stops_for_real()

        for tf in active_tfs_to_run:
            print(f"[{datetime.now().strftime('%H:%M:%S')}] ⏳ 核心排程觸發：啟動時框 {tf} 全商品指標矩陣掃描...")
        for symbol_item in SYMBOLS.values():
            try:
                _bot_ref.scan_and_process_market(symbol_item, tf)
                sleep(0.25)  # 內部防爆頻率限流阻尼
            except Exception as loop_exception:
                print(f"  ❌ 商品 {symbol_item} 於時框 [{tf}] 處理時發生系統例外: {loop_exception}")

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
