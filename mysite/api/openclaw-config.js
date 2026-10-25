/**
 * OpenRouter JavaScript Client Library — 2026 Professional Edition
 * ================================================================
 * 
 * Production-ready client dengan dukungan penuh untuk fitur OpenRouter 2026:
 * - Streaming responses (SSE / async iterator)
 * - Model fallback chains (auto-failover across providers)
 * - Structured outputs (JSON Schema / Zod-compatible)
 * - Reasoning tokens & thinking budgets
 * - Tool calling / function calling
 * - Multimodal inputs (images, PDFs, audio)
 * - Provider routing preferences (price, speed, ZDR)
 * - Request tracing & cost analytics
 * - Circuit breaker & retry dengan exponential backoff
 * - Token usage tracking & budget controls
 * 
 * Compatible dengan: Node.js 18+, Deno, Bun, Browser (ESM)
 * 
 * Usage:
 *   const { OpenRouterClient } = require('./openrouter');
 *   const client = new OpenRouterClient({
 *     apiKey: process.env.OPENROUTER_API_KEY,
 *     model: 'openai/gpt-5',
 *     fallbackModels: ['anthropic/claude-3.5-sonnet', 'meta-llama/llama-4']
 *   });
 *   
 *   // Streaming
 *   for await (const chunk of client.streamChat('Hello')) {
 *     process.stdout.write(chunk.content);
 *   }
 *   
 *   // Structured output
 *   const result = await client.chat('Extract data', {
 *     responseFormat: { type: 'json_schema', schema: mySchema }
 *   });
 */

// ============================================================================
// ERROR CLASSES
// ============================================================================

class OpenRouterError extends Error {
  constructor(message, code = null, statusCode = null, details = null) {
    super(message);
    this.name = 'OpenRouterError';
    this.code = code;
    this.statusCode = statusCode;
    this.details = details;
    this.timestamp = new Date().toISOString();
  }

  toJSON() {
    return {
      name: this.name,
      message: this.message,
      code: this.code,
      statusCode: this.statusCode,
      details: this.details,
      timestamp: this.timestamp
    };
  }
}

class OpenRouterConnectionError extends OpenRouterError {
  constructor(message, details = null) {
    super(message, 'CONNECTION_ERROR', null, details);
    this.name = 'OpenRouterConnectionError';
  }
}

class OpenRouterAuthenticationError extends OpenRouterError {
  constructor(message, details = null) {
    super(message, 'AUTH_ERROR', 401, details);
    this.name = 'OpenRouterAuthenticationError';
  }
}

class OpenRouterRateLimitError extends OpenRouterError {
  constructor(message, retryAfter = null, details = null) {
    super(message, 'RATE_LIMIT', 429, details);
    this.name = 'OpenRouterRateLimitError';
    this.retryAfter = retryAfter; // seconds
  }
}

class OpenRouterModelError extends OpenRouterError {
  constructor(message, model = null, details = null) {
    super(message, 'MODEL_ERROR', null, details);
    this.name = 'OpenRouterModelError';
    this.model = model;
  }
}

class OpenRouterValidationError extends OpenRouterError {
  constructor(message, field = null, details = null) {
    super(message, 'VALIDATION_ERROR', 400, details);
    this.name = 'OpenRouterValidationError';
    this.field = field;
  }
}

class OpenRouterTimeoutError extends OpenRouterError {
  constructor(message, timeoutMs = null, details = null) {
    super(message, 'TIMEOUT_ERROR', 408, details);
    this.name = 'OpenRouterTimeoutError';
    this.timeoutMs = timeoutMs;
  }
}

// ============================================================================
// CIRCUIT BREAKER
// ============================================================================

class CircuitBreaker {
  constructor(options = {}) {
    this.failureThreshold = options.failureThreshold || 5;
    this.resetTimeoutMs = options.resetTimeoutMs || 30000;
    this.halfOpenMaxCalls = options.halfOpenMaxCalls || 3;

    this.state = 'CLOSED'; // CLOSED, OPEN, HALF_OPEN
    this.failures = 0;
    this.lastFailureTime = null;
    this.halfOpenCalls = 0;
    this.successes = 0;
  }

  async execute(operation) {
    if (this.state === 'OPEN') {
      if (Date.now() - this.lastFailureTime >= this.resetTimeoutMs) {
        this.state = 'HALF_OPEN';
        this.halfOpenCalls = 0;
        this.successes = 0;
      } else {
        throw new OpenRouterConnectionError(
          `Circuit breaker OPEN for ${this.resetTimeoutMs}ms. Too many failures.`
        );
      }
    }

    if (this.state === 'HALF_OPEN' && this.halfOpenCalls >= this.halfOpenMaxCalls) {
      throw new OpenRouterConnectionError('Circuit breaker HALF_OPEN limit reached');
    }

    if (this.state === 'HALF_OPEN') {
      this.halfOpenCalls++;
    }

    try {
      const result = await operation();
      this.onSuccess();
      return result;
    } catch (error) {
      this.onFailure();
      throw error;
    }
  }

