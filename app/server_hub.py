"""
server_hub.py — EventHub Portable (TDE UP 2026)
═══════════════════════════════════════════════════════════════════════════

STEP 3 of 3 in the EventHub Portable roadmap:
    -> Step 1: schema.py         (DONE — DatabaseManager, DDL)
    -> Step 2: sync_manager.py   (DONE — on-demand cloud bridge)
    -> Step 3: server_hub.py     (THIS FILE — Flask HTTPS hub + Tkinter dashboard)

This is the core application engine that runs on the hub laptop for the
entire 3-day, ~10,000-attendee event:

    • A Flask server exposed over HTTPS on the laptop's LAN/hotspot, so
      up to 8 phones can reach it at https://<laptop-lan-ip>:5000.
    • A Tkinter "operator dashboard" — the only UI the person running the
      check-in desk needs: Start/Stop the server, see the LAN URL, and
      scan a QR code to hand phones the address instantly.
    • A single centralized write queue + worker thread, so 8 phones
      submitting registrations/check-ins at once can never collide on
      SQLite's single-writer file lock. register.py / check_in.py (the
      next steps) are expected to push their writes through
      `enqueue_write()` below rather than writing to SQLite directly.

WHY werkzeug.serving.make_server() INSTEAD OF app.run():
    Flask's own app.run() has no supported way to stop the server from
    another thread — once it starts serving it owns that thread until the
    process dies. Calling make_server() ourselves returns a real server
    object exposing .shutdown() / .server_close(), which is what lets the
    "Stop Server" button actually stop the HTTPS listener and free port
    5000, instead of the only way out being to kill the whole app.

THREADING MODEL:
    Main thread      -> Tkinter mainloop (owns the GUI for its lifetime).
    Daemon thread    -> Werkzeug HTTPS server (serve_forever), started and
                        stopped by the Start/Stop Server buttons.
    Daemon thread    -> Write-queue worker, started once at launch and
                        running for the life of the process.
    Ad-hoc daemon
    threads          -> Spawned per /api/sync/start call so a sync never
                        blocks a phone's HTTP request or the GUI.

OFFLINE-FIRST: this is a local, no-internet event tool. The inline HTML
below makes zero external requests (no CDN, no web fonts) — it has to
render correctly for 8 phones on a hotspot with no upstream internet.
"""

import io
import socket
import logging
import threading
import queue
from typing import Any, Callable, Optional

from flask import Flask, jsonify
from werkzeug.serving import make_server, generate_adhoc_ssl_context

import tkinter as tk
from tkinter import ttk, messagebox

from app.schema import get_manager, SchemaInitializationError
from app.sync_manager import get_sync_manager

# ─────────────────────────────────────────────────────────────────────────
# QR code generation is OPTIONAL at import time, same policy as the
# optional MySQL/Supabase/requests imports in schema.py / sync_manager.py.
# If qrcode/Pillow aren't installed, the hub server must still start — the
# dashboard just shows an install hint instead of a QR image.
# ─────────────────────────────────────────────────────────────────────────
try:
    import qrcode
    from PIL import Image, ImageTk
    QR_LIBS_AVAILABLE = True
except ImportError:  # pragma: no cover - environment dependent
    qrcode = None       # type: ignore
    Image = None        # type: ignore
    ImageTk = None       # type: ignore
    QR_LIBS_AVAILABLE = False


# ═══════════════════════════════════════════════════════════════════
#  LOGGING
# ═══════════════════════════════════════════════════════════════════
logger = logging.getLogger("eventhub.server_hub")
if not logger.handlers:
    _handler = logging.StreamHandler()
    _handler.setFormatter(logging.Formatter(
        "%(asctime)s [%(levelname)s] %(name)s: %(message)s"
    ))
    logger.addHandler(_handler)
    logger.setLevel(logging.INFO)


