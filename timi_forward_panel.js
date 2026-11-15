/* TiMi v1.8.6 — Forward-only, cost-adjusted observation panel.
 * Additive and read-only. No trading buttons, orders or ML training.
 */
(()=>{
 'use strict';
 if(window.__timiForwardPanelReady)return;
 window.__timiForwardPanelReady=true;
 const $=id=>document.getElementById(id);
 const state={tf:'1m',busy:false,seq:0,lastKey:'',timer:null};
 const fmt=(v,n=2)=>typeof v==='number'&&Number.isFinite(v)?v.toLocaleString('tr-TR',{minimumFractionDigits:n,maximumFractionDigits:n}):'—';
 const dt=ms=>Number.isFinite(Number(ms))?new Date(Number(ms)).toLocaleTimeString('tr-TR',{hour:'2-digit',minute:'2-digit',second:'2-digit'}):'—';
 const root=()=>{try{return(typeof gateway==='string'?gateway:($('gateway')?.value||location.origin)).replace(/\/$/,'');}catch(_){return location.origin;}};
 const symbol=()=>($('symbol')?.value||'BTCUSDT').toUpperCase();
 const visible=()=>document.visibilityState==='visible'&&$('overview')?.classList.contains('active');
 const note=(message)=>{$('timiFpStatus').textContent=message;};
 function label(id,text,cls){const node=$(id);node.textContent=text;if(cls)node.className=cls;}
 function build(){
  const page=$('overview');if(!page)return;
  const card=document.createElement('div');card.className='card timi-fp';card.id='timiForwardPerformance';
  card.innerHTML=`<div class="row timi-fp-head"><h3>📊 İleriye Dönük Sinyal Performansı</h3><span class="badge">Canlı gözlem · emir yok</span></div>
   <div class="timi-fp-switch"><button type="button" data-fp-tf="1m" class="active">1 dakika</button><button type="button" data-fp-tf="5m">5 dakika</button><button type="button" id="timiFpRefresh">⟳ Güncelle</button></div>
   <div id="timiFpStatus" class="note" aria-live="polite">Canlı kayıt durumu sorgulanıyor…</div>
   <div class="timi-fp-grid">
    <div class="metric"><small>Ölçülmüş sinyal</small><strong id="timiFpMeasured">—</strong></div>
    <div class="metric"><small>Net doğru / yanlış</small><strong id="timiFpCorrectWrong">—</strong></div>
    <div class="metric"><small>Maliyet sonrası pozitif</small><strong id="timiFpPositive">—</strong></div>
    <div class="metric"><small>Ortalama net değişim</small><strong id="timiFpMean">—</strong></div>
    <div class="metric"><small>Medyan net değişim</small><strong id="timiFpMedian">—</strong></div>
   </div>
   <div class="timi-fp-states"><span id="timiFpLong">LONG: —</span><span id="timiFpShort">SHORT: —</span><span id="timiFpPending">Bekleyen: —</span><span id="timiFpMissing">Eksik: —</span><span id="timiFpStorage">Kayıt: —</span></div>
   <div id="timiFpWarning" class="note timi-fp-warning">En az 100 ölçüm olmadan sonuçlar ön değerlendirmedir.</div>
   <details class="timi-fp-details"><summary>Son 20 sinyalin ayrıntıları</summary><div id="timiFpRows" class="timi-fp-rows">Henüz kayıt yok.</div></details>
   <div class="note timi-fp-note">Yalnız ana TiMi motorunun canlı LONG/SHORT sinyalleri. Sinyalden <b>sonraki</b> dakika açılışı ve ileride gözlenen alış/satış kotasyonları esas alınır. Alış-satış farkı, çift yönlü komisyon ve kayma hesaba katılır. Eksik veri sonucu başarıya dahil edilmez. Bu, gerçekleşmiş işlem kârı veya başarı garantisi değildir.</div>`;
  page.firstElementChild?.insertAdjacentElement('afterend',card);
  if(!card.isConnected)page.prepend(card);
  card.querySelectorAll('button[data-fp-tf]').forEach(btn=>btn.addEventListener('click',()=>{
   state.tf=btn.dataset.fpTf;
   card.querySelectorAll('button[data-fp-tf]').forEach(b=>b.classList.toggle('active',b===btn));
   void refresh(true);
  }));
  $('timiFpRefresh').addEventListener('click',()=>void refresh(true));
  $('symbol')?.addEventListener('change',()=>void refresh(true));
  $('saveGateway')?.addEventListener('click',()=>setTimeout(()=>void refresh(true),100));
 }
 function showRows(rows){
  const container=$('timiFpRows');if(!container)return;
  container.replaceChildren();
  if(!rows.length){container.textContent='Henüz LONG/SHORT sinyali kaydedilmedi.';return;}
  rows.forEach(row=>{
   const outcome=row.outcomes?.[state.tf];
   if(!outcome)return;
   const status=outcome.status==='measured'?'Ölçüldü':outcome.status==='missing_data'?'Eksik veri':'Beklemede';
   const line=document.createElement('div');line.className='timi-fp-record';
   const head=document.createElement('strong');head.textContent=`${dt(row.observed_at_ms)} · ${row.direction||'?'} · Skor ${row.score??'—'}`;
   const value=document.createElement('span');
   value.textContent=outcome.status==='measured'?`${fmt(outcome.net_pct,4)}% net · ${Number(outcome.net_pct)>0?'Pozitif':'Pozitif değil'}`:status;
   value.className=outcome.status==='measured'?(Number(outcome.net_pct)>0?'green':'red'):'gold';
   line.append(head,value);container.append(line);
  });
 }
 async function refresh(force=false){
  if(!visible()&&!force)return;
  if(state.busy)return;
  const k=root()+'|'+symbol()+'|'+state.tf;
  if(!force&&k===state.lastKey&&Date.now()-Number($('timiForwardPerformance')?.dataset.last||0)<15000)return;
  const seq=++state.seq;state.busy=true;
  note('Canlı performans verileri okunuyor…');
  try{
   const url=root()+'/api/forward-performance/'+encodeURIComponent(symbol())+'?limit=20';
   const controller=new AbortController();const timeout=setTimeout(()=>controller.abort(),11000);
   let response;
   try{response=await fetch(url,{cache:'no-store',signal:controller.signal});}
   finally{clearTimeout(timeout);}
   if(!response.ok)throw Error('HTTP '+response.status);
   const data=await response.json();
   if(seq!==state.seq||data.symbol!==symbol())return;
   state.lastKey=k;
   $('timiForwardPerformance').dataset.last=Date.now();
   const m=data.horizons?.[state.tf];if(!m)throw Error('Performans alanı yok');
   label('timiFpMeasured',String(m.measured));
   label('timiFpCorrectWrong',String(m.net_positive)+' / '+String(m.net_not_positive));
   for(const [dir,id] of [['LONG','timiFpLong'],['SHORT','timiFpShort']]){
    const part=m.by_direction?.[dir];
    label(id,dir+': '+(part?part.net_positive+'/'+part.measured:'—')+' net pozitif');
   }
   label('timiFpPositive',m.net_positive_pct==null?'—':fmt(m.net_positive_pct)+'%',m.net_positive_pct>=50?'green':'gold');
   label('timiFpMean',m.mean_net_pct==null?'—':fmt(m.mean_net_pct,4)+'%',m.mean_net_pct>0?'green':'red');
   label('timiFpMedian',m.median_net_pct==null?'—':fmt(m.median_net_pct,4)+'%',m.median_net_pct>0?'green':'red');
   label('timiFpPending','Bekleyen: '+m.pending);
   label('timiFpMissing','Eksik veri: '+m.missing);
   const durable=data.storage==='postgresql';
   label('timiFpStorage','Kayıt: '+(durable?'PostgreSQL':'Geçici / Bellek'));
   if(data.enabled===false){note('Bu paritede sinyal gözlemi kapalı. Varsayılan BTCUSDT; istenirse Render TIMI_SIGNAL_AUDIT_SYMBOLS ile açılır.');}
   else if(m.measured===0){note(symbol()+' · Henüz tamamlanan '+state.tf+' LONG/SHORT sinyali yok. BEKLE sonuç sayılmaz.');}
   else{note(symbol()+' · '+state.tf+' · Gerçekleşmiş piyasa gözlemleri · maliyet sonrası varsayımsal sonuç');}
   const warning=[];
   if(m.insufficient_sample)warning.push('Örnek az: '+m.measured+'/100. Geçerli başarı kanıtı değildir.');
   if(data.storage!=='postgresql')warning.push('Sunucu yeniden başlarsa geçmiş kaybolabilir. Kalıcı saklama için Render PostgreSQL gerekir.');
   if(data.storage_error)warning.push('Veritabanı: '+data.storage_error);
   if(data.failed_writes)warning.push('Başarısız kayıt yazımı: '+data.failed_writes);
   warning.push('Maliyet: '+fmt(data.roundtrip_fee_slippage_pct,2)+'% (gidiş-dönüş komisyon + kayma); alış/satış kotasyon farkı ayrıca fiyata dahildir. SHORT gözlemi spot açığa satış işlemi değildir.');
   label('timiFpWarning',warning.join(' '));
   showRows(data.observations||[]);
  }catch(e){if(seq===state.seq){
   for(const id of ['timiFpMeasured','timiFpCorrectWrong','timiFpPositive','timiFpMean','timiFpMedian'])label(id,'—');
   label('timiFpPending','Bekleyen: —');label('timiFpMissing','Eksik: —');
   label('timiFpStorage','Kayıt durumu doğrulanamadı');
   note('Performans verisi alınamadı: '+String(e.message||e));
  }}
  finally{state.busy=false;}
 }
 function boot(){build();void refresh(true);state.timer=setInterval(()=>void refresh(),15000);document.addEventListener('visibilitychange',()=>{if(visible())void refresh(true);});}
 if(document.readyState==='loading')document.addEventListener('DOMContentLoaded',boot,{once:true});else boot();
})();
