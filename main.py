"""TiMi SCALP PRO v1.8.4 — read-only Binance Spot analytics.
Developer: Suat DOĞU. No order endpoints or model training on request path.
"""
import asyncio
import json
import logging
import math
import statistics
import os
import time
from collections import deque
from decimal import Decimal, InvalidOperation
from contextlib import asynccontextmanager
from pathlib import Path

import httpx
import pandas as pd
import pandas_ta_classic as ta
import websockets
from fastapi import FastAPI, HTTPException, WebSocket, WebSocketDisconnect
from fastapi.middleware.cors import CORSMiddleware
from fastapi.responses import FileResponse

logging.basicConfig(level=logging.INFO)
LOG = logging.getLogger('timi')
ROOT = Path(__file__).resolve().parent
VERSION = '1.8.4'
SYMBOLS = ('BTCUSDT', 'ETHUSDT', 'SOLUSDT', 'BNBUSDT', 'XRPUSDT', 'DOGEUSDT', 'ADAUSDT', 'AVAXUSDT')
TF = ('1m', '3m', '5m', '15m', '30m', '1h', '4h', '1d')
MTF = ('1m', '3m', '5m', '15m')
STREAM = 'wss://data-stream.binance.vision/stream?streams='
REST = 'https://data-api.binance.vision/api/v3'

def now_ms():
    return int(time.time() * 1000)

def finite(value):
    try:
        value = float(value)
        return value if math.isfinite(value) else None
    except (ValueError, TypeError, OverflowError):
        return None

class Market:
    def __init__(self, symbol):
        self.symbol = symbol
        self.price = self.change = self.volume = self.high = self.low = None
        self.bid = self.ask = None
        self.bids, self.asks = [], []
        self.trades = deque(maxlen=100)
        self.tape = deque()
        self.book_history = deque(maxlen=500)
        # Bounded, immutable Binance partial-book snapshots for update-ID audit.
        self.depth_snapshots = deque(maxlen=240)
        self.wall_seen = {'bid': {}, 'ask': {}}
        self.last_trade_ms = self.last_book_ms = self.last_ticker_ms = None
        self.last_trade_id = None
        self.last_book_update_id = None
        self.sequence = 0
        self.book_valid = False
        self.book_error = None
        self.live_candle = None
        self.last_candle_ms = None
        self.last_closed_candle = None

    def public(self):
        cutoff = now_ms() - 60000
        tape = [x for x in self.tape if x[0] >= cutoff]
        return dict(symbol=self.symbol, price=self.price, change=self.change,
                    volume=self.volume, high=self.high, low=self.low,
                    bid=self.bid, ask=self.ask, bids=self.bids, asks=self.asks,
                    trades=list(self.trades),
                    buy_qty=sum(x[2] for x in tape if x[1] == 'buy'),
                    sell_qty=sum(x[2] for x in tape if x[1] == 'sell'),
                    last_trade_ms=self.last_trade_ms, last_book_ms=self.last_book_ms,
                    last_ticker_ms=self.last_ticker_ms, sequence=self.sequence,
                    source='Binance Spot', book_valid=self.book_valid,
                    book_error=self.book_error,
                    last_candle_ms=self.last_candle_ms)

markets = {s: Market(s) for s in SYMBOLS}
clients = []
status = {'connected': False, 'last_message_ms': None, 'error': None,
          'endpoint': 'data-stream.binance.vision'}
http_client = None
candle_cache = {}
candle_locks = {}
analysis_cache = {}
analysis_locks = {}
full_cache = {}
full_semaphore = asyncio.Semaphore(1)
depth_audit_semaphore = asyncio.Semaphore(1)

async def bootstrap():
    async def one(symbol):
        try:
            response = await http_client.get(f'{REST}/ticker/24hr', params={'symbol': symbol})
            response.raise_for_status()
            v = response.json()
            m = markets[symbol]
            m.price = finite(v.get('lastPrice'))
            m.change = finite(v.get('priceChangePercent'))
            m.volume = finite(v.get('quoteVolume'))
            m.high = finite(v.get('highPrice'))
            m.low = finite(v.get('lowPrice'))
            m.last_ticker_ms = now_ms()
        except Exception as exc:
            LOG.warning('Bootstrap %s: %s', symbol, exc)
    await asyncio.gather(*(one(s) for s in SYMBOLS))

async def broadcast(symbol, event):
    payload = json.dumps(event, separators=(',', ':'))
    for client in tuple(clients):
        if client['symbol'] != symbol:
            continue
        queue = client['queue']
        if queue.full():
            try:
                queue.get_nowait()
            except asyncio.QueueEmpty:
                pass
        try:
            queue.put_nowait(payload)
        except asyncio.QueueFull:
            pass

