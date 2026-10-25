"""
api/api.py - API Blueprint & OpenRouter 2026 Pro Service
"""
import json
import logging
import time
import os
import sys
from functools import wraps
from datetime import datetime

from flask import Blueprint, request, jsonify, current_app
import requests

bp = Blueprint('api', __name__)
logger = logging.getLogger(__name__)

# =============================================================================
# RETRY DECORATOR (FIXED SYNTAX)
# =============================================================================
def with_retry(max_attempts=3, delay=1.0, backoff=2.0, exceptions=(Exception,)):
    """
    Retry decorator dengan exponential backoff.
    FIXED: Syntax error *attempt=attempt yang invalid di Python.
    """
    def decorator(func):
        @wraps(func)
        def wrapper(*args, **kwargs):
            last_exception = None
            current_delay = delay
            for attempt in range(max_attempts):
                try:
                    # FIXED: Hapus *attempt=attempt yang invalid.
                    # Pass args dan kwargs secara normal.
                    return func(*args, **kwargs)
                except exceptions as e:
                    last_exception = e
                    logger.warning(f"{func.__name__} attempt {attempt} failed: {e}")
                    if attempt < max_attempts - 1:
                        time.sleep(current_delay)
                        current_delay *= backoff
            logger.error(f"{func.__name__} failed after {max_attempts} attempts")
            raise last_exception
        return wrapper
    return decorator


