#!/usr/bin/env python3
"""
EventHub Portable — Central Launcher (PySide6 Edition)
TENT DECOR EXPO UP 2026

Single entry point for the whole offline kit.
Auto-installs dependencies, verifies system health, captures tool logs, 
and manages tool processes with Voice & Audio feedback.
"""

from PySide6.QtWidgets import (QApplication, QMainWindow, QWidget, QVBoxLayout,
                               QHBoxLayout, QLabel, QPushButton, QFrame,
                               QTextEdit, QGridLayout, QMessageBox, QInputDialog, QSpacerItem, QSizePolicy)
import pymysql
from PySide6.QtGui import QIcon, QFont, QPixmap, QTextCursor, QColor
from PySide6.QtCore import Qt, QTimer, QSize
import os
import sys
import subprocess
import shutil
import queue
import threading
import urllib.request
import re
import ctypes
import winreg
import time
import json
import sqlite3
import platform
from datetime import datetime

# ==============================================================================
# BANNER SLIDESHOW IMAGES
# ==============================================================================
BANNER_IMAGES = [
    "eventhub-banner.png",
    "eventhub-banner0.png",
    "tdeup2025-team.png"
]

# ==============================================================================
# AUDIO & TTS IMPORTS
# ==============================================================================
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

# ==============================================================================
# AUTO-ADMINISTRATOR ELEVATION (WINDOWS) — STEALTH MODE
# ==============================================================================


def is_admin():
    try:
        return ctypes.windll.shell32.IsUserAnAdmin()
    except Exception:
        return False


if os.name == 'nt' and not is_admin():
    executable = sys.executable
    if executable.lower().endswith("python.exe"):
        executable = executable[:-10] + "pythonw.exe"

    ctypes.windll.shell32.ShellExecuteW(
        None, "runas", executable, " ".join(
            [f'"{arg}"' for arg in sys.argv]), None, 1
    )
    sys.exit()

# ==============================================================================
# 24/7 STABILITY: GLOBAL CRASH HANDLER
# ==============================================================================


def global_exception_handler(exc_type, exc_value, exc_traceback):
    print(
        f"Uncaught GUI Exception intercepted. App remains running: {exc_value}")


sys.excepthook = global_exception_handler

# ==============================================================================
# FIRST-RUN BOOTSTRAP (PYSIDE6)
# ==============================================================================
ROOT_DIR = os.path.dirname(os.path.abspath(__file__))
REQUIREMENTS_FILE = os.path.join(ROOT_DIR, "requirements.txt")


def _bootstrap_first_run():
    try:
        import PySide6
        import pymysql
        return
    except ImportError:
        pass

    if not os.path.isfile(REQUIREMENTS_FILE):
        sys.exit(1)

    flags = subprocess.CREATE_NO_WINDOW if os.name == 'nt' else 0
    subprocess.call(
        [sys.executable, "-m", "pip", "install", "-r",
            REQUIREMENTS_FILE, "--disable-pip-version-check"],
        cwd=ROOT_DIR,
        creationflags=flags,
        stdout=subprocess.DEVNULL,
        stderr=subprocess.DEVNULL
    )

    executable = sys.executable
    if os.name == 'nt' and executable.lower().endswith("python.exe"):
        executable = executable[:-10] + "pythonw.exe"

    sys.exit(subprocess.call([executable, os.path.abspath(
        __file__)] + sys.argv[1:], cwd=ROOT_DIR, creationflags=flags))


_bootstrap_first_run()


# ==============================================================================
# PATHS & CONFIG
# ==============================================================================
APP_DIR = os.path.join(ROOT_DIR, "app")
ASSETS_DIR = os.path.join(APP_DIR, "assets")
BANNER_DIR = os.path.join(ASSETS_DIR, "main-banner's")
ICON_PATH = os.path.join(ASSETS_DIR, "EventHub.ico")
CONFIG_DIR = os.path.join(APP_DIR, "config")
SCHEMA_CONFIG = os.path.join(CONFIG_DIR, "schema.json")
SECRETS_CONFIG = os.path.join(CONFIG_DIR, "secrets.json")
EXE_DIR = os.path.join(ROOT_DIR, "exe-files")

MIN_PYTHON = (3, 9)
MAX_LOG_LINES = 2000

# ==============================================================================
# ENVIRONMENT INJECTION (PERMANENT)
# ==============================================================================


def inject_cloudflared_path():
    cf_paths = [r"C:\Program Files\cloudflared",
                r"C:\Program Files (x86)\cloudflared"]
    current_path = os.environ.get("PATH", "")
    valid_path = None
    for path in cf_paths:
        if os.path.exists(path):
            valid_path = path
            if path not in current_path:
                os.environ["PATH"] = path + os.pathsep + os.environ["PATH"]
            break

    if valid_path and os.name == 'nt':
        try:
            key = winreg.OpenKey(
                winreg.HKEY_LOCAL_MACHINE, r"SYSTEM\CurrentControlSet\Control\Session Manager\Environment", 0, winreg.KEY_READ | winreg.KEY_WRITE)
            sys_path, _ = winreg.QueryValueEx(key, "Path")

            if valid_path.lower() not in sys_path.lower():
                new_path = valid_path + os.pathsep + sys_path
                winreg.SetValueEx(
                    key, "Path", 0, winreg.REG_EXPAND_SZ, new_path)
                HWND_BROADCAST = 0xFFFF
                WM_SETTINGCHANGE = 0x001A
                SMTO_ABORTIFHUNG = 0x0002
                ctypes.windll.user32.SendMessageTimeoutW(
                    HWND_BROADCAST, WM_SETTINGCHANGE, 0, "Environment", SMTO_ABORTIFHUNG, 5000, None)
            winreg.CloseKey(key)
        except Exception:
            pass


