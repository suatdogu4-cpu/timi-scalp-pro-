"""TiMi ML v5 — opt-in, read-only, durable research archive.

Developer: Suat DOĞU.
This module never sends trading orders, performs inference, or computes P&L.
No backfilled order-flow history. Only actually observed live WebSocket flow.

Storage:
  TIMI_V5_DATABASE_URL=postgresql://...  -> durable external PostgreSQL (Render supported)
  TIMI_V5_SQLITE_PATH=...              -> local research only; Render disk may be ephemeral
  neither                              -> disabled; live app is unaffected

Install after `app = FastAPI(...)`, before the route decorators:
  from timi_ml_v5_archive import install_v5_archive
  install_v5_archive(app, get_candles=get_candles, cached_pro=cached_pro,
                     now_ms=now_ms, allowed_symbols=SYMBOLS)
"""
from __future__ import annotations
import asyncio
from contextlib import asynccontextmanager
import json
import logging
import os
from pathlib import Path
import sqlite3
import time

from timi_ml_v5_indicators import calculate_26

LOG = logging.getLogger('timi.ml-v5-archive')
TF_MS = {'1m':60000,'3m':180000,'5m':300000,'15m':900000}
SCHEMA = 'timi-ml-v5-observation-v1'
SQL_PG = '''CREATE TABLE IF NOT EXISTS timi_v5_observations (
  symbol VARCHAR(20) NOT NULL,
  candle_open_ms BIGINT NOT NULL,
  candle_close_ms BIGINT NOT NULL,
  observed_ms BIGINT NOT NULL,
  payload JSONB NOT NULL,
  inserted_at TIMESTAMPTZ NOT NULL DEFAULT now(),
  PRIMARY KEY(symbol,candle_open_ms)
)'''
SQL_SQLITE = '''CREATE TABLE IF NOT EXISTS timi_v5_observations (
  symbol TEXT NOT NULL,
  candle_open_ms INTEGER NOT NULL,
  candle_close_ms INTEGER NOT NULL,
  observed_ms INTEGER NOT NULL,
  payload TEXT NOT NULL,
  inserted_at TEXT NOT NULL DEFAULT CURRENT_TIMESTAMP,
  PRIMARY KEY(symbol,candle_open_ms)
)'''


def check_observation(candles, pro, observed_at_ms):
    """Reject inferred/incomplete candles; flag late/missing live-flow snapshots.

    Returns (last, recent_closed, quality). Each `quality` flag survives into DB.
    If quality ML-valid is false, research must exclude the sample.
    """
    closed = [dict(c) for c in candles if c.get('closed') is True]
    if len(closed) < 230:
        raise ValueError('Insufficient source-confirmed 1m candles for 26 indicators')
    recent = closed[-260:]
    last = recent[-1]
    if any(int(b['t'])-int(a['t']) != 60000 for a,b in zip(recent,recent[1:])):
        raise ValueError('1m candle gap or duplicate; refusing sample')
    close_ms=int(last['t'])+60000
    delay=observed_at_ms-close_ms
    if delay < 0 or delay > 90000:
        raise ValueError('No fresh closed 1m candle')
    pro_closed=pro.get('analysis_candle_closed_at_ms')
    pro_clock=pro.get('decision_evaluated_at_ms',pro.get('ts'))
    flow=pro.get('flow') or {}
    mtf=pro.get('mtf') or []
    matched=pro_closed == close_ms
    valid_flow=flow.get('fresh') is True
    pro_fresh=isinstance(pro_clock,int) and 0 <= observed_at_ms-pro_clock<=15000
    mtf_status=[]
    for m in mtf:
        tf=m.get('tf');step=TF_MS.get(tf);t=m.get('closed_at_ms')
        eligible=bool(step and isinstance(t,int) and isinstance(pro_clock,int)
                      and t <= pro_clock and 0<=pro_clock-t<=2*step)
        mtf_status.append({'tf':tf,'last_closed_at_ms':t,'fresh':eligible,'direction':m.get('direction')})
    has_mtf={x['tf'] for x in mtf_status if x['fresh']}
    mtf_complete=set(TF_MS).issubset(has_mtf)
    quality={'closed_candle_verified':True,
             'snapshot_delay_from_close_ms':delay,
             'pro_analysis_matches_candle':matched,
             'flow_fresh':valid_flow,
             'snapshot_time_fresh':pro_fresh,
             'mtf_all_four_fresh':mtf_complete,
             'ml_eligible':bool(matched and valid_flow and pro_fresh and mtf_complete),
             'note':('Flow and MTF are OBSERVED at decision_evaluated_at_ms (after candle close). '
                     'Labels must start after that timestamp; never pretend these were known at candle close.')}
    return last, recent, quality


