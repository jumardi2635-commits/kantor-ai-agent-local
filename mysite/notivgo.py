#!/usr/bin/env python3
"""
notivgo.py — Integrasi pembayaran otomatis NotivGo (QRIS dinamis + pencocokan notifikasi) untuk Flask.

PENTING: NotivGo BUKAN payment gateway. Uang masuk langsung ke rekening/QRIS merchant;
NotivGo hanya membuat QRIS dinamis dan mencocokkan notifikasi pembayaran dari HP Android
(aplikasi NotivGo, "Pencocokan Pembayaran" aktif). Karena itu:
  * Pesanan ditandai Lunas HANYA jika GET /v1/orders/<ref> mengembalikan status "paid".
  * Webhook dari HP hanyalah pemicu untuk mengecek status; polling halaman bayar juga mengecek.

Dipasang lewat:
    register_notivgo(app, db, Order, NotificationService, csrf_protect_endpoint,
                     logger, retry_session, get_setting)

Endpoint:
    POST /api/payment/notivgo/create/<order_id>   -> buat QRIS dinamis, balikan {success, pay_url}
    GET  /pay/notivgo/<order_id>                  -> halaman QR (notivgo_pay.html)
    GET  /api/payment/notivgo/status/<order_id>   -> {success, paid, status} (sinkron ke NotivGo)
    POST /api/notivgo/webhook                     -> webhook dari HP (X-Signature HMAC-SHA256)
    POST /admin/api/payment/notivgo/test          -> tes API key

Konfigurasi (.env / Pengaturan admin — JANGAN hardcode):
    NOTIVGO_API_KEY           Kunci API "ngk_..."
    NOTIVGO_WEBHOOK_SECRET    Kunci rahasia di portal -> Tujuan kiriman
    NOTIVGO_BASE_URL          Opsional, default https://portal.notivgo.com
    NOTIVGO_OUTLET_ID         Opsional
    NOTIVGO_QRIS_ID           Opsional
    NOTIVGO_AUTO_CONFIRM_MAX  Opsional, rupiah. Jika >0, pesanan di atas nilai ini TIDAK
                              dikonfirmasi otomatis (harus dicek manual oleh admin).
    NOTIVGO_PROXY             Opsional. Proxy KHUSUS untuk request NotivGo saja
                              (format: http://user:pass@host:port atau socks5://host:port).
                              Request keluar lain di aplikasi (OpenRouter, Telegram,
                              WhatsApp, dll.) TIDAK terpengaruh.
                              Bisa juga diisi via setting admin key 'notivgo_proxy'.
"""
import calendar
import hashlib
import hmac
import json
import os
import random
import time
import types
from datetime import datetime

import requests
from flask import jsonify, render_template, request, redirect, url_for
from flask_login import current_user, login_required

PROVIDER = 'notivgo'
PROXY_MSG = ('Koneksi ke NotivGo diblokir oleh proxy hosting (mis. PythonAnywhere Free). '
             'Ajukan whitelist domain portal.notivgo.com, upgrade paket hosting, '
             'ATAU konfigurasikan proxy khusus NotivGo lewat env var NOTIVGO_PROXY '
             '/ setting admin notivgo_proxy (format: http://user:pass@host:port '
             'atau socks5://host:port — socks5 butuh pip install requests[socks]).')


