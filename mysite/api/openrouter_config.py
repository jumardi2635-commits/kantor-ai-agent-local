"""
OpenRouter Configuration & Client — Python 2026 Professional Edition
=====================================================================

Production-ready Python client untuk OpenRouter API dengan fitur 2026:
- Streaming responses (SSE generator)
- Model fallback chains (auto-failover across providers)
- Structured outputs (JSON Schema enforcement)
- Reasoning tokens & thinking budgets
- Tool calling / function calling
- Retry dengan exponential backoff + jitter
- Circuit breaker pattern
- Token cost tracking & budget controls
- Request tracing & analytics
- Multimodal inputs (images, PDFs via URL/base64)
- Provider routing preferences (price, speed, ZDR)

Compatible dengan: Flask, FastAPI, Django, standalone scripts.

Usage:
    from api.openrouter_config import OpenRouterClient, get_default_client

    client = get_default_client()

    # Streaming
    for chunk in client.stream_chat("Hello"):
        print(chunk.content, end="")

    # Structured output
    result = client.structured_chat("Extract data", schema={...})

    # Tool calling
    result = client.chat_with_tools("Weather in Jakarta?", tools=[...])
"""

import json
import logging
import random
import time
from dataclasses import dataclass, field
from datetime import datetime, timedelta
from enum import Enum
from typing import (
    Any, Callable, Dict, Generator, Iterator, List, Optional, 
    Tuple, Union, Set
)

import requests

logger = logging.getLogger(__name__)


# =============================================================================
# EXCEPTIONS
# =============================================================================

class OpenRouterError(Exception):
    """Base exception untuk semua error OpenRouter."""

    def __init__(self, message: str, code: str = None, status_code: int = None, 
                 details: Dict = None):
        super().__init__(message)
        self.code = code or "UNKNOWN_ERROR"
        self.status_code = status_code
        self.details = details or {}
        self.timestamp = datetime.utcnow().isoformat()

    def to_dict(self) -> Dict:
        return {
            "name": self.__class__.__name__,
            "message": str(self),
            "code": self.code,
            "status_code": self.status_code,
            "details": self.details,
            "timestamp": self.timestamp
        }


class OpenRouterConnectionError(OpenRouterError):
    """Error koneksi ke OpenRouter."""
    def __init__(self, message: str, details: Dict = None):
        super().__init__(message, "CONNECTION_ERROR", None, details)


class OpenRouterAuthenticationError(OpenRouterError):
    """API key invalid atau expired."""
    def __init__(self, message: str, details: Dict = None):
        super().__init__(message, "AUTH_ERROR", 401, details)


class OpenRouterRateLimitError(OpenRouterError):
    """Rate limit tercapai."""
    def __init__(self, message: str, retry_after: int = None, details: Dict = None):
        super().__init__(message, "RATE_LIMIT", 429, details)
        self.retry_after = retry_after  # seconds


class OpenRouterModelError(OpenRouterError):
    """Model tidak tersedia atau error."""
    def __init__(self, message: str, model: str = None, details: Dict = None):
        super().__init__(message, "MODEL_ERROR", None, details)
        self.model = model


class OpenRouterValidationError(OpenRouterError):
    """Input validation error."""
    def __init__(self, message: str, field: str = None, details: Dict = None):
        super().__init__(message, "VALIDATION_ERROR", 400, details)
        self.field = field


class OpenRouterTimeoutError(OpenRouterError):
    """Request timeout."""
    def __init__(self, message: str, timeout_ms: int = None, details: Dict = None):
        super().__init__(message, "TIMEOUT_ERROR", 408, details)
        self.timeout_ms = timeout_ms


class OpenRouterBudgetError(OpenRouterError):
    """Budget limit exceeded."""
    def __init__(self, message: str, spent: int = None, limit: int = None):
        super().__init__(message, "BUDGET_EXCEEDED", 402, 
                        {"spent": spent, "limit": limit})
        self.spent = spent
        self.limit = limit


# =============================================================================
# CIRCUIT BREAKER
# =============================================================================

class CircuitBreakerState(Enum):
    CLOSED = "closed"       # Normal operation
    OPEN = "open"           # Failing, reject requests
    HALF_OPEN = "half_open" # Testing if recovered


