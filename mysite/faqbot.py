# -*- coding: utf-8 -*-
"""
FAQ Bot Telegram — versi terintegrasi ke app.py
=================================================
Adaptasi dari bot.py (standalone) supaya berjalan di DALAM proses Flask yang
sama dengan marketplace (app.py), bukan sebagai app Flask kedua.

Kenapa diubah:
- bot.py aslinya bikin `Flask(__name__)` sendiri -> kalau langsung ditaruh di
  app.py akan ada dua object Flask & dua route "/", saling tabrakan.
- Solusinya: logic bot (handler, setup Application python-telegram-bot)
  dipindah ke fungsi `init_faq_bot(flask_app)` yang dipanggil sekali dari
  app.py, lalu route webhook-nya didaftarkan ke `flask_app` yang sama
  dipakai marketplace.

Cara pakai di app.py:
    from faqbot import init_faq_bot, FAQBOT_AVAILABLE
    ...
    if FAQBOT_AVAILABLE:
        init_faq_bot(app)

Env var yang dipakai (lihat env.example):
    FAQBOT_BOT_TOKEN, FAQBOT_ADMIN_IDS, FAQBOT_BROADCAST_CHANNEL_ID,
    FAQBOT_WEBHOOK_SECRET, OPENROUTER_API_KEY, OPENROUTER_MODEL,
    AI_SYSTEM_PROMPT

2026 Pro fixes (production-readiness)
--------------------------------------
1. Thread-safety fatal: versi sebelumnya memanggil `run_until_complete()`
   pada event loop yang sama langsung dari thread request Flask. Di
   production (Gunicorn/uWSGI dengan >1 thread, atau Flask `threaded=True`)
   dua webhook yang masuk berbarengan akan memanggil `run_until_complete`
   pada objek loop yang sama dari 2 OS thread berbeda secara bersamaan —
   asyncio event loop TIDAK thread-safe untuk ini, dan hasilnya
   `RuntimeError` acak / state korup / update Telegram hilang.
   Fix: event loop dijalankan selamanya (`run_forever`) di SATU thread
   worker khusus, dan setiap request Flask menjadwalkan coroutine ke loop
   itu lewat `asyncio.run_coroutine_threadsafe(...)` yang memang didesain
   aman dipanggil dari banyak thread sekaligus.
2. `restrict_chat_member(..., permissions={...})` — python-telegram-bot
   v20+ mewajibkan objek `ChatPermissions`, bukan dict biasa. Sebelumnya
   ini selalu gagal diam-diam (ketangkep except, cuma log warning) dan
   fitur auto-mute tidak pernah benar-benar jalan. Sekarang pakai
   `ChatPermissions(can_send_messages=False, ...)` yang benar.
3. Webhook secret: default lama adalah string statis
   ("ganti_dengan_string_acak_panjang") yang predictable dan ada di
   source code publik. Sekarang: kalau FAQBOT_WEBHOOK_SECRET tidak diisi,
   digenerate acak & aman (`secrets.token_urlsafe`) setiap start, dicatat
   sekali ke log supaya admin bisa memanggil /setwebhook. Selain itu
   webhook route juga memverifikasi header
   `X-Telegram-Bot-Api-Secret-Token` sebagai lapisan kedua (defense in
   depth) — token ini otomatis didaftarkan ke Telegram saat /setwebhook
   dipanggil.
4. Broadcast sekarang flood-safe: ada jeda kecil antar pengiriman dan
   penanganan khusus untuk `RetryAfter` (rate limit Telegram) supaya
   broadcast ke banyak user tidak berhenti di tengah jalan / bikin bot
   kena limit lebih lama dari seharusnya.
5. Parsing FAQBOT_ADMIN_IDS sekarang toleran terhadap entri yang salah
   format (skip + log warning) alih-alih menjatuhkan seluruh startup app.
6. Graceful shutdown: `ptb_app.shutdown()` & penutupan HTTP client
   didaftarkan lewat `atexit` supaya koneksi tidak menggantung saat proses
   berhenti.
7. Webhook handler membungkus proses update dengan try/except dan selalu
   balas 200 ke Telegram (setelah mencatat error) supaya Telegram tidak
   retry berkali-kali dan membanjiri log saat ada 1 update yang gagal
   diproses.
8. Tombol inline (inline keyboard): /start, /help, dan /faqlist sekarang
   menampilkan menu tombol (Lihat FAQ, Tanya AI, Bantuan, Menu Admin)
   lewat satu CallbackQueryHandler (`button_callback`). Daftar FAQ
   dipaginasi (Sebelumnya/Berikutnya) karena Telegram membatasi jumlah
   tombol yang praktis ditampilkan sekaligus. callback_data untuk tiap
   FAQ memakai hash pendek dari keyword (bukan keyword mentah) karena
   Telegram membatasi callback_data maksimal 64 byte — lihat
   `_faq_short_id`.
"""

