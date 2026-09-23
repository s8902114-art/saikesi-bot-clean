# 賽克斯 Bot 程式碼地圖（給 Claude / 維護者快速定位用）

> **用途**：修改前先查此表跳到對應行號/檔案，避免每次讀整個 main.py（省 token）。
> **維護規則**：每次新增/刪除函數或大段搬移後，更新此表的行號。行號會漂移，抓不到時用函數名 Grep。

---

## 檔案結構

```
saikesi-bot-clean/
├── main.py          ← 主體：設定、通知、下單、策略、主迴圈（見下方分區）
├── indicators.py    ← 純計算指標（QQE/ATR/ADX/雙底雙頂）★已抽出
├── push.sh          ← 推送到 GitHub（Railway 自動部署）
├── auto_push.py     ← 監控 main.py 變更自動 push
└── MODULE_MAP.md    ← 本檔
```

---

## main.py 分區索引（行號為近似，漂移時用「定位字串」Grep）

| 功能區 | 約略行號 | 定位字串（Grep 用） | 重點 |
|---|---|---|---|
| 套件 import / 編碼修正 | 11–53 | `REQUIRED_PACKAGES` | |
| **API 金鑰 / Discord / 交易所設定** | 61–92 | `OKX_API_KEY = os.environ` | 全走環境變數，無硬編碼 |
| **風控常數** | 100–106 | `RISK_PCT     =` | RISK_PCT、SIGNAL_COOLDOWN、RISK_TOLERANCE_MULT |
| 全域狀態開關 | 109–125 | `AUTO_TRADE: Dict` | ⚠️ 被 poll_dc_commands 用 global 改寫 |
| 幣種對照表 SYMBOLS | 139–213 | `SYMBOLS: Dict` | SYMBOLS / CONA_SPOT / CONA_PERP |
| 常數（QQE/ADX/乖離等） | 226–239 | `BEAR_MIN_BARS` | |
| **BEST_PARAMS 參數表** | 252–307 | `BEST_PARAMS: Dict` | 各時框各方向 tp/be/buf/slb/qqe ★最常查 |
| get_params() | 310 | `def get_params` | 讀 json 覆蓋 BEST_PARAMS |
| **Discord/TG 通知** | 366–470 | `def dc_log` | dc_log/dc_embed_send/create_interactive_signal |
| OKX 公開行情 | 482–528 | `def fetch_market_candles` | K線、資金費率 |
| Coinalyze CVD/OI | 533–600 | `def calculate_cumulative_volume_delta` | |
| **指標計算（已抽 indicators.py）** | — | `import indicators` | QQE/ATR/ADX 改從 indicators 引用 |
| OKX 簽章/Algo 單 | 684–759 | `def _place_okx_algo_sl` | |
| **OKX 下單主流程** | 762–1062 | `def execute_okx_trade_pipeline` | ★含 USDT單位/tgtCcy降級/張數風控 |
| BingX 簽章/請求 | 1065–1080 | `def _bingx_request` | |
| **BingX 下單主流程** | 1082–1300 | `def execute_bingx_trade_pipeline` | ★含最大槓桿/風險率/worst_loss 防呆 |
| **保本追蹤（兩所）** | 1302–1490 | `def check_trailing_stops_for_real` | TP1成交→移保本、波浪追蹤 |
| tick size | 1491 | `def _get_tick_size` | |
| **雙底雙頂（已抽 indicators.py）** | — | `import indicators` | check_double_bottom/top |
| **結構止損 SL** | 1670–1720 | `def _find_pivot_low` | ★SL 計算，留 main.py（風險高） |
| CVD 三層吸收 | 1722–1782 | `def _check_cvd_absorption` | |
| 訊號評分（未啟用） | 1791–1862 | `def filter_signals` | dead code，目前未呼叫 |
| 動態 SL/TP（未啟用） | 1875–1933 | `def dynamic_sl_tp` | dead code |
| 熔斷器（未啟用） | 1942–2002 | `class CircuitBreaker` | dead code，record() 從未被呼叫 |
| **SykesTradingBot 主類** | 2005–2383 | `class SykesTradingBot` | |
| ├─ 冷卻/熔斷 | 2014–2035 | `def is_cooldown` | |
| ├─ Paper 模擬出場 | 2037–2089 | `def update_paper_trailing_and_exits` | |
| ├─ 4H 波段旗標 | 2090–2111 | `def _get_4h_swing_flag` | |
| └─ **★進場核心 scan** | 2112–2383 | `def scan_and_process_market` | C3條件/雙底頂/SL-TP/同向去重/下單派發 |
| Discord 互動 webhook | 2393–2449 | `def discord_interactions_webhook` | |
| 收盤對齊排程 | 2457–2481 | `def synchronise_and_wait_next_candle` | |
| **Discord 指令輪詢** | 2483–2658 | `def poll_dc_commands` | ⚠️ global 改全域開關（/trade /cvd /margin…） |
| 動態幣種更新 | 2660–2752 | `def build_dynamic_symbols` | CoinGecko top100 × OKX |
| **主迴圈** | 2754–2808 | `def main_polling_loop` | |
| 進入點 | 2816 | `if __name__` | 三執行緒：主迴圈/DC輪詢/Flask |

