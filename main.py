"""TiMi SCALP PRO v1.0 — read-only Binance spot relay and paper-trading simulator.
No exchange credentials, private endpoints, or real-order execution.
"""
import asyncio
import json
import logging
import os
import time
from collections import deque
from contextlib import asynccontextmanager
from pathlib import Path

import httpx
from fastapi import FastAPI, HTTPException, WebSocket, WebSocketDisconnect
from fastapi.middleware.cors import CORSMiddleware
from fastapi.responses import FileResponse
from pydantic import BaseModel, Field

try:
    import websockets
except ImportError as exc:
    raise RuntimeError('Install requirements.txt first') from exc

logging.basicConfig(level=logging.INFO)
LOG = logging.getLogger('timi')
ROOT = Path(__file__).resolve().parent
SYMBOLS = ('BTCUSDT', 'ETHUSDT', 'SOLUSDT', 'BNBUSDT', 'XRPUSDT', 'DOGEUSDT', 'ADAUSDT', 'AVAXUSDT')
STREAM = 'wss://stream.binance.com:9443/stream?streams='
REST = 'https://data-api.binance.vision/api/v3'
FEE = 0.001  # Illustrative 0.10% per side, configurable in a later version
SLIPPAGE = 0.0002  # Illustrative 0.02% per side

class Market:
    def __init__(self, symbol):
        self.symbol = symbol
        self.price = None
        self.change = None
        self.volume = None
        self.high = None
        self.low = None
        self.bid = None
        self.ask = None
        self.bids = []
        self.asks = []
        self.trades = deque(maxlen=70)
        self.buy_qty = 0.0
        self.sell_qty = 0.0
        self.last_trade_ms = None
        self.last_book_ms = None
        self.last_ticker_ms = None
        self.source = 'Binance Spot'
        self.sequence = 0

    def public(self):
        return {
            'symbol': self.symbol, 'price': self.price, 'change': self.change,
            'volume': self.volume, 'high': self.high, 'low': self.low,
            'bid': self.bid, 'ask': self.ask, 'bids': self.bids,
            'asks': self.asks, 'trades': list(self.trades),
            'buy_qty': round(self.buy_qty, 5), 'sell_qty': round(self.sell_qty, 5),
            'last_trade_ms': self.last_trade_ms, 'last_book_ms': self.last_book_ms,
            'last_ticker_ms': self.last_ticker_ms, 'source': self.source,
            'sequence': self.sequence,
        }

markets = {s: Market(s) for s in SYMBOLS}
clients = []
connection_status = {'connected': False, 'last_message_ms': None, 'error': None}
state_lock = asyncio.Lock()
paper_cash = 10000.0
paper_positions = {s: 0.0 for s in SYMBOLS}
paper_history = deque(maxlen=200)

async def bootstrap():
    async with httpx.AsyncClient(timeout=12) as client:
        for symbol in SYMBOLS:
            try:
                response = await client.get(f'{REST}/ticker/24hr', params={'symbol': symbol})
                response.raise_for_status()
                v = response.json()
                m = markets[symbol]
                m.price = float(v['lastPrice'])
                m.change = float(v['priceChangePercent'])
                m.volume = float(v['quoteVolume'])
                m.high = float(v['highPrice'])
                m.low = float(v['lowPrice'])
                m.last_ticker_ms = int(time.time() * 1000)
            except Exception as exc:
                LOG.warning('Bootstrap %s failed: %s', symbol, exc)

