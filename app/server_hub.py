"""
TDE UP 2026 — Event Hub (server_hub.py) — V2.2 (Hardened)

Combines a Flask HTTP/HTTPS server (for scanner/kiosk devices on the LAN),
a Tkinter operator dashboard, and background workers, in one process.

Hardening pass changes, in one place for whoever reads this next:

  1. DB WRITE QUEUE — /api/checkin and /api/register hand their DB work to a
     small pool of writer threads (DB_WRITER_THREADS) via a queue.Queue +
     concurrent.futures.Future, instead of opening a transaction directly on
     whichever Waitress/Cheroot request thread received the HTTP call. Bounds
     concurrent MySQL writes to a small, known number no matter how many of
     the 20+ scanner/kiosk devices POST at once. See "DB WRITE QUEUE ENGINE".

  2. STATS CACHE — one background thread (stats_refresher_loop) computes the
     dashboard's DB-derived numbers on a timer. /api/network-data and the
     GUI's refresh_stats() both just read the cached result now, instead of
     each independently re-running the same expensive queries — and instead
     of the GUI running them directly on its own main thread, where a slow
     MySQL round-trip used to freeze the whole window.

  3. LOGGING — a real, rotating logs/server_hub.log now exists (previously
     the only module in this project without one). Every check-in,
     registration, and error is durably recorded, not just shown on screen.

  4. Smaller fixes: daemon server threads + a WM_DELETE_WINDOW handler (so
     closing the window can't orphan a running engine/tunnel in the
     background), a duplicate-mobile race in registration resolved via
     IntegrityError instead of a raw 500, a device grid that shows real
     heartbeat freshness instead of a static "Online" string, and a request
     size limit.

schema.py is intentionally untouched — all of the above works within its
existing session/model layer.
"""

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
import concurrent.futures
from dataclasses import dataclass, field
import ipaddress
import requests
import urllib3
from datetime import datetime, timezone, timedelta

import ttkbootstrap as ttk
from ttkbootstrap.constants import *
from ttkbootstrap.widgets.scrolled import ScrolledText
from ttkbootstrap.dialogs import Messagebox
import qrcode
from PIL import Image, ImageTk
import webbrowser

from flask import Flask, render_template, request, jsonify, Response
from waitress import create_server  # 🚀 Waitress WSGI Engine (plain-HTTP port 5000 only)

# Disable InsecureRequestWarning for Cloudflare pings
urllib3.disable_warnings(urllib3.exceptions.InsecureRequestWarning)

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
from sqlalchemy.exc import IntegrityError

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

HTTP_PORT = 5000 # Fast, unencrypted Local LAN traffic & Cloudflare tunnel target
HTTPS_PORT = 5001  # Secure Local LAN traffic (allows iOS Camera Access natively)
CERT_DIR = os.path.join(BASE_DIR, 'config', 'certs')

# 🧵 Concurrency & robustness tuning — grouped here so it's easy to retune
# on-site without hunting through the file.
DB_WRITER_THREADS = 4            # Parallel workers draining the DB write queue (checkin/register).
                                  # Bounds concurrent MySQL writes to a small, known number instead
                                  # of every one of the 20+ scanner/kiosk devices opening its own
                                  # write transaction the instant it POSTs.
DB_JOB_QUEUE_MAXSIZE = 300       # Backpressure limit. If ever exceeded (DB truly stuck), new
                                  # requests fail fast with a clean 503 instead of piling up.
DB_JOB_TIMEOUT = 8               # Seconds a request waits for its DB job before getting a clean
                                  # timeout response — so one stuck DB call can never hold an HTTP
                                  # worker thread (and eventually the whole pool) hostage forever.
STATS_REFRESH_INTERVAL_SEC = 3   # How often the background thread recomputes dashboard stats.
SLOW_REQUEST_THRESHOLD_MS = 500  # Requests slower than this log a WARNING — visibility into
                                  # "is the network/DB actually slow right now?" during the event.
MAX_LOG_LINES = 2000             # Per log box scrollback cap. Without this, a multi-day live event
                                  # left running would grow each Text widget's content unbounded —
                                  # trimmed from the top once a box passes this many lines.

# ==============================================================================
# 📝 LOGGING — a real, rotating log file. Before this, server_hub.py was the
# only one of the app's modules with no persistent log (sync_manager.py and
# photo_down.py already wrote to logs/*.log) — every check-in, registration,
# and error only ever lived in the on-screen panels, gone the moment the
# window closed or crashed. RotatingFileHandler caps growth over the 3-day
# event instead of one file growing without bound.
# ==============================================================================
LOG_FILE = os.path.join(LOG_DIR, 'server_hub.log')
_file_handler = RotatingFileHandler(LOG_FILE, maxBytes=5_000_000, backupCount=5, encoding='utf-8')
_file_handler.setFormatter(logging.Formatter('%(asctime)s [%(levelname)s] %(message)s'))
logging.basicConfig(level=logging.INFO, handlers=[_file_handler])

template_dir = os.path.join(BASE_DIR, 'templates')
static_dir = os.path.join(BASE_DIR, 'static')
app = Flask(__name__, template_folder=template_dir, static_folder=static_dir)
# Generous but bounded — rejects broken/abusive oversized payloads with a
# clean 413 instead of a worker thread spending time reading them.
app.config['MAX_CONTENT_LENGTH'] = 2 * 1024 * 1024  # 2MB

gui_log_callback = None
SERVER_TEST_MODE = False
SERVER_TEST_DATE = "2026-08-30"

ACTIVE_DEVICES = {}
SCAN_CLIENTS = []
scan_clients_lock = threading.Lock()

# 📱 Single source of truth for "is this device still connected?" — used by both
# the GUI panel and the /api/network-data endpoint (previously 15s in one place
# and 30s in the other, which made the two views disagree with each other).
DEVICE_ONLINE_WINDOW = 20  # seconds since last heartbeat before a device is considered offline


# 🚀 PERFORMANCE FIX: Global DB Cache prevents connection exhaustion
DB_SESSIONS_CACHE = None
_db_cache_lock = threading.Lock()
_db_cache_last_failure = 0.0
DB_SESSIONS_RETRY_COOLDOWN = 5  # seconds — see get_cached_sessions() docstring

def get_cached_sessions():
    """Returns the shared {'mysql': sessionmaker, 'sqlite': sessionmaker} dict,
    built exactly once (double-checked locking avoids a rare startup race
    creating 2 engines).

    If get_database_sessions() itself fails (DB unreachable/misconfigured),
    the failure is cached for DB_SESSIONS_RETRY_COOLDOWN seconds. Without
    this, a DB outage turns every one of the 4 DB writer threads, the stats
    refresher, and every direct-read request into its own independent slow
    connect/timeout attempt — and since they all serialize on _db_cache_lock,
    that's a real bottleneck exactly when checkin/register traffic needs the
    fastest possible failure, not the slowest. With the cooldown, only the
    first caller after a failure pays the full connect/timeout cost; every
    other caller fails fast (returns None) until the cooldown lapses, and the
    system keeps retrying on a steady cadence so it self-heals once the DB
    comes back — no restart required.
    """
    global DB_SESSIONS_CACHE, _db_cache_last_failure
    if DB_SESSIONS_CACHE is None:
        with _db_cache_lock:
            if DB_SESSIONS_CACHE is None:
                if (time.time() - _db_cache_last_failure) < DB_SESSIONS_RETRY_COOLDOWN:
                    return None
                try:
                    DB_SESSIONS_CACHE = get_database_sessions()
                except Exception:
                    logging.exception(f"get_database_sessions() failed — will retry in {DB_SESSIONS_RETRY_COOLDOWN}s")
                    _db_cache_last_failure = time.time()
                    return None
    return DB_SESSIONS_CACHE

