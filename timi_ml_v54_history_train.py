"""TiMi ML v5.4 - Binance Vision historical BTCUSDT SPOT research trainer.

Read-only, no credentials, no exchange orders, no live model deployment.
Uses verified CLOSED 1m historical klines and original taker-buy-volume columns.
NEVER writes into timi_v5_observations or claims historical live order-book snapshots.
"""
from __future__ import annotations

import argparse
import calendar
import csv
import hashlib
import io
import json
import math
import os
import sys
import tempfile
import urllib.error
import urllib.request
import zipfile
from datetime import datetime, timezone
from pathlib import Path

BASE = "https://data.binance.vision/data/spot/monthly/klines"
SYMBOL = "BTCUSDT"
INTERVAL = "1m"
MS = 60_000
FEATURE_SCHEMA = "timi-ml-v54-historical-ohlcv-taker-v1"
HORIZON = 5
CLASS_MAP = {0: "SAT", 1: "BEKLE", 2: "AL"}


def month_range(first: str, last: str):
    """Inclusive range of YYYY-MM monthly files, no future data."""
    def parse(s):
        try:
            d = datetime.strptime(s, "%Y-%m")
        except ValueError as exc:
            raise ValueError("Months must be YYYY-MM") from exc
        if d.strftime("%Y-%m") != s:
            raise ValueError("Invalid month format")
        return d.year, d.month
    y, m = parse(first)
    ey, em = parse(last)
    if (ey, em) < (y, m):
        raise ValueError("End month precedes start month")
    if (ey-y)*12+(em-m) >= 24:
        raise ValueError("At most 24 months per run")
    output = []
    while (y, m) <= (ey, em):
        output.append(f"{y:04d}-{m:02d}")
        m += 1
        if m == 13:
            y, m = y+1, 1
    return output


def normalize_ms(raw: str) -> int:
    """Binance spot kline timestamps are microseconds from 2025 onward."""
    n = int(raw)
    if n >= 10**15:
        if n % 1000: raise ValueError("Kline open timestamp is not minute-aligned microseconds")
        n //= 1000
    if n < 1_000_000_000_000 or n >= 10**14 or n % MS:
        raise ValueError("Out-of-range or unaligned kline open timestamp")
    return n


def _get(url: str, timeout=55, max_bytes=50_000_000) -> bytes:
    req = urllib.request.Request(url, headers={"User-Agent": "TiMi-ML-v5.4-historical-research/1.0"})
    with urllib.request.urlopen(req, timeout=timeout) as response:
        if response.status != 200:
            raise RuntimeError(f"HTTP {response.status}")
        data = response.read(max_bytes+1)
    if len(data) > max_bytes:
        raise ValueError("Source file exceeds configured safe limit")
    return data


def read_month(month: str):
    """Verify official SHA256 and parse one official ZIP without persistent raw-file storage."""
    basename = f"{SYMBOL}-{INTERVAL}-{month}.zip"
    url = f"{BASE}/{SYMBOL}/{INTERVAL}/{basename}"
    archive = _get(url)
    checksum_txt = _get(url+".CHECKSUM", max_bytes=1024).decode("utf-8").strip()
    digest = checksum_txt.split()[0].lower()
    if len(digest) != 64 or any(x not in "0123456789abcdef" for x in digest):
        raise ValueError("Invalid official checksum format")
    if hashlib.sha256(archive).hexdigest() != digest:
        raise ValueError("Official Binance checksum mismatch")
    items = []
    with zipfile.ZipFile(io.BytesIO(archive)) as z:
        names = [i for i in z.infolist() if not i.is_dir()]
        if len(names) != 1 or not names[0].filename.endswith(".csv"):
            raise ValueError("Unexpected Binance ZIP archive structure")
        if names[0].file_size > 100_000_000:
            raise ValueError("CSV uncompressed size exceeds safety limit")
        with z.open(names[0]) as fh:
            reader = csv.reader(io.TextIOWrapper(fh, encoding="utf-8-sig", newline=""))
            for row in reader:
                if not row:
                    continue
                if row[0].strip().lower() in ("open time", "open_time", "opentime"):
                    continue
                if len(row) < 11:
                    raise ValueError("Unexpected Binance kline column count")
                t = normalize_ms(row[0])
                o,h,l,c,v = map(float, (row[1],row[2],row[3],row[4],row[5]))
                trades = float(row[8])
                taker_buy_volume = float(row[9])
                quote_vol = float(row[7])
                if not all(map(math.isfinite, (o,h,l,c,v,trades,taker_buy_volume,quote_vol))):
                    raise ValueError("Nonfinite OHLCV values")
                if min(o,h,l,c)<=0 or h < max(o,l,c) or l > min(o,c) or v < 0 or trades<0 or taker_buy_volume<0 or taker_buy_volume>v+1e-6:
                    raise ValueError("Invalid OHLCV/trade data")
                items.append((t,o,h,l,c,v,trades,taker_buy_volume,quote_vol))
    if not items:
        raise ValueError("Empty monthly data")
    return items


