#!/usr/bin/env python3
"""
OpenRouter Webhook Handler untuk Flask (PythonAnywhere Compatible)

Ini BUKAN server standalone — ini adalah modul yang di-import ke app.py Flask.
Webhook endpoint akan menjadi bagian dari aplikasi web yang sudah berjalan.

Cara pakai di PythonAnywhere:
    1. Simpan file ini di ~/mysite/api/openrouter_webhook_flask.py
    2. Import dan register di app.py:

       from api.openrouter_webhook_flask import register_openrouter_webhook
       register_openrouter_webhook(app)

    3. Webhook URL menjadi: https://yourdomain.pythonanywhere.com/openrouter/webhook
    4. Atur di Telegram Bot: setWebhook ke URL tersebut

Environment variables (dapat diatur dalam .env atau database Setting):
    OPENROUTER_API_KEY        - API key OpenRouter
    OPENROUTER_MODEL          - Model AI (default: openrouter/free)
    OPENROUTER_BASE_URL       - Base URL API
    SITE_URL                  - HTTP-Referer
    APP_NAME                  - X-Title
    OPENROUTER_WEBHOOK_SECRET - Secret untuk verifikasi signature
    TELEGRAM_BOT_TOKEN        - Token bot Telegram
    TELEGRAM_CHANNEL_ID       - ID chat untuk notifikasi
"""

import os
import sys
import json
import hmac
import hashlib
import logging
import re
from datetime import datetime

# Tambahkan parent directory ke path
sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

try:
    import requests
except ImportError:
    print("[ERROR] requests library tidak tersedia. Install: pip install requests")
    requests = None

logger = logging.getLogger(__name__)


def _get_setting(key: str, default: str = '') -> str:
    """Ambil setting: DB Setting dulu, fallback ke environment variable, baru default.

    FIXED (2026 Pro): sebelumnya fallback ke env var HANYA terjadi kalau query
    DB melempar exception (mis. DB tidak bisa diakses). Kalau DB bisa diakses
    tapi row-nya memang belum ada/masih kosong, fungsi langsung return
    `default` tanpa pernah mengecek env var -- padahal docstring modul ini
    sendiri bilang config bisa diatur lewat '.env ATAU database Setting'.
    Akibatnya: admin yang isi OPENROUTER_API_KEY/TELEGRAM_BOT_TOKEN/dll lewat
    env var PythonAnywhere (tapi belum isi baris yang sama di tabel Setting)
    akan selalu dapat 'API key not configured' walau env var-nya sudah benar.
    Sekarang env var selalu dicek sebagai fallback kalau DB kosong, konsisten
    dengan pola _api_key()/_base_url() di ai_service.py dan api.py.
    """
    try:
        from models import Setting
        s = Setting.query.filter_by(key=key).first()
        if s and s.value:
            return s.value
    except Exception:
        pass
    return os.getenv(key.upper(), default)


# ========== OPENROUTER AI ==========

def _call_openrouter_ai(messages, temperature=0.7, max_tokens=500):
    """Kirim request ke OpenRouter AI."""
    api_key = _get_setting('openrouter_api_key', '').strip()
    if not api_key:
        return {'success': False, 'error': 'API key not configured'}

    base_url = _get_setting('openrouter_base_url', 'https://openrouter.ai/api/v1').rstrip('/')
    model = _get_setting('openrouter_model', 'openrouter/free')
    site_url = _get_setting('openrouter_site_url', 'https://slotriset.pythonanywhere.com')
    app_name = _get_setting('openrouter_app_name', 'Pusat Gadai Indonesia')

    headers = {
        'Content-Type': 'application/json',
        'Authorization': f'Bearer {api_key}',
        'HTTP-Referer': site_url,
        'X-Title': app_name
    }

    body = {
        'model': model,
        'messages': messages,
        'stream': False,
        'temperature': temperature,
        'max_tokens': max_tokens
    }

    try:
        response = requests.post(
            f"{base_url}/chat/completions",
            headers=headers,
            json=body,
            timeout=30
        )

        if response.status_code == 200:
            result = response.json()
            choices = result.get('choices', [])
            if choices and choices[0].get('message'):
                return {
                    'success': True,
                    'reply': choices[0]['message'].get('content', ''),
                    'model': result.get('model', model),
                    'usage': result.get('usage', {})
                }
            return {'success': False, 'error': 'Empty AI response'}
        elif response.status_code == 401:
            return {'success': False, 'error': 'Invalid API key'}
        elif response.status_code == 429:
            return {'success': False, 'error': 'Rate limit exceeded'}
        else:
            return {'success': False, 'error': f'HTTP {response.status_code}'}

    except requests.exceptions.ConnectionError:
        return {'success': False, 'error': 'Cannot connect to OpenRouter'}
    except requests.exceptions.Timeout:
        return {'success': False, 'error': 'Request timeout'}
    except Exception as e:
        logger.error(f"[OpenRouter] Error: {e}")
        return {'success': False, 'error': str(e)}