class CircuitBreaker:
    """Circuit breaker untuk mencegah cascade failures."""

    def __init__(self, failure_threshold: int = 5, reset_timeout_ms: int = 30000,
                 half_open_max_calls: int = 3):
        self.failure_threshold = failure_threshold
        self.reset_timeout_ms = reset_timeout_ms
        self.half_open_max_calls = half_open_max_calls

        self._state = CircuitBreakerState.CLOSED
        self._failures = 0
        self._last_failure_time = None
        self._half_open_calls = 0
        self._successes = 0

    @property
    def state(self) -> CircuitBreakerState:
        if self._state == CircuitBreakerState.OPEN:
            if (datetime.utcnow() - self._last_failure_time).total_seconds() * 1000 >= self.reset_timeout_ms:
                self._state = CircuitBreakerState.HALF_OPEN
                self._half_open_calls = 0
                self._successes = 0
        return self._state

    def execute(self, operation: Callable) -> Any:
        current_state = self.state

        if current_state == CircuitBreakerState.OPEN:
            raise OpenRouterConnectionError(
                f"Circuit breaker OPEN. Retry after {self.reset_timeout_ms}ms."
            )

        if current_state == CircuitBreakerState.HALF_OPEN:
            if self._half_open_calls >= self.half_open_max_calls:
                raise OpenRouterConnectionError("Circuit breaker HALF_OPEN limit reached")
            self._half_open_calls += 1

        try:
            result = operation()
            self._on_success()
            return result
        except Exception as e:
            self._on_failure()
            raise e

    def _on_success(self):
        self._failures = 0
        if self._state == CircuitBreakerState.HALF_OPEN:
            self._successes += 1
            if self._successes >= self.half_open_max_calls:
                self._state = CircuitBreakerState.CLOSED
                self._half_open_calls = 0
                self._successes = 0

    def _on_failure(self):
        self._failures += 1
        self._last_failure_time = datetime.utcnow()
        if self._failures >= self.failure_threshold:
            self._state = CircuitBreakerState.OPEN

    def get_state(self) -> Dict:
        return {
            "state": self.state.value,
            "failures": self._failures,
            "last_failure": self._last_failure_time.isoformat() if self._last_failure_time else None,
            "half_open_calls": self._half_open_calls,
            "successes": self._successes
        }

    def reset(self):
        self._state = CircuitBreakerState.CLOSED
        self._failures = 0
        self._last_failure_time = None
        self._half_open_calls = 0
        self._successes = 0


# =============================================================================
# RETRY UTILITIES
# =============================================================================

class ExponentialBackoff:
    """Retry dengan exponential backoff + jitter."""

    RETRYABLE_STATUS_CODES: Set[int] = {408, 429, 500, 502, 503, 504}

    def __init__(self, max_retries: int = 3, base_delay_ms: float = 1000,
                 max_delay_ms: float = 30000, backoff_multiplier: float = 2.0,
                 on_retry: Callable = None):
        self.max_retries = max_retries
        self.base_delay_ms = base_delay_ms
        self.max_delay_ms = max_delay_ms
        self.backoff_multiplier = backoff_multiplier
        self.on_retry = on_retry  # callback(error, attempt, delay_ms)

    def is_retryable(self, error: Exception) -> bool:
        if isinstance(error, (OpenRouterConnectionError, OpenRouterTimeoutError)):
            return True
        if hasattr(error, 'status_code') and error.status_code in self.RETRYABLE_STATUS_CODES:
            return True
        if isinstance(error, requests.exceptions.ConnectionError):
            return True
        if isinstance(error, requests.exceptions.Timeout):
            return True
        return False

    def execute(self, operation: Callable) -> Any:
        last_error = None

        for attempt in range(self.max_retries + 1):
            try:
                return operation()
            except Exception as error:
                last_error = error

                if attempt == self.max_retries or not self.is_retryable(error):
                    raise error

                delay = min(
                    self.base_delay_ms * (self.backoff_multiplier ** attempt),
                    self.max_delay_ms
                )
                # Add jitter ±25%
                jittered_delay = delay * (0.75 + random.random() * 0.5)

                if self.on_retry:
                    self.on_retry(error, attempt + 1, jittered_delay)

                logger.warning(f"Retry {attempt + 1}/{self.max_retries} after {jittered_delay:.0f}ms: {error}")
                time.sleep(jittered_delay / 1000)

        raise last_error


# =============================================================================
# COST TRACKER
# =============================================================================

@dataclass
class UsageRecord:
    timestamp: str
    model: str
    input_tokens: int
    output_tokens: int
    total_tokens: int
    cost_cents: int


