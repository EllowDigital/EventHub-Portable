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
from requests.adapters import HTTPAdapter
from urllib3.util.retry import Retry
import urllib3
import ctypes
import html
import sys
import functools
import hmac
import math
import gzip
from io import BytesIO
from datetime import datetime, timezone, timedelta

os.environ["QT_ENABLE_HIGHDPI_SCALING"] = "1"
os.environ["QT_AUTO_SCREEN_SCALE_FACTOR"] = "1"

from PySide6.QtCore import Qt, QTimer, QRectF, QPointF
from PySide6.QtGui import (
    QIcon, QPixmap, QImage, QColor, QFont,
    QPainter, QPen
)
from PySide6.QtWidgets import (
    QApplication, QMainWindow, QWidget, QVBoxLayout, QHBoxLayout,
    QGridLayout, QLabel, QPushButton, QTableWidget,
    QTableWidgetItem, QTabWidget, QPlainTextEdit, QGroupBox, QCheckBox,
    QComboBox, QHeaderView, QMenu, QInputDialog, QMessageBox, QFrame,
    QSplitter, QScrollArea, QSizePolicy
)

import qrcode
import webbrowser
import psutil

from flask import Flask, render_template, request, jsonify, Response
from werkzeug.exceptions import HTTPException
from waitress import create_server 

urllib3.disable_warnings(urllib3.exceptions.InsecureRequestWarning)
# Silence urllib3 connection pool logging to prevent console spam when the engine is off

# Silence urllib3 connection pool logging to prevent console spam
logging.getLogger("urllib3").setLevel(logging.ERROR)
logging.getLogger("urllib3.connectionpool").setLevel(logging.ERROR)

class SuppressWaitressSocketErrorFilter(logging.Filter):
    """Filters out internal Waitress WinError 10038 spam during server shutdown."""
    def filter(self, record):
        # Catch and block logs that contain the specific Windows Socket OSError
        if record.exc_info:
            _, exc_value, _ = record.exc_info
            if isinstance(exc_value, OSError) and getattr(exc_value, 'winerror', None) == 10038:
                return False
        
        # Fallback text matching just in case
        msg = record.getMessage()
        if "WinError 10038" in msg or "not a socket" in msg:
            return False
        return True

# Apply the filter directly to Waitress's internal logger
logging.getLogger("waitress").addFilter(SuppressWaitressSocketErrorFilter())
logging.getLogger("waitress.queue").addFilter(SuppressWaitressSocketErrorFilter())

# ----------------------------------------------------------

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
    from cryptography.x509.oid import NameOID, ExtendedKeyUsageOID
    CRYPTOGRAPHY_AVAILABLE = True
except ImportError:
    CRYPTOGRAPHY_AVAILABLE = False

try:
    import indiapins
    INDIAPINS_AVAILABLE = True
except ImportError:
    INDIAPINS_AVAILABLE = False

from sqlalchemy.orm.attributes import flag_modified
from sqlalchemy.exc import IntegrityError, OperationalError

app_window = None

def global_exception_handler(exc_type, exc_value, exc_traceback):
    logging.error("Uncaught GUI Exception intercepted. App remains running.", exc_info=(exc_type, exc_value, exc_traceback))

sys.excepthook = global_exception_handler

def _configure_windows_platform():
    if platform.system() != "Windows": return
    try:
        from ctypes import windll, c_void_p
        dpi_awareness_set = False
        try:
            if windll.user32.SetProcessDpiAwarenessContext(c_void_p(-4)): dpi_awareness_set = True
        except Exception: pass
        if not dpi_awareness_set:
            try: windll.shcore.SetProcessDpiAwareness(2); dpi_awareness_set = True
            except Exception: pass
        if not dpi_awareness_set:
            try: windll.user32.SetProcessDPIAware()
            except Exception: pass
        try: windll.shell32.SetCurrentProcessExplicitAppUserModelID("TDEUP2026.EventHub.ServerHub")
        except Exception: pass
        try: windll.kernel32.SetThreadExecutionState(0x80000000 | 0x00000001) 
        except Exception: pass
    except Exception: pass

_configure_windows_platform()

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

TELEMETRY_DATA = {
    "cpu": 0, "ram": 0, "net_type": "Disconnected",
    "dl_mbps": 0.0, "ul_mbps": 0.0, "total_mbps": 0.0,
    "iface_name": "N/A", "link_speed": 0,
    "total_dl_mb": 0.0, "total_ul_mb": 0.0,
    "cpu_ghz_used": 0.0, "cpu_ghz_total": 0.0,
    "ram_gb_used": 0.0, "ram_gb_total": 0.0,
    "local_ip": get_local_ip(),
    "all_ipv4": []
}
_telemetry_lock = threading.Lock()

_global_shutdown_event = threading.Event()
_flask_shutdown_event = threading.Event() 
_db_shutdown_event = threading.Event()

def _telemetry_worker():
    last_time = time.time()
    try: last_io = psutil.net_io_counters()
    except Exception: last_io = None
    while not _global_shutdown_event.is_set():
        try:
            cpu = int(psutil.cpu_percent(interval=None))
            vmem = psutil.virtual_memory()
            ram = int(vmem.percent)
            ram_gb_used = (vmem.total - vmem.available) / (1024**3)
            ram_gb_total = vmem.total / (1024**3)
            
            try:
                freq = psutil.cpu_freq()
                cpu_ghz_used = freq.current / 1000.0 if freq else 0.0
                cpu_ghz_total = freq.max / 1000.0 if freq else 0.0
                if cpu_ghz_total == 0.0: cpu_ghz_total = cpu_ghz_used
            except Exception:
                cpu_ghz_used = cpu_ghz_total = 0.0

            stats = psutil.net_if_stats()
            up_ifaces = [iface for iface, s in stats.items() if s.isup and iface != 'lo' and not iface.startswith('Loopback')]
            eth_iface = next((i for i in up_ifaces if 'ethernet' in i.lower() or 'eth' in i.lower()), None)
            usb_iface = next((i for i in up_ifaces if 'usb' in i.lower()), None)
            wifi_iface = next((i for i in up_ifaces if 'wi-fi' in i.lower() or 'wireless' in i.lower() or 'wlan' in i.lower()), None)
            
            if eth_iface: active_iface, iface_type = eth_iface, "ETHERNET"
            elif usb_iface: active_iface, iface_type = usb_iface, "USB ETH"
            elif wifi_iface: active_iface, iface_type = wifi_iface, "WI-FI"
            elif up_ifaces: active_iface, iface_type = up_ifaces[0], "NETWORK"
            else: active_iface, iface_type = None, "OFFLINE"
            
            dl_mbps = ul_mbps = total_mbps = dl_mb = ul_mb = 0.0
            link_speed = 0
            current_io = psutil.net_io_counters()
            current_time = time.time()
            if last_io and current_io:
                elapsed = current_time - last_time
                if elapsed > 0:
                    dl_mbps = ((current_io.bytes_recv - last_io.bytes_recv) / 1048576) / elapsed
                    ul_mbps = ((current_io.bytes_sent - last_io.bytes_sent) / 1048576) / elapsed
                    total_mbps = dl_mbps + ul_mbps
                dl_mb = current_io.bytes_recv / 1048576
                ul_mb = current_io.bytes_sent / 1048576
            last_io, last_time = current_io, current_time
            if active_iface and active_iface in stats: link_speed = stats[active_iface].speed
            
            current_ip = get_local_ip()
            
            all_ipv4 = set()
            try:
                for interface, snics in psutil.net_if_addrs().items():
                    for snic in snics:
                        if snic.family == socket.AF_INET and snic.address != "127.0.0.1":
                            all_ipv4.add(snic.address)
            except Exception: pass
            all_ipv4.add(current_ip)

            with _telemetry_lock:
                TELEMETRY_DATA.update({
                    "cpu": cpu, "ram": ram, "net_type": iface_type,
                    "dl_mbps": dl_mbps, "ul_mbps": ul_mbps, "total_mbps": total_mbps,
                    "iface_name": active_iface or "N/A", "link_speed": link_speed,
                    "total_dl_mb": dl_mb, "total_ul_mb": ul_mb,
                    "cpu_ghz_used": cpu_ghz_used, "cpu_ghz_total": cpu_ghz_total,
                    "ram_gb_used": ram_gb_used, "ram_gb_total": ram_gb_total,
                    "local_ip": current_ip,
                    "all_ipv4": sorted(list(all_ipv4))
                })
        except Exception as e:
            logging.debug(f"Telemetry Worker Error: {e}")
        _global_shutdown_event.wait(1.0)

threading.Thread(target=_telemetry_worker, daemon=True).start()

try:
    from app.schema import Attendee, OfflineKioskAttendee, get_database_sessions
except ModuleNotFoundError:
    try:
        from schema import Attendee, OfflineKioskAttendee, get_database_sessions
    except ModuleNotFoundError:
        Attendee, OfflineKioskAttendee, get_database_sessions = None, None, lambda: None

BASE_DIR = os.path.dirname(os.path.abspath(__file__))
LOG_DIR = os.path.join(BASE_DIR, 'logs')
CONFIG_DIR = os.path.join(BASE_DIR, 'config')
os.makedirs(LOG_DIR, exist_ok=True)
os.makedirs(CONFIG_DIR, exist_ok=True)

APP_VERSION = "2.6"
HTTP_PORT = 5000 
HTTPS_PORT = 5001
CERT_DIR = os.path.join(CONFIG_DIR, 'certs')

# ==========================================
# OPTIMIZED CONCURRENCY & CONNECTION POOLING
# ==========================================
HTTP_WORKER_THREADS = 64          # Reduced from 128 -> 64 to avoid GIL & thread switching contention
DB_WRITER_THREADS = 12            # Controlled concurrency to eliminate MySQL row/table lock contention
DB_JOB_QUEUE_MAXSIZE = 50000      # Retained high capacity queue
DB_JOB_TIMEOUT = 12               # Max wait time for DB jobs

# Target Database Pool Defaults (Configured in DB Engine Factory)
MYSQL_POOL_SIZE = 16
MYSQL_MAX_OVERFLOW = 4
MYSQL_POOL_TIMEOUT = 10
MYSQL_POOL_RECYCLE = 1800
MYSQL_POOL_PRE_PING = True

MAX_SSE_CLIENTS = 40             
COMPRESS_MIN_BYTES = 500          
CF_MAX_RECONNECT_ATTEMPTS = 5    

STATS_REFRESH_INTERVAL_SEC = 300  
EVENT_DATE_LABELS = {"2026-08-30": "30 August", "2026-08-31": "31 August", "2026-09-01": "1 September"}

EVENT_TIMEZONE = timezone(timedelta(hours=5, minutes=30))

def _current_event_date_str():
    return SERVER_TEST_DATE if SERVER_TEST_MODE else datetime.now(EVENT_TIMEZONE).strftime('%Y-%m-%d')
MAX_LOG_LINES = 5000 

LOG_FILE = os.path.join(LOG_DIR, 'server_hub.log')
_file_handler = RotatingFileHandler(LOG_FILE, maxBytes=25_000_000, backupCount=10, encoding='utf-8')
_file_handler.setFormatter(logging.Formatter('%(asctime)s [%(levelname)s] %(message)s'))
logging.basicConfig(level=logging.INFO, handlers=[_file_handler])

template_dir = os.path.join(BASE_DIR, 'templates')
static_dir = os.path.join(BASE_DIR, 'static')
app = Flask(__name__, template_folder=template_dir, static_folder=static_dir)

app.config['MAX_CONTENT_LENGTH'] = 5 * 1024 * 1024  
app.config['JSONIFY_PRETTYPRINT_REGULAR'] = False  
app.config['SEND_FILE_MAX_AGE_DEFAULT'] = 86400  

gui_log_callback = None
SERVER_TEST_MODE = False
SERVER_TEST_DATE = "2026-08-30"

CUSTOM_DEVICE_NAMES = {}
DEVICE_NAMES_FILE = os.path.join(CONFIG_DIR, 'device_names.json')

try:
    if os.path.exists(DEVICE_NAMES_FILE):
        with open(DEVICE_NAMES_FILE, 'r') as f: CUSTOM_DEVICE_NAMES = json.load(f)
except Exception as e:
    logging.error(f"Failed to load custom device names: {e}")

_device_names_file_lock = threading.Lock()

def _save_device_names():
    with _device_names_file_lock:
        try:
            with device_lock: snapshot = dict(CUSTOM_DEVICE_NAMES)
            tmp_path = DEVICE_NAMES_FILE + '.tmp'
            with open(tmp_path, 'w') as f: json.dump(snapshot, f, indent=4)
            os.replace(tmp_path, DEVICE_NAMES_FILE)
        except Exception as e:
            logging.error(f"Failed to save custom device names: {e}")

ACTIVE_DEVICES = {}
DEVICE_MESSAGES = {}
SCAN_CLIENTS = []
scan_clients_lock = threading.Lock()
device_lock = threading.Lock()
DEVICE_ONLINE_WINDOW = 25  

DB_SESSIONS_CACHE = None
_db_cache_lock = threading.Lock()
_db_cache_last_failure = 0.0
DB_SESSIONS_RETRY_COOLDOWN = 5  

NETWORK_LATENCY = {"local_ms": 0, "cloud_ms": 0, "local_status": "OFFLINE", "cloud_status": "OFFLINE"}
network_latency_lock = threading.Lock()
SERVER_METRICS = {"avg_process_ms": 0.0, "req_count": 0}
metrics_lock = threading.Lock()

TRAFFIC_HISTORY = collections.deque([0] * 60, maxlen=60)
_current_sec_requests = 0
_traffic_lock = threading.Lock()

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
                if (time.time() - _db_cache_last_failure) < DB_SESSIONS_RETRY_COOLDOWN: return None
                try: DB_SESSIONS_CACHE = get_database_sessions()
                except Exception as e:
                    logging.exception(f"Database session retrieval failed: {e}")
                    _db_cache_last_failure = time.time()
                    return None
    return DB_SESSIONS_CACHE

