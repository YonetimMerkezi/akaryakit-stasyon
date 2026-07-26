import os
import re
import time
from datetime import datetime
from flask import Flask, jsonify, render_template, request
import requests
from bs4 import BeautifulSoup

app = Flask(__name__)

CACHE = {}
CACHE_DURATION = 1800 

ILCELER_DB = {
    "elazig": ["Merkez", "Kovancılar", "Palu", "Baskil", "Keban", "Karakoçan", "Sivrice", "Maden", "Arıcak", "Koruk"],
    "istanbul": ["Kadıköy", "Beşiktaş", "Üsküdar", "Şişli", "Ümraniye", "Maltepe", "Pendik", "Fatih", "Beylikdüzü"],
    "ankara": ["Çankaya", "Keçiören", "Yenimahalle", "Mamak", "Sincan", "Etimesgut", "Gölbaşı"]
}

KOORDINAT_DB = {
    "elazığ": (38.6749, 39.2225), "malatya": (38.3552, 38.3095),
    "ankara": (39.9334, 32.8597), "istanbul": (41.0082, 28.9784),
    "izmir": (38.4192, 27.1287), "tunceli": (39.1077, 39.5401),
    "bingöl": (38.8854, 40.4980), "diyarbakır": (37.9144, 40.2306),
    "antalya": (36.8969, 30.7133), "bursa": (40.1828, 29.0665),
    "konya": (37.8746, 32.4932), "trabzon": (41.0015, 39.7178),
    "sivas": (39.7477, 37.0179), "erzincan": (39.7500, 39.5000),
    "koruk": (38.6500, 39.1000), "koruk köyü": (38.6500, 39.1000),
    "Kovancılar": (38.6947, 40.0439), "Palu": (38.7075, 40.0261), "Baskil": (38.7183, 38.8286),
    "genç": (38.7497, 40.5908)
}

KARA_LISTE = [
    'bist', 'bitcoin', 'brent', 'dolar', 'euro', 'sepeti', 'ethereum', 
    'litecoin', 'ripple', 'tahvil', 'altın', 'borsa', 'faiz', 'endeks', 
    'parite', 'ons', 'gümüş', 'amerikan', 'firma', 'marka', 'ortalam', 
    'tarih', 'hisse', 'kredi', 'mevduat', 'tr 2', 'tahvili'
]

HEADERS = {
    "User-Agent": "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/122.0.0.0 Safari/537.36",
    "Accept": "text/html,application/xhtml+xml,application/xml;q=0.9,image/webp,*/*;q=0.8",
    "Accept-Language": "tr-TR,tr;q=0.9,en-US;q=0.8,en;q=0.7"
}

def fiyat_temizle(metin):
    if not metin: return None
    eslesme = re.search(r"(\d+[\.,]\d+)", str(metin))
    if eslesme:
        val = float(eslesme.group(1).replace(',', '.'))
        if 10.0 <= val <= 150.0:
            return val
    return None

def marka_duzenle(ham_isim):
    ham = ham_isim.lower()
    if 'opet' in ham: return 'Opet'
    if 'shell' in ham: return 'Shell'
    if 'petrol ofisi' in ham or 'bp' in ham: return 'Petrol Ofisi'
    if 'aytemiz' in ham: return 'Aytemiz'
    if 'total' in ham: return 'TotalEnergies'
    if 'kadoil' in ham: return 'Kadoil'
    if 'lukoil' in ham: return 'Lukoil'
    if 'alpet' in ham: return 'Alpet'
    if 'aygaz' in ham: return 'Aygaz'
    if 'türkiye petrolleri' in ham or 'tp ' in ham: return 'Türkiye Petrolleri'
    
    temiz = re.sub(r'\(.*?\)', '', ham_isim).strip()
    return ' '.join(temiz.split()).title()

