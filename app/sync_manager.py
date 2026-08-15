import os
import sys
import json
import enum
import time
import logging
import threading
import queue
from datetime import datetime, timezone
import ctypes

import pymysql
pymysql.install_as_MySQLdb()

from supabase import create_client, Client, ClientOptions
from sqlalchemy import create_engine
from sqlalchemy.orm.attributes import flag_modified

from PySide6.QtWidgets import (QApplication, QMainWindow, QWidget, QVBoxLayout, QHBoxLayout, 
                               QGridLayout, QLabel, QPushButton, QFrame, QGroupBox, QLineEdit, 
                               QCheckBox, QComboBox, QProgressBar, QTabWidget, QTreeWidget, 
                               QTreeWidgetItem, QDialog, QMessageBox, QScrollArea, QHeaderView)
from PySide6.QtCore import Qt, QTimer
from PySide6.QtGui import QPainter, QPen, QColor, QFont, QIcon, QAction

try:
    from app.schema import Attendee, OfflineKioskAttendee, get_database_sessions
except ModuleNotFoundError:
    from schema import Attendee, OfflineKioskAttendee, get_database_sessions

def global_exception_handler(exc_type, exc_value, exc_traceback):
    logging.error("Uncaught GUI Exception intercepted. App remains running.", exc_info=(exc_type, exc_value, exc_traceback))

sys.excepthook = global_exception_handler

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

# PySide6 Theme Colors Map
COLORS = {
    "PRIMARY": "#375a7f",
    "INFO": "#0dcaf0",
    "SUCCESS": "#00bc8c",
    "WARNING": "#f39c12",
    "DANGER": "#e74c3c",
    "SECONDARY": "#888888",
    "LIGHT": "#e0e0e0",
    "DEFAULT": "transparent"
}

# ==============================================================================
# CUSTOM SPEEDOMETER WIDGET (Theme Aware)
# ==============================================================================
class SpeedometerWidget(QWidget):
    def __init__(self, parent=None, size=150):
        super().__init__(parent)
        self.setFixedSize(size, size)
        self.amountused = 100
        self.amounttotal = 100
        self.bootstyle_color = COLORS["SUCCESS"]
        self.subtext = "synced"
        self.textright = "%"
        
        # Theme colors
        self.bg_arc_color = "#333333"
        self.text_color = "#ffffff"
        self.sub_text_color = "#888888"

    def set_theme(self, is_light):
        self.bg_arc_color = "#e9ecef" if is_light else "#333333"
        self.text_color = "#212529" if is_light else "#ffffff"
        self.sub_text_color = "#6c757d" if is_light else "#aaaaaa"
        self.update()

    def configure(self, amountused=None, bootstyle=None):
        if amountused is not None:
            self.amountused = amountused
        if bootstyle is not None:
            self.bootstyle_color = COLORS.get(bootstyle, bootstyle)
        self.update()

    def paintEvent(self, event):
        painter = QPainter(self)
        painter.setRenderHint(QPainter.Antialiasing)
        
        padding = 10
        rect = self.rect().adjusted(padding, padding, -padding, -padding)
        
        pen_bg = QPen(QColor(self.bg_arc_color), 10, Qt.SolidLine, Qt.RoundCap)
        painter.setPen(pen_bg)
        painter.drawArc(rect, -225 * 16, -270 * 16)
        
        pen_fg = QPen(QColor(self.bootstyle_color), 10, Qt.SolidLine, Qt.RoundCap)
        painter.setPen(pen_fg)
        ratio = max(0.0, min(1.0, self.amountused / max(1, self.amounttotal)))
        span = int(-270 * ratio * 16)
        painter.drawArc(rect, -225 * 16, span)
        
        painter.setPen(QColor(self.text_color))
        font = QFont("Segoe UI", 22, QFont.Bold)
        painter.setFont(font)
        text = f"{int(self.amountused)}{self.textright}"
        painter.drawText(self.rect(), Qt.AlignCenter, text)
        
        font_sub = QFont("Segoe UI", 9)
        painter.setFont(font_sub)
        painter.setPen(QColor(self.sub_text_color))
        painter.drawText(self.rect().adjusted(0, 30, 0, 0), Qt.AlignCenter, self.subtext)

class AnimatedMeter:
    def __init__(self, meter_widget: SpeedometerWidget):
        self.meter = meter_widget
        self.current_val = 0.0
        self.target_val = 0.0

    def set_target(self, val):
        self.target_val = float(val)

    def tick(self):
        diff = self.target_val - self.current_val
        if abs(diff) > 0.5:
            self.current_val += diff * 0.15 
        else:
            self.current_val = self.target_val
        self.meter.configure(amountused=int(round(self.current_val)))

# ==============================================================================
# HELPER FUNCTIONS
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

class QtLogHandler(logging.Handler):
    def __init__(self, gui_queue, treeview):
        super().__init__()
        self.gui_queue = gui_queue
        self.treeview = treeview

    def emit(self, record):
        try: msg = self.format(record)
        except Exception: msg = record.getMessage()
        time_str = datetime.fromtimestamp(record.created).strftime('%H:%M:%S')
        level = record.levelname
        self.gui_queue.put(lambda: self._insert_log(time_str, level, msg))

    def _insert_log(self, time_str, level, msg):
        try:
            item = QTreeWidgetItem(self.treeview, [time_str, level, msg])
            if level in ["WARNING", "ERROR"]:
                color = COLORS["WARNING"] if level == "WARNING" else COLORS["DANGER"]
                item.setForeground(0, QColor(color))
                item.setForeground(1, QColor(color))
                item.setForeground(2, QColor(color))
            self.treeview.scrollToBottom()
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
    opts = ClientOptions(postgrest_client_timeout=15, schema='public')
    return create_client(url, key, options=opts)

