"""
Database Schema Definitions and Connection Manager for EventHub Portable
(TDE UP 2026 — Tent Decor Expo UP 2026)
═══════════════════════════════════════════════════════════════════════════

STEP 1 of 3 in the EventHub Portable roadmap:
    -> Step 1: schema.py        (THIS FILE — DB init & connection management)
    -> Step 2: sync_manager.py
    -> Step 3: server_hub.py

This module is the single source of truth for:
    • All SQLite (edge / kiosk / scanner) DDL, triggers, and indexes.
    • All MySQL (hub server) DDL and indexes.
    • The `DatabaseManager` class that every other module in this project
      (server_hub.py, sync_manager.py, register.py, check_in.py, explorer.py,
      photo_down.py) imports to safely get a database connection.

Core design principle for a 3-day, 10,000-attendee offline event:
    MySQL is allowed to go down, not be installed, or never have existed.
    SQLite is the reliable offline backup and must NEVER be blocked, locked,
    or crashed by a MySQL failure. Every MySQL-facing method in
    `DatabaseManager` degrades gracefully: it logs and returns None/False
    instead of raising, so the 8 phones scanning at the door never notice.
"""

import os
import sqlite3
import logging
import threading
from contextlib import contextmanager
from typing import Dict, List, Optional, Generator, Any

# ─────────────────────────────────────────────────────────────────────────
# MySQL driver is OPTIONAL at import time. If mysql-connector-python isn't
# installed, or the hub laptop simply doesn't have MySQL running, the whole
# app must still boot and run on SQLite alone.
# (Swapping to pymysql later only requires rewriting _init_mysql_pool() /
#  get_mysql_conn() — pymysql has no built-in pooling, so you'd manage a
#  simple list-based pool yourself.)
# ─────────────────────────────────────────────────────────────────────────
try:
    import mysql.connector
    from mysql.connector import pooling
    from mysql.connector.errors import Error as MySQLError
    MYSQL_DRIVER_AVAILABLE = True
except ImportError:  # pragma: no cover - environment dependent
    mysql = None            # type: ignore
    pooling = None           # type: ignore
    MySQLError = Exception   # type: ignore
    MYSQL_DRIVER_AVAILABLE = False


# ═══════════════════════════════════════════════════════════════════
#  LOGGING
# ═══════════════════════════════════════════════════════════════════
logger = logging.getLogger("eventhub.schema")
if not logger.handlers:
    _handler = logging.StreamHandler()
    _handler.setFormatter(logging.Formatter(
        "%(asctime)s [%(levelname)s] %(name)s: %(message)s"
    ))
    logger.addHandler(_handler)
    logger.setLevel(logging.INFO)


# ═══════════════════════════════════════════════════════════════════
#  SQLITE DDL & TRIGGERS (EDGE / KIOSK / SCANNER)
# ═══════════════════════════════════════════════════════════════════
_SQLITE_ATTENDEE_COLUMNS = """
    id                TEXT     NOT NULL PRIMARY KEY,
    attendee_id       TEXT     NOT NULL UNIQUE,
    full_name         TEXT     NOT NULL,
    mobile            TEXT     NOT NULL UNIQUE,
    email             TEXT,
    gender            TEXT     NOT NULL CHECK (gender IN ('MALE','FEMALE','OTHER')),
    attendee_type     TEXT     NOT NULL DEFAULT 'GENERAL'
                               CHECK (attendee_type IN ('GENERAL','BUSINESS','MEDIA','EXHIBITOR')),
    business_name     TEXT,
    business_category TEXT,
    other_category    TEXT,
    address           TEXT     NOT NULL,
    city              TEXT     NOT NULL,
    state             TEXT     NOT NULL,
    pincode           TEXT     NOT NULL,
    attendance_days   TEXT     NOT NULL DEFAULT '[]',
    photo_url         TEXT,
    created_at        DATETIME DEFAULT CURRENT_TIMESTAMP,
    updated_at        DATETIME DEFAULT CURRENT_TIMESTAMP,
    checkin_history   TEXT     NOT NULL DEFAULT '{}',
    needs_cloud_sync  INTEGER  NOT NULL DEFAULT 1 CHECK(needs_cloud_sync IN (0,1)),
    needs_sheet_sync  INTEGER  NOT NULL DEFAULT 0 CHECK(needs_sheet_sync IN (0,1)),
    local_modified    INTEGER  NOT NULL DEFAULT 0 CHECK(local_modified   IN (0,1)),
    device_name       TEXT,
    CHECK (length(mobile) >= 10),
    CHECK (attendee_type = 'GENERAL' OR (business_name IS NOT NULL AND trim(business_name) <> ''))
"""

