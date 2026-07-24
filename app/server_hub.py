import os
import json
import logging
import threading
import subprocess
import socket
import random
import platform
import re
import time
import uuid
import queue
from datetime import datetime, timezone

import ttkbootstrap as ttk
from ttkbootstrap.constants import *
from ttkbootstrap.widgets.scrolled import ScrolledText
import qrcode
from PIL import Image, ImageTk
import webbrowser

from flask import Flask, render_template, request, jsonify, Response
from werkzeug.serving import make_server

# Windows DPI Awareness for crisp text
if platform.system() == "Windows":
    try:
        from ctypes import windll
        windll.shcore.SetProcessDpiAwareness(1)
    except Exception:
        pass

# Import models dynamically based on context
try:
    from app.schema import Attendee, OfflineKioskAttendee, get_database_sessions
except ModuleNotFoundError:
    from schema import Attendee, OfflineKioskAttendee, get_database_sessions

# ==============================================================================
# CONFIGURATION
# ==============================================================================
BASE_DIR = os.path.dirname(os.path.abspath(__file__))
LOG_DIR = os.path.join(BASE_DIR, 'logs')
os.makedirs(LOG_DIR, exist_ok=True)

FLASK_PORT = 5000

template_dir = os.path.join(BASE_DIR, 'templates')
static_dir = os.path.join(BASE_DIR, 'static')
app = Flask(__name__, template_folder=template_dir, static_folder=static_dir)

gui_log_callback = None
SERVER_TEST_MODE = False
SERVER_TEST_DATE = "2026-08-30"

ACTIVE_DEVICES = {}
SCAN_CLIENTS = []  # Holds queues for Server-Sent Events (SSE)

# ==============================================================================
# FLASK MIDDLEWARE & EVENT BROADCASTER
# ==============================================================================
@app.after_request
def log_request(response):
    if request.path in ['/api/status', '/api/network-data', '/api/stream-scans'] or request.path.startswith('/static') or request.path.startswith('/favicon.ico'):
        return response  
        
    current_time_str = datetime.now().strftime('%H:%M:%S')
    log_msg = f"[{current_time_str}] {request.method} {request.path} — Status: {response.status_code}"
    if gui_log_callback:
        gui_log_callback(log_msg)
    return response

def broadcast_scan(attendee, status, message, device_name, scan_time):
    att_dict = None
    if attendee:
        att_dict = {
            "attendee_id": attendee.attendee_id,
            "full_name": attendee.full_name,
            "business_name": attendee.business_name,
            "mobile": attendee.mobile,
            "city": attendee.city,
            "state": attendee.state,
            "attendee_type": getattr(attendee.attendee_type, 'value', str(attendee.attendee_type)),
            "gender": getattr(attendee.gender, 'value', str(attendee.gender)),
        }
        
    event = {
        "status": status,
        "message": message,
        "device": device_name,
        "timestamp": scan_time,
        "attendee": att_dict
    }
    
    for q in SCAN_CLIENTS:
        try:
            q.put(event)
        except Exception:
            pass

# ==============================================================================
# FLASK ROUTES & APIS
# ==============================================================================
@app.route('/')
def index(): return render_template('index.html')

@app.route('/scanner')
def scanner(): return render_template('check_in.html')

@app.route('/register')
def register(): return render_template('registration.html')

@app.route('/stats')
def stats(): return render_template('network_stats.html')

@app.route('/api/status', methods=['GET'])
def get_server_status():
    ip = request.remote_addr
    custom_device_name = request.args.get('device_name', 'Unknown Device')
    if custom_device_name and custom_device_name != "null":
        ACTIVE_DEVICES[ip] = {'last_seen': time.time(), 'name': custom_device_name}
    return jsonify({"test_mode": SERVER_TEST_MODE, "test_date": SERVER_TEST_DATE}), 200

@app.route('/api/stream-scans')
def stream_scans():
    def event_stream():
        q = queue.Queue()
        SCAN_CLIENTS.append(q)
        try:
            while True:
                try:
                    data = q.get(timeout=10)
                    yield f"data: {json.dumps(data)}\n\n"
                except queue.Empty:
                    yield ": heartbeat\n\n"
        except GeneratorExit:
            pass 
        finally:
            if q in SCAN_CLIENTS:
                SCAN_CLIENTS.remove(q)
                
    return Response(event_stream(), mimetype='text/event-stream', headers={'Cache-Control': 'no-cache', 'Connection': 'keep-alive'})

