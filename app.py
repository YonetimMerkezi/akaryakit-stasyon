import logging
import os
from datetime import datetime

from flask import Flask, jsonify, render_template, request
import requests

from fiyat_servisi import cache_temizle, fiyat_cek

# ─── Uygulama ─────────────────────────────────────────────────────────────────
app = Flask(__name__)
logging.basicConfig(
    level=logging.INFO,
    format='%(asctime)s [%(levelname)s] %(name)s: %(message)s',
)
logger = logging.getLogger(__name__)

NOMINATIM_HEADERS = {
    'User-Agent': 'AkaryakitCepte/1.0 (iletisim: destek@akaryakitcepte.app)',
    'Accept-Language': 'tr',
}
OVERPASS_URLS = [
    'https://overpass-api.de/api/interpreter',
    'https://overpass.kumi.systems/api/interpreter',
    'https://overpass.openstreetmap.ru/api/interpreter',
]
NOMINATIM_SEARCH_URL = 'https://nominatim.openstreetmap.org/search'
NOMINATIM_REVERSE_URL = 'https://nominatim.openstreetmap.org/reverse'

# Türkçe il isimlerini slug'a normalize etmek için basit eşleme
IL_NORMALIZE = {
    'i̇stanbul': 'istanbul', 'İstanbul': 'istanbul',
}


def _marka_normalize(marka: str) -> str:
    """BP Petrolleri A.Ş., Petrol Ofisi Grubu ile birleşti (marka dönüşümü
    Kasım 2026'da tamamlanacak) — OSM üzerindeki eski 'BP' etiketlerini de
    Petrol Ofisi olarak normalize ediyoruz."""
    if not marka:
        return marka
    temiz = marka.strip().lower()
    if temiz in ('bp', 'bp türkiye', 'bp petrolleri') or temiz.startswith('bp '):
        return 'Petrol Ofisi'
    return marka


def _slugla(metin: str) -> str:
    if not metin:
        return ''
    metin = metin.strip().lower()
    ceviri = str.maketrans('çğıöşü', 'cgiosu')
    return metin.translate(ceviri).replace(' ', '-')


# ─── Nominatim (gerçek geocoding) ─────────────────────────────────────────────

def _nominatim_ara(sorgu: str, limit: int = 5) -> list:
    """Adres/yer adını gerçek koordinatlara çevirir (OpenStreetMap Nominatim)."""
    try:
        r = requests.get(
            NOMINATIM_SEARCH_URL,
            params={
                'q': sorgu,
                'format': 'jsonv2',
                'limit': limit,
                'countrycodes': 'tr',
                'addressdetails': 1,
            },
            headers=NOMINATIM_HEADERS,
            timeout=8,
        )
        r.raise_for_status()
        return r.json() or []
    except Exception as e:
        logger.warning('Nominatim arama hatası: %s', e)
        return []


def _nominatim_ters_kod(lat: float, lon: float) -> dict | None:
    """Koordinatı gerçek adrese çevirir (reverse geocoding)."""
    try:
        r = requests.get(
            NOMINATIM_REVERSE_URL,
            params={
                'lat': lat,
                'lon': lon,
                'format': 'jsonv2',
                'addressdetails': 1,
                'zoom': 16,
            },
            headers=NOMINATIM_HEADERS,
            timeout=8,
        )
        r.raise_for_status()
        return r.json()
    except Exception as e:
        logger.warning('Nominatim ters kod hatası: %s', e)
        return None


def _il_slug_cikar(adres: dict) -> str:
    """Nominatim address bloğundan il slug'ı çıkarır."""
    aday = (
        adres.get('province') or adres.get('state') or
        adres.get('city') or adres.get('county') or ''
    )
    return _slugla(aday)


# ─── Overpass (gerçek istasyon verisi) ────────────────────────────────────────

def _overpass_sorgu_calistir(query: str) -> dict | None:
    """Birden fazla Overpass aynasını sırayla dener (biri yavaş/erişilemez olursa diğerine geçer)."""
    for mirror in OVERPASS_URLS:
        try:
            r = requests.post(mirror, data={'data': query}, headers=NOMINATIM_HEADERS, timeout=20)
            if r.status_code == 200:
                return r.json()
            logger.warning('Overpass %s HTTP %d', mirror, r.status_code)
        except Exception as e:
            logger.warning('Overpass %s erişim hatası: %s', mirror, e)
    return None


