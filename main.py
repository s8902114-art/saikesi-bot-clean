import os
import requests
import pandas as pd
import ccxt
from flask import Flask
from threading import Thread
from time import sleep
from datetime import datetime, timezone, timedelta

# ══════════════════════════
# 填入你的設定
# ══════════════════════════
TG_TOKEN  = "7642408367:AAG_6HS6BLeHtST2cKjNjaU6Ajpmbe_cj8w"
TG_CHAT_ID = "8799334828"

# 監控時框
TIMEFRAMES = ["15m", "30m", "1h", "4h"]

# 每個時框對應的秒數
TF_SECONDS = {"15m": 900, "30m": 1800, "1h": 3600, "4h": 14400}

# 全部使用 OKX 永續合約（USDT.P）
SYMBOLS = [
    "BTC/USDT:USDT",
    "ETH/USDT:USDT",
    "XRP/USDT:USDT",
    "LTC/USDT:USDT",
    "ADA/USDT:USDT",
    "HYPE/USDT:USDT",
    "SUI/USDT:USDT",
    "DOGE/USDT:USDT",
    "LINK/USDT:USDT",
    "XAU/USDT:USDT",
    "ZEC/USDT:USDT",
    "H/USDT:USDT",
]
# ══════════════════════════

# 記錄上次發送的訊號，防止重複通知
last_signal = {}  # key: (symbol, timeframe), value: "long" | "short" | None

# Flask 狀態頁面
app = Flask(__name__)


@app.route("/")
def home():
    now_tw = tw_now().strftime("%Y-%m-%d %H:%M:%S")
    return f"賽克斯訊號機器人運行中｜台灣時間：{now_tw}"


def run_web():
    port = int(os.environ.get("PORT", 3000))
    app.run(host="0.0.0.0", port=port)


# ══════════════════════════
# 台灣時間工具
# ══════════════════════════
TW_OFFSET = timedelta(hours=8)

def tw_now() -> datetime:
    """回傳現在的台灣時間（UTC+8）"""
    return datetime.now(timezone.utc) + TW_OFFSET

def tw_str() -> str:
    return tw_now().strftime("%Y-%m-%d %H:%M:%S")