class CostTracker:
    """Track token usage dan cost per model."""

    # Pricing per 1M tokens in cents (update regularly)
    PRICING: Dict[str, Dict[str, float]] = {
        "openai/gpt-5": {"input": 15.0, "output": 120.0},
        "openai/gpt-4o": {"input": 2.5, "output": 10.0},
        "openai/gpt-4o-mini": {"input": 0.15, "output": 0.6},
        "anthropic/claude-3.5-sonnet": {"input": 3.0, "output": 15.0},
        "anthropic/claude-3.7-sonnet": {"input": 3.0, "output": 15.0},
        "google/gemini-2.0-flash-exp:free": {"input": 0.0, "output": 0.0},
        "meta-llama/llama-4": {"input": 0.5, "output": 2.0},
        "meta-llama/llama-3.3-8b-instruct:free": {"input": 0.0, "output": 0.0},
        "deepseek/deepseek-r1-0528-qwen3-8b:free": {"input": 0.0, "output": 0.0},
        "mistralai/mistral-small-3.2-24b-instruct:free": {"input": 0.0, "output": 0.0},
        "openrouter/auto": {"input": 0.0, "output": 0.0},
        "openrouter/free": {"input": 0.0, "output": 0.0},
    }

    def __init__(self, budget_limit_cents: int = None, warning_threshold: float = 0.8,
                 max_history: int = 1000):
        self.budget_limit = budget_limit_cents
        self.warning_threshold = warning_threshold
        self.max_history = max_history

        self.total_spent_cents = 0
        self.total_input_tokens = 0
        self.total_output_tokens = 0
        self.total_tokens = 0
        self.request_count = 0
        self.model_usage: Dict[str, Dict[str, Any]] = {}
        self.history: List[UsageRecord] = []

    def estimate_cost(self, usage: Dict, model: str) -> int:
        """Estimate cost in cents."""
        price = self.PRICING.get(model, {"input": 5.0, "output": 15.0})
        input_cost = (usage.get("prompt_tokens", 0) * price["input"]) / 1e6
        output_cost = (usage.get("completion_tokens", 0) * price["output"]) / 1e6
        return int(round((input_cost + output_cost) * 100))

    def record(self, usage: Dict, model: str, actual_cost: int = None) -> Dict:
        """Record usage and return budget status."""
        cost = actual_cost or self.estimate_cost(usage, model)

        record = UsageRecord(
            timestamp=datetime.utcnow().isoformat(),
            model=model,
            input_tokens=usage.get("prompt_tokens", 0),
            output_tokens=usage.get("completion_tokens", 0),
            total_tokens=usage.get("total_tokens", 0),
            cost_cents=cost
        )

        self.total_input_tokens += record.input_tokens
        self.total_output_tokens += record.output_tokens
        self.total_tokens += record.total_tokens
        self.total_spent_cents += cost
        self.request_count += 1

        if model not in self.model_usage:
            self.model_usage[model] = {"tokens": 0, "cost": 0, "requests": 0}
        self.model_usage[model]["tokens"] += record.total_tokens
        self.model_usage[model]["cost"] += cost
        self.model_usage[model]["requests"] += 1

        self.history.append(record)
        if len(self.history) > self.max_history:
            self.history.pop(0)

        return self.check_budget()

    def check_budget(self) -> Dict:
        """Check budget status."""
        if not self.budget_limit:
            return {"ok": True, "warning": False, "exceeded": False}

        ratio = self.total_spent_cents / self.budget_limit
        return {
            "ok": ratio < 1.0,
            "warning": self.warning_threshold <= ratio < 1.0,
            "exceeded": ratio >= 1.0,
            "spent_cents": self.total_spent_cents,
            "limit_cents": self.budget_limit,
            "remaining_cents": max(0, self.budget_limit - self.total_spent_cents),
            "usage_ratio": round(ratio, 4)
        }

    def get_stats(self) -> Dict:
        return {
            "total_spent_cents": self.total_spent_cents,
            "total_input_tokens": self.total_input_tokens,
            "total_output_tokens": self.total_output_tokens,
            "total_tokens": self.total_tokens,
            "request_count": self.request_count,
            "model_usage": self.model_usage,
            "budget": self.check_budget()
        }

    def reset(self):
        self.total_spent_cents = 0
        self.total_input_tokens = 0
        self.total_output_tokens = 0
        self.total_tokens = 0
        self.request_count = 0
        self.model_usage.clear()
        self.history.clear()


# =============================================================================
# SETTINGS HELPERS (backward compatible)
# =============================================================================

def _get_setting(key: str, default: str = '') -> str:
    """Ambil setting dari database dengan fallback."""
    try:
        from models import Setting
        s = Setting.query.filter_by(key=key).first()
        return s.value if s and s.value else default
    except Exception:
        return default


def is_openrouter_enabled() -> bool:
    """Cek apakah integrasi OpenRouter diaktifkan."""
    return _get_setting('openrouter_enabled', 'false') == 'true'


def should_notify_new_order() -> bool:
    return _get_setting('openrouter_notify_new_order', 'false') == 'true'


def should_notify_status_update() -> bool:
    return _get_setting('openrouter_notify_status_update', 'false') == 'true'


def should_notify_product_update() -> bool:
    return _get_setting('openrouter_notify_product_update', 'false') == 'true'


def get_base_url() -> str:
    return _get_setting('openrouter_base_url', 'https://openrouter.ai/api/v1')


def get_api_key() -> str:
    return _get_setting('openrouter_api_key', '')


def get_model() -> str:
    return _get_setting('openrouter_model', 'openrouter/free')


def get_fallback_models() -> List[str]:
    """Ambil daftar model fallback dari setting (comma-separated)."""
    raw = _get_setting('openrouter_fallback_models', '')
    return [m.strip() for m in raw.split(',') if m.strip()]


def get_site_url() -> str:
    return _get_setting('openrouter_site_url', 'https://pusatgadaiidn.pythonanywhere.com')


def get_app_name() -> str:
    return _get_setting('openrouter_app_name', 'Pusat Gadai Indonesia')


def get_provider_prefs() -> Dict:
    """Ambil provider routing preferences."""
    try:
        raw = _get_setting('openrouter_provider_prefs', '{}')
        return json.loads(raw) if raw else {}
    except (json.JSONDecodeError, ValueError):
        return {}


def get_budget_limit() -> Optional[int]:
    """Ambil budget limit dalam cents."""
    try:
        return int(_get_setting('openrouter_budget_limit', '0')) or None
    except (ValueError, TypeError):
        return None


def _get_headers(api_key: str = None, site_url: str = None, app_name: str = None) -> Dict[str, str]:
    """Generate headers untuk request OpenRouter."""
    return {
        'Authorization': f'Bearer {api_key or get_api_key()}',
        'Content-Type': 'application/json',
        'HTTP-Referer': site_url or get_site_url(),
        'X-Title': app_name or get_app_name()
    }


# =============================================================================
# MAIN CLIENT CLASS
# =============================================================================

