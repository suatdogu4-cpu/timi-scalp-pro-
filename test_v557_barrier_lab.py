"""Offline unit + regression checks for the v5.5.7 research-only lab."""
from __future__ import annotations
import numpy as np
import pandas as pd
import pytest

import timi_ml_v556_research as v556
import timi_ml_v557_barrier_lab as lab


def fixture(n=1200):
    ts=np.arange(n,dtype=np.int64)*60000+1767225600000
    close=100.0 + .012*np.sin(np.arange(n)*.17)
    opens=np.r_[100.0,close[:-1]]
    raw=pd.DataFrame({"ts":ts,"open":opens,"close":close,"high":np.maximum(opens,close)+.08,
                      "low":np.minimum(opens,close)-.08,"segment":np.ones(n,int),"volume":10.0,
                      "trades":10.0,"taker_buy_volume":5.0,"quote_volume":1000.0})
    feat=pd.DataFrame({"ts":ts[100:-16],"atr14_pct":np.full(n-116,.001),"close":close[100:-16]})
    return raw,feat


def test_legacy_minimum_matches_previous_geometry():
    a=np.array([.0002,.001,.004])
    g=lab.barrier_geometry(a,1.5,.5,"legacy_rr")
    old_sl=np.maximum(.5*a,.0008)
    expected=np.maximum.reduce((1.5*a,np.full_like(a,.0026),.0024+1.25*(old_sl+.0024)))
    np.testing.assert_allclose(g["tp_frac"],expected)
    assert np.all(g["net_rr"]>=1.25)


def test_cost_cover_may_not_satisfy_rr_and_is_not_auto_approved():
    g=lab.barrier_geometry(np.array([.0002]),1.5,.5,"cost_cover")
    assert round(float(g["tp_frac"][0])*10000,4)==26
    assert g["net_rr"][0]<1.25


def test_atr_only_may_have_negative_net_tp_and_is_diagnostic():
    g=lab.barrier_geometry(np.array([.0002]),1.5,.5,"atr_only")
    assert g["tp_frac"][0]*10000<lab.Settings().roundtrip_bps


def test_reject_invalid_cost_and_geometry():
    with pytest.raises(ValueError): lab.Settings(-1,2).check()
    with pytest.raises(ValueError): lab.barrier_geometry(np.array([.001]),1.5,.5,"made_up")


def test_strict_geometry_equivalent_to_v556_label_events():
    raw,feat=fixture()
    old=v556.label_events(raw,feat,3,v556.Config(),1.5,.5)
    new=lab.simulate_ohlc(raw,feat,3,1.5,.5,"legacy_rr")
    np.testing.assert_array_equal(old["valid"],new["valid"])
    np.testing.assert_allclose(old["tp_frac"]*10000,new["tp_bps"])
    np.testing.assert_allclose(old["sl_frac"]*10000,new["sl_bps"])
    np.testing.assert_array_equal(old["long_side"],new["long_flag"])
    np.testing.assert_array_equal(old["short_side"],new["short_flag"])
    np.testing.assert_allclose(old["long_net"][old["valid"]]*10000,
                               (new["long_gross_bps"]-24)[old["valid"]],atol=1e-7)


def test_next_open_reference_not_signal_close():
    raw,feat=fixture(1200)
    pos=100
    raw.loc[pos+1,"open"]=101.0
    raw.loc[pos+1,"high"]=101.1
    raw.loc[pos+1,"low"]=100.9
    ev=lab.simulate_ohlc(raw,feat,1,1.5,.5,"atr_only")
    # Signal index 100 enters at 101, not the signal candle's close (~100).
    assert ev["long_gross_bps"][0] < 0


def test_same_bar_tp_and_sl_uses_stop_first():
    raw,feat=fixture()
    raw.loc[101,"high"]=110.0
    raw.loc[101,"low"]=90.0
    e=lab.simulate_ohlc(raw,feat,1,1.5,.5,"atr_only")
    assert e["long_flag"][0]==-1
    assert e["short_flag"][0]==-1
    assert e["long_mfe_bps"][0]>e["tp_bps"][0]


def test_malformed_out_of_order_timestamps_rejected():
    raw,feat=fixture()
    raw.loc[150,"ts"]=raw.loc[149,"ts"]
    with pytest.raises(ValueError):lab.simulate_ohlc(raw,feat,5,1.5,.5,"legacy_rr")


