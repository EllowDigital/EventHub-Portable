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
from datetime import datetime

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
    # Force pythonw.exe to completely hide the background console window upon elevation
    executable = sys.executable
    if executable.lower().endswith("python.exe"):
        executable = executable[:-10] + "pythonw.exe"
        
    # Relaunch the script with an Administrator UAC prompt, fully hidden terminal
    ctypes.windll.shell32.ShellExecuteW(
        None, "runas", executable, " ".join([f'"{arg}"' for arg in sys.argv]), None, 1
    )
    sys.exit() # Exit the non-admin process

# We only import GUI modules AFTER ensuring we have admin rights to prevent double-windows
try:
    from ttkbootstrap.widgets.scrolled import ScrolledText
    from ttkbootstrap.constants import *
    import ttkbootstrap as ttk
    from tkinter import messagebox, simpledialog
except ImportError:
    pass # Will be handled by _bootstrap_first_run()

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

# ==============================================================================
# ENVIRONMENT INJECTION
# ==============================================================================
def inject_cloudflared_path():
    """
    Injects standard MSI install locations into the current session's PATH.
    Prepends to ensure it takes priority, allowing child processes (like server_hub) 
    to simply call "cloudflared" without needing absolute paths.
    """
    cf_paths = [r"C:\Program Files (x86)\cloudflared", r"C:\Program Files\cloudflared"]
    current_path = os.environ.get("PATH", "")
    
    for path in cf_paths:
        if os.path.exists(path) and path not in current_path:
            # Prepend the path so it takes highest priority
            os.environ["PATH"] = path + os.pathsep + os.environ["PATH"]

