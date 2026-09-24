# -*- coding: utf-8 -*-
"""
私人儀表板（唯讀）— 掛在 main.py 既有的 Flask 上（Procfile 已是 web: python main.py）。

設計原則（來自 trading-backtest/CLAUDE.md，違反任何一條就會重演舊坑）：
  1. ★顯示層只讀 bot 已經算好的值，絕不自己再算一次。
     否則就是手冊第12條「顯示層要跟策略同步」的老坑 —— 網頁看到的數字跟 bot 判斷用的不同，
     以後對帳會歸因錯人（BPR空61筆被標成C3空的教訓）。
  2. ★開網頁不打任何交易所 API，全部讀記憶體。
     memory 記載系統天花板是 API rate limit；儀表板 20 秒輪詢一次 × 整天開著，很容易撞牆。
  3. ★任何例外都不准往上拋。put() 全程 try/except，儀表板壞掉不可以影響交易。
  4. ★憑證不落地不入眼：active_real_trades 裡有 headers（含 API 簽章），一律白名單欄位輸出，
     絕不 dump 原始 dict。
  5. ★Fail closed：沒設 DASH_TOKEN 或 token 不符 → 一律 404。
     不回 401 —— 401 等於告訴對方「這個路徑底下有東西」。
"""

import os
import time
import hmac
from threading import Lock

# ── 被動快照：由 main.py 的掃描迴圈餵進來（零額外 API） ──────────────────────
# 結構：_DASH[symbol][tf] = {欄位...}；_SIG[symbol] = 最近一次訊號
_DASH = {}
_SIG = {}
_LOCK = Lock()

# 漏斗計數器的「上一次快照」：手冊記載看儀表要看**最近增量**，
# 累計值會被開機至今的總數淹沒（某個擋點的增量＝呼叫增量 ＝ 整條策略被一道閘卡死）。
_DIAG_SNAP = {"ts": 0.0, "vals": {}}

_MAX_SIG = 40   # 最近訊號只留這麼多筆，避免記憶體無限長

# ★版本戳記：加到手機主畫面的 PWA 沒有網址列也沒有重新整理鍵，iOS 會拿舊快照，
#   推了新版使用者卻看到舊畫面（2026-09-24 用戶回報「沒改阿」就是這個）。
#   頁面內嵌這個字串，開頁後跟 /api 回的比對，不一樣就自動重載一次。
VER = "20260924y"


def _clean(v):
    """★把值壓成 JSON 安全的型別。不做這件事會出大事：
    `float('nan')` 經 flask.jsonify 會輸出字面的 `NaN`，那**不是合法 JSON** →
    瀏覽器 `JSON.parse` 直接拋錯 → **整個儀表板空白**（不是少一格，是全滅）。
    而 ATR%/ADX 在 K 棒不足時真的會是 NaN。(2026-09-24 被自己的測試抓到)"""
    if v is None or isinstance(v, (bool, str)):
        return v
    if isinstance(v, (int, float)):
        f = float(v)
        return v if (f == f and f not in (float("inf"), float("-inf"))) else None
    return str(v)


def put(symbol, tf, **kv):
    """掃描迴圈每掃一個 (幣, 時框) 就呼叫一次。永不拋例外。"""
    try:
        with _LOCK:
            d = _DASH.setdefault(str(symbol), {})
            row = d.setdefault(str(tf), {})
            row.update({str(k): _clean(v) for k, v in kv.items()})
            row["ts"] = time.time()
    except Exception:
        pass


def sig(symbol, tf, direction, strat, price, sl=None):
    """出訊號時記一筆（給「今天到底發了什麼」用）。永不拋例外。"""
    try:
        with _LOCK:
            _SIG[f"{symbol}|{tf}|{strat}|{int(time.time())}"] = {
                "symbol": symbol, "tf": tf, "dir": direction,
                "strat": strat, "price": price, "sl": sl, "ts": time.time(),
            }
            if len(_SIG) > _MAX_SIG:
                for k in sorted(_SIG, key=lambda x: _SIG[x]["ts"])[:len(_SIG) - _MAX_SIG]:
                    _SIG.pop(k, None)
    except Exception:
        pass


# ── 權杖 ────────────────────────────────────────────────────────────────────
def _token_ok(tok):
    real = os.environ.get("DASH_TOKEN", "")
    if not real or len(real) < 16:      # 沒設或太短 → 整個儀表板不存在
        return False
    return hmac.compare_digest(str(tok), real)


# ── 蒐集（全部讀 main.py 的 globals，永遠是當下值） ─────────────────────────
_TRADE_FIELDS = ("exchange", "inst_id", "symbol", "direction", "entry_price",
                 "current_sl", "tp1_hit", "tf_id", "exit_strategy", "entry_ts",
                 "risk_dist", "remaining_qty")   # ★白名單：headers / sl_order_id 一律不吐


def _oi_board(G, top_n=30):
    """全市場 OI 增幅榜。公式逐行對齊 main.py 的 _fetch_okx_oi_movers，
    不自己另寫一套（原則 1）。資料來自 _oi_history，已在記憶體，零 API。"""
    out = []
    try:
        hist_all = G.get("_oi_history") or {}
        window_h = G.get("OI_MOVERS_WINDOW_H", 12)
        cutoff = time.time() - window_h * 3600
        for inst, hist in list(hist_all.items()):
            if not hist or len(hist) < 2:
                continue
            oldest_t, oldest_v = hist[0]
            _, latest_v = hist[-1]
            if oldest_t > cutoff + 3600 or oldest_v <= 0:   # 基準點太新 → 不可信，跟 main.py 同判準
                continue
            out.append({"inst": inst,
                        "pct": (latest_v - oldest_v) / oldest_v,
                        "oi": latest_v})
        out.sort(key=lambda r: r["pct"], reverse=True)
    except Exception:
        pass
    return {"window_h": G.get("OI_MOVERS_WINDOW_H", 12),
            "tracked": len(G.get("_oi_history") or {}),
            "up": out[:top_n], "down": out[::-1][:top_n]}


# ★官方（數據獵手）四象限語意 —— 逐字取自 memory/project_0903_oidash_spec.md 抓到的官方說明。
#   我第一版自己取名「多建/空出」是錯的，名字和語意都要照官方。
_QUAD = {
    "多頭建倉": ("本質看漲｜主動做多",
               "OI 擴張 + 價格上漲：新多單主動進場，買方資金大量湧入。"
               "是最強的看多信號，代表市場共識偏多，但需留意過熱後的短線回調風險。", "up"),
    "空頭平倉": ("本質看漲｜被動上漲",
               "OI 收縮 + 價格上漲：空頭倉位被迫平倉，需買入回補導致價格上漲。"
               "屬於被動性看漲，上漲動力來自空頭出場而非新買盤，"
               "需確認後續真實多單能否接力，否則容易反轉回落。", "up"),
    "空頭建倉": ("本質看跌｜主動做空",
               "OI 擴張 + 價格下跌：新空單主動進場，賣方資金大量湧入。"
               "是最強的看空信號，代表市場共識偏空，不宜逆勢追多，注意下跌風險。", "down"),
    "多頭平倉": ("本質看跌｜被動下跌",
               "OI 收縮 + 價格下跌：多頭倉位獲利了結或止損離場，賣出壓力導致價格下滑。"
               "屬於被動性看跌，上漲動能減弱，高位持多者需注意減倉時機，短線可能持續回調。", "down"),
}

# ★官方硬編碼的篩選條件（2026-09-24 直接看網頁抓的原文）：
#   「象限圖固定顯示 OI ≥ 1%、|價格| ≤ 5% 的標的；
#     資金注入候選固定使用 1H OI ≥ 4%、|價格| ≤ 3%。」
#   ★注意價格是**上限**不是下限 —— 要找的是「OI 大動、價格還沒動」（主力安靜建倉）。
#     我第一版做成「兩個都要超過門檻」方向相反，正好把他們要的那批濾掉。
QUAD_OI_MIN, QUAD_PX_MAX = 0.01, 0.05        # 象限圖
INFLOW_OI_MIN, INFLOW_PX_MAX = 0.04, 0.03    # 資金注入候選（官方只用 1H）
# 官方流程原文：「先找出 1H 資金注入候選；觀察 15 分鐘後，以 OI 保留、相對 BTC 強弱與 CVD
#   判斷方向。15m／30m 僅觀察變化，不另產生卡片或通知。」→ 所以只有 15m/30m/1H 三檔，沒有 4H/12H。
# ★取樣間隔一律**跟 main.py 讀**，不要在這裡另存一份：
#   「同一個值、兩個來源」是手冊記過的坑，改了一邊忘了另一邊，容差就會算錯。
DASH_SAMPLE_SEC_FALLBACK = 300.0


