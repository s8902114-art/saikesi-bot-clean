#!/usr/bin/env python3

# -*- coding: utf-8 -*-

“””
賽克斯多時框全功能生產級交易系統 v4 (Production-Grade Multi-Timeframe System)
支援 40+ 幣種監控 | 15m/30m/1H/4H 四時框輪詢 | QQE MOD 雙軌交叉策略
CVD 累積成交量背離過濾 | OKX 實盤與模擬盤自動劃轉下單 | Discord 雙向按鈕控制台
“””

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

if hasattr(sys.stdout, “reconfigure”):
sys.stdout.reconfigure(encoding=“utf-8”, errors=“replace”)
else:
sys.stdout = io.TextIOWrapper(sys.stdout.buffer, encoding=“utf-8”, errors=“replace”)

# 自動檢查並安裝缺少的第三方套件

REQUIRED_PACKAGES = [“requests”, “pandas”, “numpy”, “ccxt”, “flask”, “pynacl”]
for pkg in REQUIRED_PACKAGES:
try:
**import**(pkg)
except ImportError:
print(f”[-] 偵測到缺少必要套件 {pkg}，正在進行背景靜態安裝…”)
subprocess.check_call([sys.executable, “-m”, “pip”, “install”, pkg, “-q”])

import requests
import numpy as np
import pandas as pd
import ccxt
from flask import Flask, request, jsonify

# ══════════════════════════════════════════════════════════════════════════════

# 核心全局配置與金鑰設定 (USER CONFIGURATION)

# ══════════════════════════════════════════════════════════════════════════════

COINALYZE_API_KEY = “82087740-b30d-479f-8846-5ffb51540b19”

# Discord Bot 配置

DISCORD_TOKEN = os.environ.get(“DISCORD_TOKEN”, “”)
DISCORD_CHANNEL_ID = os.environ.get(“DISCORD_CHANNEL_ID”, “1505971611042320616”)
DISCORD_PUBLIC_KEY = os.environ.get(“DISCORD_PUBLIC_KEY”, “79788628a845970d78c0d99d2e85505d9a306bae482459d33eaa8d0f84b6c6d4”)

# Telegram 備用通知管道

TG_BOT_TOKEN = os.environ.get(“TG_BOT_TOKEN”, “”)
TG_CHAT_ID = os.environ.get(“TG_CHAT_ID”, “”)

# OKX 交易所帳戶配置

OKX_API_KEY = os.environ.get(“OKX_API_KEY”, “”)
OKX_SECRET = os.environ.get("OKX_SECRET_KEY", "359300E99DD8870F8990CC698BC4F491")
OKX_PASSPHRASE = os.environ.get("OKX_PASSPHRASE", "Small5017714@")
OKX_DEMO = False  # 是否啟用 OKX 模擬盤交易環境

# ══════════════════════════════════════════════════════════════════════════════

# 策略風控常數與運行狀態機

# ══════════════════════════════════════════════════════════════════════════════

MAX_LEVERAGE = 100         # 系統最高安全槓桿限制
MARGIN_PCT = 10.0          # 單筆交易佔用總可用資金之百分比 (10.0 = 10%)
SIGNAL_COOLDOWN = 1800     # 同一商品商品相同時框的訊號冷卻時間 (秒)
MAX_CONSEC_LOSS = 3       # 最大連續虧損次數限制，達標後觸發熔斷
PAUSE_HOURS = 24           # 熔斷冷卻時間 (小時)

# 系統底層控制開關

_LIVE_MODE = True if os.environ.get(“OKX_API_KEY”) else False
_PAUSED = False
_BOT_START_TS = time.time()
_STATE_LOCK = Lock()

# 各時框獨立自動下單路由開關

AUTO_TRADE: Dict[str, bool] = {
“15m”: True,
“30m”: True,
“1H”: True,
“4H”: True
}

# API 基本節點網址

OKX_BASE = “https://www.okx.com”
CONA_BASE = “https://api.coinalyze.net/v1”
DC_BASE = “https://discord.com/api/v10”

# ══════════════════════════════════════════════════════════════════════════════

# 完整 40+ 核心追蹤幣種字典對照表 (SYMBOLS MAPPING)

# ══════════════════════════════════════════════════════════════════════════════

SYMBOLS: Dict[str, str] = {
“BTC-USDT-SWAP”: “BTC/USDT”,
“ETH-USDT-SWAP”: “ETH/USDT”,
“SOL-USDT-SWAP”: “SOL/USDT”,
“XRP-USDT-SWAP”: “XRP/USDT”,
“BNB-USDT-SWAP”: “BNB/USDT”,
“DOGE-USDT-SWAP”: “DOGE/USDT”,
“ADA-USDT-SWAP”: “ADA/USDT”,
“TRX-USDT-SWAP”: “TRX/USDT”,
“SUI-USDT-SWAP”: “SUI/USDT”,
“LINK-USDT-SWAP”: “LINK/USDT”,
“AVAX-USDT-SWAP”: “AVAX/USDT”,
“TON-USDT-SWAP”: “TON/USDT”,
“HBAR-USDT-SWAP”: “HBAR/USDT”,
“XLM-USDT-SWAP”: “XLM/USDT”,
“BCH-USDT-SWAP”: “BCH/USDT”,
“LTC-USDT-SWAP”: “LTC/USDT”,
“DOT-USDT-SWAP”: “DOT/USDT”,
“UNI-USDT-SWAP”: “UNI/USDT”,
“TAO-USDT-SWAP”: “TAO/USDT”,
“NEAR-USDT-SWAP”: “NEAR/USDT”,
“APT-USDT-SWAP”: “APT/USDT”,
“ARB-USDT-SWAP”: “ARB/USDT”,
“ATOM-USDT-SWAP”: “ATOM/USDT”,
“ETC-USDT-SWAP”: “ETC/USDT”,
“ICP-USDT-SWAP”: “ICP/USDT”,
“AAVE-USDT-SWAP”: “AAVE/USDT”,
“RENDER-USDT-SWAP”: “RENDER/USDT”,
“FIL-USDT-SWAP”: “FIL/USDT”,
“ENA-USDT-SWAP”: “ENA/USDT”,
“ALGO-USDT-SWAP”: “ALGO/USDT”,
“WLD-USDT-SWAP”: “WLD/USDT”,
“ONDO-USDT-SWAP”: “ONDO/USDT”,
“JUP-USDT-SWAP”: “JUP/USDT”,
“POL-USDT-SWAP”: “POL/USDT”,
“ZEC-USDT-SWAP”: “ZEC/USDT”,
“DASH-USDT-SWAP”: “DASH/USDT”,
“PENGU-USDT-SWAP”: “PENGU/USDT”,
“MORPHO-USDT-SWAP”: “MORPHO/USDT”,
“HYPE-USDT-SWAP”: “HYPE/USDT”,
“SKY-USDT-SWAP”: “SKY/USDT”,
}

