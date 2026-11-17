"""TiMi SCALP PRO — QUANT LAB v1.0 (read-only research module).

Uses the existing Binance spot relay and candle cache; no extra upstream
WebSocket, exchange keys, orders, model training or invented probabilities.
"""
from __future__ import annotations

import asyncio
import math
import statistics
import time
from collections import deque

from fastapi import HTTPException

VERSION = '1.0.0'
STEPS = {'1m': 60000, '3m': 180000, '5m': 300000, '15m': 900000}
ROUNDTO = 20.0  # hypothetical roundtrip bps, illustrative, user-specific costs vary


def _ema(values, period):
    result = [None] * len(values)
    if len(values) < period:
        return result
    previous = sum(values[:period]) / period
    result[period - 1] = previous
    k = 2.0 / (period + 1.0)
    for i in range(period, len(values)):
        previous += k * (values[i] - previous)
        result[i] = previous
    return result


def _rsi(values, period=14):
    result = [None] * len(values)
    if len(values) < period + 1:
        return result
    moves = [values[i] - values[i - 1] for i in range(1, len(values))]
    gain = sum(max(x, 0) for x in moves[:period]) / period
    loss = sum(max(-x, 0) for x in moves[:period]) / period

    def value(g, l):
        return 100.0 if l == 0 and g > 0 else 50.0 if l == 0 else 100 - 100 / (1 + g / l)

    result[period] = value(gain, loss)
    for i in range(period + 1, len(values)):
        x = moves[i - 1]
        gain = (gain * (period - 1) + max(x, 0)) / period
        loss = (loss * (period - 1) + max(-x, 0)) / period
        result[i] = value(gain, loss)
    return result


def _clean(value, digits=5):
    try:
        x = float(value)
        return round(x, digits) if math.isfinite(x) else None
    except (ValueError, TypeError, OverflowError):
        return None


def validate_candles(candles, interval):
    """No missing, duplicate, incomplete, non-finite or malformed bars."""
    step = STEPS.get(interval)
    if not step or not 75 <= len(candles) <= 500:
        return False
    last_t = None
    for c in candles:
        try:
            t = int(c['t'])
            o, h, l, close, v = (float(c[k]) for k in ('o', 'h', 'l', 'c', 'v'))
            if c['closed'] is not True or t % step != 0:
                return False
            if not all(math.isfinite(x) for x in (o, h, l, close, v)):
                return False
            if l <= 0 or v < 0 or h < max(o, close, l) or l > min(o, close):
                return False
            if last_t is not None and t - last_t != step:
                return False
            last_t = t
        except (KeyError, TypeError, ValueError, OverflowError):
            return False
    return True


def _backward_diagnostic(bars, ema20, ema50, rsi, roundtrip_cost_bps=ROUNDTO):
    """Prior CLOSED bar chooses NEXT bar open->close hypothetical trade.

    Fixed rule only, not fitted, not OOS ML, no executable fill model.
    Last 40% of bar indices evaluated to avoid presenting a full-history fit.
    """
    start = max(60, int(len(bars) * .6))
    trades = []
    for i in range(start, len(bars)):
        p = i - 1
        e20, e50, r = ema20[p], ema50[p], rsi[p]
        if None in (e20, e50, r):
            continue
        prev_close = float(bars[p]['c'])
        direction = 1 if prev_close > e20 > e50 and r >= 55 else -1 if prev_close < e20 < e50 and r <= 45 else 0
        if not direction:
            continue
        op, close = float(bars[i]['o']), float(bars[i]['c'])
        # Simple one-bar directional observation, hypothetical short is not
        # executable on spot. No funding, market impact or actual fill data.
        net_bps = direction * (close / op - 1.0) * 10000 - roundtrip_cost_bps
        trades.append(net_bps)
    n = len(trades)
    wins = [x for x in trades if x > 0]
    losses = [x for x in trades if x <= 0]
    return {
        'sample_count': n,
        'sample_sufficient': n >= 30,
        'win_rate_pct': _clean(100 * len(wins) / n, 2) if n >= 30 else None,
        'mean_net_bps': _clean(statistics.mean(trades), 3) if n >= 30 else None,
        'sum_net_bps': _clean(sum(trades), 3) if n >= 30 else None,
        'profit_factor': _clean(sum(wins) / abs(sum(losses)), 3) if n >= 30 and sum(losses) < 0 else None,
        'test_period_bars': len(bars) - start,
        'roundtrip_assumed_bps': roundtrip_cost_bps,
        'method': ('Sabit EMA20/EMA50/RSI kuralı; önceki kapanmış mum ile yön, '
                   'sonraki mumun açılış-kapanış hareketi, varsayımsal çift yön maliyeti. '
                   'Son %40 tarihsel mumda geriye dönük tanı testi; ML veya yürütülebilir backtest değildir.'),
        'caution': ('Sonuçlar örneklem içi tarihsel gözlemdir; performans kanıtı veya '
                    'canlı sinyal başarı olasılığı değildir. Spot SHORT varsayımsaldır.'),
    }


