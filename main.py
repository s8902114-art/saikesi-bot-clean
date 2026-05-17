# 賽克斯訊號機器人 v4 — 推送時間：2026-05-17
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

ORDER_LEVERAGE = 5
ORDER_PCT      = 0.10
pending_orders = {}

TIMEFRAMES = ["15m", "30m", "1h", "4h"]
TF_SECONDS = {"15m": 900, "30m": 1800, "1h": 3600, "4h": 14400}
TF_MAP     = {"15m": "15min", "30m": "30min", "1h": "1hour", "4h": "4hour"}

FUNDING_LONG_MAX  =  0.0003
FUNDING_SHORT_MIN = -0.0003
CVD_LOOKBACK = 3
OI_LOOKBACK  = 3
TAKER_FEE    = 0.0005

# ╔══════════════════════════════════════════════════════════╗
# ║   回測優化參數（combined_optimizer.py 自動產生）         ║
# ╠══════════════════════════════════════════════════════════╣
# ║  多頭：Apr-May 2026 牛市 IS，WR=80% EV=+1.03           ║
# ║  空頭：Nov 2025-Mar 2026 熊市 IS，WR=45% EV=+0.30      ║
# ╚══════════════════════════════════════════════════════════╝

# ── 做多參數 ──────────────────────────────────────────────
LONG_QQE_RSI = 7        # RSI 週期
LONG_QQE_SF  = 5        # Smoothing Factor
LONG_TP1     = 1.725    # 止盈1 倍率
LONG_TP2_I   = 1.8      # 止盈2 日內 (15m/30m)
LONG_TP2_S   = 2.5      # 止盈2 波段 (1h/4h)
LONG_SL_BUF  = 0.08     # 結構低點外擴 ATR 倍數
LONG_SL_LB   = 17       # 結構回看根數

# ── 做空參數 ──────────────────────────────────────────────
SHORT_QQE_RSI = 5       # RSI 週期
SHORT_QQE_SF  = 6       # Smoothing Factor
SHORT_TP1     = 2.0     # 止盈1 倍率
SHORT_TP2_I   = 3.2     # 止盈2 日內 (15m/30m)
SHORT_TP2_S   = 4.0     # 止盈2 波段 (1h/4h，加大獲利空間)
SHORT_SL_BUF  = 0.03    # 結構高點外擴 ATR 倍數
SHORT_SL_LB   = 20      # 結構回看根數

SYMBOLS_FILE   = "symbols_cache.json"
COINALYZE_BASE = "https://api.coinalyze.net/v1"
last_signal  = {}
SYMBOLS      = {}
SPOT_SYMBOLS = {}

app = Flask(__name__)

@app.route("/")
def home():
    return f"Saikesi Bot v6 | {len(SYMBOLS)} symbols"

def run_web():
    app.run(host="0.0.0.0", port=int(os.environ.get("PORT", 3000)))

# =====================
# Telegram
# =====================
def send_tg(msg):
    try:
        requests.post(f"https://api.telegram.org/bot{TG_TOKEN}/sendMessage",
                      data={"chat_id": TG_CHAT_ID, "text": msg}, timeout=10)
    except Exception:
        pass

def send_tg_with_buttons(msg, cb):
    try:
        kb = {"inline_keyboard": [[
            {"text": "✅ 確認下單", "callback_data": f"confirm_{cb}"},
            {"text": "❌ 跳過",     "callback_data": f"skip_{cb}"}
        ]]}
        requests.post(f"https://api.telegram.org/bot{TG_TOKEN}/sendMessage",
                      json={"chat_id": TG_CHAT_ID, "text": msg, "reply_markup": kb}, timeout=10)
    except Exception as e:
        print(f"[TG] {e}")

def answer_callback(cq_id, text=""):
    try:
        requests.post(f"https://api.telegram.org/bot{TG_TOKEN}/answerCallbackQuery",
                      json={"callback_query_id": cq_id, "text": text}, timeout=10)
    except Exception:
        pass

