"""TiMi ML v5.5.8 — BTCUSDT Spot long-only cost and market-regime audit.

Developer: Suat DOĞU. PUBLIC HISTORICAL DATA, RESEARCH ONLY.
No order placement, private exchange key, database write, trained model deployment,
or live signals. Does not claim profitable backtests, an executable fill model,
or an independent holdout on previously examined May–October 2026 data.

The existing v5.5.7 ATR experiment geometry is left unchanged. For 150
predeclared alternatives, this module builds a serial, Spot-LONG-only
reference path on POLICY and historical DIAGNOSTIC folds. It reports costs,
time-of-day and causal regime subgroups without optimizing on those results.
Every reference fill is hypothetical. Trading costs are user assumptions,
not fetched from an authenticated Binance commission or quote feed.
"""
from __future__ import annotations

import argparse
import csv
import json
import math
from dataclasses import dataclass
from pathlib import Path
from typing import Any

import numpy as np
import pandas as pd

import timi_ml_v556_research as v556
import timi_ml_v557_barrier_lab as v557

SCHEMA = "timi-ml-v558-spot-long-only-cost-regime-audit-v1"
HORIZONS = v557.HORIZONS
BARRIERS = v557.BARRIERS
POLICIES = v557.POLICIES
MS = 60_000
MIN_SUBGROUP_EVENTS = 30
MIN_POLICY_REFERENCE_TRADES = 60


@dataclass(frozen=True)
class Settings:
    fee_bps_per_side: float = 10.0
    slippage_bps_per_side: float = 2.0
    min_policy_trades: int = MIN_POLICY_REFERENCE_TRADES
    min_policy_mean_net_bps: float = 2.0
    min_policy_profit_factor: float = 1.10
    max_policy_drawdown_pct: float = 12.0

    @property
    def baseline_roundtrip_bps(self) -> float:
        return 2.0 * (self.fee_bps_per_side + self.slippage_bps_per_side)

    def check(self) -> None:
        vals=(self.fee_bps_per_side, self.slippage_bps_per_side,
              self.min_policy_mean_net_bps, self.min_policy_profit_factor,
              self.max_policy_drawdown_pct)
        if not all(map(math.isfinite, vals)):
            raise ValueError("Non-finite cost or policy settings")
        if min(self.fee_bps_per_side, self.slippage_bps_per_side) < 0:
            raise ValueError("Negative fee or slippage")
        if self.min_policy_trades < 30 or self.min_policy_profit_factor < 1.0:
            raise ValueError("Invalid predeclared policy gate")
        if not 0 <= self.max_policy_drawdown_pct <= 100:
            raise ValueError("Invalid policy drawdown gate")