> ⚠️ **上表行號嚴重過期**（表列到 2816，main.py 實際已 8300+ 行）。抓不到時一律用「定位字串」Grep。

### 2026-09-10 新增：4J減速跌破做空（FOURJD）
| 區塊 | 定位字串（Grep 用） | 說明 |
|---|---|---|
| 常數/開關/診斷 | `FOURJD_SHORT_ENABLED` | 全部門檻常數＋`_FOURJD_DIAG`漏斗＋`_FOURJD_RISK`熔斷狀態 |
| ADX | `def _fourjd_adx` | ADX(14)。實測給300根1H與完整歷史誤差0.0000 |
| ★判定核心 | `def _fourjd_signal` | 逐根重放狀態機，只回報最後一根。**每輪整個重建、不跨輪保存**（redeploy不歸零） |
| 對外入口 | `def _check_fourjd_short` | 自己抓1H+2H(各300根)，不吃外面的df |
| ★熔斷 | `def _fourjd_record_result` | 連續吃滿停損8筆自動停；呼叫點在 `[Trailing] ... 倉位已關閉` 那段 |
| 出場模式 | `fourjd_2r` | 整倉TP 2R＋浮盈0.8R保本；OKX/BingX下單管線與保本邏輯共6處分支 |

### ★突破回踩做空 BOR（2026-09-13，4h 判定與進場）

