# -*- coding: utf-8 -*-
"""NQ（那斯達克 100 期貨）盤面（2026-11-26，用戶：「邊破解 另外把 nq 也做到盤面上」）—— 只顯示、不下單、不碰交易邏輯。

資料：Yahoo Finance chart API `NQ=F`（連續近月，免金鑰、要帶 User-Agent）。
  ・2026-11-26 從 Railway 出口實測 query1／query2 都 200（dashboard 的 /egress 探針；_mm_data/nq/egress_live.py）。
  ・CME 行情在 Yahoo 延遲約 10 分鐘；美國假日（勞動節、感恩節…）那一盤 Yahoo 的分K 不給（日K 有）。
  ・跟 TradingView NQ1! 的 15 分K 比（2026-09-17~11-24 共 4,407 根，用 Yahoo 5 分K 組）：高低都相同 98.2%、正規盤量比中位 1.000；
    不同的集中在三個地方：每天 00:00 那根（Yahoo 少了前幾分鐘）、週日 18:00 開盤那根（Yahoo 沒有前 10 分鐘）、
    週五 16:55 最後一根（收盤混進一個假價）；另外有零星的開盤假價。後兩種在 clean5 修掉。
存（Railway volume /data，redeploy 後還在）：5 分K（留 120 天）、60 分K（留 1100 天；5 分K 沒蓋到的更早的 1H／日K 用它）、日K（長期）。
  記憶體裡的時間一律是 UTC 秒；畫圖用「台灣時間當成 UTC」的秒（跟台指那頁一樣，畫面上直接是台灣時間）。
畫：15 分／1H／日／週 K＋量；15 分圖加測穿版階梯帶（sms_zone）與缺口框（sms_gaps）。方塊（測穿記號）還沒破解，不畫。
  ★2026-11-26 第二刀（用戶：「麥門那些指標都要有」）：彩色K（15 分參數一／二、60 分、日K 參數一／二）、判色均線、控盤階梯（參數一／二）、籌碼星
    —— **全部是台指的公式直接套到 NQ，參數沒校正（我定的）**：彩色K／均線直接呼叫 mmtx 現成函式（inst="TX"）；
    階梯、星的「日盤」換成美股正規盤（見 ladders、stars15）。背離星、缺口（跳空輔助）在前端算（同台指）。NQ 沒有官方答案可以對。

★測穿版階梯帶（麥門 TV「Smart Money Steps [mai-now]」NQ 版；用戶沒有 NQ 版權限）：
  拿他說明頁 4 張示意圖上的 23 組線校正（2026-11-26 第二輪，_mm_data/nq/remeasure.py／align_img.py／scan_center*.py／width_check.py）：
    ・量圖：價格軸改用格線定（先前用標籤文字中心，比格線高 2 像素 → 每個價格被量低 1.5~2.6 點，那就是「一律低約 2 點」的來源）；
      每根 K 對到 TradingView NQ1! 的 15 分K（成交量序列相關 0.998~0.999），影線頂點／底點對 TV 高低只差 ±0.5 點。
    ・第 4 張（0McTRgcq）＝2026-05-20~05-28，截圖時開著回溯調整：整張比 TV 高 +585.5 點（加法、跟價位無關），扣掉才比。
    中心＝當天（紐約日期）紐約 09:45 ≤ t < 15:45 的 **1 分K** Σ((高+低+收)/3×量)/Σ量；拿不到 1 分K 的日子退回 **15 分K** 同一段（他自己也是這樣，同台指版）。
         9、10 月 16 組（他拿得到 1 分K）用 TV 1 分K：平均差 0.22 點、最大 0.62；5 月 7 組（拿不到）用 TV 15 分K：平均差 0.11、最大 0.38。
         起點 09:42／09:48 平均差就變 2.4／1.8 點、終點 15:39／15:51 變 1.8／2.0 點 → 時段很尖；價格取法 1 分K 上分不出來，15 分K 上 hlc3 明顯最好。
    全寬＝0.0735 × 前 11 個 CME 交易日（不含當天）真實波幅的平均；23 組全寬差中位 0.16 點、最大 0.92 點（全寬約 29~41 點）；
         假日那一盤（勞動節、陣亡將士紀念日）算進下一個交易日（CME 的交易日期），那兩天的兩組全寬一樣（圖上 38.67／38.67）。
         N＝11 跟台指版一樣；係數不一樣（台指 0.0614）。
    放大日（×125/92）照台指版的條件算 —— ★NQ 圖上 23 組剛好都不是放大日，這條**沒驗到，是我定的**。
    生效＝紐約 16:00（美股收盤）那根起、到下一個交易日 16:00（圖上 23 組的換組點都在 15:45／16:00 那兩根之間；假日沒有 16:00 那根 → 下一盤開盤）。
★缺口框：規則照台指版 mmtx.sms_gaps（> 0.65 個階梯全寬才追、最多追 5 個、影線補滿就消失、框右端＝收盤離缺口 ≤ 8.2 個全寬的最後一根）。
  NQ 幾乎 23 小時連續交易，「換盤」**我定成**每天 17:00~18:00（紐約）休市後重新開盤那根（含週日開盤）；沒有官方答案可對。
"""
import os, json, gzip, time, bisect, calendar, traceback, datetime as dt
import numpy as np
import pandas as pd
import requests

YH = ("https://query1.finance.yahoo.com", "https://query2.finance.yahoo.com")
UA = {"User-Agent": "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/126.0 Safari/537.36",
      "Accept": "application/json"}
_HERE = os.path.dirname(os.path.abspath(__file__))
_PERSIST = os.environ.get("PERSIST_DIR") or ("/data" if os.path.isdir("/data") else _HERE)
F5 = os.path.join(_PERSIST, "mmnq_5m.json.gz")
F60 = os.path.join(_PERSIST, "mmnq_60m.json.gz")
FD = os.path.join(_PERSIST, "mmnq_daily.json.gz")
F1 = os.path.join(_PERSIST, "mmnq_1m.json.gz")
KEEP5_DAYS = 120
KEEP1_DAYS = 120
ONE_WIN = (570, 960)              # 1 分K 只存紐約 09:30~16:00（中心、階梯只用這一段；Yahoo 1 分K 只回得到最近 30 天）
KEEP60_DAYS = 1100
SHOW = {"15": 1500, "60": 1000, "D": 2000, "W": 1000}
TW = 8 * 3600                     # 台灣時間 − UTC

