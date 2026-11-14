"""TiMi ML v5.5.1 — CVD, regime and point-in-time historical funding research.

STRICTLY OFFLINE HISTORICAL RESEARCH. No orders. No model serving.

Requires sibling:
  timi_ml_v54_history_train.py (verified Binance Spot candles)
  timi_ml_v55_research.py (conservative TP/SL, costs and chronological validation)

Funding is a separate USD-M Futures source; NEVER synthesize missing funding,
never use the NEXT settlement, and do not backfill historical order book.
Select among feature groups and target definitions on VALIDATION only;
evaluate the single chosen candidate on held-out TEST exactly once.
"""
from __future__ import annotations

import argparse
import json
import math
import urllib.error
import urllib.parse
import urllib.request
from datetime import datetime, timezone
from pathlib import Path

import numpy as np
import pandas as pd

import timi_ml_v54_history_train as v54
import timi_ml_v55_research as v55

MS = 60_000
FUNDING_URL = "https://fapi.binance.com/fapi/v1/fundingRate"
FUNDING_LATENCY_MS = 5 * MS  # conservative assumed availability lag after settlement, not proven publication time
FUNDING_MAX_AGE_MS = 24 * 60 * MS
FUNDING_MIN_EVENTS = 20
FUNDING_MIN_COVERAGE = 0.95
SCHEMA = "timi-ml-v551-cvd-regime-funding-ablation-v1"
CVD_COLS = (
    "cvd_ratio_5", "cvd_ratio_15", "cvd_ratio_60",
    "cvd_price_divergence_15", "cvd_price_divergence_60",
)
REGIME_COLS = (
    "adx14", "di_spread14", "efficiency_60", "realized_vol_60",
    "vol_ratio_60_240", "ema20_50_spread", "regime_trend", "regime_range",
    "regime_high_vol",
)
FUNDING_COLS = (
    "funding_last_rate", "funding_z20", "funding_change",
    "funding_age_hours", "funding_extreme",
)


def _finite_float(value):
    try:
        f = float(value)
    except (TypeError, ValueError, OverflowError):
        return None
    return f if math.isfinite(f) else None


def funding_history(first: str, last: str, *, fetch=None):
    """Fetch official historical SETTLED funding only; no guessed values.

    Entire requested interval is paged in chronological order. A denied API,
    malformed response, pagination gap, duplicate, or empty result raises.
    """
    months = v54.month_range(first, last)
    begin = int(datetime.strptime(months[0], "%Y-%m").replace(tzinfo=timezone.utc).timestamp() * 1000)
    y, m = map(int, months[-1].split("-"))
    end = int(datetime(y + (m == 12), m % 12 + 1, 1, tzinfo=timezone.utc).timestamp() * 1000) - 1

    def default_fetch(params):
        url = FUNDING_URL + "?" + urllib.parse.urlencode(params)
        req = urllib.request.Request(url, headers={"User-Agent": "TiMi-ML-v551-public-research/1.0"})
        with urllib.request.urlopen(req, timeout=25) as resp:
            data = resp.read(2_000_001)
        if len(data) > 2_000_000:
            raise ValueError("Funding response exceeds limit")
        return json.loads(data.decode("utf-8"))

    get = fetch or default_fetch
    records = []
    cursor = begin
    seen = set()
    for _ in range(30):
        payload = get({"symbol": "BTCUSDT", "startTime": cursor, "endTime": end, "limit": 1000})
        if not isinstance(payload, list):
            raise ValueError("Funding API returned non-list response")
        if len(payload) > 1000:
            raise ValueError("Funding API ignored response size limit")
        if not payload:
            break
        page_times = []
        for item in payload:
            if not isinstance(item, dict) or item.get("symbol") != "BTCUSDT":
                raise ValueError("Funding symbol/record mismatch")
            t = int(item["fundingTime"])
            rate = _finite_float(item.get("fundingRate"))
            if rate is None or abs(rate) > 0.1 or not begin <= t <= end:
                raise ValueError("Funding response has invalid time/rate")
            if item.get("rateType", "Regular") != "Regular":
                continue  # different economic process; don't blend with regular rates
            if t in seen:
                raise ValueError("Duplicate funding settlement timestamp")
            seen.add(t)
            records.append((t, rate))
            page_times.append(t)
        if not page_times and payload:
            # still make progress through special-only pages
            last_t = int(payload[-1]["fundingTime"])
        else:
            last_t = max(page_times)
        if last_t < cursor:
            raise ValueError("Funding pagination not advancing")
        cursor = last_t + 1
        if len(payload) < 1000 or cursor > end:
            break
    else:
        raise ValueError("Funding pagination exceeds safety bound")
    if not records:
        raise ValueError("No usable regular funding events returned")
    records.sort(key=lambda x: x[0])
    if any(b[0] <= a[0] for a, b in zip(records, records[1:])):
        raise ValueError("Funding events are not uniquely increasing")
    return records


