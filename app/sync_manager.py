import os
import json
import enum
import time
import logging
import threading
import queue
from datetime import datetime, timezone
import tkinter as tk

# PyMySQL provides a pure-Python MySQL driver.
import pymysql
pymysql.install_as_MySQLdb()

from supabase import create_client, Client
from sqlalchemy import create_engine
import ttkbootstrap as ttk
from ttkbootstrap.constants import *
from ttkbootstrap.dialogs import Messagebox
from ttkbootstrap.widgets.tooltip import ToolTip

# Safely track changes merged from the cloud
from sqlalchemy.orm.attributes import flag_modified

# Import models and DB initialization from your schema
try:
    from app.schema import Attendee, OfflineKioskAttendee, get_database_sessions
except ModuleNotFoundError:
    from schema import Attendee, OfflineKioskAttendee, get_database_sessions

# ==============================================================================
# 24/7 STABILITY: GLOBAL CRASH HANDLER
# ==============================================================================
def global_exception_handler(exc_type, exc_value, exc_traceback):
    logging.error("Uncaught GUI Exception intercepted. App remains running.", exc_info=(exc_type, exc_value, exc_traceback))

tk.Tk.report_callback_exception = global_exception_handler

# ==============================================================================
# PATHS & CONFIG
# ==============================================================================
BASE_DIR = os.path.dirname(os.path.abspath(__file__))
CONFIG_DIR = os.path.join(BASE_DIR, 'config')
SECRETS_PATH = os.path.join(CONFIG_DIR, 'secrets.json')
SCHEMA_PATH = os.path.join(CONFIG_DIR, 'schema.json')
CONFLICTS_PATH = os.path.join(CONFIG_DIR, 'conflicts.json')
SYNC_STATE_PATH = os.path.join(CONFIG_DIR, 'sync_state.json')
LOG_DIR = os.path.join(BASE_DIR, 'logs')

os.makedirs(CONFIG_DIR, exist_ok=True)
os.makedirs(LOG_DIR, exist_ok=True)

EVENT_DAYS = ["2026-08-30", "2026-08-31", "2026-09-01"]

COMPARABLE_FIELDS = [
    "full_name", "mobile", "email", "gender", "attendee_type",
    "business_name", "business_category", "other_category",
    "address", "city", "state", "pincode", "photo_url",
]

PUSH_BATCH_RETRIES = 3
PULL_PAGE_RETRIES = 3
PULL_COMMIT_BATCH_SIZE = 250

# ==============================================================================
# CANONICAL CHECK-IN DAY HANDLING
# ==============================================================================
def _build_portal_key_map():
    mapping = {}
    for iso_day in EVENT_DAYS:
        dt = datetime.strptime(iso_day, "%Y-%m-%d")
        portal_key = f"{dt.day} {dt.strftime('%B')}" 
        
        mapping[iso_day.lower()] = portal_key
        mapping[iso_day] = portal_key
        mapping[portal_key.lower()] = portal_key
        mapping[portal_key] = portal_key
        mapping[f"{dt.day} {dt.strftime('%b')}".lower()] = portal_key
    return mapping

_PORTAL_KEYS = _build_portal_key_map()

def _canonical_day_key(raw_key):
    if not raw_key: return raw_key
    key = str(raw_key).strip()
    return _PORTAL_KEYS.get(key.lower(), key)

def _coerce_dict(value):
    if isinstance(value, dict): return value
    if isinstance(value, str) and value.strip():
        try:
            parsed = json.loads(value)
            return parsed if isinstance(parsed, dict) else {}
        except Exception: return {}
    return {}

def _coerce_list(value):
    if isinstance(value, list): return value
    if isinstance(value, str) and value.strip():
        try:
            parsed = json.loads(value)
            return parsed if isinstance(parsed, list) else []
        except Exception: return []
    return []

def _entry_timestamp(entry):
    if not isinstance(entry, dict): return None
    raw = entry.get("timestamp")
    if not raw: return None
    try: return datetime.fromisoformat(str(raw).replace("Z", "+00:00"))
    except Exception: return None

def _merge_checkin_history(local_history, cloud_history):
    local_history = _coerce_dict(local_history)
    cloud_history = _coerce_dict(cloud_history)
    merged = {}
    for source in (cloud_history, local_history):
        for raw_key, entry in source.items():
            day = _canonical_day_key(raw_key)
            if day not in merged:
                merged[day] = entry
                continue
            existing_ts = _entry_timestamp(merged[day])
            new_ts = _entry_timestamp(entry)
            if new_ts and existing_ts and new_ts < existing_ts:
                merged[day] = entry
    return merged

# ==============================================================================
# RETRY HELPER 
# ==============================================================================
def _with_retries(fn, attempts=3, base_delay=1.5, on_retry=None):
    last_exc = None
    for attempt in range(1, attempts + 1):
        try: return fn()
        except Exception as e:
            last_exc = e
            if attempt < attempts:
                if on_retry:
                    try: on_retry(attempt, attempts, e)
                    except Exception: pass
                time.sleep(base_delay * attempt)
    raise last_exc

# ==============================================================================
# ROBUST THREAD-SAFE LOGGING HANDLER
# ==============================================================================
class TkinterLogHandler(logging.Handler):
    """Pipes background thread logs safely into the Tkinter main loop via Queue"""
    def __init__(self, gui_queue, treeview):
        super().__init__()
        self.gui_queue = gui_queue
        self.treeview = treeview

    def emit(self, record):
        try: msg = self.format(record)
        except Exception: msg = record.getMessage()
        time_str = datetime.fromtimestamp(record.created).strftime('%H:%M:%S')

        tag = 'info'
        if record.levelno >= logging.ERROR: tag = 'error'
        elif record.levelno >= logging.WARNING: tag = 'warning'

        self.gui_queue.put(lambda: self._insert_log(time_str, record.levelname, msg, tag))

    def _insert_log(self, time_str, level, msg, tag):
        try:
            self.treeview.insert('', END, values=(time_str, level, msg), tags=(tag,))
            self.treeview.yview_moveto(1)
        except Exception: pass

logging.basicConfig(
    filename=os.path.join(LOG_DIR, 'sync.log'),
    level=logging.INFO,
    format='%(asctime)s [%(levelname)s] %(message)s'
)

def load_supabase_client() -> Client:
    if not os.path.exists(SECRETS_PATH): raise FileNotFoundError("Supabase credentials missing.")
    try:
        with open(SECRETS_PATH, 'r') as f: secrets = json.load(f)
    except json.JSONDecodeError as e:
        raise ValueError(f"config/secrets.json is not valid JSON: {e}")

    url = secrets.get("SUPABASE_URL")
    key = secrets.get("SUPABASE_KEY")
    if not url or not key: raise ValueError("SUPABASE_URL / SUPABASE_KEY are empty.")
    return create_client(url, key)

class SyncState(enum.Enum):
    IDLE = "IDLE"
    SYNCING = "SYNCING"
    ERROR = "ERROR"

# ==============================================================================
# HELPERS
# ==============================================================================
def _format_day_label(iso_date):
    try:
        dt = datetime.strptime(iso_date, "%Y-%m-%d")
        return f"📅 {dt.day} {dt.strftime('%B').upper()} {dt.year}"
    except Exception: return f"📅 {iso_date}"

def _fmt_dt(value):
    if not value: return "—"
    if isinstance(value, str):
        try: value = datetime.fromisoformat(value)
        except Exception: return value
    return value.strftime("%d %b %Y, %H:%M") + " UTC"

def _relative_time(dt):
    if not dt: return "Never"
    now = datetime.now(timezone.utc)
    if dt.tzinfo is None: dt = dt.replace(tzinfo=timezone.utc)
    seconds = int((now - dt).total_seconds())
    if seconds < 5: return "Just now"
    if seconds < 60: return f"{seconds}s ago"
    minutes = seconds // 60
    if minutes < 60: return f"{minutes}m ago"
    hours = minutes // 60
    if hours < 24: return f"{hours}h ago"
    return f"{hours // 24}d ago"

def _fields_summary(diff_fields):
    names = list(diff_fields.keys())
    if len(names) <= 3: return ", ".join(names)
    return ", ".join(names[:3]) + f" +{len(names) - 3} more"

def _compute_sync_health(stats):
    total = stats.get("mysql_total", 0) + stats.get("kiosk_reg", 0)
    if total == 0: return 100
    problem = stats.get("pending_push", 0) + stats.get("conflict_count", 0)
    healthy = max(total - problem, 0)
    return round((healthy / total) * 100)

