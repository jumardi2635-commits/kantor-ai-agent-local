import requests
import json
import logging
from flask import request, jsonify, Response

logger = logging.getLogger(__name__)

# =============================================================================
# TELEGRAM WEBHOOK CONFIGURATION
# =============================================================================

TELEGRAM_BOT_TOKEN = os.getenv("TELEGRAM_BOT_TOKEN", "")
TELEGRAM_WEBHOOK_SECRET = "pusatgadai_secret_2026"


# =============================================================================
# MAIN WEBHOOK HANDLER
# =============================================================================

def telegram_webhook():
    """
    Handle incoming Telegram webhook for Flask
    
    Endpoint: /webhook/telegram/
    Method: POST
    Headers: X-Telegram-Bot-Api-Secret-Token: pusatgadai_secret_2026
    """
    if request.method != 'POST':
        return Response('Method not allowed', status=405)
    
    # Verifikasi secret token dari Telegram
    secret_token = request.headers.get('X-Telegram-Bot-Api-Secret-Token')
    if secret_token != TELEGRAM_WEBHOOK_SECRET:
        return Response('Unauthorized', status=401)
    
    try:
        update = request.get_json(force=True, silent=True) or {}
        
        # Log untuk debugging
        logger.info("=== TELEGRAM UPDATE ===")
        logger.info(json.dumps(update, indent=2))
        
        # Handle message
        if 'message' in update:
            message = update['message']
            chat_id = message['chat']['id']
            text = message.get('text', '')
            sender = message.get('from', {})
            sender_name = f"{sender.get('first_name', '')} {sender.get('last_name', '')}".strip()
            username = sender.get('username', '')
            
            # Proses pesan dan dapatkan response
            response_text = process_telegram_message(text, sender_name, username, chat_id)
            
            # Kirim balasan ke Telegram
            send_telegram_message(chat_id, response_text)
        
        # Handle callback query (button clicks)
        elif 'callback_query' in update:
            callback_query = update['callback_query']
            chat_id = callback_query['message']['chat']['id']
            data = callback_query['data']
            
            response_text = process_callback_query(data, chat_id)
            send_telegram_message(chat_id, response_text)
        
        return jsonify({'status': 'ok'})
    
    except Exception as e:
        logger.error(f"ERROR: {e}")
        return jsonify({'status': 'error', 'message': str(e)}), 500


# =============================================================================
# MESSAGE PROCESSOR
# =============================================================================

