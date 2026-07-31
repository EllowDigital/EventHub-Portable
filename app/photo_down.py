import os
import requests
import logging
import threading
import queue
import re
from datetime import datetime, timezone
import tkinter as tk
import ttkbootstrap as ttk
from ttkbootstrap.constants import *
from ttkbootstrap.widgets.scrolled import ScrolledText

# Import models and DB initialization from your schema
try:
    from app.schema import Attendee, DownloadedPhoto, get_database_sessions
except ModuleNotFoundError:
    from schema import Attendee, DownloadedPhoto, get_database_sessions

# ==============================================================================
# 24/7 STABILITY: GLOBAL CRASH HANDLER
# ==============================================================================
def global_exception_handler(*args):
    print(f"Uncaught GUI Exception intercepted. App remains running: {args}")

tk.Tk.report_callback_exception = global_exception_handler

# ==============================================================================
# PATHS & CONFIG (PORTABLE BASE_DIR)
# ==============================================================================
BASE_DIR = os.path.dirname(os.path.abspath(__file__))
PHOTOS_DIR = os.path.join(BASE_DIR, 'attendee_photos')
LOG_DIR = os.path.join(BASE_DIR, 'logs')

os.makedirs(PHOTOS_DIR, exist_ok=True)
os.makedirs(LOG_DIR, exist_ok=True)
MAX_LOG_LINES = 2000

# ==============================================================================
# CUSTOM LOGGING HANDLER (THREAD-SAFE)
# ==============================================================================
class TkinterLogHandler(logging.Handler):
    """Streams logs safely into the GUI via Queue to prevent freezing."""
    def __init__(self, gui_queue):
        super().__init__()
        self.gui_queue = gui_queue

    def emit(self, record):
        msg = self.format(record)
        level = "INFO"
        if record.levelno >= logging.ERROR: level = "ERROR"
        elif record.levelno >= logging.WARNING: level = "WARNING"
        
        self.gui_queue.put(("log", {"msg": msg, "level": level}))

logging.basicConfig(
    filename=os.path.join(LOG_DIR, 'photo_downloader.log'),
    level=logging.INFO,
    format='%(asctime)s [%(levelname)s] %(message)s'
)

