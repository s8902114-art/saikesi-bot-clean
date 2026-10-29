# -*- coding: utf-8 -*-
"""麥門軟體官方值記錄器（2026-10-29，用戶訂閱後）——只記錄、不下單。

為什麼要有：
- 用戶：「燈號也要記下來」「要在我沒開電腦的情況也要記」。燈號（上帝視角 RGLight）只畫在加權 60 分 K 最後一根，
  伺服器不給歷史（10-29 實測：905 根 60 分 K 只有最後一根有值）→ 不自己逐時記，就永遠拿不到。
- 順便每天把台指／加權的官方值（顏色、階梯、星星、背離、缺口）存檔：伺服器分 K 只給 60~180 天，存下來答案卷才不會被截掉，
  背離星的樣本才會一直變多（10-29 三個月只有 17 顆）。

登入憑證＝環境變數 MAINOW_TOKEN（用戶自己從瀏覽器 localStorage「@mai-now-Token」複製貼到 Railway）。
本模組**不印、不存、不寫進任何檔案或訊息**這個值（存檔只存官方回傳的資料欄，標頭裡的 Token 會丟掉）。

協定（trader.mai-now.com 前端 main.b9047d35.js 讀出，10-29）：
- wss://is.mai-now.com/Quote/，子協定 WSNetMQ。每則訊息＝多個部分，每部分一個 WebSocket frame，frame 第 1 個 byte＝「後面還有部分」1／最後 0。
- 請求＝[uint32 LE 型別][zlib(JSON)]；回應＝[uint32 型別][zlib(JSON 標頭)][zlib(資料 JSON)]。心跳＝單一 byte 9（每 10 秒）。
- ColorsKLine＝117：{Token, RequestKey, StockCode, HType(4 分K／8 日K), HBase(15/60/1), StartTime, EndTime}（時間＝ISO 字串＋08:00）。
  回應每根 K 一筆：Time(.NET ticks，台北本地)、Color_P1/P2、Ladder_P1/P2、BigVol、BigVolEx、Divergence、KSpace、RGLight。
"""
import gzip, json, os, struct, threading, time, traceback, zlib
import datetime as dt

try:
    import websocket                     # websocket-client
except Exception:                        # 沒裝就整個模組不動作（status() 會說）
    websocket = None

URL = "wss://is.mai-now.com/Quote/"
ORIGIN = "https://trader.mai-now.com"
T_COLORS = 117
T_ACCOUNT = 512
_HERE = os.path.dirname(os.path.abspath(__file__))
_PERSIST = os.environ.get("PERSIST_DIR") or ("/data" if os.path.isdir("/data") else _HERE)
DIR = os.path.join(_PERSIST, "mainow")
LIGHT_FILE = os.path.join(DIR, "light.jsonl")              # 每次抓到的燈號（一行一筆）
TPE = dt.timezone(dt.timedelta(hours=8))
# 存檔的序列：(商品, HType, HBase, 每次往回抓幾天)
SERIES = [("TXF.00", 4, 15, 4), ("TXF.00", 4, 60, 4), ("TXF.00", 8, 1, 40),
          ("TSE", 4, 15, 4), ("TSE", 4, 60, 4), ("TSE", 8, 1, 40)]
LIGHT_EVERY_MIN = 5                                        # 加權盤中每 5 分鐘抓一次燈號

_state = {"enabled": False, "last_light": None, "last_light_at": None, "last_ok": None, "last_err": None,
          "err_at": None, "n_light": 0, "archive": {}, "kicked": False, "full_done": False}
_lock = threading.Lock()
_rk = [1000]


def _token():
    t = (os.environ.get("MAINOW_TOKEN") or "").strip()
    if len(t) >= 2 and t[0] == t[-1] == '"':
        t = t[1:-1]
    return t


