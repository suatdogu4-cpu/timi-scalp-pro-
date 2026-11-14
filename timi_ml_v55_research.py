"""TiMi ML v5.5 — offline BTCUSDT historical research, never live trading.

Requires sibling timi_ml_v54_history_train.py (verified Binance public kline ZIPs).
Compares 5/15-minute horizons, net-close vs ATR TP/SL targets, and
balanced vs standard LightGBM. Tunes threshold on validation ONLY.
Never fetches private accounts, places orders, modifies PostgreSQL, or deploys a model.
"""
from __future__ import annotations

import argparse
import csv
import json
import math
from dataclasses import dataclass
from datetime import datetime, timezone
from pathlib import Path

import numpy as np
import pandas as pd

import timi_ml_v54_history_train as v54

MS = 60_000
SCHEMA = "timi-ml-v55-historical-ohlcv-taker-v1"
CLASS_NAMES = {0: "SAT", 1: "BEKLE", 2: "AL"}
HORIZONS = (5, 15)
MODES = ("net_close", "atr_tp_sl")
WEIGHTS = (None, "balanced")
MAX_HORIZON = 15
MIN_TRAIN = 4000
MIN_VALID = 1200
MIN_TEST = 1200


@dataclass(frozen=True)
class Costs:
    fee_bps_side: float = 10.0
    slippage_bps_side: float = 2.0
    edge_bps: float = 2.0

    @property
    def roundtrip(self):
        return 2 * (self.fee_bps_side + self.slippage_bps_side) / 10000.0

    def validate(self):
        for n in (self.fee_bps_side, self.slippage_bps_side, self.edge_bps):
            if not math.isfinite(n) or n < 0 or n > 100:
                raise ValueError("Fee, slippage, and edge must be finite values in 0..100 bps")


def iso(ms):
    return datetime.fromtimestamp(int(ms) / 1000, tz=timezone.utc).isoformat()