def _at(hist, target_ts, max_gap):
    """在 target_ts 做**線性內插**，回 (target_ts, 內插值)；辦不到就回 None。

    為什麼不是「取最接近的點」（前兩版都錯在這）：
      ①「取 ≤ target+容差的最後一點」→ 系統性偏向較新的點，1H 窗實際只量到 45 分鐘
        （實測 +30% 被算成 +13%）。
      ②「取最接近且差距 ≤ 容差的點」→ 要求**剛好有取樣點落在目標時刻附近**。
        但取樣相位是任意的，而且改過取樣頻率後新舊資料密度不同 →
        target 隨時間滑動，時而對得上時而對不上，**同一個窗會忽有忽無**
        （2026-09-24 線上實測：1H 窗先 300 幣、20 分鐘後同一個窗 0 幣）。
    內插沒有相位問題：只要 target 落在序列範圍內就算得出來。

    兩道防線（寧可不顯示，也不要給錯的數字）：
      · target 比最舊的點還舊 → None（歷史真的不夠長，不可以拿短窗冒充長窗）
      · 跨越 target 的那兩點間距 > max_gap → None（中間有停機的大洞，內插不可信）
    """
    prev = None
    for t, v in hist:
        if t <= target_ts:
            prev = (t, v)
        elif prev is None:
            return None                      # 最舊的點都比 target 新 → 歷史不夠長
        else:
            if t - prev[0] > max_gap:
                return None                  # 這段有大洞（bot 停機）→ 不內插
            f = (target_ts - prev[0]) / (t - prev[0])
            return (target_ts, prev[1] + (v - prev[1]) * f)
    return None                              # target 比最新的點還新


def _market(G, win_h=1.0, top_n=300):
    """★四象限（OI 變化 × 價格變化）。兩邊都讀記憶體，零 API。

    OI ← `_oi_history`、價 ← `_PX_HISTORY`，**同一個時間窗**（都由 `_oi_sample_tick` 每 5 分鐘取樣）。
    官方只有 15m / 30m / 1H 三檔，排名預設 1H。窗的兩端都用 `_at` 線性內插，沒有相位問題。
    排序照官方：**依 OI 變化的「金額」**（|ΔOI USD|），不是百分比 —— 小幣百分比會灌水。
    """
    rows = []
    try:
        oi_all = G.get("_oi_history") or {}
        px_all = G.get("_PX_HISTORY") or {}
        snap = G.get("_TICKER_SNAP") or {}
        mcap = G.get("_MCAP") or {}
        now = time.time()
        target = now - win_h * 3600
        samp = float(G.get("DASH_SAMPLE_SEC") or DASH_SAMPLE_SEC_FALLBACK)
        # 內插允許跨越的最大空洞：正常取樣間隔的 4 倍，且至少 30 分鐘。
        # 超過就是 bot 停過機，那段不內插（給 None，該幣這輪不顯示）。
        max_gap = max(samp * 4, 1800.0)
        for inst, hist in list(oi_all.items()):
            if not hist or len(hist) < 2:
                continue
            base = _at(hist, target, max_gap)   # 內插；歷史不夠長或中間有洞 → None
            if not base or base[1] <= 0:
                continue
            l_v = hist[-1][1]
            d_usd = l_v - base[1]
            oi_pct = d_usd / base[1]
            # ★官方 `oi_chg_1h` = OKX 與幣安 OI 變化%的**算術平均**（逐筆驗算過：
            #   STABLE (9.29+2.21)/2=5.75、ONE (8.44+0.69)/2=4.56、PYTH (5.4−0.01)/2=2.70）。
            #   只有一所有資料時就用那一所 —— 官方 CNPY 只有 OKX 時也是直接用 5.20。
            src = "OKX"
            bh = (G.get("_BN_HISTORY") or {}).get(inst)
            if bh and len(bh) >= 2:
                bb = _at(bh, target, max_gap)
                if bb and bb[1] > 0:
                    oi_pct = (oi_pct + (bh[-1][1] - bb[1]) / bb[1]) / 2.0
                    src = "OKX+BN"
            ph = px_all.get(inst) or []
            pbase = _at(ph, target, max_gap) if len(ph) >= 2 else None
            if not pbase or pbase[1] <= 0:
                continue                  # 價格同理：不同窗不可以混（同上）
            px_pct = (ph[-1][1] - pbase[1]) / pbase[1]
            quad = ("多頭建倉" if px_pct > 0 else "空頭建倉") if oi_pct > 0 else \
                   ("空頭平倉" if px_pct > 0 else "多頭平倉")
            s = snap.get(inst) or {}
            # 排名表的象限：官方 sigKey 用 `d.priceChg`，同時刻對帳 24H 命中 17/20（1H 只有 7/20）。
            # 另 3 筆是被官方評分系統的 mktLabel 覆寫 —— 我們沒有那套評分，複刻不了，這是已知差異。
            c24 = s.get("chg24h")
            quad24 = quad if c24 is None else (
                ("多頭建倉" if c24 >= 0 else "空頭建倉") if oi_pct >= 0 else
                ("空頭平倉" if c24 >= 0 else "多頭平倉"))
            mc = mcap.get(inst.replace("-USDT-SWAP", ""))
            rows.append({
                "inst": inst, "oi": oi_pct, "d_usd": d_usd, "px": px_pct, "q": quad,
                "oiu": l_v, "last": s.get("last"), "chg24h": s.get("chg24h"),
                "vol": s.get("volccy_usd"), "oimc": (l_v / mc) if mc else None, "mcap": mc,
                # 官方兩道固定條件（價格是**上限**：要「OI 大動、價格還沒動」）
                "q24": quad24, "src": src,
                "inq": abs(oi_pct) >= QUAD_OI_MIN and abs(px_pct) <= QUAD_PX_MAX,
                "inflow": (win_h == 1.0 and oi_pct >= INFLOW_OI_MIN
                           and abs(px_pct) <= INFLOW_PX_MAX),
            })
        # 「異常」= |OI 變化%| 落在全市場高百分位。
        # ★誠實標註：這是**跨幣橫向比較**，不是「相對這個幣自己的常態」（後者要長歷史落地才做得到）。
        if rows:
            mags = sorted(abs(r["oi"]) for r in rows)
            def _q(p):
                return mags[min(len(mags) - 1, int(len(mags) * p))]
            p95, p90 = _q(0.95), _q(0.90)
            for r in rows:
                a = abs(r["oi"])
                r["an"] = 2 if a >= p95 else (1 if a >= p90 else 0)
        # ★官方前端代碼是 `Math.abs(oiChgPct)` 降序取 top20（文案寫「依變化金額」是錯的，
        #   以代碼為準），而且是**全部幣一起排**再分到四組，不是每組各取 N。
        rows.sort(key=lambda r: abs(r.get("oi") or 0), reverse=True)
    except Exception:
        pass
    # 還要等多久：拿「OI 與價格都有」的幣裡最深的那份歷史當進度。
    # 空白畫面要講得出「還差幾分鐘」，不然使用者只會看到一片空，以為壞了。
    depth = 0.0
    try:
        oi_all = G.get("_oi_history") or {}
        px_all = G.get("_PX_HISTORY") or {}
        nowt = time.time()
        for inst, h in oi_all.items():
            ph = px_all.get(inst)
            if h and ph:
                depth = max(depth, nowt - max(h[0][0], ph[0][0]))
    except Exception:
        pass
    _bn = sum(1 for r in rows if r.get("src") == "OKX+BN")
    return {"win_h": win_h, "quads": {k: list(v) for k, v in _QUAD.items()},
            "src": ("OKX+BN " + str(_bn)) if _bn else "OKX",
            "gate": {"oi_min": QUAD_OI_MIN, "px_max": QUAD_PX_MAX,
                     "inflow_oi": INFLOW_OI_MIN, "inflow_px": INFLOW_PX_MAX},
            "tracked": len(G.get("_oi_history") or {}),
            "priced": len(G.get("_TICKER_SNAP") or {}),
            "sample": dict(G.get("_DASH_SAMPLE") or {}),
            "depth_min": int(depth / 60),
            "eta_min": max(0, int((win_h * 3600 - depth) / 60)),
            "rows": rows[:top_n]}


def _breadth(G):
    """市場廣度：全市場漲／平／跌家數。零額外 API —— `_TICKER_SNAP` 是取樣時順手存的。

    官方首頁顯示的樣子：「89% 偏空　漲 20　平 10　跌 250　共 280 個合約」。
    對照該筆：250/280 = 89.3% → **偏空% = 跌家數 ÷ 總數**（不是跌/(漲+跌)）。
    「平」他們只有 10/280≈3.6%，用 |24h 漲跌| < 0.1% 抓得到這個量級。
    """
    up = flat = down = 0
    try:
        for v in (G.get("_TICKER_SNAP") or {}).values():
            c = v.get("chg24h")
            if c is None:
                continue
            if abs(c) < 0.001:
                flat += 1
            elif c > 0:
                up += 1
            else:
                down += 1
    except Exception:
        pass
    n = up + flat + down
    return {"up": up, "flat": flat, "down": down, "n": n,
            "bear_pct": round(down / n * 100) if n else None,
            "bull_pct": round(up / n * 100) if n else None}


