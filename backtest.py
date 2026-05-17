import ccxt
import pandas as pd
from time import sleep

# ══════════════════════════════════════════════
# 回測設定（完全對齊 main.py 策略邏輯）
# ══════════════════════════════════════════════
SYMBOLS = [
    "BTC/USDT:USDT",  "ETH/USDT:USDT",  "XRP/USDT:USDT",
    "SOL/USDT:USDT",  "BNB/USDT:USDT",  "ADA/USDT:USDT",
    "DOGE/USDT:USDT", "LINK/USDT:USDT", "AVAX/USDT:USDT",
    "SUI/USDT:USDT",  "DOT/USDT:USDT",  "LTC/USDT:USDT",
    "HYPE/USDT:USDT", "UNI/USDT:USDT",  "ATOM/USDT:USDT",
]
TIMEFRAMES  = ["15m", "30m", "1h", "4h"]
TARGET_BARS = 1500
LOOKBACK_SL = 30    # 與 main.py 一致
RISK_PCT    = 1.0   # 每筆固定承擔本金 1%
TAKER_FEE   = 0.0005

# ══════════════════════════════════════════════

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


# ── 指標計算（完全對齊 main.py）──────────────────────────────
def calc_rsi(series, period=14):
    delta = series.diff()
    gain  = delta.clip(lower=0).ewm(com=period - 1, adjust=False).mean()
    loss  = (-delta.clip(upper=0)).ewm(com=period - 1, adjust=False).mean()
    rs    = gain / loss
    return 100 - (100 / (1 + rs))

def prepare_df(ohlcv):
    df = pd.DataFrame(ohlcv, columns=["time","open","high","low","close","vol"])
    df["ema12"]   = df["close"].ewm(span=12,  adjust=False).mean()
    df["ema144"]  = df["close"].ewm(span=144, adjust=False).mean()
    df["ema169"]  = df["close"].ewm(span=169, adjust=False).mean()
    df["ema576"]  = df["close"].ewm(span=576, adjust=False).mean()
    df["ema676"]  = df["close"].ewm(span=676, adjust=False).mean()
    df["smallTop"] = df[["ema144","ema169"]].max(axis=1)
    df["smallBot"] = df[["ema144","ema169"]].min(axis=1)
    df["largeTop"] = df[["ema576","ema676"]].max(axis=1)
    df["largeBot"] = df[["ema576","ema676"]].min(axis=1)
    df["rsi"]    = calc_rsi(df["close"], 14)
    df["rsiMa"]  = df["rsi"].ewm(span=5, adjust=False).mean()
    return df

# ── 結構止損（完全對齊 main.py find_structure_sl）────────────
def find_structure_sl(df_slice, direction):
    w   = df_slice.iloc[-LOOKBACK_SL - 2:-2].copy().reset_index(drop=True)
    pts = []
    for i in range(2, len(w) - 2):
        if direction == "long":
            v = w.iloc[i]["low"]
            if all(v < w.iloc[i-k]["low"] for k in (1,2)) and \
               all(v < w.iloc[i+k]["low"] for k in (1,2)):
                pts.append(v)
        else:
            v = w.iloc[i]["high"]
            if all(v > w.iloc[i-k]["high"] for k in (1,2)) and \
               all(v > w.iloc[i+k]["high"] for k in (1,2)):
                pts.append(v)
    if pts:
        return pts[-1]
    return (df_slice.iloc[-7:-2]["low"].min() if direction == "long"
            else df_slice.iloc[-7:-2]["high"].max())

def calc_atr(df_slice, period=14):
    hi, lo, cl = df_slice["high"], df_slice["low"], df_slice["close"]
    tr = pd.concat([(hi - lo),
                    (hi - cl.shift()).abs(),
                    (lo - cl.shift()).abs()], axis=1).max(axis=1)
    return tr.ewm(span=period, adjust=False).mean().iloc[-1]


