"""TiMi MetaFilter v0.1 OFFLINE research trainer.

Consumes only REAL post-signal TiMi v5 archive snapshots. No historical
order-flow reconstruction, no order execution, no live model promotion.
Research outcome: 5-min future observed-price directional move NET of assumed
fees/slippage/spread; NOT a fill simulation or TP-first classification.
"""
from __future__ import annotations
import argparse
import asyncio
import bisect
import json
import math
import os
from pathlib import Path

import numpy as np

from timi_ml_meta_features import (SCHEMA, FEATURE_KEYS,
    extract_features, eligible_archive_record)
from timi_ml_metafilter import MODEL_SCHEMA

HORIZON_MS = 5 * 60_000
EVAL_TOLERANCE_MS = 90_000
PURGE_MS = HORIZON_MS + EVAL_TOLERANCE_MS + 60_000
MIN_FIT = 400
MIN_SECONDARY = 70
MIN_DIAGNOSTIC = 100


def _as_dict(obj):
    if isinstance(obj, dict):
        return obj
    if isinstance(obj, (str, bytes)):
        return json.loads(obj)
    raise ValueError("Expected JSON object")


def read_jsonl(path):
    with open(path, encoding="utf-8") as fd:
        for line in fd:
            if line.strip():
                yield _as_dict(line)


async def read_postgres():
    """Read existing database, never modify it. No credentials in logs/files."""
    import asyncpg
    url = os.getenv("TIMI_V5_DATABASE_URL", "")
    if not url:
        raise ValueError("TIMI_V5_DATABASE_URL must be configured in secret env")
    conn = await asyncpg.connect(url, timeout=15)
    try:
        return [dict(r["payload"]) if isinstance(r["payload"], dict)
                else json.loads(r["payload"])
                for r in await conn.fetch(
                    "SELECT payload FROM timi_v5_observations ORDER BY observed_ms ASC")]
    finally:
        await conn.close()


def make_dataset(records, *, fee_bps=10., slippage_bps=2.):
    """Forward-only, chronological. No synthetic price rows or future features.

    For each candidate at t, label against first REAL later observed snapshot
    at or shortly after t+5min. Actual quote executions are NOT known.
    """
    if not all(math.isfinite(v) and v >= 0 for v in (fee_bps, slippage_bps)):
        raise ValueError("Costs must be finite and nonnegative")
    rows = sorted((_as_dict(x) for x in records),
                  key=lambda x: (str(x.get("symbol")), int(x.get("observed_at_ms", 0))))
    by_symbol = {}
    for r in rows:
        if r.get("schema") != "timi-ml-v5-observation-v1":
            continue
        if isinstance(r.get("observed_at_ms"), int):
            by_symbol.setdefault(r.get("symbol"), []).append(r)
    features, labels, nets, times, directions = [], [], [], [], []
    skipped = {"ineligible":0, "no_future_quote":0, "feature_missing":0,
               "same_direction_cooldown":0}
    for symbol, stream in sorted(by_symbol.items()):
        stamps = [x["observed_at_ms"] for x in stream]
        last_event = {}
        for row in stream:
            if not eligible_archive_record(row):
                skipped["ineligible"] += 1
                continue
            direction = row["decision"]
            observed = row["observed_at_ms"]
            if observed - last_event.get(direction, -10**30) < HORIZON_MS:
                skipped["same_direction_cooldown"] += 1
                continue
            try:
                vector = extract_features(row)
                entry = float(row["market_price_at_snapshot"])
            except (TypeError, ValueError, OverflowError):
                skipped["feature_missing"] += 1
                continue
            idx = bisect.bisect_left(stamps, observed + HORIZON_MS)
            if idx >= len(stream) or stamps[idx] > observed + HORIZON_MS + EVAL_TOLERANCE_MS:
                skipped["no_future_quote"] += 1
                continue
            target = stream[idx]
            if not (target.get("flow_metadata") or {}).get("fresh", False):
                skipped["no_future_quote"] += 1
                continue
            try:
                exit_price = float(target["market_price_at_snapshot"])
                if not math.isfinite(exit_price) or exit_price <= 0:
                    raise ValueError("invalid future price")
                spread_bps = float((row.get("flow") or {})["spread_pct"]) * 100.0
            except (ValueError, TypeError, KeyError):
                skipped["no_future_quote"] += 1
                continue
            side = 1 if direction == "LONG" else -1
            # Descriptive, directional reference P&L proxy. SHORT is not executable
            # Spot shorting. Historical spread at EXIT is not known; entry spread
            # is used as a conservative fixed proxy, not true observed fill cost.
            gross = side * (exit_price / entry - 1.0) * 10000
            cost = 2 * (fee_bps + slippage_bps) + spread_bps
            net = gross - cost
            features.append(vector)
            labels.append(1 if net > 0 else 0)
            nets.append(net)
            times.append(observed)
            directions.append(direction)
            last_event[direction] = observed
    return {"X": np.asarray(features, dtype=float).reshape(-1, len(FEATURE_KEYS)),
            "y": np.asarray(labels, dtype=int), "net_bps": np.asarray(nets, dtype=float),
            "ts": np.asarray(times, dtype=np.int64), "directions": directions,
            "skipped": skipped}


