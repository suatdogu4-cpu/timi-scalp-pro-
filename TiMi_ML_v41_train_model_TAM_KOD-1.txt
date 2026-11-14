"""TiMi LightGBM v4.1 — offline, selective, regime-aware BTCUSDT research.
Developer: Suat DOĞU. No real orders, no gateway or live integration.

Important: pre-2026-11-13 historical data already inspected in v1-v4;
post-cutoff data was examined in v4, so it is NOT a pristine untouched test.
No algorithmic improvement is claimed without empirical evidence.
"""
from __future__ import annotations
import argparse
import json
import math
from datetime import datetime, timezone
from pathlib import Path

import lightgbm as lgb
import numpy as np
import pandas as pd
from lightgbm import LGBMClassifier

from ml_features import WARMUP_BARS
from ml_features_v41 import FEATURE_SCHEMA, REGIME_NAMES, make_v41_features
from train_model_v4 import (
    STEP, SYMBOLS, DEFAULT_ASOF, parse_utc, iso, validate_raw,
    fetch_training, fetch_after, triple_barrier, decide, score_metrics,
    folds, report_to_json,
)

VERSION = '4.1.0'
# Thresholds/markets are candidate hypotheses; early validation selects one.
SCORE_CUTS = (0.30, 0.38, 0.46, 0.54, 0.64)
MARGIN_CUTS = (0.00, 0.08, 0.16)
REGIMES = ('TUMU', 'TREND', 'YATAY', 'YUKSEK_OYNAKLIK')
MIN_PER_FOLD = 8  # non-overlapping events after horizon-bar cooldown
MIN_TOTAL = 24


def fit_v41(x: pd.DataFrame, y: np.ndarray, variant: str) -> LGBMClassifier:
    y = np.asarray(y, dtype=np.int64)
    cnt = np.bincount(y, minlength=3)
    if cnt.min() < 35:
        raise ValueError(f'Insufficient per-class training samples: {cnt.tolist()}')
    weights = None
    if variant == 'capped_weight':
        # Give minority directional classes more weight, capped to limit overfitting.
        weights = np.clip(np.sqrt(cnt[1] / np.maximum(cnt[y], 1)), 0.7, 3.0)
    elif variant != 'standard':
        raise ValueError(f'Unknown model variant: {variant}')
    model = LGBMClassifier(
        objective='multiclass', num_class=3,
        n_estimators=135, learning_rate=0.045, num_leaves=11, max_depth=4,
        min_child_samples=85, min_split_gain=0.03,
        colsample_bytree=0.8, subsample=0.85, subsample_freq=1,
        reg_lambda=14.0, n_jobs=2, random_state=41, verbosity=-1,
    )
    model.fit(x, y, sample_weight=weights)
    if list(model.classes_) != [0, 1, 2]:
        raise RuntimeError('Unexpected class order')
    return model


def regime_mask(regime_codes: np.ndarray, gate: str) -> np.ndarray:
    r = np.asarray(regime_codes)
    if gate == 'TUMU':
        return np.ones(len(r), dtype=bool)
    match = {'YATAY': 0, 'TREND': 1, 'YUKSEK_OYNAKLIK': 2}
    if gate not in match:
        raise ValueError(f'Unknown regime: {gate}')
    return r == match[gate]


def cooldown_predictions(probabilities: np.ndarray, global_indices: np.ndarray,
                         regime_codes: np.ndarray, *, horizon: int,
                         score_cut: float, margin: float, regime: str) -> np.ndarray:
    """At most one theoretical entry per horizon; no overlapping signal horizons.
    This reduces overlapping entries; it does NOT make price paths independent.
    """
    index = np.asarray(global_indices, dtype=np.int64)
    if horizon < 1 or len(index) != len(probabilities) or len(regime_codes) != len(index):
        raise ValueError('Invalid prediction series')
    if len(index) > 1 and np.any(np.diff(index) <= 0):
        raise ValueError('Indices must be ascending and unique')
    pred = decide(probabilities, score_cut, margin).copy()
    pred[~regime_mask(regime_codes, regime)] = 1
    last = -10 ** 15
    for k, idx in enumerate(index):
        if pred[k] == 1:
            continue
        if idx - last < horizon:
            pred[k] = 1
        else:
            last = int(idx)
    return pred


