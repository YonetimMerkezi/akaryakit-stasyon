"""
fiyat_servisi.py
~~~~~~~~~~~~~~~~
Canlı akaryakıt fiyatı çekme servisi.

Kaynak önceliği:
  1. CollectAPI (COLLECT_API_KEY varsa) — en güvenilir, şehir bazlı
  2. Opet scraper                       — HTML parse, şehir bazlı
  3. Alpet scraper                      — HTML parse, ülke geneli
  4. Shell TR scraper                   — HTML parse, ülke geneli
  5. Dosya cache (her zaman mevcut)     — hiç kaynak çalışmasa bile veri döner

Cache TTL: CACHE_TTL_SAAT (varsayılan 6 saat)
Cache dosyası: fiyat_cache.json
"""

import json
import logging
import os
import re
import time
from concurrent.futures import ThreadPoolExecutor, wait
from datetime import datetime, timedelta
from pathlib import Path

import requests
from bs4 import BeautifulSoup

logger = logging.getLogger(__name__)

# ─── Yapılandırma ────────────────────────────────────────────────────────────
CACHE_DOSYASI = Path(__file__).parent / 'fiyat_cache.json'
CACHE_TTL_SAAT = int(os.environ.get('CACHE_TTL_SAAT', 6))
COLLECT_API_KEY = os.environ.get('COLLECT_API_KEY', '')
# Cloudflare Worker (akaryakit.org scraper) — sonunda / OLMADAN gir
# örn: https://akaryakit-worker.KULLANICI-ADIN.workers.dev
WORKER_URL = os.environ.get('WORKER_URL', 'https://okul-ai-asistan.sedonet23.workers.dev').rstrip('/')

# İl adı → URL slug eşlemesi (Opet ve benzeri siteler için)
IL_SLUG_MAP = {
    'adana': 'adana',
    'ankara': 'ankara',
    'antalya': 'antalya',
    'bursa': 'bursa',
    'diyarbakir': 'diyarbakir',
    'elazig': 'elazig',
    'elagiz': 'elazig',   # frontend'deki yazım hatasını normalize et
    'erzurum': 'erzurum',
    'eskisehir': 'eskisehir',
    'gaziantep': 'gaziantep',
    'istanbul': 'istanbul-avrupa',
    'istanbul-avrupa': 'istanbul-avrupa',
    'istanbul-anadolu': 'istanbul-anadolu',
    'izmir': 'izmir',
    'kayseri': 'kayseri',
    'kocaeli': 'kocaeli',
    'konya': 'konya',
    'malatya': 'malatya',
    'mersin': 'mersin',
    'samsun': 'samsun',
    'trabzon': 'trabzon',
}

# BP Petrolleri A.Ş., Petrol Ofisi Grubu ile birleşti (birleşme Kasım 2026'da
# tamamlanacak marka dönüşümüyle sonuçlanıyor). Bu yüzden hangi kaynaktan
# "BP" olarak gelirse gelsin, artık Petrol Ofisi ile aynı firma olarak
# birleştiriyoruz.
MARKA_NORMALIZE = {
    'bp': 'Petrol Ofisi',
    'bp türkiye': 'Petrol Ofisi',
    'bp petrolleri': 'Petrol Ofisi',
}


def _marka_normalize(firma: str) -> str:
    """Firma adını normalize eder (örn. BP -> Petrol Ofisi birleşmesi)."""
    if not firma:
        return firma
    anahtar = firma.strip().lower()
    return MARKA_NORMALIZE.get(anahtar, firma.strip())


