"""
Customer Chat AI — Pusat Gadai Indonesia (2026 Professional Edition)
====================================================================
Modul AI chat khusus PELANGGAN di halaman publik.

2026 Pro Features:
- Native tool calling via ai_service (no more regex [AKSI:...] parsing)
- Streaming & async support
- Cost tracking per conversation
- READ-ONLY actions via registered tool functions
- Session memory synced with ai_service
- Rate limiter with burst bucket
- Circuit breaker for external API calls
- PythonAnywhere free account compatibility (fallback mode)
- Graceful degradation when AI is unavailable
- Multimodal support (image analysis)
- Structured JSON outputs
- Reasoning tokens

PythonAnywhere Free Account Note:
- Free accounts block outbound requests to non-whitelisted APIs
- This module detects PythonAnywhere and falls back to local database mode
- AI features require paid PythonAnywhere account or local development
"""

import asyncio
import contextvars
import html
import json
import logging
import os
import re
import threading
import time
import uuid
from dataclasses import dataclass, field
from datetime import datetime
from typing import Any, AsyncGenerator, Dict, Generator, List, Optional, Tuple

try:
    from api.chat_memory import SessionMemory, CustomerMemory
except ImportError:
    # Fallback stubs — used only if chat_memory is unavailable (e.g. unit tests)
    import threading as _threading

    class SessionMemory:  # type: ignore
        MAX_HISTORY = 24
        _store: dict = {}
        _lock = _threading.Lock()

        @classmethod
        def get(cls, session_id: str) -> list:
            with cls._lock:
                return list(cls._store.get(str(session_id), []))

        @classmethod
        def append(cls, session_id: str, role: str, content: str, **kw) -> None:
            with cls._lock:
                key = str(session_id)
                history = list(cls._store.get(key, []))
                history.append({"role": role, "content": content})
                cls._store[key] = history[-cls.MAX_HISTORY:]

        @classmethod
        def clear(cls, session_id: str) -> None:
            with cls._lock:
                cls._store.pop(str(session_id), None)

        @classmethod
        def get_full_history(cls, session_id: str) -> list:
            return cls.get(session_id)

        @classmethod
        def cache_size(cls) -> int:
            return len(cls._store)

    class CustomerMemory:  # type: ignore
        @staticmethod
        def key_for_session(session_id: str) -> str:
            return f"sid:{session_id}"

        @classmethod
        def format_for_prompt(cls, customer_key: str) -> str:
            return ""

        @classmethod
        def merge_session_into_phone(cls, session_id: str, phone: str) -> None:
            pass


logger = logging.getLogger(__name__)

# Tool functions are called by ai_service with only the arguments the model
# supplied (no session_id) — this contextvar lets tool_simpan_preferensi and
# the identity-harvesting logic know which session/customer they belong to
# without changing ai_service's tool-dispatch signature.
_session_ctx: "contextvars.ContextVar[Optional[str]]" = contextvars.ContextVar(
    'cw_session_ctx', default=None
)

# ---------------------------------------------------------------------------
# Configuration
# ---------------------------------------------------------------------------

MAX_HISTORY = 24  # kept for backward compat; actual limit lives in api/chat_memory.py
RATE_LIMIT = 20
BURST_LIMIT = 5
CIRCUIT_BREAKER_THRESHOLD = 5
CIRCUIT_BREAKER_TIMEOUT = 300  # 5 minutes

# Detect PythonAnywhere environment
# NOTE: openrouter.ai IS whitelisted on PythonAnywhere free accounts
# So outbound AI calls should work. Fallback is only for actual failures.
IS_PYTHONANYWHERE = 'PYTHONANYWHERE_DOMAIN' in os.environ or 'PYTHONANYWHERE_SITE' in os.environ

# ---------------------------------------------------------------------------
# Circuit Breaker for External API Calls
# ---------------------------------------------------------------------------

class CircuitBreaker:
    """Circuit breaker pattern untuk melindungi dari API failure loops."""

    _states: Dict[str, Dict] = {}
    _lock = threading.Lock()

    @classmethod
    def is_open(cls, key: str) -> bool:
        with cls._lock:
            state = cls._states.get(key, {'state': 'closed', 'failures': 0})
            if state['state'] == 'open':
                if time.time() - state.get('last_failure', 0) > CIRCUIT_BREAKER_TIMEOUT:
                    cls._states[key] = {'state': 'half-open', 'failures': 0}
                    logger.info(f"Circuit breaker {key}: half-open")
                    return False
                return True
            return False

    @classmethod
    def record_success(cls, key: str):
        with cls._lock:
            cls._states[key] = {'state': 'closed', 'failures': 0}

    @classmethod
    def record_failure(cls, key: str):
        with cls._lock:
            state = cls._states.get(key, {'state': 'closed', 'failures': 0})
            state['failures'] += 1
            state['last_failure'] = time.time()
            if state['failures'] >= CIRCUIT_BREAKER_THRESHOLD:
                state['state'] = 'open'
                logger.warning(f"Circuit breaker {key}: OPENED after {state['failures']} failures")
            cls._states[key] = state


# ---------------------------------------------------------------------------
# System Prompt (2026 Pro: Tool-native + Structured Output)
# ---------------------------------------------------------------------------

SYSTEM_PROMPT = (
    'Anda adalah "Jaga", asisten AI PUSAT GADAI INDONESIA website toko.\n'
    'Anda membantu PELANGGAN dengan ramah, sopan, dan efisien.\n\n'

    '=== KEMAMPUAN ANDA ===\n'
    'Anda memiliki TOOLS yang dapat Anda panggil untuk mengambil data:\n'
    '• cek_resi — cek status pengiriman berdasarkan nomor resi/order/HP\n'
    '• order_status — tampilkan detail dan status pesanan\n'
    '• cek_order_pelanggan — cari semua pesanan berdasarkan nomor HP\n'
    '• generate_invoice — tampilkan ringkasan invoice/kwitansi\n'
    '• cek_produk — informasi produk, harga, dan stok\n'
    '• cek_status_kyc — cek status verifikasi identitas (KYC) berdasarkan nomor HP\n'
    '• cek_status_cicilan — cek status pengajuan cicilan/kredit & jadwal pembayaran '
    '(berdasarkan kode pengajuan atau nomor HP)\n'
    '• simpan_preferensi_pelanggan — catat fakta/preferensi pelanggan (nama panggilan, '
    'minat produk, budget, dll.) supaya diingat di percakapan berikutnya\n\n'

    '=== MEMORI & RIWAYAT ===\n'
    'Riwayat percakapan ini TERSIMPAN, termasuk lintas sesi/reload browser.\n'
    'Jika ada blok "MEMORI PELANGGAN" di awal percakapan, itu adalah fakta yang\n'
    'sudah pernah tersimpan — gunakan secara natural (mis. sapa dengan nama jika\n'
    'diketahui), JANGAN membacakannya mentah-mentah, dan JANGAN mengarang fakta baru.\n'
    'Saat pelanggan membagikan info penting untuk masa depan (nama, minat produk,\n'
    'budget, dsb.), panggil simpan_preferensi_pelanggan secara proaktif.\n\n'

    '=== ATURAN TOOL ===\n'
    '- Panggil tool HANYA jika user meminta data spesifik\n'
    '- Tanya info yang kurang SEBELUM memanggil tool\n'
    '- Jika tool mengembalikan error, sampaikan ke user dengan sopan\n'
    '- JANGAN mengarang data yang tidak ada di database\n'
    '- Untuk cek_status_kyc dan cek_status_cicilan, WAJIB verifikasi identitas via nomor HP\n'
    '  (atau kode pengajuan untuk cicilan) sebelum menampilkan data apa pun\n\n'

    '=== FORMAT RESPONS ===\n'
    '- Teks bersih dengan emoji (BUKAN HTML, BUKAN tag <b>, <br>, dll.)\n'
    '- Respons singkat maksimal 6 baris\n'
    '- Gunakan baris baru untuk poin penting\n'
    '- Bahasa Indonesia hangat dan profesional\n\n'

    '=== STATUS ORDER ===\n'
    'Pending=Menunggu Pembayaran, Paid=Lunas-Diproses, Processing=Diproses,\n'
    'Ready_to_Ship=Siap Kirim, Shipped=Dikirim, Out_for_Delivery=Diantarkan,\n'
    'Completed=Selesai, Cancelled=Dibatalkan, Returned=Dikembalikan\n\n'

    '=== PROSEDUR GADAI ===\n'
    'cicil: ajukan data → verifikasi kyc → review\n'
    'Penebusan: bayar total (pokok + bunga) sebelum jatuh tempo\n'

    '=== BATASAN KERAS ===\n'
    '- DILARANG aksi admin: update_resi, cancel_order, konfirmasi_pembayaran, dll.\n'
    '- JANGAN tampilkan data order pelanggan lain\n'
    '- Jika di luar kemampuan, arahkan ke WhatsApp admin\n'
    '- AI TIDAK BOLEH mengarang data'
)


