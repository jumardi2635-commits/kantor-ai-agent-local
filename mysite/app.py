#!/usr/bin/env python3
"""
Marketplace HP Bekas – 2026 Professional Edition
Flask Application dengan OpenRouter Pro Integration + KYC Biometric Verification
"""

import csv
import json
import logging
import os
import re
import secrets
import shutil
import string
import time
import types
import uuid
from collections import defaultdict
from datetime import datetime, timedelta, date, timezone
from dateutil.relativedelta import relativedelta
from functools import wraps
from zoneinfo import ZoneInfo
from io import StringIO
from typing import Callable, Optional, Dict, Any
from urllib.parse import quote_plus, urlparse

from dotenv import load_dotenv
from flask import (
    Flask, render_template, redirect, url_for, request,
    flash, session, send_from_directory, jsonify, make_response,
    has_request_context, Response, stream_with_context,
)
from flask_cors import CORS
from flask_login import (
    LoginManager, login_user, logout_user, login_required, current_user,
)
from sqlalchemy import or_, desc, asc, func, text
from sqlalchemy.orm import joinedload
from werkzeug.utils import secure_filename
import requests
from requests.adapters import HTTPAdapter
from urllib3.util.retry import Retry
from sqlalchemy.exc import IntegrityError

# ---------- MODELS ----------
from models import (
    db, Admin, Product, Setting, Testimonial, HeroSlide,
    Order, OrderItem, User, Address,
    InstallmentPlan, InstallmentApplication,
    InstallmentSchedule, InstallmentPayment,
    ShippingTracking, ShippingTrackingEvent, ShippingIssue, ShippingCarrierPerformance,
    KYCVerificationSession, ApiKey,
    ChatConversation, ChatMessage, CustomerMemoryFact,
)

from visitor_stats import register_visitor_stats   # <--- TAMBAHKAN INI

# ---------- OPTIONAL API MODULES ----------
OPENROUTER_AVAILABLE = False
TELEGRAM_AVAILABLE = False
UNIFIED_AVAILABLE = False
BRIDGE_AVAILABLE = False
CUSTOMER_CHAT_AVAILABLE = False
TELEGRAM_SHOP_BOT_AVAILABLE = False

# 2026: Telegram Admin Bot (telegram_admin_bot.py) — bot wizard posting
# produk khusus admin (/postproduk, /fastpost, /addphoto, dst). Terpisah
# dari /openrouter/webhook, /telegram/webhook/<token>, dan bot shop/FAQ,
# jadi tidak saling bentrok. Import di-guard supaya deploy lama yang belum
# punya file ini tetap bisa jalan tanpa fitur ini.
TELEGRAM_ADMIN_BOT_AVAILABLE = False
try:
    from telegram_admin_bot import register_telegram_admin_bot
    TELEGRAM_ADMIN_BOT_AVAILABLE = True
except ImportError as e:
    logging.warning(f"Telegram admin bot (telegram_admin_bot.py) not available: {e}")

try:
    from api.openrouter_config import (
        is_openrouter_enabled,
        should_notify_new_order,
        should_notify_status_update,
        should_notify_product_update,
        test_openrouter_connection,
        send_notification as openrouter_send,
        OpenRouterClient,  # 2026 Pro: client dengan fallback, circuit breaker, cost tracking
    )
    from api.webhook_handler import create_webhook_route
    OPENROUTER_AVAILABLE = True
except ImportError as e:
    logging.warning(f"OpenRouter config not available: {e}")

try:
    from api.telegram_gateway import TelegramGateway
    TELEGRAM_AVAILABLE = True
except ImportError:
    pass

try:
    from api.openrouter_telegram_bridge import OpenRouterTelegramBridge
    BRIDGE_AVAILABLE = True
except ImportError:
    pass

# 2026 Pro: bot menu Telegram statis (sapaan, info produk, cara beli, status
# order, garansi, dst.) — konversi dari view.py (Django) yang sebelumnya
# tidak pernah terhubung ke aplikasi Flask ini. Endpoint terpisah dari
# /openrouter/webhook (AI-driven) dan /telegram/webhook/<token> (bridge),
# jadi tidak saling bentrok.
try:
    from api.telegram_shop_bot import register_telegram_shop_bot
    TELEGRAM_SHOP_BOT_AVAILABLE = True
except ImportError as e:
    logging.warning(f"Telegram shop bot (api/telegram_shop_bot.py) not available: {e}")

try:
    from api.customer_chat import process_customer_message, SessionMemory
    CUSTOMER_CHAT_AVAILABLE = True
except ImportError:
    pass

# 2026: FAQ Bot Telegram (bot.py) — bot terpisah dengan perintah /faq, /addfaq,
# /broadcast, /ask, dan filter anti-spam grup. Diintegrasikan sebagai modul
# `faqbot.py` supaya jalan di proses Flask yang sama dengan marketplace ini
# (lihat komentar di dalam faqbot.py untuk alasannya).
FAQBOT_AVAILABLE = False
try:
    from faqbot import init_faq_bot, FAQBOT_AVAILABLE as _faqbot_deps_ok
    FAQBOT_AVAILABLE = _faqbot_deps_ok
except ImportError as e:
    logging.warning(f"FAQ bot (faqbot.py) not available: {e}")

# 2026: NotivGo — QRIS dinamis + pencocokan pembayaran otomatis via API.
# Guard supaya deploy tanpa file notivgo.py tetap jalan.
NOTIVGO_AVAILABLE = False
NOTIVGO = None
try:
    from notivgo import register_notivgo
    NOTIVGO_AVAILABLE = True
except ImportError as e:
    logging.warning(f"NotivGo module (notivgo.py) not available: {e}")

# 2026 Pro: persistent chat history + long-term customer memory tables
# (ChatConversation/ChatMessage/CustomerMemoryFact). Sebelumnya di modul
# terpisah models_chat.py yang di-import khusus supaya kelasnya terdaftar
# ke metadata `db` sebelum db.create_all() — sekarang modelnya sudah
# digabung langsung ke models.py dan ikut ter-import lewat blok "MODELS"
# di atas, jadi selalu tersedia (tidak lagi optional/try-except).
CHAT_MEMORY_AVAILABLE = True

UNIFIED_AVAILABLE = BRIDGE_AVAILABLE

# --- Google OAuth (Login dengan Google) ---
# Authlib bersifat opsional secara import: kalau paketnya belum ter-install
# (mis. requirements.txt belum di-update di server lama), aplikasi tetap
# harus bisa jalan seperti biasa dengan login email/password saja — hanya
# tombol "Masuk dengan Google" yang otomatis disembunyikan lewat flag
# GOOGLE_OAUTH_ENABLED di bawah (lihat konfigurasi setelah `db.init_app`).
GOOGLE_OAUTH_LIB_AVAILABLE = False
try:
    from authlib.integrations.flask_client import OAuth
    GOOGLE_OAUTH_LIB_AVAILABLE = True
except ImportError as e:
    logging.warning(f"Authlib not available, Google login disabled: {e}")


# =============================================================================
# CONFIGURATION & INITIALISATION
# =============================================================================
load_dotenv()

# --- Professional Logging ---
class ColoredFormatter(logging.Formatter):
    COLORS = {
        'DEBUG': '\033[36m', 'INFO': '\033[32m', 'WARNING': '\033[33m',
        'ERROR': '\033[31m', 'CRITICAL': '\033[35m', 'RESET': '\033[0m'
    }
    def format(self, record):
        color = self.COLORS.get(record.levelname, self.COLORS['RESET'])
        record.levelname = f"{color}{record.levelname}{self.COLORS['RESET']}"
        return super().format(record)

handler = logging.StreamHandler()
handler.setFormatter(ColoredFormatter(
    '%(asctime)s %(levelname)s: [%(name)s] %(message)s',
    datefmt='%H:%M:%S'
))
logger = logging.getLogger(__name__)
logger.setLevel(logging.INFO)
logger.handlers = [handler]

class Config:
    UPLOAD_FOLDER = '/tmp/uploads' if os.getenv('VERCEL') else 'static/uploads'
    ALLOWED_EXTENSIONS = {'png', 'jpg', 'jpeg', 'gif', 'webp'}
    MAX_CONTENT_LENGTH = 16 * 1024 * 1024
    RATE_LIMIT_MESSAGES = 20
    RATE_LIMIT_WINDOW = 60
    CIRCUIT_BREAKER_THRESHOLD = 5
    CIRCUIT_BREAKER_TIMEOUT = 60
    # KYC Settings
    KYC_FACE_MATCH_THRESHOLD = 0.6  # Euclidean distance threshold (lower = stricter)
    KYC_MIN_CONFIDENCE = 70  # Minimum confidence percentage
    KYC_SESSION_EXPIRY_HOURS = 24

app = Flask(__name__)
# =============================================================================
# PATCH: cache-busting otomatis untuk file statis (fix "chat hilang saat
# refresh" yang sebenarnya adalah browser/proxy masih pakai chat_widget.js
# versi lama karena Flask default cache 12 jam + url_for('static', ...)
# tidak punya query-string versi sama sekali).
#
# Cara pakai: tempel blok ini TEPAT SETELAH baris `app = Flask(__name__)`
# di app.py Anda (sebelum app.config[...] lain juga boleh, urutannya bebas
# asal setelah `app` didefinisikan).
# =============================================================================
import hashlib

_STATIC_VERSION_CACHE = {}  # {filename: (mtime, hash8)}

def _static_file_version(filename: str) -> str:
    """Hash 8-karakter dari ISI file statis, di-cache berdasarkan mtime
    supaya tidak baca ulang file di setiap request (cuma re-hash kalau
    file benar-benar berubah sejak request terakhir)."""
    static_path = os.path.join(app.static_folder, filename)
    try:
        mtime = os.path.getmtime(static_path)
    except OSError:
        return 'na'

    cached = _STATIC_VERSION_CACHE.get(filename)
    if cached and cached[0] == mtime:
        return cached[1]

    try:
        with open(static_path, 'rb') as f:
            digest = hashlib.md5(f.read()).hexdigest()[:8]
    except OSError:
        digest = 'na'

    _STATIC_VERSION_CACHE[filename] = (mtime, digest)
    return digest


@app.template_global()
def asset_url(filename: str) -> str:
    """Pengganti url_for('static', filename=...) yang otomatis menambah
    ?v=<hash-isi-file> di belakangnya. Begitu isi file berubah (mis. deploy
    chat_widget.js baru), hash-nya ikut berubah -> URL berubah -> browser
    WAJIB fetch ulang, tidak peduli Cache-Control/max-age yang lama.

    Pakai di template:  {{ asset_url('js/chat_widget.js') }}
    Bukan:               {{ url_for('static', filename='js/chat_widget.js') }}
    """
    version = _static_file_version(filename)
    return url_for('static', filename=filename) + f'?v={version}'

app.config['SECRET_KEY'] = os.getenv('SECRET_KEY', secrets.token_hex(32))
basedir = os.path.abspath(os.path.dirname(__file__))
_database_url = os.getenv('DATABASE_URL')
IS_VERCEL = bool(os.getenv('VERCEL'))
# Vercel's deployment filesystem is read-only; only /tmp is writable (and ephemeral).
_db_dir = '/tmp/instance' if IS_VERCEL else os.path.join(basedir, 'instance')
os.makedirs(_db_dir, exist_ok=True)
_default_db_file = os.path.join(_db_dir, 'database.db').replace('\\', '/')

if not _database_url:
    _database_url = f"sqlite:///{_default_db_file}"
elif _database_url.startswith('sqlite:///'):
    raw_path = _database_url.replace('sqlite:///', '')
    if raw_path.startswith('/home/') or (os.path.dirname(raw_path) and not os.path.exists(os.path.dirname(raw_path))):
        _database_url = f"sqlite:///{_default_db_file}"
elif _database_url.startswith('postgres://'):
    _database_url = _database_url.replace('postgres://', 'postgresql://', 1)
app.config['SQLALCHEMY_DATABASE_URI'] = _database_url
app.config['SQLALCHEMY_TRACK_MODIFICATIONS'] = False
app.config['SQLALCHEMY_ENGINE_OPTIONS'] = {
    'pool_pre_ping': True,
    'pool_recycle': 300,
}
app.config['UPLOAD_FOLDER'] = Config.UPLOAD_FOLDER
app.config['MAX_CONTENT_LENGTH'] = Config.MAX_CONTENT_LENGTH
app.config['ALLOWED_EXTENSIONS'] = Config.ALLOWED_EXTENSIONS

_default_cors_origins = "http://localhost:5000,http://localhost:18789,http://host.docker.internal:18789"
_cors_origins = [o.strip() for o in os.getenv('CORS_ORIGINS', _default_cors_origins).split(',') if o.strip()]
CORS(app, origins=_cors_origins, supports_credentials=True)

# --- Production cookie hardening ---
# Diaktifkan otomatis saat FLASK_ENV=production (disetel di .env / platform
# deploy). Di local dev (tanpa HTTPS) tetap nonaktif supaya cookie tidak
# ditolak browser saat login lewat http://localhost.
if os.getenv('FLASK_ENV') == 'production':
    app.config['SESSION_COOKIE_SECURE'] = True
    app.config['SESSION_COOKIE_HTTPONLY'] = True
    app.config['SESSION_COOKIE_SAMESITE'] = 'Lax'
    app.config['PREFERRED_URL_SCHEME'] = 'https'

# --- Upload folders ---
UPLOAD_BASE = Config.UPLOAD_FOLDER
UPLOAD_PRODUCTS = os.path.join(UPLOAD_BASE, 'products')
UPLOAD_LOGO = os.path.join(UPLOAD_BASE, 'logo')
UPLOAD_HERO = os.path.join(UPLOAD_BASE, 'hero')
UPLOAD_TESTIMONIAL = os.path.join(UPLOAD_BASE, 'testimonials')
UPLOAD_AVATAR = os.path.join(UPLOAD_BASE, 'avatars')
UPLOAD_PAYMENT_PROOFS = os.path.join(UPLOAD_BASE, 'payment_proofs')
UPLOAD_AI_ATTACHMENTS = os.path.join(UPLOAD_BASE, 'ai_attachments')
UPLOAD_INSTALLMENT_DOCS = os.path.join(UPLOAD_BASE, 'installment_docs')
UPLOAD_KYC_DOCS = os.path.join(UPLOAD_BASE, 'kyc_docs')
for folder in (UPLOAD_PRODUCTS, UPLOAD_LOGO, UPLOAD_HERO, UPLOAD_TESTIMONIAL,
               UPLOAD_AVATAR, UPLOAD_PAYMENT_PROOFS, UPLOAD_AI_ATTACHMENTS,
               UPLOAD_INSTALLMENT_DOCS, UPLOAD_KYC_DOCS):
    try:
        os.makedirs(folder, exist_ok=True)
    except OSError:
        logger.warning(f"Cannot create upload folder {folder} (read-only filesystem)")

db.init_app(app)

# --- Google OAuth client ---
# Kredensial diambil dari Google Cloud Console > APIs & Services >
# Credentials > OAuth 2.0 Client ID (tipe "Web application"). Authorized
# redirect URI yang harus didaftarkan di sana persis harus sama dengan
# url_for('google_callback', _external=True), contoh:
#   https://domainanda.com/login/google/callback
GOOGLE_CLIENT_ID = os.getenv('GOOGLE_CLIENT_ID', '').strip()
GOOGLE_CLIENT_SECRET = os.getenv('GOOGLE_CLIENT_SECRET', '').strip()
GOOGLE_OAUTH_ENABLED = bool(
    GOOGLE_OAUTH_LIB_AVAILABLE and GOOGLE_CLIENT_ID and GOOGLE_CLIENT_SECRET
)

oauth = OAuth(app) if GOOGLE_OAUTH_LIB_AVAILABLE else None
if GOOGLE_OAUTH_ENABLED:
    oauth.register(
        name='google',
        client_id=GOOGLE_CLIENT_ID,
        client_secret=GOOGLE_CLIENT_SECRET,
        server_metadata_url='https://accounts.google.com/.well-known/openid-configuration',
        client_kwargs={'scope': 'openid email profile'},
    )
    logger.info("Google OAuth login enabled.")
else:
    logger.warning(
        "Google OAuth login disabled (missing Authlib package and/or "
        "GOOGLE_CLIENT_ID / GOOGLE_CLIENT_SECRET env vars)."
    )

# --- Register API Blueprint ---
try:
    from api.api import bp as api_bp
    app.register_blueprint(api_bp, url_prefix='/api')
except ImportError as e:
    logger.warning(f"Could not import API blueprint: {e}")

# --- Authentication ---
login_manager = LoginManager(app)
login_manager.login_view = 'admin_login'

@login_manager.user_loader
def load_user(user_id: str):
    if isinstance(user_id, str) and '_' in user_id:
        prefix, uid = user_id.split('_', 1)
        model = Admin if prefix == 'admin' else User
        return model.query.get(int(uid))
    try:
        uid = int(user_id)
        return Admin.query.get(uid) or User.query.get(uid)
    except (ValueError, TypeError):
        return None

@login_manager.unauthorized_handler
def unauthorized():
    if request.is_json or '/api/' in request.path:
        return jsonify({'success': False, 'message': 'Silakan login terlebih dahulu'}), 401
    flash('Silakan login untuk mengakses halaman ini.', 'warning')
    return redirect(url_for('index'))

def admin_required(func: Callable) -> Callable:
    """Decorator to ensure only Admin is logged in."""
    @wraps(func)
    def wrapper(*args, **kwargs):
        if not isinstance(current_user, Admin):
            flash('Akses ditolak.', 'error')
            return redirect(url_for('index'))
        return func(*args, **kwargs)
    return login_required(wrapper)

# =============================================================================
# PARTNER API (v1) — Integrasi eksternal via API Key
# =============================================================================
def generate_api_key() -> str:
    return "dmk_" + secrets.token_hex(24)

def require_api_key(func: Callable) -> Callable:
    """Decorator: melindungi endpoint /api/v1/* dengan API Key partner.
    Key dikirim via header 'X-API-Key' atau query param '?api_key='."""
    @wraps(func)
    def wrapper(*args, **kwargs):
        key = request.headers.get('X-API-Key') or request.args.get('api_key')
        if not key:
            return jsonify({
                'success': False,
                'error': 'API Key tidak ditemukan. Sertakan header X-API-Key.'
            }), 401
        api_key = ApiKey.query.filter_by(key=key, is_active=True).first()
        if not api_key:
            return jsonify({
                'success': False,
                'error': 'API Key tidak valid atau telah dinonaktifkan.'
            }), 401
        try:
            api_key.last_used_at = datetime.utcnow()
            db.session.commit()
        except Exception:
            db.session.rollback()
        return func(*args, **kwargs)
    return wrapper

# =============================================================================
# 2026 PRO: CIRCUIT BREAKER & RETRY UTILITIES
# =============================================================================
class CircuitBreaker:
    """Circuit breaker pattern untuk external API calls."""
    _states = {}  # key -> {state: 'closed'|'open'|'half-open', failures: int, last_failure: float}

    @classmethod
    def is_open(cls, key: str) -> bool:
        state = cls._states.get(key, {'state': 'closed'})
        if state['state'] == 'open':
            if time.time() - state.get('last_failure', 0) > Config.CIRCUIT_BREAKER_TIMEOUT:
                cls._states[key] = {'state': 'half-open', 'failures': 0}
                logger.info(f"Circuit breaker {key}: half-open")
                return False
            return True
        return False

    @classmethod
    def record_success(cls, key: str):
        cls._states[key] = {'state': 'closed', 'failures': 0}

    @classmethod
    def record_failure(cls, key: str):
        state = cls._states.get(key, {'state': 'closed', 'failures': 0})
        state['failures'] += 1
        state['last_failure'] = time.time()
        if state['failures'] >= Config.CIRCUIT_BREAKER_THRESHOLD:
            state['state'] = 'open'
            logger.warning(f"Circuit breaker {key}: OPENED after {state['failures']} failures")
        cls._states[key] = state

def create_retry_session(
    retries: int = 3,
    backoff_factor: float = 0.5,
    status_forcelist: tuple = (500, 502, 503, 504)
) -> requests.Session:
    """Create requests session dengan retry logic."""
    session = requests.Session()
    retry = Retry(
        total=retries,
        read=retries,
        connect=retries,
        backoff_factor=backoff_factor,
        status_forcelist=status_forcelist,
    )
    adapter = HTTPAdapter(max_retries=retry)
    session.mount('http://', adapter)
    session.mount('https://', adapter)
    return session

# Global retry-enabled session
retry_session = create_retry_session()

# =============================================================================
# 2026 PRO: RATE LIMITING
# =============================================================================
class RateLimiter:
    """Simple in-memory rate limiter per session."""
    _storage = defaultdict(list)  # key -> [timestamps]

    @classmethod
    def is_allowed(cls, key: str, limit: int = Config.RATE_LIMIT_MESSAGES,
                   window: int = Config.RATE_LIMIT_WINDOW) -> bool:
        now = time.time()
        # Clean old entries
        cls._storage[key] = [t for t in cls._storage[key] if now - t < window]
        if len(cls._storage[key]) >= limit:
            return False
        cls._storage[key].append(now)
        return True

    @classmethod
    def get_remaining(cls, key: str, limit: int = Config.RATE_LIMIT_MESSAGES,
                      window: int = Config.RATE_LIMIT_WINDOW) -> int:
        now = time.time()
        cls._storage[key] = [t for t in cls._storage[key] if now - t < window]
        return max(0, limit - len(cls._storage[key]))

def rate_limit(limit: int = Config.RATE_LIMIT_MESSAGES,
               window: int = Config.RATE_LIMIT_WINDOW):
    """Decorator untuk rate limit API endpoints."""
    def decorator(f):
        @wraps(f)
        def wrapped(*args, **kwargs):
            key = f"{request.remote_addr}:{session.get('_csrf_token', 'anon')}"
            if not RateLimiter.is_allowed(key, limit, window):
                return jsonify({
                    'success': False,
                    'message': 'Rate limit exceeded. Silakan tunggu sebentar.',
                    'retry_after': window
                }), 429
            return f(*args, **kwargs)
        return wrapped
    return decorator

# =============================================================================
# 2026 PRO: COST TRACKING
# =============================================================================
class CostTracker:
    """Track OpenRouter API costs per request."""
    _costs = []  # List of {timestamp, model, cost_usd, tokens}

    @classmethod
    def record(cls, model: str, cost_usd: float, tokens: int):
        cls._costs.append({
            'timestamp': datetime.utcnow().isoformat(),
            'model': model,
            'cost_usd': round(cost_usd, 6),
            'tokens': tokens
        })
        # Keep only last 1000 entries
        if len(cls._costs) > 1000:
            cls._costs = cls._costs[-1000:]

    @classmethod
    def get_stats(cls, hours: int = 24) -> Dict[str, Any]:
        cutoff = datetime.utcnow() - timedelta(hours=hours)
        recent = [c for c in cls._costs if datetime.fromisoformat(c['timestamp']) > cutoff]
        total_cost = sum(c['cost_usd'] for c in recent)
        total_tokens = sum(c['tokens'] for c in recent)
        by_model = defaultdict(lambda: {'cost': 0.0, 'tokens': 0, 'requests': 0})
        for c in recent:
            by_model[c['model']]['cost'] += c['cost_usd']
            by_model[c['model']]['tokens'] += c['tokens']
            by_model[c['model']]['requests'] += 1
        return {
            'period_hours': hours,
            'total_requests': len(recent),
            'total_cost_usd': round(total_cost, 4),
            'total_tokens': total_tokens,
            'by_model': dict(by_model)
        }

# =============================================================================
# HELPERS
# =============================================================================
def get_setting(key: str, default: Optional[str] = None) -> str:
    setting = Setting.query.filter_by(key=key).first()
    if setting and setting.value is not None:
        return setting.value
    return os.getenv(key.upper(), default or '')

def get_site_name(default: str = 'ATLANTIS CELULAR') -> str:
    """Nama website yang ditampilkan di navbar/footer/judul halaman.

    Form di /admin/settings hanya punya input 'store_name' (tidak ada input
    'site_name'), jadi nama toko yang diisi admin harus dibaca dari
    'store_name' dulu. Key 'site_name' tetap dicek sebagai fallback untuk
    kompatibilitas data lama, baru terakhir pakai default.
    """
    return get_setting('store_name') or get_setting('site_name') or default

def is_credit_available_for_current_user() -> bool:
    """Menentukan apakah UI & fungsi kredit (tombol kalkulator di halaman
    produk, panel pembayaran kredit di checkout) boleh diakses.

    Aturan: WAJIB tersedia untuk user yang login DAN sudah KYC verified,
    terlepas dari toggle admin 'credit_enabled' — user yang sudah lolos
    verifikasi identitas tidak boleh kehilangan akses kredit hanya karena
    admin mematikan promosi kredit untuk user baru/belum-KYC.

    Untuk guest & user login yang belum KYC, perilaku lama tetap dipakai:
    tergantung sepenuhnya pada setting 'credit_enabled' dari admin.
    """
    admin_credit_enabled = get_setting('credit_enabled', 'false').lower() in ('true', '1', 'yes')
    user_kyc_verified = (
        current_user.is_authenticated
        and isinstance(current_user, User)
        and current_user.kyc_verified
    )
    return admin_credit_enabled or user_kyc_verified

def format_price(price_in_cents: Optional[int], show_decimal: bool = False) -> str:
    """
    Format harga yang disimpan dalam SEN (cents) menjadi string Rupiah yang
    konsisten di seluruh aplikasi (template, notifikasi WA/Telegram, invoice,
    dan response JSON API).

    - Menerima int/float/str/None — apa pun yang bisa dikonversi ke angka.
    - Membulatkan ke sen terdekat sebelum diproses, sehingga sisa float
      seperti 149999.9999999999 tidak membuat hasil "Rp 1.499" yang salah.
    - Mendukung nilai negatif (mis. menampilkan potongan/diskon: -Rp 50.000).
    - Pemisah ribuan pakai titik, dan kalau show_decimal=True serta ada sisa
      sen, desimalnya pakai koma — sesuai konvensi Rupiah Indonesia
      (mis. Rp 1.500.000,50).
    - Tidak pernah melempar exception ke caller: input tak valid -> 'Rp 0'.
    """
    if price_in_cents is None or price_in_cents == '':
        return 'Rp 0'
    try:
        cents = int(round(float(price_in_cents)))
    except (ValueError, TypeError):
        return 'Rp 0'

    negative = cents < 0
    cents = abs(cents)
    rupiah, sisa_sen = divmod(cents, 100)

    integer_str = f"{rupiah:,}".replace(',', '.')
    if show_decimal and sisa_sen:
        formatted = f"{integer_str},{sisa_sen:02d}"
    else:
        formatted = integer_str

    return f"{'-' if negative else ''}Rp {formatted}"

def generate_csrf_token() -> str:
    if '_csrf_token' not in session:
        session['_csrf_token'] = secrets.token_hex(16)
    return session['_csrf_token']

def validate_csrf_token() -> bool:
    """Compare the token submitted by the client against the one stored in
    the session. Accepts the token from a form field, JSON body, or either
    of the two header names used across the templates in this codebase."""
    expected = session.get('_csrf_token')
    if not expected:
        return False
    submitted = request.headers.get('X-CSRFToken') or request.headers.get('X-CSRF-Token')
    if not submitted and request.is_json:
        submitted = (request.get_json(silent=True) or {}).get('csrf_token')
    if not submitted and request.form:
        submitted = request.form.get('csrf_token')
    return bool(submitted) and secrets.compare_digest(str(submitted), str(expected))

def csrf_protect_endpoint(func: Callable) -> Callable:
    """Decorator for state-changing endpoints whose templates are confirmed
    to send the CSRF token (as a header or hidden field). Do not apply this
    to endpoints whose form/JS hasn't been verified to send the token, or
    the request will be rejected with a 400."""
    @wraps(func)
    def wrapper(*args, **kwargs):
        if request.method in ('POST', 'PUT', 'PATCH', 'DELETE') and not validate_csrf_token():
            if request.is_json or request.path.startswith('/api/'):
                return jsonify({'success': False, 'message': 'Token keamanan tidak valid. Silakan muat ulang halaman.'}), 400
            flash('Token keamanan tidak valid. Silakan muat ulang halaman dan coba lagi.', 'error')
            return redirect(request.referrer or url_for('index'))
        return func(*args, **kwargs)
    return wrapper

def allowed_file(filename: str) -> bool:
    return '.' in filename and filename.rsplit('.', 1)[1].lower() in Config.ALLOWED_EXTENSIONS

def is_browser_supported_image(filename: Optional[str]) -> bool:
    if not filename or '.' not in filename:
        return False
    ext = filename.rsplit('.', 1)[1].lower()
    return ext not in {'heic', 'heif'}

def get_uploaded_image_url(
    filename: Optional[str],
    endpoint: str,
    placeholder_url: str,
) -> str:
    if filename and is_browser_supported_image(filename):
        if has_request_context():
            return url_for(endpoint, filename=filename)
        return filename
    return placeholder_url

def get_product_image_url(filename: Optional[str]) -> str:
    return get_uploaded_image_url(
        filename, 'uploaded_product_file',
        'https://placehold.co/600x600/0f172a/cbd5e1?text=No+Image',
    )

def get_hero_image_url(filename: Optional[str]) -> str:
    return get_uploaded_image_url(
        filename, 'uploaded_hero_file',
        'https://placehold.co/1920x1080/0f172a/6366f1?text=Hero+Slide',
    )

def get_testimonial_image_url(filename: Optional[str]) -> str:
    return get_uploaded_image_url(
        filename, 'uploaded_testimonial_file',
        'https://placehold.co/240x240/0f172a/64748b?text=User',
    )

def get_user_avatar_url(user) -> str:
    avatar_filename = getattr(user, 'avatar_filename', None) if user else None
    full_name = getattr(user, 'full_name', '') if user else ''
    if avatar_filename:
        return url_for('uploaded_avatar_file', filename=avatar_filename)
    return f"https://ui-avatars.com/api/?name={quote_plus(full_name or 'User')}&background=0f172a&color=6366f1&size=200&font-size=0.35&bold=true"

def save_uploaded_image(file, prefix: str = '', subfolder: str = 'products') -> Optional[str]:
    if file and file.filename and allowed_file(file.filename):
        name = secure_filename(file.filename)
        unique = str(uuid.uuid4())[:8]
        new_name = f"{unique}_{prefix}{name}"
        folder = os.path.join(Config.UPLOAD_FOLDER, subfolder)
        os.makedirs(folder, exist_ok=True)
        file.save(os.path.join(folder, new_name))
        return new_name
    return None

def _copy_kyc_doc_to_installment(kyc_filename: str, prefix: str = '') -> str:
    """Copy dokumen KYC (KTP/selfie) yang sudah terverifikasi dari
    UPLOAD_KYC_DOCS ke UPLOAD_INSTALLMENT_DOCS, supaya bisa dilampirkan ke
    pengajuan kredit tanpa meminta user yang sudah verified upload ulang
    foto yang sama. Melempar FileNotFoundError jika file sumber sudah hilang."""
    src_path = os.path.join(UPLOAD_KYC_DOCS, kyc_filename)
    if not os.path.exists(src_path):
        raise FileNotFoundError(kyc_filename)
    new_name = f"{uuid.uuid4().hex[:8]}_{prefix}{secure_filename(kyc_filename)}"
    dst_path = os.path.join(UPLOAD_INSTALLMENT_DOCS, new_name)
    shutil.copy2(src_path, dst_path)
    return new_name

def generate_order_number() -> str:
    return f"INV-{datetime.now().strftime('%Y%m%d')}-{uuid.uuid4().hex[:6].upper()}"

def sync_shipping_tracking(order):
    if not order.tracking_number:
        return
    try:
        shipment = ShippingTracking.query.filter_by(order_id=order.id).first()
        if not shipment:
            shipment = ShippingTracking(
                order_id=order.id,
                tracking_number=order.tracking_number,
                carrier=order.carrier_name or 'Kurir',
                recipient_name=order.shipping_recipient_name or order.customer_name,
                recipient_phone=order.shipping_phone or order.customer_phone,
                recipient_address=order.shipping_address or '',
                shipping_cost=order.shipping_cost or 0,
                total_cost=order.shipping_cost or 0,
                created_at=datetime.utcnow()
            )
            db.session.add(shipment)
            db.session.flush()

        status_map = {
            'Ready_to_Ship': 'Pending_Pickup',
            'Shipped': 'In_Transit',
            'Out_for_Delivery': 'Out_for_Delivery',
            'Completed': 'Delivered'
        }
        if order.status in status_map:
            new_status = status_map[order.status]
            if shipment.status != new_status:
                shipment.status = new_status
                if new_status == 'Delivered':
                    shipment.actual_delivery = datetime.utcnow()
                status_desc_map = {
                    'Pending_Pickup': 'Paket siap dikirim, menunggu pickup oleh kurir.',
                    'In_Transit': 'Paket sedang dikirim oleh kurir.',
                    'Out_for_Delivery': 'Paket sedang diantar ke alamat tujuan.',
                    'Delivered': 'Paket telah berhasil diterima.'
                }
                event = ShippingTrackingEvent(
                    shipping_tracking_id=shipment.id,
                    status=new_status,
                    status_description=status_desc_map.get(new_status, f"Status diperbarui menjadi {new_status.replace('_', ' ')}."),
                    location=shipment.current_location or 'Sistem',
                    event_type='status_update',
                    event_time=datetime.utcnow()
                )
                db.session.add(event)
    except IntegrityError:
        db.session.rollback()
        logger.error(f'IntegrityError syncing shipping tracking for order {order.id}')
    except Exception as e:
        db.session.rollback()
        logger.error(f'Error syncing shipping tracking for order {order.id}: {str(e)}')

def _build_cart_item(product: Product) -> dict:
    """Build cart item dict with price in RUPIAH (not cents) for display consistency."""
    # Convert cents to rupiah for cart session storage
    price_rupiah = int(product.price) // 100 if product.price else 0
    compare_rupiah = int(product.compare_at_price) // 100 if product.compare_at_price else None

    return {
        'product_id': product.id,
        'name': product.name,
        'brand': product.brand or '',
        'model': product.model or '',
        'price': price_rupiah,
        'compare_at_price': compare_rupiah,
        'image': product.image_filename,
        'condition': product.condition or 'Like New',
        'sku': product.sku or f"SKU-{product.id:04d}",
        'stock': product.stock if product.stock is not None else 99,
        'quantity': 1
    }

def normalize_cart_items(cart_items: list) -> list:
    normalized = []
    for item in cart_items or []:
        product_id = item.get('product_id') if isinstance(item, dict) else None
        if not product_id:
            continue
        product = Product.query.get(product_id)
        if not product:
            continue
        cart_item = _build_cart_item(product)
        cart_item['quantity'] = max(1, _safe_int(item.get('quantity', 1), 1))
        normalized.append(cart_item)
    return normalized

def _safe_int(value, default=0) -> int:
    if value is None:
        return default
    value = str(value).strip()
    if not value:
        return default
    try:
        return int(float(value))
    except (ValueError, TypeError):
        return default

def _safe_float(value) -> Optional[float]:
    if value is None:
        return None
    value = str(value).strip()
    if not value:
        return None
    try:
        return float(value)
    except (ValueError, TypeError):
        return None

def _safe_price(value) -> Optional[int]:
    """
    Konversi input harga dari form ke integer sen (cents) untuk database.

    Handle berbagai format input:
    - "1500000" → 1500000 (rupiah murni, tanpa pemisah)
    - "1.500.000" → 1500000 (titik sebagai pemisah ribuan)
    - "1,500,000" → 1500000 (koma sebagai pemisah ribuan)
    - "1500000.5" / "1500000.75" → desimal asli (1-2 digit di belakang titik), di-round
    - "" / None → None

    CATATAN PERBAIKAN (fix format price):
    Rupiah di aplikasi ini tidak pernah tampil dengan pecahan/sen di UI, jadi
    kelompok 3 digit setelah titik/koma SELALU pemisah ribuan, bukan desimal.
    Heuristik lama menebak "3 digit di belakang titik = desimal", padahal pola
    itu justru pola pemisah ribuan yang normal (mis. "150.000" atau
    "1.500.000") — akibatnya harga tersimpan 1000x lebih kecil, atau gagal
    di-parse sama sekali saat ada lebih dari satu titik. Sekarang: >1 titik/koma
    atau tepat 3 digit di belakang separator terakhir → selalu dianggap
    pemisah ribuan. Desimal asli hanya dikenali jika sisa digitnya 1–2 (mis.
    "1500000.5"), yang praktiknya tidak muncul dari form ini tapi tetap
    didukung untuk kompatibilitas API/import.
    """
    if value is None:
        return None
    value = str(value).strip()
    if not value:
        return None
    try:
        # Deteksi format: kalau ada titik dan koma, tentukan yang mana desimal
        # Format Indonesia: 1.500.000,00 (titik ribuan, koma desimal)
        # Format English: 1,500,000.00 (koma ribuan, titik desimal)

        has_dot = '.' in value
        has_comma = ',' in value

        if has_dot and has_comma:
            # Ada keduanya — format Indonesia: titik ribuan, koma desimal
            # "1.500.000,50" → hapus titik, ganti koma jadi titik → "1500000.50"
            clean = value.replace('.', '').replace(',', '.')
        elif has_comma and not has_dot:
            # Hanya koma — bisa ribuan atau desimal
            comma_count = value.count(',')
            last_comma = value.rfind(',')
            trailing = len(value) - last_comma - 1
            if comma_count > 1 or trailing == 3:
                # Ribuan: "1,500,000" atau "150,000" → "1500000" / "150000"
                clean = value.replace(',', '')
            else:
                # Desimal asli: "1500,5" → "1500.5"
                clean = value.replace(',', '.')
        elif has_dot and not has_comma:
            # Hanya titik — sama logika
            dot_count = value.count('.')
            last_dot = value.rfind('.')
            trailing = len(value) - last_dot - 1
            if dot_count > 1 or trailing == 3:
                # Ribuan: "1.500.000" atau "150.000" → "1500000" / "150000"
                clean = value.replace('.', '')
            else:
                # Desimal asli: "1500000.5" → sudah benar
                clean = value
        else:
            # Tanpa pemisah
            clean = value

        rupiah = float(clean)
        # Simpan dalam sen (cents) — kalau sudah dalam rupiah, tidak perlu * 100
        # Karena DB menyimpan sen, tapi form kirim rupiah
        # Jadi konversi: rupiah * 100 = sen
        return max(0, int(round(rupiah * 100)))
    except (ValueError, TypeError):
        return None

def _safe_specs(value) -> Optional[dict]:
    if value is None:
        return None
    value = str(value).strip()
    if not value:
        return None
    try:
        return json.loads(value)
    except (ValueError, json.JSONDecodeError):
        return None

# =============================================================================
# i18n (minimal)
# =============================================================================
TRANSLATIONS = {
    'id': {
        'home': 'Beranda', 'catalog': 'Katalog', 'cart': 'Keranjang',
        'checkout': 'Lanjut ke Checkout', 'empty_cart': 'Keranjang belanja Anda kosong',
    }
}
def get_text(key: str) -> str:
    return TRANSLATIONS.get(session.get('lang', 'id'), {}).get(key, key)
