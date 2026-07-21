import os
import requests
import logging
import threading
from datetime import datetime, timezone
import ttkbootstrap as ttk
from ttkbootstrap.constants import *

# Import models and DB initialization from your schema
try:
    from app.schema import Attendee, DownloadedPhoto, get_database_sessions
except ModuleNotFoundError:
    from schema import Attendee, DownloadedPhoto, get_database_sessions

# ==============================================================================
# PATHS & CONFIG (PORTABLE BASE_DIR)
# ==============================================================================
BASE_DIR = os.path.dirname(os.path.abspath(__file__))
PHOTOS_DIR = os.path.join(BASE_DIR, 'attendee_photos')
LOG_DIR = os.path.join(BASE_DIR, 'logs')

os.makedirs(PHOTOS_DIR, exist_ok=True)
os.makedirs(LOG_DIR, exist_ok=True)

# ==============================================================================
# CUSTOM LOGGING HANDLER FOR TKINTER
# ==============================================================================
class TkinterLogHandler(logging.Handler):
    """Streams logs directly into the GUI Treeview safely."""
    def __init__(self, treeview):
        super().__init__()
        self.treeview = treeview

    def emit(self, record):
        msg = self.format(record)
        time_str = datetime.fromtimestamp(record.created).strftime('%H:%M:%S')
        
        tag = 'info'
        if record.levelno >= logging.ERROR: tag = 'error'
        elif record.levelno >= logging.WARNING: tag = 'warning'
        
        self.treeview.after(0, self._insert_log, time_str, record.levelname, msg, tag)
        
    def _insert_log(self, time_str, level, msg, tag):
        self.treeview.insert('', END, values=(time_str, level, msg), tags=(tag,))
        self.treeview.yview_moveto(1)

logging.basicConfig(
    filename=os.path.join(LOG_DIR, 'photo_downloader.log'),
    level=logging.INFO,
    format='%(asctime)s [%(levelname)s] %(message)s'
)

# ==============================================================================
# CORE DOWNLOAD MANAGER
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
            
            data_dicts = []
            for p in mysql_photos:
                row_data = {c.name: getattr(p, c.name) for c in p.__table__.columns}
                data_dicts.append(row_data)

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
        """Calculates total pending and downloaded photos securely."""
        if not self.SessionMySQL:
            return {"total": 0, "downloaded": 0, "pending": 0, "pending_list": []}
            
        session = self.SessionMySQL()
        try:
            all_attendees = session.query(Attendee).all()
            
            attendees_with_photos = [
                a for a in all_attendees 
                if a.photo_url and str(a.photo_url).strip().lower() not in ('', 'none', 'null')
            ]
            
            downloaded_records = session.query(DownloadedPhoto).all()
            downloaded_map = {record.attendee_id: record.local_path for record in downloaded_records}
            
            valid_downloaded = 0
            pending_attendees = []
            
            for att in attendees_with_photos:
                # Resolve portable path dynamically to verify existence
                stored_path = downloaded_map.get(att.attendee_id)
                absolute_check_path = os.path.join(BASE_DIR, stored_path) if stored_path else os.path.join(PHOTOS_DIR, f"{att.attendee_id}.jpg")
                
                if att.attendee_id in downloaded_map and os.path.exists(absolute_check_path):
                    valid_downloaded += 1
                else:
                    pending_attendees.append(att)

            return {
                "total": len(attendees_with_photos),
                "downloaded": valid_downloaded,
                "pending": len(pending_attendees),
                "pending_list": pending_attendees
            }
        except Exception as e:
            logging.error(f"Failed to fetch stats: {e}")
            return {"total": 0, "downloaded": 0, "pending": 0, "pending_list": []}
        finally:
            session.close()

    def download_photos(self, update_progress_callback, finished_callback):
        """Background loop to download missing photos with relative portable paths."""
        self.is_running = True
        self.cancel_requested = False
        session = self.SessionMySQL()
        
        try:
            stats = self.get_stats()
            pending_list = stats.get("pending_list", [])
            total_pending = len(pending_list)
            
            if total_pending == 0:
                logging.info("No pending photos to download.")
                finished_callback(True)
                return

            logging.info(f"Starting download of {total_pending} missing photos...")
            
            success_count = 0
            for index, att in enumerate(pending_list):
                if self.cancel_requested:
                    logging.warning("Download process aborted by user.")
                    break
                
                url = str(att.photo_url).strip()
                local_filename = f"{att.attendee_id}.jpg"
                absolute_path = os.path.join(PHOTOS_DIR, local_filename)
                
                # STORE PORTABLE RELATIVE PATH IN DATABASE (e.g., attendee_photos/TDE26-G-XXX.jpg)
                relative_path = os.path.relpath(absolute_path, BASE_DIR)
                
                try:
                    response = requests.get(url, stream=True, timeout=10)
                    response.raise_for_status()
                    
                    with open(absolute_path, 'wb') as file:
                        for chunk in response.iter_content(1024):
                            file.write(chunk)
                    
                    file_size_kb = round(os.path.getsize(absolute_path) / 1024, 2)
                    
                    # Update or Insert Database Record with Portable Relative Path
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
                    logging.info(f"Downloaded: {local_filename} -> Saved relative path ({file_size_kb} KB)")
                    
                except requests.exceptions.RequestException as req_err:
                    logging.error(f"Failed to download {att.attendee_id}: Network Error ({req_err})")
                except Exception as e:
                    logging.error(f"Error saving {att.attendee_id}: {e}")
                
                update_progress_callback(index + 1, total_pending)
                
            logging.info(f"Finished. Successfully downloaded {success_count} photos.")
            
            if success_count > 0:
                self.mirror_photos_to_sqlite()
                
            finished_callback(True)
            
        except Exception as e:
            logging.error(f"Critical error in download engine: {e}")
            finished_callback(False)
        finally:
            session.close()
            self.is_running = False

