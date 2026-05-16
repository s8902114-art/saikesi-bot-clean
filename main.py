import os
import json
import requests
import pandas as pd
import ccxt
from flask import Flask
from threading import Thread
from time import sleep
from datetime import datetime, timezone, timedelta

# =====================
# 設定區
# =====================
TG_TOKEN      = "7642408367:AAG_6HS6BLeHtST2cKjNjaU6Ajpmbe_cj8w"
TG_CHAT_ID    = "8799334828"
COINALYZE_KEY = "82087740-b30d-479f-8846-5ffb51540b19"

OKX_API_KEY    = os.environ.get("OKX_API_KEY", "")
OKX_SECRET_KEY = os.environ.get("OKX_SECRET_KEY", "")
OKX_PASSPHRASE = os.environ.get("OKX_PASSPHRASE", "")

ORDER_LEVERAGE   = 5       # 槓桿倍數
ORDER_PCT        = 0.10    # 用帳戶餘額的 10%

# 暫存待確認訂單  callback_data -> {symbol, direction, entry, sl, tp1, tp2, be_sl}
pending_orders = {}

TIMEFRAMES = ["15m", "30m", "1h", "4h"]
TF_SECONDS = {"15m": 900, "30m": 1800, "1h": 3600, "4h": 14400}
TF_MAP     = {"15m": "15min", "30m": "30min", "1h": "1hour", "4h": "4hour"}

FUNDING_LONG_MAX  =  0.0003
FUNDING_SHORT_MIN = -0.0003
CVD_LOOKBACK      = 3
OI_LOOKBACK       = 3
TAKER_FEE         = 0.0005   # 0.05% Taker 手續費（雙邊 = 0.1%）
SYMBOLS_FILE      = "symbols_cache.json"
COINALYZE_BASE    = "https://api.coinalyze.net/v1"

last_signal = {}
SYMBOLS = {}

app = Flask(__name__)

@app.route("/")
def home():
    return f"Saikesi Bot v4 | {len(SYMBOLS)} symbols"

def run_web():
    port = int(os.environ.get("PORT", 3000))
    app.run(host="0.0.0.0", port=port)

def send_tg(msg):
    try:
        requests.post(
            f"https://api.telegram.org/bot{TG_TOKEN}/sendMessage",
            data={"chat_id": TG_CHAT_ID, "text": msg}, timeout=10)
    except Exception:
        pass

def send_tg_with_buttons(msg, callback_data):
    try:
        keyboard = {
            "inline_keyboard": [[
                {"text": "✅ 確認下單", "callback_data": f"confirm_{callback_data}"},
                {"text": "❌ 跳過",     "callback_data": f"skip_{callback_data}"}
            ]]
        }
        requests.post(
            f"https://api.telegram.org/bot{TG_TOKEN}/sendMessage",
            json={"chat_id": TG_CHAT_ID, "text": msg,
                  "reply_markup": keyboard}, timeout=10)
    except Exception as e:
        print(f"[TG按鈕] 發送失敗: {e}")

def answer_callback(callback_query_id, text=""):
    try:
        requests.post(
            f"https://api.telegram.org/bot{TG_TOKEN}/answerCallbackQuery",
            json={"callback_query_id": callback_query_id, "text": text}, timeout=10)
    except Exception:
        pass

# =====================
# OKX 下單
# =====================
def place_okx_order(symbol, direction):
    try:
        ex = ccxt.okx({
            "apiKey":     OKX_API_KEY,
            "secret":     OKX_SECRET_KEY,
            "password":   OKX_PASSPHRASE,
            "options":    {"defaultType": "swap"},
        })
        ex.load_markets()

        # 取得 USDT 餘額
        balance = ex.fetch_balance()
        usdt = balance["USDT"]["free"] if "USDT" in balance else 0
        if usdt <= 0:
            send_tg("⚠️ 下單失敗：帳戶 USDT 餘額不足")
            return

        # 設定槓桿
        ex.set_leverage(ORDER_LEVERAGE, symbol)

        # 計算下單張數（以 USDT 計算名義價值）
        ticker = ex.fetch_ticker(symbol)
        price  = ticker["last"]
        notional = usdt * ORDER_PCT * ORDER_LEVERAGE
        market = ex.market(symbol)
        contract_size = market.get("contractSize", 1)
        amount = notional / price / contract_size
        amount = round(amount, market.get("precision", {}).get("amount", 3))
        if amount <= 0:
            send_tg("⚠️ 下單失敗：計算張數為 0，請確認帳戶餘額")
            return

        side = "buy" if direction == "long" else "sell"
        order = ex.create_market_order(symbol, side, amount)
        order_id = order.get("id", "N/A")
        filled   = order.get("average") or price

        send_tg(
            f"✅ 下單成功\n"
            f"幣種：{symbol}\n"
            f"方向：{'做多' if direction == 'long' else '做空'}\n"
            f"槓桿：{ORDER_LEVERAGE}x  |  張數：{amount}\n"
            f"成交均價：{filled}\n"
            f"USDT 使用：{round(usdt * ORDER_PCT, 2)}\n"
            f"訂單ID：{order_id}"
        )
        print(f"[OKX] 下單成功 {symbol} {side} {amount}張 @ {filled}")

    except Exception as e:
        send_tg(f"⚠️ 下單失敗：{e}")
        print(f"[OKX] 下單失敗: {e}")

