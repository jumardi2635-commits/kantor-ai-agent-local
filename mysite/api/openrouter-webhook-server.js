#!/usr/bin/env node
/**
 * OpenRouter Webhook Handler — Node.js Standalone Server (2026 Pro Edition)
 * ===========================================================================
 *
 * 2026 Pro fix
 * ------------
 * File ini SEBELUMNYA berisi kode Python yang ke-copy dengan ekstensi .js
 * (identik byte-per-byte dengan api/openrouter_webhook_flask.py) — bukan
 * JavaScript sama sekali. Kalau dijalankan lewat `node openrouter-webhook-
 * server.js` akan langsung gagal dengan SyntaxError karena sintaksnya
 * Python, bukan JS. File ini ditulis ulang dari nol sebagai server Node.js
 * standalone sungguhan, TIDAK dependen pada Express atau library eksternal
 * lain — cuma modul bawaan Node 18+ (`http`, `crypto`, `fetch` global) —
 * supaya bisa langsung `node openrouter-webhook-server.js` tanpa `npm
 * install` apa pun.
 *
 * Ini adalah server Node.js TERPISAH dari aplikasi Flask utama (app.py di
 * PythonAnywhere). Dia TIDAK membaca tabel `Setting` SQLAlchemy Flask
 * secara langsung, karena itu perlu driver DB Node terpisah yang belum
 * ditentukan stack-nya. Sebagai gantinya, konfigurasi dibaca dari
 * environment variable (lihat daftar di bawah), dengan opsi `settingsResolver`
 * kalau nanti Anda ingin menyambungkannya ke sumber setting lain (mis. DB
 * yang sama dengan Flask lewat driver Node, atau memanggil endpoint
 * /admin/settings Flask lewat HTTP).
 *
 * Fungsionalitas setara dengan api/openrouter_webhook_flask.py:
 *   - Verifikasi signature webhook (HMAC-SHA256, timing-safe compare)
 *   - Panggil OpenRouter AI untuk memproses event & menentukan aksi
 *   - Fallback manual processing kalau AI gagal/tidak dikonfigurasi
 *   - Kirim notifikasi ke Telegram
 *   - Endpoint health check
 *
 * Cara pakai:
 *   1. Set environment variables (lihat bawah), minimal OPENROUTER_API_KEY
 *      dan TELEGRAM_BOT_TOKEN kalau ingin notifikasi Telegram aktif.
 *   2. Jalankan:  node openrouter-webhook-server.js
 *      (atau PORT=4000 node openrouter-webhook-server.js)
 *   3. Webhook URL:       http://<host>:<port>/openrouter/webhook
 *      Backward compat:   http://<host>:<port>/openclaw/webhook
 *      Health check:      http://<host>:<port>/openrouter/health
 *   4. Atur di Telegram Bot: setWebhook ke URL di atas (lewat reverse
 *      proxy/domain publik Anda, server ini sendiri tidak menangani TLS).
 *
 * Environment variables:
 *   PORT                       - Port HTTP server (default: 3000)
 *   OPENROUTER_API_KEY         - API key OpenRouter
 *   OPENROUTER_MODEL           - Model AI (default: openrouter/free)
 *   OPENROUTER_BASE_URL        - Base URL API (default: https://openrouter.ai/api/v1)
 *   OPENROUTER_SITE_URL        - HTTP-Referer (default: https://pusatgadaiidn.pythonanywhere.com)
 *   OPENROUTER_APP_NAME        - X-Title (default: Pusat Gadai Indonesia)
 *   OPENROUTER_WEBHOOK_SECRET  - Secret untuk verifikasi signature (kosongkan untuk nonaktifkan verifikasi)
 *   TELEGRAM_BOT_TOKEN         - Token bot Telegram
 *   TELEGRAM_CHAT_ID           - ID chat default untuk notifikasi
 *
 * Bisa juga di-require sebagai modul (untuk testing atau digabung ke server
 * Node lain):
 *   const { createServer, handleWebhookEvent } = require('./openrouter-webhook-server');
 */

'use strict';

const http = require('http');
const crypto = require('crypto');

// ============================================================================
// CONFIG
// ============================================================================

const DEFAULTS = {
  openrouter_base_url: 'https://openrouter.ai/api/v1',
  openrouter_model: 'openrouter/free',
  openrouter_site_url: 'https://pusatgadaiidn.pythonanywhere.com',
  openrouter_app_name: 'Pusat Gadai Indonesia',
};

/**
 * Ambil setting dari environment variable, dengan default.
 * Terima `settingsResolver` opsional (sync function(key) => string|undefined)
 * untuk sumber config lain (mis. DB) sebelum fallback ke env var.
 */