# =============================================================================
# OPENROUTER SERVICE
# =============================================================================
class OpenRouterService:
    """
    2026 Pro Edition OpenRouter Service dengan fallback chains,
    circuit breaker integration, dan cost tracking.
    """

    # FIXED: Model IDs yang benar untuk OpenRouter API.
    # Jangan tambahkan prefix 'openrouter/' untuk model provider.
    DEFAULT_MODELS = [
        "anthropic/claude-3.5-sonnet",
        "openai/gpt-4o-mini", 
        "google/gemini-2.0-flash-exp:free"
    ]

    def __init__(self, api_key=None, base_url=None, site_url=None, app_name=None):
        # FIXED (2026 Pro): jangan freeze api_key/base_url dari env var di sini.
        # Kalau override eksplisit diberikan simpan sebagai override; kalau tidak,
        # _api_key()/_base_url() dicek ulang tiap request (DB Setting dulu, baru
        # fallback env var) — sama seperti pola di ai_service.py — supaya
        # perubahan lewat panel /admin/settings langsung berlaku tanpa restart.
        self._api_key_override = api_key
        self._base_url_override = base_url
        self.site_url = site_url or os.getenv('OPENROUTER_SITE_URL', 'https://pusatgadaiidn.pythonanywhere.com')
        self.app_name = app_name or os.getenv('OPENROUTER_APP_NAME', 'Pusat Gadai Indonesia')
        self.session = requests.Session()
        self.tools = {}
        logger.info(f"OpenRouterService 2026 Pro Edition initialised ({len(self.DEFAULT_MODELS)} models)")

    def register_tool(self, name, func):
        self.tools[name] = func
        logger.info(f"Registered tool: {name}")

    def _api_key(self):
        """Baca API key dari tabel Setting (diisi admin lewat /admin/settings)
        dulu, fallback ke env var OPENROUTER_API_KEY.
        FIXED: sebelumnya api_key dibaca sekali dari env var saat singleton
        dibuat di get_ai_service(), jadi key yang diatur admin lewat panel
        tidak pernah dipakai endpoint /api/ai/chat -> selalu 401/"All models
        failed" walau chatbot storefront (ai_service.py) jalan normal.
        """
        if self._api_key_override:
            return self._api_key_override
        try:
            from models import Setting
            s = Setting.query.filter_by(key='openrouter_api_key').first()
            if s and s.value:
                return s.value.strip()
        except Exception:
            pass
        return os.getenv('OPENROUTER_API_KEY', '').strip()

    def _base_url(self):
        """Baca base URL dari tabel Setting dulu, fallback ke env var."""
        if self._base_url_override:
            return self._base_url_override.rstrip('/')
        try:
            from models import Setting
            s = Setting.query.filter_by(key='openrouter_base_url').first()
            if s and s.value:
                return s.value.rstrip('/')
        except Exception:
            pass
        return os.getenv('OPENROUTER_BASE_URL', 'https://openrouter.ai/api/v1').rstrip('/')

    # Backward-compat: kode lain mungkin masih akses .api_key / .base_url
    # sebagai atribut langsung -> sediakan sebagai property supaya tetap
    # ambil nilai terbaru dari DB/env, bukan nilai beku saat __init__.
    @property
    def api_key(self):
        return self._api_key()

    @property
    def base_url(self):
        return self._base_url()

    def _get_headers(self):
        return {
            "Authorization": f"Bearer {self._api_key()}",
            "Content-Type": "application/json",
            "HTTP-Referer": self.site_url,
            "X-Title": self.app_name,
        }

    def _clean_model_id(self, model):
        """Bersihkan model ID dari prefix ganda."""
        if not model:
            return self.DEFAULT_MODELS[0]
        # Hapus prefix ganda
        if model.startswith('openrouter/openrouter/'):
            return model.replace('openrouter/openrouter/', 'openrouter/', 1)
        if model.startswith('openrouter/anthropic/'):
            return model.replace('openrouter/anthropic/', 'anthropic/', 1)
        if model.startswith('openrouter/openai/'):
            return model.replace('openrouter/openai/', 'openai/', 1)
        if model.startswith('openrouter/google/'):
            return model.replace('openrouter/google/', 'google/', 1)
        return model

    @with_retry(max_attempts=2, delay=0.5)
    def chat_completion(self, messages, model=None, temperature=0.7, max_tokens=1000, 
                       tools=None, stream=False, **kwargs):
        """
        Call OpenRouter chat completions dengan fallback ke multiple models.
        FIXED: Payload format dan model IDs yang valid.
        """
        models_to_try = []
        if model:
            models_to_try.append(self._clean_model_id(model))
        for m in self.DEFAULT_MODELS:
            if m not in models_to_try:
                models_to_try.append(m)

        last_error = None
        for m in models_to_try:
            try:
                payload = {
                    "model": m,
                    "messages": messages,
                    "temperature": temperature,
                    "max_tokens": max_tokens,
                }
                if tools:
                    payload["tools"] = tools
                    payload["tool_choice"] = "auto"
                if stream:
                    payload["stream"] = True
                # Hapus key yang None
                payload.update({k: v for k, v in kwargs.items() if v is not None})

                url = f"{self.base_url}/chat/completions"
                response = self.session.post(url, headers=self._get_headers(), json=payload, timeout=30)

                if response.status_code == 200:
                    data = response.json()
                    # Extract cost info if available
                    usage = data.get('usage', {})
                    cost = self._estimate_cost(m, usage)
                    msg = data['choices'][0].get('message', {})
                    return {
                        'success': True,
                        'model': m,
                        'reply': msg.get('content', ''),
                        'tool_calls': msg.get('tool_calls'),
                        'raw': data,
                        'cost': cost,
                        'total_tokens': usage.get('total_tokens', 0)
                    }
                else:
                    err_text = response.text[:300]
                    logger.warning(f"HTTP {response.status_code} from model {m}: {err_text}")
                    last_error = f"HTTP {response.status_code}: {err_text}"

            except Exception as e:
                logger.warning(f"Error with model {m}: {e}")
                last_error = str(e)
                continue

        return {
            'success': False,
            'error': f"All models failed. Last error: {last_error}"
        }

    def _estimate_cost(self, model, usage):
        """Rough cost estimation for tracking."""
        prompt_tokens = usage.get('prompt_tokens', 0)
        completion_tokens = usage.get('completion_tokens', 0)
        # Very rough estimates per 1K tokens (USD)
        rates = {
            'claude-3.5-sonnet': 0.003,
            'gpt-4o-mini': 0.00015,
            'gemini-2.0-flash': 0.0001
        }
        rate = 0.001
        for k, v in rates.items():
            if k in model:
                rate = v
                break
        return round((prompt_tokens + completion_tokens) * rate / 1000, 6)

    def chat_stream(self, messages, model=None, **kwargs):
        """
        Streaming chat completion generator.
        FIXED: Proper SSE parsing dan model IDs.
        """
        models_to_try = []
        if model:
            models_to_try.append(self._clean_model_id(model))
        for m in self.DEFAULT_MODELS:
            if m not in models_to_try:
                models_to_try.append(m)

        for m in models_to_try:
            try:
                payload = {
                    "model": m,
                    "messages": messages,
                    "stream": True,
                }
                payload.update({k: v for k, v in kwargs.items() if v is not None})

                url = f"{self.base_url}/chat/completions"
                response = self.session.post(url, headers=self._get_headers(), json=payload, 
                                            timeout=30, stream=True)

                if response.status_code == 200:
                    for line in response.iter_lines():
                        if line:
                            decoded = line.decode('utf-8')
                            if decoded.startswith('data: '):
                                data_str = decoded[6:]
                                if data_str == '[DONE]':
                                    yield {'done': True}
                                    return
                                try:
                                    chunk = json.loads(data_str)
                                    delta = chunk.get('choices', [{}])[0].get('delta', {})
                                    content = delta.get('content', '')
                                    if content:
                                        yield {'content': content, 'model': m}
                                except json.JSONDecodeError:
                                    continue
                    return
                else:
                    logger.warning(f"Stream HTTP {response.status_code} from model {m}: {response.text[:200]}")
            except Exception as e:
                logger.warning(f"Stream error with model {m}: {e}")
                continue

        yield {'error': 'All models failed for streaming'}


# Global service instance (lazy init)
_ai_service = None

def get_ai_service():
    global _ai_service
    if _ai_service is None:
        _ai_service = OpenRouterService()
    return _ai_service