# =====================
# Telegram 回調輪詢
# =====================
_tg_offset = 0

def poll_tg_callbacks():
    global _tg_offset
    while True:
        try:
            r = requests.get(
                f"https://api.telegram.org/bot{TG_TOKEN}/getUpdates",
                params={"offset": _tg_offset, "timeout": 30,
                        "allowed_updates": ["callback_query"]},
                timeout=40)
            data = r.json()
            for update in data.get("result", []):
                _tg_offset = update["update_id"] + 1
                cq = update.get("callback_query")
                if not cq:
                    continue
                cb_data  = cq.get("data", "")
                cq_id    = cq["id"]

                if cb_data.startswith("confirm_"):
                    key = cb_data[len("confirm_"):]
                    order = pending_orders.pop(key, None)
                    if order:
                        answer_callback(cq_id, "下單中...")
                        Thread(target=place_okx_order,
                               args=(order["symbol"], order["direction"]),
                               daemon=True).start()
                    else:
                        answer_callback(cq_id, "訊號已過期")

                elif cb_data.startswith("skip_"):
                    key = cb_data[len("skip_"):]
                    pending_orders.pop(key, None)
                    answer_callback(cq_id, "已跳過")
                    send_tg("❌ 已跳過此訊號")

        except Exception as e:
            print(f"[TG輪詢] 錯誤: {e}")
            sleep(5)

# =====================
# 市值前100 幣單管理
# =====================
def fetch_top100_coingecko():
    try:
        r = requests.get(
            "https://api.coingecko.com/api/v3/coins/markets",
            params={"vs_currency": "usd", "order": "market_cap_desc",
                    "per_page": 100, "page": 1, "sparkline": False},
            timeout=15)
        if r.status_code == 200:
            return [c["symbol"].upper() for c in r.json()]
        print(f"[CoinGecko] HTTP {r.status_code}")
        return []
    except Exception as e:
        print(f"[CoinGecko] 錯誤: {e}")
        return []

def build_symbol_map(exchange, top100):
    okx_swaps = {
        info["base"]: sym
        for sym, info in exchange.markets.items()
        if info.get("swap") and info.get("quote") == "USDT"
           and info.get("settle") == "USDT" and info.get("active")
    }
    r = requests.get(f"{COINALYZE_BASE}/future-markets",
                     headers={"api_key": COINALYZE_KEY}, timeout=15)
    cl_bases = set()
    if r.status_code == 200:
        for item in r.json():
            if item.get("symbol", "").endswith(".A"):
                cl_bases.add(item.get("base_asset", "").upper())
    result = {}
    for base in top100:
        if base in okx_swaps and base in cl_bases:
            result[okx_swaps[base]] = f"{base}USDT_PERP.A"
    return result

def load_cache():
    if not os.path.exists(SYMBOLS_FILE):
        return None, None
    try:
        with open(SYMBOLS_FILE) as f:
            data = json.load(f)
        return data["symbols"], datetime.fromisoformat(data["updated_at"])
    except Exception:
        return None, None

def save_cache(symbols):
    with open(SYMBOLS_FILE, "w") as f:
        json.dump({"updated_at": datetime.now(timezone.utc).isoformat(),
                   "symbols": symbols}, f, indent=2)

