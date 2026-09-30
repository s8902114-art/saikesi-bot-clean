# -*- coding: utf-8 -*-
"""儀表板手機推播（Web Push，2026-09-30 用戶：「每頁都可以選擇開或不開，要像數據獵手手機也可以通知」）。

- 金鑰（VAPID）：第一次啟動自己產生，存在持久磁碟（Railway volume /data）→ 不經手、不進原始碼、redeploy 不變。
- 訂閱：每支手機一筆（endpoint 為鍵），各自存「哪些分頁要通知」；存在同一個持久目錄。
- 事件：背景每 60 秒呼叫一次 dashboard.collect(G) 拿到跟網頁「一模一樣」的資料，跟上一輪比對有沒有新東西。
  第一輪只記基準、不發（否則每次 redeploy 會把現有的東西全部當新的發一次）。
- 永不拋例外：推播壞掉不可能影響交易或網頁。
"""
import os, json, time, threading, base64, traceback

TOPICS = {  # 分頁代號 → (名稱, 什麼時候通知)
    "fp": ("翻倉", "有可以下的單、出結果、過 +2.2／−2.2"),
    "mkt": ("視覺篩選器", "某個幣剛出現 🔥（兩個以上來源同方向）"),
    "whale": ("巨鯨雷達", "新的巨鯨訊號"),
    "rank": ("OI 排名", "新擠進 OI 增幅前 10"),
    "anom": ("警報", "新警報、警報變成確認"),
    "dhx": ("數據訊號", "新的數據訊號"),
    "pos": ("持倉", "開倉、平倉"),
    "coins": ("幣種", "1H 結構轉向（上升↔下降）"),
    "diag": ("漏斗", "策略觸發"),
    "sig": ("訊號", "bot 發出新的策略訊號"),
    "sys": ("開關", "策略開關被改動"),
}
_LOCK = threading.Lock()
_DIR = None
_SUBS = {}          # endpoint -> {"sub": {...}, "prefs": {...}, "ts": ...}
_VAPID = None       # py_vapid 物件
_PUB = ""           # applicationServerKey（base64url）
_STATE = {"ok": False, "err": "", "sent": 0, "fail": 0, "last": 0}
CLAIM = {"sub": "https://saikesi-bot-clean-production.up.railway.app"}


def _coin(x):
    s = str(x or "").upper()
    for suf in ("-USDT-SWAP", "/USDT:USDT", "/USDT", "USDT"):
        if s.endswith(suf): return s[: -len(suf)]
    return s


def init(persist_dir):
    """載入/產生金鑰與訂閱。失敗只記錯，不拋。"""
    global _DIR, _VAPID, _PUB
    try:
        from py_vapid import Vapid01
        from cryptography.hazmat.primitives import serialization
        _DIR = os.path.join(persist_dir, "webpush"); os.makedirs(_DIR, exist_ok=True)
        kp = os.path.join(_DIR, "vapid_private.pem")
        if os.path.exists(kp):
            _VAPID = Vapid01.from_file(kp)
        else:
            _VAPID = Vapid01(); _VAPID.generate_keys(); _VAPID.save_key(kp)
        raw = _VAPID.public_key.public_bytes(serialization.Encoding.X962, serialization.PublicFormat.UncompressedPoint)
        _PUB = base64.urlsafe_b64encode(raw).decode().rstrip("=")
        sp = os.path.join(_DIR, "subs.json")
        if os.path.exists(sp):
            with open(sp, encoding="utf-8") as f:
                d = json.load(f)
            if isinstance(d, dict): _SUBS.update(d)
        _STATE.update(ok=True, err="")
    except Exception as e:
        _STATE.update(ok=False, err=f"{type(e).__name__}: {str(e)[:120]}")
        print("[推播] 初始化失敗", _STATE["err"], flush=True)


def _save():
    try:
        if not _DIR: return
        tmp = os.path.join(_DIR, "subs.json.tmp")
        with open(tmp, "w", encoding="utf-8") as f:
            json.dump(_SUBS, f, ensure_ascii=False)
        os.replace(tmp, os.path.join(_DIR, "subs.json"))
    except Exception:
        pass


def public_key():
    return _PUB


def upsert(sub, prefs):
    """存/更新一支手機的訂閱與分頁偏好。回 True/False。"""
    try:
        ep = str((sub or {}).get("endpoint") or "")
        if not ep.startswith("https://"): return False
        clean = {k: bool(v) for k, v in (prefs or {}).items() if k in TOPICS}
        with _LOCK:
            _SUBS[ep] = {"sub": {"endpoint": ep, "keys": dict((sub or {}).get("keys") or {})}, "prefs": clean, "ts": time.time()}
            _save()
        return True
    except Exception:
        return False


def remove(ep):
    try:
        with _LOCK:
            if _SUBS.pop(str(ep), None) is not None: _save()
    except Exception:
        pass


