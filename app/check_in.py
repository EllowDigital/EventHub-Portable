import os
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
import requests
import urllib3
from datetime import datetime
import tkinter as tk
from tkinter import messagebox, filedialog

import ttkbootstrap as ttk
from ttkbootstrap.constants import *
from PIL import Image, ImageTk, ImageOps

# Custom tone generator for Windows (bypasses default OS theme sounds)
try:
    if platform.system() == "Windows":
        import winsound
        HAS_WINSOUND = True
    else:
        HAS_WINSOUND = False
except ImportError:
    HAS_WINSOUND = False

# Text-to-Speech engine fallback for Mac/Linux
try:
    import pyttsx3
    HAS_TTS = True
except ImportError:
    HAS_TTS = False

# Suppress warnings for local adhoc HTTPS certificates
urllib3.disable_warnings(urllib3.exceptions.InsecureRequestWarning)

# ==============================================================================
# 24/7 STABILITY: GLOBAL CRASH HANDLER
# ==============================================================================
def global_exception_handler(*args):
    logging.error("Uncaught GUI Exception intercepted. App remains running.", exc_info=args)

tk.Tk.report_callback_exception = global_exception_handler

BASE_DIR = os.path.dirname(os.path.abspath(__file__))
CONFIG_PATH = os.path.join(BASE_DIR, 'config', 'checkin.json')
DEFAULT_PHOTO_DIR = 'attendee_photos'

LOG_DIR = os.path.join(BASE_DIR, 'logs')
os.makedirs(LOG_DIR, exist_ok=True)
LOG_FILE = os.path.join(LOG_DIR, 'gate_display.log')
_file_handler = RotatingFileHandler(LOG_FILE, maxBytes=5_000_000, backupCount=3, encoding='utf-8')
_file_handler.setFormatter(logging.Formatter('%(asctime)s [%(levelname)s] %(message)s'))
logging.basicConfig(level=logging.INFO, handlers=[_file_handler])

CATEGORY_STYLES = {
    "business": "warning",    
    "general": "primary",     
    "media": "danger",        
    "exhibitor": "info",      
    "default": "secondary"    
}

