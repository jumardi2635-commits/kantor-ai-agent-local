/* ═══════════════════════════════════════════════════════════
   AUTH MODAL ANIMATION CONTROLLER — 2026 Premium Edition
   Open/Close · Login/Register Switch · Form Validation · Effects
   ═══════════════════════════════════════════════════════════ */

(function() {
    'use strict';

    const AuthModal = {
        // State
        isAnimating: false,
        currentTab: 'login',
        modalEl: null,
        modalInstance: null,

        // Configuration
        config: {
            animationDuration: 500,
            shakeDuration: 500,
            particleCount: 12,
            particleColors: ['#6366f1', '#a855f7', '#22d3ee', '#10b981', '#f59e0b', '#ef4444']
        },

        /**
         * Initialize the auth modal animation system
         */
        init() {
            this.modalEl = document.getElementById('authModal');
            if (!this.modalEl) return;

            // Get Bootstrap modal instance
            this.modalInstance = bootstrap.Modal.getOrCreateInstance(this.modalEl);

            // Bind events
            this.bindEvents();
            this.enhanceForms();
            this.addInputIcons();
        },

        /**
         * Bind all event listeners
         */
        bindEvents() {
            // Modal show/hide events for custom animations
            this.modalEl.addEventListener('show.bs.modal', (e) => {
                this.onModalShow(e);
            });

            this.modalEl.addEventListener('shown.bs.modal', (e) => {
                this.onModalShown(e);
            });

            this.modalEl.addEventListener('hide.bs.modal', (e) => {
                this.onModalHide(e);
            });

            this.modalEl.addEventListener('hidden.bs.modal', (e) => {
                this.onModalHidden(e);
            });

            // Tab switching
            const tabLogin = document.getElementById('tabBtnLogin');
            const tabRegister = document.getElementById('tabBtnRegister');

            if (tabLogin) {
                tabLogin.addEventListener('click', () => this.switchTab('login'));
            }
            if (tabRegister) {
                tabRegister.addEventListener('click', () => this.switchTab('register'));
            }

            // Form submissions
            const loginForm = document.getElementById('userLoginForm');
            const registerForm = document.getElementById('userRegisterForm');

            if (loginForm) {
                loginForm.addEventListener('submit', (e) => this.handleLoginSubmit(e));
            }
            if (registerForm) {
                registerForm.addEventListener('submit', (e) => this.handleRegisterSubmit(e));
            }

            // Keyboard shortcuts
            document.addEventListener('keydown', (e) => {
                // Ctrl/Cmd + Shift + L = open login modal
                if ((e.ctrlKey || e.metaKey) && e.shiftKey && e.key === 'L') {
                    e.preventDefault();
                    this.open();
                }
                // Escape handled by Bootstrap modal
            });
        },

        /**
         * Called when modal starts showing
         */
        onModalShow(e) {
            // Reset state
            this.isAnimating = true;
            this.currentTab = 'login';

            // Reset forms
            this.resetForms();

            // Remove closing class if present
            this.modalEl.classList.remove('closing');

            // Focus first input after animation
            setTimeout(() => {
                const firstInput = this.modalEl.querySelector('#login_email, #reg_username');
                if (firstInput) firstInput.focus();
            }, this.config.animationDuration + 100);
        },

        /**
         * Called when modal is fully shown
         */
        onModalShown(e) {
            this.isAnimating = false;
        },

        /**
         * Called when modal starts hiding
         */
        onModalHide(e) {
            this.isAnimating = true;
            this.modalEl.classList.add('closing');
        },

        /**
         * Called when modal is fully hidden
         */
        onModalHidden(e) {
            this.isAnimating = false;
            this.modalEl.classList.remove('closing');
            this.resetForms();
        },

        /**
         * Open the modal with animation
         */
        open(tab = 'login') {
            if (this.isAnimating) return;

            if (tab !== this.currentTab) {
                this.switchTab(tab, false);
            }

            this.modalInstance.show();
        },

        /**
         * Close the modal with animation
         */
        close() {
            if (this.isAnimating) return;
            this.modalInstance.hide();
        },

        /**
         * Switch between login and register tabs with animation
         */
        switchTab(tab, animate = true) {
            if (this.isAnimating || this.currentTab === tab) return;
            this.isAnimating = true;

            const loginPanel = document.getElementById('authLoginPanel');
            const registerPanel = document.getElementById('authRegisterPanel');
            const tabLogin = document.getElementById('tabBtnLogin');
            const tabRegister = document.getElementById('tabBtnRegister');

            if (!loginPanel || !registerPanel) {
                this.isAnimating = false;
                return;
            }

            // Update tab buttons
            if (tabLogin) {
                tabLogin.classList.toggle('active', tab === 'login');
            }
            if (tabRegister) {
                tabRegister.classList.toggle('active', tab === 'register');
            }

            if (animate) {
                // Animate out current panel
                const currentPanel = this.currentTab === 'login' ? loginPanel : registerPanel;
                const nextPanel = tab === 'login' ? loginPanel : registerPanel;

                currentPanel.classList.add('panel-exit');
                currentPanel.classList.remove('panel-enter');

                setTimeout(() => {
                    currentPanel.style.display = 'none';
                    currentPanel.classList.remove('panel-exit');

                    nextPanel.style.display = 'block';
                    nextPanel.classList.add('panel-enter');

                    // Focus first input of new panel
                    setTimeout(() => {
                        const firstInput = nextPanel.querySelector('input');
                        if (firstInput) firstInput.focus();
                        this.isAnimating = false;
                    }, 100);
                }, 300);
            } else {
                // Instant switch (no animation)
                loginPanel.style.display = tab === 'login' ? 'block' : 'none';
                registerPanel.style.display = tab === 'register' ? 'block' : 'none';
                this.isAnimating = false;
            }

            this.currentTab = tab;
        },

        /**
         * Reset all forms to initial state
         */
        resetForms() {
            const loginForm = document.getElementById('userLoginForm');
            const registerForm = document.getElementById('userRegisterForm');

            if (loginForm) {
                loginForm.reset();
                this.setButtonLoading(loginForm.querySelector('button[type="submit"]'), false);
            }
            if (registerForm) {
                registerForm.reset();
                this.setButtonLoading(registerForm.querySelector('button[type="submit"]'), false);
            }

            // Remove any error states
            this.modalEl.querySelectorAll('.form-control').forEach(input => {
                input.classList.remove('is-invalid', 'is-valid');
            });
        },

        /**
         * Add decorative icons to form inputs
         */
        addInputIcons() {
            const iconMap = {
                'login_email': 'bi-envelope',
                'login_password': 'bi-lock',
                'reg_username': 'bi-person',
                'reg_full_name': 'bi-person-vcard',
                'reg_email': 'bi-envelope',
                'reg_phone': 'bi-telephone',
                'reg_password': 'bi-lock',
                'reg_confirm': 'bi-shield-check'
            };

            Object.entries(iconMap).forEach(([id, iconClass]) => {
                const input = document.getElementById(id);
                if (!input) return;

                const wrapper = input.closest('.mb-3');
                if (!wrapper) return;

                // Check if icon already exists
                if (wrapper.querySelector('.input-icon')) return;

                const icon = document.createElement('i');
                icon.className = `bi ${iconClass} input-icon`;
                wrapper.style.position = 'relative';
                wrapper.appendChild(icon);
            });
        },

        /**
         * Enhance forms with validation animations
         */
        enhanceForms() {
            const inputs = this.modalEl.querySelectorAll('.form-control');

            inputs.forEach(input => {
                // Add floating label behavior
                input.addEventListener('focus', () => {
                    input.closest('.mb-3')?.classList.add('focused');
                });

                input.addEventListener('blur', () => {
                    input.closest('.mb-3')?.classList.remove('focused');
                    this.validateField(input);
                });

                input.addEventListener('input', () => {
                    input.classList.remove('is-invalid');
                });
            });
        },

        /**
         * Validate a single field
         */
        validateField(input) {
            const value = input.value.trim();
            const isValid = input.checkValidity();

            if (value && !isValid) {
                input.classList.add('is-invalid');
                input.classList.remove('is-valid');
            } else if (value && isValid) {
                input.classList.remove('is-invalid');
                input.classList.add('is-valid');
            } else {
                input.classList.remove('is-invalid', 'is-valid');
            }

            return isValid;
        },

        /**
         * Set button loading state
         */
        setButtonLoading(btn, loading) {
            if (!btn) return;

            if (loading) {
                btn.classList.add('loading');
                const originalText = btn.innerHTML;
                btn.dataset.originalText = originalText;
                btn.innerHTML = `
                    <span class="btn-text">${originalText}</span>
                    <span class="btn-spinner">
                        <span class="spinner-border spinner-border-sm" role="status" aria-hidden="true"></span>
                    </span>
                `;
            } else {
                btn.classList.remove('loading');
                if (btn.dataset.originalText) {
                    btn.innerHTML = btn.dataset.originalText;
                }
            }
        },

        /**
         * Create particle burst effect
         */
        createParticleBurst(element) {
            const rect = element.getBoundingClientRect();
            const centerX = rect.left + rect.width / 2;
            const centerY = rect.top + rect.height / 2;

            for (let i = 0; i < this.config.particleCount; i++) {
                const particle = document.createElement('div');
                particle.className = 'auth-particle burst';

                const angle = (Math.PI * 2 * i) / this.config.particleCount;
                const distance = 50 + Math.random() * 50;
                const tx = Math.cos(angle) * distance;
                const ty = Math.sin(angle) * distance;

                particle.style.setProperty('--tx', `${tx}px`);
                particle.style.setProperty('--ty', `${ty}px`);
                particle.style.left = `${centerX}px`;
                particle.style.top = `${centerY}px`;
                particle.style.backgroundColor = this.config.particleColors[
                    Math.floor(Math.random() * this.config.particleColors.length)
                ];

                document.body.appendChild(particle);

                setTimeout(() => particle.remove(), 800);
            }
        },

        /**
         * Shake the modal for error feedback
         */
        shakeModal() {
            const dialog = this.modalEl.querySelector('.modal-dialog');
            if (!dialog) return;

            dialog.classList.remove('auth-shake');
            void dialog.offsetWidth; // Trigger reflow
            dialog.classList.add('auth-shake');

            setTimeout(() => dialog.classList.remove('auth-shake'), this.config.shakeDuration);
        },

        /**
         * Success pulse animation
         */
        successPulse(element) {
            if (!element) return;
            element.classList.remove('auth-success-pulse');
            void element.offsetWidth;
            element.classList.add('auth-success-pulse');
            setTimeout(() => element.classList.remove('auth-success-pulse'), 800);
        },

        /**
         * Handle login form submission
         */
        async handleLoginSubmit(e) {
            e.preventDefault();

            const form = e.target;
            const btn = form.querySelector('button[type="submit"]');
            const emailInput = document.getElementById('login_email');
            const passwordInput = document.getElementById('login_password');

            // Validate
            let isValid = true;
            [emailInput, passwordInput].forEach(input => {
                if (!input.value.trim()) {
                    input.classList.add('is-invalid');
                    isValid = false;
                }
            });

            if (!isValid) {
                this.shakeModal();
                return;
            }

            // Set loading state
            this.setButtonLoading(btn, true);

            try {
                const res = await fetch('/api/login', {
                    method: 'POST',
                    headers: { 'Content-Type': 'application/json' },
                    body: JSON.stringify({
                        email: emailInput.value.trim(),
                        password: passwordInput.value
                    })
                });

                const data = await res.json();

                if (data.success) {
                    // Success animation
                    this.successPulse(btn);
                    this.createParticleBurst(btn);

                    // Show success toast
                    if (typeof showToast === 'function') {
                        showToast('Login berhasil! Selamat datang kembali.', 'success');
                    }

                    // Close modal and reload
                    setTimeout(() => {
                        this.close();
                        setTimeout(() => location.reload(), 300);
                    }, 800);
                } else {
                    // Error
                    this.setButtonLoading(btn, false);
                    this.shakeModal();

                    if (typeof showToast === 'function') {
                        showToast(data.message || 'Login gagal. Periksa kredensial Anda.', 'error');
                    }
                }
            } catch (err) {
                this.setButtonLoading(btn, false);
                this.shakeModal();

                if (typeof showToast === 'function') {
                    showToast('Koneksi bermasalah. Coba lagi.', 'error');
                }
                console.error('Login error:', err);
            }
        },

        /**
         * Handle register form submission
         */
        async handleRegisterSubmit(e) {
            e.preventDefault();

            const form = e.target;
            const btn = form.querySelector('button[type="submit"]');

            const username = document.getElementById('reg_username');
            const fullName = document.getElementById('reg_full_name');
            const email = document.getElementById('reg_email');
            const phone = document.getElementById('reg_phone');
            const password = document.getElementById('reg_password');
            const confirm = document.getElementById('reg_confirm');

            // Validate all fields
            let isValid = true;
            [username, fullName, email, phone, password, confirm].forEach(input => {
                if (!input.value.trim()) {
                    input.classList.add('is-invalid');
                    isValid = false;
                }
            });

            // Password match check
            if (password.value !== confirm.value) {
                confirm.classList.add('is-invalid');
                isValid = false;
                if (typeof showToast === 'function') {
                    showToast('Konfirmasi kata sandi tidak cocok!', 'error');
                }
            }

            // Password length check
            if (password.value.length < 8) {
                password.classList.add('is-invalid');
                isValid = false;
                if (typeof showToast === 'function') {
                    showToast('Kata sandi minimal 8 karakter!', 'error');
                }
            }

            if (!isValid) {
                this.shakeModal();
                return;
            }

            // Set loading state
            this.setButtonLoading(btn, true);

            try {
                const res = await fetch('/api/register', {
                    method: 'POST',
                    headers: { 'Content-Type': 'application/json' },
                    body: JSON.stringify({
                        username: username.value.trim(),
                        full_name: fullName.value.trim(),
                        email: email.value.trim(),
                        phone: phone.value.trim(),
                        password: password.value
                    })
                });

                const data = await res.json();

                if (data.success) {
                    // Success animation
                    this.successPulse(btn);
                    this.createParticleBurst(btn);

                    if (typeof showToast === 'function') {
                        showToast('Registrasi berhasil! Silakan login.', 'success');
                    }

                    // Switch to login tab
                    setTimeout(() => {
                        this.switchTab('login');
                        this.setButtonLoading(btn, false);

                        // Pre-fill email
                        const loginEmail = document.getElementById('login_email');
                        if (loginEmail) loginEmail.value = email.value.trim();
                    }, 800);
                } else {
                    this.setButtonLoading(btn, false);
                    this.shakeModal();

                    if (typeof showToast === 'function') {
                        showToast(data.message || 'Registrasi gagal. Coba lagi.', 'error');
                    }
                }
            } catch (err) {
                this.setButtonLoading(btn, false);
                this.shakeModal();

                if (typeof showToast === 'function') {
                    showToast('Koneksi bermasalah. Coba lagi.', 'error');
                }
                console.error('Register error:', err);
            }
        }
    };

    // Initialize when DOM is ready
    if (document.readyState === 'loading') {
        document.addEventListener('DOMContentLoaded', () => AuthModal.init());
    } else {
        AuthModal.init();
    }

    // Expose to global scope
    window.AuthModal = AuthModal;
})();
