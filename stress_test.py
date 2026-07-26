import asyncio
import aiohttp
import time
import random
import urllib3
import platform
from datetime import datetime

# Suppress self-signed HTTPS certificate warnings for local testing
urllib3.disable_warnings(urllib3.exceptions.InsecureRequestWarning)

# ==============================================================================
# 🎛️ EVENT CONFIGURATION
# ==============================================================================
LOCAL_URL = "http://127.0.0.1:5000"  # Fast local route
CLOUD_URL = ""                       # Leave blank to prompt at runtime

NUM_LAN_OPERATORS = 8
NUM_WAN_OPERATORS = 8
ATTENDEES_PER_OPERATOR = 10  # How many people each operator will process in this shift

# ==============================================================================
# ⏱️ ORGANIC HUMAN TIMINGS (in seconds)
# ==============================================================================
SHIFT_START_STAGGER = (0.5, 12.0)  
TYPING_SPEED = (12.0, 35.0)      
WALK_TO_GATE = (10.0, 45.0)      
SCANNER_DELAY = (3.0, 8.0)       

# ==============================================================================
# 🧬 REALISTIC DATA GENERATORS
# ==============================================================================
FIRST_NAMES = ["Amit", "Rahul", "Priya", "Neha", "Vikas", "Sanjay", "Kavita", "Ravi", "Sneha", "Anil", "Pooja", "Arjun"]
LAST_NAMES = ["Yadav", "Singh", "Sharma", "Verma", "Mishra", "Gupta", "Kumar", "Pandey", "Tiwari", "Chauhan"]
CITIES = ["Lucknow", "Kanpur", "Varanasi", "Agra", "Gorakhpur", "Noida", "Meerut", "Prayagraj", "Bareilly"]
CATEGORIES = ["TENT", "CATERING", "DECORATOR", "FLOWER", "DJ", "LIGHT", "PHOTOGRAPHY", "STAGE"]
BIZ_NAMES = ["Events", "Decorators", "Tent House", "Caterers", "Creations", "Weddings", "Productions"]

def generate_registration_payload(operator_name):
    """Generates the registration JSON expected by /api/register"""
    fname = random.choice(FIRST_NAMES)
    lname = random.choice(LAST_NAMES)
    city = random.choice(CITIES)
    mobile = f"{random.choice([6,7,8,9])}{random.randint(100000000, 999999999)}"
    
    return {
        "full_name": f"{fname} {lname}",
        "mobile": mobile,
        "email": f"{fname.lower()}.{lname.lower()}{random.randint(1,99)}@example.com",
        "gender": random.choice(["MALE", "FEMALE"]),
        "attendee_type": "BUSINESS",
        "business_name": f"{fname} {random.choice(BIZ_NAMES)}",
        "business_category": random.choice(CATEGORIES),
        "other_category": None,
        "address": f"Plot {random.randint(1,300)}, Phase {random.randint(1,4)}, {city}",
        "city": city,
        "state": "Uttar Pradesh",
        "pincode": f"22{random.randint(1000, 9999)}",
        "attendance_days": ["30 August", "31 August", "1 September"],
        "device_name": operator_name
    }

def generate_checkin_payload(attendee_id, operator_name):
    """
    🚨 FIX YOUR CHECK-IN KEYS HERE 🚨
    Match these keys EXACTLY to what your Flask /api/checkin route expects.
    Common examples: "qr_data", "id", "badge_number".
    """
    return {
        "qr_data": attendee_id,         # <--- Update this key if your backend expects something else (e.g., "attendee_id")
        "device_name": operator_name
    }