  onSuccess() {
    this.failures = 0;
    if (this.state === 'HALF_OPEN') {
      this.successes++;
      if (this.successes >= this.halfOpenMaxCalls) {
        this.state = 'CLOSED';
        this.halfOpenCalls = 0;
        this.successes = 0;
      }
    }
  }

  onFailure() {
    this.failures++;
    this.lastFailureTime = Date.now();
    if (this.failures >= this.failureThreshold) {
      this.state = 'OPEN';
    }
  }

  getState() {
    return {
      state: this.state,
      failures: this.failures,
      lastFailure: this.lastFailureTime,
      halfOpenCalls: this.halfOpenCalls,
      successes: this.successes
    };
  }
}

// ============================================================================
// RETRY UTILITIES
// ============================================================================

class ExponentialBackoff {
  constructor(options = {}) {
    this.maxRetries = options.maxRetries || 3;
    this.baseDelayMs = options.baseDelayMs || 1000;
    this.maxDelayMs = options.maxDelayMs || 30000;
    this.backoffMultiplier = options.backoffMultiplier || 2;
    this.retryableStatusCodes = new Set([
      408, 429, 500, 502, 503, 504
    ]);
    this.onRetry = options.onRetry || null; // callback(error, attempt)
  }

  isRetryable(error) {
    if (!error) return false;
    if (error instanceof OpenRouterConnectionError) return true;
    if (error instanceof OpenRouterTimeoutError) return true;
    if (error.statusCode && this.retryableStatusCodes.has(error.statusCode)) return true;
    if (error.code === 'ECONNREFUSED' || error.code === 'ENOTFOUND' || error.code === 'ETIMEDOUT') return true;
    return false;
  }

  async execute(operation) {
    let lastError;

    for (let attempt = 0; attempt <= this.maxRetries; attempt++) {
      try {
        return await operation();
      } catch (error) {
        lastError = error;

        if (attempt === this.maxRetries || !this.isRetryable(error)) {
          throw error;
        }

        const delay = Math.min(
          this.baseDelayMs * Math.pow(this.backoffMultiplier, attempt),
          this.maxDelayMs
        );

        // Add jitter (±25%)
        const jitteredDelay = delay * (0.75 + Math.random() * 0.5);

        if (this.onRetry) {
          this.onRetry(error, attempt + 1, jitteredDelay);
        }

        await this.sleep(jitteredDelay);
      }
    }

    throw lastError;
  }

  sleep(ms) {
    return new Promise(resolve => setTimeout(resolve, ms));
  }
}

// ============================================================================
// TOKEN BUDGET & COST TRACKER
// ============================================================================

class CostTracker {
  constructor(options = {}) {
    this.budgetLimit = options.budgetLimit || null; // in cents
    this.warningThreshold = options.warningThreshold || 0.8; // 80%
    this.totalSpent = 0;
    this.totalTokens = { input: 0, output: 0, total: 0 };
    this.requestCount = 0;
    this.modelUsage = new Map();
    this.history = [];
    this.maxHistory = options.maxHistory || 1000;
  }

  recordUsage(usage, model, cost = null) {
    const record = {
      timestamp: new Date().toISOString(),
      model,
      inputTokens: usage?.prompt_tokens || 0,
      outputTokens: usage?.completion_tokens || 0,
      totalTokens: usage?.total_tokens || 0,
      cost: cost || this.estimateCost(usage, model)
    };

    this.totalTokens.input += record.inputTokens;
    this.totalTokens.output += record.outputTokens;
    this.totalTokens.total += record.totalTokens;
    this.totalSpent += record.cost;
    this.requestCount++;

    if (!this.modelUsage.has(model)) {
      this.modelUsage.set(model, { tokens: 0, cost: 0, requests: 0 });
    }
    const modelStats = this.modelUsage.get(model);
    modelStats.tokens += record.totalTokens;
    modelStats.cost += record.cost;
    modelStats.requests++;

    this.history.push(record);
    if (this.history.length > this.maxHistory) {
      this.history.shift();
    }

    return this.checkBudget();
  }

  estimateCost(usage, model) {
    // Simplified pricing — update with actual model prices
    const pricing = {
      'openai/gpt-5': { input: 15, output: 120 }, // per 1M tokens in cents
      'openai/gpt-4o': { input: 2.5, output: 10 },
      'anthropic/claude-3.5-sonnet': { input: 3, output: 15 },
      'meta-llama/llama-4': { input: 0.5, output: 2 },
      'openrouter/free': { input: 0, output: 0 },
    };

    const price = pricing[model] || { input: 5, output: 15 };
    const inputCost = (usage?.prompt_tokens || 0) * price.input / 1e6;
    const outputCost = (usage?.completion_tokens || 0) * price.output / 1e6;
    return Math.round((inputCost + outputCost) * 100); // in cents
  }