class SyncState(enum.Enum):
    IDLE = "IDLE"
    SYNCING = "SYNCING"
    ERROR = "ERROR"

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
# SYNC MANAGER (Core Logic)
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
        logging.info("Mirroring MySQL -> SQLite (Optimized Bulk Insert)...")
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
# PYSIDE6 UI CLASSES
# ==============================================================================
class ConfigDialog(QDialog):
    def __init__(self, parent=None):
        super().__init__(parent)
        self.setWindowTitle("Configure Databases")
        self.resize(560, 720)
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

        layout = QVBoxLayout(self)
        layout.setContentsMargins(25, 25, 25, 25)
        
        lbl_title = QLabel("Configure Databases")
        lbl_title.setFont(QFont("Segoe UI", 16, QFont.Bold))
        layout.addWidget(lbl_title)
        
        lbl_sub = QLabel("Run this once per machine — it writes config/secrets.json and config/schema.json.")
        lbl_sub.setObjectName("SubText")
        layout.addWidget(lbl_sub)
        
        layout.addSpacing(15)

        sb_card = QGroupBox(" Supabase Cloud ")
        sb_layout = QVBoxLayout(sb_card)
        self.ent_sb_url = self._make_input(sb_layout, "SUPABASE_URL", self.secrets.get("SUPABASE_URL", ""))
        self.ent_sb_key, sb_toggle = self._make_input(sb_layout, "SUPABASE_KEY", self.secrets.get("SUPABASE_KEY", ""), is_password=True)
        
        sb_test_row = QHBoxLayout()
        btn_test_sb = QPushButton("Test Connection")
        btn_test_sb.setStyleSheet(f"QPushButton {{ border: 1px solid {COLORS['INFO']}; color: {COLORS['INFO']}; background: transparent; border-radius: 4px; padding: 4px 8px; }} QPushButton:hover {{ background-color: {COLORS['INFO']}; color: white; }}")
        btn_test_sb.clicked.connect(self.test_supabase)
        self.lbl_sb_test = QLabel("")
        sb_test_row.addWidget(btn_test_sb)
        sb_test_row.addWidget(self.lbl_sb_test)
        sb_test_row.addStretch()
        sb_layout.addLayout(sb_test_row)
        layout.addWidget(sb_card)

        my_card = QGroupBox(" MySQL (Local Hub) ")
        my_layout = QVBoxLayout(my_card)
        my_conf = self.schema.get("mysql", {})
        self.ent_my_host = self._make_input(my_layout, "Host", my_conf.get("host", "localhost"))
        self.ent_my_user = self._make_input(my_layout, "User", my_conf.get("user", "root"))
        self.ent_my_pass, my_toggle = self._make_input(my_layout, "Password", my_conf.get("password", ""), is_password=True)
        self.ent_my_db = self._make_input(my_layout, "Database", my_conf.get("database", "eventhub_db"))
        
        my_test_row = QHBoxLayout()
        btn_test_my = QPushButton("Test Connection")
        btn_test_my.setStyleSheet(f"QPushButton {{ border: 1px solid {COLORS['INFO']}; color: {COLORS['INFO']}; background: transparent; border-radius: 4px; padding: 4px 8px; }} QPushButton:hover {{ background-color: {COLORS['INFO']}; color: white; }}")
        btn_test_my.clicked.connect(self.test_mysql)
        self.lbl_my_test = QLabel("")
        my_test_row.addWidget(btn_test_my)
        my_test_row.addWidget(self.lbl_my_test)
        my_test_row.addStretch()
        my_layout.addLayout(my_test_row)
        layout.addWidget(my_card)
        
        layout.addStretch()

        btn_row = QHBoxLayout()
        btn_row.addStretch()
        btn_cancel = QPushButton("Cancel")
        btn_cancel.clicked.connect(self.reject)
        btn_save = QPushButton("Save Settings")
        btn_save.setStyleSheet(f"QPushButton {{ background-color: {COLORS['SUCCESS']}; color: white; border: none; padding: 6px 12px; border-radius: 4px; font-weight: bold; }} QPushButton:hover {{ background-color: #009670; }}")
        btn_save.clicked.connect(self.save)
        btn_row.addWidget(btn_cancel)
        btn_row.addWidget(btn_save)
        layout.addLayout(btn_row)

    def _make_input(self, parent_layout, label_text, default, is_password=False):
        row = QHBoxLayout()
        lbl = QLabel(label_text)
        lbl.setFixedWidth(120)
        ent = QLineEdit(default)
        row.addWidget(lbl)
        row.addWidget(ent)
        
        btn_toggle = None
        if is_password:
            ent.setEchoMode(QLineEdit.Password)
            btn_toggle = QPushButton("Show")
            btn_toggle.setFixedWidth(60)
            btn_toggle.setStyleSheet(f"QPushButton {{ border: 1px solid {COLORS['SECONDARY']}; background: transparent; border-radius: 4px; padding: 4px; }} QPushButton:hover {{ background-color: {COLORS['SECONDARY']}; color: white; }}")
            def toggle():
                if ent.echoMode() == QLineEdit.Password:
                    ent.setEchoMode(QLineEdit.Normal)
                    btn_toggle.setText("Hide")
                else:
                    ent.setEchoMode(QLineEdit.Password)
                    btn_toggle.setText("Show")
            btn_toggle.clicked.connect(toggle)
            row.addWidget(btn_toggle)
            
        parent_layout.addLayout(row)
        return ent if not is_password else (ent, btn_toggle)

    def test_supabase(self):
        url, key = self.ent_sb_url.text().strip(), self.ent_sb_key.text().strip()
        if not url or not key:
            self.lbl_sb_test.setText("Enter URL and key first.")
            self.lbl_sb_test.setStyleSheet(f"color: {COLORS['WARNING']};")
            return
        self.lbl_sb_test.setText("Testing...")
        self.lbl_sb_test.setStyleSheet(f"color: {COLORS['SECONDARY']};")
        threading.Thread(target=self._test_supabase_thread, args=(url, key), daemon=True).start()

    def _test_supabase_thread(self, url, key):
        try:
            client = create_client(url, key)
            client.table('attendees').select('id').limit(1).execute()
            QTimer.singleShot(0, lambda: self.lbl_sb_test.setStyleSheet(f"color: {COLORS['SUCCESS']};"))
            QTimer.singleShot(0, lambda: self.lbl_sb_test.setText("Connected successfully."))
        except Exception as e:
            err = str(e)[:70]
            QTimer.singleShot(0, lambda: self.lbl_sb_test.setStyleSheet(f"color: {COLORS['DANGER']};"))
            QTimer.singleShot(0, lambda: self.lbl_sb_test.setText(f"Failed: {err}"))

    def test_mysql(self):
        host, user, password, db = self.ent_my_host.text().strip(), self.ent_my_user.text().strip(), self.ent_my_pass.text().strip(), self.ent_my_db.text().strip()
        if not host or not user or not db:
            self.lbl_my_test.setText("Enter host, user, and database first.")
            self.lbl_my_test.setStyleSheet(f"color: {COLORS['WARNING']};")
            return
        self.lbl_my_test.setText("Testing...")
        self.lbl_my_test.setStyleSheet(f"color: {COLORS['SECONDARY']};")
        threading.Thread(target=self._test_mysql_thread, args=(host, user, password, db), daemon=True).start()

    def _test_mysql_thread(self, host, user, password, db):
        try:
            url = f"mysql+mysqldb://{user}:{password}@{host}:3306/{db}"
            engine = create_engine(url, pool_size=10, max_overflow=20, pool_pre_ping=True, connect_args={"connect_timeout": 5})
            with engine.connect(): pass
            QTimer.singleShot(0, lambda: self.lbl_my_test.setStyleSheet(f"color: {COLORS['SUCCESS']};"))
            QTimer.singleShot(0, lambda: self.lbl_my_test.setText("Connected successfully."))
        except Exception as e:
            err = str(e)[:70]
            QTimer.singleShot(0, lambda: self.lbl_my_test.setStyleSheet(f"color: {COLORS['DANGER']};"))
            QTimer.singleShot(0, lambda: self.lbl_my_test.setText(f"Failed: {err}"))

    def save(self):
        try:
            with open(SECRETS_PATH, 'w') as f: json.dump({"SUPABASE_URL": self.ent_sb_url.text().strip(), "SUPABASE_KEY": self.ent_sb_key.text().strip()}, f, indent=4)
            self.schema.setdefault("mysql", {}); self.schema.setdefault("sqlite", {})
            self.schema["mysql"].update({"host": self.ent_my_host.text().strip(), "user": self.ent_my_user.text().strip(), "password": self.ent_my_pass.text().strip(), "database": self.ent_my_db.text().strip(), "port": self.schema["mysql"].get("port", 3306), "enabled": True})
            self.schema["sqlite"].update({"enabled": True, "folder_name": self.schema["sqlite"].get("folder_name", "db"), "file_name": self.schema["sqlite"].get("file_name", "eventhub_local.db")})
            with open(SCHEMA_PATH, 'w') as f: json.dump(self.schema, f, indent=4)
        except Exception as e:
            QMessageBox.warning(self, "Save Failed", f"Couldn't save settings: {e}")
            return
        QMessageBox.information(self, "Saved", "Settings saved. Re-initializing local connections.")
        if self.parent(): self.parent().reinitialize_manager()
        self.accept()

