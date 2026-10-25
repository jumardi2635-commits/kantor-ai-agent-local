from flask_sqlalchemy import SQLAlchemy
from datetime import datetime
from flask_login import UserMixin
from werkzeug.security import generate_password_hash, check_password_hash
import json
import calendar

db = SQLAlchemy()


# =============================================================================
# ADMIN
# =============================================================================
class Admin(UserMixin, db.Model):
    __tablename__ = 'admin'
    id = db.Column(db.Integer, primary_key=True)
    username = db.Column(db.String(80), unique=True, nullable=False, index=True)
    password_hash = db.Column(db.String(200), nullable=False)

    def set_password(self, password):
        self.password_hash = generate_password_hash(password)

    def check_password(self, password):
        return check_password_hash(self.password_hash, password)

    def get_id(self):
        return f"admin_{self.id}"

    def __repr__(self):
        return f'<Admin {self.username}>'


# =============================================================================
# USER (Pelanggan)
# =============================================================================
class User(UserMixin, db.Model):
    __tablename__ = 'user'
    id = db.Column(db.Integer, primary_key=True)
    email = db.Column(db.String(120), unique=True, nullable=False, index=True)
    username = db.Column(db.String(80), unique=True, nullable=False, index=True)
    full_name = db.Column(db.String(150), nullable=True)          # ← nullable=True agar sesuai profile.html
    phone = db.Column(db.String(20), unique=True, index=True)
    address = db.Column(db.Text)
    default_province = db.Column(db.String(100))
    default_city = db.Column(db.String(100))
    default_district = db.Column(db.String(100))
    default_postal_code = db.Column(db.String(10))
    avatar_filename = db.Column(db.String(200))
    password_hash = db.Column(db.String(200), nullable=False)
    is_active = db.Column(db.Boolean, default=True)
    created_at = db.Column(db.DateTime, default=datetime.utcnow)
    updated_at = db.Column(db.DateTime, default=datetime.utcnow, onupdate=datetime.utcnow)

    # Google OAuth (Login dengan Google) — ditambahkan agar cocok dengan
    # app.py (google_login/google_callback). google_id unik & nullable
    # karena user lama (daftar via email/password) tidak punya nilai ini.
    # has_usable_password membedakan user yang benar-benar punya password
    # asli vs user yang hanya bisa login lewat Google (password_hash-nya
    # diisi hash acak yang sengaja tidak pernah bisa cocok, lihat
    # set_unusable_password()).
    google_id = db.Column(db.String(64), unique=True, nullable=True, index=True)
    oauth_provider = db.Column(db.String(20), nullable=True)  # mis. 'google'
    google_avatar_url = db.Column(db.String(500), nullable=True)
    has_usable_password = db.Column(db.Boolean, nullable=True, default=True)

    # KYC Verification Fields
    kyc_verified = db.Column(db.Boolean, default=False)
    kyc_verified_at = db.Column(db.DateTime)
    kyc_verification_method = db.Column(db.String(50))  # 'face_api', 'manual', 'none'
    kyc_face_descriptor = db.Column(db.Text)  # JSON array of 128-dim face descriptor
    kyc_ktp_photo = db.Column(db.String(200))
    kyc_selfie_photo = db.Column(db.String(200))
    kyc_nik = db.Column(db.String(16))
    kyc_match_score = db.Column(db.Float)  # Face match confidence score, percentage 0-100
    kyc_liveness_passed = db.Column(db.Boolean, default=False)  # Anti-spoofing check

    # Relationships
    addresses = db.relationship('Address', backref='user', lazy=True, cascade='all, delete-orphan')
    orders = db.relationship('Order', backref='user', lazy=True)
    installment_applications = db.relationship('InstallmentApplication', backref='user', lazy=True)
    kyc_sessions = db.relationship('KYCVerificationSession', backref='user', lazy=True)

    def set_password(self, password):
        self.password_hash = generate_password_hash(password, method='pbkdf2:sha256')
        self.has_usable_password = True

    def set_unusable_password(self):
        """Dipakai saat user baru dibuat lewat Google OAuth (tidak pernah
        mengisi password sendiri). Mengisi password_hash dengan hash dari
        token acak yang tidak pernah diketahui siapa pun -- secara efektif
        membuat login via /login (email+password) mustahil berhasil untuk
        akun ini -- dan menandai has_usable_password=False supaya UI admin
        / profil bisa membedakan akun Google-only dari akun password biasa."""
        import secrets
        self.password_hash = generate_password_hash(
            secrets.token_urlsafe(32), method='pbkdf2:sha256'
        )
        self.has_usable_password = False

    def check_password(self, password):
        return check_password_hash(self.password_hash, password)

    def get_id(self):
        return f"user_{self.id}"

    def __repr__(self):
        return f'<User {self.username}>'


# =============================================================================
# ADDRESS (Alamat Pengiriman)
# =============================================================================
class Address(db.Model):
    __tablename__ = 'address'
    id = db.Column(db.Integer, primary_key=True)
    user_id = db.Column(db.Integer, db.ForeignKey('user.id'), nullable=False, index=True)
    label = db.Column(db.String(50), nullable=False)
    recipient_name = db.Column(db.String(150), nullable=False)
    recipient_phone = db.Column(db.String(20), nullable=False)
    full_address = db.Column(db.Text, nullable=False)
    province = db.Column(db.String(100), nullable=False)
    city = db.Column(db.String(100), nullable=False)
    district = db.Column(db.String(100), nullable=False)
    postal_code = db.Column(db.String(10), nullable=False)
    is_default = db.Column(db.Boolean, default=False)
    created_at = db.Column(db.DateTime, default=datetime.utcnow)
    updated_at = db.Column(db.DateTime, default=datetime.utcnow, onupdate=datetime.utcnow)

    def to_dict(self):
        return {
            'id': self.id,
            'user_id': self.user_id,
            'label': self.label,
            'recipient_name': self.recipient_name,
            'recipient_phone': self.recipient_phone,
            'full_address': self.full_address,
            'province': self.province,
            'city': self.city,
            'district': self.district,
            'postal_code': self.postal_code,
            'is_default': self.is_default,
        }

    def __repr__(self):
        return f'<Address {self.label} ({self.user_id})>'


