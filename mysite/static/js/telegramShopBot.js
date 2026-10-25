import express from 'express';
import axios from 'axios';
import crypto from 'crypto';

// =============================================================================
// SETTING HELPER & CONFIGURATION
// =============================================================================
const getSetting = (key, defaultValue = '') => {
    return process.env[key.toUpperCase()] || defaultValue;
};

const getBotToken = () => getSetting('telegram_bot_token', '').trim();
const getOpenRouterApiKey = () => getSetting('openrouter_api_key', '').trim();
const getOpenRouterModel = () => getSetting('openrouter_model', 'google/gemini-2.0-flash-001').trim();

// =============================================================================
// RATE LIMITING (in-memory, per-IP)
// =============================================================================
const rateLimitStorage = new Map();

const isAllowed = (ip, limit = 60, windowMs = 60000) => {
    const now = Date.now();
    if (!rateLimitStorage.has(ip)) {
        rateLimitStorage.set(ip, []);
    }

    let timestamps = rateLimitStorage.get(ip);
    timestamps = timestamps.filter(t => now - t < windowMs);

    if (timestamps.length >= limit) {
        rateLimitStorage.set(ip, timestamps);
        return false;
    }

    timestamps.push(now);
    rateLimitStorage.set(ip, timestamps);
    return true;
};

// =============================================================================
// HTML ESCAPE HELPER
// =============================================================================
const escapeHtml = (text) => {
    if (!text) return '';
    return text
        .replace(/&/g, '&amp;')
        .replace(/</g, '&lt;')
        .replace(/>/g, '&gt;');
};

// =============================================================================
// OPENROUTER AI (FALLBACK ADMIN)
// =============================================================================
const OPENROUTER_API_URL = 'https://openrouter.ai/api/v1/chat/completions';

/**
 * Memanggil OpenRouter untuk mendapatkan respons AI sebagai admin toko.
 * @param {string} userMessage - Pesan dari pelanggan
 * @param {string} senderName  - Nama pengirim
 * @returns {Promise<string|null>} Balasan dari AI atau null jika gagal
 */
const getAIResponse = async (userMessage, senderName = 'Kak') => {
    const apiKey = getOpenRouterApiKey();
    if (!apiKey) {
        console.error('[AI Admin] OpenRouter API key tidak ditemukan.');
        return null;
    }

    const model = getOpenRouterModel();
    const systemPrompt = `Kamu adalah admin dari toko "Pusat Gadai Indonesia", toko gadget second terpercaya.
Kamu membantu pelanggan dengan ramah, informatif, dan profesional.
Toko menjual iPhone, Samsung, Xiaomi, dll dengan garansi 1 tahun.
Jam operasional 09.00-21.00 WIB.
Alamat: Jl. Raya Pusat Gadai No. 123, Jakarta Barat.
WhatsApp admin: +62 812-XXXX-XXXX (jika pelanggan ingin kontak langsung).
Katalog: https://pusatgadaiidn.pythonanywhere.com
Jawablah pertanyaan pelanggan dengan singkat, padat, dan gunakan sapaan "Kak" atau "Bapak/Ibu".`;

    try {
        const response = await axios.post(
            OPENROUTER_API_URL,
            {
                model: model,
                messages: [
                    { role: 'system', content: systemPrompt },
                    { role: 'user', content: userMessage }
                ],
                temperature: 0.7,
                max_tokens: 300
            },
            {
                headers: {
                    'Authorization': `Bearer ${apiKey}`,
                    'Content-Type': 'application/json',
                    'HTTP-Referer': 'https://pusatgadaiidn.pythonanywhere.com',
                    'X-Title': 'PusatGadaiBot'
                },
                timeout: 20000
            }
        );

        const aiText = response.data.choices?.[0]?.message?.content?.trim();
        return aiText || null;
    } catch (error) {
        console.error('[AI Admin] Gagal mendapatkan respons AI:', error.message);
        return null;
    }
};

