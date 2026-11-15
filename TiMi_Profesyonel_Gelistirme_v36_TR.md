# TiMi SCALP PRO — Profesyonel Platform Karşılaştırması ve 20 Geliştirme Fırsatı

**Tarih:** 15 Kasım 2026
**Geliştirici:** Suat DOĞU
**Kapsam:** Android/PWA, salt okunur, Binance Spot + desteklenen Futures kaynakları. Gerçek emir, sinyal arşivi, sonradan isabet takibi veya hesap anahtarı eklenmez.

## Mevcut TiMi'deki doğrulanmış altyapı

- Binance Spot WebSocket canlı işlemler (`@trade`), kısmi 20-kademe depth (`@depth20`) ve 1m mumlar.
- Order Flow PRO 3.0, 15/60/300 sn Delta, fiyat kademeli akış; veri sağlığı ve kaynak kısıtı denetimi.
- SMC PRO 3.5: kapanmış mum BOS/CHoCH, FVG, OB kalite, sweep, 1m/5m/15m; KLineChart.
- Futures ve ML modülleri mevcut; kaynak yoksa veriler uydurulmamalı ve kapsamları ayrıştırılmalı.

## Bugün eklenen SMC 3.6 kullanılabilirlik güncellemesi

- MTF yönleri 1m/5m/15m ayrı yazılır. 3/3 veri geçerli ama zıt yön varsa `YÖN ÇATIŞMASI` görünür; veri eksikse ayrı uyarı.
- FVG/OB/Sweep çizgilerinin fiyat seviyeleri aynı kalır; sağ yığılmış `simpleTag` etiketleri kaldırılır, grafiğin üstünde en güncel aday başına bir kısa `K` kalite etiketi çıkar.
- 100/100 puanı yalnız kural tabanlı bölge kalitesidir; doğrulanmış başarı yüzdesi, tahmin olasılığı veya emir önerisi değildir.

## 20 uygulanabilir geliştirme (öncelik sırasıyla)

| Sıra | Geliştirme | TiMi'ye katkısı | Gereken veri/önkoşul | Durum |
|---|---|---|---|---|
| 1 | Çakışmasız SMC etiket/katman sistemi | Grafik daha okunaklı | Mevcut KLineChart/SMC | **SMC 3.6 paketinde** |
| 2 | Belirgin MTF yön çatışması ve eksik veri rozeti | Yanlış 3/3 yorumunu önler | Mevcut SMC 1/5/15m | **SMC 3.6 paketinde** |
| 3 | Kaynak kökeni/güncellik matrisi | `Hangi borsa? Kaç sn gecikme?` | Gateway health | Öncelikli |
| 4 | İşlem kaynaklı gerçek Footprint (agresif alış/satış) | Fiyat seviyelerinde baskı | Eksiksiz gözlemlenen trade akışı | Öncelikli, *yalnız canlı pencere* |
| 5 | Gözlemlenen seans CVD + sıfırlama | Delta ile fiyat çelişkisi | Trade zaman/dedup | Öncelikli, kalıcı kayıt yok |
| 6 | Gerçek işlem hacmi Volume Profile/POC | İşlemin yoğunlaştığı fiyat aralığı | Canlı trade dağılımı | Orta |
| 7 | Session / Anchored VWAP | Ortalama işlemlenen fiyat referansı | Doğru zaman/quote/volume | Orta |
| 8 | Tarih içinde 20-kademe likidite ısı haritası | Görünen pasif likidite değişimi | Snapshot zaman matrisi | Orta; 20 seviyenin ötesi yok |
| 9 | Büyük işlem baloncukları | Gerçekleşmiş yüksek notional işlemler | Trade ve sembol bazlı eşik | Orta |
| 10 | Emir defteri yenilenme/çekilme adayları | Likidite davranışı | Aynı fiyat seviyesi güncellemeleri | Mevcut motor güçlendirme; spoof/iceberg kanıtı değil |
| 11 | Cross-venue Spot fiyat farkı | Binance/OKX/Bitget uyuşmazlığı | Ayrı piyasa tanımları ve zaman eşleme | Orta |
| 12 | Futures funding/OI/basis güvenilirlik kontrolü | Spekülasyon baskısını daha iyi yorumlama | Borsa bazında birim, timestamp, sözleşme | Öncelikli, erişim kısıtı olabilir |
| 13 | Spot vs Futures yön ayrışması | Spot işlemler ile türev baskısı ayrımı | Senkronize zaman ve piyasa | Orta |
| 14 | SMC bölgesi yaşam döngüsü | OB/FVG ilk temas, dolum, iptal | Kapanmış mum geçmişi | Mevcut 3.5 genişletme |
| 15 | Likidite sweep + gerçek trade tepki kontrolü | Tek başına fitil yerine teyit | Spot trade + kapanmış mum | Orta |
| 16 | İşlem maliyet/risk kalkanı | Spread, kayma, ücret uyarısı | Anlık spread ve varsayımlar | Öncelikli; emir yok |
| 17 | Alarm eşik ve cooldown (sadece kullanıcı açıkken) | Ekranda uyarı selini önler | Canlı olay ve zaman kontrolü | Orta; bildirim opt-in |
| 18 | Grafik katman yöneticisi / tek tuşla sade görünüm | Mobil ekranı kalabalıklaştırmaz | KLineChart çizim grupları | Orta |
| 19 | Veri bağlantısı kesinti/fallback testleri | Sahte canlı etiketleri önler | WS bağlantı-durum makineleri | Öncelikli |
| 20 | Ayrı araştırma modunda OOS/walk-forward duyarlılık testleri | Parametre aşırı uyumunu azaltır | Kullanıcının ayrıca sağladığı tarihsel veri | Sonraki faz; **sinyal loglama yok** |