FALLBACK_SYMBOLS = {
    "BTC/USDT:USDT": "BTCUSDT_PERP.A", "ETH/USDT:USDT": "ETHUSDT_PERP.A",
    "SOL/USDT:USDT": "SOLUSDT_PERP.A", "XRP/USDT:USDT": "XRPUSDT_PERP.A",
    "BNB/USDT:USDT": "BNBUSDT_PERP.A", "ADA/USDT:USDT": "ADAUSDT_PERP.A",
    "DOGE/USDT:USDT":"DOGEUSDT_PERP.A","SUI/USDT:USDT":  "SUIUSDT_PERP.A",
    "LINK/USDT:USDT":"LINKUSDT_PERP.A","H/USDT:USDT":    "HUSDT_PERP.A",
}

def update_symbols(exchange, force=False):
    global SYMBOLS
    cached, updated_at = load_cache()
    need_update = force or cached is None
    if not need_update and updated_at:
        age = datetime.now(timezone.utc) - updated_at.replace(tzinfo=timezone.utc)
        if age > timedelta(days=7):
            need_update = True
    if not need_update and cached:
        SYMBOLS = cached
        print(f"[幣單] 快取 {len(SYMBOLS)} 個幣（上次更新 {updated_at.date()}）")
        return
    print("[幣單] 正在抓取市值前100...")
    top100 = fetch_top100_coingecko()
    if not top100:
        SYMBOLS = cached if cached else FALLBACK_SYMBOLS
        print(f"[幣單] CoinGecko失敗，使用{'快取' if cached else '備援'}清單 {len(SYMBOLS)}個")
        return
    new_map = build_symbol_map(exchange, top100)
    if new_map:
        SYMBOLS = new_map
        save_cache(SYMBOLS)
        print(f"[幣單] 更新完成，共 {len(SYMBOLS)} 個幣")
        send_tg(f"📋 幣單已更新\n共 {len(SYMBOLS)} 個幣（市值前100 x OKX x Coinalyze交集）\n"
                + "、".join(s.split("/")[0] for s in list(SYMBOLS)[:20]) + "...")
    else:
        SYMBOLS = cached if cached else FALLBACK_SYMBOLS
        print("[幣單] 比對失敗，使用備援")

def weekly_symbol_updater(exchange):
    while True:
        sleep(60 * 60 * 24 * 7)   # 每7天
        print("[幣單] 每週自動更新...")
        update_symbols(exchange, force=True)

# =====================
# 技術指標
# =====================
def calc_rsi(series, period=14):
    delta = series.diff()
    gain = delta.clip(lower=0).ewm(com=period-1, adjust=False).mean()
    loss = (-delta.clip(upper=0)).ewm(com=period-1, adjust=False).mean()
    return 100 - (100 / (1 + gain/loss))

def find_structure_sl(df, direction, lookback=30):
    """
    找最近的結構性支撐/壓力點作為止損
    做多：找最近一個局部低點（pivot low，比左右各2根都低）
    做空：找最近一個局部高點（pivot high，比左右各2根都高）
    """
    window = df.iloc[-lookback-2:-2].copy().reset_index(drop=True)
    n = len(window)
    pivot_points = []

    if direction == "long":
        for i in range(2, n-2):
            lo = window.iloc[i]["low"]
            if (lo < window.iloc[i-1]["low"] and lo < window.iloc[i-2]["low"] and
                lo < window.iloc[i+1]["low"] and lo < window.iloc[i+2]["low"]):
                pivot_points.append(lo)
        if pivot_points:
            return pivot_points[-1]  # 最近的結構低點
        return df.iloc[-7:-2]["low"].min()   # 備援：前5根最低

    else:  # short
        for i in range(2, n-2):
            hi = window.iloc[i]["high"]
            if (hi > window.iloc[i-1]["high"] and hi > window.iloc[i-2]["high"] and
                hi > window.iloc[i+1]["high"] and hi > window.iloc[i+2]["high"]):
                pivot_points.append(hi)
        if pivot_points:
            return pivot_points[-1]  # 最近的結構高點
        return df.iloc[-7:-2]["high"].max()  # 備援：前5根最高

def calc_atr(df, period=14):
    hi, lo, cl = df["high"], df["low"], df["close"]
    tr = pd.concat([hi-lo, (hi-cl.shift()).abs(), (lo-cl.shift()).abs()], axis=1).max(axis=1)
    return tr.ewm(span=period, adjust=False).mean().iloc[-1]

# =====================
# Coinalyze
# =====================
def coinalyze_get(endpoint, params):
    try:
        r = requests.get(f"{COINALYZE_BASE}/{endpoint}",
                         headers={"api_key": COINALYZE_KEY},
                         params=params, timeout=10)
        return r.json() if r.status_code == 200 else None
    except Exception:
        return None

