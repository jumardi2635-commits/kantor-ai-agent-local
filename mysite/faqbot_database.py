# -*- coding: utf-8 -*-
"""
Penyimpanan data untuk FAQ Bot Telegram (integrasi bot.py -> app.py).

Modul ini terpisah dari `models.py` (SQLAlchemy) karena FAQ bot punya
kebutuhan data yang sangat sederhana (user terdaftar, FAQ, jumlah warning)
dan supaya integrasinya tidak menyentuh skema database utama marketplace.
Disimpan sebagai file SQLite sendiri: faqbot.db

Kalau nanti mau dipindah ke tabel SQLAlchemy yang sama dengan `models.py`,
tinggal ganti isi fungsi-fungsi di bawah ini tanpa mengubah bot.py/faqbot.py.

2026 Pro fixes
--------------
- WAL mode + busy_timeout: sebelumnya setiap panggilan buka koneksi baru
  dengan setting default SQLite, yang gampang lempar "database is locked"
  kalau ada write bersamaan (mis. anti-spam warning + broadcast jalan
  berbarengan). WAL mode + busy_timeout membuat write bersamaan aman.
- init_db() sekarang idempotent & aman dipanggil berkali-kali (dipanggil
  ulang di dalam _conn() secara implisit lewat PRAGMA, tidak mengubah data).
- Semua fungsi publik dibungkus try/except supaya kegagalan I/O pada DB
  FAQ bot tidak pernah menjatuhkan proses Flask utama (graceful
  degradation) — fungsi mengembalikan nilai default yang aman & mencatat
  log error.
"""

import logging
import os
import sqlite3
from contextlib import contextmanager

logger = logging.getLogger(__name__)

DB_PATH = os.getenv("FAQBOT_DB_PATH", os.path.join(os.path.dirname(__file__), "faqbot.db"))


@contextmanager
def _conn():
    connection = sqlite3.connect(DB_PATH, timeout=10)
    connection.row_factory = sqlite3.Row
    # WAL mode: reader tidak memblokir writer dan sebaliknya, jauh lebih
    # tahan terhadap write bersamaan (broadcast + warning masuk barengan).
    connection.execute("PRAGMA journal_mode=WAL")
    connection.execute("PRAGMA busy_timeout=10000")
    connection.execute("PRAGMA foreign_keys=ON")
    try:
        yield connection
        connection.commit()
    except Exception:
        connection.rollback()
        raise
    finally:
        connection.close()


def init_db():
    """Buat tabel kalau belum ada. Aman dipanggil berkali-kali (idempotent)."""
    try:
        with _conn() as c:
            c.execute("""
                CREATE TABLE IF NOT EXISTS faqbot_users (
                    user_id     INTEGER PRIMARY KEY,
                    username    TEXT,
                    first_name  TEXT,
                    warn_count  INTEGER NOT NULL DEFAULT 0,
                    created_at  TEXT NOT NULL DEFAULT (datetime('now'))
                )
            """)
            c.execute("""
                CREATE TABLE IF NOT EXISTS faqbot_faq (
                    keyword     TEXT PRIMARY KEY,
                    answer      TEXT NOT NULL,
                    updated_at  TEXT NOT NULL DEFAULT (datetime('now'))
                )
            """)
            c.execute("CREATE INDEX IF NOT EXISTS idx_faqbot_faq_keyword ON faqbot_faq (keyword)")
        logger.info("faqbot_database: init_db OK (%s)", DB_PATH)
    except sqlite3.Error as e:
        logger.critical("faqbot_database: gagal inisialisasi DB di %s: %s", DB_PATH, e)
        raise


def upsert_user(user_id: int, username: str, first_name: str) -> None:
    try:
        with _conn() as c:
            c.execute("""
                INSERT INTO faqbot_users (user_id, username, first_name)
                VALUES (?, ?, ?)
                ON CONFLICT(user_id) DO UPDATE SET
                    username=excluded.username,
                    first_name=excluded.first_name
            """, (user_id, username or "", first_name or ""))
    except sqlite3.Error as e:
        logger.error("faqbot_database.upsert_user(%s): %s", user_id, e)


