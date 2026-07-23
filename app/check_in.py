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

urllib3.disable_warnings(urllib3.exceptions.InsecureRequestWarning)

BASE_DIR = os.path.dirname(os.path.abspath(__file__))
CONFIG_PATH = os.path.join(BASE_DIR, 'config', 'checkin.json')
DEFAULT_PHOTO_DIR = 'attendee_photos'

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

        # URL Input
        ttk.Label(frame, text="Hub URL", font="-weight bold").pack(anchor=W)
        self.ent_url = ttk.Entry(frame)
        self.ent_url.insert(0, self.config_manager.config["hub_url"])
        self.ent_url.pack(fill=X, pady=(0, 15))

        # Device Input
        ttk.Label(frame, text="Device Name", font="-weight bold").pack(anchor=W)
        self.ent_device = ttk.Entry(frame)
        self.ent_device.insert(0, self.config_manager.config["device_name"])
        self.ent_device.pack(fill=X, pady=(0, 15))

        # Directory Input
        ttk.Label(frame, text="Photo Directory (Relative to App)", font="-weight bold").pack(anchor=W)
        photo_frame = ttk.Frame(frame)
        photo_frame.pack(fill=X, pady=(0, 20))
        
        self.ent_photo = ttk.Entry(photo_frame)
        self.ent_photo.insert(0, self.config_manager.config["photo_directory"])
        self.ent_photo.pack(side=LEFT, fill=X, expand=True, padx=(0, 5))
        ttk.Button(photo_frame, text="Browse", bootstyle=SECONDARY, command=self.browse_dir).pack(side=RIGHT)

        # Buttons
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
        super().__init__(themename="darkly", title="Gate Display Terminal")
        self.geometry("1400x850")
        self.minsize(1200, 700)

        self.config_manager = ConfigManager()
        self.gui_queue = queue.Queue()
        self.scan_queue = queue.Queue()  # Dedicated pacing queue for simultaneous scans
        
        self.is_polling = True
        self.sound_enabled = True
        self.stats = {"Success": 0, "Duplicate": 0, "Wrong Day": 0, "Errors": 0}
        self.recent_scans = []

        self.build_ui()
        self.after(100, self.process_queue)
        self.after(100, self.process_scan_queue)  # Start the visual queue worker
        self.start_stream_thread()

    def build_ui(self):
        # --- TOP NAVBAR ---
        nav = ttk.Frame(self, padding=10, bootstyle=DARK)
        nav.pack(fill=X)
        
        title_frame = ttk.Frame(nav, bootstyle=DARK)
        title_frame.pack(side=LEFT)
        ttk.Label(title_frame, text="🎟️ Gate Display Terminal", font="-size 16 -weight bold", bootstyle=INVERSE).pack(anchor=W)
        self.lbl_subtitle = ttk.Label(title_frame, text=f"{self.config_manager.config['device_name']} • TDE UP 2026", font="-size 9", foreground="#888")
        self.lbl_subtitle.pack(anchor=W)

        controls = ttk.Frame(nav, bootstyle=DARK)
        controls.pack(side=RIGHT)
        
        ttk.Button(controls, text="⚙️", bootstyle="outline-light", command=self.open_settings).pack(side=LEFT, padx=5)
        
        # Audio Button
        self.btn_sound = ttk.Button(controls, text="🔊", bootstyle="outline-light", command=self.toggle_sound)
        self.btn_sound.pack(side=LEFT, padx=5)
        
        ttk.Button(controls, text="⛶", bootstyle="outline-light", command=lambda: self.attributes('-fullscreen', not self.attributes('-fullscreen'))).pack(side=LEFT, padx=5)
        
        self.lbl_hub_status = ttk.Label(controls, text="● Connecting...", font="-weight bold", bootstyle=WARNING)
        self.lbl_hub_status.pack(side=LEFT, padx=15)

        # --- TEST MODE BANNER ---
        test_banner = ttk.Frame(self, bootstyle=WARNING)
        test_banner.pack(fill=X)
        ttk.Label(test_banner, text="🧪 Live Event Mode", font="-weight bold", bootstyle="inverse-warning").pack(side=LEFT, padx=20, pady=5)
        
        # --- MAIN CONTENT GRID ---
        content = ttk.Frame(self, padding=20)
        content.pack(fill=BOTH, expand=True)

        # LEFT PANEL (Display)
        left_panel = ttk.Frame(content)
        left_panel.pack(side=LEFT, fill=BOTH, expand=True, padx=(0, 20))

        # Banner
        self.status_banner = ttk.Label(left_panel, text="WAITING FOR SCAN...", font="-size 24 -weight bold", bootstyle="inverse-secondary", anchor=CENTER)
        self.status_banner.pack(fill=X, pady=(0, 20), ipady=20)

        # Profile Frame
        profile_frame = ttk.Frame(left_panel)
        profile_frame.pack(fill=BOTH, expand=True)

        # Photo Container
        photo_container = ttk.Frame(profile_frame, width=320)
        photo_container.pack(side=LEFT, fill=Y, padx=(0, 40))
        photo_container.pack_propagate(False) # Keep sizing consistent
        
        self.lbl_photo = ttk.Label(photo_container, background="#111", anchor=CENTER)
        self.lbl_photo.pack(fill=BOTH, expand=True)
        self.lbl_attendee_id = ttk.Label(photo_container, text="---", font="-size 10", foreground="#888", anchor=CENTER)
        self.lbl_attendee_id.pack(pady=10)
        self.set_placeholder_photo()

        # Attendee Details Container
        details = ttk.Frame(profile_frame)
        details.pack(side=LEFT, fill=BOTH, expand=True)
        
        self.lbl_name = ttk.Label(details, text="SCAN TICKET", font="-size 38 -weight bold")
        self.lbl_name.pack(anchor=W, pady=(10, 0))
        self.lbl_company = ttk.Label(details, text="To view attendee details", font="-size 16 -weight bold", bootstyle=INFO)
        self.lbl_company.pack(anchor=W, pady=(0, 30))

        # Rewired Details Grid - Responsive weights
        grid = ttk.Frame(details)
        grid.pack(fill=BOTH, expand=True)
        grid.columnconfigure(0, weight=1)
        grid.columnconfigure(1, weight=1)
        
        self.fields = {}
        row_col = [(0,0, "Mobile"), (0,1, "Location"), (1,0, "Category"), (1,1, "Gender"), (2,0, "Event Day"), (2,1, "Scanner")]
        
        for r, c, label in row_col:
            f = ttk.Frame(grid)
            f.grid(row=r, column=c, sticky=NSEW, padx=10, pady=15)
            ttk.Label(f, text=f"{label.upper()}", font="-size 9 -weight bold", foreground="#666").pack(anchor=W)
            val = ttk.Label(f, text="---", font="-size 14 -weight bold")
            val.pack(anchor=W, pady=(2,0))
            self.fields[label] = val

        # Bottom Success Banner
        self.bottom_banner = ttk.Label(left_panel, text="READY", font="-size 14 -weight bold", bootstyle="inverse-dark", anchor=CENTER)
        self.bottom_banner.pack(fill=X, side=BOTTOM, ipady=15)

        # RIGHT PANEL (Sidebar)
        right_panel = ttk.Frame(content, width=380)
        right_panel.pack(side=RIGHT, fill=Y)
        right_panel.pack_propagate(False)

        # Manual Lookup
        lookup = ttk.Labelframe(right_panel, text=" 🔍 Manual Lookup ", padding=15)
        lookup.pack(fill=X, pady=(0, 20))
        
        self.ent_phone = self.create_placeholder_entry(lookup, "Phone Number (e.g. 90000...)")
        self.ent_phone.pack(fill=X, pady=(0, 10))
        self.ent_phone.bind("<Return>", lambda e: self.manual_scan('phone'))
        
        self.ent_id = self.create_placeholder_entry(lookup, "Attendee ID (e.g. TDE26...)")
        self.ent_id.pack(fill=X, pady=(0, 10))
        self.ent_id.bind("<Return>", lambda e: self.manual_scan('id'))

        ttk.Button(lookup, text="Submit Manual Scan", bootstyle=PRIMARY, command=self.handle_manual_submit).pack(fill=X)

        # Stats Board
        stats_frame = ttk.Frame(right_panel)
        stats_frame.pack(fill=X, pady=(0, 20))
        
        self.stat_labels = {}
        for idx, (title, color) in enumerate([("Success", SUCCESS), ("Duplicate", WARNING), ("Wrong Day", SECONDARY), ("Errors", DANGER)]):
            f = ttk.Frame(stats_frame, bootstyle="dark", borderwidth=1, relief=SOLID, padding=10)
            f.grid(row=idx//2, column=idx%2, sticky=NSEW, padx=3, pady=3)
            stats_frame.columnconfigure(idx%2, weight=1)
            
            val = ttk.Label(f, text="0", font="-size 20 -weight bold", bootstyle=color)
            val.pack(anchor=CENTER)
            ttk.Label(f, text=title, font="-size 9", foreground="#888").pack(anchor=CENTER)
            self.stat_labels[title] = val

        # Recent Scans Log
        ttk.Label(right_panel, text="🕒 Recent Scans", font="-weight bold").pack(anchor=W, pady=(0, 5))
        self.list_frame = ttk.Frame(right_panel)
        self.list_frame.pack(fill=BOTH, expand=True)

    def create_placeholder_entry(self, parent, placeholder_text):
        entry = ttk.Entry(parent)
        entry.insert(0, placeholder_text)
        entry.config(foreground='gray')

        def on_focus_in(event):
            if entry.get() == placeholder_text:
                entry.delete(0, END)
                entry.config(foreground='white')

        def on_focus_out(event):
            if not entry.get():
                entry.insert(0, placeholder_text)
                entry.config(foreground='gray')

        entry.bind("<FocusIn>", on_focus_in)
        entry.bind("<FocusOut>", on_focus_out)
        return entry

    def handle_manual_submit(self):
        if self.ent_id.get() and "Attendee ID" not in self.ent_id.get():
            self.manual_scan('id')
        elif self.ent_phone.get() and "Phone Number" not in self.ent_phone.get():
            self.manual_scan('phone')

    # --- AUDIO LOGIC ---
    def toggle_sound(self):
        self.sound_enabled = not self.sound_enabled
        self.btn_sound.configure(
            text="🔊" if self.sound_enabled else "🔇", 
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

    def set_placeholder_photo(self):
        img = Image.new('RGB', (320, 320), color='#1e1e1e')
        self.current_photo = ImageTk.PhotoImage(img)
        self.lbl_photo.configure(image=self.current_photo)

    def load_photo(self, attendee_id):
        rel_dir = self.config_manager.config.get("photo_directory", DEFAULT_PHOTO_DIR)
        abs_directory = os.path.normpath(os.path.join(BASE_DIR, rel_dir))
        
        for ext in ['.jpg', '.png', '.jpeg']:
            path = os.path.join(abs_directory, f"{attendee_id}{ext}")
            if os.path.exists(path):
                try:
                    img = Image.open(path)
                    img = ImageOps.fit(img, (320, 320), Image.Resampling.LANCZOS)
                    self.current_photo = ImageTk.PhotoImage(img)
                    self.lbl_photo.configure(image=self.current_photo)
                    return
                except Exception as e:
                    print(f"Error loading photo: {e}")
        self.set_placeholder_photo()

    def process_queue(self):
        try:
            while True:
                task = self.gui_queue.get_nowait()
                task()
        except queue.Empty:
            pass
        finally:
            self.after(100, self.process_queue)

    # --- DYNAMIC VISUAL PACING QUEUE ---
    def process_scan_queue(self):
        try:
            if not self.scan_queue.empty():
                event_data = self.scan_queue.get_nowait()
                self.update_ui_with_event(event_data)
                
                # Dynamic Pacing: 
                # If there's a backlog (> 1 item), speed up to 1 second per scan.
                # Otherwise, display for a full 2.5 seconds for optimal readability.
                q_len = self.scan_queue.qsize()
                delay_ms = 1000 if q_len > 1 else 2500
                
                self.after(delay_ms, self.process_scan_queue)
                return
        except queue.Empty:
            pass
            
        self.after(100, self.process_scan_queue)

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
            "SUCCESS": {"color": "success", "banner": "✅ CHECKED IN", "bottom": "SUCCESSFULLY CHECKED IN"},
            "DUPLICATE": {"color": "warning", "banner": "⚠️ ALREADY CHECKED IN", "bottom": "DUPLICATE SCAN DETECTED"},
            "ERROR": {"color": "danger", "banner": "❌ SCAN ERROR", "bottom": message}
        }
        cfg = configs.get(status_type, configs["ERROR"])
        c_style = cfg["color"]
        
        # UI Updates
        self.status_banner.configure(text=cfg["banner"], bootstyle=f"inverse-{c_style}")
        self.bottom_banner.configure(text=cfg["bottom"], bootstyle=f"inverse-{c_style}")
        
        # Audio Trigger
        self.play_sound(status_type)

        if attendee:
            self.lbl_name.configure(text=attendee.get("full_name", "").upper())
            self.lbl_company.configure(text=attendee.get("business_name") or "General Admission", bootstyle="info" if c_style=="success" else c_style)
            self.lbl_attendee_id.configure(text=attendee.get("attendee_id", ""))
            
            mobile = str(attendee.get("mobile", ""))
            masked_mobile = f"••••••{mobile[-4:]}" if len(mobile) >= 4 else mobile
            
            self.fields["Mobile"].configure(text=masked_mobile)
            self.fields["Location"].configure(text=f"{attendee.get('city', '')}, {attendee.get('state', '')}")
            self.fields["Category"].configure(text=attendee.get("attendee_type", ""))
            self.fields["Gender"].configure(text=attendee.get("gender", ""))
            self.fields["Event Day"].configure(text=datetime.now().strftime("%d %B"))
            self.fields["Scanner"].configure(text=scanner_dev)
            
            self.load_photo(attendee.get("attendee_id"))
            self.add_recent_scan(attendee.get("full_name"), attendee.get("attendee_id"), c_style, time_str)
        else:
            self.lbl_name.configure(text="UNKNOWN")
            self.lbl_company.configure(text="---", bootstyle=SECONDARY)
            self.lbl_attendee_id.configure(text="---")
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
        card.pack(fill=X, pady=3, padx=2)
        
        top = ttk.Frame(card)
        top.pack(fill=X, padx=10, pady=(10, 0))
        ttk.Label(top, text=f"👤 {name}", font="-weight bold").pack(side=LEFT)
        ttk.Label(top, text=time_str, font="-size 8", foreground="#888").pack(side=RIGHT)
        
        bot = ttk.Frame(card)
        bot.pack(fill=X, padx=10, pady=(5, 10))
        ttk.Label(bot, text=att_id, font="-size 8", foreground="#888").pack(side=LEFT)
        ttk.Label(bot, text="✓ OK" if style=="success" else "⚠ Warn", font="-weight bold", bootstyle=style).pack(side=RIGHT)
        
        self.recent_scans.insert(0, card)
        if len(self.recent_scans) > 4:
            old = self.recent_scans.pop()
            old.destroy()

    def start_stream_thread(self):
        self.is_polling = True
        self.stream_thread = threading.Thread(target=self.listen_to_server, daemon=True)
        self.stream_thread.start()

    def listen_to_server(self):
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
                                    event_data = json.loads(decoded[6:])
                                    # Route events to pacing queue
                                    self.scan_queue.put(event_data)
                    else:
                        self.gui_queue.put(lambda: self.lbl_hub_status.configure(text=f"● Hub Error {response.status_code}", bootstyle=WARNING))
                        time.sleep(2)
                        
            except requests.exceptions.RequestException:
                self.gui_queue.put(lambda: self.lbl_hub_status.configure(text="● Hub Disconnected. Retrying...", bootstyle=DANGER))
                time.sleep(2)

    def manual_scan(self, lookup_type):
        val = self.ent_phone.get() if lookup_type == 'phone' else self.ent_id.get()
        if "e.g." in val: return
        
        url = f"{self.config_manager.config['hub_url']}/api/checkin"
        payload = {
            "attendee_id": val,
            "search_type": lookup_type,
            "device_name": self.config_manager.config["device_name"]
        }
        
        def _post():
            try:
                requests.post(url, json=payload, timeout=3, verify=False)
            except Exception:
                err_payload = {
                    "status": "ERROR", 
                    "message": "Network failure connecting to Hub.", 
                    "attendee": None,
                    "timestamp": datetime.now().isoformat(),
                    "device": self.config_manager.config["device_name"]
                }
                self.scan_queue.put(err_payload)

        threading.Thread(target=_post, daemon=True).start()
        
        # Clear entries
        self.gui_queue.put(lambda: [e.delete(0, END) or e.event_generate('<FocusOut>') for e in (self.ent_id, self.ent_phone)])

    def open_settings(self):
        SettingsDialog(self, self.config_manager, self.on_settings_saved)

    def on_settings_saved(self):
        self.lbl_subtitle.config(text=f"{self.config_manager.config['device_name']} • TDE UP 2026")
        self.is_polling = False
        time.sleep(0.2)
        self.start_stream_thread()

if __name__ == "__main__":
    app = GateDisplay()
    app.mainloop()