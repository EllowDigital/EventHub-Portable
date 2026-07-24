import os
import json
import time
import threading
import queue
import re
import requests
import urllib3
import tkinter as tk
from tkinter import messagebox
import ttkbootstrap as ttk
from ttkbootstrap.constants import *

# Suppress InsecureRequestWarning for adhoc self-signed HTTPS certificates
urllib3.disable_warnings(urllib3.exceptions.InsecureRequestWarning)

# ==============================================================================
# CONFIGURATION MANAGER
# ==============================================================================
BASE_DIR = os.path.dirname(os.path.abspath(__file__))
CONFIG_DIR = os.path.join(BASE_DIR, 'config')
CONFIG_FILE = os.path.join(CONFIG_DIR, 'register.json')

def load_config():
    if not os.path.exists(CONFIG_DIR):
        os.makedirs(CONFIG_DIR, exist_ok=True)
        
    if os.path.exists(CONFIG_FILE):
        try:
            with open(CONFIG_FILE, 'r') as f:
                return json.load(f)
        except Exception:
            pass
    return {"server_url": "https://127.0.0.1:5000", "device_name": "Main Desktop Kiosk"}

def save_config(url, name):
    if not os.path.exists(CONFIG_DIR):
        os.makedirs(CONFIG_DIR, exist_ok=True)
    with open(CONFIG_FILE, 'w') as f:
        json.dump({"server_url": url, "device_name": name}, f, indent=4)


# ==============================================================================
# ROBUST SCROLLABLE FRAME
# ==============================================================================
class RobustScrollFrame(ttk.Frame):
    """A foolproof scrollable frame that guarantees inner content is always reachable."""
    def __init__(self, parent, **kwargs):
        super().__init__(parent, **kwargs)
        
        # Match the 'darkly' theme background color perfectly
        bg_color = ttk.Style().colors.bg
        
        self.canvas = tk.Canvas(self, highlightthickness=0, bg=bg_color)
        self.v_scroll = ttk.Scrollbar(self, orient=VERTICAL, command=self.canvas.yview)
        
        # Inner container for the actual form fields
        self.container = ttk.Frame(self.canvas, padding=(20, 10, 20, 40)) 
        
        self.canvas_window = self.canvas.create_window((0, 0), window=self.container, anchor="nw")
        self.canvas.configure(yscrollcommand=self.v_scroll.set)
        
        self.canvas.pack(side=LEFT, fill=BOTH, expand=True)
        self.v_scroll.pack(side=RIGHT, fill=Y)
        
        # Dynamic Bindings to recalculate sizes immediately
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


