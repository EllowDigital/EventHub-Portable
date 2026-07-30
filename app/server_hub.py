import os
import json
import logging
from logging.handlers import RotatingFileHandler
import threading
import subprocess
import socket
import random
import platform
import re
import time
import uuid
import queue
import collections
import concurrent.futures
from dataclasses import dataclass, field
import ipaddress
import requests
import urllib3
from datetime import datetime, timezone, timedelta
import tkinter as tk

import ttkbootstrap as ttk
from ttkbootstrap.constants import *
from ttkbootstrap.widgets.scrolled import ScrolledText
from ttkbootstrap.dialogs import Messagebox
import qrcode
from PIL import Image, ImageTk
import webbrowser
import psutil

# --- MATPLOTLIB INTEGRATION ---
try:
    import matplotlib
    matplotlib.use("TkAgg")
    from matplotlib.figure import Figure
    from matplotlib.backends.backend_tkagg import FigureCanvasTkAgg
    MATPLOTLIB_AVAILABLE = True
except ImportError:
    MATPLOTLIB_AVAILABLE = False

from flask import Flask, render_template, request, jsonify, Response
from waitress import create_server 

urllib3.disable_warnings(urllib3.exceptions.InsecureRequestWarning)

try:
    from cheroot import wsgi as cheroot_wsgi
    from cheroot.ssl.builtin import BuiltinSSLAdapter
except ImportError:
    cheroot_wsgi = None
    BuiltinSSLAdapter = None

try:
    from cryptography import x509
    from cryptography.hazmat.primitives import hashes, serialization
    from cryptography.hazmat.primitives.asymmetric import rsa
    from cryptography.x509.oid import NameOID
    CRYPTOGRAPHY_AVAILABLE = True
except ImportError:
    CRYPTOGRAPHY_AVAILABLE = False

from sqlalchemy.orm.attributes import flag_modified
from sqlalchemy.exc import IntegrityError


# ==============================================================================
# 24/7 STABILITY: GLOBAL CRASH HANDLER
# ==============================================================================
def global_exception_handler(*args):
    logging.error("Uncaught GUI Exception intercepted. App remains running.", exc_info=args)

tk.Tk.report_callback_exception = global_exception_handler


def _configure_windows_platform():
    if platform.system() != "Windows": 
        return
    try:
        from ctypes import windll, c_void_p
        dpi_awareness_set = False
        try:
            if windll.user32.SetProcessDpiAwarenessContext(c_void_p(-4)):
                dpi_awareness_set = True
        except Exception: 
            pass
        
        if not dpi_awareness_set:
            try:
                windll.shcore.SetProcessDpiAwareness(2)
                dpi_awareness_set = True
            except Exception: 
                pass
                
        if not dpi_awareness_set:
            try: 
                windll.user32.SetProcessDPIAware()
            except Exception: 
                pass

        try: windll.shell32.SetCurrentProcessExplicitAppUserModelID("TDEUP2026.EventHub.ServerHub")
        except Exception: pass
            
        try: windll.kernel32.SetThreadExecutionState(0x80000000 | 0x00000001)
        except Exception: pass
    except Exception: 
        pass

_configure_windows_platform()

try:
    from app.schema import Attendee, OfflineKioskAttendee, get_database_sessions
except ModuleNotFoundError:
    from schema import Attendee, OfflineKioskAttendee, get_database_sessions


BASE_DIR = os.path.dirname(os.path.abspath(__file__))
LOG_DIR = os.path.join(BASE_DIR, 'logs')
os.makedirs(LOG_DIR, exist_ok=True)

HTTP_PORT = 5000 
HTTPS_PORT = 5001  
CERT_DIR = os.path.join(BASE_DIR, 'config', 'certs')

# --- EXTREME PERFORMANCE TUNING (10,000+ Attendees) ---
DB_WRITER_THREADS = 16            
DB_JOB_QUEUE_MAXSIZE = 2000       
DB_JOB_TIMEOUT = 10               
STATS_REFRESH_INTERVAL_SEC = 3   
SLOW_REQUEST_THRESHOLD_MS = 300  
MAX_LOG_LINES = 2000             

LOG_FILE = os.path.join(LOG_DIR, 'server_hub.log')
_file_handler = RotatingFileHandler(LOG_FILE, maxBytes=10_000_000, backupCount=5, encoding='utf-8')
_file_handler.setFormatter(logging.Formatter('%(asctime)s [%(levelname)s] %(message)s'))
logging.basicConfig(level=logging.INFO, handlers=[_file_handler])

template_dir = os.path.join(BASE_DIR, 'templates')
static_dir = os.path.join(BASE_DIR, 'static')
app = Flask(__name__, template_folder=template_dir, static_folder=static_dir)
app.config['MAX_CONTENT_LENGTH'] = 2 * 1024 * 1024  

gui_log_callback = None
SERVER_TEST_MODE = False
SERVER_TEST_DATE = "2026-08-30"

ACTIVE_DEVICES = {}
SCAN_CLIENTS = []
scan_clients_lock = threading.Lock()
DEVICE_ONLINE_WINDOW = 25  

DB_SESSIONS_CACHE = None
_db_cache_lock = threading.Lock()
_db_cache_last_failure = 0.0
DB_SESSIONS_RETRY_COOLDOWN = 5  

# --- METRICS & TRAFFIC TRACKING ---
NETWORK_LATENCY = {"local_ms": 0, "cloud_ms": 0, "local_status": "OFFLINE", "cloud_status": "OFFLINE"}
network_latency_lock = threading.Lock()

SERVER_METRICS = {"avg_process_ms": 0.0, "req_count": 0}
metrics_lock = threading.Lock()

# Graph Data Tracker
TRAFFIC_HISTORY = collections.deque([0] * 60, maxlen=60)
_current_sec_requests = 0
traffic_lock = threading.Lock()

STATS_CACHE = {
    "total_attendees": 0, "total_registrations": 0,
    "chk_30": 0, "chk_31": 0, "chk_01": 0,
    "total_scans": 0, "today_scans": 0,
    "last_refreshed": 0.0, "last_error": None,
}
stats_lock = threading.Lock()


def get_cached_sessions():
    global DB_SESSIONS_CACHE, _db_cache_last_failure
    if DB_SESSIONS_CACHE is None:
        with _db_cache_lock:
            if DB_SESSIONS_CACHE is None:
                if (time.time() - _db_cache_last_failure) < DB_SESSIONS_RETRY_COOLDOWN:
                    return None
                try: 
                    DB_SESSIONS_CACHE = get_database_sessions()
                except Exception:
                    logging.exception(f"DB failed — retry in {DB_SESSIONS_RETRY_COOLDOWN}s")
                    _db_cache_last_failure = time.time()
                    return None
    return DB_SESSIONS_CACHE


def _write_self_signed_cert(cert_path, key_path, local_ip):
    key = rsa.generate_private_key(public_exponent=65537, key_size=2048)
    subject = issuer = x509.Name([x509.NameAttribute(NameOID.COMMON_NAME, "TDE-EventHub-EllowLabs")])
    san_entries = [x509.DNSName("localhost"), x509.IPAddress(ipaddress.ip_address("127.0.0.1"))]
    try: san_entries.append(x509.IPAddress(ipaddress.ip_address(local_ip)))
    except Exception: pass
        
    now = datetime.now(timezone.utc)
    cert = (x509.CertificateBuilder().subject_name(subject).issuer_name(issuer).public_key(key.public_key())
        .serial_number(x509.random_serial_number()).not_valid_before(now).not_valid_after(now + timedelta(days=730))
        .add_extension(x509.SubjectAlternativeName(san_entries), critical=False).sign(key, hashes.SHA256()))

    with open(key_path, "wb") as f: f.write(key.private_bytes(encoding=serialization.Encoding.PEM, format=serialization.PrivateFormat.TraditionalOpenSSL, encryption_algorithm=serialization.NoEncryption()))
    with open(cert_path, "wb") as f: f.write(cert.public_bytes(serialization.Encoding.PEM))


def ensure_ssl_certificate(local_ip):
    if not CRYPTOGRAPHY_AVAILABLE: raise RuntimeError("Cryptography package required.")
    os.makedirs(CERT_DIR, exist_ok=True)
    cert_path = os.path.join(CERT_DIR, 'hub_cert.pem')
    key_path = os.path.join(CERT_DIR, 'hub_key.pem')
    ip_marker_path = os.path.join(CERT_DIR, 'hub_cert_ip.txt')
    
    reuse_existing = False
    if os.path.exists(cert_path) and os.path.exists(key_path) and os.path.exists(ip_marker_path):
        try:
            with open(ip_marker_path, 'r') as f: reuse_existing = f.read().strip() == local_ip
        except Exception: pass

    if not reuse_existing:
        _write_self_signed_cert(cert_path, key_path, local_ip)
        with open(ip_marker_path, 'w') as f: f.write(local_ip)
            
    return cert_path, key_path


@app.before_request
def _start_request_timer(): 
    request._start_time = time.perf_counter()