# Fallback prompt untuk mode tanpa AI (PythonAnywhere free)
FALLBACK_SYSTEM_PROMPT = (
    'Anda adalah "Jaga", asisten virtual ATLANTIS CELULAR.\n'
    'Saat ini layanan AI sedang dalam mode terbatas.\n'
    'Saya dapat membantu dengan informasi dasar dari database lokal.\n\n'
    'Cara menggunakan:\n'
    '• Ketik nomor order untuk cek status pesanan\n'
    '• Ketik nomor resi untuk cek pengiriman\n'
    '• Ketik nomor HP untuk lihat pesanan Anda\n'
    '• Ketik nama produk untuk cek info produk\n'
    '• Ketik nomor HP untuk cek status verifikasi KYC\n'
    '• Ketik kode pengajuan (KRD-...) atau nomor HP untuk cek status cicilan\n\n'
    'Untuk bantuan lebih lanjut, hubungi admin via WhatsApp.'
)


# ---------------------------------------------------------------------------
# Response Models (2026 Pro: Structured)
# ---------------------------------------------------------------------------

@dataclass
class CustomerChatResult:
    success: bool
    reply: str
    session_id: str
    model: str = ""
    cost_usd: float = 0.0
    tool_calls: List[Dict[str, Any]] = field(default_factory=list)
    cached: bool = False
    error: Optional[str] = None
    source: str = "ai"  # "ai", "fallback", "local"
    structured_data: Optional[Dict] = None
    reasoning: Optional[str] = None

    def to_dict(self) -> Dict[str, Any]:
        return {
            "success": self.success,
            "reply": self.reply,
            "session_id": self.session_id,
            "model": self.model,
            "cost_usd": self.cost_usd,
            "tool_calls": self.tool_calls,
            "cached": self.cached,
            "error": self.error,
            "source": self.source,
            "structured_data": self.structured_data,
            "reasoning": self.reasoning,
        }


# ---------------------------------------------------------------------------
# Rate Limiter (Token Bucket with Burst)
# ---------------------------------------------------------------------------

class _RateLimiter:
    def __init__(self, rpm: int = 20, burst: int = 5):
        self._rpm = rpm
        self._burst = burst
        self._tokens: Dict[str, float] = {}
        self._last: Dict[str, float] = {}
        self._lock = threading.Lock()

    def is_allowed(self, key: str) -> bool:
        with self._lock:
            now = time.monotonic()
            last = self._last.get(key, now)
            elapsed = now - last
            tokens = min(
                self._rpm + self._burst,
                self._tokens.get(key, float(self._rpm)) + elapsed * (self._rpm / 60.0),
            )
            self._last[key] = now
            if tokens >= 1:
                self._tokens[key] = tokens - 1
                return True
            self._tokens[key] = tokens
            return False


_rate_limiter = _RateLimiter(rpm=RATE_LIMIT, burst=BURST_LIMIT)


# ---------------------------------------------------------------------------
# Formatting Helpers
# ---------------------------------------------------------------------------

def _fmt_price(amount) -> str:
    try:
        return 'Rp {:,.0f}'.format(int(amount)).replace(',', '.')
    except (TypeError, ValueError):
        return 'Rp {}'.format(amount)


def _fmt_date(dt) -> str:
    if not dt:
        return '-'
    try:
        return dt.strftime('%d %b %Y')
    except Exception:
        return str(dt)


def _fmt_datetime(dt) -> str:
    if not dt:
        return '-'
    try:
        return dt.strftime('%d %b %Y %H:%M')
    except Exception:
        return str(dt)


def _strip_html(text: str) -> str:
    clean = re.sub(r'<[^>]+>', '', text or '')
    return html.unescape(clean).strip()


# ---------------------------------------------------------------------------
# Database Helpers
# ---------------------------------------------------------------------------

def _resolve_order(identifier: str):
    if not identifier:
        return None
    try:
        from models import Order
        identifier = identifier.strip()
        order = Order.query.filter_by(order_number=identifier).first()
        if order:
            return order
        order = Order.query.filter_by(tracking_number=identifier).first()
        if order:
            return order
        try:
            order = Order.query.get(int(identifier))
            if order:
                return order
        except (ValueError, TypeError):
            pass
        if re.match(r'^[0-9+\-\s]{8,15}$', identifier):
            phone_clean = re.sub(r'[^0-9]', '', identifier)
            order = (
                Order.query
                .filter(Order.customer_phone.contains(phone_clean))
                .order_by(Order.created_at.desc())
                .first()
            )
            if order:
                return order
        return None
    except Exception as e:
        logger.error('_resolve_order error: %s', e)
        return None


def _resolve_product(identifier: str):
    """Resolve product by name, ID, or partial match."""
    if not identifier:
        return None
    try:
        from models import Product
        from sqlalchemy import or_
        identifier = identifier.strip()

        # Try exact match first
        product = Product.query.filter(
            or_(
                Product.name.ilike(f'%{identifier}%'),
                Product.brand.ilike(f'%{identifier}%'),
                Product.model.ilike(f'%{identifier}%')
            )
        ).first()

        if product:
            return product

        # Try ID
        try:
            product = Product.query.get(int(identifier))
            if product:
                return product
        except (ValueError, TypeError):
            pass

        return None
    except Exception as e:
        logger.error('_resolve_product error: %s', e)
        return None


# ---------------------------------------------------------------------------
# Status Maps
# ---------------------------------------------------------------------------

_STATUS_MAP = {
    'Pending': '⏳ Menunggu Pembayaran',
    'Paid': '💰 Lunas — Sedang Diproses',
    'Processing': '⚙️ Sedang Diproses',
    'Ready_to_Ship': '📦 Siap Dikirim',
    'Shipped': '🚚 Dalam Pengiriman',
    'Out_for_Delivery': '🛵 Dalam Pengantaran',
    'Completed': '✅ Selesai',
    'Cancelled': '❌ Dibatalkan',
    'Returned': '↩️ Dikembalikan',
    'Received': '📬 Sudah Diterima',
}

_TRACKING_EMOJI = {
    'pending': '📋',
    'picked_up': '📦',
    'in_transit': '🚚',
    'out_for_delivery': '🛵',
    'delivered': '✅',
    'failed_delivery': '❌',
    'returned': '↩️',
    'exception': '⚠️',
}


# ---------------------------------------------------------------------------
# Tool Functions (registered with ai_service)
# ---------------------------------------------------------------------------

