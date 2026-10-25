#!/usr/bin/env python3
"""
╔══════════════════════════════════════════════════════════════════════════════╗
║  MIGRASI DATABASE — 2026 PROFESSIONAL EDITION v3.7.0                        ║
║  Handphone Second Marketplace — OpenRouter AI + Enhanced Shipping Tracking   ║
║                                                                               ║
║  FITUR PRO:                                                                 ║
║  • Circuit Breaker — Auto-pause on repeated failures, auto-recovery         ║
║  • Retry Logic — Exponential backoff dengan jitter untuk DB ops             ║
║  • Rate Limiting — Throttle migration runs (min interval, max retries)      ║
║  • Cost Tracking — Track waktu, resource usage, per-step metrics            ║
║  • Structured Logging — JSON + console output, severity levels                ║
║  • Fallback Chains — SQLite ↔ Postgres failover, backup restore               ║
║  • Health Checks — Pre/post migration integrity validation                  ║
║  • Dry Run Mode — Preview semua perubahan tanpa menulis ke DB               ║
║  • Migration Lock — File-based lock prevents concurrent execution             ║
║  • Parallel Processing — ThreadPoolExecutor untuk tabel independen          ║
║  • Transaction Savepoints — Rollback granular per step                        ║
║  • Schema Diff — Tampilkan perubahan sebelum apply                          ║
║  • Signal Handling — Graceful shutdown pada SIGINT/SIGTERM                    ║
║  • Batch Processing — Chunked ops untuk tabel besar                         ║
║  • Config Validation — Validasi environment sebelum start                   ║
║                                                                               ║
║  STATUS FLOW (Order): 8 stages                                              ║
║    Pending → Paid → Processing → Ready_to_Ship → Shipped → Out_for_Delivery ║
║    → Received → Completed                                                   ║
║                                                                               ║
║  STATUS FLOW (Shipping): 8 stages                                           ║
║    Pending_Pickup → Picked_Up → In_Transit → Out_for_Delivery → Delivered   ║
║    → Received → Completed → Failed_Delivery                                 ║
║                                                                               ║
║  WAREHOUSE ADDRESS: Jl. Siliwangi, Kahuripan, Kec. Tawang,                  ║
║    Tasikmalaya, Jawa Barat 46115                                            ║
╚══════════════════════════════════════════════════════════════════════════════╝
"""

import sqlite3
import re
import os
import sys
import time
import shutil
import hashlib
import json
import random
import signal
import threading
import atexit
import tempfile
from datetime import datetime, timedelta
from pathlib import Path
from concurrent.futures import ThreadPoolExecutor, as_completed
from functools import wraps
from dataclasses import dataclass, field, asdict
from typing import Optional, List, Dict, Any, Callable, Tuple
from contextlib import contextmanager
from werkzeug.security import generate_password_hash

# ═══════════════════════════════════════════════════════════════════════════════
# KONFIGURASI & ENVIRONMENT
# ═══════════════════════════════════════════════════════════════════════════════

DB_URL = os.environ.get('DATABASE_URL')
DB_PATH = os.path.join('instance', 'database.db')
DB_TYPE = 'postgres' if DB_URL and DB_URL.startswith(('postgres://', 'postgresql://')) else 'sqlite'
BACKUP_DIR = os.path.join('instance', 'backups')
MIGRATION_VERSION = '3.9.0'
LOCK_FILE = os.path.join('instance', '.migration.lock')
METRICS_FILE = os.path.join('instance', '.migration_metrics.json')
LOG_FILE = os.path.join('instance', 'migration.log')

psycopg2 = None
if DB_TYPE == 'postgres':
    try:
        import psycopg2
        import psycopg2.extras  # noqa: F401 (dipakai secara implisit oleh psycopg2 untuk cursor factories)
    except ImportError:
        print("[FATAL] DATABASE_URL mengarah ke Postgres tapi psycopg2 belum ter-install.")
        print("        Install dengan: pip install psycopg2-binary")
        sys.exit(1)

# Pro Edition thresholds
CIRCUIT_BREAKER_THRESHOLD = int(os.environ.get('MIGRATION_CB_THRESHOLD', '5'))
CIRCUIT_BREAKER_TIMEOUT = int(os.environ.get('MIGRATION_CB_TIMEOUT', '300'))  # seconds
RETRY_MAX_ATTEMPTS = int(os.environ.get('MIGRATION_RETRY_MAX', '3'))
RETRY_BASE_DELAY = float(os.environ.get('MIGRATION_RETRY_DELAY', '1.0'))
RATE_LIMIT_MIN_INTERVAL = int(os.environ.get('MIGRATION_RATE_LIMIT', '60'))  # seconds between runs
MIGRATION_DRY_RUN = os.environ.get('MIGRATION_DRY_RUN', '').lower() in ('1', 'true', 'yes')
MIGRATION_VERBOSE = os.environ.get('MIGRATION_VERBOSE', '').lower() in ('1', 'true', 'yes')
MIGRATION_PRESERVE_ORPHANS = os.environ.get('PRESERVE_ORPHANS', '').lower() in ('1', 'true', 'yes')
MIGRATION_PARALLEL = os.environ.get('MIGRATION_PARALLEL', 'true').lower() in ('1', 'true', 'yes')
MIGRATION_MAX_WORKERS = int(os.environ.get('MIGRATION_WORKERS', '4'))

# ═══════════════════════════════════════════════════════════════════════════════
# STRUCTURED LOGGING & METRICS
# ═══════════════════════════════════════════════════════════════════════════════

@dataclass
class MigrationMetrics:
    version: str = MIGRATION_VERSION
    started_at: str = field(default_factory=lambda: datetime.now().isoformat())
    ended_at: Optional[str] = None
    duration_seconds: float = 0.0
    db_type: str = DB_TYPE
    db_path: str = DB_PATH
    steps_total: int = 0
    steps_success: int = 0
    steps_failed: int = 0
    tables_created: int = 0
    columns_added: int = 0
    indexes_created: int = 0
    settings_added: int = 0
    settings_updated: int = 0
    orphans_cleaned: int = 0
    orphans_preserved: int = 0
    backup_path: Optional[str] = None
    checksum_before: Optional[str] = None
    checksum_after: Optional[str] = None
    errors: List[str] = field(default_factory=list)
    warnings: List[str] = field(default_factory=list)
    step_details: List[Dict[str, Any]] = field(default_factory=list)
    circuit_breaker_tripped: bool = False
    dry_run: bool = MIGRATION_DRY_RUN

    def to_dict(self) -> Dict[str, Any]:
        return asdict(self)

    def save(self):
        try:
            with open(METRICS_FILE, 'w', encoding='utf-8') as f:
                json.dump(self.to_dict(), f, indent=2, ensure_ascii=False, default=str)
        except Exception as e:
            print(f"[WARN] Failed to save metrics: {e}")


METRICS = MigrationMetrics()


class StructuredLogger:
    """JSON + console structured logger with severity levels."""

    LEVELS = {'DEBUG': 10, 'INFO': 20, 'WARN': 30, 'ERROR': 40, 'FATAL': 50}

    def __init__(self, log_file: str = LOG_FILE, min_level: str = 'INFO'):
        self.log_file = log_file
        self.min_level = self.LEVELS.get(min_level, 20)
        self._lock = threading.Lock()
        os.makedirs(os.path.dirname(log_file) if os.path.dirname(log_file) else '.', exist_ok=True)

    def _write(self, level: str, message: str, extra: Dict = None):
        if self.LEVELS.get(level, 20) < self.min_level:
            return
        entry = {
            'timestamp': datetime.now().isoformat(),
            'level': level,
            'version': MIGRATION_VERSION,
            'message': message,
            'thread': threading.current_thread().name,
        }
        if extra:
            entry.update(extra)
        with self._lock:
            with open(self.log_file, 'a', encoding='utf-8') as f:
                f.write(json.dumps(entry, ensure_ascii=False) + '\n')

    def debug(self, msg: str, extra: Dict = None):
        self._write('DEBUG', msg, extra)

    def info(self, msg: str, extra: Dict = None):
        self._write('INFO', msg, extra)

    def warn(self, msg: str, extra: Dict = None):
        self._write('WARN', msg, extra)
        METRICS.warnings.append(msg)

    def error(self, msg: str, extra: Dict = None):
        self._write('ERROR', msg, extra)
        METRICS.errors.append(msg)

    def fatal(self, msg: str, extra: Dict = None):
        self._write('FATAL', msg, extra)
        METRICS.errors.append(msg)


LOGGER = StructuredLogger(min_level='DEBUG' if MIGRATION_VERBOSE else 'INFO')


# ═══════════════════════════════════════════════════════════════════════════════
# CIRCUIT BREAKER
# ═══════════════════════════════════════════════════════════════════════════════

class CircuitBreaker:
    """Circuit breaker pattern for DB operations."""

    def __init__(self, threshold: int = CIRCUIT_BREAKER_THRESHOLD, 
                 timeout: int = CIRCUIT_BREAKER_TIMEOUT):
        self.threshold = threshold
        self.timeout = timeout
        self.failure_count = 0
        self.last_failure_time = None
        self.state = 'CLOSED'  # CLOSED, OPEN, HALF_OPEN
        self._lock = threading.Lock()

    def _is_timeout_expired(self) -> bool:
        if self.last_failure_time is None:
            return True
        return (time.time() - self.last_failure_time) > self.timeout

    def can_execute(self) -> bool:
        with self._lock:
            if self.state == 'CLOSED':
                return True
            if self.state == 'OPEN':
                if self._is_timeout_expired():
                    self.state = 'HALF_OPEN'
                    LOGGER.info("Circuit breaker entering HALF_OPEN state")
                    return True
                LOGGER.warn("Circuit breaker OPEN — execution blocked")
                METRICS.circuit_breaker_tripped = True
                return False
            return True  # HALF_OPEN

    def record_success(self):
        with self._lock:
            self.failure_count = 0
            if self.state == 'HALF_OPEN':
                self.state = 'CLOSED'
                LOGGER.info("Circuit breaker CLOSED — recovery successful")

    def record_failure(self):
        with self._lock:
            self.failure_count += 1
            self.last_failure_time = time.time()
            if self.failure_count >= self.threshold:
                self.state = 'OPEN'
                LOGGER.error(f"Circuit breaker OPEN after {self.failure_count} failures")


CB = CircuitBreaker()


# ═══════════════════════════════════════════════════════════════════════════════
# RETRY LOGIC WITH EXPONENTIAL BACKOFF
# ═══════════════════════════════════════════════════════════════════════════════

def retry_with_backoff(max_attempts: int = RETRY_MAX_ATTEMPTS,
                       base_delay: float = RETRY_BASE_DELAY,
                       exceptions: Tuple = (Exception,),
                       on_retry: Callable = None):
    """Decorator: retry with exponential backoff + jitter."""
    def decorator(func):
        @wraps(func)
        def wrapper(*args, **kwargs):
            for attempt in range(1, max_attempts + 1):
                try:
                    if not CB.can_execute():
                        raise RuntimeError("Circuit breaker is OPEN")
                    result = func(*args, **kwargs)
                    CB.record_success()
                    return result
                except exceptions as e:
                    CB.record_failure()
                    if attempt == max_attempts:
                        LOGGER.error(f"Max retries ({max_attempts}) exceeded for {func.__name__}: {e}")
                        raise
                    delay = base_delay * (2 ** (attempt - 1)) + random.uniform(0, 1)
                    LOGGER.warn(f"Retry {attempt}/{max_attempts} for {func.__name__} in {delay:.2f}s: {e}")
                    if on_retry:
                        on_retry(attempt, e, delay)
                    time.sleep(delay)
            return None
        return wrapper
    return decorator


# ═══════════════════════════════════════════════════════════════════════════════
# RATE LIMITING
# ═══════════════════════════════════════════════════════════════════════════════

def check_rate_limit() -> bool:
    """Ensure minimum interval between migration runs."""
    if not os.path.exists(METRICS_FILE):
        return True
    try:
        with open(METRICS_FILE, 'r') as f:
            data = json.load(f)
        last_run = datetime.fromisoformat(data.get('ended_at', '1970-01-01T00:00:00'))
        elapsed = (datetime.now() - last_run).total_seconds()
        if elapsed < RATE_LIMIT_MIN_INTERVAL:
            LOGGER.warn(f"Rate limit: last run {elapsed:.0f}s ago, min interval {RATE_LIMIT_MIN_INTERVAL}s")
            return False
    except Exception:
        pass
    return True


# ═══════════════════════════════════════════════════════════════════════════════
# MIGRATION LOCK
# ═══════════════════════════════════════════════════════════════════════════════

class MigrationLock:
    """File-based lock to prevent concurrent migration runs."""

    def __init__(self, lock_file: str = LOCK_FILE):
        self.lock_file = lock_file
        self._acquired = False

    def acquire(self) -> bool:
        if os.path.exists(self.lock_file):
            try:
                with open(self.lock_file, 'r') as f:
                    pid = f.read().strip()
                if pid and os.path.exists(f"/proc/{pid}"):
                    LOGGER.error(f"Migration already running (PID {pid})")
                    return False
            except Exception:
                pass
            LOGGER.warn("Stale lock file detected, removing")
            os.remove(self.lock_file)

        with open(self.lock_file, 'w') as f:
            f.write(str(os.getpid()))
        self._acquired = True
        atexit.register(self.release)
        return True

    def release(self):
        if self._acquired and os.path.exists(self.lock_file):
            try:
                os.remove(self.lock_file)
            except Exception:
                pass
            self._acquired = False


LOCK = MigrationLock()


# ═══════════════════════════════════════════════════════════════════════════════
# SIGNAL HANDLING
# ═══════════════════════════════════════════════════════════════════════════════

_shutdown_requested = False

def _signal_handler(signum, frame):
    global _shutdown_requested
    _shutdown_requested = True
    LOGGER.warn(f"Signal {signum} received — graceful shutdown initiated")
    LOCK.release()
    sys.exit(130)

signal.signal(signal.SIGINT, _signal_handler)
signal.signal(signal.SIGTERM, _signal_handler)


# ═══════════════════════════════════════════════════════════════════════════════
# TERMINAL COLORS
# ═══════════════════════════════════════════════════════════════════════════════

class Colors:
    HEADER = '\033[95m'
    BLUE = '\033[94m'
    CYAN = '\033[96m'
    GREEN = '\033[92m'
    YELLOW = '\033[93m'
    RED = '\033[91m'
    BOLD = '\033[1m'
    END = '\033[0m'


def print_section(title):
    print()
    print(Colors.CYAN + "─" * 70 + Colors.END)
    print(Colors.BOLD + f"  📦 {title}" + Colors.END)
    print(Colors.CYAN + "─" * 70 + Colors.END)
    LOGGER.info(f"Section: {title}")


