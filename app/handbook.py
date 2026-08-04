import tkinter as tk
import ttkbootstrap as ttk
from ttkbootstrap.constants import *
from ttkbootstrap.widgets.scrolled import ScrolledFrame

class EventHubApp(ttk.Window):
    def __init__(self):
        # Start in professional dark mode by default (darkly), togglable to litera (light)
        super().__init__(themename="darkly") 
        self.title("Event Hub — Professional Quick Reference Handbook")
        self.geometry("1100x950")
        
        try:
            self.state('zoomed') 
        except tk.TclError:
            self.attributes('-zoomed', True)

        self.current_theme_is_dark = True
        self.wrap_labels = []

        # Top Control Navigation Bar (Theme Toggle & Header)
        self.build_top_nav()

        # Main Scrollable A4 Document Container
        self.main_scroll = ScrolledFrame(self, autohide=True)
        self.main_scroll.pack(fill=BOTH, expand=True, padx=15, pady=10)

        # Page Frame wrapper
        self.page = ttk.Frame(self.main_scroll, padding=30)
        self.page.pack(fill=BOTH, expand=True)

        # Build Document Sections
        self.build_header_banner()
        self.build_todo_checklist()
        self.build_error_codes()
        self.build_startup_rules()
        self.build_routing_matrix()
        self.build_offline_optimizations()
        self.build_troubleshooting()
        self.build_certificate_installation()

        # Bind window resizing to auto-wrap text perfectly
        self.main_scroll.bind("<Configure>", self.on_window_resize)

    def build_top_nav(self):
        """Top navigation bar for environment control (Theme switching)"""
        nav_frame = ttk.Frame(self, padding=10, bootstyle="dark" if self.current_theme_is_dark else "light")
        nav_frame.pack(fill=X, side=TOP)

        title_lbl = ttk.Label(
            nav_frame, 
            text="⚡ EVENT HUB MISSION CONTROL", 
            font=("Helvetica", 11, "bold")
        )
        title_lbl.pack(side=LEFT, padx=10)

        # Theme Switcher Button
        self.theme_btn = ttk.Button(
            nav_frame, 
            text="☀ Switch to Sunlight (Light) Mode", 
            command=self.toggle_theme, 
            bootstyle="outline-light"
        )
        self.theme_btn.pack(side=RIGHT, padx=10)

    def toggle_theme(self):
        """Switches themes seamlessly between Sunlight (Light) and Dark Room modes"""
        if self.current_theme_is_dark:
            self.style.theme_use("litera")
            self.theme_btn.config(text="🌙 Switch to Dark Room Mode", bootstyle="outline-dark")
            self.current_theme_is_dark = False
        else:
            self.style.theme_use("darkly")
            self.theme_btn.config(text="☀ Switch to Sunlight (Light) Mode", bootstyle="outline-light")
            self.current_theme_is_dark = True

    def on_window_resize(self, event):
        """Adjusts text wrapping dynamically based on monitor/window width"""
        wrap_width = event.width - 120
        if wrap_width > 200:
            for label in self.wrap_labels:
                label.configure(wraplength=wrap_width)

    def add_responsive_label(self, parent, text, font=("Helvetica", 9), bootstyle="default", pady=2, padx=5, bullet=False):
        if bullet and not text.startswith("•"):
            text = "• " + text
        lbl = ttk.Label(parent, text=text, font=font, bootstyle=bootstyle)
        lbl.pack(anchor=NW, pady=pady, padx=padx, fill=X)
        self.wrap_labels.append(lbl)
        return lbl

    def build_header_banner(self):
        header_card = ttk.Labelframe(self.page, text=" MANUAL SPECIFICATION ", padding=15, bootstyle="primary")
        header_card.pack(fill=X, pady=(0, 15))
        
        ttk.Label(
            header_card, 
            text="QUICK REFERENCE HANDBOOK — DIGITAL COMMAND", 
            font=("Helvetica", 15, "bold"), 
            bootstyle="primary"
        ).pack(anchor=CENTER)
        ttk.Label(
            header_card, 
            text="Standard Operating Procedures for Dual-Engine Architecture & Gate Operations[cite: 1]", 
            font=("Helvetica", 9, "italic")
        ).pack(anchor=CENTER, pady=(2, 0))

    def build_todo_checklist(self):
        card = ttk.Labelframe(self.page, text=" [0] EVENT DAY PRE-FLIGHT CHECKLIST ", padding=15, bootstyle="success")
        card.pack(fill=X, pady=10)

        self.status_label = ttk.Label(
            card, 
            text="⚠️ SYSTEM STATUS: PENDING PRE-FLIGHT CHECKS", 
            font=("Helvetica", 10, "bold"), 
            bootstyle="warning"
        )
        self.status_label.pack(anchor=NW, pady=(0, 10), padx=5)

        tasks = [
            "Network Profile is set to 'Private' on Laptop A[cite: 1].",
            "Phone A is connected via USB Tethering[cite: 1].",
            "Registration Laptops are connected via RJ45/LAN Switch[cite: 1].",
            "Mobile Hotspot is turned ON (Laptop A)[cite: 1].",
            "Scanner Phones (up to 8) are connected to Hotspot Wi-Fi[cite: 1].",
            "server_hub.py is launched and 'Start Engine' clicked[cite: 1].",
            "Cloudflare Tunnel is running (if online) and link shared[cite: 1].",
            "Scanner Phones have accepted the SSL 'Not Secure' warning[cite: 1]."
        ]

        self.check_vars = []
        for task in tasks:
            var = tk.BooleanVar(value=False)
            self.check_vars.append(var)
            cb = ttk.Checkbutton(
                card, 
                text=task, 
                variable=var, 
                command=self.validate_checklist,
                bootstyle="success-round-toggle"
            )
            cb.pack(anchor=NW, pady=3, padx=5)

    def validate_checklist(self):
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

    def build_error_codes(self):
        card = ttk.Labelframe(self.page, text=" [1] LIVE SERVER ERROR CODE BREAKDOWN ", padding=15, bootstyle="danger")
        card.pack(fill=X, pady=10)

        card.columnconfigure(0, weight=1)
        card.columnconfigure(1, weight=1)

        errs = [
            ("HTTP 400 — Duplicate / Client Rejection", "The ticket has already been scanned or is invalid."),
            ("HTTP 403 — Access Denied", "The ticket belongs to a completely different event day."),
            ("HTTP 404 — Attendee Not Found", "The scanned QR code does not match database records."),
            ("HTTP 500+ — Server Fatality", "Internal script crash. Check python console immediately.")
        ]

        for i, (code, desc) in enumerate(errs):
            row, col = divmod(i, 2)
            sub_frame = ttk.Frame(card, padding=5)
            sub_frame.grid(row=row, column=col, sticky=NSEW, padx=5, pady=5)
            
            self.add_responsive_label(sub_frame, code, font=("Helvetica", 9, "bold"), bootstyle="danger")
            self.add_responsive_label(sub_frame, desc, font=("Helvetica", 8))

    def build_startup_rules(self):
        card = ttk.Labelframe(self.page, text=" [2] STARTUP SEQUENCE & GOLDEN RULES ", padding=15, bootstyle="info")
        card.pack(fill=X, pady=10)

        self.add_responsive_label(card, "EXECUTION SEQUENCE:", font=("Helvetica", 10, "bold"), bootstyle="info")
        steps = [
            "1. Connect Phone A (via USB Tethering) to Laptop A[cite: 1].",
            "2. Connect RJ45 LAN/Switch to Registration Laptops[cite: 1].",
            "3. Turn ON Mobile Hotspot on Laptop A[cite: 1].",
            "4. Connect Scanner Phones (up to 8) to Laptop A's Hotspot[cite: 1].",
            "5. Launch server_hub.py -> Click '▶ Start Engine'[cite: 1].",
            "6. Start Cloudflare Tunnel (if online) -> Share public link[cite: 1]."
        ]
        for s in steps:
            self.add_responsive_label(card, s, font=("Helvetica", 9))

        self.add_responsive_label(card, "CRITICAL GOLDEN RULES:", font=("Helvetica", 10, "bold"), bootstyle="warning", pady=(10, 2))
        rules = [
            "STARTUP ORDER: Network -> Hotspot -> Server -> Clients[cite: 1].",
            "REGISTRATION PCs operate strictly via HTTP (Port 5000)[cite: 1].",
            "SCANNERS & GUI operate via HTTPS (Port 5001) -> Required for cameras & live sync[cite: 1]!",
            "NEVER close server_hub.py during the live event runtime[cite: 1].",
            "Staff never connect directly to MySQL; APIs handle database traffic[cite: 1]."
        ]
        for r in rules:
            self.add_responsive_label(card, r, font=("Helvetica", 9), bullet=True)

    def build_routing_matrix(self):
        card = ttk.Labelframe(self.page, text=" [3] DEVICE ROUTING & URL MATRIX ", padding=15, bootstyle="secondary")
        card.pack(fill=X, pady=10)

        columns = ("device", "network", "target_url", "purpose")
        tree = ttk.Treeview(card, columns=columns, show="headings", bootstyle="secondary", height=6)
        
        tree.heading("device", text="DEVICE")
        tree.heading("network", text="NETWORK")
        tree.heading("target_url", text="TARGET URL (ENGINE)")
        tree.heading("purpose", text="PURPOSE")
        
        tree.column("device", width=120, stretch=True)
        tree.column("network", width=90, stretch=True)
        tree.column("target_url", width=220, stretch=True)
        tree.column("purpose", width=260, stretch=True)

        data = [
            ("Kiosk Laptops", "LAN", "http://<IP>:5000 (Waitress)", "Fast data entry (No SSL lag)[cite: 1]"),
            ("Mobile Scanners", "Wi-Fi", "https://<IP>:5001 (Cheroot)", "Unlocks iOS/Android cameras[cite: 1]"),
            ("Master Phone A", "USB Tether", "https://<IP>:5001 (Cheroot)", "Hardwired scanner; immune to lag[cite: 1]"),
            ("Gate Displays", "Wi-Fi/LAN", "https://<IP>:5001 (Cheroot)", "Instant GUI updates (Unbuffered)[cite: 1]"),
            ("Roving Staff", "4G/5G", "https://<tunnel>.trycloudflare", "Secure remote scanning[cite: 1]"),
            ("Background Sync", "USB/Wi-Fi", "(Runs Automatically)", "Cloudinary Photo API sync[cite: 1]")
        ]

        for item in data:
            tree.insert("", END, values=item)

        tree.pack(fill=X, pady=5)

    def build_offline_optimizations(self):
        card = ttk.Labelframe(self.page, text=" [4] OFFLINE MODE & SYSTEM OPTIMIZATIONS ", padding=15, bootstyle="warning")
        card.pack(fill=X, pady=10)

        self.add_responsive_label(card, "OFFLINE PROTOCOL (INTERNET GOES DOWN):", font=("Helvetica", 10, "bold"), bootstyle="warning")
        off_text = "DO NOT PANIC: Local Hotspot & LAN continue working smoothly.\n• Registration uses Port 5000; Scanners use Port 5001[cite: 1].\n• Remote Cloudflare staff will pause until internet restoration[cite: 1]."
        self.add_responsive_label(card, off_text, font=("Helvetica", 9))

        self.add_responsive_label(card, "CRITICAL SYSTEM & POWER PRESETS:", font=("Helvetica", 10, "bold"), bootstyle="danger", pady=(10, 2))
        optims = [
            ("NETWORK PROFILE:", "Must be configured as 'Private' (Windows blocks LAN traffic on 'Public')[cite: 1]."),
            ("FIREWALL PORTS:", "Ensure inbound TCP rules allow ports 5000, 5001, 3306[cite: 1]."),
            ("ANTIVIRUS:", "Temporarily pause aggressive firewalls that block local socket traffic[cite: 1]."),
            ("POWER PLAN:", "Set Windows Screen & Sleep parameters strictly to 'Never'[cite: 1]."),
            ("ADAPTERS:", "Device Manager > Network Adapters > Uncheck 'Allow computer to turn off device'[cite: 1].")
        ]
        for title, desc in optims:
            self.add_responsive_label(card, f"• {title} {desc}", font=("Helvetica", 9))

    def build_troubleshooting(self):
        card = ttk.Labelframe(self.page, text=" [5] FIELD TROUBLESHOOTING & EMERGENCY FIXES ", padding=15, bootstyle="danger")
        card.pack(fill=X, pady=10)

        fixes = [
            ("» FIX: DEVICES CANNOT LOAD HUB IP", 
             "1. Verify Laptop A IP via 'ipconfig'.\n2. Check device Wi-Fi/LAN associations.\n3. Turn OFF active VPN connections (scrambles local routing)[cite: 1]."),
            
            ("» FIX: CLOUDFLARE TUNNEL TIMEOUT", 
             "• Re-configure Laptop A DNS servers to 1.1.1.1 (Primary) and 1.0.0.1 (Secondary)[cite: 1]."),
            
            ("» FIX: UNSTABLE OR DROPPING NETWORK", 
             "PowerShell Rule Command:\nNew-NetFirewallRule -DisplayName 'EventHub Ports' -Direction Inbound -Action Allow -Protocol TCP -LocalPort 5000,5001[cite: 1]\n\nCMD Reset Sequence:\nipconfig /flushdns & ipconfig /release & ipconfig /renew & netsh winsock reset[cite: 1]"),
            
            ("» MANUAL STATIC LAN FALLBACK", 
             "• Server Static IP: 192.168.10.1 (Subnet: 255.255.255.0)\n• Client Static IPs: 192.168.10.2+\n• Fallback Access URL: http://192.168.10.1:5000[cite: 1]")
        ]

        for title, desc in fixes:
            sub = ttk.Labelframe(card, text=f" {title} ", padding=10, bootstyle="dark" if self.current_theme_is_dark else "light")
            sub.pack(fill=X, pady=5)
            self.add_responsive_label(sub, desc, font=("Courier", 9))

    def build_certificate_installation(self):
        card = ttk.Labelframe(self.page, text=" [6] CERTIFICATE INSTALLATION (HTTPS WARNING FIX) ", padding=15, bootstyle="primary")
        card.pack(fill=X, pady=10)

        self.add_responsive_label(card, "WINDOWS PC INSTALLATION:", font=("Helvetica", 10, "bold"), bootstyle="info")
        pc_steps = [
            "1. Rename 'hub_cert.pem' (found in config/certs) to 'hub_cert.crt'.",
            "2. Double-click the file and click 'Install Certificate'.",
            "3. Select 'Local Machine' as the Store Location.",
            "4. CRITICAL: Choose 'Place all certificates in the following store' -> Browse -> 'Trusted Root Certification Authorities'.",
            "5. Click Finish and completely restart your web browser."
        ]
        for s in pc_steps:
            self.add_responsive_label(card, f"• {s}", font=("Helvetica", 9))

        self.add_responsive_label(card, "ANDROID / SAMSUNG INSTALLATION:", font=("Helvetica", 10, "bold"), bootstyle="success", pady=(10, 2))
        android_steps = [
            "1. Transfer the certificate to the phone and rename it to 'hub_cert.cer'.",
            "2. WARNING: Do NOT open the file directly from the File Manager (it will ask for a private key).",
            "3. Open Android Settings -> Security & Privacy -> More security settings -> Encryption & credentials.",
            "4. Tap 'Install a certificate' -> MUST select 'CA Certificate' (Do not select User/VPN).",
            "5. Accept the privacy warning ('Install anyway') and select your 'hub_cert.cer' file."
        ]
        for s in android_steps:
            self.add_responsive_label(card, f"• {s}", font=("Helvetica", 9))

if __name__ == "__main__":
    app = EventHubApp()
    app.mainloop()