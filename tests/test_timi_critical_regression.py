"""TiMi main.py data-quality and reconnect regression tests; no Binance calls."""
import time
import main


def bars(n=251, interval_ms=60000):
    start = (int(time.time() * 1000)//interval_ms - n - 1)*interval_ms
    return [{'t': start+i*interval_ms, 'o':100+i*.01, 'h':100+i*.01+.1,
             'l':100+i*.01-.1, 'c':100+i*.01+.02, 'v':100., 'closed':True}
            for i in range(n)]


def test_valid_closed_candles_pass():
    c=bars()
    assert main.closed_candles_valid(c,'1m')
    assert main.feature_frame(c, interval='1m') is not None


def test_gap_duplicate_and_invalid_candles_fail_closed():
    c=bars()
    c.pop(90)
    assert not main.closed_candles_valid(c,'1m')
    assert main.feature_frame(c, interval='1m') is None
    c=bars()
    c[90]['t']=c[89]['t']
    assert not main.closed_candles_valid(c,'1m')
    c=bars()
    c[120]['h']=c[120]['l']-1
    assert not main.closed_candles_valid(c,'1m')


def test_mtf_gap_not_accidentally_accepted_as_one_minute():
    c=bars(251, 300000)
    assert main.closed_candles_valid(c, '5m')
    assert not main.closed_candles_valid(c, '1m')


def test_reconnect_clears_state_for_every_source():
    s='BTCUSDT';m=main.markets[s]
    m.trades.appendleft({'price':1});m.tape.append((main.now_ms(),'buy',1,1))
    m.book_history.append((main.now_ms(),.9));m.bids=[[1.,1.]]
    m.last_trade_ms=main.now_ms();m.book_valid=True
    main.v2_live.record_trade(s,'buy',1,1,main.now_ms())
    main.v23_live.record_trade(s,'buy',1,1,main.now_ms())
    main.reset_stream_state()
    assert not m.trades and not m.tape and not m.book_history and not m.bids
    assert not m.book_valid and m.last_trade_ms is None
    assert not main.v2_live.buckets[s] and not main.v23_live.trades[s]
