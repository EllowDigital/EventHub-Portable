"""
sync_manager.py — EventHub Portable (TDE UP 2026)
═══════════════════════════════════════════════════════════════════════════

STEP 2 of 3 in the EventHub Portable roadmap:
    -> Step 1: schema.py         (DONE — DatabaseManager, DDL)
    -> Step 2: sync_manager.py   (THIS FILE — bridge to Supabase/Sheets)
    -> Step 3: server_hub.py

WHY THIS FILE IS "ON-DEMAND ONLY":
    There is no background thread, no `while True: sleep(...)` poll loop,
    and no scheduler in this file. The cloud is unreliable/absent for most
    of a 3-day offline event, so touching it is treated as a deliberate,
    user-initiated action — a button in the (future) UI calls
    `SyncManager.trigger_full_sync()` exactly once per click. Everything
    else in this module exists to make that single call safe, observable,
    and non-blocking to the rest of the kiosk system.

STATE MACHINE:
    IDLE     -> nothing running, safe to trigger a sync.
    SYNCING  -> a trigger_full_sync() call is currently in progress.
    ERROR    -> the most recent sync attempt failed. This state is STICKY:
                it persists until the next trigger_full_sync() call is
                made (which immediately flips to SYNCING), so a status
                endpoint in server_hub.py can show "last sync failed" to
                the person running the check-in desk instead of the error
                disappearing the instant the function returns.

SYNC SEQUENCE (per trigger_full_sync() call):
    1. Local Sync      : SQLite (local_modified=1) -> MySQL, then MySQL -> SQLite mirror.
    2. Cloud Push      : MySQL (needs_cloud_sync=1) -> Supabase.
    3. Cloud Pull      : Supabase (new online registrations) -> MySQL -> SQLite.
    4. Sheets Sync     : MySQL (needs_sheet_sync=1) -> Google Sheets webhook / edge function.

Every external call (MySQL, Supabase, Sheets webhook) is wrapped so that a
dead connection is logged and skipped, never raised past this module in a
way that could take down the Flask kiosk server.
"""

import os
import json
import logging
import sqlite3
import threading
import uuid
from datetime import datetime, timezone
from enum import Enum
from typing import Any, Dict, List, Optional, Tuple

from app.schema import get_manager

# ─────────────────────────────────────────────────────────────────────────
# Optional dependencies — this module must import cleanly and run in
# SQLite-only / no-internet mode even if these packages aren't installed.
# ─────────────────────────────────────────────────────────────────────────
try:
    from supabase import create_client, Client
    SUPABASE_SDK_AVAILABLE = True
except ImportError:  # pragma: no cover - environment dependent
    create_client = None   # type: ignore
    Client = None           # type: ignore
    SUPABASE_SDK_AVAILABLE = False

try:
    import requests
    REQUESTS_AVAILABLE = True
except ImportError:  # pragma: no cover
    requests = None  # type: ignore
    REQUESTS_AVAILABLE = False

try:
    from mysql.connector.errors import Error as MySQLError
except ImportError:  # pragma: no cover
    MySQLError = Exception  # type: ignore


# ═══════════════════════════════════════════════════════════════════
#  LOGGING
# ═══════════════════════════════════════════════════════════════════
logger = logging.getLogger("eventhub.sync_manager")
if not logger.handlers:
    _handler = logging.StreamHandler()
    _handler.setFormatter(logging.Formatter(
        "%(asctime)s [%(levelname)s] %(name)s: %(message)s"
    ))
    logger.addHandler(_handler)
    logger.setLevel(logging.INFO)


# ═══════════════════════════════════════════════════════════════════
#  CONSTANTS — shared column list for attendees / offline_kiosk_attendees
# ═══════════════════════════════════════════════════════════════════
ATTENDEE_COLUMNS: List[str] = [
    "id", "attendee_id", "full_name", "mobile", "email", "gender",
    "attendee_type", "business_name", "business_category", "other_category",
    "address", "city", "state", "pincode", "attendance_days", "photo_url",
    "created_at", "updated_at", "checkin_history", "needs_cloud_sync",
    "needs_sheet_sync", "local_modified", "device_name",
]

