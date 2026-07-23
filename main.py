#!/usr/bin/env python3
"""
EventHub Portable — Central Launcher
TENT DECOR EXPO UP 2026

Single entry point for the whole offline kit. Double-click this file
(or run `python main.py`) and it will:

  1. Make sure every package in requirements.txt is installed — including
     on a totally fresh machine that has nothing but Python + pip yet.
  2. Open one dashboard with a button to launch each tool:
     Command Center, Gate Display, Registration Kiosk, Sync Manager,
     Photo Downloader, Attendee Explorer.

Nothing else needs to be running first. Just: install Python, install
MySQL, put `cloudflared` on PATH, then run this file.
"""

import os
import sys
import subprocess

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
# FIRST-RUN BOOTSTRAP (stdlib only — must work before ttkbootstrap exists)
# ==============================================================================
def _bootstrap_first_run():
    """
    On a brand-new machine, ttkbootstrap (and everything else this project
    needs) may not be installed yet, so this file can't safely `import
    ttkbootstrap` at module load time until that's confirmed.

    This function uses ONLY the standard library. If ttkbootstrap is
    already importable, it does nothing and returns immediately (this is
    the normal case on the 2nd+ run). If not, it installs everything from
    requirements.txt with plain console output, then re-launches this same
    file so the rest of the module can import normally.
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
        print(f"\nERROR: requirements.txt not found at:\n  {REQUIREMENTS_FILE}")
        _pause()
        sys.exit(1)

    cmd = [sys.executable, "-m", "pip", "install", "-r", REQUIREMENTS_FILE,
           "--disable-pip-version-check"]
    ret = subprocess.call(cmd, cwd=ROOT_DIR)

    if ret != 0:
        print("\nSomething went wrong installing dependencies (see above).")
        print(f'Try running this manually:\n  "{sys.executable}" -m pip install -r requirements.txt')
        _pause()
        sys.exit(1)

    print("\nDependencies installed. Starting the launcher...\n")

    # Re-run this same file now that everything is importable, wait for
    # that run to finish, then exit this bootstrap process with its code.
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
# From here on, ttkbootstrap is guaranteed to be installed.
# ==============================================================================
import shutil
import queue
import threading
from datetime import datetime
from tkinter import messagebox

import ttkbootstrap as ttk
from ttkbootstrap.constants import *
from ttkbootstrap.widgets.scrolled import ScrolledText

# ==============================================================================
# TOOL REGISTRY — one entry per launchable script in app/
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
        self.geometry("1000x840")
        self.minsize(880, 680)

        self.gui_queue = queue.Queue()
        self.processes = {}      # tool key -> subprocess.Popen
        self.tool_widgets = {}   # tool key -> {"button": ..., "status": ...}

        self.build_ui()

        self.after(100, self._process_gui_queue)
        self.after(1200, self._poll_processes)
        self.after(300, self.check_dependencies)   # auto-run once on startup

    # ==========================================================================
    # UI BUILD
    # ==========================================================================
    def build_ui(self):
        header = ttk.Frame(self, padding=(25, 20, 25, 10))
        header.pack(fill=X)

        ttk.Label(header, text="EventHub Portable", font="-size 20 -weight bold").pack(anchor=W)
        ttk.Label(
            header, text="TENT DECOR EXPO UP 2026 — Central Launcher",
            font="-size 11", bootstyle=PRIMARY
        ).pack(anchor=W)

        # ---- System status strip ----
        status_frame = ttk.Frame(self, padding=(25, 0, 25, 10))
        status_frame.pack(fill=X)

        self.lbl_python = ttk.Label(status_frame, text="Python: checking…", font="-size 9")
        self.lbl_python.pack(side=LEFT, padx=(0, 20))

        self.lbl_deps = ttk.Label(status_frame, text="⏳ Dependencies: checking…", font="-size 9")
        self.lbl_deps.pack(side=LEFT, padx=(0, 20))

        self.lbl_cloudflared = ttk.Label(status_frame, text="Cloudflared: checking…", font="-size 9")
        self.lbl_cloudflared.pack(side=LEFT, padx=(0, 20))

        self.lbl_config = ttk.Label(status_frame, text="Config: checking…", font="-size 9")
        self.lbl_config.pack(side=LEFT)

        ttk.Separator(self).pack(fill=X, padx=25)

        # ---- Tool cards ----
        cards_container = ttk.Frame(self, padding=(25, 15, 25, 5))
        cards_container.pack(fill=BOTH, expand=True)

        for tool in TOOLS:
            self._build_tool_card(cards_container, tool)

        # ---- Bottom bar ----
        bottom_bar = ttk.Frame(self, padding=(25, 5, 25, 5))
        bottom_bar.pack(fill=X)

        ttk.Button(
            bottom_bar, text="⟳ Re-check / Install Dependencies",
            bootstyle=(OUTLINE, INFO), command=self.check_dependencies
        ).pack(side=LEFT)

        ttk.Button(
            bottom_bar, text="Open Project Folder",
            bootstyle=(OUTLINE, SECONDARY), command=self.open_root_folder
        ).pack(side=LEFT, padx=10)

        # ---- Log panel ----
        log_frame = ttk.Labelframe(self, text="Activity Log", padding=10)
        log_frame.pack(fill=BOTH, expand=False, padx=25, pady=(5, 15))

        self.log_box = ScrolledText(log_frame, height=9, autohide=True, wrap="word")
        self.log_box.pack(fill=BOTH, expand=True)
        self.log_box.text.configure(state="disabled", font=("Consolas", 9))

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
    # LOGGING (same gui_queue + polling pattern as the rest of the suite)
    # ==========================================================================
    def log(self, message):
        self.gui_queue.put(("log", message))

    def _process_gui_queue(self):
        try:
            while True:
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
            self.lbl_python.configure(text=f"Python: {py_version} ✓", bootstyle=DEFAULT)

        self._refresh_cloudflared_status()
        self._refresh_config_status()

        if not os.path.isfile(REQUIREMENTS_FILE):
            self.lbl_deps.configure(text="✗ Dependencies: requirements.txt not found", bootstyle=DANGER)
            self.log(f"requirements.txt not found at {REQUIREMENTS_FILE}")
            return

        self.lbl_deps.configure(text="⏳ Dependencies: installing…", bootstyle=WARNING)
        self.log("Checking / installing packages from requirements.txt …")
        threading.Thread(target=self._install_requirements_thread, daemon=True).start()

    def _install_requirements_thread(self):
        cmd = [sys.executable, "-m", "pip", "install", "-r", REQUIREMENTS_FILE,
               "--disable-pip-version-check"]
        success = False
        try:
            proc = subprocess.Popen(
                cmd, cwd=ROOT_DIR, stdout=subprocess.PIPE, stderr=subprocess.STDOUT,
                text=True, bufsize=1
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
            self.lbl_deps.configure(text="✓ Dependencies: OK", bootstyle=SUCCESS)
            self.log("All requirements are installed.")
        else:
            self.lbl_deps.configure(text="⚠ Dependencies: check log", bootstyle=DANGER)
            self.log("Dependency install reported a problem — check the log above, "
                      "or run: pip install -r requirements.txt manually.")

    def _refresh_cloudflared_status(self):
        if shutil.which("cloudflared") is not None:
            self.lbl_cloudflared.configure(text="Cloudflared: found ✓", bootstyle=SUCCESS)
        else:
            self.lbl_cloudflared.configure(text="Cloudflared: not on PATH", bootstyle=WARNING)

    def _refresh_config_status(self):
        has_schema = os.path.isfile(SCHEMA_CONFIG)
        has_secrets = os.path.isfile(SECRETS_CONFIG)
        if has_schema and has_secrets:
            self.lbl_config.configure(text="Config: set up ✓", bootstyle=SUCCESS)
        else:
            self.lbl_config.configure(text="Config: not set up yet", bootstyle=WARNING)
            missing = [name for name, present in
                       (("schema.json", has_schema), ("secrets.json", has_secrets)) if not present]
            self.log(f"Missing {', '.join(missing)} in app/config/ — on a brand-new machine, "
                      f"open Sync Manager and use 'Configure Databases' first.")

    # ==========================================================================
    # LAUNCHING TOOLS
    # ==========================================================================
    def launch_tool(self, tool):
        key = tool["key"]

        existing = self.processes.get(key)
        if existing and existing.poll() is None:
            self.log(f"{tool['label']} is already running (PID {existing.pid}).")
            return

        script_path = os.path.join(APP_DIR, tool["script"])
        if not os.path.isfile(script_path):
            self.log(f"[ERROR] Can't find {tool['script']} inside app/ — skipping.")
            return

        try:
            kwargs = {"cwd": APP_DIR}
            if os.name == "nt":
                kwargs["creationflags"] = subprocess.CREATE_NEW_CONSOLE
            proc = subprocess.Popen([sys.executable, script_path], **kwargs)
        except Exception as e:
            self.log(f"[ERROR] Failed to launch {tool['label']}: {e}")
            return

        self.processes[key] = proc
        self.log(f"Launched {tool['label']} (PID {proc.pid}).")
        self._set_tool_status(key, running=True)

    def stop_tool(self, tool):
        key = tool["key"]
        proc = self.processes.get(key)
        if proc and proc.poll() is None:
            proc.terminate()
            self.log(f"Stopping {tool['label']} …")

    def _set_tool_status(self, key, running):
        widgets = self.tool_widgets.get(key)
        tool = next((t for t in TOOLS if t["key"] == key), None)
        if not widgets or not tool:
            return
        btn = widgets["button"]
        status_lbl = widgets["status"]
        if running:
            status_lbl.configure(text="Running", bootstyle=SUCCESS)
            btn.configure(text="Stop", bootstyle=DANGER, command=lambda t=tool: self.stop_tool(t))
        else:
            status_lbl.configure(text="Idle", bootstyle=SECONDARY)
            btn.configure(text="Launch", bootstyle=tool["bootstyle"],
                          command=lambda t=tool: self.launch_tool(t))

    def _poll_processes(self):
        for key in list(self.processes.keys()):
            proc = self.processes[key]
            if proc.poll() is not None:
                tool = next((t for t in TOOLS if t["key"] == key), None)
                label = tool["label"] if tool else key
                self.log(f"{label} exited (code {proc.returncode}).")
                del self.processes[key]
                self._set_tool_status(key, running=False)
        self.after(1200, self._poll_processes)

    # ==========================================================================
    # MISC
    # ==========================================================================
    def open_root_folder(self):
        try:
            if os.name == "nt":
                os.startfile(ROOT_DIR)
            elif sys.platform == "darwin":
                subprocess.Popen(["open", ROOT_DIR])
            else:
                subprocess.Popen(["xdg-open", ROOT_DIR])
        except Exception as e:
            self.log(f"Couldn't open folder: {e}")

    def on_close(self):
        running_labels = []
        for key, proc in self.processes.items():
            if proc.poll() is None:
                tool = next((t for t in TOOLS if t["key"] == key), None)
                if tool:
                    running_labels.append(tool["label"])

        if running_labels:
            proceed = messagebox.askyesno(
                "Tools still running",
                "These are still running:\n\n" + "\n".join(running_labels) +
                "\n\nClose the launcher anyway? (they'll keep running in the background)"
            )
            if not proceed:
                return

        self.destroy()


if __name__ == "__main__":
    app = LauncherApp()
    app.protocol("WM_DELETE_WINDOW", app.on_close)
    app.mainloop()
