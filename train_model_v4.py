"""TiMi ML v4 — selective LightGBM research with TP-before-SL labels.

NO live ML, NO broker/exchange orders, NO simulated fills or real P&L.
Developer: Suat DOĞU.

Train on candles ending at the frozen ASOF cutoff. Evaluate future candles
strictly after that cutoff without training on them. Historical holdout
from old overlapping periods is EXPLORATORY, not a clean prospective test.
"""
from __future__ import annotations
import argparse
import json
import math
import time
from datetime import datetime, timezone
from pathlib import Path

import httpx
import lightgbm as lgb
import numpy as np
import pandas as pd
from lightgbm import LGBMClassifier
from sklearn.metrics import confusion_matrix, f1_score, balanced_accuracy_score

from ml_features import WARMUP_BARS
from ml_features_v4 import FEATURE_SCHEMA, make_v4_features

API = 'https://data-api.binance.vision/api/v3/klines'
STEP = {'1m': 60000, '3m': 180000, '5m': 300000, '15m': 900000}
SYMBOLS = {'BTCUSDT', 'ETHUSDT', 'SOLUSDT', 'BNBUSDT', 'XRPUSDT', 'DOGEUSDT', 'ADAUSDT', 'AVAXUSDT'}
CLASS_NAMES = {0:'SHORT', 1:'BEKLE', 2:'LONG'}
# Frozen pre-v4 research cutoff. Changing this requires a new evaluation cohort.
DEFAULT_ASOF = '2026-11-13T16:29:00+00:00'


def iso(ms):
    return datetime.fromtimestamp(int(ms)/1000, timezone.utc).isoformat()


def parse_utc(value):
    dt = datetime.fromisoformat(value.replace('Z','+00:00'))
    if dt.tzinfo is None:
        raise ValueError('UTC offset required in --asof')
    return int(dt.timestamp()*1000)


def validate_raw(raw, interval, *, after=None, through=None):
    df = raw.copy()
    required = ['t','open','high','low','close','volume']
    if not set(required).issubset(df):
        raise ValueError('Missing data columns: '+str(set(required)-set(df)))
    for k in required:
        df[k] = pd.to_numeric(df[k], errors='coerce')
    if df[required].isnull().any().any():
        raise ValueError('NaN in OHLCV')
    df['t'] = df.t.astype('int64')
    df = df.sort_values('t').reset_index(drop=True)
    if df.t.duplicated().any() or not df.t.diff().iloc[1:].eq(STEP[interval]).all():
        raise ValueError('OHLCV gaps or duplicates; training aborted')
    if (df.t % STEP[interval] != 0).any():
        raise ValueError('Misaligned OHLCV timestamps')
    if len(df) < 2 or (df[['open','high','low','close']] <= 0).any().any() or (df.volume < 0).any():
        raise ValueError('Invalid candles')
    if ((df.high < df[['open','low','close']].max(axis=1)) |
            (df.low > df[['open','high','close']].min(axis=1))).any():
        raise ValueError('Invalid high/low')
    if after is not None and (df.t <= after).any():
        raise ValueError('Unexpected historical row in prospective test')
    if through is not None and (df.t > through).any():
        raise ValueError('Future candle leaked into training set')
    return df[required]


def _request(client, params):
    for attempt in range(5):
        try:
            r = client.get(API, params=params)
            r.raise_for_status()
            data = r.json()
            if not isinstance(data,list):
                raise ValueError('Binance payload is not a list')
            return data
        except httpx.HTTPStatusError as e:
            if e.response.status_code in (400,401,403,404,418) or attempt==4:
                raise
        except (httpx.TimeoutException,httpx.TransportError):
            if attempt == 4:
                raise
        time.sleep(min(8,2**attempt))
    raise RuntimeError('No candles')


def _frame(rows):
    return pd.DataFrame(rows,columns=['t','open','high','low','close','volume','close_t',
                                      'quote_volume','n_trades','taker_base','taker_quote','ignore'])


def fetch_training(symbol, interval, bars, asof_ms):
    """Exactly `bars` completed historical candles, ending BEFORE/AT asof."""
    step = STEP[interval]
    now = int(time.time()*1000)
    end = min(asof_ms - 1, now-3000)
    rows = []
    with httpx.Client(timeout=30) as client:
        while len(rows) < bars:
            batch = _request(client,{'symbol':symbol,'interval':interval,'limit':min(1000,bars-len(rows)),
                                      'endTime':end})
            if not batch:
                raise RuntimeError('Binance cannot provide requested pre-cutoff candles')
            rows = batch+rows
            end = int(batch[0][0])-1
            if len(rows)<bars:
                time.sleep(0.13)
    raw = _frame(rows)
    raw['t'] = pd.to_numeric(raw.t,errors='raise').astype('int64')
    raw = raw[raw.t + step <= asof_ms].copy()
    if len(raw)!=bars:
        raise ValueError('Unexpected training data length')
    return validate_raw(raw,interval,through=asof_ms-step)