# 📡 TELEMETRY ENGINE GLOBALS
NETWORK_LATENCY = {
    "local_ms": 0,
    "cloud_ms": 0,
    "local_status": "OFFLINE",
    "cloud_status": "OFFLINE"
}
network_latency_lock = threading.Lock()

# ==============================================================================
# 📊 DASHBOARD STATS CACHE — refreshed by ONE background thread (defined
# below, started once at app launch), never on a Flask request thread or the
# Tkinter main thread. /api/network-data and the GUI's refresh_stats() both
# just read this dict now. Previously both of them independently ran the same
# 3 expensive full-table-scan LIKE-count queries every few seconds — and the
# GUI ran its copy directly on the Tkinter main thread, so any slow MySQL
# round-trip froze the entire operator window for its duration.
# ==============================================================================
STATS_CACHE = {
    "total_attendees": 0,
    "total_registrations": 0,
    "chk_30": 0,
    "chk_31": 0,
    "chk_01": 0,
    "total_scans": 0,
    "today_scans": 0,
    "last_refreshed": 0.0,
    "last_error": None,
}
stats_lock = threading.Lock()

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
        logging.info(f"[SSL] Generating new HTTPS certificate for {local_ip} (first run, or IP changed)...")
        _write_self_signed_cert(cert_path, key_path, local_ip)
        with open(ip_marker_path, 'w') as f:
            f.write(local_ip)
    else:
        logging.info(f"[SSL] Reusing existing HTTPS certificate for {local_ip}.")

    return cert_path, key_path

# ==============================================================================
# FLASK MIDDLEWARE & EVENT BROADCASTER
# ==============================================================================
@app.before_request
def _start_request_timer():
    request._start_time = time.time()

@app.after_request
def log_request(response):
    # /api/stream-scans is a deliberately long-lived SSE connection — "how long
    # has it been open" is meaningless there and would look like every request
    # is slow, so it stays excluded from timing along with static assets.
    if request.path.startswith('/static') or request.path.startswith('/favicon.ico') or request.path == '/api/stream-scans':
        return response

    try:
        duration_ms = (time.time() - getattr(request, '_start_time', time.time())) * 1000
        if duration_ms > SLOW_REQUEST_THRESHOLD_MS:
            logging.warning(f"Slow request: {request.method} {request.path} took {duration_ms:.0f}ms (status {response.status_code})")
    except Exception:
        pass

    return response

def _status_log_tag(status_code):
    """Maps an HTTP status code to a log color tag — green for 2xx, amber for
    4xx, red for 5xx+ — shared by every colorized log line. The actual colors
    live in _create_log_box()'s tag_configure calls."""
    if status_code >= 500:
        return "log_error"
    if status_code >= 400:
        return "log_warning"
    return "log_success"

# Auto-detected color tags for plain-string _append_log() calls — the ~20
# existing self._append_log(widget, f"[SYSTEM] ...") call sites throughout
# this file light up correctly with zero changes needed at each call site.
# Checked in order, first match wins; more specific prefixes are listed
# before broader ones they could otherwise be shadowed by.
_LOG_PREFIX_TAGS = (
    ("[PING ERROR]", "log_error"),
    ("[ERROR]", "log_error"),
    ("[WARNING]", "log_warning"),
    ("[SUCCESS]", "log_success"),
    ("[CLIPBOARD]", "log_info"),
    ("[INFO]", "log_info"),
)

def _guess_log_tag(message):
    for prefix, tag in _LOG_PREFIX_TAGS:
        if message.startswith(prefix):
            return tag
    return "log_default"

def log_event_clean(action_type, device_name, details, status_code):
    """Formats clean, human-readable operations logs for the GUI, and durably
    persists the same event to logs/server_hub.log — previously this only
    ever reached the on-screen panel, so closing the window or a crash lost
    the entire check-in/registration history for the event.

    Builds a list of (text, tag) segments rather than one plain string, so
    the GUI can color the action-type marker (REGISTER/CHECKIN) independently
    from the trailing status code — e.g. a failed CHECKIN still shows its
    "CHECKIN" marker in the usual color while "Status: 404" renders in amber,
    instead of one color winning for the whole line."""
    time_str = datetime.now().strftime('%H:%M:%S')
    status_tag = _status_log_tag(status_code)

    if action_type == "REGISTER":
        icon = "✅" if status_code == 200 else "❌"
        segments = [
            (f"[{time_str}] ", "log_dim"),
            (f"{icon} REGISTER  ", "log_register"),
            (f"[{device_name}] {details} — ", "log_default"),
            (f"Status: {status_code}", status_tag),
        ]
    elif action_type == "CHECKIN":
        icon = "🎫" if status_code == 200 else "⛔"
        segments = [
            (f"[{time_str}] ", "log_dim"),
            (f"{icon} CHECKIN  ", "log_checkin"),
            (f"[{device_name}] {details} — ", "log_default"),
            (f"Status: {status_code}", status_tag),
        ]
    else:
        segments = [
            (f"[{time_str}] ", "log_dim"),
            (f"🌐 [{device_name}] {action_type} — ", "log_default"),
            (f"Status: {status_code}", status_tag),
        ]

    if gui_log_callback:
        gui_log_callback(segments)

    plain_msg = f"[{device_name}] {action_type}: {details} (status {status_code})"
    if status_code >= 500:
        logging.error(plain_msg)
    elif status_code >= 400:
        logging.warning(plain_msg)
    else:
        logging.info(plain_msg)

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
    
    # Safely take a snapshot of the current clients list
    with scan_clients_lock:
        clients_snapshot = list(SCAN_CLIENTS)

    # Broadcast to all active queues
    for q in clients_snapshot:
        try:
            q.put_nowait(event)
        except queue.Full:
            # Client stopped reading (queue is full), safely remove them
            with scan_clients_lock:
                if q in SCAN_CLIENTS:
                    SCAN_CLIENTS.remove(q)
        except Exception:
            # Catch any other unexpected queue errors and remove the client
            with scan_clients_lock:
                if q in SCAN_CLIENTS:
                    SCAN_CLIENTS.remove(q)

# ==============================================================================
# 📊 DASHBOARD STATS ENGINE — one background thread computes these numbers on
# a timer; the API endpoint and the GUI both just read the cached result.
# ==============================================================================
def _compute_stats_snapshot():
    """Runs the actual (relatively expensive) COUNT queries exactly once. Only
    ever called from stats_refresher_loop()'s background thread — never from a
    Flask request thread or the Tkinter main thread — so a slow MySQL
    round-trip can never block an HTTP response or freeze the operator GUI."""
    sessions = get_cached_sessions()
    mysql_factory = sessions.get('mysql') if sessions else None
    if not mysql_factory:
        return None

    session = mysql_factory()
    try:
        total_attendees = session.query(Attendee).count()
        total_registrations = session.query(OfflineKioskAttendee).count()
        # Still LIKE-based (checkin_history is a JSON column, not a dedicated
        # per-day column, and schema.py is intentionally left untouched) — but
        # now it runs once every STATS_REFRESH_INTERVAL_SEC instead of once
        # per poll from every viewer, PLUS once more per GUI tick.
        chk_30 = session.query(Attendee).filter(Attendee.checkin_history.like('%"30 August"%')).count()
        chk_31 = session.query(Attendee).filter(Attendee.checkin_history.like('%"31 August"%')).count()
        chk_01 = session.query(Attendee).filter(Attendee.checkin_history.like('%"1 September"%')).count()
        return {
            "total_attendees": total_attendees,
            "total_registrations": total_registrations,
            "chk_30": chk_30, "chk_31": chk_31, "chk_01": chk_01,
            "total_scans": chk_30 + chk_31 + chk_01,
        }
    finally:
        session.close()

