"""TiMi SCALP PRO v1.7 — read-only Binance Spot analytics.
Developer: Suat DOĞU. No order endpoints or model training on request path.
"""
import asyncio
import json
import logging
import math
import os
import time
from collections import deque
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
VERSION = '1.7.0'
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
        self.last_trade_ms = self.last_book_ms = self.last_ticker_ms = None
        self.last_trade_id = None
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
                            m.book_valid, m.book_error = True, None
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
    # REST close flags are time-dependent: never retain them from cache creation.
    step = {'1m':60, '3m':180, '5m':300, '15m':900,
            '30m':1800, '1h':3600, '4h':14400, '1d':86400}[interval] * 1000
    current = now_ms()
    output = sorted(result.values(), key=lambda c: c['t'])[-limit:]
    for c in output:
        c['closed'] = bool(c['closed'] or c['t'] + step <= current)
        # If a REST candle is still in progress, it is not closed.
        if c['t'] + step > current and not (interval == '1m' and
                markets[symbol].last_closed_candle is not None and
                c['t'] == markets[symbol].last_closed_candle['t']):
            c['closed'] = False
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
    if interval == '1m' and (now_ms() - ind['last_closed_t'] > 180000):
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
            price, e20, e50, rsi = (ind['last_closed_price'], ind['ema20'],
                                     ind['ema50'], ind['rsi14'])
            direction = ('LONG' if price > e20 > e50 and rsi >= 50 else
                         'SHORT' if price < e20 < e50 and rsi <= 50 else 'KARISIK')
            return {'tf': tf, 'direction': direction, 'rsi14': round(rsi, 2),
                    'ema20': round(e20, 8), 'ema50': round(e50, 8),
                    'closed_t': ind['last_closed_t']}
        except Exception as exc:
            LOG.warning('MTF %s %s: %s', symbol, tf, exc)
            return {'tf': tf, 'direction': 'YETERSIZ', 'reason': 'Hesaplama hatası'}
    return await asyncio.gather(*(one(tf) for tf in MTF))

def flow_extras(m, flow):
    now = now_ms()
    recent = [v for ts, v in m.book_history if 0 <= now - ts <= 5000]
    previous = [v for ts, v in m.book_history if 10000 <= now - ts <= 20000]
    delta = (sum(recent) / len(recent) - sum(previous) / len(previous)
             if recent and previous else None)
    def walls(levels):
        sizes = sorted(p * q for p, q in levels)
        med = sizes[len(sizes) // 2] if sizes else 0
        return [{'price': p, 'notional': round(p * q, 2),
                 'multiple': round(p * q / med, 1)}
                for p, q in levels if med > 0 and p * q >= max(3 * med, 1000)][:5]
    return {'imbalance_delta_10s': delta,
            'bid_walls': walls(m.bids), 'ask_walls': walls(m.asks),
            'wall_definition': '>=3x median level notional and >=1000 USDT; partial 20 levels'}

def enhanced_decision(base, mtf, flow, extras):
    reasons = list(base['reasons'])
    if any(x['direction'] == 'YETERSIZ' for x in mtf):
        return {**base, 'decision': 'BEKLE',
                'reasons': reasons + ['MTF mum verisi eksik'],
                'confidence': 'veri_yetersiz', 'mtf_agreement': False}
    if base['decision'] == 'BEKLE':
        return {**base, 'mtf_agreement': False}
    direction = base['decision']
    aligned = all(x['direction'] == direction for x in mtf[:3])
    if not aligned:
        reasons.append('1m/3m/5m yönleri aynı değil')
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
    flow = orderflow(m)
    extras = flow_extras(m, flow)
    frames = await mtf_analysis(symbol)
    try:
        ind = await asyncio.to_thread(indicators, await get_candles(symbol, '1m', 250))
    except HTTPException:
        ind = None
    base = decision(m, flow, ind)
    signal = enhanced_decision(base, frames, flow, extras)
    return {'symbol': symbol, 'ts': now_ms(), 'interval': '1m',
            'flow': flow, 'indicators': ind, 'signal': signal,
            'mtf': frames, 'extras': extras, 'market_price': m.price,
            'ml': {'status': 'not_trained', 'probabilities': None},
            'disclaimer': 'Kural tabanlı gözlem; test edilmiş başarı olasılığı değildir'}

async def cached_pro(symbol):
    lock = analysis_locks.setdefault(symbol, asyncio.Lock())
    async with lock:
        entry = analysis_cache.get(symbol)
        if entry and time.monotonic() - entry[0] < 2:
            result = dict(entry[1])
            flow = orderflow(markets[symbol])
            extras = flow_extras(markets[symbol], flow)
            result['flow'] = flow
            result['extras'] = extras
            result['market_price'] = markets[symbol].price
            result['ts'] = now_ms()
            result['signal'] = enhanced_decision(
                decision(markets[symbol], flow, result['indicators']),
                result['mtf'], flow, extras)
            return result
        result = await pro_result(symbol)
        analysis_cache[symbol] = (time.monotonic(), result)
        return result

@asynccontextmanager
async def lifespan(app):
    global http_client
    http_client = httpx.AsyncClient(timeout=12)
    await bootstrap()
    task = asyncio.create_task(relay())
    try:
        yield
    finally:
        task.cancel()
        await asyncio.gather(task, return_exceptions=True)
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
            'live_kline_1m_enabled': True}

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