def split_sequential(ts):
    """Five chronological folds, 7-min purge around EACH fold boundary."""
    ts = np.asarray(ts, dtype=np.int64)
    if ts.size < 5 or np.any(np.diff(ts) < 0):
        raise ValueError("Timestamps must be sorted")
    n = len(ts)
    cuts = [ts[int(n * x)] for x in (0.50, 0.60, 0.70, 0.80)]
    output = [
        np.flatnonzero(ts < cuts[0] - PURGE_MS),
        np.flatnonzero((ts >= cuts[0]) & (ts < cuts[1] - PURGE_MS)),
        np.flatnonzero((ts >= cuts[1]) & (ts < cuts[2] - PURGE_MS)),
        np.flatnonzero((ts >= cuts[2]) & (ts < cuts[3] - PURGE_MS)),
        np.flatnonzero(ts >= cuts[3]),
    ]
    for a, b in zip(output, output[1:]):
        if len(a) and len(b) and ts[a[-1]] + PURGE_MS > ts[b[0]]:
            raise AssertionError("Leakage through fold boundary")
    return output


def _stats(y, p, net, threshold):
    selected = np.asarray(p) >= threshold
    wins = (np.asarray(net)[selected] > 0)
    chosen = np.asarray(net)[selected]
    if len(chosen):
        gains = chosen[chosen > 0].sum()
        losses = -chosen[chosen < 0].sum()
        pf = float(gains / losses) if losses > 0 else None
        mean = float(chosen.mean())
    else:
        pf = mean = None
    return {"events": len(y), "selected": int(selected.sum()),
            "directional_net_positive_rate": (float(np.mean(wins)) if len(wins) else None),
            "selected_mean_net_bps": mean, "selected_profit_factor_proxy": pf}