@app.route('/api/checkin', methods=['POST'])
def process_checkin():
    global SERVER_TEST_MODE, SERVER_TEST_DATE
    data = request.json
    
    identifier = str(data.get('attendee_id', '')).strip()
    search_type = data.get('search_type', 'id')
    device_name = data.get('device_name', f"Scanner ({request.remote_addr})")
    
    # Strictly formats timestamp to match JavaScript ISOString 'Z' ending
    iso_timestamp = datetime.now(timezone.utc).strftime('%Y-%m-%dT%H:%M:%S.000Z')
    
    if not identifier:
        msg = "No ID or Phone provided"
        broadcast_scan(None, "ERROR", msg, device_name, iso_timestamp)
        return jsonify({"status": "error", "message": msg}), 400

    if search_type == 'id' and "{" in identifier:
        try:
            parsed_json = json.loads(identifier)
            identifier = parsed_json.get('attendeeId', parsed_json.get('attendee_id', parsed_json.get('id', identifier)))
        except Exception: pass

    sessions = get_database_sessions()
    session = sessions.get('mysql')()
    
    try:
        # Row locks for rapid concurrent scanning
        attendee = None
        if search_type == 'phone':
            attendee = session.query(Attendee).filter_by(mobile=identifier).with_for_update().first()
            if not attendee:
                attendee = session.query(OfflineKioskAttendee).filter_by(mobile=identifier).with_for_update().first()
        else:
            attendee = session.query(Attendee).filter_by(attendee_id=identifier).with_for_update().first()
            if not attendee:
                attendee = session.query(OfflineKioskAttendee).filter_by(attendee_id=identifier).with_for_update().first()

        if not attendee:
            search_label = "Phone" if search_type == 'phone' else "ID"
            msg = f"{search_label} not found: {identifier}"
            broadcast_scan(None, "ERROR", msg, device_name, iso_timestamp)
            return jsonify({"status": "error", "message": msg}), 404

        history = attendee.checkin_history
        if isinstance(history, str):
            try: history = json.loads(history)
            except: history = {}
        if not isinstance(history, dict): history = {}

        current_date_str = SERVER_TEST_DATE if SERVER_TEST_MODE else datetime.now(timezone.utc).strftime('%Y-%m-%d')
        
        date_map = {
            "2026-08-30": "30 August",
            "2026-08-31": "31 August",
            "2026-09-01": "1 September"
        }
        
        if current_date_str not in date_map:
            msg = f"Scan rejected: {current_date_str} is not an official event day."
            broadcast_scan(attendee, "ERROR", msg, device_name, iso_timestamp)
            return jsonify({"status": "error", "message": msg}), 400
            
        today_key = date_map[current_date_str]

        att_days = attendee.attendance_days or []
        if isinstance(att_days, str):
            try: att_days = json.loads(att_days)
            except: att_days = []
            
        if today_key not in att_days:
            msg = f"Access Denied: {attendee.full_name} does not have a pass for today ({today_key})."
            broadcast_scan(attendee, "ERROR", msg, device_name, iso_timestamp)
            return jsonify({"status": "error", "message": msg}), 403

        if today_key in history:
            msg = f"Already checked in today: {attendee.full_name}"
            broadcast_scan(attendee, "DUPLICATE", msg, device_name, iso_timestamp)
            return jsonify({"status": "error", "message": msg}), 400

        # Exact JSON Schema enforcement
        history[today_key] = {
            "timestamp": iso_timestamp,
            "source": "offline_hub",
            "device": device_name,
            "date_code": current_date_str,
            "display_date": today_key
        }

        attendee.checkin_history = history 
        
        attendee.needs_cloud_sync = True
        attendee.needs_sheet_sync = True
        attendee.needs_local_sync = False 
        attendee.local_modified = True
        
        session.commit()
        
        success_msg = f"Checked in: {attendee.full_name}"
        if SERVER_TEST_MODE: success_msg += f" (Test: {today_key})"
        
        broadcast_scan(attendee, "SUCCESS", success_msg, device_name, iso_timestamp)
            
        return jsonify({"status": "success", "message": success_msg, "time": iso_timestamp}), 200

    except Exception as e:
        session.rollback()
        broadcast_scan(None, "ERROR", str(e), device_name, iso_timestamp)
        return jsonify({"status": "error", "message": str(e)}), 500
    finally:
        session.close()

