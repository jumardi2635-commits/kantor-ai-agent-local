#!/usr/bin/env python3
"""
visitor_stats.py — Modul Statistik Pengunjung Website (plug-in)
=================================================================

Modul mandiri (self-contained) yang menambahkan fitur pelacakan &
statistik pengunjung ke aplikasi Flask yang sudah ada, TANPA perlu
mengedit ulang seluruh app.py yang besar.

CARA INTEGRASI (hanya 2 baris di app.py Anda):

    1. Tambahkan import ini di dekat import model lain di app.py:

        from visitor_stats import register_visitor_stats

    2. Panggil SETELAH `db.init_app(app)` (dan setelah blok
       `with app.app_context(): db.create_all()` supaya tabelnya ikut
       dibuat), contoh persis di app.py:

        db.init_app(app)
        ...
        with app.app_context():
            db.create_all()
            sync_missing_columns()
            ...

        register_visitor_stats(app)   # ← tambahkan baris ini

Modul ini akan otomatis:
    - Mendaftarkan model VisitorLog ke SQLAlchemy metadata (tabelnya akan
      ikut dibuat oleh db.create_all() yang sudah ada di app.py Anda,
      asalkan register_visitor_stats() dipanggil SEBELUM db.create_all(),
      atau modul ini di-import sebelum app_context tersebut berjalan).
    - Mendaftarkan before_request hook untuk mencatat setiap kunjungan
      halaman publik (GET, bukan /admin, /api, /static).
    - Mendaftarkan route admin:
        GET /admin/visitor-stats            (dashboard statistik + chart)
        GET /admin/visitor-stats/export      (export CSV log mentah)

Route admin dilindungi login_required + cek current_user adalah Admin,
sama seperti pola admin_required() di app.py utama — didefinisikan
ulang secara independen di sini supaya modul ini tidak perlu meng-import
apa pun dari app.py (menghindari circular import).
"""

import csv
import logging
from datetime import datetime, timedelta, date
from functools import wraps
from io import StringIO

from flask import (
    Blueprint, render_template, request, session, jsonify,
    make_response, redirect, url_for, flash,
)
from flask_login import login_required, current_user
from sqlalchemy import func, desc

from models import db, Admin, VisitorLog

logger = logging.getLogger(__name__)

visitor_stats_bp = Blueprint('visitor_stats', __name__)


# =============================================================================
# KONFIGURASI TRACKING
# =============================================================================
_VISITOR_TRACK_EXCLUDE_PREFIXES = ('/static/', '/admin', '/api/')
_VISITOR_TRACK_EXCLUDE_PATHS = {'/favicon.ico', '/robots.txt'}

_BOT_UA_PATTERNS = (
    'bot', 'spider', 'crawl', 'slurp', 'facebookexternalhit', 'whatsapp',
    'telegrambot', 'discordbot', 'curl', 'wget', 'python-requests', 'scrapy',
    'headless', 'ahrefsbot', 'semrushbot', 'mj12bot', 'dotbot', 'petalbot',
)


def _is_bot_user_agent(ua: str) -> bool:
    """Deteksi sederhana berbasis daftar kata kunci umum bot/crawler pada
    User-Agent. Tidak 100% akurat, tapi cukup untuk mengeluarkan crawler
    mesin pencari & tool otomatis dari statistik utama."""
    if not ua:
        return True
    ua_l = ua.lower()
    return any(p in ua_l for p in _BOT_UA_PATTERNS)


def _detect_device_type(ua: str) -> str:
    if not ua:
        return 'unknown'
    ua_l = ua.lower()
    if 'ipad' in ua_l or 'tablet' in ua_l:
        return 'tablet'
    if 'mobi' in ua_l or 'android' in ua_l or 'iphone' in ua_l:
        return 'mobile'
    return 'desktop'


def _detect_browser(ua: str) -> str:
    if not ua:
        return 'Unknown'
    ua_l = ua.lower()
    if 'edg/' in ua_l:
        return 'Edge'
    if 'opr/' in ua_l or 'opera' in ua_l:
        return 'Opera'
    if 'chrome' in ua_l and 'chromium' not in ua_l:
        return 'Chrome'
    if 'firefox' in ua_l:
        return 'Firefox'
    if 'safari' in ua_l and 'chrome' not in ua_l:
        return 'Safari'
    return 'Lainnya'