def analyze_candles(candles, interval, *, now_ms, flow, market_price=None):
    """Pure, testable analytical function. No network or live state mutation."""
    closed = [dict(c) for c in candles if c.get('closed') is True]
    if not validate_candles(closed, interval):
        raise ValueError('Yeterli ve kesintisiz doğrulanmış kapanmış mum bulunamadı')
    close = [float(c['c']) for c in closed]
    volume = [float(c['v']) for c in closed]
    ret = [math.log(close[i] / close[i - 1]) for i in range(1, len(close))]
    e20, e50, rsi = _ema(close, 20), _ema(close, 50), _rsi(close)
    last_close_at = int(closed[-1]['t']) + STEPS[interval]
    age = now_ms - last_close_at
    candle_fresh = -5000 <= age <= 2 * STEPS[interval]
    live_fresh = bool(flow.get('fresh'))
    spread = _clean(flow.get('spread_pct'), 6)
    book_imbalance = _clean(flow.get('imbalance'), 5)
    buy = _clean(flow.get('buy_notional_60s')) or 0
    sell = _clean(flow.get('sell_notional_60s')) or 0
    aggressor = (buy - sell) / (buy + sell) if buy + sell > 0 else None
    spread_ok = spread is not None and 0 <= spread <= .12
    volume_average = statistics.mean(volume[-21:-1])
    vol_ratio = volume[-1] / volume_average if volume_average > 0 else None
    mean20, stdev20 = statistics.mean(close[-20:]), statistics.pstdev(close[-20:])
    # Realized vol over prior 20 closed intervals (not an annualized forecast).
    rv20 = statistics.stdev(ret[-20:]) * 100 if len(ret) >= 21 else None
    ret_autocorr = None
    if len(ret) >= 42:
        x, y = ret[-21:-1], ret[-20:]
        sx, sy = statistics.stdev(x), statistics.stdev(y)
        if sx > 1e-12 and sy > 1e-12:
            mx, my = statistics.mean(x), statistics.mean(y)
            ret_autocorr = sum((a - mx) * (b - my) for a, b in zip(x, y)) / (len(x) * sx * sy)
    true_ranges = []
    for i in range(-14, 0):
        bar = closed[i]
        prev = close[i - 1]
        true_ranges.append(max(float(bar['h']) - float(bar['l']), abs(float(bar['h']) - prev), abs(float(bar['l']) - prev)))
    atr14 = statistics.mean(true_ranges)
    trend = 'YATAY'
    if close[-1] > e20[-1] > e50[-1] and rsi[-1] >= 52:
        trend = 'YUKARI'
    elif close[-1] < e20[-1] < e50[-1] and rsi[-1] <= 48:
        trend = 'ASAGI'
    block = []
    if not live_fresh: block.append('İşlem akışı veya doğrulanmış emir defteri güncel değil')
    if not candle_fresh: block.append('Son kapanmış mum güncel değil')
    if not spread_ok: block.append('Spread eksik veya risk eşiğini aşıyor')
    if (flow.get('trade_count_60s') or 0) < 10: block.append('Son 60 saniye işlem sayısı yetersiz')
    if book_imbalance is None or aggressor is None: block.append('Emir defteri veya taker işlemleri eksik')
    positive = trend == 'YUKARI' and book_imbalance is not None and book_imbalance > .15 and aggressor is not None and aggressor > .12
    negative = trend == 'ASAGI' and book_imbalance is not None and book_imbalance < -.15 and aggressor is not None and aggressor < -.12
    decision = 'LONG_ADAY' if positive and not block else 'SHORT_ADAY' if negative and not block else 'BEKLE'
    reasons = list(block) if block else [
        'EMA20 / EMA50 trend: ' + trend,
        'Defter dengesizliği: ' + (f'{book_imbalance:+.3f}' if book_imbalance is not None else 'yok'),
        'Agresif işlem farkı: ' + (f'{aggressor:+.3f}' if aggressor is not None else 'yok'),
    ]
    if not block and decision == 'BEKLE': reasons.append('Üçlü yön doğrulaması sağlanmadı')
    return {
        'version': VERSION, 'interval': interval, 'evaluated_at_ms': now_ms,
        'source': 'Binance Spot closed candles + existing TiMi WebSocket orderflow',
        'market_price': _clean(market_price, 8), 'last_closed_price': _clean(close[-1], 8),
        'last_closed_at_ms': last_close_at, 'last_closed_age_ms': age,
        'bar_count': len(closed),
        'data_health': {'valid_closed_bars': True, 'fresh_candle': candle_fresh,
                        'fresh_live_orderflow': live_fresh, 'spread_ok': spread_ok,
                        'trade_age_ms': flow.get('trade_age_ms'), 'book_age_ms': flow.get('book_age_ms'),
                        'trade_count_60s': flow.get('trade_count_60s'), 'ready': not block},
        'metrics': {
            'ema20': _clean(e20[-1], 8), 'ema50': _clean(e50[-1], 8), 'rsi14': _clean(rsi[-1], 3),
            'return_1bar_pct': _clean((close[-1] / close[-2] - 1) * 100, 4),
            'return_5bar_pct': _clean((close[-1] / close[-6] - 1) * 100, 4),
            'realized_vol_20bar_pct': _clean(rv20, 5),
            'return_autocorr_20bar': _clean(ret_autocorr, 4),
            'price_zscore_20bar': _clean((close[-1] - mean20) / stdev20, 3) if stdev20 > 0 else None,
            'atr14_pct': _clean(100 * atr14 / close[-1], 4),
            'volume_ratio_20bar': _clean(vol_ratio, 3),
            'book_imbalance': book_imbalance,
            'aggressor_imbalance_60s': _clean(aggressor, 4),
            'spread_bps': _clean(spread * 100, 3) if spread is not None else None,
            'cvd_60s_usdt': _clean(flow.get('cvd_60s')),
        },
        'regime': {'trend': trend, 'volatility': 'YUKSEK' if rv20 is not None and rv20 >= .3 else 'ORTA/DUSUK',
                   'volatility_threshold_explanatory': '0.30% / bar (sabit gösterim eşiği; istatistiksel rejim modeli değil)'},
        'signal': {'decision': decision, 'reasons': reasons,
                   'type': 'heuristic_candidate_not_trading_advice',
                   'probabilities': None, 'calibration': 'not_trained'},
        'diagnostic': _backward_diagnostic(closed, e20, e50, rsi),
        'limitations': ['Gerçek emir gönderimi yok', 'Bu veriler geleceği garanti etmez',
                        'Spot SHORT yalnızca yön araştırmasıdır',
                        '20 seviye kısmi emir defteri tam piyasa derinliği değildir',
                        'Geçmiş diagnostic sonuçları bağımsız walk-forward veya canlı model testi değildir'],
    }


def install_quant_lab(app, *, markets, get_candles, orderflow, now_ms):
    """Registers one read-only endpoint without modifying existing state."""
    cache = {}
    locks = {s: asyncio.Lock() for s in markets}

    @app.get('/api/quant/{symbol}')
    async def quant_lab(symbol: str, interval: str = '1m'):
        symbol = symbol.upper()
        if symbol not in markets or interval not in STEPS:
            raise HTTPException(400, 'Desteklenmeyen parite veya zaman dilimi')
        # Candle observations are cached. Live orderflow is always sampled AFTER any await.
        async with locks[symbol]:
            key = (symbol, interval)
            cached = cache.get(key)
            if cached and time.monotonic() - cached[0] < 8:
                candles = cached[1]
            else:
                candles = await get_candles(symbol, interval, 250)
                cache[key] = (time.monotonic(), candles)
        current_flow = orderflow(markets[symbol])
        try:
            result = await asyncio.to_thread(
                analyze_candles, candles, interval,
                now_ms=now_ms(), flow=current_flow, market_price=markets[symbol].price)
        except ValueError as exc:
            raise HTTPException(422, str(exc)) from exc
        result['symbol'] = symbol
        return result
    return quant_lab