class OpenRouterClient:
    """
    Production-ready OpenRouter client dengan fitur lengkap 2026.

    Supports: streaming, fallback chains, structured outputs, tool calling,
    multimodal, reasoning, retry, circuit breaker, cost tracking.
    """

    VERSION = "2026.6.0"
    SUPPORTED_FEATURES = [
        "streaming", "fallback_chains", "structured_outputs", "tool_calling",
        "multimodal", "reasoning_tokens", "provider_routing", "cost_tracking",
        "circuit_breaker", "retry_logic", "sse_parsing"
    ]

    def __init__(self, options: Dict = None):
        opts = options or {}

        # FIXED (2026 Pro): jangan bekukan api_key/base_url di sini. get_default_client()
        # adalah singleton yang cuma dibuat sekali, jadi kalau nilainya dibaca dan
        # disimpan langsung sebagai atribut biasa, perubahan admin lewat panel
        # /admin/settings (openrouter_api_key/openrouter_base_url di tabel Setting)
        # tidak akan pernah terpakai sampai proses PythonAnywhere di-restart —
        # notifikasi admin, webhook, dan surat penebusan akan diam-diam gagal
        # dengan key lama/kosong. Simpan override eksplisit (kalau ada) saja;
        # nilai aktual dibaca ulang tiap request lewat property di bawah.
        self._api_key_override = opts.get('api_key')
        self._base_url_override = opts.get('base_url')
        self.default_model = opts.get('model') or get_model()
        self.fallback_models = opts.get('fallback_models') or get_fallback_models()
        self.site_url = opts.get('site_url') or get_site_url()
        self.app_name = opts.get('app_name') or get_app_name()
        self.provider_prefs = opts.get('provider') or get_provider_prefs()

        self.timeout = opts.get('timeout', 30)
        self.stream_timeout = opts.get('stream_timeout', 120)

        # Initialize utilities
        retry_opts = opts.get('retry', {})
        self.retry_handler = ExponentialBackoff(
            max_retries=retry_opts.get('max_retries', 3),
            base_delay_ms=retry_opts.get('base_delay_ms', 1000),
            max_delay_ms=retry_opts.get('max_delay_ms', 30000),
            on_retry=retry_opts.get('on_retry')
        )

        cb_opts = opts.get('circuit_breaker', {})
        self.circuit_breaker = CircuitBreaker(
            failure_threshold=cb_opts.get('failure_threshold', 5),
            reset_timeout_ms=cb_opts.get('reset_timeout_ms', 30000)
        )

        budget = opts.get('budget_limit') or get_budget_limit()
        self.cost_tracker = CostTracker(
            budget_limit_cents=budget,
            warning_threshold=opts.get('warning_threshold', 0.8),
            max_history=opts.get('max_history', 1000)
        )

        # Interceptors
        self.request_interceptors: List[Callable] = []
        self.response_interceptors: List[Callable] = []

    @property
    def api_key(self) -> str:
        """Selalu baca ulang dari tabel Setting (fallback env/default lewat
        get_api_key()) kecuali ada override eksplisit dari opts['api_key']."""
        return self._api_key_override or get_api_key()

    @property
    def base_url(self) -> str:
        """Sama seperti api_key: dicek ulang tiap akses, bukan dibekukan
        saat singleton dibuat."""
        return (self._base_url_override or get_base_url()).rstrip('/')

    # ------------------------------------------------------------------
    # Interceptors
    # ------------------------------------------------------------------

    def add_request_interceptor(self, fn: Callable):
        self.request_interceptors.append(fn)

    def add_response_interceptor(self, fn: Callable):
        self.response_interceptors.append(fn)

    # ------------------------------------------------------------------
    # Core Request
    # ------------------------------------------------------------------

    def _request(self, method: str, endpoint: str, body: Dict = None, 
                 options: Dict = None) -> Dict:
        """Execute HTTP request dengan error handling."""
        options = options or {}
        url = f"{self.base_url}/{endpoint.lstrip('/')}"
        headers = _get_headers(self.api_key, self.site_url, self.app_name)
        headers.update(options.get('headers', {}))

        # Apply request interceptors
        config = {"method": method, "url": url, "headers": headers, "body": body, "options": options}
        for interceptor in self.request_interceptors:
            config = interceptor(config) or config

        timeout = options.get('timeout', self.timeout)

        try:
            response = requests.request(
                config["method"],
                config["url"],
                headers=config["headers"],
                json=config["body"] if config["body"] else None,
                timeout=timeout,
                stream=options.get('stream', False)
            )

            # Apply response interceptors
            for interceptor in self.response_interceptors:
                response = interceptor(response) or response

            if not response.ok:
                self._handle_error(response, config)

            if options.get('stream'):
                return {"_raw_response": response}  # Return raw for streaming

            return response.json()

        except requests.exceptions.ConnectionError as e:
            raise OpenRouterConnectionError(f"Failed to connect to {self.base_url}: {str(e)}")
        except requests.exceptions.Timeout as e:
            raise OpenRouterTimeoutError(f"Request timeout after {timeout}s", timeout * 1000)
        except OpenRouterError:
            raise
        except Exception as e:
            raise OpenRouterError(f"Unexpected error: {str(e)}", "UNKNOWN", details={"original": str(e)})

    def _handle_error(self, response: requests.Response, config: Dict):
        """Handle HTTP error responses."""
        try:
            error_data = response.json()
        except (json.JSONDecodeError, ValueError):
            error_data = {"error": {"message": response.status_text if hasattr(response, 'status_text') else response.reason}}

        error_msg = error_data.get('error', {}).get('message', str(error_data))
        details = {
            "url": config["url"],
            "model": config.get("body", {}).get("model"),
            "status": response.status_code
        }

        if response.status_code == 401:
            raise OpenRouterAuthenticationError(error_msg, details)
        elif response.status_code == 429:
            retry_after = response.headers.get('retry-after')
            raise OpenRouterRateLimitError(error_msg, int(retry_after) if retry_after else None, details)
        elif response.status_code >= 500:
            raise OpenRouterConnectionError(f"Server error: {error_msg}", details)
        else:
            raise OpenRouterError(error_msg, "API_ERROR", response.status_code, details)

    def _safe_request(self, method: str, endpoint: str, body: Dict = None, 
                      options: Dict = None) -> Dict:
        """Execute request dengan circuit breaker + retry."""
        return self.circuit_breaker.execute(
            lambda: self.retry_handler.execute(lambda: self._request(method, endpoint, body, options))
        )

    # ------------------------------------------------------------------
    # Chat Completions
    # ------------------------------------------------------------------

    def _clean_model_id(self, model: str) -> str:
        """Bersihkan model ID dari prefix ganda."""
        if not model:
            return "openrouter/auto"
        if model.startswith('openrouter/openrouter/'):
            return model.replace('openrouter/openrouter/', 'openrouter/', 1)
        if model.startswith('openrouter/anthropic/'):
            return model.replace('openrouter/anthropic/', 'anthropic/', 1)
        if model.startswith('openrouter/openai/'):
            return model.replace('openrouter/openai/', 'openai/', 1)
        if model.startswith('openrouter/google/'):
            return model.replace('openrouter/google/', 'google/', 1)
        if model.startswith('openrouter/meta-llama/'):
            return model.replace('openrouter/meta-llama/', 'meta-llama/', 1)
        if model.startswith('openrouter/deepseek/'):
            return model.replace('openrouter/deepseek/', 'deepseek/', 1)
        if model.startswith('openrouter/mistralai/'):
            return model.replace('openrouter/mistralai/', 'mistralai/', 1)
        return model

    def chat(self, messages: Union[str, List[Dict]], options: Dict = None) -> Dict:
        """
        Non-streaming chat completion.

        Args:
            messages: String atau list of dict {'role': 'user', 'content': '...'}
            options: Dict dengan keys: model, system_prompt, temperature, max_tokens,
                     response_format, tools, tool_choice, reasoning, provider, include_reasoning

        Returns:
            {'success': True, 'reply': str, 'model': str, 'usage': dict, 
             'cost': int, 'budget': dict, 'reasoning': str, 'tool_calls': list}
        """
        options = options or {}
        model = self._clean_model_id(options.get('model', self.default_model))

        # Budget check
        budget = self.cost_tracker.check_budget()
        if budget.get('exceeded'):
            raise OpenRouterBudgetError(
                "Budget limit exceeded", 
                self.cost_tracker.total_spent_cents, 
                self.cost_tracker.budget_limit
            )

        # Build messages
        msgs = []
        system_prompt = options.get('system_prompt')
        if system_prompt:
            msgs.append({"role": "system", "content": system_prompt})

        if isinstance(messages, str):
            msgs.append({"role": "user", "content": messages})
        elif isinstance(messages, list):
            msgs.extend(messages)

        # Build request body
        body = {
            "model": model,
            "messages": msgs,
            "stream": False,
            "temperature": options.get('temperature', 0.7),
            "max_tokens": options.get('max_tokens', 1000)
        }

        # Fallback models
        if self.fallback_models:
            body["models"] = [model] + self.fallback_models

        # Structured outputs
        if 'response_format' in options:
            body['response_format'] = options['response_format']

        # Tool calling
        if 'tools' in options and options['tools']:
            body['tools'] = options['tools']
            if 'tool_choice' in options:
                body['tool_choice'] = options['tool_choice']

        # Reasoning
        if 'reasoning' in options:
            body['reasoning'] = options['reasoning']
        if options.get('include_reasoning'):
            body['include_reasoning'] = True

        # Provider routing
        provider = options.get('provider') or self.provider_prefs
        if provider:
            body['provider'] = provider

        # Extra params
        for key in ['top_p', 'top_k', 'frequency_penalty', 'presence_penalty', 'seed']:
            if key in options:
                body[key] = options[key]

        result = self._safe_request('POST', '/chat/completions', body)

        if result.get('choices') and result['choices'][0]:
            choice = result['choices'][0]
            message = choice.get('message', {})

            # Track cost
            usage = result.get('usage', {})
            budget_status = self.cost_tracker.record(usage, result.get('model', model))

            response = {
                'success': True,
                'reply': message.get('content', ''),
                'model': result.get('model', model),
                'usage': usage,
                'cost_cents': self.cost_tracker.estimate_cost(usage, result.get('model', model)),
                'budget': budget_status,
                'raw': result
            }

            if 'reasoning' in message:
                response['reasoning'] = message['reasoning']
            if 'tool_calls' in message:
                response['tool_calls'] = message['tool_calls']
                response['requires_tool_execution'] = True

            return response

        return {'success': False, 'error': 'Empty response from AI'}

    # ------------------------------------------------------------------
    # Streaming
    # ------------------------------------------------------------------

    def stream_chat(self, messages: Union[str, List[Dict]], options: Dict = None) -> Generator[Dict, None, None]:
        """
        Streaming chat completion — returns generator.

        Yields dicts with keys: content, reasoning, done, raw

        Usage:
            for chunk in client.stream_chat("Hello"):
                if chunk['done']:
                    break
                print(chunk['content'], end="")
        """
        options = options or {}
        model = self._clean_model_id(options.get('model', self.default_model))

        msgs = []
        if 'system_prompt' in options:
            msgs.append({"role": "system", "content": options['system_prompt']})
        if isinstance(messages, str):
            msgs.append({"role": "user", "content": messages})
        elif isinstance(messages, list):
            msgs.extend(messages)

        body = {
            "model": model,
            "messages": msgs,
            "stream": True,
            "temperature": options.get('temperature', 0.7),
            "max_tokens": options.get('max_tokens', 1000)
        }

        if self.fallback_models:
            body["models"] = [model] + self.fallback_models
        if 'reasoning' in options:
            body['reasoning'] = options['reasoning']
        if options.get('include_reasoning'):
            body['include_reasoning'] = True

        provider = options.get('provider') or self.provider_prefs
        if provider:
            body['provider'] = provider

        result = self._safe_request('POST', '/chat/completions', body, {
            'stream': True,
            'timeout': self.stream_timeout
        })

        response = result.get('_raw_response')
        if not response:
            raise OpenRouterConnectionError("Failed to initiate stream")

        total_content = ""
        total_reasoning = ""

        try:
            for line in response.iter_lines():
                if not line:
                    continue

                line_str = line.decode('utf-8', errors='ignore')
                if not line_str.startswith('data: '):
                    continue

                payload = line_str[6:].strip()
                if payload == '[DONE]':
                    yield {
                        'content': '',
                        'reasoning': '',
                        'done': True,
                        'full_content': total_content,
                        'full_reasoning': total_reasoning
                    }
                    return

                try:
                    chunk = json.loads(payload)
                    delta = chunk.get('choices', [{}])[0].get('delta', {})
                    content = delta.get('content', '')
                    reasoning = delta.get('reasoning', '')

                    total_content += content
                    total_reasoning += reasoning

                    yield {
                        'content': content,
                        'reasoning': reasoning,
                        'done': False,
                        'raw': chunk
                    }
                except (json.JSONDecodeError, IndexError, KeyError):
                    continue

        finally:
            response.close()

        # Final yield if no [DONE] received
        yield {
            'content': '',
            'reasoning': '',
            'done': True,
            'full_content': total_content,
            'full_reasoning': total_reasoning
        }

    # ------------------------------------------------------------------
    # Multimodal
    # ------------------------------------------------------------------

    def chat_with_images(self, text: str, image_urls: List[str], options: Dict = None) -> Dict:
        """Send text + images untuk vision models."""
        content = [{"type": "text", "text": text}]
        for url in image_urls:
            content.append({"type": "image_url", "image_url": {"url": url}})
        return self.chat([{"role": "user", "content": content}], options)

    def chat_with_document(self, text: str, document_url: str, options: Dict = None) -> Dict:
        """Send text + document (PDF) untuk analysis."""
        content = [
            {"type": "text", "text": text},
            {"type": "file", "file_url": {"url": document_url}}
        ]
        return self.chat([{"role": "user", "content": content}], options)

    # ------------------------------------------------------------------
    # Structured Outputs
    # ------------------------------------------------------------------

    def structured_chat(self, prompt: str, schema: Dict, options: Dict = None) -> Dict:
        """
        Get structured JSON response dengan schema validation.

        Args:
            prompt: User prompt
            schema: JSON Schema dict dengan keys: name, description, strict, schema
            options: Extra options

        Returns:
            {'success': True, 'data': dict, 'reply': str, ...} atau error dict
        """
        options = options or {}

        response_format = {
            "type": "json_schema",
            "json_schema": {
                "name": schema.get('name', 'response'),
                "description": schema.get('description', 'Structured response'),
                "strict": schema.get('strict', True),
                "schema": schema.get('schema', schema)
            }
        }

        system_prompt = options.get('system_prompt', 
            'You must respond with valid JSON matching the provided schema. Do not include any other text.')

        result = self.chat(prompt, {
            **options,
            'system_prompt': system_prompt,
            'response_format': response_format,
            'temperature': options.get('temperature', 0.1)  # Low temp for structured
        })

        if not result.get('success'):
            return result

        try:
            parsed = json.loads(result['reply'])
            return {**result, 'data': parsed, 'parsed': True}
        except json.JSONDecodeError as e:
            return {
                **result,
                'success': False,
                'error': f'Failed to parse JSON: {str(e)}',
                'raw_reply': result['reply']
            }

    # ------------------------------------------------------------------
    # Tool Calling
    # ------------------------------------------------------------------

    def chat_with_tools(self, messages: Union[str, List[Dict]], tools: List[Dict], 
                        options: Dict = None) -> Dict:
        """
        Chat dengan tool definitions (function calling).

        Returns dict dengan 'tool_calls' jika model memanggil tool,
        atau 'reply' biasa jika tidak.
        """
        return self.chat(messages, {
            **(options or {}),
            'tools': tools,
            'tool_choice': (options or {}).get('tool_choice', 'auto')
        })

    def continue_with_tool_results(self, messages: List[Dict], tool_results: List[Dict], 
                                   options: Dict = None) -> Dict:
        """
        Lanjutkan conversation setelah tool execution.

        tool_results format: [{'tool_call_id': str, 'name': str, 'content': str|dict}]
        """
        updated_messages = list(messages)
        for result in tool_results:
            content = result['content']
            if isinstance(content, dict):
                content = json.dumps(content)
            updated_messages.append({
                "role": "tool",
                "tool_call_id": result['tool_call_id'],
                "name": result['name'],
                "content": content
            })
        return self.chat(updated_messages, options)

    # ------------------------------------------------------------------
    # Health & Diagnostics
    # ------------------------------------------------------------------

    def health_check(self) -> Dict:
        """Periksa kesehatan koneksi OpenRouter."""
        start = datetime.utcnow()
        try:
            result = self._safe_request('GET', '/models')
            latency = (datetime.utcnow() - start).total_seconds() * 1000
            models = result.get('data', [])

            return {
                'success': True,
                'models_available': len(models),
                'latency_ms': round(latency, 2),
                'message': f'Connected ({len(models)} models, {latency:.0f}ms)',
                'circuit_breaker': self.circuit_breaker.get_state(),
                'budget': self.cost_tracker.check_budget()
            }
        except Exception as e:
            return {
                'success': False,
                'models_available': 0,
                'latency_ms': (datetime.utcnow() - start).total_seconds() * 1000,
                'message': str(e),
                'circuit_breaker': self.circuit_breaker.get_state(),
                'budget': self.cost_tracker.check_budget(),
                'error': e.to_dict() if hasattr(e, 'to_dict') else str(e)
            }

    def list_models(self, filters: Dict = None) -> List[Dict]:
        """List available models dengan filtering."""
        filters = filters or {}
        result = self._safe_request('GET', '/models')
        models = result.get('data', [])

        if filters.get('provider'):
            models = [m for m in models if m.get('id', '').startswith(filters['provider'] + '/')]
        if filters.get('capability'):
            models = [m for m in models if m.get('top_provider', {}).get('supports', {}).get(filters['capability'])]
        if filters.get('max_price'):
            models = [m for m in models if (m.get('pricing', {}).get('prompt') or 0) <= filters['max_price']]

        return models

    def get_model_info(self, model_id: str) -> Dict:
        """Get detail untuk satu model."""
        return self._safe_request('GET', f'/models/{model_id}')

    # ------------------------------------------------------------------
    # Cost & Analytics
    # ------------------------------------------------------------------

    def get_cost_stats(self) -> Dict:
        return self.cost_tracker.get_stats()

    def reset_cost_tracking(self):
        self.cost_tracker.reset()

    def set_budget_limit(self, cents: int):
        self.cost_tracker.budget_limit = cents

    # ------------------------------------------------------------------
    # Circuit Breaker Control
    # ------------------------------------------------------------------

    def get_circuit_breaker_state(self) -> Dict:
        return self.circuit_breaker.get_state()

    def reset_circuit_breaker(self):
        self.circuit_breaker.reset()

    # ------------------------------------------------------------------
    # Business Logic (Pusat Gadai Indonesia)
    # ------------------------------------------------------------------

    def generate_invoice(self, order_id: str, order_data: Dict = None) -> Dict:
        """Generate invoice untuk order."""
        system_prompt = (
            "Anda adalah sistem invoice toko online Pusat Gadai Indonesia. "
            "Generate invoice detail dengan format yang rapi. Gunakan data yang tersedia."
        )
        prompt = f"Generate invoice untuk order {order_id}."
        if order_data:
            prompt += f"\n\nData: {json.dumps(order_data, indent=2, default=str)}"
        return self.chat(prompt, {'system_prompt': system_prompt, 'max_tokens': 800, 'temperature': 0.3})

    def generate_redemption_letter(self, order_id: str, order_data: Dict = None) -> Dict:
        """Generate surat penebusan."""
        system_prompt = (
            "Anda adalah sistem surat penebusan toko gadai Pusat Gadai Indonesia. "
            "Buat surat penebusan formal dengan format standar."
        )
        prompt = f"Buat surat penebusan untuk order {order_id}."
        if order_data:
            prompt += f"\n\nData: {json.dumps(order_data, indent=2, default=str)}"
        return self.chat(prompt, {'system_prompt': system_prompt, 'max_tokens': 1000, 'temperature': 0.3})

    def get_order_status(self, order_id: str) -> Dict:
        """Cek status order via AI."""
        return self.chat(
            f"Cek status order {order_id}.",
            {'system_prompt': 'Anda adalah sistem tracking order Pusat Gadai Indonesia.', 'max_tokens': 500}
        )

    def format_telegram_message(self, text: str, chat_id: str = None) -> Dict:
        """Format pesan untuk Telegram dengan HTML."""
        result = self.chat(
            f"Format pesan ini untuk Telegram dengan HTML tags:\n\n{text}",
            {'system_prompt': 'Format pesan untuk Telegram. Gunakan <b>, <i>, <code>, <pre> tags.', 'max_tokens': 800}
        )
        return {**result, 'chat_id': chat_id}

    def notify_admin(self, message: str) -> Dict:
        """Kirim notifikasi admin dengan AI formatting."""
        return self.chat(
            f"Buat notifikasi admin yang profesional:\n\n{message}",
            {'system_prompt': 'Anda adalah asisten notifikasi toko Pusat Gadai Indonesia.', 'max_tokens': 600}
        )

    def store_summary(self, store_data: Dict = None) -> Dict:
        """Ringkasan toko (analytics)."""
        prompt = 'Berikan ringkasan toko: total produk, order hari ini, pendapatan.'
        if store_data:
            prompt = f"Berikan ringkasan toko berdasarkan data:\n{json.dumps(store_data, indent=2, default=str)}"
        return self.chat(prompt, {
            'system_prompt': 'Anda adalah dashboard analytics toko Pusat Gadai Indonesia.',
            'max_tokens': 800
        })

    def process_webhook(self, event: str, payload: Dict) -> Dict:
        """Process webhook event dengan AI."""
        system_prompt = (
            "Anda adalah webhook processor untuk toko online Pusat Gadai Indonesia. "
            "Analisis event dan payload, tentukan aksi terbaik. "
            "Respons dalam format JSON dengan field: action, message, priority, data."
        )
        prompt = f"Event: {event}\nPayload: {json.dumps(payload, indent=2, default=str)}"

        return self.structured_chat(prompt, {
            'name': 'webhook_response',
            'description': 'Webhook processing result',
            'strict': True,
            'schema': {
                'type': 'object',
                'properties': {
                    'action': {
                        'type': 'string',
                        'enum': ['notify_admin', 'sync_data', 'update_status', 'log_only', 'generate_invoice', 'generate_redemption']
                    },
                    'message': {'type': 'string'},
                    'priority': {'type': 'string', 'enum': ['high', 'normal', 'low']},
                    'data': {'type': 'object'}
                },
                'required': ['action', 'message', 'priority']
            }
        }, {'system_prompt': system_prompt, 'max_tokens': 500})

    def ping(self) -> bool:
        """Test koneksi dengan ping sederhana."""
        try:
            result = self.chat('Ping', {'max_tokens': 10, 'temperature': 0})
            return result.get('success', False)
        except Exception:
            return False

    # ------------------------------------------------------------------
    # Static Utilities
    # ------------------------------------------------------------------

    @staticmethod
    def create_message(role: str, content: str, **kwargs) -> Dict:
        """Create a message dict."""
        msg = {"role": role, "content": content}
        msg.update(kwargs)
        return msg

    @staticmethod
    def create_tool_definition(name: str, description: str, parameters: Dict) -> Dict:
        """Create a tool definition."""
        return {
            "type": "function",
            "function": {"name": name, "description": description, "parameters": parameters}
        }

    @staticmethod
    def create_image_content(url: str, detail: str = "auto") -> Dict:
        """Create an image content block."""
        return {"type": "image_url", "image_url": {"url": url, "detail": detail}}