def fetch_after(symbol, interval, asof_ms, *, cap=50000):
    """Only closed future candles; supports evaluation in a future rerun."""
    step = STEP[interval]
    end_limit = int(time.time()*1000)-3000-step
    start = asof_ms
    if start > end_limit:
        return pd.DataFrame(columns=['t','open','high','low','close','volume'])
    rows=[]
    with httpx.Client(timeout=30) as client:
        while start<=end_limit and len(rows)<cap:
            batch = _request(client,{'symbol':symbol,'interval':interval,'startTime':start,
                                      'endTime':end_limit+step-1,'limit':min(1000,cap-len(rows))})
            if not batch:
                break
            rows.extend(batch)
            next_start=int(batch[-1][0])+step
            if next_start<=start:
                raise RuntimeError('Non-advancing future candle pagination')
            start=next_start
            if start<=end_limit:
                time.sleep(0.13)
    raw=_frame(rows)
    if not len(raw):
        return pd.DataFrame(columns=['t','open','high','low','close','volume'])
    raw['t'] = pd.to_numeric(raw.t,errors='raise').astype('int64')
    raw=raw[(raw.t>=asof_ms)&(raw.t<=end_limit)]
    return validate_raw(raw,interval,after=asof_ms-1)


def triple_barrier(raw, atr_pct, horizon, cost_bps, tp_atr, sl_atr):
    """At i CLOSE, hypothetical entry at next candle OPEN; pessimistic stop-first intrabar.
    Label LONG only if long target is touched first and short is not a winner; vice versa.
    End-of-horizon closes are valued but do NOT become binary target hits.
    No real fill/fee/order-book evidence is available.
    """
    n=len(raw)
    op=raw.open.to_numpy(dtype=float)
    hi=raw.high.to_numpy(dtype=float)
    lo=raw.low.to_numpy(dtype=float)
    cl=raw.close.to_numpy(dtype=float)
    atrpct=atr_pct.to_numpy(dtype=float)
    labels=np.ones(n,dtype=np.int8)
    long_net=np.full(n,np.nan)
    short_net=np.full(n,np.nan)
    long_hit=np.zeros(n,dtype=bool)
    short_hit=np.zeros(n,dtype=bool)
    long_sl=np.zeros(n,dtype=bool)
    short_sl=np.zeros(n,dtype=bool)
    # No prediction/evaluation for rows whose full future isn't yet known.
    for i in range(n-horizon-1):
        if not np.isfinite(atrpct[i]) or atrpct[i]<=0:
            continue
        entry=op[i+1]
        atr_distance=cl[i]*atrpct[i]
        if entry<=0 or atr_distance<=0:
            continue
        take=max(tp_atr*atr_distance,entry*(cost_bps+1)/10000)
        stop=sl_atr*atr_distance
        for side in (1,-1):
            tp=entry+side*take
            sl=entry-side*stop
            state='timeout'
            exit_price=cl[i+horizon]
            for j in range(i+1,i+horizon+1):
                # Stop FIRST if both barriers touched within the same 1m bar.
                stop_touched=lo[j]<=sl if side==1 else hi[j]>=sl
                take_touched=hi[j]>=tp if side==1 else lo[j]<=tp
                if stop_touched:
                    exit_price=sl
                    state='stop'
                    break
                if take_touched:
                    exit_price=tp
                    state='target'
                    break
            net=side*(exit_price/entry-1)*10000-cost_bps
            if side==1:
                long_net[i]=net
                long_hit[i]=state=='target' and net>0
                long_sl[i]=state=='stop'
            else:
                short_net[i]=net
                short_hit[i]=state=='target' and net>0
                short_sl[i]=state=='stop'
        if long_hit[i] and not short_hit[i]:
            labels[i]=2
        elif short_hit[i] and not long_hit[i]:
            labels[i]=0
    return {'label':labels,'net_long':long_net,'net_short':short_net,
            'target_long':long_hit,'target_short':short_hit,
            'stop_long':long_sl,'stop_short':short_sl}