def print_success(msg):
    print(Colors.GREEN + f"   ✅ {msg}" + Colors.END)
    LOGGER.info(msg)


def print_info(msg):
    print(Colors.BLUE + f"   ℹ️  {msg}" + Colors.END)
    LOGGER.info(msg)


def print_warn(msg):
    print(Colors.YELLOW + f"   ⚠️  {msg}" + Colors.END)
    LOGGER.warn(msg)


def print_error(msg):
    print(Colors.RED + f"   ❌ {msg}" + Colors.END)
    LOGGER.error(msg)


# ═══════════════════════════════════════════════════════════════════════════════
# DATABASE UTILITIES WITH RETRY & CIRCUIT BREAKER
# ═══════════════════════════════════════════════════════════════════════════════

@retry_with_backoff(max_attempts=RETRY_MAX_ATTEMPTS, base_delay=RETRY_BASE_DELAY)
def table_exists(cursor, table_name):
    if DB_TYPE == 'sqlite':
        cursor.execute(
            "SELECT 1 FROM sqlite_master WHERE type='table' AND name=?",
            (table_name,)
        )
        return cursor.fetchone() is not None
    else:
        cursor.execute(
            "SELECT EXISTS (SELECT 1 FROM information_schema.tables WHERE table_schema='public' AND table_name=%s)",
            (table_name,)
        )
        return cursor.fetchone()[0]


@retry_with_backoff(max_attempts=RETRY_MAX_ATTEMPTS, base_delay=RETRY_BASE_DELAY)
def column_exists(cursor, table_name, column_name):
    if DB_TYPE == 'sqlite':
        cursor.execute(f'PRAGMA table_info("{table_name}")')
        return column_name in [row[1] for row in cursor.fetchall()]
    else:
        cursor.execute(
            "SELECT EXISTS (SELECT 1 FROM information_schema.columns WHERE table_schema='public' AND table_name=%s AND column_name=%s)",
            (table_name, column_name)
        )
        return cursor.fetchone()[0]


@retry_with_backoff(max_attempts=RETRY_MAX_ATTEMPTS, base_delay=RETRY_BASE_DELAY)
def index_exists(cursor, index_name):
    if DB_TYPE == 'sqlite':
        cursor.execute(
            "SELECT 1 FROM sqlite_master WHERE type='index' AND name=?",
            (index_name,)
        )
        return cursor.fetchone() is not None
    else:
        cursor.execute("SELECT 1 FROM pg_indexes WHERE indexname=%s", (index_name,))
        return cursor.fetchone() is not None


def safe_add_column(cursor, table, col_name, col_def):
    """Tambahkan kolom jika belum ada — SQLite-safe dengan retry."""
    clean_name = col_name.strip('"')
    if not column_exists(cursor, table, clean_name):
        safe_def = col_def
        if 'NOT NULL' in safe_def.upper() and 'DEFAULT' not in safe_def.upper():
            safe_def = safe_def.replace('NOT NULL', '').replace('not null', '').strip()
            print_warn(f"Kolom {clean_name}: menghapus NOT NULL karena SQLite limitation")
        if MIGRATION_DRY_RUN:
            print_info(f"[DRY-RUN] Would add column {clean_name} to {table}")
            return True
        try:
            if DB_TYPE == 'sqlite':
                cursor.execute(f'ALTER TABLE "{table}" ADD COLUMN "{clean_name}" {safe_def}')
            else:
                pg_def = safe_def
                pg_def = pg_def.replace('BOOLEAN DEFAULT 1', 'BOOLEAN DEFAULT TRUE')
                pg_def = pg_def.replace('BOOLEAN DEFAULT 0', 'BOOLEAN DEFAULT FALSE')
                pg_def = re.sub(r'INTEGER PRIMARY KEY AUTOINCREMENT', 'SERIAL PRIMARY KEY', pg_def, flags=re.I)
                cursor.execute(f'ALTER TABLE "{table}" ADD COLUMN {clean_name} {pg_def}')
            LOGGER.info(f"Added column {clean_name} to {table}")
            return True
        except Exception as e:
            print_warn(f"Gagal menambah kolom {clean_name} di {table}: {e}")
            LOGGER.warn(f"Column add failed: {table}.{clean_name}: {e}")
    return False


def execute_sql(cursor, sql, params=None):
    """Execute SQL with placeholder adaptation + dry-run guard."""
    if MIGRATION_DRY_RUN:
        return None
    if DB_TYPE == 'sqlite':
        return cursor.execute(sql, params or ())
    else:
        if params:
            sql_pg = sql.replace('?', '%s')
            return cursor.execute(sql_pg, params)
        else:
            return cursor.execute(sql)


def translate_ddl_for_postgres(ddl):
    s = ddl
    s = re.sub(r'INTEGER PRIMARY KEY AUTOINCREMENT', 'SERIAL PRIMARY KEY', s, flags=re.I)
    s = s.replace("BOOLEAN DEFAULT 1", "BOOLEAN DEFAULT TRUE")
    s = s.replace("BOOLEAN DEFAULT 0", "BOOLEAN DEFAULT FALSE")
    s = s.replace('DATETIME DEFAULT CURRENT_TIMESTAMP', 'TIMESTAMP DEFAULT CURRENT_TIMESTAMP')
    return s


def ensure_table_name(cursor, preferred, fallback):
    """Pastikan tabel pakai nama `preferred` yang SESUAI dengan __tablename__
    aktif di models.py saat ini -- kalau tabel lama bernama `fallback` masih
    ada (dari versi app yang lebih tua), tabel itu di-rename ke `preferred`.

    PENTING: `preferred` HARUS persis sama dengan __tablename__ di models.py,
    bukan sekadar bentuk singular. models.py v2026 secara eksplisit
    mendeklarasikan beberapa tabel dalam bentuk PLURAL (mis. Order ->
    'orders', OrderItem -> 'order_items') untuk menghindari 'order' sebagai
    reserved word SQL -- jadi 'preferred' untuk tabel-tabel itu adalah bentuk
    plural, bukan singular."""
    if table_exists(cursor, preferred):
        return preferred
    if table_exists(cursor, fallback):
        print_warn(f"Tabel lama '{fallback}' ditemukan. Rename → '{preferred}' ...")
        if MIGRATION_DRY_RUN:
            print_info(f"[DRY-RUN] Would rename {fallback} → {preferred}")
            return preferred
        try:
            cursor.execute(f'ALTER TABLE "{fallback}" RENAME TO "{preferred}"')
            print_success(f"Renamed {fallback} → {preferred}")
            LOGGER.info(f"Renamed table {fallback} → {preferred}")
            return preferred
        except sqlite3.OperationalError as e:
            print_error(f"Gagal rename tabel: {e}")
            LOGGER.error(f"Table rename failed: {e}")
            return fallback
    return preferred


# ═══════════════════════════════════════════════════════════════════════════════
# TRANSACTION & CONNECTION MANAGEMENT
# ═══════════════════════════════════════════════════════════════════════════════

@contextmanager
def get_db_connection():
    """Context manager for DB connection with auto-cleanup."""
    conn = None
    try:
        if DB_TYPE == 'sqlite':
            conn = sqlite3.connect(DB_PATH)
            conn.execute("PRAGMA foreign_keys = ON;")
            conn.execute("PRAGMA journal_mode = WAL;")
            conn.execute("PRAGMA synchronous = NORMAL;")
            conn.execute("PRAGMA temp_store = MEMORY;")
            conn.execute("PRAGMA mmap_size = 30000000000;")
        else:
            conn = psycopg2.connect(DB_URL)
        yield conn
    except Exception as e:
        LOGGER.error(f"DB connection failed: {e}")
        raise
    finally:
        if conn:
            try:
                conn.close()
            except Exception:
                pass


@contextmanager
def transaction_savepoint(conn, name: str):
    """Granular savepoint for rollback per step."""
    # Sanitize savepoint name: replace spaces and special chars with underscore
    safe_name = re.sub(r'[^a-zA-Z0-9_]', '_', name)
    cursor = conn.cursor()
    if DB_TYPE == 'sqlite':
        cursor.execute(f"SAVEPOINT {safe_name}")
    else:
        cursor.execute(f"SAVEPOINT {safe_name}")
    try:
        yield cursor
        if not MIGRATION_DRY_RUN:
            if DB_TYPE == 'sqlite':
                cursor.execute(f"RELEASE SAVEPOINT {safe_name}")
            else:
                cursor.execute(f"RELEASE SAVEPOINT {safe_name}")
    except Exception as e:
        if not MIGRATION_DRY_RUN:
            if DB_TYPE == 'sqlite':
                cursor.execute(f"ROLLBACK TO SAVEPOINT {safe_name}")
            else:
                cursor.execute(f"ROLLBACK TO SAVEPOINT {safe_name}")
        LOGGER.error(f"Savepoint {safe_name} rolled back: {e}")
        raise
    finally:
        cursor.close()


# ═══════════════════════════════════════════════════════════════════════════════
# BACKUP & CHECKSUM
# ═══════════════════════════════════════════════════════════════════════════════

def create_backup(source_path):
    os.makedirs(BACKUP_DIR, exist_ok=True)
    timestamp = datetime.now().strftime("%Y%m%d_%H%M%S")
    backup_name = f"database_backup_{timestamp}_v{MIGRATION_VERSION}.db"
    backup_path = os.path.join(BACKUP_DIR, backup_name)
    if os.path.exists(source_path):
        shutil.copy2(source_path, backup_path)
        LOGGER.info(f"Backup created: {backup_path}")
        return backup_path
    return None


def get_db_checksum(path):
    if not os.path.exists(path):
        return None
    with open(path, 'rb') as f:
        return hashlib.md5(f.read()).hexdigest()


# ═══════════════════════════════════════════════════════════════════════════════
# MIGRATION TRACKING
# ═══════════════════════════════════════════════════════════════════════════════

def init_migration_tracking(cursor):
    ddl = """
        CREATE TABLE IF NOT EXISTS _migration_log (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            version VARCHAR(20) NOT NULL,
            executed_at DATETIME DEFAULT CURRENT_TIMESTAMP,
            checksum VARCHAR(32),
            duration_seconds REAL,
            status VARCHAR(20) DEFAULT 'success',
            details TEXT,
            db_type VARCHAR(20),
            dry_run BOOLEAN DEFAULT 0
        )
    """
    if DB_TYPE == 'postgres':
        ddl = translate_ddl_for_postgres(ddl)
    execute_sql(cursor, ddl)

    # v3.7.0 backward compatibility: add new columns if table existed from older version
    safe_add_column(cursor, '_migration_log', 'db_type', 'VARCHAR(20)')
    safe_add_column(cursor, '_migration_log', 'dry_run', 'BOOLEAN DEFAULT 0')


def log_migration(cursor, version, checksum, duration, status='success', details=''):
    try:
        execute_sql(cursor, """
            INSERT INTO _migration_log (version, checksum, duration_seconds, status, details, db_type, dry_run)
            VALUES (?, ?, ?, ?, ?, ?, ?)
        """, (version, checksum, duration, status, details, DB_TYPE, int(MIGRATION_DRY_RUN)))
    except Exception as e:
        # Fallback for old _migration_log without db_type/dry_run columns
        LOGGER.warn(f"Logging with fallback (old schema): {e}")
        execute_sql(cursor, """
            INSERT INTO _migration_log (version, checksum, duration_seconds, status, details)
            VALUES (?, ?, ?, ?, ?)
        """, (version, checksum, duration, status, details))


def get_last_migration(cursor):
    cursor.execute("""
        SELECT version, executed_at FROM _migration_log
        WHERE status='success' ORDER BY id DESC LIMIT 1
    """)
    return cursor.fetchone()


def is_migration_applied(cursor, version):
    execute_sql(cursor, "SELECT 1 FROM _migration_log WHERE version=? AND status='success'", (version,))
    return cursor.fetchone() is not None


# ═══════════════════════════════════════════════════════════════════════════════
# SETTING HELPERS
# ═══════════════════════════════════════════════════════════════════════════════

def add_setting(cursor, key, value, description=''):
    execute_sql(cursor, 'SELECT 1 FROM setting WHERE "key"=?', (key,))
    if cursor.fetchone() is None:
        if MIGRATION_DRY_RUN:
            print_info(f"[DRY-RUN] Would insert setting: {key}")
            return True
        execute_sql(cursor,
            'INSERT INTO setting ("key", value, description) VALUES (?, ?, ?)',
            (key, value, description)
        )
        LOGGER.info(f"Setting added: {key}")
        return True
    return False


def update_setting(cursor, key, value):
    if MIGRATION_DRY_RUN:
        print_info(f"[DRY-RUN] Would update setting: {key}")
        return True
    execute_sql(cursor, 'UPDATE setting SET value=? WHERE "key"=?', (value, key))
    return cursor.rowcount > 0


def update_setting_desc(cursor, key, description):
    if MIGRATION_DRY_RUN:
        return True
    execute_sql(cursor,
        'UPDATE setting SET description=? WHERE "key"=? AND (description IS NULL OR description=\'\')',
        (description, key)
    )
    return cursor.rowcount > 0


# ═══════════════════════════════════════════════════════════════════════════════
# HEALTH CHECKS
# ═══════════════════════════════════════════════════════════════════════════════

def pre_migration_health_check() -> Tuple[bool, List[str]]:
    """Validate environment before migration starts."""
    issues = []

    # Check disk space (minimum 50MB)
    if hasattr(os, 'statvfs'):
        stat = os.statvfs('.')
        free_mb = (stat.f_bavail * stat.f_frsize) / (1024 * 1024)
        if free_mb < 50:
            issues.append(f"Disk space low: {free_mb:.1f}MB free (min 50MB)")

    # Check write permissions
    test_file = os.path.join('instance', '.write_test')
    try:
        os.makedirs('instance', exist_ok=True)
        with open(test_file, 'w') as f:
            f.write('test')
        os.remove(test_file)
    except Exception as e:
        issues.append(f"Write permission denied: {e}")

    # Check Python version
    if sys.version_info < (3, 8):
        issues.append(f"Python {sys.version_info.major}.{sys.version_info.minor} — minimum 3.8 required")

    # Check required modules
    required = ['sqlite3', 'werkzeug']
    if DB_TYPE == 'postgres':
        required.append('psycopg2')
    for mod in required:
        try:
            __import__(mod)
        except ImportError:
            issues.append(f"Missing module: {mod}")

    # Check DB accessibility
    if DB_TYPE == 'sqlite':
        if os.path.exists(DB_PATH):
            try:
                conn = sqlite3.connect(DB_PATH)
                conn.execute("SELECT 1")
                conn.close()
            except Exception as e:
                issues.append(f"Cannot open SQLite DB: {e}")
    else:
        try:
            conn = psycopg2.connect(DB_URL)
            conn.close()
        except Exception as e:
            issues.append(f"Cannot connect to Postgres: {e}")

    return len(issues) == 0, issues