# =============================================================================
# PRODUCT
# =============================================================================
class Product(db.Model):
    __tablename__ = 'product'
    id = db.Column(db.Integer, primary_key=True)

    # Informasi Dasar
    name = db.Column(db.String(200), nullable=False)
    slug = db.Column(db.String(200), unique=True, nullable=True, index=True)
    brand = db.Column(db.String(100), nullable=False)
    model = db.Column(db.String(100))
    sku = db.Column(db.String(50), index=True)
    barcode = db.Column(db.String(100))
    category = db.Column(db.String(100), index=True)
    subcategory = db.Column(db.String(100))
    tags = db.Column(db.String(500))

    # Harga
    price = db.Column(db.Integer, nullable=False)
    compare_at_price = db.Column(db.Integer)
    cost_price = db.Column(db.Integer)

    # Kondisi & Stok
    condition = db.Column(db.String(50))
    stock = db.Column(db.Integer, default=1)
    low_stock_alert = db.Column(db.Integer, default=5)

    # Deskripsi
    short_description = db.Column(db.Text)
    description = db.Column(db.Text)
    specifications = db.Column(db.JSON)

    # Ulasan
    rating = db.Column(db.Float, default=0.0)
    review_count = db.Column(db.Integer, default=0)

    # Dimensi & Berat
    weight = db.Column(db.Float)
    width = db.Column(db.Float)
    length = db.Column(db.Float)
    height = db.Column(db.Float)

    # Galeri Gambar
    image_filename = db.Column(db.String(200))
    image_filename2 = db.Column(db.String(200))
    image_filename3 = db.Column(db.String(200))
    image_filename4 = db.Column(db.String(200))

    # Status
    status = db.Column(db.String(20), default='published')
    is_featured = db.Column(db.Boolean, default=False)
    is_active = db.Column(db.Boolean, default=True)

    # SEO
    meta_title = db.Column(db.String(200))
    meta_description = db.Column(db.Text)
    meta_keywords = db.Column(db.String(250))

    created_at = db.Column(db.DateTime, default=datetime.utcnow)
    updated_at = db.Column(db.DateTime, onupdate=datetime.utcnow)

    def get_images(self):
        images = []
        for attr in ['image_filename', 'image_filename2', 'image_filename3', 'image_filename4']:
            val = getattr(self, attr)
            if val:
                images.append(val)
        return images

    @property
    def is_in_stock(self):
        stock = self.stock if self.stock is not None else 0
        active = self.is_active if self.is_active is not None else True
        return stock > 0 and active

    @property
    def is_low_stock(self):
        stock = self.stock if self.stock is not None else 0
        low_alert = self.low_stock_alert if self.low_stock_alert is not None else 5
        return 0 < stock <= low_alert

    @property
    def discount_percentage(self):
        if (self.compare_at_price and self.price and
            self.compare_at_price > self.price):
            discount = ((self.compare_at_price - self.price) / self.compare_at_price) * 100
            return round(discount, 2)
        return 0.0

    def __repr__(self):
        return f'<Product {self.name}>'


# =============================================================================
# SETTING
# =============================================================================
class Setting(db.Model):
    __tablename__ = 'setting'
    id = db.Column(db.Integer, primary_key=True)
    key = db.Column(db.String(100), unique=True, nullable=False, index=True)
    value = db.Column(db.Text, nullable=True)
    description = db.Column(db.Text, nullable=True)

    def __repr__(self):
        return f'<Setting {self.key}>'

    @classmethod
    def get(cls, key, default=None):
        s = cls.query.filter_by(key=key).first()
        return s.value if s and s.value is not None else default

    @classmethod
    def set(cls, key, value, description=None):
        s = cls.query.filter_by(key=key).first()
        if s:
            s.value = value
            if description:
                s.description = description
        else:
            s = cls(key=key, value=value, description=description or '')
            db.session.add(s)
        db.session.commit()
        return s

    @classmethod
    def get_bool(cls, key, default=False):
        val = cls.get(key, '').lower()
        return val in ('true', '1', 'yes', 'on') if val else default

    @classmethod
    def get_int(cls, key, default=0):
        try:
            return int(float(cls.get(key, default)))
        except (ValueError, TypeError):
            return default

    @classmethod
    def get_json(cls, key, default=None):
        try:
            return json.loads(cls.get(key, '{}') or '{}')
        except (ValueError, json.JSONDecodeError):
            return default or {}


# =============================================================================
# AI CONFIG (OpenRouter)
# =============================================================================
class AIConfig(db.Model):
    __tablename__ = 'ai_config'
    id = db.Column(db.Integer, primary_key=True)
    name = db.Column(db.String(100), default='default')
    is_active = db.Column(db.Boolean, default=True)
    base_url = db.Column(db.String(500), default='https://openrouter.ai/api/v1')
    api_key = db.Column(db.String(500))
    model = db.Column(db.String(200), default='openrouter/free')
    site_url = db.Column(db.String(500))
    app_name = db.Column(db.String(200))
    system_prompt = db.Column(db.Text)
    notify_new_order = db.Column(db.Boolean, default=False)
    notify_status_update = db.Column(db.Boolean, default=False)
    notify_product_update = db.Column(db.Boolean, default=False)
    webhook_secret = db.Column(db.String(500))
    created_at = db.Column(db.DateTime, default=datetime.utcnow)
    updated_at = db.Column(db.DateTime, default=datetime.utcnow, onupdate=datetime.utcnow)

    def __repr__(self):
        return f'<AIConfig {self.name} ({self.model})>'

    @classmethod
    def get_default(cls):
        config = cls.query.filter_by(is_active=True).order_by(cls.updated_at.desc()).first()
        if not config:
            config = cls(name='default')
            db.session.add(config)
            db.session.commit()
        return config

    def to_dict(self):
        return {
            'id': self.id,
            'name': self.name,
            'is_active': self.is_active,
            'base_url': self.base_url,
            'api_key': '***' if self.api_key else '',
            'model': self.model,
            'site_url': self.site_url,
            'app_name': self.app_name,
            'system_prompt': self.system_prompt,
            'notify_new_order': self.notify_new_order,
            'notify_status_update': self.notify_status_update,
            'notify_product_update': self.notify_product_update,
            'updated_at': self.updated_at.isoformat() if self.updated_at else None,
        }

    def get_headers(self):
        headers = {
            'Authorization': f'Bearer {self.api_key}',
            'Content-Type': 'application/json',
        }
        if self.site_url:
            headers['HTTP-Referer'] = self.site_url
        if self.app_name:
            headers['X-Title'] = self.app_name
        return headers

    def get_chat_payload(self, user_message, temperature=0.7, max_tokens=None):
        messages = []
        if self.system_prompt:
            messages.append({'role': 'system', 'content': self.system_prompt})
        messages.append({'role': 'user', 'content': user_message})

        payload = {
            'model': self.model,
            'messages': messages,
            'stream': False,
            'temperature': temperature,
        }
        if max_tokens:
            payload['max_tokens'] = max_tokens
        return payload