def _overpass_istasyonlari_cek(lat: float, lon: float, yaricap_m: int = 8000, limit: int = 40) -> list:
    """OpenStreetMap Overpass API üzerinden gerçek akaryakıt istasyonlarını çeker.
    Sonuç boşsa (kırsal/az haritalanmış bölge) yarıçapı otomatik genişletip tekrar dener."""
    denemeler = [yaricap_m, yaricap_m * 2, yaricap_m * 4]  # örn. 8km -> 16km -> 32km
    for r_m in denemeler:
        r_m = min(r_m, 40000)
        query = f"""
        [out:json][timeout:18];
        (
          node["amenity"="fuel"](around:{r_m},{lat},{lon});
          way["amenity"="fuel"](around:{r_m},{lat},{lon});
        );
        out center {limit};
        """
        data = _overpass_sorgu_calistir(query)
        if not data:
            continue

        istasyonlar = []
        for el in data.get('elements', []):
            if el.get('type') == 'node':
                slat, slon = el.get('lat'), el.get('lon')
            else:
                merkez = el.get('center')
                if not merkez:
                    continue
                slat, slon = merkez.get('lat'), merkez.get('lon')
            if slat is None or slon is None:
                continue
            etiketler = el.get('tags', {})
            marka = _marka_normalize(etiketler.get('brand') or etiketler.get('operator') or etiketler.get('name') or 'Bilinmeyen')
            isim = etiketler.get('name') or marka
            istasyonlar.append({
                'name': isim,
                'brand': marka,
                'lat': slat,
                'lon': slon,
            })
        if istasyonlar:
            return istasyonlar
        # boş sonuç -> bir sonraki (daha geniş) yarıçapı dene
    return []


# ─── İl / İlçe verileri ───────────────────────────────────────────────────────
ILCELER = {
    'elazig':    ['Merkez', 'Ağın', 'Baskil', 'Karakoçan', 'Keban', 'Maden', 'Palu', 'Sivrice'],
    'elagiz':    ['Merkez', 'Ağın', 'Baskil', 'Karakoçan', 'Keban', 'Maden', 'Palu', 'Sivrice'],
    'ankara':    ['Çankaya', 'Keçiören', 'Yenimahalle', 'Mamak', 'Sincan', 'Etimesgut', 'Altındağ'],
    'istanbul':  ['Kadıköy', 'Beşiktaş', 'Üsküdar', 'Şişli', 'Ümraniye', 'Fatih', 'Bakırköy'],
    'izmir':     ['Konak', 'Bornova', 'Karşıyaka', 'Buca', 'Çiğli', 'Gaziemir'],
    'bursa':     ['Osmangazi', 'Nilüfer', 'Yıldırım', 'Gemlik', 'İnegöl'],
    'antalya':   ['Muratpaşa', 'Kepez', 'Konyaaltı', 'Alanya', 'Manavgat'],
    'konya':     ['Selçuklu', 'Meram', 'Karatay', 'Ereğli', 'Akşehir'],
    'adana':     ['Seyhan', 'Çukurova', 'Yüreğir', 'Sarıçam', 'Ceyhan'],
    'trabzon':   ['Ortahisar', 'Akçaabat', 'Arsin', 'Araklı', 'Yomra'],
    'diyarbakir':['Bağlar', 'Kayapınar', 'Sur', 'Yenişehir', 'Bismil'],
    'erzurum':   ['Yakutiye', 'Palandöken', 'Aziziye', 'Oltu', 'Horasan'],
    'malatya':   ['Battalgazi', 'Yeşilyurt', 'Akçadağ', 'Doğanşehir'],
    'gaziantep': ['Şahinbey', 'Şehitkamil', 'Nizip', 'İslahiye'],
    'samsun':    ['İlkadım', 'Atakum', 'Canik', 'Bafra', 'Çarşamba'],
    'mersin':    ['Yenişehir', 'Akdeniz', 'Mezitli', 'Toroslar', 'Tarsus'],
    'kayseri':   ['Melikgazi', 'Kocasinan', 'Talas', 'Develi'],
    'eskisehir': ['Odunpazarı', 'Tepebaşı', 'Sivrihisar'],
    'kocaeli':   ['İzmit', 'Gebze', 'Körfez', 'Başiskele', 'Çayırova'],
}


# ─── Rotalar ──────────────────────────────────────────────────────────────────

@app.route('/')
def index():
    return render_template('index.html')