# ==============================================================================
# CORE SYNC MANAGER
# ==============================================================================
class SyncManager:
    def __init__(self):
        self.SessionMySQL = None
        self.SessionSQLite = None
        self.state = SyncState.IDLE
        self.last_error = None
        self.last_sync_at = self._load_last_sync()
        self._lock = threading.Lock()   
        self.connect_local_dbs()
        self.conflicts = self._load_conflicts()

    def connect_local_dbs(self):
        try:
            sessions = get_database_sessions()
            self.SessionMySQL = sessions.get('mysql')
            self.SessionSQLite = sessions.get('sqlite')
            logging.info("Local databases verified. Cloud connection is IDLE.")
        except Exception as e:
            logging.error(f"Local database connection failed: {e}")

    def _load_conflicts(self):
        if not os.path.exists(CONFLICTS_PATH): return {}
        try:
            with open(CONFLICTS_PATH, 'r') as f: raw = json.load(f)
            for c in raw.values():
                for key in ("local_updated_at", "cloud_updated_at", "detected_at"):
                    if isinstance(c.get(key), str):
                        try: c[key] = datetime.fromisoformat(c[key])
                        except ValueError: pass
            return raw
        except Exception as e:
            logging.error(f"Failed to load conflicts registry: {e}")
            return {}

    def _save_conflicts(self):
        try:
            serializable = {}
            for cid, c in self.conflicts.items():
                entry = dict(c)
                for key in ("local_updated_at", "cloud_updated_at", "detected_at"):
                    if isinstance(entry.get(key), datetime):
                        entry[key] = entry[key].isoformat()
                serializable[cid] = entry
            tmp_path = CONFLICTS_PATH + ".tmp"
            with open(tmp_path, 'w') as f: json.dump(serializable, f, indent=2, default=str)
            os.replace(tmp_path, CONFLICTS_PATH)  
        except Exception as e:
            logging.error(f"Failed to save conflicts registry: {e}")

    def _load_last_sync(self):
        if os.path.exists(SYNC_STATE_PATH):
            try:
                with open(SYNC_STATE_PATH, 'r') as f: data = json.load(f)
                ts = data.get("last_sync_at")
                if ts: return datetime.fromisoformat(ts)
            except Exception: pass
        return None

    def _record_sync_success(self):
        self.last_sync_at = datetime.now(timezone.utc)
        try:
            tmp_path = SYNC_STATE_PATH + ".tmp"
            with open(tmp_path, 'w') as f: json.dump({"last_sync_at": self.last_sync_at.isoformat()}, f)
            os.replace(tmp_path, SYNC_STATE_PATH)
        except Exception as e:
            logging.error(f"Failed to persist last sync timestamp: {e}")

    def _compute_diff(self, local_record, cloud_data):
        diff = {}
        for field in COMPARABLE_FIELDS:
            local_val = getattr(local_record, field, None)
            local_val = local_val.name if hasattr(local_val, "name") else local_val
            cloud_val = cloud_data.get(field)
            if (local_val or None) != (cloud_val or None):
                diff[field] = {"local": local_val, "cloud": cloud_val}
        return diff

    def _apply_cloud_fields(self, local_record, cloud_data, cloud_updated_at):
        local_record.full_name = cloud_data.get('full_name') or local_record.full_name
        local_record.mobile = cloud_data.get('mobile') or local_record.mobile
        local_record.email = cloud_data.get('email') or local_record.email
        local_record.gender = cloud_data.get('gender') or local_record.gender
        local_record.attendee_type = cloud_data.get('attendee_type') or local_record.attendee_type
        local_record.business_name = cloud_data.get('business_name') or local_record.business_name
        local_record.business_category = cloud_data.get('business_category') or local_record.business_category
        local_record.other_category = cloud_data.get('other_category') or local_record.other_category
        local_record.address = cloud_data.get('address') or local_record.address
        local_record.city = cloud_data.get('city') or local_record.city
        local_record.state = cloud_data.get('state') or local_record.state
        local_record.pincode = cloud_data.get('pincode') or local_record.pincode
        local_record.photo_url = cloud_data.get('photo_url') or local_record.photo_url
        
        local_record.needs_sheet_sync = cloud_data.get('needs_sheet_sync', local_record.needs_sheet_sync)
        if hasattr(local_record, 'needs_local_sync'): local_record.needs_local_sync = False
            
        local_record.updated_at = cloud_updated_at
        local_record.needs_cloud_sync = False

    def _build_push_payload(self, record):
        return {
            "id": record.id,
            "attendee_id": record.attendee_id,
            "full_name": record.full_name,
            "mobile": record.mobile,
            "email": record.email,
            "gender": record.gender.name if hasattr(record.gender, 'name') else record.gender,
            "attendee_type": record.attendee_type.name if hasattr(record.attendee_type, 'name') else record.attendee_type,
            "business_name": record.business_name,
            "business_category": record.business_category,
            "other_category": record.other_category,
            "address": record.address,
            "city": record.city,
            "state": record.state,
            "pincode": record.pincode,
            "attendance_days": _coerce_list(record.attendance_days),
            "photo_url": record.photo_url,
            "checkin_history": _coerce_dict(record.checkin_history),
            "needs_sheet_sync": getattr(record, 'needs_sheet_sync', True),
            "needs_local_sync": False, 
            "needs_cloud_sync": False,
            "created_at": record.created_at.isoformat() if record.created_at else datetime.now(timezone.utc).isoformat(),
            "updated_at": record.updated_at.isoformat() if record.updated_at else datetime.now(timezone.utc).isoformat(),
        }

    def mirror_mysql_to_sqlite(self):
        if not self.SessionSQLite or not self.SessionMySQL: return
        logging.info("Mirroring MySQL -> SQLite...")
        mysql_session = self.SessionMySQL()
        sqlite_session = self.SessionSQLite()
        try:
            mysql_attendees = mysql_session.query(Attendee).all()
            att_dicts = [{c.name: getattr(m, c.name) for c in m.__table__.columns} for m in mysql_attendees]
            sqlite_session.query(Attendee).delete()
            if att_dicts: sqlite_session.bulk_insert_mappings(Attendee, att_dicts)

            mysql_kiosk = mysql_session.query(OfflineKioskAttendee).all()
            kiosk_dicts = [{c.name: getattr(m, c.name) for c in m.__table__.columns} for m in mysql_kiosk]
            sqlite_session.query(OfflineKioskAttendee).delete()
            if kiosk_dicts: sqlite_session.bulk_insert_mappings(OfflineKioskAttendee, kiosk_dicts)

            sqlite_session.commit()
            logging.info(f"Mirror complete: {len(att_dicts)} + {len(kiosk_dicts)} records backed up.")
        except Exception as e:
            sqlite_session.rollback()
            logging.error(f"SQLite mirror failed: {e}")
        finally:
            mysql_session.close(); sqlite_session.close()

    def push_to_cloud(self):
        if not self._lock.acquire(blocking=False):
            logging.warning("Push skipped — another sync operation is already running.")
            return False
        try: return self._push_to_cloud_locked()
        finally: self._lock.release()

    def _push_to_cloud_locked(self):
        logging.info("--- Starting PUSH to Cloud ---")
        self.state = SyncState.SYNCING
        if not self.SessionMySQL:
            msg = "Local MySQL is offline."
            logging.error(f"Cannot push: {msg}")
            self.state = SyncState.ERROR
            self.last_error = msg
            return False

        session = self.SessionMySQL()
        try:
            pending = (session.query(Attendee).filter_by(needs_cloud_sync=True).all() +
                       session.query(OfflineKioskAttendee).filter_by(needs_cloud_sync=True).all())

            if not pending:
                logging.info("No records require pushing.")
                self.state = SyncState.IDLE
                self.last_error = None
                self._record_sync_success()
                return True

            pushable = [r for r in pending if str(r.id) not in self.conflicts]
            blocked_count = len(pending) - len(pushable)
            if blocked_count: logging.warning(f"{blocked_count} record(s) skipped — resolve their conflicts first.")

            if not pushable:
                self.state = SyncState.IDLE
                self.last_error = None
                self._record_sync_success()
                return True

            snapshot = {r.id: (r.updated_at, self._build_push_payload(r), isinstance(r, OfflineKioskAttendee)) for r in pushable}
        except Exception as e:
            session.rollback()
            msg = f"Push preparation error: {e}"
            logging.error(msg)
            self.state = SyncState.ERROR; self.last_error = msg
            return False
        finally:
            session.close()

        try: supabase = load_supabase_client()
        except Exception as e:
            msg = f"Could not reach Supabase: {e}"
            logging.error(msg)
            self.state = SyncState.ERROR; self.last_error = msg
            return False

        payloads = [p for (_, p, _) in snapshot.values()]
        pushed_ids, failed_ids = self._upsert_with_fallback(supabase, payloads)

        if not pushed_ids and failed_ids:
            msg = f"Cloud rejected all {len(failed_ids)} record(s)."
            logging.error(msg)
            self.state = SyncState.ERROR; self.last_error = msg
            return False

        session = self.SessionMySQL()
        cleared, changed_again = 0, 0
        try:
            for record_id in pushed_ids:
                captured_updated_at, _, is_kiosk = snapshot[record_id]
                model = OfflineKioskAttendee if is_kiosk else Attendee
                fresh = session.query(model).filter_by(id=record_id).with_for_update().first()
                if not fresh: continue
                if fresh.updated_at == captured_updated_at:
                    fresh.needs_cloud_sync = False
                    cleared += 1
                else: changed_again += 1
            session.commit()
        except Exception as e:
            session.rollback()
            msg = f"Push finalize error: {e}"
            logging.error(msg)
            self.state = SyncState.ERROR; self.last_error = msg
            return False
        finally: session.close()

        summary = f"Pushed {cleared} record(s)."
        if changed_again: summary += f" {changed_again} changed mid-sync."
        if failed_ids: logging.warning(summary + f" {len(failed_ids)} failed.")
        else: logging.info(summary)

        self.mirror_mysql_to_sqlite()
        self.state = SyncState.IDLE; self.last_error = None
        self._record_sync_success()
        return True

    def _upsert_with_fallback(self, supabase, payloads):
        if not payloads: return [], []
        try:
            def _do_batch(): return supabase.table('attendees').upsert(payloads).execute()
            response = _with_retries(_do_batch, attempts=PUSH_BATCH_RETRIES, base_delay=2, on_retry=lambda a, t, e: logging.warning(f"Push attempt {a}/{t} failed: {e}"))
            if response.data: return [p["id"] for p in payloads], []
        except Exception as e:
            logging.warning(f"Batch push failed ({e}); isolating record-by-record.")

        pushed_ids, failed_ids = [], []
        for payload in payloads:
            try:
                def _do_one(): return supabase.table('attendees').upsert([payload]).execute()
                response = _with_retries(_do_one, attempts=2, base_delay=1.5)
                if response.data: pushed_ids.append(payload["id"])
                else: failed_ids.append(payload["id"])
            except Exception as e:
                failed_ids.append(payload["id"])
                logging.error(f"Failed to push {payload.get('attendee_id')}: {e}")
        return pushed_ids, failed_ids

    def pull_from_cloud(self):
        if not self._lock.acquire(blocking=False):
            logging.warning("Pull skipped — another sync running.")
            return False
        try: return self._pull_from_cloud_locked()
        finally: self._lock.release()

    def _pull_from_cloud_locked(self):
        logging.info("--- Starting PULL from Cloud ---")
        self.state = SyncState.SYNCING
        if not self.SessionMySQL:
            msg = "Local MySQL is offline."
            logging.error(msg)
            self.state = SyncState.ERROR; self.last_error = msg
            return False

        try: supabase = load_supabase_client()
        except Exception as e:
            msg = f"Could not reach Supabase: {e}"
            logging.error(msg)
            self.state = SyncState.ERROR; self.last_error = msg
            return False

        try: cloud_records = self._fetch_all_cloud_records(supabase)
        except Exception as e:
            msg = f"Could not fetch data: {e}"
            logging.error(msg)
            self.state = SyncState.ERROR; self.last_error = msg
            return False

        self.conflicts = {}
        if not cloud_records:
            logging.info("Cloud has no records yet.")
            self._save_conflicts()
            self.state = SyncState.IDLE; self.last_error = None
            self._record_sync_success()
            return True

        pulled = conflicts_found = skipped_errors = 0
        pulled_ids_to_clear = []

        for batch_start in range(0, len(cloud_records), PULL_COMMIT_BATCH_SIZE):
            batch = cloud_records[batch_start: batch_start + PULL_COMMIT_BATCH_SIZE]
            session = self.SessionMySQL()
            try:
                for cloud_data in batch:
                    try:
                        outcome = self._apply_one_cloud_record(session, cloud_data)
                        if outcome == "pulled":
                            pulled += 1
                            if cloud_data.get('needs_local_sync'): pulled_ids_to_clear.append(cloud_data['id'])
                        elif outcome == "conflict": conflicts_found += 1
                    except Exception as e:
                        skipped_errors += 1
                        bad_id = cloud_data.get('attendee_id') or cloud_data.get('id') or '?'
                        logging.error(f"Skipped cloud record {bad_id} due to an error: {e}")
                session.commit()
            except Exception as e:
                session.rollback()
                logging.error(f"Batch of {len(batch)} failed to commit: {e}")
            finally: session.close()

        if pulled_ids_to_clear:
            try:
                for i in range(0, len(pulled_ids_to_clear), 200):
                    chunk = pulled_ids_to_clear[i:i+200]
                    supabase.table('attendees').update({'needs_local_sync': False}).in_('id', chunk).execute()
                logging.info(f"Cleared 'needs_local_sync' flag on cloud for {len(pulled_ids_to_clear)} records.")
            except Exception as e:
                logging.error(f"Failed to clear 'needs_local_sync' on cloud: {e}")

        self._save_conflicts()
        parts = [f"{pulled} updated"]
        if conflicts_found: parts.append(f"{conflicts_found} conflict(s) need review")
        if skipped_errors: parts.append(f"{skipped_errors} skipped due to errors")
        log_fn = logging.warning if (conflicts_found or skipped_errors) else logging.info
        log_fn("Pull complete: " + ", ".join(parts) + ".")

        self.mirror_mysql_to_sqlite()
        self.state = SyncState.IDLE; self.last_error = None
        self._record_sync_success()
        return True

    def _fetch_all_cloud_records(self, supabase):
        records = []
        page_size = 1000
        offset = 0
        while True:
            def _do_fetch(): return supabase.table('attendees').select("*").range(offset, offset + page_size - 1).execute()
            response = _with_retries(_do_fetch, attempts=PULL_PAGE_RETRIES, base_delay=2, on_retry=lambda a, t, e: logging.warning(f"Fetch page failed (attempt {a}/{t}): {e}"))
            data = response.data
            if not data: break
            records.extend(data)
            if len(data) < page_size: break
            offset += page_size
        return records

    def _parse_cloud_timestamp(self, raw):
        if raw:
            try: return datetime.fromisoformat(str(raw).replace('Z', '+00:00')).replace(tzinfo=None)
            except Exception: pass
        return datetime.now(timezone.utc).replace(tzinfo=None)

    def _apply_one_cloud_record(self, session, cloud_data):
        cloud_id = cloud_data.get('id')
        if not cloud_id: raise ValueError("cloud record is missing its 'id' field")

        local_record = session.query(Attendee).filter_by(id=cloud_id).with_for_update().first()
        if not local_record:
            local_record = session.query(OfflineKioskAttendee).filter_by(id=cloud_id).with_for_update().first()

        cloud_updated_at = self._parse_cloud_timestamp(cloud_data.get('updated_at'))
        cloud_created_at = self._parse_cloud_timestamp(cloud_data.get('created_at'))

        if local_record:
            local_record.checkin_history = _merge_checkin_history(local_record.checkin_history, cloud_data.get('checkin_history', {}))
            flag_modified(local_record, "checkin_history")

            if local_record.needs_cloud_sync:
                local_updated = local_record.updated_at
                if local_updated and cloud_updated_at > local_updated:
                    diff_fields = self._compute_diff(local_record, cloud_data)
                    if diff_fields:
                        conflict_id = str(local_record.id)
                        self.conflicts[conflict_id] = {
                            "id": conflict_id, "attendee_id": local_record.attendee_id,
                            "full_name": local_record.full_name, "local_updated_at": local_updated,
                            "cloud_updated_at": cloud_updated_at, "detected_at": datetime.now(timezone.utc),
                            "cloud_snapshot": cloud_data, "diff_fields": diff_fields,
                        }
                        return "conflict"
                return "skipped" 

            if not local_record.updated_at or cloud_updated_at > local_record.updated_at:
                self._apply_cloud_fields(local_record, cloud_data, cloud_updated_at)
                if not local_record.created_at: local_record.created_at = cloud_created_at
                return "pulled"
            return "skipped"

        new_attendee = Attendee(
            id=cloud_id, attendee_id=cloud_data.get('attendee_id') or str(cloud_id),
            full_name=cloud_data.get('full_name') or 'Unknown', mobile=cloud_data.get('mobile') or '0000000000',
            email=cloud_data.get('email'), gender=cloud_data.get('gender') or 'OTHER',
            attendee_type=cloud_data.get('attendee_type') or 'GENERAL', business_name=cloud_data.get('business_name'),
            business_category=cloud_data.get('business_category'), other_category=cloud_data.get('other_category'),
            address=cloud_data.get('address') or 'N/A', city=cloud_data.get('city') or 'N/A',
            state=cloud_data.get('state') or 'N/A', pincode=cloud_data.get('pincode') or '000000',
            attendance_days=_coerce_list(cloud_data.get('attendance_days')), photo_url=cloud_data.get('photo_url'),
            checkin_history=_merge_checkin_history({}, cloud_data.get('checkin_history')),
            created_at=cloud_created_at, updated_at=cloud_updated_at, needs_cloud_sync=False,
            needs_sheet_sync=cloud_data.get('needs_sheet_sync', False), needs_local_sync=False,  
            local_modified=False, device_name=None,
        )
        session.add(new_attendee)
        return "pulled"

    def trigger_full_sync(self):
        if not self._lock.acquire(blocking=False):
            logging.warning("Full sync skipped — another sync operation is already running.")
            return False
        try:
            logging.info("--- Starting FULL SYNC (Pull -> Push) ---")
            if not self._pull_from_cloud_locked():
                logging.error("Full sync aborted: pull stage failed.")
                return False
            return self._push_to_cloud_locked()
        finally: self._lock.release()

    def get_pending_conflicts(self):
        fallback = datetime.min.replace(tzinfo=timezone.utc)
        return sorted(self.conflicts.values(), key=lambda c: c.get("detected_at") or fallback, reverse=True)

    def resolve_conflict(self, conflict_id, keep):
        conflict = self.conflicts.get(conflict_id)
        if not conflict: return False
        if not self.SessionMySQL: return False

        session = self.SessionMySQL()
        try:
            record = session.query(Attendee).filter_by(id=conflict_id).with_for_update().first()
            if not record: record = session.query(OfflineKioskAttendee).filter_by(id=conflict_id).with_for_update().first()

            if not record:
                del self.conflicts[conflict_id]; self._save_conflicts()
                return True

            if keep == "cloud":
                self._apply_cloud_fields(record, conflict["cloud_snapshot"], conflict["cloud_updated_at"])
                logging.info(f"Conflict resolved for {record.attendee_id}: kept CLOUD version.")
            elif keep == "local":
                record.needs_cloud_sync = True
                logging.info(f"Conflict resolved for {record.attendee_id}: kept LOCAL version.")
            else: return False

            session.commit()
            del self.conflicts[conflict_id]; self._save_conflicts()
            return True
        except Exception as e:
            session.rollback()
            logging.error(f"Failed to resolve conflict: {e}")
            return False
        finally: session.close()

    def resolve_all_conflicts(self, strategy="newest"):
        resolved = 0
        for conflict_id, conflict in list(self.conflicts.items()):
            if strategy in ("local", "cloud"): keep = strategy
            else: keep = "cloud" if conflict["cloud_updated_at"] > conflict["local_updated_at"] else "local"
            if self.resolve_conflict(conflict_id, keep): resolved += 1
        return resolved

    def get_dashboard_stats(self):
        empty = {
            "mysql_total": 0, "sqlite_total": 0, "pending_push": 0, "kiosk_reg": 0,
            "conflict_count": len(self.conflicts), "checked_in": 0,
            "day_counts": {d: 0 for d in EVENT_DAYS},
        }
        if not self.SessionMySQL: return empty

        mysql_session = self.SessionMySQL()
        sqlite_session = self.SessionSQLite() if self.SessionSQLite else None
        
        try:
            total_att = mysql_session.query(Attendee).count()
            kiosk_regs = mysql_session.query(OfflineKioskAttendee).count()
            
            pending_main = mysql_session.query(Attendee).filter_by(needs_cloud_sync=True).count()
            pending_kiosk = mysql_session.query(OfflineKioskAttendee).filter_by(needs_cloud_sync=True).count()
            pending_push = pending_main + pending_kiosk

            total_sqlite = 0
            if sqlite_session:
                total_sqlite = sqlite_session.query(Attendee).count() + sqlite_session.query(OfflineKioskAttendee).count()

            day_counts = {d: 0 for d in EVENT_DAYS}
            checked_in = 0
            
            portal_to_iso = {}
            for iso_day in EVENT_DAYS:
                dt = datetime.strptime(iso_day, "%Y-%m-%d")
                portal_to_iso[f"{dt.day} {dt.strftime('%B')}"] = iso_day

            for human_date, iso_date in portal_to_iso.items():
                c_main = mysql_session.query(Attendee).filter((Attendee.checkin_history.like(f'%"{human_date}"%')) | (Attendee.checkin_history.like(f'%"{iso_date}"%'))).count()
                c_kiosk = mysql_session.query(OfflineKioskAttendee).filter((OfflineKioskAttendee.checkin_history.like(f'%"{human_date}"%')) | (OfflineKioskAttendee.checkin_history.like(f'%"{iso_date}"%'))).count()
                day_sum = c_main + c_kiosk
                day_counts[iso_date] = day_sum
                checked_in += day_sum

            return {
                "mysql_total": total_att, "sqlite_total": total_sqlite,
                "pending_push": pending_push, "kiosk_reg": kiosk_regs,
                "conflict_count": len(self.conflicts), "checked_in": checked_in,
                "day_counts": day_counts,
            }
        except Exception as e:
            logging.error(f"Stat refresh failed: {e}")
            return empty
        finally:
            mysql_session.close()
            if sqlite_session: sqlite_session.close()