# =============================================================================
# TESTIMONIAL
# =============================================================================
class Testimonial(db.Model):
    __tablename__ = 'testimonial'
    id = db.Column(db.Integer, primary_key=True)
    name = db.Column(db.String(100), nullable=False)
    location = db.Column(db.String(100))
    rating = db.Column(db.Integer, default=5)
    text = db.Column(db.Text, nullable=False)
    image_filename = db.Column(db.String(200))
    is_active = db.Column(db.Boolean, default=True)
    sort_order = db.Column('order', db.Integer, default=0)
    created_at = db.Column(db.DateTime, default=datetime.utcnow)

    def __repr__(self):
        return f'<Testimonial {self.name}>'


# =============================================================================
# HERO SLIDE
# =============================================================================
class HeroSlide(db.Model):
    __tablename__ = 'hero_slide'
    id = db.Column(db.Integer, primary_key=True)
    image_filename = db.Column(db.String(200), nullable=False)
    title = db.Column(db.String(200))
    subtitle = db.Column(db.String(200))
    button_text = db.Column(db.String(50))
    button_url = db.Column(db.String(200))
    sort_order = db.Column('order', db.Integer, default=0)
    is_active = db.Column(db.Boolean, default=True)
    created_at = db.Column(db.DateTime, default=datetime.utcnow)

    def __repr__(self):
        return f'<HeroSlide {self.id}>'


# =============================================================================
# ORDER
# =============================================================================
class Order(db.Model):
    __tablename__ = 'orders'
    id = db.Column(db.Integer, primary_key=True)
    order_number = db.Column(db.String(20), unique=True, nullable=False, index=True)
    customer_name = db.Column(db.String(100), nullable=False)
    customer_phone = db.Column(db.String(20), nullable=False)
    customer_email = db.Column(db.String(100))
    shipping_address = db.Column(db.Text, nullable=False)
    payment_method = db.Column(db.String(50), default='Transfer Bank')
    # Pembayaran otomatis (NotivGo) — nullable, ditambahkan otomatis oleh sync_missing_columns()
    payment_provider = db.Column(db.String(30))
    payment_ref = db.Column(db.String(100), index=True)
    payment_url = db.Column(db.String(500))
    payment_unique_code = db.Column(db.Integer)
    status = db.Column(db.String(20), default='Pending', index=True)
    priority = db.Column(db.String(10), default='normal')
    notes = db.Column(db.Text)
    subtotal = db.Column(db.Integer, nullable=False)
    shipping_cost = db.Column(db.Integer, default=0)
    discount = db.Column(db.Integer, default=0)
    total = db.Column(db.Integer, nullable=False)
    user_id = db.Column(db.Integer, db.ForeignKey('user.id'), nullable=True, index=True)
    paid_at = db.Column(db.DateTime)
    shipped_at = db.Column(db.DateTime)
    completed_at = db.Column(db.DateTime)
    carrier_name = db.Column(db.String(50))
    tracking_number = db.Column(db.String(100), index=True)
    estimated_delivery = db.Column(db.DateTime)
    cancel_reason = db.Column(db.Text)
    return_reason = db.Column(db.Text)
    return_resolution = db.Column(db.String(50))
    return_notes = db.Column(db.Text)
    sla_deadline = db.Column(db.DateTime)
    payment_proof = db.Column(db.String(200))

    # Credit/Installment Order Fields
    is_credit_order = db.Column(db.Boolean, default=False)
    credit_application_id = db.Column(db.Integer, db.ForeignKey('installment_applications.id'), nullable=True)
    credit_down_payment = db.Column(db.Integer, default=0)
    credit_monthly_installment = db.Column(db.Integer, default=0)
    credit_tenor_months = db.Column(db.Integer, default=0)
    credit_remaining_balance = db.Column(db.Integer, default=0)

    # Shipment fields
    shipping_recipient_name = db.Column(db.String(150))
    shipping_phone = db.Column(db.String(20))
    shipping_province = db.Column(db.String(100))
    shipping_city = db.Column(db.String(100))
    shipping_district = db.Column(db.String(100))
    shipping_postal_code = db.Column(db.String(10))
    shipping_courier = db.Column(db.String(50))
    shipping_service = db.Column(db.String(50))
    shipping_notes = db.Column(db.Text)
    created_at = db.Column(db.DateTime, default=datetime.utcnow)
    updated_at = db.Column(db.DateTime, default=datetime.utcnow, onupdate=datetime.utcnow)

    items = db.relationship('OrderItem', backref='order', lazy=True, cascade='all, delete-orphan')
    credit_application = db.relationship('InstallmentApplication', backref=db.backref('created_orders', lazy=True), foreign_keys=[credit_application_id])
    shipping_tracking = db.relationship('ShippingTracking', backref='order', uselist=False, lazy=True)

    def __repr__(self):
        return f'<Order {self.order_number}>'


# =============================================================================
# ORDER ITEM
# =============================================================================
class OrderItem(db.Model):
    __tablename__ = 'order_items'
    id = db.Column(db.Integer, primary_key=True)
    order_id = db.Column(db.Integer, db.ForeignKey('orders.id'), nullable=False, index=True)
    product_id = db.Column(db.Integer, db.ForeignKey('product.id', ondelete='SET NULL'), nullable=True)
    name = db.Column(db.String(200))
    brand = db.Column(db.String(100))
    model = db.Column(db.String(100))
    condition = db.Column(db.String(50))
    price = db.Column(db.Integer)
    quantity = db.Column(db.Integer, default=1)

    product = db.relationship('Product', backref='order_items')

    def __repr__(self):
        return f'<OrderItem {self.id}>'


