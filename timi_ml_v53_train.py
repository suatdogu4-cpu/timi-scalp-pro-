"""TiMi ML v5.3 - isolated, offline, research-only LightGBM trainer.

Developer: Suat DOGU.
Reads only the existing PostgreSQL v5 observation archive.
- No real/paper orders, no live model inference, no changes to the FastAPI service.
- Never backfills historical order flow.
- Purged chronological train/validation/test split; untouched final test set.
- Model artifact is NEVER auto-deployed to the live application.

Run: TIMI_V53_DATABASE_URL=<Render EXTERNAL PostgreSQL URL> python timi_ml_v53_train.py
The URL is a SECRET (GitHub Actions repository secret), never commit it.
"""
from __future__ import annotations

import json
import math
import os
from collections import Counter
from datetime import datetime, timezone
from pathlib import Path

MS = 60_000
HORIZON_MINUTES = 5
COST_NEUTRAL_THRESHOLD = 0.0015  # fixed research label boundary +/-15bps
MIN_RAW = 10_080  # archive research threshold, not a performance guarantee
MIN_LABELED = 9_500
MIN_DAYS = 7
MIN_TRAIN = 5_000
MIN_VALID = 1_000
MIN_TEST = 1_000
CLASSES = (0, 1, 2)   # 0=SAT/SHORT, 1=BEKLE, 2=AL/LONG
CLASS_NAMES = {0: 'SAT', 1: 'BEKLE', 2: 'AL'}
INDICATOR_NAMES = (
    'rsi14','stoch_k_14_3_3','cci20','adx14','awesome_osc',
    'momentum10','macd_hist_12_26_9','stoch_rsi_14_14_3',
    'williams_r14','bull_bear13','ultimate_7_14_28',
    'ema10','sma10','ema20','sma20','ema30','sma30','ema50','sma50',
    'ema100','sma100','ema200','sma200','vwma20','hma9','ichimoku_kijun26',
)
PRICE_INDICATORS = {x for x in INDICATOR_NAMES if
                    x.startswith(('ema','sma')) or x in ('vwma20','hma9','ichimoku_kijun26')}
DIFF_INDICATORS = {'awesome_osc','momentum10','macd_hist_12_26_9','bull_bear13'}
FLOW_KEYS = ('trade_age_ms','book_age_ms','bid_notional','ask_notional','imbalance',
             'buy_notional_60s','sell_notional_60s','cvd_60s','trade_count_60s','spread_pct')
TF_NAMES = ('1m','3m','5m','15m')
FEATURE_NAMES = (tuple('i_'+x for x in INDICATOR_NAMES) +
                 ('candle_body','candle_range','candle_volume_log','snapshot_vs_close','rule_score') +
                 tuple('flow_'+x for x in FLOW_KEYS) +
                 tuple('mtf_'+x for x in TF_NAMES))
OUTPUT = Path('timi_v53_artifacts')


def finite(value):
    """Return an IEEE finite number or None; reject booleans/strings."""
    if isinstance(value, bool) or not isinstance(value, (int,float)):
        return None
    val = float(value)
    return val if math.isfinite(val) else None


def signed_log(x):
    return math.copysign(math.log1p(abs(x)),x)


def encode_direction(direction):
    s = str(direction or '').strip().upper()
    if s in ('UP','BULL','BULLISH','LONG','AL','YUKARI','YUKARI YON','YÜKSELİŞ'):
        return 1.0
    if s in ('DOWN','BEAR','BEARISH','SHORT','SAT','ASAGI','AŞAĞI','DÜŞÜŞ'):
        return -1.0
    return 0.0  # unknown and neutral are indistinguishable for this baseline


def available_time(row):
    p=row['payload']
    # Later of observed-record time and feature snapshot calculation time.
    return max(int(row['observed_ms']), int(p['feature_available_at_ms']),
               int(p.get('observed_at_ms', row['observed_ms'])))


