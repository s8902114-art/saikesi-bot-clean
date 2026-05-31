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
