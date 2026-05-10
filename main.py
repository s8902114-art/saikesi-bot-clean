import os
import requests
import pandas as pd
import ccxt
from flask import Flask
from threading import Thread
from time import sleep

# ══════════════════════════
# 填入你的設定
# ══════════════════════════
TG_TOKEN = "7642408367:AAG_6HS6BLeHtST2cKjNjaU6Ajpmbe_cj8w"
TG_CHAT_ID = "8799334828"

# 監控時框
TIMEFRAMES = ["15m", "30m", "1h", "4h"]

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
    return "Bot 運行中"


def run_web():
    port = int(os.environ.get("PORT", 3000))
    app.run(host="0.0.0.0", port=port)


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


def check_signal(exchange, symbol, timeframe):
    try:
        ohlcv = exchange.fetch_ohlcv(symbol, timeframe, limit=700)
    except Exception as e:
        print(f"[{symbol}][{timeframe}] 取得資料失敗：{e}")
        return

    df = pd.DataFrame(ohlcv, columns=["time", "open", "high", "low", "close", "vol"])

    df["ema12"] = df["close"].ewm(span=12, adjust=False).mean()
    df["ema144"] = df["close"].ewm(span=144, adjust=False).mean()
    df["ema169"] = df["close"].ewm(span=169, adjust=False).mean()
    df["ema576"] = df["close"].ewm(span=576, adjust=False).mean()
    df["ema676"] = df["close"].ewm(span=676, adjust=False).mean()

    df["smallTop"] = df[["ema144", "ema169"]].max(axis=1)
    df["smallBot"] = df[["ema144", "ema169"]].min(axis=1)
    df["largeTop"] = df[["ema576", "ema676"]].max(axis=1)
    df["largeBot"] = df[["ema576", "ema676"]].min(axis=1)

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

    name = symbol.split("/")[0]
    key = (symbol, timeframe)
    prev_signal = last_signal.get(key)

    if bullTrend and longC1 and longC2 and longC3:
        if prev_signal != "long":
            send_tg(
                f"🟢 賽克斯做多訊號\n幣種：{name}\n時框：{timeframe}\n請確認進場條件"
            )
            print(f"[{name}][{timeframe}] 做多訊號已發送")
            last_signal[key] = "long"
        else:
            print(f"[{name}][{timeframe}] 做多訊號（重複，略過）")
    elif bearTrend and shortC1 and shortC2 and shortC3:
        if prev_signal != "short":
            send_tg(
                f"🔴 賽克斯做空訊號\n幣種：{name}\n時框：{timeframe}\n請確認進場條件"
            )
            print(f"[{name}][{timeframe}] 做空訊號已發送")
            last_signal[key] = "short"
        else:
            print(f"[{name}][{timeframe}] 做空訊號（重複，略過）")
    else:
        if prev_signal is not None:
            last_signal[key] = None
        print(f"[{name}][{timeframe}] 無訊號")


def check_all():
    exchange = ccxt.okx()
    for symbol in SYMBOLS:
        for timeframe in TIMEFRAMES:
            check_signal(exchange, symbol, timeframe)
            sleep(0.3)
    print("── 本輪檢查完畢，等待下次... ──\n")


# 啟動 Flask（背景執行）
Thread(target=run_web, daemon=True).start()

# 啟動通知
send_tg(
    "✅ 賽克斯訊號機器人已啟動\n"
    "交易所：OKX 永續合約\n"
    "時框：15m、30m、1h、4h\n"
    "監控幣種：" + "、".join(s.split("/")[0] for s in SYMBOLS)
)

while True:
    try:
        check_all()
    except Exception as e:
        print(f"錯誤：{e}")
        send_tg(f"⚠️ 腳本錯誤：{e}")
    sleep(900)
