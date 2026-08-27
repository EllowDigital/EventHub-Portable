import sys
import os
import time
import sqlite3
import logging
from PySide6.QtWidgets import (
    QApplication, QWidget, QVBoxLayout, QHBoxLayout, QPushButton,
    QTextEdit, QLabel, QProgressBar, QFileDialog, QLineEdit,
    QGroupBox, QGridLayout, QFrame, QMessageBox
)
from PySide6.QtGui import QTextCursor, QFont
from PySide6.QtCore import QThread, Signal, Qt

# 🛡️ Corrected & Optimized SQLAlchemy Imports
from sqlalchemy import create_engine, inspect, text
from sqlalchemy.orm import sessionmaker
from sqlalchemy.dialects.mysql import insert

# Import database models and configurations directly from schema.py
from schema import (
    Attendee, OfflineKioskAttendee, DownloadedPhoto, 
    load_db_config, init_database
)

CHUNK_SIZE = 5000  # Stream batch size to prevent RAM overflow


class DBValidatorWorker(QThread):
    """Worker to inspect and validate the SQLite file and target MySQL schema."""
    validation_done = Signal(bool, dict, str)
    log_signal = Signal(str)

    def __init__(self, sqlite_path):
        super().__init__()
        self.sqlite_path = sqlite_path

    def run(self):
        self.log_signal.emit(f"Validating database file: {self.sqlite_path}...")
        
        # 1. File existence and header validation
        if not os.path.exists(self.sqlite_path):
            self.validation_done.emit(False, {}, "Selected SQLite file does not exist.")
            return

        try:
            with open(self.sqlite_path, 'rb') as f:
                header = f.read(16)
                if header != b'SQLite format 3\x00':
                    self.validation_done.emit(False, {}, "File is not a valid SQLite 3 database.")
                    return
        except Exception as e:
            self.validation_done.emit(False, {}, f"Unable to read file: {e}")
            return

        # 2. SQLite schema and row count verification
        try:
            sqlite_url = f"sqlite:///{os.path.abspath(self.sqlite_path)}"
            sqlite_engine = create_engine(sqlite_url, connect_args={'check_same_thread': False})
            sqlite_session = sessionmaker(bind=sqlite_engine)()

            inspector = inspect(sqlite_engine)
            existing_tables = inspector.get_table_names()

            models_to_check = [Attendee, OfflineKioskAttendee, DownloadedPhoto]
            stats = {}
            total_records = 0

            for model in models_to_check:
                tbl_name = model.__tablename__
                if tbl_name in existing_tables:
                    count = sqlite_session.query(model).count()
                    stats[tbl_name] = count
                    total_records += count
                    self.log_signal.emit(f"Found table '{tbl_name}' -> {count:,} rows")
                else:
                    stats[tbl_name] = 0
                    self.log_signal.emit(f"Warning: Table '{tbl_name}' not found in source SQLite.")

            stats['total_records'] = total_records
            sqlite_session.close()

            # 3. Test Target MySQL Connection
            config = load_db_config()
            if not config.get("mysql", {}).get("enabled", False):
                self.validation_done.emit(False, stats, "MySQL is not enabled in schema.json.")
                return

            my_config = config["mysql"]
            db_name = my_config["database"]
            mysql_url = f"mysql+mysqldb://{my_config['user']}:{my_config['password']}@{my_config['host']}:{my_config['port']}/{db_name}"
            
            mysql_session_maker = init_database(mysql_url, db_name=db_name, is_mysql=True)
            mysql_session = mysql_session_maker()
            mysql_session.execute(text("SELECT 1"))
            mysql_session.close()

            self.log_signal.emit(f"MySQL Hub target database '{db_name}' verified and accessible.")
            self.validation_done.emit(True, stats, "Validation successful. Ready for restoration.")

        except Exception as e:
            self.log_signal.emit(f"Validation Error: {str(e)}")
            self.validation_done.emit(False, {}, str(e))


