/* TiMi Order Flow PRO 3.0 — read-only, current-session price-volume analysis.
 * No orders, credentials, signal archiving, past-session storage or inference of
 * genuine limit-order identities. Existing TiMi decisions remain unchanged.
 */
(function(root){
'use strict';
if(!root || !root.document || root.__timiV23Active)return;
root.__timiV23Active=true;
const d=root.document,$=id=>d.getElementById(id);
const fmt=(n,k=2)=>typeof n==='number'&&Number.isFinite(n)?n.toLocaleString('tr-TR',{maximumFractionDigits:k}):'—';
const pct=(n,k=2)=>typeof n==='number'&&Number.isFinite(n)?fmt(n,k)+'%':'—';
const total=(n)=>typeof n==='number'&&Number.isFinite(n)?fmt(n,0):'—';
let selected=60,abort=null,serial=0,lastSymbol='',lastGateway='',lastUpdate=0,pending=false;
function symbol(){return String($('symbol')?.value||'BTCUSDT').toUpperCase();}
function gateway(){return String($('gateway')?.value||root.location.origin).trim().replace(/\/$/,'');}
function active(){return $('proLab')?.classList.contains('active')&&$('proFlow')&&!$('proFlow').hidden&&$('timiV23Orderflow')?.open;}
function row(label,value,tone=''){
 const r=d.createElement('div');r.className='v23-stat';
 const l=d.createElement('span');l.textContent=label;
 const v=d.createElement('b');v.textContent=value;if(tone)v.className=tone;
 r.append(l,v);return r;
}
const css=`
#timiV23Orderflow{border:1px solid #36705f;border-radius:13px;background:linear-gradient(145deg,#152b41,#0d1f30);margin:0 0 12px;color:#e8f4ff;min-width:0;font-family:system-ui}
#timiV23Orderflow>summary{display:flex;justify-content:space-between;align-items:center;gap:9px;padding:13px 11px;cursor:pointer;min-height:45px;list-style:none}
#timiV23Orderflow>summary::-webkit-details-marker{display:none}
#timiV23Orderflow>summary strong{font-size:13px;color:#b9f9e5}
#timiV23Orderflow>summary small{display:block;color:#9ab5c7;font-size:10px;font-weight:400;margin-top:4px}
#timiV23Orderflow .v23-badge{border:1px solid #3d706c;border-radius:99px;padding:4px 7px;color:#a3d7cc;font-size:10px;white-space:nowrap}
#timiV23Orderflow .v23-body{border-top:1px solid #314c61;padding:10px 11px 14px;display:grid;gap:11px}
#timiV23Orderflow .v23-toolbar{display:flex;gap:6px;align-items:center;flex-wrap:wrap}
#timiV23Orderflow button{min-height:36px;padding:6px 11px;font-size:12px;background:#1f3950;border:1px solid #41627b;border-radius:8px;color:#e1edfb;cursor:pointer}
#timiV23Orderflow button[aria-pressed=true]{background:#125343;color:#88efc9;border-color:#20aa84}
#timiV23Orderflow .v23-note{font-size:11px;color:#b9cee2;line-height:1.55;border-left:3px solid #46647c;padding:5px 8px;background:#1a3147}
#timiV23Orderflow .v23-note.bad{border-color:#e0a64f;color:#ffe0a6}
#timiV23Orderflow h4{font-size:12px;color:#def0ff;margin:5px 0 3px}
#timiV23Orderflow .v23-stats{display:grid;grid-template-columns:repeat(2,minmax(0,1fr));gap:6px}
#timiV23Orderflow .v23-stat{padding:9px 8px;border:1px solid #344d65;border-radius:8px;background:#1b334b;min-width:0;display:flex;flex-direction:column;gap:5px}
#timiV23Orderflow .v23-stat span{font-size:10px;color:#b4cbe0}
#timiV23Orderflow .v23-stat b{font-size:14px;overflow-wrap:anywhere;font-variant-numeric:tabular-nums}
#timiV23Orderflow .v23-green{color:#41e3a9}#timiV23Orderflow .v23-red{color:#ff7896}
#timiV23Orderflow .v23-table{display:grid;gap:5px}
#timiV23Orderflow .v23-level{display:grid;grid-template-columns:minmax(0,1.2fr) minmax(0,1fr) minmax(0,1fr);gap:5px;border-bottom:1px solid #2d485d;padding:7px 0;font-size:10px;align-items:center;font-variant-numeric:tabular-nums}
#timiV23Orderflow .v23-level>span:nth-child(n+2){text-align:right;white-space:nowrap}
#timiV23Orderflow .v23-level .v23-level-price{color:#dbeaff;word-break:break-word}
#timiV23Orderflow .v23-meter{height:4px;background:#4b3045;border-radius:6px;overflow:hidden;margin-top:4px}
#timiV23Orderflow .v23-meter i{display:block;height:100%;background:#29d7a3}
#timiV23Orderflow details{border-top:1px solid #304b62;padding-top:9px;font-size:11px;color:#a9c4d9;line-height:1.65}
#timiV23Orderflow details summary{cursor:pointer;color:#9fe8d5}
#timiV23Orderflow button:focus-visible,#timiV23Orderflow summary:focus-visible{outline:2px solid #84f3d5;outline-offset:2px}
@media(max-width:380px){#timiV23Orderflow .v23-level{font-size:9px;gap:3px}#timiV23Orderflow .v23-body{padding:9px}}
`;
function build(){
 if($('timiV23Orderflow'))return true;
 const parent=$('proFlow');if(!parent)return false;
 const box=d.createElement('details');box.id='timiV23Orderflow';
 box.innerHTML=`<summary><span><strong>Order Flow PRO 3.0 · Fiyat × Delta</strong><small>Gerçek işlemler ve kısmi defter · Son 5 dakika</small></span><span class="v23-badge" id="v23Badge">Aç / incele</span></summary>
 <div class="v23-body">
  <div class="v23-toolbar" id="v23Toolbar"><button type="button" data-v23-window="15" aria-pressed="false">15 sn</button><button type="button" data-v23-window="60" aria-pressed="true">60 sn</button><button type="button" data-v23-window="300" aria-pressed="false">300 sn</button><button type="button" id="v23Refresh">Yenile</button></div>
  <div id="v23Status" class="v23-note">Canlı veri henüz sorgulanmadı.</div>
  <div id="v23Stats" class="v23-stats"></div>
  <div><h4>15 / 60 / 300 sn · Delta karşılaştırması</h4><div id="v23Compare" class="v23-table"></div></div>
  <div><h4>Fiyat gruplarına göre agresif işlem hacmi (USDT)</h4><div id="v23Levels" class="v23-table"></div><div class="v23-note" id="v23Coverage">Yalnızca bu sunucunun gözlemlediği işlemler.</div></div>
  <div><h4>Emir defteri · Görünen miktar değişimleri</h4><div id="v23Book" class="v23-stats"></div></div>
  <div id="v23Divergence" class="v23-note">Fiyat / Delta uyuşmazlığı kontrolü bekleniyor.</div>
  <details><summary>Bu verileri nasıl okumalıyım?</summary><p><b>Delta:</b> Binance Spot'taki gerçekleşmiş agresif alış tutarı eksi agresif satış tutarıdır; emir defteri bakiyesi değildir.</p><p><b>Fiyat grupları:</b> Sıkıştırılmış fiyat aralıklarıdır, Binance tick ölçüsü veya tam borsa footprint'i değildir. Yalnız en yoğun 18 fiyat grubu görüntülenir.</p><p><b>Defter değişimi:</b> Ortalama saniyede bir alınan iki kısmi ilk-20 defter görüntüsünde ortak fiyatlardaki miktar farkıdır. Dışarı düşen fiyatlar iptal sayılmaz. Emir kimliği, spoofing, iptal ya da gerçekleşme kesin olarak bilinemez.</p><p>Sunucu belleğinde yalnızca en fazla yaklaşık 310 saniyelik anlık veriler tutulur; yeniden başlatma/bağlantı kopmasında sıfırlanır. Sinyal kaydı veya geçmiş sonuç takibi yoktur. Bu bölüm AL/SAT olasılığı değildir.</p></details>
 </div>`;
 const before=$('v2Flow');if(before&&before.parentElement===parent)parent.insertBefore(box,before);else parent.prepend(box);
 const style=d.createElement('style');style.id='timiV23OrderflowCSS';style.textContent=css;d.head.append(style);
 box.addEventListener('toggle',()=>{if(box.open)refresh(true);else{serial++;if(abort)abort.abort();}});
 $('v23Toolbar').addEventListener('click',event=>{
  const b=event.target.closest('button');if(!b)return;
  if(b.dataset.v23Window){selected=Number(b.dataset.v23Window);for(const x of $('v23Toolbar').querySelectorAll('[data-v23-window]'))x.setAttribute('aria-pressed',String(Number(x.dataset.v23Window)===selected));}
  refresh(true);
 });
 return true;
}
function empty(message){$('v23Badge').textContent='Veri bekleniyor';$('v23Status').className='v23-note bad';$('v23Status').textContent=message;$('v23Stats').replaceChildren();$('v23Compare').replaceChildren();$('v23Levels').replaceChildren();$('v23Book').replaceChildren();$('v23Divergence').textContent='Veri geçerli olana kadar çıkarım yapılmıyor.';}
function render(data){
 if(!active())return;
 const status=data.status||{},w=data.window||{},age=status.trade_age_ms;
 const valid=status.trade_fresh&&status.book_fresh&&age!==null&&typeof age==='number'&&age<=5000;
 if(!valid){empty('BEKLE · İşlem/defter akışı güncel değil veya Gateway bağlantısı kesilmiş.');return;}
 $('v23Badge').textContent=w.complete?'CANLI':'ISINIYOR';
 $('v23Status').className='v23-note';
 $('v23Status').textContent='Binance Spot · işlem '+fmt(status.trade_age_ms/1000,1)+' sn · defter '+fmt(status.book_age_ms/1000,1)+' sn · '+selected+' sn penceresi / '+w.observed_elapsed_sec+' sn gözlem'+(w.complete?'':' · henüz dolmadı');
 const stats=$('v23Stats');stats.replaceChildren(
  row('Agresif AL',total(w.buy_usdt)+' USDT','v23-green'),
  row('Agresif SAT',total(w.sell_usdt)+' USDT','v23-red'),
  row('Net Delta',total(w.delta_usdt)+' USDT',w.delta_usdt>0?'v23-green':w.delta_usdt<0?'v23-red':''),
  row('Delta dengesi',pct(w.delta_pct,1)),
  row('Fiyat değişimi',pct(w.price_change_pct,3)),
  row('Gerçekleşen işlem',String(w.trade_count??'—'))
 );
 const compare=$('v23Compare');compare.replaceChildren();
 for(const sample of Array.isArray(data.window_comparison)?data.window_comparison:[]){
  const r=d.createElement('div');r.className='v23-level';
  const title=d.createElement('span');title.textContent=sample.window_sec+' sn'+(sample.complete?' · tam':' · kısmi');
  const trades=d.createElement('span');trades.textContent=String(sample.trade_count??0)+' işlem';
  const delta=d.createElement('span');delta.textContent=total(sample.delta_usdt)+' USDT';delta.className=sample.delta_usdt>0?'v23-green':sample.delta_usdt<0?'v23-red':'';
  r.append(title,trades,delta);compare.append(r);
 }
 const list=$('v23Levels');list.replaceChildren();
 const levels=Array.isArray(data.price_levels)?data.price_levels:[];
 if(levels.length===0){const n=d.createElement('p');n.textContent='Seçili fiyat penceresinde henüz gerçekleşen işlem yok.';list.append(n);}
 else{
  const h=d.createElement('div');h.className='v23-level';h.innerHTML='<span>Fiyat aralığı</span><span>Agresif AL</span><span>Agresif SAT</span>';list.append(h);
  const max=Math.max(1,...levels.map(x=>Math.max(x.buy_usdt||0,x.sell_usdt||0)));
  for(const x of levels){const r=d.createElement('div');r.className='v23-level';const p=d.createElement('span');p.className='v23-level-price';p.textContent=fmt(x.lower_price,8)+'–'+fmt(x.upper_price,8);const b=d.createElement('span');b.className='v23-green';b.textContent=total(x.buy_usdt);const s=d.createElement('span');s.className='v23-red';s.textContent=total(x.sell_usdt);r.append(p,b,s);const meter=d.createElement('div');meter.className='v23-meter';meter.style.gridColumn='1/-1';const fill=d.createElement('i');fill.style.width=Math.max(0,Math.min(100,(x.buy_usdt||0)/max*100))+'%';meter.append(fill);r.append(meter);list.append(r);}
 }
 $('v23Coverage').textContent='Fiyat grubu genişliği: '+fmt(data.price_group_step,8)+' USDT · En yoğun 18 grup · '+
 (w.unbinned_trade_count?'Yoğunluk sınırı: '+w.unbinned_trade_count+' işlem fiyat gruplarına dağıtılamadı. ':'')+
 'Geçmiş borsa footprint’i değil; sunucuda gözlenen geçici veriler.';
 const bc=data.book_changes,book=$('v23Book');book.replaceChildren();
 if(bc&&bc.sample_pairs>0){book.append(row('Alış seviyesi artışı',total(bc.bid_increase_usdt)+' USDT','v23-green'),row('Alış seviyesi azalışı',total(bc.bid_decrease_usdt)+' USDT'),row('Satış seviyesi artışı',total(bc.ask_increase_usdt)+' USDT','v23-red'),row('Satış seviyesi azalışı',total(bc.ask_decrease_usdt)+' USDT'),row('Kıyaslanan çift',String(bc.sample_pairs)),row('Ortak fiyat kıyası',String(bc.matched_price_level_comparisons)));}
 else{const message=d.createElement('p');message.textContent='Kıyaslanabilir ardışık kısmi defter görüntüsü henüz yok.';book.append(message);}
 $('v23Divergence').className=data.divergence_candidate?'v23-note bad':'v23-note';
 $('v23Divergence').textContent=data.divergence_candidate?'FİYAT–DELTA UYUŞMAZLIĞI ADAYI · '+data.divergence_candidate+'. Tek başına sinyal değildir.':
 (w.complete?'Fiyat / Delta için tanımlı çelişki eşiği aşılmadı.':'Pencere henüz tamamlanmadı; uyuşmazlık yorumu bekleniyor.');
}
async function refresh(force=false){
 if(!build()||!active())return;
 const s=symbol(),g=gateway();
 if(s!==lastSymbol||g!==lastGateway){lastSymbol=s;lastGateway=g;lastUpdate=0;}
 if(pending&&!force)return;
 if(!force&&Date.now()-lastUpdate<4500)return;
 const id=++serial;if(abort)abort.abort();const controller=new AbortController();abort=controller;pending=true;
 const timeout=setTimeout(()=>controller.abort(),9000);
 try{
  const response=await fetch(g+'/api/v3/orderflow/'+encodeURIComponent(s)+'?window='+selected,{cache:'no-store',signal:controller.signal});
  if(!response.ok)throw new Error('HTTP '+response.status);
  const data=await response.json();
  if(id!==serial||s!==symbol()||g!==gateway()||!active())return;
  if(data.symbol!==s||data.window_sec!==selected)throw new Error('Parite veya zaman penceresi uyuşmuyor');
  lastUpdate=Date.now();render(data);
 }catch(e){if(id===serial&&active())empty('Gateway bağlantısı veya yeni v3 veri ucu erişilemiyor: '+String(e?.message||e).slice(0,70));}
 finally{clearTimeout(timeout);if(id===serial)pending=false;}
}
function boot(){if(!build()){root.__timiV23Active=false;return;}
 $('symbol')?.addEventListener('change',()=>{serial++;lastUpdate=0;refresh(true);});
 $('saveGateway')?.addEventListener('click',()=>{serial++;lastUpdate=0;setTimeout(()=>refresh(true),350);});
 for(const b of d.querySelectorAll('[data-pro-tab],nav.foot button[data-page="proLab"]'))b.addEventListener('click',()=>setTimeout(()=>refresh(true),180));
 root.setInterval(()=>{if(active())refresh(false)},4800);
 root.timiV23Debug=()=>({version:'3.0.0',readOnly:true,signalsStored:false,window:selected,hasPanel:!!$('timiV23Orderflow'),symbol:symbol()});
}
if(d.readyState==='loading')d.addEventListener('DOMContentLoaded',boot,{once:true});else boot();
})(typeof window!=='undefined'?window:null);
