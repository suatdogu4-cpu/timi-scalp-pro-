"""TiMi Order Flow PRO 3.0, read-only and in-memory (no order or signal archive).

Binance Spot public @trade flow + top-20 partial-depth snapshots, delivered
by the existing TiMi relay. No private keys, persistence or trading actions.
"""
from collections import deque
from dataclasses import dataclass, field
import math
from fastapi import HTTPException, Query

# Bins are VISUAL GROUPS, not exchange price ticks or a full-depth footprint.
STEPS = {
    'BTCUSDT': 5.0, 'ETHUSDT': 0.5, 'SOLUSDT': 0.1,
    'BNBUSDT': 0.1, 'XRPUSDT': 0.001, 'DOGEUSDT': 0.0001,
    'ADAUSDT': 0.001, 'AVAXUSDT': 0.05,
}
WINDOWS = (15, 60, 300)
MAX_BUCKETS = 310
MAX_BINS_PER_SEC = 160


def finite(value):
    try:
        n = float(value)
        return n if math.isfinite(n) else None
    except (ValueError, TypeError, OverflowError):
        return None


@dataclass
class TapeSecond:
    sec: int
    buy: float = 0.0
    sell: float = 0.0
    count: int = 0
    first: float = 0.0
    last: float = 0.0
    # bin index -> [buy USDT, sell USDT, buy count, sell count]
    levels: dict = field(default_factory=dict)
    unbinned_usdt: float = 0.0
    unbinned_count: int = 0


@dataclass
class BookObservation:
    ms: int
    bid_plus_usdt: float = 0.0
    bid_minus_usdt: float = 0.0
    ask_plus_usdt: float = 0.0
    ask_minus_usdt: float = 0.0
    matched_levels: int = 0
    # True only if a successive top-20 snapshot was comparable.
    comparable: bool = False