def fit(x,y,mode):
    y=np.asarray(y,dtype=int)
    if np.min(np.bincount(y,minlength=3))<40:
        raise ValueError('Not enough confirmed LONG/SHORT examples')
    params=dict(objective='multiclass',num_class=3,n_estimators=115,learning_rate=0.045,
                num_leaves=9,max_depth=4,min_child_samples=100,min_split_gain=0.02,
                colsample_bytree=0.8,subsample=0.85,subsample_freq=1,reg_lambda=12,
                n_jobs=2,random_state=44,verbosity=-1)
    weights=None
    if mode=='capped_weight':
        count=np.bincount(y,minlength=3)
        weights=np.sqrt(count[1]/np.maximum(1,count[y]))
        weights=np.clip(weights,0.7,4.0)
    elif mode!='standard':
        raise ValueError('Invalid mode')
    m=LGBMClassifier(**params)
    m.fit(x,y,sample_weight=weights)
    return m


def decide(p, min_class_score, min_margin):
    """Confidence-like raw scores; NOT calibrated success probabilities."""
    arr=np.asarray(p,dtype=float)
    d=np.ones(len(arr),dtype=np.int8)
    if arr.shape[1]!=3:
        raise ValueError('Expected 3-way model scores')
    go_short=(arr[:,0]>=min_class_score)&((arr[:,0]-np.maximum(arr[:,1],arr[:,2]))>=min_margin)
    go_long=(arr[:,2]>=min_class_score)&((arr[:,2]-np.maximum(arr[:,0],arr[:,1]))>=min_margin)
    d[go_short]=0
    d[go_long]=2
    return d


def score_metrics(y,pred,barrier,idx,*,include_array=False):
    truth=np.asarray(y)[idx]
    prediction=np.asarray(pred,dtype=np.int8)
    if len(truth)!=len(prediction):
        raise ValueError('Evaluation shapes differ')
    taken=prediction!=1
    mask=idx[taken]
    sides=prediction[taken]
    net=np.where(sides==2,barrier['net_long'][mask],barrier['net_short'][mask])
    tp=np.where(sides==2,barrier['target_long'][mask],barrier['target_short'][mask])
    sl=np.where(sides==2,barrier['stop_long'][mask],barrier['stop_short'][mask])
    valid=np.isfinite(net)
    if not np.all(valid):
        raise ValueError('Incomplete future for evaluated signals')
    signals=int(taken.sum())
    actual=truth
    res={
        'rows':int(len(idx)),'signals':signals,'coverage_pct':round(100*signals/max(1,len(idx)),3),
        'class_counts':{CLASS_NAMES[k]:int((actual==k).sum()) for k in (0,1,2)},
        'signals_by_side':{'LONG':int((sides==2).sum()),'SHORT':int((sides==0).sum())},
        'target_first_count':int(tp.sum()),
        'target_first_rate_pct':round(100*float(tp.mean()),3) if signals else None,
        'positive_after_assumed_cost_count':int((net>0).sum()),
        'positive_after_assumed_cost_pct':round(100*float((net>0).mean()),3) if signals else None,
        'avg_net_change_bps':round(float(net.mean()),4) if signals else None,
        'median_net_change_bps':round(float(np.median(net)),4) if signals else None,
        'stop_first_pct':round(100*float(sl.mean()),3) if signals else None,
        'directional_label_precision_pct':round(100*float((sides==truth[taken]).mean()),3) if signals else None,
        'neutral_baseline_accuracy_pct':round(100*float((truth==1).mean()),3),
        'overall_accuracy_pct':round(100*float((prediction==truth).mean()),3),
        'macro_f1':round(float(f1_score(truth,prediction,labels=[0,1,2],average='macro',zero_division=0)),4),
        'confusion_matrix_SH_BE_LONG':confusion_matrix(truth,prediction,labels=[0,1,2]).tolist(),
    }
    # Conservative lower Wilson bound for win ratio; statistical independence NOT assumed.
    if signals:
        phat=(net>0).mean();z=1.96
        den=1+z*z/signals
        lo=(phat+z*z/(2*signals)-z*np.sqrt((phat*(1-phat)+z*z/(4*signals))/signals))/den
        res['naive_wilson_win_lower95_pct']=round(100*float(lo),3)
        res['wilson_caveat']='Naive iid approximation ONLY: overlapping signals are correlated; not a valid trading confidence interval'
    if include_array:
        res['_net_array']=net.tolist()
    return res


