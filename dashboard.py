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
VER = "20261002whale"


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


def sig_snapshot():
    """★最近訊號也要落地（2026-09-27）。`_SIG` 原本純記憶體 → 每次 redeploy 清空，
    而儀表板的 🎯（你的策略有訊號）就是讀它 —— 不存的話每部署一次 🎯 就會空好幾小時。
    最多 `_MAX_SIG` 筆，檔案很小。永不拋例外。"""
    try:
        with _LOCK:
            return {k: dict(v) for k, v in _SIG.items()}
    except Exception:
        return {}


def sig_restore(data):
    """讀回最近訊號。舊存檔沒有這個鍵 → None → 直接忽略（相容）。永不拋例外。"""
    try:
        if not isinstance(data, dict):
            return
        with _LOCK:
            for k, v in data.items():
                if isinstance(v, dict) and "ts" in v:
                    _SIG[str(k)] = dict(v)
            if len(_SIG) > _MAX_SIG:
                for k in sorted(_SIG, key=lambda x: _SIG[x]["ts"])[:len(_SIG) - _MAX_SIG]:
                    _SIG.pop(k, None)
    except Exception:
        pass


def snapshot():
    """把掃描快照倒出來給 main.py 落地（redeploy 後「幣種」那頁才不會整個空白）。永不拋例外。"""
    try:
        with _LOCK:
            return {s: {tf: dict(r) for tf, r in d.items()} for s, d in _DASH.items()}
    except Exception:
        return {}


def restore(data):
    """讀回掃描快照。舊版存檔沒有這個鍵 → 傳進來是 None，直接忽略。永不拋例外。"""
    try:
        if not isinstance(data, dict):
            return
        with _LOCK:
            for sym, tfs in data.items():
                if isinstance(tfs, dict):
                    _DASH.setdefault(str(sym), {}).update(
                        {str(tf): dict(r) for tf, r in tfs.items() if isinstance(r, dict)})
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


def _score(oi1, chg1, chg24, btc24, cvd, fr, fr_base, long_pct, liq, struct):
    """★官方 `scoreBreakdown(ticker)` 的忠實移植（規格＋原始碼：`_DHX_SCORE_0924_SPEC.md`／
    `_DHX_SCORE_SRC.js`，2026-09-24 從他們前端直接取下來的，不是逆推）。

    ★★順帶解掉「數據訊號和象限對不起來」：官方四象限的分組**有一層 `mktLabel` 覆寫**
      （`sigKey` 裡的 `m!=='主動做空'` / `m==='主動做多'` 那幾行）。我先前只用
      OI 方向 × 價格方向，對帳只命中 17/20 —— 差的 3 筆就是被這層覆寫的。
      所以這裡回傳的 `mkt_label` 要拿去覆寫象限，兩邊才會一致。

    參數單位：oi1/chg1/chg24/btc24 是**百分比數字**（+1.25 表示 +1.25%），
    fr 是資費百分比，long_pct 是多方帳戶佔比（54.18 表示 54.18%），
    fr_base=(median, mad, n)，liq=(long_liq, short_liq) 或 None，struct=(分數, 標籤)。
    """
    def _clamp(v, lo, hi):
        return max(lo, min(hi, v))

    mkt, label = 0, ""
    strong = oi1 is not None and abs(oi1) > 3
    if oi1 is not None and cvd is not None:
        oi_up, cvd_up = oi1 > 0, cvd > 0
        if cvd_up and oi_up and chg1 > 0:
            label, mkt = "主動做多", (40 if strong else 24)
        elif cvd_up and oi_up:
            label, mkt = "OI↑價↓", (-24 if strong else -12)
        elif (not cvd_up) and oi_up and chg1 < 0:
            label, mkt = "主動做空", (-40 if strong else -24)
        elif (not cvd_up) and oi_up:
            label, mkt = "OI↑價↑", (24 if strong else 12)
        elif cvd_up and not oi_up:
            label, mkt = "空頭出場", (16 if strong else 8)
        else:
            label, mkt = "多頭出場", (-16 if strong else -8)
    elif oi1 is not None:
        if oi1 > 3 and chg1 > 0:
            label, mkt = "OI↑價↑", 24
        elif oi1 > 3 and chg1 < 0:
            label, mkt = "OI↑價↓", -24
        elif oi1 > 0 and chg1 > 0:
            label, mkt = "OI↑價↑", 12
        elif oi1 > 0 and chg1 < 0:
            label, mkt = "OI↑價↓", -12
        elif oi1 < -3 and chg1 > 0:
            label, mkt = "OI↓價↑", 8
        elif oi1 < -3 and chg1 < 0:
            label, mkt = "OI↓價↓", -8
        elif oi1 < 0 and chg1 > 0:
            label, mkt = "OI↓價↑", 4
        else:
            label, mkt = "OI↓價↓", -4

    mom1 = round(_clamp(chg1 * 0.8, -8, 8))
    mom24 = round(_clamp(chg24 * 0.48, -4, 4)) if chg24 is not None else 0

    # 資費（逆向，±12）——「多方過熱」無條件扣分，「空方過熱」要嘎空已出現才給分（官方不對稱）
    fr_score, fr_label = 0, "正常"
    if fr is not None:
        med, mad, nb = fr_base or (0.0, 0.0, 0)
        has = nb >= 24
        general = max(0.01, mad * 2.5) if has else 0.01
        extreme = max(0.025, mad * 5) if has else 0.025
        dev = fr - (med if has else 0.0)
        oi_dn = (oi1 or 0) < 0
        oi_up2 = (oi1 or 0) > 0
        px_ok = (chg1 if chg1 is not None else 0) >= -0.2
        squeeze = (cvd or 0) > 0 and (oi_dn or px_ok or (oi_up2 and px_ok))
        if dev >= extreme:
            fr_score, fr_label = -12, "多方過熱"
        elif dev >= general:
            fr_score, fr_label = -6, "多方過熱"
        elif dev <= -extreme:
            fr_score = 8 if squeeze else 0
            fr_label = "空方過熱·嘎空確認" if squeeze else "空方過熱"
        elif dev <= -general:
            fr_score = 4 if squeeze else 0
            fr_label = "空方過熱·嘎空確認" if squeeze else "空方過熱"

    ls_score = 0
    if long_pct is not None:
        if long_pct > 60:
            ls_score = -4
        elif long_pct > 55:
            ls_score = -2
        elif long_pct < 40:
            ls_score = 4
        elif long_pct < 45:
            ls_score = 2

    liq_score = 0
    if liq:
        ll, sl = liq
        if sl > 0 and sl > ll * 2:
            liq_score = 3
        elif sl > ll:
            liq_score = 2
        elif ll > 0 and ll > sl * 2:
            liq_score = -3
        elif ll > sl:
            liq_score = -2

    # BTC 相對強弱（±7）——官方：coin 不是 BTC，且 |BTC 24H| < 5 才套用
    rel_score, rel_chg = 0, 0.0
    if chg24 is not None and btc24 is not None and abs(btc24) < 5:
        rel_chg = round(chg24 - btc24, 2)
        rel_score = round(_clamp(rel_chg * 1.4, -7, 7))

    st_score, st_label = (struct or (0, ""))
    total = mkt + mom1 + mom24 + fr_score + ls_score + liq_score + rel_score + st_score

    crash = chg24 is not None and chg24 <= -20
    if crash:
        if mkt > 0:
            mkt, label = 0, "崩跌存疑"
            total = mkt + mom1 + mom24 + fr_score + ls_score + liq_score + rel_score + st_score
        floor = -20 - min(60, round((abs(chg24) - 20) * 0.8))
        if total > floor:
            total = floor

    return {
        "total": int(max(-100, min(100, round(total)))),
        "mkt": mkt, "mkt_label": label, "mom1": mom1, "mom24": mom24,
        "fr": fr_score, "fr_label": fr_label, "ls": ls_score, "liq": liq_score,
        "rel": rel_score, "rel_chg": rel_chg, "struct": st_score, "struct_label": st_label,
        "crash": bool(crash),
    }


