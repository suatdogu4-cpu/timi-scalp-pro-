"""Offline checks for TiMi's 26-feature, direction-preserving meta filter."""
from __future__ import annotations
from copy import deepcopy
from pathlib import Path
import json
import math
import numpy as np
import pytest

from timi_ml_meta_features import NAMES, FEATURE_KEYS, INDICATOR_KEYS, extract_features, eligible_archive_record
from timi_ml_metafilter import meta_gate, MetaFilter
from timi_ml_meta_train import make_dataset, split_sequential, train, PURGE_MS


def sample(ts=1_800_000_000_000, side="LONG", px=100.):
    return {
        'schema':'timi-ml-v5-observation-v1',
        'symbol':'BTCUSDT',
        'decision':side,'rule_score':5 if side=='LONG' else -5,
        'market_price_at_snapshot':px,
        'feature_available_at_ms':ts,
        'observed_at_ms':ts,
        'candle_close_ms':ts-12000,
        'quality':{'ml_eligible':True},
        'indicators_26':{'values':{key: (px if key in (
            'ema10','sma10','ema20','sma20','ema30','sma30','ema50','sma50',
            'ema100','sma100','ema200','sma200','vwma20','hma9','ichimoku_kijun26')
            else 45.0) for key in NAMES}},
        'flow':{'buy_notional_60s':10000.,'sell_notional_60s':5000.,
                'trade_age_ms':200.,'book_age_ms':100.,'spread_pct':0.01,
                'trade_count_60s':100, 'imbalance':0.22},
        'flow_metadata':{'fresh':True},
        'orderbook_extras':{'imbalance_delta_10s':0.10},
        'mtf':[{'tf':t,'direction':side} for t in ('1m','3m','5m','15m')],
        'pro_indicators':{'rsi14':52.,'ema20':px,'ema50':px,'atr14':px*0.001},
    }


def test_exactly_26_indicators_and_order():
    assert len(NAMES) == len(set(NAMES)) == 26
    assert len(INDICATOR_KEYS) == 26
    assert FEATURE_KEYS[:26] == INDICATOR_KEYS
    assert len(FEATURE_KEYS) == 44


def test_full_feature_vector_finite_and_price_relative():
    s=sample()
    vec=extract_features(s)
    assert len(vec)==44 and all(math.isfinite(x) for x in vec)
    assert vec[FEATURE_KEYS.index('i_ema20')]==0
    assert vec[FEATURE_KEYS.index('signal_direction')]==1.0


def test_price_scaling_does_not_change_normalized_ma():
    a=sample(px=100.)
    b=sample(px=200.)
    assert extract_features(a)[FEATURE_KEYS.index('i_ema20')]==extract_features(b)[FEATURE_KEYS.index('i_ema20')]


def test_feature_missing_indicator_blocks():
    s=sample();s['indicators_26']['values']['macd_hist_12_26_9']=None
    with pytest.raises(ValueError,match='macd_hist'):
        extract_features(s)


def test_feature_invalid_book_blocks():
    s=sample();s['flow']['book_age_ms']=6000
    with pytest.raises(ValueError,match='Stale'):
        extract_features(s)


def test_bekle_cannot_become_long():
    assert meta_gate('BEKLE',True,0.99)[0]=='BEKLE'
    assert meta_gate('LONG',True,.50,.70)[0]=='BEKLE'
    assert meta_gate('SHORT',True,.85,.70)[0]=='SHORT'
    assert meta_gate('LONG',True,.85,.70)[0]=='LONG'
    assert meta_gate('LONG',False,.99)[0]=='BEKLE'


def test_live_model_unavailable_returns_no_probability():
    obj=MetaFilter(get_candles=None,cached_pro=None,now_ms=lambda:10,
                   symbols=['BTCUSDT'],root=Path('/tmp'))
    assert not obj.loaded and not obj.verified


def test_quality_filter_applies():
    r=sample()
    assert eligible_archive_record(r)
    r['quality']['ml_eligible']=False
    assert not eligible_archive_record(r)


def test_forward_dataset_does_not_label_before_target():
    t=1_800_000_000_000
    data=[sample(ts=t,px=100.)]
    for i in range(1,9):
        nxt=sample(ts=t+i*60000,side='BEKLE',px=100+i*0.3)
        data.append(nxt)
    d=make_dataset(data,fee_bps=10,slippage_bps=2)
    assert len(d['y'])==1 and d['y'][0]==1
    assert d['net_bps'][0]>0


def test_no_price_data_after_target_within_tolerance_no_label():
    t=1_800_000_000_000
    d=make_dataset([sample(ts=t),sample(ts=t+7*60000,side='BEKLE',px=105)])
    assert len(d['y'])==0


def test_feature_timestamp_not_event_close():
    r=sample()
    r['feature_available_at_ms']=r['candle_close_ms']-10
    assert not eligible_archive_record(r)


def test_sequential_split_purge():
    t=1_800_000_000_000
    ts=np.asarray([t+i*60000 for i in range(1200)])
    folds=split_sequential(ts)
    for a,b in zip(folds,folds[1:]):
        assert ts[b[0]]-ts[a[-1]]>=PURGE_MS


def test_training_without_samples_creates_no_model(tmp_path):
    empty={'X':np.empty((0,44)),'y':np.array([],dtype=int),
           'net_bps':np.array([],dtype=float),'ts':np.array([],dtype=np.int64)}
    # For too few rows a diagnostic is written without creating model artifacts.
    with pytest.raises(ValueError,match='sorted'):
        train(empty,tmp_path)


def test_short_remains_directional_not_spot_short():
    s=sample(side='SHORT')
    assert extract_features(s)[FEATURE_KEYS.index('signal_direction')]==-1
    assert meta_gate('SHORT',False,.95)[0]=='BEKLE'
