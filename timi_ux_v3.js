/* TiMi Experience v3.0 — read-only user experience enhancement.
 * Never generates recommendations, never fetches market data, never sends orders.
 * Existing TiMi decisions/feeds/ML/forward performance remain the only sources. */
(function(root){
 'use strict';
 const d=root&&root.document;
 if(!d||root.__timiUxV3Init)return;
 root.__timiUxV3Init=true;
 const $=id=>d.getElementById(id);
 const t=id=>($(id)?.textContent||'').trim();
 const items=[
   {id:'decision',label:'◉ Karar',match:['Canlı scalping kararı']},
   {id:'flow',label:'▥ Order Flow',match:['Canlı Order Flow']},
   {id:'performance',label:'▣ Performans',match:['İleriye Dönük Sinyal Performansı','Sinyal Performansı']},
   {id:'health',label:'◈ Veri Sağlığı',match:['Veri sağlığı']},
   {id:'intel',label:'✦ Intelligence',match:['Intelligence']}
 ];
 function findCard(spec){
   const overview=$('overview');if(!overview)return null;
   if(spec.id==='performance'){
     const el=$('timiForwardPerformance');if(el&&overview.contains(el))return el;
   }
   const candidates=[...overview.querySelectorAll('.card,section,article')].filter(x=>x!==overview);
   for(const c of candidates){
     const head=c.querySelector('h2,h3,h4');
     if(!head)continue;
     const txt=(head.textContent||'').toLocaleLowerCase('tr-TR');
     if(spec.match.some(m=>txt.includes(m.toLocaleLowerCase('tr-TR')))){
       // Pick the card, not a broad parent section, for a precise scroll destination.
       return head.closest('.card')||c;
     }
   }
   return null;
 }
 function setHint(value){const el=$('timiUxHint');if(el&&el.textContent!==value)el.textContent=value;}
 function openSection(id){
   const spec=items.find(x=>x.id===id);if(!spec)return;
   const active=$('overview')?.classList.contains('active');
   if(!active){
     const nav=d.querySelector('nav.foot button[data-page="overview"]');
     if(nav)nav.click();
   }
   // On a tab change the old layout may need one paint to become scrollable.
   root.requestAnimationFrame(()=>root.requestAnimationFrame(()=>{
     const target=findCard(spec);
     if(!target){setHint('Bu bölüm henüz hazır değil. Sayfa yüklendiğinde tekrar deneyin.');return;}
     const reduce=root.matchMedia?.('(prefers-reduced-motion: reduce)')?.matches;
     target.scrollIntoView({behavior:reduce?'auto':'smooth',block:'start'});
     d.querySelectorAll('#timiUxShortcuts button').forEach(b=>b.setAttribute('aria-pressed',String(b.dataset.uxTarget===id)));
     setHint('Bölüm: '+spec.label.replace(/^[^\s]+\s/, '')+'. Görülen veriler mevcut TiMi motorundan alınır.');
   }));
 }
 function button(spec){
   const b=d.createElement('button');b.type='button';b.textContent=spec.label;
   b.dataset.uxTarget=spec.id;b.setAttribute('aria-pressed','false');
   b.setAttribute('aria-label',spec.label.replace(/^[^\s]+\s/, '')+' bölümüne git');
   b.addEventListener('click',()=>openSection(spec.id));return b;
 }
 function make(tag,className,text){const el=d.createElement(tag);if(className)el.className=className;if(text)el.textContent=text;return el;}
 function renderState(){
   const hub=$('timiUxHub');if(!hub)return;
   const pair=$('symbol')?.value||'—';
   const signal=t('signal')||'BEKLE';
   const sync=t('analysisSync');
   const health=t('health');
   const gatewayLive=/canl[ıi]|bağl[ıi]/i.test(health)&&!/(bağlanıyor|kapalı|kopuk|hata)/i.test(health);
   const syncFresh=/güncel/i.test(sync)&&!/(değil|yetersiz|gecik)/i.test(sync);
   const state=!gatewayLive?'bad':syncFresh?'ok':'wait';
   const title=!gatewayLive?'Bağlantı bekleniyor':syncFresh?'Veri güncel':'Veri kontrol ediliyor';
   const status=$('timiUxLive');
   if(status){status.dataset.state=state;if(status.textContent!==title)status.textContent=title;}
   const pairEl=$('timiUxPair');if(pairEl)pairEl.textContent=pair;
   const decisionEl=$('timiUxDecision');if(decisionEl){
     const valid=syncFresh&&gatewayLive;
     const direction=valid&&/^(LONG|SHORT|BEKLE|AL|SAT)$/.test(signal)?signal:'BEKLE';
     decisionEl.textContent=direction;
     decisionEl.className=direction==='LONG'||direction==='AL'?'green':direction==='SHORT'||direction==='SAT'?'red':'gold';
   }
 }
 function navSync(){
   for(const b of d.querySelectorAll('nav.foot button[data-page]')){
     const selected=b.classList.contains('active');
     if(selected)b.setAttribute('aria-current','page');else b.removeAttribute('aria-current');
     if(!b.getAttribute('aria-label'))b.setAttribute('aria-label',(b.textContent||'').trim()+' sekmesi');
   }
 }
 function build(){
   const overview=$('overview');if(!overview||$('timiUxHub'))return;
   d.body.classList.add('timi-ux-v3');
   const hub=make('div');hub.id='timiUxHub';hub.setAttribute('aria-label','TiMi hızlı gezinme');
   const head=make('div','timi-ux-head');
   const left=make('div');left.append(make('span','timi-ux-label','HIZLI PİYASA KONTROLÜ'),make('h2','timi-ux-title','TiMi Kontrol Merkezi'));
   const live=make('span','timi-ux-live','Veri kontrol ediliyor');live.id='timiUxLive';live.dataset.state='wait';live.setAttribute('role','status');live.setAttribute('aria-live','polite');
   head.append(left,live);
   const ticker=make('div','timi-ux-ticker');
   ticker.append('Parite ',make('strong','','—'),'  ·  Ana karar ',make('strong','','BEKLE'),'  ·  Gerçek emir kapalı');
   ticker.querySelectorAll('strong')[0].id='timiUxPair';
   ticker.querySelectorAll('strong')[1].id='timiUxDecision';
   const row=make('div','timi-ux-shortcuts');row.id='timiUxShortcuts';row.setAttribute('role','group');row.setAttribute('aria-label','Özet ekranı bölüm kısayolları');
   for(const spec of items)row.append(button(spec));
   const hint=make('p','','İstediğin bölüme tek dokunuşla geç. Tüm veriler salt okunurdur.');hint.id='timiUxHint';
   hub.append(head,ticker,row,hint);
   const quick=$('timiUiQuickNav');
   if(quick&&quick.parentElement===overview)quick.insertAdjacentElement('afterend',hub);
   else overview.prepend(hub);
   const top=make('button','','↑');top.id='timiUxTop';top.type='button';top.setAttribute('aria-label','Sayfanın başına dön');top.title='Başa dön';
   top.addEventListener('click',()=>root.scrollTo({top:0,behavior:root.matchMedia?.('(prefers-reduced-motion: reduce)')?.matches?'auto':'smooth'}));
   d.body.append(top);
   let queued=false;
   const scrollCheck=()=>{if(queued)return;queued=true;root.requestAnimationFrame(()=>{
     queued=false;top.classList.toggle('is-shown',root.scrollY>400);
   });};
   root.addEventListener('scroll',scrollCheck,{passive:true});
   d.querySelector('nav.foot')?.addEventListener('click',()=>root.requestAnimationFrame(navSync));
   root.addEventListener('hashchange',navSync);
   $('symbol')?.addEventListener('change',renderState);
   d.addEventListener('visibilitychange',()=>{if(!d.hidden)renderState()});
   root.setInterval(()=>{if(!d.hidden)renderState()},2500);
   renderState();navSync();scrollCheck();
   root.timiUxV3Debug=Object.freeze({version:'3.0.0',readOnly:true,networkCalls:0,orderActions:0,hasHub:true});
 }
 if(d.readyState==='loading')d.addEventListener('DOMContentLoaded',build,{once:true});else build();
})(typeof window!=='undefined'?window:null);