# ==============================================================================
# MAIN KIOSK APPLICATION
# ==============================================================================
class OfflineKioskApp(ttk.Window):
    def __init__(self):
        super().__init__(themename="darkly", title="TDE UP 2026 — Desktop Registration Kiosk")
        self.geometry("850x700")
        self.minsize(600, 500)
        
        self.config = load_config()
        self.server_url = self.config["server_url"].rstrip('/')
        self.device_name = self.config["device_name"]
        
        self.gui_queue = queue.Queue()
        self.is_pinging = True
        
        # Validation Regex
        self.MOBILE_RE = re.compile(r"^[6-9]\d{9}$")
        self.PIN_RE = re.compile(r"^\d{6}$")
        self.EMAIL_RE = re.compile(r"^[\w.\-+]+@[\w.\-]+\.\w{2,}$")

        self.build_ui()
        self.setup_reactive_logic()
        
        # Start background tasks
        self.process_gui_queue()
        self.ping_thread = threading.Thread(target=self.network_ping_loop, daemon=True)
        self.ping_thread.start()

    # --- UI BUILDING ---
    def build_ui(self):
        # 1. Top Header Bar
        header_frame = ttk.Frame(self, padding=(15, 15, 15, 5))
        header_frame.pack(fill=X)
        
        title_lbl = ttk.Label(header_frame, text="Kiosk Registration", font="-size 18 -weight bold")
        title_lbl.pack(side=LEFT)

        # Network & Settings Controls
        control_frame = ttk.Frame(header_frame)
        control_frame.pack(side=RIGHT)

        self.btn_settings = ttk.Button(control_frame, text="⚙️ Settings", bootstyle=SECONDARY, command=self.open_settings)
        self.btn_settings.pack(side=LEFT, padx=(0, 15))

        self.net_pill = ttk.Frame(control_frame, borderwidth=1, relief="solid", bootstyle="dark", padding=(10, 5))
        self.net_pill.pack(side=LEFT)
        
        self.net_canvas = tk.Canvas(self.net_pill, width=12, height=12, bg="#1e1e1e", highlightthickness=0)
        self.net_dot = self.net_canvas.create_oval(2, 2, 10, 10, fill="#757575", outline="")
        self.net_canvas.pack(side=LEFT, padx=(0, 5))
        
        self.net_label = ttk.Label(self.net_pill, text="Checking...", font="-size 9 -weight bold")
        self.net_label.pack(side=LEFT)

        # 2. Main Scrollable Form Area 
        self.scroll_frame = RobustScrollFrame(self)
        self.scroll_frame.pack(fill=BOTH, expand=True)
        container = self.scroll_frame.container

        # Data Storage dictionaries
        self.vars = {}
        self.inputs = {}
        self.errors = {}

        # --- SECTION: IDENTITY ---
        self.create_section_title(container, "👤 Identity")
        self.create_input(container, "full_name", "Full Name *")
        
        row1 = ttk.Frame(container)
        row1.pack(fill=X, pady=(0, 5))
        self.create_input(row1, "mobile", "Mobile Number *", is_half=True)
        self.create_dropdown(row1, "gender", "Gender *", ["", "MALE", "FEMALE", "OTHER"], is_half=True)
        
        self.create_input(container, "email", "Email (Optional)")

        # --- SECTION: PROFESSIONAL ---
        self.create_section_title(container, "💼 Professional Details")
        
        row2 = ttk.Frame(container)
        row2.pack(fill=X, pady=(0, 5))
        self.create_dropdown(row2, "attendee_type", "Attendee Type *", ["GENERAL", "BUSINESS", "MEDIA", "EXHIBITOR"], is_half=True, default="GENERAL")
        self.create_input(row2, "business_name", "Company / Firm Name", is_half=True)

        row3 = ttk.Frame(container)
        row3.pack(fill=X, pady=(0, 5))
        cat_opts = [
            "", "TENT", "CATERING", "DECORATOR", "FLOWER", "DJ", "LIGHT", 
            "PHOTOGRAPHY", "VIDEOGRAPHY", "EVENT_PLANNER", "STAGE", "BAND", 
            "MAKEUP", "BANQUET", "TRANSPORT", "OTHER", "MEDIA_PRESS"
        ]
        self.create_dropdown(row3, "business_category", "Category", cat_opts, is_half=True)
        self.create_input(row3, "other_category", "Specify Other", is_half=True, state=DISABLED)

        # --- SECTION: LOCATION ---
        self.create_section_title(container, "📍 Location")
        self.create_input(container, "address", "Full Address *")
        
        row4 = ttk.Frame(container)
        row4.pack(fill=X, pady=(0, 5))
        self.create_input(row4, "city", "City *", width_ratio=0.33)
        self.create_input(row4, "state", "State *", width_ratio=0.33)
        self.create_input(row4, "pincode", "Pincode *", width_ratio=0.33)

        # --- SECTION: ATTENDANCE DAYS ---
        self.create_section_title(container, "📅 Attendance Days *")
        days_frame = ttk.Frame(container)
        days_frame.pack(fill=X, pady=(0, 5))
        
        self.vars['day_1'] = tk.BooleanVar()
        self.vars['day_2'] = tk.BooleanVar()
        self.vars['day_3'] = tk.BooleanVar()
        
        ttk.Checkbutton(days_frame, text="30 Aug", variable=self.vars['day_1'], bootstyle="info-square-toggle").pack(side=LEFT, padx=(0, 15))
        ttk.Checkbutton(days_frame, text="31 Aug", variable=self.vars['day_2'], bootstyle="info-square-toggle").pack(side=LEFT, padx=(0, 15))
        ttk.Checkbutton(days_frame, text="1 Sept", variable=self.vars['day_3'], bootstyle="info-square-toggle").pack(side=LEFT)
        
        self.errors['days'] = ttk.Label(container, text="", foreground="#ff4444", font="-size 8")
        self.errors['days'].pack(anchor=W, pady=(0, 15))

        # --- SUBMIT BUTTON ---
        self.btn_submit = ttk.Button(container, text="Register Offline Attendee (Enter)", bootstyle=INFO, padding=12, command=self.submit_form)
        self.btn_submit.pack(fill=X, pady=(15, 20))
        
        # Shortcut: Allow pressing 'Enter' anywhere in the app to submit
        self.bind('<Return>', self.submit_form)
        
        # Start focus on the first box
        self.inputs['full_name'].focus_set()

    # --- UI HELPERS ---
    def create_section_title(self, parent, text):
        lbl = ttk.Label(parent, text=text, font="-size 11 -weight bold", foreground="#00d2ff")
        lbl.pack(anchor=W, pady=(15, 5))
        ttk.Separator(parent).pack(fill=X, pady=(0, 10))

    def create_input(self, parent, name, label_text, is_half=False, width_ratio=1.0, state=NORMAL):
        frame = ttk.Frame(parent)
        if is_half: frame.pack(side=LEFT, fill=X, expand=True, padx=5)
        elif width_ratio < 1.0: frame.pack(side=LEFT, fill=X, expand=True, padx=5)
        else: frame.pack(fill=X, pady=(0, 5), padx=5)

        ttk.Label(frame, text=label_text, font="-size 9 -weight bold", foreground="#D4D4D4").pack(anchor=W, pady=(0, 2))
        self.vars[name] = tk.StringVar()
        entry = ttk.Entry(frame, textvariable=self.vars[name], state=state, font="-size 10")
        entry.pack(fill=X)
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
        cb = ttk.Combobox(frame, textvariable=self.vars[name], values=options, state="readonly", font="-size 10")
        cb.pack(fill=X)
        self.inputs[name] = cb
        
        err_lbl = ttk.Label(frame, text="", foreground="#ff4444", font="-size 8")
        err_lbl.pack(anchor=W)
        self.errors[name] = err_lbl

    # --- REACTIVE LOGIC ---
    def setup_reactive_logic(self):
        self.vars['attendee_type'].trace_add('write', self.on_type_change)
        self.vars['business_category'].trace_add('write', self.on_category_change)
        self.vars['mobile'].trace_add('write', lambda *args: self.filter_digits('mobile', 10))
        self.vars['pincode'].trace_add('write', lambda *args: self.filter_digits('pincode', 6))

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
        self.errors['business_category'].configure(text="")
        self.errors['business_name'].configure(text="")

    def on_category_change(self, *args):
        if self.vars['business_category'].get() == 'OTHER':
            self.inputs['other_category'].configure(state=NORMAL)
        else:
            self.vars['other_category'].set('')
            self.inputs['other_category'].configure(state=DISABLED)
        self.errors['other_category'].configure(text="")

    def filter_digits(self, field, max_len):
        val = self.vars[field].get()
        clean_val = re.sub(r'\D', '', val)[:max_len]
        if val != clean_val:
            self.vars[field].set(clean_val)

    # --- SETTINGS MODAL ---
    def open_settings(self):
        modal = tk.Toplevel(self)
        modal.title("Kiosk Configuration")
        modal.geometry("400x300")
        modal.resizable(False, False)
        modal.transient(self)
        modal.grab_set()

        ttk.Label(modal, text="Hub Connection URL:", font="-weight bold").pack(anchor=W, padx=20, pady=(20, 5))
        url_var = tk.StringVar(value=self.server_url)
        ttk.Entry(modal, textvariable=url_var).pack(fill=X, padx=20)
        ttk.Label(modal, text="Example: https://192.168.137.1:5000", font="-size 8", foreground="gray").pack(anchor=W, padx=20)

        ttk.Label(modal, text="Kiosk Device Name:", font="-weight bold").pack(anchor=W, padx=20, pady=(20, 5))
        name_var = tk.StringVar(value=self.device_name)
        ttk.Entry(modal, textvariable=name_var).pack(fill=X, padx=20)

        def save_and_close(event=None):
            self.server_url = url_var.get().rstrip('/')
            self.device_name = name_var.get()
            save_config(self.server_url, self.device_name)
            modal.destroy()
            self.gui_queue.put(lambda: self.net_label.configure(text="Reconnecting..."))

        btn_save = ttk.Button(modal, text="Save Configuration", bootstyle=SUCCESS, command=save_and_close)
        btn_save.pack(fill=X, padx=20, pady=30)
        
        modal.bind('<Return>', save_and_close)
        modal.bind('<Escape>', lambda e: modal.destroy())

    # --- BACKGROUND NETWORK PING ---
    def network_ping_loop(self):
        while self.is_pinging:
            start_time = time.time()
            try:
                url = f"{self.server_url}/api/status?device_name={requests.utils.quote(self.device_name)}"
                res = requests.get(url, timeout=2, verify=False)
                res.raise_for_status()
                
                duration_ms = (time.time() - start_time) * 1000
                if duration_ms < 150:
                    self.gui_queue.put(lambda: self.update_net_pill("Excellent", "#00e676"))
                elif duration_ms < 500:
                    self.gui_queue.put(lambda: self.update_net_pill("Fair", "#ffbb33"))
                else:
                    self.gui_queue.put(lambda: self.update_net_pill("Poor", "#ff4444"))
            except Exception:
                self.gui_queue.put(lambda: self.update_net_pill("Offline", "#757575"))
            
            time.sleep(3)

    def update_net_pill(self, text, color):
        self.net_label.configure(text=text)
        self.net_canvas.itemconfig(self.net_dot, fill=color)

    def process_gui_queue(self):
        while not self.gui_queue.empty():
            try:
                task = self.gui_queue.get_nowait()
                task()
            except queue.Empty:
                break
        self.after(100, self.process_gui_queue)

    # --- VALIDATION ---
    def set_error(self, field, msg):
        self.inputs[field].configure(bootstyle=DANGER)
        self.errors[field].configure(text=f"⚠ {msg}")

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
            self.errors['days'].configure(text="⚠ Select at least one day")
            ok = False

        return ok

    # --- SUBMISSION LOGIC ---
    def submit_form(self, event=None):
        if str(self.btn_submit['state']) == 'disabled':
            return
            
        if not self.validate_form():
            self.bell()
            return
            
        self.btn_submit.configure(state=DISABLED, text="⏳ Registering...")
        
        # Send standardized date keys to backend
        selected_days = []
        if self.vars['day_1'].get(): selected_days.append("30 August")
        if self.vars['day_2'].get(): selected_days.append("31 August")
        if self.vars['day_3'].get(): selected_days.append("1 September")

        payload = {
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

        threading.Thread(target=self._post_registration, args=(payload,), daemon=True).start()

    def _post_registration(self, payload):
        try:
            res = requests.post(f"{self.server_url}/api/register", json=payload, timeout=5, verify=False)
            res.raise_for_status()
            data = res.json()
            
            if data.get('status') == 'success':
                self.gui_queue.put(lambda: self.show_success_modal(data.get('attendee_id'), is_duplicate=False))
            elif data.get('status') == 'already_registered':
                self.gui_queue.put(lambda: self.show_success_modal(data.get('attendee_id'), is_duplicate=True))
            else:
                err_msg = data.get('message', 'Unknown Error')
                self.gui_queue.put(lambda: self.handle_submit_error(f"Server Error: {err_msg}"))
                
        except requests.exceptions.RequestException as e:
            self.gui_queue.put(lambda: self.handle_submit_error("Connection Error. Cannot reach Hub."))

    def handle_submit_error(self, message):
        messagebox.showerror("Registration Failed", message)
        self.btn_submit.configure(state=NORMAL, text="Register Offline Attendee (Enter)")
        self.bell()

    # --- SUCCESS MODAL ---
    def show_success_modal(self, aid, is_duplicate=False):
        self.bell()
        modal = tk.Toplevel(self)
        modal.geometry("450x300")
        modal.resizable(False, False)
        modal.overrideredirect(True) 
        
        x = self.winfo_x() + (self.winfo_width() // 2) - 225
        y = self.winfo_y() + (self.winfo_height() // 2) - 150
        modal.geometry(f"+{x}+{y}")

        frame = ttk.Frame(modal, borderwidth=4, relief="solid")
        frame.pack(fill=BOTH, expand=True)

        if is_duplicate:
            frame.configure(bootstyle=WARNING)
            ttk.Label(frame, text="Already Registered!", font="-size 18 -weight bold", foreground="#ffbb33").pack(pady=(30, 10))
            ttk.Label(frame, text="This mobile number is already in the system.\nExisting ID:", justify=CENTER).pack()
            id_color = "#ffbb33"
        else:
            frame.configure(bootstyle=SUCCESS)
            ttk.Label(frame, text="Registration Saved!", font="-size 18 -weight bold", foreground="#00e676").pack(pady=(30, 10))
            ttk.Label(frame, text="Please provide the attendee with their official ID pass code:", justify=CENTER).pack()
            id_color = "#00e676"

        ttk.Label(frame, text=aid, font=("Consolas", 26, "bold"), background="#1E1E1E", foreground=id_color, padding=15).pack(pady=20)

        countdown_lbl = ttk.Label(frame, text="Returning to form in 8s... (Press Enter)", foreground="#D4D4D4")
        countdown_lbl.pack()

        def close_modal(event=None):
            self.reset_form()
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

        ttk.Button(frame, text="Next Registration (Enter)", bootstyle=SECONDARY, command=close_modal).pack(pady=(10, 20))
        update_countdown(8)

    def reset_form(self):
        for name, var in self.vars.items():
            if name == 'attendee_type': var.set("GENERAL")
            elif name in ['day_1', 'day_2', 'day_3']: var.set(False)
            else: var.set("")
        
        self.inputs['business_category'].configure(state="readonly")
        self.inputs['other_category'].configure(state=DISABLED)
        self.clear_all_errors()
        self.btn_submit.configure(state=NORMAL, text="Register Offline Attendee (Enter)")
        
        self.scroll_frame.yview_moveto(0)
        self.inputs['full_name'].focus_set()

if __name__ == "__main__":
    app = OfflineKioskApp()
    app.mainloop()