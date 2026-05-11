import os
import requests
import pandas as pd
import ccxt
from flask import Flask
from threading import Thread
from time import sleep
from datetime import datetime, timezone

# ══════════════════════════
# 設定區
# ══════════════════════════
TG_TOKEN   = "7642408367:AAG_6HS6BLeHtST2cKjNjaU6Ajpmbe_cj8w"
TG_CHAT_ID = "8799334828"
COINALYZE_KEY = "82087740-b30d-479f-8846-5ffb51540b19"

TIMEFRAMES = ["15m", "30m", "1h", "4h"]
TF_SECONDS = {"15m": 900, "30m": 1800, "1h": 3600, "4h": 14400}

# OKX symbol -> Coinalyze aggregated symbol (.A = 跨所聚合)
SYMBOLS = {
    "BTC/USDT:USDT":  "BTCUSDT_PERP.A",
    "ETH/USDT:USDT":  "ETHUSDT_PERP.A",
    "XRP/USDT:USDT":  "XRPUSDT_PERP.A",
    "LTC/USDT:USDT":  "LTCUSDT_PERP.A",
    "ADA/USDT:USDT":  "ADAUSDT_PERP.A",
    "HYPE/USDT:USDT": "HYPEUSDT_PERP.A",
    "SUI/USDT:USDT":  "SUIUSDT_PERP.A",
    "DOGE/USDT:USDT": "DOGEUSDT_PERP.A",
    "LINK/USDT:USDT": "LINKUSDT_PERP.A",
    "XAU/USDT:USDT":  "XAUUSDT_PERP.A",
    "ZEC/USDT:USDT":  "ZECUSDT_PERP.A",
    "H/USDT:USDT":    "HUSDT_PERP.A",
}

# ══════════════════════════
# 過濾參數（可調）
# ══════════════════════════
FUNDING_LONG_MAX  =  0.0003
FUNDING_SHORT_MIN = -0.0003
CVD_LOOKBACK      = 3
OI_LOOKBACK       = 3
# ══════════════════════════

last_signal = {}
COINALYZE_BASE = "https://api.coinalyze.net/v1"
TF_MAP = {"15m": "15min", "30m": "30min", "1h": "1hour", "4h": "4hour"}

app = Flask(__name__)

@app.route("/")
def home():
    return "賽克斯訊號機器人運行中 v3"

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

def coinalyze_get(endpoint, params):
    try:
        r = requests.get(
            f"{COINALYZE_BASE}/{endpoint}",
            headers={"api_key": COINALYZE_KEY},
            params=params,
            timeout=10
        )
        if r.status_code == 200:
            return r.json()
        print(f"  [Coinalyze/{endpoint}] HTTP {r.status_code}")
        return None
    except Exception as e:
        print(f"  [Coinalyze/{endpoint}] 錯誤：{e}")
        return None

def get_coinalyze_data(cl_symbol, timeframe, lookback_bars=6):
    interval = TF_MAP.get(timeframe, "1hour")
    tf_sec   = TF_SECONDS[timeframe]
    now      = int(datetime.now(timezone.utc).timestamp())
    frm      = now - tf_sec * (lookback_bars + 2)

    params = {
        "symbols": cl_symbol,
        "interval": interval,
        "from": frm,
        "to": now,
        "convert_to_usd": "true"
    }

    # OI history
    oi_data = coinalyze_get("open-interest-history", params)
    oi_closes = []
    if oi_data and len(oi_data) > 0 and "history" in oi_data[0]:
        oi_closes = [bar["c"] for bar in oi_data[0]["history"]]

    # OHLCV for CVD (buy vol - sell vol cumulative)
    ohlcv_data = coinalyze_get("ohlcv-history", params)
    cvd_vals = []
    if ohlcv_data and len(ohlcv_data) > 0 and "history" in ohlcv_data[0]:
        cumulative = 0
        for bar in ohlcv_data[0]["history"]:
            buy_vol  = bar.get("bv", 0)
            sell_vol = bar.get("v", 0) - buy_vol
            delta    = buy_vol - sell_vol
            cumulative += delta
            cvd_vals.append(cumulative)

    return oi_closes, cvd_vals

