"""Offline tests: synthetic data are never evidence of a profitable strategy."""
from __future__ import annotations

import csv
import hashlib
import io
import json
import math
import sys
import urllib.error
import zipfile
from datetime import datetime, timezone
from pathlib import Path

import numpy as np
import pandas as pd
import pytest

sys.path.insert(0, str(Path(__file__).resolve().parent))
import timi_ml_v54_history_train as v54
import timi_ml_v55_research as v55
import timi_ml_v551_research as v551
import timi_ml_v552_research as v552


def dummy_source(n=12200):
    rng = np.random.default_rng(5512)
    start = int(datetime(2026, 5, 1, tzinfo=timezone.utc).timestamp()*1000)
    ts = start + np.arange(n, dtype=np.int64)*60_000
    changes = rng.normal(0, 0.0012, n)
    closes = 80_000*np.exp(np.cumsum(changes))
    opens = np.r_[80_000., closes[:-1]]
    highs = np.maximum(opens,closes)*(1+rng.uniform(0.0001,0.0008,n))
    lows = np.minimum(opens,closes)*(1-rng.uniform(0.0001,0.0008,n))
    volumes = rng.uniform(1.,5.,n)
    buys = volumes*rng.uniform(0.2,0.8,n)
    return list(zip(ts.tolist(),opens.tolist(),highs.tolist(),lows.tolist(),closes.tolist(),
                    volumes.tolist(),[100.]*n,buys.tolist(),(volumes*closes).tolist()))


@pytest.fixture(scope="module")
def sources():
    raw=v54.historical_frame(["2026-05"],download=lambda _:dummy_source())
    feat, columns=v54.features_targets(raw)
    feat=v551.enrich_cvd_regime(raw,feat)
    return raw, feat, columns


def fake_archive(month="2026-05", rate=0.0001):
    t0=int(datetime.strptime(month,"%Y-%m").replace(tzinfo=timezone.utc).timestamp()*1000)
    csv_lines=["calc_time,funding_interval_hours,last_funding_rate"]
    csv_lines += [f"{t0+i*8*3600_000},8,{rate}" for i in range(30)]
    with io.BytesIO() as stream:
        with zipfile.ZipFile(stream,"w",zipfile.ZIP_DEFLATED) as z:
            z.writestr(f"BTCUSDT-fundingRate-{month}.csv","\n".join(csv_lines)+"\n")
        data=stream.getvalue()
    digest=hashlib.sha256(data).hexdigest().encode("ascii")
    return data,digest


def test_archive_funding_sha_verified():
    data, digest=fake_archive()
    def download(url):
        return digest if url.endswith(".CHECKSUM") else data
    got=v552.read_funding_archive("2026-05","2026-05",download=download)
    assert len(got)==30
    assert got[0][1]==pytest.approx(0.0001)


def test_archive_funding_rejects_checksum_corruption():
    data,_=fake_archive()
    with pytest.raises(ValueError,match="SHA256 mismatch"):
        v552.read_funding_archive("2026-05","2026-05",download=lambda url: b"0"*64 if url.endswith(".CHECKSUM") else data)


def test_archive_funding_rejects_wrong_month():
    data,digest=fake_archive("2026-06")
    with pytest.raises(ValueError,match="outside archive month"):
        v552.read_funding_archive("2026-05","2026-05",download=lambda url: digest if url.endswith(".CHECKSUM") else data)


def test_funding_failure_diagnostics_and_no_zero_imputation(sources):
    _,feat,_=sources
    def deny(url):
        raise urllib.error.HTTPError(url,451,"denied",{},None)
    left,info=v552.add_funding(feat,"2026-05","2026-05",mode="optional",archive_download=deny,
                               api_fetch=lambda params:[])
    assert info["status"] == "skipped_source_unavailable"
    assert info["sources"]["binance_vision_sha256"]["http_status"] == 451
    assert not (set(v551.FUNDING_COLS) & set(left.columns))
    with pytest.raises(RuntimeError,match="required"):
        v552.add_funding(feat,"2026-05","2026-05",mode="required",archive_download=deny,api_fetch=lambda params:[])


def test_barrier_rr_and_cost_floor(sources):
    raw,feat,_=sources
    settings=v552.Settings()
    ev=v552.prepare_barriers(raw,feat,5,settings,2.5,0.8)
    good=ev["valid"]
    assert good.sum()>5500
    rr=(ev["tp_frac"][good]-settings.costs.roundtrip)/(ev["sl_frac"][good]+settings.costs.roundtrip)
    assert np.all(rr >= settings.minimum_reward_risk-1e-10)
    assert (ev["long_net"][good] <= ev["tp_frac"][good] -settings.costs.roundtrip+1e-7).all()
    assert np.isnan(ev["long_net"][~good]).all()


def test_new_regression_labels_include_roundtrip_cost(sources):
    raw,feat,_=sources
    cheap=v552.prepare_barriers(raw,feat,5,v552.Settings(costs=v55.Costs(2,1,2)),3.5,1)
    expensive=v552.prepare_barriers(raw,feat,5,v552.Settings(costs=v55.Costs(10,2,2)),3.5,1)
    # Comparison must not assume the same TP/SL execution, because geometry
    # is recomputed under each cost case.
    overlap=cheap["valid"]&expensive["valid"]
    assert overlap.sum()>3000
    assert not np.allclose(cheap["tp_frac"][overlap],expensive["tp_frac"][overlap])
    assert expensive["tp_frac"][overlap].min() >= expensive["sl_frac"][overlap].min()


