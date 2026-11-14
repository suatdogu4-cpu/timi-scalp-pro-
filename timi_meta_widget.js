/* TiMi MetaFilter v0.1, passive panel. Does not change the existing cockpit.
   The original browser cockpit remains the final directional veto. */
(()=>{
 'use strict';
 function boot(){
  if(document.getElementById('timiMetaPanel'))return;
  const cockpit=document.getElementById('timiProCockpit');
  if(!cockpit)return;
  const panel=document.createElement('section');
  panel.id='timiMetaPanel';
  panel.style.cssText='margin:12px 0;padding:13px;border:1px solid #30445e;border-radius:13px;background:#101d2d;color:#e5edf9;font:13px system-ui';
  panel.innerHTML='<div style="display:flex;align-items:center;justify-content:space-between;gap:8px"><strong>TiMi ML MetaFilter · 26 İndikatör</strong><span id="timiMetaBadge" style="color:#f7c65c">KONTROL EDİLİYOR</span></div>'+
    '<div id="timiMetaDecision" style="font-size:23px;font-weight:900;margin:7px 0;color:#f7c65c">BEKLE</div>'+
    '<div id="timiMetaExplanation" style="color:#a6b8cf;font-size:12px;line-height:1.45">Canlı TiMi sinyali ile model onayı denetleniyor.</div>'+
    '<div style="font-size:10px;color:#8ca2bc;margin-top:8px">Yalnızca mevcut TiMi sinyali onaylanabilir; model yeni yön üretmez. Gerçek emir gönderilmez.</div>';
  cockpit.insertAdjacentElement('afterend',panel);
  const get=id=>document.getElementById(id);
  let running=false;
  let symbolAtLast='';
  async function refresh(){
   if(running||!document.getElementById('timiMetaPanel'))return;
   const current=typeof symbol==='string'?symbol:'BTCUSDT';
   const gateway=(localStorage.getItem('timi_scalp_gateway')||location.origin).replace(/\/$/,'');
   if(!/^https?:\/\//.test(gateway))return;
   running=true;
   const abort=new AbortController();
   const timeout=setTimeout(()=>abort.abort(),10000);
   try{
    const res=await fetch(gateway+'/api/ml-meta/'+encodeURIComponent(current),{
      cache:'no-store',signal:abort.signal});
    if(!res.ok)throw new Error('ML endpoint '+res.status);
    const data=await res.json();
    if(current!==(typeof symbol==='string'?symbol:'BTCUSDT'))return;
    const cockpitSide=(get('timiProDecision')?.textContent||'BEKLE').trim().toUpperCase();
    const sourceSide=data.source_decision==='LONG'?'AL':data.source_decision==='SHORT'?'SAT':'BEKLE';
    const mlSide=data.ml_decision==='LONG'?'AL':data.ml_decision==='SHORT'?'SAT':'BEKLE';
    // Browser's existing final decision is an additional compulsory veto.
    const accepted=(mlSide!=='BEKLE'&&cockpitSide===mlSide&&sourceSide===mlSide&&
       data.ml_live_approval_enabled===true&&data.ml_model_loaded===true&&
       data.ml_probability_is_calibrated===true);
    const result=accepted?mlSide:'BEKLE';
    get('timiMetaDecision').textContent=result;
    get('timiMetaDecision').style.color=result==='AL'?'#37d5a1':result==='SAT'?'#ff708e':'#f7c65c';
    const ready=data.ml_model_loaded&&data.ml_live_approval_enabled;
    get('timiMetaBadge').textContent=ready?'DOĞRULANMIŞ MODEL':'ML ONAYI YOK';
    const probability=(ready&&data.ml_probability_is_calibrated&&typeof data.ml_probability==='number')?
      ' · Kalibre edilmiş model tahmini: %'+(100*data.ml_probability).toFixed(1):'';
    get('timiMetaExplanation').textContent='Kaynak TiMi: '+sourceSide+' · Ekran kokpiti: '+cockpitSide+
      ' · '+String(data.reason||'Doğrulama yok')+probability;
   }catch(err){
    get('timiMetaBadge').textContent='VERİ YOK';
    get('timiMetaDecision').textContent='BEKLE';
    get('timiMetaExplanation').textContent='ML bağlantısı veya yanıtı doğrulanamadı. Eski karar motoru değiştirilmedi.';
   }finally{
    clearTimeout(timeout);running=false;
   }
  }
  refresh();setInterval(refresh,15000);
 }
 if(document.readyState==='loading')document.addEventListener('DOMContentLoaded',boot,{once:true});else boot();
})();
