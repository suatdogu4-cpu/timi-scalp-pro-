/* TiMi PRO v2.0 — analytical primitives. No trading, storage, or credentials.
 * 1) Regime/adaptive  2) Confirmed candle SMC  3) Data conflict/risk shield.
 * Data source: Binance Spot/verified public derivative endpoints.
 */
(function(root){
'use strict';
const STEP={'1m':60000,'5m':300000,'15m':900000};
const number=x=>x===null||x===undefined||x===''?NaN:Number(x);
const finite=Number.isFinite;
const safe=(x,d=2)=>finite(x)?x.toLocaleString('tr-TR',{maximumFractionDigits:d}):'—';
function candles(data,tf,now=Date.now()){
 const ms=STEP[tf],out=new Map();if(!ms)return [];
 for(const v of Array.isArray(data)?data:[]){
   const t=number(v.t??v.timestamp),o=number(v.o??v.open),h=number(v.h??v.high),
   l=number(v.l??v.low),c=number(v.c??v.close),q=number(v.v??v.volume??0);
   if(![t,o,h,l,c,q].every(finite)||t<1e11||o<=0||l<=0||h<Math.max(o,c,l)||l>Math.min(o,c,h)||q<0)continue;
   if(v.closed!==true || t+ms>now-1500 || t+ms<now-ms*400)continue;
   out.set(t,{t,o,h,l,c,v:q});
 }
 return [...out.values()].sort((a,b)=>a.t-b.t);
}
function ema(arr,p){let result=Array(arr.length).fill(NaN);if(arr.length<p)return result;let v=arr.slice(0,p).reduce((s,x)=>s+x,0)/p;result[p-1]=v;for(let i=p;i<arr.length;i++){v=arr[i]*(2/(p+1))+v*(1-2/(p+1));result[i]=v;}return result;}
function atr(b,p=14){if(b.length<=p)return NaN;let sum=0;for(let i=b.length-p;i<b.length;i++){sum+=Math.max(b[i].h-b[i].l,Math.abs(b[i].h-b[i-1].c),Math.abs(b[i].l-b[i-1].c));}return sum/p;}
function rsi(b,p=14){if(b.length<=p)return NaN;let gain=0,loss=0;for(let i=1;i<=p;i++){const dif=b[i].c-b[i-1].c;gain+=Math.max(0,dif);loss+=Math.max(0,-dif);}gain/=p;loss/=p;for(let i=p+1;i<b.length;i++){const dif=b[i].c-b[i-1].c;gain=(gain*(p-1)+Math.max(0,dif))/p;loss=(loss*(p-1)+Math.max(0,-dif))/p;}return loss?100-100/(1+gain/loss):gain?100:50;}
function tfState(data,tf,now=Date.now()){
 const b=candles(data,tf,now),ms=STEP[tf],end=b.at(-1)?.t+ms,age=now-end;
 if(b.length<100||!finite(age)||age<0||age>2*ms+10000)return {tf,ready:false,stale:true,reason:'Yetersiz veya gecikmiş kapanmış mum',count:b.length,bars:b};
 const close=b.map(x=>x.c),e20=ema(close,20).at(-1),e50=ema(close,50).at(-1),r=rsi(b),a=atr(b);
 const price=b.at(-1).c;
 const dir=e20>e50&&price>e20&&r>51?'LONG':e20<e50&&price<e20&&r<49?'SHORT':'WAIT';
 return {tf,ready:true,stale:false,direction:dir,close:price,closeAt:end,age_ms:age,
  atr:a,atrPct:a/price*100,rsi:r,ema20:e20,ema50:e50,count:b.length,bars:b};
}
function smcExtended(bars,tf){
 if(!STEP[tf]||!Array.isArray(bars)||bars.length<30)return {ready:false,blocks:[],sweeps:[],equalHighs:[],equalLows:[],range:null};
 const breaks=[];
 const highs=[],lows=[],sweeps=[],equalHighs=[],equalLows=[];
 let lastH=null,lastL=null,brokenH=null,brokenL=null,segment=0;
 for(let i=0;i<bars.length;i++){
  const b=bars[i],prev=bars[i-1];
  const localAtr=atr(bars.slice(0,i+1)),tol=finite(localAtr)?localAtr*.20:0;
  if(prev && b.t-prev.t!==STEP[tf]){segment=i;lastH=lastL=brokenH=brokenL=null;}
  const p=i-2;
  if(p>=segment+2){
    const v=bars[p];let hi=true,lo=true;
    for(let j=p-2;j<=p+2;j++){if(j===p)continue;if(bars[j].h>=v.h)hi=false;if(bars[j].l<=v.l)lo=false;}
    if(hi){
      if(lastH && Math.abs(lastH.price-v.h)<=tol)equalHighs.push({price:v.h,time:b.t,pivot:v.t});
      lastH={price:v.h,time:v.t};highs.push(lastH);
    }
    if(lo){
      if(lastL && Math.abs(lastL.price-v.l)<=tol)equalLows.push({price:v.l,time:b.t,pivot:v.t});
      lastL={price:v.l,time:v.t};lows.push(lastL);
    }
  }
  if(!prev)continue;
  const buff=Math.max(b.c*.000015,(finite(localAtr)?localAtr:0)*.015);
  if(lastH && lastH.time!==brokenH && prev.c<=lastH.price+buff && b.c>lastH.price+buff){
    breaks.push({direction:'LONG',time:b.t,pivotTime:lastH.time,price:lastH.price,index:i});brokenH=lastH.time;
  }
  if(lastL && lastL.time!==brokenL && prev.c>=lastL.price-buff && b.c<lastL.price-buff){
    breaks.push({direction:'SHORT',time:b.t,pivotTime:lastL.time,price:lastL.price,index:i});brokenL=lastL.time;
  }
  if(lastH && b.h>lastH.price+buff && b.c<lastH.price && prev.c<=lastH.price){
    sweeps.push({side:'UPPER',price:lastH.price,time:b.t,wicks:true});
  }
  if(lastL && b.l<lastL.price-buff && b.c>lastL.price && prev.c>=lastL.price){
    sweeps.push({side:'LOWER',price:lastL.price,time:b.t,wicks:true});
  }
 }
 const blocks=[];
 for(const k of breaks.slice(-20)){
  const from=Math.max(0,k.index-12);let source=null;
  for(let i=k.index-1;i>=from;i--){
    if(k.direction==='LONG' && bars[i].c<bars[i].o){source=bars[i];break;}
    if(k.direction==='SHORT' && bars[i].c>bars[i].o){source=bars[i];break;}
  }
  if(!source)continue;
  const lower=source.l,upper=source.h;
  // A hypothesis is invalidated by a later closed candle beyond the source candle.
  const invalid=bars.slice(k.index+1).some(x=>k.direction==='LONG'?x.c<lower:x.c>upper);
  const touched=bars.slice(k.index+1).some(x=>x.l<=upper&&x.h>=lower);
  blocks.push({side:k.direction,lower,upper,createdAt:k.time,sourceAt:source.t,
   status:invalid?'GECERSIZ':touched?'TEMAS_EDILDI':'ADAY'});
 }
 const look=bars.slice(-100),high=Math.max(...look.map(x=>x.h)),low=Math.min(...look.map(x=>x.l));
 const midpoint=(high+low)/2,price=bars.at(-1).c;
 return {ready:true,blocks:blocks.slice(-10),sweeps:sweeps.slice(-12),equalHighs:equalHighs.slice(-8),equalLows:equalLows.slice(-8),
   range:{high,low,midpoint,price,zone:price>midpoint?'PREMIUM':price<midpoint?'DISCOUNT':'DENGE'},
   latestBreak:breaks.at(-1)||null,at:bars.at(-1).t};
}
function flowRegime(flow,now=Date.now()){
 if(!finite(flow?.time_ms)||now-flow.time_ms>7000||now-flow.time_ms<0||!flow?.status?.trades_fresh||!flow?.status?.book_fresh)return {ready:false,direction:'WAIT',reasons:['Akış veya emir defteri gecikmiş']};
 const w=flow.windows?.['60'],w15=flow.windows?.['15'],b=flow.book;
 if(!w?.complete_window||!w15?.complete_window||w.trade_count<10||!finite(w.aggressor_balance_pct)||!finite(b?.imbalance_pct))return {ready:false,direction:'WAIT',reasons:['Akış gözlem penceresi henüz dolmadı']};
 const bdir=b.imbalance_pct>15?'LONG':b.imbalance_pct<-15?'SHORT':'WAIT';
 const tdir=w.aggressor_balance_pct>12?'LONG':w.aggressor_balance_pct<-12?'SHORT':'WAIT';
 const clash=bdir!=='WAIT'&&tdir!=='WAIT'&&bdir!==tdir;
 const direction=clash?'WAIT':tdir===bdir?tdir:'WAIT';
 return {ready:true,direction,book:bdir,tape:tdir,clash,
  reasons:clash?['Bekleyen likidite ile gerçekleşmiş işlemler ters yönde']:
   (direction==='WAIT'?['İşlem ve defter baskısı ortak yönde değil']:['İşlem ve defter baskısı aynı yönde']),
  delta:w.delta_usdt,imbalance:b.imbalance_pct,absorption:flow.absorption_candidate};
}
function adaptive(states,flow,smc,futures){
 const one=states?.['1m'],five=states?.['5m'],fifteen=states?.['15m'];
 const ready=Boolean(one?.ready&&five?.ready&&fifteen?.ready);
 const f=flowRegime(flow),s=smc?.ready?smc:null;
 const trend=ready && [one,five,fifteen].every(x=>x.direction==='LONG')?'LONG':
   ready && [one,five,fifteen].every(x=>x.direction==='SHORT')?'SHORT':'WAIT';
 const momentum=one?.ready?(one.rsi>=58?'LONG':one.rsi<=42?'SHORT':'WAIT'):'WAIT';
 const reversion=one?.ready?(one.rsi<28?'LONG':one.rsi>72?'SHORT':'WAIT'):'WAIT';
 const breakout=s?.latestBreak && s.at-s.latestBreak.time<STEP['1m']*25?s.latestBreak.direction:'WAIT';
 const smcDirection=s?.latestBreak && s.at-s.latestBreak.time<STEP['1m']*30?s.latestBreak.direction:'WAIT';
 const flowDirection=f.ready?f.direction:'WAIT';
 const families={trend,momentum,reversion,breakout,smc:smcDirection,flow:flowDirection};
 const votes={LONG:0,SHORT:0,WAIT:0};Object.values(families).forEach(x=>votes[x]++);
 const conflict=votes.LONG>0&&votes.SHORT>0;
 const state=one?.ready && one.atrPct>1.3?'YÜKSEK VOLATİLİTE':
   trend!=='WAIT'&&one?.ready&&one.atr>0&&Math.abs(one.ema20-one.ema50)/one.atr>0.6?'TREND':
   'KARIŞIK/YATAY';
 let conclusion='WAIT';let causes=[];
 if(!ready)causes.push('MTF kapanmış mumlar eksik veya gecikmiş');
 if(!f.ready)causes.push('Canlı emir akışı doğrulanmadı');
 if(ready&&trend==='WAIT')causes.push('1m/5m/15m teknik yön tam uyumlu değil');
 if(conflict)causes.push('Strateji ailelerinde AL/SAT çelişkisi var');
 if(f.clash)causes.push('Defter/işlem akışı çelişiyor');
 if(ready&&f.ready&&trend!=='WAIT'&&!conflict&&flowDirection===trend&&votes[trend]>=3)conclusion=trend;
 if(conclusion==='WAIT'&&!causes.length)causes.push('Ortak doğrulama için yeterli destek yok');
 if(conclusion!=='WAIT')causes.push('Trend, piyasa yapısı/strateji aileleri ve akış koşulları aynı yönde');
 const fund=futures?.binance?.funding_rate_pct;
 if(finite(fund) && Math.abs(fund)>=.05)causes.push('Vadeli funding yüksek; kalabalık pozisyon riski göstergesi');
 return {ready,conclusion,regime:state,mtfDirection:trend,families,votes,conflict,flow:f,causes,read_only:true,
  note:'ARAŞTIRMA YÖNÜDÜR; model olasılığı veya gerçek TiMi sinyali değildir'};
}
function riskShield(states,flow,adaptiveResult,inputs={}){
 const one=states?.['1m'],data=flow?.book;
 const fee=number(inputs.feePct),slip=number(inputs.slipPct);
 const feeOk=finite(fee)&&fee>=0&&fee<=.5&&finite(slip)&&slip>=0&&slip<=.5;
 const spread=number(data?.spread_pct);
 const roundtrip=feeOk&&finite(spread)?2*(fee+slip)+spread:null;
 const atrPct=one?.ready?one.atrPct:null;
 const price=number(flow?.market_price)||one?.close;
 const stop=one?.ready&&finite(price)?one.atr*1.5:null;
 const tp=one?.ready&&finite(price)?one.atr*2.5:null;
 const direction=adaptiveResult?.conclusion||'WAIT';
 const sign=direction==='LONG'?1:direction==='SHORT'?-1:0;
 const ref=sign&&finite(stop)&&finite(tp)?{
   entry:price,stop:price-sign*stop,target:price+sign*tp,
   rewardRisk:tp/stop,atr_pct:atrPct,roundtrip_cost_pct:roundtrip}:null;
 const veto=[];
 if(!one?.ready || !adaptiveResult?.ready)veto.push('Kapanmış mum ve MTF verisi yetersiz');
 if(!finite(flow?.time_ms)||Date.now()-flow.time_ms>7000||!flow?.status?.book_fresh || !flow?.status?.trades_fresh)veto.push('Canlı işlem/derinlik gecikmiş');
 if(!adaptiveResult?.flow?.ready)veto.push('Gözlem penceresi yetersiz');
 if(!feeOk)veto.push('Maliyet parametreleri geçersiz');
 if(!finite(spread)||spread>0.12)veto.push('Spread geçersiz veya %0,12 üzerinde');
 if(finite(atrPct)&&atrPct>2.5)veto.push('1m ATR volatilitesi %2,5 üzerinde');
 if(finite(roundtrip)&&finite(atrPct)&&roundtrip>Math.max(atrPct*.75,.08))veto.push('Maliyet ATR ölçeğine göre çok yüksek');
 if(adaptiveResult?.conflict)veto.push('Çelişen yön sinyalleri');
 if(direction==='WAIT')veto.push('Ortak araştırma yönü yok');
 return {approved:veto.length===0 && sign!==0,decision:veto.length?'WAIT':direction,
  veto,reference:ref,roundtripCostPct:roundtrip,atrPct,read_only:true,
  disclaimer:'Risk planı sadece varsayımsal analizdir. Emir, kâr tahmini veya garantili başarı olasılığı değildir.'};
}
const exported={STEP,candles,ema,atr,rsi,tfState,smcExtended,flowRegime,adaptive,riskShield};
if(typeof module==='object'&&module.exports)module.exports=exported;
if(root)root.TiMiV20Core=Object.freeze(exported);
})(typeof window!=='undefined'?window:null);
