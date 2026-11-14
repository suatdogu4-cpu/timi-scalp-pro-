"""TiMi ML v5.5.4 — strict offline BTCUSDT 1/3/5-minute scalping research.

Developer: Suat DOĞU. Read-only, no orders, no live inference.
Feature engineering + cost-aware triple barriers + direction-specific TP
classifiers + honest, chronological selection + a filter rejection funnel.
Older published evaluation period has already been inspected repeatedly:
any historical scores are EXPLORATORY, not a clean new holdout.
"""
from __future__ import annotations
import argparse
import csv
import json
import math
from dataclasses import dataclass
from pathlib import Path

import numpy as np
import pandas as pd

import timi_ml_v54_history_train as v54
import timi_ml_v55_research as v55
import timi_ml_v551_research as v551
import timi_ml_v553_scalp as v553

MS = v54.MS
HORIZONS = (1, 3, 5)
# Controlled predeclared experiment; no hyperparameter tuning on final tests.
BARRIERS = (("atr_1p5_0p5", 1.5, 0.5), ("atr_2p0_0p7", 2.0, 0.7))
THRESHOLDS = ((0.60, 0.), (0.70, 2.), (0.80, 4.))  # min TP probability; min conservative expected net bps
SCHEMA = "timi-v554-scalp-feature-label-funnel-v1"
EXTRA_COLS = (
    "rolling_vwap5_pct", "rolling_vwap15_pct", "rolling_vwap60_pct", "session_vwap_pct",
    "logret1", "logret3", "logret5", "logret_lag1", "logret_lag2", "logret_lag3",
    "ema8_21_pct", "bb_bandwidth20", "rsi14_delta3", "volume_z20", "volume_change3",
    "taker_delta_ratio5", "trade_size_quote20", "illiquidity_proxy20",
)


@dataclass(frozen=True)
class Config:
    costs: v55.Costs = v55.Costs()
    min_validation_trades: int = 25
    min_validation_net_bps: float = 2.0
    min_validation_profit_factor: float = 1.10
    max_validation_drawdown_pct: float = 12.0
    max_fit_rows: int = 90_000

    def check(self):
        self.costs.validate()
        if self.min_validation_trades < 10 or self.max_fit_rows < 1000:
            raise ValueError("Invalid minimum sample settings")
        if not math.isfinite(self.min_validation_net_bps) or not math.isfinite(self.min_validation_profit_factor):
            raise ValueError("Invalid validation requirements")
        if self.min_validation_profit_factor < 1.0 or not 0 <= self.max_validation_drawdown_pct <= 100:
            raise ValueError("Invalid performance requirements")


