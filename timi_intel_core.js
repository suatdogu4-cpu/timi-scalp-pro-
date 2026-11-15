/* TiMi Intelligence v1.0 — deterministic, read-only analytics. Suat DOĞU.
   Never interprets a rule score as a calibrated trading probability.
   Every candle must have explicit closed=true. No future data in live features.
*/
(function(root,factory){const api=factory();if(typeof module==='object'&&module.exports)module.exports=api;if(root)root.TiMiIntelCore=api;})(typeof globalThis!=='undefined'?globalThis:null,function(){
'use strict';
const STEPS={'1m':60000,'5m':300000,'15m':900000};
const f=Number.isFinite,clamp=(x,a,b)=>Math.max(a,Math.min(b,x));
const number=x=>x===null||x===undefined||x===''?NaN:Number(x);
function bars(raw,tf,now=Date.now()){
 const step=STEPS[tf];if(!step||!Array.isArray(raw))return {ok:false,reason:'Geçersiz zaman dilimi',data:[]};
 const map=new Map();let bad=0;
 for(const x of raw){const t=number(x?.t??x?.timestamp),o=number(x?.o??x?.open),h=number(x?.h??x?.high),l=number(x?.l??x?.low),c=number(x?.c??x?.close),v=number(x?.v??x?.volume);
  if(x?.closed!==true||![t,o,h,l,c,v].every(f)||t<1e11||t+step>now-2000||o<=0||h<Math.max(o,c,l)||l<=0||l>Math.min(o,c)||v<0){bad++;continue;}
  map.set(t,{t,o,h,l,c,v});
 }
 const sorted=[...map.values()].sort((a,b)=>a.t-b.t).slice(-400);let start=0,gaps=0;
 for(let i=1;i<sorted.length;i++)if(sorted[i].t-sorted[i-1].t!==step){start=i;gaps++;}
 const data=sorted.slice(start),age=data.length?now-data.at(-1).t-step:Infinity;
 return {ok:data.length>=80&&age>=0&&age<=2*step+15000,reason:data.length<80?`Ardışık kapanmış mum eksik: ${data.length}/80`:age>2*step+15000?'Mum verisi gecikmiş':age<0?'Henüz kapanmamış mum':'Hazır',data,gaps,bad,age};
}
function mean(a){return a.length?a.reduce((s,x)=>s+x,0)/a.length:NaN;}
function atr(b,n=14,k=b.length-1){if(k<n||!b.length)return NaN;let s=0;for(let i=k-n+1;i<=k;i++)s+=Math.max(b[i].h-b[i].l,Math.abs(b[i].h-b[i-1].c),Math.abs(b[i].l-b[i-1].c));return s/n;}
function ema(b,n,k=b.length-1){if(k<n-1)return NaN;const a=2/(n+1);let r=mean(b.slice(0,n).map(x=>x.c));for(let i=n;i<=k;i++)r=a*b[i].c+(1-a)*r;return r;}
function adx(b,n=14,k=b.length-1){if(k<n*2+1)return NaN;const tr=[],pd=[],md=[];for(let i=1;i<=k;i++){const up=b[i].h-b[i-1].h,dn=b[i-1].l-b[i].l;pd.push(up>dn&&up>0?up:0);md.push(dn>up&&dn>0?dn:0);tr.push(Math.max(b[i].h-b[i].l,Math.abs(b[i].h-b[i-1].c),Math.abs(b[i].l-b[i-1].c)));}const dx=[];for(let end=n-1;end<tr.length;end++){const start=end-n+1,sum=a=>a.slice(start,end+1).reduce((s,x)=>s+x,0);const den=sum(tr);if(den<=0){dx.push(0);continue;}const p=100*sum(pd)/den,m=100*sum(md)/den;dx.push(p+m?100*Math.abs(p-m)/(p+m):0);}return mean(dx.slice(-n));}
function regime(b,k=b.length-1){if(k<60)return {ready:false,reason:'En az 60 mum gerekli'};const a=atr(b,14,k),fast=ema(b,20,k),slow=ema(b,50,k),old=ema(b,20,k-5),strength=adx(b,14,k),aOld=atr(b,14,k-20);if(![a,fast,slow,old,strength,aOld].every(f)||a<=0)return {ready:false,reason:'Rejim hesaplanamadı'};const direction=fast>slow&&fast>old?'LONG':fast<slow&&fast<old?'SHORT':'WAIT';const squeeze=aOld>0&&a/aOld<.73,highvol=aOld>0&&a/aOld>1.85;const kind=highvol?'YÜKSEK VOLATİLİTE':squeeze?'SIKIŞMA':strength>=23&&direction!=='WAIT'?'TREND':'YATAY/KARIŞIK';return {ready:true,kind,direction:kind==='TREND'?direction:'WAIT',adx:strength,atr:a,atrRatio:a/aOld,ema20:fast,ema50:slow,squeeze,highvol};}
function pivots(b,k,span=2){const highs=[],lows=[];for(let i=span;i<=k-span;i++){const sl=b.slice(i-span,i+span+1);if(sl.every((x,j)=>j===span||b[i].h>x.h))highs.push(i);if(sl.every((x,j)=>j===span||b[i].l<x.l))lows.push(i);}return {highs,lows};}
function smc(b,k=b.length-1){if(k<42)return {ready:false,reason:'SMC için en az 43 mum'};const p=pivots(b,k,2),a=atr(b,14,k);if(!f(a)||!p.highs.length||!p.lows.length)return {ready:false,reason:'Pivot verisi yetersiz'};
 const lo=p.lows.at(-1),hi=p.highs.at(-1);let lastSweep=null,lastBreak=null,lastFVG=null;
 // Causal: pivots recognized no earlier than pivot time + 2 closed candles.
 for(let i=Math.max(25,k-40);i<=k;i++){
  const ah=p.highs.filter(x=>x+2<i).at(-1),al=p.lows.filter(x=>x+2<i).at(-1);
  if(ah!==undefined&&b[i].h>b[ah].h&&b[i].c<b[ah].h)lastSweep={side:'SHORT',at:i,level:b[ah].h};
  if(al!==undefined&&b[i].l<b[al].l&&b[i].c>b[al].l)lastSweep={side:'LONG',at:i,level:b[al].l};
  if(ah!==undefined&&b[i].c>b[ah].h&&b[i].c>b[i].o)lastBreak={side:'LONG',at:i,level:b[ah].h};
  if(al!==undefined&&b[i].c<b[al].l&&b[i].c<b[i].o)lastBreak={side:'SHORT',at:i,level:b[al].l};
  if(i>=2&&b[i].l>b[i-2].h&&b[i-1].c>b[i-1].o)lastFVG={side:'LONG',at:i,lower:b[i-2].h,upper:b[i].l};
  if(i>=2&&b[i].h<b[i-2].l&&b[i-1].c<b[i-1].o)lastFVG={side:'SHORT',at:i,lower:b[i].h,upper:b[i-2].l};
 }
 const chain=lastSweep&&lastBreak&&lastFVG&&lastSweep.side===lastBreak.side&&lastBreak.side===lastFVG.side&&lastSweep.at<lastBreak.at&&lastBreak.at<=lastFVG.at&&k-lastFVG.at<=12;
 const fvgActive=lastFVG&&!b.slice(lastFVG.at+1,k+1).some(x=>lastFVG.side==='LONG'?x.l<=lastFVG.lower:x.h>=lastFVG.upper);
 return {ready:true,chain:!!(chain&&fvgActive),direction:chain&&fvgActive?lastBreak.side:'WAIT',sweep:lastSweep,break:lastBreak,fvg:lastFVG,fvgActive:!!fvgActive,atr:a,latestPivotHigh:b[hi].h,latestPivotLow:b[lo].l};
}
function breakout(b,k=b.length-1){if(k<35)return {ready:false};const hist=b.slice(k-20,k),hi=Math.max(...hist.map(x=>x.h)),lo=Math.min(...hist.map(x=>x.l)),cur=b[k],a=atr(b,14,k),vAvg=mean(hist.map(x=>x.v));const volumeRatio=vAvg>0?cur.v/vAvg:NaN;
 const confirmed=cur.c>hi&&cur.c-cur.o>.25*a&&volumeRatio>1.15?'LONG':cur.c<lo&&cur.o-cur.c>.25*a&&volumeRatio>1.15?'SHORT':'WAIT';
 const failed=(cur.h>hi&&cur.c<=hi)?'SHORT':(cur.l<lo&&cur.c>=lo)?'LONG':'WAIT';
 return {ready:true,confirmed,failed,volumeRatio,upper:hi,lower:lo};}
function vwap(b,anchor){if(!b.length)return {ready:false};const from=clamp(anchor,0,b.length-1);let w=0,vol=0;for(let i=from;i<b.length;i++){const x=b[i];w+=((x.h+x.l+x.c)/3)*x.v;vol+=x.v;}return {ready:vol>0,value:vol>0?w/vol:NaN,anchorTime:b[from].t,source:'Kapanmış mum HLC3 / yaklaşık VWAP'};}
function volumeProfile(trades,buckets=16){if(!Array.isArray(trades)||trades.length<20)return {ready:false,reason:'Gerçek trade akışı eksik'};const valid=trades.filter(t=>f(t.p)&&f(t.q)&&t.p>0&&t.q>0);if(valid.length<20)return {ready:false,reason:'İşlem miktarı yetersiz'};const lo=Math.min(...valid.map(x=>x.p)),hi=Math.max(...valid.map(x=>x.p)),step=Math.max((hi-lo)/buckets,lo*.00000001);const bins=Array(buckets).fill(0);for(const t of valid)bins[clamp(Math.floor((t.p-lo)/step),0,buckets-1)]+=t.q;let i=bins.indexOf(Math.max(...bins));return {ready:true,poc:lo+(i+.5)*step,volume:bins[i],buckets:bins,source:'Gerçekleşmiş işlemler; yalnız gözlemlenen pencere'};}
function analyzeFlow(trades,now=Date.now()){if(!Array.isArray(trades))return {ready:false,reason:'Trade kaynağı yok'};const a=trades.filter(x=>now-x.t<=300000&&now-x.t>=0&&f(x.q)&&x.q>0&&f(x.p)&&x.p>0);if(a.length<20)return {ready:false,reason:`En az 20 gözlemlenen işlem gerekli (${a.length})`};let buy=0,sell=0;for(const t of a)(t.aggressor==='BUY'?buy+=t.q:sell+=t.q);const cvd=buy-sell,ratio=(buy-sell)/(buy+sell),deltaPrice=(a.at(-1).p-a[0].p)/a[0].p;const absorp=(Math.abs(ratio)>.3&&Math.abs(deltaPrice)<.00045)?(ratio>0?'OLASI SATIŞ ABSORPSİYONU':'OLASI ALIŞ ABSORPSİYONU'):'DOĞRULANMADI';return {ready:true,count:a.length,buy,sell,cvd,ratio,priceChange:deltaPrice,absorption:absorp,source:'Oturum boyunca görülen trade akışı; geçmiş veri değildir'};}
function depthProxy(snapshot){const bids=snapshot?.bids,asks=snapshot?.asks;if(!Array.isArray(bids)||!Array.isArray(asks)||!bids.length||!asks.length)return {ready:false,reason:'Derinlik snapshot yok'};const sum=a=>a.slice(0,20).reduce((s,x)=>s+number(x[1]),0),b=sum(bids),a=sum(asks);if(![a,b].every(f)||a+b<=0)return {ready:false,reason:'Derinlik miktarları bozuk'};return {ready:true,ratio:(b-a)/(a+b),bid:b,ask:a,source:'Kısmi 20-kademe miktar dengesizliği; gerçek OFI değildir',isOFI:false};}
function futures(spot,info,now=Date.now()){const p=number(info?.price),oi=number(info?.oi),funding=number(info?.funding),stamp=number(info?.timestamp);if(!f(p)||p<=0||!f(spot)||spot<=0||!f(stamp)||now-stamp>120000||now<stamp-10000)return {ready:false,reason:'Zaman uyumlu Futures verisi yok'};const basis=(p-spot)/spot*100;return {ready:true,basisPct:basis,oi:f(oi)?oi:null,funding:f(funding)?funding:null,direction:basis>0.1?'LONG':basis<-.1?'SHORT':'WAIT',source:String(info.source||'Futures kaynağı'),timestamp:stamp};}
function features(b,k){if(k<60)return null;const r=regime(b,k),s=smc(b,k),q=breakout(b,k),a=r.atr;if(!r.ready||!s.ready||!q.ready||!f(a)||a<=0)return null;return [clamp((r.ema20-r.ema50)/a,-5,5),clamp((b[k].c-b[k-5].c)/a,-5,5),clamp(r.adx/50,0,2),s.direction==='LONG'?1:s.direction==='SHORT'?-1:0,q.confirmed==='LONG'?1:q.confirmed==='SHORT'?-1:0,clamp(b[k].v/(mean(b.slice(k-20,k).map(x=>x.v))||1),0,4)-1];}
function label(b,k,horizon=8){const a=atr(b,14,k);if(!f(a)||a<=0||k+horizon>=b.length)return null;const take=1.2*a,stop=1.0*a,entry=b[k].c;let outcome=0;
 for(let j=k+1;j<=k+horizon;j++){const hi=b[j].h,lo=b[j].l;if(hi>=entry+take&&lo<=entry-stop)return null; // ambiguous intrabar path, exclude
  if(hi>=entry+take){outcome=1;break;}if(lo<=entry-stop){outcome=0;break;}
 }
 return outcome;
}
const sigmoid=z=>1/(1+Math.exp(-clamp(z,-30,30)));
function trainMeta(b){const horizon=8,rows=[];for(let i=70;i<b.length-horizon;i++){const x=features(b,i),y=label(b,i,horizon);if(x&&y!==null)rows.push({i,x,y});}
 const split=Math.floor(rows.length*.76),train=rows.filter(x=>x.i<rows[split]?.i-horizon),test=rows.slice(split);
 if(train.length<110||test.length<30)return {ready:false,reason:`Kalibrasyon için örnek yetersiz (eğitim ${train.length}, test ${test.length})`};
 const avg=train.map(x=>x.y).reduce((a,b)=>a+b,0)/train.length;
 if(avg<.08||avg>.92)return {ready:false,reason:'Sınıf dengesi yetersiz'};
 const mu=Array(6).fill(0).map((_,j)=>mean(train.map(x=>x.x[j]))),sd=mu.map((m,j)=>Math.max(.01,Math.sqrt(mean(train.map(x=>(x.x[j]-m)**2)))));
 const z=x=>x.map((v,j)=>clamp((v-mu[j])/sd[j],-4,4));let w=Array(7).fill(0);w[0]=Math.log(avg/(1-avg));
 // Historical train only. No test labels are used in fitting.
 for(let epoch=0;epoch<180;epoch++){const grad=Array(7).fill(0);for(const row of train){const xx=[1,...z(row.x)],err=sigmoid(xx.reduce((s,v,j)=>s+v*w[j],0))-row.y;for(let j=0;j<7;j++)grad[j]+=err*xx[j];}for(let j=0;j<7;j++)w[j]-=.08*(grad[j]/train.length+(j?0.03*w[j]:0));}
 const predict=x=>sigmoid([1,...z(x)].reduce((s,v,j)=>s+v*w[j],0)),brier=mean(test.map(r=>(predict(r.x)-r.y)**2)),base=mean(test.map(r=>(avg-r.y)**2));
 const live=features(b,b.length-1),liveProb=live?predict(live):null;
 return {ready:true,trainCount:train.length,testCount:test.length,brier,baseBrier:base,improved:brier<base,prob:liveProb,calibrated:false,model:'L2 lojistik meta-model; kronolojik ayrım + 8 mum purge',warning:'Tek pencere yeterli değildir: ek dönemlerde walk-forward ve olasılık kalibrasyonu gereklidir.'};
}
function signal(b,k,strategy){const r=regime(b,k),q=breakout(b,k),s=smc(b,k);if(!r.ready||!q.ready||!s.ready)return 'WAIT';if(strategy==='MTF Trend')return r.kind==='TREND'?r.direction:'WAIT';if(strategy==='SMC Zinciri')return s.chain?s.direction:'WAIT';if(strategy==='Breakout')return q.confirmed;if(strategy==='Birleşik')return r.kind==='TREND'&&r.direction!=='WAIT'&&(s.direction===r.direction||q.confirmed===r.direction)?r.direction:'WAIT';return 'WAIT';}
function backtest(b,strategy,feeBps=8){const h=8,records=[];for(let i=80;i<b.length-h;i++){const dir=signal(b,i,strategy);if(dir==='WAIT')continue;const a=atr(b,14,i),entry=b[i].c;if(!f(a)||a<=0)continue;const stop=a,take=1.2*a;let exit=b[i+h].c,ambiguous=false;for(let j=i+1;j<=i+h;j++){const x=b[j],hitT=dir==='LONG'?x.h>=entry+take:x.l<=entry-take,hitS=dir==='LONG'?x.l<=entry-stop:x.h>=entry+stop;if(hitT&&hitS){ambiguous=true;exit=entry+(dir==='LONG'?-stop:stop);break;}if(hitS){exit=entry+(dir==='LONG'?-stop:stop);break;}if(hitT){exit=entry+(dir==='LONG'?take:-take);break;}}
 const gross=(dir==='LONG'?1:-1)*(exit-entry)/entry,net=gross-2*feeBps/10000;records.push({i,net,win:net>0,ambiguous});i+=h;}
 if(!records.length)return {ready:false,reason:'Test döneminde kurala uygun giriş oluşmadı',strategy};return {ready:true,strategy,count:records.length,wins:records.filter(x=>x.win).length,winRate:records.filter(x=>x.win).length/records.length,net:records.reduce((s,x)=>s+x.net,0),ambiguous:records.filter(x=>x.ambiguous).length,feeBps,experimental:true};}
function decision(r,sm,br,flow,fu){const reasons=[];if(!r?.ready||!sm?.ready||!br?.ready)return {direction:'BEKLE',reason:'Doğrulanmış kapanmış mum/analiz eksik',reasons};
 if(r.kind!=='TREND'){reasons.push('Trend rejimi doğrulanmadı');return {direction:'BEKLE',reason:reasons.join(' · '),reasons};}
 const side=r.direction;if(side==='WAIT')return {direction:'BEKLE',reason:'EMA yönü belirsiz',reasons};
 if(br.failed===side){reasons.push('Başarısız kırılım uyarısı');return {direction:'BEKLE',reason:reasons.join(' · '),reasons};}
 if(sm.chain&&sm.direction!==side){reasons.push('SMC yön çatışması');return {direction:'BEKLE',reason:reasons.join(' · '),reasons};}
 const confirm=sm.chain&&sm.direction===side||br.confirmed===side;if(!confirm){reasons.push('SMC veya kırılım teyidi yok');return {direction:'BEKLE',reason:reasons.join(' · '),reasons};}
 if(flow?.ready&&((side==='LONG'&&flow.ratio<-.23)||(side==='SHORT'&&flow.ratio>.23))){reasons.push('Gerçek işlem delta yönü ters');return {direction:'BEKLE',reason:reasons.join(' · '),reasons};}
 if(fu?.ready&&fu.direction!=='WAIT'&&fu.direction!==side){reasons.push('Spot/Futures ayrışması');return {direction:'BEKLE',reason:reasons.join(' · '),reasons};}
 reasons.push('Trend + yapı/kırılım teyidi');if(flow?.ready)reasons.push('Gerçek işlem deltası izlendi');if(!flow?.ready)reasons.push('Order Flow teyidi yok');if(!fu?.ready)reasons.push('Futures teyidi yok');return {direction:side,reason:reasons.join(' · '),reasons,provisional:!flow?.ready||!fu?.ready};
}
return {STEPS,bars,mean,atr,ema,adx,regime,smc,breakout,vwap,volumeProfile,analyzeFlow,depthProxy,futures,features,label,trainMeta,signal,backtest,decision};
});
