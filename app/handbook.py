import os
import ctypes
import tkinter as tk
import ttkbootstrap as ttk
from ttkbootstrap.constants import *
from ttkbootstrap.widgets.scrolled import ScrolledFrame

class A4Page(ttk.Frame):
    def __init__(self, parent):
        super().__init__(parent)
        self.border = ttk.Frame(self, bootstyle="secondary")
        self.border.place(relx=0.5, rely=0.5, anchor=CENTER, relwidth=0.65, relheight=0.95)
        self.paper = ttk.Frame(self.border, bootstyle="default")
        self.paper.pack(fill=BOTH, expand=True, padx=1, pady=1) 
        self.scroll = ScrolledFrame(self.paper, autohide=True)
        self.scroll.pack(fill=BOTH, expand=True, padx=50, pady=40)
        
    @property
    def content(self):
        return self.scroll

class EventHubApp(ttk.Window):
    def __init__(self):
        super().__init__(themename="darkly") 
        self.title("Event Hub — Professional Quick Reference Handbook")
        self.geometry("1200x900")
        
        base_dir = os.path.dirname(os.path.abspath(__file__))
        icon_path = os.path.join(base_dir, "assets", "EventHub.ico")
        if os.path.exists(icon_path):
            try:
                self.iconbitmap(icon_path)
            except tk.TclError:
                pass

        try:
            self.state('zoomed') 
        except tk.TclError:
            self.attributes('-zoomed', True)

        self.current_theme_is_dark = True
        self.code_blocks = []
        self.pages = []
        self.current_page_idx = 0
        self.is_animating = False

        self.build_top_nav()
        self.page_container = ttk.Frame(self)
        self.page_container.pack(fill=BOTH, expand=True, pady=10)
        self.build_bottom_nav()
        self.init_pages()

    def init_pages(self):
        p1 = A4Page(self.page_container)
        self.build_header_banner(p1.content)
        self.build_startup_rules(p1.content)
        self.pages.append(p1)

        p2 = A4Page(self.page_container)
        self.build_routing_matrix(p2.content)
        self.build_offline_optimizations(p2.content)
        self.pages.append(p2)

        p3 = A4Page(self.page_container)
        self.build_troubleshooting(p3.content)
        self.build_certificate_installation(p3.content)
        self.pages.append(p3)

        p4 = A4Page(self.page_container)
        self.build_mysql_tuning(p4.content)
        self.pages.append(p4)

        p5 = A4Page(self.page_container)
        self.build_todo_checklist(p5.content)
        self.pages.append(p5)

        self.pages[0].place(relx=0, rely=0, relwidth=1, relheight=1)
        self.update_nav_buttons()

    def build_top_nav(self):
        nav_frame = ttk.Frame(self, padding=10, bootstyle="dark" if self.current_theme_is_dark else "light")
        nav_frame.pack(fill=X, side=TOP)
        ttk.Label(
            nav_frame, 
            text="⚡ EVENT HUB MISSION CONTROL", 
            font=("Segoe UI", 12, "bold")
        ).pack(side=LEFT, padx=10)
        self.theme_btn = ttk.Button(
            nav_frame, 
            text="☀ Switch to Light Mode", 
            command=self.toggle_theme, 
            bootstyle="outline-light"
        )
        self.theme_btn.pack(side=RIGHT, padx=10)

    def build_bottom_nav(self):
        nav_frame = ttk.Frame(self, padding=15)
        nav_frame.pack(fill=X, side=BOTTOM)
        self.btn_prev = ttk.Button(nav_frame, text="◀ Previous Page", command=self.prev_page, bootstyle="outline-primary", width=20)
        self.btn_prev.pack(side=LEFT, padx=30)
        self.lbl_page_info = ttk.Label(nav_frame, text="Page 1 of 5", font=("Segoe UI", 11, "bold"))
        self.lbl_page_info.pack(side=LEFT, expand=True)
        self.btn_next = ttk.Button(nav_frame, text="Next Page ▶", command=self.next_page, bootstyle="primary", width=20)
        self.btn_next.pack(side=RIGHT, padx=30)

    def next_page(self):
        if self.is_animating or self.current_page_idx >= len(self.pages) - 1:
            return
        old_page = self.pages[self.current_page_idx]
        self.current_page_idx += 1
        new_page = self.pages[self.current_page_idx]
        self.slide_pages(old_page, new_page, direction=1)

    def prev_page(self):
        if self.is_animating or self.current_page_idx <= 0:
            return
        old_page = self.pages[self.current_page_idx]
        self.current_page_idx -= 1
        new_page = self.pages[self.current_page_idx]
        self.slide_pages(old_page, new_page, direction=-1)

    def slide_pages(self, old_page, new_page, direction):
        steps = 25
        delay = 10
        self.is_animating = True
        new_page.lift()

        def step(i):
            if i <= steps:
                progress = i / steps
                eased = 1 - pow(1 - progress, 3)
                if direction == 1:
                    old_relx = -eased
                    new_relx = 1 - eased
                else:
                    old_relx = eased
                    new_relx = eased - 1
                old_page.place(relx=old_relx, rely=0, relwidth=1, relheight=1)
                new_page.place(relx=new_relx, rely=0, relwidth=1, relheight=1)
                self.after(delay, step, i + 1)
            else:
                old_page.place_forget()
                new_page.place(relx=0, rely=0, relwidth=1, relheight=1)
                self.is_animating = False
                self.update_nav_buttons()
        step(1)

    def update_nav_buttons(self):
        self.lbl_page_info.config(text=f"Page {self.current_page_idx + 1} of {len(self.pages)}")
        self.btn_prev.config(state=NORMAL if self.current_page_idx > 0 else DISABLED)
        self.btn_next.config(state=NORMAL if self.current_page_idx < len(self.pages) - 1 else DISABLED)

    def toggle_theme(self):
        if self.current_theme_is_dark:
            self.style.theme_use("litera")
            self.theme_btn.config(text="🌙 Switch to Dark Mode", bootstyle="outline-dark")
            self.current_theme_is_dark = False
        else:
            self.style.theme_use("darkly")
            self.theme_btn.config(text="☀ Switch to Light Mode", bootstyle="outline-light")
            self.current_theme_is_dark = True
        new_bg = "#1e1e1e" if self.current_theme_is_dark else "#f4f4f4"
        new_fg = "#d4d4d4" if self.current_theme_is_dark else "#000000"
        for text_widget in self.code_blocks:
            text_widget.config(bg=new_bg, fg=new_fg)

    def add_section_title(self, parent, text, bootstyle="primary"):
        separator = ttk.Separator(parent, bootstyle=bootstyle)
        separator.pack(fill=X, pady=(30, 5))
        lbl = ttk.Label(parent, text=text, font=("Segoe UI", 14, "bold"), bootstyle=bootstyle)
        lbl.pack(anchor=NW, pady=(0, 10))

    def add_bullet_points(self, parent, items, font=("Segoe UI", 10), bootstyle="default"):
        for item in items:
            lbl = ttk.Label(parent, text=f"• {item}", font=font, bootstyle=bootstyle, wraplength=800)
            lbl.pack(anchor=NW, pady=2, padx=10)

    def create_copyable_code_block(self, parent, text_content, height=5):
        frame = ttk.Frame(parent, padding=2, bootstyle="secondary")
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
        text_widget.configure(state=DISABLED) 
        text_widget.pack(fill=BOTH, expand=True, padx=5, pady=5)
        self.code_blocks.append(text_widget)

    def build_header_banner(self, parent):
        ttk.Label(
            parent, 
            text="QUICK REFERENCE HANDBOOK — DIGITAL COMMAND", 
            font=("Segoe UI", 20, "bold"), 
            bootstyle="primary"
        ).pack(anchor=CENTER, pady=(0, 5))
        ttk.Label(
            parent, 
            text="Standard Operating Procedures for Dual-Engine Architecture & Gate Operations", 
            font=("Segoe UI", 11, "italic")
        ).pack(anchor=CENTER, pady=(0, 20))

    def build_startup_rules(self, parent):
        self.add_section_title(parent, "[1] STARTUP SEQUENCE & GOLDEN RULES", "info")
        ttk.Label(parent, text="EXECUTION SEQUENCE:", font=("Segoe UI", 11, "bold")).pack(anchor=NW, pady=(5,2))
        steps = [
            "Connect Master Phone A (via USB Tethering) to Laptop A.",
            "Connect RJ45 LAN/Switch to Registration Laptops.",
            "Turn ON Mobile Hotspot on Laptop A.",
            "Connect Mobile Scanners (up to 8) to Laptop A's Hotspot.",
            "Launch server_hub.py -> Click '▶ Start Engine'.",
            "Start Cloudflare Tunnel (if online) -> Share public link."
        ]
        for i, s in enumerate(steps, 1):
            ttk.Label(parent, text=f"{i}. {s}", font=("Segoe UI", 10)).pack(anchor=NW, padx=10, pady=1)
        ttk.Label(parent, text="CRITICAL GOLDEN RULES:", font=("Segoe UI", 11, "bold"), bootstyle="warning").pack(anchor=NW, pady=(15,2))
        rules = [
            "STARTUP ORDER: Network -> Hotspot -> Server -> Clients.",
            "REGISTRATION PCs = HTTP (Port 5000).",
            "SCANNERS & GUI = HTTPS (Port 5001) -> STRICTLY REQUIRED for cameras/live-sync!",
            "SSL WARNING: Accept the 'Not Secure' warning on scanners ONCE before the event.",
            "NEVER close server_hub.py during the live event runtime.",
            "Staff never connect directly to MySQL; APIs handle database traffic."
        ]
        self.add_bullet_points(parent, rules, bootstyle="warning")

    def build_routing_matrix(self, parent):
        self.add_section_title(parent, "[2] DEVICE ROUTING & URL MATRIX", "primary")
        columns = ("device", "network", "target_url")
        tree = ttk.Treeview(parent, columns=columns, show="headings", bootstyle="primary", height=6)
        tree.heading("device", text="DEVICE")
        tree.heading("network", text="NETWORK")
        tree.heading("target_url", text="TARGET URL (ENGINE)")
        tree.column("device", width=150)
        tree.column("network", width=100)
        tree.column("target_url", width=250)
        data = [
            ("Kiosk Laptops", "LAN", "http://<IP>:5000 (Waitress)"),
            ("Mobile Scanners", "Wi-Fi", "https://<IP>:5001 (Cheroot)"),
            ("Master Phone A", "USB Tether", "https://<IP>:5001 (Cheroot)"),
            ("Gate Displays", "Wi-Fi/LAN", "https://<IP>:5001 (Cheroot)"),
            ("Roving Staff", "4G/5G", "https://<tunnel>.trycloudflare"),
            ("Background Sync", "USB/Wi-Fi", "(Runs Automatically)")
        ]
        for item in data:
            tree.insert("", END, values=item)
        tree.pack(fill=X, pady=10, padx=10)

    def build_offline_optimizations(self, parent):
        self.add_section_title(parent, "[3] & [4] OFFLINE MODE & SYSTEM OPTIMIZATIONS", "danger")
        ttk.Label(parent, text="OFFLINE PROTOCOL (INTERNET GOES DOWN):", font=("Segoe UI", 11, "bold")).pack(anchor=NW, pady=(5,2))
        self.add_bullet_points(parent, [
            "DO NOT PANIC: Local Hotspot & LAN continue working normally.",
            "Registration uses Port 5000; Scanners use Port 5001.",
            "Remote Cloudflare staff will be DOWN until internet returns."
        ])
        ttk.Label(parent, text="SYSTEM & POWER OPTIMIZATIONS:", font=("Segoe UI", 11, "bold")).pack(anchor=NW, pady=(15,2))
        self.add_bullet_points(parent, [
            "NETWORK PROFILE: Must be 'Private' (Windows blocks LAN on 'Public').",
            "FIREWALL PORTS: Allow TCP 5000, 5001, 3306.",
            "ANTIVIRUS: Pause McAfee/Norton/Avast firewalls.",
            "POWER PLAN: Set Screen & Sleep to 'Never'."
        ])

    def build_troubleshooting(self, parent):
        self.add_section_title(parent, "[5] TROUBLESHOOTING & EMERGENCY FIXES", "warning")
        fixes = [
            ("» FIX: DEVICES CANNOT LOAD HUB IP", 
             "1. Check Laptop A's IP via 'ipconfig'.\n2. Verify devices are on the correct Wi-Fi/LAN.\n3. Turn OFF all VPNs (they scramble local routing).", 4),
            ("» FIX: CLOUDFLARE TUNNEL TIMEOUT", 
             "Change Laptop A DNS to: 1.1.1.1 (Preferred) and 1.0.0.1 (Alternate).", 2),
            ("» FIX: NETWORK GLITCHY / DROPPING", 
             "Admin PowerShell:\nNew-NetFirewallRule -DisplayName \"EventHub Ports\" -Direction Inbound -Action Allow -Protocol TCP -LocalPort 5000,5001\n\nAdmin CMD:\nipconfig /flushdns & ipconfig /release & ipconfig /renew & netsh winsock reset", 6),
        ]
        for title, cmd, lines in fixes:
            ttk.Label(parent, text=title, font=("Segoe UI", 10, "bold")).pack(anchor=NW, padx=10, pady=(10, 2))
            self.create_copyable_code_block(parent, cmd, height=lines)

    def build_certificate_installation(self, parent):
        self.add_section_title(parent, "[6] CERTIFICATE INSTALLATION", "info")
        ttk.Label(parent, text="WINDOWS PC INSTALLATION:", font=("Segoe UI", 10, "bold")).pack(anchor=NW, padx=10, pady=(5,2))
        self.add_bullet_points(parent, [
            "Rename 'hub_cert.pem' to 'hub_cert.crt'.",
            "Double-click the file and click 'Install Certificate' -> 'Local Machine'.",
            "CRITICAL: Choose 'Place all certificates in the following store' -> Browse -> 'Trusted Root Certification Authorities'."
        ])

    def build_mysql_tuning(self, parent):
        self.add_section_title(parent, "[7] MYSQL DATABASE & NETWORK TUNING", "success")
        ttk.Label(parent, text="» 1. ENABLE LAN / RJ45 / WI-FI ACCESS (my.ini REQUIRED):", font=("Segoe UI", 10, "bold")).pack(anchor=NW, padx=10, pady=(5,2))
        self.create_copyable_code_block(parent, "bind-address=*\n", height=2)
        ttk.Label(parent, text="» 2. ADD REMOTE USER FOR LAN ACCESS (MySQL Shell):", font=("Segoe UI", 10, "bold")).pack(anchor=NW, padx=10, pady=(15,2))
        self.create_copyable_code_block(parent,
            "CREATE USER IF NOT EXISTS 'event_admin'@'%' IDENTIFIED BY 'EventHub123!';\n"
            "GRANT ALL PRIVILEGES ON *.* TO 'event_admin'@'%';\n"
            "FLUSH PRIVILEGES;\n"
            "EXIT;", height=5)
        ttk.Label(parent, text="» 3. PERFORMANCE TUNING: METHOD B - LIVE SHELL:", font=("Segoe UI", 10, "bold")).pack(anchor=NW, padx=10, pady=(15,2))
        persist_content = """SET PERSIST max_connections = 150;
SET PERSIST max_connect_errors = 10000;
SET PERSIST wait_timeout = 600;
SET PERSIST interactive_timeout = 600;
SET PERSIST net_read_timeout = 60;
SET PERSIST net_write_timeout = 120;
SET PERSIST max_allowed_packet = 67108864;
SET PERSIST innodb_file_per_table = 1;
SET PERSIST innodb_buffer_pool_size = 1073741824;"""
        self.create_copyable_code_block(parent, persist_content, height=12)

    def build_todo_checklist(self, parent):
        self.add_section_title(parent, "[8] EVENT DAY PRE-FLIGHT CHECKLIST", "success")
        self.status_label = ttk.Label(
            parent, 
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
                parent, 
                text=task, 
                variable=var, 
                command=self.validate_checklist,
                bootstyle="success-round-toggle"
            )
            cb.pack(anchor=NW, pady=8, padx=15)

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

if __name__ == "__main__":
    if os.name == 'nt':
        try:
            my_app_id = os.environ.get("EVENTHUB_TOOL_ID", "EventHub.Tool.handbook")
            ctypes.windll.shell32.SetCurrentProcessExplicitAppUserModelID(my_app_id)
        except Exception:
            pass
    app = EventHubApp()
    app.mainloop()