def compact_metrics(y: np.ndarray, pred: np.ndarray, barrier: dict,
                    idx: np.ndarray, regime_codes: np.ndarray) -> dict:
    """Evaluation with per-regime AND side breakdown, no hidden no-signal win rate."""
    out = score_metrics(y, pred, barrier, idx)
    out['regime_breakdown'] = {}
    for value, name in REGIME_NAMES.items():
        mask = np.asarray(regime_codes) == value
        sub = score_metrics(y, pred[mask], barrier, idx[mask]) if mask.any() else None
        out['regime_breakdown'][name] = {
            'rows': int(mask.sum()),
            'signals': sub['signals'] if sub else 0,
            'positive_after_cost_pct': sub['positive_after_assumed_cost_pct'] if sub else None,
            'avg_net_bps': sub['avg_net_change_bps'] if sub else None,
            'target_first_pct': sub['target_first_rate_pct'] if sub else None,
        }
    out['side_breakdown'] = {}
    for side, name in ((0, 'SHORT'), (2, 'LONG')):
        mask = pred == side
        if mask.any():
            net = np.asarray(barrier['net_short' if side == 0 else 'net_long'])[idx[mask]]
            out['side_breakdown'][name] = {
                'signals': int(mask.sum()),
                'positive_after_cost_pct': round(float(100 * (net > 0).mean()), 3),
                'avg_net_bps': round(float(net.mean()), 4),
            }
        else:
            out['side_breakdown'][name] = {
                'signals': 0, 'positive_after_cost_pct': None, 'avg_net_bps': None,
            }
    return out


def score_policy(early_folds: list[dict], horizon: int, score_cut: float,
                 margin: float, regime: str) -> dict:
    """Policy selection uses ONLY first two early OOF folds."""
    results = []
    for f in early_folds:
        ix = f['idx']
        pred = cooldown_predictions(
            f['proba'], ix, f['regimes'], horizon=horizon,
            score_cut=score_cut, margin=margin, regime=regime,
        )
        results.append(score_metrics(f['labels'], pred, f['barrier'], ix))
    ns = [s['signals'] for s in results]
    n = sum(ns)
    nets = [s['avg_net_change_bps'] for s in results]
    wins = [s['positive_after_assumed_cost_pct'] for s in results]
    supported = (n >= MIN_TOTAL and min(ns) >= MIN_PER_FOLD)
    positive_both = supported and all(x is not None and x > 0 for x in nets)
    total_net = (sum(float(nets[i] or 0) * ns[i] for i in range(2)) / n) if n else None
    total_win = (sum(float(wins[i] or 0) * ns[i] for i in range(2)) / n) if n else None
    return {
        'score_cut': score_cut, 'margin': margin, 'regime': regime,
        'signals_by_fold': ns, 'net_bps_by_fold': nets,
        'signals_total': n,
        'weighted_avg_net_bps': round(total_net, 5) if total_net is not None else None,
        'weighted_positive_after_cost_pct': round(total_win, 3) if total_win is not None else None,
        'has_minimum_support': bool(supported),
        'positive_in_both_folds': bool(positive_both),
    }