class ConflictDetailDialog(QDialog):
    def __init__(self, parent, conflict, on_resolve):
        super().__init__(parent)
        self.setWindowTitle(f"Conflict — {conflict.get('attendee_id', '')}")
        self.resize(660, 520)
        self.on_resolve = on_resolve
        self.conflict_id = conflict["id"]
        
        layout = QVBoxLayout(self)
        layout.setContentsMargins(22, 22, 22, 22)
        
        lbl_name = QLabel(conflict.get("full_name", "Unknown"))
        lbl_name.setFont(QFont("Segoe UI", 15, QFont.Bold))
        layout.addWidget(lbl_name)
        
        lbl_id = QLabel(f"Attendee ID: {conflict.get('attendee_id', '')}")
        lbl_id.setObjectName("SubText")
        layout.addWidget(lbl_id)
        
        lbl_time = QLabel(f"Local last changed {_fmt_dt(conflict['local_updated_at'])}   ·   Cloud last changed {_fmt_dt(conflict['cloud_updated_at'])}")
        lbl_time.setObjectName("SubText")
        layout.addWidget(lbl_time)
        
        line = QFrame()
        line.setFrameShape(QFrame.HLine)
        line.setObjectName("DividerLine")
        layout.addWidget(line)
        layout.addSpacing(10)
        
        header_row = QHBoxLayout()
        h1 = QLabel("FIELD"); h1.setFixedWidth(140); h1.setFont(QFont("Segoe UI", 10, QFont.Bold))
        h2 = QLabel("LOCAL"); h2.setFixedWidth(190); h2.setFont(QFont("Segoe UI", 10, QFont.Bold)); h2.setStyleSheet(f"color: {COLORS['SUCCESS']};")
        h3 = QLabel("CLOUD"); h3.setFixedWidth(190); h3.setFont(QFont("Segoe UI", 10, QFont.Bold)); h3.setStyleSheet(f"color: {COLORS['INFO']};")
        header_row.addWidget(h1); header_row.addWidget(h2); header_row.addWidget(h3); header_row.addStretch()
        layout.addLayout(header_row)
        
        scroll = QScrollArea()
        scroll.setWidgetResizable(True)
        scroll.setFrameShape(QFrame.NoFrame)
        scroll_content = QWidget()
        rows_layout = QVBoxLayout(scroll_content)
        rows_layout.setContentsMargins(0, 0, 0, 0)
        
        diff_fields = conflict.get("diff_fields", {})
        for i, (field, values) in enumerate(diff_fields.items()):
            row_w = QWidget()
            row_w.setObjectName("DiffRowAlt" if i % 2 else "DiffRow")
            
            rl = QHBoxLayout(row_w)
            rl.setContentsMargins(5, 5, 5, 5)
            f_lbl = QLabel(field.replace("_", " ").title()); f_lbl.setFixedWidth(135)
            l_lbl = QLabel(str(values.get("local") or "—")); l_lbl.setFixedWidth(185); l_lbl.setWordWrap(True)
            c_lbl = QLabel(str(values.get("cloud") or "—")); c_lbl.setFixedWidth(185); c_lbl.setWordWrap(True)
            rl.addWidget(f_lbl); rl.addWidget(l_lbl); rl.addWidget(c_lbl); rl.addStretch()
            rows_layout.addWidget(row_w)
            
        rows_layout.addStretch()
        scroll.setWidget(scroll_content)
        layout.addWidget(scroll, 1)
        
        btn_row = QHBoxLayout()
        btn_row.addStretch()
        btn_cancel = QPushButton("Cancel")
        btn_cancel.clicked.connect(self.reject)
        
        btn_cloud = QPushButton("Keep Cloud")
        btn_cloud.setStyleSheet(f"QPushButton {{ background-color: {COLORS['INFO']}; color: white; border: none; padding: 6px 12px; border-radius: 4px; font-weight: bold; }} QPushButton:hover {{ background-color: #0b9ebf; }}")
        btn_cloud.clicked.connect(lambda: self._choose("cloud"))
        
        btn_local = QPushButton("Keep Local")
        btn_local.setStyleSheet(f"QPushButton {{ background-color: {COLORS['SUCCESS']}; color: white; border: none; padding: 6px 12px; border-radius: 4px; font-weight: bold; }} QPushButton:hover {{ background-color: #009670; }}")
        btn_local.clicked.connect(lambda: self._choose("local"))
        
        btn_row.addWidget(btn_cancel)
        btn_row.addWidget(btn_cloud)
        btn_row.addWidget(btn_local)
        layout.addLayout(btn_row)

    def _choose(self, keep):
        self.on_resolve([self.conflict_id], keep)
        self.accept()