@app.after_request
def log_request(response):
    if request.path.startswith('/static') or request.path.startswith('/favicon.ico') or request.path == '/api/stream-scans': 
        return response
        
    try:
        global _current_sec_requests
        with traffic_lock:
            _current_sec_requests += 1

        duration_ms = (time.perf_counter() - getattr(request, '_start_time', time.perf_counter())) * 1000
        if metrics_lock.acquire(blocking=False):
            try:
                SERVER_METRICS["req_count"] += 1
                if SERVER_METRICS["avg_process_ms"] == 0:
                    SERVER_METRICS["avg_process_ms"] = duration_ms
                else:
                    SERVER_METRICS["avg_process_ms"] = (SERVER_METRICS["avg_process_ms"] * 0.9) + (duration_ms * 0.1)
            finally:
                metrics_lock.release()
                
        if duration_ms > SLOW_REQUEST_THRESHOLD_MS: 
            logging.warning(f"Slow req: {request.method} {request.path} took {duration_ms:.0f}ms")
    except Exception: 
        pass
        
    return response


def _status_log_tag(status_code):
    if status_code >= 500: return "log_error"
    if status_code >= 400: return "log_warning"
    return "log_success"

_LOG_PREFIX_TAGS = (("[PING ERROR]", "log_error"), ("[ERROR]", "log_error"), ("[WARNING]", "log_warning"), ("[SUCCESS]", "log_success"), ("[CLIPBOARD]", "log_info"), ("[INFO]", "log_info"))

def _guess_log_tag(message):
    for prefix, tag in _LOG_PREFIX_TAGS:
        if message.startswith(prefix): return tag
    return "log_default"


def log_event_clean(action_type, device_name, details, status_code):
    time_str = datetime.now().strftime('%H:%M:%S')
    status_tag = _status_log_tag(status_code)

    if action_type == "REGISTER":
        segments = [(f"[{time_str}] ", "log_dim"), (f"{'✅' if status_code == 200 else '❌'} REGISTER  ", "log_register"), (f"[{device_name}] {details} — ", "log_default"), (f"Status: {status_code}", status_tag)]
    elif action_type == "CHECKIN":
        segments = [(f"[{time_str}] ", "log_dim"), (f"{'🎫' if status_code == 200 else '⛔'} CHECKIN  ", "log_checkin"), (f"[{device_name}] {details} — ", "log_default"), (f"Status: {status_code}", status_tag)]
    else:
        segments = [(f"[{time_str}] ", "log_dim"), (f"🌐 [{device_name}] {action_type} — ", "log_default"), (f"Status: {status_code}", status_tag)]

    if gui_log_callback: 
        gui_log_callback(segments)
    
    plain_msg = f"[{device_name}] {action_type}: {details} (status {status_code})"
    if status_code >= 500: logging.error(plain_msg)
    elif status_code >= 400: logging.warning(plain_msg)
    else: logging.info(plain_msg)


def broadcast_scan(attendee, status, message, device_name, scan_time):
    att_dict = None
    if attendee:
        att_dict = {
            "attendee_id": attendee.attendee_id, "full_name": attendee.full_name, "business_name": attendee.business_name, 
            "mobile": attendee.mobile, "city": attendee.city, "state": attendee.state, 
            "attendee_type": getattr(attendee.attendee_type, 'value', str(attendee.attendee_type)), "gender": getattr(attendee.gender, 'value', str(attendee.gender))
        }
    event = {"status": status, "message": message, "device": device_name, "timestamp": scan_time, "attendee": att_dict}
    
    with scan_clients_lock: clients_snapshot = list(SCAN_CLIENTS)
    for q in clients_snapshot:
        try: q.put_nowait(event)
        except Exception:
            with scan_clients_lock:
                if q in SCAN_CLIENTS: SCAN_CLIENTS.remove(q)


def traffic_monitor_loop():
    global _current_sec_requests
    while True:
        time.sleep(1)
        with traffic_lock:
            hits = _current_sec_requests
            _current_sec_requests = 0
        TRAFFIC_HISTORY.append(hits)


def _compute_stats_snapshot():
    sessions = get_cached_sessions()
    mysql_factory = sessions.get('mysql') if sessions else None
    if not mysql_factory: return None
        
    session = mysql_factory()
    try:
        total_attendees = session.query(Attendee).count()
        total_registrations = session.query(OfflineKioskAttendee).count()
        chk_30 = session.query(Attendee).filter(Attendee.checkin_history.like('%"30 August"%')).count()
        chk_31 = session.query(Attendee).filter(Attendee.checkin_history.like('%"31 August"%')).count()
        chk_01 = session.query(Attendee).filter(Attendee.checkin_history.like('%"1 September"%')).count()
        
        return {
            "total_attendees": total_attendees, "total_registrations": total_registrations, 
            "chk_30": chk_30, "chk_31": chk_31, "chk_01": chk_01, "total_scans": chk_30 + chk_31 + chk_01
        }
    finally: 
        session.close()


def stats_refresher_loop():
    while True:
        try:
            snapshot = _compute_stats_snapshot()
            if snapshot is not None:
                today_date = SERVER_TEST_DATE if SERVER_TEST_MODE else datetime.now(timezone.utc).strftime('%Y-%m-%d')
                today_scans = {"2026-08-30": snapshot["chk_30"], "2026-08-31": snapshot["chk_31"], "2026-09-01": snapshot["chk_01"]}.get(today_date, 0)
                
                with stats_lock:
                    STATS_CACHE.update(snapshot)
                    STATS_CACHE["today_scans"] = today_scans
                    STATS_CACHE["last_refreshed"] = time.time()
                    STATS_CACHE["last_error"] = None
        except Exception as e:
            with stats_lock: STATS_CACHE["last_error"] = str(e)
        time.sleep(STATS_REFRESH_INTERVAL_SEC)


@dataclass
class DBJob:
    kind: str
    payload: dict
    future: concurrent.futures.Future = field(default_factory=concurrent.futures.Future)

DB_WRITE_QUEUE = queue.Queue(maxsize=DB_JOB_QUEUE_MAXSIZE)
_db_writer_threads = []


def _submit_db_job(kind, payload):
    job = DBJob(kind=kind, payload=payload)
    try: DB_WRITE_QUEUE.put(job, timeout=1)
    except queue.Full: return 503, {"status": "error", "message": "Server heavily loaded."}
        
    try: return job.future.result(timeout=DB_JOB_TIMEOUT)
    except concurrent.futures.TimeoutError: return 504, {"status": "error", "message": "Request took too long."}
    except Exception: return 500, {"status": "error", "message": "Internal error."}


def _handle_checkin_job(payload):
    identifier, search_type = payload["identifier"], payload["search_type"]
    device_name, iso_timestamp = payload["device_name"], payload["iso_timestamp"]

    if not identifier: return 400, {"status": "error", "message": "No ID provided"}
    sessions = get_cached_sessions() or {}
    mysql_factory = sessions.get('mysql')
    if not mysql_factory: return 503, {"status": "error", "message": "DB offline"}
    
    session = mysql_factory()
    try:
        attendee = None
        if search_type == 'phone': 
            attendee = session.query(Attendee).filter_by(mobile=identifier).with_for_update().first() or session.query(OfflineKioskAttendee).filter_by(mobile=identifier).with_for_update().first()
        else: 
            attendee = session.query(Attendee).filter_by(attendee_id=identifier).with_for_update().first() or session.query(OfflineKioskAttendee).filter_by(attendee_id=identifier).with_for_update().first()

        if not attendee:
            log_event_clean("CHECKIN", device_name, f"Not found: {identifier}", 404)
            broadcast_scan(None, "ERROR", f"Not found: {identifier}", device_name, iso_timestamp)
            return 404, {"status": "error", "message": f"Not found: {identifier}"}

        history = attendee.checkin_history
        if isinstance(history, str):
            try: history = json.loads(history)
            except Exception: history = {}
        if not isinstance(history, dict): history = {}

        current_date_str = SERVER_TEST_DATE if SERVER_TEST_MODE else datetime.now(timezone.utc).strftime('%Y-%m-%d')
        date_map = {"2026-08-30": "30 August", "2026-08-31": "31 August", "2026-09-01": "1 September"}

        if current_date_str not in date_map: return 400, {"status": "error", "message": "Invalid date"}
        today_key = date_map[current_date_str]
        
        att_days = attendee.attendance_days or []
        if isinstance(att_days, str):
            try: att_days = json.loads(att_days)
            except Exception: att_days = []

        if today_key not in att_days:
            log_event_clean("CHECKIN", device_name, f"Denied (No pass {today_key})", 403)
            broadcast_scan(attendee, "ERROR", f"Denied (No pass {today_key})", device_name, iso_timestamp)
            return 403, {"status": "error", "message": f"Denied (No pass {today_key})"}

        if today_key in history:
            log_event_clean("CHECKIN", device_name, f"Duplicate: {attendee.full_name}", 400)
            broadcast_scan(attendee, "DUPLICATE", f"Duplicate: {attendee.full_name}", device_name, iso_timestamp)
            return 400, {"status": "error", "message": f"Duplicate: {attendee.full_name}"}

        history[today_key] = {"timestamp": iso_timestamp, "source": "offline_hub", "device": device_name, "date_code": current_date_str, "display_date": today_key}
        
        attendee.checkin_history = history
        flag_modified(attendee, "checkin_history")
        attendee.needs_cloud_sync, attendee.needs_sheet_sync, attendee.needs_local_sync, attendee.local_modified = True, True, False, True

        session.commit()
        success_msg = f"{attendee.full_name} ({attendee.attendee_id})"
        log_event_clean("CHECKIN", device_name, success_msg, 200)
        broadcast_scan(attendee, "SUCCESS", success_msg, device_name, iso_timestamp)
        return 200, {"status": "success", "message": success_msg, "time": iso_timestamp}
        
    except Exception as e:
        try: session.rollback()
        except Exception: pass
        log_event_clean("CHECKIN", device_name, f"DB Error", 500)
        return 500, {"status": "error", "message": "Internal error."}
    finally:
        try: session.close()
        except Exception: pass


