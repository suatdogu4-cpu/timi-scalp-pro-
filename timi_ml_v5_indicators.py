"""TiMi ML v5 — 26 browser-indicator formulas translated to Python.

Use only *closed* candles in ascending time order, no future shifts.
The thresholds/votes are DESCRIPTIVE, not ML success probabilities.
Developer: Suat DOĞU. Read-only research data collection.
"""
from __future__ import annotations
import math

NAMES = (
    'rsi14', 'stoch_k_14_3_3', 'cci20', 'adx14', 'awesome_osc',
    'momentum10', 'macd_hist_12_26_9', 'stoch_rsi_14_14_3',
    'williams_r14', 'bull_bear13', 'ultimate_7_14_28',
    'ema10','sma10','ema20','sma20','ema30','sma30','ema50','sma50',
    'ema100','sma100','ema200','sma200','vwma20','hma9','ichimoku_kijun26',
)

NAN=float('nan')

def ok(x):
    return isinstance(x,(int,float)) and math.isfinite(x)

def mean(a,n):
    out=[NAN]*len(a)
    for i in range(n-1,len(a)):
        t=a[i-n+1:i+1]
        if all(ok(v) for v in t):out[i]=sum(t)/n
    return out

def ema(a,n):
    out=[NAN]*len(a)
    if len(a)<n:return out
    out[n-1]=mean(a,n)[n-1]
    if not ok(out[n-1]):return out
    k=2/(n+1)
    for i in range(n,len(a)):
        out[i]=k*a[i]+(1-k)*out[i-1] if ok(a[i]) and ok(out[i-1]) else NAN
    return out

def wma(a,n):
    out=[NAN]*len(a);den=n*(n+1)/2
    for i in range(n-1,len(a)):
        t=a[i-n+1:i+1]
        if all(ok(v) for v in t):out[i]=sum((j+1)*v for j,v in enumerate(t))/den
    return out

