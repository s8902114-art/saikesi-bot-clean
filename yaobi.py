# -*- coding: utf-8 -*-
"""妖幣觀察清單＋影子單（2026-10-02 v2）：**只列出來、不下單**；每張「如果照規則掛單」的結果記下來當前推紀錄。

規則（Barry 10-01 直播 → 回測定案版，`trading-backtest/_bt_yao_feat.py`／`_bt_yao_pat.py`／`_an_yao_delist.py`）：
  ①新幣：上市 ≤400 天（上市日＝min(OKX listTime, 幣安 onboardDate)）②上市後最高到最低回調 ≥80%
  ③日線綠K、量 > 前 10 根日K最大量 →【放量訊號】
  ④★放量前要有 ≥30 天「長盤整」（結構偵測，find_box 從 boxbreak.py／_lib_box 逐行搬來）——沒長盤整的訊號三段回測都是負的
  ⑤限價掛放量K實體中點，5 天（120 根 1H）內沒碰到＝失效；先跌破放量K低點＝作廢
  ⑥停損＝放量K低點 ×0.999；停利＝1R（中點 + (中點−停損)）；★成交那根只檢查停損（同根偏誤，手冊 10-02）
★回測（成交那根只看停損、扣費）：長盤整 2020~21 +0.141R(n529)／2022~24 +0.089(n948)／2025~26 +0.075(n1526，含補回的下架幣)；
  要穿過中點 0.1% 才成交時 2025~26 只剩 +0.044、穿 0.3% +0.001 → 優勢很薄，**不是翻倉訊號**。
★資料：放量判定用 OKX 日K（1Dutc）；成交與出場用 OKX 1H。影子單結果存 PERSIST_DIR/yaobi_shadow.json（redeploy 不丟）。
"""
import os, json, time, datetime as dt, traceback
import numpy as np
import requests

OKX = "https://www.okx.com"
UA = {"User-Agent": "Mozilla/5.0"}
DAY = 86_400_000; HOUR = 3_600_000
START_UTC_MS = int(dt.datetime(2026, 10, 3, 0, 0, tzinfo=dt.timezone.utc).timestamp() * 1000)   # v2 上線日；之前的訊號標「回填」
MAX_AGE_D, MIN_DD, VOL_LOOKBACK, MIN_HIST_D = 400, 0.80, 10, 40
FILL_H = 5 * 24
SL_MULT = 0.999
FEE_PCT = 0.10                 # 往返手續費 %（跟回測一樣，換算成 R 扣掉）
GAP, HEIGHT_MAX, SLOPE_MAX, MIN_DAYS = 0.15, 2.5, 0.30, 30         # find_box 參數（＝boxbreak.py）
SHOW_DAYS = 60
INTERVAL_SEC = 1800
FULL_TTL = 6 * 3600
_PERSIST = os.environ.get("PERSIST_DIR") or ("/data" if os.path.isdir("/data") else os.path.dirname(os.path.abspath(__file__)))
SHADOW_FILE = os.path.join(_PERSIST, "yaobi_shadow.json")

_state = {"last_run": 0, "err": "", "n_univ": 0, "watch": [], "sigs": [], "ms": 0}
_hist = {}                     # inst -> {"ts", "rows": [[t,o,h,l,c,q,confirm], ...]} 日K
_h1 = {}                       # inst -> {"rows": {t: [t,o,h,l,c]}, "tmin": 已抓到最早的 t}
_shadow = None                 # key -> 已結案的影子單結果（不再重算）


def _get(path, params, tries=5):
    """OKX 公開 GET；★429／50011 限流要顯式退避（手冊 09-27）。拿不到回 None。"""
    for a in range(tries):
        try:
            r = requests.get(OKX + path, params=params, headers=UA, timeout=12)
            j = r.json()
        except Exception:
            time.sleep(0.5 * 2 ** a); continue
        if r.status_code == 429 or str(j.get("code")) == "50011":
            time.sleep(0.5 * 2 ** a); continue
        if r.status_code != 200 or str(j.get("code")) != "0":
            return None
        return j.get("data") or []
    return None


