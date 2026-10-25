"""
Shipping Tracking Models
Model database untuk tracking pengiriman
"""

from datetime import datetime
from models import db


class ShippingTracking(db.Model):
    """Model untuk tracking pengiriman"""
    __tablename__ = 'shipping_tracking'

    id = db.Column(db.Integer, primary_key=True)

    # Relasi
    order_id = db.Column(db.Integer, db.ForeignKey('orders.id'), nullable=False)

    # Tracking Info
    tracking_number = db.Column(db.String(100), unique=True, nullable=False, index=True)
    carrier = db.Column(db.String(50), nullable=False)  # JNE, TIKI, POS, GRAB, GOJEK
    carrier_reference = db.Column(db.String(100))  # Reference dari kurir
    service_type = db.Column(db.String(50))  # REG, YES, EXPRESS, etc

    # Status
    status = db.Column(db.String(50), default='Pending_Pickup')  # Current status
    last_status_update = db.Column(db.DateTime)

    # Location
    current_location = db.Column(db.String(200))  # Lokasi terakhir
    origin_location = db.Column(db.String(200))
    destination_location = db.Column(db.String(200))

    # Coordinates (optional)
    latitude = db.Column(db.Float)
    longitude = db.Column(db.Float)

    # Estimated Delivery
    estimated_delivery = db.Column(db.DateTime)
    actual_delivery = db.Column(db.DateTime)

    # Weight & Dimensions
    weight = db.Column(db.Float)
    length = db.Column(db.Float)
    width = db.Column(db.Float)
    height = db.Column(db.Float)

    # Cost
    shipping_cost = db.Column(db.Integer)
    insurance_cost = db.Column(db.Integer, default=0)
    additional_fees = db.Column(db.Integer, default=0)
    total_cost = db.Column(db.Integer)

    # Recipient Info
    recipient_name = db.Column(db.String(100))
    recipient_phone = db.Column(db.String(20))
    recipient_address = db.Column(db.Text)

    # Issues
    has_issue = db.Column(db.Boolean, default=False)
    issue_type = db.Column(db.String(50))  # damaged, lost, address_issue, etc
    issue_description = db.Column(db.Text)
    issue_reported_date = db.Column(db.DateTime)

    # Delivery Attempts
    delivery_attempts = db.Column(db.Integer, default=0)
    last_delivery_attempt = db.Column(db.DateTime)
    failed_delivery_reason = db.Column(db.Text)

    # Return
    is_return = db.Column(db.Boolean, default=False)
    return_status = db.Column(db.String(50))  # Return in Transit, Return Delivered
    return_reason = db.Column(db.Text)

    # Proof
    delivery_proof_image = db.Column(db.String(200))
    delivery_signature = db.Column(db.String(200))
    delivery_notes = db.Column(db.Text)

    # SLA Tracking
    sla_deadline = db.Column(db.DateTime)
    is_delayed = db.Column(db.Boolean, default=False)
    delay_days = db.Column(db.Integer, default=0)

    # Meta
    notes = db.Column(db.Text)
    tags = db.Column(db.JSON)

    # Timestamps
    created_at = db.Column(db.DateTime, default=datetime.utcnow, index=True)
    updated_at = db.Column(db.DateTime, default=datetime.utcnow, onupdate=datetime.utcnow)

    # Relationships
    order = db.relationship('Order', backref='shipments')
    tracking_events = db.relationship('ShippingTrackingEvent', backref='tracking', cascade='all, delete-orphan')

    def __repr__(self):
        return f'<ShippingTracking {self.tracking_number}>'

    def to_dict(self):
        return {
            'id': self.id,
            'order_id': self.order_id,
            'tracking_number': self.tracking_number,
            'carrier': self.carrier,
            'status': self.status,
            'current_location': self.current_location,
            'estimated_delivery': self.estimated_delivery.isoformat() if self.estimated_delivery else None,
            'actual_delivery': self.actual_delivery.isoformat() if self.actual_delivery else None,
            'recipient_name': self.recipient_name,
            'recipient_phone': self.recipient_phone,
            'is_delayed': self.is_delayed,
            'delay_days': self.delay_days,
            'has_issue': self.has_issue,
            'issue_type': self.issue_type,
            'created_at': self.created_at.isoformat() if self.created_at else None,
            'updated_at': self.updated_at.isoformat() if self.updated_at else None
        }


