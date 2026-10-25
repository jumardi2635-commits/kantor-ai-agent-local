/* Theme toggle (navbar moon/sun icon) — Jaga / Pusat Gadai Indonesia
   Dipindah ke file eksternal supaya tidak diblokir Content-Security-Policy
   yang melarang inline <script> di beberapa server. */
(function () {
    var html = document.documentElement;

    var btns = [
        document.getElementById('themeToggleBtn'),
        document.getElementById('themeToggleBtnDesktop')
    ].filter(Boolean);

    if (!btns.length) {
        console.warn('[theme-toggle] Tombol themeToggleBtn / themeToggleBtnDesktop tidak ditemukan di DOM.');
        return;
    }

    function syncIcons() {
        var isLight = html.getAttribute('data-theme') === 'light';
        btns.forEach(function (btn) {
            var icon = btn.querySelector('i');
            if (!icon) return;
            icon.classList.toggle('bi-moon-stars', !isLight);
            icon.classList.toggle('bi-sun-fill', isLight);
        });
    }

    function setTheme(theme) {
        html.setAttribute('data-theme', theme);
        html.setAttribute('data-bs-theme', theme);
        try { localStorage.setItem('jaga-theme', theme); } catch (e) {}
        syncIcons();
    }

    btns.forEach(function (btn) {
        btn.addEventListener('click', function () {
            var next = html.getAttribute('data-theme') === 'light' ? 'dark' : 'light';
            setTheme(next);
        });
    });

    syncIcons();
})();