# =============================================================================
# INSTALLMENT PLAN (Skema Cicilan)
# =============================================================================
class InstallmentPlan(db.Model):
    __tablename__ = 'installment_plans'

    id = db.Column(db.Integer, primary_key=True)
    name = db.Column(db.String(100), nullable=False)
    tenor_months = db.Column(db.Integer, nullable=False)
    interest_rate_monthly = db.Column(db.Float, nullable=False)
    interest_type = db.Column(db.String(20), default='flat')
    min_dp_percentage = db.Column(db.Float, default=0.30)
    max_dp_percentage = db.Column(db.Float, default=0.80)
    min_order_amount = db.Column(db.Integer, default=500000)
    max_order_amount = db.Column(db.Integer, default=500000000)
    processing_fee = db.Column(db.Integer, default=0)
    late_fee_per_day = db.Column(db.Integer, default=5000)
    grace_period_days = db.Column(db.Integer, default=3)
    is_active = db.Column(db.Boolean, default=True)
    description = db.Column(db.Text)
    created_at = db.Column(db.DateTime, default=datetime.utcnow)

    def calculate_installment(self, principal, dp_amount):
        loan_amount = principal - dp_amount
        if loan_amount <= 0:
            return 0

        if self.interest_type == 'flat':
            total_interest = loan_amount * self.interest_rate_monthly * self.tenor_months
            total_payment = loan_amount + total_interest + (self.processing_fee or 0)
            monthly_payment = total_payment / self.tenor_months
        else:
            r = self.interest_rate_monthly
            n = self.tenor_months
            if r == 0:
                monthly_payment = loan_amount / n
            else:
                monthly_payment = loan_amount * (r * (1 + r)**n) / ((1 + r)**n - 1)
            monthly_payment += (self.processing_fee or 0) / n

        return round(monthly_payment)

    def to_dict(self):
        return {
            'id': self.id,
            'name': self.name,
            'tenor_months': self.tenor_months,
            'interest_rate_monthly': self.interest_rate_monthly,
            'interest_rate_annual': round(self.interest_rate_monthly * 12 * 100, 2),
            'interest_type': self.interest_type,
            'min_dp_percentage': self.min_dp_percentage,
            'max_dp_percentage': self.max_dp_percentage,
            'processing_fee': self.processing_fee,
            'late_fee_per_day': self.late_fee_per_day,
            'grace_period_days': self.grace_period_days,
            'is_active': self.is_active,
            'description': self.description
        }

    def __repr__(self):
        return f'<InstallmentPlan {self.name}>'


# =============================================================================
# KYC VERIFICATION SESSION (Biometric Verification Log)
# =============================================================================
class KYCVerificationSession(db.Model):
    __tablename__ = 'kyc_verification_sessions'

    id = db.Column(db.Integer, primary_key=True)
    user_id = db.Column(db.Integer, db.ForeignKey('user.id'), nullable=False, index=True)
    session_token = db.Column(db.String(100), unique=True, nullable=False, index=True)

    # Face API Data
    ktp_face_descriptor = db.Column(db.Text)  # JSON 128-dim array
    selfie_face_descriptor = db.Column(db.Text)  # JSON 128-dim array
    face_match_score = db.Column(db.Float)  # Euclidean distance
    face_match_confidence = db.Column(db.Float)  # 0-100%
    liveness_score = db.Column(db.Float)  # Anti-spoofing score

    # Documents
    ktp_photo_path = db.Column(db.String(200))
    selfie_photo_path = db.Column(db.String(200))

    # KTP Data
    ktp_nik = db.Column(db.String(16))
    ktp_name = db.Column(db.String(150))
    ktp_address = db.Column(db.Text)

    # Data Tambahan — Pekerjaan & Kontak Darurat (untuk kelayakan kredit/cicilan)
    birth_date = db.Column(db.Date)
    occupation = db.Column(db.String(150))
    employment_status = db.Column(db.String(50))
    monthly_income = db.Column(db.Integer)
    emergency_contact_name = db.Column(db.String(150))
    emergency_contact_phone = db.Column(db.String(20))
    emergency_contact_relation = db.Column(db.String(50))

    # Verification Status
    status = db.Column(db.String(20), default='pending_review', index=True)  # pending_review, verified, rejected, cancelled
    verification_method = db.Column(db.String(50), default='face_api')

    # Admin Review — sebelumnya alasan penolakan hanya dikirim lewat notifikasi
    # WhatsApp dan tidak pernah disimpan, jadi hilang begitu selesai diproses.
    rejection_reason = db.Column(db.Text)
    reviewed_by = db.Column(db.Integer, db.ForeignKey('admin.id'))

    # Timestamps
    created_at = db.Column(db.DateTime, default=datetime.utcnow)
    expires_at = db.Column(db.DateTime)
    verified_at = db.Column(db.DateTime)

    # IP & Device Info
    ip_address = db.Column(db.String(45))
    user_agent = db.Column(db.Text)

    reviewed_by_admin = db.relationship('Admin', foreign_keys=[reviewed_by])

    def is_expired(self):
        if self.expires_at:
            return datetime.utcnow() > self.expires_at
        return False

    def to_dict(self):
        return {
            'id': self.id,
            'session_token': self.session_token,
            'status': self.status,
            'face_match_score': self.face_match_score,
            'face_match_confidence': self.face_match_confidence,
            'liveness_score': self.liveness_score,
            'ktp_nik': self.ktp_nik,
            'ktp_name': self.ktp_name,
            'ktp_address': self.ktp_address,
            'birth_date': self.birth_date.isoformat() if self.birth_date else None,
            'occupation': self.occupation,
            'employment_status': self.employment_status,
            'monthly_income': self.monthly_income,
            'emergency_contact_name': self.emergency_contact_name,
            'emergency_contact_phone': self.emergency_contact_phone,
            'emergency_contact_relation': self.emergency_contact_relation,
            'ktp_photo_path': self.ktp_photo_path,
            'selfie_photo_path': self.selfie_photo_path,
            'verification_method': self.verification_method,
            'rejection_reason': self.rejection_reason,
            'ip_address': self.ip_address,
            'created_at': self.created_at.isoformat() if self.created_at else None,
            'expires_at': self.expires_at.isoformat() if self.expires_at else None,
            'verified_at': self.verified_at.isoformat() if self.verified_at else None,
        }

    def __repr__(self):
        return f'<KYCVerificationSession {self.session_token}>'