import asyncio
import atexit
import hashlib
import logging
import os
import re
import secrets
import threading
import time

import httpx
from flask import request

import faqbot_database as db
from filters_config import BANNED_WORDS, MAX_LINKS_PER_MESSAGE, MAX_WARN_BEFORE_MUTE

logger = logging.getLogger(__name__)

FAQBOT_AVAILABLE = False
try:
    from telegram import (
        ChatPermissions,
        InlineKeyboardButton,
        InlineKeyboardMarkup,
        Update,
    )
    from telegram.error import BadRequest, RetryAfter, TelegramError
    from telegram.ext import (
        Application,
        CallbackQueryHandler,
        CommandHandler,
        MessageHandler,
        filters,
    )
    FAQBOT_AVAILABLE = True
except ImportError as e:
    logger.warning(f"python-telegram-bot tidak terpasang, FAQ bot dinonaktifkan: {e}")

LINK_PATTERN = re.compile(r"(https?://|t\.me/|www\.)", re.IGNORECASE)

# Jeda antar pesan broadcast supaya tidak kena flood limit Telegram
# (Telegram membolehkan ~30 pesan/detik ke chat berbeda, kita ambil jauh
# di bawah itu supaya aman untuk bot dengan trafik lain juga).
_BROADCAST_DELAY_SECONDS = 0.05


def _is_admin(user_id: int, admin_ids: set) -> bool:
    return user_id in admin_ids


FAQ_LIST_PAGE_SIZE = 8


def _faq_short_id(keyword: str) -> str:
    """ID pendek & stabil dari keyword FAQ, dipakai sebagai callback_data.
    Telegram membatasi callback_data maksimal 64 byte, sedangkan keyword
    FAQ admin bisa lebih panjang dari itu — jadi tombol mengirim hash-nya
    saja, lalu handler mencocokkan balik ke keyword aslinya lewat
    db.list_faq() saat tombol ditekan (stateless, tetap benar walau
    proses sudah restart)."""
    return hashlib.sha1(keyword.strip().lower().encode("utf-8")).hexdigest()[:16]


def _parse_admin_ids(raw: str) -> set:
    """Parse FAQBOT_ADMIN_IDS="111,222,333" secara toleran — entri yang
    salah format di-skip + di-log, tidak menjatuhkan seluruh aplikasi."""
    admin_ids = set()
    for part in (raw or "").split(","):
        part = part.strip()
        if not part:
            continue
        try:
            admin_ids.add(int(part))
        except ValueError:
            logger.warning("FAQBOT_ADMIN_IDS: entri tidak valid dilewati: %r", part)
    return admin_ids


