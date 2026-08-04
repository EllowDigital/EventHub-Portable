import os
import json
import enum
import logging
import threading
import platform
import subprocess
from datetime import datetime
import pymysql

import tkinter as tk

# Custom tone generator for Windows
try:
    import winsound
    HAS_WINSOUND = True
except ImportError:
    HAS_WINSOUND = False

# Text-to-Speech engine fallback for non-Windows
try:
    import pyttsx3
    HAS_TTS = True
except ImportError:
    HAS_TTS = False

# PyMySQL provides a pure-Python MySQL driver -- no C compiler required.
# This makes it answer to the same "MySQLdb" name SQLAlchemy expects.
pymysql.install_as_MySQLdb()

from sqlalchemy import (
    create_engine, inspect, Column, String, Text, DateTime, 
    Boolean, Enum, Float, Integer, JSON, CheckConstraint, text, exc
)
from sqlalchemy.orm import declarative_base, sessionmaker

# Resolve base directories dynamically for 100% portability
BASE_DIR = os.path.dirname(os.path.abspath(__file__))
CONFIG_PATH = os.path.join(BASE_DIR, 'config', 'schema.json')

Base = declarative_base()

# ==============================================================================
# ENUMS
# ==============================================================================
class GenderEnum(enum.Enum):
    MALE = 'MALE'
    FEMALE = 'FEMALE'
    OTHER = 'OTHER'

class AttendeeTypeEnum(enum.Enum):
    GENERAL = 'GENERAL'
    BUSINESS = 'BUSINESS'
    MEDIA = 'MEDIA'
    EXHIBITOR = 'EXHIBITOR'

# ==============================================================================
# DATABASE MODELS (Unchanged for 100% Compatibility)
# ==============================================================================

class Attendee(Base):
    __tablename__ = 'attendees'

    id = Column(String(36), primary_key=True)
    attendee_id = Column(String(30), unique=True, nullable=False, index=True)
    full_name = Column(String(255), nullable=False)
    mobile = Column(String(15), unique=True, nullable=False)
    email = Column(String(255), index=True, nullable=True)
    gender = Column(Enum(GenderEnum), nullable=False)
    attendee_type = Column(Enum(AttendeeTypeEnum), nullable=False, default=AttendeeTypeEnum.GENERAL, index=True)
    business_name = Column(String(255), nullable=True)
    business_category = Column(String(100), nullable=True)
    other_category = Column(String(255), nullable=True)
    address = Column(Text, nullable=False)
    city = Column(String(100), nullable=False)
    state = Column(String(100), nullable=False)
    pincode = Column(String(10), nullable=False)
    
    attendance_days = Column(JSON, nullable=False, default=[])
    photo_url = Column(Text, nullable=True)
    
    created_at = Column(DateTime, default=datetime.utcnow, index=True)
    updated_at = Column(DateTime, default=datetime.utcnow, onupdate=datetime.utcnow, index=True)
    
    checkin_history = Column(JSON, nullable=False, default={})
    
    # 🛡️ Synchronization Flags
    needs_cloud_sync = Column(Boolean, nullable=False, default=True, index=True)
    needs_sheet_sync = Column(Boolean, nullable=False, default=False)
    needs_local_sync = Column(Boolean, nullable=False, default=False, index=True)
    
    local_modified = Column(Boolean, nullable=False, default=False, index=True)
    device_name = Column(String(100), nullable=True)

    __table_args__ = (
        CheckConstraint('length(mobile) >= 10', name='check_mobile_length'),
    )

class OfflineKioskAttendee(Base):
    __tablename__ = 'offline_kiosk_attendees'

    id = Column(String(36), primary_key=True)
    attendee_id = Column(String(30), unique=True, nullable=False, index=True)
    full_name = Column(String(255), nullable=False)
    mobile = Column(String(15), unique=True, nullable=False)
    email = Column(String(255), nullable=True)
    gender = Column(Enum(GenderEnum), nullable=False)
    attendee_type = Column(Enum(AttendeeTypeEnum), nullable=False, default=AttendeeTypeEnum.GENERAL)
    business_name = Column(String(255), nullable=True)
    business_category = Column(String(100), nullable=True)
    other_category = Column(String(255), nullable=True)
    address = Column(Text, nullable=False)
    city = Column(String(100), nullable=False)
    state = Column(String(100), nullable=False)
    pincode = Column(String(10), nullable=False)
    
    attendance_days = Column(JSON, nullable=False, default=[])
    photo_url = Column(Text, nullable=True)
    
    created_at = Column(DateTime, default=datetime.utcnow)
    updated_at = Column(DateTime, default=datetime.utcnow, onupdate=datetime.utcnow, index=True)
    
    checkin_history = Column(JSON, nullable=False, default={})
    
    # 🛡️ Synchronization Flags
    needs_cloud_sync = Column(Boolean, nullable=False, default=True, index=True)
    needs_sheet_sync = Column(Boolean, nullable=False, default=False)
    needs_local_sync = Column(Boolean, nullable=False, default=False, index=True)
    
    local_modified = Column(Boolean, nullable=False, default=False, index=True)
    device_name = Column(String(100), nullable=True)