def check_cvd_oi_absorption(cl_symbol, timeframe, direction):
    """
    秋總邏輯：吸收背離 + 持倉上升
    做多：合約CVD止跌翻上 + OI上升 → 新多單吸收賣壓
    做空：合約CVD止漲翻下 + OI上升 → 新空單吸收買壓
    """
    oi_closes, cvd_vals = get_coinalyze_data(cl_symbol, timeframe, lookback_bars=OI_LOOKBACK + 2)

    if len(oi_closes) < OI_LOOKBACK or len(cvd_vals) < CVD_LOOKBACK:
        return True, "數據不足，略過CVD/OI過濾"

    recent_oi  = oi_closes[-OI_LOOKBACK:]
    recent_cvd = cvd_vals[-CVD_LOOKBACK:]

    oi_rising = recent_oi[-1] > recent_oi[0]

    if direction == "long":
        cvd_turned_up = recent_cvd[-1] > recent_cvd[-2] and recent_cvd[-2] <= recent_cvd[0]
        if not cvd_turned_up:
            return False, "合約CVD未向上翻轉（無買方吸收訊號）"
        if not oi_rising:
            return False, "OI未上升（無新多單進場，疑似空單平倉）"
        return True, "CVD↑翻轉+OI↑（吸收背離確認）"
    else:
        cvd_turned_down = recent_cvd[-1] < recent_cvd[-2] and recent_cvd[-2] >= recent_cvd[0]
        if not cvd_turned_down:
            return False, "合約CVD未向下翻轉（無空方吸收訊號）"
        if not oi_rising:
            return False, "OI未上升（無新空單進場，疑似多單平倉）"
        return True, "CVD↓翻轉+OI↑（空方吸收確認）"