def historical_frame(months, download=read_month):
    import numpy as np
    import pandas as pd
    rows = []
    last_t = None
    for month in months:
        section = download(month)
        for x in section:
            if last_t is not None and x[0] <= last_t:
                raise ValueError(f"Non-monotonic or duplicate candle at {month}")
            last_t = x[0]
            rows.append(x)
        print(f"Verified {month}: {len(section)} BTCUSDT 1m candles", flush=True)
    df = pd.DataFrame(rows, columns=["ts","open","high","low","close","volume","trades","taker_buy_volume","quote_volume"])
    df["minute"] = df.ts//MS
    diff = df.minute.diff()
    # This must not silently backfill unavailable intervals with invented prices.
    df["segment"] = (diff.ne(1)).cumsum()
    if df.empty:
        raise ValueError("No candles")
    # Prevent current incomplete bars from entering features/targets.
    now_ms = int(datetime.now(tz=timezone.utc).timestamp()*1000)
    df = df[df.ts+MS <= now_ms].copy()
    if df.empty:
        raise ValueError("No closed historical candles")
    return df


def features_targets(df, horizon=HORIZON, threshold=0.0015):
    """Point-in-time features and 5 COMPLETE next candles after a known signal."""
    import numpy as np
    import pandas as pd
    # Segment groups avoid feature contamination across missing-minute gaps.
    def one(g):
        g = g.copy()
        c, o, h, l, v = (g[z].astype(float) for z in ("close","open","high","low","volume"))
        pclose = c.shift(1)
        change = c.diff()
        gain = change.clip(lower=0).ewm(alpha=1/14,adjust=False,min_periods=14).mean()
        loss = -change.clip(upper=0).ewm(alpha=1/14,adjust=False,min_periods=14).mean()
        rs = gain/loss.replace(0,np.nan)
        g["rsi14"] = 100-100/(1+rs)
        ema12=c.ewm(span=12,adjust=False,min_periods=26).mean()
        ema26=c.ewm(span=26,adjust=False,min_periods=26).mean()
        macd=ema12-ema26
        g["macd_hist_pct"]=(macd-macd.ewm(span=9,adjust=False,min_periods=9).mean())/c
        tr=pd.concat([h-l,(h-pclose).abs(),(l-pclose).abs()],axis=1).max(axis=1)
        g["atr14_pct"] = tr.ewm(alpha=1/14,adjust=False,min_periods=14).mean()/c
        for n in (10,20,50,100,200):
            g[f"ema{n}_pct"] = c.ewm(span=n,adjust=False,min_periods=n).mean()/c-1
            g[f"sma{n}_pct"] = c.rolling(n).mean()/c-1
        bb20=c.rolling(20).mean()
        std20=c.rolling(20).std(ddof=0)
        g["bb20_z"]=(c-bb20)/std20.replace(0,np.nan)
        tp=(h+l+c)/3
        g["cci20"]=(tp-tp.rolling(20).mean())/(0.015*tp.rolling(20).apply(lambda x: np.abs(x-x.mean()).mean(),raw=True)).replace(0,np.nan)
        g["momentum10_pct"]=c/c.shift(10)-1
        g["range_pct"]=(h-l)/c
        g["body_pct"]=(c-o)/o
        for n in (1,3,5,15,30,60):
            g[f"return_{n}m"]=c.pct_change(n)
        g["volume_log"]=np.log1p(v)
        g["volume_ratio20"]=v/(v.rolling(20).mean().replace(0,np.nan))
        g["trades_log"]=np.log1p(g.trades)
        g["taker_buy_imbalance"] = (2*g.taker_buy_volume/v.replace(0,np.nan)-1).clip(-1,1)
        g["quote_volume_log"] = np.log1p(g.quote_volume)
        # As-of MTF: higher-timeframe value exists ONLY once its final 1m bar closes.
        for n in (3,5,15,60):
            closed = (g.minute+1).mod(n).eq(0)
            closed_prices=c.loc[closed]
            baseline=closed_prices.ewm(span=10,adjust=False,min_periods=10).mean()
            trend = (closed_prices/baseline-1).reindex(g.index).ffill()
            g[f"mtf{n}_trend"] = trend
        # Signal at close of candle t, hypothetical entry next candle open.
        # Exit at close t+H; 5 complete bars: t+1, t+2,... t+5.
        entry=o.shift(-1)
        exit_price=c.shift(-horizon)
        ret=exit_price/entry-1
        g["label"] = np.where(ret > threshold, 2, np.where(ret < -threshold, 0, 1))
        g["forward_return"] = ret
        g["entry_reference"] = entry
        g["exit_reference"] = exit_price
        # Last horizon rows are unknown, never label as neutral.
        g.loc[g.index[-horizon:],"label"] = np.nan
        return g
    parts = [one(g) for _,g in df.groupby("segment",sort=False)]
    result = pd.concat(parts,ignore_index=True)
    feature_names = [x for x in result.columns if x in (
        "rsi14","macd_hist_pct","atr14_pct","bb20_z","cci20","momentum10_pct",
        "range_pct","body_pct","volume_log","volume_ratio20","trades_log",
        "taker_buy_imbalance","quote_volume_log",
        "taker_buy_quote_estimate") or x.startswith(("ema","sma","return_","mtf"))]
    # Warmup done independently within each continuous segment; no leakage.
    result[feature_names] = result[feature_names].replace([np.inf,-np.inf],np.nan)
    result = result.dropna(subset=feature_names+["label","forward_return","entry_reference"]).copy()
    result["label"] = result["label"].astype(int)
    return result, feature_names


