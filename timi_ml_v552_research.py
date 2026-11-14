"""TiMi ML v5.5.2 — offline, cost-aware, regime-scoped BTCUSDT research.

Strictly research-only. No orders, no live serving, no private keys, no DB writes.
Requires sibling v5.4, v5.5 and v5.5.1 research modules.

Unlike v5.5.1, fits separate expected NET-return regressors for AL and SAT;
thresholds and barrier geometry are selected on a disjoint chronological
policy-validation fold. A reserved TEST fold is evaluated at most once after
selection. If validation fails cost/risk gates, signal production is disabled.

Spot SAT is a hypothetical bearish direction, NOT executable short-selling.
Binance archive funding is verified by SHA256; unavailable data is omitted.
"""
from __future__ import annotations

import argparse
import csv
import hashlib
import io
import json
import math
import urllib.error
import zipfile
from dataclasses import dataclass
from datetime import datetime, timezone
from pathlib import Path
from typing import Callable

import numpy as np
import pandas as pd

import timi_ml_v54_history_train as v54
import timi_ml_v55_research as v55
import timi_ml_v551_research as v551

MS = v54.MS
SCHEMA = "timi-ml-v552-cost-aware-fourfold-regime-v1"
FUNDING_ARCHIVE_BASE = "https://data.binance.vision/data/futures/um/monthly/fundingRate/BTCUSDT"
# The model does not impute any unobserved market data.
BARRIER_CONFIGS = (
    ("rr_1p25_t2p5_s0p8", 2.5, 0.8),
    ("rr_1p25_t3p5_s1p1", 3.5, 1.1),
    ("rr_1p25_t4p5_s1p5", 4.5, 1.5),
)
THRESHOLDS_BPS = (6., 12., 20.)
MARGINS_BPS = (4., 12.)
REGIME_POLICIES = ("all", "trend_only", "avoid_high_vol")
FEATURE_GROUPS = ("A_v55_base", "C_base_cvd_regime", "D_base_cvd_regime_funding")


@dataclass(frozen=True)
class Settings:
    costs: v55.Costs = v55.Costs()
    minimum_reward_risk: float = 1.25
    min_validation_trades: int = 40
    minimum_validation_profit_factor: float = 1.05
    minimum_validation_mean_net_bps: float = 0.0
    maximum_validation_drawdown_pct: float = 35.0
    seed: int = 552

    def validate(self):
        self.costs.validate()
        if not (math.isfinite(self.minimum_reward_risk) and 1.0 <= self.minimum_reward_risk <= 5.0):
            raise ValueError("minimum_reward_risk must be in [1,5]")
        if not (10 <= self.min_validation_trades <= 10_000):
            raise ValueError("min_validation_trades out of range")
        if not (0 <= self.maximum_validation_drawdown_pct <= 100):
            raise ValueError("max validation drawdown out of range")
        if not (math.isfinite(self.minimum_validation_profit_factor) and self.minimum_validation_profit_factor >= 1):
            raise ValueError("invalid minimum profit factor")
        if not math.isfinite(self.minimum_validation_mean_net_bps):
            raise ValueError("invalid minimum validation return")