  checkBudget() {
    if (!this.budgetLimit) return { ok: true, warning: false };

    const ratio = this.totalSpent / this.budgetLimit;
    return {
      ok: ratio < 1,
      warning: ratio >= this.warningThreshold && ratio < 1,
      exceeded: ratio >= 1,
      spent: this.totalSpent,
      limit: this.budgetLimit,
      remaining: Math.max(0, this.budgetLimit - this.totalSpent)
    };
  }

  getStats() {
    return {
      totalSpent: this.totalSpent,
      totalTokens: this.totalTokens,
      requestCount: this.requestCount,
      modelUsage: Object.fromEntries(this.modelUsage),
      budget: this.checkBudget()
    };
  }

  reset() {
    this.totalSpent = 0;
    this.totalTokens = { input: 0, output: 0, total: 0 };
    this.requestCount = 0;
    this.modelUsage.clear();
    this.history = [];
  }
}

// ============================================================================
// MAIN CLIENT CLASS
// ============================================================================

class OpenRouterClient {
  /**
   * @param {Object} options
   * @param {string} options.apiKey - API key OpenRouter (wajib)
   * @param {string} [options.baseUrl='https://openrouter.ai/api/v1'] - Base URL
   * @param {string} [options.model='openrouter/auto'] - Model default
   * @param {string[]} [options.fallbackModels=[]] - Model fallback chain
   * @param {string} [options.siteUrl=''] - HTTP-Referer
   * @param {string} [options.appName=''] - X-Title
   * @param {number} [options.timeout=30000] - Timeout per request (ms)
   * @param {number} [options.streamTimeout=120000] - Timeout untuk streaming (ms)
   * @param {Object} [options.retry] - Retry options
   * @param {Object} [options.circuitBreaker] - Circuit breaker options
   * @param {Object} [options.costTracker] - Cost tracking options
   * @param {Object} [options.provider] - Provider routing preferences
   */
  constructor(options = {}) {
    const {
      apiKey,
      baseUrl = 'https://openrouter.ai/api/v1',
      model = 'openrouter/auto',
      fallbackModels = [],
      siteUrl = '',
      appName = 'Pusat Gadai Indonesia',
      timeout = 30000,
      streamTimeout = 120000,
      retry = {},
      circuitBreaker = {},
      costTracker = {},
      provider = {}
    } = options;

    if (!apiKey) {
      throw new OpenRouterValidationError('apiKey is required', 'apiKey');
    }

    this.baseUrl = baseUrl.replace(/\/$/, '');
    this.apiKey = apiKey;
    this.defaultModel = model;
    this.fallbackModels = fallbackModels;
    this.siteUrl = siteUrl;
    this.appName = appName;
    this.timeout = timeout;
    this.streamTimeout = streamTimeout;
    this.providerPrefs = provider;

    // Initialize utilities
    this.retryHandler = new ExponentialBackoff({
      maxRetries: retry.maxRetries ?? 3,
      baseDelayMs: retry.baseDelayMs ?? 1000,
      maxDelayMs: retry.maxDelayMs ?? 30000,
      onRetry: retry.onRetry
    });

    this.circuitBreaker = new CircuitBreaker({
      failureThreshold: circuitBreaker.failureThreshold ?? 5,
      resetTimeoutMs: circuitBreaker.resetTimeoutMs ?? 30000
    });

    this.costTracker = new CostTracker({
      budgetLimit: costTracker.budgetLimit || null,
      warningThreshold: costTracker.warningThreshold || 0.8,
      maxHistory: costTracker.maxHistory || 1000
    });

    // Request interceptors
    this.requestInterceptors = [];
    this.responseInterceptors = [];

    // Runtime detection
    this.isNode = typeof process !== 'undefined' && process.versions?.node;
    this.isBrowser = typeof window !== 'undefined';
    this.isDeno = typeof Deno !== 'undefined';
  }

  /**
   * Clean model ID from duplicate prefixes
   */
  _cleanModelId(model) {
    if (!model) return 'openrouter/auto';
    if (model.startsWith('openrouter/openrouter/')) return model.replace('openrouter/openrouter/', 'openrouter/');
    if (model.startsWith('openrouter/anthropic/')) return model.replace('openrouter/anthropic/', 'anthropic/');
    if (model.startsWith('openrouter/openai/')) return model.replace('openrouter/openai/', 'openai/');
    if (model.startsWith('openrouter/google/')) return model.replace('openrouter/google/', 'google/');
    if (model.startsWith('openrouter/meta-llama/')) return model.replace('openrouter/meta-llama/', 'meta-llama/');
    if (model.startsWith('openrouter/deepseek/')) return model.replace('openrouter/deepseek/', 'deepseek/');
    if (model.startsWith('openrouter/mistralai/')) return model.replace('openrouter/mistralai/', 'mistralai/');
    return model;
  }

