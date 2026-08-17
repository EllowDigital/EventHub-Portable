#!/usr/bin/env python3
"""
EventHub Portable — Central Launcher (Autonomous PySide6 Edition)
TENT DECOR EXPO UP 2026

Fully Autonomous version: 
1. Validates and elevates Admin privileges.
2. Uses standard Tkinter to show real-time installation of Python libraries and Cloudflared.
3. Automatically transitions to the PySide6 Command Center.
4. Automatically initializes schema.py and binds the Cloudflare background service.
"""

# ==============================================================================
# STANDARD LIBRARY IMPORTS (Safe for fresh devices)
# ==============================================================================
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
import traceback
from datetime import datetime

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
        None, "runas", executable, " ".join([f'"{arg}"' for arg in sys.argv]), None, 1
    )
    sys.exit()

# ==============================================================================
# 24/7 STABILITY: GLOBAL CRASH HANDLER (GUI POPUP SUPPORT)
# ==============================================================================
def global_exception_handler(exc_type, exc_value, exc_traceback):
    err_msg = "".join(traceback.format_exception(exc_type, exc_value, exc_traceback))
    print(f"Uncaught GUI Exception intercepted:\n{err_msg}")
    try:
        from PySide6.QtWidgets import QMessageBox, QApplication
        app = QApplication.instance()
        if app:
            msg = QMessageBox()
            msg.setIcon(QMessageBox.Critical)
            msg.setWindowTitle("Critical Crash Intercepted")
            msg.setText("The application encountered an unexpected error and caught the crash.")
            msg.setDetailedText(err_msg)
            msg.exec()
    except Exception:
        pass

sys.excepthook = global_exception_handler

# ==============================================================================
# TKINTER PRE-LOADER (AUTONOMOUS DEPENDENCY INSTALLATION)
# ==============================================================================
ROOT_DIR = os.path.dirname(os.path.abspath(__file__))
REQUIREMENTS_FILE = os.path.join(ROOT_DIR, "requirements.txt")
EXE_DIR = os.path.join(ROOT_DIR, "exe-files")

def get_cloudflared_path_basic():
    if shutil.which("cloudflared"):
        return shutil.which("cloudflared")
    for base in [os.environ.get("ProgramFiles", "C:\\Program Files"), os.environ.get("ProgramFiles(x86)", "C:\\Program Files (x86)")]:
        guess = os.path.join(base, "cloudflared", "cloudflared.exe")
        if os.path.exists(guess):
            return guess
    return None

def needs_setup():
    try:
        import PySide6
        import pymysql
    except ImportError:
        return True
    if get_cloudflared_path_basic() is None:
        return True
    return False

def run_bootstrap_ui():
    import tkinter as tk
    from tkinter import messagebox, scrolledtext

    root = tk.Tk()
    root.title("EventHub Setup")
    root.geometry("500x320")
    root.attributes("-topmost", True)
    root.resizable(False, False)

    tk.Label(root, text="EventHub Autonomous Setup", font=("Arial", 12, "bold")).pack(pady=10)
    status_var = tk.StringVar(value="Analyzing system...")
    tk.Label(root, textvariable=status_var, font=("Arial", 10)).pack(pady=5)
    
    text_area = scrolledtext.ScrolledText(root, width=55, height=11, font=("Consolas", 9), bg="#1e1e1e", fg="#00ff00")
    text_area.pack(pady=5, padx=10)

    def log(msg):
        text_area.insert(tk.END, msg + "\n")
        text_area.see(tk.END)
        status_var.set(msg)

    def setup_task():
        try:
            # 1. Install pip requirements
            try:
                import PySide6
                import pymysql
            except ImportError:
                log("Missing PySide6/core libraries. Starting dependency installation...")
                if not os.path.isfile(REQUIREMENTS_FILE):
                    raise Exception(f"Missing {REQUIREMENTS_FILE}. Cannot install dependencies.")
                
                flags = subprocess.CREATE_NO_WINDOW if os.name == 'nt' else 0
                process = subprocess.Popen(
                    [sys.executable, "-m", "pip", "install", "-r", REQUIREMENTS_FILE, "--disable-pip-version-check"],
                    stdout=subprocess.PIPE, stderr=subprocess.STDOUT, text=True, creationflags=flags
                )
                for line in process.stdout:
                    log("Pip: " + line.strip())
                process.wait()
                if process.returncode != 0:
                    raise Exception("Pip installation failed! Please check your internet connection.")
                log("Python dependencies installed successfully.")

            # 2. Install Cloudflared
            if get_cloudflared_path_basic() is None:
                log("Cloudflared not found. Downloading the latest version...")
                os.makedirs(EXE_DIR, exist_ok=True)
                msi_path = os.path.join(EXE_DIR, "cloudflared-windows-amd64.msi")
                
                try:
                    urllib.request.urlretrieve(
                        "https://github.com/cloudflare/cloudflared/releases/latest/download/cloudflared-windows-amd64.msi", 
                        msi_path
                    )
                except Exception as e:
                    raise Exception(f"Failed to download Cloudflared: {e}")
                    
                log("Executing silent installation for Cloudflared MSI...")
                flags = subprocess.CREATE_NO_WINDOW if os.name == "nt" else 0
                install_proc = subprocess.run(["msiexec.exe", "/i", msi_path, "/quiet", "/norestart"], capture_output=True, text=True, creationflags=flags)
                if install_proc.returncode != 0:
                     raise Exception(f"Cloudflared MSI installation failed. Code: {install_proc.returncode}")
                log("Cloudflared installed successfully.")
                try:
                    os.remove(msi_path)
                except:
                    pass
            
            log("All requirements met! Booting PySide6 Environment...")
            time.sleep(1.5)
            root.destroy()
            
        except Exception as e:
            err_msg = traceback.format_exc()
            log("CRITICAL ERROR:")
            log(str(e))
            messagebox.showerror("Setup Error", f"Failed to setup dependencies:\n\n{str(e)}\n\nSee log for details.")
            sys.exit(1)

    threading.Thread(target=setup_task, daemon=True).start()
    root.mainloop()

    # Restart script to load fresh modules seamlessly
    flags = subprocess.CREATE_NO_WINDOW if os.name == 'nt' else 0
    executable = sys.executable
    if os.name == 'nt' and executable.lower().endswith("python.exe"):
        executable = executable[:-10] + "pythonw.exe"
    sys.exit(subprocess.call([executable, os.path.abspath(__file__)] + sys.argv[1:], cwd=ROOT_DIR, creationflags=flags))

