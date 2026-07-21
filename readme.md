EVENTHUB-PORTABLE/
│
├── app/
│   ├── attendee_photos/      # Downloaded attendee photos for offline check-in
│   │
│   ├── config/               # Configuration and JSON settings
│   │   ├── checkin.json      # Check-in configuration
│   │   ├── register.json     # Registration configuration
│   │   ├── secrets.json      # API keys & database credentials (gitignored)
│   │   ├── server_hub.json   # Server settings (IP, port, etc.)
│   │   └── sync.json         # Sync configuration
│   │
│   ├── backups/              # Automatic database backups
│   ├── exports/              # Exported Excel/CSV/PDF files
│   ├── eventhub_local.db     # Local SQLite offline database
│   ├── logs/                 # Application & error logs
│   │
│   ├── templates/            # HTML pages served to mobile devices
│   │   ├── home.html         # Home dashboard
│   │   ├── register.html     # Registration page
│   │   ├── check_in.html     # QR Check-in page
│   │   └── network.html      # Network & connected devices
│   │
│   ├── schema.py             # SQLAlchemy models & database schema
│   ├── server_hub.py         # Main Flask server, routing, and WebSocket handling
│   ├── register.py           # Offline attendee registration logic
│   ├── check_in.py           # QR code scanning and attendee check-in module
│   ├── explorer.py           # Search & attendee database explorer
│   ├── photo_down.py         # Script to batch-download attendee photos
│   ├── sync_manager.py       # Engine syncing SQLite ↔ MySQL
│   └── utils.py              # Shared helper functions
│
├── main.py                   # Application entry point to initialize and launch
├── requirements.txt          # Python dependencies
├── README.md                 # Project documentation and setup instructions
└── .gitignore                # Git ignore rules for logs, DBs, and secrets