@app.route('/api/fiyatlar')
def api_fiyatlar():
    il = request.args.get('il', 'elazig').lower().strip()
    sonuc = fiyat_cek(il)

    # Zaman bilgisini okunabilir formata çevir
    try:
        zaman_dt = datetime.fromisoformat(sonuc['zaman'])
        zaman_str = zaman_dt.strftime('%d.%m.%Y %H:%M')
    except Exception:
        zaman_str = sonuc.get('zaman', '')

    return jsonify({
        'status': 'success',
        'source': sonuc['kaynak'],
        'guncelleme': zaman_str,
        'canli': sonuc['canli'],
        'markalar': sonuc['markalar'],
    })


@app.route('/api/ilceler')
def api_ilceler():
    il = request.args.get('il', 'elazig').lower().strip()
    ilceler = ILCELER.get(il, ['Merkez'])
    return jsonify({'status': 'success', 'ilceler': ilceler})


@app.route('/api/benzinlikler')
def api_benzinlikler():
    """Verilen koordinat etrafındaki GERÇEK akaryakıt istasyonlarını döner (OSM Overpass)."""
    try:
        lat = float(request.args.get('lat', 38.6749))
        lon = float(request.args.get('lon', 39.2225))
    except (TypeError, ValueError):
        lat, lon = 38.6749, 39.2225

    try:
        yaricap_km = float(request.args.get('radius', 8))
    except (TypeError, ValueError):
        yaricap_km = 8
    yaricap_m = max(1000, min(int(yaricap_km * 1000), 30000))

    istasyonlar = _overpass_istasyonlari_cek(lat, lon, yaricap_m)

    if not istasyonlar:
        # Overpass geçici olarak erişilemezse boş harita yerine bilgilendirici yanıt dön
        return jsonify({'status': 'success', 'stations': [], 'uyari': 'Yakında istasyon bulunamadı veya harita servisi geçici olarak erişilemiyor.'})

    return jsonify({'status': 'success', 'stations': istasyonlar})


@app.route('/api/autocomplete')
def api_autocomplete():
    q = request.args.get('q', '')
    if not q or len(q) < 2:
        return jsonify([])

    sonuclar = _nominatim_ara(q, limit=6)
    oneriler = [
        {
            'display_name': s.get('display_name', q),
            'lat': float(s.get('lat')),
            'lon': float(s.get('lon')),
        }
        for s in sonuclar if s.get('lat') and s.get('lon')
    ]
    return jsonify(oneriler)


@app.route('/api/koordinat')
def api_koordinat():
    yer = request.args.get('yer', '').strip()
    if not yer:
        return jsonify({'status': 'success', 'lat': 38.6749, 'lon': 39.2225})

    sonuclar = _nominatim_ara(yer, limit=1)
    if sonuclar:
        s = sonuclar[0]
        return jsonify({
            'status': 'success',
            'lat': float(s['lat']),
            'lon': float(s['lon']),
            'display_name': s.get('display_name', yer),
        })

    logger.warning('Koordinat bulunamadı, varsayılana dönülüyor: %s', yer)
    return jsonify({'status': 'success', 'lat': 38.6749, 'lon': 39.2225, 'display_name': yer})


@app.route('/api/ters-kod')
def api_ters_kod():
    try:
        lat = float(request.args.get('lat', ''))
        lon = float(request.args.get('lon', ''))
    except (TypeError, ValueError):
        return jsonify({'status': 'success', 'display_name': 'Merkez, Elazığ', 'il': 'elazig'})

    sonuc = _nominatim_ters_kod(lat, lon)
    if sonuc:
        adres = sonuc.get('address', {})
        il_slug = _il_slug_cikar(adres) or 'elazig'
        display = sonuc.get('display_name', '')
        # Kısa/okunur konum etiketi (ilçe, il)
        ilce = adres.get('town') or adres.get('district') or adres.get('suburb') or adres.get('county') or ''
        il_adi = adres.get('province') or adres.get('state') or ''
        kisa_ad = ', '.join([p for p in [ilce, il_adi] if p]) or display[:60]
        return jsonify({
            'status': 'success',
            'display_name': kisa_ad or display,
            'tam_adres': display,
            'il': il_slug,
        })

    return jsonify({'status': 'success', 'display_name': 'Bilinmeyen konum', 'il': 'elazig'})


@app.route('/api/cache/temizle', methods=['POST'])
def api_cache_temizle():
    """Cache'i temizle — yeni fiyat çekimini zorlar."""
    il = request.json.get('il') if request.is_json else None
    cache_temizle(il)
    return jsonify({'status': 'success', 'mesaj': 'Cache temizlendi'})


if __name__ == '__main__':
    app.run(debug=True, port=5000)
