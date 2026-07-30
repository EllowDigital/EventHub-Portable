import os
import json
import enum
import math
from datetime import datetime, timezone
import pymysql
import tkinter as tk
from tkinter import filedialog, messagebox
import ttkbootstrap as tb
from ttkbootstrap.constants import *
import threading
import time
import requests
import csv
import queue
import urllib3
import random
import matplotlib.pyplot as plt
from matplotlib.backends.backend_tkagg import FigureCanvasTkAgg
from collections import deque

# PyMySQL provides a pure-Python MySQL driver
pymysql.install_as_MySQLdb()

from sqlalchemy import (
    create_engine, Column, String, Text, DateTime, 
    Boolean, Float, Integer, JSON
)
from sqlalchemy.orm import declarative_base, sessionmaker

urllib3.disable_warnings(urllib3.exceptions.InsecureRequestWarning)

# ==============================================================================
# DATABASE MODELS & INIT
# ==============================================================================
BASE_DIR = os.path.dirname(os.path.abspath(__file__))
CONFIG_PATH = os.path.join(BASE_DIR, 'config', 'schema.json')

Base = declarative_base()

class Attendee(Base):
    __tablename__ = 'attendees'
    id = Column(String(36), primary_key=True)
    attendee_id = Column(String(30), unique=True, nullable=False, index=True)
    mobile = Column(String(15), unique=True, nullable=False)
    checkin_history = Column(JSON, nullable=False, default={})

def load_db_config():
    if not os.path.exists(CONFIG_PATH):
        raise FileNotFoundError(f"Configuration file missing at: {CONFIG_PATH}")
    with open(CONFIG_PATH, 'r') as f:
        return json.load(f)

def get_database_sessions():
    config = load_db_config()
    sessions = {"sqlite": None, "mysql": None}
    
    if config.get("mysql", {}).get("enabled", False):
        my_config = config["mysql"]
        db_name = my_config["database"]
        mysql_url = f"mysql+mysqldb://{my_config['user']}:{my_config['password']}@{my_config['host']}:{my_config['port']}/{db_name}"
        engine = create_engine(mysql_url, echo=False)
        sessions["mysql"] = sessionmaker(bind=engine)

    return sessions

# ==============================================================================
# ADVANCED PAYLOAD GENERATORS
# ==============================================================================
def generate_registration_payload(device_name):
    att_types = ["GENERAL", "BUSINESS", "MEDIA", "EXHIBITOR"]
    selected_type = random.choice(att_types)
    biz_name = f"Simulated Corp {random.randint(100, 9999)}" if selected_type != "GENERAL" else None
    valid_days = ["30 August", "31 August", "1 September"]
    selected_days = random.sample(valid_days, k=random.randint(1, 3))
    
    return {
        "full_name": f"Enterprise Tester {random.randint(1000, 99999)}",
        "mobile": f"9{random.randint(100000000, 999999999)}",
        "email": f"tester{random.randint(1000, 99999)}@example.com",
        "gender": random.choice(["MALE", "FEMALE", "OTHER"]),
        "attendee_type": selected_type,
        "business_name": biz_name,
        "business_category": "OTHER",
        "other_category": "Stress Test Injector",
        "address": "123 Enterprise Load Test Ave",
        "city": "Lucknow",
        "state": "Uttar Pradesh",
        "pincode": "226001",
        "attendance_days": selected_days,
        "device_name": device_name
    }

def generate_checkin_payload(user_tuple, device_name):
    search_type = random.choice(["id", "phone"]) 
    identifier = user_tuple[0] if search_type == "id" else user_tuple[1]
    return {
        "attendee_id": identifier,
        "search_type": search_type,
        "device_name": device_name,
        "offline_scan_time": datetime.now(timezone.utc).isoformat()
    }

def calculate_percentile(data, percentile):
    if not data: return 0
    data.sort()
    index = math.ceil((len(data) * percentile) / 100) - 1
    return data[max(0, min(index, len(data) - 1))]