# ==============================================================================
# TOOL REGISTRY
# ==============================================================================
TOOLS = [
    {"key": "hub", "icon": "🖥️", "label": "Command Center", "script": "server_hub.py",
        "desc": "Central event control and server management.", "bootstyle": "primary"},
    {"key": "gate_display", "icon": "📺", "label": "Gate Display Terminal", "script": "check_in.py",
        "desc": "Live attendee check-in and access monitoring.", "bootstyle": "info"},
    {"key": "kiosk", "icon": "📝", "label": "Registration Kiosk", "script": "register.py",
        "desc": "On-site attendee registration and check-in.", "bootstyle": "success"},
    {"key": "sync", "icon": "🔄", "label": "Sync Manager", "script": "sync_manager.py",
        "desc": "Synchronizes attendee and event data across services.", "bootstyle": "warning"},
    {"key": "photos", "icon": "📸", "label": "Photo Downloader", "script": "photo_down.py",
        "desc": "Downloads and manages attendee photos for offline use.", "bootstyle": "secondary"},
    {"key": "explorer", "icon": "🔎", "label": "Attendee Explorer", "script": "explorer.py",
        "desc": "Search, view, and manage attendee profiles and records.", "bootstyle": "secondary"},
    {"key": "handbook", "icon": "📖", "label": "Digital Handbook", "script": "handbook.py",
        "desc": "Event operations, setup, and troubleshooting guide.", "bootstyle": "primary"},
    {"key": "stress_test", "icon": "⚡", "label": "Load & Stress Test", "script": "stress_test.py",
        "desc": "Tests system performance and stability under heavy load.", "bootstyle": "danger"},
]

# ==============================================================================
# PYSIDE6 GUI APPLICATION
# ==============================================================================


class BannerLabel(QLabel):
    """Custom QLabel to handle smooth resizing of the slideshow banner."""

    def __init__(self, parent=None):
        super().__init__(parent)
        self.setAlignment(Qt.AlignCenter)
        self._pixmap = None

    def setOriginalPixmap(self, pixmap):
        self._pixmap = pixmap
        self.update_pixmap()

    def resizeEvent(self, event):
        super().resizeEvent(event)
        self.update_pixmap()

    def update_pixmap(self):
        if self._pixmap and not self._pixmap.isNull():
            w = self.width() - 4
            h = min(320, self.height() - 4)
            if w > 10 and h > 10:
                scaled = self._pixmap.scaled(
                    w, h, Qt.KeepAspectRatio, Qt.SmoothTransformation)
                self.setPixmap(scaled)