def split_purged(frame, horizon=HORIZON):
    """Chronological independent blocks; purge adjacent horizon+1 rows."""
    n = len(frame)
    a=int(n*0.65)
    b=int(n*0.80)
    gap=horizon+1
    train=frame.iloc[:max(0,a-gap)]
    valid=frame.iloc[a:min(b-gap,n)]
    test=frame.iloc[b:]
    if not len(train) or not len(valid) or not len(test):
        raise ValueError("Not enough labeled examples for chrono-split")
    if not (train.ts.max()+MS*(horizon+1)<valid.ts.min() and valid.ts.max()+MS*(horizon+1)<test.ts.min()):
        raise ValueError("Purging check failed")
    return train,valid,test


def train_report(df, cols, *, seed=42):
    import numpy as np
    from sklearn.metrics import balanced_accuracy_score, accuracy_score, f1_score, confusion_matrix
    from sklearn.dummy import DummyClassifier
    import lightgbm as lgb
    train,valid,test=split_purged(df)
    if min(len(train),len(valid),len(test)) < 1000:
        return {"status":"insufficient_data", "split_counts":[len(train),len(valid),len(test)]},None
    if len(set(train.label))<3 or len(set(valid.label))<3 or len(set(test.label))<3:
        return {"status":"insufficient_class_diversity", "counts":{
            "train":train.label.value_counts().to_dict(),"valid":valid.label.value_counts().to_dict(),"test":test.label.value_counts().to_dict()}},None
    model=lgb.LGBMClassifier(objective="multiclass",num_class=3,n_estimators=400,learning_rate=0.03,
                             num_leaves=15,max_depth=6,min_child_samples=70,subsample=0.85,
                             colsample_bytree=0.9,reg_lambda=5,verbosity=-1,random_state=seed,n_jobs=2)
    model.fit(train[cols],train.label,eval_set=[(valid[cols],valid.label)],eval_metric="multi_logloss",
              callbacks=[lgb.early_stopping(25,verbose=False)])
    # Final test never used in fitting, early stopping or feature selection.
    preds=model.predict(test[cols])
    baseline=DummyClassifier(strategy="most_frequent").fit(train[cols],train.label)
    base=baseline.predict(test[cols])
    met={"model_accuracy":float(accuracy_score(test.label,preds)),
         "model_balanced_accuracy":float(balanced_accuracy_score(test.label,preds)),
         "model_macro_f1":float(f1_score(test.label,preds,average="macro",zero_division=0)),
         "majority_accuracy":float(accuracy_score(test.label,base)),
         "majority_balanced_accuracy":float(balanced_accuracy_score(test.label,base)),
         "majority_macro_f1":float(f1_score(test.label,base,average="macro",zero_division=0)),
         "test_confusion_matrix_rows_truth":confusion_matrix(test.label,preds,labels=[0,1,2]).tolist(),
         "test_class_distribution":{CLASS_MAP[k]:int((test.label==k).sum()) for k in CLASS_MAP},
         "train_count":len(train),"validation_count":len(valid),"test_count":len(test),
         "best_iteration":int(model.best_iteration_ or model.n_estimators_),
         "experimental_only":True,"trade_execution_enabled":False}
    return {"status":"trained_research_only", "metrics":met},model


