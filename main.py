"""TiMi SCALP PRO v1.2 - public spot analytics, simulation-only legacy endpoints."""
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
import websockets
from fastapi import FastAPI, HTTPException, WebSocket, WebSocketDisconnect
from fastapi.middleware.cors import CORSMiddleware
from fastapi.responses import FileResponse
from pydantic import BaseModel, Field

LOG = logging.getLogger('timi')
logging.basicConfig(level=logging.INFO)
ROOT = Path(__file__).resolve().parent
VERSION = '1.2.0'
SYMBOLS = ('BTCUSDT','ETHUSDT','SOLUSDT','BNBUSDT','XRPUSDT','DOGEUSDT','ADAUSDT','AVAXUSDT')
STREAM = 'wss://data-stream.binance.vision/stream?streams='
REST = 'https://data-api.binance.vision/api/v3'
FEE, SLIPPAGE = 0.001, 0.0002
TF = ('1m','3m','5m','15m','30m','1h','4h','1d')
now_ms = lambda: int(time.time()*1000)

class Market:
    def __init__(self, symbol):
        self.symbol=symbol; self.price=None; self.change=None; self.volume=None
        self.high=None; self.low=None; self.bid=None; self.ask=None
        self.bids=[]; self.asks=[]; self.trades=deque(maxlen=100)
        self.tape=deque(maxlen=5000); self.buy_qty=0.; self.sell_qty=0.
        self.last_trade_ms=None; self.last_book_ms=None; self.last_ticker_ms=None
        self.sequence=0; self.last_trade_id=None; self.source='Binance Spot'
    def public(self):
        return dict(symbol=self.symbol,price=self.price,change=self.change,volume=self.volume,
                    high=self.high,low=self.low,bid=self.bid,ask=self.ask,bids=self.bids,asks=self.asks,
                    trades=list(self.trades),buy_qty=round(self.buy_qty,6),sell_qty=round(self.sell_qty,6),
                    last_trade_ms=self.last_trade_ms,last_book_ms=self.last_book_ms,
                    last_ticker_ms=self.last_ticker_ms,sequence=self.sequence,source=self.source)

markets={s:Market(s) for s in SYMBOLS}
clients=[]
status={'connected':False,'last_message_ms':None,'error':None,'endpoint':'data-stream.binance.vision'}
state_lock=asyncio.Lock()
paper_cash=10000.
paper_positions={s:0. for s in SYMBOLS}
paper_history=deque(maxlen=200)

async def bootstrap():
    async with httpx.AsyncClient(timeout=12) as client:
        async def one(s):
            try:
                r=await client.get(f'{REST}/ticker/24hr',params={'symbol':s}); r.raise_for_status()
                v=r.json(); m=markets[s]
                m.price=float(v['lastPrice']);m.change=float(v['priceChangePercent'])
                m.volume=float(v['quoteVolume']);m.high=float(v['highPrice']);m.low=float(v['lowPrice'])
                m.last_ticker_ms=now_ms()
            except Exception as exc: LOG.warning('Bootstrap %s: %s',s,exc)
        await asyncio.gather(*(one(s) for s in SYMBOLS))

async def broadcast(symbol,event):
    payload=json.dumps(event,separators=(',',':'))
    for client in tuple(clients):
        if client['symbol']!=symbol:continue
        q=client['queue']
        if q.full():
            try:q.get_nowait()
            except asyncio.QueueEmpty:pass
        try:q.put_nowait(payload)
        except asyncio.QueueFull:pass