def _markalari_birlestir(*marka_listeleri: list) -> list:
    """Birden fazla kaynaktan gelen marka listelerini, aynı firmaları
    tekilleştirerek (BP/Petrol Ofisi birleşmesi dahil) tek listede toplar.
    Bir firma için birden fazla kaynaktan veri gelirse, dolu olan alanlar
    korunur (ilk gelen kaynak öncelikli, boş alanlar sonraki kaynaklarla
    tamamlanır)."""
    birlesik: dict[str, dict] = {}
    sira: list[str] = []
    for liste in marka_listeleri:
        if not liste:
            continue
        for m in liste:
            firma = _marka_normalize(m.get('firma', ''))
            if not firma:
                continue
            anahtar = firma.lower()
            if anahtar not in birlesik:
                birlesik[anahtar] = {'firma': firma, 'benzin': None, 'motorin': None, 'lpg': None}
                sira.append(anahtar)
            hedef = birlesik[anahtar]
            for alan in ('benzin', 'motorin', 'lpg'):
                if hedef.get(alan) is None and m.get(alan) is not None:
                    hedef[alan] = m.get(alan)
    return [birlesik[k] for k in sira if birlesik[k].get('benzin') or birlesik[k].get('motorin')]


ORTAK_HEADERS = {
    'User-Agent': (
        'Mozilla/5.0 (Windows NT 10.0; Win64; x64) '
        'AppleWebKit/537.36 (KHTML, like Gecko) '
        'Chrome/124.0.0.0 Safari/537.36'
    ),
    'Accept': 'text/html,application/xhtml+xml,application/xml;q=0.9,image/avif,image/webp,*/*;q=0.8',
    'Accept-Language': 'tr-TR,tr;q=0.9,en-US;q=0.8,en;q=0.7',
    'Accept-Encoding': 'gzip, deflate, br',
    'Connection': 'keep-alive',
    'DNT': '1',
    'Upgrade-Insecure-Requests': '1',
}


# ─── Cache ───────────────────────────────────────────────────────────────────

def _cache_oku(il: str) -> dict | None:
    """Dosyadan cache oku. TTL dolmuşsa None döner."""
    try:
        if not CACHE_DOSYASI.exists():
            return None
        with open(CACHE_DOSYASI, 'r', encoding='utf-8') as f:
            veri = json.load(f)
        girdi = veri.get(il)
        if not girdi:
            return None
        zaman = datetime.fromisoformat(girdi['zaman'])
        if datetime.now() - zaman < timedelta(hours=CACHE_TTL_SAAT):
            return girdi
    except Exception as e:
        logger.warning('Cache okuma hatası: %s', e)
    return None


def _eski_cache_oku(il: str) -> dict | None:
    """TTL dolmuş olsa bile son bilinen veriyi döner (tüm kaynaklar başarısız olursa)."""
    try:
        if not CACHE_DOSYASI.exists():
            return None
        with open(CACHE_DOSYASI, 'r', encoding='utf-8') as f:
            veri = json.load(f)
        return veri.get(il)
    except Exception:
        return None


def _cache_yaz(il: str, markalar: list, kaynak: str) -> None:
    """Fiyat verisini dosyaya yaz."""
    try:
        veri = {}
        if CACHE_DOSYASI.exists():
            with open(CACHE_DOSYASI, 'r', encoding='utf-8') as f:
                veri = json.load(f)
        veri[il] = {
            'zaman': datetime.now().isoformat(),
            'kaynak': kaynak,
            'markalar': markalar,
        }
        with open(CACHE_DOSYASI, 'w', encoding='utf-8') as f:
            json.dump(veri, f, ensure_ascii=False, indent=2)
    except Exception as e:
        logger.warning('Cache yazma hatası: %s', e)


# ─── Yardımcı fonksiyonlar ───────────────────────────────────────────────────

def _fiyat_parse(metin: str) -> float | None:
    """'62,50', '62.50', '62,5' gibi formatları float'a çevirir."""
    if not metin:
        return None
    temiz = re.sub(r'[^\d,.]', '', metin.strip())
    if not temiz:
        return None
    temiz = temiz.replace(',', '.')
    # Birden fazla nokta varsa son noktayı ondalık say
    parcalar = temiz.split('.')
    if len(parcalar) > 2:
        temiz = ''.join(parcalar[:-1]) + '.' + parcalar[-1]
    try:
        deger = float(temiz)
        return deger if 10 < deger < 500 else None   # makul fiyat aralığı
    except ValueError:
        return None


