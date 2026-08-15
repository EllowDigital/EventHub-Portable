import os
import sys
import ctypes
from PySide6.QtCore import Qt, QEasingCurve, QPropertyAnimation, QPoint, QParallelAnimationGroup
from PySide6.QtGui import QFont, QIcon
from PySide6.QtWidgets import (
    QApplication, QMainWindow, QWidget, QVBoxLayout, QHBoxLayout,
    QLabel, QPushButton, QScrollArea, QFrame, QStackedWidget,
    QTableWidget, QTableWidgetItem, QHeaderView, QTextEdit,
    QCheckBox, QSizePolicy
)

DARK_STYLESHEET = """
QWidget {
    background-color: #0f111a;
    color: #e5e9f0;
    font-family: "Segoe UI", sans-serif;
}
QFrame#paper, QWidget#scrollContent {
    background-color: #1a1c23;
}
QFrame#paper {
    border: 1px solid #2e3440;
    border-radius: 8px;
}
QScrollArea {
    background-color: transparent;
    border: none;
}
QLabel {
    background-color: transparent;
}
QLabel#headerTitle {
    color: #58a6ff;
    font-size: 20px;
    font-weight: bold;
}
QLabel#headerSubtitle {
    color: #8b949e;
    font-size: 12px;
    font-style: italic;
}
QLabel#sectionTitle {
    color: #79c0ff;
    font-size: 14px;
    font-weight: bold;
}
QLabel#subHeader {
    color: #f2cc60;
    font-size: 12px;
    font-weight: bold;
}
QLabel#bulletPoint {
    color: #c9d1d9;
    font-size: 12px;
    line-height: 1.4;
}
QFrame#divider {
    background-color: #30363d;
    max-height: 1px;
}
QTextEdit {
    background-color: #111217;
    color: #7ee787;
    border: 1px solid #30363d;
    border-radius: 6px;
    font-family: "Consolas", monospace;
    font-size: 11px;
    padding: 6px;
}
QTableWidget {
    background-color: #161b22;
    color: #c9d1d9;
    gridline-color: #30363d;
    border: 1px solid #30363d;
    border-radius: 6px;
    font-size: 11px;
}
QHeaderView::section {
    background-color: #21262d;
    color: #58a6ff;
    font-weight: bold;
    border: 1px solid #30363d;
    padding: 6px;
    font-size: 11px;
}
QCheckBox {
    background-color: transparent;
    color: #c9d1d9;
    font-size: 12px;
    spacing: 10px;
    padding: 3px 0px;
}
QCheckBox::indicator {
    width: 18px;
    height: 18px;
    border-radius: 4px;
    border: 1px solid #484f58;
    background: #21262d;
}
QCheckBox::indicator:checked {
    background-color: #238636;
    border: 1px solid #2ea043;
}
QPushButton#navBtn {
    background-color: #238636;
    color: #ffffff;
    border-radius: 6px;
    padding: 8px 18px;
    font-weight: bold;
    font-size: 12px;
    border: 1px solid #2ea043;
}
QPushButton#navBtn:hover {
    background-color: #2ea043;
}
QPushButton#navBtn:disabled {
    background-color: #21262d;
    color: #484f58;
    border: 1px solid #30363d;
}
QPushButton#themeBtn {
    background-color: transparent;
    border: 1px solid #58a6ff;
    color: #58a6ff;
    border-radius: 6px;
    padding: 6px 14px;
    font-weight: bold;
    font-size: 11px;
}
QPushButton#themeBtn:hover {
    background-color: #1f6feb;
    color: #ffffff;
}
QScrollBar:vertical {
    background: #161b22;
    width: 10px;
    border-radius: 5px;
}
QScrollBar::handle:vertical {
    background: #30363d;
    border-radius: 5px;
    min-height: 25px;
}
QScrollBar::handle:vertical:hover {
    background: #484f58;
}
"""

