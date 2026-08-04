import tkinter as tk
import ttkbootstrap as ttk
from ttkbootstrap.constants import *
from ttkbootstrap.widgets.scrolled import ScrolledFrame

class EventHubApp(ttk.Window):
    def __init__(self):
        # Using 'litera' for a clean, white A4 document look
        super().__init__(themename="litera") 
        self.title("Event Hub - Quick Reference Handbook")
        self.geometry("900x950")
        
        # Start maximized to fit the monitor perfectly
        try:
            self.state('zoomed') 
        except tk.TclError:
            self.attributes('-zoomed', True) # Fallback for Linux

        # List to track all labels that need dynamic text wrapping
        self.wrap_labels = []

        # Background frame (acts as the desk behind the A4 paper)
        self.bg_frame = ttk.Frame(self, bootstyle="secondary")
        self.bg_frame.pack(fill=BOTH, expand=True)

        # Scrolled container
        self.main_scroll = ScrolledFrame(self.bg_frame)
        self.main_scroll.pack(fill=BOTH, expand=True, padx=20, pady=20)

        # "A4 Page" Frame (Centered document layout)
        self.page = ttk.Frame(self.main_scroll, padding=40, bootstyle="default")
        self.page.pack(fill=BOTH, expand=True)

        # Header Title
        header = ttk.Label(
            self.page, 
            text="EVENT HUB - QUICK REFERENCE HANDBOOK", 
            font=("Helvetica", 16, "bold"), 
            bootstyle="primary"
        )
        header.pack(pady=(0, 20))

        # Build all sections sequentially
        self.build_todo_checklist()
        self.add_separator()
        self.build_error_codes()
        self.add_separator()
        self.build_startup_rules()
        self.add_separator()
        self.build_routing_matrix()
        self.add_separator()
        self.build_offline_optimizations()
        self.add_separator()
        self.build_troubleshooting()

        # Bind the resize event to adjust text wrap dynamically
        self.main_scroll.bind("<Configure>", self.on_window_resize)

    def on_window_resize(self, event):
        """Dynamically adjusts the text wrapping length based on window size"""
        # Calculate available width minus padding/margins
        wrap_width = event.width - 150 
        if wrap_width > 100:
            for label in self.wrap_labels:
                label.configure(wraplength=wrap_width)

    def add_responsive_label(self, parent, text, font, bootstyle="default", pady=2, padx=10, bullet=False):
        """Helper to create labels that wrap text responsively"""
        if bullet: text = "• " + text
        lbl = ttk.Label(parent, text=text, font=font, bootstyle=bootstyle)
        lbl.pack(anchor=NW, pady=pady, padx=padx, fill=X)
        self.wrap_labels.append(lbl)
        return lbl

    def add_separator(self):
        """Helper to add horizontal lines between sections"""
        ttk.Separator(self.page, bootstyle="secondary").pack(fill=X, pady=20, padx=10)

    def build_todo_checklist(self):
        """Interactive Event Day To-Do List"""
        ttk.Label(
            self.page, 
            text="☑ EVENT DAY TO-DO LIST & SYSTEM CHECKS", 
            font=("Helvetica", 12, "bold"), 
            bootstyle="success"
        ).pack(anchor=NW, pady=(0, 10))

        # Status Label (Updates dynamically)
        self.status_label = ttk.Label(
            self.page, 
            text="⚠️ PENDING CHECKS: Please complete all steps below.", 
            font=("Helvetica", 10, "bold"), 
            bootstyle="warning"
        )
        self.status_label.pack(anchor=NW, pady=(0, 10), padx=10)

        # Checklist Items
        tasks = [
            "Network Profile is set to 'Private' on Laptop A.",
            "Phone A is connected via USB Tethering.",
            "Registration Laptops are connected via RJ45/LAN Switch.",
            "Mobile Hotspot is turned ON (Laptop A).",
            "Scanner Phones (up to 8) are connected to Hotspot Wi-Fi.",
            "server_hub.py is launched and 'Start Engine' is clicked.",
            "Cloudflare Tunnel is running (if online) and link shared.",
            "Scanner Phones have accepted the SSL 'Not Secure' warning."
        ]

        self.check_vars = []
        for task in tasks:
            var = tk.BooleanVar(value=False)
            self.check_vars.append(var)
            cb = ttk.Checkbutton(
                self.page, 
                text=task, 
                variable=var, 
                command=self.validate_checklist,
                bootstyle="success-round-toggle"
            )
            # Custom font configuration for Checkbutton text
            cb.configure(style="Small.TCheckbutton")
            self.style.configure("Small.TCheckbutton", font=("Helvetica", 9))
            cb.pack(anchor=NW, pady=3, padx=15)

    def validate_checklist(self):
        """Checks if all items in the To-Do list are completed"""
        if all(var.get() for var in self.check_vars):
            self.status_label.config(
                text="✅ ALL SYSTEMS READY: All checks completed successfully!", 
                bootstyle="success"
            )
        else:
            self.status_label.config(
                text="⚠️ PENDING CHECKS: Please complete all steps below.", 
                bootstyle="warning"
            )

    def build_error_codes(self):
        """Live Server Dashboard Error Explanations"""
        ttk.Label(
            self.page, 
            text="⚠ LIVE SERVER ERROR CODE MEANINGS", 
            font=("Helvetica", 12, "bold"), 
            bootstyle="danger"
        ).pack(anchor=NW, pady=(0, 10))

        error_frame = ttk.Labelframe(
            self.page, 
            text=" Rejection Breakdown Dictionary ", 
            bootstyle="danger", 
            padding=10
        )
        error_frame.pack(fill=X, padx=10, pady=5)
        
        # Make the layout responsive
        error_frame.columnconfigure(0, weight=1)
        error_frame.columnconfigure(1, weight=1)

        err_400 = "HTTP 400 — Duplicate / Client Rejection: The ticket has already been scanned or is invalid."
        err_403 = "HTTP 403 — Access Denied (wrong date): The ticket belongs to a different event day."
        err_404 = "HTTP 404 — Attendee Not Found: The QR code does not match any user in the database."
        err_500 = "HTTP 500+ — Server Fatality: Internal crash. Check python console immediately."

        l1 = ttk.Label(error_frame, text=err_400, font=("Helvetica", 9))
        l1.grid(row=0, column=0, sticky=EW, pady=5, padx=5)
        self.wrap_labels.append(l1)

        l2 = ttk.Label(error_frame, text=err_403, font=("Helvetica", 9))
        l2.grid(row=0, column=1, sticky=EW, pady=5, padx=5)
        self.wrap_labels.append(l2)

        l3 = ttk.Label(error_frame, text=err_404, font=("Helvetica", 9))
        l3.grid(row=1, column=0, sticky=EW, pady=5, padx=5)
        self.wrap_labels.append(l3)

        l4 = ttk.Label(error_frame, text=err_500, font=("Helvetica", 9))
        l4.grid(row=1, column=1, sticky=EW, pady=5, padx=5)
        self.wrap_labels.append(l4)

    def build_startup_rules(self):
        """Startup Sequence and Golden Rules"""
        ttk.Label(self.page, text="[1] STARTUP SEQUENCE", font=("Helvetica", 12, "bold"), bootstyle="info").pack(anchor=NW, pady=(0, 5))
        
        steps = [
            "1. Connect Phone A (via USB Tethering) to Laptop A.",
            "2. Connect RJ45 LAN/Switch to Registration Laptops.",
            "3. Turn ON Mobile Hotspot on Laptop A.",
            "4. Connect Scanner Phones (up to 8) to Laptop A's Hotspot.",
            "5. Launch server_hub.py -> Click '▶ Start Engine'.",
            "6. Start Cloudflare Tunnel (if online) -> Share public link."
        ]
        for step in steps:
            self.add_responsive_label(self.page, step, font=("Helvetica", 9))

        ttk.Label(self.page, text="[3] GOLDEN RULES & REMINDERS", font=("Helvetica", 12, "bold"), bootstyle="warning").pack(anchor=NW, pady=(15, 5))
        
        rules = [
            "STARTUP ORDER: Network -> Hotspot -> Server -> Clients.",
            "REGISTRATION PCs = HTTP (Port 5000).",
            "SCANNERS & GUI = HTTPS (Port 5001) -> STRICTLY REQUIRED for cameras/live-sync!",
            "SSL WARNING: Accept the 'Not Secure' warning on scanners ONCE before the event.",
            "NEVER close server_hub.py during the live event.",
            "Staff never connect to MySQL directly; APIs handle all traffic."
        ]
        for rule in rules:
            self.add_responsive_label(self.page, rule, font=("Helvetica", 9), bullet=True)

    def build_routing_matrix(self):
        """Device Routing & URL Matrix"""
        self.add_responsive_label(
            self.page, 
            "[2] DEVICE ROUTING & URL MATRIX (DUAL-ENGINE ARCHITECTURE)", 
            font=("Helvetica", 12, "bold"), 
            bootstyle="info",
            padx=0
        )

        columns = ("device", "network", "target_url", "purpose")
        tree = ttk.Treeview(self.page, columns=columns, show="headings", bootstyle="info", height=6)
        
        # Apply smaller font to treeview
        self.style.configure("Treeview.Heading", font=("Helvetica", 9, "bold"))
        self.style.configure("Treeview", font=("Helvetica", 9))

        tree.heading("device", text="DEVICE")
        tree.heading("network", text="NETWORK")
        tree.heading("target_url", text="TARGET URL (ENGINE)")
        tree.heading("purpose", text="PURPOSE")
        
        # Responsive relative widths for columns
        tree.column("device", width=100, stretch=True)
        tree.column("network", width=70, stretch=True)
        tree.column("target_url", width=200, stretch=True)
        tree.column("purpose", width=250, stretch=True)

        data = [
            ("Kiosk Laptops", "LAN", "http://<IP>:5000 (Waitress)", "Fast data entry (No SSL lag)"),
            ("Mobile Scanners", "Wi-Fi", "https://<IP>:5001 (Cheroot)", "Unlocks iOS/Android cameras"),
            ("Master Phone A", "USB Tether", "https://<IP>:5001 (Cheroot)", "Hardwired scanner; immune to lag"),
            ("Gate Displays", "Wi-Fi/LAN", "https://<IP>:5001 (Cheroot)", "Instant GUI updates (Unbuffered)"),
            ("Roving Staff", "4G/5G", "https://<tunnel>.trycloudflare", "Secure remote scanning"),
            ("Background Sync", "USB/Wi-Fi", "(Runs Automatically)", "Cloudinary Photo API")
        ]

        for item in data:
            tree.insert("", END, values=item)

        tree.pack(fill=X, pady=10, padx=10)

    def build_offline_optimizations(self):
        """Offline Mode and System Optimizations"""
        ttk.Label(self.page, text="[4] OFFLINE MODE (INTERNET GOES DOWN)", font=("Helvetica", 12, "bold"), bootstyle="secondary").pack(anchor=NW, pady=(0, 5))
        
        off_text = "DO NOT PANIC: Local Hotspot & LAN continue working normally.\n• Registration uses Port 5000; Scanners use Port 5001.\n• Remote Cloudflare staff will be DOWN until internet returns."
        self.add_responsive_label(self.page, off_text, font=("Helvetica", 9), bullet=True)

        ttk.Label(self.page, text="[5] SYSTEM & POWER OPTIMIZATIONS (CRITICAL)", font=("Helvetica", 12, "bold"), bootstyle="danger").pack(anchor=NW, pady=(15, 5))
        
        optims = [
            ("NETWORK PROFILE:", "Must be 'Private' (Windows blocks LAN on 'Public')."),
            ("FIREWALL PORTS:", "Allow TCP 5000, 5001, 3306."),
            ("ANTIVIRUS:", "Pause McAfee/Norton/Avast firewalls (they block local traffic)."),
            ("POWER PLAN:", "Set Screen & Sleep to 'Never'."),
            ("ADAPTERS:", "Device Manager > Network Adapters > Uncheck 'Allow computer to turn off device'.")
        ]

        for title, desc in optims:
            # Combine the bold title and regular description into one wrapping label
            full_text = f"• {title} {desc}"
            self.add_responsive_label(self.page, full_text, font=("Helvetica", 9))

    def build_troubleshooting(self):
        """Troubleshooting & Fixes"""
        ttk.Label(self.page, text="[6] TROUBLESHOOTING & FIXES", font=("Helvetica", 12, "bold"), bootstyle="warning").pack(anchor=NW, pady=(0, 10))

        fixes = [
            ("» FIX: DEVICES CAN'T LOAD THE HUB IP", 
             "1. Check Laptop A's IP via 'ipconfig'.\n2. Verify devices are on the correct Wi-Fi/LAN.\n3. Turn OFF all VPNs (they scramble local routing)."),
            
            ("» FIX: CLOUDFLARE WON'T OPEN", 
             "Change Laptop A DNS to: 1.1.1.1 (Preferred) and 1.0.0.1 (Alternate)."),
            
            ("» FIX: NETWORK GLITCHY / DROPPING", 
             "Admin PowerShell Command (Run to force allow ports):\n"
             "New-NetFirewallRule -DisplayName \"EventHub Ports\" -Direction Inbound -Action Allow -Protocol TCP -LocalPort 5000,5001\n\n"
             "Admin CMD (Network Reset):\n"
             "ipconfig /flushdns\nipconfig /release\nipconfig /renew\nnetsh winsock reset (Requires Restart)"),
            
            ("» MANUAL LAN SETUP (FALLBACK)", 
             "Server IP: 192.168.10.1 (Subnet: 255.255.255.0)\n"
             "Client IPs: 192.168.10.2+ (Subnet: 255.255.255.0)\n"
             "Client URL: http://192.168.10.1:5000")
        ]

        # Use a grid container for the cards to make them responsive side-by-side
        trouble_frame = ttk.Frame(self.page)
        trouble_frame.pack(fill=X, padx=10)
        
        for idx, (title, desc) in enumerate(fixes):
            card = ttk.Labelframe(trouble_frame, text=f" {title} ", padding=10, bootstyle="warning")
            card.pack(fill=X, pady=5)
            # Use Courier for code-like instructions, but smaller
            self.add_responsive_label(card, desc, font=("Courier", 9), padx=5)

if __name__ == "__main__":
    app = EventHubApp()
    app.mainloop()