def run(first, last, outdir, *, downloader=read_month):
    months=month_range(first,last)
    df=historical_frame(months,download=downloader)
    labeled,columns=features_targets(df)
    report={"engine":"TiMi ML v5.4 Binance Vision SPOT historical baseline",
            "feature_schema":FEATURE_SCHEMA,"symbol":SYMBOL,"timeframe":"1m",
            "months":months,"verified_source":"binance_public_data_with_SHA256_CHECKSUM",
            "raw_closed_candles":len(df),"labeled_examples":len(labeled),
            "gap_events":int(df.segment.nunique()-1),"feature_count":len(columns),"features":columns,
            "classes":CLASS_MAP,"label_threshold":0.0015,"horizon_minutes":HORIZON,
            "hypothetical_entry_reference":"next_1m_bar_open_after_signal_close",
            "hypothetical_exit_reference":"close_of_fifth_following_1m_bar",
            "no_historical_orderbook":True,"no_live_cvd":True,
            "data_not_inserted_into_postgres":True,"not_v53_flow_model":True,
            "live_inference_enabled":False,"real_order_execution":False,
            "note":"Research-only; no historical orderbook. Not a guarantee of future profits."}
    if len(labeled)<7000:
        details={"status":"insufficient_data","minimum_labeled":7000}
        model=None
    else:
        details,model=train_report(labeled,columns)
    report.update(details)
    out=Path(outdir)
    out.mkdir(parents=True,exist_ok=True)
    (out/"report.json").write_text(json.dumps(report,indent=2,ensure_ascii=False,allow_nan=False),encoding="utf-8")
    if model is not None:
        model.booster_.save_model(str(out/"model_HISTORICAL_RESEARCH_ONLY.txt"),num_iteration=model.best_iteration_)
    print(json.dumps({k:v for k,v in report.items() if k in ("status","raw_closed_candles","labeled_examples","gap_events","feature_count","metrics")},ensure_ascii=False,default=str))
    return report


def main():
    p=argparse.ArgumentParser(description="TiMi - standalone Binance historical research-only training")
    p.add_argument("--from-month",default="2026-05",dest="first")
    p.add_argument("--to-month",default="2026-10",dest="last")
    p.add_argument("--output",default="timi_v54_artifacts")
    a=p.parse_args()
    run(a.first,a.last,a.output)


if __name__=="__main__":main()