def post_migration_health_check(cursor) -> Tuple[bool, List[str]]:
    """Validate schema integrity after migration."""
    issues = []

    required_tables = list(TABLES.keys()) + ['_migration_log']
    for t in required_tables:
        if not table_exists(cursor, t):
            issues.append(f"Missing table: {t}")

    if DB_TYPE == 'sqlite':
        cursor.execute("PRAGMA foreign_key_check")
        fk_errors = cursor.fetchall()
        if fk_errors:
            issues.append(f"{len(fk_errors)} foreign key violations")
            for err in fk_errors:
                LOGGER.error(f"FK error: table={err[0]}, row={err[1]}, parent={err[2]}, fkey={err[3]}")

    # Validate row counts
    counts = {}
    for t in TABLES.keys():
        try:
            cursor.execute(f'SELECT COUNT(*) FROM "{t}"')
            counts[t] = cursor.fetchone()[0]
        except Exception as e:
            issues.append(f"Cannot count {t}: {e}")

    LOGGER.info(f"Post-migration row counts: {counts}")
    return len(issues) == 0, issues


# ═══════════════════════════════════════════════════════════════════════════════
# SCHEMA DIFF (Dry-Run Preview)
# ═══════════════════════════════════════════════════════════════════════════════

def generate_schema_diff(cursor) -> List[str]:
    """Generate list of changes that would be applied."""
    diffs = []

    for table_name, ddl in TABLES.items():
        if not table_exists(cursor, table_name):
            diffs.append(f"CREATE TABLE {table_name}")
        else:
            cols = MIGRATION_COLUMNS.get(table_name, {})
            for col, col_def in cols.items():
                if not column_exists(cursor, table_name, col):
                    diffs.append(f"ALTER TABLE {table_name} ADD COLUMN {col}")

    for idx_name, table, column in INDEXES:
        if not index_exists(cursor, idx_name):
            diffs.append(f"CREATE INDEX {idx_name} ON {table}({column})")

    for key, value, desc in DEFAULT_SETTINGS:
        execute_sql(cursor, 'SELECT 1 FROM setting WHERE "key"=?', (key,))
        if cursor.fetchone() is None:
            diffs.append(f"INSERT setting: {key}")

    return diffs


# ═══════════════════════════════════════════════════════════════════════════════
# SKEMA TABEL (v3.7.0 — unchanged schema, enhanced execution)
# ═══════════════════════════════════════════════════════════════════════════════

TABLES = {
    'admin': """
        
        CREATE TABLE admin (
        	id INTEGER NOT NULL, 
        	username VARCHAR(80) NOT NULL, 
        	password_hash VARCHAR(200) NOT NULL, 
        	PRIMARY KEY (id)
        )
        
        
    """,
    'user': """
        
        CREATE TABLE user (
        	id INTEGER NOT NULL, 
        	email VARCHAR(120) NOT NULL, 
        	username VARCHAR(80) NOT NULL, 
        	full_name VARCHAR(150), 
        	phone VARCHAR(20), 
        	address TEXT, 
        	default_province VARCHAR(100), 
        	default_city VARCHAR(100), 
        	default_district VARCHAR(100), 
        	default_postal_code VARCHAR(10), 
        	avatar_filename VARCHAR(200), 
        	password_hash VARCHAR(200) NOT NULL, 
        	is_active BOOLEAN, 
        	created_at DATETIME, 
        	updated_at DATETIME, 
        	google_id VARCHAR(64), 
        	oauth_provider VARCHAR(20), 
        	google_avatar_url VARCHAR(500), 
        	has_usable_password BOOLEAN, 
        	kyc_verified BOOLEAN, 
        	kyc_verified_at DATETIME, 
        	kyc_verification_method VARCHAR(50), 
        	kyc_face_descriptor TEXT, 
        	kyc_ktp_photo VARCHAR(200), 
        	kyc_selfie_photo VARCHAR(200), 
        	kyc_nik VARCHAR(16), 
        	kyc_match_score FLOAT, 
        	kyc_liveness_passed BOOLEAN, 
        	PRIMARY KEY (id)
        )
        
        
    """,
    'address': """
        
        CREATE TABLE address (
        	id INTEGER NOT NULL, 
        	user_id INTEGER NOT NULL, 
        	label VARCHAR(50) NOT NULL, 
        	recipient_name VARCHAR(150) NOT NULL, 
        	recipient_phone VARCHAR(20) NOT NULL, 
        	full_address TEXT NOT NULL, 
        	province VARCHAR(100) NOT NULL, 
        	city VARCHAR(100) NOT NULL, 
        	district VARCHAR(100) NOT NULL, 
        	postal_code VARCHAR(10) NOT NULL, 
        	is_default BOOLEAN, 
        	created_at DATETIME, 
        	updated_at DATETIME, 
        	PRIMARY KEY (id), 
        	FOREIGN KEY(user_id) REFERENCES user (id)
        )
        
        
    """,
    'product': """
        
        CREATE TABLE product (
        	id INTEGER NOT NULL, 
        	name VARCHAR(200) NOT NULL, 
        	slug VARCHAR(200), 
        	brand VARCHAR(100) NOT NULL, 
        	model VARCHAR(100), 
        	sku VARCHAR(50), 
        	barcode VARCHAR(100), 
        	category VARCHAR(100), 
        	subcategory VARCHAR(100), 
        	tags VARCHAR(500), 
        	price INTEGER NOT NULL, 
        	compare_at_price INTEGER, 
        	cost_price INTEGER, 
        	condition VARCHAR(50), 
        	stock INTEGER, 
        	low_stock_alert INTEGER, 
        	short_description TEXT, 
        	description TEXT, 
        	specifications JSON, 
        	rating FLOAT, 
        	review_count INTEGER, 
        	weight FLOAT, 
        	width FLOAT, 
        	length FLOAT, 
        	height FLOAT, 
        	image_filename VARCHAR(200), 
        	image_filename2 VARCHAR(200), 
        	image_filename3 VARCHAR(200), 
        	image_filename4 VARCHAR(200), 
        	status VARCHAR(20), 
        	is_featured BOOLEAN, 
        	is_active BOOLEAN, 
        	meta_title VARCHAR(200), 
        	meta_description TEXT, 
        	meta_keywords VARCHAR(250), 
        	created_at DATETIME, 
        	updated_at DATETIME, 
        	PRIMARY KEY (id)
        )
        
        
    """,
    'setting': """
        
        CREATE TABLE setting (
        	id INTEGER NOT NULL, 
        	"key" VARCHAR(100) NOT NULL, 
        	value TEXT, 
        	description TEXT, 
        	PRIMARY KEY (id)
        )
        
        
    """,
    'ai_config': """
        
        CREATE TABLE ai_config (
        	id INTEGER NOT NULL, 
        	name VARCHAR(100), 
        	is_active BOOLEAN, 
        	base_url VARCHAR(500), 
        	api_key VARCHAR(500), 
        	model VARCHAR(200), 
        	site_url VARCHAR(500), 
        	app_name VARCHAR(200), 
        	system_prompt TEXT, 
        	notify_new_order BOOLEAN, 
        	notify_status_update BOOLEAN, 
        	notify_product_update BOOLEAN, 
        	webhook_secret VARCHAR(500), 
        	created_at DATETIME, 
        	updated_at DATETIME, 
        	PRIMARY KEY (id)
        )
        
        
    """,
    'testimonial': """
        
        CREATE TABLE testimonial (
        	id INTEGER NOT NULL, 
        	name VARCHAR(100) NOT NULL, 
        	location VARCHAR(100), 
        	rating INTEGER, 
        	text TEXT NOT NULL, 
        	image_filename VARCHAR(200), 
        	is_active BOOLEAN, 
        	"order" INTEGER, 
        	created_at DATETIME, 
        	PRIMARY KEY (id)
        )
        
        
    """,
    'hero_slide': """
        
        CREATE TABLE hero_slide (
        	id INTEGER NOT NULL, 
        	image_filename VARCHAR(200) NOT NULL, 
        	title VARCHAR(200), 
        	subtitle VARCHAR(200), 
        	button_text VARCHAR(50), 
        	button_url VARCHAR(200), 
        	"order" INTEGER, 
        	is_active BOOLEAN, 
        	created_at DATETIME, 
        	PRIMARY KEY (id)
        )
        
        
    """,
    'orders': """
        
        CREATE TABLE orders (
        	id INTEGER NOT NULL, 
        	order_number VARCHAR(20) NOT NULL, 
        	customer_name VARCHAR(100) NOT NULL, 
        	customer_phone VARCHAR(20) NOT NULL, 
        	customer_email VARCHAR(100), 
        	shipping_address TEXT NOT NULL, 
        	payment_method VARCHAR(50), 
        	status VARCHAR(20), 
        	priority VARCHAR(10), 
        	notes TEXT, 
        	subtotal INTEGER NOT NULL, 
        	shipping_cost INTEGER, 
        	discount INTEGER, 
        	total INTEGER NOT NULL, 
        	user_id INTEGER, 
        	paid_at DATETIME, 
        	shipped_at DATETIME, 
        	completed_at DATETIME, 
        	carrier_name VARCHAR(50), 
        	tracking_number VARCHAR(100), 
        	estimated_delivery DATETIME, 
        	cancel_reason TEXT, 
        	return_reason TEXT, 
        	return_resolution VARCHAR(50), 
        	return_notes TEXT, 
        	sla_deadline DATETIME, 
        	payment_proof VARCHAR(200), 
        	is_credit_order BOOLEAN, 
        	credit_application_id INTEGER, 
        	credit_down_payment INTEGER, 
        	credit_monthly_installment INTEGER, 
        	credit_tenor_months INTEGER, 
        	credit_remaining_balance INTEGER, 
        	shipping_recipient_name VARCHAR(150), 
        	shipping_phone VARCHAR(20), 
        	shipping_province VARCHAR(100), 
        	shipping_city VARCHAR(100), 
        	shipping_district VARCHAR(100), 
        	shipping_postal_code VARCHAR(10), 
        	shipping_courier VARCHAR(50), 
        	shipping_service VARCHAR(50), 
        	shipping_notes TEXT, 
        	created_at DATETIME, 
        	updated_at DATETIME, 
        	PRIMARY KEY (id), 
        	FOREIGN KEY(user_id) REFERENCES user (id), 
        	FOREIGN KEY(credit_application_id) REFERENCES installment_applications (id)
        )
        
        
    """,
    'order_items': """
        
        CREATE TABLE order_items (
        	id INTEGER NOT NULL, 
        	order_id INTEGER NOT NULL, 
        	product_id INTEGER, 
        	name VARCHAR(200), 
        	brand VARCHAR(100), 
        	model VARCHAR(100), 
        	condition VARCHAR(50), 
        	price INTEGER, 
        	quantity INTEGER, 
        	PRIMARY KEY (id), 
        	FOREIGN KEY(order_id) REFERENCES orders (id), 
        	FOREIGN KEY(product_id) REFERENCES product (id) ON DELETE SET NULL
        )
        
        
    """,
    'installment_plans': """
        
        CREATE TABLE installment_plans (
        	id INTEGER NOT NULL, 
        	name VARCHAR(100) NOT NULL, 
        	tenor_months INTEGER NOT NULL, 
        	interest_rate_monthly FLOAT NOT NULL, 
        	interest_type VARCHAR(20), 
        	min_dp_percentage FLOAT, 
        	max_dp_percentage FLOAT, 
        	min_order_amount INTEGER, 
        	max_order_amount INTEGER, 
        	processing_fee INTEGER, 
        	late_fee_per_day INTEGER, 
        	grace_period_days INTEGER, 
        	is_active BOOLEAN, 
        	description TEXT, 
        	created_at DATETIME, 
        	PRIMARY KEY (id)
        )
        
        
    """,
    'kyc_verification_sessions': """
        
        CREATE TABLE kyc_verification_sessions (
        	id INTEGER NOT NULL, 
        	user_id INTEGER NOT NULL, 
        	session_token VARCHAR(100) NOT NULL, 
        	ktp_face_descriptor TEXT, 
        	selfie_face_descriptor TEXT, 
        	face_match_score FLOAT, 
        	face_match_confidence FLOAT, 
        	liveness_score FLOAT, 
        	ktp_photo_path VARCHAR(200), 
        	selfie_photo_path VARCHAR(200), 
        	ktp_nik VARCHAR(16), 
        	ktp_name VARCHAR(150), 
        	ktp_address TEXT, 
        	birth_date DATE, 
        	occupation VARCHAR(150), 
        	employment_status VARCHAR(50), 
        	monthly_income INTEGER, 
        	emergency_contact_name VARCHAR(150), 
        	emergency_contact_phone VARCHAR(20), 
        	emergency_contact_relation VARCHAR(50), 
        	status VARCHAR(20), 
        	verification_method VARCHAR(50), 
        	rejection_reason TEXT, 
        	reviewed_by INTEGER, 
        	created_at DATETIME, 
        	expires_at DATETIME, 
        	verified_at DATETIME, 
        	ip_address VARCHAR(45), 
        	user_agent TEXT, 
        	PRIMARY KEY (id), 
        	FOREIGN KEY(user_id) REFERENCES user (id), 
        	FOREIGN KEY(reviewed_by) REFERENCES admin (id)
        )
        
        
    """,
    'installment_applications': """
        
        CREATE TABLE installment_applications (
        	id INTEGER NOT NULL, 
        	application_code VARCHAR(20) NOT NULL, 
        	user_id INTEGER NOT NULL, 
        	order_id INTEGER, 
        	plan_id INTEGER, 
        	kyc_session_id INTEGER, 
        	full_name VARCHAR(150) NOT NULL, 
        	nik VARCHAR(16) NOT NULL, 
        	birth_place VARCHAR(100), 
        	birth_date DATE, 
        	gender VARCHAR(10), 
        	marital_status VARCHAR(20), 
        	phone VARCHAR(20) NOT NULL, 
        	email VARCHAR(120), 
        	emergency_contact_name VARCHAR(150), 
        	emergency_contact_phone VARCHAR(20), 
        	emergency_contact_relation VARCHAR(50), 
        	address_ktp TEXT NOT NULL, 
        	address_domisili TEXT, 
        	is_domisili_same BOOLEAN, 
        	employment_type VARCHAR(50), 
        	company_name VARCHAR(150), 
        	company_address TEXT, 
        	job_title VARCHAR(100), 
        	work_duration_months INTEGER, 
        	monthly_income INTEGER, 
        	other_income INTEGER, 
        	ktp_photo VARCHAR(200), 
        	selfie_photo VARCHAR(200), 
        	slip_gaji_photo VARCHAR(200), 
        	npwp_photo VARCHAR(200), 
        	kk_photo VARCHAR(200), 
        	product_amount INTEGER NOT NULL, 
        	dp_amount INTEGER NOT NULL, 
        	loan_amount INTEGER NOT NULL, 
        	interest_amount INTEGER, 
        	processing_fee_amount INTEGER, 
        	total_loan INTEGER NOT NULL, 
        	monthly_installment INTEGER NOT NULL, 
        	status VARCHAR(20), 
        	rejection_reason TEXT, 
        	approved_by INTEGER, 
        	approved_at DATETIME, 
        	rejected_by INTEGER, 
        	rejected_at DATETIME, 
        	credit_score INTEGER, 
        	risk_level VARCHAR(20), 
        	biometric_verified BOOLEAN, 
        	biometric_match_score FLOAT, 
        	biometric_liveness_passed BOOLEAN, 
        	ip_address VARCHAR(45), 
        	user_agent TEXT, 
        	created_at DATETIME, 
        	updated_at DATETIME, 
        	product_id INTEGER, 
        	product_price INTEGER, 
        	down_payment INTEGER, 
        	down_payment_pct FLOAT, 
        	principal INTEGER, 
        	tenor_months INTEGER, 
        	interest_type VARCHAR(20), 
        	interest_rate_monthly FLOAT, 
        	interest_rate_annual FLOAT, 
        	total_interest INTEGER, 
        	total_payment INTEGER, 
        	ktp_name VARCHAR(150), 
        	ktp_nik VARCHAR(16), 
        	ktp_address TEXT, 
        	employment_status VARCHAR(50), 
        	PRIMARY KEY (id), 
        	FOREIGN KEY(user_id) REFERENCES user (id), 
        	FOREIGN KEY(order_id) REFERENCES orders (id), 
        	FOREIGN KEY(plan_id) REFERENCES installment_plans (id), 
        	FOREIGN KEY(kyc_session_id) REFERENCES kyc_verification_sessions (id), 
        	FOREIGN KEY(approved_by) REFERENCES admin (id), 
        	FOREIGN KEY(rejected_by) REFERENCES admin (id), 
        	FOREIGN KEY(product_id) REFERENCES product (id)
        )
        
        
    """,
    'installment_schedules': """
        
        CREATE TABLE installment_schedules (
        	id INTEGER NOT NULL, 
        	application_id INTEGER NOT NULL, 
        	installment_number INTEGER NOT NULL, 
        	due_date DATETIME NOT NULL, 
        	amount INTEGER NOT NULL, 
        	principal_amount INTEGER NOT NULL, 
        	interest_amount INTEGER NOT NULL, 
        	paid_amount INTEGER, 
        	paid_at DATETIME, 
        	payment_method VARCHAR(50), 
        	payment_proof VARCHAR(200), 
        	status VARCHAR(20), 
        	late_days INTEGER, 
        	late_fee INTEGER, 
        	notes TEXT, 
        	created_at DATETIME, 
        	updated_at DATETIME, 
        	PRIMARY KEY (id), 
        	FOREIGN KEY(application_id) REFERENCES installment_applications (id)
        )
        
        
    """,
    'installment_payments': """
        
        CREATE TABLE installment_payments (
        	id INTEGER NOT NULL, 
        	schedule_id INTEGER NOT NULL, 
        	application_id INTEGER NOT NULL, 
        	amount INTEGER NOT NULL, 
        	payment_date DATETIME, 
        	payment_method VARCHAR(50), 
        	payment_proof VARCHAR(200), 
        	reference_number VARCHAR(100), 
        	notes TEXT, 
        	processed_by INTEGER, 
        	created_at DATETIME, 
        	PRIMARY KEY (id), 
        	FOREIGN KEY(schedule_id) REFERENCES installment_schedules (id), 
        	FOREIGN KEY(application_id) REFERENCES installment_applications (id), 
        	FOREIGN KEY(processed_by) REFERENCES admin (id)
        )
        
        
    """,
    'shipping_tracking': """
        
        CREATE TABLE shipping_tracking (
        	id INTEGER NOT NULL, 
        	order_id INTEGER NOT NULL, 
        	tracking_number VARCHAR(100) NOT NULL, 
        	carrier VARCHAR(50) NOT NULL, 
        	recipient_name VARCHAR(150), 
        	recipient_phone VARCHAR(20), 
        	recipient_address TEXT, 
        	shipping_cost INTEGER, 
        	total_cost INTEGER, 
        	status VARCHAR(50), 
        	current_location VARCHAR(200), 
        	estimated_delivery DATETIME, 
        	actual_delivery DATETIME, 
        	is_delayed BOOLEAN, 
        	delay_days INTEGER, 
        	has_issue BOOLEAN, 
        	issue_type VARCHAR(50), 
        	issue_description TEXT, 
        	issue_reported_date DATETIME, 
        	created_at DATETIME, 
        	updated_at DATETIME, 
        	PRIMARY KEY (id), 
        	FOREIGN KEY(order_id) REFERENCES orders (id)
        )
        
        
    """,
    'shipping_tracking_events': """
        
        CREATE TABLE shipping_tracking_events (
        	id INTEGER NOT NULL, 
        	shipping_tracking_id INTEGER NOT NULL, 
        	status VARCHAR(50) NOT NULL, 
        	status_description TEXT, 
        	location VARCHAR(200), 
        	event_type VARCHAR(50), 
        	event_time DATETIME, 
        	metadata_json TEXT, 
        	created_at DATETIME, 
        	PRIMARY KEY (id), 
        	FOREIGN KEY(shipping_tracking_id) REFERENCES shipping_tracking (id)
        )
        
        
    """,
    'shipping_issues': """
        
        CREATE TABLE shipping_issues (
        	id INTEGER NOT NULL, 
        	shipping_tracking_id INTEGER NOT NULL, 
        	issue_type VARCHAR(50) NOT NULL, 
        	description TEXT NOT NULL, 
        	severity VARCHAR(20), 
        	reported_at DATETIME, 
        	resolved_at DATETIME, 
        	resolution TEXT, 
        	resolved_by INTEGER, 
        	status VARCHAR(20), 
        	created_at DATETIME, 
        	updated_at DATETIME, 
        	PRIMARY KEY (id), 
        	FOREIGN KEY(shipping_tracking_id) REFERENCES shipping_tracking (id), 
        	FOREIGN KEY(resolved_by) REFERENCES admin (id)
        )
        
        
    """,
    'shipping_carrier_performance': """
        
        CREATE TABLE shipping_carrier_performance (
        	id INTEGER NOT NULL, 
        	carrier_name VARCHAR(50) NOT NULL, 
        	total_shipments INTEGER, 
        	delivered_count INTEGER, 
        	delayed_count INTEGER, 
        	issue_count INTEGER, 
        	avg_delivery_days FLOAT, 
        	on_time_rate FLOAT, 
        	rating FLOAT, 
        	review_count INTEGER, 
        	last_updated DATETIME, 
        	PRIMARY KEY (id)
        )
        
        
    """,
    'chat_conversations': """
        
        CREATE TABLE chat_conversations (
        	id INTEGER NOT NULL, 
        	session_id VARCHAR(64) NOT NULL, 
        	customer_key VARCHAR(64), 
        	user_id INTEGER, 
        	title VARCHAR(160), 
        	is_archived BOOLEAN NOT NULL, 
        	created_at DATETIME NOT NULL, 
        	updated_at DATETIME NOT NULL, 
        	last_message_at DATETIME, 
        	PRIMARY KEY (id)
        )
        
        
    """,
    'chat_messages': """
        
        CREATE TABLE chat_messages (
        	id INTEGER NOT NULL, 
        	conversation_id INTEGER NOT NULL, 
        	role VARCHAR(20) NOT NULL, 
        	content TEXT NOT NULL, 
        	model VARCHAR(80), 
        	cost_usd FLOAT, 
        	tool_calls_json TEXT, 
        	source VARCHAR(20), 
        	created_at DATETIME NOT NULL, 
        	PRIMARY KEY (id), 
        	FOREIGN KEY(conversation_id) REFERENCES chat_conversations (id)
        )
        
        
    """,
    'customer_memory_facts': """
        
        CREATE TABLE customer_memory_facts (
        	id INTEGER NOT NULL, 
        	customer_key VARCHAR(64) NOT NULL, 
        	fact_key VARCHAR(80) NOT NULL, 
        	fact_value TEXT NOT NULL, 
        	fact_type VARCHAR(30), 
        	source_session VARCHAR(64), 
        	created_at DATETIME NOT NULL, 
        	updated_at DATETIME NOT NULL, 
        	PRIMARY KEY (id), 
        	CONSTRAINT uq_customer_memory_key UNIQUE (customer_key, fact_key)
        )
        
        
    """,
}