app.jinja_env.globals.update(_=get_text)

# =============================================================================
# CONTEXT PROCESSORS
# =============================================================================
@app.context_processor
def inject_globals():
    db_settings = {s.key: (s.value or '') for s in Setting.query.all()}

    ai_defaults = {
        'openrouter_base_url': 'https://openrouter.ai/api/v1',
        'openrouter_api_key': os.environ.get('OPENROUTER_API_KEY', ''),
        'openrouter_model': 'openrouter/free',
        'openrouter_site_url': 'https://pusathandphone.pythonanywhere.com',
        'openrouter_app_name': 'ATLANTIS CELULAR',
        'ai_system_prompt': (
    'Anda adalah "Jaga", asisten AI resmi ATLANTIS CELULAR, toko daring terpercaya.\n'
    'Anda melayani pelanggan dengan standar profesional tinggi: ramah, akurat, cepat, dan tepercaya.\n\n'

    '=== LINGKUP LAYANAN & KEMAMPUAN ===\n'
    'Anda memiliki akses ke tools internal untuk mengambil data nyata dari sistem:\n'
    '• cek_resi — Lacak status pengiriman berdasarkan nomor resi, nomor pesanan, atau nomor HP.\n'
    '• order_status — Tampilkan detail dan status terkini suatu pesanan.\n'
    '• cek_order_pelanggan — Temukan semua pesanan yang terkait dengan nomor HP pelanggan.\n'
    '• generate_invoice — Sajikan ringkasan invoice atau kwitansi pembelian.\n'
    '• cek_produk — Peroleh informasi produk, harga, dan ketersediaan stok.\n'
    '• cek_status_kyc — Periksa status verifikasi identitas (KYC) berdasarkan nomor HP.\n'
    '• cek_status_cicilan — Tampilkan status pengajuan cicilan/kredit & jadwal pembayaran.\n'
    '• simpan_preferensi_pelanggan — Catat preferensi pelanggan (nama panggilan, minat, anggaran, dll.) '
    'untuk pengalaman yang lebih personal di percakapan berikutnya.\n\n'

    '=== MEMORI PELANGGAN & PERSONALISASI ===\n'
    'Riwayat percakapan disimpan lintas sesi. Jika terdapat blok MEMORI PELANGGAN di awal percakapan, '
    'itu adalah fakta yang telah disetujui pelanggan untuk diingat. Gunakan secara alami dan relevan, '
    'misalnya menyapa dengan nama panggilan jika diketahui. Jangan pernah membacakan data mentah '
    'atau menciptakan fakta baru tanpa izin.\n'
    'Apabila pelanggan memberikan informasi penting untuk layanan mendatang, simpan melalui '
    'simpan_preferensi_pelanggan secara proaktif dengan tetap meminta konfirmasi singkat.\n\n'

    '=== PROTOKOL PENGGUNAAN TOOLS ===\n'
    '- Panggil tool HANYA jika pengguna meminta data spesifik yang tersedia pada tool tersebut.\n'
    '- Sebelum memanggil tool, pastikan semua parameter wajib terpenuhi. Jika ada yang kurang, '
    'tanyakan dengan jelas dan sopan.\n'
    '- Jika tool mengembalikan error, sampaikan kepada pengguna dengan bahasa yang mudah dimengerti '
    'tanpa menyalahkan sistem.\n'
    '- DILARANG mengarang, memperkirakan, atau memodifikasi data yang tidak ada di database.\n'
    '- Untuk data sensitif (KYC, cicilan), verifikasi identitas WAJIB melalui nomor HP atau kode pengajuan '
    'sebelum informasi ditampilkan.\n\n'

    '=== FORMAT RESPONS PROFESIONAL ===\n'
    '- Gunakan teks bersih dengan emoji sebagai penekanan visual. Tidak diizinkan HTML, tag <b>, <br>, atau markup lain.\n'
    '- Setiap respons maksimal 6 baris. Jika informasi lebih panjang, gunakan ringkasan terstruktur dengan baris baru.\n'
    '- Pisahkan poin penting dengan baris baru atau simbol, tetapi tetap dalam batas 6 baris.\n'
    '- Bahasa Indonesia baku dan profesional, hangat tanpa kehilangan ketegasan.\n'
    '- Awali dengan salam singkat jika relevan, akhiri dengan tawaran bantuan tambahan.\n\n'

    '=== STATUS PESANAN (Referensi Internal) ===\n'
    'Pending = Menunggu Pembayaran\n'
    'Paid = Lunas – Sedang Diproses\n'
    'Processing = Diproses\n'
    'Ready_to_Ship = Siap Dikirim\n'
    'Shipped = Dikirim\n'
    'Out_for_Delivery = Dalam Pengantaran\n'
    'Completed = Selesai\n'
    'Cancelled = Dibatalkan\n'
    'Returned = Dikembalikan\n\n'

    '=== PROSEDUR CICILAN / KREDIT ===\n'
    'Alur: Ajukan data → Verifikasi KYC → Review & Keputusan.\n'
    'Penebusan: Pelunasan total (pokok + bunga) sebelum tanggal jatuh tempo.\n'
    'Sampaikan informasi cicilan hanya setelah identitas terverifikasi dan gunakan data dari sistem.\n\n'

    '=== BATASAN KERAS & ETIKA LAYANAN ===\n'
    '- DILARANG KERAS melakukan tindakan administratif: mengubah resi, membatalkan pesanan, '
    'mengkonfirmasi pembayaran, atau mengubah data pelanggan.\n'
    '- JANGAN PERNAH menampilkan data pesanan milik pelanggan lain.\n'
    '- Jika permintaan di luar kapasitas Anda, arahkan dengan sopan ke WhatsApp admin resmi.\n'
    '- Anda TIDAK BOLEH mengarang, berspekulasi, atau memberikan janji yang tidak didukung data sistem.\n'
    '- Jaga kerahasiaan data pelanggan; jangan ulangi informasi pribadi yang tidak perlu.\n'
    '- Gunakan nada profesional dan hindari bahasa informal, slang, atau singkatan tidak baku.\n'
    '- Jika ragu akan kebenaran data, akui keterbatasan dan tawarkan bantuan eskalasi.\n\n'

    '=== PRINSIP PELAYANAN ===\n'
    '1. Akurasi adalah prioritas utama.\n'
    '2. Privasi pelanggan tidak bisa ditawar.\n'
    '3. Respons cepat namun tidak terburu-buru.\n'
    '4. Setiap jawaban harus bisa dipertanggungjawabkan berdasarkan data.\n'
    '5. Pelanggan selalu diperlakukan dengan hormat dan empati.'
    ),
        'openrouter_notify_new_order': 'true',
        'openrouter_notify_status_update': 'false',
        'openrouter_notify_product_update': 'false',
    }
    for k, v in ai_defaults.items():
        db_settings.setdefault(k, v)

    # Nama website mengikuti 'store_name' (field yang benar-benar ada di form
    # /admin/settings). Key 'site_name' lama dipakai sebagai fallback saja,
    # supaya perubahan "Nama Toko" di halaman admin langsung tampil di navbar/
    # footer/judul halaman tanpa perlu isi key terpisah yang tidak ada UI-nya.
    resolved_site_name = db_settings.get('store_name') or db_settings.get('site_name') or 'ATLANTIS CELULAR'

    gs = types.SimpleNamespace(
        openrouter_enabled=bool(db_settings.get('openrouter_api_key')),
        openrouter_api_key=db_settings.get('openrouter_api_key', ''),
        openrouter_base_url=db_settings.get('openrouter_base_url', 'https://openrouter.ai/api/v1'),
        openrouter_model=db_settings.get('openrouter_model', 'openrouter/free'),
        openrouter_site_url=db_settings.get('openrouter_site_url', 'https://pusathandphone.pythonanywhere.com'),
        openrouter_app_name=db_settings.get('openrouter_app_name', 'ATLANTIS CELULAR'),
        site_name=resolved_site_name,
        store_name=resolved_site_name,
        store_tagline=db_settings.get('store_tagline', ''),
        store_email=db_settings.get('store_email', ''),
        store_whatsapp=db_settings.get('store_whatsapp', ''),
        store_address=db_settings.get('store_address', ''),
        store_city=db_settings.get('store_city', ''),
        store_province=db_settings.get('store_province', ''),
        store_postal=db_settings.get('store_postal', ''),
        logo_url=db_settings.get('logo_url', ''),
    )

    payment_defaults = {
        'payment_bank_transfer_enabled': 'true',
        'payment_qris_enabled': 'false',
        'payment_cod_enabled': 'false',
        'cod_fee': '0',
        'cod_max_order': '0',
        'cod_areas': '',
        'packaging_cost': '0',
        'free_shipping_min': '0',
        'shipping_free_threshold': '0',
        'shipping_couriers': '',
    }
    for k, v in payment_defaults.items():
        db_settings.setdefault(k, v)

    return {
        'global_settings': gs,
        'settings': db_settings,
        'site_name': gs.site_name,
        'logo_url': gs.logo_url,
        'now': datetime.now(ZoneInfo(get_app_timezone_name())),
        'format_price': format_price,
        'csrf_token': generate_csrf_token,
    }

@app.context_processor
def inject_testimonials():
    return {
        'testimonials': Testimonial.query.filter_by(is_active=True).order_by(Testimonial.sort_order).all(),
        'hero_slides': HeroSlide.query.filter_by(is_active=True).order_by(HeroSlide.sort_order).all(),
    }

@app.context_processor
def inject_user():
    return {
        'user_logged_in': current_user.is_authenticated and isinstance(current_user, User),
        'user_name': current_user.full_name if (current_user.is_authenticated and isinstance(current_user, User)) else '',
        'google_oauth_enabled': GOOGLE_OAUTH_ENABLED,
    }

@app.context_processor
def inject_admin_stats():
    if current_user.is_authenticated and isinstance(current_user, Admin):
        return {
            'pending_orders_count': Order.query.filter_by(status='Pending').count(),
            'processing_orders_count': Order.query.filter_by(status='Processing').count(),
            'ready_to_ship_count': Order.query.filter_by(status='Ready_to_Ship').count(),
            'shipped_count': Order.query.filter_by(status='Shipped').count(),
            'completed_count': Order.query.filter_by(status='Completed').count(),
            'today_revenue': db.session.query(func.coalesce(func.sum(Order.total), 0))
                                 .filter(Order.status.in_(['Paid','Completed']), func.date(Order.paid_at) == date.today()).scalar(),
            'kyc_pending_count': KYCVerificationSession.query.filter_by(status='pending_review').count(),
        }
    return {}

def get_app_timezone_name() -> str:
    timezone_name = (get_setting('timezone', 'Asia/Jakarta') or 'Asia/Jakarta').strip()
    if not timezone_name:
        return 'Asia/Jakarta'
    try:
        ZoneInfo(timezone_name)
        return timezone_name
    except Exception:
        return 'Asia/Jakarta'


def convert_datetime_to_app_timezone(dt):
    if not dt:
        return None
    if isinstance(dt, str):
        try:
            dt = datetime.fromisoformat(dt.replace('Z', '+00:00'))
        except Exception:
            return dt
    if dt.tzinfo is None:
        dt = dt.replace(tzinfo=timezone.utc)
    return dt.astimezone(ZoneInfo(get_app_timezone_name()))


def format_date_filter(dt, style=None):
    if not dt:
        return ''

    dt = convert_datetime_to_app_timezone(dt)
    if not isinstance(dt, datetime):
        return str(dt)

    date_format = get_setting('date_format', 'dd/MM/yyyy')
    d = str(dt.day).zfill(2)
    m = str(dt.month).zfill(2)
    y = str(dt.year)
    if date_format == 'dd/MM/yyyy': return f'{d}/{m}/{y}'
    if date_format == 'dd-MM-yyyy': return f'{d}-{m}-{y}'
    if date_format == 'dd.MM.yyyy': return f'{d}.{m}.{y}'
    if date_format == 'yyyy/MM/dd': return f'{y}/{m}/{d}'
    if date_format == 'MM/dd/yyyy': return f'{m}/{d}/{y}'
    return f'{d}/{m}/{y}'


def format_datetime_filter(dt, include_time=True):
    if not dt:
        return ''

    dt = convert_datetime_to_app_timezone(dt)
    if not isinstance(dt, datetime):
        return str(dt)

    formatted = format_date_filter(dt)
    if include_time:
        time_format = get_setting('time_format', 'HH:mm')
        h = str(dt.hour).zfill(2)
        m = str(dt.minute).zfill(2)
        s = str(dt.second).zfill(2)
        if time_format == 'HH:mm:ss':
            formatted += f' {h}:{m}:{s}'
        else:
            formatted += f' {h}:{m}'
    return formatted

app.jinja_env.globals['get_setting'] = get_setting
app.jinja_env.globals['get_product_image_url'] = get_product_image_url
app.jinja_env.globals['get_hero_image_url'] = get_hero_image_url
app.jinja_env.globals['get_testimonial_image_url'] = get_testimonial_image_url
app.jinja_env.globals['get_user_avatar_url'] = get_user_avatar_url
# utcnow() → callable Jinja2 global untuk perbandingan tanggal jatuh tempo
# (context_processor 'now' adalah timezone-aware; sched.due_date adalah naive UTC)
app.jinja_env.globals['utcnow'] = datetime.utcnow
app.jinja_env.filters['format_date'] = format_date_filter
app.jinja_env.filters['format_datetime'] = format_datetime_filter

@app.route('/lang/<lang>')
def set_language(lang):
    if lang in ('en','id'):
        session['lang'] = lang
    return redirect(request.referrer or url_for('index'))

# =============================================================================
# 2026 PRO: NOTIFICATION SERVICE dengan FALLBACK CHAINS
# =============================================================================
TELEGRAM_OUTBOUND_BLOCKED = False

class NotificationService:
    """Notification service dengan fallback chains dan circuit breaker."""

    @staticmethod
    def _call_with_circuit_breaker(key: str, func, *args, **kwargs):
        """Execute function dengan circuit breaker protection."""
        if CircuitBreaker.is_open(key):
            return False, f"Circuit breaker open for {key}"
        try:
            result = func(*args, **kwargs)
            CircuitBreaker.record_success(key)
            return True, result
        except Exception as e:
            CircuitBreaker.record_failure(key)
            return False, str(e)

    @staticmethod
    def send_telegram(message: str, chat_id=None, bot_token=None):
        if TELEGRAM_OUTBOUND_BLOCKED:
            return False, "Blocked"
        token = bot_token or get_setting('telegram_bot_token')
        target = chat_id or get_setting('telegram_chat_id')
        if not token or not target:
            return False, "Missing credentials"

        def _send():
            global TELEGRAM_OUTBOUND_BLOCKED
            if TELEGRAM_AVAILABLE:
                return TelegramGateway.send_message(target, message, token)
            url = f"https://api.telegram.org/bot{token}/sendMessage"
            payload = {
                'chat_id': target, 'text': message,
                'parse_mode': 'HTML', 'disable_web_page_preview': True
            }
            r = retry_session.post(url, json=payload, timeout=10)
            if r.status_code == 200:
                return True, r.text
            if "Access Denied" in r.text and "PythonAnywhere" in r.text:
                TELEGRAM_OUTBOUND_BLOCKED = True
                return False, "Blocked by PythonAnywhere"
            return False, r.text

        return NotificationService._call_with_circuit_breaker('telegram', _send)

    @staticmethod
    def send_whatsapp(to_number: str, message: str):
        phone_id = get_setting('whatsapp_phone_number_id')
        token = get_setting('whatsapp_access_token')
        if not phone_id or not token:
            return False, "WA not configured"

        def _send():
            url = f"https://graph.facebook.com/v18.0/{phone_id}/messages"
            headers = {
                'Authorization': f'Bearer {token}',
                'Content-Type': 'application/json'
            }
            payload = {
                'messaging_product': 'whatsapp',
                'to': to_number,
                'type': 'text',
                'text': {'body': message}
            }
            r = retry_session.post(url, json=payload, headers=headers, timeout=10)
            return r.status_code in (200, 201), r.text

        return NotificationService._call_with_circuit_breaker('whatsapp', _send)

    @classmethod
    def notify_new_order(cls, order, items):
        """Send notifications dengan fallback chain: OpenRouter -> Telegram -> WhatsApp."""
        errors = []

        # 1. OpenRouter (AI-powered notification)
        if OPENROUTER_AVAILABLE and should_notify_new_order():
            try:
                if not CircuitBreaker.is_open('openrouter'):
                    openrouter_send('new_order', {
                        'order_number': order.order_number,
                        'customer_name': order.customer_name,
                        'customer_phone': order.customer_phone,
                        'total': order.total,
                        'items': [{'name': i['product'].name, 'quantity': i['quantity']} for i in items]
                    })
                    CircuitBreaker.record_success('openrouter')
            except Exception as e:
                CircuitBreaker.record_failure('openrouter')
                errors.append(f"OpenRouter: {e}")
                logger.error(f"OpenRouter notification failed: {e}")

        # 2. Telegram — build message regardless so WhatsApp fallback can use it
        lines = [
            "<b>🛒 PESANAN BARU</b>\n",
            f"<b>No. Order:</b> <code>{order.order_number}</code>",
            f"<b>Nama:</b> {order.customer_name}",
            f"<b>Telepon/WA:</b> {order.customer_phone}"
        ]
        if order.customer_email:
            lines.append(f"<b>Email:</b> {order.customer_email}")
        lines.append(f"<b>Alamat:</b> {order.shipping_address}\n")
        lines.append("<b>Detail Pesanan:</b>")
        for item in items:
            p = item['product']
            lines.append(f"• {p.name} ({p.brand} {p.model}) x {item['quantity']}")
            lines.append(f"  {format_price(p.price * item['quantity'])}")
        lines.append(f"\n<b>Total:</b> {format_price(order.total)}")
        lines.append(f"<b>Metode Pembayaran:</b> {order.payment_method}")
        lines.append(f"\n🕐 {datetime.now().strftime('%Y-%m-%d %H:%M:%S')}")
        tg = "\n".join(lines)

        telegram_enabled = get_setting('telegram_notif_new_order', 'true') in ('1', 'true', 'True')
        if telegram_enabled:
            success, msg = cls.send_telegram(tg)
            if not success:
                errors.append(f"Telegram: {msg}")

        # 3. WhatsApp Admin
        admin_wa = get_setting('whatsapp_recipient_number')
        if admin_wa:
            wa = tg.replace('<b>', '*').replace('</b>', '*').replace('<code>', '`').replace('</code>', '`')
            success, msg = cls.send_whatsapp(admin_wa, wa)
            if not success:
                errors.append(f"WhatsApp: {msg}")

        if errors:
            logger.warning(f"Notification fallback errors: {errors}")

    @classmethod
    def notify_status_update(cls, order, old_status=None, new_status=None):
        """Status update dengan fallback chain."""
        # OpenRouter
        if OPENROUTER_AVAILABLE and should_notify_status_update():
            try:
                if not CircuitBreaker.is_open('openrouter'):
                    openrouter_send('order_status_update', {
                        'order_number': order.order_number,
                        'status': order.status,
                        'customer_phone': order.customer_phone
                    })
                    CircuitBreaker.record_success('openrouter')
            except Exception as e:
                CircuitBreaker.record_failure('openrouter')
                logger.error(f"OpenRouter status update failed: {e}")

        # Telegram
        status_notify_key = f'telegram_notif_order_{order.status.lower()}'
        telegram_enabled = get_setting(status_notify_key, 'true') in ('1', 'true', 'True')
        if telegram_enabled:
            status_labels = {
                'Pending': '⏳ Menunggu Pembayaran',
                'Paid': '✅ Pembayaran Diterima',
                'Processing': '🔄 Diproses',
                'Ready_to_Ship': '📦 Siap Dikirim',
                'Shipped': '🚚 Sedang Dikirim',
                'Out_for_Delivery': '📍 Dalam Perjalanan',
                'Completed': '✅ Selesai',
                'Cancelled': '❌ Dibatalkan',
                'Returned': '↩️ Dikembalikan'
            }
            status_label = status_labels.get(order.status, order.status)
            lines = [
                f"<b>{status_label}</b>\n",
                f"<b>No. Order:</b> <code>{order.order_number}</code>",
                f"<b>Pelanggan:</b> {order.customer_name}",
                f"<b>Total:</b> {format_price(order.total)}"
            ]
            if order.tracking_number:
                lines.append(f"<b>No. Resi:</b> <code>{order.tracking_number}</code>")
            lines.append(f"\n🕐 {datetime.now().strftime('%Y-%m-%d %H:%M:%S')}")
            tg = "\n".join(lines)
            cls.send_telegram(tg)

        # WhatsApp Customer
        cls._send_wa_status(order)

    @staticmethod
    def _send_wa_status(order):
        if not order.customer_phone:
            return
        status_map = {
            'Pending': 'Pesanan Anda menunggu pembayaran. Silakan lakukan pembayaran untuk melanjutkan.',
            'Paid': 'Pembayaran Anda telah dikonfirmasi. Pesanan sedang diproses.',
            'Processing': 'Pesanan Anda sedang dikemas.',
            'Ready_to_Ship': 'Pesanan Anda siap dikirim. Pengiriman akan dimulai segera.',
            'Shipped': 'Pesanan Anda telah dikirim.',
            'Out_for_Delivery': 'Pesanan Anda sedang dalam perjalanan menuju alamat Anda.',
            'Completed': 'Pesanan telah selesai. Terima kasih telah berbelanja!',
            'Cancelled': 'Pesanan dibatalkan.',
            'Returned': 'Pesanan telah dikembalikan.'
        }
        msg = status_map.get(order.status, f'Status pesanan Anda: {order.status}')
        full = f"*Update Pesanan #{order.order_number}*\n\n{msg}"
        NotificationService.send_whatsapp(order.customer_phone, full)

def send_product_notification(product, action='new'):
    try:
        if action == 'new':
            title = f"✅ Produk Baru: {product.name}"
            msg = (
                f"📱 *{title}*\n\n"
                f"💰 Harga: {format_price(product.price)}\n"
                f"🏷️ Merek: {product.brand or '-'}\n"
                f"📊 Stok: {product.stock}\n"
                f"⭐ Kondisi: {product.condition or '-'}\n\n"
                f"🔗 {url_for('product_detail', id=product.id, _external=True)}"
            )
        else:
            title = f"🔄 Produk Diupdate: {product.name}"
            msg = (
                f"📱 *{title}*\n\n"
                f"💰 Harga: {format_price(product.price)}\n"
                f"📊 Stok: {product.stock}\n"
                f"⭐ Kondisi: {product.condition or '-'}"
            )
        NotificationService.send_telegram(msg)
    except Exception as e:
        logger.error(f"Product notification error: {e}")

# =============================================================================
# DATABASE INIT & DEFAULT ADMIN
# =============================================================================
def sync_missing_columns():
    """Add columns that exist on the SQLAlchemy models but not yet in the
    actual database table.

    db.create_all() ONLY creates tables that don't exist yet — once a table
    has been created once, create_all() never touches it again, even if the
    model gains new columns later. On a long-running deploy this means every
    new column silently never reaches production until someone runs a real
    migration, and the app crashes with "no such column: ..." the moment
    that column is queried (this is exactly what happened with
    kyc_verification_sessions.rejection_reason).

    This walks every mapped model, compares its columns against what's
    actually in the DB via SQLAlchemy's inspector, and ALTER TABLE ADD
    COLUMN's anything missing that's safe to add automatically (nullable,
    no server-side default expression required). Columns that are NOT
    NULL with no default are skipped and logged instead of guessed at,
    since adding those safely requires a real backfill decision.
    """
    inspector = db.inspect(db.engine)
    try:
        existing_tables = set(inspector.get_table_names())
    except Exception as e:
        logger.error(f"[schema-sync] Could not inspect database: {e}")
        return

    for mapper in db.Model.registry.mappers:
        table = mapper.local_table
        if table is None or table.name not in existing_tables:
            continue  # brand-new table: db.create_all() already handled it

        try:
            existing_cols = {c['name'] for c in inspector.get_columns(table.name)}
        except Exception as e:
            logger.error(f"[schema-sync] Could not inspect table {table.name}: {e}")
            continue

        for column in table.columns:
            if column.name in existing_cols:
                continue

            if not column.nullable and column.default is None and column.server_default is None:
                logger.warning(
                    f"[schema-sync] Column {table.name}.{column.name} is missing and is "
                    "NOT NULL with no default -- skipping automatic add. Add it manually "
                    "with a backfill (a proper Alembic/Flask-Migrate migration is strongly "
                    "recommended for schema changes like this going forward)."
                )
                continue

            try:
                col_type = column.type.compile(dialect=db.engine.dialect)
            except Exception as e:
                logger.error(f"[schema-sync] Could not compile type for {table.name}.{column.name}: {e}")
                continue

            ddl = f'ALTER TABLE "{table.name}" ADD COLUMN "{column.name}" {col_type}'
            try:
                with db.engine.begin() as conn:
                    conn.execute(text(ddl))
                logger.warning(f"[schema-sync] Added missing column: {table.name}.{column.name}")
            except Exception as e:
                logger.error(f"[schema-sync] Failed to add {table.name}.{column.name}: {e}")


with app.app_context():
    db.create_all()
    sync_missing_columns()
    # Backfill untuk kolom has_usable_password (ditambahkan lewat Google
    # OAuth): sync_missing_columns() menambah kolomnya via plain ALTER TABLE
    # ADD COLUMN tanpa DEFAULT, jadi baris user lama akan berisi NULL, bukan
    # True. User lama itu pasti daftar dengan password asli, jadi aman untuk
    # ditandai True di sini secara eksplisit.
    try:
        User.query.filter(User.has_usable_password.is_(None)).update(
            {User.has_usable_password: True}, synchronize_session=False
        )
        db.session.commit()
    except Exception as e:
        db.session.rollback()
        logger.warning(f"[schema-sync] Could not backfill has_usable_password: {e}")

    # ============================================================
    # REGISTER VISITOR STATS MODULE — TAMBAHKAN BARIS INI
    # ============================================================
    register_visitor_stats(app)   # <--- INI PERUBAHAN UTAMA

    if not Admin.query.first():
        try:
            admin = Admin(username='admin')
            # SECURITY: jangan pernah pakai password default yang bisa ditebak
            # (mis. 'admin123') di production. Bisa dikontrol lewat env var
            # ADMIN_INITIAL_PASSWORD untuk deploy yang terkontrol; jika tidak
            # diset, generate password acak dan tampilkan SEKALI di log saat
            # startup supaya admin bisa login pertama kali lalu wajib
            # menggantinya dari halaman /admin/settings.
            initial_password = os.getenv('ADMIN_INITIAL_PASSWORD') or secrets.token_urlsafe(12)
            admin.set_password(initial_password)
            db.session.add(admin)
            db.session.commit()
            logger.warning(
                "=" * 70 + "\n"
                "Default admin account created -- username: admin\n"
                f"Initial password: {initial_password}\n"
                "SIMPAN password ini sekarang dan segera ganti dari /admin/settings. "
                "Password ini tidak akan ditampilkan lagi.\n" + "=" * 70
            )
        except IntegrityError:
            db.session.rollback()
        except Exception as e:
            db.session.rollback()
            logger.error(f'Error creating default admin: {e}')

# =============================================================================
# PUBLIC ROUTES
# =============================================================================
@app.route('/')
def index():
    """Homepage - showcase handphone second & baru."""
    # Featured handphone (second & baru)
    featured = Product.query.filter_by(is_featured=True, is_active=True)        .order_by(Product.created_at.desc()).limit(8).all()

    # Latest handphone listings (second & baru)
    products = Product.query.filter_by(is_active=True)        .order_by(Product.created_at.desc()).limit(20).all()

    total_products = Product.query.filter_by(is_active=True).count()

    # Get distinct brands for filter
    brands = db.session.query(Product.brand).distinct()        .filter(Product.brand.isnot(None), Product.is_active == True).all()
    brands = [b[0] for b in brands if b[0]]

    # Get condition counts for display
    second_count = Product.query.filter(
        Product.condition.in_(['Like New', 'Excellent', 'Good']),
        Product.is_active == True
    ).count()
    baru_count = Product.query.filter_by(condition='Baru', is_active=True).count()

    return render_template('index.html',
                           featured=featured,
                           featured_products=featured,  # alias for base.html compatibility
                           products=products,
                           total_products=total_products,
                           brands=brands,
                           second_count=second_count,
                           baru_count=baru_count)

@app.route('/catalog')
def catalog():
    q = request.args.get('q', '').strip()
    brand = request.args.get('brand', '').strip()
    condition = request.args.get('condition', '').strip()
    sort = request.args.get('sort', 'newest')
    page = request.args.get('page', 1, type=int)
    per_page = 20
    query = Product.query
    if q:
        query = query.filter(or_(
            Product.name.ilike(f'%{q}%'),
            Product.brand.ilike(f'%{q}%'),
            Product.model.ilike(f'%{q}%')
        ))
    if brand and brand != 'all':
        query = query.filter_by(brand=brand)
    if condition and condition != 'all':
        query = query.filter_by(condition=condition)
    if sort == 'price_asc':
        query = query.order_by(asc(Product.price))
    elif sort == 'price_desc':
        query = query.order_by(desc(Product.price))
    else:
        query = query.order_by(desc(Product.created_at))
    paginated = query.paginate(page=page, per_page=per_page, error_out=False)
    all_brands = db.session.query(Product.brand).distinct().all()
    all_brands = [b[0] for b in all_brands if b[0]]
    return render_template('catalog.html', products=paginated.items, brands=all_brands,
                           pagination=paginated, current_q=q, current_brand=brand,
                           current_condition=condition, current_sort=sort)

@app.route('/product/<int:id>')
def product_detail(id):
    product = Product.query.get_or_404(id)
    related = Product.query.filter(Product.brand == product.brand, Product.id != id).limit(4).all()

    # BUGFIX: product.html memakai images/has_images/discount_pct/has_dimensions
    # (galeri, badge hemat %, blok dimensi) tapi sebelumnya tidak pernah dikirim
    # dari sini, sehingga {% if discount_pct > 0 %} menabrak Undefined dan
    # halaman produk selalu crash (500) begitu diakses.
    images = product.get_images()
    has_images = len(images) > 0

    discount_pct = 0
    if product.compare_at_price and product.compare_at_price > product.price:
        discount_pct = round(
            (product.compare_at_price - product.price) / product.compare_at_price * 100
        )

    has_dimensions = any([product.weight, product.width, product.length, product.height])

    # User yang sudah KYC verified punya KTP+selfie tervalidasi tersimpan di
    # KYCVerificationSession — dipakai supaya mereka tidak perlu upload ulang
    # dokumen yang sama setiap kali mengajukan kredit produk baru.
    verified_kyc_session = None
    if current_user.is_authenticated and isinstance(current_user, User) and current_user.kyc_verified:
        verified_kyc_session = get_latest_verified_kyc_session(current_user.id)

    credit_enabled = is_credit_available_for_current_user()

    return render_template('product.html', product=product, related=related,
                           images=images, has_images=has_images,
                           discount_pct=discount_pct, has_dimensions=has_dimensions,
                           verified_kyc_session=verified_kyc_session,
                           credit_enabled=credit_enabled)

@app.route('/about')
def about():
    # Statistik nyata dari database (bukan angka hardcoded)
    units_sold = db.session.query(
        db.func.coalesce(db.func.sum(OrderItem.quantity), 0)
    ).join(Order, OrderItem.order_id == Order.id).filter(
        Order.status == 'Completed'
    ).scalar() or 0

    active_testimonials = Testimonial.query.filter_by(is_active=True).all()
    review_count = len(active_testimonials)
    if review_count > 0:
        satisfaction_pct = round(
            sum(1 for t in active_testimonials if t.rating >= 4) / review_count * 100
        )
    else:
        satisfaction_pct = None

    founded_year = int(get_setting('store_founded_year', '2018'))
    years_experience = max(datetime.utcnow().year - founded_year, 0)
    warranty_days = int(get_setting('warranty_days', '30'))

    stats = {
        # Fallback ke angka lama hanya jika belum ada transaksi/review di database
        'units_sold': units_sold if units_sold > 0 else 15000,
        'satisfaction_pct': satisfaction_pct if satisfaction_pct is not None else 98,
        'review_count': review_count if review_count > 0 else 2500,
        'years_experience': years_experience if years_experience > 0 else 7,
        'warranty_days': warranty_days,
        'founded_year': founded_year,
    }
    return render_template('about.html', stats=stats)

@app.route('/contact')
def contact():
    return render_template('contact.html')

@app.route('/panduan-kredit')
def panduan_kredit():
    """Panduan lengkap daftar akun, verifikasi KYC, dan pengajuan kredit/cicilan."""
    return render_template('panduan-daftar-dan-kredit.html')

# =============================================================================
# KYC FACE VERIFICATION ROUTES
# =============================================================================

def _is_safe_local_redirect(target: str) -> bool:
    """Validasi bahwa target redirect adalah path lokal yang aman (bukan URL
    eksternal). Mencegah open-redirect: menolak URL dengan scheme/host
    (http://evil.com), protocol-relative (//evil.com), atau backslash yang
    bisa diinterpretasikan browser sebagai protocol-relative (\\evil.com)."""
    if not target:
        return False
    if target.startswith('//') or target.startswith('/\\') or target.startswith('\\'):
        return False
    parsed = urlparse(target)
    return not parsed.netloc and not parsed.scheme and target.startswith('/')

@app.route('/kyc-verification')
@login_required
def kyc_verification_page():
    """KYC Face Verification page using face-api.js."""
    if not isinstance(current_user, User):
        flash('Akses hanya untuk pelanggan.', 'error')
        return redirect(url_for('index'))

    # Get & validate product_id dan redirect dari query string terlebih dahulu,
    # supaya bisa dipakai baik untuk redirect "sudah verified" maupun untuk
    # dikirim ke template saat user memang perlu verifikasi.
    product_id = request.args.get('product_id', type=int)
    if product_id is not None and not Product.query.get(product_id):
        product_id = None  # produk tidak valid/tidak ditemukan, abaikan saja

    raw_redirect = request.args.get('redirect', '')
    redirect_url = raw_redirect if _is_safe_local_redirect(raw_redirect) else ''

    def _fallback_destination():
        if redirect_url:
            return redirect(redirect_url)
        if product_id:
            return redirect(url_for('product_detail', id=product_id))
        return redirect(url_for('user_profile'))

    # Sudah terverifikasi penuh → kembalikan ke tujuan semula
    if current_user.kyc_verified:
        flash('Anda sudah terverifikasi KYC.', 'info')
        return _fallback_destination()

    # Cek apakah ada pengajuan yang sedang menunggu review admin
    kyc_pending_session = KYCVerificationSession.query.filter_by(
        user_id=current_user.id, status='pending_review'
    ).order_by(KYCVerificationSession.id.desc()).first()

    return render_template('kyc.html',
                          product_id=product_id,
                          redirect_url=redirect_url,
                          user=current_user,
                          kyc_pending=kyc_pending_session)

def get_latest_verified_kyc_session(user_id: int):
    """Return the user's most recent, non-expired verified KYC session, or
    None. Used to link credit/installment applications to the KYC record
    that authorized them, so admins can review the biometric match."""
    session_obj = KYCVerificationSession.query.filter_by(
        user_id=user_id, status='verified'
    ).order_by(KYCVerificationSession.verified_at.desc()).first()
    if session_obj and session_obj.expires_at and datetime.utcnow() > session_obj.expires_at:
        return None
    return session_obj

@app.route('/api/kyc/verify', methods=['POST'])
@login_required
@csrf_protect_endpoint
def api_kyc_verify():
    """
    Process KYC verification with biometric data from face-api.js.
    Receives: ktp_photo, selfie_photo, ktp_face_descriptor, selfie_face_descriptor, match_score
    """
    if not isinstance(current_user, User):
        return jsonify({'success': False, 'message': 'User only'}), 403

    # Check if KYC already verified
    if current_user.kyc_verified:
        return jsonify({'success': True, 'message': 'Already verified', 'verified': True}), 200

    # Get uploaded files
    ktp_file = request.files.get('ktp_photo')
    selfie_file = request.files.get('selfie_photo')

    if not ktp_file or not selfie_file:
        return jsonify({'success': False, 'message': 'KTP dan foto selfie wajib diupload'}), 400

    # Save photos
    ktp_filename = save_uploaded_image(ktp_file, prefix='kyc_ktp_', subfolder='kyc_docs')
    selfie_filename = save_uploaded_image(selfie_file, prefix='kyc_selfie_', subfolder='kyc_docs')

    if not ktp_filename or not selfie_filename:
        return jsonify({'success': False, 'message': 'Format file tidak didukung'}), 400

    # Get biometric data
    ktp_descriptor = request.form.get('ktp_face_descriptor', '[]')
    selfie_descriptor = request.form.get('selfie_face_descriptor', '[]')
    match_score = request.form.get('match_score', type=float, default=0)
    liveness_score = request.form.get('liveness_score', type=float, default=0)

    # Data tambahan: pekerjaan, tanggal lahir, penghasilan, kontak darurat
    birth_date_raw = request.form.get('ktp_birth_date', '').strip()
    birth_date_val = None
    if birth_date_raw:
        try:
            birth_date_val = datetime.strptime(birth_date_raw, '%Y-%m-%d').date()
        except ValueError:
            birth_date_val = None

    occupation = request.form.get('ktp_occupation', '').strip()
    employment_status = request.form.get('ktp_employment_status', '').strip()
    monthly_income = request.form.get('ktp_monthly_income', type=int, default=0)
    emergency_contact_name = request.form.get('ktp_emergency_name', '').strip()
    emergency_contact_phone = request.form.get('ktp_emergency_phone', '').strip()
    emergency_contact_relation = request.form.get('ktp_emergency_relation', '').strip()

    # Parse descriptors
    try:
        ktp_desc = json.loads(ktp_descriptor) if isinstance(ktp_descriptor, str) else ktp_descriptor
        selfie_desc = json.loads(selfie_descriptor) if isinstance(selfie_descriptor, str) else selfie_descriptor
    except json.JSONDecodeError:
        ktp_desc = []
        selfie_desc = []

    # Validate match score
    min_confidence = float(get_setting('kyc_min_confidence', '70'))
    if match_score < min_confidence:
        return jsonify({
            'success': False,
            'message': f'Skor kecocokan wajah ({match_score:.1f}%) di bawah minimum ({min_confidence}%). Silakan coba lagi dengan pencahayaan lebih baik.',
            'match_score': match_score
        }), 400

    # Validate liveness (anti-spoofing)
    min_liveness = float(get_setting('kyc_min_liveness', '50'))
    if liveness_score > 0 and liveness_score < min_liveness:
        return jsonify({
            'success': False,
            'message': 'Verifikasi liveness gagal. Pastikan Anda mengambil foto selfie secara real-time.',
            'liveness_score': liveness_score
        }), 400

    # Tolak jika sudah ada sesi pending_review yang aktif (hindari duplikat submission)
    existing_pending = KYCVerificationSession.query.filter_by(
        user_id=current_user.id, status='pending_review'
    ).first()
    if existing_pending:
        return jsonify({
            'success': True,
            'pending': True,
            'message': 'Pengajuan KYC Anda sudah dalam antrian review admin. Silakan tunggu konfirmasi.'
        })

    # Buat KYC session dengan status pending_review — belum disetujui admin
    session_token = secrets.token_urlsafe(32)

    kyc_session = KYCVerificationSession(
        user_id=current_user.id,
        session_token=session_token,
        ktp_face_descriptor=json.dumps(ktp_desc) if ktp_desc else None,
        selfie_face_descriptor=json.dumps(selfie_desc) if selfie_desc else None,
        face_match_score=match_score,
        face_match_confidence=match_score,
        liveness_score=liveness_score,
        ktp_photo_path=ktp_filename,
        selfie_photo_path=selfie_filename,
        ktp_nik=request.form.get('ktp_nik', '').strip(),
        ktp_name=request.form.get('ktp_name', '').strip(),
        ktp_address=request.form.get('ktp_address', '').strip(),
        birth_date=birth_date_val,
        occupation=occupation or None,
        employment_status=employment_status or None,
        monthly_income=monthly_income or None,
        emergency_contact_name=emergency_contact_name or None,
        emergency_contact_phone=emergency_contact_phone or None,
        emergency_contact_relation=emergency_contact_relation or None,
        status='pending_review',          # ← menunggu persetujuan manual admin
        verification_method='face_api',
        ip_address=request.remote_addr,
        user_agent=request.user_agent.string if request.user_agent else None
        # verified_at & expires_at diisi saat admin approve
    )

    # Simpan foto ke profil user agar admin bisa melihat konteks
    # (kyc_verified tetap False sampai admin approve)
    current_user.kyc_face_descriptor = json.dumps(selfie_desc) if selfie_desc else None
    current_user.kyc_ktp_photo = ktp_filename
    current_user.kyc_selfie_photo = selfie_filename
    current_user.kyc_nik = request.form.get('ktp_nik', '').strip()
    current_user.kyc_match_score = match_score

    try:
        db.session.add(kyc_session)
        db.session.commit()

        # Notifikasi admin: ada pengajuan KYC baru yang perlu direview
        try:
            review_url = url_for('admin_kyc_applications', _external=True)
            msg = (
                f"🔐 <b>PENGAJUAN KYC BARU — PERLU REVIEW</b>\n\n"
                f"<b>Pelanggan:</b> {current_user.full_name}\n"
                f"<b>Email:</b> {current_user.email}\n"
                f"<b>NIK:</b> <code>{request.form.get('ktp_nik', 'N/A')}</code>\n"
                f"<b>Skor Kecocokan:</b> {match_score:.1f}%\n"
                f"<b>Liveness:</b> {liveness_score:.1f}%\n"
                f"<b>Waktu:</b> {datetime.now().strftime('%Y-%m-%d %H:%M:%S')}\n\n"
                f"👉 <a href='{review_url}'>Review di Panel Admin</a>"
            )
            NotificationService.send_telegram(msg)
        except Exception as e:
            logger.error(f"KYC notification error: {e}")

        return jsonify({
            'success': True,
            'pending': True,
            'message': 'Pengajuan KYC berhasil dikirim! Tim kami akan mereview dan mengkonfirmasi dalam 1×24 jam.',
            'session_token': session_token,
            'match_score': match_score,
        })

    except Exception as e:
        db.session.rollback()
        logger.error(f"KYC verification error: {e}", exc_info=True)
        return jsonify({'success': False, 'message': f'Terjadi kesalahan: {str(e)}'}), 500

