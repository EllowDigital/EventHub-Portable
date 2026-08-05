import tkinter as tk
import ttkbootstrap as ttk
from ttkbootstrap.constants import *
from ttkbootstrap.widgets.scrolled import ScrolledFrame

class EventHubApp(ttk.Window):
    def __init__(self):
        # Start in professional dark mode by default[cite: 4]
        super().__init__(themename="darkly") 
        self.title("Event Hub — Professional Quick Reference Handbook")
        self.geometry("1200x900")
        
        try:
            self.state('zoomed') 
        except tk.TclError:
            self.attributes('-zoomed', True)

        self.current_theme_is_dark = True

        # Build Layout Architecture
        self.build_top_nav()
        
        # Single continuous scrollable page for a "Text File / Note" feel
        self.scroll_frame = ScrolledFrame(self, autohide=True)
        self.scroll_frame.pack(fill=BOTH, expand=True, padx=20, pady=10)
        
        # Container to center the notes and restrict max width for readability
        self.document_container = ttk.Frame(self.scroll_frame)
        self.document_container.pack(fill=BOTH, expand=True, padx=50, pady=20)

        # Build Document Sections
        self.build_header_banner()
        self.build_startup_rules()
        self.build_routing_matrix()
        self.build_offline_optimizations()
        self.build_troubleshooting()
        self.build_certificate_installation()
        self.build_mysql_tuning()
        self.build_todo_checklist()

    # ---------------------------------------------------------
    # NAVIGATION & THEME
    # ---------------------------------------------------------
    def build_top_nav(self):
        """Top navigation bar for environment control"""
        nav_frame = ttk.Frame(self, padding=10, bootstyle="dark" if self.current_theme_is_dark else "light")
        nav_frame.pack(fill=X, side=TOP)

        title_lbl = ttk.Label(
            nav_frame, 
            text="⚡ EVENT HUB MISSION CONTROL", 
            font=("Segoe UI", 12, "bold")
        )
        title_lbl.pack(side=LEFT, padx=10)

        self.theme_btn = ttk.Button(
            nav_frame, 
            text="☀ Switch to Light Mode", 
            command=self.toggle_theme, 
            bootstyle="outline-light"
        )
        self.theme_btn.pack(side=RIGHT, padx=10)

    def toggle_theme(self):
        """Toggles between Darkly and Litera themes[cite: 4]"""
        if self.current_theme_is_dark:
            self.style.theme_use("litera")
            self.theme_btn.config(text="🌙 Switch to Dark Mode", bootstyle="outline-dark")
            self.current_theme_is_dark = False
        else:
            self.style.theme_use("darkly")
            self.theme_btn.config(text="☀ Switch to Light Mode", bootstyle="outline-light")
            self.current_theme_is_dark = True

    # ---------------------------------------------------------
    # HELPER COMPONENTS (For clean Notebook Design)
    # ---------------------------------------------------------
    def add_section_title(self, text, bootstyle="primary"):
        """Creates a clean section separator and title"""
        separator = ttk.Separator(self.document_container, bootstyle=bootstyle)
        separator.pack(fill=X, pady=(30, 5))
        
        lbl = ttk.Label(self.document_container, text=text, font=("Segoe UI", 14, "bold"), bootstyle=bootstyle)
        lbl.pack(anchor=NW, pady=(0, 10))

    def add_bullet_points(self, items, font=("Segoe UI", 10), bootstyle="default"):
        """Renders standard text bullet points"""
        for item in items:
            lbl = ttk.Label(self.document_container, text=f"• {item}", font=font, bootstyle=bootstyle, wraplength=1000)
            lbl.pack(anchor=NW, pady=2, padx=10)

    def create_copyable_code_block(self, text_content, height=5):
        """Creates a selectable, copy-pasteable text box for scripts and configs"""
        frame = ttk.Frame(self.document_container, padding=2, bootstyle="secondary")
        frame.pack(fill=X, padx=10, pady=5)
        
        text_widget = tk.Text(
            frame, 
            height=height, 
            font=("Consolas", 10), 
            wrap=WORD, 
            relief=FLAT,
            bg="#1e1e1e" if self.current_theme_is_dark else "#f4f4f4",
            fg="#d4d4d4" if self.current_theme_is_dark else "#000000"
        )
        text_widget.insert(END, text_content.strip())
        text_widget.configure(state=DISABLED) # Read-only but allows highlighting/copying
        text_widget.pack(fill=BOTH, expand=True, padx=5, pady=5)

    # ---------------------------------------------------------
    # DOCUMENT SECTIONS
    # ---------------------------------------------------------
    def build_header_banner(self):
        ttk.Label(
            self.document_container, 
            text="QUICK REFERENCE HANDBOOK — DIGITAL COMMAND", 
            font=("Segoe UI", 20, "bold"), 
            bootstyle="primary"
        ).pack(anchor=CENTER, pady=(0, 5))
        
        ttk.Label(
            self.document_container, 
            text="Standard Operating Procedures for Dual-Engine Architecture & Gate Operations", 
            font=("Segoe UI", 11, "italic")
        ).pack(anchor=CENTER, pady=(0, 20))

    def build_startup_rules(self):
        self.add_section_title("[1] STARTUP SEQUENCE & GOLDEN RULES", "info")
        
        ttk.Label(self.document_container, text="EXECUTION SEQUENCE:", font=("Segoe UI", 11, "bold")).pack(anchor=NW, pady=(5,2))
        steps = [
            "Connect Master Phone A (via USB Tethering) to Laptop A.",
            "Connect RJ45 LAN/Switch to Registration Laptops.",
            "Turn ON Mobile Hotspot on Laptop A.",
            "Connect Mobile Scanners (up to 8) to Laptop A's Hotspot.",
            "Launch server_hub.py -> Click '▶ Start Engine'.",
            "Start Cloudflare Tunnel (if online) -> Share public link."
        ]
        for i, s in enumerate(steps, 1):
            ttk.Label(self.document_container, text=f"{i}. {s}", font=("Segoe UI", 10)).pack(anchor=NW, padx=10, pady=1)

        ttk.Label(self.document_container, text="CRITICAL GOLDEN RULES:", font=("Segoe UI", 11, "bold"), bootstyle="warning").pack(anchor=NW, pady=(15,2))
        rules = [
            "STARTUP ORDER: Network -> Hotspot -> Server -> Clients.",
            "REGISTRATION PCs = HTTP (Port 5000).",
            "SCANNERS & GUI = HTTPS (Port 5001) -> STRICTLY REQUIRED for cameras/live-sync!",
            "SSL WARNING: Accept the 'Not Secure' warning on scanners ONCE before the event.",
            "NEVER close server_hub.py during the live event runtime.",
            "Staff never connect directly to MySQL; APIs handle database traffic."
        ]
        self.add_bullet_points(rules, bootstyle="warning")

    def build_routing_matrix(self):
        self.add_section_title("[2] DEVICE ROUTING & URL MATRIX", "primary")

        columns = ("device", "network", "target_url", "purpose")
        tree = ttk.Treeview(self.document_container, columns=columns, show="headings", bootstyle="primary", height=6)
        
        tree.heading("device", text="DEVICE")
        tree.heading("network", text="NETWORK")
        tree.heading("target_url", text="TARGET URL (ENGINE)")
        tree.heading("purpose", text="PURPOSE")
        
        tree.column("device", width=150)
        tree.column("network", width=100)
        tree.column("target_url", width=250)
        tree.column("purpose", width=300)

        data = [
            ("Kiosk Laptops", "LAN", "http://<IP>:5000 (Waitress)", "Fast data entry (No SSL lag)"),
            ("Mobile Scanners", "Wi-Fi", "https://<IP>:5001 (Cheroot)", "Unlocks iOS/Android cameras"),
            ("Master Phone A", "USB Tether", "https://<IP>:5001 (Cheroot)", "Hardwired scanner; immune to lag"),
            ("Gate Displays", "Wi-Fi/LAN", "https://<IP>:5001 (Cheroot)", "Instant GUI updates (Unbuffered)"),
            ("Roving Staff", "4G/5G", "https://<tunnel>.trycloudflare", "Secure remote scanning"),
            ("Background Sync", "USB/Wi-Fi", "(Runs Automatically)", "Cloudinary Photo API sync")
        ]

        for item in data:
            tree.insert("", END, values=item)

        tree.pack(fill=X, pady=10, padx=10)

    def build_offline_optimizations(self):
        self.add_section_title("[3] & [4] OFFLINE MODE & SYSTEM OPTIMIZATIONS", "danger")

        ttk.Label(self.document_container, text="OFFLINE PROTOCOL (INTERNET GOES DOWN):", font=("Segoe UI", 11, "bold")).pack(anchor=NW, pady=(5,2))
        self.add_bullet_points([
            "DO NOT PANIC: Local Hotspot & LAN continue working normally.",
            "Registration uses Port 5000; Scanners use Port 5001.",
            "Remote Cloudflare staff will be DOWN until internet returns."
        ])

        ttk.Label(self.document_container, text="SYSTEM & POWER OPTIMIZATIONS:", font=("Segoe UI", 11, "bold")).pack(anchor=NW, pady=(15,2))
        self.add_bullet_points([
            "NETWORK PROFILE: Must be 'Private' (Windows blocks LAN on 'Public').",
            "FIREWALL PORTS: Allow TCP 5000, 5001, 3306.",
            "ANTIVIRUS: Pause McAfee/Norton/Avast firewalls (they block local traffic).",
            "POWER PLAN: Set Screen & Sleep to 'Never'.",
            "ADAPTERS: Device Manager > Network Adapters > Uncheck 'Allow computer to turn off device'."
        ])

    def build_troubleshooting(self):
        self.add_section_title("[5] TROUBLESHOOTING & EMERGENCY FIXES", "warning")

        fixes = [
            ("» FIX: DEVICES CANNOT LOAD HUB IP", 
             "1. Check Laptop A's IP via 'ipconfig'.\n2. Verify devices are on the correct Wi-Fi/LAN.\n3. Turn OFF all VPNs (they scramble local routing).", 4),
            
            ("» FIX: CLOUDFLARE TUNNEL TIMEOUT", 
             "Change Laptop A DNS to: 1.1.1.1 (Preferred) and 1.0.0.1 (Alternate).", 2),
            
            ("» FIX: NETWORK GLITCHY / DROPPING", 
             "Admin PowerShell:\nNew-NetFirewallRule -DisplayName \"EventHub Ports\" -Direction Inbound -Action Allow -Protocol TCP -LocalPort 5000,5001\n\nAdmin CMD:\nipconfig /flushdns & ipconfig /release & ipconfig /renew & netsh winsock reset", 6),
            
            ("» MANUAL STATIC LAN FALLBACK", 
             "Server IP: 192.168.10.1 (Subnet: 255.255.255.0)\nClient IPs: 192.168.10.2+\nClient URL: http://192.168.10.1:5000", 4)
        ]

        for title, cmd, lines in fixes:
            ttk.Label(self.document_container, text=title, font=("Segoe UI", 10, "bold")).pack(anchor=NW, padx=10, pady=(10, 2))
            self.create_copyable_code_block(cmd, height=lines)

    def build_certificate_installation(self):
        self.add_section_title("[6] CERTIFICATE INSTALLATION", "info")

        ttk.Label(self.document_container, text="WINDOWS PC INSTALLATION:", font=("Segoe UI", 10, "bold")).pack(anchor=NW, padx=10, pady=(5,2))
        self.add_bullet_points([
            "Rename 'hub_cert.pem' (found in config/certs) to 'hub_cert.crt'.",
            "Double-click the file and click 'Install Certificate' -> 'Local Machine'.",
            "CRITICAL: Choose 'Place all certificates in the following store' -> Browse -> 'Trusted Root Certification Authorities'.",
            "Click Finish and completely restart your web browser."
        ])

        ttk.Label(self.document_container, text="ANDROID / SAMSUNG INSTALLATION:", font=("Segoe UI", 10, "bold")).pack(anchor=NW, padx=10, pady=(15,2))
        self.add_bullet_points([
            "Transfer certificate to phone and rename to 'hub_cert.cer'.",
            "WARNING: Do NOT open file directly from File Manager.",
            "Open Settings -> Security & Privacy -> More security settings -> Encryption & credentials.",
            "Tap 'Install a certificate' -> MUST select 'CA Certificate'.",
            "Accept privacy warning ('Install anyway') and select your 'hub_cert.cer' file."
        ])

    def build_mysql_tuning(self):
        self.add_section_title("[7] MYSQL DATABASE & NETWORK TUNING (LAN & 4GB OPTIMIZED)", "success")

        # Part 1
        ttk.Label(self.document_container, text="» 1. ENABLE LAN / RJ45 / WI-FI ACCESS (my.ini REQUIRED):", font=("Segoe UI", 10, "bold")).pack(anchor=NW, padx=10, pady=(5,2))
        self.add_bullet_points([
            "CRITICAL: Network binding cannot be changed live. It MUST be done in the file.",
            "Open: C:\\ProgramData\\MySQL\\MySQL Server 8.4\\my.ini",
            "Find the [mysqld] section and add/change this exact line (Use * to support IPv4 & IPv6 natively):",
        ])
        self.create_copyable_code_block("bind-address=*\n", height=2)
        ttk.Label(self.document_container, text="Save the file and restart MySQL Service (Admin CMD: net stop MySQL84 && net start MySQL84).", font=("Segoe UI", 10, "italic")).pack(anchor=NW, padx=25, pady=2)

        # Part 2
        ttk.Label(self.document_container, text="» 2. ADD REMOTE USER FOR LAN ACCESS (MySQL Shell):", font=("Segoe UI", 10, "bold")).pack(anchor=NW, padx=10, pady=(15,2))
        self.create_copyable_code_block(
            "CREATE USER IF NOT EXISTS 'event_admin'@'%' IDENTIFIED BY 'EventHub123!';\n"
            "GRANT ALL PRIVILEGES ON *.* TO 'event_admin'@'%';\n"
            "FLUSH PRIVILEGES;\n"
            "EXIT;", height=5)

        # Part 3
        ttk.Label(self.document_container, text="» 3. PERFORMANCE TUNING: METHOD A - PERMANENT TEXT EDIT (my.ini):", font=("Segoe UI", 10, "bold")).pack(anchor=NW, padx=10, pady=(15,2))
        my_ini_content = """# === EVENT HUB - PERFORMANCE TUNING (4GB RAM BASELINE) ===
max_connections=150
max_connect_errors=10000
wait_timeout=600
interactive_timeout=600
net_read_timeout=60
net_write_timeout=120
max_allowed_packet=64M
tmp_table_size=32M
max_heap_table_size=32M
table_open_cache=2000
thread_cache_size=50
sort_buffer_size=1M
join_buffer_size=1M
read_buffer_size=256K
read_rnd_buffer_size=256K
default-storage-engine=InnoDB
innodb_file_per_table=1
innodb_flush_method=unbuffered
innodb_flush_log_at_trx_commit=2
innodb_log_buffer_size=16M
innodb_redo_log_capacity=256M
innodb_io_capacity=500
innodb_io_capacity_max=1000
disable-log-bin
slow_query_log=1
long_query_time=2
character-set-server=utf8mb4
collation-server=utf8mb4_unicode_ci

# --- RAM BASED SETTINGS ---
# [ 4GB RAM -> 1G ]  [ 8GB RAM -> 4G ]  [ 16GB RAM -> 8G ]  [ 24GB RAM -> 12G ]
innodb_buffer_pool_size=1G
innodb_buffer_pool_instances=1"""
        self.create_copyable_code_block(my_ini_content, height=25)

        # Part 4
        ttk.Label(self.document_container, text="» 4. PERFORMANCE TUNING: METHOD B - LIVE SHELL (NO MY.INI EDITING):", font=("Segoe UI", 10, "bold")).pack(anchor=NW, padx=10, pady=(15,2))
        ttk.Label(self.document_container, text="Use 'SET PERSIST' to apply instantly without restarting. (Note: Log buffers and bind-address require Method A).", font=("Segoe UI", 9, "italic")).pack(anchor=NW, padx=10, pady=2)
        persist_content = """SET PERSIST max_connections = 150;
SET PERSIST max_connect_errors = 10000;
SET PERSIST wait_timeout = 600;
SET PERSIST interactive_timeout = 600;
SET PERSIST net_read_timeout = 60;
SET PERSIST net_write_timeout = 120;
SET PERSIST max_allowed_packet = 67108864;
SET PERSIST tmp_table_size = 33554432;
SET PERSIST max_heap_table_size = 33554432;
SET PERSIST table_open_cache = 2000;
SET PERSIST thread_cache_size = 50;
SET PERSIST sort_buffer_size = 1048576;
SET PERSIST join_buffer_size = 1048576;
SET PERSIST read_buffer_size = 262144;
SET PERSIST read_rnd_buffer_size = 262144;
SET PERSIST innodb_file_per_table = 1;
SET PERSIST innodb_flush_log_at_trx_commit = 2;
SET PERSIST innodb_redo_log_capacity = 268435456;
SET PERSIST innodb_io_capacity = 500;
SET PERSIST innodb_io_capacity_max = 1000;
SET PERSIST slow_query_log = 1;
SET PERSIST long_query_time = 2;
SET PERSIST character_set_server = 'utf8mb4';
SET PERSIST collation_server = 'utf8mb4_unicode_ci';

-- RAM Allocation (4GB Host System):
SET PERSIST innodb_buffer_pool_size = 1073741824;"""
        self.create_copyable_code_block(persist_content, height=20)

    def build_todo_checklist(self):
        self.add_section_title("[8] EVENT DAY PRE-FLIGHT CHECKLIST", "success")

        # Interactive checklist[cite: 4]
        self.status_label = ttk.Label(
            self.document_container, 
            text="⚠️ SYSTEM STATUS: PENDING PRE-FLIGHT CHECKS", 
            font=("Segoe UI", 11, "bold"), 
            bootstyle="warning"
        )
        self.status_label.pack(anchor=NW, pady=(0, 15), padx=10)

        tasks = [
            "Network Profile is set to 'Private' on Laptop A.",
            "Master Phone A is connected via USB Tethering.",
            "Registration Laptops are connected via RJ45/LAN Switch.",
            "Mobile Hotspot is turned ON (Laptop A).",
            "Scanner Phones (up to 8) are connected to Hotspot Wi-Fi.",
            "server_hub.py is launched and 'Start Engine' clicked.",
            "Cloudflare Tunnel is running (if online) and link shared.",
            "Scanner Phones have accepted the SSL 'Not Secure' warning."
        ]

        self.check_vars = []
        for task in tasks:
            var = tk.BooleanVar(value=False)
            self.check_vars.append(var)
            cb = ttk.Checkbutton(
                self.document_container, 
                text=task, 
                variable=var, 
                command=self.validate_checklist,
                bootstyle="success-round-toggle"
            )
            cb.pack(anchor=NW, pady=5, padx=15)

    def validate_checklist(self):
        """Updates the status banner based on checkboxes[cite: 4]"""
        if all(var.get() for var in self.check_vars):
            self.status_label.config(
                text="✅ ALL SYSTEMS READY: Operational status green for live event execution!", 
                bootstyle="success"
            )
        else:
            self.status_label.config(
                text="⚠️ SYSTEM STATUS: PENDING PRE-FLIGHT CHECKS", 
                bootstyle="warning"
            )

if __name__ == "__main__":
    app = EventHubApp()
    app.mainloop()