class LauncherApp(QMainWindow):
    def __init__(self):
        super().__init__()
        self.setWindowTitle("EventHub Portable — Central Launcher")

        screen = QApplication.primaryScreen().geometry()
        sw, sh = screen.width(), screen.height()
        ww, wh = max(1100, min(1400, int(sw * 0.85))
                     ), max(750, min(1000, int(sh * 0.85)))
        self.setGeometry(max(0, (sw - ww) // 2),
                         max(0, (sh - wh) // 2 - 15), ww, wh)
        self.setMinimumSize(1100, 780)

        if os.path.exists(ICON_PATH):
            self.setWindowIcon(QIcon(ICON_PATH))

        inject_cloudflared_path()

        # Core state
        self.gui_queue = queue.Queue()
        self.processes = {}
        self.tool_widgets = {}
        self.health_widgets = {}
        self.cached_cf_path = None
        self.sound_enabled = True

        # Slideshow state
        self.slideshow_images = []
        self.current_image_index = 0
        self.slideshow_interval = 4000

        self.apply_stylesheet()
        self.build_ui()

        # Queue Polling Timer
        self.queue_timer = QTimer(self)
        self.queue_timer.timeout.connect(self._process_gui_queue)
        self.queue_timer.start(30)

        # Process Polling Timer
        self.process_timer = QTimer(self)
        self.process_timer.timeout.connect(self._poll_processes)
        self.process_timer.start(2000)

        # Staggered startup
        QTimer.singleShot(500, self.check_system_health)
        QTimer.singleShot(800, self._run_schema_script_async)
        QTimer.singleShot(1000, self._setup_network_firewall_async)

        self.log(
            "System initialized with Administrator Privileges. Ready for operations.",
            "SUCCESS",
            speak_text="Central Launcher Initialized. System ready for operations."
        )

    def apply_stylesheet(self):
        self.setStyleSheet("""
            QMainWindow { background-color: #222222; color: #ffffff; }
            QWidget { font-family: 'Segoe UI', Arial, sans-serif; }
            QLabel { color: #dddddd; }
            QFrame.Card { background-color: #2b2b2b; border: 1px solid #3d3d3d; border-radius: 6px; }
            QFrame.Card QLabel { border: none; }
            QPushButton { border-radius: 4px; font-weight: bold; padding: 6px 12px; }
            QPushButton.Outline { background-color: transparent; border: 1px solid #555; color: #ccc; }
            QPushButton.Outline:hover { background-color: #444; }
            QTextEdit { background-color: #141414; color: #cccccc; font-family: 'Consolas', monospace; font-size: 11pt; border: 1px solid #3d3d3d; border-radius: 6px; }
            QScrollBar:vertical { background: #222; width: 12px; margin: 0px; }
            QScrollBar::handle:vertical { background: #555; min-height: 20px; border-radius: 6px; }
            QScrollBar::add-line:vertical, QScrollBar::sub-line:vertical { height: 0px; }
        """)

    def _get_button_style(self, style_name):
        styles = {
            "primary": "background-color: #3498db; color: white; border: none;",
            "success": "background-color: #2ecc71; color: white; border: none;",
            "info": "background-color: #1abc9c; color: white; border: none;",
            "warning": "background-color: #f39c12; color: white; border: none;",
            "danger": "background-color: #e74c3c; color: white; border: none;",
            "secondary": "background-color: #555555; color: white; border: none;"
        }
        hover_styles = {
            "primary": "QPushButton:hover { background-color: #2980b9; }",
            "success": "QPushButton:hover { background-color: #27ae60; }",
            "info": "QPushButton:hover { background-color: #16a085; }",
            "warning": "QPushButton:hover { background-color: #d35400; }",
            "danger": "QPushButton:hover { background-color: #c0392b; }",
            "secondary": "QPushButton:hover { background-color: #444444; }"
        }
        base = styles.get(style_name, styles["secondary"])
        hover = hover_styles.get(style_name, hover_styles["secondary"])
        return f"QPushButton {{ {base} }} {hover}"

    def _get_color_hex(self, style_name):
        return {"success": "#2ecc71", "info": "#1abc9c", "warning": "#f39c12", "danger": "#e74c3c", "secondary": "#888888"}.get(style_name, "#757575")

    # --------------------------------------------------------------------------
    # TTS & AUDIO ENGINE
    # --------------------------------------------------------------------------
    def play_sound(self, status, speak_text=""):
        if not self.sound_enabled:
            return

        def _play():
            if HAS_WINSOUND:
                try:
                    if status == "SUCCESS":
                        winsound.Beep(2000, 100)
                    elif status == "WARNING":
                        winsound.Beep(1000, 100)
                        time.sleep(0.05)
                        winsound.Beep(1000, 100)
                    else:
                        winsound.Beep(400, 150)
                        winsound.Beep(300, 300)
                except Exception:
                    QApplication.beep()
            else:
                QApplication.beep()
                if status != "SUCCESS":
                    time.sleep(0.2)
                    QApplication.beep()

            if speak_text:
                try:
                    if platform.system() == "Windows":
                        safe_text = speak_text.replace("'", "")
                        ps_script = f"Add-Type -AssemblyName System.Speech; $synth = New-Object System.Speech.Synthesis.SpeechSynthesizer; $synth.SelectVoiceByHints([System.Speech.Synthesis.VoiceGender]::Female); $synth.Rate = 0; $synth.Speak('{safe_text}');"
                        subprocess.run(
                            ["powershell", "-Command", ps_script], creationflags=subprocess.CREATE_NO_WINDOW)
                    elif HAS_TTS:
                        engine = pyttsx3.init()
                        for voice in engine.getProperty('voices'):
                            if any(x in voice.name.lower() for x in ['female', 'zira', 'samantha']):
                                engine.setProperty('voice', voice.id)
                                break
                        engine.say(speak_text)
                        engine.runAndWait()
                except Exception as e:
                    self.gui_queue.put(
                        ("log", {"msg": f"TTS Error: {e}", "level": "ERROR"}))

        threading.Thread(target=_play, daemon=True).start()

    def toggle_sound(self):
        self.sound_enabled = not self.sound_enabled
        if self.sound_enabled:
            self.btn_sound.setText("🔊 Voice Enabled")
            self.btn_sound.setStyleSheet(
                "QPushButton { border: 1px solid #2ecc71; color: #2ecc71; background: transparent; }")
            self.play_sound("SUCCESS", "Audio alerts enabled.")
        else:
            self.btn_sound.setText("🔇 Muted")
            self.btn_sound.setStyleSheet(
                "QPushButton { border: 1px solid #555; color: #888; background: transparent; }")

    # --------------------------------------------------------------------------
    # UI CONSTRUCTION
    # --------------------------------------------------------------------------
    def build_ui(self):
        main_widget = QWidget()
        self.setCentralWidget(main_widget)
        main_layout = QVBoxLayout(main_widget)
        main_layout.setContentsMargins(25, 25, 25, 25)

        # -- Header --
        header_layout = QHBoxLayout()
        main_layout.addLayout(header_layout)

        title_layout = QVBoxLayout()
        title_lbl = QLabel("EventHub Portable")
        title_lbl.setStyleSheet(
            "font-size: 26px; font-weight: bold; color: #3498db;")
        sub_lbl = QLabel(
            "Central Launcher • Engineered for Event Resilience • Powered by EllowDigital")
        sub_lbl.setStyleSheet(
            "font-size: 10px; font-weight: bold; color: #888;")
        title_layout.addWidget(title_lbl)
        title_layout.addWidget(sub_lbl)
        header_layout.addLayout(title_layout)
        header_layout.addStretch()

        self.btn_sound = QPushButton("🔊 Voice Enabled")
        self.btn_sound.setStyleSheet(
            "QPushButton { border: 1px solid #2ecc71; color: #2ecc71; background: transparent; }")
        self.btn_sound.clicked.connect(self.toggle_sound)
        btn_refresh = QPushButton("⟳ Refresh Health Check")
        btn_refresh.setProperty("class", "Outline")
        btn_refresh.clicked.connect(self.check_system_health)
        btn_stop = QPushButton("🛑 Stop All Active Tools")
        btn_stop.setStyleSheet(self._get_button_style("danger"))
        btn_stop.clicked.connect(self.stop_all_tools)

        header_layout.addWidget(self.btn_sound)
        header_layout.addWidget(btn_refresh)
        header_layout.addWidget(btn_stop)

        # -- System Health --
        lbl_health = QLabel("⚙️ SYSTEM HEALTH")
        lbl_health.setStyleSheet(
            "font-size: 11px; font-weight: bold; color: #888;")
        main_layout.addWidget(lbl_health)

        health_grid = QGridLayout()
        main_layout.addLayout(health_grid)
        self._build_health_card(health_grid, 0, "python",
                                "🐍 PYTHON VER", "Checking...")
        self._build_health_card(
            health_grid, 1, "cloudflared", "☁️ CLOUDFLARED", "Checking...")
        self._build_health_card(health_grid, 2, "deps",
                                "📦 DEPENDENCIES", "Checking...")
        self._build_health_card(health_grid, 3, "config",
                                "⚙️ CONFIGURATION", "Checking...")

        # -- Main Split --
        split_layout = QHBoxLayout()
        main_layout.addLayout(split_layout, stretch=1)

        # Left Column (Tools)
        left_col = QVBoxLayout()
        split_layout.addLayout(left_col, stretch=4)

        lbl_db = QLabel("🗄️ DATABASE HEALTH")
        lbl_db.setStyleSheet(
            "font-size: 11px; font-weight: bold; color: #888;")
        left_col.addWidget(lbl_db)

        db_grid = QGridLayout()
        left_col.addLayout(db_grid)
        self._build_health_card(db_grid, 0, "mysql_db",
                                "🐬 MYSQL (PRIMARY)", "Checking...")
        self._build_health_card(db_grid, 1, "sqlite_db",
                                "💾 SQLITE (MIRROR)", "Checking...")

        lbl_tools = QLabel("🛠️ APPLICATION TOOLS")
        lbl_tools.setStyleSheet(
            "font-size: 11px; font-weight: bold; color: #888; margin-top: 10px;")
        left_col.addWidget(lbl_tools)

        for tool in TOOLS:
            self._build_tool_card(left_col, tool)

        left_col.addStretch()

        btn_row = QHBoxLayout()
        btn_root = QPushButton("📁 Open Root Folder")
        btn_root.setProperty("class", "Outline")
        btn_root.clicked.connect(lambda: self.open_folder(ROOT_DIR))
        btn_conf = QPushButton("⚙️ Open Configs")
        btn_conf.setProperty("class", "Outline")
        btn_conf.clicked.connect(lambda: self.open_folder(CONFIG_DIR))
        btn_row.addWidget(btn_root)
        btn_row.addWidget(btn_conf)
        left_col.addLayout(btn_row)

        # Right Column (Banner & Logs)
        right_col = QVBoxLayout()
        split_layout.addLayout(right_col, stretch=6)

        self.team_card = QFrame()
        self.team_card.setProperty("class", "Card")
        team_layout = QVBoxLayout(self.team_card)
        team_layout.setContentsMargins(2, 2, 2, 2)

        self.lbl_team_photo = BannerLabel()
        team_layout.addWidget(self.lbl_team_photo)
        right_col.addWidget(self.team_card, stretch=4)

        # Load Slideshow
        for img_name in BANNER_IMAGES:
            img_path = os.path.join(BANNER_DIR, img_name)
            if os.path.exists(img_path):
                try:
                    self.slideshow_images.append(QPixmap(img_path))
                except Exception as e:
                    self.log(f"Could not load '{img_name}': {e}", "WARNING")

        if self.slideshow_images:
            self.lbl_team_photo.setOriginalPixmap(self.slideshow_images[0])
            if len(self.slideshow_images) > 1:
                self.slideshow_timer = QTimer(self)
                self.slideshow_timer.timeout.connect(self._next_slide)
                self.slideshow_timer.start(self.slideshow_interval)
        else:
            self.lbl_team_photo.setText(f"📸 Place images in:\n{BANNER_DIR}")

        log_hdr_layout = QHBoxLayout()
        lbl_log = QLabel("📟 ACTIVITY LOG (STDOUT)")
        lbl_log.setStyleSheet(
            "font-size: 11px; font-weight: bold; color: #888;")
        btn_clear = QPushButton("Clear Logs")
        btn_clear.setStyleSheet(
            "background: transparent; color: #3498db; text-decoration: underline; border: none;")
        btn_clear.clicked.connect(self.clear_log)
        log_hdr_layout.addWidget(lbl_log)
        log_hdr_layout.addStretch()
        log_hdr_layout.addWidget(btn_clear)
        right_col.addLayout(log_hdr_layout)

        self.log_box = QTextEdit()
        self.log_box.setReadOnly(True)
        right_col.addWidget(self.log_box, stretch=6)

    def _next_slide(self):
        if not self.slideshow_images:
            return
        self.current_image_index = (
            self.current_image_index + 1) % len(self.slideshow_images)
        self.lbl_team_photo.setOriginalPixmap(
            self.slideshow_images[self.current_image_index])

    def _build_health_card(self, parent_layout, column, key, title, initial_val):
        card = QFrame()
        card.setProperty("class", "Card")
        layout = QVBoxLayout(card)
        layout.setContentsMargins(12, 12, 12, 12)

        top = QHBoxLayout()
        dot = QLabel()
        dot.setFixedSize(10, 10)
        dot.setStyleSheet("background-color: #757575; border-radius: 5px;")
        top.addWidget(dot)

        lbl_title = QLabel(title)
        lbl_title.setStyleSheet(
            "font-size: 10px; font-weight: bold; color: #888;")
        top.addWidget(lbl_title)
        top.addStretch()
        layout.addLayout(top)

        val_lbl = QLabel(initial_val)
        val_lbl.setStyleSheet(
            "font-size: 14px; font-weight: bold; color: #aaa;")
        layout.addWidget(val_lbl)

        parent_layout.addWidget(card, 0, column)
        self.health_widgets[key] = {"label": val_lbl, "dot": dot}

    def _build_tool_card(self, parent_layout, tool):
        card = QFrame()
        card.setProperty("class", "Card")
        layout = QHBoxLayout(card)
        layout.setContentsMargins(14, 10, 14, 10)

        icon_lbl = QLabel(tool["icon"])
        icon_lbl.setStyleSheet("font-size: 24px;")
        layout.addWidget(icon_lbl)

        text_layout = QVBoxLayout()
        lbl_title = QLabel(tool["label"])
        lbl_title.setStyleSheet(
            "font-size: 13px; font-weight: bold; color: #ddd;")
        lbl_desc = QLabel(tool["desc"])
        lbl_desc.setStyleSheet("font-size: 11px; color: #888;")
        text_layout.addWidget(lbl_title)
        text_layout.addWidget(lbl_desc)
        layout.addLayout(text_layout, stretch=1)

        status_lbl = QLabel("⚫ IDLE")
        status_lbl.setStyleSheet(
            "font-size: 11px; font-weight: bold; color: #888;")
        status_lbl.setFixedWidth(80)
        status_lbl.setAlignment(Qt.AlignCenter)
        layout.addWidget(status_lbl)

        btn = QPushButton("Launch Tool")
        btn.setStyleSheet(self._get_button_style(tool["bootstyle"]))
        btn.setFixedWidth(120)
        btn.clicked.connect(lambda checked=False, t=tool: self.launch_tool(t))
        layout.addWidget(btn)

        parent_layout.addWidget(card)
        self.tool_widgets[tool["key"]] = {"button": btn, "status": status_lbl}

    # --------------------------------------------------------------------------
    # THREAD-SAFE UI UPDATES & LOGGING
    # --------------------------------------------------------------------------
    def log(self, message, level="INFO", speak_text=None):
        self.gui_queue.put(("log", {"msg": message, "level": level}))
        if speak_text:
            self.play_sound(level, speak_text)

    def _process_gui_queue(self):
        for _ in range(50):
            try:
                kind, payload = self.gui_queue.get_nowait()

                if kind == "log":
                    self._append_log(payload["msg"], payload["level"])
                elif kind == "update_health":
                    self._update_health_ui(
                        payload["key"], payload["text"], payload["style"])
                elif kind == "prompt_cf_token":
                    self._prompt_and_install_cf_service()
                elif kind == "python_done":
                    QMessageBox.information(
                        self, "Python Update Complete", "Python updated. Please restart.")
            except queue.Empty:
                break
            except Exception:
                pass

    def _update_health_ui(self, key, text, style):
        w = self.health_widgets.get(key)
        if not w:
            return

        color = self._get_color_hex(style)
        w["label"].setText(text)
        w["label"].setStyleSheet(
            "color: white; font-size: 14px; font-weight: bold;")
        QTimer.singleShot(150, lambda: w["label"].setStyleSheet(
            f"color: {color}; font-size: 14px; font-weight: bold;") if w["label"] else None)

        w["dot"].setStyleSheet("background-color: white; border-radius: 5px;")
        QTimer.singleShot(150, lambda: w["dot"].setStyleSheet(
            f"background-color: {color}; border-radius: 5px;") if w["dot"] else None)

    def _append_log(self, message, level):
        timestamp = datetime.now().strftime("%H:%M:%S")
        colors = {"INFO": "#cccccc", "SUCCESS": "#2ecc71",
                  "WARNING": "#f39c12", "ERROR": "#e74c3c", "TOOL": "#3498db"}
        color = colors.get(level, "#ffffff")
        bold = "font-weight: bold;" if level in [
            "SUCCESS", "WARNING", "ERROR"] else ""

        html = f'<span style="color: {color}; {bold}">[{timestamp}] {message}</span>'
        self.log_box.append(html)

        doc = self.log_box.document()
        if doc.blockCount() > MAX_LOG_LINES:
            cursor = QTextCursor(doc)
            cursor.movePosition(QTextCursor.Start)
            for _ in range(doc.blockCount() - MAX_LOG_LINES):
                cursor.movePosition(QTextCursor.Down, QTextCursor.KeepAnchor)
            cursor.removeSelectedText()

    def clear_log(self):
        self.log_box.clear()

    # --------------------------------------------------------------------------
    # ASYNC INIT & SYSTEM CHECKS
    # --------------------------------------------------------------------------
    def _run_schema_script_async(self):
        threading.Thread(target=self._schema_task, daemon=True).start()

    def _schema_task(self):
        schema_script = os.path.join(APP_DIR, "schema.py")
        if os.path.exists(schema_script):
            self.log("Initializing database schema (app/schema.py)...", "INFO")
            try:
                flags = subprocess.CREATE_NO_WINDOW if os.name == 'nt' else 0
                res = subprocess.run([sys.executable, schema_script],
                                     capture_output=True, text=True, cwd=APP_DIR, creationflags=flags)
                if res.returncode == 0:
                    self.log(
                        "Schema initialization completed successfully.", "SUCCESS")
                else:
                    self.log(
                        f"Schema initialization failed: {res.stderr.strip() or res.stdout.strip()}", "ERROR")
            except Exception as e:
                self.log(f"Error running schema script: {e}", "ERROR")

    def _setup_network_firewall_async(self):
        threading.Thread(target=self._network_firewall_task,
                         daemon=True).start()

    def _network_firewall_task(self):
        if os.name != "nt":
            return
        flags = subprocess.CREATE_NO_WINDOW
        self.log("Verifying EventHub firewall configurations...", "INFO")
        ps_fw_cmd = ("$rule = Get-NetFirewallRule -DisplayName 'EventHub Ports' -ErrorAction SilentlyContinue; "
                     "if (-not $rule) { New-NetFirewallRule -DisplayName 'EventHub Ports' -Direction Inbound -Action Allow -Protocol TCP -LocalPort 5000,5001 | Out-Null; Write-Output 'CREATED' } else { Write-Output 'EXISTS' }")
        try:
            res = subprocess.run(["powershell", "-Command", ps_fw_cmd],
                                 capture_output=True, text=True, creationflags=flags)
            if "CREATED" in res.stdout:
                self.log(
                    "Added inbound firewall rule for ports 5000, 5001.", "SUCCESS")
        except Exception as e:
            self.log(f"Failed to configure firewall: {e}", "ERROR")

        try:
            subprocess.run(["ipconfig", "/flushdns"],
                           capture_output=True, creationflags=flags)
            subprocess.run(["ipconfig", "/renew"],
                           capture_output=True, creationflags=flags)
            self.log("Network configuration reset successfully.", "SUCCESS")
        except Exception as e:
            self.log(f"Failed to reset network: {e}", "ERROR")

    def _set_health(self, key, text, style):
        self.gui_queue.put(
            ("update_health", {"key": key, "text": text, "style": style}))

    def check_system_health(self):
        self.log("Running system health checks...", "INFO")
        py_ver = f"{sys.version_info.major}.{sys.version_info.minor}.{sys.version_info.micro}"
        if sys.version_info[:2] < MIN_PYTHON:
            self._set_health("python", f"v{py_ver} ⚠", "warning")
            self._offer_python_install()
        else:
            self._set_health("python", f"v{py_ver} ✓", "success")

        if os.path.isfile(SCHEMA_CONFIG) and os.path.isfile(SECRETS_CONFIG):
            self._set_health("config", "Valid ✓", "success")
        else:
            self._set_health("config", "Missing ⚠", "warning")

        self._set_health("deps", "Verifying...", "warning")
        threading.Thread(
            target=self._install_requirements_thread, daemon=True).start()

        self._set_health("cloudflared", "Verifying...", "warning")
        threading.Thread(
            target=self._verify_cloudflared_thread, daemon=True).start()

        self._set_health("mysql_db", "Pinging...", "warning")
        self._set_health("sqlite_db", "Pinging...", "warning")
        threading.Thread(target=self._verify_databases_thread,
                         daemon=True).start()

    def _verify_databases_thread(self):
        if not os.path.exists(SCHEMA_CONFIG):
            self._set_health("mysql_db", "Missing Config", "warning")
            self._set_health("sqlite_db", "Missing Config", "warning")
            return
        try:
            with open(SCHEMA_CONFIG, 'r') as f:
                config = json.load(f)
        except Exception:
            self._set_health("mysql_db", "Invalid JSON", "danger")
            self._set_health("sqlite_db", "Invalid JSON", "danger")
            return

        my_conf = config.get("mysql", {})
        if my_conf.get("enabled"):
            try:
                start_t = time.perf_counter()
                conn = pymysql.connect(host=my_conf.get("host", "localhost"), user=my_conf.get("user", "root"), password=my_conf.get(
                    "password", ""), database=my_conf.get("database", "eventhub_db"), port=my_conf.get("port", 3306), connect_timeout=2)
                conn.ping(reconnect=False)
                conn.close()
                ms = int((time.perf_counter() - start_t) * 1000)
                self._set_health(
                    "mysql_db", f"Online ✓ ({ms}ms)", "success" if ms < 100 else "warning")
            except Exception:
                self._set_health("mysql_db", "Offline ⚠", "danger")
        else:
            self._set_health("mysql_db", "Disabled", "secondary")

        sq_conf = config.get("sqlite", {})
        if sq_conf.get("enabled"):
            db_file = os.path.join(APP_DIR, sq_conf.get(
                "folder_name", "db"), sq_conf.get("file_name", "eventhub_local.db"))
            if os.path.exists(db_file):
                try:
                    start_t = time.perf_counter()
                    conn = sqlite3.connect(db_file)
                    conn.execute("SELECT name FROM sqlite_master;")
                    conn.close()
                    ms = int((time.perf_counter() - start_t) * 1000)
                    self._set_health(
                        "sqlite_db", f"Ready ✓ (<1ms)" if ms == 0 else f"Ready ✓ ({ms}ms)", "success")
                except Exception:
                    self._set_health("sqlite_db", "Corrupted ⚠", "danger")
            else:
                self._set_health("sqlite_db", "Missing DB ⚠", "warning")
        else:
            self._set_health("sqlite_db", "Disabled", "secondary")

    def _install_requirements_thread(self):
        try:
            flags = subprocess.CREATE_NO_WINDOW if os.name == "nt" else 0
            proc = subprocess.run([sys.executable, "-m", "pip", "install", "-r", REQUIREMENTS_FILE,
                                  "--disable-pip-version-check"], cwd=ROOT_DIR, capture_output=True, text=True, creationflags=flags)
            if proc.returncode == 0:
                self.log("Python dependencies verified.", "SUCCESS")
                self._set_health("deps", "Ready ✓", "success")
            else:
                self.log(f"Dependency error: {proc.stderr}", "ERROR")
                self._set_health("deps", "Failed ⚠", "danger")
        except Exception as e:
            self.log(f"Failed to check dependencies: {e}", "ERROR")
            self._set_health("deps", "Error ⚠", "danger")

    # --------------------------------------------------------------------------
    # CLOUDFLARED & PYTHON UPDATERS
    # --------------------------------------------------------------------------
    def _get_cloudflared_path(self):
        if self.cached_cf_path:
            return self.cached_cf_path
        if shutil.which("cloudflared"):
            return "cloudflared"
        for base in [os.environ.get("ProgramFiles", "C:\\Program Files"), os.environ.get("ProgramFiles(x86)", "C:\\Program Files (x86)")]:
            guess = os.path.join(base, "cloudflared", "cloudflared.exe")
            if os.path.exists(guess):
                return "cloudflared"
        return None

    def _verify_cloudflared_thread(self):
        cf_exe = self._get_cloudflared_path()
        if not cf_exe:
            self._set_health("cloudflared", "Missing ⚠", "warning")
            QTimer.singleShot(0, self._offer_cloudflared_install)
            return
        try:
            flags = subprocess.CREATE_NO_WINDOW if os.name == "nt" else 0
            res = subprocess.run(
                [cf_exe, "--version"], capture_output=True, text=True, timeout=3, creationflags=flags)
            if res.returncode == 0:
                match = re.search(r"version\s+(\d+\.\d+\.\d+)", res.stdout)
                self._set_health(
                    "cloudflared", f"v{match.group(1)} ✓" if match else "OK", "success")
            else:
                self._set_health("cloudflared", "Broken ⚠", "danger")
        except Exception:
            self._set_health("cloudflared", "Broken ⚠", "danger")

    def _offer_cloudflared_install(self):
        if QMessageBox.question(self, "Cloudflared Missing", "Cloudflared is required. Download and install it now?") == QMessageBox.Yes:
            threading.Thread(
                target=self._download_and_install_cloudflared, daemon=True).start()

    def _download_and_install_cloudflared(self):
        os.makedirs(EXE_DIR, exist_ok=True)
        msi_path = os.path.join(EXE_DIR, "cloudflared-windows-amd64.msi")
        try:
            self.log("Downloading Cloudflared... please wait.", "INFO")
            urllib.request.urlretrieve(
                "https://github.com/cloudflare/cloudflared/releases/latest/download/cloudflared-windows-amd64.msi", msi_path)
            self.log("Installing Cloudflared completely silently...", "WARNING")
            flags = subprocess.CREATE_NO_WINDOW if os.name == "nt" else 0
            subprocess.run(["msiexec.exe", "/i", msi_path, "/quiet",
                           "/norestart"], check=True, creationflags=flags)
            self.log("Cloudflared installation successful.", "SUCCESS",
                     speak_text="Cloudflared installed successfully.")
            inject_cloudflared_path()
            try:
                os.remove(msi_path)
            except Exception:
                pass
            self.cached_cf_path = None
            self.gui_queue.put(("prompt_cf_token", None))
        except Exception as e:
            self.log(f"Cloudflared installation failed: {e}", "ERROR",
                     speak_text="Warning. Cloudflared installation failed.")

    def _prompt_and_install_cf_service(self):
        token, ok = QInputDialog.getText(
            self, "Cloudflare Tunnel", "Enter your Cloudflare tunnel secret key to bind the service:")
        if ok and token:
            self.log("Installing background service...", "INFO")
            threading.Thread(target=self._install_cf_service_thread, args=(
                token.strip(),), daemon=True).start()
        else:
            self.log("Setup skipped. Tunnel will not auto-start.", "WARNING")

    def _install_cf_service_thread(self, token):
        try:
            cf_exe = self._get_cloudflared_path() or "cloudflared"
            flags = subprocess.CREATE_NO_WINDOW if os.name == "nt" else 0
            subprocess.run([cf_exe, "service", "uninstall"],
                           capture_output=True, creationflags=flags)
            proc = subprocess.run([cf_exe, "service", "install", token],
                                  capture_output=True, text=True, creationflags=flags)
            if proc.returncode == 0:
                self.log("Tunnel service bound successfully.", "SUCCESS",
                         speak_text="Tunnel service successfully bound.")
            else:
                self.log(
                    f"Cloudflared rejected the token: {proc.stderr.strip() or proc.stdout.strip()}", "ERROR")
        except Exception as e:
            self.log(f"Service install crashed: {e}", "ERROR")
        finally:
            self.check_system_health()

    def _offer_python_install(self):
        if QMessageBox.question(self, "Python Update Required", "Your Python version is too old. Download and install Python 3.14.6?") == QMessageBox.Yes:
            threading.Thread(
                target=self._download_and_install_python, daemon=True).start()

    def _download_and_install_python(self):
        os.makedirs(EXE_DIR, exist_ok=True)
        py_path = os.path.join(EXE_DIR, "python-3.14.6-amd64.exe")
        try:
            self.log("Downloading Python Installer... please wait.", "INFO")
            urllib.request.urlretrieve(
                "https://www.python.org/ftp/python/3.14.6/python-3.14.6-amd64.exe", py_path)
            self.log("Installing Python silently...", "WARNING")
            flags = subprocess.CREATE_NO_WINDOW if os.name == "nt" else 0
            subprocess.run([py_path, "/quiet", "InstallAllUsers=1", "PrependPath=1",
                           "Include_test=0"], check=True, creationflags=flags)
            self.log("Python installation successful.", "SUCCESS",
                     speak_text="Python successfully updated.")
            try:
                os.remove(py_path)
            except Exception:
                pass
            self.gui_queue.put(("python_done", None))
        except Exception as e:
            self.log(f"Python installation failed: {e}", "ERROR")

    # --------------------------------------------------------------------------
    # TOOL PROCESS MANAGEMENT
    # --------------------------------------------------------------------------
    def launch_tool(self, tool):
        key = tool["key"]
        if key in self.processes and self.processes[key].poll() is None:
            return

        script_path = os.path.join(APP_DIR, tool["script"])
        if not os.path.isfile(script_path):
            self.log(f"Cannot find script: {tool['script']}", "ERROR")
            return

        try:
            env = os.environ.copy()
            env["PYTHONUNBUFFERED"] = "1"
            env["EVENTHUB_TOOL_ID"] = f"EventHub.Tool.{key}"

            proc = subprocess.Popen(
                [sys.executable, script_path],
                cwd=APP_DIR, stdout=subprocess.PIPE, stderr=subprocess.STDOUT, stdin=subprocess.DEVNULL,
                text=True, encoding='utf-8', errors='replace', bufsize=1,
                creationflags=subprocess.CREATE_NO_WINDOW if os.name == "nt" else 0, env=env
            )
            self.processes[key] = proc
            safe_name = tool['label'].replace(
                'Terminal', '').replace('Manager', '')
            self.log(f"Tool started: {tool['label']}",
                     "SUCCESS", speak_text=f"{safe_name} started.")
            self._set_tool_status(key, running=True)
            threading.Thread(target=self._stream_tool_logs, args=(
                proc, tool["label"]), daemon=True).start()
        except Exception as e:
            self.log(f"Failed to launch {tool['label']}: {e}", "ERROR")

    def _stream_tool_logs(self, proc, tool_name):
        try:
            for line in iter(proc.stdout.readline, ''):
                if line:
                    clean_line = line.strip()
                    if clean_line:
                        clean_line = re.sub(r'\x1b\[[0-9;]*m', '', clean_line)
                        self.log(f"[{tool_name}] {clean_line}", "TOOL")
        except ValueError:
            pass
        except Exception as e:
            self.log(f"[{tool_name}] Log stream interrupted: {e}", "WARNING")
        finally:
            if proc.stdout:
                proc.stdout.close()

    def stop_tool(self, tool):
        key = tool["key"]
        proc = self.processes.get(key)
        if proc and proc.poll() is None:
            try:
                if os.name == "nt":
                    subprocess.run(['taskkill', '/F', '/T', '/PID', str(proc.pid)],
                                   stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)
                else:
                    proc.terminate()
            except Exception:
                pass

            safe_name = tool['label'].replace(
                'Terminal', '').replace('Manager', '')
            self.log(f"Tool stopped: {tool['label']}",
                     "WARNING", speak_text=f"{safe_name} terminated.")
            self._set_tool_status(key, running=False)

    def stop_all_tools(self):
        count = sum(1 for key in list(self.processes.keys()) if self.processes[key].poll(
        ) is None and not self.stop_tool(next(t for t in TOOLS if t["key"] == key)))
        if count:
            self.log(f"Terminated {count} active tools.",
                     "INFO", speak_text="All active tools terminated.")

    def _set_tool_status(self, key, running):
        widgets = self.tool_widgets.get(key)
        tool = next((t for t in TOOLS if t["key"] == key), None)
        if not widgets or not tool:
            return

        if running:
            widgets["status"].setText("🟢 RUNNING")
            widgets["status"].setStyleSheet(
                "font-size: 11px; font-weight: bold; color: white;")
            QTimer.singleShot(150, lambda: widgets["status"].setStyleSheet(
                "font-size: 11px; font-weight: bold; color: #2ecc71;") if widgets["status"] else None)

            widgets["button"].setText("Stop")
            widgets["button"].setStyleSheet(self._get_button_style("danger"))
            try:
                widgets["button"].clicked.disconnect()
            except Exception:
                pass
            widgets["button"].clicked.connect(
                lambda checked=False, t=tool: self.stop_tool(t))
        else:
            widgets["status"].setText("⚫ IDLE")
            widgets["status"].setStyleSheet(
                "font-size: 11px; font-weight: bold; color: #888;")
            widgets["button"].setText("Launch Tool")
            widgets["button"].setStyleSheet(
                self._get_button_style(tool["bootstyle"]))
            try:
                widgets["button"].clicked.disconnect()
            except Exception:
                pass
            widgets["button"].clicked.connect(
                lambda checked=False, t=tool: self.launch_tool(t))

    def _poll_processes(self):
        for key in list(self.processes.keys()):
            proc = self.processes[key]
            if proc.poll() is not None:
                tool = next((t for t in TOOLS if t["key"] == key), None)
                safe_name = tool['label'].replace(
                    'Terminal', '').replace('Manager', '')
                self.log(f"Tool exited unexpectedly: {tool['label']} (Code {proc.returncode})",
                         "ERROR", speak_text=f"Warning. {safe_name} crashed.")
                del self.processes[key]
                self._set_tool_status(key, running=False)

    # --------------------------------------------------------------------------
    # UTILS & SHUTDOWN
    # --------------------------------------------------------------------------
    def open_folder(self, path):
        os.makedirs(path, exist_ok=True)
        try:
            if os.name == "nt":
                os.startfile(path)
            elif sys.platform == "darwin":
                subprocess.Popen(["open", path])
            else:
                subprocess.Popen(["xdg-open", path])
        except Exception as e:
            self.log(f"Failed to open folder: {e}", "ERROR")

    def closeEvent(self, event):
        active = [k for k, p in self.processes.items() if p.poll() is None]
        if active:
            reply = QMessageBox.question(
                self, "Exit Launcher", f"{len(active)} tools are running invisibly.\n\nExit and shut them down?")
            if reply == QMessageBox.No:
                event.ignore()
                return
        self.stop_all_tools()
        event.accept()


if __name__ == "__main__":
    if os.name == 'nt':
        try:
            ctypes.windll.shell32.SetCurrentProcessExplicitAppUserModelID(
                'EventHub.Portable.CentralLauncher.1.0')
        except Exception:
            pass

    app = QApplication(sys.argv)
    app.setStyle("Fusion")
    launcher = LauncherApp()
    launcher.show()
    sys.exit(app.exec())
