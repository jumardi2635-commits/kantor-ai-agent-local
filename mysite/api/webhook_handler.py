import hmac
import hashlib
import json
import logging
import os
import requests
from flask import request, jsonify

logger = logging.getLogger(__name__)

def _get_setting(key: str, default: str = '') -> str:
    """Ambil setting: DB Setting dulu, fallback ke environment variable, baru default.
    FIXED (2026 Pro): sebelumnya fallback ke env var tidak ada sama sekali --
    kalau row DB tidak ada, langsung return `default`. Sekarang konsisten
    dengan _api_key()/_base_url() di ai_service.py, api.py, dan
    openrouter_webhook_flask.py."""
    try:
        from models import Setting
        s = Setting.query.filter_by(key=key).first()
        if s and s.value:
            return s.value
    except Exception:
        pass
    return os.getenv(key.upper(), default)

def _get_openrouter_config() -> dict:
    """Ambil konfigurasi OpenRouter dari database."""
    return {
        'base_url': (_get_setting('openrouter_base_url') or 'https://openrouter.ai/api/v1').rstrip('/'),
        'api_key': (_get_setting('openrouter_api_key') or '').strip(),
        'model': (_get_setting('openrouter_model') or 'openrouter/free').strip(),
        'site_url': (_get_setting('openrouter_site_url') or 'https://slotriset.pythonanywhere.com').strip(),
        'app_name': (_get_setting('openrouter_app_name') or 'Pusat Gadai Indonesia').strip(),
    }

def _call_openrouter_ai(messages: list, temperature: float = 0.7, max_tokens: int = 500) -> dict:
    """
    Kirim request ke OpenRouter AI untuk memproses webhook.
    AI akan menentukan aksi yang tepat berdasarkan payload.
    """
    config = _get_openrouter_config()

    if not config['api_key']:
        return {'success': False, 'error': 'OpenRouter API key not configured'}

    system_prompt = (
        "Anda adalah webhook processor untuk toko online. "
        "Analisis payload webhook dan tentukan aksi yang perlu dilakukan. "
        "Respons dalam format JSON dengan field: action, message, priority. "
        "Aksi yang tersedia: notify_admin, sync_data, update_status, log_only."
    )

    try:
        response = requests.post(
            f"{config['base_url']}/chat/completions",
            headers={
                'Authorization': f'Bearer {config["api_key"]}',
                'Content-Type': 'application/json',
                'HTTP-Referer': config['site_url'],
                'X-Title': config['app_name']
            },
            json={
                'model': config['model'],
                'messages': [
                    {'role': 'system', 'content': system_prompt},
                    *messages
                ],
                'stream': False,
                'temperature': temperature,
                'max_tokens': max_tokens
            },
            timeout=30
        )

        if response.status_code == 200:
            result = response.json()
            choices = result.get('choices', [])
            if choices:
                reply = choices[0].get('message', {}).get('content', '')
                return {'success': True, 'reply': reply, 'model': result.get('model', config['model'])}
            return {'success': False, 'error': 'Empty AI response'}
        elif response.status_code == 401:
            return {'success': False, 'error': 'Invalid API key'}
        elif response.status_code == 429:
            return {'success': False, 'error': 'Rate limit exceeded'}
        else:
            return {'success': False, 'error': f'HTTP {response.status_code}: {response.text[:200]}'}

    except requests.exceptions.ConnectionError:
        return {'success': False, 'error': 'Cannot connect to OpenRouter'}
    except requests.exceptions.Timeout:
        return {'success': False, 'error': 'Request timeout'}
    except Exception as e:
        logger.error(f"OpenRouter error: {e}")
        return {'success': False, 'error': str(e)}

# Gunakan modul skip_webhook_secret jika tersedia, jika tidak fallback ke fungsi lokal
try:
    from api.skip_webhook_secret import verify_webhook_signature
except ImportError:
    def verify_webhook_signature(payload: bytes, signature_header: str, secret: str) -> bool:
        if not secret:
            return True
        if not signature_header:
            return False
        expected = hmac.new(secret.encode(), payload, hashlib.sha256).hexdigest()
        return hmac.compare_digest(expected, signature_header)