def _ensure_root_ca(ca_cert_path, ca_key_path):
    if os.path.exists(ca_cert_path) and os.path.exists(ca_key_path): return
    try:
        key = rsa.generate_private_key(public_exponent=65537, key_size=4096)
        now = datetime.now(timezone.utc)
        subject = issuer = x509.Name([
            x509.NameAttribute(NameOID.ORGANIZATION_NAME, "EllowDigital Event Root CA"),
            x509.NameAttribute(NameOID.ORGANIZATIONAL_UNIT_NAME, "EllowLabs"),
            x509.NameAttribute(NameOID.COMMON_NAME, "TDEUP 2026 Event Root CA"),
        ])
        ski = x509.SubjectKeyIdentifier.from_public_key(key.public_key())
        cert = (
            x509.CertificateBuilder()
            .subject_name(subject).issuer_name(issuer).public_key(key.public_key())
            .serial_number(x509.random_serial_number())
            .not_valid_before(now).not_valid_after(now + timedelta(days=3650))  
            .add_extension(x509.BasicConstraints(ca=True, path_length=0), critical=True)
            .add_extension(ski, critical=False)
            .add_extension(
                x509.KeyUsage(
                    digital_signature=True, content_commitment=False, key_encipherment=False,
                    data_encipherment=False, key_agreement=False, key_cert_sign=True,
                    crl_sign=True, encipher_only=False, decipher_only=False,
                ),
                critical=True,
            ).sign(key, hashes.SHA384())
        )
        with open(ca_key_path, "wb") as f: f.write(key.private_bytes(encoding=serialization.Encoding.PEM, format=serialization.PrivateFormat.TraditionalOpenSSL, encryption_algorithm=serialization.NoEncryption()))
        with open(ca_cert_path, "wb") as f: f.write(cert.public_bytes(serialization.Encoding.PEM))
    except Exception as e: raise RuntimeError(f"Root CA generation failed: {e}")

def _write_server_cert(cert_path, key_path, ca_cert_path, ca_key_path, local_ip):
    try:
        with open(ca_cert_path, "rb") as f: ca_cert = x509.load_pem_x509_certificate(f.read())
        with open(ca_key_path, "rb") as f: ca_key = serialization.load_pem_private_key(f.read(), password=None)
        server_key = rsa.generate_private_key(public_exponent=65537, key_size=2048)
        now = datetime.now(timezone.utc)
        subject = x509.Name([
            x509.NameAttribute(NameOID.ORGANIZATION_NAME, "EllowDigital"),
            x509.NameAttribute(NameOID.ORGANIZATIONAL_UNIT_NAME, "EllowLabs"),
            x509.NameAttribute(NameOID.COMMON_NAME, "TDEUP 2026 Event Hub Server"),
        ])
        san_entries = [x509.DNSName("localhost"), x509.IPAddress(ipaddress.ip_address("127.0.0.1"))]
        try: san_entries.append(x509.IPAddress(ipaddress.ip_address(local_ip)))
        except ValueError: pass
            
        try:
            for interface, snics in psutil.net_if_addrs().items():
                for snic in snics:
                    if snic.family == socket.AF_INET and snic.address not in ["127.0.0.1", local_ip]:
                        try: san_entries.append(x509.IPAddress(ipaddress.ip_address(snic.address)))
                        except ValueError: pass
        except Exception: pass
        
        ski = x509.SubjectKeyIdentifier.from_public_key(server_key.public_key())
        aki = x509.AuthorityKeyIdentifier.from_issuer_public_key(ca_key.public_key())
        cert = (
            x509.CertificateBuilder()
            .subject_name(subject).issuer_name(ca_cert.subject).public_key(server_key.public_key())
            .serial_number(x509.random_serial_number())
            .not_valid_before(now).not_valid_after(now + timedelta(days=365)) 
            .add_extension(x509.SubjectAlternativeName(san_entries), critical=False)
            .add_extension(x509.BasicConstraints(ca=False, path_length=None), critical=True)
            .add_extension(ski, critical=False).add_extension(aki, critical=False)
            .add_extension(x509.KeyUsage(digital_signature=True, content_commitment=False, key_encipherment=True, data_encipherment=False, key_agreement=False, key_cert_sign=False, crl_sign=False, encipher_only=False, decipher_only=False), critical=True)
            .add_extension(x509.ExtendedKeyUsage([ExtendedKeyUsageOID.SERVER_AUTH]), critical=False)
            .sign(ca_key, hashes.SHA256())
        )
        with open(key_path, "wb") as f: f.write(server_key.private_bytes(encoding=serialization.Encoding.PEM, format=serialization.PrivateFormat.TraditionalOpenSSL, encryption_algorithm=serialization.NoEncryption()))
        with open(cert_path, "wb") as f: f.write(cert.public_bytes(serialization.Encoding.PEM))
    except Exception as e: raise RuntimeError(f"Server Certificate generation failed: {e}")

def ensure_ssl_certificate(local_ip):
    if not CRYPTOGRAPHY_AVAILABLE: raise RuntimeError("Cryptography package required.")
    try: os.makedirs(CERT_DIR, exist_ok=True)
    except OSError: raise
    ca_cert_path = os.path.join(CERT_DIR, 'rootCA.pem')
    ca_key_path = os.path.join(CERT_DIR, 'rootCA.key')
    cert_path = os.path.join(CERT_DIR, 'hub_cert.pem')
    key_path = os.path.join(CERT_DIR, 'hub_key.pem')
    _ensure_root_ca(ca_cert_path, ca_key_path)

    current_system_ips = set()
    try:
        for interface, snics in psutil.net_if_addrs().items():
            for snic in snics:
                if snic.family == socket.AF_INET and snic.address != "127.0.0.1": current_system_ips.add(snic.address)
    except Exception: pass
    current_system_ips.add(local_ip)

    reuse_existing = False
    if os.path.exists(cert_path) and os.path.exists(key_path):
        try:
            with open(cert_path, "rb") as f: c = x509.load_pem_x509_certificate(f.read())
            try: cert_expiry = c.not_valid_after_utc
            except AttributeError: cert_expiry = c.not_valid_after.replace(tzinfo=timezone.utc)
            is_valid_time = cert_expiry > datetime.now(timezone.utc) + timedelta(days=30)
            san_ext = c.extensions.get_extension_for_class(x509.SubjectAlternativeName)
            cert_ips = {str(ip) for ip in san_ext.value.get_values_for_type(x509.IPAddress)}
            all_ips_present = all(ip in cert_ips for ip in current_system_ips)
            if is_valid_time and all_ips_present: reuse_existing = True
        except Exception: reuse_existing = False
            
    if not reuse_existing: _write_server_cert(cert_path, key_path, ca_cert_path, ca_key_path, local_ip)
    return cert_path, key_path

@app.before_request
def _start_request_timer(): request._start_time = time.perf_counter()

@app.after_request
def log_request(response):
    if request.path.startswith('/static') or request.path.startswith('/favicon.ico') or request.path == '/api/stream-scans': return response
    try:
        global _current_sec_requests
        with _traffic_lock: _current_sec_requests += 1
        
        duration_ms = (time.perf_counter() - getattr(request, '_start_time', time.perf_counter())) * 1000
        if metrics_lock.acquire(blocking=False):
            try:
                SERVER_METRICS["req_count"] += 1
                if SERVER_METRICS["avg_process_ms"] == 0: SERVER_METRICS["avg_process_ms"] = duration_ms
                else: SERVER_METRICS["avg_process_ms"] = (SERVER_METRICS["avg_process_ms"] * 0.9) + (duration_ms * 0.1)
            finally: metrics_lock.release()
    except Exception: pass
    return response

@app.after_request
def compress_response(response):
    try:
        accept_encoding = request.headers.get('Accept-Encoding', '')
        if (
            'gzip' in accept_encoding.lower()
            and response.status_code < 300
            and not response.direct_passthrough
            and not response.is_streamed
            and 'Content-Encoding' not in response.headers
            and response.content_length is not None
            and response.content_length >= COMPRESS_MIN_BYTES
            and response.mimetype in ('application/json', 'text/html', 'text/css', 'application/javascript', 'text/javascript', 'text/plain')
        ):
            buf = BytesIO()
            with gzip.GzipFile(mode='wb', fileobj=buf, compresslevel=6) as gz:
                gz.write(response.get_data())
            response.set_data(buf.getvalue())
            response.headers['Content-Encoding'] = 'gzip'
            response.headers['Content-Length'] = str(response.content_length)
        response.headers.setdefault('Vary', 'Accept-Encoding')
    except Exception:
        logging.debug("Response compression skipped", exc_info=True)
    return response

@app.errorhandler(HTTPException)
def handle_http_exception(e):
    return jsonify({"status": "error", "message": e.description or e.name}), e.code

@app.errorhandler(Exception)
def handle_global_exception(e):
    logging.exception(f"Unhandled exception on {request.method} {request.path}")
    return jsonify({"status": "error", "message": "An unexpected server fault occurred. Please retry."}), 500

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
    if action_type == "REGISTER": segments = [(f"[{time_str}] ", "log_timestamp"), (f"[{device_name}] ", "log_device"), (f"REGISTER ", "log_register"), (f"{details} ", "log_default"), (f"[{status_code}]", status_tag)]
    elif action_type == "CHECKIN": segments = [(f"[{time_str}] ", "log_timestamp"), (f"[{device_name}] ", "log_device"), (f"CHECKIN  ", "log_checkin"), (f"{details} ", "log_default"), (f"[{status_code}]", status_tag)]
    else: segments = [(f"[{time_str}] ", "log_timestamp"), (f"[{device_name}] ", "log_device"), (f"{action_type} ", "log_default"), (f"[{status_code}]", status_tag)]
    if gui_log_callback: gui_log_callback(segments)
    
    plain_msg = f"[{device_name}] {action_type}: {details} (status {status_code})"
    # Treat 4xx errors as standard operation info to prevent false system warning logs
    if status_code >= 500: logging.error(plain_msg)
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
        except queue.Full: pass 
        except Exception:
            with scan_clients_lock:
                if q in SCAN_CLIENTS: SCAN_CLIENTS.remove(q)

def traffic_monitor_loop():
    global _current_sec_requests
    while not _global_shutdown_event.is_set():
        _global_shutdown_event.wait(1.0)
        with _traffic_lock:
            hits = _current_sec_requests
            _current_sec_requests = 0
        TRAFFIC_HISTORY.append(hits)

def _compute_stats_snapshot(deep_scan=False):
    sessions = get_cached_sessions()
    mysql_factory = sessions.get('mysql') if sessions else None
    if not mysql_factory: return None
    session = mysql_factory()
    try:
        total_attendees = session.query(Attendee).count()
        total_registrations = session.query(OfflineKioskAttendee).count()
        result = {"total_attendees": total_attendees, "total_registrations": total_registrations, "is_deep_scan": False}
        if deep_scan:
            chk_30 = session.query(Attendee).filter(Attendee.checkin_history.like('%"30 August"%')).count()
            chk_31 = session.query(Attendee).filter(Attendee.checkin_history.like('%"31 August"%')).count()
            chk_01 = session.query(Attendee).filter(Attendee.checkin_history.like('%"1 September"%')).count()
            result.update({"chk_30": chk_30, "chk_31": chk_31, "chk_01": chk_01, "total_scans": chk_30 + chk_31 + chk_01, "is_deep_scan": True})
        return result
    finally: session.close()

def stats_refresher_loop():
    loop_counter = 0
    deep_scan_every_n_loops = max(1, int(STATS_REFRESH_INTERVAL_SEC / 3))  
    while not _global_shutdown_event.is_set():
        try:
            needs_deep_scan = (loop_counter % deep_scan_every_n_loops == 0)
            snapshot = _compute_stats_snapshot(deep_scan=needs_deep_scan)
            if snapshot is not None:
                today_date = _current_event_date_str()
                with stats_lock:
                    STATS_CACHE["total_attendees"] = snapshot["total_attendees"]
                    STATS_CACHE["total_registrations"] = snapshot["total_registrations"]
                    if snapshot["is_deep_scan"]:
                        STATS_CACHE["chk_30"] = snapshot["chk_30"]
                        STATS_CACHE["chk_31"] = snapshot["chk_31"]
                        STATS_CACHE["chk_01"] = snapshot["chk_01"]
                        STATS_CACHE["total_scans"] = snapshot["total_scans"]
                    STATS_CACHE["today_scans"] = {"2026-08-30": STATS_CACHE["chk_30"], "2026-08-31": STATS_CACHE["chk_31"], "2026-09-01": STATS_CACHE["chk_01"]}.get(today_date, 0)
                    STATS_CACHE["last_refreshed"] = time.time()
                    STATS_CACHE["last_error"] = None
        except Exception as e:
            with stats_lock: STATS_CACHE["last_error"] = str(e)
            logging.debug(f"Stats refresh encountered error: {e}")
        loop_counter += 1
        _global_shutdown_event.wait(3.0) 

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
    except queue.Full: return 503, {"status": "error", "message": "System is very busy. Please wait a moment and try again."}
    try: return job.future.result(timeout=DB_JOB_TIMEOUT)
    except concurrent.futures.TimeoutError: return 504, {"status": "error", "message": "The request took too long. Please try again."}
    except Exception as e: 
        logging.exception(f"DB job failed execution for kind={kind}")
        return 500, {"status": "error", "message": "An unexpected system glitch occurred. Please retry."}

