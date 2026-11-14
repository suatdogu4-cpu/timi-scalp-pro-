"""Offline deterministic checks for TiMi v5.5.6. No HTTP, orders or secrets."""
import numpy as np
import pandas as pd
import pytest

from timi_ml_v556_research import (Config, HORIZONS, causal_features,
    label_events, label_funnel, chronological_folds, align_closes)


def candles(n=350, *,gap_at=None):
    ts=np.arange(n,dtype=np.int64)*60000+1_700_000_040_000
    if gap_at is not None:
        ts[gap_at:]+=60000
    segment=np.zeros(n,dtype=int)
    if gap_at is not None:
        segment[gap_at:]=1
    v=np.full(n,100.,float)
    return pd.DataFrame({"ts":ts,"segment":segment,"open":v.copy(),
        "high":v.copy(),"low":v.copy(),"close":v.copy(),
        "volume":np.linspace(10,20,n),"taker_buy_volume":np.linspace(5,10,n)})


def features(raw):
    return pd.DataFrame({"ts":raw.ts,"atr14_pct":np.full(len(raw),.001)})


def test_default_cost_target_is_not_fake_small():
    cfg=Config()
    ev=label_events(candles(),features(candles()),1,cfg)
    assert cfg.roundtrip_bps==24
    assert ev["tp_frac"][200]*10000>=64-1e-8
    assert ev["sl_frac"][200]*10000>=8-1e-8


def test_same_bar_stop_precedes_take_profit():
    raw=candles();feat=features(raw)
    raw.loc[201,"high"]=102.
    raw.loc[201,"low"]=98.
    ev=label_events(raw,feat,1,Config())
    assert ev["valid"][200]
    assert ev["long_side"][200]==-1
    assert ev["short_side"][200]==-1
    assert ev["long_net"][200]<0 and ev["short_net"][200]<0


def test_tp_first_and_no_timeout_disguise():
    raw=candles();raw.loc[201,"high"]=102.
    ev=label_events(raw,features(raw),1,Config())
    assert ev["long_side"][200]==1
    assert ev["short_side"][200]==-1  # high crosses the short stop
    directions=np.ones(len(raw),np.int8);directions[200]=2
    f=label_funnel(ev,directions,np.array([200]),Config())
    assert f["TP_first"]==1 and f["SL_first"]==0 and f["timeout"]==0
    assert f["positive_net_any_exit"]==1
    assert f["net_win_rate"]==1.


def test_timeout_counted_separately_and_as_non_tp():
    raw=candles();ev=label_events(raw,features(raw),1,Config())
    directions=np.ones(len(raw),np.int8);directions[200]=2
    f=label_funnel(ev,directions,np.array([200]),Config())
    assert f["timeout"]==1
    assert f["TP_first"]==0
    assert f["negative_net_any_exit"]==1


def test_15m_and_gap_cannot_cross():
    raw=candles(350,gap_at=212)
    ev=label_events(raw,features(raw),15,Config())
    assert not ev["valid"][205]
    assert ev["valid"][230]
    assert len(ev["valid"])==350


def test_folds_purged_15min_no_overlap():
    ts=np.arange(2000,dtype=np.int64)*60000+1_700_000_040_000
    out=chronological_folds(ts,np.ones(len(ts),bool),15)
    assert len(out)==5 and all(len(o)>0 for o in out)
    for a,b in zip(out,out[1:]):
        assert ts[a[-1]]+16*60000<ts[b[0]]
    assert not set(out[0]).intersection(out[1])


def test_alignment_for_extended_horizons():
    ts=np.arange(60,dtype=np.int64)*60000
    assert int(np.sum(align_closes(ts,10)))==6
    assert int(np.sum(align_closes(ts,15)))==4
    with pytest.raises(ValueError):
        align_closes(ts,20)


def test_features_do_not_see_future():
    raw=candles(300)
    raw["close"] = 100 + .1*np.sin(np.arange(300)/5.)
    original=features(raw)
    a=causal_features(raw.iloc[:260].copy(),original.iloc[:260].copy())
    b=causal_features(raw,original)
    common=a.merge(b,on="ts",suffixes=("_a","_b"),validate="one_to_one")
    assert len(common)>50
    for col in ("momentum3","volume_ratio20","macd_hist_pct","bandwidth_ratio120"):
        np.testing.assert_allclose(common[f"{col}_a"],common[f"{col}_b"],rtol=1e-11)


def test_config_rejects_unrealistic_negative_costs():
    with pytest.raises(ValueError):
        Config(fee_bps=-1).check()
    with pytest.raises(ValueError):
        Config(minimum_reward_risk=.8).check()


def test_causal_feature_bad_input_rejected():
    raw=candles(250); raw.loc[1,"ts"]=raw.loc[0,"ts"]
    with pytest.raises(ValueError):
        causal_features(raw,features(raw))


