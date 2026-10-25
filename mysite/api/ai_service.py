from __future__ import annotations

import asyncio
import hashlib
import json
import logging
import os
import random
import threading
import time
from dataclasses import dataclass, field
from datetime import datetime, timedelta
from enum import Enum
from typing import (
    Any,
    AsyncGenerator,
    Callable,
    Dict,
    Generator,
    List,
    Literal,
    Optional,
    Protocol,
    Tuple,
    Union,
)

import requests

logger = logging.getLogger(__name__)

# ---------------------------------------------------------------------------
# 2026 Professional Configuration
# ---------------------------------------------------------------------------

class ModelTier(Enum):
    PRIMARY = "primary"
    FALLBACK = "fallback"
    SECONDARY = "secondary"


@dataclass(frozen=True)
class ModelConfig:
    id: str
    tier: ModelTier
    rpm: int = 20
    timeout: int = 60
    max_tokens: int = 2048
    cost_per_1k_input: float = 0.0
    cost_per_1k_output: float = 0.0
    supports_vision: bool = False
    supports_tools: bool = False
    supports_reasoning: bool = False
    supports_structured: bool = False
    context_window: int = 128000


# Default model chain with metadata — replace "openrouter/free" with real IDs
DEFAULT_MODELS: Tuple[ModelConfig, ...] = (
    ModelConfig(
        id="anthropic/claude-3.5-sonnet",
        tier=ModelTier.PRIMARY,
        rpm=30,
        cost_per_1k_input=0.003,
        cost_per_1k_output=0.015,
        supports_vision=True,
        supports_tools=True,
        supports_structured=True,
        context_window=200000,
    ),
    ModelConfig(
        id="openai/gpt-4o-mini",
        tier=ModelTier.FALLBACK,
        rpm=60,
        cost_per_1k_input=0.00015,
        cost_per_1k_output=0.0006,
        supports_vision=True,
        supports_tools=True,
        supports_structured=True,
        context_window=128000,
    ),
    ModelConfig(
        id="google/gemini-2.0-flash-exp:free",
        tier=ModelTier.SECONDARY,
        rpm=20,
        cost_per_1k_input=0.0,
        cost_per_1k_output=0.0,
        supports_vision=True,
        supports_tools=True,
        supports_structured=True,
        context_window=1000000,
    ),
    ModelConfig(
        id="openrouter/free",
        tier=ModelTier.SECONDARY,
        rpm=20,
        cost_per_1k_input=0.0,
        cost_per_1k_output=0.0,
        supports_vision=True,
        supports_tools=True,
        supports_structured=True,
        context_window=1000000,
    ),
)

BASE_URL = "https://openrouter.ai/api/v1"
TIMEOUT = 60
MAX_RETRY = 3
RETRY_DELAY = 1.5
JITTER_MAX = 0.5

_CACHE_TTL = 300
_CACHE_MAX = 500

# Circuit breaker thresholds
CB_FAILURE_THRESHOLD = 5
CB_RECOVERY_TIMEOUT = 60  # seconds
CB_HALF_OPEN_MAX = 2

# ---------------------------------------------------------------------------
# Protocols & Types
# ---------------------------------------------------------------------------

class Interceptor(Protocol):
    """Pre-request / post-response interceptor protocol."""

    def before_request(self, request: Dict[str, Any]) -> Dict[str, Any]: ...
    def after_response(self, response: Dict[str, Any], meta: Dict[str, Any]) -> Dict[str, Any]: ...


class ToolCallable(Protocol):
    """Signature for tool functions."""

    def __call__(self, **kwargs: Any) -> Any: ...


# ---------------------------------------------------------------------------
# Circuit Breaker
# ---------------------------------------------------------------------------

class CircuitState(Enum):
    CLOSED = "closed"
    OPEN = "open"
    HALF_OPEN = "half_open"


class CircuitBreaker:
    """Per-model circuit breaker with half-open recovery."""

    def __init__(self, threshold: int = CB_FAILURE_THRESHOLD, recovery: int = CB_RECOVERY_TIMEOUT):
        self.threshold = threshold
        self.recovery = recovery
        self._failures = 0
        self._last_failure = 0.0
        self._state = CircuitState.CLOSED
        self._half_open_attempts = 0
        self._lock = threading.Lock()

    @property
    def state(self) -> CircuitState:
        with self._lock:
            if self._state == CircuitState.OPEN:
                if time.monotonic() - self._last_failure >= self.recovery:
                    self._state = CircuitState.HALF_OPEN
                    self._half_open_attempts = 0
                    logger.info("Circuit breaker half-open")
            return self._state

    def record_success(self) -> None:
        with self._lock:
            self._failures = 0
            if self._state == CircuitState.HALF_OPEN:
                self._half_open_attempts += 1
                if self._half_open_attempts >= CB_HALF_OPEN_MAX:
                    self._state = CircuitState.CLOSED
                    logger.info("Circuit breaker closed")
            else:
                self._state = CircuitState.CLOSED

    def record_failure(self) -> None:
        with self._lock:
            self._failures += 1
            self._last_failure = time.monotonic()
            if self._state == CircuitState.HALF_OPEN:
                self._state = CircuitState.OPEN
                logger.warning("Circuit breaker opened (half-open failure)")
            elif self._failures >= self.threshold:
                self._state = CircuitState.OPEN
                logger.warning("Circuit breaker opened after %d failures", self._failures)

    def can_execute(self) -> bool:
        state = self.state
        if state == CircuitState.CLOSED:
            return True
        if state == CircuitState.HALF_OPEN and self._half_open_attempts < CB_HALF_OPEN_MAX:
            return True
        return False


# ---------------------------------------------------------------------------
# Retry Policy with Exponential Backoff + Jitter
# ---------------------------------------------------------------------------

@dataclass
class RetryPolicy:
    max_attempts: int = MAX_RETRY + 1
    base_delay: float = RETRY_DELAY
    max_delay: float = 30.0
    jitter: bool = True
    retry_on: Tuple[int, ...] = (429, 502, 503, 504)

    def delay(self, attempt: int) -> float:
        exp = self.base_delay * (2 ** attempt)
        capped = min(exp, self.max_delay)
        if self.jitter:
            capped += random.uniform(0, JITTER_MAX)
        return capped

    def should_retry(self, status_code: Optional[int], exception: Optional[Exception]) -> bool:
        if exception is not None:
            return isinstance(exception, (requests.Timeout, requests.ConnectionError))
        if status_code is not None:
            return status_code in self.retry_on
        return False


