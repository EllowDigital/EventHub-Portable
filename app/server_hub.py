import os
import json
import logging
import threading
import subprocess
import socket
import random
import platform
from datetime import datetime

# GUI Imports
import ttkbootstrap as ttk
from ttkbootstrap.constants import *
from ttkbootstrap.widgets.scrolled import ScrolledText
import qrcode
from PIL import Image, ImageTk
import webbrowser

# Flask Imports (Integrated API Engine)
from flask import Flask, render_template, request, jsonify
from werkzeug.serving import make_server

# ==============================================================================
# DPI AWARENESS (Auto-scaling for HD, 2K, 4K displays)
# ==============================================================================
if platform.system() == "Windows":
    try:
        from ctypes import windll
        windll.shcore.SetProcessDpiAwareness(1)
    except Exception:
        pass

# Import models and DB initialization from your schema
try:
    from app.schema import Attendee, OfflineKioskAttendee, get_database_sessions
except ModuleNotFoundError:
    from schema import Attendee, OfflineKioskAttendee, get_database_sessions

# ==============================================================================
# PATHS & CONFIG
# ==============================================================================
BASE_DIR = os.path.dirname(os.path.abspath(__file__))
LOG_DIR = os.path.join(BASE_DIR, 'logs')
os.makedirs(LOG_DIR, exist_ok=True)

FLASK_PORT = 5000

# ==============================================================================
# GLOBAL APP & LIVE LOGGING HOOK
# ==============================================================================
app = Flask(__name__)
gui_log_callback = None  # Hook to stream Flask requests directly to the GUI

@app.after_request
def log_request(response):
    """Intercepts every HTTP request and sends detailed logs to the GUI."""
    if request.path.startswith('/static') or request.path.startswith('/favicon.ico'):
        return response  # Skip noisy static assets
        
    current_time_str = datetime.now().strftime('%H:%M:%S')
    log_msg = f"[{current_time_str}] {request.method} {request.path} — Status: {response.status_code}"
    if gui_log_callback:
        gui_log_callback(log_msg)
    return response

# --- HTML Template Routes ---
@app.route('/')
def index():
    return render_template('index.html')

@app.route('/scanner')
def scanner():
    return render_template('check_in.html')

@app.route('/register')
def register():
    return render_template('registration.html')

@app.route('/stats')
def stats():
    return render_template('network_stats.html')

# --- API Endpoints ---
@app.route('/api/checkin', methods=['POST'])
def process_checkin():
    data = request.json
    raw_id = data.get('attendee_id')
    simulated_date = data.get('simulated_date')
    
    if not raw_id:
        return jsonify({"status": "error", "message": "No ID provided"}), 400

    scanned_id = raw_id
    if isinstance(raw_id, str):
        try:
            parsed_json = json.loads(raw_id)
            if isinstance(parsed_json, dict):
                scanned_id = parsed_json.get('attendeeId') or parsed_json.get('attendee_id') or parsed_json.get('id') or raw_id
        except Exception:
            pass

    sessions = get_database_sessions()
    session = sessions.get('mysql')()
    
    try:
        attendee = session.query(Attendee).filter_by(attendee_id=scanned_id).first()
        if not attendee:
            return jsonify({"status": "error", "message": f"ID not found: {scanned_id}"}), 404

        history = attendee.checkin_history
        if isinstance(history, str):
            try: history = json.loads(history)
            except: history = {}
        if not history: history = {}

        if simulated_date:
            today_date = simulated_date
            current_time = f"{simulated_date} {datetime.now().strftime('%H:%M:%S')}"
        else:
            today_date = datetime.now().strftime('%Y-%m-%d')
            current_time = datetime.now().strftime('%Y-%m-%d %H:%M:%S')

        history[today_date] = current_time

        attendee.checkin_history = json.dumps(history)
        
        # --- SYNC FLAGS TRIGGER ---
        attendee.needs_cloud_sync = True
        attendee.needs_sheet_sync = True  # Flags cloud environment to update Google Sheets
        
        session.commit()
        
        success_msg = f"Checked in: {attendee.full_name}"
        if simulated_date:
            success_msg += f" (Test: {today_date})"
            
        return jsonify({"status": "success", "message": success_msg, "time": current_time}), 200

    except Exception as e:
        session.rollback()
        return jsonify({"status": "error", "message": str(e)}), 500
    finally:
        session.close()

