import os
import sys
import json
import logging
import threading
import queue
import csv
import requests
from requests.adapters import HTTPAdapter
from urllib3.util.retry import Retry
import urllib3
from datetime import datetime
import tkinter as tk
from tkinter import filedialog, messagebox, simpledialog

import ttkbootstrap as ttk
from ttkbootstrap.constants import *
from PIL import Image, ImageTk, ImageOps

# Import SQLAlchemy components for direct DB connections
from sqlalchemy import create_engine, text
from sqlalchemy.engine import make_url
from sqlalchemy.orm import sessionmaker
from sqlalchemy.exc import SQLAlchemyError

# Suppress insecure HTTPS warnings for local hub connections
urllib3.disable_warnings(urllib3.exceptions.InsecureRequestWarning)

# Import models and DB initialization from your schema
try:
    from app.schema import Attendee, OfflineKioskAttendee, get_database_sessions
except ModuleNotFoundError:
    from schema import Attendee, OfflineKioskAttendee, get_database_sessions

# ==============================================================================
# 24/7 STABILITY: GLOBAL CRASH HANDLER
# ==============================================================================
def global_exception_handler(*args):
    logging.error("Uncaught GUI Exception intercepted. App remains running.", exc_info=args)

tk.Tk.report_callback_exception = global_exception_handler

# ==============================================================================
# PATHS & CONFIG (USING EXPLORER.JSON)
# ==============================================================================
BASE_DIR = os.path.dirname(os.path.abspath(__file__))
PHOTOS_DIR = os.path.join(BASE_DIR, 'attendee_photos')
CONFIG_DIR = os.path.join(BASE_DIR, 'config')
EXPLORER_CONFIG = os.path.join(CONFIG_DIR, 'explorer.json')
os.makedirs(PHOTOS_DIR, exist_ok=True)
os.makedirs(CONFIG_DIR, exist_ok=True)

# ==============================================================================
# API MOCK OBJECT
# ==============================================================================
class APIRecord:
    def __init__(self, d):
        self.id = d.get("id")
        self.attendee_id = d.get("attendee_id", "")
        self.full_name = d.get("full_name", "Unknown")
        self.mobile = d.get("mobile", "")
        self.email = d.get("email", "")
        
        class EnumMock:
            def __init__(self, name): self.name = name
            
        self.gender = EnumMock(d.get("gender", "OTHER"))
        self.attendee_type = EnumMock(d.get("attendee_type", "GENERAL"))
        
        self.business_name = d.get("business_name", "")
        self.business_category = d.get("business_category", "")
        self.city = d.get("city", "")
        self.state = d.get("state", "")
        self.pincode = d.get("pincode", "")
        self.needs_cloud_sync = d.get("needs_cloud_sync", False)
        self.checkin_history = d.get("checkin_history", {})
        
        try:
            raw_date = d.get("created_at", "").replace("Z", "+00:00")
            self.created_at = datetime.fromisoformat(raw_date).replace(tzinfo=None)
        except Exception:
            self.created_at = datetime.min