// =============================================================================
// MESSAGE PROCESSOR (Keyword + Fallback AI)
// =============================================================================
export const processTelegramMessage = async (text, senderName, username, chatId) => {
    const textLower = (text || '').toLowerCase().trim();
    const safeName = escapeHtml(senderName) || 'Kak';

    // === SAPAAN / GREETING ===
    if (['halo', 'hai', 'hi', 'hey', 'assalamualaikum', 'hello', 'pagi', 'siang', 'sore', 'malam'].includes(textLower)) {
        return (
            `👋 <b>Halo ${safeName}!</b> Selamat datang di Pusat Gadai Indonesia!\n\n` +
            `Apa yang bisa kami bantu hari ini?\n\n` +
            `📋 <b>Menu:</b>\n` +
            `• Info produk\n` +
            `• Cara pembelian\n` +
            `• Cek status order\n` +
            `• Hubungi admin\n` +
            `• Info garansi\n` +
            `• Info pengiriman`
        );
    }

    // === INFO PRODUK ===
    if (textLower.includes('info') && textLower.includes('produk')) {
        return (
            `📱 <b>PUSAT GADAI INDONESIA</b>\n\n` +
            `Kami menyediakan gadget second berkualitas dengan harga terjangkau!\n\n` +
            `✅ <b>Keunggulan Kami:</b>\n` +
            `• Garansi 1 tahun\n` +
            `• Unit sudah diverifikasi\n` +
            `• Fullset &amp; original\n` +
            `• Harga kompetitif\n\n` +
            `🌐 <b>Katalog:</b> https://pusatgadaiidn.pythonanywhere.com\n\n` +
            `💬 Chat admin untuk info lebih lanjut!`
        );
    }

    // === CARA PEMBELIAN ===
    if ((textLower.includes('cara') && textLower.includes('beli')) || textLower.includes('pembelian')) {
        return (
            `🛍️ <b>Cara Pembelian di Pusat Gadai Indonesia:</b>\n\n` +
            `1. Hubungi Admin via WhatsApp atau Telegram.\n` +
            `2. Sebutkan merk & tipe gadget yang Anda minati.\n` +
            `3. Admin akan mengirimkan detail foto dan kondisi unit.\n` +
            `4. Pembayaran dilakukan via Transfer Bank resmi kami.\n` +
            `5. Barang dikirim ke alamat Anda atau COD di store terdekat.\n\n` +
            `Hubungi kami dengan mengetik <b>Hubungi admin</b>.`
        );
    }

    // === STATUS ORDER ===
    if ((textLower.includes('status') && textLower.includes('order')) || textLower.includes('pesanan')) {
        return (
            `📦 <b>Cek Status Order / Pesanan:</b>\n\n` +
            `Untuk memeriksa status pengiriman gadget Anda, silakan ketik format berikut:\n` +
            `<code>STATUS#NOMOR_RESI</code>\n\n` +
            `Atau langsung hubungi Admin dengan melampirkan bukti transfer Anda.`
        );
    }

    // === HUBUNGI ADMIN (dengan tombol inline) ===
    if (textLower.includes('admin') || textLower.includes('hubungi') || textLower.includes('kontak')) {
        const replyMarkup = {
            inline_keyboard: [
                [
                    {
                        text: "💬 Chat Admin Telegram",
                        url: "https://t.me/admin_pusatgadai" // ganti username admin
                    }
                ],
                [
                    {
                        text: "📱 WhatsApp Admin",
                        url: "https://wa.me/62812XXXX?text=Halo%20Admin%20Pusat%20Gadai"
                    }
                ]
            ]
        };

        return {
            text: (
                `💬 <b>Hubungi Admin / Customer Service:</b>\n\n` +
                `Kami siap melayani Anda setiap hari (09.00 - 21.00 WIB).\n\n` +
                `Silakan pilih cara menghubungi admin di bawah ini:`
            ),
            reply_markup: replyMarkup
        };
    }

    // === INFO GARANSI ===
    if (textLower.includes('garansi')) {
        return (
            `🛡️ <b>Informasi Garansi Gadget:</b>\n\n` +
            `Setiap pembelian unit di toko kami mendapatkan perlindungan:\n` +
            `• <b>Garansi Toko:</b> 1 bulan (ganti unit jika cacat pabrik).\n` +
            `• <b>Garansi Service:</b> 1 tahun (biaya jasa perbaikan gratis).\n\n` +
            `<i>*Garansi tidak berlaku jika unit jatuh, terkena air, atau segel rusak.</i>`
        );
    }

    // === INFO PENGIRIMAN ===
    if (textLower.includes('kirim') || textLower.includes('pengiriman')) {
        return (
            `🚚 <b>Informasi Pengiriman:</b>\n\n` +
            `• Kami menggunakan ekspedisi J&T, JNE, dan SiCepat.\n` +
            `• Setiap pengiriman wajib menggunakan <b>Asuransi &amp; Packing Kayu</b>.\n` +
            `• Order sebelum jam 16.00 WIB akan dikirim di hari yang sama.\n` +
            `• Resi otomatis akan dibagikan H+1 setelah pengiriman.`
        );
    }

    // === HARGA / LOKASI ===
    if (textLower.includes('harga') || textLower.includes('biaya')) {
        return `💰 Harga produk bervariasi tergantung tipe dan kondisi fisik unit (90-99%). Cek katalog lengkap kami di: https://pusatgadaiidn.pythonanywhere.com`;
    }

    if (textLower.includes('lokasi') || textLower.includes('alamat') || textLower.includes('toko')) {
        return `📍 <b>Lokasi Store Kami:</b>\n\nJl. Raya Pusat Gadai No. 123, Jakarta Barat.\nJam Operasional: 09.00 - 21.00 WIB.`;
    }

    // === FALLBACK AI ===
    const aiReply = await getAIResponse(text, safeName);
    if (aiReply) {
        return aiReply;
    }

    // === FALLBACK DEFAULT (jika AI tidak tersedia) ===
    return (
        `🤖 Halo ${safeName}, pesan Anda telah kami terima.\n\n` +
        `Ketik salah satu kata kunci di bawah ini untuk bantuan cepat:\n` +
        `• <b>Info produk</b>\n` +
        `• <b>Cara pembelian</b>\n` +
        `• <b>Cek status order</b>\n` +
        `• <b>Hubungi admin</b>\n` +
        `• <b>Info garansi</b>\n` +
        `• <b>Info pengiriman</b>`
    );
};