# =============================================================================
# BACKWARD-COMPATIBLE FUNCTIONS (legacy support)
# =============================================================================

def send_notification(event: str, payload: Dict) -> bool:
    """
    Kirim notifikasi event ke OpenRouter AI (legacy wrapper).
    Menggunakan default client dengan retry dan error handling modern.
    """
    try:
        client = get_default_client()
        result = client.process_webhook(event, payload)
        return result.get('success', False)
    except Exception as e:
        logger.error(f"OpenRouter notify error: {e}")
        return False


def test_openrouter_connection(timeout: int = 5, base_url: str = None, api_key: str = None) -> Dict:
    """
    Test koneksi ke OpenRouter API (legacy wrapper).
    Sekarang menggunakan client health_check dengan lebih detail.
    """
    try:
        client = OpenRouterClient({
            'api_key': api_key or get_api_key(),
            'base_url': base_url or get_base_url(),
            'timeout': timeout
        })
        return client.health_check()
    except Exception as e:
        return {
            'success': False,
            'message': str(e),
            'models_available': 0
        }


def chat_completion(messages: list, temperature: float = 0.7, max_tokens: int = 1000, 
                  timeout: int = 30, **kwargs) -> Dict:
    """
    Kirim chat completion request (legacy wrapper).
    Sekarang menggunakan client.chat dengan retry, cost tracking, dll.
    """
    try:
        client = get_default_client()
        return client.chat(messages, {
            'temperature': temperature,
            'max_tokens': max_tokens,
            'timeout': timeout,
            **kwargs
        })
    except Exception as e:
        return {'success': False, 'error': str(e)}