def read_funding_archive(first: str, last: str, *, download: Callable | None = None):
    """Read Binance Vision USD-M monthly funding, verifying official .CHECKSUM.

    Supports named CSV fields: calc_time, funding_interval_hours,
    last_funding_rate. No network occurs in tests when `download` supplied.
    Does not silently replace invalid/missing archives with estimated events.
    """
    get = download or v54._get
    result = []
    seen = set()
    for month in v54.month_range(first, last):
        filename = f"BTCUSDT-fundingRate-{month}.zip"
        url = f"{FUNDING_ARCHIVE_BASE}/{filename}"
        data = get(url)
        check = get(url + ".CHECKSUM").decode("utf-8").strip().split()
        checksum = check[0].lower() if check else ""
        if len(checksum) != 64 or any(ch not in "0123456789abcdef" for ch in checksum):
            raise ValueError(f"Invalid official funding checksum metadata for {month}")
        if hashlib.sha256(data).hexdigest() != checksum:
            raise ValueError(f"Binance funding archive SHA256 mismatch for {month}")
        count = 0
        with zipfile.ZipFile(io.BytesIO(data)) as z:
            parts = [x for x in z.infolist() if not x.is_dir()]
            if len(parts) != 1 or not parts[0].filename.lower().endswith(".csv") or parts[0].file_size > 5_000_000:
                raise ValueError("Unexpected or oversized funding archive structure")
            with z.open(parts[0]) as fh:
                reader = csv.DictReader(io.TextIOWrapper(fh, encoding="utf-8-sig", newline=""))
                columns = set(reader.fieldnames or [])
                if not {"calc_time", "funding_interval_hours", "last_funding_rate"} <= columns:
                    raise ValueError(f"Unexpected official funding CSV fields: {sorted(columns)}")
                for row in reader:
                    t = int(row["calc_time"])
                    if t >= 10**15 and t % 1000 == 0:
                        t //= 1000
                    if not 10**12 <= t < 10**14:
                        raise ValueError("Funding timestamp outside millisecond range")
                    actual_month = datetime.fromtimestamp(t / 1000, tz=timezone.utc).strftime("%Y-%m")
                    if actual_month != month:
                        raise ValueError(f"Funding event outside archive month {month}")
                    interval = float(row["funding_interval_hours"])
                    rate = float(row["last_funding_rate"])
                    if not (math.isfinite(interval) and 0 < interval <= 24 and math.isfinite(rate) and abs(rate) <= 0.1):
                        raise ValueError("Invalid funding interval or rate")
                    if t in seen:
                        raise ValueError("Duplicated funding settlement")
                    seen.add(t)
                    result.append((t, rate))
                    count += 1
        if count < 20:
            raise ValueError(f"Funding archive {month} lacks required events")
    result.sort()
    if not result or any(b[0] <= a[0] for a, b in zip(result, result[1:])):
        raise ValueError("Missing or unsorted funding events")
    return result


def safe_source_error(exc: Exception):
    """Preserve useful HTTP status without secrets or raw response bodies."""
    info = {"exception": type(exc).__name__}
    if isinstance(exc, urllib.error.HTTPError):
        info["http_status"] = int(exc.code)
    return info


def add_funding(feat, first, last, *, mode="optional", archive_download=None, api_fetch=None):
    """Use verified official archive; API only when archive fails.

    Failure never fabricates missing series and does not replace missing values
    with zeros. The 'required' setting fails rather than silently skipping.
    """
    if mode not in ("optional", "off", "required"):
        raise ValueError("Invalid funding mode")
    if mode == "off":
        return feat.copy(), {"status": "disabled", "event_count": 0}
    errors = {}
    sources = (
        ("binance_vision_sha256", lambda: read_funding_archive(first, last, download=archive_download)),
        ("binance_futures_api", lambda: v551.funding_history(first, last, fetch=api_fetch)),
    )
    for name, get in sources:
        try:
            events = get()
            aligned, info = v551.enrich_funding(feat, events)
            if info.get("status") != "usable":
                errors[name] = {"reason": "insufficient_coverage", "coverage": info.get("coverage", 0)}
                continue
            info["source"] = name
            info["verified_archive_checksum"] = name == "binance_vision_sha256"
            info["candidate_sources_diagnostic"] = errors
            return aligned, info
        except (ValueError, KeyError, IndexError, TypeError, TimeoutError, urllib.error.URLError,
                OSError, json.JSONDecodeError, zipfile.BadZipFile, UnicodeDecodeError, OverflowError) as exc:
            errors[name] = safe_source_error(exc)
    if mode == "required":
        raise RuntimeError(f"Funding source required but unavailable: {json.dumps(errors)}")
    return feat.copy(), {"status": "skipped_source_unavailable", "event_count": 0, "sources": errors}