SMZ_K = 0.0735                    # 全寬係數（16 組校正的中位數；台指版 0.0614）
SMZ_N = 11
SMZ_WIDE = 125.0 / 92.0
SMZ_A, SMZ_B = 585, 945           # 中心的時段：紐約 09:45 ≤ t < 15:45（分鐘）
SMZ_EFF = 960                     # 紐約 16:00 起生效
CEN_PX = "hlc3"                   # 中心的價格取法：(高+低+收)/3（5 月那 7 組 15 分K 上 hlc3 平均差 0.11、hl2 2.48）
SMG_MIN, SMG_KEEP, SMG_NEAR = 0.65, 5, 8.2
OPEN_MIN = 1080                   # 紐約 18:00＝CME 休市後重新開盤（我定的「換盤」）
LAD_P2 = (585, 945)               # 控盤階梯參數二：紐約 09:45~15:45（＝測穿版階梯中心那段）
LAD_P1 = (570, 675)               # 控盤階梯參數一：紐約 09:30~11:15（美股開盤後 1 小時 45 分，對照台指＝加權開盤 09:00~10:45）
LAD_HALF = 9.0                    # 階梯雙線 ±9 點（照台指）
STAR_WIN = (570, 960)             # 籌碼星只算美股正規盤 09:30~16:00
STAR_VOL = 26000                  # 籌碼星門檻（台指 3000 口；NQ 取「出星頻率跟台指一樣」的口數，我定的，見 stars15）
WARM = 280                        # 同台指：每個週期最前面 280 根長均線還沒穩 → 不上色
# ★即時估價（2026-11-27，用戶：「nq沒有更即時的嗎」）：Yahoo 延遲約 10 分 → 用即時的替身（不是 NQ 本身）換算一根「正在形成」的估價K。
#   換算＝NQ ≈ 替身 × 比值；比值＝最近 60 根「Yahoo NQ=F 1 分K 收盤 ÷ 替身同一分鐘收盤」的中位數（_mm_data/nq/est_eval.py：
#   11-20~11-26 照線上做法模擬，Hyperliquid 比值法誤差中位 2.2 點、90% 6.4 點；差值法 2.2／6.5；Binance QQQ 比值 3.3／9.0、差值 49.9／160.6 → 用比值）。
EST_SRCS = ("hl", "bn")           # 主來源 Hyperliquid xyz:XYZ100，不行就退到 Binance QQQUSDT（www 網域）
EST_NAME = {"hl": "Hyperliquid 那斯達克 100 永續（xyz:XYZ100）", "bn": "Binance QQQ 永續（QQQUSDT）"}
EST_ERR = {"hl": (2.2, 6.4), "bn": (3.3, 9.0)}       # 換算誤差（點）中位／90%（校正資料是最新的時候）
EST_ERR_OLD = {"hl": ((6, (3.9, 11.5)), (24, (7.3, 13.6)))}   # 比值舊了 ≤6／≤24 小時（Yahoo 1 分K 停了，例：美國假日）；Binance 舊的沒量
EST_MAX_TRADE_AGE = 30            # 替身最新成交超過 30 秒＝這個來源過期 → 換下一個
EST_WIN = 60                      # 比值用最近 60 根配對的 1 分K
EST_JUMP = 0.004                  # 最近 5 分鐘的比值跟整段中位數差 > 0.4%（約 100 點）＝換月跳空 → 只用跳空之後的
EST_CAL_MAX_AGE = 24 * 3600       # 校正資料超過一天就不畫（舊 24 小時誤差中位 7.3、90% 13.6 點）
EST_MODE = "ratio"
COLOR_NAME = {0: "黑", 1: "金", 2: "藍", 3: "紅"}
try:
    import mmtx as _MX            # 彩色K／判色均線的公式（台指那組）；載不到就只畫 K 線
except Exception:
    _MX = None

_b5 = {}                          # UTC 秒 → [開, 高, 低, 收, 量]（5 分K，Yahoo 原始值）
_b60 = {}                         # 同上（60 分K）
_dly = {}                         # 日序（1970 起第幾天，紐約日期）→ [開, 高, 低, 收, 量, 1H 根數]（CME 交易日）
_b1 = {}                          # UTC 秒 → [開, 高, 低, 收, 量]（1 分K，只存紐約 09:30~16:00）
_y1raw = {}                       # 最近幾天 Yahoo 1 分K（全時段、不存檔）：只拿來校正即時估價的比值
_d1 = {}                          # Yahoo 日K：日序 → [開, 高, 低, 收, 量]（只拿來補週日開盤缺的 10 分鐘，見 patch_sunday；不存檔，啟動時重抓）
_state = {"ok": False, "err": "", "fetch_err": "", "last_fetch": 0, "last60": 0, "last1d": 0, "last1m": 0, "last_calc": 0, "sig": None,
          "charts": {}, "summ": None, "zones": [], "gaps": [], "zinfo": [], "save_err": "", "meta": {}}


# ───────────────────────── 時間（自己算美東夏令，不靠時區資料庫） ─────────────────────────
_DST = {}


def _dst(y):
    """美國 2007 起的夏令：3 月第二個週日 02:00（＝07:00 UTC）起、11 月第一個週日 02:00（＝06:00 UTC）止。回傳兩個 UTC 秒。"""
    if y not in _DST:
        d = dt.date(y, 3, 8); d += dt.timedelta(days=(6 - d.weekday()) % 7)
        e = dt.date(y, 11, 1); e += dt.timedelta(days=(6 - e.weekday()) % 7)
        _DST[y] = (calendar.timegm((d.year, d.month, d.day, 7, 0, 0)), calendar.timegm((e.year, e.month, e.day, 6, 0, 0)))
    return _DST[y]


def et_off(t):
    """UTC 秒（可以是陣列）→ 紐約跟 UTC 差幾秒（夏令 −4h、冬令 −5h）。"""
    t = np.asarray(t, dtype="int64")
    yrs = t.astype("datetime64[s]").astype("datetime64[Y]").astype("int64") + 1970
    out = np.full(t.shape, -5 * 3600, dtype="int64")
    for y in np.unique(yrs):
        s, e = _dst(int(y))
        out[(yrs == y) & (t >= s) & (t < e)] = -4 * 3600
    return out


def et_parts(t):
    """UTC 秒陣列 → (紐約當地秒, 當天第幾分鐘, 日序, 星期（一＝0）)。"""
    t = np.asarray(t, dtype="int64")
    loc = t + et_off(t)
    day = loc // 86400
    return loc, (loc % 86400) // 60, day, (day + 3) % 7


def et_to_utc(day, mins):
    """紐約某一天（日序）的第幾分鐘 → UTC 秒（16:00、18:00 不會落在夏令切換那一小時）。"""
    loc = int(day) * 86400 + int(mins) * 60
    return loc - int(et_off(np.array([loc + 5 * 3600]))[0])


def _fmt(t, off):
    return time.strftime("%m-%d %H:%M", time.gmtime(int(t) + int(off)))


def globex_open(now=None):
    """現在 CME 有沒有在交易（週日 18:00 ~ 週五 17:00，每天 17:00~18:00 休息；不管假日）。"""
    t = int(now if now is not None else time.time())
    _, mins, _, wd = et_parts(np.array([t]))
    mins, wd = int(mins[0]), int(wd[0])
    if wd == 5:
        return False
    if wd == 6:
        return mins >= OPEN_MIN
    if wd == 4 and mins >= 1020:
        return False
    return not (1020 <= mins < OPEN_MIN)


