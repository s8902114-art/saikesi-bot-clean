# -*- coding: utf-8 -*-
"""妖幣觀察清單（2026-10-02）：Barry 10-01 直播「打妖幣的眉角」做成清單，**只列出來給用戶判斷，不下單**。

規則（Barry 原話 → 我補的參數，回測 `trading-backtest/_bt_barry_vol.py`／`_an_barry_pump2.py`）：
  ①最高到最低回調 ≥80%（他：80~90%）②新幣：上市 ≤400 天（他：25年9月「勉強可以」、2024「淘汰」）
  ③日線出現往上拉、量明顯比前面幾根大的 K →【放量訊號】（我補：綠K 且量 > 前 10 根日K 的最大量）
  ④之後等它回踩再進 → 掛在放量K實體中點（我補：放量後 5 天內沒回踩＝失效）
  ⑤目標：他說「往上走個 10% 也蠻多了」；回測停損放底部才吃得到暴漲 → 停損＝上市高點後最低點 ×0.97，
    目標 +30%／+50%／+100% 三種都列，+1R 移保本（用戶翻倉規則）。
★回測（2025-01~2026-08，約 200 幣）：每筆平均 +0.13~0.15R（老幣同條件為負）；但押 50% 一次一筆
  模擬 5000 條路徑有約 80% 最後虧錢 → 這是**觀察清單**，不是翻倉訊號。頁面上要寫清楚。
★資料源＝OKX 日K（1Dutc，UTC 00:00 收盤＝台北 8 點）；上市日＝OKX listTime。回測用的是幣安上市日與日K，
  在 OKX 較晚上市的幣，「上市後最高點」會比回測少一段 → 回調% 可能偏小（寧可漏列，不會亂列）。
★無狀態：每次都從日K歷史重算，redeploy 不會丟東西。日K 只有日解析度：同一天同時碰到停損與目標 → 算停損（保守）。
"""
import time, datetime as dt, traceback
import requests

OKX = "https://www.okx.com"
UA = {"User-Agent": "Mozilla/5.0"}
DAY = 86_400_000
START_UTC_MS = int(dt.datetime(2026, 10, 2, 0, 0, tzinfo=dt.timezone.utc).timestamp() * 1000)   # 上線日；之前的訊號標「回填」
MAX_AGE_D = 400
MIN_DD = 0.80
VOL_LOOKBACK = 10
FILL_DAYS = 5
SL_MULT = 0.97
TARGETS = (0.30, 0.50, 1.00)
SHOW_DAYS = 60                 # 列最近 60 天的放量訊號
INTERVAL_SEC = 1800            # 30 分鐘重算一次（日K 一天才收一根；回踩/目標用當天未收盤那根判）
FULL_TTL = 6 * 3600            # 完整歷史 6 小時重抓一次，其餘只補最近 3 根

_state = {"last_run": 0, "err": "", "n_univ": 0, "watch": [], "sigs": [], "ms": 0}
_hist = {}                     # inst -> {"ts": 抓取時間, "rows": [[t,o,h,l,c,vol,confirm], ...] 由舊到新}


def _get(path, params, tries=5):
    """OKX 公開 GET；★429／50011 限流要顯式退避（手冊 09-27：被 except 吃掉＝靜默少資料）。拿不到回 None。"""
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


def universe():
    """OKX 加密幣 USDT 永續（instCategory=1，排除股票/商品），上市 ≤ MAX_AGE_D 天。"""
    d = _get("/api/v5/public/instruments", {"instType": "SWAP"})
    if d is None:
        raise RuntimeError("instruments 抓不到")
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
        if lt > 0 and (now - lt) / DAY <= MAX_AGE_D:
            out[iid] = lt
    return out


def _rows(d):
    return [[float(x[0]), float(x[1]), float(x[2]), float(x[3]), float(x[4]), float(x[5]), str(x[8]) == "1"] for x in d]


def daily(inst, list_ms, now_s):
    """日K（由舊到新）。完整歷史 FULL_TTL 重抓一次（翻頁到上市日），其餘只補最近 3 根。拿不到回 None。"""
    c = _hist.get(inst)
    if not c or now_s - c["ts"] > FULL_TTL:
        rows, after = [], None
        for _ in range(8):                                   # 100 根/頁 × 8 > 400 天
            q = {"instId": inst, "bar": "1Dutc", "limit": "100"}
            if after:
                q["after"] = after
            d = _get("/api/v5/market/history-candles", q)
            if d is None:
                return None
            if not d:
                break
            rows += _rows(d)
            after = d[-1][0]
            if float(after) <= list_ms:
                break
            time.sleep(0.12)
        rows = sorted({r[0]: r for r in rows}.values(), key=lambda r: r[0])
        c = {"ts": now_s, "rows": rows}
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