def _handle_register_job(payload):
    data, device_label = payload["data"], payload["device_label"]
    iso_timestamp, mobile_number = payload["iso_timestamp"], payload["mobile_number"]

    sessions = get_cached_sessions() or {}
    mysql_factory = sessions.get('mysql')
    
    if not mysql_factory: return 503, {"status": "error", "message": "DB offline"}
    session = mysql_factory()

    try:
        existing_main = session.query(Attendee).filter_by(mobile=mobile_number).with_for_update().first()
        if existing_main:
            log_event_clean("REGISTER", device_label, f"{data.get('full_name')} (Already Exists)", 200)
            return 200, {"status": "already_registered", "attendee_id": existing_main.attendee_id}

        existing_kiosk = session.query(OfflineKioskAttendee).filter_by(mobile=mobile_number).with_for_update().first()
        if existing_kiosk:
            log_event_clean("REGISTER", device_label, f"{data.get('full_name')} (Already Exists)", 200)
            return 200, {"status": "already_registered", "attendee_id": existing_kiosk.attendee_id}

        def gen_id(att_type: str) -> str:
            prefix = {"GENERAL":"G", "BUSINESS":"B", "MEDIA":"M", "EXHIBITOR":"E"}.get(att_type.upper(), "G")
            for _ in range(5000):
                aid = f"TDE26-{prefix}-" + "".join(random.choices("ABCDEFGHJKLMNPQRSTUVWXYZ23456789", k=6))
                if not session.query(Attendee).filter_by(attendee_id=aid).first() and not session.query(OfflineKioskAttendee).filter_by(attendee_id=aid).first(): 
                    return aid
            raise RuntimeError("ID generation failed")

        new_attendee_id = gen_id(data.get('attendee_type', 'GENERAL'))
        today_date = SERVER_TEST_DATE if SERVER_TEST_MODE else datetime.now(timezone.utc).strftime('%Y-%m-%d')
        
        checkin_history_dict = {}
        if today_date in ["2026-08-30", "2026-08-31", "2026-09-01"]:
            key = {"2026-08-30": "30 August", "2026-08-31": "31 August", "2026-09-01": "1 September"}[today_date]
            checkin_history_dict[key] = {"timestamp": iso_timestamp, "source": "offline_hub", "device": device_label, "date_code": today_date, "display_date": key}

        new_kiosk_reg = OfflineKioskAttendee(
            id=str(uuid.uuid4()), attendee_id=new_attendee_id, full_name=data.get('full_name'), mobile=mobile_number,
            email=data.get('email', ''), gender=data.get('gender'), attendee_type=data.get('attendee_type'),
            business_name=data.get('business_name', ''), business_category=data.get('business_category', ''),
            other_category=data.get('other_category', ''), address=data.get('address', ''), city=data.get('city', ''),
            state=data.get('state', ''), pincode=data.get('pincode', ''), attendance_days=data.get('attendance_days', []),
            photo_url=None, checkin_history=checkin_history_dict, device_name=device_label, needs_cloud_sync=True,
            needs_sheet_sync=True, needs_local_sync=False, local_modified=True
        )
        
        try:
            session.add(new_kiosk_reg)
            session.commit()
        except IntegrityError:
            session.rollback()
            existing = session.query(Attendee).filter_by(mobile=mobile_number).first() or session.query(OfflineKioskAttendee).filter_by(mobile=mobile_number).first()
            if existing: 
                return 200, {"status": "already_registered", "attendee_id": existing.attendee_id}
            return 500, {"status": "error", "message": "Integrity Error."}

        log_event_clean("REGISTER", device_label, f"{data.get('full_name')} ({new_attendee_id})", 200)
        return 200, {"status": "success", "message": "Saved successfully.", "attendee_id": new_attendee_id}
        
    except Exception as e:
        try: session.rollback()
        except Exception: pass
        return 500, {"status": "error", "message": "Internal error."}
    finally:
        try: session.close()
        except Exception: pass


def db_writer_loop(worker_id):
    logging.info(f"DB writer #{worker_id} ready")
    while True:
        job = DB_WRITE_QUEUE.get()
        if job is None: 
            DB_WRITE_QUEUE.task_done()
            break
            
        try:
            if job.kind == "checkin": result = _handle_checkin_job(job.payload)
            elif job.kind == "register": result = _handle_register_job(job.payload)
            else: result = (500, {"status": "error", "message": "Unknown job"})
                
            if not job.future.done(): job.future.set_result(result)
        except Exception as e:
            if not job.future.done(): job.future.set_exception(e)
        finally: 
            DB_WRITE_QUEUE.task_done()


def start_db_writers():
    for i in range(DB_WRITER_THREADS):
        t = threading.Thread(target=db_writer_loop, args=(i + 1,), daemon=True, name=f"DBWriter-{i+1}")
        t.start()
        _db_writer_threads.append(t)


def stop_db_writers():
    for _ in range(len(_db_writer_threads)): 
        DB_WRITE_QUEUE.put(None)
    _db_writer_threads.clear()


device_lock = threading.Lock()

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
        with device_lock: 
            ACTIVE_DEVICES[ip] = {'last_seen': time.time(), 'name': custom_device_name}
    return jsonify({"test_mode": SERVER_TEST_MODE, "test_date": SERVER_TEST_DATE}), 200


@app.route('/api/stream-scans')
def stream_scans():
    def event_stream():
        q = queue.Queue(maxsize=50)
        with scan_clients_lock: 
            SCAN_CLIENTS.append(q)
        try:
            while True:
                try: 
                    yield f"data: {json.dumps(q.get(timeout=10))}\n\n"
                except queue.Empty: 
                    yield ": heartbeat\n\n"
        except GeneratorExit: 
            pass 
        finally:
            with scan_clients_lock:
                if q in SCAN_CLIENTS: 
                    SCAN_CLIENTS.remove(q)
                    
    return Response(event_stream(), mimetype='text/event-stream', headers={'Cache-Control': 'no-cache', 'Connection': 'keep-alive', 'X-Accel-Buffering': 'no'})


@app.route('/api/checkin', methods=['POST'])
def process_checkin():
    data = request.json or {}
    payload = {
        "identifier": str(data.get('attendee_id', data.get('qr_data', data.get('id', '')))).strip(), 
        "search_type": data.get('search_type', 'id'), 
        "device_name": data.get('device_name', f"Scanner ({request.remote_addr})"), 
        "iso_timestamp": datetime.now(timezone.utc).strftime('%Y-%m-%dT%H:%M:%S.000Z')
    }
    status_code, body = _submit_db_job("checkin", payload)
    return jsonify(body), status_code


@app.route('/api/register', methods=['POST'])
def process_registration():
    data = request.json or {}
    payload = {
        "data": data, 
        "mobile_number": data.get('mobile', '').strip(), 
        "device_label": data.get('device_name', f"Kiosk ({request.user_agent.platform or 'Unknown'} - {request.remote_addr})"), 
        "iso_timestamp": datetime.now(timezone.utc).strftime('%Y-%m-%dT%H:%M:%S.000Z')
    }
    status_code, body = _submit_db_job("register", payload)
    return jsonify(body), status_code


@app.route('/api/check_mobile', methods=['GET'])
def check_mobile():
    mobile_number = request.args.get('mobile', '').strip()
    if not mobile_number: 
        return jsonify({"status": "error", "message": "Mobile required"}), 400
        
    sessions = get_cached_sessions() or {}
    mysql_factory = sessions.get('mysql')
    if not mysql_factory: 
        return jsonify({"status": "error", "message": "DB offline"}), 503
        
    session = mysql_factory()
    try:
        em = session.query(Attendee).filter_by(mobile=mobile_number).first()
        if em: 
            return jsonify({"status": "already_registered", "attendee_id": em.attendee_id}), 200
            
        ek = session.query(OfflineKioskAttendee).filter_by(mobile=mobile_number).first()
        if ek: 
            return jsonify({"status": "already_registered", "attendee_id": ek.attendee_id}), 200
            
        return jsonify({"status": "not_found"}), 200
    except Exception: 
        return jsonify({"status": "error", "message": "Internal error."}), 500
    finally:
        try: session.close()
        except Exception: pass


@app.route('/api/network-data', methods=['GET'])
def get_network_data():
    current_time = time.time()
    active_devices = {}
    
    with device_lock:
        for ip, data in list(ACTIVE_DEVICES.items()):
            if current_time - data['last_seen'] < DEVICE_ONLINE_WINDOW: 
                active_devices[ip] = data
            else: 
                del ACTIVE_DEVICES[ip]
                
    with stats_lock:
        global_stats = {
            "total_scans": STATS_CACHE["total_scans"], 
            "total_registrations": STATS_CACHE["total_registrations"], 
            "today_scans": STATS_CACHE["today_scans"]
        }
    return jsonify({"active_devices": active_devices, "global_stats": global_stats}), 200


class WaitressHttpThread(threading.Thread):
    def __init__(self, app, host, port):
        super().__init__(daemon=True)  
        self.server = create_server(app, host=host, port=port, threads=150, connection_limit=500, channel_timeout=15)
        self.ctx = app.app_context()
        self.ctx.push()
        
    def run(self):
        try: self.server.run()
        except Exception: logging.exception("Waitress crashed")
            
    def shutdown(self): 
        self.server.close()