def prepare_barriers(raw, feat, horizon: int, settings: Settings, tp_mult: float, sl_mult: float):
    """OHLC barriers, pessimistic same-bar fill, explicit net reward/risk gate.

    Entry = next minute OPEN, exits = TP/SL or last closed minute, never a fill.
    The SL (not TP) wins same-candle ambiguity. Gaps/fills are NOT modeled.
    """
    settings.validate()
    if horizon not in v55.HORIZONS or tp_mult <= 0 or sl_mult <= 0:
        raise ValueError("Invalid barrier parameters")
    base = v55.build_events(raw, feat, horizon, settings.costs)
    atr = feat.atr14_pct.to_numpy(dtype=float)
    cost_frac = settings.costs.roundtrip
    edge_frac = settings.costs.edge_bps / 10_000
    sl_frac = np.maximum(sl_mult * atr, 0.0008)
    # TP after cost must beat the stop after cost by at least specified R/R.
    tp_frac = np.maximum.reduce((tp_mult * atr,
                                 cost_frac + edge_frac + np.zeros(len(atr)),
                                 cost_frac + settings.minimum_reward_risk * (sl_frac + cost_frac)))
    good = base["valid"] & np.isfinite(tp_frac) & np.isfinite(sl_frac) & (sl_frac <= 0.02) & (tp_frac <= 0.035)
    entry = base["entry"].copy()
    n = len(feat)
    tp_l, sl_l = entry * (1 + tp_frac), entry * (1 - sl_frac)
    tp_s, sl_s = entry * (1 - tp_frac), entry * (1 + sl_frac)
    exit_l, exit_s = base["endpoint"].copy(), base["endpoint"].copy()
    event_l, event_s = np.zeros(n, dtype=np.int8), np.zeros(n, dtype=np.int8)
    k_l, k_s = np.full(n, horizon, dtype=np.int16), np.full(n, horizon, dtype=np.int16)
    active_l = good.copy()
    active_s = good.copy()
    raw_ts = raw.ts.to_numpy(dtype=np.int64)
    p = np.searchsorted(raw_ts, feat.ts.to_numpy(dtype=np.int64))
    hi = raw.high.to_numpy(dtype=float)
    lo = raw.low.to_numpy(dtype=float)
    for k in range(1, horizon + 1):
        cursor = np.minimum(p + k, len(raw_ts) - 1)
        current_hi, current_lo = hi[cursor], lo[cursor]
        sl_hit_l = active_l & (current_lo <= sl_l)
        tp_hit_l = active_l & ~sl_hit_l & (current_hi >= tp_l)
        sl_hit_s = active_s & (current_hi >= sl_s)
        tp_hit_s = active_s & ~sl_hit_s & (current_lo <= tp_s)
        event_l[sl_hit_l], event_l[tp_hit_l] = -1, 1
        event_s[sl_hit_s], event_s[tp_hit_s] = -1, 1
        exit_l[sl_hit_l], exit_l[tp_hit_l] = sl_l[sl_hit_l], tp_l[tp_hit_l]
        exit_s[sl_hit_s], exit_s[tp_hit_s] = sl_s[sl_hit_s], tp_s[tp_hit_s]
        k_l[sl_hit_l | tp_hit_l] = k
        k_s[sl_hit_s | tp_hit_s] = k
        active_l &= ~(sl_hit_l | tp_hit_l)
        active_s &= ~(sl_hit_s | tp_hit_s)
    with np.errstate(divide="ignore", invalid="ignore"):
        long_net = exit_l / entry - 1 - cost_frac
        short_net = (entry - exit_s) / entry - cost_frac
    long_net[~good], short_net[~good] = np.nan, np.nan
    return {"valid": good, "entry": entry, "tp_long": tp_l, "sl_long": sl_l,
            "tp_short": tp_s, "sl_short": sl_s, "long_exit": exit_l,
            "short_exit": exit_s, "long_side": event_l, "short_side": event_s,
            "long_k": k_l, "short_k": k_s, "long_net": long_net,
            "short_net": short_net, "tp_frac": tp_frac, "sl_frac": sl_frac,
            "nominal_reward_risk_min": settings.minimum_reward_risk}


