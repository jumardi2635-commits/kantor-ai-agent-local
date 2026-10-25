#!/usr/bin/env python3
"""
Telegram Shop Bot Webhook — Flask (PythonAnywhere Compatible)

Fitur:
- Menu FAQ otomatis
- Live Chat (relay pesan pelanggan ke admin)
- Admin dapat membalas dengan Reply atau perintah /reply dan /endchat
- AI fallback dengan OpenRouter (gratis) + konteks produk asli dari database
- Cek status order, resi, stok produk langsung dari database
- Mendukung chat pribadi dan grup

Cara pakai di app.py:
    from api.telegram_shop_bot import register_telegram_shop_bot
    register_telegram_shop_bot(app)
"""
import hashlib
import hmac
import html
import json
import logging
import os
import re
import time
from collections import defaultdict
from typing import Optional

try:
    import requests
except ImportError:
    requests = None

logger = logging.getLogger(__name__)


# =============================================================================
# SETTING HELPER
# =============================================================================

def _get_setting(key: str, default: str = '') -> str:
    try:
        from models import Setting
        s = Setting.query.filter_by(key=key).first()
        if s and s.value:
            return s.value
    except Exception:
        pass
    return os.getenv(key.upper(), default)


def _bot_token() -> str:
    return _get_setting('telegram_bot_token', '').strip()


def _admin_chat_id() -> str:
    return _get_setting('telegram_chat_id', '').strip()


# =============================================================================
# AI FALLBACK (OpenRouter)
# =============================================================================

OPENROUTER_API_URL = 'https://openrouter.ai/api/v1/chat/completions'
_DEFAULT_FREE_MODEL = 'meta-llama/llama-3.3-70b-instruct:free'


def _openrouter_api_key() -> str:
    return _get_setting('openrouter_api_key', '').strip()


def _openrouter_model() -> str:
    return _get_setting('openrouter_model', _DEFAULT_FREE_MODEL).strip()


# =============================================================================
# PRODUCT CATALOG CONTEXT (dari database)
# =============================================================================

_CATALOG_CACHE_TTL = 5 * 60
_catalog_cache = {'text': '', 'terms': [], 'ts': 0.0}


def _format_rupiah(price_in_cents) -> str:
    try:
        rupiah = int(round(float(price_in_cents or 0))) // 100
    except (TypeError, ValueError):
        rupiah = 0
    return f"Rp {rupiah:,}".replace(',', '.')


def _refresh_catalog_cache(limit: int = 40) -> None:
    try:
        from models import Product
    except Exception:
        _catalog_cache.update(text='', terms=[], ts=time.time())
        return
    try:
        products = (
            Product.query.filter_by(is_active=True)
            .order_by(Product.created_at.desc())
            .limit(limit)
            .all()
        )
    except Exception as e:
        logger.warning(f"[TelegramShopBot] Gagal ambil data produk: {e}")
        _catalog_cache.update(text='', terms=[], ts=time.time())
        return

    lines = []
    terms = set()
    for p in products:
        name = getattr(p, 'name', '') or ''
        brand = getattr(p, 'brand', '') or ''
        model_name = getattr(p, 'model', '') or ''
        condition = getattr(p, 'condition', '') or ''
        stock = p.stock if getattr(p, 'stock', None) is not None else '-'
        price_rp = _format_rupiah(getattr(p, 'price', None))
        compare_at = getattr(p, 'compare_at_price', None)
        price = getattr(p, 'price', None)
        discount_note = f" (harga coret {_format_rupiah(compare_at)})" if (compare_at and price and compare_at > price) else ''
        lines.append(
            f"- {name} | Merek: {brand or '-'} | Kondisi: {condition or '-'} | "
            f"Harga: {price_rp}{discount_note} | Stok: {stock}"
        )
        for term in (name, brand, model_name):
            term = (term or '').strip().lower()
            if len(term) >= 3:
                terms.add(term)

    _catalog_cache.update(text="\n".join(lines), terms=list(terms), ts=time.time())


def _get_product_catalog_context(limit: int = 40) -> str:
    if time.time() - _catalog_cache['ts'] > _CATALOG_CACHE_TTL:
        _refresh_catalog_cache(limit)
    return _catalog_cache['text']


def _text_mentions_known_product(text_lower: str) -> bool:
    if time.time() - _catalog_cache['ts'] > _CATALOG_CACHE_TTL:
        _refresh_catalog_cache()
    return any(term in text_lower for term in _catalog_cache['terms'])


