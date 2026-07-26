#!/usr/bin/env python3
"""
EventHub Portable — Central Launcher
TENT DECOR EXPO UP 2026

Single entry point for the whole offline kit. Double-click this file
(or run `python main.py`) and it will:

  1. Make sure every package in requirements.txt is installed.
  2. Open one dashboard with a button to silently launch each tool.
"""

from ttkbootstrap.widgets.scrolled import ScrolledText
from ttkbootstrap.constants import *
import ttkbootstrap as ttk
import os
import sys
import subprocess
import shutil
import queue
import threading
from datetime import datetime
from tkinter import messagebox

# ==============================================================================
# PATHS
# ==============================================================================
ROOT_DIR = os.path.dirname(os.path.abspath(__file__))
APP_DIR = os.path.join(ROOT_DIR, "app")
REQUIREMENTS_FILE = os.path.join(ROOT_DIR, "requirements.txt")
CONFIG_DIR = os.path.join(APP_DIR, "config")
SCHEMA_CONFIG = os.path.join(CONFIG_DIR, "schema.json")
SECRETS_CONFIG = os.path.join(CONFIG_DIR, "secrets.json")

MIN_PYTHON = (3, 9)


# ==============================================================================
# FIRST-RUN BOOTSTRAP (stdlib only)
# ==============================================================================
def _bootstrap_first_run():
    """
    Installs requirements if ttkbootstrap isn't found, then relaunches.
    """
    try:
        import ttkbootstrap  # noqa: F401
        return
    except ImportError:
        pass

    print("=" * 64)
    print(" EventHub Portable — first run on this machine")
    print(" Installing packages from requirements.txt (one-time setup)")
    print("=" * 64)

    if not os.path.isfile(REQUIREMENTS_FILE):
        print(
            f"\nERROR: requirements.txt not found at:\n  {REQUIREMENTS_FILE}")
        _pause()
        sys.exit(1)

    cmd = [sys.executable, "-m", "pip", "install", "-r", REQUIREMENTS_FILE,
           "--disable-pip-version-check"]
    ret = subprocess.call(cmd, cwd=ROOT_DIR)

    if ret != 0:
        print("\nSomething went wrong installing dependencies (see above).")
        print(
            f'Try running this manually:\n  "{sys.executable}" -m pip install -r requirements.txt')
        _pause()
        sys.exit(1)

    print("\nDependencies installed. Starting the launcher...\n")

    ret2 = subprocess.call([sys.executable, os.path.abspath(__file__)] + sys.argv[1:],
                           cwd=ROOT_DIR)
    sys.exit(ret2)


def _pause():
    try:
        input("\nPress Enter to close...")
    except EOFError:
        pass


_bootstrap_first_run()

# ==============================================================================
# MAIN APPLICATION
# ==============================================================================

# ==============================================================================
# TOOL REGISTRY
# ==============================================================================
TOOLS = [
    {
        "key": "hub",
        "icon": "🖥️",
        "label": "Command Center",
        "script": "server_hub.py",
        "desc": "Main hub — Flask API, Cloudflare tunnel, live stats.",
        "bootstyle": PRIMARY,
    },
    {
        "key": "gate_display",
        "icon": "📺",
        "label": "Gate Display Terminal",
        "script": "check_in.py",
        "desc": "Big-screen scan feed for an entrance / gate.",
        "bootstyle": INFO,
    },
    {
        "key": "kiosk",
        "icon": "📝",
        "label": "Registration Kiosk (Desktop)",
        "script": "register.py",
        "desc": "Staffed walk-in registration desk.",
        "bootstyle": SUCCESS,
    },
    {
        "key": "sync",
        "icon": "🔄",
        "label": "Sync Manager",
        "script": "sync_manager.py",
        "desc": "Pull/push Supabase, resolve conflicts, mirror to SQLite.",
        "bootstyle": WARNING,
    },
    {
        "key": "photos",
        "icon": "🖼️",
        "label": "Photo Downloader",
        "script": "photo_down.py",
        "desc": "Pull attendee photos from Cloudinary for offline use.",
        "bootstyle": SECONDARY,
    },
    {
        "key": "explorer",
        "icon": "🔍",
        "label": "Attendee Explorer",
        "script": "explorer.py",
        "desc": "Search and inspect any attendee's profile + photo.",
        "bootstyle": SECONDARY,
    },
]