def process_telegram_message(text, sender_name, username, chat_id):
    """
    Process incoming Telegram message and generate response
    
    Args:
        text: Message text from user
        sender_name: User's full name
        username: User's Telegram username
        chat_id: Chat ID for sending response
    
    Returns:
        str: Response message
    """
    text_lower = text.lower().strip()
    
    # === SALASAN / GREETING ===
    if text_lower in ['halo', 'hai', 'hi', 'hey', 'assalamualaikum', 'hello', 'pagi', 'siang', 'sore', 'malam']:
        return (
            f"👋 *Halo {sender_name}!* Selamat datang di Pusat Gadai Indonesia!\n\n"
            f"Apa yang bisa kami bantu hari ini?\n\n"
            f"📋 *Menu:*\n"
            f"• Info produk\n"
            f"• Cara pembelian\n"
            f"• Cek status order\n"
            f"• Hubungi admin\n"
            f"• Info garansi\n"
            f"• Info pengiriman"
        )
    
    # === INFO PRODUK ===
    elif 'info' in text_lower and 'produk' in text_lower:
        return (
            "📱 *PUSAT GADAI INDONESIA*\n\n"
            f"Kami menyediakan gadget second berkualitas dengan harga terjangkau!\n\n"
            f"✅ *Keunggulan Kami:*\n"
            f"• Garansi 1 tahun\n"
            f"• Unit sudah diverifikasi\n"
            f"• Fullset & original\n"
            f"• Harga kompetitif\n\n"
            f"🌐 *Katalog:* https://pusatgadaiidn.pythonanywhere.com\n\n"
            f"💬 Chat admin untuk info lebih lanjut!"
        )
    
    # === CARA PEMBELIAN ===
    elif 'cara' in text_lower or 'beli' in text_lower or 'pembelian' in text_lower:
        return (
            "🛒 *CARA PEMBELIAN*\n\n"
            f"1️⃣ Pilih produk di katalog\n"
            f"2️⃣ Chat admin untuk konfirmasi stok\n"
            f"3️⃣ Transfer pembayaran\n"
            f"4️⃣ Kami proses & kirim produk\n"
            f"5️⃣ Barang sampai dengan aman!\n\n"
            f"📱 *Admin:* +62 895-3005-2136\n"
            f"⏰ *Jam operasional:* 10:00 - 20:00 WIB"
        )
    
    # === STATUS ORDER ===
    elif 'status' in text_lower or 'order' in text_lower or 'kirim' in text_lower or 'resi' in text_lower:
        return (
            "📦 *CEK STATUS ORDER*\n\n"
            f"Silakan kirim nomor order Anda untuk cek status.\n\n"
            f"📝 *Format:* Order #123\n\n"
            f"Contoh:\n"
            f"`Order #PGD2026001`\n\n"
            f"👤 Atau langsung chat admin:\n"
            f"📱 +62 895-3005-2136"
        )
    
    # === HUBUNGI ADMIN ===
    elif 'admin' in text_lower or 'hubungi' in text_lower or 'contact' in text_lower or 'cs' in text_lower:
        return (
            "👤 *HUBUNGI ADMIN*\n\n"
            f"📱 *WhatsApp:* +62 895-3005-2136\n"
            f"📧 *Email:* admin@pusatgadai.id\n"
            f"🌐 *Website:* pusatgadaiidn.pythonanywhere.com\n\n"
            f"⏰ *Jam Operasional:*\n"
            f"Senin - Minggu\n"
            f"10:00 - 20:00 WIB\n\n"
            f"💬 Respon cepat dalam 1-15 menit!"
        )
    
    # === GARANSI ===
    elif 'garansi' in text_lower or 'warranty' in text_lower:
        return (
            "🛡️ *INFORMASI GARANSI*\n\n"
            f"✅ *Semua produk bergaransi 1 tahun!*\n\n"
            f"📋 *Cakupan Garansi:*\n"
            f"• Kerusakan fungsi (hardware)\n"
            f"• Tidak sesuai deskripsi\n"
            f"• IMEI terblokir\n\n"
            f"❌ *Tidak Mencakup:*\n"
            f"• Kerusakan fisik (jatuh, air)\n"
            f"• Software error karena user\n"
            f"• Kehilangan barang\n\n"
            f"📞 Klaim garansi hubungi admin!"
        )
    
    # === PENGIRIMAN ===
    elif 'kirim' in text_lower or 'pengiriman' in text_lower or 'ongkir' in text_lower or 'ongkos' in text_lower:
        return (
            "🚚 *INFORMASI PENGIRIMAN*\n\n"
            f"📍 *Area Pengiriman:* Seluruh Indonesia\n\n"
            f"📦 *Ekspedisi:*\n"
            f"• JNE (REG, YES, OKE)\n"
            f"• J&T Express\n"
            f"• SiCepat\n"
            f"• AnterAja\n\n"
            f"💰 *Ongkir:* Ditanggung pembeli\n"
            f"🎁 *Gratis ongkir* untuk area tertentu!\n\n"
            f"⏱️ *Estimasi:* 1-5 hari kerja\n\n"
            f"💬 Chat admin untuk cek ongkir!"
        )
    
    # === HARGA ===
    elif 'harga' in text_lower or 'mahal' in text_lower or 'murah' in text_lower or 'diskon' in text_lower:
        return (
            "💰 *INFORMASI HARGA*\n\n"
            f"📱 Harga kami sangat kompetitif!\n"
            f"💯 Semua unit second berkualitas\n\n"
            f"🎁 *Promo:*\n"
            f"• Diskon untuk pembelian kedua\n"
            f"• Cashback member\n"
            f"• Cicilan 0% tersedia\n\n"
            f"🌐 Cek katalog lengkap:\n"
            f"https://pusatgadaiidn.pythonanywhere.com"
        )
    
    # === LOKASI / ALAMAT ===
    elif 'lokasi' in text_lower or 'alamat' in text_lower or 'toko' in text_lower or 'store' in text_lower:
        return (
            "📍 *LOKASI KAMI*\n\n"
            f"PUSAT GADAI INDONESIA\n\n"
            f"🏪 *Store:* Online (tidak ada toko fisik)\n"
            f"📦 *Gudang:* Jakarta, Indonesia\n\n"
            f"🌐 *Website:*\n"
            f"https://pusatgadaiidn.pythonanywhere.com\n\n"
            f"💬 Semua transaksi bisa online!"
        )
    
    # === TERIMA KASIH ===
    elif 'terima' in text_lower or 'makasih' in text_lower or 'thanks' in text_lower or 'thank' in text_lower:
        return (
            f"🙏 *Sama-sama, {sender_name}!*\n\n"
            f"Terima kasih telah menghubungi Pusat Gadai Indonesia.\n\n"
            f"Ada yang bisa kami bantu lagi? 😊"
        )
    
    # === BYE ===
    elif 'bye' in text_lower or 'dadah' in text_lower or 'sampai' in text_lower:
        return (
            f"👋 *Sampai jumpa, {sender_name}!*\n\n"
            f"Terima kasih sudah menghubungi kami.\n"
            f"Jangan ragu untuk chat lagi ya! 😊\n\n"
            f"*Pusat Gadai Indonesia*\n"
            f"📱 +62 895-3005-2136"
        )
    
    # === DEFAULT RESPONSE ===
    else:
        return (
            f"👍 Terima kasih atas pesan Anda, *{sender_name}*!\n\n"
            f"Tim kami akan segera merespons.\n\n"
            f"⏱️ *Respon:* 1-15 menit (jam operasional)\n\n"
            f"📱 *Butuh bantuan cepat?*\n"
            f"WhatsApp: +62 895-3005-2136\n\n"
            f"🌐 *Katalog:* https://pusatgadaiidn.pythonanywhere.com"
        )