def calculate_apdex(data, t_ms=500):
    if not data: return 0.0
    satisfied = sum(1 for x in data if x <= t_ms)
    tolerating = sum(1 for x in data if t_ms < x <= (t_ms * 4))
    return (satisfied + (tolerating / 2)) / len(data)

# ==============================================================================
# ENTERPRISE EVENT STRESS TESTER v4
# ==============================================================================
class EnterpriseStressTestApp(tb.Window):
    def __init__(self):
        super().__init__(themename="superhero", title="TDE UP 2026 - Enterprise Load Injector", size=(1550, 950))
        
        self.is_running = False
        self.stats_queue = queue.Queue()
        self.results_history = []
        
        self.response_times = deque(maxlen=100) 
        self.throughput_history = deque(maxlen=100)
        self.recent_checkins = deque() 
        self.recent_regs = deque()     
        
        self.meter_max = 50 
        self.start_time = 0
        self.registered_users = [] 
        
        self.metrics = {
            "total": 0, "200_ok": 0, "400_dup": 0, "403_denied": 0, 
            "404_ghost": 0, "503_queue": 0, "500_err": 0, "timeouts": 0
        }
        self.rts_checkin = [] 
        self.rts_register = [] 
        self.sync_barrier = None
        
        self.setup_ui()
        self.update_gui_loop()
        threading.Thread(target=self.load_users_from_db, daemon=True).start()

    def load_users_from_db(self):
        try:
            sessions = get_database_sessions()
            Session = sessions.get("mysql")
            if Session:
                with Session() as db:
                    attendees = db.query(Attendee.attendee_id, Attendee.mobile).limit(10000).all()
                    self.registered_users = [(a.attendee_id, a.mobile) for a in attendees]
                
                if self.registered_users:
                    self.btn_start.config(state="normal")
                else:
                    self.generate_dummy_users()
            else:
                self.generate_dummy_users()
        except Exception as e:
            self.generate_dummy_users()

    def generate_dummy_users(self):
        self.registered_users = [(f"TDE26-G-{random.randint(10000, 99999)}", f"9{random.randint(100000000, 999999999)}") for _ in range(5000)]
        self.btn_start.config(state="normal")

    def setup_ui(self):
        self.style.configure("ModernCard.TFrame", background="#1a2736", borderwidth=0)
        
        paned = tb.Panedwindow(self, orient=HORIZONTAL)
        paned.pack(fill=BOTH, expand=True, padx=10, pady=10)

        # --- LEFT PANEL: CONFIGURATION ---
        control_frame = tb.Frame(paned, padding=15)
        paned.add(control_frame, weight=1)

        tb.Label(control_frame, text="Load Configuration", font=("Helvetica", 16, "bold")).pack(anchor="w", pady=(0, 15))
        
        target_lf = tb.Labelframe(control_frame, text=" 1. Routing Targets ", padding=12)
        target_lf.pack(fill="x", pady=(0, 15))
        
        self.http_var = tk.StringVar(value="http://10.37.243.246:5000")
        self.https_var = tk.StringVar(value="https://10.37.243.246:5001")
        self.cloudflare_var = tk.StringVar(value="")

        for label, var in [("Waitress Engine (HTTP):", self.http_var), ("Cheroot Engine (HTTPS):", self.https_var), ("Cloudflare WAN (Optional):", self.cloudflare_var)]:
            tb.Label(target_lf, text=label).pack(anchor="w")
            tb.Entry(target_lf, textvariable=var).pack(fill="x", pady=(0, 8))

        profile_lf = tb.Labelframe(control_frame, text=" 2. Load Profile ", padding=12)
        profile_lf.pack(fill="x", pady=(0, 15))
        
        tb.Label(profile_lf, text="Action Distribution:").pack(anchor="w")
        self.action_var = tk.StringVar(value="Mixed Load (80% Checkin / 20% Reg)")
        tb.Combobox(profile_lf, textvariable=self.action_var, values=["Strict Check-ins (Scanner Simulation)", "Strict Registrations (Kiosk Simulation)", "Mixed Load (80% Checkin / 20% Reg)"]).pack(fill="x", pady=(0, 10))

        tb.Label(profile_lf, text="Concurrency Attack Strategy:").pack(anchor="w")
        self.sync_var = tk.StringVar(value="Synchronized Millisecond Stampede")
        sync_combo = tb.Combobox(profile_lf, textvariable=self.sync_var, values=["Human Pacing (1-3s delays)", "Gradual Ramp-Up (Stress Growth)", "Synchronized Millisecond Stampede"])
        sync_combo.pack(fill="x", pady=(0, 10))

        param_lf = tb.Labelframe(control_frame, text=" 3. Execution Parameters ", padding=12)
        param_lf.pack(fill="x", pady=(0, 15))

        self.threads_var = tk.IntVar(value=15)
        self.duration_var = tk.IntVar(value=60)
        self.rampup_var = tk.IntVar(value=10)
        self.timeout_var = tk.DoubleVar(value=8.0) 

        inputs = [
            ("Devices PER Target Environment:", self.threads_var),
            ("Test Duration (seconds):", self.duration_var),
            ("Ramp-Up Time (seconds, if applicable):", self.rampup_var),
            ("Request Timeout (seconds):", self.timeout_var)
        ]
        for label_text, var in inputs:
            tb.Label(param_lf, text=label_text).pack(anchor="w")
            tb.Entry(param_lf, textvariable=var).pack(fill="x", pady=(0, 5))

        button_frame = tb.Frame(control_frame)
        button_frame.pack(fill="x", pady=10)
        self.btn_start = tb.Button(button_frame, text="▶ INJECT LOAD", bootstyle=SUCCESS, state="disabled", command=self.start_test)
        self.btn_start.pack(side="left", expand=True, fill="x", padx=(0, 5))
        self.btn_stop = tb.Button(button_frame, text="⏹ HALT", bootstyle=DANGER, state="disabled", command=self.stop_test)
        self.btn_stop.pack(side="left", expand=True, fill="x", padx=(5, 0))

        tb.Button(control_frame, text="⬇ Export Raw Enterprise Analytics (CSV)", bootstyle=INFO, command=self.export_csv).pack(fill="x")

        # --- RIGHT PANEL: TABS ---
        right_panel = tb.Frame(paned)
        paned.add(right_panel, weight=3)
        notebook = tb.Notebook(right_panel)
        notebook.pack(fill=BOTH, expand=True)

        dash_frame = tb.Frame(notebook, padding=15)
        notebook.add(dash_frame, text=" 📊 Live Execution Dashboard ")
        self.build_dashboard_tab(dash_frame)

        analytics_frame = tb.Frame(notebook, padding=20)
        notebook.add(analytics_frame, text=" 🔬 Split Deep Analytics ")
        self.build_analytics_tab(analytics_frame)
        
        grid_tab = tb.Frame(notebook, padding=10)
        notebook.add(grid_tab, text=" 🗃️ Live Data Grid ")
        self.build_data_grid(grid_tab)

    def build_dashboard_tab(self, parent):
        meter_frame = tb.Frame(parent)
        meter_frame.pack(fill="x", pady=(0, 10))
        
        self.meter_chk = tb.Meter(meter_frame, metersize=180, padding=10, amounttotal=self.meter_max, amountused=0, metertype="semi", subtext="Check-ins / Sec", interactive=False, bootstyle=INFO, textfont="-size 20 -weight bold")
        self.meter_chk.pack(side="left", expand=True)
        
        self.meter_reg = tb.Meter(meter_frame, metersize=180, padding=10, amounttotal=self.meter_max, amountused=0, metertype="semi", subtext="Writes / Sec", interactive=False, bootstyle=SUCCESS, textfont="-size 20 -weight bold")
        self.meter_reg.pack(side="left", expand=True)

        # Flat Modern Cards
        stats_frame = tb.Frame(parent)
        stats_frame.pack(fill="x", pady=(10, 20))
        
        self.stat_widgets = {}
        cards = [("Total Reqs", "primary"), ("Avg RT (ms)", "info"), ("HTTP 200", "success"), ("HTTP 503", "danger")]
        for name, color in cards:
            f = tb.Frame(stats_frame, style="ModernCard.TFrame", padding=15)
            f.pack(side="left", fill="x", expand=True, padx=5)
            tb.Label(f, text=name, background="#1a2736", font=("Helvetica", 10, "bold")).pack()
            lbl = tb.Label(f, text="0", background="#1a2736", font=("Helvetica", 22, "bold"), bootstyle=color)
            lbl.pack(pady=(5,0))
            self.stat_widgets[name] = lbl

        # Dual Subplot Chart
        self.fig, (self.ax_tps, self.ax_rt) = plt.subplots(2, 1, figsize=(8, 4.5), dpi=100)
        self.fig.patch.set_facecolor('#2b3e50') 
        self.fig.subplots_adjust(hspace=0.4, top=0.9, bottom=0.1)
        
        for ax in (self.ax_tps, self.ax_rt):
            ax.set_facecolor('#1a2736')
            ax.tick_params(colors='white')
            ax.grid(color='#444444', linestyle='--', alpha=0.5)

        self.line_tps, = self.ax_tps.plot([], [], color='#00e676', linewidth=2)
        self.ax_tps.set_title("System Throughput (Requests / Sec)", color='white', weight='bold', fontsize=10)
        
        self.line_rt, = self.ax_rt.plot([], [], color='#00d8ff', linewidth=2)
        self.ax_rt.set_title("Response Latency (Milliseconds)", color='white', weight='bold', fontsize=10)
        
        self.canvas = FigureCanvasTkAgg(self.fig, master=parent)
        self.canvas.get_tk_widget().pack(fill="both", expand=True)

    def build_analytics_tab(self, parent):
        grid = tb.Frame(parent)
        grid.pack(fill="both", expand=True)
        
        chk_frame = tb.Labelframe(grid, text=" 🎫 Check-In Analytics (Reads/Updates) ", padding=20)
        chk_frame.pack(side="left", fill="both", expand=True, padx=(0, 10))
        
        reg_frame = tb.Labelframe(grid, text=" 📝 Registration Analytics (DB Writes) ", padding=20)
        reg_frame.pack(side="left", fill="both", expand=True)
        
        self.chk_metrics = {}
        self.reg_metrics = {}
        
        metrics_list = ["Total Processed:", "Apdex Score (<500ms):", "P50 (Median) ms:", "P90 ms:", "P95 ms (Warning):", "P99 ms (Critical):", "Min / Max ms:"]
        
        for i, text in enumerate(metrics_list):
            tb.Label(chk_frame, text=text, font=("Helvetica", 11, "bold")).grid(row=i, column=0, sticky="w", pady=8)
            chk_lbl = tb.Label(chk_frame, text="-", font=("Helvetica", 12))
            chk_lbl.grid(row=i, column=1, sticky="w", padx=15)
            self.chk_metrics[text] = chk_lbl
            
            tb.Label(reg_frame, text=text, font=("Helvetica", 11, "bold")).grid(row=i, column=0, sticky="w", pady=8)
            reg_lbl = tb.Label(reg_frame, text="-", font=("Helvetica", 12))
            reg_lbl.grid(row=i, column=1, sticky="w", padx=15)
            self.reg_metrics[text] = reg_lbl

        err_frame = tb.Labelframe(parent, text=" 🚨 Application Error Breakdown ", padding=15)
        err_frame.pack(fill="x", pady=(15, 0))
        
        self.lbl_e400 = tb.Label(err_frame, text="HTTP 400 (Client Logic / Duplicates): 0", font=("Helvetica", 10))
        self.lbl_e400.pack(anchor="w", pady=3)
        self.lbl_e403 = tb.Label(err_frame, text="HTTP 403 (Access Denied / Wrong Date): 0", font=("Helvetica", 10))
        self.lbl_e403.pack(anchor="w", pady=3)
        self.lbl_e404 = tb.Label(err_frame, text="HTTP 404 (Ghost User Not Found): 0", font=("Helvetica", 10))
        self.lbl_e404.pack(anchor="w", pady=3)
        self.lbl_e500 = tb.Label(err_frame, text="HTTP 500+ (Server Fatality): 0", font=("Helvetica", 10, "bold"), bootstyle="danger")
        self.lbl_e500.pack(anchor="w", pady=3)

    def build_data_grid(self, parent):
        cols = ("time", "env", "type", "status", "rt")
        self.tree = tb.Treeview(parent, columns=cols, show="headings", height=15)
        self.tree.heading("time", text="Timestamp")
        self.tree.heading("env", text="Environment")
        self.tree.heading("type", text="Action")
        self.tree.heading("status", text="Status Code")
        self.tree.heading("rt", text="Latency (ms)")
        
        self.tree.column("time", width=120, anchor=CENTER)
        self.tree.column("env", width=150, anchor=CENTER)
        self.tree.column("type", width=120, anchor=CENTER)
        self.tree.column("status", width=100, anchor=CENTER)
        self.tree.column("rt", width=100, anchor=CENTER)
        
        scrollbar = tb.Scrollbar(parent, orient=VERTICAL, command=self.tree.yview)
        self.tree.configure(yscrollcommand=scrollbar.set)
        
        scrollbar.pack(side=RIGHT, fill=Y)
        self.tree.pack(side=LEFT, fill=BOTH, expand=True)
        
        self.tree.tag_configure("success", foreground="#00e676")
        self.tree.tag_configure("error", foreground="#ff4444")
        self.tree.tag_configure("warning", foreground="#ffbb33")

    def reset_stats(self):
        self.metrics = {k: 0 for k in self.metrics}
        self.rts_checkin.clear()
        self.rts_register.clear()
        self.recent_checkins.clear()
        self.recent_regs.clear()
        self.results_history.clear()
        self.response_times.clear()
        self.throughput_history.clear()
        for key in self.stat_widgets: self.stat_widgets[key].config(text="0")
        self.meter_chk.configure(amountused=0)
        self.meter_reg.configure(amountused=0)
        for item in self.tree.get_children(): self.tree.delete(item)

    def start_test(self):
        self.reset_stats()
        self.btn_start.config(state="disabled")
        
        raw_targets = [
            ("Waitress", self.http_var.get().strip()),
            ("Cheroot", self.https_var.get().strip()),
            ("Cloudflare", self.cloudflare_var.get().strip())
        ]
        targets = [(name, url) for name, url in raw_targets if url]
        
        if not targets:
            self.btn_start.config(state="normal")
            return
            
        self.is_running = True
        self.btn_stop.config(state="normal")
        
        mode = self.action_var.get()
        sync_mode = self.sync_var.get()
        dev_count = self.threads_var.get()
        ramp_up_sec = self.rampup_var.get()
        total_threads = dev_count * len(targets)
        
        if "Stampede" in sync_mode:
            self.sync_barrier = threading.Barrier(total_threads)
        else:
            self.sync_barrier = None

        self.start_time = time.time()
        thread_id_counter = 0

        for env_name, base_url in targets:
            for _ in range(dev_count):
                delay = 0
                if "Ramp-Up" in sync_mode and total_threads > 1:
                    delay = (thread_id_counter / (total_threads - 1)) * ramp_up_sec
                
                t = threading.Thread(target=self.api_worker, args=(env_name, base_url, thread_id_counter, mode, delay), daemon=True)
                t.start()
                thread_id_counter += 1
            
        threading.Thread(target=self.duration_monitor, daemon=True).start()

    def duration_monitor(self):
        duration = self.duration_var.get()
        while self.is_running:
            if time.time() - self.start_time >= duration:
                self.stop_test()
                break
            time.sleep(0.5)

    def stop_test(self):
        if self.is_running:
            self.is_running = False
            self.btn_start.config(state="normal")
            self.btn_stop.config(state="disabled")
            if self.sync_barrier: self.sync_barrier.abort()

    def api_worker(self, env_name, base_url, thread_id, mode, start_delay):
        if start_delay > 0:
            time.sleep(start_delay) 
            
        timeout_limit = self.timeout_var.get()
        device_name = f"Enterprise-{env_name}-D{thread_id}"
        session = requests.Session()

        while self.is_running:
            if self.sync_barrier:
                try:
                    self.sync_barrier.wait() 
                except threading.BrokenBarrierError:
                    if not self.is_running: break
                    time.sleep(0.1)

            start_req = time.time()
            is_checkin = ("Checkin" in mode)
            if "Mixed" in mode:
                is_checkin = random.random() < 0.8
                
            try:
                if is_checkin:
                    url = f"{base_url}/api/checkin"
                    payload = generate_checkin_payload(random.choice(self.registered_users), device_name)
                    resp = session.post(url, json=payload, timeout=timeout_limit, verify=False)
                else:
                    url = f"{base_url}/api/register"
                    payload = generate_registration_payload(device_name)
                    resp = session.post(url, json=payload, timeout=timeout_limit, verify=False)

                rt = (time.time() - start_req) * 1000
                req_type = "checkin" if is_checkin else "register"
                self.stats_queue.put((resp.status_code, rt, env_name, req_type, start_req))
                
            except requests.exceptions.Timeout:
                self.stats_queue.put(('TIMEOUT', 0, env_name, "error", start_req))
            except requests.exceptions.RequestException:
                self.stats_queue.put(('NETWORK_ERR', 0, env_name, "error", start_req))
                
            if self.is_running and not self.sync_barrier and "Human" in self.sync_var.get():
                time.sleep(random.uniform(1.0, 3.0))

    def update_gui_loop(self):
        updates = 0
        now = time.time()
        
        while not self.stats_queue.empty() and updates < 200: 
            try:
                code, rt, env_name, req_type, req_timestamp = self.stats_queue.get_nowait()
                self.metrics["total"] += 1
                self.results_history.append((now, env_name, req_type, code, rt))
                
                # Insert into Treeview (Limit to 100 to prevent GUI lag)
                if len(self.tree.get_children()) > 100:
                    self.tree.delete(self.tree.get_children()[-1])
                
                tag = "success" if code == 200 else ("warning" if isinstance(code, int) and code < 500 else "error")
                self.tree.insert("", 0, values=(time.strftime('%H:%M:%S'), env_name, req_type.upper(), code, f"{rt:.0f}"), tags=(tag,))
                
                if rt > 0:
                    self.response_times.append(rt)
                    if req_type == "checkin":
                        self.rts_checkin.append(rt)
                        self.recent_checkins.append(now)
                    elif req_type == "register":
                        self.rts_register.append(rt)
                        self.recent_regs.append(now) 

                if code == 200: self.metrics["200_ok"] += 1
                elif code == 400: self.metrics["400_dup"] += 1
                elif code == 403: self.metrics["403_denied"] += 1
                elif code == 404: self.metrics["404_ghost"] += 1
                elif code == 503: self.metrics["503_queue"] += 1
                elif isinstance(code, int) and code >= 500: self.metrics["500_err"] += 1
                else: self.metrics["timeouts"] += 1
                    
                updates += 1
            except queue.Empty:
                break

        if updates > 0 or self.is_running:
            # Update Speedometers
            while self.recent_checkins and self.recent_checkins[0] < now - 1.0:
                self.recent_checkins.popleft()
            while self.recent_regs and self.recent_regs[0] < now - 1.0:
                self.recent_regs.popleft()
                
            self.meter_chk.configure(amountused=min(len(self.recent_checkins), self.meter_max))
            self.meter_reg.configure(amountused=min(len(self.recent_regs), self.meter_max))

            # Update Dashboard
            elapsed = max(now - self.start_time, 1) 
            current_tps = self.metrics["total"] / elapsed
            self.throughput_history.append(current_tps)
            
            self.stat_widgets["Total Reqs"].config(text=str(self.metrics["total"]))
            self.stat_widgets["HTTP 200"].config(text=str(self.metrics["200_ok"]))
            
            if self.metrics["503_queue"] > 0:
                self.stat_widgets["HTTP 503"].config(text=str(self.metrics["503_queue"]), bootstyle="danger")
            
            all_rts = self.rts_checkin + self.rts_register
            if all_rts: self.stat_widgets["Avg RT (ms)"].config(text=f"{sum(all_rts)/len(all_rts):.0f}")

            # Update Analytics with Dynamic Colors
            def update_col(metric_dict, data):
                metric_dict["Total Processed:"].config(text=str(len(data)))
                
                apdex = calculate_apdex(data)
                ax_color = "success" if apdex >= 0.85 else ("warning" if apdex >= 0.6 else "danger")
                metric_dict["Apdex Score (<500ms):"].config(text=f"{apdex:.2f}", bootstyle=ax_color)
                
                metric_dict["P50 (Median) ms:"].config(text=f"{calculate_percentile(data, 50):.0f}")
                metric_dict["P90 ms:"].config(text=f"{calculate_percentile(data, 90):.0f}")
                
                p95 = calculate_percentile(data, 95)
                metric_dict["P95 ms (Warning):"].config(text=f"{p95:.0f}", bootstyle="warning" if p95 > 1000 else "default")
                
                p99 = calculate_percentile(data, 99)
                metric_dict["P99 ms (Critical):"].config(text=f"{p99:.0f}", bootstyle="danger" if p99 > 1500 else "default")
                
                min_v = f"{min(data):.0f}" if data else "0"
                max_v = f"{max(data):.0f}" if data else "0"
                metric_dict["Min / Max ms:"].config(text=f"{min_v} / {max_v}")

            update_col(self.chk_metrics, self.rts_checkin)
            update_col(self.reg_metrics, self.rts_register)
            
            self.lbl_e400.config(text=f"HTTP 400 (Client Logic / Duplicates): {self.metrics['400_dup']}")
            self.lbl_e403.config(text=f"HTTP 403 (Access Denied / Wrong Date): {self.metrics['403_denied']}")
            self.lbl_e404.config(text=f"HTTP 404 (Ghost User Not Found): {self.metrics['404_ghost']}")
            self.lbl_e500.config(text=f"HTTP 500+ (Server Fatality): {self.metrics['500_err']}")

            # Update Split Charts
            self.line_tps.set_xdata(range(len(self.throughput_history)))
            self.line_tps.set_ydata(list(self.throughput_history))
            self.ax_tps.relim()
            self.ax_tps.autoscale_view()
            
            self.line_rt.set_xdata(range(len(self.response_times)))
            self.line_rt.set_ydata(list(self.response_times))
            self.ax_rt.relim()
            self.ax_rt.autoscale_view()
            
            self.canvas.draw()

        self.after(500, self.update_gui_loop)

    def export_csv(self):
        if not self.results_history:
            return
        file_path = filedialog.asksaveasfilename(defaultextension=".csv", filetypes=[("CSV files", "*.csv")])
        if file_path:
            try:
                with open(file_path, mode='w', newline='', encoding='utf-8') as f:
                    writer = csv.writer(f)
                    writer.writerow(["Timestamp", "Environment", "Request Type", "Status Code", "Response Time (ms)"])
                    for row in self.results_history: writer.writerow(row)
            except Exception: pass

if __name__ == "__main__":
    app = EnterpriseStressTestApp()
    app.protocol("WM_DELETE_WINDOW", lambda: (app.stop_test(), app.quit(), app.destroy()))
    app.mainloop()