def stats_refresher_loop():
    """Runs for the lifetime of the app (started once from ServerHub.__init__),
    independent of whether the Waitress/Cheroot engines are running."""
    while True:
        try:
            snapshot = _compute_stats_snapshot()
            if snapshot is not None:
                today_date = SERVER_TEST_DATE if SERVER_TEST_MODE else datetime.now(timezone.utc).strftime('%Y-%m-%d')
                today_scans = {
                    "2026-08-30": snapshot["chk_30"],
                    "2026-08-31": snapshot["chk_31"],
                    "2026-09-01": snapshot["chk_01"],
                }.get(today_date, 0)
                with stats_lock:
                    STATS_CACHE.update(snapshot)
                    STATS_CACHE["today_scans"] = today_scans
                    STATS_CACHE["last_refreshed"] = time.time()
                    STATS_CACHE["last_error"] = None
        except Exception as e:
            logging.exception("Stats refresh failed")
            with stats_lock:
                STATS_CACHE["last_error"] = str(e)
        time.sleep(STATS_REFRESH_INTERVAL_SEC)


# ==============================================================================
# 🧵 DB WRITE QUEUE ENGINE — "no DB bottlenecks" in practice: /api/checkin and
# /api/register no longer open a DB transaction directly on whichever
# Waitress/Cheroot request thread happens to handle them. Instead they build a
# small job, drop it on a queue, and wait (with a timeout) on a Future. A
# small fixed pool of writer threads (DB_WRITER_THREADS) drains that queue.
#
# Why this is safe for correctness, not just throughput:
#   - Check-in already used SELECT ... FOR UPDATE on the attendee's own row,
#     so two simultaneous scans of the SAME badge correctly serialize via a
#     real DB row lock no matter how many writer threads exist.
#   - Registration's "does this mobile number already exist?" check does NOT
#     lock anything when nothing is found (there's no row to lock), so two
#     kiosks submitting the same mobile number at the same instant could
#     previously both pass the check and both try to INSERT — the loser saw a
#     raw 500 instead of a friendly "already registered". Fixed below by
#     catching the unique-constraint violation directly, which is correct
#     regardless of how many writer threads are running.
# ==============================================================================
@dataclass
class DBJob:
    kind: str
    payload: dict
    future: concurrent.futures.Future = field(default_factory=concurrent.futures.Future)

DB_WRITE_QUEUE = queue.Queue(maxsize=DB_JOB_QUEUE_MAXSIZE)
_db_writer_threads = []

def _submit_db_job(kind, payload):
    """Enqueues a job and blocks (only this one Flask request thread) until a
    writer thread finishes it or DB_JOB_TIMEOUT elapses. Returns the exact
    (status_code, body_dict) tuple the route should respond with."""
    job = DBJob(kind=kind, payload=payload)
    try:
        DB_WRITE_QUEUE.put(job, timeout=1)
    except queue.Full:
        logging.error(f"DB write queue full — rejecting a '{kind}' request")
        return 503, {"status": "error", "message": "Server is busy right now — please try again in a moment."}

    try:
        return job.future.result(timeout=DB_JOB_TIMEOUT)
    except concurrent.futures.TimeoutError:
        logging.error(f"DB job timed out after {DB_JOB_TIMEOUT}s ('{kind}')")
        return 504, {"status": "error", "message": "The request took too long to process. Please try again."}
    except Exception:
        logging.exception(f"Unhandled error waiting on DB job ('{kind}')")
        return 500, {"status": "error", "message": "An internal server error occurred while processing the request."}

def _handle_checkin_job(payload):
    """The exact check-in logic that used to run inline inside the Flask
    route — moved here unchanged so it now runs on a writer thread instead."""
    identifier = payload["identifier"]
    search_type = payload["search_type"]
    device_name = payload["device_name"]
    iso_timestamp = payload["iso_timestamp"]

    if not identifier:
        msg = "No ID or Phone provided"
        log_event_clean("CHECKIN", device_name, msg, 400)
        broadcast_scan(None, "ERROR", msg, device_name, iso_timestamp)
        return 400, {"status": "error", "message": msg}

    sessions = get_cached_sessions() or {}
    mysql_factory = sessions.get('mysql')
    if not mysql_factory:
        msg = "Database temporarily unavailable"
        log_event_clean("CHECKIN", device_name, msg, 503)
        broadcast_scan(None, "ERROR", msg, device_name, iso_timestamp)
        return 503, {"status": "error", "message": "Server is busy right now — please try again in a moment."}
    session = mysql_factory()

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
            return 404, {"status": "error", "message": msg}

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
            return 400, {"status": "error", "message": msg}

        today_key = date_map[current_date_str]
        att_days = attendee.attendance_days or []
        if isinstance(att_days, str):
            try: att_days = json.loads(att_days)
            except: att_days = []

        if today_key not in att_days:
            msg = f"Access Denied (No pass for {today_key})"
            log_event_clean("CHECKIN", device_name, msg, 403)
            broadcast_scan(attendee, "ERROR", msg, device_name, iso_timestamp)
            return 403, {"status": "error", "message": msg}

        if today_key in history:
            msg = f"Already checked in: {attendee.full_name}"
            log_event_clean("CHECKIN", device_name, msg, 400)
            broadcast_scan(attendee, "DUPLICATE", msg, device_name, iso_timestamp)
            return 400, {"status": "error", "message": msg}

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

        return 200, {"status": "success", "message": success_msg, "time": iso_timestamp}

    except Exception as e:
        try:
            session.rollback()
        except Exception:
            logging.exception("Checkin DB error — rollback also failed")
        logging.exception("Checkin DB error")
        log_event_clean("CHECKIN", device_name, f"DB Error: {str(e)}", 500)
        return 500, {"status": "error", "message": "An internal server error occurred while processing the request."}
    finally:
        try:
            session.close()
        except Exception:
            logging.exception("Error closing DB session after checkin")

