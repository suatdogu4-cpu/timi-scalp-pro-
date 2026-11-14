"""TiMi v5.5.5 offline checks. Synthetic tests are not profitability evidence."""
from __future__ import annotations
import numpy as np
import pandas as pd
import pytest
from pathlib import Path

import timi_ml_v54_history_train as v54
import timi_ml_v55_research as v55
import timi_ml_v551_research as v551
import timi_ml_v553_scalp as v553
import timi_ml_v554_scalp as v554
import timi_ml_v555_meta as v555
from test_v552_research import dummy_source


@pytest.fixture(scope="module")
def source():
    raw = v54.historical_frame(["2026-05"],download=lambda _:dummy_source(16000))
    feat,cols=v54.features_targets(raw)
    feat=v551.enrich_cvd_regime(raw,feat)
    feat=v554.additional_features(raw,feat)
    return raw,feat,cols


def test_sides_and_labels_are_valid(source):
    raw,feat,cols=source
    signals=v555.strategy_candidates(raw,feat)
    assert set(signals)==set(v555.STRATEGIES)
    assert all(len(x)==len(feat) and set(np.unique(x)).issubset({0,1,2}) for x in signals.values())
    ev=v553.prepare_barriers(raw,feat,3,v553.Settings(),1.5,.5)
    for sides in signals.values():
        labels=v555.tp_first_binary(ev,sides)
        assert len(labels)==len(feat) and np.isin(labels,[0,1]).all()
        assert np.all(labels[sides==1]==0)


def test_breakout_uses_prior_candles_not_current(source):
    raw,feat,cols=source
    hi,lo=v555.trailing_extremes(raw,feat)
    pos=int(np.searchsorted(raw.ts.to_numpy(),feat.ts.iloc[1500]))
    assert hi[1500] == pytest.approx(float(raw.high.iloc[pos-20:pos].max()))
    assert lo[1500] == pytest.approx(float(raw.low.iloc[pos-20:pos].min()))


def test_causality_for_strategy_filters_after_future_price_change(source):
    raw,feat,cols=source
    alt=raw.copy()
    pivot=4500
    alt.loc[alt.index>pivot,"high"]*=3
    alt.loc[alt.index>pivot,"low"]*=3
    alt.loc[alt.index>pivot,"close"]*=3
    # Strategy references from raw only at current/past timestamps.
    original=v555.strategy_candidates(raw,feat)
    mutated=v555.strategy_candidates(alt,feat)
    safe=(feat.ts.to_numpy() <= int(raw.ts.iloc[pivot]))
    for name in v555.STRATEGIES:
        np.testing.assert_array_equal(original[name][safe],mutated[name][safe])


def test_closed_mtf_alignment(source):
    raw,feat,_=source
    signals=v555.strategy_candidates(raw,feat)
    t=feat.ts.to_numpy()
    for h in v555.HORIZONS:
        mask=v554.close_aligned(t,h)
        assert not np.any(((t//v54.MS+1)%h != 0) & mask)


def test_fivefold_purge_never_crosses_label_window(source):
    _,feat,_=source
    times=feat.ts.to_numpy()
    for h in v555.HORIZONS:
        parts=v555.fivefold_split(times,np.ones(len(times),bool),h)
        assert len(parts)==5
        for a,b in zip(parts,parts[1:]):
            assert times[a[-1]]+(h+1)*v54.MS < times[b[0]]


def test_synthetic_probability_calibration():
    rng=np.random.default_rng(7)
    p=rng.uniform(.05,.95,600)
    y=rng.binomial(1,np.clip(p**1.8,0,1))
    c=v555.fit_calibrator(p[:400],y[:400]);assert c is not None
    pred=np.asarray(c.predict(p[400:]),float)
    assert np.isfinite(pred).all() and (pred>=0).all() and (pred<=1).all()
    info=v555.calibration_scores(y[400:],pred)
    assert info and 0<=info["brier"]<=1 and len(info["bins"])>0


def test_no_calibration_without_diversity():
    p=np.linspace(.1,.9,100)
    assert v555.fit_calibrator(p,np.zeros(100,dtype=int)) is None
    assert v555.fit_calibrator(p[:10],np.array([0,1]*5)) is None


def test_fee_adjusted_signal_filter():
    n=4
    events={"valid":np.ones(n,bool),"tp_frac":np.full(n,.005),"sl_frac":np.full(n,.002)}
    dirs=np.array([2,0,2,1],np.int8)
    p=np.array([.90,.65,.90,.90])
    act,exp,reason=v555.decisions_from_probability(p,np.arange(n),events,dirs,.70,2.,v55.Costs())
    assert act.tolist()==[2,1,2,1]
    assert reason[1]=="LOW_CALIBRATED_PROBABILITY"
    assert reason[3]=="INVALID_OR_NO_CANDIDATE"
    assert exp[0]==pytest.approx(.9*50-.1*20-24)
    _,_,r=v555.decisions_from_probability(np.array([.9]),np.array([0]),
             {"valid":np.array([True]),"tp_frac":np.array([.003]),"sl_frac":np.array([.002])},
             np.array([2]),.8,4.,v55.Costs())
    assert r[0]=="NO_NET_EDGE_AFTER_COST"


def test_no_live_approval_and_empty_gate():
    bad={"trades":0,"average_net_bps":None,"profit_factor":None,"maximum_drawdown_pct":0.}
    assert v555.qualify(bad,v555.Config())[0] is False
    with pytest.raises(ValueError):
        v555.Config(minimum_fit_events=2).check()


def test_meta_feature_side_and_funding(source):
    _,feat,cols=source
    sides=np.ones(len(feat),dtype=np.int8);sides[:10]=2;sides[10:20]=0
    X,names=v555.make_event_features(feat,cols,sides,False)
    assert X.candidate_side.iloc[0]==1 and X.candidate_side.iloc[10]==-1
    assert not set(v551.FUNDING_COLS).intersection(names)
    assert np.isfinite(X.to_numpy()).all()


def test_comparison_no_live_orders_when_no_valid_events(source,monkeypatch):
    raw,feat,cols=source
    monkeypatch.setattr(v555,"strategy_candidates",lambda r,f: {s:np.ones(len(f),np.int8) for s in v555.STRATEGIES})
    result,model,rows=v555.comparison(raw,feat,cols,barriers=(v555.BARRIERS[0],),horizon_choices=(1,))
    assert model is None and not rows
    assert result["status"]=="NO_TRAINABLE_CANDIDATE"
    assert result["orders_enabled"] is False
    assert result["test_scored"] is False