class SyncDashboard(QMainWindow):
    def __init__(self):
        super().__init__()
        self.setWindowTitle("EventHub Portable (v2.6) — Sync Manager")
        self.resize(1400, 850)
        self.setMinimumSize(1200, 720)
        
        icon_path = os.path.join(BASE_DIR, "assets", "EventHub.ico")
        if os.path.exists(icon_path):
            try: self.setWindowIcon(QIcon(icon_path))
            except Exception: pass

        self.gui_queue = queue.Queue(maxsize=1000) 
        self.sync_manager = SyncManager()
        self.is_syncing = False
        self._is_refreshing_stats = False  
        
        self.is_light_theme = False
        self.canvas_indicators = []
        self._spinner_frames = ["⠋", "⠙", "⠹", "⠸", "⠼", "⠴", "⠦", "⠧", "⠇", "⠏"]
        self._spinner_idx = 0
        
        self.next_pull_ts = 0
        self.next_push_ts = 0

        self.build_ui()
        self.animated_health_meter = AnimatedMeter(self.health_meter)
        
        # Safe async UI queuing
        self.queue_timer = QTimer(self)
        self.queue_timer.timeout.connect(self.process_gui_queue)
        self.queue_timer.start(20)

        # Animation logic
        self.anim_timer = QTimer(self)
        self.anim_timer.timeout.connect(self.animation_loop)
        self.anim_timer.start(16)

        # Background polling
        self.refresh_stats_async()
        self.poll_timer = QTimer(self)
        self.poll_timer.timeout.connect(self._schedule_periodic_refresh)
        self.poll_timer.start(3000)

        # Auto sync scheduler
        self.auto_sync_timer = QTimer(self)
        self.auto_sync_timer.timeout.connect(self._auto_sync_scheduler)
        self.auto_sync_timer.start(1000)

    def toggle_theme(self):
        self.is_light_theme = self.chk_theme.isChecked()
        self.health_meter.set_theme(self.is_light_theme)
        
        # Apply the master stylesheet perfectly resolving inheritance bugs across all Windows
        app = QApplication.instance()
        
        if self.is_light_theme:
            style = """
                QMainWindow, QDialog, #CentralWidget, #Sidebar { background-color: #f8f9fa; color: #212529; font-family: 'Segoe UI', Arial; }
                QGroupBox { border: 1px solid #ced4da; border-radius: 6px; margin-top: 15px; font-weight: bold; }
                QGroupBox::title { subcontrol-origin: margin; left: 10px; padding: 0 5px; color: #495057; }
                QTreeWidget { background-color: #ffffff; border: 1px solid #ced4da; color: #212529; alternate-background-color: #f1f3f5; }
                QTreeWidget::item:selected { background-color: #0dcaf0; color: #ffffff; }
                QHeaderView::section { background-color: #e9ecef; border: none; border-right: 1px solid #ced4da; border-bottom: 1px solid #ced4da; padding: 6px; font-weight: bold; color: #495057; }
                QLineEdit, QComboBox { background-color: #ffffff; border: 1px solid #ced4da; color: #212529; border-radius: 4px; padding: 5px; }
                QTabWidget::pane { border: 1px solid #ced4da; background-color: #ffffff; border-radius: 4px; }
                QTabBar::tab { background-color: #e9ecef; color: #495057; padding: 8px 20px; border: 1px solid #ced4da; border-bottom: none; border-top-left-radius: 4px; border-top-right-radius: 4px; margin-right: 2px; }
                QTabBar::tab:selected { background-color: #ffffff; color: #00bc8c; font-weight: bold; border-top: 3px solid #00bc8c; }
                QPushButton { background-color: #ffffff; color: #212529; border: 1px solid #ced4da; padding: 6px 12px; border-radius: 4px; font-weight: 500; }
                QPushButton:hover { background-color: #e9ecef; }
                QPushButton:pressed { background-color: #dee2e6; }
                QPushButton:disabled { background-color: #e9ecef; color: #adb5bd; border: 1px solid #dee2e6; }
                QLabel, QCheckBox { background: transparent; color: #212529; }
                QScrollArea { border: none; background: transparent; }
                QProgressBar { border: 1px solid #ced4da; border-radius: 2px; background-color: #e9ecef; text-align: center; color: #212529; }
                QProgressBar::chunk { background-color: #0dcaf0; }
                #StatCard { border: 1px solid #ced4da; border-radius: 6px; background-color: #ffffff; }
                #SubText { color: #6c757d; }
                #DiffRow { background-color: transparent; }
                #DiffRowAlt { background-color: #f1f3f5; }
                #DividerLine { color: #ced4da; }
            """
        else:
            style = """
                QMainWindow, QDialog, #CentralWidget, #Sidebar { background-color: #121212; color: #e0e0e0; font-family: 'Segoe UI', Arial; }
                QGroupBox { border: 1px solid #333333; border-radius: 6px; margin-top: 15px; font-weight: bold; }
                QGroupBox::title { subcontrol-origin: margin; left: 10px; padding: 0 5px; color: #aaaaaa; }
                QTreeWidget { background-color: #1a1a1a; border: 1px solid #333333; color: #e0e0e0; alternate-background-color: #222222; }
                QTreeWidget::item:selected { background-color: #375a7f; color: #ffffff; }
                QHeaderView::section { background-color: #242424; border: none; border-right: 1px solid #333333; border-bottom: 1px solid #333333; padding: 6px; font-weight: bold; color: #aaaaaa; }
                QLineEdit, QComboBox { background-color: #242424; border: 1px solid #444444; color: #ffffff; border-radius: 4px; padding: 5px; }
                QTabWidget::pane { border: 1px solid #333333; background-color: #1a1a1a; border-radius: 4px; }
                QTabBar::tab { background-color: #242424; color: #aaaaaa; padding: 8px 20px; border: 1px solid #333333; border-bottom: none; border-top-left-radius: 4px; border-top-right-radius: 4px; margin-right: 2px; }
                QTabBar::tab:selected { background-color: #1a1a1a; color: #0dcaf0; font-weight: bold; border-top: 3px solid #0dcaf0; }
                QPushButton { background-color: #242424; color: #e0e0e0; border: 1px solid #444444; padding: 6px 12px; border-radius: 4px; font-weight: 500; }
                QPushButton:hover { background-color: #333333; }
                QPushButton:pressed { background-color: #111111; }
                QPushButton:disabled { background-color: #242424; color: #555555; border: 1px solid #333333; }
                QLabel, QCheckBox { background: transparent; color: #e0e0e0; }
                QScrollArea { border: none; background: transparent; }
                QProgressBar { border: 1px solid #444444; border-radius: 2px; background-color: #242424; text-align: center; color: #e0e0e0; }
                QProgressBar::chunk { background-color: #0dcaf0; }
                #StatCard { border: 1px solid #333333; border-radius: 6px; background-color: #1a1a1a; }
                #SubText { color: #aaaaaa; }
                #DiffRow { background-color: transparent; }
                #DiffRowAlt { background-color: #222222; }
                #DividerLine { color: #444444; }
            """
        app.setStyleSheet(style)

    def _get_seconds(self, val_str, unit_str):
        try:
            v = int(val_str)
            if v <= 0: v = 1
        except ValueError:
            v = 15 
        return v * 60 if unit_str == "Minutes" else v * 3600

    def _recalc_pull_ts(self):
        if self.chk_auto_pull.isChecked():
            self.next_pull_ts = time.time() + self._get_seconds(self.ent_auto_pull.text(), self.combo_auto_pull.currentText())
        else:
            self.next_pull_ts = 0

    def _recalc_push_ts(self):
        if self.chk_auto_push.isChecked():
            self.next_push_ts = time.time() + self._get_seconds(self.ent_auto_push.text(), self.combo_auto_push.currentText())
        else:
            self.next_push_ts = 0

    def _format_countdown(self, seconds):
        m, s = divmod(seconds, 60)
        h, m = divmod(m, 60)
        return f"{h:02d}:{m:02d}:{s:02d}" if h else f"{m:02d}:{s:02d}"

    def animation_loop(self):
        self.animated_health_meter.tick()

    def _auto_sync_scheduler(self):
        now = time.time()
        self._spinner_idx = (self._spinner_idx + 1) % len(self._spinner_frames)
        spin = self._spinner_frames[self._spinner_idx]
        
        if self.chk_auto_pull.isChecked():
            if self.is_syncing:
                self.lbl_pull_countdown.setText(f"{spin} Syncing...")
                self.lbl_pull_countdown.setStyleSheet(f"color: {COLORS['WARNING']}; font-weight: bold; border: none;")
            else:
                rem_pull = max(0, int(self.next_pull_ts - now))
                self.lbl_pull_countdown.setText(f"⏱ {self._format_countdown(rem_pull)}")
                self.lbl_pull_countdown.setStyleSheet(f"color: {COLORS['INFO']}; font-weight: bold; border: none;")
        else:
            self.lbl_pull_countdown.setText("Off")
            self.lbl_pull_countdown.setStyleSheet("color: #888; font-weight: bold; border: none;")
            
        if self.chk_auto_push.isChecked():
            if self.is_syncing:
                self.lbl_push_countdown.setText(f"{spin} Syncing...")
                self.lbl_push_countdown.setStyleSheet(f"color: {COLORS['WARNING']}; font-weight: bold; border: none;")
            else:
                rem_push = max(0, int(self.next_push_ts - now))
                self.lbl_push_countdown.setText(f"⏱ {self._format_countdown(rem_push)}")
                self.lbl_push_countdown.setStyleSheet(f"color: {COLORS['SUCCESS']}; font-weight: bold; border: none;")
        else:
            self.lbl_push_countdown.setText("Off")
            self.lbl_push_countdown.setStyleSheet("color: #888; font-weight: bold; border: none;")
            
        if not self.is_syncing:
            if self.chk_auto_pull.isChecked() and self.next_pull_ts and now >= self.next_pull_ts:
                self._recalc_pull_ts()
                logging.info("Auto-sync: Initiating scheduled PULL")
                self.run_pull()
            elif self.chk_auto_push.isChecked() and self.next_push_ts and now >= self.next_push_ts:
                self._recalc_push_ts()
                logging.info("Auto-sync: Initiating scheduled PUSH")
                self.run_push()

    def process_gui_queue(self):
        for _ in range(200):
            try: self.gui_queue.get_nowait()()
            except queue.Empty: break

    def reinitialize_manager(self):
        self.sync_manager = SyncManager()
        self.refresh_stats_async()

    def _schedule_periodic_refresh(self):
        if not self.is_syncing:
            self.refresh_stats_async()

    def refresh_stats_async(self):
        if getattr(self, '_is_refreshing_stats', False): return
        self._is_refreshing_stats = True
        def _fetch():
            try:
                stats = self.sync_manager.get_dashboard_stats()
                self.gui_queue.put(lambda: self._apply_stats(stats))
            finally:
                self._is_refreshing_stats = False
        threading.Thread(target=_fetch, daemon=True).start()

    def _create_stat_card(self, layout, icon, title, initial_value, color, var_name):
        card = QFrame()
        card.setObjectName("StatCard")
        card_lyt = QHBoxLayout(card)
        card_lyt.setContentsMargins(0, 0, 0, 0)
        
        stripe = QWidget()
        stripe.setFixedWidth(5)
        stripe.setStyleSheet(f"background-color: {color}; border-top-left-radius: 4px; border-bottom-left-radius: 4px;")
        card_lyt.addWidget(stripe)
        
        inner = QVBoxLayout()
        inner.setContentsMargins(15, 15, 15, 15)
        
        top_row = QHBoxLayout()
        if icon:
            icon_lbl = QLabel(icon)
            icon_lbl.setStyleSheet("border: none; font-size: 14px;")
            top_row.addWidget(icon_lbl)
        t_lbl = QLabel(title)
        t_lbl.setStyleSheet(f"color: {color}; font-weight: bold; border: none; font-size: 11px;")
        top_row.addWidget(t_lbl)
        top_row.addStretch()
        
        val_lbl = QLabel(initial_value)
        val_lbl.setFont(QFont("Segoe UI", 28, QFont.Bold))
        val_lbl.setStyleSheet("border: none;")
        
        inner.addLayout(top_row)
        inner.addWidget(val_lbl)
        card_lyt.addLayout(inner)
        
        layout.addWidget(card)
        self.stat_vars[var_name] = val_lbl

    def build_ui(self):
        central = QWidget()
        central.setObjectName("CentralWidget")
        self.setCentralWidget(central)
        
        main_layout = QHBoxLayout(central)
        main_layout.setContentsMargins(0, 0, 0, 0)
        
        self.build_sidebar(main_layout)
        
        content_w = QWidget()
        content = QVBoxLayout(content_w)
        content.setContentsMargins(30, 30, 30, 30)
        
        header_row = QHBoxLayout()
        title_box = QVBoxLayout()
        t1 = QLabel("Sync Dashboard")
        t1.setFont(QFont("Segoe UI", 24, QFont.Bold))
        t1.setStyleSheet(f"color: {COLORS['PRIMARY']}; border: none;")
        t2 = QLabel("TDE UP 2026")
        t2.setObjectName("SubText")
        t2.setStyleSheet("font-weight: bold; border: none;")
        title_box.addWidget(t1)
        title_box.addWidget(t2)
        
        btn_ref = QPushButton("⟳ Refresh Data")
        btn_ref.setStyleSheet(f"QPushButton {{ border: 1px solid {COLORS['INFO']}; color: {COLORS['INFO']}; background: transparent; padding: 6px 12px; border-radius: 4px; }} QPushButton:hover {{ background-color: {COLORS['INFO']}; color: white; }}")
        btn_ref.clicked.connect(self.refresh_stats_async)
        
        header_row.addLayout(title_box)
        header_row.addStretch()
        header_row.addWidget(btn_ref)
        content.addLayout(header_row)
        content.addSpacing(20)
        
        self.stat_vars = {}
        cards_row1 = QHBoxLayout()
        self._create_stat_card(cards_row1, "👥", "MYSQL (PRIMARY)", "0", COLORS["PRIMARY"], "mysql_total")
        self._create_stat_card(cards_row1, "💾", "SQLITE (MIRROR)", "0", COLORS["INFO"], "sqlite_total")
        self._create_stat_card(cards_row1, "⏳", "PENDING PUSH", "0", COLORS["WARNING"], "pending_push")
        self._create_stat_card(cards_row1, "⚠", "CONFLICTS", "0", COLORS["DANGER"], "conflicts")
        self._create_stat_card(cards_row1, "🖥️", "KIOSK REG.", "0", COLORS["SECONDARY"], "kiosk_reg")
        content.addLayout(cards_row1)
        
        cards_row2 = QHBoxLayout()
        self._create_stat_card(cards_row2, "✔", "TOTAL CHECKED IN", "0", COLORS["SUCCESS"], "checked_in")
        for day in EVENT_DAYS:
            self._create_stat_card(cards_row2, "📅", _format_day_label(day).replace("📅 ", ""), "0", COLORS["SECONDARY"], f"day_{day}")
        content.addLayout(cards_row2)
        content.addSpacing(15)
        
        controls_frame = QHBoxLayout()
        self.progress = QProgressBar()
        self.progress.setRange(0, 100)
        self.progress.setValue(0)
        self.progress.setTextVisible(False)
        self.progress.setFixedHeight(4)
        self.lbl_status = QLabel("Ready.")
        controls_frame.addWidget(self.progress)
        controls_frame.addWidget(self.lbl_status)
        content.addLayout(controls_frame)
        
        self.notebook = QTabWidget()
        content.addWidget(self.notebook, 1)
        
        self.build_log_tab()
        self.build_conflicts_tab()
        
        main_layout.addWidget(content_w, 1)
        self.toggle_theme() # Instantly applies Global Styling to fix the UI!

    def build_sidebar(self, parent_layout):
        sidebar_w = QWidget()
        sidebar_w.setFixedWidth(380)
        sidebar_w.setObjectName("Sidebar")
        sidebar = QVBoxLayout(sidebar_w)
        sidebar.setContentsMargins(25, 25, 25, 25)
        
        t1 = QLabel("EventHub Portable")
        t1.setFont(QFont("Segoe UI", 18, QFont.Bold))
        t1.setStyleSheet("border: none;")
        t2 = QLabel("Data Synchronization")
        t2.setObjectName("SubText")
        t2.setStyleSheet("border: none;")
        sidebar.addWidget(t1)
        sidebar.addWidget(t2)
        sidebar.addSpacing(15)
        
        self.chk_theme = QCheckBox("☀️ Sunlight Mode")
        self.chk_theme.setStyleSheet("border: none;")
        self.chk_theme.toggled.connect(self.toggle_theme)
        sidebar.addWidget(self.chk_theme)
        sidebar.addSpacing(15)
        
        conn_frame = QGroupBox(" CONNECTION STATUS ")
        conn_lyt = QVBoxLayout(conn_frame)
        self.lbl_supa, self.supa_dot = self._create_status_label(conn_lyt, "Supabase Cloud: Idle", COLORS["SECONDARY"])
        self.lbl_mysql, self.my_dot = self._create_status_label(conn_lyt, "MySQL (Primary): Checking...", COLORS["INFO"])
        self.lbl_sqlite, self.sq_dot = self._create_status_label(conn_lyt, "SQLite (Fallback): Checking...", COLORS["INFO"])
        sidebar.addWidget(conn_frame)
        
        btn_rc = QPushButton("⟳ Refresh Connections")
        btn_rc.clicked.connect(self.reinitialize_manager)
        sidebar.addWidget(btn_rc)
        sidebar.addSpacing(15)
        
        health_frame = QGroupBox(" SYNC HEALTH ")
        health_lyt = QVBoxLayout(health_frame)
        health_lyt.setAlignment(Qt.AlignCenter)
        self.health_meter = SpeedometerWidget(size=160)
        health_lyt.addWidget(self.health_meter, 0, Qt.AlignCenter)
        self.lbl_last_sync = QLabel("Last synced: Never")
        self.lbl_last_sync.setObjectName("SubText")
        self.lbl_last_sync.setStyleSheet("font-size: 11px;")
        health_lyt.addWidget(self.lbl_last_sync, 0, Qt.AlignCenter)
        sidebar.addWidget(health_frame)
        sidebar.addSpacing(15)
        
        self.btn_full_sync = QPushButton("🔄 Full Sync (Pull + Push)")
        self.btn_full_sync.setStyleSheet(f"QPushButton {{ background-color: {COLORS['PRIMARY']}; color: white; border: none; padding: 10px; font-weight: bold; border-radius: 4px; }} QPushButton:hover {{ background-color: #2b4764; }}")
        self.btn_full_sync.clicked.connect(self.run_full_sync)
        sidebar.addWidget(self.btn_full_sync)
        
        pp_row = QHBoxLayout()
        self.btn_pull = QPushButton("↓ Pull Data")
        self.btn_pull.setStyleSheet(f"QPushButton {{ background-color: {COLORS['INFO']}; color: white; border: none; padding: 8px; font-weight: bold; border-radius: 4px; }} QPushButton:hover {{ background-color: #0b9ebf; }}")
        self.btn_pull.clicked.connect(self.run_pull)
        
        self.btn_push = QPushButton("↑ Push Data")
        self.btn_push.setStyleSheet(f"QPushButton {{ background-color: {COLORS['SUCCESS']}; color: white; border: none; padding: 8px; font-weight: bold; border-radius: 4px; }} QPushButton:hover {{ background-color: #009670; }}")
        self.btn_push.clicked.connect(self.run_push)
        pp_row.addWidget(self.btn_pull)
        pp_row.addWidget(self.btn_push)
        sidebar.addLayout(pp_row)
        sidebar.addSpacing(15)
        
        auto_frame = QGroupBox(" AUTO SYNC SCHEDULE ")
        auto_lyt = QVBoxLayout(auto_frame)
        
        p_row = QHBoxLayout()
        self.chk_auto_pull = QCheckBox("Auto Pull")
        self.chk_auto_pull.toggled.connect(self._recalc_pull_ts)
        self.ent_auto_pull = QLineEdit("15")
        self.ent_auto_pull.setFixedWidth(40)
        self.ent_auto_pull.textChanged.connect(self._recalc_pull_ts)
        self.combo_auto_pull = QComboBox()
        self.combo_auto_pull.addItems(["Minutes", "Hours"])
        self.combo_auto_pull.currentTextChanged.connect(self._recalc_pull_ts)
        self.lbl_pull_countdown = QLabel("Off")
        p_row.addWidget(self.chk_auto_pull); p_row.addWidget(self.ent_auto_pull); p_row.addWidget(self.combo_auto_pull); p_row.addStretch(); p_row.addWidget(self.lbl_pull_countdown)
        auto_lyt.addLayout(p_row)
        
        pu_row = QHBoxLayout()
        self.chk_auto_push = QCheckBox("Auto Push")
        self.chk_auto_push.toggled.connect(self._recalc_push_ts)
        self.ent_auto_push = QLineEdit("15")
        self.ent_auto_push.setFixedWidth(40)
        self.ent_auto_push.textChanged.connect(self._recalc_push_ts)
        self.combo_auto_push = QComboBox()
        self.combo_auto_push.addItems(["Minutes", "Hours"])
        self.combo_auto_push.currentTextChanged.connect(self._recalc_push_ts)
        self.lbl_push_countdown = QLabel("Off")
        pu_row.addWidget(self.chk_auto_push); pu_row.addWidget(self.ent_auto_push); pu_row.addWidget(self.combo_auto_push); pu_row.addStretch(); pu_row.addWidget(self.lbl_push_countdown)
        auto_lyt.addLayout(pu_row)
        
        sidebar.addWidget(auto_frame)
        sidebar.addStretch()
        
        btn_cfg = QPushButton("⚙ Configure Databases")
        btn_cfg.clicked.connect(lambda: ConfigDialog(self).exec())
        sidebar.addWidget(btn_cfg)
        
        parent_layout.addWidget(sidebar_w)

    def _create_status_label(self, parent_layout, text, color):
        row = QHBoxLayout()
        dot = QLabel("●")
        dot.setStyleSheet(f"color: {color}; font-size: 16px; border: none;")
        lbl = QLabel(text)
        lbl.setStyleSheet(f"color: {color}; font-weight: bold; border: none;")
        row.addWidget(dot)
        row.addWidget(lbl)
        row.addStretch()
        parent_layout.addLayout(row)
        return lbl, dot

    def _update_status_dot(self, label, dot, text, color):
        label.setText(text)
        label.setStyleSheet(f"color: {color}; font-weight: bold; border: none;")
        dot.setStyleSheet(f"color: {color}; font-size: 16px; border: none;")

    def build_log_tab(self):
        log_tab = QWidget()
        lyt = QVBoxLayout(log_tab)
        
        toolbar = QHBoxLayout()
        toolbar.addStretch()
        btn_clear = QPushButton("Clear Log")
        btn_clear.clicked.connect(self.clear_log)
        toolbar.addWidget(btn_clear)
        lyt.addLayout(toolbar)
        
        self.log_tree = QTreeWidget()
        self.log_tree.setAlternatingRowColors(True)
        self.log_tree.setHeaderLabels(["TIME", "LEVEL", "MESSAGE"])
        self.log_tree.setColumnWidth(0, 100)
        self.log_tree.setColumnWidth(1, 100)
        lyt.addWidget(self.log_tree)
        self.notebook.addTab(log_tab, "Activity Log")
        
        gui_logger = QtLogHandler(self.gui_queue, self.log_tree)
        gui_logger.setFormatter(logging.Formatter('%(message)s'))
        logging.getLogger().addHandler(gui_logger)

    def build_conflicts_tab(self):
        self.conflicts_tab = QWidget()
        lyt = QVBoxLayout(self.conflicts_tab)
        
        toolbar = QHBoxLayout()
        lbl = QLabel("Double-click a row to compare fields side-by-side.")
        lbl.setObjectName("SubText")
        toolbar.addWidget(lbl)
        toolbar.addStretch()
        
        self.btn_resolve_all = QPushButton("Resolve All → Prefer Newest")
        self.btn_resolve_all.setStyleSheet(f"QPushButton {{ border: 1px solid {COLORS['WARNING']}; color: {COLORS['WARNING']}; background: transparent; border-radius: 4px; padding: 6px; }} QPushButton:hover {{ background-color: {COLORS['WARNING']}; color: white; }}")
        self.btn_resolve_all.clicked.connect(self.resolve_all_conflicts_bulk)
        
        self.btn_keep_cloud = QPushButton("Keep Cloud (Selected)")
        self.btn_keep_cloud.setStyleSheet(f"QPushButton {{ border: 1px solid {COLORS['INFO']}; color: {COLORS['INFO']}; background: transparent; border-radius: 4px; padding: 6px; }} QPushButton:hover {{ background-color: {COLORS['INFO']}; color: white; }}")
        self.btn_keep_cloud.clicked.connect(lambda: self.resolve_selected("cloud"))
        
        self.btn_keep_local = QPushButton("Keep Local (Selected)")
        self.btn_keep_local.setStyleSheet(f"QPushButton {{ border: 1px solid {COLORS['SUCCESS']}; color: {COLORS['SUCCESS']}; background: transparent; border-radius: 4px; padding: 6px; }} QPushButton:hover {{ background-color: {COLORS['SUCCESS']}; color: white; }}")
        self.btn_keep_local.clicked.connect(lambda: self.resolve_selected("local"))
        
        toolbar.addWidget(self.btn_resolve_all)
        toolbar.addWidget(self.btn_keep_cloud)
        toolbar.addWidget(self.btn_keep_local)
        lyt.addLayout(toolbar)
        
        self.conflict_tree = QTreeWidget()
        self.conflict_tree.setAlternatingRowColors(True)
        self.conflict_tree.setHeaderLabels(["ATTENDEE ID", "NAME", "LOCAL UPDATED", "CLOUD UPDATED", "FIELDS DIFFERING"])
        self.conflict_tree.setColumnWidth(0, 140)
        self.conflict_tree.setColumnWidth(1, 160)
        self.conflict_tree.setColumnWidth(2, 160)
        self.conflict_tree.setColumnWidth(3, 160)
        self.conflict_tree.itemDoubleClicked.connect(self.on_conflict_double_click)
        self.conflict_tree.setSelectionMode(QTreeWidget.ExtendedSelection)
        lyt.addWidget(self.conflict_tree)
        
        self.conflict_empty_label = QLabel("✅ No conflicts right now — everything's in sync.")
        self.conflict_empty_label.setAlignment(Qt.AlignCenter)
        self.conflict_empty_label.setFont(QFont("Segoe UI", 12))
        self.conflict_empty_label.setStyleSheet(f"color: {COLORS['SUCCESS']};")
        lyt.addWidget(self.conflict_empty_label)
        
        self.notebook.addTab(self.conflicts_tab, "Conflicts")

    def _apply_stats(self, stats):
        if not self.sync_manager.SessionMySQL:
            self._update_status_dot(self.lbl_mysql, self.my_dot, "MySQL (Primary): Offline", COLORS["DANGER"])
            self._update_status_dot(self.lbl_sqlite, self.sq_dot, "SQLite (Fallback): Check Config", COLORS["DANGER"])
            self._update_status_dot(self.lbl_supa, self.supa_dot, "Supabase Cloud: Idle", COLORS["SECONDARY"])
            self.animated_health_meter.set_target(0)
            self.health_meter.configure(bootstyle=COLORS["DANGER"])
            self.lbl_last_sync.setText(f"Last synced: {_relative_time(self.sync_manager.last_sync_at)}")
            self._refresh_conflicts_ui()
            return
        self._update_status_dot(self.lbl_mysql, self.my_dot, "MySQL (Primary): Online", COLORS["SUCCESS"])
        if self.sync_manager.SessionSQLite:
            self._update_status_dot(self.lbl_sqlite, self.sq_dot, "SQLite (Fallback): Ready", COLORS["SUCCESS"])
        else:
            self._update_status_dot(self.lbl_sqlite, self.sq_dot, "SQLite (Fallback): Offline", COLORS["DANGER"])
        if self.sync_manager.state == SyncState.ERROR: 
            self._update_status_dot(self.lbl_supa, self.supa_dot, "Supabase Cloud: Error", COLORS["DANGER"])
        elif not self.is_syncing: 
            self._update_status_dot(self.lbl_supa, self.supa_dot, "Supabase Cloud: Idle", COLORS["SECONDARY"])
            
        def safe_set(key, val):
            if self.stat_vars[key].text() != str(val):
                self.stat_vars[key].setText(str(val))
                
        safe_set("mysql_total", stats["mysql_total"])
        safe_set("sqlite_total", stats["sqlite_total"])
        safe_set("pending_push", stats["pending_push"])
        safe_set("conflicts", stats["conflict_count"])
        safe_set("kiosk_reg", stats["kiosk_reg"])
        safe_set("checked_in", stats["checked_in"])
        for day in EVENT_DAYS: 
            safe_set(f"day_{day}", stats["day_counts"].get(day, 0))
            
        health = _compute_sync_health(stats)
        self.animated_health_meter.set_target(health)
        self.health_meter.configure(bootstyle=COLORS["SUCCESS"] if health >= 95 else (COLORS["WARNING"] if health >= 80 else COLORS["DANGER"]))
        self.lbl_last_sync.setText(f"Last synced: {_relative_time(self.sync_manager.last_sync_at)}")
        self._refresh_conflicts_ui()

    def _refresh_conflicts_ui(self):
        self.conflict_tree.clear()
        conflicts = self.sync_manager.get_pending_conflicts()
        if not conflicts:
            self.conflict_tree.hide()
            self.conflict_empty_label.show()
        else:
            self.conflict_empty_label.hide()
            self.conflict_tree.show()
            for c in conflicts:
                item = QTreeWidgetItem(self.conflict_tree, [c["attendee_id"], c["full_name"], _fmt_dt(c["local_updated_at"]), _fmt_dt(c["cloud_updated_at"]), _fields_summary(c["diff_fields"])])
                item.setData(0, Qt.UserRole, c["id"])
                if len(c["diff_fields"]) >= 5:
                    for i in range(5): item.setForeground(i, QColor(COLORS["DANGER"]))
        tab_idx = self.notebook.indexOf(self.conflicts_tab)
        self.notebook.setTabText(tab_idx, f"Conflicts ({len(conflicts)})" if conflicts else "Conflicts")

    def clear_log(self):
        self.log_tree.clear()

    def _set_controls_state(self, state):
        enabled = state != False 
        for btn in (self.btn_pull, self.btn_push, self.btn_full_sync, self.btn_keep_local, self.btn_keep_cloud, self.btn_resolve_all):
            btn.setEnabled(enabled)

    def _lock_ui(self, mode="syncing"):
        self.is_syncing = True
        self._set_controls_state(False)
        self.progress.setRange(0, 0) # Indeterminate
        self._update_status_dot(self.lbl_supa, self.supa_dot, f"Supabase Cloud: {mode.title()}...", COLORS["INFO"])

    def _unlock_ui(self, msg="Ready."):
        self.is_syncing = False
        self._set_controls_state(True)
        self.progress.setRange(0, 100)
        self.progress.setValue(0)
        self.lbl_status.setText(msg)
        self.refresh_stats_async()

    def run_push(self):
        if self.is_syncing: return
        self._lock_ui("pushing")
        self.lbl_status.setText("Connecting to cloud and pushing data...")
        threading.Thread(target=self._thread_push, daemon=True).start()

    def _thread_push(self):
        success = self.sync_manager.push_to_cloud()
        self.gui_queue.put(lambda: self._unlock_ui("Push Complete." if success else f"Push Failed: {self.sync_manager.last_error}"))

    def run_pull(self):
        if self.is_syncing: return
        self._lock_ui("pulling")
        self.lbl_status.setText("Connecting to cloud and pulling data...")
        threading.Thread(target=self._thread_pull, daemon=True).start()

    def _thread_pull(self):
        success = self.sync_manager.pull_from_cloud()
        msg = f"Pull Complete — {len(self.sync_manager.conflicts)} conflict(s) need your review." if success and self.sync_manager.conflicts else ("Pull Complete." if success else f"Pull Failed: {self.sync_manager.last_error}")
        self.gui_queue.put(lambda: self._unlock_ui(msg))

    def run_full_sync(self):
        if self.is_syncing: return
        self._lock_ui("syncing")
        self.lbl_status.setText("Running full sync (pull then push)...")
        threading.Thread(target=self._thread_full_sync, daemon=True).start()

    def _thread_full_sync(self):
        success = self.sync_manager.trigger_full_sync()
        msg = f"Full Sync Complete — {len(self.sync_manager.conflicts)} conflict(s) need review." if success and self.sync_manager.conflicts else ("Full Sync Complete." if success else f"Full Sync Failed: {self.sync_manager.last_error}")
        self.gui_queue.put(lambda: self._unlock_ui(msg))

    def on_conflict_double_click(self, item, column):
        row_id = item.data(0, Qt.UserRole)
        if not row_id: return
        conflict = self.sync_manager.conflicts.get(row_id)
        if conflict: ConflictDetailDialog(self, conflict, on_resolve=self._resolve_and_refresh).exec()

    def resolve_selected(self, keep):
        selected = self.conflict_tree.selectedItems()
        if not selected:
            QMessageBox.warning(self, "Nothing Selected", "Select one or more rows first.")
            return
        cids = [item.data(0, Qt.UserRole) for item in selected]
        self._resolve_and_refresh(cids, keep)

    def resolve_all_conflicts_bulk(self):
        if not self.sync_manager.conflicts: return
        if QMessageBox.question(self, "Resolve All", "Resolve all conflicts by picking the newest change automatically?") == QMessageBox.Yes:
            self._resolve_and_refresh(list(self.sync_manager.conflicts.keys()), "newest")

    def _resolve_and_refresh(self, conflict_ids, keep):
        if self.is_syncing: return
        self._lock_ui("syncing")
        self.lbl_status.setText("Applying conflict resolution...")
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
    if os.name == 'nt':
        try:
            my_app_id = os.environ.get("EVENTHUB_TOOL_ID", "EventHub.Tool.sync")
            ctypes.windll.shell32.SetCurrentProcessExplicitAppUserModelID(my_app_id)
        except Exception: pass
    app = QApplication(sys.argv)
    window = SyncDashboard()
    window.show()
    sys.exit(app.exec())