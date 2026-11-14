"""TiMi ML v4 local safety/integrity checks. No network, no trades."""
from __future__ import annotations
import unittest
import numpy as np
import pandas as pd
from train_model_v4 import triple_barrier, decide, validate_raw, folds
from ml_features_v4 import make_v4_features

class SafetyTests(unittest.TestCase):
    def setUp(self):
        self.n=300
        self.t=1690000000000 + 60000*np.arange(self.n)
        self.raw=pd.DataFrame({'t':self.t,'open':100.,'high':100.2,'low':99.8,'close':100.,'volume':100.})
        self.atr=pd.Series(np.repeat(0.01,self.n))

    def test_stop_first_same_bar(self):
        bars=self.raw.copy()
        bars.loc[11,['open','high','low','close']]=[100.,102.,98.8,100.]
        out=triple_barrier(bars,self.atr,5,12,1.5,1)
        self.assertFalse(out['target_long'][10])
        self.assertTrue(out['stop_long'][10])
        self.assertEqual(out['label'][10],1)

    def test_short_win(self):
        bars=self.raw.copy()
        bars.loc[11,['open','high','low','close']]=[100.,100.2,98.1,99.]
        out=triple_barrier(bars,self.atr,5,12,1.5,1)
        self.assertTrue(out['target_short'][10])
        self.assertEqual(out['label'][10],0)
        self.assertGreater(out['net_short'][10],0)

    def test_last_unseen_horizon_is_nan(self):
        out=triple_barrier(self.raw,self.atr,15,12,1.5,1)
        self.assertTrue(np.isnan(out['net_long'][-15:]).all())

    def test_training_cutoff_validation(self):
        r=self.raw.copy()
        with self.assertRaises(ValueError):
            validate_raw(r,'1m',through=int(r.t.iloc[-2]))

    def test_no_future_features(self):
        n=340
        t=1690000000000+np.arange(n)*60000
        rng=np.random.default_rng(3)
        close=100+np.cumsum(rng.normal(0,.05,n))
        raw=pd.DataFrame({'t':t,'open':np.r_[100.,close[:-1]],'high':np.maximum(close,np.r_[100.,close[:-1]])+.1,
                          'low':np.minimum(close,np.r_[100.,close[:-1]])-.1,
                          'close':close,'volume':rng.uniform(1,100,n)})
        a,_=make_v4_features(raw)
        mod=raw.copy()
        mod.loc[310:,'close']*=1.02
        b,_=make_v4_features(mod)
        pd.testing.assert_frame_equal(a.iloc[:310],b.iloc[:310])

    def test_purge(self):
        for tr,va in folds(10000,15):
            self.assertLessEqual(tr.stop+15,va[0])

    def test_abstain_when_scores_not_good(self):
        scores=np.array([[.3,.4,.3],[.8,.1,.1],[.1,.15,.75]])
        d=decide(scores,.45,.2)
        self.assertEqual(d.tolist(),[1,0,2])

if __name__=='__main__':
    unittest.main()