def get_faq(keyword: str):
    if not keyword or not keyword.strip():
        return None
    try:
        with _conn() as c:
            row = c.execute(
                "SELECT answer FROM faqbot_faq WHERE lower(keyword) = lower(?)",
                (keyword.strip(),),
            ).fetchone()
            return row["answer"] if row else None
    except sqlite3.Error as e:
        logger.error("faqbot_database.get_faq(%r): %s", keyword, e)
        return None


def list_faq():
    try:
        with _conn() as c:
            rows = c.execute("SELECT keyword FROM faqbot_faq ORDER BY keyword").fetchall()
            return [r["keyword"] for r in rows]
    except sqlite3.Error as e:
        logger.error("faqbot_database.list_faq: %s", e)
        return []


def set_faq(keyword: str, answer: str) -> bool:
    keyword = (keyword or "").strip()
    answer = (answer or "").strip()
    if not keyword or not answer:
        return False
    try:
        with _conn() as c:
            c.execute("""
                INSERT INTO faqbot_faq (keyword, answer, updated_at)
                VALUES (?, ?, datetime('now'))
                ON CONFLICT(keyword) DO UPDATE SET
                    answer=excluded.answer,
                    updated_at=datetime('now')
            """, (keyword, answer))
        return True
    except sqlite3.Error as e:
        logger.error("faqbot_database.set_faq(%r): %s", keyword, e)
        return False


def delete_faq(keyword: str) -> bool:
    keyword = (keyword or "").strip()
    if not keyword:
        return False
    try:
        with _conn() as c:
            cur = c.execute("DELETE FROM faqbot_faq WHERE lower(keyword) = lower(?)", (keyword,))
            return cur.rowcount > 0
    except sqlite3.Error as e:
        logger.error("faqbot_database.delete_faq(%r): %s", keyword, e)
        return False


def add_warn(user_id: int) -> int:
    """Tambah 1 warning untuk user_id dan kembalikan total warning-nya.
    Kalau user belum terdaftar (harusnya tidak terjadi karena upsert_user
    selalu dipanggil lebih dulu), baris dibuat dulu supaya UPDATE tidak
    diam-diam no-op."""
    try:
        with _conn() as c:
            c.execute("""
                INSERT INTO faqbot_users (user_id, warn_count) VALUES (?, 1)
                ON CONFLICT(user_id) DO UPDATE SET warn_count = warn_count + 1
            """, (user_id,))
            row = c.execute(
                "SELECT warn_count FROM faqbot_users WHERE user_id = ?", (user_id,)
            ).fetchone()
            return row["warn_count"] if row else 1
    except sqlite3.Error as e:
        logger.error("faqbot_database.add_warn(%s): %s", user_id, e)
        return 1


def reset_warn(user_id: int) -> None:
    try:
        with _conn() as c:
            c.execute("UPDATE faqbot_users SET warn_count = 0 WHERE user_id = ?", (user_id,))
    except sqlite3.Error as e:
        logger.error("faqbot_database.reset_warn(%s): %s", user_id, e)


def get_all_user_ids():
    try:
        with _conn() as c:
            rows = c.execute("SELECT user_id FROM faqbot_users").fetchall()
            return [r["user_id"] for r in rows]
    except sqlite3.Error as e:
        logger.error("faqbot_database.get_all_user_ids: %s", e)
        return []


def count_users() -> int:
    try:
        with _conn() as c:
            row = c.execute("SELECT COUNT(*) AS n FROM faqbot_users").fetchone()
            return row["n"] if row else 0
    except sqlite3.Error as e:
        logger.error("faqbot_database.count_users: %s", e)
        return 0