def enrich_cvd_regime(raw: pd.DataFrame, feat: pd.DataFrame):
    """Only completed 1m candles up to each signal bar, reset at actual gaps."""
    parts = []
    for _, g in raw.groupby("segment", sort=False):
        g = g.copy()
        close = g["close"].astype(float)
        high = g["high"].astype(float)
        low = g["low"].astype(float)
        volume = g["volume"].astype(float)
        buy = g["taker_buy_volume"].astype(float)
        if ((buy < 0) | (buy > volume + 1e-7)).any():
            raise ValueError("Invalid taker-buy volume")
        net = 2 * buy - volume
        # CVD = accumulating signed aggressor volume; normalized by rolling
        # base volume to make comparable across BTC eras. Not historical L2.
        for window in (5, 15, 60):
            denom = volume.rolling(window, min_periods=window).sum().replace(0, np.nan)
            g[f"cvd_ratio_{window}"] = net.rolling(window, min_periods=window).sum() / denom
        for window in (15, 60):
            ret = close.pct_change(window)
            delta = g[f"cvd_ratio_{window}"]
            opposite = (np.sign(ret) * np.sign(delta) < 0) & (ret.abs() > 0.0003) & (delta.abs() > 0.03)
            g[f"cvd_price_divergence_{window}"] = np.where(opposite, np.sign(ret), 0.0)
            g.loc[ret.isna() | delta.isna(), f"cvd_price_divergence_{window}"] = np.nan

        prev = close.shift(1)
        up = high.diff()
        down = -low.diff()
        pdm = up.where((up > down) & (up > 0), 0.0)
        mdm = down.where((down > up) & (down > 0), 0.0)
        tr = pd.concat([high - low, (high - prev).abs(), (low - prev).abs()], axis=1).max(axis=1)
        atr = tr.ewm(alpha=1 / 14, min_periods=14, adjust=False).mean()
        plus = 100 * pdm.ewm(alpha=1 / 14, min_periods=14, adjust=False).mean() / atr.replace(0, np.nan)
        minus = 100 * mdm.ewm(alpha=1 / 14, min_periods=14, adjust=False).mean() / atr.replace(0, np.nan)
        dx = 100 * (plus - minus).abs() / (plus + minus).replace(0, np.nan)
        g["adx14"] = dx.ewm(alpha=1 / 14, min_periods=14, adjust=False).mean()
        g["di_spread14"] = plus - minus
        direction = close.diff().abs()
        g["efficiency_60"] = (close - close.shift(60)).abs() / direction.rolling(60, min_periods=60).sum().replace(0, np.nan)
        ret1 = np.log(close / prev)
        g["realized_vol_60"] = ret1.rolling(60, min_periods=60).std(ddof=0)
        vol_long = ret1.rolling(240, min_periods=240).std(ddof=0)
        g["vol_ratio_60_240"] = g["realized_vol_60"] / vol_long.replace(0, np.nan)
        ema20 = close.ewm(span=20, min_periods=20, adjust=False).mean()
        ema50 = close.ewm(span=50, min_periods=50, adjust=False).mean()
        g["ema20_50_spread"] = (ema20 - ema50) / close
        g["regime_trend"] = (g["adx14"] >= 25).astype(float)
        g["regime_range"] = (g["adx14"] <= 18).astype(float)
        g["regime_high_vol"] = (g["vol_ratio_60_240"] > 1.5).astype(float)
        g.loc[g["adx14"].isna(), ["regime_trend", "regime_range"]] = np.nan
        g.loc[g["vol_ratio_60_240"].isna(), "regime_high_vol"] = np.nan
        parts.append(g[["ts", *CVD_COLS, *REGIME_COLS]])
    extended = pd.concat(parts, ignore_index=True)
    # Already-filtered v5.4 feature rows are joined by exact source candle time.
    if extended.ts.duplicated().any() or feat.ts.duplicated().any():
        raise ValueError("Duplicate 1m timestamps")
    out = feat.merge(extended, on="ts", how="left", validate="one_to_one", sort=False)
    for col in (*CVD_COLS, *REGIME_COLS):
        out[col] = out[col].replace([np.inf, -np.inf], np.nan)
    # Partial feature warmup is NOT zero-filled. Restrict all groups to same rows.
    out = out.dropna(subset=list(CVD_COLS + REGIME_COLS)).copy()
    if len(out) < 10000:
        raise ValueError("Too few continuous observations after feature warmup")
    return out.reset_index(drop=True)


