"""TiMi ML v5.5.5 — regime-aware, calibrated meta-labeling for 1/3/5m scalping.

READ-ONLY HISTORICAL RESEARCH. No trading API, orders, live serving or DB writes.
Developer: Suat DOĞU.

Three predeclared, closed-candle strategy families generate candidate events.
LightGBM predicts the probability of a fee-adjusted TP-first outcome ONLY for
those events. Probabilities are calibrated on a chronologically distinct fold;
entry thresholds are chosen on the next fold; historical diagnostic period is
accessed exactly once after selection. This previously researched May-Oct 2026
period is NOT a new external holdout, regardless of how the code splits it.
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
import timi_ml_v554_scalp as v554

STRATEGIES = ("trend_pullback", "vwap_reversion", "volume_breakout")
HORIZONS = (1, 3, 5)
BARRIERS = (("atr_1p5_0p5", 1.5, 0.5), ("atr_2p0_0p7", 2.0, 0.7))
THRESHOLDS = ((0.60, 2.0), (0.70, 2.0), (0.80, 4.0))
SCHEMA = "timi-ml-v555-meta-calibration-fivefold-research-v1"


@dataclass(frozen=True)
class Config:
    costs: v55.Costs = v55.Costs()
    minimum_fit_events: int = 100
    minimum_calibration_events: int = 35
    minimum_policy_events: int = 30
    minimum_validation_trades: int = 20
    minimum_profit_factor: float = 1.10
    minimum_mean_net_bps: float = 2.0
    maximum_drawdown_pct: float = 12.0
    max_fit_events: int = 45_000

    def check(self):
        self.costs.validate()
        if any(n < 10 for n in (self.minimum_fit_events, self.minimum_calibration_events,
                                self.minimum_policy_events, self.minimum_validation_trades)):
            raise ValueError("Insufficient required sample counts")
        if self.max_fit_events < 200 or self.minimum_profit_factor < 1 or self.maximum_drawdown_pct < 0:
            raise ValueError("Invalid risk gates")


def trailing_extremes(raw, feat, lookback=20):
    """Prior closed bars only. No gap crossing; today's bar cannot define its own breakout."""
    parts = []
    for _, group in raw.groupby("segment", sort=False):
        lo = group.low.shift(1).rolling(lookback, min_periods=lookback).min()
        hi = group.high.shift(1).rolling(lookback, min_periods=lookback).max()
        parts.append(pd.DataFrame({"ts": group.ts, "prior_high20": hi, "prior_low20": lo}))
    look = pd.concat(parts, ignore_index=True)
    if look.ts.duplicated().any() or feat.ts.duplicated().any():
        raise ValueError("Duplicate candle timestamp")
    merged = feat[["ts"]].merge(look, on="ts", how="left", validate="one_to_one", sort=False)
    return merged["prior_high20"].to_numpy(float), merged["prior_low20"].to_numpy(float)