@app.route('/api/kyc/status', methods=['GET'])
@login_required
def api_kyc_status():
    """Check current user's KYC verification status."""
    if not isinstance(current_user, User):
        return jsonify({'success': False, 'message': 'User only'}), 403

    # Cek sesi pending_review terlebih dahulu
    pending_session = KYCVerificationSession.query.filter_by(
        user_id=current_user.id, status='pending_review'
    ).order_by(KYCVerificationSession.id.desc()).first()

    if pending_session:
        return jsonify({
            'success': True,
            'kyc_verified': False,
            'kyc_pending': True,
            'kyc_status': 'pending_review',
            'kyc_verified_at': None,
            'match_score': current_user.kyc_match_score,
            'session_expired': False,
            'session_expires_at': None
        })

    # Cek sesi verified
    latest_session = KYCVerificationSession.query.filter_by(
        user_id=current_user.id, status='verified'
    ).order_by(KYCVerificationSession.verified_at.desc()).first()

    is_expired = False
    if latest_session and latest_session.expires_at:
        is_expired = datetime.utcnow() > latest_session.expires_at

    return jsonify({
        'success': True,
        'kyc_verified': current_user.kyc_verified and not is_expired,
        'kyc_pending': False,
        'kyc_status': 'verified' if (current_user.kyc_verified and not is_expired) else 'not_verified',
        'kyc_verified_at': current_user.kyc_verified_at.isoformat() if current_user.kyc_verified_at else None,
        'match_score': current_user.kyc_match_score,
        'session_expired': is_expired,
        'session_expires_at': latest_session.expires_at.isoformat() if latest_session and latest_session.expires_at else None
    })

@app.route('/api/kyc/renew', methods=['POST'])
@login_required
@csrf_protect_endpoint
def api_kyc_renew():
    """Renew expired KYC verification."""
    if not isinstance(current_user, User):
        return jsonify({'success': False, 'message': 'User only'}), 403

    # Reset KYC status user
    current_user.kyc_verified = False
    current_user.kyc_verified_at = None
    current_user.kyc_face_descriptor = None

    # Batalkan semua sesi verified maupun pending_review
    KYCVerificationSession.query.filter(
        KYCVerificationSession.user_id == current_user.id,
        KYCVerificationSession.status.in_(['verified', 'pending_review'])
    ).update({'status': 'cancelled'}, synchronize_session=False)

    db.session.commit()

    return jsonify({
        'success': True,
        'message': 'Pengajuan KYC dibatalkan. Silakan ajukan ulang.',
        'redirect': url_for('kyc_verification_page')
    })

@app.route('/static/uploads/kyc_docs/<filename>')
@login_required
def uploaded_kyc_doc(filename):
    """Serve KYC documents - admin or owner only."""
    if isinstance(current_user, Admin):
        return send_from_directory(UPLOAD_KYC_DOCS, filename)

    # Check if user owns this document
    user_has_doc = (
        current_user.kyc_ktp_photo == filename or
        current_user.kyc_selfie_photo == filename
    )

    if user_has_doc:
        return send_from_directory(UPLOAD_KYC_DOCS, filename)

    # Check via KYC session
    kyc_sess = KYCVerificationSession.query.filter(
        or_(KYCVerificationSession.ktp_photo_path == filename,
            KYCVerificationSession.selfie_photo_path == filename)
    ).filter_by(user_id=current_user.id).first()

    if kyc_sess:
        return send_from_directory(UPLOAD_KYC_DOCS, filename)

    return jsonify({'success': False, 'message': 'Akses ditolak'}), 403

# =============================================================================
# ADMIN KYC REVIEW DASHBOARD
# =============================================================================

@app.route('/admin/kyc-applications')
@admin_required
def admin_kyc_applications():
    """Daftar pengajuan KYC yang perlu di-review oleh admin."""
    status_filter = request.args.get('status', 'pending_review')
    page = request.args.get('page', 1, type=int)
    per_page = 25

    # joinedload user & reviewer di sini supaya tidak query user satu-per-satu
    # di dalam loop (N+1) seperti sebelumnya.
    query = KYCVerificationSession.query.options(
        joinedload(KYCVerificationSession.user),
        joinedload(KYCVerificationSession.reviewed_by_admin),
    )
    if status_filter and status_filter != 'all':
        query = query.filter_by(status=status_filter)

    pagination = query.order_by(KYCVerificationSession.id.desc()).paginate(
        page=page, per_page=per_page, error_out=False
    )

    sessions_with_users = [{'session': sess, 'user': sess.user} for sess in pagination.items]

    stats = {
        'total': KYCVerificationSession.query.count(),
        'pending_review': KYCVerificationSession.query.filter_by(status='pending_review').count(),
        'verified': KYCVerificationSession.query.filter_by(status='verified').count(),
        'rejected': KYCVerificationSession.query.filter_by(status='rejected').count(),
        'cancelled': KYCVerificationSession.query.filter_by(status='cancelled').count(),
    }

    # Ambang batas yang sedang berlaku (dipakai template untuk pewarnaan skor)
    min_confidence = float(get_setting('kyc_min_confidence', '70'))
    min_liveness = float(get_setting('kyc_min_liveness', '50'))

    return render_template(
        'admin/kyc_applications.html',
        sessions=sessions_with_users,
        pagination=pagination,
        status_filter=status_filter,
        stats=stats,
        min_confidence=min_confidence,
        min_liveness=min_liveness,
    )


@app.route('/admin/kyc-application/<int:session_id>/review', methods=['POST'])
@admin_required
@csrf_protect_endpoint
def admin_review_kyc(session_id):
    """Approve atau reject pengajuan KYC oleh admin."""
    kyc_sess = KYCVerificationSession.query.get_or_404(session_id)

    if kyc_sess.status != 'pending_review':
        return jsonify({'success': False, 'message': 'Pengajuan ini sudah diproses sebelumnya.'}), 400

    data = request.get_json(silent=True) or {}
    action = data.get('action', '').strip()

    if action not in ('approve', 'reject'):
        return jsonify({'success': False, 'message': 'Aksi tidak valid'}), 400

    user = User.query.get(kyc_sess.user_id)
    if not user:
        return jsonify({'success': False, 'message': 'User tidak ditemukan'}), 404

    if action == 'approve':
        expires_at = datetime.utcnow() + timedelta(hours=Config.KYC_SESSION_EXPIRY_HOURS)
        kyc_sess.status = 'verified'
        kyc_sess.verified_at = datetime.utcnow()
        kyc_sess.expires_at = expires_at
        kyc_sess.reviewed_by = current_user.id

        # Aktifkan akses kredit user
        user.kyc_verified = True
        user.kyc_verified_at = datetime.utcnow()
        user.kyc_verification_method = 'face_api'
        db.session.commit()

        # Notifikasi user
        try:
            msg = (
                f"✅ *VERIFIKASI KYC DISETUJUI*\n\n"
                f"Selamat {user.full_name}! Identitas Anda telah diverifikasi oleh admin.\n"
                f"Kini Anda dapat mengajukan kredit & cicilan di platform kami.\n\n"
                f"Silakan login untuk melanjutkan pengajuan kredit."
            )
            NotificationService.send_whatsapp(user.phone, msg) if user.phone else None
        except Exception as e:
            logger.error(f"KYC approve notification error: {e}")

        logger.info(f"KYC approved: session={session_id} user={user.id} by admin={current_user.id}")
        return jsonify({'success': True, 'message': f'KYC {user.full_name} berhasil disetujui'})

    elif action == 'reject':
        reason = data.get('reason', '').strip()
        kyc_sess.status = 'rejected'
        kyc_sess.rejection_reason = reason
        kyc_sess.reviewed_by = current_user.id
        db.session.commit()

        # Notifikasi user
        try:
            msg = (
                f"❌ *VERIFIKASI KYC DITOLAK*\n\n"
                f"Maaf {user.full_name}, pengajuan KYC Anda tidak dapat disetujui.\n"
                f"{'Alasan: ' + reason if reason else ''}\n\n"
                f"Silakan hubungi kami atau ajukan ulang dengan dokumen yang lebih jelas."
            )
            NotificationService.send_whatsapp(user.phone, msg) if user.phone else None
        except Exception as e:
            logger.error(f"KYC reject notification error: {e}")

        logger.info(f"KYC rejected: session={session_id} user={user.id} reason='{reason}' by admin={current_user.id}")
        return jsonify({'success': True, 'message': f'KYC {user.full_name} ditolak'})


# =============================================================================
# USER AUTH PAGES (login.html & register.html)
# =============================================================================
def _generate_unique_username(seed: str) -> str:
    """Turunkan username unik dari bagian lokal email, karena form register
    tidak meminta username secara eksplisit."""
    base = re.sub(r'[^a-z0-9_.]', '', seed.strip().lower()) or 'user'
    candidate = base
    suffix = 1
    while User.query.filter_by(username=candidate).first():
        suffix += 1
        candidate = f'{base}{suffix}'
    return candidate

@app.route('/login', methods=['GET', 'POST'])
def user_login():
    """Halaman login (login.html). Form di-submit via AJAX (FormData) ke URL ini."""
    if current_user.is_authenticated and isinstance(current_user, User):
        return redirect(url_for('index'))

    if request.method == 'POST':
        email = request.form.get('email', '').strip()
        password = request.form.get('password', '')
        remember = request.form.get('remember') == '1'

        if not email or not password:
            return jsonify({'success': False, 'message': 'Harap isi email dan kata sandi Anda.'}), 400

        user = User.query.filter(
            (User.email == email) | (User.username == email)
        ).first()

        if user and user.check_password(password):
            login_user(user, remember=remember)
            # FIX: setelah login berhasil, arahkan user langsung ke halaman
            # profil (bukan ke beranda) — kecuali ada tujuan spesifik ('next',
            # mis. dari checkout/KYC yang butuh login dulu), yang tetap
            # diprioritaskan agar alur tersebut tidak putus.
            raw_redirect = request.args.get('next', '') or request.form.get('next', '')
            redirect_url = raw_redirect if _is_safe_local_redirect(raw_redirect) else url_for('user_profile')
            return jsonify({
                'success': True,
                'message': 'Login berhasil',
                'redirect': redirect_url
            })

        return jsonify({'success': False, 'message': 'Email atau kata sandi salah.'}), 401

    return render_template('login.html')

@app.route('/register', methods=['GET', 'POST'])
def user_register():
    """Halaman daftar akun (register.html). Form di-submit via AJAX (FormData) ke URL ini."""
    if current_user.is_authenticated and isinstance(current_user, User):
        return redirect(url_for('index'))

    if request.method == 'POST':
        full_name = request.form.get('fullname', '').strip()
        username = request.form.get('username', '').strip()
        email = request.form.get('email', '').strip()
        phone = request.form.get('phone', '').strip()
        password = request.form.get('password', '')
        confirm_password = request.form.get('confirm_password', '')

        if not full_name or not email or not password:
            return jsonify({'success': False, 'message': 'Harap isi semua kolom wajib.'}), 400
        if username and not re.match(r'^[a-zA-Z0-9_.]{3,20}$', username):
            return jsonify({'success': False, 'message': 'Username 3-20 karakter: huruf, angka, titik, atau garis bawah.'}), 400
        if len(password) < 8:
            return jsonify({'success': False, 'message': 'Kata sandi minimal 8 karakter.'}), 400
        if password != confirm_password:
            return jsonify({'success': False, 'message': 'Konfirmasi kata sandi tidak cocok.'}), 400
        if username and User.query.filter_by(username=username).first():
            return jsonify({'success': False, 'message': 'Username sudah digunakan.'}), 400
        if User.query.filter_by(email=email).first():
            return jsonify({'success': False, 'message': 'Email sudah terdaftar.'}), 400
        if phone and User.query.filter_by(phone=phone).first():
            return jsonify({'success': False, 'message': 'Nomor telepon sudah terdaftar.'}), 400

        try:
            # Fallback jaga-jaga jika form lama/cache tidak mengirim username
            username = username or _generate_unique_username(email.split('@')[0])
            user = User(
                username=username,
                email=email,
                full_name=full_name,
                phone=phone or None,
            )
            user.set_password(password)
            db.session.add(user)
            db.session.commit()
            login_user(user)
            # FIX: user baru langsung diarahkan ke halaman profil (bukan
            # beranda), karena mereka sudah otomatis login (login_user di
            # atas) begitu registrasi sukses — tidak perlu login manual lagi.
            return jsonify({
                'success': True,
                'message': 'Registrasi berhasil! Selamat datang, ' + user.full_name,
                'redirect': url_for('user_profile')
            })
        except IntegrityError:
            db.session.rollback()
            return jsonify({'success': False, 'message': 'Username, email, atau nomor telepon sudah terdaftar.'}), 400
        except Exception as e:
            db.session.rollback()
            logger.error(f'Registration error: {e}')
            return jsonify({'success': False, 'message': 'Terjadi kesalahan saat registrasi.'}), 500

    return render_template('register.html')

@app.route('/forgot-password')
def user_forgot_password():
    """Halaman / aksi lupa kata sandi - mengarahkan ke CS WhatsApp."""
    wa_number = get_setting('whatsapp_number', '').strip()
    if wa_number:
        wa_clean = re.sub(r'\D', '', wa_number)
        if wa_clean.startswith('0'):
            wa_clean = '62' + wa_clean[1:]
        email = request.args.get('email', '').strip()
        text = f"Halo Admin, saya lupa kata sandi akun saya{f' ({email})' if email else ''}. Mohon bantuannya untuk reset kata sandi."
        return redirect(f"https://wa.me/{wa_clean}?text={quote(text)}")
    flash('Silakan hubungi customer service kami melalui WhatsApp untuk reset kata sandi.', 'info')
    return redirect(url_for('user_login'))

# =============================================================================
# GOOGLE OAUTH LOGIN
# =============================================================================
@app.route('/login/google')
def google_login():
    """Mulai alur OAuth: redirect user ke halaman consent Google."""
    if not GOOGLE_OAUTH_ENABLED:
        flash('Login dengan Google belum diaktifkan di server ini.', 'error')
        return redirect(url_for('user_login'))

    if current_user.is_authenticated and isinstance(current_user, User):
        return redirect(url_for('index'))

    # Simpan tujuan redirect setelah login (mis. checkout) di session supaya
    # tidak hilang saat bolak-balik ke Google -- query string 'next' tidak
    # bisa diandalkan dikembalikan apa adanya oleh semua browser/redirect.
    raw_next = request.args.get('next', '')
    if _is_safe_local_redirect(raw_next):
        session['oauth_next'] = raw_next
    else:
        session.pop('oauth_next', None)

    redirect_uri = url_for('google_callback', _external=True)
    return oauth.google.authorize_redirect(redirect_uri)

@app.route('/login/google/callback')
def google_callback():
    """Callback yang dipanggil Google setelah user menyetujui/menolak consent."""
    if not GOOGLE_OAUTH_ENABLED:
        return redirect(url_for('user_login'))

    try:
        token = oauth.google.authorize_access_token()
    except Exception as e:
        logger.error(f"Google OAuth token exchange failed: {e}")
        flash('Gagal masuk dengan Google. Silakan coba lagi.', 'error')
        return redirect(url_for('user_login'))

    userinfo = token.get('userinfo') if isinstance(token, dict) else None
    if not userinfo:
        try:
            userinfo = oauth.google.userinfo(token=token)
        except Exception as e:
            logger.error(f"Google OAuth userinfo fetch failed: {e}")
            userinfo = None

    if not userinfo or not userinfo.get('sub') or not userinfo.get('email'):
        flash('Tidak dapat mengambil data akun Google Anda. Silakan coba lagi.', 'error')
        return redirect(url_for('user_login'))

    if userinfo.get('email_verified') is False:
        flash('Email akun Google Anda belum terverifikasi oleh Google.', 'error')
        return redirect(url_for('user_login'))

    google_id = str(userinfo['sub'])
    email = userinfo['email'].strip().lower()
    full_name = (userinfo.get('name') or email.split('@')[0]).strip()
    avatar_url = userinfo.get('picture')

    try:
        user = User.query.filter_by(google_id=google_id).first()

        if not user:
            # Belum pernah login via Google dengan akun ini -- coba samakan
            # dengan akun email/password yang sudah ada supaya tidak dobel
            # akun untuk orang yang sama, baru kalau memang belum ada buat
            # akun baru.
            user = User.query.filter(func.lower(User.email) == email).first()
            if user:
                user.google_id = google_id
                user.oauth_provider = user.oauth_provider or 'google'
                if avatar_url:
                    user.google_avatar_url = avatar_url
            else:
                username = _generate_unique_username(email.split('@')[0])
                user = User(
                    username=username,
                    email=email,
                    full_name=full_name,
                    google_id=google_id,
                    oauth_provider='google',
                    google_avatar_url=avatar_url,
                )
                user.set_unusable_password()
                db.session.add(user)
            db.session.commit()
        else:
            # Sudah pernah login Google sebelumnya -- cukup segarkan foto
            # profil kalau berubah, tidak perlu bikin/link akun lagi.
            if avatar_url and user.google_avatar_url != avatar_url:
                user.google_avatar_url = avatar_url
                db.session.commit()
    except IntegrityError:
        db.session.rollback()
        logger.error(f"Google OAuth account conflict for email={email}")
        flash('Email ini sudah terdaftar dengan cara lain. Silakan hubungi dukungan pelanggan.', 'error')
        return redirect(url_for('user_login'))
    except Exception as e:
        db.session.rollback()
        logger.error(f"Google OAuth account provisioning error: {e}")
        flash('Terjadi kesalahan saat memproses login Google Anda.', 'error')
        return redirect(url_for('user_login'))

    if not user.is_active:
        flash('Akun Anda telah dinonaktifkan. Hubungi dukungan pelanggan.', 'error')
        return redirect(url_for('user_login'))

    login_user(user)
    logger.info(f"Google OAuth login success: user={user.id} email={user.email}")

    next_url = session.pop('oauth_next', None)
    redirect_url = next_url if _is_safe_local_redirect(next_url) else url_for('user_profile')
    flash(f'Berhasil masuk dengan Google. Selamat datang, {user.full_name}!', 'success')
    return redirect(redirect_url)

# =============================================================================
# USER AUTH API
# =============================================================================
@app.route('/api/user/login', methods=['POST'])
def user_login_api():
    data = request.get_json()
    if not data:
        return jsonify({'success': False, 'message': 'No data'}), 400
    email = data.get('email', '').strip()
    password = data.get('password', '')
    user = User.query.filter((User.email == email) | (User.username == email)).first()
    if user and user.check_password(password):
        login_user(user)
        return jsonify({
            'success': True,
            'message': 'Login berhasil',
            'name': user.full_name,
            'kyc_verified': user.kyc_verified,
            'redirect': url_for('user_profile')
        })
    return jsonify({'success': False, 'message': 'Email atau password salah'}), 401

@app.route('/api/user/register', methods=['POST'])
def user_register_api():
    data = request.get_json()
    if not data:
        return jsonify({'success': False, 'message': 'No data'}), 400
    username = data.get('username', '').strip()
    full_name = data.get('full_name', '').strip()
    email = data.get('email', '').strip()
    phone = data.get('phone', '').strip()
    password = data.get('password', '')
    if not all([username, full_name, email, phone, password]):
        return jsonify({'success': False, 'message': 'Semua field wajib diisi'}), 400
    if len(password) < 8:
        return jsonify({'success': False, 'message': 'Kata sandi minimal 8 karakter'}), 400
    if User.query.filter_by(username=username).first():
        return jsonify({'success': False, 'message': 'Username sudah terdaftar'}), 400
    if User.query.filter_by(email=email).first():
        return jsonify({'success': False, 'message': 'Email sudah terdaftar'}), 400
    if User.query.filter_by(phone=phone).first():
        return jsonify({'success': False, 'message': 'Nomor telepon sudah terdaftar'}), 400

    try:
        user = User(username=username, email=email, full_name=full_name, phone=phone)
        user.set_password(password)
        db.session.add(user)
        db.session.commit()
        login_user(user)
        return jsonify({
            'success': True,
            'message': 'Registrasi berhasil! Selamat datang, ' + user.full_name,
            'name': user.full_name,
            'kyc_verified': False,
            'redirect': url_for('user_profile')
        })
    except IntegrityError:
        db.session.rollback()
        return jsonify({'success': False, 'message': 'Username, email, atau nomor telepon sudah terdaftar'}), 400
    except Exception as e:
        db.session.rollback()
        logger.error(f'Registration error: {e}')
        return jsonify({'success': False, 'message': 'Terjadi kesalahan saat registrasi'}), 500

@app.route('/api/user/logout', methods=['POST'])
@login_required
def user_logout_api():
    if isinstance(current_user, User):
        logout_user()
        return jsonify({'success': True, 'message': 'Logout berhasil'})
    return jsonify({'success': False, 'message': 'Tidak ada sesi user'}), 400

@app.route('/api/contact', methods=['POST'])
def api_contact():
    data = request.get_json()
    if not data:
        return jsonify({'success': False, 'error': 'No data provided'}), 400
    name = data.get('name', '').strip()
    email = data.get('email', '').strip()
    subject = data.get('subject', '').strip() or 'Pesan dari Form Kontak'
    message = data.get('message', '').strip()
    if not all([name, email, message]):
        return jsonify({'success': False, 'error': 'Nama, email, dan pesan wajib diisi'}), 400
    if not re.match(r'^[a-zA-Z0-9._%+-]+@[a-zA-Z0-9.-]+\.[a-zA-Z]{2,}$', email):
        return jsonify({'success': False, 'error': 'Format email tidak valid'}), 400

    try:
        tg_msg = (
            f"📬 <b>Pesan Kontak Baru</b>\n\n"
            f"<b>Dari:</b> {name}\n"
            f"<b>Email:</b> {email}\n"
            f"<b>Subjek:</b> {subject}\n\n"
            f"<b>Pesan:</b>\n{message[:500]}{'...' if len(message) > 500 else ''}"
        )
        NotificationService.send_telegram(tg_msg)
        return jsonify({
            'success': True,
            'message': 'Pesan Anda telah terkirim! Kami akan segera menghubungi Anda.'
        })
    except Exception as e:
        logger.error(f"Contact form error: {e}")
        return jsonify({'success': False, 'error': 'Terjadi kesalahan saat mengirim pesan'}), 500

# =============================================================================
# USER PROFILE & ORDERS
# =============================================================================
@app.route('/user/profile')
@login_required
def user_profile():
    if not isinstance(current_user, User):
        flash('Akses hanya untuk pelanggan', 'error')
        return redirect(url_for('index'))
    orders = Order.query.filter_by(user_id=current_user.id).order_by(Order.created_at.desc()).all()

    # BUGFIX: profile.html menampilkan completed_orders_count & pending_orders_count
    # di kartu statistik, tapi kedua variabel itu sebelumnya tidak pernah dikirim ke
    # sini -- hanya tersedia lewat context processor inject_admin_stats() yang cuma
    # aktif untuk Admin, jadi di halaman profil customer selalu tampil 0.
    completed_orders_count = sum(1 for o in orders if o.status == 'Completed')
    pending_orders_count = sum(1 for o in orders if o.status not in ('Completed', 'Cancelled', 'Returned'))

    bank_accounts_json = get_setting('bank_accounts_json', '[]')
    # Surface an existing pending_review KYC session so the profile page can show
    # "Menunggu Review" instead of just "Belum Terverifikasi" — the KYC page itself
    # tells the user to "Pantau Status di Profil Saya" after submitting, so the
    # profile page needs to actually reflect that in-between state.
    kyc_pending_session = KYCVerificationSession.query.filter_by(
        user_id=current_user.id, status='pending_review'
    ).order_by(KYCVerificationSession.id.desc()).first()
    return render_template('profile.html', user=current_user, orders=orders,
                           bank_accounts_json=bank_accounts_json,
                           completed_orders_count=completed_orders_count,
                           pending_orders_count=pending_orders_count,
                           kyc_pending=kyc_pending_session)

@app.route('/static/uploads/avatars/<filename>')
def uploaded_avatar_file(filename):
    return send_from_directory(UPLOAD_AVATAR, filename)

@app.route('/user/orders')
@login_required
def user_orders():
    if not isinstance(current_user, User):
        flash('Akses hanya untuk pelanggan', 'error')
        return redirect(url_for('index'))
    orders = Order.query.filter_by(user_id=current_user.id).order_by(Order.created_at.desc()).all()
    bank_accounts_json = get_setting('bank_accounts_json', '[]')
    availability = get_payment_availability()
    payment_options = [
        {'key': k, 'label': v['label'], 'icon': v['icon'], 'desc': v['desc']}
        for k, v in CHANGEABLE_PAYMENT_METHODS.items() if availability.get(k)
    ]
    return render_template('orders.html', orders=orders, format_price=format_price,
                           bank_accounts_json=bank_accounts_json,
                           payment_options=payment_options)


# ─────────────────────────────────────────────────────────────────────────────
# Pilih / ubah metode pembayaran pada pesanan yang masih Pending (halaman orders)
# ─────────────────────────────────────────────────────────────────────────────
# Label HARUS sama dengan payment_labels di route checkout, karena orders.html
# membaca metode dari teks label ini.
CHANGEABLE_PAYMENT_METHODS = {
    'transfer': {'label': 'Transfer Bank', 'icon': 'bank',
                 'desc': 'Transfer manual, lalu upload bukti pembayaran'},
    'qris': {'label': 'QRIS (GoPay, OVO, DANA, ShopeePay)', 'icon': 'qr-code',
             'desc': 'Scan QRIS toko, lalu upload bukti pembayaran'},
    'notivgo': {'label': 'QRIS Otomatis / NotivGo', 'icon': 'qr-code-scan',
                'desc': 'Dikonfirmasi otomatis setelah pembayaran diterima'},
}


def get_payment_availability():
    """Metode pembayaran yang sedang aktif (aturan sama dengan route checkout)."""
    def _on(key, default):
        return str(get_setting(key, default)).lower() in ('1', 'true', 'yes')

    qris_file = (get_setting('qris_code', '') or '').strip()
    qris_ready = bool(qris_file) and os.path.exists(os.path.join(UPLOAD_LOGO, qris_file))
    notivgo_key = (get_setting('notivgo_api_key', '') or os.getenv('NOTIVGO_API_KEY') or '').strip()
    return {
        'transfer': _on('payment_bank_transfer_enabled', '1'),
        'qris': _on('payment_qris_enabled', '1') and qris_ready,
        'notivgo': bool(notivgo_key) and _on('payment_notivgo_enabled', '1'),
    }


def _payment_key_of(order):
    pm = (order.payment_method or '').lower()
    if order.payment_provider == 'notivgo' or 'notivgo' in pm:
        return 'notivgo'
    if 'qris' in pm:
        return 'qris'
    if 'transfer' in pm or 'bank' in pm:
        return 'transfer'
    return 'other'   # COD / kredit / lainnya


@app.route('/api/order/<int:order_id>/change-payment', methods=['POST'])
@login_required
@csrf_protect_endpoint
def change_order_payment(order_id):
    if not isinstance(current_user, User):
        return jsonify({'success': False, 'message': 'Akses hanya untuk pelanggan'}), 403
    order = Order.query.get_or_404(order_id)
    if order.user_id != current_user.id:
        return jsonify({'success': False, 'message': 'Bukan pesanan Anda'}), 403

    data = request.get_json(silent=True) or {}
    method = str(data.get('method', '')).strip().lower()
    target = CHANGEABLE_PAYMENT_METHODS.get(method)
    if not target:
        return jsonify({'success': False, 'message': 'Metode pembayaran tidak dikenal'}), 400
    if order.status != 'Pending':
        return jsonify({'success': False, 'message': 'Metode hanya bisa diubah saat pesanan menunggu pembayaran'}), 400
    if order.is_credit_order:
        return jsonify({'success': False, 'message': 'Pesanan kredit/angsuran tidak dapat diubah metodenya'}), 400
    if order.payment_proof:
        return jsonify({'success': False,
                        'message': 'Bukti pembayaran sudah diunggah dan sedang diverifikasi. Hubungi admin untuk mengubah metode.'}), 409
    if not get_payment_availability().get(method):
        return jsonify({'success': False, 'message': 'Metode ini sedang tidak tersedia. Pilih metode lain.'}), 400

    def _pay_url_for(o):
        return url_for('notivgo_pay_page', order_id=o.id) if method == 'notivgo' and NOTIVGO else None

    # Tidak ada perubahan
    if _payment_key_of(order) == method:
        return jsonify({'success': True, 'changed': False,
                        'message': 'Metode pembayaran tidak berubah',
                        'pay_url': order.payment_url or _pay_url_for(order)})

    # Pindah DARI NotivGo: pastikan belum dibayar, lalu batalkan QRIS lama di NotivGo
    # supaya tidak ada pembayaran "nyasar" (webhook hanya mencocokkan order ber-provider notivgo).
    if order.payment_provider == 'notivgo':
        if not NOTIVGO:
            return jsonify({'success': False, 'message': 'Layanan NotivGo tidak tersedia. Coba lagi nanti.'}), 503
        try:
            remote = NOTIVGO.sync_order(order, 'change_payment')   # bisa menandai Paid
            db.session.refresh(order)
        except Exception as e:
            db.session.rollback()
            app.logger.error(f'change_payment: sync NotivGo gagal order {order.id}: {e}')
            return jsonify({'success': False, 'message': 'Tidak dapat memeriksa status pembayaran. Coba lagi.'}), 503
        if order.status != 'Pending':
            return jsonify({'success': False, 'message': 'Pembayaran untuk pesanan ini sudah diterima.'}), 409
        if remote is None:
            return jsonify({'success': False,
                            'message': 'Tidak dapat memastikan status QRIS sebelumnya. Coba lagi sesaat lagi.'}), 503
        if remote == 'needs_review':
            return jsonify({'success': False,
                            'message': 'Pembayaran QRIS terdeteksi dan sedang ditinjau admin. Hubungi admin.'}), 409
        if remote not in ('cancelled', 'canceled', 'expired'):
            try:
                cancelled = NOTIVGO.cancel_remote(order)
            except Exception as e:
                app.logger.error(f'change_payment: cancel NotivGo gagal order {order.id}: {e}')
                cancelled = False
            if not cancelled:
                return jsonify({'success': False,
                                'message': 'Gagal membatalkan QRIS sebelumnya. Metode belum diubah, coba lagi.'}), 502

    # Update atomik: hanya jika masih Pending (mencegah balapan dengan webhook/konfirmasi admin)
    try:
        n = (Order.query.filter_by(id=order.id, status='Pending')
             .update({'payment_method': target['label'],
                      'payment_provider': None,
                      'payment_ref': None,
                      'payment_url': None,
                      'payment_unique_code': None,
                      'updated_at': datetime.utcnow()}))
        db.session.commit()
    except Exception as e:
        db.session.rollback()
        app.logger.error(f'change_payment: gagal update order {order.id}: {e}')
        return jsonify({'success': False, 'message': 'Gagal mengubah metode pembayaran'}), 500
    if not n:
        return jsonify({'success': False, 'message': 'Status pesanan sudah berubah. Muat ulang halaman.'}), 409

    db.session.refresh(order)
    app.logger.info(f'Order {order.order_number}: metode pembayaran diubah ke {target["label"]} oleh user {current_user.id}')
    return jsonify({'success': True, 'changed': True,
                    'message': f'Metode pembayaran diubah ke {target["label"]}',
                    'pay_url': _pay_url_for(order)})

@app.route('/update_profile', methods=['POST'])
@login_required
@csrf_protect_endpoint
def update_profile():
    if not isinstance(current_user, User):
        return jsonify({'success': False, 'message': 'Akses hanya untuk pelanggan'}), 403
    if request.is_json:
        data = request.get_json()
        full_name = data.get('fullname')
        phone = data.get('phone')
        address = data.get('address')
        current_password = data.get('current_password')
        new_password = data.get('new_password')
        default_province = data.get('default_province')
        default_city = data.get('default_city')
        default_district = data.get('default_district')
        default_postal_code = data.get('default_postal_code')
    else:
        full_name = request.form.get('fullname')
        phone = request.form.get('phone')
        address = request.form.get('address')
        current_password = request.form.get('current_password')
        new_password = request.form.get('new_password')
        default_province = request.form.get('default_province')
        default_city = request.form.get('default_city')
        default_district = request.form.get('default_district')
        default_postal_code = request.form.get('default_postal_code')
    avatar_file = request.files.get('avatar')
    if full_name:
        current_user.full_name = full_name
    if phone:
        phone = phone.strip()
        if phone and phone != current_user.phone:
            existing_phone = User.query.filter(User.phone == phone, User.id != current_user.id).first()
            if existing_phone:
                return jsonify({'success': False, 'message': 'Nomor telepon sudah dipakai akun lain'}), 400
        current_user.phone = phone
    if address is not None:
        current_user.address = address
    if default_province is not None:
        current_user.default_province = default_province
    if default_city is not None:
        current_user.default_city = default_city
    if default_district is not None:
        current_user.default_district = default_district
    if default_postal_code is not None:
        current_user.default_postal_code = default_postal_code
    if new_password:
        if not current_password:
            return jsonify({'success': False, 'message': 'Kata sandi saat ini wajib diisi'}), 400
        if not current_user.check_password(current_password):
            return jsonify({'success': False, 'message': 'Kata sandi saat ini salah'}), 400
        if len(new_password) < 8:
            return jsonify({'success': False, 'message': 'Minimal 8 karakter'}), 400
        current_user.set_password(new_password)
    avatar_url = None
    if avatar_file and avatar_file.filename:
        filename = save_uploaded_image(avatar_file, prefix='avatar_', subfolder='avatars')
        if not filename:
            return jsonify({'success': False, 'message': 'Format avatar tidak didukung'}), 400
        old_avatar = current_user.avatar_filename
        current_user.avatar_filename = filename
        avatar_url = url_for('uploaded_avatar_file', filename=filename)
        if old_avatar:
            old_path = os.path.join(UPLOAD_AVATAR, old_avatar)
            if os.path.exists(old_path):
                try:
                    os.remove(old_path)
                except OSError:
                    logger.warning("Gagal menghapus avatar lama: %s", old_path)
    try:
        db.session.commit()
    except IntegrityError:
        db.session.rollback()
        return jsonify({'success': False, 'message': 'Terjadi konflik data saat menyimpan profil'}), 400
    response = {'success': True, 'message': 'Profil berhasil diperbarui'}
    if avatar_url:
        response['avatar_url'] = avatar_url
    return jsonify(response)

# =============================================================================
# ADDRESS MANAGEMENT API
# =============================================================================
@app.route('/api/user/addresses', methods=['GET'])
@login_required
def get_user_addresses():
    if not isinstance(current_user, User):
        return jsonify({'success': False, 'message': 'Akses hanya untuk pelanggan'}), 403
    addresses = Address.query.filter_by(user_id=current_user.id).order_by(
        Address.is_default.desc(), Address.created_at.desc()
    ).all()
    return jsonify({'success': True, 'addresses': [addr.to_dict() for addr in addresses]})

@app.route('/api/user/addresses', methods=['POST'])
@login_required
@csrf_protect_endpoint
def create_user_address():
    if not isinstance(current_user, User):
        return jsonify({'success': False, 'message': 'Akses hanya untuk pelanggan'}), 403
    try:
        data = request.get_json()
        required_fields = ['label', 'recipient_name', 'recipient_phone', 'full_address',
                          'province', 'city', 'district', 'postal_code']
        for field in required_fields:
            if not data.get(field):
                return jsonify({'success': False, 'message': f'Field {field} wajib diisi'}), 400
        is_default = data.get('is_default', False)
        if is_default:
            Address.query.filter_by(user_id=current_user.id).update({'is_default': False})
        new_address = Address(
            user_id=current_user.id,
            label=data.get('label').strip(),
            recipient_name=data.get('recipient_name').strip(),
            recipient_phone=data.get('recipient_phone').strip(),
            full_address=data.get('full_address').strip(),
            province=data.get('province').strip(),
            city=data.get('city').strip(),
            district=data.get('district').strip(),
            postal_code=data.get('postal_code').strip(),
            is_default=is_default
        )
        db.session.add(new_address)
        db.session.commit()
        return jsonify({
            'success': True, 'message': 'Alamat berhasil disimpan',
            'address': new_address.to_dict()
        }), 201
    except Exception as e:
        db.session.rollback()
        logger.error(f"Error creating address: {e}")
        return jsonify({'success': False, 'message': 'Terjadi kesalahan saat menyimpan alamat'}), 500