function getSetting(key, defaultValue, settingsResolver) {
  if (typeof settingsResolver === 'function') {
    try {
      const resolved = settingsResolver(key);
      if (resolved) return resolved;
    } catch (err) {
      console.error(`[Config] settingsResolver error for "${key}":`, err.message);
    }
  }
  const envValue = process.env[key.toUpperCase()];
  if (envValue) return envValue;
  return defaultValue !== undefined ? defaultValue : (DEFAULTS[key] || '');
}

// ============================================================================
// OPENROUTER AI
// ============================================================================

/**
 * Kirim request chat completion ke OpenRouter AI.
 * Mengembalikan { success, reply, model, usage } atau { success: false, error }.
 */
async function callOpenRouterAI(messages, { temperature = 0.7, maxTokens = 500, settingsResolver } = {}) {
  const apiKey = getSetting('openrouter_api_key', '', settingsResolver).trim();
  if (!apiKey) {
    return { success: false, error: 'API key not configured' };
  }

  const baseUrl = getSetting('openrouter_base_url', DEFAULTS.openrouter_base_url, settingsResolver).replace(/\/+$/, '');
  const model = getSetting('openrouter_model', DEFAULTS.openrouter_model, settingsResolver);
  const siteUrl = getSetting('openrouter_site_url', DEFAULTS.openrouter_site_url, settingsResolver);
  const appName = getSetting('openrouter_app_name', DEFAULTS.openrouter_app_name, settingsResolver);

  const headers = {
    'Content-Type': 'application/json',
    Authorization: `Bearer ${apiKey}`,
    'HTTP-Referer': siteUrl,
    'X-Title': appName,
  };

  const body = {
    model,
    messages,
    stream: false,
    temperature,
    max_tokens: maxTokens,
  };

  try {
    const controller = new AbortController();
    const timeoutId = setTimeout(() => controller.abort(), 30000);

    const response = await fetch(`${baseUrl}/chat/completions`, {
      method: 'POST',
      headers,
      body: JSON.stringify(body),
      signal: controller.signal,
    });
    clearTimeout(timeoutId);

    if (response.status === 200) {
      const result = await response.json();
      const choices = result.choices || [];
      if (choices.length && choices[0].message) {
        return {
          success: true,
          reply: choices[0].message.content || '',
          model: result.model || model,
          usage: result.usage || {},
        };
      }
      return { success: false, error: 'Empty AI response' };
    }
    if (response.status === 401) return { success: false, error: 'Invalid API key' };
    if (response.status === 429) return { success: false, error: 'Rate limit exceeded' };
    return { success: false, error: `HTTP ${response.status}` };
  } catch (err) {
    if (err.name === 'AbortError') {
      return { success: false, error: 'Request timeout' };
    }
    if (err.cause && ['ECONNREFUSED', 'ENOTFOUND'].includes(err.cause.code)) {
      return { success: false, error: 'Cannot connect to OpenRouter' };
    }
    console.error('[OpenRouter] Error:', err.message);
    return { success: false, error: err.message };
  }
}

/** Ekstrak aksi dari respons AI, format [AKSI:nama|param=value]. */
function extractAction(aiReply) {
  const match = /\[AKSI:([^\]]+)\]/.exec(aiReply || '');
  if (!match) return { action: null, params: {} };

  const parts = match[1].split('|');
  const action = parts[0].trim();
  const params = {};
  for (const part of parts.slice(1)) {
    const idx = part.indexOf('=');
    if (idx !== -1) {
      params[part.slice(0, idx).trim()] = part.slice(idx + 1).trim();
    }
  }
  return { action, params };
}

// ============================================================================
// TELEGRAM
// ============================================================================

/** Kirim pesan ke Telegram. Mengembalikan boolean sukses. */
async function sendTelegram(chatId, text, { settingsResolver } = {}) {
  const token = getSetting('telegram_bot_token', '', settingsResolver);
  if (!token) return false;

  const target = chatId || getSetting('telegram_chat_id', '', settingsResolver);
  if (!target) return false;

  try {
    const response = await fetch(`https://api.telegram.org/bot${token}/sendMessage`, {
      method: 'POST',
      headers: { 'Content-Type': 'application/json' },
      body: JSON.stringify({ chat_id: target, text, parse_mode: 'HTML' }),
    });
    const data = await response.json();
    return Boolean(data.ok);
  } catch (err) {
    console.error('[Telegram] Error:', err.message);
    return false;
  }
}

// ============================================================================
// EVENT HANDLERS
// ============================================================================