# ═══════════════════════════════════════════════════════════════════
#  INLINE HTML — per project rule, no home.html / network.html files.
#  Plain (non f-) strings on purpose: the CSS below is full of literal
#  { } braces that an f-string would try to parse as expressions.
# ═══════════════════════════════════════════════════════════════════
HOME_HTML = """<!DOCTYPE html>
<html lang="en">
<head>
<meta charset="UTF-8">
<meta name="viewport" content="width=device-width, initial-scale=1.0">
<title>EventHub Portable — TDE UP 2026</title>
<style>
  * { box-sizing: border-box; margin: 0; padding: 0; }
  body {
    font-family: -apple-system, "Segoe UI", Roboto, Helvetica, Arial, sans-serif;
    min-height: 100vh;
    display: flex;
    align-items: center;
    justify-content: center;
    background: linear-gradient(160deg, #1b1f3b 0%, #2f3565 100%);
  }
  .panel { width: 100%; max-width: 420px; padding: 40px 28px; text-align: center; }
  h1 { color: #f4f1ec; font-size: 22px; letter-spacing: 0.5px; margin-bottom: 6px; }
  p.sub { color: #a9adcf; font-size: 13px; margin-bottom: 36px; }
  a.btn {
    display: block;
    width: 100%;
    padding: 22px 16px;
    margin-bottom: 18px;
    border-radius: 14px;
    text-decoration: none;
    font-size: 19px;
    font-weight: 600;
    letter-spacing: 0.3px;
    color: #ffffff;
    box-shadow: 0 6px 16px rgba(0,0,0,0.25);
  }
  a.register { background: #3d6bf5; }
  a.checkin  { background: #17a673; }
  a.network  { background: #6c5ce7; }
  a.btn:active { transform: translateY(1px); box-shadow: 0 3px 8px rgba(0,0,0,0.25); }
</style>
</head>
<body>
  <div class="panel">
    <h1>EventHub Portable</h1>
    <p class="sub">Tent Decor Expo UP 2026 &middot; Hub Station</p>
    <a class="btn register" href="/register">Register</a>
    <a class="btn checkin" href="/check_in">Check-in</a>
    <a class="btn network" href="/network">Network</a>
  </div>
</body>
</html>
"""

NETWORK_HTML = """<!DOCTYPE html>
<html lang="en">
<head>
<meta charset="UTF-8">
<meta name="viewport" content="width=device-width, initial-scale=1.0">
<title>Network &amp; Sync — EventHub Portable</title>
<style>
  * { box-sizing: border-box; margin: 0; padding: 0; }
  body {
    font-family: -apple-system, "Segoe UI", Roboto, Helvetica, Arial, sans-serif;
    min-height: 100vh;
    background: #14162b;
    color: #eef0f7;
    padding: 28px 20px 60px;
  }
  .wrap { max-width: 480px; margin: 0 auto; }
  a.back { display: inline-block; color: #a9adcf; text-decoration: none; font-size: 14px; margin-bottom: 18px; }
  h1 { font-size: 20px; margin-bottom: 24px; }
  .card { background: #1e2140; border-radius: 14px; padding: 20px; margin-bottom: 20px; }
  .row { display: flex; align-items: center; justify-content: space-between; margin-bottom: 14px; }
  .label { color: #a9adcf; font-size: 13px; text-transform: uppercase; letter-spacing: 0.06em; }
  .badge { display: inline-block; padding: 5px 14px; border-radius: 999px; font-size: 13px; font-weight: 700; }
  .badge.idle    { background: #2c3160; color: #9db2ff; }
  .badge.syncing { background: #4a3a12; color: #f6c453; }
  .badge.error   { background: #4a1a1a; color: #ff8080; }
  button#syncBtn {
    width: 100%;
    padding: 16px;
    border: none;
    border-radius: 12px;
    background: #6c5ce7;
    color: #fff;
    font-size: 16px;
    font-weight: 700;
    cursor: pointer;
  }
  button#syncBtn:disabled { background: #3a3d5c; cursor: not-allowed; }
  pre#summary {
    background: #101127;
    border-radius: 10px;
    padding: 14px;
    font-size: 12px;
    line-height: 1.5;
    overflow-x: auto;
    max-height: 320px;
    overflow-y: auto;
    color: #c9cce0;
    white-space: pre-wrap;
    word-break: break-word;
  }
</style>
</head>
<body>
  <div class="wrap">
    <a class="back" href="/">&larr; Back to Home</a>
    <h1>Network &amp; Cloud Sync</h1>

    <div class="card">
      <div class="row">
        <span class="label">Status</span>
        <span id="state" class="badge idle">—</span>
      </div>
      <button id="syncBtn">Trigger Full Sync</button>
    </div>

    <div class="card">
      <div class="label" style="margin-bottom:10px;">Last Sync Summary</div>
      <pre id="summary">No sync has run yet.</pre>
    </div>
  </div>

<script>
  var stateEl = document.getElementById('state');
  var summaryEl = document.getElementById('summary');
  var syncBtn = document.getElementById('syncBtn');

  function renderStatus(data) {
    var state = data.state || 'IDLE';
    stateEl.textContent = state;
    stateEl.className = 'badge ' + state.toLowerCase();
    syncBtn.disabled = (state === 'SYNCING');
    if (data.last_summary && Object.keys(data.last_summary).length > 0) {
      summaryEl.textContent = JSON.stringify(data.last_summary, null, 2);
    }
  }

  function fetchStatus() {
    fetch('/api/sync/status')
      .then(function (r) { return r.json(); })
      .then(renderStatus)
      .catch(function (err) { console.error('Status check failed:', err); });
  }

  syncBtn.addEventListener('click', function () {
    syncBtn.disabled = true;
    fetch('/api/sync/start', { method: 'POST' })
      .then(function (r) { return r.json(); })
      .then(function () { fetchStatus(); })
      .catch(function (err) { console.error('Could not start sync:', err); });
  });

  fetchStatus();
  setInterval(fetchStatus, 3000);
</script>
</body>
</html>
"""


