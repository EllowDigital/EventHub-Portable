import os
import sys
import csv
import math
import random
import threading
import time
import uuid
from collections import deque, Counter
from dataclasses import dataclass
from datetime import datetime, timezone

import requests
from requests.adapters import HTTPAdapter
from urllib3.util.retry import Retry
import urllib3

from sqlalchemy import select, update, delete

# --- PYSIDE6 INTEGRATION ---
os.environ["QT_ENABLE_HIGHDPI_SCALING"] = "1"
os.environ["QT_AUTO_SCREEN_SCALE_FACTOR"] = "1"

from PySide6.QtCore import Qt, QTimer, QRectF, QObject, Signal, Slot
from PySide6.QtGui import QIcon, QColor, QFont, QPainter, QPen
from PySide6.QtWidgets import (
    QApplication, QMainWindow, QWidget, QVBoxLayout, QHBoxLayout,
    QGridLayout, QLabel, QPushButton, QTableWidget, QTableWidgetItem, 
    QTabWidget, QPlainTextEdit, QGroupBox, QComboBox, 
    QHeaderView, QMessageBox, QFrame, QSplitter, QScrollArea, 
    QSizePolicy, QLineEdit, QSpinBox, QDoubleSpinBox, QFileDialog, QFormLayout
)

# --- MATPLOTLIB INTEGRATION FOR QT ---
try:
    import matplotlib
    matplotlib.use("QtAgg")
    import matplotlib.pyplot as plt
    from matplotlib.backends.backend_qtagg import FigureCanvasQTAgg as FigureCanvas
    MATPLOTLIB_AVAILABLE = True
except ImportError:
    MATPLOTLIB_AVAILABLE = False

# ==============================================================================
# PATHS & SCHEMA IMPORTS
# ==============================================================================
SCRIPT_DIR = os.path.dirname(os.path.abspath(__file__))
BASE_DIR = SCRIPT_DIR
APP_DIR = os.path.join(SCRIPT_DIR, "app")

if SCRIPT_DIR not in sys.path: sys.path.insert(0, SCRIPT_DIR)
if APP_DIR not in sys.path: sys.path.insert(0, APP_DIR)

try:
    from schema import Attendee, OfflineKioskAttendee, get_database_sessions
except ImportError as exc:
    raise SystemExit(f"FATAL: could not import schema.py.\nOriginal error: {exc}")

urllib3.disable_warnings(urllib3.exceptions.InsecureRequestWarning)

SYNTHETIC_NAME_PREFIX = "Enterprise Tester"
DEFAULT_POOL_CAP = 20000
DEFAULT_EVENT_DAYS = "30 August,31 August,1 September"
DEFAULT_EVENT_YEAR = 2026

# ==============================================================================
# DATA CLASSES & METRICS
# ==============================================================================
@dataclass(frozen=True)
class AttendeeRef:
    attendee_id: str
    mobile: str
    source: str

@dataclass(frozen=True)
class RunConfig:
    mode: str
    sync_mode: str
    timeout: float
    event_day_labels: tuple
    event_dates: tuple

def calculate_percentile(sorted_data, percentile):
    if not sorted_data: return 0.0
    index = math.ceil((len(sorted_data) * percentile) / 100) - 1
    return sorted_data[max(0, min(index, len(sorted_data) - 1))]

def calculate_apdex(data, satisfied_ms=500):
    if not data: return 0.0
    tolerating_ms = satisfied_ms * 4
    satisfied = sum(1 for x in data if x <= satisfied_ms)
    tolerating = sum(1 for x in data if satisfied_ms < x <= tolerating_ms)
    return (satisfied + (tolerating / 2)) / len(data)

def build_verdict(total, success_200, server_errors, p95, apdex):
    if total == 0: return "⚪ IDLE", "System armed and awaiting load injection commands."
    error_rate = server_errors / total if total else 0
    if error_rate > 0.02 or apdex < 0.5:
        return "🔴 CRITICAL", f"{error_rate:.1%} errors/timeouts. Apdex: {apdex:.2f}. Target is heavily bottlenecked."
    if server_errors > 0 or p95 > 1000 or apdex < 0.85:
        return "🟡 DEGRADED", f"{success_200/total:.1%} success. P95 latency is {p95:.0f}ms. Apdex: {apdex:.2f}."
    return "🟢 HEALTHY", f"{success_200/total:.1%} success rate. P95 latency {p95:.0f}ms. Target is absorbing load effortlessly."

def classify_status(code):
    if code == "TIMEOUT": return "timeouts"
    if code == "CONN_REFUSED": return "conn_refused"
    if code == "NETWORK_ERR": return "network_err"
    if isinstance(code, int):
        if code == 200: return "ok_200"
        if code == 400: return "dup_400"
        if code == 403: return "denied_403"
        if code == 404: return "notfound_404"
        if code == 503: return "queue_503"
        if code >= 500: return "server_5xx"
    return "other"

def parse_event_days(day_strings, year):
    parsed = []
    for raw in day_strings:
        raw = raw.strip()
        if not raw: continue
        try: parsed.append(datetime.strptime(f"{raw} {year}", "%d %B %Y"))
        except ValueError: continue
    return parsed

def random_event_timestamp(event_dates):
    base = random.choice(event_dates)
    dt = base.replace(hour=random.randint(8, 19), minute=random.randint(0, 59), second=random.randint(0, 59), tzinfo=timezone.utc)
    return dt.strftime("%Y-%m-%dT%H:%M:%S.000Z")

class _UniqueIdentityGenerator:
    def __init__(self):
        self._lock = threading.Lock()
        self._next = random.randint(0, 100_000_000)

    def next_pair(self):
        with self._lock:
            n = self._next
            self._next += 1
        suffix = 100_000_000 + (n % 900_000_000)
        return n, f"9{suffix}"

_identity_gen = _UniqueIdentityGenerator()

# ==============================================================================
# PAYLOAD GENERATORS
# ==============================================================================
def generate_registration_payload(device_name, device_id, event_day_labels):
    att_types = ["GENERAL", "BUSINESS", "MEDIA", "EXHIBITOR"]
    selected_type = random.choice(att_types)
    biz_name = f"Simulated Corp {random.randint(100, 9999)}" if selected_type != "GENERAL" else None
    n, mobile = _identity_gen.next_pair()
    return {
        "full_name": f"{SYNTHETIC_NAME_PREFIX} {n}",
        "mobile": mobile, "email": f"tester{n}@example.com",
        "gender": random.choice(["MALE", "FEMALE", "OTHER"]),
        "attendee_type": selected_type, "business_name": biz_name,
        "business_category": "OTHER" if selected_type != "GENERAL" else None,
        "other_category": "Stress Test Injector",
        "address": "123 Enterprise Load Test Ave", "city": "Lucknow",
        "state": "Uttar Pradesh", "pincode": "226001",
        "attendance_days": list(event_day_labels),
        "device_name": device_name, "device_id": device_id,
    }

def generate_checkin_payload(user: AttendeeRef, device_name, device_id, event_dates):
    search_type = random.choice(["id", "phone"])
    identifier = user.attendee_id if search_type == "id" else user.mobile
    return {
        "attendee_id": identifier, "search_type": search_type,
        "device_name": device_name, "device_id": device_id,
        "offline_scan_time": random_event_timestamp(event_dates),
    }

# ==============================================================================
# DATABASE MANAGEMENT
# ==============================================================================
def fetch_attendee_pool(session_factory, cap_per_table, exclude_synthetic=True):
    pool = []
    counts = {"attendees": 0, "kiosk": 0}
    with session_factory() as db:
        att_stmt = select(Attendee.attendee_id, Attendee.mobile)
        kiosk_stmt = select(OfflineKioskAttendee.attendee_id, OfflineKioskAttendee.mobile)
        if exclude_synthetic:
            att_stmt = att_stmt.where(Attendee.full_name.notlike(f"{SYNTHETIC_NAME_PREFIX}%"))
            kiosk_stmt = kiosk_stmt.where(OfflineKioskAttendee.full_name.notlike(f"{SYNTHETIC_NAME_PREFIX}%"))
        att_stmt = att_stmt.limit(cap_per_table)
        kiosk_stmt = kiosk_stmt.limit(cap_per_table)
        for row in db.execute(att_stmt): pool.append(AttendeeRef(row.attendee_id, row.mobile, "attendees"))
        counts["attendees"] = len(pool)
        kiosk_start = len(pool)
        for row in db.execute(kiosk_stmt): pool.append(AttendeeRef(row.attendee_id, row.mobile, "kiosk"))
        counts["kiosk"] = len(pool) - kiosk_start
    return pool, counts