if needs_setup():
    run_bootstrap_ui()


# ==============================================================================
# THIRD-PARTY IMPORTS (Guaranteed Safe by Pre-Loader)
# ==============================================================================
from PySide6.QtWidgets import (QApplication, QMainWindow, QWidget, QVBoxLayout,
                               QHBoxLayout, QLabel, QPushButton, QFrame,
                               QTextEdit, QGridLayout, QMessageBox, QInputDialog, QSpacerItem, QSizePolicy)
from PySide6.QtGui import QIcon, QFont, QPixmap, QTextCursor, QColor
from PySide6.QtCore import Qt, QTimer, QSize
import pymysql

# Audio Imports
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
# PATHS, CONFIG & INJECTIONS
# ==============================================================================
APP_DIR = os.path.join(ROOT_DIR, "app")
ASSETS_DIR = os.path.join(APP_DIR, "assets")
BANNER_DIR = os.path.join(ASSETS_DIR, "main-banner's")
ICON_PATH = os.path.join(ASSETS_DIR, "EventHub.ico")
CONFIG_DIR = os.path.join(APP_DIR, "config")
SCHEMA_CONFIG = os.path.join(CONFIG_DIR, "schema.json")
SECRETS_CONFIG = os.path.join(CONFIG_DIR, "secrets.json")

BANNER_IMAGES = [
    "eventhub-banner.png",
    "eventhub-banner0.png",
    "tdeup2025-team.png"
]

MIN_PYTHON = (3, 9)
MAX_LOG_LINES = 2000

def inject_cloudflared_path():
    cf_paths = [r"C:\Program Files\cloudflared", r"C:\Program Files (x86)\cloudflared"]
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
            key = winreg.OpenKey(winreg.HKEY_LOCAL_MACHINE, r"SYSTEM\CurrentControlSet\Control\Session Manager\Environment", 0, winreg.KEY_READ | winreg.KEY_WRITE)
            sys_path, _ = winreg.QueryValueEx(key, "Path")
            if valid_path.lower() not in sys_path.lower():
                new_path = valid_path + os.pathsep + sys_path
                winreg.SetValueEx(key, "Path", 0, winreg.REG_EXPAND_SZ, new_path)
                HWND_BROADCAST = 0xFFFF
                WM_SETTINGCHANGE = 0x001A
                SMTO_ABORTIFHUNG = 0x0002
                ctypes.windll.user32.SendMessageTimeoutW(HWND_BROADCAST, WM_SETTINGCHANGE, 0, "Environment", SMTO_ABORTIFHUNG, 5000, None)
            winreg.CloseKey(key)
        except Exception:
            pass


