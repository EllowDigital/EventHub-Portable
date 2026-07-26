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
import ipaddress
import requests
from datetime import datetime, timezone, timedelta

import ttkbootstrap as ttk
from ttkbootstrap.constants import *
from ttkbootstrap.widgets.scrolled import ScrolledText
import qrcode
from PIL import Image, ImageTk
import webbrowser

from flask import Flask, render_template, request, jsonify, Response
from waitress import create_server  # 🚀 Waitress WSGI Engine (plain-HTTP port 5000 only)

# 🔒 Cheroot WSGI Engine — real production server, replaces the old werkzeug
# ad-hoc HTTPS server. Pure Python (no compiler needed, same portability
# story as PyMySQL), with NATIVE SSL support and a real thread pool + connection
# backlog — both of which werkzeug's dev server lacked. That gap is what capped
# concurrent HTTPS devices at ~2.
try:
    from cheroot import wsgi as cheroot_wsgi
    from cheroot.ssl.builtin import BuiltinSSLAdapter
except ImportError:
    cheroot_wsgi = None
    BuiltinSSLAdapter = None

# Generates a persistent, LAN-IP-aware self-signed certificate once instead of
# a throwaway one on every start (werkzeug's ssl_context='adhoc' regenerated a
# new cert every run, forcing every device to re-accept the "insecure site"
# browser warning after every restart).
try:
    from cryptography import x509
    from cryptography.hazmat.primitives import hashes, serialization
    from cryptography.hazmat.primitives.asymmetric import rsa
    from cryptography.x509.oid import NameOID
    CRYPTOGRAPHY_AVAILABLE = True
except ImportError:
    CRYPTOGRAPHY_AVAILABLE = False

# 🛡️ Force SQLAlchemy to detect JSON column updates
from sqlalchemy.orm.attributes import flag_modified

# Windows DPI Awareness & Anti-Sleep Mode
if platform.system() == "Windows":
    try:
        from ctypes import windll
        # 1. Make text crisp on high-res displays
        windll.shcore.SetProcessDpiAwareness(1)
        # 2. Prevent Windows from going to sleep while server is running
        # 0x80000000 (ES_CONTINUOUS) | 0x00000001 (ES_SYSTEM_REQUIRED)
        windll.kernel32.SetThreadExecutionState(0x80000000 | 0x00000001)
    except Exception as e:
        print(f"Windows specific configuration failed: {e}")

# Import models dynamically based on context
try:
    from app.schema import Attendee, OfflineKioskAttendee, get_database_sessions
except ModuleNotFoundError:
    from schema import Attendee, OfflineKioskAttendee, get_database_sessions

# ==============================================================================
# CONFIGURATION & GLOBAL CACHE
# ==============================================================================
BASE_DIR = os.path.dirname(os.path.abspath(__file__))
LOG_DIR = os.path.join(BASE_DIR, 'logs')
os.makedirs(LOG_DIR, exist_ok=True)

HTTP_PORT = 5000   # Fast, unencrypted Local LAN traffic & Cloudflare tunnel target
HTTPS_PORT = 5001  # Secure Local LAN traffic (allows iOS Camera Access natively)
CERT_DIR = os.path.join(BASE_DIR, 'config', 'certs')

template_dir = os.path.join(BASE_DIR, 'templates')
static_dir = os.path.join(BASE_DIR, 'static')
app = Flask(__name__, template_folder=template_dir, static_folder=static_dir)

gui_log_callback = None
SERVER_TEST_MODE = False
SERVER_TEST_DATE = "2026-08-30"

ACTIVE_DEVICES = {}
SCAN_CLIENTS = []  

# 📱 Single source of truth for "is this device still connected?" — used by both
# the GUI panel and the /api/network-data endpoint (previously 15s in one place
# and 30s in the other, which made the two views disagree with each other).
DEVICE_ONLINE_WINDOW = 20  # seconds since last heartbeat before a device is considered offline

# 🚀 PERFORMANCE FIX: Global DB Cache prevents connection exhaustion
DB_SESSIONS_CACHE = None

def get_cached_sessions():
    global DB_SESSIONS_CACHE
    if DB_SESSIONS_CACHE is None:
        DB_SESSIONS_CACHE = get_database_sessions()
    return DB_SESSIONS_CACHE

# 📡 TELEMETRY ENGINE GLOBALS
NETWORK_LATENCY = {
    "local_ms": 0,
    "cloud_ms": 0,
    "local_status": "OFFLINE",
    "cloud_status": "OFFLINE"
}