def _send_one(ep, rec, payload):
    try:
        from pywebpush import webpush, WebPushException
        webpush(subscription_info=rec["sub"], data=json.dumps(payload, ensure_ascii=False),
                vapid_private_key=_VAPID, vapid_claims=dict(CLAIM), ttl=3600, timeout=15)
        _STATE["sent"] += 1
    except Exception as e:
        _STATE["fail"] += 1
        code = getattr(getattr(e, "response", None), "status_code", None)
        if code in (404, 410): remove(ep)                       # 手機取消訂閱／過期 → 清掉


def push(topic, title, body, only_ep=None):
    """發給所有「這一頁有開」的手機（或只發給 only_ep，測試用）。背景發送、永不拋例外。"""
    try:
        if not _STATE["ok"] or _VAPID is None: return 0
        with _LOCK:
            targets = [(ep, r) for ep, r in _SUBS.items()
                       if (only_ep and ep == only_ep) or (not only_ep and r.get("prefs", {}).get(topic))]
        payload = {"title": title[:80], "body": body[:300], "tag": f"{topic}-{int(time.time())}", "tab": topic}
        for ep, r in targets:
            threading.Thread(target=_send_one, args=(ep, r, payload), daemon=True).start()
        _STATE["last"] = time.time()
        return len(targets)
    except Exception:
        return 0


def status():
    with _LOCK:
        n = len(_SUBS)
    return {"ok": _STATE["ok"], "err": _STATE["err"], "subs": n, "sent": _STATE["sent"], "fail": _STATE["fail"]}


# ───────────────────────── 事件比對 ─────────────────────────
def _snap(P):
    s = {}
    fp = P.get("fp") or {}
    s["fp"] = {f"{r.get('t')}|{r.get('coin')}": r for r in (fp.get("rows") or [])}
    s["fp_dec"] = fp.get("decision")
    # 🔥：同一個幣、同一個方向，兩個以上來源（對齊網頁 focusMap 的來源定義）
    src = {}
    def add(c, d, n): src.setdefault((c, d), set()).add(n)
    for x in (P.get("mkt") or {}).get("rows") or []:
        if x.get("inflow"):
            d = "bull" if x.get("q") == "多頭建倉" else ("bear" if x.get("q") == "空頭建倉" else None)
            if d: add(_coin(x.get("inst")), d, "篩選器")
    for x in P.get("whale") or []:
        if x.get("dir") == "bull": add(_coin(x.get("inst")), "bull", "巨鯨")
    for x in P.get("anom") or []:
        d = x.get("confirmed_dir") or x.get("init_dir")
        if d in ("bull", "bear"): add(_coin(x.get("inst") or x.get("coin")), d, "警報")
    for x in P.get("dhxev") or []:
        d = {"LONG": "bull", "SHORT": "bear"}.get(x.get("bias"))
        if d and x.get("status") == "持倉中": add(_coin(x.get("inst")), d, "數據")
    s["fire"] = {f"{c}|{d}": sorted(v) for (c, d), v in src.items() if len(v) >= 2}
    s["whale"] = {f"{x.get('inst')}|{x.get('first_ts')}": x for x in P.get("whale") or []}
    s["rank"] = [r.get("inst") for r in ((P.get("oi") or {}).get("up") or [])[:10]]
    s["anom"] = {str(x.get("inst") or x.get("coin")): x for x in P.get("anom") or []}
    s["dhx"] = {f"{x.get('inst')}|{x.get('ts')}": x for x in P.get("dhxev") or [] if x.get("status") == "持倉中"}
    s["pos"] = {str(t.get("key")): t for t in P.get("trades") or []}
    st = {}
    for sym, tfs in (P.get("coins") or {}).items():
        lab = ((tfs or {}).get("1H") or {}).get("struct_label")
        if lab in ("上升結構", "下降結構"): st[_coin(sym)] = lab
    s["coins"] = st
    trig = {}
    for g, rows in ((P.get("diag") or {}).get("groups") or {}).items():
        for r in rows or []:
            if r.get("k") == "觸發": trig[g] = r.get("v") or 0
    s["diag"] = trig
    s["sig"] = {f"{x.get('symbol')}|{x.get('tf')}|{x.get('strat')}|{x.get('ts')}": x for x in P.get("signals") or []}
    s["sys"] = dict(P.get("flags") or {})
    return s


