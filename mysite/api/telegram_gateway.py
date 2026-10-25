"""
Telegram Gateway & OpenRouter Gateway Helper Module
===================================================
Modul jembatan komunikasi Telegram API & OpenRouter AI.
Mendukung kirim pesan, foto, dokumen, manajemen webhook, dan tes koneksi.
"""

import os
import json
import logging
import requests
from typing import Optional, Union, Tuple, Dict, Any

logger = logging.getLogger(__name__)


def _get_setting(key: str, default: str = '') -> str:
    """Ambil setting dari DB Setting jika tersedia, fallback ke Environment Variable."""
    try:
        from models import Setting
        s = Setting.query.filter_by(key=key).first()
        if s and s.value:
            return s.value
    except Exception:
        pass
    return os.getenv(key.upper(), default)


class TelegramResponse(tuple):
    """
    Subclass tuple (success: bool, text: str) dengan kompatibilitas boolean.
    
    Penggunaan:
      - `success, msg = TelegramGateway.send_message(...)`  (unpacking tuple)
      - `if TelegramGateway.send_message(...):`             (boolean check)
      - `res.success`                                       (property)
      - `res.text`                                          (property)
    """
    def __new__(cls, success: bool, text: str):
        return super().__new__(cls, (bool(success), str(text or '')))

    @property
    def success(self) -> bool:
        return self[0]

    @property
    def text(self) -> str:
        return self[1]

    def __bool__(self) -> bool:
        return self[0]

    def __repr__(self) -> str:
        return f"TelegramResponse(success={self[0]}, text={self[1]!r})"


class TelegramGateway:
    """Gateway terpusat untuk komunikasi dengan Telegram Bot API."""

    @classmethod
    def send_message(
        cls,
        chat_id: Optional[Union[int, str]] = None,
        text: str = "",
        bot_token: Optional[str] = None,
        parse_mode: str = 'HTML',
        disable_web_page_preview: bool = True,
        reply_markup: Optional[Dict[str, Any]] = None
    ) -> TelegramResponse:
        """Kirim pesan teks ke Telegram chat_id."""
        token = (bot_token or _get_setting('telegram_bot_token')).strip()
        target = chat_id or _get_setting('telegram_chat_id')

        if not token or not target:
            logger.warning("[TelegramGateway] Missing bot_token or chat_id")
            return TelegramResponse(False, "Missing credentials (bot_token or chat_id)")

        if not text:
            return TelegramResponse(False, "Message text cannot be empty")

        url = f"https://api.telegram.org/bot{token}/sendMessage"
        payload = {
            'chat_id': target,
            'text': text,
            'parse_mode': parse_mode,
            'disable_web_page_preview': disable_web_page_preview
        }
        if reply_markup:
            payload['reply_markup'] = reply_markup

        try:
            r = requests.post(url, json=payload, timeout=10)
            if r.status_code == 200:
                return TelegramResponse(True, r.text)
            
            error_msg = f"HTTP {r.status_code}: {r.text[:200]}"
            if "Access Denied" in r.text and "PythonAnywhere" in r.text:
                error_msg = "PythonAnywhere outbound proxy restricted api.telegram.org"
            
            logger.warning(f"[TelegramGateway] Failed to send message: {error_msg}")
            return TelegramResponse(False, error_msg)

        except requests.exceptions.RequestException as e:
            logger.error(f"[TelegramGateway] Network error: {e}")
            return TelegramResponse(False, f"Network error: {e}")

    @classmethod
    def send_photo(
        cls,
        chat_id: Optional[Union[int, str]] = None,
        photo_url: str = "",
        caption: Optional[str] = None,
        bot_token: Optional[str] = None,
        parse_mode: str = 'HTML'
    ) -> TelegramResponse:
        """Kirim foto URL ke Telegram chat_id."""
        token = (bot_token or _get_setting('telegram_bot_token')).strip()
        target = chat_id or _get_setting('telegram_chat_id')

        if not token or not target:
            return TelegramResponse(False, "Missing credentials (bot_token or chat_id)")

        if not photo_url:
            return TelegramResponse(False, "photo_url is required")

        url = f"https://api.telegram.org/bot{token}/sendPhoto"
        payload = {'chat_id': target, 'photo': photo_url}
        if caption:
            payload['caption'] = caption
            payload['parse_mode'] = parse_mode

        try:
            r = requests.post(url, json=payload, timeout=15)
            if r.status_code == 200:
                return TelegramResponse(True, r.text)
            return TelegramResponse(False, f"HTTP {r.status_code}: {r.text[:200]}")
        except Exception as e:
            logger.error(f"[TelegramGateway] Failed to send photo: {e}")
            return TelegramResponse(False, f"Network error: {e}")

    @classmethod
    def send_document(
        cls,
        chat_id: Optional[Union[int, str]] = None,
        document_url: str = "",
        caption: Optional[str] = None,
        bot_token: Optional[str] = None
    ) -> TelegramResponse:
        """Kirim dokumen URL ke Telegram chat_id."""
        token = (bot_token or _get_setting('telegram_bot_token')).strip()
        target = chat_id or _get_setting('telegram_chat_id')

        if not token or not target:
            return TelegramResponse(False, "Missing credentials")

        url = f"https://api.telegram.org/bot{token}/sendDocument"
        payload = {'chat_id': target, 'document': document_url}
        if caption:
            payload['caption'] = caption
            payload['parse_mode'] = 'HTML'

        try:
            r = requests.post(url, json=payload, timeout=15)
            if r.status_code == 200:
                return TelegramResponse(True, r.text)
            return TelegramResponse(False, f"HTTP {r.status_code}: {r.text[:200]}")
        except Exception as e:
            return TelegramResponse(False, str(e))

    @classmethod
    def get_me(cls, bot_token: Optional[str] = None) -> TelegramResponse:
        """Cek status bot Telegram (/getMe)."""
        token = (bot_token or _get_setting('telegram_bot_token')).strip()
        if not token:
            return TelegramResponse(False, "telegram_bot_token is missing")

        url = f"https://api.telegram.org/bot{token}/getMe"
        try:
            r = requests.get(url, timeout=10)
            if r.status_code == 200:
                return TelegramResponse(True, r.text)
            return TelegramResponse(False, f"HTTP {r.status_code}: {r.text[:200]}")
        except Exception as e:
            return TelegramResponse(False, str(e))

    @classmethod
    def set_webhook(cls, webhook_url: str, secret_token: Optional[str] = None, bot_token: Optional[str] = None) -> TelegramResponse:
        """Daftarkan webhook URL ke Telegram API."""
        token = (bot_token or _get_setting('telegram_bot_token')).strip()
        if not token:
            return TelegramResponse(False, "telegram_bot_token is missing")

        url = f"https://api.telegram.org/bot{token}/setWebhook"
        payload = {'url': webhook_url}
        if secret_token:
            payload['secret_token'] = secret_token

        try:
            r = requests.post(url, json=payload, timeout=10)
            if r.status_code == 200:
                return TelegramResponse(True, r.text)
            return TelegramResponse(False, f"HTTP {r.status_code}: {r.text[:200]}")
        except Exception as e:
            return TelegramResponse(False, str(e))

    @classmethod
    def delete_webhook(cls, bot_token: Optional[str] = None) -> TelegramResponse:
        """Hapus webhook dari Telegram API."""
        token = (bot_token or _get_setting('telegram_bot_token')).strip()
        if not token:
            return TelegramResponse(False, "telegram_bot_token is missing")

        url = f"https://api.telegram.org/bot{token}/deleteWebhook"
        try:
            r = requests.post(url, timeout=10)
            if r.status_code == 200:
                return TelegramResponse(True, r.text)
            return TelegramResponse(False, f"HTTP {r.status_code}: {r.text[:200]}")
        except Exception as e:
            return TelegramResponse(False, str(e))

    @classmethod
    def answer_callback_query(cls, callback_query_id: str, text: Optional[str] = None, bot_token: Optional[str] = None) -> TelegramResponse:
        """Kirim jawaban callback query (popup/notification)."""
        token = (bot_token or _get_setting('telegram_bot_token')).strip()
        if not token or not callback_query_id:
            return TelegramResponse(False, "Missing token or callback_query_id")

        url = f"https://api.telegram.org/bot{token}/answerCallbackQuery"
        payload = {'callback_query_id': callback_query_id}
        if text:
            payload['text'] = text[:200]

        try:
            r = requests.post(url, json=payload, timeout=10)
            if r.status_code == 200:
                return TelegramResponse(True, r.text)
            return TelegramResponse(False, f"HTTP {r.status_code}: {r.text[:200]}")
        except Exception as e:
            return TelegramResponse(False, str(e))


