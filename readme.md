<div align="center">

# 🚀 EventHub Portable

**Offline-First Event Management & Attendee Check-In Suite**

[![Status](https://img.shields.io/badge/Status-Active-success?style=for-the-badge)](#)
[![Environment](https://img.shields.io/badge/Environment-Offline--First-blue?style=for-the-badge)](#)
[![Architecture](https://img.shields.io/badge/Architecture-Modular-orange?style=for-the-badge)](#)

<p align="center">
  <em>A high-performance, resilient ecosystem engineered for seamless event operations. Built to thrive in environments with unstable or zero internet connectivity, delivering real-time QR code scanning, rapid registration, and robust bi-directional database synchronization without relying on cloud availability.</em>
</p>

</div>

---

## 🏗️ Core Architecture & Modules

EventHub is built on a micro-utility architecture, modularizing distinct operations into dedicated applications. Managed centrally through an automated, zero-configuration GUI.

| Module | Component | Description |
| :--- | :--- | :--- |
| 🎛️ **Control** | **Main Entry (`main.py`)** | The master command center. Handles automated dependency bootstrapping, system health diagnostics, and completely frictionless booting. |
| 🌐 **Network** | **Server Hub (`server_hub.py`)** | Dual-engine networking: **Waitress (HTTP)** for zero-lag local data entry, and **Cheroot (HTTPS)** to securely unlock iOS/Android cameras. |
| 🔄 **Sync** | **Sync Manager (`sync_manager.py`)** | Multi-tier sync engine managing flows between the offline SQLite mirror, local MySQL hub, and remote cloud databases (with conflict resolution). |
| 🖼️ **Assets** | **Photo Engine (`photo_down.py`)** | Background asset downloader caching attendee photos locally. Ensures instant offline loads and saves external API credits. |
| 📊 **Admin** | **Explorer (`explorer.py`)** | Lag-free dashboard with dual connection modes, real-time ticket analytics, multi-criteria filtering, and CSV exports. |
| 🎫 **Kiosks** | **Gate & Desk Interfaces** | `check_in.py` for high-speed gate scanning (visual/audio triggers). `register.py` for powering walk-in registration desks. |

---

## 📂 Project Structure

<details open>
<summary><b>Click to collapse/expand directory tree</b></summary>
<br>

```text
EventHub-Portable/
├── app/
│   ├── assets/                 # 🖼️ Visual assets and branding
│   │   ├── main-banner's/      # Application banner graphics
│   │   ├── EventHub.ico        # Window icon file
│   │   └── EventHub.png        # Transparent logo asset
│   │
│   ├── attendee_photos/        # 👤 Local image cache for instant offline profile loading
│   │
│   ├── config/                 # ⚙️ JSON configurations and certificates
│   │   ├── certs/              # SSL certificates required for HTTPS camera access
│   │   ├── checkin.json        # Gate scanner preferences
│   │   ├── conflicts.json      # Sync collision logs and rules
│   │   ├── device_names.json   # Hardware node identifiers
│   │   ├── explorer.json       # Admin dashboard view states
│   │   ├── register.json       # Walk-in desk configurations
│   │   ├── schema.json         # Database structure definitions
│   │   ├── secrets.json        # Encrypted tokens and API keys
│   │   └── sync_state.json     # Timestamp tracking for cloud synchronization
│   │
│   ├── db/                     # 🗄️ Local database instances
│   │   └── eventhub_local.db   # Primary offline SQLite mirror
│   │
│   ├── logs/                   # 📝 Rolling execution, error, and network telemetry logs
│   │
│   ├── static/                 # 🌐 PWA and static web assets
│   │   ├── favicon/            # Web app icons
│   │   ├── site.webmanifest    # Progressive Web App metadata
│   │   └── sw.js               # Service Worker for offline web caching
│   │
│   ├── templates/              # 🖥️ Responsive web portals for client devices
│   │   ├── check_in.html       # Secure web-based gate scanner feed (Requires HTTPS)
│   │   ├── index.html          # Client navigation dashboard
│   │   ├── network_stats.html  # Node traffic & active device monitor
│   │   └── registration.html   # High-speed kiosk registration form (HTTP optimized)
│   │
│   ├── check_in.py             # Gate scanning GUI terminal logic
│   ├── explorer.py             # Profile inspection & multi-criteria filtering engine
│   ├── handbook.py             # Interactive operational documentation and guides
│   ├── photo_down.py           # Automated cloud-to-local asset synchronizer
│   ├── register.py             # Walk-in registration service and data entry
│   ├── schema.py               # SQLAlchemy models & database table definitions
│   ├── server_hub.py           # Central Flask API & network event broadcaster
│   ├── stress_test.py          # Local performance & load benchmarking scripts
│   └── sync_manager.py         # Multi-tier cloud/local synchronization engine
│
├── .gitignore                  # Git tracking exclusions
├── main.py                     # 🚀 Primary application entry point and master bootstrapper
├── mynote.txt                  # Local developer notes and scratchpad
├── readme.md                   # Project documentation
└── requirements.txt            # Python runtime dependencies