def status():
    with _lock:
        s = dict(_state); s["archive"] = dict(_state["archive"])
    s["has_token"] = bool(_token()); s["has_lib"] = websocket is not None
    return s


def _err(msg):
    with _lock:
        _state["last_err"] = msg; _state["err_at"] = time.time()
    print(f"[麥門記錄] {msg}", flush=True)


# ───────────────────────── 連線 ─────────────────────────
class _Conn:
    def __init__(self, timeout=25):
        self.ws = websocket.create_connection(URL, subprotocols=["WSNetMQ"], timeout=timeout, origin=ORIGIN,
                                              header=["User-Agent: Mozilla/5.0 (saikesi-bot mainow recorder)"])
        self.parts = []

    def auth(self, timeout=15):
        """連上後先送 Account（型別 512，{Token, RequestKey}）把這條連線綁到帳號——網頁是收到伺服器 Initialize 後呼叫 su() 做這件事；
        沒做的話請求會回 State=2（10-30 上線第一次實測就是這樣）。回傳伺服器回應標頭的 State（拿不到回 None）。"""
        _rk[0] += 1; rk = _rk[0]
        body = json.dumps({"Token": _token(), "RequestKey": rk}).encode()
        self.send([struct.pack("<I", T_ACCOUNT), zlib.compress(body)])
        t0 = time.time()
        while time.time() - t0 < timeout:
            msg = self.recv()
            if not msg or _type(msg) != T_ACCOUNT:
                continue
            for p in msg:
                if len(p) > 2 and p[0] == 0x78:
                    try:
                        h = json.loads(zlib.decompress(p).decode("utf-8"))
                        if h.get("RequestKey") == rk:
                            return h.get("State")
                    except Exception:
                        pass
        return None

    def close(self):
        try: self.ws.close()
        except Exception: pass

    def send(self, parts):
        for i, p in enumerate(parts):
            self.ws.send_binary(bytes([1 if i < len(parts) - 1 else 0]) + p)

    def recv(self):
        """收一則完整訊息（多個部分）。心跳／空訊息回傳 []。"""
        while True:
            f = self.ws.recv()
            if isinstance(f, str):
                f = f.encode()
            if not f:
                return []
            self.parts.append(f[1:])
            if not (f[0] & 1):
                msg, self.parts = self.parts, []
                return [p for p in msg]


def _type(msg):
    for p in msg:
        if len(p) == 4:
            return struct.unpack("<I", p)[0]
    return -1


def _iso(t):
    return t.strftime("%Y-%m-%dT%H:%M:%S+08:00")


def fetch_colors(conn, stock, htype, hbase, start, end, timeout=40):
    """回傳 (State, 每根 K 的 dict 清單)。State 1＝正常。Token 不會出現在回傳值裡。"""
    tok = _token()
    if not tok:
        raise RuntimeError("沒有設定 MAINOW_TOKEN")
    _rk[0] += 1; rk = _rk[0]
    body = json.dumps({"Token": tok, "RequestKey": rk, "StockCode": stock, "HType": htype, "HBase": hbase,
                       "StartTime": _iso(start), "EndTime": _iso(end)}).encode()
    conn.send([struct.pack("<I", T_COLORS), zlib.compress(body)])
    t0 = time.time()
    while time.time() - t0 < timeout:
        msg = conn.recv()
        if not msg:
            continue
        ty = _type(msg)
        if ty == 2:                                   # 連線狀態訊息（Initialize／KickOut…）
            st = [p for p in msg if len(p) == 4]
            if len(st) >= 2 and struct.unpack("<i", st[1])[0] == 7:
                with _lock: _state["kicked"] = True
                raise RuntimeError("伺服器回「被踢下線」")
            continue
        if ty != T_COLORS:
            continue
        zs = [p for p in msg if len(p) > 2 and p[0] == 0x78]
        if not zs:
            continue
        try:
            hdr = json.loads(zlib.decompress(zs[0]).decode("utf-8"))
        except Exception:
            continue
        if hdr.get("RequestKey") != rk:
            continue
        stt = hdr.get("State")
        rows = json.loads(zlib.decompress(zs[1]).decode("utf-8")) if (stt == 1 and len(zs) > 1) else []
        return stt, rows
    raise TimeoutError("伺服器沒回應")


