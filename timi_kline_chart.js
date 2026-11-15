/* TiMi SCALP PRO - KLineChart 10.1.0 v1.1 period-switch fix.
 * Developer: Suat DOGU. Read only, no orders.
 * Drop-in script appended AFTER all of TiMi's existing scripts.
 * Uses the ORIGINAL TiMi Gateway & WebSocket state; it does not open a second
 * WebSocket or replace the original signal engine. No fabricated candles.
 * Apache 2.0 dependency: https://github.com/klinecharts/KLineChart
 */
(() => {
  'use strict';
  if (window.__timiKlineInstalled) return;
  window.__timiKlineInstalled = true;

  const CDN = [
    'https://cdn.jsdelivr.net/npm/klinecharts@10.1.0/dist/umd/klinecharts.min.js',
    'https://unpkg.com/klinecharts@10.1.0/dist/umd/klinecharts.min.js'
  ];
  const PERIODS = {
    '1m': { span: 1, type: 'minute' }, '3m': { span: 3, type: 'minute' },
    '5m': { span: 5, type: 'minute' }, '15m': { span: 15, type: 'minute' },
    '30m': { span: 30, type: 'minute' }, '1h': { span: 1, type: 'hour' },
    '4h': { span: 4, type: 'hour' }, '1d': { span: 1, type: 'day' }
  };
  const STEPS = {'1m':60000,'3m':180000,'5m':300000,'15m':900000,
                 '30m':1800000,'1h':3600000,'4h':14400000,'1d':86400000};
  const SYMS = ['BTCUSDT','ETHUSDT','SOLUSDT','BNBUSDT','XRPUSDT','DOGEUSDT','ADAUSDT','AVAXUSDT'];
  const $ = id => document.getElementById(id);
  const num = x => x !== null && x !== undefined && x !== '' && Number.isFinite(Number(x)) ? Number(x) : null;
  const now = () => Date.now();
  const currentSymbol = () => (typeof symbol === 'string' && SYMS.includes(symbol) ? symbol : $('symbol')?.value || 'BTCUSDT');
  const currentTf = () => (typeof tf === 'string' && PERIODS[tf] ? tf : '1m');
  const currentGateway = () => (typeof gateway === 'string' ? gateway : location.origin).replace(/\/$/, '');
  const visible = () => Boolean($('chart')?.classList.contains('active'));
  let chart = null, stage = null, oldCanvas = null, statusEl = null, lastOldDisplay = '';
  let chartSymbol = '', chartTf = '', chartGateway = '', latestBars = [], chartReady = false;
  let restoreLegacy = false, timer = null, pendingLoad = false, fetching = false;
  let lastPlanKey = '', lastSignalKey = '', signalMarkers = new Map(), subscriptions = new Map();
  let lastLiveSignature = '', lastStatus = '';
  let loadSerial = 0; // A stale async response must never reset a newer time frame.
  const requestCache = new Map();

  function status(message, state='normal') {
    if (statusEl && message !== lastStatus) {
      statusEl.textContent = message;
      statusEl.dataset.state = state;
      lastStatus = message;
    }
  }
  function periodKey(p) {
    const unit = String(p?.type || p?.timespan || '').toLowerCase();
    const span = Number(p?.span ?? p?.multiplier ?? 1);
    const suffix = unit.startsWith('min') ? 'm' : unit.startsWith('hour') ? 'h' : unit.startsWith('day') ? 'd' : '';
    const key = String(span) + suffix;
    return PERIODS[key] ? key : null;
  }
  function normalized(c) {
    if (!c || typeof c !== 'object') return null;
    const t = num(c.t ?? c.timestamp), o = num(c.o ?? c.open), h = num(c.h ?? c.high);
    const l = num(c.l ?? c.low), close = num(c.c ?? c.close), v = num(c.v ?? c.volume);
    if (![t,o,h,l,close,v].every(Number.isFinite) || t < 1e11 || o <= 0 ||
        h < Math.max(o,close,l) || l > Math.min(o,close,h) || v < 0) return null;
    return { timestamp:Math.floor(t), open:o, high:h, low:l, close, volume:v };
  }
  function sortedBars(arr) {
    const map = new Map();
    for(const item of Array.isArray(arr) ? arr : []) {
      const bar = normalized(item);
      if (bar) map.set(bar.timestamp, bar);
    }
    return [...map.values()].sort((a,b)=>a.timestamp-b.timestamp);
  }
  function validSymbolInfo(s) {
    const ticker = String(s?.ticker || '').toUpperCase();
    return SYMS.includes(ticker) ? ticker : null;
  }
  function precision(s) { return ['XRPUSDT','DOGEUSDT','ADAUSDT'].includes(s) ? 5 : s === 'AVAXUSDT' ? 3 : 2; }
  function symbolInfo(s) { return {ticker:s, pricePrecision:precision(s),volumePrecision:4}; }
  function gatewayIsSafe() {
    const u = currentGateway();
    try {const parsed=new URL(u);return parsed.protocol==='https:' || (parsed.hostname==='localhost' && parsed.protocol==='http:');}
    catch(_) {return false;}
  }

  // Read historical bars from TiMi's original read-only REST endpoint. One-time
  // initialization only: normal live updates reuse original shared WebSocket.
  async function loadBars(s, p, type, timestamp) {
    if (!SYMS.includes(s) || !PERIODS[p] || !gatewayIsSafe()) throw Error('Geçersiz veri kaynağı');
    if (type && type !== 'init') return []; // TiMi API does not support historic cursor paging.
    // NEVER take an unlabelled shared `candles` array as initial data.
    // When `tf` changes, the original TiMi UI changes `tf` synchronously but
    // replaces `candles` only after its REST call completes. That previous
    // timeframe (or previous symbol) could otherwise be rendered as the new one.
    // The gateway REST response explicitly identifies symbol and interval.
    const key=currentGateway()+'|'+s+'|'+p;
    const cached=requestCache.get(key);
    if(cached && now()-cached.time<10000) return cached.data;
    const controller=new AbortController();
    const timeout=setTimeout(()=>controller.abort(),12000);
    try {
      const url=currentGateway()+'/api/klines/'+encodeURIComponent(s)+'?interval='+encodeURIComponent(p)+'&limit=300';
      const response=await fetch(url,{cache:'no-store',signal:controller.signal});
      if (!response.ok) throw Error('TiMi API: '+response.status);
      const data=await response.json();
      if (data.symbol !== s || data.interval !== p || !Array.isArray(data.candles)) throw Error('Mum yanıtta uyuşmazlık');
      const bars=sortedBars(data.candles);
      requestCache.set(key,{time:now(),data:bars});
      if(requestCache.size>24) requestCache.delete(requestCache.keys().next().value);
      return bars;
    } finally {clearTimeout(timeout);}
  }
  function latestSharedBar(s,p) {
    if (s!==currentSymbol() || p!==currentTf()) return null;
    if (p==='1m') {
      const ready=typeof ws!=='undefined' && ws && ws.readyState===WebSocket.OPEN &&
                  typeof lastKline==='number' && now()-lastKline<6000;
      if (!ready) return null;
      const live = typeof lastLiveCandle!=='undefined' ? normalized(lastLiveCandle) : null;
      return live;
    }
    // Multi-minute bars come from the original TiMi REST refresh; no synthetic
    // intrabar bars are invented from trades.
    const cached=typeof candles!=='undefined'&&Array.isArray(candles) ? candles : [];
    const bars=sortedBars(cached), step=STEPS[p];
    if (bars.length<3) return null;
    const tail=bars.slice(-3);
    // Reject historical bars from the previous timeframe while TiMi is loading.
    if (tail[1].timestamp-tail[0].timestamp!==step ||
        tail[2].timestamp-tail[1].timestamp!==step ||
        tail[2].timestamp%step!==0 ||
        now()-tail[2].timestamp>step*3) return null;
    const last=latestBars.at(-1);
    // Do not deliver an older bar into a series for a newer completed period.
    if (last && tail[2].timestamp<last.timestamp) return null;
    return tail[2];
  }
  function trySubscribe(params) {
    const s=validSymbolInfo(params.symbol),p=periodKey(params.period);
    if (!s || !p) return;
    const key=s+'|'+p;
    subscriptions.get(key)?.();
    let sig='';
    const tick=()=>{
      if (!visible() || !chart || restoreLegacy ||
          currentSymbol()!==s || currentTf()!==p) return;
      const bar=latestSharedBar(s,p);
      if (!bar) return;
      const fingerprint=[bar.timestamp,bar.open,bar.high,bar.low,bar.close,bar.volume].join('|');
      if (fingerprint===sig) return;
      sig=fingerprint;
      try {params.callback(bar);} catch(_) { /* source chart API can reject stale bar */ }
    };
    const id=setInterval(tick,p==='1m'?450:1300);
    subscriptions.set(key,()=>clearInterval(id));
    tick();
  }
  function unsubscribe(params) {
    const key=validSymbolInfo(params.symbol)+'|'+periodKey(params.period);
    subscriptions.get(key)?.();subscriptions.delete(key);
  }
  const loader={
    async getBars({type,timestamp,symbol:s,period:p,callback}) {
      const name=validSymbolInfo(s),timeframe=periodKey(p);
      // Each init has an identity; callbacks from a prior timeframe must NOT
      // clear the new chart. In KLineChart v10 an old init callback would
      // otherwise replace the entire internal bar array.
      if (type!=='init') {
        callback([], {forward:false,backward:false});return;
      }
      const ticket=++loadSerial;
      if (!name || !timeframe) {callback([], {forward:false,backward:false});return;}
      const same=()=>ticket===loadSerial && name===chartSymbol &&
        timeframe===chartTf && currentGateway()===chartGateway;
      try {
        const data=await loadBars(name,timeframe,type,timestamp);
        if (!same()) return; // Obsolete request: don't invoke old chart callback.
        if (data.length<20) throw Error('20 mumdan az veri: '+data.length);
        callback(data,{forward:false,backward:false});
        latestBars=data;
        chartReady=true;
        if (!restoreLegacy) activateChart();
        status('KLineChart · '+name+' · '+timeframe+' · '+data.length+' mum yüklendi','good');
      } catch(e) {
        if (!same()) return;
        chartReady=false;
        callback([], {forward:false,backward:false});
        activateLegacy('KLineChart veri hatası: '+String(e.message||e)+' · Eski grafik açık');
      }
    },
    subscribeBar:trySubscribe,
    unsubscribeBar:unsubscribe
  };

  function activateChart() {
    if (!stage || !oldCanvas) return;
    stage.hidden=false;stage.style.visibility='visible';
    oldCanvas.style.display='none';
    const legacy=$('timiKlineLegacy');if (legacy) legacy.textContent='Eski Grafik';
    try {chart?.resize();} catch(_) {}
  }
  function activateLegacy(message='Eski TiMi grafiği gösteriliyor') {
    if (!stage || !oldCanvas) return;
    stage.hidden=true;stage.style.visibility='hidden';oldCanvas.style.display=lastOldDisplay;
    const legacy=$('timiKlineLegacy');if(legacy)legacy.textContent='Yeni Grafik';
    status(message,'warn');
    try {if(typeof draw==='function')draw();} catch(_) {}
  }
  function toggleLegacy() {
    restoreLegacy=!restoreLegacy;
    if (restoreLegacy || !chartReady) {
      activateLegacy();
      if(!restoreLegacy && chart && !chartReady){
        status('KLineChart verileri yeniden yükleniyor','normal');
        chart.resetData();
      }
    } else {activateChart();status('KLineChart aktif','good');}
  }

  function overlay(options) {
    if(!chart || !chartReady || restoreLegacy) return;
    try {return chart.createOverlay(options);} catch(e) {
      status('Çizim hatası: '+String(e.message||e).slice(0,80),'warn');
      return null;
    }
  }
  function overlayGroupRemove(group) {
    try {chart?.removeOverlay({groupId:group});}catch(_){}
  }
  function paintPlan() {
    const plan=typeof window.timiTpGetPlan==='function' ? window.timiTpGetPlan():null;
    const key=plan&&plan.symbol===chartSymbol&&plan.tf===chartTf ?
      [plan.symbol,plan.tf,plan.createdAt,plan.status,plan.hits?.join(','),plan.entry,plan.sl,plan.targets?.join(',')].join('|') : 'none';
    if(key===lastPlanKey)return;
    lastPlanKey=key;
    overlayGroupRemove('timi-atr-plan');
    if(!plan || key==='none' || !chartReady || restoreLegacy) return;
    const ended=plan.status==='TP3'||plan.status==='SL';
    const ref=latestBars.at(-1)?.timestamp || plan.closedAt || plan.createdAt;
    const values=[['GİRİŞ',plan.entry,'#e2e9f5'],['SL',plan.sl,'#ff6386'],
      ['TP1',plan.targets?.[0],'#22d3a0'],['TP2',plan.targets?.[1],'#22d3a0'],
      ['TP3',plan.targets?.[2],'#22d3a0']];
    for(const [label,value,color] of values) {
      const v=num(value);if(v===null||v<=0)continue;
      const suffix=ended?' (GEÇMİŞ)':'';
      overlay({name:'simpleTag',groupId:'timi-atr-plan',lock:true,
        points:[{timestamp:ref,value:v}],extendData:label+suffix+' '+v.toLocaleString('tr-TR',{maximumFractionDigits:5}),
        styles:{line:{color:ended?'#8793a2':color,size:1,style:'dashed'},
                text:{color:ended?'#9aa8b8':color,size:10}}});
    }
    status(ended?'GEÇMİŞ SENARYO · Teorik '+plan.status+' · Canlı emir yok':
      'TiMi ATR senaryosu · '+plan.direction+' · Giriş/SL/TP çizildi',ended?'warn':'good');
  }
  function safeSignalCandidate() {
    if(chartTf!=='1m' || currentTf()!=='1m' || !visible())return null;
    const a=typeof analysis==='object'&&analysis?analysis:null;
    const age=typeof analysisAt==='number'?now()-analysisAt:Infinity;
    const side=$('timiProDecision')?.textContent?.trim();
    const engine=a?.signal?.decision;
    if (!a || a.symbol!==chartSymbol || age<0 || age>12000 ||
        !a.flow?.fresh || !a.flow?.book_valid ||
        !((engine==='LONG'&&side==='AL') || (engine==='SHORT'&&side==='SAT'))) return null;
    const closeAt=num(a.analysis_candle_closed_at_ms);
    const closed=typeof candles!=='undefined'&&Array.isArray(candles)?
      candles.filter(c=>c?.closed===true && num(c.t)!==null).at(-1):null;
    if (!closed || closeAt===null || num(closed.t)+60000!==closeAt) return null;
    const price=num(closed.c); if(price===null||price<=0)return null;
    return {key:chartSymbol+'|'+closeAt+'|'+engine,
      symbol:chartSymbol,tf:'1m',time:num(closed.t),price,side:engine,observed:now()};
  }
  function paintSignal() {
    const signal=safeSignalCandidate();
    if (signal && signal.key!==lastSignalKey) {
      lastSignalKey=signal.key;
      const seen=signalMarkers.get(chartSymbol)||[];
      if(!seen.some(x=>x.key===signal.key)){
        seen.push(signal);signalMarkers.set(chartSymbol,seen.slice(-40));
        if(chartTf==='1m')drawSignal(signal);
      }
    }
  }
  function drawSignal(signal) {
    overlay({name:'simpleAnnotation',groupId:'timi-signals',lock:true,
      points:[{timestamp:signal.time,value:signal.price}],
      extendData:signal.side==='LONG'?'TiMi AL':'TiMi SAT',
      styles:{line:{color:signal.side==='LONG'?'#22d3a0':'#ff6682',size:2},
              text:{color:signal.side==='LONG'?'#22d3a0':'#ff6682',size:12}}});
  }
  function restoreSignalOverlays() {
    overlayGroupRemove('timi-signals');
    if(chartTf!=='1m')return;
    for(const s of signalMarkers.get(chartSymbol)||[])drawSignal(s);
  }
  function refreshOverlays() {
    if(!chart || !chartReady || restoreLegacy)return;
    paintPlan();paintSignal();
  }
  function updatePairPeriod() {
    if(!chart)return;
    const s=currentSymbol(),p=currentTf(),g=currentGateway();
    if(chartSymbol===s && chartTf===p && chartGateway===g)return;
    const oldS=chartSymbol,oldP=chartTf,oldG=chartGateway;
    chartSymbol=s;chartTf=p;chartGateway=g;chartReady=false;
    latestBars=[];lastPlanKey='';lastSignalKey='';
    // Show the working legacy Canvas until the new period has >=20 real bars.
    // Keep the new chart element sized but invisible during the REST request.
    if(stage && oldCanvas){stage.hidden=false;stage.style.visibility='hidden';
      oldCanvas.style.display=lastOldDisplay;}
    for(const stop of subscriptions.values())stop();subscriptions.clear();
    overlayGroupRemove('timi-atr-plan');overlayGroupRemove('timi-signals');
    status('KLineChart: '+s+' / '+p+' verileri yükleniyor','normal');
    try {
      // setSymbol / setPeriod each triggers a v10 init. Avoid resetting a
      // timeframe twice simply by reassigning an unchanged symbol.
      if(s!==oldS) chart.setSymbol(symbolInfo(s));
      if(p!==oldP) chart.setPeriod(PERIODS[p]);
      if(s===oldS && p===oldP && g!==oldG) chart.resetData();
    } catch(e) {chartReady=false;activateLegacy('Grafik geçiş hatası: '+String(e.message||e));}
    setTimeout(restoreSignalOverlays,1200);
  }

  function onTool(name) {
    if(!chartReady||restoreLegacy){status('Önce yeni grafiğin yüklenmesini bekle','warn');return;}
    if(name==='clear'){
      // Deletes ONLY user's manual overlays. Does not delete TiMi signal/ATR groups.
      overlayGroupRemove('timi-manual');status('Manuel çizimler temizlendi','good');return;
    }
    overlay({name,groupId:'timi-manual',lock:false});
  }
  function addStyles() {
    if($('timiKlineStyles'))return;
    const style=document.createElement('style');style.id='timiKlineStyles';
    style.textContent=`
      .timi-kline-tools{display:flex;flex-wrap:wrap;align-items:center;gap:7px;margin:8px 0 10px}
      .timi-kline-tools button{padding:7px 9px;background:#19304a;color:#e5f1ff;
        border:1px solid #395573;border-radius:9px;font:600 11px system-ui;cursor:pointer}
      .timi-kline-tools button:active{background:#29587b}
      #timiKlineStage{width:100%;height:410px;min-height:310px;border-radius:13px;overflow:hidden;
        background:#0b1a2b;touch-action:pan-y}
      #timiKlineStage[hidden]{display:none!important}
      #timiKlineStatus{flex:1 1 100%;font:11px system-ui;color:#b8cce2;line-height:1.45}
      #timiKlineStatus[data-state="bad"]{color:#ff8299}
      #timiKlineStatus[data-state="warn"]{color:#ffd48b}
      #timiKlineStatus[data-state="good"]{color:#59e6bd}
      @media(max-width:420px){#timiKlineStage{height:355px;min-height:300px}
      .timi-kline-tools button{padding:7px 7px;font-size:10px}}
    `;
    document.head.append(style);
  }
  function buildUI() {
    const holder=$('timiTpChart'),canvas=$('canvas');
    if(!holder||!canvas)return false;
    addStyles();
    const tools=document.createElement('div');tools.id='timiKlineTools';tools.className='timi-kline-tools';
    tools.innerHTML=`<button type="button" id="timiKlineLegacy">Eski Grafik</button>
      <button type="button" data-ktool="segment">↗ Trend</button>
      <button type="button" data-ktool="horizontalStraightLine">━ Yatay</button>
      <button type="button" data-ktool="fibonacciLine">Φ Fibonacci</button>
      <button type="button" data-ktool="clear">✕ Çizimleri Sil</button>
      <span id="timiKlineStatus" role="status" aria-live="polite">KLineChart yükleniyor · Eski grafik korunuyor</span>`;
    holder.before(tools);
    stage=document.createElement('div');stage.id='timiKlineStage';stage.hidden=true;
    holder.insertBefore(stage,canvas);oldCanvas=canvas;
    lastOldDisplay=canvas.style.display||'';
    statusEl=$('timiKlineStatus');
    $('timiKlineLegacy').addEventListener('click',toggleLegacy);
    tools.addEventListener('click',e=>{
      const b=e.target.closest('[data-ktool]');if(b)onTool(b.dataset.ktool);
    });
    $('timeframes')?.addEventListener('click',()=>setTimeout(updatePairPeriod,170));
    $('symbol')?.addEventListener('change',()=>setTimeout(updatePairPeriod,170));
    $('saveGateway')?.addEventListener('click',()=>setTimeout(updatePairPeriod,250));
    document.querySelector('nav [data-page="chart"]')?.addEventListener('click',()=>setTimeout(()=>{
      if(chart){updatePairPeriod();chart.resize();}
    },100));
    return true;
  }
  function inject(src) {
    return new Promise((resolve,reject)=>{
      const tag=document.createElement('script');tag.src=src;tag.async=true;
      tag.onload=()=>resolve(true);tag.onerror=()=>reject(Error('Kütüphane yüklenemedi'));
      document.head.append(tag);
    });
  }
  async function loadLibrary() {
    if(window.klinecharts?.init)return window.klinecharts;
    for(const src of CDN){
      try {await inject(src);if(window.klinecharts?.init)return window.klinecharts;}
      catch(_){}
    }
    throw Error('Grafik kütüphanesine erişilemedi (CDN). Eski grafiği kullan.');
  }
  async function boot() {
    if(!buildUI())return;
    let lib=null;
    try {lib=await loadLibrary();}
    catch(e){restoreLegacy=true;activateLegacy('Yeni grafik açılamadı: '+String(e.message||e));return;}
    // The original TiMi starts in Overview, where #chart has display:none.
    // Defer KLineChart.init until the Grafik tab is actually visible and sized.
    function ensureChart() {
      if(chart||!visible()||!lib?.init)return;
      try {
        stage.hidden=false;stage.style.visibility='hidden';
        chart=lib.init(stage, {styles:'dark',timezone:'Europe/Istanbul',locale:'en-US'});
        if(!chart)throw Error('Grafik oluşturulamadı');
        // KLineChart v10 initialization order is significant: configure the
        // symbol/period FIRST; attach the loader LAST for a single initial
        // request. Otherwise the same chart can be cleared by overlapping init.
        chartSymbol=currentSymbol();chartTf=currentTf();chartGateway=currentGateway();
        chartReady=false;
        chart.setSymbol(symbolInfo(chartSymbol));
        chart.setPeriod(PERIODS[chartTf]);
        try {chart.setStyles({grid:{horizontal:{color:'#243a50'},vertical:{color:'#243a50'}},
          candle:{bar:{upColor:'#22d3a0',downColor:'#ff6682',upWickColor:'#22d3a0',
          downWickColor:'#ff6682',upBorderColor:'#22d3a0',downBorderColor:'#ff6682'}}});}catch(_){}
        try {chart.createIndicator({name:'MA',calcParams:[20,50,200],paneId:'candle_pane'},true);}catch(_){}
        chart.setDataLoader(loader);
      }catch(e){chart=null;restoreLegacy=true;activateLegacy('Yeni grafik açılamadı: '+String(e.message||e));}
    }
    ensureChart();
    timer=setInterval(()=>{
      if(!visible()||restoreLegacy)return;
      ensureChart();
      if(!chart)return;
      updatePairPeriod();refreshOverlays();
    },900);
    window.addEventListener('resize',()=>{if(visible()&&!restoreLegacy)chart?.resize();});
    window.timiKlineDebug=()=>({ready:chartReady,symbol:chartSymbol,tf:chartTf,
      bars:latestBars.length,library:'KLineChart 10.1.0',readOnly:true});
  }
  if(document.readyState==='loading')document.addEventListener('DOMContentLoaded',boot,{once:true});
  else boot();
})();