| 元件 | 定位字串（Grep） | 重點 |
|---|---|---|
| 常數區 | `BOR_SHORT_ENABLED` | LOOK96/SKIP8/WAIT24/TOL0.4%/GIVE1%/TP **1.0R**/冷卻4根4h/每日上限5 |
| ★停損距上限 | `BOR_MAX_SL_PCT` | **8%**。live 的 `MAX_SL=12%` 會放行回測沒測過的 8~12%，所以策略自己設 |
| ★判定核心 | `def _bo_retest_signal` | 逐根重放狀態機 IDLE→BROKE→RETEST→進場；手抄自 `_bt_bo_retest.signals`（short/engulf/retest_low） |
| 對外入口 | `def _check_bor_short` | 自己抓 4H 300 根，不吃外面的 df；只在最新**已收盤**根成立時進場 |
| ★貼支撐閘(09-14) | `BOR_SUP_GATE` / `def _support_below_R` | 下方最近支撐區(k3樞紐、±1%合併、≥2觸、回看600根)離進場 **<0.25R 不空**；深度用 `_s4h_deep_candles`（與 S4H 共用快取，沒抓到退回300根）；**被擋也佔冷卻**（對齊回測）；log `[BOR-Short] X 擋:下方 …R 就有支撐區`、儀表 `貼支撐` 計數；對拍 `_chk_bor_sup_port.py` 724/724 |
| ★上影線閘(09-15) | `BOR_UPWICK_GATE` / `def _gate_upper_wick` | 扳機吞噬K上影線 ≥ 全幅 **20% 不空**；在貼支撐閘之後、被擋也佔冷卻；log `[BOR-Short] X 擋:吞噬K上影線佔…`、儀表 `上影擋`；對拍 1642/1642；證據(2026兩池/CI/live 6停損擋5)寫在常數旁 |
| 熔斷 | `def _bor_record_result` | 連續 20 筆吃滿停損自動停（回測最長 14）；呼叫點在 `[BOR] ... 出場判定` |
| 掃描掛載 | `BOR-Short儀表` | `tf_id == "4H"`，比照 S4H |
| ★專屬旁路 | `_bor_only` | 4H 的 `AUTO_TRADE` 是 False，只在「BOR 是唯一觸發來源」時放行自動下單 |
| 出場模式 | `bor_1r` | ★09-14 起在 `_HANDS_OFF_ES`：交易所掛 TP1=TP2 同價(1R)+SL 後 bot 不保本不移SL（原本走預設分支會 1R 移保本+pivot 移SL）；S4H 改 `s4h_fixed`，★09-15 起**移出** `_HANDS_OFF_ES`、改走 box_trend/fourjd_2r 那段只做一次保本（`S4H_BE_R`=1.5R，用戶「保住本金為主」）；BOR 維持不保本（用戶「停利1R的就不用保本」） |
| 回測腳本 | `_bt_bo_retest.py` | 12期四層；對拍 `_chk_bo_port.py`（80檔/200訊號/**0 不一致**） |

### ★進場品質閘（2026-09-15，S4H / 4JD）
| 功能 | 位置 | 說明 |
|---|---|---|
| 共用函式 | `def _gate_count_As` / `_gate_rise_leg` / `_gate_range_pos_short` / `_btc_24h_change` | 在 `_S4H_DIAG` 下方；對拍 `_chk_night_gates_port.py`(S4H 627/4JD 533/BTC 533 筆 0 不一致)、實跑 `_chk_night_gates_live.py` |
| S4H 急漲閘 | `S4H_SPIKE_GATE` | 決策時點前48根已收盤1H的上漲段:拉回≥3%次數≤1 且漲幅≥12% → 不空(用戶「急漲不空第一個頭」)；另抓 1H 300根並截到4H收盤時點；儀表 `急漲擋` |
| 4JD 位置閘 | `FOURJD_POS_GATE` | 最近30根已收盤4H區間位置<15% → 不空；儀表 `位置擋` |
| 4JD BTC閘 | `FOURJD_BTC_GATE` | BTC 永續近96根15m漲>+1% → 不空；抓不到放行；儀表 `BTC漲擋` |
| 共同 | — | 被擋也佔冷卻/每日上限(對齊回測事後過濾)；選法與五段/前推/打架結果見 main.py 常數區註解 |

### ★空單出場改版:吞噬空/4JD 改「寬停損 + TP1R」（2026-09-16 深夜，用戶要求「不降低單量提高勝率」）
| 功能 | 位置 | 說明 |
|---|---|---|
| 吞噬空 | `ENGULF_SL_MULT`=2.2 / `ENGULF_TP_R`=1.0 | 停損距離 ×2.2、TP 1R 全平、**不保本**;`exit_strategy="engulf_1r"` ∈ `_HANDS_OFF_ES`(原 swing_full 讓跑) |
| 4JD | `FOURJD_SL_MULT`=1.3 / `FOURJD_TP_R`=1.0 | 停損距離 ×1.3、TP 1R 全平、**不保本**;`exit_strategy="fourjd_1r"` ∈ `_HANDS_OFF_ES`(原 fourjd_2r=TP2R+0.8R保本) |
| ★★4JD 熔斷判準換掉 | `exit_strategy == "fourjd_1r"` 分支 | **不保本+TP1=TP2 後,原本的 `tp1_hit=False ⇒ 吃滿停損` 會把贏單算成連虧**(同 0913 BOR 的坑)→ 改用**幾何判準**:現價 ≥ 進場價 ⇒ 停損側;抓價失敗不計數 |
| 為什麼(先驗用戶那句「停損率高就是進場不對」) | `_chk_stopout_why.py` | 1650 筆吃滿停損的單:**停損前先賺過>0.5R 佔 51~62%**、從沒賺過只有 17~23%;吞噬空 **掃損後48h仍走到1R 佔 44%** → 主因是停損太緊被影線掃(4JD 例外:只有15%,它才接近「進場不對」) |
| 掃描與凍結 | `_bt_exit_sweep.py` / `_an_exit_sweep.py` / `_bt_exit_capped.py` | 停損×(1.0~2.2)×停利(1~3R)×保本(無/0.5/0.8/1/1.5R)=124組;**n 完全不變**;只用訓練段選、用勝率選;**已把 live MAX_SL=12% 封頂模擬進去** |
| 吞噬空 驗收 | — | 現行讓跑 訓+1.096/勝62%→驗+0.699/47%→新幣+0.532/49%→**2026 −0.119/勝36%** ｜ 新版 +0.536/**78%**→+0.239/**63%**→+0.149/**59%**→**+0.063/勝54%/總R+10.6**;撞上限8% |
| 4JD 驗收 | — | 現行 訓+0.386/勝35%→+0.333/33%→+0.280/30%→**2026 −0.211/勝18%** ｜ 新版 +0.362/**69%**→+0.230/**63%**→**+0.323**/**66%**→**2026 −0.108/勝46%**;逐期10/10勝率全升;撞上限7% |
| ⚠️ 代價 | — | 吞噬空**放棄長尾**(舊期別總R砍半,曾有 +13R 單筆)=拿總R換勝率;4JD 2026 仍負(−0.108, P(EV>0)=25%) |
| ⚠️ 已知小問題 | `_infer_adopted_exit` | 三支(bor/engulf/fourjd)現在都是「兩張TP同價 R≈1」→ redeploy 後接管會一律認成 `bor_1r`,行為相同但**熔斷會記到 BOR 頭上**。門檻20筆不易誤觸,未修 |
| BOR/S4H | — | **都不改**:BOR 放寬停損在驗證段+新幣層都變差;S4H 是唯一明確在賺的,拿總R換勝率不划算 |

