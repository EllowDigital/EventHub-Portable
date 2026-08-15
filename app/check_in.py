import os
import sys
import json
import logging
from logging.handlers import RotatingFileHandler
import threading
import socket
import platform
import subprocess
import time
import queue
import collections
import uuid
import requests
import urllib3
import ctypes
from datetime import datetime

from PySide6.QtWidgets import (QApplication, QMainWindow, QWidget, QVBoxLayout, QHBoxLayout, 
                               QGridLayout, QLabel, QPushButton, QFrame, QGroupBox, QLineEdit, 
                               QDialog, QMessageBox, QFileDialog, QScrollArea, QSizePolicy, QSplitter)
from PySide6.QtCore import Qt, QTimer, QSize
from PySide6.QtGui import QPixmap, QPainter, QColor, QFont, QIcon, QBrush, QImage, QKeySequence, QShortcut

urllib3.disable_warnings(urllib3.exceptions.InsecureRequestWarning)

try:
    import psutil
    HAS_PSUTIL = True
except ImportError:
    HAS_PSUTIL = False

HAS_WINSOUND = False
if platform.system() == "Windows":
    try:
        import winsound
        HAS_WINSOUND = True
    except ImportError:
        pass

HAS_TTS = False
try:
    import pyttsx3
    HAS_TTS = True
except ImportError:
    pass

BASE_DIR = os.path.dirname(os.path.abspath(__file__))
CONFIG_PATH = os.path.join(BASE_DIR, 'config', 'checkin.json')
DEFAULT_PHOTO_DIR = 'attendee_photos'

LOG_DIR = os.path.join(BASE_DIR, 'logs')
os.makedirs(LOG_DIR, exist_ok=True)
LOG_FILE = os.path.join(LOG_DIR, 'gate_display.log')
_file_handler = RotatingFileHandler(LOG_FILE, maxBytes=5_000_000, backupCount=3, encoding='utf-8')
_file_handler.setFormatter(logging.Formatter('%(asctime)s [%(levelname)s] %(message)s'))
logging.basicConfig(level=logging.INFO, handlers=[_file_handler])

# PySide6 Theme Colors Map
THEMES = {
    "darkly": {
        "BG": "#141414", "CARD_BG": "#242424", "BORDER": "#333333", "TEXT": "#e0e0e0",
        "PRIMARY": "#375a7f", "INFO": "#0dcaf0", "SUCCESS": "#00bc8c", "WARNING": "#f39c12", "DANGER": "#e74c3c", "SECONDARY": "#888888"
    },
    "flatly": {
        "BG": "#f8f9fa", "CARD_BG": "#ffffff", "BORDER": "#ced4da", "TEXT": "#212529",
        "PRIMARY": "#2c3e50", "INFO": "#3498db", "SUCCESS": "#18bc9c", "WARNING": "#f39c12", "DANGER": "#e74c3c", "SECONDARY": "#95a5a6"
    }
}

CATEGORY_STYLES = {
    "business": "WARNING",    
    "general": "PRIMARY",     
    "media": "DANGER",        
    "exhibitor": "INFO",      
    "default": "SECONDARY"    
}

def global_exception_handler(exc_type, exc_value, exc_traceback):
    logging.error("Uncaught GUI Exception intercepted. App remains running.", exc_info=(exc_type, exc_value, exc_traceback))

sys.excepthook = global_exception_handler

# ==============================================================================
# CONFIG & NOTIFICATION ENGINES
# ==============================================================================
class ConfigManager:
    def __init__(self):
        self.config = {
            "hub_url": "http://127.0.0.1:5000",
            "device_name": "Gate_Display_1",
            "poll_interval_ms": 500,
            "photo_directory": DEFAULT_PHOTO_DIR
        }
        self.load()

    def load(self):
        default_id = "display_" + uuid.uuid4().hex[:12]
        if os.path.exists(CONFIG_PATH):
            try:
                with open(CONFIG_PATH, 'r') as f:
                    content = f.read().strip()
                    if content:
                        data = json.loads(content)
                        if "device_id" not in data:
                            data["device_id"] = default_id
                        self.config.update(data)
                        self.save()
                        return
            except Exception as e:
                logging.error(f"Config load error: {e}")
        self.config["device_id"] = default_id
        self.save()

    def save(self):
        os.makedirs(os.path.dirname(CONFIG_PATH), exist_ok=True)
        with open(CONFIG_PATH, 'w') as f:
            json.dump(self.config, f, indent=4)


