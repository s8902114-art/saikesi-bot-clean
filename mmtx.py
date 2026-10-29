# -*- coding: utf-8 -*-
"""麥門「超直覺波段軟體」台指／加權指標的複刻（2026-10-11）—— **只顯示、不下單、不碰交易邏輯**。

逆推出處：trading-backtest/_mm_vf/、_mm_data/（記憶 project_1010_maimen_ladder_color）。
  ① 控盤階梯（Ladder_P1/P2，只有台指）＝當天日盤台指近月的口數加權均價（1 分 K 用 (高+低)/2×量 近似）：
       參數二(趨勢)＝08:47~13:25、參數一(盤整)＝09:00~10:45；當天 13:30 起換階；軟體畫「值 ±9 點」。（10-29 官方答案卷驗證，見 ladders()）
       驗證：37 支會議影片 133 天，P2 73/76 天 ≤3 點、P1 26/26 天 ≤2 點。
  ② 籌碼異常星（BigVol≥3000，只有台指 15 分）＝這根 15 分 K 裡最大的 5 分子K 量，只算 09:01~13:29（10-29 官方答案卷 100% 相等，見 stars15()）。
  ③ 追價氛圍彩色K（15 分 K，黑<金<藍<紅，每根最多動一格）：
       紅→藍＝收盤 < EMA55；黑→金＝收盤 > SMA35；金↔藍、進紅、進黑：GBM（只用均線特徵）。
       台指：影片標註 11,020 根四段交叉驗證 95.2%（color_export.py → mmtx_model.json.gz）。
       加權：台指顏色直接搬只對 35%、台指公式套加權 86%；台指＋加權標註合併訓練（來源旗標 src=1）
             排除暖機後 1,128 根四段交叉驗證 ~88%（color_export_tse.py → mmtx_model_tse.json.gz）。
       ★1H／日／週的顏色是同一個模型套上去的，**沒有標註可以驗**；每個週期前 WARM 根長均線還沒穩 → 不上色。
資料：期交所公開 1 分 K（mis.taifex.com.tw getChartData1M，免登入）。
  台指：10-08 日盤 300 根對麥門 量/高 100%、收 99.7%；近月＝最早到期（不可用量最大，見 near_symbol）。
  加權：SymbolID「TXF-S」＝加權指數 1 分 K，10-08 對麥門 267/267 根開高低收完全相同（沒有量）。
"""
import os, json, gzip, time, re, itertools, traceback, bisect, datetime as dt
import numpy as np
import pandas as pd
import requests

MIS = "https://mis.taifex.com.tw/futures/api/"
HDR = {"Content-Type": "application/json", "User-Agent": "Mozilla/5.0"}
TPE = dt.timezone(dt.timedelta(hours=8))
_HERE = os.path.dirname(os.path.abspath(__file__))
_PERSIST = os.environ.get("PERSIST_DIR") or ("/data" if os.path.isdir("/data") else _HERE)
BARS_FILE = os.path.join(_PERSIST, "mmtx_1m.json.gz")              # 台指 1 分 K
TSE_FILE = os.path.join(_PERSIST, "mmtx_tse_1m.json.gz")           # 加權 1 分 K
DAILY_FILE = os.path.join(_PERSIST, "mmtx_daily.json.gz")          # 兩個商品的日K（長期保存，週K由它組）
SEED_FILE = os.path.join(_HERE, "mmtx_seed.json.gz")
SEED_TSE_FILE = os.path.join(_HERE, "mmtx_tse_seed.json.gz")
SEED_DAILY_FILE = os.path.join(_HERE, "mmtx_daily_seed.json.gz")
MODEL_FILE = os.path.join(_HERE, "mmtx_model.json.gz")
MODEL_TSE_FILE = os.path.join(_HERE, "mmtx_model_tse.json.gz")
# 麥門「上線／下線」（到價推撥＝帳號自己設的警示價，不是公式）：從麥門文章圖讀出來的歷史值（_mm_data/updown_export.py 產生）
UPDOWN_FILE = os.path.join(_HERE, "mmtx_updown.json")
try:
    with open(UPDOWN_FILE, encoding="utf-8") as _fh:
        _UPDOWN = {k: v for k, v in json.load(_fh).items() if k in ("TX", "TSE")}
except Exception:
    _UPDOWN = {}
KEEP_DAYS = {"TX": 75, "TSE": 150}   # 1 分 K 保留日曆天數（加權一天只有 18 根 15 分 K，留久一點）
WARM = 280                     # 台指 15 分實測：75 天窗跟全史比，前 ~280 根之後 100% 一致 → 之前不上色
SHOW = {"15": 1500, "60": 1000, "D": 2000, "W": 1000}   # 圖表每個週期最多送幾根
INST_NAME = {"TX": "台指", "TSE": "加權"}
STAR_VOL = 3000
LADDER_HALF = 9

NS = [8, 13, 21, 34, 35, 55, 89, 100, 120, 144, 200, 233]
COLOR_NAME = {0: "黑", 1: "金", 2: "藍", 3: "紅"}