def load_full_attendee_pool(session_factories, cap_per_table, log_signal):
    combined = []
    seen_ids = set()
    counts_total = Counter()
    for backend_name in ("mysql", "sqlite"):
        factory = session_factories.get(backend_name)
        if not factory: continue
        try: rows, counts = fetch_attendee_pool(factory, cap_per_table, exclude_synthetic=True)
        except Exception as e:
            log_signal.emit(f"WARNING: could not read attendees from {backend_name.upper()}: {e}")
            continue
        added = 0
        for ref in rows:
            if ref.attendee_id in seen_ids: continue
            seen_ids.add(ref.attendee_id)
            combined.append(ref)
            added += 1
        counts_total[backend_name] += added
        log_signal.emit(f"[{backend_name.upper()}] Loaded {added} real attendees ({counts['attendees']} base, {counts['kiosk']} kiosk).")
    return combined, counts_total

def reset_synthetic_checkin_history(session_factory):
    with session_factory() as db:
        db.execute(update(Attendee).where(Attendee.full_name.like(f"{SYNTHETIC_NAME_PREFIX}%")).values(checkin_history={}))
        db.execute(update(OfflineKioskAttendee).where(OfflineKioskAttendee.full_name.like(f"{SYNTHETIC_NAME_PREFIX}%")).values(checkin_history={}))
        db.commit()

def purge_synthetic_attendees(session_factory):
    with session_factory() as db:
        r1 = db.execute(delete(Attendee).where(Attendee.full_name.like(f"{SYNTHETIC_NAME_PREFIX}%")))
        r2 = db.execute(delete(OfflineKioskAttendee).where(OfflineKioskAttendee.full_name.like(f"{SYNTHETIC_NAME_PREFIX}%")))
        db.commit()
        return r1.rowcount, r2.rowcount

def reset_all_checkin_history(session_factory):
    with session_factory() as db:
        db.execute(update(Attendee).values(checkin_history={}))
        db.execute(update(OfflineKioskAttendee).values(checkin_history={}))
        db.commit()

def clear_all_kiosk_registrations(session_factory):
    with session_factory() as db:
        r1 = db.execute(delete(OfflineKioskAttendee))
        db.commit()
        return r1.rowcount

# ==============================================================================
# PYSIDE6 SIGNALS (HANG-FREE UI COMMUNICATION)
# ==============================================================================
class AppSignals(QObject):
    log_msg = Signal(str)
    metrics_update = Signal(dict, list, list, list, int, int) # metrics, chk_rts, reg_rts, tree_items, live_rps, peak_rps
    btn_state = Signal(str, str, bool) # target_btn_name, text, is_enabled
    reset_ui = Signal()
    pool_loaded = Signal(int, dict)

# ==============================================================================
# CUSTOM PYSIDE6 WIDGETS
# ==============================================================================
class SpeedometerGauge(QWidget):
    def __init__(self, subtext="RPS", unit="req/s", max_val=100, good_is_high=False, parent=None):
        super().__init__(parent)
        self.subtext = subtext
        self.unit = unit
        self.max_val = max_val
        self.current_val = 0.0
        self.target_val = 0.0
        self.good_is_high = good_is_high
        self.setMinimumSize(120, 110)
        self.setSizePolicy(QSizePolicy.Expanding, QSizePolicy.Minimum)

    def set_target(self, val):
        self.target_val = float(val)

    def tick(self):
        diff = self.target_val - self.current_val
        if abs(diff) > 0.1:
            self.current_val += diff * 0.30 
            self.update()
        elif self.current_val != self.target_val:
            self.current_val = self.target_val
            self.update()

    def paintEvent(self, event):
        painter = QPainter(self)
        painter.setRenderHint(QPainter.Antialiasing)
        w, h = self.width(), self.height()
        diameter = min(w, h - 20)
        margin_x = (w - diameter) / 2.0
        margin_y = 5.0
        arc_rect = QRectF(margin_x + 6, margin_y + 6, diameter - 12, diameter - 12)
        arc_width = max(5.0, diameter * 0.08)

        ratio = min(max(self.current_val / self.max_val if self.max_val > 0 else 0.0, 0.0), 1.0)
        
        if self.good_is_high: dynamic_color = QColor("#4EC9B0") if ratio > 0.5 else QColor("#D7BA7D")
        else: dynamic_color = QColor("#F44747") if ratio > 0.85 else (QColor("#D7BA7D") if ratio > 0.6 else QColor("#4EC9B0"))

        pen_bg = QPen(QColor("#333333"), arc_width, Qt.DotLine, Qt.RoundCap)
        painter.setPen(pen_bg)
        painter.drawArc(arc_rect, 200 * 16, -220 * 16)

        active_span = -int(220 * ratio * 16)
        pen_fg = QPen(dynamic_color, arc_width, Qt.SolidLine, Qt.RoundCap)
        painter.setPen(pen_fg)
        if active_span != 0: painter.drawArc(arc_rect, 200 * 16, active_span)

        painter.setPen(dynamic_color)
        font_size = max(16, int(diameter * 0.28))
        painter.setFont(QFont("Segoe UI", font_size, QFont.Bold))
        val_text = str(int(round(self.current_val)))
        val_rect = QRectF(margin_x, margin_y + (diameter * 0.2), diameter, diameter * 0.45)
        painter.drawText(val_rect, Qt.AlignCenter, val_text)

        painter.setPen(QColor("#AAAAAA"))
        sub_font_size = max(9, int(diameter * 0.12))
        painter.setFont(QFont("Segoe UI", sub_font_size, QFont.Normal))
        lbl_rect = QRectF(0, h - 20, w, 20)
        painter.drawText(lbl_rect, Qt.AlignCenter, self.subtext)