def select_policy_v41(early_folds: list[dict], horizon: int) -> dict:
    rows = [score_policy(early_folds, horizon, sc, margin, regime)
            for sc in SCORE_CUTS for margin in MARGIN_CUTS for regime in REGIMES]
    qualified = [r for r in rows if r['positive_in_both_folds']]
    # Leaderboard is DIAGNOSTIC only; it must never override a failed policy gate.
    diagnostic = sorted(rows,
        key=lambda r: (
            r['has_minimum_support'],
            -1e9 if r['weighted_avg_net_bps'] is None else r['weighted_avg_net_bps'],
            r['signals_total'],
        ), reverse=True)[:10]
    if not qualified:
        return {
            'qualified': False,
            'reason': 'No policy had enough cooled-down signals and positive mean net in BOTH early folds',
            'policies_examined': len(rows),
            'policies_with_support': sum(r['has_minimum_support'] for r in rows),
            'policies_stable_positive': 0,
            'diagnostic_top10_NOT_APPROVED': diagnostic,
            'score_cut': 0.99, 'margin': 0.99, 'regime': 'TUMU',
        }
    best = max(qualified, key=lambda r: (
        r['weighted_avg_net_bps'], r['signals_total'],
        r['weighted_positive_after_cost_pct'],
    ))
    return {
        'qualified': True,
        'reason': 'Early validation selected; later folds are confirmation only',
        'policies_examined': len(rows),
        'policies_with_support': sum(r['has_minimum_support'] for r in rows),
        'policies_stable_positive': len(qualified),
        'diagnostic_top10_NOT_APPROVED': diagnostic,
        **best,
    }


def evaluate_block(labels: np.ndarray, bar: dict, model: LGBMClassifier,
                   matrix: pd.DataFrame, indices: np.ndarray, regimes: np.ndarray,
                   policy: dict, horizon: int) -> dict:
    p = model.predict_proba(matrix.iloc[indices])
    pred = (cooldown_predictions(p, indices, regimes[indices], horizon=horizon,
                                 score_cut=policy['score_cut'], margin=policy['margin'],
                                 regime=policy['regime']) if policy['qualified']
            else np.ones(len(indices), dtype=np.int8))
    out = compact_metrics(labels, pred, bar, indices, regimes[indices])
    out['score_quantiles'] = {
        'raw_max_long_short': {str(q): round(float(np.quantile(np.maximum(p[:, 0], p[:, 2]), q)), 6)
                               for q in (0.5, 0.9, 0.99)},
        'predicted_raw_classes': {
            'SHORT': int((np.argmax(p, axis=1) == 0).sum()),
            'BEKLE': int((np.argmax(p, axis=1) == 1).sum()),
            'LONG': int((np.argmax(p, axis=1) == 2).sum()),
        },
    }
    return out


