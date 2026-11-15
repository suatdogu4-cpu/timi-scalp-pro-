"""Deterministic, offline tests for TiMi v3.0 read-only order-flow primitives."""
import asyncio
import unittest
from types import SimpleNamespace

from fastapi import FastAPI, HTTPException
from timi_v23_orderflow import OrderFlow23, install_v23, WINDOWS


class Clock:
    def __init__(self):
        self.now = 1_800_000_000_000

    def __call__(self):
        return self.now


class TestOrderFlow23(unittest.TestCase):
    def setUp(self):
        self.clock = Clock()
        self.symbol = 'BTCUSDT'
        self.engine = OrderFlow23([self.symbol], self.clock)
        self.market = SimpleNamespace(last_trade_ms=None, last_book_ms=None, book_valid=True)

    def trade(self, side, amount, price=63000, advance=0):
        self.clock.now += advance
        self.engine.record_trade(self.symbol, side, amount, price, self.clock())
        self.market.last_trade_ms = self.clock()

    def depth(self, bid_qty=2, ask_qty=3, advance=1200):
        self.clock.now += advance
        self.engine.record_book(self.symbol, [[63000, bid_qty], [62995, 1]],
                                [[63005, ask_qty], [63010, 1]], self.clock())
        self.market.last_book_ms = self.clock()

    def result(self, window=15, connected=True):
        return self.engine.snapshot(self.symbol, window, self.market, connected)

    def test_trade_delta_and_grouping(self):
        self.trade('buy', 1, 63000)
        self.trade('sell', 0.2, 63002)
        self.depth(advance=1000)
        self.depth(bid_qty=2.5, ask_qty=2.0, advance=1200)
        r = self.result()
        self.assertEqual(r['window']['trade_count'], 2)
        self.assertAlmostEqual(r['window']['delta_usdt'], 63000-12600.4, places=2)
        self.assertEqual(len(r['price_levels']), 1)
        self.assertAlmostEqual(r['price_levels'][0]['buy_usdt'], 63000)
        self.assertAlmostEqual(r['book_changes']['bid_increase_usdt'], 31500)
        self.assertAlmostEqual(r['book_changes']['ask_decrease_usdt'], 63005)
        self.assertEqual([x['window_sec'] for x in r['window_comparison']], [15, 60, 300])
        self.assertEqual(r['window_comparison'][0]['trade_count'], 2)

    def test_price_levels_leaving_top20_are_not_cancellations(self):
        self.trade('buy', 0.01)
        self.depth(advance=1000)
        self.clock.now += 1200
        self.engine.record_book(self.symbol, [[62995, 2], [62990, 4]],
                                [[63005, 3], [63010, 1]], self.clock())
        self.market.last_book_ms = self.clock()
        change = self.result()['book_changes']
        self.assertAlmostEqual(change['bid_decrease_usdt'], 0)
        self.assertAlmostEqual(change['bid_increase_usdt'], 62995)

    def test_disconnect_and_invalid_snapshot(self):
        self.trade('buy', 0.01)
        self.depth(advance=1500)
        self.depth(advance=1500)
        self.assertFalse(self.result(connected=False)['status']['trade_fresh'])
        self.engine.invalidate_book(self.symbol)
        self.assertIsNone(self.result()['book_changes'])
        self.engine.reset_all()
        self.assertEqual(self.result()['window']['trade_count'], 0)
        self.assertFalse(self.result()['status']['trade_fresh'])

    def test_window_bounds_and_stale(self):
        self.trade('buy', 0.5)
        for window in WINDOWS:
            self.assertFalse(self.result(window)['window']['complete'])
        with self.assertRaises(HTTPException):
            self.result(45)
        self.clock.now += 6500
        self.assertFalse(self.result()['status']['trade_fresh'])

    def test_divergence_candidate_requires_full_window(self):
        self.trade('buy', 0.2, price=63150)
        for i in range(1, 16):
            # One buy and one small sell each second, while price declines.
            self.clock.now += 1000
            self.trade('buy', 0.4, price=63150-5*i)
            self.trade('sell', 0.02, price=63150-5*i)
        self.depth(advance=1000)
        self.assertTrue(self.result()['window']['complete'])
        self.assertIn('FİYAT DÜŞÜYOR', self.result()['divergence_candidate'])
        self.assertIsNone(self.result(300)['divergence_candidate'])

    def test_per_second_bins_bounded(self):
        self.trade('buy', 0.1, price=63000)
        for i in range(180):
            self.trade('buy', 0.01, price=63010+5*i)
        r = self.result()
        self.assertEqual(r['window']['trade_count'], 181)
        self.assertGreater(r['window']['unbinned_trade_count'], 0)
        self.assertLessEqual(len(self.engine.trades[self.symbol][-1].levels), 160)

    def test_one_public_get_route_only(self):
        app = FastAPI()
        installed = install_v23(app, {self.symbol:self.market}, {'connected':True}, self.clock)
        endpoints = [r.path for r in app.routes if hasattr(r,'path')]
        self.assertIn('/api/v3/orderflow/{symbol}', endpoints)
        self.assertFalse(any('/order' in p and p != '/api/v3/orderflow/{symbol}' for p in endpoints))
        self.assertIsInstance(installed, OrderFlow23)


if __name__ == '__main__':
    unittest.main()
