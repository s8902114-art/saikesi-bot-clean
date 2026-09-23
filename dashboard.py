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


def put(symbol, tf, **kv):
    """掃描迴圈每掃一個 (幣, 時框) 就呼叫一次。永不拋例外。"""
    try:
        with _LOCK:
            d = _DASH.setdefault(symbol, {})
            row = d.setdefault(tf, {})
            row.update(kv)
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


def collect(G):
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
        "diag": _diags(G),
        "flags": _flags(G),
        "coins": coins,
        "signals": sigs,
    }


# ── 路由 ────────────────────────────────────────────────────────────────────
def register(app, G):
    """在 main.py 的 Flask app 上掛載。G = main.py 的 globals()（讀到的永遠是當下值）。"""
    from flask import jsonify, make_response

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
            r = jsonify(collect(G))
        except Exception as e:
            r = jsonify({"error": f"{type(e).__name__}: {e}"})
        r.headers["X-Robots-Tag"] = "noindex, nofollow"
        r.headers["Cache-Control"] = "no-store"
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
       padding:12px 12px calc(28px + env(safe-area-inset-bottom));-webkit-text-size-adjust:100%}
  h1{font-size:16px;margin:0;font-weight:600;letter-spacing:.02em}
  .top{display:flex;align-items:center;gap:10px;flex-wrap:wrap;margin-bottom:12px}
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
let D = null, TAB = 'pos', SORT = {};

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

const TABS = [['pos','持倉'],['oi','OI 榜'],['coins','幣種'],['diag','漏斗'],['sig','訊號'],['sys','開關']];

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
  if(TAB==='pos') v.innerHTML = viewPos();
  if(TAB==='oi') v.innerHTML = viewOI();
  if(TAB==='coins') v.innerHTML = viewCoins();
  if(TAB==='diag') v.innerHTML = viewDiag();
  if(TAB==='sig') v.innerHTML = viewSig();
  if(TAB==='sys') v.innerHTML = viewSys();
}

function viewPos(){
  return '<div class="card"><h2>追蹤中的倉 <span>未實現 R 用被動快照最新價估，非交易所回報</span></h2>' +
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
  return '<div class="card"><h2>OI 增幅 <span>追蹤 '+o.tracked+' 個合約・讀記憶體不打 API</span></h2>'
    + tbl('oiu', o.up) + '</div>'
    + '<div class="card"><h2>OI 降幅</h2>' + tbl('oid', o.down) + '</div>';
}

function viewCoins(){
  const rows=[];
  for(const [sym,tfs] of Object.entries(D.coins))
    for(const [tf,r] of Object.entries(tfs)) rows.push({sym,tf,...r});
  const q=(document.getElementById('cq')||{}).value||'';
  const flt = q ? rows.filter(r=>r.sym.toLowerCase().includes(q.toLowerCase())) : rows;
  return '<div class="card"><h2>掃描快照 <span>bot 掃到什麼就顯示什麼</span></h2>'
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
  let h='<div class="card"><h2>策略漏斗 <span>Δ = 距上次開這頁 '
        + (D.diag.elapsed?D.diag.elapsed+'s':'首次')+'</span></h2>'
        + '<div class="sub" style="margin-bottom:10px">只看各 gate 的絕對次數與增量；'
        + '某個擋點的 Δ ≒ 呼叫的 Δ ＝ 整條策略被那道閘卡死。</div></div>';
  for(const [name,rows] of Object.entries(D.diag.groups)){
    h += '<div class="card"><h2>'+name+'</h2><div class="grid">' + rows.map(r=>
      `<div class="kv"><span class="dim">${r.k}</span><b>${r.v}${
        r.d?` <span class="${r.d>0?'warn':'dim'}">+${r.d}</span>`:''}</b></div>`).join('')
      + '</div></div>';
  }
  return h;
}

function viewSig(){
  return '<div class="card"><h2>最近訊號 <span>bot 實際發出的</span></h2>'
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
    const r = await fetch(API, {cache:'no-store'});
    if(r.ok){ D = await r.json(); draw(); }
  }catch(e){}
}
tick(); setInterval(tick, 20000);
document.addEventListener('visibilitychange', ()=>{ if(!document.hidden) tick(); });
</script></body></html>
"""
