"""
OpenRouter ↔ Telegram Bridge  –  V4 (Customer Service Edition)
===============================================================

Jembatan dua arah: setiap pesan Telegram dikirim ke OpenRouter AI,
AI memproses dan menentukan aksi backend.

V4 Additions:
  - AKSI baru: cek_resi, update_resi, update_status_pengiriman,
    cek_order_pelanggan, list_order_dikirim, konfirmasi_pembayaran,
    cancel_order, list_pending, daftar_kurir
  - SYSTEM_PROMPT diperluas: mode Admin + Customer Service
  - _resolve_order()  : lookup order fleksibel (nomor/id/HP pelanggan)
  - _format_tracking(): format resi tracking lengkap
  - ConversationMemory, TelegramRateLimiter (tetap dari V3)
"""

# ---------------------------------------------------------------------------
# Imports
# ---------------------------------------------------------------------------
import logging
import re
import time
from datetime import datetime, timedelta
from flask import request, jsonify

import requests as http_requests

try:
    from cachetools import TTLCache
    _CACHETOOLS_AVAILABLE = True
except ImportError:
    _CACHETOOLS_AVAILABLE = False
    TTLCache = None

logger = logging.getLogger(__name__)


# ---------------------------------------------------------------------------
# V3 – Conversation Memory & Rate Limiter (tetap)
# ---------------------------------------------------------------------------

if _CACHETOOLS_AVAILABLE:
    _MEMORY_CACHE: TTLCache = TTLCache(maxsize=5000, ttl=3600)
    _RATE_LIMIT_CACHE: TTLCache = TTLCache(maxsize=10000, ttl=60)
else:
    _MEMORY_CACHE = {}
    _RATE_LIMIT_CACHE = {}


class ConversationMemory:
    """Per-chat conversation history (simpan 20 giliran terakhir, 1 jam TTL)."""

    MAX_HISTORY = 20

    @staticmethod
    def get(chat_id: str) -> list:
        return list(_MEMORY_CACHE.get(str(chat_id), []))

    @staticmethod
    def append(chat_id: str, role: str, content: str) -> None:
        key = str(chat_id)
        history = list(_MEMORY_CACHE.get(key, []))
        history.append({"role": role, "content": content})
        _MEMORY_CACHE[key] = history[-ConversationMemory.MAX_HISTORY:]

    @staticmethod
    def clear(chat_id: str) -> None:
        _MEMORY_CACHE.pop(str(chat_id), None)


class TelegramRateLimiter:
    """Batasi 30 pesan/menit per chat_id."""

    LIMIT = 30
    WINDOW = 60

    @staticmethod
    def is_allowed(chat_id: str) -> bool:
        key = str(chat_id)
        now = time.monotonic()
        entry = _RATE_LIMIT_CACHE.get(key)

        if entry is None:
            _RATE_LIMIT_CACHE[key] = (1, now)
            return True

        count, start = entry
        if now - start >= TelegramRateLimiter.WINDOW:
            _RATE_LIMIT_CACHE[key] = (1, now)
            return True

        if count >= TelegramRateLimiter.LIMIT:
            return False

        _RATE_LIMIT_CACHE[key] = (count + 1, start)
        return True


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------

def _get_setting(key: str, default: str = '') -> str:
    """Ambil setting dari database dengan fallback ke default."""
    try:
        from models import Setting
        s = Setting.query.filter_by(key=key).first()
        return s.value if s and s.value else default
    except Exception:
        return default


def _format_price(amount) -> str:
    """Format angka ke format Rupiah."""
    try:
        return 'Rp {:,.0f}'.format(int(amount)).replace(',', '.')
    except (TypeError, ValueError):
        return 'Rp {}'.format(amount)


def _format_date(dt) -> str:
    """Format datetime ke string Indonesia."""
    if not dt:
        return '-'
    try:
        return dt.strftime('%d %b %Y %H:%M')
    except Exception:
        return str(dt)


def _resolve_order(identifier: str):
    """
    Cari order secara fleksibel:
    - Nomor order (INV-xxx)
    - ID integer
    - Nomor HP pelanggan (10+ digit)
    - Nomor resi pengiriman
    Kembalikan Order atau None.
    """
    if not identifier:
        return None
    try:
        from models import Order
        identifier = identifier.strip()

        # Coba nomor order eksplisit
        order = Order.query.filter_by(order_number=identifier).first()
        if order:
            return order

        # Coba nomor resi
        order = Order.query.filter_by(tracking_number=identifier).first()
        if order:
            return order

        # Coba ID integer
        try:
            order = Order.query.get(int(identifier))
            if order:
                return order
        except (ValueError, TypeError):
            pass

        # Coba nomor HP pelanggan => kembalikan order terbaru
        if re.match(r'^[0-9+\-\s]{8,15}$', identifier):
            phone_clean = re.sub(r'[^0-9]', '', identifier)
            order = (Order.query
                     .filter(Order.customer_phone.contains(phone_clean))
                     .order_by(Order.created_at.desc())
                     .first())
            if order:
                return order

        return None
    except Exception as e:
        logger.error('_resolve_order error: %s', e)
        return None


