/* ============================================================
   NOTIFIKASI PEMBELIAN SIMULASI v5.0 — ULTRA RESPONSIVE MOBILE
   SVG Inline · Touch Swipe · Haptic · Safe Area · Stack Limit
   Gesture Recognition · Auto-Resize · iOS/Android Optimized
   ============================================================ */
(function() {
    'use strict';

    // ===== SVG ICONS PER BRAND (INLINE, 100% RELIABLE) =====
    const SVG_ICONS = {
        Apple: `<svg viewBox="0 0 24 24" width="24" height="24" fill="none"><rect width="24" height="24" rx="5" fill="#1a1a2e"/><path d="M17.3 8.4c-.7 0-1.4.3-1.9.8-.5.5-.8 1.2-.8 1.9 0 .4.1.7.2 1.1-.3.1-.6.2-.9.2-.7 0-1.3-.3-1.8-.7-.5-.5-.7-1.1-.7-1.8 0-.7.3-1.3.7-1.8.5-.5 1.1-.7 1.8-.7.3 0 .6.1.9.2.1-.3.2-.7.2-1.1 0-.7.3-1.4.8-1.9.5-.5 1.2-.8 1.9-.8.7 0 1.4.3 1.9.8.5.5.8 1.2.8 1.9 0 .4-.1.8-.2 1.1-.3-.1-.6-.2-.9-.2-.7 0-1.3.3-1.8.7-.5.5-.7 1.1-.7 1.8z" fill="#f5f5f5"/><circle cx="12" cy="12" r="3" fill="none" stroke="#f5f5f5" stroke-width="1.5"/><path d="M12 6v2M12 16v2M6 12h2M16 12h2" stroke="#f5f5f5" stroke-width="1.5" stroke-linecap="round"/></svg>`,

        Samsung: `<svg viewBox="0 0 24 24" width="24" height="24" fill="none"><rect width="24" height="24" rx="5" fill="#1428a0"/><rect x="5" y="6" width="14" height="12" rx="2" fill="none" stroke="#fff" stroke-width="1.5"/><circle cx="12" cy="12" r="2.5" fill="none" stroke="#fff" stroke-width="1.5"/><path d="M9 9l1.5 1.5M15 9l-1.5 1.5M9 15l1.5-1.5M15 15l-1.5-1.5" stroke="#fff" stroke-width="1" stroke-linecap="round"/></svg>`,

        Xiaomi: `<svg viewBox="0 0 24 24" width="24" height="24" fill="none"><rect width="24" height="24" rx="5" fill="#ff6900"/><rect x="6" y="6" width="5" height="5" rx="1" fill="#fff"/><rect x="13" y="6" width="5" height="5" rx="1" fill="#fff" opacity="0.7"/><rect x="6" y="13" width="5" height="5" rx="1" fill="#fff" opacity="0.5"/><rect x="13" y="13" width="5" height="5" rx="1" fill="#fff" opacity="0.3"/></svg>`,

        Google: `<svg viewBox="0 0 24 24" width="24" height="24" fill="none"><rect width="24" height="24" rx="5" fill="#4285f4"/><circle cx="12" cy="12" r="4" fill="none" stroke="#fff" stroke-width="2"/><path d="M12 5v3M12 16v3M5 12h3M16 12h3" stroke="#fff" stroke-width="1.5" stroke-linecap="round"/></svg>`,

        Vivo: `<svg viewBox="0 0 24 24" width="24" height="24" fill="none"><rect width="24" height="24" rx="5" fill="#415fff"/><path d="M7 8l5 8 5-8" stroke="#fff" stroke-width="2.5" stroke-linecap="round" stroke-linejoin="round" fill="none"/></svg>`,

        Oppo: `<svg viewBox="0 0 24 24" width="24" height="24" fill="none"><rect width="24" height="24" rx="5" fill="#009b72"/><circle cx="12" cy="12" r="5" fill="none" stroke="#fff" stroke-width="2"/><circle cx="12" cy="12" r="2" fill="#fff"/></svg>`,

        OnePlus: `<svg viewBox="0 0 24 24" width="24" height="24" fill="none"><rect width="24" height="24" rx="5" fill="#f50514"/><rect x="6" y="10" width="12" height="4" rx="2" fill="#fff"/><path d="M16 7l-4 5-4-5" stroke="#fff" stroke-width="2" stroke-linecap="round" stroke-linejoin="round"/></svg>`,

        Realme: `<svg viewBox="0 0 24 24" width="24" height="24" fill="none"><rect width="24" height="24" rx="5" fill="#ffc915"/><path d="M12 6l6 10H6l6-10z" fill="#1a1a1a"/></svg>`,

        Infinix: `<svg viewBox="0 0 24 24" width="24" height="24" fill="none"><rect width="24" height="24" rx="5" fill="#00c853"/><path d="M8 8l4 4-4 4M16 8l-4 4 4 4" stroke="#fff" stroke-width="2" stroke-linecap="round" stroke-linejoin="round"/></svg>`,

        Nothing: `<svg viewBox="0 0 24 24" width="24" height="24" fill="none"><rect width="24" height="24" rx="5" fill="#000"/><circle cx="8" cy="12" r="2" fill="#fff"/><circle cx="12" cy="12" r="2" fill="#fff" opacity="0.6"/><circle cx="16" cy="12" r="2" fill="#fff" opacity="0.3"/></svg>`,

        Asus: `<svg viewBox="0 0 24 24" width="24" height="24" fill="none"><rect width="24" height="24" rx="5" fill="#00539b"/><path d="M7 16V8l5 5 5-5v8" stroke="#fff" stroke-width="2" fill="none" stroke-linecap="round" stroke-linejoin="round"/></svg>`,

        Huawei: `<svg viewBox="0 0 24 24" width="24" height="24" fill="none"><rect width="24" height="24" rx="5" fill="#c8102e"/><path d="M6 16c1-4 2-7 6-7s5 3 6 7" stroke="#fff" stroke-width="2" stroke-linecap="round" fill="none"/><circle cx="12" cy="9" r="1.4" fill="#fff"/></svg>`,

        Honor: `<svg viewBox="0 0 24 24" width="24" height="24" fill="none"><rect width="24" height="24" rx="5" fill="#0a63f7"/><path d="M6 17V7l6 6 6-6v10" stroke="#fff" stroke-width="2" fill="none" stroke-linecap="round" stroke-linejoin="round"/></svg>`,

        Motorola: `<svg viewBox="0 0 24 24" width="24" height="24" fill="none"><rect width="24" height="24" rx="5" fill="#1a1a1a"/><path d="M12 4l6 8-6 8-6-8 6-8z" fill="none" stroke="#5ba829" stroke-width="2" stroke-linejoin="round"/></svg>`,

        Tecno: `<svg viewBox="0 0 24 24" width="24" height="24" fill="none"><rect width="24" height="24" rx="5" fill="#0084ff"/><rect x="6" y="6" width="12" height="12" rx="2" fill="none" stroke="#fff" stroke-width="1.5"/><path d="M9 12h6M12 9v6" stroke="#fff" stroke-width="1.5" stroke-linecap="round"/></svg>`,

        Itel: `<svg viewBox="0 0 24 24" width="24" height="24" fill="none"><rect width="24" height="24" rx="5" fill="#0f9d58"/><circle cx="12" cy="12" r="5" fill="none" stroke="#fff" stroke-width="2"/><path d="M12 9v3l2 2" stroke="#fff" stroke-width="1.5" stroke-linecap="round"/></svg>`,

        Generic: `<svg viewBox="0 0 24 24" width="24" height="24" fill="none"><rect width="24" height="24" rx="5" fill="#6366f1"/><rect x="7" y="5" width="10" height="14" rx="2" fill="none" stroke="#fff" stroke-width="1.5"/><circle cx="12" cy="16" r="0.8" fill="#fff"/></svg>`
    };

    const SIM_DATA = {
        locations: [
            'Jakarta', 'Surabaya', 'Bandung', 'Medan', 'Semarang',
            'Makassar', 'Palembang', 'Depok', 'Tangerang', 'Bekasi',
            'Yogyakarta', 'Malang', 'Denpasar', 'Batam', 'Padang',
            'Pontianak', 'Manado', 'Samarinda', 'Banjarmasin', 'Pekanbaru',
            'Bandar Lampung', 'Bogor', 'Cirebon', 'Solo', 'Kediri',
            'Madiun', 'Probolinggo', 'Pasuruan', 'Mojokerto', 'Jember',
            'Mataram', 'Kupang', 'Ambon', 'Jayapura', 'Sorong',
            'Ternate', 'Gorontalo', 'Palu', 'Kendari', 'Bengkulu',
            'Jambi', 'Pangkal Pinang', 'Tanjung Pinang', 'Serang', 'Cilegon',
            'Sukabumi', 'Tasikmalaya', 'Purwokerto', 'Magelang', 'Salatiga',
            'Balikpapan', 'Palangkaraya', 'Tegal', 'Pematang Siantar', 'Binjai',
            'Sibolga', 'Lubuklinggau', 'Metro', 'Tebing Tinggi', 'Cimahi'
        ],

        products: [
            { name: 'iPhone 15 Pro Max 256GB Natural Titanium', brand: 'Apple' },
            { name: 'iPhone 15 Pro 128GB Blue Titanium', brand: 'Apple' },
            { name: 'iPhone 15 256GB Pink', brand: 'Apple' },
            { name: 'iPhone 14 Pro Max 256GB Deep Purple', brand: 'Apple' },
            { name: 'iPhone 14 Pro 128GB Space Black', brand: 'Apple' },
            { name: 'iPhone 14 128GB Midnight', brand: 'Apple' },
            { name: 'iPhone 13 Pro Max 256GB Sierra Blue', brand: 'Apple' },
            { name: 'iPhone 13 128GB Starlight', brand: 'Apple' },
            { name: 'iPhone 12 Pro 256GB Pacific Blue', brand: 'Apple' },
            { name: 'iPhone 12 64GB Product Red', brand: 'Apple' },
            { name: 'iPhone 11 Pro Max 64GB Midnight Green', brand: 'Apple' },
            { name: 'iPhone SE 2022 128GB Midnight', brand: 'Apple' },

            { name: 'Samsung Galaxy S24 Ultra 256GB Titanium Gray', brand: 'Samsung' },
            { name: 'Samsung Galaxy S24+ 256GB Cobalt Violet', brand: 'Samsung' },
            { name: 'Samsung Galaxy S24 128GB Amber Yellow', brand: 'Samsung' },
            { name: 'Samsung Galaxy Z Fold 5 512GB Phantom Black', brand: 'Samsung' },
            { name: 'Samsung Galaxy Z Flip 5 256GB Mint', brand: 'Samsung' },
            { name: 'Samsung Galaxy S23 Ultra 256GB Green', brand: 'Samsung' },
            { name: 'Samsung Galaxy S23 FE 128GB Graphite', brand: 'Samsung' },
            { name: 'Samsung Galaxy A54 5G 256GB Awesome Lime', brand: 'Samsung' },
            { name: 'Samsung Galaxy A34 5G 128GB Awesome Silver', brand: 'Samsung' },
            { name: 'Samsung Galaxy M54 5G 128GB Dark Blue', brand: 'Samsung' },

            { name: 'Xiaomi 14 Ultra 512GB White', brand: 'Xiaomi' },
            { name: 'Xiaomi 14 256GB Black', brand: 'Xiaomi' },
            { name: 'Xiaomi 13T Pro 256GB Meadow Green', brand: 'Xiaomi' },
            { name: 'Xiaomi Redmi Note 13 Pro+ 256GB Midnight Black', brand: 'Xiaomi' },
            { name: 'Xiaomi Redmi Note 13 Pro 128GB Ocean Teal', brand: 'Xiaomi' },
            { name: 'Xiaomi Redmi 13C 128GB Midnight Black', brand: 'Xiaomi' },
            { name: 'Xiaomi Poco F6 Pro 256GB Black', brand: 'Xiaomi' },
            { name: 'Xiaomi Poco X6 Pro 256GB Yellow', brand: 'Xiaomi' },

            { name: 'Google Pixel 8 Pro 256GB Obsidian', brand: 'Google' },
            { name: 'Google Pixel 8 128GB Hazel', brand: 'Google' },
            { name: 'Google Pixel 7a 128GB Charcoal', brand: 'Google' },
            { name: 'Google Pixel Fold 256GB Porcelain', brand: 'Google' },

            { name: 'Vivo X100 Pro 512GB Asteroid Black', brand: 'Vivo' },
            { name: 'Vivo X90 Pro 256GB Legendary Black', brand: 'Vivo' },
            { name: 'Vivo V30 Pro 256GB Andaman Blue', brand: 'Vivo' },
            { name: 'Vivo V29 5G 256GB Majestic Red', brand: 'Vivo' },
            { name: 'Vivo Y100 5G 128GB Silk Blue', brand: 'Vivo' },

            { name: 'Oppo Find X7 Ultra 512GB Ocean Blue', brand: 'Oppo' },
            { name: 'Oppo Find X6 Pro 256GB Desert Silver', brand: 'Oppo' },
            { name: 'Oppo Reno11 Pro 256GB Rock Grey', brand: 'Oppo' },
            { name: 'Oppo Reno11 128GB Wave Green', brand: 'Oppo' },
            { name: 'Oppo A79 5G 128GB Glowing Green', brand: 'Oppo' },

            { name: 'OnePlus 12 512GB Flowy Emerald', brand: 'OnePlus' },
            { name: 'OnePlus 12R 256GB Iron Gray', brand: 'OnePlus' },
            { name: 'OnePlus Open 512GB Voyager Black', brand: 'OnePlus' },
            { name: 'OnePlus Nord 3 5G 256GB Tempest Gray', brand: 'OnePlus' },

            { name: 'Realme GT 5 Pro 256GB Black', brand: 'Realme' },
            { name: 'Realme 12 Pro+ 256GB Navigator Beige', brand: 'Realme' },
            { name: 'Realme 12 5G 128GB Woodland Green', brand: 'Realme' },
            { name: 'Realme C67 128GB Sunny Oasis', brand: 'Realme' },

            { name: 'Infinix Note 40 Pro 256GB Vintage Green', brand: 'Infinix' },
            { name: 'Infinix Hot 40 Pro 256GB Palm Blue', brand: 'Infinix' },

            { name: 'Nothing Phone (2) 256GB Dark Grey', brand: 'Nothing' },
            { name: 'Nothing Phone (2a) 128GB Black', brand: 'Nothing' },

            { name: 'Asus ROG Phone 8 Pro 512GB Phantom Black', brand: 'Asus' },
            { name: 'Asus Zenfone 10 256GB Eclipse Red', brand: 'Asus' },

            { name: 'Huawei Mate 60 Pro 512GB Black', brand: 'Huawei' },
            { name: 'Huawei P60 Pro 256GB Rococo Pearl', brand: 'Huawei' },
            { name: 'Huawei Nova 12 Pro 256GB Green', brand: 'Huawei' },

            { name: 'Honor Magic 6 Pro 512GB Black', brand: 'Honor' },
            { name: 'Honor 90 256GB Emerald Green', brand: 'Honor' },
            { name: 'Honor X9b 256GB Titanium Silver', brand: 'Honor' },

            { name: 'Motorola Edge 40 Pro 256GB Interstellar Black', brand: 'Motorola' },
            { name: 'Motorola Razr 40 Ultra 256GB Infinite Black', brand: 'Motorola' },
            { name: 'Motorola Moto G84 5G 256GB Midnight Blue', brand: 'Motorola' },

            { name: 'Tecno Phantom V Fold 256GB Rococo Black', brand: 'Tecno' },
            { name: 'Tecno Camon 20 Pro 256GB Serenity Blue', brand: 'Tecno' },
            { name: 'Tecno Spark 10 Pro 128GB Meteorite Grey', brand: 'Tecno' },

            { name: 'Itel S23+ 128GB Glacier Blue', brand: 'Itel' },
            { name: 'Itel A60 64GB Dawn Gold', brand: 'Itel' }
        ],

        timeRanges: [
            { min: 1, max: 1, label: 'baru saja', weight: 15 },
            { min: 1, max: 2, label: 'menit yang lalu', weight: 40 },
            { min: 3, max: 10, label: 'menit yang lalu', weight: 25 },
            { min: 11, max: 30, label: 'menit yang lalu', weight: 10 },
            { min: 1, max: 2, label: 'jam yang lalu', weight: 8 },
            { min: 3, max: 5, label: 'jam yang lalu', weight: 2 }
        ],

        actions: [
            'Baru membeli',
            'Baru memesan',
            'Baru checkout',
            'Baru mengambil',
            'Baru mereview',
            'Baru membayar'
        ]
    };

    const BRAND_COLORS = {
        Apple: '#1a1a2e',
        Samsung: '#1428a0',
        Xiaomi: '#ff6900',
        Google: '#4285f4',
        Vivo: '#415fff',
        Oppo: '#009b72',
        OnePlus: '#f50514',
        Realme: '#ffc915',
        Infinix: '#00c853',
        Nothing: '#000000',
        Asus: '#00539b',
        Huawei: '#c8102e',
        Honor: '#0a63f7',
        Motorola: '#5ba829',
        Tecno: '#0084ff',
        Itel: '#0f9d58'
    };

    let notifInterval = null;
    let isEnabled = true;
    let shownProducts = new Set();
    let shownCount = 0;
    let isMobile = false;
    let maxVisible = 3;

    // Detect mobile
    // BUG FIX: disamakan dengan breakpoint .bottom-nav di base.html
    // (@media min-width: 992px baru dock disembunyikan) dan breakpoint
    // posisi "naik dari navbar" di notif_sim.css (max-width: 991px).
    // Sebelumnya 768px, jadi di rentang 769-991px dock masih tampil
    // tapi maxVisible masih 3 (harusnya 2) -> terlalu padat di layar
    // segitu karena card belum mengecil ke ukuran 200x60px (itu baru
    // berlaku di ≤768px lewat CSS terpisah).
    function detectMobile() {
        isMobile = window.innerWidth <= 991 || 'ontouchstart' in window;
        maxVisible = isMobile ? 2 : 3;
    }

    function getRandom(arr) { return arr[Math.floor(Math.random() * arr.length)]; }

    function getWeightedRandomTime() {
        const totalWeight = SIM_DATA.timeRanges.reduce((sum, t) => sum + t.weight, 0);
        let random = Math.random() * totalWeight;
        for (const range of SIM_DATA.timeRanges) {
            random -= range.weight;
            if (random <= 0) {
                const value = range.min === range.max ? range.min : Math.floor(Math.random() * (range.max - range.min + 1)) + range.min;
                return range.label === 'baru saja' ? 'baru saja' : `${value} ${range.label}`;
            }
        }
        return 'baru saja';
    }

    function getRandomProduct() {
        let attempts = 0;
        let product;
        do {
            product = getRandom(SIM_DATA.products);
            attempts++;
        } while (shownProducts.has(product.name) && attempts < 10);

        if (shownProducts.size > 30) shownProducts.clear();
        shownProducts.add(product.name);
        return product;
    }

    function getBrandIcon(brand) {
        const svg = SVG_ICONS[brand] || SVG_ICONS.Generic;
        const color = BRAND_COLORS[brand] || '#6366f1';
        return { svg, color };
    }

    function generateNotificationData() {
        const location = getRandom(SIM_DATA.locations);
        const product = getRandomProduct();
        const action = getRandom(SIM_DATA.actions);
        const time = getWeightedRandomTime();
        const iconData = getBrandIcon(product.brand);

        shownCount++;

        return {
            location: location,
            product: product.name,
            brand: product.brand,
            iconSvg: iconData.svg,
            iconColor: iconData.color,
            action: action,
            time: time,
            id: 'notif_' + Date.now() + '_' + Math.random().toString(36).slice(2, 8),
            index: shownCount
        };
    }

    // ===== MOBILE TOUCH / SWIPE TO DISMISS =====
    let touchStartX = 0;
    let touchStartY = 0;
    let currentSwipeCard = null;
    let swipeThreshold = 80;
    let isSwiping = false;
    let startTime = 0;

    function initTouchHandlers(card) {
        if (!card) return;
        card.addEventListener('touchstart', handleTouchStart, { passive: true });
        card.addEventListener('touchmove', handleTouchMove, { passive: true });
        card.addEventListener('touchend', handleTouchEnd, { passive: true });
        card.addEventListener('touchcancel', handleTouchCancel, { passive: true });
    }

    function handleTouchStart(e) {
        const touch = e.touches[0];
        touchStartX = touch.clientX;
        touchStartY = touch.clientY;
        currentSwipeCard = e.currentTarget;
        isSwiping = false;
        startTime = Date.now();
    }

    function handleTouchMove(e) {
        if (!currentSwipeCard) return;
        const touch = e.touches[0];
        const deltaX = touch.clientX - touchStartX;
        const deltaY = touch.clientY - touchStartY;

        // Only handle horizontal swipe
        if (Math.abs(deltaX) > Math.abs(deltaY) && Math.abs(deltaX) > 10) {
            isSwiping = true;
            currentSwipeCard.classList.add('swiping');
            currentSwipeCard.style.transform = `translateX(${deltaX}px)`;
            currentSwipeCard.style.opacity = Math.max(0.3, 1 - Math.abs(deltaX) / 200);
            // Prevent scroll while swiping
            if (e.cancelable) e.preventDefault();
        }
    }

    function handleTouchEnd(e) {
        if (!currentSwipeCard) return;
        const touch = e.changedTouches[0];
        const deltaX = touch.clientX - touchStartX;
        const deltaTime = Date.now() - startTime;

        currentSwipeCard.classList.remove('swiping');
        currentSwipeCard.style.transform = '';
        currentSwipeCard.style.opacity = '';

        // Fast swipe (flick) or long drag
        const isFastSwipe = deltaTime < 300 && Math.abs(deltaX) > 40;
        if (Math.abs(deltaX) > swipeThreshold || isFastSwipe) {
            dismissNotification(currentSwipeCard.id);
            if (navigator.vibrate) navigator.vibrate(50);
        } else if (!isSwiping) {
            // Tap - navigate to catalog
            window.location.href = '/catalog';
        }

        currentSwipeCard = null;
        isSwiping = false;
    }

    function handleTouchCancel() {
        if (!currentSwipeCard) return;
        currentSwipeCard.classList.remove('swiping');
        currentSwipeCard.style.transform = '';
        currentSwipeCard.style.opacity = '';
        currentSwipeCard = null;
        isSwiping = false;
    }

    // ===== PRODUCT MARQUEE (teks berjalan bila overflow) =====
    // Mengukur scrollWidth vs clientWidth pada .notif-sim-product-text
    // di dalam .notif-sim-product, lalu HANYA mengaktifkan animasi
    // (via class .is-marquee) kalau teksnya memang lebih lebar dari
    // box-nya. Kecepatan geser dihitung proporsional terhadap jarak
    // overflow lewat CSS var --marquee-duration, supaya nama produk
    // pendek & panjang bergerak dengan kecepatan konsisten.
    function setupProductMarquee(cardEl) {
        if (!cardEl) return;

        const wrap = cardEl.querySelector('.notif-sim-product');
        const text = cardEl.querySelector('.notif-sim-product-text');
        if (!wrap || !text) return;

        // Reset dulu supaya pengukuran akurat (penting kalau card
        // di-reuse untuk notifikasi berikutnya / rotasi antar notif)
        wrap.classList.remove('is-marquee');
        text.style.transform = '';
        wrap.style.removeProperty('--marquee-distance');
        wrap.style.removeProperty('--marquee-duration');

        // Tunggu 1 frame biar layout benar-benar settle
        // (terutama kalau dipanggil barengan animasi masuk island)
        requestAnimationFrame(() => {
            const overflow = text.scrollWidth - wrap.clientWidth;

            // Toleransi kecil (4px) supaya rounding sub-pixel tidak
            // memicu marquee untuk teks yang sebenarnya pas
            if (overflow > 4) {
                const distance = overflow + 4; // + buffer padding-right dari CSS
                const pxPerSecond = 22; // kecepatan geser tetap, biar konsisten
                const duration = Math.max(5, distance / pxPerSecond);

                wrap.style.setProperty('--marquee-distance', `${distance}px`);
                wrap.style.setProperty('--marquee-duration', `${duration}s`);
                wrap.classList.add('is-marquee');
            }
        });
    }

    function createNotification(data) {
        const container = document.getElementById('notifSimContainer');
        if (!container) return;

        // Mobile: limit visible notifications
        const visibleCards = container.querySelectorAll('.notif-sim-card.show');
        if (isMobile && visibleCards.length >= maxVisible) {
            // Remove oldest
            const oldest = visibleCards[0];
            if (oldest) dismissNotification(oldest.id);
        }

        const card = document.createElement('div');
        card.className = 'notif-sim-card';
        card.id = data.id;
        card.setAttribute('role', 'alert');
        card.setAttribute('aria-live', 'polite');

        card.innerHTML = `
            <div class="notif-sim-icon-wrap" style="background:${data.iconColor}15;" aria-label="${data.brand}">
                ${data.iconSvg}
            </div>
            <div class="notif-sim-content">
                <div class="notif-sim-location">
                    <svg width="10" height="10" viewBox="0 0 24 24" fill="none" style="flex-shrink:0;margin-top:1px;">
                        <path d="M12 2C8.13 2 5 5.13 5 9c0 5.25 7 13 7 13s7-7.75 7-13c0-3.87-3.13-7-7-7z" fill="currentColor"/>
                    </svg>
                    Seseorang di <strong>${data.location}</strong>
                </div>
                <div class="notif-sim-product"><span class="notif-sim-product-text">${data.action} ${data.product}</span></div>
                <div class="notif-sim-meta">
                    <span class="notif-sim-time">
                        <svg width="8" height="8" viewBox="0 0 24 24" fill="none" style="flex-shrink:0;">
                            <circle cx="12" cy="12" r="10" stroke="currentColor" stroke-width="2"/>
                            <path d="M12 6v6l4 2" stroke="currentColor" stroke-width="2" stroke-linecap="round"/>
                        </svg>
                        ${data.time}
                    </span>
                    <span class="notif-sim-verified">
                        <svg width="9" height="9" viewBox="0 0 24 24" fill="none" style="flex-shrink:0;">
                            <path d="M9 12l2 2 4-4m6 2a9 9 0 11-18 0 9 9 0 0118 0z" fill="currentColor"/>
                        </svg>
                        Verified
                    </span>
                </div>
            </div>
            <button class="notif-sim-close" onclick="event.stopPropagation();NotifSim.dismiss('${data.id}')" aria-label="Tutup notifikasi">
                <svg width="10" height="10" viewBox="0 0 24 24" fill="none">
                    <path d="M18 6L6 18M6 6l12 12" stroke="currentColor" stroke-width="2.5" stroke-linecap="round"/>
                </svg>
            </button>
            <div class="notif-sim-progress"></div>
        `;

        card.addEventListener('click', (e) => {
            if (e.target.closest('.notif-sim-close')) return;
            if (isSwiping) return;
            window.location.href = '/catalog';
        });

        initTouchHandlers(card);

        container.appendChild(card);

        // Trigger animation with slight delay for DOM render
        requestAnimationFrame(() => {
            requestAnimationFrame(() => { card.classList.add('show'); });
        });

        // BUG FIX: sebelumnya setupProductMarquee() tidak pernah dipanggil
        // di manapun, jadi class .is-marquee tidak pernah ditambahkan
        // walaupun markup & CSS-nya sudah benar. Dipanggil di sini,
        // setelah card masuk ke DOM, supaya scrollWidth/clientWidth
        // terukur dari elemen yang benar-benar sudah ter-layout.
        setupProductMarquee(card);

        // Auto dismiss after 6 seconds
        setTimeout(() => { dismissNotification(data.id); }, 6000);
        return card;
    }

    function dismissNotification(id) {
        const card = document.getElementById(id);
        if (!card || card.classList.contains('hide')) return;
        card.classList.remove('show');
        card.classList.add('hide');
        setTimeout(() => { if (card.parentNode) card.remove(); }, 500);
    }

    function showNotification() {
        if (!isEnabled || document.hidden) return;
        createNotification(generateNotificationData());
    }

    function start() {
        if (notifInterval) return;
        detectMobile();
        setTimeout(() => {
            showNotification();
            notifInterval = setInterval(showNotification, 30000);
        }, 2000);
    }

    function stop() {
        if (notifInterval) { clearInterval(notifInterval); notifInterval = null; }
    }

    window.NotifSim = {
        start, stop,
        toggle: () => { isEnabled = !isEnabled; if (isEnabled) start(); else stop(); return isEnabled; },
        dismiss: dismissNotification,
        show: showNotification,
        forceShow: (count = 1) => { for (let i = 0; i < count; i++) setTimeout(() => showNotification(), i * 300); },
        isEnabled: () => isEnabled,
        getStats: () => ({ shownCount, queueSize: shownProducts.size })
    };

    function init() {
        detectMobile();
        start();
        document.addEventListener('visibilitychange', () => {
            if (document.hidden) stop();
            else if (isEnabled) setTimeout(start, 1000);
        });
        document.addEventListener('keydown', (e) => {
            if ((e.ctrlKey || e.metaKey) && e.shiftKey && e.key === 'N') {
                e.preventDefault();
                console.log('[NotifSim]', NotifSim.toggle() ? 'Enabled' : 'Disabled');
            }
        });
        // Handle resize
        let resizeTimeout;
        window.addEventListener('resize', () => {
            clearTimeout(resizeTimeout);
            resizeTimeout = setTimeout(() => {
                detectMobile();
                const container = document.getElementById('notifSimContainer');
                if (!container) return;
                const cards = container.querySelectorAll('.notif-sim-card.show');
                if (isMobile && cards.length > maxVisible) {
                    for (let i = 0; i < cards.length - maxVisible; i++) {
                        dismissNotification(cards[i].id);
                    }
                }
                // Re-check marquee for currently visible cards, since the
                // available width for .notif-sim-product can change on
                // resize/orientation change.
                cards.forEach(setupProductMarquee);
            }, 250);
        });
    }

    if (document.readyState === 'loading') {
        document.addEventListener('DOMContentLoaded', init);
    } else {
        init();
    }
})();
// BUG FIX: file ini disajikan sebagai file statis (/static/js/base_app.js)
// sehingga TIDAK PERNAH diproses Jinja2 — sintaks {{ ... }} sebelumnya
// dikirim mentah-mentah ke browser dan bikin SELURUH file gagal di-parse
// (SyntaxError: Unexpected token '{'). Selain itu, `userLoggedIn`/`userName`
// tidak dipakai di manapun pada file ini (dan seandainya tetap dideklarasikan
// dengan `const`, akan bentrok "Identifier has already been declared" dengan
// deklarasi `const userLoggedIn` di inline <script> base.html), jadi
// dihapus saja. `dateTimeConfig` diisi nilai default yang sama dengan
// default di server; nilai asli dari pengaturan admin tetap didapat karena
// inline <script> di base.html (yang di-render Jinja) menimpanya lagi
// dengan window.dateTimeConfig setelah file ini dimuat.
window.dateTimeConfig = window.dateTimeConfig || {
    dateFormat: 'dd/MM/yyyy',
    timeFormat: 'HH:mm',
    dateDisplayStyle: 'medium',
    timezone: 'Asia/Makassar'
};

        window.formatDate = function(date, style) {
            if (!date) return '';
            if (typeof date === 'string') date = new Date(date);
            if (!(date instanceof Date)) return '';
            const d = String(date.getDate()).padStart(2, '0');
            const m = String(date.getMonth() + 1).padStart(2, '0');
            const y = date.getFullYear();
            const format = style || window.dateTimeConfig.dateFormat;
            if (format === 'dd/MM/yyyy') return `${d}/${m}/${y}`;
            if (format === 'dd-MM-yyyy') return `${d}-${m}-${y}`;
            if (format === 'dd.MM.yyyy') return `${d}.${m}.${y}`;
            if (format === 'yyyy/MM/dd') return `${y}/${m}/${d}`;
            if (format === 'MM/dd/yyyy') return `${m}/${d}/${y}`;
            return `${d}/${m}/${y}`;
        };

        window.formatTime = function(date) {
            if (!date) return '';
            if (typeof date === 'string') date = new Date(date);
            if (!(date instanceof Date)) return '';
            const h = String(date.getHours()).padStart(2, '0');
            const m = String(date.getMinutes()).padStart(2, '0');
            const format = window.dateTimeConfig.timeFormat;
            if (format === 'HH:mm:ss') return `${h}:${m}:${String(date.getSeconds()).padStart(2, '0')}`;
            return `${h}:${m}`;
        };

        window.formatDateTime = function(date) {
            if (!date) return '';
            return window.formatDate(date) + ' ' + window.formatTime(date);
        };

        // Preloader
        window.addEventListener('load', () => {
            const preloader = document.getElementById('preloader');
            if (preloader) setTimeout(() => preloader.classList.add('hidden'), 400);
        });

        // Navbar scroll
        const nav = document.getElementById('mainNav');
        const backToTopBtn = document.getElementById('backToTop');
        window.addEventListener('scroll', () => {
            if (nav) nav.classList.toggle('scrolled', window.scrollY > 20);
            if (backToTopBtn) backToTopBtn.classList.toggle('visible', window.scrollY > 500);
        });

        // Search
        function toggleSearch() {
            const overlay = document.getElementById('searchOverlay');
            const input = document.getElementById('globalSearchInput');
            if (!overlay) return;
            overlay.classList.toggle('active');
            if (overlay.classList.contains('active') && input) setTimeout(() => input.focus(), 100);
        }
        function quickSearch(term) {
            const input = document.getElementById('globalSearchInput');
            if (input) input.value = term;
            setTimeout(() => window.location.href = '/catalog?q=' + encodeURIComponent(term), 300);
        }
        document.addEventListener('keydown', e => {
            if ((e.metaKey || e.ctrlKey) && e.key === 'k') { e.preventDefault(); toggleSearch(); }
            if (e.key === 'Escape') {
                const overlay = document.getElementById('searchOverlay');
                if (overlay) overlay.classList.remove('active');
            }
        });

        // Toast
        function showToast(message, type = 'success', duration = 3000) {
            const container = document.getElementById('toastContainer');
            const toast = document.createElement('div');
            toast.className = 'toast-item ' + type;
            const icons = { success: 'check2-circle', error: 'x-circle', warning: 'exclamation-triangle', info: 'info-circle' };
            const colors = { success: 'var(--success)', error: 'var(--danger)', warning: 'var(--warning)', info: 'var(--info)' };
            toast.innerHTML = '<i class="bi bi-' + icons[type] + '" style="color: ' + colors[type] + '; font-size: 1.1rem; margin-top: 2px;"></i>' +
                '<div class="flex-grow-1 small fw-medium">' + message + '</div>' +
                '<button type="button" class="btn btn-link text-secondary p-0" onclick="this.parentElement.remove()" style="font-size: 0.8rem;"><i class="bi bi-x"></i></button>' +
                '<div class="toast-progress"></div>';
            container.appendChild(toast);
            requestAnimationFrame(() => toast.classList.add('show'));
            const progress = toast.querySelector('.toast-progress');
            progress.style.animation = 'shrink ' + duration + 'ms linear forwards';
            setTimeout(() => { toast.classList.remove('show'); setTimeout(() => toast.remove(), 400); }, duration);
        }

        // BUG FIX: token CSRF unik per sesi, jadi tidak bisa di-hardcode di
        // file statis. Dibaca dari <meta name="csrf-token"> saat request
        // dikirim (bukan saat file di-parse), supaya selalu dapat token
        // terbaru punya user yang sedang login.
        function getCSRFToken() {
            return document.querySelector('meta[name="csrf-token"]')?.content || '';
        }

        // Auth
        function switchAuthTab(tab) {
            document.getElementById('authLoginPanel').style.display = tab === 'login' ? 'block' : 'none';
            document.getElementById('authRegisterPanel').style.display = tab === 'register' ? 'block' : 'none';
            document.getElementById('tabBtnLogin').classList.toggle('active', tab === 'login');
            document.getElementById('tabBtnRegister').classList.toggle('active', tab === 'register');
        }

        document.getElementById('userLoginForm')?.addEventListener('submit', async e => {
            e.preventDefault();
            const res = await fetch('/api/user/login', {
                method: 'POST', headers: { 'Content-Type': 'application/json' },
                body: JSON.stringify({ email: document.getElementById('login_email').value, password: document.getElementById('login_password').value })
            });
            const data = await res.json();
            if (data.success) { showToast('Login berhasil!'); setTimeout(() => location.reload(), 800); }
            else showToast(data.message || 'Login gagal', 'error');
        });

        document.getElementById('userRegisterForm')?.addEventListener('submit', async e => {
            e.preventDefault();
            const password = document.getElementById('reg_password').value;
            if (password !== document.getElementById('reg_confirm').value) return showToast('Konfirmasi sandi tidak cocok!', 'error');
            if (password.length < 8) return showToast('Kata sandi minimal 8 karakter!', 'error');
            const res = await fetch('/api/user/register', {
                method: 'POST', headers: { 'Content-Type': 'application/json' },
                body: JSON.stringify({
                    username: document.getElementById('reg_username').value.trim(),
                    full_name: document.getElementById('reg_full_name').value.trim(),
                    email: document.getElementById('reg_email').value.trim(),
                    phone: document.getElementById('reg_phone').value.trim(),
                    password: password
                })
            });
            const data = await res.json();
            if (data.success) { showToast('Registrasi berhasil! Silakan login.'); switchAuthTab('login'); }
            else showToast(data.message || 'Registrasi gagal', 'error');
        });

        document.getElementById('userLogoutBtn')?.addEventListener('click', async e => {
            e.preventDefault();
            await fetch('/api/user/logout', { method: 'POST' });
            showToast('Logout berhasil'); setTimeout(() => location.reload(), 600);
        });

        // Cart
        async function addToCartAPI(id, name, price, event = null) {
            event = event || window.event;
            let btn = event ? event.target.closest('button, .quick-btn, .btn') : null;
            if (!btn) {
                const buttons = document.querySelectorAll('button, .quick-btn, .btn');
                for (const b of buttons) {
                    const onclick = b.getAttribute('onclick');
                    if (onclick && onclick.includes('addToCartAPI(' + id)) { btn = b; break; }
                }
            }
            if (!btn) return;
            btn.disabled = true;
            const originalHTML = btn.innerHTML;
            btn.innerHTML = '<span class="spinner-border spinner-border-sm"></span>';
            try {
                const res = await fetch('/api/add_to_cart', {
                    method: 'POST',
                    headers: { 'Content-Type': 'application/json', 'X-CSRFToken': getCSRFToken() },
                    body: JSON.stringify({ product_id: id, quantity: 1 })
                });
                const data = await res.json();
                if (data.success) {
                    updateCartBadge(data.cart_count);
                    showToast(name + ' ditambahkan ke keranjang', 'success');
                } else showToast(data.message || 'Gagal', 'error');
            } catch { showToast('Gagal terhubung ke server', 'error'); }
            finally { btn.disabled = false; btn.innerHTML = originalHTML; }
        }

        function updateCartBadge(count) {
            const badge = document.getElementById('fabCartBadge');
            if (badge) {
                badge.textContent = count;
                badge.style.display = count > 0 ? 'flex' : 'none';
            }
        }

        fetch('/api/cart_count')
            .then(r => r.json())
            .then(data => { if (data.success) updateCartBadge(data.cart_count); })
            .catch(() => {});

        // Reveal
        const revealObserver = new IntersectionObserver(entries => {
            entries.forEach(entry => { if (entry.isIntersecting) { entry.target.classList.add('visible'); revealObserver.unobserve(entry.target); } });
        }, { threshold: 0.1, rootMargin: '0px 0px -50px 0px' });
        document.querySelectorAll('.reveal').forEach(el => revealObserver.observe(el));

        // Quick View
        function quickView(productId) {
            const modal = bootstrap.Modal.getOrCreateInstance(document.getElementById('quickViewModal'));
            modal.show();
            fetch('/api/product/' + productId)
                .then(r => r.json())
                .then(data => {
                    if (data.success) {
                        const product = data.product;
                        document.getElementById('quickViewContent').innerHTML =
                            '<div class="row g-4 align-items-center">' +
                                '<div class="col-md-6"><img src="' + (product.image || 'https://placehold.co/600x600/0f172a/cbd5e1?text=HP') + '" class="w-100 rounded-4" style="aspect-ratio:1;object-fit:cover;" alt=""></div>' +
                                '<div class="col-md-6">' +
                                    '<div class="d-flex gap-2 mb-2"><span class="badge-chip badge-new">' + product.condition + '</span></div>' +
                                    '<h3 class="fw-bold mb-2">' + product.name + '</h3>' +
                                    '<div class="card-rating mb-3"><i class="bi bi-star-fill"></i><i class="bi bi-star-fill"></i><i class="bi bi-star-fill"></i><i class="bi bi-star-fill"></i><i class="bi bi-star-half"></i><span>(4.8)</span></div>' +
                                    '<h4 class="card-price mb-3" style="font-size:1.5rem;">' + product.price_formatted + '</h4>' +
                                    '<p class="text-secondary small mb-4">' + (product.description || 'Kondisi prima.') + '</p>' +
                                    '<div class="d-flex gap-2">' +
                                        '<button type="button" class="btn btn-primary flex-grow-1" id="qvAddBtn">Tambah ke Keranjang</button>' +
                                        '<button type="button" class="btn btn-secondary" onclick="toggleWishlist(' + product.id + ')"><i class="bi bi-heart"></i></button>' +
                                    '</div>' +
                                '</div>' +
                            '</div>';
                        document.getElementById('qvAddBtn').addEventListener('click', function(e) {
                            addToCartAPI(product.id, product.name, product.price, e);
                            bootstrap.Modal.getInstance(document.getElementById('quickViewModal')).hide();
                        });
                    }
                }).catch(() => { document.getElementById('quickViewContent').innerHTML = '<div class="text-center py-4 text-danger">Gagal memuat.</div>'; });
        }

        function toggleWishlist(productId) { showToast('Produk disimpan ke wishlist', 'success'); }
        function removeFilter(key) {
            const url = new URL(window.location.href); url.searchParams.delete(key); window.location.href = url.toString();
        }

        // FIX: jaring pengaman agar backdrop modal Bootstrap tidak pernah menutupi
        // (tampil di depan) konten modal, apapun penyebabnya di sisi CSS.
        // Dipasang delegated di document supaya berlaku untuk semua modal, bukan cuma quickViewModal.
        document.addEventListener('shown.bs.modal', function (e) {
            // Paksa urutan z-index yang benar: modal harus selalu di atas backdrop.
            document.querySelectorAll('.modal-backdrop').forEach(function (bd) {
                bd.style.zIndex = '1065';
            });
            if (e.target) e.target.style.zIndex = '1070';
        });
        document.addEventListener('hidden.bs.modal', function () {
            // Bersihkan backdrop yatim (leftover) yang kadang tertinggal di DOM --
            // ini penyebab umum backdrop lama "menumpuk" di depan modal berikutnya.
            if (!document.querySelector('.modal.show')) {
                document.querySelectorAll('.modal-backdrop').forEach(function (bd) { bd.remove(); });
                document.body.classList.remove('modal-open');
                document.body.style.removeProperty('overflow');
                document.body.style.removeProperty('padding-right');
            }
        });

        // FIX SCROLL SAAT MODAL TERBUKA:
        // <html> punya `overflow-y: scroll` (base.html), jadi viewport di-scroll oleh
        // <html>, bukan <body>. Kunci scroll bawaan Bootstrap (`body{overflow:hidden}`)
        // tidak berpengaruh -> halaman belakang ikut ter-scroll. Di sini <html> yang
        // dikunci lewat class .scroll-locked (CSS ada di base.html, bagian 16B).
        // Berlaku untuk modal & offcanvas (menu mobile), dan aman untuk modal bertumpuk.
        (function () {
            var root = document.documentElement;
            var savedScrollY = 0;

            function anyOverlayOpen() {
                return !!document.querySelector('.modal.show, .offcanvas.show');
            }
            function lock() {
                if (root.classList.contains('scroll-locked')) return;
                savedScrollY = window.scrollY || root.scrollTop || 0;
                root.classList.add('scroll-locked');
            }
            function unlock() {
                if (anyOverlayOpen()) return;
                root.classList.remove('scroll-locked');
                // Pulihkan posisi scroll (jaga-jaga untuk browser mobile yang
                // sempat mereset posisi saat overflow berubah).
                if (savedScrollY && Math.abs((window.scrollY || 0) - savedScrollY) > 1) {
                    window.scrollTo({ top: savedScrollY, behavior: 'instant' });
                }
            }

            document.addEventListener('show.bs.modal', lock);
            document.addEventListener('show.bs.offcanvas', lock);
            document.addEventListener('hidden.bs.modal', function () { setTimeout(unlock, 0); });
            document.addEventListener('hidden.bs.offcanvas', function () { setTimeout(unlock, 0); });

            // Jaring pengaman: kalau halaman dipulihkan dari bfcache dengan class tertinggal.
            window.addEventListener('pageshow', function () {
                if (!anyOverlayOpen()) root.classList.remove('scroll-locked');
            });
        })();

        // Auto-hide flash messages
        document.querySelectorAll('.flash-message').forEach(el => {
            setTimeout(() => {
                el.style.opacity = '0';
                el.style.transform = 'translateY(-10px)';
                setTimeout(() => el.remove(), 500);
            }, 5000);
        });