def test_missing_minute_not_assumed_to_exist():
    raw,feat=fixture()
    raw=raw.drop(index=[104]).reset_index(drop=True)
    raw["segment"]=(raw.ts.diff().ne(60000)).cumsum()
    feat=feat[feat.ts!=fixture()[0].ts.iloc[104]].reset_index(drop=True)
    e=lab.simulate_ohlc(raw,feat,5,1.5,.5,"legacy_rr")
    # signal at 100 crosses the gap 104 within 5-minute outcome
    assert not e["valid"][0]


def test_summary_counts_exhaustive_and_cost_repricing_fixed():
    raw,feat=fixture()
    e=lab.simulate_ohlc(raw,feat,3,1.5,.5,"cost_cover")
    dirs=np.full(len(feat),2,dtype=np.int8)
    ids=np.arange(110)
    s=lab.summarize(e,dirs,ids)
    assert s["candidate_events"]==110
    assert s["TP_first"]+s["SL_first"]+s["timeout"]==110
    assert s["reprice_same_fills"]["24"]["mean_net_bps"]==s["mean_net_bps"]
    assert s["reprice_same_fills"]["12"]["mean_net_bps"]>=s["mean_net_bps"]


def test_spot_only_excludes_short_signals_and_serializes_entries():
    raw,feat=fixture()
    e=lab.simulate_ohlc(raw,feat,5,1.5,.5,"cost_cover")
    d=np.resize(np.array([2,0,2,2,0],np.int8),len(feat))
    v=lab.long_spot_nonoverlap(e,d,np.arange(500),feat.ts.to_numpy())
    assert v["trades"]<=sum(d[:500]==2)
    assert v["sharpe_ratio"] is None


def test_exploration_outputs_all_variants_without_training():
    raw,feat=fixture()
    dirs={"toy":np.full(len(feat),2,dtype=np.int8)}
    exp=lab.research(raw,feat,policies=("legacy_rr","atr_only"),horizons=(1,3),
                     barriers=(lab.BARRIERS[0],),strategies=dirs)
    assert len(exp)==4
    assert {row["geometry_policy"] for row in exp}=={"legacy_rr","atr_only"}
    assert all("diagnostic" in row["folds"] for row in exp)
    assert all(row["folds"]["diagnostic"]["long_spot_nonoverlap"]["sharpe_ratio"] is None for row in exp)


def test_run_creates_machine_readable_outputs_without_any_orders(monkeypatch,tmp_path):
    import sys, types, json, csv
    raw,feat=fixture(1320)
    feat=feat.copy()
    fake54=types.ModuleType("timi_ml_v54_history_train")
    fake54.month_range=lambda a,b:[a,b]
    fake54.read_month=lambda *_:None
    fake54.historical_frame=lambda months,download=None:raw
    fake54.features_targets=lambda frame:(feat,["atr14_pct"])
    fake551=types.ModuleType("timi_ml_v551_research")
    fake551.enrich_cvd_regime=lambda raw,feat:feat
    fake553=types.ModuleType("timi_ml_v553_scalp")
    fake553.add_funding=lambda feat,*args,**kwargs:(feat,{"status":"off","event_count":0})
    fake554=types.ModuleType("timi_ml_v554_scalp")
    fake554.additional_features=lambda raw,feat:feat
    for name,m in (("timi_ml_v54_history_train",fake54),("timi_ml_v551_research",fake551),
                   ("timi_ml_v553_scalp",fake553),("timi_ml_v554_scalp",fake554)):
        monkeypatch.setitem(sys.modules,name,m)
    monkeypatch.setattr(v556,"causal_features",lambda raw,feat:feat)
    d={name:np.where(np.arange(len(feat))%19==0,2,1).astype(np.int8)
       for name in v556.STRATEGIES}
    monkeypatch.setattr(v556,"candidate_directions",lambda raw,feat:d)
    result=lab.run("2026-05","2026-10",tmp_path)
    assert result["experiments"]==150
    assert result["orders_enabled"] is False
    assert result["model_ready_for_live"] is False
    experiments=json.loads((tmp_path/"barrier_feasibility.json").read_text())
    with (tmp_path/"barrier_policy_comparison.csv").open(newline="") as f:
        assert len(list(csv.DictReader(f)))==750
    assert len(experiments)==150
    assert json.loads((tmp_path/"data_quality.json").read_text())["synthetic_candles_added"]==0
