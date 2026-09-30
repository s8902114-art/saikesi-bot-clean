# -*- coding: utf-8 -*-
"""翻倉紙上前推（2026-09-30 用戶選定；規則寫死、不准事後改——見 memory project_0930_fanpan）。

候選：數據獵手（菁英交易學院）官方「數據訊號」的做多單，且發訊當下 BTC 過去 24h 漲跌 ≤ 0%。
下單：每單風險＝資金 50%；一次只拿一筆（前一筆沒結束，後面的訊號不接）。
出場：先碰 +0.5R → 停損移到進場價；碰 TP2（官方 tp2＝1.5R）整筆全出；碰停損（官方 sl_price）全出。
帳：贏 ×(1+0.5×1.5)、保本 ×1、輸 ×0.5（各扣手續費 0.10% 往返）。
上線檢定（SPRT，H1 p=0.65 vs H0 p=0.50，α=β=0.1）：贏 +0.26、輸 −0.36；累積 ≥ +2.2 → 可上真錢；≤ −2.2 → 判不行。

★狀態不落地：每一輪都從官方 API 的歷史訊號＋OKX 15m K 線重算 → redeploy 不會丟紀錄。
★憑證：只從環境變數 DHX_COOKIE 讀，只放進 HTTP header；任何 log / Discord / 狀態都不印它。
★這支只記錄、不下單（FP_LIVE=False）。
"""
import os, json, time, datetime as dt, traceback
import requests

START_UTC_MS = int(dt.datetime(2026, 9, 30, 0, 0, tzinfo=dt.timezone.utc).timestamp() * 1000)   # 前推起點（這之前的訊號不算）
FP_LIVE = False
API = "https://datahunterx.com/api/signals"
OKX = "https://www.okx.com"
RISK, TP_R, BE_R, FEE = 0.5, 1.5, 0.5, 0.10
W_STEP, L_STEP, UP, DOWN = 0.26, -0.36, 2.2, -2.2
BAR = 900_000
INTERVAL_SEC = 600
UA = {"User-Agent": "Mozilla/5.0"}

_state = {"api": "尚未執行", "n_sig": 0, "n_long": 0, "last_run": 0, "err": "", "rows": [], "score": 0.0, "eq": 100.0,
          "decision": "繼續記錄", "W": 0, "B": 0, "L": 0}
_candles: dict = {}          # instId -> {bar_start_ms: (o, h, l, c)}
_posted: dict = {}           # 訊號 id -> 已發過的狀態
_first_run = True


# ───────────────────────── 資料 ─────────────────────────
def fetch_signals():
    """回傳 (狀態字串, 訊號 list)。只回傳解析後的訊號欄位，不保存 header。"""
    ck = os.environ.get("DHX_COOKIE", "").strip()
    if not ck:
        return "DHX_COOKIE 未設定", []
    try:
        r = requests.get(API, params={"type": "data_hunter", "limit": 1000},
                         headers={**UA, "Cookie": ck, "Referer": "https://datahunterx.com/"}, timeout=20)
    except Exception as e:
        return f"連線失敗 {type(e).__name__}", []
    if r.status_code != 200:
        return f"HTTP {r.status_code}（cookie 可能過期或無效）", []
    try:
        j = r.json()
    except Exception:
        return "回應不是 JSON（cookie 可能無效）", []
    data = j if isinstance(j, list) else (j.get("data") or j.get("signals") or [])
    return f"HTTP 200，讀到 {len(data)} 筆", data


def parse(x):
    """官方一筆 → dict(id, coin, d, t, e, sl, tp2, typ)。ts 是台北時間（UTC+8）。解析失敗回 None。"""
    try:
        c = x.get("content")
        c = json.loads(c) if isinstance(c, str) else (c or {})
        e = float(c.get("current_price") or c.get("entry") or x.get("price"))
        sl = float(c.get("sl_price") or c.get("sl") or x.get("sl"))
        d = 1 if str(x.get("direction") or c.get("direction")).lower() in ("long", "1", "做多") else -1
        risk = abs(e - sl)
        if risk <= 0: return None
        tp2 = c.get("tp2")
        tp2 = float(tp2) if tp2 not in (None, "") else e + d * TP_R * risk
        t = dt.datetime.strptime(str(x["ts"])[:19], "%Y-%m-%d %H:%M:%S") - dt.timedelta(hours=8)
        return dict(id=str(x.get("id") or f"{x.get('coin')}_{x['ts']}"), coin=str(x.get("coin")).upper(), d=d,
                    t=int(t.replace(tzinfo=dt.timezone.utc).timestamp() * 1000), e=e, sl=sl, tp2=tp2,
                    typ=c.get("signal_type") or "")
    except Exception:
        return None


