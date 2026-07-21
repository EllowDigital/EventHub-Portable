import os
import json
import logging
import threading
from datetime import datetime, timezone
from supabase import create_client, Client
from sqlalchemy.orm import sessionmaker
import ttkbootstrap as ttk
from ttkbootstrap.constants import *
from ttkbootstrap.dialogs import Messagebox

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
LOG_DIR = os.path.join(BASE_DIR, 'logs')

os.makedirs(CONFIG_DIR, exist_ok=True)
os.makedirs(LOG_DIR, exist_ok=True)

# ==============================================================================
# CUSTOM LOGGING HANDLER FOR TKINTER
# ==============================================================================
class TkinterLogHandler(logging.Handler):
    """Streams logs directly into the GUI Treeview safely."""
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
    """Loads credentials and creates a fresh client ONLY when called."""
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
# CORE SYNC MANAGER CLASS
# ==============================================================================
class SyncManager:
    def __init__(self):
        self.SessionMySQL = None
        self.SessionSQLite = None
        self.connect_local_dbs()

    def connect_local_dbs(self):
        """Connects ONLY to the local databases on startup. Supabase remains completely offline."""
        try:
            sessions = get_database_sessions()
            self.SessionMySQL = sessions.get('mysql')
            self.SessionSQLite = sessions.get('sqlite')
            logging.info("Local databases verified. Cloud connection is IDLE.")
        except Exception as e:
            logging.error(f"Local Database Connection Failed: {e}")

    def _merge_checkin_history(self, local_history, cloud_history) -> dict:
        if isinstance(local_history, str): local_history = json.loads(local_history) if local_history else {}
        if isinstance(cloud_history, str): cloud_history = json.loads(cloud_history) if cloud_history else {}
        if not isinstance(local_history, dict): local_history = {}
        if not isinstance(cloud_history, dict): cloud_history = {}
        
        merged = cloud_history.copy()
        merged.update(local_history)
        return merged

    def mirror_mysql_to_sqlite(self):
        """Lightning-fast bulk mirror to prevent SQLite locks and corruption."""
        if not self.SessionSQLite or not self.SessionMySQL: return
        logging.info("Starting MySQL -> SQLite mirror process...")
        
        mysql_session = self.SessionMySQL()
        sqlite_session = self.SessionSQLite()
        
        try:
            mysql_attendees = mysql_session.query(Attendee).all()
            
            # Serialize for bulk insertion
            data_dicts = []
            for m_att in mysql_attendees:
                row_data = {c.name: getattr(m_att, c.name) for c in m_att.__table__.columns}
                data_dicts.append(row_data)

            # Flush and replace for a true 1:1 mirror
            sqlite_session.query(Attendee).delete()
            if data_dicts:
                sqlite_session.bulk_insert_mappings(Attendee, data_dicts)
                
            sqlite_session.commit()
            logging.info(f"Mirror complete: {len(data_dicts)} records safely backed up.")
        except Exception as e:
            sqlite_session.rollback()
            logging.error(f"Mirror error: {e}")
        finally:
            mysql_session.close()
            sqlite_session.close()

    def push_to_cloud(self):
        """Executes manual push. ONLY connects to Supabase for the duration of this function."""
        logging.info("--- Starting PUSH to Cloud ---")
        if not self.SessionMySQL:
            logging.error("Cannot push: Local MySQL is offline.")
            return False
            
        try:
            supabase = load_supabase_client()
            logging.info("Successfully established temporary cloud connection.")
        except Exception as e:
            logging.error(f"Failed to connect to Supabase: {e}")
            return False

        mysql_session = self.SessionMySQL()
        try:
            pending = mysql_session.query(Attendee).filter_by(needs_cloud_sync=True).all()
            if not pending:
                logging.info("No records require pushing. Disconnecting from cloud.")
                return True

            batch_payload = []
            for record in pending:
                # Build payload matching 100% of Supabase columns
                # Exclude `local_modified` and `device_name` as they are local-only
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
                    "attendance_days": record.attendance_days,
                    "photo_url": record.photo_url,
                    "checkin_history": record.checkin_history,
                    "needs_sheet_sync": record.needs_sheet_sync,
                    "created_at": record.created_at.isoformat() if record.created_at else datetime.now(timezone.utc).isoformat(),
                    "updated_at": record.updated_at.isoformat() if record.updated_at else datetime.now(timezone.utc).isoformat(),
                    "needs_cloud_sync": False 
                })

            response = supabase.table('attendees').upsert(batch_payload).execute()
            
            if response.data:
                for record in pending:
                    record.needs_cloud_sync = False
                mysql_session.commit()
                logging.info(f"Successfully pushed {len(batch_payload)} records in batch.")
                self.mirror_mysql_to_sqlite()
                return True
            else:
                logging.warning("Cloud upload rejected the batch payload.")
                return False

        except Exception as e:
            mysql_session.rollback()
            logging.error(f"Push failed: {e}")
            return False
        finally:
            mysql_session.close()

    def pull_from_cloud(self):
        """Executes manual pull. ONLY connects to Supabase for the duration of this function."""
        logging.info("--- Starting PULL from Cloud ---")
        if not self.SessionMySQL:
            logging.error("Cannot pull: Local MySQL is offline.")
            return False
            
        try:
            supabase = load_supabase_client()
            logging.info("Successfully established temporary cloud connection.")
        except Exception as e:
            logging.error(f"Failed to connect to Supabase: {e}")
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

            if not cloud_records:
                logging.info("Cloud is empty. Disconnecting.")
                return True

            pulled = 0
            for cloud_data in cloud_records:
                local_record = mysql_session.query(Attendee).filter_by(id=cloud_data['id']).first()
                
                # Parse updated_at securely
                raw_updated = cloud_data.get('updated_at')
                if raw_updated:
                    try:
                        cloud_updated_at = datetime.fromisoformat(raw_updated.replace('Z', '+00:00')).replace(tzinfo=None)
                    except Exception:
                        cloud_updated_at = datetime.now(timezone.utc).replace(tzinfo=None)
                else:
                    cloud_updated_at = datetime.now(timezone.utc).replace(tzinfo=None)
                    
                # Parse created_at securely
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
                    
                    if local_record.needs_cloud_sync: continue 
                    
                    if not local_record.updated_at or cloud_updated_at > local_record.updated_at:
                        # Update all fields to match cloud
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
                        
                        if not local_record.created_at:
                            local_record.created_at = cloud_created_at
                            
                        local_record.updated_at = cloud_updated_at
                        pulled += 1
                else:
                    # New Insert - All Supabase columns mapped, local-only set to defaults
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
                        # Keep local-only columns clean
                        local_modified=False,
                        device_name=None
                    )
                    mysql_session.add(new_attendee)
                    pulled += 1
                    
            mysql_session.commit()
            logging.info(f"Pulled {pulled} new updates from cloud.")
            self.mirror_mysql_to_sqlite()
            return True
            
        except Exception as e:
            mysql_session.rollback()
            logging.error(f"Pull failed: {e}")
            return False
        finally:
            mysql_session.close()

