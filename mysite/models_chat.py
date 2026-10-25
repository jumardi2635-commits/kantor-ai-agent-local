import warnings

from models import (  # noqa: F401  (re-export, dipakai oleh caller lama)
    ChatConversation,
    ChatMessage,
    CustomerMemoryFact,
    db,
)

warnings.warn(
    "models_chat.py sudah deprecated — model ChatConversation/ChatMessage/"
    "CustomerMemoryFact sekarang didefinisikan di models.py. Modul ini "
    "hanya re-export untuk kompatibilitas mundur; import langsung dari "
    "models.py pada kode baru.",
    DeprecationWarning,
    stacklevel=3,
)

__all__ = ['ChatConversation', 'ChatMessage', 'CustomerMemoryFact', 'db']