def _session_olustur(referer_url: str = '') -> requests.Session:
    """Ortak header'larla requests Session oluşturur."""
    s = requests.Session()
    headers = dict(ORTAK_HEADERS)
    if referer_url:
        headers['Referer'] = referer_url
    s.headers.update(headers)
    return s


# ─── Kaynak 0: Cloudflare Worker (doviz.com scraper) ─────────────────────────

def _worker_cek(il: str) -> tuple[list | None, str]:
    """
    Cloudflare Worker üzerinden doviz.com'dan il bazlı firma fiyatlarını çeker.
    Worker JSON formatı: { error: bool, istasyonlar: [{dagitici, benzin, motorin, lpg, tarih}] }
    Ortam değişkeni: WORKER_URL (örn: https://xxx.workers.dev)
    """
    if not WORKER_URL:
        return None, ''
    try:
        slug = IL_SLUG_MAP.get(il.lower(), il.lower())
        r = requests.get(
            WORKER_URL,
            params={'il': slug, 'ilce': 'merkez'},
            timeout=15,
        )
        if r.status_code != 200:
            logger.warning('Worker HTTP %d', r.status_code)
            return None, ''

        data = r.json()
        if data.get('error'):
            logger.warning('Worker hata döndü: %s', data.get('message'))
            return None, ''

        markalar = []
        for m in data.get('istasyonlar', []):
            firma = (m.get('dagitici') or '').strip()
            benzin  = _fiyat_parse(m.get('benzin')  or '')
            motorin = _fiyat_parse(m.get('motorin') or '')
            lpg     = _fiyat_parse(m.get('lpg')     or '')
            if firma and (benzin or motorin):
                markalar.append({
                    'firma':   firma,
                    'benzin':  benzin,
                    'motorin': motorin,
                    'lpg':     lpg,
                })

        if markalar:
            return markalar, 'doviz.com (Worker)'
    except Exception as e:
        logger.error('Worker hatası: %s', e)
    return None, ''


# ─── Kaynak 1: CollectAPI ─────────────────────────────────────────────────────

def _collectapi_cek(il: str) -> tuple[list | None, str]:
    """
    CollectAPI üzerinden şehir bazlı akaryakıt fiyatı çeker.
    API key gerekmektedir: https://collectapi.com/tr/api/gasPrice/akaryakit-fiyatlari-api
    Ortam değişkeni: COLLECT_API_KEY
    """
    if not COLLECT_API_KEY:
        return None, ''
    try:
        url = 'https://api.collectapi.com/gasPrice/turkeyGasPrice'
        headers = {
            'Authorization': f'apikey {COLLECT_API_KEY}',
            'Content-Type': 'application/json',
        }
        params = {'city': il.replace('elagiz', 'elazig')}
        r = requests.get(url, headers=headers, params=params, timeout=10)
        r.raise_for_status()
        data = r.json()

        if not data.get('success'):
            logger.warning('CollectAPI başarısız: %s', data)
            return None, ''

        markalar = []
        for item in data.get('result', []):
            benzin = _fiyat_parse(str(item.get('gasoline', '') or item.get('benzin', '')))
            motorin = _fiyat_parse(str(item.get('diesel', '') or item.get('motorin', '') or item.get('dizel', '')))
            lpg = _fiyat_parse(str(item.get('lpg', '') or item.get('autogas', '')))
            firma = item.get('name', item.get('firma', '')).strip()
            if firma and (benzin or motorin):
                markalar.append({
                    'firma': firma,
                    'benzin': benzin,
                    'motorin': motorin,
                    'lpg': lpg,
                })
        if markalar:
            return markalar, 'CollectAPI'
    except Exception as e:
        logger.error('CollectAPI hatası: %s', e)
    return None, ''


# ─── Kaynak 2: Opet scraper ───────────────────────────────────────────────────

