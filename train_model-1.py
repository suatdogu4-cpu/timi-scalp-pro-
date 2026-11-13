"""TiMi SCALP PRO — OFFLINE 3-class LightGBM research trainer.

No exchange keys, real orders, paper trades, or automatic live model deployment.
Run from GitHub Actions or a computer. Developer: Suat DOĞU.
"""
from __future__ import annotations

import argparse
import json
import math
import sys
import time
from datetime import datetime, timezone
from pathlib import Path

import httpx
import lightgbm as lgb
import numpy as np
import pandas as pd
from lightgbm import LGBMClassifier
from sklearn.metrics import (
    accuracy_score, balanced_accuracy_score, confusion_matrix, f1_score, log_loss,
)

from ml_features import FEATURE_SCHEMA, WARMUP_BARS, make_features

API = 'https://data-api.binance.vision/api/v3/klines'
STEP = {'1m': 60_000, '3m': 180_000, '5m': 300_000, '15m': 900_000}
SYMBOLS = {'BTCUSDT', 'ETHUSDT', 'SOLUSDT', 'BNBUSDT', 'XRPUSDT',
           'DOGEUSDT', 'ADAUSDT', 'AVAXUSDT'}
CLASS_NAMES = {0: 'DOWN', 1: 'NEUTRAL', 2: 'UP'}


def stamp(ms: int) -> str:
    return datetime.fromtimestamp(ms / 1000, tz=timezone.utc).isoformat()