def fourfold_split(times, valid, horizon, *, minimums=(2000, 450, 450, 1000)):
    """60% fit, 10% early-stop, 10% policy validation, 20% untouched test.

    Both fit and each neighboring segment have 16-minute purges, ensuring
    no event label at the end of one region reaches into the next fold.
    """
    ts = np.asarray(times, dtype=np.int64)
    valid = np.asarray(valid, dtype=bool)
    if len(ts) != len(valid) or len(ts) < sum(minimums):
        raise ValueError("Too few observations for independent fourfold split")
    if len(ts) > 1 and np.any(np.diff(ts) <= 0):
        raise ValueError("Unordered timestamps")
    edges = [int(ts[int(len(ts) * ratio)]) for ratio in (0.60, 0.70, 0.80)]
    gap = (max(v55.HORIZONS) + 1) * MS
    parts = [
        np.flatnonzero(valid & (ts < edges[0] - gap)),
        np.flatnonzero(valid & (ts >= edges[0]) & (ts < edges[1] - gap)),
        np.flatnonzero(valid & (ts >= edges[1]) & (ts < edges[2] - gap)),
        np.flatnonzero(valid & (ts >= edges[2])),
    ]
    if any(len(indices) < need for indices, need in zip(parts, minimums)):
        raise ValueError(f"Insufficient fourfold data: {[len(x) for x in parts]}")
    for older, newer in zip(parts, parts[1:]):
        if not ts[older[-1]] + (horizon + 1) * MS < ts[newer[0]]:
            raise AssertionError("Future event crosses fold boundary")
    return tuple(parts)


def fit_net_regressor(train_x, train_y, early_x, early_y, seed=552):
    """Predict directly in NET bps; early-stopping and policy folds separate."""
    import lightgbm as lgb
    model = lgb.LGBMRegressor(n_estimators=240, learning_rate=0.045,
                             max_depth=5, num_leaves=15, min_child_samples=120,
                             colsample_bytree=0.85, reg_lambda=8,
                             random_state=seed, n_jobs=2, verbosity=-1)
    model.fit(train_x, train_y, eval_set=[(early_x, early_y)],
              eval_metric="l2", callbacks=[lgb.early_stopping(25, verbose=False)])
    return model


def cost_aware_decisions(pred_long_bps, pred_short_bps, min_expected_bps, margin_bps, regime=None, frame=None):
    """Probability-free expected NET return threshold; nonfinite => BEKLE."""
    pl = np.asarray(pred_long_bps, dtype=float)
    ps = np.asarray(pred_short_bps, dtype=float)
    if pl.shape != ps.shape or not math.isfinite(min_expected_bps) or min_expected_bps < 0 or margin_bps < 0:
        raise ValueError("Invalid policy inputs")
    direction = np.where(pl > ps, 2, 0).astype(np.int8)
    strongest = np.maximum(pl, ps)
    difference = abs(pl - ps)
    eligible = np.isfinite(pl) & np.isfinite(ps) & (strongest > min_expected_bps) & (difference >= margin_bps)
    if regime and regime != "all":
        if frame is None or len(frame) != len(pl):
            raise ValueError("Regime policies require aligned feature frame")
        if regime == "trend_only":
            eligible &= frame.regime_trend.to_numpy(dtype=float) == 1
        elif regime == "avoid_high_vol":
            eligible &= frame.regime_high_vol.to_numpy(dtype=float) == 0
        else:
            raise ValueError("Unsupported regime policy")
    return np.where(eligible, direction, 1).astype(np.int8)


def score_and_gate(metrics, settings):
    """Use validation net bps, PF and drawdown, never classification accuracy."""
    count = metrics["trades"]
    average = metrics["average_net_bps"]
    pf = metrics["profit_factor"]
    dd = metrics["maximum_drawdown_pct"]
    enough = count >= settings.min_validation_trades
    passed = (enough and average is not None and average > settings.minimum_validation_mean_net_bps
              and pf is not None and pf > settings.minimum_validation_profit_factor
              and dd <= settings.maximum_validation_drawdown_pct)
    rank = -1e8 if average is None or not enough else float(average) * min(2., (count / 60) ** 0.5) - 0.015 * dd
    return rank, bool(passed)