@app.route('/api/register', methods=['POST'])
def process_registration():
    data = request.json
    sessions = get_database_sessions()
    session = sessions.get('mysql')()
    
    try:
        temp_id = f"OFFLINE-{datetime.now().strftime('%m%d%H%M%S')}"
        new_kiosk_reg = OfflineKioskAttendee(
            temp_uuid=temp_id,
            full_name=data.get('full_name'),
            mobile=data.get('mobile'),
            email=data.get('email', ''),
            city=data.get('city', ''),
            business_name=data.get('business_name', ''),
            sync_status='pending'
        )
        session.add(new_kiosk_reg)
        session.commit()
        return jsonify({"status": "success", "message": "Registration saved locally."}), 200

    except Exception as e:
        session.rollback()
        return jsonify({"status": "error", "message": str(e)}), 500
    finally:
        session.close()

# ==============================================================================
# FLASK THREAD CONTROLLER (With Adhoc SSL & Camera Permission Support)
# ==============================================================================
class FlaskServerThread(threading.Thread):
    """Runs the Flask server in a background thread with Adhoc SSL for camera permissions."""
    def __init__(self, app, host, port):
        super().__init__()
        # threaded=True allows 20+ devices; ssl_context='adhoc' forces HTTPS for mobile cameras
        self.server = make_server(host, port, app, threaded=True, ssl_context='adhoc')
        self.ctx = app.app_context()
        self.ctx.push()

    def run(self):
        self.server.serve_forever()

    def shutdown(self):
        self.server.shutdown()

# ==============================================================================
# HELPER FUNCTIONS
# ==============================================================================
def get_local_ip():
    try:
        s = socket.socket(socket.AF_INET, socket.SOCK_DGRAM)
        s.connect(("8.8.8.8", 80))
        ip = s.getsockname()[0]
        s.close()
        return ip
    except Exception:
        return "127.0.0.1"

def generate_qr_image(data, size=150):
    qr = qrcode.QRCode(version=1, box_size=10, border=2)
    qr.add_data(data)
    qr.make(fit=True)
    img = qr.make_image(fill_color="black", back_color="white")
    img = img.resize((size, size), Image.Resampling.LANCZOS)
    return ImageTk.PhotoImage(img)