# ───────── find_box：從 boxbreak.py 逐行搬來（改動＝改規格；對拍 trading-backtest/_chk_yaobi_port.py） ─────────
def find_box(O, H, L, C, t, gap=GAP, hmax=HEIGHT_MAX, smax=SLOPE_MAX, min_days=MIN_DAYS):
    if t < min_days + 1: return None
    y0 = np.log(max(C[t - 1], 1e-12)); hi_c = lo_c = C[t - 1]; s = t - 1; best = None
    n = 1; Sx = float(t - 1); Sy = y0; Sxx = float((t - 1) ** 2); Sxy = (t - 1) * y0
    while s - 1 >= 0:
        c = C[s - 1]
        if t - s >= min_days and (c > hi_c * (1 + gap) or c < lo_c * (1 - gap)): break
        hi2, lo2 = max(hi_c, c), min(lo_c, c)
        if hi2 / lo2 > hmax: break
        hi_c, lo_c = hi2, lo2; s -= 1
        y = np.log(max(c, 1e-12)); n += 1; Sx += s; Sy += y; Sxx += s * s; Sxy += s * y
        if n >= min_days:
            k = (n * Sxy - Sx * Sy) / (n * Sxx - Sx * Sx)
            if abs(np.expm1(k * n)) <= smax: best = s
    if best is None: return None
    return best, L[best:t].min(), H[best:t].max()


def binance_onboard():
    """幣安永續上市日（coin -> ms），走 www.binance.com（fapi 從 Railway 451）。OKX listTime 會在重新上架時重設。"""
    try:
        r = requests.get("https://www.binance.com/fapi/v1/exchangeInfo", headers=UA, timeout=20)
        if r.status_code != 200:
            return None
        out = {}
        for s in r.json().get("symbols") or []:
            if s.get("quoteAsset") != "USDT" or s.get("contractType") != "PERPETUAL" or not s.get("onboardDate"):
                continue
            b = s.get("baseAsset") or ""
            for pre in ("1000000", "1000"):
                if b.startswith(pre) and len(b) > len(pre):
                    b = b[len(pre):]; break
            out[b] = min(out.get(b, float("inf")), float(s["onboardDate"]))
        return out or None
    except Exception:
        return None


def universe():
    d = _get("/api/v5/public/instruments", {"instType": "SWAP"})
    if d is None:
        raise RuntimeError("instruments 抓不到")
    bn = binance_onboard()
    _state["bn_ok"] = bn is not None
    now = time.time() * 1000
    out = {}
    for x in d:
        iid = x.get("instId", "")
        if not iid.endswith("-USDT-SWAP") or str(x.get("instCategory")) != "1" or x.get("state") != "live":
            continue
        try:
            lt = float(x.get("listTime") or 0)
        except ValueError:
            continue
        if bn and iid.split("-")[0] in bn:
            lt = min(lt, bn[iid.split("-")[0]]) if lt > 0 else bn[iid.split("-")[0]]
        if lt > 0 and (now - lt) / DAY <= MAX_AGE_D + SHOW_DAYS:      # 多留 60 天：近 60 天的訊號當時可能還在 400 天內
            out[iid] = lt
    return out


def _rows(d):
    """日K：[t,o,h,l,c,USDT成交額(volCcyQuote),confirm]（回測的量也是 USDT 成交額）。"""
    return [[float(x[0]), float(x[1]), float(x[2]), float(x[3]), float(x[4]), float(x[7]), str(x[8]) == "1"] for x in d]


def daily(inst, list_ms, now_s):
    c = _hist.get(inst)
    if not c or now_s - c["ts"] > FULL_TTL:
        rows, after = [], None
        for _ in range(9):
            q = {"instId": inst, "bar": "1Dutc", "limit": "100"}
            if after:
                q["after"] = after
            d = _get("/api/v5/market/history-candles", q)
            if d is None:
                return None
            if not d:
                break
            rows += _rows(d); after = d[-1][0]
            if float(after) <= list_ms:
                break
            time.sleep(0.12)
        c = {"ts": now_s, "rows": sorted({r[0]: r for r in rows}.values(), key=lambda r: r[0])}
    else:
        d = _get("/api/v5/market/candles", {"instId": inst, "bar": "1Dutc", "limit": "3"})
        if d is None:
            return None
        m = {r[0]: r for r in c["rows"]}
        for r in _rows(d):
            m[r[0]] = r
        c = {"ts": c["ts"], "rows": sorted(m.values(), key=lambda r: r[0])}
    _hist[inst] = c
    return c["rows"]


