/* TiMi SCALP PRO — 3 Profesyonel Laboratuvar (v1.4)
 * Developer: Suat DOGU. Read-only, no private API keys or order submission.
 * Original independent implementation informed by public open-source concepts.
 * NOT a copy or installation of EdgeDepth, Flowsurface, Freqtrade or Jesse.
 * Existing TiMi WebSocket, chart, 26 indicators and signal engine remain intact.
 */
(function(root){
'use strict';
const TF_STEP={'1m':60000,'3m':180000,'5m':300000,'15m':900000,'30m':1800000,'1h':3600000,'4h':14400000,'1d':86400000};
const SYMS=['BTCUSDT','ETHUSDT','SOLUSDT','BNBUSDT','XRPUSDT','DOGEUSDT','ADAUSDT','AVAXUSDT'];
const money=n=>Number.isFinite(n)?n.toLocaleString('tr-TR',{minimumFractionDigits:2,maximumFractionDigits:2}):'—';
const dec=(n,k=2)=>Number.isFinite(n)?n.toLocaleString('tr-TR',{minimumFractionDigits:0,maximumFractionDigits:k}):'—';
const num=x=>x===undefined||x===null||x===''?NaN:Number(x);
const finite=x=>Number.isFinite(x);
const percent=x=>finite(x)?dec(x,2)+'%':'—';
const date=x=>finite(x)?new Date(x).toLocaleString('tr-TR',{month:'2-digit',day:'2-digit',hour:'2-digit',minute:'2-digit'}):'—';
const fmtPrice=n=>finite(n)?dec(n,n<1?6:2):'—';
function validateBars(rows, closedOnly=true){
  const unique=new Map();
  for(const v of Array.isArray(rows)?rows:[]){
    const t=num(v.t??v.timestamp),o=num(v.o??v.open),h=num(v.h??v.high),l=num(v.l??v.low),c=num(v.c??v.close),vol=num(v.v??v.volume??0);
    if(![t,o,h,l,c,vol].every(finite)||t<1e11||o<=0||l<=0||h<Math.max(o,c,l)||l>Math.min(o,c,h)||vol<0)continue;
    if(closedOnly && v.closed!==true)continue;
    unique.set(t,{t,o,h,l,c,v:vol});
  }
  return [...unique.values()].sort((a,b)=>a.t-b.t);
}
function emaSeries(values,period){
  const out=Array(values.length).fill(null);if(values.length<period)return out;
  let sum=0;for(let i=0;i<period;i++)sum+=values[i];
  let prev=sum/period;out[period-1]=prev;const alpha=2/(period+1);
  for(let i=period;i<values.length;i++){prev=values[i]*alpha+prev*(1-alpha);out[i]=prev;}
  return out;
}
function rsiSeries(values,period=14){
  const out=Array(values.length).fill(null);if(values.length<=period)return out;
  let gain=0,loss=0;
  for(let i=1;i<=period;i++){const change=values[i]-values[i-1];gain+=Math.max(0,change);loss+=Math.max(0,-change);}
  gain/=period;loss/=period;
  const toRsi=()=>loss===0?(gain===0?50:100):100-(100/(1+gain/loss));
  out[period]=toRsi();
  for(let i=period+1;i<values.length;i++){
    const d=values[i]-values[i-1];gain=(gain*(period-1)+Math.max(0,d))/period;
    loss=(loss*(period-1)+Math.max(0,-d))/period;out[i]=toRsi();
  }
  return out;
}
function computeBacktest(bars,kind,feePct=0.1,slipPct=0.03){
  if(!['ema','rsi'].includes(kind))throw Error('Bilinmeyen strateji');
  if(!finite(feePct)||!finite(slipPct)||feePct<0||feePct>0.5||slipPct<0||slipPct>0.5)throw Error('Maliyet araligi 0..0.5% olmali');
  const data=validateBars(bars,false),fee=feePct/100,slip=slipPct/100;
  if(data.length<100)throw Error('En az 100 kapali mum gerekiyor');
  const ema20=emaSeries(data.map(b=>b.c),20),ema50=emaSeries(data.map(b=>b.c),50),rsi=rsiSeries(data.map(b=>b.c),14);
  let cash=1000,units=0,entry=0,entryTime=0,entryCapital=0,trades=[],curve=[];
  let peak=1000,maxDD=0,incomplete=0;
  // Use indicators from previous CLOSED candle; execute at the NEXT candle open.
  // A bar with a missing timestamp breaks continuity and cancels the trade for that bar.
  for(let i=51;i<data.length;i++){
    const prev=data[i-1],prev2=data[i-2],bar=data[i];
    const step=bar.t-prev.t;
    if(step<=0||step>(data[1].t-data[0].t)*1.02){
      incomplete++;curve.push({t:bar.t,equity:cash+units*bar.c*(1-fee-slip)});continue;
    }
    let buy=false,sell=false;
    if(kind==='ema' && [ema20[i-1],ema50[i-1],ema20[i-2],ema50[i-2]].every(finite)){
      buy=ema20[i-2]<=ema50[i-2] && ema20[i-1]>ema50[i-1];
      sell=ema20[i-2]>=ema50[i-2] && ema20[i-1]<ema50[i-1];
    }else if(kind==='rsi' && finite(rsi[i-1])){
      buy=rsi[i-1]<30 && finite(ema50[i-1]) && prev.c>=ema50[i-1]*0.94;
      sell=rsi[i-1]>55;
    }
    if(units>0&&sell){
      const execution=bar.o*(1-slip),proceeds=units*execution*(1-fee);
      trades.push({entryAt:entryTime,exitAt:bar.t,entry,exit:execution,pnlPct:(proceeds/entryCapital-1)*100,pnl:proceeds-entryCapital});
      cash=proceeds;units=0;entry=0;
    }else if(units===0&&buy){
      entryCapital=cash;entry=bar.o*(1+slip);entryTime=bar.t;
      units=cash/(entry*(1+fee));cash=0;
    }
    const mark=cash+units*bar.c*(1-fee-slip);
    peak=Math.max(peak,mark);maxDD=Math.max(maxDD,(peak-mark)/peak*100);
    curve.push({t:bar.t,equity:mark});
  }
  const last=data.at(-1),finalEquity=cash+units*last.c*(1-fee-slip);
  const winners=trades.filter(t=>t.pnl>0).length,losers=trades.filter(t=>t.pnl<=0).length;
  return {kind,capital:1000,equity:finalEquity,returnPct:(finalEquity/1000-1)*100,
    maxDrawdownPct:maxDD,tradeCount:trades.length,winRate:trades.length?winners/trades.length*100:null,
    winners,losers,openPosition:units>0,missingIntervals:incomplete,trades,curve,
    from:data[51].t,to:last.t,candles:data.length,feePct,slipPct,
    dataWarning:trades.length<10?'Az işlem: sonuç istatistiksel olarak güvenilir değil':null};
}
function snapshotBook(source){
  const bids=(Array.isArray(source?.bids)?source.bids:[]).filter(x=>Array.isArray(x)&&finite(num(x[0]))&&finite(num(x[1]))&&num(x[0])>0&&num(x[1])>0).slice(0,20);
  const asks=(Array.isArray(source?.asks)?source.asks:[]).filter(x=>Array.isArray(x)&&finite(num(x[0]))&&finite(num(x[1]))&&num(x[0])>0&&num(x[1])>0).slice(0,20);
  const bidNotional=bids.reduce((s,x)=>s+num(x[0])*num(x[1]),0),askNotional=asks.reduce((s,x)=>s+num(x[0])*num(x[1]),0);
  const bid=bids[0]?.[0],ask=asks[0]?.[0],spread=finite(bid)&&finite(ask)&&ask>bid?(ask-bid)/((ask+bid)/2)*10000:null;
  const balance=bidNotional+askNotional>0?(bidNotional-askNotional)/(bidNotional+askNotional)*100:null;
  const bqty=num(bids[0]?.[1]),aqty=num(asks[0]?.[1]);
  const micro=finite(bqty)&&finite(aqty)&&bqty+aqty>0&&(ask>bid)?(ask*bqty+bid*aqty)/(bqty+aqty):null;
  return {bids,asks,bidNotional,askNotional,spreadBps:spread,balancePct:balance,microPrice:micro};
}
const tests={validateBars,emaSeries,rsiSeries,computeBacktest,snapshotBook};
if(typeof module==='object'&&module.exports)module.exports=tests;
if(!root||!root.document)return;
if(root.__timiProSuiteInstalled)return;root.__timiProSuiteInstalled=true;

const d=root.document;
const el=id=>d.getElementById(id);
let tab='flow',currentContext='',history=[],lastSample=0,mtfCache=new Map(),mtfSerial=0,backtestSerial=0,auditSerial=0;
let lastTest=null,backtestSymbol='',lastAnalysisResult=null,mtfBusy=false,runBusy=false;
const getSymbol=()=>{try{return SYMS.includes(symbol)?symbol:SYMS.includes(el('symbol')?.value)?el('symbol').value:'BTCUSDT';}catch(_){return SYMS.includes(el('symbol')?.value)?el('symbol').value:'BTCUSDT';}};
const getGateway=()=>{let g='';try{g=String(gateway||'')}catch(_){};try{const u=new URL(g||root.location.origin);if(u.protocol==='https:'||(u.hostname==='localhost'&&u.protocol==='http:'))return u.origin;}catch(_){}return root.location.origin;};
const sourceMarket=()=>{try{return typeof market==='object'&&market?market:null}catch(_){return null}};
const sourceAnalysis=()=>{try{return typeof analysis==='object'&&analysis?analysis:null}catch(_){return null}};
const analysisTime=()=>{try{return typeof analysisAt==='number'?analysisAt:0}catch(_){return 0}};
const connected=()=>{try{return !!ws && ws.readyState===1}catch(_){return false}};
const tradeStamp=()=>{try{return typeof lastTrade==='number'?lastTrade:0}catch(_){return 0}};
const bookStamp=()=>{try{return typeof lastBook==='number'?lastBook:0}catch(_){return 0}};
const stillCurrent=(s,g)=>getSymbol()===s&&getGateway()===g;
const show=(id,text)=>{const node=el(id);if(node)node.textContent=text;};
const active=()=>el('proLab')?.classList.contains('active')===true;
const inTab=t=>active()&&tab===t;
async function request(path,timeout=13000){
  const controller=new AbortController(),handle=setTimeout(()=>controller.abort(),timeout);
  try {const response=await fetch(getGateway()+path,{cache:'no-store',signal:controller.signal});
    if(!response.ok)throw Error('Gateway HTTP '+response.status);
    return await response.json();
  }finally{clearTimeout(handle);}
}
function safeStr(x){return String(x??'').replace(/[&<>"']/g,c=>({'&':'&amp;','<':'&lt;','>':'&gt;','"':'&quot;',"'":'&#39;'}[c]));}
function notice(message){show('proNotice',message);}
function formatAge(ms){return finite(ms)&&ms>=0?dec(ms/1000,1)+' sn':'—';}
function makeUI(){
  const main=d.querySelector('main'),more=el('more'),nav=d.querySelector('nav.foot');
  if(!main||!more||!nav||el('proLab'))return false;
  const section=d.createElement('section');section.id='proLab';section.className='section';
  section.innerHTML=`
<div class="pro-shell">
 <div class="pro-heading"><div><strong>TiMi <span>PRO LAB</span></strong><small>3 profesyonel analiz merkezi · Salt okunur</small></div><span id="proDataHealth" class="pro-pill">VERİ BEKLENİYOR</span></div>
 <div class="pro-pills" role="tablist" aria-label="Profesyonel analizler">
  <button data-pro-tab="flow" class="selected" type="button" role="tab" aria-selected="true">≋ Akış PRO</button>
  <button data-pro-tab="smc" type="button" role="tab" aria-selected="false">◇ SMC & MTF</button>
  <button data-pro-tab="lab" type="button" role="tab" aria-selected="false">▤ Strateji</button>
 </div>
 <div id="proNotice" class="pro-notice">İşlem ve analiz verileri kontrol ediliyor.</div>
 <div id="proFlow" class="pro-panel" role="tabpanel">
  <div class="pro-mini-head"><strong>Emir Akışı · İlk 20 seviye</strong><span id="proFlowAge">—</span></div>
  <div class="pro-grid pro-grid3">
   <div class="pro-stat"><small>Alış likiditesi</small><strong id="proBids">—</strong><em>USDT</em></div>
   <div class="pro-stat"><small>Satış likiditesi</small><strong id="proAsks">—</strong><em>USDT</em></div>
   <div class="pro-stat"><small>Defter dengesi</small><strong id="proImbalance">—</strong><em>±100% aralığı</em></div>
  </div>
  <div class="pro-meter"><span id="proBookMeter"></span></div>
  <div class="pro-grid pro-grid3">
   <div class="pro-stat"><small>Spread</small><strong id="proSpread">—</strong><em>baz puan</em></div>
   <div class="pro-stat"><small>Mikrofiyat</small><strong id="proMicro">—</strong><em>en iyi seviye</em></div>
   <div class="pro-stat"><small>10 sn baskı farkı</small><strong id="proPressure">—</strong><em>karşılaştırma</em></div>
  </div>
  <h4>Gerçekleşen işlemler · 60 saniye</h4>
  <div class="pro-grid pro-grid3">
   <div class="pro-stat"><small>Agresif AL</small><strong id="proAggBuy" class="green">—</strong><em>USDT</em></div>
   <div class="pro-stat"><small>Agresif SAT</small><strong id="proAggSell" class="red">—</strong><em>USDT</em></div>
   <div class="pro-stat"><small>Net delta</small><strong id="proCvd">—</strong><em>son 60 sn</em></div>
  </div>
  <h4>Canlı defter baskısı · Bu oturum</h4>
  <div id="proFlowTrend" class="pro-chart-small">Yeni gözlem bekleniyor</div>
  <h4>Likidite seviyesi · Anlık görünüm</h4>
  <div class="pro-depth"><div><b class="green">Alış · İlk 8</b><div id="proBidLadder"></div></div><div><b class="red">Satış · İlk 8</b><div id="proAskLadder"></div></div></div>
  <details class="pro-help"><summary>Nasıl kullanılır? · EdgeDepth / Flowsurface yaklaşımı</summary>
   <p>Emir defteri, Binance Spot'un ilk 20 seviyesindeki <b>bekleyen</b> emir miktarlarıdır. Yeşil/kırmızı barlar, o andaki seviyelerin USDT tutarını gösterir; geçmiş likidite ısı haritası değildir.</p>
   <p>60 saniyelik agresif alış/satış ve delta, sunucunun <b>gerçekten gözlemlediği işlemlerdir</b>. Defterin bir yönde yoğun olması fiyatın o yöne kesin gideceğini göstermez. Defter bayatlarsa değerler güvenilmez kabul edilir.</p>
   <p>Kaynak fikirleri: <a href="https://github.com/edgedepthhq/edgedepth-terminal" target="_blank" rel="noopener noreferrer">EdgeDepth</a> · <a href="https://github.com/flowsurface-rs/flowsurface" target="_blank" rel="noopener noreferrer">Flowsurface</a> · <a href="https://github.com/bmoscon/cryptofeed" target="_blank" rel="noopener noreferrer">Cryptofeed</a>. Bu uygulama onların kodlarını çalıştırmaz.</p>
  </details>
 </div>
 <div id="proSmc" class="pro-panel" role="tabpanel" hidden>
  <div class="pro-mini-head"><strong>SMC yapı analizi · 1m / 5m / 15m</strong><button id="proSmcReload" type="button">Yenile</button></div>
  <div id="proMtfSummary" class="pro-state">Üç zaman dilimi için kapanmış mumlar kontrol ediliyor.</div>
  <div id="proMtfRows" class="pro-mtf-rows"></div>
  <h4>Aktif FVG bölgeleri</h4>
  <div id="proFvgRows" class="pro-fvg-grid"><span class="pro-muted">Veri bekleniyor</span></div>
  <div id="proSmcWarnings" class="pro-footnote">Yalnız kapanmış mumlar kullanılır. İleriye bakma ve açık mum doğrulaması yapılmaz.</div>
  <details class="pro-help"><summary>Nasıl kullanılır? · BOS, CHoCH, FVG</summary>
   <p><b>BOS:</b> Onaylanmış swing seviyesinin kapanışla aynı yönlü kırılımı. <b>CHoCH:</b> Önceki yapı kırılımı yönünün değişmesi. Bu kavramlar doğrudan AL/SAT emri değildir.</p>
   <p><b>FVG:</b> Üç ardışık kapanmış mumdaki fiyat boşluğu. "Kısmi" bölgeye temas edilip tamamı doldurulmadığını, "dolu" ise boşluğun kapandığını belirtir.</p>
   <p>1m/5m/15m hizası sadece <b>SMC yapı karşılaştırmasıdır</b>; TiMi'nin mevcut karar kurallarını değiştirmez. Kaynak fikirleri: <a href="https://github.com/Khaymat/pyvsmc" target="_blank" rel="noopener noreferrer">pyvsmc</a> ve <a href="https://github.com/klinecharts/KLineChart" target="_blank" rel="noopener noreferrer">KLineChart</a>.</p>
  </details>
 </div>
 <div id="proLabTest" class="pro-panel" role="tabpanel" hidden>
  <div class="pro-mini-head"><strong>Strateji araştırma laboratuvarı</strong><span class="pro-pill">SİMÜLASYON</span></div>
  <p class="pro-footnote">Gerçek Binance Spot kapanmış mumları · Şartlar bir önceki mumda belirlenir, sonraki mum açılışında örnek işlem hesaplanır. Gerçek emir veya TiMi sinyallerinin tarihi backtest'i değildir.</p>
  <div class="pro-controls"><label>Zaman dilimi <select id="proBtTf"><option value="1m">1m</option><option value="5m" selected>5m</option><option value="15m">15m</option><option value="1h">1h</option></select></label><label>Komisyon / taraf (%) <input id="proBtFee" type="number" min="0" max="0.5" step="0.01" value="0.10"></label><label>Kayma / taraf (%) <input id="proBtSlip" type="number" min="0" max="0.5" step="0.01" value="0.03"></label></div>
  <button type="button" class="pro-run" id="proRunBt">İki stratejiyi karşılaştır</button>
  <div id="proBtStatus" class="pro-state">Strateji testi başlatılmadı.</div>
  <div id="proBtResults" class="pro-test-grid"></div>
  <div id="proBtCurve" class="pro-chart-small" hidden></div>
  <button type="button" id="proExportBt" hidden>İşlem listesini CSV olarak indir</button>
  <h4>Gerçek TiMi sinyal gözlemleri · Ayrı çalışma</h4>
  <button type="button" id="proAuditReload">Sunucu gözlemlerini getir</button>
  <div id="proAudit" class="pro-state">İleriye dönük gözlemler bu testten bağımsızdır.</div>
  <details class="pro-help"><summary>Nasıl kullanılır? · Jesse / Freqtrade yaklaşımı</summary>
   <p><b>EMA20/50:</b> EMA20, EMA50 üzerine geçtiğinde sonraki mum açılışında örnek alış; aşağı geçtiğinde örnek çıkış. <b>RSI14:</b> RSI &lt; 30 ve fiyat EMA50'nin %6 altından daha yakınsa örnek alış; RSI &gt; 55 ise örnek çıkış. Yalnız Spot LONG, kaldıraç yok.</p>
   <p>Komisyon ve kayma giriş-çıkışta iki kez uygulanır. Sadece son 500 mum erişilebilir; işlem sayısı azsa sonuç güvenilir değildir. Veri dışı doğrulama, walk-forward ve spread bazlı gerçek yürütme simülasyonu henüz bulunmaz.</p>
   <p>"TiMi sinyal gözlemleri" bir <b>tarihsel backtest değildir</b>. Sunucunun gelecekte gözlemlediği 1m/5m fiyat değişimlerini ayrı gösterir; geçmişi sonradan yeniden üretmez.</p>
   <p>Kaynak fikirleri: <a href="https://github.com/jesse-ai/jesse" target="_blank" rel="noopener noreferrer">Jesse</a> · <a href="https://github.com/freqtrade/freqtrade" target="_blank" rel="noopener noreferrer">Freqtrade</a> · <a href="https://github.com/nautechsystems/nautilus_trader" target="_blank" rel="noopener noreferrer">NautilusTrader</a>.</p>
  </details>
 </div>
 <p class="pro-disclaimer">TiMi PRO LAB · Tasarım uyarlaması · Salt okunur analiz. Hiçbir sistem kesin getiri veya başarı olasılığı sunmaz.</p>
</div>`;
  main.insertBefore(section,more);
  const button=d.createElement('button');button.dataset.page='proLab';button.innerHTML='◈<div>PRO</div>';
  nav.insertBefore(button,nav.querySelector('[data-page="more"]'));
  button.onclick=()=>{if(typeof root.setPage==='function')root.setPage('proLab');else{
    d.querySelectorAll('.section').forEach(x=>x.classList.toggle('active',x.id==='proLab'));
    d.querySelectorAll('nav.foot button').forEach(x=>x.classList.toggle('active',x===button));
  }onOpen();};
  d.querySelectorAll('[data-pro-tab]').forEach(b=>b.onclick=()=>switchTab(b.dataset.proTab));
  el('proSmcReload').onclick=()=>loadMtf(true);
  el('proRunBt').onclick=runBacktests;
  el('proAuditReload').onclick=loadAudit;
  el('proExportBt').onclick=exportTrades;
  return true;
}
function switchTab(next){
  if(!['flow','smc','lab'].includes(next))return;
  tab=next;
  for(const b of d.querySelectorAll('[data-pro-tab]')){const selected=b.dataset.proTab===next;b.classList.toggle('selected',selected);b.setAttribute('aria-selected',String(selected));}
  for(const [id,value] of [['proFlow','flow'],['proSmc','smc'],['proLabTest','lab']])el(id).hidden=next!==value;
  if(next==='smc')loadMtf(false);
  if(next==='flow')renderFlow();
}
function onOpen(){checkContext();switchTab(tab);renderFlow();}
function checkContext(){
  const key=getGateway()+'|'+getSymbol();
  if(key===currentContext)return;
  currentContext=key;history=[];lastSample=0;mtfCache.clear();mtfSerial++;backtestSerial++;auditSerial++;
  lastTest=null;lastAnalysisResult=null;mtfBusy=false;runBusy=false;
  show('proMtfRows','');show('proFvgRows','');show('proBtResults','');show('proBtStatus','Yeni parite · test için yeniden başlatın');
  show('proAudit','Yeni parite · gözlem için yeniden getir');
  if(el('proExportBt'))el('proExportBt').hidden=true;
  if(el('proBtCurve'))el('proBtCurve').hidden=true;
}
function miniSpark(points){
  if(points.length<2)return '<span class="pro-muted">Oturumda en az iki güncel gözlem gerekli</span>';
  const w=320,h=90,min=-100,max=100;
  const coords=points.map((p,i)=>[(i/(points.length-1))*(w-14)+7,(h-12)-((p.value-min)/(max-min))*(h-20)]);
  const poly=coords.map(p=>p.map(q=>dec(q,2).replace(',','.')).join(',')).join(' ');
  return `<svg viewBox="0 0 ${w} ${h}" preserveAspectRatio="none" role="img" aria-label="Bu oturumdaki emir defteri dengesi"><path d="M0 ${h/2} H${w}" stroke="#466079" stroke-dasharray="4,4" fill="none"/><polyline points="${poly}" fill="none" stroke="#57dcb9" stroke-width="2.2" stroke-linejoin="round"/></svg>`;
}
function ladder(rows,color){
  const max=Math.max(...rows.map(x=>x[0]*x[1]),1);
  return rows.slice(0,8).map(x=>{const v=x[0]*x[1],pct=Math.max(3,v/max*100);
    return `<div class="pro-level"><div class="pro-level-fill ${color}" style="width:${pct}%"></div><span>${fmtPrice(x[0])}</span><strong>${dec(v,0)} $</strong></div>`;
  }).join('')||'<p class="pro-muted">Güncel seviye yok</p>';
}
function renderFlow(){
  if(!inTab('flow'))return;
  checkContext();const m=sourceMarket(),a=sourceAnalysis(),flow=a?.symbol===getSymbol()&&Date.now()-analysisTime()<10000?a.flow:null;
  const tAge=Date.now()-tradeStamp(),bAge=Date.now()-bookStamp();
  const healthy=connected() && m?.book_valid===true && tAge>=0&&tAge<5000 && bAge>=0&&bAge<5000;
  const book=healthy?snapshotBook(m):null;
  show('proDataHealth',healthy?'CANLI / GEÇERLİ':'VERİ BEKLENİYOR');
  el('proDataHealth').classList.toggle('pro-good',healthy);
  show('proFlowAge','İşlem '+formatAge(tAge)+' · Defter '+formatAge(bAge));
  for(const [id,value,suffix] of [['proBids',book?.bidNotional,''],['proAsks',book?.askNotional,''],['proImbalance',book?.balancePct,'%'],['proSpread',book?.spreadBps,''],['proMicro',book?.microPrice,'']])
    show(id,book?(id==='proMicro'?fmtPrice(value):id==='proImbalance'?percent(value):id==='proSpread'?dec(value,3):dec(value,0)):'—');
  el('proBookMeter').style.width=book?Math.max(0,Math.min(100,50+book.balancePct/2))+'%':'50%';
  show('proPressure',healthy&&flow&&a?.extras?.imbalance_delta_10s!==null&&finite(num(a?.extras?.imbalance_delta_10s))?percent(num(a.extras.imbalance_delta_10s)*100):'—');
  const validFlow=healthy&&flow?.fresh===true;
  const buy=validFlow?num(flow.buy_notional_60s):NaN,sell=validFlow?num(flow.sell_notional_60s):NaN;
  show('proAggBuy',finite(buy)?dec(buy,0):'—');show('proAggSell',finite(sell)?dec(sell,0):'—');
  show('proCvd',finite(buy)&&finite(sell)?dec(buy-sell,0):'—');
  show('proBidLadder', '');show('proAskLadder','');
  el('proBidLadder').innerHTML=book?ladder(book.bids,'buy'):'<p class="pro-muted">Geçerli derinlik bekleniyor</p>';
  el('proAskLadder').innerHTML=book?ladder(book.asks,'sell'):'<p class="pro-muted">Geçerli derinlik bekleniyor</p>';
  if(healthy&&book&&Date.now()-lastSample>=2000){lastSample=Date.now();history.push({time:lastSample,value:book.balancePct});history=history.slice(-100);}
  el('proFlowTrend').innerHTML=miniSpark(history);
  notice(healthy?'Binance Spot · WS canlı · 20 seviye · Salt okunur':'Binance emir verisi gecikmiş/geçersiz; eski değerlerden canlı analiz üretilmiyor.');
}
async function loadMtf(force){
  if(!inTab('smc'))return;
  checkContext();
  if(mtfBusy)return;
  const s=getSymbol(),g=getGateway(),key=g+'|'+s, cached=mtfCache.get(key);
  if(!force&&cached&&Date.now()-cached.time<60000){displayMtf(cached.rows);return;}
  if(typeof root.timiSmcReadOnly?.analyzeClosedBars!=='function'){
    show('proMtfSummary','SMC motoru erişimi doğrulanmadı; grafik motoruna müdahale edilmedi.');return;
  }
  const seq=++mtfSerial;mtfBusy=true;show('proMtfSummary',s+' · 1m / 5m / 15m kapanmış mumlar yükleniyor…');
  const tfs=['1m','5m','15m'];
  const values=await Promise.allSettled(tfs.map(async tf=>{
    const d=await request('/api/klines/'+s+'?interval='+tf+'&limit=300');
    if(d.symbol!==s||d.interval!==tf||!Array.isArray(d.candles))throw Error('Mum yanıtı uyuşmuyor');
    const bars=validateBars(d.candles,true);
    if(bars.length<50)throw Error('En az 50 kapanmış mum gerekli');
    const last=bars.at(-1);const step=TF_STEP[tf];
    const freshness=Date.now()-(last.t+step);
    if(freshness>2*step+10000||freshness< -5000)throw Error('Son kapanmış mum güncel değil');
    const result=root.timiSmcReadOnly.analyzeClosedBars(bars,tf);
    const lastBreak=result.breaks.at(-1);
    const recent=lastBreak&&last.t-lastBreak.time<=30*step?lastBreak:null;
    const fvgs=result.fvgs.filter(z=>(z.status==='AKTİF'||z.status==='KISMİ')&&z.endAt>=last.t-90*step).slice(-4);
    return {tf,barCount:bars.length,lastClosed:last.t,trend:recent?.direction||'NÖTR',lastBreak:lastBreak||null,fvgs};
  }));
  if(seq!==mtfSerial||!stillCurrent(s,g)){mtfBusy=false;return;}
  const rows=values.map((v,i)=>v.status==='fulfilled'?v.value:{tf:tfs[i],error:String(v.reason?.message||v.reason||'Hata')});
  mtfCache.set(key,{time:Date.now(),rows});mtfBusy=false;displayMtf(rows);
}
function displayMtf(rows){
  lastAnalysisResult=rows;
  const known=rows.filter(x=>!x.error),long=known.filter(x=>x.trend==='LONG').length,short=known.filter(x=>x.trend==='SHORT').length;
  const unanimous=known.length===3&&(long===3||short===3);
  const state=unanimous?(long===3?'SMC UYUMU: 3/3 YUKARI':'SMC UYUMU: 3/3 AŞAĞI'):'SMC UYUMU: TAM DOĞRULANMADI';
  show('proMtfSummary',state+' · '+getSymbol()+' · '+known.length+'/3 geçerli TF');
  el('proMtfSummary').className='pro-state '+(unanimous?'pro-match':'');
  el('proMtfRows').innerHTML=rows.map(x=>{
    if(x.error)return `<div class="pro-mtf-row"><strong>${x.tf}</strong><span class="pro-warn">${safeStr(x.error)}</span></div>`;
    const cls=x.trend==='LONG'?'green':x.trend==='SHORT'?'red':'pro-muted';
    const detail=x.lastBreak?x.lastBreak.type+' '+(x.lastBreak.direction==='LONG'?'↑':'↓')+' · '+date(x.lastBreak.time):'Son 300 mumda BOS bulunamadı';
    return `<div class="pro-mtf-row"><div><strong>${x.tf} · ${x.barCount} mum</strong><small>Son kapanış: ${date(x.lastClosed+TF_STEP[x.tf])}</small></div><div class="pro-mtf-direction ${cls}">${x.trend}</div><small>${safeStr(detail)}</small></div>`;
  }).join('');
  const fvg=known.flatMap(x=>x.fvgs.map(z=>({tf:x.tf,...z})));
  el('proFvgRows').innerHTML=fvg.length?fvg.map(z=>`<div class="pro-fvg"><div><strong class="${z.side==='BULL'?'green':'red'}">${z.tf} · ${z.side==='BULL'?'YUKARI':'AŞAĞI'} FVG</strong><span>${safeStr(z.status)} · ${dec(z.filledPct,0)}% dolu</span></div><small>${fmtPrice(z.lower)} — ${fmtPrice(z.upper)}</small></div>`).join(''):'<p class="pro-muted">Son taranan aralıkta aktif FVG bulunamadı.</p>';
  show('proSmcWarnings','SMC motoru: TiMi v1.2 kapanmış mum hesaplaması. Yapı kırılımı yalnızca son 30 mumdaysa güncel yön sayılır. Kaynak: Binance Spot REST.');
}
async function runBacktests(){
  if(!inTab('lab'))return;
  checkContext();
  if(runBusy)return;
  const s=getSymbol(),g=getGateway(),tf=el('proBtTf').value;
  const fee=num(el('proBtFee').value),slip=num(el('proBtSlip').value);
  if(!finite(fee)||!finite(slip)||fee<0||fee>0.5||slip<0||slip>0.5){show('proBtStatus','Komisyon ve kayma 0 ile %0,50 arasında olmalı.');return;}
  const seq=++backtestSerial;runBusy=true;el('proRunBt').disabled=true;
  show('proBtStatus',s+' · '+tf+' · 500 mum yükleniyor, gerçek geçmiş veri hesaplanıyor…');
  try{
    const data=await request('/api/klines/'+s+'?interval='+tf+'&limit=500',18000);
    if(seq!==backtestSerial||!stillCurrent(s,g))return;
    if(data.symbol!==s||data.interval!==tf||!Array.isArray(data.candles))throw Error('Mum yanıtı uyuşmuyor');
    const closed=validateBars(data.candles,true);
    if(closed.length<120)throw Error('Yeterli kapanmış mum yok');
    const step=TF_STEP[tf],last=closed.at(-1);
    if(Date.now()-(last.t+step)>2*step+10000)throw Error('Son mum güncel değil');
    const results=[computeBacktest(closed,'ema',fee,slip),computeBacktest(closed,'rsi',fee,slip)];
    lastTest={symbol:s,tf,fee,slip,results,from:closed[0].t,to:last.t};backtestSymbol=s;
    displayTests(results,tf);
  }catch(e){show('proBtStatus','Strateji testi başarısız: '+String(e.message||e));show('proBtResults','');el('proExportBt').hidden=true;el('proBtCurve').hidden=true;}
  finally{runBusy=false;el('proRunBt').disabled=false;}
}
function displayTests(results,tf){
  const names={ema:'EMA20/50 kesişimi',rsi:'RSI14 dönüş denemesi'};
  el('proBtResults').innerHTML=results.map(r=>`<div class="pro-test-card"><strong>${names[r.kind]}</strong><span class="pro-test-return ${r.returnPct>=0?'green':'red'}">${r.returnPct>=0?'+':''}${percent(r.returnPct)}</span>
    <div><small>Bitiş sermayesi</small><b>${money(r.equity)} USDT</b></div>
    <div><small>Maks. düşüş</small><b>${percent(r.maxDrawdownPct)}</b></div>
    <div><small>Tamamlanan işlemler</small><b>${r.tradeCount}</b></div>
    <div><small>Kârlı işlem oranı</small><b>${r.tradeCount?percent(r.winRate):'—'}</b></div>
    <div><small>Açık sanal pozisyon</small><b>${r.openPosition?'Var (kâğıt üstünde)':'Yok'}</b></div>
    <p class="pro-footnote ${r.tradeCount<10?'pro-warn':''}">${r.dataWarning||'Örnek çalışma; ileri dönem doğrulaması gerekli'}</p></div>`).join('');
  show('proBtStatus',`Araştırma: ${lastTest.symbol} · ${tf} · ${results[0].candles} kapalı mum · ${date(lastTest.from)} – ${date(lastTest.to)} · Başlangıç 1.000 USDT · Komisyon ${dec(lastTest.fee)}% + kayma ${dec(lastTest.slip)}% / taraf`);
  const all=results[0].curve;
  if(all.length>=2){
    const w=340,h=100,min=Math.min(...all.map(x=>x.equity))*0.997,max=Math.max(...all.map(x=>x.equity))*1.003;
    const points=all.map((x,i)=>`${(i/(all.length-1)*(w-8)+4).toFixed(1)},${(h-5-((x.equity-min)/Math.max(0.00001,max-min))*(h-12)).toFixed(1)}`).join(' ');
    el('proBtCurve').hidden=false;
    el('proBtCurve').innerHTML=`<strong>EMA · Tarihsel sermaye eğrisi</strong><svg viewBox="0 0 ${w} ${h}" preserveAspectRatio="none" role="img" aria-label="Komisyonlu örnek sermaye eğrisi"><polyline points="${points}" fill="none" stroke="#69d6f7" stroke-width="2"/></svg>`;
  }
  el('proExportBt').hidden=results.every(r=>r.trades.length===0);
}
function exportTrades(){
  if(!lastTest||lastTest.symbol!==getSymbol())return;
  const rows=['strategy,entry_utc,exit_utc,entry_price,exit_price,net_pnl,net_pnl_pct'];
  for(const r of lastTest.results)for(const t of r.trades)
    rows.push([r.kind,new Date(t.entryAt).toISOString(),new Date(t.exitAt).toISOString(),t.entry,t.exit,t.pnl,t.pnlPct].join(','));
  const blob=new Blob([rows.join('\n')+'\n'],{type:'text/csv;charset=utf-8'}),url=URL.createObjectURL(blob);
  const a=d.createElement('a');a.href=url;a.download=`timi_strateji_${lastTest.symbol}_${lastTest.tf}.csv`;a.click();setTimeout(()=>URL.revokeObjectURL(url),1500);
}
async function loadAudit(){
  if(!inTab('lab'))return;
  checkContext();const s=getSymbol(),g=getGateway(),seq=++auditSerial;
  show('proAudit','TiMi sunucu gözlemleri kontrol ediliyor…');
  try{
    const result=await request('/api/signal-audit/'+s+'?limit=20');
    if(seq!==auditSerial||!stillCurrent(s,g))return;
    if(result.symbol!==s)throw Error('Sembol uyuşmazlığı');
    if(result.enabled!==true){show('proAudit',s+' için ileriye dönük sunucu gözlemleme açık değil. Render üzerinde varsayılan etkin sembol BTCUSDT olabilir.');return;}
    const h1=result.summary?.horizons?.['1m'],h5=result.summary?.horizons?.['5m'];
    function line(h,name){return h?`${name}: ${h.measured||0} ölçüm · yön isabeti ${h.measured?percent(h.directional_hit_rate_pct):'—'} · ort. yönlü fiyat hareketi ${h.measured?percent(h.mean_signed_price_change_pct):'—'}${h.measured<100?' · az örnek':''}`:`${name}: veri yok`;}
    show('proAudit',`Gözlem sayısı: ${result.summary?.total_observations??'—'} · ${line(h1,'1m')} · ${line(h5,'5m')}. Bu fiyat yönü çalışmasıdır: komisyon, kayma, pozisyon P&L ve kalıcı veritabanı içermez.`);
  }catch(e){if(seq===auditSerial)show('proAudit','Gözlem okunamadı: '+String(e.message||e));}
}
const CSS=`
#proLab .pro-shell{display:grid;gap:12px}
#proLab .pro-heading{display:flex;align-items:center;justify-content:space-between;gap:8px;padding:16px;background:linear-gradient(135deg,#182e4b,#0d1f33);border:1px solid #304960;border-radius:15px}
#proLab .pro-heading strong{font-weight:900;font-size:18px;letter-spacing:.2px}
#proLab .pro-heading strong span{color:#eabe64}
#proLab .pro-heading small{display:block;color:#a5bad0;margin-top:5px;font-size:11px}
#proLab .pro-pill{border:1px solid #5b6579;color:#c6b8a4;border-radius:20px;padding:5px 8px;font-size:10px;white-space:nowrap}
#proLab .pro-pill.pro-good{color:#52e0b3;border-color:#248f6e}
#proLab .pro-pills{display:grid;grid-template-columns:repeat(3,minmax(0,1fr));gap:6px;position:sticky;top:66px;z-index:3;background:#08111e;padding:5px 0}
#proLab .pro-pills button{padding:11px 4px;color:#c0d0e1;background:#1c2c42;border:1px solid #304b66;border-radius:11px;font-size:11px;font-weight:750;white-space:nowrap}
#proLab .pro-pills .selected{color:#70ead4;background:#154636;border-color:#278e74}
#proLab .pro-notice{color:#c0d3e8;font-size:11px;line-height:1.5}
#proLab .pro-panel{border-radius:15px;border:1px solid #2d435b;background:linear-gradient(135deg,#14263a,#0c1b2b);padding:12px;min-width:0}
#proLab .pro-panel[hidden]{display:none!important}
#proLab .pro-mini-head{display:flex;justify-content:space-between;gap:8px;align-items:center;margin-bottom:12px}
#proLab .pro-mini-head strong{font-size:13px}
#proLab .pro-mini-head>span{font-size:10px;color:#92b1cd}
#proLab button,#proLab select,#proLab input{font:inherit}
#proLab .pro-panel button:not(.pro-run){border-radius:8px;border:1px solid #45617d;background:#183047;color:#c9ddf3;padding:7px 10px;font-size:11px;cursor:pointer}
#proLab .pro-grid{display:grid;gap:7px;min-width:0}
#proLab .pro-grid3{grid-template-columns:repeat(3,minmax(0,1fr))}
#proLab .pro-stat{background:#1b3148;border:1px solid #25435d;border-radius:9px;padding:10px 6px;min-width:0;text-align:center}
#proLab .pro-stat small{display:block;color:#a2b8ce;font-size:10px;min-height:25px}
#proLab .pro-stat strong{display:block;font-size:clamp(12px,3.2vw,18px);margin:4px 0;overflow-wrap:anywhere}
#proLab .pro-stat em{font-style:normal;color:#8699b0;font-size:9px}
#proLab .pro-meter{margin:10px 0 12px;border-radius:12px;background:#d45f78;height:9px;overflow:hidden}
#proLab .pro-meter span{display:block;height:100%;width:50%;background:#24cda0;transition:width .3s}
#proLab h4{font-size:12px;margin:18px 0 9px;color:#d6e8fa}
#proLab .pro-chart-small{padding:10px;background:#0c1b2c;border:1px solid #28405c;border-radius:10px;min-height:82px;color:#aabfd4;font-size:11px}
#proLab .pro-chart-small svg{width:100%;height:90px;display:block}
#proLab .pro-depth{display:grid;grid-template-columns:repeat(2,minmax(0,1fr));gap:10px}
#proLab .pro-depth b{font-size:11px;display:block;margin-bottom:8px}
#proLab .pro-level{display:flex;justify-content:space-between;position:relative;font-size:10px;padding:5px 3px;gap:3px;overflow:hidden}
#proLab .pro-level-fill{position:absolute;inset:0 auto 0 0;opacity:.14;z-index:0}
#proLab .pro-level-fill.buy{background:#20d2a0}#proLab .pro-level-fill.sell{background:#f36b89}
#proLab .pro-level span,#proLab .pro-level strong{position:relative;z-index:1;white-space:nowrap}
#proLab .pro-level strong{font-weight:650}
#proLab .pro-state{padding:11px;background:#162b40;color:#aac5dc;border:1px solid #28455f;border-radius:10px;font-size:11px;line-height:1.6;margin:6px 0}
#proLab .pro-match{color:#65dfb2;border-color:#267d62}
#proLab .pro-mtf-rows{display:grid;gap:8px}
#proLab .pro-mtf-row{display:flex;align-items:center;flex-wrap:wrap;justify-content:space-between;gap:6px;padding:10px;border:1px solid #2b4763;border-radius:9px;background:#1a3048}
#proLab .pro-mtf-row strong{font-size:13px}#proLab .pro-mtf-row small{display:block;color:#a2b6cb;font-size:10px}
#proLab .pro-mtf-row>small{flex:1 1 100%}
#proLab .pro-mtf-direction{font-size:12px;font-weight:800}
#proLab .pro-fvg-grid{display:grid;gap:7px}
#proLab .pro-fvg{padding:10px;background:#1a3048;border-radius:9px;display:grid;gap:5px;font-size:11px}
#proLab .pro-fvg>div{display:flex;justify-content:space-between;gap:10px}#proLab .pro-fvg small{color:#a5bace}
#proLab .pro-help{border:1px solid #36516c;border-radius:9px;padding:10px;margin-top:14px;color:#9fb5cc;font-size:11px;line-height:1.7}
#proLab .pro-help summary{font-size:12px;color:#e1d0a5;cursor:pointer;font-weight:750}
#proLab .pro-help a{color:#77cfff}
#proLab .pro-footnote{color:#a4bbd0;font-size:11px;line-height:1.5}
#proLab .pro-warn{color:#ffd08a!important}
#proLab .pro-muted{color:#91abc2;font-size:11px}
#proLab .pro-controls{display:grid;grid-template-columns:repeat(2,minmax(0,1fr));gap:8px;margin:10px 0}
#proLab .pro-controls label{display:grid;gap:5px;font-size:10px;color:#bcd0df}
#proLab .pro-controls input,#proLab .pro-controls select{width:100%;min-width:0;border:1px solid #45617f;border-radius:7px;background:#1a3048;color:#e9f4ff;padding:9px 6px}
#proLab .pro-run{padding:12px;color:#0b2330;background:#63e0b4;border:none;border-radius:11px;font-size:12px;font-weight:800;width:100%}
#proLab .pro-run:disabled{opacity:.45}
#proLab .pro-test-grid{display:grid;gap:10px}
#proLab .pro-test-card{padding:12px;background:#1b3148;border:1px solid #34516c;border-radius:11px;display:grid;gap:8px}
#proLab .pro-test-card>strong{font-size:13px}
#proLab .pro-test-return{font-size:23px;font-weight:900}
#proLab .pro-test-card>div{display:flex;justify-content:space-between;font-size:11px;border-bottom:1px solid #304961;padding:6px 0;gap:9px}
#proLab .pro-test-card>div small{color:#aec3d4}
#proLab .pro-disclaimer{font-size:10px;color:#879aae;line-height:1.5}
@media(min-width:680px){#proLab .pro-test-grid{grid-template-columns:1fr 1fr}}
@media(max-width:390px){#proLab .pro-stat{padding:8px 4px}#proLab .pro-grid3{gap:5px}#proLab .pro-pills button{font-size:10px}}
`;
function boot(){
  if(!makeUI())return;
  const style=d.createElement('style');style.id='timiProSuiteStyles';style.textContent=CSS;d.head.append(style);
  el('symbol')?.addEventListener('change',()=>{checkContext();if(inTab('smc'))loadMtf(true);});
  el('saveGateway')?.addEventListener('click',()=>setTimeout(checkContext,300));
  root.timiProSuiteDebug=()=>({version:'1.4.0',readOnly:true,tab,currentSymbol:getSymbol(),samples:history.length,
    smcTimeframes:lastAnalysisResult?.length||0,backtest:lastTest?{symbol:lastTest.symbol,tf:lastTest.tf,strategies:lastTest.results.length}:null});
  setInterval(()=>{if(!active())return;checkContext();if(tab==='flow')renderFlow();else if(tab==='smc'&&!mtfBusy){const key=getGateway()+'|'+getSymbol();if(!mtfCache.has(key)||Date.now()-mtfCache.get(key).time>65000)loadMtf(false);}},1500);
}
if(d.readyState==='loading')d.addEventListener('DOMContentLoaded',boot,{once:true});else boot();
})(typeof window!=='undefined'?window:null);
