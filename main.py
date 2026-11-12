"""TiMi SCALP PRO v1.1 - public Binance Spot and isolated paper trading.
No real order routes or private exchange credentials.
"""
import asyncio
import json
import logging
import math
import os
import secrets
import time
from collections import deque
from contextlib import asynccontextmanager
from pathlib import Path

import httpx
import websockets
from fastapi import FastAPI, HTTPException, WebSocket, WebSocketDisconnect, Header
from fastapi.responses import FileResponse
from pydantic import BaseModel, Field

logging.basicConfig(level=logging.INFO)
LOG = logging.getLogger('timi')
ROOT = Path(__file__).resolve().parent
SYMBOLS = ('BTCUSDT','ETHUSDT','SOLUSDT','BNBUSDT','XRPUSDT','DOGEUSDT','ADAUSDT','AVAXUSDT')
STREAM = 'wss://data-stream.binance.vision/stream?streams='
REST = 'https://data-api.binance.vision/api/v3'
FEE, SLIPPAGE = 0.001, 0.0002
MAX_AGE_MS = 5000


def now_ms():
    return int(time.time() * 1000)


class Market:
    def __init__(self, symbol):
        self.symbol = symbol
        self.price = self.change = self.volume = self.high = self.low = None
        self.bid = self.ask = None
        self.bids, self.asks = [], []
        self.trades = deque(maxlen=70)
        self.buy_qty = self.sell_qty = 0.0
        self.last_trade_ms = self.last_book_ms = self.last_ticker_ms = None
        self.sequence = 0

    def public(self):
        b = sum(q for _, q in self.bids)
        a = sum(q for _, q in self.asks)
        return dict(symbol=self.symbol, price=self.price, change=self.change,
                    volume=self.volume, high=self.high, low=self.low,
                    bid=self.bid, ask=self.ask, bids=self.bids, asks=self.asks,
                    trades=list(self.trades), buy_qty=round(self.buy_qty, 8),
                    sell_qty=round(self.sell_qty, 8),
                    cvd=round(self.buy_qty-self.sell_qty, 8),
                    book_buy_pct=round(100*b/(a+b), 2) if a+b else None,
                    last_trade_ms=self.last_trade_ms, last_book_ms=self.last_book_ms,
                    last_ticker_ms=self.last_ticker_ms, sequence=self.sequence,
                    source='Binance Spot')


markets = {s: Market(s) for s in SYMBOLS}
clients = []
status = {'connected': False, 'last_message_ms': None, 'error': None}
accounts = {}
account_lock = asyncio.Lock()


def get_account(token):
    if not token or token not in accounts:
        raise HTTPException(401, 'Sanal oturum bulunamadı; yeni oturum açın')
    return accounts[token]


def account_public(acc):
    positions = acc['positions'].copy()
    valuations = {s: (markets[s].price * q if markets[s].price is not None else None)
                  for s, q in positions.items()}
    missing = [s for s, q in positions.items() if q > 0 and markets[s].price is None]
    equity = None if missing else acc['cash'] + sum(v or 0 for v in valuations.values())
    return {'cash': round(acc['cash'], 8), 'positions': positions,
            'valuations': valuations, 'equity': round(equity, 8) if equity is not None else None,
            'missing_prices': missing, 'history': list(acc['history']),
            'fee_rate': FEE, 'slippage_rate': SLIPPAGE,
            'mode': 'simulation_only', 'persistent': False}


async def bootstrap():
    async with httpx.AsyncClient(timeout=12) as client:
        for s in SYMBOLS:
            try:
                r = await client.get(f'{REST}/ticker/24hr', params={'symbol': s})
                r.raise_for_status()
                v = r.json()
                m = markets[s]
                m.price, m.change = float(v['lastPrice']), float(v['priceChangePercent'])
                m.volume, m.high, m.low = float(v['quoteVolume']), float(v['highPrice']), float(v['lowPrice'])
                m.last_ticker_ms = now_ms()
            except Exception as e:
                LOG.warning('Bootstrap %s: %s', s, e)


async def broadcast(s, event):
    raw = json.dumps(event, separators=(',', ':'))
    for c in tuple(clients):
        if c['symbol'] != s:
            continue
        q = c['queue']
        if q.full():
            try:
                q.get_nowait()
            except asyncio.QueueEmpty:
                pass
        try:
            q.put_nowait(raw)
        except asyncio.QueueFull:
            pass