def hourly(inst, t0):
    """1H K（只收已收盤），從 t0 到現在，由舊到新。快取：已抓過的不重抓，每輪只補最新一頁。拿不到回 None。"""
    c = _h1.get(inst) or {"rows": {}, "tmin": None}
    need_back = c["tmin"] is None or c["tmin"] > t0
    newest_before = max(c["rows"]) if c["rows"] else None
    after = None if not need_back else (None if c["tmin"] is None else int(c["tmin"]))
    pages = 0
    while True:
        q = {"instId": inst, "bar": "1H", "limit": "100"}
        if after:
            q["after"] = after
        d = _get("/api/v5/market/history-candles", q)
        if d is None:
            return None
        pages += 1
        for x in d:
            if str(x[8]) == "1":
                c["rows"][float(x[0])] = [float(x[0]), float(x[1]), float(x[2]), float(x[3]), float(x[4])]
        if not d:
            break
        oldest = float(d[-1][0])
        c["tmin"] = oldest if c["tmin"] is None else min(c["tmin"], oldest)
        if oldest <= t0 or pages >= 20:
            break
        if not need_back:                                          # 只補最新：一頁就夠（30 分鐘一輪、一頁 100 小時）
            break
        after = int(oldest); time.sleep(0.12)
    if need_back and c["tmin"] is not None and c["tmin"] > t0 and pages >= 20:
        return None
    if not need_back and newest_before is not None and d and float(d[-1][0]) > newest_before + HOUR:
        _h1.pop(inst, None)                                         # 最新一頁跟舊資料之間有洞（停機太久）→ 整段重抓
        return hourly(inst, t0)
    _h1[inst] = c
    return [c["rows"][k] for k in sorted(c["rows"]) if k >= t0]


def shadow_sim(H1, mid, vlow, th=0.0):
    """＝_bt_yao_feat.py 的成交／出場（逐行對應）。H1 從放量K收盤後第一根 1H 開始。
    回傳 (state, fill_ts, R, exit_ts)。state：等回踩／作廢(先破低)／沒回踩・失效／持倉中／贏／輸。"""
    fl = None
    for m in range(min(len(H1), FILL_H)):
        if H1[m][3] <= vlow: return "作廢（先破低）", None, None, None
        if H1[m][3] <= mid * (1 - th): fl = m; break
    if fl is None:
        return ("沒回踩・失效" if len(H1) >= FILL_H else "等回踩"), None, None, None
    if not vlow < mid: return "作廢（先破低）", None, None, None
    fee = FEE_PCT / ((mid - vlow) / mid * 100); risk = mid - vlow
    if H1[fl][3] <= vlow: return "輸", H1[fl][0], -1.0 - fee, H1[fl][0]
    for m in range(fl + 1, len(H1)):
        if H1[m][3] <= vlow: return "輸", H1[fl][0], -1.0 - fee, H1[m][0]
        if H1[m][2] >= mid + risk: return "贏", H1[fl][0], 1.0 - fee, H1[m][0]
    return "持倉中", H1[fl][0], None, None


def _load_shadow():
    global _shadow
    if _shadow is None:
        try:
            _shadow = json.load(open(SHADOW_FILE, encoding="utf-8"))
        except Exception:
            _shadow = {}
    return _shadow


def _save_shadow():
    try:
        tmp = SHADOW_FILE + ".tmp"
        json.dump(_shadow, open(tmp, "w", encoding="utf-8"), ensure_ascii=False)
        os.replace(tmp, SHADOW_FILE)
    except Exception as e:
        print("[妖幣] 影子單存檔失敗", e, flush=True)