MIGRATION_COLUMNS = {
    'admin': {
        'username': 'VARCHAR(80) UNIQUE NOT NULL',
        'password_hash': 'VARCHAR(200) NOT NULL',
    },
    'user': {
        'email': 'VARCHAR(120) UNIQUE NOT NULL',
        'username': 'VARCHAR(80) UNIQUE NOT NULL',
        'full_name': 'VARCHAR(150)',
        'phone': 'VARCHAR(20) UNIQUE',
        'address': 'TEXT',
        'default_province': 'VARCHAR(100)',
        'default_city': 'VARCHAR(100)',
        'default_district': 'VARCHAR(100)',
        'default_postal_code': 'VARCHAR(10)',
        'avatar_filename': 'VARCHAR(200)',
        'password_hash': 'VARCHAR(200) NOT NULL',
        'is_active': 'BOOLEAN',
        'created_at': 'DATETIME',
        'updated_at': 'DATETIME',
        'google_id': 'VARCHAR(64) UNIQUE',
        'oauth_provider': 'VARCHAR(20)',
        'google_avatar_url': 'VARCHAR(500)',
        'has_usable_password': 'BOOLEAN',
        'kyc_verified': 'BOOLEAN',
        'kyc_verified_at': 'DATETIME',
        'kyc_verification_method': 'VARCHAR(50)',
        'kyc_face_descriptor': 'TEXT',
        'kyc_ktp_photo': 'VARCHAR(200)',
        'kyc_selfie_photo': 'VARCHAR(200)',
        'kyc_nik': 'VARCHAR(16)',
        'kyc_match_score': 'FLOAT',
        'kyc_liveness_passed': 'BOOLEAN',
    },
    'address': {
        'user_id': 'INTEGER NOT NULL',
        'label': 'VARCHAR(50) NOT NULL',
        'recipient_name': 'VARCHAR(150) NOT NULL',
        'recipient_phone': 'VARCHAR(20) NOT NULL',
        'full_address': 'TEXT NOT NULL',
        'province': 'VARCHAR(100) NOT NULL',
        'city': 'VARCHAR(100) NOT NULL',
        'district': 'VARCHAR(100) NOT NULL',
        'postal_code': 'VARCHAR(10) NOT NULL',
        'is_default': 'BOOLEAN',
        'created_at': 'DATETIME',
        'updated_at': 'DATETIME',
    },
    'product': {
        'name': 'VARCHAR(200) NOT NULL',
        'slug': 'VARCHAR(200) UNIQUE',
        'brand': 'VARCHAR(100) NOT NULL',
        'model': 'VARCHAR(100)',
        'sku': 'VARCHAR(50)',
        'barcode': 'VARCHAR(100)',
        'category': 'VARCHAR(100)',
        'subcategory': 'VARCHAR(100)',
        'tags': 'VARCHAR(500)',
        'price': 'INTEGER NOT NULL',
        'compare_at_price': 'INTEGER',
        'cost_price': 'INTEGER',
        'condition': 'VARCHAR(50)',
        'stock': 'INTEGER',
        'low_stock_alert': 'INTEGER',
        'short_description': 'TEXT',
        'description': 'TEXT',
        'specifications': 'JSON',
        'rating': 'FLOAT',
        'review_count': 'INTEGER',
        'weight': 'FLOAT',
        'width': 'FLOAT',
        'length': 'FLOAT',
        'height': 'FLOAT',
        'image_filename': 'VARCHAR(200)',
        'image_filename2': 'VARCHAR(200)',
        'image_filename3': 'VARCHAR(200)',
        'image_filename4': 'VARCHAR(200)',
        'status': 'VARCHAR(20)',
        'is_featured': 'BOOLEAN',
        'is_active': 'BOOLEAN',
        'meta_title': 'VARCHAR(200)',
        'meta_description': 'TEXT',
        'meta_keywords': 'VARCHAR(250)',
        'created_at': 'DATETIME',
        'updated_at': 'DATETIME',
    },
    'setting': {
        'key': 'VARCHAR(100) UNIQUE NOT NULL',
        'value': 'TEXT',
        'description': 'TEXT',
    },
    'ai_config': {
        'name': 'VARCHAR(100)',
        'is_active': 'BOOLEAN',
        'base_url': 'VARCHAR(500)',
        'api_key': 'VARCHAR(500)',
        'model': 'VARCHAR(200)',
        'site_url': 'VARCHAR(500)',
        'app_name': 'VARCHAR(200)',
        'system_prompt': 'TEXT',
        'notify_new_order': 'BOOLEAN',
        'notify_status_update': 'BOOLEAN',
        'notify_product_update': 'BOOLEAN',
        'webhook_secret': 'VARCHAR(500)',
        'created_at': 'DATETIME',
        'updated_at': 'DATETIME',
    },
    'testimonial': {
        'name': 'VARCHAR(100) NOT NULL',
        'location': 'VARCHAR(100)',
        'rating': 'INTEGER',
        'text': 'TEXT NOT NULL',
        'image_filename': 'VARCHAR(200)',
        'is_active': 'BOOLEAN',
        'order': 'INTEGER',
        'created_at': 'DATETIME',
    },
    'hero_slide': {
        'image_filename': 'VARCHAR(200) NOT NULL',
        'title': 'VARCHAR(200)',
        'subtitle': 'VARCHAR(200)',
        'button_text': 'VARCHAR(50)',
        'button_url': 'VARCHAR(200)',
        'order': 'INTEGER',
        'is_active': 'BOOLEAN',
        'created_at': 'DATETIME',
    },
    'orders': {
        'order_number': 'VARCHAR(20) UNIQUE NOT NULL',
        'customer_name': 'VARCHAR(100) NOT NULL',
        'customer_phone': 'VARCHAR(20) NOT NULL',
        'customer_email': 'VARCHAR(100)',
        'shipping_address': 'TEXT NOT NULL',
        'payment_method': 'VARCHAR(50)',
        'status': 'VARCHAR(20)',
        'priority': 'VARCHAR(10)',
        'notes': 'TEXT',
        'subtotal': 'INTEGER NOT NULL',
        'shipping_cost': 'INTEGER',
        'discount': 'INTEGER',
        'total': 'INTEGER NOT NULL',
        'user_id': 'INTEGER',
        'paid_at': 'DATETIME',
        'shipped_at': 'DATETIME',
        'completed_at': 'DATETIME',
        'carrier_name': 'VARCHAR(50)',
        'tracking_number': 'VARCHAR(100)',
        'estimated_delivery': 'DATETIME',
        'cancel_reason': 'TEXT',
        'return_reason': 'TEXT',
        'return_resolution': 'VARCHAR(50)',
        'return_notes': 'TEXT',
        'sla_deadline': 'DATETIME',
        'payment_proof': 'VARCHAR(200)',
        'is_credit_order': 'BOOLEAN',
        'credit_application_id': 'INTEGER',
        'credit_down_payment': 'INTEGER',
        'credit_monthly_installment': 'INTEGER',
        'credit_tenor_months': 'INTEGER',
        'credit_remaining_balance': 'INTEGER',
        'shipping_recipient_name': 'VARCHAR(150)',
        'shipping_phone': 'VARCHAR(20)',
        'shipping_province': 'VARCHAR(100)',
        'shipping_city': 'VARCHAR(100)',
        'shipping_district': 'VARCHAR(100)',
        'shipping_postal_code': 'VARCHAR(10)',
        'shipping_courier': 'VARCHAR(50)',
        'shipping_service': 'VARCHAR(50)',
        'shipping_notes': 'TEXT',
        'created_at': 'DATETIME',
        'updated_at': 'DATETIME',
    },
    'order_items': {
        'order_id': 'INTEGER NOT NULL',
        'product_id': 'INTEGER',
        'name': 'VARCHAR(200)',
        'brand': 'VARCHAR(100)',
        'model': 'VARCHAR(100)',
        'condition': 'VARCHAR(50)',
        'price': 'INTEGER',
        'quantity': 'INTEGER',
    },
    'installment_plans': {
        'name': 'VARCHAR(100) NOT NULL',
        'tenor_months': 'INTEGER NOT NULL',
        'interest_rate_monthly': 'FLOAT NOT NULL',
        'interest_type': 'VARCHAR(20)',
        'min_dp_percentage': 'FLOAT',
        'max_dp_percentage': 'FLOAT',
        'min_order_amount': 'INTEGER',
        'max_order_amount': 'INTEGER',
        'processing_fee': 'INTEGER',
        'late_fee_per_day': 'INTEGER',
        'grace_period_days': 'INTEGER',
        'is_active': 'BOOLEAN',
        'description': 'TEXT',
        'created_at': 'DATETIME',
    },
    'kyc_verification_sessions': {
        'user_id': 'INTEGER NOT NULL',
        'session_token': 'VARCHAR(100) UNIQUE NOT NULL',
        'ktp_face_descriptor': 'TEXT',
        'selfie_face_descriptor': 'TEXT',
        'face_match_score': 'FLOAT',
        'face_match_confidence': 'FLOAT',
        'liveness_score': 'FLOAT',
        'ktp_photo_path': 'VARCHAR(200)',
        'selfie_photo_path': 'VARCHAR(200)',
        'ktp_nik': 'VARCHAR(16)',
        'ktp_name': 'VARCHAR(150)',
        'ktp_address': 'TEXT',
        'birth_date': 'DATE',
        'occupation': 'VARCHAR(150)',
        'employment_status': 'VARCHAR(50)',
        'monthly_income': 'INTEGER',
        'emergency_contact_name': 'VARCHAR(150)',
        'emergency_contact_phone': 'VARCHAR(20)',
        'emergency_contact_relation': 'VARCHAR(50)',
        'status': 'VARCHAR(20)',
        'verification_method': 'VARCHAR(50)',
        'rejection_reason': 'TEXT',
        'reviewed_by': 'INTEGER',
        'created_at': 'DATETIME',
        'expires_at': 'DATETIME',
        'verified_at': 'DATETIME',
        'ip_address': 'VARCHAR(45)',
        'user_agent': 'TEXT',
    },
    'installment_applications': {
        'application_code': 'VARCHAR(20) UNIQUE NOT NULL',
        'user_id': 'INTEGER NOT NULL',
        'order_id': 'INTEGER',
        'plan_id': 'INTEGER',
        'kyc_session_id': 'INTEGER',
        'full_name': 'VARCHAR(150) NOT NULL',
        'nik': 'VARCHAR(16) NOT NULL',
        'birth_place': 'VARCHAR(100)',
        'birth_date': 'DATE',
        'gender': 'VARCHAR(10)',
        'marital_status': 'VARCHAR(20)',
        'phone': 'VARCHAR(20) NOT NULL',
        'email': 'VARCHAR(120)',
        'emergency_contact_name': 'VARCHAR(150)',
        'emergency_contact_phone': 'VARCHAR(20)',
        'emergency_contact_relation': 'VARCHAR(50)',
        'address_ktp': 'TEXT NOT NULL',
        'address_domisili': 'TEXT',
        'is_domisili_same': 'BOOLEAN',
        'employment_type': 'VARCHAR(50)',
        'company_name': 'VARCHAR(150)',
        'company_address': 'TEXT',
        'job_title': 'VARCHAR(100)',
        'work_duration_months': 'INTEGER',
        'monthly_income': 'INTEGER',
        'other_income': 'INTEGER',
        'ktp_photo': 'VARCHAR(200)',
        'selfie_photo': 'VARCHAR(200)',
        'slip_gaji_photo': 'VARCHAR(200)',
        'npwp_photo': 'VARCHAR(200)',
        'kk_photo': 'VARCHAR(200)',
        'product_amount': 'INTEGER NOT NULL',
        'dp_amount': 'INTEGER NOT NULL',
        'loan_amount': 'INTEGER NOT NULL',
        'interest_amount': 'INTEGER',
        'processing_fee_amount': 'INTEGER',
        'total_loan': 'INTEGER NOT NULL',
        'monthly_installment': 'INTEGER NOT NULL',
        'status': 'VARCHAR(20)',
        'rejection_reason': 'TEXT',
        'approved_by': 'INTEGER',
        'approved_at': 'DATETIME',
        'rejected_by': 'INTEGER',
        'rejected_at': 'DATETIME',
        'credit_score': 'INTEGER',
        'risk_level': 'VARCHAR(20)',
        'biometric_verified': 'BOOLEAN',
        'biometric_match_score': 'FLOAT',
        'biometric_liveness_passed': 'BOOLEAN',
        'ip_address': 'VARCHAR(45)',
        'user_agent': 'TEXT',
        'created_at': 'DATETIME',
        'updated_at': 'DATETIME',
        'product_id': 'INTEGER',
        'product_price': 'INTEGER',
        'down_payment': 'INTEGER',
        'down_payment_pct': 'FLOAT',
        'principal': 'INTEGER',
        'tenor_months': 'INTEGER',
        'interest_type': 'VARCHAR(20)',
        'interest_rate_monthly': 'FLOAT',
        'interest_rate_annual': 'FLOAT',
        'total_interest': 'INTEGER',
        'total_payment': 'INTEGER',
        'ktp_name': 'VARCHAR(150)',
        'ktp_nik': 'VARCHAR(16)',
        'ktp_address': 'TEXT',
        'employment_status': 'VARCHAR(50)',
    },
    'installment_schedules': {
        'application_id': 'INTEGER NOT NULL',
        'installment_number': 'INTEGER NOT NULL',
        'due_date': 'DATETIME NOT NULL',
        'amount': 'INTEGER NOT NULL',
        'principal_amount': 'INTEGER NOT NULL',
        'interest_amount': 'INTEGER NOT NULL',
        'paid_amount': 'INTEGER',
        'paid_at': 'DATETIME',
        'payment_method': 'VARCHAR(50)',
        'payment_proof': 'VARCHAR(200)',
        'status': 'VARCHAR(20)',
        'late_days': 'INTEGER',
        'late_fee': 'INTEGER',
        'notes': 'TEXT',
        'created_at': 'DATETIME',
        'updated_at': 'DATETIME',
    },
    'installment_payments': {
        'schedule_id': 'INTEGER NOT NULL',
        'application_id': 'INTEGER NOT NULL',
        'amount': 'INTEGER NOT NULL',
        'payment_date': 'DATETIME',
        'payment_method': 'VARCHAR(50)',
        'payment_proof': 'VARCHAR(200)',
        'reference_number': 'VARCHAR(100)',
        'notes': 'TEXT',
        'processed_by': 'INTEGER',
        'created_at': 'DATETIME',
    },
    'shipping_tracking': {
        'order_id': 'INTEGER NOT NULL',
        'tracking_number': 'VARCHAR(100) NOT NULL',
        'carrier': 'VARCHAR(50) NOT NULL',
        'recipient_name': 'VARCHAR(150)',
        'recipient_phone': 'VARCHAR(20)',
        'recipient_address': 'TEXT',
        'shipping_cost': 'INTEGER',
        'total_cost': 'INTEGER',
        'status': 'VARCHAR(50)',
        'current_location': 'VARCHAR(200)',
        'estimated_delivery': 'DATETIME',
        'actual_delivery': 'DATETIME',
        'is_delayed': 'BOOLEAN',
        'delay_days': 'INTEGER',
        'has_issue': 'BOOLEAN',
        'issue_type': 'VARCHAR(50)',
        'issue_description': 'TEXT',
        'issue_reported_date': 'DATETIME',
        'created_at': 'DATETIME',
        'updated_at': 'DATETIME',
    },
    'shipping_tracking_events': {
        'shipping_tracking_id': 'INTEGER NOT NULL',
        'status': 'VARCHAR(50) NOT NULL',
        'status_description': 'TEXT',
        'location': 'VARCHAR(200)',
        'event_type': 'VARCHAR(50)',
        'event_time': 'DATETIME',
        'metadata_json': 'TEXT',
        'created_at': 'DATETIME',
    },
    'shipping_issues': {
        'shipping_tracking_id': 'INTEGER NOT NULL',
        'issue_type': 'VARCHAR(50) NOT NULL',
        'description': 'TEXT NOT NULL',
        'severity': 'VARCHAR(20)',
        'reported_at': 'DATETIME',
        'resolved_at': 'DATETIME',
        'resolution': 'TEXT',
        'resolved_by': 'INTEGER',
        'status': 'VARCHAR(20)',
        'created_at': 'DATETIME',
        'updated_at': 'DATETIME',
    },
    'shipping_carrier_performance': {
        'carrier_name': 'VARCHAR(50) NOT NULL',
        'total_shipments': 'INTEGER',
        'delivered_count': 'INTEGER',
        'delayed_count': 'INTEGER',
        'issue_count': 'INTEGER',
        'avg_delivery_days': 'FLOAT',
        'on_time_rate': 'FLOAT',
        'rating': 'FLOAT',
        'review_count': 'INTEGER',
        'last_updated': 'DATETIME',
    },
    'chat_conversations': {
        'session_id': 'VARCHAR(64) UNIQUE NOT NULL',
        'customer_key': 'VARCHAR(64)',
        'user_id': 'INTEGER',
        'title': 'VARCHAR(160)',
        'is_archived': 'BOOLEAN NOT NULL',
        'created_at': 'DATETIME NOT NULL',
        'updated_at': 'DATETIME NOT NULL',
        'last_message_at': 'DATETIME',
    },
    'chat_messages': {
        'conversation_id': 'INTEGER NOT NULL',
        'role': 'VARCHAR(20) NOT NULL',
        'content': 'TEXT NOT NULL',
        'model': 'VARCHAR(80)',
        'cost_usd': 'FLOAT',
        'tool_calls_json': 'TEXT',
        'source': 'VARCHAR(20)',
        'created_at': 'DATETIME NOT NULL',
    },
    'customer_memory_facts': {
        'customer_key': 'VARCHAR(64) NOT NULL',
        'fact_key': 'VARCHAR(80) NOT NULL',
        'fact_value': 'TEXT NOT NULL',
        'fact_type': 'VARCHAR(30)',
        'source_session': 'VARCHAR(64)',
        'created_at': 'DATETIME NOT NULL',
        'updated_at': 'DATETIME NOT NULL',
    },
}


