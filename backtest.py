import ccxt
import pandas as pd
from time import sleep

# ══════════════════════════════════════════════════════════════
# 回測參數（完全對齊 main.py）
# ══════════════════════════════════════════════════════════════
SYMBOLS = [
    "BTC/USDT:USDT",  "ETH/USDT:USDT",  "XRP/USDT:USDT",
    "SOL/USDT:USDT",  "BNB/USDT:USDT",  "ADA/USDT:USDT",
    "DOGE/USDT:USDT", "LINK/USDT:USDT", "AVAX/USDT:USDT",
    "SUI/USDT:USDT",  "DOT/USDT:USDT",  "LTC/USDT:USDT",
    "HYPE/USDT:USDT", "UNI/USDT:USDT",  "ATOM/USDT:USDT",
]
TIMEFRAMES  = ["15m", "30m", "1h", "4h"]
TARGET_BARS = 1500
RISK_PCT    = 1.0   # 每筆固定承擔本金 1%

# ── 做多參數（對齊 main.py）──
LONG_QQE_RSI = 7
LONG_QQE_SF  = 5
LONG_TP1     = 1.725
LONG_TP2_I   = 1.8       # 日內 (15m/30m)
LONG_TP2_S   = 2.5       # 波段 (1h/4h)
LONG_SL_BUF  = 0.08
LONG_SL_LB   = 17

# ── 做空參數（對齊 main.py）──
SHORT_QQE_RSI = 5
SHORT_QQE_SF  = 6
SHORT_TP1     = 2.0
SHORT_TP2_I   = 3.2
SHORT_TP2_S   = 4.0
SHORT_SL_BUF  = 0.03
SHORT_SL_LB   = 20

# ══════════════════════════════════════════════════════════════

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
            print(f"  抓取失敗：{e}"); break
        if not batch:
            break
        batch = [c for c in batch if c[0] < earliest_ts]
        if not batch:
            break
        all_ohlcv = batch + all_ohlcv
        sleep(0.2)
    return all_ohlcv[-target:]


# ── 指標計算（完全對齊 main.py）──────────────────────────────
def calc_rsi(series, period):
    delta = series.diff()
    gain  = delta.clip(lower=0).ewm(com=period - 1, adjust=False).mean()
    loss  = (-delta.clip(upper=0)).ewm(com=period - 1, adjust=False).mean()
    rs    = gain / loss
    return 100 - (100 / (1 + rs))

def prepare_df(ohlcv):
    df = pd.DataFrame(ohlcv, columns=["time","open","high","low","close","vol"])
    for s in (12, 144, 169, 576, 676):
        df[f"ema{s}"] = df["close"].ewm(span=s, adjust=False).mean()
    df["smallTop"] = df[["ema144","ema169"]].max(axis=1)
    df["smallBot"] = df[["ema144","ema169"]].min(axis=1)
    df["largeTop"] = df[["ema576","ema676"]].max(axis=1)
    df["largeBot"] = df[["ema576","ema676"]].min(axis=1)
    # 多空各自的 QQE（RSI週期和SF不同）
    df["rsiMa_L"] = calc_rsi(df["close"], LONG_QQE_RSI).ewm(span=LONG_QQE_SF,  adjust=False).mean()
    df["rsiMa_S"] = calc_rsi(df["close"], SHORT_QQE_RSI).ewm(span=SHORT_QQE_SF, adjust=False).mean()
    return df

def calc_atr(df_slice, period=14):
    hi, lo, cl = df_slice["high"], df_slice["low"], df_slice["close"]
    tr = pd.concat([(hi - lo),
                    (hi - cl.shift()).abs(),
                    (lo - cl.shift()).abs()], axis=1).max(axis=1)
    return tr.ewm(span=period, adjust=False).mean().iloc[-1]

def find_structure_sl(df_slice, direction, lookback):
    w   = df_slice.iloc[-lookback - 2:-2].copy().reset_index(drop=True)
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


