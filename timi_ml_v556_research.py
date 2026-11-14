"""TiMi ML v5.5.6 — causal features, label funnel and calibrated meta-research.

Developer: Suat DOĞU. Strictly OFFLINE, READ-ONLY historical BTCUSDT Spot.
No order placement, no private API keys, no live inference, no database writes.
May-Oct 2026 has been researched already: results are EXPLORATORY, not fresh OOS.

Requires unmodified sibling v5.4 / v5.5 / v5.5.1 / v5.5.3 / v5.5.4 / v5.5.5 files.
A reported high predicted probability is meaningful only when the CALIBRATION
and POLICY folds show sufficient examples and net edge after trading costs.
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

HORIZONS = (1, 3, 5, 10, 15)
BARRIERS = (("atr_1p5_0p5", 1.5, .5), ("atr_2p0_0p7", 2., .7))
STRATEGIES = ("trend_pullback", "vwap_reversion", "volume_breakout",
              "momentum_acceleration", "squeeze_breakout")
EXTRA_COLS = ("momentum3", "momentum5", "acceleration1", "realized_vol10",
              "realized_vol30", "volume_ratio20", "volume_jump3", "green_streak3",
              "signed_volume5", "ema8_slope5", "ema21_slope5", "macd_pct",
              "macd_hist_pct", "bandwidth_ratio120", "return_lag4", "return_lag5",
              "pct_return1", "log_return1", "log_return_lag1",
              "log_return_lag2", "log_return_lag3", "range_over_close",
              "body_over_range", "upper_wick_over_close", "lower_wick_over_close",
              "volume_z20_prior", "hour_sin_utc", "hour_cos_utc",
              "weekday_sin_utc", "weekday_cos_utc")
SCHEMA = "timi-ml-v556-causal-label-diagnostic-research-v1"
# Conservative cut separation; 17 minutes protects even 15m outcomes.
PURGE_MINUTES = 17


@dataclass(frozen=True)
class Config:
    fee_bps: float = 10.
    slippage_bps: float = 2.
    minimum_edge_bps: float = 2.
    minimum_reward_risk: float = 1.25
    min_fit: int = 100
    min_early: int = 25
    min_calibration: int = 35
    min_policy: int = 30
    min_diagnostic: int = 40
    max_fit_events: int = 45000
    min_validation_trades: int = 20
    min_policy_profit_factor: float = 1.10
    min_policy_mean_net_bps: float = 2.
    max_policy_drawdown_pct: float = 12.

    def check(self):
        xs = (self.fee_bps, self.slippage_bps, self.minimum_edge_bps,
              self.minimum_reward_risk, self.min_policy_profit_factor,
              self.min_policy_mean_net_bps, self.max_policy_drawdown_pct)
        if not all(math.isfinite(x) for x in xs):
            raise ValueError("Non-finite research settings")
        if min(self.fee_bps, self.slippage_bps, self.minimum_edge_bps) < 0:
            raise ValueError("Negative costs or minimum edge")
        if self.minimum_reward_risk < 1 or self.min_policy_profit_factor < 1:
            raise ValueError("Risk thresholds too low")
        if not 0 <= self.max_policy_drawdown_pct <= 100:
            raise ValueError("Invalid drawdown guard")
        if min(self.min_fit,self.min_early,self.min_calibration,self.min_policy,self.min_diagnostic) < 10:
            raise ValueError("Insufficient fold minimum")
        if self.max_fit_events < 200 or self.min_validation_trades < 10:
            raise ValueError("Invalid model limits")

    @property
    def roundtrip_bps(self):
        return 2 * (self.fee_bps + self.slippage_bps)



def audit_historical_candles(raw: pd.DataFrame, *, max_examples=10):
    """Fail closed on malformed candles, report gaps, and NEVER synthesize bars.

    Binance Vision timestamps are UTC 1m OPEN times in milliseconds, already
    normalized by v5.4. Missing intervals are separate segments, not 0-volume
    candles. Existing genuine 0-volume rows remain genuine observations.
    """
    required = ("ts", "open", "high", "low", "close", "volume", "taker_buy_volume", "segment")
    missing = [c for c in required if c not in raw.columns]
    if missing:
        raise ValueError(f"Missing historical fields: {missing}")
    if len(raw) < 2:
        raise ValueError("At least two genuine closed candles are required")
    ts = raw.ts.to_numpy(dtype=np.int64)
    delta = np.diff(ts)
    if np.any(ts % 60000 != 0) or np.any(delta <= 0):
        raise ValueError("Unaligned, out-of-order or duplicate timestamps")
    vals = raw[["open", "high", "low", "close", "volume", "taker_buy_volume"]].to_numpy(dtype=float)
    if not np.isfinite(vals).all():
        raise ValueError("Non-finite OHLCV or taker volume")
    o, h, l, c, v, buy = vals.T
    if (np.any(o<=0) or np.any(h<=0) or np.any(l<=0) or np.any(c<=0)
        or np.any(h<np.maximum.reduce((o,c,l)))
        or np.any(l>np.minimum(o,c))
        or np.any(v<0) or np.any(buy<0) or np.any(buy>v+1e-6)):
        raise ValueError("Invalid OHLCV/taker geometry")
    if "trades" in raw and np.any(raw.trades.to_numpy(float)<0):
        raise ValueError("Negative trade count")
    if "quote_volume" in raw and np.any(raw.quote_volume.to_numpy(float)<0):
        raise ValueError("Negative quote volume")
    seg = raw.segment.to_numpy()
    is_break = delta != 60000
    if np.any((seg[1:]!=seg[:-1])!=is_break):
        raise ValueError("Segments must split exactly at missing-minute gaps")
    if np.any(delta % 60000 != 0):
        raise ValueError("Gap has a non-minute interval")
    gap_at = np.flatnonzero(is_break)
    gap_rows = []
    for i in gap_at[:max_examples]:
        gap_rows.append({
            "previous_open_utc":pd.Timestamp(int(ts[i]),unit="ms",tz="UTC").isoformat(),
            "next_open_utc":pd.Timestamp(int(ts[i+1]),unit="ms",tz="UTC").isoformat(),
            "missing_minutes":int(delta[i]//60000-1),
        })
    return {
        "timezone":"UTC","candle_size_minutes":1,
        "rows_observed":int(len(raw)),
        "first_open_utc":pd.Timestamp(int(ts[0]),unit="ms",tz="UTC").isoformat(),
        "last_open_utc":pd.Timestamp(int(ts[-1]),unit="ms",tz="UTC").isoformat(),
        "gap_events":int(len(gap_at)),
        "missing_minutes":int(np.sum(delta[is_break]//60000-1)),
        "max_gap_minutes":int(max([int(delta[i]//60000-1) for i in gap_at],default=0)),
        "gap_examples":gap_rows,
        "zero_volume_genuine_rows":int(np.sum(v==0)),
        "synthetic_candles_added":0,
        "missing_intervals_policy":"SEGMENT_AND_EXCLUDE_CROSS_GAP_LABELS_NO_FFILL",
        "feature_time_reference":"CURRENT_GENUINE_1M_CANDLE_CLOSE_UTC",
        "local_display_timezone":"Europe/Istanbul (display only)",
    }


def causal_features(raw: pd.DataFrame, feat: pd.DataFrame):
    """Use ONLY current/previous CLOSED candles. Reset at missing-minute segments.

    The taker-buy ratio is a trade-flow proxy; it is NOT whale identification,
    archived order-book imbalance or an actual CVD order-flow feed.
    """
    if not raw.ts.is_monotonic_increasing or raw.ts.duplicated().any():
        raise ValueError("Raw timestamps unordered/duplicated")
    if feat.ts.duplicated().any():
        raise ValueError("Duplicate feature timestamps")
    if not set(["ts", "segment", "close", "volume", "taker_buy_volume"]).issubset(raw.columns):
        raise ValueError("Missing required Binance historical fields")
    blocks = []
    for _, g in raw.groupby("segment", sort=False):
        c = g.close.astype(float)
        vol = g.volume.astype(float)
        buy = g.taker_buy_volume.astype(float)
        lr = np.log(c / c.shift(1))
        ema8 = c.ewm(span=8, min_periods=8, adjust=False).mean()
        ema21 = c.ewm(span=21, min_periods=21, adjust=False).mean()
        ema12 = c.ewm(span=12, min_periods=12, adjust=False).mean()
        ema26 = c.ewm(span=26, min_periods=26, adjust=False).mean()
        macd = ema12 - ema26
        macd_signal = macd.ewm(span=9, min_periods=9, adjust=False).mean()
        base_vol = vol.shift(1).rolling(20, min_periods=20).mean().replace(0,np.nan)
        center = c.rolling(20, min_periods=20).mean()
        bw = 4*c.rolling(20,min_periods=20).std(ddof=0) / center.replace(0,np.nan)
        signs = (c > c.shift(1)).astype(float)
        o = g.open.astype(float)
        h = g.high.astype(float)
        l = g.low.astype(float)
        rng = h - l
        # Minute stamp is the OPEN; use the current completed candle's UTC CLOSE.
        close_minute = g.ts.to_numpy(np.int64)//60000 + 1
        minute_of_day = close_minute % 1440
        weekday = ((close_minute // 1440) + 3) % 7  # 1970-01-01 is Thursday
        volume_mean = vol.shift(1).rolling(20,min_periods=20).mean()
        volume_std = vol.shift(1).rolling(20,min_periods=20).std(ddof=0).replace(0,np.nan)
        x = pd.DataFrame({
            "ts":g.ts,
            "momentum3": np.log(c / c.shift(3)),
            "momentum5": np.log(c / c.shift(5)),
            "acceleration1": lr - lr.shift(1),
            "realized_vol10": lr.rolling(10,min_periods=10).std(ddof=0),
            "realized_vol30": lr.rolling(30,min_periods=30).std(ddof=0),
            "volume_ratio20": vol / base_vol,
            "volume_jump3": np.log1p(vol) - np.log1p(vol.shift(3)),
            "green_streak3": signs.rolling(3,min_periods=3).sum(),
            "signed_volume5": (2*buy-vol).rolling(5,min_periods=5).sum() / vol.rolling(5,min_periods=5).sum().replace(0,np.nan),
            "ema8_slope5": (ema8 / ema8.shift(5))-1,
            "ema21_slope5": (ema21 / ema21.shift(5))-1,
            "macd_pct": macd / c,
            "macd_hist_pct": (macd-macd_signal) / c,
            "bandwidth_ratio120": bw / bw.shift(1).rolling(120,min_periods=120).median().replace(0,np.nan),
            "return_lag4":lr.shift(4),
            "return_lag5":lr.shift(5),
            "pct_return1": c/c.shift(1)-1,
            "log_return1":lr,
            "log_return_lag1":lr.shift(1),
            "log_return_lag2":lr.shift(2),
            "log_return_lag3":lr.shift(3),
            "range_over_close":rng/c,
            "body_over_range":((c-o)/rng.replace(0,np.nan)).fillna(0.),
            "upper_wick_over_close":(h-np.maximum(o,c))/c,
            "lower_wick_over_close":(np.minimum(o,c)-l)/c,
            "volume_z20_prior":(vol-volume_mean)/volume_std,
            "hour_sin_utc":np.sin(2*np.pi*minute_of_day/1440),
            "hour_cos_utc":np.cos(2*np.pi*minute_of_day/1440),
            "weekday_sin_utc":np.sin(2*np.pi*weekday/7),
            "weekday_cos_utc":np.cos(2*np.pi*weekday/7),
        })
        blocks.append(x)
    extra = pd.concat(blocks,ignore_index=True)
    frame = feat.merge(extra,on="ts",how="left",validate="one_to_one",sort=False)
    frame[list(EXTRA_COLS)] = frame[list(EXTRA_COLS)].replace([np.inf,-np.inf],np.nan)
    return frame.dropna(subset=list(EXTRA_COLS)).reset_index(drop=True)


def candidate_directions(raw, feat):
    """5 predeclared strategy families; deterministic closed-bar rules."""
    import timi_ml_v555_meta as old
    base = old.strategy_candidates(raw,feat)
    trend = feat.regime_trend.to_numpy(float)==1
    highvol = feat.regime_high_vol.to_numpy(float)==1
    mom3=feat.momentum3.to_numpy(float)
    acc=feat.acceleration1.to_numpy(float)
    em=feat.ema8_21_pct.to_numpy(float)
    vol=feat.volume_ratio20.to_numpy(float)
    hist=feat.macd_hist_pct.to_numpy(float)
    rsi=feat.rsi14.to_numpy(float)
    signed=feat.signed_volume5.to_numpy(float)
    new_long=(trend & ~highvol & (mom3>0) & (acc>0) & (em>0) &
              (hist>0) & (vol>1.25) & (rsi>45) & (rsi<72) & (signed>0))
    new_short=(trend & ~highvol & (mom3<0) & (acc<0) & (em<0) &
               (hist<0) & (vol>1.25) & (rsi<55) & (rsi>28) & (signed<0))
    bwr=feat.bandwidth_ratio120.to_numpy(float)
    # A valid squeeze is a low bandwidth relative to its OWN PRIOR history.
    raw_ts=raw.ts.to_numpy(np.int64);p=np.searchsorted(raw_ts,feat.ts.to_numpy(np.int64))
    if (p>=len(raw_ts)).any() or not np.array_equal(raw_ts[p],feat.ts.to_numpy(np.int64)):
        raise ValueError("Feature/raw alignment invalid")
    highs,lows=old.trailing_extremes(raw,feat)
    px=raw.close.to_numpy(float)[p]
    squeeze= (bwr<.85) & (vol>1.5) & ~highvol
    sq_long=squeeze & (px>highs) & (hist>0) & (signed>0)
    sq_short=squeeze & (px<lows) & (hist<0) & (signed<0)
    for name,long,short in (("momentum_acceleration",new_long,new_short),
                            ("squeeze_breakout",sq_long,sq_short)):
        if (long & short).any():
            raise ValueError("Ambiguous AL/SAT candidate")
        base[name] = np.where(long,2,np.where(short,0,1)).astype(np.int8)
    return base


def align_closes(ts, horizon):
    if horizon not in HORIZONS:
        raise ValueError("Unsupported research horizon")
    t=np.asarray(ts,np.int64)
    if (t % 60000 != 0).any():
        raise ValueError("Non-minute timestamps")
    return ((t//60000+1)%horizon)==0


def chronological_folds(ts, eligible, horizon):
    """50/10/10/10/20, contiguous clock folds; 17m pre-boundary purge.

    No randomized shuffle. Last event in each earlier fold cannot overlap
    the first event of the following fold, even for 15m outcomes.
    """
    if horizon not in HORIZONS:
        raise ValueError("Unsupported research horizon")
    ts=np.asarray(ts,np.int64);good=np.asarray(eligible,bool)
    if len(ts)<1000 or len(ts)!=len(good) or np.any(np.diff(ts)<=0):
        raise ValueError("Invalid chronological samples")
    cuts=[int(ts[int(len(ts)*f)]) for f in (.5,.6,.7,.8)]
    purge=PURGE_MINUTES*60000
    out=(np.flatnonzero(good & (ts<cuts[0]-purge)),
         np.flatnonzero(good & (ts>=cuts[0]) & (ts<cuts[1]-purge)),
         np.flatnonzero(good & (ts>=cuts[1]) & (ts<cuts[2]-purge)),
         np.flatnonzero(good & (ts>=cuts[2]) & (ts<cuts[3]-purge)),
         np.flatnonzero(good & (ts>=cuts[3])))
    for a,b in zip(out,out[1:]):
        if len(a) and len(b) and not ts[a[-1]]+(horizon+1)*60000<ts[b[0]]:
            raise AssertionError("Folds overlap by future labels")
    return out


def label_events(raw, feat, horizon, cfg:Config, tp_atr=1.5, sl_atr=.5):
    """Next 1m OPEN reference, OHLC TP/SL, same-bar SL first, no gap synthesis.

    Timeouts counted separately; both hit in the same minute => SL (worst case).
    A completed OHLC candle is NOT proof of actual exchange fill/queue execution.
    """
    cfg.check()
    if horizon not in HORIZONS or tp_atr<=0 or sl_atr<=0:
        raise ValueError("Invalid barrier geometry")
    rt=raw.ts.to_numpy(np.int64);ft=feat.ts.to_numpy(np.int64)
    if (len(rt)<2 or (np.diff(rt)<=0).any() or (np.diff(ft)<=0).any()):
        raise ValueError("Non-monotonic input")
    p=np.searchsorted(rt,ft)
    if (p>=len(rt)).any() or not np.array_equal(rt[p],ft):
        raise ValueError("Feature timestamp mismatch")
    end=p+horizon
    inside=end<len(rt)
    continuous=np.zeros(len(feat),bool)
    segment=raw.segment.to_numpy()
    continuous[inside]=(rt[end[inside]]==ft[inside]+horizon*60000) & (segment[end[inside]]==segment[p[inside]])
    entry=np.full(len(feat),np.nan,float)
    endpoint=entry.copy()
    opens=raw.open.to_numpy(float);closes=raw.close.to_numpy(float)
    entry[continuous]=opens[p[continuous]+1]
    endpoint[continuous]=closes[end[continuous]]
    atr=feat.atr14_pct.to_numpy(float)
    cost=cfg.roundtrip_bps/10000
    sl=np.maximum(sl_atr*atr,.0008)
    # Preserve prior v5.5.5 economics exactly for apples-to-apples diagnostics.
    tp=np.maximum.reduce((tp_atr*atr, cost+cfg.minimum_edge_bps/10000+np.zeros(len(feat)),
                          cost+cfg.minimum_reward_risk*(sl+cost)))
    valid=(continuous & np.isfinite(entry) & (entry>0) & np.isfinite(endpoint) &
           np.isfinite(atr) & np.isfinite(tp) & np.isfinite(sl) &
           (sl<=.02) & (tp<=.035))
    buy_tp=entry*(1+tp);buy_sl=entry*(1-sl)
    sell_tp=entry*(1-tp);sell_sl=entry*(1+sl)
    out_l=endpoint.copy();out_s=endpoint.copy()
    side_l=np.zeros(len(feat),np.int8);side_s=side_l.copy()
    active_l=valid.copy();active_s=valid.copy()
    hi=raw.high.to_numpy(float);lo=raw.low.to_numpy(float)
    step_l=np.full(len(feat),horizon,np.int16);step_s=step_l.copy()
    for k in range(1,horizon+1):
        cursor=np.minimum(p+k,len(rt)-1)
        h=hi[cursor];l=lo[cursor]
        l_stop=active_l & (l<=buy_sl)
        l_tp=active_l & ~l_stop & (h>=buy_tp)
        s_stop=active_s & (h>=sell_sl)
        s_tp=active_s & ~s_stop & (l<=sell_tp)
        out_l[l_stop]=buy_sl[l_stop];out_l[l_tp]=buy_tp[l_tp]
        out_s[s_stop]=sell_sl[s_stop];out_s[s_tp]=sell_tp[s_tp]
        side_l[l_stop]=-1;side_l[l_tp]=1
        side_s[s_stop]=-1;side_s[s_tp]=1
        step_l[l_stop|l_tp]=k;step_s[s_stop|s_tp]=k
        active_l &= ~(l_stop|l_tp)
        active_s &= ~(s_stop|s_tp)
    with np.errstate(invalid="ignore",divide="ignore"):
        net_l=out_l/entry-1-cost
        net_s=(entry-out_s)/entry-cost
    net_l[~valid]=np.nan;net_s[~valid]=np.nan
    return {"valid":valid,"entry":entry,"long_side":side_l,"short_side":side_s,
            "long_exit":out_l,"short_exit":out_s,"long_k":step_l,"short_k":step_s,
            "long_net":net_l,"short_net":net_s,"tp_frac":tp,"sl_frac":sl,
            "tp_long":buy_tp,"sl_long":buy_sl,"tp_short":sell_tp,"sl_short":sell_sl}


def _pct(v):
    return round(float(v),5) if math.isfinite(float(v)) else None


def label_funnel(events, dirs, indices, cfg:Config):
    """Diagnostic only; avoid optimizing policies from diagnostic fold labels."""
    idx=np.asarray(indices,dtype=int)
    direction=np.asarray(dirs,np.int8)[idx]
    relevant=events["valid"][idx] & (direction!=1)
    ids=idx[relevant]
    direct=direction[relevant]
    side=np.where(direct==2,events["long_side"][ids],events["short_side"][ids])
    net=np.where(direct==2,events["long_net"][ids],events["short_net"][ids])
    cost=cfg.roundtrip_bps
    result={"signals_checked":int(len(idx)),"valid_candidates":int(len(ids)),
            "AL":int(np.sum(direct==2)),"SAT_direction_only":int(np.sum(direct==0)),
            "TP_first":int(np.sum(side==1)),"SL_first":int(np.sum(side==-1)),
            "timeout":int(np.sum(side==0)),
            "TP_positive_net":int(np.sum((side==1)&(net>0))),
            "positive_net_any_exit":int(np.sum(net>0)),
            "negative_net_any_exit":int(np.sum(net<0)),
            "zero_net_any_exit":int(np.sum(net==0)),
            "roundtrip_cost_bps":cost}
    if len(ids):
        result.update(tp_rate=_pct(np.mean(side==1)),net_win_rate=_pct(np.mean(net>0)),
                      mean_net_bps=_pct(np.mean(net)*1e4),
                      median_net_bps=_pct(np.median(net)*1e4),
                      median_tp_distance_bps=_pct(np.median(events["tp_frac"][ids])*1e4),
                      median_sl_distance_bps=_pct(np.median(events["sl_frac"][ids])*1e4),
                      median_win_timeout_rate=_pct(np.mean(side==0)))
        # Fixed-exit repricing ONLY (NOT a rerun with new barrier geometry).
        gross_bps=net*10000+cost
        result["fixed_exit_cost_sensitivity"]={str(k):{"mean_net_bps":_pct(np.mean(gross_bps-k)),
            "net_positive_rate":_pct(np.mean(gross_bps-k>0))} for k in (12,24,40)}
    else:
        result.update(tp_rate=None,net_win_rate=None,mean_net_bps=None,
                      median_net_bps=None,median_tp_distance_bps=None,
                      median_sl_distance_bps=None,median_win_timeout_rate=None,
                      fixed_exit_cost_sensitivity={})
    if result["TP_first"]+result["SL_first"]+result["timeout"]!=len(ids):
        raise AssertionError("Mutually-exclusive label counts not exhaustive")
    return result


def experiment(raw,feat,base_cols,config:Config,*,funding_usable=False,train=True):
    import timi_ml_v55_research as v55
    import timi_ml_v555_meta as old
    config.check()
    if len(feat)<6000:
        raise ValueError("Insufficient feature observations")
    ts=feat.ts.to_numpy(np.int64)
    families=candidate_directions(raw,feat)
    # Work only with data observed before each corresponding decision close.
    feature_base=list(dict.fromkeys([*base_cols, *old.v551.CVD_COLS,
                                    *old.v551.REGIME_COLS,*old.v554.EXTRA_COLS,*EXTRA_COLS,
                                    *(old.v551.FUNDING_COLS if funding_usable else [])]))
    if any(c not in feat for c in feature_base):
        raise ValueError("Missing required causal feature")
    X_base=feat[feature_base].to_numpy(dtype=np.float32)
    finite=np.isfinite(X_base).all(axis=1)
    results=[];selected=None;models=0;trainable=0
    for horizon in HORIZONS:
        align=align_closes(ts,horizon)
        for barrier_name, tp_atr, sl_atr in BARRIERS:
            ev=label_events(raw,feat,horizon,config,tp_atr,sl_atr)
            for strategy in STRATEGIES:
                dirs=families[strategy]
                eligible= (dirs!=1)&align&ev["valid"]&finite
                folds=chronological_folds(ts,eligible,horizon)
                fit,early,cal,policy,diag=folds
                counts=dict(zip(("fit","early_stop","calibration","policy","diagnostic"),
                                map(len,folds)))
                row={"strategy":strategy,"horizon_minutes":horizon,
                     "barrier":barrier_name,"status":"NOT_TRAINED_DIAGNOSTICS_ONLY" if not train else "UNCHECKED",
                     "feature_count":len(feature_base)+1,"candidate_signals":int(np.sum(dirs!=1)),
                     "eligible_candidate_events":int(np.sum(eligible)),"split_counts":counts,
                     "class_funnel":{name:label_funnel(ev,dirs,indices,config) for name,indices in zip(counts,folds)}}
                results.append(row)
                if not train:
                    continue
                if (len(fit)<config.min_fit or len(early)<config.min_early or
                    len(cal)<config.min_calibration or len(policy)<config.min_policy or
                    len(diag)<config.min_diagnostic):
                    row["status"]="INSUFFICIENT_CANDIDATE_EVENTS";continue
                y=old.tp_first_binary(ev,dirs)
                # Learn only if both positive and negative labels exist in fit AND early.
                if min(np.bincount(y[fit],minlength=2))<8 or min(np.bincount(y[early],minlength=2))<3:
                    row["status"]="INSUFFICIENT_TP_CLASS_DIVERSITY";continue
                trainable+=1
                cols=feature_base+["candidate_side"]
                df=pd.DataFrame(X_base,columns=feature_base)
                df["candidate_side"]=np.where(dirs==2,1.,np.where(dirs==0,-1.,0.)).astype(np.float32)
                if len(fit)>config.max_fit_events:
                    fit=fit[::math.ceil(len(fit)/config.max_fit_events)]
                model=old.fit_model(df.iloc[fit],y[fit],df.iloc[early],y[early],seed=556+horizon)
                if model is None:
                    row["status"]="MODEL_UNAVAILABLE";continue
                models+=1
                rawcal=old.raw_probability(model,df.iloc[cal])
                calibrator=old.fit_calibrator(rawcal,y[cal])
                if calibrator is None:
                    row["status"]="INSUFFICIENT_CALIBRATION_CLASSES";continue
                ppolraw=old.raw_probability(model,df.iloc[policy])
                pp=np.asarray(calibrator.predict(ppolraw),float)
                # Reuse old v5.5.5 predetermined probability thresholds; no tuning on DIAGNOSTIC.
                choices=[]
                for pmin,edge in old.THRESHOLDS:
                    actions,expect,reasons=old.decisions_from_probability(pp,policy,ev,dirs,pmin,edge,
                                            v55.Costs(config.fee_bps,config.slippage_bps,config.minimum_edge_bps))
                    stats,_=v55.backtest(policy,actions,feat,ev,
                                            v55.Costs(config.fee_bps,config.slippage_bps,config.minimum_edge_bps))
                    n=int(stats["trades"]); avg=stats.get("average_net_bps");pf=stats.get("profit_factor")
                    dd=stats.get("maximum_drawdown_pct")
                    good=(n>=config.min_validation_trades and avg is not None and
                          avg>config.min_policy_mean_net_bps and pf is not None and
                          pf>=config.min_policy_profit_factor and dd is not None and
                          dd<=config.max_policy_drawdown_pct)
                    score=(-1e9 if n<config.min_validation_trades or avg is None or dd is None
                           else float(avg)*min(1.4,math.sqrt(n/60.))-.05*float(dd))
                    choices.append(((int(good),score),pmin,edge,stats,
                                    old.v554.funnel_summary(reasons)))
                key,pmin,edge,stats,reason=max(choices,key=lambda x:x[0])
                row.update(status="POLICY_VALIDATION_PASSED" if key[0] else "POLICY_VALIDATION_FAILED",
                           policy_threshold=pmin,policy_net_edge_threshold_bps=edge,
                           policy_stats=stats,policy_rejections=reason,
                           policy_calibration={"raw":old.calibration_scores(y[policy],ppolraw),
                                               "calibrated":old.calibration_scores(y[policy],pp)})
                if key[0] and (selected is None or key>selected[0]):
                    selected=(key,row,model,calibrator,dirs,ev,df,diag,y,cols)
    output={"status":"NO_TRAINABLE_CANDIDATE" if models==0 else
                 ("NO_VALIDATED_EDGE_BE_KLE" if selected is None else "EXPLORATORY_POLICY_PASS_RESEARCH_ONLY"),
            "experiments":results,"model_candidates":trainable,"models_trained":models,
            "validated_model_research_only":bool(selected is not None),
            "real_orders_enabled":False,"live_model_enabled":False}
    if selected is None:
        return output,None,[]
    # Strictly after policy selection. DO NOT use diagnostic fold to choose candidates.
    _,row,model,calibrator,dirs,ev,df,diag,y,cols=selected
    rawprob=old.raw_probability(model,df.iloc[diag]);pdg=np.asarray(calibrator.predict(rawprob),float)
    costs=v55.Costs(config.fee_bps,config.slippage_bps,config.minimum_edge_bps)
    actions,expected,reasons=old.decisions_from_probability(pdg,diag,ev,dirs,
                               row["policy_threshold"],row["policy_net_edge_threshold_bps"],costs)
    stats,trades=v55.backtest(diag,actions,feat,ev,costs)
    output["selected_from_policy_only"]={k:row[k] for k in
        ("strategy","horizon_minutes","barrier","policy_threshold","policy_net_edge_threshold_bps","policy_stats")}
    output["diagnostic_previously_reused_not_fresh_oos"]={
        "metrics":stats,"calibration":old.calibration_scores(y[diag],pdg),
        "funnel":old.v554.funnel_summary(reasons),"sample_count":len(diag)}
    output["calibration_research_only"]={"feature_columns_in_order":cols,
        "isotonic_x_thresholds":calibrator.X_thresholds_.tolist(),
        "isotonic_y_thresholds":calibrator.y_thresholds_.tolist(),
        "strategy":row["strategy"],"horizon_minutes":row["horizon_minutes"],
        "no_live_model_use":True}
    lookup={v55.iso(int(ts[i]+60000)):j for j,i in enumerate(diag)}
    for trade in trades:
        j=lookup.get(trade.get("signal_close_utc"))
        if j is not None:
            trade.update(research_only=True,strategy=row["strategy"],
                         horizon_minutes=row["horizon_minutes"],
                         calibrated_tp_probability=round(float(pdg[j]),6),
                         expected_net_bps=round(float(expected[j]),4))
    return output,model,trades


def run(first,last,outdir,*,funding_mode="optional",config=Config(),train=True,
        download=None,funding_download=None,api_fetch=None):
    """Fetch only official public historical files and write diagnostics to output."""
    import timi_ml_v54_history_train as v54
    import timi_ml_v551_research as v551
    import timi_ml_v553_scalp as v553
    import timi_ml_v554_scalp as v554
    config.check()
    months=v54.month_range(first,last)
    raw=v54.historical_frame(months,download=(download or v54.read_month))
    audit=audit_historical_candles(raw)
    feat,cols=v54.features_targets(raw)
    feat=v551.enrich_cvd_regime(raw,feat)
    feat=v554.additional_features(raw,feat)
    feat,funding=v553.add_funding(feat,first,last,mode=funding_mode,
                                  archive_download=funding_download,api_fetch=api_fetch)
    feat=causal_features(raw,feat)
    output,model,trades=experiment(raw,feat,cols,config,
                                   funding_usable=(funding["status"]=="usable"),train=train)
    result={"engine":"TiMi ML v5.5.6 — feature+label diagnostics and 1/3/5/10/15m meta research",
            "schema":SCHEMA,"developer":"Suat DOĞU","source":"Binance Vision Spot BTCUSDT 1m",
            "months":months,"candles":len(raw),"feature_rows":len(feat),
            "data_quality":audit,
            "strategies":list(STRATEGIES),"horizon_minutes":list(HORIZONS),
            "barrier_variants":[b[0] for b in BARRIERS],"fee_bps_per_side":config.fee_bps,
            "slippage_bps_per_side":config.slippage_bps,"roundtrip_cost_bps":config.roundtrip_bps,
            "funding":funding,"split":"50/10/10/10/20 chronological with 17m purge",
            "label":"TP before SL AND after-cost net-positive, timeout is negative class",
            "label_geometry":"Next 1m open reference; same-bar SL-first; unfilled order and queue behavior unknown",
            "spot_sell_not_executable_short":True,"no_historical_orderbook_snapshots":True,
            "funding_asof_assumptions_must_be_verified":True,
            "reused_research_period_not_independent_oos":True,
            "not_investment_advice":True,"orders_enabled":False,"live_inference_enabled":False,
            **output}
    out=Path(outdir);out.mkdir(parents=True,exist_ok=True)
    (out/"data_quality.json").write_text(json.dumps(audit,
                               ensure_ascii=False,indent=2,allow_nan=False),encoding="utf-8")
    (out/"report.json").write_text(json.dumps({k:v for k,v in result.items() if k!="experiments"},
                               ensure_ascii=False,indent=2,allow_nan=False),encoding="utf-8")
    (out/"strategy_comparison.json").write_text(json.dumps(result["experiments"],
                               ensure_ascii=False,indent=2,allow_nan=False),encoding="utf-8")
    (out/"label_diagnostics.json").write_text(json.dumps([
        {"strategy":row["strategy"],"horizon_minutes":row["horizon_minutes"],
         "barrier":row["barrier"],"status":row["status"],
         "split_counts":row["split_counts"],"class_funnel":row["class_funnel"]}
        for row in result["experiments"]],ensure_ascii=False,indent=2,allow_nan=False),encoding="utf-8")
    if "calibration_research_only" in result:
        (out/"calibration_RESEARCH_ONLY.json").write_text(
            json.dumps(result["calibration_research_only"],ensure_ascii=False,indent=2),encoding="utf-8")
    if model is not None:
        model.booster_.save_model(str(out/"selected_model_RESEARCH_ONLY.txt"))
    if trades:
        with (out/"historical_reference_trades.csv").open("w",newline="",encoding="utf-8") as f:
            writer=csv.DictWriter(f,fieldnames=list(trades[0]))
            writer.writeheader();writer.writerows(trades)
    print("TiMi v5.5.6",result["status"],"experiments",len(result["experiments"]),
          "models",result["models_trained"],flush=True)
    return result


def main():
    parser=argparse.ArgumentParser(description="TiMi v5.5.6 research ONLY; no live orders")
    parser.add_argument("--from-month",default="2026-05")
    parser.add_argument("--to-month",default="2026-10")
    parser.add_argument("--funding-mode",choices=("off","optional","required"),default="optional")
    parser.add_argument("--fee-bps",type=float,default=10.)
    parser.add_argument("--slippage-bps",type=float,default=2.)
    parser.add_argument("--output",default="timi_v556_artifacts")
    parser.add_argument("--diagnostics-only",action="store_true",help="Skip LightGBM training")
    args=parser.parse_args()
    run(args.from_month,args.to_month,args.output,funding_mode=args.funding_mode,
        config=Config(fee_bps=args.fee_bps,slippage_bps=args.slippage_bps),
        train=not args.diagnostics_only)


if __name__=="__main__":
    main()