def _get_ai_response(user_message: str, sender_name: str = 'Kak') -> Optional[str]:
    api_key = _openrouter_api_key()
    if not api_key or requests is None:
        return None

    model = _openrouter_model()
    catalog_context = _get_product_catalog_context()
    if catalog_context:
        catalog_block = (
            "\n\nBerikut daftar produk AKTIF saat ini di website (data ASLI):\n"
            + catalog_context +
            "\n\nPENTING: pakai HANYA data di atas. JANGAN mengarang produk, harga, atau stok."
        )
    else:
        catalog_block = "\n\n(Data katalog produk sedang tidak tersedia. JANGAN menebak harga/stok.)"

    system_prompt = (
        "Kamu adalah admin toko 'Pusat Gadai Indonesia', toko gadget second terpercaya. "
        "Semua unit bergaransi 1 tahun. Jam operasional 10:00-20:00 WIB. "
        "WhatsApp admin: +62 895-3005-2136. Katalog: https://pusatgadaiidn.pythonanywhere.com. "
        "Jawab pertanyaan pelanggan secara singkat, ramah, gunakan sapaan 'Kak'. "
        "JANGAN gunakan HTML/markdown." + catalog_block
    )

    try:
        response = requests.post(
            OPENROUTER_API_URL,
            json={
                'model': model,
                'messages': [
                    {'role': 'system', 'content': system_prompt},
                    {'role': 'user', 'content': user_message},
                ],
                'temperature': 0.7,
                'max_tokens': 300,
            },
            headers={
                'Authorization': f'Bearer {api_key}',
                'Content-Type': 'application/json',
                'HTTP-Referer': 'https://pusatgadaiidn.pythonanywhere.com',
                'X-Title': 'PusatGadaiTelegramShopBot',
            },
            timeout=20,
        )
        response.raise_for_status()
        data = response.json()
        ai_text = (data.get('choices') or [{}])[0].get('message', {}).get('content', '')
        return ai_text.strip() or None
    except Exception as e:
        logger.warning(f"[TelegramShopBot] AI fallback gagal: {e}")
        return None


# =============================================================================
# RATE LIMITING
# =============================================================================

class _RateLimiter:
    _storage = defaultdict(list)

    @classmethod
    def is_allowed(cls, key: str, limit: int = 60, window: int = 60) -> bool:
        now = time.time()
        cls._storage[key] = [t for t in cls._storage[key] if now - t < window]
        if len(cls._storage[key]) >= limit:
            return False
        cls._storage[key].append(now)
        return True


def _escape_html(text: str) -> str:
    return html.escape(text or '', quote=False)


# =============================================================================
# LIVE CHAT MANAGER
# =============================================================================

LIVE_CHAT_IDLE_TIMEOUT = 30 * 60
_CHAT_ID_MARKER_RE = re.compile(r'ChatID:(\d+)')

_START_CHAT_KEYWORDS = (
    'chat dengan admin', 'chat admin', 'live chat', 'livechat',
    'ngobrol dengan admin', 'ngobrol admin', 'chat manusia', 'cs manusia',
    'bicara dengan admin', 'talk to admin', 'human agent',
)
_END_CHAT_KEYWORDS = (
    'selesai', 'akhiri chat', 'tutup chat', 'end chat', 'close chat',
    '/selesai', '/end',
)


class LiveChatManager:
    _sessions = {}

    @classmethod
    def _purge_expired(cls):
        now = time.time()
        expired = [cid for cid, s in cls._sessions.items()
                   if now - s['last_active'] > LIVE_CHAT_IDLE_TIMEOUT]
        for cid in expired:
            cls._sessions.pop(cid, None)

    @classmethod
    def is_active(cls, chat_id) -> bool:
        cls._purge_expired()
        return chat_id in cls._sessions

    @classmethod
    def start(cls, chat_id, sender_name: str = '', username: str = ''):
        cls._purge_expired()
        now = time.time()
        cls._sessions[chat_id] = {
            'name': sender_name or 'Kak',
            'username': username or '',
            'started': now,
            'last_active': now,
        }

    @classmethod
    def touch(cls, chat_id):
        if chat_id in cls._sessions:
            cls._sessions[chat_id]['last_active'] = time.time()

    @classmethod
    def end(cls, chat_id):
        cls._sessions.pop(chat_id, None)

    @classmethod
    def get(cls, chat_id):
        cls._purge_expired()
        return cls._sessions.get(chat_id)


def _quick_menu_keyboard() -> dict:
    return {
        'inline_keyboard': [
            [{'text': '📱 Produk', 'callback_data': 'products'},
             {'text': '🎁 Promo', 'callback_data': 'promo'}],
            [{'text': '🛡️ Garansi', 'callback_data': 'garansi'},
             {'text': '🚚 Pengiriman', 'callback_data': 'pengiriman'}],
            [{'text': '👤 Kontak', 'callback_data': 'contact'}],
            [{'text': '💬 Chat Langsung dengan Admin', 'callback_data': 'live_chat_start'}],
        ]
    }


def _shipping_info_text() -> str:
    return (
        "🚚 <b>INFORMASI PENGIRIMAN</b>\n\n"
        "📍 Area: Seluruh Indonesia\n"
        "📦 Ekspedisi: JNE, J&T, SiCepat, AnterAja\n"
        "💰 Ongkir ditanggung pembeli, gratis untuk area tertentu\n"
        "⏱️ Estimasi: 1-5 hari kerja\n"
        "💬 Chat admin untuk cek ongkir!"
    )


