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
#  環境相依性與編碼修正
# ══════════════════════════════════════════════════════════════════════════════
if hasattr(sys.stdout, "reconfigure"):
    sys.stdout.reconfigure(encoding="utf-8", errors="replace")
else:
    sys.stdout = io.TextIOWrapper(sys.stdout.buffer, encoding="utf-8", errors="replace")

# 自動檢查並安裝缺少的第三方套件
REQUIRED_PACKAGES = ["requests", "pandas", "numpy", "ccxt", "flask", "pynacl"]
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
#  核心全局配置與金鑰設定 (USER CONFIGURATION)
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
OKX_SECRET = os.environ.get("OKX_SECRET_KEY", "")
OKX_PASSPHRASE = os.environ.get("OKX_PASSPHRASE", "")
OKX_DEMO = False  # 是否啟用 OKX 模擬盤交易環境

# ══════════════════════════════════════════════════════════════════════════════
#  策略風控常數與運行狀態機
# ══════════════════════════════════════════════════════════════════════════════
MAX_LEVERAGE = 100         # 系統最高安全槓桿限制
MARGIN_PCT = 10.0          # 單筆交易佔用總可用資金之百分比 (10.0 = 10%)
SIGNAL_COOLDOWN = 1800     # 同一商品商品相同時框的訊號冷卻時間 (秒)
MAX_CONSEC_LOSS = 3       # 最大連續虧損次數限制，達標後觸發熔斷
PAUSE_HOURS = 24           # 熔斷冷卻時間 (小時)

# 系統底層控制開關
_LIVE_MODE = True if os.environ.get("OKX_API_KEY") else False
_PAUSED = False
_BOT_START_TS = time.time()
_STATE_LOCK = Lock()

# 各時框獨立自動下單路由開關
AUTO_TRADE: Dict[str, bool] = {
    "15m": True,
    "30m": True,
    "1H": True,
    "4H": True
}

# API 基本節點網址
OKX_BASE = "https://www.okx.com"
CONA_BASE = "https://api.coinalyze.net/v1"
DC_BASE = "https://discord.com/api/v10"

# ══════════════════════════════════════════════════════════════════════════════
#  完整 40+ 核心追蹤幣種字典對照表 (SYMBOLS MAPPING)
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
#  QQE MOD 核心固定數學常數
# ══════════════════════════════════════════════════════════════════════════════
QQE_RSI = 6
QQE_SF = 5
QQE_FACTOR_P = 3.0
QQE_FACTOR_S = 1.61
QQE_THRESHOLD = 3

ADX_THR = 25
MAX_SL = 0.12
FUNDING_LONG_MAX = 0.0001
FUNDING_SHORT_MIN = -0.0001
CVD_WINDOW = 3
BEAR_MIN_BARS = 20

# 🌟 全局變數：用於追蹤 Discord 歷史最高訊息 ID，防重複處理
_dc_last_msg_id = "0"

# ══════════════════════════════════════════════════════════════════════════════
#  多時框全自動路由最佳化動態參數對照表 (PRODUCTION CONFIG MAPPINGS)
# ══════════════════════════════════════════════════════════════════════════════
BEST_PARAMS: Dict[str, Dict[str, Any]] = {
    "15m_long": {
        "tp1_mult": 1.725, "tp2_intraday_mult": 1.8, "tp2_swing_mult": 1.8,
        "sl_atr_buffer": 0.01, "structure_lookback": 28, "exit_mode": "fixed"
    },
    "15m_short": {
        "tp1_mult": 2.0, "tp2_intraday_mult": 3.2, "tp2_swing_mult": 3.2,
        "sl_atr_buffer": 0.08, "structure_lookback": 20, "exit_mode": "fixed"
    },
    "30m_long": {
        "tp1_mult": 1.725, "tp2_intraday_mult": 1.8, "tp2_swing_mult": 1.8,
        "sl_atr_buffer": 0.05, "structure_lookback": 10, "exit_mode": "fixed"
    },
    "30m_short": {
        "tp1_mult": 2.0, "tp2_intraday_mult": 3.2, "tp2_swing_mult": 3.2,
        "sl_atr_buffer": 0.01, "structure_lookback": 10, "exit_mode": "trailing"
    },
    "1H_long": {
        "tp1_mult": 1.725, "tp2_intraday_mult": 2.5, "tp2_swing_mult": 2.5,
        "sl_atr_buffer": 0.15, "structure_lookback": 10, "exit_mode": "fixed"
    },
    "1H_short": {
        "tp1_mult": 2.0, "tp2_intraday_mult": 4.0, "tp2_swing_mult": 4.0,
        "sl_atr_buffer": 0.08, "structure_lookback": 20, "exit_mode": "fixed"
    },
    "4H_long": {
        "tp1_mult": 1.725, "tp2_intraday_mult": 2.5, "tp2_swing_mult": 2.5,
        "sl_atr_buffer": 0.03, "structure_lookback": 10, "exit_mode": "trailing"
    },
    "4H_short": {
        "tp1_mult": 2.0, "tp2_intraday_mult": 4.0, "tp2_swing_mult": 4.0,
        "sl_atr_buffer": 0.05, "structure_lookback": 30, "exit_mode": "fixed"
    },
}

def get_params(tf: str, side: str, base_dir: str = ".") -> Dict[str, Any]:
    """ 精確抓取指定時框與多空方向的最佳化回測因子參數 """
    trade_keys = {"tp1_mult", "tp2_intraday_mult", "tp2_swing_mult", "sl_atr_buffer", "structure_lookback", "exit_mode"}
    paths = [
        os.path.join(base_dir, f"best_params_{tf.lower()}_{side}.json"),
        os.path.join(base_dir, "final_params_all.json")
    ]
    for fname in paths:
        if os.path.exists(fname):
            try:
                with open(fname, encoding="utf-8") as f:
                    raw = json.load(f)
                data = raw.get(f"{tf}_{side}", raw.get("params", raw))
                extracted = {k: v for k, v in data.items() if k in trade_keys}
                if len(extracted) >= 4:
                    base = BEST_PARAMS.get(f"{tf}_{side}", {}).copy()
                    base.update(extracted)
                    return base
            except:
                pass
    return BEST_PARAMS.get(f"{tf}_{side}", {
        "tp1_mult": 1.7, "tp2_intraday_mult": 1.8, "tp2_swing_mult": 2.5,
        "sl_atr_buffer": 0.08, "structure_lookback": 20, "exit_mode": "fixed"
    }).copy()

