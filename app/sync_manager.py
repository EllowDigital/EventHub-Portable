import os
import json
import enum
import logging
import threading
from datetime import datetime, timezone
from supabase import create_client, Client
from sqlalchemy import create_engine
import ttkbootstrap as ttk
from ttkbootstrap.constants import *
from ttkbootstrap.dialogs import Messagebox
from ttkbootstrap.widgets.tooltip import ToolTip

# Import models and DB initialization from your schema
try:
    from app.schema import Attendee, OfflineKioskAttendee, get_database_sessions
except ModuleNotFoundError:
    from schema import Attendee, OfflineKioskAttendee, get_database_sessions

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

# The three event days - change here if the event dates ever move.
EVENT_DAYS = ["2026-08-30", "2026-08-31", "2026-09-01"]

COMPARABLE_FIELDS = [
    "full_name", "mobile", "email", "gender", "attendee_type",
    "business_name", "business_category", "other_category",
    "address", "city", "state", "pincode", "photo_url",
]

# ==============================================================================
# CUSTOM LOGGING HANDLER FOR TKINTER
# ==============================================================================
class TkinterLogHandler(logging.Handler):
    def __init__(self, treeview):
        super().__init__()
        self.treeview = treeview

    def emit(self, record):
        msg = self.format(record)
        time_str = datetime.fromtimestamp(record.created).strftime('%H:%M:%S')

        tag = 'info'
        if record.levelno >= logging.ERROR: tag = 'error'
        elif record.levelno >= logging.WARNING: tag = 'warning'

        self.treeview.after(0, self._insert_log, time_str, record.levelname, msg, tag)

    def _insert_log(self, time_str, level, msg, tag):
        self.treeview.insert('', END, values=(time_str, level, msg), tags=(tag,))
        self.treeview.yview_moveto(1)

logging.basicConfig(
    filename=os.path.join(LOG_DIR, 'sync.log'),
    level=logging.INFO,
    format='%(asctime)s [%(levelname)s] %(message)s'
)

def load_supabase_client() -> Client:
    if not os.path.exists(SECRETS_PATH):
        raise FileNotFoundError("Supabase credentials missing.")

    with open(SECRETS_PATH, 'r') as f:
        secrets = json.load(f)

    url = secrets.get("SUPABASE_URL")
    key = secrets.get("SUPABASE_KEY")

    if not url or not key:
        raise ValueError("Invalid Supabase credentials")

    return create_client(url, key)

# ==============================================================================
# SYNC STATE
# ==============================================================================
class SyncState(enum.Enum):
    IDLE = "IDLE"
    SYNCING = "SYNCING"
    ERROR = "ERROR"

# ==============================================================================
# DISPLAY / FORMATTING HELPERS
# ==============================================================================
def _format_day_label(iso_date):
    try:
        dt = datetime.strptime(iso_date, "%Y-%m-%d")
        return f"📅 {dt.day} {dt.strftime('%B').upper()} {dt.year}"
    except Exception:
        return f"📅 {iso_date}"

def _fmt_dt(value):
    if not value:
        return "—"
    if isinstance(value, str):
        try:
            value = datetime.fromisoformat(value)
        except Exception:
            return value
    return value.strftime("%d %b %Y, %H:%M") + " UTC"

def _relative_time(dt):
    if not dt:
        return "Never"
    now = datetime.now(timezone.utc)
    if dt.tzinfo is None:
        dt = dt.replace(tzinfo=timezone.utc)
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
    if len(names) <= 3:
        return ", ".join(names)
    return ", ".join(names[:3]) + f" +{len(names) - 3} more"

def _compute_sync_health(stats):
    total = stats.get("mysql_total", 0) + stats.get("kiosk_reg", 0)
    if total == 0:
        return 100
    problem = stats.get("pending_push", 0) + stats.get("conflict_count", 0)
    healthy = max(total - problem, 0)
    return round((healthy / total) * 100)