def _end_chat_keyboard() -> dict:
    return {'inline_keyboard': [[{'text': '❌ Akhiri Live Chat', 'callback_data': 'live_chat_end'}]]}


def _live_chat_welcome_text(safe_name: str) -> str:
    return (
        f"💬 <b>Live Chat dimulai, {safe_name}!</b>\n\n"
        "Kamu sekarang terhubung langsung dengan admin. Tulis pertanyaanmu, akan diteruskan ke admin.\n"
        "⏱️ Admin membalas 1-15 menit (10:00-20:00 WIB).\n"
        "Ketik <b>selesai</b> atau tekan tombol untuk mengakhiri."
    )


def _notify_admin_chat_started(chat_id, safe_name: str, safe_username: str) -> None:
    admin_chat_id = _admin_chat_id()
    if not admin_chat_id:
        return
    username_line = f"@{safe_username}" if safe_username else '-'
    send_telegram_message(
        admin_chat_id,
        f"🟢 <b>Live Chat baru</b>\n👤 {safe_name} ({username_line})\n🆔 <code>ChatID:{chat_id}</code>\n\n"
        "<i>Balas dengan Reply atau /reply <chat_id> pesan</i>"
    )


def _notify_admin_chat_ended(chat_id, safe_name: str, closer: str = 'pelanggan') -> None:
    admin_chat_id = _admin_chat_id()
    if not admin_chat_id:
        return
    send_telegram_message(
        admin_chat_id,
        f"🔴 <b>Live Chat diakhiri oleh {closer}</b>\n👤 {safe_name}\n🆔 <code>ChatID:{chat_id}</code>"
    )


def _forward_customer_message_to_admin(chat_id, sender_name: str, username: str, text: str) -> bool:
    admin_chat_id = _admin_chat_id()
    if not admin_chat_id:
        logger.warning("[TelegramShopBot] telegram_chat_id kosong, tidak bisa forward")
        return False

    safe_name = _escape_html(sender_name) or 'Kak'
    safe_username = _escape_html(username)
    safe_text = _escape_html(text)
    username_line = f"@{safe_username}" if safe_username else '-'

    message = (
        f"💬 <b>Live Chat — Pesan Pelanggan</b>\n"
        f"👤 {safe_name} ({username_line})\n"
        f"🆔 <code>ChatID:{chat_id}</code>\n\n"
        f"{safe_text}\n\n"
        f"<i>↩️ Reply atau /reply {chat_id} ...</i>"
    )
    return send_telegram_message(admin_chat_id, message)


def _handle_admin_reply(message: dict) -> None:
    reply_to = message.get('reply_to_message') or {}
    original_text = reply_to.get('text', '') or reply_to.get('caption', '') or ''
    match = _CHAT_ID_MARKER_RE.search(original_text)
    if not match:
        return

    target_chat_id = int(match.group(1))
    reply_text = (message.get('text') or '').strip()
    if not reply_text:
        return

    if reply_text.lower() in ('/selesai', '/end', '/close'):
        session = LiveChatManager.get(target_chat_id)
        safe_name = _escape_html(session['name']) if session else 'Kak'
        LiveChatManager.end(target_chat_id)
        send_telegram_message(
            target_chat_id,
            "✅ Live chat diakhiri oleh admin. Ketik <b>menu</b> untuk bantuan lagi."
        )
        _notify_admin_chat_ended(target_chat_id, safe_name, closer='admin')
        return

    LiveChatManager.touch(target_chat_id)
    send_telegram_message(target_chat_id, f"👤 <b>Admin:</b>\n{_escape_html(reply_text)}")


# =============================================================================
# ORDER / RESI / PRODUK LOOKUP
# =============================================================================

_ORDER_STATUS_LABELS = {
    'Pending': '⏳ Menunggu Pembayaran',
    'Paid': '✅ Pembayaran Diterima',
    'Processing': '🔄 Diproses',
    'Ready_to_Ship': '📦 Siap Dikirim',
    'Shipped': '🚚 Dikirim',
    'Out_for_Delivery': '📍 Dalam Perjalanan',
    'Received': '📥 Diterima',
    'Completed': '✅ Selesai',
    'Cancelled': '❌ Dibatalkan',
    'Returned': '↩️ Dikembalikan',
}

_CODE_STRIP_WORDS_RE = re.compile(
    r'\b(cek|check|status|order|pesanan|resi|lacak|tracking|nomor|no\.?|dong|gan|kak|ya|min|dimana|udah|sudah|sampai|belum)\b',
    re.IGNORECASE,
)


def _extract_code_token(text: str) -> Optional[str]:
    cleaned = _CODE_STRIP_WORDS_RE.sub(' ', text or '')
    for candidate in re.findall(r'#?([A-Za-z0-9\-]{5,})', cleaned):
        if any(ch.isdigit() for ch in candidate):
            return candidate
    return None