def valid_row(row, symbol):
    p=row.get('payload')
    if not isinstance(p,dict) or row.get('symbol') != symbol:return False
    if p.get('schema') != 'timi-ml-v5-observation-v1':return False
    c=p.get('candle') or {}
    try:
        op=int(row['candle_open_ms']); close=int(row['candle_close_ms'])
        observed=int(row['observed_ms']); available=available_time(row)
        price=finite(c.get('c')); mprice=finite(p.get('market_price_at_snapshot'))
    except (ValueError,TypeError,KeyError,OverflowError):return False
    return bool(c.get('closed') is True and op==int(c.get('t',-1))
                and close==op+MS and observed>=close and available>=close
                and available - close <= 90_000
                and price is not None and price>0 and mprice is not None and mprice>0)


def eligible_row(row):
    q=(row['payload'].get('quality') or {})
    return bool(all(q.get(k) is True for k in (
        'closed_candle_verified','pro_analysis_matches_candle',
        'flow_fresh','snapshot_time_fresh','mtf_all_four_fresh','ml_eligible')))


def feature_vector(p):
    c=p['candle']; price=finite(c['c']);op=finite(c['o']);high=finite(c['h']);low=finite(c['l'])
    if not all(x is not None and x>0 for x in (price,op,high,low)):
        return None
    vals=(p.get('indicators_26') or {}).get('values') or {}
    if not isinstance(vals,dict) or len([n for n in INDICATOR_NAMES if finite(vals.get(n)) is not None])<24:
        return None
    vector=[]
    for n in INDICATOR_NAMES:
        v=finite(vals.get(n))
        if v is not None and n in PRICE_INDICATORS:v=v/price-1.0
        elif v is not None and n in DIFF_INDICATORS:v=v/price
        vector.append(float('nan') if v is None else v)
    volume=finite(c.get('v'))
    market=finite(p.get('market_price_at_snapshot'))
    rule=finite(p.get('rule_score'))
    vector += [price/op-1,high/low-1,math.log1p(max(0,volume or 0)),
               market/price-1 if market else float('nan'),
               rule if rule is not None else float('nan')]
    flow=p.get('flow') or {}
    for n in FLOW_KEYS:
        v=finite(flow.get(n))
        if v is not None and n in ('bid_notional','ask_notional','buy_notional_60s','sell_notional_60s','cvd_60s'):
            v=signed_log(v)
        elif v is not None and n in ('trade_age_ms','book_age_ms','trade_count_60s'):
            v=math.log1p(max(0,v))
        vector.append(float('nan') if v is None else v)
    mtf={x.get('tf'):x for x in (p.get('mtf') or []) if isinstance(x,dict)}
    for tf in TF_NAMES:vector.append(encode_direction((mtf.get(tf) or {}).get('direction')))
    if len(vector)!=len(FEATURE_NAMES):raise AssertionError('Feature width changed')
    return vector