_state = {"ok": False, "err": "", "last_fetch": 0, "last_calc": 0, "sym": {}, "payload": None, "n1m": 0,
          "fetch_err": "", "charts": {}, "summ": {}, "sig": {}}
_bars = {}                     # 台指："YYYYmmddHHMM"(台北時間、分鐘開始) -> [o,h,l,c,v]
_bars_tse = {}                 # 加權：同上（量恆為 0）
_daily = {"TX": {}, "TSE": {}}  # "YYYYmmdd"(交易日) -> [o,h,l,c,v]
_models = {}
STORE = {"TX": _bars, "TSE": _bars_tse}
FILES = {"TX": BARS_FILE, "TSE": TSE_FILE}
SEEDS = {"TX": SEED_FILE, "TSE": SEED_TSE_FILE}
MODELS = {"TX": MODEL_FILE, "TSE": MODEL_TSE_FILE}


# ───────────────────────── 資料 ─────────────────────────
def _post(path, body, tries=3):
    for a in range(tries):
        try:
            r = requests.post(MIS + path, data=json.dumps(body), headers=HDR, timeout=15)
            if r.status_code == 200:
                j = r.json()
                if str(j.get("RtCode")) == "0":
                    return j.get("RtData") or {}
        except Exception:
            pass
        time.sleep(1.0 * 2 ** a)
    return None


def near_symbol(market):
    """market '0'=日盤 '1'=夜盤；回傳近月 SymbolID（TXF?#-F / -M）＝清單裡**最早到期**的台指月合約。
    ★不能用「量最大」：2026-09-16 結算日日盤 10 月量 34,274 > 9 月 26,545，但麥門 TXF.00 當天日盤
      逐分鐘 285/285 跟 9 月同價、夜盤起才換 10 月（_mm_data/roll_check.py，期交所逐筆檔對拍）。"""
    d = _post("getQuoteList", {"MarketType": market, "SymbolType": "F", "KindID": "1", "CID": "TXF",
                               "ExpireMonth": "", "RowSize": "全部", "PageNo": "", "SortColumn": "", "AscDesc": "A"})
    if not d:
        return None
    yr = dt.datetime.now(TPE).year
    best = None
    for q in d.get("QuoteList") or []:
        sid = q.get("SymbolID") or ""
        m = re.match(r"^TXF([A-L])(\d)-[FM]$", sid)
        if not m:
            continue
        y = yr // 10 * 10 + int(m.group(2))
        if y < yr - 1:                  # 跨十年（例：2029 年看到 TXFA0）
            y += 10
        key = (y, ord(m.group(1)) - ord("A"))
        if best is None or key < best[1]:
            best = (sid, key)
    return best[0] if best else None


def fetch_session(sid):
    """回傳 {分鐘開始字串: [o,h,l,c,v]}；時間欄 T 是分鐘「結束」(HHMMSS)；夜盤過午夜屬下一日。"""
    d = _post("getChartData1M", {"SymbolID": sid})
    if not d:
        return None
    q = d.get("Quote") or {}
    cdate = q.get("CDate")
    fields = d.get("Field") or []
    ticks = d.get("Ticks") or []
    if not cdate or not ticks or fields[:6] != ["T", "O", "H", "L", "C", "V"]:
        return {}
    base = dt.datetime.strptime(cdate, "%Y%m%d")
    out = {}
    for t in ticks:
        try:
            hh, mm = int(t[0][:2]), int(t[0][2:4])
            end = base + dt.timedelta(hours=hh, minutes=mm)
            if sid.endswith("-M") and hh < 15:
                end += dt.timedelta(days=1)
            st = end - dt.timedelta(minutes=1)
            key = st.strftime("%Y%m%d%H%M")
            if key in out:      # 夜盤尾端期交所會同時送 '046000' 與 '050000'（同一分鐘），保留第一筆
                continue
            out[key] = [float(t[1]), float(t[2]), float(t[3]), float(t[4]), float(t[5])]
        except Exception:
            continue
    return out


def _load_json(f):
    with gzip.open(f, "rt", encoding="utf-8") as fh:
        return json.load(fh)


def _dump(obj, path):
    tmp = path + ".tmp"
    with gzip.open(tmp, "wt", encoding="utf-8") as fh:
        json.dump(obj, fh)
    os.replace(tmp, path)


def _load_store():
    bad = []
    for inst in ("TX", "TSE"):
        for f in (SEEDS[inst], FILES[inst]):
            try:
                if os.path.exists(f):
                    STORE[inst].update(_load_json(f))
            except Exception:
                bad.append(os.path.basename(f))
    for f in (SEED_DAILY_FILE, DAILY_FILE):
        try:
            if os.path.exists(f):
                d = _load_json(f)
                for inst in ("TX", "TSE"):
                    _daily[inst].update(d.get(inst) or {})
        except Exception:
            bad.append(os.path.basename(f))
    if bad:
        _state["err"] = "讀檔失敗 " + "、".join(bad)


