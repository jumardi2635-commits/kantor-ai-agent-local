function productForm() {
    return {
        loading: false,
        showSeo: false,
        showUnsavedModal: false,
        hasUnsavedChanges: false,
        pendingLeaveUrl: null,
        toasts: [],
        _toastId: 0,

        productId: {{ product.id if product else 'null' }},

        /* State form */
        formData: {
            name:    '{{ (product.name or "")|e }}',
            slug:    '{{ (product.slug or "")|e }}',
            brand:   '{{ (product.brand or "")|e }}',
            model:   '{{ (product.model or "")|e }}',
            // FIX: Gunakan // 100 untuk konversi sen→rupiah, tapi handle 0 dengan benar
            price:   {{ (product.price // 100) if product and product.price is not none else 'null' }},
            compare_at_price: {{ (product.compare_at_price // 100) if product and product.compare_at_price is not none else 'null' }},
            cost_price:       {{ (product.cost_price // 100) if product and product.cost_price is not none else 'null' }},
            stock:   {{ product.stock if product else 1 }},
            is_featured: {{ 'true' if product and product.is_featured else 'false' }},
            is_active:   {{ 'true' if product and product.is_active else 'true' }},
            status:  '{{ product.status if product and product.status else "published" }}',
            tags:    {{ (product.tags or '').split(',')|map('trim')|reject('equalto', '')|list|tojson if product else '[]' }},
            specifications: {% if product and product.specifications %}{% if product.specifications is string and product.specifications.startswith('[') %}{{ product.specifications|safe }}{% else %}{{ product.specifications|tojson }}{% endif %}{% else %}[]{% endif %},
        },

        /* Tampilan harga dengan pemisah ribuan */
        priceDisplay: '{{ "{:,}".format((product.price // 100) if product and product.price is not none else 0).replace(",", ".") if product and product.price is not none else "" }}',
        compareAtPriceDisplay: '{{ "{:,}".format((product.compare_at_price // 100) if product and product.compare_at_price is not none else 0).replace(",", ".") if product and product.compare_at_price is not none else "" }}',
        costPriceDisplay: '{{ "{:,}".format((product.cost_price // 100) if product and product.cost_price is not none else 0).replace(",", ".") if product and product.cost_price is not none else "" }}',

        /* HAPUS computed priceCents — tidak lagi dipakai */

        /* Format helpers */
        fmt(num) {
            if (num === null || num === undefined || num === '') return '';
            const n = parseFloat(num);
            if (isNaN(n)) return '';
            return n.toLocaleString('id-ID');
        },

        onPriceInput(event, field) {
            let val = event.target.value.replace(/[^\d]/g, '');
            this.formData[field] = val ? parseInt(val) : null;

            // Update display variable (Alpine sync via :value)
            const map = {
                price: 'priceDisplay',
                compare_at_price: 'compareAtPriceDisplay',
                cost_price: 'costPriceDisplay'
            };
            this[map[field]] = val ? parseInt(val).toLocaleString('id-ID') : '';
        },

        /* Computed untuk hidden inputs */
        get specsJson() {
            const valid = this.formData.specifications.filter(s => s.key && s.key.trim());
            return valid.length ? JSON.stringify(valid) : '';
        },

        /* Gambar */
        imagePreviews: { main: null, 1: null, 2: null, 3: null },
        existingImages: {
            main: {% if product and product.image_filename %}'{{ url_for("uploaded_product_file", filename=product.image_filename) }}'{% else %}null{% endif %},
            1:    {% if product and product.image_filename2 %}'{{ url_for("uploaded_product_file", filename=product.image_filename2) }}'{% else %}null{% endif %},
            2:    {% if product and product.image_filename3 %}'{{ url_for("uploaded_product_file", filename=product.image_filename3) }}'{% else %}null{% endif %},
            3:    {% if product and product.image_filename4 %}'{{ url_for("uploaded_product_file", filename=product.image_filename4) }}'{% else %}null{% endif %}
        },
        removeFlags: { main: false, 1: false, 2: false, 3: false },

        /* Init */
        init() {
            const initialData = JSON.stringify(this.formData);
            this.$watch('formData', () => {
                this.hasUnsavedChanges = JSON.stringify(this.formData) !== initialData;
            }, { deep: true });

            this.$watch('imagePreviews', () => {
                this.hasUnsavedChanges = true;
            }, { deep: true });

            window.addEventListener('beforeunload', (e) => {
                if (this.hasUnsavedChanges) { e.preventDefault(); e.returnValue = ''; }
            });

            document.querySelectorAll('a[href]').forEach(link => {
                if (link.hasAttribute('data-allow-leave')) return;
                const href = link.getAttribute('href') || '';
                if (href.startsWith('#') || href.startsWith('javascript:')) return;
                link.addEventListener('click', (e) => {
                    if (this.hasUnsavedChanges) {
                        e.preventDefault();
                        this.pendingLeaveUrl = link.href;
                        this.showUnsavedModal = true;
                    }
                });
            });

            if ('{{ product.meta_title if product else "" }}') this.showSeo = true;
        },

        confirmLeave() {
            this.hasUnsavedChanges = false;
            if (this.pendingLeaveUrl) window.location.href = this.pendingLeaveUrl;
        },

        /* Toast */
        addToast(title, msg, type = 'success') {
            const id = ++this._toastId;
            this.toasts.push({ id, title, msg, type, show: true });
            setTimeout(() => this.removeToast(id), 5000);
        },
        removeToast(id) {
            const t = this.toasts.find(x => x.id === id);
            if (t) t.show = false;
            setTimeout(() => { this.toasts = this.toasts.filter(x => x.id !== id); }, 300);
        },

        /* Gambar */
        handleFile(event, slot) {
            const file = event.target.files[0];
            if (!file) return;
            if (file.size > 5 * 1024 * 1024) {
                this.addToast('Ukuran terlalu besar', 'Maksimal 5MB per gambar', 'error');
                event.target.value = '';
                return;
            }
            const allowed = ['image/jpeg','image/png','image/webp','image/gif'];
            if (!allowed.includes(file.type)) {
                this.addToast('Format tidak didukung', 'Gunakan JPG, PNG, WebP, atau GIF', 'error');
                event.target.value = '';
                return;
            }
            const reader = new FileReader();
            reader.onload = (e) => { this.imagePreviews[slot] = e.target.result; };
            reader.readAsDataURL(file);
            this.removeFlags[slot] = false;
            this.hasUnsavedChanges = true;
        },
        removeImage(slot) {
            this.imagePreviews[slot] = null;
            this.existingImages[slot] = null;
            this.removeFlags[slot] = true;
            const refMap = { main:'imgMain', 1:'imgSlot1', 2:'imgSlot2', 3:'imgSlot3' };
            const ref = this.$refs[refMap[slot]];
            if (ref) ref.value = '';
            this.hasUnsavedChanges = true;
        },

        /* Slug */
        generateSlug() {
            if (this.productId) return;
            this.formData.slug = this.slugify(this.formData.name);
        },
        slugify(text) {
            return String(text).toLowerCase()
                .replace(/\s+/g, '-')
                .replace(/[^\w\-]+/g, '')
                .replace(/\-\-+/g, '-')
                .replace(/^-+|-+$/g, '');
        },

        /* Tags */
        addTag(event) {
            const val = event.target.value.trim().replace(/,/g, '');
            if (val && !this.formData.tags.includes(val)) this.formData.tags.push(val);
            event.target.value = '';
        },
        removeTag(idx) { this.formData.tags.splice(idx, 1); },

        /* Spesifikasi */
        addSpec() { this.formData.specifications.push({ key: '', value: '' }); },
        removeSpec(idx) { this.formData.specifications.splice(idx, 1); },
        applyPreset(type) {
            const p = {
                smartphone: [
                    {key:'Layar',value:''},{key:'Processor',value:''},{key:'RAM',value:''},
                    {key:'Penyimpanan',value:''},{key:'Kamera Belakang',value:''},{key:'Kamera Depan',value:''},
                    {key:'Baterai',value:''},{key:'Sistem Operasi',value:''}
                ],
                laptop: [
                    {key:'Layar',value:''},{key:'Processor',value:''},{key:'RAM',value:''},
                    {key:'Storage',value:''},{key:'VGA',value:''},{key:'Sistem Operasi',value:''},{key:'Port',value:''}
                ],
                tablet: [
                    {key:'Layar',value:''},{key:'Processor',value:''},{key:'RAM',value:''},
                    {key:'Penyimpanan',value:''},{key:'Kamera',value:''},{key:'Baterai',value:''}
                ]
            };
            this.formData.specifications = p[type] || [];
        },

        /* Margin */
        calcMargin() {
            const p = parseFloat(this.formData.price) || 0;
            const c = parseFloat(this.formData.cost_price) || 0;
            if (p <= 0) return '0%';
            return ((p - c) / p * 100).toFixed(1) + '%';
        },

        /* Submit form */
        handleSubmit(event) {
            if (!this.formData.name || !this.formData.name.trim()) {
                this.addToast('Validasi Gagal', 'Nama produk wajib diisi', 'error');
                event.preventDefault(); return;
            }
            const price = parseFloat(this.formData.price);
            if (!price || price <= 0) {
                this.addToast('Validasi Gagal', 'Harga harus lebih dari 0', 'error');
                event.preventDefault(); return;
            }
            if (!this.formData.slug || !this.formData.slug.trim()) {
                this.formData.slug = this.slugify(this.formData.name);
                event.preventDefault();
                this.$nextTick(() => {
                    document.getElementById('productForm').submit();
                });
                return;
            }
            /* Format display sebelum submit */
            this.priceDisplay = this.fmt(this.formData.price);
            this.compareAtPriceDisplay = this.fmt(this.formData.compare_at_price);
            this.costPriceDisplay = this.fmt(this.formData.cost_price);
            this.$nextTick(() => {
                this.loading = true;
                this.hasUnsavedChanges = false;
            });
        }
    }
}