def _handle_register_job(payload):
    """The exact registration logic that used to run inline inside the Flask
    route, plus a fix for the duplicate-mobile race described above."""
    data = payload["data"]
    device_label = payload["device_label"]
    iso_timestamp = payload["iso_timestamp"]
    mobile_number = payload["mobile_number"]

    sessions = get_cached_sessions() or {}
    mysql_factory = sessions.get('mysql')
    if not mysql_factory:
        log_event_clean("REGISTER", device_label, f"{data.get('full_name')} (Database unavailable)", 503)
        return 503, {"status": "error", "message": "Server is busy right now — please try again in a moment."}
    session = mysql_factory()

    try:
        existing_main = session.query(Attendee).filter_by(mobile=mobile_number).with_for_update().first()
        if existing_main:
            log_event_clean("REGISTER", device_label, f"{data.get('full_name')} (Already Exists)", 200)
            return 200, {"status": "already_registered", "message": "Already registered.", "attendee_id": existing_main.attendee_id}

        existing_kiosk = session.query(OfflineKioskAttendee).filter_by(mobile=mobile_number).with_for_update().first()
        if existing_kiosk:
            log_event_clean("REGISTER", device_label, f"{data.get('full_name')} (Already Exists)", 200)
            return 200, {"status": "already_registered", "message": "Already registered.", "attendee_id": existing_kiosk.attendee_id}

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
        try:
            session.add(new_kiosk_reg)
            session.commit()
        except IntegrityError:
            # Another writer thread won the race and inserted this same
            # mobile number a moment ago (e.g. two kiosks submitting the same
            # person at the same instant). Treat it exactly like the
            # "already registered" path above instead of surfacing a 500.
            session.rollback()
            existing = (session.query(Attendee).filter_by(mobile=mobile_number).first()
                        or session.query(OfflineKioskAttendee).filter_by(mobile=mobile_number).first())
            if existing:
                log_event_clean("REGISTER", device_label, f"{data.get('full_name')} (Already Exists - Race Resolved)", 200)
                return 200, {"status": "already_registered", "message": "Already registered.", "attendee_id": existing.attendee_id}
            masked = f"...{mobile_number[-4:]}" if len(mobile_number) >= 4 else "****"
            logging.error(f"IntegrityError on register but no matching record found for mobile {masked}")
            return 500, {"status": "error", "message": "An internal server error occurred while processing the request."}

        log_event_clean("REGISTER", device_label, f"{data.get('full_name')} ({new_attendee_id})", 200)
        return 200, {"status": "success", "message": "Saved successfully.", "attendee_id": new_attendee_id}

    except Exception as e:
        try:
            session.rollback()
        except Exception:
            logging.exception("Registration DB error — rollback also failed")
        logging.exception("Registration DB error")
        log_event_clean("REGISTER", device_label, f"DB Error: {str(e)}", 500)
        return 500, {"status": "error", "message": "An internal server error occurred while processing the request."}
    finally:
        try:
            session.close()
        except Exception:
            logging.exception("Error closing DB session after registration")

def db_writer_loop(worker_id):
    logging.info(f"DB writer thread #{worker_id} started")
    while True:
        job = DB_WRITE_QUEUE.get()
        if job is None:  # shutdown sentinel
            DB_WRITE_QUEUE.task_done()
            break
        try:
            if job.kind == "checkin":
                result = _handle_checkin_job(job.payload)
            elif job.kind == "register":
                result = _handle_register_job(job.payload)
            else:
                result = (500, {"status": "error", "message": "Unknown job type"})
            if not job.future.done():
                job.future.set_result(result)
        except Exception as e:
            logging.exception(f"Unhandled error in DB writer thread #{worker_id}")
            if not job.future.done():
                job.future.set_exception(e)
        finally:
            DB_WRITE_QUEUE.task_done()
    logging.info(f"DB writer thread #{worker_id} stopped")

def start_db_writers():
    for i in range(DB_WRITER_THREADS):
        t = threading.Thread(target=db_writer_loop, args=(i + 1,), daemon=True, name=f"DBWriter-{i+1}")
        t.start()
        _db_writer_threads.append(t)

def stop_db_writers():
    for _ in range(len(_db_writer_threads)):
        DB_WRITE_QUEUE.put(None)
    _db_writer_threads.clear()


# ==============================================================================
# FLASK ROUTES & APIS
# ==============================================================================
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
        
        # safely add the new client queue using the lock
        with scan_clients_lock:
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
            # safely remove the client queue using the lock on disconnect
            with scan_clients_lock:
                if q in SCAN_CLIENTS:
                    SCAN_CLIENTS.remove(q)
                    
    return Response(
        event_stream(), 
        mimetype='text/event-stream', 
        headers={
            'Cache-Control': 'no-cache', 
            'Connection': 'keep-alive',
            'X-Accel-Buffering': 'no'  # Prevents Nginx/Cloudflare from buffering the stream
        }
    )

@app.route('/api/checkin', methods=['POST'])
def process_checkin():
    """Thin & fast: validate the shape of the request, hand the actual DB
    work to the write-queue (see _handle_checkin_job above), then translate
    the result into the exact same JSON shape scanner clients already expect."""
    data = request.json or {}
    payload = {
        # Accept multiple possible input keys from various scanner scripts
        "identifier": str(data.get('attendee_id', data.get('qr_data', data.get('id', '')))).strip(),
        "search_type": data.get('search_type', 'id'),
        "device_name": data.get('device_name', f"Scanner ({request.remote_addr})"),
        "iso_timestamp": datetime.now(timezone.utc).strftime('%Y-%m-%dT%H:%M:%S.000Z'),
    }
    status_code, body = _submit_db_job("checkin", payload)
    return jsonify(body), status_code

@app.route('/api/register', methods=['POST'])
def process_registration():
    """Thin & fast: validate the shape of the request, hand the actual DB
    work to the write-queue (see _handle_register_job above), then translate
    the result into the exact same JSON shape kiosk clients already expect."""
    data = request.json or {}
    req_ip = request.remote_addr
    req_os = request.user_agent.platform or "Unknown"
    payload = {
        "data": data,
        "mobile_number": data.get('mobile', '').strip(),
        "device_label": data.get('device_name', f"Kiosk ({req_os.capitalize()} - {req_ip})"),
        "iso_timestamp": datetime.now(timezone.utc).strftime('%Y-%m-%dT%H:%M:%S.000Z'),
    }
    status_code, body = _submit_db_job("register", payload)
    return jsonify(body), status_code

@app.route('/api/check_mobile', methods=['GET'])
def check_mobile():
    mobile_number = request.args.get('mobile', '').strip()
    
    if not mobile_number:
        return jsonify({"status": "error", "message": "Mobile number required"}), 400
        
    sessions = get_cached_sessions() or {}
    mysql_factory = sessions.get('mysql')
    if not mysql_factory:
        return jsonify({"status": "error", "message": "Database temporarily unavailable — please try again shortly."}), 503
    session = mysql_factory()

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

    except Exception:
        logging.exception("check_mobile DB error")
        return jsonify({"status": "error", "message": "An internal server error occurred while processing the request."}), 500
    finally:
        try:
            session.close()
        except Exception:
            logging.exception("Error closing DB session in check_mobile")

@app.route('/api/network-data', methods=['GET'])
def get_network_data():
    """Fast, no DB call in the request path at all — reads the in-memory
    device map and the background-refreshed STATS_CACHE. Previously this ran
    3 full-table LIKE-count queries on every single poll from every viewer of
    /stats (every 3s), on top of the GUI running the same queries again."""
    current_time = time.time()
    active_devices = {}
    with device_lock:
        for ip, data in list(ACTIVE_DEVICES.items()):
            if current_time - data['last_seen'] < DEVICE_ONLINE_WINDOW: active_devices[ip] = data
            else: del ACTIVE_DEVICES[ip]

    with stats_lock:
        global_stats = {
            "total_scans": STATS_CACHE["total_scans"],
            "total_registrations": STATS_CACHE["total_registrations"],
            "today_scans": STATS_CACHE["today_scans"],
        }

    return jsonify({"active_devices": active_devices, "global_stats": global_stats}), 200

