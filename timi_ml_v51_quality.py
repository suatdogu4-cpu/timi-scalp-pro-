"""TiMi ML v5.1 - read-only aggregate archive data-quality statistics.

Developer: Suat DOGU. Designed for existing v5 ArchiveV5 and its JSONB schema.
Never sends orders, does ML inference, or reveals archive payloads/DB credentials.
"""
from __future__ import annotations

import asyncio
import math
import sqlite3
import time
from datetime import datetime, timedelta, timezone

from fastapi import HTTPException

FLAGS = (
    'closed_candle_verified',
    'pro_analysis_matches_candle',
    'flow_fresh',
    'snapshot_time_fresh',
    'mtf_all_four_fresh',
    'ml_eligible',
)
MIN_SAMPLE = 10_080  # descriptive research observation threshold; NOT model approval
MIN_DAYS = 7
ONE_MINUTE = 60_000


def _get_status(status):
    if status.get('backend') == 'disabled':
        return 'disabled'
    return 'connected' if status.get('database_status') == 'connected' else 'error'


def _as_bool(v):
    return v is True or v == 'true'


def summarize_rows(rows, *, symbol, now_ms=None):
    """Pure reference aggregation used for SQLite/testing. Payload dicts are never returned."""
    now_ms = int(now_ms if now_ms is not None else time.time() * 1000)
    rows = sorted(rows, key=lambda r: int(r['candle_open_ms']))
    count = len(rows)
    flagged = {x: 0 for x in FLAGS}
    gaps = 0
    missing = 0
    last_open = None
    eligible = 0
    last_observed = None
    days = {}
    cutoff = (datetime.fromtimestamp(now_ms / 1000, timezone.utc).date() - timedelta(days=13))
    for row in rows:
        t = int(row['candle_open_ms'])
        ob = int(row['observed_ms'])
        payload = row.get('payload', {})
        if isinstance(payload, str):
            import json
            payload = json.loads(payload)
        quality = payload.get('quality') or {}
        for flag in FLAGS:
            flagged[flag] += int(_as_bool(quality.get(flag)))
        valid = _as_bool(quality.get('ml_eligible'))
        eligible += int(valid)
        if last_open is not None and t > last_open + ONE_MINUTE:
            slots = (t - last_open) // ONE_MINUTE - 1
            if slots > 0:
                gaps += 1
                missing += slots
        last_open = t
        if last_observed is None or ob > last_observed:
            last_observed = ob
        day = datetime.fromtimestamp(t / 1000, timezone.utc).date()
        if cutoff <= day <= datetime.fromtimestamp(now_ms / 1000, timezone.utc).date():
            d = days.setdefault(str(day), {'day_utc': str(day), 'records': 0, 'eligible': 0})
            d['records'] += 1
            d['eligible'] += int(valid)
    return _finalize(count, eligible, flagged, missing, gaps, last_observed,
                     list(sorted(days.values(), key=lambda d: d['day_utc'])), now_ms)


def _finalize(total, eligible, flags, missing, gap_events, last_observed, daily, now_ms):
    total = int(total)
    eligible = int(eligible)
    day_count = sum(x['eligible'] > 0 for x in daily)
    return {
        'total_records': total,
        'eligible_records': eligible,
        'ineligible_records': total - eligible,
        'eligible_pct': round(eligible * 100 / total, 2) if total else None,
        'quality_pass_counts': {f: int(flags.get(f, 0)) for f in FLAGS},
        'missing_minute_slots': int(missing),
        'gap_events': int(gap_events),
        'last_observed_ms': int(last_observed) if last_observed is not None else None,
        'last_record_age_minutes': (round(max(0, now_ms - int(last_observed)) / ONE_MINUTE, 2)
                                    if last_observed is not None else None),
        'daily': daily,
        'readiness': {
            'descriptive_minimum_eligible_records': MIN_SAMPLE,
            'minimum_distinct_utc_days': MIN_DAYS,
            'distinct_utc_days_with_eligible_records_in_recent_14': day_count,
            'at_least_7_distinct_utc_days_in_recent_14': day_count >= MIN_DAYS,
            'progress_pct': round(min(eligible / MIN_SAMPLE, day_count / MIN_DAYS, 1) * 100, 2),
            'descriptive_threshold_reached': eligible >= MIN_SAMPLE and day_count >= MIN_DAYS,
            'note': 'Data quantity illustration only; NOT ML training approval or profitability proof.'
        }
    }