def tool_cek_resi(value: str = "", tracking_number: str = "", order_id: str = "", phone: str = "") -> str:
    """Cek status pengiriman/resi berdasarkan nomor resi, order, atau HP."""
    identifier = (value or tracking_number or order_id or phone or "").strip()
    if not identifier:
        return (
            '❓ Mohon berikan salah satu info:\n'
            '• Nomor resi (misal: JNE123456789)\n'
            '• Nomor order (misal: INV-2025-001)\n'
            '• Nomor HP yang terdaftar saat checkout'
        )

    try:
        from models import ShippingTracking, ShippingTrackingEvent

        shipment = ShippingTracking.query.filter_by(tracking_number=identifier).first()

        if not shipment:
            order = _resolve_order(identifier)
            if order and order.tracking_number:
                shipment = ShippingTracking.query.filter_by(tracking_number=order.tracking_number).first()
                if not shipment:
                    return _format_simple_tracking(order)
            elif order and not order.tracking_number:
                return (
                    '📦 Order {} ditemukan.\n'
                    '📋 Status: {}\n\n'
                    '⏳ Nomor resi belum tersedia.\n'
                    'Hubungi admin untuk info lebih lanjut.'
                ).format(order.order_number, _STATUS_MAP.get(order.status, order.status))

        if not shipment:
            return (
                '❌ Resi "{}" tidak ditemukan di sistem.\n\n'
                'Pastikan nomor resi benar, atau coba dengan nomor order Anda.'
            ).format(identifier)

        return _format_detailed_tracking(shipment)

    except ImportError:
        order = _resolve_order(identifier)
        if not order:
            return '❌ Resi/order "{}" tidak ditemukan.'.format(identifier)
        return _format_simple_tracking(order)


def _format_simple_tracking(order) -> str:
    return (
        '📦 Info Order {}\n'
        '━━━━━━━━━━━━\n'
        '📋 Status: {}\n'
        '🔢 Resi  : {}\n'
        '🚚 Kurir : {}\n'
        '📅 Est.  : {}'
    ).format(
        order.order_number,
        _STATUS_MAP.get(order.status, order.status),
        order.tracking_number or 'Belum ada',
        order.carrier_name or '-',
        _fmt_date(order.estimated_delivery),
    )


def _format_detailed_tracking(shipment) -> str:
    from models import ShippingTrackingEvent

    events = (
        ShippingTrackingEvent.query
        .filter_by(shipping_tracking_id=shipment.id)
        .order_by(ShippingTrackingEvent.event_time.desc())
        .limit(4)
        .all()
    )

    emoji = _TRACKING_EMOJI.get((shipment.status or '').lower(), '📦')
    lines = [
        '📬 Info Resi Pengiriman',
        '━━━━━━━━━━━━',
        '🔢 Resi  : {}'.format(shipment.tracking_number),
        '🚚 Kurir : {}'.format(shipment.carrier or '-'),
        '{} Status: {}'.format(emoji, (shipment.status or '-').replace('_', ' ').title()),
    ]
    if shipment.current_location:
        lines.append('📍 Lokasi: {}'.format(shipment.current_location))
    if shipment.estimated_delivery:
        lines.append('📅 Est. Tiba: {}'.format(_fmt_date(shipment.estimated_delivery)))
    if shipment.actual_delivery:
        lines.append('✅ Diterima: {}'.format(_fmt_datetime(shipment.actual_delivery)))
    if getattr(shipment, 'is_delayed', False):
        lines.append('⚠️ Terlambat {} hari'.format(getattr(shipment, 'delay_days', 0) or 0))

    if events:
        lines.append('\n📋 Riwayat Terbaru:')
        for ev in events[:3]:
            loc_txt = ' [{}]'.format(ev.location) if ev.location else ''
            lines.append(
                '• {} — {}{}'.format(
                    _fmt_datetime(ev.event_time),
                    (ev.status or '').replace('_', ' ').title(),
                    loc_txt,
                )
            )

    return '\n'.join(lines)


def tool_order_status(value: str = "", order_id: str = "") -> str:
    """Tampilkan detail dan status pesanan."""
    identifier = (value or order_id or "").strip()
    if not identifier:
        return (
            '❓ Mohon berikan nomor order (contoh: INV-2025-001)\n'
            'atau nomor HP yang terdaftar saat checkout.'
        )

    order = _resolve_order(identifier)
    if not order:
        return (
            '❌ Order "{}" tidak ditemukan.\n\n'
            'Coba dengan nomor order lengkap atau nomor HP Anda.'
        ).format(identifier)

    resi_line = ''
    if order.tracking_number:
        resi_line = '\n🔢 Resi  : {} ({})'.format(
            order.tracking_number, order.carrier_name or '-'
        )

    items_text = ''
    if order.items:
        items_text = '\n\n📋 Item Pesanan:\n' + '\n'.join(
            ['• {} ×{}'.format(item.name, item.quantity) for item in order.items[:5]]
        )
        if len(order.items) > 5:
            items_text += '\n• ... dan {} item lainnya'.format(len(order.items) - 5)

    return (
        '📦 Detail Pesanan\n'
        '━━━━━━━━━━━━\n'
        '📌 {}\n'
        '💰 Total  : {}\n'
        '📋 Status : {}\n'
        '💳 Bayar  : {}\n'
        '📅 Tanggal: {}'
        '{}'
        '{}'
    ).format(
        order.order_number,
        _fmt_price(order.total),
        _STATUS_MAP.get(order.status, order.status),
        order.payment_method or '-',
        _fmt_datetime(order.created_at),
        resi_line,
        items_text,
    )


def tool_cek_order_pelanggan(phone: str = "", value: str = "") -> str:
    """Cari semua pesanan pelanggan berdasarkan nomor HP."""
    phone = (phone or value or "").strip()
    phone_clean = re.sub(r'[^0-9]', '', phone)

    if not phone_clean or len(phone_clean) < 8:
        return (
            '❓ Mohon berikan nomor HP yang terdaftar saat checkout\n'
            '(minimal 8 digit, contoh: 08123456789)'
        )

    try:
        from models import Order

        orders = (
            Order.query.filter(Order.customer_phone.contains(phone_clean))
            .order_by(Order.created_at.desc())
            .limit(5)
            .all()
        )
    except Exception as e:
        logger.error('tool_cek_order_pelanggan error: %s', e)
        return '❌ Terjadi kesalahan saat mengambil data.'

    if not orders:
        return (
            '❌ Tidak ada pesanan untuk nomor HP {}.\n\n'
            'Pastikan nomor HP sesuai dengan yang digunakan saat checkout.'
        ).format(phone)

    lines = [
        '📋 Pesanan untuk {} ({})'.format(orders[0].customer_name, phone),
        '━━━━━━━━━━━━',
    ]
    for o in orders:
        resi = '\n  🔢 Resi: {}'.format(o.tracking_number) if o.tracking_number else ''
        lines.append(
            '\n📌 {}\n'
            '  💰 {}\n'
            '  📋 {}\n'
            '  📅 {}'
            '{}'.format(
                o.order_number,
                _fmt_price(o.total),
                _STATUS_MAP.get(o.status, o.status),
                _fmt_date(o.created_at),
                resi,
            )
        )

    lines.append('\nℹ️ Ketik nomor order untuk detail atau invoice.')
    return '\n'.join(lines)


def tool_generate_invoice(order_id: str = "", value: str = "") -> str:
    """Tampilkan ringkasan invoice/kwitansi."""
    identifier = (order_id or value or "").strip()
    if not identifier:
        return '❓ Mohon berikan nomor order untuk menampilkan invoice.'

    order = _resolve_order(identifier)
    if not order:
        return '❌ Order "{}" tidak ditemukan.'.format(identifier)

    items_text = ''
    if order.items:
        for item in order.items:
            subtotal = _fmt_price(getattr(item, 'price', 0) * getattr(item, 'quantity', 1))
            items_text += '\n• {} ×{} = {}'.format(item.name, item.quantity, subtotal)

    return (
        '🧾 Invoice — {}\n'
        '━━━━━━━━━━━━\n'
        '👤 {}\n'
        '📅 {}\n\n'
        'Item:{}\n\n'
        '💰 Subtotal : {}\n'
        '🚚 Ongkir   : {}\n'
        '🏷️ Diskon   : {}\n'
        '━━━━━━━━━━━━\n'
        '💵 Total    : {}\n'
        '💳 Pembayaran: {}\n\n'
        '🔗 Invoice lengkap: /invoice/{}'
    ).format(
        order.order_number,
        order.customer_name,
        _fmt_datetime(order.created_at),
        items_text,
        _fmt_price(order.subtotal),
        _fmt_price(order.shipping_cost),
        _fmt_price(order.discount),
        _fmt_price(order.total),
        order.payment_method or '-',
        order.id,
    )