def _handle_checkin_job(payload):
    identifier = payload["identifier"]
    search_type = payload["search_type"]
    device_name = payload["device_name"]
    iso_timestamp = payload["iso_timestamp"]
    if not identifier: return 400, {"status": "error", "message": "Please scan a valid QR code or enter an ID."}
    sessions = get_cached_sessions() or {}
    mysql_factory = sessions.get('mysql')
    if not mysql_factory: return 503, {"status": "error", "message": "Server database is disconnected. Please call tech support."}
    
    retries = 3
    while retries > 0:
        session = mysql_factory()
        try:
            # SHORT TRANSACTION: Fetch & Lock
            attendee = None
            if search_type == 'phone':
                attendee = session.query(Attendee).filter_by(mobile=identifier).with_for_update().first() or session.query(OfflineKioskAttendee).filter_by(mobile=identifier).with_for_update().first()
            else:
                attendee = session.query(Attendee).filter_by(attendee_id=identifier).with_for_update().first() or session.query(OfflineKioskAttendee).filter_by(attendee_id=identifier).with_for_update().first()
            
            if not attendee:
                session.rollback()
                log_event_clean("CHECKIN", device_name, f"Not found: {identifier}", 404)
                broadcast_scan(None, "ERROR", f"Not found: {identifier}", device_name, iso_timestamp)
                return 404, {"status": "error", "message": f"Record not found. Please direct attendee to the Help Desk."}
            
            history = attendee.checkin_history
            if isinstance(history, str):
                try: history = json.loads(history)
                except Exception: history = {}
            if not isinstance(history, dict): history = {}
            
            current_date_str = _current_event_date_str()
            date_map = EVENT_DATE_LABELS
            if current_date_str not in date_map: 
                session.rollback()
                return 400, {"status": "error", "message": "System date error. Check-in is not active for today."}
            
            today_key = date_map[current_date_str]
            att_days = attendee.attendance_days or []
            if isinstance(att_days, str):
                try: att_days = json.loads(att_days)
                except Exception: att_days = []
            
            if today_key not in att_days:
                session.rollback()
                log_event_clean("CHECKIN", device_name, f"Denied (No pass {today_key})", 403)
                broadcast_scan(attendee, "ERROR", f"Denied (No pass {today_key})", device_name, iso_timestamp)
                return 403, {"status": "error", "message": f"Access Denied: Attendee does not have a valid pass for today ({today_key})."}
            
            if today_key in history:
                session.rollback()
                friendly_msg = f"Already Scanned! {attendee.full_name} checked in earlier today."
                log_event_clean("CHECKIN", device_name, friendly_msg, 400)
                broadcast_scan(attendee, "DUPLICATE", friendly_msg, device_name, iso_timestamp)
                return 400, {"status": "error", "message": friendly_msg}
            
            # UPDATE & COMMIT IMMEDIATELY (Minimizes lock holding time)
            history[today_key] = {"timestamp": iso_timestamp, "source": "offline_hub", "device": device_name, "date_code": current_date_str, "display_date": today_key}
            attendee.checkin_history = history
            flag_modified(attendee, "checkin_history")
            attendee.needs_cloud_sync = True
            attendee.needs_sheet_sync = True
            attendee.needs_local_sync = False
            attendee.local_modified = True
            session.commit()
            
            with stats_lock:
                if current_date_str == "2026-08-30": STATS_CACHE["chk_30"] += 1
                elif current_date_str == "2026-08-31": STATS_CACHE["chk_31"] += 1
                elif current_date_str == "2026-09-01": STATS_CACHE["chk_01"] += 1
                STATS_CACHE["total_scans"] += 1
                STATS_CACHE["today_scans"] += 1
            
            success_msg = f"{attendee.full_name} ({attendee.attendee_id})"
            log_event_clean("CHECKIN", device_name, success_msg, 200)
            broadcast_scan(attendee, "SUCCESS", success_msg, device_name, iso_timestamp)
            return 200, {"status": "success", "message": success_msg, "time": iso_timestamp}
        except OperationalError as oe:
            session.rollback()
            retries -= 1
            if retries == 0:
                logging.warning(f"Database lock contention encountered during checkin: {oe}")
                log_event_clean("CHECKIN", device_name, "DB Locked (OperationalError)", 503)
                return 503, {"status": "error", "message": "Database is temporarily locked. Please try again."}
            time.sleep(random.uniform(0.01, 0.05))
        except Exception:
            session.rollback()
            logging.exception(f"Checkin job failed unexpectedly for device={device_name}, identifier={identifier}")
            log_event_clean("CHECKIN", device_name, "DB Error", 500)
            return 500, {"status": "error", "message": "A system error occurred. Please try scanning again."}
        finally:
            try: session.close()
            except Exception as e: logging.debug(f"Error closing DB session: {e}")

def _handle_register_job(payload):
    data = payload["data"]
    device_label = payload["device_label"]
    iso_timestamp = payload["iso_timestamp"]
    mobile_number = payload["mobile_number"]
    sessions = get_cached_sessions() or {}
    mysql_factory = sessions.get('mysql')
    if not mysql_factory: return 503, {"status": "error", "message": "Server database is disconnected. Please call tech support."}
    
    retries = 3
    while retries > 0:
        session = mysql_factory()
        try:
            existing_main = session.query(Attendee).filter_by(mobile=mobile_number).first()
            if existing_main: 
                session.rollback()
                return 200, {"status": "already_registered", "attendee_id": existing_main.attendee_id}
            existing_kiosk = session.query(OfflineKioskAttendee).filter_by(mobile=mobile_number).first()
            if existing_kiosk: 
                session.rollback()
                return 200, {"status": "already_registered", "attendee_id": existing_kiosk.attendee_id}
            
            prefix = {"GENERAL":"G", "BUSINESS":"B", "MEDIA":"M", "EXHIBITOR":"E"}.get(data.get('attendee_type', 'GENERAL').upper(), "G")
            new_attendee_id = f"TDE26-{prefix}-" + "".join(random.choices("ABCDEFGHJKLMNPQRSTUVWXYZ23456789", k=6))
            
            today_date = _current_event_date_str()
            checkin_history_dict = {}
            if today_date in EVENT_DATE_LABELS:
                key = EVENT_DATE_LABELS[today_date]
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
            session.add(new_kiosk_reg)
            session.commit()
            
            with stats_lock:
                STATS_CACHE["total_registrations"] += 1
                if today_date == "2026-08-30": STATS_CACHE["chk_30"] += 1; STATS_CACHE["total_scans"] += 1; STATS_CACHE["today_scans"] += 1
                elif today_date == "2026-08-31": STATS_CACHE["chk_31"] += 1; STATS_CACHE["total_scans"] += 1; STATS_CACHE["today_scans"] += 1
                elif today_date == "2026-09-01": STATS_CACHE["chk_01"] += 1; STATS_CACHE["total_scans"] += 1; STATS_CACHE["today_scans"] += 1
            
            log_event_clean("REGISTER", device_label, f"{data.get('full_name')} ({new_attendee_id})", 200)
            return 200, {"status": "success", "message": "Saved successfully.", "attendee_id": new_attendee_id}
        except IntegrityError:
            session.rollback()
            existing = session.query(Attendee).filter_by(mobile=mobile_number).first() or session.query(OfflineKioskAttendee).filter_by(mobile=mobile_number).first()
            if existing: return 200, {"status": "already_registered", "attendee_id": existing.attendee_id}
        except OperationalError as oe:
            session.rollback()
            retries -= 1
            if retries == 0:
                logging.warning(f"Database lock contention encountered during registration: {oe}")
                return 503, {"status": "error", "message": "Database is temporarily locked. Please try again."}
            time.sleep(random.uniform(0.01, 0.05)) 
        except Exception:
            session.rollback()
            logging.exception(f"Registration job failed unexpectedly for device={device_label}")
            return 500, {"status": "error", "message": "Something went wrong saving this registration. Please try again."}
        finally:
            try: session.close()
            except Exception as e: logging.debug(f"Error closing DB session: {e}")

def db_writer_loop(worker_id):
    while not _global_shutdown_event.is_set() and not _db_shutdown_event.is_set():
        try:
            job = DB_WRITE_QUEUE.get(timeout=1.0)
            if job is None: 
                DB_WRITE_QUEUE.task_done()
                break
            try:
                if job.kind == "checkin": result = _handle_checkin_job(job.payload)
                elif job.kind == "register": result = _handle_register_job(job.payload)
                else: result = (500, {"status": "error", "message": "Unknown job payload type."})
                if not job.future.done(): job.future.set_result(result)
            except Exception as e:
                logging.exception(f"DB writer job '{job.kind}' raised unexpectedly (worker {worker_id})")
                if not job.future.done(): job.future.set_exception(e)
            finally:
                DB_WRITE_QUEUE.task_done()
        except queue.Empty: continue
        except Exception as e: logging.debug(f"DB writer loop error: {e}")

def start_db_writers():
    _db_shutdown_event.clear()
    for i in range(DB_WRITER_THREADS):
        t = threading.Thread(target=db_writer_loop, args=(i + 1,), daemon=True, name=f"DBWriter-{i+1}")
        t.start()
        _db_writer_threads.append(t)

def stop_db_writers():
    _db_shutdown_event.set()
    while not DB_WRITE_QUEUE.empty():
        try: DB_WRITE_QUEUE.get_nowait()
        except queue.Empty: break
    for _ in range(len(_db_writer_threads)): 
        try: DB_WRITE_QUEUE.put(None, timeout=0.1)
        except queue.Full: pass
    for t in _db_writer_threads: t.join(timeout=0.2) 
    _db_writer_threads.clear()

@app.route('/manifest.json')
def serve_manifest(): return app.send_static_file('manifest.json')

@app.route('/sw.js')
def serve_service_worker(): return app.send_static_file('sw.js')

@app.route('/')
def index(): return render_template('index.html')

@app.route('/scanner')
def scanner(): return render_template('check_in.html')

@app.route('/register')
def register(): return render_template('registration.html')

@app.route('/stats')
def stats(): return render_template('network_stats.html')

@app.route('/api/health', methods=['GET'])
def health_check():
    return jsonify({"status": "ok", "version": APP_VERSION}), 200

@app.route('/api/status', methods=['GET', 'POST'])
def get_server_status():
    if request.method == 'GET':
        return jsonify({"status": "online", "version": APP_VERSION}), 200
        
    ip = request.remote_addr
    data = request.get_json(silent=True) or {}
    reported_name = data.get('device_name', 'Unknown Device')
    device_id = data.get('device_id')
    battery = data.get('battery', 'N/A')
    active_page = data.get('page', '/')

    if reported_name == "null": reported_name = "Unknown Device"
    if not device_id: device_id = f"{ip}::{reported_name}"
        
    pending_msg = None
    with device_lock:
        display_name = CUSTOM_DEVICE_NAMES.get(device_id, reported_name)
        if device_id in DEVICE_MESSAGES:
            pending_msg = DEVICE_MESSAGES.pop(device_id)
        ACTIVE_DEVICES[device_id] = {
            'last_seen': time.time(),
            'name': display_name,
            'original_name': reported_name,
            'ip': ip,
            'battery': battery,
            'page': active_page
        }
    return jsonify({"test_mode": SERVER_TEST_MODE, "test_date": SERVER_TEST_DATE, "message": pending_msg, "canonical_name": display_name}), 200

@app.route('/api/device/message', methods=['POST'])
def send_device_message():
    data = request.get_json(silent=True) or {}
    device_id = data.get('id')
    message = data.get('message', '').strip()
    if not device_id or not message: return jsonify({"status": "error", "message": "Missing device ID or message."}), 400
    with device_lock: DEVICE_MESSAGES[device_id] = message
    return jsonify({"status": "success", "message": "Message queued for device."}), 200

@app.route('/api/device/rename', methods=['POST'])
def rename_device():
    data = request.get_json(silent=True) or {}
    device_id = data.get('id') or data.get('ip')
    new_name = data.get('new_name', '').strip()
    if not device_id or not new_name: return jsonify({"status": "error", "message": "Missing device ID or new name."}), 400
    with device_lock:
        CUSTOM_DEVICE_NAMES[device_id] = new_name
        if device_id in ACTIVE_DEVICES: ACTIVE_DEVICES[device_id]['name'] = new_name
    _save_device_names()
    return jsonify({"status": "success", "message": "Device renamed."}), 200

@app.route('/api/network-data', methods=['GET'])
def get_network_data():
    current_time = time.time()
    active_devices = {}
    with device_lock:
        for d_id, data in list(ACTIVE_DEVICES.items()):
            if current_time - data['last_seen'] < DEVICE_ONLINE_WINDOW: active_devices[d_id] = data
            else: del ACTIVE_DEVICES[d_id]
    with stats_lock:
        global_stats = {"total_scans": STATS_CACHE["total_scans"], "total_registrations": STATS_CACHE["total_registrations"], "today_scans": STATS_CACHE["today_scans"]}
    return jsonify({"active_devices": active_devices, "global_stats": global_stats}), 200

@app.route('/api/stream-scans')
def stream_scans():
    with scan_clients_lock: current_clients = len(SCAN_CLIENTS)
    if current_clients >= MAX_SSE_CLIENTS:
        return jsonify({"status": "error", "message": "Too many live dashboards connected right now. Please close one and retry."}), 503, {"Retry-After": "5"}

    def event_stream():
        q = queue.Queue(maxsize=100)
        with scan_clients_lock: SCAN_CLIENTS.append(q)
        try:
            while not _flask_shutdown_event.is_set():
                try: 
                    yield f"data: {json.dumps(q.get(timeout=1))}\n\n"
                except queue.Empty: 
                    yield ": heartbeat\n\n"
        except GeneratorExit: pass 
        finally:
            with scan_clients_lock:
                if q in SCAN_CLIENTS: SCAN_CLIENTS.remove(q)
    return Response(event_stream(), mimetype='text/event-stream', headers={'Cache-Control': 'no-cache', 'Connection': 'keep-alive', 'X-Accel-Buffering': 'no'})

@app.route('/api/checkin', methods=['POST'])
def process_checkin():
    data = request.get_json(silent=True) or {}
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
    data = request.get_json(silent=True) or {}
    mobile_number = str(data.get('mobile', '')).strip()
    full_name = str(data.get('full_name', '')).strip()
    if not full_name: return jsonify({"status": "error", "message": "Full name is required."}), 400
    if not mobile_number or len(mobile_number) < 10: return jsonify({"status": "error", "message": "A valid 10-digit mobile number is required."}), 400
    payload = {
        "data": data,
        "mobile_number": mobile_number,
        "device_label": data.get('device_name', f"Kiosk ({request.user_agent.platform or 'Unknown'} - {request.remote_addr})"),
        "iso_timestamp": datetime.now(timezone.utc).strftime('%Y-%m-%dT%H:%M:%S.000Z')
    }
    status_code, body = _submit_db_job("register", payload)
    return jsonify(body), status_code