class NotificationEngine(threading.Thread):
    def __init__(self):
        super().__init__(daemon=True)
        self.queue = queue.Queue()
        self.sound_enabled = True
        self.engine = None
        if HAS_TTS and platform.system() != "Windows":
            try:
                self.engine = pyttsx3.init()
                for voice in self.engine.getProperty('voices'):
                    if any(name in voice.name.lower() for name in ['female', 'zira', 'samantha']):
                        self.engine.setProperty('voice', voice.id)
                        break
            except Exception as e:
                logging.error(f"TTS Init Error: {e}")

    def run(self):
        while True:
            task = self.queue.get()
            if not self.sound_enabled:
                self.queue.task_done()
                continue
            status, message = task.get("status"), task.get("message", "")
            if HAS_WINSOUND:
                try:
                    if status == "SUCCESS": winsound.Beep(2000, 100)  
                    elif status == "DUPLICATE": winsound.Beep(1000, 100); time.sleep(0.05); winsound.Beep(1000, 100)
                    elif status == "ALERT": winsound.Beep(600, 200); winsound.Beep(800, 500)
                    else: winsound.Beep(400, 150); winsound.Beep(300, 300)
                except Exception: QApplication.beep()
            else:
                QApplication.beep()
                if status not in ["SUCCESS", "ALERT"]:
                    time.sleep(0.2)
                    QApplication.beep()
                    
            speak_text = "Access Denied." if status not in ["SUCCESS", "DUPLICATE", "ALERT"] else ""
            if speak_text:
                if platform.system() == "Windows":
                    safe_text = speak_text.replace("'", "")
                    ps_script = (
                        f"Add-Type -AssemblyName System.Speech; "
                        f"$s = New-Object System.Speech.Synthesis.SpeechSynthesizer; "
                        f"$s.SelectVoiceByHints('Female'); "
                        f"$s.Speak('{safe_text}');"
                    )
                    subprocess.run(["powershell", "-Command", ps_script], creationflags=subprocess.CREATE_NO_WINDOW)
                elif self.engine:
                    try:
                        self.engine.say(speak_text)
                        self.engine.runAndWait()
                    except Exception as e: logging.error(f"TTS Play Error: {e}")
            self.queue.task_done()

# ==============================================================================
# UI COMPONENTS
# ==============================================================================
class SettingsDialog(QDialog):
    def __init__(self, parent, config_manager, on_save_callback):
        super().__init__(parent)
        self.setWindowTitle("Settings — Gate Terminal")
        self.setFixedSize(520, 420)
        self.config_manager = config_manager
        self.on_save = on_save_callback
        
        self.setWindowFlags(self.windowFlags() | Qt.WindowStaysOnTopHint)
        self.build_ui()

    def build_ui(self):
        lyt = QVBoxLayout(self)
        lyt.setContentsMargins(30, 30, 30, 30)
        
        t = QLabel("⚙️ Terminal Settings")
        t.setFont(QFont("Segoe UI", 18, QFont.Bold))
        t.setStyleSheet(f"color: {THEMES['darkly']['PRIMARY']};")
        lyt.addWidget(t)
        lyt.addSpacing(15)
        
        fields = [
            ("Hub Server URL (HTTP/HTTPS)", "hub_url", self.config_manager.config["hub_url"]),
            ("Device Identifier Name", "device_name", self.config_manager.config["device_name"])
        ]
        
        self.entries = {}
        for label_text, key, val in fields:
            lbl = QLabel(label_text)
            lbl.setFont(QFont("Segoe UI", 10, QFont.Bold))
            lyt.addWidget(lbl)
            
            ent = QLineEdit(val)
            lyt.addWidget(ent)
            lyt.addSpacing(10)
            self.entries[key] = ent
            
        lbl = QLabel("Local Photo Directory")
        lbl.setFont(QFont("Segoe UI", 10, QFont.Bold))
        lyt.addWidget(lbl)
        
        photo_row = QHBoxLayout()
        self.ent_photo = QLineEdit(self.config_manager.config["photo_directory"])
        photo_row.addWidget(self.ent_photo, 1)
        
        btn_browse = QPushButton("Browse")
        btn_browse.clicked.connect(self.browse_dir)
        photo_row.addWidget(btn_browse)
        lyt.addLayout(photo_row)
        
        lyt.addStretch()
        
        btn_row = QHBoxLayout()
        btn_cancel = QPushButton("Cancel")
        btn_cancel.clicked.connect(self.reject)
        
        btn_save = QPushButton("💾 Save & Apply")
        btn_save.setStyleSheet(f"background-color: {THEMES['darkly']['SUCCESS']}; color: white; font-weight: bold;")
        btn_save.clicked.connect(self.save)
        
        btn_row.addWidget(btn_cancel)
        btn_row.addWidget(btn_save)
        lyt.addLayout(btn_row)

    def browse_dir(self):
        start_dir = os.path.normpath(os.path.join(BASE_DIR, self.ent_photo.text()))
        d = QFileDialog.getExistingDirectory(self, "Select Directory", start_dir)
        if d:
            self.ent_photo.setText(os.path.relpath(d, start=BASE_DIR).replace('\\', '/'))

    def save(self):
        self.config_manager.config["hub_url"] = self.entries["hub_url"].text().strip().rstrip('/')
        self.config_manager.config["device_name"] = self.entries["device_name"].text().strip()
        self.config_manager.config["photo_directory"] = self.ent_photo.text().strip()
        self.config_manager.save()
        self.on_save()
        self.accept()