# ───────────────────────── 資料 ─────────────────────────
def _yget(interval, rng=None, p1=None, p2=None):
    """Yahoo chart API → ({UTC 秒: [開高低收量]}, 錯誤字串)。兩個主機輪流試；抓不到回 (None, 原因)。rng 或 (p1, p2)＝起訖 UTC 秒。"""
    step = 300 if interval == "5m" else 60          # 5 分K 對齊到 5 分（盤中最後一根的時間偶爾沒對齊）；60 分K／日K 只對齊到分鐘（半天盤的 1H K 在 :30）
    last = ""
    for host in YH:
        try:
            prm = {"interval": interval, "includePrePost": "true"}
            prm.update({"range": rng} if rng else {"period1": int(p1), "period2": int(p2)})
            r = requests.get(host + "/v8/finance/chart/NQ%3DF", params=prm, headers=UA, timeout=25)
            if r.status_code != 200:
                last = f"HTTP {r.status_code}"; continue
            res = ((r.json() or {}).get("chart") or {}).get("result") or []
            if not res:
                last = "回應裡沒有 K 線"; continue
            res = res[0]; ts = res.get("timestamp") or []
            q = ((res.get("indicators") or {}).get("quote") or [{}])[0]
            cols = [q.get(k) or [] for k in ("open", "high", "low", "close", "volume")]
            out = {}
            for i, t in enumerate(ts):
                try:
                    o, h, l, c = (float(cols[j][i]) for j in range(4))
                    v = float(cols[4][i] or 0)
                except (TypeError, ValueError, IndexError):
                    continue
                if not (o == o and h == h and l == l and c == c) or c <= 0:
                    continue
                k = int(t) // step * step
                if k in out:
                    p = out[k]; out[k] = [p[0], max(p[1], h), min(p[2], l), c, p[4] + v]
                else:
                    out[k] = [o, h, l, c, v]
            if not out:
                last = "回應裡沒有 K 線"; continue
            m = res.get("meta") or {}
            _state["meta"] = {k: m.get(k) for k in ("symbol", "exchangeName", "regularMarketTime", "exchangeDataDelayedBy")}
            return out, ""
        except Exception as e:
            last = type(e).__name__
    return None, last


def _rth1(b):
    """只留紐約 09:30~16:00（ONE_WIN）的 1 分K。"""
    if not b:
        return {}
    ks = np.array(sorted(b), dtype="int64"); _, mins, _, _ = et_parts(ks)
    return {int(k): b[int(k)] for k, m in zip(ks, mins) if ONE_WIN[0] <= m < ONE_WIN[1]}


def update_data():
    bad = []; n = 0
    b, err = _yget("5m", "60d" if len(_b5) < 3000 else "2d")
    if b is None:
        bad.append("Yahoo 5 分K 抓不到（" + err + "）")
    else:
        _b5.update(b); n += len(b); _state["last_fetch"] = time.time()
    if len(_b60) < 3000 or time.time() - _state["last60"] > 900:
        b, err = _yget("60m", "730d" if len(_b60) < 3000 else "5d")
        if b is None:
            bad.append("Yahoo 60 分K 抓不到（" + err + "）")
        else:
            _b60.update(b); _state["last60"] = time.time()
    # 1 分K：第一次往回補 4 段 × 7 天（Yahoo 1 分K 只給最近 30 天、一次最多 8 天），之後每輪抓今天；只存紐約 09:30~16:00
    if len(_b1) < 2000:
        now = int(time.time()); got = 0
        for k in range(4):
            b, err = _yget("1m", p1=now - (k + 1) * 7 * 86400, p2=now - k * 7 * 86400)
            if b:
                _b1.update(_rth1(b)); got += 1
                if k == 0: _y1raw.update(b)
        if not got:
            bad.append("Yahoo 1 分K 抓不到（" + err + "）")
        else:
            _state["last1m"] = time.time()
    else:
        b, err = _yget("1m", "5d" if not _y1raw else "1d")
        if b is None:
            bad.append("Yahoo 1 分K 抓不到（" + err + "）")
        else:
            _b1.update(_rth1(b)); _y1raw.update(b); _state["last1m"] = time.time()
    if not _d1 or time.time() - _state["last1d"] > 1800:
        b, err = _yget("1d", "10y" if not _d1 else "1mo")
        if b is None:
            bad.append("Yahoo 日K 抓不到（" + err + "）")
        else:
            ks = np.array(sorted(b), dtype="int64"); _, _, dd_, _ = et_parts(ks)
            _d1.update({int(d): b[int(k)] for k, d in zip(ks, dd_)}); _state["last1d"] = time.time()
    _state["fetch_err"] = "；".join(bad)           # 抓不到要顯示在網頁上，不可以默默拿舊資料當新的
    return n


def _trim():
    now = int(time.time()); fut = now + 86400
    for st, keep in ((_b5, KEEP5_DAYS), (_b60, KEEP60_DAYS), (_b1, KEEP1_DAYS)):
        cut = now - keep * 86400
        for k in [k for k in st if k < cut or k > fut]:
            st.pop(k, None)
    for k in [k for k in _y1raw if k < now - 3 * 86400 or k > fut]:
        _y1raw.pop(k, None)
    fd = (now + 2 * 86400) // 86400
    for k in [k for k in _dly if k > fd]:
        _dly.pop(k, None)


def _dump(obj, path):
    tmp = path + ".tmp"
    with gzip.open(tmp, "wt", encoding="utf-8") as fh:
        json.dump({str(k): v for k, v in obj.items()}, fh)
    os.replace(tmp, path)


def _save():
    try:
        _trim()
        _dump(_b5, F5); _dump(_b60, F60); _dump(_dly, FD); _dump(_b1, F1)
        _state["save_err"] = ""
    except Exception as e:
        _state["save_err"] = type(e).__name__


def _load():
    bad = []
    for f, st in ((F5, _b5), (F60, _b60), (FD, _dly), (F1, _b1)):
        try:
            if os.path.exists(f):
                with gzip.open(f, "rt", encoding="utf-8") as fh:
                    st.update({int(k): v for k, v in json.load(fh).items()})
        except Exception:
            bad.append(os.path.basename(f))
    _state["loaded"] = {"5m": len(_b5), "60m": len(_b60), "d": len(_dly), "1m": len(_b1), "dir": _PERSIST}     # 啟動時從存檔讀回幾根（驗收「redeploy 後歷史還在」）
    if bad:
        _state["err"] = "讀檔失敗 " + "、".join(bad)


# ───────────────────────── 整理 K 線 ─────────────────────────
def _frame(st):
    if not st:
        return pd.DataFrame(columns=list("ohlcv"), dtype=float)
    ks = np.array(sorted(st), dtype="int64")
    return pd.DataFrame(np.array([st[k][:5] for k in ks], dtype=float), index=ks, columns=list("ohlcv"))


def clean5(f):
    """Yahoo 5 分K 的兩種假價（2026-11-26 跟 TV NQ1! 逐根比出來的；規則是我定的）：
    ①開盤價離群：這根的開盤剛好是最高或最低、離前一根收盤很遠（> 10 倍近 48 根高低差中位、且 > 0.5%），收盤又回到前一根收盤附近
      （10-15 03:35 開在 +352 點、10-30 15:00 開在 +1,240 點，TV 都沒有）→ 開盤改成前一根收盤，被它撐出來的那個極值改成 max／min(開, 收)。
    ②週五 16:55（週末休市前最後一根）收盤混進一個假價、而且剛好是最高或最低（09-18~11-20 八個週五有六個這樣，差 13~165 點；週一~週四 0 個）
      → 收盤改成開盤，被它撐出來的那個極值改成 max／min(開, 收)。
    回傳 (修過的 frame, 修了幾根)。"""
    if len(f) < 3:
        return f, 0
    t = f.index.values.astype("int64")
    o, h, l, c = (f[k].values.astype(float).copy() for k in "ohlc")
    pc = np.r_[np.nan, c[:-1]]
    cont = np.r_[False, np.diff(t) <= 900]            # 前一根在 15 分鐘內（10-30 那根前面 14:55 剛好缺一根）
    rng = pd.Series(h - l).rolling(48, min_periods=12).median().shift(1).values
    thr = np.maximum(10 * np.nan_to_num(rng, nan=0.0), 0.005 * c)
    jump = np.abs(o - pc)
    A = cont & (jump > thr) & ((o == h) | (o == l)) & (np.abs(c - pc) < jump / 3)
    _, mins, _, wd = et_parts(t)
    B = (wd == 4) & (mins == 1015) & ((c == h) | (c == l)) & (c != o)
    for i in np.where(A)[0]:
        wh, wl = o[i] == h[i], o[i] == l[i]; o[i] = pc[i]
        if wh: h[i] = max(o[i], c[i])
        if wl: l[i] = min(o[i], c[i])
        h[i] = max(h[i], o[i]); l[i] = min(l[i], o[i])   # 前一根收盤可能在這根高低之外（10-30 15:00：前收 28234.25 < 這根低 28283.25）→ 高低要把新開盤包進去
    for i in np.where(B)[0]:
        wh, wl = c[i] == h[i], c[i] == l[i]; c[i] = o[i]
        if wh: h[i] = max(o[i], c[i])
        if wl: l[i] = min(o[i], c[i])
    g = f.copy(); g["o"], g["h"], g["l"], g["c"] = o, h, l, c
    return g, int(A.sum() + B.sum())


