"""TiMi SCALP PRO ML v3 — two-stage, selective-signal OFFLINE research.

Only closed Binance Spot candles. No live endpoints, trading, orders or deployment.
Developer: Suat DOĞU.

Important: Historical holdouts have already been examined in v1/v2; v3 results
on overlapping dates are exploratory, NOT an independent performance proof.
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
from sklearn.metrics import balanced_accuracy_score, confusion_matrix, f1_score

from ml_features import FEATURE_SCHEMA, WARMUP_BARS, make_features

API = 'https://data-api.binance.vision/api/v3/klines'
STEP = {'1m': 60_000, '3m': 180_000, '5m': 300_000, '15m': 900_000}
SYMBOLS = {'BTCUSDT', 'ETHUSDT', 'SOLUSDT', 'BNBUSDT', 'XRPUSDT',
           'DOGEUSDT', 'ADAUSDT', 'AVAXUSDT'}
CLASS_NAMES = {0: 'DOWN', 1: 'NEUTRAL', 2: 'UP'}
# The final test period from earlier experiments is already public and reused.
PRIOR_EVALUATION_END_UTC = '2026-11-13T16:04:44+00:00'


def utc(ms: int) -> str:
    return datetime.fromtimestamp(int(ms) / 1000, timezone.utc).isoformat()


def fetch(symbol: str, interval: str, bars: int, pause_s: float = 0.16) -> pd.DataFrame:
    """Download `bars` completed candles. Abort on missing, malformed, or duplicate data."""
    step = STEP[interval]
    now = int(time.time() * 1000)
    last_open = ((now - 3000) // step) * step - step
    end = last_open + step - 1
    rows: list[list] = []
    with httpx.Client(timeout=25) as client:
        while len(rows) < bars:
            limit = min(1000, bars - len(rows))
            for attempt in range(4):
                try:
                    r = client.get(API, params={'symbol': symbol, 'interval': interval,
                                                'limit': limit, 'endTime': end})
                    r.raise_for_status()
                    break
                except httpx.HTTPStatusError as exc:
                    code = exc.response.status_code
                    if attempt == 3 or code in (400, 401, 403, 404, 418) or (code < 500 and code != 429):
                        raise
                    time.sleep(min(15, 2 ** attempt))
                except (httpx.TimeoutException, httpx.TransportError):
                    if attempt == 3:
                        raise
                    time.sleep(min(8, 2 ** attempt))
            batch = r.json()
            if not isinstance(batch, list) or not batch:
                raise RuntimeError(f'Binance returned no candles after {len(rows)} of {bars}')
            rows = batch + rows
            end = int(batch[0][0]) - 1
            if len(rows) < bars:
                time.sleep(pause_s)
    if len(rows) != bars:
        raise RuntimeError(f'Expected {bars} candles, received {len(rows)}')
    df = pd.DataFrame(rows, columns=['t', 'open', 'high', 'low', 'close', 'volume',
                                     'close_t', 'quote_volume', 'n_trades',
                                     'taker_base', 'taker_quote', 'ignore'])
    fields = ['t', 'close_t', 'open', 'high', 'low', 'close', 'volume']
    for col in fields:
        df[col] = pd.to_numeric(df[col], errors='coerce')
    if df[fields].isna().any().any():
        raise RuntimeError('Incomplete Binance candle data')
    df = df.sort_values('t').reset_index(drop=True)
    df['t'] = df['t'].astype('int64')
    if (df['t'].duplicated().any() or not df['t'].diff().dropna().eq(step).all()
            or (df['t'] % step != 0).any()):
        raise RuntimeError('Missing/duplicated/misaligned candles')
    if (df['t'].to_numpy() + step > now - 3000).any() or (df['close_t'] >= now - 3000).any():
        raise RuntimeError('Unclosed candles detected')
    if (df[['open', 'high', 'low', 'close']] <= 0).any().any() or (df['volume'] < 0).any():
        raise RuntimeError('Invalid OHLCV')
    if ((df['high'] < df[['open', 'close', 'low']].max(axis=1)) |
            (df['low'] > df[['open', 'close', 'high']].min(axis=1))).any():
        raise RuntimeError('Inconsistent OHLC extremes')
    return df[['t', 'open', 'high', 'low', 'close', 'volume']].copy()


def threshold_series(features: pd.DataFrame, mode: str, fixed_bps: float) -> np.ndarray:
    """Thresholds are computed ONLY from already closed current/past candles."""
    if mode == 'fixed':
        return np.full(len(features), fixed_bps, dtype=float)
    if mode != 'atr_dynamic':
        raise ValueError(mode)
    # ATR is a normalized 14-bar, closed-candle historical feature.
    # This is NOT a measured spread/fee/slippage cost or executable stop.
    atr = features['atr_pct_14'].to_numpy(dtype=float) * 10_000
    return np.clip(0.75 * atr, 4.0, 24.0)


def make_labels(close: pd.Series, horizon: int, thresholds_bps: np.ndarray) -> tuple[np.ndarray, np.ndarray]:
    if len(close) != len(thresholds_bps):
        raise ValueError('Threshold/close length mismatch')
    future_ret = (close.shift(-horizon) / close - 1).to_numpy(dtype=float)
    labels = np.select((future_ret < -thresholds_bps / 10_000,
                        future_ret > thresholds_bps / 10_000), (0, 2), default=1).astype(float)
    labels[~np.isfinite(future_ret)] = np.nan
    return labels, future_ret


def folds_for(dev_end: int, horizon: int) -> list[tuple[slice, slice]]:
    """Non-overlapping expanding chronological folds with purged label overlap."""
    return [(slice(0, max(0, int(dev_end * lo) - horizon)),
             slice(int(dev_end * lo), min(dev_end, int(dev_end * hi))))
            for lo, hi in ((0.45, 0.55), (0.60, 0.70), (0.75, 0.80))]


def counts(y: np.ndarray) -> dict[str, int]:
    y = np.asarray(y)
    return {CLASS_NAMES[k]: int(np.count_nonzero(y == k)) for k in (0, 1, 2)}


def fit_pair(x: pd.DataFrame, y: np.ndarray, weight_mode: str) -> tuple[LGBMClassifier, LGBMClassifier]:
    """Event: any threshold crossing. Direction: among actual historical events."""
    y = np.asarray(y, dtype=np.int64)
    event = (y != 1).astype(np.int64)
    event_idx = np.flatnonzero(event)
    if min(np.bincount(event, minlength=2)) < 30 or min(
            np.bincount((y[event_idx] == 2).astype(int), minlength=2)) < 20:
        raise ValueError('Not enough historical events for two-stage training')
    params = dict(n_estimators=140, learning_rate=0.045, num_leaves=11,
                  max_depth=4, min_child_samples=65, colsample_bytree=0.85,
                  subsample=0.85, subsample_freq=1, reg_lambda=8.0,
                  n_jobs=2, random_state=42, verbosity=-1)
    sw = None
    if weight_mode == 'capped_event_weight':
        positives, negatives = int(event.sum()), int((1 - event).sum())
        factor = min(5.0, max(1.0, math.sqrt(negatives / positives)))
        sw = np.where(event == 1, factor, 1.0)
    elif weight_mode != 'standard':
        raise ValueError(weight_mode)
    event_model = LGBMClassifier(objective='binary', **params)
    event_model.fit(x, event, sample_weight=sw)
    dir_model = LGBMClassifier(objective='binary', **params)
    dir_model.fit(x.iloc[event_idx], (y[event_idx] == 2).astype(np.int64))
    return event_model, dir_model


def predict_pair(models: tuple[LGBMClassifier, LGBMClassifier], x: pd.DataFrame) -> tuple[np.ndarray, np.ndarray]:
    ev, dr = models
    if list(ev.classes_) != [0, 1] or list(dr.classes_) != [0, 1]:
        raise ValueError('Unexpected classifier class order')
    return ev.predict_proba(x)[:, 1], dr.predict_proba(x)[:, 1]


def signals(event_score: np.ndarray, up_given_event_score: np.ndarray,
            event_cut: float, direction_cut: float) -> np.ndarray:
    """Score gates do NOT represent calibrated probability thresholds."""
    event_score = np.asarray(event_score)
    up = np.asarray(up_given_event_score)
    decisions = np.ones(len(event_score), dtype=int)
    gate = event_score >= event_cut
    decisions[gate & (up >= direction_cut)] = 2
    decisions[gate & (up <= 1.0 - direction_cut)] = 0
    return decisions


def evaluate(truth: np.ndarray, pred: np.ndarray, future_ret: np.ndarray | None = None) -> dict:
    truth = np.asarray(truth, dtype=np.int64)
    pred = np.asarray(pred, dtype=np.int64)
    if len(truth) == 0 or len(truth) != len(pred):
        raise ValueError('Invalid evaluation vectors')
    directional = pred != 1
    true_events = truth != 1
    correct = (pred == truth) & directional
    n_signal = int(directional.sum())
    n_true = int(true_events.sum())
    result = {
        'rows': len(truth), 'actual_classes': counts(truth), 'predicted_classes': counts(pred),
        'accuracy': round(float(np.mean(pred == truth)), 6),
        'neutral_only_baseline_accuracy': round(float(np.mean(truth == 1)), 6),
        'balanced_accuracy': round(float(balanced_accuracy_score(truth, pred)), 6),
        'macro_f1': round(float(f1_score(truth, pred, labels=[0, 1, 2], average='macro', zero_division=0)), 6),
        'directional_signals': n_signal,
        'directional_coverage': round(n_signal / len(truth), 6),
        'correct_directional_signals': int(correct.sum()),
        'directional_precision': round(float(correct.sum() / n_signal), 6) if n_signal else None,
        'directional_event_recall': round(float(correct.sum() / n_true), 6) if n_true else None,
        'false_directional_alarms': int((directional & ~correct).sum()),
        'precision_by_class': {
            CLASS_NAMES[k]: (round(float(np.sum((pred == k) & (truth == k)) / np.sum(pred == k)), 6)
                             if np.sum(pred == k) else None) for k in (0, 2)},
        'confusion_matrix_0_1_2': confusion_matrix(truth, pred, labels=[0, 1, 2]).tolist(),
    }
    if future_ret is not None:
        future_ret = np.asarray(future_ret)
        signed = np.where(pred[directional] == 2, 1.0, -1.0) * future_ret[directional] * 10_000
        result['mean_signed_future_change_bps_on_signals'] = (round(float(signed.mean()), 6)
                                                              if len(signed) else None)
        result['signed_return_note'] = 'Research price change only; NOT net trading P&L or execution backtest'
    return result


def select_policy(y: np.ndarray, scores: tuple[np.ndarray, np.ndarray], min_signals: int = 60) -> dict:
    """Select scores/thresholds only on early walk-forward OOF validation rows."""
    event, upward = scores
    candidates = []
    for ec in (0.20, 0.30, 0.40, 0.50, 0.60, 0.70):
        for dc in (0.55, 0.60, 0.65, 0.70, 0.75):
            pred = signals(event, upward, ec, dc)
            met = evaluate(y, pred)
            if met['directional_signals'] >= min_signals:
                candidates.append((met['directional_precision'], met['correct_directional_signals'],
                                   -met['false_directional_alarms'], ec, dc, met))
    if not candidates:
        return {'found_min_support_policy': False, 'reason': f'No policy produced {min_signals} validation signals',
                'event_score_cut': 0.50, 'direction_score_cut': 0.65, 'selection_metrics': None}
    best = max(candidates, key=lambda v: v[:3])
    return {'found_min_support_policy': True,
            'event_score_cut': best[3], 'direction_score_cut': best[4],
            'selection_metrics': best[5],
            'policy_count_tested': 30,
            'selection_warning': 'Multiple thresholds explored: validation score is optimistically selected'}


def train_models(raw: pd.DataFrame, symbol: str, interval: str, horizons: list[int],
                 fixed_bps: float, out: Path) -> dict:
    out.mkdir(parents=True, exist_ok=True)
    features, names = make_features(raw)
    features = features.iloc[WARMUP_BARS:].reset_index(drop=True)
    raw = raw.iloc[WARMUP_BARS:].reset_index(drop=True)
    if features['atr_pct_14'].isna().any():
        raise ValueError('ATR unavailable after warmup')
    report = {
        'purpose': 'TiMi_ML_v3_research_not_deployed',
        'symbol': symbol, 'interval': interval, 'bars_after_warmup': len(raw),
        'feature_schema': FEATURE_SCHEMA, 'created_utc': datetime.now(timezone.utc).isoformat(),
        'data_start_utc': utc(int(raw.t.iloc[0])), 'data_end_utc': utc(int(raw.t.iloc[-1])),
        'prior_experiments_accessed_data_through_utc': PRIOR_EVALUATION_END_UTC,
        'holdout_independence': 'HISTORICAL_OVERLAP_WITH_PRIOR_V1_V2; EXPLORATORY_NOT_PROSPECTIVE',
        'validation_plan': ('3 expanding, purged walk-forward folds. Folds 1+2 select '
                            'weight variant and signal-score gates; fold 3 confirms; '
                            'last 20% historical holdout is reported separately.'),
        'label_modes': {'fixed': f'fixed {fixed_bps} bps',
                        'atr_dynamic': 'clip(0.75 * ATR14 bps, 4 bps, 24 bps)'},
        'models': {},
        'caveats': [
            'This is NOT a newly untouched test period; v1/v2 already examined overlapping dates.',
            'Policy thresholds optimized on folds 1+2; false discovery / multiple comparisons remain possible.',
            'LightGBM prediction scores are uncalibrated; do not interpret as true probabilities.',
            'The directional threshold is a label definition, NOT a verified transaction cost.',
            'Only closed OHLCV; no synchronized historical CVD or order book features.',
            'Overlapping time horizons create correlated observations, not independent trades.',
            'No fees, slippage, execution simulator, capital management or trading endpoints.',
            'Models must NOT be promoted to live on the basis of this report.',
        ],
    }
    variants = ('standard', 'capped_event_weight')
    for h in horizons:
        n = len(raw) - h
        if n < 5000:
            report['models'][str(h)] = {'status': 'insufficient_rows'}
            continue
        dev_end = int(n * 0.8)
        folds = folds_for(dev_end, h)
        first_train = folds[0][0].stop
        selected = [c for c in names if features[c].iloc[:first_train].notna().mean() >= .90
                    and features[c].iloc[:first_train].nunique(dropna=True) > 1]
        if len(selected) < 8:
            report['models'][str(h)] = {'status': 'insufficient_features'}
            continue
        x = features[selected].iloc[:n].copy()
        ret = (raw.close.shift(-h) / raw.close - 1).iloc[:n].to_numpy(dtype=float)
        horizon_entry = {'horizon_bars': h, 'horizon_minutes': h * STEP[interval] / 60000,
                         'feature_count': len(selected), 'modes': {}}
        for mode in ('fixed', 'atr_dynamic'):
            cut = threshold_series(features, mode, fixed_bps)
            y_full, _ = make_labels(raw.close, h, cut)
            y = y_full[:n].astype(np.int64)
            sub = {'label_classes': counts(y), 'validation_folds': [],
                   'holdout_start_utc': utc(int(raw.t.iloc[dev_end])),
                   'holdout_rows': n - dev_end,
                   'candidate_variants': {},
                   'mode': mode}
            # All selection uses strictly past training and F1/F2 forward validation.
            try:
                for variant in variants:
                    f_preds = []
                    first2 = []
                    for i, (tr, va) in enumerate(folds):
                        if min(np.bincount((y[tr] != 1).astype(int), minlength=2)) < 30:
                            raise ValueError('Insufficient directional events in fold training')
                        models = fit_pair(x.iloc[tr], y[tr], variant)
                        ev, up = predict_pair(models, x.iloc[va])
                        f_preds.append((ev, up))
                        first2.append((y[va], ret[va]))
                    candidate_policy = select_policy(
                        np.concatenate([first2[i][0] for i in (0, 1)]),
                        (np.concatenate([f_preds[i][0] for i in (0, 1)]),
                         np.concatenate([f_preds[i][1] for i in (0, 1)])))
                    third_pred = signals(*f_preds[2], candidate_policy['event_score_cut'],
                                         candidate_policy['direction_score_cut'])
                    third_stats = evaluate(y[folds[2][1]], third_pred, ret[folds[2][1]])
                    sub['candidate_variants'][variant] = {
                        'policy': candidate_policy,
                        'confirmation_fold3': third_stats,
                        'validation_fold3_rows': len(third_pred),
                    }
                # Select on F1/F2 only: never use confirmation/holdout for selection.
                def ranking(variant: str):
                    p = sub['candidate_variants'][variant]['policy']
                    s = p.get('selection_metrics')
                    return (s['directional_precision'] if s else -1,
                            s['correct_directional_signals'] if s else -1,
                            variant == 'standard')
                best = max(variants, key=ranking)
                pol = sub['candidate_variants'][best]['policy']
                final_fit_end = dev_end - h
                final_pair = fit_pair(x.iloc[:final_fit_end], y[:final_fit_end], best)
                ev, up = predict_pair(final_pair, x.iloc[dev_end:])
                hold_pred = signals(ev, up, pol['event_score_cut'], pol['direction_score_cut'])
                holdout = evaluate(y[dev_end:], hold_pred, ret[dev_end:])
                conf = sub['candidate_variants'][best]['confirmation_fold3']
                # Conservative research gate, never authorizes live deployment.
                diagnostic_pass = bool(pol['found_min_support_policy'] and
                                       pol['selection_metrics']['directional_precision'] >= .50 and
                                       conf['directional_signals'] >= 30 and
                                       conf['directional_precision'] is not None and
                                       conf['directional_precision'] >= .50)
                sub.update({'status': 'trained_research_only', 'selected_variant': best,
                            'selected_policy': pol, 'confirmation_fold3': conf,
                            'historical_holdout': holdout,
                            'validation_diagnostic_pass': diagnostic_pass,
                            'live_deployment_allowed': False})
                stem = f'{symbol}_{interval}_h{h}_{mode}'
                files = {'event_model': stem + '_event_model.txt',
                         'direction_model': stem + '_direction_model.txt',
                         'metadata': stem + '_metadata.json'}
                for model, fname in zip(final_pair, (files['event_model'], files['direction_model'])):
                    model.booster_.save_model(str(out / fname))
                # Ensure native model bytes round-trip without pickle.
                sample = x.iloc[dev_end:dev_end + 5]
                for model, filename in zip(final_pair, (files['event_model'], files['direction_model'])):
                    restored = lgb.Booster(model_file=str(out / filename)).predict(sample)
                    assert np.allclose(restored, model.predict_proba(sample)[:, 1], atol=1e-7, rtol=1e-6)
                meta = {'approval': 'RESEARCH_ONLY_NOT_APPROVED_FOR_LIVE',
                        'feature_schema': FEATURE_SCHEMA, 'features_ordered': selected,
                        'symbol': symbol, 'interval': interval, 'horizon': h,
                        'mode': mode, 'fixed_bps': fixed_bps,
                        'selected_variant': best, 'event_gate': pol['event_score_cut'],
                        'direction_gate': pol['direction_score_cut'],
                        'model_files': files, 'training_end_utc': utc(int(raw.t.iloc[final_fit_end - 1])),
                        'holdout_start_utc': utc(int(raw.t.iloc[dev_end])),
                        'holdout_reused_historical': True,
                        'lightgbm_version': lgb.__version__}
                (out / files['metadata']).write_text(json.dumps(meta, indent=2, ensure_ascii=False), encoding='utf-8')
                sub['artifacts'] = files
                print(f'h={h} mode={mode} selected={best} historical_holdout: '
                      f'signals={holdout["directional_signals"]}, '
                      f'directional_precision={holdout["directional_precision"]}, '
                      f'balanced_accuracy={holdout["balanced_accuracy"]}', flush=True)
            except ValueError as exc:
                sub['status'] = 'insufficient_data'
                sub['error'] = str(exc)
            horizon_entry['modes'][mode] = sub
        report['models'][str(h)] = horizon_entry
    (out / 'training_report.json').write_text(json.dumps(report, indent=2, ensure_ascii=False), encoding='utf-8')
    return report


def main() -> None:
    ap = argparse.ArgumentParser(description='TiMi ML v3 offline two-stage research trainer')
    ap.add_argument('--symbol', default='BTCUSDT')
    ap.add_argument('--interval', choices=tuple(STEP), default='1m')
    ap.add_argument('--bars', type=int, default=90000)
    ap.add_argument('--horizons', nargs='+', type=int, default=[1, 5])
    ap.add_argument('--cost-bps', type=float, default=12.0,
                    help='Only the fixed-label research threshold. NOT actual cost.')
    ap.add_argument('--out', default='models')
    args = ap.parse_args()
    args.symbol = args.symbol.upper()
    if args.symbol not in SYMBOLS or not 10000 <= args.bars <= 100000:
        ap.error('Invalid symbol or number of candles')
    if (not args.horizons or len(set(args.horizons)) != len(args.horizons)
            or any(h < 1 or h > 30 for h in args.horizons)):
        ap.error('Invalid horizon values')
    if not math.isfinite(args.cost_bps) or not 0 <= args.cost_bps <= 100:
        ap.error('Invalid threshold')
    raw = fetch(args.symbol, args.interval, args.bars)
    report = train_models(raw, args.symbol, args.interval, args.horizons,
                          args.cost_bps, Path(args.out))
    ready = sum(sub.get('status') == 'trained_research_only'
                for entry in report['models'].values() for sub in entry.get('modes', {}).values())
    print(f'Research training complete: {ready} out of {len(args.horizons) * 2} model pairs.', flush=True)
    if ready == 0:
        raise SystemExit('No models trained. See models/training_report.json')


if __name__ == '__main__':
    main()