  // ------------------------------------------------------------------
  // INTERCEPTORS
  // ------------------------------------------------------------------

  addRequestInterceptor(fn) {
    this.requestInterceptors.push(fn);
  }

  addResponseInterceptor(fn) {
    this.responseInterceptors.push(fn);
  }

  // ------------------------------------------------------------------
  // HEADERS
  // ------------------------------------------------------------------

  _getHeaders(extra = {}) {
    const headers = {
      'Content-Type': 'application/json',
      'Authorization': `Bearer ${this.apiKey}`,
      ...extra
    };

    if (this.siteUrl) {
      headers['HTTP-Referer'] = this.siteUrl;
    }
    if (this.appName) {
      headers['X-Title'] = this.appName;
    }

    return headers;
  }

  // ------------------------------------------------------------------
  // CORE REQUEST
  // ------------------------------------------------------------------

  async _request(method, endpoint, body = null, options = {}) {
    const url = `${this.baseUrl}/${endpoint.replace(/^\//, '')}`;
    const headers = this._getHeaders(options.headers);

    // Apply request interceptors
    let requestConfig = { method, url, headers, body, options };
    for (const interceptor of this.requestInterceptors) {
      requestConfig = await interceptor(requestConfig) || requestConfig;
    }

    const fetchOptions = {
      method: requestConfig.method,
      headers: requestConfig.headers,
      signal: AbortSignal.timeout(options.timeout || this.timeout)
    };

    if (requestConfig.body) {
      fetchOptions.body = JSON.stringify(requestConfig.body);
    }

    try {
      const response = await fetch(requestConfig.url, fetchOptions);

      // Apply response interceptors
      let responseData = response;
      for (const interceptor of this.responseInterceptors) {
        responseData = await interceptor(responseData) || responseData;
      }

      if (!responseData.ok) {
        await this._handleErrorResponse(responseData, requestConfig);
      }

      const contentType = responseData.headers.get('content-type') || '';

      if (options.stream) {
        return responseData.body; // Return ReadableStream for streaming
      }

      if (contentType.includes('application/json')) {
        return await responseData.json();
      }

      return { status: 'ok', text: await responseData.text() };

    } catch (error) {
      if (error.name === 'TimeoutError' || error.name === 'AbortError') {
        throw new OpenRouterTimeoutError(
          `Request timeout after ${options.timeout || this.timeout}ms`,
          options.timeout || this.timeout
        );
      }
      if (error instanceof OpenRouterError) throw error;

      // Network errors
      if (error.message?.includes('fetch') || 
          error.message?.includes('ENOTFOUND') || 
          error.message?.includes('ECONNREFUSED') ||
          error.message?.includes('network')) {
        throw new OpenRouterConnectionError(
          `Failed to connect to OpenRouter at ${this.baseUrl}`,
          { originalError: error.message }
        );
      }
      throw new OpenRouterError(error.message, 'UNKNOWN_ERROR', null, { original: error.message });
    }
  }

  async _handleErrorResponse(response, requestConfig) {
    let errorData;
    try {
      errorData = await response.json();
    } catch {
      errorData = { error: { message: response.statusText } };
    }

    const errorMsg = errorData.error?.message || errorData.error || `HTTP ${response.status}`;
    const details = {
      url: requestConfig.url,
      model: requestConfig.body?.model,
      status: response.status
    };

    if (response.status === 401) {
      throw new OpenRouterAuthenticationError(errorMsg, details);
    }
    if (response.status === 429) {
      const retryAfter = response.headers.get('retry-after');
      throw new OpenRouterRateLimitError(errorMsg, retryAfter ? parseInt(retryAfter) : null, details);
    }
    if (response.status >= 500) {
      throw new OpenRouterConnectionError(`Server error: ${errorMsg}`, details);
    }
    throw new OpenRouterError(errorMsg, 'API_ERROR', response.status, details);
  }

  // ------------------------------------------------------------------
  // WRAPPED REQUEST WITH RETRY + CIRCUIT BREAKER
  // ------------------------------------------------------------------

  async _safeRequest(method, endpoint, body, options = {}) {
    return this.circuitBreaker.execute(() =>
      this.retryHandler.execute(() => this._request(method, endpoint, body, options))
    );
  }

  // ============================================================================
  // CHAT COMPLETIONS
  // ============================================================================