def signals(inst, list_ms, rows, now_ms):
    """回傳 (觀察清單列 or None, [放量訊號...])。只用每個時點當下已收盤的日K。"""
    closed = [r for r in rows if r[6]]
    if len(closed) < VOL_LOOKBACK + 2:
        return None, []
    coin = inst.replace("-USDT-SWAP", "")
    hi_i = max(range(len(rows)), key=lambda i: rows[i][2])
    ath = rows[hi_i][2]; base = min(r[3] for r in rows[hi_i:]); last = rows[-1][4]
    age = (now_ms - list_ms) / DAY; dd_now = 1 - base / ath if ath > 0 else 0
    watch = None
    if dd_now >= MIN_DD and age <= MAX_AGE_D:
        watch = dict(inst=inst, coin=coin, age=round(age, 1), dd=round(dd_now * 100, 1),
                     up_from_low=round((last / base - 1) * 100, 1) if base > 0 else None, last=last)
    O = np.array([r[1] for r in closed]); Hh = np.array([r[2] for r in closed]); L = np.array([r[3] for r in closed]); C = np.array([r[4] for r in closed])
    out = []
    for k in range(MIN_HIST_D, len(closed)):                       # ＝回測 range(40, …)：上市未滿 40 根日K 不找訊號（對拍抓到的漏抄）
        t, o, h, l, c, q, _ = closed[k]
        if now_ms - (t + DAY) > SHOW_DAYS * DAY:
            continue
        if not (c > o and q > max(r[5] for r in closed[k - VOL_LOOKBACK:k])):
            continue
        age_k = (t + DAY - list_ms) / DAY
        j = int(np.argmax(Hh[:k + 1])); jl = j + int(np.argmin(L[j:k + 1])); dd_k = 1 - L[jl] / Hh[j]
        if dd_k < MIN_DD or age_k > MAX_AGE_D or age_k < 0:
            continue
        bx = find_box(O, Hh, L, C, k); box_d = (k - bx[0]) if bx else 0
        vmax = max(r[5] for r in closed[k - VOL_LOOKBACK:k])
        mid = (o + c) / 2; vlow = l * SL_MULT
        out.append(dict(inst=inst, coin=coin, day=int(t), t_close=int(t + DAY), age=round(age_k, 1), dd=round(dd_k * 100, 1),
                        vr=round(q / vmax, 2) if vmax > 0 else None, box_d=int(box_d), ok=box_d >= MIN_DAYS,
                        e=mid, sl=vlow, sld=round((mid - vlow) / mid * 100, 1), tps=[mid + (mid - vlow)],
                        backfill=(t + DAY) < START_UTC_MS, last=last))
    return watch, out


def resolve(s):
    """影子單：已結案的直接用存檔；沒結案的抓 1H 重算。"""
    sh = _load_shadow(); key = f"{s['inst']}|{s['day']}"
    if not s["ok"]:
        s.update(state="沒長盤整・不追", fill_ts=None, R=None, exit_ts=None, R1=None); return
    done = sh.get(key)
    if done and done.get("state") in ("贏", "輸", "作廢（先破低）", "沒回踩・失效"):
        s.update(state=done["state"], fill_ts=done.get("fill_ts"), R=done.get("R"), exit_ts=done.get("exit_ts"), R1=done.get("R1"))
        return
    H1 = hourly(s["inst"], s["t_close"])
    if H1 is None:
        s.update(state="1H 抓不到", fill_ts=None, R=None, exit_ts=None, R1=None); return
    H1 = [r for r in H1 if r[0] >= s["t_close"]]
    st, fts, R, ets = shadow_sim(H1, s["e"], s["sl"])
    st1, _, R1, _ = shadow_sim(H1, s["e"], s["sl"], th=0.001)        # 要穿過中點 0.1% 才算成交（排隊排不到的真實情況）
    s.update(state=st, fill_ts=fts, R=R, exit_ts=ets, R1=R1)
    if st in ("贏", "輸", "作廢（先破低）", "沒回踩・失效") and st1 not in ("等回踩", "持倉中"):
        sh[key] = dict(state=st, fill_ts=fts, R=R, exit_ts=ets, R1=R1, coin=s["coin"], ok=s["ok"], backfill=s["backfill"],
                       e=s["e"], sl=s["sl"], day=s["day"])