def _extract_product_keyword(text: str) -> str:
    cleaned = re.sub(
        r'\b(cek|check|info|produk|product|stok|stock|ready|masih|ada|kah|'
        r'apakah|dong|gan|kak|ga|gak|nggak|nih|yuk|dan|harga|nya)\b',
        ' ', (text or '').lower(), flags=re.IGNORECASE,
    )
    return re.sub(r'\s+', ' ', cleaned).strip()


def _find_order(code: str):
    try:
        from models import Order
        from sqlalchemy import or_
    except Exception:
        return None
    try:
        return Order.query.filter(
            or_(Order.order_number.ilike(f'%{code}%'),
                Order.customer_phone.ilike(f'%{code}%'))
        ).order_by(Order.created_at.desc()).first()
    except Exception as e:
        logger.warning(f"[TelegramShopBot] Gagal query order: {e}")
        return None


def _format_order_reply(order) -> str:
    status_label = _ORDER_STATUS_LABELS.get(order.status, order.status)
    items_lines = []
    try:
        for it in (order.items or [])[:10]:
            items_lines.append(f"• {_escape_html(it.name)} x{it.quantity}")
    except Exception:
        pass
    items_block = "\n".join(items_lines) if items_lines else '-'
    return (
        f"📦 <b>STATUS ORDER</b>\n\n"
        f"🧾 No. Order: <code>{_escape_html(order.order_number)}</code>\n"
        f"📌 Status: {status_label}\n"
        f"🛍️ Produk:\n{items_block}\n"
        f"💰 Total: {_format_rupiah(order.total)}\n\n"
        "💬 Ada pertanyaan lain? Chat admin ya!"
    )


def _find_order_by_tracking(code: str):
    try:
        from models import Order
    except Exception:
        return None
    try:
        return Order.query.filter(Order.tracking_number.ilike(f'%{code}%')).order_by(Order.created_at.desc()).first()
    except Exception as e:
        logger.warning(f"[TelegramShopBot] Gagal query order (fallback): {e}")
        return None


def _format_order_tracking_fallback_reply(order) -> str:
    eta = order.estimated_delivery.strftime('%d %B %Y') if getattr(order, 'estimated_delivery', None) else '-'
    status_label = _ORDER_STATUS_LABELS.get(order.status, order.status)
    return (
        f"🚚 <b>LACAK PENGIRIMAN</b>\n\n"
        f"🧾 No. Resi: <code>{_escape_html(order.tracking_number)}</code>\n"
        f"📦 Ekspedisi: {_escape_html(order.carrier_name or '-')}\n"
        f"📍 Status Order: {status_label}\n"
        f"📅 Estimasi Tiba: {eta}\n\n"
        "💬 Ada pertanyaan lain? Chat admin ya!"
    )


def _find_shipment(code: str):
    try:
        from models import ShippingTracking
        from sqlalchemy import or_
    except Exception:
        return None
    try:
        return ShippingTracking.query.filter(
            or_(ShippingTracking.tracking_number.ilike(f'%{code}%'),
                ShippingTracking.recipient_phone.ilike(f'%{code}%'))
        ).order_by(ShippingTracking.created_at.desc()).first()
    except Exception as e:
        logger.warning(f"[TelegramShopBot] Gagal query resi: {e}")
        return None


def _format_shipment_reply(shipment) -> str:
    eta = shipment.estimated_delivery.strftime('%d %B %Y') if getattr(shipment, 'estimated_delivery', None) else '-'
    issue_note = f"\n⚠️ <b>Kendala:</b> {_escape_html(shipment.issue_type or 'Ada kendala')}" if getattr(shipment, 'has_issue', False) else ''
    latest_event_note = ''
    try:
        latest = (shipment.events or [])[0] if shipment.events else None
        if latest:
            when = latest.event_time.strftime('%d %b %Y %H:%M') if latest.event_time else '-'
            desc = latest.status_description or latest.status
            latest_event_note = f"\n\n📝 <b>Update Terakhir</b> ({when}):\n{_escape_html(desc)}"
    except Exception:
        pass
    return (
        f"🚚 <b>LACAK PENGIRIMAN</b>\n\n"
        f"🧾 No. Resi: <code>{_escape_html(shipment.tracking_number)}</code>\n"
        f"📦 Ekspedisi: {_escape_html(shipment.carrier or '-')}\n"
        f"📍 Status: {_escape_html(shipment.status or '-')}\n"
        f"🗺️ Lokasi Terakhir: {_escape_html(shipment.current_location or '-')}\n"
        f"📅 Estimasi Tiba: {eta}"
        f"{issue_note}{latest_event_note}\n\n"
        "💬 Ada pertanyaan lain? Chat admin ya!"
    )


