/* TiMi Intelligence v1 — additive HTML integration. No real orders, secrets or storage.
   Sources: existing read-only /api/klines, optional observed Binance Spot public trade stream,
   optional external time-aligned Futures adapter via ingestFutures().
*/
(function(root){'use strict';
 if(!root?.document||root.__timiIntelLoaded)return;
 const C=root.TiMiIntelCore;if(!C)return;root.__timiIntelLoaded=true;
 const d=root.document,el=id=>d.getElementById(id),state={symbol:'',gateway:'',series:{},run:0,at:0,busy:false,tradeSocket:null,trades:[],lastTrade:0,lastDepth:null,lastDepthAt:0,feedSymbol:'',feedOn:false,futures:null,cache:null,tabOpen:false,error:''};
 const escape=x=>String(x??'').replace(/[&<>"']/g,c=>({'&':'&amp;','<':'&lt;','>':'&gt;','"':'&quot;',"'":'&#39;'}[c]));
 const fmt=(v,n=2)=>Number.isFinite(v)?Number(v).toLocaleString('tr-TR',{maximumFractionDigits:n}):'—';
 const pct=v=>Number.isFinite(v)?fmt(v*100,2)+'%':'—';
 const txt=(id,value)=>{const node=el(id);if(node)node.textContent=value;};
 const key=()=>state.gateway+'|'+state.symbol;
 function getSymbol(){const s=String(el('symbol')?.value||'BTCUSDT').trim().toUpperCase();return /^[A-Z0-9]{5,20}$/.test(s)?s:'BTCUSDT';}
 function getGateway(){try{const raw=String(el('gateway')?.value||location.origin).trim();const value=raw||location.origin;const url=/^https?:\/\//i.test(value)?new URL(value):new URL(value,location.origin);if(url.protocol!=='https:'&&url.hostname!=='localhost')return '';return url.origin;}catch(_){return '';}}
 function attach(where,html){if(!where)return false;const n=d.createElement('section');n.innerHTML=html.trim();const node=n.firstElementChild;where.append(node);return true;}
 function panel(id,title,body){return `<details class="ti-int-card" id="${id}" open><summary>${title} <span class="ti-int-hint">v1 · salt okunur</span></summary><div class="ti-int-body">${body}</div></details>`;}
 function line(id){return `<div class="ti-int-line" id="${id}">Veri bekleniyor</div>`;}
 function styles(){if(el('timiIntelStyle'))return;const e=d.createElement('style');e.id='timiIntelStyle';e.textContent=`
 .ti-int-card{border:1px solid #315266;border-radius:13px;background:#132638;color:#eaf4fc;margin:12px 0;overflow:hidden;font:inherit}
 .ti-int-card>summary{padding:12px;cursor:pointer;font-size:13px;font-weight:800;color:#e8f2fb;list-style-position:inside}
 .ti-int-hint{float:right;color:#8babbf;font-size:10px;font-weight:400;margin-top:4px}
 .ti-int-body{padding:2px 11px 12px}.ti-int-line{border-radius:8px;padding:9px 8px;margin:7px 0;border:1px solid #2c4960;background:#1a3247;color:#c8e1ef;font-size:11px;line-height:1.55;overflow-wrap:anywhere}
 .ti-int-kpi{display:grid;grid-template-columns:repeat(2,minmax(0,1fr));gap:7px}.ti-int-kpi>div{border:1px solid #33576c;border-radius:8px;padding:9px;background:#193247;font-size:11px;line-height:1.55}
 .ti-int-kpi b{display:block;font-size:11px;color:#e7f5ff}.ti-int-kpi small{color:#a9c3d4;font-size:10px}
 .ti-int-good{color:#56ddb7}.ti-int-bad{color:#ff8a9d}.ti-int-wait{color:#f4d28e}
 .ti-int-actions{display:flex;gap:6px;flex-wrap:wrap;margin:8px 0}.ti-int-actions button,.ti-int-labbtn{padding:9px 11px;border-radius:9px;background:#203b54;border:1px solid #46647a;color:#eaf4fc;font-size:11px;font-weight:700;cursor:pointer}
 .ti-int-actions button:focus-visible,.ti-int-labbtn:focus-visible{outline:2px solid #2bd2bd;outline-offset:2px}
 .ti-int-note{font-size:10px;color:#a4bfd1;line-height:1.6;margin-top:7px}
 .ti-int-table{width:100%;border-collapse:collapse;font-size:10px}.ti-int-table th,.ti-int-table td{padding:7px 4px;text-align:left;border-bottom:1px solid #335266}.ti-int-table td:nth-child(n+2),.ti-int-table th:nth-child(n+2){text-align:right}
 #timiIntelLab[hidden]{display:none!important}#timiIntelLab{margin:12px 0}#timiIntelLab[aria-hidden="true"]{display:none}
 `;d.head.append(e);}
 function install(){if(el('timiIntelOverview'))return true;const summary=el('overview'),pro=el('proLab'),smc=el('proSmc');if(!summary||!pro||!smc)return false;styles();
  attach(summary,panel('timiIntelOverview','TiMi Intelligence · Ortak Karar',`${line('tiIntDecision')}${line('tiIntSources')}<div class="ti-int-note">Mevcut TiMi sinyallerine müdahale etmez. Bu bağımsız doğrulama paneli, başarı yüzdesi vaat etmez.</div>`));
  attach(smc,panel('timiIntelSmc','Piyasa Rejimi · SMC Zinciri · Kırılım',`${line('tiIntRegime')}${line('tiIntSMC')}${line('tiIntBreakout')}`));
  const chart=el('chart');if(chart)attach(chart,panel('timiIntelVWAP','VWAP · Hacim Profili Referansları',`${line('tiIntVWAP')}${line('tiIntPOC')}`));
  const flow=el('proFlow')||el('proOrderFlow')||smc;
  attach(flow,panel('timiIntelFlow','Order Flow · CVD · Absorpsiyon · Derinlik',`${line('tiIntFlow')}${line('tiIntAbsorption')}${line('tiIntDepth')}<div class="ti-int-actions"><button type="button" id="tiIntStartFeed">Canlı Spot işlemlerini izle</button><button type="button" id="tiIntStopFeed">Durdur</button></div><div class="ti-int-note">Mevcut TiMi Gateway WebSocket paylaşılır; Binance'e ikinci bağlantı açılmaz. İşlemler tarayıcı belleğinde en fazla son 5 dakika tutulur. 20 kademe kısmi derinlik gerçek OFI değildir. Bağlantı kapalıyken eski veri gösterilmez.</div>`));
  const deriv=el('proFutures')||el('proDerivatives')||smc;
  attach(deriv,panel('timiIntelFutures','Futures · Spot Ayrışması',`${line('tiIntFutures')}<div class="ti-int-note">Gerçek OI/funding/basis için borsa, sözleşme, birim ve zaman damgası doğrulayan Gateway adaptörü gerekir. Veri yoksa sonuç hesaplanmaz.</div>`));
  const strateji=el('proStrategy')||el('proStrategies')||el('proSignals')||smc;
  attach(strateji,panel('timiIntelMeta','ML Meta-Model · Sinyal Kalibrasyonu',`${line('tiIntML')}<div class="ti-int-note">Model yalnızca deneyseldir. Yeterli bağımsız dönem olmadan canlı karar ağırlığına katılmaz.</div>`));
  const lab=d.createElement('section');lab.id='timiIntelLab';lab.hidden=true;lab.innerHTML=`${panel('tiIntLabCard','Test Laboratuvarı · Strateji Karşılaştırması',`<div class="ti-int-actions"><button type="button" id="tiIntRunTest">Geçmiş mumlarla test et</button><button type="button" id="tiIntLabClose">Kapat</button></div>${line('tiIntTestIntro')}<div id="tiIntTestResults" class="ti-int-line">Test başlatılmadı.</div><div class="ti-int-note">Sinyal kapandıktan sonraki mumun açılışı varsayımsal giriş fiyatıdır. Komisyon + kayma için varsayılan 8 baz puan / yön maliyet kullanılır. Kısmi geçmişle çıkan sonuçlar yatırım başarısı veya ileri tarihli performans kanıtı değildir. Lookahead, purge ve zamanlı test doğrulanmalıdır.</div>`)}`;
  pro.append(lab);
  const nav=pro.querySelector('[data-pro-tab="smc"]'),btn=d.createElement('button');btn.id='timiIntelLabNav';btn.type='button';btn.className=(nav?.className||'')+' ti-int-labbtn';btn.textContent='◫ Test Laboratuvarı';if(nav?.parentNode)nav.parentNode.append(btn);else pro.prepend(btn);
  btn.addEventListener('click',()=>{state.tabOpen=!state.tabOpen;lab.hidden=!state.tabOpen;btn.setAttribute('aria-expanded',String(state.tabOpen));if(state.tabOpen){lab.scrollIntoView({behavior:'smooth',block:'start'});void refresh(false);}});
  pro.addEventListener('click',e=>{if(e.target.closest('[data-pro-tab]')){state.tabOpen=false;lab.hidden=true;btn.setAttribute('aria-expanded','false');}},true);
  el('tiIntLabClose')?.addEventListener('click',()=>{lab.hidden=true;state.tabOpen=false;btn.setAttribute('aria-expanded','false');});
  el('tiIntRunTest')?.addEventListener('click',test);
  el('tiIntStartFeed')?.addEventListener('click',startFeed);el('tiIntStopFeed')?.addEventListener('click',stopFeed);
  el('symbol')?.addEventListener('change',reset);el('saveGateway')?.addEventListener('click',()=>setTimeout(reset,300));
  return true;
 }
 function stopFeed(){state.feedOn=false;state.lastDepth=null;state.lastDepthAt=0;state.lastTrade=0;state.trades=[];update();}
 function startFeed(){stopFeed();state.feedOn=true;state.error='';update();}
 function reset(){const sym=getSymbol(),gw=getGateway();if(sym===state.symbol&&gw===state.gateway)return;stopFeed();state.symbol=sym;state.gateway=gw;state.series={};state.at=0;state.futures=null;state.cache=null;state.run++;state.error='';startFeed();void refresh(true);}
 function onGatewayMessage(event){
  const m=event?.detail,now=Date.now();if(!m)return;
  const symbol=String(m.symbol||m.data?.symbol||'').toUpperCase();
  if(symbol!==state.symbol)return;
  // A new server session cannot reuse prior book, tape or footprint observations.
  if(m.type==='snapshot'||m.type==='upstream_reset'||m.type==='upstream_down'||(m.type==='heartbeat'&&m.upstream_connected===false)){
   state.trades=[];state.lastTrade=0;state.lastDepth=null;state.lastDepthAt=0;return;
  }
  if(!state.feedOn)return;
  if(m.type==='trade'&&m.trade){const t=Number(m.trade.ts),p=Number(m.trade.price),q=Number(m.trade.qty);
   const side=m.trade.side==='buy'?'BUY':m.trade.side==='sell'?'SELL':null;
   if(side&&Number.isFinite(t)&&Math.abs(now-t)<60000&&Number.isFinite(p)&&Number.isFinite(q)&&p>0&&q>0){
    state.trades.push({t,p,q,aggressor:side});state.lastTrade=now;
    if(state.trades.length>2500)state.trades.splice(0,state.trades.length-2500);
    state.trades=state.trades.filter(x=>now-x.t<=300000);
   }
  }
  if(m.type==='depth'&&m.book_valid===true&&Array.isArray(m.bids)&&Array.isArray(m.asks)){
    state.lastDepth={bids:m.bids,asks:m.asks};state.lastDepthAt=now;
  }
  if(m.type==='depth_invalid'){
    state.lastDepth=null;state.lastDepthAt=0;
  }
 }
 function liveSpot(){try{const p=Number(market?.price),t=Number(lastTrade);
   return typeof ws!=='undefined'&&ws?.readyState===1&&Number.isFinite(p)&&p>0&&Number.isFinite(t)&&Date.now()-t<5000?{price:p,timestamp:t}:null;
  }catch(_){return null;}}
 async function requestFutures(){const controller=new AbortController(),timer=setTimeout(()=>controller.abort(),10000);
  try{const res=await fetch(state.gateway+'/api/v2/futures/'+encodeURIComponent(state.symbol),{cache:'no-store',signal:controller.signal});
   if(!res.ok)throw Error('Futures '+res.status);
   const data=await res.json(),b=data?.binance;
   if(data?.symbol!==state.symbol||!b||b.available!==true)return null;
   const price=Number(b.mark_price),timestamp=Number(b.data_time_ms);
   if(!Number.isFinite(price)||price<=0||!Number.isFinite(timestamp)||Date.now()-timestamp>120000)return null;
   return {symbol:state.symbol,price,timestamp,oi:b.open_interest_quantity==null?null:Number(b.open_interest_quantity),
    funding:b.funding_rate_pct==null?null:Number(b.funding_rate_pct)/100,source:'Binance USD-M perpetual'};
  }catch(_){return null;}finally{clearTimeout(timer);}
 }
 async function request(tf,context,seq){const controller=new AbortController(),timeout=setTimeout(()=>controller.abort(),12500);try{
  const res=await fetch(state.gateway+'/api/klines/'+encodeURIComponent(state.symbol)+'?interval='+tf+'&limit=300',{cache:'no-store',signal:controller.signal});if(!res.ok)throw Error('Gateway '+res.status);
  const data=await res.json();if(data.symbol!==state.symbol||data.interval!==tf||!Array.isArray(data.candles))throw Error('Kaynak parite/zaman uyumsuzluğu');return C.bars(data.candles,tf);
 }finally{clearTimeout(timeout);}}
 async function refresh(force=false){if(state.busy||!state.gateway)return;if(!force&&Date.now()-state.at<45000&&Object.keys(state.series).length===3){update();return;}
  const context=key(),seq=++state.run;state.busy=true;try{const result=await Promise.allSettled([...['1m','5m','15m'].map(tf=>request(tf,context,seq)),requestFutures()]);if(seq!==state.run||context!==key())return;
   state.series=Object.fromEntries(['1m','5m','15m'].map((tf,i)=>[tf,result[i].status==='fulfilled'?result[i].value:{ok:false,reason:String(result[i].reason?.message||'Bağlantı hatası'),data:[]}]));
   state.futures=result[3].status==='fulfilled'?result[3].value:null;state.at=Date.now();update();
  }finally{state.busy=false;}
 }
 function mainAuthority(){try{
   const a=typeof analysis!=='undefined'?analysis:null,at=typeof analysisAt==='number'?analysisAt:0;
   if(!a||a.symbol!==state.symbol||Date.now()-at>10000||!a.flow?.fresh||
      typeof ws==='undefined'||ws?.readyState!==1)return 'BEKLE';
   const direction=a.signal?.decision;
   if(direction!=='LONG'&&direction!=='SHORT')return 'BEKLE';
   // The original visible cockpit is an additional veto when active.
   if(el('chart')?.classList.contains('active')){
     const screen=(el('timiProDecision')?.textContent||'BEKLE').trim().toUpperCase();
     if(screen!==(direction==='LONG'?'AL':'SAT'))return 'BEKLE';
   }
   return direction;
  }catch(_){return 'BEKLE';}}
 function current(){const one=state.series['1m'],five=state.series['5m'],fifteen=state.series['15m'];if(!one?.ok||!five?.ok||!fifteen?.ok)return {ready:false,reason:[one,five,fifteen].filter(x=>!x?.ok).map(x=>x?.reason||'1m/5m/15m eksik').join(' · ')};
  const b=one.data,cacheKey=[state.symbol,one.data.at(-1).t,five.data.at(-1).t,fifteen.data.at(-1).t].join('|');
  if(!state.cache||state.cache.key!==cacheKey){state.cache={key:cacheKey,r:C.regime(b),s:C.smc(b),q:C.breakout(b),r5:C.regime(five.data),r15:C.regime(fifteen.data),vw:C.vwap(b,Math.max(0,b.length-90)),meta:C.trainMeta(b)};}
  const {r,s,q,r5,r15,vw,meta}=state.cache,flow=state.feedOn&&Date.now()-state.lastTrade<18000?C.analyzeFlow(state.trades):{ready:false,reason:'Anlık Gateway trade akışı yok'},dep=state.feedOn&&Date.now()-state.lastDepthAt<10000?C.depthProxy(state.lastDepth):{ready:false,reason:'Güncel depth snapshot yok'};
  const spot=liveSpot(),fu=C.futures(spot?.price??NaN,spot&&state.futures?{...state.futures,spotTimestamp:spot.timestamp}:null),mtf=(r5.ready&&r15.ready&&r5.direction===r15.direction)?r5.direction:'WAIT';
  const raw=C.decision(r,s,q,flow,fu),authority=mainAuthority();
  const decision=authority==='BEKLE'?{direction:'BEKLE',reason:'Ana TiMi motoru BEKLE veya verisi güncel değil',provisional:false}:
    raw.direction!==authority?{direction:'BEKLE',reason:'Intelligence ve ana motor yönleri uyumsuz / teyit eksik',provisional:false}:
    mtf!==raw.direction?{direction:'BEKLE',reason:'5m/15m rejim uyumsuzluğu',provisional:false}:raw;
  const vp=C.volumeProfile(state.trades);
  return {ready:true,b,r,s,q,flow,dep,fu,mtf,decision,vw,vp,meta};
 }
 function update(){const z=current();if(!z.ready){txt('tiIntDecision','BEKLE · '+(z.reason||'Veri bekleniyor'));txt('tiIntSources','Kapalı mumlar ve veri kaynağı doğrulanana kadar karar üretilemez.');
  for(const id of ['tiIntRegime','tiIntSMC','tiIntBreakout','tiIntVWAP','tiIntPOC','tiIntFlow','tiIntAbsorption','tiIntDepth','tiIntFutures','tiIntML'])txt(id,'Veri doğrulanamadı · '+(z.reason||'Hazır değil'));return;}
  const {b,r,s,q,flow,dep,fu,mtf,decision,vw,vp,meta}=z;
  txt('tiIntDecision',`${decision.direction} · ${decision.reason}${decision.provisional?' · TEYİT EKSİK':''}`);
  txt('tiIntSources',`${state.symbol} · 1m/5m/15m kapanmış mum: ${b.length}/${state.series['5m'].data.length}/${state.series['15m'].data.length} · Son analiz: ${new Date(state.at).toLocaleTimeString('tr-TR')}. Veri kalitesi: ${Object.values(state.series).map(x=>x.gaps||0).reduce((a,b)=>a+b,0)} gap; genel gecikme kontrolü: GEÇTİ.`);
  txt('tiIntRegime',`1m: ${r.kind} · ADX ${fmt(r.adx,1)} · EMA yönü ${r.direction} · ATR oranı ${fmt(r.atrRatio,2)}. 5m/15m uyumu: ${mtf}.`);
  txt('tiIntSMC',`Sweep → yapı kırılımı → FVG sıralaması: ${s.chain?'TEYİTLİ · '+s.direction:'TEYİT YOK'} · FVG: ${s.fvgActive?'aday aktif':'aktif değil'}. Sadece kapanmış mumlar.`);
  txt('tiIntBreakout',`Hacim teyitli 20-mum kırılımı: ${q.confirmed} · Başarısız kırılım adayı: ${q.failed} · Hacim ortalama oranı: ${fmt(q.volumeRatio,2)}x.`);
  txt('tiIntVWAP',`90 mum HLC3 yaklaşımlı Anchored VWAP: ${fmt(vw.value,5)} · Piyasa: ${b.at(-1).c>vw.value?'üstünde':'altında'} · Gerçek trade VWAP ile karıştırılmaz.`);
  txt('tiIntPOC',vp.ready?`Gözlemlenen işlem hacim profili POC: ${fmt(vp.poc,5)} · yalnız bu oturum.`:'Gerçek trade dağılımı yok; hacim profili POC doğrulanamadı.');
  txt('tiIntFlow',flow.ready?`Son 5 dk / en fazla 2.500 gözlemlenen trade (${flow.count}) · alış ${fmt(flow.buy,4)} / satış ${fmt(flow.sell,4)} · CVD ${fmt(flow.cvd,4)} baz varlık · Delta ${pct(flow.ratio)}.`:'CVD / agresif alım-satım: '+(state.error||flow.reason||'Canlı akışı başlatın'));
  txt('tiIntAbsorption',flow.ready?`Absorpsiyon adayı: ${flow.absorption} · fiyat değişimi ${pct(flow.priceChange)}. Teyitli iceberg/spoof göstergesi değildir.`:'Absorpsiyon hesaplanamadı; zaman uyumlu trade akışı gerekli.');
  txt('tiIntDepth',dep.ready?`20-kademe kısmi miktar dengesi: ${pct(dep.ratio)} · bid ${fmt(dep.bid)} / ask ${fmt(dep.ask)} · OFI: VERİ YETERSİZ.`:'OFI: VERİ YETERSİZ · '+(dep.reason||'Artımlı senkronize L2 gerekli'));
  txt('tiIntFutures',fu.ready?`Spot/Futures basis ${fmt(fu.basisPct,3)}% · ayrışma ${fu.direction} · OI ${fmt(fu.oi)} · funding ${fmt(fu.funding,6)} · kaynak ${fu.source}`:'Futures metrikleri geçerli borsa/sözleşme/zaman eşlemesi olmadan kapalı; '+fu.reason);
  txt('tiIntML',meta.ready?`Eğitim ${meta.trainCount} · test ${meta.testCount} · Brier ${fmt(meta.brier,4)} (temel ${fmt(meta.baseBrier,4)}) · ${meta.improved?'örneklemde daha iyi':'temel modeli geçemedi'} · ${meta.warning} Canlı güven olasılığı olarak kullanılmıyor.`:'Meta-model kapalı: '+meta.reason);
 }
 function test(){const box=el('tiIntTestResults'),z=current();if(!box)return;if(!z.ready){box.textContent='Test başlatılamadı: '+z.reason;return;}
 const names=['MTF Trend','SMC Zinciri','Breakout','Birleşik'],results=names.map(n=>C.backtest(z.b,n));const table=d.createElement('table');table.className='ti-int-table';table.innerHTML='<thead><tr><th>Model</th><th>İşlem</th><th>İsabet</th><th>Net*</th></tr></thead>';const tb=d.createElement('tbody');for(const r of results){const tr=d.createElement('tr');for(const val of [r.strategy,r.ready?r.count:'—',r.ready?pct(r.winRate):'—',r.ready?pct(r.net):'—']){const td=d.createElement('td');td.textContent=String(val);tr.append(td);}tb.append(tr);}table.append(tb);box.replaceChildren(table);
 const foot=d.createElement('p');foot.className='ti-int-note';foot.textContent='* Yaklaşık mum-tabanlı tarihsel test; 8 bps/yön maliyet varsayımı, 8 mum bariyeri. Aynı mumda hedef ve stop gerçekleşirse konservatif stop kabul edilir. Örnek sayısı, bağımsız walk-forward ve gerçek spread/slippage yeterli değilse bir avantaj iddiası yapılamaz.';box.append(foot);
 }
 // Optional documented adapter. Must be called by the host's own verified Futures data bridge.
 function ingestFutures(x){if(!x||x.symbol!==getSymbol()||!Number.isFinite(Number(x.timestamp))||typeof x.source!=='string'||!x.source.trim())return false;state.futures={...x};update();return true;}
 function ingestTrade(x){if(!x||getSymbol()!==x.symbol)return false;const t=Number(x.t),p=Number(x.p),q=Number(x.q);if(![t,p,q].every(Number.isFinite)||Math.abs(Date.now()-t)>60000||p<=0||q<=0||!['BUY','SELL'].includes(x.aggressor))return false;state.trades.push({t,p,q,aggressor:x.aggressor});state.lastTrade=Date.now();if(state.trades.length>2500)state.trades.splice(0,state.trades.length-2500);return true;}
 root.TiMiIntel=Object.freeze({version:'1.0.0',readOnly:true,ingestFutures,ingestTrade,inspect:()=>({symbol:state.symbol,closedBars:Object.fromEntries(Object.entries(state.series).map(([k,v])=>[k,v.data?.length||0])),tradeCount:state.trades.length,feedActive:state.feedOn})});
 function boot(){if(!install()){let tries=0;const retry=setInterval(()=>{if(install()){clearInterval(retry);start();}else if(++tries>=40){clearInterval(retry);}},500);return;}start();}
 function start(){if(root.__timiIntelStarted)return;root.__timiIntelStarted=true;state.symbol=getSymbol();state.gateway=getGateway();d.addEventListener('timi-gateway-event',onGatewayMessage);startFeed();void refresh(true);setInterval(()=>{if(d.visibilityState==='visible'){if(state.symbol!==getSymbol()||state.gateway!==getGateway())reset();else void refresh(false);if(state.feedOn)update();}},5000);
  d.addEventListener('visibilitychange',()=>{if(d.visibilityState==='hidden')stopFeed();else startFeed();});}
 if(d.readyState==='loading')d.addEventListener('DOMContentLoaded',boot,{once:true});else boot();
})(typeof window!=='undefined'?window:null);