LIGHT_STYLESHEET = """
QWidget {
    background-color: #f6f8fa;
    color: #24292f;
    font-family: "Segoe UI", sans-serif;
}
QFrame#paper, QWidget#scrollContent {
    background-color: #ffffff;
}
QFrame#paper {
    border: 1px solid #d0d7de;
    border-radius: 8px;
}
QScrollArea {
    background-color: transparent;
    border: none;
}
QLabel {
    background-color: transparent;
}
QLabel#headerTitle {
    color: #0969da;
    font-size: 20px;
    font-weight: bold;
}
QLabel#headerSubtitle {
    color: #57609a;
    font-size: 12px;
    font-style: italic;
}
QLabel#sectionTitle {
    color: #0969da;
    font-size: 14px;
    font-weight: bold;
}
QLabel#subHeader {
    color: #9a6700;
    font-size: 12px;
    font-weight: bold;
}
QLabel#bulletPoint {
    color: #24292f;
    font-size: 12px;
    line-height: 1.4;
}
QFrame#divider {
    background-color: #d8dee4;
    max-height: 1px;
}
QTextEdit {
    background-color: #f6f8fa;
    color: #1f2328;
    border: 1px solid #d0d7de;
    border-radius: 6px;
    font-family: "Consolas", monospace;
    font-size: 11px;
    padding: 6px;
}
QTableWidget {
    background-color: #ffffff;
    color: #24292f;
    gridline-color: #d8dee4;
    border: 1px solid #d0d7de;
    border-radius: 6px;
    font-size: 11px;
}
QHeaderView::section {
    background-color: #f6f8fa;
    color: #0969da;
    font-weight: bold;
    border: 1px solid #d0d7de;
    padding: 6px;
    font-size: 11px;
}
QCheckBox {
    background-color: transparent;
    color: #24292f;
    font-size: 12px;
    spacing: 10px;
    padding: 3px 0px;
}
QCheckBox::indicator {
    width: 18px;
    height: 18px;
    border-radius: 4px;
    border: 1px solid #d0d7de;
    background: #f6f8fa;
}
QCheckBox::indicator:checked {
    background-color: #1a7f37;
    border: 1px solid #1a7f37;
}
QPushButton#navBtn {
    background-color: #1f883d;
    color: #ffffff;
    border-radius: 6px;
    padding: 8px 18px;
    font-weight: bold;
    font-size: 12px;
    border: 1px solid #1a7f37;
}
QPushButton#navBtn:hover {
    background-color: #1a7f37;
}
QPushButton#navBtn:disabled {
    background-color: #eaeef2;
    color: #8c959f;
    border: 1px solid #d0d7de;
}
QPushButton#themeBtn {
    background-color: transparent;
    border: 1px solid #0969da;
    color: #0969da;
    border-radius: 6px;
    padding: 6px 14px;
    font-weight: bold;
    font-size: 11px;
}
QPushButton#themeBtn:hover {
    background-color: #0969da;
    color: #ffffff;
}
QScrollBar:vertical {
    background: #f6f8fa;
    width: 10px;
    border-radius: 5px;
}
QScrollBar::handle:vertical {
    background: #d0d7de;
    border-radius: 5px;
    min-height: 25px;
}
QScrollBar::handle:vertical:hover {
    background: #afb8c1;
}
"""

