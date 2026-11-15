"""TiMi forward-only performance observations v1.0.

Evaluates only pre-existing, live, timestamped approved TiMi signals.
No orders, no historical reconstruction, no predictions or executed P&L.
Entry/exit *quote references* require nearby observed Spot trade and fresh top-of-book.
External PostgreSQL persistence is optional, sharing TIMI_V5_DATABASE_URL.
"""
from __future__ import annotations

import asyncio
import copy
import math
import os
import statistics
from collections import OrderedDict
from typing import Any

from fastapi import HTTPException

HORIZONS = {'1m': 60_000, '5m': 300_000}
MAX_OBSERVATIONS = 1200
QUOTE_MAX_AGE_MS = 2_500
OBSERVATION_GRACE_MS = 5_000
DDL = '''CREATE TABLE IF NOT EXISTS timi_forward_signals_v1 (
  id TEXT PRIMARY KEY,
  symbol VARCHAR(20) NOT NULL,
  observed_at_ms BIGINT NOT NULL,
  payload JSONB NOT NULL,
  updated_at TIMESTAMPTZ NOT NULL DEFAULT now()
)'''
INDEX_DDL = '''CREATE INDEX IF NOT EXISTS timi_forward_signals_v1_by_symbol_time
 ON timi_forward_signals_v1(symbol,observed_at_ms DESC)'''
UPSERT = '''INSERT INTO timi_forward_signals_v1(id,symbol,observed_at_ms,payload)
 VALUES($1,$2,$3,$4::jsonb)
 ON CONFLICT(id) DO UPDATE SET payload=EXCLUDED.payload,updated_at=now()'''


def valid_number(value: Any) -> float | None:
    try:
        v = float(value)
        return v if math.isfinite(v) and v > 0 else None
    except (ValueError, TypeError, OverflowError):
        return None


