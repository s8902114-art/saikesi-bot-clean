#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
技術指標與形態偵測模組（純計算，無全域狀態依賴）
從 main.py 抽出，供 main.py import 使用。

包含：
- calculate_smooth_rsi / calculate_full_qqe_mod  : QQE MOD
- calculate_average_true_range                    : ATR
- calculate_directional_movement_index            : ADX
- check_double_bottom / check_double_top           : 雙底/雙頂形態（WF 驗證）
"""
from typing import Tuple
import numpy as np
import pandas as pd

# ══════════════════════════════════════════════════════════════════════════════
# QQE MOD / ATR / ADX
# ══════════════════════════════════════════════════════════════════════════════

def calculate_smooth_rsi(series_src: pd.Series, rolling_period: int) -> pd.Series:
    """ 計算非線性標準化相對強弱指標線 """
    price_delta = series_src.diff()
    up_trends = price_delta.clip(lower=0.0)
    down_trends = -price_delta.clip(upper=0.0)
    mean_up = up_trends.ewm(com=rolling_period - 1, adjust=False).mean()
    mean_down = down_trends.ewm(com=rolling_period - 1, adjust=False).mean()
    rs_value = mean_up / mean_down.replace(0.0, 1e-9)
    return 100.0 - (100.0 / (1.0 + rs_value))


def calculate_full_qqe_mod(data_df: pd.DataFrame, rsi_pd: int = 6, sf_pd: int = 5, factor_mult: float = 4.236) -> Tuple[pd.Series, pd.Series]:
    """ 完全體 QQE MOD 動態移動區間軌道演算演算法 """
    src_close = data_df["close"]
    rsi_series = calculate_smooth_rsi(src_close, rsi_pd)
    rsi_smoothed_ma = rsi_series.ewm(span=sf_pd, adjust=False).mean()
    absolute_rsi_delta = rsi_smoothed_ma.diff().abs()
    smoothed_atr_rsi = absolute_rsi_delta.ewm(span=2 * rsi_pd - 1, adjust=False).mean()
    dar_trailing_band = smoothed_atr_rsi.ewm(span=2 * rsi_pd - 1, adjust=False).mean() * factor_mult

    trailing_line_value = float('nan')
    trailing_buffer_list = []
    for idx in range(len(rsi_smoothed_ma)):
        current_ma_val = rsi_smoothed_ma.iloc[idx]
        dar_val = dar_trailing_band.iloc[idx]
        if idx == 0 or (current_ma_val != current_ma_val) or (dar_val != dar_val):
            # NaN guard: keep previous or initialize
            if trailing_line_value != trailing_line_value:
                trailing_line_value = current_ma_val if (current_ma_val == current_ma_val) else 50.0
        else:
            previous_trailing_value = trailing_line_value
            if previous_trailing_value != previous_trailing_value:
                previous_trailing_value = current_ma_val
            if current_ma_val < previous_trailing_value:
                # Bear state: trail ratchets down (can only decrease)
                trailing_line_value = min(previous_trailing_value, current_ma_val + dar_val)
            else:
                # Bull state: trail ratchets up (can only increase)
                trailing_line_value = max(previous_trailing_value, current_ma_val - dar_val)
        trailing_buffer_list.append(trailing_line_value)
    return rsi_smoothed_ma, pd.Series(trailing_buffer_list, index=data_df.index)


def calculate_average_true_range(data_df: pd.DataFrame, atr_period: int = 14) -> pd.Series:
    """ 計算真實波動幅度均值 (ATR) """
    high_prices = data_df["high"]
    low_prices = data_df["low"]
    previous_closes = data_df["close"].shift(1)
    tr1 = high_prices - low_prices
    tr2 = (high_prices - previous_closes).abs()
    tr3 = (low_prices - previous_closes).abs()
    true_range = pd.concat([tr1, tr2, tr3], axis=1).max(axis=1)
    return true_range.ewm(alpha=1.0 / atr_period, adjust=False).mean()


def calculate_directional_movement_index(data_df: pd.DataFrame, adx_period: int = 14) -> pd.Series:
    """ 動向指標 (DMI/ADX) 趨勢強度過濾器 """
    high_diff = data_df["high"].diff()
    low_diff = -data_df["low"].diff()
    plus_dm = np.where((high_diff > low_diff) & (high_diff > 0), high_diff, 0.0)
    minus_dm = np.where((low_diff > high_diff) & (low_diff > 0), low_diff, 0.0)

    atr_series = calculate_average_true_range(data_df, adx_period).replace(0.0, 1e-9)
    plus_di = 100.0 * pd.Series(plus_dm, index=data_df.index).ewm(alpha=1.0 / adx_period, adjust=False).mean() / atr_series
    minus_di = 100.0 * pd.Series(minus_dm, index=data_df.index).ewm(alpha=1.0 / adx_period, adjust=False).mean() / atr_series

    dx_value = 100.0 * (plus_di - minus_di).abs() / (plus_di + minus_di).replace(0.0, 1e-9)
    return dx_value.ewm(alpha=1.0 / adx_period, adjust=False).mean()


# ══════════════════════════════════════════════════════════════════════════════
# 雙底 / 雙頂動能衰減進場偵測 (Double Bottom / Top Pattern)
# ══════════════════════════════════════════════════════════════════════════════

# 各時框雙底/雙頂參數（Walk-Forward 驗證最佳）
_DOUBLE_PARAMS = {
    "15m": {"pivot_len": 2, "neck_expand": 1.5, "price_tol": 0.01},
    "30m": {"pivot_len": 2, "neck_expand": 1.5, "price_tol": 0.01},
    "1H":  {"pivot_len": 5, "neck_expand": 2.0, "price_tol": 0.01},
    "4H":  {"pivot_len": 5, "neck_expand": 2.0, "price_tol": 0.01},
}
_DOUBLE_SEARCH_WIN = 60   # 往回找雙點的最大窗口（根數）


def _find_pattern_pivots_low(lo: np.ndarray, i: int, pivot_len: int, window: int):
    """在 [i-window, i-pivot_len] 找所有擺動低點索引"""
    pivots = []
    start = max(pivot_len, i - window)
    end   = i - pivot_len
    for j in range(start, end + 1):
        left  = lo[j - pivot_len:j]
        right = lo[j + 1:j + pivot_len + 1]
        if len(left) < pivot_len or len(right) < pivot_len:
            continue
        if lo[j] <= left.min() and lo[j] <= right.min():
            pivots.append(j)
    return pivots


def _find_pattern_pivots_high(hi: np.ndarray, i: int, pivot_len: int, window: int):
    """在 [i-window, i-pivot_len] 找所有擺動高點索引"""
    pivots = []
    start = max(pivot_len, i - window)
    end   = i - pivot_len
    for j in range(start, end + 1):
        left  = hi[j - pivot_len:j]
        right = hi[j + 1:j + pivot_len + 1]
        if len(left) < pivot_len or len(right) < pivot_len:
            continue
        if hi[j] >= left.max() and hi[j] >= right.max():
            pivots.append(j)
    return pivots


def check_double_bottom(df: pd.DataFrame, tf_id: str) -> bool:
    """
    雙底偵測：
    1. e144 > e576（多頭趨勢）
    2. 找最後兩個擺動低點，價差 < price_tol
    3. 第二底成交量 < 第一底（動能衰減）
    4. 第二底實體 < 第一底實體 × 0.8
    5. 當根收盤突破頸線 + 放量 > 近20根均量 × neck_expand
    """
    if len(df) < _DOUBLE_SEARCH_WIN + 10:
        return False
    params   = _DOUBLE_PARAMS.get(tf_id, _DOUBLE_PARAMS["1H"])
    pvt_len  = params["pivot_len"]
    neck_x   = params["neck_expand"]
    price_tol = params["price_tol"]

    hi   = df["high"].values
    lo   = df["low"].values
    cl   = df["close"].values
    op   = df["open"].values
    vol  = df["vol"].values if "vol" in df.columns else np.ones(len(df))
    ema144 = df["close"].ewm(span=144, adjust=False).mean().values
    ema576 = df["close"].ewm(span=576, adjust=False).mean().values

    i = len(df) - 2   # 最後一根已收盤的K棒

    if ema144[i] <= ema576[i]:
        return False

    pivots = _find_pattern_pivots_low(lo, i, pvt_len, _DOUBLE_SEARCH_WIN)
    if len(pivots) < 2:
        return False

    p1, p2 = pivots[-2], pivots[-1]
    if p2 - p1 < pvt_len * 2:
        return False

    v1, v2 = lo[p1], lo[p2]
    if abs(v2 - v1) / max(v1, 1e-9) > price_tol:
        return False
    if vol[p2] >= vol[p1]:
        return False
    body1 = abs(cl[p1] - op[p1])
    body2 = abs(cl[p2] - op[p2])
    if body1 > 0 and body2 >= body1 * 0.8:
        return False

    neckline  = hi[p1:p2 + 1].max()
    if cl[i] <= neckline:
        return False

    vol_avg20 = vol[max(0, i - 20):i].mean()
    if vol_avg20 <= 0 or vol[i] < vol_avg20 * neck_x:
        return False

    return True


def check_double_top(df: pd.DataFrame, tf_id: str) -> bool:
    """
    雙頂偵測：
    1. 連續20根 e144 < e576（嚴格空頭趨勢）
    2. 找最後兩個擺動高點，價差 < price_tol
    3. 第二頂成交量 < 第一頂（動能衰減）
    4. 第二頂實體 < 第一頂實體 × 0.8
    5. 當根收盤跌破頸線 + 放量 > 近20根均量 × neck_expand
    """
    if len(df) < _DOUBLE_SEARCH_WIN + 10:
        return False
    params   = _DOUBLE_PARAMS.get(tf_id, _DOUBLE_PARAMS["1H"])
    pvt_len  = params["pivot_len"]
    neck_x   = params["neck_expand"]
    price_tol = params["price_tol"]

    hi   = df["high"].values
    lo   = df["low"].values
    cl   = df["close"].values
    op   = df["open"].values
    vol  = df["vol"].values if "vol" in df.columns else np.ones(len(df))
    ema144 = df["close"].ewm(span=144, adjust=False).mean().values
    ema576 = df["close"].ewm(span=576, adjust=False).mean().values

    i = len(df) - 2

    # 嚴格空頭：連續20根
    bear_win = min(20, i + 1)
    if not all(ema144[i - k] < ema576[i - k] for k in range(bear_win)):
        return False

    pivots = _find_pattern_pivots_high(hi, i, pvt_len, _DOUBLE_SEARCH_WIN)
    if len(pivots) < 2:
        return False

    p1, p2 = pivots[-2], pivots[-1]
    if p2 - p1 < pvt_len * 2:
        return False

    v1, v2 = hi[p1], hi[p2]
    if abs(v2 - v1) / max(v1, 1e-9) > price_tol:
        return False
    if vol[p2] >= vol[p1]:
        return False
    body1 = abs(cl[p1] - op[p1])
    body2 = abs(cl[p2] - op[p2])
    if body1 > 0 and body2 >= body1 * 0.8:
        return False

    neckline  = lo[p1:p2 + 1].min()
    if cl[i] >= neckline:
        return False

    vol_avg20 = vol[max(0, i - 20):i].mean()
    if vol_avg20 <= 0 or vol[i] < vol_avg20 * neck_x:
        return False

    return True
