/* TiMi Product Experience v2.1 — presentation/navigation only.
 * No market API requests, historical logging, trading or chart calculations.
 * UI preferences are local on this phone and contain NO market/signal data.
 */
(function(root){
'use strict';
if(!root || !root.document || root.__timiUiV21Installed)return;
const d=root.document;
root.__timiUiV21Installed=true;
const prefKey='timi_ui_v21_preferences';
let prefs={density:'compact',contrast:false};
try{const p=JSON.parse(root.localStorage.getItem(prefKey)||'null');
  if(p && (p.density==='relaxed'||p.density==='compact') && typeof p.contrast==='boolean')prefs=p;
}catch(_){}
const get=id=>d.getElementById(id);
const cssClass=(name,on)=>d.body.classList.toggle(name,Boolean(on));
function persist(){try{root.localStorage.setItem(prefKey,JSON.stringify(prefs));}catch(_){}}
function syncPrefs(){cssClass('timi-ui-relaxed',prefs.density==='relaxed');
  cssClass('timi-ui-contrast',prefs.contrast);
  for(const btn of d.querySelectorAll('[data-timi-ui-density]'))btn.setAttribute('aria-pressed',String(btn.dataset.timiUiDensity===prefs.density));
  const c=get('timiUiContrast');if(c)c.setAttribute('aria-pressed',String(prefs.contrast));
}
function selectedPage(){return [...d.querySelectorAll('main>.section')].find(s=>s.classList.contains('active'))?.id||'overview';}
function syncPage(){const id=selectedPage();cssClass('timi-ui-subpage',id!=='overview');
  for(const btn of d.querySelectorAll('nav.foot button[data-page]')){
    if(btn.dataset.page===id)btn.setAttribute('aria-current','page');
    else btn.removeAttribute('aria-current');
  }
}
function navigate(page){
  const btn=d.querySelector('nav.foot button[data-page="'+page+'"]');
  if(btn){btn.click();return true;}
  return false;
}
function createControls(){
  const header=d.querySelector('header'),health=get('health');
  if(!header||!health||get('timiUiSettingsButton'))return;
  const actions=d.createElement('div');actions.className='timi-ui-header-actions';
  const button=d.createElement('button');button.type='button';button.id='timiUiSettingsButton';
  button.setAttribute('aria-label','Görünüm ayarları');button.setAttribute('title','Görünüm ayarları');
  button.setAttribute('aria-controls','timiUiPanel');button.setAttribute('aria-expanded','false');
  button.textContent='⚙';
  health.parentNode.insertBefore(actions,health);actions.append(health,button);
  const panel=d.createElement('div');panel.id='timiUiPanel';panel.className='timi-ui-control-panel';
  panel.hidden=true;panel.setAttribute('role','dialog');panel.setAttribute('aria-label','Görünüm ayarları');
  panel.innerHTML='<h3>Görünüm ayarları</h3><p>Yalnızca tasarım seçiminiz bu cihazda saklanır. Sinyal veya işlem kaydı yapılmaz.</p>'+
    '<div class="timi-ui-switches">'+
    '<button type="button" data-timi-ui-density="compact">Kompakt</button>'+
    '<button type="button" data-timi-ui-density="relaxed">Rahat</button>'+
    '<button type="button" id="timiUiContrast" aria-pressed="false">Yüksek kontrast</button>'+
    '</div><button type="button" class="timi-ui-close" id="timiUiClose">Kapat</button>';
  d.body.append(panel);
  function toggle(open){panel.hidden=!open;button.setAttribute('aria-expanded',String(open));
    if(open)get('timiUiClose')?.focus();else button.focus();}
  button.addEventListener('click',()=>toggle(panel.hidden));
  get('timiUiClose').addEventListener('click',()=>toggle(false));
  panel.addEventListener('click',e=>{
    const target=e.target.closest('button[data-timi-ui-density]');
    if(target){prefs.density=target.dataset.timiUiDensity;persist();syncPrefs();}
    if(e.target.id==='timiUiContrast'){prefs.contrast=!prefs.contrast;persist();syncPrefs();}
  });
  d.addEventListener('keydown',e=>{if(e.key==='Escape'&&!panel.hidden)toggle(false);});
  d.addEventListener('pointerdown',e=>{if(!panel.hidden&&!panel.contains(e.target)&&!actions.contains(e.target)){
    panel.hidden=true;button.setAttribute('aria-expanded','false');
  }});
  syncPrefs();
}
function createQuickNav(){
  const overview=get('overview');if(!overview||get('timiUiQuickNav'))return;
  const nav=d.createElement('div');nav.id='timiUiQuickNav';
  const links=[['📈','Grafik','chart'],['◈','PRO analiz','proLab'],['☰','Ayarlar','more']];
  for(const [icon,label,page] of links){const b=d.createElement('button');b.type='button';
    b.textContent=icon+' '+label;b.setAttribute('aria-label',label+' sekmesini aç');
    b.addEventListener('click',()=>navigate(page));nav.append(b);}
  overview.prepend(nav);
}
function boot(){
  const header=d.querySelector('header'),main=d.querySelector('main');
  if(!header||!main||!get('overview')||!get('chart')||!get('more')){
    root.__timiUiV21Installed=false;return;
  }
  d.body.classList.add('timi-ui-v21');
  const subtitle=header.querySelector('div:first-child>small');
  if(subtitle)subtitle.textContent='Suat DOĞU · Profesyonel piyasa analizi · Emir yok';
  const symbol=get('symbol');if(symbol)symbol.setAttribute('aria-label','Kripto para paritesi seçimi');
  createControls();createQuickNav();syncPage();
  const observer=new MutationObserver(()=>syncPage());
  for(const page of main.querySelectorAll('.section'))observer.observe(page,{attributes:true,attributeFilter:['class']});
  for(const btn of d.querySelectorAll('nav.foot button[data-page]')){
    btn.addEventListener('click',()=>{
      root.requestAnimationFrame(syncPage);
      root.scrollTo({top:0,behavior:'auto'});
    });
  }
  root.timiUiV21Debug=()=>({version:'2.1.0',readOnly:true,page:selectedPage(),density:prefs.density,
    contrast:prefs.contrast,hasQuickNav:!!get('timiUiQuickNav'),tradeApiCalls:0,storesSignals:false});
}
if(d.readyState==='loading')d.addEventListener('DOMContentLoaded',boot,{once:true});else boot();
})(typeof window!=='undefined'?window:null);