# ==============================================================================
# CORE DOWNLOAD & SYNC MANAGER
# ==============================================================================
class PhotoDownloadManager:
    def __init__(self):
        self.SessionMySQL = None
        self.SessionSQLite = None
        self.is_running = False
        self.cancel_requested = False
        self.connect_db()

    def connect_db(self):
        try:
            sessions = get_database_sessions()
            self.SessionMySQL = sessions.get('mysql')
            self.SessionSQLite = sessions.get('sqlite')
            logging.info("Connected to Dual Databases (MySQL + SQLite).")
        except Exception as e:
            logging.error(f"Database Connection Failed: {e}")

    def mirror_photos_to_sqlite(self):
        """Lightning-fast bulk mirror to backup downloaded_photos to SQLite."""
        if not self.SessionSQLite or not self.SessionMySQL: return
        logging.info("Mirroring downloaded_photos registry to SQLite...")
        
        mysql_session = self.SessionMySQL()
        sqlite_session = self.SessionSQLite()
        
        try:
            mysql_photos = mysql_session.query(DownloadedPhoto).all()
            data_dicts = [{c.name: getattr(p, c.name) for c in p.__table__.columns} for p in mysql_photos]

            sqlite_session.query(DownloadedPhoto).delete()
            if data_dicts:
                sqlite_session.bulk_insert_mappings(DownloadedPhoto, data_dicts)
                
            sqlite_session.commit()
            logging.info(f"Photo mirror complete: {len(data_dicts)} records backed up to SQLite.")
        except Exception as e:
            sqlite_session.rollback()
            logging.error(f"SQLite Photo Mirror error: {e}")
        finally:
            mysql_session.close()
            sqlite_session.close()

    def get_stats(self):
        """Calculates total pending downloads AND detects unregistered local files to save credits."""
        if not self.SessionMySQL:
            return {"total": 0, "synced": 0, "pending_db_sync": [], "pending_download": []}
            
        session = self.SessionMySQL()
        try:
            all_attendees = session.query(Attendee).all()
            
            # Filter attendees who actually have a photo URL attached
            attendees_with_photos = [
                a for a in all_attendees 
                if a.photo_url and str(a.photo_url).strip().lower() not in ('', 'none', 'null')
            ]
            
            downloaded_records = session.query(DownloadedPhoto).all()
            downloaded_map = {record.attendee_id: record for record in downloaded_records}
            
            fully_synced = 0
            pending_db_sync = []     # Exists on hard drive, but missing in DB (Saves Cloudinary credits)
            pending_download = []    # Missing entirely (Needs download)
            
            for att in attendees_with_photos:
                local_filename = f"{att.attendee_id}.jpg"
                absolute_path = os.path.join(PHOTOS_DIR, local_filename)
                file_exists = os.path.exists(absolute_path)
                
                if file_exists:
                    if att.attendee_id in downloaded_map:
                        fully_synced += 1
                    else:
                        pending_db_sync.append(att)
                else:
                    pending_download.append(att)

            return {
                "total": len(attendees_with_photos),
                "synced": fully_synced,
                "pending_db_sync": pending_db_sync,
                "pending_download": pending_download
            }
        except Exception as e:
            logging.error(f"Failed to fetch stats: {e}")
            return {"total": 0, "synced": 0, "pending_db_sync": [], "pending_download": []}
        finally:
            session.close()

    def process_photos(self, update_progress_callback, finished_callback):
        """Intelligently processes the queue: registers existing local files OR downloads missing ones."""
        self.is_running = True
        self.cancel_requested = False
        session = self.SessionMySQL()
        
        try:
            stats = self.get_stats()
            # Combine both queues: we process local registrations first, then downloads
            to_process = stats["pending_db_sync"] + stats["pending_download"]
            total_pending = len(to_process)
            
            if total_pending == 0:
                logging.info("Everything is up to date! No downloads or syncs needed.")
                finished_callback(True)
                return

            logging.info(f"Processing {total_pending} items ({len(stats['pending_db_sync'])} to local-sync, {len(stats['pending_download'])} to download)...")
            
            success_count = 0
            for index, att in enumerate(to_process):
                if self.cancel_requested:
                    logging.warning("Process aborted by user.")
                    break
                
                url = str(att.photo_url).strip()
                local_filename = f"{att.attendee_id}.jpg"
                absolute_path = os.path.join(PHOTOS_DIR, local_filename)
                
                # STORE PORTABLE RELATIVE PATH (cross-platform compatible)
                relative_path = os.path.relpath(absolute_path, BASE_DIR).replace('\\', '/')
                
                file_exists = os.path.exists(absolute_path)
                
                try:
                    if not file_exists:
                        # --- PHASE 1: HTTP DOWNLOAD (Because file is missing) ---
                        response = requests.get(url, stream=True, timeout=10)
                        response.raise_for_status()
                        
                        with open(absolute_path, 'wb') as file:
                            for chunk in response.iter_content(1024):
                                file.write(chunk)
                        
                        action_msg = "Downloaded & Saved"
                    else:
                        # --- PHASE 2: SMART LOCAL DETECTION (Saves Cloudinary Credits) ---
                        action_msg = "Found Locally (Cloudinary Skipped) -> DB Registered"
                    
                    # --- PHASE 3: DATABASE REGISTRATION ---
                    file_size_kb = round(os.path.getsize(absolute_path) / 1024, 2)
                    existing_record = session.query(DownloadedPhoto).filter_by(attendee_id=att.attendee_id).first()
                    
                    if existing_record:
                        existing_record.local_path = relative_path
                        existing_record.photo_url = url
                        existing_record.file_size_kb = file_size_kb
                        existing_record.downloaded_at = datetime.now(timezone.utc).replace(tzinfo=None)
                    else:
                        new_record = DownloadedPhoto(
                            attendee_id=att.attendee_id,
                            photo_url=url,
                            local_path=relative_path,
                            file_size_kb=file_size_kb,
                            downloaded_at=datetime.now(timezone.utc).replace(tzinfo=None)
                        )
                        session.add(new_record)
                    
                    session.commit()
                    success_count += 1
                    logging.info(f"[{att.attendee_id}] {action_msg} ({file_size_kb} KB)")
                    
                except requests.exceptions.RequestException as req_err:
                    logging.error(f"[{att.attendee_id}] Network Error downloading: {req_err}")
                except Exception as e:
                    logging.error(f"[{att.attendee_id}] Processing Error: {e}")
                
                update_progress_callback(index + 1, total_pending)
                
            logging.info(f"Finished. Successfully processed {success_count}/{total_pending} photos.")
            
            if success_count > 0:
                self.mirror_photos_to_sqlite()
                
            finished_callback(True)
            
        except Exception as e:
            logging.error(f"Critical error in engine: {e}")
            finished_callback(False)
        finally:
            session.close()
            self.is_running = False