# ==============================================================================
# CONFIGURATION GUI DIALOG
# ==============================================================================
class ConfigDialog(ttk.Toplevel):
    def __init__(self, parent):
        super().__init__()
        self.title("Configure Databases")
        self.transient(parent)
        self.geometry("560x720")
        self.position_center()

        self.secrets = {}
        if os.path.exists(SECRETS_PATH):
            try:
                with open(SECRETS_PATH, 'r') as f: self.secrets = json.load(f)
            except Exception: pass

        self.schema = {"mysql": {}, "sqlite": {}}
        if os.path.exists(SCHEMA_PATH):
            try:
                with open(SCHEMA_PATH, 'r') as f: self.schema = json.load(f)
            except Exception: pass

        outer = ttk.Frame(self, padding=25)
        outer.pack(fill=BOTH, expand=True)

        ttk.Label(outer, text="Configure Databases", font="-size 16 -weight bold").pack(anchor=W)
        ttk.Label(outer, text="Run this once per machine — it writes config/secrets.json and config/schema.json.", bootstyle=SECONDARY, font="-size 9").pack(anchor=W, pady=(0, 15))

        sb_card = ttk.Labelframe(outer, text=" Supabase Cloud ", padding=15)
        sb_card.pack(fill=X, pady=(0, 15))
        self.ent_sb_url = self._make_input(sb_card, "SUPABASE_URL", self.secrets.get("SUPABASE_URL", ""))
        self.ent_sb_key = self._make_input(sb_card, "SUPABASE_KEY", self.secrets.get("SUPABASE_KEY", ""), show="*")

        sb_test_row = ttk.Frame(sb_card)
        sb_test_row.pack(fill=X, pady=(4, 0))
        ttk.Button(sb_test_row, text="Test Connection", bootstyle="outline-info", command=self.test_supabase).pack(side=LEFT)
        self.lbl_sb_test = ttk.Label(sb_test_row, text="")
        self.lbl_sb_test.pack(side=LEFT, padx=10)

        my_card = ttk.Labelframe(outer, text=" MySQL (Local Hub) ", padding=15)
        my_card.pack(fill=X, pady=(0, 15))
        my_conf = self.schema.get("mysql", {})
        self.ent_my_host = self._make_input(my_card, "Host", my_conf.get("host", "localhost"))
        self.ent_my_user = self._make_input(my_card, "User", my_conf.get("user", "root"))
        self.ent_my_pass = self._make_input(my_card, "Password", my_conf.get("password", ""), show="*")
        self.ent_my_db = self._make_input(my_card, "Database", my_conf.get("database", "eventhub_db"))

        my_test_row = ttk.Frame(my_card)
        my_test_row.pack(fill=X, pady=(4, 0))
        ttk.Button(my_test_row, text="Test Connection", bootstyle="outline-info", command=self.test_mysql).pack(side=LEFT)
        self.lbl_my_test = ttk.Label(my_test_row, text="")
        self.lbl_my_test.pack(side=LEFT, padx=10)

        btn_frame = ttk.Frame(outer)
        btn_frame.pack(fill=X, pady=(10, 0), side=BOTTOM)
        ttk.Button(btn_frame, text="Save Settings", bootstyle=SUCCESS, command=self.save).pack(side=RIGHT, padx=5)
        ttk.Button(btn_frame, text="Cancel", bootstyle=SECONDARY, command=self.destroy).pack(side=RIGHT)

    def _make_input(self, parent, label, default, show=None):
        row = ttk.Frame(parent)
        row.pack(fill=X, pady=3)
        ttk.Label(row, text=label, width=15).pack(side=LEFT)
        ent = ttk.Entry(row, show=show or "")
        ent.insert(0, default)
        ent.pack(side=LEFT, fill=X, expand=True)

        if show:
            def toggle():
                if ent.cget('show') == '':
                    ent.configure(show=show); toggle_btn.configure(text="Show")
                else:
                    ent.configure(show=''); toggle_btn.configure(text="Hide")
            toggle_btn = ttk.Button(row, text="Show", bootstyle="outline-secondary", width=5, command=toggle)
            toggle_btn.pack(side=LEFT, padx=(5, 0))
        return ent

    def test_supabase(self):
        url, key = self.ent_sb_url.get().strip(), self.ent_sb_key.get().strip()
        if not url or not key:
            self.lbl_sb_test.configure(text="Enter URL and key first.", bootstyle=WARNING)
            return
        self.lbl_sb_test.configure(text="Testing...", bootstyle=SECONDARY)
        threading.Thread(target=self._test_supabase_thread, args=(url, key), daemon=True).start()

    def _test_supabase_thread(self, url, key):
        try:
            client = create_client(url, key)
            client.table('attendees').select('id').limit(1).execute()
            self.after(0, lambda: self.lbl_sb_test.configure(text="Connected successfully.", bootstyle=SUCCESS))
        except Exception as e:
            err = str(e)[:70]
            self.after(0, lambda: self.lbl_sb_test.configure(text=f"Failed: {err}", bootstyle=DANGER))

    def test_mysql(self):
        host, user, password, db = self.ent_my_host.get().strip(), self.ent_my_user.get().strip(), self.ent_my_pass.get().strip(), self.ent_my_db.get().strip()
        if not host or not user or not db:
            self.lbl_my_test.configure(text="Enter host, user, and database first.", bootstyle=WARNING)
            return
        self.lbl_my_test.configure(text="Testing...", bootstyle=SECONDARY)
        threading.Thread(target=self._test_mysql_thread, args=(host, user, password, db), daemon=True).start()

    def _test_mysql_thread(self, host, user, password, db):
        try:
            url = f"mysql+mysqldb://{user}:{password}@{host}:3306/{db}"
            engine = create_engine(url, connect_args={"connect_timeout": 5})
            with engine.connect(): pass
            self.after(0, lambda: self.lbl_my_test.configure(text="Connected successfully.", bootstyle=SUCCESS))
        except Exception as e:
            err = str(e)[:70]
            self.after(0, lambda: self.lbl_my_test.configure(text=f"Failed: {err}", bootstyle=DANGER))

    def save(self):
        try:
            with open(SECRETS_PATH, 'w') as f: json.dump({"SUPABASE_URL": self.ent_sb_url.get().strip(), "SUPABASE_KEY": self.ent_sb_key.get().strip()}, f, indent=4)
            self.schema.setdefault("mysql", {}); self.schema.setdefault("sqlite", {})
            self.schema["mysql"].update({"host": self.ent_my_host.get().strip(), "user": self.ent_my_user.get().strip(), "password": self.ent_my_pass.get().strip(), "database": self.ent_my_db.get().strip(), "port": self.schema["mysql"].get("port", 3306), "enabled": True})
            self.schema["sqlite"].update({"enabled": True, "folder_name": self.schema["sqlite"].get("folder_name", "db"), "file_name": self.schema["sqlite"].get("file_name", "eventhub_local.db")})
            with open(SCHEMA_PATH, 'w') as f: json.dump(self.schema, f, indent=4)
        except Exception as e:
            Messagebox.show_warning(f"Couldn't save settings: {e}", "Save Failed", parent=self)
            return
        Messagebox.show_info("Settings saved. Re-initializing local connections.", "Saved", parent=self)
        self.master.reinitialize_manager()
        self.destroy()