# ═══════════════════════════════════════════════════════════════════
#  HELPERS — LAN IP detection + QR code rendering
# ═══════════════════════════════════════════════════════════════════
def get_local_ip() -> str:
    """
    Best-effort detection of this laptop's LAN-facing IP address.

    Opens a UDP socket "connected" to a public address — for UDP this
    never actually sends a packet, it just makes the OS pick which
    outbound network interface would be used — then reads that socket's
    own address. Falls back to 127.0.0.1 if there is no network
    interface up yet (e.g. the hub laptop's hotspot hasn't been turned
    on), so this can never raise and block the "Start Server" button.
    """
    sock = socket.socket(socket.AF_INET, socket.SOCK_DGRAM)
    try:
        sock.connect(("8.8.8.8", 80))
        ip = sock.getsockname()[0]
    except OSError:
        ip = "127.0.0.1"
    finally:
        sock.close()
    return ip


def make_qr_photo(data: str, box_size: int = 240) -> "ImageTk.PhotoImage":
    """
    Builds a Tkinter-displayable PhotoImage of a QR code encoding `data`.
    Only ever called when QR_LIBS_AVAILABLE is True.

    Renders through an in-memory PNG buffer via the QR image's own
    documented .save() method rather than poking at qrcode's internal
    image-wrapper attributes directly, so this keeps working across
    qrcode/Pillow versions. `version` is deliberately left unset — the
    hub URL varies in length with the LAN IP, so qr.make(fit=True) must
    be free to pick a QR version large enough to hold it.
    """
    qr = qrcode.QRCode(
        error_correction=qrcode.constants.ERROR_CORRECT_M,
        box_size=8,
        border=4,
    )
    qr.add_data(data)
    qr.make(fit=True)
    raw_img = qr.make_image(fill_color="black", back_color="white")

    buf = io.BytesIO()
    raw_img.save(buf)
    buf.seek(0)

    pil_img = Image.open(buf).convert("RGB")
    pil_img = pil_img.resize((box_size, box_size), Image.LANCZOS)
    return ImageTk.PhotoImage(pil_img)


# ═══════════════════════════════════════════════════════════════════
#  CENTRALIZED WRITE QUEUE — the only path allowed to write to SQLite
#  while the hub is live. register.py / check_in.py (next steps) should
#  call enqueue_write() instead of opening their own sqlite_session(),
#  so 8 phones scanning at once are always serialized through one thread.
# ═══════════════════════════════════════════════════════════════════
write_queue: "queue.Queue" = queue.Queue()


def enqueue_write(func: Callable[..., Any], *args: Any, **kwargs: Any) -> None:
    """
    Schedules a database write to run serially on the write-worker thread.

    `func` is called as func(conn, *args, **kwargs), where `conn` is a
    sqlite3.Connection opened via db.sqlite_session() (auto-commit on
    success, auto-rollback on exception — see schema.py). Example, from
    a future file:

        from app.server_hub import enqueue_write

        def _insert_attendee(conn, attendee_id, full_name):
            conn.execute(
                "INSERT INTO attendees (id, attendee_id, full_name) "
                "VALUES (?, ?, ?);",
                (attendee_id, attendee_id, full_name),
            )

        enqueue_write(_insert_attendee, "AT00123", "Jane Doe")
    """
    write_queue.put((func, args, kwargs))


def _write_worker() -> None:
    """
    Runs forever on its own daemon thread, pulling one write job at a
    time off `write_queue` and executing it inside a single SQLite
    session. This is the only place in the running app that is meant to
    write to SQLite — it's what keeps 8 simultaneous phones from ever
    colliding on the database file lock. A failing job is logged and
    dropped; it never takes the worker thread down, so one bad payload
    can't stall every write behind it.
    """
    db = get_manager()
    logger.info("Write-queue worker thread started.")
    while True:
        func, args, kwargs = write_queue.get()
        try:
            with db.sqlite_session() as conn:
                func(conn, *args, **kwargs)
        except Exception:
            logger.exception(
                "Write-queue job failed: %s", getattr(func, "__name__", func)
            )
        finally:
            write_queue.task_done()