def build_events(raw, feat, horizon, costs: Costs):
    """Targets use future candles only, features remain available at signal close.

    Full exits are pessimistic for intrabar TP+SL ties. Signals originate at
    close of 1m candle t; hypothetical entry is OPEN of candle t+1.
    """
    if horizon not in HORIZONS:
        raise ValueError("Unsupported horizon")
    costs.validate()
    raw_ts = raw.ts.to_numpy(dtype=np.int64)
    ft_ts = feat.ts.to_numpy(dtype=np.int64)
    pos = np.searchsorted(raw_ts, ft_ts)
    if (pos >= len(raw_ts)).any() or not np.array_equal(raw_ts[pos], ft_ts):
        raise ValueError("Feature timestamps not found in raw archive")
    n = len(ft_ts)
    p = pos + 1
    end = pos + horizon
    in_bounds = end < len(raw_ts)
    can = in_bounds.copy()
    can[in_bounds] = (raw_ts[end[in_bounds]] == ft_ts[in_bounds] + horizon * MS)
    seg = raw.segment.to_numpy()
    can[in_bounds] &= seg[end[in_bounds]] == seg[pos[in_bounds]]
    open_ = raw.open.to_numpy(dtype=float)
    high_ = raw.high.to_numpy(dtype=float)
    low_ = raw.low.to_numpy(dtype=float)
    close_ = raw.close.to_numpy(dtype=float)
    entry = np.full(n, np.nan)
    endpoint = np.full(n, np.nan)
    entry[can] = open_[p[can]]
    endpoint[can] = close_[end[can]]
    atr = feat.atr14_pct.to_numpy(dtype=float)
    # ATR is known at signal time; floors avoid TP smaller than round-trip costs.
    tp_frac = np.maximum(1.6 * atr, costs.roundtrip + 0.0012)
    stop_frac = np.maximum(1.1 * atr, 0.0015)
    tp_frac = np.clip(tp_frac, 0.0, 0.02)
    stop_frac = np.clip(stop_frac, 0.0, 0.015)
    good = can & np.isfinite(entry) & (entry > 0) & np.isfinite(atr) & (atr > 0)
    tp_long = entry * (1 + tp_frac)
    sl_long = entry * (1 - stop_frac)
    tp_short = entry * (1 - tp_frac)
    sl_short = entry * (1 + stop_frac)
    # Side outcomes and returns, stop wins on a TP/SL tie in any single candle.
    long_side = np.zeros(n, dtype=np.int8)      # +1 TP, -1 SL, 0 horizon close
    short_side = np.zeros(n, dtype=np.int8)
    long_k = np.full(n, horizon, dtype=np.int16)
    short_k = np.full(n, horizon, dtype=np.int16)
    exit_long = endpoint.copy()
    exit_short = endpoint.copy()
    for k in range(1, horizon + 1):
        cur = pos + k
        valid_k = good & (cur < len(raw_ts))
        # Clip before indexing, then only act on valid candidate rows.
        hi = high_[np.minimum(cur, len(raw_ts) - 1)]
        lo = low_[np.minimum(cur, len(raw_ts) - 1)]
        active_l = valid_k & (long_side == 0) & (long_k == horizon)
        active_s = valid_k & (short_side == 0) & (short_k == horizon)
        # At the final candle, the unresolved state is still active.
        l_sl = active_l & (lo <= sl_long)
        l_tp = active_l & ~l_sl & (hi >= tp_long)
        s_sl = active_s & (hi >= sl_short)
        s_tp = active_s & ~s_sl & (lo <= tp_short)
        long_side[l_sl], long_side[l_tp] = -1, 1
        short_side[s_sl], short_side[s_tp] = -1, 1
        exit_long[l_sl], exit_long[l_tp] = sl_long[l_sl], tp_long[l_tp]
        exit_short[s_sl], exit_short[s_tp] = sl_short[s_sl], tp_short[s_tp]
        long_k[l_sl | l_tp] = k
        short_k[s_sl | s_tp] = k
    # IMPORTANT: for a TP/SL event before final horizon, do not allow later
    # events to replace its exit.  At final k unresolved horizon exits at close.
    long_net = exit_long / entry - 1 - costs.roundtrip
    short_net = (entry - exit_short) / entry - costs.roundtrip
    finish_net = endpoint / entry - 1
    cut = costs.roundtrip + costs.edge_bps / 10000.0
    net_label = np.where(finish_net > cut, 2, np.where(finish_net < -cut, 0, 1))
    # If both sides (hypothetically) win, pick the earlier; equal => BEKLE.
    lwin = (long_side == 1) & (long_net > costs.edge_bps / 10000)
    swin = (short_side == 1) & (short_net > costs.edge_bps / 10000)
    barrier_label = np.where(lwin & (~swin | (long_k < short_k)), 2,
                     np.where(swin & (~lwin | (short_k < long_k)), 0, 1))
    return {
        "valid": good, "entry": entry, "endpoint": endpoint,
        "tp_long": tp_long, "sl_long": sl_long,
        "tp_short": tp_short, "sl_short": sl_short,
        "long_net": long_net, "short_net": short_net,
        "long_exit": exit_long, "short_exit": exit_short,
        "long_side": long_side, "short_side": short_side,
        "long_k": long_k, "short_k": short_k,
        "labels": {"net_close": net_label, "atr_tp_sl": barrier_label},
    }


def split_indices(times, valid, horizon):
    """Fixed chronological anchors across all candidates, purge >= 15m on BOTH folds."""
    n = len(times)
    t1 = int(times[int(n * 0.65)])
    t2 = int(times[int(n * 0.80)])
    purge = (MAX_HORIZON + 1) * MS
    train = np.flatnonzero(valid & (times < t1 - purge))
    validation = np.flatnonzero(valid & (times >= t1) & (times < t2 - purge))
    test = np.flatnonzero(valid & (times >= t2))
    if min(len(train), len(validation), len(test)) < min(MIN_TRAIN, MIN_VALID, MIN_TEST):
        raise ValueError("Too little continuous labeled data")
    if len(train)<MIN_TRAIN or len(validation)<MIN_VALID or len(test)<MIN_TEST:
        raise ValueError("Insufficient split sizes")
    assert times[train[-1]] + (horizon+1)*MS < times[validation[0]]
    assert times[validation[-1]] + (horizon+1)*MS < times[test[0]]
    return train, validation, test


def select_directions(proba, threshold, margin):
    """Thresholds are selection rules, NOT calibrated confidence."""
    # LightGBM classes are expected [0=SAT,1=BEKLE,2=AL].
    p0, p1, p2 = proba[:, 0], proba[:, 1], proba[:, 2]
    side = np.where(p2 > p0, 2, 0)
    biggest = np.maximum(p2, p0)
    rival = np.where(p2 > p0, p0, p2)
    can_trade = (biggest >= threshold) & (biggest - rival >= margin) & (biggest >= p1)
    return np.where(can_trade, side, 1)


