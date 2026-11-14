"""Deterministic v5.5.8 unit tests. No network or real exchange orders."""
from __future__ import annotations
import csv
import json
import sys
import types
import numpy as np
import pandas as pd
import pytest
import timi_ml_v558_spot_edge as v558
import timi_ml_v557_barrier_lab as v557
import timi_ml_v556_research as v556


def fixture(n=1320):
    ts=(np.arange(n,dtype=np.int64)*60000)+1767225600000
    close=100+.05*np.sin(np.arange(n)*.13)
    opening=np.r_[100.,close[:-1]]
    raw=pd.DataFrame({"ts":ts,"open":opening,"close":close,
                      "high":np.maximum(close,opening)+.07,
                      "low":np.minimum(close,opening)-.07,
                      "segment":np.ones(n,int),"volume":np.full(n,10.),
                      "trades":np.full(n,10.),"quote_volume":np.full(n,1000.),
                      "taker_buy_volume":np.full(n,5.)})
    ids=np.arange(120,n-20)
    feat=pd.DataFrame({"ts":ts[ids],"atr14_pct":np.full(len(ids),.001),
                      "regime_trend":(ids%4==0).astype(float),
                      "regime_range":(ids%4==1).astype(float),
                      "regime_high_vol":(ids%4==2).astype(float)})
    return raw,feat


def test_settings_and_cost_arithmetic():
    assert v558.Settings().baseline_roundtrip_bps==24
    assert v558.Settings(6,1).baseline_roundtrip_bps==14
    with pytest.raises(ValueError): v558.Settings(-.1,1).check()
    with pytest.raises(ValueError): v558.Settings(1,float('nan')).check()
    with pytest.raises(ValueError): v558.Settings(1,1,min_policy_trades=1).check()


def test_regimes_prioritize_high_vol_and_are_not_future_based():
    _,f=fixture()
    f.loc[f.index[0],["regime_trend","regime_high_vol"]]=[1,1]
    data=v558.causal_context(f)
    assert data["regime"][0]=="HIGH_VOL"
    assert set(data["utc_block"])<=set(("UTC_00_08","UTC_08_16","UTC_16_24"))
    assert set(data["atr_bucket"])=={"ATR_5_TO_15bps"}
    assert len(data["regime"])==len(f)


def test_causal_time_uses_closed_candle_and_boundaries():
    _,f=fixture()
    f=f.iloc[:3].copy()
    # On the first minute of the UTC day: closed at 00:01 (first bucket).
    f["ts"]=np.array([1767225600000,1767254340000,1767283140000],dtype=np.int64)
    blocks=v558.causal_context(f)["utc_block"]
    assert list(blocks)==["UTC_00_08","UTC_08_16","UTC_16_24"]


def test_missing_causal_regime_fields_fail_closed():
    _,f=fixture()
    with pytest.raises(ValueError):v558.causal_context(f.drop(columns=["regime_trend"]))
    with pytest.raises(ValueError):v558.causal_context(f.assign(atr14_pct=-.01))


def test_long_only_excludes_sat_and_non_overlap():
    raw,f=fixture()
    ev=v557.simulate_ohlc(raw,f,5,1.5,.5,"cost_cover")
    d=np.resize(np.array([2,2,0,2,1],dtype=np.int8),len(f))
    ix=np.arange(350)
    selected=v558.select_long_nonoverlap(ev,d,ix,f.ts.to_numpy())
    assert all(d[selected]==2)
    assert len(selected)<np.sum(d[:350]==2)
    assert all(f.ts.iloc[j] >= f.ts.iloc[i]+(int(ev["long_step"][i])+1)*60000
               for i,j in zip(selected,selected[1:]))


def test_nonoverlap_indices_must_be_sorted():
    raw,f=fixture()
    ev=v557.simulate_ohlc(raw,f,1,1.5,.5,"atr_only")
    d=np.full(len(f),2,dtype=np.int8)
    with pytest.raises(ValueError):v558.select_long_nonoverlap(ev,d,np.array([2,1]),f.ts.to_numpy())
    with pytest.raises(ValueError):v558.select_long_nonoverlap(ev,d,np.array([5,5]),f.ts.to_numpy())


def test_empty_and_negative_reference_metrics_are_honest():
    assert v558.metrics(np.array([]),24)["profit_factor"] is None
    m=v558.metrics(np.array([15,16,23]),24)
    assert m["trades"]==3 and m["win_rate"]==0
    assert m["mean_net_bps"]<0
    assert m["sharpe_ratio"] is None
    assert m["cvar_05_net_bps"]<=m["p05_net_bps"]


