"""
Chat Memory — Pusat Gadai Indonesia (2026 Professional Edition)
====================================================================
Modul ini menyediakan dua kelas yang dipakai bareng oleh customer_chat.py,
ai_service.py, dan app.py:

    SessionMemory   — riwayat percakapan (per session_id), dipakai untuk
                       membangun context yang dikirim ke LLM dan untuk
                       restore riwayat di widget setelah reload/restart.
    CustomerMemory  — fakta jangka panjang tentang pelanggan (nama,
                       preferensi, dll), di-key dengan session_id sementara
                       lalu disambungkan ke nomor HP begitu identitas
                       pelanggan diketahui, supaya nyambung lintas
                       sesi/device.

Sebelum 2026 Pro, SessionMemory murni in-memory TTLCache (riwayat hilang
tiap restart / setelah 30 menit tidak aktif). Sekarang setiap pesan JUGA
disimpan ke tabel chat_conversations/chat_messages (models.py), dengan
in-memory cache tetap dipakai sebagai read-path cepat untuk request
berikutnya dalam sesi yang sama — kalau cache kosong (mis. baru restart),
otomatis dipulihkan dari DB.

Desain "gagal lembut" (graceful degradation): setiap operasi tulis/baca ke
DB dibungkus try/except. Kalau DB lagi bermasalah, cache in-memory tetap
membuat percakapan yang sedang berjalan tetap berfungsi — cuma riwayat
lintas-restart yang sementara tidak tersimpan, bukan seluruh chat-nya mati.

Catatan import: kalau modul ini gagal di-import sama sekali (mis. dipakai
di luar layout package Flask app), ai_service.py & customer_chat.py punya
fallback in-memory murni masing-masing — lihat komentar di ai_service.py
sekitar `class SessionMemory:  # type: ignore`.
"""
import json
import logging
import re
import threading
import time
from datetime import datetime
from typing import Any, Dict, List, Optional

from sqlalchemy.exc import IntegrityError

from models import db, ChatConversation, ChatMessage, CustomerMemoryFact

logger = logging.getLogger(__name__)

__all__ = ['SessionMemory', 'CustomerMemory']


# ---------------------------------------------------------------------------
# Tiny dependency-free TTL cache (sengaja tidak pakai cachetools — biar
# modul ini tidak menambah dependency wajib baru; polanya sama dgn
# _TTLCache di ai_service.py supaya konsisten gaya di seluruh codebase).
# ---------------------------------------------------------------------------
class _TTLCache:
    def __init__(self, maxsize: int = 3000, ttl: int = 1800):
        self._store: Dict[str, tuple] = {}
        self._maxsize = maxsize
        self._ttl = ttl
        self._lock = threading.Lock()

    def get(self, key: str) -> Optional[Any]:
        with self._lock:
            entry = self._store.get(key)
            if entry is None:
                return None
            value, expires_at = entry
            if time.monotonic() > expires_at:
                del self._store[key]
                return None
            return value

    def set(self, key: str, value: Any) -> None:
        with self._lock:
            if len(self._store) >= self._maxsize and key not in self._store:
                oldest = min(self._store, key=lambda k: self._store[k][1])
                del self._store[oldest]
            self._store[key] = (value, time.monotonic() + self._ttl)

    def delete(self, key: str) -> None:
        with self._lock:
            self._store.pop(key, None)

    def __len__(self) -> int:
        with self._lock:
            return len(self._store)


# In-memory cache dari riwayat percakapan (list of {'role','content'} per
# session_id), TTL 30 menit, mirror perilaku TTLCache lama sebelum 2026 Pro.
_session_cache = _TTLCache(maxsize=3000, ttl=1800)


