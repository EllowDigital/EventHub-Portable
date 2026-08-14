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
import ctypes
from datetime import datetime

from sqlalchemy import create_engine, text
from sqlalchemy.engine import make_url
from sqlalchemy.orm import sessionmaker
from sqlalchemy.exc import SQLAlchemyError

from PySide6.QtWidgets import (QApplication, QMainWindow, QWidget, QVBoxLayout, QHBoxLayout, 
                               QGridLayout, QLabel, QPushButton, QFrame, QGroupBox, QLineEdit, 
                               QCheckBox, QComboBox, QTableWidget, QTableWidgetItem, 
                               QHeaderView, QDialog, QMessageBox, QFileDialog, QInputDialog, 
                               QAbstractItemView, QSplitter)
from PySide6.QtCore import Qt, QTimer, QSize
from PySide6.QtGui import QPixmap, QPainter, QColor, QFont, QIcon, QBrush, QImage

urllib3.disable_warnings(urllib3.exceptions.InsecureRequestWarning)

try:
    from app.schema import Attendee, OfflineKioskAttendee, get_database_sessions
except ModuleNotFoundError:
    from schema import Attendee, OfflineKioskAttendee, get_database_sessions

def global_exception_handler(*args):
    logging.error("Uncaught GUI Exception intercepted. App remains running.", exc_info=args)

sys.excepthook = global_exception_handler

BASE_DIR = os.path.dirname(os.path.abspath(__file__))
PHOTOS_DIR = os.path.join(BASE_DIR, 'attendee_photos')
CONFIG_DIR = os.path.join(BASE_DIR, 'config')
EXPLORER_CONFIG = os.path.join(CONFIG_DIR, 'explorer.json')
os.makedirs(PHOTOS_DIR, exist_ok=True)
os.makedirs(CONFIG_DIR, exist_ok=True)

# Theme Colors Map
COLORS = {
    "PRIMARY": "#375a7f",
    "INFO": "#0dcaf0",
    "SUCCESS": "#00bc8c",
    "WARNING": "#f39c12",
    "DANGER": "#e74c3c",
    "SECONDARY": "#888888",
    "PURPLE": "#9b59b6",
    "BG_DARK": "#141414",
    "CARD_BG": "#242424",
    "BORDER": "#333333",
    "TEXT": "#e0e0e0"
}

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
# UI COMPONENTS
# ==============================================================================
class DatabaseConfigDialog(QDialog):
    def __init__(self, parent, current_uri, on_save_callback):
        super().__init__(parent)
        self.setWindowTitle("MySQL Database Configuration")
        self.setFixedSize(450, 420)
        self.on_save_callback = on_save_callback
        
        host, port, user, pwd, db = "localhost", "3306", "root", "", "tde_database"
        if current_uri and "mysql+pymysql" in current_uri:
            try:
                url_obj = make_url(current_uri)
                host = url_obj.host or "localhost"
                port = str(url_obj.port) if url_obj.port else "3306"
                user = url_obj.username or ""
                pwd = url_obj.password or ""
                db = url_obj.database or ""
            except Exception: pass

        layout = QVBoxLayout(self)
        layout.setContentsMargins(25, 25, 25, 25)

        lbl_title = QLabel("MySQL Connection Details")
        lbl_title.setFont(QFont("Segoe UI", 14, QFont.Bold))
        lbl_title.setStyleSheet(f"color: {COLORS['PRIMARY']};")
        layout.addWidget(lbl_title)
        
        lbl_sub = QLabel("Enter your database credentials below.")
        lbl_sub.setStyleSheet("color: gray;")
        layout.addWidget(lbl_sub)
        layout.addSpacing(15)

        self.ent_host = self._build_field(layout, "Host Address:", host, "e.g., localhost or 192.168.1.5")
        self.ent_port = self._build_field(layout, "Port:", port, "e.g., 3306")
        self.ent_user = self._build_field(layout, "Username:", user, "e.g., root")
        self.ent_pwd = self._build_field(layout, "Password:", pwd, "Leave blank if no password", is_password=True)
        self.ent_db = self._build_field(layout, "Database Name:", db, "e.g., tde_database")

        layout.addStretch()

        btn_layout = QHBoxLayout()
        btn_test = QPushButton("Test Connection")
        btn_test.setStyleSheet(f"border: 1px solid {COLORS['INFO']}; color: {COLORS['INFO']}; background: transparent; padding: 6px;")
        btn_test.clicked.connect(self.test_connection)
        
        btn_cancel = QPushButton("Cancel")
        btn_cancel.clicked.connect(self.reject)
        
        btn_save = QPushButton("Save Settings")
        btn_save.setStyleSheet(f"background-color: {COLORS['SUCCESS']}; color: white; padding: 6px; font-weight: bold;")
        btn_save.clicked.connect(self.save_settings)
        
        btn_layout.addWidget(btn_test)
        btn_layout.addStretch()
        btn_layout.addWidget(btn_cancel)
        btn_layout.addWidget(btn_save)
        layout.addLayout(btn_layout)

    def _build_field(self, parent_layout, label_text, default, placeholder="", is_password=False):
        row = QHBoxLayout()
        lbl = QLabel(label_text)
        lbl.setFixedWidth(110)
        lbl.setFont(QFont("Segoe UI", 9, QFont.Bold))
        ent = QLineEdit(default)
        ent.setPlaceholderText(placeholder)
        if is_password: ent.setEchoMode(QLineEdit.Password)
        row.addWidget(lbl)
        row.addWidget(ent)
        parent_layout.addLayout(row)
        return ent

    def build_uri(self):
        h, po, u, pw, d = self.ent_host.text().strip(), self.ent_port.text().strip(), self.ent_user.text().strip(), self.ent_pwd.text().strip(), self.ent_db.text().strip()
        auth = f"{u}:{pw}" if pw else u
        port_str = f":{po}" if po else ":3306"
        return f"mysql+pymysql://{auth}@{h}{port_str}/{d}"

    def test_connection(self):
        uri = self.build_uri()
        try:
            engine = create_engine(uri, connect_args={"connect_timeout": 3})
            with engine.connect() as conn: conn.execute(text("SELECT 1"))
            QMessageBox.information(self, "Success", "Connection successful!\nThe database is reachable.")
        except SQLAlchemyError as e:
            QMessageBox.critical(self, "Connection Failed", f"Could not connect to the database.\n\nError:\n{str(e).split(']')[0]}]")

    def save_settings(self):
        uri = self.build_uri()
        self.on_save_callback(uri)
        self.accept()

