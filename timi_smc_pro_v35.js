/* TiMi SCALP PRO — SMC PRO 3.5 (read-only and ephemeral).
 * Suat DOGU. Original integration for existing TiMi v2.3.
 * Reuses TiMi SMC v1.2 and TiMi v2.0 confirmed-candle analytical primitives.
 * Does NOT issue orders, change signals, store signals or download private data.
 */
(function(root){
'use strict';
const STEPS={'1m':60000,'5m':300000,'15m':900000};
const TF_LIST=['1m','5m','15m'];
const MAX_BARS=300;
const num=x=>x===null||x===undefined||x===''?NaN:Number(x);
const finite=Number.isFinite;
const clip=(v,min,max)=>Math.max(min,Math.min(max,v));
const dec=(v,n=2)=>finite(v)?v.toLocaleString('tr-TR',{minimumFractionDigits:0,maximumFractionDigits:n}):'—';
const short=x=>String(x??'').slice(0,120).replace(/[&<>"']/g,c=>({'&':'&amp;','<':'&lt;','>':'&gt;','"':'&quot;'}[c]));
function normalize(raw,tf,at=Date.now()){
  const step=STEPS[tf];if(!step||!Array.isArray(raw))return {bars:[],gaps:0,invalid:0,total:0};
  const byTime=new Map();let invalid=0;
  for(const item of raw){
    const t=num(item?.t??item?.timestamp),o=num(item?.o??item?.open),h=num(item?.h??item?.high),
      l=num(item?.l??item?.low),c=num(item?.c??item?.close),v=num(item?.v??item?.volume??0);
    // Only explicit verified closed bars; do not infer future candle closure.
    if(item?.closed!==true||![t,o,h,l,c,v].every(finite)||t<1e11||t+step>at-2500||
       o<=0||l<=0||h<Math.max(o,c,l)||l>Math.min(o,c,h)||v<0){invalid++;continue;}
    byTime.set(t,{t,o,h,l,c,v});
  }
  const ordered=[...byTime.values()].sort((a,b)=>a.t-b.t).slice(-MAX_BARS);
  let segmentStart=0,gaps=0;
  for(let i=1;i<ordered.length;i++)if(ordered[i].t-ordered[i-1].t!==step){segmentStart=i;gaps++;}
  return {bars:ordered.slice(segmentStart),gaps,invalid,total:ordered.length};
}
function atr(bars,n=14){
  if(!Array.isArray(bars)||bars.length<n+1)return NaN;
  let sum=0;
  for(let i=bars.length-n;i<bars.length;i++){
    const b=bars[i],prev=bars[i-1];
    sum+=Math.max(b.h-b.l,Math.abs(b.h-prev.c),Math.abs(b.l-prev.c));
  }
  return sum/n;
}
function avgVol(bars,n=20){if(bars.length<n)return NaN;return bars.slice(-n).reduce((a,b)=>a+b.v,0)/n;}
function qualityLabel(score){return score>=70?'YÜKSEK':score>=45?'ORTA':'DÜŞÜK';}
function signalSide(side){return side==='LONG'||side==='BULL'?'LONG':side==='SHORT'||side==='BEAR'?'SHORT':'WAIT';}
function assessOB(block,bars,a,tf){
  const step=STEPS[tf],lo=num(block.lower),hi=num(block.upper);
  if(!finite(lo)||!finite(hi)||hi<=lo)return null;
  const create=bars.findIndex(b=>b.t===block.createdAt),source=bars.find(x=>x.t===block.sourceAt);
  const time=bars.at(-1).t,ago=Math.max(0,Math.round((time-block.createdAt)/step));
  const widthAtr=finite(a)&&a>0?(hi-lo)/a:NaN;
  const candle=create>=0?bars[create]:null;
  const displacement=candle&&finite(a)&&a>0?Math.abs(candle.c-candle.o)/a:NaN;
  const volumeBase=avgVol(bars.slice(0,Math.max(20,create)),20);
  const volRatio=candle&&finite(volumeBase)&&volumeBase>0?candle.v/volumeBase:NaN;
  const status=block.status==='GECERSIZ'?'GEÇERSİZ':block.status==='TEMAS_EDILDI'?'TEST EDİLDİ':'ADAY';
  let score=20;
  if(status!=='GEÇERSİZ')score+=18;
  if(finite(widthAtr)&&widthAtr>=.15&&widthAtr<=1.5)score+=18;
  if(finite(displacement)&&displacement>=.55)score+=18;
  if(finite(volRatio)&&volRatio>=1.1)score+=10;
  if(ago<=30)score+=10;
  if(status==='ADAY')score+=6;
  score=clip(Math.round(score),0,status==='GEÇERSİZ'?20:100);
  return {type:'OB',id:`${tf}:${block.createdAt}:${block.sourceAt}:${block.side}`,
    side:signalSide(block.side),lower:lo,upper:hi,at:block.createdAt,sourceAt:source?.t??block.sourceAt,
    ago,status,score,quality:qualityLabel(score),widthAtr,displacement,volRatio};
}
function assessFVG(z,bars,a,tf,breaks){
  const lo=num(z.lower),hi=num(z.upper);if(!finite(lo)||!finite(hi)||hi<=lo)return null;
  const step=STEPS[tf],last=bars.at(-1).t;
  const ago=Math.max(0,Math.round((last-z.createdAt)/step));
  const widthAtr=finite(a)&&a>0?(hi-lo)/a:NaN;
  const dir=signalSide(z.side);
  const nearbyBreak=breaks.some(b=>signalSide(b.direction)===dir&&Math.abs(b.time-z.createdAt)<=10*step);
  const pct=clip(num(z.filledPct),0,100);
  const status=['DOLDU','GEÇERSİZ'].includes(z.status)?z.status:z.status==='KISMİ'?'KISMİ':'AKTİF';
  let score=20;
  if(status!=='GEÇERSİZ'&&status!=='DOLDU')score+=20;
  if(finite(widthAtr)&&widthAtr>=.12)score+=20;
  if(nearbyBreak)score+=18;
  if(ago<=30)score+=12;
  if(finite(pct)&&pct<50)score+=10;
  score=clip(Math.round(score),0,['GEÇERSİZ','DOLDU'].includes(status)?20:100);
  return {type:'FVG',id:z.id,side:dir,lower:lo,upper:hi,at:z.createdAt,
    ago,status,filledPct:finite(pct)?pct:null,score,quality:qualityLabel(score),widthAtr,nearbyBreak};
}
function assessSweep(sw,bars,a,tf){
  const step=STEPS[tf],b=bars.find(x=>x.t===sw.time);
  if(!b)return null;
  const ago=Math.max(0,Math.round((bars.at(-1).t-b.t)/step));
  const upper=sw.side==='UPPER', level=num(sw.price);
  if(!finite(level))return null;
  const wick=upper?b.h-Math.max(b.o,b.c):Math.min(b.o,b.c)-b.l;
  const wickAtr=finite(a)&&a>0?wick/a:NaN;
  const base=avgVol(bars.filter(x=>x.t<b.t),20);
  const volumeRatio=finite(base)&&base>0?b.v/base:NaN;
  const reclaim=upper?b.c<level:b.c>level;
  let score=20;
  if(reclaim)score+=24;
  if(finite(wickAtr)&&wickAtr>=.15)score+=20;
  if(finite(volumeRatio)&&volumeRatio>=1.2)score+=18;
  if(ago<=15)score+=18;
  score=clip(Math.round(score),0,100);
  return {type:'SWEEP',id:`${tf}:${sw.side}:${sw.time}:${sw.price}`,at:sw.time,
    side:upper?'SHORT':'LONG',price:level,ago,score,quality:qualityLabel(score),
    wickAtr,volumeRatio,reclaim,description:upper?'Üst likidite süpürme adayı':'Alt likidite süpürme adayı'};
}
function analyze(raw,tf,engines,at=Date.now()){
  const processed=normalize(raw,tf,at),bars=processed.bars,step=STEPS[tf];
  if(!step||bars.length<70)return {ready:false,tf,reason:`Yetersiz ardışık kapanmış mum: ${bars.length}/70`,count:bars.length,gaps:processed.gaps};
  const last=bars.at(-1),age=at-(last.t+step);
  if(age<0||age>2*step+15000)return {ready:false,tf,reason:`Kapanmış mum gecikmiş (${Math.round(age/1000)} sn)`,count:bars.length,gaps:processed.gaps};
  if(typeof engines?.smc?.analyzeClosedBars!=='function'||typeof engines?.core?.smcExtended!=='function')
    return {ready:false,tf,reason:'TiMi SMC v1.2 / v2.0 hesaplama motoru eksik',count:bars.length,gaps:processed.gaps};
  // Both engines use confirmed pivots/candles only; never the open/current candle.
  const structure=engines.smc.analyzeClosedBars(bars,tf);
  const extended=engines.core.smcExtended(bars,tf);
  if(!extended?.ready)return {ready:false,tf,reason:'SMC genişletilmiş hesaplama hazır değil',count:bars.length,gaps:processed.gaps};
  const a=atr(bars),breaks=structure.breaks||[];
  const lastBreak=breaks.at(-1)||null;
  const recentBreak=lastBreak&&(last.t-lastBreak.time)<=30*step?lastBreak:null;
  const direction=recentBreak?signalSide(recentBreak.direction):'WAIT';
  const blocks=(extended.blocks||[]).map(b=>assessOB(b,bars,a,tf)).filter(Boolean).sort((a,b)=>b.at-a.at);
  const fvgs=(structure.fvgs||[]).map(z=>assessFVG(z,bars,a,tf,breaks)).filter(Boolean).sort((a,b)=>b.at-a.at);
  const sw=(extended.sweeps||[]).filter(z=>last.t-z.time<=30*step).map(z=>assessSweep(z,bars,a,tf)).filter(Boolean);
  const uniqueSweeps=[...new Map(sw.map(z=>[z.id,z])).values()].sort((a,b)=>b.at-a.at);
  const activeOB=blocks.filter(z=>z.status!=='GEÇERSİZ').slice(0,4);
  const activeFVG=fvgs.filter(z=>!['GEÇERSİZ','DOLDU'].includes(z.status)).slice(0,5);
  return {ready:true,tf,count:bars.length,gaps:processed.gaps,age_ms:age,at:last.t+step,
    lastClosed:last.t,atr:a,price:last.c,direction,lastBreak,activeOB,activeFVG,
    sweeps:uniqueSweeps.slice(0,5),invalidOB:blocks.filter(z=>z.status==='GEÇERSİZ').length,
    invalidFVG:fvgs.filter(z=>['GEÇERSİZ','DOLDU'].includes(z.status)).length,
    equalHighs:(extended.equalHighs||[]).slice(-3),equalLows:(extended.equalLows||[]).slice(-3),
    premiumDiscount:extended.range?.zone??null,read_only:true,
    limitation:'Kural tabanlı yapı ve bölge kalitesi. Skor kazanma olasılığı değildir.'};
}
function mtf(rows){
 const ready=TF_LIST.every(tf=>rows?.[tf]?.ready);
 const directions=TF_LIST.map(tf=>rows?.[tf]?.direction||'WAIT');
 const aligned=ready&&directions.every(x=>x===directions[0])&&directions[0]!=='WAIT';
 return {ready,aligned,direction:aligned?directions[0]:'WAIT',valid:TF_LIST.filter(tf=>rows?.[tf]?.ready).length,
   directions,description:aligned?`3/3 ${directions[0]==='LONG'?'YUKARI':'AŞAĞI'} · teyitli son kırılımlar`:
   'Tam yön uyumu doğrulanamadı · yön veya veri eksik'};
}
const exported={normalize,atr,assessOB,assessFVG,assessSweep,analyze,mtf,qualityLabel,STEPS};
if(typeof module==='object'&&module.exports)module.exports=exported;
if(!root?.document)return;
if(root.__timiSmc35Loaded)return;
root.__timiSmc35Loaded=true;
const d=root.document,el=id=>d.getElementById(id);
const state={tf:'1m',showOB:true,showFVG:true,showSweep:true,showChart:false,showLabels:false,
  rows:{},symbol:'',gateway:'',busy:false,requested:0,lastFetch:0,lastDrawKey:'',chart:null,invalidated:0};
const groups=['timi-smc35-ob','timi-smc35-fvg','timi-smc35-sweep'];
function currentSymbol(){return String(el('symbol')?.value||'BTCUSDT').toUpperCase().trim();}
function gateway(){let value=String(el('gateway')?.value||root.location.origin).trim().replace(/\/$/,'');
  try{let url=/^https?:\/\//i.test(value)?new URL(value):new URL(value,root.location.origin);return url.protocol==='https:'||url.hostname==='localhost'?url.origin:'';}catch(_){return '';}}
function active(){return (el('proLab')?.classList.contains('active')&&el('proSmc')?.hidden===false)||(state.showChart&&el('chart')?.classList.contains('active'));}
function configKey(){return gateway()+'|'+currentSymbol();}
function emptyChart(chart){if(!chart)return;for(const g of groups)try{chart.removeOverlay({groupId:g});}catch(_){}}
function resetKey(){const key=configKey();if(key===state.gateway+'|'+state.symbol)return;
  state.invalidated++;state.rows={};state.lastFetch=0;state.requested=0;state.gateway=gateway();state.symbol=currentSymbol();
  if(state.chart)emptyChart(state.chart);state.lastDrawKey='';
  const badge=el('smc35Status');if(badge)badge.textContent='Yeni parite · veri bekleniyor';
}
function css(){const style=d.createElement('style');style.id='timiSmc35Style';style.textContent=`
#timiSmc35{border:1px solid #335a67;border-radius:12px;background:#132739;margin:9px 0 16px;color:#e9f2fc;overflow:hidden}
#timiSmc35>summary{padding:13px 12px;display:flex;align-items:center;justify-content:space-between;gap:8px;cursor:pointer;min-height:45px;list-style:none}
#timiSmc35>summary::-webkit-details-marker{display:none}
#timiSmc35>summary b{font-size:13px}#timiSmc35>summary small{display:block;color:#9bb4cc;font-weight:400;font-size:10px;padding-top:3px}
#smc35Status{font-size:10px;color:#80dcbf;max-width:43%;text-align:right;line-height:1.3}
#timiSmc35 .sm35body{padding:0 11px 12px}#timiSmc35 .sm35callout{padding:9px;border:1px solid #2b5164;border-radius:8px;color:#c2d9eb;font-size:11px;line-height:1.5;margin:10px 0}
#timiSmc35 .sm35toolbar{display:flex;gap:6px;flex-wrap:wrap;margin:8px 0}#timiSmc35 button{padding:8px 9px;border:1px solid #3b6076;border-radius:8px;background:#17334b;color:#dceefa;min-height:36px;font-size:11px;font-weight:650;cursor:pointer}
#proLab #timiSmc35 button.active{border-color:#2dbc91;background:#155444;color:#d1ffe9}#timiSmc35 button:focus-visible,#timiSmc35 summary:focus-visible{outline:2px solid #62e8c6;outline-offset:2px}
#timiSmc35 .sm35tf{display:grid;grid-template-columns:repeat(3,minmax(0,1fr));gap:6px;margin:8px 0}
#timiSmc35 .sm35tf button{display:grid;gap:3px;text-align:center;min-width:0;overflow-wrap:anywhere}
#timiSmc35 .sm35tf button strong{font-size:12px}#timiSmc35 .sm35tf button span{font-size:10px}#timiSmc35 .sm35tf button small{font-size:9px;color:#91a9c1}
#timiSmc35 .sm35good{color:#5ddcb2}#timiSmc35 .sm35bad{color:#ff8d9d}#timiSmc35 .sm35muted{color:#a0b6cc}
#timiSmc35 .sm35subtitle{font-size:11px;color:#b6d0e4;margin:10px 0 6px;font-weight:750}
#timiSmc35 .sm35row{display:grid;grid-template-columns:minmax(0,1fr) auto;gap:5px 8px;padding:8px 9px;border:1px solid #304e64;border-radius:9px;background:#193148;margin:6px 0;font-size:11px}
#timiSmc35 .sm35row span{font-weight:750}#timiSmc35 .sm35row strong{font-size:11px;text-align:right}#timiSmc35 .sm35row small{grid-column:1/-1;color:#a2bcd1;line-height:1.5;font-size:10px;overflow-wrap:anywhere}
#timiSmc35 .sm35help{font-size:10px;color:#98b5ce;line-height:1.65;margin-top:11px}#timiSmc35 .sm35help summary{color:#c4dced;cursor:pointer}
@media(max-width:390px){#timiSmc35 .sm35body{padding:0 9px 10px}#timiSmc35 .sm35tf{gap:4px}#timiSmc35 .sm35tf button{padding:7px 4px}#smc35Status{max-width:47%}}
`;d.head.append(style);}
function install(){if(el('timiSmc35'))return true;
 const target=el('proSmc');if(!target)return false;
 const box=d.createElement('details');box.id='timiSmc35';box.open=true;
 box.innerHTML=`<summary><div><b>SMC PRO 3.5 · Bölge Kalitesi</b><small>Order Block · FVG · Süpürme · 1m/5m/15m</small></div><strong id="smc35Status">Veri bekleniyor</strong></summary>
 <div class="sm35body">
  <div class="sm35callout" id="smc35Mtf">Üç zaman diliminin kapanmış mumları doğrulanıyor.</div>
  <div class="sm35tf" id="smc35TfCards" role="group" aria-label="Zaman dilimi seçimi"></div>
  <div class="sm35toolbar" role="group" aria-label="Analiz görünümleri">
   <button type="button" data-sm35="ob">Order Block</button><button type="button" data-sm35="fvg">FVG</button><button type="button" data-sm35="sweep">Süpürme</button>
   <button type="button" data-sm35="chart">Grafikte göster</button><button type="button" data-sm35="labels">Grafik etiketleri</button>
   <button type="button" data-sm35="refresh">Yenile</button>
  </div>
  <div id="smc35DrawInfo" style="font-size:10px;color:#9fbdd2;padding:4px 1px" aria-live="polite">Grafik: KAPALI · Etiketler: KAPALI</div>
  <div class="sm35callout" id="smc35Detail">Hesaplama bekleniyor.</div>
  <div id="smc35Rows"></div>
  <details class="sm35help"><summary>Kalite puanı ve göstergeler nasıl okunur?</summary><p>Kalite 0–100 arası kural tabanlı sınıflamadır, başarı olasılığı değildir. OB kalite: bölge genişliği/ATR, kırılım mumunun gövdesi, hacim, bölge yaşı ve test/iptal durumu. FVG kalite: boşluk/ATR, yakın yapı kırılımı, yaş ve doluluk. Sweep: fitil/ATR, hacim ve seviyenin yeniden içeride kapanması. Yalnızca kapanmış mumlar kullanılır. Kayıt, emir veya sinyal yoktur.</p><p>Grafikte göster varsayılan olarak kapalıdır ve sadece 1m/5m/15m grafiklerde, en fazla ikişer aday için işaret koyar. Bu analiz doğrulanmış kurumsal emir, likidite havuzu veya kâr olasılığı anlamına gelmez.</p></details>
 </div>`;
 const head=target.querySelector('.pro-mini-head');if(head)head.after(box);else target.prepend(box);
 css();
 box.addEventListener('toggle',()=>{if(box.open)void fetchAll(false)});
 box.addEventListener('click',event=>{const b=event.target.closest('button[data-sm35]');if(!b)return;
  const action=b.dataset.sm35;
  if(action==='refresh'){void fetchAll(true);return;}
  const map={ob:'showOB',fvg:'showFVG',sweep:'showSweep',chart:'showChart',labels:'showLabels'};
  const field=map[action];if(field)state[field]=!state[field];
  if(action==='chart'&&!state.showChart){emptyChart(state.chart);state.lastDrawKey='';}
  render();draw();
 });
 render();return true;
}
async function request(tf,context,serial){
 const ctrl=new AbortController(),timer=setTimeout(()=>ctrl.abort(),14000);
 try{
  const response=await fetch(state.gateway+'/api/klines/'+encodeURIComponent(state.symbol)+'?interval='+tf+'&limit=300',
    {cache:'no-store',signal:ctrl.signal});
  if(!response.ok)throw Error('Gateway HTTP '+response.status);
  const json=await response.json();
  if(json?.symbol!==state.symbol||json?.interval!==tf||!Array.isArray(json.candles))throw Error('Parite/zaman verisi uyuşmuyor');
  const engines={smc:root.timiSmcReadOnly,core:root.TiMiV20Core};
  return analyze(json.candles,tf,engines,Date.now());
 }finally{clearTimeout(timer);}
}
async function fetchAll(force=false){
 resetKey();if(state.busy||!state.gateway||!active())return;
 if(!force&&Date.now()-state.lastFetch<40000&&Object.keys(state.rows).length===3){render();return;}
 const context=configKey(),serial=++state.invalidated;
 state.busy=true;state.requested=Date.now();if(el('smc35Status'))el('smc35Status').textContent='Mumlar kontrol ediliyor';
 try{
  const replies=await Promise.allSettled(TF_LIST.map(tf=>request(tf,context,serial)));
  if(serial!==state.invalidated||context!==configKey())return;
  state.rows=Object.fromEntries(TF_LIST.map((tf,i)=>[tf,replies[i].status==='fulfilled'?replies[i].value:
    {ready:false,tf,reason:String(replies[i].reason?.message||replies[i].reason||'Bağlantı hatası').slice(0,90)}]));
  state.lastFetch=Date.now();render();state.lastDrawKey='';draw();
 }finally{state.busy=false;if(serial===state.invalidated)render();}
}
function rowHTML(title,body,sub,type){const cls=type==='LONG'?'sm35good':type==='SHORT'?'sm35bad':'sm35muted';
 return `<div class="sm35row"><span>${short(title)}</span><strong class="${cls}">${short(body)}</strong><small>${short(sub)}</small></div>`;}
function render(){const host=el('timiSmc35');if(!host)return;
 const summary=mtf(state.rows),value=summary.aligned?(summary.direction==='LONG'?'3/3 YUKARI':'3/3 AŞAĞI'):`${summary.valid}/3 geçerli`;
 if(el('smc35Status'))el('smc35Status').textContent=state.busy?'Hesaplanıyor':value;
 if(el('smc35Mtf'))el('smc35Mtf').textContent=summary.description+' · Bu sonuç AL/SAT olasılığı değildir.';
 if(el('smc35DrawInfo'))el('smc35DrawInfo').textContent='Grafik: '+(state.showChart?'AÇIK':'KAPALI')+' · Etiketler: '+(state.showLabels?'AÇIK':'KAPALI')+' · Zaman dilimi: '+state.tf;
 const cards=el('smc35TfCards');if(cards){cards.replaceChildren();for(const tf of TF_LIST){
  const a=state.rows[tf],b=d.createElement('button');b.type='button';b.className=state.tf===tf?'active':'';
  const name=d.createElement('strong');name.textContent=tf;
  const dir=d.createElement('span');dir.className=a?.direction==='LONG'?'sm35good':a?.direction==='SHORT'?'sm35bad':'sm35muted';
  dir.textContent=!a?'BEKLENİYOR':!a.ready?'EKSİK':a.direction==='LONG'?'YUKARI':a.direction==='SHORT'?'AŞAĞI':'NÖTR';
  const foot=d.createElement('small');foot.textContent=a?.ready?`${a.count} mum · ${dec(a.age_ms/1000,0)} sn`:a?.reason?'Veri eksik':'—';
  b.append(name,dir,foot);b.addEventListener('click',()=>{state.tf=tf;render();});cards.append(b);
 }}
 for(const [btn,val] of [['ob','showOB'],['fvg','showFVG'],['sweep','showSweep'],['chart','showChart'],['labels','showLabels']]){
  const b=host.querySelector(`button[data-sm35="${btn}"]`);if(b){b.classList.toggle('active',state[val]);b.setAttribute('aria-pressed',String(state[val]));}}
 const z=state.rows[state.tf],dest=el('smc35Rows'),detail=el('smc35Detail');if(!dest||!detail)return;
 if(!z?.ready){detail.textContent=z?.reason||'Kapanmış mumlar ve doğrulanmış SMC hesapları bekleniyor.';dest.replaceChildren();return;}
 const latest=z.lastBreak?`${z.lastBreak.type} ${z.lastBreak.direction==='LONG'?'↑':'↓'}`:'Son yapısal kırılım yok';
 detail.textContent=`${state.tf} · ${z.count} kapanmış mum · ${latest} · ${dec(z.age_ms/1000,0)} sn gecikme · ${dec(z.atr,4)} ATR · ${z.premiumDiscount||'Bölge bilinmiyor'} · OB ${z.activeOB.length} / FVG ${z.activeFVG.length} / Sweep ${z.sweeps.length}`;
 dest.replaceChildren();
 const sec=(title,items,make)=>{const heading=d.createElement('div');heading.className='sm35subtitle';heading.textContent=title;dest.append(heading);
  if(!items.length){const none=d.createElement('div');none.className='sm35row';none.textContent='Son kapanmış mumlarda uygun aday bulunmadı.';dest.append(none);return;}
  for(const it of items.slice(0,4)){const tmp=d.createElement('div');tmp.innerHTML=make(it);dest.append(tmp.firstElementChild);}};
 if(state.showOB)sec('Order Block · En güncel adaylar',z.activeOB,x=>rowHTML(
  `${x.side==='LONG'?'YUKARI':'AŞAĞI'} OB · ${x.quality}`,`${x.score}/100 · ${x.status}`,
  `${dec(x.lower,4)} — ${dec(x.upper,4)} · ${x.ago} mum önce · Genişlik ${dec(x.widthAtr,2)} ATR`,x.side));
 if(state.showFVG)sec('Fair Value Gap · Dolum ve kalite',z.activeFVG,x=>rowHTML(
  `${x.side==='LONG'?'YUKARI':'AŞAĞI'} FVG · ${x.quality}`,`${x.score}/100 · ${x.status}`,
  `${dec(x.lower,4)} — ${dec(x.upper,4)} · ${dec(x.filledPct,0)}% dolu · ${x.ago} mum`,x.side));
 if(state.showSweep)sec('Likidite Süpürmesi · Fitil geri dönüşü',z.sweeps,x=>rowHTML(
  x.description,`${x.score}/100 · ${x.quality}`,
  `${dec(x.price,4)} · ${x.ago} mum · Fitil ${dec(x.wickAtr,2)} ATR · Hacim x${dec(x.volumeRatio,2)}`,x.side));
 const foot=d.createElement('div');foot.className='sm35callout';foot.textContent=`Geçersiz / dolmuş adaylar: OB ${z.invalidOB}, FVG ${z.invalidFVG}. Veri ve puanlar yalnız bu mum penceresine aittir. Kalite puanı başarı olasılığı değildir.`;dest.append(foot);
}
function line(chart,points,color,groupId,dashed){try{chart.createOverlay({name:'segment',groupId,lock:true,visible:true,points,
 styles:{line:{color,size:1.4,style:dashed?'dashed':'solid'},point:{color:'transparent',borderColor:'transparent',radius:0}}});}catch(_){}}
function tag(chart,at,value,label,color,groupId){if(!state.showLabels)return;try{chart.createOverlay({name:'simpleTag',groupId,lock:true,points:[{timestamp:at,value}],extendData:label,
 styles:{line:{color,size:1},text:{color,size:10}}});}catch(_){}}
function draw(){const bridge=root.timiKlineSMCBridge?.snapshot?.();const chart=bridge?.chart;
 if(!state.showChart){if(state.chart)emptyChart(state.chart);state.chart=null;state.lastDrawKey='';return;}
 if(!bridge?.ready||!chart)return;
 if(state.chart&&state.chart!==chart)emptyChart(state.chart);
 state.chart=chart;
 const tf=bridge.tf,s=bridge.symbol,z=state.rows[tf];
 if(s!==currentSymbol()||!TF_LIST.includes(tf)||!z?.ready||Date.now()-z.at>2*STEPS[tf]+15000){emptyChart(chart);state.lastDrawKey='';return;}
 const bars=bridge.bars||[],lastChartBar=bars.at(-1)?.timestamp;
 const key=[s,tf,z.lastClosed,lastChartBar,state.showOB,state.showFVG,state.showSweep,state.showLabels].join('|');
 if(key===state.lastDrawKey)return;
 emptyChart(chart);state.lastDrawKey=key;
 const end=finite(num(lastChartBar))?num(lastChartBar):z.lastClosed;
 if(state.showOB)for(const ob of z.activeOB.slice(0,2)){
  const c=ob.side==='LONG'?'#2bddaa':'#ff7998';
  line(chart,[{timestamp:ob.at,value:ob.lower},{timestamp:end,value:ob.lower}],c,groups[0],true);
  line(chart,[{timestamp:ob.at,value:ob.upper},{timestamp:end,value:ob.upper}],c,groups[0],true);
  tag(chart,end,ob.upper,'OB '+ob.score+'/100',c,groups[0]);}
 if(state.showFVG)for(const f of z.activeFVG.slice(0,2)){
  const c=f.side==='LONG'?'#16cbb4':'#e59878';
  line(chart,[{timestamp:f.at,value:f.lower},{timestamp:end,value:f.lower}],c,groups[1],true);
  line(chart,[{timestamp:f.at,value:f.upper},{timestamp:end,value:f.upper}],c,groups[1],true);
  tag(chart,end,f.upper,'FVG '+f.score+'/100',c,groups[1]);}
 if(state.showSweep)for(const sw of z.sweeps.slice(0,2)){
  const c='#ffcc72';line(chart,[{timestamp:sw.at,value:sw.price},{timestamp:end,value:sw.price}],c,groups[2],true);
  tag(chart,sw.at,sw.price,'Sweep',c,groups[2]);}
}
function boot(){if(!install())return;resetKey();
 const smcBtn=d.querySelector('[data-pro-tab="smc"]');smcBtn?.addEventListener('click',()=>setTimeout(()=>void fetchAll(false),180));
 el('symbol')?.addEventListener('change',()=>{resetKey();if(active())void fetchAll(true)});
 el('saveGateway')?.addEventListener('click',()=>setTimeout(()=>{resetKey();if(active())void fetchAll(true)},380));
 setInterval(()=>{resetKey();if(active()&&el('timiSmc35')?.open)void fetchAll(false);if(state.showChart)draw();},4300);
 root.timiSmc35Debug=()=>({version:'3.5.0',readOnly:true,persistence:false,tf:state.tf,symbol:currentSymbol(),
  valid:TF_LIST.filter(tf=>state.rows[tf]?.ready).length,chart:state.showChart});
}
if(d.readyState==='loading')d.addEventListener('DOMContentLoaded',boot,{once:true});else boot();
})(typeof window!=='undefined'?window:null);