def strategy_candidates(raw, feat):
    """Return {family: direction array}; 2=AL,0=SAT,1=BEKLE.

    All filters look ONLY at closed 1m candle at time t, never the future outcome.
    Only trend/range labels from past data are used; no order-book history is invented.
    """
    n = len(feat)
    close = feat.ts.to_numpy(np.int64)
    raw_times = raw.ts.to_numpy(np.int64)
    p = np.searchsorted(raw_times, close)
    if (p >= len(raw_times)).any() or not np.array_equal(raw_times[p], close):
        raise ValueError("Feature/raw timestamps not exactly aligned")
    px = raw.close.to_numpy(float)[p]
    high20, low20 = trailing_extremes(raw, feat)
    ret = feat.return_1m.to_numpy(float)
    body = feat.body_pct.to_numpy(float)
    rsi = feat.rsi14.to_numpy(float)
    z = feat.bb20_z.to_numpy(float)
    em = feat.ema8_21_pct.to_numpy(float)
    vwap5 = feat.rolling_vwap5_pct.to_numpy(float)
    vwap15 = feat.rolling_vwap15_pct.to_numpy(float)
    volz = feat.volume_z20.to_numpy(float)
    cvd = feat.cvd_ratio_5.to_numpy(float)
    trend = feat.regime_trend.to_numpy(float) == 1
    ranging = feat.regime_range.to_numpy(float) == 1
    highvol = feat.regime_high_vol.to_numpy(float) == 1
    # A newly rising/falling bar after a modest EMA/VWAP pullback, in trend.
    tl = trend & ~highvol & (em > 0) & (vwap5 > -0.003) & (vwap5 < 0.002) & (ret > 0) & (rsi > 38) & (rsi < 69) & (cvd > -0.15)
    ts = trend & ~highvol & (em < 0) & (vwap5 < 0.003) & (vwap5 > -0.002) & (ret < 0) & (rsi > 31) & (rsi < 62) & (cvd < 0.15)
    # Reversion after extension and a reversal candle, only in range regime.
    rl = ranging & ~highvol & (z < -1.25) & (vwap15 < -0.001) & (body > 0) & (rsi < 47)
    rs = ranging & ~highvol & (z > 1.25) & (vwap15 > 0.001) & (body < 0) & (rsi > 53)
    # Breakout strictly beyond the high/low of PREVIOUS 20 candles.
    bl = (px > high20) & (volz > .65) & (ret > 0) & (cvd > .08) & ~highvol
    bs = (px < low20) & (volz > .65) & (ret < 0) & (cvd < -.08) & ~highvol
    result = {}
    for name, long_signal, short_signal in ((STRATEGIES[0], tl, ts),
                                            (STRATEGIES[1], rl, rs),
                                            (STRATEGIES[2], bl, bs)):
        if (long_signal & short_signal).any():
            raise ValueError("Ambiguous candidate sides")
        result[name] = np.where(long_signal, 2, np.where(short_signal, 0, 1)).astype(np.int8)
        if len(result[name]) != n:
            raise ValueError("Wrong signal length")
    return result


def fivefold_split(ts, good, horizon):
    """Fit 50%, early stop 10%, calibration 10%, policy 10%, diagnostic 20%.

    Folds are clock-based across ALL bars (not event-count based) and purged
    by 6 minutes, exceeding the maximum possible 5-minute outcome horizon.
    """
    ts = np.asarray(ts, dtype=np.int64)
    good = np.asarray(good, bool)
    if len(ts) != len(good) or len(ts) < 1000 or np.any(np.diff(ts) <= 0):
        raise ValueError("Invalid ordered folds")
    if horizon not in HORIZONS:
        raise ValueError("Invalid scalping horizon")
    cut = [int(ts[int(len(ts) * x)]) for x in (.50, .60, .70, .80)]
    purge = 6 * v54.MS
    result = (
        np.flatnonzero(good & (ts < cut[0] - purge)),
        np.flatnonzero(good & (ts >= cut[0]) & (ts < cut[1] - purge)),
        np.flatnonzero(good & (ts >= cut[1]) & (ts < cut[2] - purge)),
        np.flatnonzero(good & (ts >= cut[2]) & (ts < cut[3] - purge)),
        np.flatnonzero(good & (ts >= cut[3])),
    )
    for a,b in zip(result,result[1:]):
        if len(a) and len(b) and not ts[a[-1]] + (horizon+1)*v54.MS < ts[b[0]]:
            raise AssertionError("Future labels overlap chronological folds")
    return result


def tp_first_binary(events, direction):
    """Successful net-profitable TP-first event conditional on candidate direction."""
    direction = np.asarray(direction,np.int8)
    if not np.isin(direction,(0,1,2)).all():
        raise ValueError("Unexpected direction")
    is_long = direction == 2
    side = np.where(is_long, events["long_side"], events["short_side"])
    net = np.where(is_long, events["long_net"], events["short_net"])
    return (events["valid"] & (direction != 1) & (side == 1) & (net > 0)).astype(np.int8)