# ---------------------------------------------------------------------------
# Cost Tracker
# ---------------------------------------------------------------------------

@dataclass
class CostSnapshot:
    model_id: str
    input_tokens: int = 0
    output_tokens: int = 0
    total_tokens: int = 0
    cost_usd: float = 0.0
    timestamp: datetime = field(default_factory=datetime.utcnow)

    def to_dict(self) -> Dict[str, Any]:
        return {
            "model_id": self.model_id,
            "input_tokens": self.input_tokens,
            "output_tokens": self.output_tokens,
            "total_tokens": self.total_tokens,
            "cost_usd": round(self.cost_usd, 6),
            "timestamp": self.timestamp.isoformat(),
        }


class CostTracker:
    """Thread-safe cost accumulator with rolling history."""

    def __init__(self, max_history: int = 1000):
        self._history: List[CostSnapshot] = []
        self._max_history = max_history
        self._lock = threading.Lock()
        self._total_usd = 0.0

    def record(self, snapshot: CostSnapshot) -> None:
        with self._lock:
            self._history.append(snapshot)
            self._total_usd += snapshot.cost_usd
            if len(self._history) > self._max_history:
                self._history.pop(0)

    def summary(self) -> Dict[str, Any]:
        with self._lock:
            if not self._history:
                return {"total_usd": 0.0, "calls": 0, "models": {}}
            models: Dict[str, Dict[str, Any]] = {}
            for s in self._history:
                if s.model_id not in models:
                    models[s.model_id] = {"calls": 0, "tokens": 0, "cost": 0.0}
                models[s.model_id]["calls"] += 1
                models[s.model_id]["tokens"] += s.total_tokens
                models[s.model_id]["cost"] += s.cost_usd
            return {
                "total_usd": round(self._total_usd, 6),
                "calls": len(self._history),
                "models": models,
                "last_24h": round(
                    sum(s.cost_usd for s in self._history if s.timestamp > datetime.utcnow() - timedelta(hours=24)),
                    6,
                ),
            }


# ---------------------------------------------------------------------------
# Token Estimator (approximate)
# ---------------------------------------------------------------------------