class DownloadedPhoto(Base):
    __tablename__ = 'downloaded_photos'

    id = Column(Integer, primary_key=True, autoincrement=True)
    attendee_id = Column(String(100), nullable=False, unique=True, index=True)
    photo_url = Column(Text, nullable=False)
    local_path = Column(Text, nullable=False)
    downloaded_at = Column(DateTime, nullable=False, default=datetime.utcnow)
    file_size_kb = Column(Float, default=0.0)

# ==============================================================================
# DATABASE INITIALIZATION & VERIFICATION ENGINE
# ==============================================================================

def load_db_config():
    """Loads the database configurations safely from schema.json"""
    if not os.path.exists(CONFIG_PATH):
        raise FileNotFoundError(f"Configuration file missing at: {CONFIG_PATH}")
    try:
        with open(CONFIG_PATH, 'r') as f:
            return json.load(f)
    except json.JSONDecodeError as e:
        raise ValueError(f"Invalid JSON format in config file {CONFIG_PATH}: {e}")

def create_mysql_database_if_missing(mysql_url, db_name):
    """Creates the MySQL database securely if it doesn't already exist."""
    base_url = mysql_url.rsplit('/', 1)[0]
    try:
        temp_engine = create_engine(base_url, pool_pre_ping=True)
        with temp_engine.begin() as conn:
            conn.execute(text(f"CREATE DATABASE IF NOT EXISTS {db_name} CHARACTER SET utf8mb4 COLLATE utf8mb4_unicode_ci;"))
            logging.info(f"[MySQL] Verified database '{db_name}' exists.")
    except exc.SQLAlchemyError as e:
        logging.error(f"[MySQL] Connection/Creation Error: {e}")
    finally:
        temp_engine.dispose()

def verify_and_update_columns(engine):
    """Safely compares actual DB schema with SQLAlchemy models and adds missing columns."""
    inspector = inspect(engine)
    existing_tables = inspector.get_table_names()

    for table_name, table in Base.metadata.tables.items():
        if table_name not in existing_tables:
            continue

        existing_columns = [col['name'] for col in inspector.get_columns(table_name)]
        
        with engine.begin() as conn:
            for column in table.columns:
                if column.name not in existing_columns:
                    logging.info(f"[{engine.name.upper()}] Missing column detected: {table_name}.{column.name}. Attempting to add...")
                    
                    col_type = column.type.compile(engine.dialect)
                    nullable = "NULL" if column.nullable else "NOT NULL"
                    default = f"DEFAULT {column.default.arg}" if (column.default and isinstance(column.default.arg, (int, str, float))) else ""
                    
                    alter_stmt = f"ALTER TABLE {table_name} ADD COLUMN {column.name} {col_type} {nullable} {default};"
                    try:
                        conn.execute(text(alter_stmt))
                        logging.info(f"[{engine.name.upper()}] Successfully added {table_name}.{column.name}.")
                    except exc.SQLAlchemyError as e:
                        logging.error(f"[{engine.name.upper()}] Failed to add {column.name} to {table_name}: {e}")

def init_database(db_url, db_name=None, is_mysql=False):
    """Initializes the engine with High-Concurrency pooling parameters."""
    if is_mysql and db_name:
        create_mysql_database_if_missing(db_url, db_name)
        engine = create_engine(
            db_url, 
            echo=False, 
            pool_size=25, 
            max_overflow=50, 
            pool_recycle=1800, 
            pool_pre_ping=True
        )
    else:
        engine = create_engine(
            db_url, 
            echo=False,
            connect_args={'check_same_thread': False},
            pool_pre_ping=True
        )

    try:
        Base.metadata.create_all(engine)
        verify_and_update_columns(engine)
    except exc.SQLAlchemyError as e:
        logging.error(f"Error initializing schema for {engine.name.upper()}: {e}")
        
    return sessionmaker(bind=engine)