def _opet_cek(il: str) -> tuple[list | None, str]:
    """
    Opet resmi sitesinden il bazlı fiyat çeker.
    URL: https://www.opet.com.tr/akaryakit-fiyatlari/{il-slug}
    """
    slug = IL_SLUG_MAP.get(il.lower(), il.lower())
    url = f'https://www.opet.com.tr/akaryakit-fiyatlari/{slug}'

    try:
        s = _session_olustur('https://www.opet.com.tr/')
        # Önce ana sayfaya git — cookie ve session token al
        s.get('https://www.opet.com.tr/', timeout=6)
        time.sleep(0.5)

        r = s.get(url, timeout=10)
        if r.status_code != 200:
            logger.warning('Opet HTTP %d: %s', r.status_code, url)
            return None, ''

        soup = BeautifulSoup(r.text, 'html.parser')

        # Strateji 1: JSON-LD veya inline JSON
        for script in soup.find_all('script'):
            txt = script.string or ''
            if 'benzin' in txt.lower() and re.search(r'\d{2}[.,]\d{2}', txt):
                prices = _opet_json_parse(txt)
                if prices:
                    return [{'firma': 'Opet', **prices}], 'Opet'

        # Strateji 2: HTML fiyat elementleri
        prices = _opet_html_parse(soup)
        if prices:
            return [{'firma': 'Opet', **prices}], 'Opet'

        # Strateji 3: Genel metin regex
        prices = _opet_regex_parse(soup.get_text())
        if prices:
            return [{'firma': 'Opet', **prices}], 'Opet'

    except Exception as e:
        logger.error('Opet scraper hatası: %s', e)
    return None, ''


def _opet_json_parse(metin: str) -> dict | None:
    """Script içindeki JSON verisinden fiyat çıkar."""
    # JSON obje yakala
    json_match = re.search(r'\{[^{}]*(?:benzin|gasoline)[^{}]*\}', metin, re.IGNORECASE | re.DOTALL)
    if json_match:
        try:
            obj = json.loads(json_match.group())
            return _normalize_fiyat_dict(obj)
        except Exception:
            pass
    # Anahtar-değer çiftleri regex
    result = {}
    for anahtar, deger in [('benzin', r'benzin["\s:]+(\d{2,3}[.,]\d{2})'),
                            ('motorin', r'motorin["\s:]+(\d{2,3}[.,]\d{2})'),
                            ('lpg', r'lpg["\s:]+(\d{2,3}[.,]\d{2})')]:
        m = re.search(deger, metin, re.IGNORECASE)
        if m:
            result[anahtar] = _fiyat_parse(m.group(1))
    return result if result.get('benzin') or result.get('motorin') else None


def _opet_html_parse(soup: BeautifulSoup) -> dict | None:
    """BeautifulSoup üzerinden fiyat elementlerini bul."""
    result = {}
    yakıt_map = {
        'benzin': ['benzin', 'gasoline', 'petrol', '95'],
        'motorin': ['motorin', 'diesel', 'dizel', 'mazot'],
        'lpg': ['lpg', 'autogas', 'otogaz'],
    }

    # price-card, fuel-price, price-box gibi yaygın sınıflar
    selectors = [
        '[class*="price"]', '[class*="fuel"]', '[class*="fiyat"]',
        '[data-fuel]', '[data-type]', 'td', 'li',
    ]
    for sel in selectors:
        elements = soup.select(sel)
        for el in elements:
            el_text = el.get_text(separator=' ', strip=True).lower()
            fiyat = None
            yakıt_turu = None

            for yakıt, anahtar_kelimeler in yakıt_map.items():
                if any(k in el_text for k in anahtar_kelimeler):
                    yakıt_turu = yakıt
                    break

            if yakıt_turu:
                price_match = re.search(r'(\d{2,3}[.,]\d{2})', el_text)
                if price_match:
                    fiyat = _fiyat_parse(price_match.group(1))
                if fiyat:
                    result[yakıt_turu] = fiyat

        if result.get('benzin') and result.get('motorin'):
            return result

    return result if (result.get('benzin') or result.get('motorin')) else None