# ==============================================================================
# SETTINGS MANAGER
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
        if os.path.exists(CONFIG_PATH):
            try:
                with open(CONFIG_PATH, 'r') as f:
                    content = f.read().strip()
                    if content:
                        self.config.update(json.loads(content))
            except (json.JSONDecodeError, ValueError) as e:
                logging.error(f"Config corrupted. Resetting to defaults. Error: {e}")
                self.save()
            except Exception as e:
                logging.error(f"Error loading config: {e}")

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
        self.geometry("520x500") 
        self.resizable(False, False)
        self.config_manager = config_manager
        self.on_save = on_save_callback
        self.attributes('-topmost', True)

        self.build_ui()
        self.center_window(parent)

    def center_window(self, parent):
        self.update_idletasks()
        x = parent.winfo_x() + (parent.winfo_width() // 2) - (self.winfo_width() // 2)
        y = parent.winfo_y() + (parent.winfo_height() // 2) - (self.winfo_height() // 2)
        self.geometry(f"+{max(x, 0)}+{max(y, 0)}")

    def build_ui(self):
        frame = ttk.Frame(self, padding=30)
        frame.pack(fill=BOTH, expand=True)

        ttk.Label(frame, text="⚙️ Terminal Settings", font="-size 18 -weight bold", bootstyle=PRIMARY).pack(anchor=W, pady=(0, 25))

        ttk.Label(frame, text="Hub Server URL (HTTP/HTTPS)", font="-weight bold").pack(anchor=W)
        self.ent_url = ttk.Entry(frame, font="-size 11")
        self.ent_url.insert(0, self.config_manager.config["hub_url"])
        self.ent_url.pack(fill=X, pady=(5, 20), ipady=6)

        ttk.Label(frame, text="Device Identifier Name", font="-weight bold").pack(anchor=W)
        self.ent_device = ttk.Entry(frame, font="-size 11")
        self.ent_device.insert(0, self.config_manager.config["device_name"])
        self.ent_device.pack(fill=X, pady=(5, 20), ipady=6)

        ttk.Label(frame, text="Local Photo Directory (Relative Path)", font="-weight bold").pack(anchor=W)
        photo_frame = ttk.Frame(frame)
        photo_frame.pack(fill=X, pady=(5, 30))
        
        self.ent_photo = ttk.Entry(photo_frame, font="-size 11")
        self.ent_photo.insert(0, self.config_manager.config["photo_directory"])
        self.ent_photo.pack(side=LEFT, fill=X, expand=True, padx=(0, 10), ipady=6)
        ttk.Button(photo_frame, text="Browse", bootstyle=SECONDARY, command=self.browse_dir).pack(side=RIGHT, ipady=6)

        btn_frame = ttk.Frame(frame)
        btn_frame.pack(fill=X, side=BOTTOM, pady=(10, 0))
        ttk.Button(btn_frame, text="💾 Save & Apply", bootstyle=SUCCESS, command=self.save).pack(fill=X, pady=5, ipady=8)
        ttk.Button(btn_frame, text="Cancel", bootstyle=SECONDARY, command=self.destroy).pack(fill=X, ipady=8)

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
        super().__init__(themename="darkly", title="TDE UP 2026 — Gate Terminal")
        self.geometry("1440x900")
        self.minsize(1280, 750)
        
        self.update_idletasks()
        sw, sh = self.winfo_screenwidth(), self.winfo_screenheight()
        self.geometry(f"+{(sw - 1440) // 2}+{(sh - 900) // 2 - 20}")
        
        self.current_theme = "darkly"
        self.config_manager = ConfigManager()
        
        self.gui_queue = queue.Queue()
        self.scan_queue = queue.Queue()  
        
        self.is_polling = False
        self.sound_enabled = True
        self.stats = {"Success": 0, "Duplicate": 0, "Wrong Day": 0, "Errors": 0}
        self.recent_scans = []
        
        self.current_photo = None
        self._placeholder_img_cache = {}
        self._last_scan_time = 0.0
        
        # Deduplication cache to prevent ghost thread events. Increased size for safety with 4 phones.
        self._processed_sigs = collections.deque(maxlen=100) 

        self.stream_session = None
        self.api_session = None

        self.build_ui()
        self.process_queues()
        self.start_threads()

    def toggle_theme(self):
        self.current_theme = "flatly" if self.current_theme == "darkly" else "darkly"
        self.style.theme_use(self.current_theme)
        
        for e in (self.ent_id, self.ent_phone):
            if "e.g." in e.get():
                e.configure(foreground="gray")
            else:
                e.configure(foreground="") 
                
        if self.lbl_attendee_id.cget("text") == "---":
            self.set_placeholder_photo()

    def build_ui(self):
        self.nav = ttk.Frame(self, padding=20)
        self.nav.pack(fill=X)
        
        title_frame = ttk.Frame(self.nav)
        title_frame.pack(side=LEFT)
        
        ttk.Label(title_frame, text="🎟️ Gate Display Terminal", font="-size 22 -weight bold", bootstyle=PRIMARY).pack(anchor=W)
        self.lbl_subtitle = ttk.Label(title_frame, text=f"{self.config_manager.config['device_name']} • TDE UP 2026", font="-size 11 -weight bold", bootstyle=SECONDARY)
        self.lbl_subtitle.pack(anchor=W, pady=(2, 0))

        controls = ttk.Frame(self.nav)
        controls.pack(side=RIGHT)
        
        ttk.Button(controls, text="🌗 Theme", bootstyle="outline-secondary", command=self.toggle_theme).pack(side=LEFT, padx=6)
        ttk.Button(controls, text="⚙️ Settings", bootstyle="outline-secondary", command=self.open_settings).pack(side=LEFT, padx=6)
        
        self.btn_sound = ttk.Button(controls, text="🔊 Sound", bootstyle="outline-info", command=self.toggle_sound)
        self.btn_sound.pack(side=LEFT, padx=6)
        
        ttk.Button(controls, text="⛶ Fullscreen", bootstyle="outline-secondary", command=lambda: self.attributes('-fullscreen', not self.attributes('-fullscreen'))).pack(side=LEFT, padx=6)
        
        self.net_pill = ttk.Frame(controls, borderwidth=1, relief="solid", bootstyle="dark", padding=(15, 8))
        self.net_pill.pack(side=LEFT, padx=25)
        
        self.lbl_hub_status = ttk.Label(self.net_pill, text="● Connecting...", font="-weight bold -size 12", bootstyle=WARNING)
        self.lbl_hub_status.pack(side=LEFT)

        ttk.Separator(self, orient=HORIZONTAL).pack(fill=X)

        self.test_banner = ttk.Frame(self, bootstyle=DANGER)
        self.lbl_test_mode = ttk.Label(self.test_banner, text="⚠️ TEST MODE ACTIVE", font="-weight bold -size 14", bootstyle="inverse-danger")
        self.lbl_test_mode.pack(pady=10)
        
        self.content = ttk.Frame(self, padding=30)
        self.content.pack(fill=BOTH, expand=True)

        left_panel = ttk.Frame(self.content)
        left_panel.pack(side=LEFT, fill=BOTH, expand=True, padx=(0, 30))

        self.status_banner = ttk.Label(left_panel, text="WAITING FOR SCAN...", font="-size 34 -weight bold", bootstyle="inverse-secondary", anchor=CENTER)
        self.status_banner.pack(fill=X, pady=(0, 30), ipady=35)

        profile_frame = ttk.Frame(left_panel)
        profile_frame.pack(fill=BOTH, expand=True)

        photo_container = ttk.Frame(profile_frame, width=360)
        photo_container.pack(side=LEFT, fill=Y, padx=(0, 35))
        photo_container.pack_propagate(False) 
        
        photo_border = ttk.Frame(photo_container, bootstyle=SECONDARY, padding=3)
        photo_border.pack(fill=BOTH, expand=True)
        
        self.lbl_photo = ttk.Label(photo_border, anchor=CENTER)
        self.lbl_photo.pack(fill=BOTH, expand=True)
        
        self.lbl_attendee_id = ttk.Label(photo_container, text="---", font="-size 15 -weight bold", bootstyle=SECONDARY, anchor=CENTER)
        self.lbl_attendee_id.pack(pady=15)
        self.set_placeholder_photo()

        details = ttk.Frame(profile_frame)
        details.pack(side=LEFT, fill=BOTH, expand=True)
        
        header_frame = ttk.Frame(details)
        header_frame.pack(fill=X, pady=(10, 0))
        header_frame.columnconfigure(0, weight=1) 
        header_frame.columnconfigure(1, weight=0) 
        
        self.lbl_name = ttk.Label(header_frame, text="SCAN TICKET", font="-size 40 -weight bold", bootstyle=DEFAULT, wraplength=580)
        self.lbl_name.grid(row=0, column=0, sticky=NW, pady=(0, 5))
        
        self.lbl_pass_badge = ttk.Label(header_frame, text="PENDING", font="-size 18 -weight bold", bootstyle="inverse-secondary", padding=(25, 10))
        self.lbl_pass_badge.grid(row=0, column=1, sticky=NE, padx=(10, 0))

        self.lbl_company = ttk.Label(details, text="Awaiting attendee details...", font="-size 18", bootstyle=INFO, wraplength=580)
        self.lbl_company.pack(anchor=W, pady=(8, 35))

        grid = ttk.Frame(details)
        grid.pack(fill=BOTH, expand=True)
        grid.columnconfigure(0, weight=1, minsize=220)
        grid.columnconfigure(1, weight=1, minsize=220)
        
        self.fields = {}
        row_col = [
            (0, 0, "📱 Mobile Number", "mobile"), (0, 1, "📍 Location", "location"), 
            (1, 0, "🏷️ Category", "category"), (1, 1, "👤 Gender", "gender"), 
            (2, 0, "📅 Event Date", "date"), (2, 1, "📡 Scanner ID", "scanner")
        ]
        
        for r, c, label, key in row_col:
            f = ttk.Frame(grid, padding=12)
            f.grid(row=r, column=c, sticky=NSEW, padx=6, pady=6)
            ttk.Label(f, text=f"{label.upper()}", font="-size 11 -weight bold", bootstyle=SECONDARY).pack(anchor=W)
            val = ttk.Label(f, text="---", font="-size 16 -weight bold", wraplength=280)
            val.pack(anchor=W, pady=(6,0))
            self.fields[key] = val

        self.bottom_banner = ttk.Label(left_panel, text="READY FOR OPERATIONS", font="-size 18 -weight bold", bootstyle="inverse-secondary", anchor=CENTER)
        self.bottom_banner.pack(fill=X, side=BOTTOM, ipady=22)

        right_panel = ttk.Frame(self.content, width=440)
        right_panel.pack(side=RIGHT, fill=Y)
        right_panel.pack_propagate(False)

        lookup = ttk.Labelframe(right_panel, text=" 🔍 Manual Entry ", padding=25)
        lookup.pack(fill=X, pady=(0, 25))
        
        self.ent_phone = self.create_placeholder_entry(lookup, "Phone Number (e.g. 90000...)")
        self.ent_phone.pack(fill=X, pady=(0, 15), ipady=8)
        self.ent_phone.bind("<Return>", lambda e: self.manual_scan('phone'))
        
        self.ent_id = self.create_placeholder_entry(lookup, "Attendee ID (e.g. TDE26...)")
        self.ent_id.pack(fill=X, pady=(0, 18), ipady=8)
        self.ent_id.bind("<Return>", lambda e: self.manual_scan('id'))

        # CORRECTED LINE: Removed font="-weight bold" which ttk.Button does not support
        ttk.Button(lookup, text="PROCESS MANUAL SCAN", bootstyle=SUCCESS, command=self.handle_manual_submit).pack(fill=X, ipady=8)

        stats_frame = ttk.Frame(right_panel)
        stats_frame.pack(fill=X, pady=(0, 30))
        
        self.stat_labels = {}
        for idx, (title, color) in enumerate([("Success", SUCCESS), ("Duplicate", WARNING), ("Wrong Day", SECONDARY), ("Errors", DANGER)]):
            f = ttk.Frame(stats_frame, borderwidth=1, relief=SOLID, padding=15)
            f.grid(row=idx//2, column=idx%2, sticky=NSEW, padx=5, pady=5)
            stats_frame.columnconfigure(idx%2, weight=1)
            
            val = ttk.Label(f, text="0", font="-size 26 -weight bold", bootstyle=color)
            val.pack(anchor=CENTER)
            ttk.Label(f, text=title.upper(), font="-size 11 -weight bold", bootstyle=SECONDARY).pack(anchor=CENTER)
            self.stat_labels[title] = val

        ttk.Label(right_panel, text="🕒 RECENT ACTIVITY", font="-size 13 -weight bold", bootstyle=PRIMARY).pack(anchor=W, pady=(0, 12))
        self.list_frame = ttk.Frame(right_panel)
        self.list_frame.pack(fill=BOTH, expand=True)

    def create_placeholder_entry(self, parent, placeholder_text):
        entry = ttk.Entry(parent, font="-size 12")
        entry.insert(0, placeholder_text)
        entry.configure(foreground='gray')

        def on_focus_in(event):
            if entry.get() == placeholder_text:
                entry.delete(0, END)
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

    def play_sound(self, status, message="", attendee_name=""):
        if not self.sound_enabled:
            return
        
        def _play():
            # 1. Play Tone Chimes (Kept for immediate feedback without overlapping speech delays)
            if HAS_WINSOUND:
                try:
                    if status == "SUCCESS":
                        winsound.Beep(2000, 100)  
                    elif status == "DUPLICATE":
                        winsound.Beep(1000, 100) 
                        time.sleep(0.05)
                        winsound.Beep(1000, 100)
                    else:
                        if "Denied" in message:
                            winsound.Beep(400, 150)
                            winsound.Beep(300, 300)
                        else:
                            winsound.Beep(200, 600)
                except Exception:
                    self.bell() 
            else:
                self.bell()
                if status != "SUCCESS":
                    time.sleep(0.2)
                    self.bell()

            # 2. Text-to-Speech Voice Engine (Removed SUCCESS and DUPLICATE speech)
            speak_text = ""
            if status not in ["SUCCESS", "DUPLICATE"]:
                speak_text = "Access Denied."

            # Only run the heavy TTS engine if there's actually a message to speak
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
                        subprocess.run(
                            ["powershell", "-Command", ps_script], 
                            creationflags=subprocess.CREATE_NO_WINDOW
                        )
                    elif HAS_TTS:
                        engine = pyttsx3.init()
                        voices = engine.getProperty('voices')
                        for voice in voices:
                            if 'female' in voice.name.lower() or 'zira' in voice.name.lower() or 'samantha' in voice.name.lower():
                                engine.setProperty('voice', voice.id)
                                break
                        engine.say(speak_text)
                        engine.runAndWait()
                except Exception as e:
                    logging.error(f"TTS Error: {e}")

        threading.Thread(target=_play, daemon=True).start()

    def set_placeholder_photo(self):
        bg_color = '#e9ecef' if self.current_theme == "flatly" else '#222222'
        
        if bg_color not in self._placeholder_img_cache:
            img = Image.new('RGB', (340, 340), color=bg_color)
            self._placeholder_img_cache[bg_color] = ImageTk.PhotoImage(img)
            
        self.current_photo = self._placeholder_img_cache[bg_color]
        self.lbl_photo.configure(image=self.current_photo)

    def async_load_photo(self, attendee_id):
        def _load():
            rel_dir = self.config_manager.config.get("photo_directory", DEFAULT_PHOTO_DIR)
            abs_directory = os.path.normpath(os.path.join(BASE_DIR, rel_dir))
            
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
                        logging.error(f"Error loading photo: {e}")
            
            if not photo_found:
                self.gui_queue.put(self.set_placeholder_photo)

        threading.Thread(target=_load, daemon=True).start()

    def update_photo_ui(self, photo_image):
        self.current_photo = photo_image  
        self.lbl_photo.configure(image=self.current_photo)

    def process_queues(self):
        for _ in range(50):
            try: self.gui_queue.get_nowait()()
            except queue.Empty: break

        for _ in range(5):
            try:
                event_data = self.scan_queue.get_nowait()
                self.update_ui_with_event(event_data)
            except queue.Empty: break

        self.after(30, self.process_queues)

    def update_ui_with_event(self, event_data):
        status_type = event_data.get("status", "ERROR")
        message = event_data.get("message", "Unknown error")
        attendee = event_data.get("attendee")
        scanner_dev = event_data.get("device", "Unknown Scanner")
        
        raw_ts = event_data.get("timestamp", "")
        
        # --- EVENT DEDUPLICATION ---
        aid = attendee.get("attendee_id", "unknown") if attendee else "none"
        event_sig = f"{raw_ts}_{aid}_{status_type}"
        
        if event_sig in self._processed_sigs:
            return 
            
        self._processed_sigs.append(event_sig)

        time_str = datetime.now().strftime("%I:%M %p")
        if raw_ts:
            try:
                dt = datetime.fromisoformat(raw_ts.replace('Z', '+00:00'))
                time_str = dt.astimezone().strftime("%I:%M %p")
            except Exception: pass

        configs = {
            "SUCCESS": {"color": "success", "banner": "✅ ACCESS GRANTED", "bottom": "SUCCESSFULLY CHECKED IN"},
            "DUPLICATE": {"color": "warning", "banner": "⚠️ ALREADY SCANNED", "bottom": "DUPLICATE SCAN DETECTED"},
            "ERROR": {"color": "danger", "banner": "❌ ACCESS DENIED", "bottom": message}
        }
        cfg = configs.get(status_type, configs["ERROR"])
        c_style = cfg["color"]
        
        self.status_banner.configure(text=cfg["banner"], bootstyle=f"inverse-{c_style}")
        self.bottom_banner.configure(text=cfg["bottom"], bootstyle=f"inverse-{c_style}")
        
        # Trigger the Sound Alert
        att_name = attendee.get("full_name", "") if attendee else ""
        self.play_sound(status_type, message, att_name)

        if attendee:
            category_raw = str(attendee.get("attendee_type", "")).lower()
            badge_style = CATEGORY_STYLES.get(category_raw, CATEGORY_STYLES["default"])
            
            self.lbl_pass_badge.configure(
                bootstyle=f"inverse-{badge_style}", 
                text=category_raw.upper() if category_raw else "UNKNOWN"
            )

            self.lbl_name.configure(text=attendee.get("full_name", "").upper(), bootstyle="default")
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
            self.lbl_name.configure(text="UNKNOWN RECORD", bootstyle=DANGER)
            self.lbl_company.configure(text="---", bootstyle=SECONDARY)
            self.lbl_attendee_id.configure(text="---")
            self.lbl_pass_badge.configure(bootstyle="inverse-secondary", text="N/A")
            
            for lbl in self.fields.values(): lbl.configure(text="---")
            self.set_placeholder_photo()

        if status_type in ["SUCCESS", "DUPLICATE", "ERROR"]:
            key = "Success" if status_type == "SUCCESS" else ("Duplicate" if status_type == "DUPLICATE" else "Errors")
            self.stats[key] += 1
            self.stat_labels[key].configure(text=str(self.stats[key]))

    def add_recent_scan(self, name, att_id, style, time_str):
        card = ttk.Frame(self.list_frame, bootstyle=style, borderwidth=1, relief=SOLID)
        card.pack(fill=X, pady=5, padx=2)
        
        lbl_style = f"inverse-{style}"
        
        top = ttk.Frame(card, bootstyle=style)
        top.pack(fill=X, padx=14, pady=(10, 0))
        ttk.Label(top, text=f"👤 {name}", font="-size 12 -weight bold", bootstyle=lbl_style).pack(side=LEFT)
        ttk.Label(top, text=time_str, font="-size 10", bootstyle=lbl_style).pack(side=RIGHT)
        
        bot = ttk.Frame(card, bootstyle=style)
        bot.pack(fill=X, padx=14, pady=(4, 10))
        ttk.Label(bot, text=att_id, font="-size 10", bootstyle=lbl_style).pack(side=LEFT)
        ttk.Label(bot, text="✓ OK" if style=="success" else "⚠ WARN", font="-weight bold", bootstyle=lbl_style).pack(side=RIGHT)
        
        self.recent_scans.insert(0, card)
        if len(self.recent_scans) > 5:
            old = self.recent_scans.pop()
            old.destroy()

    def start_threads(self):
        self.is_polling = False
        try:
            if self.stream_session: self.stream_session.close()
            if self.api_session: self.api_session.close()
        except Exception: pass
        
        # Fresh isolated network pools
        self.stream_session = requests.Session()
        self.stream_session.headers.update({"User-Agent": "EventHub-GateDisplay-Stream/1.0", "Connection": "keep-alive"})
        
        self.api_session = requests.Session()
        self.api_session.headers.update({"User-Agent": "EventHub-GateDisplay-API/1.0", "Connection": "close"})

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
            
            start_t = time.time()
            try:
                resp = self.api_session.get(url, timeout=3, verify=False)
                latency = int((time.time() - start_t) * 1000)
                
                if resp.status_code == 200:
                    data = resp.json()
                    self.gui_queue.put(lambda l=latency, tm=data.get("test_mode", False), td=data.get("test_date", "Unknown"): (
                        self.update_net_pill(f"● Connected • {l}ms", "success" if l < 200 else "warning"),
                        self.update_test_banner(tm, td)
                    ))
                else:
                    self.gui_queue.put(lambda l=latency: (
                        self.update_net_pill(f"● Error {resp.status_code} • {l}ms", "danger"),
                        self.update_test_banner(False, "")
                    ))
            except Exception:
                self.gui_queue.put(lambda: (
                    self.update_net_pill("● Offline / Timeout", "danger"),
                    self.update_test_banner(False, "")
                ))
            
            time.sleep(3)

    def update_test_banner(self, is_test_mode, test_date):
        if is_test_mode:
            self.lbl_test_mode.configure(text=f"⚠️ TEST MODE ACTIVE (OVERRIDE: {test_date})")
            if not self.test_banner.winfo_ismapped(): self.test_banner.pack(fill=X, before=self.content)
        else:
            if self.test_banner.winfo_ismapped(): self.test_banner.pack_forget()

    def listen_to_server_stream(self):
        while self.is_polling:
            hub_url = self.config_manager.config.get('hub_url', '').rstrip('/')
            url = f"{hub_url}/api/stream-scans"
            
            try:
                with self.stream_session.get(url, stream=True, timeout=(5, 20), verify=False) as response:
                    if response.status_code == 200:
                        for line in response.iter_lines():
                            if not self.is_polling: break
                            if line:
                                decoded = line.decode('utf-8')
                                if decoded.startswith("data: "):
                                    try: self.scan_queue.put(json.loads(decoded[6:]))
                                    except Exception: pass
                    else:
                        time.sleep(3)
            except Exception:
                time.sleep(3)

    def update_net_pill(self, text, style_name):
        self.lbl_hub_status.configure(text=text, bootstyle=style_name)

    def manual_scan(self, lookup_type):
        current_time = time.time()
        if current_time - getattr(self, '_last_scan_time', 0) < 1.5:
            return 
        self._last_scan_time = current_time

        val = self.ent_phone.get() if lookup_type == 'phone' else self.ent_id.get()
        if "e.g." in val or not val.strip(): return
        
        url = f"{self.config_manager.config['hub_url'].rstrip('/')}/api/checkin"
        payload = {
            "attendee_id": val.strip(),
            "search_type": lookup_type,
            "device_name": self.config_manager.config["device_name"]
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
            for entry in (self.ent_id, self.ent_phone):
                entry.delete(0, END)
                if self.focus_get() == entry: entry.configure(foreground='') 
                else: entry.event_generate('<FocusOut>')

        self.gui_queue.put(reset_inputs)

    def open_settings(self):
        SettingsDialog(self, self.config_manager, self.on_settings_saved)

    def on_settings_saved(self):
        self.lbl_subtitle.config(text=f"{self.config_manager.config['device_name']} • TDE UP 2026")
        time.sleep(0.5)
        self.start_threads()

if __name__ == "__main__":
    app = GateDisplay()
    app.mainloop()