// =============================================================================
// TELEGRAM API OUTBOUND CALLS
// =============================================================================
export const sendTelegramMessage = async (chatId, text, replyMarkup = null) => {
    const token = getBotToken();
    if (!token) {
        console.error("[Shop Bot] Telegram bot token tidak dikonfigurasi.");
        return false;
    }

    const url = `https://api.telegram.org/bot${token}/sendMessage`;
    const payload = {
        chat_id: chatId,
        text: text,
        parse_mode: 'HTML'
    };
    if (replyMarkup) payload.reply_markup = replyMarkup;

    try {
        const response = await axios.post(url, payload, { timeout: 10000 });
        return response.status === 200;
    } catch (error) {
        console.error(`[Shop Bot] Gagal mengirim pesan ke Telegram: ${error.message}`);
        return false;
    }
};

export const sendTelegramPhoto = async (chatId, photoUrl, caption = "") => {
    const token = getBotToken();
    if (!token) {
        console.error("[Shop Bot] Telegram bot token tidak dikonfigurasi.");
        return false;
    }

    const url = `https://api.telegram.org/bot${token}/sendPhoto`;
    const payload = {
        chat_id: chatId,
        photo: photoUrl,
        caption: caption,
        parse_mode: 'HTML'
    };

    try {
        const response = await axios.post(url, payload, { timeout: 15000 });
        return response.status === 200;
    } catch (error) {
        console.error(`[Shop Bot] Gagal mengirim foto ke Telegram: ${error.message}`);
        return false;
    }
};