# Both local tables share the same column layout (see schema.py).
SYNC_TABLES: List[str] = ["attendees", "offline_kiosk_attendees"]

# Which Supabase table each local table pushes into. Kiosk walk-in
# registrations are assumed to land in the same online "attendees" table
# as normal registrations once synced — adjust here if your Supabase
# schema keeps them separate.
CLOUD_TABLE_MAP: Dict[str, str] = {
    "attendees": "attendees",
    "offline_kiosk_attendees": "attendees",
}

_BASE_DIR = os.path.dirname(os.path.abspath(__file__))  # .../app
DEFAULT_SECRETS_PATH = os.path.join(_BASE_DIR, "config", "secrets.json")

_JSON_COLUMNS = ("attendance_days", "checkin_history")
_INTERNAL_ONLY_COLUMNS = ("needs_cloud_sync", "needs_sheet_sync", "local_modified", "device_name")


class SyncState(str, Enum):
    IDLE = "IDLE"
    SYNCING = "SYNCING"
    ERROR = "ERROR"


class SyncManager:
    """
    On-demand bridge between the local databases (via schema.DatabaseManager)
    and the cloud (Supabase + Google Sheets). Nothing in this class runs
    unless trigger_full_sync() is called explicitly — no timers, no polling.
    """

    def __init__(self, secrets_path: Optional[str] = None) -> None:
        self.db = get_manager()

        self.state: SyncState = SyncState.IDLE
        self.last_error: Optional[str] = None
        self.last_sync_summary: Dict[str, Any] = {}

        # Guards against a double-click firing two syncs concurrently.
        self._run_lock = threading.Lock()

        self.secrets_path = secrets_path or DEFAULT_SECRETS_PATH
        self._secrets = self._load_secrets()
        self.supabase_url: Optional[str] = self._secrets.get("SUPABASE_URL")
        self.supabase_key: Optional[str] = self._secrets.get("SUPABASE_KEY")
        self.sheets_webhook_url: Optional[str] = self._secrets.get("SHEETS_WEBHOOK_URL")
        self.sheets_edge_function: Optional[str] = self._secrets.get(
            "SHEETS_EDGE_FUNCTION", "sync-sheets"
        )

    # ─────────────────────────────────────────────────────────────
    # PUBLIC STATUS HELPERS (for the future /sync/status route)
    # ─────────────────────────────────────────────────────────────
    def get_state(self) -> str:
        return self.state.value

    def is_busy(self) -> bool:
        return self.state == SyncState.SYNCING

    def get_last_summary(self) -> Dict[str, Any]:
        return self.last_sync_summary

    # ─────────────────────────────────────────────────────────────
    # SECRETS LOADING — never crashes, never blocks startup
    # ─────────────────────────────────────────────────────────────
    def _load_secrets(self) -> Dict[str, Any]:
        try:
            with open(self.secrets_path, "r", encoding="utf-8") as f:
                data = json.load(f)
            if not isinstance(data, dict):
                raise ValueError("secrets.json must contain a JSON object")
            return data
        except FileNotFoundError:
            logger.error(
                "Secrets file not found at %s — cloud sync will be skipped "
                "until it's created (needs SUPABASE_URL / SUPABASE_KEY).",
                self.secrets_path,
            )
        except (json.JSONDecodeError, ValueError) as e:
            logger.error("secrets.json is malformed (%s) — cloud sync disabled.", e)
        except OSError as e:
            logger.error("Could not read secrets.json: %s", e)
        return {}

    # ─────────────────────────────────────────────────────────────
    # SUPABASE CLIENT LIFECYCLE — created and torn down per sync
    # ─────────────────────────────────────────────────────────────
    def _get_supabase_client(self):
        if not SUPABASE_SDK_AVAILABLE:
            logger.error("supabase-py is not installed — skipping cloud steps this sync.")
            return None
        if not self.supabase_url or not self.supabase_key:
            logger.warning(
                "SUPABASE_URL/SUPABASE_KEY missing from secrets.json — "
                "skipping cloud steps this sync."
            )
            return None
        try:
            return create_client(self.supabase_url, self.supabase_key)
        except Exception as e:
            logger.error("Could not create Supabase client (offline / bad creds?): %s", e)
            return None

    def _close_supabase_client(self, client) -> None:
        """
        Best-effort release of the underlying HTTP session(s) so memory
        doesn't accumulate across repeated button clicks. supabase-py wraps
        httpx clients inside postgrest-py / gotrue-py / storage3, and the
        exact attribute path has shifted between SDK versions — every path
        tried here is optional, and failures are swallowed on purpose.
        """
        if client is None:
            return
        for attr_path in (("postgrest", "session"), ("_postgrest", "session")):
            obj = client
            try:
                for attr in attr_path:
                    obj = getattr(obj, attr)
                obj.close()
            except Exception:
                continue
        del client

    # ─────────────────────────────────────────────────────────────
    # THE MAIN ENTRY POINT — call this from the UI button handler
    # ─────────────────────────────────────────────────────────────
    def trigger_full_sync(self) -> Dict[str, Any]:
        """
        Runs the full sync sequence exactly once:
            Local Sync -> Cloud Push -> Cloud Pull -> Sheets Sync
        Always returns a summary dict. Never raises — any failure is
        caught, logged, and reflected in the returned summary and in
        self.state (ERROR), so the caller (a Flask route, in Step 3)
        can report it to the UI without the process crashing.
        """
        if not self._run_lock.acquire(blocking=False):
            logger.warning("trigger_full_sync() called while a sync is already running.")
            return {"status": "already_running", "state": self.state.value}

        self.state = SyncState.SYNCING
        self.last_error = None
        started_at = datetime.now(timezone.utc)
        summary: Dict[str, Any] = {
            "started_at": started_at.isoformat(),
            "local_sync": {},
            "cloud_push": {},
            "cloud_pull": {},
            "sheets_sync": {},
            "success": False,
        }
        history_id = self._start_sync_history_record()
        supabase_client = None

        try:
            # ── Step 1: Local Sync (SQLite <-> MySQL) ──────────────────
            summary["local_sync"] = self._sync_local_sqlite_mysql()

            # Cloud steps need MySQL as the hub-of-record in the middle,
            # per the required flow. If MySQL is down there is nothing
            # meaningful to push/pull yet — skip cleanly and try again
            # next click.
            if not self.db.mysql_available:
                reason = "MySQL hub database unavailable"
                summary["cloud_push"] = {"skipped": True, "reason": reason}
                summary["cloud_pull"] = {"skipped": True, "reason": reason}
                summary["sheets_sync"] = {"skipped": True, "reason": reason}
            else:
                supabase_client = self._get_supabase_client()
                if supabase_client is None:
                    reason = "Supabase not configured or unreachable"
                    summary["cloud_push"] = {"skipped": True, "reason": reason}
                    summary["cloud_pull"] = {"skipped": True, "reason": reason}
                    summary["sheets_sync"] = {"skipped": True, "reason": reason}
                else:
                    # ── Step 2: Cloud Push (MySQL -> Supabase) ─────────
                    summary["cloud_push"] = self._push_pending_to_supabase(supabase_client)

                    # ── Step 3: Cloud Pull (Supabase -> MySQL -> SQLite)
                    summary["cloud_pull"] = self._pull_new_from_supabase(supabase_client)

                    # ── Step 4: Google Sheets Sync ──────────────────────
                    summary["sheets_sync"] = self._sync_google_sheets(supabase_client)

            summary["success"] = True
            self.state = SyncState.IDLE

        except Exception as e:
            # Catches anything unexpected (including internet-down errors
            # that slipped past an inner try/except) so the kiosk server
            # never crashes because of a sync click.
            logger.error("trigger_full_sync() failed: %s", e, exc_info=True)
            self.last_error = str(e)
            summary["success"] = False
            summary["error"] = str(e)
            self.state = SyncState.ERROR

        finally:
            ended_at = datetime.now(timezone.utc)
            summary["ended_at"] = ended_at.isoformat()
            summary["duration_seconds"] = (ended_at - started_at).total_seconds()
            summary["state"] = self.state.value
            self._finish_sync_history_record(history_id, summary)
            self.last_sync_summary = summary
            self._close_supabase_client(supabase_client)
            self._run_lock.release()

        return summary

    # ─────────────────────────────────────────────────────────────
    # STEP 1: LOCAL SYNC (SQLite <-> MySQL)
    # ─────────────────────────────────────────────────────────────
    def _sync_local_sqlite_mysql(self) -> Dict[str, Any]:
        result: Dict[str, Any] = {}
        for table in SYNC_TABLES:
            push_result = self._push_local_changes_for_table(table)
            pull_result = self._pull_hub_changes_for_table(table)
            result[table] = {"push": push_result, "pull": pull_result}
        return result

    def _push_local_changes_for_table(self, table: str) -> Dict[str, Any]:
        """SQLite rows with local_modified=1 -> upsert into MySQL."""
        try:
            with self.db.sqlite_session() as sconn:
                rows = sconn.execute(
                    f"SELECT {', '.join(ATTENDEE_COLUMNS)} FROM {table} "
                    f"WHERE local_modified = 1;"
                ).fetchall()
        except sqlite3.Error as e:
            logger.error("Reading local changes from %s failed: %s", table, e)
            return {"pushed": 0, "failed": 0, "error": str(e)}

        if not rows:
            return {"pushed": 0, "failed": 0}

        pushed_ids: List[str] = []
        failed = 0

        with self.db.mysql_session() as mconn:
            if mconn is None:
                logger.warning("Skipping push for %s: MySQL unavailable.", table)
                return {"pushed": 0, "failed": len(rows), "skipped": True}

            cursor = mconn.cursor()
            placeholders = ", ".join(["%s"] * len(ATTENDEE_COLUMNS))
            update_clause = ", ".join(
                f"{c}=VALUES({c})" for c in ATTENDEE_COLUMNS if c not in ("id", "created_at")
            )
            upsert_sql = (
                f"INSERT INTO {table} ({', '.join(ATTENDEE_COLUMNS)}) "
                f"VALUES ({placeholders}) ON DUPLICATE KEY UPDATE {update_clause};"
            )
            for row in rows:
                try:
                    cursor.execute(upsert_sql, tuple(row[c] for c in ATTENDEE_COLUMNS))
                    pushed_ids.append(row["id"])
                except MySQLError as e:
                    logger.error("Failed to push %s row %s to MySQL: %s", table, row["id"], e)
                    failed += 1
            cursor.close()

        if pushed_ids:
            try:
                with self.db.sqlite_session() as sconn:
                    qmarks = ", ".join(["?"] * len(pushed_ids))
                    sconn.execute(
                        f"UPDATE {table} SET local_modified = 0 WHERE id IN ({qmarks});",
                        pushed_ids,
                    )
            except sqlite3.Error as e:
                logger.error("Could not clear local_modified flags on %s: %s", table, e)

        return {"pushed": len(pushed_ids), "failed": failed}

    def _pull_hub_changes_for_table(self, table: str) -> Dict[str, Any]:
        """MySQL rows newer than our last pull -> mirror into SQLite."""
        meta_key = f"last_pull_{table}"
        try:
            with self.db.sqlite_session() as sconn:
                meta_row = sconn.execute(
                    "SELECT value FROM sync_meta WHERE key = ?;", (meta_key,)
                ).fetchone()
        except sqlite3.Error as e:
            logger.error("Could not read sync_meta cursor for %s: %s", table, e)
            return {"pulled": 0, "error": str(e)}

        since = meta_row["value"] if meta_row else "1970-01-01 00:00:00"

        with self.db.mysql_session() as mconn:
            if mconn is None:
                return {"pulled": 0, "skipped": True}
            try:
                cursor = mconn.cursor(dictionary=True)
                cursor.execute(
                    f"SELECT {', '.join(ATTENDEE_COLUMNS)} FROM {table} "
                    f"WHERE updated_at > %s ORDER BY updated_at ASC;",
                    (since,),
                )
                rows = cursor.fetchall()
                cursor.close()
            except MySQLError as e:
                logger.error("Pulling hub changes for %s failed: %s", table, e)
                return {"pulled": 0, "error": str(e)}

        if not rows:
            return {"pulled": 0}

        # NOTE: the SQLite trigger trg_*_updated_at stamps updated_at with
        # CURRENT_TIMESTAMP on every UPDATE, including this mirroring write.
        # That means SQLite's updated_at will not exactly equal MySQL's
        # after this runs — expected, and harmless, because our real
        # sync cursor (last_pull_{table}) is tracked from MySQL's own
        # updated_at values below, not from SQLite's.
        set_clause = ", ".join(
            f"{c} = excluded.{c}" for c in ATTENDEE_COLUMNS if c not in ("id", "local_modified")
        )
        placeholders = ", ".join(["?"] * len(ATTENDEE_COLUMNS))
        upsert_sql = (
            f"INSERT INTO {table} ({', '.join(ATTENDEE_COLUMNS)}) VALUES ({placeholders}) "
            f"ON CONFLICT(id) DO UPDATE SET {set_clause} "
            f"WHERE excluded.updated_at > {table}.updated_at;"
        )

        try:
            with self.db.sqlite_session() as sconn:
                for row in rows:
                    sconn.execute(upsert_sql, tuple(row[c] for c in ATTENDEE_COLUMNS))
        except sqlite3.Error as e:
            logger.error("Mirroring hub changes into SQLite (%s) failed: %s", table, e)
            return {"pulled": 0, "error": str(e)}

        max_updated = max(str(r["updated_at"]) for r in rows)
        try:
            with self.db.sqlite_session() as sconn:
                sconn.execute(
                    "INSERT INTO sync_meta (key, value) VALUES (?, ?) "
                    "ON CONFLICT(key) DO UPDATE SET value = excluded.value;",
                    (meta_key, max_updated),
                )
        except sqlite3.Error as e:
            logger.warning("Could not advance sync_meta cursor for %s: %s", table, e)

        return {"pulled": len(rows)}

    # ─────────────────────────────────────────────────────────────
    # STEP 2: CLOUD PUSH (MySQL -> Supabase)
    # ─────────────────────────────────────────────────────────────
    def _push_pending_to_supabase(self, supabase_client) -> Dict[str, Any]:
        details: Dict[str, Any] = {}
        total_pushed = total_failed = 0

        for local_table, cloud_table in CLOUD_TABLE_MAP.items():
            with self.db.mysql_session() as mconn:
                if mconn is None:
                    details[local_table] = {"skipped": True, "reason": "MySQL unavailable"}
                    continue
                try:
                    cursor = mconn.cursor(dictionary=True)
                    cursor.execute(
                        f"SELECT {', '.join(ATTENDEE_COLUMNS)} FROM {local_table} "
                        f"WHERE needs_cloud_sync = 1;"
                    )
                    rows = cursor.fetchall()
                    cursor.close()
                except MySQLError as e:
                    logger.error("Reading pending cloud rows from %s failed: %s", local_table, e)
                    details[local_table] = {"pushed": 0, "failed": 0, "error": str(e)}
                    continue

            if not rows:
                details[local_table] = {"pushed": 0, "failed": 0}
                continue

            pushed_ids: List[str] = []
            failed = 0
            for row in rows:
                payload = self._row_to_cloud_payload(row)
                try:
                    supabase_client.table(cloud_table).upsert(
                        payload, on_conflict="attendee_id"
                    ).execute()
                    pushed_ids.append(row["id"])
                except Exception as e:
                    # Covers httpx timeouts/connection errors (internet down)
                    # as well as Supabase-side validation errors.
                    logger.error(
                        "Supabase push failed for %s (attendee_id=%s): %s",
                        local_table, row.get("attendee_id"), e,
                    )
                    failed += 1

            if pushed_ids:
                self._mark_cloud_synced(local_table, pushed_ids)

            details[local_table] = {"pushed": len(pushed_ids), "failed": failed}
            total_pushed += len(pushed_ids)
            total_failed += failed

        return {"by_table": details, "total_pushed": total_pushed, "total_failed": total_failed}

    def _mark_cloud_synced(self, table: str, ids: List[str]) -> None:
        if not ids:
            return
        with self.db.mysql_session() as mconn:
            if mconn is not None:
                try:
                    cursor = mconn.cursor()
                    fmt = ", ".join(["%s"] * len(ids))
                    cursor.execute(
                        f"UPDATE {table} SET needs_cloud_sync = 0 WHERE id IN ({fmt});", ids
                    )
                    cursor.close()
                except MySQLError as e:
                    logger.error("Could not clear needs_cloud_sync in MySQL %s: %s", table, e)
        try:
            with self.db.sqlite_session() as sconn:
                fmt = ", ".join(["?"] * len(ids))
                sconn.execute(
                    f"UPDATE {table} SET needs_cloud_sync = 0 WHERE id IN ({fmt});", ids
                )
        except sqlite3.Error as e:
            logger.error("Could not clear needs_cloud_sync in SQLite %s: %s", table, e)

    # ─────────────────────────────────────────────────────────────
    # STEP 3: CLOUD PULL (Supabase -> MySQL -> SQLite)
    # ─────────────────────────────────────────────────────────────
    def _pull_new_from_supabase(self, supabase_client) -> Dict[str, Any]:
        meta_key = "last_cloud_pull_attendees"
        try:
            with self.db.sqlite_session() as sconn:
                meta_row = sconn.execute(
                    "SELECT value FROM sync_meta WHERE key = ?;", (meta_key,)
                ).fetchone()
        except sqlite3.Error as e:
            logger.error("Could not read cloud-pull cursor: %s", e)
            return {"pulled": 0, "error": str(e)}

        since = meta_row["value"] if meta_row else "1970-01-01T00:00:00+00:00"

        try:
            response = (
                supabase_client.table("attendees")
                .select("*")
                .gt("updated_at", since)
                .order("updated_at")
                .execute()
            )
            cloud_rows = response.data or []
        except Exception as e:
            # Internet down / Supabase unreachable — log and move on.
            logger.error("Supabase pull failed (offline?): %s", e)
            return {"pulled": 0, "error": str(e)}

        if not cloud_rows:
            return {"pulled": 0}

        inserted = self._upsert_cloud_rows_into_mysql_and_sqlite(cloud_rows)

        updated_values = [r.get("updated_at") for r in cloud_rows if r.get("updated_at")]
        if updated_values:
            max_updated = max(str(v) for v in updated_values)
            try:
                with self.db.sqlite_session() as sconn:
                    sconn.execute(
                        "INSERT INTO sync_meta (key, value) VALUES (?, ?) "
                        "ON CONFLICT(key) DO UPDATE SET value = excluded.value;",
                        (meta_key, max_updated),
                    )
            except sqlite3.Error as e:
                logger.warning("Could not advance cloud-pull cursor: %s", e)

        return {"pulled": inserted}

    def _upsert_cloud_rows_into_mysql_and_sqlite(self, cloud_rows: List[Dict[str, Any]]) -> int:
        count = 0

        with self.db.mysql_session() as mconn:
            if mconn is None:
                logger.warning(
                    "MySQL unavailable — new cloud registrations will be "
                    "re-fetched and retried on the next sync."
                )
                return 0
            try:
                cursor = mconn.cursor()
                placeholders = ", ".join(["%s"] * len(ATTENDEE_COLUMNS))
                update_clause = ", ".join(
                    f"{c}=VALUES({c})" for c in ATTENDEE_COLUMNS if c not in ("id", "created_at")
                )
                upsert_sql = (
                    f"INSERT INTO attendees ({', '.join(ATTENDEE_COLUMNS)}) "
                    f"VALUES ({placeholders}) ON DUPLICATE KEY UPDATE {update_clause};"
                )
                for crow in cloud_rows:
                    try:
                        values = self._cloud_row_to_local_values(crow)
                        cursor.execute(upsert_sql, values)
                        count += 1
                    except MySQLError as e:
                        logger.error(
                            "Failed to mirror cloud row %s into MySQL: %s",
                            crow.get("attendee_id"), e,
                        )
                cursor.close()
            except MySQLError as e:
                logger.error("Cloud-pull mirroring into MySQL failed: %s", e)
                return count

        if count:
            try:
                with self.db.sqlite_session() as sconn:
                    set_clause = ", ".join(
                        f"{c} = excluded.{c}" for c in ATTENDEE_COLUMNS
                        if c not in ("id", "local_modified")
                    )
                    placeholders = ", ".join(["?"] * len(ATTENDEE_COLUMNS))
                    upsert_sql = (
                        f"INSERT INTO attendees ({', '.join(ATTENDEE_COLUMNS)}) "
                        f"VALUES ({placeholders}) ON CONFLICT(id) DO UPDATE SET {set_clause};"
                    )
                    for crow in cloud_rows:
                        try:
                            values = self._cloud_row_to_local_values(crow)
                            sconn.execute(upsert_sql, values)
                        except sqlite3.Error as e:
                            logger.error(
                                "Failed to mirror cloud row %s into SQLite: %s",
                                crow.get("attendee_id"), e,
                            )
            except sqlite3.Error as e:
                logger.error("Cloud-pull mirroring into SQLite failed: %s", e)

        return count

    def _cloud_row_to_local_values(self, crow: Dict[str, Any]) -> Tuple[Any, ...]:
        """Maps a Supabase attendee row onto our local ATTENDEE_COLUMNS order."""

        def as_json_text(value, default):
            if value is None:
                return json.dumps(default)
            if isinstance(value, str):
                return value  # already JSON text
            return json.dumps(value)

        mapped = {
            "id": crow.get("id") or str(uuid.uuid4()),
            "attendee_id": crow.get("attendee_id"),
            "full_name": crow.get("full_name"),
            "mobile": crow.get("mobile"),
            "email": crow.get("email"),
            "gender": crow.get("gender"),
            "attendee_type": crow.get("attendee_type", "GENERAL"),
            "business_name": crow.get("business_name"),
            "business_category": crow.get("business_category"),
            "other_category": crow.get("other_category"),
            "address": crow.get("address"),
            "city": crow.get("city"),
            "state": crow.get("state"),
            "pincode": crow.get("pincode"),
            "attendance_days": as_json_text(crow.get("attendance_days"), []),
            "photo_url": crow.get("photo_url"),
            "created_at": crow.get("created_at"),
            "updated_at": crow.get("updated_at"),
            "checkin_history": as_json_text(crow.get("checkin_history"), {}),
            # It came FROM the cloud, so it's already the source of truth —
            # no need to push it right back up.
            "needs_cloud_sync": 0,
            "needs_sheet_sync": int(bool(crow.get("needs_sheet_sync", 0))),
            "local_modified": 0,
            "device_name": crow.get("device_name"),
        }
        return tuple(mapped[c] for c in ATTENDEE_COLUMNS)

    # ─────────────────────────────────────────────────────────────
    # STEP 4: GOOGLE SHEETS SYNC
    # ─────────────────────────────────────────────────────────────
    def _sync_google_sheets(self, supabase_client) -> Dict[str, Any]:
        with self.db.mysql_session() as mconn:
            if mconn is None:
                return {"skipped": True, "reason": "MySQL unavailable"}
            try:
                cursor = mconn.cursor(dictionary=True)
                cursor.execute(
                    f"SELECT {', '.join(ATTENDEE_COLUMNS)} FROM attendees "
                    f"WHERE needs_sheet_sync = 1;"
                )
                rows = cursor.fetchall()
                cursor.close()
            except MySQLError as e:
                logger.error("Reading rows pending Sheets sync failed: %s", e)
                return {"pushed": 0, "error": str(e)}

        if not rows:
            return {"pushed": 0}

        payloads = [self._row_to_cloud_payload(r) for r in rows]

        try:
            if self.sheets_webhook_url and REQUESTS_AVAILABLE:
                resp = requests.post(
                    self.sheets_webhook_url, json={"records": payloads}, timeout=10
                )
                resp.raise_for_status()
            elif SUPABASE_SDK_AVAILABLE and hasattr(supabase_client, "functions"):
                supabase_client.functions.invoke(
                    self.sheets_edge_function,
                    invoke_options={"body": {"records": payloads}},
                )
            else:
                logger.warning(
                    "No SHEETS_WEBHOOK_URL configured and no usable Supabase "
                    "edge function client — skipping Sheets sync."
                )
                return {"skipped": True, "reason": "Not configured"}
        except Exception as e:
            # Covers webhook timeouts, DNS failures, edge function errors —
            # i.e. the internet-down case for this step specifically.
            logger.error("Google Sheets sync failed (offline?): %s", e)
            return {"pushed": 0, "failed": len(rows), "error": str(e)}

        ids = [r["id"] for r in rows]
        with self.db.mysql_session() as mconn:
            if mconn is not None:
                try:
                    cursor = mconn.cursor()
                    fmt = ", ".join(["%s"] * len(ids))
                    cursor.execute(
                        f"UPDATE attendees SET needs_sheet_sync = 0 WHERE id IN ({fmt});", ids
                    )
                    cursor.close()
                except MySQLError as e:
                    logger.error("Could not clear needs_sheet_sync in MySQL: %s", e)
        try:
            with self.db.sqlite_session() as sconn:
                fmt = ", ".join(["?"] * len(ids))
                sconn.execute(
                    f"UPDATE attendees SET needs_sheet_sync = 0 WHERE id IN ({fmt});", ids
                )
        except sqlite3.Error as e:
            logger.error("Could not clear needs_sheet_sync in SQLite: %s", e)

        return {"pushed": len(ids)}

    # ─────────────────────────────────────────────────────────────
    # SHARED HELPERS
    # ─────────────────────────────────────────────────────────────
    def _row_to_cloud_payload(self, row) -> Dict[str, Any]:
        """Converts one local DB row into a JSON-ready dict for Supabase."""
        payload = dict(row)
        for json_col in _JSON_COLUMNS:
            raw = payload.get(json_col)
            if isinstance(raw, str):
                try:
                    payload[json_col] = json.loads(raw)
                except (json.JSONDecodeError, TypeError):
                    pass  # send it as-is rather than dropping the whole record
        for internal_col in _INTERNAL_ONLY_COLUMNS:
            payload.pop(internal_col, None)
        return payload

    # ─────────────────────────────────────────────────────────────
    # SYNC HISTORY BOOKKEEPING (uses the sync_history table from schema.py)
    # ─────────────────────────────────────────────────────────────
    def _start_sync_history_record(self) -> Optional[int]:
        try:
            with self.db.sqlite_session() as sconn:
                cur = sconn.execute(
                    "INSERT INTO sync_history (op_type, started_at, status) "
                    "VALUES (?, ?, 'RUNNING');",
                    ("FULL_SYNC", datetime.now(timezone.utc).isoformat()),
                )
                return cur.lastrowid
        except sqlite3.Error as e:
            logger.warning("Could not create sync_history record: %s", e)
            return None

    def _finish_sync_history_record(self, record_id: Optional[int], summary: Dict[str, Any]) -> None:
        if record_id is None:
            return
        status = "SUCCESS" if summary.get("success") else "FAILED"
        try:
            with self.db.sqlite_session() as sconn:
                sconn.execute(
                    "UPDATE sync_history SET ended_at = ?, status = ? WHERE id = ?;",
                    (datetime.now(timezone.utc).isoformat(), status, record_id),
                )
        except sqlite3.Error as e:
            logger.warning("Could not finalize sync_history record: %s", e)


# ═══════════════════════════════════════════════════════════════════
#  SHARED SINGLETON — mirrors schema.get_manager() so server_hub.py can
#  do `from sync_manager import get_sync_manager` and share one instance.
# ═══════════════════════════════════════════════════════════════════
_sync_manager_instance: Optional[SyncManager] = None
_sync_manager_lock = threading.Lock()


def get_sync_manager(secrets_path: Optional[str] = None) -> SyncManager:
    global _sync_manager_instance
    if _sync_manager_instance is None:
        with _sync_manager_lock:
            if _sync_manager_instance is None:
                _sync_manager_instance = SyncManager(secrets_path=secrets_path)
    return _sync_manager_instance


# ═══════════════════════════════════════════════════════════════════
#  MANUAL TEST — run `python sync_manager.py` to fire one sync by hand
# ═══════════════════════════════════════════════════════════════════
if __name__ == "__main__":
    print("EventHub Portable — Step 2: sync_manager.py self-test\n")

    manager = get_sync_manager()
    print(f"Initial state: {manager.get_state()}")

    result = manager.trigger_full_sync()
    print("\nSync summary:")
    print(json.dumps(result, indent=2, default=str))

    print(f"\nFinal state: {manager.get_state()}")