def _format_tracking_info(shipment, events: list) -> str:
    """Format info resi + history menjadi pesan Telegram yang rapi."""
    status_emoji = {
        'pending':           '📋',
        'picked_up':         '📦',
        'in_transit':        '🚚',
        'out_for_delivery':  '🛵',
        'delivered':         '✅',
        'failed_delivery':   '❌',
        'returned':          '↩️',
        'exception':         '⚠️',
    }
    emoji = status_emoji.get((shipment.status or '').lower(), '📦')

    lines = [
        '📬 <b>Info Resi Pengiriman</b>',
        '━━━━━━━━━━━━━━',
        '🔢 No. Resi: <code>{}</code>'.format(shipment.tracking_number),
        '📦 Kurir: {}'.format(shipment.carrier or '-'),
        '{} Status: {}'.format(emoji, (shipment.status or '-').replace('_', ' ').title()),
    ]

    if shipment.current_location:
        lines.append('📍 Lokasi Saat Ini: {}'.format(shipment.current_location))
    if shipment.estimated_delivery:
        lines.append('📅 Estimasi Tiba: {}'.format(_format_date(shipment.estimated_delivery)))
    if shipment.actual_delivery:
        lines.append('✅ Diterima: {}'.format(_format_date(shipment.actual_delivery)))
    if shipment.recipient_name:
        lines.append('👤 Penerima: {}'.format(shipment.recipient_name))
    if shipment.is_delayed:
        lines.append('⚠️ Terlambat: {} hari'.format(shipment.delay_days or 0))
    if shipment.has_issue:
        lines.append('🚨 Masalah: {}'.format(shipment.issue_type or 'Ada masalah pengiriman'))

    if events:
        lines.append('\n📋 <b>Riwayat Perjalanan:</b>')
        for ev in events[:8]:
            ev_time = _format_date(ev.event_time)
            status_txt = (ev.status or '').replace('_', ' ').title()
            desc = ev.status_description or ''
            loc = ' [{}]'.format(ev.location) if ev.location else ''
            lines.append('• {} — {}{}'.format(ev_time, status_txt, loc))
            if desc:
                lines.append('  📝 {}'.format(desc))

    return '\n'.join(lines)


# ---------------------------------------------------------------------------
# Bridge Class
# ---------------------------------------------------------------------------

