"""TiMi v5.5.3 1/3/5m scalping-only research tests (synthetic data)."""
import json
from datetime import datetime, timezone

import numpy as np
import pandas as pd
import pytest

import timi_ml_v54_history_train as v54
import timi_ml_v55_research as v55
import timi_ml_v551_research as v551
import timi_ml_v553_scalp as scalp
from test_v552_research import dummy_source


@pytest.fixture(scope="module")
def sample():
    raw = v54.historical_frame(["2026-05"], download=lambda _: dummy_source(12200))
    feat, cols = v54.features_targets(raw)
    feat = v551.enrich_cvd_regime(raw, feat)
    return raw, feat, cols


def test_exact_scalp_horizons():
    assert scalp.SCALP_HORIZONS == (1, 3, 5)
    assert 15 not in scalp.SCALP_HORIZONS and 30 not in scalp.SCALP_HORIZONS


@pytest.mark.parametrize("h", [1, 3, 5])
def test_horizon_entry_and_expiry_are_exact(sample, h):
    raw, feat, _ = sample
    base = scalp.scalp_entry_endpoint(raw, feat, h, v55.Costs())
    idx = np.flatnonzero(base["valid"])
    assert len(idx) > 9000
    i = int(idx[250])
    position = int(np.searchsorted(raw.ts.to_numpy(), feat.ts.iloc[i]))
    assert base["entry"][i] == pytest.approx(raw.open.iloc[position+1])
    assert base["endpoint"][i] == pytest.approx(raw.close.iloc[position+h])
    ev = scalp.prepare_barriers(raw, feat, h, scalp.Settings(), 2.5, 0.8)
    assert ev["valid"][i]
    assert 1 <= int(ev["long_k"][i]) <= h
    assert 1 <= int(ev["short_k"][i]) <= h
    assert np.isfinite(ev["long_net"][i])


def test_outside_scalp_horizon_is_rejected(sample):
    raw, feat, _ = sample
    for h in (0, 15, 30, 60):
        with pytest.raises(ValueError, match="scalping horizon"):
            scalp.scalp_entry_endpoint(raw, feat, h, v55.Costs())
        with pytest.raises(ValueError, match="Invalid barrier"):
            scalp.prepare_barriers(raw, feat, h, scalp.Settings(), 2.5, 0.8)


def test_shortest_horizon_uses_only_next_bar(sample):
    raw, feat, _ = sample
    ev = scalp.prepare_barriers(raw, feat, 1, scalp.Settings(), 2.5, 0.8)
    i = int(np.flatnonzero(ev["valid"])[280])
    pos = int(np.searchsorted(raw.ts.to_numpy(), feat.ts.iloc[i]))
    later = raw.copy()
    # High/low from t+2 cannot affect a 1m-only outcome ending on t+1.
    later.loc[pos+2, "high"] *= 1.50
    later.loc[pos+2, "low"] *= 0.50
    new = scalp.prepare_barriers(later, feat, 1, scalp.Settings(), 2.5, 0.8)
    assert ev["long_net"][i] == pytest.approx(new["long_net"][i])
    assert ev["short_net"][i] == pytest.approx(new["short_net"][i])


def test_missing_minute_never_filled_or_crossed(sample):
    raw, feat, _ = sample
    discontinuous = raw.copy()
    i = int(np.searchsorted(raw.ts.to_numpy(), feat.ts.iloc[400]))
    discontinuous.loc[i+2, "ts"] += 60000
    discontinuous.loc[i+2:, "segment"] += 1
    # Out-of-order timestamps must fail closed.
    with pytest.raises(ValueError):
        scalp.scalp_entry_endpoint(discontinuous, feat, 3, v55.Costs())


def test_fourfold_purge_short_horizon(sample):
    raw, feat, _ = sample
    for h in (1,3,5):
        ev = scalp.prepare_barriers(raw, feat, h, scalp.Settings(), 3.5, 1.1)
        folds = scalp.fourfold_split(feat.ts.to_numpy(), ev["valid"], h)
        for before, after in zip(folds,folds[1:]):
            assert feat.ts.iloc[before[-1]]+(h+1)*60000 < feat.ts.iloc[after[0]]


def test_existing_feature_timeframes_are_already_1_3_5(sample):
    _, feat, cols = sample
    assert all(f"return_{x}m" in cols for x in (1,3,5))
    assert "mtf3_trend" in cols and "mtf5_trend" in cols
    assert feat["mtf5_trend"].notna().all()


def test_1m_regression_fit_works(sample):
    raw, feat, cols = sample
    ev = scalp.prepare_barriers(raw, feat, 1, scalp.Settings(), 2.5, 0.8)
    good = ev["valid"] & np.isfinite(feat[cols].to_numpy()).all(axis=1)
    tr, early, policy, test = scalp.fourfold_split(feat.ts.to_numpy(), good, 1)
    x = feat[cols].astype(np.float32)
    model = scalp.fit_net_regressor(x.iloc[tr[:500]], ev["long_net"][tr[:500]]*10000,
                                    x.iloc[early[:200]], ev["long_net"][early[:200]]*10000)
    assert np.isfinite(model.predict(x.iloc[test[:5]])).all()


def test_all_three_horizons_researched_but_no_live(sample, monkeypatch):
    raw, feat, cols = sample
    class DummyModel:
        def predict(self, df):
            return np.full(len(df), -8.)
    monkeypatch.setattr(scalp, "fit_net_regressor", lambda *a, **kw: DummyModel())
    outcome, regressors, trades = scalp.research(raw,feat,cols,
            horizons=(1,3,5),barriers=(("test_barrier",2.5,0.8),))
    assert outcome["scalping_horizons_minutes"] == [1,3,5]
    assert set(outcome["per_horizon_validation_only"]) == {"1","3","5"}
    assert outcome["model_ready_for_live"] is False
    assert outcome["orders_enabled"] is False
    assert outcome["heldout_test_actions"]["AL"] == 0
    assert outcome["heldout_test_actions"]["SAT"] == 0
    assert outcome["fitted_regressors"] == 12
    assert not trades
