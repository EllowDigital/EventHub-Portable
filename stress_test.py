import os
import sys
import csv
import math
import queue
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

import tkinter as tk
from tkinter import filedialog, messagebox
import ttkbootstrap as tb
from ttkbootstrap.constants import *
from ttkbootstrap.tooltip import ToolTip

from sqlalchemy import select, update, delete

# --- SAFE MATPLOTLIB INTEGRATION ---
try:
    import matplotlib
    matplotlib.use("TkAgg")
    import matplotlib.pyplot as plt
    from matplotlib.backends.backend_tkagg import FigureCanvasTkAgg
    MATPLOTLIB_AVAILABLE = True
except ImportError:
    MATPLOTLIB_AVAILABLE = False

# ==============================================================================
# PATHS & SCHEMA IMPORTS
# ==============================================================================
SCRIPT_DIR = os.path.dirname(os.path.abspath(__file__))
APP_DIR = os.path.join(SCRIPT_DIR, "app")

if SCRIPT_DIR not in sys.path:
    sys.path.insert(0, SCRIPT_DIR)
if APP_DIR not in sys.path:
    sys.path.insert(0, APP_DIR)

try:
    from schema import Attendee, OfflineKioskAttendee, get_database_sessions
except ImportError as exc:
    raise SystemExit(
        f"FATAL: could not import schema.py.\nOriginal error: {exc}")

urllib3.disable_warnings(urllib3.exceptions.InsecureRequestWarning)

SYNTHETIC_NAME_PREFIX = "Enterprise Tester"
DEFAULT_POOL_CAP = 20000
DEFAULT_EVENT_DAYS = "30 August,31 August,1 September"
DEFAULT_EVENT_YEAR = 2026
UI_TICK_MS = 400

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
    if not sorted_data:
        return 0.0
    index = math.ceil((len(sorted_data) * percentile) / 100) - 1
    index = max(0, min(index, len(sorted_data) - 1))
    return sorted_data[index]


def calculate_apdex(data, satisfied_ms=500):
    if not data:
        return 0.0
    tolerating_ms = satisfied_ms * 4
    satisfied = sum(1 for x in data if x <= satisfied_ms)
    tolerating = sum(1 for x in data if satisfied_ms < x <= tolerating_ms)
    return (satisfied + (tolerating / 2)) / len(data)


def build_verdict(total, success_200, server_errors, p95, apdex):
    if total == 0:
        return "⚪ IDLE", "No requests sent yet — press Inject Load to begin."
    error_rate = server_errors / total if total else 0
    if error_rate > 0.02 or apdex < 0.5:
        return ("🔴 CRITICAL", f"{error_rate:.1%} of requests are hitting server errors / timeouts and Apdex is {apdex:.2f}. The target is struggling.")
    if server_errors > 0 or p95 > 1000 or apdex < 0.85:
        return ("🟡 DEGRADED", f"{success_200/total:.1%} of requests succeeded, but P95 latency is {p95:.0f}ms and Apdex is {apdex:.2f}.")
    return ("🟢 HEALTHY", f"{success_200/total:.1%} success rate, P95 latency {p95:.0f}ms, Apdex {apdex:.2f}. Target is comfortably handling this load.")


def classify_status(code):
    if code == "TIMEOUT":
        return "timeouts"
    if code == "CONN_REFUSED":
        return "conn_refused"
    if code == "NETWORK_ERR":
        return "network_err"
    if isinstance(code, int):
        if code == 200:
            return "ok_200"
        if code == 400:
            return "dup_400"
        if code == 403:
            return "denied_403"
        if code == 404:
            return "notfound_404"
        if code == 503:
            return "queue_503"
        if code >= 500:
            return "server_5xx"
    return "other"


def parse_event_days(day_strings, year):
    parsed = []
    for raw in day_strings:
        raw = raw.strip()
        if not raw:
            continue
        try:
            parsed.append(datetime.strptime(f"{raw} {year}", "%d %B %Y"))
        except ValueError:
            continue
    return parsed


def random_event_timestamp(event_dates):
    base = random.choice(event_dates)
    dt = base.replace(hour=random.randint(8, 19), minute=random.randint(
        0, 59), second=random.randint(0, 59), tzinfo=timezone.utc)
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
        "mobile": mobile,
        "email": f"tester{n}@example.com",
        "gender": random.choice(["MALE", "FEMALE", "OTHER"]),
        "attendee_type": selected_type,
        "business_name": biz_name,
        "business_category": "OTHER" if selected_type != "GENERAL" else None,
        "other_category": "Stress Test Injector",
        "address": "123 Enterprise Load Test Ave",
        "city": "Lucknow",
        "state": "Uttar Pradesh",
        "pincode": "226001",
        "attendance_days": list(event_day_labels),
        "device_name": device_name,
        "device_id": device_id,
    }