def simulate(indices, decisions, frame, events, costs, *, probabilities=None,
             truth=None, collect=False):
    """Independent, nonoverlapping hypothetical signals on one evaluation fold."""
    assert len(indices) == len(decisions)
    times = frame.ts.to_numpy(dtype=np.int64)
    last_exit_bar = -1
    trades = []
    sum_win, sum_loss = 0., 0.
    wins, dd, peak, equity = 0, 0., 1., 1.
    for i, direction in zip(indices, decisions):
        i = int(i); direction = int(direction)
        if direction == 1 or times[i] <= last_exit_bar:
            continue
        is_long = direction == 2
        k = int(events["long_k"][i] if is_long else events["short_k"][i])
        side = int(events["long_side"][i] if is_long else events["short_side"][i])
        net = float(events["long_net"][i] if is_long else events["short_net"][i])
        exit_price = float(events["long_exit"][i] if is_long else events["short_exit"][i])
        if not np.isfinite(net):
            continue
        # Can only consider a new signal AFTER the prior fill/exit bar closes.
        last_exit_bar = times[i] + k * MS
        wins += net > 0
        sum_win += max(0.0, net)
        sum_loss += min(0.0, net)
        equity *= max(1e-9, 1 + net)
        peak = max(peak, equity)
        dd = max(dd, 1 - equity / peak)
        if collect:
            data = {
                "signal_close_utc": iso(times[i] + MS),
                "reference_entry_utc": iso(times[i] + MS),
                "direction": CLASS_NAMES[direction],
                "reference_entry_usdt": round(float(events["entry"][i]), 2),
                "take_profit_usdt": round(float(events["tp_long"][i] if is_long else events["tp_short"][i]), 2),
                "stop_loss_usdt": round(float(events["sl_long"][i] if is_long else events["sl_short"][i]), 2),
                "exit_reference_usdt": round(exit_price, 2),
                "exit_utc": iso(times[i] + (k+1)*MS),
                "exit_reason": "TP" if side == 1 else "SL" if side == -1 else "HORIZON_CLOSE",
                "net_bps_after_cost": round(net*10000, 3),
                "reference_only_not_executed": True,
            }
            if probabilities is not None:
                loc = int(np.searchsorted(indices, i))
                pp = probabilities[loc]
                data.update(p_sat=round(float(pp[0]),5),p_bekle=round(float(pp[1]),5),p_al=round(float(pp[2]),5))
            if truth is not None:
                loc = int(np.searchsorted(indices, i))
                data["target_label"] = CLASS_NAMES[int(truth[loc])]
            trades.append(data)
    return {"wins":wins, "gross_positive":sum_win, "gross_negative":sum_loss,
            "ending_equity":equity, "maximum_drawdown_pct":round(dd*100, 4),
            "records":trades}


def backtest(indices, directions, frame, events, costs, **kwargs):
    # Event test returns an explicit record list for counting. At most O(n) rows.
    result = simulate(indices, directions, frame, events, costs, collect=True, **kwargs)
    rows=result.pop("records")
    n=len(rows)
    nets=[r["net_bps_after_cost"] for r in rows]
    total_bps=float(sum(nets))
    pf = (result["gross_positive"] / abs(result["gross_negative"])) if result["gross_negative"] < 0 else None
    result.pop("gross_positive");result.pop("gross_negative")
    result.pop("ending_equity")
    result.update(trades=n, win_rate_pct=round(100*result.pop("wins")/n,3) if n else None,
                  average_net_bps=round(total_bps/n, 3) if n else None,
                  sum_net_bps=round(total_bps,3),
                  profit_factor=round(pf,4) if pf is not None else None,
                  compounded_hypothetical_pct=round((math.prod([1+r/10000 for r in nets])-1)*100,3) if nets else 0.0)
    return result, rows


def fmetrics(y, p, classes=(0,1,2)):
    from sklearn.metrics import accuracy_score, balanced_accuracy_score, f1_score, confusion_matrix, precision_recall_fscore_support
    precision,recall,f1,support=precision_recall_fscore_support(y,p,labels=list(classes),zero_division=0)
    return {"accuracy":float(accuracy_score(y,p)),
            "balanced_accuracy":float(balanced_accuracy_score(y,p)),
            "macro_f1":float(f1_score(y,p,average="macro",zero_division=0)),
            "confusion_matrix_rows_truth":confusion_matrix(y,p,labels=list(classes)).tolist(),
            "per_class":{CLASS_NAMES[k]:{"precision":round(float(precision[j]),4),
                 "recall":round(float(recall[j]),4),"f1":round(float(f1[j]),4),
                 "support":int(support[j])} for j,k in enumerate(classes)}}