def _ticks_to_str(t):
    """官方 Time（.NET ticks，台北本地時間）→ 'YYYY-MM-DD HH:MM'。"""
    sec = int(t) // 10_000_000 - 62135596800
    return dt.datetime.fromtimestamp(sec, dt.timezone.utc).strftime("%Y-%m-%d %H:%M")


# ───────────────────────── 存檔 ─────────────────────────
def _arc_path(stock, htype, hbase):
    return os.path.join(DIR, f"{stock}_{htype}_{hbase}.json.gz")


def _merge_save(stock, htype, hbase, rows):
    """把新抓的列併進存檔（同一根 K 以新的為準）。回傳存檔總根數。"""
    os.makedirs(DIR, exist_ok=True)
    p = _arc_path(stock, htype, hbase); old = {}
    if os.path.exists(p):
        try: old = json.load(gzip.open(p, "rt", encoding="utf-8"))
        except Exception: old = {}
    for r in rows:
        r = dict(r); k = str(r.pop("Time"))
        old[k] = r
    tmp = p + ".tmp"
    with gzip.open(tmp, "wt", encoding="utf-8") as f:
        json.dump(old, f, ensure_ascii=False)
    os.replace(tmp, p)
    return len(old)


def archive(conn, full=False):
    now = dt.datetime.now(TPE).replace(tzinfo=None)
    for stock, ht, hb, back in SERIES:
        days = (365 * 30 if ht == 8 else 200) if full else back
        try:
            st, rows = fetch_colors(conn, stock, ht, hb, now - dt.timedelta(days=days), now + dt.timedelta(days=1))
        except Exception as e:
            _err(f"存檔 {stock} {ht}/{hb} 失敗：{e}"); continue
        if st != 1:
            _err(f"存檔 {stock} {ht}/{hb} 伺服器回 State={st}（憑證過期或方案到期？）"); continue
        n = _merge_save(stock, ht, hb, rows)
        with _lock:
            _state["archive"][f"{stock}_{ht}_{hb}"] = {"total": n, "got": len(rows), "at": time.time(),
                                                       "first": _ticks_to_str(rows[0]["Time"]) if rows else None,
                                                       "last": _ticks_to_str(rows[-1]["Time"]) if rows else None}
            _state["last_ok"] = time.time()


def record_light(conn):
    now = dt.datetime.now(TPE).replace(tzinfo=None)
    st, rows = fetch_colors(conn, "TSE", 4, 60, now - dt.timedelta(days=4), now + dt.timedelta(days=1))
    if st != 1:
        raise RuntimeError(f"伺服器回 State={st}（憑證過期或方案到期？）")
    last = rows[-1] if rows else None
    rg = last.get("RGLight") if last else None
    rec = {"fetched": now.strftime("%Y-%m-%d %H:%M:%S"), "bar": _ticks_to_str(last["Time"]) if last else None,
           "light": rg, "color_p1": last.get("Color_P1") if last else None}
    os.makedirs(DIR, exist_ok=True)
    with open(LIGHT_FILE, "a", encoding="utf-8") as f:
        f.write(json.dumps(rec, ensure_ascii=False) + "\n")
    with _lock:
        _state["last_light"] = rg; _state["last_light_at"] = rec["fetched"]; _state["n_light"] += 1; _state["last_ok"] = time.time()
    return rec


def lights(n=500):
    """最近 n 筆燈號紀錄（給儀表板／對帳用）。"""
    if not os.path.exists(LIGHT_FILE):
        return []
    with open(LIGHT_FILE, encoding="utf-8") as f:
        ls = f.readlines()[-n:]
    out = []
    for l in ls:
        try: out.append(json.loads(l))
        except Exception: pass
    return out