def additional_features(raw: pd.DataFrame, frame: pd.DataFrame):
    """Only closed 1m observations, reset at gaps and at UTC session for session VWAP.

    The illiquidity feature is a price-impact *proxy*, not historical depth.
    """
    segments = []
    for _, g in raw.groupby("segment", sort=False):
        g = g.copy()
        close, high, low = (g[k].astype(float) for k in ("close", "high", "low"))
        vol = g.volume.astype(float)
        qv = g.quote_volume.astype(float)
        buys = g.taker_buy_volume.astype(float)
        typical = (high + low + close) / 3.
        pv = typical * vol
        for w in (5, 15, 60):
            denominator = vol.rolling(w, min_periods=w).sum().replace(0, np.nan)
            vw = pv.rolling(w, min_periods=w).sum() / denominator
            g[f"rolling_vwap{w}_pct"] = close / vw - 1.
        # Time is UTC, and the value resets at every calendar-day boundary.
        day = g.ts // (86_400_000)
        session_vw = pv.groupby(day).cumsum() / vol.groupby(day).cumsum().replace(0, np.nan)
        g["session_vwap_pct"] = close / session_vw - 1.
        logr = np.log(close / close.shift(1))
        for n in (1, 3, 5):
            g[f"logret{n}"] = np.log(close / close.shift(n))
        for n in (1, 2, 3):
            g[f"logret_lag{n}"] = logr.shift(n)
        ema8 = close.ewm(span=8, adjust=False, min_periods=8).mean()
        ema21 = close.ewm(span=21, adjust=False, min_periods=21).mean()
        g["ema8_21_pct"] = (ema8 - ema21) / close
        center = close.rolling(20, min_periods=20).mean()
        std = close.rolling(20, min_periods=20).std(ddof=0)
        g["bb_bandwidth20"] = 4 * std / center.replace(0, np.nan)
        # Use the full v54 RSI series via exact timestamps, after joining.
        vol_mean = vol.rolling(20, min_periods=20).mean()
        vol_std = vol.rolling(20, min_periods=20).std(ddof=0)
        g["volume_z20"] = (vol - vol_mean) / vol_std.replace(0, np.nan)
        g["volume_change3"] = np.log1p(vol) - np.log1p(vol.shift(3))
        g["taker_delta_ratio5"] = (2*buys-vol).rolling(5, min_periods=5).sum() / vol.rolling(5, min_periods=5).sum().replace(0, np.nan)
        g["trade_size_quote20"] = qv.rolling(20, min_periods=20).sum() / g.trades.rolling(20, min_periods=20).sum().replace(0, np.nan)
        g["illiquidity_proxy20"] = ((logr.abs() / qv.replace(0, np.nan)).rolling(20, min_periods=20).mean() * 1e7)
        segments.append(g[["ts", *(x for x in EXTRA_COLS if x != "rsi14_delta3")]])
    ext = pd.concat(segments, ignore_index=True)
    if ext.ts.duplicated().any() or frame.ts.duplicated().any():
        raise ValueError("Duplicate feature timestamps")
    result = frame.merge(ext, on="ts", how="left", validate="one_to_one", sort=False)
    # No crossing source gaps: rsi delta computed by each segment, not whole frame.
    delta = []
    if "segment" in result:
        for _, g in result.groupby("segment", sort=False):
            delta.append(g.rsi14.diff(3))
        result["rsi14_delta3"] = pd.concat(delta).sort_index()
    else:
        # Explicitly map segment from raw to avoid merging across irregular minutes.
        src_segment = raw.set_index("ts").segment.reindex(result.ts).to_numpy()
        result["_segment_tmp"] = src_segment
        result["rsi14_delta3"] = result.groupby("_segment_tmp", sort=False).rsi14.diff(3)
        result = result.drop(columns=["_segment_tmp"])
    result[list(EXTRA_COLS)] = result[list(EXTRA_COLS)].replace([np.inf, -np.inf], np.nan)
    result = result.dropna(subset=list(EXTRA_COLS)).reset_index(drop=True)
    if result.empty:
        raise ValueError("No complete features")
    return result