def test_same_bar_tp_and_sl_tie_stops_first(sources):
    raw,feat,_=sources
    base=v552.prepare_barriers(raw,feat,5,v552.Settings(),2.5,0.8)
    i=int(np.flatnonzero(base["valid"])[300])
    p=int(np.searchsorted(raw.ts.to_numpy(),feat.ts.iloc[i]))+1
    changed=raw.copy()
    changed.loc[p,"high"] = max(changed.loc[p,"high"], float(base["tp_long"][i])*1.02)
    changed.loc[p,"low"] = min(changed.loc[p,"low"], float(base["sl_long"][i])*0.98)
    ev=v552.prepare_barriers(changed,feat,5,v552.Settings(),2.5,0.8)
    assert ev["long_side"][i] == -1
    assert ev["long_k"][i] == 1
    assert ev["long_exit"][i] == pytest.approx(ev["sl_long"][i])


def test_fourfold_purge_and_order(sources):
    raw,feat,_=sources
    ev=v552.prepare_barriers(raw,feat,15,v552.Settings(),3.5,1.1)
    folds=v552.fourfold_split(feat.ts.to_numpy(),ev["valid"],15)
    assert len(folds)==4
    for a,b in zip(folds,folds[1:]):
        assert int(feat.ts.iloc[a[-1]])+(15+1)*60_000 < int(feat.ts.iloc[b[0]])


def test_direction_gate_is_net_and_regime_aware():
    a=np.array([23.,-5.,18.,12.,np.nan])
    b=np.array([5.,21.,17.,7.,42.])
    df=pd.DataFrame({"regime_trend":[1.,0.,1.,1.,1.],"regime_high_vol":[0.,1.,0.,0.,0.]})
    assert v552.cost_aware_decisions(a,b,12,4).tolist()==[2,0,1,1,1]
    assert v552.cost_aware_decisions(a,b,12,4,"trend_only",df).tolist()==[2,1,1,1,1]
    assert v552.cost_aware_decisions(a,b,12,4,"avoid_high_vol",df).tolist()==[2,1,1,1,1]


def test_invalid_policy_inputs_fail_closed():
    with pytest.raises(ValueError):v552.cost_aware_decisions([1,2],[3],0,4)
    with pytest.raises(ValueError):v552.cost_aware_decisions([1],[2],-1,0)
    with pytest.raises(ValueError):v552.cost_aware_decisions([1],[2],0,4,"trend_only",None)


def test_validation_gate_rejects_losses_even_with_high_accuracy():
    bad={"trades":70,"average_net_bps":-2.0,"profit_factor":0.8,"maximum_drawdown_pct":4}
    _,passed=v552.score_and_gate(bad,v552.Settings())
    assert passed is False
    good={"trades":70,"average_net_bps":8.0,"profit_factor":1.5,"maximum_drawdown_pct":7}
    _,passed=v552.score_and_gate(good,v552.Settings())
    assert passed is True


def test_cost_sensitivity_does_not_change_trade_choices():
    rows=[{"net_bps_after_cost":10},{"net_bps_after_cost":-20}]
    stats=v552.cost_sensitivity(rows,24)
    assert stats["roundtrip_12bps"]["average_net_bps"]==7
    assert stats["roundtrip_24bps"]["average_net_bps"]==-5
    assert stats["roundtrip_40bps"]["average_net_bps"]==-21
    assert len(rows)==2


def test_real_lightgbm_regression_runs_without_test_labels(sources):
    raw,feat,cols=sources
    ev=v552.prepare_barriers(raw,feat,5,v552.Settings(),2.5,0.8)
    good=ev["valid"] & np.isfinite(feat[cols].to_numpy()).all(axis=1)
    tr,es,po,te=v552.fourfold_split(feat.ts.to_numpy(),good,5)
    X=feat[cols].astype(np.float32)
    model=v552.fit_net_regressor(X.iloc[tr[:500]],ev["long_net"][tr[:500]]*10_000,
                                 X.iloc[es[:200]],ev["long_net"][es[:200]]*10_000)
    p=model.predict(X.iloc[po[:8]])
    assert len(p)==8 and np.isfinite(p).all()


def test_report_generator_does_not_deploy(monkeypatch,tmp_path,sources):
    raw,feat,cols=sources
    class FixedModel:
        def predict(self,X):
            return np.full(len(X), -4.)
    monkeypatch.setattr(v552,"fit_net_regressor",lambda *args,**kwargs:FixedModel())
    monkeypatch.setattr(v552,"BARRIER_CONFIGS",(("test_one",2.5,0.8),))
    # Trim candidate cardinality to keep the correctness test fast.
    outcome,models,rows=v552.research(raw,feat,cols,barriers=(("test_one",2.5,0.8),),horizons=(5,))
    assert outcome["model_ready_for_live"] is False
    assert outcome["selection_uses_heldout_test"] is False
    assert outcome["status"] == "rejected_validation_gate_research_only"
    assert outcome["fitted_regressors"]==4 # two feature groups x two sides
    assert outcome["heldout_test_trade_metrics_shadow_only"]["trades"]==0
    assert rows == []


def test_regime_reporting_separates_direction_and_market():
    rows=[{"direction":"AL","market_regime":"TREND","net_bps_after_cost":30},
          {"direction":"SAT","market_regime":"RANGE","net_bps_after_cost":-10}]
    m=v552.per_regime_metrics(rows)
    assert m["regime:TREND"]["trades"] == 1
    assert m["direction:SAT"]["average_net_bps"]==-10