def enrich_funding(feat: pd.DataFrame, events):
    """As-of join after the funding event is actually public, not projected rate.

    Missing values remain NaN; only used if high event coverage is verified.
    """
    if not events:
        return feat.copy(), {"status": "no_funding_events", "event_count": 0, "coverage": 0.0}
    event_df = pd.DataFrame(events, columns=["funding_time_ms", "funding_last_rate"])
    if event_df.funding_time_ms.duplicated().any():
        raise ValueError("Duplicate funding events")
    event_df = event_df.sort_values("funding_time_ms").reset_index(drop=True)
    event_df["available_ts"] = event_df.funding_time_ms + FUNDING_LATENCY_MS
    rate = event_df.funding_last_rate.astype(float)
    event_df["funding_change"] = rate.diff()
    mean = rate.rolling(20, min_periods=10).mean()
    std = rate.rolling(20, min_periods=10).std(ddof=0).replace(0, np.nan)
    event_df["funding_z20"] = (rate - mean) / std
    event_df["funding_extreme"] = np.where(event_df.funding_z20.abs() > 2.0, np.sign(event_df.funding_z20), 0.0)
    event_df.loc[event_df.funding_z20.isna(), "funding_extreme"] = np.nan
    left = feat[["ts"]].copy()
    left["signal_available_ms"] = left.ts + MS
    left = left.sort_values("signal_available_ms").reset_index(drop=True)
    aligned = pd.merge_asof(left, event_df, left_on="signal_available_ms", right_on="available_ts",
                            direction="backward", tolerance=FUNDING_MAX_AGE_MS)
    aligned["funding_age_hours"] = (aligned.signal_available_ms - aligned.available_ts) / 3_600_000
    if (aligned.available_ts.dropna() > aligned.loc[aligned.available_ts.notna(), "signal_available_ms"]).any():
        raise AssertionError("Funding lookahead detected")
    data = feat.merge(aligned[["ts", *FUNDING_COLS]], on="ts", how="left", validate="one_to_one")
    coverage = float(data.funding_last_rate.notna().mean())
    enough = len(events) >= FUNDING_MIN_EVENTS and coverage >= FUNDING_MIN_COVERAGE
    return data, {
        "status": "usable" if enough else "skipped_insufficient_funding_coverage",
        "event_count": len(events), "coverage": round(coverage, 5),
        "first_funding_time_ms": int(event_df.funding_time_ms.min()),
        "last_funding_time_ms": int(event_df.funding_time_ms.max()),
        "event_availability_lag_ms": FUNDING_LATENCY_MS,
        "lag_is_assumption_not_verified_publication_timestamp": True,
        "max_asof_staleness_hours": FUNDING_MAX_AGE_MS / 3_600_000,
        "not_spot_funding": True,
    }


def feature_groups(base_columns, funding_usable):
    groups = {
        "A_v55_base": list(base_columns),
        "B_base_cvd": list(base_columns) + list(CVD_COLS),
        "C_base_cvd_regime": list(base_columns) + list(CVD_COLS) + list(REGIME_COLS),
    }
    if funding_usable:
        groups["D_base_cvd_regime_funding"] = groups["C_base_cvd_regime"] + list(FUNDING_COLS)
    return groups