### ★空單 大盤波動閘 + 4JD 進場品質閘（2026-09-16 深夜）
| 功能 | 位置 | 說明 |
|---|---|---|
| 吞噬空 大盤波動閘 | `ENGULF_BTCVOL_GATE` / `def _btc_atr24_pct` | BTC 近96根15m (high−low) 平均 ÷ 現價 > **0.30%** 就不空(恐慌殺盤易被反彈打掉);抓不到放行;儀表 `大盤波動擋` |
| 4JD 進場品質閘 | `FOURJD_Q_GATE` | 進場收盤在近**96根1H**區間的位置 ≥30% 且 進場1H實體 ≥50%;儀表 `位階低擋`/`實體不足擋` |
| 為什麼 | `_an_shorts_gated.py` / `_an_short_excess.py` | 2026(到09-15) 吞噬空 −0.119、4JD −0.216,且**比配對隨機還差**(超額 −0.241/−0.205,P(超額≥0)=3.5%/7.6%);S4H 超額 +0.403 是真 edge |
| 真因 | `_an_short_fix2.py` | **2026 與舊期別的進場K品質分布幾乎一模一樣** → 加進場閘救不了,要看環境 |
| 驗收 | `_chk_btcvol.py` / `_an_short_env2.py` | 吞噬空 訓練+1.096→+1.794(勝62→71%)/驗證+0.699→+1.172/新幣+0.532→+0.730/**2026 −0.119→+0.050**,逐期9/9正 ｜ 4JD 訓練+0.383→+0.663/驗證+0.302→+0.433/新幣+0.211→+0.310/**2026 −0.216→+0.035**,逐期5/5正 |
| 一般性 | `_chk_env_general.py` | 波動閘套 S4H:2026 +0.372→**+0.81**(同向) ｜ BOR −0.02→−0.12(**變差,不套**) ｜ 4JD 仍負(不套);同時淘汰「BTC7天跌幅」那半(全體空單單獨看反向=曲線擬合) |
| 實跑 | `_chk_btcvol_live.py` | `_btc_atr24_pct()` 與自算差 0;live 值落在回測分布第65百分位=尺度一致 |
| ⚠️ 誠實標註 | — | 兩支 2026 的 CI 都跨 0(P(EV>0) 56~58%)=**從明確虧損變成打平,不是證明會賺**;4JD 留存只有26% |
| ⚠️ 既有閘 | `FOURJD_POS_GATE`(4H/N30/≥15%) | **2026 實測白擋**:擋掉 −0.193 vs 留下 −0.204,兩邊一樣爛。留著但它救不了策略 |

### ★V成型 群聚(加碼) + BTC反彈 + 選幣層（2026-09-16，深夜改版）
| 功能 | 位置 | 說明 |
|---|---|---|
| 常數/說明 | `VLONG_CROWD_GATE` / `VLONG_CROWD_MODE` | **群聚已從硬擋改成加碼**(`mode="boost"`);改回硬擋設 `"gate"`。證據寫在常數旁 |
| 群聚計數 | `def _vlong_crowd_count` / `_VLONG_CROWD` | 過完其他閘的 V 先記名;前4h「別的幣」出V數 ≥ 3×掃描池幣數/163 **且** BTC反彈≥3% → 下注×1.5;儀表 `群聚加碼` |
| BTC反彈 | `def _btc_rebound_24h` | BTC 15m 收盤/近96根最低 −1 ≥ **1%**(原3%)才放行;抓不到放行;儀表 `BTC未反彈` |
| **選幣層** | `def _vlong_anomaly_ok` | 進場前12h內要有**量異常(近1h量/前72h同長度中位 ≥3x)或 OI異常(\|近1h OI變化%\| > 自身近96h p95)**;兩來源都算不出→放行;儀表 `無異常` |
| OI 來源 | `def _okx_oi_hist_15m` / `_VLONG_OI_CACHE` | rubik `open-interest-history` 15m **帶 end 翻4頁=100h**(一頁只有100筆);快取10分鐘;實跑 400筆/間隔全15m無缺口/2.7s一幣 |
| 對拍/實跑 | `_chk_vlong_crowd_port.py` / `_chk_vlong_anom_live.py` / `_chk_anom_rate.py` | 前者 BTC反彈600/600、群聚3269/3269;後兩者真實 API 實跑(隨機時點通過率 8/18) |
| 為什麼改 | `_cmp_final_gates.py` | 舊12期 無新閘 +0.717/勝50%/總R+320.7 ｜ 群聚硬擋版 n291(65%) +0.964/勝57%/**總R+280.5** ｜ 現版 n375(84%) +0.856/**勝54%**/總R+320.8。2026 live幣池:硬擋版只留17%(0.18筆/天)、現版48%(0.51筆/天) |
| 選幣層證據 | `_bt_absorb_anom_live.py` | 舊12期 有異常 n412(92%) +0.814/勝52%/總R+335.5 vs **完全沒異常 n35 −0.423/勝17%/吃滿停損83%**;逐期10/12改善、無一期變差;2026中性 |