# ==============================================================================
# CONFLICT DETAIL DIALOG
# ==============================================================================
class ConflictDetailDialog(ttk.Toplevel):
    def __init__(self, parent, conflict, on_resolve):
        super().__init__()
        self.title(f"Conflict — {conflict.get('attendee_id', '')}")
        self.transient(parent)
        self.geometry("660x520")
        self.position_center()
        self.on_resolve = on_resolve
        self.conflict_id = conflict["id"]

        frame = ttk.Frame(self, padding=22)
        frame.pack(fill=BOTH, expand=True)

        ttk.Label(frame, text=conflict.get("full_name", "Unknown"), font="-size 15 -weight bold").pack(anchor=W)
        ttk.Label(frame, text=f"Attendee ID: {conflict.get('attendee_id', '')}", bootstyle=SECONDARY).pack(anchor=W, pady=(0, 6))
        ttk.Label(frame, text=f"Local last changed {_fmt_dt(conflict['local_updated_at'])}   ·   Cloud last changed {_fmt_dt(conflict['cloud_updated_at'])}", bootstyle=SECONDARY).pack(anchor=W, pady=(0, 16))
        ttk.Separator(frame).pack(fill=X, pady=(0, 10))

        header_row = ttk.Frame(frame)
        header_row.pack(fill=X)
        ttk.Label(header_row, text="FIELD", width=18, font="-weight bold").pack(side=LEFT)
        ttk.Label(header_row, text="LOCAL", width=24, font="-weight bold", bootstyle=SUCCESS).pack(side=LEFT)
        ttk.Label(header_row, text="CLOUD", width=24, font="-weight bold", bootstyle=INFO).pack(side=LEFT)

        rows_frame = ttk.Frame(frame)
        rows_frame.pack(fill=BOTH, expand=True, pady=(6, 16))

        diff_fields = conflict.get("diff_fields", {})
        for i, (field, values) in enumerate(diff_fields.items()):
            row = ttk.Frame(rows_frame, bootstyle=(SECONDARY if i % 2 else DEFAULT))
            row.pack(fill=X, pady=1)
            ttk.Label(row, text=field.replace("_", " ").title(), width=18).pack(side=LEFT, pady=3)
            ttk.Label(row, text=str(values.get("local") or "—"), width=24, wraplength=170).pack(side=LEFT, pady=3)
            ttk.Label(row, text=str(values.get("cloud") or "—"), width=24, wraplength=170).pack(side=LEFT, pady=3)

        btn_frame = ttk.Frame(frame)
        btn_frame.pack(fill=X, side=BOTTOM, pady=(10, 0))
        ttk.Button(btn_frame, text="Cancel", bootstyle=SECONDARY, command=self.destroy).pack(side=RIGHT)
        ttk.Button(btn_frame, text="Keep Cloud", bootstyle=INFO, command=lambda: self._choose("cloud")).pack(side=RIGHT, padx=5)
        ttk.Button(btn_frame, text="Keep Local", bootstyle=SUCCESS, command=lambda: self._choose("local")).pack(side=RIGHT, padx=5)

    def _choose(self, keep):
        self.on_resolve([self.conflict_id], keep)
        self.destroy()


