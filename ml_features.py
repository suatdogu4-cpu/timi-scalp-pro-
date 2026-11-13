"""TiMi SCALP PRO — deterministic CLOSED-candle ML feature engineering.

This module MUST be shared by offline training and future live inference.
It uses only the current and preceding fully closed candles; no future/negative shifts.
Developer: Suat DOĞU.
"""
from __future__ import annotations

import numpy as np
import pandas as pd

FEATURE_SCHEMA = 'timi-ohlcv-v1'
WARMUP_BARS = 250


def make_features(candles: pd.DataFrame) -> tuple[pd.DataFrame, list[str]]:
    """Input columns t/open/high/low/close/volume, ascending by open time.

    Only feed *closed* candles. Rows are aligned one-to-one with inputs.
    """
    required = {'t', 'open', 'high', 'low', 'close', 'volume'}
    if not required.issubset(candles.columns):
        raise ValueError(f'Missing OHLCV columns: {sorted(required - set(candles.columns))}')
    if not candles['t'].is_monotonic_increasing or candles['t'].duplicated().any():
        raise ValueError('Candles must have unique, strictly increasing open times')
    c = candles['close'].astype('float64')
    o = candles['open'].astype('float64')
    h = candles['high'].astype('float64')
    l = candles['low'].astype('float64')
    v = candles['volume'].astype('float64')
    x = pd.DataFrame(index=candles.index)

    for length in (5, 10, 20, 50, 100, 200):
        ema = c.ewm(span=length, adjust=False, min_periods=length).mean()
        x[f'ema_distance_{length}'] = c / ema - 1.0
        ma_volume = v.rolling(length, min_periods=length).mean()
        x[f'volume_ratio_{length}'] = v / ma_volume.replace(0, np.nan) - 1.0

    delta = c.diff()
    gain = delta.clip(lower=0)
    loss = -delta.clip(upper=0)
    prev = c.shift(1)
    true_range = pd.concat([h - l, (h - prev).abs(), (l - prev).abs()], axis=1).max(axis=1)
    for length in (7, 14, 21):
        avg_gain = gain.ewm(alpha=1 / length, adjust=False, min_periods=length).mean()
        avg_loss = loss.ewm(alpha=1 / length, adjust=False, min_periods=length).mean()
        rs = avg_gain / avg_loss.replace(0, np.nan)
        rsi = 100 - 100 / (1 + rs)
        rsi = rsi.where(avg_loss > 0, np.where(avg_gain > 0, 100.0, 50.0))
        x[f'rsi_{length}'] = rsi
        atr = true_range.ewm(alpha=1 / length, adjust=False, min_periods=length).mean()
        x[f'atr_pct_{length}'] = atr / c

    for length in (1, 2, 3, 5, 10, 20):
        x[f'return_{length}'] = c.pct_change(length)

    x['range_pct'] = (h - l) / c
    x['body_pct'] = (c - o) / c
    x['upper_wick_pct'] = (h - pd.concat([o, c], axis=1).max(axis=1)) / c
    x['lower_wick_pct'] = (pd.concat([o, c], axis=1).min(axis=1) - l) / c

    ema12 = c.ewm(span=12, adjust=False, min_periods=12).mean()
    ema26 = c.ewm(span=26, adjust=False, min_periods=26).mean()
    macd = ema12 - ema26
    macd_signal = macd.ewm(span=9, adjust=False, min_periods=9).mean()
    x['macd_pct'] = macd / c
    x['macd_signal_pct'] = macd_signal / c
    x['macd_hist_pct'] = (macd - macd_signal) / c

    mid = c.rolling(20, min_periods=20).mean()
    sd = c.rolling(20, min_periods=20).std(ddof=0)
    x['bb_low_distance'] = (mid - 2 * sd) / c - 1
    x['bb_mid_distance'] = mid / c - 1
    x['bb_high_distance'] = (mid + 2 * sd) / c - 1
    x['bb_width'] = (4 * sd) / mid.replace(0, np.nan)
    x['bb_position'] = (c - (mid - 2 * sd)) / (4 * sd).replace(0, np.nan)

    x = x.replace([np.inf, -np.inf], np.nan)
    return x, list(x.columns)
