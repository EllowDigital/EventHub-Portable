# 🚀 EventHub Portable

> **Offline-First Event Management & Attendee Check-In Suite**

EventHub Portable is a high-performance, resilient ecosystem engineered for seamless event operations. Built to thrive in environments with unstable or zero internet connectivity, it delivers real-time QR code scanning, rapid attendee registration, and robust bi-directional database synchronization without relying on cloud availability.

---

## 🏗️ Core Architecture & Modules

The system is built on a micro-utility architecture, modularizing distinct event operations into dedicated applications. Everything is managed centrally through an automated, zero-configuration GUI.

### 🎛️ Control & Networking

- **Central Launcher (`launcher.py` / `main.py`)**  
  The master command center. It features stealth administrator elevation, automated dependency bootstrapping, live system health diagnostics, and zero-CMD process management for completely frictionless booting.

- **Server Hub (`server_hub.py`)**  
  The networking brain of the event. It utilizes a dual-engine architecture:
  - **Waitress (HTTP):** Provides a zero-encryption-lag engine for high-speed local data entry.
  - **Cheroot (HTTPS):** Provides the secure SSL layer strictly required to unlock iOS/Android hardware cameras and bypasses HTTP stream-buffering for instant GUI updates.
  - _Features:_ Local API routing, real-time socket tracking, mobile endpoints, and live network telemetry.

### 🔄 Data & Asset Synchronization

- **Sync Manager (`sync_manager.py`)**  
  A multi-tier synchronization engine. It manages data flows between the offline SQLite mirror (`eventhub_local.db`), the local MySQL hub, and remote cloud databases (like Supabase), complete with automated conflict resolution.

- **Photo Engine (`photo_down.py`)**  
  A smart, background asset downloader. It caches attendee profile photos locally and links them directly to database rows, ensuring images load instantly offline while aggressively saving external API credits.

### 💻 Client Interfaces & Dashboards

- **Attendee Explorer (`explorer.py`)**  
  A lag-free administration dashboard. It features dual connection modes (Direct MySQL vs. Portable Hub API), real-time ticket analytics, multi-criteria filtering, and CSV data exports.

- **Gate Display & Kiosks (`check_in.py` / `register.py`)**  
  Dedicated physical endpoints. `check_in.py` provides high-speed gate scanning with instant visual and audio feedback triggers, while `register.py` powers walk-in registration desks.

---

## 📂 Project Structure

```text
EventHub/
├── app/
│   ├── attendee_photos/      # Local image cache for instant offline profile loading
│   ├── config/               # JSON configurations (schema, secrets, explorer prefs)
│   ├── db/                   # Local database instances (eventhub_local.db)
│   ├── logs/                 # Rolling execution, error, and network telemetry logs
│   ├── templates/            # Responsive web portals for remote client devices
│   │   ├── check_in.html     # Secure web-based gate scanner feed (Requires HTTPS)
│   │   ├── index.html        # Client navigation dashboard
│   │   ├── network_stats.html# Node traffic & active device monitor
│   │   └── registration.html # High-speed kiosk registration form (HTTP optimized)
│   │
│   ├── check_in.py           # Gate scanning GUI terminal logic
│   ├── explorer.py           # Profile inspection & multi-criteria filtering engine
│   ├── photo_down.py         # Automated cloud-to-local asset synchronizer
│   ├── register.py           # Walk-in registration service and data entry
│   ├── schema.py             # SQLAlchemy models & database table definitions
│   ├── server_hub.py         # Central Flask API & network event broadcaster
│   └── sync_manager.py       # Multi-tier cloud/local synchronization engine
│
├── .gitignore                # Version control exclusions
├── launcher.py               # Master GUI control panel & background process manager
├── main.py                   # Primary application entry point and bootstrapper
├── requirements.txt          # Python runtime dependencies
└── stress_test.py            # Local performance & load benchmarking scripts
```
