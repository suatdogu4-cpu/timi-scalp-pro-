"""TiMi MetaFilter v0.1 feature contract (research only).

Only point-in-time TiMi v1.8.4 signal snapshots and the existing 26-indicator
archive are accepted; no synthetic order-flow history or new signal generator.
Developer: Suat DOĞU. No orders.
"""
from __future__ import annotations
import math
from timi_ml_v5_indicators import NAMES

SCHEMA = "timi-metafilter-features-v1"
INDICATOR_KEYS = tuple("i_" + name for name in NAMES)
OTHER_KEYS = (
    "signal_direction", "rule_score", "flow_imbalance", "flow_aggressor_ratio",
    "flow_notional_log", "flow_trades_log", "flow_spread_bps",
    "book_age_seconds", "trade_age_seconds", "book_imbalance_delta",
    "mtf_1m", "mtf_3m", "mtf_5m", "mtf_15m",
    "rsi14_pro", "ema20_vs_price", "ema50_vs_price", "atr14_pct",
)
FEATURE_KEYS = INDICATOR_KEYS + OTHER_KEYS
PRICE_LIKE = {
    "ema10", "sma10", "ema20", "sma20", "ema30", "sma30",
    "ema50", "sma50", "ema100", "sma100", "ema200", "sma200",
    "vwma20", "hma9", "ichimoku_kijun26",
}
PRICE_DIFF = {"awesome_osc", "momentum10", "macd_hist_12_26_9", "bull_bear13"}
DIRECTION = {"LONG": 1.0, "SHORT": -1.0}
MTF_DIRECTION = {"LONG": 1.0, "SHORT": -1.0, "KARISIK": 0.0}


def number(x, name):
    try:
        x = float(x)
    except (TypeError, ValueError, OverflowError) as exc:
        raise ValueError("Missing or invalid: " + name) from exc
    if not math.isfinite(x):
        raise ValueError("Non-finite: " + name)
    return x


def extract_features(payload):
    """Build the exact same ordered vector for future training and live inference.

    Archive payload is created at/after 'feature_available_at_ms'; not the
    historical candle close. A trainer must reject records that fail quality.
    """
    if not isinstance(payload, dict):
        raise ValueError("TiMi archive payload must be an object")
    direction = payload.get("decision")
    if direction not in DIRECTION:
        raise ValueError("No executable candidate: source signal BEKLE")
    indicator = (payload.get("indicators_26") or {}).get("values") or {}
    price = number(payload.get("market_price_at_snapshot"), "market_price_at_snapshot")
    if price <= 0:
        raise ValueError("Invalid reference price")
    out = {}
    for name in NAMES:
        v = number(indicator.get(name), name)
        if name in PRICE_LIKE:
            v = v / price - 1.0
        elif name in PRICE_DIFF:
            v = v / price
        out["i_" + name] = v
    flow = payload.get("flow") or {}
    buy = number(flow.get("buy_notional_60s"), "buy_notional_60s")
    sell = number(flow.get("sell_notional_60s"), "sell_notional_60s")
    if buy < 0 or sell < 0 or (buy + sell) <= 0:
        raise ValueError("Insufficient aggressive flow")
    book_age = number(flow.get("book_age_ms"), "book_age_ms")
    trade_age = number(flow.get("trade_age_ms"), "trade_age_ms")
    spread = number(flow.get("spread_pct"), "spread_pct")
    trades = number(flow.get("trade_count_60s"), "trade_count_60s")
    if not (0 <= book_age <= 5000 and 0 <= trade_age <= 5000 and
            0 <= spread <= 0.12 and trades >= 10):
        raise ValueError("Stale flow, excessive spread or insufficient trades")
    extras = payload.get("orderbook_extras") or {}
    delta = extras.get("imbalance_delta_10s")
    if delta is None:
        raise ValueError("Book delta windows incomplete")
    mtf_list = payload.get("mtf") or []
    mtf = {x.get("tf"): x for x in mtf_list if isinstance(x, dict)}
    for interval in ("1m", "3m", "5m", "15m"):
        if interval not in mtf or mtf[interval].get("direction") not in MTF_DIRECTION:
            raise ValueError("Missing MTF frame " + interval)
    pro = payload.get("pro_indicators") or {}
    out.update({
        "signal_direction": DIRECTION[direction],
        "rule_score": number(payload.get("rule_score"), "rule_score"),
        "flow_imbalance": number(flow.get("imbalance"), "imbalance"),
        "flow_aggressor_ratio": (buy - sell) / (buy + sell),
        "flow_notional_log": math.log1p(buy + sell),
        "flow_trades_log": math.log1p(trades),
        "flow_spread_bps": spread * 100.0,
        "book_age_seconds": book_age / 1000.0,
        "trade_age_seconds": trade_age / 1000.0,
        "book_imbalance_delta": number(delta, "book delta"),
        "mtf_1m": MTF_DIRECTION[mtf["1m"]["direction"]],
        "mtf_3m": MTF_DIRECTION[mtf["3m"]["direction"]],
        "mtf_5m": MTF_DIRECTION[mtf["5m"]["direction"]],
        "mtf_15m": MTF_DIRECTION[mtf["15m"]["direction"]],
        "rsi14_pro": number(pro.get("rsi14"), "pro.rsi14"),
        "ema20_vs_price": number(pro.get("ema20"), "pro.ema20") / price - 1,
        "ema50_vs_price": number(pro.get("ema50"), "pro.ema50") / price - 1,
        "atr14_pct": number(pro.get("atr14"), "pro.atr14") / price,
    })
    if tuple(out) != FEATURE_KEYS or not all(math.isfinite(v) for v in out.values()):
        raise ValueError("Feature schema mismatch")
    return [out[k] for k in FEATURE_KEYS]


def eligible_archive_record(row):
    """Post-close signal snapshot only; eligibility not inferred from price alone."""
    if row.get("schema") != "timi-ml-v5-observation-v1":
        return False
    q = row.get("quality") or {}
    t = row.get("feature_available_at_ms")
    return bool(q.get("ml_eligible") is True and row.get("decision") in DIRECTION
                and isinstance(t, int) and t >= row.get("candle_close_ms", math.inf))