def get_database_sessions():
    """Reads config, initializes databases safely, and returns the session makers."""
    config = load_db_config()
    sessions = {"sqlite": None, "mysql": None}

    if config.get("sqlite", {}).get("enabled", False):
        sq_config = config["sqlite"]
        db_folder = os.path.join(BASE_DIR, sq_config["folder_name"])
        os.makedirs(db_folder, exist_ok=True)
        
        sqlite_path = os.path.join(db_folder, sq_config["file_name"])
        sqlite_url = f"sqlite:///{sqlite_path}"
        
        logging.info(f"Initializing Local SQLite Database at {sqlite_path}...")
        sessions["sqlite"] = init_database(sqlite_url)

    if config.get("mysql", {}).get("enabled", False):
        my_config = config["mysql"]
        db_name = my_config["database"]
        mysql_url = f"mysql+mysqldb://{my_config['user']}:{my_config['password']}@{my_config['host']}:{my_config['port']}/{db_name}"
        
        logging.info(f"Initializing MySQL Hub Database ({db_name})...")
        sessions["mysql"] = init_database(mysql_url, db_name=db_name, is_mysql=True)

    return sessions

# ==============================================================================
# AUDIO, TTS & GUI NOTIFICATION ENGINE
# ==============================================================================

def play_audio_and_speak(message):
    """Runs in a background thread: plays a custom chime and speaks the message safely."""
    # 1. Play Custom High-Tech Startup Tone
    if HAS_WINSOUND:
        try:
            winsound.Beep(880, 150)  # High pitch short
            winsound.Beep(1200, 300) # Higher pitch long
        except Exception:
            pass

    # 2. Speak the text (100% Thread-Safe native OS call)
    try:
        if platform.system() == "Windows":
            # Strip single quotes to prevent breaking the PowerShell command
            safe_message = message.replace("'", "")
            
            # Use native Windows Speech Synthesis via a hidden PowerShell subprocess
            ps_script = (
                f"Add-Type -AssemblyName System.Speech; "
                f"$synth = New-Object System.Speech.Synthesis.SpeechSynthesizer; "
                f"$synth.Rate = 0; "  # Speed: -10 to 10
                f"$synth.Speak('{safe_message}');"
            )
            # CREATE_NO_WINDOW hides the console popup
            subprocess.run(
                ["powershell", "-Command", ps_script], 
                creationflags=subprocess.CREATE_NO_WINDOW
            )
        elif HAS_TTS:
            # Fallback for Mac/Linux using pyttsx3
            engine = pyttsx3.init()
            rate = engine.getProperty('rate')
            engine.setProperty('rate', rate - 20) 
            engine.say(message)
            engine.runAndWait()
    except Exception as e:
        logging.error(f"TTS Error: {e}")

def show_popup_notification(message, duration_ms=10000):
    """Creates an auto-closing, frameless GUI popup with dark mode styling."""
    root = tk.Tk()
    
    # Remove window borders for a sleek popup look
    root.overrideredirect(True)
    root.configure(bg="#12141c", highlightbackground="#00d2ff", highlightcolor="#00d2ff", highlightthickness=2)
    
    # Center the window dynamically
    window_width = 450
    window_height = 120
    screen_width = root.winfo_screenwidth()
    screen_height = root.winfo_screenheight()
    x_cordinate = int((screen_width / 2) - (window_width / 2))
    y_cordinate = int((screen_height / 2) - (window_height / 2))
    root.geometry(f"{window_width}x{window_height}+{x_cordinate}+{y_cordinate}")
    
    # UI Elements
    title_lbl = tk.Label(root, text="SYSTEM NOTIFICATION", font=("Consolas", 10, "bold"), fg="#00d2ff", bg="#12141c")
    title_lbl.pack(pady=(10, 0))
    
    msg_lbl = tk.Label(root, text=message, font=("Arial", 12, "bold"), fg="#00e676", bg="#12141c", wraplength=400, justify="center")
    msg_lbl.pack(expand=True, fill=tk.BOTH, padx=15, pady=(0, 10))

    # Fire the audio & TTS on a separate thread so the GUI isn't frozen while speaking
    threading.Thread(target=play_audio_and_speak, args=(message,), daemon=True).start()

    # Automatically destroy the window after duration_ms
    root.after(duration_ms, root.destroy)
    
    # Keep the window on top of other applications
    root.attributes('-topmost', True)
    root.mainloop()

# ==============================================================================
# ENTRY POINT / TESTING
# ==============================================================================
if __name__ == '__main__':
    logging.basicConfig(level=logging.INFO, format="%(levelname)s: %(message)s")
    db_sessions = get_database_sessions()
    
    success_message = "Database schema initialized successfully. System ready."
    print(success_message)
    
    # Trigger the GUI, Chime, and Thread-Safe TTS
    show_popup_notification(success_message, duration_ms=4500)