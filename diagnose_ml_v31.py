"""TiMi ML v3.1 — frozen model error audit; OFFLINE, READ-ONLY research.

This script does not train, change model settings, place orders, or serve predictions.
It only reads closed Binance Spot candles and a specific immutable ML v3 artifact.
Developer: Suat DOĞU.

Note: Historical evaluation is exploratory, because earlier experiments saw those
periods. Prospective evidence means timestamps later than the model creation
instant, with NO subsequent model or threshold adjustments.
"""
from __future__ import annotations

import argparse
import hashlib
import json
import math
import time
import zipfile
from datetime import datetime, timezone
from pathlib import Path

import httpx
import lightgbm as lgb
import numpy as np
import pandas as pd

from ml_features import FEATURE_SCHEMA, make_features

API = 'https://data-api.binance.vision/api/v3/klines'
STEP_MS = 60_000
EXPECTED_ZIP_SHA256 = '36d53d7215e2a1544b8ceb6b401553d36449c4a218706a59137284b51dd6abce'
MODEL_CREATED_UTC = '2026-11-13T16:30:23.903416+00:00'
FREEZE_DATA_END_UTC = '2026-11-13T16:28:00+00:00'
HISTORICAL_START_UTC = '2026-11-01T05:15:00+00:00'
LOOKBACK = 3000  # Long pre-window to stabilize EMA200 before evaluating rows.
MAX_CANDLES = 110_000


def ms(iso: str) -> int:
    return int(datetime.fromisoformat(iso.replace('Z', '+00:00')).timestamp() * 1000)


def stamp(ts_ms: int) -> str:
    return datetime.fromtimestamp(int(ts_ms) / 1000, timezone.utc).isoformat()


def load_frozen_models(path: Path) -> tuple[dict, dict]:
    if not path.exists():
        raise FileNotFoundError(f'Frozen model ZIP not found: {path}')
    digest = hashlib.sha256(path.read_bytes()).hexdigest()
    if digest != EXPECTED_ZIP_SHA256:
        raise RuntimeError('Frozen ZIP SHA256 is different. Refusing silently changed models.')
    with zipfile.ZipFile(path) as z:
        report = json.loads(z.read('training_report.json'))
        if (report.get('purpose') != 'TiMi_ML_v3_research_not_deployed' or
            report.get('feature_schema') != FEATURE_SCHEMA or
            report.get('created_utc') != MODEL_CREATED_UTC or
            report.get('data_end_utc') != FREEZE_DATA_END_UTC):
            raise RuntimeError('Model training report does not match the frozen v3 snapshot')
        models = {}
        for horizon in (1, 5):
            for mode in ('fixed', 'atr_dynamic'):
                key = f'BTCUSDT_1m_h{horizon}_{mode}'
                metadata = json.loads(z.read(key + '_metadata.json'))
                if (metadata['approval'] != 'RESEARCH_ONLY_NOT_APPROVED_FOR_LIVE' or
                    metadata['feature_schema'] != FEATURE_SCHEMA or
                    metadata['horizon'] != horizon or metadata['mode'] != mode or
                    metadata['symbol'] != 'BTCUSDT' or metadata['interval'] != '1m'):
                    raise RuntimeError(f'Invalid metadata: {key}')
                files = metadata['model_files']
                event = lgb.Booster(model_str=z.read(files['event_model']).decode('utf-8'))
                direction = lgb.Booster(model_str=z.read(files['direction_model']).decode('utf-8'))
                if event.feature_name() != metadata['features_ordered'] or direction.feature_name() != metadata['features_ordered']:
                    raise RuntimeError(f'Features mismatch: {key}')
                models[key] = (metadata, event, direction)
    return {'archive_sha256': digest, 'report': report}, models