# ══════════════════════════
# K 棒對齊排程
# ══════════════════════════
def next_close_ts(timeframe: str) -> float:
    """計算該時框下一根 K 棒的收盤時間（Unix 秒，UTC）"""
    period = TF_SECONDS[timeframe]
    now_ts = datetime.now(timezone.utc).timestamp()
    return (int(now_ts) // period + 1) * period


def wait_for_next_candle():
    """
    等待到最近一個時框的 K 棒收盤後 5 秒。
    回傳這次應該掃描的時框清單（可能同時有多個時框收盤）。
    """
    # 計算每個時框下一次收盤+5秒的觸發時間
    triggers = {tf: next_close_ts(tf) + 5 for tf in TIMEFRAMES}
    earliest = min(triggers.values())

    # 等待到觸發前 0.5 秒（粗略 sleep）
    while True:
        wait = earliest - datetime.now(timezone.utc).timestamp() - 0.5
        if wait <= 0:
            break
        sleep(min(wait, 30))  # 最多每 30 秒重新評估一次

    # 精確等待到觸發時間
    while datetime.now(timezone.utc).timestamp() < earliest:
        sleep(0.05)

    # 收集本次到期的所有時框（在觸發時間 ±15 秒內）
    due = [tf for tf, ts in triggers.items() if abs(ts - earliest) <= 15]
    return due


# ══════════════════════════

def send_tg(msg):
    url = f"https://api.telegram.org/bot{TG_TOKEN}/sendMessage"
    try:
        requests.post(url, data={"chat_id": TG_CHAT_ID, "text": msg})
    except Exception:
        pass


def calc_rsi(series, period=14):
    delta = series.diff()
    gain = delta.clip(lower=0).ewm(com=period - 1, adjust=False).mean()
    loss = (-delta.clip(upper=0)).ewm(com=period - 1, adjust=False).mean()
    rs = gain / loss
    return 100 - (100 / (1 + rs))


def fetch_ohlcv_paginated(exchange, symbol, timeframe, target=1500):
    """分批往前抓歷史 K 線（OKX 單次上限 300）"""
    all_ohlcv = exchange.fetch_ohlcv(symbol, timeframe, limit=300)
    if not all_ohlcv:
        return []
    tf_ms = exchange.parse_timeframe(timeframe) * 1000
    while len(all_ohlcv) < target:
        earliest_ts = all_ohlcv[0][0]
        new_since   = earliest_ts - 300 * tf_ms
        try:
            batch = exchange.fetch_ohlcv(symbol, timeframe, since=new_since, limit=300)
        except Exception:
            break
        if not batch:
            break
        batch = [c for c in batch if c[0] < earliest_ts]
        if not batch:
            break
        all_ohlcv = batch + all_ohlcv
        sleep(0.2)
    return all_ohlcv[-target:]


def check_signal(exchange, symbol, timeframe):
    try:
        ohlcv = fetch_ohlcv_paginated(exchange, symbol, timeframe, target=1500)
    except Exception as e:
        print(f"[{tw_str()}][{symbol}][{timeframe}] 取得資料失敗：{e}")
        return
    if len(ohlcv) < 700:
        print(f"[{tw_str()}][{symbol}][{timeframe}] 資料不足（{len(ohlcv)} 根），略過")
        return

    df = pd.DataFrame(ohlcv, columns=["time", "open", "high", "low", "close", "vol"])

    df["ema12"]  = df["close"].ewm(span=12,  adjust=False).mean()
    df["ema144"] = df["close"].ewm(span=144, adjust=False).mean()
    df["ema169"] = df["close"].ewm(span=169, adjust=False).mean()
    df["ema576"] = df["close"].ewm(span=576, adjust=False).mean()
    df["ema676"] = df["close"].ewm(span=676, adjust=False).mean()

    df["smallTop"] = df[["ema144", "ema169"]].max(axis=1)
    df["smallBot"] = df[["ema144", "ema169"]].min(axis=1)
    df["largeTop"] = df[["ema576", "ema676"]].max(axis=1)
    df["largeBot"] = df[["ema576", "ema676"]].min(axis=1)

    df["rsi"]   = calc_rsi(df["close"], 14)
    df["rsiMa"] = df["rsi"].ewm(span=5, adjust=False).mean()

    last = df.iloc[-1]
    prev = df.iloc[-2]

    # K 棒收盤時間（轉台灣時間顯示）
    candle_close_utc = datetime.fromtimestamp(last["time"] / 1000, tz=timezone.utc)
    candle_close_tw  = (candle_close_utc + TW_OFFSET).strftime("%m/%d %H:%M")

    bullTrend   = last["ema144"] > last["ema576"]
    bearTrend   = last["ema144"] < last["ema576"]
    qqeTurnBlue = last["rsiMa"] >= 50 and prev["rsiMa"] < 50
    qqeTurnRed  = last["rsiMa"] < 50  and prev["rsiMa"] >= 50

    longC1 = (
        last["close"] > last["largeTop"]
        and last["close"] > last["smallBot"]
        and last["low"]  < last["smallBot"]
    )
    longC2 = last["close"] > last["ema12"]
    longC3 = qqeTurnBlue

    shortC1 = (
        last["close"] < last["largeBot"]
        and last["close"] < last["smallTop"]
        and last["high"] > last["smallTop"]
    )
    shortC2 = last["close"] < last["ema12"]
    shortC3 = qqeTurnRed

    name = symbol.split("/")[0]
    key  = (symbol, timeframe)
    prev_signal = last_signal.get(key)

    entry_price = last["close"]
    long_sl     = df.iloc[-6:-1]["low"].min()
    short_sl    = df.iloc[-6:-1]["high"].max()
    long_tp     = round(entry_price + (entry_price - long_sl), 4)
    short_tp    = round(entry_price - (short_sl - entry_price), 4)

    if bullTrend and longC1 and longC2 and longC3:
        if prev_signal != "long":
            send_tg(
                f"🟢 賽克斯做多訊號\n"
                f"幣種：{name}\n"
                f"時框：{timeframe}\n"
                f"K棒收盤：{candle_close_tw}（台灣時間）\n"
                f"入場價：{entry_price}\n"
                f"止損：{round(long_sl, 4)}（前5根最低）\n"
                f"止盈(1:1)：{long_tp}"
            )
            print(f"[{tw_str()}][{name}][{timeframe}] 🟢 做多 入場:{entry_price} SL:{round(long_sl,4)}")
            last_signal[key] = "long"
        else:
            print(f"[{tw_str()}][{name}][{timeframe}] 做多（重複，略過）")
    elif bearTrend and shortC1 and shortC2 and shortC3:
        if prev_signal != "short":
            send_tg(
                f"🔴 賽克斯做空訊號\n"
                f"幣種：{name}\n"
                f"時框：{timeframe}\n"
                f"K棒收盤：{candle_close_tw}（台灣時間）\n"
                f"入場價：{entry_price}\n"
                f"止損：{round(short_sl, 4)}（前5根最高）\n"
                f"止盈(1:1)：{short_tp}"
            )
            print(f"[{tw_str()}][{name}][{timeframe}] 🔴 做空 入場:{entry_price} SL:{round(short_sl,4)}")
            last_signal[key] = "short"
        else:
            print(f"[{tw_str()}][{name}][{timeframe}] 做空（重複，略過）")
    else:
        if prev_signal is not None:
            last_signal[key] = None
        print(f"[{tw_str()}][{name}][{timeframe}] 無訊號")


def check_timeframes(exchange, due_timeframes):
    for tf in due_timeframes:
        print(f"\n[{tw_str()}] ── 開始掃描 {tf} K棒 ──")
        for symbol in SYMBOLS:
            check_signal(exchange, symbol, tf)
            sleep(0.3)
    print(f"[{tw_str()}] ── 本輪掃描完畢 ──\n")


# ══════════════════════════
# 啟動
# ══════════════════════════
Thread(target=run_web, daemon=True).start()

send_tg(
    f"✅ 賽克斯訊號機器人已啟動\n"
    f"台灣時間：{tw_str()}\n"
    f"交易所：OKX 永續合約\n"
    f"時框：15m、30m、1h、4h\n"
    f"掃描時機：每根K棒收盤後5秒\n"
    f"監控幣種：" + "、".join(s.split("/")[0] for s in SYMBOLS)
)

exchange = ccxt.okx()
while True:
    try:
        due = wait_for_next_candle()
        print(f"[{tw_str()}] 觸發時框：{', '.join(due)}")
        check_timeframes(exchange, due)
    except Exception as e:
        err_msg = f"[{tw_str()}] ⚠️ 錯誤：{e}"
        print(err_msg)
        send_tg(f"⚠️ 腳本錯誤：{e}")
        sleep(60)