async def relay():
    names=[]
    for s in SYMBOLS:
        k=s.lower();names.extend((f'{k}@trade',f'{k}@depth20@100ms',f'{k}@ticker'))
    url=STREAM+'/'.join(names);delay=1
    while True:
        try:
            async with websockets.connect(url,ping_interval=20,ping_timeout=20,open_timeout=15,max_size=4_000_000) as ws:
                status.update(connected=True,error=None);delay=1
                LOG.info('Binance public stream connected')
                async for raw in ws:
                    packet=json.loads(raw); stream=packet.get('stream','');v=packet.get('data') or {}
                    symbol=v.get('s','').upper() or stream.split('@')[0].upper()
                    if symbol not in markets:continue
                    m=markets[symbol]; now=now_ms();status['last_message_ms']=now
                    if '@trade' in stream:
                        trade_id=v.get('t')
                        if trade_id is not None and trade_id==m.last_trade_id:continue
                        m.last_trade_id=trade_id
                        price=float(v['p']);qty=float(v['q']);side='sell' if v['m'] else 'buy'
                        m.price=price;m.last_trade_ms=now
                        trade={'price':price,'qty':qty,'side':side,'ts':v.get('T',now),'id':trade_id}
                        m.trades.appendleft(trade);m.tape.append((now,side,qty,price))
                        if side=='buy':m.buy_qty+=qty
                        else:m.sell_qty+=qty
                        event={'type':'trade','symbol':symbol,'trade':trade,'price':price,'ts':now}
                    elif '@depth20' in stream:
                        m.bids=[[float(p),float(q)] for p,q in v.get('bids',[])]
                        m.asks=[[float(p),float(q)] for p,q in v.get('asks',[])]
                        m.bid=m.bids[0][0] if m.bids else None;m.ask=m.asks[0][0] if m.asks else None
                        m.last_book_ms=now
                        event={'type':'depth','symbol':symbol,'bids':m.bids,'asks':m.asks,'bid':m.bid,'ask':m.ask,'ts':now}
                    elif '@ticker' in stream:
                        m.change=float(v['P']);m.volume=float(v['q']);m.high=float(v['h']);m.low=float(v['l'])
                        m.last_ticker_ms=now
                        event={'type':'ticker','symbol':symbol,'change':m.change,'volume':m.volume,'high':m.high,'low':m.low,'ts':now}
                    else:continue
                    m.sequence+=1
                    await broadcast(symbol,event)
        except asyncio.CancelledError:raise
        except Exception as exc:
            status.update(connected=False,error=str(exc));LOG.warning('Upstream disconnected: %s; retry %ss',exc,delay)
            await asyncio.sleep(delay);delay=min(30,delay*2)
        finally:status['connected']=False

async def get_candles(symbol,interval='1m',limit=150):
    async with httpx.AsyncClient(timeout=12) as client:
        try:
            r=await client.get(f'{REST}/klines',params={'symbol':symbol,'interval':interval,'limit':limit})
            r.raise_for_status();current=now_ms()
            return [{'t':x[0],'o':float(x[1]),'h':float(x[2]),'l':float(x[3]),
                     'c':float(x[4]),'v':float(x[5]),'closed':int(x[6])<current} for x in r.json()]
        except (httpx.HTTPError,ValueError,KeyError,IndexError) as exc:
            LOG.warning('Candles failed: %s',exc);raise HTTPException(503,'Binance candle service unavailable') from exc

def indicators(candles):
    closed=[c for c in candles if c['closed']]
    closes=[c['c'] for c in closed]
    if len(closes)<50:return None
    def ema(n):
        value=sum(closes[:n])/n;k=2/(n+1)
        for p in closes[n:]:value=p*k+value*(1-k)
        return value
    gains=losses=0.
    for i in range(1,15):
        delta=closes[i]-closes[i-1];gains+=max(delta,0);losses+=max(-delta,0)
    gains/=14;losses/=14
    for i in range(15,len(closes)):
        delta=closes[i]-closes[i-1]
        gains=(gains*13+max(delta,0))/14;losses=(losses*13+max(-delta,0))/14
    rsi=100 if losses==0 and gains>0 else 50 if losses==0 else 100-100/(1+gains/losses)
    tr=[]
    for i in range(1,len(closed)):
        c=closed[i];previous=closed[i-1]['c']
        tr.append(max(c['h']-c['l'],abs(c['h']-previous),abs(c['l']-previous)))
    atr=sum(tr[:14])/14
    for t in tr[14:]:atr=(atr*13+t)/14
    volume=sum(c['v'] for c in closed)
    vwap=sum(((c['h']+c['l']+c['c'])/3)*c['v'] for c in closed)/volume if volume else None
    return {'rsi14':rsi,'ema20':ema(20),'ema50':ema(50),'atr14':atr,'vwap_window':vwap,
            'last_closed_price':closes[-1],'last_closed_t':closed[-1]['t']}