def _search_products(keyword: str, limit: int = 5):
    try:
        from models import Product
        from sqlalchemy import or_
    except Exception:
        return []
    try:
        return Product.query.filter(
            Product.is_active == True,
            or_(Product.name.ilike(f'%{keyword}%'),
                Product.brand.ilike(f'%{keyword}%'),
                Product.model.ilike(f'%{keyword}%'))
        ).order_by(Product.created_at.desc()).limit(limit).all()
    except Exception as e:
        logger.warning(f"[TelegramShopBot] Gagal cari produk: {e}")
        return []


def _format_product_search_reply(keyword: str, products) -> str:
    safe_keyword = _escape_html(keyword)
    if not products:
        return (
            f"😔 Produk <b>\"{safe_keyword}\"</b> tidak ditemukan.\n\n"
            "🌐 Cek katalog: https://pusatgadaiidn.pythonanywhere.com\n"
            "💬 Tanya admin langsung!"
        )
    lines = [f"🔎 <b>HASIL PENCARIAN \"{safe_keyword}\"</b>\n"]
    for p in products:
        stock = p.stock if getattr(p, 'stock', None) is not None else 0
        ready = '✅ Ready' if stock > 0 else '❌ Habis'
        price_rp = _format_rupiah(getattr(p, 'price', None))
        compare_at = getattr(p, 'compare_at_price', None)
        price = getattr(p, 'price', None)
        discount_note = f" (coret {_format_rupiah(compare_at)})" if (compare_at and price and compare_at > price) else ''
        lines.append(
            f"📱 <b>{_escape_html(p.name)}</b>\n"
            f"   {_escape_html(p.brand or '-')} • {_escape_html(p.condition or '-')}\n"
            f"   💰 {price_rp}{discount_note}\n"
            f"   📊 {ready} (stok: {stock})\n"
        )
    lines.append("🌐 Detail: https://pusatgadaiidn.pythonanywhere.com")
    return "\n".join(lines)


# =============================================================================
# MESSAGE PROCESSOR
# =============================================================================