class HttpsFlaskThread(threading.Thread):
    def __init__(self, app, host, port, numthreads=150):
        super().__init__(daemon=True)  
        if cheroot_wsgi is None: raise RuntimeError("Cheroot required.")
            
        cert_path, key_path = ensure_ssl_certificate(get_local_ip())
        self.ctx = app.app_context()
        self.ctx.push()
        
        self.server = cheroot_wsgi.Server(bind_addr=(host, port), wsgi_app=app, numthreads=numthreads, request_queue_size=512)
        self.server.ssl_adapter = BuiltinSSLAdapter(certificate=cert_path, private_key=key_path)
        
    def run(self):
        try: self.server.start()
        except Exception: logging.exception("Cheroot crashed")
            
    def shutdown(self): 
        self.server.stop()


def get_local_ip():
    try:
        s = socket.socket(socket.AF_INET, socket.SOCK_DGRAM)
        s.connect(("10.255.255.255", 1))
        ip, _ = s.getsockname()
        s.close()
        return ip
    except Exception: 
        try: return socket.gethostbyname(socket.gethostname())
        except Exception: return "127.0.0.1"


def _hex_to_rgb(hex_color): return tuple(int(hex_color.lstrip('#')[i:i+2], 16) for i in (0, 2, 4))
def _rgb_to_hex(rgb): return "#{:02x}{:02x}{:02x}".format(*(max(0, min(255, int(round(c)))) for c in rgb))
def _mix_hex(c_a, c_b, w): return _rgb_to_hex(a + (b - a) * w for a, b in zip(_hex_to_rgb(c_a), _hex_to_rgb(c_b)))


class NetworkTelemetryWindow(ttk.Toplevel):
    def __init__(self, parent, hub_instance):
        super().__init__(parent)
        self.title("Live Network & Traffic Telemetry")
        
        if MATPLOTLIB_AVAILABLE:
            self.geometry("900x650")
        else:
            self.geometry("850x380")
            
        self.resizable(False, False)
        self.hub = hub_instance
        self.attributes('-topmost', True)
        self.update_idletasks()
        self.geometry(f"+{parent.winfo_x() + (parent.winfo_width() // 2) - (self.winfo_width() // 2)}+{parent.winfo_y() + (parent.winfo_height() // 2) - (self.winfo_height() // 2)}")
        self.build_ui()
        self.refresh_meters()

    def build_ui(self):
        main_frame = ttk.Frame(self, padding=25)
        main_frame.pack(fill=BOTH, expand=True)
        
        ttk.Label(main_frame, text="📡 Real-Time Hub Telemetry", font="-size 16 -weight bold", bootstyle=PRIMARY).pack(anchor=CENTER, pady=(0, 5))
        grid = ttk.Frame(main_frame)
        grid.pack(fill=X)
        
        local_card = ttk.Labelframe(grid, text=" Local Ping (LAN) ", padding=15)
        local_card.pack(side=LEFT, fill=BOTH, expand=True, padx=5)
        self.meter_local = ttk.Meter(local_card, metersize=180, padding=10, amounttotal=1000, amountused=0, metertype="semi", subtext="PING ms", interactive=False, stripethickness=10, meterthickness=15, bootstyle=SUCCESS)
        self.meter_local.pack(pady=(10,0))
        self.lbl_local_status = ttk.Label(local_card, text="Status: OFFLINE", font="-weight bold", bootstyle=SECONDARY)
        self.lbl_local_status.pack(pady=10)

        proc_card = ttk.Labelframe(grid, text=" Server API Speed ", padding=15)
        proc_card.pack(side=LEFT, fill=BOTH, expand=True, padx=5)
        self.meter_proc = ttk.Meter(
            proc_card, metersize=180, padding=10, amounttotal=500, amountused=0, 
            metertype="semi", subtext="AVG ms", interactive=False, 
            stripethickness=10, meterthickness=15, bootstyle=INFO, amountformat="{:.0f} ms"
        )
        self.meter_proc.pack(pady=(10,0))
        self.lbl_proc_status = ttk.Label(proc_card, text="Throughput: 0 req", font="-weight bold", bootstyle=INFO)
        self.lbl_proc_status.pack(pady=10)

        cloud_card = ttk.Labelframe(grid, text=" Cloudflare Ping (WAN) ", padding=15)
        cloud_card.pack(side=LEFT, fill=BOTH, expand=True, padx=5)
        self.meter_cloud = ttk.Meter(cloud_card, metersize=180, padding=10, amounttotal=1000, amountused=0, metertype="semi", subtext="PING ms", interactive=False, stripethickness=10, meterthickness=15, bootstyle=SUCCESS)
        self.meter_cloud.pack(pady=(10,0))
        self.lbl_cloud_status = ttk.Label(cloud_card, text="Status: OFFLINE", font="-weight bold", bootstyle=SECONDARY)
        self.lbl_cloud_status.pack(pady=10)

        # --- LIVE MATPLOTLIB GRAPH ---
        if MATPLOTLIB_AVAILABLE:
            chart_frame = ttk.Labelframe(main_frame, text=" Live API Traffic (Requests / Sec) ", padding=10)
            chart_frame.pack(fill=BOTH, expand=True, pady=(15, 0))
            
            self.fig = Figure(figsize=(8, 2), dpi=100, facecolor='#222222')
            self.ax = self.fig.add_subplot(111)
            self.ax.set_facecolor('#222222')
            self.ax.tick_params(colors='white')
            for spine in self.ax.spines.values():
                spine.set_color('#555555')
                
            self.line, = self.ax.plot([], [], color='#4CD37E', linewidth=2)
            self.canvas = FigureCanvasTkAgg(self.fig, master=chart_frame)
            self.canvas.get_tk_widget().pack(fill=BOTH, expand=True)
        else:
            ttk.Label(main_frame, text="(Install 'matplotlib' to view Live Traffic Graph)", font="-size 9 -slant italic", foreground="#888").pack(pady=10)
        
        ttk.Button(main_frame, text="Close Window", bootstyle=SECONDARY, command=self.destroy).pack(pady=(15,0))

    def refresh_meters(self):
        if not self.winfo_exists(): return
            
        with network_latency_lock: snap_net = dict(NETWORK_LATENCY)
        with metrics_lock: snap_metrics = dict(SERVER_METRICS)

        loc_ms = snap_net["local_ms"]
        self.meter_local.configure(amountused=min(loc_ms, 1000))
        if snap_net["local_status"] == "ONLINE":
            self.meter_local.configure(bootstyle=SUCCESS if loc_ms < 150 else WARNING)
            self.lbl_local_status.configure(text="Status: LIVE", bootstyle=SUCCESS)
        else: 
            self.meter_local.configure(amountused=0, bootstyle=SECONDARY)
            self.lbl_local_status.configure(text="Status: OFFLINE", bootstyle=DANGER)

        proc_ms = int(snap_metrics["avg_process_ms"])
        self.meter_proc.configure(amountused=min(proc_ms, 500))
        self.meter_proc.configure(bootstyle=SUCCESS if proc_ms < 100 else (WARNING if proc_ms < 300 else DANGER))
        self.lbl_proc_status.configure(text=f"Total: {snap_metrics['req_count']} req")

        cf_ms = snap_net["cloud_ms"]
        self.meter_cloud.configure(amountused=min(cf_ms, 1000))
        if snap_net["cloud_status"] == "ONLINE":
            self.meter_cloud.configure(bootstyle=SUCCESS if cf_ms < 300 else WARNING)
            self.lbl_cloud_status.configure(text="Status: SECURE", bootstyle=SUCCESS)
        else: 
            self.meter_cloud.configure(amountused=0, bootstyle=SECONDARY)
            self.lbl_cloud_status.configure(text="Status: OFFLINE", bootstyle=DANGER)

        # Draw Matplotlib Graph
        if MATPLOTLIB_AVAILABLE and hasattr(self, 'line'):
            y_data = list(TRAFFIC_HISTORY)
            x_data = list(range(len(y_data)))
            self.line.set_data(x_data, y_data)
            self.ax.set_xlim(0, 59)
            self.ax.set_ylim(0, max(10, max(y_data) + 5)) 
            self.canvas.draw()

        self.after(1000, self.refresh_meters)


class CloudflareLogWindow(ttk.Toplevel):
    def __init__(self, parent, hub):
        super().__init__(parent)
        self.hub = hub
        self.title("Cloudflare Tunnel — Live Logs")
        self.geometry("640x440")
        self.minsize(420, 280)
        self.protocol("WM_DELETE_WINDOW", self.hide)
        
        outer = ttk.Frame(self, padding=10)
        outer.pack(fill=BOTH, expand=True)
        self.log_box = hub._create_log_box(outer, "☁️ Cloudflare Tunnel Status", lambda: None)
        ttk.Button(outer, text="Close", bootstyle="secondary-outline", command=self.hide).pack(pady=(8, 0), anchor=E)
        self.withdraw()  

    def show(self):
        self.deiconify(); self.lift(); self.focus_force(); self.update_idletasks()
        self.geometry(f"+{max(self.hub.winfo_x() + (self.hub.winfo_width() // 2) - (self.winfo_width() // 2), 0)}+{max(self.hub.winfo_y() + (self.hub.winfo_height() // 2) - (self.winfo_height() // 2), 0)}")

    def hide(self): 
        self.withdraw()


