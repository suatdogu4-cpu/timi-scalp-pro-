# TiMi SCALP PRO — 20 geliştirmelik profesyonel ürün planı

**Temel sürüm:** v2.1 (GitHub `suatdogu4-cpu/timi-scalp-pro-`)
**Tasarım ilkesi:** Salt okunur; Android/PWA; dört alt sekme **Özet • Grafik • PRO • Menü**; mevcut canlı Binance Spot/WebSocket, SMC, grafik ve ML modüllerini koru.
**Kesin sınır:** Sinyal kaydı/arşivi, geçmiş sinyalin sonradan başarısını sınama, gerçek emir, sanal emir motoru, borsa işlem anahtarı **yok**. Mevcut istek üzerine mumlarla yapılan araştırma simülasyonu, gerçek TiMi sinyal geçmişi değildir.

## GitHub'da araştırılan başlıca açık kaynak kaynaklar

| Proje | Güçlü alan | Güncel lisans | TiMi'ye uyarlama |
|---|---|---|---|
| [EdgeDepth Terminal](https://github.com/edgedepthhq/edgedepth-terminal) | DOM, depth heatmap, footprint, trade tape | AGPL-3.0 (gateway MIT) | Tasarım ve ölçüm fikirleri; C++ terminali kopyalama yok |
| [Flowsurface](https://github.com/flowsurface-rs/flowsurface) | Çoklu borsa, footprint, likidite ısı haritası | GPL-3.0 | Görsel/mikroyapısal konsept; ayrı masaüstü Rust uygulaması doğrudan entegre edilmez |
| [Cryptofeed](https://github.com/bmoscon/cryptofeed) | Çoklu borsa WS adaptörleri | AGPL-3.0 | Feed adaptörü tasarımı; GPL/AGPL uyumu olmadan kod kopyalama yok |
| [KLineChart](https://github.com/klinecharts/KLineChart) | Mobil, göstergeler, çizim API'si | Apache-2.0 | TiMi'de zaten mevcut, sürüm 10.1 uyumluluğunu koru |
| [pyvsmc](https://github.com/Khaymat/pyvsmc) | BOS, CHoCH, FVG, OB, sweep | MIT | Kapanmış mum temelli SMC doğrulama kuralları |
| [Freqtrade](https://github.com/freqtrade/freqtrade) | Strateji araştırma/test altyapısı | GPL-3.0 | Yalnız istek üzerine simülasyon prensipleri; işlem botu yok |
| [Jesse](https://github.com/jesse-ai/jesse) | Strateji analiz ve gösterge mimarisi | MIT | Göstergelerin ayrık modülleri ve eğitim UI'si |
| [NautilusTrader](https://github.com/nautechsystems/nautilus_trader) | Olay tabanlı veri/kayıt mimarisi | LGPL-3.0 | Zaman damgası / event senkronizasyonu; trade altyapısı yok |
| [CCXT](https://github.com/ccxt/ccxt) | Çoklu borsa API soyutlaması | MIT | Kamuya açık GET adaptörü tasarım fikirleri; gereksiz bağımlılık yok |
| [Depth](https://github.com/ChloePike/depth) | Birleşik market watch ve çizelge | GPL-3.0 | Mobil bilgi mimarisi için ilham; yeni/az olgun proje, doğrudan entegrasyon yok |

**Ayrım:** GitHub'daki bir projenin varlığı veya özellik listesi, TiMi'de hazır olduğu anlamına gelmez. Açık kaynak lisanslarının yükümlülükleri korunmalı. GitHub lisans API'si `NOASSERTION` diyorsa LICENSE metni kontrol edilmeli; örneğin Cryptofeed LICENSE dosyası AGPL-3.0 belirtir.

## Ürün bilgi mimarisi

- **Özet:** tek net karar özeti, son canlı veri durumu, Risk Shield, nedenler; yeni panel gürültüsü minimum.
- **Grafik:** KLineChart 10.1.0 + kapalı mum / BOS / CHoCH / FVG; sade/detaylı ve filtreler.
- **PRO:** üç iç sekme Akış PRO, SMC & MTF, Strateji; Futures ve veri güveni alt panel olarak.
- **Menü:** bağlantı testi, UI ayarları, kaynak şeffaflığı, kullanım kılavuzu.

## 20 geliştirme — sürüm, veri önkoşulu ve kabul kriteri

**Faz A — Veri doğruluğu ve kaynak şeffaflığı (önce kurulan küçük modül)**

1. **Kaynak sağlık matrisi** — Binance spot defter, işlemler, TiMi analiz, 1m mum, Binance Futures, OKX Futures ve tarayıcı WS; *kabul:* 7 satır ve eksik değer için `EKSİK`. **v2.2 ilk paket: uygulandı.**
2. **Zaman damgası ve gecikme** — her kaynağı kendi eşiğiyle denetle, geleceğe ait/yaşlı yanıtı reddet; *kabul:* stale kaynak `GEÇERLİ` olamaz. **v2.2 ilk paket: uygulandı.**
3. **API hata/kısıt göstergesi** — 451, 429, timeout ve kaynak veri eksikliğini birbirinden ayır; *kabul:* Binance 451 durumunda fiyat `0` gösterilmez. **v2.2 ilk paket: 451 görünür; daha ayrıntılı sınıflama sonraki faz.**
4. **Kapalı mum veri bütünlüğü** — en az 70 adet mum, kapanış +2,5 sn, saat sıralaması ve boşluk denetimi; *kabul:* eksik geçmiş 1m `EKSİK`, mevcut sinyal motoruna dokunulmaz. **v2.2 ilk paket: tazelik ve miktar; tam gap denetimi sonraki faz.**
5. **Manuel teşhis ve tazelik koruması** — panel açılınca/yenileyle kaynakları GET ile denetle; 2,5 sn throttling, eşzamanlı sorgu kilidi ve parite değişiminde eski yanıtı bırak; *kabul:* veri kaydı yok. **v2.2 ilk paket: uygulandı.**

**Faz B — Gerçek Order Flow (v2.3)**

6. **Footprint adayı: fiyat kademesinde gerçekleşen işlem kümeleri** — gerçek trade akışının fiyat bazlı agregasyonu; *veri:* kısa süreli price/qty/side, sürekli işlem akışı. *Kabul:* emir defteri miktarı ile gerçekleşen işlemler karıştırılmaz.
7. **15/60/300 sn Delta karşılaştırması** — pencere tamamlanmamışsa güvenli etiket; *veri:* `/api/v2/flow`; *kabul:* eksik pencere `YETERSİZ`, başarım olasılığı yok.
8. **Likidite çekilme/eklenme gözlemi** — kısmi 20 seviye book snapshot'ları arasında değişim; *veri:* oturumluk kısa buffer; *kabul:* sipariş kimliği / spoofing kesinliği iddia edilmez.
9. **Fiyat–Delta uyuşmazlık açıklaması** — fiyat artarken gerçek işlemlerin net satıcı olması vb.; *kabul:* zamansal eşleme ve en az örnek eşiği.

**Faz C — SMC 3.5 (v2.4)**

10. **OB adayları kalite denetimi** — onaylı swing kırılımına bağlı OB, zayıf/güçlü ayrımı; *kabul:* aday ile onaylı işaret ayrı.
11. **FVG dolum / geçersizleşme açıklaması** — grafikte yalnız son ilgili bölgeler; *kabul:* `AKTİF/KISMİ/DOLDU/GEÇERSİZ` ayrımı.
12. **Likidite süpürmesi ve EQH/EQL filtresi** — ATR tabanlı tolerans ve kapanış; *kabul:* gelecek mum verisiyle geçmişe çizim yok.
13. **MTF yapı uyumu güveni** — 1m/5m/15m son kapalı mum ile uyum; *kabul:* stale herhangi TF için 3/3 başarı etiketi üretilemez.

**Faz D — Karar kalitesi ve risk (v2.5)**

14. **Strateji ailesi bağımlılığı** — trend/EMA/MACD benzer sinyallerine aynı oyları ayrı ayrı sayma; *kabul:* ağırlıklar açıklanabilir, olasılık olarak sunulmaz.
15. **Çelişki gerekçesi** — tape vs book vs SMC vs MTF uyuşmazlığının insan tarafından anlaşılır nedeni; *kabul:* doğru stale veto.
16. **Volatilite rejimi** — ATR ve fiyat yapısı bazlı trend/yatay/şok rejimleri; *kabul:* veri yoksa `BELİRSİZ`.
17. **Ücret + kayma + spread senaryosu** — kullanıcının varsaydığı maliyetle ATR R:R; *kabul:* varsayımsal plan, emir gönderimi yok.

**Faz E — Vadeli veriler ve UX (v2.6)**

18. **OI birimi / funding zamanı normalizasyonu** — Binance kontrat/miktar ve OKX USD OI ayrımı; *kabul:* tutarları doğrudan aynı sayıyla karşılaştırmama.
19. **Futures erişilebilirlik/fallback stratejisi** — Binance HTTP 451 olduğunda OKX'i 'başka kaynak' olarak göster, eksik alanı türetme; *kabul:* kaynak ve zaman etiketleri.
20. **Mobil profesyonel kullanılabilirlik** — kompakt/detaylı, katlanır paneller, erişilebilir renkler, yönlü navigasyon, bağlı verinin açıkça açıklanması; *kabul:* 360 px taşma yok ve dokunma hedefleri en az 40px.

## İlk entegrasyon v2.2

Bu paket yalnızca **Faz A'nın** kaynak güvenini gösteren bölümünü aktif olarak ekler. `index.html` içine salt okunur JavaScript ve CSS eklenir. `main.py`, modüller, ML ve veri modeli değişmez. PRO başlığının hemen altında katlanabilir **Veri Güven Merkezi** bulunur; açılana kadar ek sorgu yapılmaz. Veriler yalnız kullanıcının talebiyle alınır ve RAM'de bile kalıcı kayıt olarak saklanmaz.

### Test kabul listesi

1. Her yeni sembolde eski yanıt yeni sembolü ezmesin.
2. HTTP 451 geldiğinde Binance Futures `EKSİK`, OKX erişilebiliyorsa `GEÇERLİ` görünsün.
3. 1m son mum açıksa son kapanmış mum doğru seçilsin.
4. WebSocket kopuksa tüm kaynaklar otomatik 'canlı' sayılmasın.
5. API kısmi yanıtlarında `NaN`, sahte `0`, yüzde başarı iddiası olmasın.
6. Mevcut Özet/Grafik/PRO/Menü, eski UI v2.1 ve ML paneli yerinde kalsın.
7. Yeni düğmeler gerçek veri sorgusu başlatsın; ağ başarısızlığını kullanıcıya bildirsin.
8. Sinyal kaydı, geçmiş başarı analizi, emir API'si veya işlem anahtarı bulunmasın.