# ==============================================================================
# MAIN GUI APPLICATION
# ==============================================================================
class EnterpriseStressTestApp(QMainWindow):
    def __init__(self):
        super().__init__()
        self.setWindowTitle("TDE UP 2026 - Enterprise Load Injector (PySide6)")
        self.resize(1300, 850)
        self.setMinimumSize(1100, 700)
        
        # Integrate Application Icon securely
        icon_path = os.path.join(BASE_DIR, "assets", "EventHub.ico")
        if os.path.exists(icon_path):
            self.setWindowIcon(QIcon(icon_path))
        
        self.setStyleSheet("""
            QMainWindow, QWidget { background-color: #161618; color: #D4D4D4; font-family: 'Segoe UI', sans-serif; font-size: 12px; }
            QScrollArea { border: none; background: transparent; }
            QGroupBox { border: 1px solid #2D2D30; border-radius: 5px; margin-top: 12px; padding-top: 15px; background: #1C1C1E; }
            QGroupBox::title { subcontrol-origin: margin; left: 8px; padding: 0 4px; color: #569CD6; font-weight: bold; font-size: 11px; }
            QPushButton { background-color: #2D2D30; color: #FFF; border: 1px solid #3E3E42; border-radius: 4px; padding: 6px 12px; font-weight: bold; }
            QPushButton:hover { background-color: #3E3E42; border-color: #569CD6; }
            QPushButton:pressed { background-color: #1E1E22; border-color: #4EC9B0; }
            QPushButton:disabled { background-color: #161618; color: #555555; border-color: #2A2A2C; }
            
            QPushButton#btn_start { background-color: #107C41; color: white; border: 1px solid #107C41; padding: 10px; font-size: 13px;}
            QPushButton#btn_start:hover { background-color: #0c5e31; }
            QPushButton#btn_start:disabled { background-color: #0c5e31; color: #ffffff; border: 1px solid #0c5e31; }
            
            QPushButton#btn_stop { background-color: transparent; color: #F44747; border: 1px solid #F44747; padding: 10px; font-size: 13px;}
            QPushButton#btn_stop:hover { background-color: rgba(244, 71, 71, 0.1); }
            QPushButton#btn_stop:disabled { background-color: transparent; color: #555555; border: 1px solid #2A2A2C; }
            
            QLineEdit, QSpinBox, QDoubleSpinBox, QComboBox { background-color: #252528; color: #FFFFFF; border: 1px solid #3E3E42; border-radius: 4px; padding: 5px; }
            QLineEdit:focus, QSpinBox:focus { border: 1px solid #569CD6; }
            
            QTableWidget { background-color: #19191B; gridline-color: #28282B; border: 1px solid #2D2D30; border-radius: 4px; selection-background-color: #094771; }
            QHeaderView::section { background-color: #202022; color: #569CD6; padding: 5px; border: 1px solid #28282B; font-weight: bold; }
            
            QPlainTextEdit { background-color: #0D0D0F; color: #D4D4D4; font-family: 'Consolas', monospace; font-size: 11px; border: 1px solid #2D2D30; border-radius: 4px; }
            QTabWidget::pane { border: 1px solid #2D2D30; background: #1C1C1E; border-radius: 4px; top: -1px; }
            QTabBar::tab { background: #252528; color: #888888; padding: 8px 15px; border: 1px solid #2D2D30; font-weight: bold; border-top-left-radius: 4px; border-top-right-radius: 4px; }
            QTabBar::tab:selected { background: #1C1C1E; color: #569CD6; border-bottom: none; }
            QSplitter::handle { background-color: #2D2D30; width: 4px; margin: 0px 4px; border-radius: 2px; }
        """)

        # Threading/Queues
        self.stats_queue = queue.Queue()
        self.user_pool = []
        self.pool_counts = Counter()
        self.pool_lock = threading.Lock()
        
        # Signal Connections
        self.signals = AppSignals()
        self.signals.log_msg.connect(self._ui_log)
        self.signals.btn_state.connect(self._ui_btn_state)
        self.signals.pool_loaded.connect(self._ui_pool_loaded)
        self.signals.metrics_update.connect(self._ui_update_metrics)
        self.signals.reset_ui.connect(self.reset_stats)

        # Metrics Data
        self.data_lock = threading.Lock()
        self.metrics = Counter()
        self.rts_checkin = deque(maxlen=10000)
        self.rts_register = deque(maxlen=10000)
        self.results_history = deque(maxlen=100000)
        self.tree_buffer = []
        self.recent_activity = deque()
        self.peak_rps = 0
        self.throughput_history = deque(maxlen=300)
        self.plot_rts_history = deque(maxlen=300)

        # Run State
        self.is_running = False
        self.start_time = 0.0
        self.test_duration = 0
        self.sync_barrier = None
        self._session_factories = None

        self.stat_widgets = {}
        self.chk_metrics = {}
        self.reg_metrics = {}
        self.err_labels = {}

        self.setup_ui()

        # Dedicated High-Speed Aggregator Thread
        threading.Thread(target=self._data_aggregator_loop, daemon=True).start()

        # Setup Hardware Accelerated UI Timers
        self.timer_anim = QTimer(self)
        self.timer_anim.timeout.connect(self.animation_loop)
        self.timer_anim.start(16) 

        self.reload_attendee_pool(initial=True)

    # ==========================================================================
    # BACKGROUND AGGREGATOR (100% Hang-Free)
    # ==========================================================================
    def _data_aggregator_loop(self):
        last_emit = time.time()
        while True:
            try:
                code, rt, env_name, req_type, identifier = self.stats_queue.get(timeout=0.1)
                
                bucket = classify_status(code)
                now = time.time()
                clock = time.strftime("%H:%M:%S")

                with self.data_lock:
                    self.metrics["total"] += 1
                    self.metrics[bucket] += 1
                    self.recent_activity.append(now)

                    if rt > 0:
                        if req_type == "checkin": self.rts_checkin.append(rt)
                        elif req_type == "register": self.rts_register.append(rt)

                    record = (clock, env_name, req_type, code, f"{rt:.0f}", identifier)
                    self.results_history.append(record)

                    self.tree_buffer.append(record)
                    if len(self.tree_buffer) > 40: self.tree_buffer.pop(0)

                self.stats_queue.task_done()
            except queue.Empty:
                pass
            
            # Emit batch updates to the UI exactly every 500ms to prevent freezing
            now = time.time()
            if now - last_emit >= 0.5:
                self._emit_metrics(now)
                last_emit = now

    def _emit_metrics(self, now):
        with self.data_lock:
            # Clean old activity for RPS calculation
            while self.recent_activity and now - self.recent_activity[0] > 1.0:
                self.recent_activity.popleft()
            
            live_rps = len(self.recent_activity)
            if live_rps > self.peak_rps: self.peak_rps = live_rps
            
            metrics_snap = dict(self.metrics)
            chk_snap = list(self.rts_checkin)
            reg_snap = list(self.rts_register)
            tree_snap = list(self.tree_buffer)
            self.tree_buffer.clear()
            
            self.signals.metrics_update.emit(metrics_snap, chk_snap, reg_snap, tree_snap, live_rps, self.peak_rps)

    def _get_sessions(self):
        if self._session_factories is None:
            self._session_factories = get_database_sessions()
        return self._session_factories

    # ==========================================================================
    # DATABASE TOOLS & UNIVERSAL BUTTON STATE ENGINE
    # ==========================================================================
    def reload_attendee_pool(self, initial=False):
        if self.is_running: return self.signals.log_msg.emit("Stop test before reloading pool.")
        
        self.signals.btn_state.emit("btn_reload", "⏳ LOADING...", False)
        if not initial: self.signals.btn_state.emit("btn_start", "▶ INJECT LOAD", False)
        
        cap = self.pool_cap_var.value()
        self.signals.log_msg.emit("Connecting to database to load the real attendee pool...")
        threading.Thread(target=self._load_pool_worker, args=(cap,), daemon=True).start()

    def _load_pool_worker(self, cap):
        try:
            sessions = self._get_sessions()
            pool, counts = load_full_attendee_pool(sessions, cap, log_signal=self.signals.log_msg)
            with self.pool_lock:
                self.user_pool = pool
                self.pool_counts = counts
            if pool: self.signals.log_msg.emit(f"SUCCESS: {len(pool)} real attendees loaded and available for check-in testing.")
            else: self.signals.log_msg.emit("WARNING: No attendees found. Run a Registration load test to create synthetic attendees first.")
        except Exception as e: self.signals.log_msg.emit(f"ERROR loading attendee pool: {e}")
        finally:
            self.signals.pool_loaded.emit(len(self.user_pool), dict(self.pool_counts))
            self.signals.btn_state.emit("btn_reload", "✅ LOADED", True)
            self.signals.btn_state.emit("btn_start", "▶ INJECT LOAD", True)
            # Auto-reset text after delay
            QTimer.singleShot(2000, lambda: self.signals.btn_state.emit("btn_reload", "🔄 Reload Attendee Pool", True))

    def on_reset_synthetic_history(self):
        if self.is_running: return self.signals.log_msg.emit("Stop test before modifying database.")
        res = QMessageBox.warning(self, "Reset Test History", f"Clear checkin_history ONLY for '{SYNTHETIC_NAME_PREFIX} *' attendees?", QMessageBox.StandardButton.Yes | QMessageBox.StandardButton.No)
        if res != QMessageBox.StandardButton.Yes: return
        
        self.signals.log_msg.emit("Resetting synthetic check-in history...")
        self.signals.btn_state.emit("btn_reset_synth", "⏳ WORKING...", False)
        threading.Thread(target=self._reset_synth_worker, daemon=True).start()

    def _reset_synth_worker(self):
        try:
            sessions = self._get_sessions()
            touched = sum(1 for backend in ("mysql", "sqlite") if sessions.get(backend) and not reset_synthetic_checkin_history(sessions.get(backend)))
            self.signals.log_msg.emit(f"Test check-in history reset on active backend(s).")
        except Exception as e: self.signals.log_msg.emit(f"ERROR resetting history: {e}")
        finally:
            self.signals.btn_state.emit("btn_reset_synth", "✅ CLEARED", True)
            QTimer.singleShot(2000, lambda: self.signals.btn_state.emit("btn_reset_synth", "🧹 Reset History", True))

    def on_purge_synthetic(self):
        if self.is_running: return self.signals.log_msg.emit("Stop test before modifying database.")
        res = QMessageBox.warning(self, "Purge Synthetic Data", f"PERMANENTLY DELETE every '{SYNTHETIC_NAME_PREFIX} *' attendee? Cannot be undone.", QMessageBox.StandardButton.Yes | QMessageBox.StandardButton.No)
        if res != QMessageBox.StandardButton.Yes: return
        
        self.signals.log_msg.emit("Purging synthetic test attendees...")
        self.signals.btn_state.emit("btn_purge_synth", "⏳ PURGING...", False)
        threading.Thread(target=self._purge_synth_worker, daemon=True).start()

    def _purge_synth_worker(self):
        try:
            sessions = self._get_sessions()
            total_a = total_k = 0
            for backend in ("mysql", "sqlite"):
                factory = sessions.get(backend)
                if factory:
                    n1, n2 = purge_synthetic_attendees(factory)
                    total_a += n1; total_k += n2
            self.signals.log_msg.emit(f"Purged {total_a} main attendees + {total_k} kiosk records.")
            self.reload_attendee_pool()
        except Exception as e: self.signals.log_msg.emit(f"ERROR purging data: {e}")
        finally:
            self.signals.btn_state.emit("btn_purge_synth", "✅ PURGED", True)
            QTimer.singleShot(2000, lambda: self.signals.btn_state.emit("btn_purge_synth", "🗑️ Purge Data", True))

    def on_reset_all_history(self):
        if self.is_running: return self.signals.log_msg.emit("Stop test before modifying database.")
        res = QMessageBox.warning(self, "Wipe ALL Check-ins", "DANGER: Erase check-in history for EVERY attendee globally?", QMessageBox.StandardButton.Yes | QMessageBox.StandardButton.No)
        if res != QMessageBox.StandardButton.Yes: return
        
        self.signals.log_msg.emit("Wiping ALL check-in history globally...")
        self.signals.btn_state.emit("btn_reset_all", "⏳ WIPING...", False)
        threading.Thread(target=self._reset_all_worker, daemon=True).start()

    def _reset_all_worker(self):
        try:
            sessions = self._get_sessions()
            for backend in ("mysql", "sqlite"):
                factory = sessions.get(backend)
                if factory: reset_all_checkin_history(factory)
            self.signals.log_msg.emit(f"GLOBAL check-in history wiped.")
            self.reload_attendee_pool()
        except Exception as e: self.signals.log_msg.emit(f"ERROR wiping history: {e}")
        finally:
            self.signals.btn_state.emit("btn_reset_all", "✅ WIPED", True)
            QTimer.singleShot(2000, lambda: self.signals.btn_state.emit("btn_reset_all", "☢️ Wipe ALL Check-ins", True))

    def on_clear_kiosk(self):
        if self.is_running: return self.signals.log_msg.emit("Stop test before modifying database.")
        res = QMessageBox.warning(self, "Clear Kiosk", "DANGER: Delete EVERY registration record from the Offline Kiosk table?", QMessageBox.StandardButton.Yes | QMessageBox.StandardButton.No)
        if res != QMessageBox.StandardButton.Yes: return
        
        self.signals.log_msg.emit("Clearing offline kiosk registrations...")
        self.signals.btn_state.emit("btn_clear_kiosk", "⏳ CLEARING...", False)
        threading.Thread(target=self._clear_kiosk_worker, daemon=True).start()

    def _clear_kiosk_worker(self):
        try:
            sessions = self._get_sessions()
            total_k = sum(clear_all_kiosk_registrations(sessions.get(backend)) for backend in ("mysql", "sqlite") if sessions.get(backend))
            self.signals.log_msg.emit(f"Cleared {total_k} total kiosk registrations.")
            self.reload_attendee_pool()
        except Exception as e: self.signals.log_msg.emit(f"ERROR clearing kiosk: {e}")
        finally:
            self.signals.btn_state.emit("btn_clear_kiosk", "✅ CLEARED", True)
            QTimer.singleShot(2000, lambda: self.signals.btn_state.emit("btn_clear_kiosk", "☢️ Clear ALL Kiosk Reg", True))


    # ==========================================================================
    # UI CONSTRUCTION (Anti-Crop & Anti-Overlap)
    # ==========================================================================
    def setup_ui(self):
        central = QWidget()
        self.setCentralWidget(central)
        
        main_layout = QHBoxLayout(central)
        main_layout.setContentsMargins(10, 10, 10, 10)
        
        main_splitter = QSplitter(Qt.Horizontal)
        main_splitter.setChildrenCollapsible(False)
        main_layout.addWidget(main_splitter)

        # --- LEFT PANEL (Controls) ---
        left_panel = QWidget()
        left_layout = QVBoxLayout(left_panel)
        left_layout.setContentsMargins(0, 0, 0, 0)
        
        scroll_area = QScrollArea()
        scroll_area.setWidgetResizable(True)
        # Removed hardcoded min-width to allow perfect QSplitter resizing
        
        control_container = QWidget()
        self.control_layout = QVBoxLayout(control_container)
        self.control_layout.setSpacing(12)
        
        self._build_control_panel()
        self.control_layout.addStretch()
        
        scroll_area.setWidget(control_container)
        left_layout.addWidget(scroll_area)
        main_splitter.addWidget(left_panel)

        # --- RIGHT PANEL (Tabs) ---
        right_panel = QWidget()
        self.right_layout = QVBoxLayout(right_panel)
        self.right_layout.setContentsMargins(0, 0, 0, 0)
        
        self.tabs = QTabWidget()
        self.right_layout.addWidget(self.tabs)
        main_splitter.addWidget(right_panel)
        
        main_splitter.setSizes([380, 920])

        self._build_dashboard_tab()
        self._build_analytics_tab()
        self._build_grid_tab()
        self._build_log_tab()

    def _build_control_panel(self):
        # 1. Routing Targets (QFormLayout avoids cropping)
        grp_targets = QGroupBox("1. Routing Targets")
        lay_targets = QFormLayout(grp_targets)
        
        self.http_var = QLineEdit("http://127.0.0.1:5000")
        self.https_var = QLineEdit("https://127.0.0.1:5001")
        self.cloudflare_var = QLineEdit("")
        
        lay_targets.addRow("Waitress (HTTP):", self.http_var)
        lay_targets.addRow("Cheroot (HTTPS):", self.https_var)
        lay_targets.addRow("Cloudflare Tunnel:", self.cloudflare_var)
        self.control_layout.addWidget(grp_targets)

        # 2. Load Profile (QFormLayout)
        grp_profile = QGroupBox("2. Load Profile")
        lay_profile = QFormLayout(grp_profile)
        
        self.action_var = QComboBox()
        self.action_var.addItems(["Strict Registrations (Kiosk Simulation)", "Strict Check-ins (Scanner Simulation)", "Mixed Load (50% Check-in / 50% Reg)"])
        lay_profile.addRow("Action Distribution:", self.action_var)

        self.sync_var = QComboBox()
        self.sync_var.addItems(["Synchronized Millisecond Stampede", "Human Pacing (1-3s delays)", "Gradual Ramp-Up (Stress Growth)"])
        lay_profile.addRow("Attack Strategy:", self.sync_var)

        self.event_days_var = QLineEdit(DEFAULT_EVENT_DAYS)
        lay_profile.addRow("Event Days:", self.event_days_var)

        self.event_year_var = QLineEdit(str(DEFAULT_EVENT_YEAR))
        lay_profile.addRow("Event Year:", self.event_year_var)
        self.control_layout.addWidget(grp_profile)

        # 3. Execution Parameters (QFormLayout)
        grp_exec = QGroupBox("3. Execution Parameters")
        lay_exec = QFormLayout(grp_exec)
        
        self.threads_var = QSpinBox(); self.threads_var.setRange(1, 1000); self.threads_var.setValue(18)
        self.duration_var = QSpinBox(); self.duration_var.setRange(1, 3600); self.duration_var.setValue(60)
        self.rampup_var = QSpinBox(); self.rampup_var.setRange(0, 3600); self.rampup_var.setValue(5)
        self.timeout_var = QDoubleSpinBox(); self.timeout_var.setRange(0.1, 60.0); self.timeout_var.setValue(8.0)
        
        lay_exec.addRow("Devices PER Target:", self.threads_var)
        lay_exec.addRow("Test Duration (sec):", self.duration_var)
        lay_exec.addRow("Ramp-Up Window (sec):", self.rampup_var)
        lay_exec.addRow("Request Timeout (sec):", self.timeout_var)
        self.control_layout.addWidget(grp_exec)

        # 4. Database Tools
        grp_db = QGroupBox("4. Database Tools & Cleanup")
        lay_db = QVBoxLayout(grp_db)

        self.lbl_pool_status = QLabel("Pool: loading...")
        lay_db.addWidget(self.lbl_pool_status)

        h_cap = QHBoxLayout()
        lbl_cap = QLabel("Max rows to load:"); lbl_cap.setStyleSheet("font-weight: bold;")
        self.pool_cap_var = QSpinBox(); self.pool_cap_var.setRange(1, 1000000); self.pool_cap_var.setValue(DEFAULT_POOL_CAP)
        h_cap.addWidget(lbl_cap); h_cap.addWidget(self.pool_cap_var)
        lay_db.addLayout(h_cap)

        self.btn_reload = QPushButton("🔄 Reload Attendee Pool"); self.btn_reload.setObjectName("btn_reload")
        self.btn_reload.setStyleSheet("background-color: #005A9E;")
        self.btn_reload.clicked.connect(lambda: self.reload_attendee_pool(initial=False))
        lay_db.addWidget(self.btn_reload)

        lbl_synth = QLabel("Synthetic Test Data:"); lbl_synth.setStyleSheet("font-weight: bold; margin-top: 10px;")
        lay_db.addWidget(lbl_synth)
        h_synth = QHBoxLayout()
        self.btn_reset_synth = QPushButton("🧹 Reset History"); self.btn_reset_synth.setObjectName("btn_reset_synth")
        self.btn_reset_synth.clicked.connect(self.on_reset_synthetic_history)
        self.btn_purge_synth = QPushButton("🗑️ Purge Data"); self.btn_purge_synth.setObjectName("btn_purge_synth")
        self.btn_purge_synth.setStyleSheet("color: #F44747; border-color: #F44747;")
        self.btn_purge_synth.clicked.connect(self.on_purge_synthetic)
        h_synth.addWidget(self.btn_reset_synth); h_synth.addWidget(self.btn_purge_synth)
        lay_db.addLayout(h_synth)

        lbl_global = QLabel("Global Live Data (DANGER):"); lbl_global.setStyleSheet("font-weight: bold; color: #F44747; margin-top: 10px;")
        lay_db.addWidget(lbl_global)
        h_global = QHBoxLayout()
        self.btn_reset_all = QPushButton("☢️ Wipe ALL Check-ins"); self.btn_reset_all.setObjectName("btn_reset_all")
        self.btn_reset_all.setStyleSheet("background-color: #8C2323; color: white;")
        self.btn_reset_all.clicked.connect(self.on_reset_all_history)
        self.btn_clear_kiosk = QPushButton("☢️ Clear ALL Kiosk Reg"); self.btn_clear_kiosk.setObjectName("btn_clear_kiosk")
        self.btn_clear_kiosk.setStyleSheet("background-color: #8C2323; color: white;")
        self.btn_clear_kiosk.clicked.connect(self.on_clear_kiosk)
        h_global.addWidget(self.btn_reset_all); h_global.addWidget(self.btn_clear_kiosk)
        lay_db.addLayout(h_global)

        self.control_layout.addWidget(grp_db)

        # Execution Buttons
        h_exec = QHBoxLayout()
        self.btn_start = QPushButton("▶ INJECT LOAD"); self.btn_start.setObjectName("btn_start")
        self.btn_start.clicked.connect(self.start_test); self.btn_start.setEnabled(False)
        self.btn_stop = QPushButton("■ HALT"); self.btn_stop.setObjectName("btn_stop")
        self.btn_stop.clicked.connect(self.stop_test); self.btn_stop.setEnabled(False)
        h_exec.addWidget(self.btn_start); h_exec.addWidget(self.btn_stop)
        self.control_layout.addLayout(h_exec)

        self.btn_rep = QPushButton("📄 Export Summary Report"); self.btn_rep.setObjectName("btn_rep")
        self.btn_rep.clicked.connect(self.export_summary_report)
        self.control_layout.addWidget(self.btn_rep)


    def _build_dashboard_tab(self):
        tab = QWidget()
        layout = QVBoxLayout(tab)
        
        self.lbl_verdict = QLabel("⚪ IDLE — System armed.")
        self.lbl_verdict.setStyleSheet("font-size: 16px; font-weight: bold;")
        layout.addWidget(self.lbl_verdict)
        
        # Gauges Row
        h_gauges = QHBoxLayout()
        self.meter_chk = SpeedometerGauge("Check-ins / sec", "RPS", 50, good_is_high=True)
        self.meter_reg = SpeedometerGauge("Registrations / sec", "RPS", 50, good_is_high=True)
        h_gauges.addWidget(self.meter_chk); h_gauges.addWidget(self.meter_reg)
        layout.addLayout(h_gauges)

        # Metric Cards
        h_cards = QHBoxLayout()
        card_defs = [("Total Reqs", "#569CD6"), ("Success %", "#4EC9B0"), ("Avg RT (ms)", "#569CD6"), ("Live RPS", "#D7BA7D"), ("Peak RPS", "#F44747")]
        for name, color in card_defs:
            f = QFrame(); f.setStyleSheet("QFrame { background:#1E1E22; border:1px solid #28282B; border-radius:4px; }")
            l = QVBoxLayout(f); l.setContentsMargins(8, 8, 8, 8); l.setSpacing(2)
            lbl_title = QLabel(name); lbl_title.setStyleSheet("color: #858585; font-size: 11px; font-weight: bold; border:none;")
            l.addWidget(lbl_title, alignment=Qt.AlignCenter)
            val = QLabel("0"); val.setStyleSheet(f"color:{color}; font-size:24px; font-weight:bold; border:none;")
            l.addWidget(val, alignment=Qt.AlignCenter)
            self.stat_widgets[name] = val
            h_cards.addWidget(f)
        layout.addLayout(h_cards)

        # Matplotlib Graph (Throttled for Performance)
        if MATPLOTLIB_AVAILABLE:
            fig, (self.ax_tps, self.ax_rt) = plt.subplots(2, 1, figsize=(8, 4), facecolor="#161618")
            for ax in (self.ax_tps, self.ax_rt):
                ax.set_facecolor("#161618")
                ax.tick_params(colors="white", labelsize=8)
                for spine in ax.spines.values(): spine.set_color("#3c3c3c")
            self.ax_tps.set_title("Throughput (req/s)", color="white", fontsize=10)
            self.ax_tps.set_ylabel("req/s", color="#cccccc", fontsize=8)
            self.ax_rt.set_title("Response Latency (ms)", color="white", fontsize=10)
            self.ax_rt.set_ylabel("ms", color="#cccccc", fontsize=8)
            self.ax_rt.axhline(500, color="#ffbb33", linestyle=":", linewidth=1, alpha=0.8)
            
            (self.line_tps,) = self.ax_tps.plot([], [], color="#00bc8c", linewidth=1.5)
            (self.line_rt,) = self.ax_rt.plot([], [], color="#3498db", linewidth=1.5)
            fig.tight_layout(pad=1.5)

            self.canvas = FigureCanvas(fig)
            layout.addWidget(self.canvas, stretch=1)
        else:
            lbl_no_graph = QLabel("(Install 'matplotlib' via pip to view Live Traffic Graph)")
            lbl_no_graph.setAlignment(Qt.AlignCenter); lbl_no_graph.setStyleSheet("color: #888888; font-style: italic;")
            layout.addWidget(lbl_no_graph, stretch=1)

        self.tabs.addTab(tab, "📊 Live Dashboard")

    def _build_analytics_tab(self):
        tab = QWidget()
        layout = QVBoxLayout(tab)
        
        lbl_desc = QLabel("Analytics panel for Check-in (reads/updates) and Registration (writes) tracked separately.")
        lbl_desc.setStyleSheet("color: #858585; font-style: italic; margin-bottom: 10px;")
        layout.addWidget(lbl_desc)

        h_cols = QHBoxLayout()
        grp_chk = QGroupBox("Check-in Performance")
        lay_chk = QVBoxLayout(grp_chk)
        self.chk_metrics = self._build_metrics_column(lay_chk)
        h_cols.addWidget(grp_chk)

        grp_reg = QGroupBox("Registration Performance")
        lay_reg = QVBoxLayout(grp_reg)
        self.reg_metrics = self._build_metrics_column(lay_reg)
        h_cols.addWidget(grp_reg)
        
        layout.addLayout(h_cols)

        grp_err = QGroupBox("Error / Rejection Breakdown")
        lay_err = QGridLayout(grp_err)
        err_defs = [
            ("dup_400", "HTTP 400 — Duplicate / Client Rejection:"), ("denied_403", "HTTP 403 — Access Denied (wrong date):"),
            ("notfound_404", "HTTP 404 — Attendee Not Found:"), ("server_5xx", "HTTP 500+ — Server Fatality:"),
            ("conn_refused", "Connection Refused / Unreachable:"), ("timeouts", "Timed Out (no limit):"),
        ]
        for i, (key, label_text) in enumerate(err_defs):
            row, col = divmod(i, 2)
            lbl_t = QLabel(label_text)
            lbl_v = QLabel("0"); lbl_v.setStyleSheet("font-weight: bold; color: #D7BA7D;")
            lay_err.addWidget(lbl_t, row, col*2)
            lay_err.addWidget(lbl_v, row, col*2 + 1)
            self.err_labels[key] = lbl_v
            
        layout.addWidget(grp_err)
        layout.addStretch()

        self.tabs.addTab(tab, "🔬 Deep Analytics")

    def _build_metrics_column(self, layout):
        widgets = {}
        rows = ["Total Processed:", "Apdex Score (<500ms):", "P50 (Median) ms:", "P90 ms:", "P95 ms (Warning):", "P99 ms (Critical):", "Min / Max ms:"]
        for label_text in rows:
            h = QHBoxLayout()
            lbl = QLabel(label_text)
            val = QLabel("N/A"); val.setStyleSheet("font-weight: bold; color: #555555;")
            h.addWidget(lbl); h.addStretch(); h.addWidget(val)
            layout.addLayout(h)
            widgets[label_text] = val
        return widgets

    def _build_grid_tab(self):
        tab = QWidget()
        layout = QVBoxLayout(tab)
        
        self.tree = QTableWidget(0, 6)
        self.tree.setHorizontalHeaderLabels(["Time", "Target", "Action", "Status", "RT (ms)", "Identifier Used"])
        self.tree.horizontalHeader().setSectionResizeMode(QHeaderView.Stretch)
        self.tree.setEditTriggers(QTableWidget.NoEditTriggers)
        layout.addWidget(self.tree)

        self.tabs.addTab(tab, "🗃️ Live Data Grid")

    def _build_log_tab(self):
        tab = QWidget()
        layout = QVBoxLayout(tab)
        
        self.log_txt = QPlainTextEdit()
        self.log_txt.setReadOnly(True)
        self.log_txt.setMaximumBlockCount(1000)
        layout.addWidget(self.log_txt)

        self.tabs.addTab(tab, "📜 Terminal / Logs")


    # ==========================================================================
    # EXECUTION LOGIC
    # ==========================================================================
    def start_test(self):
        if self.is_running: return

        raw_targets = [("Waitress", self.http_var.text().strip().rstrip("/")), 
                       ("Cheroot", self.https_var.text().strip().rstrip("/")), 
                       ("Cloudflare", self.cloudflare_var.text().strip().rstrip("/"))]
        targets = [(name, url) for name, url in raw_targets if url]
        
        if not targets: return self.signals.log_msg.emit("ERROR: no routing targets provided. Fill in at least one target URL.")
        bad = [url for _, url in targets if not (url.startswith("http://") or url.startswith("https://"))]
        if bad: self.signals.log_msg.emit(f"WARNING: target(s) missing http(s):// scheme: {', '.join(bad)}")

        dev_count = self.threads_var.value()
        duration = self.duration_var.value()
        ramp_up_sec = self.rampup_var.value()
        timeout_limit = self.timeout_var.value()

        ramp_up_sec = max(0, ramp_up_sec)
        mode = self.action_var.currentText()
        
        with self.pool_lock: pool_empty = not self.user_pool
        if pool_empty and "Strict Check-in" in mode:
            return self.signals.log_msg.emit("FATAL: attendee pool is empty. Reload pool or switch to Registration/Mixed mode.")

        event_days_raw = [d.strip() for d in self.event_days_var.text().split(",") if d.strip()]
        try: event_year = int(self.event_year_var.text())
        except ValueError: event_year = DEFAULT_EVENT_YEAR
        
        event_dates = parse_event_days(event_days_raw, event_year)
        if not event_dates: event_dates = parse_event_days(DEFAULT_EVENT_DAYS.split(","), DEFAULT_EVENT_YEAR)

        self.signals.reset_ui.emit()
        
        # --- UI STATE UPDATE (START) ---
        self.signals.btn_state.emit("btn_start", "✅ RUNNING...", False)
        self.signals.btn_state.emit("btn_reload", "🔄 Reload Attendee Pool", False)
        self.signals.btn_state.emit("btn_stop", "■ HALT", True)
        
        self.is_running = True

        sync_mode = self.sync_var.currentText()
        total_threads = dev_count * len(targets)
        self.sync_barrier = threading.Barrier(total_threads) if "Stampede" in sync_mode else None

        self.user_queue = queue.Queue()
        with self.pool_lock: snapshot = list(self.user_pool)
        random.shuffle(snapshot)
        for u in snapshot: self.user_queue.put(u)

        self.start_time = time.time()
        self.test_duration = duration
        run_config = RunConfig(mode=mode, sync_mode=sync_mode, timeout=timeout_limit, event_day_labels=tuple(event_days_raw), event_dates=tuple(event_dates))

        self.signals.log_msg.emit(f"Test initialized — {total_threads} virtual devices on {len(targets)} target(s).")

        thread_id_counter = 0
        for env_name, base_url in targets:
            for _ in range(dev_count):
                delay = (thread_id_counter / (total_threads - 1)) * ramp_up_sec if ("Ramp-Up" in sync_mode and total_threads > 1) else 0.0
                threading.Thread(target=self.api_worker, args=(env_name, base_url, thread_id_counter, run_config, delay), daemon=True).start()
                thread_id_counter += 1

    def stop_test(self):
        if not self.is_running: return
        self.is_running = False
        
        # --- UI STATE UPDATE (STOPPING) ---
        self.signals.btn_state.emit("btn_stop", "⏳ STOPPING...", False)
        self.signals.btn_state.emit("btn_start", "✅ RUNNING...", False) 
        
        if self.sync_barrier: self.sync_barrier.abort()
        self.signals.log_msg.emit("Halt requested — waiting for in-flight requests to finish...")

        # Background thread to safely reset the UI after threads have stopped
        threading.Thread(target=self._wait_for_shutdown, daemon=True).start()

    def _wait_for_shutdown(self):
        time.sleep(1.5) # Wait buffer for http requests to timeout or complete
        self.signals.log_msg.emit("All devices stopped. System ready.")
        self.signals.btn_state.emit("btn_start", "▶ INJECT LOAD", True)
        self.signals.btn_state.emit("btn_reload", "🔄 Reload Attendee Pool", True)
        self.signals.btn_state.emit("btn_stop", "■ HALT", False)

    def api_worker(self, env_name, base_url, thread_id, run_config: RunConfig, start_delay):
        if start_delay > 0: time.sleep(start_delay)

        device_name = f"Enterprise-{env_name}-D{thread_id}"
        device_id = f"stresstest_{uuid.uuid4().hex[:8]}"

        session = requests.Session()
        retries = Retry(total=2, backoff_factor=0.2, status_forcelist=[500, 502, 503, 504])
        adapter = HTTPAdapter(pool_connections=500, pool_maxsize=500, max_retries=retries)
        session.mount('http://', adapter)
        session.mount('https://', adapter)

        url_checkin = f"{base_url}/api/checkin"
        url_register = f"{base_url}/api/register"

        try:
            while self.is_running:
                if self.sync_barrier:
                    try: self.sync_barrier.wait()
                    except threading.BrokenBarrierError:
                        if not self.is_running: break
                        time.sleep(0.1); continue

                start_req = time.perf_counter()

                is_checkin = ("Strict Check-in" in run_config.mode) or ("Mixed Load" in run_config.mode and random.random() < 0.50)
                user = None
                
                if is_checkin:
                    try: user = self.user_queue.get_nowait()
                    except queue.Empty:
                        with self.pool_lock: pool_snapshot = self.user_pool
                        if pool_snapshot: user = random.choice(pool_snapshot)
                        elif "Strict Check-in" in run_config.mode:
                            self.signals.log_msg.emit("FATAL: attendee pool exhausted mid-run. Halting."); self.is_running = False; break
                        else: is_checkin = False

                try:
                    if is_checkin:
                        payload = generate_checkin_payload(user, device_name, device_id, run_config.event_dates)
                        resp = session.post(url_checkin, json=payload, timeout=run_config.timeout, verify=False)
                        identifier = payload["attendee_id"]
                    else:
                        payload = generate_registration_payload(device_name, device_id, run_config.event_day_labels)
                        resp = session.post(url_register, json=payload, timeout=run_config.timeout, verify=False)
                        identifier = payload["mobile"]
                        if resp.status_code == 200:
                            try:
                                body = resp.json()
                                aid = body.get("attendee_id")
                                if aid:
                                    new_u = AttendeeRef(aid, payload["mobile"], "synthetic")
                                    with self.pool_lock: self.user_pool.append(new_u)
                                    self.user_queue.put(new_u)
                            except: pass

                    rt = (time.perf_counter() - start_req) * 1000
                    req_type = "checkin" if is_checkin else "register"
                    self.stats_queue.put((resp.status_code, rt, env_name, req_type, identifier))

                except requests.exceptions.Timeout: self.stats_queue.put(("TIMEOUT", 0, env_name, "error", "-"))
                except requests.exceptions.ConnectionError: self.stats_queue.put(("CONN_REFUSED", 0, env_name, "error", "-"))
                except requests.exceptions.RequestException: self.stats_queue.put(("NETWORK_ERR", 0, env_name, "error", "-"))

                if self.is_running and not self.sync_barrier and "Human" in run_config.sync_mode:
                    time.sleep(random.uniform(1.0, 3.0))
                    
                # Time limit check inside thread
                if self.is_running and self.test_duration and (time.time() - self.start_time) >= self.test_duration:
                    self.signals.log_msg.emit("Time limit reached. Halting traffic...")
                    self.stop_test()
                    break
        finally:
            session.close()

    def animation_loop(self):
        try:
            self.meter_chk.tick()
            self.meter_reg.tick()
        except Exception: pass

    # ==========================================================================
    # PYSIDE6 SIGNAL SLOTS (Thread-Safe UI Updates)
    # ==========================================================================
    @Slot(str)
    def _ui_log(self, msg):
        ts = time.strftime('%H:%M:%S')
        self.log_txt.appendPlainText(f"[{ts}] {msg}")
        
    @Slot(str, str, bool)
    def _ui_btn_state(self, btn_name, text, enabled):
        btn = getattr(self, btn_name, None)
        if btn:
            btn.setText(text)
            btn.setEnabled(enabled)
            
    @Slot(int, dict)
    def _ui_pool_loaded(self, total, counts):
        self._refresh_pool_label()

    @Slot(dict, list, list, list, int, int)
    def _ui_update_metrics(self, metrics_snap, chk_snap, reg_snap, tree_snap, live_rps, peak_rps):
        total = metrics_snap.get("total", 0)
        
        all_rts = chk_snap + reg_snap
        latest_rt = all_rts[-1] if all_rts else 0

        # Adjust meter max for display scale
        if live_rps > self.meter_chk.max_val:
            new_max = math.ceil(live_rps * 1.3)
            self.meter_chk.max_val = new_max
            self.meter_reg.max_val = new_max
            
        self.meter_chk.set_target(int(live_rps/2))
        self.meter_reg.set_target(int(live_rps/2) + (live_rps%2))

        ok = metrics_snap.get("ok_200", 0)
        success_rate = (ok / total * 100) if total else 0.0
        avg_rt = (sum(all_rts) / len(all_rts)) if all_rts else 0.0

        self.stat_widgets["Total Reqs"].setText(str(total))
        self.stat_widgets["Success %"].setText(f"{success_rate:.1f}%")
        self.stat_widgets["Success %"].setStyleSheet(f"font-size:24px; font-weight:bold; border:none; color: {'#4EC9B0' if success_rate >= 98 else ('#D7BA7D' if success_rate >= 90 else '#F44747')};")
        self.stat_widgets["Avg RT (ms)"].setText(f"{avg_rt:.0f}")
        self.stat_widgets["Live RPS"].setText(str(live_rps))
        self.stat_widgets["Peak RPS"].setText(str(peak_rps))

        self._update_analytics_column(self.chk_metrics, chk_snap)
        self._update_analytics_column(self.reg_metrics, reg_snap)

        for key, lbl in self.err_labels.items(): lbl.setText(str(metrics_snap.get(key, 0)))

        sorted_all = sorted(all_rts)
        p95_all = calculate_percentile(sorted_all, 95)
        apdex_all = calculate_apdex(all_rts)
        server_errors = metrics_snap.get("server_5xx", 0) + metrics_snap.get("timeouts", 0) + metrics_snap.get("conn_refused", 0)
        
        level, detail = build_verdict(total, ok, server_errors, p95_all, apdex_all)
        self.lbl_verdict.setText(f"{level} — {detail}")
        if "CRITICAL" in level: self.lbl_verdict.setStyleSheet("color: #F44747; font-size: 16px; font-weight: bold;")
        elif "DEGRADED" in level: self.lbl_verdict.setStyleSheet("color: #D7BA7D; font-size: 16px; font-weight: bold;")
        elif "HEALTHY" in level: self.lbl_verdict.setStyleSheet("color: #4EC9B0; font-size: 16px; font-weight: bold;")

        # 5. TreeView Batched Injection (No repaints while inserting)
        if tree_snap:
            self.tree.setUpdatesEnabled(False)
            for item in tree_snap:
                self.tree.insertRow(0)
                code = item[3]
                color = QColor("#4EC9B0") if code == 200 else (QColor("#D7BA7D") if isinstance(code, int) and code < 500 else QColor("#F44747"))
                for col, val in enumerate(item):
                    tw_item = QTableWidgetItem(str(val))
                    tw_item.setForeground(color)
                    self.tree.setItem(0, col, tw_item)
            while self.tree.rowCount() > 50: self.tree.removeRow(50)
            self.tree.setUpdatesEnabled(True)

        # 6. Smooth Plotly Updates
        self.throughput_history.append(live_rps)
        self.plot_rts_history.append(latest_rt)

        if MATPLOTLIB_AVAILABLE and hasattr(self, 'line_tps'):
            # Only draw if the tab is visible to save CPU!
            if self.tabs.currentIndex() == 0:
                self.line_tps.set_data(range(len(self.throughput_history)), list(self.throughput_history))
                self.ax_tps.relim(); self.ax_tps.autoscale_view()
                self.line_rt.set_data(range(len(self.plot_rts_history)), list(self.plot_rts_history))
                self.ax_rt.relim(); self.ax_rt.autoscale_view()
                self.canvas.draw_idle()

    @Slot()
    def reset_stats(self):
        with self.data_lock:
            self.metrics = Counter()
            self.rts_checkin.clear()
            self.rts_register.clear()
            self.results_history.clear()
            self.recent_activity.clear()
            self.tree_buffer.clear()
            self.peak_rps = 0

        self.throughput_history.clear()
        self.plot_rts_history.clear()

        for key, widget in self.stat_widgets.items(): widget.setText("0")
        
        self.meter_chk.set_target(0); self.meter_chk.current_val = 0
        self.meter_reg.set_target(0); self.meter_reg.current_val = 0
        
        self.tree.setRowCount(0)
        
        for widgets in (self.chk_metrics, self.reg_metrics):
            for key, widget in widgets.items(): 
                widget.setText("N/A" if key != "Total Processed:" else "0")
                widget.setStyleSheet("font-weight: bold; color: #555555;")
            
        for lbl in self.err_labels.values(): lbl.setText("0")
        self.lbl_verdict.setText("⚪ IDLE — System Armed.")
        self.lbl_verdict.setStyleSheet("color: #D4D4D4; font-size: 16px; font-weight: bold;")

    def _update_analytics_column(self, widgets, data):
        n = len(data)
        widgets["Total Processed:"].setText(str(n))
        widgets["Total Processed:"].setStyleSheet("font-weight: bold; color: #569CD6;")
        
        if n == 0:
            for key in ("Apdex Score (<500ms):", "P50 (Median) ms:", "P90 ms:", "P95 ms (Warning):", "P99 ms (Critical):", "Min / Max ms:"):
                widgets[key].setText("N/A")
                widgets[key].setStyleSheet("font-weight: bold; color: #555555;")
            return

        sorted_data = sorted(data)
        apdex = calculate_apdex(data)
        apdex_color = "#4EC9B0" if apdex >= 0.85 else ("#D7BA7D" if apdex >= 0.6 else "#F44747")
        widgets["Apdex Score (<500ms):"].setText(f"{apdex:.2f}")
        widgets["Apdex Score (<500ms):"].setStyleSheet(f"font-weight: bold; color: {apdex_color};")
        
        widgets["P50 (Median) ms:"].setText(f"{calculate_percentile(sorted_data, 50):.0f}")
        widgets["P50 (Median) ms:"].setStyleSheet("font-weight: bold; color: #569CD6;")
        
        widgets["P90 ms:"].setText(f"{calculate_percentile(sorted_data, 90):.0f}")
        widgets["P90 ms:"].setStyleSheet("font-weight: bold; color: #569CD6;")
        
        p95 = calculate_percentile(sorted_data, 95)
        widgets["P95 ms (Warning):"].setText(f"{p95:.0f}")
        widgets["P95 ms (Warning):"].setStyleSheet(f"font-weight: bold; color: {'#D7BA7D' if p95 > 1000 else '#569CD6'};")
        
        p99 = calculate_percentile(sorted_data, 99)
        widgets["P99 ms (Critical):"].setText(f"{p99:.0f}")
        widgets["P99 ms (Critical):"].setStyleSheet(f"font-weight: bold; color: {'#F44747' if p99 > 1500 else '#569CD6'};")
        
        widgets["Min / Max ms:"].setText(f"{sorted_data[0]:.0f} / {sorted_data[-1]:.0f}")
        widgets["Min / Max ms:"].setStyleSheet("font-weight: bold; color: #569CD6;")

    def export_summary_report(self):
        with self.data_lock:
            total = self.metrics.get("total", 0)
            if total == 0: return self.signals.log_msg.emit("Nothing to export yet — run a test first.")

        file_path, _ = QFileDialog.getSaveFileName(self, "Save Text Report", "", "Text Report (*.txt)")
        if not file_path: return
        
        self.btn_rep.setText("⏳ SAVING...")
        self.btn_rep.setEnabled(False)
        QApplication.processEvents()
        
        try:
            with open(file_path, "w", encoding="utf-8") as f: f.write(self._build_report_text())
            self.signals.log_msg.emit(f"Summary report saved to {file_path}")
            self.btn_rep.setText("✅ SAVED")
        except OSError as e: 
            self.signals.log_msg.emit(f"ERROR saving report: {e}")
            self.btn_rep.setText("❌ ERROR")
        finally:
            self.btn_rep.setEnabled(True)
            QTimer.singleShot(2500, lambda: self.signals.btn_state.emit("btn_rep", "📄 Export Summary Report", True))

    def _build_report_text(self):
        with self.data_lock:
            total = self.metrics.get("total", 0)
            ok = self.metrics.get("ok_200", 0)
            server_errors = self.metrics.get("server_5xx", 0) + self.metrics.get("timeouts", 0) + self.metrics.get("conn_refused", 0)
            rts_checkin_snap = list(self.rts_checkin)
            rts_register_snap = list(self.rts_register)
            metrics_snap = dict(self.metrics)

        all_rts = sorted(rts_checkin_snap + rts_register_snap)
        apdex_all = calculate_apdex(rts_checkin_snap + rts_register_snap)
        p95_all = calculate_percentile(all_rts, 95)
        level, detail = build_verdict(total, ok, server_errors, p95_all, apdex_all)

        lines = [
            "=" * 72, "ENTERPRISE EVENT LOAD TEST — SUMMARY REPORT", f"Generated: {datetime.now().strftime('%Y-%m-%d %H:%M:%S')}", "=" * 72,
            f"\nVERDICT: {level}\n{detail}\n",
            f"Total requests:          {total}",
            f"Success (HTTP 200):      {ok} ({(ok/total*100 if total else 0):.1f}%)",
            f"Duplicates (400):        {metrics_snap.get('dup_400', 0)}",
            f"Access denied (403):     {metrics_snap.get('denied_403', 0)}",
            f"Not found (404):         {metrics_snap.get('notfound_404', 0)}",
            f"Queue rejected (503):    {metrics_snap.get('queue_503', 0)}",
            f"Server errors (500+):    {metrics_snap.get('server_5xx', 0)}",
            f"Timeouts:                {metrics_snap.get('timeouts', 0)}",
            f"Connection refused:      {metrics_snap.get('conn_refused', 0)}\n"
        ]

        for label, data in (("CHECK-IN", rts_checkin_snap), ("REGISTRATION", rts_register_snap)):
            lines.append(f"--- {label} LATENCY ---")
            if not data: lines.append("  No requests of this type.\n"); continue
            sd = sorted(data)
            lines.extend([
                f"  Count: {len(sd)}", f"  Apdex: {calculate_apdex(data):.2f}",
                f"  P50: {calculate_percentile(sd, 50):.0f}ms   P90: {calculate_percentile(sd, 90):.0f}ms   P95: {calculate_percentile(sd, 95):.0f}ms   P99: {calculate_percentile(sd, 99):.0f}ms",
                f"  Min/Max: {sd[0]:.0f}ms / {sd[-1]:.0f}ms\n"
            ])

        with self.pool_lock: pool_n = len(self.user_pool); counts = dict(self.pool_counts)
        lines.append(f"Attendee pool used: {pool_n} real identities ({', '.join(f'{v} from {k}' for k, v in counts.items()) if counts else 'n/a'})")
        lines.append("=" * 72)
        return "\n".join(lines)

if __name__ == "__main__":
    if os.name == 'nt':
        try: ctypes.windll.shell32.SetCurrentProcessExplicitAppUserModelID(os.environ.get("EVENTHUB_TOOL_ID", "EventHub.Tool.stress"))
        except Exception: pass
        
    app_qt = QApplication(sys.argv)
    if hasattr(Qt, 'HighDpiScaleFactorRoundingPolicy'): app_qt.setHighDpiScaleFactorRoundingPolicy(Qt.HighDpiScaleFactorRoundingPolicy.PassThrough)
    
    window = EnterpriseStressTestApp()
    window.show()
    sys.exit(app_qt.exec())