def _trim():
    now = dt.datetime.now(TPE)
    fut = (now + dt.timedelta(days=1)).strftime("%Y%m%d%H%M")      # 未來時間的壞資料一旦存進檔就永遠不會被淘汰
    for inst, b in STORE.items():
        cut = (now - dt.timedelta(days=KEEP_DAYS[inst])).strftime("%Y%m%d%H%M")
        for k in [k for k in b if k < cut or k > fut]:
            b.pop(k, None)
    futd = fut[:8]
    for d in _daily.values():
        for k in [k for k in d if k > futd]:
            d.pop(k, None)


def _save_store():
    try:
        _trim()
        for inst in ("TX", "TSE"):
            _dump(STORE[inst], FILES[inst])
        _dump(_daily, DAILY_FILE)
        _state["save_err"] = ""
    except Exception as e:
        _state["save_err"] = type(e).__name__


def update_data():
    n = {"TX": 0, "TSE": 0}; bad = []
    for mk_ in ("0", "1"):
        sid = near_symbol(mk_)
        if not sid:
            bad.append(("日盤", "夜盤")[int(mk_)] + "合約清單抓不到"); continue
        _state["sym"][mk_] = sid
        b = fetch_session(sid)
        if b is None:
            bad.append(sid + " 1分K 抓不到")
        elif b:
            _bars.update(b); n["TX"] += len(b)
    b = fetch_session("TXF-S")            # 加權指數（期交所的現貨行情，只有日盤）
    if b is None:
        bad.append("加權(TXF-S) 1分K 抓不到")
    elif b:
        _bars_tse.update(b); n["TSE"] += len(b)
    if n["TX"] or n["TSE"]:
        _state["last_fetch"] = time.time()
    _state["fetch_err"] = "；".join(bad)      # 抓不到要顯示在網頁上，不可以默默拿舊資料當新的
    return n


# ───────────────────────── 指標 ─────────────────────────
def minute_df(bars=None):
    b = bars if bars is not None else _bars
    if not b:
        return pd.DataFrame(columns=["o", "h", "l", "c", "v"])
    ks = sorted(b)
    a = np.array([b[k] for k in ks], dtype=float)
    idx = pd.to_datetime(ks, format="%Y%m%d%H%M")
    return pd.DataFrame(a, index=idx, columns=["o", "h", "l", "c", "v"])


def to15(m):
    r = m.resample("15min", label="left", closed="left")
    df = pd.DataFrame({"o": r.o.first(), "h": r.h.max(), "l": r.l.min(), "c": r.c.last(), "v": r.v.sum()}).dropna(subset=["c"])
    return df[df.v > 0]


def ladders(m):
    """回傳 (P2, P1) Series（索引＝生效時刻）：當天日盤近月的口數加權均價。
    ★2026-10-29 用訂閱拿到的官方答案卷破到逐筆 4 位小數（_mm_data/formula/ladder/verify.py）：
      P2＝08:47:00≤t<13:25:00、P1＝09:00:00≤t<10:45:00；舊版 [08:45,13:30) 跟官方中位差 5.3 點。
      1 分K 沒有逐筆價 → 用 (高+低)/2×量 近似（59 天 held-out：P2 中位差 0.40 點、100% ≤2 點；P1 0.72 點）。
      生效：當天 13:30 起（15 分K 的 13:30 那根就是新值；60 分K 的 12:45 根起點早於 13:30 → 從 15:00 根換；
      結算日舊月沒有 13:30 根 → 從 15:00 根換），跟官方 58/58 天、60 分 62/62 天一致。"""
    mins = m.index.hour * 60 + m.index.minute
    out = []
    for nm, (a, b) in (("P2", (527, 805)), ("P1", (540, 645))):
        d = m[(mins >= a) & (mins < b)]
        if d.empty:
            out.append(None); continue
        g = d.index.normalize(); px = (d.h + d.l) / 2
        vw = (px * d.v).groupby(g).sum() / d.v.groupby(g).sum()
        s = pd.Series(vw.values, index=vw.index + pd.Timedelta(hours=13, minutes=30))
        out.append(s)
    return out


def stars15(m):
    """籌碼異常星。回傳 {15分K開始: BigVol}（只回 BigVol≥3000 的，前端寫死 ≥3000 才畫星）。
    ★2026-10-29 官方答案卷破到 100% 相等（_mm_data/formula/stars_bigvol/verify.py，held-out 486 根全等、星 11/11）：
      BigVol＝這根 15 分K 裡三根 5 分子K 量的最大值，只算 1 分K 起點 09:01~13:29（09:00 那一分鐘整個不算）；
      只有起點 09:00~13:15 的 15 分K 有值。舊版從 09:00 起算 → 多報的星全在 09:00 那根。"""
    mins = m.index.hour * 60 + m.index.minute
    d = m.v[(mins >= 541) & (mins < 810)]
    if d.empty:
        return {}
    k5 = d.resample("5min", label="left", closed="left").sum()
    k5 = k5[k5 > 0]
    big = k5.groupby(k5.index.floor("15min")).max()
    return {t: float(v) for t, v in big.items() if v >= STAR_VOL}