def generate_checkin_payload(user: AttendeeRef, device_name, device_id, event_dates):
    search_type = random.choice(["id", "phone"])
    identifier = user.attendee_id if search_type == "id" else user.mobile
    return {
        "attendee_id": identifier,
        "search_type": search_type,
        "device_name": device_name,
        "device_id": device_id,
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
        kiosk_stmt = select(OfflineKioskAttendee.attendee_id,
                            OfflineKioskAttendee.mobile)
        if exclude_synthetic:
            att_stmt = att_stmt.where(
                Attendee.full_name.notlike(f"{SYNTHETIC_NAME_PREFIX}%"))
            kiosk_stmt = kiosk_stmt.where(
                OfflineKioskAttendee.full_name.notlike(f"{SYNTHETIC_NAME_PREFIX}%"))
        att_stmt = att_stmt.limit(cap_per_table)
        kiosk_stmt = kiosk_stmt.limit(cap_per_table)

        for row in db.execute(att_stmt):
            pool.append(AttendeeRef(row.attendee_id, row.mobile, "attendees"))
        counts["attendees"] = len(pool)

        kiosk_start = len(pool)
        for row in db.execute(kiosk_stmt):
            pool.append(AttendeeRef(row.attendee_id, row.mobile, "kiosk"))
        counts["kiosk"] = len(pool) - kiosk_start

    return pool, counts


def load_full_attendee_pool(session_factories, cap_per_table, log_fn):
    combined = []
    seen_ids = set()
    counts_total = Counter()
    any_backend = False

    for backend_name in ("mysql", "sqlite"):
        factory = session_factories.get(backend_name)
        if not factory:
            continue
        any_backend = True
        try:
            rows, counts = fetch_attendee_pool(
                factory, cap_per_table, exclude_synthetic=True)
        except Exception as e:
            log_fn(
                f"WARNING: could not read attendees from {backend_name.upper()}: {e}")
            continue

        added = 0
        for ref in rows:
            if ref.attendee_id in seen_ids:
                continue
            seen_ids.add(ref.attendee_id)
            combined.append(ref)
            added += 1
        counts_total[backend_name] += added
        log_fn(
            f"{backend_name.upper()}: {added} real attendees loaded ({counts['attendees']} base, {counts['kiosk']} kiosk).")

    if not any_backend:
        log_fn("WARNING: no database backend is enabled in config/schema.json (sqlite and mysql are both off).")

    return combined, counts_total

# --- Synthetic Data Tools ---


def reset_synthetic_checkin_history(session_factory):
    with session_factory() as db:
        db.execute(update(Attendee).where(Attendee.full_name.like(
            f"{SYNTHETIC_NAME_PREFIX}%")).values(checkin_history={}))
        db.execute(update(OfflineKioskAttendee).where(OfflineKioskAttendee.full_name.like(
            f"{SYNTHETIC_NAME_PREFIX}%")).values(checkin_history={}))
        db.commit()


def purge_synthetic_attendees(session_factory):
    with session_factory() as db:
        r1 = db.execute(delete(Attendee).where(
            Attendee.full_name.like(f"{SYNTHETIC_NAME_PREFIX}%")))
        r2 = db.execute(delete(OfflineKioskAttendee).where(
            OfflineKioskAttendee.full_name.like(f"{SYNTHETIC_NAME_PREFIX}%")))
        db.commit()
        return r1.rowcount, r2.rowcount

# --- Global / Real Data Tools ---


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
# MAIN GUI APPLICATION
# ==============================================================================
class EnterpriseStressTestApp(tb.Window):
    def __init__(self):
        super().__init__(themename="darkly", title="TDE UP 2026 - Enterprise Load Injector")

        sw, sh = self.winfo_screenwidth(), self.winfo_screenheight()
        ww, wh = max(1200, min(1600, int(sw * 0.90))
                     ), max(800, min(950, int(sh * 0.90)))
        self.geometry(
            f"{ww}x{wh}+{max(0, (sw - ww) // 2)}+{max(0, (sh - wh) // 2 - 20)}")
        self.minsize(1200, 800)

        # Threading/Queues
        self.stats_queue = queue.Queue()
        self.ui_queue = queue.Queue()
        self.user_queue = queue.Queue()

        # Safe Lock Data Containers
        self.data_lock = threading.Lock()
        self.metrics = Counter()
        self.rts_checkin = deque(maxlen=10000)
        self.rts_register = deque(maxlen=10000)
        self.results_history = deque(maxlen=100000)
        self.tree_buffer = []
        self.recent_checkins = deque()
        self.recent_regs = deque()

        # Live Graph Deques
        self.throughput_history = deque(maxlen=300)
        self.plot_rts_history = deque(maxlen=300)
        self.meter_max = 50

        # Run State
        self.is_running = False
        self.start_time = 0.0
        self.test_duration = 0
        self.sync_barrier = None
        self._last_rendered_total = -1

        self.pool_lock = threading.Lock()
        self.user_pool = []
        self.pool_counts = Counter()
        self._session_factories = None

        self.stat_widgets = {}
        self.chk_metrics = {}
        self.reg_metrics = {}
        self.err_labels = {}

        self._configure_custom_styles()
        self.setup_ui()

        # Start Background Background Aggregator (Lag Free Fix)
        threading.Thread(target=self._data_aggregator_loop,
                         daemon=True).start()

        self.update_gui_loop()
        self.reload_attendee_pool(initial=True)

        self.protocol("WM_DELETE_WINDOW", self._on_close)

    def _hex_to_rgb(self, hex_color):
        return tuple(int(hex_color.lstrip('#')[i:i+2], 16) for i in (0, 2, 4))

    def _rgb_to_hex(self, rgb):
        return "#{:02x}{:02x}{:02x}".format(*(max(0, min(255, int(round(c)))) for c in rgb))

    def _mix_hex(self, c_a, c_b, w):
        return self._rgb_to_hex(a + (b - a) * w for a, b in zip(self._hex_to_rgb(c_a), self._hex_to_rgb(c_b)))

    def _configure_custom_styles(self):
        colors = self.style.colors
        self.CARD_BG = colors.get("dark")
        self.SOFT_BORDER = self._mix_hex(self.CARD_BG, colors.get("fg"), 0.15)

        self.style.configure("Card.TFrame", background=self.CARD_BG,
                             bordercolor=self.SOFT_BORDER, borderwidth=1, relief="solid")
        self.style.configure("TLabelframe", background=colors.get(
            "bg"), bordercolor=self.SOFT_BORDER, borderwidth=1)
        self.style.configure("TLabelframe.Label", background=colors.get(
            "bg"), font=("Helvetica", 10, "bold"), foreground=colors.get("info"))
        self.style.configure("Treeview.Heading", font="-size 9 -weight bold")

    def log(self, msg):
        self.ui_queue.put(("log", msg))

    # ==========================================================================
    # BACKGROUND AGGREGATOR (PREVENTS UI FREEZE)
    # ==========================================================================
    def _data_aggregator_loop(self):
        """Dedicated background thread that consumes the firehose of results.
        It does zero GUI rendering, making it lightning fast and unblockable."""
        while True:
            try:
                code, rt, env_name, req_type, identifier = self.stats_queue.get()
            except Exception:
                continue

            bucket = classify_status(code)
            now = time.time()
            clock = time.strftime("%H:%M:%S")

            with self.data_lock:
                self.metrics["total"] += 1
                self.metrics[bucket] += 1

                if rt > 0:
                    if req_type == "checkin":
                        self.rts_checkin.append(rt)
                        self.recent_checkins.append(now)
                    elif req_type == "register":
                        self.rts_register.append(rt)
                        self.recent_regs.append(now)

                record = (clock, env_name, req_type,
                          code, f"{rt:.0f}", identifier)
                self.results_history.append(record)

                # Append to buffer for GUI batch update
                self.tree_buffer.append(record)
                if len(self.tree_buffer) > 50:
                    self.tree_buffer.pop(0)

            self.stats_queue.task_done()

    def _get_sessions(self):
        if self._session_factories is None:
            self._session_factories = get_database_sessions()
        return self._session_factories

    def reload_attendee_pool(self, initial=False):
        if self.is_running:
            self.log("Stop the running test before reloading the attendee pool.")
            return
        self.ui_queue.put(("disable_widget", "btn_reload"))
        if not initial:
            self.ui_queue.put(("disable_widget", "btn_start"))
        cap = self._safe_int(self.pool_cap_var, DEFAULT_POOL_CAP, minimum=1)
        self.log(
            "Connecting to database to load the real attendee pool for check-in testing...")
        threading.Thread(target=self._load_pool_worker,
                         args=(cap,), daemon=True).start()

    def _load_pool_worker(self, cap):
        try:
            sessions = self._get_sessions()
            pool, counts = load_full_attendee_pool(
                sessions, cap, log_fn=self.log)
            with self.pool_lock:
                self.user_pool = pool
                self.pool_counts = counts
            if pool:
                self.log(
                    f"SUCCESS: attendee pool ready — {len(pool)} real attendees loaded and available for check-in testing.")
            else:
                self.log(
                    "WARNING: no attendees found. Register real attendees through your app first, "
                    "or run a Registration / Mixed Load test here to create synthetic attendees to check in."
                )
        except Exception as e:
            self.log(f"ERROR loading attendee pool: {e}")
        finally:
            self.ui_queue.put(("enable_widget", "btn_start"))
            self.ui_queue.put(("enable_widget", "btn_reload"))
            self.ui_queue.put(("pool_loaded", None))

    def _refresh_pool_label(self):
        with self.pool_lock:
            n = len(self.user_pool)
            counts = dict(self.pool_counts)
        if n == 0:
            self.lbl_pool_status.config(
                text="Pool: 0 attendees loaded", bootstyle="danger")
        else:
            detail = ", ".join(f"{v} from {k}" for k, v in counts.items())
            self.lbl_pool_status.config(
                text=f"Pool: {n} real attendees ready ({detail})", bootstyle="success")
        if "User Pool" in self.stat_widgets:
            self.stat_widgets["User Pool"].config(text=str(n))

    # --- DB TOOL TRIGGERS ---
    def on_reset_synthetic_history(self):
        if self.is_running:
            self.log("Stop the running test before modifying the database.")
            return
        if not messagebox.askyesno(
            "Reset synthetic check-in history",
            f"This clears checkin_history ONLY for attendees named "
            f"'{SYNTHETIC_NAME_PREFIX} *' (created by this tool). Real attendees "
            f"are never touched.\n\nContinue?",
        ):
            return
        self.log("Resetting synthetic check-in history...")
        threading.Thread(target=self._reset_synth_worker, daemon=True).start()

    def _reset_synth_worker(self):
        try:
            sessions = self._get_sessions()
            touched = 0
            for backend in ("mysql", "sqlite"):
                factory = sessions.get(backend)
                if factory:
                    reset_synthetic_checkin_history(factory)
                    touched += 1
            self.log(
                f"Synthetic check-in history reset on {touched} backend(s).")
        except Exception as e:
            self.log(f"ERROR resetting synthetic history: {e}")

    def on_purge_synthetic(self):
        if self.is_running:
            self.log("Stop the running test before modifying the database.")
            return
        if not messagebox.askyesno(
            "Purge synthetic test attendees",
            f"This PERMANENTLY DELETES every attendee named '{SYNTHETIC_NAME_PREFIX} *' "
            f"(created by this tool) from both the attendees and kiosk tables. "
            f"Real attendees are never touched. This cannot be undone.\n\nContinue?",
            icon="warning",
        ):
            return
        self.log("Purging synthetic test attendees...")
        threading.Thread(target=self._purge_synth_worker, daemon=True).start()

    def _purge_synth_worker(self):
        try:
            sessions = self._get_sessions()
            total_a = total_k = 0
            for backend in ("mysql", "sqlite"):
                factory = sessions.get(backend)
                if factory:
                    n1, n2 = purge_synthetic_attendees(factory)
                    total_a += n1
                    total_k += n2
            self.log(
                f"Purged {total_a} synthetic attendees + {total_k} synthetic kiosk records.")
            self.ui_queue.put(("reload_pool", None))
        except Exception as e:
            self.log(f"ERROR purging synthetic attendees: {e}")

    def on_reset_all_history(self):
        if self.is_running:
            self.log("Stop the running test before modifying the database.")
            return
        if not messagebox.askyesno(
            "Wipe ALL Check-ins (GLOBAL)",
            "DANGER: This will erase the check-in history for EVERY attendee (Real and Synthetic) across the entire database. Everyone will be eligible to check in again.\n\nAre you absolutely sure you want to wipe ALL check-ins?",
            icon="warning",
        ):
            return
        self.log("Wiping ALL check-in history globally...")
        threading.Thread(target=self._reset_all_worker, daemon=True).start()

    def _reset_all_worker(self):
        try:
            sessions = self._get_sessions()
            touched = 0
            for backend in ("mysql", "sqlite"):
                factory = sessions.get(backend)
                if factory:
                    reset_all_checkin_history(factory)
                    touched += 1
            self.log(f"GLOBAL check-in history wiped on {touched} backend(s).")
            self.ui_queue.put(("reload_pool", None))
        except Exception as e:
            self.log(f"ERROR wiping global history: {e}")

    def on_clear_kiosk(self):
        if self.is_running:
            self.log("Stop the running test before modifying the database.")
            return
        if not messagebox.askyesno(
            "Clear Kiosk Registrations",
            "DANGER: This will PERMANENTLY DELETE every single registration record in the Offline Kiosk table (Real and Synthetic).\n\nAre you absolutely sure you want to clear the kiosk table?",
            icon="warning",
        ):
            return
        self.log("Clearing all offline kiosk registrations...")
        threading.Thread(target=self._clear_kiosk_worker, daemon=True).start()

    def _clear_kiosk_worker(self):
        try:
            sessions = self._get_sessions()
            total_k = 0
            for backend in ("mysql", "sqlite"):
                factory = sessions.get(backend)
                if factory:
                    k = clear_all_kiosk_registrations(factory)
                    total_k += k
            self.log(f"Cleared {total_k} total kiosk registrations globally.")
            self.ui_queue.put(("reload_pool", None))
        except Exception as e:
            self.log(f"ERROR clearing kiosk registrations: {e}")

    @staticmethod
    def _safe_int(var, default, minimum=None):
        try:
            val = int(var.get())
        except (tk.TclError, ValueError, TypeError):
            return default
        if minimum is not None and val < minimum:
            return default
        return val

    # ==========================================================================
    # UI CONSTRUCTION
    # ==========================================================================
    def setup_ui(self):
        root_pane = tb.Panedwindow(self, orient=HORIZONTAL)
        root_pane.pack(fill=BOTH, expand=YES, padx=15, pady=15)

        control_outer = tb.Frame(root_pane, width=380)
        display_frame = tb.Frame(root_pane, padding=(15, 0, 0, 0))
        root_pane.add(control_outer, weight=1)
        root_pane.add(display_frame, weight=3)
        control_outer.pack_propagate(False)

        control_frame = self._make_scrollable(control_outer)
        self._build_control_panel(control_frame)
        self._build_display_panel(display_frame)

    def _make_scrollable(self, parent):
        bg = self.style.colors.bg
        canvas = tk.Canvas(parent, highlightthickness=0, background=bg)
        scrollbar = tb.Scrollbar(
            parent, orient="vertical", command=canvas.yview, bootstyle="round")
        inner = tb.Frame(canvas, padding=5)

        inner.bind("<Configure>", lambda e: canvas.configure(
            scrollregion=canvas.bbox("all")))
        window_id = canvas.create_window((0, 0), window=inner, anchor="nw")
        canvas.bind("<Configure>", lambda e: canvas.itemconfigure(
            window_id, width=e.width))
        canvas.configure(yscrollcommand=scrollbar.set)

        def _wheel(event):
            step = -1 if (getattr(event, "num", None) ==
                          4 or event.delta > 0) else 1
            canvas.yview_scroll(step, "units")

        canvas.bind("<Enter>", lambda e: canvas.bind_all(
            "<MouseWheel>", _wheel))
        canvas.bind("<Leave>", lambda e: canvas.unbind_all("<MouseWheel>"))

        canvas.pack(side="left", fill="both", expand=True)
        scrollbar.pack(side="right", fill="y")
        return inner

    def _build_control_panel(self, parent):
        tb.Label(parent, text="⚡ Load Injector Control", font=(
            "Helvetica", 16, "bold"), bootstyle=PRIMARY).pack(anchor="w", pady=(0, 15))

        targets_lf = tb.Labelframe(
            parent, text=" 1. Routing Targets ", padding=12)
        targets_lf.pack(fill="x", pady=(0, 12))
        self.http_var = tk.StringVar(value="http://127.0.0.1:5000")
        self.https_var = tk.StringVar(value="https://127.0.0.1:5001")
        self.cloudflare_var = tk.StringVar(value="")
        for label, var in [("Waitress Engine (HTTP):", self.http_var), ("Cheroot Engine (HTTPS):", self.https_var), ("Cloudflare Tunnel (optional):", self.cloudflare_var)]:
            tb.Label(targets_lf, text=label, font=(
                "Helvetica", 9, "bold")).pack(anchor="w")
            tb.Entry(targets_lf, textvariable=var).pack(fill="x", pady=(2, 8))

        profile_lf = tb.Labelframe(
            parent, text=" 2. Load Profile ", padding=12)
        profile_lf.pack(fill="x", pady=(0, 12))

        tb.Label(profile_lf, text="Action Distribution:",
                 font=("Helvetica", 9, "bold")).pack(anchor="w")
        self.action_var = tk.StringVar(
            value="Strict Check-ins (Scanner Simulation)")
        action_combo = tb.Combobox(profile_lf, textvariable=self.action_var, state="readonly", values=[
                                   "Strict Check-ins (Scanner Simulation)", "Strict Registrations (Kiosk Simulation)", "Mixed Load (50% Check-in / 50% Reg)"])
        action_combo.pack(fill="x", pady=(2, 8))

        tb.Label(profile_lf, text="Concurrency Attack Strategy:",
                 font=("Helvetica", 9, "bold")).pack(anchor="w")
        self.sync_var = tk.StringVar(value="Human Pacing (1-3s delays)")
        sync_combo = tb.Combobox(profile_lf, textvariable=self.sync_var, state="readonly", values=[
                                 "Human Pacing (1-3s delays)", "Gradual Ramp-Up (Stress Growth)", "Synchronized Millisecond Stampede"])
        sync_combo.pack(fill="x", pady=(2, 8))
        self._tip(sync_combo, "Human Pacing: waits 1-3s between actions.\nGradual Ramp-Up: joins one-by-one.\nSynchronized Stampede: fires exactly simultaneously.")

        day_row = tb.Frame(profile_lf)
        day_row.pack(fill="x", pady=(4, 2))
        tb.Label(day_row, text="Event Days (comma-separated):",
                 font=("Helvetica", 9, "bold")).pack(anchor="w")
        self.event_days_var = tk.StringVar(value=DEFAULT_EVENT_DAYS)
        day_entry = tb.Entry(day_row, textvariable=self.event_days_var)
        day_entry.pack(fill="x", pady=(2, 0))

        year_row = tb.Frame(profile_lf)
        year_row.pack(fill="x", pady=(6, 0))
        tb.Label(year_row, text="Event Year:", font=(
            "Helvetica", 9, "bold")).pack(side="left")
        self.event_year_var = tk.IntVar(value=DEFAULT_EVENT_YEAR)
        tb.Entry(year_row, textvariable=self.event_year_var,
                 width=8).pack(side="right")

        exec_lf = tb.Labelframe(
            parent, text=" 3. Execution Parameters ", padding=12)
        exec_lf.pack(fill="x", pady=(0, 12))
        self.threads_var = tk.IntVar(value=15)
        self.duration_var = tk.IntVar(value=60)
        self.rampup_var = tk.IntVar(value=10)
        self.timeout_var = tk.DoubleVar(value=8.0)
        for label, var in [("Devices PER Target:", self.threads_var), ("Test Duration (sec):", self.duration_var), ("Ramp-Up Window (sec):", self.rampup_var), ("Request Timeout (sec):", self.timeout_var)]:
            row = tb.Frame(exec_lf)
            row.pack(fill="x", pady=4)
            tb.Label(row, text=label, font=(
                "Helvetica", 9, "bold")).pack(side="left")
            tb.Entry(row, textvariable=var, width=8).pack(side="right")

        db_lf = tb.Labelframe(
            parent, text=" 4. Database Tools & Cleanup ", padding=12)
        db_lf.pack(fill="x", pady=(0, 12))

        self.lbl_pool_status = tb.Label(db_lf, text="Pool: loading...", font=(
            "Helvetica", 9), wraplength=260, justify="left")
        self.lbl_pool_status.pack(anchor="w", pady=(0, 8))

        cap_row = tb.Frame(db_lf)
        cap_row.pack(fill="x", pady=(0, 8))
        tb.Label(cap_row, text="Max rows to load:", font=(
            "Helvetica", 9, "bold")).pack(side="left")
        self.pool_cap_var = tk.IntVar(value=DEFAULT_POOL_CAP)
        tb.Entry(cap_row, textvariable=self.pool_cap_var,
                 width=9).pack(side="right")

        self.btn_reload = tb.Button(db_lf, text="🔄 Reload Attendee Pool",
                                    bootstyle=INFO, command=lambda: self.reload_attendee_pool(initial=False))
        self.btn_reload.pack(fill="x", pady=(0, 15))

        # Synthetic Test Data Tools
        tb.Label(db_lf, text="Synthetic Test Data:", font=(
            "Helvetica", 9, "bold")).pack(anchor="w", pady=(0, 4))
        synth_row = tb.Frame(db_lf)
        synth_row.pack(fill="x", pady=(0, 15))
        self.btn_reset_synth = tb.Button(synth_row, text="🧹 Reset Test History",
                                         bootstyle="warning-outline", command=self.on_reset_synthetic_history)
        self.btn_reset_synth.pack(
            side="left", fill="x", expand=True, padx=(0, 4))
        self.btn_purge_synth = tb.Button(
            synth_row, text="🗑️ Purge Test Users", bootstyle="danger-outline", command=self.on_purge_synthetic)
        self.btn_purge_synth.pack(
            side="right", fill="x", expand=True, padx=(4, 0))
        self._tip(self.btn_reset_synth,
                  f"Clears checkin_history ONLY for attendees named '{SYNTHETIC_NAME_PREFIX} *'")
        self._tip(self.btn_purge_synth,
                  f"Permanently DELETES attendees named '{SYNTHETIC_NAME_PREFIX} *'")

        # Global Data Tools (DANGER)
        tb.Label(db_lf, text="Global Live Data (DANGER):", font=(
            "Helvetica", 9, "bold")).pack(anchor="w", pady=(0, 4))
        global_row = tb.Frame(db_lf)
        global_row.pack(fill="x", pady=(0, 6))
        self.btn_reset_all = tb.Button(
            global_row, text="☢️ Wipe ALL Check-ins", bootstyle="danger", command=self.on_reset_all_history)
        self.btn_reset_all.pack(side="left", fill="x",
                                expand=True, padx=(0, 4))
        self.btn_clear_kiosk = tb.Button(
            global_row, text="☢️ Clear ALL Kiosk Reg", bootstyle="danger", command=self.on_clear_kiosk)
        self.btn_clear_kiosk.pack(
            side="right", fill="x", expand=True, padx=(4, 0))
        self._tip(self.btn_reset_all,
                  "Wipes check-in history for EVERY attendee in the database.")
        self._tip(self.btn_clear_kiosk,
                  "Deletes EVERY registration record from the Offline Kiosk table.")

        btn_row = tb.Frame(parent)
        btn_row.pack(fill="x", pady=(10, 12))
        self.btn_start = tb.Button(btn_row, text="▶ INJECT LOAD",
                                   bootstyle=SUCCESS, command=self.start_test, state="disabled")
        self.btn_start.pack(side="left", expand=True, fill="x", padx=(0, 4))
        self.btn_stop = tb.Button(
            btn_row, text="■ HALT", bootstyle=DANGER, command=self.stop_test, state="disabled")
        self.btn_stop.pack(side="right", expand=True, fill="x", padx=(4, 0))

        self.progress_bar = tb.Progressbar(
            parent, bootstyle="success-striped", value=0)
        self.progress_bar.pack(fill="x", pady=(0, 12))

        tb.Button(parent, text="⬇ Export Raw Data (CSV)", bootstyle="secondary-outline",
                  command=self.export_csv).pack(fill="x", pady=(0, 6))
        tb.Button(parent, text="📄 Export Summary Report", bootstyle="secondary-outline",
                  command=self.export_summary_report).pack(fill="x")

    def _tip(self, widget, text):
        ToolTip(widget, text=text, wraplength=340)

    def _build_display_panel(self, parent):
        notebook = tb.Notebook(parent)
        notebook.pack(fill=BOTH, expand=YES)

        dash_tab = tb.Frame(notebook, padding=15)
        analytics_tab = tb.Frame(notebook, padding=15)
        grid_tab = tb.Frame(notebook, padding=15)
        log_tab = tb.Frame(notebook, padding=10)

        notebook.add(dash_tab, text="  📊 Live Dashboard  ")
        notebook.add(analytics_tab, text="  🔬 Deep Analytics  ")
        notebook.add(grid_tab, text="  🗃️ Live Data Grid  ")
        notebook.add(log_tab, text="  📜 Terminal / Logs  ")

        self._build_dashboard_tab(dash_tab)
        self._build_analytics_tab(analytics_tab)
        self._build_grid_tab(grid_tab)
        self._build_log_tab(log_tab)

    def _build_dashboard_tab(self, parent):
        self.lbl_verdict = tb.Label(
            parent, text="⚪ IDLE — no requests sent yet.", font=("Helvetica", 13, "bold"))
        self.lbl_verdict.pack(fill="x", pady=(0, 10))

        meter_row = tb.Frame(parent)
        meter_row.pack(fill="x", pady=(0, 15))
        self.meter_chk = tb.Meter(meter_row, metersize=160, padding=10, amounttotal=self.meter_max, amountused=0, metertype="semi",
                                  subtext="Check-ins / sec", interactive=False, bootstyle="success", textfont=("Helvetica", 18, "bold"))
        self.meter_chk.pack(side="left", expand=True)
        self.meter_reg = tb.Meter(meter_row, metersize=160, padding=10, amounttotal=self.meter_max, amountused=0, metertype="semi",
                                  subtext="Registrations / sec", interactive=False, bootstyle="info", textfont=("Helvetica", 18, "bold"))
        self.meter_reg.pack(side="right", expand=True)

        cards_row = tb.Frame(parent)
        cards_row.pack(fill="x", pady=(0, 15))
        card_defs = [("Total Reqs", "primary"), ("Success %", "success"),
                     ("Avg RT (ms)", "info"), ("HTTP 503", "warning"), ("User Pool", "secondary")]
        for name, style in card_defs:
            card = tb.Frame(cards_row, style="Card.TFrame", padding=12)
            card.pack(side="left", expand=True, fill="both", padx=4)
            tb.Label(card, text=name, font=("Helvetica", 9, "bold"),
                     background=self.CARD_BG, foreground="gray").pack(anchor="w")
            val_lbl = tb.Label(card, text="0", font=(
                "Helvetica", 20, "bold"), background=self.CARD_BG, bootstyle=style)
            val_lbl.pack(anchor="w")
            self.stat_widgets[name] = val_lbl

        if MATPLOTLIB_AVAILABLE:
            fig, (self.ax_tps, self.ax_rt) = plt.subplots(
                2, 1, figsize=(8, 4), facecolor=self.CARD_BG)
            for ax in (self.ax_tps, self.ax_rt):
                ax.set_facecolor(self.CARD_BG)
                ax.tick_params(colors="white", labelsize=8)
                for spine in ax.spines.values():
                    spine.set_color("#3c3c3c")
            self.ax_tps.set_title("Throughput (req/s)",
                                  color="white", fontsize=10)
            self.ax_tps.set_ylabel("req/s", color="#cccccc", fontsize=8)
            self.ax_rt.set_title("Response Latency (ms)",
                                 color="white", fontsize=10)
            self.ax_rt.set_ylabel("ms", color="#cccccc", fontsize=8)
            self.ax_rt.axhline(500, color="#ffbb33",
                               linestyle=":", linewidth=1, alpha=0.8)
            self.ax_rt.text(0.99, 500, " 500ms Apdex target", color="#ffbb33", fontsize=7,
                            ha="right", va="bottom", transform=self.ax_rt.get_yaxis_transform())
            (self.line_tps,) = self.ax_tps.plot(
                [], [], color="#00bc8c", linewidth=1.5)
            (self.line_rt,) = self.ax_rt.plot(
                [], [], color="#3498db", linewidth=1.5)
            fig.tight_layout(pad=1.5)

            self.canvas = FigureCanvasTkAgg(fig, master=parent)
            self.canvas.get_tk_widget().pack(fill=BOTH, expand=YES)
            self.fig = fig
        else:
            tb.Label(parent, text="(Install 'matplotlib' via pip to view Live Traffic Graph)",
                     font="-size 10 -slant italic", foreground="#888").pack(pady=40)

    def _build_analytics_tab(self, parent):
        tb.Label(
            parent,
            text="Analytics panel for Check-in (reads/updates) and Registration (writes) tracked separately.",
            font=("Helvetica", 9), bootstyle="secondary", wraplength=1000, justify="left",
        ).pack(anchor="w", pady=(0, 12))

        cols = tb.Frame(parent)
        cols.pack(fill=BOTH, expand=YES)
        chk_col = tb.Labelframe(
            cols, text=" Check-in Performance ", padding=15)
        chk_col.pack(side="left", fill=BOTH, expand=YES, padx=(0, 8))
        reg_col = tb.Labelframe(
            cols, text=" Registration Performance ", padding=15)
        reg_col.pack(side="right", fill=BOTH, expand=YES, padx=(8, 0))

        self.chk_metrics = self._build_metrics_column(chk_col)
        self.reg_metrics = self._build_metrics_column(reg_col)

        err_lf = tb.Labelframe(
            parent, text=" Error / Rejection Breakdown ", padding=15)
        err_lf.pack(fill="x", pady=(15, 0))
        err_defs = [
            ("dup_400", "HTTP 400 — Duplicate / Client Rejection:"),
            ("denied_403", "HTTP 403 — Access Denied (wrong date):"),
            ("notfound_404", "HTTP 404 — Attendee Not Found:"),
            ("server_5xx", "HTTP 500+ — Server Fatality:"),
            ("conn_refused", "Connection Refused / Unreachable:"),
            ("timeouts", "Timed Out (no response within limit):"),
        ]
        for i, (key, label_text) in enumerate(err_defs):
            row, col = divmod(i, 2)
            cell = tb.Frame(err_lf)
            cell.grid(row=row, column=col, sticky="w", padx=10, pady=4)
            tb.Label(cell, text=label_text, font=(
                "Helvetica", 9)).pack(side="left")
            lbl = tb.Label(cell, text="0", font=("Helvetica", 9, "bold"))
            lbl.pack(side="left", padx=(6, 0))
            self.err_labels[key] = lbl

    def _build_metrics_column(self, parent):
        widgets = {}
        rows = ["Total Processed:", "Apdex Score (<500ms):", "P50 (Median) ms:",
                "P90 ms:", "P95 ms (Warning):", "P99 ms (Critical):", "Min / Max ms:"]
        for label_text in rows:
            row = tb.Frame(parent)
            row.pack(fill="x", pady=4)
            lbl = tb.Label(row, text=label_text, font=("Helvetica", 10))
            lbl.pack(side="left")
            val = tb.Label(row, text="-", font=("Helvetica", 10, "bold"))
            val.pack(side="right")
            widgets[label_text] = val
        return widgets

    def _build_grid_tab(self, parent):
        columns = ("time", "env", "type", "status", "rt", "identifier")
        self.tree = tb.Treeview(parent, columns=columns,
                                show="headings", bootstyle="info")
        headings = {"time": "Time", "env": "Target", "type": "Action",
                    "status": "Status", "rt": "RT (ms)", "identifier": "Identifier Used"}
        widths = {"time": 90, "env": 120, "type": 90,
                  "status": 80, "rt": 80, "identifier": 160}
        for col in columns:
            self.tree.heading(col, text=headings[col])
            self.tree.column(col, width=widths[col], anchor="center")
        self.tree.tag_configure("success", foreground="#4CD37E")
        self.tree.tag_configure("warning", foreground="#FFB454")
        self.tree.tag_configure("error", foreground="#FF6B6B")

        vsb = tb.Scrollbar(parent, orient="vertical",
                           command=self.tree.yview, bootstyle="round")
        self.tree.configure(yscrollcommand=vsb.set)
        self.tree.pack(side="left", fill=BOTH, expand=YES)
        vsb.pack(side="right", fill="y")

    def _build_log_tab(self, parent):
        self.log_txt = tb.Text(parent, wrap="word", state="disabled", font=(
            "Consolas", 10), background="#1e1e1e", foreground="#d4d4d4", borderwidth=0)
        vsb = tb.Scrollbar(parent, orient="vertical",
                           command=self.log_txt.yview, bootstyle="round")
        self.log_txt.configure(yscrollcommand=vsb.set)
        self.log_txt.pack(side="left", fill=BOTH, expand=YES)
        vsb.pack(side="right", fill="y")

    # ==========================================================================
    # EXECUTION LOGIC
    # ==========================================================================
    def start_test(self):
        if self.is_running:
            return

        raw_targets = [("Waitress", self.http_var.get().strip()), ("Cheroot", self.https_var.get(
        ).strip()), ("Cloudflare", self.cloudflare_var.get().strip())]
        targets = [(name, url) for name, url in raw_targets if url]
        if not targets:
            self.log(
                "ERROR: no routing targets provided. Fill in at least one target URL.")
            return
        bad = [url for _, url in targets if not (
            url.startswith("http://") or url.startswith("https://"))]
        if bad:
            self.log(
                f"WARNING: target(s) missing http(s):// scheme: {', '.join(bad)} — requests will likely fail.")

        try:
            dev_count = int(self.threads_var.get())
            duration = int(self.duration_var.get())
            ramp_up_sec = int(self.rampup_var.get())
            timeout_limit = float(self.timeout_var.get())
        except (tk.TclError, ValueError):
            self.log(
                "ERROR: Devices / Duration / Ramp-Up / Timeout must all be valid numbers.")
            return

        if dev_count < 1:
            self.log("ERROR: Devices per target must be at least 1.")
            return
        if duration < 1:
            self.log("ERROR: Test duration must be at least 1 second.")
            return
        if timeout_limit <= 0:
            self.log("ERROR: Request timeout must be greater than 0 seconds.")
            return
        ramp_up_sec = max(0, ramp_up_sec)
        if ramp_up_sec >= duration:
            self.log(
                f"WARNING: ramp-up ({ramp_up_sec}s) is >= duration ({duration}s)")

        mode = self.action_var.get()
        with self.pool_lock:
            pool_empty = not self.user_pool
        if pool_empty and "Strict Check-in" in mode:
            self.log(
                "FATAL: attendee pool is empty. Reload the pool, register real attendees, or switch to Registrations / Mixed mode first.")
            return

        event_days_raw = [d.strip()
                          for d in self.event_days_var.get().split(",") if d.strip()]
        event_year = self._safe_int(self.event_year_var, DEFAULT_EVENT_YEAR)
        event_dates = parse_event_days(event_days_raw, event_year)
        if not event_dates:
            self.log(
                "WARNING: no valid event days configured — falling back to defaults.")
            event_days_raw = DEFAULT_EVENT_DAYS.split(",")
            event_dates = parse_event_days(event_days_raw, DEFAULT_EVENT_YEAR)

        self.reset_stats()
        self.btn_start.config(state="disabled")
        self.btn_reload.config(state="disabled")
        self.is_running = True
        self.btn_stop.config(state="normal")

        sync_mode = self.sync_var.get()
        total_threads = dev_count * len(targets)
        self.sync_barrier = threading.Barrier(
            total_threads) if "Stampede" in sync_mode else None

        self.user_queue = queue.Queue()
        with self.pool_lock:
            snapshot = list(self.user_pool)
        random.shuffle(snapshot)
        for u in snapshot:
            self.user_queue.put(u)

        self.start_time = time.time()
        self.test_duration = duration
        run_config = RunConfig(
            mode=mode, sync_mode=sync_mode, timeout=timeout_limit,
            event_day_labels=tuple(event_days_raw), event_dates=tuple(event_dates),
        )

        self.log(
            f"Test initialized — spawning {total_threads} virtual devices across {len(targets)} target(s). Pool: {len(snapshot)} real attendees.")

        thread_id_counter = 0
        for env_name, base_url in targets:
            for _ in range(dev_count):
                delay = 0.0
                if "Ramp-Up" in sync_mode and total_threads > 1:
                    delay = (thread_id_counter /
                             (total_threads - 1)) * ramp_up_sec
                threading.Thread(
                    target=self.api_worker,
                    args=(env_name, base_url,
                          thread_id_counter, run_config, delay),
                    daemon=True,
                ).start()
                thread_id_counter += 1

    def stop_test(self):
        if not self.is_running:
            return
        self.is_running = False
        self.btn_start.config(state="normal")
        self.btn_reload.config(state="normal")
        self.btn_stop.config(state="disabled")
        if self.sync_barrier:
            self.sync_barrier.abort()
        self.log(
            "Halt requested — in-flight requests will finish, then all devices stop.")

    def _on_close(self):
        self.stop_test()
        self.after(150, self.destroy)

    def reset_stats(self):
        with self.data_lock:
            self.metrics = Counter()
            self.rts_checkin.clear()
            self.rts_register.clear()
            self.results_history.clear()
            self.recent_checkins.clear()
            self.recent_regs.clear()
            self.tree_buffer.clear()
            self._last_rendered_total = -1

        self.throughput_history.clear()
        self.plot_rts_history.clear()

        for key, widget in self.stat_widgets.items():
            if key == "User Pool":
                continue
            widget.config(text="0")
        self.meter_chk.configure(amountused=0)
        self.meter_reg.configure(amountused=0)
        self.progress_bar.configure(value=0)

        for item in self.tree.get_children():
            self.tree.delete(item)
        for widgets in (self.chk_metrics, self.reg_metrics):
            for key, widget in widgets.items():
                widget.config(text="-" if key != "Total Processed:" else "0")
        for lbl in self.err_labels.values():
            lbl.config(text="0")
        self.lbl_verdict.config(text="⚪ IDLE — no requests sent yet.")

    def api_worker(self, env_name, base_url, thread_id, run_config: RunConfig, start_delay):
        if start_delay > 0:
            time.sleep(start_delay)

        device_name = f"Enterprise-{env_name}-D{thread_id}"
        device_id = f"stresstest_{uuid.uuid4().hex[:8]}"

        # Hardened HTTP Session Pool to prevent Socket Exhaustion
        session = requests.Session()
        retries = Retry(total=2, backoff_factor=0.2,
                        status_forcelist=[500, 502, 503, 504])
        adapter = HTTPAdapter(pool_connections=100,
                              pool_maxsize=100, max_retries=retries)
        session.mount('http://', adapter)
        session.mount('https://', adapter)

        try:
            while self.is_running:
                if self.sync_barrier:
                    try:
                        self.sync_barrier.wait()
                    except threading.BrokenBarrierError:
                        if not self.is_running:
                            break
                        time.sleep(0.1)
                        continue

                start_req = time.time()

                if "Mixed Load" in run_config.mode:
                    is_checkin = random.random() < 0.50
                elif "Strict Check-in" in run_config.mode:
                    is_checkin = True
                else:
                    is_checkin = False

                user = None
                if is_checkin:
                    try:
                        user = self.user_queue.get_nowait()
                    except queue.Empty:
                        with self.pool_lock:
                            pool_snapshot = self.user_pool
                        if pool_snapshot:
                            user = random.choice(pool_snapshot)
                        elif "Strict Check-in" in run_config.mode:
                            self.log(
                                "FATAL: attendee pool exhausted mid-run. Halting.")
                            self.is_running = False
                            break
                        else:
                            is_checkin = False

                try:
                    if is_checkin:
                        url = f"{base_url}/api/checkin"
                        payload = generate_checkin_payload(
                            user, device_name, device_id, run_config.event_dates)
                        resp = session.post(
                            url, json=payload, timeout=run_config.timeout, verify=False)
                        identifier = payload["attendee_id"]
                    else:
                        url = f"{base_url}/api/register"
                        payload = generate_registration_payload(
                            device_name, device_id, run_config.event_day_labels)
                        resp = session.post(
                            url, json=payload, timeout=run_config.timeout, verify=False)
                        identifier = payload["mobile"]
                        if resp.status_code == 200:
                            try:
                                body = resp.json()
                                aid = body.get("attendee_id")
                                if aid:
                                    new_u = AttendeeRef(
                                        aid, payload["mobile"], "synthetic")
                                    with self.pool_lock:
                                        self.user_pool.append(new_u)
                                    self.user_queue.put(new_u)
                            except (ValueError, KeyError):
                                pass

                    rt = (time.time() - start_req) * 1000
                    req_type = "checkin" if is_checkin else "register"
                    self.stats_queue.put(
                        (resp.status_code, rt, env_name, req_type, identifier))

                except requests.exceptions.Timeout:
                    self.stats_queue.put(
                        ("TIMEOUT", 0, env_name, "error", "-"))
                except requests.exceptions.ConnectionError:
                    self.stats_queue.put(
                        ("CONN_REFUSED", 0, env_name, "error", "-"))
                except requests.exceptions.RequestException:
                    self.stats_queue.put(
                        ("NETWORK_ERR", 0, env_name, "error", "-"))

                if self.is_running and not self.sync_barrier and "Human" in run_config.sync_mode:
                    time.sleep(random.uniform(1.0, 3.0))
        finally:
            session.close()

    def update_gui_loop(self):
        # 1. Process UI/Log Queue (Batched to prevent freeze)
        log_msgs = []
        for _ in range(30):
            try:
                kind, payload = self.ui_queue.get_nowait()
                if kind == "log":
                    log_msgs.append(payload)
                elif kind == "enable_widget":
                    getattr(self, payload).config(state="normal")
                elif kind == "disable_widget":
                    getattr(self, payload).config(state="disabled")
                elif kind == "pool_loaded":
                    self._refresh_pool_label()
                elif kind == "reload_pool":
                    self.reload_attendee_pool()
            except queue.Empty:
                break

        if log_msgs:
            self.log_txt.configure(state="normal")
            ts = time.strftime('%H:%M:%S')
            for msg in log_msgs:
                self.log_txt.insert("end", f"[{ts}] {msg}\n")
            self.log_txt.see("end")
            self.log_txt.configure(state="disabled")

        # 2. Update Run Time
        now = time.time()
        if self.is_running:
            elapsed = now - self.start_time
            progress = min(100, (elapsed / self.test_duration)
                           * 100) if self.test_duration else 0
            self.progress_bar.configure(value=progress)
            if self.test_duration and elapsed >= self.test_duration:
                self.log("Time limit reached. Halting traffic...")
                self.stop_test()

        # 3. Pull Data Snapshot
        with self.data_lock:
            total = self.metrics.get("total", 0)

            # If no new requests came in, just reschedule GUI tick
            if total == self._last_rendered_total and self.is_running:
                self.after(UI_TICK_MS, self.update_gui_loop)
                return

            self._last_rendered_total = total

            # Snapshot Metrics safely
            metrics_snap = dict(self.metrics)

            # Prune sliding windows for rate calculation
            while self.recent_checkins and now - self.recent_checkins[0] > 1.0:
                self.recent_checkins.popleft()
            while self.recent_regs and now - self.recent_regs[0] > 1.0:
                self.recent_regs.popleft()

            chk_rate = len(self.recent_checkins)
            reg_rate = len(self.recent_regs)

            # Copy response times for math functions
            rts_checkin_snap = list(self.rts_checkin)
            rts_register_snap = list(self.rts_register)
            all_rts = rts_checkin_snap + rts_register_snap

            # Pull batched TreeView items
            tree_items = list(self.tree_buffer)
            self.tree_buffer.clear()

        # 4. Render Updates (OUTSIDE the lock for speed)
        latest_rt = all_rts[-1] if all_rts else 0

        peak = max(chk_rate, reg_rate, 1)
        if peak > self.meter_max:
            self.meter_max = math.ceil(peak * 1.3)
            self.meter_chk.configure(amounttotal=self.meter_max)
            self.meter_reg.configure(amounttotal=self.meter_max)
        self.meter_chk.configure(amountused=chk_rate)
        self.meter_reg.configure(amountused=reg_rate)

        ok = metrics_snap.get("ok_200", 0)
        success_rate = (ok / total * 100) if total else 0.0
        avg_rt = (sum(all_rts) / len(all_rts)) if all_rts else 0.0

        self.stat_widgets["Total Reqs"].config(text=str(total))
        self.stat_widgets["Success %"].config(text=f"{success_rate:.1f}%", bootstyle="success" if success_rate >= 98 else (
            "warning" if success_rate >= 90 else "danger"))
        self.stat_widgets["Avg RT (ms)"].config(text=f"{avg_rt:.0f}")
        self.stat_widgets["HTTP 503"].config(
            text=str(metrics_snap.get("queue_503", 0)))

        self._update_analytics_column(self.chk_metrics, rts_checkin_snap)
        self._update_analytics_column(self.reg_metrics, rts_register_snap)

        for key, lbl in self.err_labels.items():
            lbl.config(text=str(metrics_snap.get(key, 0)))

        sorted_all = sorted(all_rts)
        p95_all = calculate_percentile(sorted_all, 95)
        apdex_all = calculate_apdex(all_rts)
        server_errors = metrics_snap.get(
            "server_5xx", 0) + metrics_snap.get("timeouts", 0) + metrics_snap.get("conn_refused", 0)
        level, detail = build_verdict(
            total, ok, server_errors, p95_all, apdex_all)
        self.lbl_verdict.config(text=f"{level} — {detail}")

        # 5. TreeView Batched Injection
        if tree_items:
            for item in tree_items:
                code = item[3]
                tag = "success" if code == 200 else (
                    "warning" if isinstance(code, int) and code < 500 else "error")
                self.tree.insert("", 0, values=item, tags=(tag,))

            # Prune Grid (O(1) fast deletion)
            children = self.tree.get_children()
            if len(children) > 100:
                self.tree.delete(*children[100:])

        # 6. Smooth Plotly Updates
        self.throughput_history.append(chk_rate + reg_rate)
        self.plot_rts_history.append(latest_rt)

        if MATPLOTLIB_AVAILABLE and hasattr(self, 'line_tps'):
            x_tps = range(len(self.throughput_history))
            self.line_tps.set_data(x_tps, list(self.throughput_history))
            self.ax_tps.relim()
            self.ax_tps.autoscale_view()

            x_rt = range(len(self.plot_rts_history))
            self.line_rt.set_data(x_rt, list(self.plot_rts_history))
            self.ax_rt.relim()
            self.ax_rt.autoscale_view()
            self.canvas.draw_idle()

        self.after(UI_TICK_MS, self.update_gui_loop)

    def _update_analytics_column(self, widgets, data):
        n = len(data)
        widgets["Total Processed:"].config(text=str(n))
        if n == 0:
            for key in ("Apdex Score (<500ms):", "P50 (Median) ms:", "P90 ms:", "P95 ms (Warning):", "P99 ms (Critical):", "Min / Max ms:"):
                widgets[key].config(text="-")
            return

        sorted_data = sorted(data)
        apdex = calculate_apdex(data)
        apdex_style = "success" if apdex >= 0.85 else (
            "warning" if apdex >= 0.6 else "danger")
        widgets["Apdex Score (<500ms):"].config(
            text=f"{apdex:.2f}", bootstyle=apdex_style)
        widgets["P50 (Median) ms:"].config(
            text=f"{calculate_percentile(sorted_data, 50):.0f}")
        widgets["P90 ms:"].config(
            text=f"{calculate_percentile(sorted_data, 90):.0f}")
        p95 = calculate_percentile(sorted_data, 95)
        widgets["P95 ms (Warning):"].config(
            text=f"{p95:.0f}", bootstyle="warning" if p95 > 1000 else "default")
        p99 = calculate_percentile(sorted_data, 99)
        widgets["P99 ms (Critical):"].config(
            text=f"{p99:.0f}", bootstyle="danger" if p99 > 1500 else "default")
        widgets["Min / Max ms:"].config(
            text=f"{sorted_data[0]:.0f} / {sorted_data[-1]:.0f}")

    def export_csv(self):
        with self.data_lock:
            if not self.results_history:
                self.log("Nothing to export yet — run a test first.")
                return
            data_to_export = list(self.results_history)

        file_path = filedialog.asksaveasfilename(
            defaultextension=".csv", filetypes=[("CSV File", "*.csv")])
        if not file_path:
            return
        try:
            with open(file_path, "w", newline="", encoding="utf-8") as f:
                writer = csv.writer(f)
                writer.writerow(["Time", "Target", "Action",
                                "Status", "ResponseTimeMs", "Identifier"])
                writer.writerows(data_to_export)
            self.log(f"Raw data exported to {file_path}")
        except OSError as e:
            self.log(f"ERROR exporting CSV: {e}")

    def export_summary_report(self):
        with self.data_lock:
            total = self.metrics.get("total", 0)
            if total == 0:
                self.log("Nothing to export yet — run a test first.")
                return

        file_path = filedialog.asksaveasfilename(
            defaultextension=".txt", filetypes=[("Text Report", "*.txt")])
        if not file_path:
            return
        try:
            with open(file_path, "w", encoding="utf-8") as f:
                f.write(self._build_report_text())
            self.log(f"Summary report saved to {file_path}")
        except OSError as e:
            self.log(f"ERROR saving report: {e}")

    def _build_report_text(self):
        with self.data_lock:
            total = self.metrics.get("total", 0)
            ok = self.metrics.get("ok_200", 0)
            server_errors = self.metrics.get(
                "server_5xx", 0) + self.metrics.get("timeouts", 0) + self.metrics.get("conn_refused", 0)
            rts_checkin_snap = list(self.rts_checkin)
            rts_register_snap = list(self.rts_register)
            metrics_snap = dict(self.metrics)

        all_rts = sorted(rts_checkin_snap + rts_register_snap)
        apdex_all = calculate_apdex(rts_checkin_snap + rts_register_snap)
        p95_all = calculate_percentile(all_rts, 95)
        level, detail = build_verdict(
            total, ok, server_errors, p95_all, apdex_all)

        lines = []
        lines.append("=" * 72)
        lines.append("ENTERPRISE EVENT LOAD TEST — SUMMARY REPORT")
        lines.append(
            f"Generated: {datetime.now().strftime('%Y-%m-%d %H:%M:%S')}")
        lines.append("=" * 72)

        lines.append(f"\nVERDICT: {level}\n{detail}\n")
        lines.append(f"Total requests:          {total}")
        lines.append(
            f"Success (HTTP 200):      {ok} ({(ok/total*100 if total else 0):.1f}%)")
        lines.append(
            f"Duplicates (400):        {metrics_snap.get('dup_400', 0)}")
        lines.append(
            f"Access denied (403):     {metrics_snap.get('denied_403', 0)}")
        lines.append(
            f"Not found (404):         {metrics_snap.get('notfound_404', 0)}")
        lines.append(
            f"Queue rejected (503):    {metrics_snap.get('queue_503', 0)}")
        lines.append(
            f"Server errors (500+):    {metrics_snap.get('server_5xx', 0)}")
        lines.append(
            f"Timeouts:                {metrics_snap.get('timeouts', 0)}")
        lines.append(
            f"Connection refused:      {metrics_snap.get('conn_refused', 0)}")
        lines.append("")

        for label, data in (("CHECK-IN", rts_checkin_snap), ("REGISTRATION", rts_register_snap)):
            lines.append(f"--- {label} LATENCY ---")
            if not data:
                lines.append("  No requests of this type.\n")
                continue
            sd = sorted(data)
            lines.append(f"  Count: {len(sd)}")
            lines.append(f"  Apdex: {calculate_apdex(data):.2f}")
            lines.append(
                f"  P50: {calculate_percentile(sd, 50):.0f}ms   P90: {calculate_percentile(sd, 90):.0f}ms   P95: {calculate_percentile(sd, 95):.0f}ms   P99: {calculate_percentile(sd, 99):.0f}ms")
            lines.append(f"  Min/Max: {sd[0]:.0f}ms / {sd[-1]:.0f}ms\n")

        with self.pool_lock:
            pool_n = len(self.user_pool)
            counts = dict(self.pool_counts)
        detail_str = ", ".join(f"{v} from {k}" for k,
                               v in counts.items()) if counts else "n/a"
        lines.append(
            f"Attendee pool used: {pool_n} real identities ({detail_str})")
        lines.append("=" * 72)
        return "\n".join(lines)


if __name__ == "__main__":
    app = EnterpriseStressTestApp()
    app.mainloop()