# ==============================================================================
# 🔒 PERSISTENT SSL CERTIFICATE (replaces werkzeug's ssl_context='adhoc')
# ==============================================================================
def _write_self_signed_cert(cert_path, key_path, local_ip):
    """Generates one RSA key + self-signed cert covering localhost, 127.0.0.1,
    and the current LAN IP, valid ~2 years."""
    key = rsa.generate_private_key(public_exponent=65537, key_size=2048)
    subject = issuer = x509.Name([x509.NameAttribute(NameOID.COMMON_NAME, "TDE-EventHub-Local")])

    san_entries = [x509.DNSName("localhost"), x509.IPAddress(ipaddress.ip_address("127.0.0.1"))]
    try:
        san_entries.append(x509.IPAddress(ipaddress.ip_address(local_ip)))
    except ValueError:
        pass

    now = datetime.now(timezone.utc)
    cert = (
        x509.CertificateBuilder()
        .subject_name(subject)
        .issuer_name(issuer)
        .public_key(key.public_key())
        .serial_number(x509.random_serial_number())
        .not_valid_before(now)
        .not_valid_after(now + timedelta(days=730))
        .add_extension(x509.SubjectAlternativeName(san_entries), critical=False)
        .sign(key, hashes.SHA256())
    )

    with open(key_path, "wb") as f:
        f.write(key.private_bytes(
            encoding=serialization.Encoding.PEM,
            format=serialization.PrivateFormat.TraditionalOpenSSL,
            encryption_algorithm=serialization.NoEncryption()
        ))
    with open(cert_path, "wb") as f:
        f.write(cert.public_bytes(serialization.Encoding.PEM))


def ensure_ssl_certificate(local_ip):
    """Reuses the existing HTTPS certificate if it already covers this LAN IP,
    otherwise (first run, or the IP changed because the laptop is on a new
    network/venue) generates a fresh one. Reusing the same cert across restarts
    means operators/devices that already clicked through the browser's
    "insecure site" warning once won't be prompted again."""
    if not CRYPTOGRAPHY_AVAILABLE:
        raise RuntimeError(
            "The 'cryptography' package is required to generate the HTTPS certificate. "
            "Install it with:  pip install cryptography"
        )

    os.makedirs(CERT_DIR, exist_ok=True)
    cert_path = os.path.join(CERT_DIR, 'hub_cert.pem')
    key_path = os.path.join(CERT_DIR, 'hub_key.pem')
    ip_marker_path = os.path.join(CERT_DIR, 'hub_cert_ip.txt')

    reuse_existing = False
    if os.path.exists(cert_path) and os.path.exists(key_path) and os.path.exists(ip_marker_path):
        try:
            with open(ip_marker_path, 'r') as f:
                reuse_existing = f.read().strip() == local_ip
        except Exception:
            reuse_existing = False

    if not reuse_existing:
        print(f"[SSL] Generating new HTTPS certificate for {local_ip} (first run, or IP changed)...")
        _write_self_signed_cert(cert_path, key_path, local_ip)
        with open(ip_marker_path, 'w') as f:
            f.write(local_ip)
    else:
        print(f"[SSL] Reusing existing HTTPS certificate for {local_ip}.")

    return cert_path, key_path

# ==============================================================================
# FLASK MIDDLEWARE & EVENT BROADCASTER
# ==============================================================================
@app.after_request
def log_request(response):
    if request.path in ['/api/status', '/api/network-data', '/api/stream-scans'] or request.path.startswith('/static') or request.path.startswith('/favicon.ico'):
        return response  
        
    return response

def log_event_clean(action_type, device_name, details, status_code):
    """Formats clean, human-readable operations logs for the GUI."""
    time_str = datetime.now().strftime('%H:%M:%S')
    
    if action_type == "REGISTER":
        icon = "✅" if status_code == 200 else "❌"
        msg = f"[{time_str}] {icon} [{device_name}] Reg: {details} — Status: {status_code}"
    elif action_type == "CHECKIN":
        icon = "🎫" if status_code == 200 else "⛔"
        msg = f"[{time_str}] {icon} [{device_name}] Chk: {details} — Status: {status_code}"
    else:
        msg = f"[{time_str}] 🌐 [{device_name}] {action_type} — Status: {status_code}"
        
    if gui_log_callback:
        gui_log_callback(msg)

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
    
    for q in list(SCAN_CLIENTS):
        try:
            q.put_nowait(event)
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
        q = queue.Queue(maxsize=50)
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
    data = request.json or {}
    
    # Accept multiple possible input keys from various scanner scripts
    identifier = str(data.get('attendee_id', data.get('qr_data', data.get('id', '')))).strip()
    search_type = data.get('search_type', 'id')
    device_name = data.get('device_name', f"Scanner ({request.remote_addr})")
    
    iso_timestamp = datetime.now(timezone.utc).strftime('%Y-%m-%dT%H:%M:%S.000Z')
    
    if not identifier:
        msg = "No ID or Phone provided"
        log_event_clean("CHECKIN", device_name, msg, 400)
        broadcast_scan(None, "ERROR", msg, device_name, iso_timestamp)
        return jsonify({"status": "error", "message": msg}), 400

    sessions = get_cached_sessions()
    session = sessions.get('mysql')()
    
    try:
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
            msg = f"Not found: {identifier}"
            log_event_clean("CHECKIN", device_name, msg, 404)
            broadcast_scan(None, "ERROR", msg, device_name, iso_timestamp)
            return jsonify({"status": "error", "message": msg}), 404

        history = attendee.checkin_history
        if isinstance(history, str):
            try: history = json.loads(history)
            except: history = {}
        if not isinstance(history, dict): history = {}

        current_date_str = SERVER_TEST_DATE if SERVER_TEST_MODE else datetime.now(timezone.utc).strftime('%Y-%m-%d')
        date_map = {"2026-08-30": "30 August", "2026-08-31": "31 August", "2026-09-01": "1 September"}
        
        if current_date_str not in date_map:
            msg = f"Date invalid: {current_date_str}"
            log_event_clean("CHECKIN", device_name, msg, 400)
            broadcast_scan(attendee, "ERROR", msg, device_name, iso_timestamp)
            return jsonify({"status": "error", "message": msg}), 400
            
        today_key = date_map[current_date_str]
        att_days = attendee.attendance_days or []
        if isinstance(att_days, str):
            try: att_days = json.loads(att_days)
            except: att_days = []
            
        if today_key not in att_days:
            msg = f"Access Denied (No pass for {today_key})"
            log_event_clean("CHECKIN", device_name, msg, 403)
            broadcast_scan(attendee, "ERROR", msg, device_name, iso_timestamp)
            return jsonify({"status": "error", "message": msg}), 403

        if today_key in history:
            msg = f"Already checked in: {attendee.full_name}"
            log_event_clean("CHECKIN", device_name, msg, 400)
            broadcast_scan(attendee, "DUPLICATE", msg, device_name, iso_timestamp)
            return jsonify({"status": "error", "message": msg}), 400

        history[today_key] = {
            "timestamp": iso_timestamp,
            "source": "offline_hub",
            "device": device_name,
            "date_code": current_date_str,
            "display_date": today_key
        }

        attendee.checkin_history = history 
        flag_modified(attendee, "checkin_history")
        
        attendee.needs_cloud_sync = True
        attendee.needs_sheet_sync = True
        attendee.needs_local_sync = False 
        attendee.local_modified = True
        
        session.commit()
        
        success_msg = f"{attendee.full_name} ({attendee.attendee_id})"
        log_event_clean("CHECKIN", device_name, success_msg, 200)
        broadcast_scan(attendee, "SUCCESS", success_msg, device_name, iso_timestamp)
            
        return jsonify({"status": "success", "message": success_msg, "time": iso_timestamp}), 200

    except Exception as e:
        session.rollback()
        log_event_clean("CHECKIN", device_name, str(e), 500)
        return jsonify({"status": "error", "message": str(e)}), 500
    finally:
        session.close()

