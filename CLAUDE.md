# 賽克斯交易機器人 (Saikesi Bot) — Claude Skill

## 系統概覽

這是一個加密貨幣自動交易系統，部署在 Railway 上，透過 Discord 進行控制。

- **版本**: v5（2026-05 策略大整理：全部組合經回測+Walk-Forward 驗證）
- **策略**: 多策略 OR 觸發（C3 + 雙底W底 + RSI共振 + MACD動能），詳見下方「實盤策略地圖」
- **時框**: 15m / 30m / 1H（4H 僅通知不下單）
- **監控幣種**: 40+ 個（市值前100 × OKX 永續，動態更新）
- **交易所**: OKX（主）+ BingX（副）

---

## 實盤策略地圖（2026-06-13 大改後現況）

> 全部組合都經回測 + Walk-Forward(70/30) 驗證為正期望才保留。回測腳本在上層 `trading-backtest/`。
> 進場邏輯集中在 `scan_and_process_market`（OR 邏輯，任一觸發即下單）。

| 時框 | 做多 | 做空 |
|---|---|---|
| **15m** | C3(+CVD吸收/ls<1.0 ×1.5)、雙底+RSI共振、**MACD動能+帶量+突破24** | 雙頂+RSI共振、數據獵手空(僅4H下跌、切線無加碼)、箱突破空(箱頂+killzone)、**維加斯大通道fade空(回測e576/676被擋+1H空方共振+CVD↓+ls≥2,固定R,牛市也正)** |
| **30m** | C3讓跑 | ❌ 停用 |
| **1H** | 雙底W底+突破、MACD多+帶量+tFlow+突破24、**★主力建倉壓縮突破(×1.5,讓跑)** | C3+階梯壓力、MACD空+帶量+tFlow+(突破24低 或 跌破維加斯大通道)、**★主力建倉壓縮突破(×1.5,讓跑)** |
| **週線事件** | **★CME週末缺口(2026-07-04,麥門移植)**:主流3幣專屬,週五21:00 UTC收盤vs週日23:00開盤缺口>1.5%→補滿後順補向,SL=前12根1H極值/TP2R/300h超時。回測7期WF 7/7正 EV+0.268 PF1.49,山寨無效。每週總預算2%攤給觸發幣(`CME_GAP_*`,`_cme_gap_poll`) | 同左(雙向) |

### ★2026-06-13 重大更新(治多空雙巴+補多空訊號,全經WF)
1. **突破閘(回看24根)**:動量(MACD空/多、W底)要求穿前24根S/R才進(治追漲追跌)。回看96→24(用戶指正96太長進場太晚):1H空+0.567→+0.655、1H多+0.218→+0.410。突破訊號 position_scale×1.5。C3/DH/box/共振不套。
2. **維加斯大通道fade空(VEGAS_SHORT_ENABLED)**:牛市也賺的15m逆勢空。回測e576/676下緣±0.5ATR被擋+收黑+1H空方共振(e144<e576且close<e144)+CVD↓+ls≥2。固定R,WF+0.182各年不虧。
3. **★主力建倉壓縮突破(OI_SQUEEZE_ENABLED,1H雙向)**:12h窄幅壓縮<3%(coiled spring)+帶量突破+12h OI升>5%(建倉)+CVD確認方向(防假突破)+4H regime順向。止損=range對邊±0.3ATR,**×1.5,讓跑(swing_full抓噴出)**。WF驗+0.395/勝50%/賺賠2.8/MDD5%/各年正。訊號卡顯示「主力建多/主力建空」。用戶逼出的成果。
4. **1H MACD空**:跌破前24低 或 跌破維加斯大通道;+避地板濾(離10日低<1ATR不空)。
5. **DH空**:4H下跌regime閘(牛市噤聲)+關加碼(line_add→line_full)。
6. **BingX槓桿**:全倉用幣種實際上限(>150x/黃金500x,封頂1000);全倉+風險制下注→升槓桿不改倉位只少鎖保證金、強平更遠。
7. **PYRAMID=False;RISK_PCT預設0.10→0.05**。
8. **掃描廣度**:加OKX每日漲跌幅前25(volCcy24h≥10M),幣種列表日更。
9. **每日報告部署**:00:00 UTC(台08:00)發Discord復盤(daily_report.py)。
- ★方法論鐵則:測出負EV要對照基準率(隨機做空-0.38,crypto上漂)→負≠無料,可能是「有edge但蓋不過趨勢」(當確認層)。逆勢空要堆夠MTF+結構+數據才過線。OI建倉=確認層非觸發器,但「緊壓縮+帶量突破+CVD」例外(主力建倉策略)。腳本見上層 `trading-backtest/_vegas_c3_aux.py`/`_comp_improve.py`/`_squeeze_fake.py`/`_breakout_all.py`。