class OpenRouterTelegramBridge:
    """
    Jembatan dua arah: Telegram <-> OpenRouter AI.

    V4: AI kini mendukung penuh customer service -- cek resi, update resi,
    update status pengiriman, invoice, konfirmasi bayar, dan lainnya.
    """

    SYSTEM_PROMPT = (
        'Anda adalah asisten AI toko online bernama "Pusat Gadai Indonesia".\n'
        'Anda membantu ADMIN dan PELANGGAN melalui Telegram.\n\n'

        '=== KEMAMPUAN ANDA ===\n\n'

        'RESI & PENGIRIMAN:\n'
        '1. Cek status resi/tracking (nomor resi, nomor order, atau nomor HP)\n'
        '2. Update nomor resi dan kurir untuk suatu order\n'
        '3. Tambah/update status event pengiriman (lokasi, deskripsi, waktu)\n'
        '4. Daftar order yang sedang dikirim\n\n'

        'ORDER & INVOICE:\n'
        '5. Cek status dan detail order pelanggan\n'
        '6. Generate invoice/kwitansi\n'
        '7. Generate surat penebusan (redemption letter)\n'
        '8. Konfirmasi pembayaran order\n'
        '9. Batalkan order (dengan alasan)\n'
        '10. List order pending / perlu tindakan\n\n'

        'RINGKASAN TOKO (admin):\n'
        '11. Ringkasan toko hari ini (produk, order, pendapatan)\n'
        '12. Sinkronisasi produk\n'
        '13. Kirim notifikasi\n\n'

        '=== FORMAT RESPONS ===\n\n'
        'Jika user bertanya info: jawab langsung dengan ramah dan jelas\n'
        'Jika user minta aksi: mulai dengan [AKSI:<nama>|param=nilai] lalu penjelasan\n'
        'Selalu tanyakan info yang kurang sebelum eksekusi aksi\n\n'

        '=== DAFTAR AKSI ===\n\n'
        'PENGIRIMAN & RESI:\n'
        '  [AKSI:cek_resi|value=<nomor_resi_atau_order_atau_HP>]\n'
        '  [AKSI:update_resi|order_id=<nomor_order>|tracking_number=<nomor_resi>|carrier=<kurir>|estimated_delivery=<YYYY-MM-DD>]\n'
        '  [AKSI:update_status_pengiriman|tracking_number=<nomor_resi>|status=<status>|location=<lokasi>|description=<keterangan>]\n'
        '  [AKSI:list_order_dikirim]\n\n'

        'ORDER & INVOICE:\n'
        '  [AKSI:order_status|value=<nomor_order_atau_HP>]\n'
        '  [AKSI:cek_order_pelanggan|phone=<nomor_HP>]\n'
        '  [AKSI:generate_invoice|order_id=<nomor_order>]\n'
        '  [AKSI:generate_redemption|order_id=<nomor_order>]\n'
        '  [AKSI:konfirmasi_pembayaran|order_id=<nomor_order>]\n'
        '  [AKSI:cancel_order|order_id=<nomor_order>|reason=<alasan>]\n'
        '  [AKSI:list_pending]\n\n'

        'TOKO:\n'
        '  [AKSI:store_summary]\n'
        '  [AKSI:sync_products]\n'
        '  [AKSI:send_notification|message=<pesan>|chat_id=<opsional>]\n'
        '  [AKSI:clear_memory]\n\n'

        'STATUS PENGIRIMAN VALID:\n'
        '  pending, picked_up, in_transit, out_for_delivery, delivered,\n'
        '  failed_delivery, returned, exception\n\n'

        'KURIR YANG DIDUKUNG:\n'
        '  JNE, JNT, TIKI, POS, Sicepat, Anteraja, Wahana, Ninja, Gosend, Grab\n\n'

        '=== CONTOH ===\n\n'
        'User: "Cek resi JNE0123456789"\n'
        'AI: "[AKSI:cek_resi|value=JNE0123456789] Baik, saya cek status resi tersebut."\n\n'
        'User: "Update resi order INV-2025-ABC, resinya JNE9876543210 kurir JNE"\n'
        'AI: "[AKSI:update_resi|order_id=INV-2025-ABC|tracking_number=JNE9876543210|carrier=JNE] Saya update resi sekarang."\n\n'
        'User: "Resi JNE9876 sudah sampai di Jakarta"\n'
        'AI: "[AKSI:update_status_pengiriman|tracking_number=JNE9876|status=in_transit|location=Jakarta|description=Paket tiba di hub Jakarta]"\n\n'
        'User: "Buatkan invoice order INV-20250101-ABC123"\n'
        'AI: "[AKSI:generate_invoice|order_id=INV-20250101-ABC123] Membuat invoice..."\n\n'
        'User: "Ada berapa order pending?"\n'
        'AI: "[AKSI:list_pending] Saya cek daftar order pending."\n\n'
        'User: "Cek pesanan nomor HP 08123456789"\n'
        'AI: "[AKSI:cek_order_pelanggan|phone=08123456789] Saya cari pesanan untuk nomor tersebut."\n\n'
        'Selalu jawab dalam Bahasa Indonesia yang sopan dan profesional.'
    )

    # ------------------------------------------------------------------ #
    # Config                                                               #
    # ------------------------------------------------------------------ #

    @staticmethod
    def _get_openrouter_config() -> dict:
        return {
            'base_url':  _get_setting('openrouter_base_url', 'https://openrouter.ai/api/v1').rstrip('/'),
            'api_key':   _get_setting('openrouter_api_key', '').strip(),
            'model':     _get_setting('openrouter_model', 'openrouter/free').strip() or 'openrouter/free',
            'site_url':  _get_setting('openrouter_site_url', 'https://slotriset.pythonanywhere.com').strip(),
            'app_name':  _get_setting('openrouter_app_name', 'Pusat Gadai Indonesia').strip(),
        }

    # ------------------------------------------------------------------ #
    # Core AI call                                                         #
    # ------------------------------------------------------------------ #

    @staticmethod
    def _call_openrouter(user_message: str, chat_context: list = None,
                         chat_id: str = None) -> dict:
        config = OpenRouterTelegramBridge._get_openrouter_config()

        if not config['api_key']:
            return {
                'success': False,
                'error': 'API Key OpenRouter belum dikonfigurasi. Atur di Pengaturan > Konfigurasi AI.'
            }

        messages = [{'role': 'system', 'content': OpenRouterTelegramBridge.SYSTEM_PROMPT}]

        if chat_context:
            messages.extend(chat_context)
        elif chat_id is not None:
            messages.extend(ConversationMemory.get(str(chat_id)))

        messages.append({'role': 'user', 'content': user_message})

        try:
            response = http_requests.post(
                '{}/chat/completions'.format(config['base_url']),
                headers={
                    'Authorization': 'Bearer {}'.format(config['api_key']),
                    'Content-Type': 'application/json',
                    'HTTP-Referer': config['site_url'],
                    'X-Title': config['app_name'],
                },
                json={
                    'model': config['model'],
                    'messages': messages,
                    'stream': False,
                    'temperature': 0.7,
                    'max_tokens': 1200,
                },
                timeout=60,
            )

            if response.status_code == 200:
                result = response.json()
                choices = result.get('choices', [])
                if choices:
                    reply = choices[0].get('message', {}).get('content', '')
                    usage = result.get('usage', {})
                    if usage:
                        logger.info('OpenRouter usage: %s tokens', usage.get('total_tokens', 0))

                    if chat_id is not None and reply:
                        ConversationMemory.append(str(chat_id), 'user', user_message)
                        ConversationMemory.append(str(chat_id), 'assistant', reply)

                    return {
                        'success': True,
                        'reply': reply,
                        'model': result.get('model', config['model']),
                    }
                return {'success': False, 'error': 'Respons AI kosong. Model mungkin tidak tersedia.'}

            elif response.status_code == 401:
                return {'success': False, 'error': 'API Key tidak valid. Periksa kembali API Key di Pengaturan.'}
            elif response.status_code == 429:
                return {'success': False, 'error': 'Rate limit tercapai. Tunggu beberapa saat dan coba lagi.'}
            elif response.status_code >= 500:
                return {'success': False, 'error': 'Server OpenRouter error ({}). Coba lagi nanti.'.format(response.status_code)}
            else:
                return {'success': False, 'error': 'HTTP {}: {}'.format(response.status_code, response.text[:300])}

        except http_requests.exceptions.ConnectionError:
            logger.error('Cannot connect to OpenRouter')
            return {'success': False, 'error': 'Tidak dapat terhubung ke OpenRouter. Periksa koneksi internet.'}
        except http_requests.exceptions.Timeout:
            logger.error('OpenRouter request timeout')
            return {'success': False, 'error': 'Request timeout (60s). Server AI terlalu lambat atau sibuk.'}
        except Exception as e:
            logger.error('OpenRouter API error: %s', e, exc_info=True)
            return {'success': False, 'error': 'Error tak terduga: {}'.format(str(e))}

    # ------------------------------------------------------------------ #
    # Action extraction                                                    #
    # ------------------------------------------------------------------ #

    @staticmethod
    def _extract_action(ai_reply: str) -> tuple:
        match = re.search(r'\[AKSI:([^\]]+)\]', ai_reply)
        if not match:
            return None, None

        action_str = match.group(1).strip()
        parts = action_str.split('|')
        action_name = parts[0].strip()
        params = {}

        for part in parts[1:]:
            if '=' in part:
                k, v = part.split('=', 1)
                params[k.strip()] = v.strip()

        return action_name, params

    # ------------------------------------------------------------------ #
    # Action execution – V4                                                #
    # ------------------------------------------------------------------ #

    @staticmethod
    def _execute_action(action_name: str, params: dict, chat_id: int) -> str:
        try:
            from models import db, Product, Order, Setting
            from sqlalchemy import func

            # ============================================================
            # CEK RESI
            # ============================================================
            if action_name == 'cek_resi':
                identifier = (
                    params.get('value') or params.get('tracking_number')
                    or params.get('order_id') or params.get('phone') or ''
                ).strip()

                if not identifier:
                    return (
                        '❓ <b>Cek Resi</b>\n\n'
                        'Mohon berikan salah satu informasi:\n'
                        '• Nomor resi pengiriman (contoh: JNE123456789)\n'
                        '• Nomor order (contoh: INV-2025-ABC)\n'
                        '• Nomor HP pelanggan'
                    )

                try:
                    from models_shipping_tracking import ShippingTracking, ShippingTrackingEvent

                    shipment = ShippingTracking.query.filter_by(tracking_number=identifier).first()

                    if not shipment:
                        order = _resolve_order(identifier)
                        if order and order.tracking_number:
                            shipment = ShippingTracking.query.filter_by(
                                tracking_number=order.tracking_number
                            ).first()
                            if not shipment:
                                return (
                                    '📦 <b>Info Pengiriman Order {}</b>\n'
                                    '━━━━━━━━━━━━━━\n'
                                    '👤 {}\n'
                                    '📱 {}\n'
                                    '🔢 No. Resi: <code>{}</code>\n'
                                    '🚚 Kurir: {}\n'
                                    '📋 Status Order: {}\n'
                                    '📅 Estimasi: {}\n\n'
                                    'ℹ️ Detail tracking lebih lanjut belum tersedia di sistem.'
                                ).format(
                                    order.order_number,
                                    order.customer_name,
                                    order.customer_phone,
                                    order.tracking_number,
                                    order.carrier_name or '-',
                                    order.status,
                                    _format_date(order.estimated_delivery),
                                )
                        elif order and not order.tracking_number:
                            return (
                                '📦 Order <b>{}</b> ditemukan.\n'
                                '👤 {}\n'
                                '📋 Status: {}\n\n'
                                '❌ Nomor resi belum diinput untuk order ini.\n'
                                'Admin dapat update resi dengan perintah:\n'
                                '<i>update resi {} [nomor resi] [kurir]</i>'
                            ).format(
                                order.order_number,
                                order.customer_name,
                                order.status,
                                order.order_number,
                            )

                    if not shipment:
                        return (
                            '❌ Resi <code>{}</code> tidak ditemukan di sistem.\n\n'
                            'Pastikan nomor resi benar, atau coba gunakan nomor order.'
                        ).format(identifier)

                    events = (ShippingTrackingEvent.query
                              .filter_by(shipping_tracking_id=shipment.id)
                              .order_by(ShippingTrackingEvent.event_time.desc())
                              .all())

                    return _format_tracking_info(shipment, events)

                except ImportError:
                    order = _resolve_order(identifier)
                    if not order:
                        return '❌ Resi/order <code>{}</code> tidak ditemukan.'.format(identifier)

                    return (
                        '📦 <b>Info Order {}</b>\n'
                        '━━━━━━━━━━━━━━\n'
                        '👤 {}\n'
                        '📱 {}\n'
                        '📋 Status: {}\n'
                        '🔢 Resi: <code>{}</code>\n'
                        '🚚 Kurir: {}\n'
                        '📅 Estimasi: {}'
                    ).format(
                        order.order_number,
                        order.customer_name,
                        order.customer_phone,
                        order.status,
                        order.tracking_number or 'Belum ada',
                        order.carrier_name or '-',
                        _format_date(order.estimated_delivery),
                    )

            # ============================================================
            # UPDATE RESI
            # ============================================================
            elif action_name == 'update_resi':
                order_identifier = (params.get('order_id') or params.get('value') or '').strip()
                tracking_number = params.get('tracking_number', '').strip()
                carrier = params.get('carrier', '').strip()
                estimated_delivery_str = params.get('estimated_delivery', '').strip()

                if not order_identifier:
                    return '❌ Nomor order diperlukan. Format: update resi <nomor_order> <nomor_resi> <kurir>'
                if not tracking_number:
                    return '❌ Nomor resi diperlukan.'
                if not carrier:
                    return '❌ Nama kurir diperlukan (JNE, JNT, TIKI, Sicepat, dll).'

                order = _resolve_order(order_identifier)
                if not order:
                    return '❌ Order <code>{}</code> tidak ditemukan.'.format(order_identifier)

                old_resi = order.tracking_number or '-'
                order.tracking_number = tracking_number
                order.carrier_name = carrier

                if estimated_delivery_str:
                    try:
                        order.estimated_delivery = datetime.strptime(estimated_delivery_str, '%Y-%m-%d')
                    except ValueError:
                        pass

                if order.status not in ('Shipped', 'Out_for_Delivery', 'Completed', 'Delivered'):
                    order.status = 'Shipped'
                    order.shipped_at = datetime.utcnow()

                order.updated_at = datetime.utcnow()

                try:
                    from models_shipping_tracking import ShippingTracking, ShippingTrackingEvent
                    existing = ShippingTracking.query.filter_by(order_id=order.id).first()
                    if existing:
                        existing.tracking_number = tracking_number
                        existing.carrier = carrier
                        if estimated_delivery_str:
                            try:
                                existing.estimated_delivery = datetime.strptime(
                                    estimated_delivery_str, '%Y-%m-%d'
                                )
                            except ValueError:
                                pass
                        existing.updated_at = datetime.utcnow()
                    else:
                        new_shipment = ShippingTracking(
                            order_id=order.id,
                            tracking_number=tracking_number,
                            carrier=carrier,
                            status='picked_up',
                            recipient_name=order.shipping_recipient_name or order.customer_name,
                            recipient_phone=order.shipping_phone or order.customer_phone,
                            recipient_address=order.shipping_address,
                        )
                        db.session.add(new_shipment)
                        db.session.flush()

                        first_event = ShippingTrackingEvent(
                            shipping_tracking_id=new_shipment.id,
                            status='picked_up',
                            status_description='Paket diambil dan siap dikirim',
                            event_time=datetime.utcnow(),
                        )
                        db.session.add(first_event)
                except ImportError:
                    pass

                db.session.commit()

                result_lines = [
                    '✅ <b>Resi Berhasil Diupdate</b>',
                    '━━━━━━━━━━━━━━',
                    '📦 Order: <code>{}</code>'.format(order.order_number),
                    '👤 Pelanggan: {}'.format(order.customer_name),
                    '🔢 Resi Lama: <code>{}</code>'.format(old_resi),
                    '🔢 Resi Baru: <code>{}</code>'.format(tracking_number),
                    '🚚 Kurir: {}'.format(carrier),
                    '📋 Status: {}'.format(order.status),
                ]
                if order.estimated_delivery:
                    result_lines.append('📅 Estimasi: {}'.format(_format_date(order.estimated_delivery)))

                return '\n'.join(result_lines)

            # ============================================================
            # UPDATE STATUS PENGIRIMAN
            # ============================================================
            elif action_name == 'update_status_pengiriman':
                tracking_number = (params.get('tracking_number') or params.get('value') or '').strip()
                new_status = params.get('status', 'in_transit').strip().lower()
                location = params.get('location', '').strip()
                description = params.get('description', '').strip()
                event_time_str = params.get('event_time', '').strip()

                VALID_STATUSES = [
                    'pending', 'picked_up', 'in_transit', 'out_for_delivery',
                    'delivered', 'failed_delivery', 'returned', 'exception'
                ]

                if not tracking_number:
                    return '❌ Nomor resi diperlukan.'

                if new_status not in VALID_STATUSES:
                    return (
                        '❌ Status tidak valid: <code>{}</code>\n\n'
                        'Status yang tersedia:\n'.format(new_status)
                        + '\n'.join('• ' + s for s in VALID_STATUSES)
                    )

                try:
                    from models_shipping_tracking import ShippingTracking, ShippingTrackingEvent

                    shipment = ShippingTracking.query.filter_by(tracking_number=tracking_number).first()

                    if not shipment:
                        order = Order.query.filter_by(tracking_number=tracking_number).first()
                        if not order:
                            return '❌ Resi <code>{}</code> tidak ditemukan.'.format(tracking_number)

                        shipment = ShippingTracking(
                            order_id=order.id,
                            tracking_number=tracking_number,
                            carrier=order.carrier_name or '',
                            status=new_status,
                            recipient_name=order.shipping_recipient_name or order.customer_name,
                            recipient_phone=order.shipping_phone or order.customer_phone,
                            recipient_address=order.shipping_address,
                        )
                        db.session.add(shipment)
                        db.session.flush()

                    if event_time_str:
                        try:
                            event_dt = datetime.strptime(event_time_str, '%Y-%m-%d %H:%M')
                        except ValueError:
                            try:
                                event_dt = datetime.strptime(event_time_str, '%Y-%m-%d')
                            except ValueError:
                                event_dt = datetime.utcnow()
                    else:
                        event_dt = datetime.utcnow()

                    DEFAULT_DESC = {
                        'pending':           'Paket menunggu penjemputan',
                        'picked_up':         'Paket telah dijemput kurir',
                        'in_transit':        'Paket dalam perjalanan',
                        'out_for_delivery':  'Paket sedang diantarkan ke penerima',
                        'delivered':         'Paket telah diterima',
                        'failed_delivery':   'Pengiriman gagal, akan dicoba kembali',
                        'returned':          'Paket dikembalikan ke pengirim',
                        'exception':         'Ada masalah pada pengiriman',
                    }
                    if not description:
                        description = DEFAULT_DESC.get(new_status, new_status.replace('_', ' ').title())

                    new_event = ShippingTrackingEvent(
                        shipping_tracking_id=shipment.id,
                        status=new_status,
                        location=location or None,
                        status_description=description,
                        event_time=event_dt,
                    )
                    db.session.add(new_event)

                    shipment.status = new_status
                    if location:
                        shipment.current_location = location
                    if new_status == 'delivered':
                        shipment.actual_delivery = event_dt
                        if shipment.order_id:
                            order_obj = Order.query.get(shipment.order_id)
                            if order_obj:
                                order_obj.status = 'Completed'
                                order_obj.completed_at = event_dt

                    shipment.updated_at = datetime.utcnow()
                    db.session.commit()

                    status_labels = {
                        'pending':           '📋 Menunggu Penjemputan',
                        'picked_up':         '📦 Paket Dijemput',
                        'in_transit':        '🚚 Dalam Perjalanan',
                        'out_for_delivery':  '🛵 Sedang Diantarkan',
                        'delivered':         '✅ Terkirim',
                        'failed_delivery':   '❌ Gagal Dikirim',
                        'returned':          '↩️ Dikembalikan',
                        'exception':         '⚠️ Ada Masalah',
                    }
                    label = status_labels.get(new_status, new_status.replace('_', ' ').title())

                    return (
                        '✅ <b>Status Pengiriman Diupdate</b>\n'
                        '━━━━━━━━━━━━━━\n'
                        '🔢 Resi: <code>{}</code>\n'
                        '📋 Status Baru: {}\n'
                        '📍 Lokasi: {}\n'
                        '📝 Keterangan: {}\n'
                        '🕐 Waktu: {}'
                    ).format(tracking_number, label, location or '-', description, _format_date(event_dt))

                except ImportError:
                    return (
                        '❌ Modul shipping tracking tidak tersedia.\n'
                        'Hubungi developer untuk mengaktifkan fitur ini.'
                    )

            # ============================================================
            # LIST ORDER DIKIRIM
            # ============================================================
            elif action_name == 'list_order_dikirim':
                shipped_orders = (Order.query
                                  .filter(Order.status.in_(['Shipped', 'Out_for_Delivery']))
                                  .order_by(Order.shipped_at.desc())
                                  .limit(10)
                                  .all())

                if not shipped_orders:
                    return '📦 Tidak ada order yang sedang dalam pengiriman saat ini.'

                lines = [
                    '🚚 <b>Order Sedang Dikirim ({})</b>'.format(len(shipped_orders)),
                    '━━━━━━━━━━━━━━'
                ]
                for o in shipped_orders:
                    status_icon = '🚚' if o.status == 'Shipped' else '🛵'
                    resi_text = '📦 {}'.format(o.tracking_number) if o.tracking_number else '❌ Belum ada resi'
                    lines.append(
                        '\n{} <b>{}</b>\n'
                        '  👤 {} | {}\n'
                        '  {} [{}]\n'
                        '  📅 Dikirim: {}'
                        .format(
                            status_icon, o.order_number,
                            o.customer_name, o.customer_phone,
                            resi_text, o.carrier_name or '-',
                            _format_date(o.shipped_at),
                        )
                    )

                return '\n'.join(lines)

            # ============================================================
            # CEK ORDER PELANGGAN
            # ============================================================
            elif action_name == 'cek_order_pelanggan':
                phone = (params.get('phone') or params.get('value') or '').strip()
                phone_clean = re.sub(r'[^0-9]', '', phone)

                if not phone_clean or len(phone_clean) < 8:
                    return (
                        '❓ Mohon berikan nomor HP pelanggan.\n'
                        'Contoh: <i>cek order pelanggan 08123456789</i>'
                    )

                orders = (Order.query
                          .filter(Order.customer_phone.contains(phone_clean))
                          .order_by(Order.created_at.desc())
                          .limit(5)
                          .all())

                if not orders:
                    return '❌ Tidak ada order untuk nomor HP <code>{}</code>.'.format(phone)

                status_map = {
                    'Pending':          '⏳ Menunggu Bayar',
                    'Paid':             '💰 Lunas',
                    'Processing':       '⚙️ Diproses',
                    'Ready_to_Ship':    '📦 Siap Kirim',
                    'Shipped':          '🚚 Dikirim',
                    'Out_for_Delivery': '🛵 Diantarkan',
                    'Completed':        '✅ Selesai',
                    'Cancelled':        '❌ Dibatalkan',
                    'Returned':         '↩️ Dikembalikan',
                }

                lines = [
                    '📋 <b>Order Pelanggan {}</b>'.format(phone),
                    '👤 {}'.format(orders[0].customer_name),
                    '━━━━━━━━━━━━━━'
                ]
                for o in orders:
                    resi = '\n  🔢 Resi: <code>{}</code>'.format(o.tracking_number) if o.tracking_number else ''
                    lines.append(
                        '\n📌 <b>{}</b>\n'
                        '  💰 {}\n'
                        '  📋 {}\n'
                        '  📅 {}'
                        '{}'
                        .format(
                            o.order_number,
                            _format_price(o.total),
                            status_map.get(o.status, o.status),
                            _format_date(o.created_at),
                            resi,
                        )
                    )

                return '\n'.join(lines)

            # ============================================================
            # ORDER STATUS
            # ============================================================
            elif action_name == 'order_status':
                identifier = params.get('order_id') or params.get('value', '')
                if not identifier:
                    return '❌ ID order tidak ditemukan. Format: cek order <nomor_order>'

                order = _resolve_order(identifier)
                if not order:
                    return '❌ Order <code>{}</code> tidak ditemukan di database.'.format(identifier)

                status_map = {
                    'Pending':          '⏳ Menunggu Pembayaran',
                    'Paid':             '💰 Lunas',
                    'Processing':       '⚙️ Diproses',
                    'Ready_to_Ship':    '📦 Siap Kirim',
                    'Shipped':          '🚚 Dikirim',
                    'Out_for_Delivery': '🛵 Dalam Pengantaran',
                    'Completed':        '✅ Selesai',
                    'Cancelled':        '❌ Dibatalkan',
                    'Returned':         '↩️ Dikembalikan',
                }
                items_text = ''
                if order.items:
                    items_text = '\n📋 <b>Items:</b>\n' + '\n'.join(
                        ['  • {} x{}'.format(item.name, item.quantity) for item in order.items[:5]]
                    )

                resi_line = ''
                if order.tracking_number:
                    resi_line = '\n🔢 Resi: <code>{}</code> [{}]'.format(
                        order.tracking_number, order.carrier_name or '-'
                    )

                return (
                    '📦 <b>Detail Order</b>\n'
                    '━━━━━━━━━━━━━━\n'
                    '📌 {}\n'
                    '👤 {}\n'
                    '📱 {}\n'
                    '💰 Total: {}\n'
                    '📋 Status: {}\n'
                    '💳 Metode: {}\n'
                    '🕐 Order: {}'
                    '{}'
                    '{}'
                ).format(
                    order.order_number,
                    order.customer_name,
                    order.customer_phone,
                    _format_price(order.total),
                    status_map.get(order.status, order.status),
                    order.payment_method or '-',
                    _format_date(order.created_at),
                    resi_line,
                    items_text,
                )

            # ============================================================
            # GENERATE INVOICE
            # ============================================================
            elif action_name == 'generate_invoice':
                identifier = params.get('order_id') or params.get('value', '')
                if not identifier:
                    return '❌ Nomor order diperlukan untuk membuat invoice.'

                order = _resolve_order(identifier)
                if not order:
                    return '❌ Order <code>{}</code> tidak ditemukan.'.format(identifier)

                items_list = ''
                if order.items:
                    for item in order.items:
                        price = _format_price(
                            getattr(item, 'price', 0) * getattr(item, 'quantity', 1)
                        )
                        items_list += '\n  • {} x{} = {}'.format(item.name, item.quantity, price)

                return (
                    '🧾 <b>Invoice Order {}</b>\n'
                    '━━━━━━━━━━━━━━\n'
                    '👤 {}\n'
                    '📱 {}\n'
                    '💰 Subtotal: {}\n'
                    '🚚 Ongkir: {}\n'
                    '🏷️ Diskon: {}\n'
                    '💵 <b>Total: {}</b>\n'
                    '💳 Metode: {}'
                    '{}\n\n'
                    '🔗 Lihat/Cetak Invoice:\n/invoice/{}'
                ).format(
                    order.order_number,
                    order.customer_name,
                    order.customer_phone,
                    _format_price(order.subtotal),
                    _format_price(order.shipping_cost),
                    _format_price(order.discount),
                    _format_price(order.total),
                    order.payment_method or '-',
                    items_list,
                    order.id,
                )

            # ============================================================
            # GENERATE REDEMPTION
            # ============================================================
            elif action_name == 'generate_redemption':
                identifier = params.get('order_id') or params.get('value', '')
                if not identifier:
                    return '❌ Nomor order diperlukan.'

                order = _resolve_order(identifier)
                if not order:
                    return '❌ Order <code>{}</code> tidak ditemukan.'.format(identifier)

                return (
                    '📄 <b>Surat Penebusan — {}</b>\n'
                    '👤 {}\n'
                    '💰 Total: {}\n\n'
                    '🔗 Lihat Surat Penebusan:\n/admin/redemption-letter/{}'
                ).format(
                    order.order_number,
                    order.customer_name,
                    _format_price(order.total),
                    order.id,
                )

            # ============================================================
            # KONFIRMASI PEMBAYARAN
            # ============================================================
            elif action_name == 'konfirmasi_pembayaran':
                identifier = params.get('order_id') or params.get('value', '')
                if not identifier:
                    return '❌ Nomor order diperlukan.'

                order = _resolve_order(identifier)
                if not order:
                    return '❌ Order <code>{}</code> tidak ditemukan.'.format(identifier)

                if order.status == 'Paid':
                    return (
                        'ℹ️ Order <b>{}</b> sudah berstatus <b>Lunas</b>.\n'
                        'Tidak ada perubahan yang dilakukan.'
                    ).format(order.order_number)

                order.status = 'Paid'
                order.paid_at = datetime.utcnow()
                order.updated_at = datetime.utcnow()
                db.session.commit()

                return (
                    '✅ <b>Pembayaran Dikonfirmasi</b>\n'
                    '━━━━━━━━━━━━━━\n'
                    '📌 Order: <code>{}</code>\n'
                    '👤 {}\n'
                    '💰 Total: {}\n'
                    '🕐 Dikonfirmasi: {}\n\n'
                    '📲 Notifikasi WhatsApp akan dikirim ke pelanggan.'
                ).format(
                    order.order_number,
                    order.customer_name,
                    _format_price(order.total),
                    _format_date(datetime.utcnow()),
                )

            # ============================================================
            # CANCEL ORDER
            # ============================================================
            elif action_name == 'cancel_order':
                identifier = params.get('order_id') or params.get('value', '')
                reason = params.get('reason', 'Dibatalkan oleh admin').strip()

                if not identifier:
                    return '❌ Nomor order diperlukan.'

                order = _resolve_order(identifier)
                if not order:
                    return '❌ Order <code>{}</code> tidak ditemukan.'.format(identifier)

                if order.status in ('Completed', 'Shipped', 'Out_for_Delivery'):
                    return (
                        '⚠️ Order <b>{}</b> tidak bisa dibatalkan.\n'
                        'Status saat ini: <b>{}</b>\n'
                        'Order yang sudah dikirim tidak dapat dibatalkan melalui AI.\n'
                        'Gunakan panel admin untuk tindakan lebih lanjut.'
                    ).format(order.order_number, order.status)

                if order.status == 'Cancelled':
                    return 'ℹ️ Order <code>{}</code> sudah dibatalkan sebelumnya.'.format(order.order_number)

                order.status = 'Cancelled'
                order.cancel_reason = reason
                order.updated_at = datetime.utcnow()
                db.session.commit()

                return (
                    '❌ <b>Order Dibatalkan</b>\n'
                    '━━━━━━━━━━━━━━\n'
                    '📌 Order: <code>{}</code>\n'
                    '👤 {}\n'
                    '💰 {}\n'
                    '📝 Alasan: {}\n'
                    '🕐 {}'
                ).format(
                    order.order_number,
                    order.customer_name,
                    _format_price(order.total),
                    reason,
                    _format_date(datetime.utcnow()),
                )

            # ============================================================
            # LIST PENDING
            # ============================================================
            elif action_name == 'list_pending':
                pending_orders = (Order.query
                                  .filter(Order.status.in_(['Pending', 'Paid', 'Processing']))
                                  .order_by(Order.created_at.asc())
                                  .limit(10)
                                  .all())

                if not pending_orders:
                    return '✅ Tidak ada order yang perlu tindakan saat ini.'

                status_map = {
                    'Pending':    '⏳ Menunggu Bayar',
                    'Paid':       '💰 Lunas - Belum Diproses',
                    'Processing': '⚙️ Sedang Diproses',
                }
                lines = [
                    '📋 <b>Order Perlu Tindakan ({})</b>'.format(len(pending_orders)),
                    '━━━━━━━━━━━━━━'
                ]
                for o in pending_orders:
                    lines.append(
                        '\n• <b>{}</b>\n'
                        '  👤 {} | 📱 {}\n'
                        '  💰 {}\n'
                        '  {}\n'
                        '  📅 {}'
                        .format(
                            o.order_number,
                            o.customer_name, o.customer_phone,
                            _format_price(o.total),
                            status_map.get(o.status, o.status),
                            _format_date(o.created_at),
                        )
                    )

                return '\n'.join(lines)

            # ============================================================
            # STORE SUMMARY
            # ============================================================
            elif action_name == 'store_summary':
                total_products = Product.query.count()
                total_orders = Order.query.count()
                pending = Order.query.filter_by(status='Pending').count()
                paid = Order.query.filter_by(status='Paid').count()
                processing = Order.query.filter_by(status='Processing').count()
                shipped = Order.query.filter(
                    Order.status.in_(['Shipped', 'Out_for_Delivery'])
                ).count()
                completed = Order.query.filter_by(status='Completed').count()
                revenue = db.session.query(
                    func.coalesce(func.sum(Order.total), 0)
                ).filter(
                    Order.status.in_(['Paid', 'Completed', 'Shipped', 'Out_for_Delivery'])
                ).scalar()
                today = datetime.now().date()
                today_orders = Order.query.filter(
                    db.func.date(Order.created_at) == today
                ).count()

                return (
                    '📊 <b>Ringkasan Toko Hari Ini</b>\n'
                    '━━━━━━━━━━━━━━\n'
                    '📦 Total Produk: {}\n'
                    '🛒 Total Order: {}\n'
                    '📅 Order Hari Ini: {}\n'
                    '⏳ Pending Bayar: {}\n'
                    '💰 Lunas (proses): {}\n'
                    '⚙️ Diproses: {}\n'
                    '🚚 Dikirim: {}\n'
                    '✅ Selesai: {}\n'
                    '💰 <b>Total Pendapatan: {}</b>'
                ).format(
                    total_products, total_orders, today_orders,
                    pending, paid, processing, shipped, completed,
                    _format_price(revenue),
                )

            # ============================================================
            # SYNC PRODUCTS
            # ============================================================
            elif action_name == 'sync_products':
                products = Product.query.filter_by(is_active=True).count()
                return '✅ Sinkronisasi berhasil. {} produk aktif tersedia di katalog.'.format(products)

            # ============================================================
            # SEND NOTIFICATION
            # ============================================================
            elif action_name == 'send_notification':
                target_chat = params.get('chat_id', str(chat_id))
                message = params.get('message', 'Notifikasi dari admin')
                bot_token = _get_setting('telegram_bot_token')
                if bot_token:
                    try:
                        from api.telegram_gateway import TelegramGateway
                        TelegramGateway.send_message(target_chat, message, bot_token)
                        return '✅ Notifikasi terkirim ke chat {}.'.format(target_chat)
                    except Exception as e:
                        return '❌ Gagal kirim notifikasi: {}'.format(str(e))
                return '❌ Bot token Telegram belum dikonfigurasi. Atur di Pengaturan > Notifikasi.'

            # ============================================================
            # CREATE PRODUCT
            # ============================================================
            elif action_name == 'create_product':
                product_name = params.get('name') or params.get('value', '')
                if not product_name:
                    return '❌ Nama produk diperlukan.'
                return (
                    "📝 Produk '<b>{}</b>' perlu dibuat melalui panel admin:\n"
                    '🔗 /admin/product/new'
                ).format(product_name)

            # ============================================================
            # CLEAR MEMORY
            # ============================================================
            elif action_name == 'clear_memory':
                ConversationMemory.clear(str(chat_id))
                return '🗑️ Riwayat percakapan dihapus. Mulai sesi baru.'

            else:
                return (
                    '❓ Aksi <code>{}</code> belum diimplementasikan.\n'
                    'Hubungi developer atau gunakan panel admin.'
                ).format(action_name)

        except Exception as e:
            logger.error('Action execution error [%s]: %s', action_name, e, exc_info=True)
            return '❌ Error saat mengeksekusi aksi <b>{}</b>: {}'.format(action_name, str(e))

    # ------------------------------------------------------------------ #
    # Webhook registration                                                 #
    # ------------------------------------------------------------------ #

    @staticmethod
    def setup_telegram_webhook(app):
        """
        Daftarkan endpoint webhook Telegram di Flask app.
        Route: POST /telegram/webhook/<token>
        """

        @app.route('/telegram/webhook/<path:token>', methods=['POST'])
        def telegram_webhook(token):
            expected = _get_setting('telegram_bot_token', '')
            if not expected or token != expected:
                logger.warning('Invalid Telegram webhook token attempt')
                return jsonify({'error': 'Invalid token'}), 403

            update = request.get_json(force=True, silent=True) or {}
            message = update.get('message') or {}
            text = (message.get('text') or '').strip()

            if not text:
                return jsonify({'status': 'ignored'}), 200

            chat = message.get('chat') or {}
            chat_id = chat.get('id')
            if chat_id is None:
                return jsonify({'error': 'chat_id missing'}), 400

            user_data = message.get('from') or {}
            user_name = user_data.get('first_name') or user_data.get('username') or 'User'
            logger.info('Telegram [%s] %s: %s', chat_id, user_name, text[:120])

            if not TelegramRateLimiter.is_allowed(str(chat_id)):
                _send_reply(
                    str(chat_id), expected,
                    '⚠️ Terlalu banyak pesan. Tunggu sebentar ({}/mnt).'.format(TelegramRateLimiter.LIMIT)
                )
                return jsonify({'status': 'rate_limited'}), 200

            ai_result = OpenRouterTelegramBridge._call_openrouter(text, chat_id=str(chat_id))

            if not ai_result.get('success'):
                reply = (
                    '❌ <b>Error Koneksi AI</b>\n\n'
                    '{}\n\n'
                    '💡 Pastikan konfigurasi OpenRouter sudah benar di '
                    '<b>Pengaturan > Konfigurasi AI</b>.'
                ).format(ai_result.get('error', 'Unknown error'))
            else:
                ai_reply = ai_result.get('reply', '')
                action_name, params = OpenRouterTelegramBridge._extract_action(ai_reply)

                if action_name:
                    action_result = OpenRouterTelegramBridge._execute_action(
                        action_name, params or {}, chat_id
                    )
                    clean_reply = re.sub(r'\[AKSI:[^\]]+\]\s*', '', ai_reply).strip()
                    reply = '{}\n\n{}'.format(clean_reply, action_result) if clean_reply else action_result
                else:
                    reply = ai_reply

            if reply:
                _send_reply(str(chat_id), expected, reply)

            return jsonify({
                'status': 'ok',
                'ai_processed': ai_result.get('success', False),
            }), 200

        logger.info('OpenRouter Telegram Bridge V4 registered at POST /telegram/webhook/<token>')

    # ------------------------------------------------------------------ #
    # Utility: AI notification                                             #
    # ------------------------------------------------------------------ #

    @staticmethod
    def send_ai_notification(message: str, chat_id: str = None) -> bool:
        bot_token = _get_setting('telegram_bot_token')
        target = chat_id or _get_setting('telegram_chat_id')

        if not bot_token or not target:
            logger.warning('Telegram not configured for AI notification')
            return False

        ai_result = OpenRouterTelegramBridge._call_openrouter(
            'Format pesan notifikasi berikut agar lebih profesional dan menarik:\n\n{}'.format(message)
        )

        final_message = ai_result['reply'] if ai_result.get('success') else message

        try:
            from api.telegram_gateway import TelegramGateway
            TelegramGateway.send_message(target, final_message, bot_token)
            logger.info('AI notification sent to %s', target)
            return True
        except Exception as e:
            logger.error('Failed to send AI notification: %s', e)
            return False


# ---------------------------------------------------------------------------
# Module-level helper
# ---------------------------------------------------------------------------

def _send_reply(chat_id: str, bot_token: str, text: str) -> None:
    """Kirim pesan ke Telegram; log error jika gagal."""
    try:
        from api.telegram_gateway import TelegramGateway
        TelegramGateway.send_message(chat_id, text, bot_token)
    except Exception as e:
        logger.error('Failed to send Telegram reply to %s: %s', chat_id, e)