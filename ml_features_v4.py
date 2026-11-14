"""TiMi ML v4 — OHLCV features, exclusively current/past CLOSED candles.
Developer: Suat DOĞU. Research only. No trading execution.
"""
from __future__ import annotations
import numpy as np
import pandas as pd
from ml_features import make_features, FEATURE_SCHEMA as BASE_SCHEMA, WARMUP_BARS

FEATURE_SCHEMA = 'timi-ohlcv-v4-research-1'


def make_v4_features(candles: pd.DataFrame):
    base, _ = make_features(candles)
    c = candles['close'].astype(float)
    h = candles['high'].astype(float)
    l = candles['low'].astype(float)
    o = candles['open'].astype(float)
    v = candles['volume'].astype(float)
    atr = base['atr_pct_14'] * c
    ret = c.pct_change()
    extra = pd.DataFrame(index=candles.index)
    extra['body_to_range'] = (c - o) / (h - l).replace(0, np.nan)
    extra['range_to_atr14'] = (h - l) / atr.replace(0, np.nan)
    extra['atr7_atr21_ratio'] = base['atr_pct_7'] / base['atr_pct_21'].replace(0, np.nan)
    extra['atr14_atr21_ratio'] = base['atr_pct_14'] / base['atr_pct_21'].replace(0, np.nan)
    for period in (10, 30, 60):
        extra[f'realized_vol_{period}'] = ret.rolling(period, min_periods=period).std(ddof=0)
        extra[f'close_position_{period}'] = (c - l.rolling(period).min()) / (
            h.rolling(period).max() - l.rolling(period).min()).replace(0, np.nan)
    for period in (20, 50):
        past_high = h.shift(1).rolling(period).max()
        past_low = l.shift(1).rolling(period).min()
        extra[f'breakout_above_{period}_atr'] = (c - past_high) / atr.replace(0, np.nan)
        extra[f'breakout_below_{period}_atr'] = (c - past_low) / atr.replace(0, np.nan)
    extra['volume_candle_direction'] = np.sign(c-o) * v / v.rolling(20).mean().replace(0, np.nan)
    extra['ema20_vs_ema50_atr'] = (base['ema_distance_50'] - base['ema_distance_20']) / base['atr_pct_14'].replace(0, np.nan)
    # Time features are known as of the candle close, not future information.
    ts = pd.to_datetime(candles['t'], unit='ms', utc=True)
    hour = ts.dt.hour + ts.dt.minute / 60
    extra['utc_hour_sin'] = np.sin(2*np.pi*hour/24)
    extra['utc_hour_cos'] = np.cos(2*np.pi*hour/24)
    result = pd.concat([base, extra], axis=1).replace([np.inf, -np.inf], np.nan)
    return result, list(result.columns)