# =====================
# OKX 下單
# =====================
def place_okx_order(symbol, direction, sl, tp1, tp2):
    try:
        ex = ccxt.okx({"apiKey": OKX_API_KEY, "secret": OKX_SECRET_KEY,
                        "password": OKX_PASSPHRASE, "options": {"defaultType": "swap"}})
        ex.load_markets()
        bal  = ex.fetch_balance()
        usdt = bal["USDT"]["free"] if "USDT" in bal else 0
        if usdt <= 0:
            send_tg("⚠️ USDT 餘額不足"); return
        ex.set_leverage(ORDER_LEVERAGE, symbol)
        price = ex.fetch_ticker(symbol)["last"]
        mkt   = ex.market(symbol)
        prec  = mkt.get("precision", {}).get("amount", 3)
        amt   = round(usdt * ORDER_PCT * ORDER_LEVERAGE / price / mkt.get("contractSize", 1), prec)
        half  = round(amt / 2, prec)
        if amt <= 0:
            send_tg("⚠️ 張數為 0"); return
        is_l = direction == "long"
        es, xs = ("buy", "sell") if is_l else ("sell", "buy")
        eo = ex.create_market_order(symbol, es, amt)
        res = [f"✅ 進場\n{symbol} {'做多' if is_l else '做空'} {ORDER_LEVERAGE}x\n"
               f"均價:{eo.get('average') or price}  ID:{eo.get('id')}"]
        try:
            o = ex.create_order(symbol, "stop_market", xs, amt, None,
                                {"stopPrice": sl, "reduceOnly": True})
            res.append(f"🛑 SL {sl} ID:{o.get('id')}")
        except Exception as e:
            res.append(f"⚠️ SL失敗:{e}")
        for price_, n_, lbl in [(tp1, half, "TP1"), (tp2, half, "TP2")]:
            try:
                o = ex.create_limit_order(symbol, xs, n_, price_, {"reduceOnly": True})
                res.append(f"🎯 {lbl} {price_} x{n_} ID:{o.get('id')}")
            except Exception as e:
                res.append(f"⚠️ {lbl}失敗:{e}")
        send_tg("\n".join(res))
    except Exception as e:
        send_tg(f"⚠️ 下單失敗:{e}")

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
                        "allowed_updates": ["callback_query"]}, timeout=40).json()
            for upd in r.get("result", []):
                _tg_offset = upd["update_id"] + 1
                cq = upd.get("callback_query")
                if not cq:
                    continue
                cbd, cq_id = cq.get("data", ""), cq["id"]
                if cbd.startswith("confirm_"):
                    key = cbd[8:]
                    order = pending_orders.pop(key, None)
                    if order:
                        answer_callback(cq_id, "下單中...")
                        Thread(target=place_okx_order,
                               args=(order["symbol"], order["direction"],
                                     order["sl"], order["tp1"], order["tp2"]),
                               daemon=True).start()
                    else:
                        answer_callback(cq_id, "訊號已過期")
                elif cbd.startswith("skip_"):
                    pending_orders.pop(cbd[5:], None)
                    answer_callback(cq_id, "已跳過")
                    send_tg("❌ 已跳過")
        except Exception as e:
            print(f"[TG輪詢] {e}")
            sleep(5)

# =====================
# 幣單管理
# =====================
def fetch_top100_coingecko():
    try:
        r = requests.get("https://api.coingecko.com/api/v3/coins/markets",
                         params={"vs_currency": "usd", "order": "market_cap_desc",
                                 "per_page": 100, "page": 1, "sparkline": False}, timeout=15)
        return [c["symbol"].upper() for c in r.json()] if r.status_code == 200 else []
    except Exception:
        return []

def build_symbol_map(exchange, top100):
    okx_swaps = {info["base"]: sym for sym, info in exchange.markets.items()
                 if info.get("swap") and info.get("quote") == "USDT"
                 and info.get("settle") == "USDT" and info.get("active")}
    cl_fut, cl_spot = {}, {}
    for ep, dst in [("future-markets", cl_fut), ("spot-markets", cl_spot)]:
        r = requests.get(f"{COINALYZE_BASE}/{ep}",
                         headers={"api_key": COINALYZE_KEY}, timeout=15)
        if r.status_code == 200:
            for item in r.json():
                s = item.get("symbol", "")
                if s.endswith(".A"):
                    dst[item.get("base_asset", "").upper()] = s
    fut, spot = {}, {}
    for base in top100:
        if base in okx_swaps and base in cl_fut:
            fut[okx_swaps[base]] = cl_fut[base]
            if base in cl_spot:
                spot[okx_swaps[base]] = cl_spot[base]
    return fut, spot

def load_cache():
    if not os.path.exists(SYMBOLS_FILE):
        return None, None, None
    try:
        d = json.load(open(SYMBOLS_FILE))
        return d["symbols"], d.get("spot_symbols", {}), datetime.fromisoformat(d["updated_at"])
    except Exception:
        return None, None, None

