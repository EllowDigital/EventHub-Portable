# EventHub Portable

EventHub Portable is an offline-first event management and attendee check-in system. It is designed to handle attendee registration, QR code check-ins, and data synchronization between a local SQLite database and a remote MySQL server.

## 📂 Project Structure

Below is the directory structure of the project along with descriptions for each core component:

```text
├── app/
│   ├── attendee_photos/      # Downloaded attendee photos for offline check-in
│   ├── config/               # Configuration and JSON settings (sync, server, secrets)
│   ├── db/                   # Dedicated folder for local databases
│   │   └── eventhub_local.db # Local SQLite offline database
│   ├── logs/                 # Application & error logs
│   ├── sounds/               # Audio alerts for scanning (success, error, warning)
│   │   ├── error.wav
│   │   ├── success.wav
│   │   └── warn.wav
│   ├── templates/            # HTML pages served to connected devices
│   │   ├── check_in.html     # QR Check-in interface
│   │   ├── index.html        # Main home dashboard
│   │   ├── network_stats.html# Network & connected devices monitor
│   │   └── registration.html # Offline attendee registration page
│   ├── check_in.py           # QR code scanning and attendee check-in module
│   ├── explorer.py           # Search & attendee database explorer
│   ├── photo_down.py         # Script to batch-download attendee photos
│   ├── register.py           # Offline attendee registration logic
│   ├── schema.py             # SQLAlchemy models & database schema definitions
│   ├── server_hub.py         # Main Flask server, routing, and WebSocket handling
│   └── sync_manager.py       # Engine syncing data between local SQLite and remote MySQL
├── .gitignore                # Git ignore rules for logs, DBs, and secrets
├── main.py                   # Main application entry point to initialize and launch
├── mynote.txt                # Developer notes and scratchpad
├── readme.md                 # Project documentation and setup instructions
├── requirements.txt          # Python dependencies required for the project
└── test.py                   # Testing scripts
```

## 🚀 Getting Started

### Prerequisites
Ensure you have Python installed. You can install the required dependencies using the `requirements.txt` file.

### Installation
1. Clone the repository to your local machine.
2. Navigate to the project directory.
3. Install the dependencies:
   ```bash
   pip install -r requirements.txt
   ```

### Running the Application
To launch the EventHub Portable server, run the main entry point script:

```bash
python main.py
```

## ⚙️ Core Modules

* **Server Hub (`server_hub.py`):** Acts as the central Flask server managing all routes and real-time WebSocket communications.
* **Sync Manager (`sync_manager.py`):** Crucial for offline reliability. It ensures local changes in `eventhub_local.db` are synced to the master MySQL database when a network connection is available.
* **Check-In & Registration (`check_in.py`, `register.py`):** Core business logic for handling user data securely and efficiently during live events.