class ServerHub(ttk.Window):
    def __init__(self):
        super().__init__(themename="darkly", title="TDE UP 2026 — Event Hub V2.3 (Hardened + Responsive UI)")
        
        sw, sh = self.winfo_screenwidth(), self.winfo_screenheight()
        ww, wh = max(900, min(1600, int(sw * 0.92))), max(600, min(950, int(sh * 0.90)))
        self.geometry(f"{ww}x{wh}+{max(0, (sw - ww) // 2)}+{max(0, (sh - wh) // 2 - 15)}")
        self.minsize(min(1000, ww), min(650, wh))

        self.local_ip = get_local_ip()
        self.http_url = f"http://{self.local_ip}:{HTTP_PORT}"
        self.https_url = f"https://{self.local_ip}:{HTTPS_PORT}"
        self.cloudflare_url = "Offline"
        
        self.SessionMySQL = None
        self.SessionSQLite = None
        self._db_checked = False
        
        threading.Thread(target=self.connect_db, daemon=True).start()

        self.http_thread = None
        self.https_thread = None
        self.cf_process = None
        self._cf_connecting = False  
        
        self.gui_queue = queue.Queue()
        global gui_log_callback
        gui_log_callback = self.log_flask_event
        
        self.build_ui()
        self.process_gui_queue()
        self.refresh_stats()
        self.refresh_hw_meters()

        self.protocol("WM_DELETE_WINDOW", self.on_close)
        threading.Thread(target=self.network_ping_daemon, daemon=True).start()
        threading.Thread(target=stats_refresher_loop, daemon=True).start()
        threading.Thread(target=traffic_monitor_loop, daemon=True).start()

    def connect_db(self):
        try:
            sessions = get_cached_sessions() or {}
            self.SessionMySQL = sessions.get('mysql')
            self.SessionSQLite = sessions.get('sqlite')
        except Exception: pass
        finally: self._db_checked = True

    def network_ping_daemon(self):
        global NETWORK_LATENCY
        session = requests.Session()
        session.headers.update({"User-Agent": "EventHub-Agent/1.0"})
        
        while True:
            start_local = time.time()
            try: 
                session.get(f"http://127.0.0.1:{HTTP_PORT}/api/status", timeout=2)
                l_ms, l_stat = int((time.time() - start_local) * 1000), "ONLINE"
            except Exception: 
                l_ms, l_stat = 0, "OFFLINE"

            if self.cloudflare_url != "Offline":
                start_cf = time.time()
                try: 
                    session.get(f"{self.cloudflare_url}/api/status", timeout=7, verify=False)
                    c_ms, c_stat = int((time.time() - start_cf) * 1000), "ONLINE"
                except Exception as e:
                    c_ms, c_stat = 0, "OFFLINE"
                    if getattr(self, "_last_ping_err", "") != str(e): 
                        self._append_log(self.log_cf, f"[PING ERROR] {str(e)[:100]}...")
                        self._last_ping_err = str(e)
            else: 
                c_ms, c_stat = 0, "OFFLINE"

            with network_latency_lock: 
                NETWORK_LATENCY.update({"local_ms": l_ms, "local_status": l_stat, "cloud_ms": c_ms, "cloud_status": c_stat})
                
            time.sleep(3.0)
            
    def process_gui_queue(self):
        for _ in range(100):
            try: 
                task = self.gui_queue.get_nowait()
                task()
            except queue.Empty: 
                break
        self.after(30, self.process_gui_queue)

    def _append_log(self, widget, message, tag=None):
        segments = list(message) if isinstance(message, (list, tuple)) else [(message, tag or _guess_log_tag(message))]
        
        def append():
            widget.text.configure(state=NORMAL)
            for txt, tg in segments: 
                if tg: widget.text.insert(END, txt, tg)
                else: widget.text.insert(END, txt)
            widget.text.insert(END, "\n"); widget.text.see(END)
            
            lc = int(widget.text.index('end-1c').split('.')[0])
            if lc > MAX_LOG_LINES: widget.text.delete('1.0', f'{lc - MAX_LOG_LINES}.0')
            widget.text.configure(state=DISABLED)
            
        self.gui_queue.put(append)

    def log_flask_event(self, message): 
        self._append_log(self.log_flask, message)

    def clear_system_logs(self):
        self.log_flask.text.configure(state=NORMAL); self.log_flask.text.delete('1.0', END); self.log_flask.text.configure(state=DISABLED)
        self._append_log(self.log_flask, "[INFO] Operator cleared system logs.")

    def clear_network_logs(self):
        self.log_network.text.configure(state=NORMAL); self.log_network.text.delete('1.0', END); self.log_network.text.configure(state=DISABLED)
        self._append_log(self.log_network, "[INFO] Operator cleared network logs.")

    def copy_to_clipboard(self, text):
        if not text or text == "Offline": return
        self.clipboard_clear(); self.clipboard_append(text); self.update()
        self._append_log(self.log_network, f"[CLIPBOARD] Copied: {text}")

    def open_browser(self, url): 
        if url != "Offline": webbrowser.open(url)
        
    def open_telemetry_window(self): 
        NetworkTelemetryWindow(self, self)

    def _configure_custom_styles(self):
        colors = self.style.colors
        self.CARD_BG = colors.get("dark")
        self.SOFT_BORDER = _mix_hex(self.CARD_BG, colors.get("fg"), 0.08)
        BG_BORDER = _mix_hex(colors.get("bg"), colors.get("fg"), 0.10)
        
        self.style.configure("Card.TFrame", background=self.CARD_BG, bordercolor=self.SOFT_BORDER, lightcolor=self.SOFT_BORDER, darkcolor=self.SOFT_BORDER, borderwidth=1, relief="solid")
        self.style.configure("CardTitle.TLabel", background=self.CARD_BG, foreground=_mix_hex(self.CARD_BG, colors.get("fg"), 0.55), font="-size 8 -weight bold")
        
        for key in ("primary", "info", "success", "warning", "danger", "light", "secondary"): 
            self.style.configure(f"CardValue.{key}.TLabel", background=self.CARD_BG, foreground=colors.get(key), font="-size 18 -weight bold")
            
        self.style.configure("CardFlash.TLabel", background=self.CARD_BG, foreground="#FFFFFF", font="-size 18 -weight bold")
        self.style.configure("Soft.TFrame", background=colors.get("bg"), bordercolor=BG_BORDER, lightcolor=BG_BORDER, darkcolor=BG_BORDER, borderwidth=1, relief="solid")
        self.style.configure("TLabelframe", background=colors.get("bg"), bordercolor=BG_BORDER, lightcolor=BG_BORDER, darkcolor=BG_BORDER)
        self.style.configure("TLabelframe.Label", background=colors.get("bg"))
        self.style.configure("LogHeader.TLabel", background="#252526", foreground="#CCCCCC", font="-size 10 -weight bold", padding=10)
        self.style.configure("Treeview.Heading", background=_mix_hex(self.CARD_BG, colors.get("fg"), 0.12), foreground=_mix_hex(self.CARD_BG, colors.get("fg"), 0.82), bordercolor=BG_BORDER, relief="flat", font="-size 9 -weight bold")
        self.style.map("Treeview.Heading", background=[("active", _mix_hex(self.CARD_BG, colors.get("fg"), 0.20))])
        self.style.configure("Treeview", bordercolor=BG_BORDER, borderwidth=1)

    def build_ui(self):
        self._configure_custom_styles()
        self.root_container = ttk.Frame(self)
        self.root_container.pack(fill=BOTH, expand=True)

        sidebar_outer = ttk.Frame(self.root_container, width=280)
        sidebar_outer.pack(side=LEFT, fill=Y)
        sidebar_outer.pack_propagate(False)
        
        self.sidebar_canvas = ttk.Canvas(sidebar_outer, highlightthickness=0, background=self.style.colors.bg)
        sidebar_vsb = ttk.Scrollbar(sidebar_outer, orient=VERTICAL, command=self.sidebar_canvas.yview)
        self.sidebar_canvas.configure(yscrollcommand=sidebar_vsb.set)
        sidebar_vsb.pack(side=RIGHT, fill=Y)
        self.sidebar_canvas.pack(side=LEFT, fill=BOTH, expand=True)

        sidebar = ttk.Frame(self.sidebar_canvas, padding=15)
        sidebar_window = self.sidebar_canvas.create_window((0, 0), window=sidebar, anchor="nw")
        
        sidebar.bind("<Configure>", lambda e: self.sidebar_canvas.configure(scrollregion=self.sidebar_canvas.bbox("all")))
        self.sidebar_canvas.bind("<Configure>", lambda e: self.sidebar_canvas.itemconfig(sidebar_window, width=e.width))
        self.sidebar_canvas.bind("<Enter>", lambda e: self.sidebar_canvas.bind_all("<MouseWheel>", lambda ev: self.sidebar_canvas.yview_scroll(int(-1 * (ev.delta / 120)), "units")))
        self.sidebar_canvas.bind("<Leave>", lambda e: self.sidebar_canvas.unbind_all("<MouseWheel>"))

        ttk.Label(sidebar, text="NETWORK & ROUTING", font="-size 13 -weight bold", bootstyle=INFO).pack(pady=(0, 15), anchor=W)
        
        # --- FLASK ENGINE SECTION ---
        flask_frame = ttk.Labelframe(sidebar, text=" 🌐 High-Speed Engine ", padding=10)
        flask_frame.pack(fill=X, pady=5)
        self.btn_start_flask = ttk.Button(flask_frame, text="▶ Start Engine", bootstyle=SUCCESS, command=self.start_flask)
        self.btn_start_flask.pack(fill=X, pady=3)
        self.btn_stop_flask = ttk.Button(flask_frame, text="⏹ Stop Engine", bootstyle=DANGER, state=DISABLED, command=self.stop_flask)
        self.btn_stop_flask.pack(fill=X, pady=3)
        ttk.Label(flask_frame, text="Network QR (iOS HTTPS):", font="-size 8 -weight bold", foreground="#888").pack(pady=(10, 2))
        
        self.lbl_flask_qr = ttk.Label(flask_frame)
        self.lbl_flask_qr.pack()
        self.update_qr(self.lbl_flask_qr, "OFFLINE")
        
        self.lbl_flask_link = ttk.Label(flask_frame, text="HTTPS Offline", font="-size 8", foreground="gray", cursor="hand2")
        self.lbl_flask_link.pack(pady=5)
        self.lbl_flask_link.bind("<Button-1>", lambda e: self.open_browser(self.https_url) if self.https_thread else None)
        
        flask_btn_row1 = ttk.Frame(flask_frame)
        flask_btn_row1.pack(fill=X, pady=(2, 2))
        ttk.Button(flask_btn_row1, text="HTTPS", bootstyle="success", command=lambda: self.copy_to_clipboard(self.https_url)).pack(side=LEFT, expand=True, fill=X, padx=(0, 2))
        ttk.Button(flask_btn_row1, text="HTTP", bootstyle="info", command=lambda: self.copy_to_clipboard(self.http_url)).pack(side=LEFT, expand=True, fill=X, padx=(2, 0))

        flask_btn_row2 = ttk.Frame(flask_frame)
        flask_btn_row2.pack(fill=X, pady=(2, 5))
        ttk.Button(flask_btn_row2, text="Web (Sec)", bootstyle="outline-success", command=lambda: self.open_browser(self.https_url)).pack(side=LEFT, expand=True, fill=X, padx=(0, 2))
        ttk.Button(flask_btn_row2, text="Web (Local)", bootstyle="outline-info", command=lambda: self.open_browser(self.http_url)).pack(side=LEFT, expand=True, fill=X, padx=(2, 0))


        # --- CLOUDFLARE TUNNEL SECTION ---
        cf_frame = ttk.Labelframe(sidebar, text=" ☁️ Cloudflare Tunnel ", padding=10)
        cf_frame.pack(fill=X, pady=15)
        self.btn_start_cf = ttk.Button(cf_frame, text="▶ Start Tunnel", bootstyle=PRIMARY, state=DISABLED, command=self.start_cf)
        self.btn_start_cf.pack(fill=X, pady=3)
        self.btn_stop_cf = ttk.Button(cf_frame, text="⏹ Stop Tunnel", bootstyle=DANGER, state=DISABLED, command=self.stop_cf)
        self.btn_stop_cf.pack(fill=X, pady=3)
        
        self.cf_log_window, self.log_cf = CloudflareLogWindow(self, self), None
        self.log_cf = self.cf_log_window.log_box
        ttk.Button(cf_frame, text="📜 View Tunnel Logs", bootstyle="outline-secondary", command=self.cf_log_window.show).pack(fill=X, pady=(3, 0))
        
        ttk.Label(cf_frame, text="Public Tunnel QR:", font="-size 8 -weight bold", foreground="#888").pack(pady=(10, 2))
        self.lbl_cf_qr = ttk.Label(cf_frame)
        self.lbl_cf_qr.pack()
        self.update_qr(self.lbl_cf_qr, "OFFLINE")
        
        self.lbl_cf_link = ttk.Label(cf_frame, text="Tunnel Offline", font="-size 8", foreground="gray", cursor="hand2")
        self.lbl_cf_link.pack(pady=5)
        self.lbl_cf_link.bind("<Button-1>", lambda e: self.open_browser(self.cloudflare_url) if self.cloudflare_url != "Offline" else None)
        
        cf_btn_row = ttk.Frame(cf_frame)
        cf_btn_row.pack(fill=X, pady=(2, 5))
        ttk.Button(cf_btn_row, text="Copy URL", bootstyle="primary", command=lambda: self.copy_to_clipboard(self.cloudflare_url)).pack(side=LEFT, expand=True, fill=X, padx=(0, 2))
        ttk.Button(cf_btn_row, text="Browser", bootstyle="secondary", command=lambda: self.open_browser(self.cloudflare_url)).pack(side=LEFT, expand=True, fill=X, padx=(2, 0))


        # --- SIMULATOR SECTION ---
        test_frame = ttk.Labelframe(sidebar, text=" 🧪 Simulator Engine ", padding=10)
        test_frame.pack(fill=X, pady=5)
        self.test_mode = ttk.BooleanVar(value=False)
        self.test_date = ttk.StringVar(value="2026-08-30")
        self.chk_test = ttk.Checkbutton(test_frame, text="Testing Mode OFF", variable=self.test_mode, bootstyle="warning-round-toggle", command=self.toggle_test_mode)
        self.chk_test.pack(anchor=W, pady=5)
        self.cb_test_date = ttk.Combobox(test_frame, textvariable=self.test_date, values=["2026-08-30", "2026-08-31", "2026-09-01"], state=DISABLED)
        self.cb_test_date.pack(fill=X, pady=(5, 0))
        self.cb_test_date.bind("<<ComboboxSelected>>", lambda e: self.on_test_date_changed())


        # --- MAIN CONTENT AREA ---
        content = ttk.Frame(self.root_container, padding=20)
        content.pack(side=LEFT, fill=BOTH, expand=True)
        
        header_container = ttk.Frame(content)
        header_container.pack(fill=X, pady=(0, 15))
        
        left_hdr = ttk.Frame(header_container)
        left_hdr.pack(side=LEFT, fill=Y)
        ttk.Label(left_hdr, text="TDE UP 2026 — COMMAND CENTER", font="-size 18 -weight bold", bootstyle=PRIMARY).pack(anchor=W)
        
        bot_hdr = ttk.Frame(left_hdr)
        bot_hdr.pack(fill=X, pady=(8, 0))
        self.lbl_stat_cf = ttk.Label(bot_hdr, text="● Cloudflare: OFFLINE", bootstyle=SECONDARY, font="-weight bold")
        self.lbl_stat_cf.pack(side=LEFT, padx=(0, 15))
        self.lbl_stat_sqlite = ttk.Label(bot_hdr, text="● SQLITE: CHECKING", bootstyle=INFO, font="-weight bold")
        self.lbl_stat_sqlite.pack(side=LEFT, padx=(0, 15))
        self.lbl_stat_mysql = ttk.Label(bot_hdr, text="● MYSQL: CHECKING", bootstyle=INFO, font="-weight bold")
        self.lbl_stat_mysql.pack(side=LEFT, padx=(0, 15))

        right_hdr = ttk.Frame(header_container)
        right_hdr.pack(side=RIGHT, fill=Y)
        
        actions_f = ttk.Frame(right_hdr)
        actions_f.pack(side=RIGHT, padx=(15, 0))
        ttk.Button(actions_f, text="⟳ Refresh Data", bootstyle="outline-light", command=self.refresh_stats).pack(side=TOP, fill=X, pady=(0, 4))
        ttk.Button(actions_f, text="🎛️ Speedometers", bootstyle="outline-info", command=self.open_telemetry_window).pack(side=TOP, fill=X)
        
        hw_f = ttk.Frame(right_hdr)
        hw_f.pack(side=RIGHT)
        
        self.mini_meter_cpu = ttk.Meter(hw_f, metersize=95, padding=2, amounttotal=100, amountused=0, metertype="semi", interactive=False, stripethickness=7, meterthickness=8, bootstyle=INFO, subtext="CPU", subtextfont="-size 8", textfont="-size 11 -weight bold")
        self.mini_meter_cpu.pack(side=LEFT, padx=5)
        
        self.mini_meter_ram = ttk.Meter(hw_f, metersize=95, padding=2, amounttotal=100, amountused=0, metertype="semi", interactive=False, stripethickness=7, meterthickness=8, bootstyle=WARNING, subtext="RAM", subtextfont="-size 8", textfont="-size 11 -weight bold")
        self.mini_meter_ram.pack(side=LEFT, padx=5)
        
        # FIXED: Removed unsupported textformat option, using amountformat instead
        self.mini_meter_api = ttk.Meter(hw_f, metersize=95, padding=2, amounttotal=500, amountused=0, metertype="semi", interactive=False, stripethickness=7, meterthickness=8, bootstyle=SUCCESS, subtext="API ms", subtextfont="-size 8", textfont="-size 11 -weight bold", amountformat="{:.0f} ms")
        self.mini_meter_api.pack(side=LEFT, padx=5)
        
        net_info_card = ttk.Labelframe(right_hdr, text=" Network Health ", padding=(10, 5))
        net_info_card.pack(side=RIGHT, padx=(0, 15), fill=Y)
        
        self.lbl_hdr_local_ping = ttk.Label(net_info_card, text="LAN Ping: WAIT", font="-size 9 -weight bold", bootstyle=SUCCESS)
        self.lbl_hdr_local_ping.pack(anchor=W, pady=2)
        
        self.lbl_hdr_cloud_ping = ttk.Label(net_info_card, text="WAN Ping: WAIT", font="-size 9 -weight bold", bootstyle=SUCCESS)
        self.lbl_hdr_cloud_ping.pack(anchor=W, pady=2)

        devices_header_row = ttk.Frame(content)
        devices_header_row.pack(fill=X, pady=(5, 5))
        self.lbl_devices_header = ttk.Label(devices_header_row, text="📡 ACTIVE CONNECTED DEVICES", font="-size 11 -weight bold", bootstyle=INFO)
        self.lbl_devices_header.pack(side=LEFT, anchor=W)
        self.lbl_stats_health = ttk.Label(devices_header_row, text="", font="-size 9", bootstyle=WARNING)
        self.lbl_stats_health.pack(side=RIGHT, anchor=E)

        devices_frame = ttk.Frame(content, style="Soft.TFrame")
        devices_frame.pack(fill=X, expand=False, pady=(0, 15)) 
        self.style.configure("Treeview", rowheight=22)
        
        self.tree_devices = ttk.Treeview(devices_frame, columns=("name", "ip", "last_seen", "signal"), show="headings", height=5)
        self.tree_devices.heading("name", text="Device Name")
        self.tree_devices.heading("ip", text="IP Address")
        self.tree_devices.heading("last_seen", text="Last Heartbeat")
        self.tree_devices.heading("signal", text="Signal")
        
        self.tree_devices.column("name", width=300, anchor=W)
        self.tree_devices.column("ip", width=150, anchor=W)
        self.tree_devices.column("last_seen", width=120, anchor=CENTER)
        self.tree_devices.column("signal", width=110, anchor=CENTER)
        self.tree_devices.pack(fill=X, padx=2, pady=2)
        
        self.tree_devices.tag_configure("online", foreground="#3fd66f")
        self.tree_devices.tag_configure("stale", foreground="#ffbb33")
        self.tree_devices.tag_configure("fading", foreground="#ff8844")
        self.tree_devices.tag_configure("empty", foreground="#888")

        ttk.Label(content, text="📊 LIVE TELEMETRY & EVENT METRICS", font="-size 11 -weight bold").pack(anchor=W, pady=(0, 5))
        
        stats_container = ttk.Frame(content)
        stats_container.pack(fill=X, pady=(0, 10))
        
        row1 = ttk.Frame(stats_container)
        row1.pack(fill=X, pady=(0, 5))
        self.stat_vars = {}
        
        self._create_stat_card(row1, "TOTAL ATTENDEES", "0", PRIMARY, "total_att")
        self._create_stat_card(row1, "KIOSK REGISTRATIONS", "0", INFO, "kiosk_reg")
        self._create_stat_card(row1, "SQLITE MIRROR SIZE", "0", SUCCESS, "sqlite_total")
        self._create_stat_card(row1, "ACTIVE SCANNERS", "0", WARNING, "online_scanners")
        
        row2 = ttk.Frame(stats_container)
        row2.pack(fill=X, pady=(0, 0))
        
        self._create_stat_card(row2, "TODAY CHECK-IN", "0", SUCCESS, "chk_today")
        self._create_stat_card(row2, "30th Aug Check-ins", "0", LIGHT, "chk_30")
        self._create_stat_card(row2, "31st Aug Check-ins", "0", LIGHT, "chk_31")
        self._create_stat_card(row2, "1st SEPT Check-ins", "0", LIGHT, "chk_01")
        self._create_stat_card(row2, "TOTAL CHECK-INS", "0", PRIMARY, "chk_total")

        ttk.Label(content, text="⚙️ SYSTEM EVENT LOGS", font="-size 11 -weight bold").pack(anchor=W, pady=(5, 5))
        logs_frame = ttk.Frame(content)
        logs_frame.pack(fill=BOTH, expand=True, pady=(0, 5))
        
        self.log_flask = self._create_log_box(logs_frame, "📟 System & API Logs", self.clear_system_logs)
        self.log_network = self._create_log_box(logs_frame, "🌐 Device & Routing", self.clear_network_logs)
        
        footer = ttk.Frame(content)
        footer.pack(fill=X, pady=(5, 0))
        ttk.Label(footer, text="Engineered for Event Resilience • Powered by EllowDigital", font="-size 9", foreground="#666").pack(side=RIGHT)
        
        self._append_log(self.log_network, f"System Boot: {datetime.now().strftime('%Y-%m-%d %H:%M:%S')}")
        self._append_log(self.log_network, f"Local Network IP Address Detected: {self.local_ip}")

    def on_close(self):
        try:
            if self.http_thread or self.https_thread: 
                self.stop_flask()
            if self.cf_process: 
                self.stop_cf()
        except Exception: pass
        finally: self.destroy()

    def _create_stat_card(self, parent, title, initial_value, style, var_name):
        frame = ttk.Frame(parent, style="Card.TFrame", padding=(8, 4), height=65)
        frame.pack(side=LEFT, fill=BOTH, expand=True, padx=4, pady=2)
        frame.pack_propagate(False)
        
        ttk.Label(frame, text=title, style="CardTitle.TLabel").pack(anchor=CENTER)
        val_lbl = ttk.Label(frame, text=initial_value, style=f"CardValue.{style}.TLabel")
        val_lbl.pack(anchor=CENTER, expand=True, pady=(0, 0))
        
        self.stat_vars[var_name] = {"label": val_lbl, "style": f"CardValue.{style}.TLabel"}

    def _set_stat(self, var_name, new_value):
        entry = self.stat_vars.get(var_name)
        if not entry or entry["label"].cget("text") == str(new_value): return
        entry["label"].configure(text=str(new_value), style="CardFlash.TLabel")
        self.after(350, lambda: entry["label"].configure(style=entry["style"]) if entry["label"].winfo_exists() else None)

    def _create_log_box(self, parent, title, clear_cmd):
        frame = ttk.Frame(parent, style="Soft.TFrame")
        frame.pack(side=LEFT, fill=BOTH, expand=True, padx=6, pady=2)
        
        hdr = ttk.Frame(frame)
        hdr.pack(fill=X)
        ttk.Label(hdr, text=title, style="LogHeader.TLabel").pack(side=LEFT, fill=X, expand=True)
        if clear_cmd: ttk.Button(hdr, text="Clear", bootstyle="secondary-link", command=clear_cmd).pack(side=RIGHT, padx=5)
            
        log_box = ScrolledText(frame, font=("Consolas", 10))
        log_box.pack(fill=BOTH, expand=True, padx=2, pady=2)
        log_box.text.configure(state=DISABLED, bg="#1E1E1E", fg="#D4D4D4", insertbackground="#D4D4D4", selectbackground="#264F78", borderwidth=0)
        
        log_box.text.tag_configure("log_default", foreground="#D4D4D4")
        log_box.text.tag_configure("log_dim", foreground="#6A7178")
        log_box.text.tag_configure("log_success", foreground="#4CD37E")
        log_box.text.tag_configure("log_warning", foreground="#FFB454")
        log_box.text.tag_configure("log_error", foreground="#FF6B6B")
        log_box.text.tag_configure("log_info", foreground="#5DADE2")
        log_box.text.tag_configure("log_register", foreground="#6EC6FF", font=("Consolas", 10, "bold"))
        log_box.text.tag_configure("log_checkin", foreground="#C792EA", font=("Consolas", 10, "bold"))
        
        return log_box

    def update_qr(self, label, data):
        qr = qrcode.QRCode(version=1, box_size=10, border=1)
        qr.add_data(data); qr.make(fit=True)
        img_tk = ImageTk.PhotoImage(qr.make_image(fill_color="black", back_color="white").resize((110, 110), Image.Resampling.LANCZOS))
        label.configure(image=img_tk); label.image = img_tk

    def toggle_test_mode(self):
        global SERVER_TEST_MODE; SERVER_TEST_MODE = self.test_mode.get()
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
        global SERVER_TEST_DATE; SERVER_TEST_DATE = self.test_date.get()
        self._append_log(self.log_network, f"[WARNING] Test date updated globally to: {SERVER_TEST_DATE}"); self.refresh_stats()

    def refresh_hw_meters(self):
        try:
            c = int(psutil.cpu_percent()); r = int(psutil.virtual_memory().percent)
            self.mini_meter_cpu.configure(amountused=c, bootstyle=SUCCESS if c < 60 else (WARNING if c < 85 else DANGER))
            self.mini_meter_ram.configure(amountused=r, bootstyle=SUCCESS if r < 70 else (WARNING if r < 90 else DANGER))
        except Exception: pass
        
        try:
            with metrics_lock: snap_metrics = dict(SERVER_METRICS)
            proc_ms = int(snap_metrics["avg_process_ms"])
            self.mini_meter_api.configure(amountused=min(proc_ms, 500), bootstyle=SUCCESS if proc_ms < 100 else (WARNING if proc_ms < 300 else DANGER))
        except Exception: pass

        with network_latency_lock: snap_net = dict(NETWORK_LATENCY)
        loc_ms, c_ms = snap_net["local_ms"], snap_net["cloud_ms"]
        
        if snap_net["local_status"] == "ONLINE":
            self.lbl_hdr_local_ping.configure(text=f"LAN Ping: {loc_ms} ms", bootstyle=SUCCESS if loc_ms < 150 else WARNING)
        else:
            self.lbl_hdr_local_ping.configure(text="LAN Ping: DOWN", bootstyle=DANGER)
            
        if snap_net["cloud_status"] == "ONLINE":
            self.lbl_hdr_cloud_ping.configure(text=f"WAN Ping: {c_ms} ms", bootstyle=SUCCESS if c_ms < 300 else WARNING)
        else:
            self.lbl_hdr_cloud_ping.configure(text="WAN Ping: DOWN", bootstyle=SECONDARY)

        self.after(1000, self.refresh_hw_meters)

    def refresh_stats(self):
        current_time = time.time()
        with device_lock:
            active_ips = [ip for ip, data in ACTIVE_DEVICES.items() if current_time - data['last_seen'] < DEVICE_ONLINE_WINDOW]
            device_info = {ip: dict(ACTIVE_DEVICES[ip]) for ip in active_ips}

        self._set_stat("online_scanners", len(active_ips))
        if hasattr(self, 'lbl_devices_header'): 
            self.lbl_devices_header.configure(text=f"📡 ACTIVE CONNECTED DEVICES ({len(active_ips)})")

        for row in self.tree_devices.get_children(): self.tree_devices.delete(row)
            
        if active_ips:
            for ip in sorted(active_ips, key=lambda i: device_info[i]['name'].lower()):
                sec_ago = max(0, int(current_time - device_info[ip]['last_seen']))
                sig, tag = ("🟢 Live", "online") if sec_ago < 8 else (("🟡 Slow", "stale") if sec_ago < 15 else ("🟠 Fading", "fading"))
                self.tree_devices.insert("", END, values=(device_info[ip]['name'], ip, "just now" if sec_ago < 2 else f"{sec_ago}s ago", sig), tags=(tag,))
        else: 
            self.tree_devices.insert("", END, values=("No devices connected yet — awaiting heartbeat...", "", "", ""), tags=("empty",))

        if not self._db_checked:
            self.lbl_stat_mysql.configure(text="● MYSQL: CHECKING", bootstyle=INFO); self.lbl_stat_sqlite.configure(text="● SQLITE: CHECKING", bootstyle=INFO)
        else:
            self.lbl_stat_mysql.configure(text="● MYSQL: LIVE", bootstyle=SUCCESS) if self.SessionMySQL else self.lbl_stat_mysql.configure(text="● MYSQL: OFFLINE", bootstyle=DANGER)
            self.lbl_stat_sqlite.configure(text="● SQLITE: MIRROR ACTIVE", bootstyle=SUCCESS) if self.SessionSQLite else self.lbl_stat_sqlite.configure(text="● SQLITE: FAULT", bootstyle=DANGER)

        with stats_lock: snap = dict(STATS_CACHE)
            
        for k, v in zip(["total_att", "kiosk_reg", "sqlite_total", "chk_30", "chk_31", "chk_01", "chk_today", "chk_total"], 
                        [snap["total_attendees"], snap["total_registrations"], snap["total_attendees"], snap["chk_30"], snap["chk_31"], snap["chk_01"], snap["today_scans"], snap["total_scans"]]): 
            self._set_stat(k, v)

        if hasattr(self, 'lbl_stats_health'):
            stale = (current_time - snap["last_refreshed"]) if snap["last_refreshed"] else None
            if snap["last_error"] and stale and stale > STATS_REFRESH_INTERVAL_SEC * 4:
                self.lbl_stats_health.configure(text=f"⚠ Stats stale ({int(stale)}s): {snap['last_error']}", bootstyle=WARNING)
            else: self.lbl_stats_health.configure(text="")

        self.after(4000, self.refresh_stats)

    def start_flask(self):
        self.btn_start_flask.configure(state=DISABLED)
        self._append_log(self.log_flask, f"[{datetime.now().strftime('%H:%M:%S')}] Booting Engine...")
        start_db_writers()
        self._append_log(self.log_flask, f"[SYSTEM] {DB_WRITER_THREADS} DB writer threads ready.")
        try:
            self.http_thread = WaitressHttpThread(app, '0.0.0.0', HTTP_PORT)
            self.https_thread = HttpsFlaskThread(app, '0.0.0.0', HTTPS_PORT)
            self.http_thread.start()
            self.https_thread.start()
        except Exception as e:
            self._append_log(self.log_flask, f"[ERROR] Start failed: {e}")
            if self.http_thread: self.http_thread.shutdown(); self.http_thread = None
            self.https_thread = None; stop_db_writers(); self.btn_start_flask.configure(state=NORMAL)
            Messagebox.show_error(f"Engine failed:\n{e}", "Failed", parent=self); return

        self.btn_stop_flask.configure(state=NORMAL); self.btn_start_cf.configure(state=NORMAL)
        self.update_qr(self.lbl_flask_qr, self.https_url)
        self.lbl_flask_link.configure(text=self.https_url, foreground="#4D9CE6")
        self._append_log(self.log_flask, f"[SYSTEM] Waitress HTTP listening: {self.http_url}")
        self._append_log(self.log_flask, f"[SYSTEM] Cheroot HTTPS listening: {self.https_url}")
        
    def stop_flask(self):
        if self.btn_stop_cf['state'] == NORMAL: 
            self.stop_cf()
            
        self.btn_stop_flask.configure(state=DISABLED)
        self.btn_start_flask.configure(state=NORMAL)
        self.btn_start_cf.configure(state=DISABLED)
        self.update_qr(self.lbl_flask_qr, "OFFLINE")
        self.lbl_flask_link.configure(text="Server Offline", foreground="gray")
        
        stop_db_writers()
        
        if self.http_thread: 
            self.http_thread.shutdown()
            self.http_thread = None
            
        if self.https_thread: 
            self.https_thread.shutdown()
            self.https_thread = None
            
        self._append_log(self.log_flask, f"[{datetime.now().strftime('%H:%M:%S')}] Engine stopped.")

    def _animate_cf_connecting(self, tick=0):
        if not self.winfo_exists() or not self._cf_connecting: 
            return
        self.lbl_stat_cf.configure(text=f"● Cloudflare: CONNECTING{'.' * (tick % 4)}", bootstyle=WARNING)
        self.after(450, lambda: self._animate_cf_connecting(tick + 1))

    def _mark_cf_live(self): 
        self._cf_connecting = False
        self.lbl_stat_cf.configure(text="● Cloudflare: LIVE", bootstyle=SUCCESS)

    def start_cf(self):
        if not self.http_thread: 
            return self._append_log(self.log_cf, "[ERROR] Start Local Engine FIRST!")
            
        self.btn_start_cf.configure(state=DISABLED)
        self.btn_stop_cf.configure(state=NORMAL)
        self._cf_connecting = True
        self._animate_cf_connecting()
        self._append_log(self.log_cf, f"[{datetime.now().strftime('%H:%M:%S')}] Requesting secure tunnel...")

        def _run_cf():
            try:
                self.cf_process = subprocess.Popen(["cloudflared", "tunnel", "--url", f"http://{self.local_ip}:{HTTP_PORT}", "--http-host-header", "localhost", "--no-tls-verify"], stdout=subprocess.PIPE, stderr=subprocess.STDOUT, text=True, bufsize=1, creationflags=subprocess.CREATE_NO_WINDOW if platform.system() == "Windows" else 0)
                url_found = False
                for line in self.cf_process.stdout:
                    cl = re.sub(r'\x1b\[[0-9;]*m', '', line).strip()
                    self._append_log(self.log_cf, cl)
                    
                    if not url_found:
                        m = re.search(r"https://[a-zA-Z0-9-]+\.trycloudflare\.com", cl)
                        if m:
                            tunnel_url = m.group(0)
                            self.cloudflare_url = tunnel_url
                            url_found = True
                            
                            self._append_log(self.log_cf, "[INFO] Waiting 30s for DNS propagation...")
                            time.sleep(30)
                            
                            self.gui_queue.put(lambda u=tunnel_url: self.update_qr(self.lbl_cf_qr, u))
                            self.gui_queue.put(lambda u=tunnel_url: self.lbl_cf_link.configure(text=u, foreground="#4D9CE6"))
                            self.gui_queue.put(self._mark_cf_live)
                            self._append_log(self.log_cf, f"[SUCCESS] Tunnel active: {self.cloudflare_url}")
            except FileNotFoundError:
                self.gui_queue.put(self.stop_cf)
                self._append_log(self.log_cf, "[ERROR] 'cloudflared' not found in PATH.")
            except Exception as e: 
                self.gui_queue.put(self.stop_cf)
                self._append_log(self.log_cf, f"[ERROR] Tunnel failed: {e}")
                
        threading.Thread(target=_run_cf, daemon=True).start()
        
    def stop_cf(self):
        self.btn_stop_cf.configure(state=DISABLED)
        self._cf_connecting = False  
        self.btn_start_cf.configure(state=NORMAL if self.http_thread else DISABLED)
        self.lbl_stat_cf.configure(text="● Cloudflare: OFFLINE", bootstyle=SECONDARY)
        
        if self.cf_process:
            try: 
                if platform.system() == "Windows":
                    subprocess.run(['taskkill', '/F', '/T', '/PID', str(self.cf_process.pid)], stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)
                else:
                    self.cf_process.terminate()
            except Exception: 
                pass
            finally: 
                self.cf_process = None
                
        self.cloudflare_url = "Offline"
        self.update_qr(self.lbl_cf_qr, "OFFLINE")
        self.lbl_cf_link.configure(text="Tunnel Offline", foreground="gray")
        self._append_log(self.log_cf, f"[{datetime.now().strftime('%H:%M:%S')}] Tunnel closed.")


if __name__ == "__main__":
    app_window = ServerHub()
    app_window.mainloop()