class DatabaseRestoreWorker(QThread):
    """High-throughput chunked restoration worker."""
    log_signal = Signal(str)
    progress_signal = Signal(int)
    stats_signal = Signal(int, float)  # total_synced, rows_per_second
    finished_signal = Signal(bool, str)

    def __init__(self, sqlite_path):
        super().__init__()
        self.sqlite_path = sqlite_path
        self._is_running = True

    def stop(self):
        self._is_running = False

    def run(self):
        start_time = time.time()
        self.log_signal.emit("Initializing bulk migration engine...")
        
        try:
            # 1. Connect Sources
            sqlite_url = f"sqlite:///{os.path.abspath(self.sqlite_path)}"
            sqlite_engine = create_engine(sqlite_url, connect_args={'check_same_thread': False})
            sqlite_db = sessionmaker(bind=sqlite_engine)()

            config = load_db_config()
            my_config = config["mysql"]
            db_name = my_config["database"]
            mysql_url = f"mysql+mysqldb://{my_config['user']}:{my_config['password']}@{my_config['host']}:{my_config['port']}/{db_name}"
            
            mysql_session_maker = init_database(mysql_url, db_name=db_name, is_mysql=True)
            mysql_db = mysql_session_maker()

            tables_to_sync = [Attendee, OfflineKioskAttendee, DownloadedPhoto]
            
            # Pre-calculate counts
            table_counts = {}
            total_records_all_tables = 0
            for model in tables_to_sync:
                try:
                    count = sqlite_db.query(model).count()
                except Exception:
                    count = 0
                table_counts[model] = count
                total_records_all_tables += count

            if total_records_all_tables == 0:
                self.finished_signal.emit(True, "Source database contains 0 syncable records.")
                return

            self.log_signal.emit(f"Total workload: {total_records_all_tables:,} records across 3 tables.")
            records_processed = 0

            # 2. Chunk-stream and bulk UPSERT
            for model in tables_to_sync:
                if not self._is_running:
                    self.log_signal.emit("Restoration cancelled by user.")
                    break

                total_table_records = table_counts[model]
                if total_table_records == 0:
                    continue

                self.log_signal.emit(f"\n--- Migrating {model.__tablename__} ({total_table_records:,} rows) ---")
                
                chunk = []
                for record in sqlite_db.query(model).yield_per(CHUNK_SIZE):
                    if not self._is_running:
                        break

                    row_dict = {col.name: getattr(record, col.name) for col in model.__table__.columns}
                    chunk.append(row_dict)

                    if len(chunk) >= CHUNK_SIZE:
                        self._process_chunk(mysql_db, model, chunk)
                        records_processed += len(chunk)
                        
                        elapsed = max(time.time() - start_time, 0.001)
                        rps = records_processed / elapsed
                        progress = int((records_processed / total_records_all_tables) * 100)

                        self.progress_signal.emit(progress)
                        self.stats_signal.emit(records_processed, rps)
                        self.log_signal.emit(f"[{records_processed:,}/{total_records_all_tables:,}] Batched {CHUNK_SIZE} rows ({rps:.0f} rows/sec)")
                        chunk = []

                if chunk and self._is_running:
                    self._process_chunk(mysql_db, model, chunk)
                    records_processed += len(chunk)
                    elapsed = max(time.time() - start_time, 0.001)
                    rps = records_processed / elapsed
                    progress = int((records_processed / total_records_all_tables) * 100)
                    
                    self.progress_signal.emit(progress)
                    self.stats_signal.emit(records_processed, rps)
                    self.log_signal.emit(f"Completed table {model.__tablename__}.")

            sqlite_db.close()
            mysql_db.close()

            if self._is_running:
                total_time = time.time() - start_time
                self.finished_signal.emit(True, f"Successfully restored {records_processed:,} records in {total_time:.2f}s!")
            else:
                self.finished_signal.emit(False, "Operation aborted.")

        except Exception as e:
            self.log_signal.emit(f"CRITICAL FAULT: {str(e)}")
            self.finished_signal.emit(False, str(e))

    def _process_chunk(self, mysql_db, model, chunk):
        """🛡️ Corruption-Proof Chunk Processing with Transaction Rollbacks."""
        try:
            stmt = insert(model).values(chunk)
            primary_keys = [key.name for key in inspect(model).primary_key]
            update_dict = {c.name: c for c in stmt.inserted if c.name not in primary_keys}
            
            if update_dict:
                stmt = stmt.on_duplicate_key_update(**update_dict)
                
            mysql_db.execute(stmt)
            mysql_db.commit()  # Atomically commit if clean
            
        except Exception as e:
            mysql_db.rollback()  # Instantly wipe chunk on failure to prevent partial writes
            raise e