def patch_sunday(f5, d1):
    """Yahoo 週日 18:00 開盤的前 10 分鐘沒有分K（11-01~11-22 四個週日 1 分K 第一根都是 18:10；平日 18:00 都在）→ 開盤缺口會畫錯、
    週一那天的高低也會少（10-18 週日開盤那一段低了 275 點，拖累之後 11 天的階梯寬度）。用 Yahoo 日K（開高低是整盤的）補一根 18:00 的 5 分K（我定的）：
      開＝日K 開盤（日K 開盤＝TV 18:00 那根開盤 37/38 天）；日K 的高／低比這一盤所有分K 更極端 → 多出來的那段算在這根；收＝下一根開盤；量＝0（不知道）。
      不補：日K 收盤跟分K 收盤差 > 0.2%（季度換月那幾天日K 跟分K 可能是不同合約）、或這一盤中間還有超過 15 分鐘的洞（多出來的極值不一定在開盤）。
    d1＝{日序: [開, 高, 低, 收, 量]}（Yahoo 日K，日序＝紐約日期＝CME 交易日）。回傳 (frame, 補了幾根)。"""
    if not len(f5) or not d1:
        return f5, 0
    t = f5.index.values.astype("int64")
    _, mins, day, wd = et_parts(t)
    gd = day + (mins >= OPEN_MIN)
    H, L, C, O = (f5[k].values for k in ("h", "l", "c", "o"))
    add = []
    for D in np.unique(gd):
        ix = np.where(gd == D)[0]
        i0 = ix[0]
        if wd[i0] != 6 or not (OPEN_MIN < mins[i0] <= OPEN_MIN + 30):
            continue
        q = d1.get(int(D))
        if not q or len(ix) < 12 or (len(ix) > 1 and np.diff(t[ix]).max() > 900):
            continue
        do, dh, dl, dc = (float(x) for x in q[:4])
        if not (do > 0 and abs(dc - C[ix[-1]]) <= 0.002 * dc):
            continue
        hi, lo = float(H[ix].max()), float(L[ix].min()); nx = float(O[i0])
        add.append((et_to_utc(int(D) - 1, OPEN_MIN), [do, dh if dh > hi + 1e-9 else max(do, nx), dl if dl < lo - 1e-9 else min(do, nx), nx, 0.0]))
    if not add:
        return f5, 0
    g = pd.concat([f5, pd.DataFrame([a[1] for a in add], index=np.array([a[0] for a in add], dtype="int64"), columns=list("ohlcv"))])
    g = g[~g.index.duplicated(keep="first")].sort_index()
    return g, len(add)


def _agg(f, key):
    g = f.groupby(key)
    return pd.DataFrame({"o": g.o.first(), "h": g.h.max(), "l": g.l.min(), "c": g.c.last(), "v": g.v.sum()})