def merge_close(m):
    """台指 13:45 那筆收盤成交（常常只有 1 口）併進日盤最後一根（13:44），官方 15/60 分K 都沒有 13:45 那根。
    （2026-10-29 跳空輔助逐根 100% 對上官方時確認的切法，_mm_data/formula/kspace_mine/kspace.py）"""
    if m.empty:
        return m
    t = m.index; is1345 = (t.hour == 13) & (t.minute == 45)
    if not is1345.any():
        return m
    m = m.copy(); m.index = t.where(~is1345, t - pd.Timedelta(minutes=1))
    return _agg(m, m.index) if m.index.duplicated().any() else m


def _lines(c):
    P = {}
    for n in NS:
        P[f"s{n}"] = c.rolling(n).mean()
        P[f"e{n}"] = c.ewm(span=n, adjust=False).mean()
    return P


def _formula_inputs(df, P):
    c, o = df.c, df.o
    gt = lambda a, b: ((P[a] if a != "c" else c) > P[b]).values
    R = gt("c", "e55") & gt("s21", "s100") & gt("s55", "s200") & (c > o).values
    K = ~gt("c", "s35") & ~gt("s55", "s200") & ~gt("e21", "s89") & ~gt("e120", "s200")
    B = gt("c", "e120") & gt("e34", "e120") & gt("c", "e55") & gt("c", "e8") & gt("c", "s200")
    G = ~gt("c", "s120") & ~gt("e21", "s100") & ~gt("c", "e55") & ~gt("e21", "s89")
    Ro = ~gt("c", "e55"); Ko = gt("c", "s35")
    return R, Ro, K, Ko, B, G


def sim_formula(R, Ro, K, Ko, B, G):
    s = 1; out = np.empty(len(R), np.int8)
    for i in range(len(R)):
        if s == 3:
            if Ro[i]: s = 2
        elif s == 0:
            if Ko[i]: s = 1
        elif s == 2:
            if R[i]: s = 3
            elif G[i]: s = 1
        else:
            if K[i]: s = 0
            elif B[i]: s = 2
        out[i] = s
    return out


def features(df):
    """回傳 (X 二維陣列, 欄名, 公式版顏色 p0, ce55, cs35)。欄位順序與 color_export.py 訓練時完全相同。"""
    c = df.c; o = df.o
    P = _lines(c)
    atr = (df.h - df.l).rolling(100).mean().values
    cv = c.values
    cols, X = [], []
    for k, L in P.items():
        Ls = L.values
        cols += [f"c-{k}", f"d{k}"]
        X += [(cv - Ls) / atr, (Ls - np.r_[np.nan, Ls[:-1]]) / atr]
    for a, b in itertools.combinations(list(P), 2):
        cols.append(f"{a}-{b}"); X.append((P[a].values - P[b].values) / atr)
    cols += ["ret", "dc"]
    X += [(cv - o.values) / atr, np.r_[np.nan, np.diff(cv)] / atr]
    R, Ro, K, Ko, B, G = _formula_inputs(df, P)
    p0 = sim_formula(R, Ro, K, Ko, B, G)
    le = np.nan; k_ = 0; pa = np.nan; last, since, mv = [], [], []
    for i in range(len(cv)):
        if p0[i] in (0, 3):
            le = float(p0[i]); k_ = 0; pa = cv[i]
        else:
            k_ += 1
        last.append(le); since.append(k_); mv.append((cv[i] - pa) / atr[i] if atr[i] == atr[i] else np.nan)
    cols += ["last_ext", "since_ext", "move_since"]
    X += [np.array(last), np.array(since, float), np.array(mv)]
    return np.column_stack(X), cols, p0, (c > P["e55"]).values, (c > P["s35"]).values, Ro, Ko


# ───────────────────────── 各週期 K 棒 ─────────────────────────
def _agg(x, key):
    g = x.groupby(key)
    return pd.DataFrame({"o": g.o.first(), "h": g.h.max(), "l": g.l.min(), "c": g.c.last(), "v": g.v.sum()})


def to15_tse(m):
    """加權 15 分 K：13:30 之後的收盤撮合併入 13:15 那根（＝標註管線 _mm_vf/fast_frame.bars('TSE15') 的切法）。
    加權沒有量（期交所 TXF-S 量恆 0）→ 不可以用 v>0 篩。"""
    if m.empty:
        return m
    x = m.copy()
    x.index = x.index.where(~((x.index.hour == 13) & (x.index.minute >= 30)), x.index.normalize() + pd.Timedelta("13:15:00"))
    return _agg(x, x.index.floor("15min"))