def get_coinalyze_data(cl_symbol, timeframe, lookback_bars=8):
    interval = TF_MAP.get(timeframe, "1hour")
    tf_sec   = TF_SECONDS[timeframe]
    now      = int(datetime.now(timezone.utc).timestamp())
    frm      = now - tf_sec * (lookback_bars + 2)
    params   = {"symbols": cl_symbol, "interval": interval,
                "from": frm, "to": now, "convert_to_usd": "true"}

    oi_data  = coinalyze_get("open-interest-history", params)
    oi_closes = ([bar["c"] for bar in oi_data[0]["history"]]
                 if oi_data and oi_data[0].get("history") else [])

    ohlcv_data = coinalyze_get("ohlcv-history", params)
    cvd_vals = []
    if ohlcv_data and ohlcv_data[0].get("history"):
        cum = 0
        for bar in ohlcv_data[0]["history"]:
            bv  = bar.get("bv", 0)
            delta = bv - (bar.get("v", 0) - bv)
            cum  += delta
            cvd_vals.append(cum)

    return oi_closes, cvd_vals

def check_cvd_oi(cl_symbol, timeframe, direction):
    oi_closes, cvd_vals = get_coinalyze_data(cl_symbol, timeframe)
    if len(oi_closes) < OI_LOOKBACK or len(cvd_vals) < CVD_LOOKBACK:
        return True, "數據不足，略過CVD/OI過濾"

    r_oi  = oi_closes[-OI_LOOKBACK:]
    r_cvd = cvd_vals[-CVD_LOOKBACK:]
    oi_rising = r_oi[-1] > r_oi[0]

    if direction == "long":
        turned = r_cvd[-1] > r_cvd[-2] and r_cvd[-2] <= r_cvd[0]
        if not turned:   return False, "合約CVD未向上翻轉"
        if not oi_rising: return False, "OI未上升（疑似空單平倉非新多進場）"
        return True, "CVD↑+OI↑（吸收確認）"
    else:
        turned = r_cvd[-1] < r_cvd[-2] and r_cvd[-2] >= r_cvd[0]
        if not turned:   return False, "合約CVD未向下翻轉"
        if not oi_rising: return False, "OI未上升（疑似多單平倉非新空進場）"
        return True, "CVD↓+OI↑（空方吸收確認）"

def get_funding_rate(exchange, symbol):
    try:
        return float(exchange.fetch_funding_rate(symbol)["fundingRate"])
    except Exception:
        return None

def fetch_ohlcv_paginated(exchange, symbol, timeframe, target=1500):
    all_ohlcv = exchange.fetch_ohlcv(symbol, timeframe, limit=300)
    if not all_ohlcv:
        return []
    tf_ms = exchange.parse_timeframe(timeframe) * 1000
    while len(all_ohlcv) < target:
        earliest = all_ohlcv[0][0]
        try:
            batch = exchange.fetch_ohlcv(symbol, timeframe,
                                         since=earliest - 300*tf_ms, limit=300)
        except Exception:
            break
        batch = [c for c in (batch or []) if c[0] < earliest]
        if not batch:
            break
        all_ohlcv = batch + all_ohlcv
        sleep(0.2)
    return all_ohlcv[-target:]