# ── 訊號收集（完全對齊 main.py 含多週期過濾）────────────────
def collect_signals(df, df_1h, timeframe):
    signals   = []
    is_intraday = timeframe in ("15m", "30m")
    start = max(max(LONG_SL_LB, SHORT_SL_LB) + 10, 700)

    for i in range(start, len(df) - 1):
        row  = df.iloc[i]
        prev = df.iloc[i - 1]

        bullTrend = row["ema144"] > row["ema576"]
        bearTrend = row["ema144"] < row["ema576"]
        qqeBlue   = row["rsiMa_L"] >= 50 and prev["rsiMa_L"] < 50   # 多頭 QQE
        qqeRed    = row["rsiMa_S"] <  50 and prev["rsiMa_S"] >= 50  # 空頭 QQE

        longC1  = (row["close"] > row["largeTop"] and
                   row["close"] > row["smallBot"]  and
                   row["low"]   < row["smallBot"])
        longC2  = row["close"] > row["ema12"]
        shortC1 = (row["close"] < row["largeBot"] and
                   row["close"] < row["smallTop"]  and
                   row["high"]  > row["smallTop"])
        shortC2 = row["close"] < row["ema12"]

        is_long  = bullTrend and longC1  and longC2  and qqeBlue
        is_short = bearTrend and shortC1 and shortC2 and qqeRed

        for direction in ([d for d in
                           (("long" if is_long else None),
                            ("short" if is_short else None)) if d]):

            # ── 多週期確認（15m/30m 需符合 1h 趨勢）──
            if is_intraday and df_1h is not None:
                r1h = df_1h.iloc[-1]
                ok  = (r1h["ema144"] > r1h["ema576"]) if direction == "long" \
                      else (r1h["ema144"] < r1h["ema576"])
                if not ok:
                    continue

            entry = row["close"]
            atr   = calc_atr(df.iloc[:i+1])

            if direction == "long":
                tp1_m  = LONG_TP1
                tp2_m  = LONG_TP2_I if is_intraday else LONG_TP2_S
                sl_raw = find_structure_sl(df.iloc[:i+1], "long",  LONG_SL_LB)
                sl     = sl_raw - atr * LONG_SL_BUF
                risk   = entry - sl
                if risk <= 0:
                    continue
                tp1 = entry + risk * tp1_m
                tp2 = entry + risk * tp2_m
            else:
                tp1_m  = SHORT_TP1
                tp2_m  = SHORT_TP2_I if is_intraday else SHORT_TP2_S
                sl_raw = find_structure_sl(df.iloc[:i+1], "short", SHORT_SL_LB)
                sl     = sl_raw + atr * SHORT_SL_BUF
                risk   = sl - entry
                if risk <= 0:
                    continue
                tp1 = entry - risk * tp1_m
                tp2 = entry - risk * tp2_m

            signals.append({
                "i": i, "dir": direction, "entry": entry,
                "sl": sl, "tp1": tp1, "tp2": tp2,
                "tp1_m": tp1_m, "tp2_m": tp2_m,
            })

    return signals


# ── 模擬（分批止盈：50%@TP1，50%@TP2，TP1後止損移保本）──────
def simulate_split_tp(df, signals):
    """
    outcome（以 risk = 1 為單位）：
      TP1+TP2 全中  → 0.5×tp1_m + 0.5×tp2_m
      中TP1後保本   → 0.5×tp1_m
      SL先觸發      → -1.0
    """
    results = []
    for sig in signals:
        i        = sig["i"]
        entry    = sig["entry"]
        sl       = sig["sl"]
        tp1      = sig["tp1"]
        tp2      = sig["tp2"]
        tp1_m    = sig["tp1_m"]
        tp2_m    = sig["tp2_m"]
        is_long  = sig["dir"] == "long"
        tp1_hit  = False
        outcome  = None

        for j in range(i + 1, len(df)):
            fut = df.iloc[j]
            if not tp1_hit:
                if is_long:
                    if fut["low"]  <= sl:  outcome = -1.0;           break
                    if fut["high"] >= tp1: tp1_hit  = True
                else:
                    if fut["high"] >= sl:  outcome = -1.0;           break
                    if fut["low"]  <= tp1: tp1_hit  = True
            else:
                if is_long:
                    if fut["low"]  <= entry: outcome = 0.5 * tp1_m;  break  # 保本止損
                    if fut["high"] >= tp2:   outcome = 0.5*tp1_m + 0.5*tp2_m; break
                else:
                    if fut["high"] >= entry: outcome = 0.5 * tp1_m;  break
                    if fut["low"]  <= tp2:   outcome = 0.5*tp1_m + 0.5*tp2_m; break

        if outcome is None:
            outcome = 0.5 * tp1_m if tp1_hit else -1.0

        results.append(outcome)

    return results


def capital_sim(results):
    eq = 100.0
    for r in results:
        eq += eq * (RISK_PCT / 100) * r
    return eq

def stats(results):
    if not results:
        return 0, 0, 0.0, 100.0
    wins  = sum(1 for r in results if r > 0)
    losses = len(results) - wins
    wr    = wins / len(results) * 100
    cap   = capital_sim(results)
    return wins, losses, wr, cap


# ══════════════════════════════════════════════════════════════
# 主程式
# ══════════════════════════════════════════════════════════════
print("=" * 72)
print("  賽克斯策略回測 v3（完全對齊 main.py）")
print(f"  幣種：{len(SYMBOLS)}個  |  時框：{', '.join(TIMEFRAMES)}")
print(f"  多頭 TP1={LONG_TP1}  TP2日內={LONG_TP2_I}  TP2波段={LONG_TP2_S}")
print(f"  空頭 TP1={SHORT_TP1} TP2日內={SHORT_TP2_I} TP2波段={SHORT_TP2_S}")
print(f"  每筆風險：{RISK_PCT}%")
print("=" * 72)