def process_telegram_message(text: str, sender_name: str, username: str, chat_id) -> str:
    text_lower = (text or '').lower().strip()
    safe_name = _escape_html(sender_name) or 'Kak'

    # CEK RESI
    if any(kw in text_lower for kw in ('resi', 'lacak', 'tracking')):
        code = _extract_code_token(text)
        if code:
            shipment = _find_shipment(code)
            if shipment:
                return _format_shipment_reply(shipment)
            order_fallback = _find_order_by_tracking(code)
            if order_fallback:
                return _format_order_tracking_fallback_reply(order_fallback)
            return f"😔 Nomor resi <b>{_escape_html(code)}</b> tidak ditemukan.\n\nPastikan nomor resi benar."

    # CEK ORDER
    if 'resi' not in text_lower and ('order' in text_lower or 'pesanan' in text_lower):
        code = _extract_code_token(text)
        if code:
            order = _find_order(code)
            if order:
                return _format_order_reply(order)
            return f"😔 Order <b>{_escape_html(code)}</b> tidak ditemukan.\n\nPastikan nomor order atau HP benar."

    # CEK PRODUK / STOK
    if any(kw in text_lower for kw in ('cek produk', 'cek stok', 'stok ', 'stock ')) \
            or text_lower.startswith('ready') or text_lower.endswith('ready') \
            or ' ready ' in f' {text_lower} ':
        keyword = _extract_product_keyword(text)
        if len(keyword) >= 2:
            products = _search_products(keyword)
            return _format_product_search_reply(keyword, products)

    # PERTANYAAN SPESIFIK PRODUK (AI)
    if _text_mentions_known_product(text_lower):
        ai_reply = _get_ai_response(text, safe_name)
        if ai_reply:
            return _escape_html(ai_reply)

    # SAPAAN
    if text_lower in ('halo', 'hai', 'hi', 'hey', 'assalamualaikum', 'hello', 'pagi', 'siang', 'sore', 'malam'):
        return (
            f"👋 <b>Halo {safe_name}!</b> Selamat datang di Pusat Gadai Indonesia!\n\n"
            "Apa yang bisa kami bantu?\n"
            "📋 Menu:\n"
            "• Info produk\n• Cara pembelian\n• Cek order (ketik: cek order [nomor])\n"
            "• Cek resi (ketik: cek resi [nomor])\n• Cek stok (ketik: cek produk [nama])\n"
            "• Hubungi admin\n• Garansi\n• Pengiriman\n"
            "• 💬 Chat langsung dengan admin (ketik 'chat dengan admin')"
        )

    # INFO PRODUK
    if 'info' in text_lower and 'produk' in text_lower:
        return (
            "📱 <b>PUSAT GADAI INDONESIA</b>\n\n"
            "Gadget second berkualitas, garansi 1 tahun, fullset & original.\n"
            "🌐 Katalog: https://pusatgadaiidn.pythonanywhere.com\n"
            "💬 Chat admin untuk info lebih lanjut!"
        )

    # CARA PEMBELIAN
    if 'cara' in text_lower or 'beli' in text_lower or 'pembelian' in text_lower:
        return (
            "🛒 <b>CARA PEMBELIAN</b>\n\n"
            "1️⃣ Pilih produk di katalog\n2️⃣ Chat admin konfirmasi stok\n"
            "3️⃣ Transfer pembayaran\n4️⃣ Kami kirim\n5️⃣ Barang sampai!\n\n"
            "📱 Admin: +62 895-3005-2136\n⏰ 10:00 - 20:00 WIB"
        )

    # STATUS ORDER
    if 'status' in text_lower or 'order' in text_lower or 'resi' in text_lower:
        return (
            "📦 <b>CEK STATUS ORDER</b>\n\n"
            "Kirim nomor order Anda.\n"
            "Contoh: <code>Order #PGD2026001</code>\n"
            "Atau chat admin: +62 895-3005-2136"
        )

    # HUBUNGI ADMIN
    if 'admin' in text_lower or 'hubungi' in text_lower or 'contact' in text_lower or text_lower == 'cs':
        return (
            "👤 <b>HUBUNGI ADMIN</b>\n\n"
            "📱 WhatsApp: +62 895-3005-2136\n"
            "📧 Email: admin@pusatgadai.id\n"
            "🌐 Website: pusatgadaiidn.pythonanywhere.com\n"
            "⏰ Senin-Minggu 10:00-20:00 WIB"
        )

    # GARANSI
    if 'garansi' in text_lower or 'warranty' in text_lower:
        return (
            "🛡️ <b>GARANSI</b>\n\n"
            "✅ Garansi 1 tahun untuk semua produk.\n"
            "Cakupan: kerusakan fungsi, tidak sesuai deskripsi, IMEI terblokir.\n"
            "❌ Tidak mencakup: fisik, air, software, kehilangan.\n"
            "📞 Klaim hubungi admin."
        )

    # PENGIRIMAN
    if 'kirim' in text_lower or 'pengiriman' in text_lower or 'ongkir' in text_lower or 'ongkos' in text_lower:
        return _shipping_info_text()

    # HARGA
    if 'harga' in text_lower or 'mahal' in text_lower or 'murah' in text_lower or 'diskon' in text_lower:
        return (
            "💰 <b>HARGA</b>\n\n"
            "Harga kompetitif, semua unit second berkualitas.\n"
            "🎁 Promo: diskon pembelian kedua, cashback member, cicilan 0%.\n"
            "🌐 Cek katalog: https://pusatgadaiidn.pythonanywhere.com"
        )

    # LOKASI
    if 'lokasi' in text_lower or 'alamat' in text_lower or 'toko' in text_lower or 'store' in text_lower:
        return (
            "📍 <b>LOKASI</b>\n\n"
            "🏪 Online (tidak ada toko fisik)\n"
            "📦 Gudang: Jakarta\n"
            "🌐 https://pusatgadaiidn.pythonanywhere.com"
        )

    # TERIMA KASIH
    if 'terima' in text_lower or 'makasih' in text_lower or 'thanks' in text_lower or 'thank' in text_lower:
        return f"🙏 <b>Sama-sama, {safe_name}!</b>\n\nAda yang bisa kami bantu lagi? 😊"

    # BYE
    if 'bye' in text_lower or 'dadah' in text_lower or 'sampai' in text_lower:
        return (
            f"👋 <b>Sampai jumpa, {safe_name}!</b>\n\n"
            "Terima kasih sudah menghubungi kami.\n"
            "Jangan ragu untuk chat lagi ya! 😊"
        )

    # FALLBACK AI
    ai_reply = _get_ai_response(text, safe_name)
    if ai_reply:
        return _escape_html(ai_reply)

    # DEFAULT
    return (
        f"👍 Terima kasih, <b>{safe_name}</b>!\n\n"
        "Tim kami akan segera merespons.\n"
        "⏱️ Respon 1-15 menit (jam operasional).\n"
        "📱 WhatsApp: +62 895-3005-2136\n"
        "🌐 https://pusatgadaiidn.pythonanywhere.com"
    )


# =============================================================================
# CALLBACK QUERY
# =============================================================================

def process_callback_query(data: str, chat_id, sender_name: str = '', username: str = '') -> str:
    safe_name = _escape_html(sender_name) or 'Kak'

    if data == 'live_chat_start':
        LiveChatManager.start(chat_id, sender_name, username)
        _notify_admin_chat_started(chat_id, safe_name, _escape_html(username))
        return _live_chat_welcome_text(safe_name)

    if data == 'live_chat_end':
        was_active = LiveChatManager.is_active(chat_id)
        LiveChatManager.end(chat_id)
        if was_active:
            _notify_admin_chat_ended(chat_id, safe_name, closer='pelanggan')
        return "✅ Live chat diakhiri. Ketik <b>menu</b> untuk bantuan lagi."

    if data == 'products':
        return "📱 <b>KATALOG</b>\n\nhttps://pusatgadaiidn.pythonanywhere.com/catalog"
    if data == 'contact':
        return "👤 <b>KONTAK</b>\n\n📱 WhatsApp: +62 895-3005-2136\n🌐 pusatgadaiidn.pythonanywhere.com"
    if data == 'garansi':
        return "🛡️ <b>GARANSI</b>\n\n✅ 1 tahun\n✅ Fullset & original\n✅ Verifikasi ketat"
    if data == 'pengiriman':
        return _shipping_info_text()
    if data == 'promo':
        return "🎁 <b>PROMO</b>\n\n• Diskon 5% pembelian kedua\n• Cashback member 10%\n• Cicilan 0%\n\n💬 Chat admin untuk detail!"

    return "Terima kasih! 🙏 Ada yang bisa dibantu?"