def candles(inst, since_ms):
    """OKX 15m 已收盤 K（history-candles 往回翻頁；已抓過的只補新的）。回傳 (排序好的 bar 起始時間 list, dict)。"""
    have = _candles.setdefault(inst, {})
    newest = max(have) if have else None
    oldest_have = min(have) if have else None

    def page(after, stop_at):
        """從 after（不含）往更早翻頁，翻到 ≤ stop_at 為止。"""
        for _ in range(600):
            q = {"instId": inst, "bar": "15m", "limit": "100"}
            if after: q["after"] = str(after)
            try:
                j = requests.get(OKX + "/api/v5/market/history-candles", params=q, headers=UA, timeout=12).json()
            except Exception:
                time.sleep(1); continue
            if j.get("code") == "50011":                 # 限流 → 退避，不當成沒資料
                time.sleep(1.5); continue
            dd = j.get("data") or []
            if not dd: return
            for z in dd:
                if z[8] == "1": have[int(z[0])] = (float(z[1]), float(z[2]), float(z[3]), float(z[4]))
            after = int(dd[-1][0])
            if after <= stop_at: return
            time.sleep(0.12)

    # ① 補最新：從現在往回翻到已經有的最新一根（沒快取就翻到 since_ms）
    page(None, newest if newest is not None else since_ms)
    # ② 補更早：要的起點比快取最早還早 → 從快取最早那根往前翻（2026-09-30 修：原本只會補新的，亂序查詢會拿不到舊資料）
    if oldest_have is not None and since_ms < oldest_have:
        page(oldest_have, since_ms)
    return sorted(have), have


def btc24(t_ms):
    ks, h = candles("BTC-USDT-SWAP", t_ms - 2 * 86400000)
    last = [k for k in ks if k + BAR <= t_ms]                 # 發訊前最後一根已收盤
    if len(last) < 97: return None
    a, b = h[last[-1]][3], h[last[-97]][3]
    return (a / b - 1) * 100


def outcome(s):
    """回傳 (結果, 結束時間ms)。結果：贏/保本/輸/持倉中。同一根同時碰停損與目標 → 算停損（保守）。"""
    ks, h = candles(f"{s['coin']}-USDT-SWAP", s["t"])
    e, d, risk = s["e"], s["d"], abs(s["e"] - s["sl"])
    stop, tp, be, armed = s["sl"], s["tp2"], e + d * BE_R * risk, False
    for k in ks:
        if k < s["t"]: continue
        o, hi, lo, c = h[k]
        if (lo <= stop) if d > 0 else (hi >= stop): return ("保本" if armed else "輸"), k + BAR
        if (hi >= tp) if d > 0 else (lo <= tp): return "贏", k + BAR
        if not armed and ((hi >= be) if d > 0 else (lo <= be)): armed = True; stop = e
    return "持倉中", None


# ───────────────────────── 重算 ─────────────────────────
def run(raw=None, start_ms=None, notify=None):
    """raw：官方訊號 list（None＝打 API）。start_ms：前推起點（測試用）。notify：發 Discord 的函式。"""
    global _first_run
    st = start_ms or START_UTC_MS
    api, data = ("本機測試", raw) if raw is not None else fetch_signals()
    _state.update(api=api, n_sig=len(data), last_run=int(time.time()))
    S = [p for p in (parse(x) for x in data) if p and p["t"] >= st]
    L = sorted([s for s in S if s["d"] > 0], key=lambda s: s["t"]); _state["n_long"] = len(L)
    rows, busy, eq, score, W, B, Ls = [], 0, 100.0, 0.0, 0, 0, 0
    for s in L:
        b = btc24(s["t"])
        if b is None: rows.append(dict(s, btc=None, act="資料不足")); continue
        if b > 0: rows.append(dict(s, btc=b, act=f"不進（BTC24h {b:+.2f}%>0）")); continue
        if s["t"] < busy: rows.append(dict(s, btc=b, act="不進（前一筆還在持倉）")); continue
        K, te = outcome(s)
        fr = FEE / (abs(s["e"] - s["sl"]) / s["e"] * 100)
        if K == "持倉中":
            busy = 10 ** 15
        else:
            busy = te
            eq *= {"贏": 1 + RISK * TP_R - RISK * fr, "保本": 1 - RISK * fr, "輸": 1 - RISK - RISK * fr}[K]
            if K == "贏": W += 1; score += W_STEP
            elif K == "輸": Ls += 1; score += L_STEP
            else: B += 1
        rows.append(dict(s, btc=b, act="進場", K=K, eq=eq, score=score))
    dec = "✅ 可以上真錢（分數≥+2.2）" if score >= UP else ("❌ 判定不行（分數≤−2.2）" if score <= DOWN else "繼續記錄")
    prev = _state.get("decision")
    _state.update(rows=rows, eq=eq, score=score, W=W, B=B, L=Ls, decision=dec, err="")
    # ★過門檻那一刻發一則明顯的通知（redeploy 後第一輪不重發；當時的判定已寫在啟動訊息裡）
    if notify and not _first_run and dec != prev and dec != "繼續記錄":
        if dec.startswith("✅"):
            notify("🚨🚨 **翻倉：可以開始了** 🚨🚨\n"
                   f"前推紀錄 贏{W}/保本{B}/輸{Ls}，檢定分數 {score:+.2f} 已達 +2.2（事先寫死的上線門檻）。\n"
                   "規則照舊：官方做多＋BTC24h≤0｜每單風險=資金50%｜0.5R移保本｜TP2(1.5R)全出｜一次一筆。\n"
                   "要改成真單請跟 Claude 說（目前仍只記錄，不會自己下單）。")
        else:
            notify("🛑 **翻倉：判定不行** 🛑\n"
                   f"前推紀錄 贏{W}/保本{B}/輸{Ls}，檢定分數 {score:+.2f} 已達 −2.2（事先寫死的停止門檻）。\n"
                   "照規則停止，不上真錢。")
    if notify:
        for r in rows:
            key = (r["act"], r.get("K"))
            if _posted.get(r["id"]) == key: continue
            if not _first_run:
                notify(line(r))
            _posted[r["id"]] = key
        if _first_run:
            notify("📒 翻倉紙上前推 已啟動\n" + status_text())
    _first_run = False
    return rows


