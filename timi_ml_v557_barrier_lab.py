"""TiMi ML v5.5.7 — ATR barrier feasibility and transaction-cost laboratory.

Developer: Suat DOĞU. PUBLIC HISTORICAL DATA ONLY; NO live signal/orders/DB writes.

Compares fixed, PREDECLARED target geometries. This is an exploratory diagnostic
not an automated optimizer or backtest proving an executable trading strategy.
No future candle contributes to the pre-entry signal, ATR, target, or stop.
The observed future OHLC is used ONLY to assess hypothetical reference outcomes.
Do not select the best configuration on the previously researched May-Oct 2026
period and then claim that same period is fresh out-of-sample evidence.
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

import timi_ml_v556_research as v556

HORIZONS = (1, 3, 5, 10, 15)
BARRIERS = (("atr_1p5_0p5", 1.5, 0.5), ("atr_2p0_0p7", 2.0, 0.7))
POLICIES = ("legacy_rr", "cost_cover", "atr_only")
SCENARIOS = (12.0, 24.0, 40.0)  # total roundtrip basis points; fixed-exit re-pricing
MS = 60000
SCHEMA = "timi-ml-v557-barrier-feasibility-cost-research-v1"


@dataclass(frozen=True)
class Settings:
    fee_bps_per_side: float = 10.0
    slippage_bps_per_side: float = 2.0
    minimum_edge_bps: float = 2.0
    min_net_rr: float = 1.25

    @property
    def roundtrip_bps(self):
        return 2.0 * (self.fee_bps_per_side + self.slippage_bps_per_side)

    def check(self):
        if not all(map(math.isfinite, (self.fee_bps_per_side,self.slippage_bps_per_side,
                                     self.minimum_edge_bps,self.min_net_rr))):
            raise ValueError("Non-finite settings")
        if min(self.fee_bps_per_side,self.slippage_bps_per_side,self.minimum_edge_bps)<0 or self.min_net_rr<1:
            raise ValueError("Invalid costs or risk ratio")


def barrier_geometry(atr_pct, tp_atr, sl_atr, policy, settings=Settings()):
    """Causal ATR-based fractions, no future-dependent threshold optimization.

    legacy_rr = current v556 strict TP floor.
    cost_cover = exploratory TP covering modeled roundtrip + minimum edge,
                 NOT guaranteed to meet after-cost reward/risk.
    atr_only = purely volatility-derived target, NOT guaranteed net-positive TP.
    """
    settings.check()
    if policy not in POLICIES or not np.isfinite(tp_atr) or not np.isfinite(sl_atr) or tp_atr<=0 or sl_atr<=0:
        raise ValueError("Unsupported geometry")
    a=np.asarray(atr_pct,float)
    sl=np.maximum(sl_atr*a,0.0008)
    base=tp_atr*a
    cost=settings.roundtrip_bps/10000.0
    edge=settings.minimum_edge_bps/10000.0
    if policy=="legacy_rr":
        tp=np.maximum.reduce((base,np.full_like(base,cost+edge),cost+settings.min_net_rr*(sl+cost)))
    elif policy=="cost_cover":
        tp=np.maximum(base,cost+edge)
    else:
        tp=base.copy()
    rr=(tp-cost)/(sl+cost)
    allowed=(np.isfinite(a)&(a>=0)&np.isfinite(tp)&np.isfinite(sl)&(sl<=.02)&(tp<=.035)&(tp>0))
    return {"tp_frac":tp,"sl_frac":sl,"net_rr":rr,"valid_geometry":allowed,
            "cost_bps":settings.roundtrip_bps}


def simulate_ohlc(raw,feat,horizon,tp_atr,sl_atr,policy,settings=Settings()):
    """Reference entry next minute OPEN, exit TP/SL or last minute CLOSE.

    No missing intervals, no same-bar lookahead: SL wins intrabar ambiguity.
    Actual spread, order queue and stop-gap execution are UNKNOWN; stop and TP
    fills at their barrier levels are only hypothetical (can be optimistic).
    """
    if horizon not in HORIZONS:
        raise ValueError("Unsupported holding horizon")
    ts=raw.ts.to_numpy(np.int64)
    ft=feat.ts.to_numpy(np.int64)
    if len(ts)<2 or len(ft)<1 or (np.diff(ts)<=0).any() or (np.diff(ft)<=0).any():
        raise ValueError("Timestamps not increasing")
    p=np.searchsorted(ts,ft)
    if (p>=len(ts)).any() or not np.array_equal(ts[p],ft):
        raise ValueError("Features not aligned to original candles")
    end=p+horizon
    within=end<len(ts)
    seg=raw.segment.to_numpy()
    complete=np.zeros(len(ft),bool)
    complete[within]=(ts[end[within]]==ft[within]+horizon*MS)&(seg[end[within]]==seg[p[within]])
    opens=raw.open.to_numpy(float)
    closes=raw.close.to_numpy(float)
    hi=raw.high.to_numpy(float)
    lo=raw.low.to_numpy(float)
    entry=np.full(len(ft),np.nan)
    expiry=entry.copy()
    entry[complete]=opens[p[complete]+1]
    expiry[complete]=closes[end[complete]]
    geom=barrier_geometry(feat.atr14_pct.to_numpy(float),tp_atr,sl_atr,policy,settings)
    tp=geom["tp_frac"];sl=geom["sl_frac"]
    valid=complete & geom["valid_geometry"] & np.isfinite(entry) & (entry>0) & np.isfinite(expiry) & (expiry>0)
    ltp=entry*(1+tp);lsl=entry*(1-sl)
    stp=entry*(1-tp);ssl=entry*(1+sl)
    exit_l=expiry.copy();exit_s=expiry.copy()
    flag_l=np.zeros(len(ft),np.int8);flag_s=np.zeros(len(ft),np.int8)
    step_l=np.full(len(ft),horizon,np.int16);step_s=step_l.copy()
    active_l=valid.copy();active_s=valid.copy()
    # Maximum favorable excursion AFTER the next OPEN; includes high/low of the
    # completed horizon only. Never used in prediction or causal geometry.
    mfe_l=np.zeros(len(ft),float)
    mfe_s=np.zeros(len(ft),float)
    for k in range(1,horizon+1):
        cursor=np.minimum(p+k,len(ts)-1)
        cur_h=hi[cursor];cur_l=lo[cursor]
        with np.errstate(invalid="ignore",divide="ignore"):
            mfe_l=np.maximum(mfe_l,(cur_h/entry-1)*10000)
            mfe_s=np.maximum(mfe_s,(1-cur_l/entry)*10000)
        lstop=active_l & (cur_l<=lsl)
        lwin=active_l & ~lstop & (cur_h>=ltp)
        sstop=active_s & (cur_h>=ssl)
        swin=active_s & ~sstop & (cur_l<=stp)
        exit_l[lstop]=lsl[lstop];exit_l[lwin]=ltp[lwin]
        exit_s[sstop]=ssl[sstop];exit_s[swin]=stp[swin]
        flag_l[lstop]=-1;flag_l[lwin]=1
        flag_s[sstop]=-1;flag_s[swin]=1
        step_l[lstop|lwin]=k;step_s[sstop|swin]=k
        active_l &= ~(lstop|lwin)
        active_s &= ~(sstop|swin)
    with np.errstate(invalid="ignore",divide="ignore"):
        gross_l=(exit_l/entry-1)*10000
        gross_s=(1-exit_s/entry)*10000
    for arr in (gross_l,gross_s,mfe_l,mfe_s):
        arr[~valid]=np.nan
    return {"valid":valid,"tp_bps":tp*10000,"sl_bps":sl*10000,
            "net_rr":geom["net_rr"],"long_flag":flag_l,"short_flag":flag_s,
            "long_gross_bps":gross_l,"short_gross_bps":gross_s,
            "long_step":step_l,"short_step":step_s,
            "long_mfe_bps":mfe_l,"short_mfe_bps":mfe_s}


def summarize(events,directions,indices,settings=Settings()):
    """Overlapping event labels, NOT realized portfolio PnL or trading precision."""
    idx=np.asarray(indices,int)
    direction=np.asarray(directions,np.int8)[idx]
    eligible=(direction!=1)&events["valid"][idx]
    ids=idx[eligible];d=direction[eligible]
    result={"candidate_events":int(len(ids)),"AL":int(np.sum(d==2)),
            "SAT_directional_not_executable_spot_short":int(np.sum(d==0)),
            "baseline_cost_bps":settings.roundtrip_bps}
    if not len(ids):
        return {**result,"TP_first":0,"SL_first":0,"timeout":0,
                "mean_net_bps":None,"net_positive_rate":None,"median_tp_bps":None,
                "median_sl_bps":None,"median_max_favorable_excursion_bps":None,
                "tp_touched_regardless_of_order_rate":None,"after_cost_rr_pass_rate":None,
                "tp_net_positive_at_barrier_rate":None,"reprice_same_fills":{}}
    side=np.where(d==2,events["long_flag"][ids],events["short_flag"][ids])
    gross=np.where(d==2,events["long_gross_bps"][ids],events["short_gross_bps"][ids])
    mfe=np.where(d==2,events["long_mfe_bps"][ids],events["short_mfe_bps"][ids])
    tp=events["tp_bps"][ids];sl=events["sl_bps"][ids]
    net=gross-settings.roundtrip_bps
    values={"TP_first":int(np.sum(side==1)),"SL_first":int(np.sum(side==-1)),
            "timeout":int(np.sum(side==0)),
            "mean_net_bps":round(float(np.mean(net)),4),
            "net_positive_rate":round(float(np.mean(net>0)),6),
            "median_tp_bps":round(float(np.median(tp)),4),
            "median_sl_bps":round(float(np.median(sl)),4),
            "median_max_favorable_excursion_bps":round(float(np.median(mfe)),4),
            "tp_touched_regardless_of_order_rate":round(float(np.mean(mfe>=tp)),6),
            "after_cost_rr_pass_rate":round(float(np.mean(events["net_rr"][ids]>=settings.min_net_rr)),6),
            "tp_net_positive_at_barrier_rate":round(float(np.mean(tp>settings.roundtrip_bps)),6),
            "reprice_same_fills":{str(int(cost)):{
                "mean_net_bps":round(float(np.mean(gross-cost)),4),
                "net_positive_rate":round(float(np.mean(gross-cost>0)),6),
                "TP_first_but_net_nonpositive":int(np.sum((side==1)&(tp<=cost)))
            } for cost in SCENARIOS}}
    if values["TP_first"]+values["SL_first"]+values["timeout"]!=len(ids):
        raise AssertionError("Mutually exclusive exits do not sum")
    return {**result,**values}


def long_spot_nonoverlap(events,directions,indices,ts,settings=Settings()):
    """One reference long-at-a-time per strategy, per fold; still NOT actual fills.

    SAT directions are expressly excluded because Binance Spot short borrowing
    and short order execution are not modeled. Do not use to claim live edge.
    """
    ix=np.asarray(indices,int)
    ix=ix[(directions[ix]==2)&events["valid"][ix]]
    selected=[];next_t=-1
    for i in ix:
        if ts[i]<next_t:
            continue
        selected.append(int(i))
        next_t=int(ts[i]+(int(events["long_step"][i])+1)*MS)
    if not selected:
        return {"trades":0,"win_rate":None,"mean_net_bps":None,
                "profit_factor":None,"max_drawdown_pct":None,
                "sharpe_ratio":None,"sharpe_reason":"Event samples irregular and fills only theoretical"}
    ns=events["long_gross_bps"][selected]-settings.roundtrip_bps
    pos=float(ns[ns>0].sum());loss=float(-ns[ns<0].sum())
    # Unit notional equity at reference exit; no live sizing, borrowing or fees beyond model.
    eq=np.cumprod(1+ns/10000)
    peak=np.maximum.accumulate(np.r_[1.0,eq])
    curve=np.r_[1.0,eq]
    dd=float(100*np.max(1-curve/peak))
    return {"trades":len(selected),"win_rate":round(float(np.mean(ns>0)),6),
            "mean_net_bps":round(float(np.mean(ns)),4),
            "profit_factor":round(pos/loss,4) if loss>0 else None,
            "max_drawdown_pct":round(dd,4),
            "sharpe_ratio":None,
            "sharpe_reason":"Irregular, overlapping strategy alternatives and hypothetical OHLC fills; daily Sharpe not meaningful"}


def research(raw,feat,settings=Settings(),*,policies=POLICIES,
             horizons=HORIZONS,barriers=BARRIERS,strategies=None):
    settings.check()
    if len(feat)<1000:
        raise ValueError("Too few complete features")
    dirs_all=v556.candidate_directions(raw,feat) if strategies is None else strategies
    ts=feat.ts.to_numpy(np.int64)
    out=[]
    for horizon in horizons:
        aligned=v556.align_closes(ts,horizon)
        for bar_name,tp_atr,sl_atr in barriers:
            for policy in policies:
                ev=simulate_ohlc(raw,feat,horizon,tp_atr,sl_atr,policy,settings)
                for strategy,dirs in dirs_all.items():
                    if len(dirs)!=len(feat):
                        raise ValueError("Candidate array size mismatch")
                    eligible=(dirs!=1)&aligned&ev["valid"]
                    folds=v556.chronological_folds(ts,eligible,horizon)
                    names=("fit","early_stop","calibration","policy","diagnostic")
                    diagnostics={n:summarize(ev,dirs,ids,settings) for n,ids in zip(names,folds)}
                    for n in ("policy","diagnostic"):
                        diagnostics[n]["long_spot_nonoverlap"] = long_spot_nonoverlap(ev,dirs,folds[names.index(n)],ts,settings)
                    out.append({"strategy":strategy,"horizon_minutes":horizon,
                                "barrier":bar_name,"geometry_policy":policy,
                                "geometry_is_exploratory_only":policy!="legacy_rr",
                                "events_total":int(np.sum(eligible)),
                                "split_counts":dict(zip(names,map(len,folds))),
                                "folds":diagnostics})
    return out


def run(first,last,outdir,*,funding_mode="off",settings=Settings(),download=None,
        funding_download=None,api_fetch=None):
    """Produces historical diagnostics; deliberately does NOT train a model."""
    import timi_ml_v54_history_train as v54
    import timi_ml_v551_research as v551
    import timi_ml_v553_scalp as v553
    import timi_ml_v554_scalp as v554
    months=v54.month_range(first,last)
    raw=v54.historical_frame(months,download=(download or v54.read_month))
    quality=v556.audit_historical_candles(raw)
    feat,cols=v54.features_targets(raw)
    feat=v551.enrich_cvd_regime(raw,feat)
    feat=v554.additional_features(raw,feat)
    feat,funding=v553.add_funding(feat,first,last,mode=funding_mode,
                                  archive_download=funding_download,api_fetch=api_fetch)
    feat=v556.causal_features(raw,feat)
    experiments=research(raw,feat,settings)
    outcomes={k:int(sum(x["folds"]["fit"]["candidate_events"] for x in experiments if x["geometry_policy"]==k))
              for k in POLICIES}
    report={"engine":"TiMi ML v5.5.7 — ATR target feasibility and cost sensitivity",
            "schema":SCHEMA,"developer":"Suat DOĞU","symbol":"BTCUSDT","market":"Binance Spot",
            "source":"Binance Vision public 1-minute OHLCV with checksums",
            "months":months,"candles":len(raw),"feature_rows":len(feat),
            "strategies":list(v556.STRATEGIES),"horizons":list(HORIZONS),
            "barriers":[x[0] for x in BARRIERS],"geometry_policies":list(POLICIES),
            "experiments":len(experiments),"cost_scenarios_roundtrip_bps":list(SCENARIOS),
            "current_roundtrip_cost_bps":settings.roundtrip_bps,
            "minimum_edge_bps":settings.minimum_edge_bps,
            "minimum_net_reward_risk":settings.min_net_rr,
            "counts_by_geometry_fit_candidates_overlapping_events":outcomes,
            "data_quality":quality,"funding":funding,
            "split":"50/10/10/10/20 UTC chronological with 17-minute pre-boundary purge",
            "training":"NOT_PERFORMED_BY_DESIGN",
            "risk_policy":"No candidate is automatically promoted to a live model or order signal",
            "fixed_exit_repricing_not_new_execution_simulation":True,
            "intrabar_simultaneous_TP_SL":"SL first, conservative OHLC convention",
            "limit_warning":"Barrier price reference fills do NOT simulate order queue, spread, gaps, real slippage or actual stop execution",
            "short_warning":"SAT directional only; unexecutable Binance Spot short excluded from nonoverlap reference metrics",
            "overlap_warning":"Label counts are overlapping hypothetical opportunities, not independent trades",
            "research_period_previously_inspected_no_fresh_out_of_sample":True,
            "independent_future_forward_test_required":True,
            "model_ready_for_live":False,"live_inference_enabled":False,"orders_enabled":False,
            "status":"EXPLORATORY_BARRIER_DIAGNOSTICS_COMPLETE"}
    out=Path(outdir);out.mkdir(parents=True,exist_ok=True)
    (out/"report.json").write_text(json.dumps(report,indent=2,ensure_ascii=False,allow_nan=False),encoding="utf-8")
    (out/"data_quality.json").write_text(json.dumps(quality,indent=2,ensure_ascii=False,allow_nan=False),encoding="utf-8")
    (out/"barrier_feasibility.json").write_text(json.dumps(experiments,indent=2,ensure_ascii=False,allow_nan=False),encoding="utf-8")
    with (out/"barrier_policy_comparison.csv").open("w",newline="",encoding="utf-8") as fh:
        names=["strategy","horizon_minutes","barrier","geometry_policy","fold","candidate_events",
               "TP_first","SL_first","timeout","median_tp_bps","median_sl_bps",
               "median_max_favorable_excursion_bps","tp_touched_regardless_of_order_rate",
               "after_cost_rr_pass_rate","mean_net_bps","net_positive_rate",
               "long_spot_nonoverlap_trades","long_spot_nonoverlap_profit_factor"]
        w=csv.DictWriter(fh,fieldnames=names);w.writeheader()
        for e in experiments:
            for fold in ("fit","early_stop","calibration","policy","diagnostic"):
                d=e["folds"][fold];q=d.get("long_spot_nonoverlap",{})
                row={k:e[k] for k in ("strategy","horizon_minutes","barrier","geometry_policy")}
                row.update({"fold":fold,**{k:d.get(k) for k in names if k in d},
                            "long_spot_nonoverlap_trades":q.get("trades"),
                            "long_spot_nonoverlap_profit_factor":q.get("profit_factor")})
                w.writerow(row)
    print(f"TiMi v5.5.7 {report['status']} | {len(experiments)} experiments | live/orders OFF",flush=True)
    return report


def main():
    p=argparse.ArgumentParser(description="TiMi v5.5.7 read-only barrier feasibility lab")
    p.add_argument("--from-month",default="2026-05")
    p.add_argument("--to-month",default="2026-10")
    p.add_argument("--funding-mode",choices=("off","optional","required"),default="off")
    p.add_argument("--fee-bps",type=float,default=10.)
    p.add_argument("--slippage-bps",type=float,default=2.)
    p.add_argument("--output",default="timi_v557_artifacts")
    args=p.parse_args()
    run(args.from_month,args.to_month,args.output,funding_mode=args.funding_mode,
        settings=Settings(args.fee_bps,args.slippage_bps))

if __name__=="__main__":
    main()