def make_event_features(feat, base_cols, directions, funding_usable):
    cols = list(dict.fromkeys([*base_cols, *v551.CVD_COLS, *v551.REGIME_COLS, *v554.EXTRA_COLS]))
    if funding_usable:
        cols.extend(v551.FUNDING_COLS)
    x = feat[cols].copy()
    x["candidate_side"] = np.where(directions == 2, 1., np.where(directions == 0, -1., 0.))
    return x.astype(np.float32), cols + ["candidate_side"]


def fit_model(X, y, Xe, ye, *, seed=555):
    """Never train on calibration/policy/diagnostic data."""
    import lightgbm as lgb
    if len(np.unique(y)) < 2 or len(np.unique(ye)) < 2 or min(np.bincount(y, minlength=2)) < 8:
        return None
    model = lgb.LGBMClassifier(objective="binary", n_estimators=160, learning_rate=.045,
                               max_depth=4, num_leaves=11, min_child_samples=35,
                               colsample_bytree=.85, reg_lambda=10., random_state=seed,
                               n_jobs=2, verbosity=-1)
    model.fit(X,y,eval_set=[(Xe,ye)],eval_metric="binary_logloss",
              callbacks=[lgb.early_stopping(18,verbose=False)])
    return model


def raw_probability(model, x):
    p = np.asarray(model.predict_proba(x)[:, 1],float)
    if not np.isfinite(p).all() or ((p < 0) | (p > 1)).any():
        raise ValueError("Nonfinite or invalid model probability")
    return p


def fit_calibrator(raw_prob, labels):
    """Isotonic calibrator trained ONLY on dedicated calibration fold.

    Insufficient calibration samples/class diversity returns None and results
    in a conservative rejected candidate, never a fake high confidence score.
    """
    from sklearn.isotonic import IsotonicRegression
    p = np.asarray(raw_prob, float)
    y = np.asarray(labels,np.int8)
    if (len(p) < 35 or len(p) != len(y) or not np.isfinite(p).all() or
        len(np.unique(y)) < 2 or np.sum(y == 1) < 8 or np.sum(y == 0) < 8):
        return None
    model = IsotonicRegression(y_min=0,y_max=1,out_of_bounds="clip")
    model.fit(p,y)
    return model


def calibration_scores(y, predicted):
    from sklearn.metrics import brier_score_loss, log_loss
    y = np.asarray(y,dtype=np.int8)
    p = np.asarray(predicted,float)
    if not len(y) or not np.isfinite(p).all():
        return None
    edges = np.linspace(0,1,6)
    bins = []
    ece = 0.
    for j in range(5):
        mask = (p >= edges[j]) & (p < edges[j+1] if j < 4 else p <= edges[j+1])
        if mask.sum():
            count = int(mask.sum())
            avg = float(p[mask].mean()); actual = float(y[mask].mean())
            ece += count / len(y) * abs(avg-actual)
            bins.append({"range": f"{edges[j]:.1f}-{edges[j+1]:.1f}", "n":count,
                         "mean_probability":round(avg,4), "actual_tp_rate":round(actual,4)})
    return {"n":len(y), "base_rate":round(float(y.mean()),5),
            "brier":round(float(brier_score_loss(y,p)),6),
            "log_loss":round(float(log_loss(y,np.clip(p,1e-6,1-1e-6),labels=[0,1])),6),
            "expected_calibration_error":round(ece,6),"bins":bins}


def decisions_from_probability(p, indices, events, directions, minimum_probability,
                               minimum_expected_bps, costs):
    p = np.asarray(p,float)
    indices = np.asarray(indices,dtype=int)
    if len(p) != len(indices):
        raise ValueError("Probability/index length mismatch")
    direction = np.asarray(directions,dtype=np.int8)[indices].copy()
    cost_bps = 10000 * costs.roundtrip
    tp = 10000 * events["tp_frac"][indices]
    sl = 10000 * events["sl_frac"][indices]
    expected = p*tp - (1-p)*sl - cost_bps
    reason = np.full(len(indices),"SIGNAL",dtype=object)
    invalid = ((~events["valid"][indices]) | (direction == 1) |
               (~np.isfinite(p)) | (~np.isfinite(expected)))
    reason[invalid] = "INVALID_OR_NO_CANDIDATE"
    reason[(reason == "SIGNAL") & (p < minimum_probability)] = "LOW_CALIBRATED_PROBABILITY"
    reason[(reason == "SIGNAL") & (expected <= minimum_expected_bps)] = "NO_NET_EDGE_AFTER_COST"
    direction[reason != "SIGNAL"] = 1
    return direction, expected, reason