# ==============================================================================
# MAIN DASHBOARD GUI
# ==============================================================================
class SyncDashboard(ttk.Window):
    def __init__(self):
        super().__init__(themename="darkly", title="EventHub Portable — Sync Manager")
        
        # Center Window safely without crashing on smaller monitors
        self.update_idletasks()
        sw, sh = self.winfo_screenwidth(), self.winfo_screenheight()
        w, h = 1400, 850
        x = max(0, (sw - w) // 2)
        y = max(0, (sh - h) // 2 - 20)
        self.geometry(f"{w}x{h}+{x}+{y}")
        self.minsize(1200, 720)

        self.gui_queue = queue.Queue()
        self.sync_manager = SyncManager()
        self.is_syncing = False
        self._is_refreshing_stats = False  # Critical lock for preventing thread overload

        # --- THEME ENGINE ---
        self.is_light_theme = tk.BooleanVar(value=False)
        self.canvas_indicators = []

        # --- ANIMATION ASSETS ---
        self._spinner_frames = ["⠋", "⠙", "⠹", "⠸", "⠼", "⠴", "⠦", "⠧", "⠇", "⠏"]
        self._spinner_idx = 0

        # --- AUTO SYNC STATE MANAGER ---
        self.auto_pull_enabled = tk.BooleanVar(value=False)
        self.auto_pull_val = tk.StringVar(value="15")
        self.auto_pull_unit = tk.StringVar(value="Minutes")
        
        self.auto_push_enabled = tk.BooleanVar(value=False)
        self.auto_push_val = tk.StringVar(value="15")
        self.auto_push_unit = tk.StringVar(value="Minutes")
        
        self.next_pull_ts = 0
        self.next_push_ts = 0
        
        self.auto_pull_enabled.trace_add("write", self._recalc_pull_ts)
        self.auto_pull_val.trace_add("write", self._recalc_pull_ts)
        self.auto_pull_unit.trace_add("write", self._recalc_pull_ts)
        
        self.auto_push_enabled.trace_add("write", self._recalc_push_ts)
        self.auto_push_val.trace_add("write", self._recalc_push_ts)
        self.auto_push_unit.trace_add("write", self._recalc_push_ts)
        # -------------------------------

        self.build_ui()
        self.process_gui_queue()
        self.refresh_stats_async()
        self._schedule_periodic_refresh()
        self._auto_sync_loop() # Start automated background cycle

    def toggle_theme(self):
        """Swaps seamlessly between optimal sunlight readability and dark room low-eye-strain"""
        theme_name = "cosmo" if self.is_light_theme.get() else "darkly"
        self.style.theme_use(theme_name)
        
        # 1. Adapt dot backgrounds to match the newly swapped frame color perfectly
        bg_color = self.style.colors.bg
        for canvas in self.canvas_indicators:
            canvas.configure(bg=bg_color)
            
        # 2. Re-balance text colors inside the log area for absolute perfect contrast
        if theme_name == "cosmo":
            self.log_tree.tag_configure('info', foreground='#212529')  # Sharp black
            self.log_tree.tag_configure('warning', foreground='#d35400') # Burned orange
            self.log_tree.tag_configure('error', foreground='#c0392b') # Deep red
            self.conflict_tree.tag_configure('severe', foreground='#c0392b')
        else:
            self.log_tree.tag_configure('info', foreground='#e8e8e8') # Soft white
            self.log_tree.tag_configure('warning', foreground='#ffc046') # Bright orange
            self.log_tree.tag_configure('error', foreground='#ff5c5c') # Bright red
            self.conflict_tree.tag_configure('severe', foreground='#ff5c5c')
            
        # Force a quick UI paint refresh
        self.update_idletasks()

    def _get_seconds(self, val_str, unit_str):
        try:
            v = int(val_str)
            if v <= 0: v = 1
        except ValueError:
            v = 15 # default fallback to safely handle cleared inputs
        return v * 60 if unit_str == "Minutes" else v * 3600

    def _recalc_pull_ts(self, *args):
        if self.auto_pull_enabled.get():
            self.next_pull_ts = time.time() + self._get_seconds(self.auto_pull_val.get(), self.auto_pull_unit.get())
        else:
            self.next_pull_ts = 0

    def _recalc_push_ts(self, *args):
        if self.auto_push_enabled.get():
            self.next_push_ts = time.time() + self._get_seconds(self.auto_push_val.get(), self.auto_push_unit.get())
        else:
            self.next_push_ts = 0

    def _format_countdown(self, seconds):
        m, s = divmod(seconds, 60)
        h, m = divmod(m, 60)
        return f"{h:02d}:{m:02d}:{s:02d}" if h else f"{m:02d}:{s:02d}"

    def _auto_sync_loop(self):
        """Safely manages non-conflicting background automation execution & UI Animation"""
        now = time.time()
        
        # Advance animation frame
        self._spinner_idx = (self._spinner_idx + 1) % len(self._spinner_frames)
        spin = self._spinner_frames[self._spinner_idx]

        # --- UPDATE AUTO-PULL UI ---
        if self.auto_pull_enabled.get():
            if self.is_syncing:
                self.lbl_pull_countdown.configure(text=f"{spin} Syncing...", bootstyle=WARNING)
            else:
                rem_pull = max(0, int(self.next_pull_ts - now))
                self.lbl_pull_countdown.configure(text=f"⏱ {self._format_countdown(rem_pull)}", bootstyle=INFO)
        else:
            self.lbl_pull_countdown.configure(text="Off", bootstyle=SECONDARY)

        # --- UPDATE AUTO-PUSH UI ---
        if self.auto_push_enabled.get():
            if self.is_syncing:
                self.lbl_push_countdown.configure(text=f"{spin} Syncing...", bootstyle=WARNING)
            else:
                rem_push = max(0, int(self.next_push_ts - now))
                self.lbl_push_countdown.configure(text=f"⏱ {self._format_countdown(rem_push)}", bootstyle=SUCCESS)
        else:
            self.lbl_push_countdown.configure(text="Off", bootstyle=SECONDARY)

        # --- EXECUTE ACTIONS (If unlocked) ---
        if not self.is_syncing:
            # Prioritize Pull over Push if both conflict perfectly on the same second. 
            if self.auto_pull_enabled.get() and self.next_pull_ts and now >= self.next_pull_ts:
                self._recalc_pull_ts()
                logging.info("Auto-sync: Initiating scheduled PULL")
                self.run_pull()
            elif self.auto_push_enabled.get() and self.next_push_ts and now >= self.next_push_ts:
                self._recalc_push_ts()
                logging.info("Auto-sync: Initiating scheduled PUSH")
                self.run_push()
        
        self.after(1000, self._auto_sync_loop)

    def process_gui_queue(self):
        for _ in range(50):
            try: self.gui_queue.get_nowait()()
            except queue.Empty: break
        self.after(30, self.process_gui_queue)

    def reinitialize_manager(self):
        self.sync_manager = SyncManager()
        self.refresh_stats_async()

    def _schedule_periodic_refresh(self):
        """🛡️ ZERO-LAG AUTO-UPDATE: Runs heavy DB counting in the background."""
        if not self.is_syncing:
            self.refresh_stats_async()
        self.after(3000, self._schedule_periodic_refresh)

    def refresh_stats_async(self):
        """Fires background thread to grab DB counts. Strictly single-threaded to prevent lag."""
        if getattr(self, '_is_refreshing_stats', False): return
        self._is_refreshing_stats = True
        
        def _fetch():
            try:
                stats = self.sync_manager.get_dashboard_stats()
                self.gui_queue.put(lambda: self._apply_stats(stats))
            finally:
                self._is_refreshing_stats = False
                
        threading.Thread(target=_fetch, daemon=True).start()

    def _create_stat_card(self, parent, icon, title, initial_value, style, var_name):
        outer = ttk.Frame(parent, borderwidth=1, relief="ridge")
        outer.pack(side=LEFT, fill=BOTH, expand=True, padx=8)
        
        ttk.Frame(outer, bootstyle=style, width=4).pack(side=LEFT, fill=Y)
        
        inner = ttk.Frame(outer, padding=(18, 16))
        inner.pack(side=LEFT, fill=BOTH, expand=True)
        
        top_row = ttk.Frame(inner)
        top_row.pack(fill=X, anchor=NW)
        if icon:
            ttk.Label(top_row, text=icon, font="-size 12").pack(side=LEFT, padx=(0, 6))
        ttk.Label(top_row, text=title, font="-size 9 -weight bold", bootstyle=style).pack(side=LEFT)
        
        val_lbl = ttk.Label(inner, text=initial_value, font="-size 28 -weight bold")
        val_lbl.pack(anchor=NW, pady=(8, 0))
        
        self.stat_vars[var_name] = val_lbl

    def build_ui(self):
        self.root_container = ttk.Frame(self)
        self.root_container.pack(fill=BOTH, expand=True)

        self.build_sidebar(self.root_container)

        content = ttk.Frame(self.root_container, padding=30) # Increased to 30 for elegant modern padding
        content.pack(side=LEFT, fill=BOTH, expand=True)

        header_row = ttk.Frame(content)
        header_row.pack(fill=X, pady=(0, 20))
        
        title_box = ttk.Frame(header_row)
        title_box.pack(side=LEFT)
        ttk.Label(title_box, text="Sync Dashboard", font="-size 22 -weight bold", bootstyle=PRIMARY).pack(anchor=W)
        ttk.Label(title_box, text="TDE UP 2026", font="-size 11 -weight bold", bootstyle=SECONDARY).pack(anchor=W)

        ttk.Button(header_row, text="⟳ Refresh Data", bootstyle="outline-info", command=self.refresh_stats_async).pack(side=RIGHT)
        
        self.stat_vars = {}

        # 🛡️ RESTORED GRID UI
        cards_row1 = ttk.Frame(content)
        cards_row1.pack(fill=X, pady=(0, 15))
        self._create_stat_card(cards_row1, "👥", "MYSQL (PRIMARY)", "0", PRIMARY, "mysql_total")
        self._create_stat_card(cards_row1, "💾", "SQLITE (MIRROR)", "0", INFO, "sqlite_total")
        self._create_stat_card(cards_row1, "⏳", "PENDING PUSH", "0", WARNING, "pending_push")
        self._create_stat_card(cards_row1, "⚠", "CONFLICTS", "0", DANGER, "conflicts")
        self._create_stat_card(cards_row1, "🖥️", "KIOSK REG.", "0", SECONDARY, "kiosk_reg")

        cards_row2 = ttk.Frame(content)
        cards_row2.pack(fill=X, pady=(0, 25))
        self._create_stat_card(cards_row2, "✔", "TOTAL CHECKED IN", "0", SUCCESS, "checked_in")
        for day in EVENT_DAYS:
            self._create_stat_card(cards_row2, "📅", _format_day_label(day).replace("📅 ", ""), "0", LIGHT, f"day_{day}")

        controls_frame = ttk.Frame(content)
        controls_frame.pack(fill=X, pady=(0, 10))
        self.progress = ttk.Progressbar(controls_frame, mode='indeterminate', bootstyle=INFO)
        self.progress.pack(side=LEFT, fill=X, expand=True, padx=(0, 10))
        self.lbl_status = ttk.Label(controls_frame, text="Ready.", font="-size 10")
        self.lbl_status.pack(side=LEFT, padx=10)

        self.notebook = ttk.Notebook(content)
        self.notebook.pack(fill=BOTH, expand=True, pady=(15, 0))

        self.build_log_tab()
        self.build_conflicts_tab()
        
        # Trigger an initial theme sweep to color everything correctly
        self.toggle_theme()

    def build_sidebar(self, container):
        sidebar_outer = ttk.Frame(container, width=380) # Enhanced width for neat spacious components
        sidebar_outer.pack(side=LEFT, fill=Y)
        sidebar_outer.pack_propagate(False)

        sidebar = ttk.Frame(sidebar_outer, padding=25)
        sidebar.pack(fill=BOTH, expand=True)

        ttk.Label(sidebar, text="EventHub Portable", font="-size 18 -weight bold").pack(anchor=W)
        ttk.Label(sidebar, text="Data Synchronization", font="-size 10", bootstyle=SECONDARY).pack(anchor=W, pady=(0, 12))
        
        # Super-slick Light/Dark Mode toggle
        ttk.Checkbutton(sidebar, text="☀️ Sunlight Mode", variable=self.is_light_theme, bootstyle="round-toggle", command=self.toggle_theme).pack(anchor=W, pady=(0, 20))

        conn_frame = ttk.Labelframe(sidebar, text=" CONNECTION STATUS ", padding=15)
        conn_frame.pack(fill=X, pady=(0, 15))
        
        self.lbl_supa, self.supa_canvas, self.supa_dot = self._create_status_label(conn_frame, "Supabase Cloud: Idle", SECONDARY)
        self.lbl_mysql, self.my_canvas, self.my_dot = self._create_status_label(conn_frame, "MySQL (Primary): Checking...", INFO)
        self.lbl_sqlite, self.sq_canvas, self.sq_dot = self._create_status_label(conn_frame, "SQLite (Fallback): Checking...", INFO)

        ttk.Button(sidebar, text="⟳ Refresh Connections", bootstyle="outline-secondary", command=self.reinitialize_manager).pack(fill=X, pady=(0, 20))

        health_frame = ttk.Labelframe(sidebar, text=" SYNC HEALTH ", padding=15)
        health_frame.pack(fill=X, pady=(0, 20))
        
        meter_row = ttk.Frame(health_frame)
        meter_row.pack()
        self.health_meter = ttk.Meter(
            meter_row, metersize=150, amounttotal=100, amountused=100, bootstyle=SUCCESS, 
            subtext="synced", textright="%", stripethickness=8, meterthickness=10, interactive=False,
            textfont="-size 18 -weight bold"
        )
        self.health_meter.pack()
        self.lbl_last_sync = ttk.Label(health_frame, text="Last synced: Never", font="-size 9", bootstyle=SECONDARY)
        self.lbl_last_sync.pack(anchor=CENTER, pady=(10, 0))

        ttk.Separator(sidebar).pack(fill=X, pady=10)

        self.btn_full_sync = ttk.Button(sidebar, text="🔄 Full Sync (Pull + Push)", bootstyle=PRIMARY, command=self.run_full_sync)
        self.btn_full_sync.pack(fill=X, pady=(10, 8), ipady=6)

        pp_row = ttk.Frame(sidebar)
        pp_row.pack(fill=X, pady=(0, 8))
        self.btn_pull = ttk.Button(pp_row, text="↓ Pull Data", bootstyle=INFO, command=self.run_pull)
        self.btn_pull.pack(side=LEFT, fill=X, expand=True, padx=(0, 4), ipady=4)
        self.btn_push = ttk.Button(pp_row, text="↑ Push Data", bootstyle=SUCCESS, command=self.run_push)
        self.btn_push.pack(side=LEFT, fill=X, expand=True, padx=(4, 0), ipady=4)

        # --- AUTO SYNC SETTINGS UI ---
        auto_frame = ttk.Labelframe(sidebar, text=" AUTO SYNC SCHEDULE ", padding=12)
        auto_frame.pack(fill=X, pady=(10, 20))

        p_row = ttk.Frame(auto_frame)
        p_row.pack(fill=X, pady=6)
        ttk.Checkbutton(p_row, text="Auto Pull", variable=self.auto_pull_enabled, bootstyle="info-round-toggle", width=10).pack(side=LEFT, padx=(0,6))
        ttk.Entry(p_row, textvariable=self.auto_pull_val, width=3).pack(side=LEFT)
        ttk.Combobox(p_row, textvariable=self.auto_pull_unit, values=["Minutes", "Hours"], width=8, state="readonly").pack(side=LEFT, padx=4)
        self.lbl_pull_countdown = ttk.Label(p_row, text="Off", font="-size 9 -weight bold", bootstyle=SECONDARY)
        self.lbl_pull_countdown.pack(side=RIGHT, padx=5)

        pu_row = ttk.Frame(auto_frame)
        pu_row.pack(fill=X, pady=6)
        ttk.Checkbutton(pu_row, text="Auto Push", variable=self.auto_push_enabled, bootstyle="success-round-toggle", width=10).pack(side=LEFT, padx=(0,6))
        ttk.Entry(pu_row, textvariable=self.auto_push_val, width=3).pack(side=LEFT)
        ttk.Combobox(pu_row, textvariable=self.auto_push_unit, values=["Minutes", "Hours"], width=8, state="readonly").pack(side=LEFT, padx=4)
        self.lbl_push_countdown = ttk.Label(pu_row, text="Off", font="-size 9 -weight bold", bootstyle=SECONDARY)
        self.lbl_push_countdown.pack(side=RIGHT, padx=5)
        # -----------------------------

        ttk.Button(sidebar, text="⚙ Configure Databases", bootstyle="outline-secondary", command=lambda: ConfigDialog(self)).pack(fill=X, side=BOTTOM, pady=(20, 0))

    def _create_status_label(self, parent, text, bootstyle):
        row = ttk.Frame(parent)
        row.pack(fill=X, pady=4)
        canvas = tk.Canvas(row, width=12, height=12, bg=self.style.colors.bg, highlightthickness=0)
        self.canvas_indicators.append(canvas)
        dot = canvas.create_oval(2, 2, 10, 10, fill=self.style.colors.get(bootstyle), outline="")
        canvas.pack(side=LEFT, padx=(0, 8))
        lbl = ttk.Label(row, text=text, font="-size 10 -weight bold", bootstyle=bootstyle)
        lbl.pack(side=LEFT)
        return lbl, canvas, dot

    def _update_status_dot(self, label, canvas, dot, text, bootstyle):
        label.configure(text=text, bootstyle=bootstyle)
        
        # Dynamically grabs perfectly themed semantic colors (no more hardcoded hexes!)
        current_color = self.style.colors.get(bootstyle)
        canvas.itemconfig(dot, fill=current_color)
        
        # Tiny subtle blink effect on change
        canvas.coords(dot, 1, 1, 11, 11)
        self.after(200, lambda: canvas.coords(dot, 2, 2, 10, 10) if canvas.winfo_exists() else None)

    def build_log_tab(self):
        log_tab = ttk.Frame(self.notebook)
        self.notebook.add(log_tab, text="Activity Log")
        
        toolbar = ttk.Frame(log_tab, padding=(10, 10, 10, 5))
        toolbar.pack(fill=X)
        ttk.Button(toolbar, text="Clear Log", bootstyle="outline-secondary", command=self.clear_log).pack(side=RIGHT)

        body = ttk.Frame(log_tab)
        body.pack(fill=BOTH, expand=True, padx=10, pady=(0, 10))

        cols = ("Time", "Level", "Message")
        self.log_tree = ttk.Treeview(body, columns=cols, show="headings", bootstyle=INFO)
        self.log_tree.heading("Time", text="TIME", anchor=W)
        self.log_tree.heading("Level", text="LEVEL", anchor=W)
        self.log_tree.heading("Message", text="MESSAGE", anchor=W)
        self.log_tree.column("Time", width=100, stretch=False)
        self.log_tree.column("Level", width=100, stretch=False)

        scrollbar = ttk.Scrollbar(body, orient=VERTICAL, command=self.log_tree.yview)
        self.log_tree.configure(yscrollcommand=scrollbar.set)
        self.log_tree.pack(side=LEFT, fill=BOTH, expand=True)
        scrollbar.pack(side=RIGHT, fill=Y)

        gui_logger = TkinterLogHandler(self.gui_queue, self.log_tree)
        gui_logger.setFormatter(logging.Formatter('%(message)s'))
        logging.getLogger().addHandler(gui_logger)

    def build_conflicts_tab(self):
        conflicts_tab = ttk.Frame(self.notebook)
        self.notebook.add(conflicts_tab, text="Conflicts")
        self.conflicts_tab = conflicts_tab
        
        toolbar = ttk.Frame(conflicts_tab, padding=(10, 10, 10, 5))
        toolbar.pack(fill=X)
        ttk.Label(toolbar, text="Double-click a row to compare fields side-by-side.", bootstyle=SECONDARY).pack(side=LEFT)

        self.btn_resolve_all = ttk.Button(toolbar, text="Resolve All → Prefer Newest", bootstyle="outline-warning", command=self.resolve_all_conflicts_bulk)
        self.btn_resolve_all.pack(side=RIGHT, padx=(5, 0))
        self.btn_keep_cloud = ttk.Button(toolbar, text="Keep Cloud (Selected)", bootstyle="outline-info", command=lambda: self.resolve_selected("cloud"))
        self.btn_keep_cloud.pack(side=RIGHT, padx=5)
        self.btn_keep_local = ttk.Button(toolbar, text="Keep Local (Selected)", bootstyle="outline-success", command=lambda: self.resolve_selected("local"))
        self.btn_keep_local.pack(side=RIGHT, padx=5)

        body = ttk.Frame(conflicts_tab)
        body.pack(fill=BOTH, expand=True, padx=10, pady=(0, 10))

        cols = ("attendee_id", "full_name", "local_updated", "cloud_updated", "fields")
        self.conflict_tree = ttk.Treeview(body, columns=cols, show="headings", bootstyle=WARNING, selectmode="extended")
        headings = {"attendee_id": "ATTENDEE ID", "full_name": "NAME", "local_updated": "LOCAL UPDATED", "cloud_updated": "CLOUD UPDATED", "fields": "FIELDS DIFFERING"}
        for c in cols: self.conflict_tree.heading(c, text=headings[c], anchor=W)
        self.conflict_tree.column("attendee_id", width=140, stretch=False)
        self.conflict_tree.column("full_name", width=160, stretch=False)
        self.conflict_tree.column("local_updated", width=150, stretch=False)
        self.conflict_tree.column("cloud_updated", width=150, stretch=False)
        self.conflict_tree.bind("<Double-1>", self.on_conflict_double_click)

        self._conflict_scroll = ttk.Scrollbar(body, orient=VERTICAL, command=self.conflict_tree.yview)
        self.conflict_tree.configure(yscrollcommand=self._conflict_scroll.set)

        self.conflict_empty_label = ttk.Label(body, text="✅ No conflicts right now — everything's in sync.", font="-size 12", bootstyle=SUCCESS, anchor=CENTER, justify=CENTER)

    def _apply_stats(self, stats):
        if not self.sync_manager.SessionMySQL:
            self._update_status_dot(self.lbl_mysql, self.my_canvas, self.my_dot, "MySQL (Primary): Offline", DANGER)
            self._update_status_dot(self.lbl_sqlite, self.sq_canvas, self.sq_dot, "SQLite (Fallback): Check Config", DANGER)
            self._update_status_dot(self.lbl_supa, self.supa_canvas, self.supa_dot, "Supabase Cloud: Idle", SECONDARY)
            self.health_meter.configure(bootstyle=DANGER, amountused=0)
            self.lbl_last_sync.configure(text=f"Last synced: {_relative_time(self.sync_manager.last_sync_at)}")
            self._refresh_conflicts_ui()
            return

        self._update_status_dot(self.lbl_mysql, self.my_canvas, self.my_dot, "MySQL (Primary): Online", SUCCESS)
        if self.sync_manager.SessionSQLite:
            self._update_status_dot(self.lbl_sqlite, self.sq_canvas, self.sq_dot, "SQLite (Fallback): Ready", SUCCESS)
        else:
            self._update_status_dot(self.lbl_sqlite, self.sq_canvas, self.sq_dot, "SQLite (Fallback): Offline", DANGER)

        if self.sync_manager.state == SyncState.ERROR: 
            self._update_status_dot(self.lbl_supa, self.supa_canvas, self.supa_dot, "Supabase Cloud: Error", DANGER)
        elif not self.is_syncing: 
            self._update_status_dot(self.lbl_supa, self.supa_canvas, self.supa_dot, "Supabase Cloud: Idle", SECONDARY)

        def safe_set(key, val):
            if self.stat_vars[key].cget("text") != str(val):
                self.stat_vars[key].configure(text=str(val))
                
        safe_set("mysql_total", stats["mysql_total"])
        safe_set("sqlite_total", stats["sqlite_total"])
        safe_set("pending_push", stats["pending_push"])
        safe_set("conflicts", stats["conflict_count"])
        safe_set("kiosk_reg", stats["kiosk_reg"])
        safe_set("checked_in", stats["checked_in"])
        for day in EVENT_DAYS: 
            safe_set(f"day_{day}", stats["day_counts"].get(day, 0))

        health = _compute_sync_health(stats)
        self.health_meter.configure(bootstyle=SUCCESS if health >= 95 else (WARNING if health >= 80 else DANGER), amountused=health)
        self.lbl_last_sync.configure(text=f"Last synced: {_relative_time(self.sync_manager.last_sync_at)}")
        self._refresh_conflicts_ui()

    def _refresh_conflicts_ui(self):
        for row in self.conflict_tree.get_children(): self.conflict_tree.delete(row)
        conflicts = self.sync_manager.get_pending_conflicts()
        if not conflicts:
            self.conflict_tree.pack_forget()
            self._conflict_scroll.pack_forget()
            self.conflict_empty_label.pack(fill=BOTH, expand=True)
        else:
            self.conflict_empty_label.pack_forget()
            self.conflict_tree.pack(side=LEFT, fill=BOTH, expand=True)
            self._conflict_scroll.pack(side=RIGHT, fill=Y)
            for c in conflicts:
                self.conflict_tree.insert('', END, iid=c["id"], tags=('severe',) if len(c["diff_fields"]) >= 5 else (), values=(c["attendee_id"], c["full_name"], _fmt_dt(c["local_updated_at"]), _fmt_dt(c["cloud_updated_at"]), _fields_summary(c["diff_fields"])))
        self.notebook.tab(self.conflicts_tab, text=f"Conflicts ({len(conflicts)})" if conflicts else "Conflicts")

    def clear_log(self):
        for row in self.log_tree.get_children(): self.log_tree.delete(row)

    def _set_controls_state(self, state):
        for btn in (self.btn_pull, self.btn_push, self.btn_full_sync, self.btn_keep_local, self.btn_keep_cloud, self.btn_resolve_all):
            btn.configure(state=state)

    def _lock_ui(self, mode="syncing"):
        self.is_syncing = True
        self._set_controls_state(DISABLED)
        self.progress.start(10)
        self._update_status_dot(self.lbl_supa, self.supa_canvas, self.supa_dot, f"Supabase Cloud: {mode.title()}...", INFO)

    def _unlock_ui(self, msg="Ready."):
        self.is_syncing = False
        self._set_controls_state(NORMAL)
        self.progress.stop()
        self.lbl_status.configure(text=msg)
        self.refresh_stats_async()

    def run_push(self):
        if self.is_syncing: return
        self._lock_ui("pushing")
        self.lbl_status.configure(text="Connecting to cloud and pushing data...")
        threading.Thread(target=self._thread_push, daemon=True).start()

    def _thread_push(self):
        success = self.sync_manager.push_to_cloud()
        self.gui_queue.put(lambda: self._unlock_ui("Push Complete." if success else f"Push Failed: {self.sync_manager.last_error}"))

    def run_pull(self):
        if self.is_syncing: return
        self._lock_ui("pulling")
        self.lbl_status.configure(text="Connecting to cloud and pulling data...")
        threading.Thread(target=self._thread_pull, daemon=True).start()

    def _thread_pull(self):
        success = self.sync_manager.pull_from_cloud()
        msg = f"Pull Complete — {len(self.sync_manager.conflicts)} conflict(s) need your review." if success and self.sync_manager.conflicts else ("Pull Complete." if success else f"Pull Failed: {self.sync_manager.last_error}")
        self.gui_queue.put(lambda: self._unlock_ui(msg))

    def run_full_sync(self):
        if self.is_syncing: return
        self._lock_ui("syncing")
        self.lbl_status.configure(text="Running full sync (pull then push)...")
        threading.Thread(target=self._thread_full_sync, daemon=True).start()

    def _thread_full_sync(self):
        success = self.sync_manager.trigger_full_sync()
        msg = f"Full Sync Complete — {len(self.sync_manager.conflicts)} conflict(s) need review." if success and self.sync_manager.conflicts else ("Full Sync Complete." if success else f"Full Sync Failed: {self.sync_manager.last_error}")
        self.gui_queue.put(lambda: self._unlock_ui(msg))

    def on_conflict_double_click(self, event):
        row_id = self.conflict_tree.identify_row(event.y)
        if not row_id: return
        conflict = self.sync_manager.conflicts.get(row_id)
        if conflict: ConflictDetailDialog(self, conflict, on_resolve=self._resolve_and_refresh)

    def resolve_selected(self, keep):
        selected = self.conflict_tree.selection()
        if not selected:
            Messagebox.show_warning("Select one or more rows first.", "Nothing Selected", parent=self)
            return
        self._resolve_and_refresh(list(selected), keep)

    def resolve_all_conflicts_bulk(self):
        if not self.sync_manager.conflicts: return
        if Messagebox.yesno("Resolve all conflicts by picking the newest change automatically?", "Resolve All", parent=self) == "Yes":
            self._resolve_and_refresh(list(self.sync_manager.conflicts.keys()), "newest")

    def _resolve_and_refresh(self, conflict_ids, keep):
        if self.is_syncing: return
        self._lock_ui("syncing")
        self.lbl_status.configure(text="Applying conflict resolution...")
        threading.Thread(target=self._thread_resolve, args=(conflict_ids, keep), daemon=True).start()

    def _thread_resolve(self, conflict_ids, keep):
        resolved = 0
        for cid in conflict_ids:
            if keep == "newest" and cid in self.sync_manager.conflicts:
                c = self.sync_manager.conflicts[cid]
                self.sync_manager.resolve_conflict(cid, "cloud" if c["cloud_updated_at"] > c["local_updated_at"] else "local")
                resolved += 1
            elif self.sync_manager.resolve_conflict(cid, keep):
                resolved += 1
        if resolved: self.sync_manager.mirror_mysql_to_sqlite()
        self.gui_queue.put(lambda: self._unlock_ui(f"Resolved {resolved} conflict(s)."))


if __name__ == "__main__":
    app = SyncDashboard()
    app.mainloop()