# ==============================================================================
# TOOL REGISTRY
# ==============================================================================
TOOLS = [
    {"key": "hub", "icon": "🖥️", "label": "Command Center", "script": "server_hub.py", "desc": "Central event control and server management.", "bootstyle": "primary"},
    {"key": "gate_display", "icon": "📺", "label": "Gate Display Terminal", "script": "check_in.py", "desc": "Live attendee check-in and access monitoring.", "bootstyle": "info"},
    {"key": "kiosk", "icon": "📝", "label": "Registration Kiosk", "script": "register.py", "desc": "On-site attendee registration and check-in.", "bootstyle": "success"},
    {"key": "sync", "icon": "🔄", "label": "Sync Manager", "script": "sync_manager.py", "desc": "Synchronizes attendee and event data across services.", "bootstyle": "warning"},
    {"key": "photos", "icon": "📸", "label": "Photo Downloader", "script": "photo_down.py", "desc": "Downloads and manages attendee photos for offline use.", "bootstyle": "secondary"},
    {"key": "explorer", "icon": "🔎", "label": "Attendee Explorer", "script": "explorer.py", "desc": "Search, view, and manage attendee profiles and records.", "bootstyle": "secondary"},
    {"key": "handbook", "icon": "📖", "label": "Digital Handbook", "script": "handbook.py", "desc": "Event operations, setup, and troubleshooting guide.", "bootstyle": "primary"},
    {"key": "stress_test", "icon": "⚡", "label": "Load & Stress Test", "script": "stress_test.py", "desc": "Tests system performance and stability under heavy load.", "bootstyle": "danger"},
]


# ==============================================================================
# PYSIDE6 GUI APPLICATION
# ==============================================================================
class BannerLabel(QLabel):
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
                scaled = self._pixmap.scaled(w, h, Qt.KeepAspectRatio, Qt.SmoothTransformation)
                self.setPixmap(scaled)