def research(raw, feature_frame, groups, *, costs=v55.Costs()):
    """All feature/model/threshold choice on validation ONLY; final test once."""
    costs.validate()
    if len(feature_frame) < 10000:
        raise ValueError("Insufficient historical samples")
    times = feature_frame.ts.to_numpy(dtype=np.int64)
    for name, columns in groups.items():
        absent = [c for c in columns if c not in feature_frame]
        if absent:
            raise ValueError(f"Missing features for {name}: {absent}")
    outcomes = {h: v55.build_events(raw, feature_frame, h, costs) for h in v55.HORIZONS}
    partitions = {h: v55.split_indices(times, outcomes[h]["valid"], h) for h in v55.HORIZONS}
    candidates = []
    winner = None
    total_fit = 0
    for group_name, columns in groups.items():
        matrix = feature_frame[columns].astype(np.float32)
        for horizon in v55.HORIZONS:
            events = outcomes[horizon]
            tr, va, te = partitions[horizon]
            for label_mode in v55.MODES:
                labels = events["labels"][label_mode]
                for weight in v55.WEIGHTS:
                    desc = {"feature_group": group_name, "feature_count": len(columns),
                            "horizon_minutes": horizon, "target_mode": label_mode,
                            "class_weight": weight or "none"}
                    if len(np.unique(labels[tr])) != 3 or len(np.unique(labels[va])) != 3:
                        candidates.append({**desc, "status": "missing_training_or_validation_class"})
                        continue
                    model = v55.fit_candidate(matrix.iloc[tr], labels[tr], matrix.iloc[va], labels[va], weight)
                    total_fit += 1
                    proba = model.predict_proba(matrix.iloc[va])
                    best_policy = None
                    for threshold in (0.30, 0.38, 0.48, 0.60):
                        for margin in (0.02, 0.10):
                            acts = v55.select_directions(proba, threshold, margin)
                            stats, _ = v55.backtest(va, acts, feature_frame, events, costs)
                            if not v55.eligible_selection(stats, len(va)):
                                continue
                            rank = v55.score_policy(stats)
                            if best_policy is None or rank > best_policy[0]:
                                best_policy = (rank, threshold, margin, stats)
                    if best_policy is None:
                        candidates.append({**desc, "status": "too_few_validation_trades"})
                        continue
                    rank, threshold, margin, stats = best_policy
                    candidates.append({**desc, "status": "validation_evaluated", "threshold": threshold,
                                       "margin": margin, "validation_stats": stats,
                                       "train_samples": len(tr), "validation_samples": len(va),
                                       "test_samples_reserved": len(te)})
                    if winner is None or rank > winner[0]:
                        winner = (rank, group_name, columns, model, events, labels,
                                  tr, va, te, threshold, margin, stats, horizon, label_mode, weight)
    if winner is None:
        return {"status": "no_candidate_with_valid_validation_trades",
                "validation_candidates": candidates, "trained_candidate_count": total_fit,
                "model_ready_for_live": False}, None, []
    (_, group_name, columns, model, events, labels, tr, va, te,
     threshold, margin, valid_stats, horizon, label_mode, weight) = winner
    final_matrix = feature_frame[columns].astype(np.float32)
    ptest = model.predict_proba(final_matrix.iloc[te])
    actions = v55.select_directions(ptest, threshold, margin)
    classification = v55.fmetrics(labels[te], actions)
    test_metrics, trades = v55.backtest(te, actions, feature_frame, events, costs,
                                       probabilities=ptest, truth=labels[te])
    baseline = np.ones(len(te), dtype=np.int8)
    majority_class = max((0, 1, 2), key=lambda c: int((labels[tr] == c).sum()))
    baseline[:] = majority_class
    majority_classification = v55.fmetrics(labels[te], baseline)
    momentum = feature_frame["return_5m"].to_numpy(dtype=float)[te]
    naive = np.where(momentum >= 0.001, 2, np.where(momentum <= -0.001, 0, 1))
    momentum_metrics, _ = v55.backtest(te, naive, feature_frame, events, costs)
    result = {
        "status": "research_test_completed_not_approved",
        "selected_group": group_name,
        "selected_config": {"horizon_minutes": horizon, "target_mode": label_mode,
                            "class_weight": weight or "none", "threshold": threshold, "margin": margin},
        "validation_trade_metrics": valid_stats,
        "heldout_test_trade_metrics": test_metrics,
        "heldout_test_classification": classification,
        "majority_baseline_test_classification": majority_classification,
        "naive_momentum_test_trade_metrics": momentum_metrics,
        "test_action_counts": {v55.CLASS_NAMES[c]: int((actions == c).sum()) for c in (0, 1, 2)},
        "test_target_counts": {v55.CLASS_NAMES[c]: int((labels[te] == c).sum()) for c in (0, 1, 2)},
        "train_samples": len(tr), "validation_samples": len(va), "test_samples": len(te),
        "heldout_test_used_for_selection": False,
        "ablation_comparisons_use_validation_only": True,
        "trained_candidate_count": total_fit,
        "validation_candidates": candidates,
        "model_ready_for_live": False,
        "orders_enabled": False,
        "funding_only_as_settled_history": True,
        "research_costs_bps": {"fee_per_side": costs.fee_bps_side,
                                "slippage_per_side": costs.slippage_bps_side,
                                "roundtrip": costs.roundtrip * 10000},
        "spot_short_selling_costs_not_modeled": True,
        "probabilities_calibrated": False,
    }
    return result, model, trades


