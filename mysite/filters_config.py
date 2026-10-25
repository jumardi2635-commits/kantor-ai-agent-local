# -*- coding: utf-8 -*-
"""
Konfigurasi filter anti-spam untuk FAQ Bot Telegram.
Sesuaikan daftar kata terlarang & batasan sesuai kebutuhan grup/channel kamu.
"""

# Kata-kata yang otomatis dianggap pelanggaran bila muncul di pesan (case-insensitive)
BANNED_WORDS = [
    "judi",
    "slot gacor",
    "pinjol ilegal",
]

# Maksimal jumlah link (http/https/t.me/www.) yang boleh ada dalam satu pesan
MAX_LINKS_PER_MESSAGE = 2

# Jumlah peringatan sebelum user otomatis di-mute di grup
MAX_WARN_BEFORE_MUTE = 3
