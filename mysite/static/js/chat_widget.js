/* ==========================================================================
   JAGA AI CHAT WIDGET — 2026 Professional Edition Controller
   Streaming · SSE · Voice · Offline Queue · Cost Tracking · Telemetry
   Synced with: customer_chat.py · models.py (chat models) · app.py (api routes)
   ========================================================================== */

const cwApp = (() => {
  'use strict';

  // ===== 2026 PRO CONFIGURATION =====
  const CONFIG = {
    API_ENDPOINT:  '/api/public/chat',
    API_STREAM:    '/api/public/chat/stream',
    API_CLEAR:     '/api/public/chat/clear',
    API_HISTORY:   '/api/public/chat/history',
    SESSION_KEY:   'cw_session_id',
    OPENED_KEY:    'cw_was_opened',
    PANEL_OPEN_KEY: 'cw_panel_open',
    OFFLINE_KEY:   'cw_offline_queue',
    COST_KEY:      'cw_show_cost',
    WELCOME_DELAY: 600,
    TYPING_DELAY:  400,
    TYPING_MIN:    800,
    TYPING_MAX:    3000,
    MAX_RETRIES:   3,
    RETRY_DELAY:   1000,
    SCROLL_THRESHOLD:       100,
    STREAM_CHUNK_MS:        16,
    RECONNECT_INTERVAL:     5000,
    MAX_RECONNECT_ATTEMPTS: 5,
    VOICE_TIMEOUT:  10000,
    COST_DECIMALS:  4,
    DEBOUNCE_MS:    150,
    BATCH_SIZE:     5,
    // Rate-limit warning threshold — matches server RATE_LIMIT=20
    RATE_WARN_THRESHOLD: 3,
    MAX_MSG_LENGTH: 500   // matches server-side validation
  };

  // ===== STATE =====
  let state = {
    isOpen:            false,
    isBusy:            false,
    isStreaming:       false,
    isOnline:          navigator.onLine,
    typingEl:          null,
    sessionId:         null,
    msgCount:          0,
    quickHidden:       false,
    isNearBottom:      true,
    unreadCount:       0,
    reconnectAttempts: 0,
    streamController:  null,
    voiceRecognition:  null,
    costEnabled:       false,
    lastScrollTime:    0,
    pendingQueue:      [],
    abortController:   null,
    historyLoaded:     false,
    hasRestoredHistory: false,
    rateRemaining:     null,   // tracks remaining msgs from server header
    imageQueue:        null    // pending base64 image for multimodal send
  };

  // ===== DOM REFERENCES =====
  const $ = id => document.getElementById(id);
  const dom = {
    widget:      () => $('cwWidget'),
    panel:       () => $('cwPanel'),
    messages:    () => $('cwMessages'),
    textarea:    () => $('cwTextarea'),
    sendBtn:     () => $('cwSendBtn'),
    quickWrap:   () => $('cwQuickWrap'),
    badge:       () => $('cwBadge'),
    bubble:      () => $('cwBubble'),
    toast:       () => $('cwToast'),
    scrollBottom:() => $('cwScrollBottom'),
    costBadge:   () => $('cwCostBadge'),
    voiceBtn:    () => $('cwVoiceBtn'),
    statusDot:   () => $('cwStatusDot'),
    imageBtn:    () => $('cwImageBtn'),
    imageInput:  () => $('cwImageInput')
  };

  // ===== UTILITY FUNCTIONS =====

  function getSession() {
    if (state.sessionId) return state.sessionId;
    try {
      let sid = localStorage.getItem(CONFIG.SESSION_KEY);
      if (!sid) {
        sid = 'cw_' + (crypto.randomUUID?.() || Math.random().toString(36).slice(2) + Date.now().toString(36));
        localStorage.setItem(CONFIG.SESSION_KEY, sid);
      }
      state.sessionId = sid;
      return sid;
    } catch {
      state.sessionId = 'cw_' + Math.random().toString(36).slice(2);
      return state.sessionId;
    }
  }

  /** Generate a fresh session id (used after clearChat). */
  function newSession() {
    const sid = 'cw_' + (crypto.randomUUID?.() || Math.random().toString(36).slice(2) + Date.now().toString(36));
    state.sessionId = sid;
    try { localStorage.setItem(CONFIG.SESSION_KEY, sid); } catch {}
    return sid;
  }

  function timeStr(isoString) {
    const d = isoString ? new Date(isoString) : new Date();
    return d.toLocaleTimeString('id-ID', { hour: '2-digit', minute: '2-digit', hour12: false });
  }

  function escHtml(str) {
    if (!str) return '';
    const div = document.createElement('div');
    div.textContent = str;
    return div.innerHTML;
  }

  // ===== 2026 PRO: MARKDOWN RENDERER =====
  function renderMarkdown(raw) {
    let t = escHtml(raw);

    // Code blocks
    t = t.replace(/```(\w+)?\n([\s\S]*?)```/g, (_, lang, code) =>
      `<pre class="cw-code"><code class="language-${lang || 'text'}">${escHtml(code.trim())}</code></pre>`
    );

    // Inline code
    t = t.replace(/`([^`]+)`/g, '<code class="cw-inline-code">$1</code>');

    // URLs
    t = t.replace(/(https?:\/\/[^\s<]{6,})/g,
      '<a href="$1" target="_blank" rel="noopener noreferrer" class="cw-link">$1</a>');

    // Invoice links  (e.g. /invoice/123)
    t = t.replace(/\/invoice\/(\d+)/g,
      '<a href="/invoice/$1" target="_blank" rel="noopener noreferrer" class="cw-link">/invoice/$1</a>');

    // Product links  (e.g. /product/42)
    t = t.replace(/\/product\/(\d+)/g,
      '<a href="/product/$1" target="_blank" rel="noopener noreferrer" class="cw-link">/product/$1</a>');

    // WhatsApp
    t = t.replace(/\b(wa\.me\/[^\s<]+)/g,
      '<a href="https://$1" target="_blank" rel="noopener noreferrer" class="cw-link wa">WhatsApp Admin</a>');

    // Bold / italic / strikethrough
    t = t.replace(/\*\*(.+?)\*\*/g, '<strong>$1</strong>');
    t = t.replace(/\*(.+?)\*/g, '<em>$1</em>');
    t = t.replace(/~~(.+?)~~/g, '<del>$1</del>');

    // Ordered lists
    t = t.replace(/^\d+\.\s+(.+)$/gm, '<li>$1</li>');
    t = t.replace(/(<li>.*<\/li>\n?)+/g, '<ol class="cw-list">$&</ol>');

    // Unordered lists
    t = t.replace(/^[\-\*]\s+(.+)$/gm, '<li>$1</li>');
    t = t.replace(/(<li>.*<\/li>\n?)+/g, '<ul class="cw-list">$&</ul>');

    // Headers
    t = t.replace(/^###\s+(.+)$/gm, '<h3 class="cw-h3">$1</h3>');
    t = t.replace(/^##\s+(.+)$/gm,  '<h2 class="cw-h2">$1</h2>');
    t = t.replace(/^#\s+(.+)$/gm,   '<h1 class="cw-h1">$1</h1>');

    // Line breaks
    t = t.replace(/\n/g, '<br>');

    // Simple tables
    t = t.replace(/\|(.+)\|/g, (match) => {
      const cells = match.split('|').filter(c => c.trim());
      if (cells.length < 2) return match;
      return '<tr>' + cells.map(c => `<td>${c.trim()}</td>`).join('') + '</tr>';
    });

    return t;
  }

  // ===== 2026 PRO: SUGGESTED ACTIONS PARSER =====
  function extractSuggestedActions(text) {
    const actions = [];
    const regex = /\[ACTION:([a-z_]+)\|label=([^\]|]+)(?:\|payload=([^\]]*))?]/g;
    let match;
    while ((match = regex.exec(text)) !== null) {
      actions.push({ type: match[1], label: match[2], payload: match[3] || '' });
    }
    return actions;
  }

  function stripActionTags(text) {
    return text.replace(/\[ACTION:[^\]]+]/g, '').trim();
  }

  // ===== 2026 PRO: COST FORMATTER =====
  function formatCost(usd) {
    if (!usd || !state.costEnabled) return '';
    const idr = (usd * 16000).toFixed(0);
    return `<span class="cw-cost-badge cw-visible" title="Estimasi biaya API">💰 $${usd.toFixed(CONFIG.COST_DECIMALS)} (~Rp ${Number(idr).toLocaleString('id-ID')})</span>`;
  }

  // ===== 2026 PRO: SOURCE BADGE =====
  // Maps customer_chat.py `source` field → UI label
  const SOURCE_LABELS = {
    ai:           '',                // normal, no badge
    fallback:     '🔄 mode fallback',
    local:        '🗄️ mode lokal',
    rate_limited: '⚠️ rate limited'
  };

  function formatSourceBadge(source) {
    const label = SOURCE_LABELS[source];
    if (!label) return '';
    return `<span class="cw-source-badge" title="Sumber respons">${escHtml(label)}</span>`;
  }

  // ===== SCROLL & LAYOUT =====

  function isNearBottom() {
    const m = dom.messages();
    if (!m) return true;
    return m.scrollHeight - m.scrollTop - m.clientHeight < CONFIG.SCROLL_THRESHOLD;
  }

  function scrollBottom(force = false) {
    const m = dom.messages();
    if (!m) return;
    if (force || state.isNearBottom) {
      requestAnimationFrame(() => {
        m.scrollTop = m.scrollHeight;
        state.isNearBottom = true;
        updateScrollButton();
      });
    }
  }

  function updateScrollButton() {
    const btn = dom.scrollBottom();
    if (!btn) return;
    btn.classList.toggle('cw-visible', !state.isNearBottom && state.msgCount > 3);
  }

  function handleScroll() {
    const now = Date.now();
    if (now - state.lastScrollTime < CONFIG.DEBOUNCE_MS) return;
    state.lastScrollTime = now;
    const m = dom.messages();
    if (!m) return;
    state.isNearBottom = isNearBottom();
    updateScrollButton();
  }

  // ===== 2026 PRO: MESSAGE COMPONENTS =====

  function createMessageEl(role, text, options = {}) {
    const el = document.createElement('div');
    el.className = `cw-msg cw-msg-${role === 'user' ? 'user' : 'bot'}`;
    el.setAttribute('role', 'listitem');
    if (options.id) el.dataset.msgId = options.id;

    const time = timeStr(options.created_at || null);
    const costHtml   = options.cost   ? formatCost(options.cost)          : '';
    const sourceHtml = options.source ? formatSourceBadge(options.source) : '';

    const actionsHtml = options.suggestedActions?.length
      ? `<div class="cw-suggested-actions">${options.suggestedActions.map(a =>
          `<button class="cw-action-btn" data-action="${a.type}" data-payload="${escHtml(a.payload)}">${escHtml(a.label)}</button>`
        ).join('')}</div>`
      : '';

    if (options.isError) el.classList.add('cw-error-msg');

    if (role === 'bot') {
      el.innerHTML = `
        <div class="cw-msg-ava" aria-hidden="true">J</div>
        <div class="cw-msg-body">
          <div class="cw-msg-bubble">
            <div class="cw-msg-text">${options.isRaw ? text : renderMarkdown(text)}</div>
            ${actionsHtml}
          </div>
          <div class="cw-msg-meta">
            <span class="cw-msg-time">${time}</span>
            ${costHtml}
            ${sourceHtml}
            ${options.model ? `<span class="cw-model-badge">${escHtml(options.model)}</span>` : ''}
          </div>
        </div>
      `;
    } else {
      // user bubble — show image thumbnail if provided
      const imgHtml = options.imageUrl
        ? `<img src="${escHtml(options.imageUrl)}" class="cw-img-thumb" alt="Gambar">`
        : '';
      el.innerHTML = `
        <div class="cw-msg-body">
          <div class="cw-msg-bubble">
            ${imgHtml}
            <div class="cw-msg-text">${escHtml(text)}</div>
          </div>
          <span class="cw-msg-time">${time}</span>
        </div>
      `;
    }

    // Attach action handlers
    el.querySelectorAll('.cw-action-btn').forEach(btn => {
      btn.addEventListener('click', () => {
        const payload    = btn.dataset.payload;
        const actionType = btn.dataset.action;
        if (actionType === 'quick_reply') {
          sendMsg(payload);
        } else if (actionType === 'url') {
          window.open(payload, '_blank');
        }
      });
    });

    return el;
  }

  function addMsg(role, text, options = {}) {
    const m = dom.messages();
    if (!m) return null;
    const el = createMessageEl(role, text, options);
    m.appendChild(el);
    state.msgCount++;
    if (!options.skipScroll && state.isNearBottom) scrollBottom();
    return el;
  }

  // ===== 2026 PRO: STREAMING MESSAGE =====

  function createStreamingMessage() {
    const m = dom.messages();
    if (!m) return null;

    const el = document.createElement('div');
    el.className = 'cw-msg cw-msg-bot cw-streaming';
    el.dataset.streamId = 'stream_' + Date.now();
    el.innerHTML = `
      <div class="cw-msg-ava" aria-hidden="true">J</div>
      <div class="cw-msg-body">
        <div class="cw-msg-bubble">
          <div class="cw-msg-text"></div>
        </div>
        <div class="cw-msg-meta">
          <span class="cw-msg-time">${timeStr()}</span>
          <span class="cw-streaming-indicator">●</span>
        </div>
      </div>
    `;
    m.appendChild(el);
    scrollBottom(true);
    return el;
  }

  function updateStreamingMessage(el, chunk, isDone = false, options = {}) {
    if (!el) return;
    const textEl = el.querySelector('.cw-msg-text');
    if (!textEl) return;

    const current = textEl.dataset.raw || '';
    const updated = current + chunk;
    textEl.dataset.raw = updated;

    if (isDone) {
      textEl.innerHTML = renderMarkdown(updated);
      el.classList.remove('cw-streaming');

      const actions = extractSuggestedActions(updated);
      if (actions.length) {
        const cleanText = stripActionTags(updated);
        textEl.innerHTML = renderMarkdown(cleanText);
        const actionsDiv = document.createElement('div');
        actionsDiv.className = 'cw-suggested-actions';
        actionsDiv.innerHTML = actions.map(a =>
          `<button class="cw-action-btn" data-action="${a.type}" data-payload="${escHtml(a.payload)}">${escHtml(a.label)}</button>`
        ).join('');
        el.querySelector('.cw-msg-bubble').appendChild(actionsDiv);
        actionsDiv.querySelectorAll('.cw-action-btn').forEach(btn => {
          btn.addEventListener('click', () => {
            if (btn.dataset.action === 'quick_reply') sendMsg(btn.dataset.payload);
          });
        });
      }

      const meta = el.querySelector('.cw-msg-meta');
      if (meta) {
        if (options.cost)   meta.insertAdjacentHTML('beforeend', formatCost(options.cost));
        if (options.source) meta.insertAdjacentHTML('beforeend', formatSourceBadge(options.source));
        if (options.model)  meta.insertAdjacentHTML('beforeend', `<span class="cw-model-badge">${escHtml(options.model)}</span>`);
      }

      scrollBottom(true);
    } else {
      textEl.textContent = updated + '▋';
      if (state.isNearBottom) scrollBottom(true);
    }
  }

  // ===== TYPING INDICATOR =====

  function showTyping() {
    if (state.typingEl) return;
    const m = dom.messages();
    if (!m) return;
    state.typingEl = document.createElement('div');
    state.typingEl.className = 'cw-msg cw-msg-bot cw-typing';
    state.typingEl.setAttribute('aria-label', 'Asisten sedang mengetik...');
    state.typingEl.innerHTML = `
      <div class="cw-msg-ava" aria-hidden="true">J</div>
      <div class="cw-msg-body">
        <div class="cw-typing-dots"><span class="cw-typing-dot"></span><span class="cw-typing-dot"></span><span class="cw-typing-dot"></span></div>
      </div>
    `;
    m.appendChild(state.typingEl);
    scrollBottom(true);
  }

  function hideTyping() {
    if (state.typingEl) { state.typingEl.remove(); state.typingEl = null; }
  }

  function simulateTyping(text, onComplete) {
    const delay = Math.min(CONFIG.TYPING_MAX, Math.max(CONFIG.TYPING_MIN, text.length * 15));
    showTyping();
    setTimeout(() => { hideTyping(); onComplete(text); }, delay);
  }

  // ===== 2026 PRO: OFFLINE QUEUE =====
  function queueMessage(text) {
    state.pendingQueue.push({ text, timestamp: Date.now(), retries: 0 });
    saveOfflineQueue();
    toast('📴 Offline — pesan ditambahkan ke antrean');
  }

  function saveOfflineQueue() {
    try { localStorage.setItem(CONFIG.OFFLINE_KEY, JSON.stringify(state.pendingQueue)); } catch {}
  }

  function loadOfflineQueue() {
    try {
      const raw = localStorage.getItem(CONFIG.OFFLINE_KEY);
      if (raw) state.pendingQueue = JSON.parse(raw);
    } catch { state.pendingQueue = []; }
  }

  async function flushOfflineQueue() {
    if (!state.pendingQueue.length || !state.isOnline) return;
    const queue = [...state.pendingQueue];
    state.pendingQueue = [];
    saveOfflineQueue();
    for (const item of queue.slice(0, CONFIG.BATCH_SIZE)) {
      await sendMsg(item.text);
      await new Promise(r => setTimeout(r, 500));
    }
    if (queue.length > CONFIG.BATCH_SIZE) {
      state.pendingQueue = queue.slice(CONFIG.BATCH_SIZE);
      saveOfflineQueue();
      toast(`⏳ ${state.pendingQueue.length} pesan masih dalam antrean`);
    } else {
      toast('✓ Semua pesan offline terkirim');
    }
  }

  // ===== 2026 PRO: IMAGE / MULTIMODAL SUPPORT =====
  // Matches the `image` field in /api/public/chat (customer_chat.py multimodal).

  function initImageUpload() {
    const btn   = dom.imageBtn();
    const input = dom.imageInput();
    if (!btn || !input) return;

    btn.addEventListener('click', () => input.click());
    input.addEventListener('change', (e) => {
      const file = e.target.files?.[0];
      if (!file) return;
      if (!file.type.startsWith('image/')) { toast('❌ File harus berupa gambar'); return; }
      if (file.size > 5 * 1024 * 1024)    { toast('❌ Ukuran gambar maksimal 5 MB'); return; }

      const reader = new FileReader();
      reader.onload = (ev) => {
        state.imageQueue = ev.target.result; // data URL (base64)
        toast('🖼️ Gambar siap dikirim — ketik pesan lalu kirim');
        btn.classList.add('cw-image-queued');
      };
      reader.readAsDataURL(file);
      input.value = ''; // reset so same file can be picked again
    });
  }

  function consumeImageQueue() {
    const img = state.imageQueue;
    state.imageQueue = null;
    dom.imageBtn()?.classList.remove('cw-image-queued');
    return img; // base64 data URL or null
  }

  // ===== 2026 PRO: VOICE INPUT =====
  function initVoice() {
    const btn = dom.voiceBtn();
    if (!btn) return;
    if (!('webkitSpeechRecognition' in window) && !('SpeechRecognition' in window)) {
      btn.style.display = 'none';
      return;
    }
    const SpeechRecognition = window.SpeechRecognition || window.webkitSpeechRecognition;
    state.voiceRecognition = new SpeechRecognition();
    state.voiceRecognition.lang = 'id-ID';
    state.voiceRecognition.continuous = false;
    state.voiceRecognition.interimResults = true;

    state.voiceRecognition.onstart = () => { btn.classList.add('cw-voice-active'); toast('🎤 Mendengarkan...'); };
    state.voiceRecognition.onresult = (e) => {
      const inp = dom.textarea();
      if (!inp) return;
      let final = '', interim = '';
      for (let i = e.resultIndex; i < e.results.length; i++) {
        if (e.results[i].isFinal) final  += e.results[i][0].transcript;
        else                       interim += e.results[i][0].transcript;
      }
      inp.value = final || interim;
      handleInput();
    };
    state.voiceRecognition.onerror = (e) => {
      btn.classList.remove('cw-voice-active');
      if (e.error !== 'aborted') toast('❌ Voice error: ' + e.error);
    };
    state.voiceRecognition.onend = () => btn.classList.remove('cw-voice-active');

    btn.addEventListener('click', () => {
      if (btn.classList.contains('cw-voice-active')) {
        state.voiceRecognition.stop();
      } else {
        state.voiceRecognition.start();
        setTimeout(() => {
          if (btn.classList.contains('cw-voice-active')) state.voiceRecognition.stop();
        }, CONFIG.VOICE_TIMEOUT);
      }
    });
  }

  // ===== 2026 PRO: NETWORK STATUS =====
  function updateNetworkStatus() {
    state.isOnline = navigator.onLine;
    const dot = dom.statusDot();
    if (dot) {
      dot.classList.toggle('cw-online',  state.isOnline);
      dot.classList.toggle('cw-offline', !state.isOnline);
      dot.title = state.isOnline ? 'Online' : 'Offline';
    }
    if (state.isOnline && state.pendingQueue.length) flushOfflineQueue();
  }

  // ===== 2026 PRO: RATE LIMIT TRACKER =====
  // Updates from the `rate_limit.remaining` field returned by /api/public/chat
  // (app.py injects this for every successful response).

  function updateRateLimit(remaining) {
    if (remaining == null) return;
    state.rateRemaining = remaining;
    if (remaining <= CONFIG.RATE_WARN_THRESHOLD && remaining > 0) {
      toast(`⚠️ Sisa kuota pesan: ${remaining} lagi`);
    }
  }

  // ===== 2026 PRO: PERSISTENT HISTORY RESTORE =====
  // History messages come from SessionMemory.get_full_history() in app.py,
  // which reads ChatMessage rows (models.py). Fields: role, content,
  // model, cost_usd, created_at.  The `source` field is not stored in DB
  // history so we omit it when rendering restored messages.

  function renderHistoryMessage(msg) {
    if (!msg || !msg.content) return;
    const role = msg.role === 'user' ? 'user' : 'bot';
    addMsg(role, msg.content, {
      skipScroll:       true,
      model:            msg.model    || null,
      cost:             msg.cost_usd || 0,
      created_at:       msg.created_at || null,
      suggestedActions: role === 'bot' ? extractSuggestedActions(msg.content) : [],
      isRaw:            false
    });
  }

  async function loadHistory() {
    if (state.historyLoaded) return state.hasRestoredHistory;
    state.historyLoaded = true;

    const sid = getSession();
    try {
      const resp = await fetch(`${CONFIG.API_HISTORY}?session_id=${encodeURIComponent(sid)}`, {
        method: 'GET',
        headers: { 'X-Requested-With': 'XMLHttpRequest' }
      });
      if (!resp.ok) return false;

      const data = await resp.json();
      const messages = Array.isArray(data.messages) ? data.messages : [];
      if (!messages.length) return false;

      messages.forEach(m => {
        renderHistoryMessage(m);
        // Strip [ACTION:...] tags from restored bot bubbles — stale quick
        // replies shouldn't be clickable after a reload.
        const el = dom.messages()?.lastElementChild;
        if (el && m.role !== 'user') {
          const textEl = el.querySelector('.cw-msg-text');
          if (textEl) textEl.innerHTML = renderMarkdown(stripActionTags(m.content));
        }
      });

      hideQuick();
      state.hasRestoredHistory = true;
      scrollBottom(true);
      return true;
    } catch (err) {
      console.warn('Failed to load chat history:', err);
      return false;
    }
  }

  // ===== API FUNCTIONS =====

  async function fetchWithAbort(url, options) {
    state.abortController = new AbortController();
    try {
      return await fetch(url, { ...options, signal: state.abortController.signal });
    } finally {
      state.abortController = null;
    }
  }

  async function callAPI(text, retries = 0) {
    const image = consumeImageQueue(); // take any queued image
    try {
      const body = { message: text, session_id: getSession() };
      if (image) body.image = image; // multimodal: matches api/customer_chat.py image param

      const resp = await fetchWithAbort(CONFIG.API_ENDPOINT, {
        method: 'POST',
        headers: {
          'Content-Type': 'application/json',
          'X-Requested-With': 'XMLHttpRequest'
        },
        body: JSON.stringify(body)
      });

      if (!resp.ok) throw new Error(`HTTP ${resp.status}`);
      const data = await resp.json();

      // Persist rate limit info from server (app.py adds rate_limit.remaining)
      updateRateLimit(data.rate_limit?.remaining ?? null);

      return { ...data, _imageUrl: image }; // pass through for user bubble
    } catch (err) {
      if (err.name === 'AbortError') throw err;
      if (retries < CONFIG.MAX_RETRIES) {
        await new Promise(r => setTimeout(r, CONFIG.RETRY_DELAY * (retries + 1)));
        return callAPI(text, retries + 1);
      }
      throw err;
    }
  }

  // 2026 PRO: SSE Streaming
  async function streamAPI(text) {
    return new Promise((resolve, reject) => {
      const el = createStreamingMessage();
      let fullText = '', metadata = {};

      const evtSource = new EventSource(
        `${CONFIG.API_STREAM}?message=${encodeURIComponent(text)}&session_id=${encodeURIComponent(getSession())}`
      );
      state.streamController = evtSource;

      evtSource.onmessage = (e) => {
        try {
          const data = JSON.parse(e.data);
          if (data.type === 'chunk') {
            fullText += data.content;
            updateStreamingMessage(el, data.content, false);
          } else if (data.type === 'done') {
            metadata = data.meta || {};
            updateStreamingMessage(el, '', true, {
              cost:   metadata.cost_usd,
              model:  metadata.model,
              source: metadata.source
            });
            evtSource.close();
            state.streamController = null;
            updateRateLimit(metadata.rate_limit?.remaining ?? null);
            resolve({ reply: fullText, ...metadata });
          } else if (data.type === 'error') {
            throw new Error(data.error);
          }
        } catch (err) {
          if (err.message !== 'Unexpected end of JSON input') console.error('Stream parse error:', err);
        }
      };

      evtSource.onerror = () => {
        evtSource.close();
        state.streamController = null;
        if (fullText) {
          updateStreamingMessage(el, '', true, metadata);
          resolve({ reply: fullText, partial: true, ...metadata });
        } else {
          reject(new Error('Stream connection failed'));
        }
      };

      setTimeout(() => {
        if (state.streamController === evtSource) {
          evtSource.close();
          state.streamController = null;
          if (fullText) { updateStreamingMessage(el, '', true, metadata); resolve({ reply: fullText, partial: true, ...metadata }); }
          else          { reject(new Error('Stream timeout')); }
        }
      }, 30000);
    });
  }

  // 2026 PRO: Fallback streaming via fetch
  async function streamFetchAPI(text) {
    const resp = await fetchWithAbort(CONFIG.API_STREAM, {
      method: 'POST',
      headers: {
        'Content-Type': 'application/json',
        'X-Requested-With': 'XMLHttpRequest',
        'Accept': 'text/event-stream'
      },
      body: JSON.stringify({ message: text, session_id: getSession() })
    });
    if (!resp.ok) throw new Error(`HTTP ${resp.status}`);

    const reader  = resp.body.getReader();
    const decoder = new TextDecoder();
    const el      = createStreamingMessage();
    let fullText = '', metadata = {};

    try {
      while (true) {
        const { done, value } = await reader.read();
        if (done) break;
        const chunk = decoder.decode(value, { stream: true });
        for (const line of chunk.split('\n')) {
          if (!line.startsWith('data: ')) continue;
          const dataStr = line.slice(6);
          if (dataStr === '[DONE]') {
            updateStreamingMessage(el, '', true, { cost: metadata.cost_usd, model: metadata.model, source: metadata.source });
            updateRateLimit(metadata.rate_limit?.remaining ?? null);
            return { reply: fullText, ...metadata };
          }
          try {
            const d = JSON.parse(dataStr);
            if (d.type === 'chunk') { fullText += d.content; updateStreamingMessage(el, d.content, false); }
            else if (d.type === 'meta') { metadata = d; }
          } catch {}
        }
      }
    } finally { reader.releaseLock(); }

    updateStreamingMessage(el, '', true, metadata);
    updateRateLimit(metadata.rate_limit?.remaining ?? null);
    return { reply: fullText, ...metadata };
  }

  // ===== 2026 PRO: SEND WITH STREAMING =====
  async function sendMsg(customText, options = {}) {
    const inp  = dom.textarea();
    const text = (customText !== undefined ? customText : inp?.value || '').trim();

    if (!text || state.isBusy) return;

    // Client-side length guard (mirrors server 500-char limit)
    if (text.length > CONFIG.MAX_MSG_LENGTH) {
      toast(`❌ Pesan terlalu panjang (maks. ${CONFIG.MAX_MSG_LENGTH} karakter)`);
      return;
    }

    state.isBusy = true;
    if (customText === undefined && inp) { inp.value = ''; resetTextarea(); }
    updateSendBtn();
    hideQuick();

    const imageUrl = state.imageQueue; // save for user bubble before consuming
    addMsg('user', text, { imageUrl: imageUrl || null });

    if (!state.isOnline) {
      queueMessage(text);
      state.isBusy = false;
      updateSendBtn();
      return;
    }

    // Streaming endpoints (SSE / fetch-stream) don't carry the queued image —
    // route multimodal messages through the plain JSON endpoint (callAPI),
    // which does handle `image`, instead of silently dropping it.
    const useStream = options.stream !== false && !!window.EventSource && !imageUrl;

    try {
      let data;

      if (useStream) {
        try {
          data = await streamAPI(text);
        } catch (streamErr) {
          console.warn('SSE failed, trying fetch stream:', streamErr);
          try {
            data = await streamFetchAPI(text);
          } catch (fetchErr) {
            console.warn('Fetch stream failed, falling back to standard:', fetchErr);
            data = await callAPI(text);
            data = { ...data, _fallback: true };
          }
        }
      } else {
        data = await callAPI(text);
        data = { ...data, _fallback: true };
      }

      if (data._fallback) {
        // Standard (non-streaming) response — simulate typing, then render
        const reply   = data.reply || '😔 Tidak ada respons.';
        const source  = data.source || 'ai';
        const model   = data.model  || '';
        const costUsd = data.cost_usd || 0;

        simulateTyping(reply, (r) => {
          addMsg('bot', r, {
            cost:             costUsd,
            model:            model,
            source:           source,
            suggestedActions: extractSuggestedActions(r)
          });
        });

        // Handle tool_calls metadata (from customer_chat.py CustomerChatResult)
        if (data.tool_calls?.length) {
          logEvent('chat_tool_calls', { count: data.tool_calls.length, model });
        }

        // Handle reasoning field (future models)
        if (data.reasoning) {
          console.debug('[chat] reasoning tokens:', data.reasoning.slice(0, 200));
        }
      }

      // Unread badge if panel closed
      if (!state.isOpen) { state.unreadCount++; updateBadge(); }

      // Persist session_id returned by server if it differs
      if (data.session_id && data.session_id !== state.sessionId) {
        state.sessionId = data.session_id;
        try { localStorage.setItem(CONFIG.SESSION_KEY, data.session_id); } catch {}
      }

      logEvent('chat_message_sent', {
        model:    data.model    || 'unknown',
        cost_usd: data.cost_usd || 0,
        source:   data.source   || 'ai',
        cached:   data.cached   || false
      });

    } catch (err) {
      if (err.name === 'AbortError') {
        addMsg('bot', '⏹️ Permintaan dibatalkan.', { isError: true });
      } else {
        hideTyping();
        addMsg('bot', '😔 Koneksi bermasalah. Periksa internet Anda dan coba lagi.', { isError: true });
        console.error('Chat API error:', err);
      }
    } finally {
      state.isBusy = false;
      updateSendBtn();
      if (inp) inp.focus();
    }
  }

  // ===== UI HELPERS =====

  function resetTextarea() {
    const inp = dom.textarea();
    if (inp) {
      inp.style.height = 'auto';
      inp.style.height = Math.min(inp.scrollHeight, 100) + 'px';
    }
  }

  function updateSendBtn() {
    const btn = dom.sendBtn();
    const inp = dom.textarea();
    if (btn) {
      btn.disabled = !inp?.value?.trim() || state.isBusy;
      btn.classList.toggle('cw-streaming-btn', state.isStreaming);
    }
  }

  function handleInput() {
    const inp = dom.textarea();
    if (inp) {
      inp.style.height = 'auto';
      inp.style.height = Math.min(inp.scrollHeight, 100) + 'px';
    }
    updateSendBtn();
  }

  function handleKey(e) {
    if (e.key === 'Enter' && !e.shiftKey) { e.preventDefault(); sendMsg(); }
    if (e.key === 'Escape' && state.isOpen) toggle();
    if ((e.ctrlKey || e.metaKey) && e.key === 'k') {
      e.preventDefault();
      if (!state.isOpen) toggle();
      dom.textarea()?.focus();
    }
  }

  // ===== PANEL TOGGLE =====

  function toggle() {
    state.isOpen = !state.isOpen;
    const w  = dom.widget();
    const bb = dom.bubble();

    if (state.isOpen) {
      w?.classList.add('cw-open');
      dom.panel()?.setAttribute('aria-hidden', 'false');
      bb?.setAttribute('aria-expanded', 'true');
      hideBadge();
      try {
        localStorage.setItem(CONFIG.OPENED_KEY, '1');
        localStorage.setItem(CONFIG.PANEL_OPEN_KEY, '1');
      } catch {}
      setTimeout(() => { dom.textarea()?.focus(); scrollBottom(true); }, 360);
    } else {
      w?.classList.remove('cw-open');
      dom.panel()?.setAttribute('aria-hidden', 'true');
      bb?.setAttribute('aria-expanded', 'false');
      try { localStorage.setItem(CONFIG.PANEL_OPEN_KEY, '0'); } catch {}
    }
  }

  // BUG FIX: sebelumnya panel SELALU mulai tertutup (state.isOpen = false)
  // setiap kali halaman di-refresh, walaupun history percakapan sendiri
  // sudah persisten di server (loadHistory). Jadi dari sudut pandang user
  // terlihat seperti "chat hilang" karena widget balik jadi bubble kecil
  // padahal isi chat sebenarnya masih ada di baliknya.
  // Fix: simpan status buka/tutup panel di localStorage (PANEL_OPEN_KEY),
  // dan kalau terakhir kali panel dalam keadaan terbuka, buka lagi panel
  // itu SEBELUM history di-load — supaya begitu history selesai di-fetch,
  // user langsung melihatnya tanpa perlu klik bubble lagi.
  function restorePanelState() {
    let wasOpen = false;
    try { wasOpen = localStorage.getItem(CONFIG.PANEL_OPEN_KEY) === '1'; } catch {}
    if (!wasOpen) return;

    state.isOpen = true;
    dom.widget()?.classList.add('cw-open');
    dom.panel()?.setAttribute('aria-hidden', 'false');
    dom.bubble()?.setAttribute('aria-expanded', 'true');
    hideBadge();
  }

  // ===== CLEAR CHAT =====

  async function clearChat() {
    if (state.streamController) { state.streamController.close(); state.streamController = null; }
    if (state.abortController)  { state.abortController.abort(); }

    const m = dom.messages();
    if (m) m.innerHTML = '';

    state.msgCount     = 0;
    state.quickHidden  = false;
    state.unreadCount  = 0;
    state.isBusy       = false;
    state.imageQueue   = null;
    dom.imageBtn()?.classList.remove('cw-image-queued');
    updateBadge();
    showQuick();

    // Tell server to archive the old session (app.py: SessionMemory.clear)
    try {
      await fetch(CONFIG.API_CLEAR, {
        method:  'POST',
        headers: { 'Content-Type': 'application/json' },
        body:    JSON.stringify({ session_id: getSession() })
      });
    } catch {}

    // Immediately create a new session so the next conversation is isolated
    newSession();

    state.historyLoaded     = false;
    state.hasRestoredHistory = false;
    state.rateRemaining     = null;

    toast('✓ Percakapan baru dimulai');
    setTimeout(() => showWelcome(), 300);
  }

  // ===== WELCOME =====

  function showWelcome() {
    // Quick replies match the tools registered in customer_chat.py:
    // cek_resi · order_status · generate_invoice · cek_status_cicilan · cek_status_kyc
    const welcomeText =
      '👋 Halo! Saya **Jaga**, asisten AI Pusat Gadai Indonesia.\n\n' +
      'Saya siap membantu Anda dengan:\n' +
      '📦 Cek resi & status pengiriman\n' +
      '🛒 Status & detail pesanan\n' +
      '🧾 Invoice / kwitansi\n' +
      '💳 Status pengajuan cicilan / kredit\n' +
      '🔐 Status verifikasi KYC\n' +
      '📱 Info produk & harga\n\n' +
      'Silakan pilih bantuan di bawah atau ketik pertanyaan Anda 😊\n\n' +
      '';

    simulateTyping(welcomeText, (text) => {
      addMsg('bot', text, { suggestedActions: extractSuggestedActions(text) });
    });
  }

  // ===== QUICK REPLIES =====

  function hideQuick() {
    if (state.quickHidden) return;
    const q = dom.quickWrap();
    if (q) {
      q.style.transition = 'opacity 0.3s ease, max-height 0.35s ease, padding 0.3s ease';
      q.style.opacity = '0';
      q.style.maxHeight = '0';
      q.style.paddingTop = '0';
      q.style.paddingBottom = '0';
      q.style.overflow = 'hidden';
      setTimeout(() => { q.style.display = 'none'; }, 350);
    }
    state.quickHidden = true;
  }

  function showQuick() {
    const q = dom.quickWrap();
    if (!q) return;
    q.style.display = '';
    q.style.opacity = '1';
    q.style.maxHeight = '';
    q.style.paddingTop = '';
    q.style.paddingBottom = '';
    state.quickHidden = false;
  }

  // ===== TOAST & BADGE =====

  function toast(msg) {
    let t = dom.toast();
    if (!t) {
      t = document.createElement('div');
      t.className = 'cw-toast';
      t.id = 'cwToast';
      dom.panel()?.appendChild(t);
    }
    t.textContent = msg;
    t.classList.add('cw-toast-visible');
    setTimeout(() => t.classList.remove('cw-toast-visible'), 2500);
  }

  function updateBadge() {
    const b = dom.badge();
    if (!b) return;
    if (state.unreadCount > 0) {
      b.textContent = Math.min(state.unreadCount, 99);
      b.classList.add('cw-visible');
    } else {
      b.classList.remove('cw-visible');
    }
  }

  function showBadge()  { state.unreadCount = 1; updateBadge(); }
  function hideBadge()  { state.unreadCount = 0; updateBadge(); }

  // ===== 2026 PRO: COST TOGGLE =====
  function toggleCostDisplay() {
    state.costEnabled = !state.costEnabled;
    try { localStorage.setItem(CONFIG.COST_KEY, state.costEnabled ? '1' : '0'); } catch {}
    toast(state.costEnabled ? '💰 Tampilan biaya API diaktifkan' : '💰 Tampilan biaya API dimatikan');
    updateCostBadge();
  }

  function updateCostBadge() {
    const badge = dom.costBadge();
    if (badge) badge.classList.toggle('cw-visible', state.costEnabled);
  }

  // ===== 2026 PRO: TELEMETRY =====
  function logEvent(event, data = {}) {
    if (window.gtag)     gtag('event', event, { event_category: 'chat_widget', event_label: data.model || 'unknown', value: data.cost_usd ? Math.round(data.cost_usd * 1_000_000) : 0, ...data });
    if (window.plausible) plausible(event, { props: data });
  }

  // ===== INITIALIZATION =====

  function init() {
    const inp = dom.textarea();
    const m   = dom.messages();

    if (inp) {
      inp.addEventListener('keydown', handleKey);
      inp.addEventListener('input',   handleInput);
    }
    if (m) m.addEventListener('scroll', handleScroll, { passive: true });

    // Load preferences
    try {
      const wasOpened = localStorage.getItem(CONFIG.OPENED_KEY);
      if (!wasOpened) showBadge();
      state.costEnabled = localStorage.getItem(CONFIG.COST_KEY) === '1';
    } catch {}

    // Reopen panel immediately if it was left open before the refresh —
    // do this before history loads so there's no flash of the closed bubble.
    restorePanelState();

    loadOfflineQueue();
    initImageUpload();

    window.addEventListener('online',  updateNetworkStatus);
    window.addEventListener('offline', updateNetworkStatus);
    updateNetworkStatus();

    initVoice();

    // Restore persisted DB-backed history (models.py ChatMessage).
    // If the session has prior messages, show them instead of the welcome screen.
    loadHistory().then(restored => {
      if (!restored) setTimeout(showWelcome, CONFIG.WELCOME_DELAY);
    });

    // Global shortcuts
    document.addEventListener('keydown', e => {
      if (e.key === 'Escape' && state.isOpen) toggle();
      if ((e.ctrlKey || e.metaKey) && e.shiftKey && e.key === 'C') {
        e.preventDefault();
        toggleCostDisplay();
      }
    });

    document.addEventListener('visibilitychange', () => {
      if (!document.hidden && state.isOpen) scrollBottom(true);
    });

    setInterval(() => {
      if (!state.isOnline && navigator.onLine) updateNetworkStatus();
    }, CONFIG.RECONNECT_INTERVAL);

    logEvent('chat_widget_init');
  }

  // ===== PUBLIC API (100% backward compatible + 2026 Pro extensions) =====
  return {
    // Legacy
    toggle,
    send:         () => sendMsg(),
    sendQuick:    text => sendMsg(text),
    clearChat,
    handleInput,
    handleKey,
    scrollToBottom: () => scrollBottom(true),

    // 2026 Pro
    sendMsg,
    sendStream:   (text, opts) => sendMsg(text, { ...opts, stream: true }),
    abort: () => {
      if (state.abortController)  state.abortController.abort();
      if (state.streamController) { state.streamController.close(); state.streamController = null; }
      state.isBusy = false;
      hideTyping();
      updateSendBtn();
    },
    toggleCost:      toggleCostDisplay,
    isOpen:          () => state.isOpen,
    isBusy:          () => state.isBusy,
    getSession:      () => getSession(),
    setSession:      (id) => { state.sessionId = id; },
    getQueueLength:  () => state.pendingQueue.length,
    flushQueue:      flushOfflineQueue,
    getRateRemaining: () => state.rateRemaining,

    init
  };
})();

// ===== AUTO-INITIALIZE =====
if (document.readyState === 'loading') {
  document.addEventListener('DOMContentLoaded', cwApp.init);
} else {
  cwApp.init();
}