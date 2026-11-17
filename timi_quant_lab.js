/* TiMi QUANT LAB v1.0 — additive read-only UI, no new WebSocket. */
(function(){
'use strict';
if (window.__timiQuantLabBooted) return;
window.__timiQuantLabBooted = true;
const ID = id => document.getElementById(id);
const fmt = (v,d=2) => v === null || v === undefined || !Number.isFinite(Number(v))
  ? '—' : Number(v).toLocaleString('tr-TR',{minimumFractionDigits:d,maximumFractionDigits:d});
const fields = [
  ['ema20','EMA 20','fiyat'],['ema50','EMA 50','fiyat'],['rsi14','RSI 14','puan'],
  ['return_1bar_pct','Son mum getirisi','%'],['return_5bar_pct','5 mum getirisi','%'],
  ['realized_vol_20bar_pct','20 mum oynaklığı','% / mum'],
  ['return_autocorr_20bar','Getiri otokorelasyonu','katsayı'],
  ['price_zscore_20bar','Fiyat z-skoru','σ'],['atr14_pct','ATR 14 / fiyat','%'],
  ['volume_ratio_20bar','Hacim / 20 mum ort.','kat'],
  ['book_imbalance','Defter dengesi','−1 / +1'],
  ['aggressor_imbalance_60s','60 sn agresif akış','−1 / +1'],
  ['spread_bps','Spread','bps'],['cvd_60s_usdt','60 sn CVD','USDT']
];
const styles = `
#quantLab{--qbg:#132338;--qline:#35516b;--qmute:#aec0d4;--qgreen:#42deb0;--qred:#ff738f;--qgold:#f3c96c;max-width:1100px;margin:auto}
#quantLab *{box-sizing:border-box}
#quantLab .q-head{display:flex;align-items:flex-start;justify-content:space-between;gap:9px;flex-wrap:wrap}
#quantLab .q-head h2{font-size:21px;margin:0 0 4px}
#quantLab .q-head p,#quantLab .q-note{font-size:12px;color:var(--qmute);line-height:1.6;margin:5px 0 12px}
#quantLab .q-toolbar{display:flex;gap:8px;align-items:center;flex-wrap:wrap;margin:13px 0}
#quantLab .q-toolbar select,#quantLab .q-toolbar button{border-radius:10px;padding:10px 12px;border:1px solid var(--qline);background:#1a344e;color:#eff6ff}
#quantLab .q-toolbar label{font-size:12px;color:var(--qmute)}
#quantLab .q-card{padding:15px;margin:0 0 12px;border-radius:15px;background:linear-gradient(135deg,#182d45,#101e30);border:1px solid var(--qline)}
#quantLab .q-grid{display:grid;grid-template-columns:repeat(2,minmax(0,1fr));gap:9px}
#quantLab .q-stat{padding:11px;border:1px solid #2e4761;border-radius:11px;background:#102037;min-width:0}
#quantLab .q-stat small{color:var(--qmute);font-size:11px;display:block;line-height:1.3}
#quantLab .q-stat strong{font-size:17px;display:block;margin-top:6px;overflow-wrap:anywhere}
#quantLab .q-stat em{font-style:normal;font-size:10px;color:var(--qmute);font-weight:400}
#quantLab .q-result{font-size:clamp(25px,7vw,36px);font-weight:900;letter-spacing:-.6px}
#quantLab .q-result.long{color:var(--qgreen)}#quantLab .q-result.short{color:var(--qred)}#quantLab .q-result.wait{color:var(--qgold)}
#quantLab .q-pill{display:inline-flex;border:1px solid #586984;border-radius:18px;padding:6px 10px;font-size:11px;color:#e2eafa;background:#273953}
#quantLab .q-pill.ok{color:var(--qgreen);border-color:#299877;background:#103d34}#quantLab .q-pill.bad{color:var(--qred);border-color:#af4960;background:#472a3c}
#quantLab .q-reasons{padding-left:20px;font-size:12px;line-height:1.6;color:#c9d9e9}
#quantLab .q-muted{color:var(--qmute)}
#quantLab .q-error{color:#ff9baf;font-size:12px;padding:10px 0}
#quantLab [hidden]{display:none!important}
@media(min-width:700px){#quantLab .q-grid{grid-template-columns:repeat(4,minmax(0,1fr))}}
@media(max-width:400px){#quantLab .q-grid{grid-template-columns:repeat(2,minmax(0,1fr))}#quantLab .q-card{padding:11px}}
`;
let busy=false, serial=0, lastGood=0;
function boot(){
  const main=document.querySelector('main');
  const nav=document.querySelector('nav.foot');
  if (!main || !nav || ID('quantLab'))return;
  const style=document.createElement('style');style.id='quantLabCSS';style.textContent=styles;document.head.append(style);
  const section=document.createElement('section');section.id='quantLab';section.className='section';
  section.innerHTML=`
  <div class="q-card">
    <div class="q-head"><div><h2>◈ TiMi QUANT LAB</h2><p>İstatistik · Mikro yapı · Rejim · Tarihsel tanı</p></div><span class="q-pill" id="qHealth">VERİ BEKLENİYOR</span></div>
    <div class="q-toolbar"><label for="qInterval">Zaman dilimi</label>
    <select id="qInterval"><option value="1m">1 dakika</option><option value="3m">3 dakika</option><option value="5m">5 dakika</option><option value="15m">15 dakika</option></select>
    <button id="qRefresh" type="button">↻ Yenile</button><span id="qContext" class="q-note">—</span></div>
    <div class="q-result wait" id="qDecision">BEKLE</div>
    <p class="q-note" id="qDecisionNote">Gerçek veriye dayalı aday yön; kalibre edilmiş olasılık değildir.</p>
    <ul class="q-reasons" id="qReasons"><li>Veriler kontrol ediliyor.</li></ul>
    <div class="q-grid" id="qConditions"></div>
  </div>
  <div class="q-card"><h3>İstatistik ve mikro yapı göstergeleri</h3><div class="q-grid" id="qMetrics"></div>
  <p class="q-note">Hacim, defter dengesi ve CVD farklı veri türleridir. 20 seviyeli defter anlık bir görüntüdür; CVD son 60 saniyede gözlenen işlemlerdendir.</p></div>
  <div class="q-card"><h3>Geçmiş kural tanısı · maliyet varsayımı</h3><p class="q-note" id="qDiagMethod">Kapanmış mumlarda sabit kurallı, bir sonraki mumun açılış–kapanış yön gözlemi.</p>
  <div class="q-grid" id="qDiag"></div><p class="q-note" id="qDiagWarn">Sonuçlar model tahmini veya gerçek işlem performansı değildir.</p></div>
  <div class="q-card"><h3>Model olasılıkları</h3><strong>Henüz hesaplanmıyor</strong>
   <p class="q-note">Eğitim, ayrı zaman dilimi doğrulaması, walk-forward testi ve olasılık kalibrasyonu olmadan LONG/SHORT yüzde ihtimali gösterilmez. Gerçek emir özelliği kapalıdır.</p></div>
  <div id="qError" class="q-error" role="status" aria-live="polite"></div>`;
  main.append(section);
  const b=document.createElement('button');b.type='button';b.dataset.page='quantLab';b.innerHTML='◈<div>Quant</div>';
  b.title='Kantitatif analiz laboratuvarı';
  nav.append(b);
  b.addEventListener('click',()=>{
    if(typeof setPage==='function')setPage('quantLab');
    else{
      document.querySelectorAll('.section').forEach(x=>x.classList.toggle('active',x.id==='quantLab'));
      nav.querySelectorAll('button').forEach(x=>x.classList.toggle('active',x===b));
    }
    void refresh(true);
  });
  ID('qInterval').addEventListener('change',()=>{serial++;void refresh(true)});
  ID('qRefresh').addEventListener('click',()=>void refresh(true));
  ID('symbol')?.addEventListener('change',()=>{serial++;if(isVisible())void refresh(true)});
  document.addEventListener('visibilitychange',()=>{if(!document.hidden&&isVisible())void refresh(true)});
  window.addEventListener('timi-closed-kline',()=>{if(isVisible())void refresh(false)});
  window.setInterval(()=>{if(isVisible()&&!document.hidden)void refresh(false)},8000);
}
function isVisible(){return ID('quantLab')?.classList.contains('active')===true}
function base(){
  try{
    const raw=typeof gateway!=='undefined'&&gateway?String(gateway):location.origin;
    const url=new URL(raw,location.origin);
    if(url.protocol==='https:'||(url.protocol==='http:'&&['localhost','127.0.0.1'].includes(url.hostname)))return url.origin;
  }catch(_){/* ignored */}
  return location.origin;
}
function metricBox(label,value,suffix){
  const box=document.createElement('div');box.className='q-stat';
  const small=document.createElement('small');small.textContent=label;
  const strong=document.createElement('strong');strong.textContent=value;
  if(suffix){const em=document.createElement('em');em.textContent=' '+suffix;strong.append(em)}
  box.append(small,strong);return box;
}
function fill(id,rows){const parent=ID(id);if(!parent)return;parent.replaceChildren(...rows.map(r=>metricBox(...r)))}
function show(data){
  const good=!!data.data_health?.ready;
  const health=ID('qHealth');health.className='q-pill '+(good?'ok':'bad');health.textContent=good?'CANLI · DOĞRULANDI':'YETERSİZ / GECİKMELİ';
  ID('qContext').textContent=data.symbol+' · '+data.interval+' · '+data.bar_count+' kapanmış mum · '+new Date(data.evaluated_at_ms).toLocaleTimeString('tr-TR');
  const decision=data.signal?.decision||'BEKLE';
  ID('qDecision').textContent=decision==='LONG_ADAY'?'LONG ADAYI':decision==='SHORT_ADAY'?'SHORT ADAYI':'BEKLE';
  ID('qDecision').className='q-result '+(decision==='LONG_ADAY'?'long':decision==='SHORT_ADAY'?'short':'wait');
  ID('qDecisionNote').textContent='Rejim: '+(data.regime?.trend||'—')+' · Oynaklık: '+(data.regime?.volatility||'—')+' · Kapanış yaşı '+fmt(data.last_closed_age_ms/1000,0)+' sn · Eğitimli olasılık yok';
  const reasons=ID('qReasons');reasons.replaceChildren(...(data.signal?.reasons||[]).map(s=>{const li=document.createElement('li');li.textContent=s;return li}));
  const h=data.data_health||{};
  fill('qConditions',[
    ['Akış + emir defteri',h.fresh_live_orderflow?'GÜNCEL':'YETERSİZ',''],
    ['Kapalı mum',h.fresh_candle?'GÜNCEL':'ESKİ',''],
    ['Spread sınırı',h.spread_ok?'UYGUN':'RED',''],
    ['İşlem / 60 sn',fmt(h.trade_count_60s,0),'adet'],
  ]);
  fill('qMetrics',fields.map(([key,name,unit])=>{
    const digits=key.startsWith('ema')||key==='cvd_60s_usdt'?2:key==='return_autocorr_20bar'?4:3;
    return [name,fmt(data.metrics?.[key],digits),unit];
  }));
  const d=data.diagnostic||{};
  fill('qDiag',[
    ['Gözlem sayısı',fmt(d.sample_count,0),'adet'],
    ['Net pozitif oranı',fmt(d.win_rate_pct,1),'%'],
    ['Ortalama net sonuç',fmt(d.mean_net_bps,2),'bps'],
    ['Kâr/zarar faktörü',fmt(d.profit_factor,2),'kat'],
    ['Varsayılan toplam maliyet',fmt(d.roundtrip_assumed_bps,1),'bps'],
    ['İncelenen son dönem',fmt(d.test_period_bars,0),'mum'],
  ]);
  ID('qDiagMethod').textContent=d.method||'—';
  ID('qDiagWarn').textContent=(d.sample_sufficient?'Tarihsel tanı, gerçek emir performansı değildir.':'En az 30 olay yok: performans oranları gizlendi. ')+' '+(d.caution||'');
  ID('qError').textContent='';lastGood=Date.now();
}
async function refresh(force){
  if(!isVisible()||busy)return;
  if(!force&&Date.now()-lastGood<6000)return;
  const s=ID('symbol')?.value||'BTCUSDT',interval=ID('qInterval')?.value||'1m';
  const my=++serial,origin=base();busy=true;
  const controller=new AbortController(),timeout=window.setTimeout(()=>controller.abort(),15000);
  try{
    const response=await fetch(origin+'/api/quant/'+encodeURIComponent(s)+'?interval='+encodeURIComponent(interval),{cache:'no-store',signal:controller.signal});
    const data=await response.json();
    if(!response.ok)throw Error(data.detail||('HTTP '+response.status));
    if(my!==serial||s!==(ID('symbol')?.value||'BTCUSDT')||interval!==ID('qInterval')?.value||origin!==base())return;
    show(data);
  }catch(err){
    if(my===serial){ID('qDecision').textContent='BEKLE';ID('qDecision').className='q-result wait';
      ID('qHealth').textContent='VERİ DOĞRULANAMADI';ID('qHealth').className='q-pill bad';
      ID('qError').textContent='Quant veri sorgusu başarısız: '+String(err.message||err);
      fill('qMetrics',[]);fill('qDiag',[]);fill('qConditions',[]);
    }
  }finally{window.clearTimeout(timeout);busy=false}
}
if(document.readyState==='loading')document.addEventListener('DOMContentLoaded',boot,{once:true});else boot();
})();