# ==============================================================================
# MAIN APPLICATION
# ==============================================================================
class AttendeeExplorer(QMainWindow):
    def __init__(self):
        super().__init__()
        self.setWindowTitle("TDE UP 2026 — Attendee Explorer")
        self.resize(1400, 900) # Increased default size for better 2K/4K scaling
        self.setMinimumSize(1150, 750)
        
        icon_path = os.path.join(BASE_DIR, "assets", "EventHub.ico")
        if os.path.exists(icon_path):
            try: self.setWindowIcon(QIcon(icon_path))
            except: pass

        self.gui_queue = queue.Queue()
        self.SessionMySQL = None
        self.all_attendees = []
        self.filtered_attendees = []
        self.current_sort_col = 0
        self.sort_reverse = False
        self.current_page = 1
        self.page_size = 100
        self.total_pages = 1

        self.api_session = requests.Session()
        retries = Retry(total=5, backoff_factor=0.5, status_forcelist=[500, 502, 503, 504], raise_on_status=False)
        self.api_session.mount('http://', HTTPAdapter(max_retries=retries))
        self.api_session.mount('https://', HTTPAdapter(max_retries=retries))
        
        self._apply_stylesheet()
        self.build_ui()
        self.connect_db()
        
        self.queue_timer = QTimer(self)
        self.queue_timer.timeout.connect(self._process_gui_queue)
        self.queue_timer.start(50)
        
        self.refresh_timer = QTimer(self)
        self.refresh_timer.timeout.connect(self._auto_refresh_loop)
        self.refresh_timer.start(15000)
        
        self.load_data_async(is_manual=True)

    def _apply_stylesheet(self):
        self.setStyleSheet(f"""
            QMainWindow, QWidget {{ background-color: {COLORS['BG_DARK']}; color: {COLORS['TEXT']}; font-family: 'Segoe UI', Arial; font-size: 10pt; }}
            QFrame#Card {{ background-color: {COLORS['CARD_BG']}; border: 1px solid {COLORS['BORDER']}; border-radius: 6px; }}
            QFrame#Flat {{ background-color: {COLORS['CARD_BG']}; border: none; }}
            QLabel {{ background: transparent; border: none; }}
            QLineEdit, QComboBox {{ background-color: #1a1a1a; border: 1px solid #444; color: white; border-radius: 4px; padding: 6px; }}
            QLineEdit:focus, QComboBox:focus {{ border: 1px solid {COLORS['PRIMARY']}; }}
            QPushButton {{ background-color: #333; color: white; border: 1px solid #555; padding: 6px 12px; border-radius: 4px; font-weight: bold; }}
            QPushButton:hover {{ background-color: #444; }}
            QPushButton:disabled {{ background-color: #222; color: #666; border: 1px solid #333; }}
            QTableWidget {{ 
                background-color: {COLORS['CARD_BG']}; 
                border: 1px solid {COLORS['BORDER']}; 
                border-radius: 4px; 
                alternate-background-color: #1e1e1e; 
                gridline-color: #333333; 
            }}
            QTableWidget::item {{ border-bottom: 1px solid #2a2a2a; padding: 4px; }}
            QTableWidget::item:selected {{ background-color: {COLORS['PRIMARY']}; color: white; }}
            QHeaderView::section {{ 
                background-color: #1a1a1a; 
                color: #aaaaaa; 
                font-weight: bold; 
                padding: 8px; 
                border: none; 
                border-bottom: 2px solid #333333; 
                border-right: 1px solid #333333; 
            }}
            QScrollBar:vertical {{ background: #1a1a1a; width: 14px; border-radius: 7px; }}
            QScrollBar::handle:vertical {{ background: #444; min-height: 20px; border-radius: 7px; margin: 2px; }}
            QScrollBar::add-line:vertical, QScrollBar::sub-line:vertical {{ height: 0px; }}
            QSplitter::handle {{ background-color: {COLORS['BORDER']}; width: 3px; }}
        """)

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
                with open(EXPLORER_CONFIG, 'r') as f: config_data = json.load(f)
            config_data["mysql_uri"] = uri
            with open(EXPLORER_CONFIG, 'w') as f: json.dump(config_data, f, indent=4)
            QMessageBox.information(self, "Saved", "Database configuration saved successfully!\nReconnecting...")
            self.combo_source.setCurrentIndex(0)
            self.load_data_async(is_manual=True)
        except Exception as e:
            QMessageBox.critical(self, "Error", f"Failed to save DB Config: {e}")

    def configure_db_url(self):
        current_uri = self.get_current_mysql_uri()
        DatabaseConfigDialog(self, current_uri, self.save_mysql_uri).exec()

    def configure_api_url(self):
        current_url = "http://127.0.0.1:5000"
        if os.path.exists(EXPLORER_CONFIG):
            try:
                with open(EXPLORER_CONFIG, 'r') as f: current_url = json.load(f).get("hub_url", current_url)
            except Exception: pass
            
        new_url, ok = QInputDialog.getText(self, "API Configuration", "Enter the Hub API Server URL:\n(e.g., http://192.168.1.100:5000)", QLineEdit.Normal, current_url)
        
        if ok and new_url:
            new_url = new_url.strip()
            if not new_url.startswith("http"): new_url = "http://" + new_url
            try:
                config_data = {}
                if os.path.exists(EXPLORER_CONFIG):
                    with open(EXPLORER_CONFIG, 'r') as f: config_data = json.load(f)
                config_data["hub_url"] = new_url
                with open(EXPLORER_CONFIG, 'w') as f: json.dump(config_data, f, indent=4)
                self.combo_source.setCurrentIndex(1)
                self.load_data_async(is_manual=True)
            except Exception as e:
                QMessageBox.critical(self, "Error", f"Failed to save URL: {e}")

    def build_ui(self):
        central_widget = QWidget()
        self.setCentralWidget(central_widget)
        main_layout = QVBoxLayout(central_widget)
        main_layout.setContentsMargins(25, 25, 25, 25)
        
        # HEADER
        header_row = QHBoxLayout()
        title_box = QVBoxLayout()
        t1 = QLabel("Attendee Explorer")
        t1.setFont(QFont("Segoe UI", 26, QFont.Bold))
        t1.setStyleSheet(f"color: {COLORS['PRIMARY']}; border: none;")
        t2 = QLabel("SEARCH, INSPECT & EXPORT PROFILES")
        t2.setFont(QFont("Segoe UI", 10, QFont.Bold))
        t2.setStyleSheet(f"color: {COLORS['SECONDARY']}; border: none;")
        title_box.addWidget(t1)
        title_box.addWidget(t2)
        header_row.addLayout(title_box)
        header_row.addStretch()
        
        action_box = QHBoxLayout()
        action_box.setAlignment(Qt.AlignBottom)
        self.lbl_record_count = QLabel("Loading records...")
        self.lbl_record_count.setFont(QFont("Segoe UI", 11, QFont.Bold))
        self.lbl_record_count.setStyleSheet(f"color: {COLORS['INFO']};")
        
        self.lbl_conn_status = QLabel("● Syncing...")
        self.lbl_conn_status.setFont(QFont("Segoe UI", 10, QFont.Bold))
        self.lbl_conn_status.setStyleSheet(f"color: {COLORS['SECONDARY']};")
        
        self.combo_source = QComboBox()
        self.combo_source.addItems(["Source: MySQL (Direct DB)", "Source: Hub API (Portable)"])
        self.combo_source.setFixedWidth(220)
        self.combo_source.currentIndexChanged.connect(lambda: self.load_data_async(is_manual=False))
        
        btn_db_cfg = QPushButton("⚙️ DB Config")
        btn_db_cfg.setStyleSheet(f"border: 1px solid {COLORS['WARNING']}; color: {COLORS['WARNING']}; background: transparent;")
        btn_db_cfg.clicked.connect(self.configure_db_url)
        
        btn_api_cfg = QPushButton("⚙️ API Config")
        btn_api_cfg.setStyleSheet(f"border: 1px solid {COLORS['SECONDARY']}; color: {COLORS['SECONDARY']}; background: transparent;")
        btn_api_cfg.clicked.connect(self.configure_api_url)
        
        self.chk_auto = QCheckBox("Auto-Refresh")
        self.chk_auto.setChecked(True)
        
        btn_export = QPushButton("📥 Export CSV")
        btn_export.setStyleSheet(f"border: 1px solid {COLORS['SUCCESS']}; color: {COLORS['SUCCESS']}; background: transparent;")
        btn_export.clicked.connect(self.export_csv)
        
        self.btn_refresh = QPushButton("⟳ Refresh Data")
        self.btn_refresh.setStyleSheet(f"background-color: {COLORS['PRIMARY']}; color: white;")
        self.btn_refresh.clicked.connect(lambda: self.load_data_async(is_manual=True))
        
        for w in [self.lbl_record_count, self.lbl_conn_status, self.combo_source, btn_db_cfg, btn_api_cfg, self.chk_auto, btn_export, self.btn_refresh]:
            action_box.addWidget(w)
            if w not in [self.combo_source, btn_export]: action_box.addSpacing(10)
            
        header_row.addLayout(action_box)
        main_layout.addLayout(header_row)
        main_layout.addSpacing(15)

        # STATS ROW
        stats_frame = QHBoxLayout()
        self.lbl_stat_gen = self._build_mini_stat(stats_frame, "GENERAL PASS", COLORS["PRIMARY"])
        self.lbl_stat_biz = self._build_mini_stat(stats_frame, "BUSINESS PASS", COLORS["WARNING"])
        self.lbl_stat_med = self._build_mini_stat(stats_frame, "MEDIA PASS", COLORS["DANGER"])
        self.lbl_stat_exh = self._build_mini_stat(stats_frame, "EXHIBITOR PASS", COLORS["PURPLE"])
        main_layout.addLayout(stats_frame)
        main_layout.addSpacing(15)

        # RESPONSIVE SPLITTER (Solves UI Overlap/Squishing)
        self.splitter = QSplitter(Qt.Horizontal)
        
        # Left Panel (Table & Filters)
        left_widget = QWidget()
        left_layout = QVBoxLayout(left_widget)
        left_layout.setContentsMargins(0, 0, 10, 0) # Right margin for splitter gap
        
        # Filters
        filter_card = QFrame()
        filter_card.setObjectName("Card")
        f_lyt = QHBoxLayout(filter_card)
        f_lyt.setContentsMargins(12, 12, 12, 12)
        
        lbl_s = QLabel("🔍")
        lbl_s.setFont(QFont("Segoe UI", 12))
        self.ent_search = QLineEdit()
        self.ent_search.setPlaceholderText("Search names, IDs, email, mobile...")
        self.ent_search.textChanged.connect(lambda: self.apply_filters())
        
        lbl_t = QLabel("Type:")
        lbl_t.setStyleSheet("color: gray; font-weight: bold;")
        self.combo_type = QComboBox()
        self.combo_type.addItems(["All Types", "GENERAL", "BUSINESS", "MEDIA", "EXHIBITOR"])
        self.combo_type.currentIndexChanged.connect(lambda: self.apply_filters())
        
        lbl_sort = QLabel("Sort By:")
        lbl_sort.setStyleSheet("color: gray; font-weight: bold;")
        self.combo_sort = QComboBox()
        self.combo_sort.addItems(["Latest First", "Oldest First", "Name (A-Z)", "Name (Z-A)"])
        self.combo_sort.currentIndexChanged.connect(lambda: self.apply_filters())
        
        btn_clear = QPushButton("Clear Filters")
        btn_clear.setStyleSheet("background: transparent; color: #888; border: none; text-decoration: underline;")
        btn_clear.clicked.connect(self.clear_filters)
        
        for w in [lbl_s, self.ent_search, lbl_t, self.combo_type, lbl_sort, self.combo_sort]:
            f_lyt.addWidget(w)
            f_lyt.addSpacing(5)
        f_lyt.addStretch()
        f_lyt.addWidget(btn_clear)
        left_layout.addWidget(filter_card)
        
        # Table
        self.table = QTableWidget()
        self.table.setColumnCount(6)
        self.table.setHorizontalHeaderLabels(["ATTENDEE ID", "FULL NAME", "MOBILE", "TYPE", "CITY", "CLOUD SYNC"])
        
        # Responsive Table Columns
        header = self.table.horizontalHeader()
        header.setSectionResizeMode(0, QHeaderView.ResizeToContents) # ID
        header.setSectionResizeMode(1, QHeaderView.Stretch)          # Name (Stretches)
        header.setSectionResizeMode(2, QHeaderView.ResizeToContents) # Mobile
        header.setSectionResizeMode(3, QHeaderView.ResizeToContents) # Type
        header.setSectionResizeMode(4, QHeaderView.Stretch)          # City (Stretches)
        header.setSectionResizeMode(5, QHeaderView.ResizeToContents) # Sync Status
        
        self.table.setSelectionBehavior(QAbstractItemView.SelectRows)
        self.table.setEditTriggers(QAbstractItemView.NoEditTriggers)
        self.table.setAlternatingRowColors(True)
        self.table.setShowGrid(False)
        self.table.verticalHeader().setVisible(False)
        self.table.horizontalHeader().sectionClicked.connect(self.sort_table_column)
        self.table.itemSelectionChanged.connect(self.on_row_select)
        
        left_layout.addWidget(self.table, 1)

        # Pagination
        pagi_card = QFrame()
        pagi_card.setObjectName("Card")
        p_lyt = QHBoxLayout(pagi_card)
        p_lyt.setContentsMargins(10, 10, 10, 10)
        
        lbl_rpp = QLabel("Rows per page:")
        lbl_rpp.setStyleSheet("color: gray; font-weight: bold;")
        self.combo_page_size = QComboBox()
        self.combo_page_size.addItems(["50", "100", "500", "1000", "1500", "2000"])
        self.combo_page_size.setCurrentText("100")
        self.combo_page_size.currentIndexChanged.connect(self.on_page_size_change)
        
        self.btn_first = QPushButton("⏮ First")
        self.btn_prev = QPushButton("◀ Prev")
        self.lbl_page_info = QLabel("Page 1 of 1 (0 records)")
        self.lbl_page_info.setFont(QFont("Segoe UI", 10, QFont.Bold))
        self.btn_next = QPushButton("Next ▶")
        self.btn_last = QPushButton("Last ⏭")
        
        self.btn_first.clicked.connect(self.first_page)
        self.btn_prev.clicked.connect(self.prev_page)
        self.btn_next.clicked.connect(self.next_page)
        self.btn_last.clicked.connect(self.last_page)
        
        for btn in [self.btn_first, self.btn_prev, self.btn_next, self.btn_last]:
            btn.setStyleSheet("background: transparent; color: #e0e0e0; border: 1px solid #555;")
            
        p_lyt.addWidget(lbl_rpp); p_lyt.addWidget(self.combo_page_size); p_lyt.addSpacing(20)
        p_lyt.addWidget(self.btn_first); p_lyt.addWidget(self.btn_prev); p_lyt.addSpacing(15)
        p_lyt.addWidget(self.lbl_page_info); p_lyt.addSpacing(15)
        p_lyt.addWidget(self.btn_next); p_lyt.addWidget(self.btn_last); p_lyt.addStretch()
        
        left_layout.addWidget(pagi_card)
        
        self.splitter.addWidget(left_widget)

        # Right Panel (Profile View)
        right_card = QFrame()
        right_card.setObjectName("Card")
        # Removed setFixedWidth to allow splitter to handle responsiveness natively
        right_card.setMinimumWidth(350) 
        r_lyt = QVBoxLayout(right_card)
        r_lyt.setContentsMargins(25, 25, 25, 25)
        
        lbl_p_title = QLabel("ATTENDEE PROFILE")
        lbl_p_title.setStyleSheet("color: gray; font-weight: bold; font-size: 12px;")
        r_lyt.addWidget(lbl_p_title)
        
        self.lbl_photo = QLabel("Select an attendee to\nview profile details.")
        self.lbl_photo.setAlignment(Qt.AlignCenter)
        self.lbl_photo.setStyleSheet("color: gray; font-size: 11px;")
        self.lbl_photo.setFixedSize(190, 190)
        r_lyt.addWidget(self.lbl_photo, 0, Qt.AlignCenter)
        
        self.lbl_profile_name = QLabel("--")
        self.lbl_profile_name.setFont(QFont("Segoe UI", 18, QFont.Bold))
        self.lbl_profile_name.setWordWrap(True)
        r_lyt.addWidget(self.lbl_profile_name)
        
        self.lbl_profile_id = QLabel("--")
        self.lbl_profile_id.setFont(QFont("Segoe UI", 11, QFont.Bold))
        self.lbl_profile_id.setStyleSheet(f"color: {COLORS['SECONDARY']};")
        r_lyt.addWidget(self.lbl_profile_id)
        
        badge_row = QHBoxLayout()
        self.lbl_badge_type = QLabel("TYPE")
        self.lbl_badge_type.setStyleSheet("background-color: #555; color: white; padding: 4px 10px; border-radius: 4px; font-weight: bold; font-size: 10px;")
        self.lbl_badge_sync = QLabel("SYNC")
        self.lbl_badge_sync.setStyleSheet("background-color: #555; color: white; padding: 4px 10px; border-radius: 4px; font-weight: bold; font-size: 10px;")
        badge_row.addWidget(self.lbl_badge_type)
        badge_row.addWidget(self.lbl_badge_sync)
        badge_row.addStretch()
        r_lyt.addLayout(badge_row)
        r_lyt.addSpacing(15)
        
        line = QFrame()
        line.setFrameShape(QFrame.HLine)
        line.setStyleSheet(f"color: {COLORS['BORDER']};")
        r_lyt.addWidget(line)
        r_lyt.addSpacing(10)
        
        self.profile_labels = {}
        fields = ["Mobile", "Email", "Gender", "Business", "Location", "Registered", "Check-ins"]
        for f in fields:
            row = QHBoxLayout()
            row.setAlignment(Qt.AlignTop)
            lf = QLabel(f.upper())
            lf.setFixedWidth(90)
            lf.setStyleSheet("color: gray; font-weight: bold; font-size: 11px;")
            lv = QLabel("--")
            lv.setWordWrap(True)
            lv.setStyleSheet("font-size: 13px;")
            row.addWidget(lf)
            row.addWidget(lv)
            r_lyt.addLayout(row)
            self.profile_labels[f] = lv
            
            if f != fields[-1]:
                sep = QFrame()
                sep.setFrameShape(QFrame.HLine)
                sep.setStyleSheet(f"color: {COLORS['BORDER']};")
                r_lyt.addWidget(sep)

        r_lyt.addStretch()
        
        self.splitter.addWidget(right_card)
        self.splitter.setSizes([950, 400]) # Give 70% space to table, 30% to profile natively
        main_layout.addWidget(self.splitter, 1)

    def _build_mini_stat(self, parent_layout, title, color):
        card = QFrame()
        card.setObjectName("Card")
        lyt = QVBoxLayout(card)
        lyt.setContentsMargins(20, 12, 20, 12)
        
        t_lbl = QLabel(title)
        t_lbl.setStyleSheet("color: gray; font-weight: bold; font-size: 11px;")
        lyt.addWidget(t_lbl)
        
        v_lbl = QLabel("0")
        v_lbl.setFont(QFont("Segoe UI", 26, QFont.Bold))
        v_lbl.setStyleSheet(f"color: {color};")
        lyt.addWidget(v_lbl)
        
        parent_layout.addWidget(card)
        return v_lbl

    def _process_gui_queue(self):
        for _ in range(50):
            try:
                self.gui_queue.get_nowait()()
            except queue.Empty: break

    def _auto_refresh_loop(self):
        if self.chk_auto.isChecked():
            self.load_data_async(is_manual=False)

    def load_data_async(self, is_manual=False):
        mode = self.combo_source.currentText()
        
        # Source Switch / Loader UI Logic
        self.btn_refresh.setEnabled(False)
        self.btn_refresh.setText("Loading...")
        
        self.all_attendees = []
        self.filtered_attendees = []
        self.table.setRowCount(1)
        loader_item = QTableWidgetItem("Fetching data from source, please wait...")
        loader_item.setTextAlignment(Qt.AlignCenter)
        loader_item.setFont(QFont("Segoe UI", 12, QFont.Bold))
        loader_item.setForeground(QBrush(QColor(COLORS["INFO"])))
        self.table.setSpan(0, 0, 1, 6) # Span across all columns
        self.table.setItem(0, 0, loader_item)
        
        self.lbl_record_count.setText("Fetching records in batches...")
        self.lbl_record_count.setStyleSheet(f"color: {COLORS['INFO']}; font-weight: bold;")
            
        def _fetch():
            try:
                combined = []
                if "API" in mode:
                    hub_url = "http://127.0.0.1:5000"
                    if os.path.exists(EXPLORER_CONFIG):
                        with open(EXPLORER_CONFIG, 'r') as f:
                            hub_url = json.load(f).get("hub_url", hub_url)
                    combined = self._fetch_api_in_batches(hub_url)
                    self.gui_queue.put(lambda: self.lbl_conn_status.setText("● API: Connected"))
                    self.gui_queue.put(lambda: self.lbl_conn_status.setStyleSheet(f"color: {COLORS['SUCCESS']}; font-weight: bold;"))
                else:
                    if not self.SessionMySQL: self.connect_db()
                    if not self.SessionMySQL:
                        self.gui_queue.put(lambda: self.lbl_conn_status.setText("● DB: Offline"))
                        self.gui_queue.put(lambda: self.lbl_conn_status.setStyleSheet(f"color: {COLORS['DANGER']}; font-weight: bold;"))
                        raise Exception("MySQL database connection is unavailable. Check DB Config.")
                    combined = self._fetch_mysql_in_batches(batch_size=10000)
                    self.gui_queue.put(lambda: self.lbl_conn_status.setText("● DB: Connected"))
                    self.gui_queue.put(lambda: self.lbl_conn_status.setStyleSheet(f"color: {COLORS['SUCCESS']}; font-weight: bold;"))
                self.gui_queue.put(lambda c=combined: self._apply_data(c))
            except Exception as e:
                logging.error(f"Failed to load data: {e}")
                self.gui_queue.put(lambda: self.lbl_record_count.setText("Fetch Failed (Offline)"))
                self.gui_queue.put(lambda: self.lbl_record_count.setStyleSheet(f"color: {COLORS['DANGER']}; font-weight: bold;"))
                if is_manual:
                    self.gui_queue.put(lambda err=str(e): QMessageBox.critical(self, "Connection Error", err))
            finally:
                self.gui_queue.put(lambda: self.btn_refresh.setEnabled(True))
                self.gui_queue.put(lambda: self.btn_refresh.setText("⟳ Refresh Data"))
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
                self.gui_queue.put(lambda c=len(all_records): self.lbl_record_count.setText(f"Loaded {c:,} records..."))
            offset = 0
            while True:
                batch = session.query(OfflineKioskAttendee).offset(offset).limit(batch_size).all()
                if not batch: break
                all_records.extend(batch)
                offset += len(batch)
                self.gui_queue.put(lambda c=len(all_records): self.lbl_record_count.setText(f"Loaded {c:,} records..."))
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
                else: break
            batch_data = data["items"] if isinstance(data, dict) and "items" in data else (data if isinstance(data, list) else [])
            if not batch_data: break
            records = [APIRecord(d) for d in batch_data]
            all_records.extend(records)
            self.gui_queue.put(lambda c=len(all_records): self.lbl_record_count.setText(f"Loaded {c:,} API records..."))
            if len(batch_data) < batch_size or len(records) == len(data): break
            offset += len(batch_data)
        return all_records

    def _apply_data(self, records):
        # Remove loader span
        self.table.clearSpans()
        
        sel_items = self.table.selectedItems()
        selected_id = sel_items[0].data(Qt.UserRole) if sel_items else None
        
        self.all_attendees = records
        counts = {"GENERAL": 0, "BUSINESS": 0, "MEDIA": 0, "EXHIBITOR": 0}
        for att in records:
            atype = att.attendee_type.name if hasattr(att.attendee_type, 'name') else str(att.attendee_type)
            counts[atype.upper()] = counts.get(atype.upper(), 0) + 1
            
        self.lbl_stat_gen.setText(f"{counts.get('GENERAL', 0):,}")
        self.lbl_stat_biz.setText(f"{counts.get('BUSINESS', 0):,}")
        self.lbl_stat_med.setText(f"{counts.get('MEDIA', 0):,}")
        self.lbl_stat_exh.setText(f"{counts.get('EXHIBITOR', 0):,}")
        
        self.apply_filters(preserve_selection=selected_id)

    def clear_filters(self):
        self.ent_search.clear()
        self.combo_type.setCurrentIndex(0)
        self.combo_sort.setCurrentIndex(0)
        self.apply_filters()

    def apply_filters(self, preserve_selection=None):
        search_query = self.ent_search.text().strip().lower()
        type_filter = self.combo_type.currentText()
        sort_filter = self.combo_sort.currentText()
        
        if not preserve_selection:
            sel = self.table.selectedItems()
            preserve_selection = sel[0].data(Qt.UserRole) if sel else None
            
        filtered = []
        for att in self.all_attendees:
            att_type = att.attendee_type.name if hasattr(att.attendee_type, 'name') else str(att.attendee_type)
            if type_filter != "All Types" and att_type.upper() != type_filter: continue
            searchable_text = f"{att.full_name} {att.attendee_id} {att.mobile} {att.email or ''} {att.business_name or ''}".lower()
            if search_query and search_query not in searchable_text: continue
            filtered.append(att)
            
        # Global Sort
        if sort_filter == "Latest First": filtered.sort(key=lambda x: getattr(x, 'created_at', datetime.min) or datetime.min, reverse=True)
        elif sort_filter == "Oldest First": filtered.sort(key=lambda x: getattr(x, 'created_at', datetime.min) or datetime.min, reverse=False)
        elif sort_filter == "Name (A-Z)": filtered.sort(key=lambda x: getattr(x, 'full_name', '').lower(), reverse=False)
        elif sort_filter == "Name (Z-A)": filtered.sort(key=lambda x: getattr(x, 'full_name', '').lower(), reverse=True)
        
        self.filtered_attendees = filtered
        self.current_page = 1
        self.render_page(preserve_selection=preserve_selection)

    def on_page_size_change(self):
        try: self.page_size = int(self.combo_page_size.currentText())
        except ValueError: self.page_size = 100
        self.current_page = 1
        self.render_page()

    def first_page(self):
        if self.current_page > 1: self.current_page = 1; self.render_page()

    def prev_page(self):
        if self.current_page > 1: self.current_page -= 1; self.render_page()

    def next_page(self):
        if self.current_page < self.total_pages: self.current_page += 1; self.render_page()

    def last_page(self):
        if self.current_page < self.total_pages: self.current_page = self.total_pages; self.render_page()

    def render_page(self, preserve_selection=None):
        self.table.setRowCount(0)
        self.table.clearSpans()
        
        total_items = len(self.filtered_attendees)
        if total_items == 0:
            self.lbl_page_info.setText("Page 0 of 0 (0 records)")
            self.lbl_record_count.setText("Showing 0 records")
            for btn in [self.btn_first, self.btn_prev, self.btn_next, self.btn_last]: btn.setEnabled(False)
            return
            
        self.total_pages = max(1, (total_items + self.page_size - 1) // self.page_size)
        if self.current_page > self.total_pages: self.current_page = self.total_pages
        if self.current_page < 1: self.current_page = 1
        
        start_idx = (self.current_page - 1) * self.page_size
        end_idx = min(start_idx + self.page_size, total_items)
        page_items = self.filtered_attendees[start_idx:end_idx]
        
        self.table.setRowCount(len(page_items))
        target_row_idx = -1
        
        for i, att in enumerate(page_items):
            sync_status = "Pending ⏳" if getattr(att, 'needs_cloud_sync', False) else "Synced ✓"
            att_type = att.attendee_type.name if hasattr(att.attendee_type, 'name') else str(att.attendee_type)
            
            row_data = [
                att.attendee_id, att.full_name, att.mobile, att_type,
                f"{att.city}, {att.state}", sync_status
            ]
            for col, val in enumerate(row_data):
                item = QTableWidgetItem(str(val))
                item.setData(Qt.UserRole, att.attendee_id) # Attach ID for easy retrieval
                # Text coloring for status
                if col == 5: item.setForeground(QBrush(QColor(COLORS["WARNING"] if "Pending" in sync_status else COLORS["SUCCESS"])))
                self.table.setItem(i, col, item)
                
            if preserve_selection and att.attendee_id == preserve_selection: target_row_idx = i

        self.lbl_page_info.setText(f"Page {self.current_page} of {self.total_pages:,} (Total: {total_items:,})")
        self.lbl_record_count.setText(f"Showing {start_idx+1:,}-{end_idx:,} of {total_items:,} records")
        
        self.btn_first.setEnabled(self.current_page > 1)
        self.btn_prev.setEnabled(self.current_page > 1)
        self.btn_next.setEnabled(self.current_page < self.total_pages)
        self.btn_last.setEnabled(self.current_page < self.total_pages)
        
        if target_row_idx >= 0:
            self.table.selectRow(target_row_idx)

    def sort_table_column(self, col):
        if self.current_sort_col == col: self.sort_reverse = not self.sort_reverse
        else: self.sort_reverse = False; self.current_sort_col = col
        
        keys = [
            lambda x: getattr(x, 'attendee_id', ''),
            lambda x: getattr(x, 'full_name', '').lower(),
            lambda x: getattr(x, 'mobile', ''),
            lambda x: getattr(x.attendee_type, 'name', str(x.attendee_type)),
            lambda x: getattr(x, 'city', '').lower(),
            lambda x: getattr(x, 'needs_cloud_sync', False)
        ]
        
        sort_key = keys[col] if col < len(keys) else keys[0]
        self.filtered_attendees.sort(key=sort_key, reverse=self.sort_reverse)
        
        sel = self.table.selectedItems()
        preserve_selection = sel[0].data(Qt.UserRole) if sel else None
        
        self.current_page = 1
        self.render_page(preserve_selection=preserve_selection)

    def on_row_select(self):
        sel_items = self.table.selectedItems()
        if not sel_items: return
        selected_id = sel_items[0].data(Qt.UserRole)
        
        attendee = next((a for a in self.all_attendees if a.attendee_id == selected_id), None)
        if not attendee: return
        
        self.lbl_profile_name.setText(attendee.full_name.upper())
        self.lbl_profile_id.setText(attendee.attendee_id)
        
        att_type = attendee.attendee_type.name if hasattr(attendee.attendee_type, 'name') else str(attendee.attendee_type)
        att_type_upper = att_type.upper()
        
        type_color = {
            "GENERAL": COLORS["PRIMARY"], "BUSINESS": COLORS["WARNING"],
            "MEDIA": COLORS["DANGER"], "EXHIBITOR": COLORS["PURPLE"]
        }.get(att_type_upper, COLORS["SECONDARY"])
        
        self.lbl_badge_type.setText(att_type_upper)
        self.lbl_badge_type.setStyleSheet(f"background-color: {type_color}; color: white; padding: 4px 10px; border-radius: 4px; font-weight: bold; font-size: 10px;")
        
        if getattr(attendee, 'needs_cloud_sync', False):
            self.lbl_badge_sync.setText("PENDING SYNC")
            self.lbl_badge_sync.setStyleSheet(f"background-color: {COLORS['WARNING']}; color: white; padding: 4px 10px; border-radius: 4px; font-weight: bold; font-size: 10px;")
        else:
            self.lbl_badge_sync.setText("CLOUD SYNCED")
            self.lbl_badge_sync.setStyleSheet(f"background-color: {COLORS['SUCCESS']}; color: white; padding: 4px 10px; border-radius: 4px; font-weight: bold; font-size: 10px;")
            
        self.profile_labels["Mobile"].setText(attendee.mobile)
        self.profile_labels["Email"].setText(attendee.email or "N/A")
        gender_val = attendee.gender.name if hasattr(attendee.gender, 'name') else str(attendee.gender)
        self.profile_labels["Gender"].setText(gender_val)
        biz_name = attendee.business_name or "N/A"
        biz_cat = f" ({attendee.business_category})" if attendee.business_category else ""
        self.profile_labels["Business"].setText(f"{biz_name}{biz_cat}")
        self.profile_labels["Location"].setText(f"{attendee.city}, {attendee.state}\nPIN: {attendee.pincode}")
        
        created_at = getattr(attendee, 'created_at', None)
        if created_at and created_at != datetime.min: self.profile_labels["Registered"].setText(created_at.strftime('%d %b %Y, %H:%M'))
        else: self.profile_labels["Registered"].setText("Unknown")
            
        history = attendee.checkin_history
        if isinstance(history, str):
            try: history = json.loads(history)
            except: history = {}
        if history: checkin_text = "\n".join([f"✓ {day}: {entry.get('timestamp', 'Unknown')[:16].replace('T', ' ')}" for day, entry in history.items()])
        else: checkin_text = "No check-ins yet."
        self.profile_labels["Check-ins"].setText(checkin_text)
        
        photo_path = os.path.join(PHOTOS_DIR, f"{attendee.attendee_id}.jpg")
        if os.path.exists(photo_path): self.render_image(photo_path)
        else:
            self.lbl_photo.setPixmap(QPixmap())
            self.lbl_photo.setText("📸\nNo Photo Found")

    def render_image(self, path):
        try:
            pixmap = QPixmap(path)
            if pixmap.isNull(): raise Exception("Invalid image")
            
            scaled = pixmap.scaled(180, 180, Qt.KeepAspectRatioByExpanding, Qt.SmoothTransformation)
            
            # Crop to exact square center
            crop_rect = scaled.rect()
            crop_rect.moveCenter(scaled.rect().center())
            cropped = scaled.copy(crop_rect.intersected(scaled.rect()))
            
            # Draw on background with border
            final_img = QPixmap(190, 190)
            final_img.fill(QColor(COLORS["CARD_BG"]))
            
            painter = QPainter(final_img)
            painter.setRenderHint(QPainter.Antialiasing)
            
            # Border rect
            painter.setPen(QColor(COLORS["BORDER"]))
            painter.setBrush(QColor(COLORS["BORDER"]))
            painter.drawRect(0, 0, 190, 190)
            
            # Draw Image
            painter.drawPixmap(5, 5, 180, 180, cropped)
            painter.end()
            
            self.lbl_photo.setPixmap(final_img)
            self.lbl_photo.setText("")
        except Exception as e:
            self.lbl_photo.setPixmap(QPixmap())
            self.lbl_photo.setText(f"Error loading image")
            logging.error(f"Failed to load image for profile: {e}")

    def export_csv(self):
        if not self.filtered_attendees:
            QMessageBox.warning(self, "Export Empty", "There are no records to export currently.")
            return
        default_name = f"Attendee_Export_{datetime.now().strftime('%Y%m%d_%H%M')}.csv"
        file_path, _ = QFileDialog.getSaveFileName(self, "Export to CSV", default_name, "CSV files (*.csv);;All files (*.*)")
        
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
            QMessageBox.information(self, "Export Successful", f"Successfully exported {len(self.filtered_attendees):,} records to:\n{file_path}")
        except Exception as e:
            logging.error(f"CSV Export failed: {e}")
            QMessageBox.critical(self, "Export Failed", f"Could not save file:\n{e}")

if __name__ == "__main__":
    if os.name == 'nt':
        try:
            my_app_id = os.environ.get("EVENTHUB_TOOL_ID", "EventHub.Tool.explorer")
            ctypes.windll.shell32.SetCurrentProcessExplicitAppUserModelID(my_app_id)
        except Exception: pass
        
    # High DPI Scaling Setup for Crisp UI on 2K/4K Monitors
    if hasattr(Qt, 'AA_EnableHighDpiScaling'):
        QApplication.setAttribute(Qt.AA_EnableHighDpiScaling, True)
    if hasattr(Qt, 'AA_UseHighDpiPixmaps'):
        QApplication.setAttribute(Qt.AA_UseHighDpiPixmaps, True)
        
    app = QApplication(sys.argv)
    window = AttendeeExplorer()
    window.show()
    sys.exit(app.exec())