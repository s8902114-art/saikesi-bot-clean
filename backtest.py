import ccxt
import pandas as pd
from time import sleep

# ══════════════════════════
# 回測設定
# ══════════════════════════
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
TIMEFRAMES  = ["15m", "30m", "1h", "4h"]
TARGET_BARS = 1500
LOOKBACK_SL = 5

# 三種止盈倍數（止損固定 1 份風險）
RR_RATIOS = [1.0, 1.5, 2.0]
RISK_PCT   = 1.0   # 每筆固定承擔本金 1%
# ══════════════════════════


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
        except Exception as e:
            print(f"  抓取失敗：{e}")
            break
        if not batch:
            break
        batch = [c for c in batch if c[0] < earliest_ts]
        if not batch:
            break
        all_ohlcv = batch + all_ohlcv
        sleep(0.2)
    return all_ohlcv[-target:]


def calc_rsi(series, period=14):
    delta = series.diff()
    gain  = delta.clip(lower=0).ewm(com=period - 1, adjust=False).mean()
    loss  = (-delta.clip(upper=0)).ewm(com=period - 1, adjust=False).mean()
    rs    = gain / loss
    return 100 - (100 / (1 + rs))


def prepare_df(ohlcv):
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
    return df


def collect_signals(df, symbol, timeframe):
    """收集所有訊號，每筆紀錄 entry / sl / direction，尚未指定 TP"""
    signals = []
    start = max(LOOKBACK_SL, 700)

    for i in range(start, len(df) - 1):
        last = df.iloc[i]
        prev = df.iloc[i - 1]

        bullTrend   = last["ema144"] > last["ema576"]
        bearTrend   = last["ema144"] < last["ema576"]
        qqeTurnBlue = last["rsiMa"] >= 50 and prev["rsiMa"] < 50
        qqeTurnRed  = last["rsiMa"] <  50 and prev["rsiMa"] >= 50

        longC1 = (last["close"] > last["largeTop"] and
                  last["close"] > last["smallBot"] and
                  last["low"]   < last["smallBot"])
        longC2 = last["close"] > last["ema12"]
        longC3 = qqeTurnBlue

        shortC1 = (last["close"] < last["largeBot"] and
                   last["close"] < last["smallTop"] and
                   last["high"]  > last["smallTop"])
        shortC2 = last["close"] < last["ema12"]
        shortC3 = qqeTurnRed

        if bullTrend and longC1 and longC2 and longC3:
            entry = last["close"]
            sl    = df.iloc[i - LOOKBACK_SL: i + 1]["low"].min()
            risk  = entry - sl
            if risk > 0:
                signals.append({"i": i, "dir": "多", "entry": entry, "sl": sl, "risk": risk})

        elif bearTrend and shortC1 and shortC2 and shortC3:
            entry = last["close"]
            sl    = df.iloc[i - LOOKBACK_SL: i + 1]["high"].max()
            risk  = sl - entry
            if risk > 0:
                signals.append({"i": i, "dir": "空", "entry": entry, "sl": sl, "risk": risk})

    return signals


def simulate(df, signals, rr):
    """給定 RR 倍數，模擬每筆訊號的勝敗，回傳 (wins, losses)"""
    wins = losses = 0
    for sig in signals:
        i     = sig["i"]
        entry = sig["entry"]
        sl    = sig["sl"]
        risk  = sig["risk"]
        tp    = (entry + risk * rr) if sig["dir"] == "多" else (entry - risk * rr)

        for j in range(i + 1, len(df)):
            fut = df.iloc[j]
            if sig["dir"] == "多":
                if fut["low"] <= sl:
                    losses += 1; break
                if fut["high"] >= tp:
                    wins += 1; break
            else:
                if fut["high"] >= sl:
                    losses += 1; break
                if fut["low"] <= tp:
                    wins += 1; break
    return wins, losses


def capital_change(wins, losses, rr):
    """模擬資金變化：每筆固定風險 RISK_PCT%"""
    equity = 100.0
    for _ in range(wins):
        equity += equity * (RISK_PCT / 100) * rr
    for _ in range(losses):
        equity -= equity * (RISK_PCT / 100)
    return equity