INDEXES = [
    ("idx_user_email", "user", "email"),
    ("idx_user_username", "user", "username"),
    ("idx_user_phone", "user", "phone"),
    ("idx_order_number", "orders", "order_number"),
    ("idx_order_status", "orders", "status"),
    ("idx_order_user_id", "orders", "user_id"),
    ("idx_order_created", "orders", "created_at"),
    ("idx_order_item_order_id", "order_items", "order_id"),
    ("idx_order_item_product_id", "order_items", "product_id"),
    ("idx_product_brand", "product", "brand"),
    ("idx_product_slug", "product", "slug"),
    ("idx_product_featured", "product", "is_featured"),
    ("idx_product_active", "product", "is_active"),
    ("idx_product_created", "product", "created_at"),
    ("idx_testimonial_active", "testimonial", "is_active"),
    ("idx_hero_active", "hero_slide", "is_active"),
    ("idx_setting_key", "setting", "key"),
    ("idx_shipping_tracking_order", "shipping_tracking", "order_id"),
    ("idx_shipping_tracking_number", "shipping_tracking", "tracking_number"),
    ("idx_shipping_tracking_status", "shipping_tracking", "status"),
    ("idx_shipping_event_tracking", "shipping_tracking_events", "shipping_tracking_id"),
    ("idx_shipping_event_time", "shipping_tracking_events", "event_time"),
    ("idx_address_user_id", "address", "user_id"),
    ("idx_address_default", "address", "is_default"),
]

