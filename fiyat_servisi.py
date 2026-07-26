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


# ─── Kaynak 0: Cloudflare Worker (akaryakit.org) ──────────────────────────────

def _worker_cek(il: str) -> tuple[list | None, str]:
    """
    Cloudflare Worker üzerinden akaryakit.org'dan il bazlı firma fiyatlarını çeker.
    Worker, CORS ve bot-koruması sorunlarını aşmak için proxy görevi görür.
    Ortam değişkeni: WORKER_URL (örn: https://xxx.workers.dev)
    """
    if not WORKER_URL:
        return None, ''
    try:
        slug = IL_SLUG_MAP.get(il.lower(), il.lower())
        r = requests.get(
            WORKER_URL,
            params={'il': slug, 'mod': 'detay'},
            timeout=10,
        )
        if r.status_code != 200:
            logger.warning('Worker HTTP %d', r.status_code)
            return None, ''

        data = r.json()
        if not data.get('basari'):
            logger.warning('Worker başarısız yanıt: %s', data.get('hata'))
            return None, ''

        markalar = []
        for m in data.get('markalar', []):
            benzin = m.get('benzin')
            motorin = m.get('motorin')
            lpg = m.get('lpg')
            firma = (m.get('firma') or '').strip()
            if firma and (benzin or motorin):
                markalar.append({
                    'firma': firma,
                    'benzin': benzin,
                    'motorin': motorin,
                    'lpg': lpg,
                })

        if markalar:
            return markalar, 'akaryakit.org (Worker)'
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


# ─── Ana servis fonksiyonu ────────────────────────────────────────────────────

_KAYNAKLAR = [
    ('Worker', _worker_cek),
    ('CollectAPI', _collectapi_cek),
    ('Opet', _opet_cek),
    ('Alpet', _alpet_cek),
    ('Shell', _shell_cek),
    ('PetrolOfisi', _petrolofisi_cek),
]

_YEDEK_FIYATLAR = [
    {'firma': 'Petrol Ofisi', 'benzin': 62.90, 'motorin': 65.98, 'lpg': 31.81},
    {'firma': 'Shell', 'benzin': 62.92, 'motorin': 65.98, 'lpg': 34.29},
    {'firma': 'Opet', 'benzin': 62.89, 'motorin': 65.95, 'lpg': 28.04},
    {'firma': 'TotalEnergies', 'benzin': 62.92, 'motorin': 65.98, 'lpg': 26.07},
    {'firma': 'Alpet', 'benzin': 62.88, 'motorin': 65.92, 'lpg': 26.07},
    {'firma': 'Aytemiz', 'benzin': 62.85, 'motorin': 65.90, 'lpg': 28.00},
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

    # 2. Kaynakları sırayla dene
    for kaynak_adi, kaynak_fn in _KAYNAKLAR:
        try:
            markalar, kaynak = kaynak_fn(il)
            if markalar:
                logger.info('Canlı veri alındı: %s — %s (%d marka)', il, kaynak, len(markalar))
                zaman = datetime.now().isoformat()
                _cache_yaz(il, markalar, kaynak)
                return {
                    'markalar': markalar,
                    'kaynak': kaynak,
                    'zaman': zaman,
                    'canli': True,
                }
        except Exception as e:
            logger.error('%s kaynağında beklenmedik hata: %s', kaynak_adi, e)

    # 3. Eski cache (TTL dolmuş olsa bile)
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