def tool_cek_produk(product_name: str = "", product_id: str = "", category: str = "") -> str:
    """Cek informasi produk, harga, dan stok."""
    identifier = (product_name or product_id or "").strip()
    if not identifier:
        return (
            '❓ Mohon berikan nama produk atau ID produk.\n'
            'Contoh: "iPhone 13", "Samsung Galaxy", atau ID produk.'
        )

    product = _resolve_product(identifier)
    if not product:
        return (
            '❌ Produk "{}" tidak ditemukan.\n\n'
            'Coba dengan nama produk yang lain atau periksa ejaan.'
        ).format(identifier)

    stock_status = '✅ Tersedia' if getattr(product, 'stock', 0) > 0 else '❌ Habis'
    condition = getattr(product, 'condition', '-')
    brand = getattr(product, 'brand', '-')
    model = getattr(product, 'model', '-')

    specs = []
    if brand and brand != '-':
        specs.append(f"🏷️ Merk: {brand}")
    if model and model != '-':
        specs.append(f"📱 Model: {model}")
    if condition and condition != '-':
        specs.append(f"⭐ Kondisi: {condition}")

    specs_text = '\n'.join(specs) if specs else ''

    return (
        '📱 Info Produk\n'
        '━━━━━━━━━━━━\n'
        '🏷️ {}\n'
        '💰 Harga : {}\n'
        '📊 Stok  : {} ({})\n'
        '{}\n\n'
        '🔗 Lihat produk: /product/{}'
    ).format(
        product.name,
        _fmt_price(getattr(product, 'price', 0)),
        getattr(product, 'stock', 0),
        stock_status,
        specs_text + '\n' if specs_text else '',
        product.id,
    )


_INSTALLMENT_STATUS_MAP = {
    'pending': '⏳ Menunggu Review',
    'reviewing': '🔍 Sedang Direview',
    'approved': '✅ Disetujui',
    'active': '📆 Aktif Berjalan',
    'completed': '🎉 Lunas',
    'rejected': '❌ Ditolak',
    'cancelled': '🚫 Dibatalkan',
}


def tool_cek_status_kyc(phone: str = "", value: str = "") -> str:
    """Cek status verifikasi KYC (identitas) pelanggan berdasarkan nomor HP akun."""
    phone_raw = (phone or value or "").strip()
    phone_clean = re.sub(r'[^0-9]', '', phone_raw)

    if not phone_clean or len(phone_clean) < 8:
        return (
            '❓ Mohon berikan nomor HP yang terdaftar di akun Anda\n'
            '(minimal 8 digit, contoh: 08123456789)'
        )

    try:
        from models import User, KYCVerificationSession

        user = (
            User.query.filter(User.phone.contains(phone_clean))
            .order_by(User.id.desc())
            .first()
        )
    except Exception as e:
        logger.error('tool_cek_status_kyc error: %s', e)
        return '❌ Terjadi kesalahan saat mengambil data.'

    if not user:
        return (
            '❌ Akun dengan nomor HP {} tidak ditemukan.\n\n'
            'Pastikan nomor HP sesuai dengan yang terdaftar saat registrasi.'
        ).format(phone_raw)

    pending = (
        KYCVerificationSession.query
        .filter_by(user_id=user.id, status='pending_review')
        .order_by(KYCVerificationSession.id.desc())
        .first()
    )
    if pending:
        submitted_at = getattr(pending, 'created_at', None)
        return (
            '🔐 Status Verifikasi KYC\n'
            '━━━━━━━━━━━━\n'
            '👤 {}\n'
            '⏳ Status: Menunggu Review Admin\n'
            '{}'
            '\nTim kami akan mereview dalam 1×24 jam.'
        ).format(
            user.full_name,
            '📅 Diajukan: {}\n'.format(_fmt_datetime(submitted_at)) if submitted_at else '',
        )

    is_expired = False
    if getattr(user, 'kyc_verified', False):
        latest = (
            KYCVerificationSession.query
            .filter_by(user_id=user.id, status='verified')
            .order_by(KYCVerificationSession.verified_at.desc())
            .first()
        )
        if latest and getattr(latest, 'expires_at', None):
            is_expired = datetime.utcnow() > latest.expires_at

    if user.kyc_verified and not is_expired:
        return (
            '🔐 Status Verifikasi KYC\n'
            '━━━━━━━━━━━━\n'
            '👤 {}\n'
            '✅ Status: Terverifikasi\n'
            '📅 Sejak: {}\n\n'
            'Akun Anda sudah bisa mengajukan cicilan/gadai.'
        ).format(user.full_name, _fmt_date(getattr(user, 'kyc_verified_at', None)))

    if is_expired:
        return (
            '🔐 Status Verifikasi KYC\n'
            '━━━━━━━━━━━━\n'
            '👤 {}\n'
            '⚠️ Status: Kedaluwarsa\n\n'
            'Verifikasi Anda sudah kedaluwarsa. Silakan ajukan verifikasi ulang di halaman akun Anda.'
        ).format(user.full_name)

    return (
        '🔐 Status Verifikasi KYC\n'
        '━━━━━━━━━━━━\n'
        '👤 {}\n'
        '❌ Status: Belum Terverifikasi\n\n'
        'Silakan lakukan verifikasi KYC di halaman akun sebelum mengajukan cicilan/gadai.'
    ).format(user.full_name)


def tool_cek_status_cicilan(phone: str = "", value: str = "", application_code: str = "") -> str:
    """Cek status pengajuan dan jadwal pembayaran cicilan/kredit berdasarkan kode pengajuan atau nomor HP."""
    code = (application_code or "").strip()
    phone_raw = (phone or (value if not code else "") or "").strip()
    phone_clean = re.sub(r'[^0-9]', '', phone_raw)

    if not code and (not phone_clean or len(phone_clean) < 8):
        return (
            '❓ Mohon berikan salah satu info:\n'
            '• Kode pengajuan (misal: KRD-20260101-AB12)\n'
            '• Nomor HP yang terdaftar (minimal 8 digit)'
        )

    try:
        from models import InstallmentApplication, InstallmentSchedule, Product

        if code:
            applications = InstallmentApplication.query.filter_by(application_code=code).all()
        else:
            applications = (
                InstallmentApplication.query
                .filter(InstallmentApplication.phone.contains(phone_clean))
                .order_by(InstallmentApplication.created_at.desc())
                .limit(3)
                .all()
            )
    except Exception as e:
        logger.error('tool_cek_status_cicilan error: %s', e)
        return '❌ Terjadi kesalahan saat mengambil data.'

    if not applications:
        return (
            '❌ Tidak ada pengajuan cicilan ditemukan.\n\n'
            'Pastikan kode pengajuan atau nomor HP sudah benar.'
        )

    blocks = []
    for app_row in applications:
        status_txt = _INSTALLMENT_STATUS_MAP.get(app_row.status, app_row.status)
        product = None
        if getattr(app_row, 'product_id', None):
            try:
                product = Product.query.get(app_row.product_id)
            except Exception:
                product = None
        product_name = product.name if product else '-'

        lines = [
            '💳 Pengajuan {}'.format(app_row.application_code),
            '━━━━━━━━━━━━',
            '📱 {}'.format(product_name),
            '📋 Status: {}'.format(status_txt),
            '💰 Cicilan: {}/bulan × {} bulan'.format(
                _fmt_price(getattr(app_row, 'monthly_installment', 0) or 0),
                getattr(app_row, 'tenor_months', '-'),
            ),
            '📅 Diajukan: {}'.format(_fmt_date(getattr(app_row, 'created_at', None))),
        ]

        if app_row.status == 'rejected' and getattr(app_row, 'rejection_reason', None):
            lines.append('📝 Alasan: {}'.format(app_row.rejection_reason))

        if app_row.status in ('approved', 'active'):
            schedules = (
                InstallmentSchedule.query
                .filter_by(application_id=app_row.id)
                .order_by(InstallmentSchedule.installment_number)
                .all()
            )
            if schedules:
                paid = [s for s in schedules if s.status == 'paid']
                unpaid = [s for s in schedules if s.status != 'paid']
                lines.append('✅ Terbayar: {}/{} cicilan'.format(len(paid), len(schedules)))
                if unpaid:
                    nxt = unpaid[0]
                    lines.append(
                        '⏭️ Jatuh Tempo Berikutnya: {} — {}'.format(
                            _fmt_date(nxt.due_date), _fmt_price(nxt.amount)
                        )
                    )
                else:
                    lines.append('🎉 Semua cicilan sudah lunas!')

        blocks.append('\n'.join(lines))

    return '\n\n'.join(blocks)


