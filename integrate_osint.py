#!/usr/bin/env python3
'''TiMi SCALP PRO'ya OSINT sekmesini ekler. Var olan kodları silmez.''' 
from pathlib import Path
import re, sys


def patch(root: Path) -> None:
    main_file = root / 'main.py'
    index_file = root / 'index.html'
    for path in (main_file, index_file, root / 'timi_osint.py', root / 'osint_panel.html'):
        if not path.is_file():
            raise RuntimeError('Dosya bulunamadı: ' + str(path))

    py = main_file.read_text(encoding='utf-8')
    html = index_file.read_text(encoding='utf-8')
    if 'app = FastAPI(' not in py:
        raise RuntimeError('main.py beklenen FastAPI uygulamasını içermiyor')
    if 'nav class="foot"' not in html or 'function setPage(p)' not in html:
        raise RuntimeError('index.html TiMi SCALP PRO sürümüyle eşleşmiyor. Değişiklik yapılmadı.')
    if 'timi_osint' not in py:
        py += '''\n# TiMi Blockchain OSINT — salt okunur; mevcut canlı veri yolları korunur.\nfrom timi_osint import install_timi_osint\ninstall_timi_osint(app)\n'''
    if 'data-page="osint"' not in html:
        nav='<button data-page="more">'
        if nav not in html:
            raise RuntimeError('Menü düğmesi bulunamadı')
        html=html.replace(nav, '<button data-page="osint">◇<div>OSINT</div></button>'+nav,1)
    if 'id="osint" class="section"' not in html:
        sec='<section id="more" class="section">'
        if sec not in html:
            raise RuntimeError('Menü bölümü bulunamadı')
        insert='''<section id="osint" class="section" aria-label="Blockchain OSINT">
<div class="card" style="padding:12px;margin-bottom:12px">
 <div class="row" style="align-items:center"><h3>◇ Blockchain OSINT</h3><span class="badge">SALT OKUNUR</span></div>
 <p class="note">Bitcoin, Ethereum ve Base cüzdan/işlem araştırması. Canlı fiyat motorundan bağımsız sorgular; aynı TiMi adresi üzerinden çalışır.</p>
</div>
<iframe id="timiOsintFrame" title="TiMi Blockchain OSINT" src="/osint/panel" loading="lazy" style="display:block;width:100%;min-height:800px;height:900px;border:0;border-radius:12px;background:#07121e" referrerpolicy="no-referrer"></iframe>
</section>
'''
        html=html.replace(sec, insert+sec,1)
    marker='<!-- TIMI_OSINT_FRAME_BRIDGE_V02 -->'
    if marker not in html:
        bridge='''
<!-- TIMI_OSINT_FRAME_BRIDGE_V02 -->
<script>(function(){'use strict';
window.addEventListener('message',function(e){
 if(e.origin!==window.location.origin)return;
 var frame=document.getElementById('timiOsintFrame');
 if(!frame||e.source!==frame.contentWindow||!e.data||e.data.source!=='timi-osint-panel')return;
 var h=Number(e.data.height);if(Number.isFinite(h)&&h>=560&&h<=14000)frame.style.height=h+'px';
});
})();</script>
'''
        html=html.replace('</body>',bridge+'</body>',1)
    # Validate all replacements before modifying files.
    assert 'install_timi_osint(app)' in py
    assert 'id="osint" class="section"' in html
    assert 'data-page="osint"' in html
    compile(py, str(main_file), 'exec')
    main_file.write_text(py, encoding='utf-8')
    index_file.write_text(html, encoding='utf-8')
    print('OSINT yeni TiMi sekmesine entegre edildi. Mevcut diğer sekmeler ve fiyat kodu korundu.')

if __name__ == '__main__':
    patch(Path(sys.argv[1] if len(sys.argv)>1 else '.').resolve())