## En yüksek getirili sıradaki üç proje

1. **Flow Verisi Doğruluğu v3.1:** Trade/delta kaynak kökeni, kapsama süresi, kayıp gözlem, yeniden bağlanma; borsa farklılıklarını karıştırmama.
2. **Footprint + CVD v1.0:** Kısmi canlı oturumda gerçekleşen işlemlerden; eksik geçmişi asla sıfır veya tam grafik gibi göstermeme.
3. **Likidite Heatmap Lite:** 20 seviye kısmi defterde gözlemlenen değişimi görselleştir; tarihsel tam L2/L3 ve tasfiye verisi varmış gibi iddia etme.

## İncelenen somut kaynaklar

- Flowsurface — https://github.com/flowsurface-rs/flowsurface — DOM/Footprint/heatmap/CVD; GPL-3.0-or-later.
- EdgeDepth — https://github.com/edgedepthhq/edgedepth-terminal — CVD, piyasa yeniden oynatma, veri kapsamı; AGPL-3.0 (gateway MIT).
- Depth — https://github.com/ChloePike/depth — çoklu borsa endeksi, alarm, vadeli veri, ortak görünüm; GPLv3.
- FlowMap — https://github.com/nazmiefearmutcu/flowmap — L2 heatmap ile veri kaynağı ayrımı.
- LuxAlgo Order Flow — https://docs.luxalgo.com/platform/charts/order-flow/introduction — gerçek trade-derived footprint, eksik veri boşluğu.
- GoCharting — https://gocharting.com/docs — footprint, VWAP, market/volume profile, simülatör.
- NautilusTrader — https://github.com/nautechsystems/nautilus_trader — L1/L2/L3 veri ayrımı ve deterministik analiz mimarisi.
- Reddit /r/algotrading (2026) — https://www.reddit.com/r/algotrading/comments/1xko987/what_is_your_simplest_useful_check_against/ — parametre kararlılığı ve kör test tartışması.
- Reddit /r/algotrading (2026) — https://www.reddit.com/r/algotrading/comments/1t70xws/problem_with_overfitting/ — walk-forward, duyarlılık ve maliyet uyarıları.

**Lisans notu:** Yukarıdaki projelerden GPL/AGPL kod kopyalanmadı; yalnızca tasarım/analiz fikirleri referans alındı. Bir bağımlılık veya kod taşınacaksa lisansı ayrıca doğrulanmalıdır.

**Ölçüm uyarısı:** Gerçek trade ile mumdan tahmin edilen buy/sell hacmini, kısmi depth ile tam L2 order book'u, basit ısı haritasıyla gerçek tasfiye haritasını karıştırmıyoruz. Aday/puanlar kazanma olasılığı değildir.