def folds(dev_end,h):
    # Disjoint validation windows; labels cannot overlap training boundary.
    return [(slice(0,int(dev_end*a)-h),np.arange(int(dev_end*a),int(dev_end*b)))
            for a,b in ((.42,.52),(.57,.67),(.72,.82))]


def pick_policy(val_rows, min_signal=35, min_each=8):
    # val_rows is a list of (probs, truth, barrier, global_indices), 2 early folds only.
    policies=[]
    for p_cut in (0.33,0.40,0.48,0.56,0.64,0.72):
        for margin in (0.02,0.10,0.20,0.30):
            rows=[]
            for pred_proba,y,b,ix in val_rows:
                pred=decide(pred_proba,p_cut,margin)
                rows.append(score_metrics(y,pred,b,ix))
            if sum(x['signals'] for x in rows)<min_signal or min(x['signals'] for x in rows)<min_each:
                continue
            net=np.average([x['avg_net_change_bps'] for x in rows],weights=[x['signals'] for x in rows])
            rate=np.average([x['positive_after_assumed_cost_pct'] for x in rows],weights=[x['signals'] for x in rows])
            # Require positive net mean on both folds; no threshold hack for fake hit rate.
            stable=all(x['avg_net_change_bps']>0 for x in rows)
            policies.append({'score_cut':p_cut,'margin_cut':margin,'validation_folds':rows,
                             'weighted_avg_net_bps':round(float(net),4),
                             'weighted_win_rate_pct':round(float(rate),3),
                             'stable_positive_net':stable,
                             'total_signals':sum(x['signals'] for x in rows)})
    eligible=[x for x in policies if x['stable_positive_net']]
    if not eligible:
        return {'qualified':False,'reason':'No threshold with sufficient signals AND positive net mean in both early folds',
                'policies_tested':24,'policies_with_support':len(policies),
                'score_cut':0.99,'margin_cut':0.99,'validation_folds':[]}
    best=max(eligible,key=lambda x:(x['weighted_avg_net_bps'],x['total_signals']))
    return {'qualified':True,'policies_tested':24,'policies_with_support':len(policies),**best}


def report_to_json(data,path):
    path.write_text(json.dumps(data,ensure_ascii=False,indent=2,allow_nan=False),encoding='utf-8')


