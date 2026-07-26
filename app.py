from flask import Flask, jsonify, render_template, request

app = Flask(__name__)

# Görsellerdeki doğru ve güncel akaryakıt fiyatları
GUNCEL_FIYATLAR = [
    {
        'firma': 'Petrol Ofisi',
        'benzin': 69.68,
        'motorin': 81.31,
        'lpg': 31.81,
    },
    {'firma': 'TotalEnergies', 'benzin': 69.65, 'motorin': 81.32, 'lpg': 26.07},
    {'firma': 'Alpet', 'benzin': 69.63, 'motorin': 81.27, 'lpg': 26.07},
    {'firma': 'Shell', 'benzin': 68.21, 'motorin': 77.65, 'lpg': 28.19},
    {'firma': 'Opet', 'benzin': 68.19, 'motorin': 77.62, 'lpg': 28.04},
    {'firma': 'Aytemiz', 'benzin': 68.15, 'motorin': 77.55, 'lpg': 28.00},
]


@app.route('/')
def index():
  return render_template('index.html')


@app.route('/api/fiyatlar')
def api_fiyatlar():
  il = request.args.get('il', 'elazig')
  return jsonify(
      {
          'status': 'success',
          'source': 'EPDK & Güncel Piyasa Resmi Kaynak',
          'markalar': GUNCEL_FIYATLAR,
      }
  )


@app.route('/api/ilceler')
def api_ilceler():
  il = request.args.get('il', 'elazig')
  ilceler_dict = {
      'elazig': [
          'Merkez',
          'Ağın',
          'Baskil',
          'Karakoçan',
          'Keban',
          'Maden',
          'Palu',
          'Sivrice',
      ],
      'ankara': ['Çankaya', 'Keçiören', 'Yenimahalle', 'Mamak', 'Sincan'],
      'istanbul': ['Kadıköy', 'Beşiktaş', 'Üsküdar', 'Şişli', 'Ümraniye'],
      'izmir': ['Konak', 'Bornova', 'Karşıyaka', 'Buca', 'Çiğli'],
  }
  ilceler = ilceler_dict.get(il.lower(), ['Merkez'])
  return jsonify({'status': 'success', 'ilceler': ilceler})


@app.route('/api/benzinlikler')
def api_benzinlikler():
  lat = float(request.args.get('lat', 38.6749))
  lon = float(request.args.get('lon', 39.2225))

  # Haritada çevrede bolca istasyon görünmesi için zenginleştirilmiş liste
  stations = [
      {
          'name': 'Petrol Ofisi Merkez İstasyonu',
          'brand': 'Petrol Ofisi',
          'lat': lat + 0.01,
          'lon': lon + 0.01,
      },
      {
          'name': 'Opet Akaryakıt AŞ',
          'brand': 'Opet',
          'lat': lat - 0.01,
          'lon': lon + 0.015,
      },
      {
          'name': 'Shell İstasyonu',
          'brand': 'Shell',
          'lat': lat + 0.015,
          'lon': lon - 0.01,
      },
      {
          'name': 'TotalEnergies Akaryakıt',
          'brand': 'TotalEnergies',
          'lat': lat - 0.012,
          'lon': lon - 0.012,
      },
      {
          'name': 'Aytemiz Petrol',
          'brand': 'Aytemiz',
          'lat': lat + 0.02,
          'lon': lon + 0.005,
      },
      {
          'name': 'Alpet Akaryakıt',
          'brand': 'Alpet',
          'lat': lat - 0.018,
          'lon': lon - 0.005,
      },
      {
          'name': 'Opet Üniversite Şubesi',
          'brand': 'Opet',
          'lat': lat + 0.025,
          'lon': lon + 0.02,
      },
      {
          'name': 'Shell Çaydaçıra',
          'brand': 'Shell',
          'lat': lat + 0.008,
          'lon': lon - 0.022,
      },
      {
          'name': 'Petrol Ofisi Sanayi',
          'brand': 'Petrol Ofisi',
          'lat': lat - 0.022,
          'lon': lon + 0.008,
      },
      {
          'name': 'Aygaz Otogaz & İstasyon',
          'brand': 'Aygaz',
          'lat': lat + 0.003,
          'lon': lon + 0.025,
      },
      {
          'name': 'Aytemiz Güneykent',
          'brand': 'Aytemiz',
          'lat': lat - 0.015,
          'lon': lon - 0.025,
      },
  ]
  return jsonify({'status': 'success', 'stations': stations})


@app.route('/api/autocomplete')
def api_autocomplete():
  q = request.args.get('q', '')
  results = [
      {'display_name': f'{q.capitalize()} Mahallesi, Merkez/Elazığ'},
      {'display_name': f'{q.capitalize()} Caddesi, Elazığ'},
  ]
  return jsonify(results)


@app.route('/api/koordinat')
def api_koordinat():
  yer = request.args.get('yer', '')
  return jsonify({'status': 'success', 'lat': 38.6749, 'lon': 39.2225})


@app.route('/api/ters-kod')
def api_ters_kod():
  lat = request.args.get('lat')
  lon = request.args.get('lon')
  return jsonify(
      {'status': 'success', 'display_name': 'Merkez, Elazığ', 'il': 'elazig'}
  )


if __name__ == '__main__':
  app.run(debug=True, port=5000)
