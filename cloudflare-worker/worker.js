// Cloudflare Worker - Türkiye geneli Akaryakıt Fiyatları
// Kaynak: doviz.com
//
// KULLANIM: ?il=<il adı>&ilce=<ilçe adı> ile herhangi bir il/ilçe sorgulanabilir.
// Parametre verilmezse varsayılan olarak Elazığ/Merkez döner.
//
// ÖZEL DURUM - İSTANBUL: doviz.com İstanbul'u tek il olarak değil,
// "İstanbul Avrupa" ve "İstanbul Anadolu" diye İKİ AYRI il gibi ele alıyor.
// Bu yüzden İstanbul için normal "İstanbul" adı ÇALIŞMAZ - açıkça
// "İstanbul Avrupa" veya "İstanbul Anadolu" gönderilmeli
// (ör. ?il=İstanbul Anadolu&ilce=Kadıköy).

export default {
  async fetch(request) {
    if (request.method === "OPTIONS") {
      return new Response(null, { headers: corsHeaders() });
    }

    const url = new URL(request.url);
    const ilInput = url.searchParams.get("il") || "Elazığ";
    const ilceInput = url.searchParams.get("ilce") || "Merkez";

    const ilSlug = slugify(ilInput);
    const ilceSlug = slugify(ilceInput);

    try {
      const res = await fetch(
        `https://www.doviz.com/akaryakit-fiyatlari/${ilSlug}/${ilceSlug}`,
        {
          headers: {
            "User-Agent": "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 Chrome/120.0.0.0 Safari/537.36",
            "Accept": "text/html,application/xhtml+xml,application/xml;q=0.9,*/*;q=0.8",
            "Accept-Language": "tr-TR,tr;q=0.9",
          },
          cf: { cacheTtl: 0, cacheEverything: false },
        }
      );

      if (!res.ok) {
        return jsonResponse(
          { error: true, message: `HTTP ${res.status} - il/ilçe adını kontrol edin`, il: ilInput, ilce: ilceInput },
          500,
        );
      }

      const html = await res.text();
      const istasyonlar = parseAkaryakit(html);

      if (istasyonlar.length === 0) {
        return jsonResponse(
          { error: true, message: "Veri parse edilemedi (il/ilçe adı doviz.com'da bulunamamış olabilir)", il: ilInput, ilce: ilceInput },
          500,
        );
      }

      return jsonResponse({
        error: false,
        guncelleme: new Date().toISOString(),
        il: ilInput,
        ilce: ilceInput,
        istasyonlar,
      });

    } catch (err) {
      return jsonResponse({ error: true, message: err.message, il: ilInput, ilce: ilceInput }, 500);
    }
  },
};

/**
 * Türkçe bir il/ilçe adını doviz.com'un beklediği URL segmentine (slug)
 * çevirir: küçük harfe indirir, Türkçe karakterleri sadeleştirir, boşlukları
 * tire yapar. Örn: "Kırıkkale" -> "kirikkale", "İstanbul Anadolu" -> "istanbul-anadolu".
 * @param {string} text
 * @returns {string}
 */
function slugify(text) {
  const CHAR_MAP = {
    'ı': 'i', 'İ': 'i', 'I': 'i',
    'ğ': 'g', 'Ğ': 'g',
    'ü': 'u', 'Ü': 'u',
    'ş': 's', 'Ş': 's',
    'ö': 'o', 'Ö': 'o',
    'ç': 'c', 'Ç': 'c',
  };
  return text
    .trim()
    .split('')
    .map((ch) => CHAR_MAP[ch] ?? ch)
    .join('')
    .toLowerCase()
    .replace(/\s+/g, '-');
}

function parseAkaryakit(html) {
  const istasyonlar = [];

  const tbodyMatch = html.match(/<tbody>([\s\S]*?)<\/tbody>/i);
  if (!tbodyMatch) return istasyonlar;

  const tbody = tbodyMatch[1];
  const satirlar = tbody.split(/<tr>/).slice(1);

  for (const satir of satirlar) {
    const dagiticiMatch = satir.match(/<span class="ml-8">([^<]+)<\/span>/);
    if (!dagiticiMatch) continue;
    const dagitici = dagiticiMatch[1].trim();

    const fiyatlar = [];
    const fiyatRe = /<td class="text-bold p-12 text-center">([^<]*)<\/td>/g;
    let m;
    while ((m = fiyatRe.exec(satir)) !== null) {
      const deger = m[1].replace("₺", "").trim();
      fiyatlar.push(deger === "-" ? null : deger);
    }

    const tarihMatch = satir.match(/<td class="time p-12 text-center">([^<]+)<\/td>/);
    const tarih = tarihMatch ? tarihMatch[1].trim() : null;

    if (fiyatlar.length >= 2) {
      istasyonlar.push({
        dagitici,
        benzin:  fiyatlar[0] || null,
        motorin: fiyatlar[1] || null,
        lpg:     fiyatlar[2] || null,
        tarih,
      });
    }
  }

  return istasyonlar;
}

function corsHeaders() {
  return {
    "Access-Control-Allow-Origin": "*",
    "Access-Control-Allow-Methods": "GET, OPTIONS",
    "Access-Control-Allow-Headers": "Content-Type",
  };
}

function jsonResponse(data, status = 200) {
  return new Response(JSON.stringify(data, null, 2), {
    status,
    headers: {
      "Content-Type": "application/json; charset=utf-8",
      "Cache-Control": "no-store",
      ...corsHeaders(),
    },
  });
}