def _opet_regex_parse(metin: str) -> dict | None:
    """Son çare: ham metni regex ile tara."""
    result = {}
    patterns = [
        ('benzin', r'(?:benzin|95\s*ok)[^\d]*(\d{2,3}[.,]\d{2})'),
        ('motorin', r'(?:motorin|dizel|mazot)[^\d]*(\d{2,3}[.,]\d{2})'),
        ('lpg', r'(?:lpg|otogaz|autogas)[^\d]*(\d{2,3}[.,]\d{2})'),
    ]
    for yakıt, pattern in patterns:
        m = re.search(pattern, metin, re.IGNORECASE)
        if m:
            fiyat = _fiyat_parse(m.group(1))
            if fiyat:
                result[yakıt] = fiyat
    return result if (result.get('benzin') or result.get('motorin')) else None


def _normalize_fiyat_dict(obj: dict) -> dict | None:
    """Farklı key isimlerini normalize et."""
    result = {}
    eşleme = {
        'benzin': ['benzin', 'gasoline', 'petrol', 'kursunsuz'],
        'motorin': ['motorin', 'diesel', 'dizel', 'mazot'],
        'lpg': ['lpg', 'autogas', 'otogaz'],
    }
    for yakıt, isimler in eşleme.items():
        for isim in isimler:
            if isim in obj:
                fiyat = _fiyat_parse(str(obj[isim]))
                if fiyat:
                    result[yakıt] = fiyat
                    break
    return result if result else None


# ─── Kaynak 3: Alpet scraper ──────────────────────────────────────────────────

def _alpet_cek(il: str) -> tuple[list | None, str]:
    """
    Alpet resmi sitesinden fiyat çeker.
    Not: Alpet ülke geneli tek fiyat yayınlar, il filtreleme yoktur.
    """
    url = 'https://www.alpet.com.tr/akaryakit-fiyatlari'
    try:
        s = _session_olustur('https://www.alpet.com.tr/')
        r = s.get(url, timeout=10)
        if r.status_code != 200:
            return None, ''

        soup = BeautifulSoup(r.text, 'html.parser')

        # Sayfadaki fiyat tablosunu veya kartları ara
        fiyatlar = {}
        metin = soup.get_text()
        patterns = [
            ('benzin', r'(?:kurşunsuz|benzin|95)[^\d\n]{0,30}(\d{2,3}[.,]\d{2})'),
            ('motorin', r'(?:motorin|dizel|mazot|euro\s*dizel)[^\d\n]{0,30}(\d{2,3}[.,]\d{2})'),
            ('lpg', r'(?:lpg|autogas|otogaz)[^\d\n]{0,30}(\d{2,3}[.,]\d{2})'),
        ]
        for yakıt, pattern in patterns:
            m = re.search(pattern, metin, re.IGNORECASE)
            if m:
                fiyat = _fiyat_parse(m.group(1))
                if fiyat:
                    fiyatlar[yakıt] = fiyat

        if fiyatlar.get('benzin') or fiyatlar.get('motorin'):
            return [{'firma': 'Alpet', **fiyatlar}], 'Alpet'

    except Exception as e:
        logger.error('Alpet scraper hatası: %s', e)
    return None, ''


# ─── Kaynak 4: Shell TR scraper ───────────────────────────────────────────────