# ══════════════════════════════════════════════════════════════════════════════
#  內部記帳監控狀態管理 (STATE MANAGEMENT)
# ══════════════════════════════════════════════════════════════════════════════
pending_orders: Dict[str, Dict[str, Any]] = {}

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
#  通知元件模組 (DISCORD & TELEGRAM NOTIFICATIONS)
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
        requests.post(url, json={"chat_id": TG_CHAT_ID, "text": text, "parse_mode": "Markdown"}, timeout=10)
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
        r = requests.post(f"{DC_BASE}/channels/{DISCORD_CHANNEL_ID}/messages", headers=_dc_headers(), json=payload, timeout=10)
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
        requests.patch(url, headers=_dc_headers(), json={"content": new_text, "components": []}, timeout=10)
    except Exception as e:
        print(f"  [Embed 狀態更替失敗] {e}")

def create_interactive_signal(sig: Dict[str, Any], symbol: str, tf: str, cvd_ok: bool) -> str:
    """ 建置完全體交互控制台卡片，整合止損比率、盈虧比與手動掛單快取 """
    side_emoji = "🟢" if sig["side"] == "long" else "🔴"
    dir_name = "多頭趨勢進場" if sig["side"] == "long" else "空頭趨勢進場"
    swing_tag = "📐 趨勢波段追蹤" if sig["is_swing"] else "⚡ 短線日內反彈"
    cvd_tag = "CVD ✅ 動能同步確認" if cvd_ok else "CVD ⚠️ 量能背離過濾"
    card_color = 0x2ecc71 if sig["side"] == "long" else "0xe74c3c"

    tw_time = datetime.fromisoformat(sig["time"].replace("Z", "").replace("+00:00", "")) + timedelta(hours=8)
    coin_name = symbol.split("/")[0]
    unique_callback_key = f"sykes_{coin_name.lower()}_{tf}_{sig['side']}_{int(time.time())}"

    # 寫入待核准交易訂單池快取
    pending_orders[unique_callback_key] = {
        "symbol": OKX_SWAP.get(symbol, symbol),
        "direction": sig["side"],
        "entry": sig["entry"],
        "sl": sig["sl"],
        "tp1": sig["tp1"],
        "tp2": sig["tp2"]
    }

    embed_payload = {
        "title": f"{side_emoji} {coin_name} [{tf} - {dir_name}]",
        "description": f"**環境特徵:** {swing_tag} | {cvd_tag}",
        "color": int(card_color, 16) if isinstance(card_color, str) else card_color,
        "fields": [
            {"name": "觸發時間 (TST)", "value": tw_time.strftime("%Y/%m/%d %H:%M:%S"), "inline": True},
            {"name": "ATR 當前波動", "value": f"`{sig['atr']}`", "inline": True},
            {"name": "離場機制", "value": f"`{sig['exit_mode']}`", "inline": True},
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
#  OKX 底層 REST API 原始簽章與行情擷取 (EXCHANGE CONNECTOR)
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

    # 依時間戳由舊到新排序
    sorted_candles = sorted(raw_candles, key=lambda x: int(x[0]))

    df = pd.DataFrame(sorted_candles, columns=["ts", "open", "high", "low", "close", "vol", "volCcy", "volCcyQuote", "confirm"])
    for col in ["open", "high", "low", "close", "vol"]:
        df[col] = df[col].astype(float)

    df["ts"] = pd.to_datetime(df["ts"].astype(np.int64), unit="ms", utc=True)
    df.set_index("ts", inplace=True)
    # 去除當前未收盤的 K 棒，確保訊號絕對不會發生漂移 (Non-Repainting)
    return df.iloc[:-1]

def fetch_current_funding_rate(swap_id: str) -> float:
    """ 擷取指定永續合約商品當前秒數之資金費率 """
    data_list = _fetch_okx_public_data("/api/v5/public/funding-rate", {"instId": swap_id})
    if data_list:
        return float(data_list[0].get("fundingRate", 0.0))
    return 0.0

# ══════════════════════════════════════════════════════════════════════════════
#  COINALYZE 大數據特徵過濾引擎 (CVD + OI ANALYSIS)
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
#  OKX 實盤風控倉位自動計算與分批委託鏈 (ORDER EXECUTION)
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

def execute_okx_trade_pipeline(symbol_id: str, trade_side: str, entry_price: float, stop_loss: float, target_tp1: float, target_tp2: float):
    """ 實盤訂單路由模組：整合動態槓桿、精密合約張數轉換、市價與限價單組合 """
    global _LIVE_MODE, MAX_LEVERAGE, MARGIN_PCT
    if not _LIVE_MODE:
        dc_log(f"📝 [紙交易通知] 商品 {symbol_id} 方向 {trade_side} 通過安全審核，當前處於 Paper 模擬模式，跳過交易所劃轉。")
        return

    try:
        ex = _initialize_ccxt_client()
        ex.load_markets()

        # 1. 安全資金配置檢查
        balance_data = ex.fetch_balance()
        available_usdt = float(balance_data.get("USDT", {}).get("free", 0.0))
        if available_usdt <= 5.0:
            dc_log(f"⚠️ **實盤交易中斷**: 交易所可用保證金不足 ({available_usdt:.2f} USDT)，無法建立新倉位。")
            return

        allocated_margin = available_usdt * (MARGIN_PCT / 100.0)
        ticker_info = ex.fetch_ticker(symbol_id)
        current_market_price = float(ticker_info.get("last", entry_price))

        # 2. 精準風控防護：動態計算防禦槓桿
        sl_distance_percentage = (abs(current_market_price - stop_loss) / current_market_price) * 100.0
        if sl_distance_percentage <= 0.01:
            dc_log("⚠️ **風控異常**: 結構止損間距過小，自動拒絕下單以防爆倉。")
            return

        calculated_leverage = max(1, min(int(100.0 / sl_distance_percentage), MAX_LEVERAGE))

        market_structure = ex.market(symbol_id)
        amount_precision = int(market_structure.get("precision", {}).get("amount", 0) or 0)
        contract_size = float(market_structure.get("contractSize", 1.0) or 1.0)

        # 計算名義價值與下單張數
        calculated_nominal_value = allocated_margin * calculated_leverage
        raw_order_amount = calculated_nominal_value / (current_market_price * contract_size)

        # 根據交易所最小精度進行安全收斂
        if amount_precision == 0:
            final_order_amount = max(1, int(raw_order_amount))
            split_half_amount = max(1, int(final_order_amount // 2))
        else:
            final_order_amount = max(round(1.0 / contract_size, amount_precision), round(raw_order_amount, amount_precision))
            split_half_amount = round(final_order_amount / 2.0, amount_precision)

        # 3. 交易所持倉模式配置
        try:
            ex.set_leverage(calculated_leverage, symbol_id, params={"posSide": trade_side})
        except:
            pass  # 防止重複調整發生警告中斷

        dc_log(f"💰 **資金動態分配中**\n可用餘額: {available_usdt:.2f} U | 配置保證金: {allocated_margin:.2f} U | 安全計算槓桿: {calculated_leverage}x\n總名義價值: {calculated_nominal_value:.2f} U | 總計量張數: {final_order_amount}")

        is_buy = (trade_side == "long")
        entry_action = "buy" if is_buy else "sell"
        exit_action = "sell" if is_buy else "buy"

        # 4. 發送市價單進場
        entry_order = ex.create_market_order(
            symbol=symbol_id,
            side=entry_action,
            amount=final_order_amount,
            params={"posSide": trade_side, "tdMode": "isolated"}
        )
        executed_average_price = entry_order.get("average", current_market_price) or current_market_price

        execution_report = [
            f"🚀 **賽克斯實盤下單鏈成功發動**",
            f"商品代號: `{symbol_id}` | 交易方向: `{'做多 LONG' if is_buy else '做空 SHORT'}` | 配置槓桿: `{calculated_leverage}x`",
            f"交易所實際成交均價: `{executed_average_price}`"
        ]

        # 5. 掛載全倉市價止損觸發單 (Stop Loss)
        try:
            ex.create_order(
                symbol=symbol_id,
                type="market",
                side=exit_action,
                amount=final_order_amount,
                price=stop_loss,
                params={
                    "stopLoss": {"triggerPrice": str(stop_loss), "orderPrice": "-1"},
                    "reduceOnly": True,
                    "posSide": trade_side
                }
            )
            execution_report.append(f"🛑 條件止損委託已錨定: `{stop_loss}`")
        except Exception as sle:
            execution_report.append(f"⚠️ 止損單掛載失敗: {sle}")

        # 6. 分批止盈限價單掛單 (Take Profit 1 & 2)
        try:
            ex.create_order(
                symbol=symbol_id,
                type="limit",
                side=exit_action,
                amount=split_half_amount,
                price=target_tp1,
                params={"posSide": trade_side, "tdMode": "isolated", "reduceOnly": True}
            )
            execution_report.append(f"🎯 第一獲利目標限價單掛置 (50%): `{target_tp1}`")
        except Exception as tp1e:
            execution_report.append(f"⚠️ TP1委託失敗: {tp1e}")

        try:
            # 剩餘的張數歸入第二目標
            remainder_amount = final_order_amount - split_half_amount
            if amount_precision == 0:
                remainder_amount = int(remainder_amount)
            else:
                remainder_amount = round(remainder_amount, amount_precision)

            ex.create_order(
                symbol=symbol_id,
                type="limit",
                side=exit_action,
                amount=remainder_amount,
                price=target_tp2,
                params={"posSide": trade_side, "tdMode": "isolated", "reduceOnly": True}
            )
            execution_report.append(f"🌕 第二終點目標限價單掛置 (50%): `{target_tp2}`")
        except Exception as tp2e:
            execution_report.append(f"⚠️ TP2委託失敗: {tp2e}")

        dc_log("\n".join(execution_report))

    except Exception as general_error:
        dc_log(f"❌ **交易所執行鏈嚴重崩潰**: {general_error}")

# ══════════════════════════════════════════════════════════════════════════════
#  複雜技術指標庫算力模組 (TECHNICAL INDICATORS MATHEMATICS)
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

    trailing_line_value = 0.0
    trailing_buffer_list = []

    for idx in range(len(rsi_smoothed_ma)):
        current_ma_val = rsi_smoothed_ma.iloc[idx]
        if idx == 0:
            trailing_line_value = current_ma_val
        else:
            previous_trailing_value = trailing_line_value
            if current_ma_val < previous_trailing_value:
                trailing_line_value = current_ma_val + dar_trailing_band.iloc[idx]
                if rsi_smoothed_ma.iloc[idx - 1] < previous_trailing_value and trailing_line_value > previous_trailing_value:
                    trailing_line_value = previous_trailing_value
            else:
                trailing_line_value = current_ma_val - dar_trailing_band.iloc[idx]
                if rsi_smoothed_ma.iloc[idx - 1] > previous_trailing_value and trailing_line_value < previous_trailing_value:
                    trailing_line_value = previous_trailing_value
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
    return true_range.rolling(window=atr_period).mean()

def calculate_adx_trend_strength(data_df: pd.DataFrame, lookback_n: int = 14) -> Tuple[pd.Series, pd.Series, pd.Series]:
    """ 計算趨向指標系統 (ADX, +DI, -DI) 以精準確認趨勢動能爆發 """
    high_s = data_df["high"]
    low_s = data_df["low"]
    close_s = data_df["close"]

    tr = pd.concat([
        high_s - low_s,
        (high_s - close_s.shift(1)).abs(),
        (low_s - close_s.shift(1)).abs()
    ], axis=1).max(axis=1)

    atr_smooth = tr.rolling(lookback_n).mean()

    plus_move = high_s.diff()
    minus_move = -low_s.diff()

    plus_dm = np.where((plus_move > minus_move) & (plus_move > 0.0), plus_move, 0.0)
    minus_dm = np.where((minus_move > plus_move) & (minus_move > 0.0), minus_move, 0.0)

    plus_di = 100.0 * pd.Series(plus_dm, index=data_df.index).rolling(lookback_n).mean() / atr_smooth.replace(0.0, 1e-9)
    minus_di = 100.0 * pd.Series(minus_dm, index=data_df.index).rolling(lookback_n).mean() / atr_smooth.replace(0.0, 1e-9)

    dx_metric = 100.0 * (plus_di - minus_di).abs() / (plus_di + minus_di).replace(0.0, 1e-9)
    adx_line = dx_metric.rolling(lookback_n).mean()

    return adx_line, plus_di, minus_di

# ══════════════════════════════════════════════════════════════════════════════
#  生產線核心引擎主體 (PRODUCTION TELEMETRY ENGINE)
# ══════════════════════════════════════════════════════════════════════════════
class SykesProductionCore:
    def __init__(self):
        self.cooldown_tracker: Dict[str, float] = {}
        self.consecutive_loss_counter: Dict[str, int] = {}
        self.fuse_melt_timer: Dict[str, datetime] = {}
        self.active_positions_ledger: Dict[str, PaperPosition] = {}

        # 實例化所有交易配對在各個時框的獨立內部記帳卡片
        for s in SYMBOLS.values():
            for t in TIMEFRAMES:
                self.active_positions_ledger[f"{s}_{t}"] = PaperPosition()

    def scan_and_process_market(self, asset_symbol: str, target_tf: str):
        """ 核心策略偵測主邏輯：封裝多空交叉判斷、熊市過濾計數、與費率風控 """
        current_utc_now = datetime.now(timezone.utc)
        composite_key = f"{asset_symbol}_{target_tf}"

        # 1. 斷路器與熔斷冷卻檢測
        with _STATE_LOCK:
            if composite_key in self.fuse_melt_timer:
                if current_utc_now < self.fuse_melt_timer[composite_key]:
                    return
            if self.cooldown_tracker.get(composite_key, 0.0) > time.time():
                return

        okx_instrument_id = OKX_SWAP.get(asset_symbol, asset_symbol)
        market_df = fetch_market_candles(okx_instrument_id, target_tf, WARMUP)
        if market_df.empty or len(market_df) < 250:
            return

        # 2. 開啟持倉動態監控大腦更新 (追蹤移動止損與盈虧達標狀態)
        pos_instance = self.active_positions_ledger[composite_key]
        last_close_price = market_df["close"].iloc[-1]

        if pos_instance.open:
            self._update_and_track_active_position(asset_symbol, target_tf, pos_instance, last_close_price, market_df)
            if pos_instance.open:
                return  # 如果持倉依然有效，跳過新訊號生成，防止重疊開倉

        # 3. 技術指標全面並行演算
        fast_rsi_ma, fast_qqe_band = calculate_full_qqe_mod(market_df, QQE_RSI, QQE_SF, QQE_FACTOR_P)
        slow_rsi_ma, slow_qqe_band = calculate_full_qqe_mod(market_df, QQE_RSI, QQE_SF, QQE_FACTOR_S)

        latest_atr_value = calculate_average_true_range(market_df, 14).iloc[-1]
        adx_trend, plus_di, minus_di = calculate_adx_trend_strength(market_df, 14)

        market_df["ema200"] = market_df["close"].ewm(span=200, adjust=False).mean()
        latest_ema200 = market_df["ema200"].iloc[-1]

        # 4. 精密 QQE MOD 交叉判斷
        is_fast_cross_up = (fast_rsi_ma.iloc[-2] <= fast_qqe_band.iloc[-2]) and (fast_rsi_ma.iloc[-1] > fast_qqe_band.iloc[-1])
        is_fast_cross_down = (fast_rsi_ma.iloc[-2] >= fast_qqe_band.iloc[-2]) and (fast_rsi_ma.iloc[-1] < fast_qqe_band.iloc[-1])
        is_slow_momentum_long = slow_rsi_ma.iloc[-1] > slow_qqe_band.iloc[-1]

        determined_side = ""
        if is_fast_cross_up and is_slow_momentum_long and (fast_rsi_ma.iloc[-1] > 50.0):
            # 多頭過濾網：強勢多頭動能確認
            if adx_trend.iloc[-1] >= ADX_THR and plus_di.iloc[-1] > minus_di.iloc[-1]:
                determined_side = "long"
        elif is_fast_cross_down and not is_slow_momentum_long and (fast_rsi_ma.iloc[-1] < 40.0):
            # 空頭過濾網：強勢空頭動能確認
            if adx_trend.iloc[-1] >= ADX_THR and minus_di.iloc[-1] > plus_di.iloc[-1]:
                determined_side = "short"

        if not determined_side:
            return

        # 5. 熊市深層環境與 K 棒計數過濾機制 (200EMA 大腦過濾)
        is_below_ema200 = (last_close_price < latest_ema200)
        if is_below_ema200 and determined_side == "long":
            # 計算連續低於 EMA200 的 K 棒數量走勢
            bear_mask = (market_df["close"] < market_df["ema200"]).astype(int)
            continuous_bear_counts = bear_mask.groupby((bear_mask != bear_mask.shift()).cumsum()).cumsum()
            # 如果處於熊市但壓制時間不夠久，拒絕盲目抄底做多
            if continuous_bear_counts.iloc[-1] < BEAR_MIN_BARS:
                return

        # 6. 資金費率風控閘門
        funding_rate = fetch_current_funding_rate(okx_instrument_id)
        if determined_side == "long" and funding_rate > FUNDING_LONG_MAX:
            return
        if determined_side == "short" and funding_rate < FUNDING_SHORT_MIN:
            return

        # 7. Coinalyze 雙重現貨/永續 CVD 趨勢健康度覆核
        is_cvd_validated = self._validate_cvd_momentum(asset_symbol, target_tf, determined_side, market_df)

        # 8. 調閱最佳化回測字典計算止損與分批止盈線
        param_set = get_params(target_tf, determined_side)
        lookback_window = int(param_set.get("structure_lookback", 20))
        recent_sub_df = market_df.iloc[-lookback_window:]

        if determined_side == "long":
            lowest_support = recent_sub_df["low"].min()
            calculated_sl = lowest_support - (latest_atr_value * param_set.get("sl_atr_buffer", 0.05))
            # 風控防穿底保護
            calculated_sl = max(calculated_sl, last_close_price * (1.0 - MAX_SL))
            if calculated_sl >= last_close_price:
                calculated_sl = last_close_price - (latest_atr_value * 0.5)

            risk_pct = (last_close_price - calculated_sl) / last_close_price
            tp1_target = last_close_price + (last_close_price - calculated_sl) * param_set.get("tp1_mult", 1.5)

            # 區分波段或短線反彈調度 TP2
            is_swing_trade = (last_close_price > latest_ema200)
            tp2_multiplier = param_set.get("tp2_swing_mult" if is_swing_trade else "tp2_intraday_mult", 2.0)
            tp2_target = last_close_price + (last_close_price - calculated_sl) * tp2_multiplier
        else:
            highest_resistance = recent_sub_df["high"].max()
            calculated_sl = highest_resistance + (latest_atr_value * param_set.get("sl_atr_buffer", 0.05))
            calculated_sl = min(calculated_sl, last_close_price * (1.0 + MAX_SL))
            if calculated_sl <= last_close_price:
                calculated_sl = last_close_price + (latest_atr_value * 0.5)

            risk_pct = (calculated_sl - last_close_price) / last_close_price
            tp1_target = last_close_price - (calculated_sl - last_close_price) * param_set.get("tp1_mult", 1.5)

            is_swing_trade = (last_close_price < latest_ema200)
            tp2_multiplier = param_set.get("tp2_swing_mult" if is_swing_trade else "tp2_intraday_mult", 2.0)
            tp2_target = last_close_price - (calculated_sl - last_close_price) * tp2_multiplier

        # 9. 封裝訊號物件並推進快取與 UI 生成
        signal_packet = {
            "side": determined_side,
            "entry": round(last_close_price, 6),
            "sl": round(calculated_sl, 6),
            "tp1": round(tp1_target, 6),
            "tp2": round(tp2_target, 6),
            "atr": round(latest_atr_value, 4),
            "risk_pct": risk_pct * 100.0,
            "exit_mode": param_set.get("exit_mode", "fixed"),
            "is_swing": is_swing_trade,
            "rr1": param_set.get("tp1_mult", 1.5),
            "rr2": tp2_multiplier,
            "time": current_utc_now.isoformat() + "Z"
        }

        with _STATE_LOCK:
            self.cooldown_tracker[composite_key] = time.time() + SIGNAL_COOLDOWN

        callback_id_key = create_interactive_signal(signal_packet, asset_symbol, target_tf, is_cvd_validated)

        # 開啟內部記帳卡片監控
        pos_instance.open = True
        pos_instance.side = determined_side
        pos_instance.entry = signal_packet["entry"]
        pos_instance.sl = signal_packet["sl"]
        pos_instance.tp1 = signal_packet["tp1"]
        pos_instance.tp2 = signal_packet["tp2"]
        pos_instance.tp1_hit = False
        pos_instance.exit_mode = signal_packet["exit_mode"]

        # 自動下單路由判斷
        if AUTO_TRADE.get(target_tf, False):
            pending_orders.pop(callback_id_key, None)
            Thread(
                target=execute_okx_trade_pipeline,
                args=(okx_instrument_id, determined_side, signal_packet["entry"], signal_packet["sl"], signal_packet["tp1"], signal_packet["tp2"]),
                daemon=True
            ).start()

    def _validate_cvd_momentum(self, symbol: str, tf: str, side: str, df: pd.DataFrame) -> bool:
        spot_code = CONA_SPOT.get(symbol)
        perp_code = CONA_PERP.get(symbol)
        if not spot_code or not perp_code:
            return True
        try:
            end_ms = int(df.index[-1].timestamp() * 1000)
            start_ms = end_ms - (BAR_SECONDS[tf] * CVD_WINDOW * 1000)
            interval_str = BAR_TO_CONA.get(tf, "15min")

            spot_cvd = calculate_cumulative_volume_delta(spot_code, interval_str, start_ms, end_ms)
            perp_cvd = calculate_cumulative_volume_delta(perp_code, interval_str, start_ms, end_ms)

            if len(spot_cvd) < 2 or len(perp_cvd) < 2:
                return True

            is_spot_volume_up = spot_cvd.iloc[-1] > spot_cvd.iloc[0]
            is_perp_volume_up = perp_cvd.iloc[-1] > perp_cvd.iloc[0]

            if side == "long":
                return (is_spot_volume_up and is_perp_volume_up)
            else:
                return (not is_spot_volume_up and not is_perp_volume_up)
        except:
            return True

    def _update_and_track_active_position(self, symbol: str, tf: str, pos: PaperPosition, cur_px: float, df: pd.DataFrame):
        """ 內部持倉更新大腦：精確追蹤保證金鎖定移動止損與三階段出場邏輯 """
        composite_key = f"{symbol}_{tf}"
        coin_tag = symbol.split("/")[0]

        if pos.side == "long":
            # 1. 檢測結構止損是否被刺穿
            if df["low"].iloc[-1] <= pos.sl:
                exit_msg = f"🛑 **持倉離場通知**\n商品: {coin_tag} [{tf} 多頭] | 觸及安全結構止損線: `{pos.sl}`。此單全數平倉。"
                dc_log(exit_msg)
                tg_log(exit_msg)
                self._process_loss_event(composite_key)
                pos.open = False
            # 2. 檢測第一目標獲利點是否觸及
            elif not pos.tp1_hit and df["high"].iloc[-1] >= pos.tp1:
                pos.tp1_hit = True
                exit_msg = f"🎯 **獲利通告：第一目標價 TP1 `{pos.tp1}` 順利達標！**\n商品: {coin_tag} [{tf} 多頭] 已自動平倉 50% 鎖定獲利。\n🛡️ **保證金保護啟用**: 止損點強制同步推移至進場成本價 `{pos.entry}`，立於不敗之地。"
                dc_log(exit_msg)
                pos.sl = pos.entry  # 防禦性成本價鎖定
            # 3. 檢測終點第二獲利點
            elif pos.tp1_hit and df["high"].iloc[-1] >= pos.tp2:
                exit_msg = f"🌕 **完美波段終點！第二目標價 TP2 `{pos.tp2}` 完美全數止盈落袋！**\n商品: {coin_tag} [{tf} 多頭] 完整交易鏈落幕。"
                dc_log(exit_msg)
                tg_log(exit_msg)
                with _STATE_LOCK:
                    self.consecutive_loss_counter[composite_key] = 0
                pos.open = False
            # 4. 移動追蹤止損邏輯 (Trailing Exit Mode)
            elif pos.exit_mode == "trailing" and pos.tp1_hit:
                previous_bar_low = df["low"].iloc[-2]
                if previous_bar_low > pos.sl:
                    pos.sl = previous_bar_low
        else:
            if df["high"].iloc[-1] >= pos.sl:
                exit_msg = f"🛑 **持倉離場通知**\n商品: {coin_tag} [{tf} 空頭] | 觸及安全結構止損線: `{pos.sl}`。此單全數平倉。"
                dc_log(exit_msg)
                tg_log(exit_msg)
                self._process_loss_event(composite_key)
                pos.open = False
            elif not pos.tp1_hit and df["low"].iloc[-1] <= pos.tp1:
                pos.tp1_hit = True
                exit_msg = f"🎯 **獲利通告：第一目標價 TP1 `{pos.tp1}` 順利達標！**\n商品: {coin_tag} [{tf} 空頭] 已自動平倉 50% 鎖定獲利。\n🛡️ **保證金保護啟用**: 止損點強制同步推移至進場成本價 `{pos.entry}`，立於不敗之地。"
                dc_log(exit_msg)
                pos.sl = pos.entry
            elif pos.tp1_hit and df["low"].iloc[-1] <= pos.tp2:
                exit_msg = f"🌕 **完美波段終點！第二目標價 TP2 `{pos.tp2}` 完美全數止盈落袋！**\n商品: {coin_tag} [{tf} 空頭] 完整交易鏈落幕。"
                dc_log(exit_msg)
                tg_log(exit_msg)
                with _STATE_LOCK:
                    self.consecutive_loss_counter[composite_key] = 0
                pos.open = False
            elif pos.exit_mode == "trailing" and pos.tp1_hit:
                previous_bar_high = df["high"].iloc[-2]
                if previous_bar_high < pos.sl:
                    pos.sl = previous_bar_high

    def _process_loss_event(self, composite_key: str):
        """ 處理連續虧損熔斷邏輯 """
        with _STATE_LOCK:
            current_losses = self.consecutive_loss_counter.get(composite_key, 0) + 1
            self.consecutive_loss_counter[composite_key] = current_losses

            if current_losses >= MAX_CONSEC_LOSS:
                resume_time = datetime.now(timezone.utc) + timedelta(hours=PAUSE_HOURS)
                self.fuse_melt_timer[composite_key] = resume_time
                parts = composite_key.split("_")

                melt_msg = (
                    f"⏸ **系統熔斷核心警報**\n"
                    f"商品時框: `{parts[0]}` 在 `[{parts[1]}]` 連續發生 `{MAX_CONSEC_LOSS}` 次虧損止損！\n"
                    f"啟動冷卻安全保護，該商品時框策略將暫停運作 {PAUSE_HOURS} 小時。\n"
                    f"預計解鎖恢復時間: `{(resume_time + timedelta(hours=8)).strftime('%Y/%m/%d %H:%M:%S')}` (TST)"
                )
                dc_log(melt_msg)
                tg_log(melt_msg)

# ══════════════════════════════════════════════════════════════════════════════
#  FLASK WEBHOOK 交互控制中心 (DISCORD INTERACTIONS ENDPOINT)
# ══════════════════════════════════════════════════════════════════════════════
app = Flask(__name__)

@app.route("/")
def system_health_status():
    mode_string = "🔴 實盤運作中" if _LIVE_MODE else "📝 Paper 監控中"
    return f"<h3>賽克斯全模組多時框交易核心 v4 運行指標正常</h3>工作模式: {mode_string}<br>監控池體積: {len(SYMBOLS)} 項目"

@app.route("/discord-interactions", methods=["POST"])
def handle_discord_interaction_webhook():
    """ 實時處理來自 Discord 按鈕點擊事件的安全性 Ed25519 簽章覆核與下單分配 """
    req_signature = request.headers.get("X-Signature-Ed25519", "")
    req_timestamp = request.headers.get("X-Signature-Timestamp", "")
    raw_payload_bytes = request.data

    # 進行官方規格之 Ed25519 加密驗證
    try:
        from nacl.signing import VerifyKey
        verification_key = VerifyKey(bytes.fromhex(DISCORD_PUBLIC_KEY))
        verification_key.verify(req_timestamp.encode() + raw_payload_bytes, bytes.fromhex(req_signature))
    except:
        return jsonify({"error": "Unauthorized cryptographic signature"}), 401

    interaction_json = request.get_json() or {}

    # 處理 Discord 的 Ping 探針確認
    if interaction_json.get("type") == 1:
        return jsonify({"type": 1})

    # 處理 UI 元件點擊事件 (Component Type = 3)
    if interaction_json.get("type") == 3:
        clicked_custom_id = interaction_json.get("data", {}).get("custom_id", "")

        if clicked_custom_id.startswith("confirm_"):
            target_key = clicked_custom_id[8:]
            matched_order = pending_orders.pop(target_key, None)

            if matched_order:
                # 派發執行線程，防止 Webhook 逾時
                Thread(
                    target=execute_okx_trade_pipeline,
                    args=(
                        matched_order["symbol"], matched_order["direction"],
                        matched_order.get("entry", 0.0), matched_order["sl"],
                        matched_order["tp1"], matched_order["tp2"]
                    ),
                    daemon=True
                ).start()

                if matched_order.get("msg_id"):
                    Thread(target=dc_embed_edit, args=(matched_order["msg_id"], "✅ **已核准下單**: 指令已安全發送至 OKX 交易所交易鏈..."), daemon=True).start()
                return jsonify({"type": 6})  # 響應不留痕跡
            else:
                return jsonify({"type": 4, "data": {"content": "⚠️ **操作失敗**: 該訊號快取已失效、過期或已被自動下單模組移出佇列。", "flags": 64}})

        elif clicked_custom_id.startswith("skip_"):
            target_key = clicked_custom_id[5:]
            matched_order = pending_orders.pop(target_key, None)
            if matched_order and matched_order.get("msg_id"):
                Thread(target=dc_embed_edit, args=(matched_order["msg_id"], "❌ **手動棄單**: 指令已被交易員拒絕，放棄該進場點。"), daemon=True).start()
            return jsonify({"type": 6})

    return jsonify({"type": 1})

# ══════════════════════════════════════════════════════════════════════════════
#  DISCORD 控制台 10 條遠端指令監聽輪詢 (COMMAND POLLER)
# ══════════════════════════════════════════════════════════════════════════════
def execute_console_command(command_text: str):
    """ 精確處理 10 條核心控制指令之字串解析與底層大腦定向更替 """
    global _LIVE_MODE, MAX_LEVERAGE, MARGIN_PCT, _BOT_START_TS, _bot_ref, _PAUSED
    command_text = command_text.strip()
    tokens = command_text.split()
    if not tokens:
        return

    primary_cmd = tokens[0].lower()

    if primary_cmd == "!setrisk" and len(tokens) >= 2:
        try:
            val = float(tokens[1])
            if 0.1 <= val <= 100.0:
                with _STATE_LOCK:
                    MARGIN_PCT = val
                dc_log(f"⚙️ **風控變更成功**: 單筆交易配置資金比重已修訂為 **{val}%** 的可用可用餘額。")
        except:
            pass

    elif primary_cmd == "!setmaxlev" and len(tokens) >= 2:
        try:
            val = int(float(tokens[1]))
            if 1 <= val <= 125:
                with _STATE_LOCK:
                    MAX_LEVERAGE = val
                dc_log(f"⚙️ **風控變更成功**: 最高安全槓桿上限已修正鎖定為 **{val}x**")
        except:
            pass

    elif primary_cmd == "!pause":
        with _STATE_LOCK:
            _PAUSED = True
        dc_log("⏸ **系統暫停通告**: 全時框商品掃描與下單路由已全面進入冬眠狀態。")

    elif primary_cmd == "!resume":
        with _STATE_LOCK:
            _PAUSED = False
        dc_log("▶️ **系統恢復通告**: 全自動雙軌策略監控核心已重新恢復上線運作。")

    elif primary_cmd == "!setlive":
        if OKX_API_KEY:
            with _STATE_LOCK:
                _LIVE_MODE = True
            dc_log("🔴 **最高防禦警報**: 系統工作模式正式切換為【實盤交易所直連交易】，將動用真實 USDT 資產建立合約持倉！")
        else:
            dc_log("⚠️ **切換失敗**: 檢測不到本地變數環境中的 OKX 帳戶私鑰金鑰！")

    elif primary_cmd == "!setpaper":
        with _STATE_LOCK:
            _LIVE_MODE = False
        dc_log("📝 **工作模式提示**: 系統已切換為【Paper虛擬記帳監控模式】。")

    elif primary_cmd == "!autostatus":
        status_report = ["📊 **時框自動下單分佈狀況**\n────────────────"]
        for k, v in AUTO_TRADE.items():
            status_report.append(f"時框 `{k}`: {'🟢 全自動即時下單已啟用' if v else '⛔ 訊號僅通知(手動按鈕)'}")
        dc_log("\n".join(status_report))

    elif primary_cmd == "!auto" and len(tokens) >= 3:
        target_tf_arg = tokens[1].upper()
        switch_state = (tokens[2].lower() == "on")

        if target_tf_arg == "ALL":
            for k in AUTO_TRADE:
                AUTO_TRADE[k] = switch_state
            dc_log(f"⚙️ **系統整合開關**: 所有時框的自動下單屬性已被全體定向設定為 **{'開啟' if switch_state else '關閉'}**")
        elif target_tf_arg in AUTO_TRADE:
            AUTO_TRADE[target_tf_arg] = switch_state
            dc_log(f"⚙️ **時框開關更替**: 指定時框 `{target_tf_arg}` 自動下單已變更為 **{'開啟' if switch_state else '關閉'}**")

    elif primary_cmd == "!status":
        elapsed_seconds = int(time.time() - _BOT_START_TS)
        hours_alive = elapsed_seconds // 3600
        minutes_alive = (elapsed_seconds % 3600) // 60

        work_mode = "🔴 實盤真金模式" if _LIVE_MODE else "📝 Paper 虛擬模擬"

        try:
            ex = _initialize_ccxt_client()
            bal_node = ex.fetch_balance()
            free_u = float(bal_node.get("USDT", {}).get("free", 0.0))
            total_u = float(bal_node.get("USDT", {}).get("total", 0.0))
            wallet_string = f"總權益: `{total_u:.2f}` U | 可用保證金: `{free_u:.2f}` U"
        except:
            wallet_string = "交易所權限無連結或私鑰異常 (N/A)"

        active_position_count = 0
        if _bot_ref:
            active_position_count = sum(1 for p in _bot_ref.active_positions_ledger.values() if p and p.open)

        metrics_block = (
            f"⚙️ **賽克斯生產級交易核心運行指標系統**\n"
            f"────────────────\n"
            f"大腦監控狀態: {'⏸ 系統冬眠暫停中' if _PAUSED else '▶️ 全時掃描正常運作中'}\n"
            f"目前工作特徵: **{work_mode}**\n"
            f"交易所資產帳本: {wallet_string}\n"
            f"單倉保證金配比: `{MARGIN_PCT}%` | 槓桿安全閾值上限: `{MAX_LEVERAGE}x`\n"
            f"當前內部追蹤持倉數: `{active_position_count}` 倉位\n"
            f"累計連續運作時間: `{hours_alive}` 小時 `{minutes_alive}` 分鐘\n"
            f"多維監控規模: `{len(SYMBOLS)}` 大核心主流幣種 | 4 大時框同時覆蓋"
        )
        dc_log(metrics_block)

    elif primary_cmd == "!help":
        help_menu = (
            f"📖 **控制台核心指令操作手冊**\n"
            f"────────────────\n"
            f"`!status` - 調閱當前大腦所有核心參數與交易所可用資產報表\n"
            f"`!setlive` / `!setpaper` - 無縫切換實盤直連下單或虛擬記帳監控\n"
            f"`!auto all on/off` - 總控制：全局開啟或關閉 4 大時框之全自動交易\n"
            f"`!auto [15m/30m/1H/4H] on/off` - 單獨隔離控制指定時框是否啟用自動實盤路由\n"
            f"`!autostatus` - 查看 4 大時框自動下單開關的詳細隔離分佈狀態\n"
            f"`!setrisk [數字]` - 調整每筆交易佔用交易所可用資產的百分比比重\n"
            f"`!setmaxlev [數字]` - 強制鎖定交易所最大安全槓桿允許倍數\n"
            f"`!pause` / `!resume` - 緊急暫停或重新激活交易核心掃描訊號"
        )
        dc_log(help_menu)

# ══════════════════════════════════════════════════════════════════════════════
#  🛠️ 核心修正：將遠端訊息抓取與 10 條遠端指令完美整合成單一 Try-Except 區塊 (Non-Repainting)
# ══════════════════════════════════════════════════════════════════════════════
def run_discord_command_poller_loop():
    """ 獨立背景線程：每 3 秒長輪詢 Discord 頻道文字，實時同步控制台指令 (完美修正語法縮排版) """
    global _dc_last_msg_id

    # 初始化：先抓一次最新訊息，作為起點，避免重啟時重複執行舊指令
    try:
        init_res = requests.get(
            f"{DC_BASE}/channels/{DISCORD_CHANNEL_ID}/messages", 
            headers=_dc_headers(), 
            params={"limit": 1}, 
            timeout=15
        ).json()
        if isinstance(init_res, list) and init_res:
            _dc_last_msg_id = init_res[0]["id"]
    except Exception as e:
        print(f"[-] 初始化 Discord Poller 失敗: {e}")

    while True:
        try:
            # 1. 抓取啟動時最新訊息，避免重複處理舊指令
            r = requests.get(
                f"{DC_BASE}/channels/{DISCORD_CHANNEL_ID}/messages",
                headers=_dc_headers(),
                params={"limit": 10},
                timeout=15
            )
            msgs = r.json()
            if isinstance(msgs, list) and msgs:
                msg = msgs[0]

                # 💡 鐵律 1：如果是機器人自己發的訊息，直接記為已讀，絕對不要執行！
                author = msg.get("author", {})
                if author.get("bot") is True or author.get("username") == "RobotAhaha":
                    _dc_last_msg_id = msg["id"]
                else:
                    # 💡 鐵律 2：如果是真人的訊息，才更新最高快取 ID
                    _dc_last_msg_id = msg["id"]
        except Exception as e:
            print(f"[-] 核心拉取異常，已自動略過進入 while True 區間: {e}")
            pass

        # 2. 進入實際的 while True 輪詢區間
        while True:
            try:
                r = requests.get(
                    f"{DC_BASE}/channels/{DISCORD_CHANNEL_ID}/messages",
                    headers=_dc_headers(),
                    params={"after": _dc_last_msg_id, "limit": 10},
                    timeout=15
                )
                msgs = r.json()
                if isinstance(msgs, list) and msgs:
                    # 依時間順序由舊到新排序處理
                    for msg in sorted(msgs, key=lambda m: m.get("id", "0")):
                        _dc_last_msg_id = msg["id"]

                        # 🛡️ 終極核心防線：只要發現發言者是 Bot，立刻忽略、直接跳過！
                        author = msg.get("author", {})
                        if author.get("bot") is True or author.get("username") == "RobotAhaha":
                            continue  # 🏃 🧱 跳過此訊息，繼續看下一條

                        # 解析文字指令
                        text = msg.get("content", "")
                        if text.startswith("!"):
                            execute_console_command(text)
            except Exception as loop_err:
                print(f"[-] Poller 內部輪詢發生輕微異常，自動跳過: {loop_err}")
                pass
            sleep(3)

# ══════════════════════════════════════════════════════════════════════════════
#  精密排程與主核心輪詢控制 (SCHEDULER LOOP)
# ══════════════════════════════════════════════════════════════════════════════
def synchronise_and_wait_next_candle() -> List[str]:
    """ 精密排程對齊模組：強制讓迴圈在 K 棒準確收盤後的第 5 秒整觸發，防秒差干擾 """
    utc_timestamp_now = datetime.now(timezone.utc).timestamp()

    # 計算各時框下一次收盤的精準秒數
    next_close_targets = {
        tf: (int(utc_timestamp_now / BAR_SECONDS[tf]) + 1) * BAR_SECONDS[tf] + 5
        for tf in TIMEFRAMES
    }

    earliest_trigger_timestamp = min(next_close_targets.values())

    while True:
        remaining_wait_seconds = earliest_trigger_timestamp - datetime.now(timezone.utc).timestamp() - 0.5
        if remaining_wait_seconds <= 0.0:
            break
        sleep(min(remaining_wait_seconds, 30.0))

    # 確認本次觸發究竟對齊了哪些時框
    double_check_ts = datetime.now(timezone.utc).timestamp()
    triggered_timeframes = []
    for tf in TIMEFRAMES:
        expected_boundary = earliest_trigger_timestamp - 5
        actual_boundary = int(double_check_ts / BAR_SECONDS[tf]) * BAR_SECONDS[tf]
        if abs(actual_boundary - expected_boundary) < 15:
            triggered_timeframes.append(tf)

    return triggered_timeframes if triggered_timeframes else [TIMEFRAMES[0]]

def run_strategy_scanning_loop():
    """ 交易核心主線程：執行 40+ 幣種與多時框的矩陣全天候輪詢 """
    global _bot_ref, _PAUSED
    _bot_ref = SykesProductionCore()

    start_alert = "🚀 **賽克斯全功能完全體智慧交易系統 v4 實盤部署完成**\n控制中樞已成功對齊 40+ 主流加密商品，開始進行 15m/30m/1H/4H 收盤矩陣輪詢機制..."
    dc_log(start_alert)
    tg_log(start_alert)

    while True:
        active_tfs_to_run = synchronise_and_wait_next_candle()

        if _PAUSED:
            continue

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
#  系統總入口點 (APPLICATION ENTRY POINT)
# ══════════════════════════════════════════════════════════════════════════════
_bot_ref: Optional[SykesProductionCore] = None

if __name__ == "__main__":
    # 1. 啟動 Web 伺服器線程處理 Discord Webhook
    Thread(target=run_embedded_web_server, daemon=True).start()

    # 2. 啟動遠端控制台指令輪詢監聽線程
    Thread(target=run_discord_command_poller_loop, daemon=True).start()

    # 3. 推進主線程進入無盡交易排程核心
    run_strategy_scanning_loop()