**熔斷判準（2026-09-13，兩條路都走不通後才定的）**：
- ❌ 4JD 的 `tp1_hit=False ⇒ 吃滿停損`：它成立是因為 4JD 有 0.8R 保本、賺的單必定先經過。
  BOR 沒有保本且 TP1=TP2 都在 1R，倉位一次全平後剩餘量歸零、移保本那段 `if new_algo_id:`
  不會成立 → **賺錢出場也會是 `tp1_hit=False`**，照抄會把贏單算成連虧。
- ❌ 查 `orders-algo-pending` 看停損單還在不在：倉位關閉時系統本來就會撤停損殘單
  （`_cancel_okx_algo_order` 共 14 處呼叫，2440/2465/2477 就在移除追蹤那一帶），
  「不在 pending」無法區分是被觸發還是被自己撤掉。
- ✅ **幾何判準**：BOR 只做空，TP 在進場價下方 1R、SL 在上方 1R，對稱夾住進場價
  → 倉位消失時 `現價 ≥ 進場價 ⇒ 停損側`、`< 進場價 ⇒ 獲利側`。
  誤判邊界：關倉後到抓價之間（數秒）價格剛好穿越進場價；抓價失敗**一律不計數**
  （寧可漏算也不要誤觸熔斷）。每次判定都印 `[BOR] ... 出場判定 現價 vs 進場` 供事後對帳。