_SQLITE_TRIGGERS = """
CREATE TRIGGER IF NOT EXISTS trg_attendees_updated_at
AFTER UPDATE ON attendees
FOR EACH ROW
BEGIN
    UPDATE attendees SET updated_at = CURRENT_TIMESTAMP WHERE id = NEW.id;
END;

CREATE TRIGGER IF NOT EXISTS trg_offline_kiosk_attendees_updated_at
AFTER UPDATE ON offline_kiosk_attendees
FOR EACH ROW
BEGIN
    UPDATE offline_kiosk_attendees SET updated_at = CURRENT_TIMESTAMP WHERE id = NEW.id;
END;
"""

DDL_SQLITE: str = f"""
PRAGMA foreign_keys = ON;

CREATE TABLE IF NOT EXISTS attendees               ({_SQLITE_ATTENDEE_COLUMNS});
CREATE TABLE IF NOT EXISTS offline_kiosk_attendees ({_SQLITE_ATTENDEE_COLUMNS});

CREATE TABLE IF NOT EXISTS sync_logs (
    id            INTEGER  PRIMARY KEY AUTOINCREMENT,
    table_name    TEXT     NOT NULL,
    record_id     TEXT     NOT NULL,
    sync_status   TEXT     NOT NULL,
    error_message TEXT,
    synced_at     DATETIME DEFAULT CURRENT_TIMESTAMP
);

CREATE TABLE IF NOT EXISTS sync_history (
    id         INTEGER  PRIMARY KEY AUTOINCREMENT,
    op_type    TEXT     NOT NULL,
    started_at DATETIME NOT NULL,
    ended_at   DATETIME,
    inserted   INTEGER  DEFAULT 0,
    updated    INTEGER  DEFAULT 0,
    skipped    INTEGER  DEFAULT 0,
    errors     INTEGER  DEFAULT 0,
    status     TEXT     NOT NULL DEFAULT 'RUNNING'
);

CREATE TABLE IF NOT EXISTS sync_meta (
    key   TEXT NOT NULL PRIMARY KEY,
    value TEXT NOT NULL
);

{_SQLITE_TRIGGERS}

CREATE INDEX IF NOT EXISTS idx_att_aid    ON attendees(attendee_id);
CREATE INDEX IF NOT EXISTS idx_att_upd    ON attendees(updated_at DESC);
CREATE INDEX IF NOT EXISTS idx_att_cloud  ON attendees(needs_cloud_sync);
CREATE INDEX IF NOT EXISTS idx_att_local  ON attendees(local_modified);
CREATE INDEX IF NOT EXISTS idx_ksk_aid    ON offline_kiosk_attendees(attendee_id);
CREATE INDEX IF NOT EXISTS idx_ksk_upd    ON offline_kiosk_attendees(updated_at DESC);
CREATE INDEX IF NOT EXISTS idx_ksk_cloud  ON offline_kiosk_attendees(needs_cloud_sync);
CREATE INDEX IF NOT EXISTS idx_ksk_local  ON offline_kiosk_attendees(local_modified);
"""

_MIGRATION_COLUMNS: Dict[str, str] = {
    "checkin_history":  "TEXT     NOT NULL DEFAULT '{}'",
    "needs_cloud_sync": "INTEGER  NOT NULL DEFAULT 1",
    "needs_sheet_sync": "INTEGER  NOT NULL DEFAULT 0",
    "local_modified":   "INTEGER  NOT NULL DEFAULT 0",
    "device_name":      "TEXT",
    "photo_url":        "TEXT",
}