def train_v41(raw: pd.DataFrame, symbol: str, interval: str,
              horizons: list[int], cost_bps: float, target_atr: float, stop_atr: float,
              output: Path, asof_ms: int, future: pd.DataFrame | None = None) -> dict:
    output.mkdir(parents=True, exist_ok=True)
    raw = validate_raw(raw, interval, through=asof_ms - STEP[interval]).reset_index(drop=True)
    full_features, names, full_regimes = make_v41_features(raw)
    raw = raw.iloc[WARMUP_BARS:].reset_index(drop=True)
    features = full_features.iloc[WARMUP_BARS:].reset_index(drop=True)
    regimes = full_regimes[WARMUP_BARS:]
    if len(raw) < 8000:
        raise ValueError('Insufficient history after warmup')
    report = {
        'purpose': 'TiMi_LightGBM_v41_OFFLINE_RESEARCH_NOT_DEPLOYED',
        'version': VERSION,
        'symbol': symbol, 'interval': interval, 'asof_utc': iso(asof_ms),
        'source_start_utc': iso(raw.t.iloc[0]), 'source_end_utc': iso(raw.t.iloc[-1]),
        'created_utc': datetime.now(timezone.utc).isoformat(),
        'feature_schema': FEATURE_SCHEMA,
        'model_variants': ['standard', 'capped_weight'],
        'fixed_regime_definitions': {
            'YUKSEK_OYNAKLIK': 'ATR14 / prior 200-candle ATR median >= 1.35',
            'TREND': '|EMA20-EMA50| / ATR14 >= 0.60 AND efficiency20 >= 0.28, if not HIGH_VOL',
            'YATAY': 'remaining candles',
        },
        'estimated_total_roundtrip_cost_bps': cost_bps,
        'cost_measured_from_live_execution': False,
        'target_multiple_atr': target_atr, 'stop_multiple_atr': stop_atr,
        'entry_assumption': 'next candle OPEN (not verified execution)',
        'same_bar_tp_sl_rule': 'stop FIRST when both touched',
        'signal_cooldown_bars': 'equal to model horizon; drops overlapping entry hypotheses',
        'validation': 'Early OOF folds 1+2 select thresholds/regime, fold 3 checks, last 18% pre-asof is reused historical diagnostic',
        'prior_data_exposure': 'Pre-asof history reused from v1-v4; post-asof already examined in v4. Neither is pristine independent test.',
        'multiple_comparison_warning': '60 threshold/regime policies per model variant. Optimistic policy selection possible.',
        'limitations': ['Only closed OHLCV; no validated synchronized CVD/orderbook historical data',
                        'Hypothetical fills, spreads, fees, slippage and ATR barriers',
                        'Overlapping market conditions persist despite event cooldown',
                        'Model scores are not calibrated success probabilities',
                        'Historical splits are contaminated by repeated experiment selection',
                        'No live ML integration or real order submission'],
        'live_deployment_allowed': False,
        'horizons': {},
    }
    for horizon in horizons:
        n = len(raw) - horizon - 1
        if n < 8000:
            report['horizons'][str(horizon)] = {'status': 'INSUFFICIENT_DATA'}
            continue
        barrier = triple_barrier(raw, features['atr_pct_14'], horizon, cost_bps, target_atr, stop_atr)
        y = barrier['label']
        dev_end = int(n * 0.82)
        fold_spec = folds(dev_end, horizon)
        train_first_stop = fold_spec[0][0].stop
        chosen = [name for name in names
                  if features[name].iloc[:train_first_stop].notna().mean() >= 0.97
                  and features[name].iloc[:train_first_stop].nunique(dropna=True) > 5]
        if len(chosen) < 15:
            report['horizons'][str(horizon)] = {'status': 'INSUFFICIENT_VALID_FEATURES'}
            continue
        X = features[chosen].iloc[:n].copy()  # LightGBM handles NaNs natively.
        result = {
            'status': 'RESEARCH_ONLY',
            'feature_count': len(chosen), 'features_ordered': chosen,
            'label_counts': {
                'SHORT': int((y[:n] == 0).sum()),
                'BEKLE': int((y[:n] == 1).sum()),
                'LONG': int((y[:n] == 2).sum()),
            },
            'variants': {},
            'historical_holdout_start_utc': iso(raw.t.iloc[dev_end]),
        }
        candidate = []
        for variant in ('standard', 'capped_weight'):
            try:
                early = []
                fold3 = None
                for j, (train_slice, va) in enumerate(fold_spec):
                    if train_slice.stop + horizon > int(va[0]):
                        raise RuntimeError('PURGE VIOLATION: future label intersects validation')
                    model = fit_v41(X.iloc[train_slice], y[train_slice], variant)
                    record = {
                        'proba': model.predict_proba(X.iloc[va]),
                        'idx': va,
                        'labels': y,
                        'barrier': barrier,
                        'regimes': regimes[va],
                    }
                    if j < 2:
                        early.append(record)
                    else:
                        fold3 = record
                pol = select_policy_v41(early, horizon)
                # Confirmation fold never used to select policy or variant.
                conf_pred = (cooldown_predictions(
                    fold3['proba'], fold3['idx'], fold3['regimes'],
                    horizon=horizon, score_cut=pol['score_cut'],
                    margin=pol['margin'], regime=pol['regime'],
                ) if pol['qualified'] else np.ones(len(fold3['idx']), dtype=np.int8))
                confirmation = compact_metrics(y, conf_pred, barrier, fold3['idx'], fold3['regimes'])
                result['variants'][variant] = {
                    'policy': pol, 'fold3_confirmation': confirmation,
                }
                if pol['qualified']:
                    candidate.append((pol['weighted_avg_net_bps'], pol['signals_total'], variant))
            except ValueError as exc:
                result['variants'][variant] = {'error': str(exc)}
        if candidate:
            _, _, selected_variant = max(candidate)
            policy = result['variants'][selected_variant]['policy']
        else:
            selected_variant = 'standard'
            policy = {'qualified': False, 'reason': 'No stable positive early validation policy',
                      'score_cut': 0.99, 'margin': 0.99, 'regime': 'TUMU'}
        # Never train on rows whose horizon enters the holdout.
        train_end = dev_end - horizon
        if train_end <= 0 or train_end + horizon > dev_end:
            raise RuntimeError('Invalid train/holdout split')
        try:
            final_model = fit_v41(X.iloc[:train_end], y[:train_end], selected_variant)
        except ValueError as exc:
            result['status'] = 'INSUFFICIENT_TRAIN_CLASSES'
            result['error'] = str(exc)
            report['horizons'][str(horizon)] = result
            continue
        ix = np.arange(dev_end, n, dtype=np.int64)
        historical = evaluate_block(y, barrier, final_model, X, ix, regimes, policy, horizon)
        result.update({
            'selected_variant': selected_variant,
            'selected_policy': policy,
            'fold3_confirmation': result['variants'].get(selected_variant, {}).get('fold3_confirmation'),
            'historical_holdout_REUSED': historical,
            'reusable_historical_diagnostic_only': True,
            'diagnostic_pass': bool(
                policy['qualified']
                and result['fold3_confirmation']['signals'] >= 20
                and result['fold3_confirmation']['avg_net_change_bps'] is not None
                and result['fold3_confirmation']['avg_net_change_bps'] > 0
                and historical['signals'] >= 20
                and historical['avg_net_change_bps'] is not None
                and historical['avg_net_change_bps'] > 0
            ),
        })
        stem = f'{symbol}_{interval}_h{horizon}_v41'
        file_name = stem + '.txt'
        final_model.booster_.save_model(str(output / file_name))
        sample = X.iloc[dev_end:dev_end + 3]
        restored = lgb.Booster(model_file=str(output / file_name)).predict(sample)
        if not np.allclose(restored, final_model.predict_proba(sample), atol=1e-7, rtol=1e-6):
            raise RuntimeError('Stored model round-trip failed')
        meta = {
            'approval': 'RESEARCH_ONLY_NOT_APPROVED_FOR_LIVE',
            'feature_schema': FEATURE_SCHEMA, 'feature_order': chosen,
            'training_end_utc': iso(raw.t.iloc[train_end - 1]),
            'asof_utc': iso(asof_ms), 'symbol': symbol, 'interval': interval,
            'horizon': horizon, 'model_filename': file_name,
            'variant': selected_variant, 'selected_policy': policy,
            'target_atr': target_atr, 'stop_atr': stop_atr,
            'estimated_roundtrip_cost_bps': cost_bps,
            'cooldown_bars': horizon,
            'note': 'No live deployment permitted. Scores not calibrated probabilities.',
        }
        report_to_json(meta, output / (stem + '_metadata.json'))
        result['model_artifact'] = file_name
        if future is not None and len(future) > horizon + 1:
            # Carry only pre-cutoff history for rolling indicators, never refit the model.
            joined = pd.concat([raw.tail(WARMUP_BARS + 210), future], ignore_index=True)
            joined = validate_raw(joined, interval)
            f_features, _, f_regimes = make_v41_features(joined)
            fbar = triple_barrier(joined, f_features['atr_pct_14'], horizon,
                                  cost_bps, target_atr, stop_atr)
            off = len(joined) - len(future)
            usable = np.arange(off, len(joined) - horizon - 1, dtype=np.int64)
            if len(usable):
                p = final_model.predict_proba(f_features[chosen].iloc[usable])
                pred = (cooldown_predictions(p, usable, f_regimes[usable], horizon=horizon,
                                             score_cut=policy['score_cut'],
                                             margin=policy['margin'], regime=policy['regime'])
                        if policy['qualified'] else np.ones(len(usable), dtype=np.int8))
                future_stats = compact_metrics(fbar['label'], pred, fbar, usable, f_regimes[usable])
                result['post_cutoff_REUSED_from_v4_period'] = {
                    'start_utc': iso(joined.t.iloc[usable[0]]),
                    'end_utc': iso(joined.t.iloc[usable[-1]]),
                    'rows': len(usable), 'metrics': future_stats,
                    'sample_adequacy': 'SMALL' if len(usable) < 10000 else 'LARGER_NOT_INDEPENDENT',
                    'independence_warning': 'Same calendar period already examined in v4; not a fresh uninspected test.',
                }
            else:
                result['post_cutoff_REUSED_from_v4_period'] = {'status': 'INSUFFICIENT_ROWS'}
        else:
            result['post_cutoff_REUSED_from_v4_period'] = {'status': 'NOT_YET_AVAILABLE'}
        report['horizons'][str(horizon)] = result
        print(f'[{horizon}m] early_policy={policy["qualified"]}, '
              f'fold3_signals={result["fold3_confirmation"]["signals"]}, '
              f'historical_signals={historical["signals"]}, '
              f'historical_net_bps={historical["avg_net_change_bps"]}', flush=True)
    report_to_json(report, output / 'training_report_v41.json')
    return report