def orderflow(m):
    now=now_ms();trade_age=now-m.last_trade_ms if m.last_trade_ms else None
    book_age=now-m.last_book_ms if m.last_book_ms else None
    fresh=bool(status['connected'] and trade_age is not None and trade_age<=5000 and book_age is not None and book_age<=5000)
    bid_notional=sum(p*q for p,q in m.bids);ask_notional=sum(p*q for p,q in m.asks)
    imbalance=(bid_notional-ask_notional)/(bid_notional+ask_notional) if bid_notional+ask_notional else None
    window=[t for t in m.tape if now-t[0]<=60000]
    buy=sum(t[2]*t[3] for t in window if t[1]=='buy')
    sell=sum(t[2]*t[3] for t in window if t[1]=='sell')
    cvd=buy-sell
    spread_pct=(m.ask-m.bid)/m.ask*100 if m.ask and m.bid and m.ask>=m.bid else None
    return {'fresh':fresh,'trade_age_ms':trade_age,'book_age_ms':book_age,'bid_notional':bid_notional,
            'ask_notional':ask_notional,'imbalance':imbalance,'buy_notional_60s':buy,
            'sell_notional_60s':sell,'cvd_60s':cvd,'trade_count_60s':len(window),
            'spread_pct':spread_pct,'window':'rolling_60s_server_received',
            'depth_type':'partial_20_levels_not_full_L2'}

def decision(m,flow,ind):
    reasons=[];score=0
    if not flow['fresh']:return {'decision':'BEKLE','score':0,'confidence':'veri_yetersiz','reasons':['Canlı işlem veya derinlik verisi gecikmiş']}
    if ind is None:return {'decision':'BEKLE','score':0,'confidence':'veri_yetersiz','reasons':['Kapanmış mum verisi yetersiz']}
    if flow['spread_pct'] is None or flow['spread_pct']>0.12:
        return {'decision':'BEKLE','score':0,'confidence':'risk_vetosu','reasons':['Spread eksik veya yüksek']}
    if flow['trade_count_60s']<10:
        return {'decision':'BEKLE','score':0,'confidence':'veri_yetersiz','reasons':['Son 60 saniyede işlem örneği yetersiz']}
    imbalance=flow['imbalance']
    if imbalance is not None:
        if imbalance>0.15:score+=2;reasons.append('Emir defterinde alış likiditesi baskın')
        elif imbalance<-.15:score-=2;reasons.append('Emir defterinde satış likiditesi baskın')
    total=flow['buy_notional_60s']+flow['sell_notional_60s']
    aggressor=(flow['buy_notional_60s']-flow['sell_notional_60s'])/total if total else 0
    if aggressor>.12:score+=2;reasons.append('Son 60 saniyede agresif alış baskın')
    elif aggressor<-.12:score-=2;reasons.append('Son 60 saniyede agresif satış baskın')
    if ind['ema20']>ind['ema50']:score+=1;reasons.append('EMA20, EMA50 üzerinde')
    else:score-=1;reasons.append('EMA20, EMA50 altında')
    if ind['rsi14']>55:score+=1;reasons.append('RSI yukarı yönlü')
    elif ind['rsi14']<45:score-=1;reasons.append('RSI aşağı yönlü')
    if ind['last_closed_price']>ind['ema20']:score+=1
    elif ind['last_closed_price']<ind['ema20']:score-=1
    # Order-flow and trend must agree; score alone is insufficient.
    aligned= (score>=4 and aggressor>0 and (imbalance or 0)>0) or (score<=-4 and aggressor<0 and (imbalance or 0)<0)
    decision='LONG' if aligned and score>0 else 'SHORT' if aligned else 'BEKLE'
    if not aligned:reasons.append('Yeterli çoklu doğrulama yok')
    return {'decision':decision,'score':score,'confidence':'heuristic_not_probability',
            'reasons':reasons,'note':'Analitik değerlendirme; başarı olasılığı veya yatırım tavsiyesi değildir'}

@asynccontextmanager
async def lifespan(app):
    await bootstrap();task=asyncio.create_task(relay())
    try:yield
    finally:task.cancel();await asyncio.gather(task,return_exceptions=True)

app=FastAPI(title='TiMi SCALP PRO',version=VERSION,lifespan=lifespan)
origins=[s.strip() for s in os.getenv('TIMI_ORIGINS','').split(',') if s.strip()]
app.add_middleware(CORSMiddleware,allow_origins=origins,allow_methods=['GET','POST'],allow_headers=['*'])

@app.get('/')
async def index():return FileResponse(ROOT/'index.html')

@app.get('/api/health')
async def health():
    age=now_ms()-status['last_message_ms'] if status['last_message_ms'] else None
    return {'app':'TiMi SCALP PRO','version':VERSION,'upstream_connected':status['connected'],
            'last_message_age_ms':age,'upstream_endpoint':status['endpoint'],
            'upstream_error':status['error'],'symbols':SYMBOLS,'real_orders_enabled':False}