def process_webhook(request):
    """
    Proses webhook dengan dukungan OpenRouter AI.
    Webhook payload dianalisis AI untuk menentukan aksi terbaik.
    """
    # FIXED (2026 Pro): key sebelumnya 'openclaw_enabled' tidak pernah diisi
    # di mana pun (admin panel & modul lain semua pakai 'openrouter_enabled')
    # -> integrasi webhook selalu dianggap nonaktif walau admin sudah
    # mengaktifkannya lewat /admin/settings.
    if _get_setting('openrouter_enabled', 'false') != 'true':
        return jsonify({'error': 'Integration disabled'}), 403

    # FIXED (2026 Pro): key sebelumnya 'openclaw_webhook_secret' juga tidak
    # pernah diisi (admin panel pakai 'openrouter_webhook_secret') -> secret
    # selalu kosong -> verifikasi signature selalu ter-bypass (verify_webhook_
    # signature return True kalau secret kosong). Ini bug keamanan.
    secret = _get_setting('openrouter_webhook_secret', '')
    signature = request.headers.get('X-OpenRouter-Signature') or request.headers.get('X-OpenClaw-Signature')
    raw_data = request.get_data()

    if not verify_webhook_signature(raw_data, signature, secret):
        return jsonify({'error': 'Invalid signature'}), 401

    data = request.get_json(force=True)
    if not data:
        return jsonify({'error': 'Invalid JSON'}), 400

    event_type = data.get('event')
    logger.info(f"OpenRouter webhook received: {event_type}")

    # Kirim ke AI untuk analisis
    ai_result = _call_openrouter_ai([
        {'role': 'user', 'content': f'Process this webhook event:\n\nEvent: {event_type}\nPayload: {json.dumps(data, indent=2)}'}
    ])

    if not ai_result['success']:
        logger.warning(f"AI processing failed: {ai_result['error']}")
        # Fallback ke processing manual
        return _process_manual(event_type, data)

    # Parse AI response
    try:
        ai_decision = json.loads(ai_result['reply'])
        action = ai_decision.get('action', 'log_only')
        message = ai_decision.get('message', 'Webhook processed')
        priority = ai_decision.get('priority', 'normal')

        # Execute action
        if action == 'notify_admin':
            _send_telegram_notification(message, priority)
        elif action == 'sync_data':
            logger.info(f"Sync triggered by webhook: {event_type}")
        elif action == 'update_status':
            logger.info(f"Status update triggered: {event_type}")

        return jsonify({
            'status': 'processed',
            'action': action,
            'message': message,
            'priority': priority,
            'ai_model': ai_result.get('model')
        }), 200

    except json.JSONDecodeError:
        # AI returned plain text, use as message
        return jsonify({
            'status': 'processed',
            'action': 'log_only',
            'message': ai_result['reply'],
            'ai_model': ai_result.get('model')
        }), 200

def _process_manual(event_type: str, data: dict):
    """Fallback processing tanpa AI."""
    if event_type == 'ping':
        return jsonify({'status': 'pong'})
    elif event_type == 'command':
        command = data.get('command')
        if command == 'sync_products':
            return jsonify({'status': 'ok', 'message': 'Sync initiated'})
        elif command == 'health_check':
            return jsonify({'status': 'ok'})
    elif event_type == 'new_order':
        order_num = data.get('data', {}).get('order_number', 'N/A')
        _send_telegram_notification(f"🛒 Pesanan baru: {order_num}")
        return jsonify({'status': 'notified'})

    return jsonify({'status': 'received'}), 200

def _send_telegram_notification(message: str, priority: str = 'normal'):
    """Kirim notifikasi ke Telegram jika dikonfigurasi."""
    try:
        from api.telegram_gateway import TelegramGateway
        token = _get_setting('telegram_bot_token')
        chat_id = _get_setting('telegram_chat_id')
        if token and chat_id:
            priority_emoji = {'high': '🔴', 'normal': '🔵', 'low': '⚪'}.get(priority, '🔵')
            TelegramGateway.send_message(chat_id, f"{priority_emoji} <b>Webhook Notification</b>\n\n{message}", token)
    except Exception as e:
        logger.error(f"Failed to send Telegram notification: {e}")

def create_webhook_route(app):
    @app.route('/openrouter/webhook', methods=['POST'])
    @app.route('/openclaw/webhook', methods=['POST'])  # Backward compatibility
    def openrouter_webhook():
        return process_webhook(request)
    logger.info("Registered /openrouter/webhook and /openclaw/webhook routes")