def test_complete_50_experiment_diagnostic_grid(monkeypatch):
    """Dependency-injected smoke test; not a market performance benchmark."""
    import sys, types
    from timi_ml_v556_research import experiment
    raw=candles(6200)
    x=np.arange(len(raw))
    raw["close"]=100+0.002*x+.25*np.sin(x/7.)
    raw["open"]=raw.close
    raw["high"]=raw.close+0.06
    raw["low"]=raw.close-0.06
    feat=pd.DataFrame({"ts":raw.ts,"atr14_pct":np.full(len(raw),.001),
        "regime_trend":np.ones(len(raw)),"regime_high_vol":np.zeros(len(raw)),
        "ema8_21_pct":np.full(len(raw),.0001),"rsi14":np.full(len(raw),55.)})
    feat=causal_features(raw,feat)
    old=types.ModuleType("timi_ml_v555_meta")
    old.v551=types.SimpleNamespace(CVD_COLS=(),REGIME_COLS=(),FUNDING_COLS=())
    old.v554=types.SimpleNamespace(EXTRA_COLS=())
    old.strategy_candidates=lambda r,f: {
        name:np.where(np.arange(len(f))%50==0,2,1).astype(np.int8)
        for name in ("trend_pullback","vwap_reversion","volume_breakout")}
    old.trailing_extremes=lambda r,f:(np.full(len(f),100000.),np.full(len(f),-100000.))
    monkeypatch.setitem(sys.modules,"timi_ml_v555_meta",old)
    monkeypatch.setitem(sys.modules,"timi_ml_v55_research",types.ModuleType("timi_ml_v55_research"))
    out,model,trades=experiment(raw,feat,["atr14_pct"],Config(),train=False)
    assert len(out["experiments"])==50
    assert model is None and not trades and out["models_trained"]==0
    assert sorted({r["horizon_minutes"] for r in out["experiments"]})==[1,3,5,10,15]
    assert all("class_funnel" in r for r in out["experiments"])


def test_audit_reports_gaps_without_imputation():
    from timi_ml_v556_research import audit_historical_candles
    raw=candles(100,gap_at=51)
    info=audit_historical_candles(raw)
    assert info["rows_observed"]==100
    assert info["missing_minutes"]==1 and info["gap_events"]==1
    assert info["synthetic_candles_added"]==0
    assert info["timezone"]=="UTC"
    assert info["max_gap_minutes"]==1


def test_audit_allows_genuine_zero_volume_bars_and_rejects_bogus_ohlc():
    from timi_ml_v556_research import audit_historical_candles
    raw=candles(100)
    raw.loc[60,"volume"]=0
    raw.loc[60,"taker_buy_volume"]=0
    assert audit_historical_candles(raw)["zero_volume_genuine_rows"]==1
    raw.loc[70,"high"]=99
    with pytest.raises(ValueError,match="OHLCV"):
        audit_historical_candles(raw)


def test_audit_detects_wrong_segment_after_gap():
    from timi_ml_v556_research import audit_historical_candles
    raw=candles(100,gap_at=70)
    raw["segment"]=0
    with pytest.raises(ValueError,match="Segments"):
        audit_historical_candles(raw)


def test_utc_cyc_features_are_bounded_and_causal():
    raw=candles(300)
    raw["close"]=100+np.sin(np.arange(len(raw))/4)*0.3
    raw["open"]=raw.close
    raw["high"]=raw.close+0.1
    raw["low"]=raw.close-0.1
    raw["volume"]=np.linspace(10,100,len(raw))+np.sin(np.arange(len(raw))/3)
    raw["taker_buy_volume"]=raw.volume*0.51
    frame=causal_features(raw,features(raw))
    colset=["hour_sin_utc","hour_cos_utc","weekday_sin_utc","weekday_cos_utc",
            "log_return1","log_return_lag1","log_return_lag2","log_return_lag3",
            "body_over_range","volume_z20_prior"]
    assert all(col in frame.columns for col in colset)
    for col in colset[:4]:
        assert frame[col].between(-1.000001,1.000001).all()
    early=causal_features(raw.iloc[:260].copy(),features(raw).iloc[:260].copy())
    joined=early.merge(frame,on="ts",suffixes=("_a","_b"),validate="one_to_one")
    for col in colset:
        np.testing.assert_allclose(joined[col+"_a"],joined[col+"_b"],rtol=1e-11,atol=1e-12)


def test_body_wick_and_range_are_price_invariant():
    raw=candles(300)
    x=np.arange(len(raw))
    raw["close"]=100+0.001*x+np.sin(x/8)*0.3
    raw["open"]=raw.close-0.02
    raw["high"]=raw.close+0.07
    raw["low"]=raw.open-0.03
    a=causal_features(raw,features(raw))
    scaled=raw.copy()
    for col in ("open","high","low","close"):
        scaled[col] *= 10
    b=causal_features(scaled,features(scaled))
    for col in ("range_over_close","body_over_range","upper_wick_over_close","lower_wick_over_close"):
        np.testing.assert_allclose(a[col],b[col],rtol=1e-9,atol=1e-12)