class GateDisplay(QMainWindow):
    def __init__(self):
        super().__init__()
        self.setWindowTitle("EventHub Portable (v2.6) — Gate Terminal")
        self.resize(1500, 950) # Improved default scale for 2K/4K adaptation
        self.setMinimumSize(1280, 750)
        
        icon_path = os.path.join(BASE_DIR, "assets", "EventHub.ico")
        if os.path.exists(icon_path):
            try: self.setWindowIcon(QIcon(icon_path))
            except Exception: pass

        self.current_theme = "darkly"
        self.config_manager = ConfigManager()
        self.gui_queue = queue.Queue()
        self.scan_queue = queue.Queue()  
        self.is_polling = False
        self._showing_msg = False
        self._cached_battery = "N/A"
        self._cached_temp = "N/A"
        
        self.notifier = NotificationEngine()
        self.notifier.start()
        
        self.stats = {"Success": 0, "Duplicate": 0, "Wrong Day": 0, "Errors": 0}
        self.recent_scans = []
        self.current_photo = None
        self._placeholder_img_cache = {}
        self._photo_cache = collections.OrderedDict() 
        self._last_scan_time = 0.0
        self._processed_sigs = collections.deque(maxlen=200) 
        
        self.stream_session = None
        self.api_session = None
        
        self.build_ui()
        self.apply_theme()
        
        self.queue_timer = QTimer(self)
        self.queue_timer.timeout.connect(self.process_queues)
        self.queue_timer.start(20)
        
        self.start_threads()

    def toggle_theme(self):
        self.current_theme = "flatly" if self.current_theme == "darkly" else "darkly"
        self.apply_theme()
        if self.lbl_attendee_id.text() == "---":
            self.set_placeholder_photo()

    def apply_theme(self):
        t = THEMES[self.current_theme]
        self.setStyleSheet(f"""
            QMainWindow, QWidget {{ background-color: {t['BG']}; color: {t['TEXT']}; font-family: 'Segoe UI', Arial; font-size: 10pt; }}
            QFrame#Card {{ background-color: {t['CARD_BG']}; border: 1px solid {t['BORDER']}; border-radius: 8px; }}
            QLabel {{ background: transparent; border: none; }}
            QLineEdit {{ 
                background-color: {t['CARD_BG']}; 
                border: 1px solid {t['BORDER']}; 
                color: {t['TEXT']}; 
                border-radius: 4px; 
                padding: 10px; 
                font-size: 11pt; 
            }}
            QLineEdit:focus {{ border: 1px solid {t['PRIMARY']}; }}
            QPushButton {{ 
                background-color: {t['CARD_BG']}; 
                color: {t['TEXT']}; 
                border: 1px solid {t['BORDER']}; 
                padding: 8px 16px; 
                border-radius: 4px; 
                font-weight: bold; 
            }}
            QPushButton:hover {{ background-color: {t['BORDER']}; }}
            #Pill {{ border: 1px solid {t['BORDER']}; border-radius: 18px; background-color: {t['CARD_BG']}; }}
            #BigBanner {{ border-radius: 8px; padding: 15px; font-weight: bold; font-size: 26pt; }}
            #RecentCard {{ border: 1px solid {t['BORDER']}; border-radius: 6px; }}
            QSplitter::handle {{ background-color: {t['BORDER']}; width: 2px; }}
        """)

    def build_ui(self):
        central = QWidget()
        self.setCentralWidget(central)
        main_layout = QVBoxLayout(central)
        main_layout.setContentsMargins(0, 0, 0, 0)
        
        # NAV BAR
        self.nav = QWidget()
        self.nav.setStyleSheet(f"border-bottom: 1px solid #333;")
        nav_lyt = QHBoxLayout(self.nav)
        nav_lyt.setContentsMargins(20, 15, 20, 15)
        
        title_box = QVBoxLayout()
        t1 = QLabel("🎟️ Gate Display Terminal")
        t1.setFont(QFont("Segoe UI", 22, QFont.Bold))
        self.lbl_subtitle = QLabel(f"{self.config_manager.config['device_name']} • TDE UP 2026")
        self.lbl_subtitle.setStyleSheet("color: gray; font-weight: bold;")
        title_box.addWidget(t1)
        title_box.addWidget(self.lbl_subtitle)
        nav_lyt.addLayout(title_box)
        nav_lyt.addStretch()
        
        btn_theme = QPushButton("🌗 Theme")
        btn_theme.clicked.connect(self.toggle_theme)
        btn_settings = QPushButton("⚙️ Settings")
        btn_settings.clicked.connect(self.open_settings)
        self.btn_sound = QPushButton("🔊 Sound")
        self.btn_sound.clicked.connect(self.toggle_sound)
        btn_fs = QPushButton("⛶ Fullscreen")
        btn_fs.clicked.connect(lambda: self.showNormal() if self.isFullScreen() else self.showFullScreen())
        
        self.net_pill = QFrame()
        self.net_pill.setObjectName("Pill")
        net_lyt = QHBoxLayout(self.net_pill)
        net_lyt.setContentsMargins(15, 5, 15, 5)
        self.net_dot = QLabel("●")
        self.net_dot.setStyleSheet(f"color: {THEMES['darkly']['WARNING']}; font-size: 16px;")
        self.lbl_hub_status = QLabel("Connecting...")
        self.lbl_hub_status.setStyleSheet("font-weight: bold; font-size: 10pt;")
        net_lyt.addWidget(self.net_dot)
        net_lyt.addWidget(self.lbl_hub_status)
        
        for w in [btn_theme, btn_settings, self.btn_sound, btn_fs, self.net_pill]:
            nav_lyt.addWidget(w)
            nav_lyt.addSpacing(5)
            
        main_layout.addWidget(self.nav)

        # TEST MODE BANNER
        self.test_banner = QLabel("⚠️ TEST MODE ACTIVE")
        self.test_banner.setAlignment(Qt.AlignCenter)
        self.test_banner.setFont(QFont("Segoe UI", 14, QFont.Bold))
        self.test_banner.setStyleSheet(f"background-color: {THEMES['darkly']['DANGER']}; color: white; padding: 10px;")
        self.test_banner.hide()
        main_layout.addWidget(self.test_banner)
        
        # SPLITTER (Responsive Left/Right adaptation)
        self.splitter = QSplitter(Qt.Horizontal)
        self.splitter.setContentsMargins(30, 30, 30, 30)
        
        # --- LEFT PANEL (Scan Profile) ---
        left_panel = QWidget()
        left_lyt = QVBoxLayout(left_panel)
        left_lyt.setContentsMargins(0, 0, 15, 0)
        
        self.status_banner = QLabel("WAITING FOR SCAN...")
        self.status_banner.setObjectName("BigBanner")
        self.status_banner.setAlignment(Qt.AlignCenter)
        self.status_banner.setStyleSheet(f"background-color: {THEMES['darkly']['SECONDARY']}; color: white;")
        left_lyt.addWidget(self.status_banner)
        left_lyt.addSpacing(30)
        
        profile_frame = QHBoxLayout()
        
        photo_box = QVBoxLayout()
        self.lbl_photo = QLabel()
        self.lbl_photo.setFixedSize(340, 340)
        self.lbl_photo.setAlignment(Qt.AlignCenter)
        self.set_placeholder_photo()
        self.lbl_attendee_id = QLabel("---")
        self.lbl_attendee_id.setAlignment(Qt.AlignCenter)
        self.lbl_attendee_id.setFont(QFont("Segoe UI", 16, QFont.Bold))
        self.lbl_attendee_id.setStyleSheet("color: gray;")
        photo_box.addWidget(self.lbl_photo)
        photo_box.addWidget(self.lbl_attendee_id)
        photo_box.addStretch()
        profile_frame.addLayout(photo_box)
        profile_frame.addSpacing(30)
        
        details_box = QVBoxLayout()
        details_hdr = QHBoxLayout()
        self.lbl_name = QLabel("SCAN TICKET")
        self.lbl_name.setFont(QFont("Segoe UI", 36, QFont.Bold))
        self.lbl_name.setWordWrap(True)
        self.lbl_pass_badge = QLabel("PENDING")
        self.lbl_pass_badge.setAlignment(Qt.AlignCenter)
        self.lbl_pass_badge.setFont(QFont("Segoe UI", 16, QFont.Bold))
        self.lbl_pass_badge.setStyleSheet(f"background-color: {THEMES['darkly']['SECONDARY']}; color: white; padding: 10px 20px; border-radius: 8px;")
        details_hdr.addWidget(self.lbl_name, 1)
        details_hdr.addWidget(self.lbl_pass_badge)
        details_box.addLayout(details_hdr)
        
        self.lbl_company = QLabel("Awaiting attendee details...")
        self.lbl_company.setFont(QFont("Segoe UI", 18))
        self.lbl_company.setStyleSheet("color: gray;")
        self.lbl_company.setWordWrap(True)
        details_box.addWidget(self.lbl_company)
        details_box.addSpacing(30)
        
        grid = QGridLayout()
        grid.setSpacing(15)
        self.fields = {}
        row_col = [
            (0, 0, "📱 Mobile Number", "mobile"), (0, 1, "📍 Location", "location"), 
            (1, 0, "🏷️ Category", "category"), (1, 1, "👤 Gender", "gender"), 
            (2, 0, "📅 Event Date", "date"), (2, 1, "📡 Scanner ID", "scanner")
        ]
        for r, c, label, key in row_col:
            f = QFrame()
            f.setObjectName("Card")
            f_lyt = QVBoxLayout(f)
            l1 = QLabel(label.upper())
            l1.setStyleSheet("color: gray; font-weight: bold;")
            val = QLabel("---")
            val.setFont(QFont("Segoe UI", 16, QFont.Bold))
            val.setWordWrap(True)
            f_lyt.addWidget(l1)
            f_lyt.addWidget(val)
            grid.addWidget(f, r, c)
            self.fields[key] = val
            
        details_box.addLayout(grid)
        details_box.addStretch()
        profile_frame.addLayout(details_box, 1)
        left_lyt.addLayout(profile_frame, 1)
        
        self.bottom_banner = QLabel("READY FOR OPERATIONS")
        self.bottom_banner.setObjectName("BigBanner")
        self.bottom_banner.setAlignment(Qt.AlignCenter)
        self.bottom_banner.setStyleSheet(f"background-color: {THEMES['darkly']['SECONDARY']}; color: white;")
        left_lyt.addWidget(self.bottom_banner)

        # --- RIGHT PANEL (Controls & Recent) ---
        right_panel = QWidget()
        right_panel.setMinimumWidth(380) # Protect from over-shrinking
        right_lyt = QVBoxLayout(right_panel)
        right_lyt.setContentsMargins(15, 0, 0, 0)
        
        # Manual Entry
        lookup_card = QFrame()
        lookup_card.setObjectName("Card")
        lookup_lyt = QVBoxLayout(lookup_card)
        lookup_lyt.setContentsMargins(20, 20, 20, 20)
        
        lbl_lu = QLabel("🔍 Manual Entry")
        lbl_lu.setFont(QFont("Segoe UI", 12, QFont.Bold))
        lookup_lyt.addWidget(lbl_lu)
        lookup_lyt.addSpacing(10)
        
        self.ent_phone = QLineEdit()
        self.ent_phone.setPlaceholderText("Phone Number (e.g. 90000...)")
        self.ent_phone.returnPressed.connect(lambda: self.manual_scan('phone'))
        
        self.ent_id = QLineEdit()
        self.ent_id.setPlaceholderText("Attendee ID (e.g. TDE26...)")
        self.ent_id.returnPressed.connect(lambda: self.manual_scan('id'))
        
        btn_proc = QPushButton("PROCESS MANUAL SCAN")
        btn_proc.setStyleSheet(f"background-color: {THEMES['darkly']['SUCCESS']}; color: white; padding: 12px; font-size: 11pt;")
        btn_proc.clicked.connect(self.handle_manual_submit)
        
        lookup_lyt.addWidget(self.ent_phone)
        lookup_lyt.addWidget(self.ent_id)
        lookup_lyt.addSpacing(10)
        lookup_lyt.addWidget(btn_proc)
        right_lyt.addWidget(lookup_card)
        right_lyt.addSpacing(20)

        # Stats Grid
        stats_grid = QGridLayout()
        stats_grid.setSpacing(10)
        self.stat_labels = {}
        stat_items = [("Success", THEMES['darkly']['SUCCESS']), ("Duplicate", THEMES['darkly']['WARNING']), 
                      ("Wrong Day", THEMES['darkly']['SECONDARY']), ("Errors", THEMES['darkly']['DANGER'])]
        for idx, (title, color) in enumerate(stat_items):
            f = QFrame()
            f.setObjectName("Card")
            f_lyt = QVBoxLayout(f)
            val = QLabel("0")
            val.setFont(QFont("Segoe UI", 26, QFont.Bold))
            val.setStyleSheet(f"color: {color};")
            val.setAlignment(Qt.AlignCenter)
            lbl = QLabel(title.upper())
            lbl.setStyleSheet("color: gray; font-weight: bold; font-size: 9pt;")
            lbl.setAlignment(Qt.AlignCenter)
            f_lyt.addWidget(val)
            f_lyt.addWidget(lbl)
            stats_grid.addWidget(f, idx//2, idx%2)
            self.stat_labels[title] = val
        right_lyt.addLayout(stats_grid)
        right_lyt.addSpacing(20)

        # Recent Activity
        lbl_ra = QLabel("🕒 RECENT ACTIVITY")
        lbl_ra.setFont(QFont("Segoe UI", 12, QFont.Bold))
        lbl_ra.setStyleSheet(f"color: {THEMES['darkly']['PRIMARY']};")
        right_lyt.addWidget(lbl_ra)
        
        # Scroll area not necessarily needed since we limit to 5, but we ensure proper alignment.
        self.list_frame = QWidget()
        self.list_lyt = QVBoxLayout(self.list_frame)
        self.list_lyt.setContentsMargins(0, 0, 0, 0)
        self.list_lyt.setSpacing(8)
        self.list_lyt.setAlignment(Qt.AlignTop) # CRITICAL: Prevents stretching/overlapping
        right_lyt.addWidget(self.list_frame, 1) # 1 stretches to push it up

        self.splitter.addWidget(left_panel)
        self.splitter.addWidget(right_panel)
        self.splitter.setSizes([950, 450])
        main_layout.addWidget(self.splitter, 1)

    def handle_manual_submit(self):
        id_val = self.ent_id.text().strip()
        phone_val = self.ent_phone.text().strip()
        if id_val: self.manual_scan('id')
        elif phone_val: self.manual_scan('phone')

    def toggle_sound(self):
        self.notifier.sound_enabled = not self.notifier.sound_enabled
        if self.notifier.sound_enabled:
            self.btn_sound.setText("🔊 Sound")
            self.btn_sound.setStyleSheet(f"border: 1px solid {THEMES['darkly']['INFO']}; color: {THEMES['darkly']['INFO']}; background: transparent;")
        else:
            self.btn_sound.setText("🔇 Muted")
            self.btn_sound.setStyleSheet(f"border: 1px solid {THEMES['darkly']['SECONDARY']}; color: {THEMES['darkly']['SECONDARY']}; background: transparent;")

    def set_placeholder_photo(self):
        bg_color = "#e9ecef" if self.current_theme == "flatly" else "#222222"
        if bg_color not in self._placeholder_img_cache:
            img = QPixmap(340, 340)
            img.fill(QColor(bg_color))
            self._placeholder_img_cache[bg_color] = img
        self.current_photo = self._placeholder_img_cache[bg_color]
        self.lbl_photo.setPixmap(self.current_photo)

    def async_load_photo(self, attendee_id):
        if attendee_id in self._photo_cache:
            self.update_photo_ui(self._photo_cache[attendee_id])
            self._photo_cache.move_to_end(attendee_id)
            return

        def _load():
            rel_dir = self.config_manager.config.get("photo_directory", DEFAULT_PHOTO_DIR)
            abs_directory = os.path.normpath(os.path.join(BASE_DIR, rel_dir))
            photo_found = False
            for ext in ['.jpg', '.png', '.jpeg']:
                path = os.path.join(abs_directory, f"{attendee_id}{ext}")
                if os.path.exists(path):
                    try:
                        pixmap = QPixmap(path)
                        if not pixmap.isNull():
                            # Crop and scale properly
                            scaled = pixmap.scaled(340, 340, Qt.KeepAspectRatioByExpanding, Qt.SmoothTransformation)
                            crop_rect = scaled.rect()
                            crop_rect.moveCenter(scaled.rect().center())
                            cropped = scaled.copy(crop_rect.intersected(scaled.rect()))
                            
                            self._photo_cache[attendee_id] = cropped
                            if len(self._photo_cache) > 50: self._photo_cache.popitem(last=False)
                            self.gui_queue.put(lambda p=cropped: self.update_photo_ui(p))
                            photo_found = True
                            break
                    except Exception as e: logging.error(f"Photo error: {e}")
            if not photo_found:
                self.gui_queue.put(self.set_placeholder_photo)

        threading.Thread(target=_load, daemon=True).start()

    def update_photo_ui(self, photo_image):
        self.current_photo = photo_image  
        self.lbl_photo.setPixmap(self.current_photo)

    def process_queues(self):
        while not self.gui_queue.empty():
            try: self.gui_queue.get_nowait()()
            except queue.Empty: break
            except Exception as e: logging.error(f"GUI queue task failed: {e}")
            
        scans_to_process = []
        while not self.scan_queue.empty() and len(scans_to_process) < 5:
            try: scans_to_process.append(self.scan_queue.get_nowait())
            except queue.Empty: break
            
        for event in scans_to_process:
            self.update_ui_with_event(event)

    def trigger_banner_animation(self, c_style):
        t = THEMES[self.current_theme]
        flash_color = t['TEXT']
        original_color = t.get(c_style.upper(), t['SECONDARY'])
        
        self.status_banner.setStyleSheet(f"background-color: {flash_color}; color: {t['BG']}; border-radius: 8px;")
        QTimer.singleShot(150, lambda: self.status_banner.setStyleSheet(f"background-color: {original_color}; color: white; border-radius: 8px;"))

    def update_ui_with_event(self, event_data):
        status_type = event_data.get("status", "ERROR")
        message = event_data.get("message", "Unknown error")
        attendee = event_data.get("attendee")
        scanner_dev = event_data.get("device", "Unknown Scanner")
        raw_ts = event_data.get("timestamp", "")
        
        aid = attendee.get("attendee_id", "unknown") if attendee else "none"
        event_sig = f"{raw_ts}_{aid}_{status_type}"
        if event_sig in self._processed_sigs: return 
        self._processed_sigs.append(event_sig)

        time_str = datetime.now().strftime("%I:%M %p")
        if raw_ts:
            try:
                dt = datetime.fromisoformat(raw_ts.replace('Z', '+00:00'))
                time_str = dt.astimezone().strftime("%I:%M %p")
            except Exception: pass

        configs = {
            "SUCCESS": {"color": "SUCCESS", "banner": "✅ ACCESS GRANTED", "bottom": "SUCCESSFULLY CHECKED IN"},
            "DUPLICATE": {"color": "WARNING", "banner": "⚠️ ALREADY SCANNED", "bottom": "DUPLICATE SCAN DETECTED"},
            "ERROR": {"color": "DANGER", "banner": "❌ ACCESS DENIED", "bottom": message}
        }
        cfg = configs.get(status_type, configs["ERROR"])
        c_style = cfg["color"]
        
        t = THEMES[self.current_theme]
        banner_color = t.get(c_style, t['SECONDARY'])
        
        self.status_banner.setText(cfg["banner"])
        self.bottom_banner.setText(cfg["bottom"])
        self.bottom_banner.setStyleSheet(f"background-color: {banner_color}; color: white; border-radius: 8px;")
        self.trigger_banner_animation(c_style)
        self.notifier.queue.put({"status": status_type, "message": message})

        if attendee:
            category_raw = str(attendee.get("attendee_type", "")).lower()
            badge_style = CATEGORY_STYLES.get(category_raw, CATEGORY_STYLES["default"])
            badge_color = t.get(badge_style, t['SECONDARY'])
            
            self.lbl_pass_badge.setText(category_raw.upper() if category_raw else "UNKNOWN")
            self.lbl_pass_badge.setStyleSheet(f"background-color: {badge_color}; color: white; padding: 10px 20px; border-radius: 8px;")
            self.lbl_name.setText(attendee.get("full_name", "").upper())
            
            comp_color = t['INFO'] if c_style == "SUCCESS" else banner_color
            self.lbl_company.setText(attendee.get("business_name") or "General Admission")
            self.lbl_company.setStyleSheet(f"color: {comp_color}; font-weight: bold;")
            
            self.lbl_attendee_id.setText(attendee.get("attendee_id", ""))
            
            mobile = str(attendee.get("mobile", ""))
            masked_mobile = f"••••••{mobile[-4:]}" if len(mobile) >= 4 else mobile
            self.fields["mobile"].setText(masked_mobile)
            self.fields["location"].setText(f"{attendee.get('city', '')}, {attendee.get('state', '')}".strip(', '))
            self.fields["category"].setText(attendee.get("attendee_type", ""))
            self.fields["gender"].setText(attendee.get("gender", ""))
            self.fields["date"].setText(datetime.now().strftime("%d %B %Y"))
            self.fields["scanner"].setText(scanner_dev)
            
            self.async_load_photo(attendee.get("attendee_id"))
            self.add_recent_scan(attendee.get("full_name"), attendee.get("attendee_id"), c_style, time_str)
        else:
            self.lbl_name.setText("UNKNOWN RECORD")
            self.lbl_company.setText("---")
            self.lbl_company.setStyleSheet(f"color: {t['SECONDARY']};")
            self.lbl_attendee_id.setText("---")
            self.lbl_pass_badge.setText("N/A")
            self.lbl_pass_badge.setStyleSheet(f"background-color: {t['SECONDARY']}; color: white; padding: 10px 20px; border-radius: 8px;")
            for lbl in self.fields.values(): lbl.setText("---")
            self.set_placeholder_photo()

        if status_type in ["SUCCESS", "DUPLICATE", "ERROR"]:
            key = "Success" if status_type == "SUCCESS" else ("Duplicate" if status_type == "DUPLICATE" else "Errors")
            self.stats[key] += 1
            self.stat_labels[key].setText(str(self.stats[key]))

    def add_recent_scan(self, name, att_id, style, time_str):
        card = QFrame()
        card.setObjectName("RecentCard")
        # Ensure it maintains a strict height so text never overlaps natively.
        card.setMinimumHeight(65)
        card.setSizePolicy(QSizePolicy.Preferred, QSizePolicy.Fixed)
        
        t = THEMES[self.current_theme]
        bg_color = t.get(style, t['SECONDARY'])
        card.setStyleSheet(f"background-color: {bg_color}; color: white; border-radius: 6px;")
        
        lyt = QVBoxLayout(card)
        lyt.setContentsMargins(15, 8, 15, 8)
        
        top = QHBoxLayout()
        n = QLabel(f"👤 {name}")
        n.setFont(QFont("Segoe UI", 11, QFont.Bold))
        n.setStyleSheet("border: none; background: transparent;")
        tm = QLabel(time_str)
        tm.setStyleSheet("border: none; background: transparent; font-size: 9pt;")
        top.addWidget(n); top.addStretch(); top.addWidget(tm)
        lyt.addLayout(top)
        
        bot = QHBoxLayout()
        i = QLabel(att_id)
        i.setStyleSheet("border: none; background: transparent; font-size: 9pt;")
        st = QLabel("✓ OK" if style=="SUCCESS" else "⚠ WARN")
        st.setFont(QFont("Segoe UI", 10, QFont.Bold))
        st.setStyleSheet("border: none; background: transparent;")
        bot.addWidget(i); bot.addStretch(); bot.addWidget(st)
        lyt.addLayout(bot)
        
        self.list_lyt.insertWidget(0, card)
        self.recent_scans.insert(0, card)
        
        if len(self.recent_scans) > 5:
            old = self.recent_scans.pop()
            old.deleteLater()

    def show_hub_message(self, msg):
        if getattr(self, '_showing_msg', False): return
        self._showing_msg = True
        self.notifier.queue.put({"status": "ALERT", "message": ""})
        
        modal = QDialog(self)
        modal.setWindowTitle("Hub Alert")
        modal.setFixedSize(450, 300)
        
        lyt = QVBoxLayout(modal)
        frame = QFrame()
        frame.setStyleSheet(f"border: 3px solid {THEMES['darkly']['WARNING']}; background-color: {THEMES['darkly']['CARD_BG']}; border-radius: 6px;")
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

    def get_system_telemetry(self):
        battery_str = "N/A"
        temp_str = "N/A"
        if HAS_PSUTIL:
            try:
                batt = psutil.sensors_battery()
                if batt is not None: battery_str = f"{int(batt.percent)}%" + (" AC" if batt.power_plugged else "")
                else: battery_str = "AC Power (Desktop)"
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
                        if 0 <= celsius <= 120: temp_str = f"{celsius}°C"
                except Exception: pass
        return battery_str, temp_str

    def telemetry_loop(self):
        while self.is_polling:
            try:
                b, t = self.get_system_telemetry()
                self._cached_battery = b
                self._cached_temp = t
            except Exception: pass
            time.sleep(15)

    def start_threads(self):
        self.is_polling = False
        try:
            if self.stream_session: self.stream_session.close()
            if self.api_session: self.api_session.close()
        except Exception: pass
        
        self.stream_session = requests.Session()
        self.stream_session.headers.update({"User-Agent": "EventHub-GateDisplay-Stream/2.6", "Connection": "keep-alive"})
        self.api_session = requests.Session()
        self.api_session.headers.update({"User-Agent": "EventHub-GateDisplay-API/2.6", "Connection": "keep-alive"})
        
        self.is_polling = True
        threading.Thread(target=self.telemetry_loop, daemon=True).start()
        threading.Thread(target=self.listen_to_server_stream, daemon=True).start()
        threading.Thread(target=self.poll_server_status, daemon=True).start()

    def poll_server_status(self):
        while self.is_polling:
            try:
                hub_url = self.config_manager.config.get('hub_url', '').rstrip('/')
                payload = {
                    "device_id": self.config_manager.config.get("device_id"),
                    "device_name": self.config_manager.config.get("device_name"),
                    "page": "Gate Display",
                    "battery": getattr(self, '_cached_battery', 'N/A'),
                    "temp": getattr(self, '_cached_temp', 'N/A')
                }
                start_t = time.time()
                resp = self.api_session.post(f"{hub_url}/api/status", json=payload, timeout=3, verify=False)
                resp.raise_for_status()
                latency = int((time.time() - start_t) * 1000)
                data = resp.json()
                
                canonical = data.get("canonical_name")
                if canonical and canonical != self.config_manager.config.get("device_name") and canonical != "Unknown Device":
                    self.config_manager.config["device_name"] = canonical
                    self.config_manager.save()
                    self.gui_queue.put(lambda c=canonical: self.lbl_subtitle.setText(f"{c} • TDE UP 2026"))
                    
                msg = data.get("message")
                if msg: self.gui_queue.put(lambda m=msg: self.show_hub_message(m))
                    
                self.gui_queue.put(lambda l=latency, tm=data.get("test_mode", False), td=data.get("test_date", "Unknown"): (
                    self.update_net_pill(f"● Connected • {l}ms", THEMES[self.current_theme]["SUCCESS"] if l < 200 else THEMES[self.current_theme]["WARNING"]),
                    self.update_test_banner(tm, td)
                ))
            except Exception:
                self.gui_queue.put(lambda: (self.update_net_pill("● Offline / Timeout", THEMES[self.current_theme]["DANGER"]), self.update_test_banner(False, "")))
            time.sleep(3)

    def update_test_banner(self, is_test_mode, test_date):
        if is_test_mode:
            self.test_banner.setText(f"⚠️ TEST MODE ACTIVE (OVERRIDE: {test_date})")
            self.test_banner.show()
        else:
            self.test_banner.hide()

    def listen_to_server_stream(self):
        backoff = 1
        while self.is_polling:
            hub_url = self.config_manager.config.get('hub_url', '').rstrip('/')
            url = f"{hub_url}/api/stream-scans"
            try:
                with self.stream_session.get(url, stream=True, timeout=(5, 30), verify=False) as response:
                    if response.status_code == 200:
                        backoff = 1 
                        for line in response.iter_lines():
                            if not self.is_polling: break
                            if line:
                                decoded = line.decode('utf-8')
                                if decoded.startswith("data: "):
                                    try: self.scan_queue.put(json.loads(decoded[6:]))
                                    except Exception as e: logging.error(f"SSE JSON Error: {e}")
                    else: time.sleep(backoff)
            except Exception as e:
                logging.warning(f"Stream dropped. Reconnecting in {backoff}s. ({e})")
                time.sleep(backoff)
                backoff = min(backoff * 2, 10) 

    def update_net_pill(self, text, color):
        self.lbl_hub_status.setText(text)
        self.net_dot.setStyleSheet(f"color: {color}; font-size: 16px; border: none; background: transparent;")

    def manual_scan(self, lookup_type):
        current_time = time.time()
        if current_time - self._last_scan_time < 1.0: return 
        self._last_scan_time = current_time
        
        val = self.ent_phone.text().strip() if lookup_type == 'phone' else self.ent_id.text().strip()
        if not val: return
        
        url = f"{self.config_manager.config['hub_url'].rstrip('/')}/api/checkin"
        payload = {
            "attendee_id": val,
            "search_type": lookup_type,
            "device_name": self.config_manager.config["device_name"],
            "device_id": self.config_manager.config.get("device_id")
        }
        
        def _post_action():
            try:
                res = self.api_session.post(url, json=payload, timeout=5, verify=False)
                if res.status_code not in [200, 400, 403, 404]:
                    self.scan_queue.put({
                        "status": "ERROR", 
                        "message": f"Server reported {res.status_code}",
                        "attendee": None,
                        "timestamp": datetime.now().isoformat(),
                        "device": self.config_manager.config["device_name"]
                    })
            except Exception:
                self.scan_queue.put({
                    "status": "ERROR", 
                    "message": "Network timeout connecting to Hub.", 
                    "attendee": None,
                    "timestamp": datetime.now().isoformat(),
                    "device": self.config_manager.config["device_name"]
                })
        threading.Thread(target=_post_action, daemon=True).start()
        
        def reset_inputs():
            self.ent_id.clear()
            self.ent_phone.clear()
        self.gui_queue.put(reset_inputs)

    def open_settings(self):
        SettingsDialog(self, self.config_manager, self.on_settings_saved).exec()

    def on_settings_saved(self):
        self.lbl_subtitle.setText(f"{self.config_manager.config['device_name']} • TDE UP 2026")
        time.sleep(0.5)
        self.start_threads()

if __name__ == "__main__":
    if os.name == 'nt':
        try:
            my_app_id = os.environ.get("EVENTHUB_TOOL_ID", "EventHub.Tool.gate_display")
            ctypes.windll.shell32.SetCurrentProcessExplicitAppUserModelID(my_app_id)
        except Exception: pass
        
    # High DPI Scaling Setup for Crisp UI on 2K/4K Monitors
    if hasattr(Qt, 'AA_EnableHighDpiScaling'): QApplication.setAttribute(Qt.AA_EnableHighDpiScaling, True)
    if hasattr(Qt, 'AA_UseHighDpiPixmaps'): QApplication.setAttribute(Qt.AA_UseHighDpiPixmaps, True)
        
    app = QApplication(sys.argv)
    window = GateDisplay()
    window.show()
    sys.exit(app.exec())