# =============================================================================
# INSTALLMENT APPLICATION (Pengajuan Cicilan dengan KYC)
# =============================================================================
class InstallmentApplication(db.Model):
    __tablename__ = 'installment_applications'

    id = db.Column(db.Integer, primary_key=True)
    application_code = db.Column(db.String(20), unique=True, nullable=False, index=True)

    user_id = db.Column(db.Integer, db.ForeignKey('user.id'), nullable=False, index=True)
    order_id = db.Column(db.Integer, db.ForeignKey('orders.id'), nullable=True)
    plan_id = db.Column(db.Integer, db.ForeignKey('installment_plans.id'), nullable=True)

    # KYC Link
    kyc_session_id = db.Column(db.Integer, db.ForeignKey('kyc_verification_sessions.id'), nullable=True)

    full_name = db.Column(db.String(150), nullable=False)
    nik = db.Column(db.String(16), nullable=False)
    birth_place = db.Column(db.String(100))
    birth_date = db.Column(db.Date)
    gender = db.Column(db.String(10))
    marital_status = db.Column(db.String(20))

    phone = db.Column(db.String(20), nullable=False)
    email = db.Column(db.String(120))
    emergency_contact_name = db.Column(db.String(150))
    emergency_contact_phone = db.Column(db.String(20))
    emergency_contact_relation = db.Column(db.String(50))

    address_ktp = db.Column(db.Text, nullable=False)
    address_domisili = db.Column(db.Text)
    is_domisili_same = db.Column(db.Boolean, default=True)

    employment_type = db.Column(db.String(50))
    company_name = db.Column(db.String(150))
    company_address = db.Column(db.Text)
    job_title = db.Column(db.String(100))
    work_duration_months = db.Column(db.Integer)
    monthly_income = db.Column(db.Integer)
    other_income = db.Column(db.Integer, default=0)

    # Documents
    ktp_photo = db.Column(db.String(200))
    selfie_photo = db.Column(db.String(200))
    slip_gaji_photo = db.Column(db.String(200))
    npwp_photo = db.Column(db.String(200))
    kk_photo = db.Column(db.String(200))

    # Financial
    product_amount = db.Column(db.Integer, nullable=False)
    dp_amount = db.Column(db.Integer, nullable=False)
    loan_amount = db.Column(db.Integer, nullable=False)
    interest_amount = db.Column(db.Integer, default=0)
    processing_fee_amount = db.Column(db.Integer, default=0)
    total_loan = db.Column(db.Integer, nullable=False)
    monthly_installment = db.Column(db.Integer, nullable=False)

    # Status
    status = db.Column(db.String(20), default='pending', index=True)
    rejection_reason = db.Column(db.Text)
    approved_by = db.Column(db.Integer, db.ForeignKey('admin.id'))
    approved_at = db.Column(db.DateTime)
    rejected_by = db.Column(db.Integer, db.ForeignKey('admin.id'))
    rejected_at = db.Column(db.DateTime)

    # Risk Assessment
    credit_score = db.Column(db.Integer)
    risk_level = db.Column(db.String(20))

    # Biometric Verification Data
    biometric_verified = db.Column(db.Boolean, default=False)
    biometric_match_score = db.Column(db.Float)
    biometric_liveness_passed = db.Column(db.Boolean, default=False)

    # Metadata
    ip_address = db.Column(db.String(45))
    user_agent = db.Column(db.Text)
    created_at = db.Column(db.DateTime, default=datetime.utcnow)
    updated_at = db.Column(db.DateTime, default=datetime.utcnow, onupdate=datetime.utcnow)

    # Product reference (for direct product credit)
    product_id = db.Column(db.Integer, db.ForeignKey('product.id'), nullable=True)
    product_price = db.Column(db.Integer, default=0)
    down_payment = db.Column(db.Integer, default=0)
    down_payment_pct = db.Column(db.Float, default=0.0)
    principal = db.Column(db.Integer, default=0)
    tenor_months = db.Column(db.Integer, default=0)
    interest_type = db.Column(db.String(20), default='flat')
    interest_rate_monthly = db.Column(db.Float, default=0.0)
    interest_rate_annual = db.Column(db.Float, default=0.0)
    total_interest = db.Column(db.Integer, default=0)
    total_payment = db.Column(db.Integer, default=0)
    ktp_name = db.Column(db.String(150))
    ktp_nik = db.Column(db.String(16))
    ktp_address = db.Column(db.Text)
    employment_status = db.Column(db.String(50))

    order = db.relationship('Order', backref=db.backref('installment_applications', lazy=True), foreign_keys=[order_id])
    product = db.relationship('Product', backref='installment_applications')
    plan = db.relationship('InstallmentPlan', backref='applications')
    kyc_session = db.relationship('KYCVerificationSession', backref='installment_applications')
    schedules = db.relationship('InstallmentSchedule', backref='application', lazy=True, cascade='all, delete-orphan')
    payments = db.relationship('InstallmentPayment', backref='application', lazy=True)


    def generate_schedule(self):
        """Generate jadwal angsuran tanpa dependensi dateutil."""
        schedule = []
        start_date = self.approved_at or datetime.utcnow()

        # Safely get tenor
        tenor = 0
        if self.plan and hasattr(self.plan, 'tenor_months') and self.plan.tenor_months:
            tenor = self.plan.tenor_months
        elif self.tenor_months:
            tenor = self.tenor_months

        if tenor <= 0:
            return schedule  # Return empty if no valid tenor

        # Safely get amounts
        loan_amount = self.loan_amount or self.principal or 0
        monthly_installment = self.monthly_installment or 0
        interest_amount = self.interest_amount or self.total_interest or 0

        principal_per_month = loan_amount // tenor if tenor > 0 else 0
        interest_per_month = interest_amount // tenor if tenor > 0 else 0

        for i in range(1, tenor + 1):
            # Hitung due_date dengan calendar bawaan
            month = start_date.month + i
            year = start_date.year + (month - 1) // 12
            month = ((month - 1) % 12) + 1
            day = min(start_date.day, calendar.monthrange(year, month)[1])
            due_date = start_date.replace(year=year, month=month, day=day)

            installment = InstallmentSchedule(
                application_id=self.id,
                installment_number=i,
                due_date=due_date,
                amount=monthly_installment,
                principal_amount=principal_per_month,
                interest_amount=interest_per_month,
                status='pending'
            )
            schedule.append(installment)

        return schedule

    def to_dict(self):
        return {
            'id': self.id,
            'application_code': self.application_code,
            'status': self.status,
            'full_name': self.full_name,
            'nik': self.nik,
            'phone': self.phone,
            'product_amount': self.product_amount,
            'dp_amount': self.dp_amount,
            'loan_amount': self.loan_amount,
            'monthly_installment': self.monthly_installment,
            'tenor_months': self.plan.tenor_months if self.plan else None,
            'interest_rate': self.plan.interest_rate_monthly if self.plan else None,
            'interest_type': self.plan.interest_type if self.plan else 'flat',
            'created_at': self.created_at.isoformat() if self.created_at else None,
            'plan': self.plan.to_dict() if self.plan else None,
            'biometric_verified': self.biometric_verified,
            'biometric_match_score': self.biometric_match_score,
        }

    def __repr__(self):
        return f'<InstallmentApplication {self.application_code}>'


