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
    "多頭建倉": ("看漲·主動", "最強看多；留意過熱回調", "up"),
    "空頭平倉": ("看漲·被動", "動力來自空頭出場、不是新買盤；需真實多單接力，否則易反轉回落", "up"),
    "空頭建倉": ("看跌·主動", "最強看空", "down"),
    "多頭平倉": ("看跌·被動", "多單在離場", "down"),
}


def _at(hist, target_ts, tol):
    """取 hist 中**最接近** target_ts 的點（差距須 ≤ tol）；沒有就回 None。

    ★不可以寫成「取 ≤ target+tol 的最後一點」：那會系統性偏向**較新**的點，
      1H 窗實際只量到 45 分鐘 —— 實測 +30% 被算成 +13%（2026-09-24 被測試抓到）。
    ★也不可以在超出 tol 時硬拿最舊的點頂替：歷史不夠長就是不夠長，寧可不顯示，
      不可以拿 12h 的變化標成 1H。
    """
    best = None
    for t, v in hist:
        d = abs(t - target_ts)
        if d <= tol and (best is None or d < best[0]):
            best = (d, t, v)
    return (best[1], best[2]) if best else None


def _market(G, win_h=1.0, top_n=300):
    """★四象限（OI 變化 × 價格變化）。兩邊都讀記憶體，零 API。

    OI ← `_oi_history`、價 ← `_PX_HISTORY`，**同一個時間窗**（都由 `_oi_sample_tick` 每 15 分鐘取樣）。
    官方 OI 異動排名看的是 **1H**，所以預設 1H；UI 可切 4H/12H。
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
        tol = 1800.0                      # 取樣 15 分鐘一次，允許基準點落在目標前後半小時內
        for inst, hist in list(oi_all.items()):
            if not hist or len(hist) < 2:
                continue
            base = _at(hist, target, tol)
            # ★基準點必須**真的落在這個窗的起點附近**。只檢查上界不夠：歷史稀疏時
            #   `_at` 會回一個 12 小時前的點，卻被當成 1H 窗算 → 數字大好幾倍還標著 1H。
            #   寧可不顯示，也不可以拿不同窗的東西混進來比。
            if not base or base[1] <= 0:
                continue
            l_v = hist[-1][1]
            d_usd = l_v - base[1]
            oi_pct = d_usd / base[1]
            ph = px_all.get(inst) or []
            pbase = _at(ph, target, tol) if len(ph) >= 2 else None
            if not pbase or pbase[1] <= 0:
                continue                  # 價格同理：不同窗不可以混（同上）
            px_pct = (ph[-1][1] - pbase[1]) / pbase[1]
            quad = ("多頭建倉" if px_pct > 0 else "空頭建倉") if oi_pct > 0 else \
                   ("空頭平倉" if px_pct > 0 else "多頭平倉")
            s = snap.get(inst) or {}
            mc = mcap.get(inst.replace("-USDT-SWAP", ""))
            rows.append({
                "inst": inst, "oi": oi_pct, "d_usd": d_usd, "px": px_pct, "q": quad,
                "oiu": l_v, "last": s.get("last"), "chg24h": s.get("chg24h"),
                "vol": s.get("volccy_usd"), "oimc": (l_v / mc) if mc else None,
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
        rows.sort(key=lambda r: abs(r.get("d_usd") or 0), reverse=True)   # 官方:依變化金額
    except Exception:
        pass
    return {"win_h": win_h, "quads": {k: list(v) for k, v in _QUAD.items()},
            "tracked": len(G.get("_oi_history") or {}),
            "priced": len(G.get("_TICKER_SNAP") or {}),
            "sample": dict(G.get("_DASH_SAMPLE") or {}),
            "rows": rows[:top_n]}


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
  .sc{width:100%;height:auto;display:block;margin:2px 0 6px;overflow:visible}
  .sc .ql{font-size:9px;font-weight:600;opacity:.85}
  .sc .qr{text-anchor:end}
  .sc .ax{font-size:8px;fill:var(--dim)}
  .sc .pl{font-size:8px;fill:var(--fg)}
  .sl{display:flex;align-items:center;gap:10px;margin:6px 0}
  .sl label{font-size:12px;color:var(--dim);white-space:nowrap;min-width:108px}
  .sl label b{color:var(--fg);font-variant-numeric:tabular-nums}
  .sl input{flex:1;accent-color:var(--accent);height:26px}
  .qh{text-transform:none;letter-spacing:0;font-size:13px;font-weight:600}
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
</div>

<div class="tabs" id="tabs"></div>
<div id="view"></div>

<script>
const API = location.pathname.replace(/\\/$/,'') + '/api';
let D = null, TAB = 'mkt', SORT = {}, QF = '';

const f = (n,d=2)=> (n===null||n===undefined||isNaN(n)) ? '—' : Number(n).toFixed(d);
// 幣價：大幣固定 4 位、小幣改用有效位數（否則 PEPE 之類會被截成 0.000010）
const pf = n => (n===null||n===undefined||n===''||isNaN(n)) ? '—'
  : (Math.abs(Number(n))>=1 ? Number(n).toFixed(4) : Number(n).toPrecision(5))
      .replace(/(\\.\\d*?)0+$/,'$1').replace(/\\.$/,'');
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

const TABS = [['mkt','篩選器'],['rank','OI 排名'],['pos','持倉'],['coins','幣種'],
              ['diag','漏斗'],['sig','訊號'],['sys','開關']];
// 官方四象限順序：左上 空頭平倉 / 右上 多頭建倉 / 左下 多頭平倉 / 右下 空頭建倉
const QUADS = ['多頭建倉','空頭平倉','空頭建倉','多頭平倉'];
const QCLR = {'多頭建倉':'var(--up)','空頭平倉':'#6fd3a8','空頭建倉':'var(--down)','多頭平倉':'#e08a94'};
let W = 1, OITH = 2, PXTH = 2;
try{ const s=JSON.parse(localStorage.getItem('dash')||'{}');
     if(s.W) W=s.W; if(s.OITH) OITH=s.OITH; if(s.PXTH) PXTH=s.PXTH; }catch(e){}
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

  const v=document.getElementById('view');
  if(TAB==='mkt') v.innerHTML = viewMkt();
  if(TAB==='rank') v.innerHTML = viewRank();
  if(TAB==='pos') v.innerHTML = viewPos();
  if(TAB==='oi') v.innerHTML = viewOI();
  if(TAB==='coins') v.innerHTML = viewCoins();
  if(TAB==='diag') v.innerHTML = viewDiag();
  if(TAB==='sig') v.innerHTML = viewSig();
  if(TAB==='sys') v.innerHTML = viewSys();
}

function winBar(){
  return '<div class="wins">' + [[1,'1H'],[4,'4H'],[12,'12H']].map(([w,n])=>
    `<div class="wb ${W===w?'on':''}" onclick="setW(${w})">${n}</div>`).join('') + '</div>';
}
function noData(m){
  return '<div class="card">' + winBar() + '<div class="empty">'
    + `${m.win_h}H 窗還沒累積夠（每 15 分鐘取樣，追蹤 ${m.tracked} 個合約、報價 ${m.priced} 個）。`
    + ' bot 重啟會歸零；1H 窗約一小時後就有。</div></div>';
}

// ── 視覺篩選器：X=OI 變化%，Y=價格變化%，四象限 + 拉桿門檻框 ─────────────────
function scatter(rows){
  const Wd=360, Ht=300, L=34, R=8, T=10, B=24;
  const x0=L, x1=Wd-R, y0=T, y1=Ht-B, cx=(x0+x1)/2, cy=(y0+y1)/2;
  const mx = Math.max(OITH*1.6, ...rows.map(r=>Math.abs(r.oi*100)))*1.05 || 10;
  const my = Math.max(PXTH*1.6, ...rows.map(r=>Math.abs(r.px*100)))*1.05 || 10;
  const X = v => cx + (v*100/mx)*((x1-x0)/2);
  const Y = v => cy - (v*100/my)*((y1-y0)/2);
  const hit = r => Math.abs(r.oi*100)>=OITH && Math.abs(r.px*100)>=PXTH;
  let s = `<svg viewBox="0 0 ${Wd} ${Ht}" class="sc">`;
  s += `<rect x="${cx}" y="${y0}" width="${x1-cx}" height="${cy-y0}" fill="#35d07f" opacity=".07"/>`
    +  `<rect x="${x0}" y="${y0}" width="${cx-x0}" height="${cy-y0}" fill="#35d07f" opacity=".03"/>`
    +  `<rect x="${cx}" y="${cy}" width="${x1-cx}" height="${y1-cy}" fill="#ff5c6c" opacity=".07"/>`
    +  `<rect x="${x0}" y="${cy}" width="${cx-x0}" height="${y1-cy}" fill="#ff5c6c" opacity=".03"/>`;
  // 拉桿門檻框：四角虛線區 = 同時超過 OI 與價格門檻
  [[1,1],[-1,1],[1,-1],[-1,-1]].forEach(([sx,sy])=>{
    const bx = sx>0 ? X(OITH/100) : x0, bw = sx>0 ? x1-X(OITH/100) : X(-OITH/100)-x0;
    const by = sy>0 ? y0 : Y(-PXTH/100), bh = sy>0 ? Y(PXTH/100)-y0 : y1-Y(-PXTH/100);
    if(bw>0&&bh>0) s += `<rect x="${bx}" y="${by}" width="${bw}" height="${bh}" fill="none"
       stroke="#6aa3ff" stroke-width="1" stroke-dasharray="3 3" opacity=".55"/>`;
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
  for(const r of rows){ if(!hit(r)) continue;
    const c=QCLR[r.q], px=X(r.oi), py=Y(r.px);
    s += `<circle cx="${px.toFixed(1)}" cy="${py.toFixed(1)}" r="4" fill="${c}"/>`
      +  `<text x="${(px+6).toFixed(1)}" y="${(py+3.5).toFixed(1)}" class="pl">`
      +  `${r.inst.replace('-USDT-SWAP','')}</text>`; }
  return s + '</svg>';
}

function viewMkt(){
  const m=D.mkt, rows=m.rows||[];
  if(!rows.length) return noData(m);
  const hit = r => Math.abs(r.oi*100)>=OITH && Math.abs(r.px*100)>=PXTH;
  const sel = rows.filter(hit);
  return '<div class="card">' + winBar()
    + `<h2>視覺篩選器<span>${m.win_h}H・${rows.length} 個合約・命中 ${sel.length}</span></h2>`
    + scatter(rows)
    + `<div class="sl"><label>OI 變化 ≥ <b>${OITH}%</b></label>
         <input type="range" min="1" max="10" step="0.5" value="${OITH}"
                oninput="setTh('oi',this.value)"></div>`
    + `<div class="sl"><label>價格變化 ≥ <b>${PXTH}%</b></label>
         <input type="range" min="1" max="10" step="0.5" value="${PXTH}"
                oninput="setTh('px',this.value)"></div>`
    + (sel.length ? table('sel', ['幣','OI%','價%','象限'], sel, r=>[
          r.inst.replace('-USDT-SWAP',''),
          {v:r.oi, h:pct(r.oi), c:cls(r.oi)},
          {v:r.px, h:pct(r.px), c:cls(r.px)},
          {v:r.q, h:`<span style="color:${QCLR[r.q]}">${r.q}</span>`},
        ]) : '<div class="empty">目前沒有同時超過兩個門檻的幣，把拉桿往左拉。</div>')
    + '</div>';
}

// ── OI 異動排名：照官方分四組，組內依「變化金額」排 ────────────────────────
function viewRank(){
  const m=D.mkt, rows=m.rows||[];
  if(!rows.length) return noData(m);
  let h = '<div class="card">' + winBar()
    + `<h2>OI 異動排名<span>${m.win_h}H 持倉量變化・依變化金額排序</span></h2></div>`;
  for(const q of QUADS){
    const g = rows.filter(r=>r.q===q).slice(0,10);
    if(!g.length) continue;
    const meta = (m.quads&&m.quads[q]) ? m.quads[q] : ['',''];
    h += `<div class="card"><h2><span class="qh" style="color:${QCLR[q]}">${q}</span>`
      +  `<span title="${meta[1]}">${meta[0]}</span></h2>`
      + table('rk'+q, ['#','幣種','價格','OI變化','OI/市值','價24H'], g, r=>[
          {v:g.indexOf(r)+1, h:String(g.indexOf(r)+1), c:'dim'},
          {v:r.inst, h:(r.an===2?'<span class="star">★</span>':'')+r.inst.replace('-USDT-SWAP','')},
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

async function tick(){
  try{
    const r = await fetch(API + '?w=' + W, {cache:'no-store'});
    if(r.ok){ D = await r.json(); draw(); }
  }catch(e){}
}
tick(); setInterval(tick, 20000);
document.addEventListener('visibilitychange', ()=>{ if(!document.hidden) tick(); });
</script></body></html>
"""
