# -*- coding: utf-8 -*-
"""麥門「超直覺波段軟體」台指三指標的複刻（2026-10-11）—— **只顯示、不下單、不碰交易邏輯**。

逆推出處：trading-backtest/_mm_vf/、_mm_data/（記憶 project_1010_maimen_ladder_color）。
  ① 控盤階梯（Ladder_P1/P2）＝前一交易日台指近月 1 分 K 的 VWAP（close×vol）：
       參數二(趨勢)＝08:45~13:30、參數一(盤整)＝09:00~10:45；15:00 夜盤起換階；軟體畫「值 ±9 點」。
       驗證：37 支會議影片 133 天，P2 73/76 天 ≤3 點、P1 26/26 天 ≤2 點。
  ② 籌碼異常星（BigVol≥3000）＝某根 5 分 K 量 ≥3000 口，且該 5 分 K 開始於加權交易時間 09:00~13:25；
       15 分 K 圖只要包含任一根符合的 5 分 K 就畫星。（看得清楚的星召回 100%，抽查「規則有、量測無」15/16 其實有星）
  ③ 追價氛圍彩色K（15 分 K，黑<金<藍<紅，每根最多動一格）：
       紅→藍＝收盤 < EMA55（事件 100%／維持誤觸 0.1%）；黑→金＝收盤 > SMA35（99%／0.1%）。
       金↔藍、進紅、進黑：GBM（只用均線特徵，trading-backtest/_mm_vf/color_export.py 匯出 mmtx_model.json.gz）。
       四段交叉驗證 95.2%；公式版（COLOR_FORMULA）約 92%，模型檔讀不到時退回公式版。
資料：期交所公開 1 分 K（mis.taifex.com.tw getChartData1M，免登入）。實測 10-08 日盤 300 根對麥門資料
  量 100%、高 100%、收 99.7% 相同；單位＝單邊口數（＝逐筆檔 B+S/2），星星門檻 3000 才對得上。
"""
import os, json, gzip, time, re, itertools, traceback, datetime as dt
import numpy as np
import pandas as pd
import requests

MIS = "https://mis.taifex.com.tw/futures/api/"
HDR = {"Content-Type": "application/json", "User-Agent": "Mozilla/5.0"}
TPE = dt.timezone(dt.timedelta(hours=8))
_HERE = os.path.dirname(os.path.abspath(__file__))
_PERSIST = os.environ.get("PERSIST_DIR") or ("/data" if os.path.isdir("/data") else _HERE)
BARS_FILE = os.path.join(_PERSIST, "mmtx_1m.json.gz")
SEED_FILE = os.path.join(_HERE, "mmtx_seed.json.gz")
MODEL_FILE = os.path.join(_HERE, "mmtx_model.json.gz")
KEEP_DAYS = 75                 # 1 分 K 保留天數（日曆日）；EMA233/SMA233 暖機需要約 10 個交易日
SHOW_BARS = 240                # 網頁顯示最近幾根 15 分 K
STAR_VOL = 3000
LADDER_HALF = 9

NS = [8, 13, 21, 34, 35, 55, 89, 100, 120, 144, 200, 233]
COLOR_NAME = {0: "黑", 1: "金", 2: "藍", 3: "紅"}

_state = {"ok": False, "err": "", "last_fetch": 0, "last_calc": 0, "sym": {}, "payload": None, "n1m": 0}
_bars = {}                     # "YYYYmmddHHMM"(台北時間、分鐘開始) -> [o,h,l,c,v]
_model = None


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


def _load_store():
    global _bars
    for f in (SEED_FILE, BARS_FILE):
        try:
            if os.path.exists(f):
                with gzip.open(f, "rt", encoding="utf-8") as fh:
                    _bars.update(json.load(fh))
        except Exception:
            _state["err"] = "讀檔失敗 " + os.path.basename(f)


def _trim():
    now = dt.datetime.now(TPE)
    cut = (now - dt.timedelta(days=KEEP_DAYS)).strftime("%Y%m%d%H%M")
    fut = (now + dt.timedelta(days=1)).strftime("%Y%m%d%H%M")      # 未來時間的壞資料一旦存進檔就永遠不會被淘汰
    for k in [k for k in _bars if k < cut or k > fut]:
        _bars.pop(k, None)


def _save_store():
    try:
        _trim()
        tmp = BARS_FILE + ".tmp"
        with gzip.open(tmp, "wt", encoding="utf-8") as fh:
            json.dump(_bars, fh)
        os.replace(tmp, BARS_FILE)
    except Exception:
        pass


def update_data():
    n = 0; bad = []
    for mk_ in ("0", "1"):
        sid = near_symbol(mk_)
        if not sid:
            bad.append(("日盤", "夜盤")[int(mk_)] + "合約清單抓不到"); continue
        _state["sym"][mk_] = sid
        b = fetch_session(sid)
        if b is None:
            bad.append(sid + " 1分K 抓不到")
        elif b:
            _bars.update(b); n += len(b)
    if n:
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
    """回傳 (P2, P1) Series，對齊到每根 1 分 K：前一交易日 VWAP，當日 15:00 起生效。"""
    mins = m.index.hour * 60 + m.index.minute
    out = []
    for nm, (a, b) in (("P2", (525, 810)), ("P1", (540, 645))):
        d = m[(mins >= a) & (mins < b)]
        if d.empty:
            out.append(None); continue
        g = d.index.normalize()
        vw = (d.c * d.v).groupby(g).sum() / d.v.groupby(g).sum()
        s = pd.Series(vw.values, index=vw.index + pd.Timedelta(hours=15))
        out.append(s)
    return out


