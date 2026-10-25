"""
Health Check Module - OpenRouter Edition

Menyediakan fungsi-fungsi untuk mengecek kesehatan layanan:
- Database
- OpenRouter AI API
- Telegram Bot

Digunakan oleh endpoint /api/health.
"""

import os
import logging
from datetime import datetime
import requests

logger = logging.getLogger(__name__)

# Waktu mulai aplikasi (di-set dari app.py)
APP_START_TIME = None


def init_app(start_time: datetime = None):
    """Inisialisasi waktu mulai aplikasi untuk perhitungan uptime."""
    global APP_START_TIME
    if start_time:
        APP_START_TIME = start_time
    else:
        APP_START_TIME = datetime.utcnow()


def get_uptime() -> str:
    """Hitung uptime aplikasi dalam format yang mudah dibaca."""
    if APP_START_TIME is None:
        return "unknown"
    delta = datetime.utcnow() - APP_START_TIME
    days = delta.days
    hours, remainder = divmod(delta.seconds, 3600)
    minutes, seconds = divmod(remainder, 60)

    parts = []
    if days > 0:
        parts.append(f"{days}d")
    if hours > 0:
        parts.append(f"{hours}h")
    if minutes > 0:
        parts.append(f"{minutes}m")
    parts.append(f"{seconds}s")
    return ' '.join(parts)


def _get_setting(key: str, default: str = '') -> str:
    """Ambil nilai setting dari database."""
    try:
        from models import db, Setting
        result = db.session.execute(
            db.select(Setting).filter_by(key=key)
        ).scalar_one_or_none()
        return result.value if result and result.value else default
    except Exception as e:
        logger.warning(f"Gagal mengambil setting '{key}': {e}")
        return default


def check_database() -> dict:
    """Periksa koneksi database."""
    try:
        from models import db
        db.session.execute(db.text('SELECT 1'))
        return {'status': 'ok', 'message': 'Database connected'}
    except Exception as e:
        logger.error(f"Database health check failed: {e}")
        return {'status': 'error', 'message': str(e)}


def check_openrouter_api() -> dict:
    """Periksa koneksi ke OpenRouter AI API."""
    base_url = _get_setting('openrouter_base_url', 'https://openrouter.ai/api/v1').rstrip('/')
    api_key = _get_setting('openrouter_api_key', '')
    model = _get_setting('openrouter_model', 'openrouter/free')

    if not api_key:
        return {
            'status': 'disabled',
            'message': 'API Key OpenRouter belum dikonfigurasi',
            'gateway_url': base_url
        }

    try:
        # Test dengan simple ping ke models endpoint atau chat completion
        response = requests.get(
            f"{base_url}/models",
            headers={
                'Authorization': f'Bearer {api_key}',
                'HTTP-Referer': _get_setting('openrouter_site_url', 'https://pusatgadaiidn.pythonanywhere.com'),
                'X-Title': _get_setting('openrouter_app_name', 'pusat gadai')
            },
            timeout=10
        )

        if response.status_code == 200:
            models_data = response.json()
            available_models = len(models_data.get('data', []))
            return {
                'status': 'ok',
                'message': f'OpenRouter connected ({available_models} models available)',
                'gateway_url': base_url,
                'configured_model': model,
                'models_available': available_models
            }
        elif response.status_code == 401:
            return {
                'status': 'error',
                'message': 'API Key tidak valid',
                'gateway_url': base_url
            }
        elif response.status_code == 429:
            return {
                'status': 'warning',
                'message': 'Rate limit tercapai',
                'gateway_url': base_url
            }
        else:
            return {
                'status': 'error',
                'message': f'HTTP {response.status_code}',
                'gateway_url': base_url
            }

    except requests.exceptions.ConnectionError:
        return {
            'status': 'error',
            'message': 'Tidak dapat terhubung ke OpenRouter. Periksa koneksi internet.',
            'gateway_url': base_url
        }
    except requests.exceptions.Timeout:
        return {
            'status': 'error',
            'message': 'Connection timeout',
            'gateway_url': base_url
        }
    except Exception as e:
        logger.error(f"OpenRouter health check error: {e}")
        return {'status': 'error', 'message': str(e), 'gateway_url': base_url}


def check_telegram_bot() -> dict:
    """Periksa validitas token bot Telegram."""
    token = _get_setting('telegram_bot_token', '')
    if not token:
        return {'status': 'disabled', 'message': 'Bot token tidak dikonfigurasi'}

    try:
        r = requests.get(
            f"https://api.telegram.org/bot{token}/getMe",
            timeout=5
        )
        if r.status_code == 200:
            data = r.json()
            if data.get('ok'):
                bot_info = data.get('result', {})
                return {
                    'status': 'ok',
                    'message': 'Telegram bot connected',
                    'bot_username': bot_info.get('username', 'unknown'),
                    'bot_name': bot_info.get('first_name', 'unknown')
                }
            else:
                return {'status': 'error', 'message': data.get('description', 'Invalid token')}
        else:
            return {'status': 'error', 'message': f'HTTP {r.status_code}'}
    except requests.exceptions.Timeout:
        return {'status': 'error', 'message': 'Connection timeout'}
    except Exception as e:
        logger.error(f"Telegram bot health check error: {e}")
        return {'status': 'error', 'message': str(e)}


def full_health_check() -> dict:
    """Jalankan semua pemeriksaan dan kembalikan laporan lengkap."""
    checks = {
        'database': check_database(),
        'openrouter_api': check_openrouter_api(),
        'telegram_bot': check_telegram_bot()
    }

    all_ok = all(
        v['status'] == 'ok'
        for v in checks.values()
        if v is not None and v['status'] != 'disabled'
    )

    return {
        'status': 'ok' if all_ok else 'degraded',
        'service': 'Toko HP Second',
        'version': '2.0',
        'environment': os.getenv('FLASK_ENV', 'production'),
        'timestamp': datetime.utcnow().isoformat() + 'Z',
        'uptime': get_uptime(),
        'checks': checks
    }


def register_health_route(app):
    """Daftarkan endpoint /api/health ke aplikasi Flask."""
    from flask import jsonify

    # Inisialisasi waktu mulai aplikasi
    init_app(datetime.utcnow())

    @app.route('/api/health')
    def health_check():
        data = full_health_check()
        status_code = 200 if data['status'] == 'ok' else 503
        return jsonify(data), status_code

    app.logger.info("Health check route registered at /api/health")