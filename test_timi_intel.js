'use strict';
const A=require('node:assert/strict'),core=require('./timi_intel_core.js');
const now=Date.UTC(2026,10,15,12,0,0);
function series(n=299,tf='1m',direction=1){const step=core.STEPS[tf],start=now-(n+2)*step;return Array.from({length:n},(_,i)=>{const o=100+(i*direction*.02)+Math.sin(i/5)*.4,c=o+direction*.012+.03*Math.sin(i),h=Math.max(o,c)+.13,l=Math.min(o,c)-.13;return {t:start+i*step,o,h,l,c,v:150+Math.round(40*Math.sin(i)**2),closed:true};});}
const raw=series(),one=core.bars(raw,'1m',now);A.equal(one.ok,true);A.equal(one.data.length,299);
const future=series(120).map(x=>({...x}));future.at(-1).closed=false;const safe=core.bars(future,'1m',now);A.equal(safe.data.length,119);
const gapped=series(120);gapped.splice(59,1);const cut=core.bars(gapped,'1m',now);A.equal(cut.gaps,1);A.equal(cut.data.length,60);A.equal(cut.ok,false);
A.equal(core.regime(one.data).ready,true);
A.equal(core.smc(one.data).ready,true);
A.equal(core.breakout(one.data).ready,true);
A.equal(core.vwap(one.data,150).ready,true);
A.equal(core.volumeProfile([]).ready,false);
const tr=Array.from({length:40},(_,i)=>({t:now-40000+i*1000,p:100+i*.002,q:1,aggressor:i%2?'BUY':'SELL'}));const flow=core.analyzeFlow(tr,now);A.equal(flow.ready,true);A.equal(flow.cvd,0);
A.equal(core.depthProxy({bids:[[100,'2']],asks:[[101,'1']]}).isOFI,false);
A.equal(core.futures(100,{price:101,source:'test',timestamp:now-200000},now).ready,false);
A.equal(core.futures(100,{price:101,source:'test',timestamp:now-1000},now).ready,true);
for(const s of ['MTF Trend','SMC Zinciri','Breakout','Birleşik']){const r=core.backtest(one.data,s);A.equal(r.strategy,s);if(r.ready){A.ok(r.winRate>=0&&r.winRate<=1);A.equal(r.entryConvention,'next_open_after_closed_signal');}}
const m=core.trainMeta(one.data);A.ok(typeof m.ready==='boolean');if(m.ready)A.ok(m.brier>=0&&m.brier<=1);
const no=core.decision({ready:false},{ready:true},{ready:true},null,null);A.equal(no.direction,'BEKLE');
const withoutFlow=core.decision({ready:true,kind:'TREND',direction:'LONG'},{ready:true,chain:true,direction:'LONG'},{ready:true,confirmed:'LONG',failed:'WAIT'},null,null);
A.equal(withoutFlow.direction,'BEKLE');
A.equal(core.futures(100,{price:101,source:'test',timestamp:now-1000,spotTimestamp:now-50000},now).ready,false);
A.ok(!JSON.stringify(core).includes('PRIVATE_KEY'));
console.log('PASS: closed-only normalization, gaps, indicator, SMC, breakout, VWAP, real-trade flow, fake-OFI guard, Futures stale guard, historical tests, ML gate, WAIT veto');