class ForwardTracker:
    def __init__(self, symbols, now_ms, pg_url=None, fee_bps=10.0, slippage_bps=2.0, enabled_symbols=None):
        self.symbols = tuple(dict.fromkeys(symbols))
        self.enabled_symbols = tuple(s for s in (enabled_symbols if enabled_symbols is not None else self.symbols) if s in self.symbols)
        self.now_ms = now_ms
        self.pg_url = (pg_url if pg_url is not None else
                       os.getenv('TIMI_V5_DATABASE_URL', '')).strip()
        self.fee_bps = self._cost_bps(fee_bps, 10.0)
        self.slippage_bps = self._cost_bps(slippage_bps, 2.0)
        self.cost_pct = (self.fee_bps + self.slippage_bps) * 2 / 100.0
        self.records = {s: OrderedDict() for s in self.symbols}
        self.pending = {s: {} for s in self.symbols}
        self.queue = asyncio.Queue(maxsize=1000)
        self.pool = None
        self.worker = None
        self.pg_error = None
        self.failed_writes = 0
        self.started = False

    @staticmethod
    def _cost_bps(value, fallback):
        try:
            f = float(value)
            return f if math.isfinite(f) and 0 <= f <= 100 else fallback
        except (ValueError, TypeError, OverflowError):
            return fallback

    def _insert(self, rec):
        symbol = rec['symbol']
        bucket = self.records[symbol]
        bucket[rec['id']] = rec
        if len(bucket) > MAX_OBSERVATIONS:
            oldest, _ = bucket.popitem(last=False)
            self.pending[symbol].pop(oldest, None)
        if (rec['entry']['status'] == 'pending' or
            any(x['status'] == 'pending' for x in rec['outcomes'].values())):
            self.pending[symbol][rec['id']] = rec
        else:
            self.pending[symbol].pop(rec['id'], None)

    def _persist(self, rec):
        if not self.pg_url:
            return
        try:
            self.queue.put_nowait(copy.deepcopy(rec))
        except asyncio.QueueFull:
            self.failed_writes += 1
            self.pg_error = 'database write queue full; observations may be lost'

    async def _connect(self):
        if not self.pg_url or self.pool is not None:
            return
        import asyncpg
        pool = await asyncio.wait_for(asyncpg.create_pool(
            dsn=self.pg_url, min_size=1, max_size=2,
            command_timeout=9, timeout=9), timeout=12)
        try:
            async with pool.acquire() as connection:
                await connection.execute(DDL)
                await connection.execute(INDEX_DDL)
        except Exception:
            await pool.close()
            raise
        self.pool = pool
        self.pg_error = None

    async def start(self):
        if self.started:
            return
        self.started = True
        if not self.pg_url:
            return
        try:
            await self._connect()
            async with self.pool.acquire() as connection:
                data = await connection.fetch('''SELECT payload::text AS item
                 FROM timi_forward_signals_v1 WHERE symbol=ANY($1::text[])
                 ORDER BY observed_at_ms DESC LIMIT 4000''', list(self.symbols))
            for row in reversed(data):
                import json
                rec = json.loads(row['item'])
                if rec.get('symbol') not in self.records or not isinstance(rec.get('outcomes'), dict):
                    continue
                dirty = self._finish_unobserved(rec, 'server_restarted')
                self._insert(rec)
                if dirty:
                    self._persist(rec)
        except Exception as e:
            self.pg_error = f'{type(e).__name__}: PostgreSQL unavailable'
        self.worker = asyncio.create_task(self._write_worker())

    async def _write_worker(self):
        import json
        while True:
            record = await self.queue.get()
            try:
                for attempt in range(3):
                    try:
                        if self.pool is None:
                            await self._connect()
                        async with self.pool.acquire() as connection:
                            await connection.execute(
                                UPSERT, record['id'], record['symbol'],
                                record['observed_at_ms'], json.dumps(record, separators=(',', ':')))
                        self.pg_error = None
                        break
                    except asyncio.CancelledError:
                        raise
                    except Exception as e:
                        self.pg_error = f'{type(e).__name__}: write unavailable'
                        if self.pool is not None:
                            try:
                                await self.pool.close()
                            except Exception:
                                pass
                            self.pool = None
                        if attempt < 2:
                            await asyncio.sleep(2)
                        else:
                            self.failed_writes += 1
            finally:
                self.queue.task_done()

    async def stop(self):
        if self.worker:
            try:
                await asyncio.wait_for(self.queue.join(), timeout=5)
            except asyncio.TimeoutError:
                pass
            self.worker.cancel()
            await asyncio.gather(self.worker, return_exceptions=True)
            self.worker = None
        if self.pool:
            await self.pool.close()
            self.pool = None
        self.started = False

    def on_signal(self, observation):
        """Accept only the existing audited, non-BEKLE TiMi decision."""
        s = observation.get('symbol')
        direction = observation.get('direction')
        if s not in self.enabled_symbols or direction not in ('LONG', 'SHORT'):
            return None
        ts = observation.get('observed_at_ms')
        if type(ts) is not int or ts <= 0:
            return None
        if valid_number(observation.get('entry_reference_price')) is None:
            return None
        if not observation.get('outcomes'):
            return None
        key = f'{s}:{ts//60000}'
        if key in self.records[s]:
            return self.records[s][key]
        slot = (ts//60_000 + 1) * 60_000
        rec = {
            'id': key, 'symbol': s, 'direction': direction,
            'observed_at_ms': ts, 'score': observation.get('score'),
            'closed_candle_at_ms': observation.get('closed_candle_at_ms'),
            'planned_entry_ms': slot,
            'entry': {'status': 'pending', 'reference_quote': None,
                      'observed_at_ms': None},
            'outcomes': {name: {'status': 'pending', 'target_ms': None,
                                'reference_exit_quote': None,
                                'gross_pct': None, 'net_pct': None}
                         for name in HORIZONS},
        }
        self._insert(rec)
        self._persist(rec)
        return rec

    @staticmethod
    def _book_quote(market, now):
        age = now - market.last_book_ms if market.last_book_ms is not None else None
        if not market.book_valid or age is None or not 0 <= age <= QUOTE_MAX_AGE_MS:
            return None
        try:
            bid, ask = valid_number(market.bids[0][0]), valid_number(market.asks[0][0])
            if not bid or not ask or bid >= ask:
                return None
            return {'bid': bid, 'ask': ask, 'spread_bps': round((ask-bid)/((bid+ask)/2)*10000, 5),
                    'book_age_ms': age}
        except (IndexError, KeyError, TypeError):
            return None

    @staticmethod
    def _finish_unobserved(rec, reason):
        dirty = False
        if rec['entry']['status'] == 'pending':
            rec['entry']['status'] = 'missing_data'
            rec['entry']['reason'] = reason
            dirty = True
        for outcome in rec['outcomes'].values():
            if outcome['status'] == 'pending':
                outcome['status'] = 'missing_data'
                outcome['reason'] = reason
                dirty = True
        return dirty

    def on_stream_reset(self):
        """No extrapolation across disconnected market-data sessions."""
        for symbol, rows in self.pending.items():
            for rec in list(rows.values()):
                if self._finish_unobserved(rec, 'upstream_reset'):
                    self._persist(rec)
            rows.clear()

    def on_trade(self, symbol, price, observed_at_ms, market):
        if symbol not in self.pending or valid_number(price) is None:
            return
        if type(observed_at_ms) is not int:
            return
        rows = self.pending[symbol]
        if not rows:
            return
        for rec in tuple(rows.values()):
            dirty = False
            ent = rec['entry']
            if ent['status'] == 'pending':
                target = rec['planned_entry_ms']
                if observed_at_ms < target:
                    continue
                if observed_at_ms - target > OBSERVATION_GRACE_MS:
                    dirty = self._finish_unobserved(rec, 'entry_window_missed')
                else:
                    quote = self._book_quote(market, observed_at_ms)
                    if quote is not None:
                        entry = quote['ask'] if rec['direction'] == 'LONG' else quote['bid']
                        ent.update(status='measured', reference_quote=entry,
                                   observed_at_ms=observed_at_ms,
                                   quote_spread_bps=quote['spread_bps'],
                                   book_age_ms=quote['book_age_ms'],
                                   entry_delay_ms=observed_at_ms-target)
                        for name, ms in HORIZONS.items():
                            rec['outcomes'][name]['target_ms'] = observed_at_ms + ms
                        dirty = True
            if ent['status'] == 'measured':
                for name, outcome in rec['outcomes'].items():
                    if outcome['status'] != 'pending' or observed_at_ms < outcome['target_ms']:
                        continue
                    lag = observed_at_ms-outcome['target_ms']
                    if lag > OBSERVATION_GRACE_MS:
                        outcome.update(status='missing_data', reason='exit_window_missed')
                        dirty = True
                        continue
                    quote = self._book_quote(market, observed_at_ms)
                    if quote is None:
                        continue
                    exit_ref = quote['bid'] if rec['direction'] == 'LONG' else quote['ask']
                    signed = (1 if rec['direction'] == 'LONG' else -1)
                    gross = signed * (exit_ref/ent['reference_quote'] - 1)*100
                    net = gross-self.cost_pct
                    outcome.update(status='measured', observed_at_ms=observed_at_ms,
                                   reference_exit_quote=exit_ref,
                                   exit_delay_ms=lag,
                                   quote_spread_bps=quote['spread_bps'],
                                   gross_pct=round(gross, 6),
                                   cost_pct=round(self.cost_pct, 6),
                                   net_pct=round(net, 6),
                                   net_positive=net > 0)
                    dirty = True
            if dirty:
                if all(x['status'] != 'pending' for x in rec['outcomes'].values()):
                    rows.pop(rec['id'], None)
                self._persist(rec)

    def summary(self, symbol, limit=20):
        if symbol not in self.records:
            raise HTTPException(400, 'Unsupported symbol')
        if not 1 <= limit <= 100:
            raise HTTPException(400, 'limit must be 1..100')
        rows = list(self.records.get(symbol, {}).values())
        metrics = {}
        for name in HORIZONS:
            outcomes = [r['outcomes'][name] for r in rows]
            matched = [x['net_pct'] for x in outcomes if x['status'] == 'measured']
            n = len(matched)
            correct = sum(x > 0 for x in matched)
            gross_hits = sum(x['gross_pct'] > 0 for x in outcomes if x['status'] == 'measured')
            side_results = {}
            for side in ('LONG', 'SHORT'):
                side_vals = [r['outcomes'][name]['net_pct'] for r in rows
                             if r['direction'] == side and r['outcomes'][name]['status'] == 'measured']
                side_results[side] = {'measured': len(side_vals),
                                      'net_positive': sum(x > 0 for x in side_vals),
                                      'net_positive_pct': round(100*sum(x > 0 for x in side_vals)/len(side_vals), 2) if side_vals else None}
            metrics[name] = {
                'measured': n,
                'net_positive': correct, 'net_not_positive': n-correct,
                'directional_hit_rate_pct': round(100*gross_hits/n, 2) if n else None,
                'by_direction': side_results,
                'pending': sum(x['status'] == 'pending' for x in outcomes),
                'missing': sum(x['status'] == 'missing_data' for x in outcomes),
                'net_positive_pct': round(100*sum(x > 0 for x in matched)/n, 2) if n else None,
                'mean_net_pct': round(statistics.mean(matched), 6) if n else None,
                'median_net_pct': round(statistics.median(matched), 6) if n else None,
                'insufficient_sample': n < 100,
            }
        return {
            'version': '1.0.0', 'symbol': symbol,
            'enabled': symbol in self.enabled_symbols, 'read_only': True,
            'storage': ('postgresql' if self.pg_url and self.pool is not None and not self.pg_error
                        else 'postgresql_unavailable_memory_fallback' if self.pg_url
                        else 'memory_only_ephemeral'),
            'storage_error': self.pg_error, 'failed_writes': self.failed_writes,
            'sampled_long_short': len(rows),
            'horizons': metrics,
            'observations': copy.deepcopy(list(reversed(rows))[:limit]),
            'fee_bps_per_side': self.fee_bps,
            'slippage_bps_per_side': self.slippage_bps,
            'roundtrip_fee_slippage_pct': round(self.cost_pct, 6),
            'method': ('One sampled approved live signal per UTC minute; first observed Spot '
                       'top-of-book quote within 5 seconds of the NEXT 1m opening; '
                       'forward 1m/5m exit quotes within 5 seconds after each target. '
                       'LONG entry ask/exit bid; SHORT entry bid/exit ask. '
                       'Fees and slippage subtracted both sides. Missing windows excluded.'),
            'limitations': ('Hypothetical quote-based directional study, NOT actual fills or '
                            'realized/tradable P&L. SHORT on Spot is not an executable short; '
                            'borrow/funding/market impact/taxes excluded. Quote depth 20 only. '
                            'Overlapping horizon samples are correlated; no reconnect backfill. Results limited to retained observations '
                            '(latest 1200 per symbol); below 100 observations is preliminary. '
                            'PostgreSQL optional; in-memory data disappears on restart.'),
        }

    def install_routes(self, app):
        @app.get('/api/forward-performance/{symbol}')
        async def forward_performance(symbol: str, limit: int = 20):
            return self.summary(symbol.upper(), limit)
