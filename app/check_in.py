import os
import json
import time
import threading
import queue
import requests
import urllib3
import platform
from datetime import datetime
import tkinter as tk
from tkinter import messagebox, filedialog

import ttkbootstrap as ttk
from ttkbootstrap.constants import *
from PIL import Image, ImageTk, ImageOps

# Attempt to load Windows sound, otherwise fallback to basic UI bell
try:
    if platform.system() == "Windows":
        import winsound
        HAS_WINSOUND = True
    else:
        HAS_WINSOUND = False
except ImportError:
    HAS_WINSOUND = False

# Suppress warnings for local adhoc HTTPS certificates
urllib3.disable_warnings(urllib3.exceptions.InsecureRequestWarning)

BASE_DIR = os.path.dirname(os.path.abspath(__file__))
CONFIG_PATH = os.path.join(BASE_DIR, 'config', 'checkin.json')
DEFAULT_PHOTO_DIR = 'attendee_photos'

# Category Color Mapping (Used exclusively for the Tkinter Pass Badge)
PASS_COLORS = {
    "business": {"bg": "#FFC107", "fg": "#000000"},  # Yellow
    "general": {"bg": "#2196F3", "fg": "#FFFFFF"},   # Blue
    "media": {"bg": "#F44336", "fg": "#FFFFFF"},     # Red
    "exhibitor": {"bg": "#9C27B0", "fg": "#FFFFFF"}, # Purple
    "default": {"bg": "#6c757d", "fg": "#FFFFFF"}    # Grey fallback
}

# ==============================================================================
# SETTINGS MANAGER
# ==============================================================================
class ConfigManager:
    def __init__(self):
        self.config = {
            "hub_url": "https://127.0.0.1:5000",
            "device_name": "Gate_Display_1",
            "poll_interval_ms": 500,
            "photo_directory": DEFAULT_PHOTO_DIR
        }
        self.load()

    def load(self):
        if os.path.exists(CONFIG_PATH):
            try:
                with open(CONFIG_PATH, 'r') as f:
                    content = f.read().strip()
                    if content:
                        self.config.update(json.loads(content))
            except (json.JSONDecodeError, ValueError) as e:
                print(f"Config corrupted. Resetting to defaults. Error: {e}")
                self.save()
            except Exception as e:
                print(f"Error loading config: {e}")

    def save(self):
        os.makedirs(os.path.dirname(CONFIG_PATH), exist_ok=True)
        with open(CONFIG_PATH, 'w') as f:
            json.dump(self.config, f, indent=4)