class ModernRecoveryApp(QWidget):
    def __init__(self):
        super().__init__()
        self.setWindowTitle("Disaster Recovery Hub - SQLite to MySQL Mirror")
        self.resize(800, 680)
        self.setMinimumSize(700, 550)

        self.apply_dark_theme()
        self.init_ui()
        self.worker = None
        self.validator_worker = None

    def apply_dark_theme(self):
        self.setStyleSheet("""
            QWidget {
                background-color: #0d1117;
                color: #c9d1d9;
                font-family: 'Segoe UI', Consolas, monospace;
            }
            QGroupBox {
                border: 1px solid #30363d;
                border-radius: 8px;
                margin-top: 10px;
                font-weight: bold;
                color: #58a6ff;
                padding-top: 15px;
            }
            QGroupBox::title {
                subcontrol-origin: margin;
                left: 10px;
                padding: 0 5px;
            }
            QLineEdit {
                background-color: #161b22;
                border: 1px solid #30363d;
                border-radius: 6px;
                color: #f0f6fc;
                padding: 8px;
                font-size: 11pt;
            }
            QLineEdit:focus {
                border: 1px solid #58a6ff;
            }
            QPushButton {
                background-color: #21262d;
                color: #c9d1d9;
                border: 1px solid #30363d;
                border-radius: 6px;
                padding: 8px 16px;
                font-weight: bold;
                font-size: 10pt;
            }
            QPushButton:hover {
                background-color: #30363d;
                border-color: #8b949e;
            }
            QPushButton#ActionBtn {
                background-color: #238636;
                color: #ffffff;
                border: 1px solid #2ea043;
                font-size: 11pt;
                padding: 10px;
            }
            QPushButton#ActionBtn:hover {
                background-color: #2ea043;
            }
            QPushButton#ActionBtn:disabled {
                background-color: #1e3a29;
                color: #6e7681;
                border-color: #1e3a29;
            }
            QPushButton#VerifyBtn {
                background-color: #1f6feb;
                color: white;
                border: 1px solid #388bfd;
            }
            QPushButton#VerifyBtn:hover {
                background-color: #388bfd;
            }
            QTextEdit {
                background-color: #040d1a;
                border: 1px solid #1f2a38;
                border-radius: 6px;
                color: #39ff14;
                font-family: Consolas, monospace;
                font-size: 10pt;
                padding: 8px;
            }
            QProgressBar {
                border: 1px solid #30363d;
                border-radius: 6px;
                text-align: center;
                color: #ffffff;
                background-color: #161b22;
                font-weight: bold;
                height: 22px;
            }
            QProgressBar::chunk {
                background-color: #238636;
                border-radius: 5px;
            }
        """)

    def init_ui(self):
        main_layout = QVBoxLayout(self)
        main_layout.setContentsMargins(16, 16, 16, 16)
        main_layout.setSpacing(12)

        # 1. Header Banner
        header = QLabel("⚡ SQLite ➔ MySQL Mirror Recovery Engine")
        header.setFont(QFont("Segoe UI", 14, QFont.Bold))
        header.setStyleSheet("color: #58a6ff; margin-bottom: 2px;")
        main_layout.addWidget(header)

        # 2. Database Selection Box
        source_box = QGroupBox("Source SQLite Database Location")
        source_layout = QGridLayout(source_box)
        
        self.path_input = QLineEdit()
        self.path_input.setPlaceholderText("Select any .db3, .sqlite, or .db backup file...")
        
        browse_btn = QPushButton("📁 Browse")
        browse_btn.clicked.connect(self.browse_file)
        
        self.verify_btn = QPushButton("🔍 Check & Inspect")
        self.verify_btn.setObjectName("VerifyBtn")
        self.verify_btn.clicked.connect(self.run_verification)

        source_layout.addWidget(self.path_input, 0, 0)
        source_layout.addWidget(browse_btn, 0, 1)
        source_layout.addWidget(self.verify_btn, 0, 2)
        main_layout.addWidget(source_box)

        # 3. Telemetry / Metric Cards
        metric_box = QGroupBox("Inspection & Migration Metrics")
        metric_layout = QGridLayout(metric_box)

        self.card_attendees = self._create_metric_card("Attendees", "0")
        self.card_kiosk = self._create_metric_card("Offline Kiosk", "0")
        self.card_photos = self._create_metric_card("Photos Cached", "0")
        self.card_speed = self._create_metric_card("Transfer Rate", "0 rows/s")

        metric_layout.addWidget(self.card_attendees, 0, 0)
        metric_layout.addWidget(self.card_kiosk, 0, 1)
        metric_layout.addWidget(self.card_photos, 0, 2)
        metric_layout.addWidget(self.card_speed, 0, 3)
        main_layout.addWidget(metric_box)

        # 4. Console Log Terminal
        console_header = QHBoxLayout()
        log_label = QLabel("Live Execution Terminal:")
        log_label.setStyleSheet("color: #8b949e; font-weight: bold;")
        console_header.addWidget(log_label)
        
        clear_btn = QPushButton("Clear")
        clear_btn.setFixedHeight(22)
        clear_btn.setStyleSheet("padding: 2px 8px; font-size: 8pt;")
        clear_btn.clicked.connect(lambda: self.console.clear())
        console_header.addStretch()
        console_header.addWidget(clear_btn)
        main_layout.addLayout(console_header)

        self.console = QTextEdit()
        self.console.setReadOnly(True)
        main_layout.addWidget(self.console)

        # 5. Progress Section
        self.progress_bar = QProgressBar()
        self.progress_bar.setValue(0)
        main_layout.addWidget(self.progress_bar)

        # 6. Primary Action Buttons
        btn_layout = QHBoxLayout()
        self.start_btn = QPushButton("🚀 START MASS RESTORATION")
        self.start_btn.setObjectName("ActionBtn")
        self.start_btn.setEnabled(False)
        self.start_btn.clicked.connect(self.start_restoration)

        self.abort_btn = QPushButton("⏹ Cancel")
        self.abort_btn.setEnabled(False)
        self.abort_btn.setStyleSheet("background-color: #da3633; color: white;")
        self.abort_btn.clicked.connect(self.abort_restoration)

        btn_layout.addWidget(self.start_btn, 4)
        btn_layout.addWidget(self.abort_btn, 1)
        main_layout.addLayout(btn_layout)

    def _create_metric_card(self, title, default_val):
        frame = QFrame()
        frame.setStyleSheet("background-color: #161b22; border: 1px solid #21262d; border-radius: 6px; padding: 4px;")
        layout = QVBoxLayout(frame)
        layout.setContentsMargins(4, 4, 4, 4)

        t_lbl = QLabel(title)
        t_lbl.setStyleSheet("color: #8b949e; font-size: 9pt; border: none;")
        t_lbl.setAlignment(Qt.AlignCenter)

        v_lbl = QLabel(default_val)
        v_lbl.setObjectName("ValLabel")
        v_lbl.setStyleSheet("color: #58a6ff; font-size: 12pt; font-weight: bold; border: none;")
        v_lbl.setAlignment(Qt.AlignCenter)

        layout.addWidget(t_lbl)
        layout.addWidget(v_lbl)
        return frame

    def _update_card_val(self, frame, value):
        lbl = frame.findChild(QLabel, "ValLabel")
        if lbl:
            lbl.setText(str(value))

    def browse_file(self):
        file_path, _ = QFileDialog.getOpenFileName(
            self, "Select SQLite Mirror File", "", 
            "SQLite Databases (*.db *.sqlite *.sqlite3 *.db3);;All Files (*.*)"
        )
        if file_path:
            self.path_input.setText(file_path)
            self.run_verification()

    def log_message(self, msg):
        self.console.append(f">> {msg}")
        self.console.moveCursor(QTextCursor.End)

    def run_verification(self):
        db_path = self.path_input.text().strip()
        if not db_path:
            QMessageBox.warning(self, "Path Missing", "Please select or type a path to an SQLite database.")
            return

        self.verify_btn.setEnabled(False)
        self.start_btn.setEnabled(False)
        self.console.clear()
        self.log_message("Starting environment and database integrity check...")

        self.validator_worker = DBValidatorWorker(db_path)
        self.validator_worker.log_signal.connect(self.log_message)
        self.validator_worker.validation_done.connect(self.on_validation_finished)
        self.validator_worker.start()

    def on_validation_finished(self, success, stats, message):
        self.verify_btn.setEnabled(True)
        if success:
            self._update_card_val(self.card_attendees, f"{stats.get('attendees', 0):,}")
            self._update_card_val(self.card_kiosk, f"{stats.get('offline_kiosk_attendees', 0):,}")
            self._update_card_val(self.card_photos, f"{stats.get('downloaded_photos', 0):,}")
            self.start_btn.setEnabled(True)
            self.log_message(f"READY: {message}")
        else:
            self.start_btn.setEnabled(False)
            self.log_message(f"CHECK FAILED: {message}")
            QMessageBox.critical(self, "Integrity Check Failed", message)

    def start_restoration(self):
        db_path = self.path_input.text().strip()
        self.start_btn.setEnabled(False)
        self.verify_btn.setEnabled(False)
        self.abort_btn.setEnabled(True)
        self.progress_bar.setValue(0)

        self.worker = DatabaseRestoreWorker(db_path)
        self.worker.log_signal.connect(self.log_message)
        self.worker.progress_signal.connect(self.progress_bar.setValue)
        self.worker.stats_signal.connect(self.update_throughput_ui)
        self.worker.finished_signal.connect(self.on_restore_finished)
        self.worker.start()

    def update_throughput_ui(self, records_synced, rps):
        self._update_card_val(self.card_speed, f"{rps:,.0f} r/s")

    def abort_restoration(self):
        if self.worker and self.worker.isRunning():
            self.worker.stop()
            self.abort_btn.setEnabled(False)
            self.log_message("Abort signal transmitted. Terminating current batch...")

    def on_restore_finished(self, success, msg):
        self.start_btn.setEnabled(True)
        self.verify_btn.setEnabled(True)
        self.abort_btn.setEnabled(False)
        
        if success:
            self.log_message(f"SUCCESS: {msg}")
            QMessageBox.information(self, "Restoration Complete", msg)
        else:
            self.log_message(f"RESTORE HALTED: {msg}")
            QMessageBox.warning(self, "Restoration Stopped", msg)


if __name__ == "__main__":
    app = QApplication(sys.argv)
    window = ModernRecoveryApp()
    window.show()
    sys.exit(app.exec())