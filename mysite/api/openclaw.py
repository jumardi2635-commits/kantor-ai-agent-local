"""
Open Router Python Client Library

Modul untuk berinteraksi dengan Open Router Gateway.
Menyediakan fungsi untuk mengirim notifikasi, health check, dan manajemen konfigurasi.

Usage:
    from api.openrouter import OpenRouterClient

    client = OpenRouterClient(
        base_url="https://openrouter.ai/api/v1",
        api_key="sk-or-v1-..."
    )

    success = client.notify("new_order", {"order_id": "123"})
    health = client.health_check()
"""

import requests
import logging
from typing import Optional, Dict, Any, Tuple
from datetime import datetime

logger = logging.getLogger(__name__)


class OpenClawError(Exception):
    """Exception dasar untuk error Open Router"""
    pass

# Compatibility alias
OpenRouterError = OpenClawError


class OpenRouterConnectionError(OpenClawError):
    """Error ketika tidak dapat terhubung ke Open Router Gateway."""
    pass


class OpenRouterAuthenticationError(OpenClawError):
    """Error ketika API key tidak valid."""
    pass


class OpenClawClient:
    """Client untuk berinteraksi dengan Open Router Gateway."""

    def __init__(self, base_url: str, api_key: Optional[str] = None, timeout: int = 10):
        """
        Inisialisasi Open Router client.

        Args:
            base_url: URL Open Router Gateway (contoh: https://openrouter.ai/api/v1)
            api_key: API key untuk autentikasi (opsional)
            timeout: Timeout request dalam detik
        """
        self.base_url = base_url.rstrip('/')
        self.api_key = api_key
        self.timeout = timeout
        self._session = requests.Session()

    def _request(self, method: str, endpoint: str, **kwargs) -> Tuple[bool, Dict[str, Any]]:
        """
        Internal method untuk melakukan HTTP request.

        Returns:
            Tuple (success, response_data)
        """
        url = f"{self.base_url}/{endpoint.lstrip('/')}"
        headers = kwargs.pop('headers', {})

        if self.api_key:
            headers['Authorization'] = f'Bearer {self.api_key}'
        headers['Content-Type'] = 'application/json'

        try:
            response = self._session.request(
                method=method,
                url=url,
                headers=headers,
                timeout=self.timeout,
                **kwargs
            )
            response.raise_for_status()
            return True, response.json() if response.content else {}
        except requests.exceptions.ConnectionError as e:
            logger.error(f"Open Router connection error: {e}")
            raise OpenRouterConnectionError(f"Gagal terhubung ke Open Router Gateway di {self.base_url}") from e
        except requests.exceptions.Timeout as e:
            logger.error(f"Open Router timeout: {e}")
            raise OpenRouterConnectionError(f"Timeout setelah {self.timeout} detik") from e
        except requests.exceptions.HTTPError as e:
            if response.status_code == 401:
                raise OpenRouterAuthenticationError("API key tidak valid") from e
            logger.error(f"Open Router HTTP error {response.status_code}: {response.text}")
            return False, response.json() if response.content else {'error': response.text}
        except Exception as e:
            logger.error(f"Open Router unexpected error: {e}")
            return False, {'error': str(e)}

    def health_check(self) -> bool:
        """
        Periksa apakah Open Router Gateway dapat dijangkau.

        Returns:
            True jika gateway merespons dengan baik.
        """
        try:
            success, _ = self._request('GET', '/api/health')
            return success
        except OpenRouterError:
            return False

    def notify(self, event: str, payload: Dict[str, Any]) -> bool:
        """
        Kirim notifikasi/event ke Open Router.

        Args:
            event: Nama event (contoh: 'new_order', 'order_status_update')
            payload: Data yang akan dikirim

        Returns:
            True jika berhasil dikirim
        """
        data = {
            'event': event,
            'timestamp': datetime.utcnow().isoformat(),
            'data': payload
        }
        success, response = self._request('POST', '/api/webhooks/incoming', json=data)
        if success:
            logger.info(f"Open Router event '{event}' sent successfully")
        else:
            logger.warning(f"Open Router event '{event}' failed: {response.get('error', 'Unknown error')}")
        return success

    def send_command(self, command: str, params: Optional[Dict[str, Any]] = None) -> Tuple[bool, Dict[str, Any]]:
        """
        Kirim perintah ke Open Router untuk dieksekusi.

        Args:
            command: Nama perintah (contoh: 'sync_products', 'refresh_cache')
            params: Parameter tambahan

        Returns:
            Tuple (success, response_data)
        """
        data = {
            'command': command,
            'params': params or {}
        }
        return self._request('POST', '/api/commands', json=data)

    def ping(self) -> bool:
        """
        Kirim ping ke endpoint webhook untuk tes koneksi.

        Returns:
            True jika sukses
        """
        success, response = self._request('POST', '/openrouter/webhook', json={'event': 'ping'})
        return success and response.get('status') == 'pong'


OpenRouterClient = OpenClawClient

# ==================== GLOBAL CLIENT INSTANCE ====================
# Client dapat diinisialisasi ulang dengan konfigurasi dari aplikasi utama

_default_client: Optional[OpenRouterClient] = None


def init_openRouter_client(base_url: str, api_key: Optional[str] = None, timeout: int = 10) -> OpenRouterClient:
    """
    Inisialisasi global Open Router client.
    Gunakan fungsi ini di awal aplikasi untuk mengatur client.
    """
    global _default_client
    _default_client = OpenRouterClient(base_url, api_key, timeout)
    return _default_client


def get_openrouter_client() -> Optional[OpenRouterClient]:
    """Mendapatkan instance global Open Router client."""
    return _default_client


# ==================== CONVENIENCE FUNCTIONS ====================
# Fungsi-fungsi pembantu yang menggunakan client global (jika sudah diinisialisasi)

def notify(event: str, payload: Dict[str, Any]) -> bool:
    """
    Kirim notifikasi menggunakan client global.
    Pastikan client sudah diinisialisasi dengan init_openclaw_client().
    """
    client = get_openrouter_client()
    if client is None:
        logger.error("Open Router client belum diinisialisasi")
        return False
    return client.notify(event, payload)


def health_check() -> bool:
    """Cek koneksi ke Open Router Gateway via client global."""
    client = get_openrouter_client()
    if client is None:
        return False
    return client.health_check()