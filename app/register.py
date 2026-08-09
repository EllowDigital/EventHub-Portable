import os
import json
import logging
from logging.handlers import RotatingFileHandler
import time
import threading
import queue
import re
import uuid
import platform
import subprocess
import requests
import urllib3
import tkinter as tk
from tkinter import messagebox
import ttkbootstrap as ttk
from ttkbootstrap.constants import *

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

urllib3.disable_warnings(urllib3.exceptions.InsecureRequestWarning)

BASE_DIR = os.path.dirname(os.path.abspath(__file__))
LOG_DIR = os.path.join(BASE_DIR, 'logs')
CONFIG_DIR = os.path.join(BASE_DIR, 'config')
os.makedirs(LOG_DIR, exist_ok=True)
os.makedirs(CONFIG_DIR, exist_ok=True)

LOG_FILE = os.path.join(LOG_DIR, 'kiosk_registration.log')
_file_handler = RotatingFileHandler(LOG_FILE, maxBytes=5_000_000, backupCount=3, encoding='utf-8')
_file_handler.setFormatter(logging.Formatter('%(asctime)s [%(levelname)s] %(message)s'))
logging.basicConfig(level=logging.INFO, handlers=[_file_handler])

def global_exception_handler(*args):
    logging.error("Uncaught GUI Exception intercepted. App remains running.", exc_info=args)

tk.Tk.report_callback_exception = global_exception_handler

INDIAN_STATES = [
    "Andhra Pradesh", "Arunachal Pradesh", "Assam", "Bihar", "Chhattisgarh", "Goa", "Gujarat", 
    "Haryana", "Himachal Pradesh", "Jharkhand", "Karnataka", "Kerala", "Madhya Pradesh", 
    "Maharashtra", "Manipur", "Meghalaya", "Mizoram", "Nagaland", "Odisha", "Punjab", 
    "Rajasthan", "Sikkim", "Tamil Nadu", "Telangana", "Tripura", "Uttar Pradesh", "Uttarakhand", 
    "West Bengal", "Andaman and Nicobar Islands", "Chandigarh", "Dadra and Nagar Haveli and Daman and Diu", 
    "Delhi", "Jammu and Kashmir", "Ladakh", "Lakshadweep", "Puducherry"
]

POPULAR_CITIES = [
    "Mumbai", "Delhi", "Bengaluru", "Hyderabad", "Ahmedabad", "Chennai", "Kolkata", "Surat", 
    "Pune", "Jaipur", "Lucknow", "Kanpur", "Nagpur", "Indore", "Thane", "Bhopal", "Visakhapatnam", 
    "Pimpri-Chinchwad", "Patna", "Vadodara", "Ghaziabad", "Ludhiana", "Agra", "Nashik", "Faridabad", 
    "Meerut", "Rajkot", "Kalyan-Dombivli", "Vasai-Virar", "Varanasi", "Srinagar", "Aurangabad", 
    "Dhanbad", "Amritsar", "Navi Mumbai", "Allahabad", "Ranchi", "Howrah", "Coimbatore", "Jabalpur", 
    "Gwalior", "Vijayawada", "Jodhpur", "Madurai", "Raipur", "Kota", "Guwahati", "Chandigarh", 
    "Solapur", "Hubli-Dharwad", "Bareilly", "Mysore", "Tiruchirappalli", "Gurgaon", "Aligarh", 
    "Jalandhar", "Bhubaneswar", "Salem", "Noida", "Kochi", "Dehradun", "Durgapur", "Asansol", 
    "Rourkela", "Nanded", "Kolhapur", "Ajmer", "Akola", "Gulbarga", "Jamnagar", "Ujjain", "Loni", 
    "Siliguri", "Jhansi", "Ulhasnagar", "Jammu", "Sangli-Miraj & Kupwad", "Mangalore", "Erode", 
    "Belgaum", "Ambattur", "Tirunelveli", "Malegaon", "Gaya", "Jalgaon", "Udaipur", "Maheshtala"
]

CONFIG_FILE = os.path.join(CONFIG_DIR, 'register.json')
BACKUP_FILE = os.path.join(LOG_DIR, 'unsynced_registrations.json')

def load_config():
    if os.path.exists(CONFIG_FILE):
        try:
            with open(CONFIG_FILE, 'r') as f:
                return json.load(f)
        except Exception:
            pass
    return {"server_url": "http://127.0.0.1:5000", "device_name": "Main Desktop Kiosk"}

def save_config(url, name):
    with open(CONFIG_FILE, 'w') as f:
        json.dump({"server_url": url, "device_name": name}, f, indent=4)

