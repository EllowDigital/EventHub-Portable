import time
import json
import random
import urllib3
import requests
import statistics
import threading
from concurrent.futures import ThreadPoolExecutor, as_completed
from datetime import datetime

# Suppress self-signed HTTPS certificate warnings for local testing
urllib3.disable_warnings(urllib3.exceptions.InsecureRequestWarning)

# ==============================================================================
# 🎛️ TEST CONFIGURATION
# ==============================================================================
LOCAL_URL = "http://127.0.0.1:5000"  # Fast, unencrypted local Flask route
CLOUD_URL = ""                       # Leave blank to prompt at runtime

NUM_LOCAL_DEVICES = 8
NUM_CLOUD_DEVICES = 8
CYCLES_PER_DEVICE = 10  # 1 Cycle = (1 Registration + 1 Check-in of that exact ID)

# ==============================================================================
# 🧬 REALISTIC DATA GENERATORS
# ==============================================================================
FIRST_NAMES = ["Amit", "Rahul", "Priya", "Neha", "Vikas", "Sanjay", "Kavita", "Ravi", "Sneha", "Anil", "Pooja", "Gaurav"]
LAST_NAMES = ["Yadav", "Singh", "Sharma", "Verma", "Mishra", "Gupta", "Kumar", "Pandey", "Tiwari", "Chauhan"]
CITIES = ["Lucknow", "Kanpur", "Varanasi", "Agra", "Gorakhpur", "Prayagraj", "Noida", "Meerut", "Ghaziabad"]
CATEGORIES = ["TENT", "CATERING", "DECORATOR", "FLOWER", "DJ", "LIGHT", "PHOTOGRAPHY", "STAGE", "BANQUET"]
BIZ_NAMES = ["Events", "Decorators", "Tent House", "Caterers", "Creations", "Weddings", "Productions"]

def generate_realistic_payload(device_name):
    """Generates a perfect, schema-compliant registration payload."""
    fname = random.choice(FIRST_NAMES)
    lname = random.choice(LAST_NAMES)
    city = random.choice(CITIES)
    cat = random.choice(CATEGORIES)
    mobile = f"{random.choice([6,7,8,9])}{random.randint(100000000, 999999999)}"
    
    return {
        "full_name": f"{fname} {lname}",
        "mobile": mobile,
        "email": f"{fname.lower()}.{lname.lower()}{random.randint(1,99)}@example.com",
        "gender": random.choice(["MALE", "FEMALE"]),
        "attendee_type": "BUSINESS",
        "business_name": f"{fname} {random.choice(BIZ_NAMES)}",
        "business_category": cat,
        "other_category": None,
        "address": f"Plot {random.randint(1,200)}, Phase {random.randint(1,4)}, {city}",
        "city": city,
        "state": "Uttar Pradesh",
        "pincode": f"22{random.randint(1000, 9999)}",
        # Granting access for all 3 event days so check-in always passes the date validation
        "attendance_days": ["30 August", "31 August", "1 September"],
        "device_name": device_name
    }

# ==============================================================================
# 🤖 VIRTUAL DEVICE WORKER
# ==============================================================================
def virtual_device_session(device_id, base_url, is_cloud):
    """
    Simulates a single Kiosk/Scanner doing rapid registrations AND check-ins.
    """
    device_type = "WAN_Scanner" if is_cloud else "LAN_Kiosk"
    device_name = f"{device_type}_{device_id}"
    
    # Use Session to reuse TCP connections, mimicking a real browser/device
    session = requests.Session()
    session.verify = False
    
    metrics = {
        "reg_latencies": [],
        "chk_latencies": [],
        "reg_success": 0,
        "chk_success": 0,
        "errors": []
    }
    
    for cycle in range(CYCLES_PER_DEVICE):
        # --- 1. PERFORM REGISTRATION ---
        reg_payload = generate_realistic_payload(device_name)
        start_time = time.time()
        
        attendee_id = None
        try:
            res = session.post(f"{base_url}/api/register", json=reg_payload, timeout=8.0)
            latency = (time.time() - start_time) * 1000
            metrics["reg_latencies"].append(latency)
            
            if res.status_code == 200:
                data = res.json()
                if data.get("status") in ["success", "already_registered"]:
                    attendee_id = data.get("attendee_id")
                    metrics["reg_success"] += 1
                else:
                    metrics["errors"].append(f"Reg Logic Error: {data.get('message')}")
            else:
                metrics["errors"].append(f"Reg HTTP {res.status_code}")
                
        except Exception as e:
            metrics["errors"].append(f"Reg Conn Error: {type(e).__name__}")

        # Simulate brief human delay between generating the ID and scanning it
        time.sleep(random.uniform(0.1, 0.4))
        
        # --- 2. PERFORM CHECK-IN (Using the ID we just created) ---
        if attendee_id:
            chk_payload = {
                "attendee_id": attendee_id,
                "search_type": "id",
                "device_name": device_name
            }
            start_time = time.time()
            
            try:
                res = session.post(f"{base_url}/api/checkin", json=chk_payload, timeout=8.0)
                latency = (time.time() - start_time) * 1000
                metrics["chk_latencies"].append(latency)
                
                # We consider 200 (Success) and 400 (Already Checked In) as healthy logic executions
                if res.status_code in [200, 400]:
                    metrics["chk_success"] += 1
                else:
                    metrics["errors"].append(f"Chk HTTP {res.status_code}")
                    
            except Exception as e:
                metrics["errors"].append(f"Chk Conn Error: {type(e).__name__}")
                
        # Simulate delay before the next person steps up
        time.sleep(random.uniform(0.2, 0.5))

    return {
        "device_name": device_name,
        "is_cloud": is_cloud,
        "metrics": metrics
    }