def fetch_closed_candles(start_ms: int, end_ms: int) -> pd.DataFrame:
    """Forward-paginate a fixed UTC window; do not accept missing/unfinished bars."""
    if (start_ms % STEP_MS or end_ms % STEP_MS or end_ms < start_ms):
        raise ValueError('Invalid minute-aligned candle window')
    count = (end_ms - start_ms) // STEP_MS + 1
    if count > MAX_CANDLES:
        raise RuntimeError(f'Window has {count} candles; maximum {MAX_CANDLES}. Change storage strategy, not evaluation cutoff.')
    rows: list[list] = []
    now = int(time.time() * 1000)
    if end_ms + STEP_MS + 3000 > now:
        raise RuntimeError('Requested end candle may not be closed yet')
    t = start_ms
    with httpx.Client(timeout=30, headers={'User-Agent': 'TiMi-ML-v3.1-ReadOnly-Research'}) as client:
        while t <= end_ms:
            size = min(1000, (end_ms - t) // STEP_MS + 1)
            for attempt in range(5):
                try:
                    response = client.get(API, params={
                        'symbol': 'BTCUSDT', 'interval': '1m', 'startTime': t,
                        'endTime': end_ms + STEP_MS - 1, 'limit': size})
                    response.raise_for_status()
                    page = response.json()
                    break
                except httpx.HTTPStatusError as exc:
                    status = exc.response.status_code
                    if attempt == 4 or status in (400, 401, 403, 404, 418) or (status < 500 and status != 429):
                        raise
                    time.sleep(min(15, 2 ** attempt))
                except (httpx.TimeoutException, httpx.TransportError):
                    if attempt == 4:
                        raise
                    time.sleep(min(10, 2 ** attempt))
            if not isinstance(page, list) or not page:
                raise RuntimeError(f'Exchange returned no candles at {stamp(t)}')
            if int(page[0][0]) != t:
                raise RuntimeError(f'Missing first candle at {stamp(t)}')
            rows.extend(page)
            t = int(page[-1][0]) + STEP_MS
            time.sleep(0.15)
    if len(rows) != count:
        raise RuntimeError(f'Expected {count} candles; got {len(rows)}')
    df = pd.DataFrame(rows, columns=[
        't', 'open', 'high', 'low', 'close', 'volume', 'close_t',
        'quote_volume', 'n_trades', 'taker_base', 'taker_quote', 'ignore'])
    for c in ('t', 'close_t', 'open', 'high', 'low', 'close', 'volume'):
        df[c] = pd.to_numeric(df[c], errors='coerce')
    if df[['t','close_t','open','high','low','close','volume']].isna().any().any():
        raise RuntimeError('Malformed OHLCV')
    df['t'] = df.t.astype('int64')
    if df.t.duplicated().any() or (df.t.diff().iloc[1:] != STEP_MS).any():
        raise RuntimeError('OHLCV contains missing/duplicated candles')
    if (df.t.to_numpy() + STEP_MS + 3000 > now).any() or (df.close_t >= now - 3000).any():
        raise RuntimeError('Unclosed candles detected')
    if ((df[['open', 'high', 'low', 'close']] <= 0).any().any() or
        (df.volume < 0).any() or
        (df.high < df[['open', 'close', 'low']].max(axis=1)).any() or
        (df.low > df[['open','close','high']].min(axis=1)).any()):
        raise RuntimeError('Invalid candle OHLCV')
    return df[['t','open','high','low','close','volume']]


def score_one(raw: pd.DataFrame, feats: pd.DataFrame, meta: dict,
              event: lgb.Booster, direction: lgb.Booster,
              selection_start_ms: int, selection_end_ms: int, scope: str) -> tuple[pd.DataFrame, dict]:
    horizon = meta['horizon']
    if not all(name in feats.columns for name in meta['features_ordered']):
        raise RuntimeError('Missing feature columns')
    x = feats[meta['features_ordered']]
    if x.iloc[LOOKBACK:].isna().any().any():
        raise RuntimeError('NaN in post-warmup feature rows; research aborted')
    ev = np.asarray(event.predict(x), dtype=float)
    up = np.asarray(direction.predict(x), dtype=float)
    if (not np.isfinite(ev).all() or not np.isfinite(up).all() or
        (ev < 0).any() or (ev > 1).any() or (up < 0).any() or (up > 1).any()):
        raise RuntimeError('Invalid model scores')
    decisions = np.ones(len(raw), dtype=int)
    eligible = ev >= meta['event_gate']
    decisions[eligible & (up >= meta['direction_gate'])] = 2
    decisions[eligible & (up <= 1 - meta['direction_gate'])] = 0
    close = raw.close.to_numpy(dtype=float)
    future_close = np.full(len(raw), np.nan)
    future_close[:-horizon] = close[horizon:]
    next_open = np.r_[raw.open.to_numpy(dtype=float)[1:], np.nan]
    future_return = future_close / close - 1.0
    thresholds = (np.full(len(raw), meta['fixed_bps']) if meta['mode'] == 'fixed' else
                  np.clip(0.75 * feats.atr_pct_14.to_numpy(dtype=float) * 10_000, 4.0, 24.0))
    truth = np.select((future_return < -thresholds / 10_000,
                       future_return > thresholds / 10_000), (0, 2), default=1)
    # For h=5, a sample is evaluated only once the actual five future minutes exist.
    timestamps = raw.t.to_numpy(dtype=np.int64)
    selected = ((timestamps >= selection_start_ms) & (timestamps <= selection_end_ms) &
                np.isfinite(future_return) & (decisions != 1) &
                (np.arange(len(raw)) >= LOOKBACK))
    rows = []
    for i in np.flatnonzero(selected):
        pred, actual = int(decisions[i]), int(truth[i])
        sign = 1 if pred == 2 else -1
        signed_close_bps = sign * future_return[i] * 10000
        # Optimistic price-only entry at *next candle open*; fees and slippage are illustrative.
        gross_next_open_bps = sign * (future_close[i] / next_open[i] - 1.0) * 10000
        condition = 'correct' if pred == actual else 'neutral_actual' if actual == 1 else 'opposite'
        rows.append({
            'scope': scope, 'model': f'h{horizon}_{meta["mode"]}',
            'signal_candle_open_utc': stamp(timestamps[i]),
            'signal_direction': 'LONG' if pred == 2 else 'SHORT',
            'actual_class': ('DOWN', 'NEUTRAL', 'UP')[actual],
            'result': condition,
            'score_event_UNCALIBRATED': round(float(ev[i]), 8),
            'score_up_given_event_UNCALIBRATED': round(float(up[i]), 8),
            'price_at_signal_close': round(float(close[i]), 8),
            'hypothetical_next_candle_open': round(float(next_open[i]), 8),
            'future_horizon_close': round(float(future_close[i]), 8),
            'signed_close_to_close_bps': round(float(signed_close_bps), 6),
            'hypothetical_gross_next_open_bps': round(float(gross_next_open_bps), 6),
            'hypothetical_net_6bps': round(float(gross_next_open_bps - 6), 6),
            'hypothetical_net_12bps': round(float(gross_next_open_bps - 12), 6),
            'hypothetical_net_20bps': round(float(gross_next_open_bps - 20), 6),
            'label_threshold_bps': round(float(thresholds[i]), 6),
            'atr14_bps': round(float(feats.atr_pct_14.iloc[i] * 10000), 6),
            'rsi14': round(float(feats.rsi_14.iloc[i]), 4),
            'volume_ratio20': round(float(feats.volume_ratio_20.iloc[i]), 6),
            'ema20_distance': round(float(feats.ema_distance_20.iloc[i]), 8),
            'macd_pct': round(float(feats.macd_pct.iloc[i]), 8),
        })
    valid = ((timestamps >= selection_start_ms) & (timestamps <= selection_end_ms) &
             np.isfinite(future_return) & (np.arange(len(raw)) >= LOOKBACK))
    true_events = int(np.sum(valid & (truth != 1)))
    df = pd.DataFrame(rows)
    total_signals = len(df)
    correct = int((df.result == 'correct').sum()) if total_signals else 0
    opposite = int((df.result == 'opposite').sum()) if total_signals else 0
    neutral_actual = int((df.result == 'neutral_actual').sum()) if total_signals else 0
    mean_gross = (float(df.hypothetical_gross_next_open_bps.mean()) if total_signals else None)
    summ = {
        'scope': scope, 'model': f'h{horizon}_{meta["mode"]}',
        'evaluated_closed_candles': int(valid.sum()),
        'directional_signals': total_signals,
        'correct': correct, 'opposite': opposite, 'neutral_actual': neutral_actual,
        'directional_precision': correct / total_signals if total_signals else None,
        'signal_coverage': total_signals / int(valid.sum()) if valid.sum() else None,
        'directional_event_recall': correct / true_events if true_events else None,
        'mean_signed_next_open_gross_bps': mean_gross,
        'mean_signed_next_open_minus_6bps': mean_gross - 6 if mean_gross is not None else None,
        'mean_signed_next_open_minus_12bps': mean_gross - 12 if mean_gross is not None else None,
        'mean_signed_next_open_minus_20bps': mean_gross - 20 if mean_gross is not None else None,
        'support_warning': total_signals < 30,
    }
    return df, summ


def group_diagnostics(data: pd.DataFrame) -> pd.DataFrame:
    """Pure descriptions only: NEVER select new thresholds on these categories."""
    if data.empty:
        return pd.DataFrame()
    out = []
    for (scope, model), df in data.groupby(['scope', 'model']):
        groups = {
            'atr_lt_5_bps': df.atr14_bps < 5,
            'atr_5_to_10_bps': (df.atr14_bps >= 5) & (df.atr14_bps < 10),
            'atr_10_to_20_bps': (df.atr14_bps >= 10) & (df.atr14_bps < 20),
            'atr_gte_20_bps': df.atr14_bps >= 20,
            'rsi_lt_30': df.rsi14 < 30,
            'rsi_30_to_70': (df.rsi14 >= 30) & (df.rsi14 <= 70),
            'rsi_gt_70': df.rsi14 > 70,
            'volume_below_ma20': df.volume_ratio20 < 0,
            'volume_above_or_equal_ma20': df.volume_ratio20 >= 0,
            'price_above_ema20': df.ema20_distance >= 0,
            'price_below_ema20': df.ema20_distance < 0,
            'LONG': df.signal_direction == 'LONG',
            'SHORT': df.signal_direction == 'SHORT',
        }
        for name, mask in groups.items():
            s = df[mask]
            n = len(s)
            out.append({
                'scope': scope, 'model': model, 'group': name,
                'signal_count': n,
                'correct': int((s.result == 'correct').sum()),
                'opposite': int((s.result == 'opposite').sum()),
                'neutral_actual': int((s.result == 'neutral_actual').sum()),
                'directional_precision': (float((s.result == 'correct').mean()) if n else None),
                'mean_gross_next_open_bps': (float(s.hypothetical_gross_next_open_bps.mean()) if n else None),
                'INSUFFICIENT_SAMPLE_UNDER_30': n < 30,
            })
    return pd.DataFrame(out)


def feature_importances(models: dict) -> pd.DataFrame:
    rows = []
    for key, (_, event, direction) in models.items():
        for role, booster in (('event', event), ('direction', direction)):
            gains = booster.feature_importance(importance_type='gain')
            all_gains = float(gains.sum())
            for name, gain in zip(booster.feature_name(), gains):
                rows.append({'model': key, 'stage': role, 'feature': name,
                             'gain_importance_fraction': float(gain) / all_gains if all_gains else None,
                             'gain_importance_NOT_causal': float(gain)})
    return pd.DataFrame(rows).sort_values(['model', 'stage', 'gain_importance_fraction'],
                                          ascending=[True, True, False])


def main() -> None:
    parser = argparse.ArgumentParser(description='TiMi ML v3.1 read-only frozen model diagnostics')
    parser.add_argument('--models-zip', default='TiMi-ML-v3-Research-Results.zip')
    parser.add_argument('--scope', choices=('prospective', 'historical', 'both'), default='prospective')
    parser.add_argument('--out', default='v31_audit')
    args = parser.parse_args()
    out = Path(args.out)
    out.mkdir(parents=True, exist_ok=True)
    frozen, models = load_frozen_models(Path(args.models_zip))
    snapshots = []
    summaries = []
    windows = []
    for scope in (('historical', 'prospective') if args.scope == 'both' else (args.scope,)):
        if scope == 'historical':
            eval_start = ms(HISTORICAL_START_UTC)
            end_ms = ms(FREEZE_DATA_END_UTC)
        else:
            eval_start = (ms(MODEL_CREATED_UTC) // STEP_MS + 1) * STEP_MS
            now_ms = int(time.time() * 1000)
            end_ms = ((now_ms - 3000) // STEP_MS) * STEP_MS - STEP_MS
        if end_ms < eval_start + 5 * STEP_MS:
            windows.append({'scope': scope, 'status': 'INSUFFICIENT_COMPLETED_POST_FREEZE_CANDLES',
                            'from_utc': stamp(eval_start), 'last_utc': stamp(end_ms)})
            continue
        download_start = eval_start - LOOKBACK * STEP_MS
        print(f'Fetching CLOSED 1m BTCUSDT candles: {stamp(download_start)} -> {stamp(end_ms)} ({scope})', flush=True)
        raw = fetch_closed_candles(download_start, end_ms)
        features, feature_names = make_features(raw)
        if FEATURE_SCHEMA != 'timi-ohlcv-v1' or len(feature_names) != 36:
            raise RuntimeError('Unexpected features schema; no audit was produced')
        # Require the same Binance closed-candle data policy as training.
        windows.append({'scope': scope, 'status': 'DATA_OK',
                        'from_utc': stamp(eval_start), 'last_utc': stamp(end_ms),
                        'downloaded_rows_including_feature_warmup': len(raw)})
        for model_id, (meta, event, direction) in models.items():
            # Keep the original per-horizon holdout start for historical auditing.
            model_start = (ms(frozen['report']['models'][str(meta['horizon'])]['modes'][meta['mode']]['holdout_start_utc'])
                           if scope == 'historical' else eval_start)
            df, summary = score_one(raw, features, meta, event, direction,
                                    model_start, end_ms, scope)
            snapshots.append(df)
            summaries.append(summary)
            print(f'{scope} {model_id}: signals={summary["directional_signals"]}, '
                  f'correct={summary["correct"]}, opposite={summary["opposite"]}, '
                  f'neutral={summary["neutral_actual"]}', flush=True)
    all_signals = pd.concat(snapshots, ignore_index=True) if snapshots else pd.DataFrame()
    all_signals.to_csv(out / 'signal_audit.csv', index=False)
    grouped = group_diagnostics(all_signals)
    grouped.to_csv(out / 'condition_breakdown.csv', index=False)
    feature_importances(models).to_csv(out / 'feature_importance.csv', index=False)
    info = {
        'purpose': 'TiMi ML v3.1 RESEARCH READ-ONLY error audit and frozen prospective evaluation',
        'created_utc': datetime.now(timezone.utc).isoformat(),
        'frozen_model_archive_sha256': frozen['archive_sha256'],
        'frozen_model_created_utc': MODEL_CREATED_UTC,
        'prospective_sample_rule': 'Only 1m candle opens STRICTLY after model creation UTC; full h-candle outcome available',
        'threshold_policy': 'Unchanged, loaded from 2026-11-13 model metadata; no model retraining or tuning',
        'historical_warning': 'All historical rows overlap previously examined experiments. Exploratory ONLY.',
        'prospective_warning': 'Repeat runs observe overlapping samples; do not tune on prospective performance and then claim untouched testing.',
        'execution_warning': 'Not executed trades. Hypothetical next-open returns ignore spread, order execution, bid/ask and latency. 6/12/20bps are illustrative TOTAL cost scenarios.',
        'dependence_warning': 'Nearby signals and h-bar outcomes overlap; sample counts are not independent trades.',
        'live_deployment_allowed': False,
        'windows': windows,
        'models': summaries,
    }
    (out / 'audit_summary.json').write_text(json.dumps(info, ensure_ascii=False, indent=2), encoding='utf-8')
    lines = ['# TiMi ML v3.1 — Hata Analizi (Araştırma)', '',
             f'Model oluşturulma UTC: {MODEL_CREATED_UTC}',
             f'Rapor oluşturulma UTC: {info["created_utc"]}',
             'GERÇEK EMİR YOK. CANLI MODELE BAĞLI DEĞİL.',
             'Bu rapor işlem getirisi veya net PnL kanıtı değildir.', '',
             '## Modeller']
    for m in summaries:
        lines.extend([f'### {m["scope"]} / {m["model"]}',
                      f'- Mum: {m["evaluated_closed_candles"]}; yönlü sinyal: {m["directional_signals"]}; doğru: {m["correct"]}; ters yön: {m["opposite"]}; NÖTR: {m["neutral_actual"]}',
                      f'- Yönlü isabet: {m["directional_precision"]}; sinyal sıklığı: {m["signal_coverage"]}; gerçek olayı yakalama: {m["directional_event_recall"]}',
                      f'- Sonraki açılıştan hedef kapanışına **temsili** ortalama yönlü brüt bps: {m["mean_signed_next_open_gross_bps"]}',
                      f'- Toplam 12bps varsayımsal maliyet sonrası ortalama: {m["mean_signed_next_open_minus_12bps"]}',
                      f'- En az 30 sinyal koşulu: {"YETERSİZ" if m["support_warning"] else "Sayı yeterli; bağımsızlık yine kanıtlanmadı"}', ''])
    lines.extend(['## Sınırlamalar',
                  '- Eşikler ve modeller değiştirilmedi. Geriye dönük veri önceki deneylerle çakışıyor.',
                  '- Fiyatlar alınıp satılabilir emir fiyatı değildir; spread/slippage ve gerçek gecikme ölçülmedi.',
                  '- `signal_audit.csv` yalnızca LONG/SHORT sinyallerini içerir.',
                  '- `condition_breakdown.csv` ATR, RSI, hacim ve EMA yönü kırılımlarını gösterir.',
                  '- `feature_importance.csv` LightGBM gain önemlerini gösterir; nedensellik değildir.',
                  '- Canlıya bağlama YASAK; hata analizi yalnızca araştırma içindir.'])
    (out / 'OKU_BENI.md').write_text('\n'.join(lines)+'\n', encoding='utf-8')
    print(f'Completed read-only audit. Outputs: {out.resolve()}', flush=True)


if __name__ == '__main__':
    main()