def make_record(symbol,candles,pro,observed_at_ms):
    last,recent,quality=check_observation(candles,pro,observed_at_ms)
    measured=calculate_26(recent)
    snap_ms=int(pro.get('decision_evaluated_at_ms') or pro.get('ts') or observed_at_ms)
    flow=pro.get('flow') or {}
    extras=pro.get('extras') or {}
    market=pro.get('market_price')
    def numeric(x):
        try:
            v=float(x)
            return v if __import__('math').isfinite(v) else None
        except (TypeError,ValueError,OverflowError):return None
    # Preserve only read-only, numeric market descriptors; no keys or private data.
    flow_keys=('trade_age_ms','book_age_ms','bid_notional','ask_notional','imbalance',
               'buy_notional_60s','sell_notional_60s','cvd_60s','trade_count_60s',
               'spread_pct')
    selected_flow={k:numeric(flow.get(k)) for k in flow_keys}
    extra_keys=('imbalance_delta_10s','bid_walls','ask_walls','liquidity_score')
    selected_extras={k:extras.get(k) for k in extra_keys if k in extras}
    # Some extra values are arrays of large dictionaries; cap output size.
    selected_extras={k:v for k,v in selected_extras.items() if len(str(v))<=1500}
    result={'schema':SCHEMA,'symbol':symbol,'interval':'1m',
            'candle':{'t':int(last['t']),'o':float(last['o']),'h':float(last['h']),
                      'l':float(last['l']),'c':float(last['c']),'v':float(last['v']),
                      'closed':True},
            'candle_close_ms':int(last['t'])+60000,
            'observed_at_ms':int(observed_at_ms),'feature_available_at_ms':snap_ms,
            'decision':(pro.get('signal') or {}).get('decision','BEKLE'),
            'rule_score':numeric((pro.get('signal') or {}).get('score')),
            'market_price_at_snapshot':numeric(market),
            'indicators_26':measured,
            'pro_indicators':pro.get('indicators'),
            'flow':selected_flow,
            'flow_metadata':{'book_valid':flow.get('book_valid'),
                             'fresh':flow.get('fresh'),
                             'window':flow.get('window'),
                             'depth_type':flow.get('depth_type'),
                             'cvd_note':flow.get('cvd_note')},
            'orderbook_extras':selected_extras,
            'mtf':[{'tf':x.get('tf'),'direction':x.get('direction'),
                    'closed_at_ms':x.get('closed_at_ms'),
                    'reason':x.get('reason')} for x in (pro.get('mtf') or [])],
            'quality':quality,
            'research_only':True,
            'no_execution':True,
            'no_model_prediction':True}
    # Avoid emitting un-serializable stale 3rd-party values.
    json.dumps(result,allow_nan=False)
    return result


