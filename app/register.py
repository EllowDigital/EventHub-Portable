import os
import sys
import json
import logging
from logging.handlers import RotatingFileHandler
import time
import threading
import queue
import re
import uuid
import platform
import subprocess
import requests
import urllib3
import ctypes

from PySide6.QtWidgets import (QApplication, QMainWindow, QWidget, QVBoxLayout, QHBoxLayout, 
                               QLabel, QPushButton, QFrame, QGroupBox, QLineEdit, 
                               QCheckBox, QComboBox, QScrollArea, QDialog, QMessageBox, QCompleter)
from PySide6.QtCore import Qt, QTimer
from PySide6.QtGui import QColor, QFont, QIcon, QKeySequence, QShortcut, QCloseEvent, QKeyEvent

try:
    import psutil
    HAS_PSUTIL = True
except ImportError:
    HAS_PSUTIL = False

try:
    if platform.system() == "Windows":
        import winsound
        HAS_WINSOUND = True
    else:
        HAS_WINSOUND = False
except ImportError:
    HAS_WINSOUND = False

try:
    import pyttsx3
    HAS_TTS = True
except ImportError:
    HAS_TTS = False

urllib3.disable_warnings(urllib3.exceptions.InsecureRequestWarning)

BASE_DIR = os.path.dirname(os.path.abspath(__file__))
LOG_DIR = os.path.join(BASE_DIR, 'logs')
CONFIG_DIR = os.path.join(BASE_DIR, 'config')
os.makedirs(LOG_DIR, exist_ok=True)
os.makedirs(CONFIG_DIR, exist_ok=True)

LOG_FILE = os.path.join(LOG_DIR, 'kiosk_registration.log')
_file_handler = RotatingFileHandler(LOG_FILE, maxBytes=5_000_000, backupCount=3, encoding='utf-8')
_file_handler.setFormatter(logging.Formatter('%(asctime)s [%(levelname)s] %(message)s'))
logging.basicConfig(level=logging.INFO, handlers=[_file_handler])

def global_exception_handler(exc_type, exc_value, exc_traceback):
    logging.error("Uncaught GUI Exception intercepted. App remains running.", exc_info=(exc_type, exc_value, exc_traceback))

sys.excepthook = global_exception_handler

# Theme Colors Map - Adjusted to remove banding/black lines
COLORS = {
    "PRIMARY": "#375a7f", "INFO": "#0dcaf0", "SUCCESS": "#00bc8c",
    "WARNING": "#f39c12", "DANGER": "#e74c3c", "SECONDARY": "#888888",
    "BG_DARK": "#1E1E1E", "CARD_BG": "#252526", "BORDER": "#3E3E42", "TEXT": "#e0e0e0"
}

INDIAN_STATES = [
    "Andhra Pradesh", "Arunachal Pradesh", "Assam", "Bihar", "Chhattisgarh", "Goa", "Gujarat", 
    "Haryana", "Himachal Pradesh", "Jharkhand", "Karnataka", "Kerala", "Madhya Pradesh", 
    "Maharashtra", "Manipur", "Meghalaya", "Mizoram", "Nagaland", "Odisha", "Punjab", 
    "Rajasthan", "Sikkim", "Tamil Nadu", "Telangana", "Tripura", "Uttar Pradesh", "Uttarakhand", 
    "West Bengal", "Andaman and Nicobar Islands", "Chandigarh", "Dadra and Nagar Haveli and Daman and Diu", 
    "Delhi", "Jammu and Kashmir", "Ladakh", "Lakshadweep", "Puducherry"
]

POPULAR_CITIES = [
    "Mumbai", "Delhi", "Bengaluru", "Hyderabad", "Ahmedabad", "Chennai", "Kolkata", "Surat", 
    "Pune", "Jaipur", "Lucknow", "Kanpur", "Nagpur", "Indore", "Thane", "Bhopal", "Visakhapatnam", 
    "Pimpri-Chinchwad", "Patna", "Vadodara", "Ghaziabad", "Ludhiana", "Agra", "Nashik", "Faridabad", 
    "Meerut", "Rajkot", "Kalyan-Dombivli", "Vasai-Virar", "Varanasi", "Srinagar", "Aurangabad", 
    "Dhanbad", "Amritsar", "Navi Mumbai", "Allahabad", "Ranchi", "Howrah", "Coimbatore", "Jabalpur", 
    "Gwalior", "Vijayawada", "Jodhpur", "Madurai", "Raipur", "Kota", "Guwahati", "Chandigarh", 
    "Solapur", "Hubli-Dharwad", "Bareilly", "Mysore", "Tiruchirappalli", "Gurgaon", "Aligarh", 
    "Jalandhar", "Bhubaneswar", "Salem", "Noida", "Kochi", "Dehradun", "Durgapur", "Asansol", 
    "Rourkela", "Nanded", "Kolhapur", "Ajmer", "Akola", "Gulbarga", "Jamnagar", "Ujjain", "Loni", 
    "Siliguri", "Jhansi", "Ulhasnagar", "Jammu", "Sangli-Miraj & Kupwad", "Mangalore", "Erode", 
    "Belgaum", "Ambattur", "Tirunelveli", "Malegaon", "Gaya", "Jalgaon", "Udaipur", "Maheshtala"
]

CONFIG_FILE = os.path.join(CONFIG_DIR, 'register.json')
BACKUP_FILE = os.path.join(LOG_DIR, 'unsynced_registrations.json')

def load_config():
    default_id = "kiosk_" + uuid.uuid4().hex[:12]
    if os.path.exists(CONFIG_FILE):
        try:
            with open(CONFIG_FILE, 'r') as f:
                data = json.load(f)
                if "device_id" not in data:
                    data["device_id"] = default_id
                    save_config(data.get("server_url", "http://127.0.0.1:5000"), data.get("device_name", "Desktop Kiosk"), default_id)
                return data
        except Exception: pass
    return {"server_url": "http://127.0.0.1:5000", "device_name": "Desktop Kiosk", "device_id": default_id}