class SlidingStackedWidget(QStackedWidget):
    """Provides a smooth hardware-accelerated horizontal slide transition."""
    def __init__(self, parent=None):
        super().__init__(parent)
        self.m_speed = 280
        self.m_active = False

    def slide_in_idx(self, idx):
        if idx == self.currentIndex() or self.m_active or idx < 0 or idx >= self.count():
            return
        self.m_active = True
        width = self.frameGeometry().width()
        height = self.frameGeometry().height()
        offset_x = width if idx > self.currentIndex() else -width

        next_widget = self.widget(idx)
        curr_widget = self.currentWidget()

        next_widget.setGeometry(0, 0, width, height)
        next_widget.move(offset_x, 0)
        next_widget.show()
        next_widget.raise_()

        anim_curr = QPropertyAnimation(curr_widget, b"pos")
        anim_curr.setDuration(self.m_speed)
        anim_curr.setEasingCurve(QEasingCurve.OutCubic)
        anim_curr.setStartValue(QPoint(0, 0))
        anim_curr.setEndValue(QPoint(-offset_x, 0))

        anim_next = QPropertyAnimation(next_widget, b"pos")
        anim_next.setDuration(self.m_speed)
        anim_next.setEasingCurve(QEasingCurve.OutCubic)
        anim_next.setStartValue(QPoint(offset_x, 0))
        anim_next.setEndValue(QPoint(0, 0))

        self.group = QParallelAnimationGroup()
        self.group.addAnimation(anim_curr)
        self.group.addAnimation(anim_next)

        def on_finished():
            self.setCurrentIndex(idx)
            curr_widget.move(0, 0)
            self.m_active = False

        self.group.finished.connect(on_finished)
        self.group.start()

class A4Page(QWidget):
    """Simulates an A4 centered container with smooth vertical scrolling."""
    def __init__(self):
        super().__init__()
        outer_layout = QHBoxLayout(self)
        outer_layout.setContentsMargins(20, 10, 20, 10)

        # Centered A4 Card Frame
        self.paper = QFrame()
        self.paper.setObjectName("paper")
        self.paper.setSizePolicy(QSizePolicy.Expanding, QSizePolicy.Expanding)

        paper_layout = QVBoxLayout(self.paper)
        paper_layout.setContentsMargins(15, 15, 15, 15)

        self.scroll = QScrollArea()
        self.scroll.setWidgetResizable(True)
        self.scroll.setFrameShape(QFrame.NoFrame)
        # Bulletproof transparent viewport trick to stop white background bleed
        self.scroll.viewport().setStyleSheet("background-color: transparent;")

        self.scroll_content = QWidget()
        # [CRITICAL FIX] Ensure this inner widget gets the background color from our stylesheet
        self.scroll_content.setObjectName("scrollContent") 
        
        self.content_layout = QVBoxLayout(self.scroll_content)
        self.content_layout.setContentsMargins(25, 20, 25, 20)
        self.content_layout.setSpacing(8)

        self.scroll.setWidget(self.scroll_content)
        paper_layout.addWidget(self.scroll)
        outer_layout.addWidget(self.paper)

    @property
    def layout(self):
        return self.content_layout

