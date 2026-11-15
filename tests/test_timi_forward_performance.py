"""Offline deterministic checks for forward-only observational performance."""
from dataclasses import dataclass, field

from timi_forward_performance import ForwardTracker


@dataclass
class Market:
    last_book_ms: int = 0
    book_valid: bool = True
    bids: list = field(default_factory=lambda: [[99.9, 10]])
    asks: list = field(default_factory=lambda: [[100.1, 10]])


def observation(ts=65_000, direction='LONG', symbol='BTCUSDT'):
    return {'observed_at_ms': ts, 'symbol': symbol, 'direction': direction,
            'score': 5, 'entry_reference_price': 100, 'closed_candle_at_ms': 0,
            'outcomes': {'1m': {'status': 'pending'}, '5m': {'status': 'pending'}}}


def tracker():
    return ForwardTracker(['BTCUSDT'], now_ms=lambda: 0, pg_url='', fee_bps=10, slippage_bps=2)


def trade(t, price, m, tracker, symbol='BTCUSDT', bid=None, ask=None):
    m.last_book_ms = t
    m.bids = [[price-0.1 if bid is None else bid, 10]]
    m.asks = [[price+0.1 if ask is None else ask, 10]]
    tracker.on_trade(symbol, price, t, m)


def test_never_measure_before_next_minute_open():
    p=tracker()
    rec=p.on_signal(observation())
    m=Market()
    trade(119_999, 101, m, p)
    assert rec['entry']['status']=='pending'
    assert p.summary('BTCUSDT')['horizons']['1m']['measured']==0
    trade(120_000, 101, m, p)
    assert rec['entry']['status']=='measured'
    assert rec['entry']['reference_quote']==101.1
    assert rec['outcomes']['1m']['target_ms']==180_000
    trade(179_999, 200, m, p)
    assert rec['outcomes']['1m']['status']=='pending'


def test_horizon_costs_spread_and_win_counts():
    p=tracker()
    rec=p.on_signal(observation())
    m=Market()
    trade(120_000, 100, m, p)
    trade(180_000, 102, m, p)
    outcome=rec['outcomes']['1m']
    assert outcome['status']=='measured'
    assert outcome['gross_pct']==round((101.9/100.1-1)*100,6)
    assert outcome['cost_pct']==0.24
    assert outcome['net_pct']==round(outcome['gross_pct']-0.24,6)
    assert outcome['net_positive'] is True
    trade(420_000, 105, m, p)
    a=p.summary('BTCUSDT')
    assert a['horizons']['1m']['measured']==1
    assert a['horizons']['5m']['measured']==1
    assert a['horizons']['1m']['net_positive_pct']==100.0
    assert a['horizons']['1m']['insufficient_sample'] is True


def test_short_side_quotes_and_cost():
    p=tracker()
    rec=p.on_signal(observation(direction='SHORT'))
    m=Market()
    trade(120_000, 100, m, p)
    assert rec['entry']['reference_quote']==99.9
    trade(180_000, 98, m, p)
    outcome=rec['outcomes']['1m']
    assert outcome['reference_exit_quote']==98.1
    assert outcome['net_positive'] is True
    assert outcome['net_pct']==round((1-98.1/99.9)*100-0.24,6)


def test_no_lookahead_and_missing_excluded_from_accuracy():
    p=tracker()
    rec=p.on_signal(observation())
    m=Market()
    trade(125_100, 100, m, p)
    assert rec['entry']['status']=='missing_data'
    assert rec['outcomes']['1m']['status']=='missing_data'
    a=p.summary('BTCUSDT')
    assert a['horizons']['1m']['measured']==0
    assert a['horizons']['1m']['net_positive_pct'] is None
    assert a['horizons']['1m']['missing']==1


def test_depth_stale_rejected_and_reconnect_marks_pending_missing():
    p=tracker();rec=p.on_signal(observation());m=Market()
    m.last_book_ms=110_000
    p.on_trade('BTCUSDT',100,120_000,m)
    assert rec['entry']['status']=='pending'
    p.on_stream_reset()
    assert rec['entry']['status']=='missing_data'
    assert rec['outcomes']['1m']['status']=='missing_data'


def test_deduplicate_and_wait_does_not_count():
    p=tracker()
    a=observation()
    assert p.on_signal(a) is not None
    assert p.on_signal(a) is not None
    assert p.on_signal(observation(direction='BEKLE')) is None
    assert p.summary('BTCUSDT')['sampled_long_short']==1


def test_no_cross_restart_false_measures():
    p=tracker()
    rec=p.on_signal(observation())
    m=Market()
    trade(120_000,100,m,p)
    p._finish_unobserved(rec,'server_restarted')
    p._insert(rec)
    assert rec['outcomes']['1m']['status']=='missing_data'
    assert p.summary('BTCUSDT')['horizons']['1m']['measured']==0


def test_disabled_symbol_has_clear_empty_summary():
    p=ForwardTracker(['BTCUSDT','ETHUSDT'], now_ms=lambda:0, pg_url='', enabled_symbols=['BTCUSDT'])
    assert p.summary('ETHUSDT')['enabled'] is False
    assert p.on_signal(observation(symbol='ETHUSDT')) is None


def test_net_correct_incorrect_and_long_short_counts():
    p=tracker();m=Market()
    p.on_signal(observation());trade(120_000,100,m,p);trade(180_000,101,m,p)
    summary=p.summary('BTCUSDT')['horizons']['1m']
    assert summary['measured']==1
    assert summary['net_positive']==1
    assert summary['net_not_positive']==0
    assert summary['by_direction']['LONG']['measured']==1
    assert summary['by_direction']['SHORT']['measured']==0


def test_bad_fee_environment_never_crashes_main():
    p=ForwardTracker(['BTCUSDT'], now_ms=lambda:0, pg_url='', fee_bps='not-a-number',slippage_bps='NaN')
    assert p.fee_bps==10.0
    assert p.slippage_bps==2.0