def main() -> None:
    ap = argparse.ArgumentParser(description='TiMi v4.1 LightGBM offline research only')
    ap.add_argument('--symbol', default='BTCUSDT')
    ap.add_argument('--interval', choices=tuple(STEP), default='1m')
    ap.add_argument('--bars', type=int, default=90000)
    ap.add_argument('--horizons', nargs='+', type=int, default=[5, 15])
    ap.add_argument('--asof', default=DEFAULT_ASOF)
    ap.add_argument('--roundtrip-cost-bps', type=float, default=12.0)
    ap.add_argument('--target-atr', type=float, default=1.5)
    ap.add_argument('--stop-atr', type=float, default=1.0)
    ap.add_argument('--out', default='models_v41')
    ap.add_argument('--training-csv', help='Offline fixture; must end before frozen cutoff')
    ap.add_argument('--future-csv', help='Offline fixture; only strictly after frozen cutoff')
    args = ap.parse_args()
    args.symbol = args.symbol.upper()
    if args.symbol not in SYMBOLS or not 10000 <= args.bars <= 200000:
        ap.error('Unsupported symbol or bars (10000..200000)')
    if not args.horizons or len(args.horizons) != len(set(args.horizons)) or any(h < 1 or h > 30 for h in args.horizons):
        ap.error('Invalid horizons')
    if not all(math.isfinite(x) for x in (args.roundtrip_cost_bps, args.target_atr, args.stop_atr)):
        ap.error('Cost/ATR must be finite')
    if not (0 <= args.roundtrip_cost_bps <= 100 and 0.2 <= args.target_atr <= 5 and 0.2 <= args.stop_atr <= 5):
        ap.error('Invalid cost/ATR assumptions')
    asof_ms = parse_utc(args.asof)
    if asof_ms % STEP[args.interval] != 0:
        ap.error('ASOF must match candle alignment')
    if args.training_csv:
        training = validate_raw(pd.read_csv(args.training_csv), args.interval, through=asof_ms - STEP[args.interval])
    else:
        training = fetch_training(args.symbol, args.interval, args.bars, asof_ms)
    if args.future_csv:
        future = validate_raw(pd.read_csv(args.future_csv), args.interval, after=asof_ms - 1)
    else:
        future = fetch_after(args.symbol, args.interval, asof_ms)
    outcome = train_v41(training, args.symbol, args.interval, args.horizons,
                        args.roundtrip_cost_bps, args.target_atr, args.stop_atr,
                        Path(args.out), asof_ms, future)
    print('OFFLINE RESEARCH ONLY. Results in ' + str(Path(args.out) / 'training_report_v41.json'))
    if not any(h.get('status') == 'RESEARCH_ONLY' for h in outcome['horizons'].values()):
        raise SystemExit('No LightGBM v4.1 model trained; inspect report')


if __name__ == '__main__':
    main()
