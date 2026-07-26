import logging
import os
from datetime import datetime

from flask import Flask, jsonify, render_template, request

from fiyat_servisi import cache_temizle, fiyat_cek

# ─── Uygulama ─────────────────────────────────────────────────────────────────
app = Flask(__name__)
logging.basicConfig(
    level=logging.INFO,
    format='%(asctime)s [%(levelname)s] %(name)s: %(message)s',
)


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
    lat = float(request.args.get('lat', 38.6749))
    lon = float(request.args.get('lon', 39.2225))
    il = request.args.get('il', 'elazig')
    # İleride: OpenStreetMap Overpass API ile gerçek istasyonlar çekilebilir
    # Şu an: koordinat bazlı örnek veri
    stations = [
        {'name': 'Shell Merkez İstasyonu', 'brand': 'Shell',
         'lat': lat + 0.012, 'lon': lon + 0.010},
        {'name': 'Petrol Ofisi Merkez', 'brand': 'Petrol Ofisi',
         'lat': lat - 0.009, 'lon': lon + 0.015},
        {'name': 'Opet Akaryakıt', 'brand': 'Opet',
         'lat': lat + 0.016, 'lon': lon - 0.011},
        {'name': 'TotalEnergies', 'brand': 'TotalEnergies',
         'lat': lat - 0.014, 'lon': lon - 0.013},
        {'name': 'Alpet', 'brand': 'Alpet',
         'lat': lat + 0.007, 'lon': lon + 0.018},
        {'name': 'Aytemiz', 'brand': 'Aytemiz',
         'lat': lat - 0.018, 'lon': lon + 0.005},
    ]
    return jsonify({'status': 'success', 'stations': stations})


@app.route('/api/autocomplete')
def api_autocomplete():
    q = request.args.get('q', '')
    if not q:
        return jsonify([])
    results = [
        {'display_name': f'{q.capitalize()} Mahallesi, Merkez/Elazığ'},
        {'display_name': f'{q.capitalize()} Caddesi, Elazığ'},
        {'display_name': f'{q.capitalize()}, Elazığ Merkez'},
    ]
    return jsonify(results)


@app.route('/api/koordinat')
def api_koordinat():
    yer = request.args.get('yer', '')
    # İleride: Nominatim geocoding entegre edilebilir
    return jsonify({'status': 'success', 'lat': 38.6749, 'lon': 39.2225})


@app.route('/api/ters-kod')
def api_ters_kod():
    lat = request.args.get('lat', '')
    lon = request.args.get('lon', '')
    return jsonify({'status': 'success', 'display_name': 'Merkez, Elazığ', 'il': 'elazig'})


@app.route('/api/cache/temizle', methods=['POST'])
def api_cache_temizle():
    """Cache'i temizle — yeni fiyat çekimini zorlar."""
    il = request.json.get('il') if request.is_json else None
    cache_temizle(il)
    return jsonify({'status': 'success', 'mesaj': 'Cache temizlendi'})


if __name__ == '__main__':
    app.run(debug=True, port=5000)