async def relay():
    names = []
    for symbol in SYMBOLS:
        s = symbol.lower()
        names.extend((f'{s}@trade', f'{s}@depth20@100ms',
                      f'{s}@ticker', f'{s}@kline_1m'))
    delay = 1
    while True:
        try:
            async with websockets.connect(STREAM + '/'.join(names),
                                          ping_interval=20, ping_timeout=20,
                                          open_timeout=15, max_size=4_000_000) as ws:
                status.update(connected=True, error=None)
                delay = 1
                # Never match snapshots retained from a previous connection.
                for existing_market in markets.values():
                    existing_market.depth_snapshots.clear()
                async for raw in ws:
                    packet = json.loads(raw)
                    stream = packet.get('stream', '')
                    v = packet.get('data') or {}
                    symbol = str(v.get('s') or stream.split('@')[0]).upper()
                    if symbol not in markets:
                        continue
                    m = markets[symbol]
                    now = now_ms()
                    status['last_message_ms'] = now
                    if '@trade' in stream:
                        trade_id = v.get('t')
                        if trade_id is not None and trade_id == m.last_trade_id:
                            continue
                        price, qty = finite(v.get('p')), finite(v.get('q'))
                        if price is None or qty is None or price <= 0 or qty <= 0:
                            continue
                        m.last_trade_id = trade_id
                        side = 'sell' if v.get('m') else 'buy'
                        m.price, m.last_trade_ms = price, now
                        trade = {'price': price, 'qty': qty, 'side': side,
                                 'ts': v.get('T', now), 'id': trade_id}
                        m.trades.appendleft(trade)
                        m.tape.append((now, side, qty, price))
                        _audit_on_trade(symbol, price, now)
                        while m.tape and m.tape[0][0] < now - 60000:
                            m.tape.popleft()
                        event = {'type': 'trade', 'symbol': symbol, 'trade': trade,
                                 'price': price, 'ts': now}
                    elif '@depth20' in stream:
                        try:
                            bids = sorted([[float(p), float(q)] for p, q in v.get('bids', [])],
                                          key=lambda x: x[0], reverse=True)
                            asks = sorted([[float(p), float(q)] for p, q in v.get('asks', [])],
                                          key=lambda x: x[0])
                            levels = bids + asks
                            if (not bids or not asks or
                                any(not (math.isfinite(p) and math.isfinite(q) and p > 0 and q > 0)
                                    for p, q in levels) or
                                bids[0][0] >= asks[0][0]):
                                raise ValueError('empty, nonfinite or crossed book')
                            m.bids, m.asks = bids, asks
                            m.bid, m.ask = bids[0][0], asks[0][0]
                            m.last_book_ms = now
                            m.last_book_update_id = int(v['lastUpdateId']) if v.get('lastUpdateId') is not None else None
                            m.book_valid, m.book_error = True, None
                            # Capture original decimal strings before serialization
                            # and before any subsequent async work modifies the book.
                            if m.last_book_update_id is not None:
                                m.depth_snapshots.append({
                                    'update_id': m.last_book_update_id,
                                    'captured_ms': now,
                                    'book_time_ms': now,
                                    'valid': True,
                                    'bids': tuple((str(p), str(q)) for p, q in v['bids']),
                                    'asks': tuple((str(p), str(q)) for p, q in v['asks'])})
                            update_wall_tracking(m, now)
                            bn = sum(p * q for p, q in bids)
                            an = sum(p * q for p, q in asks)
                            if bn + an > 0:
                                m.book_history.append((now, (bn - an) / (bn + an)))
                            event = {'type': 'depth', 'symbol': symbol,
                                     'bids': bids, 'asks': asks, 'bid': m.bid,
                                     'ask': m.ask, 'book_valid': True, 'ts': now}
                        except (ValueError, TypeError, OverflowError) as exc:
                            m.book_valid, m.book_error = False, str(exc)
                            m.bids, m.asks, m.bid, m.ask = [], [], None, None
                            m.last_book_update_id = None
                            m.wall_seen = {'bid': {}, 'ask': {}}
                            m.depth_snapshots.clear()
                            event = {'type': 'depth_invalid', 'symbol': symbol,
                                     'book_valid': False, 'reason': 'invalid_book', 'ts': now}
                    elif '@kline_1m' in stream:
                        k = v.get('k') or {}
                        try:
                            candle = {'t': int(k['t']), 'o': float(k['o']),
                                      'h': float(k['h']), 'l': float(k['l']),
                                      'c': float(k['c']), 'v': float(k['v']),
                                      'closed': bool(k.get('x', False))}
                            if (not all(math.isfinite(candle[f]) for f in
                                        ('o', 'h', 'l', 'c', 'v')) or
                                min(candle[f] for f in ('o', 'h', 'l', 'c')) <= 0 or
                                candle['v'] < 0 or candle['h'] < candle['l']):
                                continue
                        except (KeyError, ValueError, TypeError, OverflowError):
                            continue
                        m.live_candle = candle
                        m.last_candle_ms = now
                        if candle['closed']:
                            m.last_closed_candle = candle.copy()
                            # A newly closed candle must invalidate the indicator cache.
                            analysis_cache.pop(symbol, None)
                        event = {'type': 'kline', 'symbol': symbol,
                                 'interval': '1m', 'candle': candle, 'ts': now}
                    elif '@ticker' in stream:
                        m.change = finite(v.get('P'))
                        m.volume = finite(v.get('q'))
                        m.high = finite(v.get('h'))
                        m.low = finite(v.get('l'))
                        m.last_ticker_ms = now
                        event = {'type': 'ticker', 'symbol': symbol,
                                 'change': m.change, 'volume': m.volume,
                                 'high': m.high, 'low': m.low, 'ts': now}
                    else:
                        continue
                    m.sequence += 1
                    await broadcast(symbol, event)
        except asyncio.CancelledError:
            raise
        except Exception as exc:
            status.update(connected=False, error=str(exc))
            for m in markets.values():
                m.book_valid = False
                m.last_book_update_id = None
                m.wall_seen = {'bid': {}, 'ask': {}}
                m.depth_snapshots.clear()
            LOG.warning('Binance stream disconnected: %s', exc)
            await asyncio.sleep(delay)
            delay = min(30, delay * 2)
        finally:
            status['connected'] = False

def merge_live_candles(symbol, interval, candles, limit):
    """Overlay authoritative Binance kline events without mutating REST cache."""
    result = {c['t']: dict(c) for c in candles}
    if interval == '1m':
        m = markets[symbol]
        for c in (m.last_closed_candle, m.live_candle):
            if c is not None and m.last_candle_ms is not None and now_ms() - m.last_candle_ms < 120000:
                result[c['t']] = dict(c)
    # Preserve source-confirmed close flags. An open REST candle captured before
    # the boundary does not magically acquire final OHLC when the clock advances.
    # A fresh REST response or a closed WebSocket kline must confirm the final data.
    step = {'1m':60, '3m':180, '5m':300, '15m':900,
            '30m':1800, '1h':3600, '4h':14400, '1d':86400}[interval] * 1000
    current = now_ms()
    output = sorted(result.values(), key=lambda c: c['t'])[-limit:]
    for c in output:
        if c['t'] + step > current:
            c['closed'] = False
        else:
            c['closed'] = bool(c['closed'])
    return output

async def get_candles(symbol, interval='1m', limit=250):
    key = (symbol, interval, limit)
    ttl = {'1m': 10, '3m': 20, '5m': 30}.get(interval, 30)
    entry = candle_cache.get(key)
    if entry and time.monotonic() - entry[0] < ttl:
        return merge_live_candles(symbol, interval, entry[1], limit)
    lock = candle_locks.setdefault(key, asyncio.Lock())
    async with lock:
        entry = candle_cache.get(key)
        if entry and time.monotonic() - entry[0] < ttl:
            return merge_live_candles(symbol, interval, entry[1], limit)
        try:
            r = await http_client.get(f'{REST}/klines',
                                      params={'symbol': symbol, 'interval': interval, 'limit': limit})
            r.raise_for_status()
            current = now_ms()
            candles = [{'t': int(x[0]), 'o': float(x[1]), 'h': float(x[2]),
                        'l': float(x[3]), 'c': float(x[4]), 'v': float(x[5]),
                        'closed': int(x[6]) < current} for x in r.json()]
            candle_cache[key] = (time.monotonic(), candles)
            return merge_live_candles(symbol, interval, candles, limit)
        except (httpx.HTTPError, ValueError, KeyError, IndexError) as exc:
            if entry and time.monotonic() - entry[0] < 30:
                return merge_live_candles(symbol, interval, entry[1], limit)
            LOG.warning('Candles %s %s: %s', symbol, interval, exc)
            raise HTTPException(503, 'Binance candle service unavailable') from exc