def _extract_action(ai_reply):
    """Ekstrak aksi dari respons AI."""
    match = re.search(r'\[AKSI:([^\]]+)\]', ai_reply)
    if not match:
        return None, {}

    parts = match.group(1).split('|')
    action_name = parts[0].strip()
    params = {}

    for part in parts[1:]:
        if '=' in part:
            k, v = part.split('=', 1)
            params[k.strip()] = v.strip()

    return action_name, params


# ========== TELEGRAM ==========

def _send_telegram(chat_id, text):
    """Kirim pesan ke Telegram."""
    token = _get_setting('telegram_bot_token', '')
    if not token:
        return False

    target = chat_id or _get_setting('telegram_chat_id', '')
    if not target:
        return False

    try:
        response = requests.post(
            f"https://api.telegram.org/bot{token}/sendMessage",
            json={'chat_id': target, 'text': text, 'parse_mode': 'HTML'},
            timeout=10
        )
        return response.json().get('ok', False)
    except Exception as e:
        logger.error(f"[Telegram] Error: {e}")
        return False


# ========== EVENT HANDLERS ==========

def _handle_manual(event_type, payload):
    """Fallback manual processing."""
    if event_type == 'ping':
        return {'status': 'pong', 'timestamp': datetime.utcnow().isoformat()}

    elif event_type == 'new_order':
        order_num = payload.get('order_number', 'N/A')
        if _get_setting('telegram_bot_token'):
            msg = (
                f"🛒 <b>Pesanan Baru</b>\n"
                f"Order: {order_num}\n"
                f"Nama: {payload.get('customer_name', 'N/A')}\n"
                f"Total: {payload.get('total', 'N/A')}"
            )
            _send_telegram(None, msg)
        return {'status': 'notified'}

    elif event_type == 'order_status_update':
        order_num = payload.get('order_number', 'N/A')
        status = payload.get('status', 'N/A')
        if _get_setting('telegram_bot_token'):
            _send_telegram(None, f"🔄 Order <b>{order_num}</b> status: {status}")
        return {'status': 'notified'}

    elif event_type == 'command':
        command = payload.get('command')
        params = payload.get('params', {})

        if command == 'ping':
            return {'status': 'pong'}
        elif command == 'sync_products':
            return {'status': 'ok', 'message': 'Sync initiated'}
        elif command == 'health_check':
            return {'status': 'ok'}
        elif command == 'test_notification':
            _send_telegram(None, '✅ Test notification dari OpenRouter')
            return {'status': 'ok'}
        elif command == 'create_product':
            name = params.get('value', 'Produk Baru')
            _send_telegram(None, f"🆕 Produk: <b>{name}</b>")
            return {'status': 'ok'}
        elif command == 'generate_invoice':
            inv_id = params.get('value', 'INV-001')
            _send_telegram(None, f"📄 Invoice <b>{inv_id}</b>")
            return {'status': 'ok'}
        elif command == 'generate_redemption_letter':
            red_id = params.get('value', 'INV-001')
            _send_telegram(None, f"📜 Surat penebusan <b>{red_id}</b>")
            return {'status': 'ok'}
        elif command == 'send_telegram':
            chat_id = params.get('chat_id')
            text = params.get('text', 'Test')
            success = _send_telegram(chat_id, text)
            return {'status': 'ok' if success else 'error'}
        else:
            return {'status': 'error', 'message': f'Unknown: {command}'}

    return {'status': 'received'}


def _handle_with_ai(event_type, payload):
    """Proses event dengan OpenRouter AI."""
    system_prompt = (
        "Anda adalah webhook processor toko online. "
        "Analisis event, tentukan aksi. "
        "Format: [AKSI:aksi|param=value]. "
        "Aksi: notify_admin, sync_data, update_status, log_only, generate_invoice, generate_redemption."
    )

    user_message = f"Event: {event_type}\nPayload: {json.dumps(payload, indent=2, default=str)}"

    ai_result = _call_openrouter_ai([
        {'role': 'system', 'content': system_prompt},
        {'role': 'user', 'content': user_message}
    ])

    if not ai_result['success']:
        return _handle_manual(event_type, payload)

    ai_reply = ai_result['reply']
    action, params = _extract_action(ai_reply)

    action_result = ''
    if action == 'notify_admin':
        clean = re.sub(r'\[AKSI:[^\]]+\]\s*', '', ai_reply).strip()
        _send_telegram(None, clean or 'Notifikasi')
        action_result = 'Notified'
    elif action == 'generate_invoice':
        oid = params.get('order_id', 'N/A')
        _send_telegram(None, f"📄 Invoice <b>{oid}</b>")
        action_result = f'Invoice {oid}'
    elif action == 'generate_redemption':
        rid = params.get('order_id', 'N/A')
        _send_telegram(None, f"📜 Surat penebusan <b>{rid}</b>")
        action_result = f'Redemption {rid}'
    else:
        action_result = 'Logged'

    return {
        'status': 'processed',
        'action': action or 'log_only',
        'ai_message': re.sub(r'\[AKSI:[^\]]+\]\s*', '', ai_reply).strip(),
        'action_result': action_result,
        'model': ai_result.get('model'),
        'tokens': ai_result.get('usage', {}).get('total_tokens', 0)
    }