  /**
   * Non-streaming chat completion
   * @param {string|Array} messages - Pesan user atau array messages
   * @param {Object} [options={}] - Opsi tambahan
   * @param {string} [options.model] - Override model
   * @param {string} [options.systemPrompt] - System prompt
   * @param {number} [options.temperature=0.7] - Sampling temperature
   * @param {number} [options.maxTokens=1000] - Max tokens
   * @param {Object} [options.responseFormat] - Structured output format
   * @param {Array} [options.tools] - Tool definitions untuk function calling
   * @param {string} [options.toolChoice] - Tool choice strategy
   * @param {Object} [options.reasoning] - Reasoning configuration
   * @param {Object} [options.provider] - Provider routing overrides
   * @returns {Promise<Object>} { success, reply, model, usage, cost, raw }
   */
  async chat(messages, options = {}) {
    const {
      model: rawModel = this.defaultModel,
      systemPrompt = null,
      temperature = 0.7,
      maxTokens = 1000,
      responseFormat = null,
      tools = null,
      toolChoice = null,
      reasoning = null,
      provider = null,
      includeReasoning = false,
      ...extraParams
    } = options;
    const model = this._cleanModelId(rawModel);

    // Build messages array
    let msgs = [];
    if (systemPrompt) {
      msgs.push({ role: 'system', content: systemPrompt });
    }
    if (typeof messages === 'string') {
      msgs.push({ role: 'user', content: messages });
    } else if (Array.isArray(messages)) {
      msgs = msgs.concat(messages);
    }

    // Build request body
    const body = {
      model,
      messages: msgs,
      stream: false,
      temperature,
      max_tokens: maxTokens,
      ...extraParams
    };

    // Add fallback models chain (clean all model IDs)
    if (this.fallbackModels.length > 0) {
      body.models = [model, ...this.fallbackModels.map(m => this._cleanModelId(m))];
    }

    // Structured outputs
    if (responseFormat) {
      body.response_format = responseFormat;
    }

    // Tool calling
    if (tools && Array.isArray(tools)) {
      body.tools = tools;
      if (toolChoice) {
        body.tool_choice = toolChoice;
      }
    }

    // Reasoning tokens
    if (reasoning) {
      body.reasoning = reasoning;
    }
    if (includeReasoning) {
      body.include_reasoning = true;
    }

    // Provider routing
    const providerPrefs = provider || this.providerPrefs;
    if (Object.keys(providerPrefs).length > 0) {
      body.provider = providerPrefs;
    }

    const result = await this._safeRequest('POST', '/chat/completions', body);

    if (result.choices && result.choices[0]) {
      const choice = result.choices[0];
      const message = choice.message || {};

      // Track usage & cost
      const budgetCheck = this.costTracker.recordUsage(result.usage, result.model || model);

      const response = {
        success: true,
        reply: message.content || '',
        model: result.model || model,
        usage: result.usage || {},
        cost: this.costTracker.estimateCost(result.usage, result.model || model),
        budget: budgetCheck,
        raw: result
      };

      // Include reasoning if available
      if (message.reasoning) {
        response.reasoning = message.reasoning;
      }

      // Include tool calls if present
      if (message.tool_calls) {
        response.toolCalls = message.tool_calls;
      }

      return response;
    }

    return { success: false, error: 'Empty response from AI' };
  }

  // ============================================================================
  // STREAMING
  // ============================================================================

  /**
   * Streaming chat completion — returns async iterator
   * @param {string|Array} messages
   * @param {Object} options
   * @returns {AsyncGenerator<{content: string, reasoning?: string, done: boolean}>}
   * 
   * Usage:
   *   for await (const chunk of client.streamChat('Hello')) {
   *     if (chunk.done) break;
   *     process.stdout.write(chunk.content);
   *   }
   */
  async *streamChat(messages, options = {}) {
    const {
      model: rawModel = this.defaultModel,
      systemPrompt = null,
      temperature = 0.7,
      maxTokens = 1000,
      reasoning = null,
      includeReasoning = false,
      ...extraParams
    } = options;
    const model = this._cleanModelId(rawModel);

    let msgs = [];
    if (systemPrompt) msgs.push({ role: 'system', content: systemPrompt });
    if (typeof messages === 'string') msgs.push({ role: 'user', content: messages });
    else if (Array.isArray(messages)) msgs = msgs.concat(messages);

    const body = {
      model,
      messages: msgs,
      stream: true,
      temperature,
      max_tokens: maxTokens,
      ...extraParams
    };

    if (this.fallbackModels.length > 0) {
      body.models = [model, ...this.fallbackModels.map(m => this._cleanModelId(m))];
    }
    if (reasoning) body.reasoning = reasoning;
    if (includeReasoning) body.include_reasoning = true;

    const providerPrefs = options.provider || this.providerPrefs;
    if (Object.keys(providerPrefs).length > 0) {
      body.provider = providerPrefs;
    }

    try {
      const stream = await this._safeRequest('POST', '/chat/completions', body, {
        stream: true,
        timeout: this.streamTimeout
      });

      const reader = stream.getReader();
      const decoder = new TextDecoder();
      let buffer = '';
      let totalContent = '';
      let totalReasoning = '';

      try {
        while (true) {
          const { done, value } = await reader.read();
          if (done) break;

          buffer += decoder.decode(value, { stream: true });
          const lines = buffer.split('\n');
          buffer = lines.pop() || '';

          for (const line of lines) {
            if (!line.trim() || !line.startsWith('data: ')) continue;

            const payload = line.slice(6).trim();
            if (payload === '[DONE]') {
              yield { content: '', reasoning: '', done: true };
              return;
            }

            try {
              const chunk = JSON.parse(payload);
              const delta = chunk.choices?.[0]?.delta || {};
              const content = delta.content || '';
              const reasoningChunk = delta.reasoning || '';

              totalContent += content;
              totalReasoning += reasoningChunk;

              yield {
                content,
                reasoning: reasoningChunk,
                done: false,
                raw: chunk
              };
            } catch (e) {
              // Skip malformed chunks
            }
          }
        }
      } finally {
        reader.releaseLock();
      }

      // Final done signal
      yield { content: '', reasoning: '', done: true, fullContent: totalContent, fullReasoning: totalReasoning };

    } catch (error) {
      if (error instanceof OpenRouterError) throw error;
      throw new OpenRouterConnectionError(`Streaming failed: ${error.message}`);
    }
  }

