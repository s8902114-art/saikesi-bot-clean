# 賽克斯交易機器人 (Saikesi Bot) — Claude Skill

## 系統概覽

這是一個加密貨幣自動交易系統，部署在 Railway 上，透過 Discord 進行控制。

- **版本**: v4
- **策略**: QQE MOD 雙軌交叉 + CVD 背離過濾
- **時框**: 15m / 30m / 1H / 4H
- **監控幣種**: 40+ 個（市值前100 × OKX 永續，動態更新）
- **交易所**: OKX（主）+ BingX（副）

---

## 部署架構

```
程式碼編輯  →  Replit (workspace/)
推送指令    →  bash push.sh
目標 Repo   →  GitHub: s8902114-art/saikesi-bot-clean
自動部署    →  Railway (專案: hopeful-endurance)
控制介面    →  Discord Bot (頻道 ID: 1505971611042320616)
```

### 關鍵注意事項
- Railway 連結的 repo 是 `saikesi-bot-clean`，**不是** `saikesi-bot`
- `push.sh` 已設定指向 `saikesi-bot-clean`，直接跑 `bash push.sh` 即可
- Railway 在 push 後會自動 redeploy（約 1-2 分鐘）
- `auto_push.py` 監控 main.py 變更會自動觸發 push（注意避免重複部署）

---

## 環境變數（Railway 設定）

| 變數名稱 | 說明 |
|---|---|
| `OKX_API_KEY` | OKX API 金鑰（有值 = 實盤模式）|
| `OKX_SECRET_KEY` | OKX Secret |
| `OKX_PASSPHRASE` | OKX Passphrase |
| `DISCORD_TOKEN` | Discord Bot Token |
| `DISCORD_CHANNEL_ID` | Discord 通知頻道 ID |
| `BINGX_API_KEY` | BingX API 金鑰 |
| `BINGX_SECRET_KEY` | BingX Secret |
| `TG_BOT_TOKEN` | Telegram（選用）|
| `TG_CHAT_ID` | Telegram Chat ID（選用）|

**實盤/模擬判斷邏輯**:
```python
_LIVE_MODE = True if os.environ.get("OKX_API_KEY") else False
```
Railway 沒有設定 `OKX_API_KEY` → 自動進入 Paper 模式（📝）

---

## 核心策略參數

```python
MAX_LEVERAGE   = 100       # 最高槓桿
RISK_PCT       = 0.10      # 單筆最大風險 = 總資金 × 10%
SIGNAL_COOLDOWN = 1800     # 同幣種同時框冷卻時間（秒）
MAX_CONSEC_LOSS = 3        # 最大連虧次數 → 觸發熔斷
PAUSE_HOURS    = 24        # 熔斷冷卻時間（小時）
```

### 各時框自動下單開關（`AUTO_TRADE`）
```python
"15m": True   # 自動下單
"30m": True   # 自動下單
"1H":  True   # 自動下單
"4H":  False  # 僅發 Discord 通知，需手動授權
```

### 其他開關
```python
CVD_ENABLED = False   # CVD 背離過濾（/cvd on 啟用）
ADX_ENABLED = True    # ADX 強度過濾
MARGIN_MODE = "cross" # 保證金模式（cross / isolated）
```

---

## Discord 指令列表

| 指令 | 功能 |
|---|---|
| `!status` | 查看 Bot 運行狀態 |
| `!pause` / `!resume` | 暫停 / 恢復交易 |
| `/trade [15m\|30m\|1h\|4h\|all] [on\|off]` | 開關時框自動下單 |
| `/margin isolated\|cross` | 切換保證金模式 |
| `/exchange okx\|bingx on\|off` | 開關交易所路由 |
| `/cvd on\|off` | 開關 CVD 背離過濾 |
| `!setslots [數字]` | 設定倉位格數 |

---

## 程式進程架構（3個執行緒）

```
主執行緒      → main_polling_loop()     # 輪詢市場掃描
執行緒 2      → poll_dc_commands()      # Discord 指令輪詢（每5秒）
執行緒 3      → run_embedded_web_server() # Flask Web（Port 3000）
```

### 常見「兩個進程」問題
症狀：Discord 每個指令回覆兩次
原因：Railway redeploy 時舊容器沒有完全終止
解法：Railway → Variables → 改任一環境變數值強制重啟

---

## 常見錯誤與解法

### 1. `setMarginMode() params["lever"] should be between 1 and 125`
**原因**: `ex.set_margin_mode()` 呼叫帶入了多餘的 `lever` 參數  
**解法**: 在 main.py 中移除或修正 `set_margin_mode` 呼叫
```python
# 正確寫法（若需要）：
ex.set_margin_mode("isolated", symbol, params={})
# 或直接刪除這行，OKX 不需要每次下單前設定
```

### 2. 推送後 Railway 沒有更新
**原因**: push.sh 推到了錯誤的 repo（`saikesi-bot` 而非 `saikesi-bot-clean`）  
**確認方法**:
```bash
head -3 ~/workspace/push.sh
# REPO 那行應該是 saikesi-bot-clean
```
**修正**:
```bash
sed -i 's/saikesi-bot.git/saikesi-bot-clean.git/' push.sh
```

### 3. Mode 顯示 📝 Paper（應為實盤）
**原因**: Railway 沒有設定 `OKX_API_KEY` 環境變數  
**解法**: Railway → Variables → 確認 `OKX_API_KEY` 有值

### 4. Discord 指令完全沒反應
**原因**: `poll_dc_commands` 線程中斷，或 Bot Token 失效  
**確認**: Railway logs 有沒有「Discord 指令輪詢啟動」訊息

### 5. Replit 自動跑起來干擾
**原因**: `.replit` 中有 `shell.exec` workflow  
**解法**:
```bash
python3 -c "
c = open('.replit').read()
c = c.replace('task = \"shell.exec\"', 'task = \"echo done\"')
c = c.replace('task = \"workflow.run\"', 'task = \"echo done\"')
open('.replit','w').write(c)
print('done')
" && git add .replit && git commit -m "disable auto-run" && git push
```

---

## 標準推送流程

```bash
# 修改 main.py 後
cd ~/workspace && bash push.sh

# 若 push.sh 壞了，手動推：
git add main.py && git commit -m "說明修改內容" && \
git push https://${GITHUB_TOKEN}@github.com/s8902114-art/saikesi-bot-clean.git main --force
```

---

## 幣種動態更新機制

- 啟動時自動執行 `build_dynamic_symbols()`
- 來源：CoinGecko 市值前100 × OKX 上線永續合約
- 每 7 天自動重新抓取
- 失敗時回退硬編碼備援列表（40個幣種）
- 快取位置：`symbols_cache.json`

---

## 下單邏輯摘要

1. 策略掃描觸發訊號（QQE MOD 雙軌交叉）
2. ADX 過濾（若 `ADX_ENABLED`）
3. CVD 背離確認（若 `CVD_ENABLED`）
4. 冷卻時間檢查（`SIGNAL_COOLDOWN = 1800s`）
5. `AUTO_TRADE[tf]` 為 True → 自動下單；False → 發 Discord 通知等手動確認
6. 同時送往 OKX 和 BingX（各自的 `EXCHANGE_ENABLED` 開關）
7. 熔斷檢查（連虧 `MAX_CONSEC_LOSS` 次 → 暫停 `PAUSE_HOURS` 小時）

---

## 使用說明

**每次問 Bot 相關問題時，請將此 SKILL.md 貼給 Claude，讓他能直接了解系統架構，不需重新解釋背景。**

若有新的問題或修改，請告知 Claude 更新此 Skill 文件。