def qualify(metrics, config):
    count = int(metrics["trades"])
    mean = metrics.get("average_net_bps")
    pf = metrics.get("profit_factor")
    dd = metrics.get("maximum_drawdown_pct",100.)
    ok = (count >= config.minimum_validation_trades and mean is not None and
          mean > config.minimum_mean_net_bps and pf is not None and
          pf >= config.minimum_profit_factor and dd <= config.maximum_drawdown_pct)
    score = -1e7 if count < config.minimum_validation_trades or mean is None else (
        float(mean) * min(1.4,math.sqrt(count/60.)) - .05 * dd)
    return bool(ok),float(score)


def comparison(raw, feat, base_cols, config=Config(), funding_usable=False,
               barriers=BARRIERS, horizon_choices=HORIZONS):
    """Model selection uses only POLICY fold. TEST opened once for selected entry."""
    config.check()
    if len(feat) < 6000:
        raise ValueError("Insufficient historical observations")
    candidates = strategy_candidates(raw,feat)
    ts = feat.ts.to_numpy(np.int64)
    summaries = []
    selected = None
    nmodels = 0
    experiment_counts = {}
    for horizon in horizon_choices:
        aligned = v554.close_aligned(ts,horizon)
        setting = v553.Settings(costs=config.costs,minimum_reward_risk=1.25)
        for barrier_name, tp, sl in barriers:
            events = v553.prepare_barriers(raw,feat,horizon,setting,tp,sl)
            for strategy in STRATEGIES:
                dirs = candidates[strategy]
                X,cols = make_event_features(feat,base_cols,dirs,funding_usable)
                finite = np.isfinite(X.to_numpy()).all(axis=1)
                eligible = (dirs != 1) & aligned & events["valid"] & finite
                fit,early,cal,policy,test = fivefold_split(ts,eligible,horizon)
                sizes = {"fit":len(fit),"early_stop":len(early),"calibration":len(cal),
                         "policy":len(policy),"historical_diagnostic":len(test)}
                meta={"horizon_minutes":horizon,"strategy":strategy,"barrier":barrier_name,
                      "feature_count":len(cols),"candidate_events_total":int(np.sum(eligible)),
                      "split_event_counts":sizes}
                experiment_counts[strategy+f"_{horizon}m"] = experiment_counts.get(strategy+f"_{horizon}m",0)+1
                if (len(fit) < config.minimum_fit_events or len(early) < 25 or
                    len(cal) < config.minimum_calibration_events or len(policy) < config.minimum_policy_events or
                    len(test) < 40):
                    summaries.append({**meta,"status":"INSUFFICIENT_CANDIDATE_EVENTS"})
                    continue
                y = tp_first_binary(events, dirs)
                if min(np.bincount(y[fit],minlength=2)) < 8 or min(np.bincount(y[early],minlength=2)) < 3:
                    summaries.append({**meta,"status":"INSUFFICIENT_TP_CLASS_DIVERSITY"})
                    continue
                if len(fit) > config.max_fit_events:
                    fit=fit[::math.ceil(len(fit)/config.max_fit_events)]
                model=fit_model(X.iloc[fit],y[fit],X.iloc[early],y[early],seed=555+horizon)
                if model is None:
                    summaries.append({**meta,"status":"MODEL_UNAVAILABLE"})
                    continue
                nmodels+=1
                pcalraw=raw_probability(model,X.iloc[cal]); calibrator=fit_calibrator(pcalraw,y[cal])
                if calibrator is None:
                    summaries.append({**meta,"status":"INSUFFICIENT_CALIBRATION_CLASSES"})
                    continue
                ppolicyraw=raw_probability(model,X.iloc[policy])
                ppolicy=np.asarray(calibrator.predict(ppolicyraw),float)
                cal_report={"raw":calibration_scores(y[policy],ppolicyraw),
                            "calibrated":calibration_scores(y[policy],ppolicy)}
                options=[]
                for cutoff,edge in THRESHOLDS:
                    actions, exp, reasons = decisions_from_probability(ppolicy,policy,events,dirs,cutoff,edge,config.costs)
                    stats,_=v55.backtest(policy,actions,feat,events,config.costs)
                    passed,score=qualify(stats,config)
                    options.append(((int(passed),score),cutoff,edge,stats,v554.funnel_summary(reasons)))
                key,cutoff,edge,stats,funnel=max(options,key=lambda x:x[0])
                info={**meta,"status":"VALIDATION_PASSED" if key[0] else "VALIDATION_FAILED",
                      "validated":bool(key[0]),"cutoff_probability":cutoff,
                      "minimum_expected_net_bps":edge,"policy_metrics":stats,
                      "policy_funnel":funnel,"policy_probability_calibration":cal_report}
                summaries.append(info)
                if selected is None or key > selected[0]:
                    selected=(key,info,model,calibrator,dirs,events,X,test,y)
    if selected is None:
        return {"status":"NO_TRAINABLE_CANDIDATE","candidate_reports":summaries,
                "models_trained":nmodels,"test_scored":False,
                "validation_per_strategy_and_horizon":experiment_counts,
                "model_ready_for_live":False,"orders_enabled":False},None,[]
    key,info,model,calibrator,dirs,events,X,test,y=selected
    # Exactly once, after validation selection. Never select a candidate on test.
    ptest_raw=raw_probability(model,X.iloc[test]);ptest=np.asarray(calibrator.predict(ptest_raw),float)
    actions,exp,reasons=decisions_from_probability(ptest,test,events,dirs,
        info["cutoff_probability"],info["minimum_expected_net_bps"],config.costs)
    stats,rows=v55.backtest(test,actions,feat,events,config.costs)
    # Entry CSV must map each executed hypothetical trade back to signal-time probabilities.
    lookup={v55.iso(int(ts[i]+v54.MS)):j for j,i in enumerate(test)}
    for row in rows:
        loc=lookup.get(row["signal_close_utc"])
        if loc is not None:
            row.update(strategy=info["strategy"],horizon_minutes=info["horizon_minutes"],
                       calibrated_tp_probability=round(float(ptest[loc]),5),
                       conservative_expected_net_bps=round(float(exp[loc]),3),
                       observed_historical_only=True,not_live_order=True)
    bundle = {"feature_columns_in_order":list(X.columns),
              "isotonic_x_thresholds":np.asarray(calibrator.X_thresholds_,float).tolist(),
              "isotonic_y_thresholds":np.asarray(calibrator.y_thresholds_,float).tolist(),
              "binary_label":"TP_FIRST_AND_POSITIVE_NET",
              "strategy":info["strategy"],
              "horizon_minutes":info["horizon_minutes"],
              "research_only_no_live_inference":True}
    return ({"status":"EXPLORATORY_GATE_PASSED_RESEARCH_ONLY" if key[0] else "NO_VALIDATED_EDGE_BE_KLE",
             "selected_model_calibration_research_only":bundle,
             "selected_from_policy_only":info,
             "candidate_reports":summaries,"models_trained":nmodels,
             "validation_per_strategy_and_horizon":experiment_counts,
             "historical_diagnostic_not_fresh_holdout":{
                 "metrics":stats,
                 "candidate_events":len(test),
                 "decisions":{"AL":int(np.sum(actions==2)),"SAT":int(np.sum(actions==0)),"BEKLE":int(np.sum(actions==1))},
                 "funnel":v554.funnel_summary(reasons),
                 "calibration":{"raw":calibration_scores(y[test],ptest_raw),
                                "calibrated":calibration_scores(y[test],ptest)},
                 "cost_sensitivity_fixed_trades":v553.cost_sensitivity(rows,10000*config.costs.roundtrip)},
             "no_historical_level2_order_book":True,
             "historical_period_previously_reused_no_clean_holdout":True,
             "new_forward_period_required":True,"model_ready_for_live":False,
             "live_inference_enabled":False,"orders_enabled":False},model,rows)


