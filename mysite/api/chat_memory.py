"""
api/chat_memory.py — DB-backed session & customer memory for customer chat AI.

Provides two classes consumed by customer_chat.py and ai_service.py:

  SessionMemory  — per-session conversation history (persisted to
                   ChatConversation / ChatMessage, with in-memory cache
                   for speed).

  CustomerMemory — long-term facts about a customer keyed by phone number
                   or session-id (backed by CustomerMemoryFact).
"""

from __future__ import annotations

import json
import logging
import threading
import time
from typing import Any, Dict, List, Optional

logger = logging.getLogger(__name__)

# ---------------------------------------------------------------------------
# Simple in-memory TTL cache (no external dependency)
# ---------------------------------------------------------------------------

class _MemoryCache:
    """Thread-safe in-memory cache with TTL and max-size eviction."""

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
                # Evict the entry closest to expiry
                oldest = min(self._store, key=lambda k: self._store[k][1])
                del self._store[oldest]
            self._store[key] = (value, time.monotonic() + self._ttl)

    def delete(self, key: str) -> None:
        with self._lock:
            self._store.pop(key, None)

    def __len__(self) -> int:
        with self._lock:
            return len(self._store)


# Module-level caches
_session_cache = _MemoryCache(maxsize=3000, ttl=1800)   # 30 min
_customer_cache = _MemoryCache(maxsize=1000, ttl=3600)  # 1 hour


# ---------------------------------------------------------------------------
# SessionMemory
# ---------------------------------------------------------------------------

class SessionMemory:
    """
    Conversation turn history for a single chat session.

    Read path:  in-memory cache → DB
    Write path: in-memory cache + async-safe DB upsert

    Format stored in cache / returned by get():
        [{"role": "user"|"assistant"|"system", "content": "..."}, ...]
    """

    MAX_HISTORY = 24  # max turns kept in memory / returned to LLM

    # ------------------------------------------------------------------
    # Public API
    # ------------------------------------------------------------------

    @classmethod
    def get(cls, session_id: str) -> List[Dict]:
        """Return recent conversation history for the session."""
        key = str(session_id)
        cached = _session_cache.get(key)
        if cached is not None:
            return list(cached)

        # Load from DB
        history = cls._load_from_db(key)
        _session_cache.set(key, history)
        return list(history)

    @classmethod
    def append(
        cls,
        session_id: str,
        role: str,
        content: str,
        *,
        model: Optional[str] = None,
        cost_usd: float = 0.0,
        tool_calls: Optional[List[Dict]] = None,
        source: str = "ai",
    ) -> None:
        """Append a new turn to the session history and persist to DB."""
        key = str(session_id)

        # Update in-memory cache
        history = list(_session_cache.get(key) or cls._load_from_db(key))
        history.append({"role": role, "content": content})
        history = history[-cls.MAX_HISTORY:]
        _session_cache.set(key, history)

        # Persist to DB (best-effort — never crash the chat flow)
        try:
            cls._persist(
                key, role, content,
                model=model, cost_usd=cost_usd,
                tool_calls=tool_calls, source=source,
            )
        except Exception as exc:
            logger.warning("SessionMemory._persist failed for %s: %s", key, exc)

    @classmethod
    def clear(cls, session_id: str) -> None:
        """Clear the in-memory cache for a session (DB records kept)."""
        _session_cache.delete(str(session_id))

    @classmethod
    def get_full_history(cls, session_id: str) -> List[Dict]:
        """Alias for get() — returns history for LLM consumption."""
        return cls.get(session_id)

    @classmethod
    def cache_size(cls) -> int:
        return len(_session_cache)

    # ------------------------------------------------------------------
    # Internal helpers
    # ------------------------------------------------------------------

    @classmethod
    def _load_from_db(cls, session_id: str) -> List[Dict]:
        try:
            from models import ChatConversation
            conv = ChatConversation.query.filter_by(session_id=session_id).first()
            if conv is None:
                return []
            msgs = (
                conv.messages
                .order_by(None)  # respect model default (id ASC)
                .limit(cls.MAX_HISTORY)
                .all()
            )
            return [{"role": m.role, "content": m.content} for m in msgs]
        except Exception as exc:
            logger.debug("SessionMemory._load_from_db error: %s", exc)
            return []

    @classmethod
    def _persist(
        cls,
        session_id: str,
        role: str,
        content: str,
        *,
        model: Optional[str],
        cost_usd: float,
        tool_calls: Optional[List[Dict]],
        source: str,
    ) -> None:
        from models import db, ChatConversation, ChatMessage

        conv = ChatConversation.query.filter_by(session_id=session_id).first()
        if conv is None:
            from datetime import datetime
            conv = ChatConversation(
                session_id=session_id,
                created_at=datetime.utcnow(),
                updated_at=datetime.utcnow(),
            )
            db.session.add(conv)
            db.session.flush()  # get conv.id without full commit

        msg = ChatMessage(
            conversation_id=conv.id,
            role=role,
            content=content,
            model=model,
            cost_usd=cost_usd or 0.0,
            tool_calls_json=json.dumps(tool_calls) if tool_calls else None,
            source=source or "ai",
        )
        db.session.add(msg)

        from datetime import datetime
        conv.updated_at = datetime.utcnow()
        conv.last_message_at = datetime.utcnow()

        # Auto-title from first user message
        if role == "user" and not conv.title:
            conv.title = content[:80].strip()

        db.session.commit()