OKX_SWAP: Dict[str, str] = {v: k for k, v in SYMBOLS.items()}

CONA_SPOT: Dict[str, str] = {
“BTC/USDT”: “BTCUSDT.A”, “ETH/USDT”: “ETHUSDT.A”, “SOL/USDT”: “SOLUSDT.A”,
“XRP/USDT”: “XRPUSDT.A”, “BNB/USDT”: “BNBUSDT.A”, “DOGE/USDT”: “DOGEUSDT.A”,
“ADA/USDT”: “ADAUSDT.A”, “TRX/USDT”: “TRXUSDT.A”, “SUI/USDT”: “SUIUSDT.A”,
“LINK/USDT”: “LINKUSDT.A”, “AVAX/USDT”: “AVAXUSDT.A”, “TON/USDT”: “TONUSDT.A”,
“HBAR/USDT”: “HBARUSDT.A”, “XLM/USDT”: “XLMUSDT.A”, “BCH/USDT”: “BCHUSDT.A”,
“LTC/USDT”: “LTCUSDT.A”, “DOT/USDT”: “DOTUSDT.A”, “UNI/USDT”: “UNIUSDT.A”,
“NEAR/USDT”: “NEARUSDT.A”, “APT/USDT”: “APTUSDT.A”, “ARB/USDT”: “ARBUSDT.A”,
“ATOM/USDT”: “ATOMUSDT.A”, “ETC/USDT”: “ETCUSDT.A”, “ICP/USDT”: “ICPUSDT.A”,
“AAVE/USDT”: “AAVEUSDT.A”, “FIL/USDT”: “FILUSDT.A”, “ENA/USDT”: “ENAUSDT.A”,
“ALGO/USDT”: “ALGOUSDT.A”, “WLD/USDT”: “WLDUSDT.A”, “ONDO/USDT”: “ONDOUSDT.A”,
}

CONA_PERP: Dict[str, str] = {
“BTC/USDT”: “BTCUSDT_PERP.A”, “ETH/USDT”: “ETHUSDT_PERP.A”, “SOL/USDT”: “SOLUSDT_PERP.A”,
“XRP/USDT”: “XRPUSDT_PERP.A”, “BNB/USDT”: “BNBUSDT_PERP.A”, “DOGE/USDT”: “DOGEUSDT_PERP.A”,
“ADA/USDT”: “ADAUSDT_PERP.A”, “TRX/USDT”: “TRXUSDT_PERP.A”, “SUI/USDT”: “SUIUSDT_PERP.A”,
“LINK/USDT”: “LINKUSDT_PERP.A”, “AVAX/USDT”: “AVAXUSDT_PERP.A”, “HBAR/USDT”: “HBARUSDT_PERP.A”,
“XLM/USDT”: “XLMUSDT_PERP.A”, “BCH/USDT”: “BCHUSDT_PERP.A”, “LTC/USDT”: “LTCUSDT_PERP.A”,
“DOT/USDT”: “DOTUSDT_PERP.A”, “UNI/USDT”: “UNIUSDT_PERP.A”, “NEAR/USDT”: “NEARUSDT_PERP.A”,
“APT/USDT”: “APTUSDT_PERP.A”, “ARB/USDT”: “ARBUSDT_PERP.A”, “ATOM/USDT”: “ATOMUSDT_PERP.A”,
“ETC/USDT”: “ETCUSDT_PERP.A”, “ICP/USDT”: “ICPUSDT_PERP.A”, “AAVE/USDT”: “AAVEUSDT_PERP.A”,
“FIL/USDT”: “FILUSDT_PERP.A”, “ENA/USDT”: “ENAUSDT_PERP.A”, “ALGO/USDT”: “ALGOUSDT_PERP.A”,
“WLD/USDT”: “WLDUSDT_PERP.A”, “ONDO/USDT”: “ONDOUSDT_PERP.A”,
}

BAR_TO_CONA = {“5m”: “5min”, “15m”: “15min”, “30m”: “30min”, “1H”: “1hour”, “4H”: “4hour”}
BAR_SECONDS = {“5m”: 300, “15m”: 900, “30m”: 1800, “1H”: 3600, “4H”: 14400}
WARMUP = 700
TIMEFRAMES = [“15m”, “30m”, “1H”, “4H”]

# ══════════════════════════════════════════════════════════════════════════════