# ═══════════════════════════════════════════════════════════════════
#  MYSQL DDL (HUB SERVER - OPTIMIZED WITH JSON & INDEXES)
# ═══════════════════════════════════════════════════════════════════
_MYSQL_ATTENDEE_COLUMNS = """
    id                CHAR(36)     NOT NULL,
    attendee_id       VARCHAR(30)  NOT NULL,
    full_name         VARCHAR(255) NOT NULL,
    mobile            VARCHAR(15)  NOT NULL,
    email             VARCHAR(255) NULL,
    gender            ENUM('MALE','FEMALE','OTHER')                   NOT NULL,
    attendee_type     ENUM('GENERAL','BUSINESS','MEDIA','EXHIBITOR')  NOT NULL DEFAULT 'GENERAL',
    business_name     VARCHAR(255) NULL,
    business_category VARCHAR(100) NULL,
    other_category    VARCHAR(255) NULL,
    address           TEXT         NOT NULL,
    city              VARCHAR(100) NOT NULL,
    state             VARCHAR(100) NOT NULL,
    pincode           VARCHAR(10)  NOT NULL,
    attendance_days   JSON         NOT NULL,
    photo_url         TEXT         NULL,
    created_at        TIMESTAMP    DEFAULT CURRENT_TIMESTAMP,
    updated_at        TIMESTAMP    DEFAULT CURRENT_TIMESTAMP ON UPDATE CURRENT_TIMESTAMP,
    checkin_history   JSON         NOT NULL,
    needs_cloud_sync  TINYINT(1)   NOT NULL DEFAULT 1,
    needs_sheet_sync  TINYINT(1)   NOT NULL DEFAULT 0,
    local_modified    TINYINT(1)   NOT NULL DEFAULT 0,
    device_name       VARCHAR(100) NULL
"""

MYSQL_ATTENDEE_DDL: str = f"""
CREATE TABLE IF NOT EXISTS attendees (
    {_MYSQL_ATTENDEE_COLUMNS},
    PRIMARY KEY (id),
    UNIQUE KEY uk_attendee_id (attendee_id),
    UNIQUE KEY uk_mobile      (mobile),
    CONSTRAINT chk_att_mobile CHECK (CHAR_LENGTH(mobile) >= 10)
) ENGINE=InnoDB DEFAULT CHARSET=utf8mb4 COLLATE=utf8mb4_unicode_ci;
"""

MYSQL_KIOSK_DDL: str = f"""
CREATE TABLE IF NOT EXISTS offline_kiosk_attendees (
    {_MYSQL_ATTENDEE_COLUMNS},
    PRIMARY KEY (id),
    UNIQUE KEY uk_ksk_attendee_id (attendee_id),
    UNIQUE KEY uk_ksk_mobile      (mobile)
) ENGINE=InnoDB DEFAULT CHARSET=utf8mb4 COLLATE=utf8mb4_unicode_ci;
"""

MYSQL_INDEXES: List[str] = [
    "CREATE INDEX idx_att_aid    ON attendees(attendee_id);",
    "CREATE INDEX idx_att_upd    ON attendees(updated_at DESC);",
    "CREATE INDEX idx_att_cloud  ON attendees(needs_cloud_sync);",
    "CREATE INDEX idx_att_local  ON attendees(local_modified);",
    "CREATE INDEX idx_ksk_aid    ON offline_kiosk_attendees(attendee_id);",
    "CREATE INDEX idx_ksk_upd    ON offline_kiosk_attendees(updated_at DESC);",
    "CREATE INDEX idx_ksk_cloud  ON offline_kiosk_attendees(needs_cloud_sync);",
    "CREATE INDEX idx_ksk_local  ON offline_kiosk_attendees(local_modified);"
]