def _shell_cek(il: str) -> tuple[list | None, str]:
    """Shell Türkiye güncel akaryakıt fiyatlarını çeker."""
    url = 'https://www.shell.com.tr/tr_tr/motoristler/shell-akaryakit/akaryakit-fiyatlari.html'
    try:
        s = _session_olustur('https://www.shell.com.tr/')
        r = s.get(url, timeout=10)
        if r.status_code != 200:
            return None, ''

        soup = BeautifulSoup(r.text, 'html.parser')
        metin = soup.get_text()
        fiyatlar = {}
        patterns = [
            ('benzin', r'(?:v-power|fuelsave\s*benzin|benzin)[^\d\n]{0,40}(\d{2,3}[.,]\d{2})'),
            ('motorin', r'(?:v-power\s*diesel|fuelsave\s*dizel|motorin|dizel)[^\d\n]{0,40}(\d{2,3}[.,]\d{2})'),
            ('lpg', r'(?:lpg|autogas)[^\d\n]{0,30}(\d{2,3}[.,]\d{2})'),
        ]
        for yakıt, pattern in patterns:
            m = re.search(pattern, metin, re.IGNORECASE)
            if m:
                fiyat = _fiyat_parse(m.group(1))
                if fiyat:
                    fiyatlar[yakıt] = fiyat

        if fiyatlar.get('benzin') or fiyatlar.get('motorin'):
            return [{'firma': 'Shell', **fiyatlar}], 'Shell'

    except Exception as e:
        logger.error('Shell scraper hatası: %s', e)
    return None, ''


# ─── Kaynak 5: PetrolOfisi scraper ───────────────────────────────────────────

def _petrolofisi_cek(il: str) -> tuple[list | None, str]:
    """Petrol Ofisi güncel akaryakıt fiyatlarını çeker."""
    url = 'https://www.petrolofisi.com.tr/akaryakit-fiyatlari'
    try:
        s = _session_olustur('https://www.petrolofisi.com.tr/')
        r = s.get(url, timeout=10)
        if r.status_code != 200:
            return None, ''

        soup = BeautifulSoup(r.text, 'html.parser')
        metin = soup.get_text()
        fiyatlar = {}
        patterns = [
            ('benzin', r'(?:benzin|kurşunsuz)[^\d\n]{0,40}(\d{2,3}[.,]\d{2})'),
            ('motorin', r'(?:motorin|dizel|mazot)[^\d\n]{0,40}(\d{2,3}[.,]\d{2})'),
            ('lpg', r'(?:lpg|autogas)[^\d\n]{0,30}(\d{2,3}[.,]\d{2})'),
        ]
        for yakıt, pattern in patterns:
            m = re.search(pattern, metin, re.IGNORECASE)
            if m:
                fiyat = _fiyat_parse(m.group(1))
                if fiyat:
                    fiyatlar[yakıt] = fiyat

        if fiyatlar.get('benzin') or fiyatlar.get('motorin'):
            return [{'firma': 'Petrol Ofisi', **fiyatlar}], 'Petrol Ofisi'

    except Exception as e:
        logger.error('PetrolOfisi scraper hatası: %s', e)
    return None, ''


# ─── Kaynak 6: TotalEnergies scraper ──────────────────────────────────────────

def _totalenergies_cek(il: str) -> tuple[list | None, str]:
    """
    TotalEnergies Türkiye güncel akaryakıt fiyatlarını çeker.
    Not: Daha önce bu kaynak hiç yoktu — 'Total enerji verileri yok' sorununun
    doğrudan sebebi buydu. Önce il bazlı sayfa denenir, olmazsa ülke geneli.
    """
    slug = IL_SLUG_MAP.get(il.lower(), il.lower())
    denenecek_urller = [
        f'https://akaryakitfiyatlari.net/{slug}-totalenergies-akaryakit-fiyatlari',
        'https://akaryakitfiyatlari.net/totalenergies-akaryakit-fiyatlari',
    ]
    for url in denenecek_urller:
        try:
            s = _session_olustur('https://akaryakitfiyatlari.net/')
            r = s.get(url, timeout=10)
            if r.status_code != 200:
                continue

            soup = BeautifulSoup(r.text, 'html.parser')
            metin = soup.get_text(separator=' ', strip=True)
            fiyatlar = {}
            patterns = [
                ('benzin', r'Benzin[:\s]{1,5}(\d{2,3}[.,]\d{2})'),
                ('motorin', r'Motorin[:\s]{1,5}(\d{2,3}[.,]\d{2})'),
                ('lpg', r'LPG[:\s]{1,5}(\d{2,3}[.,]\d{2})'),
            ]
            for yakıt, pattern in patterns:
                m = re.search(pattern, metin, re.IGNORECASE)
                if m:
                    fiyat = _fiyat_parse(m.group(1))
                    if fiyat:
                        fiyatlar[yakıt] = fiyat

            if fiyatlar.get('benzin') or fiyatlar.get('motorin'):
                return [{'firma': 'TotalEnergies', **fiyatlar}], 'TotalEnergies'

        except Exception as e:
            logger.error('TotalEnergies scraper hatası (%s): %s', url, e)
    return None, ''