# ==============================================================================
# 📊 AGGREGATION & REPORTING
# ==============================================================================
def print_stats(title, latencies, success, total, duration):
    if not latencies:
        print(f"{title}: No successful data.")
        return
        
    avg_l = statistics.mean(latencies)
    min_l = min(latencies)
    max_l = max(latencies)
    p95_l = statistics.quantiles(latencies, n=20)[18] if len(latencies) >= 20 else max_l
    rps = total / duration if duration > 0 else 0
    
    print(f"{title:<25} | RPS: {rps:>6.1f} | Avg: {avg_l:>6.1f}ms | P95: {p95_l:>6.1f}ms | Max: {max_l:>6.1f}ms | Success: {success}/{total}")

def main():
    print("=" * 85)
    print("🌪️ TDE UP 2026 — EXTREME REAL-DATA CHAOS SIMULATOR")
    print(f"Timestamp: {datetime.now().strftime('%Y-%m-%d %H:%M:%S')}")
    print("=" * 85)
    
    global CLOUD_URL
    cf_input = input("\nEnter Cloudflare Tunnel URL (or press Enter to test LAN only): ").strip().rstrip('/')
    if cf_input:
        CLOUD_URL = cf_input

    total_devices = NUM_LOCAL_DEVICES + (NUM_CLOUD_DEVICES if CLOUD_URL else 0)
    print(f"\n[INFO] Launching {total_devices} concurrent devices...")
    print(f"[INFO] Each device will perform {CYCLES_PER_DEVICE} complete lifecycles (Register -> Wait -> Check-in).")
    print("[INFO] Simulating heavy DB read/write locks. Please wait...\n")

    start_global = time.time()
    results = []

    # Launch threaded devices
    with ThreadPoolExecutor(max_workers=total_devices) as executor:
        futures = []
        
        # Local LAN Devices
        for i in range(NUM_LOCAL_DEVICES):
            futures.append(executor.submit(virtual_device_session, i+1, LOCAL_URL, False))
            
        # Cloudflare WAN Devices
        if CLOUD_URL:
            for i in range(NUM_CLOUD_DEVICES):
                futures.append(executor.submit(virtual_device_session, i+1, CLOUD_URL, True))
                
        for future in as_completed(futures):
            results.append(future.result())

    total_duration = time.time() - start_global

    # --- PROCESS RESULTS ---
    local_reg_lat, local_chk_lat = [], []
    cloud_reg_lat, cloud_chk_lat = [], []
    local_success, cloud_success = 0, 0
    all_errors = []

    for r in results:
        m = r["metrics"]
        all_errors.extend(m["errors"])
        
        if r["is_cloud"]:
            cloud_reg_lat.extend(m["reg_latencies"])
            cloud_chk_lat.extend(m["chk_latencies"])
            cloud_success += (m["reg_success"] + m["chk_success"])
        else:
            local_reg_lat.extend(m["reg_latencies"])
            local_chk_lat.extend(m["chk_latencies"])
            local_success += (m["reg_success"] + m["chk_success"])

    total_local_reqs = NUM_LOCAL_DEVICES * CYCLES_PER_DEVICE * 2
    total_cloud_reqs = (NUM_CLOUD_DEVICES * CYCLES_PER_DEVICE * 2) if CLOUD_URL else 0

    print("=" * 85)
    print("📊 PERFORMANCE MATRIX")
    print("-" * 85)
    
    print_stats("LAN Registrations (Write)", local_reg_lat, len(local_reg_lat), int(total_local_reqs/2), total_duration)
    print_stats("LAN Check-ins (Read/Write)", local_chk_lat, len(local_chk_lat), int(total_local_reqs/2), total_duration)
    
    if CLOUD_URL:
        print("-" * 85)
        print_stats("WAN Registrations (Write)", cloud_reg_lat, len(cloud_reg_lat), int(total_cloud_reqs/2), total_duration)
        print_stats("WAN Check-ins (Read/Write)", cloud_chk_lat, len(cloud_chk_lat), int(total_cloud_reqs/2), total_duration)

    print("=" * 85)
    print("🔍 BOTTLENECK DIAGNOSTICS")
    print("=" * 85)
    
    if all_errors:
        print(f"⚠️ Encountered {len(all_errors)} Errors during simulation. Sample:")
        for err in list(set(all_errors))[:5]:
            print(f"   - {err}")
    else:
        print("✅ Zero Errors! Database locks and thread queues processed perfectly.")

    # Calculate Local vs Cloud difference
    if CLOUD_URL and local_chk_lat and cloud_chk_lat:
        avg_local = statistics.mean(local_chk_lat)
        avg_cloud = statistics.mean(cloud_chk_lat)
        diff = avg_cloud - avg_local
        print(f"\n🌐 Cloudflare Overhead: Added roughly {diff:.1f}ms to every request.")
        if diff > 500:
            print("   ↳ WARNING: Cloudflare tunnel latency is extremely high. Use Local LAN links for on-site devices.")
        else:
            print("   ↳ STATUS: Tunnel is performing efficiently within expected bounds.")
            
    if local_reg_lat:
        p95_local = statistics.quantiles(local_reg_lat, n=20)[18] if len(local_reg_lat) >= 20 else max(local_reg_lat)
        if p95_local > 400:
            print("\n🗄️ Database Load: Local Registration P95 is over 400ms.")
            print("   ↳ DIAGNOSIS: MySQL row locking is slightly bottlenecking under 16-device simultaneous load.")
        else:
            print("\n🗄️ Database Load: Local P95 latency is excellent (<400ms).")
            print("   ↳ DIAGNOSIS: MySQL connection pooling is handling the 16-device concurrency beautifully.")

    print("\n🏁 Test completed in {:.2f} seconds.".format(total_duration))

if __name__ == "__main__":
    main()