async def relay():
    names = [f'{s.lower()}@{channel}' for s in SYMBOLS
             for channel in ('trade', 'depth20@100ms', 'ticker')]
    url = STREAM + '/'.join(names)
    delay = 1
    while True:
        try:
            async with websockets.connect(url, ping_interval=20, ping_timeout=20,
                                          max_size=4_000_000, open_timeout=15) as ws:
                status.update(connected=True, error=None)
                delay = 1
                LOG.info('Binance Vision stream connected')
                async for raw in ws:
                    msg = json.loads(raw)
                    stream, v = msg.get('stream', ''), msg.get('data', {})
                    s = v.get('s', '').upper()
                    if s not in markets:
                        s = stream.split('@')[0].upper()
                    if s not in markets:
                        continue
                    m, ts = markets[s], now_ms()
                    status['last_message_ms'] = ts
                    if '@trade' in stream:
                        price, qty = float(v['p']), float(v['q'])
                        side = 'sell' if v['m'] else 'buy'
                        trade = {'price': price, 'qty': qty, 'side': side,
                                 'ts': v.get('T', ts), 'id': v.get('t')}
                        m.price, m.last_trade_ms = price, ts
                        m.trades.appendleft(trade)
                        if side == 'buy':
                            m.buy_qty += qty
                        else:
                            m.sell_qty += qty
                        event = {'type': 'trade', 'symbol': s, 'trade': trade,
                                 'price': price, 'ts': ts}
                    elif '@depth20' in stream:
                        m.bids = [[float(p), float(q)] for p, q in v.get('bids', [])]
                        m.asks = [[float(p), float(q)] for p, q in v.get('asks', [])]
                        m.bid = m.bids[0][0] if m.bids else None
                        m.ask = m.asks[0][0] if m.asks else None
                        m.last_book_ms = ts
                        event = {'type': 'depth', 'symbol': s, 'bids': m.bids,
                                 'asks': m.asks, 'bid': m.bid, 'ask': m.ask, 'ts': ts}
                    elif '@ticker' in stream:
                        m.change, m.volume = float(v['P']), float(v['q'])
                        m.high, m.low = float(v['h']), float(v['l'])
                        m.last_ticker_ms = ts
                        event = {'type': 'ticker', 'symbol': s, 'change': m.change,
                                 'volume': m.volume, 'high': m.high, 'low': m.low, 'ts': ts}
                    else:
                        continue
                    m.sequence += 1
                    await broadcast(s, event)
        except asyncio.CancelledError:
            raise
        except Exception as e:
            status.update(connected=False, error=str(e))
            LOG.warning('Upstream disconnected: %s; retry %ss', e, delay)
            await asyncio.sleep(delay)
            delay = min(30, delay * 2)
        finally:
            status['connected'] = False


@asynccontextmanager
async def lifespan(app):
    await bootstrap()
    task = asyncio.create_task(relay())
    try:
        yield
    finally:
        task.cancel()
        await asyncio.gather(task, return_exceptions=True)


app = FastAPI(title='TiMi SCALP PRO', version='1.1.0', lifespan=lifespan)


@app.get('/')
async def index():
    return FileResponse(ROOT / 'index.html')


@app.get('/api/health')
async def health():
    age = now_ms() - status['last_message_ms'] if status['last_message_ms'] else None
    return {'app': 'TiMi SCALP PRO', 'version': '1.1.0',
            'upstream_connected': status['connected'], 'last_message_age_ms': age,
            'upstream_error': status['error'], 'symbols': SYMBOLS,
            'real_orders_enabled': False}


@app.get('/api/markets')
async def market_list():
    return {'markets': [{k: v for k, v in m.public().items()
                         if k not in ('bids', 'asks', 'trades')}
                        for m in markets.values()]}


@app.get('/api/snapshot/{symbol}')
async def snapshot(symbol: str):
    if symbol.upper() not in markets:
        raise HTTPException(404, 'Unsupported symbol')
    return markets[symbol.upper()].public()