class ShippingTrackingEvent(db.Model):
    """Model untuk event tracking pengiriman (history)"""
    __tablename__ = 'shipping_tracking_event'

    id = db.Column(db.Integer, primary_key=True)

    # Relasi
    shipping_tracking_id = db.Column(db.Integer, db.ForeignKey('shipping_tracking.id'), nullable=False)

    # Event Info
    status = db.Column(db.String(50), nullable=False)
    status_description = db.Column(db.Text)
    location = db.Column(db.String(200))
    latitude = db.Column(db.Float)
    longitude = db.Column(db.Float)

    # Event Details
    event_type = db.Column(db.String(50))  # status_update, issue, delivery_attempt, etc
    event_data = db.Column(db.JSON)  # Additional data sebagai JSON

    # Notification
    customer_notified = db.Column(db.Boolean, default=False)
    notification_method = db.Column(db.String(50))  # email, sms, whatsapp
    notification_sent_at = db.Column(db.DateTime)

    # Admin
    admin_notified = db.Column(db.Boolean, default=False)
    admin_notification_sent_at = db.Column(db.DateTime)

    # Timestamps
    event_time = db.Column(db.DateTime, nullable=False)  # Waktu event terjadi
    received_at = db.Column(db.DateTime, default=datetime.utcnow)  # Waktu kami terima info
    created_at = db.Column(db.DateTime, default=datetime.utcnow, index=True)

    def __repr__(self):
        return f'<ShippingTrackingEvent {self.status}>'

    def to_dict(self):
        return {
            'id': self.id,
            'status': self.status,
            'status_description': self.status_description,
            'location': self.location,
            'event_type': self.event_type,
            'event_time': self.event_time.isoformat() if self.event_time else None,
            'customer_notified': self.customer_notified,
            'created_at': self.created_at.isoformat() if self.created_at else None
        }


class ShippingIssue(db.Model):
    """Model untuk tracking masalah pengiriman"""
    __tablename__ = 'shipping_issue'

    id = db.Column(db.Integer, primary_key=True)

    # Relasi
    shipping_tracking_id = db.Column(db.Integer, db.ForeignKey('shipping_tracking.id'), nullable=False)
    order_id = db.Column(db.Integer, db.ForeignKey('orders.id'), nullable=False)

    # Issue Info
    issue_type = db.Column(db.String(50), nullable=False)  # damaged, lost, wrong_address, etc
    issue_title = db.Column(db.String(200), nullable=False)
    issue_description = db.Column(db.Text)

    # Severity & Priority
    severity = db.Column(db.String(20))  # low, medium, high, critical
    priority = db.Column(db.String(20))  # low, medium, high, urgent

    # Status
    issue_status = db.Column(db.String(50), default='Reported')  # Reported, In Progress, Resolved
    resolution_type = db.Column(db.String(50))  # Refund, Replacement, Return
    resolution_notes = db.Column(db.Text)

    # Claim
    claim_created = db.Column(db.Boolean, default=False)
    claim_number = db.Column(db.String(50))
    claim_amount = db.Column(db.Integer)
    claim_status = db.Column(db.String(50))

    # Evidence
    issue_images = db.Column(db.JSON)  # Array of image filenames
    customer_notes = db.Column(db.Text)
    admin_notes = db.Column(db.Text)

    # Dates
    reported_date = db.Column(db.DateTime, default=datetime.utcnow)
    resolved_date = db.Column(db.DateTime)
    resolution_deadline = db.Column(db.DateTime)

    # Timestamps
    created_at = db.Column(db.DateTime, default=datetime.utcnow, index=True)
    updated_at = db.Column(db.DateTime, default=datetime.utcnow, onupdate=datetime.utcnow)

    # Relationships
    shipping = db.relationship('ShippingTracking', backref='issues')
    order = db.relationship('Order', backref='shipping_issues')

    def __repr__(self):
        return f'<ShippingIssue {self.issue_type}>'

    def to_dict(self):
        return {
            'id': self.id,
            'issue_type': self.issue_type,
            'issue_title': self.issue_title,
            'issue_status': self.issue_status,
            'severity': self.severity,
            'priority': self.priority,
            'claim_status': self.claim_status,
            'reported_date': self.reported_date.isoformat() if self.reported_date else None,
            'resolved_date': self.resolved_date.isoformat() if self.resolved_date else None
        }