  // ============================================================================
  // MULTIMODAL
  // ============================================================================

  /**
   * Send multimodal message (text + images)
   * @param {string} text - Text prompt
   * @param {Array<string>} imageUrls - Array of image URLs or base64 data URIs
   * @param {Object} options
   * @returns {Promise<Object>}
   */
  async chatWithImages(text, imageUrls, options = {}) {
    const content = [
      { type: 'text', text }
    ];

    for (const url of imageUrls) {
      if (url.startsWith('data:')) {
        // Base64 data URI
        content.push({ type: 'image_url', image_url: { url } });
      } else {
        content.push({ type: 'image_url', image_url: { url } });
      }
    }

    return this.chat([{ role: 'user', content }], options);
  }

  /**
   * Send PDF or document for analysis
   * @param {string} text - Question about the document
   * @param {string} documentUrl - URL to PDF or document
   * @param {Object} options
   */
  async chatWithDocument(text, documentUrl, options = {}) {
    const content = [
      { type: 'text', text },
      { type: 'file', file_url: { url: documentUrl } }
    ];

    return this.chat([{ role: 'user', content }], options);
  }

  // ============================================================================
  // STRUCTURED OUTPUTS
  // ============================================================================

  /**
   * Get structured JSON response with schema validation
   * @param {string} prompt - User prompt
   * @param {Object} schema - JSON Schema object
   * @param {Object} options
   * @returns {Promise<Object>} Parsed JSON object
   */
  async structuredChat(prompt, schema, options = {}) {
    const responseFormat = {
      type: 'json_schema',
      json_schema: {
        name: schema.name || 'response',
        description: schema.description || 'Structured response',
        strict: schema.strict !== false,
        schema: schema.schema || schema
      }
    };

    const result = await this.chat(prompt, {
      ...options,
      responseFormat,
      systemPrompt: options.systemPrompt || `You must respond with valid JSON matching the provided schema. Do not include any other text.`
    });

    if (!result.success) return result;

    try {
      const parsed = JSON.parse(result.reply);
      return { ...result, data: parsed, parsed: true };
    } catch (e) {
      return {
        ...result,
        success: false,
        error: `Failed to parse JSON: ${e.message}`,
        rawReply: result.reply
      };
    }
  }

  // ============================================================================
  // TOOL CALLING / FUNCTION CALLING
  // ============================================================================

  /**
   * Chat with tool definitions (function calling)
   * @param {string|Array} messages
   * @param {Array} tools - Array of tool definitions
   * @param {Object} options
   * @returns {Promise<Object>} { success, reply, toolCalls, model, usage }
   * 
   * Tool definition format:
   * {
   *   type: 'function',
   *   function: {
   *     name: 'get_weather',
   *     description: 'Get current weather',
   *     parameters: {
   *       type: 'object',
   *       properties: { location: { type: 'string' } },
   *       required: ['location']
   *     }
   *   }
   * }
   */
  async chatWithTools(messages, tools, options = {}) {
    const result = await this.chat(messages, {
      ...options,
      tools,
      toolChoice: options.toolChoice || 'auto'
    });

    if (result.toolCalls) {
      return {
        ...result,
        requiresToolExecution: true,
        toolCalls: result.toolCalls
      };
    }

    return result;
  }