# ══════════════════════════
# 主程式
# ══════════════════════════
print(f"開始回測（{TARGET_BARS} 根K線，止損前{LOOKBACK_SL}根高低點，每筆風險 {RISK_PCT}%）\n")

exchange    = ccxt.okx()
# all_signals[rr] = list of (win/loss)
rr_wins   = {rr: 0 for rr in RR_RATIOS}
rr_losses = {rr: 0 for rr in RR_RATIOS}

rr_by_tf  = {rr: {tf: {"w": 0, "l": 0} for tf in TIMEFRAMES} for rr in RR_RATIOS}
rr_by_dir = {rr: {"多": {"w": 0, "l": 0}, "空": {"w": 0, "l": 0}} for rr in RR_RATIOS}

for symbol in SYMBOLS:
    name = symbol.split("/")[0]
    for tf in TIMEFRAMES:
        ohlcv = fetch_ohlcv_paginated(exchange, symbol, tf, TARGET_BARS)
        if len(ohlcv) < 750:
            print(f"[{name:6s}][{tf:3s}]  資料不足（{len(ohlcv)} 根）")
            continue
        df      = prepare_df(ohlcv)
        signals = collect_signals(df, name, tf)
        if not signals:
            print(f"[{name:6s}][{tf:3s}]  K線：{len(ohlcv):4d}  無訊號")
            continue

        parts = []
        for rr in RR_RATIOS:
            w, l = simulate(df, signals, rr)
            rr_wins[rr]   += w
            rr_losses[rr] += l
            rr_by_tf[rr][tf]["w"]        += w
            rr_by_tf[rr][tf]["l"]        += l
            for sig in signals:
                pass  # 方向統計需個別跑，整合在 simulate 外
            wr = f"{w/(w+l)*100:.1f}%" if (w + l) else "無"
            parts.append(f"1:{rr:.1f}→{wr}")

        # 方向統計（分 多/空）
        long_sigs  = [s for s in signals if s["dir"] == "多"]
        short_sigs = [s for s in signals if s["dir"] == "空"]
        for rr in RR_RATIOS:
            lw, ll = simulate(df, long_sigs,  rr)
            sw, sl = simulate(df, short_sigs, rr)
            rr_by_dir[rr]["多"]["w"] += lw
            rr_by_dir[rr]["多"]["l"] += ll
            rr_by_dir[rr]["空"]["w"] += sw
            rr_by_dir[rr]["空"]["l"] += sl

        total = len(signals)
        print(f"[{name:6s}][{tf:3s}]  K線：{len(ohlcv):4d}  訊號：{total:3d}  |  {' / '.join(parts)}")

# ══════════════════════════
# 整體統計
# ══════════════════════════
print("\n" + "═" * 60)
print(f"{'整體統計':^60}")
print("═" * 60)

header = f"{'':12s}" + "".join(f"{'1:'+str(rr):>18s}" for rr in RR_RATIOS)
print(header)

# 總覽
row = f"{'總覽':12s}"
for rr in RR_RATIOS:
    w = rr_wins[rr]; l = rr_losses[rr]; t = w + l
    wr  = f"{w/t*100:.1f}%" if t else "-"
    cap = capital_change(w, l, rr)
    row += f"  {wr:>6s} 資金{cap:>7.1f}%"
print(row)

# 依時框
print("\n── 依時框 ──")
for tf in TIMEFRAMES:
    row = f"  {tf:10s}"
    for rr in RR_RATIOS:
        w = rr_by_tf[rr][tf]["w"]; l = rr_by_tf[rr][tf]["l"]; t = w + l
        wr  = f"{w/t*100:.1f}%" if t else "-"
        cap = capital_change(w, l, rr)
        row += f"  {wr:>6s} 資金{cap:>7.1f}%"
    print(row)

# 依方向
print("\n── 依方向 ──")
for d in ["多", "空"]:
    row = f"  做{d:9s}"
    for rr in RR_RATIOS:
        w = rr_by_dir[rr][d]["w"]; l = rr_by_dir[rr][d]["l"]; t = w + l
        wr  = f"{w/t*100:.1f}%" if t else "-"
        cap = capital_change(w, l, rr)
        row += f"  {wr:>6s} 資金{cap:>7.1f}%"
    print(row)

print("\n說明：資金從 100% 起，每筆固定風險 1%，勝→+1%×RR 倍，敗→-1%")