export const answerCallbackQuery = async (callbackQueryId, text = "") => {
    const token = getBotToken();
    if (!token) return false;

    const url = `https://api.telegram.org/bot${token}/answerCallbackQuery`;
    const payload = { callback_query_id: callbackQueryId };
    if (text) payload.text = text;

    try {
        const response = await axios.post(url, payload, { timeout: 5000 });
        return response.status === 200;
    } catch (error) {
        console.error(`[Shop Bot] Gagal menjawab callback query: ${error.message}`);
        return false;
    }
};

// =============================================================================
// EXPRESS WEBHOOK ROUTE REGISTER
// =============================================================================
export const registerTelegramShopBot = (app) => {
    const webhookPath = '/webhook/telegram';

    const routes = app._router ? app._router.stack : [];
    const isRegistered = routes.some(r => r.route && r.route.path === webhookPath);
    if (isRegistered) {
        console.warn(`[Shop Bot] Endpoint ${webhookPath} sudah terdaftar sebelumnya.`);
        return;
    }

    app.post(webhookPath, async (req, res) => {
        const ipAddr = req.headers['x-forwarded-for'] || req.socket.remoteAddress || 'unknown';
        if (!isAllowed(ipAddr, 60, 60000)) {
            console.warn(`[Shop Bot] Rate limit terlampaui untuk IP: ${ipAddr}`);
            return res.status(429).json({ status: "error", message: "Too many requests" });
        }

        const configuredSecret = getSetting('telegram_webhook_secret', '').trim();
        if (configuredSecret) {
            const incomingSecret = req.headers['x-telegram-bot-api-secret-token'] || '';
            const bufIncoming = Buffer.from(incomingSecret);
            const bufConfigured = Buffer.from(configuredSecret);

            if (bufIncoming.length !== bufConfigured.length || !crypto.timingSafeEqual(bufIncoming, bufConfigured)) {
                console.warn(`[Shop Bot] Invalid secret token dari IP: ${ipAddr}`);
                return res.status(403).json({ status: "error", message: "Unauthorized" });
            }
        }

        const update = req.body || {};

        // Callback Query
        if (update.callback_query) {
            const cb = update.callback_query;
            const cbId = cb.id;
            const cbData = cb.data || '';
            const chatObj = cb.message?.chat || {};
            const chatId = chatObj.id;

            const fromUser = cb.from || {};
            const firstName = fromUser.first_name || '';
            const lastName = fromUser.last_name || '';
            const senderName = `${firstName} ${lastName}`.trim() || 'Kak';
            const username = fromUser.username || '';

            if (chatId) {
                const replyContent = await processTelegramMessage(cbData, senderName, username, chatId);
                if (replyContent) {
                    let replyText, replyMarkup = null;
                    if (typeof replyContent === 'string') {
                        replyText = replyContent;
                    } else if (replyContent && typeof replyContent === 'object') {
                        replyText = replyContent.text;
                        replyMarkup = replyContent.reply_markup;
                    }
                    await sendTelegramMessage(chatId, replyText, replyMarkup);
                }
            }

            if (cbId) {
                await answerCallbackQuery(cbId);
            }

            return res.status(200).send("OK");
        }

        // Pesan Teks
        if (update.message) {
            const message = update.message;
            const chatId = message.chat?.id;
            const text = message.text || '';

            if (!chatId) {
                return res.status(200).send("OK");
            }

            const fromUser = message.from || {};
            const firstName = fromUser.first_name || '';
            const lastName = fromUser.last_name || '';
            const senderName = `${firstName} ${lastName}`.trim() || 'Kak';
            const username = fromUser.username || '';

            const replyContent = await processTelegramMessage(text, senderName, username, chatId);
            if (replyContent) {
                let replyText, replyMarkup = null;
                if (typeof replyContent === 'string') {
                    replyText = replyContent;
                } else if (replyContent && typeof replyContent === 'object') {
                    replyText = replyContent.text;
                    replyMarkup = replyContent.reply_markup;
                }
                await sendTelegramMessage(chatId, replyText, replyMarkup);
            }

            return res.status(200).send("OK");
        }

        return res.status(200).send("OK");
    });

    console.log(`[Shop Bot] Endpoint ${webhookPath} berhasil terdaftar di Express.`);
};