import uuid
import json
import copy
from datetime import datetime, timezone

# IMPORT THIS to fix the empty JSON save bug!
from sqlalchemy.orm.attributes import flag_modified 

# Import your database session and models
try:
    from app.schema import Attendee, OfflineKioskAttendee, get_database_sessions, GenderEnum, AttendeeTypeEnum
except ModuleNotFoundError:
    from schema import Attendee, OfflineKioskAttendee, get_database_sessions, GenderEnum, AttendeeTypeEnum

def register_offline_user(session):
    """Simulates a user walking up to the offline kiosk and registering."""
    print("\n--- 1. Simulating Offline Kiosk Registration ---")
    
    test_mobile = "9998887776"
    
    # 🛑 THE SAFEGUARD: Check if this mobile number already exists!
    existing_user = session.query(OfflineKioskAttendee).filter_by(mobile=test_mobile).first()
    
    if existing_user:
        print(f"⚠️ User with mobile {test_mobile} is already registered!")
        print(f"   Reusing existing ID: {existing_user.attendee_id}")
        return existing_user.attendee_id  # Skip creation, just return their existing ID

    # If they don't exist, create a new one safely
    new_user = OfflineKioskAttendee(
        id=str(uuid.uuid4()),
        attendee_id=f"KIO-{int(datetime.now().timestamp())}", 
        full_name="Rahul Offline Test",
        mobile=test_mobile,
        email="rahul.test@example.com",
        gender=GenderEnum.MALE if hasattr(GenderEnum, 'MALE') else 'MALE',
        attendee_type=AttendeeTypeEnum.GENERAL if hasattr(AttendeeTypeEnum, 'GENERAL') else 'GENERAL',
        address="123 Offline Street",
        city="Lucknow",
        state="Uttar Pradesh",
        pincode="226001",
        attendance_days=["2026-08-30", "2026-08-31", "2026-09-01"],
        checkin_history={},
        needs_cloud_sync=True, 
        created_at=datetime.now(timezone.utc),
        updated_at=datetime.now(timezone.utc)
    )
    
    session.add(new_user)
    session.commit()
    print(f"✅ Success! User '{new_user.full_name}' registered locally with ID: {new_user.attendee_id}")
    return new_user.attendee_id

def simulate_offline_checkin(session, attendee_id_to_checkin, day):
    """Simulates scanning a user's QR code at the gate with no internet."""
    print(f"\n--- Simulating Offline Check-In for Day: {day} ---")
    
    # 1. Search main table first, then offline table
    user = session.query(Attendee).filter_by(attendee_id=attendee_id_to_checkin).first()
    if not user:
        user = session.query(OfflineKioskAttendee).filter_by(attendee_id=attendee_id_to_checkin).first()
        
    if not user:
        print("❌ Error: User not found in local database!")
        return

    # 2. Parse existing check-in history safely and DEEP COPY it
    history = user.checkin_history
    if isinstance(history, str):
        history = json.loads(history) if history else {}
    elif history is None:
        history = {}
    else:
        history = copy.deepcopy(history) # Copy prevents SQLAlchemy mutation ignorance

    # 3. Add the check-in record as a dictionary object
    history[day] = {
        "status": "CHECKED_IN",
        "timestamp": datetime.now(timezone.utc).isoformat(),
        "device": "Gate-Scanner-1"
    }

    # 4. Save and force sync flag
    user.checkin_history = history
    
    # CRITICAL FIX: Tell SQLAlchemy explicitly that the JSON column changed
    flag_modified(user, "checkin_history") 
    
    user.needs_cloud_sync = True 
    user.updated_at = datetime.now(timezone.utc)
    
    session.commit()
    print(f"✅ Success! {user.full_name} checked in.")
    print(f"   Updated History Payload: {json.dumps(user.checkin_history, indent=2)}")

if __name__ == "__main__":
    sessions = get_database_sessions()
    mysql_session_maker = sessions.get('mysql')
    
    if not mysql_session_maker:
        print("❌ Could not connect to local MySQL database. Check your schema config.")
    else:
        session = mysql_session_maker()
        try:
            # STEP 1: Register an offline user
            generated_id = register_offline_user(session)
            
            # STEP 2: Check-in on Day 1
            simulate_offline_checkin(session, generated_id, day="2026-08-30")
            
            # STEP 3: Check-in on Day 2
            simulate_offline_checkin(session, generated_id, day="2026-08-31")
            
            # STEP 4: Check-in on Day 3
            simulate_offline_checkin(session, generated_id, day="2026-09-01")
            
            print("\n🎉 Test script complete! Open your Sync Dashboard and click 'Push'.")
        finally:
            session.close()