def tally(sigs):
    """戰績：只算「有長盤整」的影子單；上線後（前推）與回填分開。"""
    out = {}
    for grp, f in (("live", lambda s: not s["backfill"]), ("backfill", lambda s: s["backfill"])):
        g = [s for s in sigs if s["ok"] and f(s) and s.get("R") is not None]
        g1 = [s for s in sigs if s["ok"] and f(s) and s.get("R1") is not None]
        out[grp] = dict(n=len(g), win=sum(1 for s in g if s["R"] > 0), sumR=round(sum(s["R"] for s in g), 3),
                        avgR=round(sum(s["R"] for s in g) / len(g), 3) if g else None,
                        n1=len(g1), avgR1=round(sum(s["R1"] for s in g1) / len(g1), 3) if g1 else None,
                        open=sum(1 for s in sigs if s["ok"] and f(s) and s.get("state") in ("等回踩", "持倉中")))
    return out


def run(now_s=None):
    t0 = time.time()
    now_s = now_s or time.time(); now_ms = now_s * 1000
    univ = universe()
    watch, sigs, miss = [], [], 0
    for inst, lt in univ.items():
        try:
            rows = daily(inst, lt, now_s)
        except Exception:
            rows = None
        if rows is None:
            miss += 1; continue
        w, s = signals(inst, lt, rows, now_ms)
        if w:
            watch.append(w)
        sigs += s
    miss1 = 0
    for s in sigs:
        try:
            resolve(s)
        except Exception:
            s.update(state="1H 抓不到", fill_ts=None, R=None, exit_ts=None, R1=None)
        if s.get("state") == "1H 抓不到":
            miss1 += 1
    _save_shadow()
    for k in [k for k in _hist if k not in univ]:
        del _hist[k]
    keep = {s["inst"] for s in sigs if s.get("state") in ("等回踩", "持倉中")}
    for k in [k for k in _h1 if k not in keep]:
        del _h1[k]
    watch.sort(key=lambda w: -w["dd"])
    sigs.sort(key=lambda s: -s["day"])
    _state.update(last_run=now_s, n_univ=len(univ), watch=watch, sigs=sigs, stats=tally(sigs), ms=int((time.time() - t0) * 1000),
                  err="；".join(x for x in ((f"{miss} 個幣日K抓不到" if miss else ""), (f"{miss1} 筆 1H 抓不到（結果未知）" if miss1 else ""),
                                             ("" if _state.get("bn_ok") else "幣安上市日抓不到，只用 OKX listTime")) if x))


def dash_payload():
    """給儀表板「妖幣」分頁：只讀已算好的值、不打 API、不含憑證。永不拋例外。"""
    try:
        def num(v):
            try:
                f = float(v); return f if f == f and abs(f) != float("inf") else None
            except Exception:
                return None
        sigs = [dict(s, e=num(s["e"]), sl=num(s["sl"]), last=num(s.get("last")), tps=[num(x) for x in s["tps"]],
                     R=num(s.get("R")), R1=num(s.get("R1"))) for s in _state.get("sigs") or []]
        return dict(ok=True, ver=2, start=START_UTC_MS, last_run=_state.get("last_run"), n_univ=_state.get("n_univ", 0),
                    err=_state.get("err") or "", ms=_state.get("ms"), watch=list(_state.get("watch") or [])[:80],
                    sigs=sigs[:200], stats=_state.get("stats") or {},
                    rule=dict(max_age=MAX_AGE_D, min_dd=MIN_DD * 100, vol_lb=VOL_LOOKBACK, fill_days=FILL_H // 24, box_days=MIN_DAYS))
    except Exception as e:
        return dict(ok=False, err=type(e).__name__)


def loop(notify=None):
    while True:
        try:
            run()
        except Exception as e:
            _state["err"] = f"{type(e).__name__}: {str(e)[:120]}"
            print("[妖幣觀察] 例外", traceback.format_exc()[-400:], flush=True)
        time.sleep(INTERVAL_SEC)
