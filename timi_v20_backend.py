"""TiMi PRO v2.0 read-only extension: bounded live flow and public futures snapshot.

No orders, credentials, persistence, database operations, training, or archived signals.
API responses label missing/stale measurements instead of inventing values.
"""
import asyncio
import math
import time
from collections import deque
from dataclasses import dataclass

from fastapi import HTTPException

FAPI = 'https://fapi.binance.com'
OKX = 'https://www.okx.com'


def finite(v):
    try:
        f = float(v)
        return f if math.isfinite(f) else None
    except (TypeError, ValueError, OverflowError):
        return None


def timestamp_ms(v):
    try:
        n = int(v)
        return n if 1000000000000 <= n < 9999999999999 else None
    except (ValueError, TypeError, OverflowError):
        return None


@dataclass
class SecondBucket:
    sec: int
    buy: float = 0.
    sell: float = 0.
    count: int = 0
    first: float = 0.
    last: float = 0.


class LiveFlow:
    """At most 310 O(1) per-second aggregates per selected market."""

    def __init__(self, symbols, clock):
        self.clock = clock
        self.buckets = {s: deque(maxlen=310) for s in symbols}

    def reset_all(self):
        """Drop observations from an upstream connection that has ended."""
        for rows in self.buckets.values():
            rows.clear()

    def record_trade(self, symbol, side, qty, price, received_ms):
        if symbol not in self.buckets or side not in ('buy', 'sell'):
            return
        price, qty = finite(price), finite(qty)
        if price is None or qty is None or price <= 0 or qty <= 0:
            return
        sec = int(received_ms) // 1000
        entries = self.buckets[symbol]
        if entries and sec < entries[-1].sec:
            return
        if not entries or entries[-1].sec != sec:
            entries.append(SecondBucket(sec=sec, first=price, last=price))
        b = entries[-1]
        b.last = price
        b.count += 1
        if side == 'buy':
            b.buy += price * qty
        else:
            b.sell += price * qty
        while entries and entries[0].sec < sec - 310:
            entries.popleft()

    def snapshot(self, symbol, market, connected):
        now = int(self.clock())
        now_sec = now // 1000
        entries = list(self.buckets[symbol])
        frames = {}
        for seconds in (15, 60, 300):
            rows = [x for x in entries if 0 <= now_sec - x.sec < seconds]
            buy = sum(x.buy for x in rows)
            sell = sum(x.sell for x in rows)
            total = buy + sell
            trade_count = sum(x.count for x in rows)
            wall_span = now_sec - entries[0].sec + 1 if entries else 0
            elapsed = min(seconds, max(0, wall_span))
            price_first = rows[0].first if rows else None
            price_last = rows[-1].last if rows else None
            change_pct = (price_last / price_first - 1) * 100 if price_first and price_last else None
            frames[str(seconds)] = {
                'window_sec': seconds, 'observed_elapsed_sec': elapsed,
                'seconds_with_trades': len(rows), 'complete_window': elapsed >= seconds,
                'trade_count': trade_count, 'buy_usdt': round(buy, 4),
                'sell_usdt': round(sell, 4), 'delta_usdt': round(buy-sell, 4),
                'aggressor_balance_pct': round((buy-sell)/total*100, 3) if total > 0 else None,
                'price_change_pct': round(change_pct, 5) if change_pct is not None else None
            }
        m = market
        book_age = (now - m.last_book_ms) if m.last_book_ms else None
        trade_age = (now - m.last_trade_ms) if m.last_trade_ms else None
        book_ok = bool(connected and m.book_valid and book_age is not None and 0 <= book_age <= 5000)
        trade_ok = bool(connected and trade_age is not None and 0 <= trade_age <= 5000)
        bids = m.bids if book_ok else []
        asks = m.asks if book_ok else []
        bn = sum(p*q for p,q in bids)
        an = sum(p*q for p,q in asks)
        bid = bids[0][0] if bids else None
        ask = asks[0][0] if asks else None
        bid_qty = bids[0][1] if bids else None
        ask_qty = asks[0][1] if asks else None
        valid_quote = bid is not None and ask is not None and ask > bid and bid_qty is not None and ask_qty is not None
        micro = (ask*bid_qty+bid*ask_qty)/(bid_qty+ask_qty) if valid_quote and bid_qty+ask_qty>0 else None
        spread_pct = (ask-bid)/((ask+bid)/2)*100 if valid_quote else None
        # Distinguish candidate absorption from verified iceberg/true order identity.
        w = frames['15']
        a = w['aggressor_balance_pct']
        candidate = None
        if trade_ok and book_ok and w['complete_window'] and w['trade_count']>=15 and a is not None \
                and w['buy_usdt']+w['sell_usdt']>=10000 and abs(a)>=30 \
                and w['price_change_pct'] is not None and abs(w['price_change_pct']) <= 0.05:
            candidate = 'ALIS_EMILIM_ADAYI' if a >= 30 else 'SATIS_EMILIM_ADAYI'
        return {
            'symbol': symbol, 'time_ms': now, 'read_only': True,
            'market_price': finite(m.price) if trade_ok else None,
            'source': 'Binance Spot 20-level partial book + received public @trade stream',
            'status': {'book_fresh': book_ok, 'trades_fresh': trade_ok,
                       'book_age_ms': book_age, 'trade_age_ms': trade_age},
            'book': {'bid_notional_usdt': round(bn, 3) if book_ok else None,
                     'ask_notional_usdt': round(an, 3) if book_ok else None,
                     'imbalance_pct': round((bn-an)/(bn+an)*100, 3) if book_ok and bn+an>0 else None,
                     'spread_pct': round(spread_pct, 6) if spread_pct is not None else None,
                     'microprice': round(micro, 8) if micro is not None else None},
            'windows': frames, 'absorption_candidate': candidate,
            'limitations': ('Five-minute in-process rolling aggregation ONLY; resets on restart; '
                            'not stored to disk. Partial top-20 book, not full-depth historical data. '
                            'An absorption candidate cannot prove resting-order identity.')
        }