# ==============================================================================
# SETTINGS DIALOG
# ==============================================================================
class SettingsDialog(ttk.Toplevel):
    def __init__(self, parent, config_manager, on_save_callback):
        super().__init__(parent)
        self.title("Settings — Gate Terminal")
        self.geometry("500x480") 
        self.resizable(True, True)
        self.config_manager = config_manager
        self.on_save = on_save_callback

        self.build_ui()
        self.center_window(parent)

    def center_window(self, parent):
        self.update_idletasks()
        x = parent.winfo_x() + (parent.winfo_width() // 2) - (self.winfo_width() // 2)
        y = parent.winfo_y() + (parent.winfo_height() // 2) - (self.winfo_height() // 2)
        self.geometry(f"+{x}+{y}")

    def build_ui(self):
        frame = ttk.Frame(self, padding=20)
        frame.pack(fill=BOTH, expand=True)

        ttk.Label(frame, text="⚙️ Terminal Settings", font="-size 14 -weight bold").pack(anchor=W, pady=(0, 20))

        ttk.Label(frame, text="Hub URL", font="-weight bold").pack(anchor=W)
        self.ent_url = ttk.Entry(frame)
        self.ent_url.insert(0, self.config_manager.config["hub_url"])
        self.ent_url.pack(fill=X, pady=(0, 15))

        ttk.Label(frame, text="Device Name", font="-weight bold").pack(anchor=W)
        self.ent_device = ttk.Entry(frame)
        self.ent_device.insert(0, self.config_manager.config["device_name"])
        self.ent_device.pack(fill=X, pady=(0, 15))

        ttk.Label(frame, text="Photo Directory (Relative to App)", font="-weight bold").pack(anchor=W)
        photo_frame = ttk.Frame(frame)
        photo_frame.pack(fill=X, pady=(0, 20))
        
        self.ent_photo = ttk.Entry(photo_frame)
        self.ent_photo.insert(0, self.config_manager.config["photo_directory"])
        self.ent_photo.pack(side=LEFT, fill=X, expand=True, padx=(0, 5))
        ttk.Button(photo_frame, text="Browse", bootstyle=SECONDARY, command=self.browse_dir).pack(side=RIGHT)

        btn_frame = ttk.Frame(frame)
        btn_frame.pack(fill=X, side=BOTTOM, pady=(10, 0))
        ttk.Button(btn_frame, text="💾 Save & Apply", bootstyle=PRIMARY, command=self.save).pack(fill=X, pady=5)
        ttk.Button(btn_frame, text="Cancel", bootstyle=SECONDARY, command=self.destroy).pack(fill=X)

    def browse_dir(self):
        start_dir = os.path.normpath(os.path.join(BASE_DIR, self.ent_photo.get()))
        d = filedialog.askdirectory(initialdir=start_dir)
        if d:
            try:
                rel_path = os.path.relpath(d, start=BASE_DIR).replace('\\', '/')
            except ValueError:
                rel_path = d
            self.ent_photo.delete(0, END)
            self.ent_photo.insert(0, rel_path)

    def save(self):
        self.config_manager.config["hub_url"] = self.ent_url.get().strip().rstrip('/')
        self.config_manager.config["device_name"] = self.ent_device.get().strip()
        self.config_manager.config["photo_directory"] = self.ent_photo.get().strip()
        self.config_manager.save()
        self.on_save()
        self.destroy()

# ==============================================================================
# MAIN GATE DISPLAY APPLICATION
# ==============================================================================
class GateDisplay(ttk.Window):
    def __init__(self):
        # Initialize with Darkly theme by default
        super().__init__(themename="darkly", title="Gate Display Terminal")
        self.geometry("1400x850")
        self.minsize(1200, 700)
        
        self.current_theme = "darkly"

        self.config_manager = ConfigManager()
        self.gui_queue = queue.Queue()
        self.scan_queue = queue.Queue()  
        
        self.is_polling = True
        self.sound_enabled = True
        self.stats = {"Success": 0, "Duplicate": 0, "Wrong Day": 0, "Errors": 0}
        self.recent_scans = []
        self.current_photo = None

        self.build_ui()
        self.after(50, self.process_queue)
        self.after(50, self.process_scan_queue)
        self.start_threads()

    def toggle_theme(self):
        # Toggle between Flatly (Light) and Darkly (Dark)
        self.current_theme = "flatly" if self.current_theme == "darkly" else "darkly"
        self.style.theme_use(self.current_theme)
        
        # Update placeholder colors based on theme context
        for e in (self.ent_id, self.ent_phone):
            if e.get() == "Attendee ID (e.g. TDE26...)" or e.get() == "Phone Number (e.g. 90000...)":
                e.configure(foreground="gray")
            else:
                e.configure(foreground="") # Native text color
                
        # Update placeholder photo if no attendee is currently shown
        if self.lbl_attendee_id.cget("text") == "---":
            self.set_placeholder_photo()

    def build_ui(self):
        # --- TOP NAVBAR ---
        # Removed explicit bootstyles to fix blocky artifacts. It now inherits natively.
        self.nav = ttk.Frame(self, padding=15)
        self.nav.pack(fill=X)
        
        title_frame = ttk.Frame(self.nav)
        title_frame.pack(side=LEFT)
        
        ttk.Label(title_frame, text="🎟️ Gate Display Terminal", font="-size 18 -weight bold").pack(anchor=W)
        self.lbl_subtitle = ttk.Label(title_frame, text=f"{self.config_manager.config['device_name']} • TDE UP 2026", font="-size 10", bootstyle=SECONDARY)
        self.lbl_subtitle.pack(anchor=W)

        controls = ttk.Frame(self.nav)
        controls.pack(side=RIGHT)
        
        ttk.Button(controls, text="🌗 Theme", bootstyle="outline-secondary", command=self.toggle_theme).pack(side=LEFT, padx=5)
        ttk.Button(controls, text="⚙️ Settings", bootstyle="outline-secondary", command=self.open_settings).pack(side=LEFT, padx=5)
        
        self.btn_sound = ttk.Button(controls, text="🔊 Sound", bootstyle="outline-info", command=self.toggle_sound)
        self.btn_sound.pack(side=LEFT, padx=5)
        
        ttk.Button(controls, text="⛶ Fullscreen", bootstyle="outline-secondary", command=lambda: self.attributes('-fullscreen', not self.attributes('-fullscreen'))).pack(side=LEFT, padx=5)
        
        self.lbl_hub_status = ttk.Label(controls, text="● Connecting...", font="-weight bold -size 11", bootstyle=WARNING)
        self.lbl_hub_status.pack(side=LEFT, padx=20)

        # --- DYNAMIC TEST MODE BANNER ---
        self.test_banner = ttk.Frame(self, bootstyle=DANGER)
        self.lbl_test_mode = ttk.Label(self.test_banner, text="⚠️ TEST MODE ACTIVE", font="-weight bold -size 12", bootstyle="inverse-danger")
        self.lbl_test_mode.pack(pady=8)
        
        # --- MAIN CONTENT GRID ---
        self.content = ttk.Frame(self, padding=25)
        self.content.pack(fill=BOTH, expand=True)

        # LEFT PANEL (Display)
        left_panel = ttk.Frame(self.content)
        left_panel.pack(side=LEFT, fill=BOTH, expand=True, padx=(0, 25))

        # Banner with true inverse styling
        self.status_banner = ttk.Label(left_panel, text="WAITING FOR SCAN...", font="-size 28 -weight bold", bootstyle="inverse-secondary", anchor=CENTER)
        self.status_banner.pack(fill=X, pady=(0, 25), ipady=25)

        # Profile Frame
        profile_frame = ttk.Frame(left_panel)
        profile_frame.pack(fill=BOTH, expand=True)

        # Photo Container 
        photo_container = ttk.Frame(profile_frame, width=340)
        photo_container.pack(side=LEFT, fill=Y, padx=(0, 40))
        photo_container.pack_propagate(False) 
        
        photo_border = ttk.Frame(photo_container, bootstyle=SECONDARY, padding=2)
        photo_border.pack(fill=BOTH, expand=True)
        
        self.lbl_photo = ttk.Label(photo_border, anchor=CENTER)
        self.lbl_photo.pack(fill=BOTH, expand=True)
        
        self.lbl_attendee_id = ttk.Label(photo_container, text="---", font="-size 12 -weight bold", bootstyle=SECONDARY, anchor=CENTER)
        self.lbl_attendee_id.pack(pady=15)
        self.set_placeholder_photo()

        # Attendee Details Container
        details = ttk.Frame(profile_frame)
        details.pack(side=LEFT, fill=BOTH, expand=True)
        
        header_frame = ttk.Frame(details)
        header_frame.pack(fill=X, pady=(10, 0))
        
        self.lbl_name = ttk.Label(header_frame, text="SCAN TICKET", font="-size 36 -weight bold")
        self.lbl_name.pack(side=LEFT, anchor=W)
        
        # Standalone TK label retains hex colors accurately without fighting ttk themes
        self.lbl_pass_badge = tk.Label(header_frame, text="PENDING", font=("Helvetica", 14, "bold"), bg="#6c757d", fg="#FFFFFF", padx=15, pady=5, relief="flat")
        self.lbl_pass_badge.pack(side=RIGHT, anchor=E, pady=10)

        self.lbl_company = ttk.Label(details, text="To view attendee details", font="-size 16", bootstyle=INFO)
        self.lbl_company.pack(anchor=W, pady=(5, 30))

        # Modernized Grid Layout (Removed explicit fg colors)
        grid = ttk.Frame(details)
        grid.pack(fill=BOTH, expand=True)
        grid.columnconfigure(0, weight=1)
        grid.columnconfigure(1, weight=1)
        
        self.fields = {}
        row_col = [
            (0, 0, "Mobile Number", "mobile"), (0, 1, "Location", "location"), 
            (1, 0, "Category", "category"), (1, 1, "Gender", "gender"), 
            (2, 0, "Event Date", "date"), (2, 1, "Scanner ID", "scanner")
        ]
        
        for r, c, label, key in row_col:
            f = ttk.Frame(grid, padding=8)
            f.grid(row=r, column=c, sticky=NSEW, padx=5, pady=8)
            ttk.Label(f, text=f"{label.upper()}", font="-size 9 -weight bold", bootstyle=SECONDARY).pack(anchor=W)
            val = ttk.Label(f, text="---", font="-size 14 -weight bold")
            val.pack(anchor=W, pady=(4,0))
            self.fields[key] = val

        # Bottom Success Banner
        self.bottom_banner = ttk.Label(left_panel, text="READY FOR OPERATIONS", font="-size 15 -weight bold", bootstyle="inverse-secondary", anchor=CENTER)
        self.bottom_banner.pack(fill=X, side=BOTTOM, ipady=15)

        # RIGHT PANEL (Sidebar)
        right_panel = ttk.Frame(self.content, width=400)
        right_panel.pack(side=RIGHT, fill=Y)
        right_panel.pack_propagate(False)

        # Manual Lookup Card
        lookup = ttk.Labelframe(right_panel, text=" 🔍 Manual Entry ", padding=20)
        lookup.pack(fill=X, pady=(0, 20))
        
        self.ent_phone = self.create_placeholder_entry(lookup, "Phone Number (e.g. 90000...)")
        self.ent_phone.pack(fill=X, pady=(0, 12), ipady=4)
        self.ent_phone.bind("<Return>", lambda e: self.manual_scan('phone'))
        
        self.ent_id = self.create_placeholder_entry(lookup, "Attendee ID (e.g. TDE26...)")
        self.ent_id.pack(fill=X, pady=(0, 12), ipady=4)
        self.ent_id.bind("<Return>", lambda e: self.manual_scan('id'))

        ttk.Button(lookup, text="PROCESS MANUAL SCAN", bootstyle=PRIMARY, command=self.handle_manual_submit).pack(fill=X, ipady=4)

        # Stats Board
        stats_frame = ttk.Frame(right_panel)
        stats_frame.pack(fill=X, pady=(0, 20))
        
        self.stat_labels = {}
        for idx, (title, color) in enumerate([("Success", SUCCESS), ("Duplicate", WARNING), ("Wrong Day", SECONDARY), ("Errors", DANGER)]):
            f = ttk.Frame(stats_frame, borderwidth=1, relief=SOLID, padding=12)
            f.grid(row=idx//2, column=idx%2, sticky=NSEW, padx=3, pady=3)
            stats_frame.columnconfigure(idx%2, weight=1)
            
            val = ttk.Label(f, text="0", font="-size 22 -weight bold", bootstyle=color)
            val.pack(anchor=CENTER)
            ttk.Label(f, text=title.upper(), font="-size 9 -weight bold", bootstyle=SECONDARY).pack(anchor=CENTER)
            self.stat_labels[title] = val

        # Recent Scans Log
        ttk.Label(right_panel, text="🕒 RECENT ACTIVITY", font="-size 11 -weight bold").pack(anchor=W, pady=(0, 8))
        self.list_frame = ttk.Frame(right_panel)
        self.list_frame.pack(fill=BOTH, expand=True)

    def create_placeholder_entry(self, parent, placeholder_text):
        entry = ttk.Entry(parent, font="-size 11")
        entry.insert(0, placeholder_text)
        entry.configure(foreground='gray')

        def on_focus_in(event):
            if entry.get() == placeholder_text:
                entry.delete(0, END)
                # Setting foreground to empty string resets it to native theme text color
                entry.configure(foreground='') 

        def on_focus_out(event):
            if not entry.get():
                entry.insert(0, placeholder_text)
                entry.configure(foreground='gray')

        entry.bind("<FocusIn>", on_focus_in)
        entry.bind("<FocusOut>", on_focus_out)
        return entry

    def handle_manual_submit(self):
        if self.ent_id.get() and "Attendee ID" not in self.ent_id.get():
            self.manual_scan('id')
        elif self.ent_phone.get() and "Phone Number" not in self.ent_phone.get():
            self.manual_scan('phone')

    def toggle_sound(self):
        self.sound_enabled = not self.sound_enabled
        self.btn_sound.configure(
            text="🔊 Sound" if self.sound_enabled else "🔇 Muted", 
            bootstyle="outline-info" if self.sound_enabled else "outline-secondary"
        )

    def play_sound(self, status):
        if not self.sound_enabled:
            return
        
        def _play():
            if HAS_WINSOUND:
                if status == "SUCCESS":
                    winsound.MessageBeep(winsound.MB_OK)
                elif status == "DUPLICATE":
                    winsound.MessageBeep(winsound.MB_ICONEXCLAMATION)
                else:
                    winsound.MessageBeep(winsound.MB_ICONHAND)
            else:
                self.bell()
        threading.Thread(target=_play, daemon=True).start()

    # --- ASYNCHRONOUS PHOTO LOADING ---
    def set_placeholder_photo(self):
        # Adapt placeholder background dynamically based on active theme
        bg_color = '#e9ecef' if self.current_theme == "flatly" else '#222222'
        img = Image.new('RGB', (340, 340), color=bg_color)
        self.current_photo = ImageTk.PhotoImage(img)
        self.lbl_photo.configure(image=self.current_photo)

    def async_load_photo(self, attendee_id):
        def _load():
            rel_dir = self.config_manager.config.get("photo_directory", DEFAULT_PHOTO_DIR)
            abs_directory = os.path.normpath(os.path.join(BASE_DIR, rel_dir))
            os.makedirs(abs_directory, exist_ok=True)
            
            photo_found = False
            for ext in ['.jpg', '.png', '.jpeg']:
                path = os.path.join(abs_directory, f"{attendee_id}{ext}")
                if os.path.exists(path):
                    try:
                        img = Image.open(path)
                        img = ImageOps.fit(img, (340, 340), Image.Resampling.LANCZOS)
                        photo_image = ImageTk.PhotoImage(img)
                        self.gui_queue.put(lambda p=photo_image: self.update_photo_ui(p))
                        photo_found = True
                        break
                    except Exception as e:
                        print(f"Error loading photo asynchronously: {e}")
            
            if not photo_found:
                self.gui_queue.put(self.set_placeholder_photo)

        threading.Thread(target=_load, daemon=True).start()

    def update_photo_ui(self, photo_image):
        self.current_photo = photo_image
        self.lbl_photo.configure(image=self.current_photo)

    # --- QUEUE PROCESSORS ---
    def process_queue(self):
        try:
            while True:
                task = self.gui_queue.get_nowait()
                task()
        except queue.Empty:
            pass
        finally:
            self.after(50, self.process_queue)

    def process_scan_queue(self):
        try:
            if not self.scan_queue.empty():
                event_data = self.scan_queue.get_nowait()
                self.update_ui_with_event(event_data)
                
                q_len = self.scan_queue.qsize()
                delay_ms = 800 if q_len > 1 else 2000
                
                self.after(delay_ms, self.process_scan_queue)
                return
        except queue.Empty:
            pass
            
        self.after(50, self.process_scan_queue)

    def update_ui_with_event(self, event_data):
        status_type = event_data.get("status", "ERROR")
        message = event_data.get("message", "Unknown error")
        attendee = event_data.get("attendee")
        scanner_dev = event_data.get("device", "Unknown Scanner")
        
        raw_ts = event_data.get("timestamp")
        time_str = datetime.now().strftime("%I:%M %p")
        if raw_ts:
            try:
                dt = datetime.fromisoformat(raw_ts.replace('Z', '+00:00'))
                time_str = dt.astimezone().strftime("%I:%M %p")
            except Exception: 
                pass

        configs = {
            "SUCCESS": {"color": "success", "banner": "✅ ACCESS GRANTED", "bottom": "SUCCESSFULLY CHECKED IN"},
            "DUPLICATE": {"color": "warning", "banner": "⚠️ ALREADY SCANNED", "bottom": "DUPLICATE SCAN DETECTED"},
            "ERROR": {"color": "danger", "banner": "❌ ACCESS DENIED", "bottom": message}
        }
        cfg = configs.get(status_type, configs["ERROR"])
        c_style = cfg["color"]
        
        self.status_banner.configure(text=cfg["banner"], bootstyle=f"inverse-{c_style}")
        self.bottom_banner.configure(text=cfg["bottom"], bootstyle=f"inverse-{c_style}")
        self.play_sound(status_type)

        if attendee:
            category_raw = str(attendee.get("attendee_type", "")).lower()
            color_cfg = PASS_COLORS.get(category_raw, PASS_COLORS["default"])
            
            self.lbl_pass_badge.configure(
                bg=color_cfg["bg"], 
                fg=color_cfg["fg"], 
                text=category_raw.upper() if category_raw else "UNKNOWN"
            )

            self.lbl_name.configure(text=attendee.get("full_name", "").upper())
            self.lbl_company.configure(text=attendee.get("business_name") or "General Admission", bootstyle="info" if c_style=="success" else c_style)
            self.lbl_attendee_id.configure(text=attendee.get("attendee_id", ""))
            
            mobile = str(attendee.get("mobile", ""))
            masked_mobile = f"••••••{mobile[-4:]}" if len(mobile) >= 4 else mobile
            
            self.fields["mobile"].configure(text=masked_mobile)
            self.fields["location"].configure(text=f"{attendee.get('city', '')}, {attendee.get('state', '')}".strip(', '))
            self.fields["category"].configure(text=attendee.get("attendee_type", ""))
            self.fields["gender"].configure(text=attendee.get("gender", ""))
            self.fields["date"].configure(text=datetime.now().strftime("%d %B %Y"))
            self.fields["scanner"].configure(text=scanner_dev)
            
            self.async_load_photo(attendee.get("attendee_id"))
            self.add_recent_scan(attendee.get("full_name"), attendee.get("attendee_id"), c_style, time_str)
        else:
            self.lbl_name.configure(text="UNKNOWN RECORD")
            self.lbl_company.configure(text="---", bootstyle=SECONDARY)
            self.lbl_attendee_id.configure(text="---")
            self.lbl_pass_badge.configure(bg=PASS_COLORS["default"]["bg"], fg=PASS_COLORS["default"]["fg"], text="N/A")
            
            for lbl in self.fields.values():
                lbl.configure(text="---")
            self.set_placeholder_photo()

        # Update stats counter
        if status_type in ["SUCCESS", "DUPLICATE", "ERROR"]:
            key = "Success" if status_type == "SUCCESS" else ("Duplicate" if status_type == "DUPLICATE" else "Errors")
            self.stats[key] += 1
            self.stat_labels[key].configure(text=str(self.stats[key]))

    def add_recent_scan(self, name, att_id, style, time_str):
        card = ttk.Frame(self.list_frame, bootstyle=style, borderwidth=1, relief=SOLID)
        card.pack(fill=X, pady=4, padx=2)
        
        # FIX: Added `inverse-{style}` to child labels so they properly inherit the colored background
        lbl_style = f"inverse-{style}"
        
        top = ttk.Frame(card, bootstyle=style)
        top.pack(fill=X, padx=12, pady=(8, 0))
        ttk.Label(top, text=f"👤 {name}", font="-size 10 -weight bold", bootstyle=lbl_style).pack(side=LEFT)
        ttk.Label(top, text=time_str, font="-size 8", bootstyle=lbl_style).pack(side=RIGHT)
        
        bot = ttk.Frame(card, bootstyle=style)
        bot.pack(fill=X, padx=12, pady=(4, 8))
        ttk.Label(bot, text=att_id, font="-size 8", bootstyle=lbl_style).pack(side=LEFT)
        ttk.Label(bot, text="✓ OK" if style=="success" else "⚠ WARN", font="-weight bold", bootstyle=lbl_style).pack(side=RIGHT)
        
        self.recent_scans.insert(0, card)
        if len(self.recent_scans) > 4:
            old = self.recent_scans.pop()
            old.destroy()

    # --- NETWORK POLLING & THREADING ---
    def start_threads(self):
        self.is_polling = True
        self.stream_thread = threading.Thread(target=self.listen_to_server_stream, daemon=True)
        self.stream_thread.start()
        
        self.status_thread = threading.Thread(target=self.poll_server_status, daemon=True)
        self.status_thread.start()

    def poll_server_status(self):
        while self.is_polling:
            hub_url = self.config_manager.config.get('hub_url', '').rstrip('/')
            device_name = self.config_manager.config.get('device_name', '')
            url = f"{hub_url}/api/status?device_name={requests.utils.quote(device_name)}"
            
            try:
                resp = requests.get(url, timeout=3, verify=False)
                if resp.status_code == 200:
                    data = resp.json()
                    is_test = data.get("test_mode", False)
                    test_date = data.get("test_date", "Unknown")
                    self.gui_queue.put(lambda t=is_test, d=test_date: self.update_test_banner(t, d))
                else:
                    self.gui_queue.put(lambda: self.update_test_banner(False, ""))
            except requests.exceptions.RequestException:
                self.gui_queue.put(lambda: self.update_test_banner(False, ""))
            
            time.sleep(3)

    def update_test_banner(self, is_test_mode, test_date):
        if is_test_mode:
            self.lbl_test_mode.configure(text=f"⚠️ TEST MODE ACTIVE (OVERRIDE: {test_date})")
            if not self.test_banner.winfo_ismapped():
                self.test_banner.pack(fill=X, before=self.content)
        else:
            if self.test_banner.winfo_ismapped():
                self.test_banner.pack_forget()

    def listen_to_server_stream(self):
        while self.is_polling:
            hub_url = self.config_manager.config.get('hub_url', '')
            url = f"{hub_url}/api/stream-scans"
            
            try:
                self.gui_queue.put(lambda: self.lbl_hub_status.configure(text="● Connecting Stream...", bootstyle=WARNING))
                with requests.get(url, stream=True, timeout=(5, 15), verify=False) as response:
                    if response.status_code == 200:
                        self.gui_queue.put(lambda: self.lbl_hub_status.configure(text="● Hub Live Stream", bootstyle=SUCCESS))
                        for line in response.iter_lines():
                            if not self.is_polling: break
                            if line:
                                decoded = line.decode('utf-8')
                                if decoded.startswith("data: "):
                                    try:
                                        event_data = json.loads(decoded[6:])
                                        self.scan_queue.put(event_data)
                                    except Exception:
                                        pass
                    else:
                        self.gui_queue.put(lambda: self.lbl_hub_status.configure(text=f"● Hub Error {response.status_code}", bootstyle=WARNING))
                        time.sleep(2)
                        
            except requests.exceptions.RequestException:
                self.gui_queue.put(lambda: self.lbl_hub_status.configure(text="● Hub Disconnected. Retrying...", bootstyle=DANGER))
                time.sleep(2)

    def manual_scan(self, lookup_type):
        val = self.ent_phone.get() if lookup_type == 'phone' else self.ent_id.get()
        if "e.g." in val or not val.strip(): return
        
        url = f"{self.config_manager.config['hub_url']}/api/checkin"
        payload = {
            "attendee_id": val.strip(),
            "search_type": lookup_type,
            "device_name": self.config_manager.config["device_name"]
        }
        
        def _post_with_retries():
            max_retries = 3
            for attempt in range(max_retries):
                try:
                    requests.post(url, json=payload, timeout=4, verify=False)
                    return 
                except requests.exceptions.RequestException:
                    if attempt < max_retries - 1:
                        time.sleep(1 + attempt) 
                    else:
                        err_payload = {
                            "status": "ERROR", 
                            "message": f"Network failure connecting to Hub (Failed {max_retries} attempts).", 
                            "attendee": None,
                            "timestamp": datetime.now().isoformat(),
                            "device": self.config_manager.config["device_name"]
                        }
                        self.scan_queue.put(err_payload)

        threading.Thread(target=_post_with_retries, daemon=True).start()
        
        # Reset Input fix preserving active focus
        def reset_inputs():
            for entry in (self.ent_id, self.ent_phone):
                entry.delete(0, END)
                if self.focus_get() == entry:
                    entry.configure(foreground='') 
                else:
                    entry.event_generate('<FocusOut>')

        self.gui_queue.put(reset_inputs)

    def open_settings(self):
        SettingsDialog(self, self.config_manager, self.on_settings_saved)

    def on_settings_saved(self):
        self.lbl_subtitle.config(text=f"{self.config_manager.config['device_name']} • TDE UP 2026")
        self.is_polling = False
        time.sleep(0.5)
        self.start_threads()

if __name__ == "__main__":
    app = GateDisplay()
    app.mainloop()