def register_notivgo(app, db, Order, NotificationService, csrf_protect_endpoint,
                     logger, retry_session=None, get_setting=None):
    if retry_session is None:
        retry_session = requests.Session()

    # ------------------------------------------------------------------
    # Config helpers
    # ------------------------------------------------------------------
    def _cfg(name):
        if get_setting:
            try:
                val = get_setting(name.lower(), '') or get_setting(name, '')
                if val:
                    return str(val).strip()
            except Exception:
                pass
        return (os.getenv(name) or '').strip()

    def _base_url():
        return (_cfg('NOTIVGO_BASE_URL') or 'https://portal.notivgo.com').rstrip('/')

    def _headers():
        return {'Authorization': f"Bearer {_cfg('NOTIVGO_API_KEY')}",
                'Content-Type': 'application/json'}

    def _proxies_for_requests():
        """Return dict proxies HANYA untuk request NotivGo, atau None.

        Dipakai per-request (bukan session.proxies) supaya request keluar lain
        di aplikasi ini — OpenRouter, Telegram, WhatsApp, dll. — tetap langsung,
        tidak ikut lewat proxy. Prioritas: setting admin 'notivgo_proxy' →
        env var NOTIVGO_PROXY. Kalau kosong, return None sehingga requests
        memakai default (env HTTP_PROXY/HTTPS_PROXY jika ada).
        """
        proxy_url = _cfg('NOTIVGO_PROXY')
        if not proxy_url:
            return None
        return {'http': proxy_url, 'https': proxy_url}

    def _is_enabled():
        if get_setting:
            val = get_setting('payment_notivgo_enabled', '')
            if val != '':
                return str(val).lower() in ('1', 'true', 'yes')
        return bool(_cfg('NOTIVGO_API_KEY'))

    def _is_admin():
        return current_user.is_authenticated and current_user.__class__.__name__ == 'Admin'

    def _get_own_order(order_id):
        order = Order.query.get(order_id)
        if not order:
            return None, (jsonify({'success': False, 'message': 'Pesanan tidak ditemukan'}), 404)
        if not _is_admin() and order.user_id and order.user_id != getattr(current_user, 'id', None):
            return None, (jsonify({'success': False, 'message': 'Bukan pesanan Anda'}), 403)
        return order, None

    if not _cfg('NOTIVGO_API_KEY'):
        logger.warning('NotivGo: NOTIVGO_API_KEY belum diisi — pembuatan QRIS akan gagal.')
    if not _cfg('NOTIVGO_WEBHOOK_SECRET'):
        logger.warning('NotivGo: NOTIVGO_WEBHOOK_SECRET belum diisi — webhook akan ditolak '
                       '(status tetap bisa dicek lewat polling).')
    if _cfg('NOTIVGO_PROXY'):
        # Log proxy tanpa membocorkan password
        import re as _re
        _safe = _re.sub(r'//([^:@/]+):[^@/]+@', r'//\1:***@', _cfg('NOTIVGO_PROXY'))
        logger.info('NotivGo: proxy aktif khusus NotivGo (%s). Request lain tetap langsung.', _safe)

    def _api(method, path, **kw):
        """Panggil API NotivGo. Return (response|None, error_message|None)."""
        proxies = _proxies_for_requests()
        if proxies:
            # Per-request proxies menimpa session.proxies / env vars hanya untuk
            # panggilan ini — tidak bocor ke request lain di aplikasi.
            kw['proxies'] = proxies

        try:
            resp = retry_session.request(method, f'{_base_url()}{path}', headers=_headers(),
                                         timeout=kw.pop('timeout', 15), **kw)
            return resp, None
        except Exception as e:
            msg = str(e)
            logger.error('NotivGo: gagal menghubungi API: %s', msg)
            # Deteksi kegagalan spesifik-proxy agar pesan ke admin actionable.
            if proxies and any(s in msg for s in (
                    'ProxyError', 'Tunnel connection failed',
                    'Proxy Authentication Required', '407')):
                return None, ('Koneksi ke NotivGo melalui proxy gagal. Periksa '
                              'format/kredensial NOTIVGO_PROXY.')
            if 'Tunnel connection failed: 403' in msg or 'ProxyError' in msg:
                return None, PROXY_MSG
            return None, 'Tidak dapat menghubungi NotivGo. Coba lagi sesaat lagi.'

    def _json(resp):
        try:
            body = resp.json()
            return body if isinstance(body, dict) else {}
        except ValueError:
            return {}

    # ------------------------------------------------------------------
    # Jumlah & kode unik
    # ------------------------------------------------------------------
    def _base_rupiah(order):
        # Order.total disimpan dalam SEN
        return int((order.total or 0) // 100)

    def _amount(order):
        return _base_rupiah(order) + int(order.payment_unique_code or 0)

    def _pick_unique_code(order):
        """Kode 1..99 supaya nominal unik di antara pesanan NotivGo yang masih Pending."""
        base = _base_rupiah(order)
        used = set()
        for o in Order.query.filter(Order.status == 'Pending',
                                    Order.payment_provider == PROVIDER,
                                    Order.id != order.id).all():
            used.add(_amount(o))
        codes = list(range(1, 100))
        random.shuffle(codes)
        for c in codes:
            if base + c not in used:
                return c
        return random.randint(1, 99)

    def _expires_in(order):
        try:
            hours = float(get_setting('payment_deadline_hours', 24)) if get_setting else 24
        except Exception:
            hours = 24
        return max(300, int(hours * 3600))

    # ------------------------------------------------------------------
    # Tandai lunas (atomik & idempoten)
    # ------------------------------------------------------------------
    def _mark_paid(order, source):
        max_auto = 0
        try:
            max_auto = int(_cfg('NOTIVGO_AUTO_CONFIRM_MAX') or 0)
        except ValueError:
            pass
        if max_auto and _amount(order) > max_auto:
            logger.warning('NotivGo: order %s Rp%s melebihi batas konfirmasi otomatis — cek mutasi manual.',
                           order.order_number, _amount(order))
            return False

        now = datetime.utcnow()
        try:
            n = (Order.query.filter_by(id=order.id, status='Pending')
                 .update({'status': 'Paid', 'paid_at': now, 'updated_at': now}))
            db.session.commit()
        except Exception as e:
            db.session.rollback()
            logger.error('NotivGo: gagal update order %s: %s', order.id, e)
            return False
        if not n:
            return False  # sudah diproses
        try:
            db.session.refresh(order)
            NotificationService.notify_status_update(order, old_status='Pending', new_status='Paid')
        except Exception as e:
            logger.error('NotivGo: notifikasi gagal: %s', e)
        logger.info('NotivGo: order %s lunas (%s)', order.order_number, source)
        return True

    def _sync_order(order, source='poll'):
        """Cek status ke NotivGo. Return status string ('open','paid',...) atau None."""
        ref = order.payment_ref or order.order_number
        resp, err = _api('GET', f'/v1/orders/{ref}', timeout=10)
        if resp is None or resp.status_code != 200:
            return None
        body = _json(resp)
        data = body.get('order', body) if isinstance(body.get('order'), dict) else body
        status = str(data.get('status') or '').lower()
        # needs_review BUKAN lunas; hanya status yang dihitung
        if status == 'paid' and order.status == 'Pending':
            _mark_paid(order, source)
        return status

    def _remote_cancel(order):
        """Best-effort: batalkan order di NotivGo (dipanggil saat admin membatalkan)."""
        if order.payment_provider != PROVIDER or not _cfg('NOTIVGO_API_KEY'):
            return False
        ref = order.payment_ref or order.order_number
        resp, _ = _api('POST', f'/v1/orders/{ref}/cancel', timeout=8)
        return bool(resp is not None and resp.status_code in (200, 201))

    # ------------------------------------------------------------------
    # Buat QRIS
    # ------------------------------------------------------------------
    def _generate(order, fmt='payload'):
        """Panggil /v1/generate (idempoten per order_ref). Return (data|None, error, http)."""
        payload = {
            'order_ref': order.order_number,
            'amount': _amount(order),
            'currency': 'IDR',
            'format': fmt,
            'create_order': True,
            'expires_in': _expires_in(order),
            'note': f'Pesanan {order.order_number}',
        }
        if _cfg('NOTIVGO_OUTLET_ID'):
            payload['outlet_id'] = _cfg('NOTIVGO_OUTLET_ID')
        if _cfg('NOTIVGO_QRIS_ID'):
            payload['qris_id'] = _cfg('NOTIVGO_QRIS_ID')

        resp, err = _api('POST', '/v1/generate', json=payload)
        if resp is None:
            return None, err, 502
        body = _json(resp)
        if resp.status_code in (200, 201):
            return body, None, 200
        code = body.get('error') or body.get('code') or ''
        logger.error('NotivGo: generate ditolak (%s): %s', resp.status_code, resp.text[:300])
        if resp.status_code == 401:
            return None, 'API Key NotivGo tidak valid. Hubungi admin.', 502
        if resp.status_code == 402:
            return None, 'Kuota NotivGo habis. Hubungi admin.', 502
        if resp.status_code == 403:
            return None, 'Akun NotivGo tidak aktif. Hubungi admin.', 502
        if resp.status_code == 409:
            return None, f'Konflik data tagihan ({code}). Hubungi admin.', 409
        if resp.status_code == 404:
            return None, 'QRIS belum disimpan di portal NotivGo. Hubungi admin.', 502
        if resp.status_code == 429:
            return None, 'Terlalu banyak permintaan. Coba lagi sebentar.', 429
        return None, 'Gagal membuat QRIS pembayaran.', 502

    def _ensure_invoice(order):
        """Pastikan QRIS sudah dibuat untuk order. Return (ok, message, http_status)."""
        if order.payment_provider == PROVIDER and order.payment_url:
            return True, None, 200
        if not _cfg('NOTIVGO_API_KEY'):
            return False, 'Konfigurasi pembayaran belum lengkap. Hubungi admin.', 500
        if _base_rupiah(order) <= 0:
            return False, 'Total pesanan tidak valid.', 400
        try:
            if not order.payment_unique_code:
                order.payment_unique_code = _pick_unique_code(order)
            db.session.commit()
        except Exception as e:
            db.session.rollback()
            logger.error('NotivGo: gagal menyimpan kode unik order %s: %s', order.id, e)
            return False, 'Gagal menyiapkan tagihan.', 500

        data, msg, http = _generate(order, 'payload')
        if data is None:
            return False, msg, http
        try:
            order.payment_provider = PROVIDER
            order.payment_ref = order.order_number
            order.payment_url = url_for('notivgo_pay_page', order_id=order.id)
            db.session.commit()
        except Exception as e:
            db.session.rollback()
            logger.error('NotivGo: gagal menyimpan tagihan order %s: %s', order.id, e)
            return False, 'Gagal menyimpan tagihan.', 500
        return True, None, 200

    @app.route('/api/payment/notivgo/create/<int:order_id>', methods=['POST'])
    @login_required
    @csrf_protect_endpoint
    def notivgo_create(order_id):
        if not _is_enabled():
            return jsonify({'success': False, 'message': 'Pembayaran NotivGo tidak aktif.'}), 400
        order, err = _get_own_order(order_id)
        if err:
            return err
        if order.status != 'Pending':
            return jsonify({'success': False, 'message': 'Pesanan ini tidak menunggu pembayaran.'}), 400
        reused = order.payment_provider == PROVIDER and bool(order.payment_url)
        ok, msg, http = _ensure_invoice(order)
        if not ok:
            return jsonify({'success': False, 'message': msg}), http
        return jsonify({'success': True, 'pay_url': order.payment_url,
                        'total_amount': _amount(order), 'reused': reused})

    # ------------------------------------------------------------------
    # Halaman QR
    # ------------------------------------------------------------------
    @app.route('/pay/notivgo/<int:order_id>', methods=['GET'])
    @login_required
    def notivgo_pay_page(order_id):
        order = Order.query.get_or_404(order_id)
        if not _is_admin() and order.user_id and order.user_id != getattr(current_user, 'id', None):
            return redirect(url_for('user_orders'))
        qr_png = qr_payload = error = None
        if order.status == 'Pending' and order.payment_provider != PROVIDER \
                and PROVIDER in (order.payment_method or '').lower() and _is_enabled():
            # Pesanan dari checkout: QRIS dibuat saat halaman ini pertama dibuka
            ok, msg, _h = _ensure_invoice(order)
            if not ok:
                error = msg
        if order.payment_provider != PROVIDER and not error:
            return redirect(url_for('user_orders'))
        if order.status == 'Pending' and order.payment_provider == PROVIDER:
            data, error, _http = _generate(order, 'png')   # idempoten, tidak ditagih ulang
            if data:
                qr_png = data.get('qris_png_base64')
                qr_payload = data.get('qris_payload')
        expires_at_ms = None
        try:
            if order.created_at:
                expires_at_ms = int((calendar.timegm(order.created_at.timetuple()) + _expires_in(order)) * 1000)
        except Exception:
            pass
        return render_template('notivgo_pay.html', order=order, amount=_amount(order),
                               expires_at_ms=expires_at_ms,
                               base_amount=_base_rupiah(order),
                               unique_code=int(order.payment_unique_code or 0),
                               qr_png=qr_png, qr_payload=qr_payload, error=error)

    # ------------------------------------------------------------------
    # Status (dipolling halaman bayar & orders.html)
    # ------------------------------------------------------------------
    @app.route('/api/payment/notivgo/status/<int:order_id>', methods=['GET'])
    @login_required
    def notivgo_status(order_id):
        order, err = _get_own_order(order_id)
        if err:
            return err
        remote = None
        if order.status == 'Pending' and order.payment_provider == PROVIDER:
            remote = _sync_order(order, 'poll')
            db.session.refresh(order)
        paid = order.status not in ('Pending', 'Cancelled', 'Returned')
        return jsonify({'success': True, 'paid': paid, 'status': order.status, 'remote_status': remote})

    # ------------------------------------------------------------------
    # Webhook (dikirim HP NotivGo)
    # ------------------------------------------------------------------
    def _verify_webhook(raw_body):
        secret = _cfg('NOTIVGO_WEBHOOK_SECRET')
        if not secret:
            return False, 'no_secret', None
        ts = (request.headers.get('X-Timestamp') or '').strip()
        nonce = (request.headers.get('X-Nonce') or '').strip()
        sig = (request.headers.get('X-Signature') or '').strip()
        if sig.lower().startswith('sha256='):
            sig = sig[7:]
        if not (ts and nonce and sig):
            return False, 'missing_headers', None
        try:
            ts_int = int(ts)
        except ValueError:
            return False, 'bad_timestamp', None
        now = int(time.time())
        if abs(now - ts_int) > 300:
            return False, 'clock_skew', now
        msg = f'{ts}.{nonce}.'.encode('utf-8') + raw_body
        expected = hmac.new(secret.encode('utf-8'), msg, hashlib.sha256).hexdigest()
        if not hmac.compare_digest(expected, sig.lower()):
            return False, 'invalid_signature', None
        return True, None, None

    @app.route('/api/notivgo/webhook', methods=['POST'])
    def notivgo_webhook():
        raw_body = request.get_data(cache=True)
        ok, reason, server_time = _verify_webhook(raw_body)
        if not ok:
            logger.warning('NotivGo webhook ditolak (%s) dari %s', reason, request.remote_addr)
            if reason == 'clock_skew':
                return jsonify({'error': 'clock_skew', 'server_time': server_time}), 401
            return jsonify({'error': reason}), 401

        event = request.headers.get('X-NotivGo-Event', '')
        if event == 'webhook.test':
            return jsonify({'status': 'ok', 'event': 'webhook.test'}), 200

        try:
            payload = json.loads(raw_body.decode('utf-8'))
        except (ValueError, UnicodeDecodeError):
            return jsonify({'error': 'bad_request'}), 400
        if not isinstance(payload, dict):
            return jsonify({'error': 'bad_request'}), 400

        amount = payload.get('amount')
        direction = payload.get('direction')
        if direction != 'in' or not isinstance(amount, (int, float)) or amount <= 0:
            return jsonify({'status': 'ignored'}), 200

        amount = int(amount)
        pending = Order.query.filter_by(status='Pending', payment_provider=PROVIDER).all()
        candidates = [o for o in pending if _amount(o) == amount]
        if not candidates:
            return jsonify({'status': 'no_match'}), 200

        # Webhook bisa tiba sebelum pencocokan di server NotivGo -> coba beberapa kali singkat.
        for attempt in range(4):
            for o in candidates:
                if o.status == 'Pending':
                    _sync_order(o, 'webhook')
                    db.session.refresh(o)
            if all(o.status != 'Pending' for o in candidates):
                break
            time.sleep(1.5)
        # Selalu 2xx agar tidak di-retry terus; polling akan menuntaskan sisanya.
        return jsonify({'status': 'ok'}), 200

    # ------------------------------------------------------------------
    # Test koneksi (Admin)
    # ------------------------------------------------------------------
    @app.route('/admin/api/payment/notivgo/test', methods=['POST'])
    @login_required
    def notivgo_test():
        if not _is_admin():
            return jsonify({'success': False, 'message': 'Akses ditolak'}), 403
        if not _cfg('NOTIVGO_API_KEY'):
            return jsonify({'success': False,
                            'message': 'API Key belum diisi. Masukkan NotivGo API Key terlebih dahulu.'}), 400

        resp, err = _api('GET', '/v1/orders', params={'limit': 1}, timeout=12)
        if resp is None:
            is_proxy_cfg_err = bool(err and 'proxy' in err.lower() and 'format/kredensial' in err)
            return jsonify({'success': False, 'is_proxy_blocked': err == PROXY_MSG,
                            'is_proxy_misconfigured': is_proxy_cfg_err,
                            'message': ('🚫 ' + err) if err in (PROXY_MSG,) or is_proxy_cfg_err else err}), 502

        # Indikator proxy aktif di pesan sukses
        proxy_note = ''
        if _cfg('NOTIVGO_PROXY'):
            proxy_note = ' [NotivGo proxy aktif]'

        extra = '' if _cfg('NOTIVGO_WEBHOOK_SECRET') else (
            ' (Catatan: NOTIVGO_WEBHOOK_SECRET belum diisi — webhook dari HP akan ditolak.)')

        if resp.status_code == 200:
            return jsonify({'success': True,
                            'message': '✅ Koneksi NotivGo berhasil!' + extra + proxy_note})
        if resp.status_code == 401:
            return jsonify({'success': False, 'message': '❌ NotivGo API Key tidak valid (401).'})
        if resp.status_code == 403:
            return jsonify({'success': False, 'is_subscription_issue': True,
                            'message': '⚠️ Terhubung ke NotivGo, tetapi akun tidak aktif (403).'})
        if resp.status_code == 503:
            return jsonify({'success': False, 'message': 'NotivGo sedang maintenance (503).'})
        return jsonify({'success': False,
                        'message': f'NotivGo merespons status {resp.status_code}: {resp.text[:200]}'})

    return types.SimpleNamespace(cancel_remote=_remote_cancel, sync_order=_sync_order,
                                 verify_webhook=_verify_webhook)