# ==============================================================================
# CORE SYNC MANAGER CLASS
# ==============================================================================
class SyncManager:
    def __init__(self):
        self.SessionMySQL = None
        self.SessionSQLite = None
        self.state = SyncState.IDLE
        self.last_error = None
        self.last_sync_at = self._load_last_sync()
        self.connect_local_dbs()
        self.conflicts = self._load_conflicts()

    def connect_local_dbs(self):
        try:
            sessions = get_database_sessions()
            self.SessionMySQL = sessions.get('mysql')
            self.SessionSQLite = sessions.get('sqlite')
            logging.info("Local databases verified. Cloud connection is IDLE.")
        except Exception as e:
            logging.error(f"Local Database Connection Failed: {e}")

    def _load_conflicts(self):
        if not os.path.exists(CONFLICTS_PATH):
            return {}
        try:
            with open(CONFLICTS_PATH, 'r') as f:
                raw = json.load(f)
            for c in raw.values():
                for key in ("local_updated_at", "cloud_updated_at", "detected_at"):
                    if isinstance(c.get(key), str):
                        try:
                            c[key] = datetime.fromisoformat(c[key])
                        except ValueError:
                            pass
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
            with open(CONFLICTS_PATH, 'w') as f:
                json.dump(serializable, f, indent=2, default=str)
        except Exception as e:
            logging.error(f"Failed to save conflicts registry: {e}")

    def _load_last_sync(self):
        if os.path.exists(SYNC_STATE_PATH):
            try:
                with open(SYNC_STATE_PATH, 'r') as f:
                    data = json.load(f)
                ts = data.get("last_sync_at")
                if ts:
                    return datetime.fromisoformat(ts)
            except Exception:
                pass
        return None

    def _record_sync_success(self):
        self.last_sync_at = datetime.now(timezone.utc)
        try:
            with open(SYNC_STATE_PATH, 'w') as f:
                json.dump({"last_sync_at": self.last_sync_at.isoformat()}, f)
        except Exception as e:
            logging.error(f"Failed to persist last sync timestamp: {e}")

    def _merge_checkin_history(self, local_history, cloud_history) -> dict:
        if isinstance(local_history, str): local_history = json.loads(local_history) if local_history else {}
        if isinstance(cloud_history, str): cloud_history = json.loads(cloud_history) if cloud_history else {}
        if not isinstance(local_history, dict): local_history = {}
        if not isinstance(cloud_history, dict): cloud_history = {}

        merged = cloud_history.copy()
        merged.update(local_history)
        return merged

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
        local_record.updated_at = cloud_updated_at
        local_record.needs_cloud_sync = False

    def mirror_mysql_to_sqlite(self):
        """Lightning-fast bulk mirror for BOTH tables to prevent SQLite locks and ensure 100% backup coverage."""
        if not self.SessionSQLite or not self.SessionMySQL: return
        logging.info("Starting MySQL -> SQLite mirror process...")

        mysql_session = self.SessionMySQL()
        sqlite_session = self.SessionSQLite()

        try:
            # 1. Mirror Main Attendees Table
            mysql_attendees = mysql_session.query(Attendee).all()
            att_dicts = [{c.name: getattr(m_att, c.name) for c in m_att.__table__.columns} for m_att in mysql_attendees]
            sqlite_session.query(Attendee).delete()
            if att_dicts:
                sqlite_session.bulk_insert_mappings(Attendee, att_dicts)

            # 2. Mirror Offline Kiosk Registrations Table
            mysql_kiosk = mysql_session.query(OfflineKioskAttendee).all()
            kiosk_dicts = [{c.name: getattr(m_kiosk, c.name) for c in m_kiosk.__table__.columns} for m_kiosk in mysql_kiosk]
            sqlite_session.query(OfflineKioskAttendee).delete()
            if kiosk_dicts:
                sqlite_session.bulk_insert_mappings(OfflineKioskAttendee, kiosk_dicts)

            sqlite_session.commit()
            logging.info(f"Mirror complete: {len(att_dicts)} Online + {len(kiosk_dicts)} Kiosk records safely backed up.")
        except Exception as e:
            sqlite_session.rollback()
            logging.error(f"Mirror error: {e}")
        finally:
            mysql_session.close()
            sqlite_session.close()

    def push_to_cloud(self):
        logging.info("--- Starting PUSH to Cloud ---")
        self.state = SyncState.SYNCING
        if not self.SessionMySQL:
            msg = "Local MySQL is offline."
            logging.error(f"Cannot push: {msg}")
            self.state = SyncState.ERROR
            self.last_error = msg
            return False

        try:
            supabase = load_supabase_client()
            logging.info("Successfully established temporary cloud connection.")
        except Exception as e:
            msg = f"Could not reach Supabase: {e}"
            logging.error(f"Failed to connect to Supabase: {e}")
            self.state = SyncState.ERROR
            self.last_error = msg
            return False

        mysql_session = self.SessionMySQL()
        try:
            # Gather pending records from BOTH tables
            pending_online = mysql_session.query(Attendee).filter_by(needs_cloud_sync=True).all()
            pending_kiosk = mysql_session.query(OfflineKioskAttendee).filter_by(needs_cloud_sync=True).all()
            
            pending = pending_online + pending_kiosk

            if not pending:
                logging.info("No records require pushing. Disconnecting from cloud.")
                self.state = SyncState.IDLE
                self.last_error = None
                self._record_sync_success()
                return True

            blocked = [r for r in pending if str(r.id) in self.conflicts]
            pushable = [r for r in pending if str(r.id) not in self.conflicts]

            if blocked:
                logging.warning(
                    f"{len(blocked)} record(s) skipped - resolve their conflicts before they can push."
                )

            if not pushable:
                logging.info("No records pushed - all pending changes are blocked by unresolved conflicts.")
                self.state = SyncState.IDLE
                self.last_error = None
                self._record_sync_success()
                return True

            batch_payload = []
            for record in pushable:
                batch_payload.append({
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
                    "attendance_days": record.attendance_days if isinstance(record.attendance_days, list) else json.loads(record.attendance_days or "[]"),
                    "photo_url": record.photo_url,
                    "checkin_history": record.checkin_history if isinstance(record.checkin_history, dict) else json.loads(record.checkin_history or "{}"),
                    "needs_sheet_sync": record.needs_sheet_sync, # Uses whatever Flask set locally
                    "created_at": record.created_at.isoformat() if record.created_at else datetime.now(timezone.utc).isoformat(),
                    "updated_at": record.updated_at.isoformat() if record.updated_at else datetime.now(timezone.utc).isoformat(),
                    "needs_cloud_sync": False
                })

            response = supabase.table('attendees').upsert(batch_payload).execute()

            if response.data:
                # 🛑 LOCAL RESET LOGIC
                for record in pushable:
                    record.needs_cloud_sync = False
                    record.needs_sheet_sync = False  # Set to false to prevent duplicate Google Sheet triggers
                mysql_session.commit()
                logging.info(f"Successfully pushed {len(batch_payload)} record(s) in batch.")
                self.mirror_mysql_to_sqlite()
                self.state = SyncState.IDLE
                self.last_error = None
                self._record_sync_success()
                return True
            else:
                msg = "Cloud rejected the batch upload."
                logging.warning(msg)
                self.state = SyncState.ERROR
                self.last_error = msg
                return False

        except Exception as e:
            mysql_session.rollback()
            msg = f"Push error: {e}"
            logging.error(f"Push failed: {e}")
            self.state = SyncState.ERROR
            self.last_error = msg
            return False
        finally:
            mysql_session.close()

    def pull_from_cloud(self):
        logging.info("--- Starting PULL from Cloud ---")
        self.state = SyncState.SYNCING
        if not self.SessionMySQL:
            msg = "Local MySQL is offline."
            logging.error(f"Cannot pull: {msg}")
            self.state = SyncState.ERROR
            self.last_error = msg
            return False

        try:
            supabase = load_supabase_client()
            logging.info("Successfully established temporary cloud connection.")
        except Exception as e:
            msg = f"Could not reach Supabase: {e}"
            logging.error(f"Failed to connect to Supabase: {e}")
            self.state = SyncState.ERROR
            self.last_error = msg
            return False

        mysql_session = self.SessionMySQL()
        try:
            cloud_records = []
            page_size = 1000
            offset = 0

            while True:
                response = supabase.table('attendees').select("*").range(offset, offset + page_size - 1).execute()
                data = response.data
                if not data: break
                cloud_records.extend(data)
                if len(data) < page_size: break
                offset += page_size

            self.conflicts = {}

            if not cloud_records:
                logging.info("Cloud is empty. Disconnecting.")
                self._save_conflicts()
                self.state = SyncState.IDLE
                self.last_error = None
                self._record_sync_success()
                return True

            pulled = 0
            conflicts_found = 0
            for cloud_data in cloud_records:
                # 1. Check main table first
                local_record = mysql_session.query(Attendee).filter_by(id=cloud_data['id']).first()
                
                # 2. If not found, check Kiosk table
                if not local_record:
                    local_record = mysql_session.query(OfflineKioskAttendee).filter_by(id=cloud_data['id']).first()

                raw_updated = cloud_data.get('updated_at')
                if raw_updated:
                    try:
                        cloud_updated_at = datetime.fromisoformat(raw_updated.replace('Z', '+00:00')).replace(tzinfo=None)
                    except Exception:
                        cloud_updated_at = datetime.now(timezone.utc).replace(tzinfo=None)
                else:
                    cloud_updated_at = datetime.now(timezone.utc).replace(tzinfo=None)

                raw_created = cloud_data.get('created_at')
                if raw_created:
                    try:
                        cloud_created_at = datetime.fromisoformat(raw_created.replace('Z', '+00:00')).replace(tzinfo=None)
                    except Exception:
                        cloud_created_at = datetime.now(timezone.utc).replace(tzinfo=None)
                else:
                    cloud_created_at = datetime.now(timezone.utc).replace(tzinfo=None)

                if local_record:
                    merged = self._merge_checkin_history(local_record.checkin_history, cloud_data.get('checkin_history', {}))
                    local_record.checkin_history = merged

                    if local_record.needs_cloud_sync and local_record.updated_at and cloud_updated_at > local_record.updated_at:
                        diff_fields = self._compute_diff(local_record, cloud_data)
                        if diff_fields:
                            conflict_id = str(local_record.id)
                            self.conflicts[conflict_id] = {
                                "id": conflict_id,
                                "attendee_id": local_record.attendee_id,
                                "full_name": local_record.full_name,
                                "local_updated_at": local_record.updated_at,
                                "cloud_updated_at": cloud_updated_at,
                                "detected_at": datetime.now(timezone.utc),
                                "cloud_snapshot": cloud_data,
                                "diff_fields": diff_fields,
                            }
                            conflicts_found += 1
                        continue

                    if local_record.needs_cloud_sync: continue

                    if not local_record.updated_at or cloud_updated_at > local_record.updated_at:
                        self._apply_cloud_fields(local_record, cloud_data, cloud_updated_at)
                        if not local_record.created_at:
                            local_record.created_at = cloud_created_at
                        pulled += 1
                else:
                    new_attendee = Attendee(
                        id=cloud_data['id'],
                        attendee_id=cloud_data['attendee_id'],
                        full_name=cloud_data.get('full_name') or 'Unknown',
                        mobile=cloud_data.get('mobile') or '0000000000',
                        email=cloud_data.get('email'),
                        gender=cloud_data.get('gender') or 'OTHER',
                        attendee_type=cloud_data.get('attendee_type') or 'GENERAL',
                        business_name=cloud_data.get('business_name'),
                        business_category=cloud_data.get('business_category'),
                        other_category=cloud_data.get('other_category'),
                        address=cloud_data.get('address') or 'N/A',
                        city=cloud_data.get('city') or 'N/A',
                        state=cloud_data.get('state') or 'N/A',
                        pincode=cloud_data.get('pincode') or '000000',
                        attendance_days=cloud_data.get('attendance_days', []),
                        photo_url=cloud_data.get('photo_url'),
                        checkin_history=self._merge_checkin_history({}, cloud_data.get('checkin_history')),
                        created_at=cloud_created_at,
                        updated_at=cloud_updated_at,
                        needs_cloud_sync=False,
                        needs_sheet_sync=cloud_data.get('needs_sheet_sync', False),
                        local_modified=False,
                        device_name=None
                    )
                    mysql_session.add(new_attendee)
                    pulled += 1

            mysql_session.commit()
            self._save_conflicts()
            if conflicts_found:
                logging.warning(f"Pull complete: {pulled} updated, {conflicts_found} conflict(s) need review before pushing.")
            else:
                logging.info(f"Pulled {pulled} new updates from cloud.")
            self.mirror_mysql_to_sqlite()
            self.state = SyncState.IDLE
            self.last_error = None
            self._record_sync_success()
            return True

        except Exception as e:
            mysql_session.rollback()
            msg = f"Pull error: {e}"
            logging.error(f"Pull failed: {e}")
            self.state = SyncState.ERROR
            self.last_error = msg
            return False
        finally:
            mysql_session.close()

    def trigger_full_sync(self):
        logging.info("--- Starting FULL SYNC (Pull -> Push) ---")
        pull_ok = self.pull_from_cloud()
        if not pull_ok:
            logging.error("Full sync aborted: pull stage failed.")
            return False
        return self.push_to_cloud()

    def get_pending_conflicts(self):
        fallback = datetime.min.replace(tzinfo=timezone.utc)
        return sorted(self.conflicts.values(), key=lambda c: c.get("detected_at") or fallback, reverse=True)

    def resolve_conflict(self, conflict_id, keep):
        conflict = self.conflicts.get(conflict_id)
        if not conflict:
            return False
        if not self.SessionMySQL:
            logging.error("Cannot resolve conflict: Local MySQL is offline.")
            return False

        session = self.SessionMySQL()
        try:
            record = session.query(Attendee).filter_by(id=conflict_id).first()
            if not record:
                record = session.query(OfflineKioskAttendee).filter_by(id=conflict_id).first()
                
            if not record:
                logging.warning(f"Conflict for {conflict.get('attendee_id')} dropped: record no longer exists locally.")
                del self.conflicts[conflict_id]
                self._save_conflicts()
                return True

            if keep == "cloud":
                self._apply_cloud_fields(record, conflict["cloud_snapshot"], conflict["cloud_updated_at"])
                logging.info(f"Conflict resolved for {record.attendee_id}: kept CLOUD version.")
            elif keep == "local":
                record.needs_cloud_sync = True
                logging.info(f"Conflict resolved for {record.attendee_id}: kept LOCAL version (queued for next push).")
            else:
                return False

            session.commit()
            del self.conflicts[conflict_id]
            self._save_conflicts()
            return True
        except Exception as e:
            session.rollback()
            logging.error(f"Failed to resolve conflict for {conflict.get('attendee_id')}: {e}")
            return False
        finally:
            session.close()

    def resolve_all_conflicts(self, strategy="newest"):
        resolved = 0
        for conflict_id, conflict in list(self.conflicts.items()):
            if strategy in ("local", "cloud"):
                keep = strategy
            else:
                keep = "cloud" if conflict["cloud_updated_at"] > conflict["local_updated_at"] else "local"
            if self.resolve_conflict(conflict_id, keep):
                resolved += 1
        return resolved

    def get_dashboard_stats(self):
        empty = {
            "mysql_total": 0, "sqlite_total": 0, "pending_push": 0, "kiosk_reg": 0,
            "conflict_count": len(self.conflicts), "checked_in": 0,
            "day_counts": {d: 0 for d in EVENT_DAYS},
        }
        if not self.SessionMySQL:
            return empty

        mysql_session = self.SessionMySQL()
        sqlite_session = self.SessionSQLite() if self.SessionSQLite else None

        try:
            attendees = mysql_session.query(Attendee).all()
            kiosk_regs = mysql_session.query(OfflineKioskAttendee).all()
            
            all_records = attendees + kiosk_regs

            pending_push = 0
            checked_in = 0
            day_counts = {d: 0 for d in EVENT_DAYS}

            for att in all_records:
                if att.needs_cloud_sync:
                    pending_push += 1

                history = att.checkin_history
                if isinstance(history, str):
                    try:
                        history = json.loads(history)
                    except Exception:
                        history = {}

                if history and len(history) > 0:
                    checked_in += 1
                    history_str = json.dumps(history)
                    for day in EVENT_DAYS:
                        if day in history_str:
                            day_counts[day] += 1

            total_sqlite = sqlite_session.query(Attendee).count() + sqlite_session.query(OfflineKioskAttendee).count() if sqlite_session else 0

            return {
                "mysql_total": len(attendees),
                "sqlite_total": total_sqlite,
                "pending_push": pending_push,
                "kiosk_reg": len(kiosk_regs),
                "conflict_count": len(self.conflicts),
                "checked_in": checked_in,
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
        self.geometry("540x700")
        self.position_center()

        self.secrets = {}
        if os.path.exists(SECRETS_PATH):
            with open(SECRETS_PATH, 'r') as f: self.secrets = json.load(f)

        self.schema = {"mysql": {}, "sqlite": {}}
        if os.path.exists(SCHEMA_PATH):
            with open(SCHEMA_PATH, 'r') as f: self.schema = json.load(f)

        frame = ttk.Frame(self, padding=20)
        frame.pack(fill=BOTH, expand=True)

        ttk.Label(frame, text="Supabase Cloud Settings", font="-weight bold").pack(anchor=W, pady=(0, 10))
        self.ent_sb_url = self._make_input(frame, "SUPABASE_URL", self.secrets.get("SUPABASE_URL", ""))
        self.ent_sb_key = self._make_input(frame, "SUPABASE_KEY", self.secrets.get("SUPABASE_KEY", ""), show="*")

        sb_test_row = ttk.Frame(frame)
        sb_test_row.pack(fill=X, pady=(2, 0))
        ttk.Button(sb_test_row, text="Test Connection", bootstyle="outline-info", command=self.test_supabase).pack(side=LEFT)
        self.lbl_sb_test = ttk.Label(sb_test_row, text="")
        self.lbl_sb_test.pack(side=LEFT, padx=10)

        ttk.Separator(frame).pack(fill=X, pady=15)

        ttk.Label(frame, text="MySQL Settings (Local Hub)", font="-weight bold").pack(anchor=W, pady=(0, 10))
        my_conf = self.schema.get("mysql", {})
        self.ent_my_host = self._make_input(frame, "Host", my_conf.get("host", "localhost"))
        self.ent_my_user = self._make_input(frame, "User", my_conf.get("user", "root"))
        self.ent_my_pass = self._make_input(frame, "Password", my_conf.get("password", ""), show="*")
        self.ent_my_db   = self._make_input(frame, "Database", my_conf.get("database", "eventhub_db"))

        my_test_row = ttk.Frame(frame)
        my_test_row.pack(fill=X, pady=(2, 0))
        ttk.Button(my_test_row, text="Test Connection", bootstyle="outline-info", command=self.test_mysql).pack(side=LEFT)
        self.lbl_my_test = ttk.Label(my_test_row, text="")
        self.lbl_my_test.pack(side=LEFT, padx=10)

        btn_frame = ttk.Frame(frame)
        btn_frame.pack(fill=X, pady=20, side=BOTTOM)
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
                    ent.configure(show=show)
                    toggle_btn.configure(text="Show")
                else:
                    ent.configure(show='')
                    toggle_btn.configure(text="Hide")
            toggle_btn = ttk.Button(row, text="Show", bootstyle="outline-secondary", width=5, command=toggle)
            toggle_btn.pack(side=LEFT, padx=(5, 0))
        return ent

    def test_supabase(self):
        url = self.ent_sb_url.get().strip()
        key = self.ent_sb_key.get().strip()
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
        host = self.ent_my_host.get().strip()
        user = self.ent_my_user.get().strip()
        password = self.ent_my_pass.get().strip()
        db = self.ent_my_db.get().strip()
        if not host or not user or not db:
            self.lbl_my_test.configure(text="Enter host, user, and database first.", bootstyle=WARNING)
            return
        self.lbl_my_test.configure(text="Testing...", bootstyle=SECONDARY)
        threading.Thread(target=self._test_mysql_thread, args=(host, user, password, db), daemon=True).start()

    def _test_mysql_thread(self, host, user, password, db):
        try:
            url = f"mysql+mysqldb://{user}:{password}@{host}:3306/{db}"
            engine = create_engine(url, connect_args={"connect_timeout": 5})
            with engine.connect():
                pass
            self.after(0, lambda: self.lbl_my_test.configure(text="Connected successfully.", bootstyle=SUCCESS))
        except Exception as e:
            err = str(e)[:70]
            self.after(0, lambda: self.lbl_my_test.configure(text=f"Failed: {err}", bootstyle=DANGER))

    def save(self):
        with open(SECRETS_PATH, 'w') as f:
            json.dump({
                "SUPABASE_URL": self.ent_sb_url.get().strip(),
                "SUPABASE_KEY": self.ent_sb_key.get().strip()
            }, f, indent=4)

        with open(SCHEMA_PATH, 'w') as f:
            self.schema["mysql"]["host"] = self.ent_my_host.get().strip()
            self.schema["mysql"]["user"] = self.ent_my_user.get().strip()
            self.schema["mysql"]["password"] = self.ent_my_pass.get().strip()
            self.schema["mysql"]["database"] = self.ent_my_db.get().strip()
            self.schema["mysql"]["port"] = 3306
            self.schema["mysql"]["enabled"] = True

            self.schema["sqlite"]["enabled"] = True
            self.schema["sqlite"]["folder_name"] = "db"
            self.schema["sqlite"]["file_name"] = "eventhub_local.db"
            json.dump(self.schema, f, indent=4)

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
        self.geometry("640x480")
        self.position_center()

        self.on_resolve = on_resolve
        self.conflict_id = conflict["id"]

        frame = ttk.Frame(self, padding=20)
        frame.pack(fill=BOTH, expand=True)

        ttk.Label(frame, text=conflict.get("full_name", "Unknown"), font="-size 14 -weight bold").pack(anchor=W)
        ttk.Label(frame, text=f"Attendee ID: {conflict.get('attendee_id', '')}", bootstyle=SECONDARY).pack(anchor=W, pady=(0, 5))
        ttk.Label(
            frame,
            text=f"Local last changed {_fmt_dt(conflict['local_updated_at'])}   ·   "
                 f"Cloud last changed {_fmt_dt(conflict['cloud_updated_at'])}",
            bootstyle=SECONDARY,
        ).pack(anchor=W, pady=(0, 15))

        ttk.Separator(frame).pack(fill=X, pady=(0, 10))

        header_row = ttk.Frame(frame)
        header_row.pack(fill=X)
        ttk.Label(header_row, text="FIELD", width=18, font="-weight bold").pack(side=LEFT)
        ttk.Label(header_row, text="LOCAL", width=22, font="-weight bold", bootstyle=SUCCESS).pack(side=LEFT)
        ttk.Label(header_row, text="CLOUD", width=22, font="-weight bold", bootstyle=INFO).pack(side=LEFT)

        rows_canvas_frame = ttk.Frame(frame)
        rows_canvas_frame.pack(fill=BOTH, expand=True, pady=(5, 15))

        diff_fields = conflict.get("diff_fields", {})
        for field, values in diff_fields.items():
            row = ttk.Frame(rows_canvas_frame)
            row.pack(fill=X, pady=2)
            ttk.Label(row, text=field.replace("_", " ").title(), width=18).pack(side=LEFT)
            ttk.Label(row, text=str(values.get("local") or "—"), width=22, wraplength=160).pack(side=LEFT)
            ttk.Label(row, text=str(values.get("cloud") or "—"), width=22, wraplength=160).pack(side=LEFT)

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
        super().__init__(themename="cyborg", title="TDE UP 2026 — Sync Manager v5.0")
        self.geometry("1500x850")
        self.minsize(1200, 700)

        self.sync_manager = SyncManager()
        self.is_syncing = False

        self.build_ui()
        self.refresh_stats()
        self._schedule_periodic_refresh()

    def reinitialize_manager(self):
        self.sync_manager = SyncManager()
        self.refresh_stats()

    def _schedule_periodic_refresh(self):
        if not self.is_syncing:
            self.refresh_stats()
        self.after(30000, self._schedule_periodic_refresh)

    def build_ui(self):
        main_paned = ttk.Panedwindow(self, orient=HORIZONTAL)
        main_paned.pack(fill=BOTH, expand=True)

        self.build_sidebar(main_paned)

        content = ttk.Frame(main_paned, padding=20)
        main_paned.add(content, weight=1)

        header_row = ttk.Frame(content)
        header_row.pack(fill=X, pady=(0, 20))
        ttk.Label(header_row, text="Database Synchronisation Dashboard", font="-size 16 -weight bold").pack(side=LEFT, anchor=W)

        self.stat_vars = {}

        cards_row1 = ttk.Frame(content)
        cards_row1.pack(fill=X, pady=(0, 10))

        self._create_stat_card(cards_row1, "👥 MYSQL (PRIMARY)", "0", PRIMARY, var_name="mysql_total")
        self._create_stat_card(cards_row1, "💾 SQLITE (MIRROR)", "0", INFO, var_name="sqlite_total")
        self._create_stat_card(cards_row1, "⏳ PENDING PUSH", "0", WARNING, var_name="pending_push")
        self._create_stat_card(cards_row1, "⚠ CONFLICTS", "0", DANGER, var_name="conflicts")
        self._create_stat_card(cards_row1, "🖥️ KIOSK REG.", "0", SECONDARY, var_name="kiosk_reg")

        cards_row2 = ttk.Frame(content)
        cards_row2.pack(fill=X, pady=(0, 20))

        self._create_stat_card(cards_row2, "✔ TOTAL CHECKED IN", "0", SUCCESS, var_name="checked_in")
        for day in EVENT_DAYS:
            self._create_stat_card(cards_row2, _format_day_label(day), "0", LIGHT, var_name=f"day_{day}")

        controls_frame = ttk.Frame(content)
        controls_frame.pack(fill=X, pady=10)

        self.progress = ttk.Progressbar(controls_frame, mode='indeterminate', bootstyle=INFO)
        self.progress.pack(side=LEFT, fill=X, expand=True, padx=(0, 10))

        self.lbl_status = ttk.Label(controls_frame, text="Ready.")
        self.lbl_status.pack(side=LEFT, padx=10)

        self.notebook = ttk.Notebook(content)
        self.notebook.pack(fill=BOTH, expand=True, pady=(20, 0))

        self.build_log_tab()
        self.build_conflicts_tab()

    def build_sidebar(self, main_paned):
        sidebar = ttk.Frame(main_paned, width=320, padding=20)
        main_paned.add(sidebar, weight=0)

        ttk.Label(sidebar, text="TDE UP 2026", font="-size 20 -weight bold", bootstyle=PRIMARY).pack(anchor=W)
        ttk.Label(sidebar, text="Sync Manager v5.0\n", font="-size 10", foreground="gray").pack(anchor=W, pady=(0, 15))

        conn_frame = ttk.Labelframe(sidebar, text="CONNECTION STATUS", padding=10)
        conn_frame.pack(fill=X, pady=(0, 15))

        self.lbl_supa = ttk.Label(conn_frame, text="● Supabase Cloud: Idle", bootstyle=SECONDARY)
        self.lbl_supa.pack(anchor=W, pady=2)
        self.lbl_mysql = ttk.Label(conn_frame, text="● MySQL (Primary): Checking...", bootstyle=INFO)
        self.lbl_mysql.pack(anchor=W, pady=2)
        self.lbl_sqlite = ttk.Label(conn_frame, text="● SQLite (Fallback): Checking...", bootstyle=INFO)
        self.lbl_sqlite.pack(anchor=W, pady=2)

        refresh_conn_btn = ttk.Button(sidebar, text="⟳ Refresh Connections", bootstyle="outline-secondary", command=self.reinitialize_manager)
        refresh_conn_btn.pack(fill=X, pady=(0, 15))
        ToolTip(refresh_conn_btn, text="Reconnect to local MySQL/SQLite without restarting the app")

        health_frame = ttk.Labelframe(sidebar, text="SYNC HEALTH", padding=10)
        health_frame.pack(fill=X, pady=(0, 15))
        self.health_bar = ttk.Progressbar(health_frame, mode="determinate", maximum=100, bootstyle=SUCCESS)
        self.health_bar.pack(fill=X)
        self.lbl_health = ttk.Label(health_frame, text="100% synced", font="-size 10")
        self.lbl_health.pack(anchor=W, pady=(5, 0))
        self.lbl_last_sync = ttk.Label(health_frame, text="Last synced: Never", font="-size 9", bootstyle=SECONDARY)
        self.lbl_last_sync.pack(anchor=W, pady=(5, 0))

        ttk.Separator(sidebar).pack(fill=X, pady=10)

        self.btn_full_sync = ttk.Button(sidebar, text="🔄 Full Sync", bootstyle=PRIMARY, command=self.run_full_sync)
        self.btn_full_sync.pack(fill=X, pady=(0, 8))
        ToolTip(self.btn_full_sync, text="Pull cloud changes, then push local changes in one step")

        pp_row = ttk.Frame(sidebar)
        pp_row.pack(fill=X, pady=(0, 8))
        self.btn_pull = ttk.Button(pp_row, text="↓ Pull", bootstyle=INFO, command=self.run_pull)
        self.btn_pull.pack(side=LEFT, fill=X, expand=True, padx=(0, 4))
        self.btn_push = ttk.Button(pp_row, text="↑ Push", bootstyle=SUCCESS, command=self.run_push)
        self.btn_push.pack(side=LEFT, fill=X, expand=True, padx=(4, 0))

        ttk.Button(
            sidebar, text="⚙ Configure Databases", bootstyle="outline-light",
            command=lambda: ConfigDialog(self)
        ).pack(fill=X, side=BOTTOM, pady=(20, 0))

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

        self.log_tree.tag_configure('error', foreground='#ff4444')
        self.log_tree.tag_configure('warning', foreground='#ffbb33')
        self.log_tree.tag_configure('info', foreground='white')

        scrollbar = ttk.Scrollbar(body, orient=VERTICAL, command=self.log_tree.yview)
        self.log_tree.configure(yscrollcommand=scrollbar.set)

        self.log_tree.pack(side=LEFT, fill=BOTH, expand=True)
        scrollbar.pack(side=RIGHT, fill=Y)

        gui_logger = TkinterLogHandler(self.log_tree)
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
        headings = {
            "attendee_id": "ATTENDEE ID", "full_name": "NAME",
            "local_updated": "LOCAL UPDATED", "cloud_updated": "CLOUD UPDATED",
            "fields": "FIELDS DIFFERING",
        }
        for c in cols:
            self.conflict_tree.heading(c, text=headings[c], anchor=W)
        self.conflict_tree.column("attendee_id", width=140, stretch=False)
        self.conflict_tree.column("full_name", width=160, stretch=False)
        self.conflict_tree.column("local_updated", width=150, stretch=False)
        self.conflict_tree.column("cloud_updated", width=150, stretch=False)
        self.conflict_tree.tag_configure('severe', foreground='#ff4444')
        self.conflict_tree.bind("<Double-1>", self.on_conflict_double_click)

        self._conflict_scroll = ttk.Scrollbar(body, orient=VERTICAL, command=self.conflict_tree.yview)
        self.conflict_tree.configure(yscrollcommand=self._conflict_scroll.set)

        self.conflict_empty_label = ttk.Label(
            body, text="✅  No conflicts right now — everything's in sync.",
            font="-size 12", bootstyle=SUCCESS, anchor=CENTER, justify=CENTER,
        )

    def _create_stat_card(self, parent, title, initial_value, style, var_name):
        frame = ttk.Frame(parent, borderwidth=1, relief="solid", padding=20)
        frame.pack(side=LEFT, fill=BOTH, expand=True, padx=5)

        ttk.Label(frame, text=title, font="-size 9 -weight bold", bootstyle=style).pack(anchor=W)
        val_lbl = ttk.Label(frame, text=initial_value, font="-size 26 -weight bold")
        val_lbl.pack(anchor=W, pady=(10, 0))

        self.stat_vars[var_name] = val_lbl
        return val_lbl

    def refresh_stats(self):
        if not self.sync_manager.SessionMySQL:
            self.lbl_mysql.configure(text="● MySQL (Primary): Offline", bootstyle=DANGER)
            self.lbl_sqlite.configure(text="● SQLite (Fallback): Check Config", bootstyle=DANGER)
            self.lbl_supa.configure(text="● Supabase Cloud: Idle", bootstyle=SECONDARY)
            self.health_bar.configure(bootstyle=DANGER)
            self.health_bar['value'] = 0
            self.lbl_health.configure(text="No local database connected")
            self.lbl_last_sync.configure(text=f"Last synced: {_relative_time(self.sync_manager.last_sync_at)}")
            self.refresh_conflicts_table()
            return

        self.lbl_mysql.configure(text="● MySQL (Primary): Online", bootstyle=SUCCESS)
        self.lbl_sqlite.configure(
            text="● SQLite (Fallback): Ready" if self.sync_manager.SessionSQLite else "● SQLite (Fallback): Offline",
            bootstyle=SUCCESS if self.sync_manager.SessionSQLite else DANGER,
        )

        if self.sync_manager.state == SyncState.ERROR:
            self.lbl_supa.configure(text="● Supabase Cloud: Error", bootstyle=DANGER)
        elif not self.is_syncing:
            self.lbl_supa.configure(text="● Supabase Cloud: Idle", bootstyle=SECONDARY)

        stats = self.sync_manager.get_dashboard_stats()

        self.stat_vars["mysql_total"].configure(text=str(stats["mysql_total"]))
        self.stat_vars["sqlite_total"].configure(text=str(stats["sqlite_total"]))
        self.stat_vars["pending_push"].configure(text=str(stats["pending_push"]))
        self.stat_vars["conflicts"].configure(text=str(stats["conflict_count"]))
        self.stat_vars["kiosk_reg"].configure(text=str(stats["kiosk_reg"]))
        self.stat_vars["checked_in"].configure(text=str(stats["checked_in"]))
        for day in EVENT_DAYS:
            self.stat_vars[f"day_{day}"].configure(text=str(stats["day_counts"].get(day, 0)))

        health = _compute_sync_health(stats)
        health_style = SUCCESS if health >= 95 else (WARNING if health >= 80 else DANGER)
        self.health_bar.configure(bootstyle=health_style)
        self.health_bar['value'] = health
        self.lbl_health.configure(text=f"{health}% synced")
        self.lbl_last_sync.configure(text=f"Last synced: {_relative_time(self.sync_manager.last_sync_at)}")

        self.refresh_conflicts_table()

    def refresh_conflicts_table(self):
        for row in self.conflict_tree.get_children():
            self.conflict_tree.delete(row)

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
                tags = ('severe',) if len(c["diff_fields"]) >= 5 else ()
                self.conflict_tree.insert('', END, iid=c["id"], tags=tags, values=(
                    c["attendee_id"], c["full_name"],
                    _fmt_dt(c["local_updated_at"]), _fmt_dt(c["cloud_updated_at"]),
                    _fields_summary(c["diff_fields"]),
                ))

        count = len(conflicts)
        self.notebook.tab(self.conflicts_tab, text=f"Conflicts ({count})" if count else "Conflicts")

    def clear_log(self):
        for row in self.log_tree.get_children():
            self.log_tree.delete(row)

    def _set_controls_state(self, state):
        for btn in (self.btn_pull, self.btn_push, self.btn_full_sync,
                    self.btn_keep_local, self.btn_keep_cloud, self.btn_resolve_all):
            btn.configure(state=state)

    def _lock_ui(self, mode="syncing"):
        self.is_syncing = True
        self._set_controls_state(DISABLED)
        self.progress.start(10)
        self.lbl_supa.configure(text=f"● Supabase Cloud: {mode.title()}...", bootstyle=INFO)

    def _unlock_ui(self, msg="Ready."):
        self.is_syncing = False
        self._set_controls_state(NORMAL)
        self.progress.stop()
        self.lbl_status.configure(text=msg)
        self.refresh_stats()

    def _lock_for_resolve(self):
        self.is_syncing = True
        self._set_controls_state(DISABLED)
        self.progress.start(10)
        self.lbl_status.configure(text="Applying conflict resolution...")

    def _unlock_after_resolve(self, msg):
        self.is_syncing = False
        self._set_controls_state(NORMAL)
        self.progress.stop()
        self.lbl_status.configure(text=msg)
        self.refresh_stats()

    def run_push(self):
        if self.is_syncing: return
        self._lock_ui(mode="pushing")
        self.lbl_status.configure(text="Connecting to cloud and pushing data...")
        threading.Thread(target=self._thread_push, daemon=True).start()

    def _thread_push(self):
        success = self.sync_manager.push_to_cloud()
        msg = "Push Complete." if success else f"Push Failed: {self.sync_manager.last_error or 'Unknown error.'}"
        self.after(0, lambda: self._unlock_ui(msg))

    def run_pull(self):
        if self.is_syncing: return
        self._lock_ui(mode="pulling")
        self.lbl_status.configure(text="Connecting to cloud and pulling data...")
        threading.Thread(target=self._thread_pull, daemon=True).start()

    def _thread_pull(self):
        success = self.sync_manager.pull_from_cloud()
        if success and self.sync_manager.conflicts:
            msg = f"Pull Complete — {len(self.sync_manager.conflicts)} conflict(s) need your review."
        elif success:
            msg = "Pull Complete."
        else:
            msg = f"Pull Failed: {self.sync_manager.last_error or 'Unknown error.'}"
        self.after(0, lambda: self._unlock_ui(msg))

    def run_full_sync(self):
        if self.is_syncing: return
        self._lock_ui(mode="syncing")
        self.lbl_status.configure(text="Running full sync (pull then push)...")
        threading.Thread(target=self._thread_full_sync, daemon=True).start()

    def _thread_full_sync(self):
        success = self.sync_manager.trigger_full_sync()
        if success and self.sync_manager.conflicts:
            msg = f"Full Sync Complete — {len(self.sync_manager.conflicts)} conflict(s) need your review."
        elif success:
            msg = "Full Sync Complete."
        else:
            msg = f"Full Sync Failed: {self.sync_manager.last_error or 'Unknown error.'}"
        self.after(0, lambda: self._unlock_ui(msg))

    def on_conflict_double_click(self, event):
        row_id = self.conflict_tree.identify_row(event.y)
        if not row_id: return
        conflict = self.sync_manager.conflicts.get(row_id)
        if not conflict: return
        ConflictDetailDialog(self, conflict, on_resolve=self._resolve_and_refresh)

    def resolve_selected(self, keep):
        selected = self.conflict_tree.selection()
        if not selected:
            Messagebox.show_warning("Select one or more rows first.", "Nothing Selected", parent=self)
            return
        self._resolve_and_refresh(list(selected), keep)

    def resolve_all_conflicts_bulk(self):
        n = len(self.sync_manager.conflicts)
        if n == 0: return
        result = Messagebox.yesno(
            f"This will auto-resolve all {n} conflict(s): for each, whichever side (local or "
            f"cloud) changed most recently wins, and individual review is skipped. Continue?",
            title="Resolve All Conflicts",
            parent=self,
        )
        if result == "Yes":
            self._resolve_and_refresh(list(self.sync_manager.conflicts.keys()), "newest")

    def _resolve_and_refresh(self, conflict_ids, keep):
        if self.is_syncing: return
        self._lock_for_resolve()
        threading.Thread(target=self._thread_resolve, args=(conflict_ids, keep), daemon=True).start()

    def _thread_resolve(self, conflict_ids, keep):
        resolved = 0
        for cid in conflict_ids:
            conflict = self.sync_manager.conflicts.get(cid)
            if not conflict: continue
            side = keep
            if keep == "newest":
                side = "cloud" if conflict["cloud_updated_at"] > conflict["local_updated_at"] else "local"
            if self.sync_manager.resolve_conflict(cid, side):
                resolved += 1

        if resolved:
            self.sync_manager.mirror_mysql_to_sqlite()

        msg = f"Resolved {resolved} conflict(s)." if resolved else "No conflicts were resolved."
        self.after(0, lambda: self._unlock_after_resolve(msg))

if __name__ == "__main__":
    app = SyncDashboard()
    app.mainloop()