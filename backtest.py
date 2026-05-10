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
TARGET_BARS = 1500   # 目標 K 線數量（分批抓）
LOOKBACK_SL = 5      # 止損取訊號前 N 根的最高/最低點
# ══════════════════════════

def fetch_ohlcv_paginated(exchange, symbol, timeframe, target=1500):
    """分批往前抓歷史 K 線（OKX 單次上限 300）"""
    # 先抓最新一批
    all_ohlcv = exchange.fetch_ohlcv(symbol, timeframe, limit=300)
    if not all_ohlcv:
        return []

    tf_ms = exchange.parse_timeframe(timeframe) * 1000  # 每根時間長度（毫秒）

    while len(all_ohlcv) < target:
        earliest_ts = all_ohlcv[0][0]
        new_since   = earliest_ts - 300 * tf_ms         # 往前推 300 根
        try:
            batch = exchange.fetch_ohlcv(symbol, timeframe, since=new_since, limit=300)
        except Exception as e:
            print(f"  抓取失敗：{e}")
            break
        if not batch:
            break
        # 只保留比現有資料更早的 K 線
        batch = [c for c in batch if c[0] < earliest_ts]
        if not batch:
            break
        all_ohlcv = batch + all_ohlcv
        sleep(0.2)

    return all_ohlcv[-target:]

def calc_rsi(series, period=14):
    delta = series.diff()
    gain  = delta.clip(lower=0).ewm(com=period-1, adjust=False).mean()
    loss  = (-delta.clip(upper=0)).ewm(com=period-1, adjust=False).mean()
    rs    = gain / loss
    return 100 - (100 / (1 + rs))

def prepare_df(ohlcv):
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
    return df

def backtest(df, symbol, timeframe):
    results = []
    start = max(LOOKBACK_SL, 700)   # ema676 暖機後才開始

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

        # ── 做多 ──
        if bullTrend and longC1 and longC2 and longC3:
            entry = last["close"]
            sl    = df.iloc[i - LOOKBACK_SL : i + 1]["low"].min()
            risk  = entry - sl
            if risk <= 0:
                continue
            tp = entry + risk
            for j in range(i + 1, len(df)):
                fut = df.iloc[j]
                if fut["low"] <= sl:
                    results.append({"symbol": symbol, "tf": timeframe, "dir": "多", "result": "敗"})
                    break
                if fut["high"] >= tp:
                    results.append({"symbol": symbol, "tf": timeframe, "dir": "多", "result": "勝"})
                    break

        # ── 做空 ──
        elif bearTrend and shortC1 and shortC2 and shortC3:
            entry = last["close"]
            sl    = df.iloc[i - LOOKBACK_SL : i + 1]["high"].max()
            risk  = sl - entry
            if risk <= 0:
                continue
            tp = entry - risk
            for j in range(i + 1, len(df)):
                fut = df.iloc[j]
                if fut["high"] >= sl:
                    results.append({"symbol": symbol, "tf": timeframe, "dir": "空", "result": "敗"})
                    break
                if fut["low"] <= tp:
                    results.append({"symbol": symbol, "tf": timeframe, "dir": "空", "result": "勝"})
                    break

    return results

# ══════════════════════════
# 主程式
# ══════════════════════════
print(f"開始回測（分批抓 {TARGET_BARS} 根 K 線，止損取前 {LOOKBACK_SL} 根高低點）...\n")
exchange    = ccxt.okx()
all_results = []

for symbol in SYMBOLS:
    name = symbol.split("/")[0]
    for tf in TIMEFRAMES:
        ohlcv = fetch_ohlcv_paginated(exchange, symbol, tf, TARGET_BARS)
        if len(ohlcv) < 750:
            print(f"[{name:6s}][{tf:3s}]  資料不足（只有 {len(ohlcv)} 根）")
            continue
        df  = prepare_df(ohlcv)
        res = backtest(df, name, tf)
        all_results.extend(res)
        wins  = sum(1 for r in res if r["result"] == "勝")
        total = len(res)
        wr    = f"{wins/total*100:.1f}%" if total else "無訊號"
        print(f"[{name:6s}][{tf:3s}]  K線：{len(ohlcv):4d}  訊號：{total:3d}  勝率：{wr}")

# ── 整體統計 ──
print("\n" + "═"*50)
print("整體統計")
print("═"*50)
total_all = len(all_results)
wins_all  = sum(1 for r in all_results if r["result"] == "勝")
if total_all:
    print(f"總訊號：{total_all}  勝：{wins_all}  敗：{total_all-wins_all}  整體勝率：{wins_all/total_all*100:.1f}%")

    print("\n依時框：")
    for tf in TIMEFRAMES:
        sub = [r for r in all_results if r["tf"] == tf]
        w = sum(1 for r in sub if r["result"] == "勝")
        t = len(sub)
        print(f"  {tf:3s}  訊號：{t:3d}  勝率：{w/t*100:.1f}%" if t else f"  {tf:3s}  無訊號")

    print("\n依方向：")
    for d in ["多", "空"]:
        sub = [r for r in all_results if r["dir"] == d]
        w = sum(1 for r in sub if r["result"] == "勝")
        t = len(sub)
        print(f"  做{d}  訊號：{t:3d}  勝率：{w/t*100:.1f}%" if t else f"  做{d}  無訊號")

    print("\n依幣種：")
    for nm in list(dict.fromkeys(r["symbol"] for r in all_results)):
        sub = [r for r in all_results if r["symbol"] == nm]
        w = sum(1 for r in sub if r["result"] == "勝")
        t = len(sub)
        print(f"  {nm:6s}  訊號：{t:3d}  勝率：{w/t*100:.1f}%" if t else f"  {nm:6s}  無訊號")
else:
    print("無任何訊號，資料可能不足")
