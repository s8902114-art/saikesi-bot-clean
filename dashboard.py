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
VER = "20261011tx10"


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
        # ★妖幣觀察清單（2026-10-02，Barry 10-01 直播）：只讀 yaobi 已算好的值＋巨鯨事件標記，不打 API
        "yao": _yao(G),
        # ★麥門台指（2026-10-11）：只讀 mmtx 已算好的值（期交所 1 分 K → 階梯／籌碼異常星／彩色K），不打 API
        "tx": _tx(G),
    }


def _yao(G):
    try:
        m = G.get("yaobi")
        p = m.dash_payload() if m else {"ok": False, "err": "模組未載入"}
        if p.get("ok"):
            wh = {}
            for ev in (G.get("_WHALE") or {}).values():
                if ev.get("dir") in ("bull", "pending"):
                    wh[ev.get("inst")] = ev.get("dir")
            p["whale"] = wh
        return p
    except Exception as e:
        return {"ok": False, "err": type(e).__name__}


def _tx(G):
    try:
        m = G.get("mmtx")
        return m.dash_payload() if m else {"ok": False, "err": "模組未載入"}
    except Exception as e:
        return {"ok": False, "err": type(e).__name__}


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

    @app.route("/d/<tok>/txc")
    def _dash_txc(tok):
        # ★麥門指標圖表 K 棒（2026-10-11）：只讀 mmtx 已算好的列；since＝前端手上最後一根 → 只回之後的（20 秒一次不用整包重抓）
        if not _token_ok(tok):
            return "", 404
        try:
            m = G.get("mmtx")
            try:
                since = int(request.args.get("since")) if request.args.get("since") else None
            except (TypeError, ValueError):
                since = None
            r = jsonify(m.dash_chart(request.args.get("s", "TX"), request.args.get("tf", "15"), since) if m
                        else {"ok": False, "err": "模組未載入"})
        except Exception as e:
            r = jsonify({"ok": False, "err": type(e).__name__})
        r.headers["X-Robots-Tag"] = "noindex, nofollow"
        r.headers["Cache-Control"] = "no-store"
        r.headers["Access-Control-Allow-Origin"] = "*"
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
  .txbar{display:flex;flex-wrap:wrap;gap:6px;align-items:center;margin:6px 0}
  .txsp{flex:0 0 10px}
  .cd .txwrap{height:360px}
  .txq{width:70px;padding:4px 6px;border:1px solid var(--line);border-radius:7px;background:#0f141c;color:var(--fg);font-size:12px}
  .txb{display:inline-block;padding:0 6px;border-radius:5px;font-weight:700;color:#fff;font-size:11px;line-height:17px}
  /* 開高低收那排放在圖的上面、不疊在圖上（用戶 10-14：「圖表可以不要跟開高低收那欄重疊嗎 會被卡住」）→ 圖從 --lgh 往下開始 */
  .txwrap{position:relative;width:100%;height:480px;margin:6px 0;--lgh:42px}
  .txbox{position:absolute;left:0;top:var(--lgh);right:0;bottom:0}
  .txov{position:absolute;left:0;top:var(--lgh);right:60px;bottom:0;pointer-events:none;z-index:2;overflow:hidden}
  .txsr{position:absolute;left:0;top:var(--lgh);right:60px;bottom:0;pointer-events:none;z-index:1;overflow:hidden}
  .txlg{position:absolute;left:0;top:0;right:0;height:var(--lgh);overflow:hidden;z-index:3;font-size:12px;pointer-events:none;
        padding:2px 6px;line-height:1.6}
  @media (max-width:600px){ .txwrap{height:440px;--lgh:58px} .txlg{font-size:11px;line-height:1.5} }
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
let D = null, TAB = 'tx', SORT = {}, QF = '';      // 用戶 10-14：「讓我開進去的時候首頁在台指那頁」

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

const TABS = [['tx','台指'],['pat','型態'],['fp','翻倉'],['yao','妖幣'],['mkt','視覺篩選器'],['whale','巨鯨雷達'],['rank','OI 排名'],['anom','警報'],['dhx','數據訊號'],['pos','持倉'],['coins','幣種'],
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
  if(TAB==='yao') body = viewYao();
  if(TAB==='tx') body = viewTx();
  if(TAB==='pat') body = viewPat();
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
  if(TAB==='tx' || CARD){ try{ txMount(); }catch(e){ TXERR = e.name+' '+e.message; } }   // 圖表常駐在 draw() 外，搬回新插槽
  if(TAB==='pat'){ try{ patScan(); }catch(e){} }                                          // 型態分頁：開著才掃（10 分鐘內掃過就不重掃）
}

// ── ★手機推播（2026-09-30，用戶：「每頁都可以選擇開或不開，要像數據獵手手機也可以通知」）──
//   每支手機各自記住哪幾頁要通知（存伺服器，redeploy 不掉；本機也存一份給畫面用）。
const PUSHAPI = location.pathname.replace(/\\/$/,'') + '/push';
const NOTI = {fp:'有可以下的單、出結果、過 +2.2／−2.2', tx:'台指／加權彩色K換色（15 分 K 收盤確認）、台指籌碼異常星', yao:'有長盤整的新放量訊號、限價成交、贏或輸', mkt:'某個幣剛出現 🔥', whale:'新的巨鯨訊號', rank:'新擠進 OI 增幅前 10',
  anom:'新警報、警報變成確認', dhx:'新的數據訊號', pos:'開倉、平倉', coins:'1H 結構轉向（上升↔下降）',
  diag:'策略觸發', sig:'bot 發出新的策略訊號', sys:'策略開關被改動',
  pat:'（這頁是手機自己跟 OKX 抓、自己算，bot 沒有這份資料，不會推播）'};
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
// ★字卡裡直接有 K 線圖（用戶 10-11：「我是希望按幣的字卡就可以看到」）：開字卡時圖切到這個幣，關掉還原原本看的
let TXSAVE = null;
function openCard(inst){
  if(D && D.mkt && (D.mkt.rows || []).some(x => x.inst === inst)){ if(!TXSAVE) TXSAVE = [TXS, TXTF]; TXS = 'C:' + inst; }
  CARD = inst; draw(); }
function closeCard(){ CARD = null; if(TXSAVE){ TXS = TXSAVE[0]; TXTF = TXSAVE[1]; TXSAVE = null; } draw(); }
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
    + (TXS === 'C:' + r.inst ? '<div class="txbar">'
        + txTfs().map(k => `<span class="wb ${TXTF===k?'on':''}" onclick="txSel(null,'${k}')">${TXTFN[k]}</span>`).join(' ')
        + (txIntra() ? '<span class="txsp"></span>' + [['1','參數一'],['2','參數二']].map(([k, n]) =>
            `<span class="wb ${TXLAD===k?'on':''}" onclick="txLadSel('${k}')">階梯${n}</span>`).join(' ') : '')
        + '<span class="txsp"></span>' + txTglBar()
        + '</div>' + txCSum() + '<div id="txslotc"></div>'
        + '<div class="sub dim">雙指／滾輪縮放、左右拖曳、按住看那根的價；彩色K＝台指公式直接套（沒驗證）、階梯＝CME 時段 VWAP、灰色＝暖機。</div>' : '')
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
         <button class="bt" onclick="openChart('${r.inst}')">大圖</button>
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

// ★麥門指標（台指／加權，2026-10-11）：摘要讀 D.tx；K 棒走 /txc（只有這頁開著才抓，之後只抓最後一根）。
//   圖表＝TradingView lightweight-charts（滾輪/雙指縮放、拖曳、十字線看價）。圖表物件放在 draw() 外面常駐，
//   每 20 秒重繪只把它搬回新的插槽 → 縮放位置不會被洗掉。程式庫載不到時退回簡易 SVG。
//   K 棒每根 = [時間秒(台北當UTC), 開, 高, 低, 收, 量, 顏色(-1=暖機不上色), 階梯P2, 階梯P1, 星(5分K量,0=無)]
const TXC = {'-1':'#4b5563', 0:'#6b7280', 1:'#eab208', 2:'#2c8ce3', 3:'#d15d5d'};   // 黑在暗底看不到 → 深灰
const TXN = {'-1':'暖機', 0:'黑', 1:'金', 2:'藍', 3:'紅'};
const TXTFN = {'15':'15分', '60':'1H', '240':'4H', 'D':'日', 'W':'週'};   // 4H 只有幣有（台指/加權伺服器沒算）
const TXAPI = API.slice(0, -3) + 'txc';
const TXLIB = 'https://cdn.jsdelivr.net/npm/lightweight-charts@4.2.0/dist/lightweight-charts.standalone.production.js';
let TXS = 'TX', TXTF = '15', TXCH = null, TXDATA = {}, TXLIBST = '', TXERR = '', TXBUSY = {}, TXLAST = {};
try{ const v = JSON.parse(localStorage.getItem('txsel')||'{}');
  if(TXTFN[v.tf]) TXTF = v.tf; if(TXTF === '240' && !txIsC(TXS)) TXTF = '60'; }catch(e){}   // 商品不還原：一打開就是台指（用戶 10-14），週期照上次
function txSel(s, tf){ if(s) TXS = s; if(tf) TXTF = tf; if(TXTF === '240' && !txIsC(TXS)) TXTF = '60'; if(!TXSAVE){ try{ localStorage.setItem('txsel', JSON.stringify({s:TXS, tf:TXTF})); }catch(e){} } draw(); }
// 階梯參數：麥門一次只畫一個（設定裡選）→ 按鈕切換，預設參數一（用戶 10-11）。K 棒列裡 參數二＝b[7]、參數一＝b[8]
let TXLAD = '1';
try{ const v = localStorage.getItem('txlad'); if(v === '1' || v === '2') TXLAD = v; }catch(e){}
function txLadIdx(){ return TXLAD === '2' ? 7 : 8; }
function txLadSel(k){ TXLAD = k; try{ localStorage.setItem('txlad', k); }catch(e){}
  if(TXCH && TXDATA[TXCH.key]){ try{ txLadApply(); txLegend(null); }catch(e){} }
  draw(); }
// ★上下線（麥門「到價推撥」的上線／下線＝帳號自己設的警示價，不是公式；2026-10-11）：值從麥門文章圖讀出（mmtx_updown.json，±3 點）。
//   用戶：「做個開關，我看一下有什麼跡象」「要分段，不然同時太多會不知道什麼時間點的」
//   → 每條線只畫「這次讀到 → 下次讀到不同值」那一段，段首標時間與值；段的起點＝那張圖的上傳時間（圖檔 UUIDv7）。
//   不參與縱軸自動縮放（離價格很遠的線不可以把 K 棒壓扁）；看不到時圖例與下方紀錄照樣有數值。
const TXUDC = {up:'#bf57aa', dn:'#4f7fd6'};
let TXUD = '1';
try{ const v = localStorage.getItem('txud'); if(v === '0' || v === '1') TXUD = v; }catch(e){}
function txUdSel(){ TXUD = TXUD === '1' ? '0' : '1'; try{ localStorage.setItem('txud', TXUD); }catch(e){}
  if(TXCH && TXDATA[TXCH.key]){ try{ txUdApply(); txLegend(null); }catch(e){} }
  draw(); }
function txUdObs(){ const U = (D && D.tx && D.tx.updown) || {}; return txIsC(TXS) ? [] : (U[TXS] || []); }
function txUdSegs(k){      // [{s:起點秒, v:值, d:'MM-DD HH:MM'}]；±5 點內＝同一條線（讀數誤差）→ 不換段
  const out = [];
  for(const o of txUdObs()){
    const v = o[k]; if(v === null || v === undefined) continue;
    const last = out[out.length-1];
    if(last && Math.abs(last.v - v) <= 5) continue;
    out.push({s:o.ts, v:v, d:String(o.t).slice(5)});
  }
  return out;
}
function txUdAt(segs, t){ let r = null; for(const g of segs){ if(g.s <= t) r = g; else break; } return r; }
function txUdData(bars, segs, k){
  const pts = [], mk = []; let j = -1, pj = -1;
  for(const b of bars){
    while(j + 1 < segs.length && segs[j+1].s <= b[0]) j++;
    if(j < 0){ pts.push({time:b[0]}); continue; }
    if(j !== pj){
      if(pj >= 0){ const q = pts[pts.length-1]; if(q && q.value !== undefined) q.color = TXGAP; }    // 換段處斷開
      mk.push({time:b[0], position:(k === 'up' ? 'aboveBar' : 'belowBar'), color:TXUDC[k], shape:'circle',
               text:(k === 'up' ? '上 ' : '下 ')+segs[j].d.slice(0, 5)});
      pj = j;
    }
    pts.push({time:b[0], value:segs[j].v});
  }
  return {pts:pts, mk:mk};
}
function txUdApply(){
  const C = TXCH; if(!C || !C.uu) return; const R = TXDATA[C.key];
  const on = TXUD === '1' && !txIsC(TXS) && R && R.bars && R.bars.length;
  for(const [k, sr] of [['up', C.uu], ['dn', C.ud]]){
    for(const pl of (C.udpl[k] || [])){ try{ sr.removePriceLine(pl); }catch(e){} }
    C.udpl[k] = [];
    if(!on){ sr.setData([]); sr.setMarkers([]); continue; }
    const segs = txUdSegs(k), r = txUdData(R.bars, segs, k); sr.setData(r.pts); C.udm[k] = r.mk;
    // ★目前生效的那條：整條橫跨圖面的虛線＋價格軸標籤（跟麥門軟體一樣）。
    //   用戶 10-11：「你哪有畫到加權」——10-10 晚上設的線，後面還沒有 K 棒，分段畫不出來 → 現值一律用橫線顯示
    const cur = segs[segs.length - 1];
    if(cur && sr.createPriceLine) C.udpl[k].push(sr.createPriceLine({price:cur.v, color:TXUDC[k], lineWidth:1, lineStyle:2,
      axisLabelVisible:true, title:(k === 'up' ? '上線 ' : '下線 ') + cur.d.slice(0, 5)}));
  }
  if(on) txUdMarks();
}
// 段首標籤：縮小時很密會疊在一起（用戶：同時太多會不知道什麼時間點）→ 跟前一個標籤距離不到 60px 的只留點不寫字；縮放後重排
function txUdMarks(){
  const C = TXCH; if(!C || !C.uu || TXUD !== '1' || txIsC(TXS)) return; const ts = C.ch.timeScale();
  for(const [k, sr] of [['up', C.uu], ['dn', C.ud]]){
    let lastX = -1e9; const out = [];
    for(const m of (C.udm[k] || [])){
      const x = ts.timeToCoordinate(m.time), show = x !== null && x - lastX >= 60;
      if(show) lastX = x;
      out.push(Object.assign({}, m, {text: show ? m.text : ''}));
    }
    sr.setMarkers(out);
  }
}
function txUdLeg(t){
  if(TXUD !== '1' || txIsC(TXS)) return '';
  const u = txUdAt(txUdSegs('up'), t), d = txUdAt(txUdSegs('dn'), t);
  return (u ? ' <span style="color:'+TXUDC.up+'">上線 '+Math.round(u.v)+'</span>' : '') + (d ? ' <span style="color:'+TXUDC.dn+'">下線 '+Math.round(d.v)+'</span>' : '');
}
// ★威廉指標／支撐壓力／型態（2026-10-14，用戶：「幫我在網頁上新增威廉指標和開關」「在圖上畫支撐壓力 一樣要有開關 型態可以的話一樣加上去」）
//   ・威廉 %R(14)＝(14 根最高 − 收) ÷ (14 根最高 − 14 根最低) × −100；−20 以上超買、−80 以下超賣（4J 10/11~13 直播用的設定）。畫在量的下面。
//   ・支撐壓力＝用戶第 9~10 輪看圖判過的那套（上層 _lib_swing_pats.py）：明顯轉折＝zigzag 2×ATR(14)；近 200 根內、價差 ≤0.5×ATR 的
//     轉折高點（壓力）或低點（支撐）≥2 個圍成一區（上下緣＝那幾個影線的最高／最低）。只畫到最新收盤都還沒被收盤穿過的區。
//   ・型態＝用戶判過 8 成以上的：壓力區突破（17/21）、支撐區跌破（21/21）、上升／下降三角形（教科書版 17/18、16/18，上層 _lib_tri3.py）；
//     濾網同回測（型態高度 ≥2.5 ATR、突破那根量 ≥ 前 30 根均量）；只用已收盤的 K（盤中那根不算）。
//   ・<txsr> ~ </txsr> 之間的函數由上層 _chk_txsr_port.py 抽出來，跟 Python 原碼在同一批 K 上逐筆對拍。
//   ★只在加密貨幣 4H 判過／回測過（型態本身扣費後≈不賺），其他週期、台指加權沒驗證。
// <txsr>
function txAtrW(h, l, c, n){            // = _lib_dhx_patterns.atr：前 n 根用 TR 平均當起點，之後 Wilder 遞迴
  const N = h.length, tr = new Array(N), out = new Array(N);
  for(let i = 0; i < N; i++){ const pc = i ? c[i-1] : c[0]; tr[i] = Math.max(h[i] - l[i], Math.abs(h[i] - pc), Math.abs(l[i] - pc)); }
  const m = Math.min(n, N); let s = 0; for(let i = 0; i < m; i++) s += tr[i];
  for(let i = 0; i < m; i++) out[i] = s / m;
  for(let i = n; i < N; i++) out[i] = (out[i-1] * (n - 1) + tr[i]) / n;
  return out;
}
function txZig(h, l, a, k){             // = _lib_dhx_patterns.zigzag：[極值idx, 價, +1高/-1低, 確認idx]
  const piv = []; let d = 0, hi_i = 0, hi_p = h[0], lo_i = 0, lo_p = l[0], ex_i = 0, ex_p = 0;
  for(let i = 1; i < h.length; i++){
    if(d === 0){
      if(h[i] > hi_p){ hi_i = i; hi_p = h[i]; }
      if(l[i] < lo_p){ lo_i = i; lo_p = l[i]; }
      if(h[i] >= lo_p + k * a[i]){ piv.push([lo_i, lo_p, -1, i]); d = 1; ex_i = i; ex_p = h[i]; }
      else if(l[i] <= hi_p - k * a[i]){ piv.push([hi_i, hi_p, 1, i]); d = -1; ex_i = i; ex_p = l[i]; }
    } else if(d === 1){
      if(h[i] > ex_p){ ex_i = i; ex_p = h[i]; }
      else if(l[i] <= ex_p - k * a[i]){ piv.push([ex_i, ex_p, 1, i]); d = -1; ex_i = i; ex_p = l[i]; }
    } else {
      if(l[i] < ex_p){ ex_i = i; ex_p = l[i]; }
      else if(h[i] >= ex_p + k * a[i]){ piv.push([ex_i, ex_p, -1, i]); d = 1; ex_i = i; ex_p = h[i]; }
    }
  }
  return piv;
}
function txWr(h, l, c, n){              // 威廉 %R
  const o = new Array(c.length).fill(null);
  for(let i = n - 1; i < c.length; i++){
    let hh = -Infinity, ll = Infinity;
    for(let j = i - n + 1; j <= i; j++){ if(h[j] > hh) hh = h[j]; if(l[j] < ll) ll = l[j]; }
    o[i] = hh > ll ? (hh - c[i]) / (hh - ll) * -100 : null;
  }
  return o;
}
function txGroups(P, a, t, sgn){        // = _lib_swing_pats._cands 區的那段：近 200 根、同種轉折照價排序，相鄰差 >0.5×ATR 就分組，≥2 個才算區
  const S = P.filter(p => p[2] === sgn && p[0] >= t - 200).sort((x, y) => x[1] - y[1]), G = []; let cur = [];
  for(const p of S){ if(cur.length && p[1] - cur[cur.length-1][1] > 0.5 * a[t]){ G.push(cur); cur = []; } cur.push(p); }
  if(cur.length) G.push(cur);
  return G.filter(g => g.length >= 2);
}
function txVma(v, t){ if(t < 30) return NaN; let s = 0; for(let j = t - 30; j < t; j++) s += v[j]; return s / 30; }   // 前 30 根均量（不含當根）
function txZoneEv(h, l, c, v, a, piv, n){   // = _lib_swing_pats.detect 的 壓力區突破／支撐區跌破
  const ev = [], done = {}, P = []; let j = 0;
  for(let t = 40; t < n; t++){
    while(j < piv.length && piv[j][3] <= t - 1){ P.push(piv[j]); j++; }
    if(P.length < 4) continue;
    for(const sgn of [1, -1]) for(const g of txGroups(P, a, t, sgn)){
      let lo = Infinity, hi = -Infinity, first = Infinity, lastp = -1;
      for(const p of g){ lo = Math.min(lo, p[1]); hi = Math.max(hi, p[1]); first = Math.min(first, p[0]); lastp = Math.max(lastp, p[0]); }
      const edge = sgn > 0 ? hi : lo, key = sgn + '|' + first;
      if(done[key]) continue;
      let opp = null; for(const p of P) if(p[2] === -sgn && p[0] < t) opp = p;
      if(!opp) continue;
      if(!((c[t] - edge) * sgn > 0 && (c[t-1] - edge) * sgn <= 0)) continue;
      let crossed = false; for(let m = first + 1; m < t; m++) if((c[m] - edge) * sgn > 0){ crossed = true; break; }
      if(crossed) continue;
      let ext = sgn > 0 ? Infinity : -Infinity; for(let m = first; m < t; m++) ext = sgn > 0 ? Math.min(ext, l[m]) : Math.max(ext, h[m]);
      const stop = opp[1], height = Math.abs(edge - ext);
      if((c[t] - stop) * sgn <= 0 || !(height > 0)) continue;
      done[key] = 1; const vm = txVma(v, t);
      ev.push({i:t, pat:(sgn > 0 ? '壓力區突破' : '支撐區跌破'), dir:sgn, level:edge, stop:stop, height:height, hatr:height / a[t],
               volx:(vm > 0 ? v[t] / vm : NaN), lo:lo, hi:hi, first:first, lastp:lastp, nz:g.length});
    }
  }
  return ev;
}
function txTriCollect(P6){              // = _lib_tri3._collect（只看最近 6 個轉折，三種都收、同數量時上升/下降優先）
  let best = null;
  const span = A => Math.max(...A.map(p => p[1])) - Math.min(...A.map(p => p[1]));
  for(const pat of ['上升三角', '下降三角', '對稱三角']){
    let k = P6.length;
    while(k > 0){
      const S = P6.slice(k - 1), Hs = S.filter(p => p[2] > 0), Ls = S.filter(p => p[2] < 0);
      if(Hs.length >= 1 && Ls.length >= 1){
        const rng = span(S); let okH = true, okL = true;
        if(pat !== '上升三角'){ for(let m = 0; m < Hs.length - 1; m++) if(!(Hs[m+1][1] < Hs[m][1])) okH = false; } else okH = span(Hs) <= 0.10 * rng;
        if(pat !== '下降三角'){ for(let m = 0; m < Ls.length - 1; m++) if(!(Ls[m+1][1] > Ls[m][1])) okL = false; } else okL = span(Ls) <= 0.10 * rng;
        if(!(okH && okL)) break;
      }
      k--;
    }
    const S = P6.slice(k), Hs = S.filter(p => p[2] > 0), Ls = S.filter(p => p[2] < 0);
    if(S.length >= 6 && Hs.length >= 2 && Ls.length >= 2
       && (pat === '對稱三角' || (pat === '上升三角' && Ls[Ls.length-1][1] > Ls[0][1]) || (pat === '下降三角' && Hs[Hs.length-1][1] < Hs[0][1]))
       && (best === null || S.length > best[1].length)) best = [pat, S];
  }
  return best;
}
function txTriEv(h, l, c, v, a, piv, n){    // = _lib_tri3.detect
  const ev = [], done = {}, P = []; let j = 0;
  for(let t = 30; t < n; t++){
    while(j < piv.length && piv[j][3] <= t - 1){ P.push(piv[j]); j++; }
    if(P.length < 6) continue;
    const r = txTriCollect(P.slice(-6)); if(!r) continue;
    const pat = r[0], S = r[1], key = pat + '|' + S[0][0];
    if(done[key]) continue;
    const Hs = S.filter(p => p[2] > 0), Ls = S.filter(p => p[2] < 0), last = S[S.length-1][0];
    const hlv = pat === '上升三角' ? Math.max(...Hs.map(p => p[1])) : Hs[Hs.length-1][1];
    const llv = pat === '下降三角' ? Math.min(...Ls.map(p => p[1])) : Ls[Ls.length-1][1];
    const height = S[0][2] > 0 ? S[0][1] - S[1][1] : S[1][1] - S[0][1];
    const sw = []; for(let m = 0; m < S.length - 1; m++) sw.push(Math.abs(S[m+1][1] - S[m][1]));
    if(pat === '對稱三角' && sw[sw.length-1] > 0.5 * sw[1]) continue;
    for(const [dr, lv, stop] of [[1, hlv, Ls[Ls.length-1][1]], [-1, llv, Hs[Hs.length-1][1]]]){
      if((pat === '上升三角' && dr < 0) || (pat === '下降三角' && dr > 0)) continue;
      if(!((c[t] - lv) * dr > 0 && (c[t-1] - lv) * dr <= 0)) continue;
      let crossed = false; for(let m = last + 1; m < t; m++) if((c[m] - lv) * dr > 0){ crossed = true; break; }
      if(crossed) continue;
      if((c[t] - stop) * dr <= 0) continue;
      const vm = txVma(v, t);
      ev.push({i:t, pat:(pat === '對稱三角' ? (dr > 0 ? '對稱三角↑' : '對稱三角↓') : pat), dir:dr, level:lv, stop:stop, height:Math.abs(height),
               hatr:Math.abs(height) / a[t], volx:(vm > 0 ? v[t] / vm : NaN), pts:S.map(p => [p[0], p[1], p[2]])});
      done[key] = 1; break;
    }
  }
  return ev;
}
function txActiveZones(h, l, c, a, piv, n){  // 最新一根收盤時還有效的區：壓力＝第一個轉折之後沒收盤站上上緣、支撐＝沒收盤跌破下緣
  const t = n - 1; if(t < 40) return [];
  const P = piv.filter(p => p[3] <= t), out = [];
  for(const sgn of [1, -1]) for(const g of txGroups(P, a, t, sgn)){
    let lo = Infinity, hi = -Infinity, first = Infinity;
    for(const p of g){ lo = Math.min(lo, p[1]); hi = Math.max(hi, p[1]); first = Math.min(first, p[0]); }
    let ok = true; for(let m = first + 1; m <= t; m++) if(sgn > 0 ? c[m] > hi : c[m] < lo){ ok = false; break; }
    if(ok) out.push({sgn:sgn, lo:lo, hi:hi, first:first, n:g.length});
  }
  return out;
}
function txStudyArr(h, l, c, v, n){         // 已收盤的前 n 根 → {zones, pats, life}（濾網同回測）
  const hh = h.slice(0, n), ll = l.slice(0, n), cc = c.slice(0, n), vv = v.slice(0, n);
  const a = txAtrW(hh, ll, cc, 14), piv = txZig(hh, ll, a, 2.0);
  const ze = txZoneEv(hh, ll, cc, vv, a, piv, n).filter(e => e.hatr >= 2.5 && e.volx >= 1);
  const te = txTriEv(hh, ll, cc, vv, a, piv, n).filter(e => (e.pat === '上升三角' || e.pat === '下降三角') && e.hatr >= 2.5 && e.volx >= 1);
  return {zones:txActiveZones(hh, ll, cc, a, piv, n), pats:ze.concat(te).sort((x, y) => x.i - y.i), life:txZoneLife(hh, ll, cc, a, piv, n), atr:a[n - 1]};
}
// </txsr>
// ★畫圖用的「區的一生」（用戶 10-14：「支撐壓力照我之前要你判斷的圖那種 支撐用綠色 壓力用淡紫色 不用寫支撐壓力
//   如果是有支撐壓力互換 或是回來測試的 那就延長」）。區的定義同上（txGroups）；形狀在第一次被收盤穿過時凍結。
//   ★延長規則是我定的（他沒給細節）：從沒被穿過＝畫到最右邊；被收盤穿過＝角色互換（顏色跟著換）；
//    互換後價格回來碰到區＝延長到那一根，之後到現在都沒再被反穿＝延長到最右邊；穿過後再也沒回來＝畫到穿過那根為止。
//   每一段 [起, 迄, 角色(+1 壓力/-1 支撐), 是否到最右邊]。區成形前價格就已經收盤穿過的，不算區。
// ★畫圖用的分組要「整個區」價差 ≤0.5 ATR（用戶 10-14：「那個大綠框是什麼鬼」——原本 txGroups 是相鄰兩個差 ≤0.5 ATR 就串起來，
//   6 個低點一路串成 2,200 點寬的區；規則原文是「價差 ≤0.5 ATR 的轉折圍成一區」）。型態偵測 txZoneEv 不動（那是跟回測對拍過的版本）。
function txGroupsTight(P, a, t, sgn){
  const S = P.filter(p => p[2] === sgn && p[0] >= t - 200).sort((x, y) => x[1] - y[1]), G = []; let cur = [];
  for(const p of S){ if(cur.length && p[1] - cur[0][1] > 0.5 * a[t]){ G.push(cur); cur = []; } cur.push(p); }
  if(cur.length) G.push(cur);
  return G.filter(g => g.length >= 2);
}
function txZoneLife(h, l, c, a, piv, n){
  const Z = {}, P = [], out = []; let j = 0;
  for(let t = 40; t < n; t++){
    while(j < piv.length && piv[j][3] <= t - 1){ P.push(piv[j]); j++; }
    if(P.length >= 4) for(const sgn of [1, -1]) for(const g of txGroupsTight(P, a, t, sgn)){
      let lo = Infinity, hi = -Infinity, first = Infinity;
      for(const p of g){ lo = Math.min(lo, p[1]); hi = Math.max(hi, p[1]); first = Math.min(first, p[0]); }
      const key = sgn + '|' + first, z = Z[key];
      if(z && (z.bad || z.broke >= 0)) continue;
      const edge = sgn > 0 ? hi : lo; let crossed = false;
      for(let m = first + 1; m < t; m++) if((c[m] - edge) * sgn > 0){ crossed = true; break; }
      if(crossed){ if(!z) Z[key] = {bad:true}; continue; }
      if(!z) Z[key] = {sgn:sgn, lo:lo, hi:hi, first:first, n:g.length, pts:g.map(p => [p[0], p[1]]), from:t, broke:-1};
      else { z.lo = lo; z.hi = hi; z.n = g.length; z.pts = g.map(p => [p[0], p[1]]); }
    }
    for(const k in Z){ const z = Z[k]; if(!z.bad && z.broke < 0 && (z.sgn > 0 ? c[t] > z.hi : c[t] < z.lo)) z.broke = t; }
  }
  for(const k in Z){
    const z = Z[k]; if(z.bad) continue;
    // ★大小門檻＝判圖時同一道（用戶 10-14：「在很小的盤整內認支撐壓力」）：區的邊到「第一個轉折之後的另一邊極端」≥2.5 ATR；
    //   被穿過的區用穿過那根的 ATR、還沒穿過的用最新一根（＝型態偵測 hatr 的算法）
    const te = z.broke >= 0 ? z.broke : n - 1; let ext = z.sgn > 0 ? Infinity : -Infinity;
    for(let m = z.first; m < te; m++) ext = z.sgn > 0 ? Math.min(ext, l[m]) : Math.max(ext, h[m]);
    if(!(Math.abs((z.sgn > 0 ? z.hi : z.lo) - ext) >= 2.5 * a[te])) continue;
    const seg = []; let role = z.sgn, s0 = z.first, tested = false, flips = 0, act = Math.max(...z.pts.map(p => p[0]));   // act＝最後一次有動靜（轉折／碰到／穿過）
    for(let m = z.from; m < n; m++){
      if(role > 0 ? c[m] > z.hi : c[m] < z.lo){ seg.push([s0, m, role, false]); role = -role; s0 = m; tested = false; flips++; act = m; continue; }
      if(m > s0 && (role > 0 ? h[m] >= z.lo : l[m] <= z.hi)){ act = m; if(flips) tested = true; }
    }
    // 延長上限（★我定的）：最後一次動靜之後最多再延 200 根（＝區本身「近 200 根」的定義）；超過就停在那裡，不再拉到最右邊
    if(!flips || tested){ const e = Math.min(n - 1, act + 200); seg.push([s0, e, role, e === n - 1]); }
    out.push({sgn:z.sgn, lo:z.lo, hi:z.hi, first:z.first, n:z.n, pts:z.pts, seg:seg, role:role, live:((!flips || tested) && act + 200 >= n - 1)});
  }
  // ★價位重疊、時間也重疊的區只留碰最多次的那個（★我定的；用戶 10-14：「在很小的盤整內認支撐壓力」＝大區裡又包小區）
  out.sort((x, y) => y.n - x.n || (y.hi - y.lo) - (x.hi - x.lo));
  const keep = [], span = z => [z.first, z.seg.length ? z.seg[z.seg.length - 1][1] : z.first];
  for(const z of out){
    const [a0, a1] = span(z);
    if(keep.some(k => { const [b0, b1] = span(k); return z.lo <= k.hi && z.hi >= k.lo && a0 <= b1 && b0 <= a1; })) continue;
    keep.push(z);
  }
  return keep.sort((x, y) => x.first - y.first);
}
const TXSTC = {res:'#e5484d', sup:'#30a46c', wr:'#b48ef0'};     // 用戶 10-14：支撐綠、壓力紅（淡紫會跟階梯撞色）
let TXWR = '1', TXSR = '1', TXPT = '1';
try{ const a = localStorage.getItem('txwr'), b = localStorage.getItem('txsr'), d = localStorage.getItem('txpt');
  if(a === '0' || a === '1') TXWR = a; if(b === '0' || b === '1') TXSR = b; if(d === '0' || d === '1') TXPT = d; }catch(e){}
function txTgl(k){
  if(k === 'wr') TXWR = TXWR === '1' ? '0' : '1'; else if(k === 'sr') TXSR = TXSR === '1' ? '0' : '1'; else TXPT = TXPT === '1' ? '0' : '1';
  try{ localStorage.setItem('txwr', TXWR); localStorage.setItem('txsr', TXSR); localStorage.setItem('txpt', TXPT); }catch(e){}
  if(TXCH && TXDATA[TXCH.key]){ try{ txStApply(); txHiLo(); txLegend(null); }catch(e){} }
  draw();
}
function txTglBar(){ return [['wr', '威廉%R', TXWR], ['sr', '支撐壓力', TXSR], ['pt', '型態', TXPT]]
  .map(([k, n, v]) => `<span class="wb ${v==='1'?'on':''}" onclick="txTgl('${k}')">${n}</span>`).join(' '); }
// 每份 K 棒算一次（K 數或最後一根收盤變了才重算）。幣：OKX 第 9 欄 '1'＝收完；台指/加權沒有這欄 → 最後一根當盤中不算型態。
function txStudy(R){
  const bars = R.bars, N = bars.length; if(!N) return null;
  const lc = bars[N-1][4];
  if(R.st && R.st.N === N && R.st.lc === lc && R.st.t0 === bars[0][0]) return R.st;
  const h = bars.map(b => b[2]), l = bars.map(b => b[3]), c = bars.map(b => b[4]), v = bars.map(b => Number(b[5]) || 0);
  const live = (R.raw && R.raw[N-1]) ? R.raw[N-1][8] !== '1' : true, n = live ? N - 1 : N;
  const st = {N:N, lc:lc, t0:bars[0][0], wr:txWr(h, l, c, 14), zones:[], pats:[]};
  if(n > 60){ const r = txStudyArr(h, l, c, v, n); st.zones = r.zones; st.pats = r.pats; st.life = r.life; }
  R.st = st; return st;
}
function txStApply(){
  const C = TXCH; if(!C || !C.wr) return; const R = TXDATA[C.key];
  const st = (R && R.bars && R.bars.length) ? txStudy(R) : null, on = TXWR === '1' && !!st;
  C.wr.setData(on ? R.bars.map((b, i) => st.wr[i] === null ? {time:b[0]} : {time:b[0], value:st.wr[i]}) : []);
  C.cs.priceScale().applyOptions({scaleMargins: on ? {top:0.06, bottom:0.36} : {top:0.08, bottom:0.22}});
  C.ch.priceScale('vol').applyOptions({scaleMargins: on ? {top:0.66, bottom:0.2} : {top:0.82, bottom:0}});
  txSrDraw();
}
// 支撐壓力區＝半透明框（從第一個轉折畫到右邊）；型態＝區突破畫虛線框到突破那根、三角形畫頸線（平的）＋另一邊轉折的回歸線。縮放後重畫。
function txSrDraw(){
  const C = TXCH; if(!C || !C.sr) return; const R = TXDATA[C.key];
  if(!R || !R.bars || !R.bars.length || (TXSR !== '1' && TXPT !== '1' && TXWR !== '1')){ C.sr.innerHTML = ''; return; }
  const st = txStudy(R), ts = C.ch.timeScale(), cs = C.cs;
  if(!st || typeof ts.logicalToCoordinate !== 'function' || typeof cs.priceToCoordinate !== 'function'){ C.sr.innerHTML = ''; return; }
  const W = C.sr.clientWidth || Math.max(0, (C.box.clientWidth || 0) - 60), prec = R.meta ? R.meta.prec : 2;
  if(!(W > 0)){ C.sr.innerHTML = ''; return; }          // 圖剛建立還沒有寬度 → 等尺寸回呼（redo）再畫
  const X = i => ts.logicalToCoordinate(i), Y = p => cs.priceToCoordinate(p), bad = q => q === null || q === undefined || isNaN(q);
  let g = '', gw = '';
  // %R 跟 K 線共用右邊的價格軸（程式庫 4.x 不能分窗格）→ 在 −20／−80 虛線上直接寫數字，免得把右軸的價格當成 %R
  if(TXWR === '1' && C.wr && typeof C.wr.priceToCoordinate === 'function') for(const [p, t] of [[-20, '-20'], [-80, '-80']]){     // 用戶：不用寫超買超賣
    const y = C.wr.priceToCoordinate(p); if(!bad(y)) gw += '<text x="44" y="'+(y - 2)+'" font-size="9" fill="'+txAlpha(TXSTC.wr, 0.85)+'">'+t+'</text>';   // x=44：左下角有程式庫的標誌
  }
  // 區與型態只畫在 K 線那一塊（下面是量和 %R）：K 線區下緣＝圖高 ×（1 − K 線的下邊界比例）
  const Hb = C.box.clientHeight, yMax = Hb > 0 ? Hb * (1 - (TXWR === '1' ? 0.36 : 0.22)) : 1e6;
  // 支撐壓力（用戶 10-14 第二版：「我不要這樣 太亂太醜了」）：只畫「現在還有效」的區（沒被穿過，或互換後回測過、到現在還守住），
  //   一個區一條帶（帶狀＋上下緣＋轉折點，照判圖時的畫法），從第一個轉折畫到最右邊，顏色＝現在的角色（壓力紅、支撐綠），不寫字；
  //   ★只留離現價最近的壓力 2 個、支撐 2 個（我定的）。已經結束的舊區不畫。
  //   用戶 10-14 第三版：「舊的我也要看啊」→ 舊的區（已經結束的，或互換前那一段）只畫上下兩條淡線、不填色；現在有效的才填色。
  if(TXSR === '1'){
    const px = R.bars[R.bars.length - 1][4], L = (st.life || []).filter(z => z.live);
    const res = L.filter(z => z.role > 0).sort((x, y) => Math.abs(x.lo - px) - Math.abs(y.lo - px)).slice(0, 2);
    const sup = L.filter(z => z.role < 0).sort((x, y) => Math.abs(x.hi - px) - Math.abs(y.hi - px)).slice(0, 2), on = res.concat(sup);
    for(const z of (st.life || [])){
      const y1 = Y(z.hi), y2 = Y(z.lo); if(bad(y1) || bad(y2)) continue;
      const top = Math.min(y1, y2), hg = Math.max(2, Math.abs(y2 - y1)); let drawn = false;
      for(const s of z.seg){
        const now = s[3] && on.indexOf(z) >= 0, xa = X(s[0]), xb = s[3] ? W : X(s[1]); if(bad(xa) || bad(xb) || xb < 0 || xa > W) continue;
        const col = s[2] > 0 ? TXSTC.res : TXSTC.sup, x0 = Math.max(0, xa), x1 = Math.min(W, xb); if(x1 <= x0) continue;
        const la = now ? 0.75 : 0.4; drawn = true;
        if(now) g += '<rect x="'+x0+'" y="'+top+'" width="'+(x1 - x0)+'" height="'+hg+'" fill="'+txAlpha(col, 0.16)+'" stroke="none"/>';
        g += '<line x1="'+x0+'" y1="'+top+'" x2="'+x1+'" y2="'+top+'" stroke="'+txAlpha(col, la)+'" stroke-width="1"/>'
           + '<line x1="'+x0+'" y1="'+(top + hg)+'" x2="'+x1+'" y2="'+(top + hg)+'" stroke="'+txAlpha(col, la)+'" stroke-width="1"/>';
      }
      if(!drawn) continue;
      const c0 = z.sgn > 0 ? TXSTC.res : TXSTC.sup, ca = on.indexOf(z) >= 0 ? 1 : 0.55;
      for(const p of z.pts){ const xp = X(p[0]), yp = Y(p[1]); if(!bad(xp) && !bad(yp) && xp >= 0 && xp <= W) g += '<circle cx="'+xp+'" cy="'+yp+'" r="2.4" fill="'+txAlpha(c0, ca)+'"/>'; }
    }
  }
  if(TXPT === '1') for(const e of st.pats){
    const col = e.dir > 0 ? '#ef4444' : '#22c55e', x1 = X(e.i); if(bad(x1)) continue;
    if(e.lo !== undefined){     // 被突破的區（只在支撐壓力關著時畫虛線框；開著時區本身已經畫了）：從最後一次碰到畫到突破那根
      if(TXSR === '1') continue;
      const x0 = X(e.lastp !== undefined ? e.lastp : e.first), y1 = Y(e.hi), y2 = Y(e.lo); if(bad(x0) || bad(y1) || bad(y2) || x1 < 0 || x0 > W) continue;
      g += '<rect x="'+x0+'" y="'+Math.min(y1, y2)+'" width="'+Math.max(1, x1 - x0)+'" height="'+Math.max(2, Math.abs(y2 - y1))+'" fill="none" stroke="'+col+'" stroke-width="1.3" stroke-dasharray="4,3"/>';
    } else {
      const Hs = e.pts.filter(p => p[2] > 0), Ls = e.pts.filter(p => p[2] < 0), flat = e.pat === '上升三角' ? Hs : Ls, sl = e.pat === '上升三角' ? Ls : Hs;
      const n = sl.length, mx = sl.reduce((s, p) => s + p[0], 0) / n, my = sl.reduce((s, p) => s + p[1], 0) / n;
      const vx = sl.reduce((s, p) => s + (p[0] - mx) * (p[0] - mx), 0), k = vx > 0 ? sl.reduce((s, p) => s + (p[0] - mx) * (p[1] - my), 0) / vx : 0;
      const xf = X(flat[0][0]), xs = X(sl[0][0]), yl = Y(e.level), ys0 = Y(my + k * (sl[0][0] - mx)), ys1 = Y(my + k * (e.i - mx));
      if([xf, xs, yl, ys0, ys1].some(bad) || x1 < 0) continue;
      g += '<line x1="'+xf+'" y1="'+yl+'" x2="'+x1+'" y2="'+yl+'" stroke="'+col+'" stroke-width="1.6"/>'
         + '<line x1="'+xs+'" y1="'+ys0+'" x2="'+x1+'" y2="'+ys1+'" stroke="'+col+'" stroke-width="1.6"/>';
    }
  }
  C.sr.innerHTML = (g || gw) ? '<svg width="100%" height="100%"><defs><clipPath id="txsrclip"><rect x="0" y="0" width="'+W+'" height="'+yMax+'"/></clipPath></defs>'
    + '<g clip-path="url(#txsrclip)">'+g+'</g>'+gw+'</svg>' : '';
}
function txPtMarks(R){
  if(TXPT !== '1' || !R) return []; const st = txStudy(R); if(!st) return [];
  return st.pats.filter(e => R.bars[e.i]).map(e => ({time:R.bars[e.i][0], position:(e.dir > 0 ? 'belowBar' : 'aboveBar'), color:(e.dir > 0 ? '#ef4444' : '#22c55e'),
    shape:(e.dir > 0 ? 'arrowUp' : 'arrowDown'), text:e.pat}));
}
function txBadge(c){ return '<span class="txb" style="background:'+(TXC[c]||'#555')+'">'+(TXN[c]||'?')+'</span>'; }
function txAlpha(hex, a){ const n = parseInt(String(hex).slice(1), 16); return 'rgba('+((n>>16)&255)+','+((n>>8)&255)+','+(n&255)+','+a+')'; }
function txP2(n){ return String(n).padStart(2, '0'); }
function txTime(t, intra){ const d = new Date(t*1000);
  return intra ? txP2(d.getUTCMonth()+1)+'-'+txP2(d.getUTCDate())+' '+txP2(d.getUTCHours())+':'+txP2(d.getUTCMinutes())
               : d.getUTCFullYear()+'-'+txP2(d.getUTCMonth()+1)+'-'+txP2(d.getUTCDate()); }
function txTick(t, k){ const d = new Date(t*1000);
  if(k===0) return String(d.getUTCFullYear());
  if(k===1) return (d.getUTCMonth()+1)+'月';
  if(k===2) return (d.getUTCMonth()+1)+'/'+d.getUTCDate();
  return txP2(d.getUTCHours())+':'+txP2(d.getUTCMinutes()); }
function txNum(v, prec){ return (v===null||v===undefined||isNaN(v)) ? '—' : Number(v).toFixed(prec); }
function txIntra(){ return TXTF==='15' || TXTF==='60' || TXTF==='240'; }
function txTfs(){ return Object.keys(TXTFN).filter(k => k !== '240' || txIsC(TXS)); }
// 台股慣例：漲紅跌綠
function txChg(v, prec){ return '<span style="color:'+(v>0?'#ef4444':(v<0?'#22c55e':'var(--dim)'))+'">'+(v>0?'+':'')+txNum(v, prec)+'</span>'; }

function txCandle(b){ const c = TXC[b[6]] || '#888888', up = b[4] >= b[1];
  return {time:b[0], open:b[1], high:b[2], low:b[3], close:b[4], color: up ? c : txAlpha(c, 0.4), borderColor:c, wickColor:c}; }
function txVol(b){ return {time:b[0], value:b[5], color: txAlpha(TXC[b[6]] || '#888888', 0.45)}; }
// 階梯分段：選到的參數（idx）同一個值連續的 K 棒＝一段；每段最後一個點 color 透明 → 連到下一段的那一小段看不見
//   回傳 2 條的資料（值+9、值−9＝麥門的淡紫雙線），每條每根都有點（沒值給空白點）
const TXGAP = 'rgba(0,0,0,0)';
function txLadSeg(bars, half, idx){
  const out = [[], []]; let prev = null;
  for(const b of bars){
    const t = b[0], v = b[idx];
    if(v === null || v === undefined){ for(const s of out) s.push({time:t}); prev = null; continue; }
    if(prev !== null && v !== prev) for(const s of out){ const q = s[s.length - 1]; if(q && q.value !== undefined) q.color = TXGAP; }
    prev = v;
    out[0].push({time:t, value:v + half}); out[1].push({time:t, value:v - half});
  }
  return out;
}

// ★幣（OKX 永續，2026-10-11）：K 棒由手機直接跟 OKX 公開 API 拿（實測 CORS 有開）→ bot 不多做任何事、不佔 bot 的 API 額度。
//   彩色K＝台指「公式版」（用戶：照做、先用他的參數）。模型版是拿台指標註訓練的，幣沒有標註可以驗 → 用公式。
//   階梯＝用 CME 比特幣期貨的交易時段當窗口（17:00~隔天 16:00 芝加哥時間；週末、假日也照平日每天算——用戶 10-11），
//   算「這個合約自己的」VWAP 再畫回合約（用戶：時間用他的開盤時間，計算後畫到合約）。
//   參數二＝整個 CME 交易日；參數一＝美股開盤後 1 小時 45 分（紐約 09:30~11:15＝芝加哥 08:30~10:15，對照台指＝加權開盤 09:00~10:45）；16:00 收盤換階。
//   ★參數一原本取 CME 開盤 17:00 起，用戶：「在全球這絕對不是交易量最大的時間」→ 實測 OKX BTC/ETH/SOL 60 天 15 分成交額（_mm_data/okx_vol_profile.py）：
//     最大的 105 分三個幣都是芝加哥 08:30~10:15（占全天 18.7/17.5/15.5%，第 1 名）；17:00 起只有 3.3~4.7%（96 段排 86~91）。
//   VWAP＝Σ成交額(USDT)÷Σ成交量（OKX 每根 K 都有這兩欄 → 是真的成交均價，不是拿收盤價近似）。
//   雙線寬度＝值 ±0.02%（台指 ±9 點 ÷ 約 4.8 萬）。
//   幣的 K 棒列 = [時間秒(台北當UTC), 開, 高, 低, 收, 量(幣), 顏色, 階梯P2, 階梯P1, 0, CME標記('o'開/'c'收/0)]
const TXOKX = 'https://www.okx.com/api/v5/market/candles';
const TXBAR = {'15':'15m', '60':'1H', '240':'4H', 'D':'1D', 'W':'1W'};
const TXHALF = 0.0002, TXWARM = 280;
const TXCTF = (() => { try{ return new Intl.DateTimeFormat('en-US', {timeZone:'America/Chicago', hourCycle:'h23',
  year:'numeric', month:'2-digit', day:'2-digit', hour:'2-digit', minute:'2-digit'}); }catch(e){ return null; } })();
function txIsC(s){ return String(s || '').indexOf('C:') === 0; }
function txCoin(s){ return String(s || '').slice(2).replace('-USDT-SWAP', ''); }
// 芝加哥時間（夏令/冬令由瀏覽器時區資料處理）→ [日期 'YYYY-MM-DD', 當天第幾分鐘]
function txCt(ms){
  const o = {}; for(const p of TXCTF.formatToParts(new Date(ms))) o[p.type] = p.value;
  return [o.year+'-'+o.month+'-'+o.day, Number(o.hour)*60 + Number(o.minute)];
}
function txDayAdd(ymd, k){ const d = new Date(ymd+'T00:00:00Z'); d.setUTCDate(d.getUTCDate() + k); return d.toISOString().slice(0, 10); }
// 這一刻屬於哪個「交易日」：17:00 之後算隔天；16:00~17:00＝CME 休息 → null。週末假日照平日算（幣 24 小時在交易）
function txCmeDay(ymd, mn){
  if(mn >= 960 && mn < 1020) return null;
  return mn >= 1020 ? txDayAdd(ymd, 1) : ymd;
}
// 盤別虛線用：這根是 CME 開盤時刻（17:00）或收盤時刻（16:00＝換階），每天都有
function txCmeMark(ms){
  const mn = txCt(ms)[1];
  return mn === 1020 ? 'o' : (mn === 960 ? 'c' : 0);
}
// 換階事件：每個 CME 交易日收盤後那一根起生效。第一個交易日資料不是從 17:00 開始（只有半天）→ 不算。
//   參數一（芝加哥 08:30~10:15）只有 15 分 K 切得出來；1H K 只算參數二（17:00~16:00 整點切得齊，加總跟 15 分一樣）。
function txCmeEvents(raw, withP1){
  const ev = []; let cur = null;
  for(const r of raw){
    const cm = txCt(r[7]), ymd = cm[0], mn = cm[1], d = txCmeDay(ymd, mn);
    if(cur && d !== cur.d){
      if(!cur.part && cur.b2 > 0) ev.push({t:r[0], p2:cur.q2 / cur.b2, p1:(withP1 && cur.b1 > 0) ? cur.q1 / cur.b1 : null});
      cur = null;
    }
    if(d === null) continue;
    if(!cur) cur = {d:d, part:mn !== 1020, b2:0, q2:0, b1:0, q1:0};
    cur.b2 += r[5]; cur.q2 += r[6];
    if(mn >= 510 && mn < 615){ cur.b1 += r[5]; cur.q1 += r[6]; }     // 美股開盤 1h45m（芝加哥 08:30~10:15）
  }
  return ev;
}
function txEvAt(E, t, k){     // 時間 t 那一刻生效的值（最後一個 生效時間 ≤ t 的事件）
  let lo = 0, hi = E.length - 1, j = -1;
  while(lo <= hi){ const mid = (lo + hi) >> 1; if(E[mid].t <= t){ j = mid; lo = mid + 1; } else hi = mid - 1; }
  return j < 0 ? null : E[j][k];
}
// 彩色K 公式版（＝mmtx._formula_inputs + sim_formula，_chk_txcrypto_port.py 逐根對拍）
function txSma(c, n){ const o = new Array(c.length).fill(NaN); let s = 0;
  for(let i = 0; i < c.length; i++){ s += c[i]; if(i >= n) s -= c[i-n]; if(i >= n - 1) o[i] = s / n; } return o; }
function txEma(c, n){ const o = new Array(c.length), a = 2 / (n + 1);
  for(let i = 0; i < c.length; i++) o[i] = i ? a*c[i] + (1 - a)*o[i-1] : c[0]; return o; }
function txFormula(o, c){
  const S = n => txSma(c, n), E = n => txEma(c, n), gt = (x, y) => x > y;
  const e8 = E(8), e21 = E(21), e34 = E(34), e55 = E(55), e120 = E(120);
  const s21 = S(21), s35 = S(35), s55 = S(55), s89 = S(89), s100 = S(100), s120 = S(120), s200 = S(200);
  const out = new Array(c.length); let s = 1;
  for(let i = 0; i < c.length; i++){
    const R = gt(c[i], e55[i]) && gt(s21[i], s100[i]) && gt(s55[i], s200[i]) && c[i] > o[i];
    const K = !gt(c[i], s35[i]) && !gt(s55[i], s200[i]) && !gt(e21[i], s89[i]) && !gt(e120[i], s200[i]);
    const B = gt(c[i], e120[i]) && gt(e34[i], e120[i]) && gt(c[i], e55[i]) && gt(c[i], e8[i]) && gt(c[i], s200[i]);
    const G = !gt(c[i], s120[i]) && !gt(e21[i], s100[i]) && !gt(c[i], e55[i]) && !gt(e21[i], s89[i]);
    if(s === 3){ if(!gt(c[i], e55[i])) s = 2; }
    else if(s === 0){ if(gt(c[i], s35[i])) s = 1; }
    else if(s === 2){ if(R) s = 3; else if(G) s = 1; }
    else { if(K) s = 0; else if(B) s = 2; }
    out[i] = s;
  }
  return out;
}
function txDec(s){ s = String(s); const i = s.indexOf('.'); return i < 0 ? 0 : s.length - i - 1; }
// OKX 一次最多 300 根，往回翻頁最多拿到最近 1440 根（實測 15m/1H/1D 都 1440、1W 359）
async function txOkx(inst, tf, need){
  let out = [], after = '';
  for(let k = 0; k < 5 && out.length < need; k++){
    const u = TXOKX+'?instId='+inst+'&bar='+TXBAR[tf]+'&limit='+Math.min(300, need)+(after ? '&after='+after : '');
    const r = await (await fetch(u, {cache:'no-store'})).json();
    if(!r || r.code !== '0') throw new Error('OKX '+((r && (r.msg || r.code)) || '沒回應'));
    const d = r.data || []; if(!d.length) break;
    out = out.concat(d); after = d[d.length-1][0];
    if(d.length < 300) break;
  }
  let prec = 0; for(const x of out.slice(0, 60)) prec = Math.max(prec, txDec(x[1]), txDec(x[2]), txDec(x[3]), txDec(x[4]));
  // OKX 欄位：[ts, 開, 高, 低, 收, 張數, 成交量(幣), 成交額(USDT), 是否收完]，新的在前
  const raw = out.reverse().map(x => [Number(x[0])/1000 + 8*3600, +x[1], +x[2], +x[3], +x[4], +x[6], +x[7], Number(x[0]), x[8]]);
  return {raw:raw, prec:Math.min(prec, 8)};
}
// 由原始 K 算成圖表列（顏色、階梯、CME 標記）。1H 的參數一要用 15 分那份的換階事件。
function txCRows(key){
  const R = TXDATA[key]; if(!R || !R.raw || !R.raw.length) return;
  const raw = R.raw, inst = key.slice(2).split('|')[0], tf = key.split('|')[1], intra = tf === '15' || tf === '60' || tf === '240';
  const col = txFormula(raw.map(r => r[1]), raw.map(r => r[4]));
  let e2 = null, e1 = null;
  if(intra){
    const r15 = TXDATA['C:'+inst+'|15'];
    e1 = (r15 && r15.raw) ? txCmeEvents(r15.raw, true) : [];
    if(tf === '15') e2 = e1;
    else if(tf === '60') e2 = txCmeEvents(raw, false);
    else { const r60 = TXDATA['C:'+inst+'|60']; e2 = (r60 && r60.raw) ? txCmeEvents(r60.raw, false) : e1; }   // 4H：收盤落在 4H K 中間 → 收盤後第一根 4H 才換
  }
  R.bars = raw.map((r, i) => [r[0], r[1], r[2], r[3], r[4], r[5], i >= TXWARM ? col[i] : -1,
    e2 ? txEvAt(e2, r[0], 'p2') : null, e1 ? txEvAt(e1, r[0], 'p1') : null, 0, (tf === '15' || tf === '60') ? txCmeMark(r[7]) : 0]);
  const lc = raw[raw.length-1][4];
  R.meta = {ok:true, ladder:intra, vol:true, prec:R.prec, half:Number((lc * TXHALF).toFixed(R.prec)), crypto:true};
}
async function txFetchC(){
  const inst = TXS.slice(2), tf = TXTF;      // 摘要＋參數一都靠 15 分；4H 的參數二靠 1H
  const keys = tf === '15' ? [TXS+'|15'] : (tf === '240' ? [TXS+'|15', TXS+'|60', TXS+'|240'] : [TXS+'|15', TXS+'|'+tf]);
  let first = false;
  for(const k of keys){
    if(TXBUSY[k]) continue;
    const c0 = TXDATA[k];       // draw() 很常被叫（點按鈕、20 秒刷新）→ 5 秒內剛抓過就不再打 OKX
    if(c0 && c0.raw && c0.raw.length && Date.now() - (TXLAST[k] || 0) < 5000) continue;
    TXBUSY[k] = 1;
    try{
      const ktf = k.split('|')[1], cur = TXDATA[k];
      if(!cur || !cur.raw || !cur.raw.length){
        const got = await txOkx(inst, ktf, ktf === 'W' ? 400 : 1440);
        if(!got.raw.length) throw new Error('OKX 沒有 '+inst);
        TXDATA[k] = {raw:got.raw, prec:got.prec, bars:[], meta:null}; txCRows(k); first = true;
        if(TXCH && TXCH.key === k) txApply();
      } else {
        const got = await txOkx(inst, ktf, 3), lastT = cur.raw[cur.raw.length-1][0];
        for(const r of got.raw){ if(r[0] < lastT) continue; if(r[0] === lastT) cur.raw[cur.raw.length-1] = r; else cur.raw.push(r); }
        txCRows(k);
        if(TXCH && TXCH.key === k) txUpdate(cur.bars.filter(b => b[0] >= lastT));
      }
      TXERR = ''; TXLAST[k] = Date.now();
    }catch(e){ TXERR = (e && (e.message || e.name)) || 'err'; }
    TXBUSY[k] = 0;
  }
  if((tf === '60' || tf === '240') && TXDATA[TXS+'|'+tf]){ txCRows(TXS+'|'+tf); }   // 15 分／1H 有新換階 → 跟著更新
  if(first) draw();                                                // 第一次拿到資料 → 摘要不用等下一輪 20 秒
}
function txCSum(){
  const R = TXDATA[TXS+'|15'];
  if(!R || !R.raw || !R.bars || !R.bars.length) return '<div class="empty">'+(TXERR ? '抓不到：'+TXERR : '載入中…（手機直接跟 OKX 拿 K 線）')+'</div>';
  const b = R.bars, n = b.length, last = b[n-1], m = R.meta, live = R.raw[n-1][8] !== '1', cl = (live && n > 1) ? b[n-2] : last;
  let chg = null;
  for(let i = n - 1; i > TXWARM; i--) if(b[i][6] !== b[i-1][6]){ chg = [txTime(b[i][0], true), TXN[b[i-1][6]], TXN[b[i][6]]]; break; }
  const two = TXLAD === '2', L = two ? last[7] : last[8], Lo = two ? last[8] : last[7];
  return '<div class="fpsum">' + (live ? '15 分盤中這根 '+txBadge(last[6])+' <span class="dim">（還沒收，可能再變）</span>　上一根收盤 '+txBadge(cl[6])
                                       : '15 分 現在 '+txBadge(last[6]))
    + (chg ? '　最近換色 <b>'+chg[0]+'</b> '+chg[1]+' → '+chg[2] : '')
    + '<br>最新 <b>'+txNum(last[4], m.prec)+'</b>'
    + '　階梯(參數'+(two ? '二' : '一')+') <b>'+txNum(L, m.prec)+'</b> ±'+txNum(m.half, m.prec)
    + ' <span class="dim">參數'+(two ? '一' : '二')+' '+txNum(Lo, m.prec)+'</span></div>';
}
function txKey(e){ if(e && e.key === 'Enter') txGo(); }
function txGo(){ const el = document.getElementById('txq');
  const v = String((el && el.value) || '').toUpperCase().replace(/[^A-Z0-9]/g, '');
  if(v) txSel('C:'+v+'-USDT-SWAP'); }
function openChart(inst){ CARD = null; TXSAVE = null; TAB = 'tx'; txSel('C:'+inst); }
function txVolS(v){ v = Number(v) || 0; return v >= 1e6 ? (v/1e6).toFixed(2)+'M' : (v >= 1e3 ? (v/1e3).toFixed(1)+'K' : v.toFixed(v >= 10 ? 0 : 2)); }

function txLoadLib(){
  if(TXLIBST) return;
  TXLIBST = 'loading';
  const s = document.createElement('script'); s.src = TXLIB; s.async = true;
  s.onload = () => { TXLIBST = 'ok'; draw(); };
  s.onerror = () => { TXLIBST = 'fail'; draw(); };
  (document.head || document.body).appendChild(s);
}
function txCreate(){
  const LW = LightweightCharts;
  const el = document.createElement('div'); el.className = 'txwrap';
  const box = document.createElement('div'); box.className = 'txbox'; el.appendChild(box);
  const ov = document.createElement('div'); ov.className = 'txov'; el.appendChild(ov);
  const lg = document.createElement('div'); lg.className = 'txlg'; el.appendChild(lg);
  const ch = LW.createChart(box, {autoSize:true,
    layout:{background:{type:'solid', color:'transparent'}, textColor:'#8b97ab', fontSize:11},
    grid:{vertLines:{color:'rgba(139,151,171,0.06)'}, horzLines:{color:'rgba(139,151,171,0.06)'}},
    crosshair:{mode: LW.CrosshairMode.Normal},
    rightPriceScale:{borderVisible:false},
    timeScale:{borderVisible:false, rightOffset:4, timeVisible:true, secondsVisible:false, tickMarkFormatter: txTick},
    localization:{locale:'zh-TW', timeFormatter: t => txTime(t, txIntra())},
    handleScroll:{mouseWheel:true, pressedMouseMove:true, horzTouchDrag:true, vertTouchDrag:false},   // 上下滑還是捲頁面
    handleScale:{mouseWheel:true, pinch:true, axisPressedMouseMove:true}});
  const cs = ch.addCandlestickSeries({priceLineVisible:true, lastValueVisible:true});
  const vs = ch.addHistogramSeries({priceScaleId:'vol', priceFormat:{type:'volume'}, lastValueVisible:false, priceLineVisible:false});
  ch.priceScale('vol').applyOptions({scaleMargins:{top:0.82, bottom:0}});
  cs.priceScale().applyOptions({scaleMargins:{top:0.08, bottom:0.22}});
  // 階梯 2 條：選到的參數 ±9。換階處要斷開（用戶：「斷點突然又連起來了」）：
  //   這個程式庫的線會「跨過空白點直接連過去」（實測畫出斜線），所以改用逐點顏色——
  //   「每段最後一個點」塗透明＝從它連到下一段的那一小段看不見（實測：線段顏色取「起點」那個點的 color）。
  const lo = {lineWidth:1, lineType:0, priceLineVisible:false, lastValueVisible:false, crosshairMarkerVisible:false};
  const lad = [ch.addLineSeries(Object.assign({color:'#a1a1e8'}, lo)), ch.addLineSeries(Object.assign({color:'#a1a1e8'}, lo))];
  const uo = {lineWidth:2, lineType:0, priceLineVisible:false, lastValueVisible:false, crosshairMarkerVisible:false, autoscaleInfoProvider: () => null};
  const uu = ch.addLineSeries(Object.assign({color:TXUDC.up}, uo)), ud = ch.addLineSeries(Object.assign({color:TXUDC.dn}, uo));
  // 威廉 %R：自己一個縱軸（固定 −100~0），放在量的下面；−20／−80 兩條虛線
  const wr = ch.addLineSeries({priceScaleId:'wr', color:TXSTC.wr, lineWidth:1, priceLineVisible:false, lastValueVisible:false, crosshairMarkerVisible:false,
    autoscaleInfoProvider: () => ({priceRange:{minValue:-100, maxValue:0}})});
  ch.priceScale('wr').applyOptions({scaleMargins:{top:0.8, bottom:0.02}});
  if(wr.createPriceLine) for(const p of [-20, -80]) wr.createPriceLine({price:p, color:'rgba(139,151,171,0.45)', lineWidth:1, lineStyle:2, axisLabelVisible:false, title:String(p)});
  const sr = document.createElement('div'); sr.className = 'txsr'; el.appendChild(sr);     // 支撐壓力／型態的疊圖（SVG）
  TXCH = {el:el, box:box, ov:ov, lg:lg, ch:ch, cs:cs, vs:vs, lad:lad, uu:uu, ud:ud, udm:{}, udpl:{}, key:'', fit:true, hl:0, wr:wr, sr:sr};
  ch.subscribeCrosshairMove(p => { try{ txLegend(p && p.time !== undefined ? p.time : null); }catch(e){} });
  const later = window.requestAnimationFrame || (f => setTimeout(f, 16));
  const redo = () => { if(!TXCH.hl){ TXCH.hl = 1; later(() => { TXCH.hl = 0; try{ txFit(); txHiLo(); txSess(); txUdMarks(); txSrDraw(); }catch(e){} }); } };
  ch.timeScale().subscribeVisibleLogicalRangeChange(redo);
  if(ch.timeScale().subscribeSizeChange) ch.timeScale().subscribeSizeChange(redo);
}
function txApply(){
  const C = TXCH, R = TXDATA[C.key];
  if(!R || !R.bars.length){ for(const s of [C.cs, C.vs, C.uu, C.ud, C.wr].concat(C.lad)) s.setData([]); C.cs.setMarkers([]); C.vs.setMarkers([]);
    C.lg.innerHTML = '<span class="dim">'+(TXERR ? '抓不到：'+TXERR : '載入中…')+'</span>'; C.ov.innerHTML = ''; C.sr.innerHTML = ''; return; }
  const bars = R.bars, m = R.meta;
  C.cs.applyOptions({priceFormat:{type:'price', precision:m.prec, minMove: m.prec ? 0.01 : 1}});
  C.ch.applyOptions({timeScale:{timeVisible: txIntra()}});
  C.cs.setData(bars.map(txCandle));
  C.vs.setData(m.vol ? bars.map(txVol) : []);
  txLadApply(); txUdApply(); txStApply();
  txFit(); txHiLo(); txSess(); txLegend(null);
}
function txLadApply(){
  const C = TXCH, R = TXDATA[C.key]; if(!R) return;
  const seg = R.meta.ladder ? txLadSeg(R.bars, R.meta.half, txLadIdx()) : null;
  C.lad.forEach((s, j) => s.setData(seg ? seg[j] : []));
}
// 一開始只顯示最近 120 根（日/週 150 根）。★圖表剛建立時寬度還是 0，這時設範圍會被當成「全部擠進 0 寬」
//   → 等它有寬度（尺寸變化回呼）再設；stub 環境沒有 clientWidth 就直接設。
//   ★程式庫是下一個畫面才套用 setData，有時會把剛設好的範圍蓋回「全部」（BTC 實測 -110~1437）→
//    範圍變動回呼裡再確認一次：沒對上就再設，最多 5 次（stub 環境沒有回呼，設一次就算）。
function txFit(){
  const C = TXCH; if(!C || !C.fit) return; const R = TXDATA[C.key]; if(!R || !R.bars.length) return;
  const w = C.box.clientWidth; if(w !== undefined && !(w > 0)) return;
  const n = R.bars.length, want = {from: Math.max(0, n - (txIntra() ? 120 : 150)), to: n + 3};
  const r = C.want ? C.ch.timeScale().getVisibleLogicalRange() : null;
  if(r && Math.abs(r.from - C.want.from) < 2 && Math.abs(r.to - C.want.to) < 2){ C.fit = false; C.want = null; C.fitN = 0; return; }
  C.fitN = (C.fitN || 0) + 1;
  if(C.fitN > 5 || w === undefined){ C.fit = false; C.want = null; C.fitN = 0; }
  else C.want = want;
  C.ch.timeScale().setVisibleLogicalRange(want);
}
function txUpdate(nb){
  const C = TXCH, m = TXDATA[C.key].meta;
  for(const b of nb){
    C.cs.update(txCandle(b));
    if(m.vol) C.vs.update(txVol(b));
  }
  if(m.ladder){     // 段號從頭算才知道單雙 → 整串重分段，只把新來的那幾根推進去（舊的段不會變）
    const all = TXDATA[C.key].bars, seg = txLadSeg(all, m.half, txLadIdx());
    for(let i = all.length - nb.length; i < all.length; i++) C.lad.forEach((s, j) => s.update(seg[j][i]));
  }
  if(TXUD === '1' && !txIsC(TXS) && C.uu){
    const all = TXDATA[C.key].bars;
    for(const [k, sr] of [['up', C.uu], ['dn', C.ud]]){
      const r = txUdData(all, txUdSegs(k), k);
      for(let i = all.length - nb.length; i < all.length; i++) sr.update(r.pts[i]);
    }
  }
  txStApply();                // 威廉 %R 每根都可能變（盤中那根）→ 整條重餵；型態／區只在收盤後才會變（txStudy 有快取）
  txHiLo(); txSess(); txLegend(null);
}
// 畫面範圍內的最高／最低標數值；台指的籌碼異常星畫在量上
function txHiLo(){
  const C = TXCH; if(!C) return; const R = TXDATA[C.key]; if(!R || !R.bars.length) return;
  const bars = R.bars, m = R.meta, r = C.ch.timeScale().getVisibleLogicalRange();
  let i0 = 0, i1 = bars.length - 1;
  if(r){ i0 = Math.max(0, Math.ceil(r.from)); i1 = Math.min(bars.length - 1, Math.floor(r.to)); }
  if(i1 < i0) return;
  let ih = i0, il = i0;
  for(let i = i0; i <= i1; i++){ if(bars[i][2] > bars[ih][2]) ih = i; if(bars[i][3] < bars[il][3]) il = i; }
  const mk = [{time:bars[ih][0], position:'aboveBar', color:'#e6edf7', shape:'arrowDown', text:'高 '+txNum(bars[ih][2], m.prec)},
              {time:bars[il][0], position:'belowBar', color:'#e6edf7', shape:'arrowUp', text:'低 '+txNum(bars[il][3], m.prec)}].concat(txPtMarks(R));
  mk.sort((a, b) => a.time - b.time);
  C.cs.setMarkers(mk);
  const st = [];
  if(m.vol) for(const b of bars) if(b[9] > 0) st.push({time:b[0], position:'aboveBar', color:'#ffd166', shape:'circle', text:'★'});
  C.vs.setMarkers(st);
}
// 盤別分隔：兩根 K 之間有休市空檔＝新的一盤開始 → 淡淡的虛線＋「夜盤／日盤」（用戶：台指夜盤要看得出來）
function txSess(){
  const C = TXCH; if(!C) return; const R = TXDATA[C.key];
  if(!R || !R.bars.length || !txIntra()){ C.ov.innerHTML = ''; return; }
  const bars = R.bars, ts = C.ch.timeScale(), step = (TXTF==='15' ? 15 : 60) * 60;
  const r = ts.getVisibleLogicalRange();
  const i0 = r ? Math.max(1, Math.floor(r.from)) : 1, i1 = r ? Math.min(bars.length - 1, Math.ceil(r.to)) : bars.length - 1;
  const H = Math.max(0, (C.box.clientHeight || 0) - 26);
  let g = '', lastX = -1e9;
  if(R.meta && R.meta.crypto){      // 幣 24 小時沒有空檔 → 標 CME 開盤／收盤（收盤＝換階）
    for(let i = i0; i <= i1; i++){
      const mk = bars[i][10]; if(!mk) continue;
      const xa = ts.timeToCoordinate(bars[i-1][0]), xb = ts.timeToCoordinate(bars[i][0]);
      if(xa === null || xb === null) continue;
      const x = Math.round((xa + xb) / 2) + 0.5, cl = mk === 'c';
      g += '<line x1="'+x+'" x2="'+x+'" y1="0" y2="'+H+'" stroke="'+(cl ? 'rgba(161,161,232,0.35)' : 'rgba(139,151,171,0.28)')+'" stroke-width="1" stroke-dasharray="3,4"/>';
      if(x - lastX >= 34){ lastX = x;
        g += '<text x="'+(x+3)+'" y="'+(H-4)+'" font-size="9" fill="'+(cl ? 'rgba(161,161,232,0.8)' : 'rgba(139,151,171,0.7)')+'">'+(cl ? 'CME收' : 'CME開')+'</text>'; }
    }
    C.ov.innerHTML = g ? '<svg width="100%" height="100%">'+g+'</svg>' : ''; return;
  }
  for(let i = i0; i <= i1; i++){
    if(bars[i][0] - bars[i-1][0] <= step) continue;
    const xa = ts.timeToCoordinate(bars[i-1][0]), xb = ts.timeToCoordinate(bars[i][0]);
    if(xa === null || xb === null) continue;
    const x = Math.round((xa + xb) / 2) + 0.5, hr = new Date(bars[i][0]*1000).getUTCHours();
    const night = hr >= 15 || hr < 5;
    g += '<line x1="'+x+'" x2="'+x+'" y1="0" y2="'+H+'" stroke="'+(night ? 'rgba(161,161,232,0.35)' : 'rgba(139,151,171,0.28)')+'" stroke-width="1" stroke-dasharray="3,4"/>';
    if(x - lastX >= 34){ lastX = x;      // 縮很小時線很密 → 字只標放得下的
      g += '<text x="'+(x+3)+'" y="'+(H-4)+'" font-size="9" fill="'+(night ? 'rgba(161,161,232,0.8)' : 'rgba(139,151,171,0.7)')+'">'+(night ? '夜盤' : '日盤')+'</text>'; }
  }
  C.ov.innerHTML = g ? '<svg width="100%" height="100%">'+g+'</svg>' : '';
}
function txLegend(t){
  const C = TXCH; if(!C) return; const R = TXDATA[C.key]; if(!R || !R.bars.length) return;
  const bars = R.bars, m = R.meta; let i = bars.length - 1;
  if(t !== null && t !== undefined){ let lo = 0, hi = bars.length - 1;
    while(lo < hi){ const mid = (lo + hi + 1) >> 1; if(bars[mid][0] <= t) lo = mid; else hi = mid - 1; } i = lo; }
  const b = bars[i], pv = i > 0 ? bars[i-1][4] : b[1];
  C.lg.innerHTML = '<b>'+txTime(b[0], txIntra())+'</b> '+txBadge(b[6])
    + ' 開 '+txNum(b[1], m.prec)+' 高 '+txNum(b[2], m.prec)+' 低 '+txNum(b[3], m.prec)+' 收 <b>'+txNum(b[4], m.prec)+'</b> '+txChg(b[4] - pv, m.prec)
    + (m.vol ? ' 量 '+(m.crypto ? txVolS(b[5]) : b[5]) : '') + (m.ladder && b[txLadIdx()] !== null && b[txLadIdx()] !== undefined ? ' 階梯'+(TXLAD === '2' ? '二 ' : '一 ')+(m.crypto ? txNum(b[txLadIdx()], m.prec) : b[txLadIdx()]) : '') + (b[9] > 0 ? ' ★'+b[9] : '') + txUdLeg(b[0])
    + txWrLeg(R, i);
}
function txWrLeg(R, i){
  if(TXWR !== '1') return ''; const st = txStudy(R); if(!st || st.wr[i] === null || st.wr[i] === undefined) return '';
  return ' <span style="color:'+TXSTC.wr+'">%R '+st.wr[i].toFixed(1)+'</span>';
}
async function txFetch(){
  if(txIsC(TXS)) return txFetchC();                // 幣：手機直接跟 OKX 拿
  const key = TXS+'|'+TXTF; if(TXBUSY[key]) return; TXBUSY[key] = 1;
  const cur = TXDATA[key];
  let url = TXAPI+'?s='+TXS+'&tf='+TXTF;
  if(cur && cur.bars.length) url += '&since='+cur.bars[cur.bars.length-1][0];
  try{
    const r = await (await fetch(url, {cache:'no-store'})).json();
    if(!r || !r.ok){ TXERR = (r && r.err) || '抓不到'; }
    else {
      TXERR = '';
      if(r.full || !cur){ TXDATA[key] = {bars:r.bars, meta:r}; if(TXCH && TXCH.key === key) txApply(); else if(TXLIBST === 'fail') draw(); }
      else { cur.bars.pop(); for(const b of r.bars) cur.bars.push(b); cur.meta = r; if(TXCH && TXCH.key === key) txUpdate(r.bars); }
    }
  }catch(e){ TXERR = e.name || 'err'; }
  TXBUSY[key] = 0;
}
// draw() 之後呼叫：把常駐的圖表搬進這次的插槽
function txMount(){
  const slot = document.getElementById(CARD && TXSAVE ? 'txslotc' : 'txslot'); if(!slot) return;
  if(typeof LightweightCharts === 'undefined'){
    if(TXLIBST !== 'fail'){ txLoadLib(); slot.innerHTML = '<div class="empty">圖表載入中…</div>'; }
    else slot.innerHTML = '<div class="warn">互動圖表程式庫載不到（cdn.jsdelivr.net），先用簡易圖</div>' + txSvg();
    txFetch(); return;
  }
  if(!TXCH) txCreate();
  slot.appendChild(TXCH.el);
  const key = TXS+'|'+TXTF;
  if(TXCH.key !== key){ TXCH.key = key; TXCH.fit = true; TXCH.want = null; TXCH.fitN = 0; txApply(); }
  txFetch();
}
// 程式庫載不到時的簡易圖（最近 96 根，不能縮放）
function txSvg(){
  const R = TXDATA[TXS+'|'+TXTF]; if(!R || !R.bars.length) return '<div class="empty">'+(TXERR ? '抓不到：'+TXERR : '載入中…')+'</div>';
  const bars = R.bars.slice(-96), m = R.meta, n = bars.length;
  const step = 9, padL = 4, padR = 58, W = padL + n*step + padR, Hp = 260, Hv = m.vol ? 70 : 0, gap = 18, H = Hp + gap + Hv + 18;
  let lo = Infinity, hi = -Infinity, vmax = 1;
  for(const b of bars){ lo = Math.min(lo, b[3]); hi = Math.max(hi, b[2]); vmax = Math.max(vmax, b[5]);
    const lv = b[txLadIdx()]; if(m.ladder && lv !== null && lv !== undefined){ lo = Math.min(lo, lv - 12); hi = Math.max(hi, lv + 12); } }
  const pad = (hi - lo)*0.04 || 10; lo -= pad; hi += pad;
  const y = v => Hp - (v - lo)/(hi - lo)*Hp, yv = v => Hp + gap + Hv - v/vmax*Hv;
  let g = '';
  // 階梯：每一段各畫一條平線（段與段之間不連）
  if(m.ladder) for(const k of [txLadIdx()]){
    let i0 = 0;
    for(let i = 1; i <= n; i++){
      if(i === n || bars[i][k] !== bars[i0][k]){
        const v = bars[i0][k];
        if(v !== null && v !== undefined){
          const x0 = padL + i0*step, x1 = padL + i*step - 1;
          for(const off of [m.half, -m.half])
            g += '<line x1="'+x0+'" x2="'+x1+'" y1="'+y(v + off)+'" y2="'+y(v + off)+'" stroke="#a1a1e8" stroke-width="1.2"/>';
        }
        i0 = i;
      }
    }
  }
  for(let i = 0; i < n; i++){
    const b = bars[i], x = padL + i*step + step/2, col = TXC[b[6]] || '#888';
    const yo = y(b[1]), yc = y(b[4]);
    g += '<line x1="'+x+'" x2="'+x+'" y1="'+y(b[2])+'" y2="'+y(b[3])+'" stroke="'+col+'"/>'
       + '<rect x="'+(x-3)+'" y="'+Math.min(yo, yc)+'" width="6" height="'+Math.max(1, Math.abs(yo - yc))+'" fill="'+col+'" fill-opacity="'+(b[4] >= b[1] ? 1 : 0.45)+'" stroke="'+col+'"/>';
    if(Hv){ const vy = yv(b[5]); g += '<rect x="'+(x-3)+'" y="'+vy+'" width="6" height="'+(Hp + gap + Hv - vy)+'" fill="'+col+'" fill-opacity="0.45"/>'; }
    if(i % 16 === 0) g += '<text x="'+x+'" y="'+(H-4)+'" text-anchor="middle" font-size="9" fill="#8b97ab">'+txTime(b[0], txIntra())+'</text>';
  }
  for(let k = 0; k <= 4; k++){ const v = lo + (hi - lo)*k/4; g += '<text x="'+(W - padR + 4)+'" y="'+(y(v) + 3)+'" font-size="9" fill="#8b97ab">'+txNum(v, m.prec)+'</text>'; }
  return '<div dir="rtl" style="overflow-x:auto"><svg dir="ltr" viewBox="0 0 '+W+' '+H+'" width="'+Math.max(W, 320)+'" height="'+H+'" style="max-width:none;display:block">'+g+'</svg></div>';
}
function txCoins(){        // 幣的快捷鈕：BTC/ETH/SOL＋持倉的幣＋正在看的
  const out = ['BTC', 'ETH', 'SOL'];
  for(const t of (D.trades || [])){
    const c = String(t.inst_id || t.symbol || '').replace('-USDT-SWAP', '').replace('/USDT', '').replace('-USDT', '').split(':')[0];
    if(c && out.indexOf(c) < 0) out.push(c);
  }
  if(txIsC(TXS) && out.indexOf(txCoin(TXS)) < 0) out.push(txCoin(TXS));
  return out;
}
function viewTx(){
  const P = D.tx || {}, isC = txIsC(TXS);
  const p = isC ? {} : (P[TXS] || {});
  let h = '<div class="card"><h2>麥門指標<span>'+(isC ? 'OKX 永續・手機直接抓 K 線' : '期交所 1 分 K・'+(P.age != null ? P.age+' 秒前算' : ''))+'</span></h2>';
  if(!isC && (P.stale || P.fetch_err)) h += '<div class="warn">⚠ '+(P.stale ? '盤中超過 5 分鐘沒抓到新資料，下面是舊的。' : '')+(P.fetch_err || '')+'</div>';
  h += '<div class="txbar">' + [['TX','台指'],['TSE','加權']].map(([k, n]) => {
      const q = P[k] || {};
      return `<span class="wb ${TXS===k?'on':''}" onclick="txSel('${k}')">${n} ${q.ok ? txBadge(q.c) : ''}</span>`; }).join(' ')
    + '<span class="txsp"></span>'
    + txCoins().map(c => `<span class="wb ${TXS==='C:'+c+'-USDT-SWAP'?'on':''}" onclick="txSel('C:${c}-USDT-SWAP')">${c}</span>`).join(' ')
    + ' <input id="txq" class="txq" placeholder="其他幣" autocomplete="off" onkeydown="txKey(event)"><span class="wb" onclick="txGo()">看</span>'
    + '</div><div class="txbar">'
    + txTfs().map(k => `<span class="wb ${TXTF===k?'on':''}" onclick="txSel(null,'${k}')">${TXTFN[k]}</span>`).join(' ')
    + ((TXS === 'TX' || isC) && txIntra() ? '<span class="txsp"></span>' + [['1','參數一'],['2','參數二']].map(([k, n]) =>
        `<span class="wb ${TXLAD===k?'on':''}" onclick="txLadSel('${k}')">階梯${n}</span>`).join(' ') : '')
    + (!isC ? `<span class="txsp"></span><span class="wb ${TXUD==='1'?'on':''}" onclick="txUdSel()">上下線</span>` : '')
    + '<span class="txsp"></span>' + txTglBar() + '</div>';
  if(isC) h += txCSum();
  else if(!p.ok) h += '<div class="empty">'+(p.err || P.err || '沒有資料')+'</div>';
  else {
    h += '<div class="fpsum">' + (p.live_bar ? '15 分盤中這根 '+txBadge(p.c)+' <span class="dim">（還沒收，可能再變）</span>　上一根收盤 '+txBadge(p.closed[1])
                                             : '15 分 現在 '+txBadge(p.c))
       + (p.model ? '' : ' <span class="warn">（模型檔沒讀到，用公式版）</span>')
       + (p.last_change ? '　最近換色 <b>'+p.last_change[0]+'</b> '+p.last_change[1]+' → '+p.last_change[2] : '')
       + '<br>最新 <b>'+txNum(p.last, TXS==='TX' ? 0 : 2)+'</b>'
       + (TXS==='TX' ? (TXLAD === '2' ? '　階梯(參數二) <b>'+txNum(p.ladder2, 1)+'</b> ±'+(p.half || 9)+' <span class="dim">參數一 '+txNum(p.ladder1, 1)+'</span>'
                                       : '　階梯(參數一) <b>'+txNum(p.ladder1, 1)+'</b> ±'+(p.half || 9)+' <span class="dim">參數二 '+txNum(p.ladder2, 1)+'</span>') : '')
       + '　<span class="dim">最後一分鐘 '+(p.last_minute || '—')+'</span>'
       + (p.color_formula !== p.color ? '<br><span class="dim">公式版這根是 '+p.color_formula+'（模型與公式不同時以模型為準）</span>' : '') + '</div>';
  }
  if(isC) h += '<div class="sub dim">⚠ 幣的彩色K是台指公式直接套（沒有標註可以驗）；階梯＝CME 交易時段算這個合約自己的 VWAP（虛線：CME開／CME收＝換階）。</div>';
  else if(TXTF !== '15') h += '<div class="sub dim">⚠ '+TXTFN[TXTF]+' 的顏色是 15 分模型直接套用，沒有標註可以驗證；只有 15 分驗過。</div>';
  h += '<div id="txslot"></div>';
  h += '<div class="sub dim">雙指／滾輪縮放、左右拖曳；手指按住或滑鼠移上去看那根的開高低收。灰色＝每個週期最前面 '+(P.warm || 280)+' 根長均線還沒穩，不上色。</div>';
  if(!isC && TXUD === '1'){
    const ob = txUdObs().slice().reverse();
    h += '<div class="sech">上下線紀錄 <span class="dim">（麥門文章圖讀出、±3 點；時間＝圖上傳時間）</span></div>'
       + (ob.length ? ob.map(o => '<div class="sub">'+o.t+'　<span style="color:'+TXUDC.up+'">上線 '+(o.up == null ? '—' : o.up)+'</span>'
           + '　<span style="color:'+TXUDC.dn+'">下線 '+(o.dn == null ? '—' : o.dn)+'</span>　<span class="dim">'+o.tf+'圖</span></div>').join('')
         : '<div class="empty">這個商品沒有紀錄</div>')
       + '<div class="sub dim">「—」＝那張圖上沒讀到（可能在畫面外），不是被取消。</div>';
  }
  if(TXS === 'TX' && p.ok){
    const st = (p.stars || []).slice().reverse();
    h += '<div class="sech">最近的籌碼異常星</div>' + (st.length ? st.map(s => '<div class="sub">★ '+s[0]+'　5 分 K 量 <b>'+s[1]+'</b> 口</div>').join('') : '<div class="empty">最近沒有</div>');
  }
  h += '<details><summary class="sech">規則與準確度</summary><div class="note">'
    + '<b>彩色K（追價氛圍）</b>：黑&lt;金&lt;藍&lt;紅，每根最多動一格。紅→藍＝收盤跌破 EMA55；黑→金＝收盤站上 SMA35。金↔藍、進紅、進黑由均線特徵模型判斷。'
    + '<br>・<b>台指</b> 15 分：影片畫面 11,020 根標註（2023-12~2024-09）四段時間交叉驗證 <b>95%</b>（公式版 92.6%）。'
    + '<br>・<b>加權</b> 15 分：台指顏色直接搬只對 35%（兩邊各算各的）；台指公式套加權 86%；台指＋加權標註合併訓練 <b>約 88%</b>（加權標註只有 1,128 根、2023-12~2024-06，最弱那段 72%）。'
    + '<br>・1H／日／週：同一個模型套上去，<b>沒有驗證</b>；週K只有 2023-12 起約 150 根，長均線算不出來，所以整條灰色。'
    + '<br><b>階梯</b>（只有台指）：前一交易日台指 VWAP，參數二＝08:45~13:30、參數一＝09:00~10:45，15:00 夜盤起換階；畫值 ±9 點的雙線；跟麥門一樣一次只畫一個，按鈕切換（預設參數一）。'
    + '<br><b>上下線</b>（按鈕開關）：麥門的「到價推撥」上線／下線是帳號自己設的警示價（不是公式）；這裡是從麥門文章圖讀出來的歷史值，'
    + '每條只畫「這次讀到 → 下次改值」那一段，段首標時間；時間＝那張圖的上傳時間，讀數 ±3 點；不參與縱軸縮放（太遠的線會在畫面外）。'
    + '<br><b>籌碼異常星</b>（只有台指 15 分）：加權開盤時間（09:00~13:25 開始）內某根 5 分 K 量 ≥3000 口。'
    + '<br><b>K 棒</b>：台指日K＝前一晚夜盤＋當天日盤（期交所交易日；量跟期交所公布的當日總量一致），1H 日盤從 08:45 起算；加權 1H 從 09:00 起算，13:00 那根含收盤。虛線＝換盤（夜盤／日盤開始）。'
    + '<br>資料：期交所公開 1 分 K（台指對麥門資料量 100%、收 99.7% 相同；加權 267/267 根完全相同、沒有量）。近月合約＝最早到期（結算日日盤還是舊月）。'
    + '<br><b>幣</b>（OKX 永續，K 線由手機直接跟 OKX 拿，最多最近 1440 根）：彩色K＝台指公式版直接套，<b>沒有驗證</b>。'
    + '階梯＝用 CME 比特幣期貨的交易時段（17:00~隔天 16:00 芝加哥時間；週末、假日照平日每天算）當窗口，算這個合約自己的 VWAP（成交額÷成交量）：'
    + '參數二＝整個 CME 交易日（台灣夏令 06:00→隔天 05:00）、參數一＝美股開盤後 1 小時 45 分（紐約 09:30~11:15＝台灣夏令 21:30~23:15，'
    + 'OKX 實測一天中成交額最大的 105 分鐘，對照台指＝加權開盤 09:00~10:45），16:00 收盤換階（台灣時間夏令 05:00／冬令 06:00）；'
    + '雙線寬度 ±0.02%。1H／4H 的參數一要用 15 分 K 才切得出來，所以只有最近約 15 天有；4H 的參數二用 1H 算（約 60 天），收盤落在 4H K 中間，所以收盤後第一根 4H 才換階、也不畫 CME 開收虛線。'
    + '<br><b>威廉%R</b>（按鈕開關，紫線在量的下面）：(14 根最高 − 收) ÷ (14 根最高 − 14 根最低) × −100；−20 以上超買、−80 以下超賣（4J 的設定）。'
    + '4J 自己說不能單看：要先看高低點有沒有被突破／跌破，回踩那條線時剛好超賣才做多（空單反過來）。<b>回測</b>（BTC/ETH/SOL 2021~2026、停損 1%／目標 +2%、扣費）照他講的做每筆約 −0.1%，沒有優勢。'
    + '<br><b>支撐壓力</b>（按鈕開關）：你第 9~10 輪看圖判過的那套——明顯轉折（反向走 2 倍 ATR 才算）、近 200 根內價差 ≤0.5 ATR 的轉折高點（紅＝壓力）或低點（綠＝支撐）≥2 個圍成一區，點＝那幾個轉折。'
    + '只畫<b>現在還有效</b>的區：沒被收盤穿過的，或被穿過後支撐壓力互換、價格有回來測試、到現在還守住的（延長到最右邊）；穿過後沒回來的就不畫。'
    + '顏色＝現在的角色（壓力紅、支撐綠）。跟判圖時一樣只畫夠大的區（區的邊到另一邊極端 ≥2.5 ATR）。'
    + '<b>我自己定的</b>：最後一次碰到／回測超過 200 根就不算有效；價位和時間都重疊的區只留碰最多次的；上下各只畫離現價最近的 2 個。'
    + '<br><b>型態</b>（按鈕開關）：你判過 8 成對的——壓力區突破（17/21）、支撐區跌破（21/21）、上升三角形（17/18）、下降三角形（16/18）；濾網同回測（型態高度 ≥2.5 ATR、突破那根量 ≥ 前 30 根均量）。'
    + '箭頭＝突破那根（紅↑做多、綠↓做空），虛線框＝被突破的區，兩條線＝三角形的頸線和另一邊；只用已收盤的 K。'
    + '<b>注意</b>：這些只在加密貨幣 4H 判過、回測過，型態本身扣費後跟隨機進場差不多；其他週期、台指加權沒驗證。'
    + '</div></details>';
  if(!isC) h += '<div class="sub">合約 '+Object.values(P.sym || {}).join(' / ')+'</div>';
  h += '</div>';
  return h;
}

// ★型態分頁（2026-10-14，用戶：「幫我做一個分頁是型態的 然後要做多做空分開 看是要突破前就有還是突破後顯示這樣」）
//   兩種都給：「還沒突破」＝型態已成形、現價離突破線 ≤1.5 ATR（★我定的）；「剛突破」＝最近 6 根已收盤 K 內突破（4H＝24 小時）。
//   型態與濾網跟圖上同一套（txStudyArr：壓力區突破／支撐區跌破／上升、下降三角形，高度 ≥2.5 ATR；突破那根還要量 ≥ 前 30 根均量——
//   還沒突破的沒有量可以看，只標高度）。幣＝視覺篩選器裡 24H 成交額前 60；手機直接跟 OKX 抓（每幣 600 根，3 個一起），10 分鐘重掃。
const PAT_N = 60, PAT_POST = 6, PAT_NEAR = 1.5, PAT_TTL = 10 * 60 * 1000;
let PATTF = '240', PATD = {};
try{ const v = localStorage.getItem('pattf'); if(v === '240' || v === '60') PATTF = v; }catch(e){}
function patSel(tf){ PATTF = tf; try{ localStorage.setItem('pattf', tf); }catch(e){} draw(); }
function patCoins(){ const rows = (D && D.mkt && D.mkt.rows) || [];
  return rows.filter(r => r && r.inst).slice().sort((a, b) => (b.vol || 0) - (a.vol || 0)).slice(0, PAT_N).map(r => r.inst); }
// 一個幣的 K（OKX 原始列）→ {post:剛突破, pre:還沒突破, last, atr}
function txPatNow(raw){
  const N = raw.length, n = raw[N-1][8] === '1' ? N : N - 1;
  if(n < 120) return null;
  const h = raw.map(r => r[2]), l = raw.map(r => r[3]), c = raw.map(r => r[4]), v = raw.map(r => r[5]);
  const R = txStudyArr(h, l, c, v, n), A = R.atr, last = c[N-1];
  const hh = h.slice(0, n), ll = l.slice(0, n), cc = c.slice(0, n), a = txAtrW(hh, ll, cc, 14), piv = txZig(hh, ll, a, 2.0);
  const post = R.pats.filter(e => e.i >= n - PAT_POST).map(e => ({dir:e.dir, pat:e.pat, level:e.level, stop:e.stop, target:e.level + e.dir * e.height,
    t:raw[e.i][0], ago:n - 1 - e.i, volx:e.volx, hatr:e.hatr}));
  const pre = [];
  for(const z of (R.life || []).filter(q => q.live)){   // 圖上畫的那些「現在還有效」的區（同一套）：壓力＝做多候選（等突破上緣）、支撐＝做空候選（等跌破下緣）
    const dr = z.role, edge = dr > 0 ? z.hi : z.lo, dist = (edge - last) * dr;
    if(!(dist >= 0 && dist <= PAT_NEAR * A)) continue;
    let ext = dr > 0 ? Infinity : -Infinity; for(let m = z.first; m < n; m++) ext = dr > 0 ? Math.min(ext, ll[m]) : Math.max(ext, hh[m]);
    const ht = Math.abs(edge - ext); if(ht < 2.5 * A) continue;
    let opp = null; for(const p of piv) if(p[2] === -dr && p[3] <= n - 1) opp = p;
    pre.push({dir:dr, pat:(dr > 0 ? '壓力區' : '支撐區'), level:edge, stop:opp ? opp[1] : null, target:edge + dr * ht, dist:dist, distA:dist / A, nz:z.n});
  }
  const P = piv.filter(p => p[3] <= n - 1);
  if(P.length >= 6){                          // 還沒突破的上升／下降三角形：最近 6 個轉折成形、最後一個轉折之後沒收盤穿過頸線
    const tr = txTriCollect(P.slice(-6));
    if(tr && (tr[0] === '上升三角' || tr[0] === '下降三角')){
      const S = tr[1], Hs = S.filter(p => p[2] > 0), Ls = S.filter(p => p[2] < 0), up = tr[0] === '上升三角', dr = up ? 1 : -1;
      const lv = up ? Math.max(...Hs.map(p => p[1])) : Math.min(...Ls.map(p => p[1])), lastI = S[S.length-1][0];
      const ht = Math.abs(S[0][2] > 0 ? S[0][1] - S[1][1] : S[1][1] - S[0][1]);
      let crossed = false; for(let m = lastI + 1; m < n; m++) if((cc[m] - lv) * dr > 0){ crossed = true; break; }
      const dist = (lv - last) * dr;
      if(!crossed && ht >= 2.5 * A && dist >= 0 && dist <= PAT_NEAR * A)
        pre.push({dir:dr, pat:tr[0] + '形', level:lv, stop:(up ? Ls[Ls.length-1][1] : Hs[Hs.length-1][1]), target:lv + dr * ht, dist:dist, distA:dist / A});
    }
  }
  return {post:post, pre:pre, last:last, atr:A};
}
async function patScan(){
  const tf = PATTF, S = PATD[tf] || (PATD[tf] = {res:{}, prec:{}, at:0, busy:false, done:0, total:0, err:0});
  if(S.busy || Date.now() - S.at < PAT_TTL) return;
  const list = patCoins(); if(!list.length) return;
  S.busy = true; S.done = 0; S.total = list.length; S.err = 0; let k = 0;
  const worker = async () => {
    while(k < list.length){
      const inst = list[k++];
      try{ const g = await txOkx(inst, tf, 600); S.res[inst] = txPatNow(g.raw); S.prec[inst] = g.prec; }catch(e){ S.err++; }
      S.done++; if(S.done % 10 === 0 && TAB === 'pat') draw();
    }
  };
  try{ await Promise.all([worker(), worker(), worker()]); }finally{ S.busy = false; S.at = Date.now(); }
  if(TAB === 'pat') draw();
}
function viewPat(){
  const S = PATD[PATTF] || {res:{}, prec:{}}, tfn = PATTF === '240' ? '4H' : '1H';
  let h = '<div class="card"><h2>型態<span>'+tfn+'・成交額前 '+PAT_N+' 幣・手機直接抓 OKX</span></h2>'
    + '<div class="txbar">' + [['240', '4H'], ['60', '1H']].map(([k, n]) => `<span class="wb ${PATTF===k?'on':''}" onclick="patSel('${k}')">${n}</span>`).join(' ')
    + '<span class="txsp"></span><span class="dim">'+(S.busy ? '掃描中 '+S.done+' / '+S.total : (S.at ? '上次掃描 '+ago(S.at/1000)+'前' : '準備掃描…'))
    + (S.err ? '，'+S.err+' 幣抓不到' : '')+'</span></div>'
    + '<div class="sub dim">型態＝你判過 8 成對的四種（壓力區突破、支撐區跌破、上升三角形、下降三角形），跟「台指」頁圖上同一套。'
    + '「還沒突破」＝型態已成形、現價離突破線 ≤'+PAT_NEAR+' ATR；「剛突破」＝最近 '+PAT_POST+' 根已收盤 K 內突破（突破那根量 ≥ 前 30 根均量）。'
    + '停損＝反方向最後一個轉折、目標＝等幅。'+(PATTF !== '240' ? '<b>1H 沒有判過、沒回測過。</b>' : '')
    + '⚠ 回測：這些型態本身扣費後跟隨機進場差不多，是看盤工具不是訊號。</div></div>';
  const rows = {1:{pre:[], post:[]}, '-1':{pre:[], post:[]}};
  for(const inst in S.res){ const r = S.res[inst]; if(!r) continue;
    for(const e of r.pre) rows[e.dir].pre.push(Object.assign({inst:inst, last:r.last}, e));
    for(const e of r.post) rows[e.dir].post.push(Object.assign({inst:inst, last:r.last}, e)); }
  const P = inst => (S.prec && S.prec[inst] !== undefined) ? S.prec[inst] : 4;
  const coin = inst => `<a class="cl" onclick="patOpen('${inst}')">${inst.replace('-USDT-SWAP', '')}</a>`;
  for(const [dr, title, cl] of [[1, '做多 📈', 'up'], [-1, '做空 📉', 'down']]){
    const R = rows[dr]; R.pre.sort((a, b) => a.distA - b.distA); R.post.sort((a, b) => a.ago - b.ago);
    h += `<div class="card"><h2><span class="${cl}">${title}</span><span>還沒突破 ${R.pre.length}・剛突破 ${R.post.length}</span></h2>`
      + '<h3 class="sech">還沒突破 <span class="dim">（離突破線近 → 遠）</span></h3>'
      + (R.pre.length ? table('patpre'+dr, ['幣', '型態', '突破線', '現價', '距離', '停損', '目標'], R.pre, e => [
          {v:e.inst, h:coin(e.inst)}, {v:e.pat, h:e.pat + (e.nz ? `<span class="dim"> 碰${e.nz}次</span>` : '')},
          {v:e.level, h:txNum(e.level, P(e.inst))}, {v:e.last, h:txNum(e.last, P(e.inst))},
          {v:e.distA, h:f(e.dist / e.last * 100, 2) + '%<span class="dim"> ' + f(e.distA, 1) + 'ATR</span>'},
          {v:e.stop, h:e.stop == null ? '—' : txNum(e.stop, P(e.inst))}, {v:e.target, h:txNum(e.target, P(e.inst))}]) : '<div class="empty">'+(S.at || S.busy ? '目前沒有' : '掃描中…')+'</div>')
      + '<h3 class="sech">剛突破 <span class="dim">（最近 '+PAT_POST+' 根 '+tfn+' 內，新 → 舊）</span></h3>'
      + (R.post.length ? table('patpost'+dr, ['幣', '型態', '突破時間', '突破線', '現價', '停損', '目標', '量'], R.post, e => [
          {v:e.inst, h:coin(e.inst)}, {v:e.pat, h:e.pat}, {v:e.t, h:txTime(e.t, true) + `<span class="dim"> ${e.ago} 根前</span>`},
          {v:e.level, h:txNum(e.level, P(e.inst))}, {v:e.last, h:txNum(e.last, P(e.inst))},
          {v:e.stop, h:txNum(e.stop, P(e.inst))}, {v:e.target, h:txNum(e.target, P(e.inst))}, {v:e.volx, h:f(e.volx, 1) + '倍'}]) : '<div class="empty">'+(S.at || S.busy ? '目前沒有' : '掃描中…')+'</div>')
      + '</div>';
  }
  return h;
}
function patOpen(inst){ CARD = null; TXSAVE = null; TAB = 'tx'; TXTF = PATTF; txSel('C:' + inst); }
// ★妖幣觀察清單（2026-10-02，Barry 10-01 直播「打妖幣的眉角」）：只顯示 yaobi 已算好的值（D.yao），不打 API、不下單。
//   用戶：「這什麼啊 密密麻麻的」→ 版面只留「現在能下的」小卡，其他（規則／全部訊號／觀察清單）收進折疊。
function yaoDay(ms){ if(!ms) return '—'; const d=new Date(ms), p=n=>String(n).padStart(2,'0'); return p(d.getUTCMonth()+1)+'-'+p(d.getUTCDate()); }
const YST = {'贏':['✅ 贏','up'], '輸':['❌ 輸','down'], '持倉中':['⏳ 持倉中',''], '等回踩':['⏳ 等回踩',''],
  '沒回踩・失效':['沒回踩・失效','dim'], '作廢（先破低）':['先破低・作廢','dim'], '沒長盤整・不追':['沒長盤整','dim'], '1H 抓不到':['⚠ 1H 抓不到','down']};
function yaoR(v){ return v==null ? '' : ' <span class="'+cls(v)+'">'+(v>=0?'+':'')+f(v,2)+'R</span>'; }
function yaoStat(t, lab){
  if(!t || !t.n) return '<div class="sub">'+lab+'：還沒有結案的單'+(t&&t.open ? '（進行中 '+t.open+'）' : '')+'</div>';
  return '<div class="sub">'+lab+'：結案 <b>'+t.n+'</b> 筆・贏 '+t.win+'・勝率 '+f(t.win/t.n*100,0)+'%・每筆 <b class="'+cls(t.avgR)+'">'+(t.avgR>=0?'+':'')+f(t.avgR,3)+'R</b>'
    + '・要穿過0.1%才成交 '+(t.avgR1==null?'—':(t.avgR1>=0?'+':'')+f(t.avgR1,3)+'R')+(t.open ? '・進行中 '+t.open : '')+'</div>';
}
function viewYao(){
  const p = D.yao || {};
  if(!p.ok) return '<div class="card"><h2>妖幣</h2><div class="empty">妖幣觀察沒有資料（'+(p.err||'未知')+'）</div></div>';
  const rule = p.rule || {}, wh = p.whale || {}, sigs = p.sigs || [], watch = p.watch || [], st = p.stats || {};
  const nowMs = (D.now||0)*1000;
  // 「現在能下」＝有長盤整、而且還在等回踩（限價掛著）；或 3 天內剛成交、還在持倉中
  const live = sigs.filter(s => s.ok && (s.state==='等回踩' || (s.state==='持倉中' && s.fill_ts && nowMs - s.fill_ts <= 3*86400000)));
  let h = '<div class="card"><h2>妖幣<span>只列出來・不下單・影子單記錄'+(p.last_run ? '・'+ago(p.last_run)+'前更新' : '')+'</span></h2>';
  if(p.err) h += '<div class="sub">⚠ '+p.err+'</div>';
  if(!live.length){
    h += '<div class="fpbig dim">目前沒有可以掛的單</div>';
  } else {
    for(const s of live){
      const gap = (s.last && s.e) ? (s.last/s.e-1)*100 : null, tp = (s.tps||[])[0];
      h += '<div class="fpsum">'
        + `<a class="cl" onclick="openCard('${s.inst}')"><b>${s.coin}</b></a>` + (wh[s.inst] ? ' 🐋' : '')
        + '　' + (s.state==='等回踩' ? '⏳ 限價掛 <b>'+pf(s.e)+'</b>' : '✅ 已成交 '+yaoDay(s.fill_ts)+'，進場 <b>'+pf(s.e)+'</b>')
        + '　現價 '+pf(s.last)+(gap==null ? '' : ' <span class="'+cls(gap)+'">'+(gap>=0?'+':'')+f(gap,1)+'%</span>')
        + '<br>停損 <b>'+pf(s.sl)+'</b> <span class="dim">−'+f(s.sld,1)+'%</span>　目標 1R <b>'+pf(tp)+'</b>'
        + '<br><span class="dim">'+yaoDay(s.day)+' 放量 ×'+f(s.vr,1)+'・盤整 '+s.box_d+' 天・從高點跌 '+f(s.dd,0)+'%・上市 '+f(s.age,0)+' 天</span></div>';
    }
  }
  h += '<div class="sech">影子單戰績（照規則掛單的話，只算有長盤整的）</div>' + yaoStat(st.live, '上線後') + yaoStat(st.backfill, '近 60 天回填');
  h += '<details><summary class="sech">規則</summary><div class="note">'
    + 'Barry 原話：從最高點跌 <b>'+(rule.min_dd||80)+'%</b> 以上的<b>新幣</b>（上市 ≤'+(rule.max_age||400)+' 天），<b>日線</b>出現量明顯變大的綠K，之後<b>等回踩再進</b>。'
    + '<br>回測定案：量 &gt; 前 '+(rule.vol_lb||10)+' 根日K最大量；放量前要有 ≥'+(rule.box_days||30)+' 天<b>長盤整</b>（沒有的三段回測都是負的）；'
    + '限價掛放量K實體中點，'+(rule.fill_days||5)+' 天沒成交或先跌破放量K低點就作廢；停損＝放量K低點；目標＝1R。'
    + '<br>回測（含補回的下架幣、扣費）：每筆 2020~21 +0.14R、2022~24 +0.09R、2025~26 +0.08R；'
    + '<b>要穿過中點 0.1% 才成交時 2025~26 只剩 +0.04R</b> → 優勢很薄，不是翻倉訊號。</div></details>';
  const cols = ['幣','放量日','盤整','進場','停損','狀態'];
  const row = s => {
    const z = YST[s.state] || [s.state||'—',''];
    return [
      {v:s.coin, h:`<a class="cl" onclick="openCard('${s.inst}')">${s.coin}</a>` + (wh[s.inst] ? ' 🐋' : '')},
      {v:s.day, h:yaoDay(s.day)}, {v:s.box_d||0, h:s.ok ? s.box_d+'天' : '<span class="dim">—</span>'},
      {v:s.e||0, h:pf(s.e)}, {v:s.sld||0, h:pf(s.sl)+' <span class="dim">−'+f(s.sld,1)+'%</span>'},
      {v:s.state||'', h:'<span class="'+z[1]+'">'+z[0]+'</span>'+yaoR(s.R)}];
  };
  const okS = sigs.filter(s => s.ok), noS = sigs.filter(s => !s.ok);
  h += '<details><summary class="sech">近 60 天有長盤整的放量訊號 <span class="dim">'+okS.length+'</span></summary>'
    + (okS.length ? table('yao2', cols, okS, row) : '<div class="empty">沒有</div>') + '</details>';
  h += '<details><summary class="sech">沒長盤整的放量訊號（不追，只列參考）<span class="dim">'+noS.length+'</span></summary>'
    + (noS.length ? table('yao4', cols, noS, row) : '<div class="empty">沒有</div>') + '</details>';
  h += '<details><summary class="sech">觀察清單（新幣、已跌 ≥'+(rule.min_dd||80)+'%、還沒放量）<span class="dim">'+watch.length+'</span></summary>'
    + (watch.length ? table('yao3', ['幣','從高點跌','上市','低點以來','現價'], watch, w => [
      {v:w.coin, h:`<a class="cl" onclick="openCard('${w.inst}')">${w.coin}</a>` + (wh[w.inst] ? ' 🐋' : '')},
      {v:w.dd, h:f(w.dd,1)+'%'}, {v:w.age, h:f(w.age,0)+'天'},
      {v:w.up_from_low==null?-1:w.up_from_low, h:w.up_from_low==null?'—':'+'+f(w.up_from_low,0)+'%'}, {v:w.last||0, h:pf(w.last)}])
      : '<div class="empty">沒有</div>') + '</details>';
  return h + '</div>';
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
      if(!(ae && (ae.id === 'cq' || ae.id === 'txq'))) draw();
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
