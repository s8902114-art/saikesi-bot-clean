# -*- coding: utf-8 -*-
"""
每日 00:00(UTC)真診斷復盤(V1 + OKX 平倉明細)。純分析無下單。
價值=分流:把「勝率差」分成 正常變異(別動) / 行為漏洞群發(限倉降risk) / regime不利(降size)。
掛法:① 進場處 record_entry(coin,tf,side,strat) ② 主迴圈 r=daily_tick(ex); if r: dc_log(r)
"""
import os, json
from datetime import datetime, timezone, timedelta

HERE = os.path.dirname(os.path.abspath(__file__))
ENTRIES = os.path.join(HERE, "report_entries.json")
STATE   = os.path.join(HERE, "report_state.json")
CLUSTER_N   = 4        # 同向同策略 2h 內 ≥ N 單 → 群發紅旗
CLUSTER_WIN = 7200     # 2 小時(秒)

def _load(p, d):
    try: return json.load(open(p, encoding="utf-8"))
    except Exception: return d
def _save(p, o):
    try: json.dump(o, open(p, "w", encoding="utf-8"), ensure_ascii=False, default=str)
    except Exception: pass

def record_entry(coin, tf, side, strat):
    """進場時呼叫,記一筆。strat 用 exit_strategy 或可讀策略名。"""
    e = _load(ENTRIES, [])
    e.append({"t": datetime.now(timezone.utc).timestamp(), "coin": str(coin),
              "tf": str(tf), "side": str(side), "strat": str(strat or "?")})
    # 只留近 7 天,避免無限長
    cut = datetime.now(timezone.utc).timestamp() - 7*86400
    _save(ENTRIES, [x for x in e if x.get("t", 0) >= cut])

def _fetch_closed_pnl(ex, since_ms):
    """OKX 平倉明細 realizedPnl → [{symbol, side, pnl, ct(ms)}]。失敗回 []."""
    out = []
    try:
        hist = ex.fetch_positions_history(None, since_ms, 100)
        for h in hist:
            info = h.get("info", {}) or {}
            pnl = info.get("realizedPnl", info.get("pnl"))
            if pnl is None: pnl = h.get("realizedPnl") or 0
            out.append({"symbol": h.get("symbol"), "side": (h.get("side") or "").lower(),
                        "pnl": float(pnl or 0),
                        "ct": int(info.get("uTime") or info.get("cTime") or 0)})
    except Exception as e:
        out = [{"err": str(e)[:140]}]
    return out

def _attr_strat(closed, entries):
    """把每筆平倉P&L對到策略:同 coin、進場時間在平倉前最近的那筆。"""
    res = {}  # strat -> [pnl,...]
    for c in closed:
        if "err" in c: continue
        coin = (c["symbol"] or "").split("/")[0].split("-")[0].upper()
        cands = [e for e in entries if e["coin"].upper() == coin and e["t"] * 1000 <= c["ct"]]
        strat = max(cands, key=lambda e: e["t"])["strat"] if cands else "未知"
        res.setdefault(strat, []).append(c["pnl"])
    return res

def _clusters(entries):
    """同向同策略 2h 內 ≥CLUSTER_N → 回 [(strat,side,count)]"""
    flags = []
    groups = {}
    for e in entries: groups.setdefault((e["strat"], e["side"]), []).append(e["t"])
    for (strat, side), ts in groups.items():
        ts = sorted(ts); i = 0
        for j in range(len(ts)):
            while ts[j] - ts[i] > CLUSTER_WIN: i += 1
            if j - i + 1 >= CLUSTER_N:
                flags.append((strat, side, j - i + 1)); break
    return flags

def build_report(ex):
    now = datetime.now(timezone.utc)
    since = now - timedelta(hours=24)
    entries = [e for e in _load(ENTRIES, []) if e["t"] >= since.timestamp()]
    closed = _fetch_closed_pnl(ex, int(since.timestamp() * 1000))
    valid = [c for c in closed if "err" not in c]
    err = next((c["err"] for c in closed if "err" in c), None)

    L = [f"📊 **每日復盤** {now.strftime('%Y-%m-%d')} (近24h)"]
    # P&L
    if valid:
        tot = sum(c["pnl"] for c in valid); wins = sum(1 for c in valid if c["pnl"] > 0)
        wr = wins / len(valid) * 100
        L.append(f"已實現 **{tot:+.2f}U** | 平倉 {len(valid)} 筆 勝率 {wr:.0f}% ({wins}勝{len(valid)-wins}敗)")
        by = _attr_strat(valid, entries)
        for s, ps in sorted(by.items(), key=lambda kv: sum(kv[1])):
            L.append(f"  · {s}: {sum(ps):+.2f}U ({len(ps)}筆)")
    else:
        L.append(f"平倉明細抓取失敗或無平倉" + (f":{err}" if err else ""))

    # 進場活動 + 方向偏斜
    nL = sum(1 for e in entries if e["side"] in ("long", "多"))
    nS = len(entries) - nL
    L.append(f"今日進場 {len(entries)} 筆(多{nL}/空{nS})")

    # ★分流診斷
    cl = _clusters(entries)
    if cl:
        for strat, side, n in cl:
            L.append(f"🔴 **群發紅旗**:2h 內 {n} 個「{strat}·{side}」相關單。相關曝險過高")
        L.append("→ 拉已知槓桿:`!risk` 降、`!setslots` 限倉、或同向只取 R:R 最佳數個")
    elif valid and sum(c["pnl"] for c in valid) < 0:
        nstrat = len(set(e["strat"] for e in entries))
        if nstrat >= 3:
            L.append("🟡 虧損但**無群發、分散在多策略** → 大概率正常變異,**勿因單日改策略**")
        else:
            L.append("🟠 虧損集中、策略單一 → 檢視是否該策略 regime 不利")
    elif max(nL, nS) >= 6 and min(nL, nS) <= 1:
        L.append(f"🟠 **方向嚴重偏斜**({nL}多/{nS}空)→ 若與大盤趨勢逆,考慮整體降 size")
    else:
        L.append("🟢 行為正常,無群發/無偏斜")
    return "\n".join(L)

def daily_tick(ex):
    """每輪呼叫;UTC 跨日後在 00:xx 時段發一次。回報告字串或 None。"""
    today = datetime.now(timezone.utc).strftime("%Y-%m-%d")
    st = _load(STATE, {})
    if st.get("last") == today: return None
    if datetime.now(timezone.utc).hour != 0: return None   # 只在 00:00-00:59 觸發
    st["last"] = today; _save(STATE, st)
    try: return build_report(ex)
    except Exception as e: return f"📊 每日復盤生成失敗:{str(e)[:140]}"