# ---------------------------------------------------------------------------
# Tool Schema Definitions (OpenAI-compatible)
# ---------------------------------------------------------------------------

TOOLS_SCHEMA = [
    {
        "type": "function",
        "function": {
            "name": "cek_resi",
            "description": "Cek status pengiriman/resi berdasarkan nomor resi, nomor order, atau nomor HP pelanggan.",
            "parameters": {
                "type": "object",
                "properties": {
                    "value": {
                        "type": "string",
                        "description": "Nomor resi, order, atau HP",
                    },
                    "tracking_number": {
                        "type": "string",
                        "description": "Nomor resi pengiriman",
                    },
                    "order_id": {
                        "type": "string",
                        "description": "Nomor order/invoice",
                    },
                    "phone": {
                        "type": "string",
                        "description": "Nomor HP pelanggan",
                    },
                },
            },
        },
    },
    {
        "type": "function",
        "function": {
            "name": "order_status",
            "description": "Tampilkan detail dan status pesanan berdasarkan nomor order atau HP.",
            "parameters": {
                "type": "object",
                "properties": {
                    "value": {
                        "type": "string",
                        "description": "Nomor order atau HP",
                    },
                    "order_id": {
                        "type": "string",
                        "description": "Nomor order/invoice",
                    },
                },
            },
        },
    },
    {
        "type": "function",
        "function": {
            "name": "cek_order_pelanggan",
            "description": "Cari semua pesanan pelanggan berdasarkan nomor HP.",
            "parameters": {
                "type": "object",
                "properties": {
                    "phone": {
                        "type": "string",
                        "description": "Nomor HP pelanggan",
                    },
                    "value": {
                        "type": "string",
                        "description": "Nomor HP pelanggan (alias)",
                    },
                },
                "required": ["phone"],
            },
        },
    },
    {
        "type": "function",
        "function": {
            "name": "generate_invoice",
            "description": "Tampilkan ringkasan invoice/kwitansi pesanan.",
            "parameters": {
                "type": "object",
                "properties": {
                    "order_id": {
                        "type": "string",
                        "description": "Nomor order/invoice",
                    },
                    "value": {
                        "type": "string",
                        "description": "Nomor order (alias)",
                    },
                },
                "required": ["order_id"],
            },
        },
    },
    {
        "type": "function",
        "function": {
            "name": "cek_produk",
            "description": "Cek informasi produk, harga, dan stok berdasarkan nama atau ID produk.",
            "parameters": {
                "type": "object",
                "properties": {
                    "product_name": {
                        "type": "string",
                        "description": "Nama produk",
                    },
                    "product_id": {
                        "type": "string",
                        "description": "ID produk",
                    },
                    "category": {
                        "type": "string",
                        "description": "Kategori produk",
                    },
                },
            },
        },
    },
    {
        "type": "function",
        "function": {
            "name": "cek_status_kyc",
            "description": "Cek status verifikasi identitas (KYC) pelanggan berdasarkan nomor HP akun: belum verifikasi, menunggu review, terverifikasi, atau kedaluwarsa.",
            "parameters": {
                "type": "object",
                "properties": {
                    "phone": {
                        "type": "string",
                        "description": "Nomor HP yang terdaftar di akun",
                    },
                    "value": {
                        "type": "string",
                        "description": "Nomor HP (alias)",
                    },
                },
                "required": ["phone"],
            },
        },
    },
    {
        "type": "function",
        "function": {
            "name": "cek_status_cicilan",
            "description": "Cek status pengajuan cicilan/kredit gadai dan jadwal pembayarannya (jumlah terbayar, jatuh tempo berikutnya) berdasarkan kode pengajuan atau nomor HP.",
            "parameters": {
                "type": "object",
                "properties": {
                    "application_code": {
                        "type": "string",
                        "description": "Kode pengajuan cicilan, contoh: KRD-20260101-AB12",
                    },
                    "phone": {
                        "type": "string",
                        "description": "Nomor HP yang terdaftar",
                    },
                    "value": {
                        "type": "string",
                        "description": "Nomor HP atau kode pengajuan (alias)",
                    },
                },
            },
        },
    },
    {
        "type": "function",
        "function": {
            "name": "simpan_preferensi_pelanggan",
            "description": (
                "Simpan fakta atau preferensi penting tentang pelanggan (nama panggilan, "
                "minat produk, budget, kebutuhan khusus, dsb.) agar diingat di percakapan "
                "berikutnya. Panggil ini secara proaktif saat pelanggan membagikan info "
                "yang relevan untuk masa depan — jangan tanya izin dulu, cukup catat."
            ),
            "parameters": {
                "type": "object",
                "properties": {
                    "fact": {
                        "type": "string",
                        "description": "Label singkat fakta, contoh: 'nama', 'minat_produk', 'budget'",
                    },
                    "value": {
                        "type": "string",
                        "description": "Isi fakta/preferensi yang ingin diingat",
                    },
                },
                "required": ["fact", "value"],
            },
        },
    },
]


# ---------------------------------------------------------------------------
# Service Initializer
# ---------------------------------------------------------------------------

def _init_tools():
    """Register tools with ai_service singleton (idempotent)."""
    try:
        from api.ai_service import get_service

        svc = get_service()
        if not getattr(svc, '_customer_tools_registered', False):
            svc.register_tool('cek_resi', tool_cek_resi)
            svc.register_tool('order_status', tool_order_status)
            svc.register_tool('cek_order_pelanggan', tool_cek_order_pelanggan)
            svc.register_tool('generate_invoice', tool_generate_invoice)
            svc.register_tool('cek_produk', tool_cek_produk)
            svc.register_tool('cek_status_kyc', tool_cek_status_kyc)
            svc.register_tool('cek_status_cicilan', tool_cek_status_cicilan)
            svc.register_tool('simpan_preferensi_pelanggan', tool_simpan_preferensi)
            svc._customer_tools_registered = True
            logger.info('Customer chat tools registered with ai_service')
    except Exception as e:
        logger.warning('Failed to register tools (expected on PythonAnywhere free): %s', e)