@app.route('/api/register', methods=['POST'])
def process_registration():
    global SERVER_TEST_MODE, SERVER_TEST_DATE
    data = request.json or {}
    sessions = get_cached_sessions()
    session = sessions.get('mysql')()
    
    mobile_number = data.get('mobile', '').strip()
    req_ip = request.remote_addr
    req_os = request.user_agent.platform or "Unknown"
    device_label = data.get('device_name', f"Kiosk ({req_os.capitalize()} - {req_ip})")

    iso_timestamp = datetime.now(timezone.utc).strftime('%Y-%m-%dT%H:%M:%S.000Z')

    try:
        existing_main = session.query(Attendee).filter_by(mobile=mobile_number).with_for_update().first()
        if existing_main: 
            log_event_clean("REGISTER", device_label, f"{data.get('full_name')} (Already Exists)", 200)
            return jsonify({"status": "already_registered", "message": "Already registered.", "attendee_id": existing_main.attendee_id}), 200

        existing_kiosk = session.query(OfflineKioskAttendee).filter_by(mobile=mobile_number).with_for_update().first()
        if existing_kiosk: 
            log_event_clean("REGISTER", device_label, f"{data.get('full_name')} (Already Exists)", 200)
            return jsonify({"status": "already_registered", "message": "Already registered.", "attendee_id": existing_kiosk.attendee_id}), 200

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
        
        log_event_clean("REGISTER", device_label, f"{data.get('full_name')} ({new_attendee_id})", 200)
        return jsonify({"status": "success", "message": "Saved successfully.", "attendee_id": new_attendee_id}), 200

    except Exception as e:
        session.rollback()
        log_event_clean("REGISTER", device_label, str(e), 500)
        return jsonify({"status": "error", "message": str(e)}), 500
    finally:
        session.close()

@app.route('/api/check_mobile', methods=['GET'])
def check_mobile():
    mobile_number = request.args.get('mobile', '').strip()
    
    if not mobile_number:
        return jsonify({"status": "error", "message": "Mobile number required"}), 400
        
    sessions = get_cached_sessions()
    session = sessions.get('mysql')()
    
    try:
        # 1. Check the main Attendee table first
        existing_main = session.query(Attendee).filter_by(mobile=mobile_number).first()
        if existing_main:
            return jsonify({
                "status": "already_registered",
                "attendee_id": existing_main.attendee_id
            }), 200

        # 2. Check the local OfflineKioskAttendee table
        existing_kiosk = session.query(OfflineKioskAttendee).filter_by(mobile=mobile_number).first()
        if existing_kiosk:
            return jsonify({
                "status": "already_registered",
                "attendee_id": existing_kiosk.attendee_id
            }), 200

        # 3. If not found in either table, return not_found
        return jsonify({"status": "not_found"}), 200

    except Exception as e:
        return jsonify({"status": "error", "message": str(e)}), 500
    finally:
        session.close()