class EventHubHandbookApp(QMainWindow):
    def __init__(self):
        super().__init__()
        self.setWindowTitle("EventHub Portable (v2.6) — Quick Reference Handbook")
        self.resize(1240, 920)

        base_dir = os.path.dirname(os.path.abspath(__file__))
        icon_path = os.path.join(base_dir, "assets", "EventHub.ico")
        if os.path.exists(icon_path):
            self.setWindowIcon(QIcon(icon_path))

        self.is_dark_theme = True
        self.check_vars = []

        central_widget = QWidget()
        central_widget.setObjectName("centralWidget")
        self.setCentralWidget(central_widget)

        main_layout = QVBoxLayout(central_widget)
        main_layout.setContentsMargins(0, 0, 0, 0)
        main_layout.setSpacing(0)

        # Top Bar
        main_layout.addWidget(self.build_top_nav())

        # Page Container
        self.stacked_widget = SlidingStackedWidget()
        main_layout.addWidget(self.stacked_widget, 1)

        # Bottom Bar
        main_layout.addWidget(self.build_bottom_nav())

        # Build Pages
        self.init_pages()
        self.apply_theme()
        self.update_nav_state()

    def build_top_nav(self):
        top_frame = QFrame()
        top_frame.setFixedHeight(50)
        layout = QHBoxLayout(top_frame)
        layout.setContentsMargins(25, 0, 25, 0)

        title_lbl = QLabel("⚡ EVENT HUB MISSION CONTROL")
        title_lbl.setFont(QFont("Segoe UI", 12, QFont.Bold))
        layout.addWidget(title_lbl)
        layout.addStretch()

        self.theme_btn = QPushButton("☀ Switch to Light Mode")
        self.theme_btn.setObjectName("themeBtn")
        self.theme_btn.clicked.connect(self.toggle_theme)
        layout.addWidget(self.theme_btn)
        return top_frame

    def build_bottom_nav(self):
        bottom_frame = QFrame()
        bottom_frame.setFixedHeight(60)
        layout = QHBoxLayout(bottom_frame)
        layout.setContentsMargins(35, 0, 35, 0)

        self.btn_prev = QPushButton("◀ Previous Page")
        self.btn_prev.setObjectName("navBtn")
        self.btn_prev.setFixedWidth(170)
        self.btn_prev.clicked.connect(self.prev_page)
        layout.addWidget(self.btn_prev)

        layout.addStretch()
        self.lbl_page_info = QLabel("Page 1 of 5")
        self.lbl_page_info.setFont(QFont("Segoe UI", 11, QFont.Bold))
        layout.addWidget(self.lbl_page_info)
        layout.addStretch()

        self.btn_next = QPushButton("Next Page ▶")
        self.btn_next.setObjectName("navBtn")
        self.btn_next.setFixedWidth(170)
        self.btn_next.clicked.connect(self.next_page)
        layout.addWidget(self.btn_next)
        return bottom_frame

    def next_page(self):
        cur = self.stacked_widget.currentIndex()
        if cur < self.stacked_widget.count() - 1:
            self.stacked_widget.slide_in_idx(cur + 1)
            self.lbl_page_info.setText(f"Page {cur + 2} of {self.stacked_widget.count()}")
            self.update_nav_state(cur + 1)

    def prev_page(self):
        cur = self.stacked_widget.currentIndex()
        if cur > 0:
            self.stacked_widget.slide_in_idx(cur - 1)
            self.lbl_page_info.setText(f"Page {cur} of {self.stacked_widget.count()}")
            self.update_nav_state(cur - 1)

    def update_nav_state(self, current_idx=0):
        self.btn_prev.setEnabled(current_idx > 0)
        self.btn_next.setEnabled(current_idx < self.stacked_widget.count() - 1)

    def toggle_theme(self):
        self.is_dark_theme = not self.is_dark_theme
        self.apply_theme()
        if self.is_dark_theme:
            self.theme_btn.setText("☀ Switch to Light Mode")
        else:
            self.theme_btn.setText("🌙 Switch to Dark Mode")

    def apply_theme(self):
        if self.is_dark_theme:
            self.setStyleSheet(DARK_STYLESHEET)
        else:
            self.setStyleSheet(LIGHT_STYLESHEET)

    # Helper UI Builders
    def add_section_header(self, layout, text):
        div = QFrame()
        div.setObjectName("divider")
        layout.addWidget(div)
        lbl = QLabel(text)
        lbl.setObjectName("sectionTitle")
        layout.addWidget(lbl)

    def add_sub_header(self, layout, text):
        lbl = QLabel(text)
        lbl.setObjectName("subHeader")
        layout.addWidget(lbl)

    def add_bullet_points(self, layout, items):
        for item in items:
            lbl = QLabel(f"•  {item}")
            lbl.setObjectName("bulletPoint")
            lbl.setWordWrap(True)
            layout.addWidget(lbl)

    def add_code_block(self, layout, text, min_lines=2):
        edit = QTextEdit()
        edit.setReadOnly(True)
        edit.setPlainText(text.strip())
        font_metrics = edit.fontMetrics()
        line_spacing = font_metrics.lineSpacing()
        edit.setFixedHeight(int(line_spacing * (min_lines + 1.2)) + 12)
        layout.addWidget(edit)

    # Pages Initialization
    def init_pages(self):
        # Page 1: Startup & Golden Rules
        p1 = A4Page()
        self.build_page_1(p1.layout)
        self.stacked_widget.addWidget(p1)

        # Page 2: Routing Matrix & Offline Optimizations
        p2 = A4Page()
        self.build_page_2(p2.layout)
        self.stacked_widget.addWidget(p2)

        # Page 3: Troubleshooting & Certificates
        p3 = A4Page()
        self.build_page_3(p3.layout)
        self.stacked_widget.addWidget(p3)

        # Page 4: MySQL Database & Performance Tuning
        p4 = A4Page()
        self.build_page_4(p4.layout)
        self.stacked_widget.addWidget(p4)

        # Page 5: Pre-Flight Checklist
        p5 = A4Page()
        self.build_page_5(p5.layout)
        self.stacked_widget.addWidget(p5)

    def build_page_1(self, layout):
        h_title = QLabel("QUICK REFERENCE HANDBOOK — DIGITAL COMMAND")
        h_title.setObjectName("headerTitle")
        h_title.setAlignment(Qt.AlignCenter)
        layout.addWidget(h_title)

        h_sub = QLabel("Standard Operating Procedures for Dual-Engine Architecture & Gate Operations")
        h_sub.setObjectName("headerSubtitle")
        h_sub.setAlignment(Qt.AlignCenter)
        layout.addWidget(h_sub)

        self.add_section_header(layout, "[1] STARTUP SEQUENCE & GOLDEN RULES")
        self.add_sub_header(layout, "EXECUTION SEQUENCE:")
        steps = [
            "1. Connect Master Phone A (via USB Tethering) to Laptop A.",
            "2. Connect RJ45 LAN/Switch to Registration Laptops.",
            "3. Turn ON Mobile Hotspot on Laptop A.",
            "4. Connect Mobile Scanners (up to 8) to Laptop A's Hotspot.",
            "5. Launch server_hub.py -> Click '▶ Start Engine'.",
            "6. Start Cloudflare Tunnel (if online) -> Share public link."
        ]
        for s in steps:
            lbl = QLabel(s)
            lbl.setObjectName("bulletPoint")
            layout.addWidget(lbl)

        self.add_sub_header(layout, "CRITICAL GOLDEN RULES:")
        rules = [
            "STARTUP ORDER: Network -> Hotspot -> Server -> Clients.",
            "REGISTRATION PCs = HTTP (Port 5000).",
            "SCANNERS & GUI = HTTPS (Port 5001) -> STRICTLY REQUIRED for cameras/live-sync!",
            "NEVER close server_hub.py during the live event runtime.",
            "Staff never connect directly to MySQL; APIs handle database traffic."
        ]
        self.add_bullet_points(layout, rules)
        layout.addStretch()

    def build_page_2(self, layout):
        self.add_section_header(layout, "[2] DEVICE ROUTING & URL MATRIX (DUAL-ENGINE ARCHITECTURE)")
        
        table = QTableWidget(6, 4)
        table.setHorizontalHeaderLabels(["DEVICE", "NETWORK", "TARGET URL (ENGINE)", "PURPOSE"])
        table.horizontalHeader().setSectionResizeMode(0, QHeaderView.ResizeToContents)
        table.horizontalHeader().setSectionResizeMode(1, QHeaderView.ResizeToContents)
        table.horizontalHeader().setSectionResizeMode(2, QHeaderView.ResizeToContents)
        table.horizontalHeader().setSectionResizeMode(3, QHeaderView.Stretch)
        table.verticalHeader().setVisible(False)
        table.setSelectionMode(QTableWidget.NoSelection)
        table.setEditTriggers(QTableWidget.NoEditTriggers)

        matrix_data = [
            ("Kiosk Laptops", "LAN", "http://<IP>:5000 (Waitress)", "Fast data entry (No SSL lag)"),
            ("Mobile Scanners", "Wi-Fi", "https://<IP>:5001 (Cheroot)", "Unlocks iOS/Android cameras"),
            ("Master Phone A", "USB Tether", "https://<IP>:5001 (Cheroot)", "Hardwired scanner; immune to lag"),
            ("Gate Displays", "Wi-Fi/LAN", "https://<IP>:5001 (Cheroot)", "Instant GUI updates (Unbuffered)"),
            ("Roving Staff", "4G/5G", "https://<tunnel>.trycloudflare", "Secure remote scanning"),
            ("Background Sync", "USB/Wi-Fi", "(Runs Automatically)", "Cloudinary Photo API")
        ]
        for row, row_data in enumerate(matrix_data):
            for col, text in enumerate(row_data):
                item = QTableWidgetItem(text)
                table.setItem(row, col, item)
        table.setFixedHeight(210)
        layout.addWidget(table)

        self.add_section_header(layout, "[3] & [4] OFFLINE MODE & SYSTEM OPTIMIZATIONS")
        self.add_sub_header(layout, "OFFLINE PROTOCOL (INTERNET GOES DOWN):")
        self.add_bullet_points(layout, [
            "DO NOT PANIC: Local Hotspot & LAN continue working normally.",
            "Registration uses Port 5000; Scanners use Port 5001.",
            "Remote Cloudflare staff will be DOWN until internet returns."
        ])

        self.add_sub_header(layout, "SYSTEM & POWER OPTIMIZATIONS (CRITICAL):")
        self.add_bullet_points(layout, [
            "NETWORK PROFILE: Must be 'Private' (Windows blocks LAN on 'Public').",
            "FIREWALL PORTS: Allow TCP 5000, 5001, 3306.",
            "ANTIVIRUS: Pause McAfee/Norton/Avast firewalls (they block local traffic).",
            "POWER PLAN: Set Screen & Sleep to 'Never'.",
            "ADAPTERS: Device Manager > Network Adapters > Uncheck 'Allow computer to turn off device'."
        ])
        layout.addStretch()

    def build_page_3(self, layout):
        self.add_section_header(layout, "[5] TROUBLESHOOTING & EMERGENCY FIXES")
        
        self.add_sub_header(layout, "» FIX: DEVICES CAN'T LOAD THE HUB IP")
        self.add_bullet_points(layout, [
            "1. Check Laptop A's IP via 'ipconfig'.",
            "2. Verify devices are on the correct Wi-Fi/LAN.",
            "3. Turn OFF all VPNs (they scramble local routing)."
        ])

        self.add_sub_header(layout, "» FIX: CLOUDFLARE WON'T OPEN")
        self.add_bullet_points(layout, [
            "Change Laptop A DNS to: 1.1.1.1 (Preferred) and 1.0.0.1 (Alternate)."
        ])

        self.add_sub_header(layout, "» FIX: NETWORK GLITCHY / DROPPING")
        self.add_bullet_points(layout, ["Admin PowerShell Command (Run to force allow ports):"])
        self.add_code_block(layout, 'New-NetFirewallRule -DisplayName "EventHub Ports" -Direction Inbound -Action Allow -Protocol TCP -LocalPort 5000,5001', min_lines=1)
        self.add_bullet_points(layout, ["Admin CMD (Network Reset):"])
        self.add_code_block(layout, "ipconfig /flushdns\nipconfig /release\nipconfig /renew\nnetsh winsock reset", min_lines=4)

        self.add_sub_header(layout, "» MANUAL LAN SETUP (FALLBACK)")
        self.add_code_block(layout, "- Server IP: 192.168.10.1 (Subnet: 255.255.255.0)\n- Client IPs: 192.168.10.2+ (Subnet: 255.255.255.0)\n- Client URL: http://192.168.10.1:5000", min_lines=3)

        self.add_section_header(layout, "[6] CERTIFICATE INSTALLATION (HTTPS WARNING FIX) - UPDATED ARCHITECTURE")
        self.add_sub_header(layout, "» WINDOWS PC INSTALLATION:")
        self.add_bullet_points(layout, [
            "Locate 'rootCA.pem' (found in config/certs) and rename to 'rootCA.crt'.",
            "Double-click the file and click 'Install Certificate'.",
            "Select 'Local Machine' as the Store Location.",
            "CRITICAL: Choose 'Place all certificates in the following store' -> Browse -> 'Trusted Root Certification Authorities'.",
            "Click Finish and completely restart your web browser."
        ])

        self.add_sub_header(layout, "» ANDROID / iOS DEVICE INSTALLATION:")
        self.add_bullet_points(layout, [
            "Transfer the 'rootCA.pem' certificate to the phone and rename it to 'rootCA.cer'.",
            "WARNING: Do NOT open the file directly from the File Manager.",
            "Open Android Settings -> Security & Privacy -> More security settings -> Encryption & credentials.",
            "Tap 'Install a certificate' -> MUST select 'CA Certificate' (Do not select User/VPN).",
            "Accept the privacy warning ('Install anyway') and select your 'rootCA.cer' file.",
            "(For iOS: Install Profile in Settings -> General -> About -> Certificate Trust Settings -> Enable Full Trust)."
        ])
        layout.addStretch()

    def build_page_4(self, layout):
        self.add_section_header(layout, "[7] MYSQL DATABASE & NETWORK TUNING (LAN & PERFORMANCE)")
        
        # 1. Network Binding
        self.add_sub_header(layout, "» 1. ENABLE LAN / RJ45 / WI-FI ACCESS (my.ini REQUIRED)")
        self.add_bullet_points(layout, [
            "CRITICAL: Network binding cannot be changed live. It MUST be done in the file.",
            "Open: C:\\ProgramData\\MySQL\\MySQL Server 8.4\\my.ini (Note: ProgramData is a hidden folder).",
            "Find the [mysqld] section and add/change this exact line (Use * to support IPv4 & IPv6 natively):"
        ])
        self.add_code_block(layout, "bind-address=*", min_lines=1)
        self.add_bullet_points(layout, ["Save the file and restart the MySQL Service (Admin CMD: net stop MySQL84 && net start MySQL84)."])

        # 2. Remote User
        self.add_sub_header(layout, "» 2. ADD REMOTE USER FOR LAN ACCESS (MySQL Shell)")
        self.add_bullet_points(layout, [
            "Open Command Prompt and log in: mysql -u root -p",
            "Run these exact commands to allow any laptop/PC on the network to connect:"
        ])
        remote_user_sql = """CREATE USER IF NOT EXISTS 'event_admin'@'%' IDENTIFIED BY 'EventHub123!';
GRANT ALL PRIVILEGES ON *.* TO 'event_admin'@'%';
FLUSH PRIVILEGES;
EXIT;"""
        self.add_code_block(layout, remote_user_sql, min_lines=4)
        self.add_bullet_points(layout, ["Other laptops can now connect using the Host Laptop's IP (e.g., 192.168.1.X) and these credentials."])

        # 3. Method A
        self.add_sub_header(layout, "» 3. PERFORMANCE TUNING: METHOD A - PERMANENT TEXT EDIT (my.ini)")
        self.add_bullet_points(layout, [
            "Open my.ini and locate the [mysqld] section.",
            "Paste the complete Event Hub tuning block directly into the file:"
        ])
        method_a_config = """# === EVENT HUB - PERFORMANCE TUNING (4GB RAM BASELINE) ===
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

# --- RAM BASED SETTINGS (Set based on Host PC Total RAM) ---
# [ 4GB RAM -> 1G ]  [ 8GB RAM -> 4G ]  [ 16GB RAM -> 8G ]  [ 24GB RAM -> 12G ]  [ 32GB RAM -> 16G ]
innodb_buffer_pool_size=1G
innodb_buffer_pool_instances=1
# ======================================"""
        self.add_code_block(layout, method_a_config, min_lines=28)
        self.add_bullet_points(layout, ["Requires restarting the MySQL service (Admin CMD: net stop MySQL84 && net start MySQL84)."])

        # 4. Method B
        self.add_sub_header(layout, "» 4. PERFORMANCE TUNING: METHOD B - LIVE SHELL (NO MY.INI EDITING)")
        self.add_bullet_points(layout, [
            "Use this to apply changes INSTANTLY without restarting the server.",
            "Using 'SET PERSIST' saves the changes for future reboots without touching my.ini.",
            "Note: 'innodb_log_buffer_size', 'innodb_buffer_pool_instances', 'innodb_flush_method', and 'disable-log-bin' MUST be done via Method A.",
            "Open MySQL Shell (mysql -u root -p) and paste this block:"
        ])
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
SET PERSIST collation_server = 'utf8mb4_unicode_ci';"""
        self.add_code_block(layout, persist_content, min_lines=24)
        self.add_bullet_points(layout, ["Set Buffer Pool dynamically (values MUST be in exact bytes in the shell):"])
        ram_allocations = """[ 4 GB RAM PC  -> 1G  ] : SET PERSIST innodb_buffer_pool_size = 1073741824;