/** Fallback manual processing kalau AI gagal/tidak dikonfigurasi. */
async function handleManual(eventType, payload, opts = {}) {
  const { settingsResolver } = opts;

  if (eventType === 'ping') {
    return { status: 'pong', timestamp: new Date().toISOString() };
  }

  if (eventType === 'new_order') {
    const orderNum = payload.order_number || 'N/A';
    if (getSetting('telegram_bot_token', '', settingsResolver)) {
      const msg =
        `🛒 <b>Pesanan Baru</b>\n` +
        `Order: ${orderNum}\n` +
        `Nama: ${payload.customer_name || 'N/A'}\n` +
        `Total: ${payload.total || 'N/A'}`;
      await sendTelegram(null, msg, opts);
    }
    return { status: 'notified' };
  }

  if (eventType === 'order_status_update') {
    const orderNum = payload.order_number || 'N/A';
    const status = payload.status || 'N/A';
    if (getSetting('telegram_bot_token', '', settingsResolver)) {
      await sendTelegram(null, `🔄 Order <b>${orderNum}</b> status: ${status}`, opts);
    }
    return { status: 'notified' };
  }

  if (eventType === 'command') {
    const command = payload.command;
    const params = payload.params || {};

    switch (command) {
      case 'ping':
        return { status: 'pong' };
      case 'sync_products':
        return { status: 'ok', message: 'Sync initiated' };
      case 'health_check':
        return { status: 'ok' };
      case 'test_notification':
        await sendTelegram(null, '✅ Test notification dari OpenRouter', opts);
        return { status: 'ok' };
      case 'create_product': {
        const name = params.value || 'Produk Baru';
        await sendTelegram(null, `🆕 Produk: <b>${name}</b>`, opts);
        return { status: 'ok' };
      }
      case 'generate_invoice': {
        const invId = params.value || 'INV-001';
        await sendTelegram(null, `📄 Invoice <b>${invId}</b>`, opts);
        return { status: 'ok' };
      }
      case 'generate_redemption_letter': {
        const redId = params.value || 'INV-001';
        await sendTelegram(null, `📜 Surat penebusan <b>${redId}</b>`, opts);
        return { status: 'ok' };
      }
      case 'send_telegram': {
        const chatId = params.chat_id;
        const text = params.text || 'Test';
        const success = await sendTelegram(chatId, text, opts);
        return { status: success ? 'ok' : 'error' };
      }
      default:
        return { status: 'error', message: `Unknown: ${command}` };
    }
  }

  return { status: 'received' };
}

/** Proses event dengan OpenRouter AI, fallback ke manual kalau AI gagal. */
async function handleWithAI(eventType, payload, opts = {}) {
  const systemPrompt =
    'Anda adalah webhook processor toko online. ' +
    'Analisis event, tentukan aksi. ' +
    'Format: [AKSI:aksi|param=value]. ' +
    'Aksi: notify_admin, sync_data, update_status, log_only, generate_invoice, generate_redemption.';

  const userMessage = `Event: ${eventType}\nPayload: ${JSON.stringify(payload, null, 2)}`;

  const aiResult = await callOpenRouterAI(
    [
      { role: 'system', content: systemPrompt },
      { role: 'user', content: userMessage },
    ],
    opts
  );

  if (!aiResult.success) {
    return handleManual(eventType, payload, opts);
  }

  const aiReply = aiResult.reply;
  const { action, params } = extractAction(aiReply);
  const stripped = aiReply.replace(/\[AKSI:[^\]]+\]\s*/g, '').trim();

  let actionResult = '';
  if (action === 'notify_admin') {
    await sendTelegram(null, stripped || 'Notifikasi', opts);
    actionResult = 'Notified';
  } else if (action === 'generate_invoice') {
    const oid = params.order_id || 'N/A';
    await sendTelegram(null, `📄 Invoice <b>${oid}</b>`, opts);
    actionResult = `Invoice ${oid}`;
  } else if (action === 'generate_redemption') {
    const rid = params.order_id || 'N/A';
    await sendTelegram(null, `📜 Surat penebusan <b>${rid}</b>`, opts);
    actionResult = `Redemption ${rid}`;
  } else {
    actionResult = 'Logged';
  }

  return {
    status: 'processed',
    action: action || 'log_only',
    ai_message: stripped,
    action_result: actionResult,
    model: aiResult.model,
    tokens: (aiResult.usage && aiResult.usage.total_tokens) || 0,
  };
}

/** Entry point tunggal untuk memproses event webhook (dipakai server & test). */
async function handleWebhookEvent(eventType, payload, opts = {}) {
  return handleWithAI(eventType, payload, opts);
}