def generate_examples(rows, *, symbol='BTCUSDT', horizon_minutes=HORIZON_MINUTES):
    """Only label after the actual recorded feature availability time.

    A label is emitted only when every intervening 1m candle has been *observed*
    and the future candle close is at least `horizon_minutes` after features.
    No output feature contains any future candle data.
    """
    valid={int(r['candle_open_ms']):r for r in rows if valid_row(r,symbol)}
    examples=[]; skipped=Counter()
    for op,r in sorted(valid.items()):
        if not eligible_row(r):
            skipped['quality']+=1;continue
        f=feature_vector(r['payload'])
        if f is None:
            skipped['features']+=1;continue
        feature_ms=available_time(r)
        target_open=((feature_ms+horizon_minutes*MS-MS+MS-1)//MS)*MS
        if target_open <= op:
            skipped['target_before_feature']+=1;continue
        # Require gapless records, including target. Missing future rows must not
        # silently shift the realized horizon to a much later price.
        if any(t not in valid for t in range(op+MS,target_open+MS,MS)):
            skipped['future_gap_or_pending']+=1;continue
        future=valid[target_open];future_close_ms=target_open+MS
        if future_close_ms<feature_ms+horizon_minutes*MS:
            raise AssertionError('Label leaked into feature horizon')
        entry=float(r['payload']['market_price_at_snapshot'])
        exit_price=float(future['payload']['candle']['c'])
        ratio=exit_price/entry-1
        label=2 if ratio>=COST_NEUTRAL_THRESHOLD else 0 if ratio<=-COST_NEUTRAL_THRESHOLD else 1
        examples.append({'symbol':symbol,'t':op,'feature_ms':feature_ms,
                         'target_ms':future_close_ms,'x':f,'y':label})
    return examples,dict(skipped)


def purged_split(examples):
    a=sorted(examples,key=lambda x:(x['feature_ms'],x['t']))
    n=len(a);i=int(n*.60);j=int(n*.80)
    if i<=0 or j<=i or j>=n:return [],[],[]
    v_start=a[i]['feature_ms'];test_start=a[j]['feature_ms']
    train=[x for x in a[:i] if x['target_ms']<v_start]
    valid=[x for x in a[i:j] if x['target_ms']<test_start]
    test=a[j:]
    return train,valid,test


def count_classes(rows):
    c=Counter(x['y'] for x in rows)
    return {CLASS_NAMES[k]:c[k] for k in CLASSES}


def readiness(rows,examples):
    eligible=[r for r in rows if eligible_row(r) and valid_row(r,'BTCUSDT')]
    days=len({datetime.fromtimestamp(available_time(r)/1000,timezone.utc).date() for r in eligible})
    checks={'eligible_observations':len(eligible)>=MIN_RAW,
            'observed_utc_days':days>=MIN_DAYS,
            'usable_forward_labels':len(examples)>=MIN_LABELED}
    return {'eligible_observations':len(eligible),'utc_days':days,
            'forward_labeled_examples':len(examples),'checks':checks,
            'ready_for_research_training':all(checks.values())}


def train_and_evaluate(examples):
    """Training / validation / final test strictly chronological with purging."""
    import numpy as np
    import lightgbm as lgb
    from sklearn.metrics import accuracy_score, balanced_accuracy_score, confusion_matrix, f1_score, log_loss

    train,valid,test=purged_split(examples)
    if len(train)<MIN_TRAIN or len(valid)<MIN_VALID or len(test)<MIN_TEST:
        return None,{'reason':'Insufficient rows after chronological purge',
                     'train':len(train),'validation':len(valid),'test':len(test)}
    cc=count_classes(train)
    if min(cc.values())<50 or min(count_classes(valid).values())<10:
        return None,{'reason':'At least one class is too rare for supervised training',
                     'train_classes':cc,'validation_classes':count_classes(valid)}
    Xtr=np.asarray([x['x'] for x in train],dtype=np.float64)
    Xv=np.asarray([x['x'] for x in valid],dtype=np.float64)
    Xte=np.asarray([x['x'] for x in test],dtype=np.float64)
    ytr=np.asarray([x['y'] for x in train],dtype=int)
    yv=np.asarray([x['y'] for x in valid],dtype=int)
    yte=np.asarray([x['y'] for x in test],dtype=int)
    model=lgb.LGBMClassifier(objective='multiclass',num_class=3,
        learning_rate=.035,n_estimators=300,num_leaves=15,max_depth=4,
        min_child_samples=70,subsample=.90,subsample_freq=1,
        colsample_bytree=.80,reg_lambda=5,random_state=53,n_jobs=2,verbosity=-1)
    model.fit(Xtr,ytr,eval_set=[(Xv,yv)],eval_metric='multi_logloss',
              callbacks=[lgb.early_stopping(25,first_metric_only=True,verbose=False)])
    pred=model.predict(Xte);proba=model.predict_proba(Xte)
    major=int(Counter(ytr).most_common(1)[0][0]);baseline=np.full(len(yte),major,dtype=int)
    metrics={
        'rows':{'train':len(train),'validation':len(valid),'test_untouched':len(test)},
        'classes':{'train':count_classes(train),'validation':count_classes(valid),'test':count_classes(test)},
        'test_accuracy':round(float(accuracy_score(yte,pred)),5),
        'test_balanced_accuracy':round(float(balanced_accuracy_score(yte,pred)),5),
        'test_macro_f1':round(float(f1_score(yte,pred,labels=CLASSES,average='macro',zero_division=0)),5),
        'test_log_loss':round(float(log_loss(yte,proba,labels=list(model.classes_))),5),
        'baseline_majority_class':CLASS_NAMES[major],
        'baseline_accuracy':round(float(accuracy_score(yte,baseline)),5),
        'baseline_macro_f1':round(float(f1_score(yte,baseline,labels=CLASSES,average='macro',zero_division=0)),5),
        'confusion_matrix_order':['SAT','BEKLE','AL'],
        'confusion_matrix':confusion_matrix(yte,pred,labels=CLASSES).tolist(),
        'best_iteration':int(model.best_iteration_ or model.n_estimators),
        'first_test_feature_ms':test[0]['feature_ms'],
        'last_test_target_ms':test[-1]['target_ms'],
        'label_horizon_minutes':HORIZON_MINUTES,
        'neutral_threshold_bps':COST_NEUTRAL_THRESHOLD*10_000,
        'round_trip_costs_in_performance':False,
        'approved_for_live':False,
        'automatically_used_by_TiMi':False,
    }
    # Deliberately report an exploratory comparison, NOT a live-trading go/no-go.
    metrics['outperforms_majority_baseline_on_test']=(
        metrics['test_macro_f1']>metrics['baseline_macro_f1'] and
        metrics['test_accuracy']>metrics['baseline_accuracy'])
    return model,metrics


def read_archive(dsn,symbol):
    """Read-only transaction; GitHub-hosted runner needs Render EXTERNAL URL."""
    import psycopg
    from psycopg.rows import dict_row
    with psycopg.connect(dsn,sslmode='require',connect_timeout=15,row_factory=dict_row) as conn:
        with conn.cursor() as cur:
            cur.execute('SET TRANSACTION READ ONLY')
            cur.execute('''SELECT symbol, candle_open_ms, candle_close_ms,
                                  observed_ms, payload
                           FROM timi_v5_observations
                           WHERE symbol=%s ORDER BY candle_open_ms ASC''',(symbol,))
            return cur.fetchall()


def run(rows,*,symbol='BTCUSDT'):
    if symbol!='BTCUSDT':raise ValueError('v5.3 baseline only permits BTCUSDT')
    examples,skipped=generate_examples(rows,symbol=symbol)
    ready=readiness(rows,examples)
    report={'engine':'TiMi ML v5.3 offline researcher',
            'schema':'timi-ml-v53-report-v1','symbol':symbol,
            'generated_at_utc':datetime.now(timezone.utc).isoformat(),
            'source':'timi_v5_observations PostgreSQL; observed records only',
            'candidate_features':list(FEATURE_NAMES),
            'label':{'horizon_minutes':HORIZON_MINUTES,'up_bps':15,'down_bps':-15,
                     'starts_after_actual_feature_availability':True,
                     'requires_gapless_future_observed_candles':True},
            'readiness':ready,'skipped':skipped,'status':'waiting_for_data',
            'live_inference':False,'real_orders':False,
            'model_approved':False}
    if not ready['ready_for_research_training']:
        return report,None
    model,result=train_and_evaluate(examples)
    if model is None:
        report.update(status='waiting_for_class_coverage',split_check=result)
    else:
        report.update(status='trained_research_only',metrics=result)
    return report,model


def main():
    symbol=os.environ.get('TIMI_V53_SYMBOL','BTCUSDT').strip().upper()
    if symbol!='BTCUSDT':raise SystemExit('Only BTCUSDT is permitted in v5.3 baseline')
    dsn=os.environ.get('TIMI_V53_DATABASE_URL','').strip()
    if not dsn:raise SystemExit('Missing TIMI_V53_DATABASE_URL secret (Render EXTERNAL Database URL)')
    OUTPUT.mkdir(parents=True,exist_ok=True)
    rows=read_archive(dsn,symbol)
    report,model=run(rows,symbol=symbol)
    (OUTPUT/'report.json').write_text(json.dumps(report,indent=2,ensure_ascii=False,allow_nan=False),encoding='utf-8')
    if model is not None:
        model.booster_.save_model(str(OUTPUT/'model_RESEARCH_ONLY.txt'),
                                   num_iteration=model.best_iteration_)
    # Never print DB URL, raw observations or historical prices in CI logs.
    print('TiMi ML v5.3:',report['status'])
    print('Eligibility:',json.dumps(report['readiness'],ensure_ascii=False))
    if 'metrics' in report:
        m=report['metrics']
        print('Test / majority baseline accuracy:',m['test_accuracy'],m['baseline_accuracy'])
        print('Model remains research-only; no live or simulated orders.')


if __name__=='__main__':main()