# ---------------------------------------------------------------------------
# CustomerMemory
# ---------------------------------------------------------------------------

class CustomerMemory:
    """
    Long-term facts about a customer, keyed by phone number (permanent) or
    session-id (temporary, before the customer is identified).

    Keys:
      "ph:<clean_phone>"  after the customer gives their phone number
      "sid:<session_id>"  before identification
    """

    MAX_FACTS = 20

    # ------------------------------------------------------------------
    # Key helpers
    # ------------------------------------------------------------------

    @staticmethod
    def key_for_session(session_id: str) -> str:
        return f"sid:{session_id}"

    @staticmethod
    def key_for_phone(phone: str) -> str:
        import re
        clean = re.sub(r"[^0-9]", "", phone)
        return f"ph:{clean}"

    # ------------------------------------------------------------------
    # Public API
    # ------------------------------------------------------------------

    @classmethod
    def get_facts(cls, customer_key: str) -> List[Dict]:
        """Return all known facts about this customer."""
        cached = _customer_cache.get(customer_key)
        if cached is not None:
            return list(cached)
        facts = cls._load_facts(customer_key)
        _customer_cache.set(customer_key, facts)
        return list(facts)

    @classmethod
    def set_fact(
        cls,
        customer_key: str,
        fact_key: str,
        fact_value: str,
        fact_type: str = "catatan",
        source_session: Optional[str] = None,
    ) -> None:
        """Upsert a single fact for this customer."""
        try:
            cls._upsert_fact(customer_key, fact_key, fact_value, fact_type, source_session)
        except Exception as exc:
            logger.warning("CustomerMemory.set_fact failed for %s: %s", customer_key, exc)
        # Invalidate cache
        _customer_cache.delete(customer_key)

    @classmethod
    def format_for_prompt(cls, customer_key: str) -> str:
        """Return a system-prompt-friendly summary of known facts, or ''."""
        facts = cls.get_facts(customer_key)
        if not facts:
            return ""
        lines = ["[Informasi pelanggan yang diketahui:]"]
        for f in facts:
            lines.append(f"  • {f.get('fact_key', '?')}: {f.get('fact_value', '')}")
        return "\n".join(lines)

    @classmethod
    def merge_session_into_phone(cls, session_id: str, phone: str) -> None:
        """
        After identifying a customer's phone number, migrate all session-keyed
        facts to the permanent phone-keyed bucket, and link the conversation.
        """
        sid_key = cls.key_for_session(session_id)
        ph_key = cls.key_for_phone(phone)

        # Move facts
        old_facts = cls.get_facts(sid_key)
        for f in old_facts:
            try:
                cls._upsert_fact(
                    ph_key, f["fact_key"], f["fact_value"],
                    f.get("fact_type", "catatan"), session_id,
                )
            except Exception:
                pass

        # Link ChatConversation to customer_key
        try:
            from models import db, ChatConversation
            conv = ChatConversation.query.filter_by(session_id=str(session_id)).first()
            if conv and not conv.customer_key:
                conv.customer_key = ph_key
                db.session.commit()
        except Exception as exc:
            logger.debug("merge_session_into_phone link conv failed: %s", exc)

        # Refresh caches
        _customer_cache.delete(sid_key)
        _customer_cache.delete(ph_key)

    # ------------------------------------------------------------------
    # Internal helpers
    # ------------------------------------------------------------------

    @classmethod
    def _load_facts(cls, customer_key: str) -> List[Dict]:
        try:
            from models import CustomerMemoryFact
            rows = (
                CustomerMemoryFact.query
                .filter_by(customer_key=customer_key)
                .order_by(CustomerMemoryFact.updated_at.desc())
                .limit(cls.MAX_FACTS)
                .all()
            )
            return [r.to_dict() for r in rows]
        except Exception as exc:
            logger.debug("CustomerMemory._load_facts error: %s", exc)
            return []

    @classmethod
    def _upsert_fact(
        cls,
        customer_key: str,
        fact_key: str,
        fact_value: str,
        fact_type: str,
        source_session: Optional[str],
    ) -> None:
        from models import db, CustomerMemoryFact
        from datetime import datetime

        existing = CustomerMemoryFact.query.filter_by(
            customer_key=customer_key, fact_key=fact_key
        ).first()

        if existing:
            existing.fact_value = fact_value
            existing.fact_type = fact_type
            existing.updated_at = datetime.utcnow()
            if source_session:
                existing.source_session = source_session
        else:
            row = CustomerMemoryFact(
                customer_key=customer_key,
                fact_key=fact_key,
                fact_value=fact_value,
                fact_type=fact_type,
                source_session=source_session,
            )
            db.session.add(row)

        db.session.commit()