# ==============================================================================
# FIRST-RUN BOOTSTRAP (100% Invisible)
# ==============================================================================
def _bootstrap_first_run():
    """Installs ttkbootstrap silently if missing so the UI can launch."""
    try:
        import ttkbootstrap  # noqa: F401
        return
    except ImportError:
        pass

    if not os.path.isfile(REQUIREMENTS_FILE):
        sys.exit(1) # Cannot proceed without requirements

    flags = subprocess.CREATE_NO_WINDOW if os.name == 'nt' else 0
    
    # Install dependencies silently
    subprocess.call(
        [sys.executable, "-m", "pip", "install", "-r", REQUIREMENTS_FILE, "--disable-pip-version-check"], 
        cwd=ROOT_DIR, 
        creationflags=flags,
        stdout=subprocess.DEVNULL,
        stderr=subprocess.DEVNULL
    )
    
    # Relaunch UI silently
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
        self.geometry("1100x950")
        self.minsize(1000, 800)

        # Inject PATH on startup
        inject_cloudflared_path()

        self.gui_queue = queue.Queue()
        self.processes = {}      
        self.tool_widgets = {}   
        self.cached_cf_path = None

        self.build_ui()

        # Staggered startup for smooth UI loading
        self.after(100, self._process_gui_queue)
        self.after(600, self.check_system_health)
        self.after(2000, self._poll_processes)

    # --------------------------------------------------------------------------
    # UI CONSTRUCTION
    # --------------------------------------------------------------------------
    def build_ui(self):
        # -- Header --
        header_frame = ttk.Frame(self, padding=(30, 25, 30, 10))
        header_frame.pack(fill=X)

        ttk.Label(header_frame, text="EventHub Portable", font="-size 26 -weight bold").pack(side=LEFT)
        ttk.Button(header_frame, text="⟳ Refresh Health Check", bootstyle=(OUTLINE, INFO), command=self.check_system_health).pack(side=RIGHT, pady=5)
        ttk.Label(header_frame, text="TENT DECOR EXPO UP 2026", font="-size 12", bootstyle=PRIMARY).pack(anchor=W, pady=(5, 0))

        # -- System Health Panel --
        health_frame = ttk.Labelframe(self, text="System Health & Versions", padding=15)
        health_frame.pack(fill=X, padx=30, pady=(10, 20))
        
        grid_frame = ttk.Frame(health_frame)
        grid_frame.pack(fill=X)

        self.lbl_python = ttk.Label(grid_frame, text="Python: checking...", font="-size 10")
        self.lbl_python.grid(row=0, column=0, padx=(0, 40), sticky=W)

        self.lbl_cloudflared = ttk.Label(grid_frame, text="Cloudflared: checking...", font="-size 10")
        self.lbl_cloudflared.grid(row=0, column=1, padx=(0, 40), sticky=W)

        self.lbl_deps = ttk.Label(grid_frame, text="Dependencies: checking...", font="-size 10")
        self.lbl_deps.grid(row=0, column=2, padx=(0, 40), sticky=W)

        self.lbl_config = ttk.Label(grid_frame, text="Configuration: checking...", font="-size 10")
        self.lbl_config.grid(row=0, column=3, sticky=W)

        # -- Tool Control Grid --
        tools_frame = ttk.Frame(self, padding=(30, 0, 30, 10))
        tools_frame.pack(fill=BOTH, expand=True)

        for tool in TOOLS:
            self._build_tool_card(tools_frame, tool)

        # -- Action Bar --
        action_bar = ttk.Frame(self, padding=(30, 15, 30, 5))
        action_bar.pack(fill=X)

        ttk.Button(action_bar, text="📁 Project Root", bootstyle=(OUTLINE, SECONDARY), command=lambda: self.open_folder(ROOT_DIR)).pack(side=LEFT, padx=(0, 10))
        ttk.Button(action_bar, text="⚙️ Config Folder", bootstyle=(OUTLINE, SECONDARY), command=lambda: self.open_folder(CONFIG_DIR)).pack(side=LEFT)
        ttk.Button(action_bar, text="🛑 Stop All Active Tools", bootstyle=DANGER, command=self.stop_all_tools).pack(side=RIGHT, padx=(10, 0))
        ttk.Button(action_bar, text="🗑️ Clear Log", bootstyle=(OUTLINE, SECONDARY), command=self.clear_log).pack(side=RIGHT)

        # -- Activity Log --
        log_frame = ttk.Labelframe(self, text="Activity Log (System & Tool Output)", padding=10)
        log_frame.pack(fill=BOTH, expand=True, padx=30, pady=(10, 25))

        self.log_box = ScrolledText(log_frame, autohide=True, wrap="word")
        self.log_box.pack(fill=BOTH, expand=True)
        self.log_box.text.configure(state="disabled", font=("Consolas", 10), bg="#1e1e1e")
        
        # Log color tags
        self.log_box.text.tag_config("INFO", foreground="#cccccc")
        self.log_box.text.tag_config("SUCCESS", foreground="#5cb85c", font=("Consolas", 10, "bold"))
        self.log_box.text.tag_config("WARNING", foreground="#f0ad4e", font=("Consolas", 10, "bold"))
        self.log_box.text.tag_config("ERROR", foreground="#d9534f", font=("Consolas", 10, "bold"))
        self.log_box.text.tag_config("TOOL", foreground="#5bc0de") # Color for tool stdout streams

        self.log("System initialized with Administrator Privileges. Ready for operations.", "SUCCESS")

    def _build_tool_card(self, parent, tool):
        card = ttk.Frame(parent, relief="solid", borderwidth=1, padding=2)
        card.pack(fill=X, pady=5)

        inner = ttk.Frame(card, padding=15)
        inner.pack(fill=BOTH, expand=True)

        ttk.Label(inner, text=tool["icon"], font="-size 24").grid(row=0, column=0, rowspan=2, padx=(5, 20), sticky=W)
        ttk.Label(inner, text=tool["label"], font="-size 12 -weight bold").grid(row=0, column=1, sticky=W)
        ttk.Label(inner, text=tool["desc"], font="-size 10", bootstyle=SECONDARY).grid(row=1, column=1, sticky=W)

        status_lbl = ttk.Label(inner, text="⚫ IDLE", font="-size 10 -weight bold", bootstyle=SECONDARY, width=12, anchor=CENTER)
        status_lbl.grid(row=0, column=2, rowspan=2, padx=20)

        btn = ttk.Button(inner, text="Launch Tool", bootstyle=tool["bootstyle"], width=15, command=lambda t=tool: self.launch_tool(t))
        btn.grid(row=0, column=3, rowspan=2, padx=(5, 5))

        inner.columnconfigure(1, weight=1)
        self.tool_widgets[tool["key"]] = {"button": btn, "status": status_lbl}

    # --------------------------------------------------------------------------
    # QUEUE & LOGGING (Thread-Safe UI Updates)
    # --------------------------------------------------------------------------
    def log(self, message, level="INFO"):
        self.gui_queue.put(("log", {"msg": message, "level": level}))

    def _process_gui_queue(self):
        try:
            for _ in range(50):
                kind, payload = self.gui_queue.get_nowait()
                
                if kind == "log":
                    self._append_log(payload["msg"], payload["level"])
                elif kind == "deps_done":
                    if payload:
                        self.lbl_deps.configure(text="Dependencies: Ready ✓", bootstyle=SUCCESS)
                    else:
                        self.lbl_deps.configure(text="Dependencies: Failed ⚠", bootstyle=DANGER)
                elif kind == "cf_checked":
                    self._update_cf_ui(payload)
                elif kind == "prompt_cf_token":
                    self._prompt_and_install_cf_service()
                elif kind == "python_done":
                    messagebox.showinfo("Python Update Complete", "Python 3.14.6 installed. Please restart the app.", parent=self)
        except queue.Empty:
            pass
        self.after(100, self._process_gui_queue) # Light on CPU

    def _append_log(self, message, level):
        timestamp = datetime.now().strftime("%H:%M:%S")
        formatted_msg = f"[{timestamp}] {message}\n"
        self.log_box.text.configure(state="normal")
        self.log_box.text.insert(END, formatted_msg, level)
        self.log_box.text.see(END)
        self.log_box.text.configure(state="disabled")

    def clear_log(self):
        self.log_box.text.configure(state="normal")
        self.log_box.text.delete("1.0", END)
        self.log_box.text.configure(state="disabled")

    # --------------------------------------------------------------------------
    # SYSTEM HEALTH & VERSION CHECKS
    # --------------------------------------------------------------------------
    def check_system_health(self):
        self.log("Running system health & version checks...", "INFO")
        
        # 1. Python Version Check
        py_ver = f"{sys.version_info.major}.{sys.version_info.minor}.{sys.version_info.micro}"
        if sys.version_info[:2] < MIN_PYTHON:
            self.lbl_python.configure(text=f"Python: v{py_ver} (Needs {MIN_PYTHON[0]}.{MIN_PYTHON[1]}+) ⚠", bootstyle=WARNING)
            self._offer_python_install()
        else:
            self.lbl_python.configure(text=f"Python: v{py_ver} ✓", bootstyle=SUCCESS)

        # 2. Config Check
        if os.path.isfile(SCHEMA_CONFIG) and os.path.isfile(SECRETS_CONFIG):
            self.lbl_config.configure(text="Configuration: Valid ✓", bootstyle=SUCCESS)
        else:
            self.lbl_config.configure(text="Configuration: Missing ⚠", bootstyle=WARNING)

        # 3. Pip Dependencies Check
        self.lbl_deps.configure(text="Dependencies: Verifying...", bootstyle=WARNING)
        threading.Thread(target=self._install_requirements_thread, daemon=True).start()

        # 4. Cloudflared Check (Threaded to prevent freeze)
        self.lbl_cloudflared.configure(text="Cloudflared: Verifying...", bootstyle=WARNING)
        threading.Thread(target=self._verify_cloudflared_thread, daemon=True).start()

    def _install_requirements_thread(self):
        try:
            flags = subprocess.CREATE_NO_WINDOW if os.name == "nt" else 0
            cmd = [sys.executable, "-m", "pip", "install", "-r", REQUIREMENTS_FILE, "--disable-pip-version-check"]
            proc = subprocess.run(cmd, cwd=ROOT_DIR, capture_output=True, text=True, creationflags=flags)
            if proc.returncode == 0:
                self.log("Python dependencies verified.", "SUCCESS")
                self.gui_queue.put(("deps_done", True))
            else:
                self.log(f"Dependency error: {proc.stderr}", "ERROR")
                self.gui_queue.put(("deps_done", False))
        except Exception as e:
            self.log(f"Failed to check dependencies: {e}", "ERROR")
            self.gui_queue.put(("deps_done", False))

    # --------------------------------------------------------------------------
    # CLOUDFLARED MANAGEMENT
    # --------------------------------------------------------------------------
    def _get_cloudflared_path(self):
        if self.cached_cf_path and os.path.exists(self.cached_cf_path):
            return self.cached_cf_path
            
        cf_path = shutil.which("cloudflared")
        if cf_path:
            self.cached_cf_path = cf_path
            return cf_path
        
        # Check standard MSI install locations (Path variable lag fix)
        for base in [os.environ.get("ProgramFiles(x86)", "C:\\Program Files (x86)"), os.environ.get("ProgramFiles", "C:\\Program Files")]:
            guess = os.path.join(base, "cloudflared", "cloudflared.exe")
            if os.path.exists(guess):
                self.cached_cf_path = guess
                return guess
        return None

    def _verify_cloudflared_thread(self):
        cf_exe = self._get_cloudflared_path()
        if not cf_exe:
            self.gui_queue.put(("cf_checked", {"status": "missing"}))
            return

        try:
            flags = subprocess.CREATE_NO_WINDOW if os.name == "nt" else 0
            # 100% Assurance Test: Run it and parse output
            res = subprocess.run(
                [cf_exe, "--version"], capture_output=True, text=True, timeout=3,
                creationflags=flags
            )
            if res.returncode == 0:
                # Extract version string (e.g. "cloudflared version 2024.x.x")
                match = re.search(r"version\s+(\d+\.\d+\.\d+)", res.stdout)
                ver = match.group(1) if match else "OK"
                self.gui_queue.put(("cf_checked", {"status": "ok", "version": ver}))
            else:
                self.gui_queue.put(("cf_checked", {"status": "broken"}))
        except Exception:
            self.gui_queue.put(("cf_checked", {"status": "broken"}))

    def _update_cf_ui(self, payload):
        status = payload["status"]
        if status == "ok":
            self.lbl_cloudflared.configure(text=f"Cloudflared: v{payload['version']} ✓", bootstyle=SUCCESS)
        elif status == "missing":
            self.lbl_cloudflared.configure(text="Cloudflared: Missing ⚠", bootstyle=WARNING)
            self._offer_cloudflared_install()
        else:
            self.lbl_cloudflared.configure(text="Cloudflared: Broken ⚠", bootstyle=DANGER)
            self.log("Cloudflared executable found, but failed to execute properly.", "ERROR")

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
            # /quiet prevents ALL UI popups from the installer
            subprocess.run(["msiexec.exe", "/i", msi_path, "/quiet", "/norestart"], check=True, creationflags=flags)
            self.log("Cloudflared installation successful.", "SUCCESS")
            
            # Instantly update environment PATH so child tools can find it
            inject_cloudflared_path()

            try:
                os.remove(msi_path)
            except Exception: pass # Cleanup silent fail
            
            self.cached_cf_path = None # Reset cache
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
            self.check_system_health() # Refresh UI

    def _install_cf_service_thread(self, token):
        try:
            cf_exe = self._get_cloudflared_path() or "cloudflared.exe"
            flags = subprocess.CREATE_NO_WINDOW if os.name == "nt" else 0
            
            self.log("Clearing any old tunnel configurations...", "INFO")
            # 1. Quietly uninstall any existing service so it doesn't block the new one
            subprocess.run([cf_exe, "service", "uninstall"], capture_output=True, creationflags=flags)
            
            # 2. Install the new token, capturing the actual error output
            proc = subprocess.run(
                [cf_exe, "service", "install", token], 
                capture_output=True, 
                text=True, 
                creationflags=flags
            )
            
            if proc.returncode == 0:
                self.log("Tunnel service bound successfully. It will now run in the background.", "SUCCESS")
                self.log("Note: You do NOT need to click 'Start Tunnel' in the Command Center anymore. It is running automatically.", "WARNING")
            else:
                # Capture Cloudflared's actual error message instead of a generic Python error
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
            # /quiet runs completely invisibly
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
        if key in self.processes and self.processes[key].poll() is None:
            return

        script_path = os.path.join(APP_DIR, tool["script"])
        if not os.path.isfile(script_path):
            self.log(f"Cannot find script: {tool['script']}", "ERROR")
            return

        try:
            # 🛡️ 100% Error-Free Output capturing & Zero CMD window creation
            proc = subprocess.Popen(
                [sys.executable, script_path], 
                cwd=APP_DIR,
                stdout=subprocess.PIPE, 
                stderr=subprocess.STDOUT, 
                stdin=subprocess.DEVNULL,
                text=True,
                encoding='utf-8',       # Prevents unicode crashes
                errors='replace',       # Safely handles weird characters
                bufsize=1,              # Line-buffered streaming
                creationflags=subprocess.CREATE_NO_WINDOW if os.name == "nt" else 0
            )
            self.processes[key] = proc
            self.log(f"Tool started: {tool['label']}", "SUCCESS")
            self._set_tool_status(key, running=True)
            
            # Start background thread to continuously stream tool logs to UI
            threading.Thread(target=self._stream_tool_logs, args=(proc, tool["label"]), daemon=True).start()
            
        except Exception as e:
            self.log(f"Failed to launch {tool['label']}: {e}", "ERROR")

    def _stream_tool_logs(self, proc, tool_name):
        """Background thread reads a running tool's stdout line-by-line safely."""
        try:
            for line in iter(proc.stdout.readline, ''):
                if line:
                    clean_line = line.strip()
                    if clean_line:
                        # Strip raw ANSI terminal color codes (like [0m)
                        clean_line = re.sub(r'\x1b\[[0-9;]*m', '', clean_line)
                        self.log(f"[{tool_name}] {clean_line}", "TOOL")
        except Exception as e:
            self.log(f"[{tool_name}] Log stream interrupted: {e}", "WARNING")
        finally:
            if proc.stdout:
                proc.stdout.close()

    def stop_tool(self, tool):
        key = tool["key"]
        proc = self.processes.get(key)
        if proc and proc.poll() is None:
            proc.terminate()
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
            widgets["status"].configure(text="🟢 RUNNING", bootstyle=SUCCESS)
            widgets["button"].configure(text="Stop", bootstyle=DANGER, command=lambda t=tool: self.stop_tool(t))
        else:
            widgets["status"].configure(text="⚫ IDLE", bootstyle=SECONDARY)
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