@app.route('/api/user/addresses/<int:address_id>', methods=['PUT'])
@login_required
@csrf_protect_endpoint
def update_user_address(address_id):
    if not isinstance(current_user, User):
        return jsonify({'success': False, 'message': 'Akses hanya untuk pelanggan'}), 403
    address = Address.query.filter_by(id=address_id, user_id=current_user.id).first()
    if not address:
        return jsonify({'success': False, 'message': 'Alamat tidak ditemukan'}), 404
    try:
        data = request.get_json()
        required_fields = ['label', 'recipient_name', 'recipient_phone', 'full_address',
                          'province', 'city', 'district', 'postal_code']
        for field in required_fields:
            if not data.get(field):
                return jsonify({'success': False, 'message': f'Field {field} wajib diisi'}), 400
        is_default = data.get('is_default', False)
        if is_default and not address.is_default:
            Address.query.filter_by(user_id=current_user.id).update({'is_default': False})
        address.label = data.get('label').strip()
        address.recipient_name = data.get('recipient_name').strip()
        address.recipient_phone = data.get('recipient_phone').strip()
        address.full_address = data.get('full_address').strip()
        address.province = data.get('province').strip()
        address.city = data.get('city').strip()
        address.district = data.get('district').strip()
        address.postal_code = data.get('postal_code').strip()
        address.is_default = is_default
        db.session.commit()
        return jsonify({
            'success': True, 'message': 'Alamat berhasil diperbarui',
            'address': address.to_dict()
        })
    except Exception as e:
        db.session.rollback()
        logger.error(f"Error updating address: {e}")
        return jsonify({'success': False, 'message': 'Terjadi kesalahan saat memperbarui alamat'}), 500

@app.route('/api/user/addresses/<int:address_id>', methods=['DELETE'])
@login_required
@csrf_protect_endpoint
def delete_user_address(address_id):
    if not isinstance(current_user, User):
        return jsonify({'success': False, 'message': 'Akses hanya untuk pelanggan'}), 403
    address = Address.query.filter_by(id=address_id, user_id=current_user.id).first()
    if not address:
        return jsonify({'success': False, 'message': 'Alamat tidak ditemukan'}), 404
    try:
        if address.is_default:
            next_address = Address.query.filter_by(user_id=current_user.id).filter(
                Address.id != address_id
            ).first()
            if next_address:
                next_address.is_default = True
        db.session.delete(address)
        db.session.commit()
        return jsonify({'success': True, 'message': 'Alamat berhasil dihapus'})
    except Exception as e:
        db.session.rollback()
        logger.error(f"Error deleting address: {e}")
        return jsonify({'success': False, 'message': 'Terjadi kesalahan saat menghapus alamat'}), 500

@app.route('/api/user/addresses/<int:address_id>/set-default', methods=['POST'])
@login_required
@csrf_protect_endpoint
def set_default_address(address_id):
    if not isinstance(current_user, User):
        return jsonify({'success': False, 'message': 'Akses hanya untuk pelanggan'}), 403
    address = Address.query.filter_by(id=address_id, user_id=current_user.id).first()
    if not address:
        return jsonify({'success': False, 'message': 'Alamat tidak ditemukan'}), 404
    try:
        Address.query.filter_by(user_id=current_user.id).update({'is_default': False})
        address.is_default = True
        db.session.commit()
        return jsonify({'success': True, 'message': 'Alamat utama diperbarui'})
    except Exception as e:
        db.session.rollback()
        logger.error(f"Error setting default address: {e}")
        return jsonify({'success': False, 'message': 'Terjadi kesalahan'}), 500

@app.route('/user/logout')
@login_required
def user_logout_page():
    if isinstance(current_user, User):
        logout_user()
    flash('Anda telah keluar dari akun pelanggan', 'success')
    return redirect(url_for('index'))

# =============================================================================
# CART & CHECKOUT
# =============================================================================
@app.route('/cart')
def cart():
    cart_items = session.get('cart', [])
    # Prices in cart are already in rupiah, sum directly
    total = sum(item['price'] * item['quantity'] for item in cart_items)
    total_savings = sum(
        (item.get('compare_at_price', 0) - item.get('price', 0)) * item.get('quantity', 1)
        for item in cart_items
        if item.get('compare_at_price') and item.get('compare_at_price', 0) > item.get('price', 0)
    )
    return render_template('cart.html', cart_items=cart_items, total=total, total_savings=total_savings)

@app.route('/add_to_cart/<int:product_id>', methods=['POST'])
def add_to_cart(product_id):
    product = Product.query.get_or_404(product_id)
    cart = session.get('cart', [])
    for item in cart:
        if item['product_id'] == product_id:
            item['quantity'] += 1
            break
    else:
        cart.append(_build_cart_item(product))
    session['cart'] = cart
    flash('Produk ditambahkan ke keranjang', 'success')
    return redirect(request.referrer or url_for('cart'))

@app.route('/api/add_to_cart', methods=['POST'])
@csrf_protect_endpoint
def api_add_to_cart():
    if request.is_json:
        data = request.get_json() or {}
        product_id = data.get('product_id')
        quantity = data.get('quantity', 1)
        mode = data.get('mode', 'add')
    else:
        product_id = request.form.get('product_id')
        quantity = request.form.get('quantity', 1, type=int)
        mode = request.form.get('mode', 'add')
    if not product_id:
        return jsonify({'success': False, 'message': 'ID produk tidak ditemukan'}), 400
    product = Product.query.get(int(product_id))
    if not product:
        return jsonify({'success': False, 'message': 'Produk tidak ditemukan'}), 404
    cart = session.get('cart', [])
    found = False
    for item in cart:
        if item['product_id'] == int(product_id):
            # mode='set' dipakai oleh tombol "Checkout Sekarang": jumlah yang
            # dipilih pembeli harus jadi jumlah final, bukan ditambahkan ke atas
            # quantity yang sudah ada di keranjang dari sebelumnya (itu yang
            # menyebabkan pesanan jadi 2 padahal pembeli hanya pilih 1).
            if mode == 'set':
                item['quantity'] = int(quantity)
            else:
                item['quantity'] += int(quantity)
            found = True
            break
    if not found:
        new_item = _build_cart_item(product)
        new_item['quantity'] = int(quantity)
        cart.append(new_item)
    session['cart'] = cart
    cart_count = sum(i['quantity'] for i in cart)
    return jsonify({'success': True, 'cart_count': cart_count})

@app.route('/api/cart_count')
def api_cart_count():
    cart = session.get('cart', [])
    return jsonify({'success': True, 'cart_count': sum(i['quantity'] for i in cart)})

@app.route('/api/wishlist/add', methods=['POST'])
def api_wishlist_add():
    """Toggle item in wishlist (stored in session)."""
    data = request.get_json(silent=True) or {}
    product_id = data.get('product_id')
    if not product_id:
        return jsonify({'success': False, 'message': 'ID produk tidak valid'}), 400

    product = Product.query.get(product_id)
    if not product:
        return jsonify({'success': False, 'message': 'Produk tidak ditemukan'}), 404

    wishlist = session.get('wishlist', [])
    if product_id in wishlist:
        wishlist.remove(product_id)
        session['wishlist'] = wishlist
        session.modified = True
        return jsonify({'success': True, 'action': 'removed', 'message': f'{product.name} dihapus dari wishlist', 'wishlist_count': len(wishlist)})
    else:
        wishlist.append(product_id)
        session['wishlist'] = wishlist
        session.modified = True
        return jsonify({'success': True, 'action': 'added', 'message': f'{product.name} ditambahkan ke wishlist!', 'wishlist_count': len(wishlist)})

@app.route('/api/wishlist_count')
def api_wishlist_count():
    wishlist = session.get('wishlist', [])
    return jsonify({'success': True, 'wishlist_count': len(wishlist)})

@app.route('/cart/update/<int:product_id>', methods=['POST'])
def update_cart(product_id):
    cart = session.get('cart', [])
    if request.is_json:
        payload = request.get_json(silent=True) or {}
        quantity = max(1, _safe_int(payload.get('quantity'), 1))
    else:
        quantity = max(1, _safe_int(request.form.get('quantity'), 1))
    for item in cart:
        if item['product_id'] == product_id:
            item['quantity'] = quantity
            break
    session['cart'] = cart
    if request.is_json:
        total = sum(i['price'] * i['quantity'] for i in cart)
        item_subtotal = next((i['price'] * i['quantity'] for i in cart if i['product_id'] == product_id), 0)
        total_savings = sum(
            (i.get('compare_at_price', 0) - i.get('price', 0)) * i.get('quantity', 1)
            for i in cart
            if i.get('compare_at_price') and i.get('compare_at_price', 0) > i.get('price', 0)
        )
        return jsonify({
            'success': True,
            'item_subtotal': item_subtotal,
            'item_subtotal_formatted': format_price(item_subtotal * 100),  # format_price expects cents
            'cart_total': total,
            'cart_total_formatted': format_price(total * 100),
            'total_savings': total_savings,
            'total_savings_formatted': format_price(total_savings * 100)
        })
    flash('Jumlah diperbarui', 'success')
    return redirect(url_for('cart'))

@app.route('/cart/remove/<int:product_id>', methods=['POST'])
def remove_from_cart(product_id):
    cart = [i for i in session.get('cart', []) if i['product_id'] != product_id]
    session['cart'] = cart
    if request.is_json:
        total = sum(i['price'] * i['quantity'] for i in cart)
        total_savings = sum(
            (i.get('compare_at_price', 0) - i.get('price', 0)) * i.get('quantity', 1)
            for i in cart
            if i.get('compare_at_price') and i.get('compare_at_price', 0) > i.get('price', 0)
        )
        return jsonify({
            'success': True,
            'cart_total': total,
            'cart_total_formatted': format_price(total * 100),
            'total_savings': total_savings,
            'total_savings_formatted': format_price(total_savings * 100)
        })
    flash('Item dihapus', 'success')
    return redirect(url_for('cart'))

@app.route('/cart/clear', methods=['POST'])
def clear_cart():
    session.pop('cart', None)
    if request.is_json:
        return jsonify({'success': True, 'message': 'Keranjang dikosongkan'})
    flash('Keranjang dikosongkan', 'success')
    return redirect(url_for('cart'))

# =============================================================================
# CHECKOUT — FULLY INTEGRATED WITH KYC & CREDIT SYSTEM
# =============================================================================
@app.route('/checkout', methods=['GET', 'POST'])
@csrf_protect_endpoint
def checkout():
    cart_items = session.get('cart', [])
    if not cart_items:
        flash('Keranjang belanja kosong', 'warning')
        return redirect(url_for('catalog'))

    session['cart'] = cart_items
    subtotal = sum(item['price'] * item['quantity'] for item in cart_items)
    discount = 0
    total = subtotal - discount

    # Shipping settings
    shipping_enabled = get_setting('shipping_enabled', 'true') == 'true'
    shipping_couriers = [c.strip() for c in get_setting('shipping_couriers', 'JNE,J&T,SiCepat').split(',') if c.strip()]
    shipping_provinces = [p.strip() for p in get_setting('shipping_provinces', '').split(',') if p.strip()]

    free_shipping_enabled = get_setting('free_shipping_enabled', 'false') == 'true'
    fs_threshold = _safe_int(get_setting('shipping_free_threshold', '0')) or _safe_int(get_setting('free_shipping_min', '0'))

    shipping_free_threshold = fs_threshold if free_shipping_enabled else 0
    packaging_cost = _safe_int(get_setting('packaging_cost', '0'))

    # Payment settings - dukung nilai '1', 'true', 'yes' dari DB maupun form admin
    payment_bank_enabled = str(get_setting('payment_bank_transfer_enabled', '1')).lower() in ('1', 'true', 'yes')
    # QRIS MANUAL (GoPay, OVO, DANA, ShopeePay): berdiri sendiri, TIDAK
    # bergantung pada gateway otomatis. Aktif jika toggle dinyalakan DAN admin sudah
    # mengunggah gambar QR (qris_code) yang filenya benar-benar ada di server.
    qris_code_file = (get_setting('qris_code', '') or '').strip()
    payment_qris_image_url = ''
    if qris_code_file and os.path.exists(os.path.join(UPLOAD_LOGO, qris_code_file)):
        payment_qris_image_url = url_for('uploaded_logo_file', filename=qris_code_file)
    payment_qris_enabled = (
        str(get_setting('payment_qris_enabled', '1')).lower() in ('1', 'true', 'yes')
        and bool(payment_qris_image_url)
    )
    payment_cod_enabled = str(get_setting('payment_cod_enabled', '0')).lower() in ('1', 'true', 'yes')
    # NotivGo (QRIS otomatis): independen dari QRIS manual; cukup API Key + toggle sendiri.
    notivgo_key = (get_setting('notivgo_api_key', '') or os.getenv('NOTIVGO_API_KEY') or '').strip()
    payment_notivgo_enabled = (
        bool(notivgo_key)
        and str(get_setting('payment_notivgo_enabled', '1')).lower() in ('1', 'true', 'yes')
    )
    cod_fee = _safe_int(get_setting('cod_fee', '0'))
    cod_max_order = _safe_int(get_setting('cod_max_order', '0'))
    cod_areas = get_setting('cod_areas', '')

    # Build active couriers detail
    active_couriers_detail = []
    for courier_key in shipping_couriers:
        key_lower = courier_key.lower().replace('&', '').replace(' ', '').replace('.', '')
        courier_active_val = get_setting(f'courier_{key_lower}_active', '0')
        if courier_active_val in ('0', '', None, 'false'):
            continue
        all_courier_services = {
            'fedex': {'name': 'FEDEX', 'key': 'fedex', 'services': [
                {'key': 'reg', 'label': 'REG (2–3 hari)'},
                {'key': 'yes', 'label': 'YES – Yakin Esok Sampai (1 hari)'},
                {'key': 'oke', 'label': 'OKE (4–6 hari)'},
                {'key': 'jnd', 'label': 'JND – J&D Express (1–2 hari)'},
            ]},
            'jnt': {'name': 'J&T Express', 'key': 'jnt', 'services': [
                {'key': 'reg', 'label': 'Regular (2–3 hari)'},
                {'key': 'ez', 'label': 'EZ (1–2 hari)'},
                {'key': 'eco', 'label': 'Economy (4–7 hari)'},
            ]},
            'sicepat': {'name': 'SiCepat', 'key': 'sicepat', 'services': [
                {'key': 'reg', 'label': 'HALU – Regular (2–3 hari)'},
                {'key': 'best', 'label': 'BEST – Best Service (1–2 hari)'},
            ]},
            'anteraja': {'name': 'AnterAja', 'key': 'anteraja', 'services': [
                {'key': 'reg', 'label': 'Regular (2–4 hari)'},
                {'key': 'ndd', 'label': 'Next Day Delivery (1 hari)'},
                {'key': 'sdd', 'label': 'Same Day Delivery (hari ini)'},
            ]},
            'pos': {'name': 'Pos Indonesia', 'key': 'pos', 'services': [
                {'key': 'biasa', 'label': 'Pos Biasa (5–7 hari)'},
                {'key': 'kilat', 'label': 'Pos Kilat Khusus (2–3 hari)'},
                {'key': 'ekspres', 'label': 'Pos Ekspres (1 hari)'},
            ]},
            'tiki': {'name': 'TIKI', 'key': 'tiki', 'services': [
                {'key': 'reg', 'label': 'Regular (3–5 hari)'},
                {'key': 'ons', 'label': 'ONS – Over Night Service (1 hari)'},
                {'key': 'eco', 'label': 'Economy (5–7 hari)'},
            ]},
        }
        if key_lower not in all_courier_services:
            continue
        base = all_courier_services[key_lower]
        active_services = []
        for svc in base['services']:
            svc_active_val = get_setting(f'svc_{key_lower}_{svc["key"]}_active', '0')
            if svc_active_val in ('0', '', None, 'false'):
                continue
            cost = _safe_int(get_setting(f'svc_{key_lower}_{svc["key"]}_cost', '0'))
            active_services.append({'key': svc['key'], 'label': svc['label'], 'cost': cost})
        if active_services:
            active_couriers_detail.append({
                'name': base['name'], 'key': base['key'], 'services': active_services,
            })

    if request.method == 'POST':
        if not current_user.is_authenticated or not isinstance(current_user, User):
            flash('Silakan login atau daftar akun terlebih dahulu untuk melanjutkan checkout.', 'warning')
            return redirect(url_for('checkout'))

        # FIX: alamat tujuan pengiriman sebelumnya hanya diambil dari form
        # checkout (shipping_province/city/district/postal_code) TANPA fallback
        # sama sekali. Jika field-field ini kosong (mis. JS di checkout.html
        # tidak mengirim nilainya, atau user belum sempat memilih lagi),
        # Order tersimpan dengan shipping_province/city/district/postal_code
        # kosong padahal user sudah punya alamat tersimpan (baik alamat utama
        # di tab "Alamat" / current_user.address, default_* dari checkout
        # sebelumnya, maupun alamat default di tab "Alamat Tersimpan" /
        # Address.is_default). Akibatnya alamat tujuan pengiriman di
        # orders.html (shipping_address/shipping_province/dst.) tidak sesuai
        # dengan alamat yang sebenarnya dipunya/diatur user.
        #
        # Sekarang fallback-nya berurutan dari yang paling spesifik ke paling
        # umum: (1) input form checkout saat ini, (2) alamat utama tersimpan
        # user (Address.is_default), (3) default_* / address di profil User.
        default_saved_address = Address.query.filter_by(
            user_id=current_user.id, is_default=True
        ).first()

        name = request.form.get('name', '').strip() or current_user.full_name
        phone = request.form.get('phone', '').strip() or current_user.phone
        email = request.form.get('email', '').strip() or current_user.email
        address = (
            request.form.get('address', '').strip()
            or (default_saved_address.full_address if default_saved_address else None)
            or current_user.address
        )
        payment_method = request.form.get('payment_method', 'transfer')
        notes = request.form.get('notes', '').strip()

        ship_recipient = (
            request.form.get('shipping_recipient_name', '').strip()
            or (default_saved_address.recipient_name if default_saved_address else None)
            or name
        )
        ship_phone = (
            request.form.get('shipping_phone', '').strip()
            or (default_saved_address.recipient_phone if default_saved_address else None)
            or phone
        )
        ship_province = (
            request.form.get('shipping_province', '').strip()
            or (default_saved_address.province if default_saved_address else None)
            or current_user.default_province
            or ''
        )
        ship_city = (
            request.form.get('shipping_city', '').strip()
            or (default_saved_address.city if default_saved_address else None)
            or current_user.default_city
            or ''
        )
        ship_district = (
            request.form.get('shipping_district', '').strip()
            or (default_saved_address.district if default_saved_address else None)
            or current_user.default_district
            or ''
        )
        ship_postal = (
            request.form.get('shipping_postal_code', '').strip()
            or (default_saved_address.postal_code if default_saved_address else None)
            or current_user.default_postal_code
            or ''
        )
        ship_courier = request.form.get('shipping_courier', '').strip()
        ship_service = request.form.get('shipping_service', '').strip()
        ship_notes = request.form.get('shipping_notes', '').strip()

        if shipping_enabled and ship_province:
            addr_parts = [p for p in [address, ship_district, ship_city, ship_province, ship_postal] if p]
            full_address = ', '.join(addr_parts)
        else:
            full_address = address

        if not (name and phone and (address or full_address)):
            flash('Harap isi semua field wajib', 'error')
            return redirect(url_for('checkout'))

        payment_labels = {
            'transfer': 'Transfer Bank',
            'credit': 'Kredit/Angsuran',
            'qris': 'QRIS (GoPay, OVO, DANA, ShopeePay)',
            'notivgo': 'QRIS Otomatis / NotivGo',
            'cod': 'Bayar di Tempat (COD)',
        }
        payment_label = payment_labels.get(payment_method, 'Transfer Bank')

        # Validasi server-side: QRIS manual hanya boleh dipilih bila aktif & QR sudah diunggah
        if payment_method == 'qris' and not payment_qris_enabled:
            flash('Pembayaran QRIS belum tersedia. Silakan pilih metode pembayaran lain.', 'error')
            return redirect(url_for('checkout'))
        if payment_method == 'notivgo' and not payment_notivgo_enabled:
            flash('Pembayaran QRIS otomatis belum tersedia. Silakan pilih metode pembayaran lain.', 'error')
            return redirect(url_for('checkout'))

        items_data = []
        db_subtotal = 0
        for item in cart_items:
            product = Product.query.get(item.get('product_id'))
            if product:
                item_subtotal = product.price * item['quantity']
                db_subtotal += item_subtotal
                items_data.append({'product': product, 'quantity': item['quantity'], 'price': product.price})

        if not items_data:
            flash('Produk di keranjang Anda sudah tidak tersedia. Silakan periksa kembali keranjang Anda.', 'error')
            return redirect(url_for('cart'))

        # FIX: stok sebelumnya tidak pernah divalidasi maupun dikurangi saat
        # checkout, jadi pelanggan bisa memesan lebih banyak dari stok yang
        # tersedia dan stok tidak pernah berkurang otomatis setelah terjual.
        # Validasi awal ini memberi pesan yang jelas sebelum proses checkout
        # (termasuk pengajuan kredit) berjalan. product.stock == None berarti
        # stok tidak dilacak untuk produk ini (selalu tersedia), jadi dilewati.
        for d in items_data:
            p = d['product']
            if p.stock is not None and d['quantity'] > p.stock:
                flash(f'Stok "{p.name}" tidak mencukupi (tersisa {p.stock}). Silakan sesuaikan jumlah di keranjang.', 'error')
                return redirect(url_for('cart'))

        # Hitung shipping_cost dari kurir+layanan spesifik yang dipilih user
        # (format nilai radio: "{courier_key}_{service_key}", mis. "jnt_reg"),
        # bukan dari shipping_default_cost generik. Sebelumnya field ini
        # diabaikan sepenuhnya sehingga ongkir yang ditagihkan ke Order tidak
        # pernah cocok dengan ongkir per-kurir/layanan yang ditampilkan ke
        # user di halaman checkout.
        shipping_cost = _safe_int(get_setting('shipping_default_cost', '0'))
        if ship_courier and '_' in ship_courier:
            courier_key, svc_key = ship_courier.split('_', 1)
            svc_active_val = get_setting(f'svc_{courier_key}_{svc_key}_active', '0')
            if svc_active_val not in ('0', '', 'false'):
                # Layanan ini memang aktif/dikonfigurasi admin -> pakai biaya
                # spesifiknya (sama seperti nilai yang ditampilkan ke user di
                # active_couriers_detail), bukan shipping_default_cost.
                shipping_cost = _safe_int(get_setting(f'svc_{courier_key}_{svc_key}_cost', '0'))
        if shipping_free_threshold > 0 and db_subtotal >= shipping_free_threshold:
            shipping_cost = 0

        # ═══════════════════════════════════════════════════════════════
        # CREDIT ORDER HANDLING — INTEGRATED WITH KYC
        # ══════════════════════════════���════════════════════════════════
        is_credit = payment_method == 'credit'
        credit_down_payment = 0
        credit_monthly = 0
        credit_tenor = 0
        credit_app_id = None
        credit_remaining = 0

        if is_credit:
            # Validate KYC requirement
            kyc_required = get_setting('credit_require_kyc', 'true') == 'true'
            if kyc_required and not current_user.kyc_verified:
                flash('Anda harus menyelesaikan verifikasi KYC terlebih dahulu untuk menggunakan pembayaran kredit.', 'warning')
                return redirect(url_for('kyc_verification_page', redirect=url_for('checkout')))

            # Get approved credit application
            credit_app = InstallmentApplication.query.filter_by(
                user_id=current_user.id,
                status='approved'
            ).order_by(InstallmentApplication.approved_at.desc()).first()

            if not credit_app:
                # Belum ada pengajuan kredit yang disetujui sebelumnya —
                # untuk user yang sudah verifikasi KYC, izinkan pilih paket
                # cicilan langsung di checkout. FIX: sebelumnya paket ini
                # dihitung dari CreditCalculator (tabel bunga hardcoded di
                # kode, tenor {3,6,12,24}) yang sama sekali lepas dari
                # InstallmentPlan yang admin atur di Pengaturan Cicilan —
                # jadi mengubah bunga/tenor/DP lewat admin tidak berpengaruh
                # ke checkout. Sekarang pilihan & perhitungan diambil dari
                # InstallmentPlan (plan.calculate_installment) yang sama
                # dipakai alur /installment/apply.
                plan_id_input = request.form.get('credit_plan_id', type=int)
                dp_amount_input = _safe_int(request.form.get('credit_dp_amount', 0))

                if not plan_id_input or not dp_amount_input:
                    flash('Anda tidak memiliki pengajuan kredit yang disetujui. Silakan pilih paket cicilan dan DP.', 'warning')
                    return redirect(url_for('checkout'))

                subtotal_rupiah = db_subtotal / 100
                available_plans = _get_checkout_credit_plans(subtotal_rupiah)
                plan = next((p for p in available_plans if p.id == plan_id_input), None)

                if not plan:
                    flash('Paket cicilan yang dipilih tidak tersedia untuk total belanja ini.', 'error')
                    return redirect(url_for('checkout'))

                min_dp = round(subtotal_rupiah * (plan.min_dp_percentage or 0))
                max_dp = round(subtotal_rupiah * (plan.max_dp_percentage or 1))

                if dp_amount_input < min_dp:
                    flash(f'Uang muka (DP) minimum untuk paket {plan.name} adalah {format_price(min_dp * 100)}', 'error')
                    return redirect(url_for('checkout'))

                if dp_amount_input > max_dp or dp_amount_input >= subtotal_rupiah:
                    flash(f'Uang muka (DP) maksimum untuk paket {plan.name} adalah {format_price(max_dp * 100)}', 'error')
                    return redirect(url_for('checkout'))

                if not (current_user.full_name and current_user.phone and (current_user.address or full_address)):
                    flash('Lengkapi profil Anda (nama, no. HP, alamat) sebelum menggunakan pembayaran kredit.', 'warning')
                    return redirect(url_for('checkout'))

                dp_pct = dp_amount_input / subtotal_rupiah
                loan_amount = subtotal_rupiah - dp_amount_input
                monthly_installment = plan.calculate_installment(subtotal_rupiah, dp_amount_input)
                total_payment = monthly_installment * plan.tenor_months
                total_interest = max(0, total_payment - loan_amount - (plan.processing_fee or 0))

                kyc_session_for_app = get_latest_verified_kyc_session(current_user.id)
                app_code = f"CKT-{datetime.now().strftime('%Y%m%d')}-{uuid.uuid4().hex[:4].upper()}"

                single_product_id = cart_items[0].get('product_id') if len(cart_items) == 1 else None

                credit_app = InstallmentApplication(
                    application_code=app_code,
                    user_id=current_user.id,
                    plan_id=plan.id,
                    product_id=single_product_id,
                    kyc_session_id=kyc_session_for_app.id if kyc_session_for_app else None,
                    full_name=current_user.full_name,
                    nik=current_user.kyc_nik or '',
                    phone=current_user.phone or '',
                    address_ktp=(current_user.address or full_address or ''),
                    # product_amount/dp_amount/loan_amount/total_loan adalah RUPIAH
                    # (konvensi yang sama dipakai /api/installment/apply).
                    product_amount=round(subtotal_rupiah),
                    dp_amount=dp_amount_input,
                    loan_amount=round(loan_amount),
                    interest_amount=round(total_interest),
                    processing_fee_amount=plan.processing_fee or 0,
                    total_loan=round(total_payment),
                    monthly_installment=monthly_installment,
                    product_price=int(db_subtotal),
                    down_payment=dp_amount_input,
                    down_payment_pct=dp_pct,
                    principal=round(loan_amount),
                    tenor_months=plan.tenor_months,
                    interest_type=plan.interest_type,
                    interest_rate_monthly=plan.interest_rate_monthly,
                    interest_rate_annual=round(plan.interest_rate_monthly * 12, 4),
                    total_interest=round(total_interest),
                    total_payment=round(total_payment),
                    ktp_name=current_user.full_name,
                    ktp_nik=current_user.kyc_nik,
                    ktp_address=current_user.address or full_address,
                    ktp_photo=current_user.kyc_ktp_photo,
                    selfie_photo=current_user.kyc_selfie_photo,
                    biometric_verified=True,
                    biometric_match_score=current_user.kyc_match_score,
                    biometric_liveness_passed=bool(current_user.kyc_liveness_passed),
                    # Sudah lolos KYC -> langsung disetujui, tidak perlu review manual lagi.
                    status='approved',
                    approved_at=datetime.utcnow(),
                    created_at=datetime.utcnow(),
                    ip_address=request.remote_addr,
                    user_agent=request.user_agent.string if request.user_agent else None
                )
                db.session.add(credit_app)
                db.session.flush()

                for sched in credit_app.generate_schedule():
                    db.session.add(sched)

            # FIX: credit_app.down_payment / monthly_installment / total_loan are
            # stored in RUPIAH (see CreditCalculator.calculate), but shipping_cost,
            # db_subtotal and every money column on Order (subtotal, shipping_cost,
            # total — see format_price(order.total) in orders.html) are in CENTS.
            # Previously these were combined directly (credit_down_payment +
            # shipping_cost), mixing units, so credit orders' grand_total / stored
            # Order fields came out wrong. Convert to cents here, once.
            credit_down_payment = int(round((credit_app.down_payment or credit_app.dp_amount or 0) * 100))
            credit_monthly = int(round((credit_app.monthly_installment or 0) * 100))
            credit_tenor = credit_app.tenor_months or (credit_app.plan.tenor_months if credit_app.plan else 0)
            credit_app_id = credit_app.id
            credit_remaining = int(round(
                ((credit_app.total_loan or 0) - (credit_app.down_payment or credit_app.dp_amount or 0)) * 100
            ))

            # Validate cart total matches credit product
            if len(cart_items) == 1:
                cart_product_id = cart_items[0].get('product_id')
                if credit_app.product_id and credit_app.product_id != cart_product_id:
                    flash('Pengajuan kredit Anda tidak sesuai dengan produk di keranjang.', 'error')
                    return redirect(url_for('checkout'))

            # Adjust total for credit: only pay DP at checkout
            total = credit_down_payment + shipping_cost

        grand_total = db_subtotal + shipping_cost - discount
        if is_credit:
            grand_total = credit_down_payment + shipping_cost

        if grand_total < 0:
            grand_total = 0

        try:
            order = Order(
                order_number=generate_order_number(),
                customer_name=name,
                customer_phone=phone,
                customer_email=email or None,
                shipping_address=full_address,
                payment_method=payment_label,
                status='Pending',
                subtotal=db_subtotal,
                shipping_cost=shipping_cost,
                discount=discount,
                total=grand_total,
                user_id=current_user.id,
                notes=notes or None,
                shipping_recipient_name=ship_recipient or None,
                shipping_phone=ship_phone or None,
                shipping_province=ship_province or None,
                shipping_city=ship_city or None,
                shipping_district=ship_district or None,
                shipping_postal_code=ship_postal or None,
                shipping_courier=ship_courier or None,
                shipping_service=ship_service or None,
                shipping_notes=ship_notes or None,
                is_credit_order=is_credit,
                credit_application_id=credit_app_id,
                credit_down_payment=credit_down_payment,
                credit_monthly_installment=credit_monthly,
                credit_tenor_months=credit_tenor,
                credit_remaining_balance=credit_remaining
            )
            db.session.add(order)
            db.session.flush()

            for d in items_data:
                product_obj = d['product']
                db.session.add(OrderItem(
                    order_id=order.id, product_id=product_obj.id, name=product_obj.name,
                    brand=product_obj.brand, model=product_obj.model,
                    condition=product_obj.condition, price=d['price'], quantity=d['quantity']
                ))
                if product_obj.stock is not None:
                    stock_result = db.session.execute(
                        Product.__table__.update()
                        .where(Product.id == product_obj.id, Product.stock >= d['quantity'])
                        .values(stock=Product.stock - d['quantity'])
                    )
                    if stock_result.rowcount == 0:
                        db.session.rollback()
                        flash(
                            f'Maaf, stok "{product_obj.name}" baru saja habis dibeli pembeli lain. '
                            'Silakan periksa kembali keranjang Anda.', 'error'
                        )
                        return redirect(url_for('cart'))

            if request.form.get('save_shipping_default') and shipping_enabled:
                if ship_province:
                    current_user.default_province = ship_province
                if ship_city:
                    current_user.default_city = ship_city
                if ship_district:
                    current_user.default_district = ship_district
                if ship_postal:
                    current_user.default_postal_code = ship_postal
                street_address = request.form.get('address', '').strip()
                if street_address:
                    current_user.address = street_address
                elif not current_user.address and full_address:
                    current_user.address = full_address

            db.session.commit()

            try:
                NotificationService.notify_new_order(order, items_data)
            except Exception:
                pass

            session.pop('cart', None)
            flash('Pesanan berhasil dibuat!' + (' Silakan lakukan pembayaran DP untuk mengaktifkan kredit.' if is_credit else ''), 'success')
            if payment_method == 'notivgo':
                return redirect(url_for('notivgo_pay_page', order_id=order.id))
            return redirect(url_for('user_orders', _anchor='', **{'from': 'checkout'}))

        except IntegrityError:
            db.session.rollback()
            flash('Terjadi kesalahan saat menyimpan pesanan. Silakan coba lagi.', 'error')
            return redirect(url_for('checkout'))
        except Exception as e:
            db.session.rollback()
            logging.error(f'Order creation error: {e}')
            flash('Terjadi kesalahan sistem saat membuat pesanan. Silakan hubungi admin.', 'error')
            return redirect(url_for('checkout'))

    # ═══════════════════════════════════════════════════════════════
    # GET REQUEST: Prepare checkout page with credit integration
    # ═══════════════════════════════════════════════════════════════
    credit_vars = _get_checkout_credit_vars(subtotal)
    bank_accounts_json = get_setting('bank_accounts_json', '[]')

    # INTEGRASI: installment_apply.html — link alternatif "isi di halaman penuh"
    # di bawah opsi Kredit/Angsuran, sama seperti di product.html. Hanya kirim
    # product_id kalau keranjang berisi 1 produk (installment_apply &
    # InstallmentApplication.product_id dirancang untuk satu produk per
    # pengajuan) — kalau lebih dari 1 item, link tetap tampil tapi tanpa
    # product_id supaya user isi manual di halaman itu.
    checkout_installment_product_id = cart_items[0].get('product_id') if len(cart_items) == 1 else None

    # FIX: checkout.html sudah punya UI lengkap untuk status "sedang direview"
    # (banner KYC, opsi kredit "Sedang Direview", polling /api/kyc/status) yang
    # bergantung pada kyc_pending & kyc_pending_since, tapi kedua variabel itu
    # sebelumnya tidak pernah dikirim ke template ini -- jadi user yang KYC-nya
    # masih pending_review selalu melihat pesan "⚠️ Verifikasi KYC Diperlukan"
    # (seolah belum pernah mengajukan sama sekali) alih-alih status pending
    # yang sebenarnya.
    kyc_pending_session = None
    if current_user.is_authenticated and isinstance(current_user, User) and not current_user.kyc_verified:
        kyc_pending_session = KYCVerificationSession.query.filter_by(
            user_id=current_user.id, status='pending_review'
        ).order_by(KYCVerificationSession.id.desc()).first()

    return render_template('checkout.html',
                           cart_items=cart_items,
                           subtotal=subtotal,
                           subtotal_rupiah=subtotal,
                           subtotal_formatted=format_price(int(subtotal) * 100),
                           discount=discount,
                           total=total,
                           total_rupiah=total,
                           total_formatted=format_price(int(total) * 100),
                           shipping_enabled=shipping_enabled,
                           shipping_couriers=shipping_couriers,
                           shipping_provinces=shipping_provinces,
                           shipping_free_threshold=shipping_free_threshold,
                           packaging_cost=packaging_cost,
                           packaging_cost_sen=packaging_cost,
                           active_couriers_detail=active_couriers_detail,
                           payment_bank_enabled=payment_bank_enabled,
                           payment_qris_enabled=payment_qris_enabled,
                           payment_qris_image_url=payment_qris_image_url,
                           payment_notivgo_enabled=payment_notivgo_enabled,
                           payment_cod_enabled=payment_cod_enabled,
                           cod_fee=cod_fee,
                           cod_fee_sen=cod_fee,
                           cod_max_order=cod_max_order,
                           cod_areas=cod_areas,
                           bank_accounts_json=bank_accounts_json,
                           kyc_pending=kyc_pending_session,
                           kyc_pending_since=kyc_pending_session.created_at if kyc_pending_session else None,
                           checkout_installment_product_id=checkout_installment_product_id,
                           **credit_vars)

# =============================================================================
# CHECKOUT CREDIT VARIABLES HELPER
# =============================================================================
def _get_checkout_credit_dp_min() -> int:
    """DP minimum (rupiah) khusus untuk pilihan cicilan langsung di checkout.
    Default Rp500.000, bisa diatur admin lewat setting 'credit_min_dp_checkout'."""
    return _safe_int(get_setting('credit_min_dp_checkout', '500000'), 500000)


def _get_checkout_credit_max_tenor() -> int:
    return int(get_setting('credit_max_tenor', '24') or 24)


def _get_checkout_credit_plans(subtotal_rupiah=None) -> list:
    """Paket cicilan yang bisa dipilih langsung di checkout, diambil dari
    InstallmentPlan (pengaturan admin di Pengaturan Cicilan) — BUKAN lagi
    dari tabel bunga hardcoded di CreditCalculator. Dibatasi oleh
    credit_max_tenor (setting admin) dan, jika subtotal diketahui, oleh
    min/max_order_amount masing-masing plan supaya hanya paket yang
    benar-benar berlaku untuk total belanja saat ini yang ditampilkan."""
    max_tenor = _get_checkout_credit_max_tenor()
    plans = (InstallmentPlan.query
             .filter_by(is_active=True)
             .filter(InstallmentPlan.tenor_months <= max_tenor)
             .order_by(InstallmentPlan.tenor_months)
             .all())
    if subtotal_rupiah is not None:
        plans = [
            p for p in plans
            if (p.min_order_amount or 0) <= subtotal_rupiah <= (p.max_order_amount or float('inf'))
        ]
    return plans