**ls_ratio/taker_ratio 資料源**：OKX rubik `contracts/long-short-account-ratio`+`taker-volume`
(免費,快取5分,`_fetch_ls_taker`,舊名 `_fetch_binance_ls_taker` 仍是別名)。
★2026-06-30 改源:原幣安 fapi 被 Railway 雲端IP地理封鎖→熔斷噤聲 DH空/維加斯/逆勢多,
改 OKX(bot 既有拉K線的同源,不被封)。OKX rubik period 僅 5m/1H/1D,15m/30m 映到 5m;
ls=最新一根多空帳戶比,taker=最近3根 buyVol/sellVol。值域與幣安相近(ls~2-3.5),沿用原門檻。
DH_SHORT_ENABLED toggle。2B檢查在前擋,通過才打API不洗量。

**2026-06-02 新增 數據獵手 CVD（DH_CVD_ENABLED，C方案=加碼非過濾）**：
- **15m 多 CVD吸收加碼**：不過濾(保留全頻率)，CVD吸收(`合約CVD[-1]<CVD[-4]`)確認時
  下注 ×`DH_BOOST_MULT`(1.5)。函數 `_dh_cvd_ok`，透過 `position_scale` 傳入兩所管線。
  - 為何加碼非過濾：全策略×階梯回測，硬性過濾(A)雖EV↑MDD↓但砍頻率→總複利剩1/8(10%RISK)；
    C×1.5 保留頻率+資金傾斜高品質單 → 成長>不過濾基準(5%:72x→88x/10%:1401x→1927x)且MDD略低。
- **1H 空**：只用現役階梯壓力過濾(WF+0.141)；曾試加CVD頂背離但全期無增益(已撤)。
- ⚠️ 純數據獵手CVD背離當「觸發器」WF全失敗(訓練正/驗證負,銅板)；只當「加碼權重」有效。

**2026-06-02 新增 強平守門員（OKX下單管線）**：高槓桿保留，但若估算強平價會落在停損之前
(`停損距% ≥ (可用USDT+本倉保證金)/倉位價值 ×0.85`)則跳過此單。
修復：全倉+max槓桿+遠停損 → 強平搶在停損前 → 帳戶級強平連帶清掉手動倉的 bug。

**2026-06-02 新增 金字塔加碼（PYRAMID_ENABLED，預設False=未啟動）**：
**僅驗證過的多單(C3 15m/30m、1H W底；pyramid_eligible)** 達+1R時加一單位(=原始張數)、
停損上移到原進場價。排除15m雙底共振(n=20且加碼變差)、MACD。函數`_okx_pyramid_add`，
觸發在`check_trailing_stops_for_real`。安全設計：加碼走強平守門員(合併部位強平在停損前→不加)、
停損=原進場價使合併最大虧≈1單位(有界不爆倉)、每筆只加一次。
- 回測WF：金字塔每筆EV翻倍(+0.118→+0.233)、整體×階梯5%RISK 51x→1103x但MDD 30%→48%。
- ⚠️ live≠回測：OKX同方向合併成一個部位(非兩獨立單)，行為與回測有出入。
- 啟動：改`PYRAMID_ENABLED=True` + RISK建議5%(`!risk 5`)。新live下單邏輯，首次啟用需盯。