# ==============================================================================
# MULTI-THREADED WSGI ENGINE THREADS
# ==============================================================================
class WaitressHttpThread(threading.Thread):
    """Runs Waitress WSGI server optimized for 30+ concurrent low-latency LAN connections"""
    def __init__(self, app, host, port):
        super().__init__(daemon=True)  # never let a forgotten "Stop Engine" click hang process exit
        self.server = create_server(app, host=host, port=port, threads=30)
        self.ctx = app.app_context()
        self.ctx.push()

    def run(self):
        try:
            self.server.run()
        except Exception:
            logging.exception("Waitress HTTP engine crashed")

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
        super().__init__(daemon=True)  # never let a forgotten "Stop Engine" click hang process exit
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
        try:
            self.server.start()
        except Exception:
            logging.exception("Cheroot HTTPS engine crashed")

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
# 🎨 UI COLOR HELPERS
# Small, dependency-free hex color math backing the custom styles configured
# in ServerHub._configure_custom_styles(). Deliberately NOT reaching into any
# ttkbootstrap-internal color-mixing helpers — those differ between
# ttkbootstrap versions/releases, whereas plain hex-tuple math works
# identically on any version installed on the operator's machine.
# ==============================================================================
def _hex_to_rgb(hex_color):
    hex_color = hex_color.lstrip('#')
    return tuple(int(hex_color[i:i + 2], 16) for i in (0, 2, 4))

def _rgb_to_hex(rgb):
    return "#{:02x}{:02x}{:02x}".format(*(max(0, min(255, int(round(c)))) for c in rgb))