def feature_frame(candles, all_indicators=False):
    closed = [x for x in candles if x['closed']]
    if len(closed) < 60:
        return None
    df = pd.DataFrame(closed).rename(columns={
        'o': 'open', 'h': 'high', 'l': 'low', 'c': 'close', 'v': 'volume'})
    df = df.sort_values('t').drop_duplicates('t')
    if len(df) < 60:
        return None
    df.index = pd.to_datetime(df['t'], unit='ms', utc=True)
    if all_indicators:
        df.ta.strategy('all', lookahead=False, verbose=False, timed=False)
    else:
        df['EMA_20'] = ta.ema(df['close'], length=20)
        df['EMA_50'] = ta.ema(df['close'], length=50)
        df['RSI_14'] = ta.rsi(df['close'], length=14)
        df['ATR_14'] = ta.atr(df['high'], df['low'], df['close'], length=14)
        macd = ta.macd(df['close'])
        bb = ta.bbands(df['close'], length=20)
        if macd is not None:
            df = df.join(macd)
        if bb is not None:
            df = df.join(bb)
    return df

def indicators(candles):
    df = feature_frame(candles)
    if df is None:
        return None
    row = df.iloc[-1]
    volume = df['volume'].sum()
    vwap = finite(((((df['high'] + df['low'] + df['close']) / 3) *
                    df['volume']).sum() / volume)) if volume > 0 else None
    result = {'rsi14': finite(row.get('RSI_14')),
              'ema20': finite(row.get('EMA_20')),
              'ema50': finite(row.get('EMA_50')),
              'atr14': finite(row.get('ATR_14')),
              'vwap_window': vwap,
              'last_closed_price': finite(row['close']),
              'last_closed_t': int(row['t'])}
    for key in df.columns:
        if key.startswith(('MACD_', 'MACDh_', 'MACDs_', 'BBL_', 'BBM_', 'BBU_', 'BBB_', 'BBP_')):
            result[key.lower()] = finite(row[key])
    return result

def orderflow(m):
    now = now_ms()
    trade_age = now - m.last_trade_ms if m.last_trade_ms else None
    book_age = now - m.last_book_ms if m.last_book_ms else None
    fresh = bool(status['connected'] and m.book_valid and
                 trade_age is not None and 0 <= trade_age <= 5000 and
                 book_age is not None and 0 <= book_age <= 5000)
    bn = sum(p * q for p, q in m.bids)
    an = sum(p * q for p, q in m.asks)
    imbalance = (bn - an) / (bn + an) if bn + an > 0 else None
    tape = [x for x in m.tape if now - 60000 <= x[0] <= now]
    buy = sum(x[2] * x[3] for x in tape if x[1] == 'buy')
    sell = sum(x[2] * x[3] for x in tape if x[1] == 'sell')
    spread = ((m.ask - m.bid) / ((m.ask + m.bid) / 2) * 100
              if m.book_valid and m.ask and m.bid and m.ask > m.bid else None)
    return {'fresh': fresh, 'trade_age_ms': trade_age,
            'book_age_ms': book_age, 'book_valid': m.book_valid,
            'book_error': m.book_error, 'bid_notional': bn,
            'ask_notional': an, 'imbalance': imbalance,
            'buy_notional_60s': buy, 'sell_notional_60s': sell,
            'cvd_60s': buy - sell, 'trade_count_60s': len(tape),
            'spread_pct': spread,
            'window': 'rolling_60s_server_received',
            'depth_type': 'partial_20_levels_not_full_L2',
            'cvd_note': 'Observed stream trades only, not historical or market-wide CVD'}

def decision(m, flow, ind, interval='1m'):
    def wait(reason, confidence='veri_yetersiz'):
        return {'decision': 'BEKLE', 'score': 0, 'confidence': confidence,
                'reasons': [reason], 'note': 'Test edilmiş başarı olasılığı değildir'}
    if not flow['fresh']:
        return wait('Canlı işlem veya geçerli emir defteri verisi gecikmiş')
    if not ind or any(ind.get(k) is None for k in
                      ('ema20', 'ema50', 'rsi14', 'last_closed_price')):
        return wait('Kapanmış mum veya gösterge verisi yetersiz')
    if interval == '1m' and (now_ms() - (ind['last_closed_t'] + 60000) > 120000):
        return wait('Son kapanmış 1m mum güncel değil')
    if flow['spread_pct'] is None or flow['spread_pct'] > 0.12:
        return wait('Spread eksik veya yüksek', 'risk_vetosu')
    if flow['trade_count_60s'] < 10:
        return wait('Son 60 saniyedeki işlem örneği yetersiz')
    score, reasons = 0, []
    imbalance = flow['imbalance']
    total = flow['buy_notional_60s'] + flow['sell_notional_60s']
    aggressor = ((flow['buy_notional_60s'] - flow['sell_notional_60s']) / total
                 if total > 0 else 0)
    if imbalance is not None:
        if imbalance > 0.15:
            score += 2
            reasons.append('İlk 20 seviyede alış likiditesi baskın')
        elif imbalance < -0.15:
            score -= 2
            reasons.append('İlk 20 seviyede satış likiditesi baskın')
    if aggressor > 0.12:
        score += 2
        reasons.append('Agresif alış baskın')
    elif aggressor < -0.12:
        score -= 2
        reasons.append('Agresif satış baskın')
    if ind['ema20'] > ind['ema50']:
        score += 1
        reasons.append('EMA20, EMA50 üzerinde')
    else:
        score -= 1
        reasons.append('EMA20, EMA50 altında')
    if ind['rsi14'] > 55:
        score += 1
        reasons.append('RSI yukarı yönlü')
    elif ind['rsi14'] < 45:
        score -= 1
        reasons.append('RSI aşağı yönlü')
    if ind['last_closed_price'] > ind['ema20']:
        score += 1
    elif ind['last_closed_price'] < ind['ema20']:
        score -= 1
    long_ok = score >= 4 and aggressor > 0 and imbalance is not None and imbalance > 0
    short_ok = score <= -4 and aggressor < 0 and imbalance is not None and imbalance < 0
    direction = 'LONG' if long_ok else 'SHORT' if short_ok else 'BEKLE'
    if direction == 'BEKLE':
        reasons.append('Yeterli çoklu doğrulama yok')
    return {'decision': direction, 'score': score,
            'confidence': 'heuristic_not_probability', 'reasons': reasons,
            'note': 'Test edilmiş başarı olasılığı değildir'}