class FuturesData:
    """Public unauthenticated market data. Each venue/metric independently validated."""

    def __init__(self, symbols, client_getter, clock):
        self.symbols = set(symbols)
        self.get_client = client_getter
        self.clock = clock
        self.cache = {}
        self.locks = {s: asyncio.Lock() for s in symbols}

    async def _query(self, endpoint, params):
        client = self.get_client()
        if client is None:
            raise RuntimeError('HTTP client not ready')
        response = await asyncio.wait_for(client.get(endpoint, params=params), timeout=9)
        response.raise_for_status()
        return response.json()

    async def snapshot(self, symbol):
        if symbol not in self.symbols:
            raise HTTPException(status_code=400, detail='Unsupported symbol')
        now=int(self.clock())
        cached=self.cache.get(symbol)
        if cached and now-cached[0] < 25000:
            return {**cached[1], 'cache_age_ms': now-cached[0]}
        async with self.locks[symbol]:
            now = int(self.clock())
            cached=self.cache.get(symbol)
            if cached and now-cached[0] < 25000:
                return {**cached[1], 'cache_age_ms': now-cached[0]}
            coin=symbol.removesuffix('USDT')
            iid=coin+'-USDT-SWAP'
            tasks = [
                self._query(FAPI+'/fapi/v1/premiumIndex', {'symbol':symbol}),
                self._query(FAPI+'/fapi/v1/openInterest', {'symbol':symbol}),
                self._query(FAPI+'/futures/data/takerlongshortRatio',{'symbol':symbol,'period':'5m','limit':1}),
                self._query(OKX+'/api/v5/public/funding-rate', {'instId':iid}),
                self._query(OKX+'/api/v5/public/open-interest', {'instType':'SWAP','instId':iid})
            ]
            results=await asyncio.gather(*tasks,return_exceptions=True)
            errors={}
            def get(i,name):
                if isinstance(results[i], Exception):
                    e=results[i]
                    status=getattr(getattr(e,'response',None),'status_code',None)
                    errors[name]=f'HTTP {status}' if status is not None else type(e).__name__
                    return None
                return results[i]
            mark=get(0,'binance_mark')
            oi=get(1,'binance_oi')
            taker=get(2,'binance_taker')
            okf=get(3,'okx_funding')
            oko=get(4,'okx_oi')
            b={'venue':'Binance USD-M perpetual', 'mark_price':None,'index_price':None,
               'funding_rate_pct':None,'next_funding_ms':None,'open_interest_quantity':None,
               'taker_buy_sell_ratio_5m':None,'data_time_ms':None,'available':False}
            if isinstance(mark,dict) and mark.get('symbol')==symbol:
                t=timestamp_ms(mark.get('time'))
                if t and abs(now-t)<120000:
                    b['mark_price']=finite(mark.get('markPrice'))
                    b['index_price']=finite(mark.get('indexPrice'))
                    fund=finite(mark.get('lastFundingRate'))
                    b['funding_rate_pct']=round(fund*100,7) if fund is not None else None
                    b['next_funding_ms']=timestamp_ms(mark.get('nextFundingTime'))
                    b['data_time_ms']=t
                else:
                    errors['binance_mark']='stale or invalid exchange timestamp'
            if isinstance(oi,dict) and oi.get('symbol')==symbol:
                t=timestamp_ms(oi.get('time'))
                if t and abs(now-t)<120000:
                    b['open_interest_quantity']=finite(oi.get('openInterest'))
                else:
                    errors['binance_oi']='stale or invalid timestamp'
            if isinstance(taker,list) and len(taker)>0 and isinstance(taker[-1],dict):
                t=timestamp_ms(taker[-1].get('timestamp'))
                if t and -300000 < now-t < 1200000:
                    b['taker_buy_sell_ratio_5m']=finite(taker[-1].get('buySellRatio'))
                else:
                    errors['binance_taker']='stale 5m statistic'
            b['available']=any(b[k] is not None for k in
                               ('mark_price','funding_rate_pct','open_interest_quantity','taker_buy_sell_ratio_5m'))
            o={'venue':'OKX USDT SWAP','instrument_id':iid,'funding_rate_pct':None,
               'next_funding_ms':None,'open_interest_usd':None,'data_time_ms':None,'available':False}
            if isinstance(okf,dict) and okf.get('code')=='0':
                row=next((x for x in okf.get('data',[]) if x.get('instId')==iid),None)
                if row:
                    t=timestamp_ms(row.get('ts'))
                    if t and abs(now-t)<180000:
                        z=finite(row.get('fundingRate'))
                        o['funding_rate_pct']=round(z*100,7) if z is not None else None
                        o['next_funding_ms']=timestamp_ms(row.get('nextFundingTime'))
                        o['data_time_ms']=t
                    else:
                        errors['okx_funding']='stale or invalid timestamp'
            if isinstance(oko,dict) and oko.get('code')=='0':
                row=next((x for x in oko.get('data',[]) if x.get('instId')==iid),None)
                if row:
                    t=timestamp_ms(row.get('ts'))
                    if t and abs(now-t)<180000:
                        o['open_interest_usd']=finite(row.get('oiUsd'))
                        o['data_time_ms']=t
                    else:
                        errors['okx_oi']='stale or invalid timestamp'
            o['available']=any(o[k] is not None for k in ('funding_rate_pct','open_interest_usd'))
            result={'symbol':symbol,'time_ms':now,'cache_age_ms':0,'read_only':True,
                    'binance':b,'okx':o,'errors':errors,
                    'comparison_note':('OI units differ: Binance quantity vs OKX USD notional; '
                                       'NEVER compare these OI amounts directly. Funding rates can '
                                       'have different intervals and next-settlement times. '
                                       'Unavailable or restricted APIs are labelled missing. '
                                       'No liquidation data or imaginary heat map.')}
            self.cache[symbol]=(now,result)
            return result


def install_v20(app, markets, status, clock, http_getter):
    """Registers two GET-only API routes and returns a bounded live trade recorder."""
    flow=LiveFlow(markets.keys(),clock)
    futures=FuturesData(markets.keys(),http_getter,clock)

    @app.get('/api/v2/flow/{symbol}')
    async def v20_flow(symbol:str):
        symbol=symbol.upper()
        if symbol not in markets:
            raise HTTPException(status_code=400,detail='Unsupported symbol')
        return flow.snapshot(symbol,markets[symbol],bool(status.get('connected')))

    @app.get('/api/v2/futures/{symbol}')
    async def v20_futures(symbol:str):
        return await futures.snapshot(symbol.upper())

    return flow
