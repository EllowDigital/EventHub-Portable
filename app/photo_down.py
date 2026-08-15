import os
import sys
import ctypes
import requests
from requests.adapters import HTTPAdapter
from urllib3.util.retry import Retry
import logging
from logging.handlers import RotatingFileHandler
import threading
import queue
from datetime import datetime, timezone

from PySide6.QtWidgets import (QApplication, QMainWindow, QWidget, QVBoxLayout, QHBoxLayout, 
                               QGridLayout, QLabel, QPushButton, QFrame, QProgressBar, 
                               QTextEdit, QMessageBox)
from PySide6.QtCore import Qt, QTimer
from PySide6.QtGui import QColor, QFont, QIcon, QTextCursor

try:
    from app.schema import Attendee, DownloadedPhoto, get_database_sessions
except ModuleNotFoundError:
    from schema import Attendee, DownloadedPhoto, get_database_sessions

def global_exception_handler(*args):
    logging.error("Uncaught GUI Exception intercepted. App remains running.", exc_info=args)

sys.excepthook = global_exception_handler

BASE_DIR = os.path.dirname(os.path.abspath(__file__))
PHOTOS_DIR = os.path.join(BASE_DIR, 'attendee_photos')
LOG_DIR = os.path.join(BASE_DIR, 'logs')

os.makedirs(PHOTOS_DIR, exist_ok=True)
os.makedirs(LOG_DIR, exist_ok=True)
MAX_LOG_LINES = 2000

# Theme Colors Map
COLORS = {
    "PRIMARY": "#375a7f",
    "INFO": "#0dcaf0",
    "SUCCESS": "#00bc8c",
    "WARNING": "#f39c12",
    "DANGER": "#e74c3c",
    "SECONDARY": "#888888",
    "BG_DARK": "#141414",
    "CARD_BG": "#242424",
    "BORDER": "#333333",
    "TEXT": "#e0e0e0"
}

# ==============================================================================
# LOG HANDLER
# ==============================================================================
class QtLogHandler(logging.Handler):
    def __init__(self, gui_queue):
        super().__init__()
        self.gui_queue = gui_queue

    def emit(self, record):
        msg = self.format(record)
        level = "INFO"
        if record.levelno >= logging.ERROR:
            level = "ERROR"
        elif record.levelno >= logging.WARNING:
            level = "WARNING"
        elif "SUCCESS" in msg.upper() or "FINISHED" in msg.upper():
            level = "SUCCESS"
        self.gui_queue.put(("log", {"msg": msg, "level": level}))

logging.basicConfig(
    handlers=[RotatingFileHandler(os.path.join(LOG_DIR, 'photo_downloader.log'), maxBytes=5_000_000, backupCount=2)],
    level=logging.INFO,
    format='%(asctime)s [%(levelname)s] %(message)s'
)