# =====================
# 主訊號邏輯
# =====================
def check_signal(exchange, symbol, timeframe):
    cl_symbol = SYMBOLS.get(symbol)
    if not cl_symbol:
        return

    try:
        ohlcv = fetch_ohlcv_paginated(exchange, symbol, timeframe, target=1500)
    except Exception as e:
        print(f"[{symbol}][{timeframe}] 資料失敗: {e}")
        return
    if len(ohlcv) < 700:
        return

    df = pd.DataFrame(ohlcv, columns=["time","open","high","low","close","vol"])
    df["ema12"]  = df["close"].ewm(span=12,  adjust=False).mean()
    df["ema144"] = df["close"].ewm(span=144, adjust=False).mean()
    df["ema169"] = df["close"].ewm(span=169, adjust=False).mean()
    df["ema576"] = df["close"].ewm(span=576, adjust=False).mean()
    df["ema676"] = df["close"].ewm(span=676, adjust=False).mean()

    df["smallTop"] = df[["ema144","ema169"]].max(axis=1)
    df["smallBot"] = df[["ema144","ema169"]].min(axis=1)
    df["largeTop"] = df[["ema576","ema676"]].max(axis=1)
    df["largeBot"] = df[["ema576","ema676"]].min(axis=1)

    df["rsi"]   = calc_rsi(df["close"], 14)
    df["rsiMa"] = df["rsi"].ewm(span=5, adjust=False).mean()

    # 動態判斷收盤棒
    tf_ms = TF_SECONDS[timeframe] * 1000
    now_ms = datetime.now(timezone.utc).timestamp() * 1000
    if df.iloc[-1]["time"] + tf_ms > now_ms:
        ci, pi = -2, -3
    else:
        ci, pi = -1, -2

    last = df.iloc[ci]
    prev = df.iloc[pi]

    bullTrend   = last["ema144"] > last["ema576"]
    bearTrend   = last["ema144"] < last["ema576"]
    qqeTurnBlue = last["rsiMa"] >= 50 and prev["rsiMa"] < 50
    qqeTurnRed  = last["rsiMa"] <  50 and prev["rsiMa"] >= 50

    longC1  = last["close"] > last["largeTop"] and last["close"] > last["smallBot"] and last["low"] < last["smallBot"]
    longC2  = last["close"] > last["ema12"]
    longC3  = qqeTurnBlue
    shortC1 = last["close"] < last["largeBot"] and last["close"] < last["smallTop"] and last["high"] > last["smallTop"]
    shortC2 = last["close"] < last["ema12"]
    shortC3 = qqeTurnRed

    is_long  = bullTrend and longC1  and longC2  and longC3
    is_short = bearTrend and shortC1 and shortC2 and shortC3

    name = symbol.split("/")[0]
    key  = (symbol, timeframe)
    prev_sig = last_signal.get(key)

    if not is_long and not is_short:
        if prev_sig is not None:
            last_signal[key] = None
        print(f"[{name}][{timeframe}] 無訊號")
        return

    direction = "long" if is_long else "short"

    # ── 過濾層 ──
    funding = get_funding_rate(exchange, symbol)
    rejects = []
    if funding is not None:
        if is_long  and funding > FUNDING_LONG_MAX:
            rejects.append(f"Funding過高({funding:.4%})")
        if is_short and funding < FUNDING_SHORT_MIN:
            rejects.append(f"Funding過低({funding:.4%})")

    cvd_pass, cvd_reason = check_cvd_oi(cl_symbol, timeframe, direction)
    if not cvd_pass:
        rejects.append(cvd_reason)

    if rejects:
        reason_str = "、".join(rejects)
        print(f"[{name}][{timeframe}] ⛔ {reason_str}")
        send_tg(f"⛔ 訊號被過濾\n幣種：{name}  時框：{timeframe}\n方向：{'做多' if is_long else '做空'}\n原因：{reason_str}")
        return

    # ── 多週期確認（15m / 30m 需符合 1h 趨勢）──
    if timeframe in ("15m", "30m"):
        try:
            ohlcv_1h = fetch_ohlcv_paginated(exchange, symbol, "1h", target=700)
            if len(ohlcv_1h) >= 200:
                df1h = pd.DataFrame(ohlcv_1h, columns=["time","open","high","low","close","vol"])
                ema144_1h = df1h["close"].ewm(span=144, adjust=False).mean().iloc[-1]
                ema576_1h = df1h["close"].ewm(span=576, adjust=False).mean().iloc[-1]
                trend_ok = (ema144_1h > ema576_1h) if is_long else (ema144_1h < ema576_1h)
                if not trend_ok:
                    print(f"[{name}][{timeframe}] ⛔ 1h趨勢不一致，略過")
                    send_tg(f"⛔ 訊號被過濾\n幣種：{name}  時框：{timeframe}\n方向：{'做多' if is_long else '做空'}\n原因：1h趨勢不一致")
                    return
        except Exception as e:
            print(f"[{name}][{timeframe}] 1h趨勢確認失敗（略過過濾）: {e}")

    # ── 止損止盈計算 ──
    entry   = last["close"]
    atr     = calc_atr(df.iloc[:ci])

    if direction == "long":
        sl_raw  = find_structure_sl(df.iloc[:ci+1], "long", lookback=30)
        sl      = round(sl_raw - atr * 0.1, 6)          # 結構低點再寬 0.1 ATR 緩衝
        risk    = entry - sl
        tp1     = round(entry + risk, 6)                  # 1:1  (50% 倉位)
        tp2     = round(entry + risk * 2, 6)              # 1:2  (剩50%)
        be_sl   = round(entry * (1 + TAKER_FEE * 2), 6)  # 止盈1後移動止損到成本+手續費
    else:
        sl_raw  = find_structure_sl(df.iloc[:ci+1], "short", lookback=30)
        sl      = round(sl_raw + atr * 0.1, 6)
        risk    = sl - entry
        tp1     = round(entry - risk, 6)
        tp2     = round(entry - risk * 2, 6)
        be_sl   = round(entry * (1 - TAKER_FEE * 2), 6)

    rr      = round(risk / entry * 100, 2)  # 止損距離 %
    funding_str = f"{funding:.4%}" if funding is not None else "N/A"

    if is_long:
        if prev_sig != "long":
            send_tg(
                f"🟢 賽克斯做多訊號\n"
                f"幣種：{name}  |  時框：{timeframe}\n"
                f"入場價：{entry}\n"
                f"━━━━━━━━━━━━\n"
                f"🛑 止損：{sl}（結構低點 -{rr}%）\n"
                f"🎯 止盈1（50%）：{tp1}  ← 1:1\n"
                f"🎯 止盈2（50%）：{tp2}  ← 1:2\n"
                f"📌 止盈1達到後，剩餘止損移至：{be_sl}\n"
                f"━━━━━━━━━━━━\n"
                f"Funding：{funding_str}\n"
                f"CVD+OI：{cvd_reason}"
            )
            print(f"[{name}][{timeframe}] 🟢 做多 入場:{entry} SL:{sl} TP1:{tp1} TP2:{tp2} BE:{be_sl}")
            last_signal[key] = "long"
        else:
            print(f"[{name}][{timeframe}] 做多（重複，略過）")

    else:
        if prev_sig != "short":
            send_tg(
                f"🔴 賽克斯做空訊號\n"
                f"幣種：{name}  |  時框：{timeframe}\n"
                f"入場價：{entry}\n"
                f"━━━━━━━━━━━━\n"
                f"🛑 止損：{sl}（結構高點 +{rr}%）\n"
                f"🎯 止盈1（50%）：{tp1}  ← 1:1\n"
                f"🎯 止盈2（50%）：{tp2}  ← 1:2\n"
                f"📌 止盈1達到後，剩餘止損移至：{be_sl}\n"
                f"━━━━━━━━━━━━\n"
                f"Funding：{funding_str}\n"
                f"CVD+OI：{cvd_reason}"
            )
            print(f"[{name}][{timeframe}] 🔴 做空 入場:{entry} SL:{sl} TP1:{tp1} TP2:{tp2} BE:{be_sl}")
            last_signal[key] = "short"
        else:
            print(f"[{name}][{timeframe}] 做空（重複，略過）")