DEFAULT_SETTINGS = [
    ('site_name', 'ATLANTIS CELULAR', 'Nama situs / brand'),
    ('preview_title', '', 'Judul web untuk preview link (Open Graph)'),
    ('store_name', '', 'Nama toko lengkap'),
    ('store_tagline', '', 'Tagline / slogan toko'),
    ('preview_description', '', 'Deskripsi web untuk preview link (Open Graph)'),
    ('store_email', '', 'Email kontak toko'),
    ('store_whatsapp', '', 'Nomor WhatsApp toko'),
    ('store_address', '', 'Alamat toko fisik'),
    ('store_city', '', 'Kota toko'),
    ('store_province', '', 'Provinsi toko'),
    ('store_postal', '', 'Kode pos toko'),
    ('logo_url', '', 'Filename logo toko'),
    ('preview_logo_url', '', 'Gambar thumbnail untuk preview link (Open Graph)'),
    ('officer_name', 'Admin', 'Nama petugas untuk surat penebusan'),
    ('currency', 'IDR', 'Mata uang default'),
    ('timezone', 'Asia/Jakarta', 'Zona waktu default'),
    ('date_format', 'dd/MM/yyyy', 'Format tanggal'),
    ('time_format', 'HH:mm', 'Format waktu'),
    ('datetime_format', 'dd/MM/yyyy HH:mm', 'Format tanggal dan waktu'),
    ('date_display_style', 'medium', 'Gaya tampilan: short, medium, long'),
    ('meta_title', '', 'Meta title SEO'),
    ('meta_description', '', 'Meta description SEO'),
    ('admin_ui_enable_analytics', 'true', 'Tampilkan analytics di admin dashboard'),
    ('admin_ui_enable_chat_widget', 'true', 'Tampilkan OpenRouter AI chat di admin'),
    ('admin_ui_enable_quick_stats', 'true', 'Tampilkan quick stats widgets'),
    ('admin_ui_enable_order_timeline', 'true', 'Tampilkan order timeline'),
    ('admin_ui_show_product_metrics', 'true', 'Tampilkan product performance metrics'),
    ('admin_ui_sidebar_collapsed', 'false', 'Sidebar default collapsed'),
    ('admin_ui_dark_mode', 'false', 'Aktifkan dark mode (future)'),
    ('admin_ui_items_per_page', '25', 'Items per page di list admin'),
    ('admin_ui_enable_bulk_actions', 'true', 'Aktifkan bulk actions di tables'),
    ('admin_ui_show_changelog', 'true', 'Tampilkan changelog di dashboard'),
    ('admin_dashboard_default_filter', 'all', 'Filter default dashboard'),
    ('admin_notif_system_alerts', 'true', 'Notifikasi system alerts di admin'),
    ('admin_notif_user_messages', 'true', 'Notifikasi user messages'),
    ('admin_notif_payment_issues', 'true', 'Notifikasi payment issues'),
    ('admin_notif_low_stock', 'true', 'Notifikasi stok produk habis'),
    ('admin_notif_chat_messages', 'true', 'Notifikasi chat messages dari pelanggan'),
    ('telegram_bot_token', '', 'Token bot Telegram'),
    ('telegram_chat_id', '', 'ID chat Telegram'),
    ('telegram_api_id', '', 'API ID Telegram (MTProto)'),
    ('telegram_api_hash', '', 'API Hash Telegram (MTProto)'),
    ('telegram_phone', '', 'Nomor telepon akun Telegram'),
    ('telegram_session_string', '', 'Session string Pyrogram'),
    ('telegram_group_link', '', 'Link grup Telegram'),
    ('whatsapp_phone_number_id', '', 'Phone Number ID WhatsApp Business'),
    ('whatsapp_access_token', '', 'Access Token WhatsApp Business'),
    ('whatsapp_recipient_number', '', 'Nomor admin WA penerima notif'),
    ('openrouter_base_url', 'https://openrouter.ai/api/v1', 'Base URL OpenRouter API'),
    ('openrouter_api_key', '', 'API Key OpenRouter (Bearer token)'),
    ('openrouter_model', 'openrouter/free', 'Model default AI'),
    ('openrouter_site_url', '', 'Site URL untuk header HTTP-Referer'),
    ('openrouter_app_name', 'ATLANTIS CELULAR', 'App Name untuk header X-Title'),
    ('openrouter_webhook_secret', '', 'Webhook secret verifikasi'),
    ('ai_system_prompt', 'Anda adalah asisten AI untuk ATLANTIS CELULAR, jawab setiap pertanyaan dengan ramah, cerdas, profesional, berikan jawaban produk hanya yang ada di website https://pusatgadaiidn.pythonanywhere.com.', 'System Prompt AI'),
    ('openrouter_enabled', 'true', 'Aktifkan OpenRouter AI'),
    ('openrouter_notify_new_order', 'false', 'Notifikasi pesanan baru via OpenRouter'),
    ('openrouter_notify_status_update', 'false', 'Notifikasi status via OpenRouter'),
    ('openrouter_notify_product_update', 'false', 'Notifikasi produk via OpenRouter'),
    ('ai_checkout_prompt', '', 'Custom prompt untuk halaman checkout'),
    ('ai_cart_prompt', '', 'Custom prompt untuk halaman cart'),
    ('ai_product_prompt', '', 'Custom prompt untuk halaman produk'),
    ('ai_profile_prompt', '', 'Custom prompt untuk halaman profile'),
    ('ai_orders_prompt', '', 'Custom prompt untuk halaman orders'),
    ('ai_admin_dashboard_prompt', '', 'Custom prompt untuk admin dashboard'),
    ('ai_admin_orders_prompt', '', 'Custom prompt untuk admin orders'),
    ('ai_admin_products_prompt', '', 'Custom prompt untuk admin products'),
    ('bank_name', '', 'Nama bank utama'),
    ('bank_account_number', '', 'Nomor rekening utama'),
    ('bank_account_holder', '', 'Atas nama rekening utama'),
    ('ewallet_name', '', 'Nama e-wallet'),
    ('qris_code', '', 'Filename gambar QRIS'),
    ('bank_accounts_json', '[]', 'Daftar rekening bank (JSON array)'),
    ('bank_transfer_instructions', '', 'Instruksi transfer bank'),
    ('payment_deadline_hours', '24', 'Batas waktu pembayaran (jam)'),
    ('payment_bank_transfer_enabled', 'true', 'Aktifkan transfer bank'),
    ('payment_qris_enabled', 'false', 'Aktifkan QRIS'),
    ('payment_cod_enabled', 'false', 'Aktifkan COD'),
    ('cod_fee', '0', 'Biaya tambahan COD (dalam RUPIAH)'),
    ('cod_max_order', '0', 'Maksimal nilai pesanan COD (0 = tak terbatas, dalam RUPIAH)'),
    ('cod_areas', '', 'Area yang mendukung COD'),
    ('shipping_enabled', 'true', 'Aktifkan form pengiriman'),
    ('shipping_couriers', 'JNE,J&T,SiCepat,AnterAja,POS Indonesia,TIKI', 'Daftar kurir tersedia'),
    ('shipping_default_cost', '0', 'Biaya pengiriman default (RUPIAH)'),
    ('shipping_free_threshold', '0', 'Minimal belanja gratis ongkir (0 = nonaktif, dalam RUPIAH)'),
    ('shipping_provinces', '', 'Daftar provinsi tujuan (kosong = semua)'),
    ('free_shipping_enabled', 'false', 'Aktifkan gratis ongkir otomatis'),
    ('free_shipping_min', '500000', 'Minimal pembelian gratis ongkir (RUPIAH)'),
    ('packaging_cost', '0', 'Biaya kemasan tambahan (RUPIAH)'),
    ('insurance_mode', 'none', 'Mode asuransi: none/optional/mandatory'),
    ('courier_jne_active', 'true', 'Aktifkan JNE'),
    ('courier_jnt_active', 'true', 'Aktifkan J&T Express'),
    ('courier_sicepat_active', 'true', 'Aktifkan SiCepat'),
    ('courier_anteraja_active', 'true', 'Aktifkan AnterAja'),
    ('courier_pos_active', 'false', 'Aktifkan Pos Indonesia'),
    ('courier_tiki_active', 'false', 'Aktifkan TIKI'),
    ('wa_notif_new_order', 'true', 'WA notif pesanan baru'),
    ('wa_notif_payment_received', 'true', 'WA notif pembayaran diterima'),
    ('wa_notif_order_shipped', 'true', 'WA notif pesanan dikirim'),
    ('wa_notif_order_done', 'true', 'WA notif pesanan selesai'),
    ('wa_notif_order_cancelled', 'false', 'WA notif pesanan dibatalkan'),
    ('email_notif_new_order', 'false', 'Email notif pesanan baru ke admin'),
    ('email_notif_to_buyer', 'true', 'Email konfirmasi ke pembeli'),
    ('email_notif_shipped', 'true', 'Email notif dikirim ke pembeli'),
    ('warehouse_street', 'Jl. Siliwangi, Kahuripan', 'Jalan dan nomor gudang'),
    ('warehouse_district', 'Tawang', 'Kecamatan gudang'),
    ('warehouse_city', 'Tasikmalaya', 'Kota gudang'),
    ('warehouse_province', 'Jawa Barat', 'Provinsi gudang'),
    ('warehouse_postal_code', '46115', 'Kode pos gudang'),
]

AI_CONFIG_PRESETS = {
    'openrouter/free': {'description': 'Gratis — cocok untuk testing', 'recommended': True},
    'anthropic/claude-3.5-sonnet': {'description': 'Premium — akurat & cepat', 'recommended': False},
    'openai/gpt-4o': {'description': 'Premium — multimodal', 'recommended': False},
    'google/gemini-2.0-flash-exp:free': {'description': 'Gratis — cepat untuk task sederhana', 'recommended': True}
}


# ═══════════════════════════════════════════════════════════════════════════════
# STEP WRAPPER — Track timing & metrics per step
# ═══════════════════════════════════════════════════════════════════════════════

def timed_step(name: str):
    """Decorator: track execution time and success/failure per migration step."""
    def decorator(func):
        @wraps(func)
        def wrapper(cursor, *args, **kwargs):
            step_start = time.time()
            step_info = {'name': name, 'start': datetime.now().isoformat(), 'status': 'running'}
            METRICS.steps_total += 1
            try:
                result = func(cursor, *args, **kwargs)
                step_info['status'] = 'success'
                METRICS.steps_success += 1
                return result
            except Exception as e:
                step_info['status'] = 'failed'
                step_info['error'] = str(e)
                METRICS.steps_failed += 1
                LOGGER.error(f"Step {name} failed: {e}")
                raise
            finally:
                step_info['duration'] = time.time() - step_start
                METRICS.step_details.append(step_info)
        return wrapper
    return decorator


# ═══════════════════════════════════════════════════════════════════════════════
# MIGRATION FUNCTIONS (wrapped with timed_step)
# ═══════════════════════════════════════════════════════════════════════════════

@timed_step('Admin')
def migrate_admin(cursor):
    print_section("TABEL ADMIN")
    if not table_exists(cursor, 'admin'):
        ddl = translate_ddl_for_postgres(TABLES['admin']) if DB_TYPE == 'postgres' else TABLES['admin']
        if MIGRATION_DRY_RUN:
            print_info("[DRY-RUN] Would create table: admin")
        else:
            execute_sql(cursor, ddl)
            print_success("Tabel admin dibuat")
            METRICS.tables_created += 1

    cursor.execute("SELECT COUNT(*) FROM admin")
    if cursor.fetchone()[0] == 0:
        if MIGRATION_DRY_RUN:
            print_info("[DRY-RUN] Would insert default admin")
        else:
            default_hash = generate_password_hash('admin123')
            cursor.execute("INSERT INTO admin (username, password_hash) VALUES (?, ?)", ('admin', default_hash))
            print_success("Admin default dibuat (admin / admin123)")
    else:
        print_info("Admin sudah ada, skip")


@timed_step('User')
def migrate_user(cursor):
    print_section("TABEL USER")
    active_name = ensure_table_name(cursor, 'user', 'users')

    if not table_exists(cursor, active_name):
        ddl = translate_ddl_for_postgres(TABLES['user']) if DB_TYPE == 'postgres' else TABLES['user']
        if MIGRATION_DRY_RUN:
            print_info(f"[DRY-RUN] Would create table: {active_name}")
        else:
            execute_sql(cursor, ddl)
            print_success(f"Tabel {active_name} dibuat")
            METRICS.tables_created += 1
    else:
        added = 0
        for col, col_def in MIGRATION_COLUMNS['user'].items():
            if safe_add_column(cursor, active_name, col, col_def):
                added += 1
        if added > 0:
            print_success(f"{added} kolom ditambahkan ke {active_name}")
            METRICS.columns_added += added
        else:
            print_info(f"Semua kolom {active_name} sudah ada")

        if not MIGRATION_DRY_RUN:
            cursor.execute(f'UPDATE "{active_name}" SET username = \'username_\' || id WHERE username IS NULL')
            if cursor.rowcount > 0:
                print_info(f"{cursor.rowcount} username diperbaiki")
            try:
                cursor.execute(f'CREATE UNIQUE INDEX IF NOT EXISTS idx_user_username ON "{active_name}"(username)')
            except sqlite3.OperationalError:
                pass


@timed_step('Product')
def migrate_product(cursor):
    print_section("TABEL PRODUCT")
    if not table_exists(cursor, 'product'):
        ddl = translate_ddl_for_postgres(TABLES['product']) if DB_TYPE == 'postgres' else TABLES['product']
        if MIGRATION_DRY_RUN:
            print_info("[DRY-RUN] Would create table: product")
        else:
            execute_sql(cursor, ddl)
            print_success("Tabel product dibuat")
            METRICS.tables_created += 1
    else:
        added = 0
        for col, col_def in MIGRATION_COLUMNS['product'].items():
            if safe_add_column(cursor, 'product', col, col_def):
                added += 1
        if added > 0:
            print_success(f"{added} kolom ditambahkan ke product")
            METRICS.columns_added += added
        else:
            print_info("Semua kolom product sudah ada")


@timed_step('Order')
def migrate_order(cursor):
    print_section("TABEL ORDER")
    # FIXED: nama tabel aktif di models.py adalah bentuk PLURAL 'orders'
    # (untuk menghindari 'order' sebagai reserved word SQL). Argumen
    # ensure_table_name() sebelumnya terbalik (preferred='order', fallback=
    # 'orders'), yang akan me-RENAME tabel 'orders' yang benar menjadi
    # 'order' setiap kali migrasi jalan -- lalu langsung crash dengan
    # KeyError karena TABLES/MIGRATION_COLUMNS memakai key 'orders', bukan
    # 'order'. Savepoint rollback menyelamatkan data dari rename yang salah,
    # tapi akibatnya tabel order TIDAK PERNAH ter-migrasi sama sekali.
    active_name = ensure_table_name(cursor, 'orders', 'order')

    if not table_exists(cursor, active_name):
        ddl = translate_ddl_for_postgres(TABLES['orders']) if DB_TYPE == 'postgres' else TABLES['orders']
        if MIGRATION_DRY_RUN:
            print_info(f"[DRY-RUN] Would create table: {active_name}")
        else:
            execute_sql(cursor, ddl)
            print_success(f"Tabel {active_name} dibuat")
            METRICS.tables_created += 1
    else:
        added = 0
        for col, col_def in MIGRATION_COLUMNS['orders'].items():
            if safe_add_column(cursor, active_name, col, col_def):
                added += 1
        if added > 0:
            print_success(f"{added} kolom ditambahkan ke {active_name}")
            METRICS.columns_added += added
        else:
            print_info(f"Semua kolom {active_name} sudah ada")