@app.route('/api/register', methods=['POST'])
def process_registration():
    global SERVER_TEST_MODE, SERVER_TEST_DATE
    data = request.json
    sessions = get_database_sessions()
    session = sessions.get('mysql')()
    
    mobile_number = data.get('mobile', '').strip()
    
    req_ip = request.remote_addr
    req_os = request.user_agent.platform or "Unknown"
    device_label = data.get('device_name', f"Kiosk ({req_os.capitalize()} - {req_ip})")

    # Strictly formats timestamp to match JavaScript ISOString 'Z' ending
    iso_timestamp = datetime.now(timezone.utc).strftime('%Y-%m-%dT%H:%M:%S.000Z')

    try:
        existing_main = session.query(Attendee).filter_by(mobile=mobile_number).with_for_update().first()
        if existing_main: 
            return jsonify({"status": "already_registered", "message": "Found in main DB.", "attendee_id": existing_main.attendee_id}), 200

        existing_kiosk = session.query(OfflineKioskAttendee).filter_by(mobile=mobile_number).with_for_update().first()
        if existing_kiosk: 
            return jsonify({"status": "already_registered", "message": "Found in kiosk DB.", "attendee_id": existing_kiosk.attendee_id}), 200

        def gen_id(att_type: str) -> str:
            prefix = {"GENERAL":"G", "BUSINESS":"B", "MEDIA":"M", "EXHIBITOR":"E"}.get(att_type.upper(), "G")
            chars = "ABCDEFGHJKLMNPQRSTUVWXYZ23456789"
            for _ in range(5000):
                code = "".join(random.choices(chars, k=6))
                aid = f"TDE26-{prefix}-{code}"
                if not session.query(Attendee).filter_by(attendee_id=aid).first() and not session.query(OfflineKioskAttendee).filter_by(attendee_id=aid).first():
                    return aid
            raise RuntimeError("ID generation failed")

        new_attendee_id = gen_id(data.get('attendee_type', 'GENERAL'))
        
        today_date = SERVER_TEST_DATE if SERVER_TEST_MODE else datetime.now(timezone.utc).strftime('%Y-%m-%d')
        valid_event_days = ["2026-08-30", "2026-08-31", "2026-09-01"]
        
        checkin_history_dict = {}
        if today_date in valid_event_days:
            date_map = {"2026-08-30": "30 August", "2026-08-31": "31 August", "2026-09-01": "1 September"}
            key = date_map[today_date]
            # Exact JSON Schema enforcement for auto-checkin
            checkin_history_dict[key] = {
                "timestamp": iso_timestamp,
                "source": "offline_hub",
                "device": device_label,
                "date_code": today_date,
                "display_date": key
            }

        new_kiosk_reg = OfflineKioskAttendee(
            id=str(uuid.uuid4()), 
            attendee_id=new_attendee_id, 
            full_name=data.get('full_name'),
            mobile=mobile_number,
            email=data.get('email', ''),
            gender=data.get('gender'),
            attendee_type=data.get('attendee_type'),
            business_name=data.get('business_name', ''),
            business_category=data.get('business_category', ''),
            other_category=data.get('other_category', ''),
            address=data.get('address', ''),
            city=data.get('city', ''),
            state=data.get('state', ''),
            pincode=data.get('pincode', ''),
            attendance_days=data.get('attendance_days', []),
            photo_url=None, 
            checkin_history=checkin_history_dict, 
            device_name=device_label,
            needs_cloud_sync=True,
            needs_sheet_sync=True,
            needs_local_sync=False, 
            local_modified=True
        )
        session.add(new_kiosk_reg)
        session.commit()
        return jsonify({"status": "success", "message": "Registration saved locally.", "attendee_id": new_attendee_id}), 200

    except Exception as e:
        session.rollback()
        return jsonify({"status": "error", "message": str(e)}), 500
    finally:
        session.close()

@app.route('/api/network-data', methods=['GET'])
def get_network_data():
    current_time = time.time()
    active_devices = {}
    for ip, data in list(ACTIVE_DEVICES.items()):
        if current_time - data['last_seen'] < 30: active_devices[ip] = data
        else: del ACTIVE_DEVICES[ip]

    sessions = get_database_sessions()
    session = sessions.get('mysql')()
    global_stats = {"total_scans": 0, "total_registrations": 0, "today_scans": 0}
    
    try:
        global_stats["total_registrations"] = session.query(OfflineKioskAttendee).count()
        today_date = SERVER_TEST_DATE if SERVER_TEST_MODE else datetime.now(timezone.utc).strftime('%Y-%m-%d')
        
        chk_30 = session.query(Attendee).filter(Attendee.checkin_history.like('%"30 August"%')).count()
        chk_31 = session.query(Attendee).filter(Attendee.checkin_history.like('%"31 August"%')).count()
        chk_01 = session.query(Attendee).filter(Attendee.checkin_history.like('%"1 September"%')).count()
        
        global_stats["total_scans"] = chk_30 + chk_31 + chk_01
        
        if today_date == "2026-08-30": 
            global_stats["today_scans"] = chk_30
        elif today_date == "2026-08-31": 
            global_stats["today_scans"] = chk_31
        elif today_date == "2026-09-01": 
            global_stats["today_scans"] = chk_01

    except Exception as e: 
        print(f"Network Data Error: {e}")
    finally: 
        session.close()

    return jsonify({"active_devices": active_devices, "global_stats": global_stats}), 200