# ========== FLASK REGISTRATION ==========

def register_openrouter_webhook(app):
    """Register webhook endpoint ke Flask app.

    FIXED (2026 Pro): app.py sudah punya route '/openrouter/webhook' sendiri
    (didaftarkan lewat create_webhook_route() dari api/webhook_handler.py,
    atau stub fallback-nya). Kalau modul ini di-import & register_openrouter_
    webhook(app) dipanggil sesuai instruksi di docstring atas file ini,
    Flask akan crash saat startup dengan:
        AssertionError: View function mapping is overwriting an existing
        endpoint function: openrouter_webhook
    ...yang berarti SELURUH aplikasi gagal load (sama fatalnya dengan bug
    duplicate-table di models_chat.py yang sudah diperbaiki sebelumnya).
    Sekarang dicek dulu apakah endpoint 'openrouter_webhook' sudah terdaftar
    sebelum mendaftarkan ulang -> aman dipanggil dobel, cukup di-skip &
    di-log kalau sudah ada implementasi lain yang aktif.
    """
    from flask import request, jsonify

    if 'openrouter_webhook' in app.view_functions:
        logger.warning(
            "register_openrouter_webhook() dilewati: endpoint 'openrouter_webhook' "
            "sudah terdaftar (kemungkinan lewat create_webhook_route() di "
            "api/webhook_handler.py). Hindari duplicate-route crash."
        )
        print(
            "[OpenRouter Webhook] Dilewati - endpoint '/openrouter/webhook' "
            "sudah terdaftar oleh modul lain."
        )
        return

    @app.route('/openrouter/webhook', methods=['POST'])
    @app.route('/openclaw/webhook', methods=['POST'])  # Backward compat
    def openrouter_webhook():
        # Verifikasi signature
        secret = _get_setting('openrouter_webhook_secret', '')
        signature = request.headers.get('X-OpenRouter-Signature') or request.headers.get('X-OpenClaw-Signature')
        raw_data = request.get_data()

        if secret:
            expected = hmac.new(secret.encode(), raw_data, hashlib.sha256).hexdigest()
            if not hmac.compare_digest(expected, signature or ''):
                return jsonify({'error': 'Invalid signature'}), 401

        # Parse JSON
        data = request.get_json(force=True)
        if not data:
            return jsonify({'error': 'Invalid JSON'}), 400

        event_type = data.get('event')
        if not event_type:
            return jsonify({'error': "Missing 'event'"}), 400

        # Process
        try:
            result = _handle_with_ai(event_type, data.get('data', {}))
            return jsonify(result), 200
        except Exception as e:
            logger.error(f"[Webhook] Error: {e}")
            return jsonify({'error': 'Internal error'}), 500

    if 'openrouter_webhook_health' in app.view_functions:
        return

    @app.route('/openrouter/health', methods=['GET'])
    def openrouter_webhook_health():
        """Health check endpoint."""
        return jsonify({
            'status': 'ok',
            'service': 'OpenRouter Webhook',
            'openrouter_configured': bool(_get_setting('openrouter_api_key')),
            'telegram_configured': bool(_get_setting('telegram_bot_token'))
        }), 200

    logger.info("OpenRouter webhook registered at /openrouter/webhook")
    print("[OpenRouter Webhook] Registered at /openrouter/webhook")
    print("[OpenRouter Webhook] Health check at /openrouter/health")


# ========== STANDALONE TEST ==========

if __name__ == '__main__':
    print("[INFO] Ini adalah modul Flask, bukan server standalone.")
    print("[INFO] Cara pakai:")
    print("  1. Import: from api.openrouter_webhook_flask import register_openrouter_webhook")
    print("  2. Register: register_openrouter_webhook(app)")
    print("  3. URL: https://yourdomain.pythonanywhere.com/openrouter/webhook")