# =============================================================================
# SHIPMENT API PROXY — GET /api/v1/shipments/<resi> (lacak resi + riwayat status)
# =============================================================================
# CARA PASANG: tempel blok ini di app.py, tepat SETELAH route
# `api_get_tracking` (bagian "# --- Shipping Tracking API ---"). Semua nama
# (app, Order, User, Admin, CircuitBreaker, rate_limit, logger, quote_plus, os,
# re, requests) sudah ter-import/terdefinisi di app.py, jadi tidak perlu import baru.
#
# Set di .env server (JANGAN di-commit, JANGAN ditaruh di JS/template):
#   SHIPMENT_API_BASE=https://<host-api-anda>
#   SHIPMENT_API_KEY=dmk_...
#   SHIPMENT_API_AUTH=bearer        # atau: x-api-key  (sesuai skema auth API)
#
# API key hanya hidup di server. Browser memanggil /api/shipments/<resi>
# milik Anda sendiri, lalu server yang meneruskan ke provider.

SHIPMENT_API_BASE = os.getenv('SHIPMENT_API_BASE', '').rstrip('/')
SHIPMENT_API_KEY = os.getenv('SHIPMENT_API_KEY', '')
SHIPMENT_API_AUTH = os.getenv('SHIPMENT_API_AUTH', 'bearer').strip().lower()
_RESI_RE = re.compile(r'^[A-Za-z0-9\-_]{4,64}$')


def _shipment_auth_headers() -> dict:
    headers = {'Accept': 'application/json'}
    if SHIPMENT_API_AUTH == 'x-api-key':
        headers['X-API-Key'] = SHIPMENT_API_KEY
    else:
        headers['Authorization'] = f'Bearer {SHIPMENT_API_KEY}'
    return headers


@app.route('/api/shipments/<resi>')
@login_required
@rate_limit(limit=30, window=60)   # jaga kuota API provider
def api_shipment_proxy(resi):
    if not _RESI_RE.match(resi):
        return jsonify({'success': False, 'message': 'Nomor resi tidak valid'}), 400

    # Otorisasi: Admin boleh melacak resi apa pun. Pelanggan (User) hanya resi
    # pesanannya sendiri. PENTING: id Admin dan User berasal dari tabel berbeda
    # dan bisa sama-sama bernilai 1, jadi filter user_id HANYA dipakai untuk
    # User — jangan diterapkan ke Admin, atau id admin bisa "cocok" dengan
    # pesanan pelanggan lain.
    if not isinstance(current_user, Admin):
        if not isinstance(current_user, User):
            return jsonify({'success': False, 'message': 'Akses ditolak'}), 403
        owned = Order.query.filter_by(
            tracking_number=resi, user_id=current_user.id
        ).first()
        if not owned:
            # 404 (bukan 403) supaya resi milik orang lain tidak bisa ditebak-tebak.
            return jsonify({'success': False, 'message': 'Resi tidak ditemukan'}), 404

    if not SHIPMENT_API_BASE or not SHIPMENT_API_KEY:
        logger.error('Shipment API belum dikonfigurasi (SHIPMENT_API_BASE / SHIPMENT_API_KEY)')
        return jsonify({'success': False, 'message': 'Layanan pelacakan belum dikonfigurasi'}), 503

    if CircuitBreaker.is_open('shipment_api'):
        return jsonify({'success': False, 'message': 'Layanan pelacakan sedang sibuk, coba lagi nanti'}), 503

    try:
        r = requests.get(
            f'{SHIPMENT_API_BASE}/api/v1/shipments/{quote_plus(resi)}',
            headers=_shipment_auth_headers(),
            timeout=(3, 8),   # (connect, read)
        )
    except requests.RequestException as e:
        CircuitBreaker.record_failure('shipment_api')
        logger.warning(f'Shipment API tidak dapat dihubungi: {e}')
        return jsonify({'success': False, 'message': 'Layanan pelacakan tidak dapat dihubungi'}), 502

    if r.status_code == 404:
        CircuitBreaker.record_success('shipment_api')   # 404 = jawaban sah, bukan gangguan
        return jsonify({'success': False, 'message': 'Resi belum terdaftar di sistem pelacakan'}), 404
    if r.status_code in (401, 403):
        logger.error('Shipment API menolak kredensial (HTTP %s) — periksa SHIPMENT_API_KEY / SHIPMENT_API_AUTH', r.status_code)
        return jsonify({'success': False, 'message': 'Layanan pelacakan error'}), 502
    if r.status_code == 429:
        return jsonify({'success': False, 'message': 'Terlalu banyak permintaan, coba lagi sebentar'}), 503
    if not r.ok:
        CircuitBreaker.record_failure('shipment_api')
        logger.warning(f'Shipment API HTTP {r.status_code}: {r.text[:200]}')
        return jsonify({'success': False, 'message': 'Layanan pelacakan error'}), 502

    try:
        payload = r.json()
    except ValueError:
        CircuitBreaker.record_failure('shipment_api')
        logger.warning('Shipment API mengembalikan respons non-JSON')
        return jsonify({'success': False, 'message': 'Respons layanan pelacakan tidak valid'}), 502

    CircuitBreaker.record_success('shipment_api')
    return jsonify({'success': True, 'data': payload})