def hma(a,n):
    x=wma(a,n//2);y=wma(a,n)
    return wma([2*x[i]-y[i] if ok(x[i]) and ok(y[i]) else NAN for i in range(len(a))],round(math.sqrt(n)))

def rsi(a,n):
    out=[NAN]*len(a)
    if len(a)<=n:return out
    diffs=[a[i]-a[i-1] for i in range(1,n+1)]
    gain=sum(max(0,v) for v in diffs)/n;loss=sum(max(0,-v) for v in diffs)/n
    def value(g,l):return (50. if g==0 else 100.) if l==0 else 100-100/(1+g/l)
    out[n]=value(gain,loss)
    for i in range(n+1,len(a)):
        d=a[i]-a[i-1];gain=(gain*(n-1)+max(d,0))/n;loss=(loss*(n-1)+max(-d,0))/n
        out[i]=value(gain,loss)
    return out

def atr_dmi(candles,n):
    m=len(candles);tr=[NAN]*m;pd=[0.]*m;md=[0.]*m
    for i in range(1,m):
        a=candles[i];p=candles[i-1];up=a['h']-p['h'];dn=p['l']-a['l']
        tr[i]=max(a['h']-a['l'],abs(a['h']-p['c']),abs(a['l']-p['c']))
        pd[i]=up if up>dn and up>0 else 0.
        md[i]=dn if dn>up and dn>0 else 0.
    atr=[NAN]*m;plus=[NAN]*m;minus=[NAN]*m;dx=[NAN]*m;adx=[NAN]*m
    if m<=n:return atr,plus,minus,adx
    atv=sum(tr[1:n+1])/n;pv=sum(pd[1:n+1])/n;mv=sum(md[1:n+1])/n
    for i in range(n,m):
        if i>n:
            atv=(atv*(n-1)+tr[i])/n;pv=(pv*(n-1)+pd[i])/n;mv=(mv*(n-1)+md[i])/n
        atr[i]=atv;plus[i]=100*pv/atv if atv>0 else 0.
        minus[i]=100*mv/atv if atv>0 else 0.
        dx[i]=100*abs(plus[i]-minus[i])/(plus[i]+minus[i]) if plus[i]+minus[i]>0 else 0.
    if m>=2*n:
        adx[2*n-1]=sum(dx[n:2*n])/n
        for i in range(2*n,m):adx[i]=(adx[i-1]*(n-1)+dx[i])/n
    return atr,plus,minus,adx

def stochastic(a,n=14):
    out=[NAN]*len(a)
    for i in range(n-1,len(a)):
        hi=max(x['h'] for x in a[i-n+1:i+1]);lo=min(x['l'] for x in a[i-n+1:i+1])
        out[i]=50. if hi==lo else 100*(a[i]['c']-lo)/(hi-lo)
    k=mean(out,3)
    return k,mean(k,3)

def cci(a,n):
    tp=[(x['h']+x['l']+x['c'])/3 for x in a];av=mean(tp,n)
    out=[NAN]*len(a)
    for i in range(n-1,len(a)):
        mad=sum(abs(x-av[i]) for x in tp[i-n+1:i+1])/n
        out[i]=(tp[i]-av[i])/(.015*mad) if mad else 0.
    return out

def williams(a,n):
    out=[NAN]*len(a)
    for i in range(n-1,len(a)):
        sl=a[i-n+1:i+1];hi=max(x['h'] for x in sl);lo=min(x['l'] for x in sl)
        out[i]=-50. if hi==lo else -100*(hi-a[i]['c'])/(hi-lo)
    return out

def ultimate(a):
    if len(a)<28:return NAN
    bp=[];tr=[]
    for i,x in enumerate(a):
        p=a[i-1]['c'] if i else x['c'];low=min(x['l'],p);high=max(x['h'],p)
        bp.append(x['c']-low);tr.append(high-low)
    def m(n):
        den=sum(tr[-n:]);return sum(bp[-n:])/den if den else 0.
    return 100*(4*m(7)+2*m(14)+m(28))/7

def calculate_26(candles):
    """Return numeric values + 26 descriptive votes, browser-equivalent formulas.

    Candles: >=230 *closed* 1m OHLCV; timestamps ms, aligned and gapless.
    """
    if len(candles)<230:raise ValueError('Need >=230 closed candles')
    if any(not isinstance(x,dict) or x.get('closed') is not True for x in candles):
        raise ValueError('Only source-confirmed closed candles are allowed')
    seq=[int(x['t']) for x in candles]
    if any(b-a != 60000 for a,b in zip(seq,seq[1:])):raise ValueError('Missing/duplicate 1m candle')
    a=candles;n=len(a);C=[x['c'] for x in a];H=[x['h'] for x in a];L=[x['l'] for x in a];V=[x['v'] for x in a]
    if any(not all(ok(x[k]) for k in ('o','h','l','c','v')) or x['c']<=0 or x['l']<=0 or x['v']<0 or x['h']<max(x['c'],x['o'],x['l']) or x['l']>min(x['c'],x['o']) for x in a):
        raise ValueError('Invalid OHLCV')
    now=C[-1];rv=rsi(C,14);rs=rv[-1];atr,plus,minus,adx=atr_dmi(a,14);vol=atr[-1];d={};votes={}
    def put(k,v,buy,sell):
        d[k]=float(v) if ok(v) else None
        votes[k]=1 if ok(v) and buy else -1 if ok(v) and sell else 0 if ok(v) else None
    put('rsi14',rs,rs<30,rs>70)
    kk,dd=stochastic(a);k=kk[-1]
    put('stoch_k_14_3_3',k,k<20 and k>dd[-1],k>80 and k<dd[-1])
    cc=cci(a,20)[-1];put('cci20',cc,cc< -100,cc>100)
    ad=adx[-1];put('adx14',ad,ad>25 and plus[-1]>minus[-1],ad>25 and minus[-1]>plus[-1])
    median=[(x['h']+x['l'])/2 for x in a];avg5=mean(median,5);avg34=mean(median,34)
    ao=avg5[-1]-avg34[-1];ap=avg5[-2]-avg34[-2]
    put('awesome_osc',ao,ao>0 and ao>ap,ao<0 and ao<ap)
    mom=now-C[-11];put('momentum10',mom,mom>0,mom<0)
    e12=ema(C,12);e26=ema(C,26);macd=[e12[i]-e26[i] for i in range(n)];macds=ema(macd[25:],9)
    hist=macd[-1]-macds[-1];put('macd_hist_12_26_9',hist,hist>0,hist<0)
    st_r=[NAN]*n
    for i in range(27,n):
        sl=rv[i-13:i+1]
        if not all(ok(x) for x in sl):continue
        hi=max(sl);lo=min(sl);st_r[i]=50. if hi==lo else 100*(rv[i]-lo)/(hi-lo)
    sr_k=mean(st_r,3);sr=sr_k[-1]
    put('stoch_rsi_14_14_3',sr,sr<20 and sr>sr_k[-2],sr>80 and sr<sr_k[-2])
    wr=williams(a,14)[-1];put('williams_r14',wr,wr< -80,wr> -20)
    e13=ema(C,13)[-1];bbp=(H[-1]-e13)+(L[-1]-e13)
    put('bull_bear13',bbp,bbp>vol*.05,bbp< -vol*.05)
    ul=ultimate(a);put('ultimate_7_14_28',ul,ul<30,ul>70)
    def ma_put(key,val):
        margin=max(vol*.025,now*.00005)
        put(key,val,now>val+margin,now<val-margin)
    for period in (10,20,30,50,100,200):
        ma_put('ema'+str(period),ema(C,period)[-1]);ma_put('sma'+str(period),mean(C,period)[-1])
    denom=sum(V[-20:]);vw=sum(V[i]*C[i] for i in range(n-20,n))/denom if denom else NAN
    ma_put('vwma20',vw);ma_put('hma9',hma(C,9)[-1]);ma_put('ichimoku_kijun26',(max(H[-26:])+min(L[-26:]))/2)
    if tuple(d)!=NAMES:raise AssertionError('Feature order mismatch')
    buy=sum(x==1 for x in votes.values());sell=sum(x==-1 for x in votes.values());neutral=sum(x==0 for x in votes.values())
    return {'schema':'timi-v5-indicators-v1','values':d,'votes':votes,'counts':{'al':buy,'sat':sell,'notr':neutral,'valid':buy+sell+neutral},
            'vote_ratio':(buy-sell)/(buy+sell+neutral) if buy+sell+neutral else None,'candle_open_ms':seq[-1],
            'note':'Browser formula parity intended; verify JS golden tests on production candle datasets'}