@app.route('/api/network-data', methods=['GET'])
def get_network_data():
    current_time = time.time()
    active_devices = {}
    for ip, data in list(ACTIVE_DEVICES.items()):
        if current_time - data['last_seen'] < DEVICE_ONLINE_WINDOW: active_devices[ip] = data
        else: del ACTIVE_DEVICES[ip]

    sessions = get_cached_sessions()
    session = sessions.get('mysql')()
    global_stats = {"total_scans": 0, "total_registrations": 0, "today_scans": 0}
    
    try:
        global_stats["total_registrations"] = session.query(OfflineKioskAttendee).count()
        today_date = SERVER_TEST_DATE if SERVER_TEST_MODE else datetime.now(timezone.utc).strftime('%Y-%m-%d')
        
        chk_30 = session.query(Attendee).filter(Attendee.checkin_history.like('%"30 August"%')).count()
        chk_31 = session.query(Attendee).filter(Attendee.checkin_history.like('%"31 August"%')).count()
        chk_01 = session.query(Attendee).filter(Attendee.checkin_history.like('%"1 September"%')).count()
        
        global_stats["total_scans"] = chk_30 + chk_31 + chk_01
        if today_date == "2026-08-30": global_stats["today_scans"] = chk_30
        elif today_date == "2026-08-31": global_stats["today_scans"] = chk_31
        elif today_date == "2026-09-01": global_stats["today_scans"] = chk_01
    except Exception: 
        pass
    finally: 
        session.close()

    return jsonify({"active_devices": active_devices, "global_stats": global_stats}), 200

# ==============================================================================
# MULTI-THREADED WSGI ENGINE THREADS
# ==============================================================================
class WaitressHttpThread(threading.Thread):
    """Runs Waitress WSGI server optimized for 30+ concurrent low-latency LAN connections"""
    def __init__(self, app, host, port):
        super().__init__()
        self.server = create_server(app, host=host, port=port, threads=30)
        self.ctx = app.app_context()
        self.ctx.push()

    def run(self): 
        self.server.run()
        
    def shutdown(self): 
        self.server.close()

class HttpsFlaskThread(threading.Thread):
    """Runs a real production HTTPS server (cheroot) for mobile browsers that need
    a secure context for native camera access (QR/barcode scanning).

    This replaces werkzeug's ssl_context='adhoc' dev server, which could only
    reliably hold ~2 concurrent TLS connections before refusing/hanging on the
    rest. Cheroot uses a real thread pool (same threading model as the Waitress
    engine above — no exotic event-loop mixing) with a proper connection
    backlog, so 20+ simultaneous phones connecting at once is no problem. It
    also reuses the SAME persistent certificate across restarts instead of a
    new one every time.
    """
    def __init__(self, app, host, port, numthreads=60):
        super().__init__()
        if cheroot_wsgi is None:
            raise RuntimeError(
                "The 'cheroot' package is required for the HTTPS engine. "
                "Install it with:  pip install cheroot"
            )

        cert_path, key_path = ensure_ssl_certificate(get_local_ip())

        self.ctx = app.app_context()
        self.ctx.push()
        self.server = cheroot_wsgi.Server(
            bind_addr=(host, port),
            wsgi_app=app,
            numthreads=numthreads,      # real thread pool -> handles 20+ devices concurrently
            request_queue_size=128,     # OS-level backlog (werkzeug's default here was only 5)
        )
        self.server.ssl_adapter = BuiltinSSLAdapter(certificate=cert_path, private_key=key_path)

    def run(self):
        self.server.start()

    def shutdown(self):
        self.server.stop()

def get_local_ip():
    try:
        s = socket.socket(socket.AF_INET, socket.SOCK_DGRAM)
        s.connect(("10.255.255.255", 1))
        ip = s.getsockname()[0]
        s.close()
        return ip
    except Exception: 
        try: return socket.gethostbyname(socket.gethostname())
        except: return "127.0.0.1"