[ 8 GB RAM PC  -> 4G  ] : SET PERSIST innodb_buffer_pool_size = 4294967296;
[ 16 GB RAM PC -> 8G  ] : SET PERSIST innodb_buffer_pool_size = 8589934592;
[ 24 GB RAM PC -> 12G ] : SET PERSIST innodb_buffer_pool_size = 12884901888;
[ 32 GB RAM PC -> 16G ] : SET PERSIST innodb_buffer_pool_size = 17179869184;"""
        self.add_code_block(layout, ram_allocations, min_lines=5)

        # 5. Verification
        self.add_sub_header(layout, "» 5. VERIFY CHANGES (MySQL Shell)")
        self.add_bullet_points(layout, ["To confirm LAN is open and RAM is allocated, open MySQL Shell and run:"])
        self.add_code_block(layout, "SHOW GLOBAL VARIABLES WHERE Variable_name IN ('bind_address', 'innodb_buffer_pool_size', 'max_connections');", min_lines=1)
        layout.addStretch()

    def build_page_5(self, layout):
        self.add_section_header(layout, "[8] EVENT DAY PRE-FLIGHT CHECKLIST")

        self.status_label = QLabel("⚠️ SYSTEM STATUS: PENDING PRE-FLIGHT CHECKS")
        self.status_label.setFont(QFont("Segoe UI", 11, QFont.Bold))
        self.status_label.setStyleSheet("color: #d29922; margin-bottom: 12px;")
        layout.addWidget(self.status_label)

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

        self.check_boxes = []
        for task in tasks:
            cb = QCheckBox(task)
            cb.stateChanged.connect(self.validate_checklist)
            layout.addWidget(cb)
            self.check_boxes.append(cb)

        layout.addStretch()

    def validate_checklist(self):
        if all(cb.isChecked() for cb in self.check_boxes):
            self.status_label.setText("✅ ALL SYSTEMS READY: Operational status green for live event execution!")
            self.status_label.setStyleSheet("color: #3fb950; margin-bottom: 12px;")
        else:
            self.status_label.setText("⚠️ SYSTEM STATUS: PENDING PRE-FLIGHT CHECKS")
            self.status_label.setStyleSheet("color: #d29922; margin-bottom: 12px;")

if __name__ == "__main__":
    if os.name == 'nt':
        try:
            my_app_id = os.environ.get("EVENTHUB_TOOL_ID", "EventHub.Tool.handbook")
            ctypes.windll.shell32.SetCurrentProcessExplicitAppUserModelID(my_app_id)
        except Exception:
            pass

    app = QApplication(sys.argv)
    window = EventHubHandbookApp()
    window.showMaximized()
    sys.exit(app.exec())