def _detect_os(ua: str) -> str:
    if not ua:
        return 'Unknown'
    ua_l = ua.lower()
    if 'windows' in ua_l:
        return 'Windows'
    if 'android' in ua_l:
        return 'Android'
    if 'iphone' in ua_l or 'ipad' in ua_l or 'ios' in ua_l:
        return 'iOS'
    if 'mac os' in ua_l or 'macintosh' in ua_l:
        return 'macOS'
    if 'linux' in ua_l:
        return 'Linux'
    return 'Lainnya'


def _track_visitor():
    """before_request hook: catat kunjungan halaman publik. Dibungkus
    try/except menyeluruh supaya kegagalan logging (mis. DB bermasalah)
    tidak pernah menjatuhkan request utama pengguna."""
    try:
        path = request.path or '/'
        if (request.method != 'GET' or
                path in _VISITOR_TRACK_EXCLUDE_PATHS or
                any(path.startswith(p) for p in _VISITOR_TRACK_EXCLUDE_PREFIXES) or
                request.endpoint is None):
            return

        if 'visitor_uid' not in session:
            import secrets
            session['visitor_uid'] = secrets.token_hex(16)

        ua_string = request.headers.get('User-Agent', '')
        log = VisitorLog(
            ip_address=request.remote_addr,
            user_agent=ua_string[:500],
            path=path[:500],
            referrer=(request.referrer or '')[:500] or None,
            method=request.method,
            session_key=session['visitor_uid'],
            device_type=_detect_device_type(ua_string),
            browser=_detect_browser(ua_string),
            os=_detect_os(ua_string),
            is_bot=_is_bot_user_agent(ua_string),
        )
        db.session.add(log)
        db.session.commit()
    except Exception as e:
        db.session.rollback()
        logger.error(f"Visitor tracking error: {e}")


# =============================================================================
# STATISTIK
# =============================================================================
def get_visitor_stats(days: int = 30) -> dict:
    """Kumpulkan statistik pengunjung. "Unik" dihitung per session_key
    (cookie sesi Flask). Bot/crawler dikeluarkan dari semua angka utama,
    kecuali bot_count sendiri."""
    since = datetime.utcnow() - timedelta(days=days)
    today = date.today()

    base_filter = (VisitorLog.visited_at >= since, VisitorLog.is_bot == False)

    total_visits = VisitorLog.query.filter(*base_filter).count()

    unique_visitors = db.session.query(func.count(func.distinct(VisitorLog.session_key))) \
        .filter(*base_filter).scalar() or 0

    today_visits = VisitorLog.query.filter(
        func.date(VisitorLog.visited_at) == today, VisitorLog.is_bot == False
    ).count()

    today_unique = db.session.query(func.count(func.distinct(VisitorLog.session_key))) \
        .filter(func.date(VisitorLog.visited_at) == today, VisitorLog.is_bot == False).scalar() or 0

    yesterday_visits = VisitorLog.query.filter(
        func.date(VisitorLog.visited_at) == (today - timedelta(days=1)), VisitorLog.is_bot == False
    ).count()

    daily_chart = []
    for i in range(days - 1, -1, -1):
        day = today - timedelta(days=i)
        day_visits = VisitorLog.query.filter(
            func.date(VisitorLog.visited_at) == day, VisitorLog.is_bot == False
        ).count()
        day_unique = db.session.query(func.count(func.distinct(VisitorLog.session_key))) \
            .filter(func.date(VisitorLog.visited_at) == day, VisitorLog.is_bot == False).scalar() or 0
        daily_chart.append({
            'label': day.strftime('%d %b'),
            'date': day.isoformat(),
            'visits': day_visits,
            'unique': day_unique,
        })

    max_daily = max([d['visits'] for d in daily_chart] + [1])
    for d in daily_chart:
        d['percent'] = round((d['visits'] / max_daily) * 100) if max_daily > 0 else 0

    top_pages = [
        {'path': p, 'count': c} for p, c in
        db.session.query(VisitorLog.path, func.count(VisitorLog.id).label('cnt'))
        .filter(*base_filter)
        .group_by(VisitorLog.path).order_by(desc('cnt')).limit(10).all()
    ]

    device_breakdown = [
        {'label': d or 'unknown', 'count': c} for d, c in
        db.session.query(VisitorLog.device_type, func.count(VisitorLog.id).label('cnt'))
        .filter(*base_filter)
        .group_by(VisitorLog.device_type).order_by(desc('cnt')).all()
    ]

    browser_breakdown = [
        {'label': b or 'Unknown', 'count': c} for b, c in
        db.session.query(VisitorLog.browser, func.count(VisitorLog.id).label('cnt'))
        .filter(*base_filter)
        .group_by(VisitorLog.browser).order_by(desc('cnt')).limit(6).all()
    ]

    os_breakdown = [
        {'label': o or 'Unknown', 'count': c} for o, c in
        db.session.query(VisitorLog.os, func.count(VisitorLog.id).label('cnt'))
        .filter(*base_filter)
        .group_by(VisitorLog.os).order_by(desc('cnt')).limit(6).all()
    ]

    top_referrers = [
        {'referrer': r, 'count': c} for r, c in
        db.session.query(VisitorLog.referrer, func.count(VisitorLog.id).label('cnt'))
        .filter(*base_filter, VisitorLog.referrer.isnot(None))
        .group_by(VisitorLog.referrer).order_by(desc('cnt')).limit(10).all()
    ]

    bot_count = VisitorLog.query.filter(
        VisitorLog.visited_at >= since, VisitorLog.is_bot == True
    ).count()

    return {
        'days': days,
        'total_visits': total_visits,
        'unique_visitors': unique_visitors,
        'today_visits': today_visits,
        'today_unique': today_unique,
        'yesterday_visits': yesterday_visits,
        'daily_chart': daily_chart,
        'top_pages': top_pages,
        'device_breakdown': device_breakdown,
        'browser_breakdown': browser_breakdown,
        'os_breakdown': os_breakdown,
        'top_referrers': top_referrers,
        'bot_count': bot_count,
        'avg_daily_visits': round(total_visits / days) if days else 0,
    }