def save_cache(s, sp):
    json.dump({"updated_at": datetime.now(timezone.utc).isoformat(),
               "symbols": s, "spot_symbols": sp}, open(SYMBOLS_FILE, "w"), indent=2)

FALLBACK_SYMBOLS = {
    "BTC/USDT:USDT": "BTCUSDT_PERP.A", "ETH/USDT:USDT": "ETHUSDT_PERP.A",
    "SOL/USDT:USDT": "SOLUSDT_PERP.A", "XRP/USDT:USDT": "XRPUSDT_PERP.A",
    "BNB/USDT:USDT": "BNBUSDT_PERP.A", "ADA/USDT:USDT": "ADAUSDT_PERP.A",
    "DOGE/USDT:USDT": "DOGEUSDT_PERP.A", "SUI/USDT:USDT": "SUIUSDT_PERP.A",
    "LINK/USDT:USDT": "LINKUSDT_PERP.A",
}

def update_symbols(exchange, force=False):
    global SYMBOLS, SPOT_SYMBOLS
    cached, cached_spot, updated_at = load_cache()
    need = force or cached is None
    if not need and updated_at:
        if (datetime.now(timezone.utc) - updated_at.replace(tzinfo=timezone.utc)).days > 7:
            need = True
    if not need and cached:
        SYMBOLS, SPOT_SYMBOLS = cached, cached_spot or {}
        print(f"[幣單] 快取 {len(SYMBOLS)} 個"); return
    top100 = fetch_top100_coingecko()
    if not top100:
        SYMBOLS = cached or FALLBACK_SYMBOLS; SPOT_SYMBOLS = cached_spot or {}; return
    nf, ns = build_symbol_map(exchange, top100)
    if nf:
        SYMBOLS, SPOT_SYMBOLS = nf, ns
        save_cache(SYMBOLS, SPOT_SYMBOLS)
        print(f"[幣單] 更新 {len(SYMBOLS)} 個，現貨CVD {len(SPOT_SYMBOLS)} 個")
        send_tg(f"📋 幣單更新\n合約:{len(SYMBOLS)} 個  現貨CVD:{len(SPOT_SYMBOLS)} 個\n"
                + "、".join(s.split("/")[0] for s in list(SYMBOLS)[:20]) + "...")
    else:
        SYMBOLS = cached or FALLBACK_SYMBOLS; SPOT_SYMBOLS = cached_spot or {}

def weekly_symbol_updater(exchange):
    while True:
        sleep(604800)
        update_symbols(exchange, force=True)

# =====================
# 技術指標
# =====================
def calc_rsi(series, period):
    d = series.diff()
    g = d.clip(lower=0).ewm(com=period - 1, adjust=False).mean()
    l = (-d.clip(upper=0)).ewm(com=period - 1, adjust=False).mean()
    return 100 - (100 / (1 + g / l))

def find_structure_sl(df, direction, lookback):
    w = df.iloc[-lookback - 2:-2].copy().reset_index(drop=True)
    pts = []
    for i in range(2, len(w) - 2):
        if direction == "long":
            v = w.iloc[i]["low"]
            if all(v < w.iloc[i - k]["low"] for k in (1, 2)) and \
               all(v < w.iloc[i + k]["low"] for k in (1, 2)):
                pts.append(v)
        else:
            v = w.iloc[i]["high"]
            if all(v > w.iloc[i - k]["high"] for k in (1, 2)) and \
               all(v > w.iloc[i + k]["high"] for k in (1, 2)):
                pts.append(v)
    if pts:
        return pts[-1]
    return (df.iloc[-7:-2]["low"].min() if direction == "long"
            else df.iloc[-7:-2]["high"].max())

def calc_atr(df, period=14):
    hi, lo, cl = df["high"], df["low"], df["close"]
    tr = pd.concat([hi - lo, (hi - cl.shift()).abs(), (lo - cl.shift()).abs()], axis=1).max(axis=1)
    return tr.ewm(span=period, adjust=False).mean().iloc[-1]

# =====================
# Coinalyze 三層 CVD
# =====================
def coinalyze_get(ep, params):
    try:
        r = requests.get(f"{COINALYZE_BASE}/{ep}",
                         headers={"api_key": COINALYZE_KEY}, params=params, timeout=10)
        return r.json() if r.status_code == 200 else None
    except Exception:
        return None

def _cl_params(sym, tf, lb=8):
    s = TF_SECONDS[tf]
    now = int(datetime.now(timezone.utc).timestamp())
    return {"symbols": sym, "interval": TF_MAP.get(tf, "1hour"),
            "from": now - s * (lb + 2), "to": now, "convert_to_usd": "true"}

