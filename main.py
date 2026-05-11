import os
import requests
import pandas as pd
import ccxt
from flask import Flask
from threading import Thread
from time import sleep
from datetime import datetime, timezone

# ══════════════════════════
# 填入你的設定
# ══════════════════════════
TG_TOKEN   = "7642408367:AAG_6HS6BLeHtST2cKjNjaU6Ajpmbe_cj8w"
TG_CHAT_ID = "8799334828"

TIMEFRAMES = ["15m", "30m", "1h", "4h"]
TF_SECONDS = {"15m": 900, "30m": 1800, "1h": 3600, "4h": 14400}

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

last_signal = {}

app = Flask(__name__)


@app.route("/")
def home():
    return "賽克斯訊號機器人運行中"


def run_web():
    port = int(os.environ.get("PORT", 3000))
    app.run(host="0.0.0.0", port=port)


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
        print(f"[{symbol}][{timeframe}] 取得資料失敗：{e}")
        return
    if len(ohlcv) < 700:
        print(f"[{symbol}][{timeframe}] 資料不足（{len(ohlcv)} 根），略過")
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

    # 用已收盤的K棒（倒數第2根）做判斷，避免用到未收盤的當前棒
    last = df.iloc[-2]
    prev = df.iloc[-3]

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
    long_sl     = df.iloc[-7:-2]["low"].min()   # 前5根已收盤的最低點
    short_sl    = df.iloc[-7:-2]["high"].max()  # 前5根已收盤的最高點
    long_tp     = round(entry_price + (entry_price - long_sl), 4)
    short_tp    = round(entry_price - (short_sl - entry_price), 4)

    if bullTrend and longC1 and longC2 and longC3:
        if prev_signal != "long":
            send_tg(
                f"🟢 賽克斯做多訊號\n"
                f"幣種：{name}\n"
                f"時框：{timeframe}\n"
                f"入場價：{entry_price}\n"
                f"止損：{round(long_sl, 4)}（前5根最低）\n"
                f"止盈(1:1)：{long_tp}"
            )
            print(f"[{name}][{timeframe}] 🟢 做多 入場:{entry_price} SL:{round(long_sl,4)} TP:{long_tp}")
            last_signal[key] = "long"
        else:
            print(f"[{name}][{timeframe}] 做多（重複，略過）")
    elif bearTrend and shortC1 and shortC2 and shortC3:
        if prev_signal != "short":
            send_tg(
                f"🔴 賽克斯做空訊號\n"
                f"幣種：{name}\n"
                f"時框：{timeframe}\n"
                f"入場價：{entry_price}\n"
                f"止損：{round(short_sl, 4)}（前5根最高）\n"
                f"止盈(1:1)：{short_tp}"
            )
            print(f"[{name}][{timeframe}] 🔴 做空 入場:{entry_price} SL:{round(short_sl,4)} TP:{short_tp}")
            last_signal[key] = "short"
        else:
            print(f"[{name}][{timeframe}] 做空（重複，略過）")
    else:
        if prev_signal is not None:
            last_signal[key] = None
        print(f"[{name}][{timeframe}] 無訊號")


# ══════════════════════════
# K 棒收盤對齊排程
# ══════════════════════════
def next_close_ts(timeframe: str) -> float:
    period = TF_SECONDS[timeframe]
    now_ts = datetime.now(timezone.utc).timestamp()
    return (int(now_ts) // period + 1) * period


def wait_for_next_candle():
    triggers = {tf: next_close_ts(tf) + 5 for tf in TIMEFRAMES}
    earliest = min(triggers.values())

    while True:
        wait = earliest - datetime.now(timezone.utc).timestamp() - 0.5
        if wait <= 0:
            break
        sleep(min(wait, 30))

    while datetime.now(timezone.utc).timestamp() < earliest:
        sleep(0.05)

    due = [tf for tf, ts in triggers.items() if abs(ts - earliest) <= 15]
    return due


def check_timeframes(exchange, due_timeframes):
    for tf in due_timeframes:
        print(f"\n── 掃描 {tf} K棒 ──")
        for symbol in SYMBOLS:
            check_signal(exchange, symbol, tf)
            sleep(0.3)
    print("── 本輪掃描完畢 ──\n")


# ══════════════════════════
# 啟動
# ══════════════════════════
Thread(target=run_web, daemon=True).start()

send_tg(
    "✅ 賽克斯訊號機器人已啟動\n"
    "交易所：OKX 永續合約\n"
    "時框：15m、30m、1h、4h\n"
    "掃描時機：每根K棒收盤後5秒\n"
    "監控幣種：" + "、".join(s.split("/")[0] for s in SYMBOLS)
)

exchange = ccxt.okx()
while True:
    try:
        due = wait_for_next_candle()
        print(f"觸發時框：{', '.join(due)}")
        check_timeframes(exchange, due)
    except Exception as e:
        print(f"錯誤：{e}")
        send_tg(f"⚠️ 腳本錯誤：{e}")
        sleep(60)