def _diags(G):
    """漏斗：累計值 + 距上次開頁的增量。手冊：只能看各 gate 的絕對次數，
    不能拿「無V成型/呼叫」當比例（_VLONG_DIAG 那個尾端無條件 +1 的坑）。"""
    names = [n for n in G if n.endswith("_DIAG") and isinstance(G.get(n), dict)]
    now = time.time()
    cur, out = {}, {}
    for n in sorted(names):
        d = G.get(n) or {}
        for k, v in d.items():
            if isinstance(v, (int, float)):
                cur[f"{n}.{k}"] = v
    prev = _DIAG_SNAP["vals"]
    elapsed = now - _DIAG_SNAP["ts"] if _DIAG_SNAP["ts"] else 0
    for n in sorted(names):
        d = G.get(n) or {}
        rows = []
        for k, v in d.items():
            if not isinstance(v, (int, float)):
                continue
            key = f"{n}.{k}"
            rows.append({"k": k, "v": v, "d": (v - prev.get(key, v)) if prev else 0})
        out[n.strip("_")] = rows
    _DIAG_SNAP["vals"] = cur
    _DIAG_SNAP["ts"] = now
    return {"elapsed": int(elapsed), "groups": out}


def _trades(G):
    out = []
    try:
        for key, t in list((G.get("active_real_trades") or {}).items()):
            row = {"key": key}
            for f in _TRADE_FIELDS:
                v = t.get(f)
                row[f] = v if isinstance(v, (int, float, str, bool)) or v is None else str(v)
            # 未實現 R：用被動快照裡的最新價算（不打 API）
            try:
                entry = float(t.get("entry_price") or 0)
                risk = float(t.get("risk_dist") or 0)
                last = None
                with _LOCK:
                    for tf_row in (_DASH.get(t.get("symbol") or "", {}) or {}).values():
                        if tf_row.get("px"):
                            if last is None or tf_row.get("ts", 0) > last[1]:
                                last = (tf_row["px"], tf_row.get("ts", 0))
                if last and entry > 0 and risk > 0:
                    diff = (last[0] - entry) if t.get("direction") == "long" else (entry - last[0])
                    row["r"] = round(diff / risk, 2)
                    row["px"] = last[0]
            except Exception:
                pass
            out.append(row)
        out.sort(key=lambda r: r.get("entry_ts") or 0, reverse=True)
    except Exception:
        pass
    return out


def _flags(G):
    out = {}
    for n in sorted(G):
        if n.endswith("_ENABLED") and isinstance(G.get(n), bool):
            out[n] = G[n]
    return out


def collect(G, win_h=1.0):
    with _LOCK:
        coins = {s: {tf: dict(r) for tf, r in d.items()} for s, d in _DASH.items()}
        sigs = sorted(_SIG.values(), key=lambda r: r["ts"], reverse=True)
    return {
        "now": time.time(),
        "ver": VER,
        "mode": {
            "live": bool(G.get("_LIVE_MODE")),
            "demo": bool(G.get("OKX_DEMO")),
            "pool": len(G.get("SYMBOLS") or {}),
            "risk_pct": G.get("RISK_PCT"),
            "auto_trade": dict(G.get("AUTO_TRADE") or {}),
        },
        "trades": _trades(G),
        "oi": _oi_board(G),
        "mkt": _market(G, win_h),
        "dhx": sorted((G.get("_DHX_SIG") or {}).values(),
                      key=lambda r: r.get("ts") or 0, reverse=True)[:30],
        "breadth": _breadth(G),
        "anom": sorted((G.get("_ANOM") or {}).values(),
                       key=lambda r: r.get("last_ts") or 0, reverse=True)[:40],
        "diag": _diags(G),
        "flags": _flags(G),
        "coins": coins,
        "signals": sigs,
    }


# ── 路由 ────────────────────────────────────────────────────────────────────
def register(app, G):
    """在 main.py 的 Flask app 上掛載。G = main.py 的 globals()（讀到的永遠是當下值）。"""
    from flask import jsonify, make_response, request

    @app.route("/d/<tok>")
    def _dash_page(tok):
        if not _token_ok(tok):
            return "", 404
        r = make_response(_HTML)
        r.headers["Content-Type"] = "text/html; charset=utf-8"
        r.headers["X-Robots-Tag"] = "noindex, nofollow"
        r.headers["Cache-Control"] = "no-store"
        return r

    @app.route("/d/<tok>/api")
    def _dash_api(tok):
        if not _token_ok(tok):
            return "", 404
        try:
            try:
                _w = float(request.args.get("w", 1) or 1)
            except (TypeError, ValueError):
                _w = 1.0
            _w = min(24.0, max(0.25, _w))
            r = jsonify(collect(G, _w))
        except Exception as e:
            r = jsonify({"error": f"{type(e).__name__}: {e}"})
        r.headers["X-Robots-Tag"] = "noindex, nofollow"
        r.headers["Cache-Control"] = "no-store"
        # ★CORS:讓前端可以住在 Railway 以外(claude.ai Artifact / 本機 html),
        #   改版面就不必再推 bot、不必讓 bot 重啟(重啟代價=OI歷史歸零+持倉重新接管)。
        #   安全性不靠來源網域,靠路徑裡的 token —— 沒 token 根本進不到這裡。
        r.headers["Access-Control-Allow-Origin"] = "*"
        return r

    return app