def _events(a, b):
    """a＝上一輪、b＝這一輪 → [(topic, 標題, 內文)]。"""
    E = []
    for k, r in b["fp"].items():
        o = a["fp"].get(k)
        if o and (o.get("act"), o.get("K")) == (r.get("act"), r.get("K")): continue
        if r.get("act") == "進場" and r.get("K") == "持倉中":
            E.append(("fp", f"翻倉：可以下 {r.get('coin')} 做多", f"進 {r.get('e')}｜停損 {r.get('sl')}｜漲到 {r.get('be')} 移保本｜TP2 {r.get('tp2')}"))
        elif r.get("act") == "進場" and r.get("K") in ("贏", "保本", "輸"):
            E.append(("fp", f"翻倉：{r.get('coin')} 結果【{r.get('K')}】", f"紙上資金 {r.get('eq'):.1f}U｜檢定分數 {r.get('score'):+.2f}" if r.get("eq") is not None else ""))
        elif not o:
            E.append(("fp", f"翻倉：{r.get('coin')} 官方做多 不進", str(r.get("act"))))
    if b["fp_dec"] != a["fp_dec"] and b["fp_dec"] and b["fp_dec"] != "繼續記錄":
        E.append(("fp", "🚨 翻倉判定：" + b["fp_dec"], "到儀表板「翻倉」分頁看明細"))
    for k, v in b["fire"].items():
        if k not in a["fire"]:
            c, d = k.split("|"); E.append(("mkt", f"🔥 {c} {'偏多' if d == 'bull' else '偏空'}", "來源：" + "、".join(v)))
    for k, x in b["whale"].items():
        if k not in a["whale"]:
            E.append(("whale", f"巨鯨雷達：{_coin(x.get('inst'))}", {"bull": "偏多", "bear": "偏空"}.get(x.get("dir"), "待確認") + ("｜" + x["note"] if x.get("note") else "")))
    new_rank = [i for i in b["rank"] if i not in a["rank"]]
    if new_rank:
        E.append(("rank", "OI 排名：新進前 10", "、".join(_coin(i) for i in new_rank)))
    for k, x in b["anom"].items():
        o = a["anom"].get(k)
        if not o:
            E.append(("anom", f"警報：{_coin(k)}", str(x.get("bias_label") or x.get("init_dir") or "")))
        elif o.get("stage") != "CONFIRMED" and x.get("stage") == "CONFIRMED":
            E.append(("anom", f"警報確認：{_coin(k)}", str(x.get("bias_label") or "")))
    for k, x in b["dhx"].items():
        if k not in a["dhx"]:
            E.append(("dhx", f"數據訊號：{_coin(x.get('inst'))} {'做多' if x.get('bias') == 'LONG' else '做空'}", f"{x.get('kind')}｜進 {x.get('entry')}｜停損 {x.get('sl')}"))
    for k, t in b["pos"].items():
        if k not in a["pos"]:
            E.append(("pos", f"開倉：{_coin(t.get('symbol'))} {'做多' if t.get('direction') == 'long' else '做空'}", f"進 {t.get('entry_price')}｜停損 {t.get('current_sl')}｜{t.get('exit_strategy') or ''}"))
    for k, t in a["pos"].items():
        if k not in b["pos"]:
            E.append(("pos", f"平倉：{_coin(t.get('symbol'))}", f"原進場 {t.get('entry_price')}"))
    flips = [f"{c} {a['coins'][c][:2]}→{lab[:2]}" for c, lab in b["coins"].items() if c in a["coins"] and a["coins"][c] != lab]
    if flips:
        E.append(("coins", f"幣種：{len(flips)} 個 1H 結構轉向", "、".join(flips[:8]) + ("…" if len(flips) > 8 else "")))
    tr = [f"{g.replace('_DIAG', '')} +{int(v - a['diag'].get(g, v))}" for g, v in b["diag"].items() if v > a["diag"].get(g, v)]
    if tr:
        E.append(("diag", "漏斗：策略觸發", "、".join(tr)))
    for k, x in b["sig"].items():
        if k not in a["sig"]:
            E.append(("sig", f"訊號：{x.get('strat')} {_coin(x.get('symbol'))} {x.get('tf')}", f"{'做多' if x.get('dir') == 'long' else '做空'}｜價 {x.get('price')}"))
    ch = [f"{k.replace('_ENABLED', '')}→{'開' if v else '關'}" for k, v in b["sys"].items() if a["sys"].get(k) is not None and a["sys"].get(k) != v]
    if ch:
        E.append(("sys", "開關被改動", "、".join(ch)))
    return E


def _merge(E):
    """同一頁一輪超過 3 則 → 合併成一則，避免洗版。"""
    by = {}
    for t, ti, bo in E: by.setdefault(t, []).append((ti, bo))
    out = []
    for t, L in by.items():
        if len(L) <= 3: out += [(t, ti, bo) for ti, bo in L]
        else: out.append((t, f"{TOPICS[t][0]}：{len(L)} 則新通知", "；".join(ti for ti, _ in L[:6]) + ("…" if len(L) > 6 else "")))
    return out


def watch(G, collect, interval=60):
    prev = None
    while True:
        try:
            cur = _snap(collect(G, 1.0))
            if prev is not None:
                for t, ti, bo in _merge(_events(prev, cur)):
                    push(t, ti, bo)
            prev = cur
        except Exception:
            print("[推播] 比對例外", traceback.format_exc()[-300:], flush=True)
        time.sleep(interval)


def start(G, collect, persist_dir):
    init(persist_dir)
    threading.Thread(target=watch, args=(G, collect), name="webpush-watch", daemon=True).start()