# =============================================================================
# INSTALLMENT SCHEDULE
# =============================================================================
class InstallmentSchedule(db.Model):
    __tablename__ = 'installment_schedules'

    id = db.Column(db.Integer, primary_key=True)
    application_id = db.Column(db.Integer, db.ForeignKey('installment_applications.id'), nullable=False, index=True)
    installment_number = db.Column(db.Integer, nullable=False)

    due_date = db.Column(db.DateTime, nullable=False)
    amount = db.Column(db.Integer, nullable=False)
    principal_amount = db.Column(db.Integer, nullable=False)
    interest_amount = db.Column(db.Integer, nullable=False)

    paid_amount = db.Column(db.Integer, default=0)
    paid_at = db.Column(db.DateTime)
    payment_method = db.Column(db.String(50))
    payment_proof = db.Column(db.String(200))

    status = db.Column(db.String(20), default='pending')

    late_days = db.Column(db.Integer, default=0)
    late_fee = db.Column(db.Integer, default=0)

    notes = db.Column(db.Text)
    created_at = db.Column(db.DateTime, default=datetime.utcnow)
    updated_at = db.Column(db.DateTime, default=datetime.utcnow, onupdate=datetime.utcnow)

    payments = db.relationship('InstallmentPayment', backref='schedule', lazy=True)

    @property
    def installment_no(self):
        return self.installment_number

    @property
    def total_amount(self):
        return self.amount

    def calculate_late_fee(self, plan):
        if self.status in ['paid', 'waived'] or not self.due_date:
            return 0

        now = datetime.utcnow()
        if now <= self.due_date:
            return 0

        days_late = (now - self.due_date).days
        grace = plan.grace_period_days if plan else 3

        if days_late <= grace:
            return 0

        effective_days = days_late - grace
        daily_fee = plan.late_fee_per_day if plan else 5000

        return effective_days * daily_fee

    def get_total_due(self):
        return self.amount + self.late_fee - self.paid_amount

    def to_dict(self):
        return {
            'id': self.id,
            'installment_number': self.installment_number,
            'due_date': self.due_date.isoformat() if self.due_date else None,
            'amount': self.amount,
            'principal_amount': self.principal_amount,
            'interest_amount': self.interest_amount,
            'paid_amount': self.paid_amount,
            'paid_at': self.paid_at.isoformat() if self.paid_at else None,
            'status': self.status,
            'late_days': self.late_days,
            'late_fee': self.late_fee,
            'total_due': self.get_total_due()
        }

    def __repr__(self):
        return f'<InstallmentSchedule {self.installment_number}>'


# =============================================================================
# INSTALLMENT PAYMENT HISTORY
# =============================================================================
class InstallmentPayment(db.Model):
    __tablename__ = 'installment_payments'

    id = db.Column(db.Integer, primary_key=True)
    schedule_id = db.Column(db.Integer, db.ForeignKey('installment_schedules.id'), nullable=False, index=True)
    application_id = db.Column(db.Integer, db.ForeignKey('installment_applications.id'), nullable=False, index=True)

    amount = db.Column(db.Integer, nullable=False)
    payment_date = db.Column(db.DateTime, default=datetime.utcnow)
    payment_method = db.Column(db.String(50))
    payment_proof = db.Column(db.String(200))
    reference_number = db.Column(db.String(100))

    notes = db.Column(db.Text)
    processed_by = db.Column(db.Integer, db.ForeignKey('admin.id'))
    created_at = db.Column(db.DateTime, default=datetime.utcnow)

    def __repr__(self):
        return f'<InstallmentPayment {self.id}>'