def train(raw, symbol, interval, horizons, cost_bps, tp_atr, sl_atr, out, asof_ms, future=None):
    out.mkdir(parents=True,exist_ok=True)
    full_x,all_names=make_v4_features(raw)
    raw=raw.iloc[WARMUP_BARS:].reset_index(drop=True)
    x=full_x.iloc[WARMUP_BARS:].reset_index(drop=True)
    report={
      'purpose':'TiMi_ML_v4_SELECTIVE_OFFLINE_RESEARCH_NOT_DEPLOYED',
      'symbol':symbol,'interval':interval,'feature_schema':FEATURE_SCHEMA,
      'train_frozen_asof_utc':iso(asof_ms),'raw_training_end_utc':iso(raw.t.iloc[-1]),
      'raw_training_start_utc':iso(raw.t.iloc[0]),
      'historical_overlap_warning':'All pre-cutoff historical splits overlap previously inspected v1-v3 data: EXPLORATORY',
      'created_utc':datetime.now(timezone.utc).isoformat(),
      'training_bars_after_warmup':len(raw),
      'cost_assumption_roundtrip_bps':cost_bps,
      'cost_not_measured':'Illustrative TOTAL round-trip costs including fees, spread, slippage. NOT live executable PnL',
      'target_ATR_multiple':tp_atr,'stop_ATR_multiple':sl_atr,
      'entry_assumption':'Open of next minute bar, NOT a verified executable trade',
      'same_bar_rule':'Stop has precedence if TP/SL touched in same bar',
      'horizons':{},
      'warnings':['No real trading orders and no real trading/execution P&L.',
                  'No synchronized historical order book or CVD features.',
                  'Model scores are NOT calibrated probabilities.',
                  'Reusing old historical periods makes holdout exploratory, never a fresh untouched prospective test.',
                  'Selection of best rule on folds 1 and 2 can overfit; folds 3 and holdout are diagnostic only.',
                  'Overlapping horizons make observations highly correlated.',
                  'Must confirm net results on independent future data before any live integration.'],
      'live_deployment_allowed':False,
    }
    for horizon in horizons:
        n=len(raw)-horizon-1
        if n<8000:
            report['horizons'][str(horizon)]={'status':'INSUFFICIENT_DATA'}
            continue
        lab=triple_barrier(raw,x['atr_pct_14'],horizon,cost_bps,tp_atr,sl_atr)
        y=lab['label']
        dev_end=int(n*.82)
        fold_spec=folds(dev_end,horizon)
        chosen=[name for name in all_names if x[name].iloc[:fold_spec[0][0].stop].notna().mean()>=.97
                and x[name].iloc[:fold_spec[0][0].stop].nunique(dropna=True)>5]
        if len(chosen)<15:
            report['horizons'][str(horizon)]={'status':'INSUFFICIENT_VALID_FEATURES'}
            continue
        X=x[chosen].iloc[:n].fillna(0.0)
        result={'status':'RESEARCH_ONLY','features':chosen,'label_definition':'TP-before-SL only, otherwise NEUTRAL',
                'label_counts':{CLASS_NAMES[k]:int((y[:n]==k).sum()) for k in (0,1,2)},
                'historical_holdout_start_utc':iso(raw.t.iloc[dev_end]),
                'variants':{}}
        candidate=[]
        for variant in ('standard','capped_weight'):
            try:
                model_rows=[]
                for tr,va in fold_spec:
                    model=fit(X.iloc[tr],y[tr],variant)
                    p=model.predict_proba(X.iloc[va])
                    model_rows.append((p,y,lab,va))
                policy=pick_policy(model_rows[:2])
                confirmation=score_metrics(y,decide(model_rows[2][0],policy['score_cut'],policy['margin_cut']),lab,model_rows[2][3])
                result['variants'][variant]={'selected_policy':policy,'fold3_confirmation':confirmation}
                if policy['qualified']:
                    candidate.append((policy['weighted_avg_net_bps'],policy['total_signals'],variant))
            except ValueError as e:
                result['variants'][variant]={'error':str(e)}
        if candidate:
            _,_,best=max(candidate)
            pol=result['variants'][best]['selected_policy']
        else:
            best='standard'
            pol={'qualified':False,'score_cut':0.99,'margin_cut':0.99,
                 'reason':'No stable validation policy; abstain from directional predictions'}
        train_end=dev_end-horizon
        try:
            final=fit(X.iloc[:train_end],y[:train_end],best)
        except ValueError as e:
            result['status']='INSUFFICIENT_EVENTS';result['reason']=str(e)
            report['horizons'][str(horizon)]=result
            continue
        p=final.predict_proba(X.iloc[dev_end:n])
        pred=decide(p,pol['score_cut'],pol['margin_cut'])
        holdout=score_metrics(y,pred,lab,np.arange(dev_end,n))
        result.update({'selected_variant':best,'selection':pol,
                       'historical_holdout':holdout,
                       'historical_validation_only':True,
                       'diagnostic_pass':bool(pol['qualified'] and
                           result['variants'][best]['fold3_confirmation']['signals']>=30 and
                           result['variants'][best]['fold3_confirmation']['avg_net_change_bps'] is not None and
                           result['variants'][best]['fold3_confirmation']['avg_net_change_bps']>0 and
                           holdout['signals']>=30 and holdout['avg_net_change_bps'] is not None and
                           holdout['avg_net_change_bps']>0)})
        stem=f'{symbol}_{interval}_h{horizon}_v4'
        model_file=stem+'.txt'
        final.booster_.save_model(str(out/model_file))
        # Verify model can be read back exactly, no pickles.
        test_sample=X.iloc[dev_end:dev_end+3]
        restored=lgb.Booster(model_file=str(out/model_file)).predict(test_sample)
        if not np.allclose(restored,final.predict_proba(test_sample),rtol=1e-6,atol=1e-7):
            raise RuntimeError('Model round-trip failed')
        meta={'status':'RESEARCH_ONLY_NOT_APPROVED','feature_schema':FEATURE_SCHEMA,
              'features_ordered':chosen,'symbol':symbol,'interval':interval,
              'horizon':horizon,'asof_utc':iso(asof_ms),'train_end_utc':iso(raw.t.iloc[train_end-1]),
              'model_file':model_file,'variant':best,
              'score_cut':pol['score_cut'],'margin_cut':pol['margin_cut'],
              'tp_atr':tp_atr,'sl_atr':sl_atr,'roundtrip_cost_bps_assumed':cost_bps}
        report_to_json(meta,out/(stem+'_metadata.json'))
        result['artifacts']={'model':model_file,'metadata':stem+'_metadata.json'}
        if future is not None and len(future)>horizon+1:
            combined=pd.concat([raw.tail(WARMUP_BARS+20),future],ignore_index=True)
            combined=validate_raw(combined,interval)
            fx,_=make_v4_features(combined)
            fbar=triple_barrier(combined,fx['atr_pct_14'],horizon,cost_bps,tp_atr,sl_atr)
            offset=len(combined)-len(future)
            usable=np.arange(offset,len(combined)-horizon-1)
            if len(usable):
                fX=fx[chosen].iloc[usable].fillna(0)
                fP=final.predict_proba(fX)
                fdecision=decide(fP,pol['score_cut'],pol['margin_cut'])
                result['prospective_after_asof']={
                    'period_start_utc':iso(combined.t.iloc[usable[0]]),
                    'period_end_utc':iso(combined.t.iloc[usable[-1]]),
                    'no_future_training':True,
                    'metrics':score_metrics(fbar['label'],fdecision,fbar,usable),
                    'sample_adequacy':'INSUFFICIENT' if len(usable)<10000 else 'MORE_DATA_AVAILABLE',
                    'warning':'Prospective signals share overlapping horizons, and TP/SL touches do not imply executable fills.'}
            else:
                result['prospective_after_asof']={'status':'INSUFFICIENT_ROWS'}
        else:
            result['prospective_after_asof']={'status':'NOT_YET_AVAILABLE'}
        report['horizons'][str(horizon)]=result
        print(f'h={horizon} select={best} qualifying={pol["qualified"]} historical_signals={holdout["signals"]}, historical_mean_net_bps={holdout["avg_net_change_bps"]}, post_asof={result["prospective_after_asof"].get("metrics",{}).get("signals","none")}',flush=True)
    report_to_json(report,out/'training_report_v4.json')
    return report