def causal_context(feat: pd.DataFrame) -> dict[str, np.ndarray]:
    """Fixed UTC/regime buckets use ONLY present/past closed-candle features.

    'high volatility' flag takes priority over trend/range labels. Do not
    use any future labels or full-sample ATR quantiles to define the buckets.
    """
    required=("ts", "atr14_pct", "regime_trend", "regime_range", "regime_high_vol")
    absent=[key for key in required if key not in feat.columns]
    if absent:
        raise ValueError(f"Missing causal regime fields: {absent}")
    ts=feat.ts.to_numpy(np.int64)
    atr=feat.atr14_pct.to_numpy(float)
    if (len(ts)<1 or (np.diff(ts)<=0).any() or (ts % MS != 0).any()
            or not np.isfinite(atr).all() or (atr<0).any()):
        raise ValueError("Invalid closed candle time or ATR")
    high=feat.regime_high_vol.to_numpy(float)==1
    trend=feat.regime_trend.to_numpy(float)==1
    ran=feat.regime_range.to_numpy(float)==1
    regime=np.where(high,"HIGH_VOL",np.where(trend,"TREND",np.where(ran,"RANGE","OTHER")))
    hour=((ts//MS+1) % 1440)//60
    time_block=np.where(hour<8,"UTC_00_08",np.where(hour<16,"UTC_08_16","UTC_16_24"))
    # Prespecified absolute ATR buckets (not a hindsight quantile cut).
    vol=np.where(atr<.0005,"ATR_LT_5bps",
                 np.where(atr<.0015,"ATR_5_TO_15bps","ATR_GE_15bps"))
    return {"regime":regime,"utc_block":time_block,"atr_bucket":vol}


def select_long_nonoverlap(events: dict, directions: np.ndarray,
                           indices: np.ndarray, ts: np.ndarray) -> np.ndarray:
    """Serial one-long-at-a-time references, separated within each variant/fold.

    A signal at minute-open t enters at next candle OPEN and is unavailable
    until the minute AFTER the exiting candle. A newly selected entry cannot
    overlap an earlier selected trade. No short references are executed.
    """
    ix=np.asarray(indices, dtype=int)
    directions=np.asarray(directions,np.int8)
    ts=np.asarray(ts,np.int64)
    if len(directions)!=len(ts) or len(events["valid"])!=len(ts):
        raise ValueError("Mismatched arrays")
    if len(ix) and (ix.min()<0 or ix.max()>=len(ts) or np.any(np.diff(ix)<=0)):
        raise ValueError("Event indices must be ordered and unique")
    valid_long=ix[(directions[ix]==2)&events["valid"][ix]]
    selected=[]
    next_available=-1
    for i in valid_long:
        t=int(ts[i])
        if t < next_available:
            continue
        k=int(events["long_step"][i])
        if k<1:
            raise ValueError("Exit must not precede entry")
        selected.append(int(i))
        next_available=t+(k+1)*MS
    return np.asarray(selected,dtype=int)


def metrics(gross_bps: np.ndarray, cost_bps: float) -> dict[str, Any]:
    """Reference path assumes fixed notional per selected LONG, not an actual fill.

    Drawdown is a hypothetical fully reinvested all-in trade sequence; it is
    NOT realistic continuous mark-to-market or a live portfolio risk estimate.
    Sharpe is intentionally withheld for irregular execution-only samples.
    """
    gross=np.asarray(gross_bps,float)
    if not np.isfinite(cost_bps) or cost_bps<0 or not np.isfinite(gross).all():
        raise ValueError("Invalid reference gross returns or costs")
    n=len(gross)
    if not n:
        return {"trades":0,"mean_net_bps":None,"win_rate":None,"profit_factor":None,
                "max_drawdown_pct":None,"net_sum_bps_simple":None,"p05_net_bps":None,
                "cvar_05_net_bps":None,"median_net_bps":None,"sharpe_ratio":None,
                "sharpe_reason":"No observations; no regular marked-to-market returns"}
    net=gross-cost_bps
    positive=float(net[net>0].sum())
    negative=float(-net[net<0].sum())
    gross_factors=np.maximum(1e-12,1+net/10000.0)
    equity=np.r_[1.0,np.cumprod(gross_factors)]
    drawdown=100*np.max(1-equity/np.maximum.accumulate(equity))
    cutoff=float(np.quantile(net,.05))
    tail=net[net<=cutoff]
    return {"trades":int(n),"mean_net_bps":round(float(net.mean()),4),
            "median_net_bps":round(float(np.median(net)),4),
            "net_sum_bps_simple":round(float(net.sum()),4),
            "win_rate":round(float((net>0).mean()),6),
            "profit_factor":round(positive/negative,5) if negative>0 else None,
            "max_drawdown_pct":round(float(drawdown),4),
            "p05_net_bps":round(cutoff,4),
            "cvar_05_net_bps":round(float(tail.mean()),4),
            "sharpe_ratio":None,
            "sharpe_reason":"OHLC-only irregular hypothetical fills; no daily marked-to-market returns"}


def evaluate(events: dict, directions: np.ndarray, indices: np.ndarray,
             ts: np.ndarray, context: dict[str,np.ndarray], cfg: Settings) -> dict[str,Any]:
    """Compute aggregate + prespecified subgroup and cost sensitivity results.

    Subgroups split *the same* one-long-at-a-time reference set. Never rank
    or select on the previously inspected diagnostic fold.
    """
    cfg.check()
    ids=select_long_nonoverlap(events,directions,indices,ts)
    idx=np.asarray(indices,int)
    attempted=int(np.sum((np.asarray(directions)[idx]==2)&events["valid"][idx]))
    rejected_shorts=int(np.sum((np.asarray(directions)[idx]==0)&events["valid"][idx]))
    gross=events["long_gross_bps"][ids]
    base=metrics(gross,cfg.baseline_roundtrip_bps)
    scenarios=sorted({12.0,24.0,40.0,float(cfg.baseline_roundtrip_bps),
                      float(cfg.baseline_roundtrip_bps+8.0)})
    by_cost={f"{v:g}":metrics(gross,v) for v in scenarios}
    groups={}
    for dimension in ("regime","utc_block","atr_bucket"):
        labels=context[dimension][ids]
        groups[dimension]={}
        for label in sorted(set(context[dimension])):
            sub=gross[labels==label]
            item=metrics(sub,cfg.baseline_roundtrip_bps)
            item["descriptive_only"] = True
            item["sample_size_sufficient_for_description"] = bool(len(sub)>=MIN_SUBGROUP_EVENTS)
            groups[dimension][str(label)]=item
    flags=events["long_flag"][ids]
    if int(np.sum(flags==1)+np.sum(flags==-1)+np.sum(flags==0))!=len(ids):
        raise AssertionError("Long-only TP/SL/timeout outcomes not exhaustive")
    if len(ids) and len(events["net_rr"])!=len(ts):
        raise ValueError("Broken risk ratio vector")
    rr_pass=int(np.sum(np.isfinite(events["net_rr"][ids])&(events["net_rr"][ids]>=1.25)))
    # Gate evaluated *only* on the explicitly named policy fold by the caller.
    pf=base["profit_factor"]
    gate=(len(ids)>=cfg.min_policy_trades and
          base["mean_net_bps"] is not None and
          base["mean_net_bps"]>=cfg.min_policy_mean_net_bps and
          pf is not None and pf>=cfg.min_policy_profit_factor and
          base["max_drawdown_pct"]<=cfg.max_policy_drawdown_pct and
          rr_pass==len(ids))
    return {"candidate_LONG_before_nonoverlap":attempted,
            "excluded_SAT_directional_signals":rejected_shorts,
            "selected_reference_LONG_trades":int(len(ids)),
            "long_tp_first":int(np.sum(flags==1)),"long_sl_first":int(np.sum(flags==-1)),
            "long_timeout":int(np.sum(flags==0)),"net_reward_risk_pass_count":rr_pass,
            "baseline":base,"fixed_fill_cost_scenarios_bps":by_cost,
            "causal_subgroups":groups,"internal_policy_gate_passed":bool(gate),
            "not_independent_executable_evidence":True}


def audit(raw: pd.DataFrame, feat: pd.DataFrame, cfg:Settings=Settings(),
          *,horizons=HORIZONS,barriers=BARRIERS,policies=POLICIES,
          candidate_map=None) -> list[dict]:
    """150 variants; no training, no re-optimization, no live promotion."""
    cfg.check()
    if len(feat)<1000:
        raise ValueError("Too few complete features")
    ts=feat.ts.to_numpy(np.int64)
    context=causal_context(feat)
    candidates=v556.candidate_directions(raw,feat) if candidate_map is None else candidate_map
    expected=set(v556.STRATEGIES) if candidate_map is None else set(candidate_map)
    if not candidates or set(candidates)!=expected:
        raise ValueError("Unknown strategy family names")
    ref_cfg=v557.Settings(cfg.fee_bps_per_side,cfg.slippage_bps_per_side)
    results=[]
    for horizon in horizons:
        aligned=v556.align_closes(ts,horizon)
        for bar,tp,sl in barriers:
            for policy in policies:
                ev=v557.simulate_ohlc(raw,feat,horizon,tp,sl,policy,ref_cfg)
                for strategy,directions in candidates.items():
                    dirs=np.asarray(directions,np.int8)
                    if len(dirs)!=len(feat) or not np.isin(dirs,(0,1,2)).all():
                        raise ValueError("Invalid strategy candidate directions")
                    eligible=(dirs!=1)&aligned&ev["valid"]
                    folds=v556.chronological_folds(ts,eligible,horizon)
                    # Preserve fold construction on ALL candidate directions.
                    # Separately score only executable Spot long references.
                    summaries={}
                    for fold_name,ids in (("policy",folds[3]),("diagnostic",folds[4])):
                        summaries[fold_name]=evaluate(ev,dirs,ids,ts,context,cfg)
                    results.append({"strategy":strategy,"horizon_minutes":horizon,
                                    "barrier":bar,"geometry_policy":policy,
                                    "non_rr_geometry_exploratory_only":policy!="legacy_rr",
                                    "fold_candidate_counts":dict(zip(
                                      ("fit","early_stop","calibration","policy","diagnostic"),
                                      (int(len(x)) for x in folds))),
                                    "policy":summaries["policy"],
                                    "historical_diagnostic":summaries["diagnostic"],
                                    "model_trained":False,"live_promotion_allowed":False})
    return results


def run(first:str,last:str,output:str,*,cfg:Settings=Settings(),
        download=None) -> dict:
    """Load official verified Binance Vision monthly candles, never live trading."""
    import timi_ml_v54_history_train as v54
    import timi_ml_v551_research as v551
    import timi_ml_v554_scalp as v554
    months=v54.month_range(first,last)
    raw=v54.historical_frame(months,download=(download or v54.read_month))
    quality=v556.audit_historical_candles(raw)
    feat,_=v54.features_targets(raw)
    feat=v551.enrich_cvd_regime(raw,feat)
    feat=v554.additional_features(raw,feat)
    feat=v556.causal_features(raw,feat)
    experiments=audit(raw,feat,cfg)
    summary={"total_experiments":len(experiments),"policy_gate_pass_count":int(sum(
             x["policy"]["internal_policy_gate_passed"] for x in experiments)),
             "model_trained":False,"live_promotion_allowed":False,
             "market":"Binance BTCUSDT Spot, public historical OHLCV",
             "candles":len(raw),"complete_feature_rows":len(feat),
             "excluded_short_execution":"SAT signals counted only, never executed",
             "costs":{
               "assumed_fee_bps_per_side":cfg.fee_bps_per_side,
               "assumed_slippage_bps_per_side":cfg.slippage_bps_per_side,
               "assumed_roundtrip_bps":cfg.baseline_roundtrip_bps,
               "actual_user_commission_verified":False,
               "order_book_spread_verified":False,
               "slippage_or_queue_fills_verified":False,
               "cost_scenarios_are_fixed_fill_repricing_not_new_market_simulations":True},
             "restrictions":{
               "no_training":True,"no_live_inference":True,"no_real_orders":True,
               "no_private_exchange_keys":True,"no_database_writes":True,
               "may_oct_previously_researched_not_independent_holdout":True,
               "new_future_forward_period_required":True,
               "each_strategy_tested_separately_no_cross_strategy_portfolio":True,
               "grouped_statistics_not_optimized_or_executable":True,
               "reference_fills_do_not_model_spread_slippage_queue_or_stop_gap":True}}
    report={"schema":SCHEMA,"engine":"TiMi ML v5.5.8 Spot Long-Only Edge Audit",
            "developer":"Suat DOĞU","period_months":months,
            "data_quality":quality,"costs":summary["costs"],
            "experiments":summary["total_experiments"],
            "policy_gates_passed_internal":summary["policy_gate_pass_count"],
            "model_ready_for_live":False,"live_inference_enabled":False,"orders_enabled":False,
            "training":"NOT_PERFORMED_BY_DESIGN",
            "reused_research_period_not_independent_oos":True,
            "fresh_forward_validation_required":True,
            "minimum_policy_reference_trades":cfg.min_policy_trades,
            "minimum_policy_mean_net_bps":cfg.min_policy_mean_net_bps,
            "minimum_policy_profit_factor":cfg.min_policy_profit_factor,
            "maximum_policy_drawdown_pct":cfg.max_policy_drawdown_pct,
            "notes":[
                "3 predefined policies x 2 ATR barrier pairs x 5 horizons x 5 strategies = 150 experiments",
                "A candidate is not a real order. Even a passing historical policy gate is NOT independent proof of advantage.",
                "May–October 2026 is already repeatedly researched. Do not choose winners from diagnostic results.",
                "Sharpe intentionally omitted without verifiable marked-to-market equity curve.",
                "ATR and UTC segment groups are descriptive and use only closed-candle features.",
                "Fee, slippage and spread assumptions must be verified before any forward execution evaluation.",
                "Futures funding is not a Binance Spot trading fee; omitted."]}
    out=Path(output);out.mkdir(parents=True,exist_ok=True)
    (out/"report.json").write_text(json.dumps(report,ensure_ascii=False,indent=2,allow_nan=False),encoding="utf8")
    (out/"data_quality.json").write_text(json.dumps(quality,ensure_ascii=False,indent=2,allow_nan=False),encoding="utf8")
    (out/"spot_edge_experiments.json").write_text(json.dumps(experiments,ensure_ascii=False,indent=2,allow_nan=False),encoding="utf8")
    (out/"cost_audit.json").write_text(json.dumps(summary,ensure_ascii=False,indent=2,allow_nan=False),encoding="utf8")
    with (out/"spot_long_comparison.csv").open("w",encoding="utf8",newline="") as fh:
        keys=("strategy","horizon_minutes","barrier","geometry_policy","fold",
              "candidate_LONG_before_nonoverlap","excluded_SAT_directional_signals",
              "selected_reference_LONG_trades","long_tp_first","long_sl_first","long_timeout",
              "mean_net_bps","profit_factor","win_rate","max_drawdown_pct",
              "internal_policy_gate_passed")
        writer=csv.DictWriter(fh,fieldnames=keys)
        writer.writeheader()
        for variant in experiments:
            for fold,output_name in (("policy","policy"),("diagnostic","historical_diagnostic")):
                m=variant[output_name]
                row={k:variant[k] for k in keys[:4]}
                row.update({"fold":fold,**{k:m.get(k) for k in keys[5:11]},
                           **{k:m["baseline"].get(k) for k in keys[11:15]},
                           "internal_policy_gate_passed":m["internal_policy_gate_passed"]})
                writer.writerow(row)
    print(f"TiMi v5.5.8 research: {len(experiments)} variants; live and orders OFF",flush=True)
    return report


def main():
    p=argparse.ArgumentParser(description="TiMi Spot long-only historical edge audit; read-only")
    p.add_argument("--from-month",default="2026-05")
    p.add_argument("--to-month",default="2026-10")
    p.add_argument("--fee-bps",type=float,default=10.0)
    p.add_argument("--slippage-bps",type=float,default=2.0)
    p.add_argument("--output",default="timi_v558_artifacts")
    args=p.parse_args()
    run(args.from_month,args.to_month,args.output,cfg=Settings(
        fee_bps_per_side=args.fee_bps,slippage_bps_per_side=args.slippage_bps))

if __name__=="__main__":
    main()