async def mtf_analysis(symbol):
    async def one(tf):
        try:
            ind = await asyncio.to_thread(indicators, await get_candles(symbol, tf, 250))
            if not ind or any(ind.get(k) is None for k in
                              ('last_closed_price', 'ema20', 'ema50', 'rsi14')):
                return {'tf': tf, 'direction': 'YETERSIZ', 'reason': 'Mum yetersiz'}
            step_ms = {'1m': 60000, '3m': 180000, '5m': 300000,
                       '15m': 900000}[tf]
            closed_at = ind['last_closed_t'] + step_ms
            candle_age = now_ms() - closed_at
            if candle_age < -5000 or candle_age > 2 * step_ms:
                return {'tf': tf, 'direction': 'YETERSIZ',
                        'reason': 'Kapanmış mum güncel değil',
                        'closed_t': ind['last_closed_t'],
                        'closed_at_ms': closed_at, 'candle_age_ms': candle_age}
            price, e20, e50, rsi = (ind['last_closed_price'], ind['ema20'],
                                     ind['ema50'], ind['rsi14'])
            direction = ('LONG' if price > e20 > e50 and rsi >= 50 else
                         'SHORT' if price < e20 < e50 and rsi <= 50 else 'KARISIK')
            return {'tf': tf, 'direction': direction, 'rsi14': round(rsi, 2),
                    'ema20': round(e20, 8), 'ema50': round(e50, 8),
                    'closed_t': ind['last_closed_t'],
                    'closed_at_ms': closed_at, 'candle_age_ms': candle_age}
        except Exception as exc:
            LOG.warning('MTF %s %s: %s', symbol, tf, exc)
            return {'tf': tf, 'direction': 'YETERSIZ', 'reason': 'Hesaplama hatası'}
    return await asyncio.gather(*(one(tf) for tf in MTF))

def update_wall_tracking(m, ts):
    """Track *eligible, approximately stable* level liquidity across snapshots.

    Requiring a stable notional within a 25% band avoids inheriting a long
    price-level age when a large wall suddenly arrives. This still cannot
    establish individual order identity from partial-book snapshots.
    """
    for side, levels in (('bid', m.bids), ('ask', m.asks)):
        seen = m.wall_seen[side]
        total = sum(p * q for p, q in levels)
        eligible = {p: p * q for p, q in levels
                    if total > 0 and p * q >= 5000 and p * q / total >= 0.05}
        for price in list(seen):
            if price not in eligible:
                del seen[price]
        for price, notional in eligible.items():
            previous = seen.get(price)
            if previous is None:
                seen[price] = (ts, notional)
            else:
                started_at, reference_notional = previous
                if (reference_notional <= 0 or
                        abs(notional - reference_notional) / reference_notional > 0.25):
                    seen[price] = (ts, notional)