**驗證腳本**（上層 `trading-backtest/`）：`_chk_4jd_port.py`（移植對拍264/264）、
`_chk_4jd_exec.py`（exec實跑＋熔斷）、`_chk_4jd_win2.py`（2h視窗深度）、
`_chk_scope.py`（★作用域檢查器，補 `_chk_names.py` 抓不到的局部變數遮蔽）


---

## 常見修改 → 看哪裡

| 你想改… | 去這裡 |
|---|---|
| 某時框的 TP/SL/保本參數 | `BEST_PARAMS`（252–307） |
| 進場條件（C3/雙底/共振/MACD） | `scan_and_process_market`；定位字串 `雙底(W底)第二套訊號`、`MACD 多週期動能濾波`。現況：15m C3多空＋15m雙底/雙頂RSI共振＋15m MACD多；1H 雙底多＋C3空＋1H MACD空（C3 1H多停用、30m空停用、M頭單獨做空停用）。MACD用4H EMA200定向+快線斜率過濾 |
| 停損怎麼算 | `_find_pivot_low/high`（1670+）+ scan 內 SL 段（~2300） |
| OKX 下單/張數/槓桿 | `execute_okx_trade_pipeline`（762+） |
| BingX 下單/風控 | `execute_bingx_trade_pipeline`（1082+） |
| 保本/移動止損 | `check_trailing_stops_for_real`（1302+） |
| Discord 訊號卡片 | `create_interactive_signal`（414+） |
| Discord 指令行為 | `poll_dc_commands`（2483+） |
| 風控常數（風險%/容忍倍數） | 100–106 |

---

## ⚠️ 重構地雷（為何沒有全拆成 8 檔）

`AUTO_TRADE / _PAUSED / _LIVE_MODE / CVD_ENABLED / MARGIN_MODE / RISK_PCT / pending_orders / active_real_trades`
是 **module-level 可變全域**，被 `poll_dc_commands` 用 `global` 改寫。若搬到別的檔案，`global X` 會改不到原本那份 → Discord 指令默默失效、實盤狀態錯亂。
要拆這些需改成 **state 物件**（重構，非搬移），風險高，未做。目前只抽出零 global 依賴的純函數（indicators.py）。


### ★倉位追蹤持久化與接管出場（2026-09-14）
| 功能 | 位置 | 說明 |
|---|---|---|
| 存檔目錄 | `_PERSIST_DIR` | Railway volume `/data`（沒掛就退回程式目錄）；啟動 log `[Persist] 存檔目錄 …` |
| 熔斷計數落地 | `save_risk_state` / `load_risk_state` | BOR/4JD `consec_sl`/`halted`，redeploy 不歸零 |
| 不碰型出場 | `_HANDS_OFF_ES` | `bor_1r`/`adopt_hold`：OKX 與 BingX 追蹤迴圈都直接 continue（S4H 09-15 移出，改 1.5R 保本） |
| 接管出場推斷 | `def _infer_adopted_exit` | 看交易所 reduceOnly TP 限價單：同價兩張 R≈1→BOR、≈2.5→S4H、CME幣≈2→cme_gap；單張全倉 R≈2 空→4JD；半倉→swing_tp；無TP→swing_full；停損已在獲利側或對不上→adopt_hold |
| 吞噬空出場 | 山寨覆寫處 `and not is_engulf_short` | 不再被改成 swing_tp，回到驗過的 swing_full |

