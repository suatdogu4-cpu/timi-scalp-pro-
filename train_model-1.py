"""TiMi SCALP PRO v1.6 — offline research trainer, no trading endpoints.
Run separately from the live Render service. Developer: Suat DOĞU.
"""
import argparse
import json
import math
import time
from pathlib import Path

import httpx
import numpy as np
import pandas as pd
import pandas_ta_classic as ta
from lightgbm import LGBMClassifier
from sklearn.metrics import accuracy_score, balanced_accuracy_score, confusion_matrix, log_loss

API = 'https://data-api.binance.vision/api/v3/klines'
STEP = {'1m':60000,'3m':180000,'5m':300000,'15m':900000,'30m':1800000,'1h':3600000}


def fetch(symbol, interval, bars):
    step=STEP[interval]
    end=(int(time.time()*1000)//step)*step-1
    rows=[]
    with httpx.Client(timeout=25) as client:
        while len(rows)<bars:
            limit=min(1000,bars-len(rows))
            response=client.get(API,params={'symbol':symbol,'interval':interval,'limit':limit,'endTime':end})
            response.raise_for_status()
            batch=response.json()
            if not batch:break
            rows=batch+rows
            end=int(batch[0][0])-1
            time.sleep(.12)
    df=pd.DataFrame(rows,columns=['t','open','high','low','close','volume','close_t','quote_volume','n_trades','taker_base','taker_quote','ignore'])
    if df.empty:raise RuntimeError('No candles returned')
    df=df[['t','open','high','low','close','volume']].copy()
    for col in ['open','high','low','close','volume']:df[col]=pd.to_numeric(df[col],errors='coerce')
    df['t']=pd.to_numeric(df['t']).astype('int64')
    df=df.drop_duplicates('t').sort_values('t').reset_index(drop=True)
    if len(df)<1000:raise RuntimeError('Need at least 1000 candles')
    if not (df.t.diff().dropna()==step).all():raise RuntimeError('Candle gaps detected; training aborted')
    return df


def features(df):
    x=df.copy()
    c=x.close
    for length in (5,10,20,50,100,200):
        x[f'ema_dist_{length}']=c/ta.ema(c,length=length)-1
        x[f'vol_ratio_{length}']=x.volume/x.volume.rolling(length).mean()-1
    for length in (7,14,21):
        x[f'rsi_{length}']=ta.rsi(c,length=length)
        x[f'atr_pct_{length}']=ta.atr(x.high,x.low,c,length=length)/c
    for length in (1,2,3,5,10,20):
        x[f'return_{length}']=c.pct_change(length)
    x['range_pct']=(x.high-x.low)/c
    x['body_pct']=(x.close-x.open)/c
    x['upper_wick_pct']=(x.high-x[['open','close']].max(axis=1))/c
    x['lower_wick_pct']=(x[['open','close']].min(axis=1)-x.low)/c
    macd=ta.macd(c)
    if macd is not None:
        for col in macd.columns:x[col]=macd[col]/c
    bb=ta.bbands(c,length=20)
    if bb is not None:
        for col in bb.columns:
            if col.startswith(('BBL_','BBM_','BBU_')):x[col]=bb[col]/c-1
            else:x[col]=bb[col]
    names=[col for col in x.columns if col not in ('t','open','high','low','close','volume')]
    return x,names


def main():
    p=argparse.ArgumentParser()
    p.add_argument('--symbol',default='BTCUSDT')
    p.add_argument('--interval',choices=list(STEP),default='5m')
    p.add_argument('--bars',type=int,default=12000)
    p.add_argument('--horizons',type=int,nargs='+',default=[1,3,5])
    p.add_argument('--cost-bps',type=float,default=24.,help='Illustrative total round-trip fee and slippage in basis points')
    p.add_argument('--out',default='models')
    a=p.parse_args()
    if not (1000<=a.bars<=100000) or any(h<1 or h>100 for h in a.horizons):p.error('Invalid bars or horizons')
    if a.cost_bps<0:p.error('cost-bps must be nonnegative')
    raw=fetch(a.symbol.upper(),a.interval,a.bars)
    frame,columns=features(raw)
    out=Path(a.out);out.mkdir(parents=True,exist_ok=True)
    report={'symbol':a.symbol.upper(),'interval':a.interval,'bars':len(raw),'features':len(columns),'round_trip_cost_bps_assumed':a.cost_bps,'models':{}}
    for horizon in a.horizons:
        future=raw.close.shift(-horizon)/raw.close-1
        threshold=a.cost_bps/10000
        label=pd.Series(np.select([future < -threshold,future > threshold],[0,2],default=1),index=raw.index)
        valid=frame[columns].replace([np.inf,-np.inf],np.nan).copy()
        valid['target']=label
        valid['t']=raw.t
        valid=valid.iloc[:-horizon].dropna(subset=['target'])
        # Remove columns with too few valid observations; LightGBM handles remaining NaNs.
        chosen=[col for col in columns if valid[col].notna().mean()>=.85 and valid[col].nunique(dropna=True)>1]
        valid=valid.dropna(subset=chosen,how='all').reset_index(drop=True)
        n=len(valid);first=int(n*.6);second=int(n*.8)
        # Purge overlapping forward-label windows at both boundaries.
        train=valid.iloc[:max(0,first-horizon)]
        validation=valid.iloc[first:max(first,second-horizon)]
        test=valid.iloc[second:]
        if min(len(train),len(validation),len(test))<150 or len(chosen)<5:
            report['models'][str(horizon)]={'status':'insufficient_data'};continue
        if len(set(train.target))<2:
            report['models'][str(horizon)]={'status':'insufficient_classes'};continue
        def model():return LGBMClassifier(n_estimators=220,learning_rate=.035,num_leaves=15,max_depth=5,min_child_samples=80,
                                           colsample_bytree=.8,subsample=.8,subsample_freq=1,reg_lambda=5,
                                           random_state=42,verbosity=-1,n_jobs=2)
        clf=model();clf.fit(train[chosen],train.target)
        vp=clf.predict_proba(validation[chosen]);vpred=clf.classes_[np.argmax(vp,axis=1)]
        # No hyperparameter tuning against test. Validation results are diagnostic only.
        tp=clf.predict_proba(test[chosen]);pred=clf.classes_[np.argmax(tp,axis=1)]
        metrics={'status':'research_only_not_calibrated','train_rows':len(train),'validation_rows':len(validation),
                 'test_rows':len(test),'class_labels':clf.classes_.tolist(),
                 'validation_balanced_accuracy':round(float(balanced_accuracy_score(validation.target,vpred)),4),
                 'test_accuracy':round(float(accuracy_score(test.target,pred)),4),
                 'test_balanced_accuracy':round(float(balanced_accuracy_score(test.target,pred)),4),
                 'test_log_loss':round(float(log_loss(test.target,tp,labels=clf.classes_)),4),
                 'test_confusion_matrix':confusion_matrix(test.target,pred,labels=[0,1,2]).tolist(),
                 'test_majority_baseline':round(float(test.target.value_counts(normalize=True).max()),4),
                 'feature_count':len(chosen),'test_start_ms':int(test.t.iloc[0]),'test_end_ms':int(test.t.iloc[-1]),
                 'note':'Probabilities are uncalibrated. No execution backtest or profitability claim.'}
        report['models'][str(horizon)]=metrics
        # Only save metadata and reports. Do not load untrusted serialized models into the live server.
        (out/f'{a.symbol}_{a.interval}_h{horizon}_features.json').write_text(json.dumps(chosen,indent=2),encoding='utf-8')
        print('Horizon',horizon,metrics)
    (out/'training_report.json').write_text(json.dumps(report,indent=2,ensure_ascii=False),encoding='utf-8')
    print('Report:',out/'training_report.json')

if __name__=='__main__':main()