# =============================================================================
# TELEGRAM API HELPERS
# =============================================================================

def send_telegram_message(chat_id, text: str, parse_mode: str = 'HTML', reply_markup: dict = None) -> bool:
    token = _bot_token()
    if not token or requests is None:
        return False

    url = f"https://api.telegram.org/bot{token}/sendMessage"
    data = {
        'chat_id': chat_id,
        'text': text,
        'parse_mode': parse_mode,
        'disable_web_page_preview': True,
    }
    if reply_markup:
        data['reply_markup'] = reply_markup

    try:
        response = requests.post(url, json=data, timeout=10)
        response.raise_for_status()
        return True
    except Exception as e:
        logger.error(f"[TelegramShopBot] Gagal kirim pesan ke {chat_id}: {e}")
        return False


def send_telegram_photo(chat_id, photo_url: str, caption: str = None, parse_mode: str = 'HTML') -> bool:
    token = _bot_token()
    if not token or requests is None:
        return False

    url = f"https://api.telegram.org/bot{token}/sendPhoto"
    data = {'chat_id': chat_id, 'photo': photo_url}
    if caption:
        data['caption'] = caption
        data['parse_mode'] = parse_mode

    try:
        response = requests.post(url, json=data, timeout=15)
        response.raise_for_status()
        return True
    except Exception as e:
        logger.error(f"[TelegramShopBot] Gagal kirim foto ke {chat_id}: {e}")
        return False


def _answer_callback_query(callback_query_id: str, text: str = None) -> bool:
    token = _bot_token()
    if not token or requests is None:
        return False

    url = f"https://api.telegram.org/bot{token}/answerCallbackQuery"
    data = {'callback_query_id': callback_query_id}
    if text:
        data['text'] = text[:200]

    try:
        response = requests.post(url, json=data, timeout=10)
        return response.json().get('ok', False)
    except Exception as e:
        logger.error(f"[TelegramShopBot] Gagal jawab callback: {e}")
        return False


# =============================================================================
# FLASK REGISTRATION
# =============================================================================