### 私人儀表板 dashboard.py（2026-09-24，唯讀，不碰交易邏輯）

| 元件 | 位置 / 定位字串 | 重點 |
|---|---|---|
| 模組 | `dashboard.py` | 全部 stdlib，無新依賴；flask 只在 `register()` 裡 import |
| 掛載 | main.py `dashboard.register(app, globals())`（`app = Flask(__name__)` 下方） | 傳 globals 進去→讀到的永遠是當下的 `SYMBOLS`/`_oi_history`/`active_real_trades`/各 `*_DIAG` |
| 被動快照 | main.py `dashboard.put(symbol_item, tf_id,` （在 scan 的「5. 空頭趨勢」之後） | 只記上面**已經算完**的值：價格/ATR%/ADX/通道位置/bear-bull。零額外 API、零額外指標計算 |
| 訊號記錄 | main.py `dashboard.sig(symbol, tf,`（`create_interactive_signal` 內） | 策略名用訊號卡同一個 `source_tag`，避免顯示層跟策略對不上 |
| 路由 | `/d/<DASH_TOKEN>`（頁面）、`/d/<DASH_TOKEN>/api`（JSON） | 沒設 `DASH_TOKEN` 或長度<16 → **一律 404**（不回 401） |

★四條不准違反的規矩（違反就會重演舊坑）：
1. 顯示層只讀 bot 算好的值，**不准自己再算一次**（CLAUDE.md 第12條，BPR 標錯策略的教訓）。
   OI 榜的 `(latest-oldest)/oldest` 與有效性判準是逐行抄 `_fetch_okx_oi_movers`，改一邊要改兩邊。
2. 開網頁**不打任何交易所 API**（天花板是 rate limit）。
3. `put()`/`sig()` 永不拋例外，呼叫點另外再包一層 try —— 儀表板壞掉不可以影響交易。
4. `active_real_trades` 內含 `headers`（API 簽章）→ 輸出走 `_TRADE_FIELDS` **白名單**，絕不 dump 原始 dict。

驗證腳本：`_chk_dash.py`（27 項，含權杖 fail-closed／OI 公式對齊／未實現 R／**憑證不外洩**／漏斗 Δ）。

### 2026-09-24 第三輪：照官方網頁重做（直接登入 datahunterx 抓的原文）

★**官方硬編碼條件（不是我猜的，是網頁上的字）**：
> 「象限圖固定顯示 **OI ≥ 1%、|價格| ≤ 5%** 的標的；資金注入候選固定使用 **1H OI ≥ 4%、|價格| ≤ 3%**。」
> 「先找出 **1H** 資金注入候選；觀察 **15 分鐘**後，以 OI 保留、相對 BTC 強弱與 CVD 判斷方向。
>   **15m／30m 僅觀察變化，不另產生卡片或通知**。」
> 「1H 持倉量變化，依變化金額排序，僅顯示 OKX 上架幣種」

★**價格是上限不是下限** —— 要找「OI 大動、價格還沒動」＝主力安靜建倉。
  我第一版做成「兩個都要超過門檻」**方向相反**，正好把官方要的那批濾掉。
★時間級別只有 **15m / 30m / 1H**（4H/12H 是我自己加的，已移除）。
★容差 `tol = min(取樣間隔/2+60, 窗長*0.25)` —— 寫死 1800 時，
  才累積 37 分鐘也會通過 1H 窗檢查（線上實測抓到），畫面就把 37 分鐘標成 1H。

**他們的資料來源（`/api/oi-cache` 真實欄位）**：`okx_chg_*`(OKX) + `bn_chg`(幣安) +
`cvd_ratio`(來源 `binance_taker_ratio`) + `open_interest_usd` + `market_cap`。
→ **他們也沒做全所聚合**，就是 OKX+幣安兩所、CVD 單一幣安來源。
→ 我們只有 OKX（幣安 fapi 在 Railway 被地理封鎖 HTTP 451），UI 右上標「OKX」徽章說明。