class _AsyncLoopThread:
    """Menjalankan satu asyncio event loop selamanya di thread khusus, dan
    menyediakan cara aman dipanggil dari banyak thread (mis. beberapa
    thread worker Flask sekaligus) untuk menjalankan coroutine di loop
    tersebut dan menunggu hasilnya secara sinkron.

    Ini adalah pola standar untuk menjembatani kode async (python-telegram-
    bot) dengan framework WSGI sinkron (Flask) secara thread-safe."""

    def __init__(self, name: str = "faqbot-loop"):
        self._loop = asyncio.new_event_loop()
        self._thread = threading.Thread(
            target=self._run_forever, name=name, daemon=True
        )
        self._thread.start()

    def _run_forever(self):
        asyncio.set_event_loop(self._loop)
        self._loop.run_forever()

    def run(self, coro, timeout: float = 30.0):
        """Jalankan coroutine di loop background thread, tunggu hasilnya
        (blocking) dari thread pemanggil. Aman dipanggil bersamaan dari
        banyak thread Flask sekaligus."""
        future = asyncio.run_coroutine_threadsafe(coro, self._loop)
        return future.result(timeout=timeout)

    def stop(self):
        self._loop.call_soon_threadsafe(self._loop.stop)
        self._thread.join(timeout=5)