exchange = ccxt.okx()

all_results = {tf: [] for tf in TIMEFRAMES}
dir_results = {tf: {"long": [], "short": []} for tf in TIMEFRAMES}
sym_results = {}
cache_1h    = {}

for symbol in SYMBOLS:
    name = symbol.split("/")[0]
    sym_results[name] = []
    print(f"\n{'─'*60}")
    print(f"  {name}")
    print(f"{'─'*60}")

    # 先抓 1h 做多週期過濾用
    try:
        o1h = fetch_ohlcv_paginated(exchange, symbol, "1h", 700)
        cache_1h[symbol] = prepare_df(o1h) if len(o1h) >= 200 else None
    except Exception:
        cache_1h[symbol] = None

    for tf in TIMEFRAMES:
        try:
            ohlcv = fetch_ohlcv_paginated(exchange, symbol, tf, TARGET_BARS)
        except Exception as e:
            print(f"  [{tf:3s}] 抓取失敗：{e}"); continue

        if len(ohlcv) < 750:
            print(f"  [{tf:3s}] 資料不足（{len(ohlcv)} 根）"); continue

        df      = prepare_df(ohlcv)
        df_1h_f = cache_1h.get(symbol) if tf in ("15m","30m") else None
        signals = collect_signals(df, df_1h_f, tf)

        if not signals:
            print(f"  [{tf:3s}] K線：{len(ohlcv):4d}  無訊號"); continue

        results   = simulate_split_tp(df, signals)
        w, l, wr, cap = stats(results)

        long_res  = simulate_split_tp(df, [s for s in signals if s["dir"] == "long"])
        short_res = simulate_split_tp(df, [s for s in signals if s["dir"] == "short"])
        _, _, lwr, _ = stats(long_res)
        _, _, swr, _ = stats(short_res)

        all_results[tf].extend(results)
        dir_results[tf]["long"].extend(long_res)
        dir_results[tf]["short"].extend(short_res)
        sym_results[name].extend(results)

        # 顯示多空各自的平均獲利倍數
        avg_r = sum(results) / len(results)
        print(f"  [{tf:3s}] K線:{len(ohlcv):4d}  訊號:{len(signals):3d}  "
              f"勝:{w:3d} 敗:{l:3d}  勝率:{wr:5.1f}%  資金:{cap:7.2f}%  "
              f"avgR:{avg_r:+.2f}  │ 多:{lwr:5.1f}% 空:{swr:5.1f}%")

        sleep(0.3)

# ══════════════════════════════════════════════════════════════
# 整體統計
# ══════════════════════════════════════════════════════════════
print("\n\n" + "═" * 72)
print("  整體統計")
print("═" * 72)

print(f"\n{'依時框':─<70}")
print(f"  {'時框':<6} {'訊號':>5} {'勝':>4} {'敗':>4} {'勝率':>7} {'資金':>9}  │  {'多勝率':>7} {'空勝率':>7}")
print(f"  {'─'*63}")

total_all = []
for tf in TIMEFRAMES:
    r = all_results[tf]
    total_all.extend(r)
    if not r:
        continue
    w, l, wr, cap = stats(r)
    _, _, lwr, _  = stats(dir_results[tf]["long"])
    _, _, swr, _  = stats(dir_results[tf]["short"])
    avg_r = sum(r) / len(r)
    print(f"  {tf:<6} {len(r):>5} {w:>4} {l:>4} {wr:>6.1f}%  {cap:>8.2f}%  │  {lwr:>6.1f}%  {swr:>6.1f}%")

if total_all:
    w, l, wr, cap = stats(total_all)
    avg_r = sum(total_all) / len(total_all)
    print(f"  {'─'*63}")
    print(f"  {'全部':<6} {len(total_all):>5} {w:>4} {l:>4} {wr:>6.1f}%  {cap:>8.2f}%  avgR={avg_r:+.3f}")

print(f"\n{'依幣種':─<70}")
print(f"  {'幣種':<8} {'訊號':>5} {'勝':>4} {'敗':>4} {'勝率':>7} {'資金':>9}")
print(f"  {'─'*48}")
for name, r in sym_results.items():
    if not r:
        continue
    w, l, wr, cap = stats(r)
    print(f"  {name:<8} {len(r):>5} {w:>4} {l:>4} {wr:>6.1f}%  {cap:>8.2f}%")

print("\n" + "═" * 72)
print("  說明：資金從100%起 | 每筆風險1%")
print(f"  多頭：TP1=1:{LONG_TP1}  日內TP2=1:{LONG_TP2_I}  波段TP2=1:{LONG_TP2_S}")
print(f"  空頭：TP1=1:{SHORT_TP1}  日內TP2=1:{SHORT_TP2_I}  波段TP2=1:{SHORT_TP2_S}")
print("  TP1中後止損移至保本 | 15m/30m需通過1h EMA144/576過濾")
print("═" * 72)