@app.route('/api/check_mobile', methods=['GET'])
def check_mobile():
    mobile_number = request.args.get('mobile', '').strip()
    if not mobile_number: return jsonify({"status": "error", "message": "Please enter a valid mobile number."}), 400
    sessions = get_cached_sessions() or {}
    mysql_factory = sessions.get('mysql')
    if not mysql_factory: return jsonify({"status": "error", "message": "System database disconnected."}), 503
    session = mysql_factory()
    try:
        em = session.query(Attendee).filter_by(mobile=mobile_number).first()
        if em: return jsonify({"status": "already_registered", "attendee_id": em.attendee_id}), 200
        ek = session.query(OfflineKioskAttendee).filter_by(mobile=mobile_number).first()
        if ek: return jsonify({"status": "already_registered", "attendee_id": ek.attendee_id}), 200
        return jsonify({"status": "not_found"}), 200
    except Exception:
        logging.exception(f"check_mobile failed for mobile={mobile_number}")
        return jsonify({"status": "error", "message": "Could not check mobile number right now. Try again."}), 500
    finally:
        try: session.close()
        except Exception as e: logging.debug(f"Error closing session: {e}")

@app.route('/api/pincode/<code>', methods=['GET'])
def lookup_pincode(code):
    code = (code or '').strip()
    if not code.isdigit() or len(code) != 6: return jsonify({"status": "error", "message": "Enter a valid 6-digit pincode."}), 400
    if not INDIAPINS_AVAILABLE: return jsonify({"status": "unavailable", "message": "Pincode lookup not installed on this hub."}), 503
    try:
        matches = indiapins.matching(code)
        if not matches: return jsonify({"status": "not_found"}), 200
        first = matches[0]
        return jsonify({"status": "success", "district": first.get("District", ""), "state": first.get("State", "")}), 200
    except Exception:
        logging.exception(f"lookup_pincode failed for code={code}")
        return jsonify({"status": "error", "message": "Lookup failed."}), 500

@app.route('/api/attendees', methods=['GET'])
def get_all_attendees():
    limit = max(1, min(request.args.get('limit', 500, type=int), 1000))
    page = request.args.get('page', 1, type=int)
    offset = max(0, (page - 1) * limit)
    sessions = get_cached_sessions() or {}
    mysql_factory = sessions.get('mysql')
    if not mysql_factory: return jsonify({"status": "error", "message": "System database disconnected."}), 503
    session = mysql_factory()
    try:
        main_total = session.query(Attendee).count()
        if offset < main_total:
            main_att = session.query(Attendee).order_by(Attendee.created_at.asc(), Attendee.id.asc()).offset(offset).limit(limit).all()
            kiosk_offset, kiosk_limit = 0, limit - len(main_att)
        else:
            main_att = []
            kiosk_offset, kiosk_limit = offset - main_total, limit
        kiosk_att = []
        if kiosk_limit > 0: kiosk_att = session.query(OfflineKioskAttendee).order_by(OfflineKioskAttendee.created_at.asc(), OfflineKioskAttendee.id.asc()).offset(kiosk_offset).limit(kiosk_limit).all()
        results = []
        for att in (main_att + kiosk_att):
            att_dict = {
                "id": att.id, "attendee_id": att.attendee_id, "full_name": att.full_name, "mobile": att.mobile, "email": att.email,
                "gender": att.gender.name if hasattr(att.gender, 'name') else str(att.gender),
                "attendee_type": att.attendee_type.name if hasattr(att.attendee_type, 'name') else str(att.attendee_type),
                "business_name": att.business_name, "business_category": att.business_category, "city": att.city, "state": att.state,
                "pincode": att.pincode, "needs_cloud_sync": getattr(att, 'needs_cloud_sync', False),
                "checkin_history": att.checkin_history if isinstance(att.checkin_history, dict) else {},
                "created_at": att.created_at.isoformat() + "Z" if att.created_at else None
            }
            results.append(att_dict)
        return jsonify(results), 200
    except Exception:
        logging.exception("get_all_attendees failed")
        return jsonify({"status": "error", "message": "Failed to load attendees list. Please refresh."}), 500
    finally:
        try: session.close()
        except Exception as e: logging.debug(f"Error closing session: {e}")

class WaitressHttpThread(threading.Thread):
    def __init__(self, app, host, port):
        super().__init__(daemon=True, name="WaitressHttpServer")
        self.app = app
        self.host = host
        self.port = port
        self._is_shutting_down = False
        self.server = None
        self.ctx = app.app_context()
        self.ctx.push()
        
    def run(self):
        try:
            self.server = create_server(self.app, host=self.host, port=self.port, threads=HTTP_WORKER_THREADS, connection_limit=8192, channel_timeout=60, cleanup_interval=30, outbuf_overflow=10485760)
            self.server.run()
        except OSError as e:
            # Ignore Windows socket closure error if triggered by intentional shutdown
            if not self._is_shutting_down and getattr(e, 'winerror', None) != 10038:
                logging.error(f"Waitress Server OSError: {e}")
        except Exception as e:
            if not self._is_shutting_down:
                logging.error(f"Waitress Server Exception: {e}")
                
    def shutdown(self):
        self._is_shutting_down = True
        if self.server:
            try:
                self.server.close()
            except Exception as e:
                logging.debug(f"Waitress close suppressed: {e}")

class HttpsFlaskThread(threading.Thread):
    def __init__(self, app, host, port, numthreads=HTTP_WORKER_THREADS):
        super().__init__(daemon=True)  
        if cheroot_wsgi is None: raise RuntimeError("Cheroot required.")
        cert_path, key_path = ensure_ssl_certificate(get_local_ip())
        self.ctx = app.app_context()
        self.ctx.push()
        self.server = cheroot_wsgi.Server(bind_addr=(host, port), wsgi_app=app, numthreads=numthreads, request_queue_size=8192)
        self.server.timeout = 60
        self.server.keep_alive_timeout = 30
        self.server.ssl_adapter = BuiltinSSLAdapter(certificate=cert_path, private_key=key_path)
    def run(self):
        try: self.server.start()
        except Exception as e: logging.debug(f"Cheroot server stopped: {e}")
    def shutdown(self): self.server.stop()

class SpeedometerGauge(QWidget):
    REFERENCE_FRAME_MS = 16.0  

    def __init__(self, subtext="CPU", unit="%", max_val=100, good_is_high=False, parent=None, response_speed=0.30):
        super().__init__(parent)
        self.subtext = subtext
        self.unit = unit
        self.max_val = max_val
        self.current_val = 0.0
        self.target_val = 0.0
        self.good_is_high = good_is_high
        self.response_speed = response_speed
        self.setMinimumSize(70, 60)
        self.setSizePolicy(QSizePolicy.Expanding, QSizePolicy.Expanding)

    def set_target(self, val):
        self.target_val = float(val)
        
    def set_subtext(self, new_subtext):
        if self.subtext != new_subtext:
            self.subtext = new_subtext
            self.update()

    def tick(self, elapsed_ms=16.0):
        diff = self.target_val - self.current_val
        if abs(diff) > 0.02:
            alpha = 1.0 - (1.0 - self.response_speed) ** (max(elapsed_ms, 1.0) / self.REFERENCE_FRAME_MS)
            self.current_val += diff * alpha
            self.update()
        elif self.current_val != self.target_val:
            self.current_val = self.target_val
            self.update()

    def _zone_color(self, ratio):
        stops = [(0.0, (133, 133, 133)), (0.35, (215, 186, 125)), (1.0, (78, 201, 176))] if self.good_is_high \
            else [(0.0, (78, 201, 176)), (0.72, (215, 186, 125)), (1.0, (244, 71, 71))]
        for (r0, c0), (r1, c1) in zip(stops, stops[1:]):
            if ratio <= r1:
                t = 0.0 if r1 == r0 else (ratio - r0) / (r1 - r0)
                return QColor(*(int(c0[i] + (c1[i] - c0[i]) * t) for i in range(3)))
        return QColor(*stops[-1][1])

    def paintEvent(self, event):
        painter = QPainter(self)
        painter.setRenderHint(QPainter.Antialiasing)
        painter.setRenderHint(QPainter.TextAntialiasing)

        w = self.width()
        h = self.height()
        diameter = min(w, h - 14)
        margin_x = (w - diameter) / 2.0
        margin_y = 2.0
        arc_rect = QRectF(margin_x + 4, margin_y + 4, diameter - 8, diameter - 8)
        arc_width = max(3.5, diameter * 0.08)

        ratio = min(max(self.current_val / self.max_val if self.max_val > 0 else 0.0, 0.0), 1.0)
        dynamic_color = self._zone_color(ratio)

        cx, cy = arc_rect.center().x(), arc_rect.center().y()
        r_outer = diameter / 2.0 - 1
        painter.setPen(QPen(QColor("#3A3A3E"), 1.2))
        for i in range(11):
            frac = i / 10.0
            angle_rad = math.radians(200 - 220 * frac)
            r_inner = r_outer - (arc_width + 3 if i % 5 == 0 else arc_width + 1)
            x1, y1 = cx + r_outer * math.cos(angle_rad), cy - r_outer * math.sin(angle_rad)
            x2, y2 = cx + r_inner * math.cos(angle_rad), cy - r_inner * math.sin(angle_rad)
            painter.drawLine(QPointF(x1, y1), QPointF(x2, y2))

        pen_bg = QPen(QColor("#333333"), arc_width, Qt.DotLine, Qt.RoundCap)
        painter.setPen(pen_bg)
        painter.drawArc(arc_rect, 200 * 16, -220 * 16)

        active_span = -int(220 * ratio * 16)
        if active_span != 0:
            glow_color = QColor(dynamic_color)
            glow_color.setAlpha(60)
            painter.setPen(QPen(glow_color, arc_width * 2.2, Qt.SolidLine, Qt.RoundCap))
            painter.drawArc(arc_rect, 200 * 16, active_span)

            pen_fg = QPen(dynamic_color, arc_width, Qt.SolidLine, Qt.RoundCap)
            painter.setPen(pen_fg)
            painter.drawArc(arc_rect, 200 * 16, active_span)

        painter.setPen(dynamic_color)
        font_size = max(9, int(diameter * 0.23))
        painter.setFont(QFont("Consolas", font_size, QFont.Bold))
        
        if "MB" in self.unit or self.max_val <= 10.0:
            val_text = f"{self.current_val:.1f}"
        else: 
            val_text = str(int(round(self.current_val)))
            
        val_rect = QRectF(margin_x, margin_y + (diameter * 0.18), diameter, diameter * 0.45)
        painter.drawText(val_rect, Qt.AlignCenter, val_text)

        painter.setPen(QColor("#AAAAAA"))
        sub_font_size = max(7, int(diameter * 0.11))
        painter.setFont(QFont("Segoe UI", sub_font_size, QFont.Normal))
        lbl_rect = QRectF(0, h - 14, w, 14)
        painter.drawText(lbl_rect, Qt.AlignCenter, self.subtext)


class AnimatedMeter:
    def __init__(self, meter_widget):
        self.meter = meter_widget
    def set_target(self, val): self.meter.set_target(val)
    def set_subtext(self, text): self.meter.set_subtext(text)
    def tick(self, elapsed_ms=16.0): self.meter.tick(elapsed_ms)