# ── 訊號收集（對齊 main.py check_signal，含多週期過濾）────────
def collect_signals(df, df_1h, timeframe):
    """
    回傳每筆訊號：entry / sl / tp1 / tp2 / be_sl / direction
    15m/30m 訊號加上 1h EMA 趨勢過濾
    """
    signals = []
    start   = max(LOOKBACK_SL + 10, 700)

    for i in range(start, len(df) - 1):
        row  = df.iloc[i]
        prev = df.iloc[i - 1]

        bullTrend    = row["ema144"] > row["ema576"]
        bearTrend    = row["ema144"] < row["ema576"]
        qqeTurnBlue  = row["rsiMa"] >= 50 and prev["rsiMa"] < 50
        qqeTurnRed   = row["rsiMa"] <  50 and prev["rsiMa"] >= 50

        longC1 = (row["close"] > row["largeTop"] and
                  row["close"] > row["smallBot"]  and
                  row["low"]   < row["smallBot"])
        longC2 = row["close"] > row["ema12"]
        shortC1 = (row["close"] < row["largeBot"] and
                   row["close"] < row["smallTop"]  and
                   row["high"]  > row["smallTop"])
        shortC2 = row["close"] < row["ema12"]

        for direction in (
            ("long"  if (bullTrend and longC1  and longC2  and qqeTurnBlue) else None),
            ("short" if (bearTrend and shortC1 and shortC2 and qqeTurnRed)  else None),
        ):
            if direction is None:
                continue

            # ── 多週期過濾（15m/30m 需符合 1h 趨勢）──
            if timeframe in ("15m", "30m") and df_1h is not None:
                row_1h = df_1h.iloc[-1]
                trend_ok = (row_1h["ema144"] > row_1h["ema576"]) if direction == "long" \
                           else (row_1h["ema144"] < row_1h["ema576"])
                if not trend_ok:
                    continue

            entry = row["close"]
            atr   = calc_atr(df.iloc[:i+1])
            sl_raw = find_structure_sl(df.iloc[:i+1], direction)

            if direction == "long":
                sl   = sl_raw - atr * 0.1
                risk = entry - sl
                if risk <= 0:
                    continue
                tp1  = entry + risk
                tp2  = entry + risk * 2
                be_sl = entry * (1 + TAKER_FEE * 2)
            else:
                sl   = sl_raw + atr * 0.1
                risk = sl - entry
                if risk <= 0:
                    continue
                tp1  = entry - risk
                tp2  = entry - risk * 2
                be_sl = entry * (1 - TAKER_FEE * 2)

            signals.append({
                "i": i, "dir": direction, "entry": entry,
                "sl": sl, "tp1": tp1, "tp2": tp2, "be_sl": be_sl,
            })

    return signals


# ── 回測模擬（分批止盈：50%@TP1，50%@TP2，TP1後止損移保本）─
def simulate_split_tp(df, signals):
    """
    回傳每筆交易的 pnl_pct（以風險為 1 單位）：
      TP1+TP2 全中  → +1.5 (0.5×1.0 + 0.5×2.0 = 1.5)
      只中 TP1，保本止損  → +0.5
      只中 TP1，然後被止損  → +0.5 - ~0 ≈ +0.5（保本後近 0 損失，簡化算 +0.5）
      SL 在 TP1 前被觸發  → -1.0
    """
    results = []
    for sig in signals:
        i       = sig["i"]
        entry   = sig["entry"]
        sl      = sig["sl"]
        tp1     = sig["tp1"]
        tp2     = sig["tp2"]
        be_sl   = sig["be_sl"]
        is_long = sig["dir"] == "long"

        tp1_hit   = False
        outcome   = None

        for j in range(i + 1, len(df)):
            fut = df.iloc[j]
            if not tp1_hit:
                # TP1 還沒觸及
                if is_long:
                    if fut["low"] <= sl:
                        outcome = -1.0; break
                    if fut["high"] >= tp1:
                        tp1_hit = True
                else:
                    if fut["high"] >= sl:
                        outcome = -1.0; break
                    if fut["low"] <= tp1:
                        tp1_hit = True
            else:
                # TP1 已觸及，止損移至保本
                if is_long:
                    if fut["low"] <= be_sl:
                        outcome = +0.5; break   # 保本離場（50% 已出，另50%≈保本）
                    if fut["high"] >= tp2:
                        outcome = +1.5; break
                else:
                    if fut["high"] >= be_sl:
                        outcome = +0.5; break
                    if fut["low"] <= tp2:
                        outcome = +1.5; break

        if outcome is None:
            outcome = +0.5 if tp1_hit else -1.0   # 還在場：TP1到了算+0.5，否則算輸

        results.append(outcome)

    return results


def capital_sim(results):
    equity = 100.0
    for r in results:
        equity += equity * (RISK_PCT / 100) * r
    return equity

def stats(results):
    if not results:
        return 0, 0, 0, 100.0
    wins     = sum(1 for r in results if r > 0)
    losses   = sum(1 for r in results if r <= 0)
    win_rate = wins / len(results) * 100
    cap      = capital_sim(results)
    return wins, losses, win_rate, cap


# ══════════════════════════════════════════════
# 主程式
# ══════════════════════════════════════════════
print("=" * 70)
print("  賽克斯策略回測 v2（結構SL + ATR緩衝 + 分批止盈 + 多週期確認）")
print(f"  幣種：{len(SYMBOLS)}個  |  時框：{', '.join(TIMEFRAMES)}")
print(f"  每筆風險：{RISK_PCT}%  |  止盈：50%@1:1 + 50%@1:2（TP1後移保本）")
print("=" * 70)