async def relay():
    names = []
    for s in SYMBOLS:
        key = s.lower()
        names.extend((f'{key}@trade', f'{key}@depth20@100ms', f'{key}@ticker'))
    url = STREAM + '/'.join(names)
    delay = 1
    while True:
        try:
            async with websockets.connect(url, ping_interval=20, ping_timeout=20, max_size=4_000_000, open_timeout=15) as ws:
                connection_status.update(connected=True, error=None)
                LOG.info('Binance websocket connected')
                delay = 1
                async for raw in ws:
                    msg = json.loads(raw)
                    stream = msg.get('stream', '')
                    v = msg.get('data', {})
                    symbol = v.get('s', '').upper()
                    if symbol not in markets:
                        continue
                    m = markets[symbol]
                    now = int(time.time() * 1000)
                    connection_status['last_message_ms'] = now
                    if '@trade' in stream:
                        price, qty = float(v['p']), float(v['q'])
                        side = 'sell' if v['m'] else 'buy'  # buyer is maker => seller aggressor
                        m.price = price
                        m.last_trade_ms = now
                        m.trades.appendleft({'price': price, 'qty': qty, 'side': side, 'ts': v.get('T', now), 'id': v.get('t')})
                        if side == 'buy':
                            m.buy_qty += qty
                        else:
                            m.sell_qty += qty
                        # Rolling session counters reset periodically below.
                        event = {'type': 'trade', 'symbol': symbol, 'trade': m.trades[0], 'price': price, 'ts': now}
                    elif '@depth20' in stream:
                        m.bids = [[float(p), float(q)] for p, q in v.get('bids', [])]
                        m.asks = [[float(p), float(q)] for p, q in v.get('asks', [])]
                        m.bid = m.bids[0][0] if m.bids else None
                        m.ask = m.asks[0][0] if m.asks else None
                        m.last_book_ms = now
                        event = {'type': 'depth', 'symbol': symbol, 'bids': m.bids, 'asks': m.asks, 'bid': m.bid, 'ask': m.ask, 'ts': now}
                    elif '@ticker' in stream:
                        m.change = float(v['P'])
                        m.volume = float(v['q'])
                        m.high = float(v['h'])
                        m.low = float(v['l'])
                        m.last_ticker_ms = now
                        event = {'type': 'ticker', 'symbol': symbol, 'price': m.price, 'change': m.change, 'volume': m.volume, 'high': m.high, 'low': m.low, 'ts': now}
                    else:
                        continue
                    m.sequence += 1
                    if clients:
                        dead = []
                        data = json.dumps(event, separators=(',', ':'))
                        for client in tuple(clients):
                            if client['symbol'] != symbol:
                                continue
                            try:
                                client['queue'].put_nowait(data)
                            except asyncio.QueueFull:
                                # Drop stale UI updates; latest snapshot will resynchronize.
                                try:
                                    client['queue'].get_nowait()
                                    client['queue'].put_nowait(data)
                                except (asyncio.QueueEmpty, asyncio.QueueFull):
                                    dead.append(client)
                        for item in dead:
                            clients.remove(item) if item in clients else None
        except asyncio.CancelledError:
            raise
        except Exception as exc:
            connection_status.update(connected=False, error=str(exc))
            LOG.warning('Binance stream disconnected: %s; retry in %ss', exc, delay)
            await asyncio.sleep(delay)
            delay = min(30, delay * 2)
        finally:
            connection_status['connected'] = False

@asynccontextmanager
async def lifespan(app):
    await bootstrap()
    task = asyncio.create_task(relay())
    try:
        yield
    finally:
        task.cancel()
        await asyncio.gather(task, return_exceptions=True)

app = FastAPI(title='TiMi SCALP PRO', version='1.0.0', lifespan=lifespan)
# For production: set TIMI_ORIGINS to comma-separated frontend origins.
origins = [x.strip() for x in os.getenv('TIMI_ORIGINS', '').split(',') if x.strip()]
app.add_middleware(CORSMiddleware, allow_origins=origins, allow_methods=['GET', 'POST'], allow_headers=['*'])

@app.get('/')
async def index():
    return FileResponse(ROOT / 'index.html')

@app.get('/api/health')
async def health():
    age = None
    if connection_status['last_message_ms']:
        age = int(time.time() * 1000) - connection_status['last_message_ms']
    return {'app': 'TiMi SCALP PRO', 'version': '1.0.0', 'upstream_connected': connection_status['connected'], 'last_message_age_ms': age, 'symbols': SYMBOLS, 'real_orders_enabled': False}