def _cvd(d):
    cvd, cum = [], 0
    if d and d[0].get("history"):
        for b in d[0]["history"]:
            bv = b.get("bv", 0)
            cum += bv - (b.get("v", 0) - bv)
            cvd.append(cum)
    return cvd

def get_futures_cvd_oi(cl_f, tf):
    p = _cl_params(cl_f, tf)
    oi = coinalyze_get("open-interest-history", p)
    oi_c = ([b["c"] for b in oi[0]["history"]] if oi and oi[0].get("history") else [])
    return oi_c, _cvd(coinalyze_get("ohlcv-history", p))

def get_spot_cvd(cl_s, tf):
    return _cvd(coinalyze_get("ohlcv-history", _cl_params(cl_s, tf))) if cl_s else []

def check_cvd_oi(cl_f, cl_s, tf, direction):
    """秋總三層確認：現貨CVD + 合約CVD + OI"""
    oi_c, fut_cvd = get_futures_cvd_oi(cl_f, tf)
    spot_cvd = get_spot_cvd(cl_s, tf) if cl_s else []
    if len(fut_cvd) < CVD_LOOKBACK:
        return True, "CVD數據不足"
    rej = []
    if len(oi_c) >= OI_LOOKBACK and oi_c[-1] <= oi_c[-OI_LOOKBACK]:
        rej.append("OI未上升")
    fr = fut_cvd[-1] > fut_cvd[-CVD_LOOKBACK]
    if direction == "long"  and not fr: rej.append("合約CVD未上升")
    elif direction == "short" and fr:   rej.append("合約CVD未下降")
    hs = len(spot_cvd) >= CVD_LOOKBACK
    if hs:
        sr = spot_cvd[-1] > spot_cvd[-CVD_LOOKBACK]
        if direction == "long"  and not sr: rej.append("現貨CVD未上升")
        elif direction == "short" and sr:   rej.append("現貨CVD未下降")
    if rej:
        return False, "、".join(rej)
    if direction == "long":
        return True, ("現貨CVD↑+合約CVD↑+OI↑" if hs else "合約CVD↑+OI↑")
    return True, ("現貨CVD↓+合約CVD↓+OI↑" if hs else "合約CVD↓+OI↑")

def get_funding_rate(exchange, symbol):
    try:
        return float(exchange.fetch_funding_rate(symbol)["fundingRate"])
    except Exception:
        return None

def fetch_ohlcv_paginated(exchange, symbol, timeframe, target=1500):
    all_o = exchange.fetch_ohlcv(symbol, timeframe, limit=300)
    if not all_o:
        return []
    tf_ms = exchange.parse_timeframe(timeframe) * 1000
    while len(all_o) < target:
        e0 = all_o[0][0]
        try:
            batch = exchange.fetch_ohlcv(symbol, timeframe,
                                         since=e0 - 300 * tf_ms, limit=300)
        except Exception:
            break
        batch = [c for c in (batch or []) if c[0] < e0]
        if not batch:
            break
        all_o = batch + all_o
        sleep(0.2)
    return all_o[-target:]

