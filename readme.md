# EventHub Portable
**Offline-First Event Management & Attendee Check-In Suite**

EventHub Portable is a high-performance, robust offline ecosystem engineered for seamless event operations, real-time QR code check-ins, attendee registration, and bi-directional database synchronization. It is optimized to operate smoothly even with zero or unstable internet connectivity.

---

## 🏗️ Core Architecture & Modules

The system is split into modular, dedicated micro-utilities managed centrally through an automated GUI launcher:

* **Central Launcher (`launcher.py` / `main.py`):** Acts as the master command center with stealth administrator elevation, automated dependency bootstrapping, live system health diagnostics, and zero-CMD process management.
* **Server Hub (`server_hub.py`):** A high-performance Flask + Waitress/Cheroot engine managing local API routing, real-time socket tracking, mobile registration endpoints, and network telemetry.
* **Sync Manager (`sync_manager.py`):** Handles robust synchronization between the local offline SQLite database (`eventhub_local.db`), local MySQL hub, and remote cloud databases (Supabase), including automated conflict resolution.
* **Attendee Explorer (`explorer.py`):** A lag-free management dashboard supporting dual connection modes (Direct MySQL vs. Portable Hub API), real-time analytics by ticket type, multi-criteria sorting, and CSV data export.
* **Gate Display & Kiosks (`check_in.py`, `register.py`):** Dedicated interfaces for high-speed gate scanning, audio feedback triggers, and walk-in registration desks.
* **Photo Engine (`photo_down.py`):** Smart local asset downloader that automatically caches attendee photos locally and links them to database rows while saving Cloudinary API credits.

---

## 📂 Project Structure

```text
├── app/
│   ├── attendee_photos/      # Local image assets for offline profiles
│   ├── config/               # JSON configurations (schema.json, secrets.json, explorer.json)
│   ├── db/                   # Local database instances (eventhub_local.db)
│   ├── logs/                 # Rolling execution and error logs
│   ├── sounds/               # Audio alerts for scan events (success, warning, error)
│   ├── templates/            # Responsive web portals for remote client devices
│   │   ├── check_in.html     # Gate scanner feed
│   │   ├── index.html        # Client dashboard
│   │   ├── network_stats.html# Node traffic & active devices monitor
│   │   └── registration.html # Kiosk registration form
│   ├── check_in.py           # Gate scanning terminal logic
│   ├── explorer.py           # Profile inspection & filtering engine
│   ├── photo_down.py         # Cloud asset synchronizer
│   ├── register.py           # Walk-in registration service
│   ├── schema.py             # SQLAlchemy models & database definitions
│   ├── server_hub.py         # Central Flask API & network broadcaster
│   └── sync_manager.py       # Multi-tier synchronization engine
├── .gitignore                # Version control exclusions
├── launcher.py               # Master GUI control panel & process manager
├── main.py                   # Primary application entry point
├── requirements.txt          # Python runtime dependencies
└── stress_test.py            # Performance benchmarking scripts