class LauncherApp(QMainWindow):
    def __init__(self):
        super().__init__()
        self.setWindowTitle("EventHub Portable — Central Launcher")

        screen = QApplication.primaryScreen().geometry()
        sw, sh = screen.width(), screen.height()
        ww, wh = max(1100, min(1400, int(sw * 0.85))), max(750, min(1000, int(sh * 0.85)))
        self.setGeometry(max(0, (sw - ww) // 2), max(0, (sh - wh) // 2 - 15), ww, wh)
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

        # Autonomous delayed tasks (ensuring dependencies map properly)
        QTimer.singleShot(500, self.check_system_health)
        QTimer.singleShot(1500, self._run_schema_script_async) # 1.5 seconds later to let UI breathe
        QTimer.singleShot(2500, self._setup_network_firewall_async)

        self.log(
            "System initialized with Administrator Privileges. UI Rendered successfully.",
            "SUCCESS", speak_text="Central Launcher Initialized. System ready for operations."
        )

    def apply_stylesheet(self):
        self.setStyleSheet("""
            QMainWindow { background-color: #222222; color: #ffffff; }
            QWidget { font-family: 'Segoe UI', Arial, sans-serif; }
            QLabel { color: #dddddd; }
            QFrame.Card { background-color: #2b2b2b; border: 1px solid #3d3d3d; border-radius: 6px; }
            QPushButton { border-radius: 4px; font-weight: bold; padding: 6px 12px; }
            QPushButton.Outline { background-color: transparent; border: 1px solid #555; color: #ccc; }
            QPushButton.Outline:hover { background-color: #444; }
            QTextEdit { background-color: #141414; color: #cccccc; font-family: 'Consolas', monospace; font-size: 11pt; border: 1px solid #3d3d3d; border-radius: 6px; }
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
        return f"QPushButton {{ {styles.get(style_name, styles['secondary'])} }} {hover_styles.get(style_name, hover_styles['secondary'])}"

    def _get_color_hex(self, style_name):
        return {"success": "#2ecc71", "info": "#1abc9c", "warning": "#f39c12", "danger": "#e74c3c", "secondary": "#888888"}.get(style_name, "#757575")

    # --------------------------------------------------------------------------
    # TTS & AUDIO ENGINE
    # --------------------------------------------------------------------------
    def play_sound(self, status, speak_text=""):
        if not self.sound_enabled: return
        def _play():
            if HAS_WINSOUND:
                try:
                    if status == "SUCCESS": winsound.Beep(2000, 100)
                    elif status == "WARNING":
                        winsound.Beep(1000, 100); time.sleep(0.05); winsound.Beep(1000, 100)
                    else:
                        winsound.Beep(400, 150); winsound.Beep(300, 300)
                except: QApplication.beep()
            else:
                QApplication.beep()
                if status != "SUCCESS": time.sleep(0.2); QApplication.beep()

            if speak_text:
                try:
                    if platform.system() == "Windows":
                        ps_script = f"Add-Type -AssemblyName System.Speech; $synth = New-Object System.Speech.Synthesis.SpeechSynthesizer; $synth.Rate = 0; $synth.Speak('{speak_text.replace(chr(39), '')}');"
                        subprocess.run(["powershell", "-Command", ps_script], creationflags=subprocess.CREATE_NO_WINDOW)
                    elif HAS_TTS:
                        engine = pyttsx3.init()
                        engine.say(speak_text)
                        engine.runAndWait()
                except Exception as e:
                    self.gui_queue.put(("log", {"msg": f"TTS Error: {e}", "level": "ERROR"}))

        threading.Thread(target=_play, daemon=True).start()

    def toggle_sound(self):
        self.sound_enabled = not self.sound_enabled
        self.btn_sound.setText("🔊 Voice Enabled" if self.sound_enabled else "🔇 Muted")
        self.btn_sound.setStyleSheet(f"QPushButton {{ border: 1px solid {'#2ecc71; color: #2ecc71' if self.sound_enabled else '#555; color: #888'}; background: transparent; }}")
        if self.sound_enabled: self.play_sound("SUCCESS", "Audio alerts enabled.")

    # --------------------------------------------------------------------------
    # UI CONSTRUCTION
    # --------------------------------------------------------------------------
    def build_ui(self):
        main_widget = QWidget()
        self.setCentralWidget(main_widget)
        main_layout = QVBoxLayout(main_widget)
        main_layout.setContentsMargins(25, 25, 25, 25)

        header_layout = QHBoxLayout()
        main_layout.addLayout(header_layout)

        title_layout = QVBoxLayout()
        title_lbl = QLabel("EventHub Portable")
        title_lbl.setStyleSheet("font-size: 26px; font-weight: bold; color: #3498db;")
        sub_lbl = QLabel("Central Launcher • Autonomous Environment Bootstrapping")
        sub_lbl.setStyleSheet("font-size: 10px; font-weight: bold; color: #888;")
        title_layout.addWidget(title_lbl)
        title_layout.addWidget(sub_lbl)
        header_layout.addLayout(title_layout)
        header_layout.addStretch()

        self.btn_sound = QPushButton("🔊 Voice Enabled")
        self.btn_sound.setStyleSheet("QPushButton { border: 1px solid #2ecc71; color: #2ecc71; background: transparent; }")
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

        lbl_health = QLabel("⚙️ SYSTEM HEALTH")
        lbl_health.setStyleSheet("font-size: 11px; font-weight: bold; color: #888;")
        main_layout.addWidget(lbl_health)

        health_grid = QGridLayout()
        main_layout.addLayout(health_grid)
        self._build_health_card(health_grid, 0, "python", "🐍 PYTHON VER", "Checking...")
        self._build_health_card(health_grid, 1, "cloudflared", "☁️ CLOUDFLARED", "Checking...")
        self._build_health_card(health_grid, 2, "deps", "📦 DEPENDENCIES", "Checking...")
        self._build_health_card(health_grid, 3, "config", "⚙️ CONFIGURATION", "Checking...")

        split_layout = QHBoxLayout()
        main_layout.addLayout(split_layout, stretch=1)

        left_col = QVBoxLayout()
        split_layout.addLayout(left_col, stretch=4)

        lbl_db = QLabel("🗄️ DATABASE HEALTH")
        lbl_db.setStyleSheet("font-size: 11px; font-weight: bold; color: #888;")
        left_col.addWidget(lbl_db)

        db_grid = QGridLayout()
        left_col.addLayout(db_grid)
        self._build_health_card(db_grid, 0, "mysql_db", "🐬 MYSQL (PRIMARY)", "Checking...")
        self._build_health_card(db_grid, 1, "sqlite_db", "💾 SQLITE (MIRROR)", "Checking...")

        lbl_tools = QLabel("🛠️ APPLICATION TOOLS")
        lbl_tools.setStyleSheet("font-size: 11px; font-weight: bold; color: #888; margin-top: 10px;")
        left_col.addWidget(lbl_tools)

        for tool in TOOLS: self._build_tool_card(left_col, tool)

        left_col.addStretch()

        # Restore Open Folder Buttons
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

        right_col = QVBoxLayout()
        split_layout.addLayout(right_col, stretch=6)

        self.team_card = QFrame()
        self.team_card.setProperty("class", "Card")
        team_layout = QVBoxLayout(self.team_card)
        team_layout.setContentsMargins(2, 2, 2, 2)
        self.lbl_team_photo = BannerLabel()
        team_layout.addWidget(self.lbl_team_photo)
        right_col.addWidget(self.team_card, stretch=4)

        for img_name in BANNER_IMAGES:
            img_path = os.path.join(BANNER_DIR, img_name)
            if os.path.exists(img_path):
                self.slideshow_images.append(QPixmap(img_path))

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
        lbl_log.setStyleSheet("font-size: 11px; font-weight: bold; color: #888;")
        btn_clear = QPushButton("Clear Logs")
        btn_clear.setStyleSheet("background: transparent; color: #3498db; text-decoration: underline; border: none;")
        btn_clear.clicked.connect(self.clear_log)
        log_hdr_layout.addWidget(lbl_log)
        log_hdr_layout.addStretch()
        log_hdr_layout.addWidget(btn_clear)
        right_col.addLayout(log_hdr_layout)

        self.log_box = QTextEdit()
        self.log_box.setReadOnly(True)
        right_col.addWidget(self.log_box, stretch=6)

        # Add the Powered by EllowDigital footer
        footer_lbl = QLabel("Powered by EllowDigital")
        footer_lbl.setAlignment(Qt.AlignCenter)
        footer_lbl.setStyleSheet("font-size: 11px; font-weight: bold; color: #555555; margin-top: 10px;")
        main_layout.addWidget(footer_lbl)

    def _next_slide(self):
        if not self.slideshow_images: return
        self.current_image_index = (self.current_image_index + 1) % len(self.slideshow_images)
        self.lbl_team_photo.setOriginalPixmap(self.slideshow_images[self.current_image_index])

    def _build_health_card(self, parent, col, key, title, init_val):
        card = QFrame(); card.setProperty("class", "Card")
        layout = QVBoxLayout(card); layout.setContentsMargins(12, 12, 12, 12)
        top = QHBoxLayout()
        dot = QLabel(); dot.setFixedSize(10, 10); dot.setStyleSheet("background-color: #757575; border-radius: 5px;")
        top.addWidget(dot)
        lbl_title = QLabel(title); lbl_title.setStyleSheet("font-size: 10px; font-weight: bold; color: #888;")
        top.addWidget(lbl_title); top.addStretch()
        layout.addLayout(top)
        val_lbl = QLabel(init_val); val_lbl.setStyleSheet("font-size: 14px; font-weight: bold; color: #aaa;")
        layout.addWidget(val_lbl)
        parent.addWidget(card, 0, col)
        self.health_widgets[key] = {"label": val_lbl, "dot": dot}

    def _build_tool_card(self, parent, tool):
        card = QFrame(); card.setProperty("class", "Card")
        layout = QHBoxLayout(card); layout.setContentsMargins(14, 10, 14, 10)
        icon = QLabel(tool["icon"]); icon.setStyleSheet("font-size: 24px;")
        layout.addWidget(icon)
        text = QVBoxLayout()
        title = QLabel(tool["label"]); title.setStyleSheet("font-size: 13px; font-weight: bold; color: #ddd;")
        desc = QLabel(tool["desc"]); desc.setStyleSheet("font-size: 11px; color: #888;")
        text.addWidget(title); text.addWidget(desc)
        layout.addLayout(text, stretch=1)
        status = QLabel("⚫ IDLE"); status.setStyleSheet("font-size: 11px; font-weight: bold; color: #888;")
        status.setFixedWidth(80); status.setAlignment(Qt.AlignCenter)
        layout.addWidget(status)
        btn = QPushButton("Launch Tool")
        btn.setStyleSheet(self._get_button_style(tool["bootstyle"])); btn.setFixedWidth(120)
        btn.clicked.connect(lambda checked=False, t=tool: self.launch_tool(t))
        layout.addWidget(btn)
        parent.addWidget(card)
        self.tool_widgets[tool["key"]] = {"button": btn, "status": status}

    # --------------------------------------------------------------------------
    # THREAD-SAFE UI UPDATES & EXCEPTION CATCHING
    # --------------------------------------------------------------------------
    def log(self, message, level="INFO", speak_text=None):
        self.gui_queue.put(("log", {"msg": message, "level": level}))
        if speak_text: self.play_sound(level, speak_text)

    def _process_gui_queue(self):
        for _ in range(50):
            try:
                kind, payload = self.gui_queue.get_nowait()
                if kind == "log":
                    self._append_log(payload["msg"], payload["level"])
                elif kind == "update_health":
                    self._update_health_ui(payload["key"], payload["text"], payload["style"])
                elif kind == "prompt_cf_token":
                    self._prompt_and_install_cf_service()
                elif kind == "schema_error":
                    QMessageBox.warning(self, "Schema Initialization Error", f"The schema.py script encountered a critical failure:\n\n{payload}")
            except queue.Empty:
                break
            except Exception:
                pass

    def _update_health_ui(self, key, text, style):
        w = self.health_widgets.get(key)
        if w:
            c = self._get_color_hex(style)
            w["label"].setText(text)
            w["label"].setStyleSheet(f"color: {c}; font-size: 14px; font-weight: bold;")
            w["dot"].setStyleSheet(f"background-color: {c}; border-radius: 5px;")

    def _append_log(self, msg, level):
        timestamp = datetime.now().strftime("%H:%M:%S")
        color = {"INFO": "#ccc", "SUCCESS": "#2ecc71", "WARNING": "#f39c12", "ERROR": "#e74c3c", "TOOL": "#3498db"}.get(level, "#fff")
        bold = "font-weight: bold;" if level in ["SUCCESS", "WARNING", "ERROR"] else ""
        self.log_box.append(f'<span style="color: {color}; {bold}">[{timestamp}] {msg}</span>')

    def clear_log(self): self.log_box.clear()

    # --------------------------------------------------------------------------
    # ASYNC INIT & HEALTH CHECKS
    # --------------------------------------------------------------------------
    def _run_schema_script_async(self):
        threading.Thread(target=self._schema_task, daemon=True).start()

    def _schema_task(self):
        schema_script = os.path.join(APP_DIR, "schema.py")
        if os.path.exists(schema_script):
            self.log("Initializing database schema (app/schema.py)...", "INFO")
            try:
                flags = subprocess.CREATE_NO_WINDOW if os.name == 'nt' else 0
                res = subprocess.run([sys.executable, schema_script], capture_output=True, text=True, cwd=APP_DIR, creationflags=flags)
                if res.returncode == 0:
                    self.log("Schema initialization completed successfully.", "SUCCESS")
                else:
                    err = res.stderr.strip() or res.stdout.strip()
                    self.log(f"Schema initialization failed: {err}", "ERROR")
                    self.gui_queue.put(("schema_error", err))
            except Exception as e:
                self.log(f"Error executing schema script: {e}", "ERROR")
                self.gui_queue.put(("schema_error", str(e)))

    def _setup_network_firewall_async(self):
        threading.Thread(target=self._network_firewall_task, daemon=True).start()

    def _network_firewall_task(self):
        if os.name != "nt": return
        flags = subprocess.CREATE_NO_WINDOW
        ps_fw = ("$rule = Get-NetFirewallRule -DisplayName 'EventHub Ports' -ErrorAction SilentlyContinue; "
                 "if (-not $rule) { New-NetFirewallRule -DisplayName 'EventHub Ports' -Direction Inbound -Action Allow -Protocol TCP -LocalPort 5000,5001 | Out-Null; Write-Output 'CREATED' } else { Write-Output 'EXISTS' }")
        try:
            res = subprocess.run(["powershell", "-Command", ps_fw], capture_output=True, text=True, creationflags=flags)
            if "CREATED" in res.stdout: self.log("Added inbound firewall rule for local network ports.", "SUCCESS")
        except Exception: pass

    def _set_health(self, key, text, style):
        self.gui_queue.put(("update_health", {"key": key, "text": text, "style": style}))

    def check_system_health(self):
        self.log("Running comprehensive system health checks...", "INFO")
        
        # 1. Clear cache to force a real re-check of Cloudflared paths
        self.cached_cf_path = None
        
        # 2. Instantly reset all UI cards to show they are actively refreshing
        self._set_health("python", "Checking...", "warning")
        self._set_health("config", "Checking...", "warning")
        self._set_health("deps", "Checking...", "warning")
        self._set_health("cloudflared", "Verifying...", "warning")
        self._set_health("mysql_db", "Pinging...", "warning")
        self._set_health("sqlite_db", "Pinging...", "warning")

        # 3. As requested: Re-run schema initialization on every refresh
        self._run_schema_script_async()

        # 4. Perform Synchronous Checks (Python, Configs, Dependencies)
        py_ver = f"{sys.version_info.major}.{sys.version_info.minor}.{sys.version_info.micro}"
        self._set_health("python", f"v{py_ver} ✓", "success")
        
        if os.path.isfile(SCHEMA_CONFIG) and os.path.isfile(SECRETS_CONFIG):
            self._set_health("config", "Valid ✓", "success")
        else:
            self._set_health("config", "Missing ⚠", "warning")
            
        self._set_health("deps", "Ready ✓", "success") # Handled by Tkinter safely earlier
        
        # 5. Perform Asynchronous Checks (Network, Services, Databases)
        threading.Thread(target=self._verify_cloudflared_thread, daemon=True).start()
        threading.Thread(target=self._verify_databases_thread, daemon=True).start()

    def _verify_databases_thread(self):
        # --- 1. Set Your Requested Defaults ---
        mysql_host = "localhost"
        mysql_user = "root"
        mysql_pass = "sarwan"
        mysql_db = "eventhub_db"
        mysql_port = 3306
        mysql_enabled = True

        # Correct SQLite absolute path based on APP_DIR
        sqlite_file = os.path.join(APP_DIR, "db", "eventhub_local.db")
        sqlite_enabled = True

        # --- 2. Attempt to override with schema.json if it exists ---
        if os.path.exists(SCHEMA_CONFIG):
            try:
                with open(SCHEMA_CONFIG, 'r') as f:
                    config = json.load(f)
                
                my_conf = config.get("mysql", {})
                mysql_enabled = my_conf.get("enabled", True)
                mysql_host = my_conf.get("host", mysql_host)
                mysql_user = my_conf.get("user", mysql_user)
                mysql_pass = my_conf.get("password", mysql_pass)
                mysql_db = my_conf.get("database", mysql_db)
                mysql_port = my_conf.get("port", mysql_port)

                sq_conf = config.get("sqlite", {})
                sqlite_enabled = sq_conf.get("enabled", True)
                if "folder_name" in sq_conf or "file_name" in sq_conf:
                    sqlite_file = os.path.join(APP_DIR, sq_conf.get("folder_name", "db"), sq_conf.get("file_name", "eventhub_local.db"))
            except Exception:
                pass  # Fallback to your hardcoded defaults if JSON fails

        # --- 3. Verify MySQL ---
        if mysql_enabled:
            try:
                start_t = time.perf_counter()
                conn = pymysql.connect(
                    host=mysql_host, 
                    user=mysql_user, 
                    password=mysql_pass, 
                    database=mysql_db, 
                    port=mysql_port, 
                    connect_timeout=2
                )
                conn.ping(reconnect=False)
                conn.close()
                ms = int((time.perf_counter() - start_t) * 1000)
                self._set_health("mysql_db", f"Online ✓ ({ms}ms)", "success" if ms < 100 else "warning")
            except Exception as e:
                self._set_health("mysql_db", "Offline ⚠", "danger")
        else:
            self._set_health("mysql_db", "Disabled", "secondary")

        # --- 4. Verify SQLite ---
        if sqlite_enabled:
            if os.path.exists(sqlite_file):
                try:
                    start_t = time.perf_counter()
                    conn = sqlite3.connect(sqlite_file)
                    conn.execute("SELECT name FROM sqlite_master;")
                    conn.close()
                    ms = int((time.perf_counter() - start_t) * 1000)
                    self._set_health("sqlite_db", "Ready ✓ (<1ms)" if ms == 0 else f"Ready ✓ ({ms}ms)", "success")
                except Exception:
                    self._set_health("sqlite_db", "Corrupted ⚠", "danger")
            else:
                self._set_health("sqlite_db", "Missing DB ⚠", "warning")
        else:
            self._set_health("sqlite_db", "Disabled", "secondary")

    # --------------------------------------------------------------------------
    # CLOUDFLARED SERVICE VERIFICATION & AUTOMATION
    # --------------------------------------------------------------------------
    def _get_cloudflared_path(self):
        if self.cached_cf_path: return self.cached_cf_path
        res = get_cloudflared_path_basic()
        if res: self.cached_cf_path = res
        return res

    def _verify_cloudflared_thread(self):
        cf_exe = self._get_cloudflared_path()
        if not cf_exe:
            self._set_health("cloudflared", "Missing EXE ⚠", "danger")
            return
            
        try:
            flags = subprocess.CREATE_NO_WINDOW if os.name == "nt" else 0
            res = subprocess.run([cf_exe, "--version"], capture_output=True, text=True, timeout=3, creationflags=flags)
            if res.returncode == 0:
                match = re.search(r"version\s+(\d+\.\d+\.\d+)", res.stdout)
                
                # IMPORTANT: Check if the background service is actually installed
                if os.name == 'nt':
                    svc_res = subprocess.run(["sc", "query", "cloudflared"], capture_output=True, text=True, creationflags=flags)
                    if "1060" in svc_res.stdout or "does not exist" in svc_res.stdout:
                        self._set_health("cloudflared", f"v{match.group(1)} (No Svc) ⚠" if match else "No Svc ⚠", "warning")
                        self.gui_queue.put(("prompt_cf_token", None))
                        return
                        
                self._set_health("cloudflared", f"v{match.group(1)} ✓" if match else "OK", "success")
            else:
                self._set_health("cloudflared", "Broken ⚠", "danger")
        except Exception:
            self._set_health("cloudflared", "Broken ⚠", "danger")

    def _prompt_and_install_cf_service(self):
        default_token = "eyJhIjoiZjM2MTRhMWEwNjFhYTlmNzNlZjAwNTVhMGVlZDJhMTciLCJ0IjoiYzY2MGQ3MDgtMTI3Yy00NDM0LWI3YmMtNTU0N2VlNmQ0NTEyIiwicyI6IlpqVm1PVFV5WVRrdE1HTTVOQzAwWlRnd0xUZzBPV010TnpBelpqZ3hPREEzTXpjNCJ9"
        token, ok = QInputDialog.getText(
            self, "Cloudflare Tunnel Setup (Autonomous Setup Request)", 
            "Cloudflare tunnel service is not bound to this fresh device.\n\nPress OK to apply your pre-filled service install token and bind instantly:",
            text=default_token
        )
        if ok and token:
            self.log("Installing background tunnel service autonomously...", "INFO")
            threading.Thread(target=self._install_cf_service_thread, args=(token.strip(),), daemon=True).start()
        else:
            self.log("Tunnel binding bypassed by user. Remote access will not auto-start.", "WARNING")

    def _install_cf_service_thread(self, token):
        try:
            cf_exe = self._get_cloudflared_path() or "cloudflared"
            flags = subprocess.CREATE_NO_WINDOW if os.name == "nt" else 0
            
            subprocess.run([cf_exe, "service", "uninstall"], capture_output=True, creationflags=flags)
            proc = subprocess.run([cf_exe, "service", "install", token], capture_output=True, text=True, creationflags=flags)
            
            if proc.returncode == 0:
                self.log("Tunnel service completely bound successfully.", "SUCCESS", speak_text="Tunnel service successfully bound.")
            else:
                self.log(f"Token rejection or privilege issue: {proc.stderr.strip() or proc.stdout.strip()}", "ERROR")
        except Exception as e:
            self.log(f"Service install crashed natively: {e}", "ERROR")
        finally:
            self.check_system_health()

    # --------------------------------------------------------------------------
    # TOOL PROCESS MANAGEMENT
    # --------------------------------------------------------------------------
    def launch_tool(self, tool):
        key = tool["key"]
        if key in self.processes and self.processes[key].poll() is None: return

        script_path = os.path.join(APP_DIR, tool["script"])
        if not os.path.isfile(script_path):
            self.log(f"Cannot find script: {tool['script']}", "ERROR")
            return

        try:
            env = os.environ.copy()
            env["PYTHONUNBUFFERED"] = "1"
            proc = subprocess.Popen(
                [sys.executable, script_path], cwd=APP_DIR, stdout=subprocess.PIPE, stderr=subprocess.STDOUT, stdin=subprocess.DEVNULL,
                text=True, encoding='utf-8', errors='replace', bufsize=1,
                creationflags=subprocess.CREATE_NO_WINDOW if os.name == "nt" else 0, env=env
            )
            self.processes[key] = proc
            self.log(f"Tool started: {tool['label']}", "SUCCESS", speak_text=f"{tool['label']} started.")
            self._set_tool_status(key, running=True)
            threading.Thread(target=self._stream_tool_logs, args=(proc, tool["label"]), daemon=True).start()
        except Exception as e:
            self.log(f"Failed to launch {tool['label']}: {e}", "ERROR")

    def _stream_tool_logs(self, proc, tool_name):
        try:
            for line in iter(proc.stdout.readline, ''):
                if line and (clean_line := line.strip()):
                    self.log(f"[{tool_name}] {re.sub(r'\x1b\[[0-9;]*m', '', clean_line)}", "TOOL")
        except Exception: pass
        finally:
            if proc.stdout: proc.stdout.close()

    def stop_tool(self, tool):
        key = tool["key"]
        proc = self.processes.get(key)
        if proc and proc.poll() is None:
            try:
                if os.name == "nt": subprocess.run(['taskkill', '/F', '/T', '/PID', str(proc.pid)], stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)
                else: proc.terminate()
            except Exception: pass
            self.log(f"Tool stopped: {tool['label']}", "WARNING", speak_text=f"{tool['label']} terminated.")
            self._set_tool_status(key, running=False)

    def stop_all_tools(self):
        for key in list(self.processes.keys()):
            if self.processes[key].poll() is None:
                self.stop_tool(next(t for t in TOOLS if t["key"] == key))

    def _set_tool_status(self, key, running):
        widgets = self.tool_widgets.get(key)
        tool = next((t for t in TOOLS if t["key"] == key), None)
        if not widgets or not tool: return

        if running:
            widgets["status"].setText("🟢 RUNNING")
            widgets["status"].setStyleSheet("font-size: 11px; font-weight: bold; color: #2ecc71;")
            widgets["button"].setText("Stop"); widgets["button"].setStyleSheet(self._get_button_style("danger"))
            try: widgets["button"].clicked.disconnect()
            except Exception: pass
            widgets["button"].clicked.connect(lambda checked=False, t=tool: self.stop_tool(t))
        else:
            widgets["status"].setText("⚫ IDLE")
            widgets["status"].setStyleSheet("font-size: 11px; font-weight: bold; color: #888;")
            widgets["button"].setText("Launch Tool"); widgets["button"].setStyleSheet(self._get_button_style(tool["bootstyle"]))
            try: widgets["button"].clicked.disconnect()
            except Exception: pass
            widgets["button"].clicked.connect(lambda checked=False, t=tool: self.launch_tool(t))

    def _poll_processes(self):
        for key in list(self.processes.keys()):
            proc = self.processes[key]
            if proc.poll() is not None:
                tool = next((t for t in TOOLS if t["key"] == key), None)
                self.log(f"Tool exited unexpectedly: {tool['label']} (Code {proc.returncode})", "ERROR", speak_text=f"Warning. {tool['label']} crashed.")
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
        if active and QMessageBox.question(self, "Exit Launcher", f"{len(active)} tools are running invisibly.\n\nExit and shut them down?") == QMessageBox.No:
            event.ignore(); return
        self.stop_all_tools(); event.accept()

if __name__ == "__main__":
    if os.name == 'nt':
        try: ctypes.windll.shell32.SetCurrentProcessExplicitAppUserModelID('EventHub.Portable.CentralLauncher.1.0')
        except Exception: pass

    app = QApplication(sys.argv)
    app.setStyle("Fusion")
    launcher = LauncherApp()
    launcher.show()
    sys.exit(app.exec())