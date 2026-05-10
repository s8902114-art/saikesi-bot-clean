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
TIMEFRAMES = ["15m", "30m", "1h", "4h"]
LIMIT      = 500   # 每個幣取幾根 K 線
# ══════════════════════════

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
    for i in range(1, len(df) - 1):
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

        # 做多訊號
        if bullTrend and longC1 and longC2 and longC3:
            entry = last["close"]
            sl    = last["smallBot"]          # 止損：小帶下緣
            risk  = entry - sl
            if risk <= 0:
                continue
            tp = entry + risk                 # 1:1 止盈

            # 往後找結果
            for j in range(i + 1, len(df)):
                future = df.iloc[j]
                if future["low"] <= sl:
                    results.append({"symbol": symbol, "tf": timeframe,
                                    "dir": "多", "result": "敗"})
                    break
                if future["high"] >= tp:
                    results.append({"symbol": symbol, "tf": timeframe,
                                    "dir": "多", "result": "勝"})
                    break

        # 做空訊號
        elif bearTrend and shortC1 and shortC2 and shortC3:
            entry = last["close"]
            sl    = last["smallTop"]          # 止損：小帶上緣
            risk  = sl - entry
            if risk <= 0:
                continue
            tp = entry - risk                 # 1:1 止盈

            for j in range(i + 1, len(df)):
                future = df.iloc[j]
                if future["high"] >= sl:
                    results.append({"symbol": symbol, "tf": timeframe,
                                    "dir": "空", "result": "敗"})
                    break
                if future["low"] <= tp:
                    results.append({"symbol": symbol, "tf": timeframe,
                                    "dir": "空", "result": "勝"})
                    break

    return results

# ══════════════════════════
# 主程式
# ══════════════════════════
print("開始回測，請稍候...\n")
exchange    = ccxt.okx()
all_results = []

for symbol in SYMBOLS:
    name = symbol.split("/")[0]
    for tf in TIMEFRAMES:
        try:
            ohlcv = exchange.fetch_ohlcv(symbol, tf, limit=LIMIT)
            df    = prepare_df(ohlcv)
            res   = backtest(df, name, tf)
            all_results.extend(res)
            wins  = sum(1 for r in res if r["result"] == "勝")
            total = len(res)
            wr    = f"{wins/total*100:.1f}%" if total else "無訊號"
            print(f"[{name:6s}][{tf:3s}]  訊號次數：{total:3d}  勝率：{wr}")
        except Exception as e:
            print(f"[{name}][{tf}] 錯誤：{e}")
        sleep(0.3)

# 整體統計
print("\n" + "═"*40)
print("整體統計")
print("═"*40)
total_all = len(all_results)
wins_all  = sum(1 for r in all_results if r["result"] == "勝")
if total_all:
    print(f"總訊號次數：{total_all}")
    print(f"總勝場次數：{wins_all}")
    print(f"總敗場次數：{total_all - wins_all}")
    print(f"整體勝率：  {wins_all/total_all*100:.1f}%")

    # 依時框分組
    print("\n依時框統計：")
    for tf in TIMEFRAMES:
        sub   = [r for r in all_results if r["tf"] == tf]
        w     = sum(1 for r in sub if r["result"] == "勝")
        t     = len(sub)
        wr    = f"{w/t*100:.1f}%" if t else "無訊號"
        print(f"  {tf:3s}  訊號：{t:3d}  勝率：{wr}")

    # 依方向分組
    print("\n依方向統計：")
    for d in ["多", "空"]:
        sub = [r for r in all_results if r["dir"] == d]
        w   = sum(1 for r in sub if r["result"] == "勝")
        t   = len(sub)
        wr  = f"{w/t*100:.1f}%" if t else "無訊號"
        print(f"  做{d}  訊號：{t:3d}  勝率：{wr}")
else:
    print("無任何訊號產生")