class ServerHub(QMainWindow):
    def __init__(self):
        super().__init__()
        self.setWindowTitle(f"EventHub Portable (v{APP_VERSION}) - Command Center")
        self.resize(1300, 780)
        self.setMinimumSize(960, 580)
        
        app_icon_path = os.path.join(BASE_DIR, "assets", "EventHub.ico")
        if os.path.exists(app_icon_path):
            self.setWindowIcon(QIcon(app_icon_path))
        
        self.setStyleSheet("""
            QMainWindow, QWidget { 
                background-color: #161618; 
                color: #D4D4D4; 
                font-family: 'Segoe UI', system-ui, sans-serif; 
                font-size: 12px; 
            }
            QScrollArea { border: none; background: transparent; }
            QGroupBox { 
                border: 1px solid #2D2D30; 
                border-radius: 5px; 
                margin-top: 8px; 
                padding-top: 10px; 
                background: #1C1C1E; 
            }
            QGroupBox::title { 
                subcontrol-origin: margin; 
                left: 8px; 
                padding: 0 4px; 
                color: #569CD6; 
                font-weight: bold; 
                font-size: 11px;
            }
            QPushButton { 
                background-color: #2D2D30; 
                color: #FFF; 
                border: 1px solid #3E3E42; 
                border-radius: 4px; 
                padding: 4px 8px; 
                font-weight: bold; 
                font-size: 11px;
            }
            QPushButton:hover { background-color: #3E3E42; border-color: #569CD6; }
            QPushButton:pressed { background-color: #1E1E22; border-color: #4EC9B0; }
            QPushButton:disabled { background-color: #161618; color: #555555; border-color: #2A2A2C; }
            
            QPushButton#btnStartEngine { background-color: #107C41; color: white; border: 1px solid #107C41; }
            QPushButton#btnStartEngine:hover { background-color: #0c5e31; }
            QPushButton#btnStartEngine:pressed { background-color: #084021; }
            QPushButton#btnStartEngine:disabled { background-color: #0c5e31; color: #ffffff; border: 1px solid #0c5e31; font-weight: bold; }
            
            QPushButton#btnStopEngine { background-color: transparent; color: #F44747; border: 1px solid #F44747; }
            QPushButton#btnStopEngine:hover { background-color: rgba(244, 71, 71, 0.1); }
            QPushButton#btnStopEngine:pressed { background-color: rgba(244, 71, 71, 0.2); }
            QPushButton#btnStopEngine:disabled { background-color: transparent; color: #555555; border: 1px solid #2A2A2C; }
            
            QPushButton#btnStartTunnel { background-color: #005A9E; color: white; border: 1px solid #005A9E; }
            QPushButton#btnStartTunnel:hover { background-color: #004578; }
            QPushButton#btnStartTunnel:pressed { background-color: #003358; }
            QPushButton#btnStartTunnel:disabled { background-color: #004578; color: #ffffff; border: 1px solid #004578; font-weight: bold; }
            
            QPushButton#btnStopTunnel { background-color: transparent; color: #F44747; border: 1px solid #F44747; }
            QPushButton#btnStopTunnel:hover { background-color: rgba(244, 71, 71, 0.1); }
            QPushButton#btnStopTunnel:pressed { background-color: rgba(244, 71, 71, 0.2); }
            QPushButton#btnStopTunnel:disabled { background-color: transparent; color: #555555; border: 1px solid #2A2A2C; }
            
            QPushButton#btnWhatsApp { background-color: #107C41; color: white; border: 1px solid #107C41; }
            QPushButton#btnWhatsApp:hover { background-color: #0c5e31; }
            QPushButton#btnWhatsApp:pressed { background-color: #084021; }

            QComboBox {
                background-color: #2D2D30;
                color: #FFFFFF;
                border: 1px solid #3E3E42;
                border-radius: 4px;
                padding: 4px 8px;
                font-size: 11px;
            }
            QComboBox:disabled { background-color: #1C1C1E; color: #555555; border-color: #2D2D30; }
            QComboBox QAbstractItemView {
                background-color: #252528;
                color: #FFFFFF;
                border: 1px solid #3E3E42;
                selection-background-color: #094771;
                selection-color: #FFFFFF;
            }
            
            QCheckBox { color: #D4D4D4; font-size: 11px; font-weight: bold; }
            QCheckBox::indicator { width: 14px; height: 14px; border: 1px solid #3E3E42; border-radius: 3px; background-color: #1C1C1E; }
            QCheckBox::indicator:checked { background-color: #4EC9B0; border: 1px solid #4EC9B0; image: url(none); }

            QTableWidget { 
                background-color: #19191B; 
                gridline-color: #28282B; 
                border: 1px solid #2D2D30; 
                border-radius: 4px;
                selection-background-color: #094771; 
                font-size: 11px;
            }
            QTableCornerButton::section { background-color: #202022; border: 1px solid #2D2D30; }
            QHeaderView::section { 
                background-color: #202022; 
                color: #569CD6; 
                padding: 4px 6px; 
                border: 1px solid #28282B; 
                border-bottom: 2px solid #2D2D30;
                font-weight: bold; 
                font-size: 11px;
            }
            
            QPlainTextEdit { 
                background-color: #0D0D0F; 
                color: #D4D4D4; 
                font-family: 'Consolas', monospace; 
                font-size: 8pt; 
                border: 1px solid #2D2D30; 
                border-radius: 4px;
            }
            QTabWidget::pane { border: 1px solid #2D2D30; background: #1C1C1E; border-radius: 4px; top: -1px; }
            QTabBar::tab { background: #252528; color: #888888; padding: 5px 12px; border: 1px solid #2D2D30; font-size: 11px; border-top-left-radius: 4px; border-top-right-radius: 4px; }
            QTabBar::tab:selected { background: #1C1C1E; color: #569CD6; font-weight: bold; border-bottom: none; }
            QSplitter::handle { background-color: #2D2D30; height: 4px; margin: 4px 0px; border-radius: 2px; }
        """)

        self.local_ip = get_local_ip()
        self.all_ipv4 = [] 
        self.http_url = f"http://{self.local_ip}:{HTTP_PORT}"
        self.https_url = f"https://{self.local_ip}:{HTTPS_PORT}"
        self.cf_lock = threading.Lock()
        self.cloudflare_url = "Offline"
        self.cf_process = None
        self._cf_connecting = False  
        self._cf_manual_stop = False
        self._cf_reconnect_attempts = 0
        self._cf_generation = 0
        self.SessionMySQL = None
        self.SessionSQLite = None
        self._db_checked = False
        threading.Thread(target=self.connect_db, daemon=True).start()
        
        self.http_thread = None
        self.https_thread = None
        self.log_lock = threading.Lock()
        self.log_buffer_flask = []
        self.log_buffer_network = []
        self.log_buffer_cf = []
        
        self.gui_queue = queue.Queue(maxsize=1000)
        self._meter_cache = {}
        self._context_device_id = None
        self._last_device_order = []
        self._device_table_empty_state = False

        global gui_log_callback
        global app_window
        gui_log_callback = self.log_flask_event
        app_window = self
        
        self.build_ui()
        
        self.animated_meters = {
            "cpu": AnimatedMeter(self.mini_meter_cpu),
            "ram": AnimatedMeter(self.mini_meter_ram),
            "net": AnimatedMeter(self.mini_meter_net),
            "api": AnimatedMeter(self.mini_meter_api)
        }
        self._last_anim_time = None
        
        self.timer_gui_queue = QTimer(self)
        self.timer_gui_queue.timeout.connect(self.process_gui_queue)
        self.timer_gui_queue.start(10) 
        
        self.timer_log_flush = QTimer(self)
        self.timer_log_flush.timeout.connect(self.flush_log_buffers)
        self.timer_log_flush.start(250)
        
        self.timer_anim = QTimer(self)
        self.timer_anim.timeout.connect(self.animation_loop)
        self.timer_anim.start(16) 
        
        self.timer_hw = QTimer(self)
        self.timer_hw.timeout.connect(self.refresh_hw_meters)
        self.timer_hw.start(1000)
        
        self.timer_stats = QTimer(self)
        self.timer_stats.timeout.connect(self.refresh_stats)
        self.timer_stats.start(3000)

        self.ping_executor = concurrent.futures.ThreadPoolExecutor(max_workers=4)
        threading.Thread(target=self.network_ping_daemon, daemon=True).start()
        threading.Thread(target=stats_refresher_loop, daemon=True).start()
        threading.Thread(target=traffic_monitor_loop, daemon=True).start()

    def connect_db(self):
        try:
            sessions = get_cached_sessions() or {}
            self.SessionMySQL = sessions.get('mysql')
            self.SessionSQLite = sessions.get('sqlite')
        except Exception as e:
            logging.debug(f"DB connection initial check: {e}")
        finally:
            self._db_checked = True

    def _ping_local(self, session):
        if not self.http_thread or _flask_shutdown_event.is_set():
            return 0, "OFFLINE"
        start = time.perf_counter()
        try:
            resp = session.get(f"http://127.0.0.1:{HTTP_PORT}/api/status", timeout=0.8)
            if resp.status_code == 200:
                return int((time.perf_counter() - start) * 1000), "ONLINE"
            return 0, "OFFLINE"
        except Exception:
            return 0, "OFFLINE"

    def _ping_cloud(self, session):
        with self.cf_lock: cf_url = self.cloudflare_url
        if cf_url == "Offline" or cf_url == "Pending": return 0, "OFFLINE"
        start = time.time()
        try:
            session.get(f"{cf_url}/api/status", timeout=(2.0, 5.0), verify=False)
            return int((time.time() - start) * 1000), "ONLINE"
        except requests.exceptions.RequestException as e:
            err_msg = str(e)
            if "Max retries exceeded" in err_msg or "NameResolutionError" in err_msg: clean_msg = "DNS resolution pending or tunnel unreachable."
            else: clean_msg = err_msg[:100] + "..."
            if getattr(self, "_last_ping_err", "") != clean_msg: 
                self._append_log('cf', f"[PING ERROR] {clean_msg}")
                self._last_ping_err = clean_msg
            return 0, "OFFLINE"

    def network_ping_daemon(self):
        global NETWORK_LATENCY
        session = requests.Session()
        session.headers.update({"User-Agent": "EventHub-PingDaemon/2.0"})
        while not _global_shutdown_event.is_set():
            try:
                future_local = self.ping_executor.submit(self._ping_local, session)
                future_cloud = self.ping_executor.submit(self._ping_cloud, session)
                l_ms, l_stat = future_local.result(timeout=2)
                c_ms, c_stat = future_cloud.result(timeout=6)
                with network_latency_lock:
                    NETWORK_LATENCY.update({"local_ms": l_ms, "local_status": l_stat, "cloud_ms": c_ms, "cloud_status": c_stat})
            except Exception as e:
                logging.debug(f"Ping daemon iteration error: {e}")
            _global_shutdown_event.wait(3.0)

    def _append_log(self, widget_id, message, tag=None):
        segments = list(message) if isinstance(message, (list, tuple)) else [(message, tag or _guess_log_tag(message))]
        with self.log_lock:
            if widget_id == 'flask': self.log_buffer_flask.append(segments)
            elif widget_id == 'network': self.log_buffer_network.append(segments)
            elif widget_id == 'cf': self.log_buffer_cf.append(segments)

    def log_flask_event(self, message):
        self._append_log('flask', message)

    def flush_log_buffers(self):
        try:
            with self.log_lock:
                CHUNK_SIZE = 500
                flask_logs = self.log_buffer_flask[:CHUNK_SIZE]
                net_logs = self.log_buffer_network[:CHUNK_SIZE]
                cf_logs = self.log_buffer_cf[:CHUNK_SIZE]
                del self.log_buffer_flask[:CHUNK_SIZE]
                del self.log_buffer_network[:CHUNK_SIZE]
                del self.log_buffer_cf[:CHUNK_SIZE]
            if flask_logs: self._write_logs_to_widget(self.log_flask, flask_logs)
            if net_logs: self._write_logs_to_widget(self.log_network, net_logs)
            if cf_logs: self._write_logs_to_widget(self.log_cf, cf_logs)
        except Exception as e:
            logging.debug(f"Log buffer flush error: {e}")

    def _write_logs_to_widget(self, text_widget, log_batches):
        tag_colors = {
            "log_default": "#D4D4D4", "log_timestamp": "#757575", "log_device": "#569CD6",
            "log_success": "#4EC9B0", "log_warning": "#D7BA7D", "log_error": "#F44747",
            "log_info": "#9CDCFE", "log_register": "#C586C0", "log_checkin": "#CE9178"
        }
        html_lines = []
        for segments in log_batches:
            line_html = ""
            for txt, tg in segments:
                color = tag_colors.get(tg, "#D4D4D4")
                line_html += f"<span style='color:{color};'>{html.escape(txt)}</span>"
            html_lines.append(line_html)
            
        if html_lines:
            text_widget.appendHtml("<br>".join(html_lines))

    def process_gui_queue(self):
        start_time = time.perf_counter()
        processed_count = 0
        try:
            while time.perf_counter() - start_time < 0.010 and processed_count < 200:
                try: task = self.gui_queue.get_nowait()
                except queue.Empty: break
                try: task()
                except Exception as e: logging.error(f"GUI task execution failed: {e}")
                processed_count += 1
        except Exception as e:
            logging.debug(f"Process GUI queue error: {e}")

    def animation_loop(self):
        try:
            now = time.perf_counter()
            elapsed_ms = (now - self._last_anim_time) * 1000.0 if self._last_anim_time else 16.0
            self._last_anim_time = now
            elapsed_ms = min(elapsed_ms, 100.0)  
            for anim_meter in self.animated_meters.values(): anim_meter.tick(elapsed_ms)
        except Exception as e:
            logging.debug(f"Animation loop error: {e}")

    def clear_system_logs(self):
        with self.log_lock: self.log_buffer_flask.clear()
        self.log_flask.clear()
        self._append_log('flask', "[INFO] Operator cleared system logs.")

    def clear_network_logs(self):
        with self.log_lock: self.log_buffer_network.clear()
        self.log_network.clear()
        self._append_log('network', "[INFO] Operator cleared network logs.")

    def clear_cf_logs(self):
        with self.log_lock: self.log_buffer_cf.clear()
        self.log_cf.clear()
        self._append_log('cf', "[INFO] Operator cleared Cloudflare logs.")

    def copy_to_clipboard(self, text):
        if not text or text == "Offline": return
        QApplication.clipboard().setText(text)
        self._append_log('network', f"[CLIPBOARD] Copied: {text}")

    def open_browser(self, url): 
        if url != "Offline" and url != "Pending": webbrowser.open(url)
        
    def toggle_fullscreen(self):
        if self.isFullScreen(): self.showNormal()
        else: self.showFullScreen()

    def _build_status_badge(self, parent_layout, initial_text, color):
        f = QFrame()
        f.setStyleSheet("QFrame { background-color:#1E1E22; border: 1px solid #2D2D30; border-radius: 4px; padding: 2px 4px; }")
        l = QVBoxLayout(f)
        l.setContentsMargins(5,3,5,3)
        lbl = QLabel(initial_text)
        lbl.setStyleSheet(f"color: {color}; font-weight: bold; font-size: 11px; border:none;")
        l.addWidget(lbl)
        parent_layout.addWidget(f)
        return lbl

    def _create_log_box(self, parent_layout, title, clear_cmd):
        frame = QFrame()
        frame.setStyleSheet("QFrame { background: #1C1C1E; border: 1px solid #2D2D30; border-radius: 4px; }")
        v = QVBoxLayout(frame)
        v.setContentsMargins(0,0,0,0); v.setSpacing(0)
        
        hdr = QFrame()
        hdr.setStyleSheet("background: #202023; border-bottom: 1px solid #2D2D30; border-top-left-radius: 4px; border-top-right-radius: 4px;")
        h = QHBoxLayout(hdr)
        h.setContentsMargins(8,3,8,3)
        l = QLabel(title)
        l.setStyleSheet("color: #4EC9B0; font-weight: bold; font-size: 11px; border:none;")
        h.addWidget(l)
        h.addStretch()
        if clear_cmd:
            btn = QPushButton("Clear")
            btn.setStyleSheet("background: transparent; border: none; color: #569CD6; font-size: 11px;")
            btn.clicked.connect(clear_cmd)
            h.addWidget(btn)
        v.addWidget(hdr)
        
        pt = QPlainTextEdit()
        pt.setReadOnly(True)
        pt.setMaximumBlockCount(MAX_LOG_LINES)
        v.addWidget(pt)
        
        parent_layout.addWidget(frame)
        return pt

    def _show_device_menu(self, pos):
        item = self.tree_devices.itemAt(pos)
        if item:
            row = item.row()
            self._context_device_id = self.tree_devices.item(row, 6).text()
            if self._context_device_id == "empty_msg": return
            
            menu = QMenu(self)
            menu.setStyleSheet("QMenu { background-color: #252528; border: 1px solid #3E3E42; } QMenu::item:selected { background-color: #094771; }")
            m_ren = menu.addAction("✏️ Rename Device")
            m_msg = menu.addAction("📨 Send Text Message")
            
            action = menu.exec(self.tree_devices.viewport().mapToGlobal(pos))
            if action == m_ren: self._prompt_rename_device()
            elif action == m_msg: self._prompt_send_message()

    def _prompt_rename_device(self):
        d_id = getattr(self, '_context_device_id', None)
        if not d_id or d_id == "empty_msg": return
        with device_lock:
            if d_id not in ACTIVE_DEVICES: return
            old_name = ACTIVE_DEVICES[d_id]['name']
        new_name, ok = QInputDialog.getText(self, "Rename Device", f"Enter new name for '{old_name}':", text=old_name)
        if ok and new_name.strip() and new_name.strip() != old_name:
            with device_lock:
                CUSTOM_DEVICE_NAMES[d_id] = new_name.strip()
                if d_id in ACTIVE_DEVICES: ACTIVE_DEVICES[d_id]['name'] = new_name.strip()
            _save_device_names()
            self._append_log('network', f"[INFO] Device '{old_name}' renamed to '{new_name.strip()}'.")

    def _prompt_send_message(self):
        d_id = getattr(self, '_context_device_id', None)
        if not d_id or d_id == "empty_msg": return
        with device_lock:
            if d_id not in ACTIVE_DEVICES: return
            d_name = ACTIVE_DEVICES[d_id]['name']
        msg, ok = QInputDialog.getText(self, "Send Message", f"Enter message for '{d_name}':")
        if ok and msg.strip():
            with device_lock: DEVICE_MESSAGES[d_id] = msg.strip()
            self._append_log('network', f"[INFO] Message queued for {d_name}: {msg.strip()}")

    def _prompt_broadcast_message(self):
        with device_lock: active_count = len(ACTIVE_DEVICES)
        if active_count == 0:
            QMessageBox.warning(self, "No Devices", "No active devices connected to broadcast to.")
            return
        msg, ok = QInputDialog.getText(self, "Broadcast Message", f"Enter message for ALL ({active_count}) active devices:")
        if ok and msg.strip():
            with device_lock:
                for d_id in ACTIVE_DEVICES.keys(): DEVICE_MESSAGES[d_id] = msg.strip()
            self._append_log('network', f"[INFO] Broadcast queued for {active_count} devices: {msg.strip()}")

    def manual_refresh_all(self):
        self.refresh_stats()
        self.refresh_hw_meters()
        self._append_log('flask', f"[{datetime.now().strftime('%H:%M:%S')}] [INFO] Operator triggered manual system refresh.")

    def build_ui(self):
        central = QWidget()
        self.setCentralWidget(central)
        main_layout = QHBoxLayout(central)
        main_layout.setContentsMargins(6, 6, 6, 6)
        main_layout.setSpacing(8)
        
        sidebar_scroll = QScrollArea()
        sidebar_scroll.setWidgetResizable(True)
        sidebar_scroll.setFixedWidth(275) 
        sidebar_scroll.setHorizontalScrollBarPolicy(Qt.ScrollBarAlwaysOff)
        
        sidebar = QWidget()
        side_layout = QVBoxLayout(sidebar)
        side_layout.setContentsMargins(4, 4, 4, 4)
        side_layout.setSpacing(6)
        
        lbl_net = QLabel("NETWORK & ROUTING")
        lbl_net.setStyleSheet("color: #569CD6; font-size: 13px; font-weight: bold;")
        side_layout.addWidget(lbl_net)
        
        grp_eng = QGroupBox("🌐 High-Speed Engine")
        l_eng = QVBoxLayout(grp_eng)
        l_eng.setContentsMargins(8, 12, 8, 8)
        l_eng.setSpacing(6)
        
        self.btn_start_flask = QPushButton("▶ START ENGINE")
        self.btn_start_flask.setObjectName("btnStartEngine")
        self.btn_start_flask.clicked.connect(self.start_flask)
        self.btn_stop_flask = QPushButton("⏹ STOP ENGINE")
        self.btn_stop_flask.setObjectName("btnStopEngine")
        self.btn_stop_flask.setEnabled(False)
        self.btn_stop_flask.clicked.connect(self.stop_flask)
        l_eng.addWidget(self.btn_start_flask); l_eng.addWidget(self.btn_stop_flask)
        
        lbl_qr_desc = QLabel("Network QR (iOS HTTPS):")
        lbl_qr_desc.setStyleSheet("font-size: 10px; color: #888888;")
        l_eng.addWidget(lbl_qr_desc)
        
        self.lbl_flask_qr = QLabel(); self.lbl_flask_qr.setAlignment(Qt.AlignCenter)
        l_eng.addWidget(self.lbl_flask_qr); self.update_qr(self.lbl_flask_qr, "OFFLINE")
        
        self.lbl_flask_link = QLabel("HTTPS Offline")
        self.lbl_flask_link.setWordWrap(True) 
        self.lbl_flask_link.setStyleSheet("color: #858585; font-size: 10px;"); self.lbl_flask_link.setAlignment(Qt.AlignCenter)
        l_eng.addWidget(self.lbl_flask_link)
        
        r1 = QHBoxLayout(); r1.setSpacing(4)
        b_cpy_s = QPushButton("Copy HTTPS"); b_cpy_s.clicked.connect(lambda: self.copy_to_clipboard(self.https_url))
        b_cpy_h = QPushButton("Copy HTTP"); b_cpy_h.clicked.connect(lambda: self.copy_to_clipboard(self.http_url))
        r1.addWidget(b_cpy_s); r1.addWidget(b_cpy_h); l_eng.addLayout(r1)
        
        r2 = QHBoxLayout(); r2.setSpacing(4)
        b_opn_s = QPushButton("Open Secure"); b_opn_s.clicked.connect(lambda: self.open_browser(self.https_url))
        b_opn_h = QPushButton("Open Local"); b_opn_h.clicked.connect(lambda: self.open_browser(self.http_url))
        r2.addWidget(b_opn_s); r2.addWidget(b_opn_h); l_eng.addLayout(r2)
        side_layout.addWidget(grp_eng)
        
        grp_cf = QGroupBox("☁️ Cloudflare Tunnel")
        l_cf = QVBoxLayout(grp_cf)
        l_cf.setContentsMargins(8, 12, 8, 8)
        l_cf.setSpacing(6)
        
        self.btn_start_cf = QPushButton("▶ START TUNNEL")
        self.btn_start_cf.setObjectName("btnStartTunnel")
        self.btn_start_cf.setEnabled(False)
        self.btn_start_cf.clicked.connect(self.start_cf)
        self.btn_stop_cf = QPushButton("⏹ STOP TUNNEL")
        self.btn_stop_cf.setObjectName("btnStopTunnel")
        self.btn_stop_cf.setEnabled(False)
        self.btn_stop_cf.clicked.connect(self.stop_cf)
        l_cf.addWidget(self.btn_start_cf); l_cf.addWidget(self.btn_stop_cf)
        
        self.lbl_cf_qr = QLabel(); self.lbl_cf_qr.setAlignment(Qt.AlignCenter)
        l_cf.addWidget(self.lbl_cf_qr); self.update_qr(self.lbl_cf_qr, "OFFLINE")
        
        self.lbl_cf_link = QLabel("Tunnel Offline")
        self.lbl_cf_link.setWordWrap(True) 
        self.lbl_cf_link.setStyleSheet("color: #858585; font-size: 10px;"); self.lbl_cf_link.setAlignment(Qt.AlignCenter)
        l_cf.addWidget(self.lbl_cf_link)
        
        r3 = QHBoxLayout(); r3.setSpacing(4)
        b_cpy_cf = QPushButton("Copy URL"); b_cpy_cf.clicked.connect(lambda: self.copy_to_clipboard(self.cloudflare_url))
        b_opn_cf = QPushButton("Open URL"); b_opn_cf.clicked.connect(lambda: self.open_browser(self.cloudflare_url))
        r3.addWidget(b_cpy_cf); r3.addWidget(b_opn_cf); l_cf.addLayout(r3)
        side_layout.addWidget(grp_cf)
        
        grp_test = QGroupBox("🧪 Simulator Engine")
        l_test = QVBoxLayout(grp_test); l_test.setContentsMargins(8, 12, 8, 8); l_test.setSpacing(6)
        
        self.chk_test = QCheckBox("Testing Mode OFF")
        self.chk_test.toggled.connect(self.toggle_test_mode) 
        l_test.addWidget(self.chk_test)
        
        self.cb_test_date = QComboBox()
        self.cb_test_date.addItems(["2026-08-30", "2026-08-31", "2026-09-01"])
        self.cb_test_date.setEnabled(False)
        self.cb_test_date.currentTextChanged.connect(self.on_test_date_changed)
        l_test.addWidget(self.cb_test_date)
        side_layout.addWidget(grp_test)
        
        grp_sup = QGroupBox("📞 Support")
        l_sup = QVBoxLayout(grp_sup); l_sup.setContentsMargins(8, 12, 8, 8); l_sup.setSpacing(6)
        l_sup.addWidget(QLabel("Contact: +91 8960446756"))
        b_chat = QPushButton("💬 Chat on WhatsApp")
        b_chat.setObjectName("btnWhatsApp")
        b_chat.clicked.connect(lambda: self.open_browser("https://wa.me/918960446756"))
        l_sup.addWidget(b_chat)
        side_layout.addWidget(grp_sup)
        
        side_layout.addStretch()
        sidebar_scroll.setWidget(sidebar)
        main_layout.addWidget(sidebar_scroll)
        
        content = QWidget()
        c_lay = QVBoxLayout(content)
        c_lay.setContentsMargins(4, 0, 0, 0)
        c_lay.setSpacing(8)
        
        hdr_row = QHBoxLayout()
        hdr_row.setSpacing(10)
        
        h_left = QVBoxLayout()
        h_left.setSpacing(4)
        lbl_h = QLabel("EventHub Portable — COMMAND CENTER")
        lbl_h.setStyleSheet("color: #4EC9B0; font-size: 18px; font-weight: bold;")
        h_left.addWidget(lbl_h)
        
        b_row = QHBoxLayout()
        b_row.setSpacing(5)
        self.lbl_stat_cf = self._build_status_badge(b_row, "● CF: OFF", "#858585")
        self.lbl_stat_sqlite = self._build_status_badge(b_row, "● LITE: WAIT", "#569CD6")
        self.lbl_stat_mysql = self._build_status_badge(b_row, "● SQL: WAIT", "#569CD6")
        b_row.addStretch()
        h_left.addLayout(b_row)
        hdr_row.addLayout(h_left, stretch=1)
        
        hw_f = QHBoxLayout()
        hw_f.setSpacing(5)
        self.mini_meter_cpu = SpeedometerGauge("CPU", "%", 100, good_is_high=False)
        self.mini_meter_ram = SpeedometerGauge("RAM", "%", 100, good_is_high=False)
        self.mini_meter_net = SpeedometerGauge("OFFLINE", "MB/s", 100, good_is_high=True)
        self.mini_meter_api = SpeedometerGauge("API ms", "ms", 500, good_is_high=False)
        hw_f.addWidget(self.mini_meter_cpu)
        hw_f.addWidget(self.mini_meter_ram)
        hw_f.addWidget(self.mini_meter_net)
        hw_f.addWidget(self.mini_meter_api)
        hdr_row.addLayout(hw_f)
        
        sys_h = QFrame()
        sys_h.setStyleSheet("QFrame { background: #1C1C1E; border: 1px solid #2D2D30; border-radius: 4px; padding: 2px; }")
        s_lay = QGridLayout(sys_h)
        s_lay.setContentsMargins(8, 4, 8, 4)
        s_lay.setSpacing(4)
        
        lbl_sh = QLabel("SYSTEM HEALTH")
        lbl_sh.setStyleSheet("color: #777777; font-size: 10px; font-weight: bold;")
        s_lay.addWidget(lbl_sh, 0, 0, 1, 2)
        
        self.lbl_hdr_local_ping = QLabel("LAN: WAIT"); self.lbl_hdr_local_ping.setStyleSheet("color:#4EC9B0; font-weight:bold; font-size: 11px; border:none;")
        self.lbl_hdr_cloud_ping = QLabel("WAN: WAIT"); self.lbl_hdr_cloud_ping.setStyleSheet("color:#4EC9B0; font-weight:bold; font-size: 11px; border:none;")
        self.lbl_hdr_traffic = QLabel("Traffic: 0 req/s"); self.lbl_hdr_traffic.setStyleSheet("color:#569CD6; font-weight:bold; font-size: 11px; border:none;")
        self.lbl_hdr_db_queue = QLabel("DB Q: 0"); self.lbl_hdr_db_queue.setStyleSheet("color:#C586C0; font-weight:bold; font-size: 11px; border:none;")
        
        s_lay.addWidget(self.lbl_hdr_local_ping, 1, 0)
        s_lay.addWidget(self.lbl_hdr_cloud_ping, 1, 1)
        s_lay.addWidget(self.lbl_hdr_traffic, 2, 0)
        s_lay.addWidget(self.lbl_hdr_db_queue, 2, 1)
        hdr_row.addWidget(sys_h)
        
        acts = QVBoxLayout(); acts.setSpacing(3)
        b_ref = QPushButton("⟳ Refresh"); b_ref.clicked.connect(self.manual_refresh_all)
        b_full = QPushButton("⛶ Fullscreen"); b_full.clicked.connect(self.toggle_fullscreen)
        acts.addWidget(b_ref); acts.addWidget(b_full)
        hdr_row.addLayout(acts)
        c_lay.addLayout(hdr_row)
        
        self.lbl_test_banner = QLabel("⚠ TESTING MODE ACTIVE — scans are NOT counted against the live event date ⚠")
        self.lbl_test_banner.setAlignment(Qt.AlignCenter)
        self.lbl_test_banner.setStyleSheet("background:#D7BA7D; color:#1C1C1E; font-weight:bold; font-size:12px; border-radius:4px; padding:4px;")
        self.lbl_test_banner.setVisible(False)
        c_lay.addWidget(self.lbl_test_banner)
        
        self.stat_vars = {}
        grid = QGridLayout()
        grid.setSpacing(6)
        grid.setContentsMargins(0, 0, 0, 0)
        
        def _mk(t, c):
            f = QFrame(); f.setStyleSheet("QFrame { background:#1E1E22; border:1px solid #28282B; border-radius:4px; }")
            l = QVBoxLayout(f); l.setContentsMargins(5, 5, 5, 5); l.setSpacing(2)
            lbl_title = QLabel(t); lbl_title.setStyleSheet("color: #858585; font-size: 10px; font-weight: bold; border:none;")
            l.addWidget(lbl_title, alignment=Qt.AlignCenter)
            val = QLabel("0"); val.setStyleSheet(f"color:{c}; font-size:20px; font-weight:bold; border:none;")
            l.addWidget(val, alignment=Qt.AlignCenter)
            return f, val
        
        f1, self.stat_vars["total_att"] = _mk("TOTAL ATTENDEES", "#569CD6"); grid.addWidget(f1, 0, 0)
        f2, self.stat_vars["kiosk_reg"] = _mk("KIOSK REGISTRATIONS", "#9CDCFE"); grid.addWidget(f2, 0, 1)
        f3, self.stat_vars["sqlite_total"] = _mk("SQLITE MIRROR SIZE", "#4EC9B0"); grid.addWidget(f3, 0, 2)
        f4, self.stat_vars["online_scanners"] = _mk("ACTIVE SCANNERS", "#D7BA7D"); grid.addWidget(f4, 1, 0)
        f5, self.stat_vars["chk_today"] = _mk("TODAY CHECK-IN", "#4EC9B0"); grid.addWidget(f5, 1, 1)
        f9, self.stat_vars["chk_total"] = _mk("TOTAL CHECK-IN (ALL DAYS)", "#D7BA7D"); grid.addWidget(f9, 1, 2)
        f6, self.stat_vars["chk_30"] = _mk("30 Aug Check-in", "#CCCCCC"); grid.addWidget(f6, 2, 0)
        f7, self.stat_vars["chk_31"] = _mk("31 Aug Check-in", "#CCCCCC"); grid.addWidget(f7, 2, 1)
        f8, self.stat_vars["chk_01"] = _mk("01 Sept Check-in", "#CCCCCC"); grid.addWidget(f8, 2, 2)
        c_lay.addLayout(grid)
        
        main_splitter = QSplitter(Qt.Vertical)
        main_splitter.setChildrenCollapsible(False)
        
        dev_container = QWidget()
        dev_layout = QVBoxLayout(dev_container); dev_layout.setContentsMargins(0, 4, 0, 0); dev_layout.setSpacing(4)
        
        dev_hdr = QHBoxLayout(); dev_hdr.setSpacing(6)
        self.lbl_devices_header = QLabel("📡 ACTIVE CONNECTED DEVICES (0) — Right-Click to Manage")
        self.lbl_devices_header.setStyleSheet("color: #569CD6; font-weight:bold; font-size: 12px;")
        dev_hdr.addWidget(self.lbl_devices_header)
        
        self.lbl_stats_health = QLabel(""); self.lbl_stats_health.setStyleSheet("color:#D7BA7D; font-size: 11px;")
        dev_hdr.addWidget(self.lbl_stats_health)
        dev_hdr.addStretch()
        
        btn_bc = QPushButton("📢 Broadcast"); btn_bc.clicked.connect(self._prompt_broadcast_message)
        dev_hdr.addWidget(btn_bc)
        dev_layout.addLayout(dev_hdr)
        
        self.tree_devices = QTableWidget(0, 7)
        self.tree_devices.setHorizontalHeaderLabels(["Device Name", "IP Address", "Active Page", "Battery", "Last Heartbeat", "Signal", "_ID"])
        self.tree_devices.horizontalHeader().setSectionResizeMode(QHeaderView.Stretch)
        self.tree_devices.hideColumn(6)
        self.tree_devices.setContextMenuPolicy(Qt.CustomContextMenu)
        self.tree_devices.customContextMenuRequested.connect(self._show_device_menu)
        self.tree_devices.setEditTriggers(QTableWidget.NoEditTriggers)
        dev_layout.addWidget(self.tree_devices)
        main_splitter.addWidget(dev_container)
        
        log_container = QWidget()
        log_layout = QVBoxLayout(log_container); log_layout.setContentsMargins(0, 4, 0, 0); log_layout.setSpacing(4)
        
        log_h = QHBoxLayout(); log_h.setSpacing(6)
        self.log_flask = self._create_log_box(log_h, "📟 System & API Logs", self.clear_system_logs)
        
        tabs = QTabWidget()
        t1 = QWidget(); l1 = QVBoxLayout(t1); l1.setContentsMargins(0,0,0,0)
        self.log_network = self._create_log_box(l1, "Network Events", self.clear_network_logs)
        t2 = QWidget(); l2 = QVBoxLayout(t2); l2.setContentsMargins(0,0,0,0)
        self.log_cf = self._create_log_box(l2, "Tunnel Status", self.clear_cf_logs)
        
        tabs.addTab(t1, "🌐 Device Routing")
        tabs.addTab(t2, "☁️ Cloudflare Tunnel")
        log_h.addWidget(tabs)
        
        log_layout.addLayout(log_h)
        main_splitter.addWidget(log_container)
        
        main_splitter.setSizes([200, 700])
        c_lay.addWidget(main_splitter, stretch=1)
        
        ftr = QLabel("Engineered for Event Resilience • Powered by EllowDigital")
        ftr.setStyleSheet("color: #D7BA7D; font-family: 'Consolas'; font-weight: bold; font-size: 11px;")
        ftr.setAlignment(Qt.AlignRight)
        c_lay.addWidget(ftr)
        
        main_layout.addWidget(content, stretch=1)

    def _set_stat(self, var_name, new_value):
        entry = self.stat_vars.get(var_name)
        if not entry or entry.text() == str(new_value): return
        entry.setText(str(new_value))

    def update_qr(self, label, data):
        qr = qrcode.QRCode(version=1, box_size=3, border=1)
        qr.add_data(data)
        qr.make(fit=True)
        img = qr.make_image(fill_color="black", back_color="white").convert('RGBA')
        qim = QImage(img.tobytes('raw', 'RGBA'), img.size[0], img.size[1], QImage.Format_RGBA8888)
        pix = QPixmap.fromImage(qim).scaled(75, 75, Qt.KeepAspectRatio, Qt.SmoothTransformation)
        label.setPixmap(pix)

    def toggle_test_mode(self, is_checked):
        global SERVER_TEST_MODE
        SERVER_TEST_MODE = is_checked
        self.lbl_test_banner.setVisible(SERVER_TEST_MODE)
        if SERVER_TEST_MODE:
            self.chk_test.setText("Testing Mode ON")
            self.cb_test_date.setEnabled(True)
            self._append_log('network', f"[WARNING] Testing Mode ON. Server date overridden to {self.cb_test_date.currentText()}.")
        else:
            self.chk_test.setText("Testing Mode OFF")
            self.cb_test_date.setEnabled(False)
            self._append_log('network', "[INFO] Testing Mode OFF. Real system date restored.")
        self.refresh_stats()

    def on_test_date_changed(self, text):
        global SERVER_TEST_DATE
        SERVER_TEST_DATE = text
        self._append_log('network', f"[WARNING] Test date updated globally to: {SERVER_TEST_DATE}")
        self.refresh_stats()

    def refresh_hw_meters(self):
        try:
            with _telemetry_lock: snap_telemetry = dict(TELEMETRY_DATA)
            c = snap_telemetry.get("cpu", 0)
            r = snap_telemetry.get("ram", 0)
            
            cpu_curr = snap_telemetry.get("cpu_ghz_used", 0.0)
            cpu_max = snap_telemetry.get("cpu_ghz_total", 0.0)
            ram_used = snap_telemetry.get("ram_gb_used", 0.0)
            ram_total = snap_telemetry.get("ram_gb_total", 0.0)
            
            self.animated_meters["cpu"].set_target(c)
            self.animated_meters["cpu"].set_subtext(f"CPU: {cpu_curr:.1f}/{cpu_max:.1f} GHz")
            
            self.animated_meters["ram"].set_target(r)
            self.animated_meters["ram"].set_subtext(f"RAM: {ram_used:.1f}/{ram_total:.1f} GB")

            net_type = snap_telemetry.get("net_type", "Disconnected")
            if net_type in ["Disconnected", "Offline"]:
                self.animated_meters["net"].set_target(0)
                self.animated_meters["net"].set_subtext("OFFLINE")
            else:
                mbps = snap_telemetry.get("total_mbps", 0.0)
                cap = 1000 if mbps > 100 else (100 if mbps > 10 else 10)
                self.mini_meter_net.max_val = cap
                self.animated_meters["net"].set_target(mbps)
                self.animated_meters["net"].set_subtext(f"{net_type[:7]}: {mbps:.1f} MB/s")

            req_sec = TRAFFIC_HISTORY[-1] if len(TRAFFIC_HISTORY) > 0 else 0
            with metrics_lock: 
                if not self.http_thread and not self.https_thread:
                    SERVER_METRICS["avg_process_ms"] = 0.0
                    SERVER_METRICS["req_count"] = 0
                elif req_sec == 0:
                    SERVER_METRICS["avg_process_ms"] *= 0.85 
                    if SERVER_METRICS["avg_process_ms"] < 0.5: SERVER_METRICS["avg_process_ms"] = 0.0
                snap_metrics = dict(SERVER_METRICS)

            proc_ms = int(snap_metrics["avg_process_ms"])
            self.animated_meters["api"].set_target(min(proc_ms, 500))
            self.animated_meters["api"].set_subtext(f"API: {proc_ms} ms")

            new_ip = snap_telemetry.get("local_ip", self.local_ip)
            new_ipv4 = snap_telemetry.get("all_ipv4", self.all_ipv4)
            
            needs_restart = False
            
            if new_ip != self.local_ip:
                self.local_ip = new_ip
                self.http_url = f"http://{self.local_ip}:{HTTP_PORT}"
                self.https_url = f"https://{self.local_ip}:{HTTPS_PORT}"
                needs_restart = True
                
            added_ips = [ip for ip in new_ipv4 if ip not in self.all_ipv4]
            if added_ips:
                needs_restart = True
                
            self.all_ipv4 = new_ipv4
            
            if needs_restart:
                if not self.btn_start_flask.isEnabled(): 
                    self.update_qr(self.lbl_flask_qr, self.https_url)
                    self.lbl_flask_link.setText(self.https_url)
                    self._append_log('flask', f"[INFO] Primary network IP updated to {self.local_ip}")
                    # Auto-restarting engine is disabled here to prevent active scanner interruptions

            with network_latency_lock: snap_net = dict(NETWORK_LATENCY)
            loc_ms = snap_net["local_ms"]
            c_ms = snap_net["cloud_ms"]

            if snap_net["local_status"] == "ONLINE": 
                self.lbl_hdr_local_ping.setText(f"LAN: {loc_ms} ms")
                self.lbl_hdr_local_ping.setStyleSheet("color:#4EC9B0; font-weight:bold; font-size:11px; border:none;")
            else: 
                self.lbl_hdr_local_ping.setText("LAN: DOWN")
                self.lbl_hdr_local_ping.setStyleSheet("color:#F44747; font-weight:bold; font-size:11px; border:none;")

            if snap_net["cloud_status"] == "ONLINE": 
                self.lbl_hdr_cloud_ping.setText(f"WAN: {c_ms} ms")
                self.lbl_hdr_cloud_ping.setStyleSheet("color:#4EC9B0; font-weight:bold; font-size:11px; border:none;")
            else: 
                self.lbl_hdr_cloud_ping.setText("WAN: DOWN")
                self.lbl_hdr_cloud_ping.setStyleSheet("color:#D7BA7D; font-weight:bold; font-size:11px; border:none;")
                
            t_color = "#569CD6" if req_sec < 50 else ("#D7BA7D" if req_sec < 200 else "#F44747")
            self.lbl_hdr_traffic.setText(f"Traffic: {req_sec} req/s")
            self.lbl_hdr_traffic.setStyleSheet(f"color:{t_color}; font-weight:bold; font-size:11px; border:none;")
            
            q_size = DB_WRITE_QUEUE.qsize()
            q_color = "#C586C0" if q_size < 100 else ("#D7BA7D" if q_size < 1000 else "#F44747")
            self.lbl_hdr_db_queue.setText(f"DB Q: {q_size}")
            self.lbl_hdr_db_queue.setStyleSheet(f"color:{q_color}; font-weight:bold; font-size:11px; border:none;")
        except Exception as e:
            logging.debug(f"Refresh HW meters error: {e}")

    def refresh_stats(self):
        try:
            current_time = time.time()
            with device_lock:
                for d_id, data in list(ACTIVE_DEVICES.items()):
                    if current_time - data['last_seen'] >= DEVICE_ONLINE_WINDOW:
                        del ACTIVE_DEVICES[d_id]
                active_ids = [d_id for d_id, data in ACTIVE_DEVICES.items() if current_time - data['last_seen'] < DEVICE_ONLINE_WINDOW]
                device_info = {d_id: dict(ACTIVE_DEVICES[d_id]) for d_id in active_ids}

            self._set_stat("online_scanners", len(active_ids))
            self.lbl_devices_header.setText(f"📡 ACTIVE CONNECTED DEVICES ({len(active_ids)}) — Right-Click to Manage")

            current_order = sorted(active_ids, key=lambda i: device_info[i]['name'].lower())
            self._sync_device_table(current_order, device_info, current_time)

            if not self._db_checked:
                self.lbl_stat_mysql.setText("● SQL: WAIT")
                self.lbl_stat_mysql.setStyleSheet("color:#569CD6; font-weight:bold; font-size:11px; border:none;")
                self.lbl_stat_sqlite.setText("● LITE: WAIT")
                self.lbl_stat_sqlite.setStyleSheet("color:#569CD6; font-weight:bold; font-size:11px; border:none;")
            else:
                if self.SessionMySQL:
                    self.lbl_stat_mysql.setText("● SQL: LIVE")
                    self.lbl_stat_mysql.setStyleSheet("color:#4EC9B0; font-weight:bold; font-size:11px; border:none;")
                else:
                    self.lbl_stat_mysql.setText("● SQL: ERR")
                    self.lbl_stat_mysql.setStyleSheet("color:#F44747; font-weight:bold; font-size:11px; border:none;")
                if self.SessionSQLite:
                    self.lbl_stat_sqlite.setText("● LITE: SYNC")
                    self.lbl_stat_sqlite.setStyleSheet("color:#4EC9B0; font-weight:bold; font-size:11px; border:none;")
                else:
                    self.lbl_stat_sqlite.setText("● LITE: ERR")
                    self.lbl_stat_sqlite.setStyleSheet("color:#F44747; font-weight:bold; font-size:11px; border:none;")

            with stats_lock: snap = dict(STATS_CACHE)
            for k, v in zip(["total_att", "kiosk_reg", "sqlite_total", "chk_30", "chk_31", "chk_01", "chk_today", "chk_total"], 
                            [snap["total_attendees"], snap["total_registrations"], snap["total_attendees"], snap["chk_30"], snap["chk_31"], snap["chk_01"], snap["today_scans"], snap["total_scans"]]): 
                self._set_stat(k, v)

            stale = (current_time - snap["last_refreshed"]) if snap["last_refreshed"] else None
            if snap["last_error"] and stale and stale > STATS_REFRESH_INTERVAL_SEC * 4:
                self.lbl_stats_health.setText(f"⚠ DB Sync Delay ({int(stale)}s)")
            else:
                self.lbl_stats_health.setText("")
        except Exception as e:
            logging.error(f"refresh_stats error: {e}")

    def _sync_device_table(self, current_order, device_info, current_time):
        table = self.tree_devices
        page_labels = {"/": "Home Portal", "/scanner": "Scanner", "/register": "Registration", "/stats": "Network Stats"}

        if not current_order:
            if not self._device_table_empty_state:
                table.setRowCount(0)
                table.insertRow(0)
                it = QTableWidgetItem("No devices connected yet — awaiting heartbeat...")
                it.setForeground(QColor("#858585"))
                table.setItem(0, 0, it)
                table.setItem(0, 6, QTableWidgetItem("empty_msg"))
                self._device_table_empty_state = True
            self._last_device_order = []
            return

        fast_path = (
            current_order == self._last_device_order
            and not self._device_table_empty_state
            and table.rowCount() == len(current_order)
        )

        if not fast_path:
            table.setRowCount(0)

        for row, d_id in enumerate(current_order):
            info = device_info[d_id]
            sec_ago = max(0, int(current_time - info['last_seen']))
            sig_text, color = ("🟢 Live", "#4EC9B0") if sec_ago < 8 else (("🟡 Slow", "#D7BA7D") if sec_ago < 15 else ("🟠 Fading", "#F44747"))
            page_label = page_labels.get(info.get('page', '/'), info.get('page', '/'))
            values = [info['name'], info['ip'], page_label, f"🔋 {info.get('battery', 'N/A')}",
                      "just now" if sec_ago < 2 else f"{sec_ago}s ago", sig_text]
            qcolor = QColor(color)

            if fast_path:
                for col, val in enumerate(values):
                    item = table.item(row, col)
                    if item is None:
                        item = QTableWidgetItem()
                        table.setItem(row, col, item)
                    item.setText(val)
                    item.setForeground(qcolor)
            else:
                table.insertRow(row)
                items = [QTableWidgetItem(v) for v in values] + [QTableWidgetItem(d_id)]
                for col, it in enumerate(items):
                    it.setForeground(qcolor)
                    table.setItem(row, col, it)

        self._last_device_order = current_order
        self._device_table_empty_state = False

    def start_flask(self):
        self.btn_start_flask.setEnabled(False)
        self.btn_start_flask.setText("✅ ENGINE RUNNING")
        self._append_log('flask', f"[{datetime.now().strftime('%H:%M:%S')}] Booting Engine...")
        
        _flask_shutdown_event.clear()
        
        start_db_writers()
        self._append_log('flask', f"[SYSTEM] {DB_WRITER_THREADS} Multi-threaded DB writers ready.")
        try:
            self.http_thread = WaitressHttpThread(app, '0.0.0.0', HTTP_PORT)
            self.https_thread = HttpsFlaskThread(app, '0.0.0.0', HTTPS_PORT)
            self.http_thread.start()
            self.https_thread.start()
        except Exception as e:
            self._append_log('flask', f"[ERROR] Start failed: {e}")
            if self.http_thread: self.http_thread.shutdown(); self.http_thread = None
            self.https_thread = None
            stop_db_writers()
            self.btn_start_flask.setEnabled(True)
            self.btn_start_flask.setText("▶ START ENGINE")
            QMessageBox.critical(self, "Failed", f"Engine failed:\n{e}")
            return

        self.btn_stop_flask.setEnabled(True)
        self.btn_start_cf.setEnabled(True)
        self.update_qr(self.lbl_flask_qr, self.https_url)
        self.lbl_flask_link.setText(self.https_url)
        self.lbl_flask_link.setStyleSheet("color:#569CD6; font-size:10px;")
        self._append_log('flask', f"[SYSTEM] Waitress HTTP listening: {self.http_url} (Threads: {HTTP_WORKER_THREADS})")
        self._append_log('flask', f"[SYSTEM] Cheroot HTTPS listening: {self.https_url} (Threads: {HTTP_WORKER_THREADS})")
        
    def stop_flask(self):
        if self.btn_stop_cf.isEnabled(): self.stop_cf()
        self.btn_stop_flask.setEnabled(False)
        self.btn_start_flask.setEnabled(False)
        self.btn_start_flask.setText("⏳ STOPPING...")
        self.btn_start_cf.setEnabled(False)
        self.btn_start_cf.setText("▶ START TUNNEL")
        self.update_qr(self.lbl_flask_qr, "OFFLINE")
        self.lbl_flask_link.setText("Server Offline")
        self.lbl_flask_link.setStyleSheet("color:#858585; font-size:10px;")
        
        self._append_log('flask', f"[{datetime.now().strftime('%H:%M:%S')}] Engine stopping gracefully... Please wait.")

        def _async_stop():
            _flask_shutdown_event.set()
            time.sleep(1.5)
            
            stop_db_writers()
            if self.http_thread: self.http_thread.shutdown(); self.http_thread = None
            if self.https_thread: self.https_thread.shutdown(); self.https_thread = None
            self.gui_queue.put(lambda: self.btn_start_flask.setEnabled(True))
            self.gui_queue.put(lambda: self.btn_start_flask.setText("▶ START ENGINE"))
            self.gui_queue.put(lambda: self._append_log('flask', f"[{datetime.now().strftime('%H:%M:%S')}] Engine completely stopped."))
            
        threading.Thread(target=_async_stop, daemon=True).start()

    def _animate_cf_connecting(self, tick=0):
        if not self._cf_connecting: return
        self.lbl_stat_cf.setText(f"● CF: CONNECTING{'.' * (tick % 4)}")
        self.lbl_stat_cf.setStyleSheet("color:#D7BA7D; font-weight:bold; font-size:11px; border:none;")
        QTimer.singleShot(450, lambda: self._animate_cf_connecting(tick + 1))

    def _mark_cf_live(self): 
        self._cf_connecting = False
        self._cf_reconnect_attempts = 0
        self.lbl_stat_cf.setText("● CF: LIVE")
        self.lbl_stat_cf.setStyleSheet("color:#4EC9B0; font-weight:bold; font-size:11px; border:none;")

    def start_cf(self):
        if not self.http_thread:
            return self._append_log('cf', "[ERROR] Start Local Engine FIRST!")
        self._cf_manual_stop = False
        self._cf_reconnect_attempts = 0
        self._cf_generation += 1
        self._launch_cf_tunnel()

    def _launch_cf_tunnel(self):
        self.btn_start_cf.setEnabled(False)
        self.btn_start_cf.setText("⏳ TUNNEL CONNECTING...")
        self.btn_stop_cf.setEnabled(True)
        self._cf_connecting = True
        self._animate_cf_connecting()
        with self.cf_lock: self.cloudflare_url = "Pending"
            
        self._append_log('cf', f"[{datetime.now().strftime('%H:%M:%S')}] Requesting secure tunnel...")

        def _run_cf():
            try:
                proc = subprocess.Popen(
                    ["cloudflared", "tunnel", "--url", f"http://127.0.0.1:{HTTP_PORT}", "--http-host-header", "localhost", "--no-tls-verify"], 
                    stdout=subprocess.PIPE, stderr=subprocess.STDOUT, text=True, bufsize=1, 
                    creationflags=subprocess.CREATE_NO_WINDOW if platform.system() == "Windows" else 0
                )
                with self.cf_lock: self.cf_process = proc
                
                # NON-BLOCKING PIPE READER THREAD
                line_queue = queue.Queue()
                def _pipe_reader(out_pipe, q):
                    for raw_line in iter(out_pipe.readline, ''):
                        if _global_shutdown_event.is_set(): break
                        q.put(raw_line)
                    try: out_pipe.close()
                    except Exception: pass

                t_reader = threading.Thread(target=_pipe_reader, args=(proc.stdout, line_queue), daemon=True)
                t_reader.start()

                url_found = False
                while proc.poll() is None and not _global_shutdown_event.is_set() and not self._cf_manual_stop:
                    try:
                        line = line_queue.get(timeout=0.5)
                        cl = re.sub(r'\x1b\[[0-9;]*m', '', line).strip()
                        self._append_log('cf', cl)
                        if not url_found:
                            m = re.search(r"https://[a-zA-Z0-9-]+\.trycloudflare\.com", cl)
                            if m:
                                tunnel_url = m.group(0)
                                url_found = True
                                self._append_log('cf', "[INFO] Waiting 30s for DNS propagation...")
                                def finalize_tunnel(t_url):
                                    time.sleep(30)
                                    if self.cf_process is None: return 
                                    with self.cf_lock: self.cloudflare_url = t_url
                                    self.gui_queue.put(lambda u=t_url: self.update_qr(self.lbl_cf_qr, u))
                                    self.gui_queue.put(lambda u=t_url: (self.lbl_cf_link.setText(u), self.lbl_cf_link.setStyleSheet("color:#569CD6; font-size:10px;")))
                                    self.gui_queue.put(lambda: self.btn_start_cf.setText("✅ TUNNEL ACTIVE"))
                                    self.gui_queue.put(self._mark_cf_live)
                                    self._append_log('cf', f"[SUCCESS] Tunnel active: {t_url}")
                                threading.Thread(target=finalize_tunnel, args=(tunnel_url,), daemon=True).start()
                    except queue.Empty:
                        continue
            except FileNotFoundError:
                self.gui_queue.put(self.stop_cf)
                self._append_log('cf', "[ERROR] 'cloudflared' not found in PATH.")
            except Exception as e: 
                self.gui_queue.put(self.stop_cf)
                self._append_log('cf', f"[ERROR] Tunnel failed: {e}")
            finally:
                with self.cf_lock: is_active = (self.cf_process is not None)
                if is_active:
                    if self._cf_manual_stop or _global_shutdown_event.is_set() or self._cf_reconnect_attempts >= CF_MAX_RECONNECT_ATTEMPTS:
                        self.gui_queue.put(self.stop_cf)
                    else:
                        self._cf_reconnect_attempts += 1
                        attempt_no = self._cf_reconnect_attempts
                        delay = min(5 * attempt_no, 30)
                        my_generation = self._cf_generation
                        with self.cf_lock:
                            self.cf_process = None
                            self.cloudflare_url = "Pending"
                        self.gui_queue.put(lambda: self._append_log('cf', f"[WARNING] Tunnel dropped unexpectedly. Reconnecting in {delay}s (attempt {attempt_no}/{CF_MAX_RECONNECT_ATTEMPTS})..."))
                        self.gui_queue.put(lambda: (self.lbl_stat_cf.setText("● CF: RECONNECTING"), self.lbl_stat_cf.setStyleSheet("color:#D7BA7D; font-weight:bold; font-size:11px; border:none;")))

                        def _delayed_retry():
                            _global_shutdown_event.wait(delay)
                            if self._cf_generation != my_generation: return
                            if not _global_shutdown_event.is_set() and not self._cf_manual_stop and self.http_thread:
                                self.gui_queue.put(self._launch_cf_tunnel)
                        threading.Thread(target=_delayed_retry, daemon=True, name="CFReconnect").start()
                
        threading.Thread(target=_run_cf, daemon=True).start()
        
    def stop_cf(self):
        self._cf_manual_stop = True
        self._cf_generation += 1
        if not self.btn_stop_cf.isEnabled() and self.cloudflare_url == "Offline": return
        
        self.btn_stop_cf.setEnabled(False)
        self._cf_connecting = False  
        self.btn_start_cf.setEnabled(True if self.http_thread else False)
        self.btn_start_cf.setText("▶ START TUNNEL")
        
        self.lbl_stat_cf.setText("● CF: OFF")
        self.lbl_stat_cf.setStyleSheet("color:#858585; font-weight:bold; font-size:11px; border:none;")
        
        with self.cf_lock:
            proc = self.cf_process
            self.cf_process = None
            self.cloudflare_url = "Offline"
            
        if proc:
            try: 
                parent = psutil.Process(proc.pid)
                for child in parent.children(recursive=True): child.kill()
                parent.kill()
            except Exception:
                try:
                    if platform.system() == "Windows": subprocess.run(['taskkill', '/F', '/T', '/PID', str(proc.pid)], stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL, creationflags=0x08000000)
                    else: proc.terminate()
                except Exception as e: logging.debug(f"Process termination cleanup: {e}")
                
        self.update_qr(self.lbl_cf_qr, "OFFLINE")
        self.lbl_cf_link.setText("Tunnel Offline")
        self.lbl_cf_link.setStyleSheet("color:#858585; font-size:10px;")
        self._append_log('cf', f"[{datetime.now().strftime('%H:%M:%S')}] Tunnel closed successfully.")

    def closeEvent(self, event):
        try:
            _global_shutdown_event.set() 
            if self.http_thread or self.https_thread: self.stop_flask()
            with self.cf_lock: cf_proc = self.cf_process
            if cf_proc: self.stop_cf()
        except Exception as e: logging.debug(f"Close event error: {e}")
        finally:
            self.ping_executor.shutdown(wait=False)
            event.accept()

if __name__ == "__main__":
    if os.name == 'nt':
        try:
            my_app_id = os.environ.get("EVENTHUB_TOOL_ID", "EventHub.Tool.hub")
            ctypes.windll.shell32.SetCurrentProcessExplicitAppUserModelID(my_app_id)
        except Exception: pass
    
    app_qt = QApplication(sys.argv)
    if hasattr(Qt, 'HighDpiScaleFactorRoundingPolicy'):
        app_qt.setHighDpiScaleFactorRoundingPolicy(Qt.HighDpiScaleFactorRoundingPolicy.PassThrough)
        
    app_window = ServerHub()
    app_window.show()
    sys.exit(app_qt.exec())