def save_config(url, name, device_id):
    with open(CONFIG_FILE, 'w') as f:
        json.dump({"server_url": url, "device_name": name, "device_id": device_id}, f, indent=4)

class LocalBackupManager:
    def __init__(self):
        self.lock = threading.Lock()

    def save(self, payload):
        with self.lock:
            data = self.load()
            data.append(payload)
            with open(BACKUP_FILE, 'w') as f:
                json.dump(data, f, indent=4)

    def remove(self, backup_id):
        with self.lock:
            data = self.load()
            data = [d for d in data if d.get('_backup_id') != backup_id]
            with open(BACKUP_FILE, 'w') as f:
                json.dump(data, f, indent=4)

    def load(self):
        if not os.path.exists(BACKUP_FILE): return []
        try:
            with open(BACKUP_FILE, 'r') as f: return json.load(f)
        except Exception: return []

backup_mgr = LocalBackupManager()

# ==============================================================================
# MAIN APPLICATION
# ==============================================================================
class OfflineKioskApp(QMainWindow):
    def __init__(self):
        super().__init__()
        
        self.config = load_config()
        self.server_url = self.config["server_url"].rstrip('/')
        self.device_name = self.config["device_name"]
        self.device_id = self.config["device_id"]
        
        self.setWindowTitle(f"TDE UP 2026 — {self.device_name}")
        self.resize(950, 850)
        self.setMinimumSize(800, 700)
        
        icon_path = os.path.join(BASE_DIR, "assets", "EventHub.ico")
        if os.path.exists(icon_path):
            try: self.setWindowIcon(QIcon(icon_path))
            except Exception: pass

        self.sound_enabled = True
        
        self.api_session = requests.Session()
        self.ping_session = requests.Session()
        self.sync_session = requests.Session()
        for session in [self.api_session, self.ping_session, self.sync_session]:
            session.headers.update({"User-Agent": "EventHub-Kiosk/2.6", "Connection": "keep-alive"})
        
        self.gui_queue = queue.Queue()
        self.is_pinging = True
        self.is_submitting = False
        self._showing_msg = False
        
        self._cached_battery = "N/A"
        self._cached_temp = "N/A"
        
        self.MOBILE_RE = re.compile(r"^[6-9]\d{9}$")
        self.PIN_RE = re.compile(r"^\d{6}$")
        self.EMAIL_RE = re.compile(r"^[\w.\-+]+@[\w.\-]+\.\w{2,}$")
        self._mobile_check_timer = None
        self._pincode_check_timer = None

        self._apply_stylesheet()
        self.build_ui()
        self.setup_reactive_logic()
        self.bind_shortcuts()
        
        self.queue_timer = QTimer(self)
        self.queue_timer.timeout.connect(self.process_gui_queue)
        self.queue_timer.start(20)
        
        threading.Thread(target=self.telemetry_loop, daemon=True).start()
        threading.Thread(target=self.network_ping_loop, daemon=True).start()
        threading.Thread(target=self.background_sync_loop, daemon=True).start()

    def _apply_stylesheet(self):
        # Unifying backgrounds and reducing border clash to fix "black lines"
        self.setStyleSheet(f"""
            QMainWindow, QWidget {{ background-color: {COLORS['BG_DARK']}; color: {COLORS['TEXT']}; font-family: 'Segoe UI', Arial; }}
            QGroupBox {{ 
                border: 1px solid {COLORS['BORDER']}; 
                border-radius: 8px; 
                margin-top: 20px; 
                padding-top: 15px;
                font-weight: bold; 
                background-color: transparent; 
            }}
            QGroupBox::title {{ 
                subcontrol-origin: margin; 
                left: 15px; 
                padding: 0 5px; 
                color: #CCCCCC; 
            }}
            QLineEdit, QComboBox {{ 
                background-color: #2D2D30; 
                border: 1px solid {COLORS['BORDER']}; 
                color: white; 
                border-radius: 4px; 
                padding: 8px; 
                font-size: 14px; 
            }}
            QLineEdit:focus, QComboBox:focus {{ border: 1px solid {COLORS['PRIMARY']}; }}
            QPushButton {{ 
                background-color: #333; 
                color: white; 
                border: 1px solid #555; 
                padding: 8px 16px; 
                border-radius: 4px; 
                font-weight: bold; 
            }}
            QPushButton:hover {{ background-color: #444; }}
            QPushButton:disabled {{ background-color: #222; color: #666; border: 1px solid #333; }}
            
            /* Crucial for fixing scroll area banding */
            QScrollArea {{ border: none; background-color: transparent; }}
            QScrollArea > QWidget > QWidget {{ background-color: transparent; }}
            
            QScrollBar:vertical {{ background: #1a1a1a; width: 14px; }}
            QScrollBar::handle:vertical {{ background: #444; min-height: 20px; border-radius: 7px; margin: 2px; }}
        """)

    def bind_shortcuts(self):
        QShortcut(QKeySequence("Ctrl+S"), self).activated.connect(self.submit_form)
        QShortcut(QKeySequence("Alt+C"), self).activated.connect(self.reset_form)

    def closeEvent(self, event: QCloseEvent):
        self.is_pinging = False
        try:
            self.api_session.close()
            self.ping_session.close()
            self.sync_session.close()
        except Exception: pass
        event.accept()

    def build_ui(self):
        central = QWidget()
        self.setCentralWidget(central)
        main_layout = QVBoxLayout(central)
        main_layout.setContentsMargins(0, 0, 0, 0)
        
        # Header
        header_frame = QWidget()
        header_frame.setStyleSheet(f"background-color: {COLORS['CARD_BG']}; border-bottom: 1px solid {COLORS['BORDER']};")
        header_lyt = QHBoxLayout(header_frame)
        header_lyt.setContentsMargins(20, 15, 20, 15)
        
        title_lbl = QLabel("Kiosk Registration")
        title_lbl.setFont(QFont("Segoe UI", 20, QFont.Bold))
        title_lbl.setStyleSheet(f"color: {COLORS['PRIMARY']}; border: none;")
        header_lyt.addWidget(title_lbl)
        header_lyt.addStretch()
        
        self.btn_sound = QPushButton("🔊 Sound Enabled")
        self.btn_sound.setStyleSheet(f"border: 1px solid {COLORS['SUCCESS']}; color: {COLORS['SUCCESS']}; background: transparent;")
        self.btn_sound.clicked.connect(self.toggle_sound)
        header_lyt.addWidget(self.btn_sound)
        
        btn_settings = QPushButton("⚙️ Settings")
        btn_settings.setStyleSheet(f"border: 1px solid {COLORS['SECONDARY']}; color: {COLORS['SECONDARY']}; background: transparent;")
        btn_settings.clicked.connect(self.open_settings)
        header_lyt.addWidget(btn_settings)
        
        self.net_pill = QFrame()
        self.net_pill.setStyleSheet(f"border: 1px solid {COLORS['BORDER']}; border-radius: 12px; background-color: #1e1e1e;")
        net_lyt = QHBoxLayout(self.net_pill)
        net_lyt.setContentsMargins(10, 5, 10, 5)
        self.net_dot = QLabel("●")
        self.net_dot.setStyleSheet(f"color: {COLORS['SECONDARY']}; font-size: 16px; border: none; background: transparent;")
        self.net_label = QLabel("Checking...")
        self.net_label.setStyleSheet("font-weight: bold; border: none; background: transparent; font-size: 11px;")
        net_lyt.addWidget(self.net_dot)
        net_lyt.addWidget(self.net_label)
        header_lyt.addWidget(self.net_pill)
        
        main_layout.addWidget(header_frame)
        
        # Shortcut Hint
        hint_lbl = QLabel("⌨️ Shortcuts: [Ctrl+S] Save  |  [Alt+C] Clear Form")
        hint_lbl.setStyleSheet("color: gray; font-size: 11px;")
        hint_lbl.setAlignment(Qt.AlignRight)
        hint_lbl.setContentsMargins(0, 5, 20, 0)
        main_layout.addWidget(hint_lbl)

        # Scroll Area
        scroll = QScrollArea()
        scroll.setWidgetResizable(True)
        scroll_content = QWidget()
        self.form_layout = QVBoxLayout(scroll_content)
        self.form_layout.setContentsMargins(30, 10, 30, 40)
        self.form_layout.setSpacing(25) # Increased spacing between blocks
        
        self.inputs = {}
        self.errors = {}
        
        # Identity Card
        id_card = QGroupBox(" 👤 Identity Details ")
        id_lyt = QVBoxLayout(id_card)
        id_lyt.setSpacing(15)
        self.create_input(id_lyt, "full_name", "Full Name *")
        row1 = QHBoxLayout()
        row1.setSpacing(15)
        self.create_input(row1, "mobile", "Mobile Number *")
        self.create_dropdown(row1, "gender", "Gender *", ["", "MALE", "FEMALE", "OTHER"])
        id_lyt.addLayout(row1)
        self.create_input(id_lyt, "email", "Email Address (Optional)")
        self.form_layout.addWidget(id_card)
        
        # Professional Card
        prof_card = QGroupBox(" 💼 Professional Details ")
        prof_lyt = QVBoxLayout(prof_card)
        prof_lyt.setSpacing(15)
        row2 = QHBoxLayout()
        row2.setSpacing(15)
        self.create_dropdown(row2, "attendee_type", "Attendee Type *", ["GENERAL", "BUSINESS", "MEDIA", "EXHIBITOR"], default="GENERAL")
        self.create_input(row2, "business_name", "Company / Firm Name")
        prof_lyt.addLayout(row2)
        row3 = QHBoxLayout()
        row3.setSpacing(15)
        cat_opts = [
            "", "TENT", "CATERING", "DECORATOR", "FLOWER", "DJ", "LIGHT", 
            "PHOTOGRAPHY", "VIDEOGRAPHY", "EVENT_PLANNER", "STAGE", "BAND", 
            "MAKEUP", "BANQUET", "TRANSPORT", "OTHER", "MEDIA_PRESS"
        ]
        self.create_dropdown(row3, "business_category", "Category", cat_opts)
        self.create_input(row3, "other_category", "Specify Other", is_disabled=True)
        prof_lyt.addLayout(row3)
        self.form_layout.addWidget(prof_card)
        
        # Location Card
        loc_card = QGroupBox(" 📍 Location Details ")
        loc_lyt = QVBoxLayout(loc_card)
        loc_lyt.setSpacing(15)
        self.create_input(loc_lyt, "address", "Full Address *")
        row4 = QHBoxLayout()
        row4.setSpacing(15)
        self.create_input(row4, "pincode", "Pincode *")
        self.create_autocomplete(row4, "city", "City *", POPULAR_CITIES)
        self.create_autocomplete(row4, "state", "State *", INDIAN_STATES)
        loc_lyt.addLayout(row4)
        self.form_layout.addWidget(loc_card)
        
        # Attendance Days Card
        day_card = QGroupBox(" 📅 Attendance Days * ")
        day_lyt = QVBoxLayout(day_card)
        d_row = QHBoxLayout()
        self.chk_day1 = QCheckBox("30 Aug")
        self.chk_day2 = QCheckBox("31 Aug")
        self.chk_day3 = QCheckBox("1 Sept")
        for chk in [self.chk_day1, self.chk_day2, self.chk_day3]:
            chk.setStyleSheet(f"font-weight: bold; font-size: 14px; padding: 5px;")
            d_row.addWidget(chk)
            chk.toggled.connect(lambda: self.errors['days'].setText(""))
        d_row.addStretch()
        day_lyt.addLayout(d_row)
        self.errors['days'] = QLabel("")
        self.errors['days'].setStyleSheet("color: #ff4444; font-size: 11px;")
        day_lyt.addWidget(self.errors['days'])
        self.form_layout.addWidget(day_card)
        
        # Action Area
        action_row = QHBoxLayout()
        self.chk_auto_clear = QCheckBox("Auto-clear form on success")
        self.chk_auto_clear.setChecked(True)
        btn_clear = QPushButton("🗑️ Clear Form (Alt+C)")
        btn_clear.setStyleSheet("background: transparent; color: #888; border: none; text-decoration: underline;")
        btn_clear.clicked.connect(self.reset_form)
        action_row.addWidget(self.chk_auto_clear)
        action_row.addStretch()
        action_row.addWidget(btn_clear)
        self.form_layout.addLayout(action_row)
        
        self.btn_submit = QPushButton("Register Attendee (Ctrl+S)")
        self.btn_submit.setStyleSheet(f"background-color: {COLORS['SUCCESS']}; color: white; padding: 15px; font-size: 16px; border-radius: 6px;")
        self.btn_submit.clicked.connect(self.submit_form)
        self.form_layout.addWidget(self.btn_submit)
        
        self.form_layout.addStretch()
        scroll.setWidget(scroll_content)
        main_layout.addWidget(scroll, 1)

        self.inputs['full_name'].setFocus()

    def create_input(self, parent_layout, name, label_text, is_disabled=False):
        wrapper = QVBoxLayout()
        wrapper.setSpacing(4) # Tighter coupling between label, input, and error
        
        lbl = QLabel(label_text)
        lbl.setStyleSheet("color: #CCCCCC; font-weight: bold; font-size: 12px;")
        wrapper.addWidget(lbl)
        
        ent = QLineEdit()
        if is_disabled: ent.setEnabled(False)
        self.inputs[name] = ent
        wrapper.addWidget(ent)
        
        err = QLabel("")
        err.setStyleSheet("color: #ff4444; font-size: 11px; margin: 0px; padding: 0px;")
        self.errors[name] = err
        wrapper.addWidget(err)
        
        if isinstance(parent_layout, QHBoxLayout): parent_layout.addLayout(wrapper, 1)
        else: parent_layout.addLayout(wrapper)

    def create_dropdown(self, parent_layout, name, label_text, options, default=""):
        wrapper = QVBoxLayout()
        wrapper.setSpacing(4)
        
        lbl = QLabel(label_text)
        lbl.setStyleSheet("color: #CCCCCC; font-weight: bold; font-size: 12px;")
        wrapper.addWidget(lbl)
        
        cb = QComboBox()
        cb.addItems(options)
        if default: cb.setCurrentText(default)
        self.inputs[name] = cb
        wrapper.addWidget(cb)
        
        err = QLabel("")
        err.setStyleSheet("color: #ff4444; font-size: 11px; margin: 0px; padding: 0px;")
        self.errors[name] = err
        wrapper.addWidget(err)
        
        if isinstance(parent_layout, QHBoxLayout): parent_layout.addLayout(wrapper, 1)
        else: parent_layout.addLayout(wrapper)

    def create_autocomplete(self, parent_layout, name, label_text, options):
        wrapper = QVBoxLayout()
        wrapper.setSpacing(4)
        
        lbl = QLabel(label_text)
        lbl.setStyleSheet("color: #CCCCCC; font-weight: bold; font-size: 12px;")
        wrapper.addWidget(lbl)
        
        cb = QComboBox()
        cb.setEditable(True)
        cb.addItems([""] + sorted(list(set(options))))
        completer = QCompleter(options)
        completer.setCaseSensitivity(Qt.CaseInsensitive)
        completer.setFilterMode(Qt.MatchContains)
        cb.setCompleter(completer)
        
        self.inputs[name] = cb
        wrapper.addWidget(cb)
        
        err = QLabel("")
        err.setStyleSheet("color: #ff4444; font-size: 11px; margin: 0px; padding: 0px;")
        self.errors[name] = err
        wrapper.addWidget(err)
        
        if isinstance(parent_layout, QHBoxLayout): parent_layout.addLayout(wrapper, 1)
        else: parent_layout.addLayout(wrapper)

    def toggle_sound(self):
        self.sound_enabled = not self.sound_enabled
        if self.sound_enabled:
            self.btn_sound.setText("🔊 Voice Enabled")
            self.btn_sound.setStyleSheet(f"border: 1px solid {COLORS['SUCCESS']}; color: {COLORS['SUCCESS']}; background: transparent;")
            self.play_sound("SUCCESS", "Audio alerts enabled.")
        else:
            self.btn_sound.setText("🔇 Muted")
            self.btn_sound.setStyleSheet(f"border: 1px solid {COLORS['SECONDARY']}; color: {COLORS['SECONDARY']}; background: transparent;")

    def play_sound(self, status, speak_text=""):
        if not self.sound_enabled: return
        def _play():
            if HAS_WINSOUND:
                try:
                    if status == "SUCCESS": winsound.Beep(2000, 100)  
                    elif status == "DUPLICATE": 
                        winsound.Beep(1000, 100); time.sleep(0.05); winsound.Beep(1000, 100)
                    else: winsound.Beep(200, 600)
                except Exception: QApplication.beep()
            else:
                QApplication.beep()
                if status != "SUCCESS": time.sleep(0.2); QApplication.beep()
                
            if speak_text:
                try:
                    if platform.system() == "Windows":
                        safe_text = speak_text.replace("'", "")
                        ps = f"Add-Type -AssemblyName System.Speech; $s = New-Object System.Speech.Synthesis.SpeechSynthesizer; $s.Rate=0; $s.Speak('{safe_text}');"
                        subprocess.run(["powershell", "-Command", ps], creationflags=subprocess.CREATE_NO_WINDOW)
                    elif HAS_TTS:
                        engine = pyttsx3.init()
                        engine.say(speak_text)
                        engine.runAndWait()
                except Exception as e: logging.error(f"TTS Error: {e}")
        threading.Thread(target=_play, daemon=True).start()

    def show_hub_message(self, msg):
        if getattr(self, '_showing_msg', False): return
        self._showing_msg = True
        self.play_sound("ALERT", "New message from Hub")
        
        modal = QDialog(self)
        modal.setWindowTitle("Hub Alert")
        modal.setFixedSize(450, 300)
        
        lyt = QVBoxLayout(modal)
        frame = QFrame()
        frame.setStyleSheet(f"border: 3px solid {COLORS['WARNING']}; background-color: {COLORS['CARD_BG']}; border-radius: 6px;")
        f_lyt = QVBoxLayout(frame)
        
        t = QLabel("📨 Hub Message")
        t.setFont(QFont("Segoe UI", 18, QFont.Bold))
        t.setAlignment(Qt.AlignCenter)
        t.setStyleSheet("border: none;")
        f_lyt.addWidget(t)
        
        m = QLabel(msg)
        m.setFont(QFont("Segoe UI", 12, QFont.Bold))
        m.setAlignment(Qt.AlignCenter)
        m.setWordWrap(True)
        m.setStyleSheet("border: none;")
        f_lyt.addWidget(m, 1)
        
        btn = QPushButton("Acknowledge Message")
        btn.setStyleSheet("background-color: #333; padding: 10px; font-weight: bold;")
        def close_msg():
            self._showing_msg = False
            modal.accept()
        btn.clicked.connect(close_msg)
        f_lyt.addWidget(btn)
        
        lyt.addWidget(frame)
        modal.exec()

    def setup_reactive_logic(self):
        self.inputs['attendee_type'].currentTextChanged.connect(self.on_type_change)
        self.inputs['business_category'].currentTextChanged.connect(self.on_category_change)
        self.inputs['mobile'].textChanged.connect(self.on_mobile_change)
        self.inputs['pincode'].textChanged.connect(self.on_pincode_change)
        
        for field, widget in self.inputs.items():
            if isinstance(widget, QLineEdit): widget.textChanged.connect(lambda t, f=field: self.clear_single_error(f))
            elif isinstance(widget, QComboBox): widget.currentTextChanged.connect(lambda t, f=field: self.clear_single_error(f))

    def clear_single_error(self, field):
        if field in self.inputs:
            self.inputs[field].setStyleSheet(f"background-color: #2D2D30; border: 1px solid {COLORS['BORDER']}; color: white; border-radius: 4px; padding: 8px; font-size: 14px;")
        if field in self.errors:
            self.errors[field].setText("")

    def on_type_change(self, text):
        if text == 'MEDIA':
            self.inputs['business_category'].setCurrentText('MEDIA_PRESS')
            self.inputs['business_category'].setEnabled(False)
            self.inputs['other_category'].setText('')
            self.inputs['other_category'].setEnabled(False)
        else:
            self.inputs['business_category'].setEnabled(True)
            if self.inputs['business_category'].currentText() == 'MEDIA_PRESS':
                self.inputs['business_category'].setCurrentIndex(0)
        self.clear_single_error('business_category')
        self.clear_single_error('business_name')

    def on_category_change(self, text):
        if text == 'OTHER':
            self.inputs['other_category'].setEnabled(True)
        else:
            self.inputs['other_category'].setText('')
            self.inputs['other_category'].setEnabled(False)
        self.clear_single_error('other_category')

    def on_mobile_change(self, text):
        clean_val = re.sub(r'\D', '', text)[:10]
        if text != clean_val:
            self.inputs['mobile'].setText(clean_val)
            
        if self._mobile_check_timer: self._mobile_check_timer.stop()
        
        if len(clean_val) == 10:
            self.errors['mobile'].setText("⏳ Checking number...")
            self.errors['mobile'].setStyleSheet(f"color: {COLORS['INFO']}; font-size: 11px;")
            self._mobile_check_timer = QTimer.singleShot(50, lambda: threading.Thread(target=self._check_mobile_status, args=(clean_val,), daemon=True).start())
        else:
            self.clear_single_error('mobile')

    def _check_mobile_status(self, mobile_num):
        try:
            res = self.api_session.get(f"{self.server_url}/api/check_mobile", params={"mobile": mobile_num}, timeout=3, verify=False)
            if res.status_code == 200:
                data = res.json()
                if data.get('status') in ['already_registered', 'registered', 'exists']:
                    aid = data.get('attendee_id', 'UNKNOWN ID')
                    self.gui_queue.put(lambda: self.errors['mobile'].setText(f"⚠ Already Registered! ID: {aid}"))
                    self.gui_queue.put(lambda: self.errors['mobile'].setStyleSheet(f"color: {COLORS['WARNING']}; font-size: 11px; font-weight: bold;"))
                    self.gui_queue.put(lambda: self.inputs['mobile'].setStyleSheet(f"border: 1px solid {COLORS['WARNING']}; background-color: #2D2D30; padding: 8px; border-radius: 4px;"))
                else:
                    self.gui_queue.put(lambda: self.errors['mobile'].setText("✓ Ready"))
                    self.gui_queue.put(lambda: self.errors['mobile'].setStyleSheet(f"color: {COLORS['SUCCESS']}; font-size: 11px;"))
            elif res.status_code == 404:
                self.gui_queue.put(lambda: self.errors['mobile'].setText("⚠ Backend missing route"))
                self.gui_queue.put(lambda: self.errors['mobile'].setStyleSheet(f"color: {COLORS['DANGER']}; font-size: 11px;"))
        except Exception:
            self.gui_queue.put(lambda: self.errors['mobile'].setText("⚠ Server offline"))
            self.gui_queue.put(lambda: self.errors['mobile'].setStyleSheet(f"color: {COLORS['DANGER']}; font-size: 11px;"))
            
    def on_pincode_change(self, text):
        clean_val = re.sub(r'\D', '', text)[:6]
        if text != clean_val:
            self.inputs['pincode'].setText(clean_val)
            
        if self._pincode_check_timer: self._pincode_check_timer.stop()
        
        if len(clean_val) == 6:
            self._pincode_check_timer = QTimer.singleShot(50, lambda: threading.Thread(target=self._check_pincode, args=(clean_val,), daemon=True).start())
        else:
            self.clear_single_error('pincode')

    def _check_pincode(self, pincode):
        try:
            res = self.api_session.get(f"{self.server_url}/api/pincode/{pincode}", timeout=3, verify=False)
            if res.status_code == 200:
                data = res.json()
                if data.get('status') == 'success':
                    state = data.get('state', '')
                    district = data.get('district', '')
                    self.gui_queue.put(lambda s=state, d=district: self._apply_pincode_data(s, d))
        except Exception as e:
            logging.error(f"Pincode API lookup failed: {e}")

    def _apply_pincode_data(self, state, district):
        if state: self.inputs['state'].setCurrentText(state.title())
        if district: self.inputs['city'].setCurrentText(district.title())
        self.clear_single_error('pincode')

    def open_settings(self):
        modal = QDialog(self)
        modal.setWindowTitle("Kiosk Configuration")
        modal.setFixedSize(400, 300)
        
        lyt = QVBoxLayout(modal)
        lyt.setContentsMargins(20, 20, 20, 20)
        
        lyt.addWidget(QLabel("Hub Connection URL:"))
        url_ent = QLineEdit(self.server_url)
        lyt.addWidget(url_ent)
        hint = QLabel("Example: http://192.168.137.1:5000")
        hint.setStyleSheet("color: gray; font-size: 11px;")
        lyt.addWidget(hint)
        
        lyt.addSpacing(15)
        lyt.addWidget(QLabel("Kiosk Device Name:"))
        name_ent = QLineEdit(self.device_name)
        lyt.addWidget(name_ent)
        
        lyt.addStretch()
        
        btn = QPushButton("Save Configuration")
        btn.setStyleSheet(f"background-color: {COLORS['SUCCESS']}; color: white; padding: 10px; font-weight: bold;")
        def save():
            self.server_url = url_ent.text().strip().rstrip('/')
            self.device_name = name_ent.text().strip()
            self.setWindowTitle(f"TDE UP 2026 — {self.device_name}")
            save_config(self.server_url, self.device_name, self.device_id)
            modal.accept()
            self.net_label.setText("Reconnecting...")
        btn.clicked.connect(save)
        lyt.addWidget(btn)
        
        modal.exec()

    def get_system_telemetry(self):
        battery_str = "N/A"
        temp_str = "N/A"
        if HAS_PSUTIL:
            try:
                batt = psutil.sensors_battery()
                if batt is not None:
                    battery_str = f"{int(batt.percent)}%" + (" AC" if batt.power_plugged else "")
                else:
                    battery_str = "AC Power (Desktop)"
            except Exception: pass
            try:
                if hasattr(psutil, 'sensors_temperatures'):
                    temps = psutil.sensors_temperatures()
                    if temps:
                        for k, v in temps.items():
                            if v and len(v) > 0:
                                temp_str = f"{int(v[0].current)}°C"
                                break
            except Exception: pass
            if temp_str == "N/A" and platform.system() == "Windows":
                try:
                    cmd = "powershell -Command \"(Get-CimInstance -Namespace root/wmi -ClassName MSAcpi_ThermalZoneTemperature).CurrentTemperature\""
                    output = subprocess.check_output(cmd, shell=True, stderr=subprocess.DEVNULL, timeout=2).decode().strip()
                    if output and output.isdigit():
                        kelvin_x10 = float(output)
                        celsius = int((kelvin_x10 / 10.0) - 273.15)
                        if 0 <= celsius <= 120:
                            temp_str = f"{celsius}°C"
                except Exception: pass
        return battery_str, temp_str

    def telemetry_loop(self):
        while self.is_pinging:
            try:
                b, t = self.get_system_telemetry()
                self._cached_battery = b
                self._cached_temp = t
            except Exception: pass
            time.sleep(15)

    def network_ping_loop(self):
        while self.is_pinging:
            try:
                payload = {
                    "device_id": self.device_id,
                    "device_name": self.device_name,
                    "page": "Desktop App",
                    "battery": getattr(self, '_cached_battery', 'N/A'),
                    "temp": getattr(self, '_cached_temp', 'N/A')
                }
                start_time = time.time()
                res = self.ping_session.post(f"{self.server_url}/api/status", json=payload, timeout=3, verify=False)
                res.raise_for_status()
                duration_ms = int((time.time() - start_time) * 1000)
                data = res.json()
                
                canonical = data.get("canonical_name")
                if canonical and canonical != self.device_name and canonical != "Unknown Device":
                    self.device_name = canonical
                    save_config(self.server_url, self.device_name, self.device_id)
                    self.gui_queue.put(lambda c=canonical: self.setWindowTitle(f"TDE UP 2026 — {c}"))
                msg = data.get("message")
                if msg:
                    self.gui_queue.put(lambda m=msg: self.show_hub_message(m))
                    
                if duration_ms < 150:
                    self.gui_queue.put(lambda ms=duration_ms: self.update_net_pill(f"Excellent • {ms}ms", "#00e676"))
                elif duration_ms < 500:
                    self.gui_queue.put(lambda ms=duration_ms: self.update_net_pill(f"Fair • {ms}ms", "#ffbb33"))
                else:
                    self.gui_queue.put(lambda ms=duration_ms: self.update_net_pill(f"Poor • {ms}ms", "#ff4444"))
            except Exception:
                self.gui_queue.put(lambda: self.update_net_pill("Offline", "#757575"))
            time.sleep(3)

    def background_sync_loop(self):
        while self.is_pinging:
            backups = backup_mgr.load()
            if backups:
                for b in list(backups):
                    if not self.is_pinging: break
                    try:
                        res = self.sync_session.post(f"{self.server_url}/api/register", json=b, timeout=5, verify=False)
                        if res.status_code == 200:
                            backup_mgr.remove(b['_backup_id'])
                    except Exception: break 
            time.sleep(10)

    def update_net_pill(self, text, color):
        self.net_label.setText(text)
        self.net_dot.setStyleSheet(f"color: {color}; font-size: 16px; border: none; background: transparent;")

    def process_gui_queue(self):
        for _ in range(100):
            try:
                self.gui_queue.get_nowait()()
            except queue.Empty: break

    def animate_submit_button(self, count=0):
        if self.is_submitting:
            dots = "." * ((count % 3) + 1)
            self.btn_submit.setText(f"⏳ Registering{dots}")
            QTimer.singleShot(400, lambda: self.animate_submit_button(count + 1))

    def set_error(self, field, msg):
        self.inputs[field].setStyleSheet(f"border: 1px solid {COLORS['DANGER']}; background-color: #2D2D30; padding: 8px; border-radius: 4px;")
        self.errors[field].setText(f"⚠ {msg}")
        self.errors[field].setStyleSheet(f"color: {COLORS['DANGER']}; font-size: 11px;")

    def clear_all_errors(self):
        for field, entry in self.inputs.items():
            entry.setStyleSheet(f"background-color: #2D2D30; border: 1px solid {COLORS['BORDER']}; color: white; border-radius: 4px; padding: 8px; font-size: 14px;")
        for err_lbl in self.errors.values():
            err_lbl.setText("")

    def get_val(self, field):
        w = self.inputs[field]
        if isinstance(w, QLineEdit): return w.text().strip()
        elif isinstance(w, QComboBox): return w.currentText().strip()
        return ""

    def validate_form(self):
        self.clear_all_errors()
        ok = True
        
        if len(self.get_val('full_name')) < 2:
            self.set_error('full_name', "Required (min 2 chars)")
            ok = False
            
        if not self.MOBILE_RE.match(self.get_val('mobile')):
            self.set_error('mobile', "Valid 10-digit number required")
            ok = False
            
        email = self.get_val('email')
        if email and not self.EMAIL_RE.match(email):
            self.set_error('email', "Invalid email")
            ok = False
            
        if not self.get_val('gender'):
            self.set_error('gender', "Required")
            ok = False
            
        att_type = self.get_val('attendee_type')
        biz_name = self.get_val('business_name')
        if att_type in ['BUSINESS', 'EXHIBITOR', 'MEDIA'] and not biz_name:
            self.set_error('business_name', "Required for this type")
            ok = False
            
        cat = self.get_val('business_category')
        other = self.get_val('other_category')
        if att_type in ['BUSINESS', 'EXHIBITOR']:
            if not cat:
                self.set_error('business_category', "Required")
                ok = False
            elif cat == 'OTHER' and not other:
                self.set_error('other_category', "Specify category")
                ok = False
                
        if len(self.get_val('address')) < 5:
            self.set_error('address', "Required (min 5 chars)")
            ok = False
        if len(self.get_val('city')) < 2:
            self.set_error('city', "Required")
            ok = False
        if len(self.get_val('state')) < 2:
            self.set_error('state', "Required")
            ok = False
        if not self.PIN_RE.match(self.get_val('pincode')):
            self.set_error('pincode', "6-digit pincode required")
            ok = False
            
        d1, d2, d3 = self.chk_day1.isChecked(), self.chk_day2.isChecked(), self.chk_day3.isChecked()
        if not (d1 or d2 or d3):
            self.errors['days'].setText("⚠ Select at least one day")
            ok = False
            
        return ok

    def submit_form(self):
        if self.is_submitting: return
        if not self.validate_form():
            self.play_sound("ERROR", "Please check the form for errors.")
            return
            
        self.is_submitting = True
        self.btn_submit.setEnabled(False)
        self.btn_submit.setStyleSheet(f"background-color: {COLORS['WARNING']}; color: white; padding: 15px; font-size: 16px; border-radius: 6px;")
        self.animate_submit_button()
        
        selected_days = []
        if self.chk_day1.isChecked(): selected_days.append("30 August")
        if self.chk_day2.isChecked(): selected_days.append("31 August")
        if self.chk_day3.isChecked(): selected_days.append("1 September")
        
        payload = {
            "_backup_id": str(uuid.uuid4()),
            "full_name": self.get_val('full_name'),
            "mobile": self.get_val('mobile'),
            "email": self.get_val('email') or None,
            "gender": self.get_val('gender'),
            "attendee_type": self.get_val('attendee_type'),
            "business_name": self.get_val('business_name') or None,
            "business_category": self.get_val('business_category') or None,
            "other_category": self.get_val('other_category') or None,
            "address": self.get_val('address'),
            "city": self.get_val('city'),
            "state": self.get_val('state'),
            "pincode": self.get_val('pincode'),
            "attendance_days": selected_days,
            "device_name": self.device_name,
            "device_id": self.device_id
        }
        backup_mgr.save(payload)
        threading.Thread(target=self._post_registration_infinite_loop, args=(payload,), daemon=True).start()

    def _post_registration_infinite_loop(self, payload):
        attempt = 1
        while self.is_submitting and self.is_pinging:
            try:
                res = self.api_session.post(f"{self.server_url}/api/register", json=payload, timeout=5, verify=False)
                res.raise_for_status()
                data = res.json()
                backup_mgr.remove(payload['_backup_id'])
                self.is_submitting = False
                
                if data.get('status') == 'success':
                    self.gui_queue.put(lambda: self.show_success_modal(data.get('attendee_id'), is_duplicate=False))
                elif data.get('status') in ['already_registered', 'registered', 'exists']:
                    self.gui_queue.put(lambda: self.show_success_modal(data.get('attendee_id'), is_duplicate=True))
                else:
                    self.gui_queue.put(lambda: self.handle_submit_error(f"Server Error: {data.get('message', 'Unknown Error')}"))
                break
            except requests.exceptions.RequestException:
                self.gui_queue.put(lambda a=attempt: self.btn_submit.setText(f"⏳ Connection Lost... Retrying ({a})"))
                self.gui_queue.put(lambda: self.btn_submit.setStyleSheet(f"background-color: {COLORS['DANGER']}; color: white; padding: 15px; font-size: 16px; border-radius: 6px;"))
                time.sleep(3)
                attempt += 1

    def handle_submit_error(self, message):
        self.is_submitting = False
        self.play_sound("ERROR", "Warning. Registration failed.")
        QMessageBox.critical(self, "Registration Failed", message)
        self.btn_submit.setEnabled(True)
        self.btn_submit.setText("Register Attendee (Ctrl+S)")
        self.btn_submit.setStyleSheet(f"background-color: {COLORS['SUCCESS']}; color: white; padding: 15px; font-size: 16px; border-radius: 6px;")

    class SuccessDialog(QDialog):
        def __init__(self, parent, aid, is_duplicate, countdown_val, on_close_cb):
            super().__init__(parent)
            self.setWindowFlags(Qt.Popup | Qt.FramelessWindowHint)
            # Widen the modal so the full ID fits perfectly without clipping
            self.setFixedSize(550, 350)
            self.countdown_val = countdown_val
            self.on_close_cb = on_close_cb
            
            lyt = QVBoxLayout(self)
            frame = QFrame()
            frame.setStyleSheet(f"border: 2px solid {COLORS['BORDER']}; background-color: {COLORS['CARD_BG']}; border-radius: 6px;")
            f_lyt = QVBoxLayout(frame)
            
            if is_duplicate:
                t = QLabel("Already Registered!")
                t.setStyleSheet(f"color: {COLORS['WARNING']}; font-size: 22px; font-weight: bold; border: none;")
                m = QLabel("This mobile number is already in the system.\nExisting ID:")
                id_color = COLORS['WARNING']
            else:
                t = QLabel("Registration Saved!")
                t.setStyleSheet(f"color: {COLORS['SUCCESS']}; font-size: 22px; font-weight: bold; border: none;")
                m = QLabel("Please provide the attendee with their pass code:")
                id_color = COLORS['SUCCESS']
                
            t.setAlignment(Qt.AlignCenter)
            m.setAlignment(Qt.AlignCenter)
            m.setStyleSheet("font-size: 12px; border: none;")
            f_lyt.addWidget(t)
            f_lyt.addWidget(m)
            
            id_lbl = QLabel(aid)
            id_lbl.setAlignment(Qt.AlignCenter)
            # Reduced font size slightly to guarantee it fits entirely inside the box
            id_lbl.setFont(QFont("Consolas", 26, QFont.Bold))
            id_lbl.setStyleSheet(f"background-color: {COLORS['BG_DARK']}; color: {id_color}; border-radius: 6px; padding: 10px; margin: 10px 20px; letter-spacing: 2px;")
            f_lyt.addWidget(id_lbl)
            
            self.cd_lbl = QLabel(f"Returning to form in {self.countdown_val}s... (Press Enter)")
            self.cd_lbl.setAlignment(Qt.AlignCenter)
            self.cd_lbl.setStyleSheet("color: gray; border: none;")
            f_lyt.addWidget(self.cd_lbl)
            
            btn = QPushButton("Next Registration (Enter)")
            btn.setStyleSheet(f"background-color: {COLORS['SECONDARY']}; padding: 10px; font-weight: bold; margin: 10px 40px;")
            btn.clicked.connect(self.accept)
            f_lyt.addWidget(btn)
            
            lyt.addWidget(frame)
            
            self.timer = QTimer(self)
            self.timer.timeout.connect(self.tick)
            self.timer.start(1000)
            
        def keyPressEvent(self, event: QKeyEvent):
            if event.key() in (Qt.Key_Return, Qt.Key_Enter, Qt.Key_Escape):
                self.accept()
                
        def tick(self):
            self.countdown_val -= 1
            if self.countdown_val > 0:
                self.cd_lbl.setText(f"Returning to form in {self.countdown_val}s... (Press Enter)")
            else:
                self.accept()

        def accept(self):
            self.timer.stop()
            super().accept()
            self.on_close_cb()

    def show_success_modal(self, aid, is_duplicate=False):
        if is_duplicate: self.play_sound("DUPLICATE", "Warning. Attendee already registered.")
        else: self.play_sound("SUCCESS", "Registration saved successfully.")
        
        def on_close():
            if self.chk_auto_clear.isChecked(): self.reset_form()
            else:
                self.btn_submit.setEnabled(True)
                self.btn_submit.setText("Register Attendee (Ctrl+S)")
                self.btn_submit.setStyleSheet(f"background-color: {COLORS['SUCCESS']}; color: white; padding: 15px; font-size: 16px; border-radius: 6px;")
                self.inputs['full_name'].setFocus()
                
        dlg = self.SuccessDialog(self, aid, is_duplicate, 8, on_close)
        dlg.exec()

    def reset_form(self):
        for name, widget in self.inputs.items():
            if isinstance(widget, QLineEdit): widget.setText("")
            elif isinstance(widget, QComboBox):
                if name == 'attendee_type': widget.setCurrentText("GENERAL")
                else: widget.setCurrentText("")
                
        self.chk_day1.setChecked(False)
        self.chk_day2.setChecked(False)
        self.chk_day3.setChecked(False)
        
        self.inputs['business_category'].setEnabled(True)
        self.inputs['other_category'].setEnabled(False)
        
        self.clear_all_errors()
        self.is_submitting = False
        
        self.btn_submit.setEnabled(True)
        self.btn_submit.setText("Register Attendee (Ctrl+S)")
        self.btn_submit.setStyleSheet(f"background-color: {COLORS['SUCCESS']}; color: white; padding: 15px; font-size: 16px; border-radius: 6px;")
        self.inputs['full_name'].setFocus()

if __name__ == "__main__":
    if os.name == 'nt':
        try:
            my_app_id = os.environ.get("EVENTHUB_TOOL_ID", "EventHub.Tool.kiosk")
            ctypes.windll.shell32.SetCurrentProcessExplicitAppUserModelID(my_app_id)
        except Exception: pass
    app = QApplication(sys.argv)
    window = OfflineKioskApp()
    window.show()
    sys.exit(app.exec())