@app.route('/api/device/rename', methods=['POST'])
def rename_device():
    data = request.json or {}
    ip = data.get('ip')
    new_name = data.get('new_name')
    if ip in ACTIVE_DEVICES and new_name:
        ACTIVE_DEVICES[ip]['name'] = new_name
        return jsonify({"status": "success", "message": "Device renamed successfully"}), 200
    return jsonify({"status": "error", "message": "Device not found or invalid name"}), 404


# ==============================================================================
# THREADING & HELPERS
# ==============================================================================
class FlaskServerThread(threading.Thread):
    def __init__(self, app, host, port):
        super().__init__()
        self.server = make_server(host, port, app, threaded=True, ssl_context='adhoc')
        self.ctx = app.app_context()
        self.ctx.push()

    def run(self): self.server.serve_forever()
    def shutdown(self): self.server.shutdown()

def get_local_ip():
    try:
        s = socket.socket(socket.AF_INET, socket.SOCK_DGRAM)
        s.connect(("8.8.8.8", 80))
        ip = s.getsockname()[0]
        s.close()
        return ip
    except Exception: return "127.0.0.1"

def generate_qr_image(data, size=150):
    qr = qrcode.QRCode(version=1, box_size=10, border=2)
    qr.add_data(data)
    qr.make(fit=True)
    img = qr.make_image(fill_color="black", back_color="white")
    return ImageTk.PhotoImage(img.resize((size, size), Image.Resampling.LANCZOS))