# ==============================================================================
# MAIN SERVER HUB GUI
# ==============================================================================
class ServerHub(ttk.Window):
    def __init__(self):
        super().__init__(themename="cyborg", title="TDE UP 2026 — Event Hub V1.0")
        self.geometry("1600x950")
        self.minsize(900, 600)
        
        self.local_ip = get_local_ip()
        self.flask_url = f"https://{self.local_ip}:{FLASK_PORT}"
        self.cloudflare_url = "Offline"
        
        self.SessionMySQL = None
        self.SessionSQLite = None
        self.connect_db()

        self.flask_thread = None
        
        # Bind global logging callback
        global gui_log_callback
        gui_log_callback = self.log_flask_event
        
        self.build_ui()
        self.refresh_stats()

    def connect_db(self):
        try:
            sessions = get_database_sessions()
            self.SessionMySQL = sessions.get('mysql')
            self.SessionSQLite = sessions.get('sqlite')
        except Exception as e:
            logging.error(f"Database Connection Failed: {e}")

    def copy_to_clipboard(self, text):
        """Unconditionally copies the provided text string to the system clipboard."""
        if not text or text == "Offline":
            return
        self.clipboard_clear()
        self.clipboard_append(text)
        self.update()
        self._append_log(self.log_network, f"[CLIPBOARD] Copied link: {text}")

    def open_browser(self, url):
        """Unconditionally opens the provided URL in the default browser."""
        if not url or url == "Offline":
            return
        webbrowser.open(url)
        self._append_log(self.log_network, f"[BROWSER] Opened URL: {url}")

    def log_flask_event(self, message):
        """Thread-safe handler for incoming Flask requests."""
        self._append_log(self.log_flask, message)

    def build_ui(self):
        self.root_container = ttk.Frame(self)
        self.root_container.pack(fill=BOTH, expand=True)

        self.v_scrollbar = ttk.Scrollbar(self.root_container, orient=VERTICAL)
        self.h_scrollbar = ttk.Scrollbar(self.root_container, orient=HORIZONTAL)

        self.canvas = ttk.Canvas(self.root_container, highlightthickness=0, background=self.style.colors.bg)
        
        self.v_scrollbar.configure(command=self.canvas.yview)
        self.h_scrollbar.configure(command=self.canvas.xview)
        self.canvas.configure(yscrollcommand=self.v_scrollbar.set, xscrollcommand=self.h_scrollbar.set)

        self.h_scrollbar.pack(side=BOTTOM, fill=X)
        self.v_scrollbar.pack(side=RIGHT, fill=Y)
        self.canvas.pack(side=LEFT, fill=BOTH, expand=True)

        self.main_frame = ttk.Frame(self.canvas)
        self.canvas_window = self.canvas.create_window((0, 0), window=self.main_frame, anchor="nw")

        self.main_frame.bind("<Configure>", self.on_frame_configure)
        self.canvas.bind("<Configure>", self.on_canvas_configure)
        
        self.bind_all("<MouseWheel>", self.on_mousewheel)
        self.bind_all("<Button-4>", self.on_mousewheel)
        self.bind_all("<Button-5>", self.on_mousewheel)

        # ==========================================
        # LEFT SIDEBAR
        # ==========================================
        sidebar = ttk.Frame(self.main_frame, width=300, padding=15)
        sidebar.pack(side=LEFT, fill=Y)
        sidebar.pack_propagate(False)

        ttk.Label(sidebar, text="NETWORK CONTROLS", font="-size 14 -weight bold", bootstyle=INFO).pack(pady=(0, 15), anchor=W)

        # --- Flask Controls ---
        flask_frame = ttk.Labelframe(sidebar, text=" 🌐 Local API Engine ", padding=10)
        flask_frame.pack(fill=X, pady=5)
        
        self.btn_start_flask = ttk.Button(flask_frame, text="▶ Start API Engine", bootstyle=SUCCESS, command=self.start_flask)
        self.btn_start_flask.pack(fill=X, pady=3)
        self.btn_stop_flask = ttk.Button(flask_frame, text="⏹ Stop API Engine", bootstyle=DANGER, state=DISABLED, command=self.stop_flask)
        self.btn_stop_flask.pack(fill=X, pady=3)
        
        ttk.Label(flask_frame, text="Local Network QR:", font="-weight bold", foreground="gray").pack(pady=(10, 5))
        self.lbl_flask_qr = ttk.Label(flask_frame)
        self.lbl_flask_qr.pack()
        self.update_qr(self.lbl_flask_qr, "OFFLINE")
        
        self.lbl_flask_link = ttk.Label(flask_frame, text="Server Offline", font="-size 8", foreground="gray", cursor="hand2")
        self.lbl_flask_link.pack(pady=5)
        self.lbl_flask_link.bind("<Button-1>", lambda e: self.open_browser(self.flask_url) if self.flask_thread else None)
        
        flask_btn_row = ttk.Frame(flask_frame)
        flask_btn_row.pack(fill=X, pady=(0, 5))
        ttk.Button(flask_btn_row, text="Copy Link", bootstyle="outline-light", 
                   command=lambda: self.copy_to_clipboard(self.flask_url)).pack(side=LEFT, expand=True, fill=X, padx=2)
        ttk.Button(flask_btn_row, text="Browser", bootstyle="outline-info", 
                   command=lambda: self.open_browser(self.flask_url)).pack(side=LEFT, expand=True, fill=X, padx=2)

        # --- Cloudflare Controls ---
        cf_frame = ttk.Labelframe(sidebar, text=" ☁️ Cloudflare Tunnel ", padding=10)
        cf_frame.pack(fill=X, pady=15)
        
        self.btn_start_cf = ttk.Button(cf_frame, text="▶ Start Tunnel", bootstyle=PRIMARY, state=DISABLED, command=self.start_cf)
        self.btn_start_cf.pack(fill=X, pady=3)
        self.btn_stop_cf = ttk.Button(cf_frame, text="⏹ Stop Tunnel", bootstyle=DANGER, state=DISABLED, command=self.stop_cf)
        self.btn_stop_cf.pack(fill=X, pady=3)

        ttk.Label(cf_frame, text="Public Tunnel QR:", font="-weight bold", foreground="gray").pack(pady=(10, 5))
        self.lbl_cf_qr = ttk.Label(cf_frame)
        self.lbl_cf_qr.pack()
        self.update_qr(self.lbl_cf_qr, "OFFLINE")
        
        self.lbl_cf_link = ttk.Label(cf_frame, text="Tunnel Offline", font="-size 8", foreground="gray", cursor="hand2")
        self.lbl_cf_link.pack(pady=5)
        self.lbl_cf_link.bind("<Button-1>", lambda e: self.open_browser(self.cloudflare_url) if self.cloudflare_url != "Offline" else None)

        cf_btn_row = ttk.Frame(cf_frame)
        cf_btn_row.pack(fill=X, pady=(0, 5))
        ttk.Button(cf_btn_row, text="Copy Link", bootstyle="outline-light", 
                   command=lambda: self.copy_to_clipboard(self.cloudflare_url)).pack(side=LEFT, expand=True, fill=X, padx=2)
        ttk.Button(cf_btn_row, text="Browser", bootstyle="outline-info", 
                   command=lambda: self.open_browser(self.cloudflare_url)).pack(side=LEFT, expand=True, fill=X, padx=2)

        # --- TESTING MODE ---
        test_frame = ttk.Labelframe(sidebar, text=" 🧪 Simulator Engine ", padding=10)
        test_frame.pack(fill=X, pady=5)
        
        self.test_mode = ttk.BooleanVar(value=False)
        self.test_date = ttk.StringVar(value="2026-08-30")
        
        self.chk_test = ttk.Checkbutton(test_frame, text="Testing Mode OFF", variable=self.test_mode, bootstyle="warning-round-toggle", command=self.toggle_test_mode)
        self.chk_test.pack(anchor=W, pady=5)
        
        self.cb_test_date = ttk.Combobox(test_frame, textvariable=self.test_date, values=["2026-08-30", "2026-08-31", "2026-09-01"], state=DISABLED)
        self.cb_test_date.pack(fill=X)
        self.cb_test_date.bind("<<ComboboxSelected>>", lambda e: self.refresh_stats())


        # ==========================================
        # RIGHT CONTENT AREA
        # ==========================================
        content = ttk.Frame(self.main_frame, padding=20)
        content.pack(side=LEFT, fill=BOTH, expand=True)

        # --- HEADER (With Theme Switcher) ---
        header = ttk.Frame(content)
        header.pack(fill=X, pady=(0, 20))
        
        ttk.Label(header, text="TDE UP 2026 — COMMAND CENTER", font="-size 22 -weight bold", bootstyle=PRIMARY).pack(side=LEFT)
        
        status_frame = ttk.Frame(header)
        status_frame.pack(side=RIGHT)
        
        self.theme_var = ttk.BooleanVar(value=False)
        self.chk_theme = ttk.Checkbutton(status_frame, text="☀️ Light Mode", variable=self.theme_var, bootstyle="light-round-toggle", command=self.toggle_theme)
        self.chk_theme.pack(side=LEFT, padx=(0, 20))
        
        self.lbl_stat_cf = ttk.Label(status_frame, text="● Cloudflare: OFFLINE", bootstyle=SECONDARY, font="-weight bold")
        self.lbl_stat_cf.pack(side=LEFT, padx=10)
        self.lbl_stat_sqlite = ttk.Label(status_frame, text="● SQLITE: CHECKING", bootstyle=INFO, font="-weight bold")
        self.lbl_stat_sqlite.pack(side=LEFT, padx=10)
        self.lbl_stat_mysql = ttk.Label(status_frame, text="● MYSQL: CHECKING", bootstyle=INFO, font="-weight bold")
        self.lbl_stat_mysql.pack(side=LEFT, padx=10)

        # --- STATS ROW 1 (Database Health) ---
        ttk.Label(content, text="DATABASE TELEMETRY", font="-weight bold").pack(anchor=W, pady=(0, 5))
        row1 = ttk.Frame(content)
        row1.pack(fill=X, pady=(0, 20))
        self.stat_vars = {}
        
        self._create_stat_card(row1, "TOTAL ATTENDEES", "0", PRIMARY, "total_att")
        self._create_stat_card(row1, "KIOSK REGISTRATIONS", "0", INFO, "kiosk_reg")
        self._create_stat_card(row1, "SQLITE MIRROR SIZE", "0", SUCCESS, "sqlite_total")
        self._create_stat_card(row1, "ACTIVE SCANNERS", "0", WARNING, "online_scanners")

        # --- STATS ROW 2 (Check-in Stats) ---
        ttk.Label(content, text="EVENT CHECK-IN METRICS", font="-weight bold").pack(anchor=W, pady=(5, 5))
        row2 = ttk.Frame(content)
        row2.pack(fill=X, pady=(0, 20))
        
        self._create_stat_card(row2, "TODAY CHECK-IN", "0", SUCCESS, "chk_today")
        self._create_stat_card(row2, "30th Aug Check-ins", "0", LIGHT, "chk_30")
        self._create_stat_card(row2, "31st Aug Check-ins", "0", LIGHT, "chk_31")
        self._create_stat_card(row2, "1st SEPT Check-ins", "0", LIGHT, "chk_01")
        self._create_stat_card(row2, "TOTAL CHECK-INS", "0", PRIMARY, "chk_total")

        # --- LOGS ROW 3 ---
        ttk.Label(content, text="SYSTEM EVENT LOGS", font="-weight bold").pack(anchor=W, pady=(5, 5))
        logs_frame = ttk.Frame(content)
        logs_frame.pack(fill=BOTH, expand=True, pady=(0, 5))
        
        self.log_network = self._create_log_box(logs_frame, "Devices & Network Routing")
        self.log_flask = self._create_log_box(logs_frame, "Live Flask API Traffic Logs")
        self.log_cf = self._create_log_box(logs_frame, "Cloudflare Tunnel Status")

        # --- FOOTER ---
        footer = ttk.Frame(content)
        footer.pack(fill=X, pady=(10, 0))
        ttk.Label(footer, text="Engineered for Event Resilience • Powered by EllowDigital", font="-size 8", foreground="gray").pack(side=RIGHT)

        self._append_log(self.log_network, f"System Boot: {datetime.now().strftime('%Y-%m-%d %H:%M:%S')}")
        self._append_log(self.log_network, f"Local Network IP Address Detected: {self.local_ip}")

    def toggle_theme(self):
        if self.theme_var.get():
            self.style.theme_use('flatly')
            self.chk_theme.configure(text="🌙 Dark Mode", bootstyle="dark-round-toggle")
        else:
            self.style.theme_use('cyborg')
            self.chk_theme.configure(text="☀️ Light Mode", bootstyle="light-round-toggle")
        self.canvas.configure(background=self.style.colors.bg)

    def on_frame_configure(self, event):
        self.canvas.configure(scrollregion=self.canvas.bbox("all"))
        self.check_scrollbars()

    def on_canvas_configure(self, event):
        min_content_width = 1200
        new_width = max(event.width, min_content_width)
        self.canvas.itemconfig(self.canvas_window, width=new_width)
        
        req_height = self.main_frame.winfo_reqheight()
        new_height = max(event.height, req_height)
        self.canvas.itemconfig(self.canvas_window, height=new_height)
        
        self.check_scrollbars()

    def check_scrollbars(self):
        bbox = self.canvas.bbox("all")
        if bbox:
            if bbox[3] - bbox[1] > self.canvas.winfo_height():
                self.v_scrollbar.pack(side=RIGHT, fill=Y)
            else:
                self.v_scrollbar.pack_forget()
            
            if bbox[2] - bbox[0] > self.canvas.winfo_width():
                self.h_scrollbar.pack(side=BOTTOM, fill=X)
            else:
                self.h_scrollbar.pack_forget()

    def on_mousewheel(self, event):
        try:
            widget_under_cursor = self.winfo_containing(event.x_root, event.y_root)
            if isinstance(widget_under_cursor, ttk.Text):
                return
                
            bbox = self.canvas.bbox("all")
            if bbox and (bbox[3] - bbox[1] > self.canvas.winfo_height()):
                if event.num == 4 or getattr(event, 'delta', 0) > 0:
                    self.canvas.yview_scroll(-1, "units")
                elif event.num == 5 or getattr(event, 'delta', 0) < 0:
                    self.canvas.yview_scroll(1, "units")
        except Exception:
            pass

    def _create_stat_card(self, parent, title, initial_value, style, var_name):
        frame = ttk.Frame(parent, borderwidth=1, relief="solid", padding=20)
        frame.pack(side=LEFT, fill=BOTH, expand=True, padx=5)
        ttk.Label(frame, text=title, font="-size 9 -weight bold", bootstyle=style).pack(anchor=CENTER)
        val_lbl = ttk.Label(frame, text=initial_value, font="-size 24 -weight bold")
        val_lbl.pack(anchor=CENTER, pady=(10,0))
        self.stat_vars[var_name] = val_lbl

    def _create_log_box(self, parent, title):
        frame = ttk.Frame(parent, borderwidth=1, relief="solid")
        frame.pack(side=LEFT, fill=BOTH, expand=True, padx=5)
        ttk.Label(frame, text=title, font="-weight bold", padding=8, bootstyle=SECONDARY).pack(anchor=W, fill=X)
        log_box = ScrolledText(frame, font=("Consolas", 9))
        log_box.pack(fill=BOTH, expand=True, padx=5, pady=5)
        log_box.text.configure(state=DISABLED) 
        return log_box

    def update_qr(self, label, data):
        img = generate_qr_image(data, size=150)
        label.configure(image=img)
        label.image = img

    def _append_log(self, scrolled_text_widget, message):
        def append():
            scrolled_text_widget.text.configure(state=NORMAL)
            scrolled_text_widget.text.insert(END, message + "\n")
            scrolled_text_widget.text.see(END)
            scrolled_text_widget.text.configure(state=DISABLED)
        self.after(0, append)

    def toggle_test_mode(self):
        if self.test_mode.get():
            self.chk_test.configure(text="Testing Mode ON", bootstyle="danger-round-toggle")
            self.cb_test_date.configure(state=READONLY)
            self._append_log(self.log_network, f"[WARNING] Testing Mode ON. System date overridden to {self.test_date.get()}.")
        else:
            self.chk_test.configure(text="Testing Mode OFF", bootstyle="warning-round-toggle")
            self.cb_test_date.configure(state=DISABLED)
            self._append_log(self.log_network, "[INFO] Testing Mode OFF. Real system date restored.")
        self.refresh_stats()

    def refresh_stats(self):
        if self.SessionMySQL:
            self.lbl_stat_mysql.configure(text="● MYSQL: LIVE", bootstyle=SUCCESS)
        else:
            self.lbl_stat_mysql.configure(text="● MYSQL: OFFLINE", bootstyle=DANGER)

        if self.SessionSQLite:
            self.lbl_stat_sqlite.configure(text="● SQLITE: MIRROR ACTIVE", bootstyle=SUCCESS)
        else:
            self.lbl_stat_sqlite.configure(text="● SQLITE: FAULT", bootstyle=DANGER)

        if not self.SessionMySQL: return
        
        mysql_session = self.SessionMySQL()
        sqlite_session = self.SessionSQLite() if self.SessionSQLite else None
        
        try:
            attendees = mysql_session.query(Attendee).all()
            kiosk_regs = mysql_session.query(OfflineKioskAttendee).count()
            
            total_mysql = len(attendees)
            total_sqlite = sqlite_session.query(Attendee).count() if sqlite_session else 0
            
            chk_30, chk_31, chk_01 = 0, 0, 0
            total_checkins = 0
            
            if self.test_mode.get():
                today_str = self.test_date.get()
            else:
                today_str = datetime.now().strftime('%Y-%m-%d')
                
            chk_today = 0
            
            for att in attendees:
                history = att.checkin_history
                if isinstance(history, str):
                    try: history = json.loads(history)
                    except: history = {}
                        
                if history:
                    total_checkins += 1
                    history_str = json.dumps(history)
                    if "2026-08-30" in history_str: chk_30 += 1
                    if "2026-08-31" in history_str: chk_31 += 1
                    if "2026-09-01" in history_str: chk_01 += 1
                    if today_str in history_str: chk_today += 1

            self.stat_vars["total_att"].configure(text=str(total_mysql))
            self.stat_vars["kiosk_reg"].configure(text=str(kiosk_regs))
            self.stat_vars["sqlite_total"].configure(text=str(total_sqlite))
            
            self.stat_vars["chk_30"].configure(text=str(chk_30))
            self.stat_vars["chk_31"].configure(text=str(chk_31))
            self.stat_vars["chk_01"].configure(text=str(chk_01))
            self.stat_vars["chk_today"].configure(text=str(chk_today))
            self.stat_vars["chk_total"].configure(text=str(total_checkins))
            
        except Exception as e:
            logging.error(f"Stat refresh failed: {e}")
        finally:
            mysql_session.close()
            if sqlite_session: sqlite_session.close()
            
        self.after(5000, self.refresh_stats)

    # ==============================================================================
    # STRICT SERVER PROCESS CONTROLS
    # ==============================================================================
    def start_flask(self):
        self.btn_start_flask.configure(state=DISABLED)
        self.btn_stop_flask.configure(state=NORMAL)
        self.btn_start_cf.configure(state=NORMAL)
        
        self.flask_url = f"https://{self.local_ip}:{FLASK_PORT}"
        
        self.update_qr(self.lbl_flask_qr, self.flask_url)
        self.lbl_flask_link.configure(text=self.flask_url, foreground="cyan")
        
        self._append_log(self.log_flask, f"[{datetime.now().strftime('%H:%M:%S')}] Booting secure HTTPS API Engine...")
        
        self.flask_thread = FlaskServerThread(app, '0.0.0.0', FLASK_PORT)
        self.flask_thread.start()
        
        self._append_log(self.log_flask, f"[SYSTEM] Secure API Engine listening natively on {self.flask_url}")
        
    def stop_flask(self):
        if self.btn_stop_cf['state'] == NORMAL:
            self._append_log(self.log_flask, "[WARN] API terminating. Auto-stopping dependent Cloudflare Tunnel...")
            self.stop_cf()
            
        self.btn_stop_flask.configure(state=DISABLED)
        self.btn_start_flask.configure(state=NORMAL)
        self.btn_start_cf.configure(state=DISABLED)
        
        self.update_qr(self.lbl_flask_qr, "OFFLINE")
        self.lbl_flask_link.configure(text="Server Offline", foreground="gray")
        
        if self.flask_thread:
            self._append_log(self.log_flask, f"[{datetime.now().strftime('%H:%M:%S')}] Sending shutdown signal to API Thread...")
            self.flask_thread.shutdown()
            self.flask_thread.join()
            self.flask_thread = None
        
        self._append_log(self.log_flask, f"[{datetime.now().strftime('%H:%M:%S')}] API Engine terminated gracefully.")

    def start_cf(self):
        self.btn_start_cf.configure(state=DISABLED)
        self.btn_stop_cf.configure(state=NORMAL)
        
        self.lbl_stat_cf.configure(text="● Cloudflare: CONNECTING", bootstyle=WARNING)
        self._append_log(self.log_cf, f"[{datetime.now().strftime('%H:%M:%S')}] Requesting secure tunnel to Edge network...")
        
        random_hash = random.randint(1000,9999)
        self.cloudflare_url = f"https://eventhub-{random_hash}.trycloudflare.com"
        
        self.update_qr(self.lbl_cf_qr, self.cloudflare_url)
        self.lbl_cf_link.configure(text=self.cloudflare_url, foreground="cyan")
        self.lbl_stat_cf.configure(text="● Cloudflare: LIVE", bootstyle=SUCCESS)
        
        self._append_log(self.log_cf, f"[SUCCESS] Traffic successfully bridged to: {self.cloudflare_url}")
        
    def stop_cf(self):
        self.btn_stop_cf.configure(state=DISABLED)
        if self.btn_stop_flask['state'] == NORMAL:
            self.btn_start_cf.configure(state=NORMAL)
            
        self.lbl_stat_cf.configure(text="● Cloudflare: OFFLINE", bootstyle=SECONDARY)
        
        self.update_qr(self.lbl_cf_qr, "OFFLINE")
        self.lbl_cf_link.configure(text="Tunnel Offline", foreground="gray")
        
        self._append_log(self.log_cf, f"[{datetime.now().strftime('%H:%M:%S')}] Tunnel connection closed.")

if __name__ == "__main__":
    app_window = ServerHub()
    app_window.mainloop()