import requests
import pandas as pd
import ccxt
from time import sleep

# ══════════════════════════
# 填入你的設定
# ══════════════════════════
TG_TOKEN = "7642408367:AAG_6HS6BLeHtST2cKjNjaU6Ajpmbe_cj8w"
TG_CHAT_ID = "150066168"

# 要監控的幣種和時框架
SYMBOL = "SOL/USDT"
TIMEFRAME = "1h"
# ══════════════════════════


def send_tg(msg):
    url = f"https://api.telegram.org/bot{TG_TOKEN}/sendMessage"
    try:
        requests.post(url, data={"chat_id": TG_CHAT_ID, "text": msg})
    except:
        pass


def calc_rsi(series, period=14):
    delta = series.diff()
    gain = delta.clip(lower=0).ewm(com=period - 1, adjust=False).mean()
    loss = (-delta.clip(upper=0)).ewm(com=period - 1, adjust=False).mean()
    rs = gain / loss
    return 100 - (100 / (1 + rs))


def check_signal():
    exchange = ccxt.binance()
    ohlcv = exchange.fetch_ohlcv(SYMBOL, TIMEFRAME, limit=700)
    df = pd.DataFrame(ohlcv, columns=["time", "open", "high", "low", "close", "vol"])

    # EMA
    df["ema12"] = df["close"].ewm(span=12, adjust=False).mean()
    df["ema144"] = df["close"].ewm(span=144, adjust=False).mean()
    df["ema169"] = df["close"].ewm(span=169, adjust=False).mean()
    df["ema576"] = df["close"].ewm(span=576, adjust=False).mean()
    df["ema676"] = df["close"].ewm(span=676, adjust=False).mean()

    df["smallTop"] = df[["ema144", "ema169"]].max(axis=1)
    df["smallBot"] = df[["ema144", "ema169"]].min(axis=1)
    df["largeTop"] = df[["ema576", "ema676"]].max(axis=1)
    df["largeBot"] = df[["ema576", "ema676"]].min(axis=1)

    # QQE MOD
    df["rsi"] = calc_rsi(df["close"], 14)
    df["rsiMa"] = df["rsi"].ewm(span=5, adjust=False).mean()

    last = df.iloc[-1]
    prev = df.iloc[-2]

    bullTrend = last["ema144"] > last["ema576"]
    bearTrend = last["ema144"] < last["ema576"]
    qqeTurnBlue = last["rsiMa"] >= 50 and prev["rsiMa"] < 50
    qqeTurnRed = last["rsiMa"] < 50 and prev["rsiMa"] >= 50

    longC1 = (
        last["close"] > last["largeTop"]
        and last["close"] > last["smallBot"]
        and last["low"] < last["smallBot"]
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

    if bullTrend and longC1 and longC2 and longC3:
        send_tg(f"🟢 賽克斯做多訊號\n幣種：{SYMBOL}\n時框：{TIMEFRAME}\n請確認進場條件")
        print("做多訊號已發送")

    if bearTrend and shortC1 and shortC2 and shortC3:
        send_tg(f"🔴 賽克斯做空訊號\n幣種：{SYMBOL}\n時框：{TIMEFRAME}\n請確認進場條件")
        print("做空訊號已發送")

    print("檢查完畢，等待下次...")


# 啟動
send_tg("✅ 賽克斯訊號機器人已啟動")
while True:
    try:
        check_signal()
    except Exception as e:
        print(f"錯誤：{e}")
        send_tg(f"⚠️ 腳本錯誤：{e}")
    sleep(3600)