class OpenRouterGateway:
    """Gateway helper untuk OpenRouter AI completions."""

    @classmethod
    def send_message(cls, user_message: str, model: Optional[str] = None, system_prompt: Optional[str] = None) -> str:
        api_key = _get_setting('openrouter_api_key')
        if not api_key:
            return "API Key OpenRouter belum dikonfigurasi."

        target_model = model or _get_setting('openrouter_model', 'openrouter/free')
        base_url = _get_setting('openrouter_base_url', 'https://openrouter.ai/api/v1').rstrip('/')

        messages = []
        if system_prompt:
            messages.append({'role': 'system', 'content': system_prompt})
        messages.append({'role': 'user', 'content': user_message})

        headers = {
            'Authorization': f'Bearer {api_key}',
            'Content-Type': 'application/json',
            'HTTP-Referer': _get_setting('openrouter_site_url', 'https://slotriset.pythonanywhere.com'),
            'X-Title': _get_setting('openrouter_app_name', 'Pusat Gadai Indonesia')
        }

        try:
            r = requests.post(
                f"{base_url}/chat/completions",
                json={'model': target_model, 'messages': messages, 'max_tokens': 500},
                headers=headers,
                timeout=20
            )
            r.raise_for_status()
            data = r.json()
            choices = data.get('choices', [])
            if choices:
                return choices[0].get('message', {}).get('content', '').strip()
            return "Tidak ada respon dari AI."
        except Exception as e:
            logger.error(f"[OpenRouterGateway] Error: {e}")
            return f"Error OpenRouter: {e}"


def send_telegram_notification(message: str, priority: str = 'normal', chat_id: Optional[str] = None, bot_token: Optional[str] = None) -> TelegramResponse:
    """Fungsi pembantu untuk mengirim notifikasi Telegram dengan penanda prioritas."""
    priority_emoji = {'high': '🔴', 'normal': '🔵', 'low': '⚪'}.get(priority, '🔵')
    formatted = f"{priority_emoji} <b>Notifikasi Sistem</b>\n\n{message}"
    return TelegramGateway.send_message(chat_id=chat_id, text=formatted, bot_token=bot_token)