class LauncherApp(ttk.Window):
    def __init__(self):
        super().__init__(themename="darkly", title="EventHub Portable — Central Launcher")
        self.geometry("1050x900")
        self.minsize(950, 750)

        self.gui_queue = queue.Queue()
        self.processes = {}      # tool key -> subprocess.Popen
        self.tool_widgets = {}   # tool key -> {"button": ..., "status": ...}

        self.build_ui()

        # Staggered startup tasks for better GUI performance
        self.after(100, self._process_gui_queue)
        self.after(500, self.check_dependencies)
        self.after(1500, self._poll_processes)

    # ==========================================================================
    # UI BUILD
    # ==========================================================================
    def build_ui(self):
        # ---- Header ----
        header = ttk.Frame(self, padding=(25, 20, 25, 10))
        header.pack(fill=X)

        title_frame = ttk.Frame(header)
        title_frame.pack(fill=X)

        ttk.Label(title_frame, text="EventHub Portable",
                  font="-size 22 -weight bold").pack(side=LEFT)

        # FIX: Removed the invalid size="sm" argument here
        ttk.Button(
            title_frame, text="⟳ Check Dependencies", bootstyle=(OUTLINE, INFO),
            command=self.check_dependencies
        ).pack(side=RIGHT, pady=5)

        ttk.Label(
            header, text="TENT DECOR EXPO UP 2026 — Central Launcher",
            font="-size 11", bootstyle=PRIMARY
        ).pack(anchor=W, pady=(2, 0))

        # ---- System Status Strip ----
        status_frame = ttk.Frame(self, padding=(25, 0, 25, 15))
        status_frame.pack(fill=X)

        self.lbl_python = ttk.Label(
            status_frame, text="Python: checking…", font="-size 9")
        self.lbl_python.pack(side=LEFT, padx=(0, 20))

        self.lbl_deps = ttk.Label(
            status_frame, text="⏳ Dependencies: checking…", font="-size 9")
        self.lbl_deps.pack(side=LEFT, padx=(0, 20))

        self.lbl_cloudflared = ttk.Label(
            status_frame, text="Cloudflared: checking…", font="-size 9")
        self.lbl_cloudflared.pack(side=LEFT, padx=(0, 20))

        self.lbl_config = ttk.Label(
            status_frame, text="Config: checking…", font="-size 9")
        self.lbl_config.pack(side=LEFT)

        ttk.Separator(self).pack(fill=X, padx=25)

        # ---- Tool Cards Grid ----
        cards_container = ttk.Frame(self, padding=(25, 15, 25, 5))
        cards_container.pack(fill=BOTH, expand=True)

        for tool in TOOLS:
            self._build_tool_card(cards_container, tool)

        # ---- Action Bar (Bottom Tools) ----
        action_bar = ttk.Frame(self, padding=(25, 10, 25, 5))
        action_bar.pack(fill=X)

        # Left Actions (Folders)
        ttk.Button(
            action_bar, text="📁 Project Root", bootstyle=(OUTLINE, SECONDARY),
            command=lambda: self.open_folder(ROOT_DIR)
        ).pack(side=LEFT, padx=(0, 10))

        ttk.Button(
            action_bar, text="⚙️ Config Folder", bootstyle=(OUTLINE, SECONDARY),
            command=lambda: self.open_folder(CONFIG_DIR)
        ).pack(side=LEFT)

        # Right Actions (Controls)
        ttk.Button(
            action_bar, text="🛑 Stop All Tools", bootstyle=DANGER,
            command=self.stop_all_tools
        ).pack(side=RIGHT, padx=(10, 0))

        ttk.Button(
            action_bar, text="🗑️ Clear Log", bootstyle=(OUTLINE, SECONDARY),
            command=self.clear_log
        ).pack(side=RIGHT)

        # ---- Log Panel ----
        log_frame = ttk.Labelframe(self, text="Activity Log", padding=10)
        log_frame.pack(fill=BOTH, expand=False, padx=25, pady=(5, 20))

        self.log_box = ScrolledText(
            log_frame, height=8, autohide=True, wrap="word")
        self.log_box.pack(fill=BOTH, expand=True)
        self.log_box.text.configure(state="disabled", font=("Consolas", 9))

        self.log("Launcher initialized. Ready.")

    def _build_tool_card(self, parent, tool):
        card = ttk.Frame(parent, relief="solid", borderwidth=1, padding=1)
        card.pack(fill=X, pady=6)

        inner = ttk.Frame(card, padding=14)
        inner.pack(fill=BOTH, expand=True)

        ttk.Label(inner, text=tool["icon"], font="-size 22").grid(
            row=0, column=0, rowspan=2, padx=(0, 15), sticky=W)

        ttk.Label(inner, text=tool["label"], font="-size 13 -weight bold").grid(
            row=0, column=1, sticky=W)

        ttk.Label(inner, text=tool["desc"], font="-size 9", bootstyle=SECONDARY).grid(
            row=1, column=1, sticky=W)

        status_lbl = ttk.Label(
            inner, text="Idle", font="-size 9 -weight bold",
            bootstyle=SECONDARY, width=10, anchor=CENTER
        )
        status_lbl.grid(row=0, column=2, rowspan=2, padx=15)

        btn = ttk.Button(
            inner, text="Launch", bootstyle=tool["bootstyle"], width=12,
            command=lambda t=tool: self.launch_tool(t)
        )
        btn.grid(row=0, column=3, rowspan=2, padx=(5, 0))

        inner.columnconfigure(1, weight=1)
        self.tool_widgets[tool["key"]] = {"button": btn, "status": status_lbl}

    # ==========================================================================
    # LOGGING
    # ==========================================================================
    def log(self, message):
        self.gui_queue.put(("log", message))

    def _process_gui_queue(self):
        try:
            # Process up to 50 items per cycle to prevent UI lockup
            for _ in range(50):
                kind, payload = self.gui_queue.get_nowait()
                if kind == "log":
                    self._append_log(payload)
                elif kind == "deps_done":
                    self._on_deps_done(payload)
        except queue.Empty:
            pass
        self.after(100, self._process_gui_queue)

    def _append_log(self, message):
        timestamp = datetime.now().strftime("%H:%M:%S")
        self.log_box.text.configure(state="normal")
        self.log_box.text.insert(END, f"[{timestamp}] {message}\n")
        self.log_box.text.see(END)
        self.log_box.text.configure(state="disabled")

    def clear_log(self):
        self.log_box.text.configure(state="normal")
        self.log_box.text.delete("1.0", END)
        self.log_box.text.configure(state="disabled")
        self.log("Log cleared.")

    # ==========================================================================
    # DEPENDENCY CHECK / INSTALL
    # ==========================================================================
    def check_dependencies(self):
        py_version = f"{sys.version_info.major}.{sys.version_info.minor}.{sys.version_info.micro}"
        if sys.version_info[:2] < MIN_PYTHON:
            self.lbl_python.configure(
                text=f"Python: {py_version} (⚠ needs {MIN_PYTHON[0]}.{MIN_PYTHON[1]}+)",
                bootstyle=WARNING)
        else:
            self.lbl_python.configure(
                text=f"Python: {py_version} ✓", bootstyle=SUCCESS)

        self._refresh_cloudflared_status()
        self._refresh_config_status()

        if not os.path.isfile(REQUIREMENTS_FILE):
            self.lbl_deps.configure(
                text="✗ Dependencies: requirements.txt missing", bootstyle=DANGER)
            self.log(f"requirements.txt not found at {REQUIREMENTS_FILE}")
            return

        self.lbl_deps.configure(
            text="⏳ Dependencies: checking…", bootstyle=WARNING)
        threading.Thread(
            target=self._install_requirements_thread, daemon=True).start()

    def _install_requirements_thread(self):
        cmd = [sys.executable, "-m", "pip", "install", "-r", REQUIREMENTS_FILE,
               "--disable-pip-version-check"]
        try:
            proc = subprocess.Popen(
                cmd, cwd=ROOT_DIR, stdout=subprocess.PIPE, stderr=subprocess.STDOUT,
                text=True, bufsize=1, creationflags=subprocess.CREATE_NO_WINDOW if os.name == "nt" else 0
            )
            for line in proc.stdout:
                line = line.rstrip()
                if line:
                    self.log(f"[pip] {line}")
            proc.wait()
            success = proc.returncode == 0
        except Exception as e:
            self.log(f"[pip] Failed to run pip: {e}")
            success = False

        self.gui_queue.put(("deps_done", success))

    def _on_deps_done(self, success):
        if success:
            self.lbl_deps.configure(
                text="Dependencies: OK ✓", bootstyle=SUCCESS)
        else:
            self.lbl_deps.configure(
                text="⚠ Dependencies: Error", bootstyle=DANGER)

    def _refresh_cloudflared_status(self):
        if shutil.which("cloudflared") is not None:
            self.lbl_cloudflared.configure(
                text="Cloudflared: found ✓", bootstyle=SUCCESS)
        else:
            self.lbl_cloudflared.configure(
                text="Cloudflared: missing ⚠", bootstyle=WARNING)

    def _refresh_config_status(self):
        has_schema = os.path.isfile(SCHEMA_CONFIG)
        has_secrets = os.path.isfile(SECRETS_CONFIG)
        if has_schema and has_secrets:
            self.lbl_config.configure(text="Config: OK ✓", bootstyle=SUCCESS)
        else:
            self.lbl_config.configure(
                text="Config: missing ⚠", bootstyle=WARNING)

    # ==========================================================================
    # TOOL PROCESS MANAGEMENT (SILENT EXECUTION)
    # ==========================================================================
    def launch_tool(self, tool):
        key = tool["key"]

        existing = self.processes.get(key)
        if existing and existing.poll() is None:
            self.log(f"{tool['label']} is already running.")
            return

        script_path = os.path.join(APP_DIR, tool["script"])
        if not os.path.isfile(script_path):
            self.log(f"[ERROR] Can't find {tool['script']} inside app/.")
            return

        try:
            # Silent execution setup: redirect IO and hide console
            kwargs = {
                "cwd": APP_DIR,
                "stdout": subprocess.DEVNULL,
                "stderr": subprocess.DEVNULL,
                "stdin": subprocess.DEVNULL
            }
            if os.name == "nt":
                kwargs["creationflags"] = subprocess.CREATE_NO_WINDOW

            proc = subprocess.Popen([sys.executable, script_path], **kwargs)

        except Exception as e:
            self.log(f"[ERROR] Failed to launch {tool['label']}: {e}")
            return

        self.processes[key] = proc
        self.log(f"Launched: {tool['label']} (PID {proc.pid}).")
        self._set_tool_status(key, running=True)

    def stop_tool(self, tool):
        key = tool["key"]
        proc = self.processes.get(key)
        if proc and proc.poll() is None:
            proc.terminate()
            self.log(f"Stopped: {tool['label']}")
            self._set_tool_status(key, running=False)

    def stop_all_tools(self):
        running_count = 0
        for key in list(self.processes.keys()):
            tool = next((t for t in TOOLS if t["key"] == key), None)
            if tool and self.processes[key].poll() is None:
                self.stop_tool(tool)
                running_count += 1

        if running_count > 0:
            self.log(f"Successfully stopped {running_count} active tools.")
        else:
            self.log("No tools are currently running.")

    def _set_tool_status(self, key, running):
        widgets = self.tool_widgets.get(key)
        tool = next((t for t in TOOLS if t["key"] == key), None)
        if not widgets or not tool:
            return

        btn = widgets["button"]
        status_lbl = widgets["status"]

        if running:
            status_lbl.configure(text="Running", bootstyle=SUCCESS)
            btn.configure(text="Stop", bootstyle=DANGER,
                          command=lambda t=tool: self.stop_tool(t))
        else:
            status_lbl.configure(text="Idle", bootstyle=SECONDARY)
            btn.configure(text="Launch", bootstyle=tool["bootstyle"],
                          command=lambda t=tool: self.launch_tool(t))

    def _poll_processes(self):
        # Create a static list of keys to safely delete from dictionary during iteration
        for key in list(self.processes.keys()):
            proc = self.processes[key]
            if proc.poll() is not None:
                tool = next((t for t in TOOLS if t["key"] == key), None)
                label = tool["label"] if tool else key
                self.log(f"Exited: {label} (code {proc.returncode}).")
                del self.processes[key]
                self._set_tool_status(key, running=False)

        # Polling rate of 1.5 seconds is light on CPU but responsive enough for UI
        self.after(1500, self._poll_processes)

    # ==========================================================================
    # MISCELLANEOUS & SHUTDOWN
    # ==========================================================================
    def open_folder(self, path):
        # Create folder if it doesn't exist (e.g., config folder on first run)
        if not os.path.exists(path):
            try:
                os.makedirs(path)
                self.log(f"Created directory: {path}")
            except Exception as e:
                self.log(f"Could not create directory {path}: {e}")
                return

        try:
            if os.name == "nt":
                os.startfile(path)
            elif sys.platform == "darwin":
                subprocess.Popen(["open", path])
            else:
                subprocess.Popen(["xdg-open", path])
            self.log(f"Opened folder: {os.path.basename(path)}")
        except Exception as e:
            self.log(f"Couldn't open folder: {e}")

    def on_close(self):
        active_tools = [key for key,
                        proc in self.processes.items() if proc.poll() is None]

        if active_tools:
            proceed = messagebox.askyesno(
                "Tools still running",
                f"There are {len(active_tools)} tools running invisibly in the background.\n\n"
                "Are you sure you want to exit? (This will safely shut them all down)"
            )
            if not proceed:
                return

            self.stop_all_tools()

        self.destroy()


if __name__ == "__main__":
    app = LauncherApp()
    app.protocol("WM_DELETE_WINDOW", app.on_close)
    app.mainloop()
