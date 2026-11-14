"""Offline, deterministic v4.1 guardrails. No Binance calls or trading."""
from __future__ import annotations
import unittest

import numpy as np
import pandas as pd

from ml_features_v41 import make_v41_features
from train_model_v41 import (
    cooldown_predictions, regime_mask, select_policy_v41, fit_v41, score_policy,
    REGIMES, SCORE_CUTS, MARGIN_CUTS,
)
from train_model_v4 import triple_barrier, folds


class SafetyV41(unittest.TestCase):
    def test_cooldown_keeps_nonoverlapping_signals(self):
        p = np.tile(np.array([[0.90, 0.04, 0.06]]), (14, 1))
        idx = np.arange(100, 114)
        pred = cooldown_predictions(p, idx, np.zeros(len(idx)),
                                    horizon=5, score_cut=0.5, margin=0.1, regime='TUMU')
        self.assertEqual(np.where(pred != 1)[0].tolist(), [0, 5, 10])

    def test_cooldown_respects_real_index_gaps(self):
        p = np.tile(np.array([[0.04, 0.02, 0.94]]), (4, 1))
        idx = np.array([10, 11, 17, 18])
        pred = cooldown_predictions(p, idx, np.zeros(4),
                                    horizon=5, score_cut=0.5, margin=0.1, regime='TUMU')
        self.assertEqual(pred.tolist(), [2, 1, 2, 1])

    def test_reject_nonmonotonic_indices(self):
        with self.assertRaises(ValueError):
            cooldown_predictions(np.ones((2, 3))/3, np.array([3, 2]),
                                 np.zeros(2), horizon=5, score_cut=.3,
                                 margin=0, regime='TUMU')

    def test_regime_filter(self):
        regime = np.array([0, 1, 2, 1, 0])
        self.assertEqual(regime_mask(regime, 'TREND').tolist(), [False, True, False, True, False])
        self.assertTrue(regime_mask(regime, 'TUMU').all())
        with self.assertRaises(ValueError):
            regime_mask(regime, 'FUTURE_BEST')

    def test_no_predictive_leakage_in_features(self):
        n = 650
        rng = np.random.default_rng(1)
        c = 100 + np.cumsum(rng.normal(0, .1, n))
        op = np.r_[100., c[:-1]]
        raw = pd.DataFrame({
            't': np.arange(n, dtype=np.int64) * 60_000 + 1_700_000_040_000,
            'open': op, 'high': np.maximum(c, op) + .1,
            'low': np.minimum(c, op) - .1, 'close': c,
            'volume': rng.uniform(1, 200, n),
        })
        x, columns, states = make_v41_features(raw)
        corrupted = raw.copy()
        corrupted.loc[500:, 'close'] *= 1.1
        corrupted.loc[500:, 'high'] *= 1.1
        b, new_columns, new_states = make_v41_features(corrupted)
        self.assertEqual(columns, new_columns)
        pd.testing.assert_frame_equal(x.iloc[:500], b.iloc[:500])
        np.testing.assert_array_equal(states[:500], new_states[:500])
        self.assertTrue(set(states).issubset({0, 1, 2}))

    def test_triple_barrier_is_stop_first(self):
        n = 90
        raw = pd.DataFrame({
            't': np.arange(n) * 60000,
            'open': 100., 'high': 100.1, 'low': 99.9,
            'close': 100., 'volume': 10.,
        })
        raw.loc[21, ['high', 'low']] = [102, 98]
        result = triple_barrier(raw, pd.Series(np.full(n, .01)), 5, 12, 1.5, 1.)
        self.assertTrue(result['stop_long'][20])
        self.assertFalse(result['target_long'][20])

    def test_purged_folds(self):
        for h in [5, 15]:
            for tr, va in folds(10_000, h):
                self.assertLessEqual(tr.stop + h, int(va[0]))

    def test_no_supported_policy_stays_abstain(self):
        n = 120
        idx = np.arange(20, n)
        scores = np.tile([.15, .7, .15], (len(idx), 1))
        bar = {
            'net_long': np.full(n, -10.0),
            'net_short': np.full(n, -10.0),
            'target_long': np.zeros(n, dtype=bool),
            'target_short': np.zeros(n, dtype=bool),
            'stop_long': np.ones(n, dtype=bool),
            'stop_short': np.ones(n, dtype=bool),
        }
        rows = [dict(proba=scores, idx=idx, labels=np.ones(n, dtype=np.int8),
                     barrier=bar, regimes=np.zeros(len(idx)))]*2
        pol = select_policy_v41(rows, 5)
        self.assertFalse(pol['qualified'])
        self.assertEqual(pol['score_cut'], .99)
        self.assertEqual(pol['policies_examined'], len(SCORE_CUTS)*len(MARGIN_CUTS)*len(REGIMES))

    def test_zero_signals_in_one_fold_stays_unqualified(self):
        n = 110
        idx = np.arange(10, n)
        bar = {
            'net_long': np.full(n, 10.0),
            'net_short': np.full(n, 10.0),
            'target_long': np.ones(n, dtype=bool),
            'target_short': np.ones(n, dtype=bool),
            'stop_long': np.zeros(n, dtype=bool),
            'stop_short': np.zeros(n, dtype=bool),
        }
        win = dict(proba=np.tile([.9, .05, .05], (len(idx), 1)), idx=idx,
                   labels=np.zeros(n, dtype=np.int8), barrier=bar, regimes=np.zeros(len(idx)))
        abstain = dict(proba=np.tile([.01, .98, .01], (len(idx), 1)), idx=idx,
                       labels=np.ones(n, dtype=np.int8), barrier=bar, regimes=np.zeros(len(idx)))
        s = score_policy([win, abstain], horizon=5, score_cut=.5, margin=.1, regime='TUMU')
        self.assertGreater(s['signals_total'], 0)
        self.assertFalse(s['has_minimum_support'])
        self.assertFalse(s['positive_in_both_folds'])

    def test_negative_fold_rejects_even_if_other_fold_wins(self):
        n = 120
        idx = np.arange(10, n)
        bar_plus = dict(net_long=np.full(n, 10.), net_short=np.full(n, 10.),
                        target_long=np.ones(n, dtype=bool), target_short=np.ones(n, dtype=bool),
                        stop_long=np.zeros(n, dtype=bool), stop_short=np.zeros(n, dtype=bool))
        bar_minus = dict(net_long=np.full(n, -2.), net_short=np.full(n, -2.),
                         target_long=np.zeros(n, dtype=bool), target_short=np.zeros(n, dtype=bool),
                         stop_long=np.ones(n, dtype=bool), stop_short=np.ones(n, dtype=bool))
        p = np.tile([.9, .05, .05], (len(idx), 1))
        first = dict(proba=p, idx=idx, labels=np.zeros(n, dtype=np.int8),
                     barrier=bar_plus, regimes=np.zeros(len(idx)))
        second = dict(proba=p, idx=idx, labels=np.zeros(n, dtype=np.int8),
                      barrier=bar_minus, regimes=np.zeros(len(idx)))
        s = score_policy([first, second], horizon=5, score_cut=.5, margin=.1, regime='TUMU')
        self.assertTrue(s['has_minimum_support'])
        self.assertFalse(s['positive_in_both_folds'])


if __name__ == '__main__':
    unittest.main()