**已停用（回測負期望/過擬合）**：
- C3 1H 做多（−0.024）→ 1H 多單改由雙底W底觸發
- 30m 做空（全期 −0.023，只在 2022 賺）
- M頭單獨做空（四版全賠）→ 改用「雙頂+RSI共振」才有效
- 評分層/動態SL/熔斷（未回測，已移除呼叫；函數保留為 dead code）

**各策略定義**：
- **C3**：Vegas 通道回踩 + QQE rsiMa 穿50 + ADX（原始核心，`is_long`/`is_short`）
- **雙底/雙頂**：兩個相近 pivot + 量縮 + 頸線突破放量（`indicators.check_double_bottom/top`）
- **RSI 共振**（限 15m）：雙底/頂 + RSI(14) 穿50 同時成立
- **MACD 動能**：4H EMA200 斜率定向 + MACD(12,26,9) 交叉 + 快線DIF斜率加速(非收腳)（`indicators.calculate_macd`/`macd_difslope_ok`）

**出場**：固定 R，TP1=1.2R(50%)/TP2=2.5R(50%)，**保本觸發已延後至 1.5R**（15m多空/1H空，WF驗證 EV+68% MDD-9pt）

### 已測淘汰/部分有效（避免重測）
- ❌ SMC、VCP：WF 過擬合 / 樣本不足
- ❌ MJ(MACD+KDJ)：柱體同色過濾無效甚至有害
- ❌ ATR移動止盈/全trailing：加密短線震盪多，輸給固定TP（EV −0.07）
- ⚠️ CVD 當過濾器（舊版三層吸收）：效益極小（多單反害、空單微升），維持關閉
- ✅ CVD 吸收方向過濾（新發現，2026-06-02）：`cvd_15m[i] < cvd_15m[i-3]` 用於 **15m C3 多頭**，EV +0.133→+0.207，WF驗證穩定（訓練+0.218/驗證+0.187，MDD 40%→22%）。腳本：backtest_15m_sop.py。**注意：OI上升過濾對15m多有害，手冊完整SOP(OI+CVD)WF失敗**
- ⚠️ C3+MACD 共振(15m多)：WF 驗證集 EV +0.117 > 單C3 +0.086、賺賠比 1.32(穩定)，但**勝率提升是過擬合假象**(訓練59.5%→驗證48.1%)。結論：賺賠比改善真實，可用於「共振時加碼」而非當新進場。未上線。
- 🚫 Lorentzian AI：黑盒 kNN，無法在 Python 端忠實複刻/回測，不採用

### 已測總表（2026-06-02~03，WF 70/30，避免重測）
**✅ 有效/已上線：**
- 15m C3多 + CVD吸收(`cvd[-1]<cvd[-4]`) → ×1.5加碼。WF驗+0.187。
- 15m C3多 + ls<1.0(散戶淨空) → ×1.5加碼。WF驗+0.138。(taker/資金費/OI對多單無效)
- 15m DH做空：2B(96根=1天高戳破收回)+CVD頂背離+OI升6根+ls≥2.5+taker>1.0。WF驗+0.199/MDD20%。
- 15m 箱突破做空：破96根窄箱(<8%)底+帶量1.5x+CVD↓+OI升。WF驗+0.193(固定1.5/3R)。
  └ 麥門切線出場(TP1 1.5R+連下降擺動高pv3,收盤破線出)WF驗+0.107/**MDD17%**/勝54%(壓MDD用,待上線)
- 1H C3空 + 階梯壓力位(±0.5ATR)。WF驗+0.141~0.208。
- 強平守門員(估強平距<停損距→不下單)、秒進秒損修(止損無效/<0.6%不下單)。

**❌ 已測淘汰(WF敗/過擬合/無資料)：**
- 純DH CVD背離當「觸發器」(無C3,雙樞紐/寬鬆)：銅板,訓練正驗證負。只能當「加碼權重」。
- DH/C3空 + RSI/MACD粗/非收腳過濾：救不了15m空(訓練漂亮驗證崩)。
- 階梯Fibo當「進場觸發器」：過擬合(訓+0.3→驗-0.2)。**但當「匯流確認」C3多+0.073→+0.114有效**。
- 動態結構區間(Swing頂底分型/Bollinger壓縮/VolumeProfile POC)當2B定義：**三種全WF敗**,
  固定「1天高」完勝(+0.199 vs 動態-0.13~+0.002)。動態訊號多2-3倍但雜訊稀釋。選擇性>花俏。
