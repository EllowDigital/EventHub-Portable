import os
import json
import enum
import logging
from datetime import datetime
import pymysql

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
        # Using engine.begin() ensures an auto-committing transaction block
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
        
        # Use engine.begin() for safe schema altering (rolls back on failure)
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
        # Robust Pooling: 
        # pool_recycle drops connections before MySQL's default 8-hour timeout kills them.
        # pool_pre_ping tests the socket before every single query to ensure zero dead connections.
        engine = create_engine(
            db_url, 
            echo=False, 
            pool_size=25, 
            max_overflow=50, 
            pool_recycle=1800, 
            pool_pre_ping=True
        )
    else:
        # SQLite needs check_same_thread=False to support 150+ concurrent Flask Waitress threads
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

    # Initialize SQLite Server
    if config.get("sqlite", {}).get("enabled", False):
        sq_config = config["sqlite"]
        db_folder = os.path.join(BASE_DIR, sq_config["folder_name"])
        os.makedirs(db_folder, exist_ok=True)
        
        sqlite_path = os.path.join(db_folder, sq_config["file_name"])
        sqlite_url = f"sqlite:///{sqlite_path}"
        
        logging.info(f"Initializing Local SQLite Database at {sqlite_path}...")
        sessions["sqlite"] = init_database(sqlite_url)

    # Initialize MySQL Server
    if config.get("mysql", {}).get("enabled", False):
        my_config = config["mysql"]
        db_name = my_config["database"]
        mysql_url = f"mysql+mysqldb://{my_config['user']}:{my_config['password']}@{my_config['host']}:{my_config['port']}/{db_name}"
        
        logging.info(f"Initializing MySQL Hub Database ({db_name})...")
        sessions["mysql"] = init_database(mysql_url, db_name=db_name, is_mysql=True)

    return sessions

# ==============================================================================
# ENTRY POINT / TESTING
# ==============================================================================
if __name__ == '__main__':
    logging.basicConfig(level=logging.INFO, format="%(levelname)s: %(message)s")
    db_sessions = get_database_sessions()
    print("Database robust initialization script completed successfully.")