# ═══════════════════════════════════════════════════════════════════
#  FLASK SERVER — the web API the 8 phones and the hub laptop talk to
# ═══════════════════════════════════════════════════════════════════
flask_app = Flask(__name__)


@flask_app.route("/")
def home():
    return HOME_HTML


@flask_app.route("/network")
def network_page():
    return NETWORK_HTML


@flask_app.route("/api/sync/start", methods=["POST"])
def api_sync_start():
    def _run_background_sync() -> None:
        try:
            get_sync_manager().trigger_full_sync()
        except Exception:
            # trigger_full_sync() already catches everything internally
            # and reflects failures in its own state/summary — this is
            # only a last-resort net in case something outside that
            # try/except (e.g. lock acquisition) ever misbehaves.
            logger.exception("Unexpected error while running background sync.")

    threading.Thread(
        target=_run_background_sync, name="SyncTriggerThread", daemon=True
    ).start()
    return jsonify({"status": "started"})


@flask_app.route("/api/sync/status", methods=["GET"])
def api_sync_status():
    sync_mgr = get_sync_manager()
    return jsonify({
        "state": sync_mgr.get_state(),
        "last_summary": sync_mgr.get_last_summary(),
    })


# ═══════════════════════════════════════════════════════════════════
#  TKINTER GUI — the laptop operator's dashboard
# ═══════════════════════════════════════════════════════════════════
class ServerHubApp:
    """
    Owns the lifecycle of the Flask/Werkzeug HTTPS server from the
    Tkinter side: starting it on a background daemon thread, stopping it
    cleanly through werkzeug's make_server() (which — unlike app.run() —
    exposes a real .shutdown()), and keeping the LAN IP / URL / QR
    display in sync with whether the server is actually running.
    """

    HOST = "0.0.0.0"
    PORT = 5000

    def __init__(self, root: tk.Tk) -> None:
        self.root = root
        self.root.title("EventHub Portable — Hub Control Panel (TDE UP 2026)")
        self.root.geometry("440x640")
        self.root.minsize(400, 580)
        self.root.protocol("WM_DELETE_WINDOW", self._on_close)

        self.httpd: Optional[Any] = None
        self.server_thread: Optional[threading.Thread] = None
        self._qr_photo: Optional[Any] = None  # keep a live ref - Tk needs this

        self.status_var = tk.StringVar(value="\u25CF Server stopped")
        self.url_var = tk.StringVar(value="Not running")

        self._build_ui()

    # ─────────────────────────────────────────────────────────────
    # UI CONSTRUCTION
    # ─────────────────────────────────────────────────────────────
    def _build_ui(self) -> None:
        ttk.Label(
            self.root, text="EventHub Portable", font=("Segoe UI", 16, "bold")
        ).pack(pady=(20, 0))
        ttk.Label(
            self.root, text="TDE UP 2026 — Hub Station", foreground="#666666"
        ).pack(pady=(0, 16))

        controls = ttk.Frame(self.root)
        controls.pack(pady=8)
        self.start_btn = ttk.Button(
            controls, text="Start Server", command=self.start_server
        )
        self.start_btn.grid(row=0, column=0, padx=6)
        self.stop_btn = ttk.Button(
            controls, text="Stop Server", command=self.stop_server, state=tk.DISABLED
        )
        self.stop_btn.grid(row=0, column=1, padx=6)

        ttk.Label(
            self.root, textvariable=self.status_var, font=("Segoe UI", 11, "bold")
        ).pack(pady=(16, 4))

        url_frame = ttk.Frame(self.root)
        url_frame.pack(padx=16, pady=6, fill="x")
        ttk.Label(url_frame, text="URL:").pack(side="left")
        url_entry = ttk.Entry(
            url_frame, textvariable=self.url_var, justify="center", state="readonly"
        )
        url_entry.pack(side="left", fill="x", expand=True, padx=(8, 0))

        self.qr_label = ttk.Label(
            self.root,
            text="QR code will appear here\nonce the server is started.",
            justify="center",
            anchor="center",
        )
        self.qr_label.pack(pady=20, ipadx=10, ipady=10)

        ttk.Label(
            self.root,
            text=(
                "Point a phone's camera at the QR code to open the "
                "check-in station.\nPhones will show a security warning "
                "the first time (self-signed certificate) —\ntap "
                "Advanced \u2192 Proceed. This is expected, not an error."
            ),
            foreground="#888888",
            justify="center",
            font=("Segoe UI", 8),
        ).pack(side="bottom", pady=16, padx=12)

    # ─────────────────────────────────────────────────────────────
    # SERVER LIFECYCLE
    # ─────────────────────────────────────────────────────────────
    def start_server(self) -> None:
        if self.httpd is not None:
            return  # already running - ignore a double click

        ip = get_local_ip()

        try:
            ssl_ctx = generate_adhoc_ssl_context()
            self.httpd = make_server(
                self.HOST, self.PORT, flask_app, threaded=True, ssl_context=ssl_ctx
            )
        except ImportError as e:
            messagebox.showerror(
                "Missing dependency",
                "HTTPS requires the 'cryptography' package.\n\n"
                f"Install it with:  pip install cryptography\n\n({e})",
            )
            return
        except OSError as e:
            messagebox.showerror(
                "Could not start server",
                f"Could not bind to port {self.PORT}. It may already be "
                "in use by another program (or a previous run that "
                f"didn't shut down cleanly).\n\n{e}",
            )
            return
        except Exception as e:  # noqa: BLE001 - surface any startup failure to the operator
            logger.exception("Unexpected error starting the HTTPS server.")
            messagebox.showerror("Could not start server", str(e))
            return

        self.server_thread = threading.Thread(
            target=self.httpd.serve_forever, name="FlaskServerThread", daemon=True
        )
        self.server_thread.start()

        url = f"https://{ip}:{self.PORT}"
        self.status_var.set("\u25CF Server RUNNING")
        self.url_var.set(url)
        self._update_qr(url)
        self.start_btn.config(state=tk.DISABLED)
        self.stop_btn.config(state=tk.NORMAL)
        logger.info("HTTPS hub server started at %s", url)

    def stop_server(self) -> None:
        if self.httpd is None:
            return
        self.stop_btn.config(state=tk.DISABLED)
        # shutdown() blocks until serve_forever() returns, so it runs on
        # its own thread rather than the Tk main thread - the GUI must
        # never freeze while the socket closes.
        threading.Thread(target=self._shutdown_worker, daemon=True).start()

    def _shutdown_worker(self) -> None:
        httpd = self.httpd
        self.httpd = None
        try:
            httpd.shutdown()
            httpd.server_close()
        except Exception:
            logger.exception("Error while stopping the HTTPS server.")
        self.root.after(0, self._on_server_stopped)

    def _on_server_stopped(self) -> None:
        self.status_var.set("\u25CF Server stopped")
        self.url_var.set("Not running")
        self.qr_label.config(
            image="", text="QR code will appear here\nonce the server is started."
        )
        self._qr_photo = None
        self.start_btn.config(state=tk.NORMAL)
        self.stop_btn.config(state=tk.DISABLED)
        logger.info("HTTPS hub server stopped.")

    def _update_qr(self, url: str) -> None:
        if not QR_LIBS_AVAILABLE:
            self.qr_label.config(
                text="qrcode / Pillow not installed.\nRun: pip install qrcode Pillow",
                image="",
            )
            return
        try:
            photo = make_qr_photo(url)
        except Exception:
            logger.exception("Failed to generate QR code.")
            self.qr_label.config(text="Could not generate QR code.", image="")
            return
        self._qr_photo = photo  # keep a strong reference - Tk drops GC'd images
        self.qr_label.config(image=photo, text="")

    def _on_close(self) -> None:
        if self.httpd is not None:
            try:
                self.httpd.shutdown()
                self.httpd.server_close()
            except Exception:
                logger.exception("Error stopping server during window close.")
        self.root.destroy()


# ═══════════════════════════════════════════════════════════════════
#  APPLICATION ENTRY POINT
# ═══════════════════════════════════════════════════════════════════
def main() -> None:
    root = tk.Tk()
    root.withdraw()  # stay hidden until DB init is confirmed to succeed

    db = get_manager()
    try:
        status = db.initialize_databases()
        logger.info("Database initialization status: %s", status)
    except SchemaInitializationError as e:
        logger.critical("Fatal database initialization error: %s", e)
        messagebox.showerror(
            "EventHub Portable — Fatal Error",
            "The local SQLite database could not be initialized, so the "
            f"hub server cannot start.\n\n{e}",
        )
        root.destroy()
        return

    threading.Thread(
        target=_write_worker, name="WriteQueueWorker", daemon=True
    ).start()

    root.deiconify()
    ServerHubApp(root)
    root.mainloop()


if __name__ == "__main__":
    main()