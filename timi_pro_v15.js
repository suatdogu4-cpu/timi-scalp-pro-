/* TiMi PRO LAB v1.5 — Live multi-strategy alignment and contradiction checks.
 * Developer: Suat DOĞU. Read-only research, no trade placement or signal archive.
 * Existing TiMi signal engine, KLineChart, v1.4 PRO LAB and Gateway unchanged.
 * Prices: confirmed Binance Spot candles; flow: existing TiMi WS /api/analysis-pro.
 */
(function(root){
'use strict';
const d=root?.document;
const TF={'1m':60000,'5m':300000,'15m':900000};
const SYMS=['BTCUSDT','ETHUSDT','SOLUSDT','BNBUSDT','XRPUSDT','DOGEUSDT','ADAUSDT','AVAXUSDT'];
const n=x=>x===null||x===undefined||x===''?NaN:Number(x);
const ok=Number.isFinite;
const $=id=>d?.getElementById(id);
const display=v=>ok(v)?v.toLocaleString('tr-TR',{maximumFractionDigits:3}):'—';
const fixed=(v,digits=2)=>ok(v)?v.toLocaleString('tr-TR',{minimumFractionDigits:digits,maximumFractionDigits:digits}):'—';
const label={LONG:'YUKARI',SHORT:'AŞAĞI',WAIT:'BEKLE'};
const names={
 ema9:'EMA 9/21 · Hızlı trend',ema20:'EMA 20/50 · Trend',sma200:'SMA 50/200 · Ana trend',
 rsiRev:'RSI14 · Aşırı bölge',rsiMom:'RSI14 · Momentum',macd:'MACD 12/26/9',
 bollRev:'Bollinger · Ortalamaya dönüş',bollBreak:'Bollinger · Taşma',
 vwap:'VWAP · Son 50 mum',donchian:'Donchian · 20 mum kırılım',
 roc:'ROC10 · Fiyat momentumu',atr:'ATR14 · Güçlü mum',
 structure:'SMC · BOS/CHoCH',fvg:'SMC · FVG yakınlığı',
 tape:'Akış · Agresif alım/satım',book:'Akış · İlk 20 seviye'};
const families={trend:'Trend',momentum:'Momentum',reversion:'Dönüş',breakout:'Kırılım',smc:'SMC',flow:'Order Flow'};
const FAMILIES={ema9:'trend',ema20:'trend',sma200:'trend',rsiRev:'reversion',rsiMom:'momentum',
 macd:'momentum',bollRev:'reversion',bollBreak:'breakout',vwap:'trend',donchian:'breakout',
 roc:'momentum',atr:'breakout',structure:'smc',fvg:'smc',tape:'flow',book:'flow'};
function validateCandles(rows,tf,reference=Date.now()){
 const step=TF[tf];if(!step)return [];
 const clean=new Map();
 for(const b of Array.isArray(rows)?rows:[]){
  const t=n(b?.t??b?.timestamp),o=n(b?.o??b?.open),h=n(b?.h??b?.high),l=n(b?.l??b?.low),c=n(b?.c??b?.close),v=n(b?.v??b?.volume??0);
  if(![t,o,h,l,c,v].every(ok)||t<1e11||o<=0||l<=0||h<Math.max(o,c,l)||l>Math.min(o,c,h)||v<0||b?.closed!==true||t+step>reference-2000)continue;
  clean.set(t,{t,o,h,l,c,v});
 }
 return [...clean.values()].sort((a,b)=>a.t-b.t);
}
function ema(arr,p){const a=Array(arr.length).fill(NaN);if(arr.length<p)return a;let v=arr.slice(0,p).reduce((s,x)=>s+x,0)/p;a[p-1]=v;for(let i=p;i<arr.length;i++){v=arr[i]*(2/(p+1))+v*(1-2/(p+1));a[i]=v;}return a;}
function sma(arr,p){return arr.length>=p?arr.slice(-p).reduce((s,x)=>s+x,0)/p:NaN;}
function rsi(arr,p=14){if(arr.length<p+1)return NaN;let g=0,l=0;for(let i=1;i<=p;i++){const delta=arr[i]-arr[i-1];g+=Math.max(0,delta);l+=Math.max(0,-delta);}g/=p;l/=p;for(let i=p+1;i<arr.length;i++){const z=arr[i]-arr[i-1];g=(g*(p-1)+Math.max(0,z))/p;l=(l*(p-1)+Math.max(0,-z))/p;}return l===0?(g===0?50:100):100-100/(1+g/l);}
function atr(bars,p=14){if(bars.length<p+1)return NaN;let sum=0;for(let i=bars.length-p;i<bars.length;i++){const b=bars[i],prev=bars[i-1];sum+=Math.max(b.h-b.l,Math.abs(b.h-prev.c),Math.abs(b.l-prev.c));}return sum/p;}
function vote(condBuy,condSell){return condBuy&&!condSell?'LONG':condSell&&!condBuy?'SHORT':'WAIT';}
function analyzeTechnical(bars){
 if(!Array.isArray(bars)||bars.length<220)return {rows:[],ready:false,reason:'En az 220 kapanmış mum gerekli'};
 const c=bars.map(x=>x.c),v=bars.map(x=>x.v),current=bars.at(-1),last=c.at(-1);
 const e9=ema(c,9).at(-1),e21=ema(c,21).at(-1),e20=ema(c,20).at(-1),e50=ema(c,50).at(-1);
 const s50=sma(c,50),s200=sma(c,200),r=rsi(c),macFast=ema(c,12),macSlow=ema(c,26);
 const mac=macFast.map((x,i)=>ok(x)&&ok(macSlow[i])?x-macSlow[i]:NaN);
 const macValid=mac.filter(ok),macSignal=ema(macValid,9).at(-1),macNow=mac.at(-1);
 const bbMid=sma(c,20),bbVariance=c.slice(-20).reduce((s,x)=>s+(x-bbMid)**2,0)/20;
 const bbDeviation=2*Math.sqrt(bbVariance),bbUpper=bbMid+bbDeviation,bbLower=bbMid-bbDeviation;
 const prev20=bars.slice(-21,-1),top=Math.max(...prev20.map(b=>b.h)),bottom=Math.min(...prev20.map(b=>b.l));
 const volAvg=sma(v,20),atrNow=atr(bars),previous=c.at(-11),roc=(last/previous-1)*100;
 const vwapBars=bars.slice(-50),vwapVolume=vwapBars.reduce((s,b)=>s+b.v,0);
 const vwap=vwapVolume>0?vwapBars.reduce((s,b)=>s+(b.h+b.l+b.c)/3*b.v,0)/vwapVolume:NaN;
 const add=(id,value,detail)=>({id,name:names[id],family:FAMILIES[id],value,detail});
 const rows=[
  add('ema9',vote(last>e9&&e9>e21,last<e9&&e9<e21),`EMA9 ${fixed(e9)} · EMA21 ${fixed(e21)}`),
  add('ema20',vote(last>e20&&e20>e50,last<e20&&e20<e50),`EMA20 ${fixed(e20)} · EMA50 ${fixed(e50)}`),
  add('sma200',vote(s50>s200&&last>s50,s50<s200&&last<s50),`SMA50 ${fixed(s50)} · SMA200 ${fixed(s200)}`),
  add('rsiRev',vote(r<30,r>70),`RSI14 ${fixed(r,1)} · karşı-trend adayı`),
  add('rsiMom',vote(r>55&&last>e20,r<45&&last<e20),`RSI14 ${fixed(r,1)}`),
  add('macd',vote(macNow>macSignal&&macNow>0,macNow<macSignal&&macNow<0),`MACD ${fixed(macNow,4)} · sinyal ${fixed(macSignal,4)}`),
  add('bollRev',vote(last<bbLower,last>bbUpper),`Alt ${fixed(bbLower)} · Üst ${fixed(bbUpper)}`),
  add('bollBreak',vote(last>bbUpper&&current.v>volAvg*1.2,last<bbLower&&current.v>volAvg*1.2),'Bant dışı kapanış + hacim'),
  add('vwap',vote(last>vwap*1.0008,last<vwap*.9992),`50 mum VWAP ${fixed(vwap)}`),
  add('donchian',vote(last>top,last<bottom),`Önceki 20 mum: ${fixed(bottom)} – ${fixed(top)}`),
  add('roc',vote(roc>.25,roc<-.25),`10 mum fiyat değişimi ${fixed(roc,2)}%`),
  add('atr',vote(current.c>current.o&&current.c-current.o>atrNow*.75&&current.v>volAvg,
     current.c<current.o&&current.o-current.c>atrNow*.75&&current.v>volAvg),`ATR14 ${fixed(atrNow)} · hacim kontrolü`)
 ];
 return {ready:true,rows,price:last,atr:atrNow,closeAt:current.t,reason:'12 teknik strateji ayrı önkoşullarla incelendi'};
}
function analyzeStructure(bars,tf,fn){
 if(typeof fn!=='function'||bars.length<30)return {rows:[{id:'structure',name:names.structure,family:'smc',value:'WAIT',detail:'SMC hazır değil'},
  {id:'fvg',name:names.fvg,family:'smc',value:'WAIT',detail:'FVG hazır değil'}]};
 const r=fn(bars,tf),last=bars.at(-1),lastBreak=r?.breaks?.at(-1);
 const recent=lastBreak && last.t-lastBreak.time<=TF[tf]*30;
 const struct=recent?lastBreak.direction:'WAIT';
 const a=atr(bars);const active=(r?.fvgs||[]).filter(z=>['AKTİF','KISMİ'].includes(z.status) &&
  last.t-z.createdAt<=TF[tf]*70 && (last.c>=z.lower-2*a&&last.c<=z.upper+2*a));
 let fvg='WAIT';if(active.length){const latest=active.at(-1);fvg=latest.side==='BULL'?'LONG':'SHORT';}
 return {rows:[{id:'structure',name:names.structure,family:'smc',value:struct,
  detail:recent?`${lastBreak.type} ${lastBreak.direction==='LONG'?'↑':'↓'} · kapanmış mum`:'Güncel yapı kırılımı bulunamadı'},
  {id:'fvg',name:names.fvg,family:'smc',value:fvg,detail:active.length?'Yakın aktif FVG bölgesi':'Fiyat yakınında aktif FVG yok'}],
  recentBreak:recent?lastBreak:null,nearFvgs:active.length};
}
function analyzeFlow(data, now=Date.now()){
 const flow=data?.flow,m=data?.market;
 const fresh=!!(data?.wsConnected && flow?.fresh===true && m?.book_valid===true &&
  ok(data?.lastTrade)&&now-data.lastTrade>=0&&now-data.lastTrade<5000 &&
  ok(data?.lastBook)&&now-data.lastBook>=0&&now-data.lastBook<5000 &&
  ok(data?.analysisAt)&&now-data.analysisAt>=0&&now-data.analysisAt<12000);
 if(!fresh)return {fresh:false,rows:[{id:'tape',name:names.tape,family:'flow',value:'WAIT',detail:'Canlı işlem akışı güncel değil'},
  {id:'book',name:names.book,family:'flow',value:'WAIT',detail:'Emir defteri güncel değil'}]};
 const buy=n(flow.buy_notional_60s),sell=n(flow.sell_notional_60s),imb=n(flow.imbalance);
 const trades=n(flow.trade_count_60s),total=buy+sell;
 if(![buy,sell,imb,trades].every(ok)||buy<0||sell<0||trades<10||total<=0)return {fresh:false,rows:[
 {id:'tape',name:names.tape,family:'flow',value:'WAIT',detail:'Yetersiz işlem örneği'},
 {id:'book',name:names.book,family:'flow',value:'WAIT',detail:'Yetersiz emir defteri örneği'}]};
 const pressure=(buy-sell)/total;
 return {fresh:true,pressure,imbalance:imb,rows:[
  {id:'tape',name:names.tape,family:'flow',value:vote(pressure>.12,pressure<-.12),detail:`Delta / hacim ${fixed(pressure*100,1)}%`},
  {id:'book',name:names.book,family:'flow',value:vote(imb>.15,imb<-.15),detail:`İlk 20 seviye ${fixed(imb*100,1)}% · anlık ve değişebilir`}]};
}
function consensus(tech,smc,flow,mtf){
 const rows=[...(tech?.rows||[]),...(smc?.rows||[]),...(flow?.rows||[])];
 const totals={LONG:0,SHORT:0,WAIT:0};for(const r of rows){totals[r.value]++;}
 const groupVotes={};for(const group of Object.keys(families)){
  const groupRows=rows.filter(x=>x.family===group),a=groupRows.filter(x=>x.value==='LONG').length,b=groupRows.filter(x=>x.value==='SHORT').length;
  groupVotes[group]=a>b?'LONG':b>a?'SHORT':'WAIT';
 }
 const l=Object.values(groupVotes).filter(x=>x==='LONG').length,s=Object.values(groupVotes).filter(x=>x==='SHORT').length;
 const need=['1m','5m','15m'];const frames=need.map(x=>mtf?.[x]);
 const mtfValid=frames.every(x=>x?.ready===true&&x.fresh===true);
 const allUp=mtfValid&&frames.every(x=>x.direction==='LONG');const allDn=mtfValid&&frames.every(x=>x.direction==='SHORT');
 const aligned=allUp?'LONG':allDn?'SHORT':'WAIT';
 let signal='WAIT',reasons=[];
 if(!tech?.ready)reasons.push('Yeterli kapanmış mum yok');
 if(!flow?.fresh)reasons.push('Order Flow güncel değil: araştırma uyumu beklemede');
 if(!mtfValid)reasons.push('MTF kapanmış mumlar güncel veya yeterli değil');
 if(mtfValid&&aligned==='WAIT')reasons.push('1m / 5m / 15m trend yönü tam uyuşmuyor');
 if(tech?.ready&&flow?.fresh&&aligned!=='WAIT'){
  const agree=aligned==='LONG'?l:s,oppose=aligned==='LONG'?s:l;
  if(agree>=3&&oppose<=1 && groupVotes.flow!== (aligned==='LONG'?'SHORT':'LONG'))signal=aligned;
  else reasons.push('Strateji aileleri arasında yön uyuşmazlığı');
 }
 if(!reasons.length)reasons.push('MTF, teknik ve akış koşulları aynı yönde');
 return {rows,totals,groupVotes,mtfDirection:aligned,direction:signal,reasons,
  note:'Yalnız araştırma yönüdür; başarı olasılığı veya TiMi AL/SAT kararı değildir.'};
}
const exported={validateCandles,ema,sma,rsi,atr,analyzeTechnical,analyzeStructure,analyzeFlow,consensus};
if(typeof module==='object'&&module.exports)module.exports=exported;
if(!d)return;
if(root.__timiProV15Installed)return;root.__timiProV15Installed=true;
let cache=new Map(),loading=false,serial=0,lastKey='',lastRendered='',lastRefresh=0;
function currentSymbol(){try{if(typeof symbol==='string'&&SYMS.includes(symbol))return symbol;}catch(_){}return SYMS.includes($('symbol')?.value)?$('symbol').value:'BTCUSDT';}
function currentGateway(){try{const raw=typeof gateway==='string'?gateway:($('gateway')?.value||root.location.origin);const p=new URL(raw,root.location.href);if(p.protocol==='https:'||(p.protocol==='http:'&&p.hostname==='localhost'))return p.origin;}catch(_){}return root.location.origin;}
function hasSection(){return $('proLab')?.classList.contains('active')===true;}
function tab(){return d.querySelector('[data-pro-tab].selected')?.dataset?.proTab||'flow';}
function health(){try{return {
  wsConnected:typeof ws!=='undefined'&&ws?.readyState===1,
  market:typeof market!=='undefined'?market:null,
  flow:typeof analysis!=='undefined'?analysis?.flow:null,
  analysisAt:typeof analysisAt==='number'?analysisAt:NaN,
  lastTrade:typeof lastTrade==='number'?lastTrade:NaN,
  lastBook:typeof lastBook==='number'?lastBook:NaN,
  decision:typeof analysis!=='undefined'?analysis?.signal?.decision||'BEKLE':'BEKLE'
 };}catch(_){return {};}}
function safeText(id,t){const el=$(id);if(el)el.textContent=t;}
async function fetchFrame(sym,tf,source,now){
 const key=sym+'|'+tf+'|'+source,prev=cache.get(key),ttl=TF[tf]==60000?32000:65000;
 if(prev&&now-prev.fetched<ttl){const last=prev.bars.at(-1);return {...prev,fresh:Date.now()-(last.t+TF[tf])<=TF[tf]*2+15000};}
 const controller=new AbortController(),timer=setTimeout(()=>controller.abort(),11000);
 try{
  const res=await fetch(source+'/api/klines/'+sym+'?interval='+tf+'&limit=350',{cache:'no-store',signal:controller.signal});
  if(!res.ok)throw Error('HTTP '+res.status);
  const data=await res.json();if(data.symbol!==sym||data.interval!==tf||!Array.isArray(data.candles))throw Error('Sembol/periyot yanıtı uyuşmuyor');
  const bars=validateCandles(data.candles,tf,Date.now());if(bars.length<220)throw Error('220 kapanmış mum yok');
  const last=bars.at(-1),fresh=Date.now()-(last.t+TF[tf])<=TF[tf]*2+15000;
  const frame={symbol:sym,tf,bars,ready:true,fresh,fetched:Date.now()};
  cache.set(key,frame);return frame;
 }finally{clearTimeout(timer);}
}
function frameDirection(f){if(!f?.ready||!f.fresh)return 'WAIT';const c=f.bars.map(x=>x.c),e20=ema(c,20).at(-1),e50=ema(c,50).at(-1),p=c.at(-1);return vote(p>e20&&e20>e50,p<e20&&e20<e50);}
function switchContext(){const k=currentSymbol()+'|'+currentGateway();if(k===lastKey)return;lastKey=k;serial++;cache.clear();lastRendered='';}
function createElement(tag,id,parent,text){const a=d.createElement(tag);if(id)a.id=id;if(text!==undefined)a.textContent=text;parent.append(a);return a;}
function makePanel(target,id,headline,tag){const c=createElement('div',id,target);c.className='pro-v15-card';const head=createElement('div',null,c);head.className='pro-v15-head';createElement('strong',null,head,headline);createElement('span',null,head,tag);return c;}
function init(){
 const main=$('proLabTest'),flow=$('proFlow'),smc=$('proSmc');if(!main||!flow||!smc)return false;
 if(!$('proV15Live')){
  const c=makePanel(main,'proV15Live','16 analiz kuralı · Canlı Strateji Uyum Kontrolü','ANLIK ARAŞTIRMA');
  main.prepend(c);const q=createElement('div','proV15Status',c,'Kapanmış mumlar ve piyasa verisi bekleniyor');q.className='pro-v15-status';
  const summary=createElement('div','proV15Summary',c,'');summary.className='pro-v15-summary';
  const stats=createElement('div','proV15Stats',c,'');stats.className='pro-v15-stats';
  const details=createElement('details',null,c);details.className='pro-v15-detail';createElement('summary',null,details,'16 kuralın gerekçelerini göster');
  createElement('div','proV15Rows',details);
  const cost=createElement('div','proV15Cost',c,'Komisyon / kayma hesaplanıyor');cost.className='pro-v15-cost';
  createElement('p',null,c,'Bu bölüm TiMi’nin kural tabanlı AL/SAT kararını değiştirmez. Stratejiler birbirinden bağımsız istatistiksel kanıt sayılmaz. Geçmiş sinyal kaydı tutulmaz.').className='pro-v15-help';
 }
 if(!$('proV15Flow')){
  const c=makePanel(flow,'proV15Flow','Akış · Strateji Çelişki Kontrolü','CANLI');
  createElement('div','proV15FlowStatus',c,'Akış karşılaştırması hazırlanıyor');
 }
 if(!$('proV15Smc')){
  const c=makePanel(smc,'proV15Smc','SMC · Strateji Uyum Kontrolü','ÇOKLU TF');
  createElement('div','proV15SmcStatus',c,'SMC ve teknik sinyaller karşılaştırılıyor');
 }
 // User opted out of signal saving and after-the-fact tracking. Hide the
 // pre-existing audit presentation; underlying server is intentionally untouched.
 const audit=$('proAudit');const auditButton=$('proAuditReload');
 if(audit){const heading=audit.previousElementSibling?.previousElementSibling;
  if(heading?.tagName==='H4'&&heading.textContent.includes('sinyal gözlemleri'))heading.remove();audit.remove();}
 auditButton?.remove();
 const s=d.createElement('style');s.id='proV15Styles';s.textContent=`
 #proLab .pro-v15-card{border:1px solid #3b5872;border-radius:12px;background:#12283e;padding:12px;margin:0 0 15px;display:grid;gap:10px;min-width:0}
 #proLab .pro-v15-head{display:flex;justify-content:space-between;flex-wrap:wrap;align-items:center;gap:8px}
 #proLab .pro-v15-head strong{font-size:13px;color:#e8f3fe}
 #proLab .pro-v15-head span{font-size:10px;color:#75d8bb;border:1px solid #387869;border-radius:16px;padding:4px 6px}
 #proLab .pro-v15-status,#proLab .pro-v15-cost,#proLab .pro-v15-help{font-size:11px;line-height:1.55;color:#a4c0d6}
 #proLab .pro-v15-summary{font-size:18px;font-weight:850;line-height:1.4;color:#ffc978}
 #proLab .pro-v15-summary.up{color:#60dcb0}#proLab .pro-v15-summary.down{color:#ff809c}
 #proLab .pro-v15-stats{display:grid;grid-template-columns:repeat(3,minmax(0,1fr));gap:7px}
 #proLab .pro-v15-stats>div{padding:9px 5px;background:#1c354e;border-radius:9px;text-align:center;min-width:0}
 #proLab .pro-v15-stats b{display:block;font-size:16px}#proLab .pro-v15-stats small{font-size:10px;color:#c2d2e3}
 #proLab .pro-v15-detail{border-top:1px solid #35506a;padding-top:10px}
 #proLab .pro-v15-detail>summary{font-size:12px;color:#afdeec;cursor:pointer}
 #proLab .pro-v15-rule{padding:8px 0;border-bottom:1px solid #294159;display:grid;gap:3px;font-size:11px}
 #proLab .pro-v15-rule>div{display:flex;justify-content:space-between;gap:9px}
 #proLab .pro-v15-rule small{font-size:10px;color:#a1b9d0}
 #proLab .pro-v15-rule b.up{color:#6fe3b7}#proLab .pro-v15-rule b.down{color:#ff819e}
 #proLab #proV15FlowStatus,#proLab #proV15SmcStatus{color:#c9dcec;font-size:12px;line-height:1.6}
 `;d.head.append(s);
 // Completely remove the duplicate bottom navigation entry. Keep the hidden
 // legacy DOM because TiMi v1.8.4 still writes #asks/#bids/#trades/#mid.
 d.querySelector('nav.foot [data-page="flow"]')?.remove();
 $('flow')?.classList.remove('active');
 return true;
}
function render(result,frames){
 if(!hasSection())return;
 const c=result?.alignment;if(!c)return;
 const stats=$('proV15Stats');const nums=c.totals;const sign=c.direction;
 safeText('proV15Status',`${currentSymbol()} · ${new Date().toLocaleTimeString('tr-TR')} · ${frames['1m']?.bars?.length||0} kapalı 1m mum · 1m / 5m / 15m doğrulaması`);
 const summary=$('proV15Summary');summary.className='pro-v15-summary '+(sign==='LONG'?'up':sign==='SHORT'?'down':'');
 summary.textContent=sign==='WAIT'?'BEKLE · Araştırma şartları uyumsuz':sign==='LONG'?'YUKARI YÖNLÜ ARAŞTIRMA UYUMU':'AŞAĞI YÖNLÜ ARAŞTIRMA UYUMU';
 stats.replaceChildren();for(const [what,key] of [['Yukarı','LONG'],['Aşağı','SHORT'],['Nötr','WAIT']]){
  const box=d.createElement('div'),numEl=d.createElement('b');numEl.textContent=nums[key];box.append(numEl);const e=d.createElement('small');e.textContent=what;box.append(e);stats.append(box);
 }
 const rows=$('proV15Rows');rows.replaceChildren();for(const item of c.rows){
  const box=d.createElement('div');box.className='pro-v15-rule';const line=d.createElement('div');const title=d.createElement('span');title.textContent=item.name;const val=d.createElement('b');val.className=item.value==='LONG'?'up':item.value==='SHORT'?'down':'';
  val.textContent=item.value==='WAIT'?'BEKLE':label[item.value];line.append(title,val);box.append(line);
  const small=d.createElement('small');small.textContent=item.detail;box.append(small);rows.append(box);
 }
 const costFee=n($('proBtFee')?.value),costSlip=n($('proBtSlip')?.value);
 const cost=ok(costFee)&&ok(costSlip)&&costFee>=0&&costSlip>=0&&costFee<=.5&&costSlip<=.5?2*(costFee+costSlip):NaN;
 safeText('proV15Cost',`TiMi asıl karar: ${health().decision||'BEKLE'} · Araştırma: ${label[sign]||'BEKLE'} · Yaklaşık gidiş-dönüş maliyeti: ${ok(cost)?fixed(cost,2)+'%':'bilinmiyor'} · ${c.reasons.join('; ')}.`);
 const f=result.flow;
 safeText('proV15FlowStatus',f.fresh?`Agresif delta: ${fixed(f.pressure*100,1)}% · İlk 20 seviye baskısı: ${fixed(f.imbalance*100,1)}% · Akış ailesi ${label[c.groupVotes.flow]} · MTF ${label[c.mtfDirection]}. ${c.groupVotes.flow!=='WAIT'&&c.mtfDirection!=='WAIT'&&c.groupVotes.flow!==c.mtfDirection?'AKIŞ / MTF ÇELİŞKİSİ':'Akış kesin fiyat yönü değildir.'}`:'Akış gecikmiş veya yetersiz. Canlı uyum onayı verilmez.');
 safeText('proV15SmcStatus',`1m: ${label[frames['1m']?.direction||'WAIT']} · 5m: ${label[frames['5m']?.direction||'WAIT']} · 15m: ${label[frames['15m']?.direction||'WAIT']} · SMC ailesi ${label[c.groupVotes.smc]} · Teknik trend ${label[c.groupVotes.trend]} · ${c.groupVotes.smc!=='WAIT'&&c.groupVotes.trend!=='WAIT'&&c.groupVotes.smc!==c.groupVotes.trend?'SMC / TREND ÇELİŞKİSİ':'Doğrulama amaçlıdır.'}`);
}
async function refresh(){
 if(!hasSection()||loading)return;
 switchContext();const run=++serial,sym=currentSymbol(),source=currentGateway();loading=true;
 safeText('proV15Status',sym+' · kapanmış mumlar kontrol ediliyor');
 try{
  const [a,b,c]=await Promise.allSettled(['1m','5m','15m'].map(tf=>fetchFrame(sym,tf,source,Date.now())));
  if(run!==serial||sym!==currentSymbol()||source!==currentGateway())return;
  const recs=[a,b,c],frames={},details=[];
  for(const [i,tf] of ['1m','5m','15m'].entries()){
   const r=recs[i];if(r.status==='fulfilled'){const f=r.value;frames[tf]={...f,direction:frameDirection(f)};if(!f.fresh)details.push(tf+' mum gecikmiş');}
   else{frames[tf]={ready:false,fresh:false,direction:'WAIT'};details.push(tf+': '+String(r.reason?.message||'erişim yok').slice(0,55));}
  }
  const tech=frames['1m']?.ready&&frames['1m'].fresh?analyzeTechnical(frames['1m'].bars):{ready:false,rows:[]};
  const smc=frames['1m']?.ready&&frames['1m'].fresh?analyzeStructure(frames['1m'].bars,'1m',root.timiSmcReadOnly?.analyzeClosedBars):{rows:[]};
  const flow=analyzeFlow(health());const mtf={};for(const tf of ['1m','5m','15m'])mtf[tf]={ready:frames[tf].ready,fresh:frames[tf].fresh,direction:frames[tf].direction};
  const alignment=consensus(tech,smc,flow,mtf);const result={tech,smc,flow,alignment,details};render(result,frames);
  if(details.length)safeText('proV15Status',sym+' · Kısıt: '+details.join(' | '));
  lastRendered=JSON.stringify({symbol:sym,at:Date.now(),direction:alignment.direction});lastRefresh=Date.now();
 }catch(e){safeText('proV15Status','Veri yükleme hatası: '+String(e.message||e).slice(0,90));}finally{loading=false;}
}
function liveFlowUpdate(){
 if(!hasSection())return;
 const h=health(),f=analyzeFlow(h),a=f.rows||[];
 if(!f.fresh){safeText('proV15FlowStatus','Emir defteri veya gerçekleşmiş işlemler güncel değil.');return;}
 safeText('proV15FlowStatus',`Agresif alım/satım: ${label[a[0]?.value]} · Defter: ${label[a[1]?.value]} · Delta ${fixed(f.pressure*100,1)}% · Baskı ${fixed(f.imbalance*100,1)}%. ${a[0]?.value!==a[1]?.value&&a[0]?.value!=='WAIT'&&a[1]?.value!=='WAIT'?'ÇELİŞKİ: bekleyen likidite ve gerçekleşen işlemler farklı yönde.':'Tek başına AL/SAT onayı değildir.'}`);
}
function start(){if(!init())return;
 $('symbol')?.addEventListener('change',()=>{switchContext();if(hasSection())refresh();});
 $('saveGateway')?.addEventListener('click',()=>setTimeout(()=>{switchContext();if(hasSection())refresh();},500));
 d.querySelector('nav.foot [data-page="proLab"]')?.addEventListener('click',()=>setTimeout(refresh,180));
 d.querySelectorAll('[data-pro-tab]').forEach(b=>b.addEventListener('click',()=>setTimeout(refresh,100)));
 $('proBtFee')?.addEventListener('change',()=>{lastRefresh=0;});$('proBtSlip')?.addEventListener('change',()=>{lastRefresh=0;});
 setInterval(()=>{if(!hasSection())return;liveFlowUpdate();if(Date.now()-lastRefresh>33000)refresh();},2500);
 root.timiProV15Debug=()=>({version:'1.5.0',readOnly:true,archiving:false,legacyFlowTab:!!d.querySelector('nav.foot [data-page="flow"]'),lastRendered});
}
if(d.readyState==='loading')d.addEventListener('DOMContentLoaded',start,{once:true});else start();
})(typeof window!=='undefined'?window:null);