class ShippingCarrierPerformance(db.Model):
    """Model untuk tracking performa carrier"""
    __tablename__ = 'shipping_carrier_performance'

    id = db.Column(db.Integer, primary_key=True)

    # Carrier Info
    carrier_name = db.Column(db.String(50), nullable=False, unique=True, index=True)
    period_date = db.Column(db.Date, nullable=False)  # Tanggal periode

    # Metrics
    total_shipments = db.Column(db.Integer, default=0)
    on_time_deliveries = db.Column(db.Integer, default=0)
    delayed_deliveries = db.Column(db.Integer, default=0)
    failed_deliveries = db.Column(db.Integer, default=0)
    on_time_delivery_rate = db.Column(db.Float)  # Percentage

    # Average Times
    average_delivery_time_days = db.Column(db.Float)
    average_delivery_time_hours = db.Column(db.Float)

    # Issues
    damaged_packages = db.Column(db.Integer, default=0)
    lost_packages = db.Column(db.Integer, default=0)
    address_issues = db.Column(db.Integer, default=0)
    issue_rate = db.Column(db.Float)  # Percentage

    # Cost
    total_cost = db.Column(db.Integer)
    average_cost_per_shipment = db.Column(db.Float)

    # Rating
    customer_rating = db.Column(db.Float)  # 1-5
    reliability_score = db.Column(db.Float)  # 0-100

    # Status
    status = db.Column(db.String(20), default='Active')  # Active, Inactive, Review
    notes = db.Column(db.Text)

    # Timestamps
    created_at = db.Column(db.DateTime, default=datetime.utcnow)
    updated_at = db.Column(db.DateTime, default=datetime.utcnow, onupdate=datetime.utcnow)

    def __repr__(self):
        return f'<ShippingCarrierPerformance {self.carrier_name} {self.period_date}>'

    def to_dict(self):
        return {
            'id': self.id,
            'carrier_name': self.carrier_name,
            'total_shipments': self.total_shipments,
            'on_time_delivery_rate': self.on_time_delivery_rate,
            'average_delivery_time_days': self.average_delivery_time_days,
            'issue_rate': self.issue_rate,
            'customer_rating': self.customer_rating,
            'reliability_score': self.reliability_score
        }


# ============================================================================
# QUERY HELPERS
# ============================================================================

class ShippingTrackingQuery:
    """Helper class untuk query shipping tracking"""

    @staticmethod
    def get_by_tracking_number(tracking_number):
        """Dapatkan tracking berdasarkan nomor tracking"""
        return ShippingTracking.query.filter_by(tracking_number=tracking_number).first()

    @staticmethod
    def get_by_order_id(order_id):
        """Dapatkan semua tracking untuk order tertentu"""
        return ShippingTracking.query.filter_by(order_id=order_id).all()

    @staticmethod
    def get_delayed_shipments(days=1):
        """Dapatkan pengiriman yang terlambat"""
        from datetime import timedelta
        threshold_date = datetime.utcnow() - timedelta(days=days)
        return ShippingTracking.query.filter(
            ShippingTracking.is_delayed == True,
            ShippingTracking.actual_delivery.is_(None),
            ShippingTracking.estimated_delivery < threshold_date
        ).all()

    @staticmethod
    def get_failed_deliveries():
        """Dapatkan pengiriman yang gagal"""
        return ShippingTracking.query.filter(
            ShippingTracking.status == 'Failed_Delivery'
        ).all()

    @staticmethod
    def get_shipments_by_status(status):
        """Dapatkan pengiriman berdasarkan status"""
        return ShippingTracking.query.filter_by(status=status).all()

    @staticmethod
    def get_shipments_by_carrier(carrier):
        """Dapatkan pengiriman berdasarkan carrier"""
        return ShippingTracking.query.filter_by(carrier=carrier).all()

    @staticmethod
    def get_shipments_with_issues():
        """Dapatkan pengiriman yang memiliki masalah"""
        return ShippingTracking.query.filter_by(has_issue=True).all()

    @staticmethod
    def get_tracking_history(tracking_id):
        """Dapatkan history tracking"""
        return ShippingTrackingEvent.query.filter_by(
            shipping_tracking_id=tracking_id
        ).order_by(ShippingTrackingEvent.event_time.asc()).all()

    @staticmethod
    def search_shipments(search_term):
        """Cari pengiriman"""
        from sqlalchemy import or_
        return ShippingTracking.query.filter(
            or_(
                ShippingTracking.tracking_number.ilike(f'%{search_term}%'),
                ShippingTracking.recipient_name.ilike(f'%{search_term}%'),
                ShippingTracking.recipient_phone.ilike(f'%{search_term}%')
            )
        ).all()

    @staticmethod
    def get_on_time_delivery_rate(carrier, days=30):
        """Dapatkan on-time delivery rate untuk carrier"""
        from datetime import timedelta
        start_date = datetime.utcnow() - timedelta(days=days)

        total = ShippingTracking.query.filter(
            ShippingTracking.carrier == carrier,
            ShippingTracking.actual_delivery >= start_date
        ).count()

        on_time = ShippingTracking.query.filter(
            ShippingTracking.carrier == carrier,
            ShippingTracking.actual_delivery >= start_date,
            ShippingTracking.actual_delivery <= ShippingTracking.estimated_delivery
        ).count()

        if total == 0:
            return 0
        return (on_time / total) * 100