# =============================================================================
# CALLBACK QUERY HANDLER (BUTTON CLICKS)
# =============================================================================

def process_callback_query(data, chat_id):
    """
    Handle callback query from inline button clicks
    
    Args:
        data: Callback data from button
        chat_id: Chat ID for response
    
    Returns:
        str: Response message
    """
    if data == 'products':
        return "📱 *KATALOG PRODUK*\n\nKunjungi website kami untuk melihat semua produk:\n\nhttps://pusatgadaiidn.pythonanywhere.com/catalog"
    
    elif data == 'contact':
        return "👤 *HUBUNGI KAMI*\n\n📱 WhatsApp: +62 895-3005-2136\n🌐 Website: pusatgadaiidn.pythonanywhere.com"
    
    elif data == 'garansi':
        return "🛡️ *GARANSI*\n\n✅ Garansi 1 tahun\n✅ Fullset & original\n✅ Verifikasi ketat"
    
    elif data == 'promo':
        return "🎁 *PROMO TERBARU*\n\n• Diskon 5% pembelian kedua\n• Cashback member 10%\n• Cicilan 0% tersedia\n\n💬 Chat admin untuk detail!"
    
    else:
        return "Terima kasih telah menghubungi Pusat Gadai! 🙏\n\nAda yang bisa dibantu?"


# =============================================================================
# TELEGRAM API HELPER
# =============================================================================

def send_telegram_message(chat_id, text, parse_mode='Markdown'):
    """
    Send message to Telegram chat
    
    Args:
        chat_id: Telegram chat ID
        text: Message text
        parse_mode: 'Markdown' or 'HTML'
    """
    url = f"https://api.telegram.org/bot{TELEGRAM_BOT_TOKEN}/sendMessage"
    
    data = {
        'chat_id': chat_id,
        'text': text,
        'parse_mode': parse_mode
    }
    
    try:
        response = requests.post(url, json=data, timeout=10)
        response.raise_for_status()
        print(f"✓ Message sent to {chat_id}")
    except requests.exceptions.RequestException as e:
        print(f"✗ Failed to send message: {e}")


def send_telegram_photo(chat_id, photo_url, caption=None):
    """
    Send photo to Telegram chat
    
    Args:
        chat_id: Telegram chat ID
        photo_url: URL or file_id of photo
        caption: Optional caption
    """
    url = f"https://api.telegram.org/bot{TELEGRAM_BOT_TOKEN}/sendPhoto"
    
    data = {
        'chat_id': chat_id,
        'photo': photo_url
    }
    if caption:
        data['caption'] = caption
    try:
        response = requests.post(url, json=data, timeout=10)
        response.raise_for_status()
        logger.info(f"✓ Photo sent to {chat_id}")
    except requests.exceptions.RequestException as e:
        logger.error(f"✗ Failed to send photo: {e}")