/* TiMi KLineChart v1.2 — SMC BOS/CHoCH and FVG drawings.
 * Developer: Suat DOGU. Visualization-only, Binance Spot read-only.
 * Depends on the KLineChart v1.1 bridge installed by the accompanying workflow.
 * All decisions remain with the existing TiMi rule engine. No order calls.
 */
(function (root) {
  'use strict';
  const STEP = {'1m':60000,'3m':180000,'5m':300000,'15m':900000,
    '30m':1800000,'1h':3600000,'4h':14400000,'1d':86400000};
  const LEFT = 2, RIGHT = 2;
  const clamp = (v,min,max) => Math.max(min,Math.min(max,v));
  const finite = v => typeof v === 'number' && Number.isFinite(v);
  const getNum = v => (v===null||v===undefined||v==='')?null:Number(v);

  // No forward-looking swing detection. Pivots are accepted only after
  // RIGHT future bars have actually CLOSED. Gaps reset structure analysis.
  function analyzeClosedBars(raw, tf) {
    const step=STEP[tf];
    if (!step) return {breaks:[],fvgs:[],lastSwingHigh:null,lastSwingLow:null,count:0};
    const dedup=new Map();
    for (const b of Array.isArray(raw)?raw:[]) {
      const t=getNum(b.timestamp??b.t),o=getNum(b.open??b.o),
        h=getNum(b.high??b.h),l=getNum(b.low??b.l),
        c=getNum(b.close??b.c),v=getNum(b.volume??b.v??0);
      if (![t,o,h,l,c,v].every(Number.isFinite) || t<1e11 || o<=0 ||
          h<Math.max(o,c,l) || l>Math.min(o,c,h) || v<0) continue;
      dedup.set(t,{t,o,h,l,c,v});
    }
    const all=[...dedup.values()].sort((a,b)=>a.t-b.t);
    const breaks=[],fvgs=[];
    let high=null,low=null,lastHigh=null,lastLow=null;
    let lastDirection=null,brokenHigh=null,brokenLow=null;
    let trBuffer=[],segmentStart=0;
    const sameStep=(a,b)=>b.t-a.t===step;
    for(let j=0;j<all.length;j++) {
      const b=all[j],prev=all[j-1];
      if(prev && !sameStep(prev,b)) {
        high=low=lastHigh=lastLow=null;
        lastDirection=brokenHigh=brokenLow=null;
        trBuffer=[];segmentStart=j;
      }
      if(prev && sameStep(prev,b)) {
        const tr=Math.max(b.h-b.l,Math.abs(b.h-prev.c),Math.abs(b.l-prev.c));
        trBuffer.push(tr);
        if(trBuffer.length>14)trBuffer.shift();
      }
      const atr=trBuffer.length ? trBuffer.reduce((s,x)=>s+x,0)/trBuffer.length : 0;
      const pivotIndex=j-RIGHT;
      if(pivotIndex>=segmentStart+LEFT) {
        const p=all[pivotIndex];
        let top=true,bottom=true;
        for(let k=pivotIndex-LEFT;k<=pivotIndex+RIGHT;k++){
          if(k===pivotIndex)continue;
          if(all[k].h>=p.h)top=false;
          if(all[k].l<=p.l)bottom=false;
        }
        if(top) {
          const type=lastHigh ? (p.h>lastHigh.price?'HH':'LH') : 'H';
          high={time:p.t,price:p.h,type,confirmedAt:b.t};
          lastHigh=high;
        }
        if(bottom){
          const type=lastLow?(p.l>lastLow.price?'HL':'LL'):'L';
          low={time:p.t,price:p.l,type,confirmedAt:b.t};
          lastLow=low;
        }
      }
      if(prev && sameStep(prev,b)) {
        // Close crossing only: wick piercing a level is NOT a structure break.
        const buffer=Math.max(b.c*0.000015,atr*0.015);
        const crossedUp=high && brokenHigh!==high.time &&
          prev.c<=high.price+buffer && b.c>high.price+buffer;
        const crossedDown=low && brokenLow!==low.time &&
          prev.c>=low.price-buffer && b.c<low.price-buffer;
        const direction=crossedUp&&!crossedDown?'LONG':crossedDown&&!crossedUp?'SHORT':null;
        if(direction) {
          const level=direction==='LONG'?high:low;
          const type=lastDirection && lastDirection!==direction?'CHoCH':'BOS';
          breaks.push({id:`${tf}|${b.t}|${direction}`,type,direction,
            price:level.price,pivotTime:level.time,time:b.t,
            swing:level.type,confirmed:true,reference:level.confirmedAt});
          if(direction==='LONG')brokenHigh=high.time;else brokenLow=low.time;
          lastDirection=direction;
        }
      }
      // Three consecutive, CLOSED bars only. Do not infer an FVG across gaps.
      if(j>=segmentStart+2 && sameStep(all[j-2],all[j-1]) &&
          sameStep(all[j-1],b)) {
        const first=all[j-2], middle=all[j-1];
        const minGap=Math.max(b.c*0.000015,atr*0.03);
        let side=null,lower=0,upper=0;
        if(b.l-first.h>minGap && middle.c>=middle.o) {
          side='BULL'; lower=first.h; upper=b.l;
        } else if(first.l-b.h>minGap && middle.c<=middle.o) {
          side='BEAR';lower=b.h;upper=first.l;
        }
        if(side)fvgs.push({id:`${tf}|${b.t}|${side}`,side,
          start:first.t,createdAt:b.t,lower,upper,filledPct:0,
          status:'AKTİF',endAt:b.t});
      }
      for(const z of fvgs) {
        if(z.status==='GEÇERSİZ' || z.status==='DOLDU' || b.t<=z.createdAt)continue;
        const gap=z.upper-z.lower;
        if(gap<=0)continue;
        const touched=z.side==='BULL' ? b.l<z.upper : b.h>z.lower;
        if(touched){
          const partial=z.side==='BULL'?(z.upper-b.l)/gap:(b.h-z.lower)/gap;
          z.filledPct=Math.max(z.filledPct,clamp(partial*100,0,100));
          z.status=z.filledPct>=100?'DOLDU':'KISMİ';
        }
        if(z.side==='BULL' && b.c<z.lower)z.status='GEÇERSİZ';
        if(z.side==='BEAR' && b.c>z.upper)z.status='GEÇERSİZ';
        z.endAt=b.t;
      }
    }
    return {breaks:breaks.slice(-40),fvgs:fvgs.slice(-60),
      lastSwingHigh:lastHigh,lastSwingLow:lastLow,count:all.length};
  }

  // Exclude the forming candle by wall-clock boundary. KLine's live bar
  // has no "closed" flag; requiring close time + safety delay prevents repaint.
  function onlyClosed(raw,tf,wallTime) {
    const step=STEP[tf];
    if(!step || !Array.isArray(raw))return [];
    const before=wallTime-step-2500;
    return raw.filter(b=>finite(b?.timestamp) && b.timestamp<=before);
  }
  const exportsObj={analyzeClosedBars,onlyClosed,STEP};
  if(typeof module==='object'&&module.exports)module.exports=exportsObj;
  if(!root||!root.document)return;

  const d=root.document;
  const GROUP_STRUCTURE='timi-smc-v12-break';
  const GROUP_FVG='timi-smc-v12-fvg';
  let settings={structure:true,fvg:true},lastKey='',lastVersion='';
  let ui=null,timer=null,drawErrors=0,chartRef=null,registered=false,lastResult=null;
  try {
    const saved=JSON.parse(root.localStorage.getItem('timi_smc_v12_settings')||'null');
    if(saved && typeof saved.structure==='boolean' && typeof saved.fvg==='boolean')
      settings={structure:saved.structure,fvg:saved.fvg};
  } catch(_){}

  function bridge(){
    const b=root.timiKlineSMCBridge;
    if(!b || typeof b.snapshot!=='function')return null;
    try{return b.snapshot();}catch(_){return null;}
  }
  function clear(chart) {
    for(const groupId of [GROUP_STRUCTURE,GROUP_FVG]) {
      try{chart?.removeOverlay({groupId});}catch(_){}
    }
  }
  function registerFvgOverlay(){
    if(registered || typeof root.klinecharts?.registerOverlay!=='function')return false;
    try{
      root.klinecharts.registerOverlay({
        name:'timiSmcFvgBox',totalStep:3,
        createPointFigures:({coordinates,overlay})=>{
          if(coordinates.length!==2)return [];
          const x=Math.min(coordinates[0].x,coordinates[1].x),
                y=Math.min(coordinates[0].y,coordinates[1].y),
                w=Math.max(2,Math.abs(coordinates[1].x-coordinates[0].x)),
                h=Math.max(2,Math.abs(coordinates[1].y-coordinates[0].y));
          const bull=overlay.extendData?.side==='BULL';
          const c=bull?'#1ee1a7':'#ff6f91';
          const label=String(overlay.extendData?.label||'FVG').slice(0,40);
          return [{
            type:'rect',ignoreEvent:true,
            attrs:{x,y,width:w,height:h},
            styles:{style:'stroke_fill',color:bull?'rgba(30,225,167,.13)':'rgba(255,111,145,.13)',
              borderColor:c,borderSize:1,borderStyle:'dashed',borderRadius:1}
          },{
            type:'text',ignoreEvent:true,
            attrs:{x:x+4,y:y+11,text:label,baseline:'middle'},
            styles:{color:c,size:10,backgroundColor:'transparent',borderColor:'transparent'}
          }];
        }
      });
      registered=true;return true;
    }catch(_){return false;}
  }
  function line(chart,points,color,groupId,dashed=false){
    try{return chart.createOverlay({
      name:'segment',groupId,lock:true,visible:true,points,
      styles:{line:{color,size:1.5,style:dashed?'dashed':'solid'},
        point:{color:'transparent',borderColor:'transparent',radius:0}}
    });}catch(_){drawErrors++;return null;}
  }
  function annotation(chart,item){
    try{
      const text=(item.type==='CHoCH'?'CHoCH':'BOS')+
        (item.direction==='LONG'?' ↑ ':' ↓ ')+item.swing;
      chart.createOverlay({
        name:'simpleAnnotation',groupId:GROUP_STRUCTURE,lock:true,
        points:[{timestamp:item.time,value:item.price}],
        extendData:text,
        styles:{line:{color:item.direction==='LONG'?'#38e5ae':'#ff7895',size:1},
          text:{color:item.direction==='LONG'?'#38e5ae':'#ff7895',size:10}}
      });
    }catch(_){drawErrors++;}
  }
  function applyOverlays(chart,result,bars,tf){
    clear(chart);drawErrors=0;
    if(settings.structure) {
      for(const b of result.breaks.slice(-9)){
        const color=b.type==='CHoCH'?'#ffc569':(b.direction==='LONG'?'#28dba4':'#ff698c');
        line(chart,[{timestamp:b.pivotTime,value:b.price},
          {timestamp:b.time,value:b.price}],color,GROUP_STRUCTURE,b.type==='CHoCH');
        annotation(chart,b);
      }
    }
    if(settings.fvg) {
      const nowEnd=bars.at(-1)?.timestamp;
      const active=result.fvgs.filter(z=>z.status!=='GEÇERSİZ' && z.status!=='DOLDU' &&
        z.endAt>=nowEnd-STEP[tf]*90).slice(-9);
      for(const z of active) {
        const end=Math.max(z.createdAt+STEP[tf],z.endAt);
        const label=(z.side==='BULL'?'↑':'↓')+' FVG · '+
          (z.status==='KISMİ'?Math.round(z.filledPct)+'% dolu':'ADAY');
        try{
          if(registered){
            chart.createOverlay({
              name:'timiSmcFvgBox',groupId:GROUP_FVG,lock:true,
              points:[{timestamp:z.start,value:z.upper},{timestamp:end,value:z.lower}],
              extendData:{side:z.side,label}
            });
          }else{
            const color=z.side==='BULL'?'#25d9a2':'#fa7898';
            line(chart,[{timestamp:z.start,value:z.lower},{timestamp:end,value:z.lower}],
              color,GROUP_FVG,true);
            line(chart,[{timestamp:z.start,value:z.upper},{timestamp:end,value:z.upper}],
              color,GROUP_FVG,true);
          }
        }catch(_){drawErrors++;}
      }
    }
  }
  function setupUI(){
    if(ui)return true;
    const toolbar=d.getElementById('timiKlineTools');
    if(!toolbar)return false;
    const div=d.createElement('div');div.id='timiSmcToolbar';
    div.style.cssText='width:100%;display:flex;gap:7px;align-items:center;flex-wrap:wrap;'+
      'padding:7px 0;border-top:1px solid #314760';
    div.innerHTML='<button type="button" data-smc="structure">BOS / CHoCH</button>'+
      '<button type="button" data-smc="fvg">FVG Bölgeleri</button>'+
      '<span id="timiSmcInfo" style="font:11px system-ui;color:#bbcede;line-height:1.5">'+
      'SMC: kapanmış mum bekleniyor</span>';
    toolbar.append(div);ui=div;
    function updateButtons(){
      for(const prop of ['structure','fvg']){
        const b=div.querySelector('[data-smc="'+prop+'"]');
        if(!b)continue;
        b.setAttribute('aria-pressed',String(settings[prop]));
        b.style.borderColor=settings[prop]?'#22bb91':'#46576e';
        b.style.color=settings[prop]?'#7eeec9':'#a9bad0';
      }
    }
    div.addEventListener('click',event=>{
      const b=event.target.closest('[data-smc]');
      if(!b)return;
      const key=b.dataset.smc;
      if(!(key in settings))return;
      settings[key]=!settings[key];
      try{root.localStorage.setItem('timi_smc_v12_settings',JSON.stringify(settings));}catch(_){}
      lastVersion='';updateButtons();tick(true);
    });
    updateButtons();return true;
  }
  function info(message){
    const node=d.getElementById('timiSmcInfo');
    if(node && node.textContent!==message)node.textContent=message;
  }
  function tick(force=false){
    if(!d.getElementById('chart')?.classList.contains('active'))return;
    if(!setupUI())return;
    const s=bridge();
    if(!s?.chart || !s.ready || s.legacy || !s.symbol || !STEP[s.tf]){
      if(s?.chart && s.chart!==chartRef)clear(chartRef);
      info('SMC: yeni grafik ve kapanmış mumlar bekleniyor');
      return;
    }
    const chart=s.chart;
    if(chart!==chartRef){clear(chartRef);chartRef=chart;lastKey='';lastVersion='';}
    const key=s.symbol+'|'+s.tf+'|'+s.gateway;
    if(key!==lastKey){clear(chart);lastKey=key;lastVersion='';}
    const bars=onlyClosed(s.bars,s.tf,Date.now());
    if(bars.length<15){clear(chart);lastVersion='';info('SMC: en az 15 kapanmış mum bekleniyor');return;}
    const tail=bars.at(-1);
    const signature=[key,bars.length,tail.timestamp,tail.close,tail.high,tail.low,
      settings.structure,settings.fvg].join('|');
    if(!force && signature===lastVersion)return;
    lastVersion=signature;
    if(!registered)registerFvgOverlay();
    const result=analyzeClosedBars(bars,s.tf);
    lastResult=result;
    applyOverlays(chart,result,bars,s.tf);
    const b=result.breaks.at(-1);
    const liveFvg=result.fvgs.filter(z=>z.status==='AKTİF'||z.status==='KISMİ').length;
    info('SMC '+s.tf+' · '+bars.length+' kapalı mum · '+
      (b?b.type+' '+(b.direction==='LONG'?'↑':'↓'):'Yapı kırılımı bekleniyor')+
      ' · '+liveFvg+' FVG'+(drawErrors?' · '+drawErrors+' çizim hatası':''));
  }
  function boot(){
    setupUI();
    timer=setInterval(()=>tick(),1200);
    d.getElementById('timeframes')?.addEventListener('click',()=>{lastVersion='';setTimeout(()=>tick(),350)});
    d.getElementById('symbol')?.addEventListener('change',()=>{lastVersion='';setTimeout(()=>tick(),350)});
    d.querySelector('nav [data-page="chart"]')?.addEventListener('click',()=>setTimeout(()=>tick(),200));
    root.timiSmcV12Debug=()=>({ready:!!bridge()?.ready,symbol:bridge()?.symbol,
      tf:bridge()?.tf,closedBars:lastResult?.count||0,
      breaks:lastResult?.breaks.length||0,fvgs:lastResult?.fvgs.length||0,
      overlaysErrors:drawErrors,version:'1.2.0',readOnly:true});
  }
  if(d.readyState==='loading')d.addEventListener('DOMContentLoaded',boot,{once:true});
  else boot();
})(typeof window!=='undefined'?window:null);