# ═══════════════════════════════════════════════════════════════════
#  PHOTO CACHE DDL
# ═══════════════════════════════════════════════════════════════════
_SQLITE_PHOTO_DDL = """
CREATE TABLE IF NOT EXISTS downloaded_photos (
    id            INTEGER  PRIMARY KEY AUTOINCREMENT,
    attendee_id   TEXT     NOT NULL UNIQUE,
    photo_url     TEXT     NOT NULL,
    local_path    TEXT     NOT NULL,
    downloaded_at DATETIME NOT NULL,
    file_size_kb  REAL     DEFAULT 0
);
CREATE INDEX IF NOT EXISTS idx_ph_aid ON downloaded_photos(attendee_id);
"""

MYSQL_PHOTO_DDL = """
CREATE TABLE IF NOT EXISTS downloaded_photos (
    id            INT AUTO_INCREMENT PRIMARY KEY,
    attendee_id   VARCHAR(100) NOT NULL,
    photo_url     TEXT         NOT NULL,
    local_path    TEXT         NOT NULL,
    downloaded_at DATETIME     NOT NULL,
    file_size_kb  DOUBLE       DEFAULT 0,
    UNIQUE KEY uk_photo_aid (attendee_id)
) ENGINE=InnoDB DEFAULT CHARSET=utf8mb4 COLLATE=utf8mb4_unicode_ci;
"""


# ═══════════════════════════════════════════════════════════════════
#  CUSTOM EXCEPTIONS
# ═══════════════════════════════════════════════════════════════════
class SchemaInitializationError(Exception):
    """
    Raised only when SQLite itself fails to initialize.
    SQLite is the last line of defense for this offline event — if it
    can't be created/opened, there is no fallback left, so this is the
    one case in this module that is allowed to be fatal.
    """
    pass


# ═══════════════════════════════════════════════════════════════════
#  DEFAULT CONFIG HELPERS
# ═══════════════════════════════════════════════════════════════════
_BASE_DIR = os.path.dirname(os.path.abspath(__file__))          # .../app
DEFAULT_SQLITE_PATH = os.path.join(_BASE_DIR, "db", "eventhub_local.db")


def _default_mysql_config() -> Dict[str, Any]:
    """
    Builds a default MySQL connection config from environment variables,
    falling back to sane local-hub defaults. Kept here (not hardcoded
    inline) so config/ can later inject real values without touching
    the DatabaseManager class itself.
    """
    return {
        "host":     os.environ.get("MYSQL_HOST", "127.0.0.1"),
        "port":     int(os.environ.get("MYSQL_PORT", "3306")),
        "user":     os.environ.get("MYSQL_USER", "root"),
        "password": os.environ.get("MYSQL_PASSWORD", "sarwan"),
        "database": os.environ.get("MYSQL_DATABASE", "eventhub_hub"),
        "connection_timeout": int(os.environ.get("MYSQL_CONN_TIMEOUT", "5")),
        "autocommit": False,
    }