def _get_checkout_credit_vars(subtotal_rupiah=None):
    """Get credit-related variables for checkout template."""
    # WAJIB true untuk user yang sudah KYC verified, terlepas dari toggle
    # admin credit_enabled — lihat is_credit_available_for_current_user().
    credit_enabled = is_credit_available_for_current_user()
    approved_credit = None
    credit_max_tenor = _get_checkout_credit_max_tenor()
    credit_plans = _get_checkout_credit_plans(subtotal_rupiah)

    # DP minimum yang ditampilkan di badge "Pilih DP & Tenor" — diambil dari
    # persentase DP paling ringan di antara paket yang benar-benar tersedia,
    # supaya tidak menyesatkan (sebelumnya nilai ini flat dari setting admin
    # dan lepas dari aturan DP per-paket).
    if credit_plans and subtotal_rupiah:
        cheapest_min_dp_pct = min((p.min_dp_percentage or 0) for p in credit_plans)
        credit_min_dp = max(round(subtotal_rupiah * cheapest_min_dp_pct), _get_checkout_credit_dp_min())
    else:
        credit_min_dp = _get_checkout_credit_dp_min()

    # Pilihan cicilan langsung di checkout (DP + paket manual) hanya untuk
    # user yang sudah verifikasi KYC, belum punya pengajuan kredit yang
    # sudah disetujui sebelumnya, dan ada minimal satu paket cicilan aktif
    # yang berlaku untuk total belanja saat ini.
    credit_inline_available = (
        credit_enabled
        and current_user.is_authenticated
        and isinstance(current_user, User)
        and current_user.kyc_verified
        and bool(credit_plans)
    )

    if credit_enabled and current_user.is_authenticated and isinstance(current_user, User):
        credit_app = InstallmentApplication.query.filter_by(
            user_id=current_user.id,
            status='approved'
        ).order_by(InstallmentApplication.approved_at.desc()).first()

        if credit_app:
            installments = []
            if hasattr(credit_app, 'schedules') and credit_app.schedules:
                for sched in credit_app.schedules:
                    installments.append({
                        'installment_no': sched.installment_no,
                        'due_date': sched.due_date,
                        # FIX: sched.total_amount (== sched.amount == monthly_installment) is
                        # stored in RUPIAH, but checkout.html renders it with format_price()
                        # which expects CENTS. Convert here, not in the template.
                        'total_amount': int(round((sched.total_amount or 0) * 100))
                    })

            # NOTE: applications can be created via two flows that historically
            # used different column names on the same InstallmentApplication
            # model (see api_credit_check_approved for the full explanation):
            #   - "/api/credit/apply" -> product_price (CENTS), down_payment, principal, total_interest (RUPIAH)
            #   - "/api/installment/apply" -> product_amount, dp_amount, loan_amount, interest_amount (all RUPIAH)
            # Fall back across both naming schemes so the checkout page shows
            # the real numbers regardless of which flow the approval came from.
            #
            # FIX: checkout.html renders every one of these figures through
            # format_price(), which expects CENTS everywhere. down_payment,
            # principal, monthly_installment and total_interest are always stored
            # in RUPIAH (see CreditCalculator.calculate), and product_price is only
            # in cents for the legacy flow. Previously these were passed straight
            # through, so the whole "Detail Kredit" card (DP, pokok hutang, bunga,
            # angsuran/bulan, and the schedule preview) displayed ~100x too small —
            # this is also what CHECKOUT.approvedCredit gets initialized from on
            # the page, so the client-side total preview was wrong too. Normalize
            # everything to cents here, once, so format_price() and the JS math
            # that consumes this same dict are both correct.
            product_price_cents = (
                int(credit_app.product_price) if credit_app.product_price
                else int(round((credit_app.product_amount or 0) * 100))
            )
            down_payment_rupiah = credit_app.down_payment or credit_app.dp_amount or 0
            principal_rupiah = credit_app.principal or credit_app.loan_amount or 0
            monthly_installment_rupiah = credit_app.monthly_installment or 0
            total_interest_rupiah = credit_app.total_interest or credit_app.interest_amount or 0

            approved_credit = {
                'id': credit_app.id,
                'product_price': product_price_cents,
                'down_payment': int(round(down_payment_rupiah * 100)),
                'principal': int(round(principal_rupiah * 100)),
                'tenor_months': credit_app.tenor_months or (credit_app.plan.tenor_months if credit_app.plan else 0),
                'monthly_installment': int(round(monthly_installment_rupiah * 100)),
                'total_interest': int(round(total_interest_rupiah * 100)),
                'interest_type': credit_app.interest_type or (credit_app.plan.interest_type if credit_app.plan else 'flat'),
                'installments': installments
            }

    # Kalau user sudah punya pengajuan kredit yang disetujui, dia pakai itu
    # (kartu "Aktif" seperti sebelumnya) — picker DP/tenor manual hanya
    # ditampilkan kalau belum ada pengajuan yang disetujui.
    if approved_credit:
        credit_inline_available = False

    return {
        'credit_enabled': credit_enabled,
        'approved_credit': approved_credit,
        'credit_min_dp': credit_min_dp,
        'credit_min_dp_formatted': format_price(credit_min_dp * 100),
        'credit_max_tenor': credit_max_tenor,
        'credit_plans': [p.to_dict() for p in credit_plans],
        'credit_inline_available': credit_inline_available,
    }

# =============================================================================
# CREDIT CALCULATOR
# =============================================================================
class CreditCalculator:
    """Calculate credit installments with flat and effective interest."""
    RATES = {
        'flat': {3: 0.015, 6: 0.018, 12: 0.020, 24: 0.022},
        'effective': {3: 0.012, 6: 0.015, 12: 0.018, 24: 0.020}
    }

    @classmethod
    def calculate(cls, product_price_cents: int, down_payment_pct: float, tenor: int, interest_type: str = 'flat'):
        price = product_price_cents / 100
        dp_amount = round(price * down_payment_pct)
        principal = price - dp_amount

        rate = cls.RATES.get(interest_type, cls.RATES['flat']).get(tenor, 0.02)

        if interest_type == 'flat':
            total_interest = round(principal * rate * tenor)
            total_payment = principal + total_interest
            monthly = round(total_payment / tenor)
        else:
            monthly_rate = rate
            if monthly_rate == 0:
                monthly = round(principal / tenor)
            else:
                factor = (monthly_rate * (1 + monthly_rate) ** tenor) / ((1 + monthly_rate) ** tenor - 1)
                monthly = round(principal * factor)
            total_payment = monthly * tenor
            total_interest = total_payment - principal

        return {
            'product_price': product_price_cents,
            'product_price_rupiah': price,
            'down_payment_pct': down_payment_pct,
            'down_payment': dp_amount,
            'principal': principal,
            'tenor': tenor,
            'interest_type': interest_type,
            'interest_rate_monthly': rate,
            'interest_rate_annual': rate * 12,
            'monthly_installment': monthly,
            'total_interest': total_interest,
            'total_payment': total_payment,
            'total_cost': total_payment + dp_amount
        }

@app.route('/api/checkout/credit/simulate', methods=['POST'])
@login_required
def api_checkout_credit_simulate():
    """Hitung simulasi cicilan (paket + DP pilihan user) untuk isi keranjang
    checkout saat ini. Hanya untuk user yang sudah verifikasi KYC. Paket
    cicilan (tenor, bunga, batas DP) diambil dari InstallmentPlan yang admin
    atur di Pengaturan Cicilan — bukan lagi dari tabel bunga hardcoded."""
    credit_enabled = is_credit_available_for_current_user()
    if not credit_enabled:
        return jsonify({'success': False, 'message': 'Pembayaran kredit belum tersedia'}), 400

    if not isinstance(current_user, User) or not current_user.kyc_verified:
        return jsonify({'success': False, 'message': 'Verifikasi KYC wajib untuk menggunakan pembayaran kredit.'}), 403

    cart_items = session.get('cart', [])
    if not cart_items:
        return jsonify({'success': False, 'message': 'Keranjang belanja kosong'}), 400

    subtotal_cents = 0
    for item in cart_items:
        product = Product.query.get(item.get('product_id'))
        if product:
            subtotal_cents += product.price * item['quantity']

    if subtotal_cents <= 0:
        return jsonify({'success': False, 'message': 'Keranjang belanja kosong'}), 400

    data = request.get_json() or {}
    dp_amount = _safe_int(data.get('dp_amount', 0))
    plan_id = data.get('plan_id')
    try:
        plan_id = int(plan_id)
    except (TypeError, ValueError):
        return jsonify({'success': False, 'message': 'Paket cicilan tidak valid'}), 400

    subtotal_rupiah = subtotal_cents / 100
    available_plans = _get_checkout_credit_plans(subtotal_rupiah)
    plan = next((p for p in available_plans if p.id == plan_id), None)
    if not plan:
        return jsonify({'success': False, 'message': 'Paket cicilan tidak tersedia untuk total belanja ini'}), 400

    min_dp = round(subtotal_rupiah * (plan.min_dp_percentage or 0))
    max_dp = round(subtotal_rupiah * (plan.max_dp_percentage or 1))

    if dp_amount < min_dp:
        return jsonify({
            'success': False,
            'message': f'Uang muka (DP) minimum untuk paket {plan.name} adalah {format_price(min_dp * 100)}'
        }), 400

    if dp_amount > max_dp or dp_amount >= subtotal_rupiah:
        return jsonify({
            'success': False,
            'message': f'Uang muka (DP) maksimum untuk paket {plan.name} adalah {format_price(max_dp * 100)}'
        }), 400

    loan_amount = subtotal_rupiah - dp_amount
    monthly_installment = plan.calculate_installment(subtotal_rupiah, dp_amount)
    total_payment = monthly_installment * plan.tenor_months
    total_interest = max(0, total_payment - loan_amount - (plan.processing_fee or 0))

    # FIX: nilai di atas (loan_amount, monthly_installment, total_interest,
    # total_payment) dihitung dalam RUPIAH, sedangkan checkout.html's JS
    # memakai field mentah (non "_formatted") langsung dalam matematika
    # preview total yang lain sepenuhnya berbasis SEN (subtotal, shipping,
    # packaging, approvedCredit). Konversi ke sen di sini supaya
    # penjumlahannya tidak salah 100x.
    down_payment_cents = int(round(dp_amount * 100))
    principal_cents = int(round(loan_amount * 100))
    monthly_installment_cents = int(round(monthly_installment * 100))
    total_interest_cents = int(round(total_interest * 100))
    total_payment_cents = int(round(total_payment * 100))

    return jsonify({
        'success': True,
        'calc': {
            'plan_id': plan.id,
            'plan_name': plan.name,
            'product_price': subtotal_cents,
            'product_price_formatted': format_price(subtotal_cents),
            'down_payment': down_payment_cents,
            'down_payment_formatted': format_price(down_payment_cents),
            'principal': principal_cents,
            'principal_formatted': format_price(principal_cents),
            'tenor': plan.tenor_months,
            'monthly_installment': monthly_installment_cents,
            'monthly_installment_formatted': format_price(monthly_installment_cents),
            'total_interest': total_interest_cents,
            'total_interest_formatted': format_price(total_interest_cents),
            'total_payment': total_payment_cents,
            'total_payment_formatted': format_price(total_payment_cents),
        }
    })



# =============================================================================
# INSTALLMENT / CREDIT APPLICATION ROUTES
# =============================================================================
@app.route('/installment/apply')
@login_required
def installment_apply():
    """Public page for applying for installment/credit."""
    if not isinstance(current_user, User):
        flash('Akses hanya untuk pelanggan', 'error')
        return redirect(url_for('index'))

    # Check KYC requirement
    kyc_required = get_setting('credit_require_kyc', 'true') == 'true'
    if kyc_required and not current_user.kyc_verified:
        flash('Verifikasi KYC wajib dilakukan sebelum mengajukan kredit. Anda akan diarahkan ke halaman verifikasi.', 'warning')
        return redirect(url_for('kyc_verification_page', redirect=url_for('installment_apply')))

    product_id = request.args.get('product_id', type=int)
    product = Product.query.get(product_id) if product_id else None
    plans = InstallmentPlan.query.filter_by(is_active=True).order_by(InstallmentPlan.tenor_months).all()

    # FIX: user yang sudah KYC verified (dan datanya sudah direview/disetujui admin
    # lewat KYCVerificationSession) tidak perlu mengisi ulang Data Diri & Dokumen di
    # step 2 — data KTP/NIK/alamat yang sudah tervalidasi dipakai untuk prefill.
    verified_kyc_session = get_latest_verified_kyc_session(current_user.id) if current_user.kyc_verified else None

    return render_template('installment_apply.html',
                           product=product,
                           plans=plans,
                           format_price=format_price,
                           get_product_image_url=get_product_image_url,
                           kyc_verified=current_user.kyc_verified,
                           verified_kyc_session=verified_kyc_session)

# =============================================================================
# CREDIT API ENDPOINTS
# =============================================================================
@app.route('/api/credit/check-approved', methods=['GET'])
@login_required
def api_credit_check_approved():
    """Check if current user has approved credit."""
    if not isinstance(current_user, User):
        return jsonify({'success': False, 'message': 'User only'}), 403

    # BUGFIX: credit_enabled hanya mengontrol apakah widget kalkulator kredit di
    # halaman produk boleh dipakai untuk mengajukan kredit BARU. Sebelumnya endpoint
    # ini langsung return has_credit=False kalau credit_enabled mati, padahal user
    # bisa saja sudah punya pengajuan cicilan yang disetujui lewat alur terpisah
    # (/installment/apply, kode aplikasi berawalan "CLN-") yang tidak bergantung pada
    # setting itu sama sekali. Akibatnya "Ringkasan Cicilan" & jadwal pembayaran di
    # profil selalu kosong meski ada cicilan aktif yang sudah disetujui. credit_enabled
    # tetap dikirim ke frontend untuk keperluan lain, tapi tidak lagi menghalangi
    # pencarian pengajuan yang sudah disetujui.
    credit_enabled = get_setting('credit_enabled', 'false').lower() in ('true', '1', 'yes')

    credit_app = InstallmentApplication.query.filter_by(
        user_id=current_user.id,
        status='approved'
    ).order_by(InstallmentApplication.approved_at.desc()).first()

    if not credit_app:
        return jsonify({
            'success': True,
            'has_credit': False,
            'credit_enabled': credit_enabled
        })

    installments = []
    if hasattr(credit_app, 'schedules') and credit_app.schedules:
        for sched in credit_app.schedules:
            installments.append({
                'installment_no': getattr(sched, 'installment_no', None) or getattr(sched, 'installment_number', None),
                'due_date': sched.due_date.isoformat() if sched.due_date else None,
                'principal_amount': sched.principal_amount,
                'interest_amount': sched.interest_amount,
                'total_amount': getattr(sched, 'total_amount', None) or getattr(sched, 'amount', None),
                'status': sched.status
            })

    # NOTE: applications can be created via two flows that historically used
    # different column names on the same InstallmentApplication model:
    #   - legacy "/api/credit/apply" flow -> product_price (CENTS), down_payment, principal, total_interest (all rupiah)
    #   - current "/installment/apply" flow -> product_amount, dp_amount, loan_amount, interest_amount (all rupiah)
    # Fall back across both naming schemes so the profile page renders correctly either way.
    # FIX: product_price on the legacy flow is stored in cents (see CreditCalculator.calculate,
    # which keeps 'product_price' as the raw product_price_cents), but profile.html's
    # formatRupiah() does NOT divide by 100 (unlike format_price()). Returning raw cents here
    # made the product price display 100x too large on the profile page for users who applied
    # via the direct "beli kredit dari produk" flow. Convert to rupiah before returning.
    tenor_months = credit_app.tenor_months or (credit_app.plan.tenor_months if credit_app.plan else 0)

    if credit_app.product_price:
        product_price_rupiah = round(credit_app.product_price / 100)
    else:
        product_price_rupiah = credit_app.product_amount

    return jsonify({
        'success': True,
        'has_credit': True,
        'credit_enabled': credit_enabled,
        'credit': {
            'id': credit_app.id,
            'product_price': product_price_rupiah,
            'down_payment': credit_app.down_payment or credit_app.dp_amount,
            'principal': credit_app.principal or credit_app.loan_amount,
            'tenor_months': tenor_months,
            'monthly_installment': credit_app.monthly_installment,
            'total_interest': credit_app.total_interest or credit_app.interest_amount,
            'interest_type': credit_app.interest_type,
            'status': credit_app.status,
            'installments': installments
        }
    })


@app.route('/api/credit/my-applications', methods=['GET'])
@login_required
def api_credit_my_applications():
    """Return all of the current user's credit/installment applications
    (any status), normalized to rupiah, so the profile page can show a real
    application history instead of the previous hardcoded placeholder."""
    if not isinstance(current_user, User):
        return jsonify({'success': False, 'message': 'User only'}), 403

    apps = InstallmentApplication.query.filter_by(
        user_id=current_user.id
    ).order_by(InstallmentApplication.created_at.desc()).limit(20).all()

    def _normalize(a):
        # Same cents-vs-rupiah normalization as /api/credit/check-approved:
        # legacy "/api/credit/apply" flow stores product_price in CENTS,
        # current "/installment/apply" flow stores product_amount in RUPIAH.
        if a.product_price:
            product_price_rupiah = round(a.product_price / 100)
        else:
            product_price_rupiah = a.product_amount

        product_name = None
        if getattr(a, 'product_id', None):
            prod = getattr(a, 'product', None)
            if prod is not None:
                product_name = getattr(prod, 'name', None)

        return {
            'id': a.id,
            'application_code': a.application_code,
            'product_name': product_name,
            'product_price': product_price_rupiah,
            'monthly_installment': a.monthly_installment,
            'tenor_months': a.tenor_months or (a.plan.tenor_months if a.plan else None),
            'status': a.status,
            'created_at': a.created_at.isoformat() if a.created_at else None,
            'rejection_reason': getattr(a, 'rejection_reason', None),
        }

    return jsonify({
        'success': True,
        'applications': [_normalize(a) for a in apps]
    })


@app.route('/api/credit/apply', methods=['POST'])
@login_required
@csrf_protect_endpoint
def api_credit_apply():
    """Submit credit application with KTP and selfie verification."""
    if not isinstance(current_user, User):
        return jsonify({'success': False, 'message': 'User only'}), 403

    credit_enabled = is_credit_available_for_current_user()
    if not credit_enabled:
        return jsonify({'success': False, 'message': 'Credit system disabled'}), 403

    # SECURITY: enforce KYC server-side. The pages that link here already
    # redirect unverified users, but that check must also happen here or
    # this endpoint can be called directly, bypassing KYC entirely.
    kyc_required = get_setting('credit_require_kyc', 'true') == 'true'
    if kyc_required and not current_user.kyc_verified:
        # Sertakan product_id & redirect balik ke halaman produk agar
        # frontend bisa mengarahkan user langsung ke kyc.html dengan konteks
        # yang sama seperti tombol "Lanjut" di step 1 (bukan hanya ke
        # /kyc-verification kosong lalu balik ke /profile).
        pid_for_redirect = request.form.get('product_id', type=int)
        kyc_url = url_for('kyc_verification_page')
        if pid_for_redirect:
            back_url = url_for('product_detail', id=pid_for_redirect)
            kyc_url = url_for('kyc_verification_page', product_id=pid_for_redirect, redirect=back_url)
        return jsonify({
            'success': False,
            'message': 'Verifikasi KYC wajib diselesaikan sebelum mengajukan kredit.',
            'redirect': kyc_url
        }), 403

    product_id = request.form.get('product_id', type=int)
    down_payment_pct = request.form.get('down_payment_pct', type=float)
    tenor = request.form.get('tenor', type=int)
    interest_type = request.form.get('interest_type', 'flat')

    ktp_name = request.form.get('ktp_name', '').strip()
    ktp_nik = request.form.get('ktp_nik', '').strip()
    ktp_address = request.form.get('ktp_address', '').strip()
    employment_status = request.form.get('employment_status', '').strip()
    monthly_income = request.form.get('monthly_income', type=int) or 0
    emergency_contact_name = request.form.get('emergency_contact_name', '').strip()
    emergency_contact_phone = request.form.get('emergency_contact_phone', '').strip()

    # SECURITY: user yang sudah KYC verified TIDAK BOLEH mengirim identitasnya
    # sendiri lewat form — nilai ktp_name/ktp_nik/ktp_address di atas hanya
    # prefill di frontend (product.html) dan bisa dimanipulasi lewat DevTools
    # atau panggilan API langsung. Untuk user kyc_verified, timpa dengan data
    # resmi dari sesi KYC yang sudah direview admin / data user di database,
    # abaikan sepenuhnya apa pun yang dikirim client untuk field-field ini.
    kyc_session_for_app = get_latest_verified_kyc_session(current_user.id) if current_user.kyc_verified else None
    if current_user.kyc_verified:
        ktp_name = (kyc_session_for_app.ktp_name if kyc_session_for_app else None) or current_user.full_name or ktp_name
        ktp_nik = (kyc_session_for_app.ktp_nik if kyc_session_for_app else None) or current_user.kyc_nik or ktp_nik
        ktp_address = (kyc_session_for_app.ktp_address if kyc_session_for_app else None) or current_user.address or ktp_address

    if not all([product_id, down_payment_pct is not None, tenor, ktp_name, ktp_nik, ktp_address]):
        return jsonify({'success': False, 'message': 'Semua field wajib diisi'}), 400

    if len(ktp_nik) != 16 or not ktp_nik.isdigit():
        return jsonify({'success': False, 'message': 'NIK harus 16 digit angka'}), 400

    product = Product.query.get(product_id)
    if not product:
        return jsonify({'success': False, 'message': 'Produk tidak ditemukan'}), 404

    # VALIDASI KERAS: DP minimum (nominal Rp500.000) & tenor maksimum (24 bulan)
    # sesuai pengaturan admin. Sinkron dengan MIN_DP_NOMINAL & peringatan yang
    # sudah ditampilkan di product.html, dan dengan alur checkout kredit
    # (_get_checkout_credit_dp_min / _get_checkout_credit_max_tenor).
    min_dp_nominal = _get_checkout_credit_dp_min()
    max_tenor = _get_checkout_credit_max_tenor()

    product_price_rupiah = (product.price or 0) / 100  # price disimpan dalam sen
    dp_amount_nominal = round(product_price_rupiah * down_payment_pct)

    if dp_amount_nominal < min_dp_nominal:
        return jsonify({
            'success': False,
            'message': f'Uang muka (DP) minimum adalah {format_price(min_dp_nominal * 100)}'
        }), 400

    if tenor > max_tenor:
        return jsonify({
            'success': False,
            'message': f'Tenor maksimum adalah {max_tenor} bulan'
        }), 400

    if tenor not in CreditCalculator.RATES.get(interest_type, CreditCalculator.RATES['flat']):
        return jsonify({'success': False, 'message': 'Tenor tidak valid'}), 400

    existing = InstallmentApplication.query.filter_by(
        user_id=current_user.id,
        product_id=product_id,
        status='pending'
    ).first()
    if existing:
        return jsonify({'success': False, 'message': 'Anda sudah memiliki pengajuan pending untuk produk ini'}), 400

    # FITUR: user yang sudah KYC verified punya KTP+selfie tervalidasi
    # tersimpan di server (KYCVerificationSession) — untuk mereka, salin
    # dokumen yang sudah ada alih-alih memaksa upload ulang file yang sama.
    use_existing_kyc = request.form.get('use_existing_kyc') == '1'

    if use_existing_kyc:
        # Hanya diperbolehkan untuk user yang benar-benar sudah KYC verified
        # server-side -- jangan percaya flag ini begitu saja dari client.
        if not current_user.kyc_verified:
            return jsonify({'success': False, 'message': 'Anda belum terverifikasi KYC.'}), 403
        if not current_user.kyc_ktp_photo or not current_user.kyc_selfie_photo:
            return jsonify({'success': False, 'message': 'Data dokumen KYC tidak lengkap. Silakan ajukan verifikasi ulang.'}), 400
        try:
            ktp_filename = _copy_kyc_doc_to_installment(current_user.kyc_ktp_photo, prefix='ktp_kyc_')
            selfie_filename = _copy_kyc_doc_to_installment(current_user.kyc_selfie_photo, prefix='selfie_kyc_')
        except FileNotFoundError:
            return jsonify({'success': False, 'message': 'Dokumen KYC tidak ditemukan di server. Silakan ajukan verifikasi ulang.'}), 400
    else:
        ktp_file = request.files.get('ktp_photo')
        selfie_file = request.files.get('selfie_photo')

        if not ktp_file or not selfie_file:
            return jsonify({'success': False, 'message': 'Foto KTP dan selfie wajib diupload'}), 400

        ktp_filename = save_uploaded_image(ktp_file, prefix='ktp_', subfolder='installment_docs')
        if not ktp_filename:
            return jsonify({'success': False, 'message': 'Format foto KTP tidak didukung'}), 400

        selfie_filename = save_uploaded_image(selfie_file, prefix='selfie_', subfolder='installment_docs')
        if not selfie_filename:
            return jsonify({'success': False, 'message': 'Format foto selfie tidak didukung'}), 400

    calc = CreditCalculator.calculate(
        product_price_cents=product.price,
        down_payment_pct=down_payment_pct,
        tenor=tenor,
        interest_type=interest_type
    )

    # kyc_session_for_app sudah dihitung di atas (dipakai juga untuk override
    # identitas), tidak perlu query ulang di sini.

    # Generate application_code (wajib NOT NULL, unik)
    app_code = f"KRD-{datetime.now().strftime('%Y%m%d')}-{uuid.uuid4().hex[:4].upper()}"

    try:
        application = InstallmentApplication(
            application_code=app_code,
            user_id=current_user.id,
            plan_id=None,  # aplikasi kredit langsung dari produk, tidak terikat InstallmentPlan
            product_id=product_id,
            kyc_session_id=kyc_session_for_app.id if kyc_session_for_app else None,
            # Field kanonis (dipakai admin panel, to_dict(), generate_schedule()) --
            # wajib diisi juga di sini, bukan hanya field duplikat ktp_*/product_price
            # di bawah, atau NOT NULL constraint pada full_name/nik/phone/address_ktp/
            # product_amount/dp_amount/loan_amount/total_loan akan gagal saat commit.
            full_name=ktp_name,
            nik=ktp_nik,
            phone=(current_user.phone or ''),
            address_ktp=ktp_address,
            employment_type=employment_status or None,
            monthly_income=monthly_income,
            emergency_contact_name=emergency_contact_name,
            emergency_contact_phone=emergency_contact_phone,
            # FIX: product_amount is RUPIAH everywhere else (admin templates multiply it
            # by 100 for format_price). calc['product_price'] is the raw CENTS input;
            # use calc['product_price_rupiah'] so the unit matches convention.
            product_amount=calc['product_price_rupiah'],
            dp_amount=calc['down_payment'],
            loan_amount=calc['principal'],
            interest_amount=calc['total_interest'],
            total_loan=calc['total_payment'],
            # Field duplikat khusus alur "kredit langsung dari produk"
            product_price=calc['product_price'],
            down_payment=calc['down_payment'],
            down_payment_pct=down_payment_pct,
            principal=calc['principal'],
            tenor_months=tenor,
            interest_type=interest_type,
            interest_rate_monthly=calc['interest_rate_monthly'],
            interest_rate_annual=calc['interest_rate_annual'],
            monthly_installment=calc['monthly_installment'],
            total_interest=calc['total_interest'],
            total_payment=calc['total_payment'],
            ktp_name=ktp_name,
            ktp_nik=ktp_nik,
            ktp_address=ktp_address,
            ktp_photo=ktp_filename,
            selfie_photo=selfie_filename,
            biometric_verified=use_existing_kyc,
            biometric_match_score=current_user.kyc_match_score if use_existing_kyc else None,
            employment_status=employment_status,
            status='pending',
            created_at=datetime.utcnow(),
            ip_address=request.remote_addr,
            user_agent=request.user_agent.string if request.user_agent else None
        )
        db.session.add(application)
        db.session.flush()

        for i in range(1, tenor + 1):
            due_date = datetime.utcnow() + relativedelta(months=i)
            principal_per_month = round(calc['principal'] / tenor)
            interest_per_month = round(calc['total_interest'] / tenor)

            schedule = InstallmentSchedule(
                application_id=application.id,
                installment_number=i,
                due_date=due_date,
                principal_amount=principal_per_month,
                interest_amount=interest_per_month,
                amount=principal_per_month + interest_per_month,
                status='pending'
            )
            db.session.add(schedule)

        db.session.commit()

        try:
            review_url = url_for('admin_installment_detail', app_id=application.id, _external=True)
            msg = (
                f"📋 <b>PENGAJUAN KREDIT BARU</b>\n\n"
                f"<b>Pelanggan:</b> {current_user.full_name}\n"
                f"<b>Produk:</b> {product.name}\n"
                f"<b>Harga:</b> {format_price(product.price)}\n"
                f"<b>DP:</b> {calc['down_payment_pct']*100:.0f}% ({format_price(int(calc['down_payment'] * 100))})\n"
                f"<b>Tenor:</b> {tenor} bulan\n"
                f"<b>Angsuran:</b> {format_price(int(calc['monthly_installment'] * 100))}/bulan\n"
                f"<b>Status:</b> Menunggu Review\n\n"
                f"👉 <a href='{review_url}'>Review di Panel Admin</a>"
            )
            NotificationService.send_telegram(msg)
        except Exception as e:
            logging.error(f"Credit notification error: {e}")

        return jsonify({
            'success': True,
            'message': 'Pengajuan kredit berhasil dikirim! Tim kami akan meninjau dalam 1x24 jam.',
            'application_id': application.id
        })

    except Exception as e:
        db.session.rollback()
        logging.error(f"Credit application error: {e}", exc_info=True)
        return jsonify({'success': False, 'message': f'Terjadi kesalahan: {str(e)}'}), 500

@app.route('/api/credit/simulate', methods=['POST'])
def api_credit_simulate():
    """Simulate credit calculation without applying."""
    data = request.get_json() or {}

    try:
        product_id = int(data.get('product_id'))
    except (TypeError, ValueError):
        return jsonify({'success': False, 'message': 'product_id tidak valid'}), 400

    try:
        down_payment_pct = float(data.get('down_payment_pct', 0.3))
    except (TypeError, ValueError):
        down_payment_pct = 0.3

    try:
        tenor = int(data.get('tenor', 12))
    except (TypeError, ValueError):
        tenor = 12

    interest_type = data.get('interest_type', 'flat')

    product = Product.query.get(product_id)
    if not product:
        return jsonify({'success': False, 'message': 'Produk tidak ditemukan'}), 404

    calc = CreditCalculator.calculate(
        product_price_cents=product.price,
        down_payment_pct=down_payment_pct,
        tenor=tenor,
        interest_type=interest_type
    )

    return jsonify({
        'success': True,
        'simulation': {
            'product_price': calc['product_price'],
            'down_payment': calc['down_payment'],
            'principal': calc['principal'],
            'tenor_months': calc['tenor'],
            'monthly_installment': calc['monthly_installment'],
            'total_interest': calc['total_interest'],
            'total_payment': calc['total_payment'],
            'interest_type': calc['interest_type'],
            'interest_rate_annual': calc['interest_rate_annual']
        }
    })

@app.route('/order-success')
def order_success():
    return render_template('order_success.html')

# =============================================================================
# INVOICE & QUICK VIEW
# =============================================================================
@app.route('/invoice/<int:order_id>')
@login_required
def invoice(order_id):
    order = Order.query.get_or_404(order_id)
    if not isinstance(current_user, Admin) and order.user_id != current_user.id:
        flash('Akses ditolak.', 'error')
        return redirect(url_for('index'))

    status_map = {
        'Pending': 'Menunggu Pembayaran', 'Paid': 'Lunas', 'Processing': 'Diproses',
        'Ready_to_Ship': 'Siap Dikirim', 'Shipped': 'Dikirim',
        'Out_for_Delivery': 'Sedang Diantar', 'Received': 'Diterima',
        'Completed': 'Selesai', 'Cancelled': 'Dibatalkan', 'Returned': 'Dikembalikan'
    }
    order_status_text = status_map.get(order.status, order.status)
    wit = order.created_at + timedelta(hours=9) if order.created_at else datetime.utcnow() + timedelta(hours=9)
    order_datetime_wit = wit.strftime('%d %B %Y')
    logo_url = get_setting('logo_url')

    bank_accounts = []
    try:
        bank_raw = get_setting('bank_accounts_json', '[]')
        if bank_raw:
            bank_accounts = json.loads(bank_raw) if isinstance(bank_raw, str) else bank_raw
            if not isinstance(bank_accounts, list):
                bank_accounts = []
    except Exception:
        bank_accounts = []

    return render_template('print_order.html',
                           order=order, order_number=order.order_number,
                           order_status=order.status, order_status_text=order_status_text,
                           order_datetime_wit=order_datetime_wit,
                           payment_method=order.payment_method,
                           customer_name=order.customer_name,
                           customer_phone=order.customer_phone,
                           customer_email=order.customer_email or '-',
                           shipping_recipient_name=order.shipping_recipient_name,
                           shipping_phone=order.shipping_phone,
                           shipping_address=order.shipping_address,
                           shipping_district=order.shipping_district,
                           shipping_city=order.shipping_city,
                           shipping_province=order.shipping_province,
                           shipping_postal_code=order.shipping_postal_code,
                           shipping_courier=order.shipping_courier,
                           shipping_service=order.shipping_service,
                           shipping_cost=order.shipping_cost,
                           tracking_number=order.tracking_number,
                           carrier_name=order.carrier_name,
                           items=order.items, subtotal=order.subtotal,
                           discount=order.discount, grand_total=order.total,
                           site_name=get_site_name(),
                           logo_url=logo_url, bank_accounts=bank_accounts,
                           format_price=format_price)

@app.route('/api/product/<int:product_id>')
def api_product_quick_view(product_id):
    product = Product.query.get_or_404(product_id)
    images = product.get_images()
    return jsonify({'success': True, 'product': {
        'id': product.id, 'name': product.name, 'brand': product.brand,
        'model': product.model, 'price': product.price,
        'price_formatted': format_price(product.price),
        'condition': product.condition, 'stock': product.stock,
        'description': product.short_description or product.description,
        'image': get_product_image_url(images[0]) if images else None,
        'images': [get_product_image_url(img) for img in images]
    }})

@app.route('/api/order/<int:order_id>/upload-payment-proof', methods=['POST'])
@login_required
def upload_payment_proof(order_id):
    if not isinstance(current_user, (User, Admin)):
        return jsonify({'success': False, 'message': 'Akses ditolak'}), 403
    order = Order.query.get_or_404(order_id)
    if not isinstance(current_user, Admin) and order.user_id != current_user.id:
        return jsonify({'success': False, 'message': 'Bukan pesanan Anda'}), 403
    if 'proof' not in request.files:
        return jsonify({'success': False, 'message': 'File tidak ditemukan'}), 400
    file = request.files['proof']
    filename = save_uploaded_image(file, prefix='proof_', subfolder='payment_proofs')
    if not filename:
        return jsonify({'success': False, 'message': 'Format tidak didukung'}), 400
    if order.payment_proof:
        old_path = os.path.join(UPLOAD_PAYMENT_PROOFS, order.payment_proof)
        if os.path.exists(old_path):
            try:
                os.remove(old_path)
            except OSError as e:
                app.logger.warning(f"Failed to remove old payment proof {old_path}: {e}")
    order.payment_proof = filename
    db.session.commit()
    return jsonify({'success': True, 'message': 'Bukti berhasil diunggah'})

@app.route('/static/uploads/payment_proofs/<filename>')
@login_required
def uploaded_payment_proof(filename):
    """Bukti pembayaran berisi data finansial pelanggan: hanya Admin atau
    pemilik pesanan yang boleh melihatnya (sebelumnya terbuka untuk publik)."""
    if isinstance(current_user, Admin):
        return send_from_directory(UPLOAD_PAYMENT_PROOFS, filename)
    owns = Order.query.filter_by(payment_proof=filename, user_id=current_user.id).first()
    if owns:
        return send_from_directory(UPLOAD_PAYMENT_PROOFS, filename)
    return jsonify({'success': False, 'message': 'Akses ditolak'}), 403

# =============================================================================
# 2026 PRO: AI CHAT ENDPOINTS
# =============================================================================
@app.route('/api/ai/chat', methods=['POST'])
@rate_limit(limit=30, window=60)
def ai_chat():
    """
    AI Chat endpoint dengan support untuk:
    - Streaming responses
    - Structured outputs (JSON mode)
    - Multimodal (image + text)
    - Tool calling
    - Cost tracking
    """
    try:
        if not BRIDGE_AVAILABLE:
            return jsonify({
                'success': False,
                'reply': 'OpenRouter Bridge tidak tersedia'
            }), 503

        data = request.get_json(silent=True) or {}
        user_message = str(data.get('message', '')).strip()
        stream_mode = data.get('stream', False)
        structured_output = data.get('structured', False)
        image_data = data.get('image')  # Base64 encoded image untuk multimodal

        if not user_message:
            return jsonify({
                'success': False,
                'reply': 'Pesan tidak boleh kosong'
            }), 400

        # Cost tracking & circuit breaker check
        if CircuitBreaker.is_open('openrouter_chat'):
            return jsonify({
                'success': False,
                'reply': 'Layanan AI sedang sibuk. Silakan coba lagi nanti.'
            }), 503

        # Streaming response
        if stream_mode:
            def generate_stream():
                try:
                    # Gunakan OpenRouterClient jika tersedia (2026 Pro)
                    if OPENROUTER_AVAILABLE and hasattr(OpenRouterClient, 'chat_stream'):
                        for chunk in OpenRouterClient.chat_stream(
                            message=user_message,
                            image=image_data,
                            system_prompt=get_setting('ai_system_prompt')
                        ):
                            yield f"data: {json.dumps(chunk)}\n\n"
                    else:
                        # Fallback ke bridge
                        result = OpenRouterTelegramBridge._call_openrouter(user_message)
                        yield f"data: {json.dumps(result)}\n\n"
                    yield "data: [DONE]\n\n"
                    CircuitBreaker.record_success('openrouter_chat')
                except Exception as e:
                    CircuitBreaker.record_failure('openrouter_chat')
                    yield f"data: {json.dumps({'error': str(e)})}\n\n"

            return Response(
                stream_with_context(generate_stream()),
                mimetype='text/event-stream',
                headers={'Cache-Control': 'no-cache', 'X-Accel-Buffering': 'no'}
            )

        # Normal (non-streaming) response dengan structured output support
        result = None
        if OPENROUTER_AVAILABLE and hasattr(OpenRouterClient, 'chat'):
            try:
                result = OpenRouterClient.chat(
                    message=user_message,
                    image=image_data,
                    system_prompt=get_setting('ai_system_prompt'),
                    structured=structured_output
                )
            except Exception as e:
                logger.warning(f"OpenRouterClient.chat failed: {e}")

        if not result or not result.get('success'):
            if BRIDGE_AVAILABLE:
                result = OpenRouterTelegramBridge._call_openrouter(user_message)
            else:
                return jsonify({
                    'success': False,
                    'reply': 'Layanan AI sedang tidak tersedia.'
                }), 503

        if not result.get('success'):
            CircuitBreaker.record_failure('openrouter_chat')
            logger.error(f"Bridge Error: {result.get('error')}")
            return jsonify({
                'success': False,
                'reply': result.get('error', 'AI tidak tersedia')
            }), 500

        # Record cost jika tersedia
        if 'cost' in result and 'model' in result:
            CostTracker.record(
                model=result['model'],
                cost_usd=result.get('cost', 0),
                tokens=result.get('total_tokens', 0)
            )

        CircuitBreaker.record_success('openrouter_chat')

        response_data = {
            'success': True,
            'reply': result.get('reply'),
            'model': result.get('model'),
            'source': 'bridge' if not OPENROUTER_AVAILABLE else 'openrouter_pro'
        }
        if structured_output and 'structured_data' in result:
            response_data['structured_data'] = result['structured_data']
        if 'reasoning' in result:
            response_data['reasoning'] = result['reasoning']

        return jsonify(response_data)

    except Exception as e:
        logger.exception('AI Chat Error')
        return jsonify({
            'success': False,
            'reply': str(e)
        }), 500

