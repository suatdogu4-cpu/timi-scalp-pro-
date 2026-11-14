"""TiMi LightGBM v4.1: closed-candle features and fixed regime definition.
Developer: Suat DOĞU. Research only; no trade execution.

All features and market regimes at row i depend ONLY on rows <=i.
"""
from __future__ import annotations

import numpy as np
import pandas as pd

from ml_features_v4 import make_v4_features

FEATURE_SCHEMA = 'timi-ohlcv-v41-research-1'
REGIME_NAMES = {0: 'YATAY', 1: 'TREND', 2: 'YUKSEK_OYNAKLIK'}


def make_v41_features(candles: pd.DataFrame) -> tuple[pd.DataFrame, list[str], np.ndarray]:
    base, _ = make_v4_features(candles)
    c = candles['close'].astype(float)
    h = candles['high'].astype(float)
    l = candles['low'].astype(float)
    v = candles['volume'].astype(float)
    atr = base['atr_pct_14'] * c
    delta = c.diff()
    # Normalized trend efficiency, strictly backward-looking.
    dist = (c - c.shift(20)).abs()
    traveled = delta.abs().rolling(20, min_periods=20).sum()
    efficiency = (dist / traveled.replace(0, np.nan)).clip(0, 1)
    ema20 = c / (1.0 + base['ema_distance_20'])
    ema50 = c / (1.0 + base['ema_distance_50'])
    strength = (ema20 - ema50) / atr.replace(0, np.nan)
    prior_atr_median = base['atr_pct_14'].rolling(200, min_periods=200).median().shift(1)
    vol_ratio = base['atr_pct_14'] / prior_atr_median.replace(0, np.nan)
    extra = pd.DataFrame(index=base.index)
    extra['trend_efficiency_20'] = efficiency
    extra['trend_strength_ema20_50_atr'] = strength
    extra['atr14_vs_prior_200_median'] = vol_ratio
    extra['relative_volume_20'] = v / v.rolling(20, min_periods=20).mean().replace(0, np.nan)
    extra['ret3_vs_atr14'] = base['return_3'] / base['atr_pct_14'].replace(0, np.nan)
    extra['ret10_vs_atr14'] = base['return_10'] / base['atr_pct_14'].replace(0, np.nan)
    extra['close_vs_prev20_hi_atr'] = (c - h.shift(1).rolling(20, min_periods=20).max()) / atr.replace(0, np.nan)
    extra['close_vs_prev20_lo_atr'] = (c - l.shift(1).rolling(20, min_periods=20).min()) / atr.replace(0, np.nan)
    # Fixed categories (NOT tuned on the post-cutoff test): high-vol first, then trend, else range.
    high_vol = (vol_ratio >= 1.35).fillna(False)
    trend = ((strength.abs() >= 0.6) & (efficiency >= 0.28)).fillna(False)
    regimes = np.where(high_vol.to_numpy(), 2, np.where(trend.to_numpy(), 1, 0)).astype(np.int8)
    extra['market_regime_code'] = regimes.astype(float)
    result = pd.concat([base, extra], axis=1).replace([np.inf, -np.inf], np.nan)
    return result, list(result.columns), regimes