def fetch(symbol: str, interval: str, bars: int, *, now_ms: int | None = None,
          pause_s: float = 0.15) -> pd.DataFrame:
    """Download exactly `bars` completed candles; fail if any gap or malformed OHLCV."""
    step = STEP[interval]
    now_ms = now_ms if now_ms is not None else int(time.time() * 1000)
    # Exclude current open candle and allow a 3s publication margin.
    last_closed_open = ((now_ms - 3000) // step) * step - step
    end = last_closed_open + step - 1
    rows: list[list] = []
    with httpx.Client(timeout=25) as client:
        while len(rows) < bars:
            limit = min(1000, bars - len(rows))
            response = None
            for attempt in range(4):
                try:
                    response = client.get(API, params={
                        'symbol': symbol, 'interval': interval, 'limit': limit,
                        'endTime': end,
                    })
                    response.raise_for_status()
                    break
                except httpx.HTTPStatusError as exc:
                    code = exc.response.status_code
                    if code in (418, 400, 401, 403, 404) or (code != 429 and code < 500) or attempt == 3:
                        raise
                    delay = min(15.0, 1.0 * 2 ** attempt)
                    print(f'HTTP {code}, retrying after {delay}s', flush=True)
                    time.sleep(delay)
                except (httpx.TimeoutException, httpx.TransportError):
                    if attempt == 3:
                        raise
                    time.sleep(min(8.0, 1.0 * 2 ** attempt))
            batch = response.json()
            if not isinstance(batch, list) or not batch:
                raise RuntimeError(f'Binance returned no candles after {len(rows)} of {bars}')
            rows = batch + rows
            end = int(batch[0][0]) - 1
            if len(rows) < bars:
                time.sleep(pause_s)
    if len(rows) != bars:
        raise RuntimeError(f'Expected {bars} candles but fetched {len(rows)}')
    frame = pd.DataFrame(rows, columns=[
        't', 'open', 'high', 'low', 'close', 'volume', 'close_t',
        'quote_volume', 'n_trades', 'taker_base', 'taker_quote', 'ignore',
    ])
    for col in ('open', 'high', 'low', 'close', 'volume'):
        frame[col] = pd.to_numeric(frame[col], errors='coerce')
    frame['t'] = pd.to_numeric(frame['t'], errors='coerce')
    frame['close_t'] = pd.to_numeric(frame['close_t'], errors='coerce')
    if frame[['t', 'close_t', 'open', 'high', 'low', 'close', 'volume']].isna().any().any():
        raise RuntimeError('NaNs or malformed values in raw Binance OHLCV')
    frame['t'] = frame['t'].astype('int64')
    frame['close_t'] = frame['close_t'].astype('int64')
    frame = frame.sort_values('t').reset_index(drop=True)
    if frame['t'].duplicated().any() or not frame['t'].diff().dropna().eq(step).all():
        raise RuntimeError('Duplicate or missing candles; training aborted')
    if (frame['t'] % step != 0).any():
        raise RuntimeError('Candles not aligned to UTC time buckets')
    if (frame['t'] + step > now_ms - 3000).any() or (frame['close_t'] >= now_ms - 3000).any():
        raise RuntimeError('Unclosed candle detected; training aborted')
    if (frame[['open', 'high', 'low', 'close']] <= 0).any().any() or (frame['volume'] < 0).any():
        raise RuntimeError('Non-positive OHLC or negative volume')
    if ((frame['high'] < frame[['open', 'close', 'low']].max(axis=1)) |
            (frame['low'] > frame[['open', 'close', 'high']].min(axis=1))).any():
        raise RuntimeError('Inconsistent OHLC ranges')
    return frame[['t', 'open', 'high', 'low', 'close', 'volume']]


def label_for_horizon(close: pd.Series, horizon: int, cost_bps: float) -> pd.Series:
    """0=DOWN, 1=NEUTRAL, 2=UP. Last horizon rows have missing labels."""
    future_return = close.shift(-horizon) / close - 1
    threshold = cost_bps / 10_000
    label = pd.Series(np.select([future_return < -threshold,
                                 future_return > threshold], [0, 2], default=1),
                      index=close.index, dtype='float64')
    label.loc[future_return.isna()] = np.nan
    return label


def purged_splits(n: int, horizon: int) -> tuple[slice, slice, slice]:
    """Remove samples whose forward label spans a subsequent data split."""
    first, second = int(n * 0.60), int(n * 0.80)
    return (slice(0, first - horizon),
            slice(first, second - horizon),
            slice(second, n))


def distribution(series: pd.Series) -> dict[str, int]:
    counts = series.value_counts().to_dict()
    return {CLASS_NAMES[k]: int(counts.get(k, 0)) for k in (0, 1, 2)}


def metrics(model: LGBMClassifier, x: pd.DataFrame, y: pd.Series) -> dict:
    probs = model.predict_proba(x)
    labels = model.classes_[np.argmax(probs, axis=1)]
    return {
        'rows': len(y),
        'class_distribution': distribution(y),
        'accuracy': round(float(accuracy_score(y, labels)), 6),
        'balanced_accuracy': round(float(balanced_accuracy_score(y, labels)), 6),
        'macro_f1': round(float(f1_score(y, labels, labels=[0, 1, 2], average='macro', zero_division=0)), 6),
        'log_loss': round(float(log_loss(y, probs, labels=[0, 1, 2])), 6),
        'majority_baseline_accuracy': round(float(y.value_counts(normalize=True).max()), 6),
        'confusion_matrix_0_1_2': confusion_matrix(y, labels, labels=[0, 1, 2]).tolist(),
    }


def train_models(raw: pd.DataFrame, symbol: str, interval: str, horizons: list[int],
                 cost_bps: float, out: Path) -> dict:
    out.mkdir(parents=True, exist_ok=True)
    frame, candidates = make_features(raw)
    frame = frame.iloc[WARMUP_BARS:].reset_index(drop=True)
    raw = raw.iloc[WARMUP_BARS:].reset_index(drop=True)
    report = {
        'purpose': 'offline_research_only_not_deployed',
        'symbol': symbol,
        'interval': interval,
        'bars_after_warmup': len(raw),
        'feature_schema': FEATURE_SCHEMA,
        'label_threshold_bps': cost_bps,
        'label_definition': 'close_t+h / close_t; DOWN below -threshold, UP above +threshold, else NEUTRAL',
        'class_labels': CLASS_NAMES,
        'split': '60/20/20 chronological; purge h samples at both earlier boundaries',
        'data_start_utc': stamp(int(raw['t'].iloc[0])),
        'data_end_utc': stamp(int(raw['t'].iloc[-1])),
        'created_utc': datetime.now(timezone.utc).isoformat(),
        'models': {},
        'caveats': [
            'Scores from predict_proba are not calibrated forecast probabilities.',
            'The threshold is illustrative, not measured transaction cost.',
            'Single chronological holdout; not walk-forward or execution backtest.',
            'No order-book or trade-flow features without historical synchronized archives.',
            'Live TiMi main.py does not load these models automatically.',
        ],
    }
    for horizon in horizons:
        labels = label_for_horizon(raw['close'], horizon, cost_bps)
        n = len(raw) - horizon
        if n < 1200:
            report['models'][str(horizon)] = {'status': 'insufficient_rows'}
            continue
        matrix = frame.iloc[:n].copy()
        target = labels.iloc[:n].astype('int64')
        t = raw['t'].iloc[:n]
        tr_slice, va_slice, te_slice = purged_splits(n, horizon)
        tr, va, te = matrix.iloc[tr_slice], matrix.iloc[va_slice], matrix.iloc[te_slice]
        ytr, yva, yte = target.iloc[tr_slice], target.iloc[va_slice], target.iloc[te_slice]
        selected = [col for col in candidates if
                    tr[col].notna().mean() >= 0.90 and tr[col].nunique(dropna=True) > 1]
        entry = {
            'horizon_bars': horizon,
            'horizon_minutes': horizon * STEP[interval] // 60_000,
            'rows_train': len(tr), 'rows_validation': len(va), 'rows_test': len(te),
            'train_end_utc': stamp(int(t.iloc[tr_slice].iloc[-1])) if len(tr) else None,
            'validation_start_utc': stamp(int(t.iloc[va_slice].iloc[0])) if len(va) else None,
            'test_start_utc': stamp(int(t.iloc[te_slice].iloc[0])) if len(te) else None,
            'classes_train': distribution(ytr),
            'feature_count': len(selected),
        }
        if min(len(tr), len(va), len(te)) < 200 or len(selected) < 8:
            entry['status'] = 'insufficient_rows_or_features'
            report['models'][str(horizon)] = entry
            continue
        if min(distribution(ytr).values()) < 20:
            entry['status'] = 'insufficient_class_diversity'
            report['models'][str(horizon)] = entry
            continue
        clf = LGBMClassifier(
            objective='multiclass', n_estimators=220, learning_rate=0.035,
            num_leaves=15, max_depth=5, min_child_samples=80,
            colsample_bytree=0.8, subsample=0.8, subsample_freq=1,
            reg_lambda=5.0, random_state=42, n_jobs=2, verbosity=-1,
        )
        clf.fit(tr[selected], ytr)
        entry.update({'status': 'research_only_not_calibrated',
                      'validation': metrics(clf, va[selected], yva),
                      'test': metrics(clf, te[selected], yte)})
        model_name = f'{symbol}_{interval}_h{horizon}_model.txt'
        clf.booster_.save_model(str(out / model_name))
        metadata = {
            'model_file': model_name,
            'symbol': symbol,
            'interval': interval,
            'horizon_bars': horizon,
            'horizon_minutes': entry['horizon_minutes'],
            'feature_schema': FEATURE_SCHEMA,
            'feature_names_ordered': selected,
            'class_order': [int(v) for v in clf.classes_],
            'class_labels': CLASS_NAMES,
            'threshold_bps': cost_bps,
            'training_end_open_ms': int(t.iloc[tr_slice].iloc[-1]),
            'training_rows': len(tr),
            'approval': 'NOT_APPROVED_FOR_LIVE_TRADING',
            'lightgbm_version': lgb.__version__,
        }
        (out / f'{symbol}_{interval}_h{horizon}_metadata.json').write_text(
            json.dumps(metadata, indent=2, ensure_ascii=False), encoding='utf-8')
        # Verify saved native text model can be loaded and reproduces predictions.
        booster = lgb.Booster(model_file=str(out / model_name))
        sample = te[selected].head(3)
        restored = booster.predict(sample)
        if not np.allclose(restored, clf.predict_proba(sample), rtol=1e-6, atol=1e-7):
            raise RuntimeError('Saved LightGBM model failed round-trip verification')
        report['models'][str(horizon)] = entry
        print(f'Horizon {horizon} bars ({entry["horizon_minutes"]}min): '
              f'test_balanced_accuracy={entry["test"]["balanced_accuracy"]}; '
              f'majority_baseline={entry["test"]["majority_baseline_accuracy"]}',
              flush=True)
    (out / 'training_report.json').write_text(
        json.dumps(report, indent=2, ensure_ascii=False), encoding='utf-8')
    return report


def main() -> None:
    parser = argparse.ArgumentParser(description='TiMi offline ML trainer; no trading endpoints')
    parser.add_argument('--symbol', default='BTCUSDT')
    parser.add_argument('--interval', choices=STEP, default='1m')
    parser.add_argument('--bars', type=int, default=30000)
    parser.add_argument('--horizons', type=int, nargs='+', default=[1, 5])
    parser.add_argument('--cost-bps', type=float, default=12.0,
                        help='Illustrative neutral-zone threshold in basis points, NOT measured fees')
    parser.add_argument('--out', default='models')
    args = parser.parse_args()
    args.symbol = args.symbol.upper()
    if args.symbol not in SYMBOLS:
        parser.error('Unsupported symbol')
    if not 3000 <= args.bars <= 100000:
        parser.error('bars must be 3000..100000')
    if (not args.horizons or len(set(args.horizons)) != len(args.horizons)
            or any(h < 1 or h > 60 for h in args.horizons)):
        parser.error('horizons must be unique integers 1..60')
    if not (math.isfinite(args.cost_bps) and 0 <= args.cost_bps <= 100):
        parser.error('cost-bps must be finite and between 0 and 100')
    output = Path(args.out)
    try:
        raw = fetch(args.symbol, args.interval, args.bars)
        report = train_models(raw, args.symbol, args.interval,
                              args.horizons, args.cost_bps, output)
    except Exception as exc:
        print(f'ERROR: {type(exc).__name__}: {exc}', file=sys.stderr)
        raise
    trained = sum(item.get('status') == 'research_only_not_calibrated'
                  for item in report['models'].values())
    print(f'Finished: {trained}/{len(args.horizons)} models. '
          f'Report: {output / "training_report.json"}', flush=True)
    if trained == 0:
        raise SystemExit('No trainable model: inspect training_report.json')


if __name__ == '__main__':
    main()