# ==============================================================================
# CORE PHOTO MANAGER
# ==============================================================================
class PhotoDownloadManager:
    def __init__(self):
        self.SessionMySQL = None
        self.SessionSQLite = None
        self.is_running = False
        self.cancel_requested = False
        
        self.http_session = requests.Session()
        retries = Retry(total=3, backoff_factor=0.5, status_forcelist=[500, 502, 503, 504])
        self.http_session.mount('http://', HTTPAdapter(max_retries=retries))
        self.http_session.mount('https://', HTTPAdapter(max_retries=retries))
        
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
        if not self.SessionSQLite or not self.SessionMySQL:
            return
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
        if not self.SessionMySQL:
            return {"total": 0, "synced": 0, "pending_db_sync": [], "pending_download": []}
        session = self.SessionMySQL()
        try:
            all_attendees = session.query(Attendee).all()
            attendees_with_photos = [
                a for a in all_attendees 
                if a.photo_url and str(a.photo_url).strip().lower() not in ('', 'none', 'null')
            ]
            downloaded_records = session.query(DownloadedPhoto).all()
            downloaded_map = {record.attendee_id: record for record in downloaded_records}
            
            fully_synced = 0
            pending_db_sync = []     
            pending_download = []    
            
            for att in attendees_with_photos:
                local_filename = f"{att.attendee_id}.jpg"
                absolute_path = os.path.join(PHOTOS_DIR, local_filename)
                
                if os.path.exists(absolute_path):
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
        self.is_running = True
        self.cancel_requested = False
        session = self.SessionMySQL()
        try:
            stats = self.get_stats()
            to_process = stats["pending_db_sync"] + stats["pending_download"]
            total_pending = len(to_process)
            
            if total_pending == 0:
                logging.info("Everything is up to date! No downloads or syncs needed.")
                finished_callback(True)
                return

            logging.info(f"Processing {total_pending} items ({len(stats['pending_db_sync'])} local-sync, {len(stats['pending_download'])} cloud-download)...")
            existing_map = {p.attendee_id: p for p in session.query(DownloadedPhoto).all()}
            success_count = 0
            
            for index, att in enumerate(to_process):
                if self.cancel_requested:
                    logging.warning("Process aborted by user.")
                    break
                
                url = str(att.photo_url).strip()
                local_filename = f"{att.attendee_id}.jpg"
                absolute_path = os.path.join(PHOTOS_DIR, local_filename)
                relative_path = os.path.relpath(absolute_path, BASE_DIR).replace('\\', '/')
                
                try:
                    if not os.path.exists(absolute_path):
                        response = self.http_session.get(url, stream=True, timeout=10)
                        response.raise_for_status()
                        with open(absolute_path, 'wb') as file:
                            for chunk in response.iter_content(32768):
                                file.write(chunk)
                        action_msg = "Downloaded & Saved"
                    else:
                        action_msg = "Found Locally -> DB Registered"
                    
                    file_size_kb = round(os.path.getsize(absolute_path) / 1024, 2)
                    existing_record = existing_map.get(att.attendee_id)
                    
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
                        existing_map[att.attendee_id] = new_record
                    
                    session.commit()
                    success_count += 1
                    logging.info(f"[{att.attendee_id}] {action_msg} ({file_size_kb} KB)")
                except requests.exceptions.RequestException as req_err:
                    logging.error(f"[{att.attendee_id}] Network Error downloading: {req_err}")
                except Exception as e:
                    logging.error(f"[{att.attendee_id}] Processing Error: {e}")
                    session.rollback()
                
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
# PYSIDE6 UI
# ==============================================================================
class PhotoDownloaderGUI(QMainWindow):
    def __init__(self):
        super().__init__()
        self.setWindowTitle("EventHub Portable (v2.6) — Offline Photo Engine")
        self.resize(1000, 700)
        self.setMinimumSize(950, 650)
        
        icon_path = os.path.join(BASE_DIR, "assets", "EventHub.ico")
        if os.path.exists(icon_path):
            try: self.setWindowIcon(QIcon(icon_path))
            except: pass

        self.gui_queue = queue.Queue()
        self.manager = PhotoDownloadManager()
        
        self._apply_stylesheet()
        self.build_ui()
        
        gui_logger = QtLogHandler(self.gui_queue)
        gui_logger.setFormatter(logging.Formatter('%(message)s'))
        logging.getLogger().addHandler(gui_logger)
        
        # Async Queue Processor
        self.queue_timer = QTimer(self)
        self.queue_timer.timeout.connect(self._process_gui_queue)
        self.queue_timer.start(50)
        
        QTimer.singleShot(200, self.refresh_stats_async)

    def _apply_stylesheet(self):
        self.setStyleSheet(f"""
            QMainWindow, QWidget {{ background-color: {COLORS['BG_DARK']}; color: {COLORS['TEXT']}; font-family: 'Segoe UI', Arial; }}
            QFrame#Card {{ background-color: {COLORS['CARD_BG']}; border: 1px solid {COLORS['BORDER']}; border-radius: 6px; }}
            QLabel {{ background: transparent; }}
            QTextEdit {{ background-color: {COLORS['BG_DARK']}; color: {COLORS['TEXT']}; border: none; font-family: 'Consolas', monospace; font-size: 11pt; }}
            QPushButton {{ background-color: #333; color: white; border: 1px solid #555; padding: 6px 12px; border-radius: 4px; font-weight: bold; }}
            QPushButton:hover {{ background-color: #444; }}
            QPushButton:disabled {{ background-color: #222; color: #666; border: 1px solid #333; }}
            QProgressBar {{ border: 1px solid {COLORS['BORDER']}; border-radius: 4px; background-color: {COLORS['BG_DARK']}; text-align: center; color: white; font-weight: bold; }}
            QProgressBar::chunk {{ background-color: {COLORS['SUCCESS']}; width: 20px; }}
            QScrollBar:vertical {{ background: {COLORS['BG_DARK']}; width: 14px; }}
            QScrollBar::handle:vertical {{ background: #444; min-height: 20px; border-radius: 7px; margin: 2px; }}
        """)

    def build_ui(self):
        central_widget = QWidget()
        self.setCentralWidget(central_widget)
        main_layout = QVBoxLayout(central_widget)
        main_layout.setContentsMargins(30, 30, 30, 30)

        # HEADER
        header = QHBoxLayout()
        title_box = QVBoxLayout()
        t1 = QLabel("📸 Smart Photo Downloader & Syncer")
        t1.setFont(QFont("Segoe UI", 24, QFont.Bold))
        t1.setStyleSheet(f"color: {COLORS['PRIMARY']};")
        t2 = QLabel("Engineered for Event Resilience • Powered by EllowDigital")
        t2.setFont(QFont("Segoe UI", 10, QFont.Bold))
        t2.setStyleSheet(f"color: {COLORS['SECONDARY']};")
        title_box.addWidget(t1)
        title_box.addWidget(t2)
        
        btn_refresh = QPushButton("⟳ Refresh Stats")
        btn_refresh.setStyleSheet(f"border: 1px solid {COLORS['INFO']}; color: {COLORS['INFO']}; background: transparent; padding: 8px 16px;")
        btn_refresh.clicked.connect(self.refresh_stats_async)
        
        header.addLayout(title_box)
        header.addStretch()
        header.addWidget(btn_refresh, 0, Qt.AlignBottom)
        main_layout.addLayout(header)
        main_layout.addSpacing(25)

        # STAT CARDS
        cards_frame = QHBoxLayout()
        self.stat_vars = {}
        self._create_stat_card(cards_frame, "📸", "TOTAL PROFILES", "0", COLORS["INFO"], "total")
        self._create_stat_card(cards_frame, "💾", "FULLY SYNCED", "0", COLORS["SUCCESS"], "synced")
        self._create_stat_card(cards_frame, "🔍", "LOCAL FILES UNLINKED", "0", COLORS["WARNING"], "pending_db_sync")
        self._create_stat_card(cards_frame, "☁️", "CLOUD DOWNLOADS", "0", COLORS["DANGER"], "pending_download")
        main_layout.addLayout(cards_frame)
        main_layout.addSpacing(25)

        # CONTROLS
        controls_frame = QFrame()
        controls_frame.setObjectName("Card")
        ctrl_layout = QHBoxLayout(controls_frame)
        ctrl_layout.setContentsMargins(20, 20, 20, 20)
        
        self.btn_start = QPushButton("▶ Start Processing Pipeline")
        self.btn_start.setFixedWidth(200)
        self.btn_start.setStyleSheet(f"background-color: {COLORS['SUCCESS']}; color: white; padding: 10px;")
        self.btn_start.clicked.connect(self.start_download)
        
        self.btn_stop = QPushButton("⏹ Stop")
        self.btn_stop.setFixedWidth(100)
        self.btn_stop.setStyleSheet(f"background-color: transparent; color: {COLORS['DANGER']}; border: 1px solid {COLORS['DANGER']}; padding: 10px;")
        self.btn_stop.setEnabled(False)
        self.btn_stop.clicked.connect(self.stop_download)
        
        self.progress = QProgressBar()
        self.progress.setRange(0, 100)
        self.progress.setValue(0)
        self.progress.setTextVisible(False)
        
        self.lbl_progress_text = QLabel("0 / 0")
        self.lbl_progress_text.setFixedWidth(100)
        self.lbl_progress_text.setAlignment(Qt.AlignRight | Qt.AlignVCenter)
        self.lbl_progress_text.setFont(QFont("Segoe UI", 11, QFont.Bold))
        
        ctrl_layout.addWidget(self.btn_start)
        ctrl_layout.addWidget(self.btn_stop)
        ctrl_layout.addSpacing(10)
        ctrl_layout.addWidget(self.progress, 1)
        ctrl_layout.addWidget(self.lbl_progress_text)
        main_layout.addWidget(controls_frame)
        main_layout.addSpacing(20)

        # LOGS
        log_hdr = QHBoxLayout()
        lbl_log = QLabel("📟 PROCESSING LOG (STDOUT)")
        lbl_log.setStyleSheet(f"color: {COLORS['SECONDARY']}; font-weight: bold; font-size: 11px;")
        btn_clear = QPushButton("Clear")
        btn_clear.setStyleSheet("background: transparent; color: #888; border: none; text-decoration: underline;")
        btn_clear.clicked.connect(self.clear_log)
        
        log_hdr.addWidget(lbl_log)
        log_hdr.addStretch()
        log_hdr.addWidget(btn_clear)
        main_layout.addLayout(log_hdr)
        
        log_frame = QFrame()
        log_frame.setObjectName("Card")
        log_lyt = QVBoxLayout(log_frame)
        log_lyt.setContentsMargins(10, 10, 10, 10)
        
        self.log_box = QTextEdit()
        self.log_box.setReadOnly(True)
        self.log_box.document().setMaximumBlockCount(MAX_LOG_LINES)
        log_lyt.addWidget(self.log_box)
        main_layout.addWidget(log_frame, 1)

        self.log("Photo Engine ready. Run a stat refresh to detect unlinked local files.", "SUCCESS")

    def _create_stat_card(self, parent_layout, icon, title, initial_value, color, var_name):
        card = QFrame()
        card.setObjectName("Card")
        lyt = QHBoxLayout(card)
        lyt.setContentsMargins(0, 0, 0, 0)
        
        stripe = QWidget()
        stripe.setFixedWidth(5)
        stripe.setStyleSheet(f"background-color: {color}; border-top-left-radius: 4px; border-bottom-left-radius: 4px;")
        lyt.addWidget(stripe)
        
        inner = QVBoxLayout()
        inner.setContentsMargins(15, 15, 15, 15)
        
        top_row = QHBoxLayout()
        icon_lbl = QLabel(icon)
        icon_lbl.setStyleSheet("border: none; font-size: 14px;")
        t_lbl = QLabel(title)
        t_lbl.setStyleSheet(f"color: {color}; font-weight: bold; border: none; font-size: 11px;")
        top_row.addWidget(icon_lbl)
        top_row.addWidget(t_lbl)
        top_row.addStretch()
        
        val_lbl = QLabel(initial_value)
        val_lbl.setFont(QFont("Segoe UI", 28, QFont.Bold))
        val_lbl.setStyleSheet(f"color: {color}; border: none;")
        
        inner.addLayout(top_row)
        inner.addWidget(val_lbl)
        lyt.addLayout(inner)
        
        parent_layout.addWidget(card)
        self.stat_vars[var_name] = {"label": val_lbl, "color": color}

    def log(self, message, level="INFO"):
        self.gui_queue.put(("log", {"msg": message, "level": level}))

    def animate_stat_flash(self, var_name):
        data = self.stat_vars[var_name]
        lbl = data["label"]
        original_color = data["color"]
        lbl.setStyleSheet(f"color: white; border: none;")
        QTimer.singleShot(150, lambda: lbl.setStyleSheet(f"color: {original_color}; border: none;") if lbl else None)

    def _process_gui_queue(self):
        for _ in range(100):
            try:
                kind, payload = self.gui_queue.get_nowait()
                if kind == "log": self._append_log(payload["msg"], payload["level"])
                elif kind == "stats": self._apply_stats(payload)
                elif kind == "progress": self._apply_progress(payload["current"], payload["total"])
                elif kind == "finished": self._apply_finished(payload["success"])
            except queue.Empty:
                break

    def _append_log(self, message, level):
        timestamp = datetime.now().strftime("%H:%M:%S")
        color_map = {
            "INFO": "#cccccc",
            "SUCCESS": COLORS["SUCCESS"],
            "WARNING": COLORS["WARNING"],
            "ERROR": COLORS["DANGER"]
        }
        color = color_map.get(level, "#cccccc")
        html_msg = f'<span style="color: {color};">[{timestamp}] {message}</span><br>'
        
        self.log_box.moveCursor(QTextCursor.End)
        self.log_box.insertHtml(html_msg)
        self.log_box.moveCursor(QTextCursor.End)

    def clear_log(self):
        self.log_box.clear()

    def refresh_stats_async(self):
        def _fetch():
            stats = self.manager.get_stats()
            self.gui_queue.put(("stats", stats))
        threading.Thread(target=_fetch, daemon=True).start()

    def _apply_stats(self, stats):
        for key in ["total", "synced", "pending_db_sync", "pending_download"]:
            new_val = str(stats[key] if key == "total" or key == "synced" else len(stats[key]))
            lbl = self.stat_vars[key]["label"]
            if lbl.text() != new_val:
                lbl.setText(new_val)
                self.animate_stat_flash(key)
        
        pending_total = len(stats["pending_db_sync"]) + len(stats["pending_download"])
        self.progress.setValue(0)
        self.lbl_progress_text.setText(f"0 / {pending_total}")

    def _proxy_update_progress(self, current, total):
        self.gui_queue.put(("progress", {"current": current, "total": total}))

    def _apply_progress(self, current, total):
        percent = int((current / total) * 100) if total > 0 else 0
        self.progress.setValue(percent)
        self.lbl_progress_text.setText(f"{current} / {total}")
        if current % 10 == 0 or current == total:
            self.refresh_stats_async()

    def _proxy_download_finished(self, success):
        self.gui_queue.put(("finished", {"success": success}))

    def _apply_finished(self, success):
        self.btn_start.setEnabled(True)
        self.btn_stop.setEnabled(False)
        self.btn_start.setStyleSheet(f"background-color: {COLORS['SUCCESS']}; color: white; padding: 10px;")
        self.btn_stop.setStyleSheet(f"background-color: transparent; color: {COLORS['DANGER']}; border: 1px solid {COLORS['DANGER']}; padding: 10px;")
        self.progress.setValue(100 if success else 0)
        self.refresh_stats_async()

    def start_download(self):
        if self.manager.is_running: return
        self.btn_start.setEnabled(False)
        self.btn_start.setStyleSheet(f"background-color: transparent; color: {COLORS['SUCCESS']}; border: 1px solid {COLORS['SUCCESS']}; padding: 10px;")
        self.btn_stop.setEnabled(True)
        self.btn_stop.setStyleSheet(f"background-color: {COLORS['DANGER']}; color: white; padding: 10px;")
        
        threading.Thread(
            target=self.manager.process_photos, 
            args=(self._proxy_update_progress, self._proxy_download_finished), 
            daemon=True
        ).start()

    def stop_download(self):
        if self.manager.is_running:
            self.log("Canceling process... finishing current file.", "WARNING")
            self.manager.cancel_requested = True
            self.btn_stop.setEnabled(False)

    def closeEvent(self, event):
        if self.manager.is_running:
            self.manager.cancel_requested = True
            self.log("Waiting for background processes to exit...", "WARNING")
            QTimer.singleShot(500, self.close)
            event.ignore()
        else:
            event.accept()

if __name__ == "__main__":
    if os.name == 'nt':
        try:
            my_app_id = os.environ.get("EVENTHUB_TOOL_ID", "EventHub.Tool.photos")
            ctypes.windll.shell32.SetCurrentProcessExplicitAppUserModelID(my_app_id)
        except Exception: pass
    app = QApplication(sys.argv)
    window = PhotoDownloaderGUI()
    window.show()
    sys.exit(app.exec())