# ==============================================================================
# MAIN SERVER HUB GUI
# ==============================================================================
class ServerHub(ttk.Window):
    def __init__(self):
        super().__init__(themename="darkly", title="TDE UP 2026 — Event Hub V1.0")
        self.geometry("1600x950")
        self.minsize(1000, 700)
        
        self.local_ip = get_local_ip()
        self.flask_url = f"https://{self.local_ip}:{FLASK_PORT}"
        self.cloudflare_url = "Offline"
        
        self.SessionMySQL = None
        self.SessionSQLite = None
        self.connect_db()

        self.flask_thread = None
        self.cf_process = None 
        
        self.gui_queue = queue.Queue()
        
        global gui_log_callback
        gui_log_callback = self.log_flask_event
        
        self.build_ui()
        self.process_gui_queue() 
        self.refresh_stats()

    def connect_db(self):
        try:
            sessions = get_database_sessions()
            self.SessionMySQL = sessions.get('mysql')
            self.SessionSQLite = sessions.get('sqlite')
        except Exception as e:
            logging.error(f"Database Connection Failed: {e}")

    def process_gui_queue(self):
        while not self.gui_queue.empty():
            try:
                task = self.gui_queue.get_nowait()
                task() 
            except queue.Empty:
                break
        self.after(50, self.process_gui_queue)

    def _append_log(self, scrolled_text_widget, message):
        def append():
            scrolled_text_widget.text.configure(state=NORMAL)
            scrolled_text_widget.text.insert(END, message + "\n")
            scrolled_text_widget.text.see(END)
            scrolled_text_widget.text.configure(state=DISABLED)
        self.gui_queue.put(append)

    def log_flask_event(self, message):
        self._append_log(self.log_flask, message)

    def copy_to_clipboard(self, text):
        if not text or text == "Offline": return
        self.clipboard_clear()
        self.clipboard_append(text)
        self.update()
        self._append_log(self.log_network, f"[CLIPBOARD] Copied link: {text}")

    def open_browser(self, url):
        if not url or url == "Offline": return
        webbrowser.open(url)
        self._append_log(self.log_network, f"[BROWSER] Opened URL: {url}")

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

        # --- SIDEBAR ---
        sidebar = ttk.Frame(self.main_frame, width=320, padding=20)
        sidebar.pack(side=LEFT, fill=Y)
        sidebar.pack_propagate(False)

        ttk.Label(sidebar, text="NETWORK & ROUTING", font="-size 13 -weight bold", bootstyle=INFO).pack(pady=(0, 15), anchor=W)

        flask_frame = ttk.Labelframe(sidebar, text=" 🌐 Local API Engine ", padding=15)
        flask_frame.pack(fill=X, pady=5)
        
        self.btn_start_flask = ttk.Button(flask_frame, text="▶ Start Engine", bootstyle=SUCCESS, command=self.start_flask)
        self.btn_start_flask.pack(fill=X, pady=3)
        self.btn_stop_flask = ttk.Button(flask_frame, text="⏹ Stop Engine", bootstyle=DANGER, state=DISABLED, command=self.stop_flask)
        self.btn_stop_flask.pack(fill=X, pady=3)
        
        ttk.Label(flask_frame, text="Local Network QR:", font="-size 9 -weight bold", foreground="#888").pack(pady=(15, 5))
        self.lbl_flask_qr = ttk.Label(flask_frame)
        self.lbl_flask_qr.pack()
        self.update_qr(self.lbl_flask_qr, "OFFLINE")
        
        self.lbl_flask_link = ttk.Label(flask_frame, text="Server Offline", font="-size 9", foreground="gray", cursor="hand2")
        self.lbl_flask_link.pack(pady=8)
        self.lbl_flask_link.bind("<Button-1>", lambda e: self.open_browser(self.flask_url) if self.flask_thread else None)
        
        flask_btn_row = ttk.Frame(flask_frame)
        flask_btn_row.pack(fill=X, pady=(5, 5))
        ttk.Button(flask_btn_row, text="Copy Link", bootstyle="outline-light", command=lambda: self.copy_to_clipboard(self.flask_url)).pack(side=LEFT, expand=True, fill=X, padx=2)
        ttk.Button(flask_btn_row, text="Browser", bootstyle="outline-info", command=lambda: self.open_browser(self.flask_url)).pack(side=LEFT, expand=True, fill=X, padx=2)

        cf_frame = ttk.Labelframe(sidebar, text=" ☁️ Cloudflare Tunnel ", padding=15)
        cf_frame.pack(fill=X, pady=20)
        
        self.btn_start_cf = ttk.Button(cf_frame, text="▶ Start Tunnel", bootstyle=PRIMARY, state=DISABLED, command=self.start_cf)
        self.btn_start_cf.pack(fill=X, pady=3)
        self.btn_stop_cf = ttk.Button(cf_frame, text="⏹ Stop Tunnel", bootstyle=DANGER, state=DISABLED, command=self.stop_cf)
        self.btn_stop_cf.pack(fill=X, pady=3)

        ttk.Label(cf_frame, text="Public Tunnel QR:", font="-size 9 -weight bold", foreground="#888").pack(pady=(15, 5))
        self.lbl_cf_qr = ttk.Label(cf_frame)
        self.lbl_cf_qr.pack()
        self.update_qr(self.lbl_cf_qr, "OFFLINE")
        
        self.lbl_cf_link = ttk.Label(cf_frame, text="Tunnel Offline", font="-size 9", foreground="gray", cursor="hand2")
        self.lbl_cf_link.pack(pady=8)
        self.lbl_cf_link.bind("<Button-1>", lambda e: self.open_browser(self.cloudflare_url) if self.cloudflare_url != "Offline" else None)

        cf_btn_row = ttk.Frame(cf_frame)
        cf_btn_row.pack(fill=X, pady=(5, 5))
        ttk.Button(cf_btn_row, text="Copy Link", bootstyle="outline-light", command=lambda: self.copy_to_clipboard(self.cloudflare_url)).pack(side=LEFT, expand=True, fill=X, padx=2)
        ttk.Button(cf_btn_row, text="Browser", bootstyle="outline-info", command=lambda: self.open_browser(self.cloudflare_url)).pack(side=LEFT, expand=True, fill=X, padx=2)

        test_frame = ttk.Labelframe(sidebar, text=" 🧪 Simulator Engine ", padding=15)
        test_frame.pack(fill=X, pady=5)
        
        self.test_mode = ttk.BooleanVar(value=False)
        self.test_date = ttk.StringVar(value="2026-08-30")
        
        self.chk_test = ttk.Checkbutton(test_frame, text="Testing Mode OFF", variable=self.test_mode, bootstyle="warning-round-toggle", command=self.toggle_test_mode)
        self.chk_test.pack(anchor=W, pady=5)
        
        self.cb_test_date = ttk.Combobox(test_frame, textvariable=self.test_date, values=["2026-08-30", "2026-08-31", "2026-09-01"], state=DISABLED)
        self.cb_test_date.pack(fill=X, pady=(10, 0))
        self.cb_test_date.bind("<<ComboboxSelected>>", lambda e: self.on_test_date_changed())

        # --- MAIN CONTENT AREA ---
        content = ttk.Frame(self.main_frame, padding=25)
        content.pack(side=LEFT, fill=BOTH, expand=True)

        header = ttk.Frame(content)
        header.pack(fill=X, pady=(0, 25))
        
        ttk.Label(header, text="TDE UP 2026 — COMMAND CENTER", font="-size 20 -weight bold", bootstyle=PRIMARY).pack(side=LEFT)
        
        status_frame = ttk.Frame(header)
        status_frame.pack(side=RIGHT)
        
        self.lbl_stat_cf = ttk.Label(status_frame, text="● Cloudflare: OFFLINE", bootstyle=SECONDARY, font="-weight bold")
        self.lbl_stat_cf.pack(side=LEFT, padx=10)
        self.lbl_stat_sqlite = ttk.Label(status_frame, text="● SQLITE: CHECKING", bootstyle=INFO, font="-weight bold")
        self.lbl_stat_sqlite.pack(side=LEFT, padx=10)
        self.lbl_stat_mysql = ttk.Label(status_frame, text="● MYSQL: CHECKING", bootstyle=INFO, font="-weight bold")
        self.lbl_stat_mysql.pack(side=LEFT, padx=10)

        ttk.Label(content, text="📡 ACTIVE CONNECTED DEVICES", font="-size 11 -weight bold", bootstyle=INFO).pack(anchor=W, pady=(5, 5))
        self.lbl_devices = ttk.Label(content, text="Awaiting connections...", font=("Consolas", 11), bootstyle=SUCCESS)
        self.lbl_devices.pack(anchor=W, pady=(0, 20))

        ttk.Label(content, text="🗄️ DATABASE TELEMETRY", font="-size 11 -weight bold").pack(anchor=W, pady=(0, 10))
        row1 = ttk.Frame(content)
        row1.pack(fill=X, pady=(0, 25))
        self.stat_vars = {}
        
        self._create_stat_card(row1, "TOTAL ATTENDEES", "0", PRIMARY, "total_att")
        self._create_stat_card(row1, "KIOSK REGISTRATIONS", "0", INFO, "kiosk_reg")
        self._create_stat_card(row1, "SQLITE MIRROR SIZE", "0", SUCCESS, "sqlite_total")
        self._create_stat_card(row1, "ACTIVE SCANNERS", "0", WARNING, "online_scanners")

        ttk.Label(content, text="📅 EVENT CHECK-IN METRICS", font="-size 11 -weight bold").pack(anchor=W, pady=(5, 10))
        row2 = ttk.Frame(content)
        row2.pack(fill=X, pady=(0, 25))
        
        self._create_stat_card(row2, "TODAY CHECK-IN", "0", SUCCESS, "chk_today")
        self._create_stat_card(row2, "30th Aug Check-ins", "0", LIGHT, "chk_30")
        self._create_stat_card(row2, "31st Aug Check-ins", "0", LIGHT, "chk_31")
        self._create_stat_card(row2, "1st SEPT Check-ins", "0", LIGHT, "chk_01")
        self._create_stat_card(row2, "TOTAL CHECK-INS", "0", PRIMARY, "chk_total")

        ttk.Label(content, text="⚙️ SYSTEM EVENT LOGS", font="-size 11 -weight bold").pack(anchor=W, pady=(5, 10))
        logs_frame = ttk.Frame(content)
        logs_frame.pack(fill=BOTH, expand=True, pady=(0, 5))
        
        self.log_network = self._create_log_box(logs_frame, "Devices & Network Routing")
        self.log_flask = self._create_log_box(logs_frame, "Live Flask API Traffic Logs")
        self.log_cf = self._create_log_box(logs_frame, "Cloudflare Tunnel Status")

        footer = ttk.Frame(content)
        footer.pack(fill=X, pady=(15, 0))
        ttk.Label(footer, text="Engineered for Event Resilience • Powered by EllowDigital", font="-size 9", foreground="#666").pack(side=RIGHT)

        self._append_log(self.log_network, f"System Boot: {datetime.now().strftime('%Y-%m-%d %H:%M:%S')}")
        self._append_log(self.log_network, f"Local Network IP Address Detected: {self.local_ip}")

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
            if bbox[3] - bbox[1] > self.canvas.winfo_height(): self.v_scrollbar.pack(side=RIGHT, fill=Y)
            else: self.v_scrollbar.pack_forget()
            if bbox[2] - bbox[0] > self.canvas.winfo_width(): self.h_scrollbar.pack(side=BOTTOM, fill=X)
            else: self.h_scrollbar.pack_forget()

    def _create_stat_card(self, parent, title, initial_value, style, var_name):
        frame = ttk.Frame(parent, borderwidth=1, relief="solid", padding=20, bootstyle="dark")
        frame.pack(side=LEFT, fill=BOTH, expand=True, padx=8)
        ttk.Label(frame, text=title, font="-size 9 -weight bold", foreground="#AAA").pack(anchor=CENTER)
        val_lbl = ttk.Label(frame, text=initial_value, font="-size 28 -weight bold", bootstyle=style)
        val_lbl.pack(anchor=CENTER, pady=(12,0))
        self.stat_vars[var_name] = val_lbl

    def _create_log_box(self, parent, title):
        frame = ttk.Frame(parent, borderwidth=1, relief="solid", bootstyle="dark")
        frame.pack(side=LEFT, fill=BOTH, expand=True, padx=8)
        ttk.Label(frame, text=title, font="-size 10 -weight bold", padding=10, background="#252526", foreground="#CCC").pack(anchor=W, fill=X)
        log_box = ScrolledText(frame, font=("Consolas", 10))
        log_box.pack(fill=BOTH, expand=True, padx=2, pady=2)
        log_box.text.configure(state=DISABLED, bg="#1E1E1E", fg="#D4D4D4", insertbackground="#D4D4D4", selectbackground="#264F78", borderwidth=0) 
        return log_box

    def update_qr(self, label, data):
        img = generate_qr_image(data, size=160)
        label.configure(image=img)
        label.image = img

    def toggle_test_mode(self):
        global SERVER_TEST_MODE
        SERVER_TEST_MODE = self.test_mode.get()
        if SERVER_TEST_MODE:
            self.chk_test.configure(text="Testing Mode ON", bootstyle="danger-round-toggle")
            self.cb_test_date.configure(state="normal")
            self._append_log(self.log_network, f"[WARNING] Testing Mode ON. Server date overridden to {self.test_date.get()}.")
        else:
            self.chk_test.configure(text="Testing Mode OFF", bootstyle="warning-round-toggle")
            self.cb_test_date.configure(state=DISABLED)
            self._append_log(self.log_network, "[INFO] Testing Mode OFF. Real system date restored.")
        self.refresh_stats()

    def on_test_date_changed(self):
        global SERVER_TEST_DATE
        SERVER_TEST_DATE = self.test_date.get()
        self._append_log(self.log_network, f"[WARNING] Test date updated globally to: {SERVER_TEST_DATE}")
        self.refresh_stats()

    def refresh_stats(self):
        global SERVER_TEST_MODE, SERVER_TEST_DATE, ACTIVE_DEVICES
        
        current_time = time.time()
        active_ips = [ip for ip, data in ACTIVE_DEVICES.items() if current_time - data['last_seen'] < 15]
        
        self.stat_vars["online_scanners"].configure(text=str(len(active_ips)))
        
        device_strings = []
        for ip in active_ips:
            name_str = ACTIVE_DEVICES[ip]['name']
            device_strings.append(f"📱 {name_str} ({ip})")
            
        if device_strings: self.lbl_devices.configure(text="  |  ".join(device_strings), bootstyle=SUCCESS)
        else: self.lbl_devices.configure(text="No external devices connected. Awaiting connections...", bootstyle=WARNING)

        if self.SessionMySQL: self.lbl_stat_mysql.configure(text="● MYSQL: LIVE", bootstyle=SUCCESS)
        else: self.lbl_stat_mysql.configure(text="● MYSQL: OFFLINE", bootstyle=DANGER)

        if self.SessionSQLite: self.lbl_stat_sqlite.configure(text="● SQLITE: MIRROR ACTIVE", bootstyle=SUCCESS)
        else: self.lbl_stat_sqlite.configure(text="● SQLITE: FAULT", bootstyle=DANGER)

        if not self.SessionMySQL: return
        
        mysql_session = self.SessionMySQL()
        sqlite_session = self.SessionSQLite() if self.SessionSQLite else None
        
        try:
            total_mysql = mysql_session.query(Attendee).count()
            kiosk_regs = mysql_session.query(OfflineKioskAttendee).count()
            total_sqlite = sqlite_session.query(Attendee).count() if sqlite_session else 0
            
            # FIX: Database queries updated to search for the proper canonical JSON keys
            chk_30 = mysql_session.query(Attendee).filter(Attendee.checkin_history.like('%"30 August"%')).count()
            chk_31 = mysql_session.query(Attendee).filter(Attendee.checkin_history.like('%"31 August"%')).count()
            chk_01 = mysql_session.query(Attendee).filter(Attendee.checkin_history.like('%"1 September"%')).count()
            
            today_str = SERVER_TEST_DATE if SERVER_TEST_MODE else datetime.now().strftime('%Y-%m-%d')
            
            if today_str == "2026-08-30": 
                chk_today = chk_30
            elif today_str == "2026-08-31": 
                chk_today = chk_31
            elif today_str == "2026-09-01": 
                chk_today = chk_01
            else:
                chk_today = 0 # Out of event dates bounds

            total_checkins = chk_30 + chk_31 + chk_01

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

    def start_flask(self):
        self.btn_start_flask.configure(state=DISABLED)
        self.btn_stop_flask.configure(state=NORMAL)
        self.btn_start_cf.configure(state=NORMAL)
        self.flask_url = f"https://{self.local_ip}:{FLASK_PORT}"
        self.update_qr(self.lbl_flask_qr, self.flask_url)
        self.lbl_flask_link.configure(text=self.flask_url, foreground="#4D9CE6")
        self._append_log(self.log_flask, f"[{datetime.now().strftime('%H:%M:%S')}] Booting secure HTTPS API Engine...")
        self.flask_thread = FlaskServerThread(app, '0.0.0.0', FLASK_PORT)
        self.flask_thread.start()
        self._append_log(self.log_flask, f"[SYSTEM] Secure API Engine listening natively on {self.flask_url}")
        
    def stop_flask(self):
        if self.btn_stop_cf['state'] == NORMAL: self.stop_cf()
        self.btn_stop_flask.configure(state=DISABLED)
        self.btn_start_flask.configure(state=NORMAL)
        self.btn_start_cf.configure(state=DISABLED)
        self.update_qr(self.lbl_flask_qr, "OFFLINE")
        self.lbl_flask_link.configure(text="Server Offline", foreground="gray")
        if self.flask_thread:
            self.flask_thread.shutdown()
            self.flask_thread.join()
            self.flask_thread = None
        self._append_log(self.log_flask, f"[{datetime.now().strftime('%H:%M:%S')}] API Engine terminated gracefully.")

    def start_cf(self):
        self.btn_start_cf.configure(state=DISABLED)
        self.btn_stop_cf.configure(state=NORMAL)
        self.lbl_stat_cf.configure(text="● Cloudflare: CONNECTING", bootstyle=WARNING)
        self._append_log(self.log_cf, f"[{datetime.now().strftime('%H:%M:%S')}] Requesting secure tunnel to Edge network...")

        def _run_cf():
            try:
                cmd = ["cloudflared", "tunnel", "--url", f"https://127.0.0.1:{FLASK_PORT}", "--no-tls-verify"]
                creationflags = subprocess.CREATE_NO_WINDOW if platform.system() == "Windows" else 0
                
                self.cf_process = subprocess.Popen(
                    cmd, stdout=subprocess.PIPE, stderr=subprocess.STDOUT, text=True, creationflags=creationflags
                )

                url_found = False
                for line in self.cf_process.stdout:
                    self._append_log(self.log_cf, line.strip())
                    if not url_found:
                        match = re.search(r"https://[a-zA-Z0-9-]+\.trycloudflare\.com", line)
                        if match:
                            self.cloudflare_url = match.group(0)
                            url_found = True
                            
                            self.gui_queue.put(lambda: self.update_qr(self.lbl_cf_qr, self.cloudflare_url))
                            self.gui_queue.put(lambda: self.lbl_cf_link.configure(text=self.cloudflare_url, foreground="#4D9CE6"))
                            self.gui_queue.put(lambda: self.lbl_stat_cf.configure(text="● Cloudflare: LIVE", bootstyle=SUCCESS))
                            self._append_log(self.log_cf, f"[SUCCESS] Traffic successfully bridged to: {self.cloudflare_url}")
            except FileNotFoundError:
                self.gui_queue.put(self.stop_cf)
                self._append_log(self.log_cf, "[ERROR] 'cloudflared' not found. Is it installed and in your system PATH?")
            except Exception as e:
                self.gui_queue.put(self.stop_cf)
                self._append_log(self.log_cf, f"[ERROR] Cloudflare Tunnel failed: {str(e)}")

        threading.Thread(target=_run_cf, daemon=True).start()
        
    def stop_cf(self):
        self.btn_stop_cf.configure(state=DISABLED)
        if self.btn_stop_flask['state'] == NORMAL: self.btn_start_cf.configure(state=NORMAL)
        self.lbl_stat_cf.configure(text="● Cloudflare: OFFLINE", bootstyle=SECONDARY)
        
        if hasattr(self, 'cf_process') and self.cf_process:
            try:
                self.cf_process.terminate()
                self.cf_process.wait(timeout=2)
            except Exception: pass
            finally: self.cf_process = None

        self.cloudflare_url = "Offline"
        self.update_qr(self.lbl_cf_qr, "OFFLINE")
        self.lbl_cf_link.configure(text="Tunnel Offline", foreground="gray")
        self._append_log(self.log_cf, f"[{datetime.now().strftime('%H:%M:%S')}] Tunnel connection closed.")


if __name__ == "__main__":
    app_window = ServerHub()
    app_window.mainloop()