def run(first,last,outdir,*,funding_mode="optional",config=Config(),download=v54.read_month,
        funding_download=None,api_fetch=None):
    config.check()
    months=v54.month_range(first,last)
    raw=v54.historical_frame(months,download=download)
    feat,cols=v54.features_targets(raw)
    feat=v551.enrich_cvd_regime(raw,feat)
    feat=v554.additional_features(raw,feat)
    feat,fund_info=v553.add_funding(feat,first,last,mode=funding_mode,
                                    archive_download=funding_download,api_fetch=api_fetch)
    result,model,rows=comparison(raw,feat,cols,config=config,
                                 funding_usable=fund_info["status"]=="usable")
    report={"engine":"TiMi ML v5.5.5 — 3 candidate strategies, calibrated TP meta-model, 1/3/5m",
            "schema":SCHEMA,"symbol":"BTCUSDT","market":"Binance Spot public historical",
            "months":months,"source":"Verified Binance Vision Spot 1m ZIP + optional settled funding",
            "candles":len(raw),"complete_feature_rows":len(feat),
            "candidate_strategies":list(STRATEGIES),"holding_horizons_minutes":list(HORIZONS),
            "funding":fund_info,"fee_bps_per_side":config.costs.fee_bps_side,
            "slippage_bps_per_side":config.costs.slippage_bps_side,
            "modeled_roundtrip_cost_bps":round(config.costs.roundtrip*10000,3),
            "label":"binary TP-first AND NET positive; timeout treated as non-TP",
            "split":"fit(50%)/early-stop(10%)/calibration(10%)/threshold-policy(10%)/diagnostic(20%); 6m embargo",
            "execution_limits":"Reference fills at next 1m OPEN; unknown intrabar path uses SL-first; spot SAT directional only, NOT a spot short trade. No actual spread or queue simulation",
            "note":"Previously inspected May-Oct 2026 data, exploratory statistics only. No claim of high win rate or profitability.",
            **result}
    out=Path(outdir);out.mkdir(parents=True,exist_ok=True)
    (out/"report.json").write_text(json.dumps(report,ensure_ascii=False,indent=2,allow_nan=False),encoding="utf-8")
    (out/"strategy_comparison.json").write_text(json.dumps(result["candidate_reports"],ensure_ascii=False,indent=2,allow_nan=False),encoding="utf-8")
    if "selected_model_calibration_research_only" in result:
        (out/"calibration_RESEARCH_ONLY.json").write_text(
            json.dumps(result["selected_model_calibration_research_only"],ensure_ascii=False,indent=2,allow_nan=False),encoding="utf-8")
    if rows:
        with (out/"historical_reference_trades.csv").open("w",newline="",encoding="utf-8") as fh:
            writer=csv.DictWriter(fh,fieldnames=list(rows[0]))
            writer.writeheader();writer.writerows(rows)
    if model is not None:
        model.booster_.save_model(str(out/"selected_model_RESEARCH_ONLY.txt"))
    print("TiMi v5.5.5",report["status"],"| candidate experiments",len(result["candidate_reports"]),"| models",result["models_trained"],flush=True)
    return report


def main():
    parser=argparse.ArgumentParser(description="TiMi ML v5.5.5 — calibrated 1/3/5m research only")
    parser.add_argument("--from-month",default="2026-05")
    parser.add_argument("--to-month",default="2026-10")
    parser.add_argument("--funding-mode",choices=("off","optional","required"),default="optional")
    parser.add_argument("--output",default="timi_v555_artifacts")
    parser.add_argument("--fee-bps",type=float,default=10.)
    parser.add_argument("--slippage-bps",type=float,default=2.)
    args=parser.parse_args()
    run(args.from_month,args.to_month,args.output,funding_mode=args.funding_mode,
        config=Config(costs=v55.Costs(args.fee_bps,args.slippage_bps,2.)))


if __name__=="__main__":
    main()