# =============================================================================
# SessionMemory — riwayat percakapan per session_id
# =============================================================================
class SessionMemory:
    """Riwayat percakapan (user/assistant) untuk satu session_id.

    `get()` mengembalikan window pendek ({'role','content'} saja) yang
    dikirim sebagai context ke LLM — cache-first, fallback ke DB kalau
    cache kosong. `get_full_history()` selalu membaca DB dan mengembalikan
    seluruh riwayat dengan metadata lengkap (model, cost_usd, created_at,
    dst) untuk restore di widget.
    """

    MAX_HISTORY = 24  # ~12 giliran user/assistant dikirim sbg context ke LLM

    # -- internal -------------------------------------------------------
    @staticmethod
    def _get_or_create_conversation(session_id: str) -> ChatConversation:
        conv = ChatConversation.query.filter_by(session_id=session_id).first()
        if conv:
            if conv.is_archived:
                # session_id unik di DB, jadi tidak bisa membuat baris baru
                # untuk sesi yang sama — kalau ada pesan baru masuk ke sini
                # (mis. request yang sempat nyangkut pas clearChat() terjadi
                # di sisi klien), anggap percakapan aktif lagi alih-alih
                # diam-diam menumpuk pesan baru ke thread yang sudah
                # "ditutup" dan disembunyikan dari daftar aktif.
                conv.is_archived = False
            return conv
        conv = ChatConversation(
            session_id=session_id,
            customer_key=CustomerMemory.key_for_session(session_id),
        )
        db.session.add(conv)
        try:
            db.session.commit()
        except IntegrityError:
            # Race: request lain sudah membuat baris yg sama duluan.
            db.session.rollback()
            conv = ChatConversation.query.filter_by(session_id=session_id).first()
        if conv is None:
            # Edge case ekstrem: baris hilang di antara IntegrityError dan
            # query ulang (mis. dihapus request lain bersamaan). Lempar
            # error yang jelas alih-alih membiarkan caller crash dengan
            # AttributeError yang membingungkan saat mengakses conv.id.
            raise RuntimeError(f'Gagal mendapatkan/membuat ChatConversation utk session_id={session_id!r}')
        return conv

    # -- public API -------------------------------------------------------
    @classmethod
    def get(cls, session_id: str) -> List[Dict]:
        """Window riwayat terbaru (maks MAX_HISTORY pesan) siap dikirim ke
        LLM sebagai `messages`. Cache-first; kalau cache kosong (baru
        restart / worker berbeda), dipulihkan dari DB lalu di-cache lagi."""
        key = str(session_id)
        cached = _session_cache.get(key)
        if cached is not None:
            return list(cached)

        history: List[Dict] = []
        try:
            conv = ChatConversation.query.filter_by(session_id=key).first()
            if conv:
                recent = (
                    ChatMessage.query
                    .filter_by(conversation_id=conv.id)
                    .order_by(ChatMessage.id.desc())
                    .limit(cls.MAX_HISTORY)
                    .all()
                )
                history = [{'role': m.role, 'content': m.content} for m in reversed(recent)]
        except Exception as e:
            logger.warning('SessionMemory.get: gagal memuat riwayat dari DB utk %s: %s', key, e)

        _session_cache.set(key, history)
        return list(history)

    @classmethod
    def append(cls, session_id: str, role: str, content: str, **meta: Any) -> None:
        """Tambahkan satu pesan. `meta` opsional: model, cost_usd,
        tool_calls (list, akan di-JSON-encode), source (ai|fallback|local|
        rate_limited|user, default 'ai')."""
        key = str(session_id)

        # 1) Cache in-memory dulu — supaya percakapan yg sedang berjalan
        #    tetap lanjut normal walau langkah (2) di bawah gagal.
        history = list(_session_cache.get(key) or [])
        history.append({'role': role, 'content': content})
        history = history[-cls.MAX_HISTORY:]
        _session_cache.set(key, history)

        # 2) Persist ke DB.
        try:
            conv = cls._get_or_create_conversation(key)

            if not conv.title and role == 'user':
                title = content.strip().replace('\n', ' ')[:60]
                conv.title = title or None

            tool_calls = meta.get('tool_calls')
            msg = ChatMessage(
                conversation_id=conv.id,
                role=role,
                content=content,
                model=meta.get('model'),
                cost_usd=meta.get('cost_usd') or 0.0,
                tool_calls_json=json.dumps(tool_calls) if tool_calls else None,
                source=meta.get('source', 'ai'),
            )
            conv.last_message_at = datetime.utcnow()
            db.session.add(msg)
            db.session.commit()
        except Exception as e:
            db.session.rollback()
            logger.warning('SessionMemory.append: gagal menyimpan pesan utk %s: %s', key, e)

    @classmethod
    def clear(cls, session_id: str) -> None:
        """Arsipkan percakapan (riwayat TIDAK dihapus, cuma disembunyikan
        dari daftar aktif) + kosongkan cache in-memory. Widget selalu
        generate session_id baru setelah memanggil ini, jadi pelanggan
        mulai percakapan bersih tanpa kehilangan histori lama di DB."""
        key = str(session_id)
        _session_cache.delete(key)
        try:
            conv = ChatConversation.query.filter_by(session_id=key).first()
            if conv:
                conv.is_archived = True
                db.session.commit()
        except Exception as e:
            db.session.rollback()
            logger.warning('SessionMemory.clear: gagal arsipkan percakapan %s: %s', key, e)

    @classmethod
    def get_full_history(cls, session_id: str) -> List[Dict]:
        """Seluruh riwayat pesan (tanpa batas MAX_HISTORY) dengan metadata
        lengkap — dipakai endpoint /api/public/chat/history untuk restore
        widget setelah reload. Selalu baca langsung dari DB (bukan cache)
        supaya lengkap & akurat."""
        key = str(session_id)
        try:
            conv = ChatConversation.query.filter_by(session_id=key).first()
            if not conv:
                return []
            return [m.to_dict() for m in conv.messages]
        except Exception as e:
            logger.warning('SessionMemory.get_full_history: gagal utk %s: %s', key, e)
            return []

    @classmethod
    def cache_size(cls) -> int:
        """Jumlah sesi yang sedang ada di cache in-memory (diagnostik utk
        health check, bukan jumlah total percakapan di DB)."""
        return len(_session_cache)