exchange = ccxt.okx()

# 聚合統計
all_results = {tf: [] for tf in TIMEFRAMES}
dir_results = {tf: {"long": [], "short": []} for tf in TIMEFRAMES}
sym_results = {}   # symbol → all tf combined

# 快取 1h 數據（多週期用）
cache_1h = {}

for symbol in SYMBOLS:
    name = symbol.split("/")[0]
    sym_results[name] = []
    print(f"\n{'─'*60}")
    print(f"  {name}")
    print(f"{'─'*60}")

    # 先抓 1h 數據（供 15m/30m 過濾用）
    try:
        ohlcv_1h = fetch_ohlcv_paginated(exchange, symbol, "1h", 700)
        if len(ohlcv_1h) >= 200:
            df_1h = prepare_df(ohlcv_1h)
            cache_1h[symbol] = df_1h
        else:
            cache_1h[symbol] = None
    except Exception:
        cache_1h[symbol] = None

    for tf in TIMEFRAMES:
        try:
            ohlcv = fetch_ohlcv_paginated(exchange, symbol, tf, TARGET_BARS)
        except Exception as e:
            print(f"  [{tf:3s}] 抓取失敗：{e}")
            continue

        if len(ohlcv) < 750:
            print(f"  [{tf:3s}] 資料不足（{len(ohlcv)} 根）")
            continue

        df      = prepare_df(ohlcv)
        df_1h_f = cache_1h.get(symbol) if tf in ("15m","30m") else None
        signals = collect_signals(df, df_1h_f, tf)

        if not signals:
            print(f"  [{tf:3s}] K線：{len(ohlcv):4d}  無訊號")
            continue

        results = simulate_split_tp(df, signals)
        w, l, wr, cap = stats(results)

        long_res  = simulate_split_tp(df, [s for s in signals if s["dir"] == "long"])
        short_res = simulate_split_tp(df, [s for s in signals if s["dir"] == "short"])
        lw, ll, lwr, _ = stats(long_res)
        sw, sl_c, swr, _ = stats(short_res)

        all_results[tf].extend(results)
        dir_results[tf]["long"].extend(long_res)
        dir_results[tf]["short"].extend(short_res)
        sym_results[name].extend(results)

        tf_label = tf if tf != "15m" else " 15m"
        print(f"  [{tf:3s}] K線:{len(ohlcv):4d}  訊號:{len(signals):3d}  "
              f"勝:{w:3d} 敗:{l:3d}  勝率:{wr:5.1f}%  資金:{cap:7.2f}%  "
              f"│ 多:{lwr:5.1f}% 空:{swr:5.1f}%")

        sleep(0.3)

# ══════════════════════════════════════════════
# 整體統計
# ══════════════════════════════════════════════
print("\n\n" + "═" * 70)
print(f"  整體統計")
print("═" * 70)

print(f"\n{'依時框':─<68}")
print(f"  {'時框':<6} {'訊號':>6} {'勝':>5} {'敗':>5} {'勝率':>7} {'資金':>9}  │  {'多勝率':>7} {'空勝率':>7}")
print(f"  {'─'*65}")

total_all = []
for tf in TIMEFRAMES:
    r = all_results[tf]
    total_all.extend(r)
    if not r:
        continue
    w, l, wr, cap = stats(r)
    lw, ll, lwr, _ = stats(dir_results[tf]["long"])
    sw, sl_c, swr, _ = stats(dir_results[tf]["short"])
    print(f"  {tf:<6} {len(r):>6} {w:>5} {l:>5} {wr:>6.1f}%  {cap:>8.2f}%  │  {lwr:>6.1f}%  {swr:>6.1f}%")

if total_all:
    w, l, wr, cap = stats(total_all)
    print(f"  {'─'*65}")
    print(f"  {'全部':<6} {len(total_all):>6} {w:>5} {l:>5} {wr:>6.1f}%  {cap:>8.2f}%")

print(f"\n{'依幣種':─<68}")
print(f"  {'幣種':<8} {'訊號':>6} {'勝':>5} {'敗':>5} {'勝率':>7} {'資金':>9}")
print(f"  {'─'*50}")
for name, r in sym_results.items():
    if not r:
        continue
    w, l, wr, cap = stats(r)
    print(f"  {name:<8} {len(r):>6} {w:>5} {l:>5} {wr:>6.1f}%  {cap:>8.2f}%")

print("\n" + "═" * 70)
print("  說明：資金從100%起 | 每筆風險1% | 勝=+1.5%，只中TP1=+0.5%，敗=-1%")
print("  多週期確認：15m/30m 訊號需通過 1h EMA144/576 方向過濾")
print("═" * 70)