class OrderFlow23:
    """Maximum 310 seconds per symbol, fully RAM-only.

    Book changes are quantities at COMMON prices between two partial-depth
    snapshots. Vanishing outside the top-20 range is never counted as removal.
    Neither additions nor removals prove identities, executions or spoofing.
    """

    def __init__(self, symbols, clock):
        self.clock = clock
        self.symbols = tuple(x for x in symbols if x in STEPS)
        self.trades = {s: deque(maxlen=MAX_BUCKETS) for s in self.symbols}
        self.books = {s: deque(maxlen=MAX_BUCKETS) for s in self.symbols}
        self.prev_book = {s: None for s in self.symbols}
        self.prev_book_ms = {s: 0 for s in self.symbols}
        self.last_book_ms = {s: 0 for s in self.symbols}
        self.last_trade_ms = {s: 0 for s in self.symbols}

    def reset_all(self):
        # Reset on websocket reconnection. Never bridge a disconnection.
        for symbol in self.symbols:
            self.trades[symbol].clear()
            self.books[symbol].clear()
            self.prev_book[symbol] = None
            self.prev_book_ms[symbol] = 0
            self.last_book_ms[symbol] = 0
            self.last_trade_ms[symbol] = 0

    def invalidate_book(self, symbol):
        if symbol in self.books:
            self.books[symbol].clear()
            self.prev_book[symbol] = None
            self.prev_book_ms[symbol] = 0
            self.last_book_ms[symbol] = 0

    def record_trade(self, symbol, side, qty, price, received_ms):
        if symbol not in self.trades or side not in ('buy', 'sell'):
            return
        p, q, t = finite(price), finite(qty), finite(received_ms)
        if p is None or q is None or t is None or p <= 0 or q <= 0 or t < 1e12:
            return
        sec = int(t) // 1000
        rows = self.trades[symbol]
        if rows and sec < rows[-1].sec:
            return
        if not rows or rows[-1].sec != sec:
            rows.append(TapeSecond(sec=sec, first=p, last=p))
        bar = rows[-1]
        bar.last = p
        bar.count += 1
        notional = p * q
        if side == 'buy':
            bar.buy += notional
        else:
            bar.sell += notional
        # Quantize by configured display step; not a tick-by-tick footprint.
        step = STEPS[symbol]
        bin_index = math.floor(p / step + 1e-9)
        if bin_index not in bar.levels and len(bar.levels) >= MAX_BINS_PER_SEC:
            bar.unbinned_usdt += notional
            bar.unbinned_count += 1
        else:
            values = bar.levels.setdefault(bin_index, [0., 0., 0, 0])
            side_index = 0 if side == 'buy' else 1
            values[side_index] += notional
            values[side_index + 2] += 1
        self.last_trade_ms[symbol] = int(t)

    def record_book(self, symbol, bids, asks, received_ms, update_id=None):
        if symbol not in self.books or not isinstance(bids, (list, tuple)) or not isinstance(asks, (list, tuple)):
            return
        t = finite(received_ms)
        if t is None or t < 1e12:
            return
        # Sample at ~1 Hz rather than processing every ~100ms depth snapshot.
        if self.last_book_ms[symbol] and t - self.last_book_ms[symbol] < 1000:
            return
        book = {}
        for key, levels in (('bid', bids), ('ask', asks)):
            side = {}
            for level in levels[:20]:
                if not isinstance(level, (tuple, list)) or len(level) < 2:
                    continue
                price, qty = finite(level[0]), finite(level[1])
                if price is not None and qty is not None and price > 0 and qty >= 0:
                    side[price] = qty
            if not side:
                self.invalidate_book(symbol)
                return
            book[key] = side
        obs = BookObservation(ms=int(t))
        previous = self.prev_book[symbol]
        if previous is not None and 0 < t - self.prev_book_ms[symbol] <= 5000:
            for side in ('bid', 'ask'):
                old, new = previous[side], book[side]
                for price in old.keys() & new.keys():
                    obs.matched_levels += 1
                    difference = price * (new[price] - old[price])
                    if difference > 0:
                        if side == 'bid': obs.bid_plus_usdt += difference
                        else: obs.ask_plus_usdt += difference
                    elif difference < 0:
                        if side == 'bid': obs.bid_minus_usdt += -difference
                        else: obs.ask_minus_usdt += -difference
            obs.comparable = obs.matched_levels > 0
        self.books[symbol].append(obs)
        self.prev_book[symbol] = book
        self.prev_book_ms[symbol] = int(t)
        self.last_book_ms[symbol] = int(t)

    def snapshot(self, symbol, window, market, connected):
        if symbol not in self.trades:
            raise HTTPException(status_code=400, detail='Unsupported symbol')
        if window not in WINDOWS:
            raise HTTPException(status_code=400, detail='Window must be 15, 60 or 300 seconds')
        now = int(self.clock())
        last_trade = self.last_trade_ms[symbol]
        last_book = self.last_book_ms[symbol]
        trade_age = now - last_trade if last_trade else None
        book_age = now - last_book if last_book else None
        trade_ok = bool(connected and market.last_trade_ms and trade_age is not None and 0 <= trade_age <= 5000)
        book_ok = bool(connected and market.book_valid and market.last_book_ms and book_age is not None and 0 <= book_age <= 5000)
        now_sec = now // 1000
        rows = [x for x in self.trades[symbol] if 0 <= now_sec-x.sec < window]
        all_rows = [x for x in self.trades[symbol] if x.sec <= now_sec]
        observed_sec = min(window, now_sec-all_rows[0].sec+1) if all_rows else 0
        complete_window = observed_sec >= window and trade_ok
        buy = sum(x.buy for x in rows)
        sell = sum(x.sell for x in rows)
        trades_count = sum(x.count for x in rows)
        totals = {}
        for bar in rows:
            for index, item in bar.levels.items():
                value = totals.setdefault(index, [0., 0., 0, 0])
                for i in range(4):
                    value[i] += item[i]
        # Bound response to the 18 busiest price groups, then show price-sorted.
        top = sorted(totals, key=lambda k: totals[k][0]+totals[k][1], reverse=True)[:18]
        step = STEPS[symbol]
        levels = [
            {'lower_price': round(k*step, 8), 'upper_price': round((k+1)*step, 8),
             'buy_usdt': round(totals[k][0], 2), 'sell_usdt': round(totals[k][1], 2),
             'delta_usdt': round(totals[k][0]-totals[k][1], 2),
             'trade_count': totals[k][2]+totals[k][3]}
            for k in sorted(top, reverse=True)
        ]
        observations = [x for x in self.books[symbol] if 0 <= now-x.ms < window*1000 and x.comparable]
        book_count = len(observations)
        changes = {
            'sample_pairs': book_count,
            'matched_price_level_comparisons': sum(x.matched_levels for x in observations),
            'bid_increase_usdt': round(sum(x.bid_plus_usdt for x in observations), 2),
            'bid_decrease_usdt': round(sum(x.bid_minus_usdt for x in observations), 2),
            'ask_increase_usdt': round(sum(x.ask_plus_usdt for x in observations), 2),
            'ask_decrease_usdt': round(sum(x.ask_minus_usdt for x in observations), 2),
        }
        # A guarded, non-tradable price-vs-aggressor divergence candidate.
        imbalance = (buy-sell)/(buy+sell)*100 if buy+sell>0 else None
        change_pct = (rows[-1].last/rows[0].first-1)*100 if rows and rows[0].first>0 else None
        divergence = None
        if (complete_window and trades_count >= 20 and buy+sell >= 5000 and
            imbalance is not None and change_pct is not None and
            abs(imbalance) >= 20 and abs(change_pct) >= 0.04 and
            imbalance * change_pct < 0):
            divergence = ('FİYAT DÜŞÜYOR · AGRESİF ALIŞ BASKIN' if imbalance>0
                          else 'FİYAT YÜKSELİYOR · AGRESİF SATIŞ BASKIN')
        overflow_count = sum(x.unbinned_count for x in rows)
        comparison = []
        for seconds in WINDOWS:
            sample = [x for x in all_rows if 0 <= now_sec-x.sec < seconds]
            amount_buy = sum(x.buy for x in sample)
            amount_sell = sum(x.sell for x in sample)
            comparison.append({
                'window_sec': seconds,
                'complete': bool(trade_ok and observed_sec >= seconds),
                'trade_count': sum(x.count for x in sample),
                'buy_usdt': round(amount_buy, 2),
                'sell_usdt': round(amount_sell, 2),
                'delta_usdt': round(amount_buy-amount_sell, 2),
            })
        return {
            'version': '3.0.0', 'read_only': True, 'ephemeral': True,
            'symbol': symbol, 'time_ms': now, 'window_sec': window,
            'status': {'connected': bool(connected), 'trade_fresh': trade_ok,
                       'book_fresh': book_ok, 'trade_age_ms': trade_age,
                       'book_age_ms': book_age},
            'window': {'observed_elapsed_sec': observed_sec, 'complete': complete_window,
                       'trade_count': trades_count, 'buy_usdt': round(buy, 2),
                       'sell_usdt': round(sell, 2), 'delta_usdt': round(buy-sell, 2),
                       'delta_pct': round(imbalance, 2) if imbalance is not None else None,
                       'price_change_pct': round(change_pct, 4) if change_pct is not None else None,
                       'unbinned_usdt': round(sum(x.unbinned_usdt for x in rows), 2),
                       'unbinned_trade_count': overflow_count},
            'price_group_step': step, 'price_levels': levels,
            'window_comparison': comparison,
            'book_changes': changes if book_ok else None,
            'divergence_candidate': divergence,
            'limits': ('Trade bins are sampled from this TiMi server public Binance Spot @trade stream; '
                       'group widths are NOT Binance tick sizes and selected levels are a top-volume subset. '
                       'Book changes compare COMMON prices in consecutive partial top-20 snapshots '
                       'at ~1Hz; disappearance outside top-20 is excluded, and quantity changes '
                       'cannot distinguish cancels, executions, spoofing or order identity. '
                       'Data are in RAM for up to 310 seconds and reset at restart/reconnect. '
                       'No stored signals, orders, full historic depth, or liquidation feed.')
        }


def install_v23(app, markets, status, clock):
    """Registers ONE public, read-only GET endpoint; no background tasks."""
    recorder = OrderFlow23(markets.keys(), clock)

    @app.get('/api/v3/orderflow/{symbol}')
    async def v23_orderflow(symbol: str, window: int = Query(60, ge=15, le=300)):
        return recorder.snapshot(symbol.upper(), window, markets[symbol.upper()]
                                 if symbol.upper() in markets else None,
                                 bool(status.get('connected')))

    return recorder