def stars15(m):
    """5 分 K 量 ≥3000 且開始於 09:00~13:25 → 該 5 分 K 所在的 15 分 K 標星。回傳 {15分K開始: 最大5分K量}。"""
    k5 = m.v.resample("5min", label="left", closed="left").sum()
    mins = k5.index.hour * 60 + k5.index.minute
    hit = k5[(k5 >= STAR_VOL) & (mins >= 540) & (mins < 810)]
    out = {}
    for t, v in hit.items():
        key = t.floor("15min")
        out[key] = max(out.get(key, 0), float(v))
    return out


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


def load_model():
    global _model
    if _model is None:
        try:
            with gzip.open(MODEL_FILE, "rt", encoding="utf-8") as fh:
                _model = json.load(fh)
        except Exception:
            _model = {}
    return _model


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


def colors(df):
    """回傳 (模型版顏色, 公式版顏色, 是否用到模型)。"""
    X, cols, p0, ce55, cs35, Ro, Ko = features(df)
    mdl = load_model()
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


def compute():
    m = minute_df()
    _state["n1m"] = len(m)
    if len(m) < 3000:
        return {"ok": False, "err": f"1 分 K 不足（{len(m)} 根），等資料累積"}
    df = to15(m)
    col, col_f, used_model = colors(df)
    P2, P1 = ladders(m)
    st = stars15(m)
    lad2 = P2.reindex(df.index, method="ffill") if P2 is not None else pd.Series(np.nan, index=df.index)
    lad1 = P1.reindex(df.index, method="ffill") if P1 is not None else pd.Series(np.nan, index=df.index)
    n = min(SHOW_BARS, len(df))
    rows = []
    for i in range(len(df) - n, len(df)):
        t = df.index[i]; r = df.iloc[i]
        rows.append([t.strftime("%m-%d %H:%M"), r.o, r.h, r.l, r.c, int(r.v), int(col[i]), int(col_f[i]),
                     None if np.isnan(lad2.iloc[i]) else round(float(lad2.iloc[i]), 1),
                     None if np.isnan(lad1.iloc[i]) else round(float(lad1.iloc[i]), 1),
                     int(st.get(t, 0))])
    # 最近一次換色
    last_chg = None
    for i in range(len(col) - 1, 0, -1):
        if col[i] != col[i - 1]:
            last_chg = [df.index[i].strftime("%m-%d %H:%M"), COLOR_NAME[int(col[i - 1])], COLOR_NAME[int(col[i])]]
            break
    lt = m.index[-1]
    live_bar = bool(lt < df.index[-1] + pd.Timedelta(minutes=14))     # 最後一根 15 分 K 還沒走完（盤中）
    return {"ok": True, "err": "", "bars": rows, "live_bar": live_bar, "model": used_model, "color": COLOR_NAME[int(col[-1])],
            "color_formula": COLOR_NAME[int(col_f[-1])], "last_change": last_chg,
            "ladder2": rows[-1][8], "ladder1": rows[-1][9], "half": LADDER_HALF,
            "last_minute": lt.strftime("%m-%d %H:%M"), "sym": dict(_state["sym"]),
            "fetched": int(_state["last_fetch"]), "n1m": len(m)}


def in_session(now=None):
    now = now or dt.datetime.now(TPE)
    mins = now.hour * 60 + now.minute
    wd = now.weekday()
    day = wd < 5 and 524 <= mins <= 826
    night = (wd < 5 and mins >= 899) or (0 < wd <= 5 and mins <= 301)
    return day or night


def dash_payload():
    try:
        p = _state.get("payload")
        if p:
            p = dict(p); p["age"] = int(time.time() - (_state.get("last_calc") or 0))
            p["fetch_err"] = _state.get("fetch_err") or ""
            # 盤中超過 5 分鐘沒抓到新資料 = 過期（期交所擋雲端 IP、或斷線）
            p["stale"] = bool(in_session() and time.time() - (_state.get("last_fetch") or 0) > 300)
            return p
        return {"ok": False, "err": _state.get("err") or "尚未算完第一次"}
    except Exception as e:
        return {"ok": False, "err": type(e).__name__}


def run_once():
    try:
        update_data()
        _trim()
        k = max(_bars) if _bars else ""
        sig = (len(_bars), k, tuple(_bars.get(k) or ()))
        if sig == _state.get("sig") and _state.get("payload"):
            return                      # 資料沒變 → 不重算（一次 2~3 秒 CPU，跟交易主迴圈共用 GIL）
        _save_store()
        p = compute()
        _state["sig"] = sig
        _state["payload"] = p
        _state["last_calc"] = time.time()
        _state["err"] = p.get("err", "")
        _state["ok"] = p.get("ok", False)
    except Exception as e:
        _state["err"] = f"{type(e).__name__}: {e}"[:200]
        traceback.print_exc()


def loop(notify=None):
    _load_store()
    while True:
        run_once()
        time.sleep(60 if in_session() else 600)