@app.get('/api/klines/{symbol}')
async def klines(symbol: str, interval: str = '1m', limit: int = 150):
    symbol = symbol.upper()
    if symbol not in markets or interval not in ('1m','3m','5m','15m','30m','1h','4h','1d'):
        raise HTTPException(400, 'Invalid symbol or interval')
    try:
        async with httpx.AsyncClient(timeout=12) as client:
            r = await client.get(f'{REST}/klines', params={
                'symbol': symbol, 'interval': interval, 'limit': max(20, min(limit, 300))})
            r.raise_for_status()
            ts = now_ms()
            return {'symbol': symbol, 'interval': interval, 'source': 'Binance Spot REST',
                    'candles': [{'t': x[0], 'o': float(x[1]), 'h': float(x[2]),
                                 'l': float(x[3]), 'c': float(x[4]), 'v': float(x[5]),
                                 'closed': x[6] < ts} for x in r.json()]}
    except (httpx.HTTPError, ValueError) as e:
        LOG.warning('Klines failed: %s', e)
        raise HTTPException(503, 'Candle service unavailable')


@app.websocket('/ws/{symbol}')
async def live(ws: WebSocket, symbol: str):
    symbol = symbol.upper()
    if symbol not in markets:
        await ws.close(code=1008)
        return
    await ws.accept()
    item = {'symbol': symbol, 'queue': asyncio.Queue(maxsize=120)}
    clients.append(item)
    try:
        await ws.send_json({'type': 'snapshot', 'data': markets[symbol].public()})
        while True:
            try:
                await ws.send_text(await asyncio.wait_for(item['queue'].get(), timeout=10))
            except asyncio.TimeoutError:
                await ws.send_json({'type': 'heartbeat', 'ts': now_ms(),
                                    'upstream_connected': status['connected']})
    except (WebSocketDisconnect, RuntimeError, OSError):
        pass
    finally:
        if item in clients:
            clients.remove(item)


class PaperOrder(BaseModel):
    symbol: str
    side: str
    quantity: float = Field(gt=0, le=100000, allow_inf_nan=False)


@app.post('/api/paper/session')
async def create_session():
    token = secrets.token_urlsafe(32)
    async with account_lock:
        accounts[token] = {'cash': 10000.0,
                           'positions': {s: 0.0 for s in SYMBOLS},
                           'history': deque(maxlen=200)}
    return {'token': token, 'mode': 'simulation_only'}


@app.get('/api/paper')
async def paper(x_timi_paper_token: str | None = Header(default=None)):
    return account_public(get_account(x_timi_paper_token))


@app.post('/api/paper/order')
async def paper_order(order: PaperOrder, x_timi_paper_token: str | None = Header(default=None)):
    token = x_timi_paper_token
    symbol, side = order.symbol.upper(), order.side.lower()
    if symbol not in markets or side not in ('buy', 'sell'):
        raise HTTPException(400, 'Invalid symbol or side')
    if not math.isfinite(order.quantity):
        raise HTTPException(400, 'Invalid quantity')
    async with account_lock:
        acc = get_account(token)
        m, ts = markets[symbol], now_ms()
        if (not status['connected'] or m.price is None or m.last_trade_ms is None
                or ts - m.last_trade_ms > MAX_AGE_MS):
            raise HTTPException(409, 'No fresh live trade price; paper order blocked')
        px = m.price * (1 + SLIPPAGE if side == 'buy' else 1 - SLIPPAGE)
        notional, fee = px * order.quantity, px * order.quantity * FEE
        if not all(map(math.isfinite, (px, notional, fee))):
            raise HTTPException(400, 'Invalid calculated value')
        if side == 'buy':
            if notional + fee > acc['cash']:
                raise HTTPException(409, 'Insufficient paper cash')
            acc['cash'] -= notional + fee
            acc['positions'][symbol] += order.quantity
        else:
            if order.quantity > acc['positions'][symbol] + 1e-10:
                raise HTTPException(409, 'Insufficient paper position; spot short disabled')
            acc['cash'] += notional - fee
            acc['positions'][symbol] = max(0.0, acc['positions'][symbol] - order.quantity)
        record = {'symbol': symbol, 'side': side, 'quantity': order.quantity,
                  'price': round(px, 8), 'fee': round(fee, 8), 'ts': ts, 'simulated': True}
        acc['history'].appendleft(record)
        return {'order': record, **account_public(acc)}


@app.post('/api/paper/reset')
async def reset_paper(x_timi_paper_token: str | None = Header(default=None)):
    async with account_lock:
        acc = get_account(x_timi_paper_token)
        acc['cash'] = 10000.0
        acc['positions'] = {s: 0.0 for s in SYMBOLS}
        acc['history'].clear()
    return {'ok': True, 'mode': 'simulation_only'}

# Security boundary: No real order endpoints, exchange secrets, or order signing.
# Demo session tokens are not production authentication. Single-process in-memory only.
