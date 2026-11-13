"""TiMi SCALP PRO v1.4 — public Binance Spot analytics, no order endpoints.
Developer: Suat DOĞU. Models are never trained on the live request path.
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

LOG = logging.getLogger('timi')
logging.basicConfig(level=logging.INFO)
ROOT = Path(__file__).resolve().parent
VERSION = '1.4.0'
SYMBOLS = ('BTCUSDT','ETHUSDT','SOLUSDT','BNBUSDT','XRPUSDT','DOGEUSDT','ADAUSDT','AVAXUSDT')
TF = ('1m','3m','5m','15m','30m','1h','4h','1d')
MTF = ('1m','3m','5m','15m')
STREAM = 'wss://data-stream.binance.vision/stream?streams='
REST = 'https://data-api.binance.vision/api/v3'
now_ms = lambda: int(time.time()*1000)

class Market:
    def __init__(self, symbol):
        self.symbol=symbol;self.price=None;self.change=None;self.volume=None
        self.high=None;self.low=None;self.bid=None;self.ask=None
        self.bids=[];self.asks=[];self.trades=deque(maxlen=100)
        self.tape=deque();self.last_trade_ms=None;self.last_book_ms=None
        self.last_ticker_ms=None;self.book_history=deque(maxlen=500)
        self.sequence=0;self.last_trade_id=None;self.source='Binance Spot'
    def public(self):
        return dict(symbol=self.symbol,price=self.price,change=self.change,volume=self.volume,
                    high=self.high,low=self.low,bid=self.bid,ask=self.ask,bids=self.bids,asks=self.asks,
                    trades=list(self.trades),buy_qty=sum(x[2] for x in self.tape if x[1]=='buy'),
                    sell_qty=sum(x[2] for x in self.tape if x[1]=='sell'),
                    last_trade_ms=self.last_trade_ms,last_book_ms=self.last_book_ms,
                    last_ticker_ms=self.last_ticker_ms,sequence=self.sequence,source=self.source)

markets={s:Market(s) for s in SYMBOLS}
clients=[]
status={'connected':False,'last_message_ms':None,'error':None,'endpoint':'data-stream.binance.vision'}
cache={};cache_locks={};http_client=None

def finite(v):
    try:
        x=float(v)
        return x if math.isfinite(x) else None
    except (ValueError,TypeError,OverflowError):return None

async def bootstrap():
    async def one(s):
        try:
            r=await http_client.get(f'{REST}/ticker/24hr',params={'symbol':s});r.raise_for_status()
            v=r.json();m=markets[s]
            m.price=finite(v['lastPrice']);m.change=finite(v['priceChangePercent'])
            m.volume=finite(v['quoteVolume']);m.high=finite(v['highPrice']);m.low=finite(v['lowPrice'])
            m.last_ticker_ms=now_ms()
        except Exception as exc:LOG.warning('Bootstrap %s: %s',s,exc)
    await asyncio.gather(*(one(s) for s in SYMBOLS))

async def broadcast(symbol,event):
    payload=json.dumps(event,separators=(',',':'))
    for c in tuple(clients):
        if c['symbol']!=symbol:continue
        q=c['queue']
        if q.full():
            try:q.get_nowait()
            except asyncio.QueueEmpty:pass
        try:q.put_nowait(payload)
        except asyncio.QueueFull:pass

async def relay():
    names=[]
    for s in SYMBOLS:
        k=s.lower();names.extend((f'{k}@trade',f'{k}@depth20@100ms',f'{k}@ticker'))
    delay=1
    while True:
        try:
            async with websockets.connect(STREAM+'/'.join(names),ping_interval=20,ping_timeout=20,
                                          open_timeout=15,max_size=4_000_000) as ws:
                status.update(connected=True,error=None);delay=1
                async for raw in ws:
                    packet=json.loads(raw);stream=packet.get('stream','');v=packet.get('data') or {}
                    symbol=v.get('s','').upper() or stream.split('@')[0].upper()
                    if symbol not in markets:continue
                    m=markets[symbol];now=now_ms();status['last_message_ms']=now
                    if '@trade' in stream:
                        trade_id=v.get('t')
                        if trade_id is not None and trade_id==m.last_trade_id:continue
                        m.last_trade_id=trade_id
                        price=finite(v.get('p'));qty=finite(v.get('q'))
                        if price is None or qty is None:continue
                        side='sell' if v.get('m') else 'buy'
                        m.price=price;m.last_trade_ms=now
                        trade={'price':price,'qty':qty,'side':side,'ts':v.get('T',now),'id':trade_id}
                        m.trades.appendleft(trade);m.tape.append((now,side,qty,price))
                        cutoff=now-60000
                        while m.tape and m.tape[0][0]<cutoff:m.tape.popleft()
                        event={'type':'trade','symbol':symbol,'trade':trade,'price':price,'ts':now}
                    elif '@depth20' in stream:
                        m.bids=[[float(p),float(q)] for p,q in v.get('bids',[])]
                        m.asks=[[float(p),float(q)] for p,q in v.get('asks',[])]
                        m.bid=m.bids[0][0] if m.bids else None
                        m.ask=m.asks[0][0] if m.asks else None
                        m.last_book_ms=now
                        bn=sum(p*q for p,q in m.bids);an=sum(p*q for p,q in m.asks)
                        if bn+an:m.book_history.append((now,(bn-an)/(bn+an)))
                        event={'type':'depth','symbol':symbol,'bids':m.bids,'asks':m.asks,
                               'bid':m.bid,'ask':m.ask,'ts':now}
                    elif '@ticker' in stream:
                        m.change=finite(v.get('P'));m.volume=finite(v.get('q'))
                        m.high=finite(v.get('h'));m.low=finite(v.get('l'))
                        m.last_ticker_ms=now
                        event={'type':'ticker','symbol':symbol,'change':m.change,'volume':m.volume,
                               'high':m.high,'low':m.low,'ts':now}
                    else:continue
                    m.sequence+=1
                    await broadcast(symbol,event)
        except asyncio.CancelledError:raise
        except Exception as exc:
            status.update(connected=False,error=str(exc))
            LOG.warning('Upstream disconnected: %s; retry %ss',exc,delay)
            await asyncio.sleep(delay);delay=min(30,delay*2)
        finally:status['connected']=False

async def get_candles(symbol,interval='1m',limit=250):
    # Cache per symbol/timeframe; closed-candle changes invalidate within 2 seconds.
    key=(symbol,interval,limit);now=time.monotonic();entry=cache.get(key)
    if entry and now-entry[0]<2:return entry[1]
    lock=cache_locks.setdefault(key,asyncio.Lock())
    async with lock:
        now=time.monotonic();entry=cache.get(key)
        if entry and now-entry[0]<2:return entry[1]
        try:
            r=await http_client.get(f'{REST}/klines',params={'symbol':symbol,'interval':interval,'limit':limit})
            r.raise_for_status();current=now_ms()
            candles=[{'t':int(x[0]),'o':float(x[1]),'h':float(x[2]),'l':float(x[3]),
                      'c':float(x[4]),'v':float(x[5]),'closed':int(x[6])<current} for x in r.json()]
            cache[key]=(time.monotonic(),candles)
            return candles
        except (httpx.HTTPError,ValueError,KeyError,IndexError) as exc:
            if entry and now-entry[0]<30:return entry[1]
            LOG.warning('Candles %s %s failed: %s',symbol,interval,exc)
            raise HTTPException(503,'Binance candle service unavailable') from exc

def feature_frame(candles,all_indicators=False):
    closed=[x for x in candles if x['closed']]
    if len(closed)<60:return None
    df=pd.DataFrame(closed).rename(columns={'o':'open','h':'high','l':'low','c':'close','v':'volume'})
    df=df.sort_values('t').drop_duplicates('t')
    if len(df)<60:return None
    df.index=pd.to_datetime(df['t'],unit='ms',utc=True)
    if all_indicators:
        # Run in an isolated worker: expensive and never used in the hot signal path.
        df.ta.strategy('all',lookahead=False,verbose=False,timed=False)
    else:
        df['EMA_20']=ta.ema(df['close'],length=20)
        df['EMA_50']=ta.ema(df['close'],length=50)
        df['RSI_14']=ta.rsi(df['close'],length=14)
        df['ATR_14']=ta.atr(df['high'],df['low'],df['close'],length=14)
        macd=ta.macd(df['close']);bb=ta.bbands(df['close'],length=20)
        if macd is not None:df=df.join(macd)
        if bb is not None:df=df.join(bb)
    return df

def indicators(candles):
    df=feature_frame(candles)
    if df is None:return None
    row=df.iloc[-1]
    # VWAP is explicitly a windowed volume-weighted typical price, not session VWAP.
    volume=df['volume'].sum()
    vwap=float((((df['high']+df['low']+df['close'])/3)*df['volume']).sum()/volume) if volume else None
    out={'rsi14':finite(row.get('RSI_14')),'ema20':finite(row.get('EMA_20')),
         'ema50':finite(row.get('EMA_50')),'atr14':finite(row.get('ATR_14')),
         'vwap_window':vwap,'last_closed_price':finite(row['close']),
         'last_closed_t':int(row['t'])}
    for key in df.columns:
        if key.startswith(('MACD_','MACDh_','MACDs_','BBL_','BBM_','BBU_','BBB_','BBP_')):
            out[key.lower()]=finite(row[key])
    return out

def orderflow(m):
    now=now_ms();trade_age=now-m.last_trade_ms if m.last_trade_ms else None
    book_age=now-m.last_book_ms if m.last_book_ms else None
    fresh=bool(status['connected'] and trade_age is not None and 0<=trade_age<=5000 and
               book_age is not None and 0<=book_age<=5000)
    bid_notional=sum(p*q for p,q in m.bids);ask_notional=sum(p*q for p,q in m.asks)
    imbalance=(bid_notional-ask_notional)/(bid_notional+ask_notional) if bid_notional+ask_notional else None
    window=[x for x in m.tape if now-x[0]<=60000]
    buy=sum(x[2]*x[3] for x in window if x[1]=='buy')
    sell=sum(x[2]*x[3] for x in window if x[1]=='sell')
    spread_pct=(m.ask-m.bid)/m.ask*100 if m.ask and m.bid and m.ask>=m.bid else None
    return {'fresh':fresh,'trade_age_ms':trade_age,'book_age_ms':book_age,
            'bid_notional':bid_notional,'ask_notional':ask_notional,'imbalance':imbalance,
            'buy_notional_60s':buy,'sell_notional_60s':sell,'cvd_60s':buy-sell,
            'trade_count_60s':len(window),'spread_pct':spread_pct,
            'window':'rolling_60s_server_received','depth_type':'partial_20_levels_not_full_L2',
            'cvd_note':'Observed stream trades only; not historical or exchange-wide CVD'}

def decision(m,flow,ind):
    reasons=[];score=0
    if not flow['fresh']:
        return {'decision':'BEKLE','score':0,'confidence':'veri_yetersiz','reasons':['Canlı işlem veya derinlik verisi gecikmiş']}
    if not ind or any(ind.get(k) is None for k in ('ema20','ema50','rsi14','last_closed_price')):
        return {'decision':'BEKLE','score':0,'confidence':'veri_yetersiz','reasons':['Kapanmış mum veya gösterge verisi yetersiz']}
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
    if aggressor>.12:score+=2;reasons.append('Agresif alış baskın')
    elif aggressor<-.12:score-=2;reasons.append('Agresif satış baskın')
    if ind['ema20']>ind['ema50']:score+=1;reasons.append('EMA20, EMA50 üzerinde')
    else:score-=1;reasons.append('EMA20, EMA50 altında')
    if ind['rsi14']>55:score+=1;reasons.append('RSI yukarı yönlü')
    elif ind['rsi14']<45:score-=1;reasons.append('RSI aşağı yönlü')
    if ind['last_closed_price']>ind['ema20']:score+=1
    elif ind['last_closed_price']<ind['ema20']:score-=1
    aligned=(score>=4 and aggressor>0 and (imbalance or 0)>0) or (score<=-4 and aggressor<0 and (imbalance or 0)<0)
    direction='LONG' if aligned and score>0 else 'SHORT' if aligned else 'BEKLE'
    if not aligned:reasons.append('Yeterli çoklu doğrulama yok')
    return {'decision':direction,'score':score,'confidence':'heuristic_not_probability',
            'reasons':reasons,'note':'Test edilmiş başarı olasılığı değildir'}

async def mtf_analysis(symbol):
    async def one(tf):
        try:
            ind=await asyncio.to_thread(indicators,await get_candles(symbol,tf,250))
            if not ind or any(ind.get(k) is None for k in ('last_closed_price','ema20','ema50','rsi14')):
                return {'tf':tf,'direction':'YETERSIZ','reason':'Kapanmış mum yetersiz'}
            last=ind['last_closed_price'];e20=ind['ema20'];e50=ind['ema50'];r=ind['rsi14']
            direction='LONG' if last>e20>e50 and r>=50 else 'SHORT' if last<e20<e50 and r<=50 else 'KARISIK'
            return {'tf':tf,'direction':direction,'rsi14':round(r,2),'ema20':round(e20,8),
                    'ema50':round(e50,8),'closed_t':ind['last_closed_t']}
        except Exception as exc:
            LOG.warning('MTF %s %s: %s',symbol,tf,exc)
            return {'tf':tf,'direction':'YETERSIZ','reason':'Veri veya gösterge hesaplama hatası'}
    return await asyncio.gather(*(one(tf) for tf in MTF))

def flow_extras(m,flow):
    now=now_ms();recent=[v for ts,v in m.book_history if now-ts<=5000]
    previous=[v for ts,v in m.book_history if 10000<=now-ts<=20000]
    delta=(sum(recent)/len(recent)-sum(previous)/len(previous)) if recent and previous else None
    def walls(levels):
        sizes=sorted(p*q for p,q in levels);med=sizes[len(sizes)//2] if sizes else 0
        return [{'price':p,'notional':round(p*q,2),'multiple':round(p*q/med,1)}
                for p,q in levels if med>0 and p*q>=max(3*med,1000)][:5]
    return {'imbalance_delta_10s':delta,'bid_walls':walls(m.bids),'ask_walls':walls(m.asks),
            'wall_definition':'>=3x median level notional and >=1000 USDT; partial 20 levels'}

def enhanced_decision(base,mtf,flow,extras):
    reasons=list(base['reasons']);dirs=[x['direction'] for x in mtf]
    if any(d=='YETERSIZ' for d in dirs):
        return {**base,'decision':'BEKLE','reasons':reasons+['MTF mum verisi eksik'],
                'confidence':'veri_yetersiz','mtf_agreement':False}
    if base['decision']=='BEKLE':return {**base,'mtf_agreement':False}
    direction=base['decision'];aligned=all(d==direction for d in dirs[:3])
    if not aligned:reasons.append('1m/3m/5m zaman dilimleri aynı yönde değil')
    delta=extras['imbalance_delta_10s']
    if delta is not None and ((direction=='LONG' and delta<-.35) or (direction=='SHORT' and delta>.35)):
        aligned=False;reasons.append('Emir defteri baskısı ters yöne hızla değişiyor')
    return {**base,'decision':direction if aligned else 'BEKLE','reasons':reasons,
            'mtf_agreement':aligned,'confidence':'heuristic_not_probability'}

@asynccontextmanager
async def lifespan(app):
    global http_client
    http_client=httpx.AsyncClient(timeout=12)
    await bootstrap();task=asyncio.create_task(relay())
    try:yield
    finally:
        task.cancel();await asyncio.gather(task,return_exceptions=True)
        await http_client.aclose()

app=FastAPI(title='TiMi SCALP PRO',version=VERSION,lifespan=lifespan)
origins=[s.strip() for s in os.getenv('TIMI_ORIGINS','').split(',') if s.strip()]
app.add_middleware(CORSMiddleware,allow_origins=origins,allow_methods=['GET'],allow_headers=['*'])

@app.get('/')
async def index():return FileResponse(ROOT/'index.html')

@app.get('/api/health')
async def health():
    age=now_ms()-status['last_message_ms'] if status['last_message_ms'] else None
    return {'app':'TiMi SCALP PRO','version':VERSION,'upstream_connected':status['connected'],
            'last_message_age_ms':age,'upstream_endpoint':status['endpoint'],
            'upstream_error':status['error'],'symbols':SYMBOLS,'real_orders_enabled':False,
            'paper_orders_enabled':False,'ml_model_loaded':False}

@app.get('/api/markets')
async def market_list():
    return {'markets':[{k:v for k,v in m.public().items() if k not in ('bids','asks','trades')}
                       for m in markets.values()]}

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
    try:ind=await asyncio.to_thread(indicators,await get_candles(symbol,interval,250))
    except HTTPException:ind=None
    return {'symbol':symbol,'interval':interval,'ts':now_ms(),'flow':flow,
            'indicators':ind,'signal':decision(m,flow,ind),'market_price':m.price}

@app.get('/api/analysis-pro/{symbol}')
async def analysis_pro(symbol:str):
    symbol=symbol.upper()
    if symbol not in markets:raise HTTPException(400,'Invalid symbol')
    m=markets[symbol];flow=orderflow(m)
    frames=await mtf_analysis(symbol);extras=flow_extras(m,flow)
    try:ind=await asyncio.to_thread(indicators,await get_candles(symbol,'1m',250))
    except HTTPException:ind=None
    base=decision(m,flow,ind);signal=enhanced_decision(base,frames,flow,extras)
    return {'symbol':symbol,'ts':now_ms(),'interval':'1m','flow':flow,'indicators':ind,
            'signal':signal,'mtf':frames,'extras':extras,'market_price':m.price,
            'ml':{'status':'not_trained','probabilities':None},
            'disclaimer':'Kural tabanlı gözlem; test edilmiş başarı olasılığı değildir'}

@app.get('/api/indicators-all/{symbol}')
async def indicators_all(symbol:str,interval:str='5m'):
    symbol=symbol.upper()
    if symbol not in markets or interval not in TF:raise HTTPException(400,'Invalid symbol or interval')
    candles=await get_candles(symbol,interval,500)
    try:
        df=await asyncio.wait_for(asyncio.to_thread(feature_frame,candles,True),timeout=45)
    except Exception as exc:
        LOG.warning('Full indicators failed %s %s: %s',symbol,interval,exc)
        raise HTTPException(503,'Full indicator calculation unavailable') from exc
    if df is None:raise HTTPException(422,'Not enough closed candles')
    row=df.iloc[-1]
    values={str(k):finite(v) for k,v in row.items() if k not in ('t','closed') and finite(v) is not None}
    return {'symbol':symbol,'interval':interval,'last_closed_t':int(row['t']),
            'feature_count':len(values),'features':values,
            'note':'Indicator features, not independent predictions; not a trained model'}

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

# No paper trading, no credentials, no real order endpoints.
