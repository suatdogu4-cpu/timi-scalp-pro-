"""TiMi SCALP PRO — ML v2.0 — walk-forward LightGBM research trainer.

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
    accuracy_score, balanced_accuracy_score, confusion_matrix, f1_score, log_loss, precision_recall_fscore_support,
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



def distribution(series: pd.Series) -> dict[str, int]:
    counts = series.value_counts().to_dict()
    return {CLASS_NAMES[k]: int(counts.get(k, 0)) for k in (0, 1, 2)}


def fold_boundaries(dev_end: int, horizon: int) -> list[tuple[slice, slice]]:
    """Three expanding-window folds; each forward label stops before validation.

    The untouched final holdout begins at dev_end, beyond all fold validations.
    """
    folds = []
    for fraction in (0.45, 0.60, 0.75):
        start = int(dev_end * fraction)
        end = min(dev_end, start + int(dev_end * 0.10))
        folds.append((slice(0, start - horizon), slice(start, end)))
    return folds


def model_config(weight_mode: str, target: pd.Series) -> dict:
    """Class weighting is calculated on training rows alone, never validation/test."""
    weights = None
    if weight_mode == 'weighted_capped':
        counts = target.value_counts().to_dict()
        neutral = max(int(counts.get(1, 0)), 1)
        weights = {c: float(min(6.0, max(1.0, math.sqrt(neutral / max(int(counts.get(c, 0)), 1)))))
                   for c in (0, 2)}
        weights[1] = 1.0
    elif weight_mode != 'standard':
        raise ValueError(f'Invalid weight mode: {weight_mode}')
    return dict(
        objective='multiclass', n_estimators=220, learning_rate=0.035,
        num_leaves=15, max_depth=5, min_child_samples=80,
        colsample_bytree=0.8, subsample=0.8, subsample_freq=1,
        reg_lambda=5.0, random_state=42, n_jobs=2, verbosity=-1,
        class_weight=weights,
    )


def evaluate(y: pd.Series, probabilities: np.ndarray, classes: np.ndarray) -> dict:
    """Class-specific diagnostic metrics. All three classes are expected in training."""
    classes = np.asarray(classes, dtype=int)
    preds = classes[np.argmax(probabilities, axis=1)]
    truth = np.asarray(y, dtype=int)
    pr, rc, f1, _ = precision_recall_fscore_support(
        truth, preds, labels=[0, 1, 2], zero_division=0)
    directional_predictions = int(np.isin(preds, [0, 2]).sum())
    directional_events = int(np.isin(truth, [0, 2]).sum())
    correct_directions = int(((preds == truth) & np.isin(preds, [0, 2])).sum())
    return {
        'rows': len(truth),
        'actual_classes': distribution(pd.Series(truth)),
        'predicted_classes': distribution(pd.Series(preds)),
        'accuracy': round(float(accuracy_score(truth, preds)), 6),
        'balanced_accuracy': round(float(balanced_accuracy_score(truth, preds)), 6),
        'macro_f1': round(float(f1_score(truth, preds, labels=[0, 1, 2],
                                         average='macro', zero_division=0)), 6),
        'class_precision': {CLASS_NAMES[i]: round(float(pr[i]), 6) for i in range(3)},
        'class_recall': {CLASS_NAMES[i]: round(float(rc[i]), 6) for i in range(3)},
        'class_f1': {CLASS_NAMES[i]: round(float(f1[i]), 6) for i in range(3)},
        'directional_prediction_count': directional_predictions,
        'directional_prediction_rate': round(directional_predictions / len(truth), 6),
        'correct_directional_predictions': correct_directions,
        'directional_precision': (round(correct_directions / directional_predictions, 6)
                                  if directional_predictions else None),
        'directional_event_recall': (round(correct_directions / directional_events, 6)
                                     if directional_events else None),
        'neutral_only_baseline_accuracy': round(float((truth == 1).mean()), 6),
        'log_loss': round(float(log_loss(truth, probabilities, labels=classes)), 6),
        'confusion_matrix_0_1_2': confusion_matrix(truth, preds, labels=[0, 1, 2]).tolist(),
    }


def fit_and_evaluate(x_train: pd.DataFrame, y_train: pd.Series,
                     x_val: pd.DataFrame, y_val: pd.Series,
                     mode: str) -> tuple[LGBMClassifier, dict]:
    model = LGBMClassifier(**model_config(mode, y_train))
    model.fit(x_train, y_train)
    result = evaluate(y_val, model.predict_proba(x_val), model.classes_)
    return model, result


def train_models(raw: pd.DataFrame, symbol: str, interval: str, horizons: list[int],
                 cost_bps: float, out: Path) -> dict:
    """Choose weight mode on three forward validations, then test once at end."""
    out.mkdir(parents=True, exist_ok=True)
    frame, candidates = make_features(raw)
    frame = frame.iloc[WARMUP_BARS:].reset_index(drop=True)
    raw = raw.iloc[WARMUP_BARS:].reset_index(drop=True)
    report = {
        'purpose': 'v2_walk_forward_offline_research_only_not_deployed',
        'symbol': symbol, 'interval': interval,
        'bars_after_warmup': len(raw), 'feature_schema': FEATURE_SCHEMA,
        'label_threshold_bps': cost_bps,
        'label_definition': 'close[i+h] / close[i] - 1; DOWN below -threshold, UP above threshold; otherwise NEUTRAL',
        'class_labels': CLASS_NAMES,
        'selection': '3 expanding walk-forward validation folds (train-only class weights); choose larger mean macro-F1; 20% final untouched holdout',
        'data_start_utc': stamp(int(raw['t'].iloc[0])),
        'data_end_utc': stamp(int(raw['t'].iloc[-1])),
        'created_utc': datetime.now(timezone.utc).isoformat(),
        'models': {},
        'caveats': [
            'Weights are a diagnostic experiment, not an assurance of predictive power.',
            'Final holdout is evaluated only after model selection; retesting changes on it would invalidate its untouched status.',
            'predict_proba outputs are NOT calibrated probabilities.',
            '12 bps is an illustrative movement threshold, NOT independently measured execution cost.',
            'One asset and a short historical window do not establish robustness across regimes.',
            'Only OHLCV features are available; historical synchronized CVD/order book records are absent.',
            'No trades, broker calls, or automatic production ML deployment.',
        ],
    }
    for horizon in horizons:
        labels = label_for_horizon(raw['close'], horizon, cost_bps)
        n = len(raw) - horizon
        entry = {'horizon_bars': horizon, 'horizon_minutes':
                 horizon * STEP[interval] // 60_000}
        if n < 3000:
            entry['status'] = 'insufficient_rows'
            report['models'][str(horizon)] = entry
            continue
        matrix = frame.iloc[:n].copy()
        target = labels.iloc[:n].astype('int64')
        t = raw['t'].iloc[:n]
        dev_end = int(n * .80)
        folds = fold_boundaries(dev_end, horizon)
        first_train_end = folds[0][0].stop
        chosen = [col for col in candidates if
                  matrix[col].iloc[:first_train_end].notna().mean() >= .90 and
                  matrix[col].iloc[:first_train_end].nunique(dropna=True) > 1]
        entry.update({'feature_count': len(chosen),
                      'raw_class_distribution': distribution(target),
                      'final_holdout_start_utc': stamp(int(t.iloc[dev_end])),
                      'final_holdout_rows': n - dev_end,
                      'fold_count': len(folds),
                      'folds': [], 'variants': {}})
        if len(chosen) < 8 or dev_end - horizon < 1000:
            entry['status'] = 'insufficient_features'
            report['models'][str(horizon)] = entry
            continue
        ready = True
        for idx, (tr, va) in enumerate(folds, 1):
            train_y, val_y = target.iloc[tr], target.iloc[va]
            if min(len(train_y), len(val_y)) < 200 or any(
                    distribution(train_y)[v] < 20 for v in CLASS_NAMES.values()) or any(
                    distribution(val_y)[v] < 1 for v in CLASS_NAMES.values()):
                ready = False
                entry['reason'] = f'Fold {idx} has too few rows or rare directional classes'
                break
            entry['folds'].append({
                'fold': idx,
                'train_rows': len(train_y), 'validation_rows': len(val_y),
                'train_end_open_utc': stamp(int(t.iloc[tr].iloc[-1])),
                'validation_start_open_utc': stamp(int(t.iloc[va].iloc[0])),
                'validation_end_open_utc': stamp(int(t.iloc[va].iloc[-1])),
                'train_classes': distribution(train_y),
                'validation_classes': distribution(val_y),
                'purged_rows_before_validation': horizon,
            })
        if not ready:
            entry['status'] = 'insufficient_class_diversity'
            report['models'][str(horizon)] = entry
            continue
        for mode in ('standard', 'weighted_capped'):
            fold_metrics = []
            for tr, va in folds:
                _, fm = fit_and_evaluate(matrix[chosen].iloc[tr], target.iloc[tr],
                                         matrix[chosen].iloc[va], target.iloc[va], mode)
                fold_metrics.append(fm)
            mean_f1 = float(np.mean([f['macro_f1'] for f in fold_metrics]))
            mean_bal = float(np.mean([f['balanced_accuracy'] for f in fold_metrics]))
            entry['variants'][mode] = {
                'fold_metrics': fold_metrics,
                'mean_macro_f1': round(mean_f1, 6),
                'mean_balanced_accuracy': round(mean_bal, 6),
            }
        best = max(('standard', 'weighted_capped'), key=lambda mode: (
            entry['variants'][mode]['mean_macro_f1'],
            entry['variants'][mode]['mean_balanced_accuracy'],
            mode == 'standard',
        ))
        final_fit_end = dev_end - horizon
        train_x, train_y = matrix[chosen].iloc[:final_fit_end], target.iloc[:final_fit_end]
        test_x, test_y = matrix[chosen].iloc[dev_end:], target.iloc[dev_end:]
        if min(distribution(train_y).values()) < 20 or min(distribution(test_y).values()) < 1:
            entry['status'] = 'insufficient_holdout_classes'
            report['models'][str(horizon)] = entry
            continue
        final_model, test_metrics = fit_and_evaluate(train_x, train_y, test_x, test_y, best)
        entry.update({
            'status': 'research_only_not_calibrated',
            'selected_variant': best, 'final_train_rows': len(train_x),
            'final_train_classes': distribution(train_y),
            'test': test_metrics,
            'test_beats_neutral_accuracy_baseline': (
                test_metrics['accuracy'] > test_metrics['neutral_only_baseline_accuracy']),
            'test_macro_f1_exceeds_one_third': (test_metrics['macro_f1'] > (1.0 / 3.0)),
        })
        model_name = f'{symbol}_{interval}_h{horizon}_model.txt'
        final_model.booster_.save_model(str(out / model_name))
        metadata = {
            'model_file': model_name, 'symbol': symbol, 'interval': interval,
            'horizon_bars': horizon, 'horizon_minutes': entry['horizon_minutes'],
            'feature_schema': FEATURE_SCHEMA,
            'feature_names_ordered': chosen,
            'selected_variant': best,
            'class_order': [int(v) for v in final_model.classes_],
            'class_labels': CLASS_NAMES,
            'threshold_bps': cost_bps,
            'training_end_open_ms': int(t.iloc[final_fit_end - 1]),
            'test_start_open_ms': int(t.iloc[dev_end]),
            'training_rows': len(train_x),
            'approval': 'NOT_APPROVED_FOR_LIVE_TRADING',
            'lightgbm_version': lgb.__version__,
        }
        (out / f'{symbol}_{interval}_h{horizon}_metadata.json').write_text(
            json.dumps(metadata, indent=2, ensure_ascii=False), encoding='utf-8')
        booster = lgb.Booster(model_file=str(out / model_name))
        sample = test_x.head(3)
        restored = booster.predict(sample)
        if not np.allclose(restored, final_model.predict_proba(sample), rtol=1e-6, atol=1e-7):
            raise RuntimeError('Saved LightGBM model failed round-trip verification')
        report['models'][str(horizon)] = entry
        print(f'Horizon {horizon} ({entry["horizon_minutes"]}m), variant={best}: '
              f'holdout macro_F1={test_metrics["macro_f1"]} '
              f'balanced_accuracy={test_metrics["balanced_accuracy"]} '
              f'directional_precision={test_metrics["directional_precision"]} '
              f'directional_coverage={test_metrics["directional_prediction_rate"]}',
              flush=True)
    (out / 'training_report.json').write_text(
        json.dumps(report, indent=2, ensure_ascii=False), encoding='utf-8')
    return report


def main() -> None:
    parser = argparse.ArgumentParser(description='TiMi ML v2 walk-forward OFFLINE research; no live trading')
    parser.add_argument('--symbol', default='BTCUSDT')
    parser.add_argument('--interval', choices=STEP, default='1m')
    parser.add_argument('--bars', type=int, default=60000)
    parser.add_argument('--horizons', type=int, nargs='+', default=[1, 5])
    parser.add_argument('--cost-bps', type=float, default=12.0,
                        help='Illustrative movement threshold in basis points, NOT measured fees')
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
    raw = fetch(args.symbol, args.interval, args.bars)
    report = train_models(raw, args.symbol, args.interval, args.horizons, args.cost_bps, output)
    trained = sum(item.get('status') == 'research_only_not_calibrated'
                  for item in report['models'].values())
    print(f'Finished: {trained}/{len(args.horizons)} models. '
          f'Report: {output / "training_report.json"}', flush=True)
    if trained == 0:
        raise SystemExit('No trainable model: inspect training_report.json')


if __name__ == '__main__':
    main()