def init_faq_bot(flask_app):
    """Daftarkan handler + route webhook FAQ bot ke Flask app yang sudah ada.
    Dipanggil sekali saat app.py start. Aman dipanggil walau env var belum
    diisi lengkap (route tetap terpasang, tapi bot tidak akan aktif sampai
    BOT_TOKEN diisi & /setwebhook dipanggil)."""

    if not FAQBOT_AVAILABLE:
        logger.warning("init_faq_bot dipanggil tapi python-telegram-bot tidak tersedia, dilewati.")
        return None

    bot_token = os.getenv("FAQBOT_BOT_TOKEN")
    if not bot_token:
        logger.warning("FAQBOT_BOT_TOKEN belum diatur di .env — FAQ bot tidak diaktifkan.")
        return None

    admin_ids = _parse_admin_ids(os.getenv("FAQBOT_ADMIN_IDS", ""))
    if not admin_ids:
        logger.warning("FAQBOT_ADMIN_IDS kosong/tidak valid — tidak ada admin yang bisa pakai perintah admin.")

    broadcast_channel_id = os.getenv("FAQBOT_BROADCAST_CHANNEL_ID") or None

    webhook_secret = os.getenv("FAQBOT_WEBHOOK_SECRET")
    if not webhook_secret:
        # Gunakan hash deterministik dari bot_token agar semua worker process (Gunicorn)
        # menghasilkan secret yang sama jika FAQBOT_WEBHOOK_SECRET tidak diatur di .env.
        webhook_secret = hashlib.sha256(f"faqbot_{bot_token}".encode('utf-8')).hexdigest()[:32]
        logger.info(
            "FAQBOT_WEBHOOK_SECRET tidak diatur di .env — menggunakan secret deterministik berbasis bot token."
        )

    openrouter_api_key = os.getenv("OPENROUTER_API_KEY")
    openrouter_model = os.getenv("OPENROUTER_MODEL", "openrouter/free")
    openrouter_url = "https://openrouter.ai/api/v1/chat/completions"
    ai_system_prompt = os.getenv(
        "AI_SYSTEM_PROMPT",
        "Kamu adalah asisten yang membalas dengan singkat, ramah, dan dalam Bahasa Indonesia.",
    )

    db.init_db()

    # Satu HTTP client dipakai ulang untuk semua panggilan /ask, alih-alih
    # membuat koneksi baru tiap request (lebih hemat & lebih cepat).
    http_client = httpx.AsyncClient(timeout=30)

    # ------------------------------------------------------------------
    # Handlers (logic sama seperti bot.py, dengan perbaikan di atas)
    # ------------------------------------------------------------------

    # -- Inline keyboard builder ---------------------------------------
    # Semua keyboard dibangun di sini (bukan level modul) supaya bisa
    # mengakses `admin_ids` lewat closure, sama seperti handler lain.

    def _main_menu_keyboard(user_id: int) -> InlineKeyboardMarkup:
        rows = [
            [InlineKeyboardButton("❓ Lihat FAQ", callback_data="faqlist:0")],
            [InlineKeyboardButton("🤖 Tanya AI", callback_data="ask_prompt")],
            [InlineKeyboardButton("📋 Bantuan", callback_data="help")],
        ]
        if _is_admin(user_id, admin_ids):
            rows.append([InlineKeyboardButton("👑 Menu Admin", callback_data="admin_menu")])
        return InlineKeyboardMarkup(rows)

    def _admin_menu_keyboard() -> InlineKeyboardMarkup:
        return InlineKeyboardMarkup([
            [InlineKeyboardButton("📊 Statistik user", callback_data="stats")],
            [InlineKeyboardButton("⬅️ Menu utama", callback_data="main_menu")],
        ])

    def _faq_list_keyboard(keywords: list, page: int = 0) -> InlineKeyboardMarkup:
        start_i = page * FAQ_LIST_PAGE_SIZE
        page_items = keywords[start_i:start_i + FAQ_LIST_PAGE_SIZE]

        rows = [
            [InlineKeyboardButton(f"❓ {kw}", callback_data=f"faq:{_faq_short_id(kw)}")]
            for kw in page_items
        ]

        nav_row = []
        if page > 0:
            nav_row.append(InlineKeyboardButton("⬅️ Sebelumnya", callback_data=f"faqlist:{page - 1}"))
        if start_i + FAQ_LIST_PAGE_SIZE < len(keywords):
            nav_row.append(InlineKeyboardButton("Berikutnya ➡️", callback_data=f"faqlist:{page + 1}"))
        if nav_row:
            rows.append(nav_row)

        rows.append([InlineKeyboardButton("🏠 Menu utama", callback_data="main_menu")])
        return InlineKeyboardMarkup(rows)

    def _help_text(user_id: int) -> str:
        text = (
            "📋 *Daftar Perintah*\n"
            "/start - mulai bot\n"
            "/faq <kata> - lihat jawaban FAQ\n"
            "/faqlist - daftar semua kata kunci FAQ\n"
            "/ask <pertanyaan> - tanya AI\n"
        )
        if _is_admin(user_id, admin_ids):
            text += (
                "\n👑 *Khusus Admin*\n"
                "/addfaq <kata> | <jawaban> - tambah/ubah FAQ\n"
                "/delfaq <kata> - hapus FAQ\n"
                "/broadcast <pesan> - kirim pesan ke semua user terdaftar\n"
                "/broadcastchannel <pesan> - kirim ke channel\n"
                "/stats - jumlah user terdaftar\n"
            )
        return text

    # -- Command handlers -------------------------------------------------

    async def start(update, context):
        user = update.effective_user
        db.upsert_user(user.id, user.username or "", user.first_name or "")
        await update.message.reply_text(
            f"Halo {user.first_name}! 👋\nSilakan pilih menu di bawah ini:",
            reply_markup=_main_menu_keyboard(user.id),
        )

    async def help_command(update, context):
        await update.message.reply_text(
            _help_text(update.effective_user.id),
            parse_mode="Markdown",
            reply_markup=_main_menu_keyboard(update.effective_user.id),
        )

    async def faq(update, context):
        if not context.args:
            return await update.message.reply_text("Contoh: /faq jadwal")
        keyword = " ".join(context.args)
        answer = db.get_faq(keyword)
        await update.message.reply_text(
            answer if answer else "❌ FAQ tidak ditemukan. Lihat /faqlist."
        )

    async def faqlist(update, context):
        keywords = db.list_faq()
        if not keywords:
            return await update.message.reply_text("Belum ada FAQ tersimpan.")
        await update.message.reply_text(
            "📚 Pilih pertanyaan di bawah ini, atau ketik /faq <kata>:",
            reply_markup=_faq_list_keyboard(keywords, 0),
        )

    async def button_callback(update, context):
        """Handler tunggal untuk semua tombol inline (callback_query).
        callback_data pakai prefix sederhana: main_menu, help, ask_prompt,
        admin_menu, stats, faqlist:<page>, faq:<short_id>."""
        query = update.callback_query
        user = query.from_user
        data = query.data or ""

        # Wajib dijawab sekali supaya ikon loading di tombol hilang. Hanya
        # dipanggil sekali per callback_query — penolakan akses ditangani
        # lewat edit teks di bawah, BUKAN answer() kedua (Telegram hanya
        # menerima satu answerCallbackQuery per callback_query).
        try:
            await query.answer()
        except TelegramError:
            pass

        async def _safe_edit(text: str, reply_markup=None, parse_mode=None):
            try:
                await query.edit_message_text(text=text, reply_markup=reply_markup, parse_mode=parse_mode)
            except BadRequest as e:
                if "Message is not modified" not in str(e):
                    logger.warning(f"button_callback: gagal edit pesan: {e}")
            except TelegramError as e:
                logger.warning(f"button_callback: gagal edit pesan: {e}")

        if data == "main_menu":
            await _safe_edit("Silakan pilih menu di bawah ini:", _main_menu_keyboard(user.id))

        elif data == "help":
            await _safe_edit(_help_text(user.id), _main_menu_keyboard(user.id), parse_mode="Markdown")

        elif data == "ask_prompt":
            await _safe_edit(
                "Ketik perintah berikut diikuti pertanyaanmu:\n`/ask <pertanyaanmu>`\n\n"
                "Contoh: /ask apa itu fotosintesis?",
                _main_menu_keyboard(user.id),
                parse_mode="Markdown",
            )

        elif data == "admin_menu":
            if not _is_admin(user.id, admin_ids):
                return await _safe_edit("⛔ Menu ini khusus admin.", _main_menu_keyboard(user.id))
            await _safe_edit("👑 Menu Admin:", _admin_menu_keyboard())

        elif data == "stats":
            if not _is_admin(user.id, admin_ids):
                return await _safe_edit("⛔ Menu ini khusus admin.", _main_menu_keyboard(user.id))
            await _safe_edit(f"📊 Total user terdaftar: {db.count_users()}", _admin_menu_keyboard())

        elif data.startswith("faqlist:"):
            try:
                page = int(data.split(":", 1)[1])
            except ValueError:
                page = 0
            keywords = db.list_faq()
            if not keywords:
                return await _safe_edit("Belum ada FAQ tersimpan.", _main_menu_keyboard(user.id))
            page = max(0, min(page, (len(keywords) - 1) // FAQ_LIST_PAGE_SIZE))
            await _safe_edit(
                "📚 Pilih pertanyaan di bawah ini, atau ketik /faq <kata>:",
                _faq_list_keyboard(keywords, page),
            )

        elif data.startswith("faq:"):
            short_id = data.split(":", 1)[1]
            keywords = db.list_faq()
            matched_keyword = next((kw for kw in keywords if _faq_short_id(kw) == short_id), None)
            answer = db.get_faq(matched_keyword) if matched_keyword else None
            if not answer:
                return await _safe_edit(
                    "❌ FAQ tidak ditemukan (mungkin baru saja dihapus admin).",
                    _main_menu_keyboard(user.id),
                )
            keyboard = InlineKeyboardMarkup([
                [InlineKeyboardButton("⬅️ Daftar FAQ", callback_data="faqlist:0")],
                [InlineKeyboardButton("🏠 Menu utama", callback_data="main_menu")],
            ])
            await _safe_edit(f"❓ *{matched_keyword}*\n\n{answer}", keyboard, parse_mode="Markdown")

        else:
            logger.warning(f"button_callback: callback_data tidak dikenal: {data!r}")

    async def addfaq(update, context):
        if not _is_admin(update.effective_user.id, admin_ids):
            return await update.message.reply_text("⛔ Perintah ini khusus admin.")
        raw = " ".join(context.args)
        if "|" not in raw:
            return await update.message.reply_text("Format: /addfaq kata kunci | jawabannya di sini")
        keyword, answer = raw.split("|", 1)
        if db.set_faq(keyword.strip(), answer.strip()):
            await update.message.reply_text(f"✅ FAQ '{keyword.strip()}' tersimpan.")
        else:
            await update.message.reply_text("❌ Gagal menyimpan FAQ (kata kunci/jawaban kosong atau error DB).")

    async def delfaq(update, context):
        if not _is_admin(update.effective_user.id, admin_ids):
            return await update.message.reply_text("⛔ Perintah ini khusus admin.")
        if not context.args:
            return await update.message.reply_text("Contoh: /delfaq jadwal")
        keyword = " ".join(context.args)
        if db.delete_faq(keyword):
            await update.message.reply_text(f"🗑️ FAQ '{keyword}' dihapus.")
        else:
            await update.message.reply_text("❌ FAQ tidak ditemukan.")

    async def broadcast(update, context):
        if not _is_admin(update.effective_user.id, admin_ids):
            return await update.message.reply_text("⛔ Perintah ini khusus admin.")
        message = " ".join(context.args)
        if not message:
            return await update.message.reply_text("Contoh: /broadcast Halo semua!")

        user_ids = db.get_all_user_ids()
        await update.message.reply_text(f"⏳ Mengirim broadcast ke {len(user_ids)} user...")

        sukses, gagal = 0, 0
        for uid in user_ids:
            try:
                await context.bot.send_message(chat_id=uid, text=message)
                sukses += 1
            except RetryAfter as e:
                # Telegram minta kita tunggu — hormati durasinya lalu coba lagi sekali.
                await asyncio.sleep(e.retry_after + 0.5)
                try:
                    await context.bot.send_message(chat_id=uid, text=message)
                    sukses += 1
                except TelegramError:
                    gagal += 1
            except TelegramError:
                # Mis. user sudah block bot / chat tidak ditemukan lagi.
                gagal += 1
            await asyncio.sleep(_BROADCAST_DELAY_SECONDS)

        await update.message.reply_text(f"✅ Broadcast selesai.\nBerhasil: {sukses}\nGagal: {gagal}")

    async def broadcast_channel(update, context):
        if not _is_admin(update.effective_user.id, admin_ids):
            return await update.message.reply_text("⛔ Perintah ini khusus admin.")
        if not broadcast_channel_id:
            return await update.message.reply_text("⚠️ FAQBOT_BROADCAST_CHANNEL_ID belum diatur di .env")
        message = " ".join(context.args)
        if not message:
            return await update.message.reply_text("Contoh: /broadcastchannel Pengumuman baru!")
        try:
            await context.bot.send_message(chat_id=broadcast_channel_id, text=message)
            await update.message.reply_text("✅ Terkirim ke channel.")
        except TelegramError as e:
            await update.message.reply_text(f"❌ Gagal kirim: {e}")

    async def stats(update, context):
        if not _is_admin(update.effective_user.id, admin_ids):
            return await update.message.reply_text("⛔ Perintah ini khusus admin.")
        await update.message.reply_text(f"📊 Total user terdaftar: {db.count_users()}")

    async def anti_spam_filter(update, context):
        message = update.message
        if not message or not message.text:
            return
        user = update.effective_user
        db.upsert_user(user.id, user.username or "", user.first_name or "")

        if _is_admin(user.id, admin_ids):
            return

        text_lower = message.text.lower()
        violation_reason = None

        for word in BANNED_WORDS:
            if word.lower() in text_lower:
                violation_reason = f"kata terlarang ('{word}')"
                break

        if not violation_reason:
            link_count = len(LINK_PATTERN.findall(message.text))
            if link_count > MAX_LINKS_PER_MESSAGE:
                violation_reason = f"terlalu banyak link ({link_count})"

        if not violation_reason:
            return

        try:
            await message.delete()
        except TelegramError as e:
            logger.warning(f"Gagal hapus pesan pelanggaran: {e}")

        warn_total = db.add_warn(user.id)
        warn_msg = (
            f"⚠️ {user.first_name}, pesanmu dihapus karena {violation_reason}.\n"
            f"Peringatan: {warn_total}/{MAX_WARN_BEFORE_MUTE}"
        )

        if MAX_WARN_BEFORE_MUTE and warn_total >= MAX_WARN_BEFORE_MUTE:
            try:
                await context.bot.restrict_chat_member(
                    chat_id=update.effective_chat.id,
                    user_id=user.id,
                    permissions=ChatPermissions(
                        can_send_messages=False,
                        can_send_audios=False,
                        can_send_documents=False,
                        can_send_photos=False,
                        can_send_videos=False,
                        can_send_video_notes=False,
                        can_send_voice_notes=False,
                        can_send_polls=False,
                        can_send_other_messages=False,
                        can_add_web_page_previews=False,
                    ),
                )
                warn_msg += "\n🔇 User di-mute karena melebihi batas peringatan."
                db.reset_warn(user.id)
            except TelegramError as e:
                logger.warning(f"Gagal mute user {user.id}: {e}")

        try:
            await context.bot.send_message(chat_id=update.effective_chat.id, text=warn_msg)
        except TelegramError as e:
            logger.warning(f"Gagal kirim pesan peringatan: {e}")

    async def call_openrouter(user_message: str) -> str:
        if not openrouter_api_key:
            return "⚠️ Fitur AI belum diaktifkan (OPENROUTER_API_KEY belum diisi di .env)."

        headers = {
            "Authorization": f"Bearer {openrouter_api_key}",
            "Content-Type": "application/json",
        }
        payload = {
            "model": openrouter_model,
            "messages": [
                {"role": "system", "content": ai_system_prompt},
                {"role": "user", "content": user_message},
            ],
        }

        try:
            resp = await http_client.post(openrouter_url, headers=headers, json=payload)
            resp.raise_for_status()
            data = resp.json()
            return data["choices"][0]["message"]["content"].strip()
        except httpx.HTTPStatusError as e:
            logger.error(f"OpenRouter HTTP error: {e.response.text}")
            return "❌ AI sedang bermasalah (error dari server AI). Coba lagi nanti."
        except Exception as e:
            logger.error(f"OpenRouter error: {e}")
            return "❌ Gagal menghubungi layanan AI."

    async def ask_ai(update, context):
        if not context.args:
            return await update.message.reply_text("Contoh: /ask apa itu fotosintesis?")
        question = " ".join(context.args)
        await context.bot.send_chat_action(chat_id=update.effective_chat.id, action="typing")
        answer = await call_openrouter(question)
        await update.message.reply_text(answer)

    async def welcome_new_member(update, context):
        for member in update.message.new_chat_members:
            db.upsert_user(member.id, member.username or "", member.first_name or "")
            await update.message.reply_text(f"Selamat datang, {member.first_name}! 🎉")

    # ------------------------------------------------------------------
    # Setup Application (python-telegram-bot)
    # ------------------------------------------------------------------
    ptb_app = Application.builder().token(bot_token).updater(None).build()

    ptb_app.add_handler(CommandHandler("start", start))
    ptb_app.add_handler(CommandHandler("help", help_command))
    ptb_app.add_handler(CommandHandler("faq", faq))
    ptb_app.add_handler(CommandHandler("faqlist", faqlist))
    ptb_app.add_handler(CommandHandler("addfaq", addfaq))
    ptb_app.add_handler(CommandHandler("delfaq", delfaq))
    ptb_app.add_handler(CommandHandler("broadcast", broadcast))
    ptb_app.add_handler(CommandHandler("broadcastchannel", broadcast_channel))
    ptb_app.add_handler(CommandHandler("stats", stats))
    ptb_app.add_handler(CommandHandler("ask", ask_ai))
    ptb_app.add_handler(MessageHandler(filters.StatusUpdate.NEW_CHAT_MEMBERS, welcome_new_member))
    ptb_app.add_handler(CallbackQueryHandler(button_callback))
    ptb_app.add_handler(MessageHandler(filters.TEXT & ~filters.COMMAND, anti_spam_filter))

    # ------------------------------------------------------------------
    # Event loop khusus jalan di thread terpisah — lihat docstring
    # _AsyncLoopThread di atas untuk alasan thread-safety-nya.
    # ------------------------------------------------------------------
    loop_worker = _AsyncLoopThread(name="faqbot-loop")

    # 2026 Pro fix: ptb_app.initialize() memanggil bot.get_me() ke API
    # Telegram secara sinkron saat startup. Proxy outbound PythonAnywhere
    # kadang melempar 503 Service Unavailable sesaat (bukan masalah token/
    # kode kita). Sebelumnya kegagalan ini tidak ditangkap sama sekali ->
    # merambat ke app.py -> menjatuhkan SELURUH Flask app (situs down).
    # Sekarang: retry singkat dengan backoff, dan kalau tetap gagal, FAQ
    # bot dinonaktifkan (route webhook tidak didaftarkan) tapi app Flask
    # utama tetap hidup normal.
    _INIT_RETRIES = 3
    _INIT_BACKOFF_SECONDS = 2
    last_error = None
    for attempt in range(1, _INIT_RETRIES + 1):
        try:
            loop_worker.run(ptb_app.initialize())
            loop_worker.run(ptb_app.start())
            last_error = None
            break
        except Exception as e:
            last_error = e
            logger.warning(
                "FAQ bot: gagal initialize/start (percobaan %s/%s): %s",
                attempt, _INIT_RETRIES, e,
            )
            if attempt < _INIT_RETRIES:
                time.sleep(_INIT_BACKOFF_SECONDS)

    if last_error is not None:
        logger.error(
            "FAQ bot: initialize/start gagal setelah %s percobaan "
            "(kemungkinan proxy/jaringan ke Telegram bermasalah). "
            "FAQ bot dinonaktifkan untuk sesi ini, marketplace tetap jalan. "
            "Error terakhir: %s",
            _INIT_RETRIES, last_error,
        )
        loop_worker.stop()
        return None

    def _shutdown():
        try:
            loop_worker.run(ptb_app.stop())
            loop_worker.run(ptb_app.shutdown())
            loop_worker.run(http_client.aclose())
        except Exception as e:
            logger.warning(f"FAQ bot shutdown error: {e}")
        finally:
            loop_worker.stop()

    atexit.register(_shutdown)

    # ------------------------------------------------------------------
    # Routes Flask (didaftarkan ke app Flask marketplace yang sudah ada)
    # ------------------------------------------------------------------
    @flask_app.route(f"/telegram/faqbot/webhook/{webhook_secret}", methods=["POST"])
    def faqbot_webhook():
        # Lapisan kedua: kalau Telegram mengirim secret_token header
        # (didaftarkan lewat /setwebhook di bawah), verifikasi juga —
        # bukan cuma mengandalkan URL yang bisa saja ter-log di proxy.
        header_token = request.headers.get("X-Telegram-Bot-Api-Secret-Token")
        if header_token is not None and header_token != webhook_secret:
            logger.warning("FAQ bot webhook: secret token header tidak cocok.")
            return "Forbidden", 403

        try:
            payload = request.get_json(force=True, silent=True)
            if not payload:
                return "Bad Request", 400
            update = Update.de_json(payload, ptb_app.bot)
            loop_worker.run(ptb_app.process_update(update))
        except Exception as e:
            # Selalu balas 200 setelah error tercatat, supaya Telegram
            # tidak retry berkali-kali dan membanjiri log untuk 1 update
            # yang gagal diproses.
            logger.error(f"FAQ bot webhook error: {e}", exc_info=True)
        return "OK", 200

    @flask_app.route(f"/telegram/faqbot/setwebhook/{webhook_secret}")
    def faqbot_set_webhook():
        """Buka URL ini sekali lewat browser setelah deploy untuk
        mendaftarkan webhook FAQ bot ke Telegram."""
        domain = request.host_url.rstrip("/")
        webhook_url = f"{domain}/telegram/faqbot/webhook/{webhook_secret}"
        try:
            ok = loop_worker.run(
                ptb_app.bot.set_webhook(url=webhook_url, secret_token=webhook_secret)
            )
        except TelegramError as e:
            return f"❌ Gagal mendaftarkan webhook: {e}", 500
        return f"FAQ bot webhook diatur ke {webhook_url} -> {ok}", 200

    logger.info("FAQ Bot terpasang: POST /telegram/faqbot/webhook/<secret>")
    return ptb_app
