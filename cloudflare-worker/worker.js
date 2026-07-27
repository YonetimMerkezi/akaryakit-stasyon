// Cloudflare Worker - Akaryakıt Fiyatları
// Kaynak: doviz.com
// Kullanım: ?il=elazig&ilce=merkez

export default {
  async fetch(request) {
    if (request.method === "OPTIONS") {
      return new Response(null, { headers: corsHeaders() });
    }

    const url = new URL(request.url);
    const il    = url.searchParams.get("il")    || "elazig";
    const ilce  = url.searchParams.get("ilce")  || "merkez";

    // Türkçe karakter → URL uyumlu slug
    const slug = (s) => s
      .toLowerCase()
      .replace(/ğ/g, "g").replace(/ü/g, "u").replace(/ş/g, "s")
      .replace(/ı/g, "i").replace(/ö/g, "o").replace(/ç/g, "c")
      .replace(/\s+/g, "-");

    const ilSlug   = slug(il);
    const ilceSlug = slug(ilce);
    const hedefUrl = `https://www.doviz.com/akaryakit-fiyatlari/${ilSlug}/${ilceSlug}`;

    try {
      const res = await fetch(hedefUrl, {
        headers: {
          "User-Agent": "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 Chrome/120.0.0.0 Safari/537.36",
          "Accept": "text/html,application/xhtml+xml,application/xml;q=0.9,*/*;q=0.8",
          "Accept-Language": "tr-TR,tr;q=0.9",
        },
      });

      if (!res.ok) {
        return jsonResponse({ error: true, message: `HTTP ${res.status}`, url: hedefUrl }, 500);
      }

      const html = await res.text();
      const istasyonlar = parseAkaryakit(html);

      if (istasyonlar.length === 0) {
        return jsonResponse({ error: true, message: "Veri parse edilemedi", url: hedefUrl }, 500);
      }

      // Sayfa başlığından il/ilçe adını al
      const baslikMatch = html.match(/<h1[^>]*class="page-title"[^>]*>([^<]+)<\/h1>/);
      const baslik = baslikMatch ? baslikMatch[1].trim() : `${il} / ${ilce}`;

      return jsonResponse({
        error: false,
        guncelleme: new Date().toISOString(),
        baslik,
        il: ilSlug,
        ilce: ilceSlug,
        istasyonlar,
      });

    } catch (err) {
      return jsonResponse({ error: true, message: err.message }, 500);
    }
  },
};

// ── Parser ────────────────────────────────────────────────────
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

// ── Yardımcı ─────────────────────────────────────────────────
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
      ...corsHeaders(),
    },
  });
}