@app.route('/api/ai/health', methods=['GET'])
def ai_health():
    if not BRIDGE_AVAILABLE:
        return jsonify({
            'success': False,
            'bridge_available': False
        }), 503

    health_data = {
        'success': True,
        'bridge_available': True,
        'circuit_breaker': {
            'openrouter_chat': CircuitBreaker._states.get('openrouter_chat', {'state': 'closed'})
        },
        'cost_stats_24h': CostTracker.get_stats(hours=24)
    }

    if OPENROUTER_AVAILABLE and hasattr(OpenRouterClient, 'health'):
        health_data.update(OpenRouterClient.health())

    return jsonify(health_data)

@app.route('/api/ai/costs', methods=['GET'])
@admin_required
def ai_costs():
    """Admin endpoint untuk melihat cost tracking."""
    hours = request.args.get('hours', 24, type=int)
    return jsonify(CostTracker.get_stats(hours=hours))

# =============================================================================
# PUBLIC CUSTOMER AI CHAT (2026 Pro)
# =============================================================================
def _public_chat_rate_key() -> str:
    """Key rate-limit yang dipakai KONSISTEN di semua endpoint chat publik
    (non-stream, stream, dan saat melaporkan 'remaining' ke widget). Basis-nya
    sesi browser (_csrf_token), bukan chat session_id, supaya kuota tidak
    ikut reset saat pelanggan menekan 'percakapan baru' (clearChat)."""
    return f"{request.remote_addr}:{session.get('_csrf_token', 'anon')}"


@app.route('/api/public/chat', methods=['POST'])
@rate_limit(limit=20, window=60)
def public_ai_chat():
    """
    AI Chat endpoint untuk PELANGGAN di halaman publik.
    - Read-only (tidak ada aksi admin)
    - Session memory per browser session
    - Rate limited: 20 pesan/menit per sesi
    - Multimodal support
    """
    try:
        data = request.get_json(silent=True) or {}
        user_message = str(data.get('message', '')).strip()
        session_id = str(data.get('session_id', '') or '').strip()
        image_data = data.get('image')  # Base64 untuk multimodal

        if not user_message:
            return jsonify({
                'success': False,
                'reply': 'Pesan tidak boleh kosong.'
            }), 400

        if len(user_message) > 500:
            return jsonify({
                'success': False,
                'reply': 'Pesan terlalu panjang (maksimal 500 karakter).'
            }), 400

        if CUSTOMER_CHAT_AVAILABLE:
            try:
                # Coba dengan image support (multimodal)
                result = process_customer_message(
                    user_message,
                    session_id or None,
                    image=image_data
                )
            except TypeError:
                # Fallback untuk customer_chat lama tanpa image support
                result = process_customer_message(
                    user_message,
                    session_id or None
                )
        else:
            # Fallback ke bridge jika customer_chat tidak tersedia
            if BRIDGE_AVAILABLE:
                result = OpenRouterTelegramBridge._call_openrouter(user_message)
            else:
                return jsonify({
                    'success': False,
                    'reply': 'Layanan chat sedang tidak tersedia.'
                }), 503

        # Add rate limit info.
        # PENTING: key ini HARUS sama persis dengan key yang dipakai oleh
        # decorator @rate_limit di atas (remote_addr + _csrf_token sesi
        # browser), BUKAN session_id chat (yang berubah tiap clearChat()).
        # Sebelumnya dua key ini berbeda sehingga angka 'remaining' yang
        # dikirim ke widget selalu salah/basi.
        result['rate_limit'] = {
            'remaining': RateLimiter.get_remaining(_public_chat_rate_key(), limit=20, window=60)
        }
        return jsonify(result)

    except Exception:
        logger.exception('public_ai_chat error')
        return jsonify({
            'success': False,
            'reply': '😔 Terjadi kesalahan. Silakan coba lagi.'
        }), 500

@app.route('/api/public/chat/clear', methods=['POST'])
def public_ai_chat_clear():
    try:
        data = request.get_json(silent=True) or {}
        session_id = str(data.get('session_id', '') or '').strip()
        if session_id and CUSTOMER_CHAT_AVAILABLE:
            # Archives the conversation (persisted history isn't destroyed,
            # just hidden) and clears the in-memory cache for this session.
            # The widget always generates a fresh session_id right after
            # calling this, so the customer starts a clean conversation.
            SessionMemory.clear(session_id)
        return jsonify({'success': True, 'message': 'Riwayat percakapan dihapus.'})
    except Exception:
        return jsonify({'success': True})


@app.route('/api/public/chat/history', methods=['GET'])
def public_ai_chat_history():
    """
    2026 Pro: return persisted chat history for a session so the widget can
    restore the conversation after a page reload / browser restart instead
    of always showing the welcome message.
    """
    try:
        session_id = str(request.args.get('session_id', '') or '').strip()
        if not session_id or not CUSTOMER_CHAT_AVAILABLE or not CHAT_MEMORY_AVAILABLE:
            return jsonify({'success': True, 'messages': []})

        messages = SessionMemory.get_full_history(session_id)
        # Only expose what the widget needs to render bubbles — never leak
        # internal fields like cost/model unless the cost display is on
        # client-side already (it re-derives that from the same payload).
        safe_messages = [
            {
                'role': m.get('role'),
                'content': m.get('content'),
                'model': m.get('model'),
                'cost_usd': m.get('cost_usd'),
                'created_at': m.get('created_at'),
            }
            for m in messages
            if m.get('role') in ('user', 'assistant')
        ]
        return jsonify({'success': True, 'messages': safe_messages})
    except Exception:
        logger.exception('public_ai_chat_history error')
        return jsonify({'success': True, 'messages': []})


def _run_public_chat(user_message: str, session_id: Optional[str], image_data=None) -> dict:
    """Logika inti chat publik dipakai bersama oleh endpoint non-stream
    (/api/public/chat) dan stream (/api/public/chat/stream), supaya
    perilakunya (fallback, error handling) selalu identik di kedua jalur."""
    if CUSTOMER_CHAT_AVAILABLE:
        try:
            return process_customer_message(user_message, session_id or None, image=image_data)
        except TypeError:
            return process_customer_message(user_message, session_id or None)
    if BRIDGE_AVAILABLE:
        return OpenRouterTelegramBridge._call_openrouter(user_message)
    return {'success': False, 'reply': 'Layanan chat sedang tidak tersedia.'}


def _chunk_reply(text: str, size: int = 3):
    """Pecah teks balasan jadi potongan kecil (per beberapa kata) untuk
    disimulasikan sebagai stream. process_customer_message() TIDAK benar-
    benar streaming token-per-token dari LLM (hasilnya sudah lengkap saat
    dikembalikan) — ini murni mengubah pengalaman "muncul sekaligus" jadi
    progresif di sisi UI, konsisten dengan efek mengetik yang sudah ada di
    chat_widget.js untuk jalur non-stream."""
    words = (text or '').split(' ')
    for i in range(0, len(words), size):
        piece = ' '.join(words[i:i + size])
        if i + size < len(words):
            piece += ' '
        if piece:
            yield piece


@app.route('/api/public/chat/stream', methods=['GET', 'POST'])
@rate_limit(limit=20, window=60)
def public_ai_chat_stream():
    """
    Versi streaming (SSE) dari /api/public/chat, dipakai oleh chat_widget.js:
      - GET  (EventSource) : ?message=...&session_id=...
                             event: {"type":"chunk","content":...} lalu
                             {"type":"done","meta":{...}}
      - POST (fetch fallback bila EventSource gagal, mis. proxy yang tidak
        mendukung SSE via GET) : body JSON {message, session_id}
                             baris: data: {"type":"chunk",...}
                                    data: {"type":"meta",...}
                                    data: [DONE]

    Catatan: gambar (multimodal) SENGAJA tidak didukung di endpoint ini —
    chat_widget.js hanya mengaktifkan streaming saat tidak ada gambar
    terlampir (lihat useStream di chat_widget.js) dan mengirim gambar lewat
    /api/public/chat biasa, supaya base64 besar tidak perlu lewat query
    string GET / EventSource.
    """
    if request.method == 'GET':
        user_message = str(request.args.get('message', '') or '').strip()
        session_id = str(request.args.get('session_id', '') or '').strip()
    else:
        data = request.get_json(silent=True) or {}
        user_message = str(data.get('message', '') or '').strip()
        session_id = str(data.get('session_id', '') or '').strip()

    is_get = request.method == 'GET'

    def error_event(message: str) -> str:
        payload = json.dumps({'type': 'error', 'error': message})
        return f"data: {payload}\n\n"

    if not user_message:
        return Response(error_event('Pesan tidak boleh kosong.'),
                         mimetype='text/event-stream')
    if len(user_message) > 500:
        return Response(error_event('Pesan terlalu panjang (maksimal 500 karakter).'),
                         mimetype='text/event-stream')

    def generate():
        try:
            result = _run_public_chat(user_message, session_id, image_data=None)
            reply = str(result.get('reply') or '') if isinstance(result, dict) else str(result)
            remaining = RateLimiter.get_remaining(_public_chat_rate_key(), limit=20, window=60)

            for piece in _chunk_reply(reply):
                chunk_payload = json.dumps({'type': 'chunk', 'content': piece})
                yield f"data: {chunk_payload}\n\n"

            meta = {
                'model': result.get('model') if isinstance(result, dict) else None,
                'cost_usd': result.get('cost_usd') if isinstance(result, dict) else None,
                'source': result.get('source', 'ai') if isinstance(result, dict) else 'ai',
                'session_id': result.get('session_id') if isinstance(result, dict) else None,
                'rate_limit': {'remaining': remaining},
            }

            if is_get:
                # Kontrak EventSource: satu event 'done' dengan meta bersarang.
                done_payload = json.dumps({'type': 'done', 'meta': meta})
                yield f"data: {done_payload}\n\n"
            else:
                # Kontrak fetch fallback: event 'meta' rata (flat), lalu sentinel [DONE].
                meta_payload = json.dumps({'type': 'meta', **meta})
                yield f"data: {meta_payload}\n\n"
                yield "data: [DONE]\n\n"

        except Exception as e:
            logger.exception('public_ai_chat_stream error')
            yield error_event(str(e) if app.debug else '😔 Terjadi kesalahan. Silakan coba lagi.')

    return Response(
        stream_with_context(generate()),
        mimetype='text/event-stream',
        headers={'Cache-Control': 'no-cache', 'X-Accel-Buffering': 'no'}
    )


# =============================================================================
# 2026 PRO: TOOL CALLING ENDPOINT
# =============================================================================
@app.route('/api/ai/tools', methods=['POST'])
@admin_required
def ai_tools():
    """
    Endpoint untuk AI Tool Calling.
    Memungkinkan AI untuk mengeksekusi aksi admin via function calling.
    """
    try:
        data = request.get_json(silent=True) or {}
        tool_name = data.get('tool')
        parameters = data.get('parameters', {})

        if not tool_name:
            return jsonify({'success': False, 'message': 'Tool name required'}), 400

        # Execute tool via OpenRouterClient jika tersedia
        if OPENROUTER_AVAILABLE and hasattr(OpenRouterClient, 'execute_tool'):
            result = OpenRouterClient.execute_tool(tool_name, parameters)
            return jsonify({
                'success': True,
                'result': result,
                'tool': tool_name
            })

        # Manual tool execution fallback
        tool_results = {
            'store_summary': _tool_store_summary,
            'list_pending': _tool_list_pending,
            'cek_resi': _tool_cek_resi,
            'order_status': _tool_order_status,
        }

        if tool_name in tool_results:
            return jsonify({
                'success': True,
                'result': tool_results[tool_name](**parameters),
                'tool': tool_name
            })

        return jsonify({
            'success': False,
            'message': f'Tool {tool_name} tidak tersedia'
        }), 404

    except Exception as e:
        logger.exception('AI Tool Error')
        return jsonify({'success': False, 'message': str(e)}), 500

def _tool_store_summary():
    today = date.today()
    return {
        'total_products': Product.query.count(),
        'total_orders_today': Order.query.filter(func.date(Order.created_at) == today).count(),
        'pending_orders': Order.query.filter_by(status='Pending').count(),
        'revenue_today': db.session.query(func.coalesce(func.sum(Order.total), 0))
            .filter(Order.status.in_(['Paid','Completed']), func.date(Order.paid_at) == today).scalar()
    }

def _tool_list_pending(limit=10):
    orders = Order.query.filter_by(status='Pending').order_by(Order.created_at.desc()).limit(limit).all()
    return [{'id': o.id, 'order_number': o.order_number, 'customer': o.customer_name,
             'total': o.total, 'created_at': o.created_at.isoformat()} for o in orders]

def _tool_cek_resi(value=''):
    shipment = ShippingTracking.query.filter(
        or_(ShippingTracking.tracking_number.ilike(f'%{value}%'),
            ShippingTracking.recipient_phone.ilike(f'%{value}%'))
    ).first()
    if not shipment:
        return {'found': False}
    return {
        'found': True,
        'tracking_number': shipment.tracking_number,
        'carrier': shipment.carrier,
        'status': shipment.status,
        'location': shipment.current_location
    }

def _tool_order_status(value=''):
    order = Order.query.filter(
        or_(Order.order_number.ilike(f'%{value}%'),
            Order.customer_phone.ilike(f'%{value}%'))
    ).first()
    if not order:
        return {'found': False}
    return {
        'found': True,
        'order_number': order.order_number,
        'status': order.status,
        'customer': order.customer_name,
        'total': order.total
    }

# =============================================================================
# ADMIN ROUTES
# =============================================================================
@app.route('/admin')
@login_required
def admin_index():
    return redirect(url_for('admin_dashboard'))

@app.route('/admin/dashboard')
@admin_required
def admin_dashboard():
    total_products = Product.query.count()
    total_orders = Order.query.count()
    pending_orders = Order.query.filter_by(status='Pending').count()
    processing_orders = Order.query.filter_by(status='Processing').count()
    ready_to_ship_orders = Order.query.filter_by(status='Ready_to_Ship').count()
    shipped_orders = Order.query.filter_by(status='Shipped').count()
    completed_orders = Order.query.filter_by(status='Completed').count()
    paid_orders = Order.query.filter(Order.status.in_(['Paid', 'Completed', 'Shipped'])).all()
    total_revenue = sum(o.total for o in paid_orders)
    recent_orders = Order.query.order_by(Order.created_at.desc()).limit(5).all()
    recent_products = Product.query.order_by(Product.created_at.desc()).limit(5).all()
    ready_redemption = Order.query.filter(Order.status.in_(['Paid', 'Completed'])).count()
    recent_paid = Order.query.filter(Order.status.in_(['Paid', 'Completed'])).order_by(Order.updated_at.desc()).limit(5).all()
    today = date.today()
    new_customers_today = User.query.filter(func.date(User.created_at) == today).count()
    completed_today = Order.query.filter(Order.status == 'Completed', func.date(Order.completed_at) == today).count()
    revenue_chart = []
    for i in range(6, -1, -1):
        day = today - timedelta(days=i)
        day_label = day.strftime('%a')
        day_amount = db.session.query(func.coalesce(func.sum(Order.total), 0)).filter(
            Order.status.in_(['Paid','Completed','Shipped']), func.date(Order.created_at) == day).scalar()
        max_amount = max([day_amount] + [item['amount'] for item in revenue_chart] + [1])
        percent = (day_amount / max_amount * 100) if max_amount > 0 else 0
        revenue_chart.append({'label': day_label, 'amount': day_amount, 'percent': percent})
    now = datetime.now().strftime('%A, %d %B %Y %H:%M')

    # 2026 Pro: AI health & cost stats untuk dashboard
    ai_health_data = None
    if OPENROUTER_AVAILABLE:
        try:
            ai_health_data = CostTracker.get_stats(hours=24)
        except Exception:
            pass

    return render_template('admin/dashboard.html',
                           total_products=total_products, total_orders=total_orders,
                           pending_orders=pending_orders, processing_orders=processing_orders,
                           ready_to_ship_orders=ready_to_ship_orders, shipped_orders=shipped_orders,
                           completed_orders=completed_orders, total_revenue=total_revenue,
                           recent_orders=recent_orders, recent_products=recent_products,
                           ready_for_redemption_count=ready_redemption, recent_paid_orders=recent_paid,
                           new_customers_today=new_customers_today, completed_today=completed_today,
                           revenue_chart=revenue_chart, now=now,
                           openrouter_enabled=get_setting('openrouter_enabled', 'false'),
                           openrouter_base_url=get_setting('openrouter_base_url', 'http://localhost:18789'),
                           format_price=format_price,
                           ai_health=ai_health_data)

# --- Admin Products ---
@app.route('/admin/products')
@admin_required
def admin_products():
    page = request.args.get('page', 1, type=int)
    per_page = 25
    query = Product.query
    q = request.args.get('q', '').strip()
    if q:
        query = query.filter(or_(Product.name.ilike(f'%{q}%'),
                                 Product.brand.ilike(f'%{q}%'),
                                 Product.model.ilike(f'%{q}%')))
    condition = request.args.get('condition', '').strip()
    if condition:
        query = query.filter_by(condition=condition)
    brand = request.args.get('brand', '').strip()
    if brand:
        query = query.filter_by(brand=brand)
    sort = request.args.get('sort', 'newest')
    if sort == 'price_asc':
        query = query.order_by(asc(Product.price))
    elif sort == 'price_desc':
        query = query.order_by(desc(Product.price))
    elif sort == 'name':
        query = query.order_by(asc(Product.name))
    elif sort == 'stock_low':
        query = query.order_by(asc(Product.stock))
    else:
        query = query.order_by(desc(Product.created_at))
    pagination = query.paginate(page=page, per_page=per_page, error_out=False)
    return render_template('admin/products.html', products=pagination.items, pagination=pagination)

@app.route('/admin/product/new', methods=['GET', 'POST'])
@admin_required
def admin_product_new():
    if request.method == 'POST':
        try:
            name = request.form.get('name', '').strip()
            brand = request.form.get('brand', '').strip()
            if not name or not brand:
                flash('❌ Nama dan Brand wajib diisi!', 'error')
                return render_template('admin/product_form.html')

            slug = request.form.get('slug', '').strip()
            if not slug:
                slug = re.sub(r'[^a-z0-9-]', '', name.lower().replace(' ', '-'))

            model = request.form.get('model', '').strip()
            category = request.form.get('category', '').strip()
            subcategory = request.form.get('subcategory', '').strip()
            tags = request.form.get('tags', '').strip()

            price = _safe_price(request.form.get('price')) or 0
            compare_at_price = _safe_price(request.form.get('compare_at_price'))
            cost_price = _safe_price(request.form.get('cost_price'))

            sku = request.form.get('sku', '').strip()
            barcode = request.form.get('barcode', '').strip()
            stock = _safe_int(request.form.get('stock'), 1)
            condition = request.form.get('condition', 'Like New')
            low_stock_alert = _safe_int(request.form.get('low_stock_alert'), 5)

            short_description = request.form.get('short_description', '').strip()
            description = request.form.get('description', '').strip()

            meta_title = request.form.get('meta_title', '').strip()
            meta_description = request.form.get('meta_description', '').strip()
            meta_keywords = request.form.get('meta_keywords', '').strip()

            weight = _safe_float(request.form.get('weight')) or 0.0
            width = _safe_float(request.form.get('width')) or 0.0
            length = _safe_float(request.form.get('length')) or 0.0
            height = _safe_float(request.form.get('height')) or 0.0

            is_featured = 'is_featured' in request.form
            is_active = 'is_active' in request.form

            # ===== TAMBAHKAN INI =====
            status = request.form.get('status', 'published').strip()
            specs_raw = request.form.get('specifications', '').strip()
            # =========================

            image_filename = save_uploaded_image(request.files.get('image'), 'main_')
            image_filename2 = save_uploaded_image(request.files.get('image2'), 'img2_')
            image_filename3 = save_uploaded_image(request.files.get('image3'), 'img3_')
            image_filename4 = save_uploaded_image(request.files.get('image4'), 'img4_')

            product = Product(
                name=name, slug=slug, brand=brand, model=model,
                category=category, subcategory=subcategory, tags=tags,
                price=price, compare_at_price=compare_at_price, cost_price=cost_price,
                sku=sku, barcode=barcode, stock=stock, condition=condition,
                low_stock_alert=low_stock_alert,
                short_description=short_description, description=description,
                meta_title=meta_title, meta_description=meta_description, meta_keywords=meta_keywords,
                weight=weight, width=width, length=length, height=height,
                is_featured=is_featured, is_active=is_active,
                image_filename=image_filename, image_filename2=image_filename2,
                image_filename3=image_filename3, image_filename4=image_filename4,
                # ===== TAMBAHKAN INI =====
                status=status,
                specifications=_safe_specs(specs_raw),
                # =========================
            )
            db.session.add(product)
            db.session.commit()
            flash(f'✅ Produk "{name}" berhasil ditambahkan!', 'success')
            return redirect(url_for('admin_products'))
        except Exception as e:
            db.session.rollback()
            logger.error(f"Error adding product: {e}", exc_info=True)
            flash(f'❌ Error menambahkan produk: {str(e)}', 'error')
            return render_template('admin/product_form.html')

    return render_template('admin/product_form.html')

@app.route('/admin/product/edit/<int:id>', methods=['GET','POST'])
@admin_required
def admin_product_edit(id):
    product = Product.query.get_or_404(id)
    if request.method == 'POST':
        product.name = request.form.get('name', product.name)
        product.slug = request.form.get('slug', '').strip() or None
        product.brand = request.form.get('brand', product.brand)
        product.model = request.form.get('model', product.model)
        product.sku = request.form.get('sku', product.sku)
        product.barcode = request.form.get('barcode', product.barcode)
        product.category = request.form.get('category', product.category)
        product.subcategory = request.form.get('subcategory', product.subcategory)
        product.tags = request.form.get('tags', product.tags)

        price_new = _safe_price(request.form.get('price'))
        if price_new is not None:
            product.price = price_new
        product.compare_at_price = _safe_price(request.form.get('compare_at_price'))
        product.cost_price = _safe_price(request.form.get('cost_price'))

        product.condition = request.form.get('condition', product.condition)
        product.low_stock_alert = _safe_int(request.form.get('low_stock_alert'), default=product.low_stock_alert)
        product.stock = _safe_int(request.form.get('stock'), default=product.stock)

        product.short_description = request.form.get('short_description', product.short_description)
        product.description = request.form.get('description', product.description)

        specs_raw = request.form.get('specifications', '').strip()
        product.specifications = _safe_specs(specs_raw)

        product.weight = _safe_float(request.form.get('weight'))
        product.width = _safe_float(request.form.get('width'))
        product.length = _safe_float(request.form.get('length'))
        product.height = _safe_float(request.form.get('height'))

        product.is_featured = 'is_featured' in request.form
        product.is_active = 'is_active' in request.form

        # ===== TAMBAHKAN: UPDATE STATUS =====
        product.status = request.form.get('status', product.status)
        # =====================================

        # ===== TAMBAHKAN: HANDLE PENGHAPUSAN GAMBAR (sebelum upload baru) =====
        removal_map = {
            'remove_image_main': 'image_filename',
            'remove_image_1': 'image_filename2',
            'remove_image_2': 'image_filename3',
            'remove_image_3': 'image_filename4',
        }
        for flag_field, db_field in removal_map.items():
            if request.form.get(flag_field) == '1':
                old_filename = getattr(product, db_field)
                if old_filename:
                    old_path = os.path.join(UPLOAD_PRODUCTS, old_filename)
                    if os.path.exists(old_path):
                        os.remove(old_path)
                setattr(product, db_field, None)
        # =======================================================================

        for field, file_field, prefix in [
            ('image_filename', 'image', 'main_'),
            ('image_filename2', 'image2', 'img2_'),
            ('image_filename3', 'image3', 'img3_'),
            ('image_filename4', 'image4', 'img4_')
        ]:
            file = request.files.get(file_field)
            if file and file.filename:
                old = getattr(product, field)
                if old:
                    old_path = os.path.join(UPLOAD_PRODUCTS, old)
                    if os.path.exists(old_path):
                        os.remove(old_path)
                setattr(product, field, save_uploaded_image(file, prefix))

        product.meta_title = request.form.get('meta_title', product.meta_title)
        product.meta_description = request.form.get('meta_description', product.meta_description)
        product.meta_keywords = request.form.get('meta_keywords', product.meta_keywords)

        try:
            db.session.commit()
        except Exception as e:
            db.session.rollback()
            logger.error(f"Gagal update produk id={id}: {e}")
            flash(f'Gagal menyimpan perubahan: {e}', 'error')
            return render_template('admin/product_form.html', product=product)

        send_product_notification(product, 'update')
        flash('Produk berhasil diperbarui.', 'success')
        return redirect(url_for('admin_products'))

    return render_template('admin/product_form.html', product=product)

@app.route('/admin/product/delete/<int:id>', methods=['POST'])
@admin_required
def admin_product_delete(id):
    product = Product.query.get_or_404(id)
    try:
        db.session.execute(
            text("UPDATE order_items SET product_id = NULL WHERE product_id = :pid"),
            {'pid': id}
        )
        for f in ['image_filename', 'image_filename2', 'image_filename3', 'image_filename4']:
            img = getattr(product, f, None)
            if img:
                path = os.path.join(UPLOAD_PRODUCTS, img)
                if os.path.exists(path):
                    os.remove(path)
        db.session.delete(product)
        db.session.commit()
        return jsonify({
            'success': True,
            'message': f'Produk "{product.name}" berhasil dihapus permanen.'
        })
    except Exception as e:
        db.session.rollback()
        logger.error(f"Error deleting product {id}: {e}", exc_info=True)
        return jsonify({
            'success': False,
            'message': f'Error: {str(e)}'
        }), 500

@app.route('/admin/product/<int:id>/stock', methods=['POST'])
@admin_required
def admin_product_quick_stock(id):
    product = Product.query.get_or_404(id)
    data = request.get_json()
    if not data or 'stock' not in data:
        return jsonify({'success': False, 'message': 'Stock value required'}), 400
    product.stock = max(0, int(data['stock']))
    db.session.commit()
    return jsonify({'success': True, 'message': 'Stock updated'})

@app.route('/admin/bulk-toggle-featured', methods=['POST'])
@admin_required
def admin_bulk_toggle_featured():
    data = request.get_json()
    ids = data.get('product_ids', [])
    featured = data.get('featured', True)
    updated = Product.query.filter(Product.id.in_(ids)).update(
        {Product.is_featured: featured}, synchronize_session=False
    )
    db.session.commit()
    return jsonify({'success': True, 'updated': updated})

@app.route('/admin/bulk-delete-products', methods=['POST'])
@admin_required
def admin_bulk_delete_products():
    data = request.get_json()
    ids = data.get('product_ids', [])
    products = Product.query.filter(Product.id.in_(ids)).all()
    for p in products:
        db.session.execute(
            text("UPDATE order_items SET product_id = NULL WHERE product_id = :pid"),
            {'pid': p.id}
        )
        for f in ['image_filename', 'image_filename2', 'image_filename3', 'image_filename4']:
            img = getattr(p, f)
            if img:
                path = os.path.join(UPLOAD_PRODUCTS, img)
                if os.path.exists(path): os.remove(path)
        db.session.delete(p)
    db.session.commit()
    return jsonify({'success': True, 'deleted': len(products)})

@app.route('/admin/products/export')
@admin_required
def admin_products_export():
    ids = request.args.get('ids', '')
    if ids:
        ids = [int(i) for i in ids.split(',') if i.strip().isdigit()]
        products = Product.query.filter(Product.id.in_(ids)).all()
    else:
        products = Product.query.order_by(Product.created_at.desc()).all()
    output = StringIO()
    writer = csv.writer(output)
    writer.writerow(['ID', 'Name', 'Brand', 'Model', 'Price', 'Stock', 'Condition'])
    for p in products:
        writer.writerow([p.id, p.name, p.brand, p.model, p.price, p.stock, p.condition])
    response = make_response(output.getvalue())
    response.headers['Content-Disposition'] = 'attachment; filename=products.csv'
    response.headers['Content-type'] = 'text/csv'
    return response

# --- Admin Orders ---
@app.route('/admin/orders')
@admin_required
def admin_orders():
    q = request.args.get('q', '').strip()
    status = request.args.get('status', '').strip()
    priority = request.args.get('priority', '').strip()
    date_from = request.args.get('date_from', '').strip()
    date_to = request.args.get('date_to', '').strip()
    overdue_only = request.args.get('overdue_only')
    page = request.args.get('page', 1, type=int)
    per_page = min(max(request.args.get('per_page', 20, type=int), 1), 100)
    query = Order.query
    if q:
        query = query.filter(or_(Order.order_number.ilike(f'%{q}%'),
                                 Order.customer_name.ilike(f'%{q}%'),
                                 Order.customer_phone.ilike(f'%{q}%')))
    if status:
        query = query.filter(Order.status == status)
    if priority:
        query = query.filter(Order.priority == priority)
    if date_from:
        try:
            d = datetime.strptime(date_from, '%Y-%m-%d')
            query = query.filter(Order.created_at >= d)
        except ValueError:
            pass
    if date_to:
        try:
            d = datetime.strptime(date_to, '%Y-%m-%d')
            query = query.filter(Order.created_at <= d + timedelta(days=1))
        except ValueError:
            pass
    if overdue_only:
        now_utc = datetime.utcnow()
        query = query.filter(Order.sla_deadline.isnot(None), Order.sla_deadline < now_utc,
                             Order.status.notin_(['Completed', 'Returned', 'Cancelled']))
    orders_pag = query.order_by(Order.created_at.desc()).paginate(page=page, per_page=per_page, error_out=False)
    return render_template('admin/orders.html', orders=orders_pag.items, pagination=orders_pag)

@app.route('/admin/order/<int:order_id>')
@admin_required
def admin_order_detail(order_id):
    order = Order.query.get_or_404(order_id)
    status_flow = ['Pending', 'Paid', 'Processing', 'Ready_to_Ship', 'Shipped', 'Out_for_Delivery', 'Received', 'Completed']
    terminal_statuses = ['Cancelled', 'Returned']
    return render_template('admin/order_detail.html', order=order,
                           status_flow=status_flow, terminal_statuses=terminal_statuses)

@app.route('/admin/order/<int:order_id>/status', methods=['POST'])
@admin_required
def admin_update_order_status(order_id):
    order = Order.query.get_or_404(order_id)
    data = request.get_json()
    new_status = data.get('status') if data else None
    valid = ['Pending', 'Paid', 'Processing', 'Ready_to_Ship', 'Shipped', 'Out_for_Delivery', 'Received', 'Completed', 'Cancelled', 'Returned']
    if new_status not in valid:
        return jsonify({'success': False, 'message': 'Invalid status'}), 400
    old = order.status
    order.status = new_status
    order.updated_at = datetime.utcnow()

    # Restoration of product stock when status becomes Cancelled or Returned
    if new_status in ['Cancelled', 'Returned'] and old not in ['Cancelled', 'Returned']:
        for item in order.items:
            if item.product and item.product.stock is not None:
                item.product.stock += item.quantity

    try:
        sync_shipping_tracking(order)
    except Exception as e:
        app.logger.error(f"Error syncing shipping tracking: {e}")
    db.session.commit()
    if new_status != old:
        NotificationService.notify_status_update(order, old, new_status)
    return jsonify({'success': True, 'message': f'Status changed to {new_status}'})

@app.route('/admin/order/<int:order_id>/proses')
@admin_required
def admin_proses_order(order_id):
    order = Order.query.get_or_404(order_id)
    return render_template('admin/proses_order.html', order=order)

@app.route('/admin/order/<int:order_id>/konfirmasi-pembayaran')
@admin_required
def admin_konfirmasi_pembayaran(order_id):
    order = Order.query.get_or_404(order_id)
    return render_template('admin/konfirmasi_pembayaran.html', order=order)

@app.route('/admin/order/<int:order_id>/cancel', methods=['POST'])
@admin_required
def admin_order_cancel(order_id):
    order = Order.query.get_or_404(order_id)
    data = request.get_json()
    reason = data.get('reason', '') if data else ''
    if order.status in ['Cancelled', 'Completed', 'Returned']:
        return jsonify({'success': False, 'message': 'Cannot cancel'}), 400
    order.status = 'Cancelled'
    if hasattr(order, 'cancel_reason'):
        order.cancel_reason = reason
    order.updated_at = datetime.utcnow()
    # Kembalikan stok untuk tiap item pesanan (produk mungkin sudah dihapus
    # -- product_id nullable via ondelete='SET NULL' -- jadi item.product
    # bisa None; stock None berarti produk itu memang tidak melacak stok).
    for item in order.items:
        if item.product and item.product.stock is not None:
            item.product.stock += item.quantity
    db.session.commit()
    if NOTIVGO and order.payment_provider == 'notivgo':
        try:
            NOTIVGO.cancel_remote(order)
        except Exception as e:
            app.logger.error(f"NotivGo remote cancel gagal: {e}")
    NotificationService.notify_status_update(order, new_status='Cancelled')
    return jsonify({'success': True, 'message': 'Order cancelled'})

@app.route('/admin/order/<int:order_id>/priority', methods=['POST'])
@admin_required
def admin_order_priority(order_id):
    order = Order.query.get_or_404(order_id)
    data = request.get_json()
    priority = data.get('priority', 'normal') if data else 'normal'
    valid = ['urgent', 'high', 'normal', 'low']
    if priority not in valid:
        return jsonify({'success': False, 'message': 'Invalid priority'}), 400
    order.priority = priority
    order.updated_at = datetime.utcnow()
    db.session.commit()
    return jsonify({'success': True, 'message': f'Priority set to {priority}'})

@app.route('/admin/orders/bulk-update-status', methods=['POST'])
@admin_required
def admin_bulk_update_status():
    data = request.get_json()
    ids = data.get('order_ids', [])
    new_status = data.get('status')
    if not ids or not new_status:
        return jsonify({'success': False, 'message': 'Missing parameters'}), 400
    valid = ['Pending', 'Paid', 'Processing', 'Ready_to_Ship', 'Shipped', 'Out_for_Delivery', 'Received', 'Completed', 'Cancelled']
    if new_status not in valid:
        return jsonify({'success': False, 'message': 'Invalid status'}), 400
    updated = 0
    for oid in ids:
        order = Order.query.get(int(oid))
        if order and order.status != new_status:
            old = order.status
            order.status = new_status
            order.updated_at = datetime.utcnow()
            if new_status in ['Cancelled', 'Returned'] and old not in ['Cancelled', 'Returned']:
                for item in order.items:
                    if item.product and item.product.stock is not None:
                        item.product.stock += item.quantity
            try:
                sync_shipping_tracking(order)
            except Exception as e:
                app.logger.error(f"Error syncing shipping tracking in bulk: {e}")
            updated += 1
    db.session.commit()
    return jsonify({'success': True, 'updated': updated})

@app.route('/admin/orders/bulk-invoice')
@admin_required
def admin_bulk_invoice():
    ids_str = request.args.get('ids', '')
    if not ids_str:
        return "No orders selected", 400
    ids = [int(i) for i in ids_str.split(',') if i.strip().isdigit()]
    orders = Order.query.filter(Order.id.in_(ids)).all()
    return render_template('admin/bulk_invoice.html', orders=orders)

@app.route('/admin/export-orders')
@admin_required
def admin_export_orders():
    q = request.args.get('q', '').strip()
    status = request.args.get('status', '').strip()
    query = Order.query
    if q:
        query = query.filter(or_(Order.order_number.ilike(f'%{q}%'),
                                 Order.customer_name.ilike(f'%{q}%'),
                                 Order.customer_phone.ilike(f'%{q}%')))
    if status:
        query = query.filter(Order.status == status)
    orders = query.order_by(Order.created_at.desc()).all()
    output = StringIO()
    writer = csv.writer(output)
    writer.writerow(['Order Number', 'Customer', 'Phone', 'Status', 'Total', 'Date'])
    for o in orders:
        writer.writerow([o.order_number, o.customer_name, o.customer_phone, o.status, o.total,
                         o.created_at.strftime('%Y-%m-%d %H:%M')])
    response = make_response(output.getvalue())
    response.headers['Content-Disposition'] = 'attachment; filename=orders_export.csv'
    response.headers['Content-type'] = 'text/csv'
    return response

@app.route('/admin/order/update-tracking', methods=['POST'])
@admin_required
def admin_update_tracking():
    data = request.get_json()
    order_id = data.get('order_id')
    carrier = data.get('carrier', '').strip()
    tracking_number = data.get('tracking_number', '').strip()
    estimated_delivery = data.get('estimated_delivery', '')
    shipping_cost = data.get('shipping_cost', 0)
    if not order_id or not carrier or not tracking_number:
        return jsonify({'success': False, 'message': 'Data tidak lengkap'}), 400
    order = Order.query.get(int(order_id))
    if not order:
        return jsonify({'success': False, 'message': 'Order tidak ditemukan'}), 404
    order.carrier_name = carrier
    order.tracking_number = tracking_number
    if estimated_delivery:
        try:
            order.estimated_delivery = datetime.strptime(estimated_delivery, '%Y-%m-%d')
        except (ValueError, TypeError):
            return jsonify({'success': False, 'message': 'Format tanggal salah. Gunakan format YYYY-MM-DD'}), 400
    if shipping_cost is not None:
        try:
            order.shipping_cost = max(0, int(round(float(shipping_cost))))
        except (ValueError, TypeError):
            return jsonify({'success': False, 'message': 'Biaya pengiriman tidak valid'}), 400
    order.status = 'Shipped'
    order.shipped_at = datetime.utcnow()
    try:
        sync_shipping_tracking(order)
    except Exception as e:
        app.logger.error(f"Error syncing shipping tracking: {e}")
    db.session.commit()
    NotificationService.notify_status_update(order, new_status='Shipped')
    return jsonify({'success': True, 'message': 'Tracking berhasil disimpan'})

@app.route('/admin/order/process-return', methods=['POST'])
@admin_required
def admin_process_return():
    data = request.get_json()
    order_id = data.get('order_id')
    reason = data.get('reason', '')
    resolution = data.get('resolution', 'refund')
    notes = data.get('notes', '')
    order = Order.query.get(int(order_id))
    if not order:
        return jsonify({'success': False, 'message': 'Order tidak ditemukan'}), 404
    order.status = 'Returned'
    order.return_reason = reason
    order.return_resolution = resolution
    order.return_notes = notes
    order.updated_at = datetime.utcnow()
    db.session.commit()
    return jsonify({'success': True, 'message': 'Return berhasil diproses'})