@app.get('/api/markets')
async def market_list():return {'markets':[{k:v for k,v in m.public().items() if k not in ('bids','asks','trades')} for m in markets.values()]}

@app.get('/api/snapshot/{symbol}')
async def snapshot(symbol:str):
    symbol=symbol.upper()
    if symbol not in markets:raise HTTPException(404,'Unsupported symbol')
    return markets[symbol].public()

@app.get('/api/klines/{symbol}')
async def klines(symbol:str,interval:str='1m',limit:int=150):
    symbol=symbol.upper()
    if symbol not in markets or interval not in TF:raise HTTPException(400,'Invalid symbol or interval')
    return {'symbol':symbol,'interval':interval,'source':'Binance Spot REST',
            'candles':await get_candles(symbol,interval,max(20,min(limit,300)))}

@app.get('/api/analysis/{symbol}')
async def analysis(symbol:str,interval:str='1m'):
    symbol=symbol.upper()
    if symbol not in markets or interval not in TF:raise HTTPException(400,'Invalid symbol or interval')
    m=markets[symbol];flow=orderflow(m)
    try:ind=indicators(await get_candles(symbol,interval,150))
    except HTTPException:ind=None
    return {'symbol':symbol,'interval':interval,'ts':now_ms(),'flow':flow,'indicators':ind,
            'signal':decision(m,flow,ind),'market_price':m.price}

@app.websocket('/ws/{symbol}')
async def live(ws:WebSocket,symbol:str):
    symbol=symbol.upper()
    if symbol not in markets:await ws.close(code=1008);return
    await ws.accept();item={'symbol':symbol,'queue':asyncio.Queue(maxsize=150)};clients.append(item)
    try:
        await ws.send_json({'type':'snapshot','data':markets[symbol].public()})
        while True:
            try:await ws.send_text(await asyncio.wait_for(item['queue'].get(),timeout=10))
            except asyncio.TimeoutError:
                await ws.send_json({'type':'heartbeat','ts':now_ms(),'upstream_connected':status['connected']})
    except (WebSocketDisconnect,RuntimeError):pass
    finally:
        if item in clients:clients.remove(item)

# Legacy simulation-only API kept for backward compatibility; not expanded in v1.2.
class PaperOrder(BaseModel):
    symbol:str
    side:str
    quantity:float=Field(gt=0,le=100000,allow_inf_nan=False)

@app.get('/api/paper')
async def paper():
    return {'cash':round(paper_cash,2),'positions':paper_positions.copy(),
            'history':list(paper_history),'fee_rate':FEE,'slippage_rate':SLIPPAGE,'mode':'simulation_only'}

@app.post('/api/paper/order')
async def paper_order(order:PaperOrder):
    global paper_cash
    symbol=order.symbol.upper();side=order.side.lower()
    if symbol not in markets or side not in ('buy','sell'):raise HTTPException(400,'Invalid symbol or side')
    async with state_lock:
        m=markets[symbol];now=now_ms()
        if not status['connected'] or m.price is None or not m.last_trade_ms or now-m.last_trade_ms>5000:
            raise HTTPException(409,'No fresh trade price; paper execution blocked')
        px=m.price*(1+SLIPPAGE if side=='buy' else 1-SLIPPAGE)
        notional=px*order.quantity;fee=notional*FEE
        if side=='buy':
            if notional+fee>paper_cash:raise HTTPException(409,'Insufficient paper cash')
            paper_cash-=notional+fee;paper_positions[symbol]+=order.quantity
        else:
            if order.quantity>paper_positions[symbol]+1e-10:raise HTTPException(409,'Insufficient paper position; spot short disabled')
            paper_cash+=notional-fee;paper_positions[symbol]=max(0.,paper_positions[symbol]-order.quantity)
        record={'symbol':symbol,'side':side,'quantity':order.quantity,'price':round(px,8),
                'fee':round(fee,6),'ts':now,'simulated':True}
        paper_history.appendleft(record)
        return {'order':record,'cash':round(paper_cash,2),'position':paper_positions[symbol]}

@app.post('/api/paper/reset')
async def reset_paper():
    global paper_cash
    async with state_lock:
        paper_cash=10000.
        for s in SYMBOLS:paper_positions[s]=0.
        paper_history.clear()
    return {'ok':True,'mode':'simulation_only'}

# SECURITY: No exchange credentials, signing, or real order execution endpoints.