def _market(G, win_h=1.0, top_n=300):
    """★四象限（OI 變化 × 價格變化）。兩邊都讀記憶體，零 API。

    OI ← `_oi_history`、價 ← `_PX_HISTORY`，**同一個時間窗**（都由 `_oi_sample_tick` 每 5 分鐘取樣）。
    官方只有 15m / 30m / 1H 三檔，排名預設 1H。窗的兩端都用 `_at` 線性內插，沒有相位問題。
    排序照官方：**依 OI 變化的「金額」**（|ΔOI USD|），不是百分比 —— 小幣百分比會灌水。
    """
    rows = []
    _mkt_err = None
    _use_agg = False      # ★先給預設：下面 try 外的 depth/keep_h/return 都依賴它
    try:
        # ★OI 改用**跨所聚合**（OKX+幣安+Bitget+Gate）。官方排名用的是 CoinGlass 跨所加總，
        #   只用 OKX 會差 12~45 倍、變化% 甚至方向相反（ZRO 我 +8.2% vs 官方 −0.32%）。
        #   聚合還沒累積起來（剛部署）就退回 OKX 歷史，不讓畫面整個空掉。
        _agg_all = G.get("_AGG_HISTORY") or {}
        _use_agg = len(_agg_all) >= 50
        oi_all = _agg_all if _use_agg else (G.get("_oi_history") or {})
        px_all = G.get("_PX_HISTORY") or {}
        snap = G.get("_TICKER_SNAP") or {}
        mcap = G.get("_MCAP") or {}
        # 市值兩家口徑不同（實測 24% 的幣差 >20%，CoinPaprika 系統性偏低）→ 來源要跟著走，
        # 不然 OI／市值 這個「風險/擁擠度」指標會在不同幣之間不可比。
        mcsrc = G.get("_MCAP_SRC") or {}
        now = time.time()
        # ★★窗的兩端要用**同一個時間基準**。
        #   原本起點用 `now - win_h*3600` 內插、終點卻直接拿 `hist[-1]`（最後一筆取樣，
        #   最多可能是 DASH_SAMPLE_SEC 之前）→ 實際量到的是 **win_h 減掉取樣年齡**。
        #   2026-09-24 與官方逐幣對帳抓到：23 個共同幣，我的 OI 1H 中位比官方低 0.67pt、
        #   價格 1H 低 0.54pt —— 系統性偏小，不是雜訊。後果不只是數字小一點：
        #   `oiStrong=|OI|>3` 幾乎踩不到，一堆幣就掉進「OI>0 且 價>0 → OI↑價↑ +12」那格，
        #   分數整體被墊高（我 68% 為正、官方只有 21%）。
        #   改成以**最後一筆取樣的時間**為終點反推起點，窗長就真的是 win_h。
        _t_end = now
        try:
            _ends = [h[-1][0] for h in oi_all.values() if h]
            if _ends:
                _t_end = max(_ends)      # 取樣是全市場同一輪寫的，取最大即該輪的時間
        except Exception:
            pass
        target = _t_end - win_h * 3600
        target1 = _t_end - 3600.0       # 評分固定用 1H 窗（見下方 _score 呼叫處的說明）
        # 資費／多空比／CVD：由 bot 在背景取樣好的（原則 2：開網頁不打交易所 API）
        _raw = G.get("_BN_EXTRA") or {}
        _base_fn = G.get("_bn_fund_base")
        extra = {}
        for _k, _v in _raw.items():
            _f = _v.get("funding")
            _e = dict(_v)
            # 幣安 lastFundingRate 是**小數**（0.00003426），官方公式用的是**百分比** → ×100
            _e["funding_pct"] = (_f * 100.0) if _f is not None else None
            if _base_fn:
                try:
                    _m, _d, _n = _base_fn(_k)
                    _e["fr_base"] = (_m * 100.0, _d * 100.0, _n)
                except Exception:
                    _e["fr_base"] = None
            extra[_k] = _e
        # 結構分：掃描迴圈在 1H 那輪算好放進 _DASH 的（同樣不多打 API）
        dash_sn = {}
        try:
            with _LOCK:                       # _DASH 就是本模組的全域，掃描執行緒會寫它
                _items = [(s, dict(t.get("1H") or {})) for s, t in _DASH.items()]
            for _sym, _r1 in _items:
                if "struct" in _r1:
                    dash_sn[_sym.replace("/USDT", "") + "-USDT-SWAP"] = _r1
        except Exception:
            pass
        _bs = snap.get("BTC-USDT-SWAP") or {}
        btc24 = (_bs.get("chg24h") * 100.0) if _bs.get("chg24h") is not None else None
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
            # 用了聚合就**不可以**再混幣安平均——幣安已經是聚合的一員，會重複計算。
            src = ("4所聚合" if _use_agg else "OKX")
            bh = None if _use_agg else (G.get("_BN_HISTORY") or {}).get(inst)
            # ★★幣安是**輪流取樣**的（每輪只打 DASH_BN_TOP_N 個幣，因為它沒有全市場 OI
            #   的批量端點）。所以「這個幣有幣安歷史」不等於「它這一輪有被更新」——
            #   輪出去的幣，`bh[-1]` 會停在幾十分鐘前，拿它當「現在」算出來的**根本不是 1H 變化**，
            #   而且會被平均進 OI 變化%，安靜地污染排名。OKX 那一腳沒這個問題（每輪全市場都取）。
            #   → 幣安腳必須自己檢查新鮮度，不新鮮就退回「只用 OKX」。
            if bh and len(bh) >= 2 and (now - bh[-1][0]) <= max_gap:
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
            # 另 3 筆是被官方評分系統的 mktLabel 覆寫 —— ★2026-09-24 已把那套評分抓下來並移植
            # （見下方 _score 與 mktLabel 覆寫），所以這裡先算「純方向」版，稍後被覆寫。
            c24 = s.get("chg24h")
            quad24 = quad if c24 is None else (
                ("多頭建倉" if c24 >= 0 else "空頭建倉") if oi_pct >= 0 else
                ("空頭平倉" if c24 >= 0 else "多頭平倉"))
            _coin = inst.replace("-USDT-SWAP", "")
            mc = mcap.get(_coin)
            # ★評分一律用 **1H**（官方 scoreBreakdown 就是 1H），跟使用者選的窗無關 ——
            #   不然切到 12H 窗時分數會跟官方對不上，而且卡片上的「市場結構」會跟著窗漂。
            if abs(win_h - 1.0) < 1e-9:
                oi1, px1 = oi_pct, px_pct
            else:
                _b1 = _at(hist, target1, max_gap)
                _p1 = _at(ph, target1, max_gap) if len(ph) >= 2 else None
                oi1 = ((l_v - _b1[1]) / _b1[1]) if (_b1 and _b1[1] > 0) else None
                # 同上：幣安腳輪流取樣，不新鮮就不准混進來（見上面 src 那段的說明）
                if oi1 is not None and bh and len(bh) >= 2 and (now - bh[-1][0]) <= max_gap:
                    _bb1 = _at(bh, target1, max_gap)
                    if _bb1 and _bb1[1] > 0:
                        oi1 = (oi1 + (bh[-1][1] - _bb1[1]) / _bb1[1]) / 2.0
                px1 = ((ph[-1][1] - _p1[1]) / _p1[1]) if (_p1 and _p1[1] > 0) else None
            ex = extra.get(inst) or {}
            # 資費優先用 OI 加權跨所平均（對齊官方 avg_funding_rate_by_oi），沒有才退回幣安
            _fr_agg = (G.get("_FR_AGG") or {}).get(inst)
            if _fr_agg is not None:
                ex = dict(ex, funding_pct=_fr_agg)
            _stv = (dash_sn.get(inst) or {})
            sc = _score(
                oi1 * 100 if oi1 is not None else None,
                px1 * 100 if px1 is not None else 0.0,
                (c24 * 100) if c24 is not None else None,
                btc24,
                ex.get("cvd_ratio"),
                ex.get("funding_pct"),
                ex.get("fr_base"),
                ex.get("long_pct"),
                None,                       # 爆倉：沒有免費來源 → 官方 ±3 這項我們一律 0
                (_stv.get("struct") or 0, _stv.get("struct_label") or ""),
            )
            # ★官方 `sigKey` 的 mktLabel 覆寫層（這就是先前對帳只中 17/20 的那 3 筆）
            _m = sc["mkt_label"]
            if _m == "主動做多":
                quad24 = "多頭建倉"
            elif _m == "主動做空":
                quad24 = "空頭建倉"
            elif _m == "空頭出場":
                quad24 = "空頭平倉"
            elif _m == "多頭出場":
                quad24 = "多頭平倉"
            # ★同一套公式、只換 `chg` 的時間尺度，兩個都給（回測實測兩者性質不同）：
            #   `sc`   用真 1H —— 反應快，但相鄰小時翻轉 49.7%（≈丟銅板），方向性較弱
            #   `sc24` 用 24H  —— 遲鈍，但翻轉只有 37.8%、往後 24H 的多−空價差好 5 倍
            #   （n=308,069／59 幣／2026 全年，分季 A 49.5%~50.3%、B 37.6%~38.0% 穩定；
            #     24H 前瞻價差 A +0.027 vs B +0.138，B 在 3/4 季較佳。腳本 _an_score_horizon.py）
            #   ★官方其實是混的：CoinGlass 有的幣用 1H、沒有的退回 24H，實測他們 281 支裡
            #     **223 支走 24H fallback** —— 所以他們整頁偏空主要是這個，不是判斷比較準。
            #   ★兩者量級都很小（最好的 +0.138% vs 往返成本 0.1%）→ 這是掃描器不是訊號源。
            sc24 = _score(
                oi1 * 100 if oi1 is not None else None,
                (c24 * 100) if c24 is not None else 0.0,
                (c24 * 100) if c24 is not None else None,
                btc24, ex.get("cvd_ratio"), ex.get("funding_pct"), ex.get("fr_base"),
                ex.get("long_pct"), None,
                (_stv.get("struct") or 0, _stv.get("struct_label") or ""),
            )
            rows.append({
                "sc": sc, "sc24": sc24, "oi1": oi1, "px1": px1,
                "fr": ex.get("funding_pct"), "lp": ex.get("long_pct"),
                "cvd": ex.get("cvd_ratio"),
                "inst": inst, "oi": oi_pct, "d_usd": d_usd, "px": px_pct, "q": quad,
                "oiu": l_v, "last": s.get("last"), "chg24h": s.get("chg24h"),
                "vol": s.get("volccy_usd"), "oimc": (l_v / mc) if mc else None, "mcap": mc, "mcs": mcsrc.get(_coin),
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
    except Exception as e:
        # ★不可以 `pass`：這一段出例外時 rows 會變空，頁面只顯示「累積中」，
        #   看起來跟「資料還不夠」一模一樣 —— 2026-09-24 就這樣白查了半小時
        #   （OI 排名有 30 筆、depth 760 分鐘，唯獨篩選器 0 筆 = 這裡在爆而不是沒資料）。
        #   把錯誤帶回 payload + 印進 log，下次一眼就看得到。
        _mkt_err = f"{type(e).__name__}: {e}"
        try:
            import traceback
            print("[DASH] _market 例外(不影響交易): "
                  + traceback.format_exc()[-1200:], flush=True)
        except Exception:
            pass
        rows = []
    # 還要等多久：拿「OI 與價格都有」的幣裡最深的那份歷史當進度。
    # 空白畫面要講得出「還差幾分鐘」，不然使用者只會看到一片空，以為壞了。
    depth = 0.0
    try:
        # ★★要用市場視圖**實際吃的那份** OI 算深度（2026-09-27）。
        #   原本固定讀 `_oi_history` —— 那份是交易用、刻意只留 13h（見 main.py `DASH_HIST_KEEP_H`），
        #   而 4 所聚合開著時市場視圖吃的是 `_AGG_HISTORY`（留 25h）。
        #   讀錯份的後果：24H 窗明明已經有資料，頁面還是永遠顯示「還要約 660 分鐘」。
        oi_all = (G.get("_AGG_HISTORY") if _use_agg else G.get("_oi_history")) or {}
        px_all = G.get("_PX_HISTORY") or {}
        nowt = time.time()
        for inst, h in oi_all.items():
            ph = px_all.get(inst)
            if h and ph:
                depth = max(depth, nowt - max(h[0][0], ph[0][0]))
    except Exception:
        pass
    # ★短線廣度：同一批幣在**這個窗**內漲的家數。
    #   為什麼要它：大盤 24H 可能 91% 偏空，但最近 1H 是全面反彈（2026-09-24 實況），
    #   於是「OI 排名」(用 24H 價格) 一片偏空、「視覺篩選器/評分」(全用 1H) 一片偏多 ——
    #   兩個都對，只是量的東西不一樣。不把這個對照擺在畫面上，使用者只會覺得數字自相矛盾
    #   （用戶原話：「大部份都偏空 你一堆主力建倉是正常的嗎」）。
    _up_w = sum(1 for r in rows if (r.get("px") or 0) > 0)
    _bn = sum(1 for r in rows if r.get("src") in ("OKX+BN", "4所聚合"))
    return {"win_h": win_h, "err": _mkt_err, "quads": {k: list(v) for k, v in _QUAD.items()},
            "src": (("4所聚合 " if _use_agg else "OKX+BN ") + str(_bn)) if _bn else "OKX",
            "agg": bool(_use_agg), "agg_ex": G.get("AGG_EXCHANGES") or "",
            "gate": {"oi_min": QUAD_OI_MIN, "px_max": QUAD_PX_MAX,
                     "inflow_oi": INFLOW_OI_MIN, "inflow_px": INFLOW_PX_MAX},
            "tracked": len(G.get("_oi_history") or {}),
            "priced": len(G.get("_TICKER_SNAP") or {}),
            "sample": dict(G.get("_DASH_SAMPLE") or {}),
            "up_w": _up_w, "n_w": len(rows),
            "depth_min": int(depth / 60),
            "eta_min": max(0, int((win_h * 3600 - depth) / 60)),
            # ★保留期上限：窗比它長就**永遠**等不到，前端要改說實話，不能給一個跑不完的倒數。
            #   聚合開著 → 儀表板保留期；沒開 → 退回交易那份（13h）。
            "keep_h": (G.get("DASH_HIST_KEEP_H") or 25) if _use_agg
                      else ((G.get("OI_MOVERS_WINDOW_H") or 12) + 1),
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


_DHXEV_FIELDS = ("inst", "kind", "bias", "entry", "sl", "tp1", "sl_dist_pct", "ts", "status",
                 "r", "exit_ts", "oi_delta_pct", "swing_amp_pct", "engulf_rng_pct", "tp", "tp_r")


def _dhx_events(G):
    """數據訊號事件池 → 前端列表（近 24h、新到舊）。永不拋例外。"""
    try:
        snap = G.get("_TICKER_SNAP") or {}
        now = time.time()
        out = []
        for e in list((G.get("_DHX_EVENTS") or {}).values()):
            if now - float(e.get("ts") or 0) > 24 * 3600:
                continue
            r = {k: e.get(k) for k in _DHXEV_FIELDS}
            r["last"] = (snap.get(e.get("inst")) or {}).get("last")
            out.append(r)
        out.sort(key=lambda r: r.get("ts") or 0, reverse=True)
        return out[:200]
    except Exception:
        return []


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
        # 數據訊號事件（官方式 24h 列表）＋當下價格（算浮動 R 用；顯示層只讀 bot 已有的 tickers）
        "dhxev": _dhx_events(G),
        # 掃描統計：涵蓋幣數／幣安沒有的幾個／**過閘後真實筆數**（未被顯示上限截斷）
        "dhxq": {k: (G.get("_DHX_STATE") or {}).get(k)
                 for k in ("i", "miss", "raw", "n", "ms")},
        "whale": sorted((G.get("_WHALE") or {}).values(),
                        key=lambda r: r.get("first_ts") or 0, reverse=True)[:40],
        "whaleq": {k: (G.get("_WHALE_STATE") or {}).get(k)
                   for k in ("raw", "formal", "src", "err", "ms")},
        "breadth": _breadth(G),
        "anom": sorted((G.get("_ANOM") or {}).values(),
                       key=lambda r: r.get("last_ts") or 0, reverse=True)[:40],
        "diag": _diags(G),
        "flags": _flags(G),
        "coins": coins,
        "signals": sigs,
        # ★翻倉紙上前推（2026-09-30）：只讀 fanpan 已算好的值，fanpan.dash_payload 自己不拋例外
        "fp": _fp(G),
    }


def _fp(G):
    try:
        m = G.get("fanpan")
        return m.dash_payload() if m else {"ok": False, "err": "模組未載入"}
    except Exception as e:
        return {"ok": False, "err": type(e).__name__}


# ── 路由 ────────────────────────────────────────────────────────────────────
def register(app, G):
    """在 main.py 的 Flask app 上掛載。G = main.py 的 globals()（讀到的永遠是當下值）。"""
    from flask import jsonify, make_response, request

    # ★手機推播（2026-09-30）：金鑰/訂閱存持久磁碟；背景每 60 秒比對各分頁有沒有新東西。失敗不影響網頁。
    try:
        import webpush_notify as _wp
        _wp.start(G, collect, G.get("_PERSIST_DIR") or ".")
    except Exception as _e:
        _wp = None
        print(f"[推播] 沒啟動：{_e}", flush=True)

    @app.route("/d/<tok>")
    def _dash_page(tok):
        if not _token_ok(tok):
            return "", 404
        # service worker 與 manifest 掛在同一個路徑的 ?sw=1 / ?m=1：
        #   腳本在 /d/ 目錄下 → 預設 scope＝/d/，蓋得到 /d/<tok>；不必另外開放 scope。
        if request.args.get("sw"):
            r = make_response(_SW_JS)
            r.headers["Content-Type"] = "application/javascript; charset=utf-8"
            r.headers["Cache-Control"] = "no-store"
            return r
        if request.args.get("m"):
            r = jsonify({"name": "盤面", "short_name": "盤面", "start_url": f"/d/{tok}", "scope": "/d/",
                         "display": "standalone", "background_color": "#0b0e14", "theme_color": "#0b0e14"})
            r.headers["Content-Type"] = "application/manifest+json"
            return r
        r = make_response(_HTML.replace("__VER__", VER))
        r.headers["Content-Type"] = "text/html; charset=utf-8"
        r.headers["X-Robots-Tag"] = "noindex, nofollow"
        r.headers["Cache-Control"] = "no-store"
        return r

    @app.route("/d/<tok>/push", methods=["GET", "POST"])
    def _dash_push(tok):
        if not _token_ok(tok):
            return "", 404
        try:
            if _wp is None:
                return jsonify({"ok": False, "err": "推播模組沒啟動"})
            if request.method == "GET":
                return jsonify({"ok": True, "key": _wp.public_key(), "status": _wp.status(),
                                "topics": {k: {"name": v[0], "desc": v[1]} for k, v in _wp.TOPICS.items()}})
            b = request.get_json(silent=True) or {}
            op = b.get("op")
            if op == "sub":
                return jsonify({"ok": _wp.upsert(b.get("sub") or {}, b.get("prefs") or {})})
            if op == "unsub":
                _wp.remove((b.get("sub") or {}).get("endpoint")); return jsonify({"ok": True})
            if op == "test":
                ep = (b.get("sub") or {}).get("endpoint")
                n = _wp.push("test", "🔔 盤面通知測試", "收到這則就代表手機通知設定成功了", only_ep=ep)
                return jsonify({"ok": n > 0})
            return jsonify({"ok": False, "err": "未知操作"})
        except Exception as e:
            return jsonify({"ok": False, "err": type(e).__name__})

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

    @app.route("/d/<tok>/egress")
    def _dash_egress(tok):
        """★出口連通性探針：從 Railway 的出口 IP 打一輪候選網域，回狀態碼。

        為什麼要這支：幣安對雲端出口 IP 回 451，但**不同網域走不同邊緣節點**
        （fapi.binance.com 被封 ≠ www.binance.com 被封）。要找出還通的那條路，
        就得反覆試不同端點 —— 每試一個就推一次 bot 太慢（一次部署 1~2 分鐘，
        而且重啟代價是 OI 歷史歸零＋持倉重新接管）。掛成端點就能隨時重打。

        ★安全：候選清單**寫死在程式裡**，不吃任何 request 參數的網址 →
          它不會變成開放代理。也不轉發任何 header（全是公開端點，不需要金鑰）。
        """
        if not _token_ok(tok):
            return "", 404
        import urllib.request
        import urllib.error
        cands = [
            ("fapi 主網域",      "https://fapi.binance.com/fapi/v1/openInterest?symbol=BTCUSDT"),
            ("fapi1 備援",       "https://fapi1.binance.com/fapi/v1/openInterest?symbol=BTCUSDT"),
            ("www 前置 fapi",    "https://www.binance.com/fapi/v1/openInterest?symbol=BTCUSDT"),
            ("www 前置 OI歷史",  "https://www.binance.com/futures/data/openInterestHist"
                                 "?symbol=BTCUSDT&period=5m&limit=3"),
            ("data-api.vision",  "https://data-api.binance.vision/api/v3/ticker/price?symbol=BTCUSDT"),
            ("data-api fapi",    "https://data-api.binance.vision/fapi/v1/openInterest?symbol=BTCUSDT"),
            ("api 現貨主網域",   "https://api.binance.com/api/v3/ticker/price?symbol=BTCUSDT"),
            ("data.vision 檔案", "https://data.binance.vision/?delimiter=/&prefix=data/futures/um/daily/"),
            ("Bybit OI",         "https://api.bybit.com/v5/market/open-interest"
                                 "?category=linear&symbol=BTCUSDT&intervalTime=5min&limit=3"),
            ("Bitget OI",        "https://api.bitget.com/api/v2/mix/market/open-interest"
                                 "?symbol=BTCUSDT&productType=usdt-futures"),
            # ★跨所 OI 聚合可行性：官方的 OI 是 CoinGlass 跨所加總，我只有 OKX，
            #   同一幣金額差 12~45 倍。這四家都有**批量**端點（一支回全市場），
            #   所以只要 Railway 打得通就能自己聚合。本機實測：
            #   Bybit 891 支 0.4s／Bitget 805 支 0.4s／Gate 1013 支 1.1s／OKX 492 支 0.3s。
            ("Bybit 批量tickers", "https://api.bybit.com/v5/market/tickers?category=linear"),
            ("Bitget 批量tickers", "https://api.bitget.com/api/v2/mix/market/tickers"
                                   "?productType=usdt-futures"),
            ("Gate 批量contracts", "https://api.gateio.ws/api/v4/futures/usdt/contracts"),
            # Bybit 的 api.bybit.com 在 Railway 被 CloudFront 403。幣安的教訓是
            # **封鎖按網域**（fapi 擋、www 通），所以 Bybit 的備援網域也要各試一次。
            ("Bybit bytick",     "https://api.bytick.com/v5/market/tickers?category=linear"),
            ("Bybit .nl",        "https://api.bybit.nl/v5/market/tickers?category=linear"),
        ]
        out = []
        for name, url in cands:
            row = {"name": name, "host": url.split("/")[2]}
            try:
                req = urllib.request.Request(
                    url, headers={"User-Agent": "Mozilla/5.0", "accept": "application/json"})
                with urllib.request.urlopen(req, timeout=6) as resp:
                    row["code"] = resp.status
                    row["body"] = resp.read(160).decode("utf-8", "replace")[:160]
            except urllib.error.HTTPError as e:
                row["code"] = e.code
                try:
                    row["body"] = e.read(160).decode("utf-8", "replace")[:160]
                except Exception:
                    row["body"] = ""
            except Exception as e:
                row["code"] = None
                row["body"] = f"{type(e).__name__}: {e}"[:160]
            out.append(row)
        r = jsonify({"ver": VER, "probe": out})
        r.headers["X-Robots-Tag"] = "noindex, nofollow"
        r.headers["Cache-Control"] = "no-store"
        r.headers["Access-Control-Allow-Origin"] = "*"
        return r

    return app


# ★手機推播的 service worker（2026-09-30）：收到推播就跳通知；點通知打開儀表板並切到對應分頁（#tab=xxx）。
_SW_JS = """
self.addEventListener('install', e => self.skipWaiting());
self.addEventListener('activate', e => e.waitUntil(self.clients.claim()));
self.addEventListener('push', e => {
  let d = {};
  try { d = e.data ? e.data.json() : {}; } catch (_) { d = {title: '盤面', body: e.data ? e.data.text() : ''}; }
  e.waitUntil(self.registration.showNotification(d.title || '盤面', {
    body: d.body || '', tag: d.tag || undefined, data: {tab: d.tab || ''}}));
});
self.addEventListener('notificationclick', e => {
  e.notification.close();
  const tab = (e.notification.data || {}).tab || '';
  e.waitUntil(self.clients.matchAll({type: 'window', includeUncontrolled: true}).then(ws => {
    for (const w of ws) { if (w.url.indexOf('/d/') >= 0) {
      try { w.postMessage({tab}); } catch (_) {}
      return w.focus(); } }
    // 自己的網址＝/d/<token>?sw=1 → 路徑就是儀表板（scope 只有 /d/，直接開會 404）
    return self.clients.openWindow(self.location.pathname + (tab ? '#tab=' + tab : ''));
  }));
});
"""

_HTML = """<!doctype html>
<html lang="zh-Hant"><head>
<meta charset="utf-8">
<meta name="viewport" content="width=device-width, initial-scale=1, viewport-fit=cover">
<meta name="robots" content="noindex, nofollow">
<meta name="apple-mobile-web-app-capable" content="yes">
<meta name="apple-mobile-web-app-status-bar-style" content="black-translucent">
<meta name="theme-color" content="#0b0e14">
<link rel="manifest" href="?m=1">
<title>盤面</title>
<style>
  .sps{display:flex;flex-wrap:wrap;gap:6px;margin:4px 0 10px}
  .sp{background:var(--card);border:1px solid var(--line);border-radius:6px;
      padding:3px 7px;font-size:12px;display:flex;gap:5px;align-items:baseline}
  .sp i{font-style:normal;color:var(--dim)}
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
  /* ★桌機上不要讓表格橫跨整個螢幕：欄位會被拉到兩端、要左右掃視才讀得完。
     手機寬度本來就小於這個上限，完全不受影響。 */
  table{border-collapse:collapse;width:100%;max-width:1040px;
        font-variant-numeric:tabular-nums;font-size:13px}
  /* 第二欄幾乎都是幣名，靠左才不會跟右邊的數字黏在一起 */
  th:nth-child(2),td:nth-child(2){text-align:left}
  th,td{text-align:right;padding:6px 8px;white-space:nowrap;border-bottom:1px solid var(--line)}
  th:first-child,td:first-child{text-align:left;position:sticky;left:0;background:var(--card)}
  th{color:var(--dim);font-weight:500;font-size:11px;cursor:pointer;user-select:none}
  th:hover{color:var(--fg)}
  tbody tr:last-child td{border-bottom:none}
  /* 固定欄寬 + 限制總寬：桌機不要把六欄拉開到螢幕兩端，手機照常橫向捲動 */
  table.fx{table-layout:fixed;max-width:560px}
  table.fx th:nth-child(2),table.fx td:nth-child(2){text-align:left}
  table.fx td,table.fx th{overflow:hidden;text-overflow:ellipsis}
  tr.sec td{text-align:left;background:#0f141c;font-size:11px;padding:7px 8px;
            border-bottom:1px solid var(--line);position:sticky;left:0}
  .scp{margin-left:6px;font-size:11px;font-variant-numeric:tabular-nums}
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
  /* ── 聚焦標記（🎯 你的策略／🔥 多頁共振）── */
  .tg{display:inline-block;font-size:11px;line-height:1;margin-right:3px;cursor:help}
  .fbar{display:flex;flex-wrap:wrap;gap:6px;align-items:center;padding:8px 10px;margin:0 0 10px;
        border:1px solid var(--line);border-radius:8px;background:var(--card,transparent);font-size:12px}
  .fbar .lab{color:var(--dim);margin-right:2px}
  .fchip{display:inline-flex;gap:3px;align-items:center;padding:2px 8px;border-radius:99px;
         border:1px solid var(--line);cursor:pointer;white-space:nowrap}
  .fchip.up{border-color:var(--up,#16a34a)} .fchip.down{border-color:var(--down,#dc2626)}
  .fbar .sep{width:1px;align-self:stretch;background:var(--line);margin:0 4px}
  .fbar .tgl{margin-left:auto;cursor:pointer;color:var(--dim);text-decoration:underline}
  .sech{font-size:13px;margin:12px 0 6px;font-weight:600}
  details.sub summary{cursor:pointer;color:var(--dim)}
  .note{color:var(--dim);font-size:11px;line-height:1.5;padding:6px 8px;margin:4px 0 8px;
        border-left:2px solid var(--warn,#c90);background:rgba(200,150,0,.07);border-radius:3px}
  .sub{color:var(--dim);font-size:11px}
  .fpbig{font-size:15px;padding:12px;margin:6px 0 10px;border-radius:10px;border:1px solid var(--line);line-height:1.7}
  .fpbig.up{border-color:var(--up);background:rgba(22,163,74,.08)} .fpbig.dim{color:var(--fg)}
  .fpsum{font-size:13px;line-height:1.8;margin:4px 0 8px}
  .nbar{display:flex;flex-wrap:wrap;gap:6px;align-items:center;margin:0 0 8px;font-size:12px}
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

const TABS = [['fp','翻倉'],['mkt','視覺篩選器'],['whale','巨鯨雷達'],['rank','OI 排名'],['anom','警報'],['dhx','數據訊號'],['pos','持倉'],['coins','幣種'],
              ['diag','漏斗'],['sig','訊號'],['sys','開關']];
// 官方四象限順序：左上 空頭平倉 / 右上 多頭建倉 / 左下 多頭平倉 / 右下 空頭建倉
const QUADS = ['多頭建倉','空頭平倉','空頭建倉','多頭平倉'];
const QCLR = {'多頭建倉':'var(--up)','空頭平倉':'#6fd3a8','空頭建倉':'var(--down)','多頭平倉':'#e08a94'};
// 官方只有 15m / 30m / 1H 三檔（原文：「15m／30m 僅觀察變化，不另產生卡片或通知」）
const WINS = [[0.25,'15m'],[0.5,'30m'],[1,'1H']];
let W = 1, OITH = 1, PXTH = 5;   // 預設＝官方象限圖條件：OI ≥ 1%、|價格| ≤ 5%
// ★ALLCOINS=false（預設）→ 視覺篩選器只看「OI 金額前 100」，跟官方同一種選法
//   （實測 CoinGlass 收錄是**依 OI 挑**的，不是依市值）。282 幣全列就是「看起來沒過濾」的主因。
let ALLCOINS = false;
// 數據訊號篩選（官方同款：做多/做空 × 吸收/衰竭；多一個「假突破」因為我們也發 TRAP）
// q:'A' = 只看「精選」（預設開）：吞噬 K 全幅 ≥0.8% 且 |OI 變化| ≥1%。來源見 dhxIsA()。
let DHXF = {dir:'', kind:'', q:'A'};
try{ const s=JSON.parse(localStorage.getItem('dash')||'{}');
     if(s.W && WINS.some(x=>x[0]===s.W)) W=s.W;      // 舊版存的 4/12 會被丟掉
     if(s.OITH) OITH=s.OITH; if(s.PXTH) PXTH=s.PXTH;
     if(s.ALLCOINS) ALLCOINS=true;
     if(s.DHXF && typeof s.DHXF==='object')
       DHXF={dir:s.DHXF.dir||'', kind:s.DHXF.kind||'', q:(s.DHXF.q===undefined?'A':s.DHXF.q)}; }catch(e){}
function save(){ try{ localStorage.setItem('dash',JSON.stringify({W,OITH,PXTH,ALLCOINS,DHXF})); }catch(e){} }
function toggleAll(){ ALLCOINS=!ALLCOINS; save(); draw(); }
function setW(w){ W=w; save(); tick(); }
function setTh(which,v){ v=parseFloat(v); if(which==='oi') OITH=v; else PXTH=v; save(); draw(); }

// ── 聚焦標記：🎯 你的策略／🔥 多頁共振（2026-09-27）──────────────────────────
// ★用 🎯 不用 ⭐：OI 排名已經用 ★ 表示「OI 變化落在全市場前 5%（異常）」、
//   視覺篩選器用 ◆ 表示「資金注入」，⭐ 跟 ★ 長得太像一定會搞混。
// ★為什麼只有這兩種、而且 🔥 標「未驗證」：前 5 頁**單獨或疊加都沒被證明能提高勝率**
//   （警報加在進場上是負貢獻 +0.246→+0.078；四象限 64 檢定只 3 格過；評分最好的 24H 版
//   前瞻價差 +0.138% 連往返手續費都快蓋不過）。官方自己也寫「觸發次數多不代表勝率高」。
//   做一個「這符號＝勝率高」等於騙人 → 只有 🎯（你的策略，有回測勝率背書）當主訊號。
// ★幣名統一：訊號是 'SOL/USDT'（可能帶 ':USDT'），其餘頁是 'SOL-USDT-SWAP'
//   → 一律切成純幣名再比。0827 追蹤池就是格式不一致、比對 100% 失敗的事故。
// ★不用正規式：這段 JS 是包在 **Python 字串**裡的，正規式裡「反斜線加斜線」那種跳脫
//   在 Python 是無效跳脫字元 → 現在只噴 SyntaxWarning，但 Python 已公告未來會升級成
//   SyntaxError → dashboard.py import 失敗 → **bot 啟動就掛**。改成逐字切，零反斜線。
//   （連這段註解都不能寫出那個字元組合 —— 註解也在同一個 Python 字串裡。）
function coinOf(s){
  let t = String(s||'');
  for(const ch of ['/', '-', ':']){ const i = t.indexOf(ch); if(i >= 0) t = t.slice(0, i); }
  return t.toUpperCase();
}
const STAR_HOURS = 24;    // 🎯 看最近 24 小時內的策略訊號（持倉則一律算）
// ★🔥 門檻 = 2：只計算**本身就有「亮／不亮」判定**的 4 頁（篩選器用官方資金注入條件、
//   巨鯨用已判定方向、警報、數據訊號）。OI 排名只有 −100~+100 的總分、**官方沒有強弱分級**，
//   自己訂一條「≥30 算亮」就是手冊記過最多次的「把連續量切成自己想的門檻」→ 不計入。
//   門檻用 2 不用 3 是實測的：當下 282 幣只有 10 幣被任一頁點名、2 幣被 2 頁點名、0 幣被 3 頁點名
//   （一個時間點的快照，樣本小）→ 定 3 等於永遠不亮。
const FIRE_MIN = 2;
let FOCUS = {};
function focusMap(){
  const m = {};
  const get = c => (m[c] = m[c] || {star:null, why:[], bull:new Set(), bear:new Set()});
  (D.trades||[]).forEach(t=>{                        // 🎯 持倉（已落地，重啟不掉）
    const f = get(coinOf(t.symbol||t.inst_id));
    f.star = (t.direction==='short') ? 'bear' : 'bull';
    f.why.push('持倉' + (t.exit_strategy ? '・'+t.exit_strategy : ''));
  });
  const cut = (D.now||0) - STAR_HOURS*3600;
  (D.signals||[]).forEach(s=>{                       // 🎯 最近 24h 策略訊號
    if((s.ts||0) < cut) return;
    const f = get(coinOf(s.symbol));
    if(!f.star) f.star = (s.dir==='short') ? 'bear' : 'bull';
    f.why.push((s.strat||'策略') + (s.tf ? ' '+s.tf : ''));
  });
  ((D.mkt||{}).rows||[]).forEach(x=>{                // 🔥 篩選器：官方資金注入條件
    if(!x.inflow) return;
    const d = x.q==='多頭建倉' ? 'bull' : (x.q==='空頭建倉' ? 'bear' : null);
    if(d) get(coinOf(x.inst))[d].add('篩選器');
  });
  // 巨鯨雷達頁照官方只列偏多（偏空不列）→ 標記也只算偏多，否則 🔥 會引用一個頁面上看不到的來源
  (D.whale||[]).forEach(x=>{ if(x.dir==='bull') get(coinOf(x.inst)).bull.add('巨鯨'); });
  (D.anom||[]).forEach(x=>{ const d = x.confirmed_dir||x.init_dir;
    if(d==='bull'||d==='bear') get(coinOf(x.inst))[d].add('警報'); });
  // 跟數據訊號頁一致：只算「入場訊號」段（持倉中）的事件，結單的不投票
  (D.dhxev||[]).forEach(x=>{ const d = {LONG:'bull',SHORT:'bear'}[x.bias];
    if(d && x.status==='持倉中' && dhxIsA(x)) get(coinOf(x.inst))[d].add('數據'); });  // 只算精選（跟數據訊號頁預設一致）
  return m;
}
function fireDir(f){ return !f ? null : (f.bull.size>=FIRE_MIN ? 'bull' : (f.bear.size>=FIRE_MIN ? 'bear' : null)); }
// 表格裡幣名前面的小標記
function tag(inst){
  const f = FOCUS[coinOf(inst)];
  if(!f) return '';
  let h = '';
  if(f.star) h += `<span class="tg" title="你的策略：${f.why.join('、')}">🎯</span>`;
  for(const d of ['bull','bear']) if(f[d].size >= FIRE_MIN)
    h += `<span class="tg" title="${[...f[d]].join('＋')} 同時${d==='bull'?'偏多':'偏空'}（未驗證勝率）">🔥</span>`;
  return h;
}
// 最上面的聚焦列：先看 🎯，🔥 當「值得看一眼」
function focusBar(){
  const star = [], fire = [];
  for(const [c,f] of Object.entries(FOCUS)){
    if(f.star) star.push([c,f]);
    else if(fireDir(f)) fire.push([c,f]);
  }
  const chip = (c,d,t) => `<span class="fchip ${d==='bull'?'up':'down'}" title="${t}"`
    + ` onclick="openCard('${c}-USDT-SWAP')">${c}<b class="${d==='bull'?'up':'down'}">${d==='bull'?'多':'空'}</b></span>`;
  let h = '<div class="fbar">';
  h += '<span class="lab">🎯 你的策略</span>'
     + (star.length ? star.map(([c,f])=>chip(c,f.star,f.why.join('、'))).join('')
                    : '<span class="dim">現在沒有（持倉＋近 24h 訊號）</span>');
  h += '<span class="sep"></span><span class="lab">🔥 ≥2 頁同向</span>'
     + (fire.length ? fire.map(([c,f])=>{ const d=fireDir(f); return chip(c,d,[...f[d]].join('＋')+'（未驗證勝率）'); }).join('')
                    : '<span class="dim">現在沒有</span>');
  h += `<span class="tgl" onclick="toggleAll()">${ALLCOINS ? '只看 OI 前 100' : '顯示全部幣'}</span>`;
  return h + '</div>';
}
// 視覺篩選器的幣池：預設 OI 金額前 100；🎯🔥 的幣一律保留（不能因為小就被藏掉）
function poolRows(rows){
  if(ALLCOINS) return rows;
  const top = new Set(rows.slice().sort((a,b)=>(b.oiu||0)-(a.oiu||0)).slice(0,100).map(r=>r.inst));
  return rows.filter(r => top.has(r.inst) || (FOCUS[coinOf(r.inst)] &&
    (FOCUS[coinOf(r.inst)].star || fireDir(FOCUS[coinOf(r.inst)]))));
}

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
  // ★標記要在任何 view 之前算好（tag()/poolRows() 都讀 FOCUS）；算壞了不可以讓整頁掛掉
  try{ FOCUS = focusMap(); }catch(e){ FOCUS = {}; }
  document.getElementById('card').innerHTML = cardHTML();
  let body = '';
  if(TAB==='mkt') body = viewMkt();
  if(TAB==='whale') body = viewWhale();
  if(TAB==='rank') body = viewRank();
  if(TAB==='dhx') body = viewDhx();
  if(TAB==='fp') body = viewFp();
  if(TAB==='anom') body = viewAnom();
  if(TAB==='pos') body = viewPos();
  if(TAB==='oi') body = viewOI();
  if(TAB==='coins') body = viewCoins();
  if(TAB==='diag') body = viewDiag();
  if(TAB==='sig') body = viewSig();
  if(TAB==='sys') body = viewSys();
  // 聚焦列只放在前 5 頁（看盤那幾頁）；持倉/幣種/系統頁不需要
  const FOCUS_TABS = ['mkt','whale','rank','anom','dhx'];
  let fb = '';
  if(FOCUS_TABS.indexOf(TAB) >= 0){ try{ fb = focusBar(); }catch(e){ fb = ''; } }
  let nb = ''; try{ nb = notiBar(); }catch(e){ nb = ''; }     // 每一頁都有自己的通知開關；壞了不能拖垮整頁
  v.innerHTML = nb + fb + body;
}

// ── ★手機推播（2026-09-30，用戶：「每頁都可以選擇開或不開，要像數據獵手手機也可以通知」）──
//   每支手機各自記住哪幾頁要通知（存伺服器，redeploy 不掉；本機也存一份給畫面用）。
const PUSHAPI = location.pathname.replace(/\\/$/,'') + '/push';
const NOTI = {fp:'有可以下的單、出結果、過 +2.2／−2.2', mkt:'某個幣剛出現 🔥', whale:'新的巨鯨訊號', rank:'新擠進 OI 增幅前 10',
  anom:'新警報、警報變成確認', dhx:'新的數據訊號', pos:'開倉、平倉', coins:'1H 結構轉向（上升↔下降）',
  diag:'策略觸發', sig:'bot 發出新的策略訊號', sys:'策略開關被改動'};
let PUSH = {reg:null, sub:null, perm:'default', err:'', prefs:{}};
try{ PUSH.prefs = JSON.parse(localStorage.getItem('pushprefs')||'{}') || {}; }catch(e){ PUSH.prefs = {}; }
function pushOK(){ try{ return ('serviceWorker' in navigator) && ('PushManager' in window) && (typeof Notification !== 'undefined'); }catch(e){ return false; } }
function isIOS(){ try{ return /iPhone|iPad|iPod/.test(navigator.userAgent||''); }catch(e){ return false; } }
function isStandalone(){ try{ return (window.matchMedia && window.matchMedia('(display-mode: standalone)').matches) || navigator.standalone===true; }catch(e){ return false; } }
function b64u(s){ const p='='.repeat((4-s.length%4)%4), b=atob((s+p).replace(/-/g,'+').replace(/_/g,'/'));
  const a=new Uint8Array(b.length); for(let i=0;i<b.length;i++) a[i]=b.charCodeAt(i); return a; }
async function pushInit(){
  if(!pushOK()) return;
  try{
    PUSH.perm = Notification.permission;
    PUSH.reg = await navigator.serviceWorker.register(location.pathname + '?sw=1');
    PUSH.sub = await PUSH.reg.pushManager.getSubscription();
    navigator.serviceWorker.addEventListener('message', e => { const t=(e.data||{}).tab; if(t){ TAB=t; draw(); } });
    if(PUSH.sub) pushSave();                        // 讓伺服器端的偏好跟這支手機同步
  }catch(e){ PUSH.err = String(e); }
  draw();
}
async function pushEnable(){
  try{
    const perm = await Notification.requestPermission(); PUSH.perm = perm;
    if(perm !== 'granted'){ draw(); return; }
    const k = await (await fetch(PUSHAPI, {cache:'no-store'})).json();
    if(!k.key){ PUSH.err = k.err || '伺服器沒有推播金鑰'; draw(); return; }
    const reg = PUSH.reg || await navigator.serviceWorker.register(location.pathname + '?sw=1'); PUSH.reg = reg;
    PUSH.sub = await reg.pushManager.subscribe({userVisibleOnly:true, applicationServerKey:b64u(k.key)});
    if(!Object.keys(PUSH.prefs).length) PUSH.prefs = {fp:true};     // 第一次開：先只開翻倉，其他頁自己選
    await pushSave();
  }catch(e){ PUSH.err = String(e); }
  draw();
}
async function pushSave(){
  try{ localStorage.setItem('pushprefs', JSON.stringify(PUSH.prefs)); }catch(e){}
  if(!PUSH.sub) return;
  try{ await fetch(PUSHAPI, {method:'POST', headers:{'Content-Type':'application/json'},
         body: JSON.stringify({op:'sub', sub: PUSH.sub.toJSON ? PUSH.sub.toJSON() : PUSH.sub, prefs: PUSH.prefs})}); }catch(e){}
}
function pushToggle(t){ PUSH.prefs[t] = !PUSH.prefs[t]; pushSave(); draw(); }
function pushAll(on){ Object.keys(NOTI).forEach(t => PUSH.prefs[t] = on); pushSave(); draw(); }
async function pushTest(){
  if(!PUSH.sub) return;
  try{ const r = await (await fetch(PUSHAPI, {method:'POST', headers:{'Content-Type':'application/json'},
         body: JSON.stringify({op:'test', sub: PUSH.sub.toJSON ? PUSH.sub.toJSON() : PUSH.sub})})).json();
       if(!r.ok) PUSH.err = '測試沒發出去（' + (r.err||'伺服器找不到這支手機的訂閱') + '）'; }catch(e){ PUSH.err = String(e); }
  draw();
}
function tabName(t){ const x = TABS.find(z => z[0]===t); return x ? x[1] : t; }
function notiBar(){
  if(!pushOK()) return '<div class="nbar sub">🔔 這個瀏覽器不能收推播'
    + (isIOS() && !isStandalone() ? '：iPhone 要先按「分享 → 加入主畫面」，再從主畫面的圖示打開這頁' : '') + '</div>';
  if(!PUSH.sub || PUSH.perm !== 'granted')
    return '<div class="nbar"><span class="wb" onclick="pushEnable()">🔔 開啟手機通知</span>'
      + (PUSH.perm==='denied' ? ' <span class="sub">通知被封鎖了，要到手機「設定 → 通知」把這個 App 打開</span>' : '')
      + (PUSH.err ? ' <span class="sub warn">'+PUSH.err+'</span>' : '') + '</div>';
  const on = !!PUSH.prefs[TAB];
  return `<div class="nbar"><span class="wb ${on?'on':''}" onclick="pushToggle('${TAB}')">🔔 「${tabName(TAB)}」通知：${on?'開':'關'}</span>`
    + ` <span class="sub">${NOTI[TAB]||''}</span> <span class="wb" onclick="TAB='sys';draw()">全部設定</span></div>`;
}
function notiPanel(){
  let h = '<div class="card"><h2>手機通知<span>每一頁各自開關</span></h2>';
  if(!pushOK() || !PUSH.sub || PUSH.perm !== 'granted') return h + notiBar() + '</div>';
  h += '<div class="wins"><span class="wb" onclick="pushAll(true)">全部開</span><span class="wb" onclick="pushAll(false)">全部關</span>'
     + '<span class="wb" onclick="pushTest()">發一則測試</span></div>';
  h += '<div class="grid">' + Object.keys(NOTI).map(t => {
      const on = !!PUSH.prefs[t];
      return `<div class="kv" onclick="pushToggle('${t}')" style="cursor:pointer"><span>${tabName(t)}<br><span class="sub">${NOTI[t]}</span></span><b class="${on?'up':'dim'}">${on?'開':'關'}</b></div>`;
    }).join('') + '</div>';
  return h + (PUSH.err ? '<div class="sub warn">'+PUSH.err+'</div>' : '') + '</div>';
}

// ★時間尺度對照：這一頁用的窗 vs 大盤 24H。兩者背離時（例如 24H 大跌、近 1H 反彈）
//   必須講出來，否則「排名一片偏空、篩選器一片偏多」看起來像自相矛盾。
function scaleBar(){
  const m=D.mkt, b=D.breadth||{};
  if(!m || !m.n_w) return '';
  const p1 = Math.round(m.up_w / m.n_w * 100);
  const p24 = 100 - (b.bear_pct===undefined ? 50 : b.bear_pct);
  const diverge = Math.abs(p1 - p24) >= 25;
  return `<div class="sub" style="margin-bottom:8px">`
    + `<b>${m.win_h}H 上漲 ${p1}%</b>（${m.up_w}/${m.n_w}）　·　`
    + `<b>24H 上漲 ${p24}%</b>（大盤廣度）`
    + (diverge ? `<span class="warn">　◆ 兩個尺度背離：這一頁的象限與分數算的是 `
        + `<b>${m.win_h}H</b>，${p1>p24?'短線反彈會讓「多頭建倉／主動做多」大量出現'
                                   :'短線回落會讓「空頭建倉／主動做空」大量出現'}，`
        + `不等於趨勢翻${p1>p24?'多':'空'}。OI 排名那頁用的是 24H 價格，所以看起來會相反。</span>` : '')
    + '</div>';
}

function winBar(){
  return '<div class="wins">' + WINS.map(([w,n])=>
    `<div class="wb ${W===w?'on':''}" onclick="setW(${w})">${n}</div>`).join('')
    + `<span class="src" title="官方 oi_chg_1h = OKX 與幣安的算術平均。這裡有補到幣安的幣會標 OKX+BN；
       幣安 fapi 若被 Railway 地理封鎖(451)就只剩 OKX。">${(D&&D.mkt&&D.mkt.src)||'OKX'}</span></div>`;
}
function noData(m){
  // ★窗比保留期長 = **永遠**等不到。2026-09-27 前就是這樣：保留期被寫死 13h，
  //   24H 窗卻一直顯示「還要約 660 分鐘」—— 一個永遠跑不完的倒數等於在騙人，改成直說。
  if(m.keep_h && m.win_h > m.keep_h - 0.5)
    return '<div class="card">' + winBar()
      + `<h2>${m.win_h}H 窗無法使用<span>保留期只有 ${m.keep_h} 小時</span></h2>`
      + `<div class="sub" style="margin-top:8px">目前只保留最近 ${m.keep_h} 小時的取樣，`
      + `這個窗需要 ${m.win_h} 小時，<b>等再久也不會出現</b>。`
      + (m.agg ? '' : '（跨所聚合還沒累積到 50 幣以上，暫時退回 OKX 單所歷史，那份只留 13 小時。）')
      + ' 先看較短的窗。</div></div>';
  const pctDone = Math.min(100, Math.round(m.depth_min/(m.win_h*60)*100)) || 0;
  return '<div class="card">' + winBar()
    + `<h2>${m.win_h}H 窗累積中<span>${m.depth_min} / ${m.win_h*60} 分鐘</span></h2>`
    + `<div class="bar"><i style="width:${pctDone}%"></i></div>`
    + `<div class="sub" style="margin-top:8px">`
    + `已取樣 ${m.depth_min} 分鐘，還要約 <b>${m.eta_min} 分鐘</b>`
    // ★取樣間隔：2026-09-24 改成獨立執行緒後實測 [328, 301] 秒 ≈ 5 分鐘（原文寫 15 分鐘已過期）
    + `（約每 5 分鐘取樣一次，追蹤 ${m.tracked} 個合約、報價 ${m.priced} 個）。`
    + (m.win_h > 1 ? ' 先看 1H 那格，它最快滿。' : '')
    // ★歷史已落地到 Railway volume（v5），重新部署不會歸零（原文寫「會歸零」已過期）
    + ' 取樣會存檔，重新部署不會歸零。</div></div>';
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
// ── 巨鯨雷達（＝資金注入候選 + 15 分鐘觀察狀態機）──────────────────────────
// ★跟「視覺篩選器」是兩個東西（用戶 2026-09-24 指正）：
//   篩選器＝象限散點圖，OI ≥1%、|價格| ≤5%，只描述狀態、不判多空，是**瀏覽工具**；
//   巨鯨雷達＝1H OI ≥4%、|價格| ≤3% 的候選，**會發卡片/通知**，而且有後續判方向的流程。
//   官方契約字串就寫得很白：`batch_title: "巨鯨雷達｜資金注入候選 {count} 個"`。
const WDIR = {bull:['偏多','up'], bear:['偏空','down'],
              pending:['觀察中','warn'], none:['方向未成立','dim']};

// ★官方卡片摘要用的片語表（`_whaleRadarSharedContract.notice_rules` 原樣，單位：%／分位）。
//   每列 [下界, 上界, 含下界, 含上界, 片語]；null = 無界。
const WHALE_PHRASE = {
  oi:   [[3,6,true,false,'持倉持續增加'],[6,10,true,false,'持倉明顯增加'],[10,null,true,false,'持倉大幅增加']],
  px:   [[-3,-1.5,true,true,'價格明顯回落'],[-1.5,-0.5,false,true,'價格小幅回落'],
         [-0.5,0.5,false,false,'價格仍在盤整'],[0.5,1.5,true,false,'價格小幅上漲'],[1.5,3,true,true,'價格開始上漲']],
  oi15: [[null,-1,false,false,'短線持倉明顯回落'],[-1,-0.3,true,false,'短線持倉小幅回落'],
         [-0.3,0.3,true,false,'短線持倉暫時平穩'],[0.3,1,true,false,'短線持倉小幅增加'],[1,null,true,false,'短線持倉持續增加']],
  vp:   [[0.8,0.9,true,false,'量能升溫'],[0.9,0.95,true,false,'量能明顯放大'],[0.95,1,true,true,'量能極度活躍']],
};
function whaleSummary(r){
  const vals = {oi: r.oi==null?null:r.oi*100, px: r.px==null?null:r.px*100,
                oi15: r.oi15==null?null:r.oi15*100, vp: r.vpct};
  const out = [];
  for(const k of ['oi','px','oi15','vp']){
    const v = vals[k]; if(v==null || !isFinite(v)) continue;
    for(const [lo,hi,il,ih,p] of WHALE_PHRASE[k]){
      if((lo===null || v>lo || (il && v===lo)) && (hi===null || v<hi || (ih && v===hi))){ out.push(p); break; }
    }
  }
  return out.join('｜') || '資金活動升溫';
}
function whaleSort(a,b){
  // ★官方 v2 排序（`candidateRows.sort`）：有量能分位的在前、分位高的在前，同分再比 1H OI
  const ar = a.vpct!=null && isFinite(a.vpct), br = b.vpct!=null && isFinite(b.vpct);
  if(ar!==br) return br - ar;
  if(ar && a.vpct!==b.vpct) return b.vpct - a.vpct;
  return (b.oi||0) - (a.oi||0);
}
function whaleRow(r){
  return [
    {v:r.inst, h:tag(r.inst) + `<a class="cl" onclick="openCard('${r.inst}')">`
      + r.inst.replace('-USDT-SWAP','') + '</a>'},
    {v:r.dir, h:(WDIR[r.dir]||[r.dir,''])[0]
      + (r.dir==='pending' ? `<span class="dim"> ${Math.max(0, Math.ceil((900-(D.now-r.first_ts))/60))}分</span>` : ''),
      c:(WDIR[r.dir]||['','dim'])[1]},
    {v:r.oi, h:pct(r.oi), c:'up'},
    {v:r.px, h:pct(r.px), c:cls(r.px)},
    {v:r.oi15==null?-99:r.oi15, h:r.oi15==null?'—':pct(r.oi15), c:cls(r.oi15||0)},
    {v:r.vpct==null?-1:r.vpct, h:r.vpct==null?'<span class="dim">補充中</span>':'P'+f(r.vpct*100,0)},
    {v:0, h:`<span class="dim" title="${r.note||''}">${whaleSummary(r)}</span>`},
    ago(r.first_ts),
  ];
}
function viewWhale(){
  const all = D.whale || [], q = D.whaleq || {};
  // ★官方 v2（2026-10-02 改版）：列表**只列後端判「偏多／觀察中」**的幣（`_whaleRadarBackendDecision`），
  //   量能分位只用來排序（拿不到的排最後）。官方的方向判定在後端、我們拿不到，
  //   這裡的方向是我訂的 → 我判「方向未成立／偏空」的**不藏**，放到下面一區（官方可能判偏多）。
  const cols = ['幣','狀態','OI 1H','價 1H','15m OI','量能','摘要','起算'];
  const rows = all.filter(r => r.dir==='bull' || r.dir==='pending').sort(whaleSort);
  const rest = all.filter(r => !(r.dir==='bull' || r.dir==='pending')).sort(whaleSort);
  let h = '<div class="card"><h2>◆ 巨鯨雷達｜資金注入候選 ' + rows.length + ' 個'
    + `<span>此刻候選 ${q.raw||0}・OI 來源 ${q.src||'—'}</span></h2>`
    + '<div class="sub" style="margin-bottom:8px">條件：<b>1H OI ≥3%、|1H 價格| ≤3%</b>'
    + '（持倉先行、價格還沒擴張）→ 觀察 15 分鐘 → 判方向。<b>量能只用來排序</b>（官方原話），'
    + '新幣量能還沒滿 25 天的顯示「補充中」排最後。</div>';
  h += rows.length ? table('whale', cols, rows, whaleRow)
                   : '<div class="empty">目前沒有偏多／觀察中的候選。</div>';
  if(rest.length){
    h += `<div class="sub" style="margin:10px 0 6px">我判方向未成立／偏空 ${rest.length} 個`
       + '（官方方向在後端算、我們拿不到，<b>官方可能判偏多</b>，所以不藏）：</div>'
       + table('whale2', cols, rest, whaleRow);
  }
  return h + '<div class="sub" style="margin-top:8px">'
    + '★來源：官方前端契約 v2「持倉先行且價格尚未擴張的事件候選；成交量分位只用於排序，不作硬性淘汰」。'
    + '3% 門檻是從契約片語下界＋對帳 Barry 10-01 直播畫面官方 13 幣（3% 抓到 13/13、4% 只有 8/13）推回來的。'
    + '方向三因子（OI 保留／相對 BTC／CVD）是官方的，<b>怎麼合成是我訂的</b>（各 ±1，≥2 偏多）。</div></div>';
}

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
// ★官方評分拆解（公式與原始碼見 trading-backtest/_DHX_SCORE_0924_SPEC.md）
function scoreHTML(r){
  const s = r.sc; if(!s) return '';
  const cl = s.total>=20?'up':(s.total<=-20?'down':'dim');
  const part = (k,v,extra='') => v===0&&!extra ? ''
    : `<span class="sp"><i>${k}</i><b class="${v>0?'up':(v<0?'down':'dim')}">${v>0?'+':''}${v}</b>${extra}</span>`;
  // 數據訊號（15m 進場觸發）跟象限（1H/24H 狀態）本來就會不同號 —— 講清楚比藏起來好
  const sig = (D.dhxev||[]).find(x=>x.inst===r.inst && x.status==='持倉中');
  // ★主顯示改用 24H 尺度：回測說它翻轉率 37.8% vs 1H 版 49.7%（≈丟銅板）、
  //   往後 24H 的多−空價差好 5 倍（+0.138% vs +0.027%，3/4 季較佳），
  //   而且跟官方對得上（同時刻：24H 版為正 15%/中位 −21，官方 19%/−18；
  //   1H 版為正 67%/中位 +5 = 用戶說的「一堆主力建多」）。1H 版留著當「快但吵」的參考。
  const s2 = r.sc24 || {};
  const cl2 = (s2.total>=20?'up':(s2.total<=-20?'down':'dim'));
  // ★`sm` 必須宣告在 `s2` **之後**：先前放在上面，形成 TDZ
  //   （ReferenceError: Cannot access 's2' before initialization）→ cardHTML 整個拋例外、
  //   字卡按不出來。`node --check` 只驗語法抓不到，所以 _chk_dash_js.py 已補成「真的執行」。
  const sm = (s2.total!==undefined) ? s2 : s;      // 拆解跟著主顯示（24H）走
  return (s2.total!==undefined
          ? `<div class="cr"><span>順籌碼分數 <small class="dim">24H・與官方同尺度</small></span>`
            + `<b class="${cl2}" style="font-size:20px">${s2.total>0?'+':''}${s2.total}</b></div>`
          : '')
       + `<div class="cr"><span>同公式・1H 尺度 <small class="dim">快但翻轉率 50%</small></span>`
       + `<b class="${cl}">${s.total>0?'+':''}${s.total}</b></div>`
       + `<div class="sps">`
       + part('市場結構', sm.mkt, sm.mkt_label?`<i class="dim">${sm.mkt_label}</i>`:'')
       + part('結構', sm.struct, sm.struct_label?`<i class="dim">${sm.struct_label}</i>`:'')
       + part('BTC相對', sm.rel, sm.rel_chg?`<i class="dim">${f(sm.rel_chg,1)}%</i>`:'')
       + part('資費', sm.fr, sm.fr_label&&sm.fr_label!=='正常'?`<i class="dim">${sm.fr_label}</i>`:'')
       + part('動能1H', sm.mom1) + part('動能24H', sm.mom24) + part('多空比', sm.ls)
       + `<span class="sp"><i>爆倉</i><b class="dim">無資料</b></span>`
       + `</div>`
       + `<div class="sub">兩個分數是<b>同一套官方公式</b>，只差 <code>chg</code> 用 1H 還是 24H。`
       + `回測 n=308,069（59 幣/2026 全年）：<b>1H 版相鄰小時翻轉 49.7%</b>（≈丟銅板）、`
       + `24H 版 37.8%；往後 24H 的多−空價差 1H 版 +0.027% vs 24H 版 +0.138%（3/4 季 24H 版較佳）。`
       + `<b>反應快的代價就是雜訊多、方向性弱。</b>`
       + `★兩者量級都遠小於往返成本 0.1% —— 這是<b>掃描器不是進場訊號</b>。`
       + `（官方 281 支裡有 223 支其實走 24H fallback，所以他們整頁偏空。）</div>`
       + (sm.crash? '<div class="sub" style="color:var(--warn)">◆ 24H 跌逾 20%：官方會把偏多結構歸零'
           + '並強制壓到偏空（接刀／插針的假性買盤容易被誤判成「主動做多」）。</div>' : '')
       + (sig? `<div class="sub" style="margin:6px 0">◆ 數據訊號此刻是「<b>${(KIND[sig.kind]||[sig.kind])[0]}`
           + `${sig.bias==='LONG'?' 做多':' 做空'}</b>」，跟上面的象限不同號是正常的：`
           + `<b>象限是 1H／24H 的「狀態」</b>（現在資金站哪邊），`
           + `<b>數據訊號是 15m 的「進場觸發」</b>（結構剛翻轉）。`
           + `要進場看訊號，要判斷大環境看象限；兩個同號才是順勢單。</div>` : '');
}

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
    + scoreHTML(r)
    + row('資金費率', r.fr===null||r.fr===undefined? '—' : f(r.fr,4)+'%　<small class="dim">幣安</small>',
          r.fr>0?'up':(r.fr<0?'down':''))
    + row('多空帳戶比', r.lp===null||r.lp===undefined? '—'
          : `多 ${f(r.lp,1)}% / 空 ${f(100-r.lp,1)}%`, r.lp>55?'down':(r.lp<45?'up':''))
    + row('合約 CVD (1H)', r.cvd===null||r.cvd===undefined? '—' : f(r.cvd,2)+'%',
          r.cvd>0?'up':(r.cvd<0?'down':''))
    + row('未平倉量', r.oiu? big(r.oiu)+' USDT' : '—')
    + row('市值', r.mcap? big(r.mcap)
        + (r.mcs&&r.mcs!=='coingecko'? ` <small class="dim">${r.mcs}</small>`:'') : '—')
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
          {v:r.coin, h:tag(r.inst) + `<a class="cl" onclick="openCard('${r.inst}')">${r.coin}</a>`},
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
// ★2026-09-27 改成官方頁面的結構（用戶：「你的好亂」）：
//   官方只列**近 24h** 事件，分「入場訊號」（還在跑）與「已結單區」（止盈/止損/過期），
//   篩選只有 做多/做空 × 吸收/衰竭。原本這頁是每 15 分鐘整批重掃的**狀態快照**、四類分組＋一大段說明。
const DHX_FAM = {ABSORPTION:'吸收', EXHAUSTION:'衰竭', SHORT_TRAP:'假突破', LONG_TRAP:'假突破'};
const DHX_ST = {'持倉中':'warn', '止盈':'up', '止損':'down', '過期':'dim'};
function setDhxF(k, v){ DHXF[k] = (DHXF[k]===v ? '' : v); save(); draw(); }
function dhxR(e){          // 持倉中：以現價算浮動 R（R = 停損距離）
  if(e.status!=='持倉中') return e.r;
  const risk = Math.abs(e.entry - e.sl);
  if(!e.last || !risk) return null;
  return (e.bias==='LONG' ? (e.last - e.entry) : (e.entry - e.last)) / risk;
}
// 已結單（止盈/止損）扣往返手續費 0.10% 後的平均 R：±1R − 0.10/停損距%。過期單不算（沒有 1R 結果）。
function dhxNetR(list){
  const d = list.filter(e=>e.status==='止盈'||e.status==='止損');
  if(!d.length) return null;
  // 用事件自己記的 r（止盈 = 目標 R，2026-09-28 起 2R；更早的舊單是 1R）；沒有 r 才退回 ±1
  return d.reduce((s,e)=>s + (e.r!=null ? e.r : (e.status==='止盈'?1:-1))
                    - 0.10/Math.max(e.sl_dist_pct||0,1e-6), 0) / d.length;
}
function fmtR(r){ return r==null ? '—' : (r>=0?'+':'') + f(r,2) + 'R'; }
function dhxRows(list, id){
  return table(id, ['時間','幣','方向','類型','進場','停損','目標','停損%','狀態'], list, e=>{
    const r = dhxR(e), st = e.status||'';
    const stH = st==='持倉中'
      ? `持倉中 <span class="${r==null?'dim':cls(r)}">${r==null?'':(r>=0?'+':'')+f(r,2)+'R'}</span>`
      : st + (st!=='過期' && e.exit_ts ? ` <span class="dim">${ago(e.exit_ts)}前</span>` : '');
    return [
      {v:e.ts, h:ago(e.ts)+'前'},
      {v:e.inst, h:tag(e.inst) + `<a class="cl" onclick="openCard('${e.inst}')">`
        + e.inst.replace('-USDT-SWAP','') + '</a>'},
      {v:e.bias, h:e.bias==='LONG'?'做多':'做空', c:e.bias==='LONG'?'up':'down'},
      {v:e.kind, h:(KIND[e.kind]||[e.kind])[0]},
      pf(e.entry), pf(e.sl), {v:e.tp||e.tp1, h:pf(e.tp||e.tp1) + `<span class="dim"> ${e.tp_r||1}R</span>`},
      f(e.sl_dist_pct,2)+'%',
      {v:r==null?-99:r, h:stH, c:DHX_ST[st]||''},
    ];
  });
}
// ★「精選」= 像官方的那一群（2026-09-27 `_an_dhx_v3_neg.py` / `_an_dhx_v3_gates.py`）：
//   同 20 幣比「官方有發」vs「我多發」，官方的吞噬 K 明顯較大（全幅 AUC 0.78）、OI 變化較大（0.69）。
//   吞噬全幅 ≥0.8% 且 |OI| ≥1%：頻率 14.1→7.1 倍，v3 抓得到的官方單留 16/20。
//   ★門檻是在同一批 20 筆官方紀錄上挑的 → 只做成網頁篩選（可關），不寫進偵測器；等新紀錄做樣本外驗證。
//   假突破（TRAP）沒評估過，不受這個篩選影響。
const DHX_A_RNG = 0.8, DHX_A_OI = 1.0;
function dhxIsA(e){
  if(DHX_FAM[e.kind]==='假突破') return true;
  return (e.engulf_rng_pct||0) >= DHX_A_RNG && Math.abs(e.oi_delta_pct||0) >= DHX_A_OI;
}
function viewDhx(){
  const all = D.dhxev||[], q = D.dhxq||{};
  const match = e => (!DHXF.dir || e.bias===DHXF.dir) && (!DHXF.kind || DHX_FAM[e.kind]===DHXF.kind)
                     && (!DHXF.q || dhxIsA(e));
  const rows = all.filter(match);
  const nA = all.filter(dhxIsA).length;
  const open = rows.filter(e=>e.status==='持倉中');
  const done = rows.filter(e=>e.status!=='持倉中').sort((a,b)=>(b.exit_ts||0)-(a.exit_ts||0));
  const nTp = done.filter(e=>e.status==='止盈').length, nSl = done.filter(e=>e.status==='止損').length;
  const nEx = done.length - nTp - nSl, nDec = nTp + nSl;
  const chip = (k,v,lab) => `<div class="wb ${DHXF[k]===v?'on':''}" onclick="setDhxF('${k}','${v}')">${lab}</div>`;
  let h = '<div class="card"><h2>數據背離訊號'
    + `<span>近 24h ${all.length} 筆（精選 ${nA}）・15m・掃 ${q.i||0} 幣</span></h2>`
    + '<div class="wins">' + chip('q','A','精選') + '<span class="src"></span>'
    + chip('dir','LONG','做多 📈') + chip('dir','SHORT','做空 📉')
    + '<span class="src"></span>' + chip('kind','吸收','吸收') + chip('kind','衰竭','衰竭')
    + chip('kind','假突破','假突破') + '</div>'
    + '<div class="note">⚠ 這是<b>我自己的偵測器</b>，不是官方訊號：同幣同期抓得到官方約一半（47%）、'
    + '發的量約官方 14 倍；<b>精選</b>（吞噬K ≥0.8%、OI ≥1%）壓到約 7 倍、官方單留 8 成。'
    + '精選門檻只用 20 筆官方紀錄挑出來，還沒做樣本外驗證。</div>';
  h += `<h3 class="sech">入場訊號 <span class="dim">${open.length}</span></h3>`
    + (open.length ? dhxRows(open, 'dhxo') : '<div class="empty">暫無入場訊號</div>');
  h += `<h3 class="sech">已結單區 <span class="dim">止盈 ${nTp}・止損 ${nSl}・過期 ${nEx}`
    + (nDec ? `・勝率 ${f(nTp/nDec*100,0)}%・<b>扣費後平均 ${fmtR(dhxNetR(done))}</b>`
        + `（n=${nDec}${nDec<20?'，⚠ 樣本太少不能下結論':''}）` : '')
    + '</span></h3>'
    // ★目標 1R、停損 1R → 勝率 50% 只是打平；停損距很近時手續費吃掉一大塊 R，勝率會騙人。
    //   2026-09-28 實測：頁面勝率 62% 扣費後只剩 +0.02R；v3 回測 50%／官方自己 52%，扣費後都 ≈0。
    + '<div class="sub">2R 目標（止盈 +2R／止損 −1R）：勝率 33% ＝打平。扣費用往返 0.10%（停損距越近、每筆扣的 R 越多）。'
    + '回測（20 幣 20 天，扣費）：v3 2R +0.06R／精選 +0.14R／官方自己 +0.15R，'
    + '但信賴區間都跨 0、跟隨機進場比也分不出來 —— 不能當成已驗證的勝率。</div>'
    + (done.length ? dhxRows(done, 'dhxc') : '<div class="empty">暫無紀錄</div>');
  return h + '<details class="sub" style="margin-top:8px"><summary>判定規則與狀態怎麼算</summary>'
    + '<b>狀態</b>：進場＝偵測當下價格。之後用幣安 15m 高低（從下一根起算）加上 5 分鐘取樣價判定：'
    + '碰目標（2R；09-28 以前的舊單是 1R）＝止盈、碰停損＝止損、同一根兩者都碰到算止損（不知道先後，保守算）、'
    + '24 小時都沒碰到＝過期。官方還有「平保」，規則沒公布，這裡沒做。<br>'
    + '<b>假跌破收回</b>（做多）：跌破前低又收回收盤價，且<b>合約 CVD 降、現貨 CVD 升</b>、OI 升。'
    + '<b>假突破收回</b>（做空）為鏡像。這組 CVD／OI 條件是官方硬條件'
    + '（400 筆實測 100% 一致），沒有 CVD 就不發。<br>'
    + '<b>吸收背離</b>：做多＝低點<b>抬高</b>但 <b>CVD 樞紐低點降低</b>（賣壓被限價買單吸收）；做空為鏡像。'
    + '停損放 pivot2 的價格。<br>'
    + '<b>衰竭背離</b>：做多＝<b>砸破</b>前低但 CVD <b>未</b>創新低（空方力竭）；做空為鏡像。<br>'
    + '吸收 vs 衰竭只差一件事：價格<b>有沒有破</b>前低／前高。<br>'
    + '選幣＝24h 成交額前 100（官方 <code>volume_top100</code>）。'
    + '「方向」是型態的進場方向，跟 OI 排名的「象限」是不同維度，兩者不同是正常的。'
    + '</details></div>';
}

// ★翻倉紙上前推（2026-09-30）：只顯示 fanpan 已算好的值（D.fp），不打任何 API。
function fpT(ms){ if(!ms) return '—'; const d=new Date(ms+8*3600000), p=n=>String(n).padStart(2,'0');
  return p(d.getUTCMonth()+1)+'-'+p(d.getUTCDate())+' '+p(d.getUTCHours())+':'+p(d.getUTCMinutes()); }
function viewFp(){
  const p = D.fp || {};
  if(!p.ok) return '<div class="card"><h2>翻倉</h2><div class="empty">翻倉前推沒有資料（'+(p.err||'未知')+'）</div></div>';
  const rows = p.rows || [];
  const open = rows.filter(r=>r.act==='進場' && r.K==='持倉中');
  const last = rows.length ? rows[rows.length-1] : null;
  let h = '<div class="card"><h2>翻倉<span>紙上前推・只記錄不下單・起點 '+fpT(p.start)+'（台北）</span></h2>';
  if(open.length){
    const r = open[open.length-1];
    h += '<div class="fpbig up">✅ 現在這筆可以下：<b>'+r.coin+'</b> 做多'
      + '<br>進場 <b>'+pf(r.e)+'</b>　停損 <b>'+pf(r.sl)+'</b>　漲到 <b>'+pf(r.be)+'</b> 停損移進場價　TP2 停利 <b>'+pf(r.tp2)+'</b>（整筆全出）'
      + '<div class="sub">'+fpT(r.t)+' 官方 '+(r.typ||'')+' 訊號・發訊時 BTC 24h '+f2(r.btc)+'</div></div>';
  } else {
    h += '<div class="fpbig dim">目前沒有可以下的單'
      + (last ? '<div class="sub">最新一筆官方做多：'+fpT(last.t)+' '+last.coin+' → '+last.act+(last.K?('（'+last.K+'）'):'')+'</div>' : '')
      + '</div>';
  }
  const sc = p.score||0, need = (p.up||0) - sc;
  h += '<div class="fpsum">紙上資金 100U → <b>'+f(p.eq,1)+'U</b>　贏 '+p.W+'・保本 '+p.B+'・輸 '+p.L
    + '<br>檢定分數 <b class="'+cls(sc)+'">'+(sc>=0?'+':'')+f(sc,2)+'</b>（到 +'+f(p.up,1)+' 可以開始翻倉、到 '+f(p.down,1)+' 判定不行；每贏 +'+f(p.w_step,2)+'、每輸 '+f(p.l_step,2)+'）'
    + '<br><b>'+(p.decision||'—')+'</b>'
    + (p.decision==='繼續記錄' && p.w_step ? '・離可以開始還差 '+f(need,2)+'（約再淨贏 '+Math.max(0,Math.ceil(need/p.w_step))+' 筆）' : '')
    + '</div>';
  h += '<div class="note">規則（事先寫死、不會改）：官方數據訊號做多 ＋ 發訊時 BTC 過去 24h ≤0%｜每單風險＝資金 50%｜漲到 0.5R 停損移進場價｜TP2（1.5R）整筆全出｜一次只拿一筆。'
    + '到 +2.2 時 Discord 會發 🚨 通知；這頁跟 Discord 都<b>不會自己下真單</b>。官方 API：'+(p.api||'—')+(p.err?'・⚠ '+p.err:'')+'</div>';
  h += '<h3 class="sech">最近官方做多訊號 <span class="dim">'+rows.length+'</span></h3>';
  if(!rows.length) return h + '<div class="empty">前推開始後還沒有官方做多訊號</div></div>';
  return h + table('fp', ['時間','幣','進場','停損','TP2','BTC24h','處理','紙上資金'], rows.slice().reverse(), r => [
    {v:r.t, h:fpT(r.t)}, r.coin, pf(r.e), pf(r.sl), pf(r.tp2), {v:r.btc||0, h:f2(r.btc), c:cls(-(r.btc||0))},
    r.act==='進場' ? {v:1, h:'✅ '+(r.K||''), c:r.K==='贏'?'up':(r.K==='輸'?'down':'')} : {v:0, h:'⛔ '+String(r.act).replace('不進','')},
    {v:r.eq==null?-1:r.eq, h:r.eq==null?'—':f(r.eq,1)+'U'}]) + '</div>';
}

function viewMkt(){
  const m=D.mkt, all=m.rows||[];
  if(!all.length) return noData(m);
  // ★預設只看 OI 金額前 100（跟官方同選法）；🎯🔥 的幣不論大小一律保留。散佈圖跟表都吃這份。
  const rows = poolRows(all);
  const hit = r => Math.abs(r.oi*100)>=OITH && Math.abs(r.px*100)<=PXTH;
  const sel = rows.filter(hit);
  return '<div class="card">' + winBar()
    + `<h2>視覺篩選器<span>${m.win_h}H・${ALLCOINS ? '全部 '+all.length : 'OI 前 100（共 '+all.length+'）'}`
    + ` 個合約・命中 ${sel.length}</span></h2>`
    + scaleBar()
    // ★官方把這一頁叫「巨鯨雷達」（前端 data-target-tab="visual"），跟「OI 儀表板」(data-target-tab="oi") 是兩個不同分頁。
    //   原話：「用所選週期的持倉變化＋價格變化，觀察資金是否已經注入市場，
    //   找出可能『資金先動、行情還沒完全啟動』的機會」。
    + '<div class="sub" style="margin-bottom:8px">X＝持倉變化、Y＝價格變化，'
      + `門檻 OI ≥${((m.gate||{}).oi_min*100)||1}%、|價格| ≤${((m.gate||{}).px_max*100)||5}%。`
      + '官方原話：<b>「象限只描述持倉與價格，不直接判定多空；方向請以詳細數據綜合判斷」</b>'
      + ' —— 這是<b>瀏覽工具</b>；會發警報、會判方向的是<b>巨鯨雷達</b>那一頁（門檻不同）。'
      + '另外「OI 排名」那頁的象限有套市場結構覆寫、是帶多空語意的，跟這裡不一樣。</div>'
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
          {v:r.inst, h:tag(r.inst) + `<a class="cl" onclick="openCard('${r.inst}')">`
            + (r.inflow?'<span class="star">◆</span>':'')+r.inst.replace('-USDT-SWAP','')+'</a>'},
          {v:r.oi, h:pct(r.oi), c:cls(r.oi)},
          {v:r.px, h:pct(r.px), c:cls(r.px)},
          {v:r.q, h:`<span style="color:${QCLR[r.q]}">${r.q}</span>`},
        ]) : '<div class="empty">沒有符合的幣：OI 要夠大、價格要夠靜。把 OI 門檻往左拉。</div>')
    + '</div>';
}

// ── OI 異動排名 ─────────────────────────────────────────────────────────────
// ★版面照官方：**一張表、表頭只出現一次**，四個象限當「分段標題列」插在表身裡。
//   先前寫成四個象限各一張 <table>，欄寬各自算 → 四塊對不齊（用戶 2026-09-24：「很醜」）。
//   順便用 colgroup 固定欄寬，數字欄才不會因為位數不同跳來跳去。
// ★加「OI 金額」欄：官方的 OI 是 CoinGlass **跨所聚合**、我的只有 OKX，
//   金額差 12~45 倍（實測 SUI 857.8M vs 37.0M、VVV 190M vs 4.2M）。
//   不把金額擺出來的話，OKX 上只有 $1M 未平倉的小幣（一張大單就 +10%）
//   會跟 $800M 的大幣在榜上長得一樣大 —— 那不是「主力建倉」。
const RANK_COLS = ['#','幣種','價格','OI變化','OI金額','OI/市值','價24H'];
// ★欄寬全部寫死、不留 auto：留 auto 的那一欄會在寬螢幕上把剩餘寬度全吃掉，
//   加上第二欄以後預設靠右，結果 # 在最左、其他擠在最右，中間一片空白
//   （用戶 2026-09-24：「這是比目魚才能看嗎 隔那麼遠」）。
const RANK_W    = ['32px','120px','92px','84px','86px','78px','78px'];

// ★通用分段表：一張 <table>、表頭只出現一次，分組當標題列插在表身。
//   訊號類的頁面原本是一張平表全部擠在一起（用戶 2026-09-24：「全都擠在一起很難看」）。
//   不用「每組一張表」是因為那樣欄寬各自算、組跟組之間會對不齊（OI 排名踩過）。
function secTable(id, cols, widths, groups, render){
  const total = groups.reduce((n,g)=>n+g[2].length, 0);
  if(!total) return '<div class="empty">沒有資料</div>';
  let body = '';
  for(const [label, cls_, rows] of groups){
    if(!rows.length) continue;
    body += `<tr class="sec"><td colspan="${cols.length}">`
          + `<b class="${cls_||''}">${label}</b>`
          + `<span class="dim" style="margin-left:8px">${rows.length}</span></td></tr>`;
    rows.forEach((r,i)=>{
      body += '<tr>' + render(r,i).map(c=>{
        const v = (c&&c.v!==undefined) ? (c.h!==undefined?c.h:c.v) : c;
        return `<td class="${(c&&c.c)||''}">${v}</td>`;
      }).join('') + '</tr>';
    });
  }
  return '<div class="scroll"><table class="fx">'
    + '<colgroup>' + widths.map(w=>`<col style="width:${w}">`).join('') + '</colgroup>'
    + '<thead><tr>' + cols.map(c=>`<th>${c}</th>`).join('') + '</tr></thead>'
    + '<tbody>' + body + '</tbody></table></div>';
}

function viewRank(){
  const m=D.mkt, rows=m.rows||[];
  if(!rows.length) return noData(m);
  const top20 = rows.slice(0,20);      // ★官方：全部一起排取前 20，再分四組
  let body = '';
  for(const q of QUADS){
    const g = top20.filter(r=>(r.q24||r.q)===q);
    if(!g.length) continue;
    const meta = (m.quads&&m.quads[q]) ? m.quads[q] : ['',''];
    body += `<tr class="sec"><td colspan="${RANK_COLS.length}">`
          + `<b style="color:${QCLR[q]}">${q}</b>`
          + `<span class="dim" style="margin-left:8px">${meta[0]}</span></td></tr>`;
    g.forEach((r,i)=>{
      const s = r.sc24 || r.sc || {};   // 排名上的小分數也用主尺度（24H）
      body += '<tr>'
        + `<td class="dim">${i+1}</td>`
        + `<td>${tag(r.inst)}<a class="cl" onclick="openCard('${r.inst}')">`
          + (r.an===2?'<span class="star">★</span>':'')
          + r.inst.replace('-USDT-SWAP','') + '</a>'
          + (s.total!==undefined
             ? `<span class="scp ${s.total>=20?'up':(s.total<=-20?'down':'dim')}">`
               + `${s.total>0?'+':''}${s.total}</span>` : '')
        + '</td>'
        + `<td>${pf(r.last)}</td>`
        + `<td class="${cls(r.oi)}">${pct(r.oi)}</td>`
        + `<td class="${(r.oiu||0)<5e6?'dim':''}">${r.oiu?big(r.oiu):'—'}</td>`
        + `<td>${r.oimc?f(r.oimc*100,2)+'%':'—'}</td>`
        + `<td class="${cls(r.chg24h)}">${pct(r.chg24h)}</td>`
        + '</tr>';
    });
  }
  return '<div class="card">' + winBar()
    + `<h2>OI 異動排名<span>${m.win_h}H 持倉量變化・依 |OI 變化%| 取前 20</span></h2>`
    + scaleBar()
    + '<div class="scroll"><table class="fx">'
    + '<colgroup>' + RANK_W.map(w=>`<col style="width:${w}">`).join('') + '</colgroup>'
    + '<thead><tr>' + RANK_COLS.map(c=>`<th>${c}</th>`).join('') + '</tr></thead>'
    + '<tbody>' + body + '</tbody></table></div>'
    + '<div class="sub" style="margin-top:8px">'
    + (m.agg ? `★OI 已改為<b>跨所聚合</b>（${m.agg_ex||'OKX+幣安+Bitget+Gate'}）。`
             + '★<b>拿不到 Bybit</b>：三個網域從雲端都被 CloudFront 擋 403，'
             + '實測它約佔三到四成 —— 所以這不是全市場，也不會跟官方逐筆相同。'
             : '★<b>OI 目前只有 OKX</b>（聚合還在累積）：官方是 CoinGlass 跨所加總，'
             + '同一幣金額差 12~45 倍、變化% 甚至方向相反。')
    + '<b>金額小的（灰字，&lt;5M）一張大單就能推到 +10%，別當成主力建倉。</b><br>'
    + '象限已套用官方的市場結構覆寫層'
    + '（評分裡的「主動做多／主動做空／多頭出場／空頭出場」會蓋過單純的 OI×價格方向）。</div>'
    + '</div>';
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

// ★搜尋框的狀態存在變數裡，不要每次從 DOM 讀 —— 因為 draw() 會把輸入框整個重建。
//   原本寫 `oninput="draw();...focus()"`：重建後 focus() 讓**游標回到位置 0**，
//   下一個字就插在最前面 → 打「BTC」變成「CTB」，看起來像由右到左，過濾當然也對不上
//   （用戶 2026-09-24 回報「沒辦法搜尋 而且打字會變成由右到左」）。
let CQ = '';
function coinSearch(el){
  CQ = el.value;
  const pos = el.selectionStart;      // 記住游標，重繪後放回原處
  draw();
  const n = document.getElementById('cq');
  if(n){ n.focus(); try{ n.setSelectionRange(pos, pos); }catch(e){} }
}

function viewCoins(){
  const rows=[];
  for(const [sym,tfs] of Object.entries(D.coins))
    for(const [tf,r] of Object.entries(tfs)) rows.push({sym,tf,...r});
  const q = CQ;
  const flt = q ? rows.filter(r=>r.sym.toLowerCase().includes(q.toLowerCase())) : rows;
  if(!rows.length)
    return '<div class="card"><h2>掃描快照</h2>'
      + '<div class="empty">還沒有資料：這一頁是<b>掃描迴圈</b>每根 K 收盤時順手記下來的，'
      + '重新部署後要等下一根 <b>15m 收盤</b>才會出現（最多 15 分鐘）。</div></div>';
  return '<div class="card">'
    + `<h2>掃描快照<span>${rows.length} 列`
    + (q ? `・篩出 ${flt.length}` : '') + '</span></h2>'
    + `<input class="f" id="cq" placeholder="篩選幣種…" value="${q}" oninput="coinSearch(this)">`
    + (flt.length ? table('coins', ['幣','時框','價格','ATR%','ADX','通道','趨勢','更新'], flt, r=>[
        {v:r.sym, h:`<a class="cl" onclick="openCard('${r.sym.replace('/USDT','')}-USDT-SWAP')">`
                  + r.sym.replace('/USDT','') + '</a>'},
        r.tf, pf(r.px),
        {v:r.atrp, h:r.atrp===undefined?'—':f(r.atrp*100,2)},
        {v:r.adx, h:f(r.adx,1), c:r.adx>=25?'up':(r.adx<15?'dim':'')},
        r.vg||'—',
        {v:r.trend||'', h:r.trend==='bear'?'空':(r.trend==='bull'?'多':'—'),
         c:r.trend==='bear'?'down':(r.trend==='bull'?'up':'dim')},
        ago(r.ts),
      ]) : `<div class="empty">沒有符合「${q}」的幣。</div>`) + '</div>';
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
  const all = (D.signals||[]).slice().sort((a,b)=>b.ts-a.ts);
  const nL = all.filter(s=>s.dir==='long').length;
  // 依**策略**分組（要掃「哪支策略在發單」比逐筆看有用），組內按時間新到舊；
  // 組的順序用「最近一次發訊」排，最近在動的策略在最上面。
  const by = {};
  all.forEach(s=>{ (by[s.strat||'—'] = by[s.strat||'—'] || []).push(s); });
  const groups = Object.entries(by)
    .sort((a,b)=>b[1][0].ts - a[1][0].ts)
    .map(([k,v])=>[k, (v.filter(x=>x.dir==='long').length >= v.length/2) ? 'up':'down', v]);
  return '<div class="card">'
    + `<h2>最近訊號<span>${all.length} 筆・多 ${nL}／空 ${all.length-nL}`
    + `・${groups.length} 支策略</span></h2>`
    + secTable('sig', ['幣','時框','方向','價格','停損','時間'],
        ['110px','64px','56px','100px','100px','76px'], groups, s=>[
        {v:s.symbol, h:`<a class="cl" onclick="openCard('${(s.symbol||'').replace('/USDT','')}-USDT-SWAP')">`
           + (s.symbol||'').replace('/USDT','') + '</a>'},
        s.tf,
        {v:s.dir, h:s.dir==='long'?'多':'空', c:s.dir==='long'?'up':'down'},
        pf(s.price), s.sl?pf(s.sl):'—', ago(s.ts),
      ]) + '</div>';
}

function viewSys(){
  const m=D.mode;
  const kv=(k,v,c='')=>`<div class="kv"><span class="dim">${k}</span><b class="${c}">${v}</b></div>`;
  let h = notiPanel() + '<div class="card"><h2>執行設定</h2><div class="grid">'
    + kv('風險/筆', (m.risk_pct*100).toFixed(1)+'%')
    + Object.entries(m.auto_trade).map(([k,v])=>kv(k, v?'開':'關', v?'up':'dim')).join('')
    + '</div></div>';
  h += '<div class="card"><h2>策略開關</h2><div class="grid">'
    + Object.entries(D.flags).map(([k,v])=>
        kv(k.replace(/_ENABLED$/,''), v?'開':'關', v?'up':'dim')).join('')
    + '</div></div>';
  return h;
}

const PAGE_VER = '__VER__';   // ★送頁面時由伺服器填入 VER（2026-09-30：曾經只改了 Python 的 VER 忘了這裡 → 每次開頁都重載一次）
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
      // ★正在搜尋框打字時不要重繪：20 秒的自動刷新會把輸入框重建、游標歸零，
      //   症狀跟上面那個「由右到左」一模一樣，只是隔 20 秒才發作一次（更難查）。
      const ae = document.activeElement;
      if(!(ae && ae.id === 'cq')) draw();
    }
  }catch(e){}
}
// 從推播點進來會帶 #tab=xxx → 直接切到那一頁
try{ const _h = (location.hash||'').match(/tab=([a-z]+)/); if(_h && TABS.some(t=>t[0]===_h[1])) TAB = _h[1]; }catch(e){}
tick(); setInterval(tick, 20000);
pushInit();
document.addEventListener('visibilitychange', ()=>{ if(!document.hidden) tick(); });
</script></body></html>
"""
