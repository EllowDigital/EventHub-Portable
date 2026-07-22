import os
import json
import logging
from datetime import datetime
import ttkbootstrap as ttk
from ttkbootstrap.constants import *
from PIL import Image, ImageTk

# Import models and DB initialization from your schema
try:
    from app.schema import Attendee, get_database_sessions
except ModuleNotFoundError:
    from schema import Attendee, get_database_sessions

# ==============================================================================
# PATHS & CONFIG
# ==============================================================================
BASE_DIR = os.path.dirname(os.path.abspath(__file__))
PHOTOS_DIR = os.path.join(BASE_DIR, 'attendee_photos')
PLACEHOLDER_IMG = os.path.join(BASE_DIR, 'assets', 'placeholder.png') # Optional placeholder

# Ensure directories exist
os.makedirs(PHOTOS_DIR, exist_ok=True)

class AttendeeExplorer(ttk.Window):
    def __init__(self):
        super().__init__(themename="cyborg", title="TDE UP 2026 — Attendee Explorer")
        self.geometry("1300x800")
        
        self.SessionMySQL = None
        self.connect_db()
        
        self.build_ui()
        self.load_data()

    def connect_db(self):
        try:
            sessions = get_database_sessions()
            self.SessionMySQL = sessions.get('mysql')
        except Exception as e:
            logging.error(f"Database Connection Failed: {e}")

    def build_ui(self):
        main_paned = ttk.Panedwindow(self, orient=HORIZONTAL)
        main_paned.pack(fill=BOTH, expand=True, padx=20, pady=20)

        # ==========================================
        # LEFT PANEL: SEARCH & DATAGRID
        # ==========================================
        left_frame = ttk.Frame(main_paned)
        main_paned.add(left_frame, weight=2)

        # Search Bar
        search_frame = ttk.Frame(left_frame)
        search_frame.pack(fill=X, pady=(0, 15))
        
        ttk.Label(search_frame, text="🔍 Search:", font="-weight bold").pack(side=LEFT, padx=(0, 10))
        self.ent_search = ttk.Entry(search_frame)
        self.ent_search.pack(side=LEFT, fill=X, expand=True, padx=(0, 10))
        self.ent_search.bind("<KeyRelease>", lambda e: self.filter_data())
        
        ttk.Button(search_frame, text="⟳ Refresh Data", bootstyle=INFO, command=self.load_data).pack(side=RIGHT)

        # Treeview (Data Grid)
        cols = ("ID", "Name", "Mobile", "Type", "City", "Synced")
        self.tree = ttk.Treeview(left_frame, columns=cols, show="headings", bootstyle=PRIMARY)
        
        self.tree.heading("ID", text="ATTENDEE ID", anchor=W)
        self.tree.heading("Name", text="FULL NAME", anchor=W)
        self.tree.heading("Mobile", text="MOBILE", anchor=W)
        self.tree.heading("Type", text="TYPE", anchor=W)
        self.tree.heading("City", text="CITY", anchor=W)
        self.tree.heading("Synced", text="CLOUD SYNC", anchor=W)
        
        self.tree.column("ID", width=120)
        self.tree.column("Name", width=200)
        self.tree.column("Mobile", width=120)
        self.tree.column("Type", width=100)
        self.tree.column("City", width=120)
        self.tree.column("Synced", width=100)
        
        self.tree.bind("<<TreeviewSelect>>", self.on_row_select)
        
        scrollbar = ttk.Scrollbar(left_frame, orient=VERTICAL, command=self.tree.yview)
        self.tree.configure(yscrollcommand=scrollbar.set)
        
        self.tree.pack(side=LEFT, fill=BOTH, expand=True)
        scrollbar.pack(side=RIGHT, fill=Y)

        # ==========================================
        # RIGHT PANEL: PROFILE & PHOTO
        # ==========================================
        right_frame = ttk.Frame(main_paned, padding=20)
        main_paned.add(right_frame, weight=1)

        ttk.Label(right_frame, text="ATTENDEE PROFILE", font="-size 14 -weight bold", bootstyle=PRIMARY).pack(anchor=W, pady=(0, 15))

        # Photo Display
        photo_frame = ttk.Frame(right_frame, borderwidth=1, relief="solid", padding=5)
        photo_frame.pack(pady=(0, 20))
        
        self.lbl_photo = ttk.Label(photo_frame, text="No Photo Selected", justify=CENTER)
        self.lbl_photo.pack()
        
        # Profile Details Data Map
        self.profile_vars = {
            "ID": ttk.StringVar(value="--"),
            "Name": ttk.StringVar(value="--"),
            "Mobile": ttk.StringVar(value="--"),
            "Email": ttk.StringVar(value="--"),
            "Gender": ttk.StringVar(value="--"),
            "Business": ttk.StringVar(value="--"),
            "Location": ttk.StringVar(value="--"),
            "Check-ins": ttk.StringVar(value="--")
        }

        # Build Profile Rows
        for label_text, var in self.profile_vars.items():
            row = ttk.Frame(right_frame)
            row.pack(fill=X, pady=5)
            ttk.Label(row, text=f"{label_text}:", width=12, font="-weight bold", foreground="gray").pack(side=LEFT)
            ttk.Label(row, textvariable=var, font="-weight bold", wraplength=250).pack(side=LEFT, fill=X, expand=True)

    def load_data(self):
        """Fetches all attendees from local MySQL and populates the grid."""
        if not self.SessionMySQL: return
        session = self.SessionMySQL()
        
        try:
            self.all_attendees = session.query(Attendee).order_by(Attendee.created_at.desc()).all()
            self.filter_data() # Populates the treeview using the full list initially
        except Exception as e:
            logging.error(f"Failed to load data: {e}")
        finally:
            session.close()

    def filter_data(self):
        """Filters the treeview based on the search bar input."""
        query = self.ent_search.get().strip().lower()
        
        # Clear existing items
        for item in self.tree.get_children():
            self.tree.delete(item)
            
        for att in getattr(self, 'all_attendees', []):
            # Check if query matches Name, ID, or Mobile
            if (query in att.full_name.lower() or 
                query in att.attendee_id.lower() or 
                query in att.mobile):
                
                sync_status = "Pending ⏳" if att.needs_cloud_sync else "Synced ✔"
                att_type = att.attendee_type.name if hasattr(att.attendee_type, 'name') else att.attendee_type
                
                self.tree.insert('', END, iid=att.attendee_id, values=(
                    att.attendee_id,
                    att.full_name,
                    att.mobile,
                    att_type,
                    att.city,
                    sync_status
                ))

    def on_row_select(self, event):
        """Loads detailed profile and photo when an attendee is clicked."""
        selected_items = self.tree.selection()
        if not selected_items: return
        
        selected_id = selected_items[0]
        
        # Find attendee in memory
        attendee = next((a for a in self.all_attendees if a.attendee_id == selected_id), None)
        if not attendee: return
        
        # Update Profile Text
        self.profile_vars["ID"].set(attendee.attendee_id)
        self.profile_vars["Name"].set(attendee.full_name)
        self.profile_vars["Mobile"].set(attendee.mobile)
        self.profile_vars["Email"].set(attendee.email or "N/A")
        
        gender_val = attendee.gender.name if hasattr(attendee.gender, 'name') else attendee.gender
        self.profile_vars["Gender"].set(gender_val)
        
        biz_name = attendee.business_name or "N/A"
        biz_cat = f" ({attendee.business_category})" if attendee.business_category else ""
        self.profile_vars["Business"].set(f"{biz_name}{biz_cat}")
        
        self.profile_vars["Location"].set(f"{attendee.city}, {attendee.state} - {attendee.pincode}")
        
        # Format Check-in History
        history = attendee.checkin_history
        if isinstance(history, str):
            try: history = json.loads(history)
            except: history = {}
        
        checkin_text = "\n".join([f"Day {day[-1]}: {time}" for day, time in history.items()]) if history else "No check-ins yet."
        self.profile_vars["Check-ins"].set(checkin_text)

        # Load Photo securely using absolute path derivation
        photo_path = os.path.join(PHOTOS_DIR, f"{attendee.attendee_id}.jpg")
        
        if os.path.exists(photo_path):
            self.render_image(photo_path)
        else:
            self.lbl_photo.configure(image='', text="No Photo Found\n(Run Photo Sync)")
            self.lbl_photo.image = None # Clear reference

    def render_image(self, path):
        """Uses Pillow to resize and display the JPG image."""
        try:
            # Open and resize the image to a standard profile portrait size
            img = Image.open(path)
            img = img.resize((200, 200), Image.Resampling.LANCZOS)
            
            # Convert to Tkinter format
            tk_img = ImageTk.PhotoImage(img)
            
            # Update label and keep reference to prevent garbage collection
            self.lbl_photo.configure(image=tk_img, text="")
            self.lbl_photo.image = tk_img 
        except Exception as e:
            self.lbl_photo.configure(image='', text=f"Error loading image")
            logging.error(f"Failed to load image for profile: {e}")

if __name__ == "__main__":
    app = AttendeeExplorer()
    app.mainloop()