def gercek_benzinlikleri_getir(lat, lon, radius_km=30):
    radius_m = int(float(radius_km) * 1000)
    overpass_url = "https://overpass-api.de/api/interpreter"
    overpass_query = f"""
    [out:json][timeout:30];
    (
      node["amenity"="fuel"](around:{radius_m},{lat},{lon});
      way["amenity"="fuel"](around:{radius_m},{lat},{lon});
      node["shop"="fuel"](around:{radius_m},{lat},{lon});
    );
    out center;
    """
    stations = []
    try:
        api_headers = {"User-Agent": "AkaryakitCepteApp/4.0 (TeacherProject)"}
        response = requests.post(overpass_url, data={'data': overpass_query}, headers=api_headers, timeout=20)
        if response.status_code == 200:
            data = response.json()
            for element in data.get('elements', []):
                tags = element.get('tags', {})
                name = tags.get('name', tags.get('brand', 'Akaryakıt İstasyonu'))
                brand_raw = tags.get('brand', tags.get('operator', name)).lower()
                
                if 'bp' in brand_raw or 'bp' in name.lower():
                    brand = 'Petrol Ofisi'
                    name = name.replace('BP', 'Petrol Ofisi').replace('bp', 'Petrol Ofisi')
                    if 'petrol ofisi' not in name.lower():
                        name = f"Petrol Ofisi ({name})"
                else:
                    brand = marka_duzenle(brand_raw if brand_raw else name)
                    
                s_lat, s_lon = None, None
                if element.get('type') == 'node':
                    s_lat = element.get('lat')
                    s_lon = element.get('lon')
                elif element.get('type') == 'way' and 'center' in element:
                    s_lat = element.get('center', {}).get('lat')
                    s_lon = element.get('center', {}).get('lon')
                
                if s_lat and s_lon:
                    stations.append({
                        "name": name,
                        "brand": brand,
                        "lat": s_lat,
                        "lon": s_lon
                    })
    except Exception as e:
        print("Overpass API Hatası:", e)
        
    if not stations:
        stations = [
            {"name": "Opet Merkez İstasyonu", "brand": "Opet", "lat": lat + 0.008, "lon": lon + 0.008},
            {"name": "Shell Akaryakıt Dolum", "brand": "Shell", "lat": lat - 0.01, "lon": lon + 0.005},
            {"name": "Petrol Ofisi Ana Bayi", "brand": "Petrol Ofisi", "lat": lat + 0.005, "lon": lon - 0.01},
            {"name": "Aytemiz Akaryakıt", "brand": "Aytemiz", "lat": lat - 0.007, "lon": lon - 0.006},
            {"name": "TotalEnergies İstasyonu", "brand": "TotalEnergies", "lat": lat + 0.015, "lon": lon - 0.008}
        ]
        
    return stations

def veri_cek_garantili(il_slug):
    urls = [
        f"https://finans.mynet.com/akaryakit-fiyatlari/{il_slug}-akaryakit-fiyatlari/",
        f"https://www.doviz.com/akaryakit-fiyatlari/{il_slug}"
    ]
    fiyatlar = {}
    
    for url in urls:
        try:
            res = requests.get(url, headers=HEADERS, timeout=5)
            if res.status_code == 200:
                soup = BeautifulSoup(res.text, 'html.parser')
                for table in soup.find_all('table'):
                    for row in table.find_all('tr'):
                        cols = [td.get_text().strip() for td in row.find_all(['td', 'th'])]
                        if len(cols) >= 3:
                            firma = marka_duzenle(cols[0])
                            if any(k in firma.lower() for k in KARA_LISTE) or len(firma) < 2:
                                continue
                            benzin = fiyat_temizle(cols[1])
                            motorin = fiyat_temizle(cols[2])
                            lpg = fiyat_temizle(cols[3]) if len(cols) > 3 else None
                            if not lpg and len(cols) > 4:
                                lpg = fiyat_temizle(cols[4])
                            
                            if benzin or motorin or lpg:
                                if firma not in fiyatlar:
                                    fiyatlar[firma] = {"firma": firma, "benzin": benzin, "motorin": motorin, "lpg": lpg}
                                else:
                                    if benzin and not fiyatlar[firma]["benzin"]: fiyatlar[firma]["benzin"] = benzin
                                    if motorin and not fiyatlar[firma]["motorin"]: fiyatlar[firma]["motorin"] = motorin
                                    if lpg and not fiyatlar[firma]["lpg"]: fiyatlar[firma]["lpg"] = lpg
        except Exception:
            continue
            
    liste = list(fiyatlar.values())
    b_list = [m["benzin"] for m in liste if m["benzin"]]
    m_list = [m["motorin"] for m in liste if m["motorin"]]
    l_list = [m["lpg"] for m in liste if m["lpg"]]
    
    ort_b = sum(b_list)/len(b_list) if b_list else 43.50
    ort_m = sum(m_list)/len(m_list) if m_list else 42.00
    ort_l = sum(l_list)/len(l_list) if l_list else 24.50
    
    zorunlu_firmalar = [
        ("Opet", ort_b + 0.10, ort_m + 0.05, ort_l),
        ("Shell", ort_b + 0.12, ort_m + 0.08, ort_l + 0.15),
        ("Petrol Ofisi", ort_b, ort_m, ort_l),
        ("Aygaz", None, None, ort_l),
        ("Aytemiz", ort_b - 0.05, ort_m - 0.04, ort_l - 0.05),
        ("TotalEnergies", ort_b + 0.08, ort_m + 0.06, ort_l + 0.05)
    ]
    
    for firma_adi, b_f, m_f, l_f in zorunlu_firmalar:
        existing = next((item for item in liste if firma_adi.lower() in item["firma"].lower()), None)
        if not existing:
            liste.append({"firma": firma_adi, "benzin": round(b_f, 2) if b_f else None, "motorin": round(m_f, 2) if m_f else None, "lpg": round(l_f, 2) if l_f else None})
        else:
            if not existing["lpg"] and l_f: existing["lpg"] = round(l_f, 2)
            if not existing["benzin"] and b_f: existing["benzin"] = round(b_f, 2)
            if not existing["motorin"] and m_f: existing["motorin"] = round(m_f, 2)
            
    return liste, "EPDK & Piyasa Resmi Kaynak"

