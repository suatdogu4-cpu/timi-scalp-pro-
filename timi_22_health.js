/* TiMi v2.2: Veri Guven Merkezi — source-trace extension.
 * Independent original code; no external project code copied.
 * READ ONLY; no signal archives, execution, private credentials or persistence.
 */
(function(root){
  'use strict';
  const MAX_AGE={flow:7000,analysis:12000,health:120000,klines:130000,futures:90000};
  const isFiniteNumber=v=>typeof v==='number'&&Number.isFinite(v);
  const num=v=>v===null||v===undefined||v===''?null:(Number.isFinite(Number(v))?Number(v):null);
  function age(time,now){const t=num(time);return t===null?null:now-t;}
  function gradeAge(value,limit){return value===null||value< -5000||value>limit?'bad':'good';}
  function healthSummary(inputs, now=Date.now()){
    const {flow,analysis,klines,futures,server}=inputs||{};
    const results=[];
    const add=(id,title,status,detail,source)=>results.push({id,title,status,detail,source});
    const flowAge=age(flow?.time_ms,now);
    const freshResponse=gradeAge(flowAge,MAX_AGE.flow)==='good';
    const bookFresh=freshResponse&&flow?.status?.book_fresh===true&&num(flow?.status?.book_age_ms)!==null&&flow.status.book_age_ms<=5000;
    const tradeFresh=freshResponse&&flow?.status?.trades_fresh===true&&num(flow?.status?.trade_age_ms)!==null&&flow.status.trade_age_ms<=5000;
    add('book','Emir defteri',bookFresh?'good':'bad',bookFresh?`20 seviye · ${Math.max(0,Math.round(flow.status.book_age_ms/1000))} sn`:'Gecikmiş, bağlantısız veya eksik','Binance Spot · partial depth 20');
    add('trades','Gerçekleşmiş işlemler',tradeFresh?'good':'bad',tradeFresh?`${Math.max(0,Math.round(flow.status.trade_age_ms/1000))} sn`:'İşlem akışı güncel değil','Binance Spot · public @trade');
    const analysisAge=age(analysis?.ts,now);
    const candleAge=num(analysis?.analysis_candle_age_ms);
    const candleOk=gradeAge(analysisAge,MAX_AGE.analysis)==='good'&&candleAge!==null&&candleAge>=-5000&&candleAge<130000;
    add('analysis','Kapalı mum analizi',candleOk?'good':'bad',candleOk?`Analiz ${Math.round(analysisAge/1000)} sn önce · 1m kapanış ${Math.round(candleAge/1000)} sn`:'Analiz veya kapalı mum gecikmiş','TiMi · /api/analysis-pro');
    const responseAge=age(klines?._received_at,now);
    const candleList=Array.isArray(klines?.candles)?klines.candles:[];
    const step=60000;
    const lastBar=[...candleList].reverse().find(b=>num(b?.t)!==null && num(b.t)+step<=now-2500);
    const closeTime=num(lastBar?.t);
    const lastClosed=closeTime!==null;
    const klineFresh=lastClosed&&now-closeTime<MAX_AGE.klines && responseAge!==null&&responseAge<20000&&candleList.length>=70;
    add('history','1m veri penceresi',klineFresh?'good':'bad',klineFresh?`${candleList.length} mum · son kapanış ${Math.round((now-closeTime-step)/1000)} sn`:'Eksik, açık mum veya eski fiyat dizisi','Binance/OKX mum Gateway’i');
    const futAge=age(futures?.time_ms,now);
    const responseValid=gradeAge(futAge,MAX_AGE.futures)==='good';
    const bAvailable=responseValid&&futures?.binance?.available===true;
    const oAvailable=responseValid&&futures?.okx?.available===true;
    const errors=futures?.errors||{};
    const restriction=Object.entries(errors).filter(([_,v])=>String(v).includes('451')).map(([k])=>k);
    add('binance_futures','Binance Futures',bAvailable?'good':'bad',bAvailable?'Genel veri erişilebilir':restriction.length?`HTTP 451 · Bölgesel kısıt (${restriction.join(', ')})`:'Kaynak erişimi / veri doğrulaması başarısız','Binance USD-M · mark/funding/OI/taker');
    add('okx_futures','OKX Futures',oAvailable?'good':'bad',oAvailable?'Funding / OI kaynakları erişilebilir':'Kaynak erişimi / veri doğrulaması başarısız','OKX USDT SWAP · funding/OI');
    const wsConnected=server?.ws===true;
    add('browser_ws','Telefon WebSocket',wsConnected?'good':'bad',wsConnected?'Bağlantı açık':'Telefon ile Gateway bağlantısı kapalı','Tarayıcı · TiMi gateway WS');
    const good=results.filter(x=>x.status==='good').length;
    return {results,good,total:results.length,complete:good===results.length,critical:bookFresh&&tradeFresh&&candleOk,
      restriction,ageFlowMs:flowAge,rule:'Bu sağlık denetimi AL/SAT kararı veya başarı olasılığı değildir.'};
  }
  if(typeof module==='object'&&module.exports)module.exports={healthSummary,MAX_AGE};
  if(!root||!root.document)return;
  if(root.__timiHealth22Loaded)return;
  root.__timiHealth22Loaded=true;
  const d=root.document,el=id=>d.getElementById(id);
  let busy=false,seq=0,lastQuery=0,lastSymbol='',lastGateway='';
  function gateway(){
    const s=String(el('gateway')?.value||root.location.origin).trim().replace(/\/$/,'');
    try {const u=new URL(s);if(u.protocol==='https:'||(u.hostname==='localhost'&&u.protocol==='http:'))return u.origin;}catch(_){ }
    return root.location.origin;
  }
  function symbol(){return String(el('symbol')?.value||'BTCUSDT').toUpperCase().replace(/[^A-Z0-9]/g,'').slice(0,20);}
  function wsOk(){try{return root.document.getElementById('wsState')?.textContent==='Gateway açık'||Boolean(ws?.readyState===1)}catch(_){return false}}
  async function request(path,timeout=12500){
    const controller=new AbortController(),tid=setTimeout(()=>controller.abort(),timeout);
    try{const r=await fetch(gateway()+path,{signal:controller.signal,cache:'no-store'});if(!r.ok)throw Error('Gateway HTTP '+r.status);return await r.json();}
    finally{clearTimeout(tid)}
  }
  function create(){
    if(el('timiHealth22'))return true;
    const shell=el('proLab')?.querySelector('.pro-shell');if(!shell)return false;
    const box=d.createElement('details');box.id='timiHealth22';box.className='timi-health22';
    box.innerHTML=`<summary><span>Veri Güven Merkezi <small>· Kaynak, gecikme, kısıt</small></span><strong id="timiHealth22Badge">Kontrol edilmedi</strong></summary>
    <div class="timi-h22-body"><p>Veri sağlığı, sinyal başarı oranı değildir. Eksik veriye sayı uydurulmaz.</p>
      <div class="timi-h22-controls"><button id="timiHealth22Refresh" type="button">Kaynakları denetle</button><span id="timiHealth22Status">Bu panel açılınca isteğe bağlı sorgu yapılır.</span></div>
      <div id="timiHealth22List" role="list" class="timi-h22-list"></div>
      <details><summary>Kaynak tanımları ve sınırlar</summary><p>Binance Spot: 20 seviye kısmi defter ve Gateway'in gözlemlediği işlemler. Futures: Binance USD-M ve OKX SWAP farklı OI birimlerine sahiptir; doğrudan kıyaslanmaz. Tarihsel DOM/footprint bu panelde yoktur. HTTP 451 bölgesel kısıt durumudur, veri sıfır demek değildir.</p></details>
    </div>`;
    const heading=shell.querySelector('.pro-heading');if(heading)heading.after(box);else shell.prepend(box);
    box.addEventListener('toggle',()=>{if(box.open)refresh()});
    el('timiHealth22Refresh').addEventListener('click',()=>refresh(true));
    const style=d.createElement('style');style.id='timi-health22-style';style.textContent=`
#timiHealth22{border:1px solid #31556a;border-radius:12px;background:#142a3e;max-width:100%;color:#e2effd;font:12px system-ui;margin:0 0 9px}
#timiHealth22>summary{list-style:none;cursor:pointer;padding:12px;display:flex;gap:7px;align-items:center;justify-content:space-between;min-height:45px}
#timiHealth22>summary::-webkit-details-marker{display:none}
#timiHealth22>summary span{font-weight:750;font-size:13px}#timiHealth22>summary small{display:block;color:#aabed0;font-size:10px;font-weight:400;margin-top:2px}
#timiHealth22Badge{border-radius:15px;background:#23384f;padding:5px 8px;white-space:nowrap;font-size:10px;font-weight:700}
#timiHealth22Badge.ok{color:#76e5bc}#timiHealth22Badge.warning{color:#ffc882}
#timiHealth22 .timi-h22-body{padding:0 12px 12px;color:#acbfd2;font-size:11px;line-height:1.5}
#timiHealth22 .timi-h22-controls{display:flex;align-items:center;gap:9px;flex-wrap:wrap;margin:12px 0}
#timiHealth22Refresh{min-height:38px;border:1px solid #379a7d;border-radius:8px;background:#145141;color:#c2ffe9;padding:7px 11px;font-weight:700}
#timiHealth22Refresh:disabled{opacity:.5}
#timiHealth22Status{font-size:10px;color:#b9cae0}
#timiHealth22 .timi-h22-list{display:grid;gap:6px}
#timiHealth22 .timi-h22-item{display:grid;grid-template-columns:1fr auto;gap:2px 8px;padding:9px 10px;border:1px solid #2b4760;border-radius:9px;background:#1c3249}
#timiHealth22 .timi-h22-item b{font-size:11px}#timiHealth22 .timi-h22-item em{font-style:normal;font-size:10px;font-weight:800}
#timiHealth22 .timi-h22-item .ok{color:#6be3b7}#timiHealth22 .timi-h22-item .bad{color:#ffc082}
#timiHealth22 .timi-h22-item small{grid-column:1/-1;color:#aec1d5;line-height:1.45}
#timiHealth22 .timi-h22-body>details{margin-top:10px;border-top:1px solid #34516a;padding-top:8px}
#timiHealth22 button:focus-visible,#timiHealth22 summary:focus-visible{outline:2px solid #7ce3cf;outline-offset:3px}
@media(max-width:380px){#timiHealth22>summary{padding:10px}#timiHealth22 .timi-h22-item{padding:8px}}`;
    d.head.append(style);return true;
  }
  function row(x){const line=d.createElement('div');line.className='timi-h22-item';line.setAttribute('role','listitem');
    const label=d.createElement('b'),status=d.createElement('em'),desc=d.createElement('small'),source=d.createElement('small');
    label.textContent=x.title;status.className=x.status;status.textContent=x.status==='good'?'GEÇERLİ':'EKSİK';
    desc.textContent=x.detail;source.textContent=x.source;line.append(label,status,desc,source);return line;}
  async function refresh(force=false){
    const box=el('timiHealth22');if(!box?.open||busy)return;
    const now=Date.now();if(!force&&now-lastQuery<12000)return;
    if(now-lastQuery<2500)return;
    const s=symbol(),g=gateway(),id=++seq;lastQuery=now;lastSymbol=s;lastGateway=g;
    busy=true;el('timiHealth22Refresh').disabled=true;el('timiHealth22Status').textContent='Kaynaklar sorgulanıyor…';
    const paths={flow:`/api/v2/flow/${s}`,analysis:`/api/analysis-pro/${s}`,klines:`/api/klines/${s}?interval=1m&limit=150`,futures:`/api/v2/futures/${s}`};
    const entries=Object.entries(paths);const values=await Promise.allSettled(entries.map(([_,path])=>request(path)));
    if(id!==seq||s!==symbol()||g!==gateway()){busy=false;el('timiHealth22Refresh').disabled=false;return;}
    const obj={},errors=[];for(let i=0;i<entries.length;i++){
      if(values[i].status==='fulfilled')obj[entries[i][0]]=values[i].value;
      else errors.push(entries[i][0]+': '+String(values[i].reason?.message||values[i].reason).slice(0,75));
    }
    if(obj.klines)obj.klines._received_at=Date.now();
    const summary=healthSummary({...obj,server:{ws:wsOk()}},Date.now());
    const target=el('timiHealth22List');if(target)target.replaceChildren(...summary.results.map(row));
    const badge=el('timiHealth22Badge');badge.textContent=summary.good+'/'+summary.total+' kaynak';badge.className=summary.complete?'ok':'warning';
    el('timiHealth22Status').textContent=errors.length?'Erişilemeyen uç: '+errors.join(' · '):'Denetim güncellendi · '+new Date().toLocaleTimeString('tr-TR');
    el('timiHealth22Refresh').disabled=false;busy=false;
  }
  function boot(){
    if(!create()){root.__timiHealth22Loaded=false;return;}
    el('symbol')?.addEventListener('change',()=>{seq++;lastQuery=0;const b=el('timiHealth22Badge');if(b)b.textContent='Yeni parite';});
    el('saveGateway')?.addEventListener('click',()=>{seq++;lastQuery=0;const b=el('timiHealth22Badge');if(b)b.textContent='Yeni bağlantı';});
    root.timiHealth22Debug=()=>({version:'2.2.0',readOnly:true,recordsSaved:false,hasPanel:!!el('timiHealth22')});
  }
  if(d.readyState==='loading')d.addEventListener('DOMContentLoaded',boot,{once:true});else boot();
})(typeof window!=='undefined'?window:null);