def main():
    ap=argparse.ArgumentParser(description='TiMi ML v4 offline selective LightGBM study')
    ap.add_argument('--symbol',default='BTCUSDT')
    ap.add_argument('--interval',choices=tuple(STEP),default='1m')
    ap.add_argument('--bars',type=int,default=90000)
    ap.add_argument('--horizons',nargs='+',type=int,default=[5,15])
    ap.add_argument('--asof',default=DEFAULT_ASOF,help='UTC freeze boundary, not a moving date')
    ap.add_argument('--roundtrip-cost-bps',type=float,default=12.0,
                    help='Assumed total fee+spread+slippage in basis points, not measured')
    ap.add_argument('--target-atr',type=float,default=1.5)
    ap.add_argument('--stop-atr',type=float,default=1.0)
    ap.add_argument('--out',default='models_v4')
    ap.add_argument('--training-csv',help='Optional strictly pre-asof OHLCV CSV for offline smoke tests')
    ap.add_argument('--future-csv',help='Optional after-asof CSV for offline smoke tests')
    args=ap.parse_args()
    args.symbol=args.symbol.upper()
    if args.symbol not in SYMBOLS or not 10000<=args.bars<=200000:
        ap.error('Unsupported symbol or bars')
    if not args.horizons or len(set(args.horizons))!=len(args.horizons) or any(h<1 or h>30 for h in args.horizons):
        ap.error('horizons must be distinct and 1..30')
    if not (0<=args.roundtrip_cost_bps<=100 and 0.2<=args.target_atr<=5
            and 0.2<=args.stop_atr<=5):
        ap.error('Costs or ATR multipliers out of bounds')
    asof=parse_utc(args.asof)
    if asof % STEP[args.interval] != 0:
        ap.error('asof must align to candle boundary')
    if args.training_csv:
        raw=validate_raw(pd.read_csv(args.training_csv),args.interval,through=asof-STEP[args.interval])
    else:
        raw=fetch_training(args.symbol,args.interval,args.bars,asof)
    if args.future_csv:
        future=validate_raw(pd.read_csv(args.future_csv),args.interval,after=asof-1)
    else:
        future=fetch_after(args.symbol,args.interval,asof)
    report=train(raw,args.symbol,args.interval,args.horizons,args.roundtrip_cost_bps,
                 args.target_atr,args.stop_atr,Path(args.out),asof,future)
    print('RESEARCH ONLY. Live deployment disabled. Report at '+str(Path(args.out)/'training_report_v4.json'))
    if not any(x.get('status')=='RESEARCH_ONLY' for x in report['horizons'].values()):
        raise SystemExit('No research models trained; see report')

if __name__=='__main__':
    main()