@timed_step('OrderItem')
def migrate_order_item(cursor):
    print_section("TABEL ORDER ITEM")
    # FIXED: sama seperti di atas -- nama tabel aktif adalah 'order_items'
    # (plural). Argumen ensure_table_name() sebelumnya terbalik.
    active_name = ensure_table_name(cursor, 'order_items', 'order_item')

    if not table_exists(cursor, active_name):
        ddl = translate_ddl_for_postgres(TABLES['order_items']) if DB_TYPE == 'postgres' else TABLES['order_items']
        if MIGRATION_DRY_RUN:
            print_info(f"[DRY-RUN] Would create table: {active_name}")
        else:
            execute_sql(cursor, ddl)
            print_success(f"Tabel {active_name} dibuat")
            METRICS.tables_created += 1
    else:
        added = 0
        for col, col_def in MIGRATION_COLUMNS['order_items'].items():
            if safe_add_column(cursor, active_name, col, col_def):
                added += 1
        if added > 0:
            print_success(f"{added} kolom ditambahkan ke {active_name}")
            METRICS.columns_added += added
        else:
            print_info(f"Semua kolom {active_name} sudah ada")


@timed_step('Testimonial')
def migrate_testimonial(cursor):
    print_section("TABEL TESTIMONIAL")
    if not table_exists(cursor, 'testimonial'):
        ddl = translate_ddl_for_postgres(TABLES['testimonial']) if DB_TYPE == 'postgres' else TABLES['testimonial']
        if MIGRATION_DRY_RUN:
            print_info("[DRY-RUN] Would create table: testimonial")
        else:
            execute_sql(cursor, ddl)
            print_success("Tabel testimonial dibuat")
            METRICS.tables_created += 1
    else:
        added = 0
        for col, col_def in MIGRATION_COLUMNS['testimonial'].items():
            if safe_add_column(cursor, 'testimonial', col, col_def):
                added += 1
        if added > 0:
            print_success(f"{added} kolom ditambahkan ke testimonial")
            METRICS.columns_added += added
        else:
            print_info("Semua kolom testimonial sudah ada")


@timed_step('HeroSlide')
def migrate_hero_slide(cursor):
    print_section("TABEL HERO SLIDE")
    if not table_exists(cursor, 'hero_slide'):
        ddl = translate_ddl_for_postgres(TABLES['hero_slide']) if DB_TYPE == 'postgres' else TABLES['hero_slide']
        if MIGRATION_DRY_RUN:
            print_info("[DRY-RUN] Would create table: hero_slide")
        else:
            execute_sql(cursor, ddl)
            print_success("Tabel hero_slide dibuat")
            METRICS.tables_created += 1
    else:
        added = 0
        for col, col_def in MIGRATION_COLUMNS['hero_slide'].items():
            if safe_add_column(cursor, 'hero_slide', col, col_def):
                added += 1
        if added > 0:
            print_success(f"{added} kolom ditambahkan ke hero_slide")
            METRICS.columns_added += added
        else:
            print_info("Semua kolom hero_slide sudah ada")


@timed_step('Setting')
def migrate_setting(cursor):
    print_section("TABEL SETTING")
    if not table_exists(cursor, 'setting'):
        ddl = translate_ddl_for_postgres(TABLES['setting']) if DB_TYPE == 'postgres' else TABLES['setting']
        if MIGRATION_DRY_RUN:
            print_info("[DRY-RUN] Would create table: setting")
        else:
            execute_sql(cursor, ddl)
            print_success("Tabel setting dibuat")
            METRICS.tables_created += 1
    else:
        added = 0
        for col, col_def in MIGRATION_COLUMNS['setting'].items():
            if safe_add_column(cursor, 'setting', col, col_def):
                added += 1
        if added > 0:
            print_success(f"{added} kolom ditambahkan ke setting")
            METRICS.columns_added += added
        else:
            print_info("Semua kolom setting sudah ada")


@timed_step('Shipping')
def migrate_shipping_tables(cursor):
    print_section("SHIPPING TRACKING")
    # FIXED: TABLES dict declares these plural ('shipping_tracking_events',
    # 'shipping_issues') to match the actual CREATE TABLE statements and
    # models.py __tablename__ values. The old singular keys here
    # ('shipping_tracking_event', 'shipping_issue') don't exist in TABLES
    # at all, so this raised a KeyError on every single migration run and
    # none of the shipping tables (including shipping_tracking and
    # shipping_carrier_performance, which never even got reached) were
    # ever created.
    tables_to_create = [
        ('shipping_tracking', TABLES['shipping_tracking']),
        ('shipping_tracking_events', TABLES['shipping_tracking_events']),
        ('shipping_issues', TABLES['shipping_issues']),
        ('shipping_carrier_performance', TABLES['shipping_carrier_performance']),
    ]
    created = 0
    for name, ddl in tables_to_create:
        if not table_exists(cursor, name):
            ddl_exec = translate_ddl_for_postgres(ddl) if DB_TYPE == 'postgres' else ddl
            if MIGRATION_DRY_RUN:
                print_info(f"[DRY-RUN] Would create table: {name}")
            else:
                execute_sql(cursor, ddl_exec)
                print_success(f"Tabel {name} dibuat")
                created += 1
        else:
            print_info(f"Tabel {name} sudah ada")
    METRICS.tables_created += created
    if created == 0:
        print_info("Semua tabel shipping tracking sudah ada")


@timed_step('Address')
def migrate_address_table(cursor):
    print_section("ADDRESS TABLE")
    if not table_exists(cursor, 'address'):
        ddl = translate_ddl_for_postgres(TABLES['address']) if DB_TYPE == 'postgres' else TABLES['address']
        if MIGRATION_DRY_RUN:
            print_info("[DRY-RUN] Would create table: address")
        else:
            execute_sql(cursor, ddl)
            print_success("Tabel address dibuat")
            METRICS.tables_created += 1
            cursor.execute("CREATE INDEX IF NOT EXISTS idx_address_user_id ON address(user_id)")
            cursor.execute("CREATE INDEX IF NOT EXISTS idx_address_default ON address(is_default)")
            print_success("Indexes address dibuat")
            METRICS.indexes_created += 2
    else:
        print_info("Tabel address sudah ada")
        added = 0
        for col, col_def in MIGRATION_COLUMNS.get('address', {}).items():
            if col not in ['id', 'user_id', 'created_at', 'updated_at']:
                if safe_add_column(cursor, 'address', col, col_def):
                    added += 1
        if added > 0:
            print_success(f"{added} kolom ditambahkan ke address")
            METRICS.columns_added += added
        else:
            print_info("Semua kolom address sudah ada")


@timed_step('ExtraTables')
def migrate_extra_tables(cursor):
    """FIXED: tabel-tabel berikut sudah didefinisikan di TABLES (dan aktif
    dipakai app.py/models.py: AI config, cicilan/kredit + KYC, riwayat chat
    AI + memori pelanggan) tapi TIDAK ADA satupun migrate_* function yang
    membuatnya. Sebelumnya tabel-tabel ini hanya ada kalau app.py sempat
    menjalankan db.create_all() duluan -- di instalasi baru yang migrasinya
    dijalankan lebih dulu, tabel ini akan hilang total dan
    post_migration_health_check() akan selalu melaporkan 'Missing table'.
    Loop ini idempotent: tabel yang sudah ada hanya dilewati, tidak dibuat
    ulang."""
    print_section("TABEL AI / CICILAN / KYC / CHAT (v3.8.0)")
    extra_table_names = [
        'ai_config',
        'installment_plans',
        'kyc_verification_sessions',
        'installment_applications',
        'installment_schedules',
        'installment_payments',
        'chat_conversations',
        'chat_messages',
        'customer_memory_facts',
    ]
    created = 0
    for name in extra_table_names:
        if not table_exists(cursor, name):
            ddl = translate_ddl_for_postgres(TABLES[name]) if DB_TYPE == 'postgres' else TABLES[name]
            if MIGRATION_DRY_RUN:
                print_info(f"[DRY-RUN] Would create table: {name}")
            else:
                execute_sql(cursor, ddl)
                print_success(f"Tabel {name} dibuat")
                created += 1
        else:
            print_info(f"Tabel {name} sudah ada")
    METRICS.tables_created += created
    if created == 0:
        print_info("Semua tabel AI/cicilan/KYC/chat sudah ada")


@timed_step('Indexes')
def migrate_indexes(cursor):
    print_section("INDEXES & OPTIMIZATION")
    idx_count = 0
    for idx_name, table, column in INDEXES:
        try:
            if MIGRATION_DRY_RUN:
                if not index_exists(cursor, idx_name):
                    print_info(f"[DRY-RUN] Would create index: {idx_name}")
                    idx_count += 1
            else:
                cursor.execute(f'CREATE INDEX IF NOT EXISTS {idx_name} ON "{table}" ({column})')
                idx_count += 1
        except sqlite3.OperationalError as e:
            print_warn(f"Index {idx_name}: {e}")
    if not MIGRATION_DRY_RUN:
        cursor.execute("ANALYZE")
    print_success(f"{idx_count} indeks dipastikan aktif")
    METRICS.indexes_created += idx_count


@timed_step('Settings')
def migrate_default_settings(cursor):
    print_section("DEFAULT SETTINGS")
    added_count = 0
    updated_desc_count = 0
    for key, value, desc in DEFAULT_SETTINGS:
        if add_setting(cursor, key, value, desc):
            added_count += 1
        if update_setting_desc(cursor, key, desc):
            updated_desc_count += 1

    if not MIGRATION_DRY_RUN:
        cursor.execute('SELECT value FROM setting WHERE "key"=\'free_shipping_min\'')
        row = cursor.fetchone()
        fsm = row[0] if row else '0'
        cursor.execute('SELECT value FROM setting WHERE "key"=\'shipping_free_threshold\'')
        row2 = cursor.fetchone()
        sft = row2[0] if row2 else '0'
        if fsm and (not sft or sft == '0'):
            update_setting(cursor, 'shipping_free_threshold', fsm)
            print_success(f"shipping_free_threshold disinkronkan dengan free_shipping_min ({fsm})")

    METRICS.settings_added += added_count
    METRICS.settings_updated += updated_desc_count
    if added_count > 0:
        print_success(f"{added_count} setting baru ditambahkan")
    if updated_desc_count > 0:
        print_info(f"{updated_desc_count} deskripsi setting diperbarui")
    if added_count == 0 and updated_desc_count == 0:
        print_info("Semua setting default sudah aktif")


@timed_step('AI Config')
def validate_ai_config(cursor):
    print_section("VALIDASI AI CONFIG")
    ai_defaults = [
        ('openrouter_base_url', 'https://openrouter.ai/api/v1'),
        ('openrouter_model', 'openrouter/free'),
        ('ai_system_prompt', 'Anda adalah asisten AI untuk ATLANTIS CELULAR, jawab setiap pertanyaan dengan ramah, cerdas, profesional, berikan jawaban produk hanya yang ada di website https://pusatgadaiidn.pythonanywhere.com.'),
    ]
    for key, default_val in ai_defaults:
        execute_sql(cursor, 'SELECT value FROM setting WHERE "key"=?', (key,))
        row = cursor.fetchone()
        if not row or not row[0]:
            if not MIGRATION_DRY_RUN:
                execute_sql(cursor, 'UPDATE setting SET value=? WHERE "key"=?', (default_val, key))
            print_success(f"{key} di-reset ke default")
    print_info("Validasi AI config selesai")


@timed_step('Orphans')
def handle_orphan_addresses(cursor):
    print_section("ORPHAN ADDRESS HANDLING")
    if MIGRATION_PRESERVE_ORPHANS:
        return preserve_orphan_addresses(cursor)
    else:
        return cleanup_orphan_addresses(cursor)


def cleanup_orphan_addresses(cursor):
    try:
        execute_sql(cursor, 'SELECT id, user_id, label FROM address WHERE user_id NOT IN (SELECT id FROM "user") ORDER BY id')
        orphans = cursor.fetchall()
        orphan_count = len(orphans) if orphans else 0
        if orphan_count == 0:
            print_success("Tidak ada alamat yatim ditemukan")
            return 0
        print_warn(f"{orphan_count} alamat yatim ditemukan — akan dihapus:")
        for orphan in orphans:
            addr_id, user_id, label = orphan
            print(f"   • ID {addr_id}: user_id={user_id}, label='{label}'")
        if not MIGRATION_DRY_RUN:
            execute_sql(cursor, 'DELETE FROM address WHERE user_id NOT IN (SELECT id FROM "user")')
            deleted = cursor.rowcount
        else:
            deleted = orphan_count
            print_info("[DRY-RUN] Would delete orphan addresses")
        print_success(f"{deleted} baris address dihapus")
        METRICS.orphans_cleaned += deleted
        return deleted
    except Exception as e:
        print_error(f"Gagal membersihkan orphan addresses: {e}")
        LOGGER.error(f"Cleanup orphans error: {e}")
        return 0


def get_or_create_placeholder_user(cursor, username=None, email=None):
    username = username or os.environ.get('ORPHAN_USER_USERNAME', 'orphan_address_holder')
    email = email or os.environ.get('ORPHAN_USER_EMAIL', 'orphan_addresses@local')
    execute_sql(cursor, 'SELECT id FROM "user" WHERE username=? OR email=? LIMIT 1', (username, email))
    row = cursor.fetchone()
    if row:
        return row[0]
    pw = generate_password_hash(os.environ.get('ORPHAN_USER_PASSWORD', 'placeholder'))
    try:
        if MIGRATION_DRY_RUN:
            print_info(f"[DRY-RUN] Would create placeholder user: {username}")
            return -1
        execute_sql(cursor, 'INSERT INTO "user" (email, username, full_name, password_hash, is_active) VALUES (?, ?, ?, ?, ?)',
                    (email, username, 'Orphan Address Placeholder', pw, 0))
        execute_sql(cursor, 'SELECT id FROM "user" WHERE username=? LIMIT 1', (username,))
        row = cursor.fetchone()
        return row[0] if row else None
    except Exception as e:
        print_error(f"Gagal membuat placeholder user: {e}")
        return None


def preserve_orphan_addresses(cursor):
    try:
        execute_sql(cursor, 'SELECT id, user_id, label FROM address WHERE user_id NOT IN (SELECT id FROM "user") ORDER BY id')
        orphans = cursor.fetchall()
        orphan_count = len(orphans) if orphans else 0
        if orphan_count == 0:
            print_success("Tidak ada alamat yatim ditemukan")
            return 0
        print_warn(f"{orphan_count} alamat yatim ditemukan — akan di-preserve:")
        for orphan in orphans:
            addr_id, user_id, label = orphan
            print(f"   • ID {addr_id}: user_id={user_id}, label='{label}'")
        placeholder_id = get_or_create_placeholder_user(cursor)
        if not placeholder_id:
            print_error('Gagal memperoleh placeholder user — membatalkan preserve')
            return 0
        if not MIGRATION_DRY_RUN:
            execute_sql(cursor, 'UPDATE address SET user_id = ? WHERE user_id NOT IN (SELECT id FROM "user")', (placeholder_id,))
            updated = cursor.rowcount
        else:
            updated = orphan_count
            print_info("[DRY-RUN] Would link orphan addresses to placeholder user")
        print_success(f"{updated} alamat di-link ke user placeholder (id={placeholder_id})")
        METRICS.orphans_preserved += updated
        return updated
    except Exception as e:
        print_error(f"Gagal preserve orphan addresses: {e}")
        LOGGER.error(f"Preserve orphans error: {e}")
        return 0