def tool_simpan_preferensi(fact: str = "", value: str = "") -> str:
    """Simpan fakta/preferensi penting tentang pelanggan (nama panggilan, minat produk,
    budget, dll.) agar AI mengingatnya di percakapan berikutnya.

    2026 Pro fix: fungsi ini SEBELUMNYA didefinisikan ~200 baris DI BAWAH
    `_LEGACY_ACTION_MAP` (di bagian "Customer identity / memory helpers"),
    padahal `_LEGACY_ACTION_MAP` adalah dict level-modul yang langsung
    mereferensikan `tool_simpan_preferensi` saat modul di-import. Urutan
    itu menyebabkan `NameError: name 'tool_simpan_preferensi' is not
    defined` SETIAP KALI modul ini di-import — dan karena NameError bukan
    ImportError, `app.py` (yang membungkus import modul ini dengan
    `except ImportError`) tidak menangkapnya, sehingga SELURUH aplikasi
    Flask gagal start. Fix: pindahkan definisi ke sini, sebelum
    `_LEGACY_ACTION_MAP` didefinisikan. Pemanggilan `_resolve_customer_key`
    di dalam body function ini aman walau definisinya ada di bawah, karena
    baru dieksekusi saat function dipanggil (runtime), bukan saat modul
    di-import.
    """
    session_id = _session_ctx.get()
    content = (value or '').strip()
    label = re.sub(r'\s+', '_', (fact or 'catatan').strip().lower())[:80]
    if not session_id or not content or not label:
        return '(tidak disimpan — info tidak lengkap)'
    key = _resolve_customer_key(session_id)
    CustomerMemory.remember(key, label, content, fact_type='preferensi', source_session=str(session_id))
    return '✅ Dicatat, akan saya ingat.'


# ---------------------------------------------------------------------------
# Legacy Action Parser (fallback for non-tool models)
# ---------------------------------------------------------------------------

def _extract_legacy_action(text: str):
    match = re.search(r'\[AKSI:([a-z_]+)(?:\|([^\]]*))?\]', text)
    if not match:
        return None, {}
    action_name = match.group(1)
    params: Dict[str, str] = {}
    raw_params = match.group(2) or ''
    for part in raw_params.split('|'):
        if '=' in part:
            k, v = part.split('=', 1)
            params[k.strip()] = v.strip()
    return action_name, params


_LEGACY_ACTION_MAP = {
    'cek_resi': tool_cek_resi,
    'order_status': tool_order_status,
    'cek_order_pelanggan': tool_cek_order_pelanggan,
    'generate_invoice': tool_generate_invoice,
    'cek_produk': tool_cek_produk,
    'cek_status_kyc': tool_cek_status_kyc,
    'cek_status_cicilan': tool_cek_status_cicilan,
    'simpan_preferensi_pelanggan': tool_simpan_preferensi,
}


def _execute_legacy_action(action_name: str, params: Dict[str, str]) -> str:
    if action_name not in _LEGACY_ACTION_MAP:
        return (
            '⚠️ Permintaan ini hanya bisa dilakukan oleh admin.\n'
            'Silakan hubungi kami via WhatsApp untuk bantuan lebih lanjut.'
        )
    try:
        fn = _LEGACY_ACTION_MAP[action_name]
        return fn(**params)
    except Exception as e:
        logger.error('legacy action error [%s]: %s', action_name, e, exc_info=True)
        return '❌ Terjadi kesalahan saat mengambil data. Silakan coba lagi.'


# ---------------------------------------------------------------------------
# Intent Detection (Local, no AI needed)
# ---------------------------------------------------------------------------

_INTENT_PATTERNS = {
    'cek_resi': [
        r'resi', r'pengiriman', r'kurir', r'jne', r'jnt', r'sicepat', r'anteraja',
        r'pos', r'tiki', r'nomor resi', r'cek resi', r'where is my', r'tracking'
    ],
    'order_status': [
        r'order', r'pesanan', r'status', r'invoice', r'cek order', r'cek pesanan',
        r'where is my order', r'nomor order'
    ],
    'cek_order_pelanggan': [
        r'pesanan saya', r'order saya', r'riwayat', r'history', r'nomor hp',
        r'no hp', r'telepon', r'phone'
    ],
    'generate_invoice': [
        r'invoice', r'kwitansi', r'nota', r'bukti pembayaran', r'print invoice'
    ],
    'cek_produk': [
        r'produk', r'harga', r'stok', r'barang', r'iphone', r'samsung', r'hp',
        r'laptop', r'tersedia', r'ada', r'berapa'
    ],
    'cek_status_kyc': [
        r'kyc', r'verifikasi', r'ktp', r'selfie', r'identitas', r'status verifikasi'
    ],
    'cek_status_cicilan': [
        r'cicilan', r'kredit', r'angsuran', r'gadai', r'krd-', r'tenor',
        r'jatuh tempo', r'pengajuan'
    ],
}


def _detect_intent(message: str) -> Tuple[Optional[str], Optional[str]]:
    """
    Detect user intent and extract identifier from message.
    Returns: (intent_name, identifier) or (None, None)
    """
    msg_lower = message.lower()

    # Extract potential identifiers (order numbers, phone numbers, tracking numbers)
    # Order number pattern: INV-XXXX or numbers
    order_match = re.search(r'(INV-?[\w\-]+|\b\d{6,}\b)', message, re.IGNORECASE)
    # Phone pattern
    phone_match = re.search(r'(\+?62|0)[\d\s\-]{8,15}', message)
    # Resi pattern (alphanumeric, often with courier prefix)
    resi_match = re.search(r'\b([A-Z]{2,4}\d{8,})\b', message, re.IGNORECASE)

    identifier = None
    if order_match:
        identifier = order_match.group(1)
    elif phone_match:
        identifier = phone_match.group(0)
    elif resi_match:
        identifier = resi_match.group(1)

    # Detect intent
    for intent, patterns in _INTENT_PATTERNS.items():
        for pattern in patterns:
            if pattern in msg_lower:
                return intent, identifier

    return None, identifier


# ---------------------------------------------------------------------------
# Local Fallback Response Generator (PythonAnywhere Free Mode)
# ---------------------------------------------------------------------------

def _generate_fallback_response(user_message: str, session_id: str) -> CustomerChatResult:
    """
    Generate response using local database without external AI.
    Used when AI service is temporarily unavailable (circuit breaker open or connection failure).
    Note: openrouter.ai IS whitelisted on PythonAnywhere, so this is a rare fallback.
    """
    intent, identifier = _detect_intent(user_message)

    if intent and identifier:
        # Execute tool directly
        result = _execute_legacy_action(intent, {'value': identifier})
        return CustomerChatResult(
            success=True,
            reply=result,
            session_id=session_id,
            model="local",
            cost_usd=0.0,
            source="local",
        )
    elif intent:
        # Need more info
        prompts = {
            'cek_resi': '❓ Mohon berikan nomor resi, nomor order, atau nomor HP Anda.',
            'order_status': '❓ Mohon berikan nomor order atau nomor HP Anda.',
            'cek_order_pelanggan': '❓ Mohon berikan nomor HP yang terdaftar (minimal 8 digit).',
            'generate_invoice': '❓ Mohon berikan nomor order untuk generate invoice.',
            'cek_produk': '❓ Mohon berikan nama produk yang ingin dicek.',
            'cek_status_kyc': '❓ Mohon berikan nomor HP yang terdaftar di akun Anda.',
            'cek_status_cicilan': '❓ Mohon berikan kode pengajuan atau nomor HP Anda.',
        }
        return CustomerChatResult(
            success=True,
            reply=prompts.get(intent, '❓ Mohon berikan informasi yang diperlukan.'),
            session_id=session_id,
            model="local",
            cost_usd=0.0,
            source="local",
        )
    else:
        # General response
        return CustomerChatResult(
            success=True,
            reply=(
                '👋 Halo! Saya Jaga, asisten virtual Pusat Gadai Indonesia.\n\n'
                'Saya dapat membantu Anda dengan:\n'
                '• 📦 Cek status pengiriman/resi\n'
                '• 📋 Cek status pesanan\n'
                '• 🧾 Generate invoice\n'
                '• 📱 Cek info produk dan stok\n'
                '• 🔐 Cek status verifikasi KYC\n'
                '• 💳 Cek status pengajuan cicilan/kredit\n\n'
                'Silakan ketik apa yang Anda butuhkan!'
            ),
            session_id=session_id,
            model="local",
            cost_usd=0.0,
            source="local",
        )


