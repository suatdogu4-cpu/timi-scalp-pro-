/* Tests are entirely synthetic. No network and no external trading orders. */
'use strict';
const assert=require('node:assert/strict');
const S=require('./timi_smc_pro_v35.js');
const core=require('./timi_v20_core.js');
const old=require('./timi_smc_v12.js');
const now=Date.now(), step=60000;
const t0=Math.floor(now/step)*step-123*step;
function make(n=122,tf='1m',baseTime=t0){
  const delta=S.STEPS[tf];const rows=[];
  for(let i=0;i<n;i++){
    const trend=100+i*.16, pull=4*Math.sin(i*.17);
    const o=trend+pull,c=o+(i%5===0?-1:1.1),hi=Math.max(o,c)+.6,lo=Math.min(o,c)-.5;
    rows.push({t:baseTime+i*delta,o,h:hi,l:lo,c,v:10+i%11,closed:true});
  }
  return rows;
}
function test(label,fn){try{fn();console.log('PASS',label)}catch(e){console.error('FAIL',label,e.message);throw e}}
const rows=make();
test('normalize excludes forming/open bars and duplicates',()=>{
  const malformed=[...rows, {...rows.at(-1),c:rows.at(-1).c+1},
    {t:now-step+1000,o:100,h:101,l:99,c:100,v:1,closed:false},
    {t:now,o:100,h:101,l:99,c:100,v:1,closed:true}];
  const v=S.normalize(malformed,'1m',now);
  assert.equal(v.bars.length,122);
  assert(v.invalid>=2);
  assert.equal(v.bars.at(-1).t,rows.at(-1).t);
});
test('missing candle causes contiguous-segment reset',()=>{
  const gap=[...rows.slice(0,75),...rows.slice(76)];
  const z=S.normalize(gap,'1m',now);
  assert(z.gaps>=1);assert(z.bars.length<70);
});
test('old engine compatibility: valid 1m',()=>{
  const result=S.analyze(rows,'1m',{smc:old,core},now);
  assert.equal(result.ready,true);
  assert.equal(result.count,122);
  assert.equal(result.read_only,true);
  assert(Array.isArray(result.activeFVG));
  assert(Array.isArray(result.activeOB));
  assert(Array.isArray(result.sweeps));
});
test('engine absence is marked missing, never manufactured',()=>{
  const r=S.analyze(rows,'1m',{},now);
  assert.equal(r.ready,false);assert.match(r.reason,/motoru eksik/);
});
test('stale candles are blocked',()=>{
  const r=S.analyze(make(122,'1m',t0-4*step),'1m',{smc:old,core},now);
  assert.equal(r.ready,false);assert.match(r.reason,/gecikmiş/);
});
test('OB invalidation is never rated high',()=>{
  const bars=rows.map(x=>({...x}));
  const invalid=S.assessOB({lower:102,upper:105,createdAt:bars[100].t,sourceAt:bars[95].t,status:'GECERSIZ',side:'LONG'},bars,2,'1m');
  assert(invalid);assert(invalid.score<=20);assert.equal(invalid.status,'GEÇERSİZ');
});
test('FVG filled zone cannot receive high confidence',()=>{
  const r=S.assessFVG({id:'z',lower:102,upper:104,side:'BULL',status:'DOLDU',filledPct:100,createdAt:rows[105].t},rows,2,'1m',[]);
  assert(r);assert.equal(r.status,'DOLDU');assert(r.score<=20);
});
test('sweep classification respects direction and close reclaim',()=>{
  const bar=rows[115];const lv=bar.h-.2;
  const r=S.assessSweep({side:'UPPER',price:lv,time:bar.t},rows,2,'1m');
  assert.equal(r.side,'SHORT');assert.equal(typeof r.score,'number');
});
test('MTF only aligns for three valid, non-neutral frames',()=>{
 const valid={ready:true,direction:'SHORT'};
 assert.equal(S.mtf({'1m':valid,'5m':valid,'15m':valid}).aligned,true);
 assert.equal(S.mtf({'1m':valid,'5m':valid,'15m':{ready:true,direction:'WAIT'}}).aligned,false);
 assert.equal(S.mtf({'1m':valid,'5m':valid}).aligned,false);
});
test('does not declare archives or order execution',()=>{
 const s=require('fs').readFileSync(require('path').join(__dirname,'timi_smc_pro_v35.js'),'utf8');
 assert(!s.includes('localStorage.setItem'));
 assert(!s.includes('indexedDB'));
 assert(!s.includes("method:'POST'"));
 assert(!s.includes('createOrder('));
});
console.log('PASS ALL 10 SMC PRO 3.5 TESTS');