def line(r):
    tt = dt.datetime.fromtimestamp(r["t"] / 1000, dt.timezone.utc) + dt.timedelta(hours=8)
    base = f"📒 翻倉紀錄｜{tt:%m-%d %H:%M} {r['coin']} 官方做多（{r['typ']}） 進 {r['e']:.6g} 停損 {r['sl']:.6g} TP2 {r['tp2']:.6g}"
    if r["act"] != "進場": return base + f" → {r['act']}"
    if r["K"] == "持倉中": return base + f" → ✅進場（BTC24h {r['btc']:+.2f}%），持倉中"
    return base + f" → 結果【{r['K']}】 紙上資金 {r['eq']:,.1f}U  檢定分數 {r['score']:+.2f}（+2.2上線／−2.2停）"


def status_text():
    s = _state
    last = [r for r in s["rows"] if r["act"] == "進場"][-5:]
    tl = "\n".join("  " + line(r).replace("📒 翻倉紀錄｜", "") for r in last) or "  （還沒有進場的單）"
    tw = lambda ms: (dt.datetime.fromtimestamp(ms / 1000, dt.timezone.utc) + dt.timedelta(hours=8)).strftime("%m-%d %H:%M")
    lr = tw(s["last_run"] * 1000) if s["last_run"] else "—"
    out = [f"**翻倉紙上前推**（只記錄不下單）起點 {tw(START_UTC_MS)}（台北）",
           "規則：官方做多＋BTC24h≤0｜風險50%｜0.5R保本｜TP2(1.5R)全出｜一次一筆",
           f"官方API：{s['api']}（前推期間做多 {s['n_long']} 筆）　上次更新 {lr}",
           f"贏 {s['W']} / 保本 {s['B']} / 輸 {s['L']}　紙上資金 100U → {s['eq']:,.1f}U",
           f"檢定分數 {s['score']:+.2f}（≥+2.2 上線、≤−2.2 停）→ **{s['decision']}**",
           "最近進場：", tl]
    if s["err"]: out.append(f"⚠️ 錯誤：{s['err']}")
    return "\n".join(out)


def dash_payload():
    """給儀表板「翻倉」分頁（只讀已算好的值、不打任何 API、不含憑證）。永不拋例外。"""
    try:
        def num(v):
            try:
                f = float(v); return f if f == f and f not in (float("inf"), float("-inf")) else None
            except Exception:
                return None
        rows = []
        for r in _state.get("rows") or []:
            risk = abs(r["e"] - r["sl"])
            rows.append(dict(t=int(r["t"]), coin=r["coin"], typ=r.get("typ") or "", e=num(r["e"]), sl=num(r["sl"]),
                             be=num(r["e"] + r["d"] * BE_R * risk), tp2=num(r["tp2"]), btc=num(r.get("btc")),
                             act=r["act"], K=r.get("K"), eq=num(r.get("eq")), score=num(r.get("score"))))
        s = _state
        return dict(ok=True, start=START_UTC_MS, live=FP_LIVE, api=s.get("api"), last_run=s.get("last_run"),
                    W=s.get("W", 0), B=s.get("B", 0), L=s.get("L", 0), eq=num(s.get("eq")), score=num(s.get("score")),
                    decision=s.get("decision"), err=s.get("err") or "", up=UP, down=DOWN, w_step=W_STEP, l_step=L_STEP,
                    rows=rows[-40:])
    except Exception as e:
        return dict(ok=False, err=f"{type(e).__name__}")


def loop(notify):
    while True:
        try:
            run(notify=notify)
        except Exception as e:
            _state["err"] = f"{type(e).__name__}: {str(e)[:120]}"
            print("[翻倉前推] 例外", traceback.format_exc()[-400:], flush=True)
        time.sleep(INTERVAL_SEC)