# ---------------------------------------------------------------------------
# Customer identity / memory helpers
# ---------------------------------------------------------------------------
# SessionMemory (conversation turns) and CustomerMemory (long-term facts)
# are imported from api.chat_memory — both DB-backed with in-memory caching.
# See that module for details. The helpers below wire them into this
# module's tool-calling flow.

_IDENTITY_TOOLS = {'order_status', 'cek_order_pelanggan', 'cek_status_kyc', 'cek_status_cicilan', 'cek_resi'}


def _resolve_customer_key(session_id: str) -> str:
    """Best available memory key for a session: the permanent phone-based
    key if this conversation has already identified the customer, else a
    temporary session-scoped key."""
    try:
        from models import ChatConversation
        conv = ChatConversation.query.filter_by(session_id=str(session_id)).first()
        if conv and conv.customer_key:
            return conv.customer_key
    except Exception:
        pass
    return CustomerMemory.key_for_session(session_id)


def _harvest_identity_from_tool_calls(session_id: str, tool_calls: Optional[List[Dict]]) -> None:
    """If the model just called a tool with a phone number, link this
    session's memory to that phone number so facts persist across future
    sessions/devices from the same customer."""
    if not tool_calls:
        return
    for tc in tool_calls:
        fn = tc.get('function', {}) if isinstance(tc, dict) else {}
        name = fn.get('name')
        if name not in _IDENTITY_TOOLS:
            continue
        try:
            args = json.loads(fn.get('arguments') or '{}')
        except Exception:
            continue
        phone_raw = args.get('phone') or args.get('value') or ''
        phone_clean = re.sub(r'[^0-9]', '', phone_raw)
        if len(phone_clean) >= 8:
            try:
                CustomerMemory.merge_session_into_phone(session_id, phone_clean)
            except Exception as e:
                logger.warning('identity harvest failed for %s: %s', session_id, e)
            return


# ---------------------------------------------------------------------------
# Core Processing
# ---------------------------------------------------------------------------

def _build_messages(user_message: str, session_id: str, image: Optional[str] = None) -> List[Dict]:
    """Build message list with system prompt, known customer memory, and history."""
    messages = [{'role': 'system', 'content': SYSTEM_PROMPT}]

    memory_block = CustomerMemory.format_for_prompt(_resolve_customer_key(session_id))
    if memory_block:
        messages.append({'role': 'system', 'content': memory_block})

    messages.extend(SessionMemory.get(session_id))

    # Multimodal support: if image provided, use vision format
    if image:
        user_msg = {
            'role': 'user',
            'content': [
                {'type': 'text', 'text': user_message},
                {'type': 'image_url', 'image_url': {'url': image, 'detail': 'auto'}}
            ]
        }
    else:
        user_msg = {'role': 'user', 'content': user_message}

    messages.append(user_msg)
    return messages


def _build_fallback_messages(user_message: str, session_id: str) -> List[Dict]:
    """Build message list for fallback mode (no AI)."""
    messages = [{'role': 'system', 'content': FALLBACK_SYSTEM_PROMPT}]
    messages.extend(SessionMemory.get(session_id))
    messages.append({'role': 'user', 'content': user_message})
    return messages


# ---------------------------------------------------------------------------
# Main Entry Points (2026 Pro)
# ---------------------------------------------------------------------------

def process_customer_message(
    user_message: str,
    session_id: Optional[str] = None,
    image: Optional[str] = None,
    *,
    use_tools: bool = True,
) -> Dict[str, Any]:
    """
    Process a customer message (2026 Professional Edition).

    Args:
        user_message: Text from the customer
        session_id: Browser session UUID (auto-generated if None)
        image: Base64 image data or URL for multimodal support
        use_tools: Enable native tool calling (requires tool-capable model)

    Returns:
        Dict with success, reply, session_id, model, cost_usd, source, etc.
    """
    if not session_id:
        session_id = 'cw_' + str(uuid.uuid4()).replace('-', '')[:16]

    if not _rate_limiter.is_allowed(session_id):
        return CustomerChatResult(
            success=False,
            reply='⚠️ Terlalu banyak pesan. Mohon tunggu sebentar lalu coba lagi.',
            session_id=session_id,
            source="rate_limited",
        ).to_dict()

    # Check circuit breaker (only skip if consistently failing)
    if CircuitBreaker.is_open('openrouter_chat'):
        logger.warning("Circuit breaker open for openrouter_chat, using fallback")
        result = _generate_fallback_response(user_message, session_id)
        SessionMemory.append(session_id, 'user', user_message)
        SessionMemory.append(session_id, 'assistant', result.reply)
        return result.to_dict()

    # On PythonAnywhere, openrouter.ai IS whitelisted for free accounts
    # So we try AI first, fallback only on actual connection failure

    _init_tools()

    # Try AI service first
    try:
        from api.ai_service import get_service

        svc = get_service()
        messages = _build_messages(user_message, session_id, image)

        _ctx_token = _session_ctx.set(session_id)
        try:
            if use_tools:
                result = svc.chat_completion(
                    messages,
                    temperature=0.6,
                    max_tokens=700,
                    use_cache=False,
                    rate_key=session_id,
                    tools=TOOLS_SCHEMA,
                    execute_tools=True,
                )
            else:
                result = svc.chat_completion(
                    messages,
                    temperature=0.6,
                    max_tokens=700,
                    use_cache=False,
                    rate_key=session_id,
                )
        finally:
            _session_ctx.reset(_ctx_token)

        if result.success:
            CircuitBreaker.record_success('openrouter_chat')
            reply = _strip_html(result.reply)

            # Fallback: parse legacy [AKSI:...] tags if tools weren't used
            if not result.tool_calls and '[' in reply:
                action_name, params = _extract_legacy_action(reply)
                if action_name:
                    clean_reply = re.sub(r'\s*\[AKSI:[^\]]+\]', '', reply).strip()
                    action_result = _execute_legacy_action(action_name, params)
                    reply = (
                        '{}\n\n{}'.format(clean_reply, action_result)
                        if clean_reply
                        else action_result
                    )

            _harvest_identity_from_tool_calls(session_id, result.tool_calls)

            cost_usd = result.cost.cost_usd if result.cost else 0.0

            SessionMemory.append(session_id, 'user', user_message, source='user')
            SessionMemory.append(
                session_id, 'assistant', reply,
                model=result.model, cost_usd=cost_usd,
                tool_calls=result.tool_calls, source='ai',
            )

            return CustomerChatResult(
                success=True,
                reply=reply.strip(),
                session_id=session_id,
                model=result.model,
                cost_usd=cost_usd,
                tool_calls=result.tool_calls or [],
                cached=result.cached,
                source="ai",
            ).to_dict()
        else:
            # AI returned error, record failure
            CircuitBreaker.record_failure('openrouter_chat')
            logger.warning(f"AI service error: {result.error}")

            # Fallback to local mode
            fallback = _generate_fallback_response(user_message, session_id)
            SessionMemory.append(session_id, 'user', user_message)
            SessionMemory.append(session_id, 'assistant', fallback.reply)
            return fallback.to_dict()

    except ImportError as e:
        logger.warning(f"ai_service not available (expected on PythonAnywhere free): {e}")
        # Use local fallback mode
        fallback = _generate_fallback_response(user_message, session_id)
        SessionMemory.append(session_id, 'user', user_message)
        SessionMemory.append(session_id, 'assistant', fallback.reply)
        return fallback.to_dict()

    except Exception as e:
        logger.error('customer_chat process error: %s', e, exc_info=True)
        CircuitBreaker.record_failure('openrouter_chat')

        # Fallback to local mode on any error
        fallback = _generate_fallback_response(user_message, session_id)
        SessionMemory.append(session_id, 'user', user_message)
        SessionMemory.append(session_id, 'assistant', fallback.reply)
        return fallback.to_dict()


