#!/usr/bin/env python3
"""
EventHub Portable — Central Launcher
TENT DECOR EXPO UP 2026

Single entry point for the whole offline kit. 
Auto-installs dependencies, verifies system health, captures tool logs, 
and manages tool processes in a robust environment (Zero CMD Shells).
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
from datetime import datetime
import tkinter as tk

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
# FIRST-RUN BOOTSTRAP (100% Invisible)
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
    {"key": "hub", "icon": "🖥️", "label": "Command Center", "script": "server_hub.py", "desc": "Main hub — Flask API & live stats.", "bootstyle": PRIMARY},
    {"key": "gate_display", "icon": "📺", "label": "Gate Display Terminal", "script": "check_in.py", "desc": "Big-screen scan feed for gate entrance.", "bootstyle": INFO},
    {"key": "kiosk", "icon": "📝", "label": "Registration Kiosk", "script": "register.py", "desc": "Staffed walk-in registration desk.", "bootstyle": SUCCESS},
    {"key": "sync", "icon": "🔄", "label": "Sync Manager", "script": "sync_manager.py", "desc": "Pull/push Supabase, resolve conflicts.", "bootstyle": WARNING},
    {"key": "photos", "icon": "🖼️", "label": "Photo Downloader", "script": "photo_down.py", "desc": "Pull attendee photos for offline use.", "bootstyle": SECONDARY},
    {"key": "explorer", "icon": "🔍", "label": "Attendee Explorer", "script": "explorer.py", "desc": "Search and inspect attendee profiles.", "bootstyle": SECONDARY},
]

# ==============================================================================
# MAIN GUI APPLICATION
# ==============================================================================
class LauncherApp(ttk.Window):
    def __init__(self):
        super().__init__(themename="darkly", title="EventHub Portable — Central Launcher (Administrator)")
        
        sw, sh = self.winfo_screenwidth(), self.winfo_screenheight()
        ww, wh = max(1000, min(1400, int(sw * 0.85))), max(750, min(1000, int(sh * 0.85)))
        self.geometry(f"{ww}x{wh}+{max(0, (sw - ww) // 2)}+{max(0, (sh - wh) // 2 - 15)}")
        self.minsize(1000, 750)

        inject_cloudflared_path()

        self.gui_queue = queue.Queue()
        self.processes = {}      
        self.tool_widgets = {}   
        self.health_widgets = {}
        self.cached_cf_path = None
        self.team_img_original = None

        self._configure_custom_styles()
        self.build_ui()

        # Staggered startup for smooth UI loading
        self.after(50, self._process_gui_queue)
        self.after(500, self.check_system_health)
        self.after(2000, self._poll_processes)

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
    # UI CONSTRUCTION
    # --------------------------------------------------------------------------
    def build_ui(self):
        main_container = ttk.Frame(self, padding=25)
        main_container.pack(fill=BOTH, expand=True)

        # -- Header --
        header_frame = ttk.Frame(main_container)
        header_frame.pack(fill=X, pady=(0, 20))

        title_box = ttk.Frame(header_frame)
        title_box.pack(side=LEFT)
        ttk.Label(title_box, text="EventHub Portable", font="-size 24 -weight bold", bootstyle=PRIMARY).pack(anchor=W)
        ttk.Label(title_box, text="CENTRAL LAUNCHER • TDE UP 2026", font="-size 10 -weight bold", bootstyle=SECONDARY).pack(anchor=W)

        action_box = ttk.Frame(header_frame)
        action_box.pack(side=RIGHT)
        ttk.Button(action_box, text="⟳ Refresh Health Check", bootstyle="outline-info", command=self.check_system_health).pack(side=LEFT, padx=5)
        ttk.Button(action_box, text="🛑 Stop All Active Tools", bootstyle=DANGER, command=self.stop_all_tools).pack(side=LEFT, padx=5)

        # -- System Health Panel (Cards) --
        ttk.Label(main_container, text="⚙️ SYSTEM HEALTH", font="-size 11 -weight bold", foreground="gray").pack(anchor=W, pady=(0, 5))
        health_grid = ttk.Frame(main_container)
        health_grid.pack(fill=X, pady=(0, 15))
        
        self._build_health_card(health_grid, "python", "🐍 PYTHON VER", "Checking...")
        self._build_health_card(health_grid, "cloudflared", "☁️ CLOUDFLARED", "Checking...")
        self._build_health_card(health_grid, "deps", "📦 DEPENDENCIES", "Checking...")
        self._build_health_card(health_grid, "config", "⚙️ CONFIGURATION", "Checking...")

        # -- Main Split Content --
        split_frame = ttk.Frame(main_container)
        split_frame.pack(fill=BOTH, expand=True)

        # Left: DB Health + Tools 
        left_col = ttk.Frame(split_frame, width=480)
        left_col.pack(side=LEFT, fill=Y, padx=(0, 20))
        left_col.pack_propagate(False)

        # 🗄️ Database Health (Clean Text Cards instead of Speedometers)
        ttk.Label(left_col, text="🗄️ DATABASE HEALTH", font="-size 10 -weight bold", foreground="gray").pack(anchor=W, pady=(0, 5))
        db_grid = ttk.Frame(left_col)
        db_grid.pack(fill=X, pady=(0, 15))
        
        # Uses a slightly modified health card builder to add a right margin on the first card
        self._build_db_status_card(db_grid, "mysql_db", "🐬 MYSQL (PRIMARY)", "Checking...")
        self._build_db_status_card(db_grid, "sqlite_db", "💾 SQLITE (MIRROR)", "Checking...")

        # 🛠️ Application Tools
        ttk.Label(left_col, text="🛠️ APPLICATION TOOLS", font="-size 10 -weight bold", foreground="gray").pack(anchor=W, pady=(0, 5))
        for tool in TOOLS:
            self._build_tool_card(left_col, tool)
            
        btn_row = ttk.Frame(left_col)
        btn_row.pack(fill=X, side=BOTTOM, pady=(10, 0))
        ttk.Button(btn_row, text="📁 Open Root", bootstyle="outline-secondary", command=lambda: self.open_folder(ROOT_DIR)).pack(side=LEFT, fill=X, expand=True, padx=(0, 5))
        ttk.Button(btn_row, text="⚙️ Configs", bootstyle="outline-secondary", command=lambda: self.open_folder(CONFIG_DIR)).pack(side=LEFT, fill=X, expand=True, padx=(5, 0))

        # Right: Banner & Logs
        right_col = ttk.Frame(split_frame)
        right_col.pack(side=LEFT, fill=BOTH, expand=True)

        # -- Dynamic Team Banner Card --
        self.team_card = ttk.Frame(right_col, style="Card.TFrame", padding=2) 
        self.team_card.pack(fill=X, pady=(0, 15))
        self.team_card.pack_propagate(False) 
        
        self.lbl_team_photo = ttk.Label(self.team_card, background=self.CARD_BG, anchor=CENTER)
        self.lbl_team_photo.pack(fill=BOTH, expand=True)

        team_img_path = os.path.join(ROOT_DIR, "team.png")
        try:
            if os.path.exists(team_img_path):
                self.team_img_original = Image.open(team_img_path)
                self.team_card.bind("<Configure>", self._resize_team_banner)
            else:
                self.team_card.configure(height=100) 
                self.lbl_team_photo.configure(
                    text="📸 Place 'team.png' in the root folder to view your team banner.", 
                    font="-size 10 -slant italic", 
                    foreground="gray"
                )
        except Exception as e:
            self.team_card.configure(height=100)
            self.lbl_team_photo.configure(text=f"Image Error: {e}", foreground="#FF6B6B")

        # -- Log Console --
        log_hdr = ttk.Frame(right_col)
        log_hdr.pack(fill=X, pady=(0, 5))
        ttk.Label(log_hdr, text="📟 ACTIVITY LOG (STDOUT)", font="-size 11 -weight bold", foreground="gray").pack(side=LEFT)
        ttk.Button(log_hdr, text="Clear", bootstyle="secondary-link", command=self.clear_log).pack(side=RIGHT)

        log_frame = ttk.Frame(right_col, style="Card.TFrame", padding=2)
        log_frame.pack(fill=BOTH, expand=True)

        self.log_box = ScrolledText(log_frame, autohide=True, wrap="word")
        self.log_box.pack(fill=BOTH, expand=True, padx=2, pady=2)
        self.log_box.text.configure(state="disabled", font=("Consolas", 10), bg="#1e1e1e", borderwidth=0)
        
        self.log_box.text.tag_config("INFO", foreground="#cccccc")
        self.log_box.text.tag_config("SUCCESS", foreground="#4CD37E", font=("Consolas", 10, "bold"))
        self.log_box.text.tag_config("WARNING", foreground="#FFB454", font=("Consolas", 10, "bold"))
        self.log_box.text.tag_config("ERROR", foreground="#FF6B6B", font=("Consolas", 10, "bold"))
        self.log_box.text.tag_config("TOOL", foreground="#5DADE2") 

        self.log("System initialized with Administrator Privileges. Ready for operations.", "SUCCESS")

    def _resize_team_banner(self, event):
        """Dynamically scales and crops the team banner based on real image aspect ratio."""
        if not self.team_img_original or event.width <= 10:
            return
            
        # Debounce to prevent stuttering
        if hasattr(self, '_last_banner_w') and abs(self._last_banner_w - event.width) < 15:
            return
        self._last_banner_w = event.width

        try:
            original_w, original_h = self.team_img_original.size
            tw = event.width - 4
            th = int(tw * (original_h / original_w))
            
            max_height = 320
            if th > max_height:
                th = max_height
                
            self.team_card.configure(height=th + 4)
            img = ImageOps.fit(self.team_img_original, (tw, th), Image.Resampling.LANCZOS)
            photo = ImageTk.PhotoImage(img)
            self.lbl_team_photo.configure(image=photo)
            self.lbl_team_photo.image = photo 
        except Exception:
            pass

    def _build_health_card(self, parent, key, title, initial_val):
        card = ttk.Frame(parent, style="Card.TFrame", padding=(12, 10))
        card.pack(side=LEFT, fill=X, expand=True, padx=4)
        
        top = ttk.Frame(card, style="Card.TFrame")
        top.pack(fill=X)
        
        canvas = tk.Canvas(top, width=12, height=12, bg=self.CARD_BG, highlightthickness=0)
        dot = canvas.create_oval(2, 2, 10, 10, fill="#757575", outline="")
        canvas.pack(side=LEFT, padx=(0, 5))
        
        ttk.Label(top, text=title, font="-size 8 -weight bold", background=self.CARD_BG, foreground="gray").pack(side=LEFT)
        
        val_lbl = ttk.Label(card, text=initial_val, font="-size 11 -weight bold", background=self.CARD_BG, foreground="gray")
        val_lbl.pack(anchor=W, pady=(4, 0))
        
        self.health_widgets[key] = {"label": val_lbl, "canvas": canvas, "dot": dot}

    def _build_db_status_card(self, parent, key, title, initial_val):
        card = ttk.Frame(parent, style="Card.TFrame", padding=(12, 10))
        # Adds right margin to the first card (mysql) to create a gap between them
        card.pack(side=LEFT, fill=X, expand=True, padx=(0, 8) if key == "mysql_db" else (0, 0))
        
        top = ttk.Frame(card, style="Card.TFrame")
        top.pack(fill=X)
        
        canvas = tk.Canvas(top, width=12, height=12, bg=self.CARD_BG, highlightthickness=0)
        dot = canvas.create_oval(2, 2, 10, 10, fill="#757575", outline="")
        canvas.pack(side=LEFT, padx=(0, 5))
        
        ttk.Label(top, text=title, font="-size 8 -weight bold", background=self.CARD_BG, foreground="gray").pack(side=LEFT)
        
        val_lbl = ttk.Label(card, text=initial_val, font="-size 11 -weight bold", background=self.CARD_BG, foreground="gray")
        val_lbl.pack(anchor=W, pady=(4, 0))
        
        self.health_widgets[key] = {"label": val_lbl, "canvas": canvas, "dot": dot}

    def _build_tool_card(self, parent, tool):
        card = ttk.Frame(parent, style="Card.TFrame", padding=12)
        card.pack(fill=X, pady=4)

        inner = ttk.Frame(card, style="Card.TFrame")
        inner.pack(fill=BOTH, expand=True)

        ttk.Label(inner, text=tool["icon"], font="-size 20", background=self.CARD_BG).grid(row=0, column=0, rowspan=2, padx=(5, 15), sticky=W)
        ttk.Label(inner, text=tool["label"], font="-size 11 -weight bold", background=self.CARD_BG).grid(row=0, column=1, sticky=W)
        
        ttk.Label(inner, text=tool["desc"], font="-size 9", background=self.CARD_BG, foreground="gray", wraplength=220).grid(row=1, column=1, sticky=W)

        status_lbl = ttk.Label(inner, text="⚫ IDLE", font="-size 9 -weight bold", background=self.CARD_BG, foreground="gray", width=12, anchor=CENTER)
        status_lbl.grid(row=0, column=2, rowspan=2, padx=10)

        btn = ttk.Button(inner, text="Launch Tool", bootstyle=tool["bootstyle"], width=14, command=lambda t=tool: self.launch_tool(t))
        btn.grid(row=0, column=3, rowspan=2)

        inner.columnconfigure(1, weight=1)
        self.tool_widgets[tool["key"]] = {"button": btn, "status": status_lbl}

    # --------------------------------------------------------------------------
    # QUEUE & LOGGING (Thread-Safe UI Updates)
    # --------------------------------------------------------------------------
    def log(self, message, level="INFO"):
        self.gui_queue.put(("log", {"msg": message, "level": level}))

    def _process_gui_queue(self):
        """Immortal Queue Processing — wrapped in a try/except to survive bad data"""
        for _ in range(100):
            try:
                kind, payload = self.gui_queue.get_nowait()
                
                if kind == "log":
                    self._append_log(payload["msg"], payload["level"])
                    
                elif kind == "update_health":
                    w = self.health_widgets.get(payload["key"])
                    if w:
                        color = getattr(self.style.colors, payload["style"], "#757575")
                        w["label"].configure(text=payload["text"], foreground=color)
                        w["canvas"].itemconfig(w["dot"], fill=color)
                        w["canvas"].coords(w["dot"], 1, 1, 11, 11)
                        self.after(200, lambda cv=w["canvas"], dt=w["dot"]: cv.coords(dt, 2, 2, 10, 10) if cv.winfo_exists() else None)
                        
                elif kind == "prompt_cf_token":
                    self._prompt_and_install_cf_service()
                elif kind == "python_done":
                    messagebox.showinfo("Python Update Complete", "Python 3.14.6 installed. Please restart the app.", parent=self)
                    
            except queue.Empty:
                break
            except Exception as e:
                try: self._append_log(f"GUI Queue Error suppressed: {e}", "ERROR")
                except Exception: pass
                
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
    # SYSTEM HEALTH & VERSION CHECKS
    # --------------------------------------------------------------------------
    def _set_health(self, key, text, style):
        self.gui_queue.put(("update_health", {"key": key, "text": text, "style": style}))

    def check_system_health(self):
        self.log("Running system health & version checks...", "INFO")
        
        # 1. Python Version Check
        py_ver = f"{sys.version_info.major}.{sys.version_info.minor}.{sys.version_info.micro}"
        if sys.version_info[:2] < MIN_PYTHON:
            self._set_health("python", f"v{py_ver} (Needs {MIN_PYTHON[0]}.{MIN_PYTHON[1]}+) ⚠", "warning")
            self._offer_python_install()
        else:
            self._set_health("python", f"v{py_ver} ✓", "success")

        # 2. Config Check
        if os.path.isfile(SCHEMA_CONFIG) and os.path.isfile(SECRETS_CONFIG):
            self._set_health("config", "Valid ✓", "success")
        else:
            self._set_health("config", "Missing ⚠", "warning")

        # 3. Pip Dependencies Check
        self._set_health("deps", "Verifying...", "warning")
        threading.Thread(target=self._install_requirements_thread, daemon=True).start()

        # 4. Cloudflared Check
        self._set_health("cloudflared", "Verifying...", "warning")
        threading.Thread(target=self._verify_cloudflared_thread, daemon=True).start()
        
        # 5. Database Status Check
        self._set_health("mysql_db", "Pinging...", "warning")
        self._set_health("sqlite_db", "Pinging...", "warning")
        threading.Thread(target=self._verify_databases_thread, daemon=True).start()

    def _verify_databases_thread(self):
        """Lightweight check to test MySQL and SQLite latency without breaking UI."""
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

        # 1. Test MySQL (Lightweight Socket Ping)
        my_conf = config.get("mysql", {})
        if my_conf.get("enabled"):
            try:
                start_t = time.perf_counter()
                conn = pymysql.connect(
                    host=my_conf.get("host", "localhost"),
                    user=my_conf.get("user", "root"),
                    password=my_conf.get("password", ""),
                    database=my_conf.get("database", "eventhub_db"),
                    port=my_conf.get("port", 3306),
                    connect_timeout=2
                )
                conn.ping(reconnect=False)
                conn.close()
                ms = int((time.perf_counter() - start_t) * 1000)
                status_text = f"Online ✓ ({ms}ms)"
                self._set_health("mysql_db", status_text, "success" if ms < 100 else "warning")
            except Exception:
                self._set_health("mysql_db", "Offline ⚠", "danger")
        else:
            self._set_health("mysql_db", "Disabled", "secondary")

        # 2. Test SQLite
        sq_conf = config.get("sqlite", {})
        if sq_conf.get("enabled"):
            db_file = os.path.join(APP_DIR, sq_conf.get("folder_name", "db"), sq_conf.get("file_name", "eventhub_local.db"))
            if os.path.exists(db_file):
                try:
                    start_t = time.perf_counter()
                    conn = sqlite3.connect(db_file)
                    conn.execute("SELECT name FROM sqlite_master WHERE type='table';")
                    conn.close()
                    ms = int((time.perf_counter() - start_t) * 1000)
                    ms_text = "<1ms" if ms == 0 else f"{ms}ms"
                    self._set_health("sqlite_db", f"Ready ✓ ({ms_text})", "success")
                except Exception:
                    self._set_health("sqlite_db", "Corrupted ⚠", "danger")
            else:
                self._set_health("sqlite_db", "Missing DB ⚠", "warning")
        else:
            self._set_health("sqlite_db", "Disabled", "secondary")

    def _install_requirements_thread(self):
        try:
            flags = subprocess.CREATE_NO_WINDOW if os.name == "nt" else 0
            cmd = [sys.executable, "-m", "pip", "install", "-r", REQUIREMENTS_FILE, "--disable-pip-version-check"]
            proc = subprocess.run(cmd, cwd=ROOT_DIR, capture_output=True, text=True, creationflags=flags)
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
        if shutil.which("cloudflared"):
            self.cached_cf_path = "cloudflared"
            return "cloudflared"
        for base in [os.environ.get("ProgramFiles", "C:\\Program Files"), os.environ.get("ProgramFiles(x86)", "C:\\Program Files (x86)")]:
            guess = os.path.join(base, "cloudflared", "cloudflared.exe")
            if os.path.exists(guess):
                self.cached_cf_path = "cloudflared" 
                return "cloudflared"
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
                ver = match.group(1) if match else "OK"
                self._set_health("cloudflared", f"v{ver} ✓", "success")
            else:
                self._set_health("cloudflared", "Broken ⚠", "danger")
        except Exception as e:
            self.log(f"Cloudflared checking error: {e}", "ERROR")
            self._set_health("cloudflared", "Broken ⚠", "danger")

    def _offer_cloudflared_install(self):
        if messagebox.askyesno("Cloudflared Missing", "Cloudflared is required for the tunnel. Download and install it now?", parent=self):
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
            self.log("Cloudflared installation successful.", "SUCCESS")
            
            inject_cloudflared_path()
            try: os.remove(msi_path)
            except Exception: pass 
            
            self.cached_cf_path = None 
            self.gui_queue.put(("prompt_cf_token", None))
        except Exception as e:
            self.log(f"Cloudflared installation failed: {e}", "ERROR")

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
            
            self.log("Clearing any old tunnel configurations...", "INFO")
            subprocess.run([cf_exe, "service", "uninstall"], capture_output=True, creationflags=flags)
            
            proc = subprocess.run([cf_exe, "service", "install", token], capture_output=True, text=True, creationflags=flags)
            
            if proc.returncode == 0:
                self.log("Tunnel service bound successfully. It will now run in the background.", "SUCCESS")
                self.log("Note: You do NOT need to click 'Start Tunnel' in the Command Center anymore. It is running automatically.", "WARNING")
            else:
                error_output = proc.stderr.strip() or proc.stdout.strip()
                self.log(f"Cloudflared rejected the token: {error_output}", "ERROR")
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
        py_url = "https://www.python.org/ftp/python/3.14.6/python-3.14.6-amd64.exe"

        try:
            self.log("Downloading Python Installer... please wait.", "INFO")
            urllib.request.urlretrieve(py_url, py_path)
            self.log("Installing Python silently...", "WARNING")

            flags = subprocess.CREATE_NO_WINDOW if os.name == "nt" else 0
            subprocess.run([py_path, "/quiet", "InstallAllUsers=1", "PrependPath=1", "Include_test=0"], check=True, creationflags=flags)
            self.log("Python installation successful.", "SUCCESS")

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
            self.log(f"Tool started: {tool['label']}", "SUCCESS")
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
            
            self.log(f"Tool stopped: {tool['label']}", "WARNING")
            self._set_tool_status(key, running=False)

    def stop_all_tools(self):
        count = sum(1 for key in list(self.processes.keys()) if self.processes[key].poll() is None and not self.stop_tool(next(t for t in TOOLS if t["key"] == key)))
        if count:
            self.log(f"Terminated {count} active tools.", "INFO")

    def _set_tool_status(self, key, running):
        widgets = self.tool_widgets.get(key)
        tool = next((t for t in TOOLS if t["key"] == key), None)
        if not widgets or not tool: return

        if running:
            color = getattr(self.style.colors, "success", "#4CD37E")
            widgets["status"].configure(text="🟢 RUNNING", foreground=color)
            widgets["button"].configure(text="Stop", bootstyle=DANGER, command=lambda t=tool: self.stop_tool(t))
        else:
            widgets["status"].configure(text="⚫ IDLE", foreground="gray")
            widgets["button"].configure(text="Launch Tool", bootstyle=tool["bootstyle"], command=lambda t=tool: self.launch_tool(t))

    def _poll_processes(self):
        for key in list(self.processes.keys()):
            proc = self.processes[key]
            if proc.poll() is not None:
                tool = next((t for t in TOOLS if t["key"] == key), None)
                self.log(f"Tool exited unexpectedly: {tool['label']} (Code {proc.returncode})", "ERROR")
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
    app = LauncherApp()
    app.protocol("WM_DELETE_WINDOW", app.on_close)
    app.mainloop()