# ───────────────────────── 排程 ─────────────────────────
def _tse_session(now):
    """加權盤中（週一~五 09:00~13:40，含收盤後 10 分鐘把最後一根抓到）。假日照抓，伺服器會回同一根，無害。"""
    if now.weekday() >= 5:
        return False
    m = now.hour * 60 + now.minute
    return 9 * 60 <= m <= 13 * 60 + 40


def loop(notify=None):
    """每 60 秒醒一次：加權盤中每 5 分鐘記燈號；每天 13:50、05:10 各存一次全部序列；啟動時先把伺服器給得到的全部歷史存一次。"""
    if websocket is None:
        _err("沒有安裝 websocket-client，記錄器不啟動"); return
    if not _token():
        _err("沒有設定 MAINOW_TOKEN，記錄器不啟動（到 Railway Variables 新增）"); return
    with _lock: _state["enabled"] = True
    last_light_slot = None; last_arc_slot = None; warned_at = 0
    while True:
        now = dt.datetime.now(TPE).replace(tzinfo=None)
        try:
            need_full = not _state["full_done"]
            slot_light = (now.strftime("%Y%m%d%H") + str(now.minute // LIGHT_EVERY_MIN)) if _tse_session(now) else None
            hm = now.hour * 60 + now.minute
            slot_arc = None
            if 13 * 60 + 50 <= hm < 14 * 60 + 5: slot_arc = now.strftime("%Y%m%d") + "A"
            if 5 * 60 + 10 <= hm < 5 * 60 + 25: slot_arc = now.strftime("%Y%m%d") + "B"
            todo_light = slot_light and slot_light != last_light_slot
            todo_arc = need_full or (slot_arc and slot_arc != last_arc_slot)
            if todo_light or todo_arc:
                c = _Conn()
                try:
                    st = c.auth()
                    if st != 1:
                        raise RuntimeError(f"帳號綁定失敗 State={st}（憑證過期或方案到期？）")
                    if todo_light:
                        record_light(c); last_light_slot = slot_light
                    if todo_arc:
                        archive(c, full=need_full)
                        if need_full:
                            with _lock: _state["full_done"] = True
                        last_arc_slot = slot_arc
                finally:
                    c.close()
        except Exception as e:
            _err(f"{type(e).__name__}: {e}")
            if notify and time.time() - warned_at > 12 * 3600:
                warned_at = time.time()
                try: notify(f"⚠️ 麥門記錄器出錯：{e}\n（燈號/官方值暫停記錄；若是憑證過期，到 trader.mai-now.com 重新複製 @mai-now-Token 貼到 Railway 的 MAINOW_TOKEN）")
                except Exception: pass
            time.sleep(120)
        time.sleep(60)


if __name__ == "__main__":
    # 手動測試：python -u mainow.py（憑證要放在環境變數，例如 railway run python -u mainow.py）。只印統計，不印憑證。
    import sys
    try: sys.stdout.reconfigure(encoding="utf-8")
    except Exception: pass
    print("有憑證" if _token() else "沒有 MAINOW_TOKEN", "｜套件", "有" if websocket else "沒有")
    c = _Conn()
    try:
        print("帳號綁定 State", c.auth())
        rec = record_light(c); print("燈號", rec)
        now = dt.datetime.now(TPE).replace(tzinfo=None)
        st, rows = fetch_colors(c, "TXF.00", 4, 15, now - dt.timedelta(days=3), now + dt.timedelta(days=1))
        print("台指15分 State", st, "根數", len(rows), "最後一根", _ticks_to_str(rows[-1]["Time"]) if rows else None,
              "欄位", sorted({k for r in rows for k in r}) if rows else None)
    finally:
        c.close()
    print("存檔資料夾", DIR)