def _mix_hex(color_a, color_b, weight):
    """Blends color_a toward color_b by `weight` (0 = pure color_a,
    1 = pure color_b). Used to derive a border color that's a subtle step
    lighter than a given panel background, instead of a color hand-picked in
    isolation that could look wrong if the theme is ever switched."""
    a, b = _hex_to_rgb(color_a), _hex_to_rgb(color_b)
    return _rgb_to_hex(a[i] + (b[i] - a[i]) * weight for i in range(3))


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

        with network_latency_lock:
            snap = dict(NETWORK_LATENCY)

        loc_ms = snap["local_ms"]
        self.meter_local.configure(amountused=min(loc_ms, 1000))
        if snap["local_status"] == "ONLINE":
            self.meter_local.configure(bootstyle=SUCCESS if loc_ms < 150 else WARNING)
            self.lbl_local_status.configure(text="Status: LIVE & CONNECTED", bootstyle=SUCCESS)
        else:
            self.meter_local.configure(amountused=0, bootstyle=SECONDARY)
            self.lbl_local_status.configure(text="Status: SERVER OFF", bootstyle=DANGER)

        cf_ms = snap["cloud_ms"]
        self.meter_cloud.configure(amountused=min(cf_ms, 1000))
        if snap["cloud_status"] == "ONLINE":
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
        super().__init__(themename="darkly", title="TDE UP 2026 — Event Hub V2.2 (Hardened)")
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

        # Closing the window with the [X] button used to leave Waitress/Cheroot
        # (non-daemon by default before this pass) running invisibly in the
        # background with the ports still held. Route the close button through
        # a real shutdown instead of Tk's default destroy.
        self.protocol("WM_DELETE_WINDOW", self.on_close)

        threading.Thread(target=self.network_ping_daemon, daemon=True).start()
        # Feeds STATS_CACHE for both this GUI and /api/network-data — runs for
        # the app's whole lifetime, independent of Start/Stop Engine, exactly
        # like the ping daemon above.
        threading.Thread(target=stats_refresher_loop, daemon=True, name="StatsRefresher").start()

    def connect_db(self):
        try:
            sessions = get_cached_sessions() or {}
            self.SessionMySQL = sessions.get('mysql')
            self.SessionSQLite = sessions.get('sqlite')
        except Exception:
            logging.exception("Database connection failed")

    def network_ping_daemon(self):
        global NETWORK_LATENCY
        
        headers = {
            "User-Agent": "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/120.0.0.0 Safari/537.36"
        }
        
        session = requests.Session()
        session.headers.update(headers)

        while True:
            # --- Local Ping ---
            start_local = time.time()
            try:
                session.get(f"http://127.0.0.1:{HTTP_PORT}/api/status", timeout=2)
                local_ms, local_status = int((time.time() - start_local) * 1000), "ONLINE"
            except Exception:
                local_ms, local_status = 0, "OFFLINE"

            # --- Cloudflare Ping ---
            if self.cloudflare_url and self.cloudflare_url != "Offline":
                start_cf = time.time()
                try:
                    # We ping the tunnel
                    session.get(f"{self.cloudflare_url}/api/status", timeout=7, verify=False)
                    
                    # If the above line finishes without crashing, it means Cloudflare responded!
                    # We don't care if it's a 200, 403, 502, or 503 splash page. 
                    # If Cloudflare answered, the tunnel URL is alive.
                    cloud_ms, cloud_status = int((time.time() - start_cf) * 1000), "ONLINE"
                    
                except Exception as e:
                    # It only drops to OFFLINE if the URL literally cannot be found (DNS failure/Timeout)
                    cloud_ms, cloud_status = 0, "OFFLINE"
                    
                    if not hasattr(self, "_last_ping_err") or self._last_ping_err != str(e):
                        self._append_log(self.log_cf, f"[PING ERROR] {str(e)[:100]}...")
                        self._last_ping_err = str(e)
            else:
                cloud_ms, cloud_status = 0, "OFFLINE"

            with network_latency_lock:
                NETWORK_LATENCY["local_ms"] = local_ms
                NETWORK_LATENCY["local_status"] = local_status
                NETWORK_LATENCY["cloud_ms"] = cloud_ms
                NETWORK_LATENCY["cloud_status"] = cloud_status

            time.sleep(3.0)
            
    def process_gui_queue(self):
        while not self.gui_queue.empty():
            try:
                task = self.gui_queue.get_nowait()
                task() 
            except queue.Empty:
                break
        self.after(30, self.process_gui_queue)

    def _append_log(self, scrolled_text_widget, message, tag=None):
        """Thread-safe log append — the only code path allowed to touch a log
        widget's contents. `message` is either:
          - a plain string, colored by `tag` if given, else auto-detected
            from a leading "[PREFIX]" (see _guess_log_tag) — this is what
            every existing self._append_log(widget, f"[SYSTEM] ...") call
            throughout the file already does, unchanged; or
          - a list of (text, tag) segments built by log_event_clean(), for
            lines that need more than one color at once (e.g. a colored
            "CHECKIN" marker followed by a status-code-colored "Status: 404"
            later in the same line).

        Whichever shape it is, the actual Text widget mutation only ever runs
        inside the append() closure queued onto self.gui_queue — so this
        method is always safe to call from a Flask worker, DB writer, or
        network-ping background thread, never just directly on the caller's
        own thread.
        """
        if isinstance(message, (list, tuple)):
            segments = list(message)
        else:
            segments = [(message, tag or _guess_log_tag(message))]

        def append():
            text_widget = scrolled_text_widget.text
            text_widget.configure(state=NORMAL)
            for seg_text, seg_tag in segments:
                if seg_tag:
                    text_widget.insert(END, seg_text, seg_tag)
                else:
                    text_widget.insert(END, seg_text)
            text_widget.insert(END, "\n")
            text_widget.see(END)

            # Cap scrollback so a multi-day live event left running can't
            # quietly grow a log box to hundreds of thousands of lines and
            # bloat memory — trim the oldest lines once comfortably past cap.
            line_count = int(text_widget.index('end-1c').split('.')[0])
            if line_count > MAX_LOG_LINES:
                text_widget.delete('1.0', f'{line_count - MAX_LOG_LINES}.0')

            text_widget.configure(state=DISABLED)
        self.gui_queue.put(append)

    def log_flask_event(self, message):
        # Routes logs directly into the Flask Traffic Log box. `message` is
        # either a plain string or a list of (text, tag) segments — see
        # log_event_clean() and _append_log() above.
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

    def on_close(self):
        """Runs when the operator closes the window with the [X] button.
        Before this, doing that while the engine/tunnel were running left
        Waitress/Cheroot (and possibly cloudflared.exe) alive in the
        background with the ports still held — invisible, no window, still
        running — until the machine was rebooted."""
        try:
            logging.info("Shutdown requested — stopping engine, tunnel, and DB writers...")
            if self.http_thread or self.https_thread:
                self.stop_flask()
            if self.cf_process:
                self.stop_cf()
        except Exception:
            logging.exception("Error while shutting down")
        finally:
            self.destroy()

    def _configure_custom_styles(self):
        """Central place for every custom ttk style this dashboard relies on.
        Called once, first thing in build_ui(), before any widget that uses
        these style names is constructed.

        Fixes two visual bugs by construction rather than by tweaking colors
        until they happen to look right:

        1. THE METRIC-CARD BACKGROUND BUG. A Label styled with just
           `bootstyle=PRIMARY` gets colored TEXT, but the label's own
           background still resolves to the theme's base window background —
           not whatever background its parent Frame actually has. Put that
           label inside a `bootstyle="dark"` card (a different background
           than the window) and the mismatch shows up as a visible rectangle
           behind the number. The fix: compute the card's real background as
           one hex value (self.CARD_BG) and give every label inside it that
           *exact same* hex as its own background — not just a bootstyle
           keyword — so there's nothing left to mismatch.
        2. HARSH BORDERS / LOUD TREEVIEW HEADER. Softened to a muted grey
           derived from each panel's own background (self.SOFT_BORDER),
           instead of the theme's brighter default border and saturated
           accent-blue Treeview heading.
        """
        colors = self.style.colors

        # The actual pixel background every metric card uses. colors.dark
        # reads as a "raised panel" against colors.bg in the darkly theme —
        # the ORIGINAL intent behind bootstyle="dark" cards; the bug was only
        # ever that the child labels didn't match it.
        self.CARD_BG = colors.get("dark")

        # A soft, muted border a few shades lighter than whatever background
        # it's drawn on — enough to still define an edge, nowhere near the
        # theme's brighter default. Recomputed from real colors so it stays
        # sensible even if the ttkbootstrap theme is ever switched.
        self.SOFT_BORDER = _mix_hex(self.CARD_BG, colors.get("fg"), 0.08)
        BG_BORDER = _mix_hex(colors.get("bg"), colors.get("fg"), 0.10)

        # --- Metric cards (DATABASE TELEMETRY / EVENT CHECK-IN METRICS) ---
        self.style.configure(
            "Card.TFrame", background=self.CARD_BG,
            bordercolor=self.SOFT_BORDER, lightcolor=self.SOFT_BORDER,
            darkcolor=self.SOFT_BORDER, borderwidth=1, relief="solid",
        )
        self.style.configure(
            "CardTitle.TLabel", background=self.CARD_BG,
            foreground=_mix_hex(self.CARD_BG, colors.get("fg"), 0.55),
            font="-size 9 -weight bold",
        )
        # One matched-background value style per metric color actually used
        # across the DATABASE TELEMETRY / EVENT CHECK-IN METRICS cards.
        for key in ("primary", "info", "success", "warning", "danger", "light", "secondary"):
            self.style.configure(
                f"CardValue.{key}.TLabel", background=self.CARD_BG,
                foreground=colors.get(key), font="-size 28 -weight bold",
            )

        # --- Generic soft-bordered dark panel (devices table, log boxes) ---
        self.style.configure(
            "Soft.TFrame", background=colors.get("bg"),
            bordercolor=BG_BORDER, lightcolor=BG_BORDER, darkcolor=BG_BORDER,
            borderwidth=1, relief="solid",
        )

        # --- Labelframe (sidebar boxes) — same soft border treatment ---
        self.style.configure(
            "TLabelframe", background=colors.get("bg"),
            bordercolor=BG_BORDER, lightcolor=BG_BORDER, darkcolor=BG_BORDER,
        )
        self.style.configure("TLabelframe.Label", background=colors.get("bg"))

        # --- Log box header strip ---
        self.style.configure(
            "LogHeader.TLabel", background="#252526", foreground="#CCCCCC",
            font="-size 10 -weight bold", padding=10,
        )

        # --- Treeview: a muted, dark-theme-friendly header instead of the
        # theme's saturated accent blue, plus a matching soft border. ---
        self.style.configure(
            "Treeview.Heading",
            background=_mix_hex(self.CARD_BG, colors.get("fg"), 0.12),
            foreground=_mix_hex(self.CARD_BG, colors.get("fg"), 0.82),
            bordercolor=BG_BORDER, relief="flat", font="-size 9 -weight bold",
        )
        self.style.map(
            "Treeview.Heading",
            background=[("active", _mix_hex(self.CARD_BG, colors.get("fg"), 0.20))],
        )
        self.style.configure("Treeview", bordercolor=BG_BORDER, borderwidth=1)

    def build_ui(self):
        self._configure_custom_styles()

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
        
        # width=164, not 160: the QR image itself is 160x160 (see update_qr's
        # resize), but a ttk.Label adds ~4px of its own internal padding
        # around image content (confirmed via winfo_reqwidth()) — matching
        # the label's actual rendered width, not just the image size, is
        # what makes this land pixel-precise under the QR code above it.
        flask_btn_row = ttk.Frame(flask_frame, width=164, height=32)
        flask_btn_row.pack(pady=(5, 5))
        flask_btn_row.pack_propagate(False)
        ttk.Button(flask_btn_row, text="Copy HTTPS", bootstyle="outline-light", command=lambda: self.copy_to_clipboard(self.https_url)).pack(side=LEFT, expand=True, fill=BOTH, padx=(0, 3))
        ttk.Button(flask_btn_row, text="Copy HTTP", bootstyle="outline-info", command=lambda: self.copy_to_clipboard(self.http_url)).pack(side=LEFT, expand=True, fill=BOTH, padx=(3, 0))

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

        cf_btn_row = ttk.Frame(cf_frame, width=164, height=32)  # 164, not 160 — see flask_btn_row comment above
        cf_btn_row.pack(pady=(5, 5))
        cf_btn_row.pack_propagate(False)
        ttk.Button(cf_btn_row, text="Copy Link", bootstyle="outline-light", command=lambda: self.copy_to_clipboard(self.cloudflare_url)).pack(side=LEFT, expand=True, fill=BOTH, padx=(0, 3))
        ttk.Button(cf_btn_row, text="Browser", bootstyle="outline-info", command=lambda: self.open_browser(self.cloudflare_url)).pack(side=LEFT, expand=True, fill=BOTH, padx=(3, 0))

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

        devices_header_row = ttk.Frame(content)
        devices_header_row.pack(fill=X, pady=(5, 5))
        self.lbl_devices_header = ttk.Label(devices_header_row, text="📡 ACTIVE CONNECTED DEVICES", font="-size 11 -weight bold", bootstyle=INFO)
        self.lbl_devices_header.pack(side=LEFT, anchor=W)
        self.lbl_stats_health = ttk.Label(devices_header_row, text="", font="-size 9", bootstyle=WARNING)
        self.lbl_stats_health.pack(side=RIGHT, anchor=E)

        devices_frame = ttk.Frame(content, style="Soft.TFrame")
        devices_frame.pack(fill=X, pady=(0, 20))

        # A slightly tighter row height than the theme default — more devices
        # visible at once without scrolling, without shrinking the font enough
        # to hurt readability on a gate laptop viewed from arm's length.
        self.style.configure("Treeview", rowheight=22)

        # No bootstyle= here on purpose: bootstyle=INFO (the previous value)
        # generates its own "info.Treeview.Heading" style using the theme's
        # saturated accent blue — that's what actually caused the loud blue
        # header, and it can't be muted by reconfiguring "Treeview.Heading"
        # since that's a different, unrelated style name. Leaving bootstyle
        # unset uses the plain "Treeview"/"Treeview.Heading" styles, which
        # _configure_custom_styles() already set up with muted colors.
        self.tree_devices = ttk.Treeview(
            devices_frame,
            columns=("name", "ip", "last_seen", "signal"),
            show="headings",
            height=6,
        )
        self.tree_devices.heading("name", text="Device Name")
        self.tree_devices.heading("ip", text="IP Address")
        self.tree_devices.heading("last_seen", text="Last Heartbeat")
        self.tree_devices.heading("signal", text="Signal")
        self.tree_devices.column("name", width=300, anchor=W)
        self.tree_devices.column("ip", width=150, anchor=W)
        self.tree_devices.column("last_seen", width=120, anchor=CENTER)
        self.tree_devices.column("signal", width=110, anchor=CENTER)
        self.tree_devices.pack(fill=X, padx=2, pady=2)
        # Freshness bands: a device nearing DEVICE_ONLINE_WINDOW (20s) now
        # visibly ambers/oranges before it silently drops off the list,
        # instead of every row always reading an identical static "Online".
        self.tree_devices.tag_configure("online", foreground="#3fd66f")
        self.tree_devices.tag_configure("stale", foreground="#ffbb33")
        self.tree_devices.tag_configure("fading", foreground="#ff8844")
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

        # Previously all 3 logs sat side by side in one row, giving each
        # barely a third of the width and causing aggressive text wrapping.
        # Now: the busiest, most information-dense log — every checkin/
        # register event, fully color-coded — gets a full-width row of its
        # own on top (weighted 3 vs 2, i.e. ~60% of the available height).
        # The two lower-traffic, mostly-status logs share the row below.
        logs_frame.columnconfigure(0, weight=1)
        logs_frame.rowconfigure(0, weight=3)
        logs_frame.rowconfigure(1, weight=2)

        logs_top_row = ttk.Frame(logs_frame)
        logs_top_row.grid(row=0, column=0, sticky="nsew", pady=(0, 10))
        self.log_flask = self._create_log_box(logs_top_row, "📟 Live Operator Activity & API Logs")

        logs_bottom_row = ttk.Frame(logs_frame)
        logs_bottom_row.grid(row=1, column=0, sticky="nsew")
        self.log_network = self._create_log_box(logs_bottom_row, "🌐 Devices & Network Routing")
        self.log_cf = self._create_log_box(logs_bottom_row, "☁️ Cloudflare Tunnel Status")

        footer = ttk.Frame(content)
        footer.pack(fill=X, pady=(15, 0))
        ttk.Label(footer, text="Engineered for Event Resilience • Powered by EllowDigital", font="-size 9", foreground="#666").pack(side=RIGHT)

        self._append_log(self.log_network, f"System Boot: {datetime.now().strftime('%Y-%m-%d %H:%M:%S')}")
        self._append_log(self.log_network, f"Local Network IP Address Detected: {self.local_ip}")

    def _create_stat_card(self, parent, title, initial_value, style, var_name):
        # height=118 + pack_propagate(False): every card in a row is the same
        # height regardless of content, instead of the row squishing/growing
        # based on whichever card happens to be tallest.
        frame = ttk.Frame(parent, style="Card.TFrame", padding=(18, 16), height=118)
        frame.pack(side=LEFT, fill=BOTH, expand=True, padx=6, pady=2)
        frame.pack_propagate(False)

        # style="CardValue.<color>.TLabel" (not bootstyle=) — these styles
        # were configured in _configure_custom_styles() with a background
        # that EXACTLY matches this card's own background. That's the actual
        # fix for the old floating-rectangle bug: bootstyle=style alone gave
        # colored text but left the label's background at the theme's base
        # window color, which doesn't match this card's "dark" background.
        ttk.Label(frame, text=title, style="CardTitle.TLabel").pack(anchor=CENTER)
        val_lbl = ttk.Label(frame, text=initial_value, style=f"CardValue.{style}.TLabel")
        val_lbl.pack(anchor=CENTER, expand=True, pady=(8, 0))
        self.stat_vars[var_name] = val_lbl

    def _create_log_box(self, parent, title):
        frame = ttk.Frame(parent, style="Soft.TFrame")
        frame.pack(side=LEFT, fill=BOTH, expand=True, padx=6, pady=2)
        ttk.Label(frame, text=title, style="LogHeader.TLabel").pack(anchor=W, fill=X)
        log_box = ScrolledText(frame, font=("Consolas", 10))
        log_box.pack(fill=BOTH, expand=True, padx=2, pady=2)
        text_widget = log_box.text
        text_widget.configure(state=DISABLED, bg="#1E1E1E", fg="#D4D4D4", insertbackground="#D4D4D4", selectbackground="#264F78", borderwidth=0)

        # --- Color tags for at-a-glance readability during a live event ---
        # System/info text stays neutral; status codes and API actions get
        # distinct, consistent colors so an operator can scan hundreds of
        # lines and immediately spot errors or watch REGISTER/CHECKIN flow.
        text_widget.tag_configure("log_default", foreground="#D4D4D4")                                    # general system/info text
        text_widget.tag_configure("log_dim",     foreground="#6A7178")                                    # timestamps — present, out of the way
        text_widget.tag_configure("log_success", foreground="#4CD37E")                                    # 2xx status codes / [SUCCESS]
        text_widget.tag_configure("log_warning", foreground="#FFB454")                                    # 4xx status codes / [WARNING]
        text_widget.tag_configure("log_error",   foreground="#FF6B6B")                                    # 5xx status codes / [ERROR]
        text_widget.tag_configure("log_info",    foreground="#5DADE2")                                    # [CLIPBOARD] / misc info
        text_widget.tag_configure("log_register",foreground="#6EC6FF", font=("Consolas", 10, "bold"))     # REGISTER events — light blue
        text_widget.tag_configure("log_checkin", foreground="#C792EA", font=("Consolas", 10, "bold"))     # CHECKIN events — soft purple
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
        """Runs every 4s on the Tkinter main thread — but now only ever touches
        in-memory dicts guarded by locks, never the DB directly. Before this
        pass, the second half of this method opened a fresh MySQL session and
        ran 5 queries right here, on the main thread: a slow/laggy round-trip
        (a momentary network hiccup, MySQL busy with a burst of check-ins)
        froze the entire operator window for as long as the query took."""
        current_time = time.time()

        # Protect dictionary access properly on Tkinter's main loop
        with device_lock:
            active_ips = [ip for ip, data in ACTIVE_DEVICES.items() if current_time - data['last_seen'] < DEVICE_ONLINE_WINDOW]
            device_info = {ip: dict(ACTIVE_DEVICES[ip]) for ip in active_ips}

        self.stat_vars["online_scanners"].configure(text=str(len(active_ips)))
        if hasattr(self, 'lbl_devices_header'):
            self.lbl_devices_header.configure(text=f"📡 ACTIVE CONNECTED DEVICES ({len(active_ips)})")

        for row in self.tree_devices.get_children():
            self.tree_devices.delete(row)

        if active_ips:
            sorted_ips = sorted(active_ips, key=lambda ip: device_info[ip]['name'].lower())
            for ip in sorted_ips:
                name = device_info[ip]['name']
                seconds_ago = max(0, int(current_time - device_info[ip]['last_seen']))
                last_seen_text = "just now" if seconds_ago < 2 else f"{seconds_ago}s ago"
                # "Last Seen" used to always read a static "🟢 Online" / "Active"
                # for every row regardless of actual heartbeat age. This shows
                # real freshness, so a device about to drop off (nearing
                # DEVICE_ONLINE_WINDOW) is visible before it actually vanishes.
                if seconds_ago < 8:
                    signal, tag = "🟢 Live", "online"
                elif seconds_ago < 15:
                    signal, tag = "🟡 Slow", "stale"
                else:
                    signal, tag = "🟠 Fading", "fading"
                self.tree_devices.insert("", END, values=(name, ip, last_seen_text, signal), tags=(tag,))
        else:
            self.tree_devices.insert("", END, values=("No devices connected yet — awaiting heartbeat...", "", "", ""), tags=("empty",))

        if self.SessionMySQL: self.lbl_stat_mysql.configure(text="● MYSQL: LIVE", bootstyle=SUCCESS)
        else: self.lbl_stat_mysql.configure(text="● MYSQL: OFFLINE", bootstyle=DANGER)

        if self.SessionSQLite: self.lbl_stat_sqlite.configure(text="● SQLITE: MIRROR ACTIVE", bootstyle=SUCCESS)
        else: self.lbl_stat_sqlite.configure(text="● SQLITE: FAULT", bootstyle=DANGER)

        # Read the background-refreshed cache instead of querying MySQL here.
        with stats_lock:
            snap = dict(STATS_CACHE)

        self.stat_vars["total_att"].configure(text=str(snap["total_attendees"]))
        self.stat_vars["kiosk_reg"].configure(text=str(snap["total_registrations"]))
        self.stat_vars["sqlite_total"].configure(text=str(snap["total_attendees"]))
        self.stat_vars["chk_30"].configure(text=str(snap["chk_30"]))
        self.stat_vars["chk_31"].configure(text=str(snap["chk_31"]))
        self.stat_vars["chk_01"].configure(text=str(snap["chk_01"]))
        self.stat_vars["chk_today"].configure(text=str(snap["today_scans"]))
        self.stat_vars["chk_total"].configure(text=str(snap["total_scans"]))

        # Surface it if the background refresher has been failing quietly,
        # instead of the numbers just looking frozen with no explanation.
        if hasattr(self, 'lbl_stats_health'):
            stale_for = (current_time - snap["last_refreshed"]) if snap["last_refreshed"] else None
            if snap["last_error"] and stale_for and stale_for > STATS_REFRESH_INTERVAL_SEC * 4:
                self.lbl_stats_health.configure(text=f"⚠ Stats stale ({int(stale_for)}s): {snap['last_error']}", bootstyle=WARNING)
            else:
                self.lbl_stats_health.configure(text="")

        self.after(4000, self.refresh_stats)

    def start_flask(self):
        self.btn_start_flask.configure(state=DISABLED)
        self._append_log(self.log_flask, f"[{datetime.now().strftime('%H:%M:%S')}] Booting Waitress Multi-Threaded Engine...")

        # DB writer pool first: the HTTP/HTTPS engines must never be able to
        # accept a checkin/register request before something exists to drain
        # the queue it gets enqueued onto.
        start_db_writers()
        self._append_log(self.log_flask, f"[SYSTEM] {DB_WRITER_THREADS} DB writer thread(s) started.")

        try:
            # Start Waitress HTTP Engine (30 Concurrent Threads)
            self.http_thread = WaitressHttpThread(app, '0.0.0.0', HTTP_PORT)
            self.http_thread.start()

            # Start HTTPS Engine for iOS Camera Fallback
            self.https_thread = HttpsFlaskThread(app, '0.0.0.0', HTTPS_PORT)
            self.https_thread.start()
        except Exception as e:
            # e.g. port already in use, or cheroot missing — fail cleanly
            # instead of leaving buttons/threads in a half-started state.
            logging.exception("Failed to start engine")
            self._append_log(self.log_flask, f"[ERROR] Failed to start engine: {e}")
            if self.http_thread:
                self.http_thread.shutdown()
                self.http_thread = None
            self.https_thread = None
            stop_db_writers()
            self.btn_start_flask.configure(state=NORMAL)
            Messagebox.show_error(f"Could not start the engine:\n\n{e}", "Engine Start Failed", parent=self)
            return

        self.btn_stop_flask.configure(state=NORMAL)
        self.btn_start_cf.configure(state=NORMAL)

        self.update_qr(self.lbl_flask_qr, self.https_url)
        self.lbl_flask_link.configure(text=self.https_url, foreground="#4D9CE6")
        
        self._append_log(self.log_flask, f"[SYSTEM] Waitress HTTP listening on: {self.http_url}")
        self._append_log(self.log_flask, f"[SYSTEM] iOS Secure HTTPS listening on: {self.https_url}")
        logging.info(f"Engine started — HTTP {self.http_url} / HTTPS {self.https_url}")
        
    def stop_flask(self):
        if self.btn_stop_cf['state'] == NORMAL: self.stop_cf()
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
                    bufsize=1,
                    creationflags=creationflags
                )
                
                url_found = False
                for line in self.cf_process.stdout:
                    clean_line = re.sub(r'\x1b\[[0-9;]*m', '', line)
                    self._append_log(self.log_cf, clean_line.strip())
                    
                    if not url_found:
                        match = re.search(r"https://[a-zA-Z0-9-]+\.trycloudflare\.com", clean_line)
                        if match:
                            tunnel_url = match.group(0)
                            self.cloudflare_url = tunnel_url
                            url_found = True
                            
                            # Give Cloudflare DNS a full 30 seconds to propagate globally
                            self._append_log(self.log_cf, "[INFO] Waiting 30 seconds for Cloudflare Edge DNS propagation...")
                            time.sleep(30)
                            
                            self.gui_queue.put(lambda u=tunnel_url: self.update_qr(self.lbl_cf_qr, u))
                            self.gui_queue.put(lambda u=tunnel_url: self.lbl_cf_link.configure(text=u, foreground="#4D9CE6"))
                            self.gui_queue.put(lambda: self.lbl_stat_cf.configure(text="● Cloudflare: LIVE", bootstyle=SUCCESS))
                            self._append_log(self.log_cf, f"[SUCCESS] Tunnel active at: {self.cloudflare_url}")
                            
            except FileNotFoundError:
                # Covers all 3 places it might be missing from — MSI install,
                # sitting next to the script, or on PATH — in one clear message
                # instead of three near-duplicate handlers only the first of
                # which could ever actually fire.
                logging.error("'cloudflared' binary not found")
                self.gui_queue.put(self.stop_cf)
                self._append_log(
                    self.log_cf,
                    "[ERROR] 'cloudflared' not found. Make sure the MSI installer finished "
                    "successfully, or that cloudflared.exe sits next to server_hub.py, or "
                    "that 'cloudflared' is on your system PATH.",
                )
            except Exception as e:
                logging.exception("Cloudflare tunnel failed")
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
                # Force kill the process tree to clear zombie cloudflared.exe on Windows
                if platform.system() == "Windows":
                    subprocess.run(['taskkill', '/F', '/T', '/PID', str(self.cf_process.pid)], stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)
                else:
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