- ADX趨勢過濾(C3多)：全期漂亮、驗證崩,過擬合。
- 「讓贏單跑」全trailing/regime trailing(多單)：驗證敗(震盪吐回)。
- 金字塔加碼(+1R加單)：WF過(+0.233)但MDD大增(10%RISK→74%);**預設關**,配低RISK才用。
- 1H空 + CVD頂背離：全期無增益(WF的+0.287是n40假象),已撤。
- OI上升當15m多過濾：有害(+0.133→+0.048)。日線OI僅對1H空有效。
- 2R/3R單一出場(15m)：勝率掉10-20%,雜訊掃損。但箱突破(順勢)用3R/讓跑才好。
- 爆倉量輔助：Binance已移除歷史、Coinalyze僅3週,無法回測。
- 機械15m逆勢空(C3空/純2B)跨7年(含2022熊)全負:框架要熊市趨勢=偏誤;加ls≥2.5+大級別才翻正。

**含手續費(往返0.1%,maker TP 0.02%)：** 組合淨EV+0.090、勝48%、賺賠1.27;
階梯10U起5%RISK年化~56-82%/MDD46%、2%年化~30%/MDD28%、10%年化~102%/MDD69%(太兇)。建議RISK 3-5%。

---

## ⚠️ 風險設定（重要）

`RISK_PCT` 預設 0.10（10%）對小本金太高——回測模擬顯示 10% 風險下實盤 MDD 可達 99%、存活率僅 56%。
建議降至 **0.02~0.05**：5% 存活率 99%、2% MDD 約 44%。用 Discord `!risk 5` 或直接改 `RISK_PCT`。

### 分段複利下注（壓 MDD 的核心機制）
**MDD 高的真相**：不是策略爛，是「純複利」（每筆風險=即時餘額×RISK_PCT）放大的——
帳戶長大後單筆變大，遇最糟連虧(回測 −19R)從高點回落 → 純複利 MDD 96%。
**現行機制（分段複利）**：單筆風險 = `LADDER_BASE_USDT × RISK_PCT × (1+level)`，
`level = (錢包餘額 − BASE) ÷ LADDER_STEP_USDT`，即**每多賺 `LADDER_STEP_USDT`(預設50U) 才把單筆風險加一級**。
- 回測對照（10U起、4年、RISK 5%）：每+50U → **37倍/MDD 50%**；純複利 7萬倍/MDD 96%；純固定 15倍/MDD 29%
- ⚠️ **報酬與 MDD 是鎖死的取捨**，沒有又快又穩的下注法。級距越小越接近複利(高報酬高MDD)、越大越接近固定(穩但慢)
- 級距用 `!setladder 50` 調（OKX+BingX 同步套用）
- 注意：回測那條「MDD 50%」用 RISK 5%；若 RISK_PCT 維持 10%，實際 MDD 會更高
- 風險基準用 walletBalance（不含浮動盈虧），平倉賺賠才改變，避免持倉中倉位忽大忽小

---

## 模組結構（省 token 用 MODULE_MAP.md）