# =============================================================================
# DEFAULT CLIENT (singleton)
# =============================================================================

_default_client: Optional[OpenRouterClient] = None


def get_default_client() -> OpenRouterClient:
    """Get atau create default singleton client."""
    global _default_client
    if _default_client is None:
        _default_client = OpenRouterClient()
    return _default_client


def set_default_client(client: OpenRouterClient):
    """Set custom default client."""
    global _default_client
    _default_client = client


# =============================================================================
# EXPORTS
# =============================================================================

__all__ = [
    # Classes
    'OpenRouterClient',
    'CircuitBreaker',
    'ExponentialBackoff',
    'CostTracker',
    'UsageRecord',

    # Exceptions
    'OpenRouterError',
    'OpenRouterConnectionError',
    'OpenRouterAuthenticationError',
    'OpenRouterRateLimitError',
    'OpenRouterModelError',
    'OpenRouterValidationError',
    'OpenRouterTimeoutError',
    'OpenRouterBudgetError',

    # Functions
    'get_default_client',
    'set_default_client',
    'send_notification',
    'test_openrouter_connection',
    'chat_completion',

    # Settings helpers
    'is_openrouter_enabled',
    'should_notify_new_order',
    'should_notify_status_update',
    'should_notify_product_update',
    'get_base_url',
    'get_api_key',
    'get_model',
    'get_fallback_models',
    'get_site_url',
    'get_app_name',
    'get_provider_prefs',
    'get_budget_limit',
    '_get_setting',
]