# QQE MOD 核心固定數學常數

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

_dc_last_msg_id = “0”

# ══════════════════════════════════════════════════════════════════════════════

# 多時框全自動路由最佳化動態參數對照表 (PRODUCTION CONFIG MAPPINGS)

# ══════════════════════════════════════════════════════════════════════════════

BEST_PARAMS: Dict[str, Dict[str, Any]] = {
“15m_long”: {
“tp1_mult”: 1.725, “tp2_intraday_mult”: 1.8, “tp2_swing_mult”: 1.8,
“sl_atr_buffer”: 0.01, “structure_lookback”: 28, “exit_mode”: “fixed”
},
“15m_short”: {
“tp1_mult”: 2.0, “tp2_intraday_mult”: 3.2, “tp2_swing_mult”: 3.2,
“sl_atr_buffer”: 0.08, “structure_lookback”: 20, “exit_mode”: “fixed”
},
“30m_long”: {
“tp1_mult”: 1.725, “tp2_intraday_mult”: 1.8, “tp2_swing_mult”: 1.8,
“sl_atr_buffer”: 0.05, “structure_lookback”: 10, “exit_mode”: “fixed”
},
“30m_short”: {
“tp1_mult”: 2.0, “tp2_intraday_mult”: 3.2, “tp2_swing_mult”: 3.2,
“sl_atr_buffer”: 0.01, “structure_lookback”: 10, “exit_mode”: “trailing”
},
“1H_long”: {
“tp1_mult”: 1.725, “tp2_intraday_mult”: 2.5, “tp2_swing_mult”: 2.5,
“sl_atr_buffer”: 0.15, “structure_lookback”: 10, “exit_mode”: “fixed”
},
“1H_short”: {
“tp1_mult”: 2.0, “tp2_intraday_mult”: 4.0, “tp2_swing_mult”: 4.0,
“sl_atr_buffer”: 0.08, “structure_lookback”: 20, “exit_mode”: “fixed”
},
“4H_long”: {
“tp1_mult”: 1.725, “tp2_intraday_mult”: 2.5, “tp2_swing_mult”: 2.5,
“sl_atr_buffer”: 0.03, “structure_lookback”: 10, “exit_mode”: “trailing”
},
“4H_short”: {
“tp1_mult”: 2.0, “tp2_intraday_mult”: 4.0, “tp2_swing_mult”: 4.0,
“sl_atr_buffer”: 0.05, “structure_lookback”: 30, “exit_mode”: “fixed”
},
}

def get_params(tf: str, side: str, base_dir: str = “.”) -> Dict[str, Any]:
“”” 精確抓取指定時框與多空方向的最佳化回測因子參數 “””
trade_keys = {“tp1_mult”, “tp2_intraday_mult”, “tp2_swing_mult”, “sl_atr_buffer”, “structure_lookback”, “exit_mode”}
paths = [
os.path.join(base_dir, f”best_params_{tf.lower()}*{side}.json”),
os.path.join(base_dir, “final_params_all.json”)
]
for fname in paths:
if os.path.exists(fname):
try:
with open(fname, encoding=“utf-8”) as f:
raw = json.load(f)
data = raw.get(f”{tf}*{side}”, raw.get(“params”, raw))
extracted = {k: v for k, v in data.items() if k in trade_keys}
if len(extracted) >= 4:
base = BEST_PARAMS.get(f”{tf}*{side}”, {}).copy()
base.update(extracted)
return base
except:
pass
return BEST_PARAMS.get(f”{tf}*{side}”, {
“tp1_mult”: 1.7, “tp2_intraday_mult”: 1.8, “tp2_swing_mult”: 2.5,
“sl_atr_buffer”: 0.08, “structure_lookback”: 20, “exit_mode”: “fixed”
}).copy()

# ══════════════════════════════════════════════════════════════════════════════

# 內部記帳監控狀態管理 (STATE MANAGEMENT)

# ══════════════════════════════════════════════════════════════════════════════

pending_orders: Dict[str, Dict[str, Any]] = {}

class PaperPosition:
def **init**(self):
self.open: bool = False
self.side: str = “”
self.entry: float = 0.0
self.sl: float = 0.0
self.tp1: float = 0.0
self.tp2: float = 0.0
self.tp1_hit: bool = False
self.exit_mode: str = “fixed”

# ══════════════════════════════════════════════════════════════════════════════

# 通知元件模組 (DISCORD & TELEGRAM NOTIFICATIONS)

# ══════════════════════════════════════════════════════════════════════════════

def _dc_headers() -> Dict[str, str]:
return {“Authorization”: f”Bot {DISCORD_TOKEN}”, “Content-Type”: “application/json”}

def dc_log(text: str):
“”” 標準化 Discord 文字頻道輸出 “””
if not DISCORD_TOKEN or not DISCORD_CHANNEL_ID:
print(f”[Console Log] {text}”)
return
try:
payload = {“content”: str(text)}
requests.post(f”{DC_BASE}/channels/{DISCORD_CHANNEL_ID}/messages”, headers=_dc_headers(), json=payload, timeout=10)
except Exception as e:
print(f”  [Discord HTTP 異常] {e}”)

def tg_log(text: str):
“”” 備用 Telegram 管道輸出 “””
if not TG_BOT_TOKEN or not TG_CHAT_ID:
return
try:
url = f”https://api.telegram.org/bot{TG_BOT_TOKEN}/sendMessage”
requests.post(url, json={“chat_id”: TG_CHAT_ID, “text”: text, “parse_mode”: “Markdown”}, timeout=10)
except:
pass

