"""TiMi v5.5.4 offline correctness tests. Synthetic outcomes are not evidence of trading returns."""
from __future__ import annotations
import math
import numpy as np
import pytest

import timi_ml_v54_history_train as v54
import timi_ml_v55_research as v55
import timi_ml_v551_research as v551
import timi_ml_v553_scalp as v553
import timi_ml_v554_scalp as v554
from test_v552_research import dummy_source


@pytest.fixture(scope="module")
def sources():
    raw = v54.historical_frame(["2026-05"], download=lambda _:dummy_source(16000))
    feat, cols = v54.features_targets(raw)
    feat = v551.enrich_cvd_regime(raw, feat)
    ext = v554.additional_features(raw, feat)
    return raw, feat, ext, cols


def test_horizons_closed_candles():
    # Source timestamps are OPEN, close becomes known one minute later.
    t = np.arange(15, dtype=np.int64)*60_000
    assert v554.close_aligned(t, 1).all()
    assert np.flatnonzero(v554.close_aligned(t,3)).tolist() == [2,5,8,11,14]
    assert np.flatnonzero(v554.close_aligned(t,5)).tolist() == [4,9,14]
    for h in (0, 15, 30):
        with pytest.raises(ValueError): v554.close_aligned(t,h)


def test_added_features_causal_under_future_movement(sources):
    raw, base, ext, cols = sources
    a = raw.copy()
    pivot = 4500
    a.loc[a.index > pivot, "close"] *= 3
    a.loc[a.index > pivot, "high"] *= 3
    a.loc[a.index > pivot, "low"] *= 3
    a.loc[a.index > pivot, "open"] *= 3
    # Trading features at source times <= pivot cannot use future candles.
    alt = v554.additional_features(a,base)
    x = ext.set_index("ts")[list(v554.EXTRA_COLS)]
    y = alt.set_index("ts")[list(v554.EXTRA_COLS)]
    limit = int(raw.ts.iloc[pivot])
    overlap = x.loc[x.index <= limit].index.intersection(y.loc[y.index <= limit].index)
    assert len(overlap) > 3500
    np.testing.assert_allclose(x.loc[overlap].to_numpy(),y.loc[overlap].to_numpy(),atol=1e-10,rtol=1e-10)


def test_feature_family_has_no_orderbook_proxy_mislabel(sources):
    raw,_,frame,cols = sources
    assert all(c in frame for c in v554.EXTRA_COLS)
    assert not any(c.startswith("orderbook_") for c in frame.columns)
    assert "illiquidity_proxy20" in frame
    assert len(frame) > 12000
    assert np.isfinite(frame[list(v554.EXTRA_COLS)].to_numpy()).all()


def test_vwap_reconstructed_from_current_and_past(sources):
    raw,_,frame,_ = sources
    i=1500
    pos=int(np.searchsorted(raw.ts.to_numpy(),frame.ts.iloc[i]))
    subset=raw.iloc[pos-4:pos+1]
    typical=(subset.high+subset.low+subset.close)/3
    expected=float(raw.close.iloc[pos]/((typical*subset.volume).sum()/subset.volume.sum())-1)
    assert frame.rolling_vwap5_pct.iloc[i]==pytest.approx(expected)


def test_confidence_funnel_payoff_after_fees():
    ev={"valid":np.array([True]*5),"tp_frac":np.array([.005]*5),"sl_frac":np.array([.002]*5),"cost_bps":24.}
    p1=np.array([.8,.55,.9,np.nan,.9])
    p2=np.array([.2,.3,.1,.1,.1])
    choice,expected,reasons=v554.sample_funnel(p1,p2,ev,np.arange(5),min_probability=.7,min_expected_bps=3.)
    assert choice[0]==2 and reasons[0]=="SIGNAL"
    assert reasons[1]=="BELOW_TP_PROBABILITY"
    assert reasons[3]=="INVALID_FEATURE_OR_BARRIER"
    assert float(expected[0])==pytest.approx(.8*50-.2*20-24)
    assert v554.funnel_summary(reasons)["SIGNAL"] >=2


def test_low_expected_return_blocks_even_high_probability():
    ev={"valid":np.array([True]),"tp_frac":np.array([.003]),"sl_frac":np.array([.002]),"cost_bps":24.}
    act,ex,reason=v554.sample_funnel(np.array([.9]),np.array([.1]),ev,np.array([0]),min_probability=.8,min_expected_bps=4.)
    assert act[0]==1
    assert reason[0]=="COST_OR_EDGE_FAILURE"


def test_gating_never_approves_empty_or_loss():
    c=v554.Config()
    empty={"trades":0,"average_net_bps":None,"profit_factor":None,"maximum_drawdown_pct":0}
    bad={"trades":150,"average_net_bps":-1.,"profit_factor":0.8,"maximum_drawdown_pct":25}
    assert not v554.validation_gate(empty,c)[0]
    assert not v554.validation_gate(bad,c)[0]


def test_label_entry_is_after_signal(sources):
    raw, _, feat, _=sources
    for h in v554.HORIZONS:
        ev=v553.prepare_barriers(raw,feat,h,v553.Settings(),1.5,.5)
        idx=np.flatnonzero(ev["valid"] & v554.close_aligned(feat.ts,h))
        assert len(idx)>1000
        i=int(idx[50]);j=int(np.searchsorted(raw.ts.to_numpy(),feat.ts.iloc[i]))
        assert ev["entry"][i]==pytest.approx(raw.open.iloc[j+1])
        assert 1 <= int(ev["long_k"][i]) <= h
        assert 1 <= int(ev["short_k"][i]) <= h


def test_training_probability_is_finite():
    rng=np.random.default_rng(42)
    x=rng.normal(size=(1500,3))
    yt=(x[:,0]+.2*x[:,1]>1).astype(int)
    from pandas import DataFrame
    model=v554.train_tp_model(DataFrame(x[:1050]),yt[:1050],DataFrame(x[1050:1300]),yt[1050:1300],seed=42)
    p=v554.safe_prob(model,DataFrame(x[1300:]))
    assert model is not None and np.isfinite(p).all() and (p>=0).all() and (p<=1).all()


def test_no_deploy_run_and_reports(monkeypatch,tmp_path,sources):
    raw,_,feat,cols=sources
    # A compact integration smoke: substitute optimizer train to keep tests fast.
    class FakeModel:
        def predict_proba(self,X):
            p=np.full(len(X),.15)
            return np.column_stack([1-p,p])
    monkeypatch.setattr(v554,"train_tp_model",lambda *a,**kw:FakeModel())
    result, models, trades=v554.research(raw,feat,cols,barriers=(v554.BARRIERS[0],))
    assert result["orders_enabled"] is False
    assert result["live_inference_enabled"] is False
    assert result["model_ready_for_live"] is False
    assert result["candidate_count"]==6
    assert set(result["validation_per_horizon"])=={"1","3","5"}
    assert all(v["validation_passed"] == 0 for v in result["validation_per_horizon"].values())
    assert not trades