# =============================================================================
# ADMIN GUARD (independen dari app.py, untuk menghindari circular import)
# =============================================================================
def _admin_only(func):
    @wraps(func)
    def wrapper(*args, **kwargs):
        if not isinstance(current_user, Admin):
            flash('Akses ditolak.', 'error')
            return redirect(url_for('index'))
        return func(*args, **kwargs)
    return login_required(wrapper)


# =============================================================================
# ROUTES
# =============================================================================
@visitor_stats_bp.route('/admin/visitor-stats')
@_admin_only
def admin_visitor_stats():
    days = request.args.get('days', 30, type=int)
    days = max(1, min(days, 90))
    stats = get_visitor_stats(days=days)
    return render_template('admin/visitor_stats.html', stats=stats)


@visitor_stats_bp.route('/admin/visitor-stats/export')
@_admin_only
def admin_visitor_stats_export():
    days = request.args.get('days', 30, type=int)
    days = max(1, min(days, 90))
    since = datetime.utcnow() - timedelta(days=days)

    logs = VisitorLog.query.filter(VisitorLog.visited_at >= since) \
        .order_by(VisitorLog.visited_at.desc()).all()

    output = StringIO()
    writer = csv.writer(output)
    writer.writerow(['Waktu', 'IP', 'Path', 'Referrer', 'Device', 'Browser', 'OS', 'Bot'])
    for log in logs:
        writer.writerow([
            log.visited_at.strftime('%Y-%m-%d %H:%M:%S') if log.visited_at else '',
            log.ip_address or '',
            log.path or '',
            log.referrer or '',
            log.device_type or '',
            log.browser or '',
            log.os or '',
            'Ya' if log.is_bot else 'Tidak',
        ])
    response = make_response(output.getvalue())
    response.headers['Content-Disposition'] = 'attachment; filename=visitor_logs.csv'
    response.headers['Content-type'] = 'text/csv'
    return response


# =============================================================================
# REGISTRASI KE APLIKASI UTAMA
# =============================================================================
def register_visitor_stats(app):
    """Panggil fungsi ini sekali di app.py, SETELAH db.init_app(app).
    Mendaftarkan before_request hook tracking + blueprint route admin."""
    app.before_request(_track_visitor)
    app.register_blueprint(visitor_stats_bp)
    logger.info("Visitor stats module registered: GET /admin/visitor-stats")