# ═══════════════════════════════════════════════════════════════════════════════
# SUMMARY PRINTERS
# ═══════════════════════════════════════════════════════════════════════════════

def print_ai_summary(cursor):
    print_section("RINGKASAN AI CONFIG")
    ai_keys = ['openrouter_base_url', 'openrouter_model', 'openrouter_site_url', 'openrouter_app_name', 'ai_system_prompt']
    for key in ai_keys:
        execute_sql(cursor, 'SELECT value FROM setting WHERE "key"=?', (key,))
        row = cursor.fetchone()
        val = row[0] if row else '(kosong)'
        if 'api_key' in key and val:
            val = val[:8] + '...' + val[-4:] if len(val) > 12 else '***'
        display = val[:50] + '...' if len(val) > 50 else val
        print_info(f"{key}: {display}")
    execute_sql(cursor, 'SELECT value FROM setting WHERE "key"=\'openrouter_api_key\'')
    row = cursor.fetchone()
    if row and row[0] and len(row[0]) > 10:
        print_success("API Key terdeteksi (terkonfigurasi)")
    else:
        print_warn("API Key belum di-set — AI chat akan menggunakan fallback")


def print_warehouse_config(cursor):
    print_section("WAREHOUSE ADDRESS")
    warehouse_fields = [
        ('warehouse_street', 'Alamat Jalan'),
        ('warehouse_district', 'Kecamatan'),
        ('warehouse_city', 'Kota'),
        ('warehouse_province', 'Provinsi'),
        ('warehouse_postal_code', 'Kode Pos')
    ]
    for key, label in warehouse_fields:
        execute_sql(cursor, 'SELECT value FROM setting WHERE "key"=?', (key,))
        row = cursor.fetchone()
        val = row[0] if row else '(tidak di-set)'
        print_info(f"{label}: {val}")
    print_success("Warehouse address configured")


def print_status_flow_summary():
    print_section("STATUS FLOW DOCUMENTATION")
    print_info("Order Status Flow (8 stages):")
    print("  Pending → Paid → Processing → Ready_to_Ship → Shipped → Out_for_Delivery → Received → Completed")
    print("  Terminal: Cancelled, Returned")
    print()
    print_info("Shipping Tracking Status Flow (8 stages):")
    print("  Pending_Pickup → Picked_Up → In_Transit → Out_for_Delivery → Delivered → Received → Completed → Failed_Delivery")
    print()
    print_info("Auto-sync: Delivered→Out_for_Delivery | Received→Received | Completed→Completed")
    print_success("All status updates logged to shipping_tracking_event")


def print_metrics_summary():
    print_section("MIGRATION METRICS")
    print_info(f"Version:        {METRICS.version}")
    print_info(f"DB Type:        {METRICS.db_type}")
    print_info(f"Duration:       {METRICS.duration_seconds:.2f}s")
    print_info(f"Steps:          {METRICS.steps_success}/{METRICS.steps_total} success ({METRICS.steps_failed} failed)")
    print_info(f"Tables created: {METRICS.tables_created}")
    print_info(f"Columns added:  {METRICS.columns_added}")
    print_info(f"Indexes created:{METRICS.indexes_created}")
    print_info(f"Settings added: {METRICS.settings_added}")
    print_info(f"Settings updated:{METRICS.settings_updated}")
    print_info(f"Orphans cleaned:{METRICS.orphans_cleaned}")
    print_info(f"Orphans preserved:{METRICS.orphans_preserved}")
    if METRICS.errors:
        print(Colors.YELLOW + f"   ⚠️  Errors:         {len(METRICS.errors)}" + Colors.END)
    if METRICS.warnings:
        print(Colors.YELLOW + f"   ⚠️  Warnings:       {len(METRICS.warnings)}" + Colors.END)
    if METRICS.circuit_breaker_tripped:
        print(Colors.YELLOW + "   ⚠️  Circuit breaker was tripped during execution" + Colors.END)
    if METRICS.dry_run:
        print(Colors.YELLOW + "   ⚠️  DRY-RUN MODE — No changes were written to database" + Colors.END)


# ═══════════════════════════════════════════════════════════════════════════════
# MAIN MIGRATION RUNNER — 2026 PRO EDITION
# ═══════════════════════════════════════════════════════════════════════════════

def run_migration():
    global _shutdown_requested
    start_time = time.time()

    # ── Parse CLI flags ──
    if '--dry-run' in sys.argv:
        global MIGRATION_DRY_RUN
        MIGRATION_DRY_RUN = True
        METRICS.dry_run = True
    if '--preserve-orphans' in sys.argv:
        global MIGRATION_PRESERVE_ORPHANS
        MIGRATION_PRESERVE_ORPHANS = True
    if '--verbose' in sys.argv:
        global MIGRATION_VERBOSE
        MIGRATION_VERBOSE = True

    os.makedirs('instance', exist_ok=True)

    # ── Banner ──
    print(Colors.HEADER)
    print("╔" + "═" * 76 + "╗")
    mode_str = "DRY-RUN" if MIGRATION_DRY_RUN else ("Preserve Orphans" if MIGRATION_PRESERVE_ORPHANS else "Cleanup Orphans")
    print("║" + " " * 14 + "🔧 MIGRASI DATABASE 2026 PRO v" + MIGRATION_VERSION + " " * 22 + "║")
    print("║" + " " * 76 + "║")
    print("║   Lokasi:  " + DB_PATH.ljust(64) + "║")
    print("║   Waktu:   " + datetime.now().strftime("%Y-%m-%d %H:%M:%S").ljust(64) + "║")
    print("║   Backup:  " + BACKUP_DIR.ljust(64) + "║")
    print("║   Mode:    " + mode_str.ljust(64) + "║")
    print("║   Pro:     Circuit Breaker | Retry | Rate Limit | Metrics | Lock" + " " * 12 + "║")
    print("╚" + "═" * 76 + "╝")
    print(Colors.END)

    # ── Rate Limit Check ──
    if not check_rate_limit():
        print_error("Rate limit exceeded — migration blocked")
        return 1

    # ── Acquire Lock ──
    if not LOCK.acquire():
        print_error("Cannot acquire migration lock — another instance running?")
        return 1

    # ── Pre-Migration Health Check ──
    print_section("PRE-MIGRATION HEALTH CHECK")
    healthy, issues = pre_migration_health_check()
    if not healthy:
        for issue in issues:
            print_error(issue)
            LOGGER.error(f"Health check failed: {issue}")
        print_error("Pre-migration health check FAILED — aborting")
        LOCK.release()
        return 1
    print_success("Health check passed")

    # ── Backup ──
    if not os.path.exists(DB_PATH):
        print_info("Database belum ada, akan dibuat baru")
    else:
        print_info("Database ditemukan, membuat backup...")
        try:
            METRICS.checksum_before = get_db_checksum(DB_PATH)
            backup_path = create_backup(DB_PATH)
            if backup_path:
                METRICS.backup_path = backup_path
                print_success(f"Backup dibuat: {os.path.basename(backup_path)}")
            else:
                print_warn("Gagal membuat backup")
                METRICS.warnings.append("Backup creation failed")
        except Exception as e:
            print_warn(f"Backup error: {e}")
            METRICS.warnings.append(f"Backup failed: {str(e)}")

    # ── Schema Diff (Dry-Run Preview) ──
    if MIGRATION_DRY_RUN:
        print_section("SCHEMA DIFF PREVIEW")
        try:
            with get_db_connection() as conn:
                cursor = conn.cursor()
                diffs = generate_schema_diff(cursor)
                if diffs:
                    for d in diffs:
                        print_info(f"  → {d}")
                else:
                    print_info("No schema changes required")
        except Exception as e:
            print_warn(f"Schema diff failed: {e}")

    # ── Execute Migration ──
    try:
        with get_db_connection() as conn:
            cursor = conn.cursor()

            init_migration_tracking(cursor)

            last_migration = get_last_migration(cursor)
            if last_migration:
                print_info(f"Migrasi terakhir: v{last_migration[0]} pada {last_migration[1]}")

            if is_migration_applied(cursor, MIGRATION_VERSION) and not MIGRATION_DRY_RUN:
                print_warn(f"Migrasi v{MIGRATION_VERSION} sudah pernah dijalankan. Mode idempotent aktif.")

            # Schema steps (sequential for FK integrity)
            schema_steps = [
                ('Admin', migrate_admin), ('User', migrate_user), ('Product', migrate_product),
                ('Order', migrate_order), ('OrderItem', migrate_order_item), ('Testimonial', migrate_testimonial),
                ('HeroSlide', migrate_hero_slide), ('Setting', migrate_setting), ('Shipping', migrate_shipping_tables),
                ('Address', migrate_address_table), ('ExtraTables', migrate_extra_tables)
            ]

            for step_name, step_func in schema_steps:
                if _shutdown_requested:
                    print_warn("Shutdown requested — stopping migration")
                    break
                try:
                    with transaction_savepoint(conn, f"sp_{step_name.lower()}"):
                        step_func(cursor)
                except Exception as e:
                    LOGGER.error(f"Schema step {step_name} failed: {e}")
                    print_error(f"[ERR] {step_name}: {e}")
                    if MIGRATION_VERBOSE:
                        import traceback
                        traceback.print_exc()

            # Data & optimization steps
            data_steps = [
                ('Settings', migrate_default_settings), ('Indexes', migrate_indexes), ('AI Config', validate_ai_config),
                ('Orphans', handle_orphan_addresses)
            ]

            for step_name, step_func in data_steps:
                if _shutdown_requested:
                    break
                try:
                    with transaction_savepoint(conn, f"sp_{step_name.lower()}"):
                        step_func(cursor)
                except Exception as e:
                    LOGGER.error(f"Data step {step_name} failed: {e}")
                    print_error(f"[ERR] {step_name}: {e}")

            # Post-migration health check
            print_section("POST-MIGRATION HEALTH CHECK")
            post_healthy, post_issues = post_migration_health_check(cursor)
            if not post_healthy:
                for issue in post_issues:
                    print_warn(issue)
                    METRICS.warnings.append(issue)
            else:
                print_success("Post-migration health check passed")

            # Commit or rollback
            if not MIGRATION_DRY_RUN:
                conn.commit()
                METRICS.checksum_after = get_db_checksum(DB_PATH)

            elapsed = time.time() - start_time
            METRICS.duration_seconds = elapsed
            METRICS.ended_at = datetime.now().isoformat()

            status = 'success' if METRICS.steps_failed == 0 else 'warning'
            if not MIGRATION_DRY_RUN:
                log_migration(cursor, MIGRATION_VERSION, METRICS.checksum_after, elapsed, status,
                             f"Pro Edition: {METRICS.steps_success}/{METRICS.steps_total} steps OK")
                conn.commit()

            # Summaries
            print_ai_summary(cursor)
            print_warehouse_config(cursor)
            print_status_flow_summary()
            print_metrics_summary()

            if METRICS.errors:
                print_section("⚠️  ERRORS")
                for idx, err in enumerate(METRICS.errors, 1):
                    print(Colors.RED + f"   ❌ {idx}. {err}" + Colors.END)

            if METRICS.warnings:
                print_section("⚠️  WARNINGS")
                for idx, warn in enumerate(METRICS.warnings, 1):
                    print(Colors.YELLOW + f"   ⚠️  {idx}. {warn}" + Colors.END)

            print()
            if MIGRATION_DRY_RUN:
                print(Colors.YELLOW + "╔" + "═" * 76 + "╗" + Colors.END)
                print(Colors.YELLOW + "║" + " " * 20 + "🏁 DRY-RUN COMPLETE" + " " * 35 + "║" + Colors.END)
                print(Colors.YELLOW + "║" + " " * 76 + "║" + Colors.END)
                print(Colors.YELLOW + f"║   Waktu:     {elapsed:.2f}s" + " " * 55 + "║" + Colors.END)
                print(Colors.YELLOW + "║   No changes were written to the database." + " " * 33 + "║" + Colors.END)
                print(Colors.YELLOW + "╚" + "═" * 76 + "╝" + Colors.END)
            else:
                print(Colors.GREEN + "╔" + "═" * 76 + "╗" + Colors.END)
                print(Colors.GREEN + "║" + " " * 22 + "🎉 MIGRASI BERHASIL!" + " " * 33 + "║" + Colors.END)
                print(Colors.GREEN + "║" + " " * 76 + "║" + Colors.END)
                print(Colors.GREEN + f"║   Waktu:     {elapsed:.2f}s" + " " * 55 + "║" + Colors.END)
                print(Colors.GREEN + f"║   Database:  {DB_PATH}" + " " * (56 - len(DB_PATH)) + "║" + Colors.END)
                print(Colors.GREEN + f"║   Checksum:  {METRICS.checksum_after or METRICS.checksum_before}" + " " * (56 - len(METRICS.checksum_after or METRICS.checksum_before or '')) + "║" + Colors.END)
                print(Colors.GREEN + "╚" + "═" * 76 + "╝" + Colors.END)

            METRICS.save()
            LOCK.release()
            return 0

    except Exception as e:
        elapsed = time.time() - start_time
        METRICS.duration_seconds = elapsed
        METRICS.ended_at = datetime.now().isoformat()
        METRICS.errors.append(str(e))

        print()
        print(Colors.RED + "╔" + "═" * 76 + "╗" + Colors.END)
        print(Colors.RED + "║" + " " * 25 + "❌ MIGRASI GAGAL" + " " * 34 + "║" + Colors.END)
        print(Colors.RED + "╚" + "═" * 76 + "╝" + Colors.END)
        print_error(str(e))

        if METRICS.errors:
            print_section("ERROR LOG")
            for idx, err in enumerate(METRICS.errors, 1):
                print_error(f"  {idx}. {err}")

        if METRICS.step_details:
            print_section("STEP SUMMARY")
            for step in METRICS.step_details:
                status_icon = "✓" if step['status'] == 'success' else "✗"
                color = Colors.GREEN if step['status'] == 'success' else Colors.RED
                print(f"{color}   {status_icon} {step['name']}: {step['status']} ({step.get('duration', 0):.3f}s){Colors.END}")

        import traceback
        traceback.print_exc()

        try:
            with get_db_connection() as conn:
                cursor = conn.cursor()
                init_migration_tracking(cursor)
                try:
                    log_migration(cursor, MIGRATION_VERSION, None, elapsed, 'failed', str(e))
                except Exception as log_err:
                    LOGGER.warn(f"Failed to log migration error: {log_err}")
                conn.commit()
        except Exception:
            pass

        METRICS.save()
        LOCK.release()
        return 1


if __name__ == '__main__':
    sys.exit(run_migration())
