"""No-network v5.5.1 tests; fake data are NOT evidence of trading performance."""
from __future__ import annotations

import math
import sys
from datetime import datetime, timezone
from pathlib import Path

import numpy as np
import pandas as pd
import pytest

sys.path.insert(0, str(Path(__file__).resolve().parent))
import timi_ml_v54_history_train as v54
import timi_ml_v55_research as v55
import timi_ml_v551_research as v551


def fake_klines(n=11000, *, gap=None):
    rng = np.random.default_rng(991)
    t0 = int(datetime(2026, 5, 1, tzinfo=timezone.utc).timestamp() * 1000)
    ts = t0 + np.arange(n, dtype=np.int64) * v54.MS
    if gap is not None:
        ts[gap:] += 2*v54.MS
    ret = rng.normal(0, 0.0015, n)
    close = 75000*np.exp(np.cumsum(ret))
    op = np.r_[75000,close[:-1]]
    hi = np.maximum(close,op)*(1+rng.uniform(0,0.0006,n))
    lo = np.minimum(close,op)*(1-rng.uniform(0,0.0006,n))
    vol = rng.uniform(5,25,n)
    buy = vol*rng.uniform(0.3,0.7,n)
    return list(zip(ts.tolist(),op.tolist(),hi.tolist(),lo.tolist(),close.tolist(),vol.tolist(),
                    [100]*n,buy.tolist(),(vol*close).tolist()))


@pytest.fixture(scope='module')
def frame():
    df=v54.historical_frame(['2026-05'],download=lambda _:fake_klines())
    base, cols=v54.features_targets(df)
    ext=v551.enrich_cvd_regime(df,base)
    return df,ext,cols


def test_cvd_and_regime_feature_limits(frame):
    raw,ext,cols=frame
    assert len(ext)>10000
    assert set(v551.CVD_COLS+v551.REGIME_COLS).issubset(ext.columns)
    assert np.isfinite(ext[list(v551.CVD_COLS+v551.REGIME_COLS)].to_numpy(dtype=float)).all()
    for n in (5,15,60):
        assert (ext[f'cvd_ratio_{n}'].abs()<=1.000001).all()
    assert ext.efficiency_60.ge(0).all() and ext.efficiency_60.le(1.000001).all()


def test_no_future_leakage_in_cvd_and_regime(frame):
    raw,ext,_=frame
    change_at=6400
    target_ts=int(raw.ts.iloc[change_at])
    older=ext[ext.ts < target_ts].set_index('ts')
    altered=raw.copy()
    altered.loc[altered.ts >= target_ts, 'taker_buy_volume']=0.0
    altered.loc[altered.ts >= target_ts, 'high']*=1.2
    newer=v551.enrich_cvd_regime(altered, ext.drop(columns=list(v551.CVD_COLS+v551.REGIME_COLS)))
    newer=newer[newer.ts < target_ts].set_index('ts')
    pd.testing.assert_frame_equal(older[list(v551.CVD_COLS+v551.REGIME_COLS)],
                                  newer[list(v551.CVD_COLS+v551.REGIME_COLS)])


def test_funding_only_after_settlement_plus_latency():
    t0=int(datetime(2026,5,1,tzinfo=timezone.utc).timestamp()*1000)
    funding_t=t0+8*v551.MS
    feat=pd.DataFrame({'ts':[t0+10*v551.MS,t0+11*v551.MS,t0+12*v551.MS,t0+13*v551.MS]})
    aligned,report=v551.enrich_funding(feat,[(funding_t,0.0001)])
    assert aligned.funding_last_rate.isna().iloc[:2].all()
    assert aligned.funding_last_rate.iloc[2]==pytest.approx(0.0001)
    assert aligned.funding_age_hours.iloc[2]==pytest.approx(0)
    assert report['status']=='skipped_insufficient_funding_coverage'


def test_funding_stale_is_missing():
    t0=int(datetime(2026,5,1,tzinfo=timezone.utc).timestamp()*1000)
    feat=pd.DataFrame({'ts':[t0+5*v551.MS, t0+(25*60+5)*v551.MS]})
    aligned,_=v551.enrich_funding(feat,[(t0,0.0002)])
    assert aligned.funding_last_rate.iloc[0]==pytest.approx(0.0002)
    assert math.isnan(aligned.funding_last_rate.iloc[1])


def test_no_invented_funding_and_required_mode():
    with pytest.raises(ValueError,match='No usable'):
        v551.funding_history('2026-05','2026-05',fetch=lambda params:[])
    assert 'D_base_cvd_regime_funding' not in v551.feature_groups(['x'],False)


def test_funding_pagination_no_future():
    t0=int(datetime(2026,5,1,tzinfo=timezone.utc).timestamp()*1000)
    records=[{'symbol':'BTCUSDT','fundingTime':t0+i*4*3600*1000,'fundingRate':'0.0001'} for i in range(1005)]
    calls=[]
    def fetch(params):
        calls.append(params['startTime'])
        return [x for x in records if params['startTime']<=x['fundingTime']<=params['endTime']][:1000]
    got=v551.funding_history('2026-05','2026-10',fetch=fetch)
    assert len(got)==1005
    assert len(calls)==2
    assert got[0][0]==t0 and got[-1][0]==records[-1]['fundingTime']


def test_feature_groups_are_nested(frame):
    _,ext,cols=frame
    groups=v551.feature_groups(cols,True)
    assert [len(groups[x]) for x in groups]==[len(cols),len(cols)+5,len(cols)+14,len(cols)+19]
    assert all(x in ext for x in groups['C_base_cvd_regime'])


def test_same_holdout_not_used_to_select(monkeypatch,frame):
    raw,feat,cols=frame
    class MockModel:
        classes_ = np.array([0,1,2])
        def predict_proba(self,X):
            n=len(X)
            # fixed probabilities, no access to test target
            return np.tile([0.30,0.19,0.51],(n,1))
    monkeypatch.setattr(v55,'fit_candidate',lambda *args,**kwargs:MockModel())
    r,model,rows=v551.research(raw,feat,v551.feature_groups(cols,False))
    assert r['heldout_test_used_for_selection'] is False if model else r['model_ready_for_live'] is False
    assert r['model_ready_for_live'] is False
    assert r['orders_enabled'] is False if model else r['model_ready_for_live'] is False
    assert r['trained_candidate_count'] >= 3
    if model:
        assert 'validation_candidates' in r and 'heldout_test_trade_metrics' in r
        assert all(x['reference_only_not_executed'] for x in rows)


def test_funding_group_can_fit_real_lightgbm(frame):
    # One real estimator test with synthetic candles and funding events, not
    # a claim about profitability or market performance.
    raw,feat,base=frame
    t0=int(raw.ts.iloc[0])
    events=[(t0+i*8*60*v551.MS,0.0001*math.sin(i)) for i in range(24)]
    joined,summary=v551.enrich_funding(feat,events)
    assert summary['status']=='usable'
    cols=v551.feature_groups(base,True)['D_base_cvd_regime_funding']
    ev=v55.build_events(raw,joined,5,v55.Costs())
    tr,va,te=v55.split_indices(joined.ts.to_numpy(dtype=np.int64),ev['valid'],5)
    model=v55.fit_candidate(joined[cols].astype('float32').iloc[tr],ev['labels']['net_close'][tr],
                            joined[cols].astype('float32').iloc[va],ev['labels']['net_close'][va],None)
    pp=model.predict_proba(joined[cols].astype('float32').iloc[te][:7])
    assert pp.shape==(7,3)