# =====================
# 主訊號邏輯
# =====================
def check_signal(exchange, symbol, timeframe):
    cl_f = SYMBOLS.get(symbol)
    cl_s = SPOT_SYMBOLS.get(symbol)
    if not cl_f:
        return

    try:
        ohlcv = fetch_ohlcv_paginated(exchange, symbol, timeframe, target=1500)
    except Exception as e:
        print(f"[{symbol}][{timeframe}] 資料失敗: {e}"); return
    if len(ohlcv) < 700:
        return

    df = pd.DataFrame(ohlcv, columns=["time", "open", "high", "low", "close", "vol"])
    for s in (12, 144, 169, 576, 676):
        df[f"ema{s}"] = df["close"].ewm(span=s, adjust=False).mean()
    df["smallTop"] = df[["ema144", "ema169"]].max(axis=1)
    df["smallBot"] = df[["ema144", "ema169"]].min(axis=1)
    df["largeTop"] = df[["ema576", "ema676"]].max(axis=1)
    df["largeBot"] = df[["ema576", "ema676"]].min(axis=1)

    # 多頭專用 QQE（RSI=7, SF=5）
    df["rsiMa_L"] = calc_rsi(df["close"], LONG_QQE_RSI).ewm(span=LONG_QQE_SF, adjust=False).mean()
    # 空頭專用 QQE（RSI=5, SF=6）
    df["rsiMa_S"] = calc_rsi(df["close"], SHORT_QQE_RSI).ewm(span=SHORT_QQE_SF, adjust=False).mean()

    tf_ms  = TF_SECONDS[timeframe] * 1000
    now_ms = datetime.now(timezone.utc).timestamp() * 1000
    ci, pi = (-2, -3) if df.iloc[-1]["time"] + tf_ms > now_ms else (-1, -2)
    last, prev = df.iloc[ci], df.iloc[pi]

    bullTrend = last["ema144"] > last["ema576"]
    bearTrend = last["ema144"] < last["ema576"]
    qqeBlue   = last["rsiMa_L"] >= 50 and prev["rsiMa_L"] < 50
    qqeRed    = last["rsiMa_S"] <  50 and prev["rsiMa_S"] >= 50

    longC1  = (last["close"] > last["largeTop"] and
               last["close"] > last["smallBot"] and
               last["low"]   < last["smallBot"])
    longC2  = last["close"] > last["ema12"]
    shortC1 = (last["close"] < last["largeBot"] and
               last["close"] < last["smallTop"] and
               last["high"]  > last["smallTop"])
    shortC2 = last["close"] < last["ema12"]

    is_long  = bullTrend and longC1  and longC2  and qqeBlue
    is_short = bearTrend and shortC1 and shortC2 and qqeRed

    name = symbol.split("/")[0]
    key  = (symbol, timeframe)
    prev_sig = last_signal.get(key)

    if not is_long and not is_short:
        if prev_sig is not None:
            last_signal[key] = None
        print(f"[{name}][{timeframe}] 無訊號"); return

    direction = "long" if is_long else "short"

    # ── 過濾：Funding Rate ──
    funding = get_funding_rate(exchange, symbol)
    rej = []
    if funding is not None:
        if is_long  and funding > FUNDING_LONG_MAX:  rej.append(f"Funding過高({funding:.4%})")
        if is_short and funding < FUNDING_SHORT_MIN: rej.append(f"Funding過低({funding:.4%})")

    # ── 過濾：三層 CVD ──
    ok, cvd_reason = check_cvd_oi(cl_f, cl_s, timeframe, direction)
    if not ok:
        rej.append(cvd_reason)

    if rej:
        rs = "、".join(rej)
        print(f"[{name}][{timeframe}] ⛔ {rs}")
        send_tg(f"⛔ 過濾\n{name} {timeframe} {'多' if is_long else '空'}\n{rs}"); return

    # ── 多週期確認（15m/30m 需符合 1h 趨勢）──
    if timeframe in ("15m", "30m"):
        try:
            o1h = fetch_ohlcv_paginated(exchange, symbol, "1h", target=700)
            if len(o1h) >= 200:
                d1h = pd.DataFrame(o1h, columns=["time", "open", "high", "low", "close", "vol"])
                e144 = d1h["close"].ewm(span=144, adjust=False).mean().iloc[-1]
                e576 = d1h["close"].ewm(span=576, adjust=False).mean().iloc[-1]
                if not ((e144 > e576) if is_long else (e144 < e576)):
                    send_tg(f"⛔ {name} {timeframe} 1h趨勢不一致"); return
        except Exception:
            pass

    # ── 止損止盈（多空各自用優化參數）──
    is_intraday = timeframe in ("15m", "30m")
    if is_long:
        tp1_m = LONG_TP1
        tp2_m = LONG_TP2_I if is_intraday else LONG_TP2_S
        sl_buf, sl_lb = LONG_SL_BUF, LONG_SL_LB
    else:
        tp1_m = SHORT_TP1
        tp2_m = SHORT_TP2_I if is_intraday else SHORT_TP2_S
        sl_buf, sl_lb = SHORT_SL_BUF, SHORT_SL_LB

    trade_type = "日內" if is_intraday else "波段"
    atr   = calc_atr(df.iloc[:ci])
    entry = round(last["close"], 6)

    if is_long:
        sl_raw = find_structure_sl(df.iloc[:ci + 1], "long", sl_lb)
        sl     = round(sl_raw - atr * sl_buf, 6)
        risk   = entry - sl
        tp1    = round(entry + risk * tp1_m, 6)
        tp2    = round(entry + risk * tp2_m, 6)
        be_sl  = round(entry * (1 + TAKER_FEE * 2), 6)
        sl_lbl = "結構低點"
    else:
        sl_raw = find_structure_sl(df.iloc[:ci + 1], "short", sl_lb)
        sl     = round(sl_raw + atr * sl_buf, 6)
        risk   = sl - entry
        tp1    = round(entry - risk * tp1_m, 6)
        tp2    = round(entry - risk * tp2_m, 6)
        be_sl  = round(entry * (1 - TAKER_FEE * 2), 6)
        sl_lbl = "結構高點"

    if risk <= 0:
        return

    rr  = round(risk / entry * 100, 2)
    fs  = f"{funding:.4%}" if funding is not None else "N/A"
    sig_dir = "long" if is_long else "short"

    if prev_sig == sig_dir:
        print(f"[{name}][{timeframe}] {'多' if is_long else '空'}（重複，略過）"); return

    cb_key = f"{name}_{timeframe}_{int(datetime.now(timezone.utc).timestamp())}"
    pending_orders[cb_key] = {"symbol": symbol, "direction": sig_dir,
                               "sl": sl, "tp1": tp1, "tp2": tp2}

    emoji = "🟢" if is_long else "🔴"
    dir_s = "做多" if is_long else "做空"
    send_tg_with_buttons(
        f"{emoji} 賽克斯{dir_s}訊號【{trade_type}】\n"
        f"幣種：{name}  |  時框：{timeframe}\n"
        f"入場價：{entry}\n"
        f"━━━━━━━━━━━━\n"
        f"🛑 止損：{sl}（{sl_lbl} {'+' if not is_long else '-'}{rr}%）\n"
        f"🎯 止盈1（50%）：{tp1}  ← 1:{tp1_m}\n"
        f"🎯 止盈2（50%）：{tp2}  ← 1:{tp2_m}\n"
        f"📌 止盈1後止損移至：{be_sl}\n"
        f"━━━━━━━━━━━━\n"
        f"Funding：{fs}\nCVD+OI：{cvd_reason}",
        cb_key)
    print(f"[{name}][{timeframe}]【{trade_type}】{emoji} {dir_s} "
          f"入場:{entry} SL:{sl} TP1:{tp1}(1:{tp1_m}) TP2:{tp2}(1:{tp2_m})")
    last_signal[key] = sig_dir