def process_customer_stream(
    user_message: str,
    session_id: Optional[str] = None,
) -> Generator[str, None, None]:
    """
    Streaming generator for real-time customer responses.
    Yields text chunks as they arrive from the AI.
    Falls back to single response if streaming not available.
    """
    if not session_id:
        session_id = 'cw_' + str(uuid.uuid4()).replace('-', '')[:16]

    if not _rate_limiter.is_allowed(session_id):
        yield '⚠️ Terlalu banyak pesan. Mohon tunggu sebentar lalu coba lagi.'
        return

    if CircuitBreaker.is_open('openrouter_chat'):
        result = _generate_fallback_response(user_message, session_id)
        yield result.reply
        SessionMemory.append(session_id, 'user', user_message)
        SessionMemory.append(session_id, 'assistant', result.reply)
        return

    _init_tools()

    try:
        from api.ai_service import get_service

        svc = get_service()
        messages = _build_messages(user_message, session_id)

        full_reply = []
        for chunk in svc.stream_completion(
            messages,
            temperature=0.6,
            max_tokens=700,
        ):
            full_reply.append(chunk)
            yield chunk

        # Store complete reply after stream finishes
        complete = ''.join(full_reply)
        SessionMemory.append(session_id, 'user', user_message)
        SessionMemory.append(session_id, 'assistant', complete)
        CircuitBreaker.record_success('openrouter_chat')

    except Exception as e:
        logger.error('customer_chat stream error: %s', e)
        CircuitBreaker.record_failure('openrouter_chat')
        # Fallback to local mode
        result = _generate_fallback_response(user_message, session_id)
        yield result.reply
        SessionMemory.append(session_id, 'user', user_message)
        SessionMemory.append(session_id, 'assistant', result.reply)


# ---------------------------------------------------------------------------
# Async Entry Points
# ---------------------------------------------------------------------------

async def aprocess_customer_message(
    user_message: str,
    session_id: Optional[str] = None,
    image: Optional[str] = None,
    *,
    use_tools: bool = True,
) -> Dict[str, Any]:
    """Async version of process_customer_message."""
    if not session_id:
        session_id = 'cw_' + str(uuid.uuid4()).replace('-', '')[:16]

    if not _rate_limiter.is_allowed(session_id):
        return CustomerChatResult(
            success=False,
            reply='⚠️ Terlalu banyak pesan. Mohon tunggu sebentar lalu coba lagi.',
            session_id=session_id,
            source="rate_limited",
        ).to_dict()

    if CircuitBreaker.is_open('openrouter_chat'):
        result = _generate_fallback_response(user_message, session_id)
        SessionMemory.append(session_id, 'user', user_message)
        SessionMemory.append(session_id, 'assistant', result.reply)
        return result.to_dict()

    _init_tools()

    try:
        from api.ai_service import get_service

        svc = get_service()
        messages = _build_messages(user_message, session_id, image)

        _ctx_token = _session_ctx.set(session_id)
        try:
            if use_tools:
                result = await svc.achat_completion(
                    messages,
                    temperature=0.6,
                    max_tokens=700,
                    use_cache=False,
                    rate_key=session_id,
                    tools=TOOLS_SCHEMA,
                    execute_tools=True,
                )
            else:
                result = await svc.achat_completion(
                    messages,
                    temperature=0.6,
                    max_tokens=700,
                    use_cache=False,
                    rate_key=session_id,
                )
        finally:
            _session_ctx.reset(_ctx_token)

        if result.success:
            CircuitBreaker.record_success('openrouter_chat')
            reply = _strip_html(result.reply)

            if not result.tool_calls and '[' in reply:
                action_name, params = _extract_legacy_action(reply)
                if action_name:
                    clean_reply = re.sub(r'\s*\[AKSI:[^\]]+\]', '', reply).strip()
                    action_result = _execute_legacy_action(action_name, params)
                    reply = (
                        '{}\n\n{}'.format(clean_reply, action_result)
                        if clean_reply
                        else action_result
                    )

            _harvest_identity_from_tool_calls(session_id, result.tool_calls)

            cost_usd = result.cost.cost_usd if result.cost else 0.0

            SessionMemory.append(session_id, 'user', user_message, source='user')
            SessionMemory.append(
                session_id, 'assistant', reply,
                model=result.model, cost_usd=cost_usd,
                tool_calls=result.tool_calls, source='ai',
            )

            return CustomerChatResult(
                success=True,
                reply=reply.strip(),
                session_id=session_id,
                model=result.model,
                cost_usd=cost_usd,
                tool_calls=result.tool_calls or [],
                cached=result.cached,
                source="ai",
            ).to_dict()
        else:
            CircuitBreaker.record_failure('openrouter_chat')
            fallback = _generate_fallback_response(user_message, session_id)
            SessionMemory.append(session_id, 'user', user_message)
            SessionMemory.append(session_id, 'assistant', fallback.reply)
            return fallback.to_dict()

    except ImportError as e:
        logger.warning(f"ai_service not available: {e}")
        fallback = _generate_fallback_response(user_message, session_id)
        SessionMemory.append(session_id, 'user', user_message)
        SessionMemory.append(session_id, 'assistant', fallback.reply)
        return fallback.to_dict()

    except Exception as e:
        logger.error('customer_chat async process error: %s', e, exc_info=True)
        CircuitBreaker.record_failure('openrouter_chat')
        fallback = _generate_fallback_response(user_message, session_id)
        SessionMemory.append(session_id, 'user', user_message)
        SessionMemory.append(session_id, 'assistant', fallback.reply)
        return fallback.to_dict()


async def aprocess_customer_stream(
    user_message: str,
    session_id: Optional[str] = None,
) -> AsyncGenerator[str, None]:
    """Async streaming generator for real-time responses."""
    if not session_id:
        session_id = 'cw_' + str(uuid.uuid4()).replace('-', '')[:16]

    if not _rate_limiter.is_allowed(session_id):
        yield '⚠️ Terlalu banyak pesan. Mohon tunggu sebentar lalu coba lagi.'
        return

    if CircuitBreaker.is_open('openrouter_chat'):
        result = _generate_fallback_response(user_message, session_id)
        yield result.reply
        SessionMemory.append(session_id, 'user', user_message)
        SessionMemory.append(session_id, 'assistant', result.reply)
        return

    _init_tools()

    try:
        from api.ai_service import get_service

        svc = get_service()
        messages = _build_messages(user_message, session_id)

        full_reply = []
        async for chunk in svc.astream_completion(
            messages,
            temperature=0.6,
            max_tokens=700,
        ):
            full_reply.append(chunk)
            yield chunk

        complete = ''.join(full_reply)
        SessionMemory.append(session_id, 'user', user_message)
        SessionMemory.append(session_id, 'assistant', complete)
        CircuitBreaker.record_success('openrouter_chat')

    except Exception as e:
        logger.error('customer_chat async stream error: %s', e)
        CircuitBreaker.record_failure('openrouter_chat')
        result = _generate_fallback_response(user_message, session_id)
        yield result.reply
        SessionMemory.append(session_id, 'user', user_message)
        SessionMemory.append(session_id, 'assistant', result.reply)


# ---------------------------------------------------------------------------
# Health Check
# ---------------------------------------------------------------------------

def get_health_status() -> Dict[str, Any]:
    """Return health status of customer chat service."""
    cb_state = CircuitBreaker._states.get('openrouter_chat', {'state': 'closed'})

    return {
        'status': 'ok',
        'version': '2026-pro',
        'pythonanywhere': IS_PYTHONANYWHERE,
        'circuit_breaker': cb_state,
        'session_count': SessionMemory.cache_size(),
        'tools_registered': len(_LEGACY_ACTION_MAP),
    }


# ---------------------------------------------------------------------------
# Backward Compatibility Aliases
# ---------------------------------------------------------------------------

process_message = process_customer_message