# ==============================================================================
# CONFIGURATION GUI DIALOG
# ==============================================================================
class ConfigDialog(ttk.Toplevel):
    def __init__(self, parent):
        super().__init__()
        self.title("Configure Databases")
        self.transient(parent) 
        self.geometry("500x520")
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
        
        ttk.Separator(frame).pack(fill=X, pady=15)
        
        ttk.Label(frame, text="MySQL Settings (Local Hub)", font="-weight bold").pack(anchor=W, pady=(0, 10))
        my_conf = self.schema.get("mysql", {})
        self.ent_my_host = self._make_input(frame, "Host", my_conf.get("host", "localhost"))
        self.ent_my_user = self._make_input(frame, "User", my_conf.get("user", "root"))
        self.ent_my_pass = self._make_input(frame, "Password", my_conf.get("password", ""), show="*")
        self.ent_my_db   = self._make_input(frame, "Database", my_conf.get("database", "eventhub_db"))

        btn_frame = ttk.Frame(frame)
        btn_frame.pack(fill=X, pady=20)
        ttk.Button(btn_frame, text="Save Settings", bootstyle=SUCCESS, command=self.save).pack(side=RIGHT, padx=5)
        ttk.Button(btn_frame, text="Cancel", bootstyle=SECONDARY, command=self.destroy).pack(side=RIGHT)

    def _make_input(self, parent, label, default, show=None):
        row = ttk.Frame(parent)
        row.pack(fill=X, pady=3)
        ttk.Label(row, text=label, width=15).pack(side=LEFT)
        ent = ttk.Entry(row, show=show)
        ent.insert(0, default)
        ent.pack(side=LEFT, fill=X, expand=True)
        return ent

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
# MAIN DASHBOARD GUI
# ==============================================================================
class SyncDashboard(ttk.Window):
    def __init__(self):
        super().__init__(themename="cyborg", title="TDE UP 2026 — Sync Manager v4.0")
        self.geometry("1500x850")
        
        self.sync_manager = SyncManager()
        self.is_syncing = False
        
        self.build_ui()
        self.refresh_stats()

    def reinitialize_manager(self):
        self.sync_manager = SyncManager()
        self.refresh_stats()

    def build_ui(self):
        main_paned = ttk.Panedwindow(self, orient=HORIZONTAL)
        main_paned.pack(fill=BOTH, expand=True)

        # --- LEFT SIDEBAR ---
        sidebar = ttk.Frame(main_paned, width=300, padding=20)
        main_paned.add(sidebar, weight=0)

        ttk.Label(sidebar, text="TDE UP 2026", font="-size 20 -weight bold", bootstyle=PRIMARY).pack(anchor=W)
        ttk.Label(sidebar, text="Sync Manager v4.0\n", font="-size 10", foreground="gray").pack(anchor=W, pady=(0,20))

        ttk.Label(sidebar, text="CONNECTION STATUS", font="-size 8 -weight bold").pack(anchor=W, pady=(10,5))
        
        self.lbl_supa = ttk.Label(sidebar, text="● Supabase Cloud: Idle", bootstyle=SECONDARY)
        self.lbl_supa.pack(anchor=W, pady=2)
        self.lbl_mysql = ttk.Label(sidebar, text="● MySQL (Primary): Checking...", bootstyle=INFO)
        self.lbl_mysql.pack(anchor=W, pady=2)
        self.lbl_sqlite = ttk.Label(sidebar, text="● SQLite (Fallback): Checking...", bootstyle=INFO)
        self.lbl_sqlite.pack(anchor=W, pady=2)
        
        ttk.Button(sidebar, text="⟳ Refresh Connections", bootstyle="outline-secondary", command=self.reinitialize_manager).pack(fill=X, pady=15)
        ttk.Separator(sidebar).pack(fill=X, pady=20)
        
        ttk.Button(sidebar, text="⚙ Configure Databases", bootstyle="outline-light", command=lambda: ConfigDialog(self)).pack(fill=X, side=BOTTOM, pady=20)

        # --- RIGHT CONTENT AREA ---
        content = ttk.Frame(main_paned, padding=20)
        main_paned.add(content, weight=1)

        ttk.Label(content, text="Database Synchronisation Dashboard", font="-size 16 -weight bold").pack(anchor=W, pady=(0, 20))

        # --- TELEMETRY CARDS (2 ROWS, 4 COLS) ---
        self.stat_vars = {} 
        
        # ROW 1 (Database Health & Sync)
        cards_row1 = ttk.Frame(content)
        cards_row1.pack(fill=X, pady=(0,10))
        
        self._create_stat_card(cards_row1, "👥 MYSQL (PRIMARY)", "0", PRIMARY, var_name="mysql_total")
        self._create_stat_card(cards_row1, "💾 SQLITE (MIRROR)", "0", INFO, var_name="sqlite_total")
        self._create_stat_card(cards_row1, "⏳ PENDING PUSH", "0", WARNING, var_name="pending_push")
        self._create_stat_card(cards_row1, "🖥️ KIOSK REG.", "0", SECONDARY, var_name="kiosk_reg")

        # ROW 2 (Event Operations)
        cards_row2 = ttk.Frame(content)
        cards_row2.pack(fill=X, pady=(0,20))
        
        self._create_stat_card(cards_row2, "✔ TOTAL CHECKED IN", "0", SUCCESS, var_name="checked_in")
        self._create_stat_card(cards_row2, "📅 30 AUGUST 2026", "0", LIGHT, var_name="day_1")
        self._create_stat_card(cards_row2, "📅 31 AUGUST 2026", "0", LIGHT, var_name="day_2")
        self._create_stat_card(cards_row2, "📅 1 SEPTEMBER 2026", "0", LIGHT, var_name="day_3")

        # --- CONTROLS ---
        controls_frame = ttk.Frame(content)
        controls_frame.pack(fill=X, pady=10)
        
        self.btn_pull = ttk.Button(controls_frame, text="↓ Pull from Cloud", bootstyle=PRIMARY, width=20, command=self.run_pull)
        self.btn_pull.pack(side=LEFT, padx=(0,10))
        
        self.btn_push = ttk.Button(controls_frame, text="↑ Push to Cloud", bootstyle=SUCCESS, width=20, command=self.run_push)
        self.btn_push.pack(side=LEFT, padx=(0,10))

        self.progress = ttk.Progressbar(controls_frame, mode='indeterminate', bootstyle=INFO)
        self.progress.pack(side=LEFT, fill=X, expand=True, padx=10)
        
        self.lbl_status = ttk.Label(controls_frame, text="Ready.")
        self.lbl_status.pack(side=LEFT, padx=10)

        # --- LOGS ---
        notebook = ttk.Notebook(content)
        notebook.pack(fill=BOTH, expand=True, pady=(20,0))
        
        log_tab = ttk.Frame(notebook)
        notebook.add(log_tab, text="Activity Log")
        
        cols = ("Time", "Level", "Message")
        self.log_tree = ttk.Treeview(log_tab, columns=cols, show="headings", bootstyle=INFO)
        self.log_tree.heading("Time", text="TIME", anchor=W)
        self.log_tree.heading("Level", text="LEVEL", anchor=W)
        self.log_tree.heading("Message", text="MESSAGE", anchor=W)
        self.log_tree.column("Time", width=100, stretch=False)
        self.log_tree.column("Level", width=100, stretch=False)
        
        self.log_tree.tag_configure('error', foreground='#ff4444')
        self.log_tree.tag_configure('warning', foreground='#ffbb33')
        self.log_tree.tag_configure('info', foreground='white')
        
        scrollbar = ttk.Scrollbar(log_tab, orient=VERTICAL, command=self.log_tree.yview)
        self.log_tree.configure(yscrollcommand=scrollbar.set)
        
        self.log_tree.pack(side=LEFT, fill=BOTH, expand=True)
        scrollbar.pack(side=RIGHT, fill=Y)

        gui_logger = TkinterLogHandler(self.log_tree)
        gui_logger.setFormatter(logging.Formatter('%(message)s'))
        logging.getLogger().addHandler(gui_logger)

    def _create_stat_card(self, parent, title, initial_value, style, var_name):
        frame = ttk.Frame(parent, borderwidth=1, relief="solid", padding=20)
        frame.pack(side=LEFT, fill=BOTH, expand=True, padx=5)
        
        ttk.Label(frame, text=title, font="-size 9 -weight bold", bootstyle=style).pack(anchor=W)
        val_lbl = ttk.Label(frame, text=initial_value, font="-size 26 -weight bold")
        val_lbl.pack(anchor=W, pady=(10,0))
        
        self.stat_vars[var_name] = val_lbl 
        return val_lbl

    def refresh_stats(self):
        if not self.sync_manager.SessionMySQL:
            self.lbl_mysql.configure(text="● MySQL (Primary): Offline", bootstyle=DANGER)
            self.lbl_sqlite.configure(text="● SQLite (Fallback): Check Config", bootstyle=DANGER)
            return
            
        self.lbl_mysql.configure(text="● MySQL (Primary): Online", bootstyle=SUCCESS)
        self.lbl_sqlite.configure(text="● SQLite (Fallback): Ready", bootstyle=SUCCESS if self.sync_manager.SessionSQLite else DANGER)
        self.lbl_supa.configure(text="● Supabase Cloud: Idle", bootstyle=SECONDARY)
        
        mysql_session = self.sync_manager.SessionMySQL()
        sqlite_session = self.sync_manager.SessionSQLite() if self.sync_manager.SessionSQLite else None
        
        try:
            attendees = mysql_session.query(Attendee).all()
            kiosk_regs = mysql_session.query(OfflineKioskAttendee).count()
            
            total_mysql = len(attendees)
            pending_push = 0
            checked_in = 0
            day1_count = 0
            day2_count = 0
            day3_count = 0
            
            for att in attendees:
                if att.needs_cloud_sync:
                    pending_push += 1
                
                history = att.checkin_history
                if isinstance(history, str):
                    try:
                        history = json.loads(history)
                    except:
                        history = {}
                        
                if history and len(history) > 0:
                    checked_in += 1
                    history_str = json.dumps(history)
                    if "2026-08-30" in history_str: day1_count += 1
                    if "2026-08-31" in history_str: day2_count += 1
                    if "2026-09-01" in history_str: day3_count += 1

            total_sqlite = sqlite_session.query(Attendee).count() if sqlite_session else 0

            self.stat_vars["mysql_total"].configure(text=str(total_mysql))
            self.stat_vars["sqlite_total"].configure(text=str(total_sqlite))
            self.stat_vars["pending_push"].configure(text=str(pending_push))
            self.stat_vars["kiosk_reg"].configure(text=str(kiosk_regs))
            
            self.stat_vars["checked_in"].configure(text=str(checked_in))
            self.stat_vars["day_1"].configure(text=str(day1_count))
            self.stat_vars["day_2"].configure(text=str(day2_count))
            self.stat_vars["day_3"].configure(text=str(day3_count))

        except Exception as e:
            logging.error(f"Stat refresh failed: {e}")
        finally:
            mysql_session.close()
            if sqlite_session: sqlite_session.close()

    def _lock_ui(self, mode="syncing"):
        self.is_syncing = True
        self.btn_pull.configure(state=DISABLED)
        self.btn_push.configure(state=DISABLED)
        self.progress.start(10)
        self.lbl_supa.configure(text=f"● Supabase Cloud: {mode.title()}...", bootstyle=INFO)
        
    def _unlock_ui(self, msg="Ready."):
        self.is_syncing = False
        self.btn_pull.configure(state=NORMAL)
        self.btn_push.configure(state=NORMAL)
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
        self.after(0, lambda: self._unlock_ui("Push Complete." if success else "Push Failed."))

    def run_pull(self):
        if self.is_syncing: return
        self._lock_ui(mode="pulling")
        self.lbl_status.configure(text="Connecting to cloud and pulling data...")
        threading.Thread(target=self._thread_pull, daemon=True).start()

    def _thread_pull(self):
        success = self.sync_manager.pull_from_cloud()
        self.after(0, lambda: self._unlock_ui("Pull Complete." if success else "Pull Failed."))

if __name__ == "__main__":
    app = SyncDashboard()
    app.mainloop()