// ============================================================================
// SIGNATURE VERIFICATION
// ============================================================================

function verifySignature(secret, rawBody, signature) {
  if (!secret) return true; // verifikasi dinonaktifkan kalau secret kosong
  if (!signature) return false;

  const expected = crypto.createHmac('sha256', secret).update(rawBody).digest('hex');
  const expectedBuf = Buffer.from(expected, 'utf8');
  const signatureBuf = Buffer.from(signature, 'utf8');

  if (expectedBuf.length !== signatureBuf.length) return false;
  return crypto.timingSafeEqual(expectedBuf, signatureBuf);
}

// ============================================================================
// HTTP SERVER
// ============================================================================

function readRawBody(req) {
  return new Promise((resolve, reject) => {
    const chunks = [];
    let size = 0;
    const MAX_BODY_BYTES = 2 * 1024 * 1024; // 2MB guard

    req.on('data', (chunk) => {
      size += chunk.length;
      if (size > MAX_BODY_BYTES) {
        reject(new Error('Payload too large'));
        req.destroy();
        return;
      }
      chunks.push(chunk);
    });
    req.on('end', () => resolve(Buffer.concat(chunks)));
    req.on('error', reject);
  });
}

function sendJson(res, statusCode, payload) {
  const body = JSON.stringify(payload);
  res.writeHead(statusCode, { 'Content-Type': 'application/json' });
  res.end(body);
}

/**
 * Buat instance server Node http bawaan (tanpa Express).
 * `options.settingsResolver` opsional: function(key) => string|undefined,
 * dipakai sebelum fallback ke environment variable.
 */
function createServer(options = {}) {
  const { settingsResolver } = options;

  const server = http.createServer(async (req, res) => {
    const url = new URL(req.url, `http://${req.headers.host || 'localhost'}`);
    const path = url.pathname;

    // ---- Health check ----
    if (req.method === 'GET' && path === '/openrouter/health') {
      return sendJson(res, 200, {
        status: 'ok',
        service: 'OpenRouter Webhook',
        openrouter_configured: Boolean(getSetting('openrouter_api_key', '', settingsResolver)),
        telegram_configured: Boolean(getSetting('telegram_bot_token', '', settingsResolver)),
      });
    }

    // ---- Webhook (+ backward-compat path) ----
    const isWebhookPath = path === '/openrouter/webhook' || path === '/openclaw/webhook';
    if (req.method === 'POST' && isWebhookPath) {
      let rawBody;
      try {
        rawBody = await readRawBody(req);
      } catch (err) {
        return sendJson(res, 413, { error: 'Payload too large' });
      }

      const secret = getSetting('openrouter_webhook_secret', '', settingsResolver);
      const signature =
        req.headers['x-openrouter-signature'] || req.headers['x-openclaw-signature'];

      if (!verifySignature(secret, rawBody, signature)) {
        return sendJson(res, 401, { error: 'Invalid signature' });
      }

      let data;
      try {
        data = JSON.parse(rawBody.toString('utf8') || '{}');
      } catch (err) {
        return sendJson(res, 400, { error: 'Invalid JSON' });
      }

      if (!data || typeof data !== 'object') {
        return sendJson(res, 400, { error: 'Invalid JSON' });
      }

      const eventType = data.event;
      if (!eventType) {
        return sendJson(res, 400, { error: "Missing 'event'" });
      }

      try {
        const result = await handleWebhookEvent(eventType, data.data || {}, { settingsResolver });
        return sendJson(res, 200, result);
      } catch (err) {
        console.error('[Webhook] Error:', err);
        return sendJson(res, 500, { error: 'Internal error' });
      }
    }

    return sendJson(res, 404, { error: 'Not found' });
  });

  return server;
}

// ============================================================================
// STANDALONE ENTRY POINT
// ============================================================================

if (require.main === module) {
  const port = parseInt(process.env.PORT, 10) || 3000;
  const server = createServer();
  server.listen(port, () => {
    console.log(`[OpenRouter Webhook] Listening on port ${port}`);
    console.log(`[OpenRouter Webhook] Webhook:      POST http://localhost:${port}/openrouter/webhook`);
    console.log(`[OpenRouter Webhook] Backward compat: POST http://localhost:${port}/openclaw/webhook`);
    console.log(`[OpenRouter Webhook] Health check:  GET  http://localhost:${port}/openrouter/health`);
  });
}

module.exports = {
  createServer,
  handleWebhookEvent,
  handleManual,
  handleWithAI,
  callOpenRouterAI,
  sendTelegram,
  extractAction,
  verifySignature,
  getSetting,
};