def expected_opportunities(events, idx, min_edge_bps):
    """Post-hoc direction labels solely for diagnostics, not model training."""
    long_good = events["long_net"][idx] * 10_000 > min_edge_bps
    short_good = events["short_net"][idx] * 10_000 > min_edge_bps
    return np.where(long_good & ~short_good, 2, np.where(short_good & ~long_good, 0, 1)).astype(np.int8)


def per_regime_metrics(rows):
    groups = {}
    for row in rows:
        for label in (row.get("market_regime", "UNKNOWN"), row.get("direction", "UNKNOWN")):
            groups.setdefault("regime:" + label if label in ("TREND", "RANGE", "MIXED", "HIGH_VOL", "UNKNOWN") else "direction:" + label, []).append(float(row["net_bps_after_cost"]))
    summary = {}
    for key, values in groups.items():
        pos = sum(max(0, v) for v in values)
        neg = sum(min(0, v) for v in values)
        summary[key] = {"trades": len(values), "win_rate_pct": round(100 * sum(v > 0 for v in values) / len(values), 2),
                        "average_net_bps": round(float(np.mean(values)), 3),
                        "sum_net_bps": round(float(sum(values)), 3),
                        "profit_factor": round(pos / -neg, 4) if neg < 0 else None}
    return summary


def cost_sensitivity(rows, baseline_roundtrip_bps, scenarios=(12., 24., 40.)):
    """Re-price SAME test fills at assumed costs, never optimize on TEST."""
    out = {}
    for cost in scenarios:
        nets = [float(x["net_bps_after_cost"]) + baseline_roundtrip_bps - cost for x in rows]
        out[f"roundtrip_{cost:g}bps"] = {
            "assumed_roundtrip_bps": cost, "trades": len(nets),
            "average_net_bps": round(float(np.mean(nets)), 4) if nets else None,
            "sum_net_bps": round(float(sum(nets)), 4),
            "hypothetical_compound_pct": round(100 * (math.prod(max(1e-9, 1 + n / 10_000) for n in nets) - 1), 3) if nets else 0.0,
        }
    return out