# ==============================================================================
# DASHBOARD GUI
# ==============================================================================
class PhotoDownloaderGUI(ttk.Window):
    def __init__(self):
        super().__init__(themename="cyborg", title="TDE UP 2026 — Offline Photo Engine")
        self.geometry("1100x700")
        
        self.manager = PhotoDownloadManager()
        
        self.build_ui()
        self.refresh_stats()

    def build_ui(self):
        main_frame = ttk.Frame(self, padding=30)
        main_frame.pack(fill=BOTH, expand=True)

        header = ttk.Frame(main_frame)
        header.pack(fill=X, pady=(0, 20))
        ttk.Label(header, text="Cloudinary → Local Portable Mirror Engine", font="-size 20 -weight bold", bootstyle=PRIMARY).pack(side=LEFT)
        ttk.Button(header, text="⟳ Refresh Stats", bootstyle="outline-secondary", command=self.refresh_stats).pack(side=RIGHT)

        cards_frame = ttk.Frame(main_frame)
        cards_frame.pack(fill=X, pady=(0,20))
        
        self.stat_vars = {}
        
        self._create_stat_card(cards_frame, "📸 TOTAL ATTENDEES WITH PHOTOS", "0", INFO, var_name="total")
        self._create_stat_card(cards_frame, "💾 SAVED LOCALLY", "0", SUCCESS, var_name="downloaded")
        self._create_stat_card(cards_frame, "⏳ PENDING DOWNLOAD", "0", WARNING, var_name="pending")

        controls_frame = ttk.Frame(main_frame, padding=15, borderwidth=1, relief="solid")
        controls_frame.pack(fill=X, pady=10)
        
        self.btn_start = ttk.Button(controls_frame, text="▶ Start Download", bootstyle=SUCCESS, width=20, command=self.start_download)
        self.btn_start.pack(side=LEFT, padx=(0,10))
        
        self.btn_stop = ttk.Button(controls_frame, text="⏹ Stop", bootstyle=DANGER, width=10, command=self.stop_download, state=DISABLED)
        self.btn_stop.pack(side=LEFT, padx=(0,15))

        self.progress_var = ttk.DoubleVar()
        self.progress = ttk.Progressbar(controls_frame, variable=self.progress_var, maximum=100, bootstyle=SUCCESS)
        self.progress.pack(side=LEFT, fill=X, expand=True, padx=10)
        
        self.lbl_progress_text = ttk.Label(controls_frame, text="0 / 0", width=12, anchor=E)
        self.lbl_progress_text.pack(side=LEFT, padx=10)

        log_frame = ttk.Frame(main_frame)
        log_frame.pack(fill=BOTH, expand=True, pady=(20,0))
        ttk.Label(log_frame, text="Activity Log", font="-weight bold").pack(anchor=W, pady=(0,5))
        
        cols = ("Time", "Level", "Message")
        self.log_tree = ttk.Treeview(log_frame, columns=cols, show="headings", bootstyle=INFO)
        self.log_tree.heading("Time", text="TIME", anchor=W)
        self.log_tree.heading("Level", text="LEVEL", anchor=W)
        self.log_tree.heading("Message", text="MESSAGE", anchor=W)
        self.log_tree.column("Time", width=100, stretch=False)
        self.log_tree.column("Level", width=100, stretch=False)
        
        self.log_tree.tag_configure('error', foreground='#ff4444')
        self.log_tree.tag_configure('warning', foreground='#ffbb33')
        self.log_tree.tag_configure('info', foreground='white')
        
        scrollbar = ttk.Scrollbar(log_frame, orient=VERTICAL, command=self.log_tree.yview)
        self.log_tree.configure(yscrollcommand=scrollbar.set)
        
        self.log_tree.pack(side=LEFT, fill=BOTH, expand=True)
        scrollbar.pack(side=RIGHT, fill=Y)

        gui_logger = TkinterLogHandler(self.log_tree)
        gui_logger.setFormatter(logging.Formatter('%(message)s'))
        logging.getLogger().addHandler(gui_logger)

    def _create_stat_card(self, parent, title, initial_value, style, var_name):
        frame = ttk.Frame(parent, borderwidth=1, relief="solid", padding=20)
        frame.pack(side=LEFT, fill=BOTH, expand=True, padx=5)
        
        ttk.Label(frame, text=title, font="-size 9 -weight bold", bootstyle=style).pack(anchor=W)
        val_lbl = ttk.Label(frame, text=initial_value, font="-size 26 -weight bold")
        val_lbl.pack(anchor=W, pady=(10,0))
        
        self.stat_vars[var_name] = val_lbl

    def refresh_stats(self):
        stats = self.manager.get_stats()
        self.stat_vars["total"].configure(text=str(stats["total"]))
        self.stat_vars["downloaded"].configure(text=str(stats["downloaded"]))
        self.stat_vars["pending"].configure(text=str(stats["pending"]))
        self.progress_var.set(0)
        self.lbl_progress_text.configure(text=f"0 / {stats['pending']}")

    def update_progress(self, current, total):
        def update():
            percent = (current / total) * 100 if total > 0 else 0
            self.progress_var.set(percent)
            self.lbl_progress_text.configure(text=f"{current} / {total}")
            if current % 5 == 0 or current == total:
                self.refresh_stats()
        self.after(0, update)

    def download_finished(self, success):
        def finalize():
            self.btn_start.configure(state=NORMAL)
            self.btn_stop.configure(state=DISABLED)
            self.progress_var.set(100 if success else 0)
            self.refresh_stats()
        self.after(0, finalize)

    def start_download(self):
        if self.manager.is_running: return
        self.btn_start.configure(state=DISABLED)
        self.btn_stop.configure(state=NORMAL)
        
        threading.Thread(
            target=self.manager.download_photos, 
            args=(self.update_progress, self.download_finished), 
            daemon=True
        ).start()

    def stop_download(self):
        if self.manager.is_running:
            logging.warning("Canceling download process... finishing current file.")
            self.manager.cancel_requested = True
            self.btn_stop.configure(state=DISABLED)

if __name__ == "__main__":
    app = PhotoDownloaderGUI()
    app.mainloop()