- `main.py`：主體（設定/通知/下單/策略/主迴圈）
- `indicators.py`：純計算指標（QQE/ATR/ADX/MACD/雙底頂）
- `MODULE_MAP.md`：功能→行號索引，修改前先查表跳轉，不需讀全文

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
| **`幣` 或 `幣 空/多 [時框]`** | 順籌碼即時判斷（仿數據獵手）：直接打幣名（`ADA`/`ADA 空`/`ADA 空 15m`，無需前綴，`!幣`也可）。回市場結構象限(OI×CVD)+評分(±10)+方向轉折(🔄剛轉多/空)+適合多/空+建議SL/TP(ATR)。時框預設1H可5m/15m/30m/1H/4H。唯讀、用 bot 自己的資料、不碰外部 API。每張訊號卡也自動掛精簡版「順籌碼」。`judge_coin()` |
| `!status` | 查看 Bot 運行狀態 |
| `!pause` / `!resume` | 暫停 / 恢復交易 |
| `/trade [15m\|30m\|1h\|4h\|all] [on\|off]` | 開關時框自動下單 |
| `/margin isolated\|cross` | 切換保證金模式 |
| `/exchange okx\|bingx on\|off` | 開關交易所路由 |
| `/cvd on\|off` | 開關 CVD 背離過濾 |
| `!setslots [數字]` | 設定倉位格數 |
| `!risk [數字]` | 設定每倉風險%（如 `!risk 5`）|
| `!setladder [U]` | 分段複利級距：每多賺 N U 才把單筆風險加一級（如 `!setladder 50`）|
| `!setmmr [%]` | OKX 維持保證金率門檻：預估加新倉後 < 此% 就跳過（如 `!setmmr 350`）|

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

### 6. 倉位追蹤管理（active_real_trades）— 兩個已修的 critical bug
`active_real_trades` 是保本/移動止損的追蹤池，`check_trailing_stops_for_real()` 靠它管理已開倉位。

**Bug A：重啟/redeploy 後追蹤丟失（已修 commit 5010669）**
- 原本是純記憶體 dict，Railway 每次 redeploy 就清空 → 已開倉的保本/移動止損停擺。
- 解法：持久化到 `active_trades.json`（已加 .gitignore）。開倉後 + 每輪 `check_trailing_stops` 後存檔；啟動時 `load_active_trades()` 讀回。
- BingX 的 `headers`（含金鑰）不落地，讀回時用全域 `BINGX_API_KEY` 重建。
- ⚠️ 勿刪 `save_active_trades()` / `load_active_trades()` 呼叫，否則追蹤丟失重現。

**Bug B：OKX 倉位從未被追蹤（已修 commit 1e4dfb7）**
- 原本只有 BingX 開倉會 `active_real_trades[...]=`，OKX 完全沒有 → OKX 的保本/移動止損從未執行。
- 解法：`execute_okx_trade_pipeline` 末端、止損掛上(sl_algo_id 存在)後，把 OKX 倉位寫入追蹤池。
- 驗證方式：OKX 開單後 Discord 應出現「📋 已加入保本/移動止損追蹤池」，TP1 成交時應有「止損移至保本價」通知。

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

1. 多策略掃描（C3 / 雙底 / RSI共振 / MACD），OR 邏輯任一觸發（見「實盤策略地圖」）
2. ADX 過濾（C3 用，若 `ADX_ENABLED`）
3. CVD 背離（若 `CVD_ENABLED`，預設關閉；回測顯示當過濾器效益極小，建議維持關閉）
4. 冷卻：同幣同時框 `SIGNAL_COOLDOWN=1800s` + 同幣同向跨時框 `DIR_SIGNAL_COOLDOWN=3600s`
5. `AUTO_TRADE[tf]` True → 自動下單；False → Discord 通知等手動
6. 同時送 OKX 和 BingX（各自 `EXCHANGE_ENABLED`，全倉用該幣最大槓桿，停損上限 = 風險預算 × `RISK_TOLERANCE_MULT`(2.0)）
7. 熔斷：連虧 `MAX_CONSEC_LOSS` 次 → 暫停 `PAUSE_HOURS` 小時

## CVD 資料說明
- `cvd_*_perp_cvd` / `spot_cvd`：2022起齊全（spot 已修正，之前 perp==spot 重複的 bug 已解）
- `cvd_*_oi`：僅近數月（Binance OI API 限制），三層吸收的 OI 層資料不足
- 下載腳本：上層 `trading-backtest/download_perp_cvd.py`

---

## 使用說明

**每次問 Bot 相關問題時，請將此 SKILL.md 貼給 Claude，讓他能直接了解系統架構，不需重新解釋背景。**

若有新的問題或修改，請告知 Claude 更新此 Skill 文件。