# =============================================================================
# SHIPPING TRACKING
# =============================================================================
class ShippingTracking(db.Model):
    __tablename__ = 'shipping_tracking'

    id = db.Column(db.Integer, primary_key=True)
    order_id = db.Column(db.Integer, db.ForeignKey('orders.id'), nullable=False, index=True)
    tracking_number = db.Column(db.String(100), nullable=False, index=True)
    carrier = db.Column(db.String(50), nullable=False, default='Kurir')

    recipient_name = db.Column(db.String(150))
    recipient_phone = db.Column(db.String(20))
    recipient_address = db.Column(db.Text)

    shipping_cost = db.Column(db.Integer, default=0)
    total_cost = db.Column(db.Integer, default=0)

    status = db.Column(db.String(50), default='Pending_Pickup')
    current_location = db.Column(db.String(200))
    current_lat = db.Column(db.Float)
    current_lng = db.Column(db.Float)

    estimated_delivery = db.Column(db.DateTime)
    actual_delivery = db.Column(db.DateTime)

    is_delayed = db.Column(db.Boolean, default=False)
    delay_days = db.Column(db.Integer, default=0)

    has_issue = db.Column(db.Boolean, default=False)
    issue_type = db.Column(db.String(50))
    issue_description = db.Column(db.Text)
    issue_reported_date = db.Column(db.DateTime)

    created_at = db.Column(db.DateTime, default=datetime.utcnow)
    updated_at = db.Column(db.DateTime, default=datetime.utcnow, onupdate=datetime.utcnow)

    events = db.relationship('ShippingTrackingEvent', backref='shipment', lazy=True,
                               cascade='all, delete-orphan', order_by='ShippingTrackingEvent.event_time.desc()')
    issues = db.relationship('ShippingIssue', backref='shipment', lazy=True)

    def __repr__(self):
        return f'<ShippingTracking {self.tracking_number}>'

    def to_dict(self):
        return {
            'id': self.id,
            'order_id': self.order_id,
            'tracking_number': self.tracking_number,
            'carrier': self.carrier,
            'recipient_name': self.recipient_name,
            'recipient_phone': self.recipient_phone,
            'recipient_address': self.recipient_address,
            'status': self.status,
            'current_location': self.current_location,
            'current_lat': self.current_lat,
            'current_lng': self.current_lng,
            'estimated_delivery': self.estimated_delivery.isoformat() if self.estimated_delivery else None,
            'actual_delivery': self.actual_delivery.isoformat() if self.actual_delivery else None,
            'is_delayed': self.is_delayed,
            'delay_days': self.delay_days,
            'has_issue': self.has_issue,
            'issue_type': self.issue_type,
            'issue_description': self.issue_description,
            'created_at': self.created_at.isoformat() if self.created_at else None,
        }


class ShippingTrackingEvent(db.Model):
    __tablename__ = 'shipping_tracking_events'

    id = db.Column(db.Integer, primary_key=True)
    shipping_tracking_id = db.Column(db.Integer, db.ForeignKey('shipping_tracking.id'), nullable=False, index=True)

    status = db.Column(db.String(50), nullable=False)
    status_description = db.Column(db.Text)
    location = db.Column(db.String(200))
    event_type = db.Column(db.String(50), default='status_update')
    event_time = db.Column(db.DateTime, default=datetime.utcnow)

    metadata_json = db.Column(db.Text)
    created_at = db.Column(db.DateTime, default=datetime.utcnow)

    def __repr__(self):
        return f'<ShippingTrackingEvent {self.status} @ {self.location}>'

    def to_dict(self):
        return {
            'id': self.id,
            'shipping_tracking_id': self.shipping_tracking_id,
            'status': self.status,
            'status_description': self.status_description,
            'location': self.location,
            'event_type': self.event_type,
            'event_time': self.event_time.isoformat() if self.event_time else None,
            'metadata': json.loads(self.metadata_json) if self.metadata_json else None,
        }


class ShippingIssue(db.Model):
    __tablename__ = 'shipping_issues'

    id = db.Column(db.Integer, primary_key=True)
    shipping_tracking_id = db.Column(db.Integer, db.ForeignKey('shipping_tracking.id'), nullable=False, index=True)

    issue_type = db.Column(db.String(50), nullable=False)
    description = db.Column(db.Text, nullable=False)
    severity = db.Column(db.String(20), default='medium')

    reported_at = db.Column(db.DateTime, default=datetime.utcnow)
    resolved_at = db.Column(db.DateTime)
    resolution = db.Column(db.Text)
    resolved_by = db.Column(db.Integer, db.ForeignKey('admin.id'))

    status = db.Column(db.String(20), default='open')

    created_at = db.Column(db.DateTime, default=datetime.utcnow)
    updated_at = db.Column(db.DateTime, default=datetime.utcnow, onupdate=datetime.utcnow)

    def __repr__(self):
        return f'<ShippingIssue {self.issue_type}>'


class ApiKey(db.Model):
    """API Key untuk partner integrasi eksternal (Partner API v1)."""
    __tablename__ = 'api_keys'

    id = db.Column(db.Integer, primary_key=True)
    key = db.Column(db.String(64), unique=True, nullable=False, index=True)
    partner_name = db.Column(db.String(200), nullable=False)
    is_active = db.Column(db.Boolean, default=True)
    created_at = db.Column(db.DateTime, default=datetime.utcnow)
    last_used_at = db.Column(db.DateTime)

    def __repr__(self):
        return f'<ApiKey {self.partner_name}>'

    def masked_key(self):
        if not self.key:
            return ''
        return f"{self.key[:7]}{'•' * 10}{self.key[-4:]}"

    def to_dict(self):
        return {
            'id': self.id,
            'partner_name': self.partner_name,
            'is_active': self.is_active,
            'created_at': self.created_at.isoformat() if self.created_at else None,
            'last_used_at': self.last_used_at.isoformat() if self.last_used_at else None,
        }


class ShippingCarrierPerformance(db.Model):
    __tablename__ = 'shipping_carrier_performance'

    id = db.Column(db.Integer, primary_key=True)
    carrier_name = db.Column(db.String(50), nullable=False, index=True)

    total_shipments = db.Column(db.Integer, default=0)
    delivered_count = db.Column(db.Integer, default=0)
    delayed_count = db.Column(db.Integer, default=0)
    issue_count = db.Column(db.Integer, default=0)

    avg_delivery_days = db.Column(db.Float, default=0.0)
    on_time_rate = db.Column(db.Float, default=0.0)

    rating = db.Column(db.Float, default=0.0)
    review_count = db.Column(db.Integer, default=0)

    last_updated = db.Column(db.DateTime, default=datetime.utcnow, onupdate=datetime.utcnow)

    def __repr__(self):
        return f'<ShippingCarrierPerformance {self.carrier_name}>'

    def to_dict(self):
        return {
            'id': self.id,
            'carrier_name': self.carrier_name,
            'total_shipments': self.total_shipments,
            'delivered_count': self.delivered_count,
            'delayed_count': self.delayed_count,
            'issue_count': self.issue_count,
            'avg_delivery_days': self.avg_delivery_days,
            'on_time_rate': self.on_time_rate,
            'rating': self.rating,
            'review_count': self.review_count,
            'last_updated': self.last_updated.isoformat() if self.last_updated else None,
        }