def flow_extras(m, flow):
    now = now_ms()
    # Compare two non-overlapping windows, not arbitrary consecutive snapshots.
    recent = [v for ts, v in m.book_history if 0 <= now - ts < 5000]
    previous = [v for ts, v in m.book_history if 10000 <= now - ts < 20000]
    delta = (sum(recent) / len(recent) - sum(previous) / len(previous)
             if recent and previous else None)

    def walls(levels, side):
        total = sum(p * q for p, q in levels)
        notionals = sorted(p * q for p, q in levels)
        median = ((notionals[len(notionals)//2 - 1] + notionals[len(notionals)//2]) / 2
                  if len(notionals) % 2 == 0 and notionals else
                  notionals[len(notionals)//2] if notionals else 0)
        # Never divide by a dust-sized median; ratios are descriptive only.
        baseline = max(median, 1000.0)
        result = []
        for p, q in levels:
            notional = p * q
            share = 100 * notional / total if total > 0 else 0
            persistence_ms = max(0, now - m.wall_seen[side].get(p, (now, notional))[0])
            if notional < 5000 or share < 5 or persistence_ms < 3000:
                continue
            result.append({'price': p, 'notional': round(notional, 2),
                           'share_pct': round(share, 2),
                           'multiple': round(min(notional / baseline, 999), 1),
                           'observed_ms': persistence_ms,
                           'persistent': True})
        return sorted(result, key=lambda x: x['notional'], reverse=True)[:5]

    return {'imbalance_delta_10s': delta,
            'pressure_recent_window': '0-5s',
            'pressure_reference_window': '10-20s',
            'pressure_recent_samples': len(recent),
            'pressure_reference_samples': len(previous),
            'bid_walls': walls(m.bids, 'bid') if flow['fresh'] else [],
            'ask_walls': walls(m.asks, 'ask') if flow['fresh'] else [],
            'wall_definition': ('partial 20 levels; >=5000 USDT, >=5% of side '
                                'notional, >=3s observed at price; baseline >=1000 USDT'),
            'wall_note': 'Approximately stable price-level liquidity does not prove order identity'}

def enhanced_decision(base, mtf, flow, extras):
    reasons = list(base['reasons'])
    if any(x['direction'] == 'YETERSIZ' for x in mtf):
        return {**base, 'decision': 'BEKLE',
                'reasons': reasons + ['MTF mum verisi eksik'],
                'confidence': 'veri_yetersiz', 'mtf_agreement': False}
    if base['decision'] == 'BEKLE':
        return {**base, 'mtf_agreement': False}
    if not flow['fresh']:
        return {**base, 'decision': 'BEKLE', 'mtf_agreement': False,
                'reasons': reasons + ['Akış güncel değil']}
    direction = base['decision']
    aligned = all(x['direction'] == direction for x in mtf[:3])
    if not aligned:
        reasons.append('1m/3m/5m yönleri aynı değil')
    # Veto strong disagreement between executed trades and resting liquidity.
    traded = flow['buy_notional_60s'] + flow['sell_notional_60s']
    aggressor = (flow['cvd_60s'] / traded) if traded > 0 else 0
    imbalance = flow['imbalance']
    if imbalance is not None and ((direction == 'LONG' and aggressor < -0.10) or
                                  (direction == 'SHORT' and aggressor > 0.10) or
                                  (direction == 'LONG' and imbalance < -0.15) or
                                  (direction == 'SHORT' and imbalance > 0.15)):
        aligned = False
        reasons.append('İşlem akışı veya defter baskısı sinyal yönüne ters')
    delta = extras['imbalance_delta_10s']
    if delta is not None and ((direction == 'LONG' and delta < -0.35) or
                              (direction == 'SHORT' and delta > 0.35)):
        aligned = False
        reasons.append('Emir defteri baskısı ters yöne hızla değişiyor')
    return {**base, 'decision': direction if aligned else 'BEKLE',
            'reasons': reasons, 'mtf_agreement': aligned,
            'confidence': 'heuristic_not_probability'}

async def pro_result(symbol):
    m = markets[symbol]
    # Fetch MTF and closed-candle features first. This section may await REST,
    # so an order-flow sample taken before it could be stale by decision time.
    frames = await mtf_analysis(symbol)
    try:
        ind = await asyncio.to_thread(indicators, await get_candles(symbol, '1m', 250))
    except HTTPException:
        ind = None
    # No awaits between reading order flow and calculating the final signal:
    # trade/book ages are checked against the decision's actual evaluation time.
    flow = orderflow(m)
    extras = flow_extras(m, flow)
    base = decision(m, flow, ind)
    signal = enhanced_decision(base, frames, flow, extras)
    evaluated_at = now_ms()
    closed_at = ind['last_closed_t'] + 60000 if ind else None
    return {'symbol': symbol, 'ts': evaluated_at, 'interval': '1m',
            'flow': flow, 'indicators': ind, 'signal': signal,
            'mtf': frames, 'extras': extras, 'market_price': m.price,
            'decision_evaluated_at_ms': evaluated_at,
            'analysis_candle_closed_at_ms': closed_at,
            'analysis_candle_age_ms': (evaluated_at - closed_at if closed_at else None),
            'ml': {'status': 'not_trained', 'probabilities': None},
            'disclaimer': 'Kural tabanlı gözlem; test edilmiş başarı olasılığı değildir'}

async def cached_pro(symbol):
    lock = analysis_locks.setdefault(symbol, asyncio.Lock())
    async with lock:
        entry = analysis_cache.get(symbol)
        if entry and time.monotonic() - entry[0] < 2:
            result = dict(entry[1])
            # Avoid mutating cached MTF frames when refreshing their age.
            result['mtf'] = [dict(frame) for frame in result['mtf']]
            flow = orderflow(markets[symbol])
            extras = flow_extras(markets[symbol], flow)
            result['flow'] = flow
            result['extras'] = extras
            result['market_price'] = markets[symbol].price
            result['ts'] = now_ms()
            result['decision_evaluated_at_ms'] = result['ts']
            if result.get('analysis_candle_closed_at_ms') is not None:
                result['analysis_candle_age_ms'] = now_ms() - result['analysis_candle_closed_at_ms']
            for frame in result['mtf']:
                if frame.get('closed_at_ms') is not None:
                    frame['candle_age_ms'] = now_ms() - frame['closed_at_ms']
                    step = {'1m': 60000, '3m': 180000, '5m': 300000, '15m': 900000}[frame['tf']]
                    if frame['candle_age_ms'] > 2 * step:
                        frame['direction'] = 'YETERSIZ'
                        frame['reason'] = 'Kapanmış mum güncel değil'
            result['signal'] = enhanced_decision(
                decision(markets[symbol], flow, result['indicators']),
                result['mtf'], flow, extras)
            return result
        result = await pro_result(symbol)
        analysis_cache[symbol] = (time.monotonic(), result)
        return result

# ---------------------------------------------------------------------------
# Read-only, forward-only SIGNAL OBSERVATION (not backtesting or paper trading)
# ---------------------------------------------------------------------------
# Render's default filesystem is ephemeral. The bounded in-memory observations
# also disappear if the process restarts or scales to another worker. For a
# durable study, an opt-in external database and timestamped raw feed are needed.
AUDIT_SYMBOLS = tuple(dict.fromkeys(
    symbol.strip().upper()
    for symbol in os.getenv('TIMI_SIGNAL_AUDIT_SYMBOLS', 'BTCUSDT').split(',')
    if symbol.strip().upper() in SYMBOLS))
AUDIT_HORIZONS_MS = {'1m': 60000, '5m': 300000}
AUDIT_EVAL_MAX_DELAY_MS = 10000
AUDIT_LOG_MAX = 1200
signal_audit_log = {symbol: deque(maxlen=AUDIT_LOG_MAX) for symbol in SYMBOLS}
signal_audit_pending = {symbol: [] for symbol in SYMBOLS}
signal_audit_errors = {symbol: None for symbol in SYMBOLS}


def _audit_observe(symbol, pro, sampled_ms=None):
    """Take one timestamped observation; never create an order or position.

    Outcomes use last observed spot-trade price at the first trade near each
    horizon. This is a directional price study; NOT tradable P&L, fills, or a
    tested profitability claim. No fees, spread or slippage are deducted.
    """
    ts = now_ms() if sampled_ms is None else sampled_ms
    signal = pro.get('signal') or {}
    flow = pro.get('flow') or {}
    direction = signal.get('decision', 'BEKLE')
    if direction not in ('LONG', 'SHORT', 'BEKLE'):
        direction = 'BEKLE'
    price = finite(pro.get('market_price'))
    if not flow.get('fresh') or price is None or price <= 0:
        direction = 'BEKLE'
    # An observation is valid for forward outcomes only if the market data
    # snapshot remained fresh at the time the observation was recorded.
    m = markets[symbol]
    data_fresh = (m.last_trade_ms is not None and
                  0 <= ts - m.last_trade_ms <= 5000 and
                  m.last_book_ms is not None and m.book_valid and
                  0 <= ts - m.last_book_ms <= 5000 and status['connected'])
    if not data_fresh:
        direction = 'BEKLE'
    observation = {
        'observed_at_ms': ts, 'symbol': symbol,
        'direction': direction, 'score': signal.get('score'),
        'entry_reference_price': price,
        'last_trade_age_ms': (ts - m.last_trade_ms if m.last_trade_ms else None),
        'last_book_age_ms': (ts - m.last_book_ms if m.last_book_ms else None),
        'closed_candle_at_ms': pro.get('analysis_candle_closed_at_ms'),
        'mtf': [{'tf': frame.get('tf'), 'direction': frame.get('direction'),
                 'closed_at_ms': frame.get('closed_at_ms')}
                for frame in pro.get('mtf', [])],
        'outcomes': {name: {'status': 'pending', 'target_ms': ts + ms,
                            'reference_exit_price': None,
                            'signed_price_change_pct': None}
                     for name, ms in AUDIT_HORIZONS_MS.items()} if
                     direction in ('LONG', 'SHORT') else {},
    }
    signal_audit_log[symbol].append(observation)
    if observation['outcomes']:
        signal_audit_pending[symbol].append(observation)
    return observation


def _audit_on_trade(symbol, observed_price, received_ms):
    """Finalize 1m/5m forward prices using first subsequent Binance trade."""
    pending = signal_audit_pending[symbol]
    if not pending:
        return
    for record in pending[:]:
        for outcome in record['outcomes'].values():
            if outcome['status'] != 'pending' or received_ms < outcome['target_ms']:
                continue
            lag = received_ms - outcome['target_ms']
            if lag > AUDIT_EVAL_MAX_DELAY_MS or not math.isfinite(observed_price) or observed_price <= 0:
                outcome['status'] = 'missing_data'
                continue
            signed = 1 if record['direction'] == 'LONG' else -1
            move = signed * (observed_price / record['entry_reference_price'] - 1) * 100
            outcome.update(status='measured', reference_exit_price=observed_price,
                           signed_price_change_pct=round(move, 8),
                           observed_at_ms=received_ms, delay_ms=lag)
        if all(outcome['status'] != 'pending' for outcome in record['outcomes'].values()):
            pending.remove(record)


def _audit_summary(symbol):
    rows = list(signal_audit_log[symbol])
    summary = {'total_observations': len(rows),
               'long': sum(r['direction'] == 'LONG' for r in rows),
               'short': sum(r['direction'] == 'SHORT' for r in rows),
               'wait': sum(r['direction'] == 'BEKLE' for r in rows),
               'horizons': {}}
    for label in AUDIT_HORIZONS_MS:
        values = [r['outcomes'][label]['signed_price_change_pct']
                  for r in rows if label in r['outcomes'] and
                  r['outcomes'][label]['status'] == 'measured']
        missing = sum(label in r['outcomes'] and
                      r['outcomes'][label]['status'] == 'missing_data' for r in rows)
        pending = sum(label in r['outcomes'] and
                      r['outcomes'][label]['status'] == 'pending' for r in rows)
        n = len(values)
        summary['horizons'][label] = {
            'measured': n, 'pending': pending, 'missing': missing,
            'directional_hit_rate_pct': round(100 * sum(x > 0 for x in values) / n, 2) if n else None,
            'mean_signed_price_change_pct': round(statistics.mean(values), 6) if n else None,
            'median_signed_price_change_pct': round(statistics.median(values), 6) if n else None,
            'insufficient_sample': n < 100,
        }
    return summary


async def _signal_audit_monitor():
    """Once per minute, independently sample signals for explicitly enabled symbols.

    BTCUSDT alone is enabled by default to keep Render API load modest. A
    background coroutine runs only while this server process is alive; nothing
    runs in ChatGPT's background. Startup starts the sampler immediately.
    """
    while True:
        try:
            # Sample shortly after each UTC minute boundary so the 1m candle
            # close has had a chance to arrive from Binance.
            current = now_ms()
            next_slot = (current // 60000 + 1) * 60000 + 5000
            await asyncio.sleep(max(0.1, (next_slot - current) / 1000))
            for symbol in AUDIT_SYMBOLS:
                try:
                    pro = await cached_pro(symbol)
                    _audit_observe(symbol, pro)
                    signal_audit_errors[symbol] = None
                except asyncio.CancelledError:
                    raise
                except Exception as exc:
                    signal_audit_errors[symbol] = type(exc).__name__
                    LOG.warning('Signal audit %s: %s', symbol, exc)
        except asyncio.CancelledError:
            raise
        except Exception as exc:
            LOG.warning('Signal audit monitor: %s', exc)
            await asyncio.sleep(10)

@asynccontextmanager
async def lifespan(app):
    global http_client
    http_client = httpx.AsyncClient(timeout=12)
    await bootstrap()
    task = asyncio.create_task(relay())
    audit_task = asyncio.create_task(_signal_audit_monitor()) if AUDIT_SYMBOLS else None
    try:
        yield
    finally:
        task.cancel()
        if audit_task:
            audit_task.cancel()
        await asyncio.gather(*(t for t in (task, audit_task) if t is not None),
                             return_exceptions=True)
        await http_client.aclose()

app = FastAPI(title='TiMi SCALP PRO', version=VERSION, lifespan=lifespan)
origins = [x.strip() for x in os.getenv('TIMI_ORIGINS', '').split(',') if x.strip()]
app.add_middleware(CORSMiddleware, allow_origins=origins,
                   allow_methods=['GET'], allow_headers=['*'])

@app.get('/')
async def index():
    return FileResponse(ROOT / 'index.html')

@app.get('/api/health')
async def health():
    age = now_ms() - status['last_message_ms'] if status['last_message_ms'] else None
    return {'app': 'TiMi SCALP PRO', 'version': VERSION,
            'upstream_connected': status['connected'],
            'last_message_age_ms': age, 'upstream_endpoint': status['endpoint'],
            'upstream_error': status['error'], 'symbols': SYMBOLS,
            'real_orders_enabled': False, 'paper_orders_enabled': False,
            'ml_model_loaded': False, 'book_validation_enabled': True,
            'live_kline_1m_enabled': True, 'depth_audit_enabled': True,
            'depth_audit_matching_enabled': True,
            'signal_audit_forward_enabled': bool(AUDIT_SYMBOLS),
            'signal_audit_symbols': AUDIT_SYMBOLS}

def _book_capture(symbol):
    """Copy latest stream book for diagnostics when update-ID matching fails."""
    m = markets[symbol]
    return {'captured_ms': now_ms(), 'book_time_ms': m.last_book_ms,
            'update_id': m.last_book_update_id, 'valid': m.book_valid,
            'bids': [x[:] for x in m.bids], 'asks': [x[:] for x in m.asks]}


def _archive_match(symbol, update_id, start_ms=None, end_ms=None):
    """Find a *captured* WS top-20 snapshot with the exact REST update ID.

    Stream snapshots are retained for at most 240 events per market (~24 s at
    100 ms, depending on actual traffic). No event ID is fabricated/interpolated.
    """
    if update_id is None:
        return None
    matches = [x for x in markets[symbol].depth_snapshots
               if x['update_id'] == update_id]
    if not matches:
        return None
    if start_ms is None or end_ms is None:
        return matches[-1]
    center = (start_ms + end_ms) / 2
    # Only accept an archived snapshot close to the REST request window.
    # This prevents a stale pre-reconnect frame from producing a false pass.
    nearest = min(matches, key=lambda x: abs(x['captured_ms'] - center))
    if nearest['captured_ms'] < start_ms - 3000 or nearest['captured_ms'] > end_ms + 3000:
        return None
    return nearest


def _book_summary(book):
    bids, asks = book['bids'], book['asks']
    bn = sum(float(p) * float(q) for p, q in bids)
    an = sum(float(p) * float(q) for p, q in asks)
    return {'bid_usdt': round(bn, 2), 'ask_usdt': round(an, 2),
            'bid_share_pct': round(100 * bn / (bn + an), 2) if bn + an else None,
            'best_bid': float(bids[0][0]) if bids else None,
            'best_ask': float(asks[0][0]) if asks else None,
            'bid_levels': len(bids), 'ask_levels': len(asks)}


def _canonical_levels(rows, reverse):
    """Decimal-exact representation: never compare floating-point approximations."""
    return sorted(((Decimal(str(p)), Decimal(str(q))) for p, q in rows),
                  key=lambda item: item[0], reverse=reverse)


def _book_snapshot_comparison(local, remote):
    ls, rs = _book_summary(local), _book_summary(remote)
    same_id = (local['update_id'] is not None and
               local['update_id'] == remote['update_id'])
    def difference(key):
        lv, rv = ls[key], rs[key]
        return round((lv - rv) / rv * 100, 2) if rv else None

    identical = None
    first_differences = []
    if same_id and local['valid']:
        for side, reverse in (('bids', True), ('asks', False)):
            ws_levels = _canonical_levels(local[side], reverse)
            rest_levels = _canonical_levels(remote[side], reverse)
            if ws_levels != rest_levels:
                first_differences.append({
                    'side': side,
                    'websocket_levels': len(ws_levels),
                    'rest_levels': len(rest_levels),
                    'first_mismatch_index': next((i for i in range(min(len(ws_levels), len(rest_levels)))
                        if ws_levels[i] != rest_levels[i]), min(len(ws_levels), len(rest_levels)))})
        identical = not first_differences
    age = (max(0, now_ms() - local['book_time_ms'])
           if local.get('book_time_ms') is not None else None)
    verdict = ('EXACT_MATCH' if same_id and identical is True else
               'SAME_ID_MISMATCH' if same_id and identical is False else
               'NO_SAME_ID_SNAPSHOT')
    return {
        'ws_update_id': local['update_id'], 'rest_update_id': remote['update_id'],
        'same_update_id': same_id,
        'match_found_in_history': same_id,
        'exact_levels_match': identical,
        'ws_book_age_ms': age,
        'ws_book_valid': local['valid'],
        'websocket_20_levels': ls, 'binance_rest_20_levels': rs,
        'bid_notional_difference_pct': difference('bid_usdt'),
        'ask_notional_difference_pct': difference('ask_usdt'),
        'first_differences': first_differences,
        'result': verdict,
    }


@app.get('/api/depth-audit/{symbol}')
async def depth_audit(symbol: str, samples: int = 3):
    """Compare REST /depth to archived WS snapshot with identical lastUpdateId.

    If the ID was never observed on WebSocket, report an inconclusive result.
    A different-update comparison is never labeled a mismatch or match.
    """
    symbol = symbol.upper()
    if symbol not in markets:
        raise HTTPException(400, 'Invalid symbol')
    if not 1 <= samples <= 5:
        raise HTTPException(400, 'samples must be between 1 and 5')
    if depth_audit_semaphore.locked():
        raise HTTPException(429, 'Audit busy; retry later')
    if http_client is None:
        raise HTTPException(503, 'Server not ready')
    output = []
    async with depth_audit_semaphore:
        for idx in range(samples):
            before = _book_capture(symbol)
            start = now_ms()
            try:
                response = await http_client.get(
                    f'{REST}/depth', params={'symbol': symbol, 'limit': 20},
                    timeout=8.0)
                response.raise_for_status()
                payload = response.json()
                bids, asks = payload['bids'], payload['asks']
                if (not bids or not asks or
                    any(finite(p) is None or finite(q) is None or
                        finite(p) <= 0 or finite(q) <= 0 for p, q in bids + asks) or
                    float(bids[0][0]) >= float(asks[0][0])):
                    raise ValueError('Binance REST depth invalid')
                remote = {'update_id': int(payload['lastUpdateId']),
                          'bids': bids, 'asks': asks}
            except (httpx.HTTPError, KeyError, TypeError, ValueError, IndexError) as exc:
                LOG.warning('Depth audit %s: %s', symbol, exc)
                raise HTTPException(503, 'Binance REST depth unavailable') from exc
            end = now_ms()
            # If REST is ahead of the stream, allow ~0.8 s for the same
            # update ID to appear. Never infer missing snapshots.
            matched = _archive_match(symbol, remote['update_id'], start, end)
            for _ in range(8):
                if matched is not None or not status['connected']:
                    break
                if (markets[symbol].last_book_update_id is not None and
                        markets[symbol].last_book_update_id > remote['update_id']):
                    break
                await asyncio.sleep(0.1)
                matched = _archive_match(symbol, remote['update_id'], start, end)
            after = _book_capture(symbol)
            mid = (start + end) / 2
            local = matched if matched is not None else min(
                (before, after), key=lambda x: abs(x['captured_ms'] - mid))
            row = _book_snapshot_comparison(local, remote)
            if matched is None:
                row['same_update_id'] = False
                row['match_found_in_history'] = False
                row['exact_levels_match'] = None
                row['result'] = 'NO_SAME_ID_SNAPSHOT'
                row['first_differences'] = []
            row.update({'sample': idx + 1,
                        'rest_round_trip_ms': end - start,
                        'ws_snapshot_capture_ms': local['captured_ms'],
                        'rest_request_start_ms': start,
                        'rest_response_end_ms': end,
                        'archive_size': len(markets[symbol].depth_snapshots),
                        'archive_capacity': markets[symbol].depth_snapshots.maxlen,
                        'comparison_note': (
                            'Same Binance update ID; decimal-exact level comparison' if matched else
                            'Different or unavailable update IDs; no exact verdict')})
            output.append(row)
            if idx + 1 < samples:
                await asyncio.sleep(0.8)
    return {
        'symbol': symbol, 'tested_at_ms': now_ms(), 'samples': output,
        'summary': {
            'matching_update_ids': sum(x['match_found_in_history'] for x in output),
            'exactly_identical': sum(x['result'] == 'EXACT_MATCH' for x in output),
            'same_id_mismatches': sum(x['result'] == 'SAME_ID_MISMATCH' for x in output),
            'inconclusive_different_ids': sum(x['result'] == 'NO_SAME_ID_SNAPSHOT' for x in output),
            'ws_stale_samples': sum(x['ws_book_age_ms'] is None or x['ws_book_age_ms'] > 2000
                                    or not x['ws_book_valid'] for x in output)},
        'interpretation': ('EXACT_MATCH: same ID, equal top-20 decimal levels. '
                           'SAME_ID_MISMATCH: same ID, different top-20 decimal levels. '
                           'NO_SAME_ID_SNAPSHOT: not synchronized; difference is inconclusive. '
                           'Matching is opportunistic and cannot be guaranteed.'),
        'source': 'Binance Spot REST /api/v3/depth?limit=20 versus TiMi depth20 stream',
        'read_only': True,
    }

@app.get('/api/signal-audit/{symbol}')
async def signal_audit(symbol: str, limit: int = 30):
    """Read-only forward signal observations; NOT historical strategy backtest."""
    symbol = symbol.upper()
    if symbol not in markets:
        raise HTTPException(400, 'Invalid symbol')
    if not 1 <= limit <= 100:
        raise HTTPException(400, 'limit must be 1..100')
    return {'symbol': symbol, 'version': VERSION,
            'enabled': symbol in AUDIT_SYMBOLS,
            'first_observation_ms': (signal_audit_log[symbol][0]['observed_at_ms']
                                     if signal_audit_log[symbol] else None),
            'summary': _audit_summary(symbol),
            'observations': list(reversed(list(signal_audit_log[symbol])))[:limit],
            'last_sampling_error': signal_audit_errors[symbol],
            'method': ('Once-per-minute observations of actual rule-based signals. '
                       'Compare the next observed Binance trade near 1m and 5m horizons. '
                       'Price-direction study only; no orders, fills, fees, spread, '
                       'slippage, or risk-adjusted P&L. Not a historical backtest.'),
            'limitations': ('In-memory observations reset on process restart; '
                            'Render scaling creates independent histories; '
                            'samples under 100 are insufficient for claims.')}

@app.get('/api/markets')
async def market_list():
    return {'markets': [{k: v for k, v in m.public().items()
                         if k not in ('bids', 'asks', 'trades')}
                        for m in markets.values()]}

@app.get('/api/snapshot/{symbol}')
async def snapshot(symbol: str):
    symbol = symbol.upper()
    if symbol not in markets:
        raise HTTPException(404, 'Unsupported symbol')
    return markets[symbol].public()

@app.get('/api/klines/{symbol}')
async def klines(symbol: str, interval: str = '1m', limit: int = 150):
    symbol = symbol.upper()
    if symbol not in markets or interval not in TF:
        raise HTTPException(400, 'Invalid symbol or interval')
    return {'symbol': symbol, 'interval': interval, 'source': 'Binance Spot REST + 1m WebSocket overlay',
            'candles': await get_candles(symbol, interval, max(20, min(limit, 500)))}

@app.get('/api/analysis/{symbol}')
async def analysis(symbol: str, interval: str = '1m'):
    symbol = symbol.upper()
    if symbol not in markets or interval not in TF:
        raise HTTPException(400, 'Invalid symbol or interval')
    m = markets[symbol]
    flow = orderflow(m)
    try:
        ind = await asyncio.to_thread(indicators, await get_candles(symbol, interval, 250))
    except HTTPException:
        ind = None
    return {'symbol': symbol, 'interval': interval, 'ts': now_ms(),
            'flow': flow, 'indicators': ind, 'signal': decision(m, flow, ind, interval),
            'market_price': m.price}

@app.get('/api/analysis-pro/{symbol}')
async def analysis_pro(symbol: str):
    symbol = symbol.upper()
    if symbol not in markets:
        raise HTTPException(400, 'Invalid symbol')
    return await cached_pro(symbol)

@app.get('/api/indicators-all/{symbol}')
async def indicators_all(symbol: str, interval: str = '5m'):
    symbol = symbol.upper()
    if symbol not in markets or interval not in TF:
        raise HTTPException(400, 'Invalid symbol or interval')
    candles = await get_candles(symbol, interval, 500)
    closed_t = max((x['t'] for x in candles if x['closed']), default=None)
    key = (symbol, interval, closed_t)
    entry = full_cache.get(key)
    if entry and time.monotonic() - entry[0] < 60:
        return entry[1]
    if full_semaphore.locked():
        raise HTTPException(429, 'Full indicator worker busy; retry later')
    try:
        async with full_semaphore:
            df = await asyncio.wait_for(
                asyncio.to_thread(feature_frame, candles, True), timeout=45)
    except Exception as exc:
        LOG.warning('Full indicators %s %s: %s', symbol, interval, exc)
        raise HTTPException(503, 'Full indicator calculation unavailable') from exc
    if df is None:
        raise HTTPException(422, 'Not enough closed candles')
    row = df.iloc[-1]
    values = {str(k):finite(v) for k,v in row.items()
              if k not in ('t','closed') and finite(v) is not None}
    result = {'symbol': symbol, 'interval': interval,
              'last_closed_t': int(row['t']), 'feature_count': len(values),
              'features': values,
              'note': 'Indicator features, not independent predictions or a trained model'}
    full_cache.clear()
    full_cache[key] = (time.monotonic(), result)
    return result

@app.websocket('/ws/{symbol}')
async def live(ws: WebSocket, symbol: str):
    symbol = symbol.upper()
    if symbol not in markets:
        await ws.close(code=1008)
        return
    await ws.accept()
    item = {'symbol': symbol, 'queue': asyncio.Queue(maxsize=150)}
    clients.append(item)
    try:
        await ws.send_json({'type': 'snapshot', 'data': markets[symbol].public()})
        while True:
            try:
                await ws.send_text(await asyncio.wait_for(item['queue'].get(), timeout=10))
            except asyncio.TimeoutError:
                await ws.send_json({'type': 'heartbeat', 'ts': now_ms(),
                                    'upstream_connected': status['connected']})
    except (WebSocketDisconnect, RuntimeError):
        pass
    finally:
        if item in clients:
            clients.remove(item)

# Read-only analytics. No paper orders, real orders, credentials or training endpoints.