# =====================
# 排程
# =====================
def next_close_ts(tf):
    p = TF_SECONDS[tf]
    return (int(datetime.now(timezone.utc).timestamp()) // p + 1) * p

def wait_for_next_candle():
    trig = {tf: next_close_ts(tf) + 5 for tf in TIMEFRAMES}
    earliest = min(trig.values())
    while True:
        w = earliest - datetime.now(timezone.utc).timestamp() - 0.5
        if w <= 0:
            break
        sleep(min(w, 30))
    while datetime.now(timezone.utc).timestamp() < earliest:
        sleep(0.05)
    return [tf for tf, ts in trig.items() if abs(ts - earliest) <= 15]

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
Thread(target=poll_tg_callbacks, daemon=True).start()

exchange = ccxt.okx()
exchange.load_markets()
update_symbols(exchange, force=False)
Thread(target=weekly_symbol_updater, args=(exchange,), daemon=True).start()

send_tg(
    "✅ 賽克斯機器人 v6（多空獨立優化）已啟動\n"
    "━━━━━━━━━━━━━━━━━\n"
    f"🟢 多頭 QQE RSI={LONG_QQE_RSI} SF={LONG_QQE_SF}  "
    f"TP1=1:{LONG_TP1}  SLbuf={LONG_SL_BUF}  LB={LONG_SL_LB}\n"
    f"🔴 空頭 QQE RSI={SHORT_QQE_RSI} SF={SHORT_QQE_SF}  "
    f"TP1=1:{SHORT_TP1}  SLbuf={SHORT_SL_BUF}  LB={SHORT_SL_LB}\n"
    "━━━━━━━━━━━━━━━━━\n"
    "時框：15m/30m（日內）  1h/4h（波段）\n"
    "過濾：Funding + 現貨CVD + 合約CVD + OI（三層）\n"
    f"監控：{len(SYMBOLS)} 個幣（市值前100 × OKX × Coinalyze）\n"
    "🔔 按按鈕確認下單（5x槓桿，10%餘額）"
)

while True:
    try:
        due = wait_for_next_candle()
        print(f"觸發：{', '.join(due)}")
        check_timeframes(exchange, due)
    except Exception as e:
        print(f"錯誤：{e}")
        send_tg(f"⚠️ {e}")
        sleep(60)