# =============================================================================
# CustomerMemory — fakta jangka panjang tentang pelanggan
# =============================================================================
class CustomerMemory:
    """Fakta yang diingat AI tentang seorang pelanggan lintas sesi/device
    (nama panggilan, minat produk, budget, dst). Di-key dengan:

        'sid:<session_id>'  — sementara, sebelum identitas diketahui
        'ph:<nomor_hp>'     — permanen, setelah pelanggan sebut no. HP-nya
                              (mis. saat cek order/KYC/cicilan)

    Lihat CustomerMemoryFact di models.py untuk skema tabelnya.
    """

    @staticmethod
    def key_for_session(session_id: str) -> str:
        """Kunci sementara sebelum identitas (no. HP) pelanggan diketahui."""
        return f'sid:{session_id}'

    @staticmethod
    def key_for_phone(phone: str) -> str:
        """Kunci permanen setelah pelanggan teridentifikasi lewat no. HP.

        Menormalkan format nomor Indonesia ke satu bentuk baku (awalan
        '62') sebelum jadi key, karena pelanggan bisa menyebut nomor yang
        SAMA dengan format berbeda-beda di sesi/percakapan berbeda —
        '081234567890', '6281234567890', '+6281234567890' — yang kalau
        cuma dibuang karakter non-digitnya akan menghasilkan TIGA key yang
        berbeda untuk pelanggan yang sama, sehingga merge_session_into_phone()
        gagal menyambungkan memorinya lintas sesi/device.
        """
        digits = re.sub(r'[^0-9]', '', phone or '')
        if digits.startswith('0'):
            digits = '62' + digits[1:]
        elif digits and not digits.startswith('62'):
            digits = '62' + digits
        return f'ph:{digits}'

    @classmethod
    def remember(
        cls,
        customer_key: str,
        fact_key: str,
        fact_value: str,
        fact_type: str = 'catatan',
        source_session: Optional[str] = None,
    ) -> None:
        """Simpan/update satu fakta (upsert berdasarkan customer_key +
        fact_key — lihat UniqueConstraint di CustomerMemoryFact)."""
        try:
            fact = CustomerMemoryFact.query.filter_by(
                customer_key=customer_key, fact_key=fact_key
            ).first()
            if fact:
                fact.fact_value = fact_value
                fact.fact_type = fact_type
                fact.source_session = source_session
                fact.updated_at = datetime.utcnow()
            else:
                fact = CustomerMemoryFact(
                    customer_key=customer_key,
                    fact_key=fact_key,
                    fact_value=fact_value,
                    fact_type=fact_type,
                    source_session=source_session,
                )
                db.session.add(fact)
            db.session.commit()
        except IntegrityError:
            # Race: dua request nyimpen fact_key yg sama nyaris bersamaan.
            # Baris sudah keburu dibuat request lain di antara query & commit
            # di atas — coba sekali lagi sebagai update murni.
            db.session.rollback()
            try:
                fact = CustomerMemoryFact.query.filter_by(
                    customer_key=customer_key, fact_key=fact_key
                ).first()
                if fact:
                    fact.fact_value = fact_value
                    fact.fact_type = fact_type
                    fact.source_session = source_session
                    fact.updated_at = datetime.utcnow()
                    db.session.commit()
            except Exception as e:
                db.session.rollback()
                logger.warning('CustomerMemory.remember: gagal utk %s/%s: %s', customer_key, fact_key, e)
        except Exception as e:
            db.session.rollback()
            logger.warning('CustomerMemory.remember: gagal utk %s/%s: %s', customer_key, fact_key, e)

    @classmethod
    def get_facts(cls, customer_key: str, limit: int = 30) -> List[CustomerMemoryFact]:
        """Ambil fakta-fakta tersimpan untuk satu customer_key, terbaru dulu."""
        try:
            return (
                CustomerMemoryFact.query
                .filter_by(customer_key=customer_key)
                .order_by(CustomerMemoryFact.updated_at.desc())
                .limit(limit)
                .all()
            )
        except Exception as e:
            logger.warning('CustomerMemory.get_facts: gagal utk %s: %s', customer_key, e)
            return []

    @classmethod
    def format_for_prompt(cls, customer_key: str) -> str:
        """Rangkum fakta yang diketahui jadi satu blok teks system-prompt.
        String kosong kalau belum ada fakta apa pun — caller (customer_chat.
        py `_build_messages`) memakai `if memory_block:` sebelum menyisipkan
        ini, jadi aman untuk selalu dipanggil tanpa cek dulu."""
        facts = cls.get_facts(customer_key)
        if not facts:
            return ''
        lines = [f'- {f.fact_key}: {f.fact_value}' for f in facts]
        return (
            'Catatan tentang pelanggan ini dari percakapan sebelumnya '
            '(gunakan secara natural dalam jawaban, jangan sebutkan bahwa '
            'ini "data tersimpan" atau semacamnya):\n' + '\n'.join(lines)
        )

    @classmethod
    def merge_session_into_phone(cls, session_id: str, phone: str) -> None:
        """Begitu pelanggan menyebutkan no. HP di suatu percakapan,
        sambungkan sesi itu — dan fakta-fakta sementara yg sudah tercatat
        di key sid:<session_id> — ke key permanen ph:<no_hp>, supaya
        memorinya nyambung lintas sesi/device berikutnya."""
        old_key = cls.key_for_session(session_id)
        new_key = cls.key_for_phone(phone)
        if not phone or old_key == new_key:
            return

        try:
            conv = ChatConversation.query.filter_by(session_id=str(session_id)).first()
            if conv:
                conv.customer_key = new_key

            old_facts = CustomerMemoryFact.query.filter_by(customer_key=old_key).all()
            for fact in old_facts:
                already_exists = CustomerMemoryFact.query.filter_by(
                    customer_key=new_key, fact_key=fact.fact_key
                ).first()
                if already_exists:
                    # Fakta permanen sudah ada duluan — buang duplikat
                    # sementara, pertahankan yg permanen.
                    db.session.delete(fact)
                else:
                    fact.customer_key = new_key

            db.session.commit()
        except Exception as e:
            db.session.rollback()
            logger.warning(
                'CustomerMemory.merge_session_into_phone: gagal %s -> %s: %s',
                old_key, new_key, e,
            )