async def _query_postgres(archive, symbol, now_ms):
    # SQL aggregates: avoid transferring private raw payloads to the public API.
    parts = ',\n'.join(
        f"COUNT(*) FILTER (WHERE payload #>> '{{quality,{f}}}' = 'true') AS {f}"
        for f in FLAGS)
    async with archive.pool.acquire() as conn:
        totals = await conn.fetchrow(f'''
            SELECT COUNT(*) AS total, MAX(observed_ms) AS last_observed,
                   {parts}
            FROM timi_v5_observations WHERE symbol=$1
        ''', symbol)
        gap = await conn.fetchrow('''
            WITH lagged AS (
              SELECT candle_open_ms, LAG(candle_open_ms) OVER (ORDER BY candle_open_ms) AS prev
              FROM timi_v5_observations WHERE symbol=$1
            ), diffs AS (
              SELECT GREATEST(0, (candle_open_ms - prev) / 60000 - 1) AS missing
              FROM lagged WHERE prev IS NOT NULL
            )
            SELECT COALESCE(SUM(missing),0) AS missing,
                   COUNT(*) FILTER (WHERE missing>0) AS gaps FROM diffs
        ''', symbol)
        cutoff_date = datetime.fromtimestamp(now_ms / 1000, timezone.utc).date() - timedelta(days=13)
        cutoff_ms = int(datetime(cutoff_date.year, cutoff_date.month, cutoff_date.day,
                                 tzinfo=timezone.utc).timestamp() * 1000)
        dayrows = await conn.fetch('''
            SELECT to_char(to_timestamp(candle_open_ms / 1000.0) AT TIME ZONE 'UTC',
                           'YYYY-MM-DD') AS day_utc,
                   COUNT(*) AS records,
                   COUNT(*) FILTER (WHERE payload #>> '{quality,ml_eligible}' = 'true') AS eligible
            FROM timi_v5_observations WHERE symbol=$1 AND candle_open_ms >= $2
            GROUP BY 1 ORDER BY 1
        ''', symbol, cutoff_ms)
    daily = [dict(day_utc=r['day_utc'], records=int(r['records']), eligible=int(r['eligible']))
             for r in dayrows]
    flags = {f: totals[f] for f in FLAGS}
    return _finalize(totals['total'], flags['ml_eligible'], flags, gap['missing'], gap['gaps'],
                     totals['last_observed'], daily, now_ms)


async def _query_sqlite(archive, symbol, now_ms):
    def load():
        with sqlite3.connect(archive.sqlite_path, timeout=8) as conn:
            cur = conn.execute('''SELECT candle_open_ms,observed_ms,payload
                                  FROM timi_v5_observations WHERE symbol=? ORDER BY candle_open_ms''',
                               (symbol,))
            return [dict(candle_open_ms=x, observed_ms=y, payload=z) for x,y,z in cur]
    rows = await asyncio.to_thread(load)
    return summarize_rows(rows, symbol=symbol, now_ms=now_ms)


def install_v51_quality(app, archive):
    """Add one non-sensitive read-only endpoint; leave existing archive lifecycles untouched."""
    cache = {}
    lock = asyncio.Lock()

    @app.get('/api/ml-v51/quality')
    async def ml_v51_quality(symbol: str = 'BTCUSDT'):
        symbol = symbol.strip().upper()
        if symbol not in archive.symbols:
            raise HTTPException(400, 'Symbol not enabled for ML research archive')
        now_ms = int(time.time() * 1000)
        output = {
            'version': '5.1',
            'schema': 'timi-ml-v51-quality-v1',
            'symbol': symbol,
            'tracked_symbols': list(archive.symbols),
            'backend': archive.backend,
            'database_status': 'disabled' if archive.backend == 'disabled' else 'unknown',
            'process_observations': {
                'sampled': archive.sampled,
                'duplicates': archive.duplicates,
                'rejected': archive.rejected,
                'errors': archive.errors,
            },
            'research_only': True,
            'live_ml_enabled': False,
            'real_orders_enabled': False,
            'raw_payloads_returned': False,
        }
        if archive.backend == 'disabled':
            output['message'] = 'TIMI_V5_DATABASE_URL configured only in Render; archive disabled.'
            return output
        # Cache aggregates briefly; never hold up WebSocket handlers or sampling.
        cached = cache.get(symbol)
        if cached and now_ms - cached[0] < 25_000 and cached[2] == archive.sampled:
            output.update(database_status='connected', statistics=cached[1])
            return output
        async with lock:
            try:
                async with archive._db_lock:
                    await asyncio.wait_for(archive._ensure_connection(), timeout=12)
                if archive.backend == 'postgres':
                    stats = await asyncio.wait_for(_query_postgres(archive, symbol, now_ms), timeout=16)
                else:
                    stats = await asyncio.wait_for(_query_sqlite(archive, symbol, now_ms), timeout=16)
            except Exception as exc:
                output['database_status'] = 'error'
                output['message'] = 'Aggregate query unavailable; inspect private Render logs.'
                output['error_type'] = type(exc).__name__  # no secrets or exception messages
                return output
            cache[symbol] = (now_ms, stats, archive.sampled)
        output.update(database_status='connected', statistics=stats)
        return output
    return ml_v51_quality