class TokenEstimator:
    """Rough estimation using 4 chars ≈ 1 token heuristic."""

    @staticmethod
    def count(text: str) -> int:
        return max(1, len(text) // 4)

    @staticmethod
    def estimate_cost(
        model: ModelConfig, input_text: str, output_text: str
    ) -> Tuple[int, int, float]:
        inp = TokenEstimator.count(input_text)
        out = TokenEstimator.count(output_text)
        cost = (inp / 1000 * model.cost_per_1k_input) + (out / 1000 * model.cost_per_1k_output)
        return inp, out, cost


# ---------------------------------------------------------------------------
# Metrics Collector
# ---------------------------------------------------------------------------

class MetricsCollector:
    """Lightweight in-memory metrics for observability."""

    def __init__(self):
        self._counters: Dict[str, int] = {}
        self._timers: Dict[str, List[float]] = {}
        self._lock = threading.Lock()

    def increment(self, metric: str, value: int = 1) -> None:
        with self._lock:
            self._counters[metric] = self._counters.get(metric, 0) + value

    def timer(self, metric: str, duration_ms: float) -> None:
        with self._lock:
            if metric not in self._timers:
                self._timers[metric] = []
            self._timers[metric].append(duration_ms)
            if len(self._timers[metric]) > 1000:
                self._timers[metric] = self._timers[metric][-500:]

    def snapshot(self) -> Dict[str, Any]:
        with self._lock:
            timers = {}
            for k, v in self._timers.items():
                if v:
                    timers[k] = {
                        "count": len(v),
                        "avg_ms": round(sum(v) / len(v), 2),
                        "p95_ms": round(sorted(v)[int(len(v) * 0.95)], 2) if len(v) > 1 else v[0],
                    }
            return {"counters": dict(self._counters), "timers": timers}


# ---------------------------------------------------------------------------
# Enhanced TTL Cache
# ---------------------------------------------------------------------------

class _TTLCache:
    def __init__(self, maxsize: int = 500, ttl: int = 300):
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

    def clear(self) -> None:
        with self._lock:
            self._store.clear()

    def __len__(self) -> int:
        with self._lock:
            return len(self._store)


# ---------------------------------------------------------------------------
# Enhanced Rate Limiter (Token Bucket with Burst)
# ---------------------------------------------------------------------------

class _RateLimiter:
    def __init__(self, rpm: int = 20, burst: int = 5):
        self._rpm = rpm
        self._burst = burst
        self._tokens: Dict[str, float] = {}
        self._last: Dict[str, float] = {}
        self._lock = threading.Lock()

    def is_allowed(self, key: str) -> bool:
        with self._lock:
            now = time.monotonic()
            last = self._last.get(key, now)
            elapsed = now - last
            tokens = min(self._rpm + self._burst, self._tokens.get(key, self._rpm) + elapsed * (self._rpm / 60.0))
            self._last[key] = now
            if tokens >= 1:
                self._tokens[key] = tokens - 1
                return True
            self._tokens[key] = tokens
            return False

    def remaining(self, key: str) -> int:
        with self._lock:
            now = time.monotonic()
            last = self._last.get(key, now)
            elapsed = now - last
            tokens = min(self._rpm + self._burst, self._tokens.get(key, self._rpm) + elapsed * (self._rpm / 60.0))
            return int(max(0, tokens))


# ---------------------------------------------------------------------------
# Session Memory (2026 Pro: persisted to DB, see api/chat_memory.py)
# ---------------------------------------------------------------------------
# Previously an in-memory-only TTLCache (30 min, max 12 turns) — history was
# lost on every restart. Now backed by chat_conversations/chat_messages so
# it survives restarts and page reloads, with the same in-memory-cache-first
# read path for speed. CustomerMemory (long-term facts keyed by phone) lives
# in the same module and is used by api/customer_chat.py.
try:
    from api.chat_memory import SessionMemory, CustomerMemory  # noqa: F401
except ImportError:
    # Standalone fallback (e.g. module imported outside the Flask app package
    # layout) — keep the service usable with a pure in-memory cache.
    class SessionMemory:  # type: ignore
        MAX_HISTORY = 24
        try:
            from cachetools import TTLCache as _CacheTool

            _store = _CacheTool(maxsize=3000, ttl=1800)
        except ImportError:
            _store = {}  # type: ignore

        @classmethod
        def get(cls, session_id: str) -> List[Dict]:
            return list(cls._store.get(str(session_id), []))

        @classmethod
        def append(cls, session_id: str, role: str, content: str, **meta: Any) -> None:
            key = str(session_id)
            history = list(cls._store.get(key, []))
            history.append({"role": role, "content": content})
            cls._store[key] = history[-cls.MAX_HISTORY:]

        @classmethod
        def clear(cls, session_id: str) -> None:
            cls._store.pop(str(session_id), None)

        @classmethod
        def get_full_history(cls, session_id: str) -> List[Dict]:
            return cls.get(session_id)


# ---------------------------------------------------------------------------
# Response Models
# ---------------------------------------------------------------------------

@dataclass
class ChatResponse:
    success: bool
    reply: str
    model: str = ""
    cached: bool = False
    cost: Optional[CostSnapshot] = None
    raw: Optional[Dict[str, Any]] = None
    tool_calls: Optional[List[Dict[str, Any]]] = None
    reasoning: Optional[str] = None
    error: Optional[str] = None

    def to_dict(self) -> Dict[str, Any]:
        d = {
            "success": self.success,
            "reply": self.reply,
            "model": self.model,
            "cached": self.cached,
            "error": self.error,
        }
        if self.cost:
            d["cost"] = self.cost.to_dict()
        if self.tool_calls:
            d["tool_calls"] = self.tool_calls
        if self.reasoning:
            d["reasoning"] = self.reasoning
        return d


# ---------------------------------------------------------------------------
# OpenRouter AI Service — 2026 Professional Edition
# ---------------------------------------------------------------------------

class OpenRouterService:
    """
    Thread-safe singleton OpenRouter client with:
      • Circuit breaker failover across model chain
      • Exponential backoff + jitter retry
      • Cost tracking & token estimation
      • Structured outputs (JSON schema)
      • Tool calling with auto-execution
      • Multimodal (vision) support
      • Reasoning tokens extraction
      • Request/response interceptors
      • Async streaming
      • Health monitoring & metrics
    """

    _instance: Optional["OpenRouterService"] = None
    _lock = threading.Lock()

    def __new__(cls) -> "OpenRouterService":
        with cls._lock:
            if cls._instance is None:
                cls._instance = super().__new__(cls)
                cls._instance._init()
            return cls._instance

    def _init(self) -> None:
        self._response_cache = _TTLCache(maxsize=_CACHE_MAX, ttl=_CACHE_TTL)
        self._rate_limiter = _RateLimiter(rpm=20, burst=5)
        self._session = requests.Session()
        self._session.headers.update(self._default_headers())

        self._models: Tuple[ModelConfig, ...] = DEFAULT_MODELS
        self._breakers: Dict[str, CircuitBreaker] = {m.id: CircuitBreaker() for m in self._models}
        self._retry_policy = RetryPolicy()
        self._cost_tracker = CostTracker()
        self._metrics = MetricsCollector()
        self._interceptors: List[Interceptor] = []
        self._tools: Dict[str, Callable] = {}

        # Model health scoring (0-1)
        self._health_scores: Dict[str, float] = {m.id: 1.0 for m in self._models}
        self._model_stats: Dict[str, Dict] = {m.id: {"errors": 0, "ok": 0, "latency_ms": []} for m in self._models}

        logger.info("OpenRouterService 2026 Pro Edition initialised (%d models)", len(self._models))

    # ------------------------------------------------------------------
    # Config helpers
    # ------------------------------------------------------------------

    def _default_headers(self) -> Dict[str, str]:
        return {
            "Content-Type": "application/json",
            "HTTP-Referer": os.getenv("OPENROUTER_SITE_URL", "https://pusatgadaiidn.pythonanywhere.com"),
            "X-Title": os.getenv("OPENROUTER_APP_NAME", "Pusat Gadai Indonesia"),
        }

    def _api_key(self) -> str:
        try:
            from models import Setting

            s = Setting.query.filter_by(key="openrouter_api_key").first()
            if s and s.value:
                return s.value.strip()
        except Exception:
            pass
        return os.getenv("OPENROUTER_API_KEY", "").strip()

    def _base_url(self) -> str:
        try:
            from models import Setting

            s = Setting.query.filter_by(key="openrouter_base_url").first()
            if s and s.value:
                return s.value.rstrip("/")
        except Exception:
            pass
        return os.getenv("OPENROUTER_BASE_URL", BASE_URL).rstrip("/")

    # ------------------------------------------------------------------
    # Interceptors & Tools
    # ------------------------------------------------------------------

    def add_interceptor(self, interceptor: Interceptor) -> None:
        self._interceptors.append(interceptor)

    def register_tool(self, name: str, fn: Callable[..., Any]) -> None:
        self._tools[name] = fn
        logger.info("Registered tool: %s", name)

    def _run_interceptors_before(self, request: Dict[str, Any]) -> Dict[str, Any]:
        for i in self._interceptors:
            try:
                request = i.before_request(request)
            except Exception as exc:
                logger.warning("Interceptor before_request error: %s", exc)
        return request

    def _run_interceptors_after(self, response: Dict[str, Any], meta: Dict[str, Any]) -> Dict[str, Any]:
        for i in self._interceptors:
            try:
                response = i.after_response(response, meta)
            except Exception as exc:
                logger.warning("Interceptor after_response error: %s", exc)
        return response

    # ------------------------------------------------------------------
    # Model selection
    # ------------------------------------------------------------------

    def _clean_model_id(self, model_id: str) -> str:
        """Bersihkan model ID dari prefix ganda."""
        if not model_id:
            return self._models[0].id if self._models else "openrouter/auto"
        # Hapus prefix ganda
        if model_id.startswith('openrouter/openrouter/'):
            return model_id.replace('openrouter/openrouter/', 'openrouter/', 1)
        if model_id.startswith('openrouter/anthropic/'):
            return model_id.replace('openrouter/anthropic/', 'anthropic/', 1)
        if model_id.startswith('openrouter/openai/'):
            return model_id.replace('openrouter/openai/', 'openai/', 1)
        if model_id.startswith('openrouter/google/'):
            return model_id.replace('openrouter/google/', 'google/', 1)
        if model_id.startswith('openrouter/meta-llama/'):
            return model_id.replace('openrouter/meta-llama/', 'meta-llama/', 1)
        if model_id.startswith('openrouter/deepseek/'):
            return model_id.replace('openrouter/deepseek/', 'deepseek/', 1)
        if model_id.startswith('openrouter/mistralai/'):
            return model_id.replace('openrouter/mistralai/', 'mistralai/', 1)
        return model_id

    def _active_models(self, need_vision: bool = False, need_tools: bool = False, need_structured: bool = False) -> List[ModelConfig]:
        """Return ranked model chain, filtering by capability and health."""
        eligible = []
        for m in self._models:
            if need_vision and not m.supports_vision:
                continue
            if need_tools and not m.supports_tools:
                continue
            if need_structured and not m.supports_structured:
                continue
            if not self._breakers[m.id].can_execute():
                continue
            eligible.append(m)

        # Sort by health score (desc), then errors (asc), then tier priority
        def sort_key(m: ModelConfig) -> Tuple[float, int, int]:
            tier_order = {ModelTier.PRIMARY: 0, ModelTier.FALLBACK: 1, ModelTier.SECONDARY: 2}
            health = self._health_scores.get(m.id, 0.0)
            errors = self._model_stats[m.id]["errors"]
            return (-health, errors, tier_order.get(m.tier, 99))

        eligible.sort(key=sort_key)
        return eligible

    def _update_health(self, model_id: str, success: bool, latency_ms: float) -> None:
        stats = self._model_stats[model_id]
        if success:
            stats["ok"] += 1
            self._breakers[model_id].record_success()
        else:
            stats["errors"] += 1
            self._breakers[model_id].record_failure()

        latencies = stats["latency_ms"]
        latencies.append(latency_ms)
        latencies[:] = latencies[-100:]

        # Health score: blend of success rate and latency percentile
        total = stats["ok"] + stats["errors"]
        if total > 0:
            success_rate = stats["ok"] / total
            avg_latency = sum(latencies) / len(latencies) if latencies else 0
            latency_penalty = min(1.0, avg_latency / 10000)  # 10s = full penalty
            self._health_scores[model_id] = max(0.0, success_rate * (1.0 - latency_penalty))

    # ------------------------------------------------------------------
    # Cache key
    # ------------------------------------------------------------------

    def _cache_key(self, messages: List[Dict], extras: Optional[str] = None) -> str:
        """Cache key SENGAJA tidak menyertakan model id. Kalau disertakan,
        key ini harus identik antara saat lookup (sebelum tahu model mana
        yang akan menjawab — bisa primary/fallback/secondary tergantung
        health & circuit breaker saat itu) dan saat store (setelah tahu
        model mana yang benar-benar menjawab). Sebelumnya lookup selalu
        memakai self._models[0].id (model pertama, hardcoded) sementara
        store memakai model.id aktual — begitu failover terjadi (justru
        saat caching paling berguna, karena model utama sedang bermasalah)
        kedua key itu berbeda dan cache SELALU miss. Membuatnya model-
        agnostic menghilangkan bug ini sekaligus menaikkan hit-rate."""
        raw = json.dumps({"msgs": messages, "x": extras}, sort_keys=True)
        return hashlib.sha256(raw.encode()).hexdigest()[:32]

    # ------------------------------------------------------------------
    # Internal request builder
    # ------------------------------------------------------------------

    def _build_payload(
        self,
        model: ModelConfig,
        messages: List[Dict],
        temperature: float,
        max_tokens: int,
        stream: bool = False,
        response_format: Optional[Dict[str, Any]] = None,
        tools: Optional[List[Dict[str, Any]]] = None,
        reasoning: Optional[Dict[str, Any]] = None,
    ) -> Dict[str, Any]:
        body: Dict[str, Any] = {
            "model": self._clean_model_id(model.id),
            "messages": messages,
            "temperature": temperature,
            "max_tokens": max_tokens,
            "stream": stream,
        }
        if response_format:
            body["response_format"] = response_format
        if tools:
            body["tools"] = tools
            body["tool_choice"] = "auto"
        if reasoning and model.supports_reasoning:
            body["reasoning"] = reasoning
        return body

    def _build_headers(self, api_key: str) -> Dict[str, str]:
        headers = {
            "Authorization": f"Bearer {api_key}",
            "Content-Type": "application/json",
        }
        headers.update(self._default_headers())
        return headers

    # ------------------------------------------------------------------
    # Core completion — 2026 Pro Edition
    # ------------------------------------------------------------------

    def chat_completion(
        self,
        messages: List[Dict],
        *,
        temperature: float = 0.65,
        max_tokens: int = 700,
        use_cache: bool = True,
        rate_key: Optional[str] = None,
        response_format: Optional[Dict[str, Any]] = None,
        tools: Optional[List[Dict[str, Any]]] = None,
        execute_tools: bool = False,
        reasoning: Optional[Dict[str, Any]] = None,
        max_tool_hops: int = 3,
        _tool_hop: int = 0,
    ) -> ChatResponse:
        """
        Send chat completion with automatic failover, circuit breakers,
        cost tracking, structured output, tool calling, and reasoning.

        max_tool_hops: maksimum berapa kali AI boleh merangkai tool calls
        secara berturut-turut dalam satu permintaan (mis. cek_order_pelanggan
        lalu cek_resi) sebelum dipaksa memberi jawaban akhir. _tool_hop adalah
        counter internal untuk rekursi, jangan diisi manual dari luar.
        """
        api_key = self._api_key()
        if not api_key:
            return ChatResponse(
                success=False,
                reply="Maaf, informasi tersebut tidak tersedia pada sistem kami.",
                error="API key tidak dikonfigurasi.",
            )

        # Rate limiting
        if rate_key and not self._rate_limiter.is_allowed(rate_key):
            self._metrics.increment("rate_limited")
            return ChatResponse(
                success=False,
                reply="⚠️ Terlalu banyak permintaan. Mohon tunggu sebentar.",
                error="rate_limit",
            )

        need_vision = any(
            isinstance(m.get("content"), list) and any(
                c.get("type") == "image_url" for c in m["content"] if isinstance(c, dict)
            )
            for m in messages
        )
        need_tools = bool(tools)
        need_structured = bool(response_format)

        # Cache lookup. Extras mencakup temperature/max_tokens supaya request
        # dengan pesan sama tapi parameter sampling berbeda tidak saling
        # bertabrakan di cache.
        cache_extra = json.dumps(
            {"temp": temperature, "max_tok": max_tokens, "rf": response_format, "t": tools},
            sort_keys=True,
        )
        if use_cache:
            ck = self._cache_key(messages, cache_extra)
            cached = self._response_cache.get(ck)
            if cached:
                logger.debug("Cache hit for key %s", ck[:8])
                cached.cached = True
                self._metrics.increment("cache_hit")
                return cached

        headers = self._build_headers(api_key)
        url = f"{self._base_url()}/chat/completions"

        candidates = self._active_models(need_vision=need_vision, need_tools=need_tools, need_structured=need_structured)

        for model in candidates:
            for attempt in range(self._retry_policy.max_attempts):
                start = time.monotonic()
                try:
                    body = self._build_payload(
                        model, messages, temperature, max_tokens,
                        stream=False, response_format=response_format, tools=tools, reasoning=reasoning,
                    )
                    body = self._run_interceptors_before(body)

                    resp = self._session.post(url, headers=headers, json=body, timeout=TIMEOUT)
                    latency_ms = (time.monotonic() - start) * 1000

                    if resp.status_code == 200:
                        data = resp.json()
                        choices = data.get("choices") or []
                        if not choices:
                            continue

                        message = choices[0].get("message") or {}
                        reply = message.get("content", "") or ""
                        reply = reply.strip()

                        # Extract reasoning if present
                        reasoning_text = None
                        if model.supports_reasoning:
                            reasoning_text = message.get("reasoning") or (choices[0].get("reasoning") if isinstance(choices[0], dict) else None)

                        # Extract tool calls
                        tool_calls = message.get("tool_calls")

                        # Cost tracking
                        usage = data.get("usage", {})
                        inp_tok = usage.get("prompt_tokens", 0)
                        out_tok = usage.get("completion_tokens", 0)
                        if not inp_tok:
                            # fallback estimation
                            all_text = " ".join(
                                str(m.get("content", "")) for m in messages
                            ) + " " + reply
                            inp_tok, out_tok, _ = TokenEstimator.estimate_cost(model, all_text, reply)
                        cost_usd = (inp_tok / 1000 * model.cost_per_1k_input) + (out_tok / 1000 * model.cost_per_1k_output)
                        cost_snap = CostSnapshot(
                            model_id=model.id,
                            input_tokens=inp_tok,
                            output_tokens=out_tok,
                            total_tokens=inp_tok + out_tok,
                            cost_usd=cost_usd,
                        )
                        self._cost_tracker.record(cost_snap)

                        # Auto-execute tools if requested (supports multi-hop chaining,
                        # e.g. cek_order_pelanggan -> cek_resi, up to max_tool_hops)
                        if execute_tools and tool_calls:
                            tool_results = self._execute_tool_calls(tool_calls)
                            new_messages = list(messages)
                            new_messages.append({"role": "assistant", "content": reply, "tool_calls": tool_calls})
                            for tr in tool_results:
                                new_messages.append({"role": "tool", "tool_call_id": tr["id"], "content": str(tr["result"])})
                            next_hop = _tool_hop + 1
                            return self.chat_completion(
                                new_messages,
                                temperature=temperature,
                                max_tokens=max_tokens,
                                use_cache=False,
                                rate_key=rate_key,
                                response_format=response_format,
                                tools=tools,
                                # Izinkan tool call lagi selama belum mencapai batas hop,
                                # supaya AI bisa merangkai beberapa pengecekan sekaligus.
                                execute_tools=next_hop < max_tool_hops,
                                reasoning=reasoning,
                                max_tool_hops=max_tool_hops,
                                _tool_hop=next_hop,
                            )

                        result = ChatResponse(
                            success=True,
                            reply=reply,
                            model=model.id,
                            cached=False,
                            cost=cost_snap,
                            raw=data,
                            tool_calls=tool_calls,
                            reasoning=reasoning_text,
                        )

                        self._update_health(model.id, success=True, latency_ms=latency_ms)
                        self._metrics.increment("request_success")
                        self._metrics.timer("request_latency", latency_ms)

                        if use_cache:
                            ck = self._cache_key(messages, cache_extra)
                            self._response_cache.set(ck, result)

                        result = ChatResponse(
                            success=result.success,
                            reply=result.reply,
                            model=result.model,
                            cached=result.cached,
                            cost=result.cost,
                            raw=result.raw,
                            tool_calls=result.tool_calls,
                            reasoning=result.reasoning,
                        )
                        return result

                    elif resp.status_code == 429:
                        logger.warning("Rate limited by OpenRouter on model %s", model.id)
                        self._update_health(model.id, success=False, latency_ms=latency_ms)
                        self._metrics.increment("rate_limit_upstream")
                        break

                    elif resp.status_code in (401, 403):
                        logger.error("Auth error %d on model %s", resp.status_code, model.id)
                        return ChatResponse(
                            success=False,
                            reply="Maaf, informasi tersebut tidak tersedia pada sistem kami.",
                            error=f"auth_error_{resp.status_code}",
                        )

                    else:
                        self._update_health(model.id, success=False, latency_ms=latency_ms)
                        self._metrics.increment("http_error")
                        logger.warning("HTTP %d from model %s attempt %d", resp.status_code, model.id, attempt)
                        if self._retry_policy.should_retry(resp.status_code, None):
                            delay = self._retry_policy.delay(attempt)
                            time.sleep(delay)
                            continue
                        break

                except requests.Timeout:
                    self._update_health(model.id, success=False, latency_ms=(time.monotonic() - start) * 1000)
                    self._metrics.increment("timeout")
                    logger.warning("Timeout model=%s attempt=%d", model.id, attempt)
                    if self._retry_policy.should_retry(None, requests.Timeout()):
                        time.sleep(self._retry_policy.delay(attempt))
                        continue
                    break

                except requests.ConnectionError:
                    self._update_health(model.id, success=False, latency_ms=(time.monotonic() - start) * 1000)
                    self._metrics.increment("connection_error")
                    logger.warning("ConnectionError model=%s attempt=%d", model.id, attempt)
                    break

                except Exception as exc:
                    self._update_health(model.id, success=False, latency_ms=(time.monotonic() - start) * 1000)
                    self._metrics.increment("unexpected_error")
                    logger.error("Unexpected error model=%s: %s", model.id, exc, exc_info=True)
                    if self._retry_policy.should_retry(None, exc):
                        time.sleep(self._retry_policy.delay(attempt))
                        continue
                    break

        logger.error("All models failed for chat_completion request")
        self._metrics.increment("all_models_failed")
        return ChatResponse(
            success=False,
            reply="Maaf, layanan AI sedang tidak tersedia. Silakan coba lagi.",
            error="all_models_failed",
        )

    def _execute_tool_calls(self, tool_calls: List[Dict[str, Any]]) -> List[Dict[str, Any]]:
        results = []
        for tc in tool_calls:
            fn = tc.get("function", {})
            name = fn.get("name")
            try:
                args = json.loads(fn.get("arguments", "{}"))
            except json.JSONDecodeError:
                args = {}
            handler = self._tools.get(name)
            if handler:
                try:
                    result = handler(**args)
                except Exception as exc:
                    result = f"Error executing tool {name}: {exc}"
                    logger.error("Tool execution error: %s", exc)
            else:
                result = f"Tool {name} not found."
            results.append({"id": tc.get("id"), "name": name, "result": result})
        return results

    # ------------------------------------------------------------------
    # Streaming (sync generator) — Enhanced SSE parsing
    # ------------------------------------------------------------------

    def stream_completion(
        self,
        messages: List[Dict],
        *,
        temperature: float = 0.65,
        max_tokens: int = 700,
        response_format: Optional[Dict[str, Any]] = None,
        tools: Optional[List[Dict[str, Any]]] = None,
        reasoning: Optional[Dict[str, Any]] = None,
    ) -> Generator[str, None, None]:
        """
        Generator that yields text chunks for streaming responses.
        Enhanced with circuit breaker awareness and better SSE parsing.
        """
        api_key = self._api_key()
        if not api_key:
            yield "Maaf, informasi tersebut tidak tersedia pada sistem kami."
            return

        headers = self._build_headers(api_key)
        url = f"{self._base_url()}/chat/completions"

        candidates = self._active_models()

        for model in candidates:
            start = time.monotonic()
            try:
                body = self._build_payload(
                    model, messages, temperature, max_tokens,
                    stream=True, response_format=response_format, tools=tools, reasoning=reasoning,
                )
                body = self._run_interceptors_before(body)

                with self._session.post(url, headers=headers, json=body, timeout=TIMEOUT, stream=True) as resp:
                    latency_ms = (time.monotonic() - start) * 1000
                    if resp.status_code != 200:
                        self._update_health(model.id, success=False, latency_ms=latency_ms)
                        continue

                    self._update_health(model.id, success=True, latency_ms=latency_ms)
                    self._metrics.increment("stream_success")

                    for line in resp.iter_lines():
                        if not line:
                            continue
                        try:
                            line_str = line.decode("utf-8", errors="ignore")
                        except UnicodeDecodeError:
                            continue

                        if not line_str.startswith("data: "):
                            continue
                        payload = line_str[6:]
                        if payload.strip() == "[DONE]":
                            return

                        try:
                            chunk = json.loads(payload)
                        except json.JSONDecodeError:
                            continue

                        choices = chunk.get("choices") or []
                        if not choices:
                            continue

                        delta = choices[0].get("delta", {})
                        content = delta.get("content") or ""
                        if content:
                            yield content

                        # Yield reasoning tokens if present
                        if model.supports_reasoning:
                            reasoning_delta = delta.get("reasoning") or ""
                            if reasoning_delta:
                                yield f"\n[REASONING]{reasoning_delta}[/REASONING]\n"

                    return

            except Exception as exc:
                self._update_health(model.id, success=False, latency_ms=(time.monotonic() - start) * 1000)
                self._metrics.increment("stream_error")
                logger.warning("Stream error model=%s: %s", model.id, exc)
                continue

        yield "Maaf, layanan AI sedang tidak tersedia."

    # ------------------------------------------------------------------
    # Async support (optional, requires aiohttp)
    # ------------------------------------------------------------------

    async def achat_completion(
        self,
        messages: List[Dict],
        *,
        temperature: float = 0.65,
        max_tokens: int = 700,
        use_cache: bool = True,
        rate_key: Optional[str] = None,
        response_format: Optional[Dict[str, Any]] = None,
        tools: Optional[List[Dict[str, Any]]] = None,
        execute_tools: bool = False,
        reasoning: Optional[Dict[str, Any]] = None,
        max_tool_hops: int = 3,
        _tool_hop: int = 0,
    ) -> ChatResponse:
        """Async version of chat_completion. See chat_completion for max_tool_hops."""
        try:
            import aiohttp
        except ImportError:
            logger.warning("aiohttp not installed; falling back to sync in thread")
            loop = asyncio.get_event_loop()
            return await loop.run_in_executor(
                None,
                lambda: self.chat_completion(
                    messages,
                    temperature=temperature,
                    max_tokens=max_tokens,
                    use_cache=use_cache,
                    rate_key=rate_key,
                    response_format=response_format,
                    tools=tools,
                    execute_tools=execute_tools,
                    reasoning=reasoning,
                    max_tool_hops=max_tool_hops,
                    _tool_hop=_tool_hop,
                ),
            )

        api_key = self._api_key()
        if not api_key:
            return ChatResponse(
                success=False,
                reply="Maaf, informasi tersebut tidak tersedia pada sistem kami.",
                error="API key tidak dikonfigurasi.",
            )

        if rate_key and not self._rate_limiter.is_allowed(rate_key):
            return ChatResponse(
                success=False,
                reply="⚠️ Terlalu banyak permintaan. Mohon tunggu sebentar.",
                error="rate_limit",
            )

        cache_extra = json.dumps(
            {"temp": temperature, "max_tok": max_tokens, "rf": response_format, "t": tools},
            sort_keys=True,
        )
        if use_cache:
            ck = self._cache_key(messages, cache_extra)
            cached = self._response_cache.get(ck)
            if cached:
                cached.cached = True
                return cached

        headers = self._build_headers(api_key)
        url = f"{self._base_url()}/chat/completions"
        candidates = self._active_models()

        async with aiohttp.ClientSession() as session:
            for model in candidates:
                for attempt in range(self._retry_policy.max_attempts):
                    start = time.monotonic()
                    try:
                        body = self._build_payload(
                            model, messages, temperature, max_tokens,
                            stream=False, response_format=response_format, tools=tools, reasoning=reasoning,
                        )
                        body = self._run_interceptors_before(body)

                        async with session.post(url, headers=headers, json=body, timeout=aiohttp.ClientTimeout(total=TIMEOUT)) as resp:
                            latency_ms = (time.monotonic() - start) * 1000
                            if resp.status == 200:
                                data = await resp.json()
                                choices = data.get("choices") or []
                                if not choices:
                                    continue

                                message = choices[0].get("message") or {}
                                reply = (message.get("content") or "").strip()
                                reasoning_text = message.get("reasoning") if model.supports_reasoning else None
                                tool_calls = message.get("tool_calls")

                                usage = data.get("usage", {})
                                inp_tok = usage.get("prompt_tokens", 0)
                                out_tok = usage.get("completion_tokens", 0)
                                cost_usd = (inp_tok / 1000 * model.cost_per_1k_input) + (out_tok / 1000 * model.cost_per_1k_output)
                                cost_snap = CostSnapshot(
                                    model_id=model.id,
                                    input_tokens=inp_tok,
                                    output_tokens=out_tok,
                                    total_tokens=inp_tok + out_tok,
                                    cost_usd=cost_usd,
                                )
                                self._cost_tracker.record(cost_snap)

                                if execute_tools and tool_calls:
                                    tool_results = self._execute_tool_calls(tool_calls)
                                    new_messages = list(messages)
                                    new_messages.append({"role": "assistant", "content": reply, "tool_calls": tool_calls})
                                    for tr in tool_results:
                                        new_messages.append({"role": "tool", "tool_call_id": tr["id"], "content": str(tr["result"])})
                                    next_hop = _tool_hop + 1
                                    return await self.achat_completion(
                                        new_messages,
                                        temperature=temperature,
                                        max_tokens=max_tokens,
                                        use_cache=False,
                                        rate_key=rate_key,
                                        response_format=response_format,
                                        tools=tools,
                                        execute_tools=next_hop < max_tool_hops,
                                        reasoning=reasoning,
                                        max_tool_hops=max_tool_hops,
                                        _tool_hop=next_hop,
                                    )

                                result = ChatResponse(
                                    success=True,
                                    reply=reply,
                                    model=model.id,
                                    cost=cost_snap,
                                    raw=data,
                                    tool_calls=tool_calls,
                                    reasoning=reasoning_text,
                                )
                                self._update_health(model.id, success=True, latency_ms=latency_ms)
                                if use_cache:
                                    ck = self._cache_key(messages, cache_extra)
                                    self._response_cache.set(ck, result)
                                return result

                            elif resp.status == 429:
                                self._update_health(model.id, success=False, latency_ms=latency_ms)
                                break
                            elif resp.status in (401, 403):
                                return ChatResponse(
                                    success=False,
                                    reply="Maaf, informasi tersebut tidak tersedia pada sistem kami.",
                                    error=f"auth_error_{resp.status}",
                                )
                            else:
                                self._update_health(model.id, success=False, latency_ms=latency_ms)
                                if self._retry_policy.should_retry(resp.status, None):
                                    await asyncio.sleep(self._retry_policy.delay(attempt))
                                    continue
                                break

                    except asyncio.TimeoutError:
                        self._update_health(model.id, success=False, latency_ms=(time.monotonic() - start) * 1000)
                        if self._retry_policy.should_retry(None, requests.Timeout()):
                            await asyncio.sleep(self._retry_policy.delay(attempt))
                            continue
                        break

                    except Exception as exc:
                        self._update_health(model.id, success=False, latency_ms=(time.monotonic() - start) * 1000)
                        if self._retry_policy.should_retry(None, exc):
                            await asyncio.sleep(self._retry_policy.delay(attempt))
                            continue
                        break

        return ChatResponse(
            success=False,
            reply="Maaf, layanan AI sedang tidak tersedia. Silakan coba lagi.",
            error="all_models_failed",
        )

    async def astream_completion(
        self,
        messages: List[Dict],
        *,
        temperature: float = 0.65,
        max_tokens: int = 700,
        response_format: Optional[Dict[str, Any]] = None,
        tools: Optional[List[Dict[str, Any]]] = None,
        reasoning: Optional[Dict[str, Any]] = None,
    ) -> AsyncGenerator[str, None]:
        """Async streaming generator."""
        try:
            import aiohttp
        except ImportError:
            loop = asyncio.get_event_loop()
            sync_gen = self.stream_completion(
                messages,
                temperature=temperature,
                max_tokens=max_tokens,
                response_format=response_format,
                tools=tools,
                reasoning=reasoning,
            )
            for chunk in sync_gen:
                yield chunk
            return

        api_key = self._api_key()
        if not api_key:
            yield "Maaf, informasi tersebut tidak tersedia pada sistem kami."
            return

        headers = self._build_headers(api_key)
        url = f"{self._base_url()}/chat/completions"
        candidates = self._active_models()

        async with aiohttp.ClientSession() as session:
            for model in candidates:
                try:
                    body = self._build_payload(
                        model, messages, temperature, max_tokens,
                        stream=True, response_format=response_format, tools=tools, reasoning=reasoning,
                    )
                    body = self._run_interceptors_before(body)

                    async with session.post(url, headers=headers, json=body, timeout=aiohttp.ClientTimeout(total=TIMEOUT)) as resp:
                        if resp.status != 200:
                            continue

                        async for line in resp.content:
                            if not line:
                                continue
                            line_str = line.decode("utf-8", errors="ignore")
                            if not line_str.startswith("data: "):
                                continue
                            payload = line_str[6:]
                            if payload.strip() == "[DONE]":
                                return
                            try:
                                chunk = json.loads(payload)
                            except json.JSONDecodeError:
                                continue
                            choices = chunk.get("choices") or []
                            if not choices:
                                continue
                            delta = choices[0].get("delta", {})
                            content = delta.get("content") or ""
                            if content:
                                yield content
                        return
                except Exception as exc:
                    logger.warning("Async stream error model=%s: %s", model.id, exc)
                    continue

        yield "Maaf, layanan AI sedang tidak tersedia."

    # ------------------------------------------------------------------
    # Convenience helpers
    # ------------------------------------------------------------------

    def structured_output(
        self,
        messages: List[Dict],
        schema: Dict[str, Any],
        *,
        temperature: float = 0.2,
        max_tokens: int = 2048,
        model: Optional[str] = None,
    ) -> ChatResponse:
        """Request JSON-structured output matching a schema."""
        response_format = {
            "type": "json_schema",
            "json_schema": {"name": "structured_response", "schema": schema, "strict": True},
        }
        if model:
            # Force specific model if provided
            forced = [m for m in self._models if m.id == model]
            if forced:
                # Temporarily override active models
                original = self._models
                self._models = tuple(forced)
                result = self.chat_completion(
                    messages,
                    temperature=temperature,
                    max_tokens=max_tokens,
                    response_format=response_format,
                )
                self._models = original
                return result
        return self.chat_completion(
            messages,
            temperature=temperature,
            max_tokens=max_tokens,
            response_format=response_format,
        )

    def generate_image_message(
        self, text: str, image_url: str, detail: str = "auto"
    ) -> Dict[str, Any]:
        """Build a multimodal message with image."""
        return {
            "role": "user",
            "content": [
                {"type": "text", "text": text},
                {"type": "image_url", "image_url": {"url": image_url, "detail": detail}},
            ],
        }

    # ------------------------------------------------------------------
    # Health / diagnostics
    # ------------------------------------------------------------------

    def health(self) -> Dict[str, Any]:
        api_key = self._api_key()
        return {
            "configured": bool(api_key),
            "models": [m.id for m in self._models],
            "model_configs": [
                {
                    "id": m.id,
                    "tier": m.tier.value,
                    "health_score": round(self._health_scores.get(m.id, 0.0), 3),
                    "circuit_state": self._breakers[m.id].state.value,
                    "stats": self._model_stats[m.id],
                    "supports_vision": m.supports_vision,
                    "supports_tools": m.supports_tools,
                    "supports_structured": m.supports_structured,
                    "supports_reasoning": m.supports_reasoning,
                }
                for m in self._models
            ],
            "cache_size": len(self._response_cache),
            "cost_summary": self._cost_tracker.summary(),
            "metrics": self._metrics.snapshot(),
        }

    def clear_cache(self) -> None:
        self._response_cache.clear()
        logger.info("Response cache cleared")

    def reset_health(self) -> None:
        for m in self._models:
            self._breakers[m.id] = CircuitBreaker()
            self._health_scores[m.id] = 1.0
            self._model_stats[m.id] = {"errors": 0, "ok": 0, "latency_ms": []}
        logger.info("Health scores and circuit breakers reset")


# ---------------------------------------------------------------------------
# Module-level singleton instance
# ---------------------------------------------------------------------------

_service: Optional[OpenRouterService] = None


def get_service() -> OpenRouterService:
    global _service
    if _service is None:
        _service = OpenRouterService()
    return _service


def chat(
    messages: List[Dict],
    *,
    temperature: float = 0.65,
    max_tokens: int = 700,
    use_cache: bool = True,
    rate_key: Optional[str] = None,
) -> Dict[str, Any]:
    """Convenience wrapper — returns dict for backward compatibility."""
    result = get_service().chat_completion(
        messages,
        temperature=temperature,
        max_tokens=max_tokens,
        use_cache=use_cache,
        rate_key=rate_key,
    )
    return result.to_dict()


# ---------------------------------------------------------------------------
# New convenience wrappers (2026 Pro)
# ---------------------------------------------------------------------------

def chat_structured(
    messages: List[Dict],
    schema: Dict[str, Any],
    *,
    temperature: float = 0.2,
    max_tokens: int = 2048,
) -> Dict[str, Any]:
    """Convenience wrapper for structured JSON output."""
    result = get_service().structured_output(
        messages, schema, temperature=temperature, max_tokens=max_tokens
    )
    return result.to_dict()


def chat_stream(
    messages: List[Dict],
    *,
    temperature: float = 0.65,
    max_tokens: int = 700,
) -> Generator[str, None, None]:
    """Convenience wrapper for streaming."""
    return get_service().stream_completion(
        messages, temperature=temperature, max_tokens=max_tokens
    )


async def achat(
    messages: List[Dict],
    *,
    temperature: float = 0.65,
    max_tokens: int = 700,
    use_cache: bool = True,
    rate_key: Optional[str] = None,
) -> Dict[str, Any]:
    """Async convenience wrapper."""
    result = await get_service().achat_completion(
        messages,
        temperature=temperature,
        max_tokens=max_tokens,
        use_cache=use_cache,
        rate_key=rate_key,
    )
    return result.to_dict()


async def achat_stream(
    messages: List[Dict],
    *,
    temperature: float = 0.65,
    max_tokens: int = 700,
) -> AsyncGenerator[str, None]:
    """Async streaming convenience wrapper."""
    async for chunk in get_service().astream_completion(
        messages, temperature=temperature, max_tokens=max_tokens
    ):
        yield chunk