# ─── Ana servis fonksiyonu ────────────────────────────────────────────────────

# Çok-markalı (tek çağrıda birden fazla firma döndüren) kaynaklar.
# Biri başarılı olursa tek başına yeterli sayılır.
_COK_MARKALI_KAYNAKLAR = [
    ('Worker', _worker_cek),
    ('CollectAPI', _collectapi_cek),
]

# Tek-markalı (her biri SADECE kendi firmasının fiyatını döndüren) resmi
# site scraper'ları. ÖNEMLİ: Bunlar eskiden "ilk başarılı olan kazanır"
# mantığıyla çalıştırılıyordu — yani Opet başarılı olduğunda Shell,
# PetrolOfisi, Aytemiz ve TotalEnergies hiç denenmiyor, tek kart
# görünüyordu. Şimdi HEPSİ paralel çalıştırılıp birleştiriliyor.
_TEK_MARKALI_KAYNAKLAR = [
    ('Opet', _opet_cek),
    ('Alpet', _alpet_cek),
    ('Shell', _shell_cek),
    ('PetrolOfisi', _petrolofisi_cek),
    ('TotalEnergies', _totalenergies_cek),
]

_YEDEK_FIYATLAR = [
    {'firma': 'Petrol Ofisi', 'benzin': 69.68, 'motorin': 81.31, 'lpg': 31.81},
    {'firma': 'Shell',        'benzin': 69.65, 'motorin': 81.32, 'lpg': 34.29},
    {'firma': 'Opet',         'benzin': 69.61, 'motorin': 81.32, 'lpg': None},
    {'firma': 'Total',        'benzin': 69.63, 'motorin': 81.33, 'lpg': None},
    {'firma': 'Alpet',        'benzin': 69.63, 'motorin': 81.27, 'lpg': None},
    {'firma': 'Aytemiz',      'benzin': 69.57, 'motorin': 81.31, 'lpg': None},
]