class LocalBackupManager:
    def __init__(self):
        self.lock = threading.Lock()

    def save(self, payload):
        with self.lock:
            data = self.load()
            data.append(payload)
            with open(BACKUP_FILE, 'w') as f:
                json.dump(data, f, indent=4)

    def remove(self, backup_id):
        with self.lock:
            data = self.load()
            data = [d for d in data if d.get('_backup_id') != backup_id]
            with open(BACKUP_FILE, 'w') as f:
                json.dump(data, f, indent=4)

    def load(self):
        if not os.path.exists(BACKUP_FILE): return []
        try:
            with open(BACKUP_FILE, 'r') as f: return json.load(f)
        except Exception: return []

backup_mgr = LocalBackupManager()

class RobustScrollFrame(ttk.Frame):
    def __init__(self, parent, **kwargs):
        super().__init__(parent, **kwargs)
        
        bg_color = ttk.Style().colors.bg
        self.canvas = tk.Canvas(self, highlightthickness=0, bg=bg_color)
        self.v_scroll = ttk.Scrollbar(self, orient=VERTICAL, command=self.canvas.yview)
        self.container = ttk.Frame(self.canvas, padding=(20, 10, 20, 40)) 
        
        self.canvas_window = self.canvas.create_window((0, 0), window=self.container, anchor="nw")
        self.canvas.configure(yscrollcommand=self.v_scroll.set)
        
        self.canvas.pack(side=LEFT, fill=BOTH, expand=True)
        self.v_scroll.pack(side=RIGHT, fill=Y)
        
        self.container.bind("<Configure>", self.on_frame_configure)
        self.canvas.bind("<Configure>", self.on_canvas_configure)
        self.bind_all("<MouseWheel>", self.on_mousewheel)
        
    def on_frame_configure(self, event=None):
        self.canvas.configure(scrollregion=self.canvas.bbox("all"))
        
    def on_canvas_configure(self, event):
        self.canvas.itemconfig(self.canvas_window, width=event.width)
        
    def on_mousewheel(self, event):
        try: self.canvas.yview_scroll(int(-1*(event.delta/120)), "units")
        except Exception: pass
        
    def yview_moveto(self, fraction):
        self.canvas.yview_moveto(fraction)

class AutocompleteCombobox(ttk.Combobox):
    def __init__(self, parent, completion_list, **kwargs):
        self._completion_list = sorted(list(set(completion_list)))
        super().__init__(parent, values=self._completion_list, **kwargs)
        self.bind('<KeyRelease>', self.handle_keyrelease)
        
    def handle_keyrelease(self, event):
        if event.keysym in ('BackSpace', 'Left', 'Right', 'Up', 'Down', 'Return', 'Tab', 'Shift_L', 'Shift_R'):
            return
            
        typed_text = self.get().lower()
        if not typed_text:
            self.configure(values=self._completion_list)
            return

        matching_items = [item for item in self._completion_list if item.lower().startswith(typed_text)]
        self.configure(values=matching_items)