def bars15(f5):
    return _agg(f5, f5.index.values // 900 * 900) if len(f5) else f5


def bars60(f5, f60):
    """1H K：5 分K 蓋到的那段用 5 分K 組（跟 15 分同一份、修過假價），更早的用 Yahoo 60 分K。"""
    if not len(f5):
        return f60
    h0 = (int(f5.index[0]) + 3599) // 3600 * 3600
    new = _agg(f5, f5.index.values // 3600 * 3600)
    return pd.concat([f60[f60.index < h0], new[new.index >= h0]]).sort_index()


def globex_days(f1h):
    """1H K → CME 交易日（紐約 18:00 起算隔天；週日晚上算週一）。index＝日序，多一欄 n＝幾根 1H K。
    Yahoo 不給假日那一盤 → 那天直接沒有；只剩前一晚那幾根、當天沒有任何美股正規盤時段 K 的「假日盤」（例：感恩節前一晚 18:00~23:00）
    併進下一個交易日（我定的；勞動節那一盤 TV 圖上的寬度：併進隔天、整盤丟掉都是最大差 0.87 點，自己算一根最差 2.6 點）。
    黑色星期五那種半天盤有正規盤的 K → 照算一天。最後一天（還在交易）不併。"""
    if not len(f1h):
        return pd.DataFrame(columns=["o", "h", "l", "c", "v", "n"], dtype=float)
    t = f1h.index.values.astype("int64")
    _, mins, day, _ = et_parts(t)
    gd = day + (mins >= OPEN_MIN)
    keep = ((gd + 3) % 7) < 5
    f1h = f1h[keep]; gd = gd[keep]; mins = mins[keep]; day = day[keep]
    ks = np.unique(gd)
    rth = {int(k) for k in np.unique(gd[(day == gd) & (mins >= 540) & (mins < 960)])}     # 這一盤有沒有 09:00~16:00 起點的 1H K
    mp = {int(k): int(k) for k in ks}
    for i in range(len(ks) - 2, -1, -1):
        if int(ks[i]) not in rth:
            mp[int(ks[i])] = mp[int(ks[i + 1])]
    g2 = np.array([mp[int(k)] for k in gd], dtype="int64")
    d = _agg(f1h, g2)
    d["n"] = pd.Series(1, index=g2).groupby(level=0).sum()
    return d


def _merge_daily(dn, d1=None):
    """把 1H 組出來的交易日寫進長期日K：1H 蓋到的範圍以新的為準（短日被併掉的舊紀錄要刪），第一天可能不完整 → 已經有就不覆蓋。
    1H 蓋不到的更早日子用 Yahoo 日K（d1，10 年）補（彩色K 日K 的長均線要暖機；Yahoo 日K 的收盤在週五／換月那幾天跟分K 不一樣，只當舊歷史用）。"""
    if not len(dn):
        return
    for d, r in (d1 or {}).items():
        if d < int(dn.index[0]) and d not in _dly and ((d + 3) % 7) < 5:
            _dly[int(d)] = [float(r[0]), float(r[1]), float(r[2]), float(r[3]), float(r[4]), 0]
    first, last = int(dn.index[0]), int(dn.index[-1])
    have = set(int(x) for x in dn.index)
    for k in [k for k in _dly if first < k <= last and k not in have]:
        _dly.pop(k, None)
    for d, r in dn.iterrows():
        d = int(d)
        if d == first and d in _dly:
            continue
        _dly[d] = [float(r.o), float(r.h), float(r.l), float(r.c), float(r.v), int(r.n)]


def daily_df():
    if not _dly:
        return pd.DataFrame(columns=["o", "h", "l", "c", "v"], dtype=float)
    ks = np.array(sorted(_dly), dtype="int64")
    return pd.DataFrame(np.array([_dly[k][:5] for k in ks], dtype=float), index=ks, columns=["o", "h", "l", "c", "v"])


def to_week(dd):
    if not len(dd):
        return dd
    ix = dd.index.values.astype("int64")
    return _agg(dd, ix - (ix + 3) % 7)


# ───────────────────────── 指標 ─────────────────────────
def sms_width(dd, k=None, n=None):
    """全寬（每個交易日一個值，index 同 dd）＝k × 前 n 根已收完日K 的 TR 平均 ×（放大日 ? 125/92 : 1）。
    算法跟 mmtx.sms_width 一模一樣（_chk_nq.py 拿台指日K 逐日對過），只是係數／根數可以給、而且不吃台指的 TV 日K 覆寫。
    回傳 (全寬 Series, 放大日 Series)。"""
    k = SMZ_K if k is None else k
    n = SMZ_N if n is None else n
    pc = dd.c.shift(1)
    tr = np.maximum(dd.h - dd.l, np.maximum((dd.h - pc).abs(), (dd.l - pc).abs()))
    tr[pc.isna()] = (dd.h - dd.l)[pc.isna()]
    ch = dd.c - pc
    a6 = 3 * tr.shift(1) + 2 * tr.shift(2) + tr.shift(3)
    b3 = tr.shift(4) + tr.shift(5) + tr.shift(6)
    m6 = (3 * ch.shift(1) + 2 * ch.shift(2) + ch.shift(3)).abs()
    wide = (10 * a6 >= 26 * b3) & (2 * m6 >= a6)
    return k * tr.rolling(n).mean().shift(1) * np.where(wide, SMZ_WIDE, 1.0), wide


def centers(f5, a, b, f1=None):
    """每個紐約日期 [a, b) 分鐘的 Σ((高+低+收)/3×量)/Σ量 → {日序: (值, "1m"／"15m")}。
    那天有完整的 1 分K（≥ 那段 15 分K 根數×15 的 9 成）就用 1 分K，不然退回 15 分K（由 5 分K 組）——
    他自己也是這樣：截圖前 1~1.5 個月的 16 組對 TV 1 分K 平均差 0.22 點，5 個月前的 7 組對 TV 15 分K 平均差 0.11 點（同台指版）。"""
    def agg(fr):
        t = fr.index.values.astype("int64"); _, mins, day, wd = et_parts(t)
        v = fr.v.values.astype(float); m = (mins >= a) & (mins < b) & (wd < 5) & (v > 0)
        px = (fr.h.values + fr.l.values + fr.c.values) / 3 if CEN_PX == "hlc3" else (fr.h.values + fr.l.values) / 2
        return pd.DataFrame({"d": day[m], "pv": px[m] * v[m], "v": v[m], "n": 1}).groupby("d").sum()
    out = {}
    if f5 is None or not len(f5):
        return out
    g15 = agg(bars15(f5)); g1 = agg(f1) if f1 is not None and len(f1) else None
    for d, r in g15.iterrows():
        d = int(d)
        if g1 is not None and d in g1.index and g1.n[d] >= 0.9 * r.n * 15 and g1.v[d] > 0:
            out[d] = (float(g1.pv[d] / g1.v[d]), "1m")
        elif r.v > 0:
            out[d] = (float(r.pv / r.v), "15m")
    return out


def sms_zone(f5, dd, f1=None):
    """測穿版階梯帶 → (zones [[生效秒（台灣當 UTC）, 下緣, 上緣], …], info [{d, c, w, wide, eff, src}, …])，照時間排。
    當天的時段還沒走完（還沒有 15:45 以後的 K）就不出那一組。f1＝1 分K（有就用，見 centers）。"""
    try:
        if f5 is None or not len(f5) or dd is None or len(dd) < SMZ_N + 1:
            return [], []
        t = f5.index.values.astype("int64")
        cen = centers(f5, SMZ_A, SMZ_B, f1)
        if not cen:
            return [], []
        wid, wide = sms_width(dd)
        last_t = int(t[-1])
        out, info = [], []
        for d, (c, src) in sorted(cen.items()):
            d = int(d)
            if last_t < et_to_utc(d, SMZ_B):
                continue
            if d not in wid.index:
                continue
            w = wid.loc[d]
            if w != w or c != c:
                continue
            eff = et_to_utc(d, SMZ_EFF) + TW
            hw = float(w) / 2
            out.append([int(eff), round(float(c) - hw, 2), round(float(c) + hw, 2)])
            info.append({"d": d, "c": round(float(c), 2), "w": round(float(w), 2), "wide": bool(wide.loc[d]), "eff": int(eff), "src": src})
        return out, info
    except Exception:
        traceback.print_exc()
        return [], []


def sms_gaps_core(ts, H, L, C, opn, zones, bar=900):
    """缺口框的核心（＝mmtx.sms_gaps 的迴圈，換盤判定改成外面給的布林陣列 opn；_chk_nq.py 拿台指 15 分K 逐框對過一樣）。
    ts＝台灣當 UTC 的秒。回傳 [[左端秒, 右端秒, 下緣, 上緣, 還在追], …]（照左端排）。"""
    try:
        n = len(ts)
        if n < 3 or not zones:
            return []
        ts = np.asarray(ts, dtype="int64")
        H = np.asarray(H, dtype=float); L = np.asarray(L, dtype=float); C = np.asarray(C, dtype=float)
        zt = np.array([z[0] for z in zones], dtype="int64"); zw = np.array([z[2] - z[1] for z in zones], dtype=float)
        kz = np.searchsorted(zt, ts, side="right") - 1
        arr = []; frozen = []
        for k in range(1, n):
            if kz[k] < 0:
                continue
            w = zw[kz[k]]; keep = []
            for g in arr:                                   # g＝[產生根, 下緣, 上緣, 向上缺口?, 右端根]
                if k > g[0] and ((L[k] <= g[1]) if g[3] else (H[k] >= g[2])):
                    continue                                # 影線碰到遠邊＝補滿
                if g[1] <= C[k] <= g[2] or min(abs(C[k] - g[1]), abs(C[k] - g[2])) <= SMG_NEAR * w:
                    g[4] = k
                keep.append(g)
            arr = keep
            if opn[k] and ts[k] - ts[k - 1] > bar and (L[k] > H[k - 1] or H[k] < L[k - 1]):
                up = bool(L[k] > H[k - 1]); lo, hi = (H[k - 1], L[k]) if up else (H[k], L[k - 1])
                if (hi - lo) > SMG_MIN * w:
                    if len(arr) >= SMG_KEEP:
                        frozen.append(arr.pop(0))
                    arr.append([k, lo, hi, up, k])
        out = [[int(ts[g[0] - 1]), int(ts[g[4]]), round(float(g[1]), 2), round(float(g[2]), 2), 1] for g in arr]
        out += [[int(ts[g[0] - 1]), int(ts[g[4]]), round(float(g[1]), 2), round(float(g[2]), 2), 0] for g in frozen]
        return sorted(out)
    except Exception:
        traceback.print_exc()
        return []


def sms_gaps(d15, zones):
    """NQ 的缺口框：換盤＝紐約 18:00 那根（CME 每天 17:00~18:00 休市後重新開盤，含週日開盤）而且前一根隔超過 15 分鐘（我定的）。"""
    if d15 is None or len(d15) < 3:
        return []
    t = d15.index.values.astype("int64")
    _, mins, _, _ = et_parts(t)
    return sms_gaps_core(t + TW, d15.h.values, d15.l.values, d15.c.values, mins == OPEN_MIN, zones)


def ladders(f5, f1=None):
    """控盤階梯（麥門台指軟體的參數一／二；台指＝當天日盤口數加權均價，P2 08:47~13:25、P1 09:00~10:45，13:30 生效，mmtx.ladders）套到 NQ。
    ★我定的（NQ 沒有官方答案）：「日盤」＝美股正規盤。參數二＝紐約 09:45~15:45（＝測穿版階梯中心，同一個算法所以同值）、
      參數一＝紐約 09:30~11:15（美股開盤後 1 小時 45 分，對照台指＝加權開盤 09:00~10:45；同幣圖的參數一）；
      價格＝(高+低+收)/3×量、有 1 分K 用 1 分K（同測穿版中心，見 centers），紐約 16:00 生效。那段還沒走完就不出。
    回傳 (參數二, 參數一)，各為 [(生效 UTC 秒, 值), …]。"""
    out = []
    if f5 is None or not len(f5):
        return [], []
    last_t = int(f5.index.values[-1])
    for a, b in (LAD_P2, LAD_P1):
        out.append([(et_to_utc(d, SMZ_EFF), v) for d, (v, _) in sorted(centers(f5, a, b, f1).items()) if last_t >= et_to_utc(d, b)])
    return out[0], out[1]


def lad_at(ev, t):
    """每根 K（UTC 秒陣列）當時生效的階梯值（最後一個 生效 ≤ 這根起點 的；之前沒有＝None）。"""
    if not ev:
        return [None] * len(t)
    et = np.array([e[0] for e in ev], dtype="int64"); val = [e[1] for e in ev]
    k = np.searchsorted(et, np.asarray(t, dtype="int64"), side="right") - 1
    return [None if j < 0 else round(float(val[j]), 2) for j in k]


def stars15(f5):
    """籌碼異常星（台指＝這根 15 分K 裡最大的 5 分K 量 ≥3000 口、只算台股盤中，mmtx.stars15）套到 NQ。回傳 {15 分K 起點 UTC 秒: 那根最大的 5 分K 量}（只回 ≥ 門檻的）。
    ★我定的：只算美股正規盤 09:30~16:00（紐約）的 5 分K；門檻 STAR_VOL＝26,000 口＝讓 NQ 出星的頻率跟台指一樣
      （台指 58 個日盤 18 顆＝每根 15 分K 1.72%、每天 0.31 顆；NQ 50 個正規盤用 26,000 口是 1.78%、每天 0.46 顆。台指樣本只有 18 顆，只能當參考）。
      3000 口直接套 → NQ 每天 23.5 顆（幾乎每根）。台指 09:00 那一分鐘不算，NQ 的 5 分K 切不出來 → 09:30 那根照算。"""
    if f5 is None or not len(f5):
        return {}
    t = f5.index.values.astype("int64")
    _, mins, _, wd = et_parts(t)
    m = (mins >= STAR_WIN[0]) & (mins < STAR_WIN[1]) & (wd < 5)
    if not m.any():
        return {}
    big = pd.Series(f5.v.values[m].astype(float)).groupby((t[m] // 900) * 900).max()
    return {int(k): float(v) for k, v in big.items() if v >= STAR_VOL}


def colors(d15, h1, dd):
    """彩色K：台指的公式直接套（mmtx.colors15_v3／colors15_p2／colors60_v3／colors_daily／colors_daily_p2，inst="TX"），參數沒校正（我定的）。
    回傳 {"15": (參數一, 參數二), "60": (參數一, None), "D": (參數一, 參數二)}；mmtx 載不到回 {}。"""
    if _MX is None:
        return {}
    out = {}
    try:
        out["15"] = (_MX.colors15_v3(d15, "TX"), _MX.colors15_p2(d15, "TX"))
        out["60"] = (_MX.colors60_v3(h1, "TX"), None)
        out["D"] = (np.asarray(_MX.colors_daily(dd, "TX"), float), np.asarray(_MX.colors_daily_p2(dd, "TX"), float))
    except Exception:
        traceback.print_exc()
    return out


def ma(df, tf):
    """判顏色用的均線（同台指那組 mmtx.MA_SPEC）→ (參數一那組, 參數二那組)，沒有給 None。"""
    if _MX is None or not len(df):
        return (None, None)
    return (_MX.ma_lines(df, "TX", tf, 1), _MX.ma_lines(df, "TX", tf, 2))


def ma_names(tf):
    return _MX.ma_names("TX", tf) if _MX is not None else {}


# ───────────────────────── 即時估價 ─────────────────────────
def _post(url, body, timeout=10):
    r = requests.post(url, json=body, headers=UA, timeout=timeout)
    r.raise_for_status()
    return r.json()


def _proxy(src, cal_end):
    """替身的 (最新成交 UTC 秒, 最新成交價, {1 分K 起點 UTC 秒: [開, 高, 低, 收]})。1 分K 要蓋到兩段：
    校正用的「Yahoo 最後一根往前 75 分鐘」（美國假日 Yahoo 停很久時離現在可能十幾小時）、估價K 用的最近 4 小時。抓不到就拋例外。"""
    now = int(time.time()); a = int(min(cal_end, now) - 75 * 60)
    if src == "hl":
        tr = _post("https://api.hyperliquid.xyz/info", {"type": "recentTrades", "coin": "xyz:XYZ100"})            # 只取時間、價格（錢包地址不留）
        t = max(tr, key=lambda x: x["time"])
        cs = _post("https://api.hyperliquid.xyz/info", {"type": "candleSnapshot", "req": {"coin": "xyz:XYZ100", "interval": "1m", "startTime": max(a, now - 26 * 3600) * 1000, "endTime": now * 1000}})
        return int(t["time"]) / 1000, float(t["px"]), {int(x["t"]) // 1000: [float(x["o"]), float(x["h"]), float(x["l"]), float(x["c"])] for x in cs}
    r = requests.get("https://www.binance.com/fapi/v1/trades", params={"symbol": "QQQUSDT", "limit": 1}, headers=UA, timeout=10); r.raise_for_status(); t = r.json()[-1]
    pc = {}
    for prm in ({"startTime": a * 1000, "limit": 90}, {"limit": 240}):
        r = requests.get("https://www.binance.com/fapi/v1/klines", params=dict({"symbol": "QQQUSDT", "interval": "1m"}, **prm), headers=UA, timeout=10); r.raise_for_status()
        pc.update({int(x[0]) // 1000: [float(x[1]), float(x[2]), float(x[3]), float(x[4])] for x in r.json()})
    return int(t["time"]) / 1000, float(t["price"]), pc


def est_calib(y1, pc):
    """比值（或差值，EST_MODE）＝最近 EST_WIN 根配對 1 分K 的中位數；Yahoo 最後一根（還在走）不算。
    換月跳空：最近 5 根的中位跟整段中位差 > EST_JUMP → 只用跳空之後、跟最近 5 根接近的那一段。回傳 (係數, 最後配對那根 UTC 秒, 用了幾根) 或 (None, None, 0)。"""
    ks = sorted(k for k in y1 if k in pc)
    if y1 and ks and ks[-1] == max(y1):
        ks = ks[:-1]
    ks = ks[-EST_WIN:]
    if len(ks) < 10:
        return None, None, len(ks)
    f = np.array([(y1[k][3] / pc[k][3]) if EST_MODE == "ratio" else (y1[k][3] - pc[k][3]) for k in ks])
    m5 = float(np.median(f[-5:])); mall = float(np.median(f)); scale = abs(m5) if EST_MODE == "ratio" else abs(y1[ks[-1]][3])
    if abs(m5 - mall) > EST_JUMP * scale:
        i = len(f)
        while i > 0 and abs(f[i - 1] - m5) <= EST_JUMP / 2 * scale:
            i -= 1
        f = f[i:]; ks = ks[i:]
    return float(np.median(f)), int(ks[-1]), len(ks)


def estimate(y1=None, last_end=None, now=None, fetch=None):
    """即時估價：主來源 → 退路，回傳 {"ok": True, 來源、換算後開高低收、成交時間…} 或 {"ok": False, "why": 原因}。
    y1＝Yahoo 1 分K（全時段）、last_end＝Yahoo 資料最後一根的結束（UTC 秒；估價K 從這裡畫到現在）。fetch 可換掉（測試用）。"""
    now = time.time() if now is None else now; y1 = _y1raw if y1 is None else y1; fetch = fetch or _proxy
    if not globex_open(now):
        return {"ok": False, "why": "CME 休市中"}
    if not y1:
        return {"ok": False, "why": "還沒有 Yahoo 1 分K 可以校正"}
    if last_end is None:
        last_end = max(y1) + 60
    why = []
    for src in EST_SRCS:
        nm = EST_NAME[src]
        try:
            lt, lpx, pc = fetch(src, max(y1))
        except Exception as e:
            why.append(f"{nm} 抓不到（{type(e).__name__}）"); continue
        if now - lt > EST_MAX_TRADE_AGE:
            why.append(f"{nm} 最新成交是 {int(now - lt)} 秒前（超過 {EST_MAX_TRADE_AGE} 秒）"); continue
        k, ct, n = est_calib(y1, pc)
        if k is None:
            why.append(f"{nm} 跟 Yahoo 重疊的 1 分K 只有 {n} 根，比值算不出來"); continue
        if now - ct > EST_CAL_MAX_AGE:
            why.append(f"{nm} 的比值是 {int((now - ct) // 3600)} 小時前校的（超過 24 小時）"); continue
        age_h = (now - ct) / 3600; err = EST_ERR[src]; note = ""
        if age_h > 1:
            olds = EST_ERR_OLD.get(src)
            if olds:
                err = next((e for h, e in olds if age_h <= h), olds[-1][1]); note = f"比值是 {age_h:.0f} 小時前校的（Yahoo 1 分K 停了），誤差變大"
            else:
                note = f"比值是 {age_h:.0f} 小時前校的，這個來源舊比值的誤差沒量過"
        cv = (lambda x: x * k) if EST_MODE == "ratio" else (lambda x: x + k)
        seg = [pc[t] for t in sorted(pc) if last_end <= t <= now]
        o = cv(seg[0][0]) if seg else cv(lpx); c = cv(lpx)
        h = max([cv(x[1]) for x in seg] + [o, c]); l = min([cv(x[2]) for x in seg] + [o, c])
        return {"ok": True, "src": src, "name": nm, "o": round(o, 2), "h": round(h, 2), "l": round(l, 2), "c": round(c, 2), "raw": lpx, "k": round(k, 6), "mode": EST_MODE,
                "cal_n": n, "cal_at": _fmt(ct, TW), "cal_age_h": round((now - ct) / 3600, 1), "at": _fmt_s(lt), "t0": int(last_end) + TW,
                "fetched": now, "err": err, "err_note": note, "fallback": src != EST_SRCS[0], "why_main": why[:1]}
    return {"ok": False, "why": "；".join(why) or "沒有可用的替身"}


def _fmt_s(t):
    return time.strftime("%H:%M:%S", time.gmtime(int(t) + TW))


# ───────────────────────── 組裝 ─────────────────────────
def _rows(df, n_show, mk=None, daily=False, col=None, col2=None, lad2=None, lad1=None, st=None, ma=None):
    """圖表列（格式同台指那頁 mmtx._rows）：[時間秒（台灣當 UTC；日K＝那天 00:00）, 開, 高, 低, 收, 量, 顏色參數一（-1＝暖機／不上色）,
      階梯參數二, 階梯參數一, 星（5 分K 量，0＝無）, 盤別記號, 期現價差（NQ 沒有＝None）, 顏色參數二（沒有＝None）, 均線參數一 [..], 均線參數二 [..]]。
    盤別記號：'o'＝美股開盤（紐約 09:30）、'c'＝換階（紐約 16:00）、0＝沒有。"""
    n = len(df); i0 = max(0, n - n_show)
    t = df.index.values.astype("int64")
    ts = t * 86400 if daily else t + TW
    o, h, l, c, v = (df[k].values for k in ("o", "h", "l", "c", "v"))
    cc = lambda a, i: int(a[i]) if (a is not None and i >= WARM and a[i] == a[i]) else -1
    mr = [None if L is None else [[None if x != x else round(float(x), 2) for x in np.asarray(a, float)[i0:]] for a in L] for L in (ma or (None, None))]
    return [[int(ts[i]), round(float(o[i]), 2), round(float(h[i]), 2), round(float(l[i]), 2), round(float(c[i]), 2),
             int(round(float(v[i]))), cc(col, i), lad2[i] if lad2 else None, lad1[i] if lad1 else None,
             int(round(st.get(int(t[i]), 0))) if st else 0, (mk[i] if mk is not None else 0), None,
             (cc(col2, i) if col2 is not None else None)] + [None if L is None else [a[i - i0] for a in L] for L in mr] for i in range(i0, n)]


def _marks(df, labels):
    if not len(df):
        return []
    _, mins, _, _ = et_parts(df.index.values.astype("int64"))
    return [labels.get(int(x), 0) for x in mins]


def compute():
    """回傳 (摘要, {週期: 圖表列})。"""
    f5raw = _frame(_b5)
    if len(f5raw) < 500:
        return {"ok": False, "err": f"NQ 5 分K 不足（{len(f5raw)} 根），等資料累積"}, {}
    f5, nfix = clean5(f5raw)
    f5, npatch = patch_sunday(f5, _d1)
    d15 = bars15(f5)
    h1 = bars60(f5, _frame(_b60))
    _merge_daily(globex_days(h1), _d1)
    dd = daily_df()
    f1 = clean5(_frame(_b1))[0] if _b1 else None
    zs, zinfo = sms_zone(f5, dd, f1)
    gp = sms_gaps(d15, zs)
    P2, P1 = ladders(f5, f1); st = stars15(f5); C = colors(d15, h1, dd)
    t15, t60 = d15.index.values.astype("int64"), h1.index.values.astype("int64")
    c15, c15b = C.get("15", (None, None)); c60 = C.get("60", (None, None))[0]; cD, cD2 = C.get("D", (None, None))
    charts = {"15": _rows(d15, SHOW["15"], _marks(d15, {570: "o", 960: "c"}), col=c15, col2=c15b, lad2=lad_at(P2, t15), lad1=lad_at(P1, t15), st=st, ma=ma(d15, "15")),
              "60": _rows(h1, SHOW["60"], _marks(h1, {570: "o", 960: "c"}), col=c60, lad2=lad_at(P2, t60), lad1=lad_at(P1, t60), ma=ma(h1, "60")),
              "D": _rows(dd, SHOW["D"], daily=True, col=cD, col2=cD2, ma=ma(dd, "D")),
              "W": _rows(to_week(dd), SHOW["W"], daily=True)}
    _state["zones"] = zs; _state["gaps"] = gp; _state["zinfo"] = zinfo
    lt = int(f5.index[-1]); now_tw = lt + TW
    act = [z for z in zinfo if z["eff"] <= now_tw]
    nxt = [z for z in zinfo if z["eff"] > now_tw]
    zf = lambda z: None if not z else {"lo": round(z["c"] - z["w"] / 2, 2), "hi": round(z["c"] + z["w"] / 2, 2), "c": z["c"], "w": z["w"],
                                         "wide": z["wide"], "src": z.get("src"), "from": time.strftime("%m-%d %H:%M", time.gmtime(z["eff"])),
                                         "day": time.strftime("%Y-%m-%d", time.gmtime(z["d"] * 86400))}
    s = {"ok": True, "err": "", "name": "NQ", "last": round(float(f5.c.iloc[-1]), 2), "last_t": lt,
         "last_bar": _fmt(lt, TW), "last_ny": _fmt(lt, int(et_off(np.array([lt]))[0])),
         "zone": zf(act[-1] if act else None), "next": zf(nxt[-1] if nxt else None), "n_gaps": sum(1 for g in gp if g[4]),
         "n5": len(f5), "n1": len(_b1), "nfix": nfix, "npatch": npatch, "days": len(dd), "first_day": time.strftime("%Y-%m-%d", time.gmtime(int(dd.index[0]) * 86400)) if len(dd) else None,
         "k": SMZ_K, "n": SMZ_N, "half": LAD_HALF, "star_vol": STAR_VOL, "warm": WARM}
    r15 = charts["15"]
    if r15:
        s["ladder2"], s["ladder1"] = r15[-1][7], r15[-1][8]
    s["stars"] = [[_fmt(k, TW), int(v)] for k, v in sorted(st.items())[-12:]]
    if c15 is not None and len(d15) > 1:
        live = bool(int(f5.index[-1]) < int(t15[-1]) + 600)            # 最後一根 15 分K 的第三根 5 分K 還沒出來＝還沒收完
        ci = len(d15) - 2 if live else len(d15) - 1
        def last_chg(cc):
            for i in range(len(cc) - 1, max(WARM, 1) - 1, -1):
                if cc[i] != cc[i - 1] and cc[i] == cc[i] and cc[i - 1] == cc[i - 1]:
                    return [_fmt(t15[i], TW), COLOR_NAME[int(cc[i - 1])], COLOR_NAME[int(cc[i])]]
            return None
        for key, cc in (("", c15), ("2", c15b)):
            if cc is None or cc[-1] != cc[-1] or cc[ci] != cc[ci]:
                continue
            s["c" + key] = int(cc[-1]); s["color" + key] = COLOR_NAME[int(cc[-1])]
            s["closed" + key] = [_fmt(t15[ci], TW), int(cc[ci])]; s["last_change" + key] = last_chg(cc)
        s["live_bar"] = live
    return s, charts


def _sig():
    if not _b5:
        return None
    k = max(_b5)
    return (k, tuple(_b5[k]), sum(1 for x in _b5 if x >= k - 2 * 86400), max(_b60) if _b60 else None, len(_d1), max(_b1) if _b1 else None)


def run_once():
    try:
        update_data()
        _trim()
        try:
            f5k = max(_b5) + 300 if _b5 else None
            e = estimate(last_end=max(max(_y1raw) + 60, f5k) if (_y1raw and f5k) else None)
        except Exception as ex:
            e = {"ok": False, "why": f"{type(ex).__name__}: {ex}"[:160]}
        e["fetched"] = time.time(); _state["est"] = e
        sig = _sig()
        if sig == _state["sig"] and _state["summ"]:
            return                          # 資料沒變 → 不重算（跟交易主迴圈共用 GIL）
        s, ch = compute()
        _state["summ"] = s
        if ch:
            _state["charts"] = ch
        if s.get("ok"):
            _state["sig"] = sig
        _save()
        _state["last_calc"] = time.time()
        _state["err"] = s.get("err", "")
        _state["ok"] = bool(s.get("ok"))
    except Exception as e:
        _state["err"] = f"{type(e).__name__}: {e}"[:200]
        traceback.print_exc()


def dash_payload():
    """/api 摘要用（不含 K 棒）。永不拋例外。"""
    try:
        s = _state.get("summ")
        if not s:
            return {"ok": False, "err": _state.get("err") or "NQ 還沒算完第一次", "fetch_err": _state.get("fetch_err") or ""}
        p = dict(s)
        now = time.time()
        p["age"] = int(now - (_state.get("last_calc") or 0))
        p["fetch_err"] = _state.get("fetch_err") or ""
        if p.get("ok"):
            lag = now - (p["last_t"] + 300)
            p["lag_min"] = int(lag // 60)
            # 盤中最後一根比現在晚超過 40 分鐘（Yahoo 延遲約 10 分＋一根 5 分）＝過期（美國假日那盤 Yahoo 不給分K，也會這樣）
            p["stale"] = bool(globex_open(now) and lag > 40 * 60)
        p["save_err"] = _state.get("save_err") or ""
        p["est"] = _state.get("est")
        p["loaded"] = _state.get("loaded")
        return p
    except Exception as e:
        return {"ok": False, "err": type(e).__name__}


def dash_chart(tf, since=None):
    """圖表 K 棒（格式同 mmtx.dash_chart）。since＝前端手上最後一根的時間秒：對得上就只回那根之後（含那根）。"""
    try:
        if tf not in SHOW:
            return {"ok": False, "err": "參數錯誤"}
        rows = (_state.get("charts") or {}).get(tf)
        if not rows:
            return {"ok": False, "err": _state.get("err") or _state.get("fetch_err") or "NQ 還沒算完第一次"}
        meta = {"ok": True, "s": "NQ", "tf": tf, "ladder": tf in ("15", "60"), "vol": True, "prec": 2, "half": LAD_HALF, "n": len(rows), "nq": True,
                "ma": ma_names(tf) if tf != "W" else {}, "star_vol": STAR_VOL, "est": _state.get("est")}
        if tf == "15":
            z = _state.get("zones") or []
            if z:
                i = max(0, bisect.bisect_right([x[0] for x in z], rows[0][0]) - 1)
                meta["smz"] = z[i:]
            g = _state.get("gaps") or []
            meta["smg"] = [x for x in g if x[1] >= rows[0][0]]
            meta["smz_info"] = {"k": SMZ_K, "n": SMZ_N, "win": "09:45~15:45", "eff": "16:00"}
        if since is not None:
            ts = [r[0] for r in rows]
            i = bisect.bisect_left(ts, since)
            if i < len(rows) and rows[i][0] == since:
                return dict(meta, bars=rows[i:], full=False)
        return dict(meta, bars=rows, full=True)
    except Exception as e:
        return {"ok": False, "err": type(e).__name__}


def loop(notify=None):
    _load()
    while True:
        run_once()
        time.sleep(120 if globex_open() else 900)