def register_telegram_shop_bot(app):
    from flask import request, jsonify, Response

    if 'telegram_shop_bot_webhook' in app.view_functions:
        logger.warning("register_telegram_shop_bot() dilewati: endpoint sudah terdaftar.")
        return

    @app.route('/webhook/telegram', methods=['POST'], strict_slashes=False)
    def telegram_shop_bot_webhook():
        # Rate limit
        client_ip = request.headers.get('X-Forwarded-For', request.remote_addr or 'unknown').split(',')[0].strip()
        if not _RateLimiter.is_allowed(f"tg_shop_bot:{client_ip}", limit=60, window=60):
            return jsonify({'status': 'error', 'message': 'Rate limit exceeded'}), 429

        # Verifikasi secret token
        configured_secret = _get_setting('telegram_webhook_secret', '')
        if configured_secret:
            secret_token = request.headers.get('X-Telegram-Bot-Api-Secret-Token', '')
            if not hmac.compare_digest(secret_token, configured_secret):
                return Response('Unauthorized', status=401)

        try:
            update = json.loads(request.get_data() or b'{}')
        except json.JSONDecodeError:
            logger.warning("[TelegramShopBot] Invalid JSON")
            return jsonify({'status': 'error', 'message': 'Invalid JSON'}), 400

        try:
            logger.info(f"[TelegramShopBot] Update diterima: {json.dumps(update)[:300]}")

            if 'message' in update:
                message = update['message'] or {}
                chat = message.get('chat') or {}
                chat_id = chat.get('id')
                chat_type = chat.get('type', '')
                text = message.get('text', '')
                sender = message.get('from') or {}
                sender_name = f"{sender.get('first_name', '')} {sender.get('last_name', '')}".strip()
                username = sender.get('username', '')

                admin_chat_id = _admin_chat_id()
                is_from_admin_chat = bool(admin_chat_id) and chat_id is not None and str(chat_id) == str(admin_chat_id)

                # --- ADMIN PERINTAH MANUAL ---
                if is_from_admin_chat and text:
                    text_lower = text.lower().strip()
                    if text_lower.startswith('/reply'):
                        parts = text.split(maxsplit=2)
                        if len(parts) >= 3:
                            target_chat_id = parts[1]
                            reply_text = parts[2]
                            if reply_text.strip():
                                if send_telegram_message(target_chat_id, reply_text):
                                    send_telegram_message(chat_id, f"✅ Pesan terkirim ke {target_chat_id}")
                                else:
                                    send_telegram_message(chat_id, f"❌ Gagal kirim ke {target_chat_id}")
                            else:
                                send_telegram_message(chat_id, "⚠️ Pesan balasan tidak boleh kosong.")
                        else:
                            send_telegram_message(chat_id, "📝 Format: /reply <chat_id> <pesan>")
                        return jsonify({'status': 'ok'})

                    if text_lower.startswith('/endchat'):
                        parts = text.split()
                        if len(parts) >= 2:
                            target_chat_id = parts[1]
                            try:
                                target_id = int(target_chat_id)
                                if LiveChatManager.is_active(target_id):
                                    LiveChatManager.end(target_id)
                                    send_telegram_message(target_id, "🛑 Live chat diakhiri oleh admin.")
                                    send_telegram_message(chat_id, f"✅ Live chat dengan {target_chat_id} diakhiri.")
                                else:
                                    send_telegram_message(chat_id, f"ℹ️ Tidak ada sesi aktif untuk {target_chat_id}")
                            except ValueError:
                                send_telegram_message(chat_id, "❌ chat_id harus angka.")
                        else:
                            send_telegram_message(chat_id, "📝 Format: /endchat <chat_id>")
                        return jsonify({'status': 'ok'})

                # --- ADMIN REPLY (bawaan) ---
                if is_from_admin_chat and message.get('reply_to_message'):
                    _handle_admin_reply(message)
                    return jsonify({'status': 'ok'})

                # --- PROSES PESAN PELANGGAN ---
                if chat_id is not None:
                    text_lower = (text or '').lower().strip()

                    # Live Chat aktif (hanya private)
                    if chat_type == 'private' and LiveChatManager.is_active(chat_id):
                        if text_lower in _END_CHAT_KEYWORDS:
                            session = LiveChatManager.get(chat_id)
                            safe_name = _escape_html(session['name']) if session else _escape_html(sender_name) or 'Kak'
                            LiveChatManager.end(chat_id)
                            send_telegram_message(
                                chat_id,
                                "✅ Live chat diakhiri. Ketik <b>menu</b> untuk bantuan lagi."
                            )
                            _notify_admin_chat_ended(chat_id, safe_name, closer='pelanggan')
                        elif text.strip():
                            LiveChatManager.touch(chat_id)
                            if not _forward_customer_message_to_admin(chat_id, sender_name, username, text):
                                send_telegram_message(
                                    chat_id,
                                    "⚠️ Live chat tidak tersedia. Hubungi admin lewat WhatsApp: +62 895-3005-2136"
                                )
                        # Pesan kosong atau non-teks diabaikan
                        return jsonify({'status': 'ok'})

                    # Mulai Live Chat (private)
                    if chat_type == 'private' and text_lower in _START_CHAT_KEYWORDS:
                        safe_name = _escape_html(sender_name) or 'Kak'
                        LiveChatManager.start(chat_id, sender_name, username)
                        _notify_admin_chat_started(chat_id, safe_name, _escape_html(username))
                        send_telegram_message(
                            chat_id,
                            _live_chat_welcome_text(safe_name),
                            reply_markup=_end_chat_keyboard(),
                        )
                        return jsonify({'status': 'ok'})

                    # Menu FAQ + AI (private tanpa live chat, atau grup)
                    response_text = process_telegram_message(text, sender_name, username, chat_id)
                    markup = _quick_menu_keyboard() if chat_type == 'private' else None
                    send_telegram_message(chat_id, response_text, reply_markup=markup)

            elif 'callback_query' in update:
                callback_query = update['callback_query'] or {}
                callback_query_id = callback_query.get('id')
                cb_message = callback_query.get('message') or {}
                chat = cb_message.get('chat') or {}
                chat_id = chat.get('id')
                data = callback_query.get('data', '')
                cb_sender = callback_query.get('from') or {}
                cb_sender_name = f"{cb_sender.get('first_name', '')} {cb_sender.get('last_name', '')}".strip()
                cb_username = cb_sender.get('username', '')

                if callback_query_id:
                    _answer_callback_query(callback_query_id)
                if chat_id is not None:
                    response_text = process_callback_query(data, chat_id, cb_sender_name, cb_username)
                    markup = _end_chat_keyboard() if data == 'live_chat_start' else None
                    send_telegram_message(chat_id, response_text, reply_markup=markup)

            return jsonify({'status': 'ok'})

        except Exception as e:
            logger.error(f"[TelegramShopBot] Error: {e}", exc_info=True)
            return jsonify({'status': 'error', 'message': 'Internal error logged'}), 200

    logger.info("Telegram Shop Bot webhook registered at /webhook/telegram")


if __name__ == '__main__':
    print("[INFO] Modul Flask, bukan server standalone.")
    print("Cara pakai: from api.telegram_shop_bot import register_telegram_shop_bot")