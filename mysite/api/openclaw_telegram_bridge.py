# === V3 ADDITIONS ===
from cachetools import TTLCache
MEMORY_CACHE = TTLCache(maxsize=5000, ttl=3600)
RATE_LIMIT_CACHE = TTLCache(maxsize=10000, ttl=60)

class ConversationMemory:
    @staticmethod
    def get(chat_id):
        return MEMORY_CACHE.get(str(chat_id), [])

    @staticmethod
    def append(chat_id, role, content):
        h = MEMORY_CACHE.get(str(chat_id), [])
        h.append({"role": role, "content": content})
        MEMORY_CACHE[str(chat_id)] = h[-20:]

class TelegramRateLimiter:
    LIMIT = 30


import logging
import requests
import re
from flask import request, jsonify
from datetime import datetime

logger = logging.getLogger(__name__)

def _get_setting(key: str, default: str = '') -> str:
    """Ambil setting dari database dengan fallback ke default."""
    try:
        from models import Setting
        s = Setting.query.filter_by(key=key).first()
        return s.value if s and s.value else default
    except Exception:
        return default


class OpenRouterTelegramBridge:
    """
    Jembatan dua arah: Telegram ↔ OpenRouter AI.

    Setiap pesan Telegram dikirim ke OpenRouter AI untuk diproses
    secara cerdas. AI dapat:
    - Menjawab pertanyaan tentang produk & order
    - Memicu aksi backend (sync, invoice, redemption, dll.)
    - Memberikan rekomendasi berbasis data toko

    Dokumentasi OpenRouter: https://openrouter.ai/docs/api-reference/overview
    """

    SYSTEM_PROMPT = """Anda adalah asisten AI admin toko online bernama "Pusat Gadai Indonesia".
Anda memiliki akses ke sistem manajemen toko dan dapat membantu admin melalui Telegram.

KEMAMPUAN ANDA:
1. Jawab pertanyaan tentang produk, order, pengiriman, dan pembayaran
2. Berikan ringkasan data toko (jumlah produk, order, pendapatan)
3. Memicu aksi: sinkronisasi produk, cek status order, buat invoice, buat surat penebusan
4. Kirim notifikasi ke channel Telegram

FORMAT RESPONS:
- Jika user bertanya info: jawab langsung dengan ramah dan profesional
- Jika user minta aksi: mulai dengan [AKSI:<nama_aksi>] lalu penjelasan
- Aksi yang tersedia: sync_products, order_status, create_product, generate_invoice, generate_redemption, send_notification, store_summary

CONTOH:
User: "Ada berapa order hari ini?"
AI: "Hari ini terdapat 5 order baru dengan total pendapatan Rp 2.500.000."

User: "Buatkan invoice untuk order INV-20250101-ABC123"
AI: "[AKSI:generate_invoice] Baik, saya akan membuatkan invoice untuk order INV-20250101-ABC123."

User: "Sync produk"
AI: "[AKSI:sync_products] Menyinkronkan semua produk aktif ke gateway..."

Selalu jawab dalam Bahasa Indonesia yang sopan dan profesional."""

    @staticmethod
    def _get_openrouter_config() -> dict:
        """Ambil konfigurasi OpenRouter dari database."""
        return {
            'base_url': _get_setting('openrouter_base_url', 'https://openrouter.ai/api/v1').rstrip('/'),
            'api_key': _get_setting('openrouter_api_key', '').strip(),
            'model': _get_setting('openrouter_model', 'openrouter/free').strip() or 'openrouter/free',
            'site_url': _get_setting('openrouter_site_url', 'https://pusatgadaiidn.pythonanywhere.com').strip(),
            'app_name': _get_setting('openrouter_app_name', 'pusat gadai').strip(),
        }

    @staticmethod
    def _call_openrouter(user_message: str, chat_context: list = None) -> dict:
        """
        Kirim pesan ke OpenRouter AI dan kembalikan respons.

        Menggunakan endpoint /chat/completions dengan format OpenAI-compatible.
        Headers wajib: Authorization, HTTP-Referer, X-Title.
        """
        config = OpenRouterTelegramBridge._get_openrouter_config()

        if not config['api_key']:
            return {'success': False, 'error': 'API Key OpenRouter belum dikonfigurasi. Atur di Pengaturan > Konfigurasi AI.'}

        messages = [{'role': 'system', 'content': OpenRouterTelegramBridge.SYSTEM_PROMPT}]

        if chat_context:
            messages.extend(chat_context)

        messages.append({'role': 'user', 'content': user_message})

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
                    'messages': messages,
                    'stream': False,
                    'temperature': 0.7,
                    'max_tokens': 1000
                },
                timeout=60
            )

            if response.status_code == 200:
                result = response.json()
                choices = result.get('choices', [])
                if choices:
                    reply = choices[0].get('message', {}).get('content', '')
                    # Log usage jika tersedia
                    usage = result.get('usage', {})
                    if usage:
                        logger.info(f"OpenRouter usage: {usage.get('total_tokens', 0)} tokens")
                    return {'success': True, 'reply': reply, 'model': result.get('model', config['model'])}
                return {'success': False, 'error': 'Respons AI kosong. Model mungkin tidak tersedia.'}

            elif response.status_code == 401:
                return {'success': False, 'error': 'API Key tidak valid. Periksa kembali API Key di Pengaturan.'}
            elif response.status_code == 429:
                return {'success': False, 'error': 'Rate limit tercapai. Tunggu beberapa saat dan coba lagi.'}
            elif response.status_code >= 500:
                return {'success': False, 'error': f'Server OpenRouter error ({response.status_code}). Coba lagi nanti.'}
            else:
                return {
                    'success': False,
                    'error': f'HTTP {response.status_code}: {response.text[:300]}'
                }

        except requests.exceptions.ConnectionError:
            logger.error("Cannot connect to OpenRouter")
            return {'success': False, 'error': 'Tidak dapat terhubung ke OpenRouter. Periksa koneksi internet atau coba gunakan proxy.'}
        except requests.exceptions.Timeout:
            logger.error("OpenRouter request timeout")
            return {'success': False, 'error': 'Request timeout (60s). Server AI terlalu lambat atau sibuk.'}
        except Exception as e:
            logger.error(f"OpenRouter API error: {e}", exc_info=True)
            return {'success': False, 'error': f'Error tak terduga: {str(e)}'}

    @staticmethod
    def _extract_action(ai_reply: str) -> tuple:
        """
        Ekstrak aksi dari respons AI.
        Format: [AKSI:nama_aksi] atau [AKSI:nama_aksi|param1=value1|param2=value2]
        Returns: (action_name, params_dict) atau (None, None)
        """
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

    @staticmethod
    def _execute_action(action_name: str, params: dict, chat_id: int) -> str:
        """
        Eksekusi aksi yang diminta AI dan kembalikan hasil.
        """
        try:
            from models import db, Product, Order
            from sqlalchemy import func
            from api.telegram_gateway import TelegramGateway

            if action_name == 'sync_products':
                products = Product.query.filter_by(is_active=True).count()
                return f"✅ Sinkronisasi berhasil. {products} produk aktif tersedia di katalog."

            elif action_name == 'order_status':
                order_id = params.get('order_id') or params.get('value', '')
                if not order_id:
                    return "❌ ID order tidak ditemukan. Format: cek order <nomor_order>"

                order = Order.query.filter_by(order_number=order_id).first()
                if not order:
                    try:
                        order = Order.query.get(int(order_id))
                    except ValueError:
                        pass

                if order:
                    status_map = {
                        'Pending': '⏳ Menunggu Pembayaran',
                        'Paid': '💰 Lunas',
                        'Processing': '⚙️ Diproses',
                        'Ready_to_Ship': '📦 Siap Kirim',
                        'Shipped': '🚚 Dikirim',
                        'Out_for_Delivery': '🛵 Dalam Pengantaran',
                        'Completed': '✅ Selesai',
                        'Cancelled': '❌ Dibatalkan',
                        'Returned': '↩️ Dikembalikan'
                    }
                    items_text = ""
                    if order.items:
                        items_text = "\n📋 Items:\n" + "\n".join([f"  • {item.name} x{item.quantity}" for item in order.items[:5]])

                    return (
                        f"📦 <b>Order {order.order_number}</b>\n"
                        f"━━━━━━━━━━━━━━\n"
                        f"👤 {order.customer_name}\n"
                        f"📱 {order.customer_phone}\n"
                        f"💰 Total: Rp {order.total:,.0f}\n"
                        f"📋 Status: {status_map.get(order.status, order.status)}\n"
                        f"💳 Metode: {order.payment_method or '-'}\n"
                        f"🕐 {order.created_at.strftime('%d %b %Y %H:%M') if order.created_at else '-'}"
                        f"{items_text}"
                    )
                return f"❌ Order {order_id} tidak ditemukan di database."

            elif action_name == 'generate_invoice':
                order_id = params.get('order_id') or params.get('value', '')
                if not order_id:
                    return "❌ ID order diperlukan."
                order = Order.query.filter_by(order_number=order_id).first()
                if order:
                    return f"✅ Invoice untuk order {order_id} tersedia di:\n🔗 /invoice/{order.id}"
                return f"❌ Order {order_id} tidak ditemukan."

            elif action_name == 'generate_redemption':
                order_id = params.get('order_id') or params.get('value', '')
                if not order_id:
                    return "❌ ID order diperlukan."
                order = Order.query.filter_by(order_number=order_id).first()
                if order:
                    return f"✅ Surat penebusan untuk order {order_id}:\n🔗 /admin/redemption-letter/{order.id}"
                return f"❌ Order {order_id} tidak ditemukan."

            elif action_name == 'send_notification':
                target_chat = params.get('chat_id', str(chat_id))
                message = params.get('message', 'Notifikasi dari admin')
                bot_token = _get_setting('telegram_bot_token')
                if bot_token:
                    TelegramGateway.send_message(target_chat, message, bot_token)
                    return f"✅ Notifikasi terkirim ke chat {target_chat}."
                return "❌ Bot token Telegram belum dikonfigurasi. Atur di Pengaturan > Notifikasi."

            elif action_name == 'create_product':
                product_name = params.get('name') or params.get('value', '')
                if not product_name:
                    return "❌ Nama produk diperlukan."
                return f"📝 Produk '{product_name}' perlu dibuat melalui panel admin:\n🔗 /admin/product/new"

            elif action_name == 'store_summary':
                total_products = Product.query.count()
                total_orders = Order.query.count()
                pending = Order.query.filter_by(status='Pending').count()
                processing = Order.query.filter_by(status='Processing').count()
                completed = Order.query.filter_by(status='Completed').count()
                revenue = db.session.query(func.coalesce(func.sum(Order.total), 0)).filter(
                    Order.status.in_(['Paid', 'Completed', 'Shipped'])
                ).scalar()
                today = datetime.now().date()
                today_orders = Order.query.filter(
                    db.func.date(Order.created_at) == today
                ).count()

                return (
                    f"📊 <b>Ringkasan Toko Hari Ini</b>\n"
                    f"━━━━━━━━━━━━━━\n"
                    f"📦 Total Produk: {total_products}\n"
                    f"🛒 Total Order: {total_orders}\n"
                    f"📅 Order Hari Ini: {today_orders}\n"
                    f"⏳ Pending: {pending}\n"
                    f"⚙️ Diproses: {processing}\n"
                    f"✅ Selesai: {completed}\n"
                    f"💰 Total Pendapatan: Rp {revenue:,.0f}"
                )

            else:
                return f"❓ Aksi '{action_name}' belum diimplementasikan. Hubungi developer."

        except Exception as e:
            logger.error(f"Action execution error: {e}", exc_info=True)
            return f"❌ Error saat mengeksekusi aksi: {str(e)}"

    @staticmethod
    def setup_telegram_webhook(app):
        """Daftarkan endpoint webhook Telegram di Flask."""

        @app.route('/telegram/webhook/<path:token>', methods=['POST'])
        def telegram_webhook(token):
            # Validasi token bot
            expected = _get_setting('telegram_bot_token', '')
            if not expected or token != expected:
                logger.warning(f"Invalid Telegram webhook token attempt")
                return jsonify({'error': 'Invalid token'}), 403

            update = request.get_json(force=True)
            if not update or 'message' not in update:
                return jsonify({'status': 'ignored'}), 200

            message = update['message']
            text = message.get('text', '').strip()
            chat_id = message['chat']['id']
            user_name = message.get('from', {}).get('first_name', 'Admin')

            # Skip non-text messages
            if not text:
                return jsonify({'status': 'ignored'}), 200

            logger.info(f"Telegram message from {user_name} (chat {chat_id}): {text[:100]}")

            # Kirim ke OpenRouter AI untuk diproses
            ai_result = OpenRouterTelegramBridge._call_openrouter(text)

            if not ai_result['success']:
                reply = (
                    f"❌ <b>Error Koneksi AI</b>\n\n"
                    f"{ai_result['error']}\n\n"
                    f"💡 Pastikan konfigurasi OpenRouter sudah benar di halaman <b>Pengaturan > Konfigurasi AI</b>."
                )
            else:
                ai_reply = ai_result['reply']
                action_name, params = OpenRouterTelegramBridge._extract_action(ai_reply)

                if action_name:
                    # Eksekusi aksi dan gabungkan hasil
                    action_result = OpenRouterTelegramBridge._execute_action(action_name, params, chat_id)
                    # Hapus tag [AKSI:...] dari reply dan tambahkan hasil
                    clean_reply = re.sub(r'\[AKSI:[^\]]+\]\s*', '', ai_reply).strip()
                    reply = f"{clean_reply}\n\n{action_result}" if clean_reply else action_result
                else:
                    reply = ai_reply

            # Kirim balasan ke Telegram
            bot_token = _get_setting('telegram_bot_token')
            if bot_token and reply:
                try:
                    from api.telegram_gateway import TelegramGateway
                    TelegramGateway.send_message(str(chat_id), reply, bot_token)
                except Exception as e:
                    logger.error(f"Failed to send Telegram reply: {e}")

            return jsonify({'status': 'ok', 'ai_processed': ai_result['success']}), 200

        logger.info("OpenRouter Telegram Bridge registered at /telegram/webhook/<token>")

    @staticmethod
    def send_ai_notification(message: str, chat_id: str = None) -> bool:
        """
        Kirim notifikasi otomatis ke Telegram dengan bantuan AI untuk memformat pesan.
        """
        bot_token = _get_setting('telegram_bot_token')
        target = chat_id or _get_setting('telegram_chat_id')

        if not bot_token or not target:
            logger.warning("Telegram not configured for AI notification")
            return False

        # Minta AI mempercantik pesan
        ai_result = OpenRouterTelegramBridge._call_openrouter(
            f"Format pesan notifikasi berikut agar lebih profesional dan menarik untuk admin toko:\n\n{message}"
        )

        final_message = ai_result['reply'] if ai_result['success'] else message

        try:
            from api.telegram_gateway import TelegramGateway
            TelegramGateway.send_message(target, final_message, bot_token)
            logger.info(f"AI notification sent to {target}")
            return True
        except Exception as e:
            logger.error(f"Failed to send AI notification: {e}")
            return False