# --- Redemption ---
@app.route('/admin/redemption-letters')
@admin_required
def admin_redemption_letters():
    eligible_statuses = ['Paid', 'Processing', 'Ready_to_Ship', 'Shipped', 'Out_for_Delivery', 'Completed']
    orders = Order.query.filter(Order.status.in_(eligible_statuses)) \
                     .order_by(Order.updated_at.desc()).all()
    return render_template('admin/redemption_letters.html', orders=orders)

@app.route('/admin/redemption-letter/<int:order_id>')
@admin_required
def redemption_letter(order_id):
    order = Order.query.get_or_404(order_id)
    product_name = order.items[0].name if order.items else 'Barang Gadai'
    officer = get_setting('officer_name', 'Admin')
    redemption_amount = order.total
    receipt_number = order.order_number
    redemption_date = order.paid_at.strftime('%d %B %Y') if order.paid_at else datetime.now().strftime('%d %B %Y')
    letter_date = datetime.now().strftime('%d %B %Y')

    specs = ""
    if order.items:
        first = order.items[0]
        parts = [p for p in [first.brand, first.model, first.condition] if p]
        specs = " / ".join(parts)

    return render_template('admin/redemption_letter.html',
                           order_number=order.order_number,
                           customer_name=order.customer_name,
                           customer_address=order.shipping_address,
                           product_name=product_name,
                           product_specs=specs,
                           receipt_number=receipt_number,
                           redemption_date=redemption_date,
                           redemption_amount=redemption_amount,
                           redemption_year=datetime.now().year,
                           letter_date=letter_date,
                           officer_name=officer,
                           site_name=get_site_name(),
                           format_price=format_price)

@app.route('/admin/redemption-letter/<int:order_id>/print')
@admin_required
def admin_redemption_letter_print(order_id):
    order = Order.query.get_or_404(order_id)
    product_name = order.items[0].name if order.items else 'Barang Gadai'
    officer = get_setting('officer_name', 'Admin')
    redemption_amount = order.total
    receipt_number = order.order_number
    redemption_date = order.paid_at.strftime('%d %B %Y') if order.paid_at else datetime.now().strftime('%d %B %Y')
    letter_date = datetime.now().strftime('%d %B %Y')

    specs = ""
    if order.items:
        first = order.items[0]
        parts = [p for p in [first.brand, first.model, first.condition] if p]
        specs = " / ".join(parts)

    return render_template(
        'admin/redemption_letter.html',
        order_number=order.order_number,
        customer_name=order.customer_name,
        customer_address=order.shipping_address,
        product_name=product_name,
        product_specs=specs,
        receipt_number=receipt_number,
        redemption_date=redemption_date,
        redemption_amount=redemption_amount,
        redemption_year=datetime.now().year,
        letter_date=letter_date,
        officer_name=officer,
        site_name=get_site_name(),
        format_price=format_price,
        auto_print=True
    )

# --- Hero Slides ---
@app.route('/admin/hero')
@admin_required
def admin_hero():
    heroes = HeroSlide.query.order_by(HeroSlide.sort_order).all()
    return render_template('admin/hero_slides.html', slides=heroes)

@app.route('/admin/hero/new', methods=['GET','POST'])
@admin_required
def admin_hero_new():
    if request.method == 'POST':
        filename = save_uploaded_image(request.files.get('image'), 'hero_', 'hero')
        hero = HeroSlide(
            title=request.form.get('title'), subtitle=request.form.get('subtitle'),
            button_text=request.form.get('button_text'), button_url=request.form.get('button_url'),
            is_active='is_active' in request.form, sort_order=_safe_int(request.form.get('order'), 0),
            image_filename=filename
        )
        db.session.add(hero)
        db.session.commit()
        flash('Hero berhasil ditambahkan', 'success')
        return redirect(url_for('admin_hero'))
    return render_template('admin/hero_form.html', hero=None)

@app.route('/admin/hero/edit/<int:id>', methods=['GET','POST'])
@admin_required
def admin_hero_edit(id):
    hero = HeroSlide.query.get_or_404(id)
    if request.method == 'POST':
        hero.title = request.form.get('title')
        hero.subtitle = request.form.get('subtitle')
        hero.button_text = request.form.get('button_text')
        hero.button_url = request.form.get('button_url')
        hero.is_active = 'is_active' in request.form
        hero.sort_order = _safe_int(request.form.get('order'), hero.sort_order)
        file = request.files.get('image')
        if file and file.filename:
            old = hero.image_filename
            if old:
                old_path = os.path.join(UPLOAD_HERO, old)
                if os.path.exists(old_path): os.remove(old_path)
            hero.image_filename = save_uploaded_image(file, 'hero_', 'hero')
        db.session.commit()
        flash('Hero berhasil diperbarui', 'success')
        return redirect(url_for('admin_hero'))
    return render_template('admin/hero_form.html', hero=hero)

@app.route('/admin/hero/delete/<int:id>')
@admin_required
def admin_hero_delete(id):
    hero = HeroSlide.query.get_or_404(id)
    if hero.image_filename:
        path = os.path.join(UPLOAD_HERO, hero.image_filename)
        if os.path.exists(path): os.remove(path)
    db.session.delete(hero)
    db.session.commit()
    flash('Hero dihapus', 'success')
    return redirect(url_for('admin_hero'))

# --- Testimonials ---
@app.route('/admin/testimonials')
@admin_required
def admin_testimonials():
    testimonials = Testimonial.query.order_by(Testimonial.sort_order).all()
    return render_template('admin/testimonials.html', testimonials=testimonials)

@app.route('/admin/testimonial/new', methods=['GET','POST'])
@admin_required
def admin_testimonial_new():
    if request.method == 'POST':
        filename = save_uploaded_image(request.files.get('avatar'), 'testimonial_', 'testimonials')
        testimonial = Testimonial(
            name=request.form.get('name'), location=request.form.get('location'),
            text=request.form.get('text'), rating=_safe_int(request.form.get('rating'), 5),
            is_active='is_active' in request.form, sort_order=_safe_int(request.form.get('order'), 0),
            image_filename=filename
        )
        db.session.add(testimonial)
        db.session.commit()
        flash('Testimoni berhasil ditambahkan', 'success')
        return redirect(url_for('admin_testimonials'))
    return render_template('admin/testimonial_form.html', testimonial=None)

@app.route('/admin/testimonial/edit/<int:id>', methods=['GET','POST'])
@admin_required
def admin_testimonial_edit(id):
    testimonial = Testimonial.query.get_or_404(id)
    if request.method == 'POST':
        testimonial.name = request.form.get('name')
        testimonial.location = request.form.get('location')
        testimonial.text = request.form.get('text')
        testimonial.rating = _safe_int(request.form.get('rating'), testimonial.rating)
        testimonial.is_active = 'is_active' in request.form
        testimonial.sort_order = _safe_int(request.form.get('order'), testimonial.sort_order)
        file = request.files.get('avatar')
        if file and file.filename:
            old = testimonial.image_filename
            if old:
                old_path = os.path.join(UPLOAD_TESTIMONIAL, old)
                if os.path.exists(old_path): os.remove(old_path)
            testimonial.image_filename = save_uploaded_image(file, 'testimonial_', 'testimonials')
        db.session.commit()
        flash('Testimoni berhasil diperbarui', 'success')
        return redirect(url_for('admin_testimonials'))
    return render_template('admin/testimonial_form.html', testimonial=testimonial)

@app.route('/admin/testimonial/delete/<int:id>')
@admin_required
def admin_testimonial_delete(id):
    testimonial = Testimonial.query.get_or_404(id)
    if testimonial.image_filename:
        path = os.path.join(UPLOAD_TESTIMONIAL, testimonial.image_filename)
        if os.path.exists(path): os.remove(path)
    db.session.delete(testimonial)
    db.session.commit()
    flash('Testimoni dihapus', 'success')
    return redirect(url_for('admin_testimonials'))

# --- Admin Users ---
@app.route('/admin/users')
@admin_required
def admin_users():
    page = request.args.get('page', 1, type=int)
    per_page = 20
    q = request.args.get('q', '').strip()
    query = User.query
    if q:
        query = query.filter(or_(User.email.ilike(f'%{q}%'), User.full_name.ilike(f'%{q}%'),
                                 User.username.ilike(f'%{q}%'), User.phone.ilike(f'%{q}%')))
    pagination = query.order_by(User.created_at.desc()).paginate(page=page, per_page=per_page, error_out=False)
    return render_template('admin/users.html', users=pagination.items, pagination=pagination, search_q=q)

@app.route('/admin/user/<int:user_id>/edit', methods=['GET','POST'])
@admin_required
def admin_user_edit(user_id):
    user = User.query.get_or_404(user_id)
    if request.method == 'POST':
        user.full_name = request.form.get('full_name', user.full_name)
        user.phone = request.form.get('phone', user.phone)
        user.address = request.form.get('address', user.address)
        user.username = request.form.get('username') or None
        new_password = request.form.get('new_password')
        if new_password and len(new_password) >= 8:
            user.set_password(new_password)
        elif new_password:
            flash('Kata sandi baru minimal 8 karakter.', 'error')
        db.session.commit()
        flash('Data pengguna berhasil diperbarui.', 'success')
        return redirect(url_for('admin_users'))
    return render_template('admin/user_edit.html', user=user)

@app.route('/admin/user/<int:user_id>/delete', methods=['POST'])
@admin_required
def admin_user_delete(user_id):
    user = User.query.get_or_404(user_id)
    if user.id == current_user.id:
        return jsonify({'success': False, 'message': 'Tidak dapat menghapus akun sendiri'}), 400

    # Force-delete eksplisit: admin secara sadar minta hapus TERMASUK seluruh
    # riwayat cicilan (aplikasi, jadwal, pembayaran). Default-nya tetap ditolak
    # (lihat blok di bawah) supaya tidak ada yang kehapus tanpa sengaja.
    force = request.args.get('force', '').lower() in ('1', 'true', 'yes')
    if not force:
        force = bool((request.get_json(silent=True) or {}).get('force'))

    applications = InstallmentApplication.query.filter_by(user_id=user.id).all()

    # installment_applications.user_id juga NOT NULL dan relasi User.installment_applications
    # tidak punya cascade delete, jadi masalahnya sama persis dengan KYC di bawah. Secara
    # default ini diblokir (bukan auto-cascade) karena tabel ini menyimpan riwayat
    # cicilan/pembayaran finansial yang tidak boleh ikut terhapus diam-diam.
    if applications and not force:
        return jsonify({
            'success': False,
            'message': 'Pengguna memiliki riwayat pengajuan cicilan dan tidak dapat dihapus. '
                       'Nonaktifkan akun sebagai gantinya, atau kirim ulang dengan force=true '
                       'jika ingin menghapus permanen beserta seluruh riwayat cicilan.',
            'has_installment_history': True
        }), 400

    if applications:
        app_ids = [a.id for a in applications]

        # Order.credit_application_id nullable, tapi karena aplikasi di bawah
        # dihapus lewat bulk delete (bukan db.session.delete per-objek), relasi
        # ORM tidak akan otomatis meng-null-kan-nya -- jadi dilepas manual dulu
        # supaya tidak jadi foreign key menggantung ke baris yang sudah dihapus.
        Order.query.filter(Order.credit_application_id.in_(app_ids)).update(
            {'credit_application_id': None}, synchronize_session=False
        )

        # installment_payments.application_id & schedule_id NOT NULL dan tidak
        # ada cascade delete sama sekali -- harus dihapus manual paling dulu.
        InstallmentPayment.query.filter(InstallmentPayment.application_id.in_(app_ids)).delete(synchronize_session=False)

        # installment_schedules.application_id NOT NULL. Relasi ORM-nya sudah
        # cascade='all, delete-orphan', tapi cascade itu hanya berlaku kalau
        # application dihapus lewat db.session.delete() per-objek -- karena di
        # sini pakai bulk delete, cascade-nya tidak jalan, jadi dihapus manual.
        InstallmentSchedule.query.filter(InstallmentSchedule.application_id.in_(app_ids)).delete(synchronize_session=False)

        InstallmentApplication.query.filter(InstallmentApplication.id.in_(app_ids)).delete(synchronize_session=False)

    # kyc_verification_sessions.user_id bersifat NOT NULL. Tanpa baris ini,
    # SQLAlchemy akan mencoba UPDATE ... SET user_id = NULL pada sesi KYC milik
    # user ini sebelum menghapus user (perilaku default relasi tanpa cascade
    # delete), yang memicu IntegrityError. Jadi sesi KYC dihapus lebih dulu.
    KYCVerificationSession.query.filter_by(user_id=user.id).delete(synchronize_session=False)

    db.session.delete(user)
    try:
        db.session.commit()
    except IntegrityError:
        db.session.rollback()
        return jsonify({
            'success': False,
            'message': 'Pengguna tidak dapat dihapus karena masih memiliki data terkait (pesanan, cicilan, dll).'
        }), 400

    if applications:
        return jsonify({
            'success': True,
            'message': f'Pengguna berhasil dihapus beserta {len(applications)} riwayat cicilan (aplikasi, jadwal, dan pembayaran terkait).'
        })
    return jsonify({'success': True, 'message': 'Pengguna berhasil dihapus'})

@app.route('/admin/users/export')
@admin_required
def admin_users_export():
    q = request.args.get('q', '').strip()
    query = User.query
    if q:
        query = query.filter(or_(User.email.ilike(f'%{q}%'), User.full_name.ilike(f'%{q}%'),
                                 User.username.ilike(f'%{q}%'), User.phone.ilike(f'%{q}%')))
    users = query.order_by(User.created_at.desc()).all()
    output = StringIO()
    writer = csv.writer(output)
    writer.writerow(['ID', 'Nama Lengkap', 'Email', 'Username', 'Telepon', 'Status', 'Tanggal Bergabung'])
    for u in users:
        status = 'Aktif' if getattr(u, 'is_active', True) else 'Nonaktif'
        writer.writerow([u.id, u.full_name, u.email, u.username or '', u.phone or '', status, u.created_at.strftime('%Y-%m-%d')])
    response = make_response(output.getvalue())
    response.headers['Content-Disposition'] = 'attachment; filename=users_export.csv'
    response.headers['Content-type'] = 'text/csv'
    return response

@app.route('/admin/user/<int:user_id>/reset-password', methods=['POST'])
@admin_required
def admin_user_reset_password(user_id):
    user = User.query.get_or_404(user_id)
    data = request.get_json()
    new_password = data.get('password', '').strip() if data else ''
    if not new_password:
        new_password = ''.join(secrets.choice(string.ascii_letters + string.digits) for _ in range(10))
    user.set_password(new_password)
    db.session.commit()
    return jsonify({'success': True, 'message': f'Password direset. Baru: {new_password}'})

@app.route('/admin/user/<int:user_id>/toggle-status', methods=['POST'])
@admin_required
def admin_user_toggle_status(user_id):
    user = User.query.get_or_404(user_id)
    user.is_active = not user.is_active
    db.session.commit()
    status_text = 'diaktifkan' if user.is_active else 'dinonaktifkan'
    return jsonify({'success': True, 'message': f'Akun berhasil {status_text}'})

@app.route('/api/admin/user/<int:user_id>/orders')
@admin_required
def admin_user_orders_api(user_id):
    user = User.query.get_or_404(user_id)
    orders = Order.query.filter_by(user_id=user.id).order_by(Order.created_at.desc()).all()
    result = []
    for o in orders:
        result.append({
            'id': o.id,
            'order_number': o.order_number,
            'status': o.status,
            'total': o.total,
            'total_formatted': format_price(o.total),
            'items_count': len(o.items),
            'created_at': o.created_at.strftime('%d %b %Y %H:%M'),
            'payment_method': o.payment_method or '-'
        })
    return jsonify({'success': True, 'orders': result})

@app.route('/admin/check-username')
@admin_required
def admin_check_username():
    username = request.args.get('username', '').strip()
    exclude_id = request.args.get('exclude_id', type=int)
    if not username or len(username) < 3:
        return jsonify({'available': False, 'message': 'Username minimal 3 karakter'})
    query = User.query.filter_by(username=username)
    if exclude_id:
        query = query.filter(User.id != exclude_id)
    existing = query.first()
    return jsonify({
        'available': existing is None,
        'message': 'Username tersedia' if existing is None else 'Username sudah digunakan'
    })

# --- Admin Settings ---
@app.route('/admin/settings', methods=['GET','POST'])
@admin_required
def admin_settings():
    if request.method == 'POST':
        def update(key, val):
            s = Setting.query.filter_by(key=key).first()
            if s:
                s.value = val
            else:
                db.session.add(Setting(key=key, value=val))
        if request.is_json:
            data = request.get_json()
            for k in data:
                update(k, str(data[k]) if not isinstance(data[k], bool) else ('true' if data[k] else 'false'))
            db.session.commit()
            return jsonify({'success': True, 'message': 'Pengaturan berhasil disimpan'})

        if request.form.get('tab') == 'ai':
            ai_keys = [
                'openrouter_base_url', 'openrouter_api_key', 'openrouter_model',
                'openrouter_site_url', 'openrouter_app_name', 'ai_system_prompt'
            ]
            for k in ai_keys:
                update(k, request.form.get(k, ''))
            db.session.commit()
            flash('Konfigurasi AI berhasil disimpan', 'success')
            return redirect(url_for('admin_ai_config'))

        try:
            active_settings = Setting.query.filter(
                or_(Setting.key.like('courier_%_active'), Setting.key.like('svc_%_active'))
            ).all()
            for s in active_settings:
                s.value = '0'
        except Exception as e:
            db.session.rollback()
            logger.error(f"Error resetting active couriers/services: {e}")

        skip_keys = {'tab', 'csrf_token', 'logo_remove'}
        for k in request.form:
            if k in skip_keys:
                continue
            update(k, request.form.get(k, ''))

        if request.form.get('tab') == 'payment':
            for chk in ('payment_bank_transfer_enabled', 'payment_qris_enabled', 'payment_cod_enabled', 'payment_notivgo_enabled'):
                update(chk, '1' if request.form.get(chk) in ('1', 'true', 'on') else '0')

        active_couriers = []
        for k in request.form:
            if k.startswith('courier_') and k.endswith('_active'):
                if request.form.get(k):
                    courier_key = k.replace('courier_', '').replace('_active', '')
                    active_couriers.append(courier_key.upper())
        update('shipping_couriers', ','.join(active_couriers))

        fsm = request.form.get('free_shipping_min', '0')
        if fsm:
            update('shipping_free_threshold', fsm)

        if request.form.get('logo_remove') == '1':
            old = get_setting('logo_url')
            if old:
                old_path = os.path.join(UPLOAD_LOGO, old)
                if os.path.exists(old_path):
                    os.remove(old_path)
            update('logo_url', '')
        logo = request.files.get('logo')
        if logo and logo.filename:
            fname = save_uploaded_image(logo, 'logo_', 'logo')
            if fname:
                old = get_setting('logo_url')
                if old:
                    old_path = os.path.join(UPLOAD_LOGO, old)
                    if os.path.exists(old_path): os.remove(old_path)
                update('logo_url', fname)
        qris = request.files.get('qris_code')
        if qris and qris.filename:
            fname = save_uploaded_image(qris, 'qris_', 'logo')
            if fname:
                old = get_setting('qris_code')
                if old:
                    old_path = os.path.join(UPLOAD_LOGO, old)
                    if os.path.exists(old_path): os.remove(old_path)
                update('qris_code', fname)

        if request.form.get('preview_logo_remove') == '1':
            old = get_setting('preview_logo_url')
            if old:
                old_path = os.path.join(UPLOAD_LOGO, old)
                if os.path.exists(old_path):
                    os.remove(old_path)
            update('preview_logo_url', '')

        preview_logo = request.files.get('preview_logo')
        if preview_logo and preview_logo.filename:
            fname = save_uploaded_image(preview_logo, 'preview_', 'logo')
            if fname:
                old = get_setting('preview_logo_url')
                if old:
                    old_path = os.path.join(UPLOAD_LOGO, old)
                    if os.path.exists(old_path): os.remove(old_path)
                update('preview_logo_url', fname)
        db.session.commit()
        flash('Pengaturan berhasil disimpan', 'success')
        return redirect(url_for('admin_settings'))

    settings = {s.key: (s.value or '') for s in Setting.query.all()}
    defaults = {
        'site_name': '', 'store_name': '', 'store_tagline': '', 'store_email': '',
        'store_whatsapp': '', 'store_address': '', 'store_city': '',
        'store_province': '', 'store_postal': '',
        'currency': 'IDR', 'timezone': 'Asia/Jakarta',
        'meta_title': '', 'meta_description': '', 'logo_filename': '', 'logo_url': '',
        'preview_title': '', 'preview_description': '', 'preview_logo_url': '',
        'telegram_bot_token': '', 'telegram_chat_id': '', 'telegram_api_id': '',
        'telegram_api_hash': '', 'telegram_phone': '', 'telegram_session_string': '',
        'telegram_group_link': '', 'officer_name': '', 'telegram_webhook_secret': '',
        'whatsapp_phone_number_id': '', 'whatsapp_access_token': '', 'whatsapp_recipient_number': '',
        'openrouter_base_url': '', 'openrouter_api_key': '', 'openrouter_webhook_secret': '',
        'openrouter_model': '',
        'openrouter_site_url': '', 'openrouter_app_name': '', 'ai_system_prompt': '',
        'openrouter_enabled': 'false', 'openrouter_notify_new_order': 'false',
        'openrouter_notify_status_update': 'false', 'openrouter_notify_product_update': 'false',
        'bank_name': '', 'bank_account_number': '', 'bank_account_holder': '',
        'ewallet_name': '', 'qris_code': '', 'qris_image': '',
        'bank_accounts_json': '[]', 'bank_transfer_instructions': '',
        'payment_bank_transfer_enabled': '1', 'payment_qris_enabled': '1',
        'payment_cod_enabled': '0', 'payment_deadline_hours': '24',
        'payment_notivgo_enabled': '1',
        'notivgo_api_key': os.getenv('NOTIVGO_API_KEY', ''),
        'notivgo_webhook_secret': os.getenv('NOTIVGO_WEBHOOK_SECRET', ''),
        'notivgo_base_url': os.getenv('NOTIVGO_BASE_URL', 'https://portal.notivgo.com'),
        'notivgo_auto_confirm_max': os.getenv('NOTIVGO_AUTO_CONFIRM_MAX', '0'),
        'free_shipping_enabled': 'false', 'free_shipping_min': '0',
        'shipping_enabled': 'true', 'packaging_cost': '0',
        'insurance_mode': 'optional', 'cod_fee': '0', 'cod_max_order': '0', 'cod_areas': '',
        'shipping_couriers': '', 'shipping_default_cost': '0',
        'shipping_free_threshold': '0', 'shipping_provinces': '',
        'wa_notif_new_order': 'false', 'wa_notif_payment_received': 'false',
        'wa_notif_order_shipped': 'false', 'wa_notif_order_done': 'false',
        'wa_notif_order_cancelled': 'false',
        'email_notif_new_order': 'false', 'email_notif_to_buyer': 'false',
        'email_notif_shipped': 'false',
    }
    for k, v in defaults.items():
        settings.setdefault(k, v)
    api_keys = ApiKey.query.order_by(ApiKey.created_at.desc()).all()
    return render_template('admin/settings.html', settings=settings, api_keys=api_keys)


# =============================================================================
# ADMIN — Kelola API Key Partner (dipakai di tab "Integrasi API")
# =============================================================================
@app.route('/admin/api-keys/new', methods=['POST'])
@admin_required
def admin_api_key_new():
    partner_name = (request.form.get('partner_name') or '').strip()
    custom_key = (request.form.get('custom_key') or '').strip()

    if not partner_name:
        flash('Nama partner wajib diisi.', 'error')
        return redirect(url_for('admin_settings') + '#api')

    if custom_key:
        # Mode integrasi: pakai API Key pelacakan paket yang sudah ada
        # (mis. dari partner ekspedisi eksternal), bukan generate baru.
        if not custom_key.startswith('dmk_') or len(custom_key) < 10:
            flash('Format API Key tidak valid. Key harus diawali "dmk_".', 'error')
            return redirect(url_for('admin_settings') + '#api')
        if ApiKey.query.filter_by(key=custom_key).first():
            flash('API Key tersebut sudah terdaftar.', 'error')
            return redirect(url_for('admin_settings') + '#api')
        key_value = custom_key
    else:
        key_value = generate_api_key()

    new_key = ApiKey(key=key_value, partner_name=partner_name)
    db.session.add(new_key)
    db.session.commit()
    flash(f'API Key untuk "{partner_name}" berhasil diintegrasikan.', 'success')
    return redirect(url_for('admin_settings') + '#api')

@app.route('/admin/api-keys/<int:id>/toggle', methods=['POST'])
@admin_required
def admin_api_key_toggle(id):
    api_key = ApiKey.query.get_or_404(id)
    api_key.is_active = not api_key.is_active
    db.session.commit()
    flash(f"API Key \"{api_key.partner_name}\" {'diaktifkan' if api_key.is_active else 'dinonaktifkan'}.", 'success')
    return redirect(url_for('admin_settings') + '#api')

@app.route('/admin/api-keys/<int:id>/delete', methods=['POST'])
@admin_required
def admin_api_key_delete(id):
    api_key = ApiKey.query.get_or_404(id)
    partner_name = api_key.partner_name
    db.session.delete(api_key)
    db.session.commit()
    flash(f'API Key untuk "{partner_name}" berhasil dihapus.', 'success')
    return redirect(url_for('admin_settings') + '#api')


@app.route('/admin/settings/test-openrouter', methods=['POST'])
@admin_required
def admin_test_openrouter_api():
    data = request.get_json()
    if not data:
        return jsonify({'success': False, 'message': 'Invalid data'}), 400
    base_url = data.get('base_url', '').strip()
    api_key = data.get('api_key', '').strip()
    if OPENROUTER_AVAILABLE:
        try:
            res = test_openrouter_connection(base_url=base_url, api_key=api_key, timeout=3)
            return jsonify(res)
        except Exception as e:
            return jsonify({'success': False, 'message': str(e)})
    else:
        try:
            if not base_url:
                return jsonify({'success': False, 'message': 'Base URL tidak diisi'})
            r = retry_session.get(f"{base_url}/api/health", timeout=3)
            if r.status_code == 200:
                return jsonify({'success': True, 'message': 'OK'})
            return jsonify({'success': False, 'message': f'Status {r.status_code}'})
        except Exception as e:
            return jsonify({'success': False, 'message': str(e)})

@app.route('/admin/settings/test-ai', methods=['POST'])
@admin_required
def admin_test_ai_connection():
    data = request.get_json() or {}
    base_url = (data.get('base_url') or '').strip()
    api_key = (data.get('api_key') or '').strip()
    model = (data.get('model') or 'openrouter/free').strip()
    # Bersihkan model ID dari prefix ganda
    if model.startswith('openrouter/openrouter/'):
        model = model.replace('openrouter/openrouter/', 'openrouter/', 1)
    elif model.startswith('openrouter/anthropic/'):
        model = model.replace('openrouter/anthropic/', 'anthropic/', 1)
    elif model.startswith('openrouter/openai/'):
        model = model.replace('openrouter/openai/', 'openai/', 1)
    elif model.startswith('openrouter/google/'):
        model = model.replace('openrouter/google/', 'google/', 1)
    site_url = (data.get('site_url') or 'https://slotriset.pythonanywhere.com').strip()
    app_name = (data.get('app_name') or 'ATLANTIS CELULAR').strip()
    if not base_url or not api_key:
        return jsonify({'success': False, 'message': 'Base URL dan API key wajib diisi'})
    try:
        r = retry_session.post(
            f"{base_url.rstrip('/')}/chat/completions",
            headers={
                'Authorization': f'Bearer {api_key}',
                'Content-Type': 'application/json',
                'HTTP-Referer': site_url,
                'X-Title': app_name
            },
            json={
                'model': model,
                'messages': [{'role': 'user', 'content': 'Ping'}],
                'max_tokens': 12
            },
            timeout=20
        )
        if r.status_code != 200:
            return jsonify({'success': False, 'message': f'Status {r.status_code}: {r.text[:200]}'})
        payload = r.json()
        reply = (((payload.get('choices') or [{}])[0].get('message') or {}).get('content') or '').strip()
        return jsonify({'success': True, 'message': f"Koneksi AI OK. Balasan: {reply[:80] or '(kosong)'}"})
    except Exception as e:
        return jsonify({'success': False, 'message': str(e)})

@app.route('/admin/ai-config')
@admin_required
def admin_ai_config():
    return render_template('admin/ai_config.html')

@app.route('/admin/openrouter')
@admin_required
def admin_openrouter_widget():
    return render_template('admin/openrouter_widget.html')

# =============================================================================
# WEBHOOK & BRIDGE
# =============================================================================
if OPENROUTER_AVAILABLE:
    create_webhook_route(app)
else:
    @app.route('/openrouter/webhook', methods=['POST'])
    def openrouter_webhook():
        return jsonify({'status': 'received'}), 200

if BRIDGE_AVAILABLE:
    OpenRouterTelegramBridge.setup_telegram_webhook(app)
    logger.info("Telegram Bridge webhook registered: POST /telegram/webhook/<token>")

if TELEGRAM_SHOP_BOT_AVAILABLE:
    # Dibungkus try/except: modul tambahan (bot menu statis) tidak boleh
    # menjatuhkan startup aplikasi utama kalau ada masalah saat registrasi,
    # sama seperti pola FAQ Bot di bawah.
    try:
        register_telegram_shop_bot(app)
    except Exception as e:
        logger.error(
            "Telegram Shop Bot gagal diregister — dilewati, aplikasi utama tetap jalan: %s",
            e, exc_info=True,
        )

if TELEGRAM_ADMIN_BOT_AVAILABLE:
    # 2026: Telegram Admin Bot (wizard posting produk /postproduk, /fastpost,
    # dst.) — dibungkus try/except mengikuti pola bot-bot lain di atas supaya
    # kegagalan register bot ini (mis. dependency hilang) tidak menjatuhkan
    # aplikasi utama.
    try:
        register_telegram_admin_bot(app)
        logger.info("Telegram Admin Bot webhook registered: POST /webhook/telegram/admin")
    except Exception as e:
        logger.error(
            "Telegram Admin Bot gagal diregister — dilewati, aplikasi utama tetap jalan: %s",
            e, exc_info=True,
        )

if NOTIVGO_AVAILABLE:
    try:
        with app.app_context():
            NOTIVGO = register_notivgo(
                app=app,
                db=db,
                Order=Order,
                NotificationService=NotificationService,
                csrf_protect_endpoint=csrf_protect_endpoint,
                logger=logger,
                retry_session=create_retry_session(),
                get_setting=get_setting,
            )
        logger.info("NotivGo payment registered: /api/payment/notivgo/*, /api/notivgo/webhook")
    except Exception as e:
        logger.error(
            "NotivGo gagal diregister — dilewati, aplikasi utama tetap jalan: %s",
            e, exc_info=True,
        )

if FAQBOT_AVAILABLE:
    # 2026 Pro fix: init_faq_bot() melakukan panggilan jaringan sinkron ke
    # Telegram (bot.get_me()) saat proses startup WSGI. Kalau proxy
    # outbound PythonAnywhere sedang bermasalah (503/timeout), exception
    # itu dulu TIDAK tertangkap di sini -> seluruh app gagal load (site
    # down total), padahal FAQ bot Telegram hanyalah fitur tambahan yang
    # tidak boleh menjatuhkan marketplace utama. Sekarang dibungkus
    # try/except: kalau gagal, cukup log error dan lanjutkan startup;
    # bot Telegram tidak aktif, tapi situs tetap jalan normal.
    try:
        _faqbot_instance = init_faq_bot(app)
        if _faqbot_instance is not None:
            logger.info("FAQ Bot (bot.py) registered: POST /telegram/faqbot/webhook/<secret>")
        else:
            # init_faq_bot() sendiri sudah menangani & mencatat error detailnya
            # (mis. gagal initialize/start setelah retry) dan sengaja
            # mengembalikan None tanpa melempar exception — jadi baris
            # "registered" TIDAK boleh ikut tercetak di sini, supaya log
            # startup tidak menyesatkan (sempat kejadian: log bilang
            # "registered" padahal route webhook tidak pernah didaftarkan).
            logger.warning(
                "FAQ Bot TIDAK terdaftar sesi ini (init_faq_bot mengembalikan None) — "
                "route webhook Telegram tidak aktif, aplikasi utama tetap jalan."
            )
    except Exception as e:
        logger.error(
            "FAQ Bot gagal diinisialisasi (kemungkinan jaringan/proxy ke "
            "Telegram bermasalah) — dilewati, aplikasi utama tetap jalan: %s",
            e, exc_info=True,
        )

# =============================================================================
# ADMIN AUTH
# =============================================================================
@app.route('/admin/login', methods=['GET','POST'])
def admin_login():
    if current_user.is_authenticated and isinstance(current_user, Admin):
        return redirect(url_for('admin_dashboard'))
    if request.method == 'POST':
        admin = Admin.query.filter_by(username=request.form.get('username')).first()
        if admin and admin.check_password(request.form.get('password')):
            login_user(admin)
            return redirect(url_for('admin_dashboard'))
        flash('Invalid username or password', 'error')
    return render_template('admin/login.html')

@app.route('/admin/logout')
@login_required
def admin_logout():
    logout_user()
    return redirect(url_for('admin_login'))

# --- Admin Shipping Tracking ---
@app.route('/admin/shipping-tracking')
@admin_required
def admin_shipping_tracking():
    search = request.args.get('search', '').strip()
    status = request.args.get('status', '').strip()
    carrier = request.args.get('carrier', '').strip()
    filter_type = request.args.get('filter', '').strip()
    page = request.args.get('page', 1, type=int)
    per_page = 20

    query = ShippingTracking.query
    if search:
        query = query.filter(
            or_(
                ShippingTracking.tracking_number.ilike(f'%{search}%'),
                ShippingTracking.recipient_name.ilike(f'%{search}%'),
                ShippingTracking.recipient_phone.ilike(f'%{search}%')
            )
        )
    if status:
        query = query.filter(ShippingTracking.status == status)
    if carrier:
        query = query.filter(ShippingTracking.carrier.ilike(carrier))
    if filter_type == 'delayed':
        query = query.filter(ShippingTracking.is_delayed == True)
    elif filter_type == 'issues':
        query = query.filter(ShippingTracking.has_issue == True)

    paginated = query.order_by(ShippingTracking.created_at.desc()).paginate(page=page, per_page=per_page, error_out=False)
    shipments = paginated.items
    total_pages = paginated.pages

    active_shipments = ShippingTracking.query.filter(
        ShippingTracking.status.notin_(['Delivered', 'Failed_Delivery'])
    ).count()
    delayed_shipments = ShippingTracking.query.filter(
        ShippingTracking.is_delayed == True, ShippingTracking.status != 'Delivered'
    ).count()
    delivered_shipments = ShippingTracking.query.filter_by(status='Delivered').count()
    problem_shipments = ShippingTracking.query.filter_by(has_issue=True).count()

    carrier_performance = ShippingCarrierPerformance.query.all()

    return render_template(
        'admin/shipping_tracking_dashboard.html',
        shipments=shipments,
        active_shipments=active_shipments,
        delayed_shipments=delayed_shipments,
        delivered_shipments=delivered_shipments,
        problem_shipments=problem_shipments,
        carrier_performance=carrier_performance,
        page=page,
        total_pages=total_pages,
        now=datetime.now().strftime('%A, %d %B %Y %H:%M')
    )

@app.route('/admin/shipping-tracking/update/<int:shipment_id>', methods=['GET', 'POST'])
@admin_required
def admin_update_shipping(shipment_id):
    shipment = ShippingTracking.query.get_or_404(shipment_id)
    if request.method == 'POST':
        status = request.form.get('status')
        current_location = request.form.get('current_location', '').strip()
        current_lat = request.form.get('current_lat', '').strip()
        current_lng = request.form.get('current_lng', '').strip()
        estimated_delivery = request.form.get('estimated_delivery')
        has_issue = request.form.get('has_issue') == 'on'
        issue_type = request.form.get('issue_type')
        issue_description = request.form.get('issue_description', '').strip()

        old_status = shipment.status
        shipment.status = status
        shipment.current_location = current_location
        try:
            shipment.current_lat = float(current_lat) if current_lat else None
            shipment.current_lng = float(current_lng) if current_lng else None
        except ValueError:
            pass
        if estimated_delivery:
            try:
                shipment.estimated_delivery = datetime.strptime(estimated_delivery, '%Y-%m-%d')
            except ValueError:
                pass

        shipment.has_issue = has_issue
        if has_issue:
            shipment.issue_type = issue_type
            shipment.issue_description = issue_description
            shipment.issue_reported_date = datetime.utcnow()
        else:
            shipment.issue_type = None
            shipment.issue_description = None

        if status == 'Delivered' and old_status != 'Delivered':
            shipment.actual_delivery = datetime.utcnow()
            shipment.order.status = 'Out_for_Delivery'
            shipment.order.updated_at = datetime.utcnow()
        elif status == 'Received' and old_status != 'Received':
            shipment.order.status = 'Received'
            shipment.order.updated_at = datetime.utcnow()
        elif status == 'Completed' and old_status != 'Completed':
            shipment.actual_delivery = datetime.utcnow()
            shipment.order.status = 'Completed'
            shipment.order.completed_at = datetime.utcnow()
        elif status != 'Delivered' and status not in ['Received', 'Completed'] and status in ['Pending_Pickup', 'Picked_Up', 'In_Transit', 'Out_for_Delivery']:
            if status == 'Out_for_Delivery':
                shipment.order.status = 'Out_for_Delivery'
            else:
                shipment.order.status = 'Shipped'

        event_desc = request.form.get('event_description', '').strip()
        event_loc = request.form.get('event_location', '').strip()
        if event_desc or (status != old_status):
            desc = event_desc if event_desc else f"Status pengiriman diperbarui menjadi {status.replace('_', ' ')}."
            loc = event_loc if event_loc else current_location
            event = ShippingTrackingEvent(
                shipping_tracking_id=shipment.id,
                status=status,
                status_description=desc,
                location=loc,
                event_type='status_update',
                event_time=datetime.utcnow()
            )
            db.session.add(event)

        db.session.commit()
        flash('Status pengiriman berhasil diperbarui.', 'success')
        return redirect(url_for('admin_shipping_tracking'))

    return render_template('admin/update_shipping.html', shipment=shipment)