def test_cost_sensitivity_reprices_same_fills_without_optimizing():
    low=v558.metrics(np.array([40.,-10.,28.]),12)
    base=v558.metrics(np.array([40.,-10.,28.]),24)
    high=v558.metrics(np.array([40.,-10.,28.]),40)
    assert low["trades"]==base["trades"]==high["trades"]==3
    assert low["mean_net_bps"]-base["mean_net_bps"]==12
    assert base["mean_net_bps"]-high["mean_net_bps"]==16
    assert low["profit_factor"]>high["profit_factor"]


def test_nonoverlap_reference_exits_are_exhaustive():
    raw,f=fixture()
    ev=v557.simulate_ohlc(raw,f,3,1.5,.5,"legacy_rr")
    d=np.resize(np.array([2,0,1,2,2],np.int8),len(f))
    idx=np.arange(850)
    m=v558.evaluate(ev,d,idx,f.ts.to_numpy(),v558.causal_context(f),v558.Settings())
    assert m["excluded_SAT_directional_signals"]>0
    assert m["selected_reference_LONG_trades"]==sum(m[x] for x in
                                    ("long_tp_first","long_sl_first","long_timeout"))
    assert m["baseline"]["trades"]==m["selected_reference_LONG_trades"]
    assert not m["not_independent_executable_evidence"] is False
    assert set(m["causal_subgroups"])=={"regime","utc_block","atr_bucket"}
    for category in m["causal_subgroups"].values():
        assert sum(x["trades"] for x in category.values())==m["baseline"]["trades"]


def test_rr_gate_not_silently_bypassed():
    raw,f=fixture()
    ev=v557.simulate_ohlc(raw,f,1,1.5,.5,"atr_only")
    d=np.full(len(f),2,np.int8)
    m=v558.evaluate(ev,d,np.arange(600),f.ts.to_numpy(),v558.causal_context(f),v558.Settings())
    assert m["net_reward_risk_pass_count"] < m["selected_reference_LONG_trades"]
    assert m["internal_policy_gate_passed"] is False


def test_later_candle_does_not_change_causal_regimes():
    _,f=fixture()
    a=v558.causal_context(f)
    updated=f.copy()
    updated.loc[updated.index[-1],"regime_high_vol"]=1
    updated.loc[updated.index[-1],"atr14_pct"]=.02
    b=v558.causal_context(updated)
    for key in a:
        np.testing.assert_array_equal(a[key][:-1],b[key][:-1])


def test_audit_variant_counts_and_never_deploys(monkeypatch):
    raw,f=fixture()
    d={"toy":np.resize(np.array([2,0,1,2],np.int8),len(f))}
    exp=v558.audit(raw,f,horizons=(1,3),barriers=(v557.BARRIERS[0],),
                    policies=("legacy_rr","cost_cover"),candidate_map=d)
    assert len(exp)==4
    assert all(x["live_promotion_allowed"] is False for x in exp)
    assert all(x["model_trained"] is False for x in exp)
    assert all("historical_diagnostic" in x for x in exp)


def test_run_writes_audit_files_without_orders(monkeypatch,tmp_path):
    raw,f=fixture()
    fake54=types.ModuleType("timi_ml_v54_history_train")
    fake54.month_range=lambda first,last:[first,last]
    fake54.read_month=lambda month:None
    fake54.historical_frame=lambda months,download=None:raw
    fake54.features_targets=lambda frame:(f,[])
    fake551=types.ModuleType("timi_ml_v551_research")
    fake551.enrich_cvd_regime=lambda raw,feat:feat
    fake554=types.ModuleType("timi_ml_v554_scalp")
    fake554.additional_features=lambda raw,feat:feat
    for name,obj in (("timi_ml_v54_history_train",fake54),
                     ("timi_ml_v551_research",fake551),
                     ("timi_ml_v554_scalp",fake554)):
        monkeypatch.setitem(sys.modules,name,obj)
    monkeypatch.setattr(v556,"causal_features",lambda raw,feat:feat)
    monkeypatch.setattr(v556,"candidate_directions",lambda raw,feat:{
         key:np.resize(np.array([2,1,0,2],np.int8),len(feat)) for key in v556.STRATEGIES})
    result=v558.run("2026-05","2026-10",str(tmp_path))
    assert result["experiments"]==150
    assert result["model_ready_for_live"] is False
    assert result["orders_enabled"] is False
    assert result["reused_research_period_not_independent_oos"] is True
    for p in ("report.json","data_quality.json","spot_edge_experiments.json","cost_audit.json","spot_long_comparison.csv"):
        assert (tmp_path/p).is_file()
    exp=json.loads((tmp_path/"spot_edge_experiments.json").read_text())
    assert len(exp)==150
    with (tmp_path/"spot_long_comparison.csv").open(newline="") as fp:
        assert len(list(csv.DictReader(fp)))==300
    assert json.loads((tmp_path/"data_quality.json").read_text())["synthetic_candles_added"]==0