# ═══════════════════════════════════════════════════════════════════
#  DATABASE MANAGER — centralized connection manager for the whole app
# ═══════════════════════════════════════════════════════════════════
class DatabaseManager:
    """
    Single, centralized connection manager shared by every module in
    EventHub Portable (server_hub.py, sync_manager.py, register.py,
    check_in.py, explorer.py, photo_down.py).

    Usage from any other file:

        from schema import get_manager

        db = get_manager()

        # SQLite (always available — this is the offline source of truth)
        with db.sqlite_session() as conn:
            conn.execute("INSERT INTO attendees (...) VALUES (...)", (...))

        # MySQL (may be None if the hub DB is down — always check!)
        with db.mysql_session() as conn:
            if conn is None:
                logger.warning("MySQL unreachable, will retry via sync_manager")
            else:
                cur = conn.cursor()
                cur.execute("SELECT ...")

    Concurrency model:
        • SQLite: one connection per thread (thread-local), WAL mode,
          busy_timeout set — safe for a Flask threaded server handling
          ~8 phones hitting check-in endpoints simultaneously.
        • MySQL: a small connection pool. If the pool can't be created
          (driver missing, hub DB down, wrong credentials) the manager
          quietly disables MySQL and every mysql_* method returns None
          instead of raising.
    """

    def __init__(
        self,
        sqlite_path: str = DEFAULT_SQLITE_PATH,
        mysql_config: Optional[Dict[str, Any]] = None,
        mysql_pool_size: int = 5,
    ) -> None:
        self.sqlite_path = sqlite_path
        self.mysql_config = mysql_config or _default_mysql_config()
        self.mysql_pool_size = mysql_pool_size

        self.mysql_available: bool = False

        self._sqlite_local = threading.local()
        self._sqlite_lock = threading.Lock()  # guards schema-level DDL only

        self._mysql_pool: Optional["pooling.MySQLConnectionPool"] = None
        self._mysql_pool_lock = threading.Lock()

        os.makedirs(os.path.dirname(self.sqlite_path) or ".", exist_ok=True)

        if MYSQL_DRIVER_AVAILABLE:
            self._init_mysql_pool()
        else:
            logger.warning(
                "mysql-connector-python is not installed — running in "
                "SQLite-only mode. Install it later and MySQL will be "
                "picked up automatically on next initialize_databases()."
            )

    # ─────────────────────────────────────────────────────────────
    # SQLITE — connection acquisition
    # ─────────────────────────────────────────────────────────────
    def get_sqlite_conn(self) -> sqlite3.Connection:
        """
        Returns this thread's SQLite connection, creating it on first use.
        Never closed automatically between calls (reused for the life of
        the thread) — call close_sqlite_conn() at thread/request teardown
        if you want to release it explicitly (e.g. Flask teardown hook).
        """
        conn = getattr(self._sqlite_local, "conn", None)
        if conn is not None:
            return conn

        conn = sqlite3.connect(
            self.sqlite_path,
            timeout=30,               # wait up to 30s on a locked db
            check_same_thread=True,   # safe: 1 connection per thread
        )
        conn.row_factory = sqlite3.Row
        conn.execute("PRAGMA foreign_keys = ON;")
        conn.execute("PRAGMA journal_mode = WAL;")
        conn.execute("PRAGMA busy_timeout = 5000;")
        self._sqlite_local.conn = conn
        return conn

    def close_sqlite_conn(self) -> None:
        """Closes and releases this thread's SQLite connection, if any."""
        conn = getattr(self._sqlite_local, "conn", None)
        if conn is not None:
            try:
                conn.close()
            except sqlite3.Error as e:
                logger.warning("Error closing SQLite connection: %s", e)
            finally:
                self._sqlite_local.conn = None

    @contextmanager
    def sqlite_session(self) -> Generator[sqlite3.Connection, None, None]:
        """
        Context manager around the thread-local SQLite connection.
        Commits on success, rolls back on any exception. Does NOT close
        the connection (it's reused by this thread for future calls).
        """
        conn = self.get_sqlite_conn()
        try:
            yield conn
            conn.commit()
        except Exception:
            conn.rollback()
            raise

    # ─────────────────────────────────────────────────────────────
    # MYSQL — connection acquisition (must never crash the app)
    # ─────────────────────────────────────────────────────────────
    def _init_mysql_pool(self) -> None:
        """Attempts to (re)build the MySQL connection pool. Never raises."""
        if not MYSQL_DRIVER_AVAILABLE:
            return
        with self._mysql_pool_lock:
            try:
                self._mysql_pool = pooling.MySQLConnectionPool(
                    pool_name="eventhub_pool",
                    pool_size=self.mysql_pool_size,
                    **self.mysql_config,
                )
                self.mysql_available = True
                logger.info(
                    "MySQL connection pool ready (host=%s, db=%s).",
                    self.mysql_config.get("host"),
                    self.mysql_config.get("database"),
                )
            except MySQLError as e:
                self._mysql_pool = None
                self.mysql_available = False
                logger.error(
                    "MySQL pool could not be created — continuing in "
                    "SQLite-only mode. Will retry on next attempted use. "
                    "Reason: %s", e,
                )
            except Exception as e:  # driver/misc failures should not crash us
                self._mysql_pool = None
                self.mysql_available = False
                logger.error("Unexpected error building MySQL pool: %s", e)

    def get_mysql_conn(self):
        """
        Returns a live MySQL connection from the pool, or None if MySQL
        is unreachable/unavailable/uninstalled. Never raises — callers
        must check for None and fall back to SQLite-only behavior.
        """
        if not MYSQL_DRIVER_AVAILABLE:
            return None

        if self._mysql_pool is None:
            # MySQL may have come online after our app started — retry.
            self._init_mysql_pool()
        if self._mysql_pool is None:
            return None

        try:
            conn = self._mysql_pool.get_connection()
            if conn.is_connected():
                self.mysql_available = True
                return conn
            conn.close()
        except MySQLError as e:
            logger.error("MySQL connection unavailable: %s", e)
            self.mysql_available = False
        except Exception as e:
            logger.error("Unexpected error acquiring MySQL connection: %s", e)
            self.mysql_available = False
        return None

    @contextmanager
    def mysql_session(self) -> Generator[Optional[Any], None, None]:
        """
        Context manager around a pooled MySQL connection.
        Yields None if MySQL is unreachable — ALWAYS check for None
        before using the connection. Commits on success, rolls back
        on exception, and always returns the connection to the pool.
        """
        conn = self.get_mysql_conn()
        if conn is None:
            yield None
            return
        try:
            yield conn
            conn.commit()
        except Exception:
            try:
                conn.rollback()
            except Exception:
                pass
            raise
        finally:
            try:
                conn.close()  # returns connection to the pool, doesn't kill it
            except Exception:
                pass

    # ─────────────────────────────────────────────────────────────
    # SCHEMA INITIALIZATION
    # ─────────────────────────────────────────────────────────────
    def initialize_databases(self) -> Dict[str, bool]:
        """
        Creates all tables/triggers/indexes on both databases if they
        don't already exist, and migrates older SQLite files forward.
        Returns a status dict, e.g. {"sqlite": True, "mysql": False}.
        SQLite failure is fatal (raises SchemaInitializationError);
        MySQL failure is logged and simply reported as False.
        """
        status = {"sqlite": False, "mysql": False}
        status["sqlite"] = self._init_sqlite_schema()
        status["mysql"] = self._init_mysql_schema()
        return status

    def _init_sqlite_schema(self) -> bool:
        with self._sqlite_lock:
            try:
                conn = self.get_sqlite_conn()
                conn.executescript(DDL_SQLITE)
                conn.executescript(_SQLITE_PHOTO_DDL)
                self._migrate_sqlite_table(conn, "attendees")
                self._migrate_sqlite_table(conn, "offline_kiosk_attendees")
                conn.commit()
                logger.info(
                    "SQLite schema initialized/verified at %s", self.sqlite_path
                )
                return True
            except sqlite3.Error as e:
                logger.critical("FATAL: SQLite schema initialization failed: %s", e)
                raise SchemaInitializationError(
                    f"Could not initialize SQLite backup database: {e}"
                ) from e

    def _migrate_sqlite_table(self, conn: sqlite3.Connection, table: str) -> None:
        """Adds any columns from _MIGRATION_COLUMNS missing on an older DB file."""
        existing = {row[1] for row in conn.execute(f"PRAGMA table_info({table});")}
        for col_name, col_def in _MIGRATION_COLUMNS.items():
            if col_name not in existing:
                try:
                    conn.execute(
                        f"ALTER TABLE {table} ADD COLUMN {col_name} {col_def};"
                    )
                    logger.info("Migrated %s: added column %s", table, col_name)
                except sqlite3.Error as e:
                    logger.warning(
                        "Could not add column %s to %s: %s", col_name, table, e
                    )

    def _init_mysql_schema(self) -> bool:
        if not MYSQL_DRIVER_AVAILABLE:
            return False

        conn = self.get_mysql_conn()
        if conn is None:
            logger.warning(
                "MySQL unreachable during initialization — continuing on "
                "SQLite only. Hub sync will resume automatically once "
                "MySQL is back (handled by sync_manager.py)."
            )
            return False

        try:
            cursor = conn.cursor()
            cursor.execute(MYSQL_ATTENDEE_DDL)
            cursor.execute(MYSQL_KIOSK_DDL)
            cursor.execute(MYSQL_PHOTO_DDL)

            for stmt in MYSQL_INDEXES:
                try:
                    cursor.execute(stmt)
                except MySQLError as e:
                    # 1061 = Duplicate key name -> index already exists, ignore.
                    if getattr(e, "errno", None) == 1061:
                        continue
                    logger.warning("Index statement skipped (%s): %s", stmt.strip(), e)

            conn.commit()
            cursor.close()
            logger.info("MySQL schema initialized/verified.")
            return True
        except MySQLError as e:
            logger.error(
                "MySQL schema initialization failed — system will continue "
                "on SQLite alone: %s", e
            )
            self.mysql_available = False
            return False
        finally:
            try:
                conn.close()
            except Exception:
                pass

    # ─────────────────────────────────────────────────────────────
    # HEALTH / DIAGNOSTICS
    # ─────────────────────────────────────────────────────────────
    def health_check(self) -> Dict[str, Any]:
        """Quick status snapshot — handy for a /health endpoint in server_hub.py."""
        sqlite_ok = False
        try:
            self.get_sqlite_conn().execute("SELECT 1;")
            sqlite_ok = True
        except sqlite3.Error as e:
            logger.error("SQLite health check failed: %s", e)

        mysql_ok = False
        conn = self.get_mysql_conn()
        if conn is not None:
            try:
                conn.cursor().execute("SELECT 1;")
                mysql_ok = True
            except MySQLError:
                pass
            finally:
                try:
                    conn.close()
                except Exception:
                    pass

        return {
            "sqlite_ok": sqlite_ok,
            "mysql_ok": mysql_ok,
            "mysql_driver_installed": MYSQL_DRIVER_AVAILABLE,
            "sqlite_path": self.sqlite_path,
        }

    # ─────────────────────────────────────────────────────────────
    # SHUTDOWN / CONTEXT MANAGER SUPPORT
    # ─────────────────────────────────────────────────────────────
    def close_all(self) -> None:
        """Releases this thread's SQLite connection. Safe to call anytime."""
        self.close_sqlite_conn()

    def __enter__(self) -> "DatabaseManager":
        return self

    def __exit__(self, exc_type, exc_val, exc_tb) -> None:
        self.close_all()

    def __repr__(self) -> str:  # pragma: no cover
        return (
            f"<DatabaseManager sqlite='{self.sqlite_path}' "
            f"mysql_available={self.mysql_available}>"
        )