# ==============================================================================
# SPEEDOMETER HUD WINDOW
# ==============================================================================
class NetworkTelemetryWindow(ttk.Toplevel):
    def __init__(self, parent, hub_instance):
        super().__init__(parent)
        self.title("Live Network Speedometers")
        self.geometry("750x420")
        self.resizable(False, False)
        self.hub = hub_instance
        self.attributes('-topmost', True)
        
        self.update_idletasks()
        x = parent.winfo_x() + (parent.winfo_width() // 2) - (self.winfo_width() // 2)
        y = parent.winfo_y() + (parent.winfo_height() // 2) - (self.winfo_height() // 2)
        self.geometry(f"+{x}+{y}")

        self.build_ui()
        self.refresh_meters()

    def build_ui(self):
        main_frame = ttk.Frame(self, padding=25)
        main_frame.pack(fill=BOTH, expand=True)

        ttk.Label(main_frame, text="📡 Real-Time API Latency", font="-size 16 -weight bold", bootstyle=PRIMARY).pack(anchor=CENTER, pady=(0, 5))
        grid = ttk.Frame(main_frame)
        grid.pack(fill=BOTH, expand=True)
        
        local_card = ttk.Labelframe(grid, text=" Local Waitress Engine (Wi-Fi/LAN) ", padding=15)
        local_card.pack(side=LEFT, fill=BOTH, expand=True, padx=10)
        
        self.meter_local = ttk.Meter(local_card, metersize=200, padding=10, amounttotal=1000, amountused=0, metertype="semi", subtext="PING ms", interactive=False, stripethickness=10, meterthickness=15, bootstyle=SUCCESS)
        self.meter_local.pack(pady=(10,0))
        self.lbl_local_status = ttk.Label(local_card, text="Status: OFFLINE", font="-weight bold", bootstyle=SECONDARY)
        self.lbl_local_status.pack(pady=10)

        cloud_card = ttk.Labelframe(grid, text=" Cloudflare Tunnel (Internet WAN) ", padding=15)
        cloud_card.pack(side=LEFT, fill=BOTH, expand=True, padx=10)
        
        self.meter_cloud = ttk.Meter(cloud_card, metersize=200, padding=10, amounttotal=1000, amountused=0, metertype="semi", subtext="PING ms", interactive=False, stripethickness=10, meterthickness=15, bootstyle=SUCCESS)
        self.meter_cloud.pack(pady=(10,0))
        self.lbl_cloud_status = ttk.Label(cloud_card, text="Status: OFFLINE", font="-weight bold", bootstyle=SECONDARY)
        self.lbl_cloud_status.pack(pady=10)
        
        ttk.Button(main_frame, text="Close Window", bootstyle=SECONDARY, command=self.destroy).pack(pady=(20,0))

    def refresh_meters(self):
        if not self.winfo_exists(): return
        global NETWORK_LATENCY
        
        loc_ms = NETWORK_LATENCY["local_ms"]
        self.meter_local.configure(amountused=min(loc_ms, 1000))
        if NETWORK_LATENCY["local_status"] == "ONLINE":
            self.meter_local.configure(bootstyle=SUCCESS if loc_ms < 150 else WARNING)
            self.lbl_local_status.configure(text="Status: LIVE & CONNECTED", bootstyle=SUCCESS)
        else:
            self.meter_local.configure(amountused=0, bootstyle=SECONDARY)
            self.lbl_local_status.configure(text="Status: SERVER OFF", bootstyle=DANGER)

        cf_ms = NETWORK_LATENCY["cloud_ms"]
        self.meter_cloud.configure(amountused=min(cf_ms, 1000))
        if NETWORK_LATENCY["cloud_status"] == "ONLINE":
            self.meter_cloud.configure(bootstyle=SUCCESS if cf_ms < 300 else WARNING)
            self.lbl_cloud_status.configure(text="Status: SECURE TUNNEL ACTIVE", bootstyle=SUCCESS)
        else:
            self.meter_cloud.configure(amountused=0, bootstyle=SECONDARY)
            self.lbl_cloud_status.configure(text="Status: TUNNEL OFFLINE", bootstyle=DANGER)

        self.after(1000, self.refresh_meters)


# ==============================================================================
# MAIN SERVER HUB GUI
# ==============================================================================
class ServerHub(ttk.Window):
    def __init__(self):
        super().__init__(themename="darkly", title="TDE UP 2026 — Event Hub V2.1 (Multi-Threaded)")
        self.geometry("1600x950")
        self.minsize(1000, 700)
        
        self.local_ip = get_local_ip()
        self.http_url = f"http://{self.local_ip}:{HTTP_PORT}"
        self.https_url = f"https://{self.local_ip}:{HTTPS_PORT}"
        self.cloudflare_url = "Offline"
        
        self.SessionMySQL = None
        self.SessionSQLite = None
        self.connect_db()

        self.http_thread = None
        self.https_thread = None
        self.cf_process = None 
        
        self.gui_queue = queue.Queue()
        
        global gui_log_callback
        gui_log_callback = self.log_flask_event
        
        self.build_ui()
        self.process_gui_queue() 
        self.refresh_stats()
        
        threading.Thread(target=self.network_ping_daemon, daemon=True).start()

    def connect_db(self):
        try:
            sessions = get_cached_sessions()
            self.SessionMySQL = sessions.get('mysql')
            self.SessionSQLite = sessions.get('sqlite')
        except Exception as e:
            logging.error(f"Database Connection Failed: {e}")

    def network_ping_daemon(self):
        global NETWORK_LATENCY
        
        # Add a fake User-Agent so Cloudflare doesn't block the ping as a bot
        headers = {
            "User-Agent": "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/120.0.0.0 Safari/537.36"
        }

        while True:
            # --- Local Ping ---
            start_local = time.time()
            try:
                requests.get(f"http://127.0.0.1:{HTTP_PORT}/api/status", timeout=2)
                NETWORK_LATENCY["local_ms"] = int((time.time() - start_local) * 1000)
                NETWORK_LATENCY["local_status"] = "ONLINE"
            except:
                NETWORK_LATENCY["local_ms"] = 0
                NETWORK_LATENCY["local_status"] = "OFFLINE"

            # --- Cloudflare Ping ---
            if self.cloudflare_url and self.cloudflare_url != "Offline":
                start_cf = time.time()
                try:
                    # Added headers and increased timeout to 7 seconds
                    requests.get(f"{self.cloudflare_url}/api/status", headers=headers, timeout=7, verify=False)
                    NETWORK_LATENCY["cloud_ms"] = int((time.time() - start_cf) * 1000)
                    NETWORK_LATENCY["cloud_status"] = "ONLINE"
                except:
                    NETWORK_LATENCY["cloud_ms"] = 0
                    NETWORK_LATENCY["cloud_status"] = "OFFLINE"
            else:
                NETWORK_LATENCY["cloud_ms"] = 0
                NETWORK_LATENCY["cloud_status"] = "OFFLINE"

            time.sleep(1.5)
            
    def process_gui_queue(self):
        while not self.gui_queue.empty():
            try:
                task = self.gui_queue.get_nowait()
                task() 
            except queue.Empty:
                break
        self.after(30, self.process_gui_queue)

    def _append_log(self, scrolled_text_widget, message):
        def append():
            scrolled_text_widget.text.configure(state=NORMAL)
            scrolled_text_widget.text.insert(END, message + "\n")
            scrolled_text_widget.text.see(END)
            scrolled_text_widget.text.configure(state=DISABLED)
        self.gui_queue.put(append)

    def log_flask_event(self, message):
        # Routes logs directly into the Flask Traffic Log box cleanly
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

    def open_telemetry_window(self):
        NetworkTelemetryWindow(self, self)

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
        self.main_frame.bind("<Configure>", lambda e: self.canvas.configure(scrollregion=self.canvas.bbox("all")))
        self.canvas.bind("<Configure>", lambda e: self.canvas.itemconfig(self.canvas_window, width=max(e.width, 1200)))

        # --- SIDEBAR ---
        sidebar = ttk.Frame(self.main_frame, width=320, padding=20)
        sidebar.pack(side=LEFT, fill=Y)
        sidebar.pack_propagate(False)

        ttk.Label(sidebar, text="NETWORK & ROUTING", font="-size 13 -weight bold", bootstyle=INFO).pack(pady=(0, 15), anchor=W)

        flask_frame = ttk.Labelframe(sidebar, text=" 🌐 Waitress High-Speed Engine ", padding=15)
        flask_frame.pack(fill=X, pady=5)
        
        self.btn_start_flask = ttk.Button(flask_frame, text="▶ Start Engine", bootstyle=SUCCESS, command=self.start_flask)
        self.btn_start_flask.pack(fill=X, pady=3)
        self.btn_stop_flask = ttk.Button(flask_frame, text="⏹ Stop Engine", bootstyle=DANGER, state=DISABLED, command=self.stop_flask)
        self.btn_stop_flask.pack(fill=X, pady=3)
        
        ttk.Label(flask_frame, text="Network QR (iOS HTTPS):", font="-size 9 -weight bold", foreground="#888").pack(pady=(15, 5))
        self.lbl_flask_qr = ttk.Label(flask_frame)
        self.lbl_flask_qr.pack()
        self.update_qr(self.lbl_flask_qr, "OFFLINE")
        
        self.lbl_flask_link = ttk.Label(flask_frame, text="HTTPS Offline", font="-size 9", foreground="gray", cursor="hand2")
        self.lbl_flask_link.pack(pady=8)
        self.lbl_flask_link.bind("<Button-1>", lambda e: self.open_browser(self.https_url) if self.https_thread else None)
        
        flask_btn_row = ttk.Frame(flask_frame)
        flask_btn_row.pack(fill=X, pady=(5, 5))
        ttk.Button(flask_btn_row, text="Copy HTTPS", bootstyle="outline-light", command=lambda: self.copy_to_clipboard(self.https_url)).pack(side=LEFT, expand=True, fill=X, padx=2)
        ttk.Button(flask_btn_row, text="Copy HTTP", bootstyle="outline-info", command=lambda: self.copy_to_clipboard(self.http_url)).pack(side=LEFT, expand=True, fill=X, padx=2)

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
        
        actions_f = ttk.Frame(header)
        actions_f.pack(side=LEFT, padx=20)
        ttk.Button(actions_f, text="⟳ Refresh Data", bootstyle="outline-light", command=self.refresh_stats).pack(side=LEFT, padx=5)
        ttk.Button(actions_f, text="🎛️ Network Speedometers", bootstyle="outline-info", command=self.open_telemetry_window).pack(side=LEFT, padx=5)
        
        status_frame = ttk.Frame(header)
        status_frame.pack(side=RIGHT)
        
        self.lbl_stat_cf = ttk.Label(status_frame, text="● Cloudflare: OFFLINE", bootstyle=SECONDARY, font="-weight bold")
        self.lbl_stat_cf.pack(side=LEFT, padx=10)
        self.lbl_stat_sqlite = ttk.Label(status_frame, text="● SQLITE: CHECKING", bootstyle=INFO, font="-weight bold")
        self.lbl_stat_sqlite.pack(side=LEFT, padx=10)
        self.lbl_stat_mysql = ttk.Label(status_frame, text="● MYSQL: CHECKING", bootstyle=INFO, font="-weight bold")
        self.lbl_stat_mysql.pack(side=LEFT, padx=10)

        ttk.Label(content, text="📡 ACTIVE CONNECTED DEVICES", font="-size 11 -weight bold", bootstyle=INFO).pack(anchor=W, pady=(5, 5))
        devices_frame = ttk.Frame(content, borderwidth=1, relief="solid", bootstyle="dark")
        devices_frame.pack(fill=X, pady=(0, 20))

        self.tree_devices = ttk.Treeview(
            devices_frame,
            columns=("name", "ip", "last_seen", "status"),
            show="headings",
            height=6,
            bootstyle=INFO,
        )
        self.tree_devices.heading("name", text="Device Name")
        self.tree_devices.heading("ip", text="IP Address")
        self.tree_devices.heading("last_seen", text="Last Seen")
        self.tree_devices.heading("status", text="Status")
        self.tree_devices.column("name", width=300, anchor=W)
        self.tree_devices.column("ip", width=150, anchor=W)
        self.tree_devices.column("last_seen", width=120, anchor=CENTER)
        self.tree_devices.column("status", width=110, anchor=CENTER)
        self.tree_devices.pack(fill=X, padx=2, pady=2)
        self.tree_devices.tag_configure("online", foreground="#3fd66f")
        self.tree_devices.tag_configure("empty", foreground="#888")

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
        self.log_flask = self._create_log_box(logs_frame, "Live Operator Activity & API Logs")
        self.log_cf = self._create_log_box(logs_frame, "Cloudflare Tunnel Status")

        footer = ttk.Frame(content)
        footer.pack(fill=X, pady=(15, 0))
        ttk.Label(footer, text="Engineered for Event Resilience • Powered by EllowDigital", font="-size 9", foreground="#666").pack(side=RIGHT)

        self._append_log(self.log_network, f"System Boot: {datetime.now().strftime('%Y-%m-%d %H:%M:%S')}")
        self._append_log(self.log_network, f"Local Network IP Address Detected: {self.local_ip}")

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
        qr = qrcode.QRCode(version=1, box_size=10, border=2)
        qr.add_data(data)
        qr.make(fit=True)
        img = qr.make_image(fill_color="black", back_color="white")
        img_tk = ImageTk.PhotoImage(img.resize((160, 160), Image.Resampling.LANCZOS))
        label.configure(image=img_tk)
        label.image = img_tk

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
        active_ips = [ip for ip, data in ACTIVE_DEVICES.items() if current_time - data['last_seen'] < DEVICE_ONLINE_WINDOW]
        
        self.stat_vars["online_scanners"].configure(text=str(len(active_ips)))

        for row in self.tree_devices.get_children():
            self.tree_devices.delete(row)

        if active_ips:
            sorted_ips = sorted(active_ips, key=lambda ip: ACTIVE_DEVICES[ip]['name'].lower())
            for ip in sorted_ips:
                name = ACTIVE_DEVICES[ip]['name']
                seconds_ago = int(current_time - ACTIVE_DEVICES[ip]['last_seen'])
                self.tree_devices.insert("", END, values=(name, ip, f"{seconds_ago}s ago", "🟢 Online"), tags=("online",))
        else:
            self.tree_devices.insert("", END, values=("No devices connected yet — awaiting heartbeat...", "", "", ""), tags=("empty",))

        if self.SessionMySQL: self.lbl_stat_mysql.configure(text="● MYSQL: LIVE", bootstyle=SUCCESS)
        else: self.lbl_stat_mysql.configure(text="● MYSQL: OFFLINE", bootstyle=DANGER)

        if self.SessionSQLite: self.lbl_stat_sqlite.configure(text="● SQLITE: MIRROR ACTIVE", bootstyle=SUCCESS)
        else: self.lbl_stat_sqlite.configure(text="● SQLITE: FAULT", bootstyle=DANGER)

        if not self.SessionMySQL: return
        
        mysql_session = self.SessionMySQL()
        try:
            total_mysql = mysql_session.query(Attendee).count()
            kiosk_regs = mysql_session.query(OfflineKioskAttendee).count()
            
            chk_30 = mysql_session.query(Attendee).filter(Attendee.checkin_history.like('%"30 August"%')).count()
            chk_31 = mysql_session.query(Attendee).filter(Attendee.checkin_history.like('%"31 August"%')).count()
            chk_01 = mysql_session.query(Attendee).filter(Attendee.checkin_history.like('%"1 September"%')).count()
            
            today_str = SERVER_TEST_DATE if SERVER_TEST_MODE else datetime.now().strftime('%Y-%m-%d')
            if today_str == "2026-08-30": chk_today = chk_30
            elif today_str == "2026-08-31": chk_today = chk_31
            elif today_str == "2026-09-01": chk_today = chk_01
            else: chk_today = 0

            total_checkins = chk_30 + chk_31 + chk_01

            self.stat_vars["total_att"].configure(text=str(total_mysql))
            self.stat_vars["kiosk_reg"].configure(text=str(kiosk_regs))
            self.stat_vars["sqlite_total"].configure(text=str(total_mysql))
            self.stat_vars["chk_30"].configure(text=str(chk_30))
            self.stat_vars["chk_31"].configure(text=str(chk_31))
            self.stat_vars["chk_01"].configure(text=str(chk_01))
            self.stat_vars["chk_today"].configure(text=str(chk_today))
            self.stat_vars["chk_total"].configure(text=str(total_checkins))
        except Exception:
            pass
        finally:
            mysql_session.close()
            
        self.after(4000, self.refresh_stats)

    def start_flask(self):
        self.btn_start_flask.configure(state=DISABLED)
        self.btn_stop_flask.configure(state=NORMAL)
        self.btn_start_cf.configure(state=NORMAL)
        
        self._append_log(self.log_flask, f"[{datetime.now().strftime('%H:%M:%S')}] Booting Waitress Multi-Threaded Engine...")
        
        # Start Waitress HTTP Engine (30 Concurrent Threads)
        self.http_thread = WaitressHttpThread(app, '0.0.0.0', HTTP_PORT)
        self.http_thread.start()
        
        # Start HTTPS Engine for iOS Camera Fallback
        self.https_thread = HttpsFlaskThread(app, '0.0.0.0', HTTPS_PORT)
        self.https_thread.start()

        self.update_qr(self.lbl_flask_qr, self.https_url)
        self.lbl_flask_link.configure(text=self.https_url, foreground="#4D9CE6")
        
        self._append_log(self.log_flask, f"[SYSTEM] Waitress HTTP listening on: {self.http_url}")
        self._append_log(self.log_flask, f"[SYSTEM] iOS Secure HTTPS listening on: {self.https_url}")
        
    def stop_flask(self):
        if self.btn_stop_cf['state'] == NORMAL: self.stop_cf()
        self.btn_stop_flask.configure(state=DISABLED)
        self.btn_start_flask.configure(state=NORMAL)
        self.btn_start_cf.configure(state=DISABLED)
        self.update_qr(self.lbl_flask_qr, "OFFLINE")
        self.lbl_flask_link.configure(text="Server Offline", foreground="gray")
        
        if self.http_thread:
            self.http_thread.shutdown()
            self.http_thread = None
            
        if self.https_thread:
            self.https_thread.shutdown()
            self.https_thread = None
            
        self._append_log(self.log_flask, f"[{datetime.now().strftime('%H:%M:%S')}] Engine stopped.")

    def start_cf(self):
        # Ensure local engine is running first
        if not self.http_thread:
            self._append_log(self.log_cf, "[ERROR] Start the Local Engine (Port 5000) BEFORE starting the tunnel!")
            return

        self.btn_start_cf.configure(state=DISABLED)
        self.btn_stop_cf.configure(state=NORMAL)
        self.lbl_stat_cf.configure(text="● Cloudflare: CONNECTING", bootstyle=WARNING)
        self._append_log(self.log_cf, f"[{datetime.now().strftime('%H:%M:%S')}] Requesting secure tunnel to port {HTTP_PORT}...")

        def _run_cf():
            try:
                cmd = [
                    "cloudflared", "tunnel", 
                    "--url", f"http://{self.local_ip}:{HTTP_PORT}",
                    "--http-host-header", "localhost",  
                    "--no-tls-verify"
                ]
                creationflags = subprocess.CREATE_NO_WINDOW if platform.system() == "Windows" else 0
                self.cf_process = subprocess.Popen(
                    cmd,
                    stdout=subprocess.PIPE,
                    stderr=subprocess.STDOUT,
                    text=True,
                    bufsize=1,  # 👈 Force line-by-line unbuffered streaming
                    creationflags=creationflags
                )

                url_found = False
                for line in self.cf_process.stdout:
                    # Clean ANSI terminal escape codes from cloudflared output
                    clean_line = re.sub(r'\x1b\[[0-9;]*m', '', line)
                    self._append_log(self.log_cf, clean_line.strip())
                    
                    if not url_found:
                        match = re.search(r"https://[a-zA-Z0-9-]+\.trycloudflare\.com", clean_line)
                        if match:
                            tunnel_url = match.group(0)
                            self.cloudflare_url = tunnel_url
                            url_found = True
                            
                            # 🛡️ FIX: Give Cloudflare's global edge 3 seconds to propagate DNS
                            # Give Cloudflare edge DNS slightly more time to propagate
                            self._append_log(self.log_cf, "[INFO] Waiting 5 seconds for Cloudflare Edge DNS propagation...")
                            time.sleep(5)
                            
                            # Update UI safely through thread queue
                            self.gui_queue.put(lambda u=tunnel_url: self.update_qr(self.lbl_cf_qr, u))
                            self.gui_queue.put(lambda u=tunnel_url: self.lbl_cf_link.configure(text=u, foreground="#4D9CE6"))
                            self.gui_queue.put(lambda: self.lbl_stat_cf.configure(text="● Cloudflare: LIVE", bootstyle=SUCCESS))
                            self._append_log(self.log_cf, f"[SUCCESS] Tunnel active at: {self.cloudflare_url}")
                            
            except FileNotFoundError:
                self.gui_queue.put(self.stop_cf)
                self._append_log(self.log_cf, "[ERROR] 'cloudflared' binary not found. Ensure it is installed and in your system PATH.")
            except Exception as e:
                self.gui_queue.put(self.stop_cf)
                self._append_log(self.log_cf, f"[ERROR] Tunnel failed: {str(e)}")

        threading.Thread(target=_run_cf, daemon=True).start()
        
    def stop_cf(self):
        self.btn_stop_cf.configure(state=DISABLED)
        
        # 🛡️ FIX: Directly check if the Local Engine thread is active to re-enable the button
        if self.http_thread is not None:
            self.btn_start_cf.configure(state=NORMAL)
        else:
            self.btn_start_cf.configure(state=DISABLED)
            
        self.lbl_stat_cf.configure(text="● Cloudflare: OFFLINE", bootstyle=SECONDARY)
        
        if self.cf_process:
            try:
                self.cf_process.terminate()
            except Exception: pass
            finally: self.cf_process = None

        self.cloudflare_url = "Offline"
        self.update_qr(self.lbl_cf_qr, "OFFLINE")
        self.lbl_cf_link.configure(text="Tunnel Offline", foreground="gray")
        self._append_log(self.log_cf, f"[{datetime.now().strftime('%H:%M:%S')}] Tunnel connection closed.")

if __name__ == "__main__":
    app_window = ServerHub()
    app_window.mainloop()