# =============================================================================
# CHAT — Persistent chat history & customer memory
# -----------------------------------------------------------------------------
# 2026 Pro addition (digabung dari models_chat.py): sebelumnya riwayat chat
# customer hanya hidup di in-memory TTLCache (hilang tiap restart / 30 menit).
# Tiga model di bawah menyimpan riwayat percakapan dan "memori" fakta
# pelanggan (nama, preferensi, dsb.) secara permanen di database yang sama.
# =============================================================================
class ChatConversation(db.Model):
    __tablename__ = 'chat_conversations'

    id = db.Column(db.Integer, primary_key=True)
    session_id = db.Column(db.String(64), unique=True, nullable=False, index=True)
    # Diisi otomatis begitu pelanggan teridentifikasi (mis. sebutkan no HP
    # saat cek order/KYC/cicilan) — dipakai untuk menyambungkan memori
    # jangka panjang lintas sesi/device.
    customer_key = db.Column(db.String(64), index=True, nullable=True)
    user_id = db.Column(db.Integer, nullable=True, index=True)  # User.id jika login
    title = db.Column(db.String(160), nullable=True)
    is_archived = db.Column(db.Boolean, default=False, nullable=False, index=True)
    created_at = db.Column(db.DateTime, default=datetime.utcnow, nullable=False)
    updated_at = db.Column(db.DateTime, default=datetime.utcnow, onupdate=datetime.utcnow, nullable=False)
    last_message_at = db.Column(db.DateTime, nullable=True)

    messages = db.relationship(
        'ChatMessage',
        backref='conversation',
        lazy='dynamic',
        cascade='all, delete-orphan',
        order_by='ChatMessage.id',
    )

    def to_dict(self, with_messages: bool = False):
        data = {
            'id': self.id,
            'session_id': self.session_id,
            'customer_key': self.customer_key,
            'title': self.title,
            'is_archived': self.is_archived,
            'created_at': self.created_at.isoformat() if self.created_at else None,
            'last_message_at': self.last_message_at.isoformat() if self.last_message_at else None,
        }
        if with_messages:
            data['messages'] = [m.to_dict() for m in self.messages]
        return data


class ChatMessage(db.Model):
    __tablename__ = 'chat_messages'

    id = db.Column(db.Integer, primary_key=True)
    conversation_id = db.Column(
        db.Integer, db.ForeignKey('chat_conversations.id'), nullable=False, index=True
    )
    role = db.Column(db.String(20), nullable=False)  # 'user' | 'assistant' | 'system'
    content = db.Column(db.Text, nullable=False)
    model = db.Column(db.String(80), nullable=True)
    cost_usd = db.Column(db.Float, default=0.0)
    tool_calls_json = db.Column(db.Text, nullable=True)
    source = db.Column(db.String(20), default='ai')  # ai | fallback | local | rate_limited
    created_at = db.Column(db.DateTime, default=datetime.utcnow, nullable=False, index=True)

    def to_dict(self):
        tool_calls = None
        if self.tool_calls_json:
            try:
                tool_calls = json.loads(self.tool_calls_json)
            except Exception:
                tool_calls = None
        return {
            'id': self.id,
            'role': self.role,
            'content': self.content,
            'model': self.model,
            'cost_usd': self.cost_usd,
            'tool_calls': tool_calls,
            'source': self.source,
            'created_at': self.created_at.isoformat() if self.created_at else None,
        }


class CustomerMemoryFact(db.Model):
    """Fakta jangka panjang yang diingat AI tentang seorang pelanggan,
    lintas sesi/percakapan (mis. nama panggilan, minat produk, budget)."""

    __tablename__ = 'customer_memory_facts'

    id = db.Column(db.Integer, primary_key=True)
    # 'ph:<nomor_hp_bersih>' setelah identitas diketahui, atau
    # 'sid:<session_id>' sementara sebelum itu.
    customer_key = db.Column(db.String(64), nullable=False, index=True)
    fact_key = db.Column(db.String(80), nullable=False)     # mis. 'nama', 'minat_produk', 'budget'
    fact_value = db.Column(db.Text, nullable=False)
    fact_type = db.Column(db.String(30), default='catatan')  # identitas | preferensi | catatan
    source_session = db.Column(db.String(64), nullable=True)
    created_at = db.Column(db.DateTime, default=datetime.utcnow, nullable=False)
    updated_at = db.Column(db.DateTime, default=datetime.utcnow, onupdate=datetime.utcnow, nullable=False)

    __table_args__ = (
        db.UniqueConstraint('customer_key', 'fact_key', name='uq_customer_memory_key'),
    )

    def to_dict(self):
        return {
            'fact_key': self.fact_key,
            'fact_value': self.fact_value,
            'fact_type': self.fact_type,
            'updated_at': self.updated_at.isoformat() if self.updated_at else None,
        }


# =============================================================================
# VISITOR LOG — Statistik Pengunjung Website
# -----------------------------------------------------------------------------
# Mencatat setiap kunjungan halaman publik (GET, bukan /admin, /api, /static)
# untuk keperluan statistik: total kunjungan, pengunjung unik (per
# session_key), halaman terpopuler, breakdown device/browser/OS, dan sumber
# referrer. Bot/crawler tetap dicatat (is_bot=True) tapi dikecualikan dari
# angka statistik utama supaya tidak menggelembungkan data.
# =============================================================================
class VisitorLog(db.Model):
    __tablename__ = 'visitor_logs'

    id = db.Column(db.Integer, primary_key=True)
    ip_address = db.Column(db.String(45))
    user_agent = db.Column(db.String(500))
    path = db.Column(db.String(500), nullable=False)
    referrer = db.Column(db.String(500))
    method = db.Column(db.String(10))
    session_key = db.Column(db.String(64), index=True)
    device_type = db.Column(db.String(20))   # desktop | mobile | tablet
    browser = db.Column(db.String(50))
    os = db.Column(db.String(50))
    is_bot = db.Column(db.Boolean, default=False, index=True)
    visited_at = db.Column(db.DateTime, default=datetime.utcnow, index=True)

    def __repr__(self):
        return f'<VisitorLog {self.path} @ {self.visited_at}>'