def research(raw, feat, base_cols, *, settings=Settings(), funding_usable=False,
             barriers=BARRIER_CONFIGS, horizons=v55.HORIZONS):
    settings.validate()
    groups_all = v551.feature_groups(base_cols, funding_usable)
    groups = {name: columns for name, columns in groups_all.items() if name in FEATURE_GROUPS}
    if len(feat) < 7000:
        raise ValueError("Too few feature rows")
    feature_ts = feat.ts.to_numpy(dtype=np.int64)
    candidates = []
    best = None
    models_trained = 0
    for horizon in horizons:
        for barrier_name, tp, sl in barriers:
            ev = prepare_barriers(raw, feat, horizon, settings, tp, sl)
            for group_name, cols in groups.items():
                X = feat[cols].astype(np.float32)
                good = ev["valid"] & np.isfinite(X.to_numpy()).all(axis=1)
                folds = fourfold_split(feature_ts, good, horizon)
                tr, early, policy, test = folds
                regressor = {}
                for side in ("long", "short"):
                    targets_bps = ev[f"{side}_net"] * 10_000
                    if not np.isfinite(targets_bps[tr]).all() or not np.isfinite(targets_bps[early]).all():
                        raise ValueError("Nonfinite regression label")
                    m = fit_net_regressor(X.iloc[tr], targets_bps[tr], X.iloc[early], targets_bps[early],
                                          seed=settings.seed + (0 if side == "long" else 1))
                    regressor[side] = m
                    models_trained += 1
                pl = regressor["long"].predict(X.iloc[policy])
                ps = regressor["short"].predict(X.iloc[policy])
                policy_feat = feat.iloc[policy]
                winning_policy = None
                for minimum in THRESHOLDS_BPS:
                    for margin in MARGINS_BPS:
                        for regime in REGIME_POLICIES:
                            actions = cost_aware_decisions(pl, ps, minimum, margin, regime, policy_feat)
                            stats, _ = v55.backtest(policy, actions, feat, ev, settings.costs)
                            rank, passed = score_and_gate(stats, settings)
                            if winning_policy is None or rank > winning_policy[0]:
                                winning_policy = (rank, passed, minimum, margin, regime, stats)
                rank, passed, minimum, margin, regime, metrics = winning_policy
                meta = {"feature_group": group_name, "feature_count": len(cols),
                        "horizon_minutes": horizon, "barrier": barrier_name,
                        "tp_atr_multiplier": tp, "sl_atr_multiplier": sl,
                        "threshold_net_bps": minimum, "margin_bps": margin,
                        "regime_policy": regime, "validation_gate_passed": passed,
                        "validation_trade_metrics": metrics,
                        "fit_samples": len(tr), "early_stop_samples": len(early),
                        "policy_validation_samples": len(policy), "test_reserved_samples": len(test)}
                candidates.append(meta)
                # Prefer cost-profitable candidates. If none pass, retain best
                # diagnostics but do NOT permit live or shadow trading signals.
                key = (int(passed), float(rank))
                if best is None or key > best[0]:
                    best = (key, meta, regressor, ev, cols, test)
    if best is None:
        raise ValueError("No research model candidates")
    _, selected, regressors, events, cols, te = best
    Xte = feat[cols].astype(np.float32).iloc[te]
    # HOLDOUT is accessed only now, once, for validation-selected candidate.
    long_predictions = regressors["long"].predict(Xte)
    short_predictions = regressors["short"].predict(Xte)
    choices = cost_aware_decisions(long_predictions, short_predictions,
                                    selected["threshold_net_bps"], selected["margin_bps"],
                                    selected["regime_policy"], feat.iloc[te])
    metrics, trade_rows = v55.backtest(te, choices, feat, events, settings.costs)
    opportunities = expected_opportunities(events, te, settings.costs.edge_bps)
    classification = v55.fmetrics(opportunities, choices)
    baseline = v55.fmetrics(opportunities, np.ones(len(te), dtype=np.int8))
    iso_lookup = {}
    for j, ix in enumerate(te):
        iso_lookup[v55.iso(feature_ts[ix] + MS)] = (j, int(ix))
    for row in trade_rows:
        local, i = iso_lookup[row["signal_close_utc"]]
        reg = ("HIGH_VOL" if feat.regime_high_vol.iloc[i] == 1 else
               "TREND" if feat.regime_trend.iloc[i] == 1 else
               "RANGE" if feat.regime_range.iloc[i] == 1 else "MIXED")
        row.update(expected_long_net_bps=round(float(long_predictions[local]), 3),
                   expected_short_net_bps=round(float(short_predictions[local]), 3),
                   market_regime=reg, selected_barrier=selected["barrier"],
                   regression_output_is_not_calibrated_probability=True)
    gate_passed = bool(selected["validation_gate_passed"])
    final = {
        "status": "validation_gate_passed_research_only" if gate_passed else "rejected_validation_gate_research_only",
        "selected_config": selected,
        "validation_trade_metrics": selected["validation_trade_metrics"],
        "heldout_test_trade_metrics_shadow_only": metrics,
        "heldout_test_diagnostic_classification": classification,
        "heldout_test_always_wait_baseline": baseline,
        "heldout_test_actions": {v55.CLASS_NAMES[k]: int(np.sum(choices == k)) for k in (0, 1, 2)},
        "regime_test_metrics": per_regime_metrics(trade_rows),
        "cost_sensitivity": cost_sensitivity(trade_rows, 10_000 * settings.costs.roundtrip),
        "candidate_comparisons_validation_only": candidates,
        "model_pair_count": models_trained // 2,
        "fitted_regressors": models_trained,
        "split_counts": {"fit": selected["fit_samples"], "early_stopping": selected["early_stop_samples"],
                         "policy_validation": selected["policy_validation_samples"], "test": len(te)},
        "selection_uses_heldout_test": False,
        "all_test_statistics_for_research_only": True,
        "validation_pass_does_not_approve_live_deployment": True,
        "holdout_is_new_only_within_this_run": True,
        "previous_v551_research_already_examined_part_of_same_historical_period": True,
        "fresh_forward_period_needed_before_live_approval": True,
        "cost_gate_live_signal_blocked": True,
        "model_ready_for_live": False,
        "live_inference_enabled": False,
        "orders_enabled": False,
    }
    return final, regressors, trade_rows