def fiyat_cek(il: str = 'elazig') -> dict:
    """
    İl için güncel akaryakıt fiyatlarını döner.

    Dönüş yapısı:
    {
        'markalar': [{'firma': str, 'benzin': float, 'motorin': float, 'lpg': float}, ...],
        'kaynak': str,
        'zaman': str (ISO 8601),
        'canli': bool,
    }
    """
    il = il.lower().strip()

    # 1. Taze cache var mı?
    cache = _cache_oku(il)
    if cache:
        logger.info('Oncbellekten donduruldu: %s / %s', il, cache['kaynak'])
        return {
            'markalar': cache['markalar'],
            'kaynak': f"{cache['kaynak']} (önbellek)",
            'zaman': cache['zaman'],
            'canli': False,
        }

    # 2. Çok-markalı kaynakları sırayla dene (biri tutarsa yeterli)
    cok_markali_sonuc = None
    cok_markali_kaynak_adi = ''
    for kaynak_adi, kaynak_fn in _COK_MARKALI_KAYNAKLAR:
        try:
            markalar, kaynak = kaynak_fn(il)
            if markalar:
                cok_markali_sonuc = markalar
                cok_markali_kaynak_adi = kaynak
                break
        except Exception as e:
            logger.error('%s kaynağında beklenmedik hata: %s', kaynak_adi, e)

    # 3. Tek-markalı resmi site scraper'larını PARALEL çalıştır ve birleştir.
    #    (Eskiden ilk başarılı olan kazanıyordu; bu yüzden örn. Opet
    #    başarılı olduğunda Shell/PetrolOfisi/TotalEnergies hiç denenmiyordu.)
    #    NOT: as_completed(..., timeout=N) kullanmıyoruz çünkü N saniye içinde
    #    HEPSİ bitmezse TimeoutError FIRLATIR ve bu da /api/fiyatlar'ı 500
    #    hatasına düşürüp frontend'de "Bağlantı hatası" gösterirdi. wait()
    #    asla hata fırlatmaz — o ana kadar bitenleri, bitmeyenleri ayrı ayrı
    #    döner, biz de sadece bitenleri kullanırız.
    tekli_sonuclar = []
    tekli_kaynak_adlari = []
    with ThreadPoolExecutor(max_workers=len(_TEK_MARKALI_KAYNAKLAR)) as havuz:
        gelecekler = {
            havuz.submit(fn, il): ad for ad, fn in _TEK_MARKALI_KAYNAKLAR
        }
        tamamlanan, tamamlanmayan = wait(gelecekler.keys(), timeout=18)
        for gelecek in tamamlanan:
            ad = gelecekler[gelecek]
            try:
                markalar, kaynak = gelecek.result()
                if markalar:
                    tekli_sonuclar.append(markalar)
                    tekli_kaynak_adlari.append(kaynak)
            except Exception as e:
                logger.error('%s kaynağında beklenmedik hata: %s', ad, e)
        if tamamlanmayan:
            logger.warning(
                '%d kaynak zaman aşımına uğradı (il=%s): %s',
                len(tamamlanmayan), il, [gelecekler[g] for g in tamamlanmayan],
            )

    tum_markalar = _markalari_birlestir(cok_markali_sonuc, *tekli_sonuclar)

    if tum_markalar:
        kaynak_etiketi = ' + '.join(filter(None, [cok_markali_kaynak_adi] + tekli_kaynak_adlari)) or 'Bilinmeyen'
        logger.info('Canlı veri alındı: %s — %s (%d marka)', il, kaynak_etiketi, len(tum_markalar))
        zaman = datetime.now().isoformat()
        _cache_yaz(il, tum_markalar, kaynak_etiketi)
        return {
            'markalar': tum_markalar,
            'kaynak': kaynak_etiketi,
            'zaman': zaman,
            'canli': True,
        }

    # 3b. Eski cache (TTL dolmuş olsa bile)
    eski = _eski_cache_oku(il)
    if eski:
        logger.warning('Tüm kaynaklar başarısız, eski cache kullanılıyor: %s', il)
        return {
            'markalar': eski['markalar'],
            'kaynak': f"{eski['kaynak']} (eski önbellek)",
            'zaman': eski['zaman'],
            'canli': False,
        }

    # 4. Son çare: kodlanmış yedek fiyatlar
    logger.warning('Yedek fiyatlar kullanılıyor: %s', il)
    return {
        'markalar': _YEDEK_FIYATLAR,
        'kaynak': 'Yedek veri (güncel olmayabilir)',
        'zaman': datetime.now().isoformat(),
        'canli': False,
    }


def cache_temizle(il: str | None = None) -> None:
    """Cache'i temizler. il=None ise tümünü siler."""
    try:
        if il is None:
            CACHE_DOSYASI.unlink(missing_ok=True)
        else:
            if CACHE_DOSYASI.exists():
                with open(CACHE_DOSYASI, 'r', encoding='utf-8') as f:
                    veri = json.load(f)
                veri.pop(il, None)
                with open(CACHE_DOSYASI, 'w', encoding='utf-8') as f:
                    json.dump(veri, f, ensure_ascii=False, indent=2)
    except Exception as e:
        logger.warning('Cache temizleme hatası: %s', e)