# ==============================================================================
# CUSTOM UI DIALOGS (EASY DB CONFIGURATION)
# ==============================================================================
class DatabaseConfigDialog(tk.Toplevel):
    def __init__(self, parent, current_uri, on_save_callback):
        super().__init__(parent)
        self.title("MySQL Database Configuration")
        self.geometry("450x420")
        self.resizable(False, False)
        self.transient(parent)
        self.grab_set()
        
        # Center the dialog
        self.update_idletasks()
        x = parent.winfo_x() + (parent.winfo_width() // 2) - (450 // 2)
        y = parent.winfo_y() + (parent.winfo_height() // 2) - (420 // 2)
        self.geometry(f"+{x}+{y}")

        self.on_save_callback = on_save_callback
        
        # Parse existing URI to populate fields
        host, port, user, pwd, db = "localhost", "3306", "root", "", "tde_database"
        if current_uri and "mysql+pymysql" in current_uri:
            try:
                url_obj = make_url(current_uri)
                host = url_obj.host or "localhost"
                port = str(url_obj.port) if url_obj.port else "3306"
                user = url_obj.username or ""
                pwd = url_obj.password or ""
                db = url_obj.database or ""
            except Exception:
                pass

        # UI Layout
        container = ttk.Frame(self, padding=25)
        container.pack(fill=BOTH, expand=True)
        
        ttk.Label(container, text="MySQL Connection Details", font="-size 14 -weight bold", bootstyle=PRIMARY).pack(anchor=W, pady=(0, 5))
        ttk.Label(container, text="Enter your database credentials below.", font="-size 9", foreground="gray").pack(anchor=W, pady=(0, 20))

        # Form Fields
        self.vars = {
            "host": tk.StringVar(value=host),
            "port": tk.StringVar(value=port),
            "user": tk.StringVar(value=user),
            "pwd": tk.StringVar(value=pwd),
            "db": tk.StringVar(value=db)
        }

        self._build_field(container, "Host Address:", self.vars["host"], "e.g., localhost or 192.168.1.5")
        self._build_field(container, "Port:", self.vars["port"], "e.g., 3306")
        self._build_field(container, "Username:", self.vars["user"], "e.g., root")
        self._build_field(container, "Password:", self.vars["pwd"], "Leave blank if no password", is_password=True)
        self._build_field(container, "Database Name:", self.vars["db"], "e.g., tde_database")

        # Buttons
        btn_frame = ttk.Frame(container)
        btn_frame.pack(fill=X, pady=(20, 0))
        
        ttk.Button(btn_frame, text="Test Connection", bootstyle="outline-info", command=self.test_connection).pack(side=LEFT)
        ttk.Button(btn_frame, text="Save Settings", bootstyle="success", command=self.save_settings).pack(side=RIGHT, padx=(10, 0))
        ttk.Button(btn_frame, text="Cancel", bootstyle="secondary", command=self.destroy).pack(side=RIGHT)

    def _build_field(self, parent, label_text, text_var, placeholder="", is_password=False):
        frame = ttk.Frame(parent)
        frame.pack(fill=X, pady=4)
        ttk.Label(frame, text=label_text, width=15, font="-size 9 -weight bold").pack(side=LEFT)
        entry = ttk.Entry(frame, textvariable=text_var, show="*" if is_password else "")
        entry.pack(side=LEFT, fill=X, expand=True)

    def build_uri(self):
        h, po, u, pw, d = (self.vars[k].get().strip() for k in ["host", "port", "user", "pwd", "db"])
        auth = f"{u}:{pw}" if pw else u
        port_str = f":{po}" if po else ":3306"
        return f"mysql+pymysql://{auth}@{h}{port_str}/{d}"

    def test_connection(self):
        uri = self.build_uri()
        try:
            engine = create_engine(uri, connect_args={"connect_timeout": 3})
            with engine.connect() as conn:
                conn.execute(text("SELECT 1"))
            messagebox.showinfo("Success", "Connection successful!\nThe database is reachable.", parent=self)
        except SQLAlchemyError as e:
            messagebox.showerror("Connection Failed", f"Could not connect to the database.\n\nError:\n{str(e).split(']')[0]}]", parent=self)

    def save_settings(self):
        uri = self.build_uri()
        self.on_save_callback(uri)
        self.destroy()


# ==============================================================================
# MAIN APPLICATION
# ==============================================================================
class AttendeeExplorer(ttk.Window):
    def __init__(self):
        super().__init__(themename="darkly", title="TDE UP 2026 — Attendee Explorer")
        
        sw, sh = self.winfo_screenwidth(), self.winfo_screenheight()
        ww, wh = max(1200, min(1450, int(sw * 0.90))), max(800, min(950, int(sh * 0.90)))
        self.geometry(f"{ww}x{wh}+{max(0, (sw - ww) // 2)}+{max(0, (sh - wh) // 2 - 20)}")
        self.minsize(1150, 750)
        
        self.gui_queue = queue.Queue()
        self.SessionMySQL = None
        self.all_attendees = []
        self.filtered_attendees = []
        self.current_sort_col = None
        self.sort_reverse = False
        
        # Pagination Control Variables
        self.current_page = 1
        self.page_size = 100
        self.total_pages = 1

        # Setup Resilient API Session with Auto-Retries
        self.api_session = requests.Session()
        retries = Retry(total=5, backoff_factor=0.5, status_forcelist=[500, 502, 503, 504], raise_on_status=False)
        self.api_session.mount('http://', HTTPAdapter(max_retries=retries))
        self.api_session.mount('https://', HTTPAdapter(max_retries=retries))
        
        self._configure_custom_styles()
        self.build_ui()
        self.connect_db()
        
        self.after(50, self._process_gui_queue)
        self.load_data_async(is_manual=True)
        self._auto_refresh_loop()

    # ==========================================================================
    # COLOR MATH & STYLES (ENHANCED UI)
    # ==========================================================================
    def _hex_to_rgb(self, hex_color):
        return tuple(int(hex_color.lstrip('#')[i:i+2], 16) for i in (0, 2, 4))
        
    def _rgb_to_hex(self, rgb):
        return "#{:02x}{:02x}{:02x}".format(*(max(0, min(255, int(round(c)))) for c in rgb))
        
    def _mix_hex(self, c_a, c_b, w):
        return self._rgb_to_hex(a + (b - a) * w for a, b in zip(self._hex_to_rgb(c_a), self._hex_to_rgb(c_b)))

    def _configure_custom_styles(self):
        colors = self.style.colors
        self.CARD_BG = colors.get("dark")
        self.SOFT_BORDER = self._mix_hex(self.CARD_BG, colors.get("fg"), 0.15)
        
        self.style.configure("Card.TFrame", background=self.CARD_BG, bordercolor=self.SOFT_BORDER, borderwidth=1, relief="solid")
        self.style.configure("Flat.TFrame", background=self.CARD_BG)
        
        # Enhanced Treeview for better list readability
        self.style.configure("Treeview", rowheight=34, font="-size 10", borderwidth=0)
        self.style.configure("Treeview.Heading", font="-size 10 -weight bold", padding=(5, 8))
        self.style.map('Treeview', background=[('selected', colors.get('primary'))], foreground=[('selected', 'white')])
        
        self.style.configure("PurpleBadge.TLabel", background="#9b59b6", foreground="#ffffff", padding=(10, 4))

    # ==========================================================================
    # DATABASE & CONFIGURATIONS
    # ==========================================================================
    def connect_db(self):
        try:
            db_uri = None
            if os.path.exists(EXPLORER_CONFIG):
                with open(EXPLORER_CONFIG, 'r') as f:
                    conf = json.load(f)
                    db_uri = conf.get("mysql_uri")

            if db_uri:
                engine = create_engine(db_uri, pool_pre_ping=True, pool_recycle=3600)
                self.SessionMySQL = sessionmaker(bind=engine)
            else:
                sessions = get_database_sessions()
                self.SessionMySQL = sessions.get('mysql')

            if self.SessionMySQL:
                sess = self.SessionMySQL()
                sess.execute(text("SELECT 1"))
                sess.close()
                return True
        except Exception as e:
            logging.error(f"Database Connection Failed: {e}")
            self.SessionMySQL = None
        return False

    def get_current_mysql_uri(self):
        if os.path.exists(EXPLORER_CONFIG):
            try:
                with open(EXPLORER_CONFIG, 'r') as f:
                    return json.load(f).get("mysql_uri", "")
            except Exception: pass
        return ""

    def save_mysql_uri(self, uri):
        try:
            config_data = {}
            if os.path.exists(EXPLORER_CONFIG):
                with open(EXPLORER_CONFIG, 'r') as f:
                    config_data = json.load(f)
                    
            config_data["mysql_uri"] = uri
            
            with open(EXPLORER_CONFIG, 'w') as f:
                json.dump(config_data, f, indent=4)
                
            messagebox.showinfo("Saved", "Database configuration saved successfully!\nReconnecting...")
            self.combo_source.current(0)
            self.load_data_async(is_manual=True)
        except Exception as e:
            messagebox.showerror("Error", f"Failed to save DB Config: {e}")

    def configure_db_url(self):
        """Opens the new, easy-to-use custom Database config dialog"""
        current_uri = self.get_current_mysql_uri()
        DatabaseConfigDialog(self, current_uri, self.save_mysql_uri)

    def configure_api_url(self):
        current_url = "http://127.0.0.1:5000"
        if os.path.exists(EXPLORER_CONFIG):
            try:
                with open(EXPLORER_CONFIG, 'r') as f:
                    current_url = json.load(f).get("hub_url", current_url)
            except Exception: pass

        new_url = simpledialog.askstring(
            "API Configuration", 
            "Enter the Hub API Server URL:\n(e.g., http://192.168.1.100:5000)", 
            initialvalue=current_url, 
            parent=self
        )
        
        if new_url is not None:
            new_url = new_url.strip()
            if not new_url.startswith("http"): new_url = "http://" + new_url
            try:
                config_data = {}
                if os.path.exists(EXPLORER_CONFIG):
                    with open(EXPLORER_CONFIG, 'r') as f:
                        config_data = json.load(f)
                config_data["hub_url"] = new_url
                with open(EXPLORER_CONFIG, 'w') as f:
                    json.dump(config_data, f, indent=4)
                self.combo_source.current(1)
                self.load_data_async(is_manual=True)
            except Exception as e:
                messagebox.showerror("Error", f"Failed to save URL: {e}")

    # ==========================================================================
    # UI CONSTRUCTION
    # ==========================================================================
    def build_ui(self):
        main_container = ttk.Frame(self, padding=25)
        main_container.pack(fill=BOTH, expand=True)

        # -- HEADER & CONTROLS --
        header_frame = ttk.Frame(main_container)
        header_frame.pack(fill=X, pady=(0, 20))

        title_box = ttk.Frame(header_frame)
        title_box.pack(side=LEFT)
        ttk.Label(title_box, text="Attendee Explorer", font="-size 26 -weight bold", bootstyle=PRIMARY).pack(anchor=W)
        ttk.Label(title_box, text="SEARCH, INSPECT & EXPORT PROFILES", font="-size 10 -weight bold", bootstyle=SECONDARY).pack(anchor=W)

        action_box = ttk.Frame(header_frame)
        action_box.pack(side=RIGHT, anchor=S)
        
        self.lbl_record_count = ttk.Label(action_box, text="Loading records...", font="-size 11 -weight bold", bootstyle=INFO)
        self.lbl_record_count.pack(side=LEFT, padx=(0, 15))
        
        self.lbl_conn_status = ttk.Label(action_box, text="● Syncing...", font="-size 10 -weight bold", bootstyle=SECONDARY)
        self.lbl_conn_status.pack(side=LEFT, padx=(0, 20))
        
        self.combo_source = ttk.Combobox(action_box, values=["Source: MySQL (Direct DB)", "Source: Hub API (Portable)"], state="readonly", width=25, font="-size 10 -weight bold")
        self.combo_source.current(0)
        self.combo_source.pack(side=LEFT, padx=(0, 5))
        self.combo_source.bind("<<ComboboxSelected>>", lambda e: self.load_data_async(is_manual=False))
        
        ttk.Button(action_box, text="⚙️ DB Config", bootstyle="outline-warning", command=self.configure_db_url).pack(side=LEFT, padx=(0, 5))
        ttk.Button(action_box, text="⚙️ API Config", bootstyle="outline-secondary", command=self.configure_api_url).pack(side=LEFT, padx=(0, 20))
        
        self.auto_refresh_var = tk.BooleanVar(value=True)
        self.chk_auto = ttk.Checkbutton(action_box, text="Auto-Refresh", variable=self.auto_refresh_var, bootstyle="info-round-toggle")
        self.chk_auto.pack(side=LEFT, padx=(0, 15))

        ttk.Button(action_box, text="📥 Export CSV", bootstyle="outline-success", command=self.export_csv).pack(side=LEFT, padx=5)
        self.btn_refresh = ttk.Button(action_box, text="⟳ Refresh Data", bootstyle="primary", command=lambda: self.load_data_async(is_manual=True))
        self.btn_refresh.pack(side=LEFT, padx=5)

        # -- ANALYTICS HEADER --
        stats_frame = ttk.Frame(main_container)
        stats_frame.pack(fill=X, pady=(0, 15))
        
        self.lbl_stat_gen = self._build_mini_stat(stats_frame, "GENERAL PASS", "primary")
        self.lbl_stat_biz = self._build_mini_stat(stats_frame, "BUSINESS PASS", "warning")
        self.lbl_stat_med = self._build_mini_stat(stats_frame, "MEDIA PASS", "danger")
        self.lbl_stat_exh = self._build_mini_stat(stats_frame, "EXHIBITOR PASS", "info", is_purple=True)

        # -- SPLIT LAYOUT --
        split_frame = ttk.Frame(main_container)
        split_frame.pack(fill=BOTH, expand=True)

        # LEFT PANEL: SEARCH, DATAGRID & PAGINATION
        left_frame = ttk.Frame(split_frame)
        left_frame.pack(side=LEFT, fill=BOTH, expand=True, padx=(0, 20))

        # -- ADVANCED FILTER BAR --
        filter_frame = ttk.Frame(left_frame, style="Card.TFrame", padding=12)
        filter_frame.pack(fill=X, pady=(0, 15))
        
        ttk.Label(filter_frame, text="🔍", font="-size 12", background=self.CARD_BG).pack(side=LEFT, padx=(5, 5))
        self.ent_search = ttk.Entry(filter_frame, font="-size 10", width=22)
        self.ent_search.pack(side=LEFT, fill=X, expand=True, padx=(0, 15))
        self.ent_search.bind("<KeyRelease>", lambda e: self.apply_filters())

        ttk.Label(filter_frame, text="Type:", font="-size 9 -weight bold", background=self.CARD_BG, foreground="gray").pack(side=LEFT, padx=(5, 5))
        self.combo_type = ttk.Combobox(filter_frame, values=["All Types", "GENERAL", "BUSINESS", "MEDIA", "EXHIBITOR"], state="readonly", width=14)
        self.combo_type.current(0)
        self.combo_type.pack(side=LEFT, padx=(0, 15))
        self.combo_type.bind("<<ComboboxSelected>>", lambda e: self.apply_filters())

        ttk.Label(filter_frame, text="Sort By:", font="-size 9 -weight bold", background=self.CARD_BG, foreground="gray").pack(side=LEFT, padx=(5, 5))
        self.combo_sort = ttk.Combobox(filter_frame, values=["Latest First", "Oldest First", "Name (A-Z)", "Name (Z-A)"], state="readonly", width=14)
        self.combo_sort.current(0)
        self.combo_sort.pack(side=LEFT, padx=(0, 15))
        self.combo_sort.bind("<<ComboboxSelected>>", lambda e: self.apply_filters())

        ttk.Button(filter_frame, text="Clear Filters", bootstyle="secondary-link", command=self.clear_filters).pack(side=RIGHT, padx=(5, 5))

        # -- TREEVIEW CONTAINER --
        tree_card = ttk.Frame(left_frame, style="Card.TFrame", padding=1)
        tree_card.pack(fill=BOTH, expand=True)

        cols = ("ID", "Name", "Mobile", "Type", "City", "Synced")
        self.tree = ttk.Treeview(tree_card, columns=cols, show="headings", bootstyle=INFO)
        
        # Tags for alternating row colors
        self.tree.tag_configure('evenrow', background=self.CARD_BG)
        self.tree.tag_configure('oddrow', background=self._mix_hex(self.CARD_BG, "#ffffff", 0.03))
        
        headings = {"ID": "ATTENDEE ID", "Name": "FULL NAME", "Mobile": "MOBILE", "Type": "TYPE", "City": "CITY", "Synced": "CLOUD SYNC"}
        for col, text in headings.items():
            self.tree.heading(col, text=text, anchor=W, command=lambda c=col: self.sort_treeview(c))
        
        self.tree.column("ID", width=130, stretch=False)
        self.tree.column("Name", width=220, stretch=True)
        self.tree.column("Mobile", width=120, stretch=False)
        self.tree.column("Type", width=110, stretch=False)
        self.tree.column("City", width=140, stretch=True)
        self.tree.column("Synced", width=110, stretch=False)
        
        self.tree.bind("<<TreeviewSelect>>", self.on_row_select)
        
        scrollbar = ttk.Scrollbar(tree_card, orient=VERTICAL, command=self.tree.yview)
        self.tree.configure(yscrollcommand=scrollbar.set)
        
        self.tree.pack(side=LEFT, fill=BOTH, expand=True)
        scrollbar.pack(side=RIGHT, fill=Y)

        # -- PAGINATION CONTROL BAR --
        pagi_frame = ttk.Frame(left_frame, style="Card.TFrame", padding=10)
        pagi_frame.pack(fill=X, pady=(15, 0))

        ttk.Label(pagi_frame, text="Rows per page:", font="-size 9 -weight bold", background=self.CARD_BG, foreground="gray").pack(side=LEFT, padx=(5, 5))
        self.combo_page_size = ttk.Combobox(pagi_frame, values=["50", "100", "500", "1000", "1500", "2000"], state="readonly", width=7)
        self.combo_page_size.set("100")
        self.combo_page_size.pack(side=LEFT, padx=(0, 20))
        self.combo_page_size.bind("<<ComboboxSelected>>", self.on_page_size_change)

        self.btn_first = ttk.Button(pagi_frame, text="⏮ First", bootstyle="secondary-outline", width=8, command=self.first_page)
        self.btn_first.pack(side=LEFT, padx=3)
        
        self.btn_prev = ttk.Button(pagi_frame, text="◀ Prev", bootstyle="secondary-outline", width=8, command=self.prev_page)
        self.btn_prev.pack(side=LEFT, padx=3)

        self.lbl_page_info = ttk.Label(pagi_frame, text="Page 1 of 1 (0 records)", font="-size 10 -weight bold", background=self.CARD_BG)
        self.lbl_page_info.pack(side=LEFT, padx=20)

        self.btn_next = ttk.Button(pagi_frame, text="Next ▶", bootstyle="secondary-outline", width=8, command=self.next_page)
        self.btn_next.pack(side=LEFT, padx=3)

        self.btn_last = ttk.Button(pagi_frame, text="Last ⏭", bootstyle="secondary-outline", width=8, command=self.last_page)
        self.btn_last.pack(side=LEFT, padx=3)

        # RIGHT PANEL: PROFILE & PHOTO CARD
        right_frame = ttk.Frame(split_frame, width=400)
        right_frame.pack(side=RIGHT, fill=Y)
        right_frame.pack_propagate(False)

        profile_card = ttk.Frame(right_frame, style="Card.TFrame", padding=25)
        profile_card.pack(fill=BOTH, expand=True)

        ttk.Label(profile_card, text="ATTENDEE PROFILE", font="-size 12 -weight bold", background=self.CARD_BG, foreground="gray").pack(anchor=W, pady=(0, 15))

        self.lbl_photo = ttk.Label(profile_card, text="Select an attendee to\nview profile details.", justify=CENTER, background=self.CARD_BG, font="-size 10", foreground="gray")
        self.lbl_photo.pack(pady=(0, 15))
        
        self.lbl_profile_name = ttk.Label(profile_card, text="--", font="-size 18 -weight bold", background=self.CARD_BG, wraplength=340)
        self.lbl_profile_name.pack(anchor=W)
        self.lbl_profile_id = ttk.Label(profile_card, text="--", font="-size 11 -weight bold", background=self.CARD_BG, bootstyle=SECONDARY)
        self.lbl_profile_id.pack(anchor=W, pady=(0, 15))

        self.badge_frame = ttk.Frame(profile_card, style="Flat.TFrame")
        self.badge_frame.pack(fill=X, anchor=W, pady=(0, 15))
        
        self.lbl_badge_type = ttk.Label(self.badge_frame, text="TYPE", font="-size 9 -weight bold", padding=(10, 4), bootstyle="inverse-secondary")
        self.lbl_badge_type.pack(side=LEFT, padx=(0, 10))
        
        self.lbl_badge_sync = ttk.Label(self.badge_frame, text="SYNC", font="-size 9 -weight bold", padding=(10, 4), bootstyle="inverse-secondary")
        self.lbl_badge_sync.pack(side=LEFT)

        ttk.Separator(profile_card).pack(fill=X, pady=(0, 15))

        details_frame = ttk.Frame(profile_card, style="Flat.TFrame")
        details_frame.pack(fill=BOTH, expand=True)
        
        self.profile_vars = {
            "Mobile": ttk.StringVar(value="--"),
            "Email": ttk.StringVar(value="--"),
            "Gender": ttk.StringVar(value="--"),
            "Business": ttk.StringVar(value="--"),
            "Location": ttk.StringVar(value="--"),
            "Registered": ttk.StringVar(value="--"),
            "Check-ins": ttk.StringVar(value="--")
        }

        for i, (label_text, var) in enumerate(self.profile_vars.items()):
            row = ttk.Frame(details_frame, style="Flat.TFrame")
            row.pack(fill=X, pady=(4, 4))
            
            ttk.Label(row, text=f"{label_text.upper()}", width=12, font="-size 9 -weight bold", background=self.CARD_BG, foreground="gray").pack(side=LEFT, anchor=N)
            ttk.Label(row, textvariable=var, font="-size 10", background=self.CARD_BG, wraplength=230).pack(side=LEFT, fill=X, expand=True, anchor=N)
            
            if i < len(self.profile_vars) - 1:
                ttk.Separator(details_frame).pack(fill=X, pady=2)

    def _build_mini_stat(self, parent, title, bootstyle, is_purple=False):
        frame = ttk.Frame(parent, style="Card.TFrame", padding=(20, 12))
        frame.pack(side=LEFT, fill=X, expand=True, padx=(0, 15))
        
        ttk.Label(frame, text=title, font="-size 9 -weight bold", foreground="gray", background=self.CARD_BG).pack(anchor=W)
        
        val_lbl = ttk.Label(frame, text="0", font="-size 26 -weight bold", background=self.CARD_BG)
        val_lbl.pack(anchor=W, pady=(2, 0))
        
        if is_purple:
            val_lbl.configure(foreground="#9b59b6") 
        else:
            val_lbl.configure(bootstyle=bootstyle)
            
        return val_lbl

    # ==========================================================================
    # THREAD-SAFE DATA LOADING & BATCH FETCHING
    # ==========================================================================
    def _process_gui_queue(self):
        for _ in range(50):
            try:
                task = self.gui_queue.get_nowait()
                task()
            except queue.Empty:
                break
        self.after(50, self._process_gui_queue)
        
    def _auto_refresh_loop(self):
        if self.auto_refresh_var.get():
            self.load_data_async(is_manual=False)
        self.after(15000, self._auto_refresh_loop)

    def load_data_async(self, is_manual=False):
        mode = self.combo_source.get()
        
        if is_manual:
            self.btn_refresh.configure(state=DISABLED, text="Loading...")
            
        if "Failed" not in self.lbl_record_count.cget("text"):
            self.lbl_record_count.configure(text="Fetching records in batches...", bootstyle=INFO)
        
        def _fetch():
            try:
                combined = []
                if "API" in mode:
                    hub_url = "http://127.0.0.1:5000"
                    if os.path.exists(EXPLORER_CONFIG):
                        with open(EXPLORER_CONFIG, 'r') as f:
                            hub_url = json.load(f).get("hub_url", hub_url)
                    combined = self._fetch_api_in_batches(hub_url)
                    self.gui_queue.put(lambda: self.lbl_conn_status.configure(text="● API: Connected", bootstyle=SUCCESS))
                else:
                    if not self.SessionMySQL:
                        self.connect_db()
                    if not self.SessionMySQL:
                        self.gui_queue.put(lambda: self.lbl_conn_status.configure(text="● DB: Offline", bootstyle=DANGER))
                        raise Exception("MySQL database connection is unavailable. Check DB Config.")
                        
                    combined = self._fetch_mysql_in_batches(batch_size=10000)
                    self.gui_queue.put(lambda: self.lbl_conn_status.configure(text="● DB: Connected", bootstyle=SUCCESS))
                
                self.gui_queue.put(lambda c=combined: self._apply_data(c))
                
            except Exception as e:
                logging.error(f"Failed to load data: {e}")
                self.gui_queue.put(lambda: self.lbl_record_count.configure(text="Fetch Failed (Offline)", bootstyle=DANGER))
                if is_manual:
                    self.gui_queue.put(lambda err=str(e): messagebox.showerror("Connection Error", err))
            finally:
                if is_manual:
                    self.gui_queue.put(lambda: self.btn_refresh.configure(state=NORMAL, text="⟳ Refresh Data"))
                
        threading.Thread(target=_fetch, daemon=True).start()

    def _fetch_mysql_in_batches(self, batch_size=10000):
        all_records = []
        if not self.SessionMySQL and not self.connect_db():
            raise Exception("Cannot establish MySQL connection.")

        session = self.SessionMySQL()
        try:
            offset = 0
            while True:
                batch = session.query(Attendee).offset(offset).limit(batch_size).all()
                if not batch: break
                all_records.extend(batch)
                offset += len(batch)
                self.gui_queue.put(lambda c=len(all_records): self.lbl_record_count.configure(text=f"Loaded {c:,} records...", bootstyle=INFO))

            offset = 0
            while True:
                batch = session.query(OfflineKioskAttendee).offset(offset).limit(batch_size).all()
                if not batch: break
                all_records.extend(batch)
                offset += len(batch)
                self.gui_queue.put(lambda c=len(all_records): self.lbl_record_count.configure(text=f"Loaded {c:,} records...", bootstyle=INFO))

            return all_records
        except Exception as e:
            logging.warning(f"MySQL error, re-connecting... ({e})")
            session.close()
            self.connect_db()
            raise e
        finally:
            try: session.close()
            except: pass

    def _fetch_api_in_batches(self, hub_url, batch_size=5000):
        all_records = []
        offset = 0
        while True:
            api_endpoint = f"{hub_url}/api/attendees?limit={batch_size}&offset={offset}"
            try:
                resp = self.api_session.get(api_endpoint, timeout=10, verify=False)
                resp.raise_for_status()
                data = resp.json()
            except Exception:
                if offset == 0:
                    resp = self.api_session.get(f"{hub_url}/api/attendees", timeout=10, verify=False)
                    resp.raise_for_status()
                    data = resp.json()
                else:
                    break

            batch_data = data["items"] if isinstance(data, dict) and "items" in data else (data if isinstance(data, list) else [])
            if not batch_data: break

            records = [APIRecord(d) for d in batch_data]
            all_records.extend(records)
            self.gui_queue.put(lambda c=len(all_records): self.lbl_record_count.configure(text=f"Loaded {c:,} API records...", bootstyle=INFO))

            if len(batch_data) < batch_size or len(records) == len(data): break
            offset += len(batch_data)
        return all_records

    def _apply_data(self, records):
        sel = self.tree.selection()
        selected_id = sel[0] if sel else None

        self.all_attendees = records
        counts = {"GENERAL": 0, "BUSINESS": 0, "MEDIA": 0, "EXHIBITOR": 0}
        
        for att in records:
            atype = att.attendee_type.name if hasattr(att.attendee_type, 'name') else str(att.attendee_type)
            counts[atype.upper()] = counts.get(atype.upper(), 0) + 1
                
        self.lbl_stat_gen.configure(text=f"{counts.get('GENERAL', 0):,}")
        self.lbl_stat_biz.configure(text=f"{counts.get('BUSINESS', 0):,}")
        self.lbl_stat_med.configure(text=f"{counts.get('MEDIA', 0):,}")
        self.lbl_stat_exh.configure(text=f"{counts.get('EXHIBITOR', 0):,}")
        
        self.apply_filters(preserve_selection=selected_id)

    # ==========================================================================
    # FILTERING, SORTING & UI PAGINATION ENGINE
    # ==========================================================================
    def clear_filters(self):
        self.ent_search.delete(0, END)
        self.combo_type.current(0)
        self.combo_sort.current(0)
        self.apply_filters()

    def apply_filters(self, preserve_selection=None):
        search_query = self.ent_search.get().strip().lower()
        type_filter = self.combo_type.get()
        sort_filter = self.combo_sort.get()

        if not preserve_selection:
            sel = self.tree.selection()
            preserve_selection = sel[0] if sel else None

        filtered = []
        for att in self.all_attendees:
            att_type = att.attendee_type.name if hasattr(att.attendee_type, 'name') else str(att.attendee_type)
            if type_filter != "All Types" and att_type.upper() != type_filter:
                continue
            
            searchable_text = f"{att.full_name} {att.attendee_id} {att.mobile} {att.email or ''} {att.business_name or ''}".lower()
            if search_query and search_query not in searchable_text:
                continue
                
            filtered.append(att)

        if sort_filter == "Latest First":
            filtered.sort(key=lambda x: getattr(x, 'created_at', datetime.min) or datetime.min, reverse=True)
        elif sort_filter == "Oldest First":
            filtered.sort(key=lambda x: getattr(x, 'created_at', datetime.min) or datetime.min, reverse=False)
        elif sort_filter == "Name (A-Z)":
            filtered.sort(key=lambda x: getattr(x, 'full_name', '').lower(), reverse=False)
        elif sort_filter == "Name (Z-A)":
            filtered.sort(key=lambda x: getattr(x, 'full_name', '').lower(), reverse=True)

        self.filtered_attendees = filtered
        self.current_page = 1
        self.render_page(preserve_selection=preserve_selection)

    # ==========================================================================
    # PAGINATION CONTROL HANDLERS
    # ==========================================================================
    def on_page_size_change(self, event=None):
        try:
            self.page_size = int(self.combo_page_size.get())
        except ValueError:
            self.page_size = 100
        self.current_page = 1
        self.render_page()

    def first_page(self):
        if self.current_page > 1:
            self.current_page = 1
            self.render_page()

    def prev_page(self):
        if self.current_page > 1:
            self.current_page -= 1
            self.render_page()

    def next_page(self):
        if self.current_page < self.total_pages:
            self.current_page += 1
            self.render_page()

    def last_page(self):
        if self.current_page < self.total_pages:
            self.current_page = self.total_pages
            self.render_page()

    def render_page(self, preserve_selection=None):
        for item in self.tree.get_children():
            self.tree.delete(item)

        total_items = len(self.filtered_attendees)
        if total_items == 0:
            self.lbl_page_info.configure(text="Page 0 of 0 (0 records)")
            self.lbl_record_count.configure(text="Showing 0 records", bootstyle=INFO)
            self.btn_first.configure(state=DISABLED)
            self.btn_prev.configure(state=DISABLED)
            self.btn_next.configure(state=DISABLED)
            self.btn_last.configure(state=DISABLED)
            return

        self.total_pages = max(1, (total_items + self.page_size - 1) // self.page_size)
        
        if self.current_page > self.total_pages: self.current_page = self.total_pages
        if self.current_page < 1: self.current_page = 1

        start_idx = (self.current_page - 1) * self.page_size
        end_idx = min(start_idx + self.page_size, total_items)
        page_items = self.filtered_attendees[start_idx:end_idx]

        for i, att in enumerate(page_items):
            sync_status = "Pending ⏳" if getattr(att, 'needs_cloud_sync', False) else "Synced ✓"
            att_type = att.attendee_type.name if hasattr(att.attendee_type, 'name') else str(att.attendee_type)
            
            # Apply Zebra Striping tags (odd/even rows)
            row_tag = 'evenrow' if i % 2 == 0 else 'oddrow'
            
            self.tree.insert('', END, iid=att.attendee_id, values=(
                att.attendee_id, att.full_name, att.mobile, att_type,
                f"{att.city}, {att.state}", sync_status
            ), tags=(row_tag,))

        self.lbl_page_info.configure(text=f"Page {self.current_page} of {self.total_pages:,} (Total: {total_items:,})")
        self.lbl_record_count.configure(text=f"Showing {start_idx+1:,}-{end_idx:,} of {total_items:,} records", bootstyle=INFO)

        self.btn_first.configure(state=NORMAL if self.current_page > 1 else DISABLED)
        self.btn_prev.configure(state=NORMAL if self.current_page > 1 else DISABLED)
        self.btn_next.configure(state=NORMAL if self.current_page < self.total_pages else DISABLED)
        self.btn_last.configure(state=NORMAL if self.current_page < self.total_pages else DISABLED)

        if preserve_selection and self.tree.exists(preserve_selection):
            self.tree.selection_set(preserve_selection)
            self.on_row_select(None)

    def sort_treeview(self, col):
        if self.current_sort_col == col:
            self.sort_reverse = not self.sort_reverse
        else:
            self.sort_reverse = False
            self.current_sort_col = col

        data_list = [(self.tree.set(k, col), k) for k in self.tree.get_children('')]
        try:
            data_list.sort(key=lambda t: float(t[0]), reverse=self.sort_reverse)
        except ValueError:
            data_list.sort(reverse=self.sort_reverse)

        for index, (val, k) in enumerate(data_list):
            self.tree.move(k, '', index)
            
        # Re-apply zebra striping after sort
        for i, item in enumerate(self.tree.get_children('')):
            self.tree.item(item, tags=('evenrow' if i % 2 == 0 else 'oddrow',))

    # ==========================================================================
    # PROFILE RENDERING 
    # ==========================================================================
    def on_row_select(self, event):
        selected_items = self.tree.selection()
        if not selected_items: return
        
        selected_id = selected_items[0]
        attendee = next((a for a in self.all_attendees if a.attendee_id == selected_id), None)
        if not attendee: return
        
        self.lbl_profile_name.configure(text=attendee.full_name.upper())
        self.lbl_profile_id.configure(text=attendee.attendee_id)
        
        att_type = attendee.attendee_type.name if hasattr(attendee.attendee_type, 'name') else str(attendee.attendee_type)
        att_type_upper = att_type.upper()
        
        if att_type_upper == "GENERAL":
            self.lbl_badge_type.configure(style="TLabel", bootstyle="inverse-primary")  
        elif att_type_upper == "BUSINESS":
            self.lbl_badge_type.configure(style="TLabel", bootstyle="inverse-warning")  
        elif att_type_upper == "MEDIA":
            self.lbl_badge_type.configure(style="TLabel", bootstyle="inverse-danger")   
        elif att_type_upper == "EXHIBITOR":
            self.lbl_badge_type.configure(bootstyle="", style="PurpleBadge.TLabel")     
        else:
            self.lbl_badge_type.configure(style="TLabel", bootstyle="inverse-secondary")
            
        self.lbl_badge_type.configure(text=att_type_upper)
        
        if getattr(attendee, 'needs_cloud_sync', False):
            self.lbl_badge_sync.configure(text="PENDING SYNC", bootstyle="inverse-warning")
        else:
            self.lbl_badge_sync.configure(text="CLOUD SYNCED", bootstyle="inverse-success")

        self.profile_vars["Mobile"].set(attendee.mobile)
        self.profile_vars["Email"].set(attendee.email or "N/A")
        
        gender_val = attendee.gender.name if hasattr(attendee.gender, 'name') else str(attendee.gender)
        self.profile_vars["Gender"].set(gender_val)
        
        biz_name = attendee.business_name or "N/A"
        biz_cat = f" ({attendee.business_category})" if attendee.business_category else ""
        self.profile_vars["Business"].set(f"{biz_name}{biz_cat}")
        
        self.profile_vars["Location"].set(f"{attendee.city}, {attendee.state}\nPIN: {attendee.pincode}")
        
        created_at = getattr(attendee, 'created_at', None)
        if created_at and created_at != datetime.min:
            self.profile_vars["Registered"].set(created_at.strftime('%d %b %Y, %H:%M'))
        else:
            self.profile_vars["Registered"].set("Unknown")
        
        history = attendee.checkin_history
        if isinstance(history, str):
            try: history = json.loads(history)
            except: history = {}
            
        if history:
            checkin_text = "\n".join([f"✓ {day}: {entry.get('timestamp', 'Unknown')[:16].replace('T', ' ')}" for day, entry in history.items()])
        else:
            checkin_text = "No check-ins yet."
            
        self.profile_vars["Check-ins"].set(checkin_text)

        photo_path = os.path.join(PHOTOS_DIR, f"{attendee.attendee_id}.jpg")
        if os.path.exists(photo_path):
            self.render_image(photo_path)
        else:
            self.lbl_photo.configure(image='', text="📸\nNo Photo Found")
            self.lbl_photo.image = None

    def render_image(self, path):
        try:
            img = Image.open(path)
            img = ImageOps.fit(img, (180, 180), Image.Resampling.LANCZOS)
            
            bg = Image.new('RGBA', (190, 190), self._hex_to_rgb(self.SOFT_BORDER) + (255,))
            bg.paste(img, (5, 5))
            
            tk_img = ImageTk.PhotoImage(bg)
            self.lbl_photo.configure(image=tk_img, text="")
            self.lbl_photo.image = tk_img 
        except Exception as e:
            self.lbl_photo.configure(image='', text=f"Error loading image")
            logging.error(f"Failed to load image for profile: {e}")

    # ==========================================================================
    # EXPORT CAPABILITY
    # ==========================================================================
    def export_csv(self):
        if not self.filtered_attendees:
            messagebox.showwarning("Export Empty", "There are no records to export currently.")
            return
            
        default_name = f"Attendee_Export_{datetime.now().strftime('%Y%m%d_%H%M')}.csv"
        file_path = filedialog.asksaveasfilename(
            defaultextension=".csv", 
            initialfile=default_name,
            title="Export to CSV",
            filetypes=[("CSV files", "*.csv"), ("All files", "*.*")]
        )
        
        if not file_path: return
            
        try:
            with open(file_path, 'w', newline='', encoding='utf-8') as csvfile:
                writer = csv.writer(csvfile)
                writer.writerow(["Attendee ID", "Full Name", "Mobile", "Email", "Gender", "Type", "Company", "Category", "City", "State", "Pincode", "Registered", "Cloud Synced"])
                
                for att in self.filtered_attendees:
                    att_type = att.attendee_type.name if hasattr(att.attendee_type, 'name') else str(att.attendee_type)
                    gender = att.gender.name if hasattr(att.gender, 'name') else str(att.gender)
                    sync_status = "No" if getattr(att, 'needs_cloud_sync', False) else "Yes"
                    
                    created_at = getattr(att, 'created_at', None)
                    reg_date = created_at.strftime('%Y-%m-%d %H:%M:%S') if created_at and created_at != datetime.min else "Unknown"
                    
                    writer.writerow([
                        att.attendee_id, att.full_name, att.mobile, att.email, gender,
                        att_type, att.business_name, att.business_category, 
                        att.city, att.state, att.pincode, reg_date, sync_status
                    ])
                    
            messagebox.showinfo("Export Successful", f"Successfully exported {len(self.filtered_attendees):,} records to:\n{file_path}")
        except Exception as e:
            logging.error(f"CSV Export failed: {e}")
            messagebox.showerror("Export Failed", f"Could not save file:\n{e}")

if __name__ == "__main__":
    app = AttendeeExplorer()
    app.mainloop()