# ==============================================================================
# 👤 THE HUMAN OPERATOR (Virtual Kiosk + Scanner)
# ==============================================================================
async def human_operator(operator_id, base_url, is_cloud, stats):
    op_type = "WAN_Desk" if is_cloud else "LAN_Desk"
    operator_name = f"{op_type}_{operator_id}"
    
    # Staggered Start
    stagger_delay = random.uniform(*SHIFT_START_STAGGER)
    await asyncio.sleep(stagger_delay)
    print(f"👋 [{operator_name}] Clocked in and ready at desk (Delay: {stagger_delay:.1f}s)")
    
    connector = aiohttp.TCPConnector(force_close=False, ssl=False)
    
    async with aiohttp.ClientSession(connector=connector) as session:
        for i in range(ATTENDEES_PER_OPERATOR):
            reg_payload = generate_registration_payload(operator_name)
            attendee_name = reg_payload["full_name"]
            
            # 1. 📝 HUMAN TYPING DELAY
            await asyncio.sleep(random.uniform(*TYPING_SPEED))
            
            # 2. 💾 SUBMIT REGISTRATION
            start_time = time.time()
            attendee_id = None
            try:
                async with session.post(f"{base_url}/api/register", json=reg_payload, ssl=False, timeout=15) as res:
                    lat = int((time.time() - start_time) * 1000)
                    data = await res.json()
                    
                    # Strict validation for registration
                    if res.status == 200 and data.get("status") in ["success", "already_registered"]:
                        attendee_id = data.get("attendee_id") or data.get("id")
                        stats[operator_name]["reg_success"] += 1
                        print(f"✅ [{operator_name}] Reg: {attendee_name} ({attendee_id}) in {lat}ms")
                    else:
                        print(f"⚠️ [{operator_name}] Reg Rejected: {data.get('message', 'Unknown Error')}")
            except Exception as e:
                print(f"🔌 [{operator_name}] Conn Drop (Registration): {type(e).__name__}")

            # 3. 🚶 ATTENDEE WALKING DELAY
            if attendee_id:
                await asyncio.sleep(random.uniform(*WALK_TO_GATE))
                
                # 4. 📷 SUBMIT CHECK-IN (Scan)
                chk_payload = generate_checkin_payload(attendee_id, operator_name)
                start_time = time.time()
                try:
                    async with session.post(f"{base_url}/api/checkin", json=chk_payload, ssl=False, timeout=15) as res:
                        lat = int((time.time() - start_time) * 1000)
                        
                        # Handle potential empty or non-JSON responses gracefully
                        try:
                            data = await res.json()
                        except:
                            data = {"status": "error", "message": f"Invalid JSON response. HTTP {res.status}"}
                            
                        # STRICT VALIDATION: Only increment if the API explicitly says success
                        if res.status == 200 and data.get("status") == "success":
                            stats[operator_name]["chk_success"] += 1
                            print(f"🎫 [{operator_name}] Chk: {attendee_name} successfully in {lat}ms")
                        else:
                            print(f"⛔ [{operator_name}] Check-in Denied: {data.get('message', 'Key mismatch or logic error')}")
                except Exception as e:
                    print(f"🔌 [{operator_name}] Conn Drop (Check-in): {type(e).__name__}")
            
            # 5. ⏳ SCANNER RESET DELAY
            await asyncio.sleep(random.uniform(*SCANNER_DELAY))

    print(f"🏁 [{operator_name}] Shift complete. Handled {ATTENDEES_PER_OPERATOR} attendees.")

# ==============================================================================
# 🚀 CORE ENGINE
# ==============================================================================
async def run_simulation():
    print("=" * 85)
    print("🚶‍♂️ TDE UP 2026 — ORGANIC HUMAN CROWD SIMULATOR V3")
    print("=" * 85)
    print("⚠️  REMINDER: Ensure 'Testing Mode ON' is active in your Server Hub GUI!")
    print("=" * 85)
    
    global CLOUD_URL
    cf_input = input("\nEnter Cloudflare Tunnel URL (or press Enter to test LAN only): ").strip().rstrip('/')
    if cf_input:
        CLOUD_URL = cf_input

    total_ops = NUM_LAN_OPERATORS + (NUM_WAN_OPERATORS if CLOUD_URL else 0)
    print(f"\n[INFO] Starting shift for {total_ops} human operators...")
    print("-" * 85 + "\n")

    stats = {}
    for i in range(1, NUM_LAN_OPERATORS + 1):
        stats[f"LAN_Desk_{i}"] = {"reg_success": 0, "chk_success": 0}
    if CLOUD_URL:
        for i in range(1, NUM_WAN_OPERATORS + 1):
            stats[f"WAN_Desk_{i}"] = {"reg_success": 0, "chk_success": 0}

    start_global = time.time()
    
    tasks = []
    for i in range(1, NUM_LAN_OPERATORS + 1):
        tasks.append(asyncio.create_task(human_operator(i, LOCAL_URL, False, stats)))
        
    if CLOUD_URL:
        for i in range(1, NUM_WAN_OPERATORS + 1):
            tasks.append(asyncio.create_task(human_operator(i, CLOUD_URL, True, stats)))

    await asyncio.gather(*tasks)

    duration = time.time() - start_global
    
    print("\n" + "=" * 85)
    print("📊 SHIFT END: OPERATOR PERFORMANCE REPORT")
    print("-" * 85)
    print(f"Total Time Elapsed: {duration:.2f} seconds ({duration/60:.2f} minutes)")
    print("-" * 85)
    print(f"{'OPERATOR DESK':<18} | {'REG SUCCESS':<15} | {'CHECK-IN SUCCESS'}")
    print("-" * 85)
    
    total_reg = 0
    total_chk = 0
    
    for op in sorted(stats.keys()):
        r_succ = stats[op]["reg_success"]
        c_succ = stats[op]["chk_success"]
        total_reg += r_succ
        total_chk += c_succ
        print(f"{op:<18} | {r_succ:>5}/{ATTENDEES_PER_OPERATOR:<9} | {c_succ:>5}/{ATTENDEES_PER_OPERATOR:<9}")

    print("-" * 85)
    print(f"{'TOTALS':<18} | {total_reg:>5}/{total_ops*ATTENDEES_PER_OPERATOR:<9} | {total_chk:>5}/{total_ops*ATTENDEES_PER_OPERATOR:<9}")
    print("=" * 85)

if __name__ == "__main__":
    if platform.system() == 'Windows':
        # Safely handle deprecation warnings in newer Python versions
        try:
            asyncio.set_event_loop_policy(asyncio.WindowsSelectorEventLoopPolicy())
        except AttributeError:
            pass
            
    try:
        asyncio.run(run_simulation())
    except KeyboardInterrupt:
        print("\n\n[!] Simulation paused by event manager. Shutting down operator desks...")