def fit_candidate(Xt, yt, Xv, yv, weight):
    import lightgbm as lgb
    model=lgb.LGBMClassifier(objective="multiclass",num_class=3,
        n_estimators=180,learning_rate=0.04,num_leaves=19,max_depth=6,
        min_child_samples=100,colsample_bytree=0.85,reg_lambda=6,
        class_weight=weight,verbosity=-1,random_state=41,n_jobs=2)
    model.fit(Xt,yt,eval_set=[(Xv,yv)],eval_metric="multi_logloss",
              callbacks=[lgb.early_stopping(20,verbose=False)])
    if list(model.classes_) != [0,1,2]:
        raise ValueError("Unexpected class ordering")
    return model


def eligible_selection(trade_metrics, valid_len):
    # Small validation wins cannot make a model look viable.
    n=trade_metrics["trades"]
    return n >= max(60,int(0.002*valid_len))


def score_policy(stats):
    # Select on validation mean net after modeled costs, weighted for sampling.
    # This is exploratory selection. Its optimism is evaluated on untouched test.
    pf=stats["profit_factor"] or 0.0
    av=stats["average_net_bps"] or -1e6
    return av + min(pf,4.0)*0.02


def research(raw, feat, cols, *, costs=Costs()):
    times=feat.ts.to_numpy(dtype=np.int64)
    data=feat[cols].astype(np.float32)
    if len(feat)<10000:
        raise ValueError("Insufficient historical features")
    candidate_reports=[]
    best=None
    for horizon in HORIZONS:
        ev=build_events(raw,feat,horizon,costs)
        train,valid,test=split_indices(times,ev["valid"],horizon)
        for mode in MODES:
            labels=ev["labels"][mode]
            for weight in WEIGHTS:
                cfg={"horizon_minutes":horizon,"target_mode":mode,"class_weight":weight or "none"}
                if min(len(np.unique(labels[x])) for x in (train,valid,test))<3:
                    candidate_reports.append({**cfg,"status":"missing_class"})
                    continue
                model=fit_candidate(data.iloc[train],labels[train],data.iloc[valid],labels[valid],weight)
                p=model.predict_proba(data.iloc[valid])
                choices=[]
                for threshold in (0.30,0.38,0.48,0.60):
                    for margin in (0.02,0.10):
                        actions=select_directions(p,threshold,margin)
                        stats,_=backtest(valid,actions,feat,ev,costs)
                        if eligible_selection(stats,len(valid)):
                            choices.append((score_policy(stats),threshold,margin,stats))
                if not choices:
                    candidate_reports.append({**cfg,"status":"no_minimum_validation_trades",
                                              "validation_counts":len(valid)})
                    continue
                choices.sort(key=lambda x:x[0],reverse=True)
                _,threshold,margin,vstats=choices[0]
                report={**cfg,"status":"evaluated_on_validation_only",
                        "validation_trade_stats":vstats,"threshold":threshold,
                        "direction_margin":margin,"validation_count":len(valid),
                        "training_count":len(train),"test_reserved_count":len(test),
                        "validation_class_counts":{CLASS_NAMES[k]:int((labels[valid]==k).sum()) for k in CLASS_NAMES}}
                candidate_reports.append(report)
                # Choose winner on validation ONLY. Negative validation results remain
                # eligible for reporting, but NOT eligible for a deployment claim.
                rank=score_policy(vstats)
                if best is None or rank>best[0]:
                    best=(rank,model,cfg,ev,labels,train,valid,test,threshold,margin,vstats)
    if best is None:
        return {"status":"no_valid_model","candidates":candidate_reports},None,[]
    _,model,cfg,events,labels,train,valid,test,threshold,margin,vstats=best
    pp=model.predict_proba(data.iloc[test])    # FINAL TEST SCORED EXACTLY ONCE
    actions=select_directions(pp,threshold,margin)
    metrics=fmetrics(labels[test],actions)
    trade_stats,rows=backtest(test,actions,feat,events,costs,probabilities=pp,truth=labels[test])
    # Momentum comparator uses only features at signal time.
    momentum=feat["return_5m"].to_numpy(dtype=float)[test]
    naive=np.where(momentum>=0.001,2,np.where(momentum<=-0.001,0,1))
    base_stats,_=backtest(test,naive,feat,events,costs)
    hold_stats,_=backtest(test,np.ones(len(test),dtype=np.int8),feat,events,costs)
    hold_classification=fmetrics(labels[test],np.ones(len(test),dtype=np.int8))
    reliable=(vstats["average_net_bps"] or 0)>0 and vstats["trades"]>=60
    status="research_test_completed_not_approved"
    report={"status":status,"model_ready_for_live":False,"real_orders_enabled":False,
            "not_reconciled_with_real_fills":True,
            "model_probabilities_are_uncalibrated":True,
            "test_used_for_model_selection":False,
            "selection_only_on_validation":True,
            "selected_config":cfg,"validation_threshold":threshold,"validation_margin":margin,
            "validation_stats":vstats,"validation_cost_positive":reliable,
            "test_classification":metrics,"test_trade_stats":trade_stats,
            "test_naive_5m_momentum_trade_stats":base_stats,
            "test_always_wait_trade_stats":hold_stats,
            "test_always_wait_classification":hold_classification,
            "test_class_distribution":{CLASS_NAMES[k]:int((labels[test]==k).sum()) for k in CLASS_NAMES},
            "test_selected_actions":{CLASS_NAMES[k]:int((actions==k).sum()) for k in CLASS_NAMES},
            "test_passed_cost_gate": bool(reliable and trade_stats["trades"]>=100 and
                (trade_stats["average_net_bps"] or 0)>0 and
                (trade_stats["sum_net_bps"] or 0)>0),
            "test_first_signal_utc":iso(times[test[0]]+MS),
            "test_last_signal_utc":iso(times[test[-1]]+MS),
            "test_observations":len(test),
            "training_observations":len(train),"validation_observations":len(valid),
            "candidate_reports":candidate_reports}
    return report,model,rows