def run(first, last, outdir, *, costs=v55.Costs(), download=v54.read_month,
        funding_fetch=None, funding_mode="optional"):
    """Funding mode optional/off/required. Non-secret, public research sources."""
    if funding_mode not in ("optional", "off", "required"):
        raise ValueError("Bad funding mode")
    months = v54.month_range(first, last)
    raw = v54.historical_frame(months, download=download)
    old_frame, base_columns = v54.features_targets(raw)
    feature_frame = enrich_cvd_regime(raw, old_frame)
    funding_info = {"status": "disabled", "event_count": 0}
    if funding_mode != "off":
        try:
            events = funding_history(first, last, fetch=funding_fetch)
            feature_frame, funding_info = enrich_funding(feature_frame, events)
        except (ValueError, KeyError, IndexError, TypeError, TimeoutError,
                urllib.error.URLError, OSError, json.JSONDecodeError) as exc:
            if funding_mode == "required":
                raise
            # Public funding API can be blocked by region/IP or rate limits;
            # NEVER fill missing values as zero or invent history.
            funding_info = {"status": "skipped_source_unavailable",
                            "reason_type": type(exc).__name__, "event_count": 0}
    funding_ok = funding_info.get("status") == "usable"
    groups = feature_groups(base_columns, funding_ok)
    report, model, trades = research(raw, feature_frame, groups, costs=costs)
    result = {
        "engine": "TiMi ML v5.5.1 — CVD regime funding ablation",
        "schema": SCHEMA,
        "data_source": "verified Binance Spot monthly 1m klines plus optional settled Binance USD-M Futures funding",
        "historical_months": months,
        "raw_closed_candles": len(raw),
        "eligible_feature_rows": len(feature_frame),
        "base_features": len(base_columns),
        "experimental_feature_groups": {name: len(cols) for name, cols in groups.items()},
        "cvd_data": "kline taker buy base volume; normalized rolling signed base-volume CVD, not orderbook CVD",
        "market_regime": "past-only ADX14, DI spread, realized-volatility, 60m efficiency and EMA trend",
        "funding": funding_info,
        "no_historical_l2_orderbook": True,
        "no_historical_liquidation_heatmap": True,
        "no_on_chain_wallet_attribution": True,
        "hypothetical_entry": "next 1-minute candle open after signal; no actual fill",
        "research_only": True,
        **report,
    }
    out = Path(outdir)
    out.mkdir(parents=True, exist_ok=True)
    (out / "report.json").write_text(json.dumps(result, ensure_ascii=False, indent=2, allow_nan=False), encoding="utf-8")
    if model is not None:
        model.booster_.save_model(str(out / "model_RESEARCH_ONLY.txt"), num_iteration=model.best_iteration_)
    if trades:
        import csv
        with (out / "test_signal_references.csv").open("w", encoding="utf-8", newline="") as fh:
            writer = csv.DictWriter(fh, fieldnames=list(trades[0]))
            writer.writeheader()
            writer.writerows(trades)
    print("TiMi ML v5.5.1:", result["status"], "candidates:", result.get("trained_candidate_count", 0))
    print("Funding:", funding_info.get("status"), "| selected group:", result.get("selected_group", "none"))
    print("Never send orders; no live model deployment.")
    return result


def main():
    parser = argparse.ArgumentParser(description="TiMi v5.5.1 historical research-only ablation")
    parser.add_argument("--from-month", default="2026-05")
    parser.add_argument("--to-month", default="2026-10")
    parser.add_argument("--output", default="timi_v551_artifacts")
    parser.add_argument("--funding-mode", choices=("optional", "off", "required"), default="optional")
    parser.add_argument("--fee-bps", type=float, default=10.0)
    parser.add_argument("--slippage-bps", type=float, default=2.0)
    parser.add_argument("--min-edge-bps", type=float, default=2.0)
    args = parser.parse_args()
    run(args.from_month, args.to_month, args.output,
        costs=v55.Costs(args.fee_bps, args.slippage_bps, args.min_edge_bps),
        funding_mode=args.funding_mode)


if __name__ == "__main__":
    main()