# =====================
# 排程
# =====================
def next_close_ts(tf):
    p = TF_SECONDS[tf]
    return (int(datetime.now(timezone.utc).timestamp()) // p + 1) * p

def wait_for_next_candle():
    triggers = {tf: next_close_ts(tf) + 5 for tf in TIMEFRAMES}
    earliest = min(triggers.values())
    while True:
        wait = earliest - datetime.now(timezone.utc).timestamp() - 0.5
        if wait <= 0: break
        sleep(min(wait, 30))
    while datetime.now(timezone.utc).timestamp() < earliest:
        sleep(0.05)
    return [tf for tf, ts in triggers.items() if abs(ts - earliest) <= 15]

def check_timeframes(exchange, due_tfs):
    for tf in due_tfs:
        print(f"\n── 掃描 {tf} ──")
        for symbol in list(SYMBOLS.keys()):
            check_signal(exchange, symbol, tf)
            sleep(0.5)
    print("── 完畢 ──\n")

# =====================
# 啟動
# =====================
Thread(target=run_web, daemon=True).start()

exchange = ccxt.okx()
exchange.load_markets()

update_symbols(exchange, force=False)
Thread(target=weekly_symbol_updater, args=(exchange,), daemon=True).start()

send_tg(
    "✅ 賽克斯機器人 v4 已啟動\n"
    "時框：15m / 30m / 1h / 4h\n"
    "止損：結構支撐壓力 + ATR緩衝\n"
    "止盈：分批（50%@1:1，50%@1:2）\n"
    "止盈1後止損移至：成本+手續費\n"
    "過濾：Funding Rate + CVD+OI吸收背離\n"
    f"監控：{len(SYMBOLS)} 個幣（市值前100 x OKX x Coinalyze）"
)

while True:
    try:
        due = wait_for_next_candle()
        print(f"觸發時框：{', '.join(due)}")
        check_timeframes(exchange, due)
    except Exception as e:
        print(f"錯誤：{e}")
        send_tg(f"⚠️ 錯誤：{e}")
        sleep(60)