class ArchiveV5:
    def __init__(self,*,get_candles,cached_pro,now_ms,allowed_symbols):
        self.get_candles=get_candles;self.cached_pro=cached_pro;self.now_ms=now_ms
        choices=[x.strip().upper() for x in os.getenv('TIMI_V5_SYMBOLS','BTCUSDT').split(',')]
        self.symbols=tuple(dict.fromkeys(x for x in choices if x in allowed_symbols))
        self.pg_url=os.getenv('TIMI_V5_DATABASE_URL','').strip()
        self.sqlite_path=os.getenv('TIMI_V5_SQLITE_PATH','').strip()
        self.backend='postgres' if self.pg_url else 'sqlite' if self.sqlite_path else 'disabled'
        self.pool=None;self.sampled=0;self.duplicates=0;self.rejected=0;self.errors=0
        self.last_sample_ms=None;self.last_error=None;self.task=None;self._db_lock=asyncio.Lock()
        self._counts_cache=None;self._counts_at=0

    async def _ensure_connection(self):
        if self.backend=='disabled':return
        if self.backend=='postgres' and self.pool is None:
            # Lazy import: production requires asyncpg in requirements.txt.
            import asyncpg
            pool=await asyncio.wait_for(asyncpg.create_pool(
                dsn=self.pg_url,min_size=1,max_size=2,command_timeout=8,
                timeout=8,statement_cache_size=0),timeout=10)
            try:
                async with pool.acquire() as conn:await conn.execute(SQL_PG)
            except Exception:
                await pool.close()
                raise
            self.pool=pool
        elif self.backend=='sqlite':
            def create():
                path=Path(self.sqlite_path).expanduser();path.parent.mkdir(parents=True,exist_ok=True)
                with sqlite3.connect(str(path),timeout=5) as conn:
                    conn.execute(SQL_SQLITE)
            await asyncio.to_thread(create)

    async def _write(self,record):
        s=record['symbol'];t=int(record['candle']['t']);c=int(record['candle_close_ms'])
        snap=int(record['feature_available_at_ms'])
        payload=json.dumps(record,ensure_ascii=False,allow_nan=False,separators=(',',':'))
        async with self._db_lock:
            await self._ensure_connection()
            if self.backend=='postgres':
                async with self.pool.acquire() as conn:
                    status=await conn.execute('''INSERT INTO timi_v5_observations
                      (symbol,candle_open_ms,candle_close_ms,observed_ms,payload)
                      VALUES($1,$2,$3,$4,$5::jsonb)
                      ON CONFLICT(symbol,candle_open_ms) DO NOTHING''',s,t,c,snap,payload)
                inserted=status.endswith(' 1')
            else:
                def write():
                    with sqlite3.connect(self.sqlite_path,timeout=5) as conn:
                        cur=conn.execute('''INSERT OR IGNORE INTO timi_v5_observations
                          (symbol,candle_open_ms,candle_close_ms,observed_ms,payload)
                          VALUES(?,?,?,?,?)''',(s,t,c,snap,payload))
                        return cur.rowcount==1
                inserted=await asyncio.to_thread(write)
            self._counts_cache=None
        if inserted:self.sampled+=1;self.last_sample_ms=snap
        else:self.duplicates+=1
        return inserted

    async def sample(self,symbol):
        # get_candles cannot infer closure. Source-confirmed closed=True is mandatory.
        candles=await self.get_candles(symbol,'1m',260)
        pro=await self.cached_pro(symbol)
        stamp=self.now_ms()
        record=await asyncio.to_thread(make_record,symbol,candles,pro,stamp)
        await self._write(record)

    async def loop(self):
        while True:
            try:
                now=self.now_ms()
                next_slot=(now//60000+1)*60000+12000
                await asyncio.sleep(max(.1,(next_slot-now)/1000))
                for symbol in self.symbols:
                    try:
                        await asyncio.wait_for(self.sample(symbol),timeout=35)
                        self.last_error=None
                    except asyncio.CancelledError:raise
                    except (ValueError,TypeError) as exc:
                        self.rejected+=1;self.last_error=f'{symbol}: {type(exc).__name__}: {str(exc)[:160]}'
                        LOG.warning('V5 sample rejected: %s',self.last_error)
                    except Exception as exc:
                        self.errors+=1;self.last_error=f'{symbol}: {type(exc).__name__} (details in Render logs)'
                        LOG.warning('V5 sample failed, live TiMi unaffected: %s',type(exc).__name__)
            except asyncio.CancelledError:raise
            except Exception as exc:
                self.errors+=1;self.last_error=f'loop: {type(exc).__name__}'
                LOG.exception('V5 sampler error; continuing')
                await asyncio.sleep(5)

    async def start(self):
        if self.backend=='disabled' or not self.symbols:return
        # Keep the existing live FastAPI startup unblocked by database connection.
        # The first scheduled sample (or status request) opens the DB lazily.
        self.task=asyncio.create_task(self.loop(),name='timi_ml_v5_sampler')

    async def stop(self):
        if self.task:
            self.task.cancel();await asyncio.gather(self.task,return_exceptions=True)
            self.task=None
        if self.pool:
            await self.pool.close();self.pool=None

    async def status(self):
        output={'schema':SCHEMA,'enabled':self.backend!='disabled' and bool(self.symbols),
                'backend':self.backend,'symbols':self.symbols,
                'durability':('external_postgres' if self.backend=='postgres' else
                              'sqlite_requires_actual_persistent_disk' if self.backend=='sqlite' else
                              'not_recording'),
                'sampled_this_process':self.sampled,'duplicates_this_process':self.duplicates,
                'rejected_this_process':self.rejected,'errors_this_process':self.errors,
                'last_sample_ms':self.last_sample_ms,'last_error':self.last_error,
                'read_only':True,'real_orders_enabled':False,
                'notes':['Only data observed after activation are recorded.',
                         '26 indicators reconstructed from source-confirmed CLOSED 1m candles.',
                         'Live flow is captured after close, not backfilled or synchronized to historical candles.',
                         'PostgreSQL primary key deduplicates multiple server instances.']}
        if self.backend=='disabled':
            output['setup_required']='Set TIMI_V5_DATABASE_URL in Render, and add asyncpg to requirements.txt'
            return output
        if self._counts_cache is None or time.monotonic()-self._counts_at>20:
            try:
                async with self._db_lock:
                    await self._ensure_connection()
                    if self.backend=='postgres':
                        async with self.pool.acquire() as conn:
                            rows=await conn.fetch('''SELECT symbol,COUNT(*) AS n,MAX(observed_ms) AS last_ts
                                               FROM timi_v5_observations GROUP BY symbol ORDER BY symbol''')
                            counts=[{'symbol':x['symbol'],'rows':x['n'],'last_snapshot_ms':x['last_ts']} for x in rows]
                    else:
                        def count():
                            with sqlite3.connect(self.sqlite_path,timeout=5) as conn:
                                return [{'symbol':s,'rows':n,'last_snapshot_ms':t} for s,n,t in
                                      conn.execute('''SELECT symbol,COUNT(*),MAX(observed_ms)
                                                      FROM timi_v5_observations GROUP BY symbol ORDER BY symbol''')]
                        counts=await asyncio.to_thread(count)
                self._counts_cache=counts;self._counts_at=time.monotonic()
            except Exception as exc:
                output['database_status']='error'
                output['last_error']=f'{type(exc).__name__} (see private Render logs)'
                return output
        output['database_status']='connected'
        output['stored_rows_by_symbol']=self._counts_cache
        return output


def install_v5_archive(app,*,get_candles,cached_pro,now_ms,allowed_symbols):
    """Wrap existing FastAPI lifespan; does not replace existing WebSocket tasks/routes."""
    archive=ArchiveV5(get_candles=get_candles,cached_pro=cached_pro,
                      now_ms=now_ms,allowed_symbols=allowed_symbols)
    previous=app.router.lifespan_context
    @asynccontextmanager
    async def wrapper(application):
        async with previous(application):
            await archive.start()
            try:yield
            finally:await archive.stop()
    app.router.lifespan_context=wrapper

    @app.get('/api/ml-v5/status')
    async def ml_v5_status():
        return await archive.status()
    return archive