@app.get('/api/markets')
async def market_list():
    return {'markets': [{k: v for k, v in m.public().items() if k not in ('bids', 'asks', 'trades')} for m in markets.values()]}

@app.get('/api/snapshot/{symbol}')
async def snapshot(symbol: str):
    symbol = symbol.upper()
    if symbol not in markets:
        raise HTTPException(404, 'Unsupported symbol')
    return markets[symbol].public()

@app.get('/api/klines/{symbol}')
async def klines(symbol: str, interval: str = '1m', limit: int = 150):
    symbol = symbol.upper()
    if symbol not in markets or interval not in ('1m', '3m', '5m', '15m', '30m', '1h', '4h', '1d'):
        raise HTTPException(400, 'Invalid symbol or interval')
    async with httpx.AsyncClient(timeout=12) as client:
        try:
            r = await client.get(f'{REST}/klines', params={'symbol': symbol, 'interval': interval, 'limit': max(20, min(limit, 300))})
            r.raise_for_status()
            return {'symbol': symbol, 'interval': interval, 'source': 'Binance Spot REST', 'candles': [{'t': x[0], 'o': float(x[1]), 'h': float(x[2]), 'l': float(x[3]), 'c': float(x[4]), 'v': float(x[5]), 'closed': x[6] < int(time.time() * 1000)} for x in r.json()]}
        except httpx.HTTPError:
            raise HTTPException(503, 'Binance candle service unavailable')

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
                data = await asyncio.wait_for(item['queue'].get(), timeout=10)
                await ws.send_text(data)
            except asyncio.TimeoutError:
                await ws.send_json({'type': 'heartbeat', 'ts': int(time.time()*1000), 'upstream_connected': connection_status['connected']})
    except (WebSocketDisconnect, RuntimeError):
        pass
    finally:
        clients.remove(item) if item in clients else None

class PaperOrder(BaseModel):
    symbol: str
    side: str
    quantity: float = Field(gt=0, le=100000)

@app.get('/api/paper')
async def paper():
    return {'cash': round(paper_cash, 2), 'positions': paper_positions, 'history': list(paper_history), 'fee_rate': FEE, 'slippage_rate': SLIPPAGE, 'mode': 'simulation_only'}

@app.post('/api/paper/order')
async def paper_order(order: PaperOrder):
    global paper_cash
    symbol = order.symbol.upper()
    if symbol not in markets or order.side not in ('buy', 'sell'):
        raise HTTPException(400, 'Invalid symbol or side')
    m = markets[symbol]
    now = int(time.time() * 1000)
    if not connection_status['connected'] or m.price is None or not m.last_trade_ms or now - m.last_trade_ms > 5000:
        raise HTTPException(409, 'No fresh trade price; paper execution blocked')
    async with state_lock:
        px = m.price * (1 + SLIPPAGE if order.side == 'buy' else 1 - SLIPPAGE)
        notional = px * order.quantity
        fee = notional * FEE
        if order.side == 'buy':
            if notional + fee > paper_cash:
                raise HTTPException(409, 'Insufficient paper cash')
            paper_cash -= notional + fee
            paper_positions[symbol] += order.quantity
        else:
            if order.quantity > paper_positions[symbol] + 1e-10:
                raise HTTPException(409, 'Insufficient paper position; spot short disabled')
            paper_cash += notional - fee
            paper_positions[symbol] = max(0, paper_positions[symbol] - order.quantity)
        record = {'symbol': symbol, 'side': order.side, 'quantity': order.quantity, 'price': round(px, 8), 'fee': round(fee, 6), 'ts': now, 'simulated': True}
        paper_history.appendleft(record)
        return {'order': record, 'cash': round(paper_cash, 2), 'position': paper_positions[symbol]}

@app.post('/api/paper/reset')
async def reset_paper():
    global paper_cash
    async with state_lock:
        paper_cash = 10000.0
        for s in SYMBOLS:
            paper_positions[s] = 0.0
        paper_history.clear()
    return {'ok': True, 'mode': 'simulation_only'}

# Security boundary: No private exchange API keys, order-signing code or real-order routes.
