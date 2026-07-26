const HEADERS = {
  'User-Agent': 'Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/124.0.0.0 Safari/537.36',
  'Accept': 'text/html,application/xhtml+xml,*/*',
  'Accept-Language': 'tr-TR,tr;q=0.9',
};

function parseFiyat(s) {
  if (!s || s.trim() === '-') return null;
  const v = parseFloat(s.replace(',', '.').replace(/[^\d.]/g, ''));
  return (v > 10 && v < 500) ? v : null;
}

function htmlDecode(s) {
  return s
    .replace(/&#x20BA;/g, '').replace(/&#x130;/g, 'İ').replace(/&#x131;/g, 'ı')
    .replace(/&#x11F;/g, 'ğ').replace(/&#x11E;/g, 'Ğ').replace(/&#x15F;/g, 'ş')
    .replace(/&#x15E;/g, 'Ş').replace(/&#xFC;/g, 'ü').replace(/&#xDC;/g, 'Ü')
    .replace(/&#xF6;/g, 'ö').replace(/&#xD6;/g, 'Ö').replace(/&#xE7;/g, 'ç')
    .replace(/&#xC7;/g, 'Ç').replace(/&amp;/g, '&').trim();
}

// Ana sayfadan tüm illerin ortalama fiyatını çek
async function tumIllerCek() {
  const r = await fetch('https://akaryakit.org/', { headers: HEADERS });
  if (!r.ok) return null;
  const html = await r.text();

  const tbodyIdx = html.indexOf('<tbody>');
  if (tbodyIdx === -1) return null;
  const tbodyHtml = html.substring(tbodyIdx + 7);

  const satirlar = tbodyHtml.split('<tr>').filter(s => s.includes('<td'));
  const iller = {};

  for (const satir of satirlar) {
    const tdler = [...satir.matchAll(/<td[^>]*>(.*?)(?=<td|<tr|$)/gi)]
      .map(m => htmlDecode(m[1].replace(/<[^>]+>/g, '').trim()));

    if (tdler.length < 3) continue;

    // İl adını href'ten al
    const hrefMatch = satir.match(/href=\/([a-z-]+)-akaryakit-fiyatlari/i);
    if (!hrefMatch) continue;
    const slug = hrefMatch[1];
    const ilAdi = tdler[0];
    const benzin = parseFiyat(tdler[1]);
    const motorin = parseFiyat(tdler[2]);
    const lpg = parseFiyat(tdler[3]);

    if (ilAdi && (benzin || motorin)) {
      iller[slug] = { il: ilAdi, benzin, motorin, lpg };
    }
  }

  return Object.keys(iller).length > 0 ? iller : null;
}

// İl detay sayfasından firma bazlı fiyat çek
async function ilDetayCek(slug) {
  const r = await fetch(`https://akaryakit.org/${slug}-akaryakit-fiyatlari`, { headers: HEADERS });
  if (!r.ok) return null;
  const html = await r.text();

  const tbodyIdx = html.indexOf('<tbody>');
  if (tbodyIdx === -1) return null;
  const tbodyHtml = html.substring(tbodyIdx + 7);

  const satirlar = tbodyHtml.split('<tr>').filter(s => s.includes('<td'));
  const markalar = [];

  for (const satir of satirlar) {
    const tdler = [...satir.matchAll(/<td[^>]*>(.*?)(?=<td|<tr|<th|$)/gi)]
      .map(m => htmlDecode(m[1].replace(/<[^>]+>/g, '').trim()));

    if (tdler.length < 4) continue;
    const firma = tdler[0];
    const benzin = parseFiyat(tdler[1]);
    const lpg = parseFiyat(tdler[2]);
    const motorin = parseFiyat(tdler[3]);

    if (firma && firma.length > 1 && (benzin || motorin)) {
      markalar.push({ firma, benzin, motorin, lpg });
    }
  }

  return markalar.length > 0 ? markalar : null;
}

export default {
  async fetch(request) {
    if (request.method === 'OPTIONS') {
      return new Response(null, {
        headers: {
          'Access-Control-Allow-Origin': '*',
          'Access-Control-Allow-Methods': 'GET',
          'Access-Control-Allow-Headers': 'X-Worker-Secret',
        },
      });
    }

    const url = new URL(request.url);
    const il = (url.searchParams.get('il') || '').toLowerCase().trim();
    const mod = url.searchParams.get('mod') || 'detay'; // detay | tum

    try {
      // mod=tum → tüm illerin ortalama fiyatı
      if (mod === 'tum') {
        const iller = await tumIllerCek();
        if (iller) {
          return new Response(
            JSON.stringify({ basari: true, kaynak: 'akaryakit.org', zaman: new Date().toISOString(), iller }),
            { headers: { 'Content-Type': 'application/json;charset=UTF-8', 'Access-Control-Allow-Origin': '*' } }
          );
        }
      }

      // mod=detay (varsayılan) → il bazlı firma fiyatları
      const slug = il || 'elazig';
      const markalar = await ilDetayCek(slug);
      if (markalar) {
        return new Response(
          JSON.stringify({ basari: true, il: slug, kaynak: 'akaryakit.org', zaman: new Date().toISOString(), markalar }),
          { headers: { 'Content-Type': 'application/json;charset=UTF-8', 'Access-Control-Allow-Origin': '*' } }
        );
      }
    } catch(e) {
      return new Response(
        JSON.stringify({ basari: false, hata: e.message }),
        { status: 502, headers: { 'Content-Type': 'application/json;charset=UTF-8', 'Access-Control-Allow-Origin': '*' } }
      );
    }

    return new Response(
      JSON.stringify({ basari: false, hata: 'Veri alınamadı' }),
      { status: 502, headers: { 'Content-Type': 'application/json;charset=UTF-8', 'Access-Control-Allow-Origin': '*' } }
    );
  },
};