def close_aligned(ts, horizon):
    """1m candle open time + 1m must be a true 3m/5m UTC close."""
    if horizon not in HORIZONS:
        raise ValueError("Only 1/3/5 minute scalping")
    t = np.asarray(ts, dtype=np.int64)
    return ((t // MS + 1) % horizon == 0)


def sample_funnel(p_long, p_short, events, idx, *, min_probability, min_expected_bps):
    """Pretrade rejection reasons in priority order; no future outcomes read here."""
    pl, ps = np.asarray(p_long, float), np.asarray(p_short, float)
    if len(idx) != len(pl) or len(idx) != len(ps):
        raise ValueError("Wrong probability shape")
    cost_bps = 10_000 * (v55.Costs().roundtrip)  # overridden per event below
    if "cost_bps" in events:
        cost_bps = float(events["cost_bps"])
    pmax = np.maximum(pl, ps)
    decision = np.where(pl >= ps, 2, 0).astype(np.int8)
    chosen_tp = events["tp_frac"][idx] * 10_000
    chosen_sl = events["sl_frac"][idx] * 10_000
    # Conservatively treat every non-TP path as a full SL and charge full costs.
    expected = pmax * chosen_tp - (1 - pmax) * chosen_sl - cost_bps
    reason = np.full(len(idx), "SIGNAL", dtype=object)
    invalid = (~np.isfinite(pl) | ~np.isfinite(ps) | ~np.isfinite(expected) |
               (~events["valid"][idx]))
    reason[invalid] = "INVALID_FEATURE_OR_BARRIER"
    below_p = (reason == "SIGNAL") & (pmax < min_probability)
    reason[below_p] = "BELOW_TP_PROBABILITY"
    below_edge = (reason == "SIGNAL") & (expected <= min_expected_bps)
    reason[below_edge] = "COST_OR_EDGE_FAILURE"
    decision[reason != "SIGNAL"] = 1
    return decision, expected, reason


def funnel_summary(reasons):
    return {str(k): int(v) for k, v in zip(*np.unique(np.asarray(reasons, dtype=object), return_counts=True))}


def validation_gate(metrics, config: Config):
    cnt = int(metrics["trades"])
    mean = metrics.get("average_net_bps")
    pf = metrics.get("profit_factor")
    dd = metrics.get("maximum_drawdown_pct", 100.)
    passed = (cnt >= config.min_validation_trades and mean is not None and
              mean > config.min_validation_net_bps and pf is not None and
              pf >= config.min_validation_profit_factor and dd <= config.max_validation_drawdown_pct)
    # Avoid selection by tiny samples or empty-behavior models.
    score = -1e9 if cnt < config.min_validation_trades or mean is None else float(mean) * min(1., cnt/100.) - .04 * dd
    return bool(passed), float(score)


def train_tp_model(Xtrain, ytrain, Xearly, yearly, *, seed):
    """Only the fit/early folds affect the model. No policy/test tuning."""
    if len(np.unique(ytrain)) < 2:
        return None  # no invented training labels
    import lightgbm as lgb
    model = lgb.LGBMClassifier(objective="binary", n_estimators=180,
        learning_rate=.045, max_depth=4, num_leaves=11, min_child_samples=100,
        colsample_bytree=.85, reg_lambda=10., random_state=seed,
        n_jobs=2, verbosity=-1)
    model.fit(Xtrain, ytrain, eval_set=[(Xearly, yearly)], eval_metric="binary_logloss",
              callbacks=[lgb.early_stopping(20, verbose=False)])
    return model


def safe_prob(model, X):
    if model is None:
        return np.zeros(len(X), dtype=float)
    return np.asarray(model.predict_proba(X)[:, 1], dtype=float)



def movement_diagnostics(raw, feat, horizon, indices, costs):
    """Post-hoc diagnostic: how often even a perfect entry covers round-trip costs.

    Not a signal and never used to pick a candidate. Fixed endpoint ignores
    intrabar TP/SL and therefore cannot establish profitability.
    """
    ref = v553.scalp_entry_endpoint(raw, feat, horizon, costs)
    ix = np.asarray(indices, dtype=int)
    mask = ref["valid"][ix]
    ret_bps = np.abs(ref["endpoint"][ix][mask] / ref["entry"][ix][mask] - 1) * 10000
    cost = 10000 * costs.roundtrip
    return {"sample_count": len(ret_bps), "abs_endpoint_move_bps": quantiles(ret_bps),
            "pct_absolute_move_exceeds_roundtrip_cost": round(100 * float(np.mean(ret_bps > cost)), 3) if len(ret_bps) else None,
            "pct_absolute_move_exceeds_cost_plus_edge": round(100 * float(np.mean(ret_bps > cost + costs.edge_bps)), 3) if len(ret_bps) else None,
            "roundtrip_cost_bps": cost}


def trade_quality_metrics(rows):
    """Exploratory non-annualized trade return Sharpe. None with too few trades."""
    vals = np.asarray([float(x["net_bps_after_cost"]) for x in rows], dtype=float)
    if len(vals) < 30 or np.std(vals, ddof=1) <= 1e-12:
        return {"per_trade_sharpe_nonannualized": None, "reason": "fewer_than_30_trades_or_zero_variation"}
    return {"per_trade_sharpe_nonannualized": round(float(np.mean(vals)/np.std(vals,ddof=1)), 4),
            "reason": "per_trade_statistic_not_annualized_portfolio_sharpe"}


def quantify_opportunities(events, indices):
    ix = np.asarray(indices, int)
    good = events["valid"][ix]
    return {"eligible_bars": int(len(ix)), "valid_barriers": int(good.sum()),
            "long_tp_first": int(np.sum((events["long_side"][ix] == 1) & good)),
            "short_tp_first": int(np.sum((events["short_side"][ix] == 1) & good)),
            "both_tp_first": int(np.sum((events["long_side"][ix] == 1) & (events["short_side"][ix] == 1) & good))}


def research(raw, feat, base_cols, *, config=Config(), funding_usable=False,
             barriers=BARRIERS, horizons=HORIZONS):
    config.check()
    allcols = {"baseline": list(base_cols),
               "engineered_cvd_regime": list(dict.fromkeys(list(base_cols) + list(v551.CVD_COLS) + list(v551.REGIME_COLS) + list(EXTRA_COLS)))}
    if funding_usable:
        allcols["engineered_plus_settled_funding"] = allcols["engineered_cvd_regime"] + list(v551.FUNDING_COLS)
    candidate_info = []
    best = None
    counts = {str(h): {"candidates": 0, "validation_passed": 0} for h in HORIZONS}
    estimated_models = 0
    horizon_movement = {}
    times = feat.ts.to_numpy(dtype=np.int64)
    min_count = (1000, 300, 300, 500)  # scale to 3m/5m decisions
    for horizon in horizons:
        alignment = close_aligned(times, horizon)
        for barrier_name, tp, sl in barriers:
            settings = v553.Settings(costs=config.costs, minimum_reward_risk=1.25)
            events = v553.prepare_barriers(raw, feat, horizon, settings, tp, sl)
            events["cost_bps"] = 10_000 * config.costs.roundtrip
            for group, columns in allcols.items():
                valid = events["valid"] & alignment & np.isfinite(feat[columns].to_numpy(dtype=float)).all(axis=1)
                folds = v553.fourfold_split(times, valid, horizon, minimums=min_count)
                tr, early, policy, test = folds
                if str(horizon) not in horizon_movement:
                    horizon_movement[str(horizon)] = movement_diagnostics(raw, feat, horizon, policy, config.costs)
                if len(tr) > config.max_fit_rows:
                    stride = math.ceil(len(tr) / config.max_fit_rows)
                    tr = tr[::stride]
                models = {}
                X = feat[columns].astype(np.float32)
                for side in ("long", "short"):
                    labels = (events[f"{side}_side"] == 1).astype(np.int8)
                    models[side] = train_tp_model(X.iloc[tr], labels[tr], X.iloc[early], labels[early],
                                                seed=554 if side == "long" else 555)
                    estimated_models += int(models[side] is not None)
                pl = safe_prob(models["long"], X.iloc[policy])
                ps = safe_prob(models["short"], X.iloc[policy])
                local_best = None
                for min_p, min_edge in THRESHOLDS:
                    decisions, exp, reasons = sample_funnel(pl, ps, events, policy,
                        min_probability=min_p, min_expected_bps=min_edge)
                    stats, _ = v55.backtest(policy, decisions, feat, events, config.costs)
                    passed, score = validation_gate(stats, config)
                    key = (int(passed), score)
                    if local_best is None or key > local_best[0]:
                        local_best = (key, min_p, min_edge, stats, funnel_summary(reasons),
                                      pd.Series(exp).replace([np.inf, -np.inf], np.nan).quantile([.05,.5,.95]).to_dict())
                key, min_p, min_edge, stats, funnel, exp_q = local_best
                summary = {"horizon_minutes": horizon, "feature_group": group,
                           "feature_count": len(columns), "barrier": barrier_name,
                           "train_rows": len(tr), "early_rows": len(early), "validation_rows": len(policy),
                           "test_rows_reserved": len(test), "validation_gate_passed": bool(key[0]),
                           "validation_trades": stats, "validation_funnel": funnel,
                           "expected_net_bps_quantiles_on_validation": {str(k): float(v) for k,v in exp_q.items()},
                           "tp_first_label_counts_fit": quantify_opportunities(events, tr),
                           "min_tp_probability": min_p, "min_conservative_expected_bps": min_edge}
                candidate_info.append(summary)
                counts[str(horizon)]["candidates"] += 1
                counts[str(horizon)]["validation_passed"] += int(key[0])
                if best is None or key > best[0]:
                    best = (key, summary, models, events, columns, test)
    if best is None:
        raise ValueError("No valid training candidates")
    key, selected, models, events, cols, test = best
    # Access the historical diagnostic partition only AFTER all validation choices are frozen.
    Xte = feat[cols].astype(np.float32).iloc[test]
    pl, ps = safe_prob(models["long"], Xte), safe_prob(models["short"], Xte)
    direction, expected, reasons = sample_funnel(pl, ps, events, test,
        min_probability=selected["min_tp_probability"],
        min_expected_bps=selected["min_conservative_expected_bps"])
    metrics, trades = v55.backtest(test, direction, feat, events, config.costs)
    prob_summary = {"tp_probability_long_quantiles": quantiles(pl),
                    "tp_probability_short_quantiles": quantiles(ps),
                    "conservative_expected_bps_quantiles": quantiles(expected)}
    for trade in trades:
        trade["not_actual_order"] = True
        trade["historic_period_reused_not_clean_test"] = True
    return ({"status": "EXPLORATORY_VALIDATION_PASSED_NO_LIVE_APPROVAL" if selected["validation_gate_passed"] else "NO_VALIDATED_EDGE_BE_KLE",
            "scalp_timeframes_minutes": list(HORIZONS),
            "candidate_count": len(candidate_info), "trained_classifiers": estimated_models,
            "validation_per_horizon": counts, "validation_price_movement_diagnostics": horizon_movement,
            "validation_selected_config": selected,
            "candidate_comparisons_validation_only": candidate_info,
            "historical_diagnostic_not_fresh_holdout": {"trade_metrics": metrics,
                "decisions": {"AL": int(np.sum(direction == 2)), "SAT": int(np.sum(direction == 0)), "BEKLE": int(np.sum(direction == 1))},
                "funnel": funnel_summary(reasons), "probability_distributions": prob_summary,
                "label_opportunities": quantify_opportunities(events, test),
                "trade_quality": trade_quality_metrics(trades),
                "cost_sensitivity_fixed_trades": v553.cost_sensitivity(trades, 10000 * config.costs.roundtrip)},
            "no_historical_orderbook_imbalance": True,
            "no_sharpe_claim_for_no_trades": True,
            "previous_models_already_inspected_2026_05_to_2026_10": True,
            "requires_new_forward_period_for_clean_validation": True,
            "live_inference_enabled": False, "orders_enabled": False,
            "model_ready_for_live": False}, models, trades)


def quantiles(x):
    a = np.asarray(x, float)
    a = a[np.isfinite(a)]
    if len(a) == 0:
        return None
    return {str(k): round(float(v),4) for k,v in zip((5, 50, 95), np.percentile(a, (5,50,95)))}


def run(first, last, outdir, *, funding_mode="optional", config=Config(), download=v54.read_month,
        funding_download=None, api_fetch=None):
    config.check()
    months = v54.month_range(first, last)
    raw = v54.historical_frame(months, download=download)
    frame, cols = v54.features_targets(raw)
    frame = v551.enrich_cvd_regime(raw, frame)
    frame = additional_features(raw, frame)
    frame, funding = v553.add_funding(frame, first, last, mode=funding_mode,
                                      archive_download=funding_download, api_fetch=api_fetch)
    results, models, rows = research(raw, frame, cols, config=config, funding_usable=funding["status"] == "usable")
    result = {"engine": "TiMi ML v5.5.4 — cost-aware triple-barrier TP confidence and feature diagnostics",
        "schema": SCHEMA, "symbol": "BTCUSDT", "market": "Binance Spot 1m",
        "months": months, "raw_closed_candles": len(raw), "feature_rows": len(frame),
        "base_features": len(cols), "new_engineered_features": len(EXTRA_COLS),
        "new_feature_names": list(EXTRA_COLS),
        "funding": funding, "source": "verified Binance Vision spot 1m + optional settled futures funding",
        "costs": {"fee_bps_per_side":config.costs.fee_bps_side,"slippage_bps_per_side":config.costs.slippage_bps_side,
                  "roundtrip_cost_bps":config.costs.roundtrip*10_000},
        "label": "side-specific TP first with SL-first same-candle tie; timeout is NOT a win",
        "prediction": "separate long/short TP probabilities; conservative expected return = p*TP - (1-p)*SL - costs",
        "decision_alignment": "1m close for 1m; UTC 3m and 5m candle closes only for 3m/5m",
        "feature_availability": "closed source candle only; no future OHLCV, no fake order-book history",
        "execution_limitations": "next 1m open is not guaranteed executable; spread/gaps and spot shorts may increase losses",
        "backtest_limitations": "OHLC intrabar path unknown, ties stop first; no tick-by-tick fills or market impact",
        "historical_period_not_new_out_of_sample": True,
        **results}
    p = Path(outdir)
    p.mkdir(parents=True, exist_ok=True)
    (p/"report.json").write_text(json.dumps(result,ensure_ascii=False,indent=2,allow_nan=False),encoding="utf-8")
    (p/"funnel.json").write_text(json.dumps(result["historical_diagnostic_not_fresh_holdout"]["funnel"],ensure_ascii=False,indent=2),encoding="utf-8")
    (p/"validation_candidates.json").write_text(json.dumps(results["candidate_comparisons_validation_only"],ensure_ascii=False,indent=2),encoding="utf-8")
    if rows:
        with (p/"historical_reference_trades.csv").open("w",newline="",encoding="utf-8") as fh:
            writer=csv.DictWriter(fh,fieldnames=rows[0].keys())
            writer.writeheader();writer.writerows(rows)
    for side, model in models.items():
        if model is not None:
            model.booster_.save_model(str(p/f"model_{side.upper()}_RESEARCH_ONLY.txt"))
    print("TiMi ML v5.5.4",result["status"],"| funding:",funding["status"],"| candidate:",len(results["candidate_comparisons_validation_only"]),flush=True)
    return result


def main():
    pa = argparse.ArgumentParser(description="TiMi ML v5.5.4 1/3/5m read-only scalping")
    pa.add_argument("--from-month",default="2026-05")
    pa.add_argument("--to-month",default="2026-10")
    pa.add_argument("--funding-mode",choices=("off","optional","required"),default="optional")
    pa.add_argument("--output",default="timi_v554_artifacts")
    pa.add_argument("--fee-bps",type=float,default=10)
    pa.add_argument("--slippage-bps",type=float,default=2)
    opts=pa.parse_args()
    run(opts.from_month,opts.to_month,opts.output,funding_mode=opts.funding_mode,
        config=Config(costs=v55.Costs(opts.fee_bps,opts.slippage_bps,2.)))


if __name__ == "__main__":
    main()