class OfflineKioskApp(ttk.Window):
    def __init__(self):
        super().__init__(themename="darkly", title="TDE UP 2026 — Desktop Registration Kiosk")
        self.geometry("900x800")
        self.minsize(700, 600)
        
        self.update_idletasks()
        sw, sh = self.winfo_screenwidth(), self.winfo_screenheight()
        self.geometry(f"+{(sw - 900) // 2}+{(sh - 800) // 2 - 20}")
        
        self.config = load_config()
        self.server_url = self.config["server_url"].rstrip('/')
        self.device_name = self.config["device_name"]
        self.sound_enabled = True
        
        self.api_session = requests.Session()
        self.ping_session = requests.Session()
        self.sync_session = requests.Session()
        
        for session in [self.api_session, self.ping_session, self.sync_session]:
            session.headers.update({"User-Agent": "EventHub-Kiosk/1.0", "Connection": "close"})
        
        self.gui_queue = queue.Queue()
        self.is_pinging = True
        self.is_submitting = False
        
        self.MOBILE_RE = re.compile(r"^[6-9]\d{9}$")
        self.PIN_RE = re.compile(r"^\d{6}$")
        self.EMAIL_RE = re.compile(r"^[\w.\-+]+@[\w.\-]+\.\w{2,}$")
        self._mobile_check_timer = None
        self._pincode_check_timer = None

        self.protocol("WM_DELETE_WINDOW", self.on_close)
        
        self.build_ui()
        self.setup_reactive_logic()
        self.bind_shortcuts()
        self.process_gui_queue()
        
        threading.Thread(target=self.network_ping_loop, daemon=True).start()
        threading.Thread(target=self.background_sync_loop, daemon=True).start()

    def bind_shortcuts(self):
        self.bind("<Control-s>", self.submit_form)
        self.bind("<Control-S>", self.submit_form)
        self.bind("<Control-Return>", self.submit_form)
        self.bind("<Alt-c>", lambda e: self.reset_form())
        self.bind("<Alt-C>", lambda e: self.reset_form())

    def on_close(self):
        self.is_pinging = False
        try:
            self.api_session.close()
            self.ping_session.close()
            self.sync_session.close()
        except Exception: pass
        self.destroy()

    def build_ui(self):
        header_frame = ttk.Frame(self, padding=(15, 15, 15, 5))
        header_frame.pack(fill=X)
        
        title_lbl = ttk.Label(header_frame, text="Kiosk Registration", font="-size 20 -weight bold", bootstyle=PRIMARY)
        title_lbl.pack(side=LEFT)

        control_frame = ttk.Frame(header_frame)
        control_frame.pack(side=RIGHT)

        self.btn_sound = ttk.Button(control_frame, text="🔊 Sound Enabled", bootstyle="outline-success", command=self.toggle_sound)
        self.btn_sound.pack(side=LEFT, padx=(0, 5))

        self.btn_settings = ttk.Button(control_frame, text="⚙️ Settings", bootstyle=SECONDARY, command=self.open_settings)
        self.btn_settings.pack(side=LEFT, padx=(0, 15))

        self.net_pill = ttk.Frame(control_frame, borderwidth=1, relief="solid", bootstyle="dark", padding=(10, 5))
        self.net_pill.pack(side=LEFT)
        
        self.net_canvas = tk.Canvas(self.net_pill, width=12, height=12, bg="#1e1e1e", highlightthickness=0)
        self.net_dot = self.net_canvas.create_oval(2, 2, 10, 10, fill="#757575", outline="")
        self.net_canvas.pack(side=LEFT, padx=(0, 5))
        
        self.net_label = ttk.Label(self.net_pill, text="Checking...", font="-size 9 -weight bold")
        self.net_label.pack(side=LEFT)

        ttk.Label(self, text="⌨️ Shortcuts: [Ctrl+S] Save  |  [Alt+C] Clear Form", font="-size 9", foreground="gray").pack(anchor=E, padx=15)

        self.scroll_frame = RobustScrollFrame(self)
        self.scroll_frame.pack(fill=BOTH, expand=True)
        container = self.scroll_frame.container

        self.vars = {}
        self.inputs = {}
        self.errors = {}

        id_card = ttk.Labelframe(container, text=" 👤 Identity Details ", padding=15)
        id_card.pack(fill=X, pady=(10, 10), padx=5)
        
        self.create_input(id_card, "full_name", "Full Name *")
        
        row1 = ttk.Frame(id_card)
        row1.pack(fill=X, pady=(5, 0))
        self.create_input(row1, "mobile", "Mobile Number *", is_half=True)
        self.create_dropdown(row1, "gender", "Gender *", ["", "MALE", "FEMALE", "OTHER"], is_half=True)
        
        self.create_input(id_card, "email", "Email Address (Optional)")

        prof_card = ttk.Labelframe(container, text=" 💼 Professional Details ", padding=15)
        prof_card.pack(fill=X, pady=(10, 10), padx=5)
        
        row2 = ttk.Frame(prof_card)
        row2.pack(fill=X, pady=(0, 5))
        self.create_dropdown(row2, "attendee_type", "Attendee Type *", ["GENERAL", "BUSINESS", "MEDIA", "EXHIBITOR"], is_half=True, default="GENERAL")
        self.create_input(row2, "business_name", "Company / Firm Name", is_half=True)

        row3 = ttk.Frame(prof_card)
        row3.pack(fill=X, pady=(0, 5))
        cat_opts = [
            "", "TENT", "CATERING", "DECORATOR", "FLOWER", "DJ", "LIGHT", 
            "PHOTOGRAPHY", "VIDEOGRAPHY", "EVENT_PLANNER", "STAGE", "BAND", 
            "MAKEUP", "BANQUET", "TRANSPORT", "OTHER", "MEDIA_PRESS"
        ]
        self.create_dropdown(row3, "business_category", "Category", cat_opts, is_half=True)
        self.create_input(row3, "other_category", "Specify Other", is_half=True, state=DISABLED)

        loc_card = ttk.Labelframe(container, text=" 📍 Location Details ", padding=15)
        loc_card.pack(fill=X, pady=(10, 10), padx=5)
        
        self.create_input(loc_card, "address", "Full Address *")
        
        row4 = ttk.Frame(loc_card)
        row4.pack(fill=X, pady=(5, 0))
        self.create_input(row4, "pincode", "Pincode *", width_ratio=0.33)
        self.create_autocomplete(row4, "city", "City *", POPULAR_CITIES, width_ratio=0.33)
        self.create_autocomplete(row4, "state", "State *", INDIAN_STATES, width_ratio=0.33)

        day_card = ttk.Labelframe(container, text=" 📅 Attendance Days * ", padding=15)
        day_card.pack(fill=X, pady=(10, 15), padx=5)
        
        days_frame = ttk.Frame(day_card)
        days_frame.pack(fill=X)
        
        self.vars['day_1'] = tk.BooleanVar()
        self.vars['day_2'] = tk.BooleanVar()
        self.vars['day_3'] = tk.BooleanVar()
        
        ttk.Checkbutton(days_frame, text="30 Aug", variable=self.vars['day_1'], bootstyle="info-square-toggle").pack(side=LEFT, padx=(0, 20))
        ttk.Checkbutton(days_frame, text="31 Aug", variable=self.vars['day_2'], bootstyle="info-square-toggle").pack(side=LEFT, padx=(0, 20))
        ttk.Checkbutton(days_frame, text="1 Sept", variable=self.vars['day_3'], bootstyle="info-square-toggle").pack(side=LEFT)
        
        self.errors['days'] = ttk.Label(day_card, text="", foreground="#ff4444", font="-size 8")
        self.errors['days'].pack(anchor=W, pady=(5, 0))

        action_frame = ttk.Frame(container)
        action_frame.pack(fill=X, pady=(10, 5), padx=5)

        self.vars['auto_clear'] = tk.BooleanVar(value=True)
        chk_auto_clear = ttk.Checkbutton(action_frame, text=" Auto-clear form on success", variable=self.vars['auto_clear'], bootstyle="round-toggle")
        chk_auto_clear.pack(side=LEFT)

        btn_clear = ttk.Button(action_frame, text="🗑️ Clear Form (Alt+C)", bootstyle="outline-secondary", command=self.reset_form)
        btn_clear.pack(side=RIGHT)

        self.btn_submit = ttk.Button(container, text="Register Attendee (Ctrl+S)", bootstyle=SUCCESS, padding=12, command=self.submit_form)
        self.btn_submit.pack(fill=X, pady=(5, 20), padx=5)
        
        self.inputs['full_name'].focus_set()

    def create_input(self, parent, name, label_text, is_half=False, width_ratio=1.0, state=NORMAL):
        frame = ttk.Frame(parent)
        if is_half: frame.pack(side=LEFT, fill=X, expand=True, padx=5)
        elif width_ratio < 1.0: frame.pack(side=LEFT, fill=X, expand=True, padx=5)
        else: frame.pack(fill=X, pady=(0, 5), padx=5)

        ttk.Label(frame, text=label_text, font="-size 9 -weight bold", foreground="#D4D4D4").pack(anchor=W, pady=(0, 2))
        self.vars[name] = tk.StringVar()
        entry = ttk.Entry(frame, textvariable=self.vars[name], state=state, font="-size 11")
        entry.pack(fill=X, ipady=4)
        self.inputs[name] = entry
        
        err_lbl = ttk.Label(frame, text="", foreground="#ff4444", font="-size 8")
        err_lbl.pack(anchor=W)
        self.errors[name] = err_lbl

    def create_dropdown(self, parent, name, label_text, options, is_half=False, default=""):
        frame = ttk.Frame(parent)
        if is_half: frame.pack(side=LEFT, fill=X, expand=True, padx=5)
        else: frame.pack(fill=X, pady=(0, 5), padx=5)

        ttk.Label(frame, text=label_text, font="-size 9 -weight bold", foreground="#D4D4D4").pack(anchor=W, pady=(0, 2))
        self.vars[name] = tk.StringVar(value=default)
        cb = ttk.Combobox(frame, textvariable=self.vars[name], values=options, state="readonly", font="-size 11")
        cb.pack(fill=X, ipady=4)
        self.inputs[name] = cb
        
        err_lbl = ttk.Label(frame, text="", foreground="#ff4444", font="-size 8")
        err_lbl.pack(anchor=W)
        self.errors[name] = err_lbl

    def create_autocomplete(self, parent, name, label_text, options, is_half=False, width_ratio=1.0):
        frame = ttk.Frame(parent)
        if is_half: frame.pack(side=LEFT, fill=X, expand=True, padx=5)
        elif width_ratio < 1.0: frame.pack(side=LEFT, fill=X, expand=True, padx=5)
        else: frame.pack(fill=X, pady=(0, 5), padx=5)

        ttk.Label(frame, text=label_text, font="-size 9 -weight bold", foreground="#D4D4D4").pack(anchor=W, pady=(0, 2))
        self.vars[name] = tk.StringVar()
        
        cb = AutocompleteCombobox(frame, completion_list=options, textvariable=self.vars[name], font="-size 11")
        cb.pack(fill=X, ipady=4)
        self.inputs[name] = cb
        
        err_lbl = ttk.Label(frame, text="", foreground="#ff4444", font="-size 8")
        err_lbl.pack(anchor=W)
        self.errors[name] = err_lbl

    def toggle_sound(self):
        self.sound_enabled = not self.sound_enabled
        if self.sound_enabled:
            self.btn_sound.configure(text="🔊 Voice Enabled", bootstyle="outline-success")
            self.play_sound("SUCCESS", "Audio alerts enabled.")
        else:
            self.btn_sound.configure(text="🔇 Muted", bootstyle="outline-secondary")

    def play_sound(self, status, speak_text=""):
        if not self.sound_enabled:
            return
            
        def _play():
            if HAS_WINSOUND:
                try:
                    if status == "SUCCESS":
                        winsound.Beep(2000, 100)  
                    elif status == "DUPLICATE":
                        winsound.Beep(1000, 100) 
                        time.sleep(0.05)
                        winsound.Beep(1000, 100)
                    else:
                        winsound.Beep(200, 600)
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

    def setup_reactive_logic(self):
        self.vars['attendee_type'].trace_add('write', self.on_type_change)
        self.vars['business_category'].trace_add('write', self.on_category_change)
        self.vars['mobile'].trace_add('write', self.on_mobile_change)
        self.vars['pincode'].trace_add('write', self.on_pincode_change)

        for field, var in self.vars.items():
            if field not in ['auto_clear', 'day_1', 'day_2', 'day_3']:
                var.trace_add('write', lambda n, i, m, f=field: self.clear_single_error(f))

        for day in ['day_1', 'day_2', 'day_3']:
            self.vars[day].trace_add('write', lambda n, i, m: self.clear_single_error('days'))

    def clear_single_error(self, field):
        if field in self.inputs:
            self.inputs[field].configure(bootstyle=DEFAULT)
        if field in self.errors:
            self.errors[field].configure(text="")

    def on_type_change(self, *args):
        att_type = self.vars['attendee_type'].get()
        if att_type == 'MEDIA':
            self.vars['business_category'].set('MEDIA_PRESS')
            self.inputs['business_category'].configure(state=DISABLED)
            self.vars['other_category'].set('')
            self.inputs['other_category'].configure(state=DISABLED)
        else:
            self.inputs['business_category'].configure(state="readonly")
            if self.vars['business_category'].get() == 'MEDIA_PRESS':
                self.vars['business_category'].set('')
        self.clear_single_error('business_category')
        self.clear_single_error('business_name')

    def on_category_change(self, *args):
        if self.vars['business_category'].get() == 'OTHER':
            self.inputs['other_category'].configure(state=NORMAL)
        else:
            self.vars['other_category'].set('')
            self.inputs['other_category'].configure(state=DISABLED)
        self.clear_single_error('other_category')

    def on_mobile_change(self, *args):
        val = self.vars['mobile'].get()
        clean_val = re.sub(r'\D', '', val)[:10]
        
        if val != clean_val:
            self.vars['mobile'].set(clean_val)
            
        if self._mobile_check_timer:
            self.after_cancel(self._mobile_check_timer)
            
        if len(clean_val) == 10:
            self.errors['mobile'].configure(text="⏳ Checking number...", foreground="#00d2ff")
            self._mobile_check_timer = self.after(400, lambda: threading.Thread(target=self._check_mobile_status, args=(clean_val,), daemon=True).start())
        else:
            self.clear_single_error('mobile')

    def _check_mobile_status(self, mobile_num):
        try:
            res = self.api_session.get(f"{self.server_url}/api/check_mobile", params={"mobile": mobile_num}, timeout=3, verify=False)
            if res.status_code == 200:
                data = res.json()
                if data.get('status') in ['already_registered', 'registered', 'exists']:
                    aid = data.get('attendee_id', 'UNKNOWN ID')
                    self.gui_queue.put(lambda: self.errors['mobile'].configure(text=f"⚠ Already Registered! ID: {aid}", foreground="#ffbb33"))
                    self.gui_queue.put(lambda: self.inputs['mobile'].configure(bootstyle=WARNING))
                else:
                    self.gui_queue.put(lambda: self.errors['mobile'].configure(text="✓ Ready", foreground="#00e676"))
            elif res.status_code == 404:
                self.gui_queue.put(lambda: self.errors['mobile'].configure(text="⚠ Backend missing route", foreground="#ff4444"))
        except Exception:
            self.gui_queue.put(lambda: self.errors['mobile'].configure(text="⚠ Server offline", foreground="#ff4444"))
            
    def on_pincode_change(self, *args):
        val = self.vars['pincode'].get()
        clean_val = re.sub(r'\D', '', val)[:6]
        
        if val != clean_val:
            self.vars['pincode'].set(clean_val)
            
        if self._pincode_check_timer:
            self.after_cancel(self._pincode_check_timer)
            
        if len(clean_val) == 6:
            self._pincode_check_timer = self.after(400, lambda: threading.Thread(target=self._check_pincode, args=(clean_val,), daemon=True).start())
        else:
            self.clear_single_error('pincode')

    def _check_pincode(self, pincode):
        try:
            res = self.api_session.get(f"{self.server_url}/api/pincode/{pincode}", timeout=3, verify=False)
            if res.status_code == 200:
                data = res.json()
                if data.get('status') == 'success':
                    state = data.get('state', '')
                    district = data.get('district', '')
                    self.gui_queue.put(lambda s=state, d=district: self._apply_pincode_data(s, d))
        except Exception as e:
            logging.error(f"Pincode API lookup failed: {e}")

    def _apply_pincode_data(self, state, district):
        if state: self.vars['state'].set(state.title())
        if district: self.vars['city'].set(district.title())
        self.clear_single_error('pincode')

    def open_settings(self):
        modal = tk.Toplevel(self)
        modal.title("Kiosk Configuration")
        modal.geometry("400x300")
        modal.resizable(False, False)
        modal.transient(self)
        modal.grab_set()

        ttk.Label(modal, text="Hub Connection URL:", font="-weight bold").pack(anchor=W, padx=20, pady=(20, 5))
        url_var = tk.StringVar(value=self.server_url)
        ttk.Entry(modal, textvariable=url_var).pack(fill=X, padx=20, ipady=4)
        ttk.Label(modal, text="Example: http://192.168.137.1:5000", font="-size 8", foreground="gray").pack(anchor=W, padx=20)

        ttk.Label(modal, text="Kiosk Device Name:", font="-weight bold").pack(anchor=W, padx=20, pady=(20, 5))
        name_var = tk.StringVar(value=self.device_name)
        ttk.Entry(modal, textvariable=name_var).pack(fill=X, padx=20, ipady=4)

        def save_and_close(event=None):
            self.server_url = url_var.get().rstrip('/')
            self.device_name = name_var.get()
            save_config(self.server_url, self.device_name)
            modal.destroy()
            self.gui_queue.put(lambda: self.net_label.configure(text="Reconnecting..."))

        btn_save = ttk.Button(modal, text="Save Configuration", bootstyle=SUCCESS, command=save_and_close)
        btn_save.pack(fill=X, padx=20, pady=30, ipady=4)
        
        modal.bind('<Return>', save_and_close)
        modal.bind('<Escape>', lambda e: modal.destroy())

    def network_ping_loop(self):
        while self.is_pinging:
            start_time = time.time()
            try:
                url = f"{self.server_url}/api/status?device_name={requests.utils.quote(self.device_name)}"
                res = self.ping_session.get(url, timeout=2, verify=False)
                res.raise_for_status()
                
                duration_ms = int((time.time() - start_time) * 1000)
                if duration_ms < 150:
                    self.gui_queue.put(lambda ms=duration_ms: self.update_net_pill(f"Excellent • {ms}ms", "#00e676"))
                elif duration_ms < 500:
                    self.gui_queue.put(lambda ms=duration_ms: self.update_net_pill(f"Fair • {ms}ms", "#ffbb33"))
                else:
                    self.gui_queue.put(lambda ms=duration_ms: self.update_net_pill(f"Poor • {ms}ms", "#ff4444"))
            except Exception:
                self.gui_queue.put(lambda: self.update_net_pill("Offline", "#757575"))
            
            time.sleep(3)

    def background_sync_loop(self):
        while self.is_pinging:
            backups = backup_mgr.load()
            if backups:
                for b in list(backups):
                    if not self.is_pinging: break
                    try:
                        res = self.sync_session.post(f"{self.server_url}/api/register", json=b, timeout=5, verify=False)
                        if res.status_code == 200:
                            backup_mgr.remove(b['_backup_id'])
                    except Exception:
                        break 
            time.sleep(10)

    def update_net_pill(self, text, color):
        self.net_label.configure(text=text)
        self.net_canvas.itemconfig(self.net_dot, fill=color)
        self.net_canvas.coords(self.net_dot, 1, 1, 11, 11)
        self.after(200, lambda: self.net_canvas.coords(self.net_dot, 2, 2, 10, 10))

    def process_gui_queue(self):
        if not self.winfo_exists(): return
        start_time = time.perf_counter()
        while time.perf_counter() - start_time < 0.015:
            try:
                task = self.gui_queue.get_nowait()
            except queue.Empty:
                break
            try:
                task()
            except Exception as e:
                logging.error(f"gui_queue task execution error: {e}")
        self.after(25, self.process_gui_queue)

    def animate_submit_button(self, count=0):
        if self.is_submitting and self.winfo_exists():
            dots = "." * ((count % 3) + 1)
            self.btn_submit.configure(text=f"⏳ Registering{dots}")
            self.after(400, lambda: self.animate_submit_button(count + 1))

    def set_error(self, field, msg):
        self.inputs[field].configure(bootstyle=DANGER)
        self.errors[field].configure(text=f"⚠ {msg}", foreground="#ff4444")

    def clear_all_errors(self):
        for field, entry in self.inputs.items():
            entry.configure(bootstyle=DEFAULT)
        for err_lbl in self.errors.values():
            err_lbl.configure(text="")

    def validate_form(self):
        self.clear_all_errors()
        ok = True

        if len(self.vars['full_name'].get().strip()) < 2:
            self.set_error('full_name', "Required (min 2 chars)")
            ok = False

        if not self.MOBILE_RE.match(self.vars['mobile'].get().strip()):
            self.set_error('mobile', "Valid 10-digit number required")
            ok = False

        email = self.vars['email'].get().strip()
        if email and not self.EMAIL_RE.match(email):
            self.set_error('email', "Invalid email")
            ok = False

        if not self.vars['gender'].get():
            self.set_error('gender', "Required")
            ok = False

        att_type = self.vars['attendee_type'].get()
        biz_name = self.vars['business_name'].get().strip()
        if att_type in ['BUSINESS', 'EXHIBITOR', 'MEDIA'] and not biz_name:
            self.set_error('business_name', "Required for this type")
            ok = False

        cat = self.vars['business_category'].get()
        other = self.vars['other_category'].get().strip()
        if att_type in ['BUSINESS', 'EXHIBITOR']:
            if not cat:
                self.set_error('business_category', "Required")
                ok = False
            elif cat == 'OTHER' and not other:
                self.set_error('other_category', "Specify category")
                ok = False

        if len(self.vars['address'].get().strip()) < 5:
            self.set_error('address', "Required (min 5 chars)")
            ok = False
        if len(self.vars['city'].get().strip()) < 2:
            self.set_error('city', "Required")
            ok = False
        if len(self.vars['state'].get().strip()) < 2:
            self.set_error('state', "Required")
            ok = False
        if not self.PIN_RE.match(self.vars['pincode'].get().strip()):
            self.set_error('pincode', "6-digit pincode required")
            ok = False

        d1, d2, d3 = self.vars['day_1'].get(), self.vars['day_2'].get(), self.vars['day_3'].get()
        if not (d1 or d2 or d3):
            self.errors['days'].configure(text="⚠ Select at least one day", foreground="#ff4444")
            ok = False

        return ok

    def submit_form(self, event=None):
        if self.is_submitting: return
        if not self.validate_form():
            self.play_sound("ERROR", "Please check the form for errors.")
            return
            
        self.is_submitting = True
        self.btn_submit.configure(state=DISABLED, bootstyle=WARNING)
        self.animate_submit_button()
        
        selected_days = []
        if self.vars['day_1'].get(): selected_days.append("30 August")
        if self.vars['day_2'].get(): selected_days.append("31 August")
        if self.vars['day_3'].get(): selected_days.append("1 September")

        payload = {
            "_backup_id": str(uuid.uuid4()),
            "full_name": self.vars['full_name'].get().strip(),
            "mobile": self.vars['mobile'].get().strip(),
            "email": self.vars['email'].get().strip() or None,
            "gender": self.vars['gender'].get(),
            "attendee_type": self.vars['attendee_type'].get(),
            "business_name": self.vars['business_name'].get().strip() or None,
            "business_category": self.vars['business_category'].get() or None,
            "other_category": self.vars['other_category'].get().strip() or None,
            "address": self.vars['address'].get().strip(),
            "city": self.vars['city'].get().strip(),
            "state": self.vars['state'].get().strip(),
            "pincode": self.vars['pincode'].get().strip(),
            "attendance_days": selected_days,
            "device_name": self.device_name
        }

        backup_mgr.save(payload)
        threading.Thread(target=self._post_registration_infinite_loop, args=(payload,), daemon=True).start()

    def _post_registration_infinite_loop(self, payload):
        attempt = 1
        while self.is_submitting and self.is_pinging:
            try:
                res = self.api_session.post(f"{self.server_url}/api/register", json=payload, timeout=5, verify=False)
                res.raise_for_status()
                data = res.json()
                
                backup_mgr.remove(payload['_backup_id'])
                self.is_submitting = False
                
                if data.get('status') == 'success':
                    self.gui_queue.put(lambda: self.show_success_modal(data.get('attendee_id'), is_duplicate=False))
                elif data.get('status') in ['already_registered', 'registered', 'exists']:
                    self.gui_queue.put(lambda: self.show_success_modal(data.get('attendee_id'), is_duplicate=True))
                else:
                    self.gui_queue.put(lambda: self.handle_submit_error(f"Server Error: {data.get('message', 'Unknown Error')}"))
                break

            except requests.exceptions.RequestException:
                self.gui_queue.put(lambda a=attempt: self.btn_submit.configure(
                    text=f"⏳ Connection Lost... Retrying ({a})", bootstyle=DANGER
                ))
                time.sleep(3)
                attempt += 1

    def handle_submit_error(self, message):
        self.is_submitting = False
        self.play_sound("ERROR", "Warning. Registration failed.")
        messagebox.showerror("Registration Failed", message)
        self.btn_submit.configure(state=NORMAL, text="Register Attendee (Ctrl+S)", bootstyle=SUCCESS)

    def show_success_modal(self, aid, is_duplicate=False):
        if is_duplicate:
            self.play_sound("DUPLICATE", "Warning. Attendee already registered.")
        else:
            self.play_sound("SUCCESS", "Registration saved successfully.")
            
        modal = tk.Toplevel(self)
        modal.geometry("450x350")
        modal.resizable(False, False)
        modal.overrideredirect(True) 
        
        x = self.winfo_x() + (self.winfo_width() // 2) - 225
        y = self.winfo_y() + (self.winfo_height() // 2) - 175 
        modal.geometry(f"+{x}+{y}")

        frame = ttk.Frame(modal, borderwidth=2, relief="solid")
        frame.pack(fill=BOTH, expand=True)

        if is_duplicate:
            ttk.Label(frame, text="Already Registered!", font="-size 22 -weight bold", foreground="#ffbb33").pack(pady=(30, 10))
            ttk.Label(frame, text="This mobile number is already in the system.\nExisting ID:", justify=CENTER, font="-size 12").pack()
            id_color = "#ffbb33"
        else:
            ttk.Label(frame, text="Registration Saved!", font="-size 22 -weight bold", foreground="#00e676").pack(pady=(30, 10))
            ttk.Label(frame, text="Please provide the attendee with their pass code:", justify=CENTER, font="-size 12").pack()
            id_color = "#00e676"

        ttk.Label(frame, text=aid, font=("Consolas", 32, "bold"), background="#1E1E1E", foreground=id_color, padding=15).pack(pady=25)

        countdown_lbl = ttk.Label(frame, text="Returning to form in 8s... (Press Enter)", foreground="#D4D4D4")
        countdown_lbl.pack()

        def close_modal(event=None):
            if self.vars['auto_clear'].get():
                self.reset_form()
            else:
                self.btn_submit.configure(state=NORMAL, text="Register Attendee (Ctrl+S)", bootstyle=SUCCESS)
                self.inputs['full_name'].focus_set()
            modal.destroy()

        modal.bind('<Return>', close_modal)
        modal.bind('<Escape>', close_modal)
        modal.focus_force() 

        def update_countdown(count):
            if count > 0 and modal.winfo_exists():
                countdown_lbl.configure(text=f"Returning to form in {count}s... (Press Enter)")
                modal.after(1000, update_countdown, count - 1)
            elif modal.winfo_exists():
                close_modal()

        ttk.Button(frame, text="Next Registration (Enter)", bootstyle=SECONDARY, command=close_modal).pack(pady=(15, 20), ipady=4)
        update_countdown(8)

    def reset_form(self):
        for name, var in self.vars.items():
            if name == 'auto_clear': continue 
            elif name == 'attendee_type': var.set("GENERAL")
            elif name in ['day_1', 'day_2', 'day_3']: var.set(False)
            else: var.set("")
        
        self.inputs['business_category'].configure(state="readonly")
        self.inputs['other_category'].configure(state=DISABLED)
        self.clear_all_errors()
        self.is_submitting = False
        self.btn_submit.configure(state=NORMAL, text="Register Attendee (Ctrl+S)", bootstyle=SUCCESS)
        
        self.scroll_frame.yview_moveto(0)
        self.inputs['full_name'].focus_set()

if __name__ == "__main__":
    app = OfflineKioskApp()
    app.mainloop()