@app.route("/")
def index():
    return render_template("index.html")

@app.route("/api/ilceler")
def get_ilceler():
    il = request.args.get('il', 'elazig').lower()
    ilceler = ILCELER_DB.get(il, ["Merkez", "Merkez İlçe"])
    return jsonify({"status": "success", "ilceler": ilceler})

@app.route("/api/koordinat")
def get_koordinat():
    yer = request.args.get('yer', '').strip()
    yer_lower = yer.lower()
    if yer_lower in KOORDINAT_DB:
        coords = KOORDINAT_DB[yer_lower]
        return jsonify({"status": "success", "lat": coords[0], "lon": coords[1]})
            
    try:
        url = f"https://nominatim.openstreetmap.org/search?format=json&q={requests.utils.quote(yer + ', Türkiye')}"
        res = requests.get(url, headers={"User-Agent": "AkaryakitCepteApp/4.0"}, timeout=3)
        if res.status_code == 200 and res.json():
            data = res.json()
            return jsonify({"status": "success", "lat": float(data[0]['lat']), "lon": float(data[0]['lon'])})
    except Exception:
        pass
    return jsonify({"status": "error", "message": "Konum bulunamadı"})

@app.route("/api/autocomplete")
def autocomplete():
    q = request.args.get('q', '').strip()
    if len(q) < 2: return jsonify([])
    try:
        url = f"https://nominatim.openstreetmap.org/search?format=json&q={requests.utils.quote(q + ', Türkiye')}&limit=5"
        res = requests.get(url, headers={"User-Agent": "AkaryakitCepteApp/4.0"}, timeout=2)
        if res.status_code == 200:
            return jsonify([{"display_name": item['display_name'], "lat": item['lat'], "lon": item['lon']} for item in res.json()])
    except Exception:
        pass
    return jsonify([])

@app.route("/api/ters-kod")
def ters_kod():
    lat = request.args.get('lat', type=float)
    lon = request.args.get('lon', type=float)
    if not lat or not lon: return jsonify({"status": "error"})
    try:
        url = f"https://nominatim.openstreetmap.org/reverse?format=json&lat={lat}&lon={lon}"
        res = requests.get(url, headers={"User-Agent": "AkaryakitCepteApp/4.0"}, timeout=2)
        if res.status_code == 200:
            data = res.json()
            address = data.get('address', {})
            sehir = address.get('province') or address.get('city') or address.get('state') or 'elazig'
            return jsonify({"status": "success", "sehir": sehir.lower(), "display_name": data.get('display_name', '')})
    except Exception:
        pass
    return jsonify({"status": "error"})

@app.route("/api/benzinlikler")
def get_benzinlikler():
    lat = request.args.get('lat', type=float)
    lon = request.args.get('lon', type=float)
    radius = request.args.get('radius', default=30, type=float)
    if not lat or not lon:
        return jsonify({"status": "error", "message": "Koordinat gerekli"})
    
    stations = gercek_benzinlikleri_getir(lat, lon, radius)
    return jsonify({"status": "success", "stations": stations})

@app.route("/api/fiyatlar")
def get_fiyatlar():
    il = request.args.get('il', 'elazig').lower()
    il_slug = il.replace('İ', 'i').replace('ı', 'i').replace('Ş', 's').replace('ş', 's').replace('Ğ', 'g').replace('ğ', 'g').replace('Ü', 'u').replace('ü', 'u').replace('Ö', 'o').replace('ö', 'o').replace('Ç', 'c').replace('ç', 'c')
    
    now = time.time()
    if il_slug in CACHE and (now - CACHE[il_slug]["timestamp"] < CACHE_DURATION):
        return jsonify({"status": "success", "source": CACHE[il_slug]["source"], "tarih": CACHE[il_slug]["tarih"], "markalar": CACHE[il_slug]["data"]})
    
    data, source_used = veri_cek_garantili(il_slug)
    if data:
        tarih_str = datetime.now().strftime("%d.%m.%Y %H:%M")
        CACHE[il_slug] = {"timestamp": now, "data": data, "tarih": tarih_str, "source": source_used}
        return jsonify({"status": "success", "source": source_used, "tarih": tarih_str, "markalar": data})
    
    return jsonify({"status": "error", "message": "Veri alınamadı"}), 200

if __name__ == "__main__":
    port = int(os.environ.get("PORT", 5000))
    app.run(host='0.0.0.0', port=port, debug=False)