# =============================================================================
# TOOL DEFINITIONS
# =============================================================================
def tool_cek_resi(tracking_number=None, phone=None):
    """Cek status resi pengiriman."""
    try:
        from models import ShippingTracking
        from sqlalchemy import or_

        if not tracking_number and not phone:
            return {"found": False, "error": "Nomor resi atau HP diperlukan"}

        query = ShippingTracking.query
        if tracking_number:
            query = query.filter(ShippingTracking.tracking_number.ilike(f'%{tracking_number}%'))
        if phone:
            query = query.filter(ShippingTracking.recipient_phone.ilike(f'%{phone}%'))

        shipment = query.first()
        if not shipment:
            return {"found": False}

        return {
            "found": True,
            "tracking_number": shipment.tracking_number,
            "carrier": shipment.carrier,
            "status": shipment.status,
            "current_location": shipment.current_location
        }
    except Exception as e:
        logger.error(f"tool_cek_resi error: {e}")
        return {"found": False, "error": str(e)}

def tool_order_status(order_number=None, phone=None):
    """Cek status order."""
    try:
        from models import Order
        from sqlalchemy import or_

        query = Order.query
        if order_number:
            query = query.filter(Order.order_number.ilike(f'%{order_number}%'))
        if phone:
            query = query.filter(Order.customer_phone.ilike(f'%{phone}%'))

        order = query.first()
        if not order:
            return {"found": False}

        return {
            "found": True,
            "order_number": order.order_number,
            "status": order.status,
            "customer": order.customer_name,
            "total": order.total
        }
    except Exception as e:
        logger.error(f"tool_order_status error: {e}")
        return {"found": False, "error": str(e)}

def tool_cek_order_pelanggan(phone=None):
    """Cek order berdasarkan nomor HP pelanggan."""
    return tool_order_status(phone=phone)

def tool_generate_invoice(order_id=None):
    """Generate invoice untuk order."""
    try:
        from models import Order
        order = Order.query.get(int(order_id)) if order_id else None
        if not order:
            return {"found": False, "error": "Order tidak ditemukan"}
        return {
            "found": True,
            "invoice_url": f"/invoice/{order.id}",
            "order_number": order.order_number,
            "total": order.total
        }
    except Exception as e:
        logger.error(f"tool_generate_invoice error: {e}")
        return {"found": False, "error": str(e)}

# Register tools on module load
try:
    svc = get_ai_service()
    svc.register_tool('cek_resi', tool_cek_resi)
    svc.register_tool('order_status', tool_order_status)
    svc.register_tool('cek_order_pelanggan', tool_cek_order_pelanggan)
    svc.register_tool('generate_invoice', tool_generate_invoice)
    logger.info("Customer chat tools registered with ai_service")
except Exception as e:
    logger.warning(f"Could not register tools: {e}")


# =============================================================================
# API ROUTES
# =============================================================================
@bp.route('/health', methods=['GET'])
def health_check():
    return jsonify({'status': 'ok', 'service': 'api', 'version': '2026-pro-fixed'})

@bp.route('/products', methods=['GET'])
def api_products():
    try:
        from models import Product
        page = request.args.get('page', 1, type=int)
        per_page = min(request.args.get('per_page', 20, type=int), 100)
        q = request.args.get('q', '').strip()

        query = Product.query
        if q:
            query = query.filter(Product.name.ilike(f'%{q}%'))

        paginated = query.order_by(Product.created_at.desc()).paginate(
            page=page, per_page=per_page, error_out=False
        )
        return jsonify({
            'success': True,
            'products': [{
                'id': p.id, 'name': p.name, 'brand': p.brand,
                'price': p.price, 'stock': p.stock, 'condition': p.condition
            } for p in paginated.items],
            'total': paginated.total,
            'pages': paginated.pages
        })
    except Exception as e:
        logger.error(f"api_products error: {e}")
        return jsonify({'success': False, 'error': str(e)}), 500

@bp.route('/orders', methods=['GET'])
def api_orders():
    try:
        from models import Order
        status = request.args.get('status', '').strip()
        query = Order.query
        if status:
            query = query.filter_by(status=status)
        orders = query.order_by(Order.created_at.desc()).limit(50).all()
        return jsonify({
            'success': True,
            'orders': [{
                'id': o.id, 'order_number': o.order_number,
                'customer_name': o.customer_name, 'status': o.status,
                'total': o.total
            } for o in orders]
        })
    except Exception as e:
        logger.error(f"api_orders error: {e}")
        return jsonify({'success': False, 'error': str(e)}), 500

@bp.route('/ai/chat', methods=['POST'])
def api_ai_chat():
    """Direct AI chat endpoint via API blueprint."""
    try:
        data = request.get_json(silent=True) or {}
        message = str(data.get('message', '')).strip()
        model = data.get('model')

        if not message:
            return jsonify({'success': False, 'error': 'Pesan kosong'}), 400

        svc = get_ai_service()
        messages = [
            {"role": "system", "content": "Anda adalah asisten AI toko online."},
            {"role": "user", "content": message}
        ]
        result = svc.chat_completion(messages, model=model)
        return jsonify(result)
    except Exception as e:
        logger.exception("api_ai_chat error")
        return jsonify({'success': False, 'error': str(e)}), 500