# --- Shipping Tracking API ---
@app.route('/api/tracking/<tracking_number>')
def api_get_tracking(tracking_number):
    shipment = ShippingTracking.query.filter_by(tracking_number=tracking_number).first()
    if not shipment:
        return jsonify({'success': False, 'message': 'Tracking tidak ditemukan'}), 404

    events = ShippingTrackingEvent.query.filter_by(
        shipping_tracking_id=shipment.id
    ).order_by(ShippingTrackingEvent.event_time.desc()).all()
    history_data = []
    for e in events:
        history_data.append({
            'status': e.status.replace('_', ' '),
            'location': e.location,
            'description': e.status_description,
            'created_at': e.event_time.isoformat()
        })

    return jsonify({
        'success': True,
        'tracking_number': shipment.tracking_number,
        'carrier': shipment.carrier,
        'status': shipment.status,
        'current_location': shipment.current_location,
        'estimated_delivery': shipment.estimated_delivery.isoformat() if shipment.estimated_delivery else None,
        'actual_delivery': shipment.actual_delivery.isoformat() if shipment.actual_delivery else None,
        'recipient_name': shipment.recipient_name,
        'recipient_phone': shipment.recipient_phone,
        'recipient_address': shipment.recipient_address,
        'is_delayed': shipment.is_delayed,
        'delay_days': shipment.delay_days,
        'has_issue': shipment.has_issue,
        'issue_type': shipment.issue_type,
        'history': history_data
    })

# =============================================================================
# PARTNER API (v1) — Endpoint publik untuk mitra eksternal
# Semua endpoint di bawah membutuhkan header: X-API-Key: <key>
# Kelola API Key partner melalui menu Admin > Pengaturan > tab "Integrasi API"
# =============================================================================
def _lookup_shipment_tracking(resi):
    """Cari ShippingTracking berdasarkan nomor resi (case-insensitive) dan
    kembalikan payload dict siap-JSON, atau None jika tidak ditemukan.
    Dipakai bersama oleh endpoint Partner API dan proxy internal storefront."""
    resi_clean = (resi or '').strip()
    if not resi_clean:
        return None

    shipment = ShippingTracking.query.filter(
        func.lower(ShippingTracking.tracking_number) == resi_clean.lower()
    ).first()
    if not shipment:
        return None

    events = ShippingTrackingEvent.query.filter_by(
        shipping_tracking_id=shipment.id
    ).order_by(ShippingTrackingEvent.event_time.desc()).all()

    data = shipment.to_dict()
    data['order_number'] = shipment.order.order_number if shipment.order else None
    data['history'] = [e.to_dict() for e in events]
    return data

@app.route('/api/v1/shipments/<resi>', methods=['GET'])
@require_api_key
def api_v1_track_shipment(resi):
    """Lacak resi + riwayat status pengiriman (Partner API, butuh X-API-Key)."""
    if not (resi or '').strip():
        return jsonify({'success': False, 'error': 'Nomor resi tidak valid.'}), 400

    data = _lookup_shipment_tracking(resi)
    if data is None:
        return jsonify({'success': False, 'error': 'Nomor resi tidak ditemukan.'}), 404

    return jsonify({'success': True, 'data': data})

@app.route('/api/shipments/<resi>', methods=['GET'])
def api_shipments_proxy(resi):
    """Proxy internal untuk storefront (orders.html): sama seperti
    /api/v1/shipments/<resi>, tapi tanpa API Key, karena dipanggil langsung
    dari browser pelanggan sendiri. API Key partner tidak pernah dikirim
    ke frontend/browser."""
    if not (resi or '').strip():
        return jsonify({'success': False, 'error': 'Nomor resi tidak valid.'}), 400

    data = _lookup_shipment_tracking(resi)
    if data is None:
        return jsonify({'success': False, 'error': 'Nomor resi tidak ditemukan.'}), 404

    return jsonify({'success': True, 'data': data})

# =============================================================================
# STATIC FILES
# =============================================================================
@app.route('/static/uploads/products/<filename>')
def uploaded_product_file(filename):
    return send_from_directory(UPLOAD_PRODUCTS, filename)

@app.route('/static/uploads/logo/<filename>')
def uploaded_logo_file(filename):
    return send_from_directory(UPLOAD_LOGO, filename)

@app.route('/static/uploads/hero/<filename>')
def uploaded_hero_file(filename):
    return send_from_directory(UPLOAD_HERO, filename)

@app.route('/static/uploads/testimonials/<filename>')
def uploaded_testimonial_file(filename):
    return send_from_directory(UPLOAD_TESTIMONIAL, filename)

@app.route('/static/uploads/selfie/<filename>')
@login_required
def uploaded_selfie_file(filename):
    if os.path.exists(os.path.join(UPLOAD_INSTALLMENT_DOCS, filename)):
        return send_from_directory(UPLOAD_INSTALLMENT_DOCS, filename)
    if os.path.exists(os.path.join(UPLOAD_KYC_DOCS, filename)):
        return send_from_directory(UPLOAD_KYC_DOCS, filename)
    return send_from_directory(UPLOAD_INSTALLMENT_DOCS, filename)

@app.route('/static/uploads/ktp/<filename>')
@login_required
def uploaded_ktp_file(filename):
    if os.path.exists(os.path.join(UPLOAD_INSTALLMENT_DOCS, filename)):
        return send_from_directory(UPLOAD_INSTALLMENT_DOCS, filename)
    if os.path.exists(os.path.join(UPLOAD_KYC_DOCS, filename)):
        return send_from_directory(UPLOAD_KYC_DOCS, filename)
    return send_from_directory(UPLOAD_INSTALLMENT_DOCS, filename)

# =============================================================================
# ERROR HANDLERS
# =============================================================================
@app.errorhandler(404)
def not_found(e):
    if request.is_json:
        return jsonify({'success': False, 'message': 'Not found'}), 404
    return render_template('errors/404.html'), 404

@app.errorhandler(403)
def forbidden(e):
    if request.is_json:
        return jsonify({'success': False, 'message': 'Forbidden'}), 403
    flash('Akses ditolak.', 'error')
    return redirect(url_for('index'))

@app.errorhandler(500)
def internal_error(e):
    db.session.rollback()
    if request.is_json:
        return jsonify({'success': False, 'message': 'Internal server error'}), 500
    return render_template('errors/500.html'), 500

@app.errorhandler(429)
def rate_limit_handler(e):
    if request.is_json:
        return jsonify({'success': False, 'message': 'Too many requests'}), 429
    flash('Terlalu banyak permintaan. Silakan tunggu sebentar.', 'warning')
    return redirect(request.referrer or url_for('index'))

# =============================================================================
# CSRF PROTECTION
# =============================================================================
@app.before_request
def csrf_protect():
    if request.method == "POST":
        session.setdefault('_csrf_token', secrets.token_hex(16))

app.jinja_env.globals['csrf_token'] = generate_csrf_token

# =============================================================================

def _get_credit_enabled() -> bool:
    """Check if credit system is enabled in settings."""
    return get_setting('credit_enabled', 'false').lower() in ('true', '1', 'yes')


def _get_user_approved_credit(user_id: int):
    """Get approved credit application for user, or None."""
    if not _get_credit_enabled():
        return None
    credit_app = InstallmentApplication.query.filter_by(
        user_id=user_id,
        status='approved'
    ).order_by(InstallmentApplication.approved_at.desc()).first()
    return credit_app

# =============================================================================
# ADMIN CREDIT MANAGEMENT ROUTES
# =============================================================================

@app.route('/admin/credit-applications')
@admin_required
def admin_credit_applications():
    """List all credit applications for admin review."""
    status = request.args.get('status', '').strip()
    search = request.args.get('search', '').strip()
    page = request.args.get('page', 1, type=int)
    per_page = 25

    query = InstallmentApplication.query

    if status:
        query = query.filter_by(status=status)
    else:
        query = query.filter(InstallmentApplication.status.in_(['pending', 'under_review']))

    if search:
        search_pattern = f"%{search}%"
        query = query.filter(
            or_(
                InstallmentApplication.full_name.ilike(search_pattern),
                InstallmentApplication.nik.ilike(search_pattern),
                InstallmentApplication.phone.ilike(search_pattern),
                InstallmentApplication.email.ilike(search_pattern),
            )
        )

    pagination = query.order_by(InstallmentApplication.created_at.desc()).paginate(
        page=page, per_page=per_page, error_out=False
    )

    stats = {
        'total': InstallmentApplication.query.count(),
        'pending': InstallmentApplication.query.filter_by(status='pending').count(),
        'approved': InstallmentApplication.query.filter_by(status='approved').count(),
        'active': InstallmentApplication.query.filter_by(status='active').count(),
        'rejected': InstallmentApplication.query.filter_by(status='rejected').count(),
        'defaulted': InstallmentApplication.query.filter_by(status='defaulted').count(),
    }

    return render_template('admin/admin_credit_applications.html',
                           applications=pagination.items,
                           pagination=pagination,
                           status=status,
                           current_status=status,
                           search_q=search,
                           stats=stats,
                           format_price=format_price)


@app.route('/admin/credit-application/<int:app_id>')
@admin_required
def admin_credit_application_detail(app_id):
    """View credit application details."""
    application = InstallmentApplication.query.get_or_404(app_id)
    product = Product.query.get(application.product_id) if application.product_id else None
    user = User.query.get(application.user_id) if application.user_id else None

    schedule = []
    try:
        if hasattr(application, 'generate_schedule'):
            schedule = application.generate_schedule()
    except Exception as e:
        logger.warning(f"Error generating credit schedule: {e}")

    return render_template('admin/admin_credit_detail.html',
                           app=application,
                           application=application,
                           product=product,
                           user=user,
                           schedule=schedule,
                           installments=application.schedules or [],
                           format_price=format_price)


@app.route('/admin/credit-application/<int:app_id>/approve', methods=['POST'])
@admin_required
def admin_credit_approve(app_id):
    """Approve a credit application."""
    application = InstallmentApplication.query.get_or_404(app_id)

    if application.status != 'pending':
        return jsonify({'success': False, 'message': 'Application already processed'}), 400

    application.status = 'approved'
    application.approved_at = datetime.utcnow()
    application.approved_by = current_user.id

    db.session.commit()

    # Notify user
    try:
        user = User.query.get(application.user_id)
        if user and user.phone:
            _prod_price_cents = (
                application.product_price if application.product_price is not None and application.product_price > 0
                else int(round((application.product_amount or 0) * 100))
            )
            _monthly_cents = int(round((application.monthly_installment or 0) * 100))
            msg = (
                f"✅ *PENGAJUAN KREDIT DISETUJUI*\n\n"
                f"Selamat {user.full_name}! Pengajuan kredit Anda telah disetujui.\n"
                f"Produk: {format_price(_prod_price_cents)}\n"
                f"Angsuran: {format_price(_monthly_cents)}/bulan x {application.tenor_months} bulan\n\n"
                f"Silakan lanjutkan ke checkout untuk menyelesaikan pembelian."
            )
            NotificationService.send_whatsapp(user.phone, msg)
    except Exception as e:
        logger.error(f"Credit approval notification error: {e}")

    return jsonify({'success': True, 'message': 'Pengajuan kredit disetujui'})


@app.route('/admin/credit-application/<int:app_id>/reject', methods=['POST'])
@admin_required
def admin_credit_reject(app_id):
    """Reject a credit application."""
    application = InstallmentApplication.query.get_or_404(app_id)
    data = request.get_json() or {}
    reason = data.get('reason', '').strip()

    if application.status != 'pending':
        return jsonify({'success': False, 'message': 'Application already processed'}), 400

    application.status = 'rejected'
    application.rejected_at = datetime.utcnow()
    application.rejected_by = current_user.id
    application.rejection_reason = reason

    db.session.commit()

    # Notify user
    try:
        user = User.query.get(application.user_id)
        if user and user.phone:
            msg = (
                f"❌ *PENGAJUAN KREDIT DITOLAK*\n\n"
                f"Maaf {user.full_name}, pengajuan kredit Anda tidak dapat disetujui.\n"
                f"{'Alasan: ' + reason if reason else ''}\n\n"
                f"Silakan hubungi kami untuk informasi lebih lanjut."
            )
            NotificationService.send_whatsapp(user.phone, msg)
    except Exception as e:
        logger.error(f"Credit rejection notification error: {e}")

    return jsonify({'success': True, 'message': 'Pengajuan kredit ditolak'})


@app.route('/admin/credit-application/<int:app_id>/update-status', methods=['POST'])
@admin_required
@csrf_protect_endpoint
def admin_credit_update_status(app_id):
    """Update status of a credit application (e.g. manual activation)."""
    application = InstallmentApplication.query.get_or_404(app_id)
    data = request.get_json(silent=True) or {}
    new_status = data.get('status', '').strip()
    if not new_status:
        return jsonify({'success': False, 'message': 'Status tidak valid'}), 400

    application.status = new_status
    if new_status == 'active' and not application.approved_at:
        application.approved_at = datetime.utcnow()
        application.approved_by = current_user.id
    db.session.commit()
    return jsonify({'success': True, 'message': f'Status kredit berhasil diubah menjadi {new_status}'})


@app.route('/admin/credit-settings', methods=['GET', 'POST'])
@admin_required
def admin_credit_settings():
    """Manage credit system settings."""
    if request.method == 'POST':
        settings_to_update = {
            'credit_enabled': 'true' if request.form.get('credit_enabled') else 'false',
            'credit_min_dp_pct': request.form.get('min_dp_pct', '30'),
            'credit_max_tenor': request.form.get('max_tenor', '24'),
            'credit_max_amount': request.form.get('max_amount', '0'),
            'credit_require_verification': 'true' if request.form.get('require_verification') else 'false',
        }

        for key, value in settings_to_update.items():
            setting = Setting.query.filter_by(key=key).first()
            if setting:
                setting.value = value
            else:
                db.session.add(Setting(key=key, value=value))

        db.session.commit()
        flash('Pengaturan kredit berhasil diperbarui', 'success')
        return redirect(url_for('admin_credit_settings'))

    current_settings = {
        'credit_enabled': get_setting('credit_enabled', 'false'),
        'min_dp_pct': get_setting('credit_min_dp_pct', '30'),
        'max_tenor': get_setting('credit_max_tenor', '24'),
        'max_amount': get_setting('credit_max_amount', '0'),
        'require_verification': get_setting('credit_require_verification', 'true'),
    }

    return render_template('admin/credit_settings.html', settings=current_settings)




# =============================================================================
# PUBLIC INSTALLMENT API ROUTES
# =============================================================================

@app.route('/api/installment/plans')
def api_installment_plans_public():
    """Get all active installment plans for public calculator.

    Tenor dibatasi maksimal 24 bulan (credit_max_tenor) sehingga customer
    tidak bisa memilih plan yang tenornya akan ditolak saat submit di
    /api/installment/apply."""
    max_tenor = _get_checkout_credit_max_tenor()
    plans = (InstallmentPlan.query
             .filter_by(is_active=True)
             .filter(InstallmentPlan.tenor_months <= max_tenor)
             .order_by(InstallmentPlan.tenor_months)
             .all())
    return jsonify({
        'success': True,
        'plans': [p.to_dict() for p in plans]
    })


@app.route('/api/installment/calculate', methods=['POST'])
def api_installment_calculate():
    """Calculate installment simulation based on plan and DP."""
    data = request.get_json() or {}

    # product_amount dari frontend dikirim dalam CENTS. Kita harus konversi ke
    # RUPIAH agar kalkulasi konsisten, karena plan.processing_fee disimpan
    # dalam RUPIAH di database.
    product_amount_cents = _safe_float(data.get('product_amount'), 0.0)
    product_amount = product_amount_cents / 100.0

    dp_percentage = _safe_float(data.get('dp_percentage'), 0.3)
    plan_id = _safe_int(data.get('plan_id'), 0)

    if not plan_id:
        return jsonify({'success': False, 'message': 'Plan ID wajib diisi'}), 400

    plan = InstallmentPlan.query.get(plan_id)
    if not plan:
        return jsonify({'success': False, 'message': 'Plan tidak ditemukan'}), 404

    dp_amount = round(product_amount * dp_percentage)
    loan_amount = product_amount - dp_amount

    if loan_amount <= 0:
        return jsonify({'success': False, 'message': 'DP tidak boleh 100%'}), 400

    monthly_installment = plan.calculate_installment(product_amount, dp_amount)
    total_payment = monthly_installment * plan.tenor_months
    total_interest = total_payment - loan_amount - (plan.processing_fee or 0)

    # Kembalikan semua nilai dalam CENTS karena frontend installment_apply.html
    # menggunakan formatCents() yang membagi dengan 100.
    return jsonify({
        'success': True,
        'calculation': {
            'product_amount': int(round(product_amount * 100)),
            'dp_amount': int(round(dp_amount * 100)),
            'loan_amount': int(round(loan_amount * 100)),
            'monthly_installment': int(round(monthly_installment * 100)),
            'total_interest': int(round(max(0, total_interest) * 100)),
            'processing_fee': int(round((plan.processing_fee or 0) * 100)),
            'total_payment': int(round(total_payment * 100)),
            'tenor_months': plan.tenor_months,
            'interest_rate_monthly': plan.interest_rate_monthly,
            'interest_type': plan.interest_type
        }
    })


@app.route('/api/installment/apply', methods=['POST'])
@login_required
@csrf_protect_endpoint
def api_installment_apply_public():
    """Submit installment application from public form."""
    if not isinstance(current_user, User):
        return jsonify({'success': False, 'message': 'Hanya pelanggan yang dapat mengajukan'}), 403

    # SECURITY: enforce KYC server-side (mirrors the check in the
    # installment_apply page route, which only guards the GET request).
    kyc_required = get_setting('credit_require_kyc', 'true') == 'true'
    if kyc_required and not current_user.kyc_verified:
        return jsonify({
            'success': False,
            'message': 'Verifikasi KYC wajib diselesaikan sebelum mengajukan cicilan.',
            'redirect': url_for('kyc_verification_page')
        }), 403

    # Handle both JSON and FormData
    if request.content_type and 'multipart/form-data' in request.content_type:
        data = request.form.to_dict()
    else:
        data = request.get_json() or {}

    def _to_int(value, default=0):
        """Safely coerce form/JSON values (which may arrive as strings) to int."""
        if value is None or value == '':
            return default
        try:
            return int(float(value))
        except (TypeError, ValueError):
            return default

    plan_id = _to_int(data.get('plan_id'), None)
    product_amount = _to_int(data.get('product_amount', 0))
    dp_amount = _to_int(data.get('dp_amount', 0))

    # Personal info
    full_name = data.get('full_name', '').strip()
    nik = data.get('nik', '').strip()
    phone = data.get('phone', '').strip()
    email = data.get('email', '').strip()
    birth_place = data.get('birth_place', '').strip()
    birth_date = data.get('birth_date', '')
    gender = data.get('gender', '').strip()
    marital_status = data.get('marital_status', '').strip()
    address_ktp = data.get('address_ktp', '').strip()

    # Employment
    employment_type = data.get('employment_type', '').strip()
    company_name = data.get('company_name', '').strip()
    job_title = data.get('job_title', '').strip()
    monthly_income = _to_int(data.get('monthly_income', 0))

    # Emergency contact
    emergency_contact_name = data.get('emergency_contact_name', '').strip()
    emergency_contact_phone = data.get('emergency_contact_phone', '').strip()
    emergency_contact_relation = data.get('emergency_contact_relation', '').strip()

    # SECURITY: user yang sudah KYC verified TIDAK BOLEH mengirim identitasnya
    # sendiri. installment_apply.html mengirim full_name/nik/phone/address_ktp
    # sebagai hidden input yang diisi otomatis dari data KYC, tapi hidden input
    # tetap bisa dimanipulasi lewat DevTools atau panggilan API langsung.
    # Untuk user kyc_verified, timpa dengan data resmi dari sesi KYC yang sudah
    # direview admin / data user di database — abaikan apa pun yang dikirim
    # client untuk field-field identitas ini.
    kyc_session_for_app = get_latest_verified_kyc_session(current_user.id) if current_user.kyc_verified else None
    if current_user.kyc_verified:
        full_name = (kyc_session_for_app.ktp_name if kyc_session_for_app else None) or current_user.full_name or full_name
        nik = (kyc_session_for_app.ktp_nik if kyc_session_for_app else None) or current_user.kyc_nik or nik
        phone = current_user.phone or phone
        address_ktp = (kyc_session_for_app.ktp_address if kyc_session_for_app else None) or current_user.address or address_ktp

    # Validation
    if not all([plan_id, full_name, nik, phone, address_ktp]):
        return jsonify({'success': False, 'message': 'Field wajib: nama, NIK, HP, alamat'}), 400

    if len(nik) != 16 or not nik.isdigit():
        return jsonify({'success': False, 'message': 'NIK harus 16 digit angka'}), 400

    plan = InstallmentPlan.query.get(plan_id)
    if not plan:
        return jsonify({'success': False, 'message': 'Plan tidak ditemukan'}), 404

    # VALIDASI KERAS: DP minimum (nominal Rp500.000) & tenor maksimum (24 bulan)
    # sesuai pengaturan admin. Sinkron dengan alur checkout kredit / product.html
    # (_get_checkout_credit_dp_min / _get_checkout_credit_max_tenor) sehingga
    # aturan DP tidak lagi berbasis persentase yang berbeda-beda per produk.
    if product_amount <= 0:
        return jsonify({'success': False, 'message': 'Jumlah produk tidak valid'}), 400

    min_dp_nominal = _get_checkout_credit_dp_min()
    max_tenor = _get_checkout_credit_max_tenor()

    if dp_amount < min_dp_nominal:
        return jsonify({
            'success': False,
            'message': f'Uang muka (DP) minimum adalah {format_price(min_dp_nominal * 100)}'
        }), 400

    if plan.tenor_months > max_tenor:
        return jsonify({
            'success': False,
            'message': f'Tenor maksimum adalah {max_tenor} bulan'
        }), 400

    if not plan.is_active:
        return jsonify({'success': False, 'message': 'Plan cicilan tidak aktif'}), 400

    # Check for existing pending application
    existing = InstallmentApplication.query.filter_by(
        user_id=current_user.id,
        status='pending'
    ).first()
    if existing:
        return jsonify({'success': False, 'message': 'Anda sudah memiliki pengajuan pending'}), 400

    # Calculate loan details
    loan_amount = product_amount - dp_amount
    monthly_installment = plan.calculate_installment(product_amount, dp_amount)
    total_loan = monthly_installment * plan.tenor_months
    interest_amount = total_loan - loan_amount - (plan.processing_fee or 0)

    # Generate application code
    import uuid
    app_code = f"CLN-{datetime.now().strftime('%Y%m%d')}-{uuid.uuid4().hex[:4].upper()}"

    # Handle file uploads.
    # FIX: user yang sudah KYC verified tidak perlu upload ulang KTP/selfie —
    # salin dokumen yang sudah tervalidasi dari sesi KYC-nya. Flag ini hanya
    # dipercaya jika current_user.kyc_verified benar-benar True di server
    # (jangan percaya begitu saja nilai dari client).
    use_existing_kyc = data.get('use_existing_kyc') == '1' and current_user.kyc_verified

    if use_existing_kyc:
        if not current_user.kyc_ktp_photo or not current_user.kyc_selfie_photo:
            return jsonify({'success': False, 'message': 'Data dokumen KYC tidak lengkap. Silakan ajukan verifikasi ulang.'}), 400
        try:
            ktp_photo = _copy_kyc_doc_to_installment(current_user.kyc_ktp_photo, prefix='ktp_kyc_')
            selfie_photo = _copy_kyc_doc_to_installment(current_user.kyc_selfie_photo, prefix='selfie_kyc_')
        except FileNotFoundError:
            return jsonify({'success': False, 'message': 'Dokumen KYC tidak ditemukan di server. Silakan ajukan verifikasi ulang.'}), 400
        # Slip gaji/NPWP/KK bukan bagian dari verifikasi KYC (wajah+KTP), jadi tetap
        # ambil dari upload jika user melampirkannya (opsional, tidak wajib).
        slip_gaji_photo = save_uploaded_image(request.files.get('slip_gaji_file'), prefix='slip_', subfolder='installment_docs')
        npwp_photo = save_uploaded_image(request.files.get('npwp_file'), prefix='npwp_', subfolder='installment_docs')
        kk_photo = save_uploaded_image(request.files.get('kk_file'), prefix='kk_', subfolder='installment_docs')
    else:
        ktp_file = request.files.get('ktp_file') or request.files.get('ktp_photo')
        selfie_file = request.files.get('selfie_file') or request.files.get('selfie_photo')
        if not ktp_file or not selfie_file:
            return jsonify({'success': False, 'message': 'Foto KTP dan selfie wajib diupload'}), 400

        ktp_photo = save_uploaded_image(ktp_file, prefix='ktp_', subfolder='installment_docs')
        if not ktp_photo:
            return jsonify({'success': False, 'message': 'Format foto KTP tidak didukung'}), 400

        selfie_photo = save_uploaded_image(selfie_file, prefix='selfie_', subfolder='installment_docs')
        if not selfie_photo:
            return jsonify({'success': False, 'message': 'Format foto selfie tidak didukung'}), 400

        slip_gaji_photo = save_uploaded_image(request.files.get('slip_gaji_file'), prefix='slip_', subfolder='installment_docs')
        npwp_photo = save_uploaded_image(request.files.get('npwp_file'), prefix='npwp_', subfolder='installment_docs')
        kk_photo = save_uploaded_image(request.files.get('kk_file'), prefix='kk_', subfolder='installment_docs')

    # kyc_session_for_app sudah dihitung di atas (dipakai juga untuk override
    # identitas), tidak perlu query ulang di sini.

    try:
        application = InstallmentApplication(
            application_code=app_code,
            user_id=current_user.id,
            plan_id=plan_id,
            kyc_session_id=kyc_session_for_app.id if kyc_session_for_app else None,
            full_name=full_name,
            nik=nik,
            phone=phone,
            email=email or None,
            birth_place=birth_place or None,
            birth_date=datetime.strptime(birth_date, '%Y-%m-%d').date() if birth_date else None,
            gender=gender or None,
            marital_status=marital_status or None,
            address_ktp=address_ktp,
            employment_type=employment_type or None,
            company_name=company_name or None,
            job_title=job_title or None,
            monthly_income=monthly_income or 0,
            emergency_contact_name=emergency_contact_name or None,
            emergency_contact_phone=emergency_contact_phone or None,
            emergency_contact_relation=emergency_contact_relation or None,
            ktp_photo=ktp_photo,
            selfie_photo=selfie_photo,
            slip_gaji_photo=slip_gaji_photo,
            npwp_photo=npwp_photo,
            kk_photo=kk_photo,
            product_amount=product_amount,
            dp_amount=dp_amount,
            loan_amount=loan_amount,
            interest_amount=max(0, interest_amount),
            processing_fee_amount=plan.processing_fee or 0,
            total_loan=total_loan,
            monthly_installment=monthly_installment,
            biometric_verified=use_existing_kyc,
            biometric_match_score=current_user.kyc_match_score if use_existing_kyc else None,
            status='pending',
            ip_address=request.remote_addr,
            user_agent=request.user_agent.string if request.user_agent else None
        )
        db.session.add(application)
        db.session.commit()

        # Notify admin
        try:
            review_url = url_for('admin_installment_detail', app_id=application.id, _external=True)
            msg = (
                f"📋 <b>PENGAJUAN CICILAN BARU</b>\n\n"
                f"<b>Kode:</b> {app_code}\n"
                f"<b>Pelanggan:</b> {full_name}\n"
                f"<b>NIK:</b> {nik}\n"
                f"<b>HP:</b> {phone}\n"
                f"<b>Nominal:</b> {format_price(product_amount * 100)}\n"
                f"<b>DP:</b> {format_price(dp_amount * 100)}\n"
                f"<b>Cicilan:</b> {format_price(monthly_installment * 100)}/bulan x {plan.tenor_months} bulan\n"
                f"<b>Status:</b> Menunggu Review\n\n"
                f"👉 <a href='{review_url}'>Review di Panel Admin</a>"
            )
            NotificationService.send_telegram(msg)
        except Exception as e:
            logger.error(f"Installment notification error: {e}")

        return jsonify({
            'success': True,
            'message': 'Pengajuan berhasil dikirim',
            'application': {
                'id': application.id,
                'application_code': application.application_code,
                'status': application.status
            }
        })

    except Exception as e:
        db.session.rollback()
        logger.error(f"Installment apply error: {e}", exc_info=True)
        return jsonify({'success': False, 'message': f'Terjadi kesalahan: {str(e)}'}), 500


# =============================================================================
# ADMIN INSTALLMENT ROUTES (Cicilan)
# =============================================================================

@app.route('/admin/installments')
@admin_required
def admin_installments():
    """List all installment applications with stats."""
    status = request.args.get('status', '').strip()
    page = request.args.get('page', 1, type=int)
    per_page = 25

    query = InstallmentApplication.query.options(
        joinedload(InstallmentApplication.kyc_session),
        joinedload(InstallmentApplication.user),
        joinedload(InstallmentApplication.plan),
        joinedload(InstallmentApplication.product),
    )
    if status:
        query = query.filter_by(status=status)

    pagination = query.order_by(InstallmentApplication.created_at.desc()).paginate(
        page=page, per_page=per_page, error_out=False
    )

    # Calculate stats
    stats = {
        'total': InstallmentApplication.query.count(),
        'pending': InstallmentApplication.query.filter_by(status='pending').count(),
        'reviewing': InstallmentApplication.query.filter_by(status='reviewing').count(),
        'approved': InstallmentApplication.query.filter_by(status='approved').count(),
        'rejected': InstallmentApplication.query.filter_by(status='rejected').count(),
        'total_outstanding': db.session.query(func.coalesce(func.sum(InstallmentSchedule.amount - InstallmentSchedule.paid_amount), 0))
            .filter(InstallmentSchedule.status != 'paid').scalar()
    }

    return render_template('admin/admin_installments.html',
                           applications=pagination.items,
                           pagination=pagination,
                           stats=stats,
                           status=status,
                           plans=[p.to_dict() for p in InstallmentPlan.query.filter_by(is_active=True).order_by(InstallmentPlan.tenor_months).all()],
                           format_price=format_price)


@app.route('/admin/installment/<int:app_id>')
@admin_required
def admin_installment_detail(app_id):
    """View installment application detail."""
    application = InstallmentApplication.query.get_or_404(app_id)
    schedules = InstallmentSchedule.query.filter_by(application_id=app_id).order_by(InstallmentSchedule.installment_number).all()

    return render_template('admin/admin_installment_detail.html',
                           application=application,
                           schedules=schedules,
                           format_price=format_price)


@app.route('/admin/installment/<int:app_id>/review', methods=['POST'])
@admin_required
@csrf_protect_endpoint
def admin_review_installment(app_id):
    """Approve or reject an installment application."""
    application = InstallmentApplication.query.get_or_404(app_id)
    data = request.get_json() or {}
    action = data.get('action')

    if application.status not in ['pending', 'reviewing']:
        return jsonify({'success': False, 'message': 'Pengajuan sudah diproses'}), 400

    if action == 'approve':
        application.status = 'approved'
        application.approved_at = datetime.utcnow()
        application.approved_by = current_user.id

        # Generate schedule — hanya jika belum dibuat saat pengajuan
        # (api_credit_apply membuat jadwal langsung; api_installment_apply tidak)
        db.session.flush()
        if not InstallmentSchedule.query.filter_by(application_id=application.id).first():
            schedules = application.generate_schedule()
            for sched in schedules:
                db.session.add(sched)

        db.session.commit()

        # Notify user
        try:
            user = User.query.get(application.user_id)
            if user and user.phone:
                # product_amount and monthly_installment from api_installment_apply_public
                # are stored in RUPIAH → multiply by 100 for format_price (which expects cents)
                _tenor = (application.plan.tenor_months if application.plan
                          else getattr(application, 'tenor_months', None) or '?')
                msg = (
                    f"✅ *PENGAJUAN CICILAN DISETUJUI*\n\n"
                    f"Selamat {user.full_name}! Pengajuan cicilan Anda telah disetujui.\n"
                    f"Nominal: {format_price(int((application.product_amount or 0) * 100))}\n"
                    f"Cicilan: {format_price(int((application.monthly_installment or 0) * 100))}/bulan x {_tenor} bulan\n\n"
                    f"Silakan login untuk melihat jadwal pembayaran."
                )
                NotificationService.send_whatsapp(user.phone, msg)
        except Exception as e:
            logger.error(f"Installment approval notification error: {e}")

        return jsonify({'success': True, 'message': 'Pengajuan disetujui'})

    elif action == 'reject':
        reason = data.get('reason', '').strip()
        application.status = 'rejected'
        application.rejected_at = datetime.utcnow()
        application.rejected_by = current_user.id
        application.rejection_reason = reason
        db.session.commit()

        # Notify user
        try:
            user = User.query.get(application.user_id)
            if user and user.phone:
                msg = (
                    f"❌ *PENGAJUAN CICILAN DITOLAK*\n\n"
                    f"Maaf {user.full_name}, pengajuan cicilan Anda tidak dapat disetujui.\n"
                    f"{'Alasan: ' + reason if reason else ''}\n\n"
                    f"Silakan hubungi kami untuk informasi lebih lanjut."
                )
                NotificationService.send_whatsapp(user.phone, msg)
        except Exception as e:
            logger.error(f"Installment rejection notification error: {e}")

        return jsonify({'success': True, 'message': 'Pengajuan ditolak'})

    return jsonify({'success': False, 'message': 'Aksi tidak valid'}), 400


@app.route('/admin/installment/schedule/<int:schedule_id>/pay', methods=['POST'])
@admin_required
@csrf_protect_endpoint
def admin_mark_schedule_paid(schedule_id):
    """Mark an installment schedule as paid."""
    schedule = InstallmentSchedule.query.get_or_404(schedule_id)
    data = request.get_json() or {}

    if schedule.status == 'paid':
        return jsonify({'success': False, 'message': 'Sudah lunas'}), 400

    schedule.status = 'paid'
    schedule.paid_amount = schedule.amount
    schedule.paid_at = datetime.utcnow()
    schedule.payment_method = data.get('payment_method', 'transfer')

    # Create payment record
    payment = InstallmentPayment(
        schedule_id=schedule.id,
        application_id=schedule.application_id,
        amount=schedule.amount,
        payment_method=schedule.payment_method,
        processed_by=current_user.id
    )
    db.session.add(payment)
    db.session.commit()

    return jsonify({'success': True, 'message': 'Pembayaran tercatat'})


@app.route('/admin/installment-settings', methods=['GET', 'POST'])
@admin_required
@csrf_protect_endpoint
def admin_installment_settings():
    """Manage installment plans."""
    if request.method == 'POST':
        plan_id = request.form.get('plan_id', type=int)

        if plan_id:
            plan = InstallmentPlan.query.get_or_404(plan_id)
        else:
            plan = InstallmentPlan()
            db.session.add(plan)

        plan.name = request.form.get('name', '').strip()
        try:
            plan.tenor_months = int(request.form.get('tenor_months', 6))
            plan.interest_rate_monthly = float(request.form.get('interest_rate_monthly', 1.5)) / 100
            plan.interest_type = request.form.get('interest_type', 'flat')
            # Form now sends 0-100 range (e.g. 30 = 30%). Divide by 100 to store as decimal 0-1.
            _min_dp_raw = float(request.form.get('min_dp_percentage', 30))
            _max_dp_raw = float(request.form.get('max_dp_percentage', 80))
            plan.min_dp_percentage = _min_dp_raw / 100
            plan.max_dp_percentage = _max_dp_raw / 100
            plan.processing_fee = int(request.form.get('processing_fee', 0) or 0)
            plan.late_fee_per_day = int(request.form.get('late_fee_per_day', 5000) or 5000)
            plan.grace_period_days = int(request.form.get('grace_period_days', 3) or 3)
        except (ValueError, TypeError):
            db.session.rollback()
            flash('Nilai numerik pada form tidak valid. Periksa kembali tenor, bunga, DP, dan biaya.', 'error')
            return redirect(url_for('admin_installment_settings'))
        plan.is_active = request.form.get('is_active') == 'true'
        plan.description = request.form.get('description', '').strip()

        db.session.commit()
        flash('Plan cicilan berhasil disimpan', 'success')
        return redirect(url_for('admin_installment_settings'))

    plans = InstallmentPlan.query.order_by(InstallmentPlan.tenor_months).all()
    return render_template('admin/admin_installment_settings.html',
                           plans=plans,
                           format_price=format_price)


@app.route('/admin/installment-plan/<int:plan_id>/toggle', methods=['POST'])
@admin_required
@csrf_protect_endpoint
def admin_toggle_installment_plan(plan_id):
    """Activate or deactivate an installment plan."""
    plan = InstallmentPlan.query.get_or_404(plan_id)
    data = request.get_json(silent=True) or {}
    plan.is_active = bool(data.get('is_active', not plan.is_active))
    db.session.commit()
    return jsonify({
        'success': True,
        'message': f"Plan {'diaktifkan' if plan.is_active else 'dinonaktifkan'}",
        'is_active': plan.is_active
    })


@app.route('/admin/installment-plan/<int:plan_id>/delete', methods=['POST'])
@admin_required
@csrf_protect_endpoint
def admin_delete_installment_plan(plan_id):
    """Delete an installment plan, unless applications already reference it
    (plan_id is a required, non-nullable FK on InstallmentApplication, so
    deleting it out from under existing applications would break their
    records). In that case, tell the admin to deactivate it instead."""
    plan = InstallmentPlan.query.get_or_404(plan_id)

    in_use = InstallmentApplication.query.filter_by(plan_id=plan_id).count()
    if in_use > 0:
        return jsonify({
            'success': False,
            'message': f'Plan ini dipakai oleh {in_use} pengajuan cicilan dan tidak bisa dihapus. Nonaktifkan plan ini sebagai gantinya.'
        }), 400

    try:
        db.session.delete(plan)
        db.session.commit()
        return jsonify({'success': True, 'message': 'Plan cicilan berhasil dihapus'})
    except Exception as e:
        db.session.rollback()
        logger.error(f"Delete installment plan error: {e}", exc_info=True)
        return jsonify({'success': False, 'message': 'Gagal menghapus plan'}), 500


@app.route('/api/admin/installment/plans')
@admin_required
def api_installment_plans():
    """API to get all installment plans for JS editing."""
    plans = InstallmentPlan.query.all()
    return jsonify({
        'success': True,
        'plans': [p.to_dict() for p in plans]
    })

# =============================================================================
# FILE SERVING FOR INSTALLMENT DOCUMENTS
# =============================================================================

@app.route('/static/uploads/installment_docs/<filename>')
@login_required
def uploaded_installment_doc(filename):
    """Serve uploaded installment documents (KTP, selfie). Admin or owner only."""
    # Check if admin or document owner
    if isinstance(current_user, Admin):
        return send_from_directory(UPLOAD_INSTALLMENT_DOCS, filename)

    # Check if user owns this document
    app_doc = InstallmentApplication.query.filter(
        or_(InstallmentApplication.ktp_photo == filename,
            InstallmentApplication.selfie_photo == filename)
    ).first()

    if app_doc and app_doc.user_id == current_user.id:
        return send_from_directory(UPLOAD_INSTALLMENT_DOCS, filename)

    return jsonify({'success': False, 'message': 'Akses ditolak'}), 403

# =============================================================================
# ENTRY POINT
# =============================================================================
if __name__ == '__main__':
    app.run(debug=False, host='0.0.0.0', port=8080)