def run(first, last, outdir, *, settings=Settings(), download=v54.read_month,
        funding_mode="optional", archive_download=None, api_fetch=None,
        research_fn=research):
    settings.validate()
    months = v54.month_range(first, last)
    raw = v54.historical_frame(months, download=download)
    feat, cols = v54.features_targets(raw)
    feat = v551.enrich_cvd_regime(raw, feat)
    feat, funding_info = add_funding(feat, first, last, mode=funding_mode,
                                    archive_download=archive_download, api_fetch=api_fetch)
    funding_usable = funding_info["status"] == "usable"
    details, regressors, rows = research_fn(raw, feat, cols, settings=settings, funding_usable=funding_usable)
    result = {
        "engine": "TiMi ML v5.5.2 — net-return regression, barrier/risk & regime research",
        "schema": SCHEMA, "symbol": "BTCUSDT", "market": "Binance Spot",
        "months": months, "raw_closed_1m_candles": len(raw), "eligible_feature_rows": len(feat),
        "funding": funding_info, "historical_source": "Binance Spot 1m monthly archives with SHA256 verification",
        "feature_sources": "v5.4 features + v5.5.1 CVD/rejim + optional verified settled funding",
        "fee_bps_per_side": settings.costs.fee_bps_side,
        "slippage_bps_per_side": settings.costs.slippage_bps_side,
        "assumed_roundtrip_bps": settings.costs.roundtrip * 10_000,
        "minimum_net_reward_risk": settings.minimum_reward_risk,
        "hypothetical_entry": "next 1m bar open after candle closes",
        "tp_sl_same_bar": "SL wins in same-bar tie; ignores slippage from gap/spread changes",
        "spot_short_execution_and_borrow_costs_not_modeled": True,
        "funding_is_feature_not_spot_cash_flow": True,
        "no_historical_orderbook": True,
        "no_future_in_features": True,
        "not_a_profit_guarantee": True,
        "not_actual_executions": True,
        **details,
    }
    out = Path(outdir)
    out.mkdir(parents=True, exist_ok=True)
    (out / "report.json").write_text(json.dumps(result, ensure_ascii=False, indent=2, allow_nan=False), encoding="utf-8")
    (out / "regime_report.json").write_text(json.dumps(details.get("regime_test_metrics", {}), ensure_ascii=False, indent=2), encoding="utf-8")
    if regressors:
        for side, m in regressors.items():
            m.booster_.save_model(str(out / f"model_{side.upper()}_RESEARCH_ONLY.txt"),
                                  num_iteration=getattr(m, "best_iteration_", None))
    if rows:
        with (out / "test_signal_references.csv").open("w", encoding="utf-8", newline="") as f:
            writer = csv.DictWriter(f, fieldnames=list(rows[0]))
            writer.writeheader()
            writer.writerows(rows)
    print("TiMi ML v5.5.2", result["status"], "| funding:", funding_info.get("status"),
          "| validation gate:", details["selected_config"]["validation_gate_passed"], flush=True)
    print("Research only; no orders or model serving.")
    return result


def main():
    parser = argparse.ArgumentParser(description="TiMi ML v5.5.2 research-only cost-aware analysis")
    parser.add_argument("--from-month", default="2026-05")
    parser.add_argument("--to-month", default="2026-10")
    parser.add_argument("--funding-mode", choices=("optional", "off", "required"), default="optional")
    parser.add_argument("--output", default="timi_v552_artifacts")
    parser.add_argument("--fee-bps", type=float, default=10.0)
    parser.add_argument("--slippage-bps", type=float, default=2.0)
    parser.add_argument("--min-edge-bps", type=float, default=2.0)
    parser.add_argument("--min-reward-risk", type=float, default=1.25)
    args = parser.parse_args()
    settings = Settings(costs=v55.Costs(args.fee_bps, args.slippage_bps, args.min_edge_bps),
                        minimum_reward_risk=args.min_reward_risk)
    run(args.from_month, args.to_month, args.output, settings=settings, funding_mode=args.funding_mode)


if __name__ == "__main__":
    main()
