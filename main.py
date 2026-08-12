#!/usr/bin/env python3
"""
EventHub Portable — Central Launcher
TENT DECOR EXPO UP 2026

Single entry point for the whole offline kit. 
Auto-installs dependencies, verifies system health, captures tool logs, 
and manages tool processes with Voice & Audio feedback.
"""

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
import tkinter as tk

# ==============================================================================
# BANNER SLIDESHOW IMAGES (EDIT THIS LIST)
# ==============================================================================
# Add or remove image filenames here. 
# Ensure these files are placed inside the folder: app/assets/main-banner's/
BANNER_IMAGES = [
    "eventhub-banner.png",
    "eventhub-banner0.png",
    # "eventhub-banner1.png",
    # "eventhub-banner2.png",
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
    """Check if the script is currently running with Administrator privileges."""
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
# 24/7 STABILITY: GLOBAL CRASH HANDLER
# ==============================================================================
def global_exception_handler(*args):
    print(f"Uncaught GUI Exception intercepted. App remains running: {args}")

tk.Tk.report_callback_exception = global_exception_handler

try:
    import ttkbootstrap as ttk
    from ttkbootstrap.constants import *
    from ttkbootstrap.widgets.scrolled import ScrolledText
    from tkinter import messagebox, simpledialog
    from PIL import Image, ImageTk, ImageOps
    import pymysql
except ImportError:
    pass 

# ==============================================================================
# PATHS & CONFIG
# ==============================================================================
ROOT_DIR = os.path.dirname(os.path.abspath(__file__))
APP_DIR = os.path.join(ROOT_DIR, "app")
ASSETS_DIR = os.path.join(APP_DIR, "assets")
BANNER_DIR = os.path.join(ASSETS_DIR, "main-banner's")
ICON_PATH = os.path.join(ASSETS_DIR, "EventHub.ico")

REQUIREMENTS_FILE = os.path.join(ROOT_DIR, "requirements.txt")
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
# FIRST-RUN BOOTSTRAP
# ==============================================================================
def _bootstrap_first_run():
    try:
        import ttkbootstrap 
        import pymysql
        return
    except ImportError:
        pass

    if not os.path.isfile(REQUIREMENTS_FILE):
        sys.exit(1) 

    flags = subprocess.CREATE_NO_WINDOW if os.name == 'nt' else 0
    subprocess.call(
        [sys.executable, "-m", "pip", "install", "-r", REQUIREMENTS_FILE, "--disable-pip-version-check"], 
        cwd=ROOT_DIR, 
        creationflags=flags,
        stdout=subprocess.DEVNULL,
        stderr=subprocess.DEVNULL
    )
    
    executable = sys.executable
    if os.name == 'nt' and executable.lower().endswith("python.exe"):
        executable = executable[:-10] + "pythonw.exe"
        
    sys.exit(subprocess.call([executable, os.path.abspath(__file__)] + sys.argv[1:], cwd=ROOT_DIR, creationflags=flags))

_bootstrap_first_run()

# ==============================================================================
# TOOL REGISTRY
# ==============================================================================
TOOLS = [
    {"key": "hub", "icon": "🖥️", "label": "Command Center", "script": "server_hub.py", "desc": "Central control server.", "bootstyle": PRIMARY},
    {"key": "gate_display", "icon": "📺", "label": "Gate Display Terminal", "script": "check_in.py", "desc": "Live access monitor.", "bootstyle": INFO},
    {"key": "kiosk", "icon": "📝", "label": "Registration Kiosk", "script": "register.py", "desc": "Walk-in registration desk.", "bootstyle": SUCCESS},
    {"key": "sync", "icon": "🔄", "label": "Sync Manager", "script": "sync_manager.py", "desc": "Database synchronization engine.", "bootstyle": WARNING},
    {"key": "photos", "icon": "🖼️", "label": "Photo Downloader", "script": "photo_down.py", "desc": "Offline photo cache.", "bootstyle": SECONDARY},
    {"key": "explorer", "icon": "🔍", "label": "Attendee Explorer", "script": "explorer.py", "desc": "Profile search directory.", "bootstyle": SECONDARY},
    {"key": "handbook", "icon": "📖", "label": "Digital Handbook", "script": "handbook.py", "desc": "Troubleshooting reference guide.", "bootstyle": PRIMARY},
]

# ==============================================================================
# MAIN GUI APPLICATION
# ==============================================================================
class LauncherApp(ttk.Window):
    def __init__(self):
        super().__init__(themename="darkly", title="EventHub Portable — Central Launcher (Administrator)")
        
        sw, sh = self.winfo_screenwidth(), self.winfo_screenheight()
        ww, wh = max(1100, min(1400, int(sw * 0.85))), max(750, min(1000, int(sh * 0.85)))
        self.geometry(f"{ww}x{wh}+{max(0, (sw - ww) // 2)}+{max(0, (sh - wh) // 2 - 15)}")
        self.minsize(1100, 780)

        # Set custom window icon if it exists
        if os.path.exists(ICON_PATH):
            try:
                self.iconbitmap(ICON_PATH)
            except Exception:
                pass

        inject_cloudflared_path()

        self.gui_queue = queue.Queue()
        self.processes = {}      
        self.tool_widgets = {}   
        self.health_widgets = {}
        self.cached_cf_path = None
        self.sound_enabled = True
        
        # --- SLIDESHOW VARIABLES ---
        self.slideshow_images = []
        self.current_image_original = None
        self.current_image_index = 0
        self.slideshow_interval = 4000  # Change image every 4000 ms (4 seconds)

        self._configure_custom_styles()
        self.build_ui()

        # Staggered startup for smooth UI loading
        self.after(50, self._process_gui_queue)
        self.after(500, self.check_system_health)
        self.after(800, self._run_schema_script_async)
        self.after(1000, self._setup_network_firewall_async)
        self.after(2000, self._poll_processes)

        self.log(
            "System initialized with Administrator Privileges. Ready for operations.", 
            "SUCCESS", 
            speak_text="Central Launcher Initialized. System ready for operations."
        )

    def _configure_custom_styles(self):
        colors = self.style.colors
        self.CARD_BG = colors.get("dark")
        self.SOFT_BORDER = self._mix_hex(self.CARD_BG, colors.get("fg"), 0.08)
        self.style.configure("Card.TFrame", background=self.CARD_BG, bordercolor=self.SOFT_BORDER, borderwidth=1, relief="solid")

    def _hex_to_rgb(self, hex_color):
        hex_color = hex_color.lstrip('#')
        return tuple(int(hex_color[i:i+2], 16) for i in (0, 2, 4))

    def _rgb_to_hex(self, rgb):
        return "#{:02x}{:02x}{:02x}".format(*(max(0, min(255, int(round(c)))) for c in rgb))

    def _mix_hex(self, c_a, c_b, w):
        return self._rgb_to_hex(a + (b - a) * w for a, b in zip(self._hex_to_rgb(c_a), self._hex_to_rgb(c_b)))

    # --------------------------------------------------------------------------
    # TTS & AUDIO NOTIFICATION ENGINE
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
                    self.bell() 
            else:
                self.bell()
                if status != "SUCCESS":
                    time.sleep(0.2)
                    self.bell()

            if speak_text:
                try:
                    if platform.system() == "Windows":
                        safe_text = speak_text.replace("'", "")
                        ps_script = (
                            f"Add-Type -AssemblyName System.Speech; "
                            f"$synth = New-Object System.Speech.Synthesis.SpeechSynthesizer; "
                            f"$synth.SelectVoiceByHints([System.Speech.Synthesis.VoiceGender]::Female); "
                            f"$synth.Rate = 0; "
                            f"$synth.Speak('{safe_text}');"
                        )
                        subprocess.run(["powershell", "-Command", ps_script], creationflags=subprocess.CREATE_NO_WINDOW)
                    elif HAS_TTS:
                        engine = pyttsx3.init()
                        for voice in engine.getProperty('voices'):
                            if any(x in voice.name.lower() for x in ['female', 'zira', 'samantha']):
                                engine.setProperty('voice', voice.id)
                                break
                        engine.say(speak_text)
                        engine.runAndWait()
                except Exception as e:
                    self.gui_queue.put(("log", {"msg": f"TTS Error: {e}", "level": "ERROR"}))

        threading.Thread(target=_play, daemon=True).start()

    def toggle_sound(self):
        self.sound_enabled = not self.sound_enabled
        if self.sound_enabled:
            self.btn_sound.configure(text="🔊 Voice Enabled", bootstyle="outline-success")
            self.play_sound("SUCCESS", "Audio alerts enabled.")
        else:
            self.btn_sound.configure(text="🔇 Muted", bootstyle="outline-secondary")

    # --------------------------------------------------------------------------
    # UI CONSTRUCTION (RESPONSIVE GRID)
    # --------------------------------------------------------------------------
    def build_ui(self):
        main_container = ttk.Frame(self, padding=25)
        main_container.pack(fill=BOTH, expand=True)

        # -- Header --
        header_frame = ttk.Frame(main_container)
        header_frame.pack(fill=X, pady=(0, 20))

        title_box = ttk.Frame(header_frame)
        title_box.pack(side=LEFT)
        ttk.Label(title_box, text="EventHub Portable", font="-size 26 -weight bold", bootstyle=PRIMARY).pack(anchor=W)
        ttk.Label(title_box, text="CENTRAL LAUNCHER • TDE UP 2026", font="-size 10 -weight bold", bootstyle=SECONDARY).pack(anchor=W)

        action_box = ttk.Frame(header_frame)
        action_box.pack(side=RIGHT)
        
        self.btn_sound = ttk.Button(action_box, text="🔊 Voice Enabled", bootstyle="outline-success", command=self.toggle_sound)
        self.btn_sound.pack(side=LEFT, padx=10, ipady=4)
        
        ttk.Button(action_box, text="⟳ Refresh Health Check", bootstyle="outline-info", command=self.check_system_health).pack(side=LEFT, padx=5, ipady=4)
        ttk.Button(action_box, text="🛑 Stop All Active Tools", bootstyle=DANGER, command=self.stop_all_tools).pack(side=LEFT, padx=5, ipady=4)

        # -- System Health Panel (Responsive Cards) --
        ttk.Label(main_container, text="⚙️ SYSTEM HEALTH", font="-size 11 -weight bold", foreground="gray").pack(anchor=W, pady=(0, 5))
        health_grid = ttk.Frame(main_container)
        health_grid.pack(fill=X, pady=(0, 20))
        health_grid.columnconfigure((0, 1, 2, 3), weight=1, uniform="health")
        
        self._build_health_card(health_grid, 0, "python", "🐍 PYTHON VER", "Checking...")
        self._build_health_card(health_grid, 1, "cloudflared", "☁️ CLOUDFLARED", "Checking...")
        self._build_health_card(health_grid, 2, "deps", "📦 DEPENDENCIES", "Checking...")
        self._build_health_card(health_grid, 3, "config", "⚙️ CONFIGURATION", "Checking...")

        # -- Main Split Content (Responsive Grid) --
        split_frame = ttk.Frame(main_container)
        split_frame.pack(fill=BOTH, expand=True)
        split_frame.columnconfigure(0, weight=4) # Left takes ~40% space
        split_frame.columnconfigure(1, weight=6) # Right takes ~60% space
        split_frame.rowconfigure(0, weight=1)

        # Left Column: DB Health + Tools 
        left_col = ttk.Frame(split_frame)
        left_col.grid(row=0, column=0, sticky=NSEW, padx=(0, 25))

        ttk.Label(left_col, text="🗄️ DATABASE HEALTH", font="-size 11 -weight bold", foreground="gray").pack(anchor=W, pady=(0, 5))
        db_grid = ttk.Frame(left_col)
        db_grid.pack(fill=X, pady=(0, 20))
        db_grid.columnconfigure((0, 1), weight=1, uniform="db")
        
        self._build_db_status_card(db_grid, 0, "mysql_db", "🐬 MYSQL (PRIMARY)", "Checking...")
        self._build_db_status_card(db_grid, 1, "sqlite_db", "💾 SQLITE (MIRROR)", "Checking...")

        ttk.Label(left_col, text="🛠️ APPLICATION TOOLS", font="-size 11 -weight bold", foreground="gray").pack(anchor=W, pady=(0, 5))
        for tool in TOOLS:
            self._build_tool_card(left_col, tool)
            
        btn_row = ttk.Frame(left_col)
        btn_row.pack(fill=X, side=BOTTOM, pady=(10, 0))
        ttk.Button(btn_row, text="📁 Open Root Folder", bootstyle="outline-secondary", command=lambda: self.open_folder(ROOT_DIR)).pack(side=LEFT, fill=X, expand=True, padx=(0, 5), ipady=6)
        ttk.Button(btn_row, text="⚙️ Open Configs", bootstyle="outline-secondary", command=lambda: self.open_folder(CONFIG_DIR)).pack(side=LEFT, fill=X, expand=True, padx=(5, 0), ipady=6)

        # Right Column: Banner & Logs
        right_col = ttk.Frame(split_frame)
        right_col.grid(row=0, column=1, sticky=NSEW)
        right_col.columnconfigure(0, weight=1)
        right_col.rowconfigure(1, weight=1)

        self.team_card = ttk.Frame(right_col, style="Card.TFrame", padding=2) 
        self.team_card.grid(row=0, column=0, sticky=EW, pady=(0, 15))
        self.team_card.pack_propagate(False) 
        
        self.lbl_team_photo = ttk.Label(self.team_card, background=self.CARD_BG, anchor=CENTER)
        self.lbl_team_photo.pack(fill=BOTH, expand=True)

        # --- LOAD SLIDESHOW IMAGES FROM THE GLOBAL BANNER_IMAGES LIST ---
        for img_name in BANNER_IMAGES:
            img_path = os.path.join(BANNER_DIR, img_name)
            if os.path.exists(img_path):
                try:
                    self.slideshow_images.append(Image.open(img_path))
                except Exception as e:
                    self.log(f"Could not open image '{img_name}': {e}", "WARNING")

        if self.slideshow_images:
            self.current_image_original = self.slideshow_images[0]
            self.team_card.bind("<Configure>", self._resize_team_banner)
            if len(self.slideshow_images) > 1:
                self.after(self.slideshow_interval, self._next_slide)
        else:
            self.team_card.configure(height=100) 
            self.lbl_team_photo.configure(
                text=f"📸 Place images in:\n{BANNER_DIR}", 
                font="-size 10 -slant italic", 
                foreground="gray",
                justify=CENTER
            )

        log_wrapper = ttk.Frame(right_col)
        log_wrapper.grid(row=1, column=0, sticky=NSEW)
        log_wrapper.rowconfigure(1, weight=1)
        log_wrapper.columnconfigure(0, weight=1)
        
        log_hdr = ttk.Frame(log_wrapper)
        log_hdr.grid(row=0, column=0, sticky=EW, pady=(0, 5))
        ttk.Label(log_hdr, text="📟 ACTIVITY LOG (STDOUT)", font="-size 11 -weight bold", foreground="gray").pack(side=LEFT)
        ttk.Button(log_hdr, text="Clear Logs", bootstyle="secondary-link", command=self.clear_log).pack(side=RIGHT)

        log_frame = ttk.Frame(log_wrapper, style="Card.TFrame", padding=4)
        log_frame.grid(row=1, column=0, sticky=NSEW)

        self.log_box = ScrolledText(log_frame, autohide=True, wrap="word")
        self.log_box.pack(fill=BOTH, expand=True)
        self.log_box.text.configure(state="disabled", font=("Consolas", 11), bg="#141414", borderwidth=0, padx=10, pady=10)
        
        self.log_box.text.tag_config("INFO", foreground="#cccccc")
        self.log_box.text.tag_config("SUCCESS", foreground="#4CD37E", font=("Consolas", 11, "bold"))
        self.log_box.text.tag_config("WARNING", foreground="#FFB454", font=("Consolas", 11, "bold"))
        self.log_box.text.tag_config("ERROR", foreground="#FF6B6B", font=("Consolas", 11, "bold"))
        self.log_box.text.tag_config("TOOL", foreground="#5DADE2") 

    def _next_slide(self):
        """Advances the slideshow to the next image in the queue."""
        if not self.slideshow_images: return
        
        self.current_image_index = (self.current_image_index + 1) % len(self.slideshow_images)
        self.current_image_original = self.slideshow_images[self.current_image_index]
        
        # Apply to the last known width
        w = getattr(self, '_last_banner_w', self.team_card.winfo_width())
        if w > 10:
            self._apply_image_to_banner(w)
            
        self.after(self.slideshow_interval, self._next_slide)

    def _resize_team_banner(self, event):
        """Handles resizing events effectively without spamming recalculations."""
        if not self.current_image_original or event.width <= 10: return
        if hasattr(self, '_last_banner_w') and abs(self._last_banner_w - event.width) < 15: return
        
        self._last_banner_w = event.width
        self._apply_image_to_banner(event.width)

    def _apply_image_to_banner(self, width):
        """Processes and dynamically applies the loaded original image to perfectly fit the banner label without cropping."""
        if not self.current_image_original or width <= 10: return
        try:
            original_w, original_h = self.current_image_original.size
            
            # Maximum allowed dimensions
            max_w = width - 4
            max_h = 320
            
            # Calculate the scaling ratio to keep aspect ratio perfectly without cropping
            ratio = min(max_w / original_w, max_h / original_h)
            new_w = int(original_w * ratio)
            new_h = int(original_h * ratio)
            
            # Set container height to exactly match the scaled image height
            self.team_card.configure(height=new_h + 4)
            
            # Use basic resize (instead of ImageOps.fit) so it doesn't crop edges
            img = self.current_image_original.resize((new_w, new_h), Image.Resampling.LANCZOS)
            
            photo = ImageTk.PhotoImage(img)
            self.lbl_team_photo.configure(image=photo)
            self.lbl_team_photo.image = photo 
        except Exception as e: 
            pass

    def _build_health_card(self, parent, column, key, title, initial_val):
        card = ttk.Frame(parent, style="Card.TFrame", padding=(16, 12))
        card.grid(row=0, column=column, sticky=NSEW, padx=6)
        
        top = ttk.Frame(card, style="Card.TFrame")
        top.pack(fill=X)
        
        canvas = tk.Canvas(top, width=12, height=12, bg=self.CARD_BG, highlightthickness=0)
        dot = canvas.create_oval(2, 2, 10, 10, fill="#757575", outline="")
        canvas.pack(side=LEFT, padx=(0, 6))
        
        ttk.Label(top, text=title, font="-size 9 -weight bold", background=self.CARD_BG, foreground="gray").pack(side=LEFT)
        
        val_lbl = ttk.Label(card, text=initial_val, font="-size 12 -weight bold", background=self.CARD_BG, foreground="gray")
        val_lbl.pack(anchor=W, pady=(6, 0))
        
        self.health_widgets[key] = {"label": val_lbl, "canvas": canvas, "dot": dot}

    def _build_db_status_card(self, parent, column, key, title, initial_val):
        card = ttk.Frame(parent, style="Card.TFrame", padding=(16, 12))
        card.grid(row=0, column=column, sticky=NSEW, padx=(0, 10) if column == 0 else (0, 0))
        
        top = ttk.Frame(card, style="Card.TFrame")
        top.pack(fill=X)
        
        canvas = tk.Canvas(top, width=12, height=12, bg=self.CARD_BG, highlightthickness=0)
        dot = canvas.create_oval(2, 2, 10, 10, fill="#757575", outline="")
        canvas.pack(side=LEFT, padx=(0, 6))
        
        ttk.Label(top, text=title, font="-size 9 -weight bold", background=self.CARD_BG, foreground="gray").pack(side=LEFT)
        
        val_lbl = ttk.Label(card, text=initial_val, font="-size 12 -weight bold", background=self.CARD_BG, foreground="gray")
        val_lbl.pack(anchor=W, pady=(6, 0))
        
        self.health_widgets[key] = {"label": val_lbl, "canvas": canvas, "dot": dot}

    def _build_tool_card(self, parent, tool):
        card = ttk.Frame(parent, style="Card.TFrame", padding=14)
        card.pack(fill=X, pady=5)

        inner = ttk.Frame(card, style="Card.TFrame")
        inner.pack(fill=BOTH, expand=True)

        ttk.Label(inner, text=tool["icon"], font="-size 20", background=self.CARD_BG).grid(row=0, column=0, rowspan=2, padx=(5, 18), sticky=W)
        ttk.Label(inner, text=tool["label"], font="-size 12 -weight bold", background=self.CARD_BG).grid(row=0, column=1, sticky=W)
        
        ttk.Label(inner, text=tool["desc"], font="-size 9", background=self.CARD_BG, foreground="gray", wraplength=220).grid(row=1, column=1, sticky=W)

        status_lbl = ttk.Label(inner, text="⚫ IDLE", font="-size 10 -weight bold", background=self.CARD_BG, foreground="gray", width=12, anchor=CENTER)
        status_lbl.grid(row=0, column=2, rowspan=2, padx=10)

        btn = ttk.Button(inner, text="Launch Tool", bootstyle=tool["bootstyle"], width=16, command=lambda t=tool: self.launch_tool(t))
        btn.grid(row=0, column=3, rowspan=2)

        inner.columnconfigure(1, weight=1)
        self.tool_widgets[tool["key"]] = {"button": btn, "status": status_lbl}

    # --------------------------------------------------------------------------
    # ANIMATIONS & THREAD-SAFE UI UPDATES
    # --------------------------------------------------------------------------
    def _animate_pulse(self, widget, property_name, target_color, fallback="gray"):
        """Creates a smooth bright flash before settling on the new status color."""
        try:
            widget.configure(**{property_name: "white"})
            self.after(150, lambda: widget.configure(**{property_name: target_color}) if widget.winfo_exists() else None)
        except Exception:
            pass

    def log(self, message, level="INFO", speak_text=None):
        self.gui_queue.put(("log", {"msg": message, "level": level}))
        if speak_text:
            self.play_sound(level, speak_text)

    def _process_gui_queue(self):
        for _ in range(100):
            try:
                kind, payload = self.gui_queue.get_nowait()
                
                if kind == "log":
                    self._append_log(payload["msg"], payload["level"])
                    
                elif kind == "update_health":
                    w = self.health_widgets.get(payload["key"])
                    if w:
                        color = getattr(self.style.colors, payload["style"], "#757575")
                        w["label"].configure(text=payload["text"])
                        self._animate_pulse(w["label"], "foreground", color)
                        
                        w["canvas"].itemconfig(w["dot"], fill="white")
                        w["canvas"].coords(w["dot"], 1, 1, 11, 11)
                        self.after(150, lambda cv=w["canvas"], dt=w["dot"], c=color: (cv.itemconfig(dt, fill=c), cv.coords(dt, 2, 2, 10, 10)) if cv.winfo_exists() else None)
                        
                elif kind == "prompt_cf_token":
                    self._prompt_and_install_cf_service()
                elif kind == "python_done":
                    messagebox.showinfo("Python Update Complete", "Python 3.14.6 installed. Please restart the app.", parent=self)
                    
            except queue.Empty:
                break
            except Exception as e:
                pass
                
        self.after(30, self._process_gui_queue) 

    def _append_log(self, message, level):
        timestamp = datetime.now().strftime("%H:%M:%S")
        formatted_msg = f"[{timestamp}] {message}\n"
        self.log_box.text.configure(state="normal")
        self.log_box.text.insert(END, formatted_msg, level)
        self.log_box.text.see(END)
        
        lc = int(self.log_box.text.index('end-1c').split('.')[0])
        if lc > MAX_LOG_LINES: 
            self.log_box.text.delete('1.0', f'{lc - MAX_LOG_LINES}.0')
            
        self.log_box.text.configure(state="disabled")

    def clear_log(self):
        self.log_box.text.configure(state="normal")
        self.log_box.text.delete("1.0", END)
        self.log_box.text.configure(state="disabled")

    # --------------------------------------------------------------------------
    # ASYNC SYSTEM/NETWORK RULES & SCHEMA INIT
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
                    self.log(f"Schema initialization failed: {res.stderr.strip() or res.stdout.strip()}", "ERROR")
            except Exception as e:
                self.log(f"Error running schema script: {e}", "ERROR")
        else:
            self.log("Schema script not found. Skipping initialization.", "WARNING")

    def _setup_network_firewall_async(self):
        threading.Thread(target=self._network_firewall_task, daemon=True).start()

    def _network_firewall_task(self):
        if os.name != "nt": return
        flags = subprocess.CREATE_NO_WINDOW
        
        self.log("Verifying EventHub firewall configurations...", "INFO")
        ps_fw_cmd = (
            "$rule = Get-NetFirewallRule -DisplayName 'EventHub Ports' -ErrorAction SilentlyContinue; "
            "if (-not $rule) { New-NetFirewallRule -DisplayName 'EventHub Ports' -Direction Inbound -Action Allow -Protocol TCP -LocalPort 5000,5001 | Out-Null; Write-Output 'CREATED' } else { Write-Output 'EXISTS' }"
        )
        try:
            res = subprocess.run(["powershell", "-Command", ps_fw_cmd], capture_output=True, text=True, creationflags=flags)
            if "CREATED" in res.stdout:
                self.log("Added new inbound firewall rule for ports 5000, 5001.", "SUCCESS")
            else:
                self.log("Firewall rules for ports 5000 and 5001 are already configured.", "INFO")
        except Exception as e:
            self.log(f"Failed to configure firewall: {e}", "ERROR")

        self.log("Flushing DNS and renewing IP configuration...", "WARNING")
        try:
            subprocess.run(["ipconfig", "/flushdns"], capture_output=True, creationflags=flags)
            subprocess.run(["ipconfig", "/renew"], capture_output=True, creationflags=flags)
            self.log("Network configuration reset successfully.", "SUCCESS")
        except Exception as e:
            self.log(f"Failed to reset network: {e}", "ERROR")

    # --------------------------------------------------------------------------
    # SYSTEM HEALTH & VERSION CHECKS
    # --------------------------------------------------------------------------
    def _set_health(self, key, text, style):
        self.gui_queue.put(("update_health", {"key": key, "text": text, "style": style}))

    def check_system_health(self):
        self.log("Running system health & version checks...", "INFO")
        
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
        threading.Thread(target=self._install_requirements_thread, daemon=True).start()

        self._set_health("cloudflared", "Verifying...", "warning")
        threading.Thread(target=self._verify_cloudflared_thread, daemon=True).start()
        
        self._set_health("mysql_db", "Pinging...", "warning")
        self._set_health("sqlite_db", "Pinging...", "warning")
        threading.Thread(target=self._verify_databases_thread, daemon=True).start()

    def _verify_databases_thread(self):
        if not os.path.exists(SCHEMA_CONFIG):
            self._set_health("mysql_db", "Missing Config", "warning")
            self._set_health("sqlite_db", "Missing Config", "warning")
            return

        try:
            with open(SCHEMA_CONFIG, 'r') as f: config = json.load(f)
        except Exception:
            self._set_health("mysql_db", "Invalid JSON", "danger")
            self._set_health("sqlite_db", "Invalid JSON", "danger")
            return

        my_conf = config.get("mysql", {})
        if my_conf.get("enabled"):
            try:
                start_t = time.perf_counter()
                conn = pymysql.connect(host=my_conf.get("host", "localhost"), user=my_conf.get("user", "root"), password=my_conf.get("password", ""), database=my_conf.get("database", "eventhub_db"), port=my_conf.get("port", 3306), connect_timeout=2)
                conn.ping(reconnect=False)
                conn.close()
                ms = int((time.perf_counter() - start_t) * 1000)
                self._set_health("mysql_db", f"Online ✓ ({ms}ms)", "success" if ms < 100 else "warning")
            except Exception:
                self._set_health("mysql_db", "Offline ⚠", "danger")
        else:
            self._set_health("mysql_db", "Disabled", "secondary")

        sq_conf = config.get("sqlite", {})
        if sq_conf.get("enabled"):
            db_file = os.path.join(APP_DIR, sq_conf.get("folder_name", "db"), sq_conf.get("file_name", "eventhub_local.db"))
            if os.path.exists(db_file):
                try:
                    start_t = time.perf_counter()
                    conn = sqlite3.connect(db_file)
                    conn.execute("SELECT name FROM sqlite_master;")
                    conn.close()
                    ms = int((time.perf_counter() - start_t) * 1000)
                    self._set_health("sqlite_db", f"Ready ✓ (<1ms)" if ms == 0 else f"Ready ✓ ({ms}ms)", "success")
                except Exception:
                    self._set_health("sqlite_db", "Corrupted ⚠", "danger")
            else:
                self._set_health("sqlite_db", "Missing DB ⚠", "warning")
        else:
            self._set_health("sqlite_db", "Disabled", "secondary")

    def _install_requirements_thread(self):
        try:
            flags = subprocess.CREATE_NO_WINDOW if os.name == "nt" else 0
            proc = subprocess.run([sys.executable, "-m", "pip", "install", "-r", REQUIREMENTS_FILE, "--disable-pip-version-check"], cwd=ROOT_DIR, capture_output=True, text=True, creationflags=flags)
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
    # CLOUDFLARED MANAGEMENT
    # --------------------------------------------------------------------------
    def _get_cloudflared_path(self):
        if self.cached_cf_path: return self.cached_cf_path
        if shutil.which("cloudflared"): return "cloudflared"
        for base in [os.environ.get("ProgramFiles", "C:\\Program Files"), os.environ.get("ProgramFiles(x86)", "C:\\Program Files (x86)")]:
            guess = os.path.join(base, "cloudflared", "cloudflared.exe")
            if os.path.exists(guess): return "cloudflared"
        return None

    def _verify_cloudflared_thread(self):
        cf_exe = self._get_cloudflared_path()
        if not cf_exe:
            self._set_health("cloudflared", "Missing ⚠", "warning")
            self._offer_cloudflared_install()
            return
        try:
            flags = subprocess.CREATE_NO_WINDOW if os.name == "nt" else 0
            res = subprocess.run([cf_exe, "--version"], capture_output=True, text=True, timeout=3, creationflags=flags)
            if res.returncode == 0:
                match = re.search(r"version\s+(\d+\.\d+\.\d+)", res.stdout)
                self._set_health("cloudflared", f"v{match.group(1)} ✓" if match else "OK", "success")
            else:
                self._set_health("cloudflared", "Broken ⚠", "danger")
        except Exception:
            self._set_health("cloudflared", "Broken ⚠", "danger")

    def _offer_cloudflared_install(self):
        if messagebox.askyesno("Cloudflared Missing", "Cloudflared is required. Download and install it now?", parent=self):
            threading.Thread(target=self._download_and_install_cloudflared, daemon=True).start()

    def _download_and_install_cloudflared(self):
        os.makedirs(EXE_DIR, exist_ok=True)
        msi_path = os.path.join(EXE_DIR, "cloudflared-windows-amd64.msi")
        msi_url = "https://github.com/cloudflare/cloudflared/releases/latest/download/cloudflared-windows-amd64.msi"
        try:
            self.log("Downloading Cloudflared... please wait.", "INFO")
            urllib.request.urlretrieve(msi_url, msi_path)
            self.log("Installing Cloudflared completely silently in background...", "WARNING")
            flags = subprocess.CREATE_NO_WINDOW if os.name == "nt" else 0
            subprocess.run(["msiexec.exe", "/i", msi_path, "/quiet", "/norestart"], check=True, creationflags=flags)
            self.log("Cloudflared installation successful.", "SUCCESS", speak_text="Cloudflared installed successfully.")
            inject_cloudflared_path()
            try: os.remove(msi_path)
            except Exception: pass 
            self.cached_cf_path = None 
            self.gui_queue.put(("prompt_cf_token", None))
        except Exception as e:
            self.log(f"Cloudflared installation failed: {e}", "ERROR", speak_text="Warning. Cloudflared installation failed.")

    def _prompt_and_install_cf_service(self):
        token = simpledialog.askstring("Cloudflare Tunnel", "Enter your Cloudflare tunnel secret key to bind the service:", parent=self)
        if token:
            self.log("Installing background service...", "INFO")
            threading.Thread(target=self._install_cf_service_thread, args=(token.strip(),), daemon=True).start()
        else:
            self.log("Setup skipped. Tunnel will not auto-start.", "WARNING")
            self.check_system_health() 

    def _install_cf_service_thread(self, token):
        try:
            cf_exe = self._get_cloudflared_path() or "cloudflared"
            flags = subprocess.CREATE_NO_WINDOW if os.name == "nt" else 0
            subprocess.run([cf_exe, "service", "uninstall"], capture_output=True, creationflags=flags)
            proc = subprocess.run([cf_exe, "service", "install", token], capture_output=True, text=True, creationflags=flags)
            if proc.returncode == 0:
                self.log("Tunnel service bound successfully. It will now run in the background.", "SUCCESS", speak_text="Tunnel service successfully bound.")
            else:
                self.log(f"Cloudflared rejected the token: {proc.stderr.strip() or proc.stdout.strip()}", "ERROR")
        except Exception as e:
            self.log(f"Service install crashed: {e}", "ERROR")
        finally:
            self.check_system_health()

    # --------------------------------------------------------------------------
    # PYTHON MANAGEMENT
    # --------------------------------------------------------------------------
    def _offer_python_install(self):
        if messagebox.askyesno("Python Update Required", "Your Python version is too old. Download and install Python 3.14.6?", parent=self):
            threading.Thread(target=self._download_and_install_python, daemon=True).start()

    def _download_and_install_python(self):
        os.makedirs(EXE_DIR, exist_ok=True)
        py_path = os.path.join(EXE_DIR, "python-3.14.6-amd64.exe")
        try:
            self.log("Downloading Python Installer... please wait.", "INFO")
            urllib.request.urlretrieve("https://www.python.org/ftp/python/3.14.6/python-3.14.6-amd64.exe", py_path)
            self.log("Installing Python silently...", "WARNING")
            flags = subprocess.CREATE_NO_WINDOW if os.name == "nt" else 0
            subprocess.run([py_path, "/quiet", "InstallAllUsers=1", "PrependPath=1", "Include_test=0"], check=True, creationflags=flags)
            self.log("Python installation successful.", "SUCCESS", speak_text="Python successfully updated.")
            try: os.remove(py_path) 
            except Exception: pass
            self.gui_queue.put(("python_done", None))
        except Exception as e:
            self.log(f"Python installation failed: {e}", "ERROR")

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
            # Passing a unique tool ID that the child script can optionally read to split its taskbar icon
            env["EVENTHUB_TOOL_ID"] = f"EventHub.Tool.{key}" 
            
            proc = subprocess.Popen(
                [sys.executable, script_path], 
                cwd=APP_DIR,
                stdout=subprocess.PIPE, 
                stderr=subprocess.STDOUT, 
                stdin=subprocess.DEVNULL,
                text=True,
                encoding='utf-8',       
                errors='replace',       
                bufsize=1,              
                creationflags=subprocess.CREATE_NO_WINDOW if os.name == "nt" else 0,
                env=env
            )
            self.processes[key] = proc
            safe_name = tool['label'].replace('Terminal', '').replace('Manager', '')
            self.log(f"Tool started: {tool['label']}", "SUCCESS", speak_text=f"{safe_name} started.")
            self._set_tool_status(key, running=True)
            threading.Thread(target=self._stream_tool_logs, args=(proc, tool["label"]), daemon=True).start()
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
            if proc.stdout: proc.stdout.close()

    def stop_tool(self, tool):
        key = tool["key"]
        proc = self.processes.get(key)
        if proc and proc.poll() is None:
            try:
                if os.name == "nt":
                    subprocess.run(['taskkill', '/F', '/T', '/PID', str(proc.pid)], stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)
                else:
                    proc.terminate()
            except Exception: pass
            
            safe_name = tool['label'].replace('Terminal', '').replace('Manager', '')
            self.log(f"Tool stopped: {tool['label']}", "WARNING", speak_text=f"{safe_name} terminated.")
            self._set_tool_status(key, running=False)

    def stop_all_tools(self):
        count = sum(1 for key in list(self.processes.keys()) if self.processes[key].poll() is None and not self.stop_tool(next(t for t in TOOLS if t["key"] == key)))
        if count:
            self.log(f"Terminated {count} active tools.", "INFO", speak_text="All active tools terminated.")

    def _set_tool_status(self, key, running):
        widgets = self.tool_widgets.get(key)
        tool = next((t for t in TOOLS if t["key"] == key), None)
        if not widgets or not tool: return

        if running:
            color = getattr(self.style.colors, "success", "#4CD37E")
            widgets["status"].configure(text="🟢 RUNNING")
            self._animate_pulse(widgets["status"], "foreground", color)
            widgets["button"].configure(text="Stop", bootstyle=DANGER, command=lambda t=tool: self.stop_tool(t))
        else:
            widgets["status"].configure(text="⚫ IDLE", foreground="gray")
            widgets["button"].configure(text="Launch Tool", bootstyle=tool["bootstyle"], command=lambda t=tool: self.launch_tool(t))

    def _poll_processes(self):
        for key in list(self.processes.keys()):
            proc = self.processes[key]
            if proc.poll() is not None:
                tool = next((t for t in TOOLS if t["key"] == key), None)
                safe_name = tool['label'].replace('Terminal', '').replace('Manager', '')
                self.log(f"Tool exited unexpectedly: {tool['label']} (Code {proc.returncode})", "ERROR", speak_text=f"Warning. {safe_name} crashed.")
                del self.processes[key]
                self._set_tool_status(key, running=False)
        self.after(2000, self._poll_processes)

    # --------------------------------------------------------------------------
    # UTILS & SHUTDOWN
    # --------------------------------------------------------------------------
    def open_folder(self, path):
        os.makedirs(path, exist_ok=True)
        try:
            if os.name == "nt": os.startfile(path)
            elif sys.platform == "darwin": subprocess.Popen(["open", path])
            else: subprocess.Popen(["xdg-open", path])
        except Exception as e:
            self.log(f"Failed to open folder: {e}", "ERROR")

    def on_close(self):
        active = [k for k, p in self.processes.items() if p.poll() is None]
        if active and not messagebox.askyesno("Exit Launcher", f"{len(active)} tools are running invisibly.\n\nExit and shut them down?", parent=self):
            return
        self.stop_all_tools()
        self.destroy()

if __name__ == "__main__":
    # PREVENT TASKBAR MERGING: Give the Launcher its own unique Application ID
    if os.name == 'nt':
        try:
            my_app_id = 'EventHub.Portable.CentralLauncher.1.0'
            ctypes.windll.shell32.SetCurrentProcessExplicitAppUserModelID(my_app_id)
        except Exception:
            pass
            
    app = LauncherApp()
    app.protocol("WM_DELETE_WINDOW", app.on_close)
    app.mainloop()