def to60(m, inst):
    """60 分 K：台指日盤從 08:45 起算（08:45、09:45…12:45）、夜盤從 15:00；加權從 09:00（13:00 那根含收盤）。"""
    if m.empty:
        return m
    idx = m.index; mins = idx.hour * 60 + idx.minute; d = idx.normalize()
    if inst == "TX":
        anc = np.where((mins >= 525) & (mins < 900), (d + pd.Timedelta(minutes=525)).values,
                       np.where(mins >= 900, (d + pd.Timedelta(minutes=900)).values,
                                (d - pd.Timedelta(days=1) + pd.Timedelta(minutes=900)).values))
        anc = pd.DatetimeIndex(anc)
    else:
        anc = d + pd.Timedelta(minutes=540)
    key = anc + ((idx - anc) // pd.Timedelta(hours=1)) * pd.Timedelta(hours=1)
    df = _agg(m, key)
    return df[df.v > 0] if inst == "TX" else df


def trade_dates(m, inst):
    """每根 1 分 K 屬於哪個交易日。加權＝日期；台指＝夜盤算「下一個日盤」那天（期交所慣例，連假前的夜盤算到假後）。
    還沒有下一個日盤（今晚的夜盤）→ 先記下一個工作日，等日盤資料進來會被改正（update_daily 會整段重算）。"""
    idx = m.index; d = idx.normalize()
    if inst != "TX":
        return d
    mins = idx.hour * 60 + idx.minute
    day = (mins >= 525) & (mins < 900)
    days = np.array(sorted(set(d[day].values)))
    nst = np.where(mins >= 900, d.values, (d - pd.Timedelta(days=1)).values)      # 這段夜盤開始的那天
    nxt = (pd.DatetimeIndex(nst) + pd.offsets.BDay(1)).values
    if len(days):
        pos = np.searchsorted(days, nst, side="right")
        td = np.where(pos < len(days), days[np.minimum(pos, len(days) - 1)], nxt)
    else:
        td = nxt
    return pd.DatetimeIndex(np.where(day, d.values, td))


def update_daily(inst, m):
    """用最近 10 天的 1 分 K 重算日K、覆蓋進長期日K存檔（台指從某天 15:00 夜盤開始切，第一根交易日才完整）。"""
    if m.empty:
        return
    d0 = pd.Timestamp(m.index[-1].date()) - pd.Timedelta(days=10)
    x = m[m.index >= (d0 + pd.Timedelta(hours=15) if inst == "TX" else d0)]
    if x.empty:
        return
    dd = _agg(x, trade_dates(x, inst))
    if inst == "TX":
        dd = dd[dd.v > 0]
    if dd.empty:
        return
    D = _daily[inst]; lo = dd.index[0].strftime("%Y%m%d")
    for k in [k for k in D if k >= lo]:
        D.pop(k, None)
    for t, r in zip(dd.index, dd.itertuples()):
        D[t.strftime("%Y%m%d")] = [float(r.o), float(r.h), float(r.l), float(r.c), float(r.v)]


def daily_df(inst):
    D = _daily[inst]
    if not D:
        return pd.DataFrame(columns=["o", "h", "l", "c", "v"])
    ks = sorted(D)
    return pd.DataFrame(np.array([D[k] for k in ks], float), index=pd.to_datetime(ks, format="%Y%m%d"), columns=["o", "h", "l", "c", "v"])


def to_week(dd):
    if dd.empty:
        return dd
    return _agg(dd, dd.index - pd.to_timedelta(dd.index.weekday, unit="D"))


def load_model(inst="TX"):
    if inst not in _models:
        try:
            _models[inst] = _load_json(MODELS[inst])
        except Exception:
            _models[inst] = {}
    return _models[inst]


def gbm_proba(mdl, X):
    """HistGradientBoosting（二元）的純 numpy 推論：raw = baseline + Σ 葉值；p = sigmoid(raw)。"""
    raw = np.full(len(X), float(mdl["baseline"]))
    for tr in mdl["trees"]:
        f, th, mgl, lf, rt, leaf, val = (np.asarray(tr[k]) for k in ("f", "th", "mgl", "l", "r", "leaf", "v"))
        node = np.zeros(len(X), dtype=np.int64)
        for _ in range(64):
            act = ~leaf[node].astype(bool)
            if not act.any():
                break
            n_ = node[act]
            x = X[act, f[n_]]
            go_left = np.where(np.isnan(x), mgl[n_].astype(bool), x <= th[n_])
            node[act] = np.where(go_left, lf[n_], rt[n_])
        raw += val[node]
    return 1.0 / (1.0 + np.exp(-raw))



def _wma(s, n):
    """線性加權均線（最近一根權重 n），前 n−1 根 NaN。"""
    v = s.values.astype(float); w = np.arange(1, n + 1, dtype=float)
    out = np.full(len(v), np.nan)
    if len(v) >= n:
        out[n - 1:] = np.convolve(v, w[::-1], mode="valid") / w.sum()
    return pd.Series(out, index=s.index)


DAILY_P = {"TX": (22, 9, 21), "TSE": (21, 8, 20)}


def colors_daily(dd, inst="TX"):
    """日K 彩色K 參數一。★2026-10-29 用訂閱的官方答案卷破解：台指 28 年 6,993 根、加權 26 年 6,624 根逐根 0 錯
    （_mm_data/formula/color_daily/verify.py）。無狀態，三條件數幾條成立：0 黑／1 金／2 藍／3 紅。
    台指 A 收>SMA22、B 收>9 天前收（剛好相等沿用前一天的 B）、C SMA9>WMA21；加權各少 1（21／8／SMA8>WMA20）。"""
    L1, m, b1 = DAILY_P.get(inst, DAILY_P["TX"]); c = dd.c
    A = (c > c.rolling(L1).mean()).values
    B = (c > c.shift(m)).values.copy()
    for i in np.where((c == c.shift(m)).values)[0]:
        B[i] = B[i - 1]
    C = (c.rolling(m).mean() > _wma(c, b1)).values
    return (A.astype(np.int8) + B + C).astype(np.int8)


def colors15_v2(df, inst="TX"):
    """15 分K 彩色K 參數一（★2026-10-30 官方答案卷反推的公式，取代 GBM 模型）。
    台指：狀態機 紅出 c<EMA55、黑出 c>SMA35、金藍＝c>SMA121 藍、進紅 藍且 c>EMA55 且 SMA60>SMA200 且 EMA55>WMA120、
         進黑 金且 c<SMA35 且 SMA60<SMA200 且 WMA34<WMA120（10 月 held-out 96.9%，舊 GBM 93.3%；_mm_data/formula/tx15_p1_r2/）。
    加權：三線狀態機 紅出線＝黑出線＝EMA55、SMA235 分藍金（SMA 不足 n 根用目前為止平均）、進紅 EMA34>SMA200、進黑 EMA35<SMA200
         （held-out 98.0%、2024 影片 99.6%；_mm_data/formula/daily_p2_tse_r2/）。起始狀態＝金。"""
    c = df.c; cv = c.values.astype(float); n = len(cv); out = np.empty(n, np.int8); s = 1
    e = lambda k: c.ewm(span=k, adjust=False).mean().values
    if inst == "TX":
        sm = lambda k: c.rolling(k).mean().values
        e55 = e(55); s35 = sm(35); s121 = sm(121); s60 = sm(60); s200 = sm(200); w120 = _wma(c, 120).values; w34 = _wma(c, 34).values
        red_in = (cv > e55) & (s60 > s200) & (e55 > w120); blk_in = (cv < s35) & (s60 < s200) & (w34 < w120)
        red_out = cv < e55; blk_out = cv > s35; wb = cv > s121
        for i in range(n):
            if s == 3:
                if red_out[i]: s = 2 if wb[i] else 1
            elif s == 0:
                if blk_out[i]: s = 2 if wb[i] else 1
            else:
                s = 2 if wb[i] else 1
                if s == 2 and red_in[i]: s = 3
                elif s == 1 and blk_in[i]: s = 0
            out[i] = s
        return out
    sx = lambda k: c.rolling(k, min_periods=1).mean().values
    FR = e(55); SW = sx(235); ar = e(34) > sx(200); ak = e(35) < sx(200)
    for i in range(n):
        ar_ = cv[i] > FR[i]; bk = cv[i] < FR[i]; aw = cv[i] > SW[i]
        if s == 3:
            if not ar_: s = 2 if aw else 1
        elif s == 0:
            if not bk: s = 2 if aw else 1
        elif s == 2:
            if ar_ and ar[i]: s = 3
            elif not aw: s = 1
        else:
            if bk and ak[i]: s = 0
            elif aw: s = 2
        out[i] = s
    return out


def colors(df, inst="TX"):
    """回傳 (模型版顏色, 公式版顏色, 是否用到模型)。加權模型多一欄來源旗標 src=1（台指＋加權合併訓練）。"""
    X, cols, p0, ce55, cs35, Ro, Ko = features(df)
    mdl = load_model(inst)
    if inst != "TX":
        X = np.column_stack([X, np.ones(len(X))]); cols = cols + ["src"]
    if not mdl or mdl.get("cols") != cols:
        return p0, p0, False
    pb = gbm_proba(mdl["weak"], X); pr = gbm_proba(mdl["red"], X); pk = gbm_proba(mdl["black"], X)
    s = 1; out = np.empty(len(X), np.int8)
    for i in range(len(X)):
        if s == 3:
            if Ro[i]: s = 2
        elif s == 0:
            if Ko[i]: s = 1
        elif s == 2:
            if ce55[i] and pr[i] > 0.5: s = 3
            elif pb[i] < 0.5: s = 1
        else:
            if (not cs35[i]) and pk[i] > 0.5: s = 0
            elif pb[i] > 0.5: s = 2
        out[i] = s
    return out, p0, True


# ───────────────────────── 組裝 ─────────────────────────
def _ts(t):
    """台北時間「當成 UTC」的秒數：圖表程式庫照 UTC 顯示 → 畫面上剛好是台北時間。（naive Timestamp.timestamp() 視為 UTC）"""
    return int(pd.Timestamp(t).timestamp())


def _lad(P, index):
    if P is None or not len(index):
        return None
    v = P.reindex(index, method="ffill").values
    return [None if x != x else round(float(x), 1) for x in v]


def _rows(df, col, inst, lad2=None, lad1=None, st=None, n_show=1500, bs=None):
    """圖表用：[時間秒, 開, 高, 低, 收, 量, 顏色(-1=暖機不上色), 階梯P2, 階梯P1, 星(5分K量,0=無)]。"""
    n = len(df); i0 = max(0, n - n_show); nd = 0 if inst == "TX" else 2
    o, h, l, c, v = (df[k].values for k in ("o", "h", "l", "c", "v"))
    out = []
    for i in range(i0, n):
        t = df.index[i]
        out.append([_ts(t), round(float(o[i]), nd), round(float(h[i]), nd), round(float(l[i]), nd), round(float(c[i]), nd),
                    int(v[i]), int(col[i]) if i >= WARM else -1,
                    lad2[i] if lad2 else None, lad1[i] if lad1 else None, int(st.get(t, 0)) if st else 0]
                   + ([0, bs[i]] if bs else []))
    return out


def basis_for(df, minutes, tse_c):
    """期現價差＝台指收盤 − 加權收盤（★2026-10-30 照官方前端 FindSpreadLastPrice：每根台指 K 取「起點早於這根結束時間」的最後一筆加權收盤；
    所以夜盤是台指在動、加權停在日盤收盤價）。minutes＝K 棒長度（15/60）；日K 傳 None（同一天的加權日K收盤）。"""
    if tse_c is None or not len(tse_c) or not len(df):
        return [None] * len(df)
    if minutes is None:
        t = tse_c.reindex(df.index).ffill().values
    else:
        ends = df.index + pd.Timedelta(minutes=minutes)
        pos = tse_c.index.searchsorted(ends, side="left") - 1
        t = np.where(pos >= 0, tse_c.values[np.clip(pos, 0, None)], np.nan)
    b = df.c.values - t
    return [None if x != x else round(float(x), 1) for x in b]


def _bar_end(inst, t):
    if inst == "TSE" and t.hour == 13 and t.minute == 15:
        return t.normalize() + pd.Timedelta("13:31:00")       # 加權最後一根含收盤撮合
    return t + pd.Timedelta(minutes=14)


def compute_inst(inst):
    """回傳 (摘要, {週期: 圖表列})。摘要只看 15 分 K（標註驗證過的週期）。"""
    m = minute_df(STORE[inst])
    if len(m) < 3000:
        return {"ok": False, "err": f"{INST_NAME[inst]} 1 分 K 不足（{len(m)} 根），等資料累積"}, {}
    charts = {}
    mk = merge_close(m) if inst == "TX" else m           # ★台指 13:45 收盤那筆併進日盤最後一根（同官方切法）
    df = to15(mk) if inst == "TX" else to15_tse(m)
    _, col_f, _ = colors(df, inst)
    col = colors15_v2(df, inst); used = "v2"               # ★2026-10-30 官方答案卷反推的公式（台指 96.9%／加權 98.0%）
    P2 = P1 = None; st = {}
    if inst == "TX":
        P2, P1 = ladders(m); st = stars15(m)
    tse_c = None
    if inst == "TX":
        try:
            _mt = minute_df(STORE["TSE"]); tse_c = _mt.c if len(_mt) else None
        except Exception:
            tse_c = None
    charts["15"] = _rows(df, col, inst, _lad(P2, df.index), _lad(P1, df.index), st, SHOW["15"],
                         bs=basis_for(df, 15, tse_c) if inst == "TX" else None)
    d60 = to60(mk, inst)
    c60, _, _ = colors(d60, inst)
    charts["60"] = _rows(d60, c60, inst, _lad(P2, d60.index), _lad(P1, d60.index), None, SHOW["60"],
                         bs=basis_for(d60, 60, tse_c) if inst == "TX" else None)
    update_daily(inst, m)
    dd = daily_df(inst)
    if len(dd) >= 3:
        cD = colors_daily(dd, inst)                                                           # ★日K＝破解公式（官方 100%）
        bsD = None
        if inst == "TX":
            try: bsD = basis_for(dd, None, daily_df("TSE").c)
            except Exception: bsD = None
        charts["D"] = _rows(dd, cD, inst, n_show=SHOW["D"], bs=bsD)
        wk = to_week(dd)
        cW, _, _ = colors(wk, inst); charts["W"] = _rows(wk, cW, inst, n_show=SHOW["W"])
    lt = m.index[-1]
    live = bool(lt < _bar_end(inst, df.index[-1]))           # 最後一根 15 分 K 還沒走完（盤中）
    ci = len(df) - 2 if live and len(df) > 1 else len(df) - 1
    last_chg = None
    for i in range(len(col) - 1, max(WARM, 1) - 1, -1):
        if col[i] != col[i - 1]:
            last_chg = [df.index[i].strftime("%m-%d %H:%M"), COLOR_NAME[int(col[i - 1])], COLOR_NAME[int(col[i])]]
            break
    s = {"ok": True, "err": "", "name": INST_NAME[inst], "model": used, "live_bar": live,
         "color": COLOR_NAME[int(col[-1])], "c": int(col[-1]), "color_formula": COLOR_NAME[int(col_f[-1])],
         "closed": [df.index[ci].strftime("%m-%d %H:%M"), int(col[ci])], "last_change": last_chg,
         "last": round(float(df.c.iloc[-1]), 2), "last_minute": lt.strftime("%m-%d %H:%M"), "n1m": len(m)}
    if inst == "TX":
        s["ladder2"] = charts["15"][-1][7]; s["ladder1"] = charts["15"][-1][8]; s["half"] = LADDER_HALF
        s["stars"] = [[t.strftime("%m-%d %H:%M"), int(v)] for t, v in sorted(st.items())[-12:]]
    return s, charts


def in_session(now=None):
    now = now or dt.datetime.now(TPE)
    mins = now.hour * 60 + now.minute
    wd = now.weekday()
    day = wd < 5 and 524 <= mins <= 826
    night = (wd < 5 and mins >= 899) or (0 < wd <= 5 and mins <= 301)
    return day or night


def dash_payload():
    """/api 用的摘要（不含 K 棒；K 棒走 dash_chart）。永不拋例外。"""
    try:
        p = _state.get("payload")
        if p:
            p = dict(p); p["age"] = int(time.time() - (_state.get("last_calc") or 0))
            p["fetch_err"] = _state.get("fetch_err") or ""
            # 盤中超過 5 分鐘沒抓到新資料 = 過期（期交所擋雲端 IP、或斷線）
            p["stale"] = bool(in_session() and time.time() - (_state.get("last_fetch") or 0) > 300)
            p["updown"] = _UPDOWN
            return p
        return {"ok": False, "err": _state.get("err") or "尚未算完第一次"}
    except Exception as e:
        return {"ok": False, "err": type(e).__name__}


def dash_chart(inst, tf, since=None):
    """圖表 K 棒。since＝前端手上最後一根的時間秒：對得上就只回那根之後（含那根，它可能還在變），對不上回全部。"""
    try:
        if inst not in STORE or tf not in SHOW:
            return {"ok": False, "err": "參數錯誤"}
        rows = (_state.get("charts") or {}).get((inst, tf))
        if not rows:
            return {"ok": False, "err": _state.get("err") or "尚未算完第一次"}
        meta = {"ok": True, "s": inst, "tf": tf, "ladder": inst == "TX" and tf in ("15", "60"),
                "vol": inst == "TX", "prec": 0 if inst == "TX" else 2, "half": LADDER_HALF, "n": len(rows)}
        if since is not None:
            ts = [r[0] for r in rows]
            i = bisect.bisect_left(ts, since)
            if i < len(rows) and rows[i][0] == since:
                return dict(meta, bars=rows[i:], full=False)
        return dict(meta, bars=rows, full=True)
    except Exception as e:
        return {"ok": False, "err": type(e).__name__}


def _sig(inst):
    b = STORE[inst]
    if not b:
        return None
    k = max(b)
    rec = (dt.datetime.strptime(k, "%Y%m%d%H%M") - dt.timedelta(days=2)).strftime("%Y%m%d%H%M")
    return (k, tuple(b[k]), sum(1 for x in b if x >= rec))     # 不含總筆數：每輪淘汰舊資料不該觸發重算


def run_once():
    try:
        update_data()
        _trim()
        sigs = {i: _sig(i) for i in STORE}
        todo = [i for i in STORE if sigs[i] != _state["sig"].get(i) or i not in _state["summ"]]
        if not todo:
            return                      # 資料沒變 → 不重算（跟交易主迴圈共用 GIL）
        charts = dict(_state["charts"]); summ = dict(_state["summ"])
        for inst in todo:
            s, ch = compute_inst(inst)
            summ[inst] = s
            for tf, rows in ch.items():
                charts[(inst, tf)] = rows
            if s.get("ok"):
                _state["sig"][inst] = sigs[inst]
        _save_store()
        _state["charts"] = charts; _state["summ"] = summ
        _state["payload"] = {"ok": any(v.get("ok") for v in summ.values()), "err": "",
                             "TX": summ.get("TX") or {"ok": False}, "TSE": summ.get("TSE") or {"ok": False},
                             "sym": dict(_state["sym"]), "fetched": int(_state["last_fetch"]), "warm": WARM}
        _state["last_calc"] = time.time()
        _state["err"] = "；".join(v.get("err", "") for v in summ.values() if v.get("err"))
        _state["ok"] = _state["payload"]["ok"]
    except Exception as e:
        _state["err"] = f"{type(e).__name__}: {e}"[:200]
        traceback.print_exc()


def loop(notify=None):
    _load_store()
    while True:
        run_once()
        time.sleep(60 if in_session() else 600)