  /**
   * Execute tool calls and continue conversation
   * @param {Array} messages - Conversation history
   * @param {Array} toolResults - Results from executed tools
   * @param {Object} options
   */
  async continueWithToolResults(messages, toolResults, options = {}) {
    const updatedMessages = [...messages];

    for (const result of toolResults) {
      updatedMessages.push({
        role: 'tool',
        tool_call_id: result.toolCallId,
        name: result.name,
        content: typeof result.content === 'string' ? result.content : JSON.stringify(result.content)
      });
    }

    return this.chat(updatedMessages, options);
  }

  // ============================================================================
  // HEALTH & DIAGNOSTICS
  // ============================================================================

  /**
   * Periksa kesehatan koneksi OpenRouter
   * @returns {Promise<Object>} { success, modelsAvailable, latency, message }
   */
  async healthCheck() {
    const start = Date.now();
    try {
      const result = await this._safeRequest('GET', '/models');
      const latency = Date.now() - start;
      const models = result.data || [];

      return {
        success: true,
        modelsAvailable: models.length,
        latency,
        message: `Connected (${models.length} models, ${latency}ms)`,
        circuitBreaker: this.circuitBreaker.getState(),
        budget: this.costTracker.checkBudget()
      };
    } catch (error) {
      return {
        success: false,
        modelsAvailable: 0,
        latency: Date.now() - start,
        message: error.message,
        circuitBreaker: this.circuitBreaker.getState(),
        budget: this.costTracker.checkBudget(),
        error: error.toJSON()
      };
    }
  }

  /**
   * Get available models with filtering
   * @param {Object} filters - { provider, capability, maxPrice }
   * @returns {Promise<Array>} Model list
   */
  async listModels(filters = {}) {
    const result = await this._safeRequest('GET', '/models');
    let models = result.data || [];

    if (filters.provider) {
      models = models.filter(m => m.id.startsWith(filters.provider + '/'));
    }
    if (filters.capability) {
      models = models.filter(m => m.top_provider?.supports?.[filters.capability]);
    }
    if (filters.maxPrice) {
      models = models.filter(m => {
        const price = m.pricing?.prompt || 0;
        return price <= filters.maxPrice;
      });
    }

    return models;
  }

  /**
   * Get model details
   * @param {string} modelId
   */
  async getModelInfo(modelId) {
    return this._safeRequest('GET', `/models/${modelId}`);
  }

  // ============================================================================
  // COST & ANALYTICS
  // ============================================================================

  getCostStats() {
    return this.costTracker.getStats();
  }

  resetCostTracking() {
    this.costTracker.reset();
  }

  setBudgetLimit(cents) {
    this.costTracker.budgetLimit = cents;
  }

  // ============================================================================
  // CIRCUIT BREAKER CONTROL
  // ============================================================================

  getCircuitBreakerState() {
    return this.circuitBreaker.getState();
  }

  resetCircuitBreaker() {
    this.circuitBreaker.state = 'CLOSED';
    this.circuitBreaker.failures = 0;
    this.circuitBreaker.lastFailureTime = null;
  }

  // ============================================================================
  // BUSINESS LOGIC HELPERS (Pusat Gadai Indonesia)
  // ============================================================================

  /**
   * Generate invoice untuk order
   * @param {string} orderId - ID order
   * @param {Object} orderData - Data order untuk konteks
   */
  async generateInvoice(orderId, orderData = null) {
    const systemPrompt = `Anda adalah sistem invoice toko online Pusat Gadai Indonesia. 
Generate invoice detail dengan format yang rapi. Gunakan data yang tersedia.`;

    const prompt = orderData 
      ? `Generate invoice untuk order ${orderId}:
${JSON.stringify(orderData, null, 2)}`
      : `Generate invoice untuk order ${orderId}. Berikan link dan ringkasan.`;

    return this.chat(prompt, { systemPrompt, maxTokens: 800, temperature: 0.3 });
  }

  /**
   * Generate surat penebusan (redemption letter)
   * @param {string} orderId
   * @param {Object} orderData
   */
  async generateRedemptionLetter(orderId, orderData = null) {
    const systemPrompt = `Anda adalah sistem surat penebusan toko gadai Pusat Gadai Indonesia.
Buat surat penebusan formal dengan format standar.`;

    const prompt = orderData
      ? `Buat surat penebusan untuk order ${orderId}:
${JSON.stringify(orderData, null, 2)}`
      : `Buat surat penebusan untuk order ${orderId}.`;

    return this.chat(prompt, { systemPrompt, maxTokens: 1000, temperature: 0.3 });
  }

  /**
   * Cek status order via AI
   * @param {string} orderId
   */
  async getOrderStatus(orderId) {
    return this.chat(
      `Cek status order ${orderId}.`,
      { systemPrompt: 'Anda adalah sistem tracking order Pusat Gadai Indonesia.', maxTokens: 500 }
    );
  }