_HTML = """<!doctype html>
<html lang="zh-Hant"><head>
<meta charset="utf-8">
<meta name="viewport" content="width=device-width, initial-scale=1, viewport-fit=cover">
<meta name="robots" content="noindex, nofollow">
<meta name="apple-mobile-web-app-capable" content="yes">
<meta name="apple-mobile-web-app-status-bar-style" content="black-translucent">
<meta name="theme-color" content="#0b0e14">
<title>盤面</title>
<style>
  :root{
    --bg:#0b0e14; --card:#141922; --line:#232a36; --fg:#e6edf7; --dim:#8b97ab;
    --up:#35d07f; --down:#ff5c6c; --warn:#ffb74d; --accent:#6aa3ff;
  }
  *{box-sizing:border-box}
  body{margin:0;background:var(--bg);color:var(--fg);
       font:14px/1.5 -apple-system,"Segoe UI","Noto Sans TC",system-ui,sans-serif;
       /* ★瀏海/動態島:加到主畫面是全螢幕,頂部要讓開 safe-area,否則第一排會被島擋住 */
       padding:calc(10px + env(safe-area-inset-top)) calc(12px + env(safe-area-inset-right))
               calc(28px + env(safe-area-inset-bottom)) calc(12px + env(safe-area-inset-left));
       -webkit-text-size-adjust:100%}
  h1{font-size:15px;margin:0;font-weight:600;letter-spacing:.02em}
  .top{display:flex;align-items:center;gap:8px;flex-wrap:wrap;margin-bottom:10px}
  .pill{font-size:11px;padding:3px 9px;border-radius:99px;border:1px solid var(--line);
        color:var(--dim);white-space:nowrap}
  .pill.live{color:var(--up);border-color:#1e4d36;background:#0f2419}
  .pill.paper{color:var(--warn);border-color:#4d3d1e}
  .card{background:var(--card);border:1px solid var(--line);border-radius:12px;
        padding:12px;margin-bottom:12px;overflow:hidden}
  .card h2{font-size:12px;margin:0 0 10px;color:var(--dim);font-weight:600;
           text-transform:uppercase;letter-spacing:.08em;display:flex;
           justify-content:space-between;align-items:baseline;gap:8px}
  .card h2 span{text-transform:none;letter-spacing:0;font-weight:400;font-size:11px}
  .scroll{overflow-x:auto;-webkit-overflow-scrolling:touch;margin:0 -12px;padding:0 12px}
  table{border-collapse:collapse;width:100%;font-variant-numeric:tabular-nums;font-size:13px}
  th,td{text-align:right;padding:6px 8px;white-space:nowrap;border-bottom:1px solid var(--line)}
  th:first-child,td:first-child{text-align:left;position:sticky;left:0;background:var(--card)}
  th{color:var(--dim);font-weight:500;font-size:11px;cursor:pointer;user-select:none}
  th:hover{color:var(--fg)}
  tbody tr:last-child td{border-bottom:none}
  .up{color:var(--up)} .down{color:var(--down)} .dim{color:var(--dim)} .warn{color:var(--warn)}
  .grid{display:grid;grid-template-columns:repeat(auto-fill,minmax(150px,1fr));gap:6px}
  .q4{display:grid;grid-template-columns:repeat(4,1fr);gap:6px;margin-bottom:10px}
  .qc{background:#0f141c;border:1px solid var(--line);border-radius:8px;padding:8px 6px;
      text-align:center;cursor:pointer;line-height:1.25}
  .qc.on{border-color:var(--accent);background:#16233a}
  .qc b{display:block;font-size:17px;font-variant-numeric:tabular-nums}
  .qc span{font-size:10px;color:var(--dim)}
  .star{color:var(--warn)}
  .wins{display:flex;gap:6px;margin-bottom:10px}
  .wb{padding:4px 12px;border:1px solid var(--line);border-radius:7px;background:#0f141c;
      color:var(--dim);cursor:pointer;font-size:12px}
  .wb.on{color:var(--fg);border-color:var(--accent);background:#16233a}
  .sc{width:100%;max-width:560px;height:auto;display:block;margin:2px auto 6px;overflow:hidden}
  .sc .ql{font-size:9px;font-weight:600;opacity:.85}
  .sc .qr{text-anchor:end}
  .sc .ax{font-size:8px;fill:var(--dim)}
  .sc .pl{font-size:8px;fill:var(--fg)}
  .sl{display:flex;align-items:center;gap:10px;margin:6px 0}
  .sl label{font-size:12px;color:var(--dim);white-space:nowrap;min-width:108px}
  .sl label b{color:var(--fg);font-variant-numeric:tabular-nums}
  .sl input{flex:1;accent-color:var(--accent);height:26px}
  .qh{text-transform:none;letter-spacing:0;font-size:13px;font-weight:600}
  .src{margin-left:auto;align-self:center;font-size:10px;color:var(--dim);
       border:1px solid var(--line);border-radius:99px;padding:2px 8px}
  .cl{color:var(--fg);text-decoration:underline;text-decoration-color:var(--line);
      cursor:pointer;text-underline-offset:3px}
  .ovl{position:fixed;inset:0;background:#000a;z-index:40}
  .cd{position:fixed;z-index:41;left:50%;transform:translateX(-50%);bottom:0;width:100%;
      max-width:460px;border-radius:14px 14px 0 0;max-height:86vh;overflow:auto;
      padding-bottom:calc(16px + env(safe-area-inset-bottom))}
  .cq{font-size:15px;font-weight:700}
  .cr{display:flex;justify-content:space-between;padding:6px 0;border-bottom:1px solid var(--line);
      font-size:13px;font-variant-numeric:tabular-nums}
  .cr span{color:var(--dim)}
  .btns{display:flex;gap:8px;margin-top:12px;flex-wrap:wrap}
  .bt{flex:1;min-width:104px;padding:11px 8px;border-radius:9px;border:1px solid var(--line);
      background:#0f141c;color:var(--fg);font-size:13px;cursor:pointer;font-weight:600}
  .bt.bo{background:#16233a;border-color:var(--accent);color:#cfe0ff}
  .rel{display:flex;flex-wrap:wrap;gap:6px;margin-top:6px}
  .rl{font-size:12px;padding:4px 8px;border:1px solid var(--line);border-radius:7px;
      background:#0f141c;cursor:pointer;font-variant-numeric:tabular-nums}
  .bar{height:6px;background:#0f141c;border-radius:99px;overflow:hidden}
  .bar i{display:block;height:100%;background:var(--accent);border-radius:99px;transition:width .3s}
  .ver{font-size:10px;color:var(--dim);cursor:pointer;padding:3px 8px;border:1px solid var(--line);
       border-radius:99px}
  .kv{display:flex;justify-content:space-between;gap:8px;padding:4px 8px;
      background:#0f141c;border-radius:6px;font-size:12px}
  .kv b{font-weight:600;font-variant-numeric:tabular-nums}
  .tabs{display:flex;gap:6px;margin-bottom:12px;overflow-x:auto;-webkit-overflow-scrolling:touch}
  .tab{padding:6px 12px;border:1px solid var(--line);border-radius:8px;background:var(--card);
       color:var(--dim);cursor:pointer;white-space:nowrap;font-size:13px}
  .tab.on{color:var(--fg);border-color:var(--accent);background:#16233a}
  .empty{color:var(--dim);font-size:12px;padding:8px 0}
  .sub{color:var(--dim);font-size:11px}
  input.f{background:#0f141c;border:1px solid var(--line);color:var(--fg);border-radius:8px;
          padding:6px 10px;font-size:13px;width:100%;margin-bottom:10px}
</style></head><body>

<div class="top">
  <h1>盤面</h1>
  <span class="pill" id="mode">—</span>
  <span class="pill" id="pool">—</span>
  <span class="pill" id="age">—</span>
  <span class="ver" onclick="location.reload()" title="點一下強制重新載入">⟳ <span id="ver">—</span></span>
</div>

<div class="tabs" id="tabs"></div>
<div id="view"></div>
<div id="card"></div>

<script>
const API = location.pathname.replace(/\\/$/,'') + '/api';
let D = null, TAB = 'mkt', SORT = {}, QF = '';

const f = (n,d=2)=> (n===null||n===undefined||isNaN(n)) ? '—' : Number(n).toFixed(d);
// 幣價：大幣固定 4 位、小幣改用有效位數（否則 PEPE 之類會被截成 0.000010）
const pf = n => (n===null||n===undefined||n===''||isNaN(n)) ? '—'
  : (Math.abs(Number(n))>=1 ? Number(n).toFixed(4) : Number(n).toPrecision(5))
      .replace(/(\\.\\d*?)0+$/,'$1').replace(/\\.$/,'');
const f2 = n => (n===null||n===undefined||isNaN(n)) ? '—' : (n>=0?'+':'')+Number(n).toFixed(2)+'%';
const pct = n => (n===null||n===undefined||isNaN(n)) ? '—' : (n*100>=0?'+':'') + (n*100).toFixed(2) + '%';
const cls = n => n>0 ? 'up' : (n<0 ? 'down' : 'dim');
const ago = ts => { if(!ts) return '—'; const s=Math.max(0,D.now-ts);
  return s<60 ? Math.round(s)+'s' : s<3600 ? Math.round(s/60)+'m' : Math.round(s/3600)+'h'; };

function table(id, cols, rows, render){
  if(!rows.length) return '<div class="empty">沒有資料</div>';
  const s = SORT[id];
  if(s){ const i=s.i; rows=[...rows].sort((a,b)=>{
    const x=render(a)[i], y=render(b)[i];
    const xv=x&&x.v!==undefined?x.v:x, yv=y&&y.v!==undefined?y.v:y;
    if(typeof xv==='number'&&typeof yv==='number') return s.dir*(yv-xv);
    return s.dir*String(yv).localeCompare(String(xv));
  }); }
  let h = '<div class="scroll"><table><thead><tr>' +
    cols.map((c,i)=>`<th onclick="sortBy('${id}',${i})">${c}${s&&s.i===i?(s.dir>0?' ↓':' ↑'):''}</th>`).join('') +
    '</tr></thead><tbody>';
  for(const r of rows){
    h += '<tr>' + render(r).map(c=>{
      const v = (c&&c.v!==undefined)?c.h!==undefined?c.h:c.v:c;
      return `<td class="${(c&&c.c)||''}">${v}</td>`;
    }).join('') + '</tr>';
  }
  return h + '</tbody></table></div>';
}
function sortBy(id,i){ const s=SORT[id]; SORT[id] = (s&&s.i===i)?{i,dir:-s.dir}:{i,dir:1}; draw(); }

const TABS = [['mkt','篩選器'],['rank','OI 排名'],['anom','警報'],['dhx','數據訊號'],['pos','持倉'],['coins','幣種'],
              ['diag','漏斗'],['sig','訊號'],['sys','開關']];
// 官方四象限順序：左上 空頭平倉 / 右上 多頭建倉 / 左下 多頭平倉 / 右下 空頭建倉
const QUADS = ['多頭建倉','空頭平倉','空頭建倉','多頭平倉'];
const QCLR = {'多頭建倉':'var(--up)','空頭平倉':'#6fd3a8','空頭建倉':'var(--down)','多頭平倉':'#e08a94'};
// 官方只有 15m / 30m / 1H 三檔（原文：「15m／30m 僅觀察變化，不另產生卡片或通知」）
const WINS = [[0.25,'15m'],[0.5,'30m'],[1,'1H']];
let W = 1, OITH = 1, PXTH = 5;   // 預設＝官方象限圖條件：OI ≥ 1%、|價格| ≤ 5%
try{ const s=JSON.parse(localStorage.getItem('dash')||'{}');
     if(s.W && WINS.some(x=>x[0]===s.W)) W=s.W;      // 舊版存的 4/12 會被丟掉
     if(s.OITH) OITH=s.OITH; if(s.PXTH) PXTH=s.PXTH; }catch(e){}
function save(){ try{ localStorage.setItem('dash',JSON.stringify({W,OITH,PXTH})); }catch(e){} }
function setW(w){ W=w; save(); tick(); }
function setTh(which,v){ v=parseFloat(v); if(which==='oi') OITH=v; else PXTH=v; save(); draw(); }

function draw(){
  if(!D) return;
  document.getElementById('tabs').innerHTML = TABS.map(([k,n])=>
    `<div class="tab ${k===TAB?'on':''}" onclick="TAB='${k}';draw()">${n}${k==='pos'?' '+D.trades.length:''}</div>`).join('');
  const m=D.mode, mo=document.getElementById('mode');
  mo.textContent = m.demo ? '模擬盤' : (m.live ? 'LIVE' : 'PAPER');
  mo.className = 'pill ' + (m.live && !m.demo ? 'live':'paper');
  document.getElementById('pool').textContent = '幣池 ' + m.pool;
  document.getElementById('age').textContent = new Date(D.now*1000)
    .toLocaleTimeString('zh-TW',{hour12:false});
  const vEl=document.getElementById('ver');
  vEl.textContent = D.ver || '—';
  vEl.parentElement.style.color = (D.ver===PAGE_VER) ? '' : 'var(--warn)';

  const v=document.getElementById('view');
  document.getElementById('card').innerHTML = cardHTML();
  if(TAB==='mkt') v.innerHTML = viewMkt();
  if(TAB==='rank') v.innerHTML = viewRank();
  if(TAB==='dhx') v.innerHTML = viewDhx();
  if(TAB==='anom') v.innerHTML = viewAnom();
  if(TAB==='pos') v.innerHTML = viewPos();
  if(TAB==='oi') v.innerHTML = viewOI();
  if(TAB==='coins') v.innerHTML = viewCoins();
  if(TAB==='diag') v.innerHTML = viewDiag();
  if(TAB==='sig') v.innerHTML = viewSig();
  if(TAB==='sys') v.innerHTML = viewSys();
}

function winBar(){
  return '<div class="wins">' + WINS.map(([w,n])=>
    `<div class="wb ${W===w?'on':''}" onclick="setW(${w})">${n}</div>`).join('')
    + `<span class="src" title="官方 oi_chg_1h = OKX 與幣安的算術平均。這裡有補到幣安的幣會標 OKX+BN；
       幣安 fapi 若被 Railway 地理封鎖(451)就只剩 OKX。">${(D&&D.mkt&&D.mkt.src)||'OKX'}</span></div>`;
}
function noData(m){
  const pctDone = Math.min(100, Math.round(m.depth_min/(m.win_h*60)*100)) || 0;
  return '<div class="card">' + winBar()
    + `<h2>${m.win_h}H 窗累積中<span>${m.depth_min} / ${m.win_h*60} 分鐘</span></h2>`
    + `<div class="bar"><i style="width:${pctDone}%"></i></div>`
    + `<div class="sub" style="margin-top:8px">`
    + `已取樣 ${m.depth_min} 分鐘，還要約 <b>${m.eta_min} 分鐘</b>`
    + `（每 15 分鐘取樣一次，追蹤 ${m.tracked} 個合約、報價 ${m.priced} 個）。`
    + (m.win_h > 1 ? ' 先看 1H 那格，它最快滿。' : '')
    + ' bot 每次重新部署會歸零重算。</div></div>';
}

// ── 視覺篩選器：X=OI 變化%，Y=價格變化%，四象限 + 拉桿門檻框 ─────────────────
function scatter(rows){
  const Wd=360, Ht=300, L=34, R=8, T=10, B=24;
  const x0=L, x1=Wd-R, y0=T, y1=Ht-B, cx=(x0+x1)/2, cy=(y0+y1)/2;
  // ★軸範圍用 p95 不用 max：用 max 時一個離群值就把整張圖壓扁，
  //   實測線上凌晨時段所有點擠成一條水平線（Y 軸 ±20% 但點都在 ±3% 內）。
  //   下限綁在門檻的 1.6 倍，確保虛線框一定看得到；超出範圍的點夾到邊緣。
  const q95 = a => { if(!a.length) return 0; const s=[...a].sort((p,q)=>p-q);
                     return s[Math.min(s.length-1, Math.floor(s.length*0.95))]; };
  const mx = Math.max(OITH*1.6, q95(rows.map(r=>Math.abs(r.oi*100)))*1.15) || 10;
  const my = Math.max(PXTH*1.6, q95(rows.map(r=>Math.abs(r.px*100)))*1.15) || 10;
  const clamp = (v,lo,hi) => Math.max(lo, Math.min(hi, v));
  const X = v => clamp(cx + (v*100/mx)*((x1-x0)/2), x0+2, x1-2);
  const Y = v => clamp(cy - (v*100/my)*((y1-y0)/2), y0+2, y1-2);
  const hit = r => Math.abs(r.oi*100)>=OITH && Math.abs(r.px*100)<=PXTH;
  let s = `<svg viewBox="0 0 ${Wd} ${Ht}" class="sc">`;
  s += `<rect x="${cx}" y="${y0}" width="${x1-cx}" height="${cy-y0}" fill="#35d07f" opacity=".07"/>`
    +  `<rect x="${x0}" y="${y0}" width="${cx-x0}" height="${cy-y0}" fill="#35d07f" opacity=".03"/>`
    +  `<rect x="${cx}" y="${cy}" width="${x1-cx}" height="${y1-cy}" fill="#ff5c6c" opacity=".07"/>`
    +  `<rect x="${x0}" y="${cy}" width="${cx-x0}" height="${y1-cy}" fill="#ff5c6c" opacity=".03"/>`;
  // ★官方條件：|OI| ≥ 下限 **且** |價格| ≤ 上限 → 命中區是「左右兩塊 × 中央水平帶」，
  //   不是四個角。要找的是「OI 大動、價格還沒動」＝主力安靜建倉。
  const byT = Y(PXTH/100), byB = Y(-PXTH/100);
  [[x0, X(-OITH/100)], [X(OITH/100), x1]].forEach(([bx, bx2])=>{
    if(bx2>bx && byB>byT) s += `<rect x="${bx}" y="${byT}" width="${bx2-bx}" height="${byB-byT}"
       fill="#6aa3ff" fill-opacity=".05" stroke="#6aa3ff" stroke-width="1"
       stroke-dasharray="3 3" opacity=".65"/>`;
  });
  s += `<line x1="${x0}" y1="${cy}" x2="${x1}" y2="${cy}" stroke="#2c3646"/>`
    +  `<line x1="${cx}" y1="${y0}" x2="${cx}" y2="${y1}" stroke="#2c3646"/>`
    +  `<text x="${x0+3}" y="${y0+11}" class="ql" fill="#6fd3a8">空頭平倉</text>`
    +  `<text x="${x1-3}" y="${y0+11}" class="ql qr" fill="#35d07f">多頭建倉</text>`
    +  `<text x="${x0+3}" y="${y1-4}" class="ql" fill="#e08a94">多頭平倉</text>`
    +  `<text x="${x1-3}" y="${y1-4}" class="ql qr" fill="#ff5c6c">空頭建倉</text>`
    +  `<text x="3" y="${y0+9}" class="ax">+${my.toFixed(0)}</text>`
    +  `<text x="3" y="${y1}" class="ax">-${my.toFixed(0)}</text>`
    +  `<text x="${x0}" y="${Ht-6}" class="ax">-${mx.toFixed(0)}</text>`
    +  `<text x="${x1}" y="${Ht-6}" class="ax qr">+${mx.toFixed(0)}% OI</text>`;
  for(const r of rows){ if(hit(r)) continue;
    s += `<circle cx="${X(r.oi).toFixed(1)}" cy="${Y(r.px).toFixed(1)}" r="2" fill="#4a5568" opacity=".6"/>`; }
  // 命中的點全部畫，但**只給前 18 大標字**：85 個標籤會糊成一團看不懂（線上實測）
  const named = new Set(rows.filter(hit)
    .sort((a,b)=>Math.abs(b.oi)-Math.abs(a.oi)).slice(0,18).map(r=>r.inst));
  for(const r of rows){ if(!hit(r)) continue;
    const c=QCLR[r.q], px=X(r.oi), py=Y(r.px);
    s += `<circle cx="${px.toFixed(1)}" cy="${py.toFixed(1)}" r="${named.has(r.inst)?4:2.8}"`
      +  ` fill="${c}" style="cursor:pointer" onclick="openCard('${r.inst}')"/>`;
    if(named.has(r.inst)){
      // 靠右半邊的標籤改放在點的**左側**並右對齊，否則寬螢幕下會被切掉（實測 CA/AP/ST…）
      const right = px > (x0+x1)/2;
      s += `<text x="${(right?px-6:px+6).toFixed(1)}" y="${(py+3.5).toFixed(1)}"`
        +  ` class="pl${right?' qr':''}">${r.inst.replace('-USDT-SWAP','')}</text>`; } }
  return s + '</svg>';
}

// 官方「資金注入候選」：1H OI ≥ 4%、|價格| ≤ 3%（他們寫死的，不跟著拉桿動）
// 官方流程：「先找出 1H 資金注入候選；觀察 15 分鐘後，以 OI 保留、相對 BTC 強弱與 CVD 判斷方向。」
function inflowCard(){
  const m=D.mkt, g=(m.gate||{}), rows=(m.rows||[]).filter(r=>r.inflow);
  if(m.win_h!==1) return '<div class="card"><h2>資金注入候選<span>官方只用 1H</span></h2>'
    + '<div class="empty">切到 1H 才看得到。</div></div>';
  return '<div class="card"><h2>◆ 資金注入候選'
    + `<span>1H OI ≥ ${(g.inflow_oi*100)||4}%、|價格| ≤ ${(g.inflow_px*100)||3}%</span></h2>`
    + (rows.length ? table('inf', ['幣','OI%','價%','OI/市值'], rows, r=>[
          r.inst.replace('-USDT-SWAP',''),
          {v:r.oi, h:pct(r.oi), c:'up'},
          {v:r.px, h:pct(r.px), c:cls(r.px)},
          {v:r.oimc||0, h:r.oimc?f(r.oimc*100,2)+'%':'—'},
        ]) : '<div class="empty">目前沒有：OI 進來 4% 以上、價格卻還壓在 3% 內的幣。</div>')
    + '<div class="sub" style="margin-top:8px">官方下一步：觀察 15 分鐘後，'
    + '看 OI 有沒有保留、相對 BTC 強弱、CVD 方向再判斷多空。</div></div>';
}

// ── 幣種字卡 + 直接開 APP 的按鈕 ────────────────────────────────────────────
// ★「先開 APP、沒裝才退回網頁」的手法抄自 datahunterx 官方前端（2026-09-24 抓到）：
//   設一個 1500ms 後開網頁的 timeout → 監聽 blur（APP 跳出來時頁面失焦）就取消它。
//   OKX 的 scheme 是官方驗證過的；TradingView 用通用 scheme；
//   CoinGlass 沒有公開 scheme，用 https（手機裝了 APP 會被 Universal Link 接走）。
function openApp(scheme, webUrl){
  if(/Android|iPhone|iPad|iPod/i.test(navigator.userAgent) && scheme){
    const t = setTimeout(()=>window.open(webUrl,'_blank'), 1500);
    window.addEventListener('blur', ()=>clearTimeout(t), {once:true});
    window.location.href = scheme;
  } else {
    window.open(webUrl,'_blank');
  }
}
const MOB = /Android|iPhone|iPad|iPod/i.test(navigator.userAgent);
// Universal Link（iOS）/ App Link（Android）要用 location.href 才會被 APP 接走；
// window.open 會開新分頁，常常就留在瀏覽器裡 —— 這就是「跳了 APP 卻沒跳到幣別」的原因。
function goUL(url){ if(MOB) location.href = url; else window.open(url,'_blank'); }

function goOKX(c){      // 下單頁（永續）。scheme 是官方 _okxOrder() 驗證過的
  openApp('okx://pro/trade/main/page?bizType=2&instId='+c+'-USDT-SWAP',
          'https://www.okx.com/trade-swap/'+c.toLowerCase()+'-usdt-swap'); }
function goTV(c){
  // ★試過三種都不對，這是第四種：
  //   ① scheme + window.open        → 開了 APP，沒帶符號
  //   ② Universal Link + location.href → PWA(standalone) 直接在 webview 開網頁，根本沒跳 APP
  //   ③ scheme + location.href      → 一樣開了 APP，沒帶符號（TV 的 scheme 參數格式查不到文件）
  //   ④ 本版：**Universal Link + window.open('_blank')** —— 在 PWA 裡這會跳出到 Safari，
  //      再由 Safari 去處理 Universal Link，才有機會帶著符號進 APP。
  window.open('https://www.tradingview.com/chart/?symbol='
              + encodeURIComponent('OKX:'+c+'USDT.P'), '_blank'); }
function goCG(c){
  // `coinglass://` 實測「無效的網址」→ APP 沒註冊該 scheme，iOS 只能走網頁。
  // ★網址帶 zh-TW 才是繁體中文（官方自己用的就是這個路徑）。
  // ★★時框與指標**無法用網址控制**：2026-09-24 實測 `?interval=15m` 送進去會被整個洗掉
  //   （location.href 回來沒有 query），設定全存在 localStorage
  //   （`tradingview.chartproperties.mainSeriesProperties.interval`，實測值 "60"）。
  //   → 正解是在同一個瀏覽器手動設一次 15m + 指標，之後每次從這裡點過去都會沿用。
  const web = 'https://www.coinglass.com/tv/zh-TW/Binance_'+c+'USDT';
  if(/Android/i.test(navigator.userAgent)){
    location.href = 'intent://www.coinglass.com/tv/zh-TW/Binance_'+c+'USDT'
      + '#Intent;scheme=https;package=com.coinglass.android;S.browser_fallback_url='
      + encodeURIComponent(web) + ';end';
  } else { window.open(web,'_blank'); } }

// 大數字縮寫（官方字卡寫 57.70B USDT / 1.70T 這種格式）
function big(v){
  if(v===null||v===undefined||isNaN(v)) return '—';
  const a=Math.abs(v);
  if(a>=1e12) return f(v/1e12,2)+'T';
  if(a>=1e9)  return f(v/1e9,2)+'B';
  if(a>=1e6)  return f(v/1e6,2)+'M';
  return f(v,0);
}
// 官方字卡有「相關幣種 → 📊 同樣<象限>」一區：同象限、OI 變化最大的其他幣
function relatedHTML(r){
  const qm=r.q24||r.q;
  const g=(D.mkt.rows||[]).filter(x=>(x.q24||x.q)===qm && x.inst!==r.inst && x.inq)
    .sort((a,b)=>Math.abs(b.oi)-Math.abs(a.oi)).slice(0,8);
  if(!g.length) return '';
  return `<div class="sub" style="margin-top:12px">📊 同樣「${qm}」的幣</div>`
    + '<div class="rel">' + g.map(x=>{
        const c=x.inst.replace('-USDT-SWAP','');
        return `<span class="rl" onclick="openCard('${x.inst}')">${c}`
          + ` <b class="${cls(x.oi)}">${pct(x.oi)}</b></span>`;
      }).join('') + '</div>';
}

let CARD = null;
function openCard(inst){ CARD = inst; draw(); }
function closeCard(){ CARD = null; draw(); }
function cardHTML(){
  if(!CARD || !D.mkt) return '';
  const r = (D.mkt.rows||[]).find(x=>x.inst===CARD);
  if(!r) return '';
  const c = r.inst.replace('-USDT-SWAP','');
  // ★象限有兩套，不可以混：排名表用 24H 價格分組（對齊官方），散點圖用所選週期。
  //   之前字卡只顯示同週期那套 → 從排名點進來會看到「排名寫空頭建倉、字卡寫多頭建倉」
  //   （用戶 2026-09-24 回報）。現在主標題跟排名一致，另外把同週期那套也列出來。
  const qMain = r.q24 || r.q;
  const meta = (D.mkt.quads&&D.mkt.quads[qMain]) ? D.mkt.quads[qMain] : ['',''];
  const row=(k,v,cl='')=>`<div class="cr"><span>${k}</span><b class="${cl}">${v}</b></div>`;
  return '<div class="ovl" onclick="closeCard()"></div>'
    + `<div class="card cd" onclick="event.stopPropagation()">`
    + `<h2><span class="qh">${c}</span><span onclick="closeCard()" style="cursor:pointer">✕</span></h2>`
    + `<div class="cq" style="color:${QCLR[qMain]}">${qMain}　<small>${meta[0]}</small></div>`
    + `<div class="sub" style="margin:6px 0 10px">${meta[1]||''}</div>`
    // 欄位順序與名稱對齊官方字卡（2026-09-24 實際抓到的版面）
    + row('現價', pf(r.last))
    + row('24H 漲跌', pct(r.chg24h), cls(r.chg24h))
    + row(`動能 ${D.mkt.win_h}H`, pct(r.px), cls(r.px))
    + row(`OI 變化 ${D.mkt.win_h}H`, pct(r.oi), cls(r.oi))
    + row('未平倉量', r.oiu? big(r.oiu)+' USDT' : '—')
    + row('市值', r.mcap? big(r.mcap) : '—')
    + row('OI／市值比', r.oimc? f(r.oimc*100,2)+'%' : '—')
    + row('24H 成交額', r.vol? big(r.vol)+' USDT' : '—')
    + (r.q!==qMain ? row(`同 ${D.mkt.win_h}H 窗象限`,
          `<span style="color:${QCLR[r.q]}">${r.q}</span>`) : '')
    + row('資料來源', r.src||'OKX', 'dim')
    + relatedHTML(r)
    + (r.inflow? '<div class="sub" style="color:var(--warn);margin-top:8px">◆ 資金注入候選'
        + '（1H OI ≥ 4%、|價格| ≤ 3%）：官方下一步是觀察 15 分鐘後看 OI 有沒有保留、'
        + '相對 BTC 強弱與 CVD 方向。</div>' : '')
    + `<div class="btns">
         <button class="bt bo" onclick="goOKX('${c}')">OKX 下單</button>
         <button class="bt" onclick="goTV('${c}')">TradingView</button>
         <button class="bt" onclick="goCG('${c}')">CoinGlass</button>
       </div>`
    + '<div class="sub" style="margin-top:8px">手機會直接開 APP；沒安裝才退回網頁。</div>'
    + '</div>';
}

// 異常警報：官方分「看漲／看跌／觀察中」三區，卡片式
const ST = {RADAR:['觀察中','dim'], CONFIRMED:['確認','up'],
            WEAKENING:['轉弱','warn'], INVALIDATED:['失效','down']};
function viewAnom(){
  const rows = D.anom||[];
  const b = D.breadth||{};
  let h = '<div class="card"><h2>市場廣度<span>全市場 24H</span></h2>'
    + `<div class="q4">
         <div class="qc"><b class="down">${b.bear_pct!=null?b.bear_pct+'%':'—'}</b><span>偏空</span></div>
         <div class="qc"><b class="up">${b.up||0}</b><span>漲</span></div>
         <div class="qc"><b class="dim">${b.flat||0}</b><span>平</span></div>
         <div class="qc"><b class="down">${b.down||0}</b><span>跌</span></div>
       </div><div class="sub">共 ${b.n||0} 個合約。偏空% = 跌家數 ÷ 總數（對齊官方算法）。</div></div>`;
  if(!rows.length) return h + '<div class="card"><h2>異常警報</h2><div class="empty">'
    + '目前沒有異動（門檻：15 分鐘或 5 分鐘價格變動 ≥ 3%，官方實測門檻）。</div></div>';
  const sec = (title, f, note) => {
    const g = rows.filter(f);
    return `<div class="card"><h2>${title}<span>${g.length}</span></h2>`
      + (note?`<div class="sub" style="margin-bottom:8px">${note}</div>`:'')
      + (g.length ? table('an'+title, ['幣','階段','15m','OI15m','相對BTC','CVD','觸發'], g, r=>[
          {v:r.coin, h:`<a class="cl" onclick="openCard('${r.inst}')">${r.coin}</a>`},
          {v:r.status, h:`${r.bias_label}`, c:(ST[r.status]||['',''])[1]},
          {v:r.p15, h:f2(r.p15), c:cls(r.p15)},
          {v:r.oi15, h:f2(r.oi15), c:cls(r.oi15)},
          {v:r.rel_btc, h:f2(r.rel_btc), c:cls(r.rel_btc)},
          {v:r.cvd_dir, h:r.cvd_dir==null?'—':(r.cvd_dir>0?'買壓':(r.cvd_dir<0?'賣壓':'中性')),
           c:r.cvd_dir>0?'up':(r.cvd_dir<0?'down':'dim')},
          {v:r.trigger_count, h:r.trigger_count+' 次'},
        ]) : '<div class="empty">—</div>') + '</div>';
  };
  h += sec('看漲', r=>r.init_dir==='bull', '警報當下資料偏多');
  h += sec('看跌', r=>r.init_dir==='bear', '警報當下資料偏空');
  h += '<div class="card"><div class="sub">'
    + '方向判定照官方那句話的三項：<b>OI 保留</b>＋<b>相對 BTC 強弱</b>＋<b>CVD</b>，三項都成立才從'
    + '「觀察中」升級成「確認」；OI 還在但 CVD 翻向 → 降「轉弱」（官方統計：偏多確認 CVD 中位 +4.83、'
    + '偏空 −8.24、觀察中 ≈0、偏多轉弱 −1.80）。<br>'
    + '★差異：官方 CVD 來自幣安 taker（全市場快取），我用 OKX rubik 且<b>只對已觸發的幣</b>打'
    + '（每輪最多 6 個），所以同一時刻不一定每筆都有 CVD，也不會與官方數值相同。<br>'
    + '官方原話：「警報只代表這個幣正在異動，<b>不等於可以直接進場</b>」；建議槓桿 5 倍。<br>'
    + '另：官方統計純價格觸發有 73~78% 會停在「觀察中」，本來就多半不成方向。'
    + '</div></div>';
  return h;
}

// 型態名稱用中文（官方 kind 是英文代碼，畫面上看不懂）
const KIND = {
  SHORT_TRAP:  ['假跌破收回', ''],
  LONG_TRAP:   ['假突破收回', ''],
  ABSORPTION:  ['吸收背離', ''],   // 價格**未破**前低/前高 + CVD 創新極值
  EXHAUSTION:  ['衰竭背離', ''],   // 價格**破了** + CVD **未**創新極值
};
// 該幣在 OI 排名的象限（跟數據訊號並排顯示）
// ★用戶 2026-09-24 指出「數據訊號寫做多、但那個幣是空頭建倉」——
//   那不是矛盾，是**兩個不同維度**：型態在講進場方向，象限在描述持倉×價格狀態。
//   官方自己就寫「象限只描述持倉與價格，**不直接判定多空**」。並排顯示才不會各說各話。
function quadOf(inst){
  const m = ((D.mkt&&D.mkt.rows)||[]).find(x=>x.inst===inst);
  if(!m) return {v:'', h:'<span class="dim">—</span>'};
  const q = m.q24 || m.q;
  return {v:q, h:`<span style="color:${QCLR[q]}">${q}</span>`};
}
// 兩個錨點的 CVD：只顯示「升/降」比數值有用（數值量級各幣差很多）
function cvdCell(a, b){
  if(a===null||a===undefined||b===null||b===undefined)
    return {v:0, h:'<span class="dim">—</span>'};
  const up = b > a;
  return {v: up?1:-1, h: up?'升':'降', c: up?'up':'down'};
}

// 數據訊號（TRAP / ABSORPTION / EXHAUSTION）：規格見 _DHX_DATASIG_0924_SPEC.md
function viewDhx(){
  const rows = D.dhx||[];
  if(!rows.length) return '<div class="card"><h2>數據訊號<span>TRAP · 15m</span></h2>'
    + '<div class="empty">目前沒有成立的 TRAP。每 15 分鐘掃一次，'
    + '每輪只掃 OI 變化最大的 8 個幣（CVD 要逐幣翻頁，成本高）。</div></div>';
  return '<div class="card"><h2>數據訊號<span>TRAP · 15m · 假突破收回</span></h2>'
    + table('dhx', ['幣','型態','方向','象限','進場','停損','停損%','TP1','合約CVD','現貨CVD'], rows, r=>[
        {v:r.inst, h:`<a class="cl" onclick="openCard('${r.inst}')">`
          + r.inst.replace('-USDT-SWAP','')+'</a>'},
        {v:r.kind, h:(KIND[r.kind]||[r.kind,''])[0], c:(KIND[r.kind]||['',''])[1]},
        {v:r.bias, h:r.bias==='LONG'?'做多':'做空', c:r.bias==='LONG'?'up':'down'},
        quadOf(r.inst),
        pf(r.entry), pf(r.sl), f(r.sl_dist_pct,2)+'%', pf(r.tp1),
        cvdCell(r.fut_cvd_i1, r.fut_cvd_i2), cvdCell(r.spot_cvd_i1, r.spot_cvd_i2),
      ])
    + '<div class="sub" style="margin-top:8px">'
    + '<b>假跌破收回</b>（做多）：跌破前低又收回收盤價，且<b>合約 CVD 降、現貨 CVD 升</b>、OI 升。<br>'
    + '<b>假突破收回</b>（做空）：鏡像 —— 合約 CVD 升、現貨 CVD 降、OI 升。<br>'
    + '<b>吸收背離</b>（多空皆有）：做多＝低點<b>抬高</b>（沒破前低）但 <b>CVD 樞紐低點降低</b>'
    + '（官方原話：賣方砸盤但價格未破前低，買方限價單吸收賣壓）；做空為鏡像。'
    + '停損放 <b>pivot2 的價格</b>。<br>'
    + '★TRAP 的 CVD／OI 條件是官方硬條件（400 筆實測 100% 一致：做空 36/36 合約升、'
    + '0/36 現貨升；做多 0/60、59/60），沒有 CVD 就不發訊號。<br>'
    + '<b>衰竭背離</b>（多空皆有）：做多＝<b>砸破</b>前低但 CVD <b>未</b>創新低（空方力竭）；'
    + '做空＝突破前高但 CVD 未創新高（多方力竭）。<br>'
    + '★<b>吸收 vs 衰竭只差一件事</b>：價格<b>有沒有破</b>前低／前高。'
    + '沒破＝吸收（有人在吸），破了但 CVD 沒跟＝衰竭（推的人沒力了）。'
    + '以上四句都是官方 <code>cvd_signal</code> 的原文，不是我的解讀。<br>'
    + '★選幣層照官方 <code>volume_top100</code>（24h 成交額前 100）輪替掃描。<br>'
    + '<b>「方向」和「象限」會不一樣，那是正常的</b>：方向是這個<b>型態</b>要怎麼進場，'
    + '象限是這個幣<b>當下持倉×價格</b>的狀態。官方原話：「象限只描述持倉與價格，'
    + '<b>不直接判定多空</b>」。兩者不同時，代表型態是在跟當下的持倉結構對做（例如假跌破收回做多，'
    + '但持倉結構還在空頭建倉）——自己判斷要不要跟。'
    + '</div></div>';
}

function viewMkt(){
  const m=D.mkt, rows=m.rows||[];
  if(!rows.length) return noData(m);
  const hit = r => Math.abs(r.oi*100)>=OITH && Math.abs(r.px*100)<=PXTH;
  const sel = rows.filter(hit);
  return '<div class="card">' + winBar()
    + `<h2>視覺篩選器<span>${m.win_h}H・${rows.length} 個合約・命中 ${sel.length}</span></h2>`
    + scatter(rows)
    + `<div class="sl"><label>OI 變化 <b>≥ ${OITH}%</b></label>
         <input type="range" min="1" max="10" step="0.5" value="${OITH}"
                oninput="setTh('oi',this.value)"></div>`
    + `<div class="sl"><label>價格變化 <b>≤ ${PXTH}%</b></label>
         <input type="range" min="1" max="10" step="0.5" value="${PXTH}"
                oninput="setTh('px',this.value)"></div>`
    + `<div class="sub">官方固定值：象限圖 OI ≥ 1%、|價格| ≤ 5%。`
    + `<b>價格是上限</b> —— 找的是「OI 大動、價格還沒動」。</div>`
    + (sel.length ? table('sel', ['幣','OI%','價%','象限'], sel, r=>[
          {v:r.inst, h:`<a class="cl" onclick="openCard('${r.inst}')">`
            + (r.inflow?'<span class="star">◆</span>':'')+r.inst.replace('-USDT-SWAP','')+'</a>'},
          {v:r.oi, h:pct(r.oi), c:cls(r.oi)},
          {v:r.px, h:pct(r.px), c:cls(r.px)},
          {v:r.q, h:`<span style="color:${QCLR[r.q]}">${r.q}</span>`},
        ]) : '<div class="empty">沒有符合的幣：OI 要夠大、價格要夠靜。把 OI 門檻往左拉。</div>')
    + '</div>' + inflowCard();
}

// ── OI 異動排名：照官方分四組，組內依「變化金額」排 ────────────────────────
function viewRank(){
  const m=D.mkt, rows=m.rows||[];
  if(!rows.length) return noData(m);
  let h = '<div class="card">' + winBar()
    + `<h2>OI 異動排名<span>${m.win_h}H 持倉量變化・依變化金額排序</span></h2></div>`;
  const top20 = rows.slice(0,20);      // ★官方：全部一起排取前 20，再分四組
  for(const q of QUADS){
    const g = top20.filter(r=>(r.q24||r.q)===q);
    if(!g.length) continue;
    const meta = (m.quads&&m.quads[q]) ? m.quads[q] : ['',''];
    h += `<div class="card"><h2><span class="qh" style="color:${QCLR[q]}">${q}</span>`
      +  `<span title="${meta[1]}">${meta[0]}</span></h2>`
      + table('rk'+q, ['#','幣種','價格','OI變化','OI/市值','價24H'], g, r=>[
          {v:g.indexOf(r)+1, h:String(g.indexOf(r)+1), c:'dim'},
          {v:r.inst, h:`<a class="cl" onclick="openCard('${r.inst}')">`
            + (r.an===2?'<span class="star">★</span>':'')+r.inst.replace('-USDT-SWAP','')+'</a>'},
          pf(r.last),
          {v:r.oi, h:pct(r.oi), c:cls(r.oi)},
          {v:r.oimc||0, h:r.oimc?f(r.oimc*100,2)+'%':'—'},
          {v:r.chg24h, h:pct(r.chg24h), c:cls(r.chg24h)},
        ]) + '</div>';
  }
  return h;
}

function viewPos(){
  return '<div class="card"><h2>追蹤中的倉 <span title="用被動快照最新價估，非交易所回報">R≈估</span></h2>' +
    table('pos', ['幣','方向','R','進場','停損','出場','時框','開倉'], D.trades, t=>[
      (t.symbol||t.inst_id||'').replace('/USDT','').replace('-USDT-SWAP',''),
      {v:t.direction==='long'?'多':'空', c:t.direction==='long'?'up':'down'},
      {v:t.r===undefined?-999:t.r, h:t.r===undefined?'—':f(t.r), c:cls(t.r)},
      pf(t.entry_price), pf(t.current_sl),
      (t.exit_strategy||'—') + (t.tp1_hit?' ✓':''), t.tf_id||'—', ago(t.entry_ts),
    ]) + '</div>';
}

function viewOI(){
  const o=D.oi;
  const tbl = (id,rows)=> table(id, ['合約', o.window_h+'h OI%', 'OI (M)'], rows, r=>[
    r.inst.replace('-USDT-SWAP',''),
    {v:r.pct, h:pct(r.pct), c:cls(r.pct)},
    {v:r.oi, h:f(r.oi/1e6,1)},
  ]);
  return '<div class="card"><h2>OI 增幅 <span>'+o.tracked+' 個合約</span></h2>'
    + tbl('oiu', o.up) + '</div>'
    + '<div class="card"><h2>OI 降幅</h2>' + tbl('oid', o.down) + '</div>';
}

function viewCoins(){
  const rows=[];
  for(const [sym,tfs] of Object.entries(D.coins))
    for(const [tf,r] of Object.entries(tfs)) rows.push({sym,tf,...r});
  const q=(document.getElementById('cq')||{}).value||'';
  const flt = q ? rows.filter(r=>r.sym.toLowerCase().includes(q.toLowerCase())) : rows;
  return '<div class="card"><h2>掃描快照</h2>'
    + `<input class="f" id="cq" placeholder="篩選幣種…" value="${q}" oninput="draw();document.getElementById('cq').focus()">`
    + table('coins', ['幣','時框','價格','ATR%','ADX','通道','趨勢','更新'], flt, r=>[
        r.sym.replace('/USDT',''), r.tf, pf(r.px),
        {v:r.atrp, h:r.atrp===undefined?'—':f(r.atrp*100,2)},
        {v:r.adx, h:f(r.adx,1), c:r.adx>=25?'up':(r.adx<15?'dim':'')},
        r.vg||'—',
        {v:r.trend||'', h:r.trend==='bear'?'空':(r.trend==='bull'?'多':'—'),
         c:r.trend==='bear'?'down':(r.trend==='bull'?'up':'dim')},
        ago(r.ts),
      ]) + '</div>';
}

function viewDiag(){
  let h='';
  for(const [name,rows] of Object.entries(D.diag.groups)){
    h += '<div class="card"><h2>'+name.replace('_DIAG','')
       + `<span>Δ 距上次 ${D.diag.elapsed?D.diag.elapsed+'s':'首次'}</span></h2><div class="grid">` + rows.map(r=>
      `<div class="kv"><span class="dim">${r.k}</span><b>${r.v}${
        r.d?` <span class="${r.d>0?'warn':'dim'}">+${r.d}</span>`:''}</b></div>`).join('')
      + '</div></div>';
  }
  return h;
}

function viewSig(){
  return '<div class="card"><h2>最近訊號</h2>'
    + table('sig', ['幣','時框','方向','策略','價格','時間'], D.signals, s=>[
        (s.symbol||'').replace('/USDT',''), s.tf,
        {v:s.dir==='long'?'多':'空', c:s.dir==='long'?'up':'down'},
        s.strat||'—', pf(s.price), ago(s.ts),
      ]) + '</div>';
}

function viewSys(){
  const m=D.mode;
  const kv=(k,v,c='')=>`<div class="kv"><span class="dim">${k}</span><b class="${c}">${v}</b></div>`;
  let h='<div class="card"><h2>執行設定</h2><div class="grid">'
    + kv('風險/筆', (m.risk_pct*100).toFixed(1)+'%')
    + Object.entries(m.auto_trade).map(([k,v])=>kv(k, v?'開':'關', v?'up':'dim')).join('')
    + '</div></div>';
  h += '<div class="card"><h2>策略開關</h2><div class="grid">'
    + Object.entries(D.flags).map(([k,v])=>
        kv(k.replace(/_ENABLED$/,''), v?'開':'關', v?'up':'dim')).join('')
    + '</div></div>';
  return h;
}

const PAGE_VER = '20260924y';
async function tick(){
  try{
    const r = await fetch(API + '?w=' + W, {cache:'no-store'});
    if(r.ok){
      D = await r.json();
      // ★PWA 快取：伺服器已是新版但手機拿的是舊快照 → 自動重載一次（只做一次，避免無限迴圈）
      if(D.ver && D.ver !== PAGE_VER){
        let done=false; try{ done = sessionStorage.getItem('rl')===D.ver; }catch(e){}
        if(!done){ try{ sessionStorage.setItem('rl', D.ver); }catch(e){} location.reload(); return; }
      }
      draw();
    }
  }catch(e){}
}
tick(); setInterval(tick, 20000);
document.addEventListener('visibilitychange', ()=>{ if(!document.hidden) tick(); });
</script></body></html>
"""