def dc_embed_send(embed: Dict[str, Any], components: List[Any] = None) -> Optional[str]:
“”” 發送互動式帶有 UI 按鈕組件的 Embed 進階通知 “””
if not DISCORD_TOKEN or not DISCORD_CHANNEL_ID:
return None
payload = {“embeds”: [embed]}
if components:
payload[“components”] = components
try:
r = requests.post(f”{DC_BASE}/channels/{DISCORD_CHANNEL_ID}/messages”, headers=_dc_headers(), json=payload, timeout=10)
return r.json().get(“id”)
except Exception as e:
print(f”  [Embed 發送失敗] {e}”)
return None

def dc_embed_edit(message_id: str, new_text: str):
“”” 實時更新交互按鈕的點擊結果狀態，防止二次重複操作 “””
if not DISCORD_TOKEN or not message_id:
return
try:
url = f”{DC_BASE}/channels/{DISCORD_CHANNEL_ID}/messages/{message_id}”
requests.patch(url, headers=_dc_headers(), json={“content”: new_text, “components”: []}, timeout=10)
except Exception as e:
print(f”  [Embed 狀態更替失敗] {e}”)

def create_interactive_signal(sig: Dict[str, Any], symbol: str, tf: str, cvd_ok: bool) -> str:
“”” 建置完全體交互控制台卡片，整合止損比率、盈虧比與手動掛單快取 “””
side_emoji = “🟢” if sig[“side”] == “long” else “🔴”
dir_name = “多頭趨勢進場” if sig[“side”] == “long” else “空頭趨勢進場”
swing_tag = “📐 趨勢波段追蹤” if sig[“is_swing”] else “⚡ 短線日內反彈”
cvd_tag = “CVD ✅ 動能同步確認” if cvd_ok else “CVD ⚠️ 量能背離過濾”
card_color = 0x2ecc71 if sig[“side”] == “long” else 0xe74c3c

```
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
```

# ══════════════════════════════════════════════════════════════════════════════

# OKX 底層 REST API 原始簽章與行情擷取 (EXCHANGE CONNECTOR)

# ══════════════════════════════════════════════════════════════════════════════

def _okx_generate_signature(timestamp: str, method: str, request_path: str, body: str = “”) -> str:
message = timestamp + method + request_path + body
secret_bytes = OKX_SECRET.encode(“utf-8”)
signature = hmac.new(secret_bytes, message.encode(“utf-8”), hashlib.sha256).digest()
return base64.b64encode(signature).decode(“utf-8”)

def _fetch_okx_public_data(endpoint_path: str, query_params: dict) -> list:
“”” 高穩定度原生連接器，負責拉取 K 線與即時費率 “””
try:
url = f”{OKX_BASE}{endpoint_path}”
headers = {
“Content-Type”: “application/json”,
**({“x-simulated-trading”: “1”} if OKX_DEMO else {})
}
r = requests.get(url, params=query_params, headers=headers, timeout=12)
if r.status_code == 200:
res_json = r.json()
if res_json.get(“code”) == “0”:
return res_json.get(“data”, [])
return []
except:
return []

def fetch_market_candles(inst_id: str, timeframe_bar: str, fetch_limit: int = WARMUP) -> pd.DataFrame:
“”” 抓取歷史 K 線數據並轉換為精準格式之 Pandas DataFrame “””
raw_candles = _fetch_okx_public_data(”/api/v5/market/candles”, {
“instId”: inst_id,
“bar”: timeframe_bar,
“limit”: str(min(fetch_limit, 300))
})
if not raw_candles:
return pd.DataFrame()

```
# 依時間戳由舊到新排序
sorted_candles = sorted(raw_candles, key=lambda x: int(x[0]))

df = pd.DataFrame(sorted_candles, columns=["ts", "open", "high", "low", "close", "vol", "volCcy", "volCcyQuote", "confirm"])
for col in ["open", "high", "low", "close", "vol"]:
    df[col] = df[col].astype(float)

df["ts"] = pd.to_datetime(df["ts"].astype(np.int64), unit="ms", utc=True)
df.set_index("ts", inplace=True)
# 去除當前未收盤的 K 棒，確保訊號絕對不會發生漂移 (Non-Repainting)
return df.iloc[:-1]
```

def fetch_current_funding_rate(swap_id: str) -> float:
“”” 擷取指定永續合約商品當前秒數之資金費率 “””
data_list = _fetch_okx_public_data(”/api/v5/public/funding-rate”, {“instId”: swap_id})
if data_list:
return float(data_list[0].get(“fundingRate”, 0.0))
return 0.0

# ══════════════════════════════════════════════════════════════════════════════

# COINALYZE 大數據特徵過濾引擎 (CVD + OI ANALYSIS)

# ══════════════════════════════════════════════════════════════════════════════

def _execute_coinalyze_request(endpoint: str, query_params: dict) -> list:
“”” 對接 Coinalyze API 獲取大體量逐筆成交明細累積與未平倉特徵 “””
if not COINALYZE_API_KEY:
return []
try:
url = f”{CONA_BASE}/{endpoint}”
req_headers = {“api-key”: COINALYZE_API_KEY}
r = requests.get(url, params=query_params, headers=req_headers, timeout=15)
if r.status_code == 200:
res_data = r.json()
if isinstance(res_data, list):
return res_data
return res_data.get(“result”, res_data.get(“data”, []))
return []
except:
return []