def train(dataset, output, *, min_fit=MIN_FIT):
    """Offline LightGBM + chronological calibration; never auto-approved live."""
    from lightgbm import LGBMClassifier, early_stopping, log_evaluation
    from sklearn.isotonic import IsotonicRegression
    from sklearn.metrics import brier_score_loss, log_loss
    X, y, net, ts = (dataset[k] for k in ("X", "y", "net_bps", "ts"))
    order = np.argsort(ts, kind="stable")
    X, y, net, ts = [a[order] for a in (X, y, net, ts)]
    folds = split_sequential(ts)
    required = (min_fit, MIN_SECONDARY, MIN_SECONDARY, MIN_SECONDARY, MIN_DIAGNOSTIC)
    counts = [len(i) for i in folds]
    report = {"schema": "timi-metafilter-research-v1", "feature_schema":SCHEMA,
              "indicator_count":26, "feature_count":len(FEATURE_KEYS),
              "observations": len(y), "fold_sizes":counts,
              "folds": "50% fit, 10% earlystop, 10% calibration, 10% policy, 20% diagnostic; 7m purge",
              "research_only": True, "live_promotion_allowed": False,
              "no_live_orders": True, "no_automatic_live_approval": True,
              "label": "Forward 5m observed directional price move NET of assumed costs (not actual fills)",
              "limitations": ["Previously inspected research data are not a fresh holdout",
                  "SHORT in Spot is only a directional/exiting proxy, not executable short profit",
                  "Last-trade reference prices and proxy spread do not model real fills"],
              "status": "NO_TRAINABLE_CANDIDATE"}
    out = Path(output)
    out.mkdir(parents=True, exist_ok=True)
    if any(c < minimum for c, minimum in zip(counts, required)):
        report["reason"] = "Insufficient chronological sample sizes"
    elif any(len(np.unique(y[idx])) != 2 for idx in folds):
        report["reason"] = "Insufficient positive/negative labels in one or more time folds"
    else:
        fit, es, cal, policy, diag = folds
        model = LGBMClassifier(n_estimators=300, learning_rate=0.035,
                               max_depth=4, num_leaves=12, min_child_samples=35,
                               colsample_bytree=0.75, subsample=0.85,
                               reg_lambda=3.0, random_state=42,
                               n_jobs=1, verbosity=-1)
        model.fit(X[fit], y[fit], eval_set=[(X[es], y[es])],
                  callbacks=[early_stopping(25, verbose=False),log_evaluation(0)])
        cal_raw = model.predict_proba(X[cal])[:,1]
        isotonic = IsotonicRegression(out_of_bounds="clip").fit(cal_raw, y[cal])
        predict = lambda idx: isotonic.transform(model.predict_proba(X[idx])[:,1])
        policy_prob = predict(policy)
        # Select threshold ONLY using the policy fold (not the diagnostic fold).
        options = []
        for threshold in (0.60, 0.70, 0.80, 0.90):
            s = _stats(y[policy], policy_prob, net[policy], threshold)
            if s["selected"] >= 25 and (s["selected_mean_net_bps"] or 0) > 0:
                options.append((s["selected_mean_net_bps"], threshold))
        threshold = sorted(options, reverse=True)[0][1] if options else 0.90
        diag_prob = predict(diag)
        summary = _stats(y[diag], diag_prob, net[diag], threshold)
        summary.update(brier=float(brier_score_loss(y[diag], diag_prob)),
                       logloss=float(log_loss(y[diag], np.clip(diag_prob,1e-9,1-1e-9))),
                       label_base_rate=float(y[diag].mean()))
        report.update(status="EXPLORATORY_MODEL_TRAINED_RESEARCH_ONLY",
                      policy_threshold=threshold,
                      diagnostic_not_fresh_holdout=summary,
                      feature_importance=dict(zip(FEATURE_KEYS,
                          map(int, model.feature_importances_))),
                      model_ready_for_live=False)
        model.booster_.save_model(str(out / "timi_meta_model_RESEARCH_ONLY.txt"))
        meta = {"schema":MODEL_SCHEMA, "feature_schema":SCHEMA,
                "feature_keys":list(FEATURE_KEYS), "threshold":threshold,
                "calibration":{"type":"isotonic", "x":list(map(float,isotonic.X_thresholds_)),
                               "y":list(map(float,isotonic.y_thresholds_))},
                "research_only": True, "live_promotion_allowed": False,
                "model_trained": True,
                "warning":"Never turn on live approval without independent new-period validation and explicit review"}
        (out / "timi_meta_model_RESEARCH_ONLY.json").write_text(
            json.dumps(meta,indent=2,allow_nan=False),encoding="utf8")
    (out / "research_report.json").write_text(
        json.dumps(report,indent=2,ensure_ascii=False,allow_nan=False),encoding="utf8")
    return report


def main():
    parser = argparse.ArgumentParser(description="TiMi MetaFilter offline research only")
    source = parser.add_mutually_exclusive_group(required=True)
    source.add_argument("--archive-jsonl", help="Local exported authentic TiMi v5 payloads")
    source.add_argument("--from-db-env", action="store_true",help="Read-only env TIMI_V5_DATABASE_URL")
    parser.add_argument("--output", default="timi_meta_research_outputs")
    parser.add_argument("--fee-bps", type=float, default=10.)
    parser.add_argument("--slippage-bps", type=float, default=2.)
    args = parser.parse_args()
    if args.archive_jsonl:
        records = list(read_jsonl(args.archive_jsonl))
    else:
        records = asyncio.run(read_postgres())
    dataset = make_dataset(records, fee_bps=args.fee_bps,
                           slippage_bps=args.slippage_bps)
    report = train(dataset,args.output)
    report["cost_assumptions"] = {"fee_bps_each_side":args.fee_bps,
                                   "slippage_bps_each_side":args.slippage_bps,
                                   "entry_spread_bps_proxy": "from each observed snapshot"}
    report["excluded_records"] = dataset["skipped"]
    (Path(args.output)/"research_report.json").write_text(
        json.dumps(report,indent=2,ensure_ascii=False,allow_nan=False),encoding="utf8")
    print(json.dumps({"status":report["status"],"observations":report["observations"],
                      "model_ready_for_live":False},ensure_ascii=False))

if __name__ == "__main__":
    main()