# ═══════════════════════════════════════════════════════════════════
#  SHARED SINGLETON — every other module should use this, not create
#  its own DatabaseManager, so they all share one SQLite thread-local
#  store and one MySQL pool.
# ═══════════════════════════════════════════════════════════════════
_manager_instance: Optional[DatabaseManager] = None
_manager_lock = threading.Lock()


def get_manager(
    sqlite_path: Optional[str] = None,
    mysql_config: Optional[Dict[str, Any]] = None,
) -> DatabaseManager:
    """
    Returns the shared DatabaseManager instance, creating it on first call.
    Import this from server_hub.py / sync_manager.py / etc.:

        from schema import get_manager
        db = get_manager()
    """
    global _manager_instance
    if _manager_instance is None:
        with _manager_lock:
            if _manager_instance is None:  # double-checked locking
                _manager_instance = DatabaseManager(
                    sqlite_path=sqlite_path or DEFAULT_SQLITE_PATH,
                    mysql_config=mysql_config,
                )
    return _manager_instance


# ═══════════════════════════════════════════════════════════════════
#  MANUAL TEST — run `python schema.py` to verify both DBs initialize
# ═══════════════════════════════════════════════════════════════════
if __name__ == "__main__":
    print("EventHub Portable — Step 1: schema.py self-test\n")

    db = get_manager()
    result = db.initialize_databases()

    print(f"SQLite ready : {result['sqlite']}  ({db.sqlite_path})")
    print(f"MySQL ready  : {result['mysql']}")

    if not result["mysql"]:
        print(
            "\nNote: MySQL is unavailable right now — this is fine. "
            "The kiosk system runs entirely on SQLite until sync_manager.py "
            "(Step 2) reconnects to the hub."
        )

    print("\nHealth check:", db.health_check())
    db.close_all()