def get_funding_rate(exchange, symbol):
    try:
        info = exchange.fetch_funding_rate(symbol)
        return float(info["fundingRate"])
    except Exception as e:
        print(f"  [Funding] 取得失敗：{e}")
        return None

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
    cl_symbol = SYMBOLS.get(symbol)
    if not cl_symbol:
        print(f"[{symbol}] 無對應 Coinalyze symbol，略過")
        return

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

    # 動態判斷哪一根是「剛收盤的棒」
    # OKX 可能已開新棒（df[-1]=新棒，df[-2]=剛收盤）
    # 或還沒開新棒（df[-1]=剛收盤，df[-2]=上一根）
    tf_ms = TF_SECONDS[timeframe] * 1000
    last_bar_open_ms = df.iloc[-1]["time"]
    now_ms = datetime.now(timezone.utc).timestamp() * 1000
    if last_bar_open_ms + tf_ms > now_ms:
        # df[-1] 是尚未收盤的新棒 → df[-2] 才是剛收盤的棒
        ci, pi, sl_s = -2, -3, slice(-7, -2)
    else:
        # df[-1] 就是剛收盤的棒（OKX 還沒開新棒）
        ci, pi, sl_s = -1, -2, slice(-6, -1)

    last = df.iloc[ci]
    prev = df.iloc[pi]

    bullTrend   = last["ema144"] > last["ema576"]
    bearTrend   = last["ema144"] < last["ema576"]
    qqeTurnBlue = last["rsiMa"] >= 50 and prev["rsiMa"] < 50
    qqeTurnRed  = last["rsiMa"] < 50  and prev["rsiMa"] >= 50

    longC1 = (
        last["close"] > last["largeTop"]
        and last["close"] > last["smallBot"]
        and last["low"]  < last["smallBot"]
    )
    longC2  = last["close"] > last["ema12"]
    longC3  = qqeTurnBlue
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
    long_sl  = df.iloc[sl_s]["low"].min()   # 前5根已收盤最低點
    short_sl = df.iloc[sl_s]["high"].max()  # 前5根已收盤最高點
    long_tp  = round(entry_price + (entry_price - long_sl), 4)
    short_tp = round(entry_price - (short_sl - entry_price), 4)

    is_long_signal  = bullTrend and longC1  and longC2  and longC3
    is_short_signal = bearTrend and shortC1 and shortC2 and shortC3

    if not is_long_signal and not is_short_signal:
        if prev_signal is not None:
            last_signal[key] = None
        print(f"[{name}][{timeframe}] 無訊號")
        return

    direction = "long" if is_long_signal else "short"

    # 第一層：Funding Rate
    funding = get_funding_rate(exchange, symbol)
    reject_reasons = []
    if funding is not None:
        if is_long_signal  and funding > FUNDING_LONG_MAX:
            reject_reasons.append(f"Funding過高({funding:.4%})")
        if is_short_signal and funding < FUNDING_SHORT_MIN:
            reject_reasons.append(f"Funding過低({funding:.4%})")

    # 第二層：CVD + OI 吸收背離（秋總邏輯）
    cvd_oi_pass, cvd_oi_reason = check_cvd_oi_absorption(cl_symbol, timeframe, direction)
    if not cvd_oi_pass:
        reject_reasons.append(cvd_oi_reason)

    if reject_reasons:
        reason_str = "、".join(reject_reasons)
        print(f"[{name}][{timeframe}] ⛔ 過濾：{reason_str}")
        send_tg(
            f"⛔ 訊號被過濾\n"
            f"幣種：{name}  時框：{timeframe}\n"
            f"方向：{'做多' if is_long_signal else '做空'}\n"
            f"原因：{reason_str}"
        )
        return

    funding_str = f"{funding:.4%}" if funding is not None else "N/A"

    if is_long_signal:
        if prev_signal != "long":
            send_tg(
                f"🟢 賽克斯做多訊號\n"
                f"幣種：{name}\n"
                f"時框：{timeframe}\n"
                f"入場價：{entry_price}\n"
                f"止損：{round(long_sl, 4)}（前5根最低）\n"
                f"止盈(1:1)：{long_tp}\n"
                f"──────────\n"
                f"Funding：{funding_str}\n"
                f"CVD+OI：{cvd_oi_reason}"
            )
            print(f"[{name}][{timeframe}] 🟢 做多 入場:{entry_price} SL:{round(long_sl,4)} TP:{long_tp}")
            last_signal[key] = "long"
        else:
            print(f"[{name}][{timeframe}] 做多（重複，略過）")

    elif is_short_signal:
        if prev_signal != "short":
            send_tg(
                f"🔴 賽克斯做空訊號\n"
                f"幣種：{name}\n"
                f"時框：{timeframe}\n"
                f"入場價：{entry_price}\n"
                f"止損：{round(short_sl, 4)}（前5根最高）\n"
                f"止盈(1:1)：{short_tp}\n"
                f"──────────\n"
                f"Funding：{funding_str}\n"
                f"CVD+OI：{cvd_oi_reason}"
            )
            print(f"[{name}][{timeframe}] 🔴 做空 入場:{entry_price} SL:{round(short_sl,4)} TP:{short_tp}")
            last_signal[key] = "short"
        else:
            print(f"[{name}][{timeframe}] 做空（重複，略過）")

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
            sleep(0.5)
    print("── 本輪掃描完畢 ──\n")

# ══════════════════════════
# 啟動
# ══════════════════════════
Thread(target=run_web, daemon=True).start()

send_tg(
    "✅ 賽克斯訊號機器人已啟動 v3\n"
    "交易所：OKX 永續合約\n"
    "時框：15m、30m、1h、4h\n"
    "過濾層：\n"
    "  1️⃣ Funding Rate 極端值\n"
    "  2️⃣ CVD翻轉 + OI上升（吸收背離）\n"
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