def calculate_cumulative_volume_delta(cona_symbol: str, cona_interval: str, start_timestamp: int, end_timestamp: int) -> pd.Series:
“”” 計算出精準的 CVD (累積成交量差額) 指標線 “””
raw_data = _execute_coinalyze_request(“history”, {
“symbols”: cona_symbol,
“interval”: cona_interval,
“from”: str(start_timestamp // 1000),
“to”: str(end_timestamp // 1000),
“convert_to_usd”: “false”
})
if not raw_data:
return pd.Series(dtype=float)

```
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
```

def fetch_open_interest_series(cona_symbol: str, cona_interval: str, start_timestamp: int, end_timestamp: int) -> pd.Series:
“”” 追蹤機構持倉未平倉合約總量 (OI) 走勢 “””
raw_data = _execute_coinalyze_request(“open-interest-history”, {
“symbols”: cona_symbol,
“interval”: cona_interval,
“from”: str(start_timestamp // 1000),
“to”: str(end_timestamp // 1000)
})
if not raw_data:
return pd.Series(dtype=float)

```
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
```

# ══════════════════════════════════════════════════════════════════════════════

# 複雜技術指標庫算力模組 (TECHNICAL INDICATORS MATHEMATICS)

# ══════════════════════════════════════════════════════════════════════════════

def calculate_smooth_rsi(series_src: pd.Series, rolling_period: int) -> pd.Series:
“”” 計算非線性標準化相對強弱指標線 “””
price_delta = series_src.diff()
up_trends = price_delta.clip(lower=0.0)
down_trends = -price_delta.clip(upper=0.0)
mean_up = up_trends.ewm(com=rolling_period - 1, adjust=False).mean()
mean_down = down_trends.ewm(com=rolling_period - 1, adjust=False).mean()
rs_value = mean_up / mean_down.replace(0.0, 1e-9)
return 100.0 - (100.0 / (1.0 + rs_value))

def calculate_full_qqe_mod(data_df: pd.DataFrame, rsi_pd: int = 6, sf_pd: int = 5, factor_mult: float = 4.236) -> Tuple[pd.Series, pd.Series]:
“”” 完全體 QQE MOD 動態移動區間軌道演算演算法 “””
src_close = data_df[“close”]
rsi_series = calculate_smooth_rsi(src_close, rsi_pd)
rsi_smoothed_ma = rsi_series.ewm(span=sf_pd, adjust=False).mean()
absolute_rsi_delta = rsi_smoothed_ma.diff().abs()
smoothed_atr_rsi = absolute_rsi_delta.ewm(span=2 * rsi_pd - 1, adjust=False).mean()
dar_trailing_band = smoothed_atr_rsi.ewm(span=2 * rsi_pd - 1, adjust=False).mean() * factor_mult

```
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
```

def calculate_average_true_range(data_df: pd.DataFrame, atr_period: int = 14) -> pd.Series:
“”” 計算真實波動幅度均值 (ATR) “””
high_prices = data_df[“high”]
low_prices = data_df[“low”]
previous_closes = data_df[“close”].shift(1)
tr1 = high_prices - low_prices
tr2 = (high_prices - previous_closes).abs()
tr3 = (low_prices - previous_closes).abs()
true_range = pd.concat([tr1, tr2, tr3], axis=1).max(axis=1)
return true_range.ewm(alpha=1.0 / atr_period, adjust=False).mean()

def calculate_directional_movement_index(data_df: pd.DataFrame, adx_period: int = 14) -> pd.Series:
“”” 動向指標 (DMI/ADX) 趨勢強度過濾器 “””
high_diff = data_df[“high”].diff()
low_diff = -data_df[“low”].diff()
plus_dm = np.where((high_diff > low_diff) & (high_diff > 0), high_diff, 0.0)
minus_dm = np.where((low_diff > high_diff) & (low_diff > 0), low_diff, 0.0)

```
atr_series = calculate_average_true_range(data_df, adx_period).replace(0.0, 1e-9)
plus_di = 100.0 * pd.Series(plus_dm, index=data_df.index).ewm(alpha=1.0 / adx_period, adjust=False).mean() / atr_series
minus_di = 100.0 * pd.Series(minus_dm, index=data_df.index).ewm(alpha=1.0 / adx_period, adjust=False).mean() / atr_series

dx_value = 100.0 * (plus_di - minus_di).abs() / (plus_di + minus_di).replace(0.0, 1e-9)
return dx_value.ewm(alpha=1.0 / adx_period, adjust=False).mean()
```

# ══════════════════════════════════════════════════════════════════════════════

# 接續上篇：OKX 實盤風控倉位自動計算與分批委託鏈 (ORDER EXECUTION)

# ══════════════════════════════════════════════════════════════════════════════

def _initialize_ccxt_client() -> ccxt.okx:
client = ccxt.okx({
“apiKey”: OKX_API_KEY,
“secret”: OKX_SECRET,
“password”: OKX_PASSPHRASE,
“options”: {“defaultType”: “swap”}
})
if OKX_DEMO:
client.set_sandbox_mode(True)
return client

def execute_okx_trade_pipeline(symbol_id: str, trade_side: str, entry_price: float, stop_loss: float, target_tp1: float, target_tp2: float):
“”” 實盤訂單路由模組：整合動態槓桿、精密合約張數轉換、市價與限價單組合 “””
global _LIVE_MODE, MAX_LEVERAGE, MARGIN_PCT
if not _LIVE_MODE:
dc_log(f”📝 [紙交易通知] 商品 {symbol_id} 方向 {trade_side} 通過安全審核，當前處於 Paper 模擬模式，跳過交易所劃轉。”)
return

```
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
```

# ══════════════════════════════════════════════════════════════════════════════

# 核心策略掃描中樞核心引擎 (MARKET MATRIX ENGINE)

# ══════════════════════════════════════════════════════════════════════════════

class SykesTradingBot:
def **init**(self):
self.cooldown_dict: Dict[str, float] = {}
self.consec_losses = 0
self.circuit_break_until: Optional[float] = None
self.paper_positions: Dict[str, PaperPosition] = {}

```
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
    dc_log(f"🚨 **風控核心硬熔斷發動** 🚨\n系統已連續虧損 {MAX_CONSEC_LOSS} 次，全面暫停自動下單模組 {PAUSE_HOURS} 小時！")

def update_paper_trailing_and_exits(self, symbol_item: str, current_price: float):
    """ Paper 模式下動態追蹤止損與模擬止盈更新 """
    for tf in TIMEFRAMES:
        pos_key = f"{symbol_item}_{tf}"
        if pos_key not in self.paper_positions or not self.paper_positions[pos_key].open:
            continue
        pos = self.paper_positions[pos_key]

        # 多頭部位離場檢測
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
                dc_log(f"🎯 [紙交易獲利] {symbol_item} ({tf}) 達標第一目標價 TP1 `{pos.tp1}`，推動保本止損至進場價。")
                pos.tp1_hit = True
                pos.sl = pos.entry
            elif current_price >= pos.tp2:
                dc_log(f"🌕 [紙交易獲利] {symbol_item} ({tf}) 全滿達標終點 TP2 `{pos.tp2}`！")
                pos.open = False
                self.consec_losses = 0
            elif pos.exit_mode == "trailing" and pos.tp1_hit:
                # 追蹤止損：隨價格上漲動態調高止損
                new_sl = current_price * 0.98
                if new_sl > pos.sl:
                    pos.sl = new_sl

        # 空頭部位離場檢測
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
                dc_log(f"🎯 [紙交易獲利] {symbol_item} ({tf}) 達標第一目標價 TP1 `{pos.tp1}`，推動保本止損至進場價。")
                pos.tp1_hit = True
                pos.sl = pos.entry
            elif current_price <= pos.tp2:
                dc_log(f"🌕 [紙交易獲利] {symbol_item} ({tf}) 全滿達標終點 TP2 `{pos.tp2}`！")
                pos.open = False
                self.consec_losses = 0
            elif pos.exit_mode == "trailing" and pos.tp1_hit:
                new_sl = current_price * 1.02
                if new_sl < pos.sl:
                    pos.sl = new_sl

def scan_and_process_market(self, symbol_item: str, tf_id: str):
    """ 核心策略演算法矩陣掃描線 """
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

    current_close_price = df["close"].iloc[-1]
    self.update_paper_trailing_and_exits(symbol_item, current_close_price)

    # 2. QQE MOD 與指標訊號計算
    p = get_params(tf_id, "long")  # 預設載入基本結構看盤
    rsi_ma, trailing_band = calculate_full_qqe_mod(df, QQE_RSI, QQE_SF, QQE_FACTOR_P)
    atr_series = calculate_average_true_range(df, 14)
    adx_series = calculate_directional_movement_index(df, 14)

    current_rsi_ma = rsi_ma.iloc[-1]
    current_band = trailing_band.iloc[-1]
    prev_rsi_ma = rsi_ma.iloc[-2]
    prev_band = trailing_band.iloc[-2]
    current_atr = atr_series.iloc[-1]
    current_adx = adx_series.iloc[-1]

    # 3. 趨勢強度與多空狀態過濾
    trend_is_long = current_rsi_ma > current_band
    trend_just_crossed_long = (prev_rsi_ma <= prev_band) and trend_is_long
    trend_just_crossed_short = (prev_rsi_ma >= prev_band) and not trend_is_long

    # 資金費率過濾
    funding_rate = fetch_current_funding_rate(okx_swap_symbol)

    # 4. 多頭訊號與進場規劃
    if trend_just_crossed_long and current_adx >= ADX_THR:
        if funding_rate > FUNDING_LONG_MAX:
            return  # 多頭費率過高過濾

        p = get_params(tf_id, "long")
        lookback = int(p["structure_lookback"])
        recent_low = df["low"].iloc[-lookback:].min()
        calculated_sl = recent_low - (current_atr * p["sl_atr_buffer"])

        # 確保止損百分比安全
        risk_pct = (abs(current_close_price - calculated_sl) / current_close_price)
        if risk_pct > MAX_SL:
            calculated_sl = current_close_price * (1.0 - MAX_SL)
            risk_pct = MAX_SL

        tp1_target = current_close_price + (current_atr * p["tp1_mult"])
        tp2_target = current_close_price + (current_atr * p["tp2_swing_mult"])

        # 計算盈虧比 (Reward-to-Risk Ratio)
        risk_delta = abs(current_close_price - calculated_sl) or 1e-9
        rr1 = abs(tp1_target - current_close_price) / risk_delta
        rr2 = abs(tp2_target - current_close_price) / risk_delta

        # Coinalyze CVD 動能過濾
        cvd_verified = True
        cona_code = CONA_PERP.get(symbol_item, CONA_SPOT.get(symbol_item))
        if cona_code:
            end_ts = int(time.time() * 1000)
            start_ts = end_ts - (BAR_SECONDS[tf_id] * CVD_WINDOW * 1000)
            cvd_line = calculate_cumulative_volume_delta(cona_code, okx_bar_fmt, start_ts, end_ts)
            if not cvd_line.empty and len(cvd_line) >= 2:
                if cvd_line.iloc[-1] < cvd_line.iloc[-2]:
                    cvd_verified = False  # 價漲量縮，屬於背離訊號

        signal_payload = {
            "side": "long", "entry": current_close_price, "sl": round(calculated_sl, 5),
            "tp1": round(tp1_target, 5), "tp2": round(tp2_target, 5), "atr": round(current_atr, 4),
            "risk_pct": risk_pct * 100.0, "rr1": rr1, "rr2": rr2, "is_swing": True,
            "exit_mode": p["exit_mode"], "time": datetime.now(timezone.utc).isoformat()
        }

        self.set_cooldown(symbol_item, tf_id)
        callback_id = create_interactive_signal(signal_payload, symbol_item, tf_id, cvd_verified)

        # 路由：若開啟自動下單則直通實盤管道
        if AUTO_TRADE.get(tf_id) and cvd_verified:
            execute_okx_trade_pipeline(okx_swap_symbol, "long", current_close_price, signal_payload["sl"], signal_payload["tp1"], signal_payload["tp2"])
        else:
            # 寫入 PaperPosition 狀態紀錄機
            pos = PaperPosition()
            pos.open = True; pos.side = "long"; pos.entry = current_close_price
            pos.sl = signal_payload["sl"]; pos.tp1 = signal_payload["tp1"]; pos.tp2 = signal_payload["tp2"]
            pos.exit_mode = p["exit_mode"]
            self.paper_positions[f"{symbol_item}_{tf_id}"] = pos

    # 5. 空頭訊號與進場規劃
    elif trend_just_crossed_short and current_adx >= ADX_THR:
        if funding_rate < FUNDING_SHORT_MIN:
            return  # 空頭費率過度負值過濾

        p = get_params(tf_id, "short")
        lookback = int(p["structure_lookback"])
        recent_high = df["high"].iloc[-lookback:].max()
        calculated_sl = recent_high + (current_atr * p["sl_atr_buffer"])

        risk_pct = (abs(calculated_sl - current_close_price) / current_close_price)
        if risk_pct > MAX_SL:
            calculated_sl = current_close_price * (1.0 + MAX_SL)
            risk_pct = MAX_SL

        tp1_target = current_close_price - (current_atr * p["tp1_mult"])
        tp2_target = current_close_price - (current_atr * p["tp2_swing_mult"])

        risk_delta = abs(calculated_sl - current_close_price) or 1e-9
        rr1 = abs(current_close_price - tp1_target) / risk_delta
        rr2 = abs(current_close_price - tp2_target) / risk_delta

        cvd_verified = True
        cona_code = CONA_PERP.get(symbol_item, CONA_SPOT.get(symbol_item))
        if cona_code:
            end_ts = int(time.time() * 1000)
            start_ts = end_ts - (BAR_SECONDS[tf_id] * CVD_WINDOW * 1000)
            cvd_line = calculate_cumulative_volume_delta(cona_code, okx_bar_fmt, start_ts, end_ts)
            if not cvd_line.empty and len(cvd_line) >= 2:
                if cvd_line.iloc[-1] > cvd_line.iloc[-2]:
                    cvd_verified = False  # 價跌量增，空頭背離過濾

        signal_payload = {
            "side": "short", "entry": current_close_price, "sl": round(calculated_sl, 5),
            "tp1": round(tp1_target, 5), "tp2": round(tp2_target, 5), "atr": round(current_atr, 4),
            "risk_pct": risk_pct * 100.0, "rr1": rr1, "rr2": rr2, "is_swing": True,
            "exit_mode": p["exit_mode"], "time": datetime.now(timezone.utc).isoformat()
        }

        self.set_cooldown(symbol_item, tf_id)
        callback_id = create_interactive_signal(signal_payload, symbol_item, tf_id, cvd_verified)

        if AUTO_TRADE.get(tf_id) and cvd_verified:
            execute_okx_trade_pipeline(okx_swap_symbol, "short", current_close_price, signal_payload["sl"], signal_payload["tp1"], signal_payload["tp2"])
        else:
            pos = PaperPosition()
            pos.open = True; pos.side = "short"; pos.entry = current_close_price
            pos.sl = signal_payload["sl"]; pos.tp1 = signal_payload["tp1"]; pos.tp2 = signal_payload["tp2"]
            pos.exit_mode = p["exit_mode"]
            self.paper_positions[f"{symbol_item}_{tf_id}"] = pos
```

_bot_ref = SykesTradingBot()

# ══════════════════════════════════════════════════════════════════════════════

# 嵌入式 WEB 伺服器與 DISCORD INTERACTION API 控制台 (WEB CONTROL CENTER)

# ══════════════════════════════════════════════════════════════════════════════

app = Flask(**name**)

def verify_discord_signature(raw_body: bytes, signature: str, timestamp: str) -> bool:
“”” Ed25519 靜態無狀態簽章驗證演算法 “””
if not DISCORD_PUBLIC_KEY or not signature or not timestamp:
return False
try:
from nacl.signing import VerifyKey
verify_key = VerifyKey(bytes.fromhex(DISCORD_PUBLIC_KEY))
verify_key.verify(timestamp.encode() + raw_body, signature=bytes.fromhex(signature))
return True
except:
return False

@app.route(”/interactions”, methods=[“POST”])
def discord_interactions_webhook():
“”” 接收並解析來自 Discord 互動式 UI 按鈕的異步點擊授權回調 “””
signature = request.headers.get(“X-Signature-Ed25519”, “”)
timestamp = request.headers.get(“X-Signature-Timestamp”, “”)
raw_body = request.data

```
if not verify_discord_signature(raw_body, signature, timestamp):
    return jsonify({"type": 1}), 200  # 即使密鑰未配對，依舊響應 Ping 以防止 Discord 斷開

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
                    order["sl"], order["tp1"], order["tp2"]
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
```

# ══════════════════════════════════════════════════════════════════════════════

# 時間同步與無漂移收盤矩陣輪詢機制 (SCHEDULER & MAIN LOOP)

# ══════════════════════════════════════════════════════════════════════════════

def synchronise_and_wait_next_candle() -> List[str]:
“”” 無漂移收盤對齊引擎：等待下一個整點/15分收盤 K 棒，返回觸發的時框 “””
while True:
now = datetime.now()
current_minute = now.minute
current_second = now.second

```
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
        sleep(5)  # 延遲 5 秒，確保 OKX 伺服器端已經完全收盤並封裝好 K 棒數據
        return active_timeframes

    sleep(1)
```

_dc_last_msg_id = None

def poll_dc_commands():
“”” 輪詢 Discord 頻道訊息，處理 ! 指令 “””
global _PAUSED, _LIVE_MODE, _dc_last_msg_id
if not DISCORD_TOKEN or not DISCORD_CHANNEL_ID:
print(”[DC] DISCORD_TOKEN 或 DISCORD_CHANNEL_ID 未設定，指令輪詢停用。”)
return
headers = {“Authorization”: f”Bot {DISCORD_TOKEN}”}
print(”[DC] 指令輪詢已啟動。”)
while True:
try:
params = {“limit”: 5}
if _dc_last_msg_id:
params[“after”] = _dc_last_msg_id
resp = requests.get(f”{DC_BASE}/channels/{DISCORD_CHANNEL_ID}/messages”, headers=headers, params=params, timeout=10)
if resp.status_code == 200:
messages = resp.json()
if messages:
# Discord 回傳最新在前，反轉處理
for msg in reversed(messages):
msg_id = msg.get(“id”, “”)
content = msg.get(“content”, “”).strip()
author = msg.get(“author”, {})
is_bot = author.get(“bot”, False)
if msg_id and (not _dc_last_msg_id or int(msg_id) > int(_dc_last_msg_id)):
_dc_last_msg_id = msg_id
if is_bot or not content.startswith(”!”):
continue
cmd = content.lower().split()[0]
uptime_s = int(time.time() - _BOT_START_TS)
uptime_h = uptime_s // 3600
uptime_m = (uptime_s % 3600) // 60
if cmd == “!status”:
mode = “🟢 LIVE 實盤” if _LIVE_MODE else “🟡 PAPER 模擬”
paused = “⏸️ 已暫停” if _PAUSED else “▶️ 全時掃描正常運作中”
positions = len([p for p in _bot_ref.paper_positions.values() if p.open])
tf_status = “ | “.join([f”{tf}:{‘✅’ if AUTO_TRADE.get(tf) else ‘❌’}” for tf in AUTO_TRADE])
status_msg = (
f”⚙️ **賽克斯生產級交易核心運行指標系統**\n”
f”{‘─’*16}\n”
f”大腦監控狀態: {paused}\n”
f”目前工作特徵: **{mode}**\n”
f”單倉保證金配比: `{MARGIN_PCT}%` | 槓桿安全閾值上限: `{MAX_LEVERAGE}x`\n”
f”當前內部追蹤持倉數: `{positions}` 倉位\n”
f”累計連續運作時間: `{uptime_h}` 小時 `{uptime_m}` 分鐘\n”
f”多維監控規模: `40` 大核心主流幣種 | `4` 大時框同時覆蓋”
)
dc_log(status_msg)
elif cmd == “!help”:
dc_log(
“📋 **指令列表**\n”
“`!status` - 查看系統運行狀態\n”
“`!setlive` - 切換為實盤模式\n”
“`!setpaper` - 切換為模擬模式\n”
“`!pause` - 暫停自動交易\n”
“`!resume` - 恢復自動交易”
)
elif cmd == “!setlive”:
_LIVE_MODE = True
dc_log(“🟢 **已切換為實盤模式**，自動下單鏈已啟用。”)
elif cmd == “!setpaper”:
_LIVE_MODE = False
dc_log(“🟡 **已切換為模擬模式**，僅觀察訊號不執行下單。”)
elif cmd == “!pause”:
_PAUSED = True
dc_log(“⏸️ **系統已暫停**，停止掃描與下單。”)
elif cmd == “!resume”:
_PAUSED = False
dc_log(“▶️ **系統已恢復**，重新開始掃描。”)
except Exception as e:
print(f”[DC] 指令輪詢異常: {e}”)
sleep(5)

def main_polling_loop():
“”” 交易中樞核心守護進程主迴圈 “””
global _PAUSED, _bot_ref
start_alert = “🚀 **賽克斯全功能完全體智慧交易系統 v4 實盤部署完成**\n控制中樞已成功對齊 40+ 主流加密商品，開始進行 15m/30m/1H/4H 收盤矩陣輪詢機制…”
dc_log(start_alert)
tg_log(start_alert)

```
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
```

def run_embedded_web_server():
import logging
werkzeug_logger = logging.getLogger(“werkzeug”)
werkzeug_logger.setLevel(logging.ERROR)
app.run(host=“0.0.0.0”, port=int(os.environ.get(“PORT”, 3000)), debug=False)

# ══════════════════════════════════════════════════════════════════════════════

# 交易核心主入口引導程序 (ENTRYPOINT)

# ══════════════════════════════════════════════════════════════════════════════

if **name** == “**main**”:
parser = argparse.ArgumentParser(description=“Sykes Multi-Timeframe Trading System Engine”)
parser.add_argument(”–live”, action=“store_true”, help=“強制覆蓋開啟 OKX 實盤下單鏈”)
parser.add_argument(”–demo”, action=“store_true”, help=“切換至 OKX 模擬盤測試環境”)
args = parser.parse_args()

```
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