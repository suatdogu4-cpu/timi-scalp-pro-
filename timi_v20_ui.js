/* TiMi PRO v2.0 — five large read-only modules in existing four-tab UI.
 * Flow PRO 2 | SMC PRO 3 | Adaptive Engines | Futures Radar | Risk Shield
 * No historical signal archive, no persistent monitoring, no order submission.
 */
(function(root){
'use strict';
if(!root?.document || root.__timiProV20Installed)return;
root.__timiProV20Installed=true;
const d=root.document,C=root.TiMiV20Core;
if(!C){root.__timiProV20Installed=false;return;}
const el=id=>d.getElementById(id),num=x=>x===null||x===undefined||x===''?NaN:Number(x),finite=Number.isFinite;
const fmt=(n,k=2)=>finite(n)?n.toLocaleString('tr-TR',{maximumFractionDigits:k}):'—';
const pct=(n,k=2)=>finite(n)?fmt(n,k)+'%':'—';
const money=n=>finite(n)?fmt(n,0)+' USDT':'—';
const short=(x,max=160)=>String(x??'').slice(0,max);
const dirs={LONG:'YUKARI',SHORT:'AŞAĞI',WAIT:'BEKLE'};
const names={trend:'Trend / MTF',momentum:'Momentum',reversion:'Ortalamaya dönüş',breakout:'Yapı kırılımı',smc:'SMC',flow:'Gerçekleşmiş akış'};
let activeSymbol='',activeBase='',sequence=0,flow=null,futures=null,states={},smc={},answer=null,risk=null;
let flowTime=0,futuresTime=0,tfTime=0,fetchingFlow=false,fetchingFuture=false,fetchingTF=false;
const symbol=()=>['BTCUSDT','ETHUSDT','SOLUSDT','BNBUSDT','XRPUSDT','DOGEUSDT','ADAUSDT','AVAXUSDT'].includes(el('symbol')?.value)?el('symbol').value:'BTCUSDT';
function base(){let input=el('gateway')?.value?.trim().replace(/\/$/,'')||root.location.origin;
 try{const u=new URL(input);if(u.protocol==='https:'||u.hostname==='localhost')return u.origin;}catch(_){}
 return root.location.origin;
}
const proActive=()=>el('proLab')?.classList.contains('active')===true;
const overviewActive=()=>el('overview')?.classList.contains('active')===true;
const proPanel=id=>proActive()&&el(id)?.hidden===false;
function text(id,msg){const node=el(id);if(node)node.textContent=msg;}
function cssClass(v){return v==='LONG'?'v2-green':v==='SHORT'?'v2-red':'v2-gold';}
function escapeHtml(x){return short(x,120).replace(/[&<>"']/g,c=>({'&':'&amp;','<':'&lt;','>':'&gt;','"':'&quot;',"'":'&#39;'}[c]));}
async function request(path,timeout=12000){const c=new AbortController(),id=setTimeout(()=>c.abort(),timeout);
 try{const r=await fetch(base()+path,{cache:'no-store',signal:c.signal});if(!r.ok)throw Error('HTTP '+r.status);return await r.json();}
 finally{clearTimeout(id);}
}
function ensureContext(){
 const sym=symbol(),b=base(),key=sym+'|'+b;
 if(key===activeSymbol+'|'+activeBase)return;
 activeSymbol=sym;activeBase=b;sequence++;flow=futures=null;states={};smc={};answer=risk=null;
 flowTime=futuresTime=tfTime=0;
 ['v2FlowStatus','v2SmcStatus','v2AdaptiveStatus','v2FuturesStatus','v2RiskStatus'].forEach(id=>text(id,'Yeni parite · canlı veri doğrulanıyor'));
 ['v2FlowRows','v2SmcRows','v2AdaptiveRows','v2FuturesRows','v2RiskRows','v2SummaryRows'].forEach(id=>{if(el(id))el(id).replaceChildren();});
}
const CSS=`
 .v2-card{border:1px solid #35536b;border-radius:13px;background:linear-gradient(145deg,#142b42,#0e2032);padding:12px;margin:12px 0;display:grid;gap:9px;min-width:0;color:#e4f0ff;font-family:system-ui}
 .v2-head{display:flex;justify-content:space-between;align-items:center;gap:9px;flex-wrap:wrap}
 .v2-head strong{font-size:13px;line-height:1.3}
 .v2-kicker{font-size:10px;color:#98b7cf;letter-spacing:.2px}
 .v2-grid{display:grid;grid-template-columns:repeat(3,minmax(0,1fr));gap:6px}
 .v2-grid.two{grid-template-columns:repeat(2,minmax(0,1fr))}
 .v2-cell{background:#203951;border:1px solid #304b67;border-radius:9px;padding:8px 5px;text-align:center;min-width:0}
 .v2-cell small{display:block;min-height:26px;color:#b8cede;font-size:9.5px}
 .v2-cell b{display:block;overflow-wrap:anywhere;margin-top:3px;font-size:clamp(11px,3.4vw,15px)}
 .v2-cell em{font-style:normal;font-size:9px;color:#8facc3}
 .v2-green{color:#66e2b9!important}.v2-red{color:#ff819e!important}.v2-gold{color:#f1c777!important}
 .v2-line{display:flex;align-items:center;justify-content:space-between;gap:8px;padding:7px 1px;border-bottom:1px solid #30495f;font-size:11px}
 .v2-line span{min-width:0;color:#bbcedf}.v2-line b{font-size:11px;text-align:right;min-width:0}
 .v2-status{font-size:11px;color:#b3c9df;line-height:1.5;border-left:2px solid #405e77;padding:6px 8px;background:#1b3045}
 .v2-muted{color:#9cb6c9;font-size:10px;line-height:1.55}
 .v2-button{font-size:11px;background:#1c4c42;border:1px solid #3ba58b;color:#99f4cf;border-radius:8px;padding:8px 11px;cursor:pointer}
 .v2-bad{background:#472737;border-color:#ad5870;color:#ffd5e1}
 .v2-emph{font-size:16px;font-weight:900;line-height:1.4}
 .v2-track{background:#3e3146;height:8px;border-radius:7px;overflow:hidden}
 .v2-track span{display:block;height:100%;background:#3cd8ab;max-width:100%}
 .v2-collapsed{border-top:1px solid #304b64;padding-top:8px;font-size:11px}
 .v2-collapsed summary{color:#aee5d6;cursor:pointer}
 #overview>.v2-card{margin:0 0 12px}
 @media(max-width:385px){.v2-cell{padding:7px 3px}.v2-grid{gap:4px}}
`;
const widget=(title,subtitle,body,id)=>`<div class="v2-card" id="${id}">
  <div class="v2-head"><strong>${title}</strong><small class="v2-kicker">${subtitle}</small></div>${body}</div>`;
function setup(){
 const pf=el('proFlow'),sm=el('proSmc'),lab=el('proLabTest'),overview=el('overview');
 if(!pf||!sm||!lab||!overview||el('v2Flow'))return false;
 const style=d.createElement('style');style.id='timiV20Styles';style.textContent=CSS;d.head.append(style);
 pf.insertAdjacentHTML('afterbegin',widget('Order Flow PRO 2.0','Binance Spot · oturumluk canlı ölçüm',
  `<div id="v2FlowStatus" class="v2-status">İşlem akışı bekleniyor</div>
  <div id="v2FlowRows"></div><div id="v2Absorption" class="v2-status">Emilim kontrolü bekleniyor</div>
  <details class="v2-collapsed"><summary>15 / 60 / 300 saniyelik ölçümler nasıl okunur?</summary>
  <p>Delta = agresif alış eksi agresif satış tutarıdır. Bu rakamlar yalnızca çalışan sunucunun aldığı işlemlerdir. Eksik gözlem süresi gösterilir; emir defteri yalnızca ilk 20 seviyeyi içerir.</p>
  <p>Emilim işareti yalnızca fiyatın az değişmesine rağmen tek tarafın agresif işlem hacmi baskınsa <b>aday</b> olarak gösterilir; iceberg veya spoofing kanıtı değildir.</p></details>`, 'v2Flow'));
 pf.insertAdjacentHTML('beforeend',widget('Futures Intelligence','Binance USD-M + OKX SWAP',
  `<div id="v2FuturesStatus" class="v2-status">Vadeli verileri bekleniyor</div>
  <div id="v2FuturesRows"></div>
  <details class="v2-collapsed"><summary>Funding / OI / taker oranları nasıl okunur?</summary>
  <p>Binance açık pozisyon miktarı, OKX USD açık pozisyon değeri ile doğrudan karşılaştırılamaz. Funding dönemleri farklı olabilir. Bu veriler tek başına LONG veya SHORT sinyali değildir.</p>
  <p>API kısıtlanırsa değer uydurulmaz; erişim yok olarak gösterilir. Gerçek tasfiye haritası bu sürümde yoktur.</p></details>`, 'v2Futures'));
 sm.insertAdjacentHTML('beforeend',widget('SMC PRO 3.0','Kapanmış mumlarla yapısal aday analizi',
  `<div id="v2SmcStatus" class="v2-status">Yapı hesaplaması bekleniyor</div><div id="v2SmcRows"></div>
  <details class="v2-collapsed"><summary>Order Block, Sweep, EQH/EQL ve Premium/Discount</summary>
  <p>Order Block: onaylanmış yapı kırılımından önceki ters renkli mumdan çıkarılan aday bölgedir. Sweep: onaylı swing seviyesinin fitille aşılması ve altında/üstünde kapanıştır. EQH/EQL: ATR toleranslı eşit tepe/dip adaylarıdır.</p>
  <p>Bölgeler kesin kurumsal emir veya garanti edilen likidite alanları değildir. Canlı mumlar bu hesaplara girmez.</p></details>`, 'v2Smc'));
 lab.insertAdjacentHTML('afterbegin',widget('Adaptive Engine v2.0','Canlı strateji aileleri ve piyasa rejimi',
  `<div id="v2AdaptiveStatus" class="v2-status">Kapalı mum ve akış verisi bekleniyor</div>
  <div id="v2AdaptiveRows"></div>
  <p class="v2-muted">Bu panel, mevcut 16-kural v1.5 analizine ek 6 strateji ailesiyle anlık doğrulama yapar. Skorlar başarı olasılığı değildir.</p>`, 'v2Adaptive'));
 overview.insertAdjacentHTML('afterbegin',widget('Risk Shield 2.0','Canlı araştırma güvenlik katmanı',
  `<div id="v2RiskStatus" class="v2-status">Veri doğrulanıyor · BEKLE</div><div id="v2RiskRows"></div>
  <details class="v2-collapsed"><summary>ATR / maliyet / risk vetosu açıklaması</summary>
  <p>Varsayımsal stop = 1,5 × ATR14, hedef = 2,5 × ATR14. Hesaplamalar emir açmaz. Komisyon ve kayma değerleri PRO → Strateji alanından alınır. Veriler gecikirse durum BEKLE olur.</p></details>`, 'v2Risk'));
 return true;
}
function row(label,val,type){const div=d.createElement('div');div.className='v2-line';const a=d.createElement('span');a.textContent=label;const b=d.createElement('b');b.textContent=val;if(type)b.className=type;div.append(a,b);return div;}
function add(id,title,value,unit){const div=d.createElement('div');div.className='v2-cell';const small=d.createElement('small');small.textContent=title;const strong=d.createElement('b');strong.textContent=value;div.append(small,strong);if(unit){const em=d.createElement('em');em.textContent=unit;div.append(em);}return div;}
function grid(id,items){const elem=el(id);if(!elem)return;elem.replaceChildren();const box=d.createElement('div');box.className='v2-grid';for(const [t,v,u] of items)box.append(add(id,t,v,u));elem.append(box);}
function renderFlow(){const rootElem=el('v2FlowRows');if(!rootElem||!flow)return;
 const st=flow.status,book=flow.book;const fresh=st?.book_fresh&&st?.trades_fresh;
 text('v2FlowStatus',`Kaynak: Binance Spot · İşlem ${finite(st?.trade_age_ms)?fmt(st.trade_age_ms/1000,1)+' sn':'—'} · Defter ${finite(st?.book_age_ms)?fmt(st.book_age_ms/1000,1)+' sn':'—'} · ${fresh?'CANLI':'GECİKMELİ'}`);
 rootElem.replaceChildren();
 const boxes=d.createElement('div');boxes.className='v2-grid';
 boxes.append(add('','Mikrofiyat',finite(book.microprice)?fmt(book.microprice,6):'—','USDT'),add('','Spread',pct(book.spread_pct,4),'spot'),add('','Defter dengesi',pct(book.imbalance_pct,1),'ilk 20 seviye'));
 rootElem.append(boxes);
 for(const dur of ['15','60','300']){
  const w=flow.windows?.[dur];if(!w)continue;
  rootElem.append(row(`${dur} sn · Delta`,money(w.delta_usdt),w.delta_usdt>0?'v2-green':w.delta_usdt<0?'v2-red':''));
  rootElem.append(row(`${dur} sn · Alış / Satış`,`${money(w.buy_usdt)} / ${money(w.sell_usdt)}`));
  rootElem.append(row(`${dur} sn · Gözlem`,`${w.observed_elapsed_sec}/${dur} sn · ${w.trade_count} işlem ${w.complete_window?'✓':'(kısmi)'}`));
 }
 const reg=C.flowRegime(flow);
 text('v2Absorption',flow.absorption_candidate?
   `Olası emilim adayı: ${flow.absorption_candidate==='ALIS_EMILIM_ADAYI'?'agresif AL':'agresif SAT'} baskısı var, fiyat tepkisi sınırlı. Kesin doğrulama değildir.`:
   reg.clash?'ÇELİŞKİ: Bekleyen defter likiditesi ile gerçekleşmiş işlemler ters yönde.':
   'Emilim kanıtı yok. Tek başına AL/SAT kararı verilmez.');
}
function renderFutures(){const target=el('v2FuturesRows');if(!target)return;target.replaceChildren();if(!futures){text('v2FuturesStatus','Kaynaklar sorgulanıyor');return;}
 const b=futures.binance,o=futures.okx;
 text('v2FuturesStatus',`Kamuya açık vadeli piyasa verileri · ${new Date(futures.time_ms).toLocaleTimeString('tr-TR')} · ${b?.available?'Binance ✓':'Binance erişim yok'} / ${o?.available?'OKX ✓':'OKX erişim yok'}`);
 target.append(row('Binance USD-M · Mark',b?.mark_price?fmt(b.mark_price,6)+' USDT':'—'));
 target.append(row('Binance Funding (son)',pct(b?.funding_rate_pct,5)));
 target.append(row('Binance Open Interest',finite(b?.open_interest_quantity)?fmt(b.open_interest_quantity,3)+' miktar':'—'));
 target.append(row('Binance 5m taker alım/satım',finite(b?.taker_buy_sell_ratio_5m)?fmt(b.taker_buy_sell_ratio_5m,3)+' oran':'—'));
 target.append(row('OKX Funding',pct(o?.funding_rate_pct,5)));
 target.append(row('OKX Open Interest',finite(o?.open_interest_usd)?fmt(o.open_interest_usd,0)+' USD':'—'));
 if(Object.keys(futures.errors||{}).length){const note=d.createElement('div');note.className='v2-muted';note.textContent='API eksikleri: '+Object.entries(futures.errors).map(([k,v])=>k+' '+v).join(' · ');target.append(note);}
}
function renderSmc(){const target=el('v2SmcRows');if(!target)return;target.replaceChildren();
 const keys=['1m','5m','15m'];const one=states['1m'];
 const ready=one?.ready&&smc['1m']?.ready;
 text('v2SmcStatus',ready?`Kapanmış mumlar: 1m ${one.count} · 5m ${states['5m']?.count??0} · 15m ${states['15m']?.count??0} · Açık mum hariç`:'Yapı verisi yetersiz veya gecikmiş; alanlar BEKLE');
 for(const k of keys){const z=smc[k],tf=states[k];if(!z?.ready||!tf?.ready){target.append(row(k+' · Yapı','BEKLE','v2-gold'));continue;}
  const cutoff=tf.bars.at(-1).t-C.STEP[k]*30;
  const ob=z.blocks.filter(b=>b.status!=='GECERSIZ'&&b.createdAt>=cutoff).slice(-2),
    sw=z.sweeps.filter(x=>x.time>=cutoff).slice(-2),
    eq=z.equalHighs.filter(x=>x.time>=cutoff).length+z.equalLows.filter(x=>x.time>=cutoff).length;
  target.append(row(k+' · OB adayları',`${ob.length} aktif · ${ob.length?ob.map(x=>(x.side==='LONG'?'↑':'↓')+' '+fmt(x.lower,2)+'–'+fmt(x.upper,2)).join(' / '):'—'}`));
  target.append(row(k+' · Sweep / EQH-EQL',`${sw.length} sweep · ${eq} EQH/EQL · son 30 mum`));
  target.append(row(k+' · Premium/Discount',`${z.range.zone} · orta ${fmt(z.range.midpoint,2)}`));
 }
 const msg=d.createElement('p');msg.className='v2-muted';msg.textContent='OB ve likidite işaretleri yalnızca hesaplanan adaylardır; piyasa yapısı kesin giriş sinyali değildir.';target.append(msg);
}
function renderAdaptive(){const target=el('v2AdaptiveRows');if(!target)return;target.replaceChildren();if(!answer){text('v2AdaptiveStatus','Yeterli veri bekleniyor');return;}
 const a=answer;
 text('v2AdaptiveStatus',`${a.conclusion==='WAIT'?'BEKLE':dirs[a.conclusion]+' ARAŞTIRMA UYUMU'} · Rejim: ${a.regime} · ${a.ready?'Kapanmış MTF doğrulandı':'MTF eksik'}`);
 const stats=d.createElement('div');stats.className='v2-grid';stats.append(add('','Yukarı',String(a.votes.LONG),'aile'),add('','Aşağı',String(a.votes.SHORT),'aile'),add('','Nötr',String(a.votes.WAIT),'aile'));target.append(stats);
 for(const [k,v]of Object.entries(a.families))target.append(row(names[k],dirs[v]||'BEKLE',cssClass(v)));
 for(const reason of a.causes.slice(0,4)){const n=d.createElement('div');n.className='v2-muted';n.textContent='• '+reason;target.append(n);}
}
function renderRisk(){const target=el('v2RiskRows');if(!target)return;target.replaceChildren();if(!risk){text('v2RiskStatus','BEKLE · Live risk verisi bekleniyor');return;}
 text('v2RiskStatus',risk.approved?`${dirs[risk.decision]} · Yalnızca koşullu araştırma uyumu`:`BEKLE · ${risk.veto.length} risk/veto kontrolü`);
 const grid=d.createElement('div');grid.className='v2-grid';grid.append(add('','ATR14',pct(risk.atrPct,2),'1m'),add('','Gidiş–dönüş maliyet',pct(risk.roundtripCostPct,3),'varsayım'),add('','Risk veto',String(risk.veto.length),'aktif'));target.append(grid);
 const v=risk.reference;
 if(v){target.append(row('Örnek giriş',fmt(v.entry,4)+' USDT'));
  target.append(row('ATR referans STOP',fmt(v.stop,4)+' USDT'));
  target.append(row('ATR referans TP',fmt(v.target,4)+' USDT · R/R '+fmt(v.rewardRisk,2)));}
 for(const s of risk.veto.slice(0,6)){const p=d.createElement('div');p.className='v2-muted v2-red';p.textContent='• '+s;target.append(p);}
}
function updateAnswers(){if(!states['1m']){answer=null;risk=null;text('v2RiskStatus','BEKLE · Kapanmış mum veya Gateway verisi bekleniyor');text('v2AdaptiveStatus','BEKLE · MTF verisi bekleniyor');for(const id of ['v2RiskRows','v2AdaptiveRows'])el(id)?.replaceChildren();return;}
 answer=C.adaptive(states,flow,smc['1m'],futures);
 const fee=num(el('proBtFee')?.value),slip=num(el('proBtSlip')?.value);
 risk=C.riskShield(states,flow,answer,{feePct:fee,slipPct:slip});
 renderAdaptive();renderRisk();
}
async function updateFlow(force=false){if(fetchingFlow)return;ensureContext();if(!force&&Date.now()-flowTime<3900)return;
 const s=symbol(),g=base(),serial=sequence;fetchingFlow=true;
 try{const reply=await request('/api/v2/flow/'+s,9500);
  if(serial!==sequence||s!==symbol()||g!==base()||reply.symbol!==s)return;
  flow=reply;flowTime=Date.now();renderFlow();updateAnswers();}
 catch(e){if(serial===sequence){flow=null;flowTime=0;text('v2FlowStatus','Akış PRO 2.0 Gateway erişimi yok: '+short(e.message,80));updateAnswers();}}
 finally{fetchingFlow=false;}
}
async function updateFuture(force=false){if(fetchingFuture)return;ensureContext();if(!force&&Date.now()-futuresTime<35000)return;
 const s=symbol(),g=base(),serial=sequence;fetchingFuture=true;
 try{const r=await request('/api/v2/futures/'+s,15000);
  if(serial!==sequence||s!==symbol()||g!==base()||r.symbol!==s)return;
  futures=r;futuresTime=Date.now();renderFutures();updateAnswers();}
 catch(e){if(serial===sequence){futures=null;futuresTime=Date.now();text('v2FuturesStatus','Futures Gateway erişimi yok: '+short(e.message,90));}}
 finally{fetchingFuture=false;}
}
async function updateTF(force=false){if(fetchingTF)return;ensureContext();if(!force&&Date.now()-tfTime<45000)return;
 const s=symbol(),g=base(),serial=sequence;fetchingTF=true;
 text('v2SmcStatus','Kapanmış 1m / 5m / 15m mumları alınıyor');
 try{const replies=await Promise.allSettled(['1m','5m','15m'].map(tf=>request('/api/klines/'+s+'?interval='+tf+'&limit=300',15000)));
  if(serial!==sequence||s!==symbol()||g!==base())return;
  const freshStates={},smcStates={};
  ['1m','5m','15m'].forEach((tf,i)=>{
    const r=replies[i];if(r.status!=='fulfilled'||r.value.symbol!==s||r.value.interval!==tf){freshStates[tf]={tf,ready:false,reason:'API yanıtı yok'};smcStates[tf]={ready:false};return;}
    const st=C.tfState(r.value.candles,tf,Date.now());freshStates[tf]=st;
    smcStates[tf]=st.ready?C.smcExtended(st.bars,tf):{ready:false};
  });
  states=freshStates;smc=smcStates;tfTime=Date.now();renderSmc();updateAnswers();
 }catch(e){if(serial===sequence){states={};smc={};tfTime=0;text('v2SmcStatus','MTF yükleme başarısız: '+short(e.message,75));updateAnswers();}}
 finally{fetchingTF=false;}
}
async function refresh(){ensureContext();const focus=proPanel('proFlow')||proPanel('proSmc')||proPanel('proLabTest');
 if(overviewActive()||focus){await updateFlow();if(overviewActive()||proPanel('proSmc')||proPanel('proLabTest'))void updateTF();if(proPanel('proFlow')||proPanel('proLabTest'))void updateFuture();}
}
function boot(){if(!setup())return;
 el('symbol')?.addEventListener('change',()=>{ensureContext();void refresh();});
 el('saveGateway')?.addEventListener('click',()=>setTimeout(()=>{ensureContext();void refresh();},350));
 d.querySelectorAll('[data-pro-tab],nav.foot [data-page="proLab"],nav.foot [data-page="overview"]').forEach(n=>n.addEventListener('click',()=>setTimeout(()=>void refresh(),130)));
 el('proBtFee')?.addEventListener('change',updateAnswers);el('proBtSlip')?.addEventListener('change',updateAnswers);
 setInterval(()=>void refresh(),4600);void refresh();
 root.timiV20Debug=()=>({version:'2.0.0',readOnly:true,archiving:false,flow:!!flow,futures:!!futures,
  symbol:activeSymbol,tf:Object.keys(states),advisory:answer?.conclusion||'WAIT',veto:risk?.veto||[]});
}
if(d.readyState==='loading')d.addEventListener('DOMContentLoaded',boot,{once:true});else boot();
})(typeof window!=='undefined'?window:null);