  /**
   * Format pesan untuk Telegram dengan HTML
   * @param {string} text - Teks mentah
   * @param {string} chatId - ID chat tujuan
   */
  async formatTelegramMessage(text, chatId = null) {
    const result = await this.chat(
      `Format pesan ini untuk Telegram dengan HTML tags:

${text}`,
      { systemPrompt: 'Format pesan untuk Telegram. Gunakan <b>, <i>, <code>, <pre> tags.', maxTokens: 800 }
    );
    return { ...result, chatId };
  }

  /**
   * Kirim notifikasi admin dengan AI formatting
   * @param {string} message - Pesan mentah
   */
  async notifyAdmin(message) {
    return this.chat(
      `Buat notifikasi admin yang profesional:

${message}`,
      { systemPrompt: 'Anda adalah asisten notifikasi toko Pusat Gadai Indonesia.', maxTokens: 600 }
    );
  }

  /**
   * Ringkasan toko (analytics)
   * @param {Object} storeData - Data toko untuk analisis
   */
  async storeSummary(storeData = null) {
    const prompt = storeData
      ? `Berikan ringkasan toko berdasarkan data:
${JSON.stringify(storeData, null, 2)}`
      : 'Berikan ringkasan toko: total produk, order hari ini, pendapatan.';

    return this.chat(prompt, {
      systemPrompt: 'Anda adalah dashboard analytics toko Pusat Gadai Indonesia.',
      maxTokens: 800
    });
  }

  /**
   * Process webhook event dengan AI
   * @param {string} event - Nama event
   * @param {Object} payload - Data event
   */
  async processWebhook(event, payload) {
    const systemPrompt = (
      'Anda adalah webhook processor untuk toko online Pusat Gadai Indonesia. ' +
      'Analisis event dan tentukan aksi terbaik. ' +
      'Respons dalam format JSON: { action, message, priority, data }'
    );

    const userMessage = `Event: ${event}
Payload: ${JSON.stringify(payload, null, 2)}`;

    return this.structuredChat(userMessage, {
      name: 'webhook_response',
      description: 'Webhook processing result',
      strict: true,
      schema: {
        type: 'object',
        properties: {
          action: { type: 'string', enum: ['notify_admin', 'sync_data', 'update_status', 'log_only', 'generate_invoice', 'generate_redemption'] },
          message: { type: 'string' },
          priority: { type: 'string', enum: ['high', 'normal', 'low'] },
          data: { type: 'object' }
        },
        required: ['action', 'message', 'priority']
      }
    }, { systemPrompt, maxTokens: 500 });
  }

  /**
   * Test koneksi dengan ping sederhana
   * @returns {Promise<boolean>}
   */
  async ping() {
    try {
      const result = await this.chat('Ping', { maxTokens: 10, temperature: 0 });
      return result.success;
    } catch {
      return false;
    }
  }

  // ============================================================================
  // STATIC UTILITIES
  // ============================================================================

  static createMessage(role, content, options = {}) {
    const msg = { role, content };
    if (options.name) msg.name = options.name;
    if (options.toolCalls) msg.tool_calls = options.toolCalls;
    if (options.toolCallId) msg.tool_call_id = options.toolCallId;
    return msg;
  }

  static createToolDefinition(name, description, parameters) {
    return {
      type: 'function',
      function: { name, description, parameters }
    };
  }

  static createImageContent(url, detail = 'auto') {
    return { type: 'image_url', image_url: { url, detail } };
  }
}

// ============================================================================
// FACTORY & CONVENIENCE
// ============================================================================

function createClient(options) {
  return new OpenRouterClient(options);
}

// Default singleton (lazy init)
let _defaultClient = null;
function getDefaultClient() {
  if (!_defaultClient) {
    const apiKey = process?.env?.OPENROUTER_API_KEY || '';
    if (!apiKey) throw new OpenRouterValidationError('OPENROUTER_API_KEY not set');
    _defaultClient = new OpenRouterClient({
      apiKey,
      model: process?.env?.OPENROUTER_MODEL || 'openrouter/auto'
    });
  }
  return _defaultClient;
}

// ============================================================================
// EXPORTS
// ============================================================================

module.exports = {
  // Main class
  OpenRouterClient,

  // Factory
  createClient,
  getDefaultClient,

  // Error classes
  OpenRouterError,
  OpenRouterConnectionError,
  OpenRouterAuthenticationError,
  OpenRouterRateLimitError,
  OpenRouterModelError,
  OpenRouterValidationError,
  OpenRouterTimeoutError,

  // Utilities
  CircuitBreaker,
  ExponentialBackoff,
  CostTracker,

  // Constants
  VERSION: '2026.6.0',
  SUPPORTED_FEATURES: [
    'streaming',
    'fallback_chains',
    'structured_outputs',
    'tool_calling',
    'multimodal',
    'reasoning_tokens',
    'provider_routing',
    'cost_tracking',
    'circuit_breaker',
    'retry_logic'
  ]
};