def run(first,last,outdir,costs=Costs(),download=v54.read_month):
    costs.validate()
    months=v54.month_range(first,last)
    raw=v54.historical_frame(months,download=download)
    feat,cols=v54.features_targets(raw)  # 33 strictly past-looking features
    out=Path(outdir);out.mkdir(parents=True,exist_ok=True)
    source={"engine":"TiMi ML v5.5 historical research","feature_schema":v54.FEATURE_SCHEMA,
       "training_schema":SCHEMA,"months":months,"symbol":"BTCUSDT","market":"Binance SPOT",
       "data_source":"verified monthly Binance Vision kline ZIP SHA256",
       "raw_closed_candles":len(raw),"eligible_feature_rows":len(feat),
       "features":cols,"no_historical_orderbook":True,"no_live_order_flow":True,
       "hypothetical_fill":"OPEN of next 1m bar, not an executed order",
       "tp_sl_assumption":"SL first whenever high and low touch TP and SL within same candle",
       "cost_assumption":{"fee_bps_per_side":costs.fee_bps_side,
                          "slippage_bps_per_side":costs.slippage_bps_side,
                          "round_trip_bps":10000*costs.roundtrip,"minimum_net_edge_bps":costs.edge_bps},
       "label_horizons_minutes":list(HORIZONS),"label_modes":list(MODES),
       "report_note":"No actual orders. SHORT on SPOT is only a theoretical directional label; no borrowing costs or funding modeled. Compounded hypothetical P&L assumes successive full-size positions. No future guarantee.",
       "live_inference":False,"model_deployed":False}
    if len(feat)<10000:
        result={**source,"status":"insufficient_data"}
        model=None;rows=[]
    else:
        r,model,rows=research(raw,feat,cols,costs=costs)
        result={**source,**r}
    (out/"report.json").write_text(json.dumps(result,ensure_ascii=False,indent=2,allow_nan=False),encoding="utf-8")
    if model is not None:
        model.booster_.save_model(str(out/"model_RESEARCH_ONLY.txt"),num_iteration=model.best_iteration_)
    if rows:
        with (out/"test_signal_references.csv").open("w",encoding="utf-8",newline="") as f:
            writer=csv.DictWriter(f,fieldnames=list(rows[0]));writer.writeheader();writer.writerows(rows)
    print("TiMi ML v5.5:",result["status"],"| rows:",len(feat),"| model research-only")
    if "test_trade_stats" in result:
        print("Final test",json.dumps(result["test_trade_stats"],ensure_ascii=False))
    return result


def main():
    p=argparse.ArgumentParser(description="TiMi ML v5.5 — research-only historical validation")
    p.add_argument("--from-month",default="2026-05")
    p.add_argument("--to-month",default="2026-10")
    p.add_argument("--output",default="timi_v55_artifacts")
    p.add_argument("--fee-bps",type=float,default=10.0)
    p.add_argument("--slippage-bps",type=float,default=2.0)
    p.add_argument("--min-edge-bps",type=float,default=2.0)
    args=p.parse_args()
    run(args.from_month,args.to_month,args.output,
        Costs(args.fee_bps,args.slippage_bps,args.min_edge_bps))


if __name__=="__main__":
    main()