# ==============================================================================
# DASHBOARD GUI (MODERN CARD UI)
# ==============================================================================
class PhotoDownloaderGUI(ttk.Window):
    def __init__(self):
        super().__init__(themename="darkly", title="TDE UP 2026 — Offline Photo Engine")
        
        sw, sh = self.winfo_screenwidth(), self.winfo_screenheight()
        ww, wh = max(900, min(1200, int(sw * 0.8))), max(650, min(800, int(sh * 0.8)))
        self.geometry(f"{ww}x{wh}+{(sw - ww) // 2}+{(sh - wh) // 2 - 20}")
        self.minsize(900, 650)
        
        self.gui_queue = queue.Queue()
        self.manager = PhotoDownloadManager()
        
        self._configure_custom_styles()
        self.build_ui()
        
        # Staggered startup
        self.after(50, self._process_gui_queue)
        self.after(200, self.refresh_stats_async)

    def _configure_custom_styles(self):
        colors = self.style.colors
        self.CARD_BG = colors.get("dark")
        self.SOFT_BORDER = self._mix_hex(self.CARD_BG, colors.get("fg"), 0.08)
        self.style.configure("Card.TFrame", background=self.CARD_BG, bordercolor=self.SOFT_BORDER, borderwidth=1, relief="solid")

    def _hex_to_rgb(self, hex_color):
        hex_color = hex_color.lstrip('#')
        return tuple(int(hex_color[i:i+2], 16) for i in (0, 2, 4))
    def _rgb_to_hex(self, rgb):
        return "#{:02x}{:02x}{:02x}".format(*(max(0, min(255, int(round(c)))) for c in rgb))
    def _mix_hex(self, c_a, c_b, w):
        return self._rgb_to_hex(a + (b - a) * w for a, b in zip(self._hex_to_rgb(c_a), self._hex_to_rgb(c_b)))

    def build_ui(self):
        main_frame = ttk.Frame(self, padding=30)
        main_frame.pack(fill=BOTH, expand=True)

        header = ttk.Frame(main_frame)
        header.pack(fill=X, pady=(0, 20))
        
        title_box = ttk.Frame(header)
        title_box.pack(side=LEFT)
        ttk.Label(title_box, text="Smart Photo Downloader & Syncer", font="-size 22 -weight bold", bootstyle=PRIMARY).pack(anchor=W)
        ttk.Label(title_box, text="SAVES CLOUDINARY CREDITS BY AUTO-DETECTING LOCAL FILES", font="-size 9 -weight bold", bootstyle=SECONDARY).pack(anchor=W)

        ttk.Button(header, text="⟳ Refresh Stats", bootstyle="outline-info", command=self.refresh_stats_async).pack(side=RIGHT)

        # -- Metrics Grid --
        cards_frame = ttk.Frame(main_frame)
        cards_frame.pack(fill=X, pady=(0, 20))
        
        self.stat_vars = {}
        self._create_stat_card(cards_frame, "📸", "TOTAL PROFILES WITH PHOTOS", "0", INFO, "total")
        self._create_stat_card(cards_frame, "💾", "FULLY SYNCED (DISK + DB)", "0", SUCCESS, "synced")
        self._create_stat_card(cards_frame, "🔍", "UNREGISTERED LOCAL FILES", "0", WARNING, "pending_db_sync")
        self._create_stat_card(cards_frame, "☁️", "CLOUD DOWNLOADS NEEDED", "0", DANGER, "pending_download")

        # -- Controls & Progress --
        controls_frame = ttk.Frame(main_frame, style="Card.TFrame", padding=15)
        controls_frame.pack(fill=X, pady=(0, 20))
        
        self.btn_start = ttk.Button(controls_frame, text="▶ Start Processing Pipeline", bootstyle=SUCCESS, width=25, command=self.start_download)
        self.btn_start.pack(side=LEFT, padx=(0, 10))
        
        self.btn_stop = ttk.Button(controls_frame, text="⏹ Stop", bootstyle=DANGER, width=10, command=self.stop_download, state=DISABLED)
        self.btn_stop.pack(side=LEFT, padx=(0, 15))

        self.progress_var = ttk.DoubleVar()
        self.progress = ttk.Progressbar(controls_frame, variable=self.progress_var, maximum=100, bootstyle=SUCCESS)
        self.progress.pack(side=LEFT, fill=X, expand=True, padx=10)
        
        self.lbl_progress_text = ttk.Label(controls_frame, text="0 / 0", font="-weight bold", background=self.CARD_BG, width=12, anchor=E)
        self.lbl_progress_text.pack(side=LEFT, padx=10)

        # -- Activity Log --
        log_hdr = ttk.Frame(main_frame)
        log_hdr.pack(fill=X, pady=(0, 5))
        ttk.Label(log_hdr, text="📟 PROCESSING LOG (STDOUT)", font="-size 11 -weight bold", foreground="gray").pack(side=LEFT)
        ttk.Button(log_hdr, text="Clear", bootstyle="secondary-link", command=self.clear_log).pack(side=RIGHT)

        log_frame = ttk.Frame(main_frame, style="Card.TFrame", padding=2)
        log_frame.pack(fill=BOTH, expand=True)

        self.log_box = ScrolledText(log_frame, autohide=True, wrap="word")
        self.log_box.pack(fill=BOTH, expand=True, padx=2, pady=2)
        self.log_box.text.configure(state="disabled", font=("Consolas", 10), bg="#1e1e1e", borderwidth=0)
        
        self.log_box.text.tag_config("INFO", foreground="#cccccc")
        self.log_box.text.tag_config("SUCCESS", foreground="#4CD37E", font=("Consolas", 10, "bold"))
        self.log_box.text.tag_config("WARNING", foreground="#FFB454", font=("Consolas", 10, "bold"))
        self.log_box.text.tag_config("ERROR", foreground="#FF6B6B", font=("Consolas", 10, "bold"))

        # Inject robust logger
        gui_logger = TkinterLogHandler(self.gui_queue)
        gui_logger.setFormatter(logging.Formatter('%(message)s'))
        logging.getLogger().addHandler(gui_logger)
        
        self.log("Photo Engine ready. Run a stat refresh to detect unlinked local files.", "SUCCESS")

    def _create_stat_card(self, parent, icon, title, initial_value, style, var_name):
        outer = ttk.Frame(parent, borderwidth=1, relief="solid")
        outer.pack(side=LEFT, fill=BOTH, expand=True, padx=6)
        
        ttk.Frame(outer, bootstyle=style, width=4).pack(side=LEFT, fill=Y)
        
        inner = ttk.Frame(outer, padding=(16, 14))
        inner.pack(side=LEFT, fill=BOTH, expand=True)
        
        top_row = ttk.Frame(inner)
        top_row.pack(fill=X, anchor=W)
        ttk.Label(top_row, text=icon, font="-size 12").pack(side=LEFT, padx=(0, 6))
        ttk.Label(top_row, text=title, font="-size 8 -weight bold", bootstyle=style).pack(side=LEFT)
        
        val_lbl = ttk.Label(inner, text=initial_value, font="-size 26 -weight bold")
        val_lbl.pack(anchor=W, pady=(8, 0))
        
        self.stat_vars[var_name] = val_lbl

    # --------------------------------------------------------------------------
    # THREAD-SAFE QUEUE LOGIC
    # --------------------------------------------------------------------------
    def log(self, message, level="INFO"):
        self.gui_queue.put(("log", {"msg": message, "level": level}))

    def _process_gui_queue(self):
        for _ in range(100):
            try:
                kind, payload = self.gui_queue.get_nowait()
                if kind == "log":
                    self._append_log(payload["msg"], payload["level"])
                elif kind == "stats":
                    self._apply_stats(payload)
                elif kind == "progress":
                    self._apply_progress(payload["current"], payload["total"])
                elif kind == "finished":
                    self._apply_finished(payload["success"])
            except queue.Empty:
                break
            except Exception as e:
                print(f"GUI Queue Error: {e}")
        self.after(30, self._process_gui_queue)

    def _append_log(self, message, level):
        timestamp = datetime.now().strftime("%H:%M:%S")
        formatted_msg = f"[{timestamp}] {message}\n"
        self.log_box.text.configure(state="normal")
        self.log_box.text.insert(END, formatted_msg, level)
        self.log_box.text.see(END)
        
        lc = int(self.log_box.text.index('end-1c').split('.')[0])
        if lc > MAX_LOG_LINES: 
            self.log_box.text.delete('1.0', f'{lc - MAX_LOG_LINES}.0')
            
        self.log_box.text.configure(state="disabled")

    def clear_log(self):
        self.log_box.text.configure(state="normal")
        self.log_box.text.delete("1.0", END)
        self.log_box.text.configure(state="disabled")

    # --------------------------------------------------------------------------
    # ENGINE TRIGGERS
    # --------------------------------------------------------------------------
    def refresh_stats_async(self):
        def _fetch():
            stats = self.manager.get_stats()
            self.gui_queue.put(("stats", stats))
        threading.Thread(target=_fetch, daemon=True).start()

    def _apply_stats(self, stats):
        self.stat_vars["total"].configure(text=str(stats["total"]))
        self.stat_vars["synced"].configure(text=str(stats["synced"]))
        self.stat_vars["pending_db_sync"].configure(text=str(len(stats["pending_db_sync"])))
        self.stat_vars["pending_download"].configure(text=str(len(stats["pending_download"])))
        
        pending_total = len(stats["pending_db_sync"]) + len(stats["pending_download"])
        self.progress_var.set(0)
        self.lbl_progress_text.configure(text=f"0 / {pending_total}")

    def _proxy_update_progress(self, current, total):
        """Thread-safe pipe for the engine callback"""
        self.gui_queue.put(("progress", {"current": current, "total": total}))

    def _apply_progress(self, current, total):
        percent = (current / total) * 100 if total > 0 else 0
        self.progress_var.set(percent)
        self.lbl_progress_text.configure(text=f"{current} / {total}")
        if current % 5 == 0 or current == total:
            self.refresh_stats_async()

    def _proxy_download_finished(self, success):
        self.gui_queue.put(("finished", {"success": success}))

    def _apply_finished(self, success):
        self.btn_start.configure(state=NORMAL)
        self.btn_stop.configure(state=DISABLED)
        self.progress_var.set(100 if success else 0)
        self.refresh_stats_async()

    def start_download(self):
        if self.manager.is_running: return
        self.btn_start.configure(state=DISABLED)
        self.btn_stop.configure(state=NORMAL)
        
        threading.Thread(
            target=self.manager.process_photos, 
            args=(self._proxy_update_progress, self._proxy_download_finished), 
            daemon=True
        ).start()

    def stop_download(self):
        if self.manager.is_running:
            self.log("Canceling process... finishing current file.", "WARNING")
            self.manager.cancel_requested = True
            self.btn_stop.configure(state=DISABLED)

if __name__ == "__main__":
    app = PhotoDownloaderGUI()
    app.mainloop()