def analyze(inst, list_ms, rows, now_ms):
    """回傳 (觀察清單列 or None, [放量訊號...])。只用每個時點當下已收盤的日K（不偷看）。"""
    closed = [r for r in rows if r[6]]
    if len(closed) < VOL_LOOKBACK + 2:
        return None, []
    coin = inst.replace("-USDT-SWAP", "")
    # 當下：上市後最高、最高後最低、現價
    hi_i = max(range(len(rows)), key=lambda i: rows[i][2])
    ath = rows[hi_i][2]; base = min(r[3] for r in rows[hi_i:]); last = rows[-1][4]
    age = (now_ms - list_ms) / DAY
    dd_now = 1 - base / ath if ath > 0 else 0
    watch = None
    if dd_now >= MIN_DD:
        watch = dict(inst=inst, coin=coin, age=round(age, 1), dd=round(dd_now * 100, 1),
                     up_from_low=round((last / base - 1) * 100, 1) if base > 0 else None, last=last)
    sigs = []
    for k in range(VOL_LOOKBACK, len(closed)):
        t, o, h, l, c, v, _ = closed[k]
        if now_ms - (t + DAY) > SHOW_DAYS * DAY:
            continue
        if not (c > o and v > max(r[5] for r in closed[k - VOL_LOOKBACK:k])):
            continue
        sub = closed[:k + 1]
        hi = max(range(len(sub)), key=lambda i: sub[i][2])
        ath_k = sub[hi][2]; base_k = min(r[3] for r in sub[hi:])
        dd_k = 1 - base_k / ath_k if ath_k > 0 else 0
        age_k = (t + DAY - list_ms) / DAY
        if dd_k < MIN_DD or age_k > MAX_AGE_D:
            continue
        mid = (o + c) / 2; sl = base_k * SL_MULT
        if not (0 < sl < mid):
            continue
        vmax = max(r[5] for r in closed[k - VOL_LOOKBACK:k])
        s = dict(inst=inst, coin=coin, day=int(t), age=round(age_k, 1), dd=round(dd_k * 100, 1), vr=round(v / vmax, 2) if vmax > 0 else None,
                 e=mid, sl=sl, sld=round((mid - sl) / mid * 100, 1), tps=[mid * (1 + x) for x in TARGETS],
                 backfill=(t + DAY) < START_UTC_MS, state="等回踩", fill_day=None, res={}, last=rows[-1][4])
        after = [r for r in rows if r[0] > t]                 # 放量K之後（含今天未收盤）
        risk = mid - sl
        for j, r in enumerate(after[:FILL_DAYS]):
            if r[3] <= mid:
                s["fill_day"] = int(r[0]); s["state"] = "已回踩"
                break
        if s["fill_day"] is None:
            if len(after) >= FILL_DAYS and all(r[6] for r in after[:FILL_DAYS]):
                s["state"] = "沒回踩・失效"
        else:
            rest = [r for r in after if r[0] >= s["fill_day"]]
            for x, tp in zip(TARGETS, s["tps"]):
                stop, armed, out = sl, False, "持倉中"
                for r in rest:
                    if r[3] <= stop:
                        out = "保本" if armed else "停損"; break
                    if r[2] >= tp:
                        out = "到價"; break
                    if not armed and r[2] >= mid + risk:
                        armed, stop = True, mid
                s["res"][f"+{int(x * 100)}%"] = out
            s["state"] = "已回踩"
        sigs.append(s)
    return watch, sigs


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
        w, s = analyze(inst, lt, rows, now_ms)
        if w:
            watch.append(w)
        sigs += s
    for k in [k for k in _hist if k not in univ]:
        del _hist[k]
    watch.sort(key=lambda w: -w["dd"])
    sigs.sort(key=lambda s: -s["day"])
    _state.update(last_run=now_s, n_univ=len(univ), watch=watch, sigs=sigs, ms=int((time.time() - t0) * 1000),
                  err=(f"{miss} 個幣日K抓不到" if miss else ""))


def dash_payload():
    """給儀表板「妖幣」分頁：只讀已算好的值、不打 API、不含憑證。永不拋例外。"""
    try:
        def num(v):
            try:
                f = float(v); return f if f == f and abs(f) != float("inf") else None
            except Exception:
                return None
        sigs = [dict(s, e=num(s["e"]), sl=num(s["sl"]), last=num(s.get("last")), tps=[num(x) for x in s["tps"]])
                for s in _state.get("sigs") or []]
        return dict(ok=True, start=START_UTC_MS, last_run=_state.get("last_run"), n_univ=_state.get("n_univ", 0),
                    err=_state.get("err") or "", ms=_state.get("ms"), watch=list(_state.get("watch") or [])[:80],
                    sigs=sigs[:150], rule=dict(max_age=MAX_AGE_D, min_dd=MIN_DD * 100, vol_lb=VOL_LOOKBACK,
                                             fill_days=FILL_DAYS, sl_mult=SL_MULT, targets=[int(x * 100) for x in TARGETS]))
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
