from __future__ import annotations

import os
import base64
import sqlite3
import json
import hashlib
import ipaddress
import uuid
import secrets
import hmac
import re
import time
import urllib.error
import urllib.request
from contextlib import contextmanager
from datetime import datetime, date
from pathlib import Path
from typing import Optional

from fastapi import FastAPI, HTTPException, Request
from fastapi.middleware.cors import CORSMiddleware
from fastapi.responses import HTMLResponse, JSONResponse
from fastapi.staticfiles import StaticFiles
from pydantic import BaseModel, Field

ROOT = Path(__file__).resolve().parent.parent
DB_PATH = Path(os.getenv("JINI_DB_PATH", str(ROOT / "data" / "jini_monto.db")))
if not DB_PATH.is_absolute():
    DB_PATH = ROOT / DB_PATH
DB_PATH = DB_PATH.resolve()
DB_PATH.parent.mkdir(parents=True, exist_ok=True)
ADMIN_TOKEN_PATH = ROOT / ".admin_token"


def load_admin_token() -> str:
    token = os.getenv("JINI_ADMIN_TOKEN", "").strip()
    if token:
        return token.strip()
    try:
        token = ADMIN_TOKEN_PATH.read_text(encoding="utf-8").strip()
    except FileNotFoundError:
        token = secrets.token_urlsafe(32)
        try:
            with ADMIN_TOKEN_PATH.open("x", encoding="utf-8") as token_file:
                token_file.write(token)
        except FileExistsError:
            token = ADMIN_TOKEN_PATH.read_text(encoding="utf-8").strip()
    if len(token) < 32:
        raise RuntimeError("JINI admin token must be at least 32 characters")
    return token


ADMIN_TOKEN = load_admin_token()
DEVICE_TOKEN_PATH = ROOT / ".device_token"
DEVICE_TOKEN = os.getenv("JINI_DEVICE_TOKEN", "").strip()
if not DEVICE_TOKEN:
    try:
        DEVICE_TOKEN = DEVICE_TOKEN_PATH.read_text(encoding="utf-8").strip()
    except FileNotFoundError:
        DEVICE_TOKEN = secrets.token_urlsafe(32)
        DEVICE_TOKEN_PATH.write_text(DEVICE_TOKEN, encoding="utf-8")
MANAGER_SESSION_SECONDS = 8 * 60 * 60
RAZORPAY_KEY_ID = os.getenv("RAZORPAY_KEY_ID", "").strip()
RAZORPAY_KEY_SECRET = os.getenv("RAZORPAY_KEY_SECRET", "").strip()
RAZORPAY_WEBHOOK_SECRET = os.getenv("RAZORPAY_WEBHOOK_SECRET", "").strip()
PUBLIC_ORDER_RATE_LIMIT = 20
PUBLIC_ORDER_RATE_WINDOW_SECONDS = 60
_public_order_attempts: dict[str, list[float]] = {}

app = FastAPI(
    title="JINI MONTO Café API",
    description="Software-only prototype. Payments and dispensing are simulated.",
    version="0.1.0",
)
cors_origins = [origin.strip() for origin in os.getenv("JINI_CORS_ORIGINS", "").split(",") if origin.strip()]
if cors_origins:
    app.add_middleware(
        CORSMiddleware,
        allow_origins=cors_origins,
        allow_methods=["GET", "POST", "OPTIONS"],
        allow_headers=["Content-Type"],
        allow_credentials=False,
    )
app.mount("/static", StaticFiles(directory=str(ROOT / "static")), name="static")


@app.middleware("http")
async def protect_api_and_set_security_headers(request: Request, call_next):
    path = request.url.path
    public_api = (
        (request.method == "OPTIONS" and path.startswith("/api/"))
        or
        (request.method == "GET" and path in {"/api/health", "/api/menu"})
        or (request.method == "GET" and path == "/api/auth/status")
        or (request.method == "POST" and path == "/api/auth/login/pin")
        or (request.method == "POST" and path == "/api/orders")
        or (request.method == "POST" and re.fullmatch(r"/api/orders/[A-Za-z0-9_-]+/razorpay", path))
        or (request.method == "POST" and path == "/api/payments/razorpay/webhook")
        or (request.method == "POST" and path == "/api/payments/razorpay/verify")
        or (request.method == "GET" and path.startswith("/api/payments/razorpay/status/"))
    )
    if path.startswith("/api/") and not public_api:
        supplied = request.headers.get("X-JINI-Admin-Token", "")
        device_supplied = request.headers.get("X-JINI-Device-Token", "")
        device_route = path in {"/api/device/orders/next"} or path.startswith("/api/device/orders/")
        device_ok = device_route and hmac.compare_digest(device_supplied, DEVICE_TOKEN)
        if not device_ok and not hmac.compare_digest(supplied, ADMIN_TOKEN) and not valid_manager_session(supplied):
            return JSONResponse(status_code=401, content={"detail": "Manager token required or invalid"})
    if request.method == "POST" and path == "/api/orders":
        client_ip = request.client.host if request.client else "unknown"
        now = time.monotonic()
        recent = [t for t in _public_order_attempts.get(client_ip, [])
                  if now - t < PUBLIC_ORDER_RATE_WINDOW_SECONDS]
        if len(recent) >= PUBLIC_ORDER_RATE_LIMIT:
            return JSONResponse(status_code=429, content={"detail": "Too many order attempts"})
        recent.append(now)
        _public_order_attempts[client_ip] = recent
    response = await call_next(request)
    response.headers.setdefault("X-Content-Type-Options", "nosniff")
    response.headers.setdefault("X-Frame-Options", "DENY")
    response.headers.setdefault("Referrer-Policy", "no-referrer")
    response.headers.setdefault("Permissions-Policy", "camera=(), microphone=(), geolocation=()")
    if path.startswith("/api/"):
        response.headers["Cache-Control"] = "no-store"
    return response

DENOMINATIONS = [500, 200, 100, 50, 20, 10, 5, 2, 1]
DEMO_STAFF = [
    ("Rahul Patel", "staff"),
    ("Staff Member B", "staff"),
    ("Staff Member C", "staff"),
    ("Admin", "admin"),
]
DEMO_MENU = [
    ("CC-ICE-AMERICANO", "Ice Americano", 15900),
    ("CC-COLD-FRAPPE", "Cold Frappe", 18900),
    ("CC-CARAMEL-FRAPPE", "Caramel Frappe", 21900),
    ("CC-IRISH-FRAPPE", "Irish Frappe", 21900),
    ("CC-FRENCH-VANILLA-FRAPPE", "French Vanilla Frappe", 21900),
    ("CC-HAZELNUT-FRAPPE", "Hazelnut Frappe", 21900),
    ("CC-BISCOFF-FRAPPE", "Biscoff Frappe", 21900),
    ("CC-TIRAMISU-FRAPPE", "Tiramisu Frappe", 21900),
    ("CC-CHOCO-ALMOND-FRAPPE", "Choco Almond Frappe", 21900),
    ("CC-JAVA-CHIPS-FRAPPE", "Java Chips Frappe", 21900),
    ("CC-MOCHA-FRAPPE", "Mocha Frappe", 22900),
    ("CC-COOKIES-CRUMBLE-FRAPPE", "Cookies Crumble Frappe", 22900),
    ("CC-BROWNIE-FRAPPE", "Brownie Frappe", 22900),
    ("CC-OREO-FRAPPE", "Oreo Frappe", 22900),
    ("CC-BB-SPECIAL-FRAPPE", "BB Special Frappe", 25900),
    ("SH-STRAWBERRY-SHAKE", "Strawberry Shake", 22900),
    ("SH-OREO-SHAKE", "Oreo Shake", 22900),
    ("SH-HAZELNUT", "Hazelnut", 22900),
    ("SH-KIT-KAT-SHAKE", "Kit Kat Shake", 23900),
    ("SH-TRIPLE-CHOCOLATE-SHAKE", "Triple Chocolate Shake", 23900),
    ("SH-RED-VELVET-FUDGE", "Red Velvet Fudge Shake", 23900),
    ("SH-MUD-PIE", "Mud Pie Shake", 23900),
    ("SH-CHOCO-ALMOND", "Choco Almond Shake", 23900),
    ("SH-TIRAMISU-TRUFFLE", "Tiramisu Truffle", 23900),
    ("SH-CHOCO-HAZELNUT", "Choco Hazelnut", 23900),
    ("SH-BROWNIE", "Brownie Shake", 23900),
    ("SH-COOKIES-CRUMBLE", "Cookies Crumble Shake", 24900),
    ("SH-WHITE-CHOCOLATE-KITKAT", "White Chocolate Kitkat Shake", 24900),
    ("SH-NUTELLA", "Nutella Shake", 24900),
    ("SH-OREO-STRAWBERRY", "Oreo Strawberry Shake", 24900),
    ("SH-FERRERO-ROCHER", "Ferrero Rocher Shake", 24900),
    ("SH-TIRAMISU-BROWNIE", "Tiramisu Brownie Shake", 24900),
    ("SH-MOCHA-BROWNIE", "Mocha Brownie Shake", 24900),
    ("SH-BB-SPECIAL", "BB Special", 25900),
    ("SH-BISCOFF-DELIGHT", "Biscoff Delight", 25900),
    ("HC-PREMIUM", "Premium Hot Chocolate", 17900),
    ("HC-HAZELNUT", "Hazelnut Hot Chocolate", 19900),
    ("HC-IRISH", "Irish Hot Chocolate", 19900),
    ("HC-TIRAMISU", "Tiramisu Hot Chocolate", 19900),
    ("HC-FRENCH-VANILLA", "French Vanilla Hot Chocolate", 19900),
    ("HC-NUTELLA", "Nutella Hot Chocolate", 23900),
    ("HF-ESPRESSO", "Espresso", 11900),
    ("HF-AMERICANO", "Hot Americano", 13900),
    ("HF-CAPPUCCINO", "Cappuccino", 15900),
    ("HF-LATTE", "Cafe Latte", 17900),
    ("HF-MOCHA", "Cafe Mocha", 18900),
    ("HF-IRISH-LATTE", "Irish Latte", 18900),
    ("HF-HAZELNUT-LATTE", "Hazelnut Latte", 18900),
    ("HF-VANILLA-LATTE", "Vanilla Latte", 18900),
    ("HF-TIRAMISU", "Tiramisu", 18900),
    ("HF-BISCOFF-LATTE", "Biscoff Latte", 18900),
    ("MO-MINT", "Mint Mojito", 16900),
    ("MO-STRAWBERRY", "Strawberry Mojito", 16900),
    ("MO-WATERMELON", "Watermelon", 16900),
    ("MO-KIWI", "Kiwi Mojito", 17900),
    ("MO-RED-BULL", "Red Bull", 26900),
    ("CH-MASALA", "Masala Chai", 4900),
    ("CH-ADRAK-ELAICHI", "Adrak Elaichi Chai", 5900),
    ("CH-TOAST-COMBO", "Chai + Toast Combo", 9900),
    ("IT-LEMON", "Lemon Ice Tea", 14900),
    ("IT-PEACH", "Peach Ice Tea", 14900),
    ("IT-STRAWBERRY", "Fresh Strawberry Ice Tea", 17900),
    ("IT-WATERMELON", "Watermelon Ice Tea", 15900),
    ("EX-CHEESE", "Extra Cheese", 2900),
    ("EX-MAYONNAISE", "Extra Mayonnaise", 2900),
]
LEGACY_MENU_CODES = (
    "REG-COF", "COLD-COF", "LASSI", "MANGO-LASSI",
    "SHAKE", "OREO-SHAKE", "VANILLA-SCOOP", "CHOC-SCOOP",
)


def now_iso() -> str:
    return datetime.now().astimezone().isoformat(timespec="seconds")


@contextmanager
def db():
    conn = sqlite3.connect(DB_PATH, timeout=10)
    conn.row_factory = sqlite3.Row
    conn.execute("PRAGMA foreign_keys = ON")
    try:
        yield conn
        conn.commit()
    except Exception:
        conn.rollback()
        raise
    finally:
        conn.close()


def init_db() -> None:
    with db() as con:
        con.executescript("""
        CREATE TABLE IF NOT EXISTS staff (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            name TEXT NOT NULL UNIQUE,
            role TEXT NOT NULL DEFAULT 'staff',
            monthly_salary_paise INTEGER NOT NULL DEFAULT 1500000,
            active INTEGER NOT NULL DEFAULT 1
        );
        CREATE TABLE IF NOT EXISTS menu (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            code TEXT NOT NULL UNIQUE,
            name TEXT NOT NULL,
            price_paise INTEGER NOT NULL,
            active INTEGER NOT NULL DEFAULT 1
        );
        CREATE TABLE IF NOT EXISTS orders (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            order_no TEXT NOT NULL UNIQUE,
            source TEXT NOT NULL,
            status TEXT NOT NULL DEFAULT 'awaiting_payment',
            subtotal_paise INTEGER NOT NULL,
            discount_paise INTEGER NOT NULL DEFAULT 0,
            total_paise INTEGER NOT NULL,
            customer_note TEXT NOT NULL DEFAULT '',
            idempotency_key TEXT,
            request_fingerprint TEXT,
            created_at TEXT NOT NULL
        );
        CREATE TABLE IF NOT EXISTS order_items (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            order_id INTEGER NOT NULL REFERENCES orders(id),
            product_code TEXT NOT NULL,
            product_name_snapshot TEXT NOT NULL,
            unit_price_paise INTEGER NOT NULL,
            quantity INTEGER NOT NULL,
            line_total_paise INTEGER NOT NULL
        );
        CREATE TABLE IF NOT EXISTS dispense_queue (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            order_id INTEGER NOT NULL UNIQUE REFERENCES orders(id),
            status TEXT NOT NULL DEFAULT 'queued',
            created_at TEXT NOT NULL,
            claimed_at TEXT,
            completed_at TEXT
        );
        CREATE TABLE IF NOT EXISTS payments (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            order_id INTEGER NOT NULL REFERENCES orders(id),
            method TEXT NOT NULL,
            status TEXT NOT NULL,
            amount_paise INTEGER NOT NULL,
            cash_received_paise INTEGER NOT NULL DEFAULT 0,
            change_paise INTEGER NOT NULL DEFAULT 0,
            denominations_json TEXT NOT NULL DEFAULT '{}',
            staff_id INTEGER REFERENCES staff(id),
            note TEXT NOT NULL DEFAULT '',
            created_at TEXT NOT NULL,
            UNIQUE(order_id)
        );
        CREATE TABLE IF NOT EXISTS payment_attempts (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            order_id INTEGER NOT NULL UNIQUE REFERENCES orders(id),
            provider TEXT NOT NULL DEFAULT 'razorpay',
            provider_order_id TEXT NOT NULL UNIQUE,
            amount_paise INTEGER NOT NULL,
            currency TEXT NOT NULL DEFAULT 'INR',
            status TEXT NOT NULL DEFAULT 'created',
            status_token_hash TEXT NOT NULL,
            provider_payment_id TEXT,
            created_at TEXT NOT NULL,
            updated_at TEXT NOT NULL
        );
        CREATE TABLE IF NOT EXISTS payment_webhook_events (
            event_id TEXT PRIMARY KEY,
            event_type TEXT NOT NULL,
            received_at TEXT NOT NULL,
            outcome TEXT NOT NULL
        );
        CREATE TABLE IF NOT EXISTS attendance (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            staff_id INTEGER NOT NULL REFERENCES staff(id),
            work_date TEXT NOT NULL,
            status TEXT NOT NULL,
            note TEXT NOT NULL DEFAULT '',
            edited_by TEXT NOT NULL,
            created_at TEXT NOT NULL,
            updated_at TEXT NOT NULL,
            UNIQUE(staff_id, work_date)
        );
        CREATE TABLE IF NOT EXISTS audit_log (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            actor TEXT NOT NULL,
            action TEXT NOT NULL,
            entity_type TEXT NOT NULL,
            entity_id TEXT NOT NULL,
            old_value TEXT NOT NULL DEFAULT '',
            new_value TEXT NOT NULL DEFAULT '',
            reason TEXT NOT NULL DEFAULT '',
            created_at TEXT NOT NULL
        );
        CREATE TABLE IF NOT EXISTS cash_movements (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            movement_type TEXT NOT NULL,
            amount_paise INTEGER NOT NULL,
            staff_id INTEGER REFERENCES staff(id),
            note TEXT NOT NULL DEFAULT '',
            created_at TEXT NOT NULL
        );
        CREATE TABLE IF NOT EXISTS payroll_runs (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            staff_id INTEGER NOT NULL REFERENCES staff(id),
            period TEXT NOT NULL,
            scheduled_days INTEGER NOT NULL,
            payable_days REAL NOT NULL,
            gross_paise INTEGER NOT NULL,
            status TEXT NOT NULL DEFAULT 'draft',
            created_at TEXT NOT NULL,
            UNIQUE(staff_id, period)
        );
        CREATE TABLE IF NOT EXISTS manager_sessions (
            token_hash TEXT PRIMARY KEY,
            expires_at INTEGER NOT NULL,
            created_at TEXT NOT NULL
        );
        CREATE TABLE IF NOT EXISTS manager_auth_config (
            id INTEGER PRIMARY KEY CHECK(id=1),
            pin_salt TEXT,
            pin_hash TEXT,
            pin_failures INTEGER NOT NULL DEFAULT 0,
            pin_locked_until INTEGER NOT NULL DEFAULT 0
        );
        """)

        con.execute("INSERT OR IGNORE INTO manager_auth_config(id) VALUES (1)")

        order_columns = {row["name"] for row in con.execute("PRAGMA table_info(orders)")}
        if "idempotency_key" not in order_columns:
            con.execute("ALTER TABLE orders ADD COLUMN idempotency_key TEXT")
        if "request_fingerprint" not in order_columns:
            con.execute("ALTER TABLE orders ADD COLUMN request_fingerprint TEXT")
        con.execute("CREATE UNIQUE INDEX IF NOT EXISTS idx_orders_idempotency_key "
                    "ON orders(idempotency_key) WHERE idempotency_key IS NOT NULL")

        for name, role in DEMO_STAFF:
            con.execute(
                "INSERT OR IGNORE INTO staff(name, role) VALUES (?, ?)",
                (name, role),
            )
        for code, name, price in DEMO_MENU:
            con.execute(
                """INSERT INTO menu(code, name, price_paise, active) VALUES (?, ?, ?, 1)
                   ON CONFLICT(code) DO UPDATE SET name=excluded.name,
                   price_paise=excluded.price_paise, active=1""",
                (code, name, price),
            )
        con.executemany(
            "UPDATE menu SET active=0 WHERE code=?",
            [(code,) for code in LEGACY_MENU_CODES],
        )


@app.on_event("startup")
def startup() -> None:
    init_db()


def valid_manager_session(token: str) -> bool:
    if not token:
        return False
    token_hash = hashlib.sha256(token.encode("utf-8")).hexdigest()
    with db() as con:
        row = con.execute(
            "SELECT expires_at FROM manager_sessions WHERE token_hash=?", (token_hash,)
        ).fetchone()
        return bool(row and row["expires_at"] > int(time.time()))


def create_manager_session(auth_method: str) -> dict:
    token = secrets.token_urlsafe(32)
    expires_at = int(time.time()) + MANAGER_SESSION_SECONDS
    with db() as con:
        con.execute(
            "INSERT INTO manager_sessions(token_hash, expires_at, created_at) VALUES (?, ?, ?)",
            (hashlib.sha256(token.encode("utf-8")).hexdigest(), expires_at, now_iso()),
        )
        con.execute("DELETE FROM manager_sessions WHERE expires_at<=?", (int(time.time()),))
    return {"access_token": token, "expires_in": MANAGER_SESSION_SECONDS,
            "auth_method": auth_method}


def require_recovery_token(request: Request) -> None:
    supplied = request.headers.get("X-JINI-Admin-Token", "")
    if not hmac.compare_digest(supplied, ADMIN_TOKEN):
        raise HTTPException(403, "Use the local recovery token to set up or replace manager sign-in")


def require_manager_localhost(request: Request) -> None:
    origin = request.headers.get("origin", "").rstrip("/")
    client_host = request.client.host if request.client else ""
    try:
        client_is_loopback = ipaddress.ip_address(client_host).is_loopback
    except ValueError:
        client_is_loopback = False
    if (origin != "http://localhost:8000"
            or request.url.hostname != "localhost"
            or not client_is_loopback):
        raise HTTPException(400, "Manager PIN sign-in is available only at http://localhost:8000")


def pin_digest(pin: str, salt: bytes) -> bytes:
    return hashlib.scrypt(pin.encode("utf-8"), salt=salt, n=2**14, r=8, p=1, dklen=32)


@app.get("/api/auth/status")
def manager_auth_status():
    with db() as con:
        config = con.execute("SELECT pin_hash IS NOT NULL AS configured FROM manager_auth_config WHERE id=1").fetchone()
    return {"pin_configured": bool(config["configured"])}


@app.post("/api/auth/pin/setup")
def setup_manager_pin(payload: dict, request: Request):
    require_recovery_token(request)
    pin = str(payload.get("pin", ""))
    if not re.fullmatch(r"\d{6,12}", pin):
        raise HTTPException(400, "PIN must contain 6 to 12 digits")
    salt = secrets.token_bytes(16)
    digest = pin_digest(pin, salt)
    with db() as con:
        con.execute(
            """UPDATE manager_auth_config SET pin_salt=?, pin_hash=?, pin_failures=0,
               pin_locked_until=0 WHERE id=1""",
            (base64.urlsafe_b64encode(salt).decode("ascii"),
             base64.urlsafe_b64encode(digest).decode("ascii")),
        )
    return {"pin_configured": True}


@app.post("/api/auth/login/pin")
def login_manager_pin(payload: dict, request: Request):
    require_manager_localhost(request)
    pin = str(payload.get("pin", ""))
    if not re.fullmatch(r"\d{6,12}", pin):
        raise HTTPException(400, "Enter the 6 to 12 digit manager PIN")
    with db() as con:
        con.execute("BEGIN IMMEDIATE")
        config = con.execute("SELECT * FROM manager_auth_config WHERE id=1").fetchone()
        if not config["pin_hash"]:
            raise HTTPException(409, "PIN sign-in has not been set up yet")
        now = int(time.time())
        if config["pin_locked_until"] > now:
            raise HTTPException(429, f"Too many attempts. Try again in {config['pin_locked_until'] - now} seconds")
        salt = base64.urlsafe_b64decode(config["pin_salt"].encode("ascii"))
        expected = base64.urlsafe_b64decode(config["pin_hash"].encode("ascii"))
        valid = hmac.compare_digest(pin_digest(pin, salt), expected)
        if not valid:
            failures = config["pin_failures"] + 1
            locked_until = now + 300 if failures >= 5 else 0
            con.execute(
                "UPDATE manager_auth_config SET pin_failures=?, pin_locked_until=? WHERE id=1",
                (0 if locked_until else failures, locked_until),
            )
        else:
            con.execute("UPDATE manager_auth_config SET pin_failures=0, pin_locked_until=0 WHERE id=1")
    if not valid:
        raise HTTPException(401, "Incorrect manager PIN")
    return create_manager_session("pin")


@app.post("/api/auth/logout")
def logout_manager(request: Request):
    supplied = request.headers.get("X-JINI-Admin-Token", "")
    if supplied and not hmac.compare_digest(supplied, ADMIN_TOKEN):
        token_hash = hashlib.sha256(supplied.encode("utf-8")).hexdigest()
        with db() as con:
            con.execute("DELETE FROM manager_sessions WHERE token_hash=?", (token_hash,))
    return {"locked": True}


def rowdict(row):
    return dict(row) if row is not None else None


def get_staff(con, name: str):
    row = con.execute("SELECT * FROM staff WHERE name=? AND active=1", (name,)).fetchone()
    return row


def audit(con, actor, action, entity_type, entity_id, old="", new="", reason=""):
    con.execute(
        """INSERT INTO audit_log(actor, action, entity_type, entity_id, old_value,
           new_value, reason, created_at) VALUES (?, ?, ?, ?, ?, ?, ?, ?)""",
        (actor, action, entity_type, str(entity_id), old, new, reason, now_iso()),
    )


class OrderItemIn(BaseModel):
    product_code: str
    quantity: int = Field(ge=1, le=50)


class OrderIn(BaseModel):
    source: str = Field(default="customer_kiosk", pattern="^(customer_kiosk|staff_counter)$")
    items: list[OrderItemIn] = Field(min_length=1, max_length=65)
    discount_paise: int = Field(default=0, ge=0)
    customer_note: str = Field(default="", max_length=500)
    idempotency_key: Optional[str] = Field(default=None, max_length=128,
                                            pattern=r"^[A-Za-z0-9:_-]+$")


class RazorpayVerifyIn(BaseModel):
    order_no: str = Field(min_length=3, max_length=40)
    razorpay_order_id: str = Field(min_length=5, max_length=80)
    razorpay_payment_id: str = Field(min_length=5, max_length=80)
    razorpay_signature: str = Field(min_length=32, max_length=128)
    status_token: str = Field(min_length=32, max_length=128)


class CashPaymentIn(BaseModel):
    staff_name: str
    denominations: dict[str, int]
    note: str = Field(default="", max_length=500)


class ManualPaymentReviewIn(BaseModel):
    staff_name: str = "Admin"
    received: bool
    method: str = Field(default="manual", pattern="^(manual|cash|upi|card|other)$")
    note: str = Field(default="", max_length=500)


class OrderCancelIn(BaseModel):
    actor: str = Field(default="Admin", max_length=100)
    reason: str = Field(default="", max_length=500)


class AttendanceIn(BaseModel):
    staff_name: str
    work_date: str
    status: str = Field(pattern="^(present|absent|half_day|paid_leave|unpaid_leave)$")
    actor: str = "Admin"
    reason: str = ""


class SalaryIn(BaseModel):
    staff_name: str
    period: str = Field(pattern=r"^\d{4}-\d{2}$")
    scheduled_days: int = Field(ge=1, le=31)
    paid_leave_days: float = Field(default=0, ge=0, le=31)
    monthly_salary_paise: Optional[int] = Field(default=None, ge=0)
    actor: str = "Admin"


@app.get("/", response_class=HTMLResponse)
def home():
    return (ROOT / "static" / "landing.html").read_text(encoding="utf-8")


@app.get("/manager", response_class=HTMLResponse)
def manager_page():
    return (ROOT / "static" / "index.html").read_text(encoding="utf-8")


@app.get("/employee", response_class=HTMLResponse)
def employee_page():
    return (ROOT / "static" / "employee.html").read_text(encoding="utf-8")


@app.get("/api/health")
def health():
    return {"status": "ok", "mode": os.getenv("JINI_MODE", "software-only"), "dispensing": "simulated"}


@app.get("/api/menu")
def menu():
    with db() as con:
        return [rowdict(r) for r in con.execute(
            "SELECT code, name, price_paise FROM menu WHERE active=1 ORDER BY id"
        ).fetchall()]


@app.get("/api/staff")
def staff():
    with db() as con:
        return [rowdict(r) for r in con.execute(
            "SELECT id, name, role, monthly_salary_paise FROM staff WHERE active=1 ORDER BY id"
        ).fetchall()]


@app.get("/api/employee/staff")
def employee_staff():
    """Expose employee names only for the local order-taking screen."""
    with db() as con:
        return [rowdict(r) for r in con.execute(
            "SELECT name FROM staff WHERE active=1 AND role='staff' ORDER BY id"
        ).fetchall()]


@app.get("/api/employee/orders")
def employee_orders():
    """Show only unpaid order details needed by the employee cash workflow."""
    with db() as con:
        rows = con.execute(
            "SELECT id, order_no, source, status, total_paise, created_at "
            "FROM orders WHERE status='awaiting_payment' ORDER BY id DESC LIMIT 100"
        ).fetchall()
        result = []
        for order in rows:
            item_rows = con.execute(
                "SELECT product_name_snapshot, quantity, line_total_paise "
                "FROM order_items WHERE order_id=?", (order["id"],)
            ).fetchall()
            data = rowdict(order)
            data["items"] = [rowdict(item) for item in item_rows]
            result.append(data)
        return result


@app.post("/api/orders")
def create_order(payload: OrderIn, request: Request):
    supplied = request.headers.get("X-JINI-Admin-Token", "")
    is_manager = (hmac.compare_digest(supplied, ADMIN_TOKEN)
                  or valid_manager_session(supplied))
    if not is_manager and (payload.source != "customer_kiosk" or payload.discount_paise != 0
                           or bool(payload.customer_note.strip())):
        raise HTTPException(403, "Public orders must be customer kiosk orders at listed prices")
    with db() as con:
        # Serialize idempotency lookup and order creation so retries cannot race.
        con.execute("BEGIN IMMEDIATE")
        fingerprint_data = {
            "source": payload.source,
            "items": sorted((item.product_code, item.quantity) for item in payload.items),
            "discount_paise": payload.discount_paise,
        }
        request_fingerprint = hashlib.sha256(
            json.dumps(fingerprint_data, separators=(",", ":")).encode("utf-8")
        ).hexdigest()
        if payload.idempotency_key:
            existing = con.execute(
                "SELECT id, order_no, source, status, subtotal_paise, discount_paise, "
                "total_paise, created_at, request_fingerprint FROM orders WHERE idempotency_key=?",
                (payload.idempotency_key,),
            ).fetchone()
            if existing:
                if existing["request_fingerprint"] != request_fingerprint:
                    raise HTTPException(409, "Idempotency key was already used for a different order")
                replay = rowdict(existing)
                replay.pop("request_fingerprint", None)
                replay["idempotent_replay"] = True
                return replay
        item_rows = []
        subtotal = 0
        for item in payload.items:
            product = con.execute(
                "SELECT code, name, price_paise FROM menu WHERE code=? AND active=1",
                (item.product_code,),
            ).fetchone()
            if not product:
                raise HTTPException(400, f"Unknown or inactive product: {item.product_code}")
            line_total = product["price_paise"] * item.quantity
            subtotal += line_total
            item_rows.append((product, item.quantity, line_total))
        if payload.discount_paise > subtotal:
            raise HTTPException(400, "Discount cannot exceed subtotal")
        total = subtotal - payload.discount_paise
        created = now_iso()
        cur = con.execute(
            """INSERT INTO orders(order_no, source, status, subtotal_paise,
               discount_paise, total_paise, customer_note, idempotency_key,
               request_fingerprint, created_at)
               VALUES (?, ?, 'awaiting_payment', ?, ?, ?, ?, ?, ?, ?)""",
            (f"TMP-{uuid.uuid4().hex}", payload.source, subtotal, payload.discount_paise,
             total, payload.customer_note, payload.idempotency_key, request_fingerprint, created),
        )
        order_id = cur.lastrowid
        order_no = f"JM-{1000 + order_id}"
        con.execute("UPDATE orders SET order_no=? WHERE id=?", (order_no, order_id))
        for product, quantity, line_total in item_rows:
            con.execute(
                """INSERT INTO order_items(order_id, product_code, product_name_snapshot,
                   unit_price_paise, quantity, line_total_paise) VALUES (?, ?, ?, ?, ?, ?)""",
                (order_id, product["code"], product["name"], product["price_paise"], quantity, line_total),
            )
        audit(con, payload.source, "order_created", "order", order_no, new=json.dumps({
            "subtotal_paise": subtotal, "discount_paise": payload.discount_paise, "total_paise": total
        }))
        return {
            "id": order_id, "order_no": order_no, "source": payload.source,
            "status": "awaiting_payment", "subtotal_paise": subtotal,
            "discount_paise": payload.discount_paise, "total_paise": total,
            "created_at": created,
        }


def razorpay_request(method: str, resource: str, body: Optional[dict] = None) -> dict:
    if not RAZORPAY_KEY_ID or not RAZORPAY_KEY_SECRET:
        raise HTTPException(503, "Razorpay is not configured on this server")
    credentials = base64.b64encode(
        f"{RAZORPAY_KEY_ID}:{RAZORPAY_KEY_SECRET}".encode("utf-8")
    ).decode("ascii")
    data = json.dumps(body).encode("utf-8") if body is not None else None
    req = urllib.request.Request(
        f"https://api.razorpay.com/v1/{resource.lstrip('/')}",
        data=data,
        method=method,
        headers={"Authorization": f"Basic {credentials}",
                 "Content-Type": "application/json"},
    )
    try:
        with urllib.request.urlopen(req, timeout=12) as response:
            return json.loads(response.read().decode("utf-8"))
    except urllib.error.HTTPError as exc:
        # Do not return provider responses that may contain sensitive diagnostics.
        raise HTTPException(502, "Razorpay request failed; check server configuration and gateway logs") from exc
    except (urllib.error.URLError, TimeoutError, json.JSONDecodeError) as exc:
        raise HTTPException(502, "Razorpay is temporarily unreachable") from exc


def razorpay_status_token(order_no: str) -> str:
    return hmac.new(RAZORPAY_KEY_SECRET.encode("utf-8"),
                    f"status:{order_no}".encode("utf-8"), hashlib.sha256).hexdigest()


def razorpay_checkout_response(order_no: str, order: sqlite3.Row, attempt: sqlite3.Row) -> dict:
    return {
        "provider": "razorpay",
        "key_id": RAZORPAY_KEY_ID,
        "razorpay_order_id": attempt["provider_order_id"],
        "order_no": order_no,
        "amount": attempt["amount_paise"],
        "currency": attempt["currency"],
        "status_token": razorpay_status_token(order_no),
        "name": "JINI MONTO Café",
        "description": f"Order {order_no}",
        "status": attempt["status"],
    }


@app.post("/api/orders/{order_no}/razorpay")
def create_razorpay_payment(order_no: str):
    if not RAZORPAY_KEY_ID or not RAZORPAY_KEY_SECRET:
        raise HTTPException(503, "Razorpay sandbox credentials are not configured")
    with db() as con:
        con.execute("BEGIN IMMEDIATE")
        order = con.execute("SELECT * FROM orders WHERE order_no=?", (order_no,)).fetchone()
        if not order:
            raise HTTPException(404, "Order not found")
        existing = con.execute("SELECT * FROM payment_attempts WHERE order_id=?", (order["id"],)).fetchone()
        if existing:
            return razorpay_checkout_response(order_no, order, existing)
        if order["status"] != "awaiting_payment":
            raise HTTPException(409, "Order is not awaiting payment")
        if order["total_paise"] <= 0:
            raise HTTPException(400, "Payment amount must be positive")
        # Receipt is stable per internal order; amount is always read from the database.
        remote = razorpay_request("POST", "orders", {
            "amount": order["total_paise"], "currency": "INR",
            "receipt": order_no, "notes": {"jini_order_no": order_no},
        })
        if (remote.get("currency") != "INR" or remote.get("amount") != order["total_paise"]
                or not remote.get("id")):
            raise HTTPException(502, "Razorpay returned an unexpected order")
        created = now_iso()
        con.execute(
            """INSERT INTO payment_attempts(order_id, provider_order_id, amount_paise,
               currency, status, status_token_hash, created_at, updated_at)
               VALUES (?, ?, ?, 'INR', 'created', ?, ?, ?)""",
            (order["id"], remote["id"], order["total_paise"],
             hashlib.sha256(razorpay_status_token(order_no).encode()).hexdigest(), created, created),
        )
        attempt = con.execute("SELECT * FROM payment_attempts WHERE order_id=?", (order["id"],)).fetchone()
        audit(con, "customer", "razorpay_order_created", "order", order_no,
              new=json.dumps({"amount_paise": order["total_paise"], "provider_order_id": remote["id"]}))
        return razorpay_checkout_response(order_no, order, attempt)


def record_captured_razorpay_payment(con, attempt: sqlite3.Row, payment: dict) -> str:
    if (payment.get("status") != "captured" or payment.get("order_id") != attempt["provider_order_id"]
            or payment.get("amount") != attempt["amount_paise"]
            or payment.get("currency") != "INR"):
        con.execute("UPDATE payment_attempts SET status='review_required', updated_at=? WHERE id=?",
                    (now_iso(), attempt["id"]))
        return "review_required"
    order = con.execute("SELECT * FROM orders WHERE id=?", (attempt["order_id"],)).fetchone()
    if order["status"] == "paid":
        existing = con.execute("SELECT method FROM payments WHERE order_id=?", (order["id"],)).fetchone()
        if existing and existing["method"] == "razorpay":
            con.execute("UPDATE payment_attempts SET status='paid', provider_payment_id=?, updated_at=? WHERE id=?",
                        (payment["id"], now_iso(), attempt["id"]))
            return "paid"
        con.execute("UPDATE payment_attempts SET status='review_required', provider_payment_id=?, updated_at=? WHERE id=?",
                    (payment["id"], now_iso(), attempt["id"]))
        audit(con, "razorpay", "captured_payment_conflict", "order", order["order_no"],
              reason="Order was already paid through another method")
        return "review_required"
    if order["status"] != "awaiting_payment":
        con.execute("UPDATE payment_attempts SET status='review_required', provider_payment_id=?, updated_at=? WHERE id=?",
                    (payment["id"], now_iso(), attempt["id"]))
        return "review_required"
    paid_at = now_iso()
    con.execute(
        """INSERT INTO payments(order_id, method, status, amount_paise, note, created_at)
           VALUES (?, 'razorpay', 'paid', ?, ?, ?)""",
        (order["id"], attempt["amount_paise"], f"Razorpay payment {payment['id']}", paid_at),
    )
    con.execute("UPDATE orders SET status='paid' WHERE id=? AND status='awaiting_payment'", (order["id"],))
    con.execute("UPDATE payment_attempts SET status='paid', provider_payment_id=?, updated_at=? WHERE id=?",
                (payment["id"], paid_at, attempt["id"]))
    audit(con, "razorpay", "razorpay_payment_captured", "order", order["order_no"],
          new=json.dumps({"payment_id": payment["id"], "amount_paise": attempt["amount_paise"]}))
    return "paid"


@app.post("/api/payments/razorpay/verify")
def verify_razorpay_payment(payload: RazorpayVerifyIn):
    if not RAZORPAY_KEY_SECRET:
        raise HTTPException(503, "Razorpay is not configured on this server")
    expected_sig = hmac.new(
        RAZORPAY_KEY_SECRET.encode("utf-8"),
        f"{payload.razorpay_order_id}|{payload.razorpay_payment_id}".encode("utf-8"),
        hashlib.sha256,
    ).hexdigest()
    if not hmac.compare_digest(expected_sig, payload.razorpay_signature):
        raise HTTPException(400, "Invalid Razorpay checkout signature")
    with db() as con:
        attempt = con.execute(
            """SELECT pa.*, o.order_no, o.id AS internal_order_id FROM payment_attempts pa
               JOIN orders o ON o.id=pa.order_id WHERE o.order_no=? AND pa.provider_order_id=?""",
            (payload.order_no, payload.razorpay_order_id),
        ).fetchone()
        if not attempt or not hmac.compare_digest(
                attempt["status_token_hash"], hashlib.sha256(payload.status_token.encode()).hexdigest()):
            raise HTTPException(404, "Payment attempt not found")
    payment = razorpay_request("GET", f"payments/{payload.razorpay_payment_id}")
    if payment.get("order_id") != payload.razorpay_order_id:
        raise HTTPException(400, "Payment does not belong to this Razorpay order")
    if payment.get("status") != "captured":
        with db() as con:
            con.execute("UPDATE payment_attempts SET status=?, updated_at=? WHERE id=?",
                        ("failed" if payment.get("status") == "failed" else "pending", now_iso(), attempt["id"]))
        return {"order_no": payload.order_no, "status": "pending", "payment_status": payment.get("status")}
    with db() as con:
        fresh_attempt = con.execute("SELECT * FROM payment_attempts WHERE id=?", (attempt["id"],)).fetchone()
        status = record_captured_razorpay_payment(con, fresh_attempt, payment)
    return {"order_no": payload.order_no, "status": status}


@app.post("/api/payments/razorpay/webhook")
async def razorpay_webhook(request: Request):
    if not RAZORPAY_WEBHOOK_SECRET:
        raise HTTPException(503, "Razorpay webhook secret is not configured")
    raw = await request.body()
    supplied = request.headers.get("X-Razorpay-Signature", "")
    expected = hmac.new(RAZORPAY_WEBHOOK_SECRET.encode("utf-8"), raw, hashlib.sha256).hexdigest()
    if not supplied or not hmac.compare_digest(expected, supplied):
        raise HTTPException(400, "Invalid Razorpay webhook signature")
    try:
        event = json.loads(raw)
    except (json.JSONDecodeError, UnicodeDecodeError) as exc:
        raise HTTPException(400, "Malformed webhook payload") from exc
    event_id = request.headers.get("X-Razorpay-Event-Id", "").strip()
    if not event_id or len(event_id) > 200:
        raise HTTPException(400, "Missing Razorpay event ID")
    event_type = str(event.get("event", "unknown"))[:100]
    entity = ((event.get("payload") or {}).get("payment") or {}).get("entity") or {}
    result = "ignored"
    with db() as con:
        con.execute("BEGIN IMMEDIATE")
        if con.execute("SELECT 1 FROM payment_webhook_events WHERE event_id=?", (event_id,)).fetchone():
            return {"received": True, "duplicate": True}
        if event_type == "payment.captured" and entity.get("order_id"):
            attempt = con.execute("SELECT * FROM payment_attempts WHERE provider_order_id=?",
                                  (entity.get("order_id"),)).fetchone()
            if attempt:
                result = record_captured_razorpay_payment(con, attempt, entity)
            else:
                result = "unmatched"
        con.execute("INSERT INTO payment_webhook_events(event_id, event_type, received_at, outcome) VALUES (?, ?, ?, ?)",
                    (event_id, event_type, now_iso(), result))
    return {"received": True, "outcome": result}


@app.get("/api/payments/razorpay/status/{order_no}")
def razorpay_payment_status(order_no: str, token: str):
    if not RAZORPAY_KEY_SECRET:
        raise HTTPException(503, "Razorpay is not configured on this server")
    expected_token = razorpay_status_token(order_no)
    if not hmac.compare_digest(expected_token, token):
        raise HTTPException(404, "Payment attempt not found")
    with db() as con:
        attempt = con.execute(
            """SELECT pa.status FROM payment_attempts pa JOIN orders o ON o.id=pa.order_id
               WHERE o.order_no=?""", (order_no,)
        ).fetchone()
    if not attempt:
        raise HTTPException(404, "Payment attempt not found")
    return {"order_no": order_no, "status": attempt["status"]}


@app.get("/api/orders")
def list_orders():
    with db() as con:
        orders = con.execute("SELECT * FROM orders ORDER BY id DESC LIMIT 200").fetchall()
        result = []
        for o in orders:
            item_rows = con.execute(
                "SELECT product_name_snapshot, quantity, line_total_paise FROM order_items WHERE order_id=?",
                (o["id"],),
            ).fetchall()
            p = con.execute("SELECT method, status, cash_received_paise, change_paise, note FROM payments WHERE order_id=?",
                            (o["id"],)).fetchone()
            data = rowdict(o)
            data["items"] = [rowdict(i) for i in item_rows]
            data["payment"] = rowdict(p)
            result.append(data)
        return result


@app.post("/api/orders/{order_no}/cancel")
def cancel_order(order_no: str, payload: OrderCancelIn):
    with db() as con:
        con.execute("BEGIN IMMEDIATE")
        order = con.execute("SELECT * FROM orders WHERE order_no=?", (order_no,)).fetchone()
        if not order:
            raise HTTPException(404, "Order not found")
        if order["status"] != "awaiting_payment":
            if order["status"] == "paid":
                raise HTTPException(409, "Paid orders cannot be cancelled")
            if order["status"] == "cancelled":
                raise HTTPException(409, "Order is already cancelled")
            raise HTTPException(409, "Only unpaid orders can be cancelled")
        updated = con.execute(
            "UPDATE orders SET status='cancelled' WHERE id=? AND status='awaiting_payment'",
            (order["id"],),
        )
        if updated.rowcount != 1:
            raise HTTPException(409, "Order status changed; refresh and try again")
        audit(con, payload.actor, "order_cancelled", "order", order_no,
              old="awaiting_payment", new="cancelled", reason=payload.reason)
        return {"order_no": order_no, "status": "cancelled",
                "note": "Order cancelled. No payment or dispensing was initiated."}


@app.post("/api/orders/{order_no}/cash-payment")
def confirm_cash_payment(order_no: str, payload: CashPaymentIn):
    # Prototype validation only. Production must authenticate staff and enforce roles.
    normalized = {}
    total_received = 0
    for denomination in DENOMINATIONS:
        count = payload.denominations.get(str(denomination), 0)
        if not isinstance(count, int) or isinstance(count, bool) or count < 0 or count > 10000:
            raise HTTPException(400, f"Invalid count for denomination {denomination}")
        normalized[str(denomination)] = count
        total_received += denomination * count * 100
    for key in payload.denominations:
        if key not in {str(x) for x in DENOMINATIONS}:
            raise HTTPException(400, f"Unsupported denomination: {key}")

    with db() as con:
        con.execute("BEGIN IMMEDIATE")
        order = con.execute("SELECT * FROM orders WHERE order_no=?", (order_no,)).fetchone()
        if not order:
            raise HTTPException(404, "Order not found")
        if order["status"] != "awaiting_payment":
            if order["status"] == "paid":
                raise HTTPException(409, "Order is already paid")
            raise HTTPException(409, "Only unpaid orders can be paid")
        if total_received < order["total_paise"]:
            raise HTTPException(400, "Cash received is less than order total")
        staff_row = get_staff(con, payload.staff_name)
        if not staff_row or staff_row["role"] != "staff":
            raise HTTPException(400, "An active employee is required to record cash")
        change = total_received - order["total_paise"]
        con.execute(
            """INSERT INTO payments(order_id, method, status, amount_paise, cash_received_paise,
               change_paise, denominations_json, staff_id, note, created_at)
               VALUES (?, 'cash', 'confirmed', ?, ?, ?, ?, ?, ?, ?)""",
            (order["id"], order["total_paise"], total_received, change,
             json.dumps(normalized), staff_row["id"], payload.note, now_iso()),
        )
        updated = con.execute(
            "UPDATE orders SET status='paid' WHERE id=? AND status='awaiting_payment'",
            (order["id"],),
        )
        if updated.rowcount != 1:
            raise HTTPException(409, "Order status changed; refresh and try again")
        audit(con, payload.staff_name, "cash_payment_confirmed", "order", order_no,
              new=json.dumps({"received_paise": total_received, "change_paise": change, "denominations": normalized}))
        # A real implementation would enqueue a dispensing job only after authorization.
        return {
            "order_no": order_no, "status": "paid", "method": "cash",
            "bill_total_paise": order["total_paise"], "cash_received_paise": total_received,
            "change_paise": change, "denominations": normalized, "staff_name": payload.staff_name,
            "note": payload.note, "dispensing": "not_connected"
        }


@app.post("/api/orders/{order_no}/payment-review")
def review_manual_payment(order_no: str, payload: ManualPaymentReviewIn):
    """Local prototype admin decision. No gateway integration; no dispensing."""
    with db() as con:
        con.execute("BEGIN IMMEDIATE")
        order = con.execute("SELECT * FROM orders WHERE order_no=?", (order_no,)).fetchone()
        if not order:
            raise HTTPException(404, "Order not found")
        staff_row = get_staff(con, payload.staff_name)
        if not staff_row or staff_row["role"] != "admin":
            raise HTTPException(400, "An active admin staff member is required")
        if order["status"] != "awaiting_payment":
            if order["status"] == "paid":
                raise HTTPException(409, "Order is already marked paid")
            raise HTTPException(409, "Only unpaid orders can be reviewed")
        if payload.received:
            # A human admin confirms funds were actually received. This is not gateway verification.
            con.execute(
                """INSERT INTO payments(order_id, method, status, amount_paise, cash_received_paise,
                   change_paise, denominations_json, staff_id, note, created_at)
                   VALUES (?, ?, 'confirmed', ?, 0, 0, '{}', ?, ?, ?)""",
                (order["id"], payload.method, order["total_paise"], staff_row["id"], payload.note, now_iso()),
            )
            updated = con.execute(
                "UPDATE orders SET status='paid' WHERE id=? AND status='awaiting_payment'",
                (order["id"],),
            )
            if updated.rowcount != 1:
                raise HTTPException(409, "Order status changed; refresh and try again")
            audit(con, payload.staff_name, "manual_payment_received", "order", order_no,
                  old="awaiting_payment", new=json.dumps({"status":"paid", "method":payload.method}),
                  reason=payload.note)
            return {"order_no": order_no, "status": "paid", "payment_received": True,
                    "method": payload.method, "amount_paise": order["total_paise"],
                    "note": "Manually confirmed by admin; not gateway-verified. Dispensing not connected."}
        audit(con, payload.staff_name, "manual_payment_not_received", "order", order_no,
              old=order["status"], new="awaiting_payment", reason=payload.note)
        return {"order_no": order_no, "status": "awaiting_payment", "payment_received": False,
                "note": "Order remains unpaid. Dispensing not connected."}


@app.post("/api/attendance")
def set_attendance(payload: AttendanceIn):
    try:
        date.fromisoformat(payload.work_date)
    except ValueError:
        raise HTTPException(400, "work_date must be YYYY-MM-DD")
    with db() as con:
        person = get_staff(con, payload.staff_name)
        if not person:
            raise HTTPException(400, "Unknown staff member")
        old = con.execute(
            "SELECT status, note FROM attendance WHERE staff_id=? AND work_date=?",
            (person["id"], payload.work_date),
        ).fetchone()
        old_json = json.dumps(rowdict(old)) if old else ""
        con.execute(
            """INSERT INTO attendance(staff_id, work_date, status, note, edited_by, created_at, updated_at)
               VALUES (?, ?, ?, ?, ?, ?, ?)
               ON CONFLICT(staff_id, work_date) DO UPDATE SET status=excluded.status,
               note=excluded.note, edited_by=excluded.edited_by, updated_at=excluded.updated_at""",
            (person["id"], payload.work_date, payload.status, payload.reason, payload.actor, now_iso(), now_iso()),
        )
        new_json = json.dumps({"status": payload.status, "note": payload.reason})
        audit(con, payload.actor, "attendance_upsert", "attendance", f'{person["id"]}:{payload.work_date}',
              old=old_json, new=new_json, reason=payload.reason)
        return {"staff_name": payload.staff_name, "work_date": payload.work_date,
                "status": payload.status, "edited_by": payload.actor}


@app.get("/api/attendance")
def list_attendance(work_date: Optional[str] = None):
    with db() as con:
        if work_date:
            try:
                date.fromisoformat(work_date)
            except ValueError:
                raise HTTPException(400, "work_date must be YYYY-MM-DD")
            rows = con.execute(
                """SELECT a.work_date, s.name AS staff_name, a.status, a.note, a.edited_by, a.updated_at
                   FROM attendance a JOIN staff s ON s.id=a.staff_id
                   WHERE a.work_date=? ORDER BY s.name""", (work_date,)
            ).fetchall()
        else:
            rows = con.execute(
                """SELECT a.work_date, s.name AS staff_name, a.status, a.note, a.edited_by, a.updated_at
                   FROM attendance a JOIN staff s ON s.id=a.staff_id
                   ORDER BY a.work_date DESC, s.name LIMIT 500"""
            ).fetchall()
        return [rowdict(r) for r in rows]


@app.post("/api/payroll/estimate")
def payroll_estimate(payload: SalaryIn):
    with db() as con:
        person = get_staff(con, payload.staff_name)
        if not person:
            raise HTTPException(400, "Unknown staff member")
        monthly = payload.monthly_salary_paise if payload.monthly_salary_paise is not None else person["monthly_salary_paise"]
        month_start = payload.period + "-01"
        rows = con.execute(
            "SELECT status, COUNT(*) AS n FROM attendance WHERE staff_id=? AND work_date LIKE ? GROUP BY status",
            (person["id"], payload.period + "-%"),
        ).fetchall()
        counts = {r["status"]: r["n"] for r in rows}
        present = counts.get("present", 0)
        half_day = counts.get("half_day", 0)
        unpaid_absent = counts.get("absent", 0) + counts.get("unpaid_leave", 0)
        # Simple configurable demonstration rule: present=1 day, half_day=0.5,
        # paid_leave=1; absent/unpaid_leave=0. This is not legal payroll advice.
        paid_leave = counts.get("paid_leave", 0)
        payable_days = present + half_day * 0.5 + paid_leave
        payable_days = max(0.0, min(float(payload.scheduled_days), payable_days))
        gross = round(monthly * payable_days / payload.scheduled_days)
        return {
            "staff_name": payload.staff_name, "period": payload.period,
            "monthly_salary_paise": monthly, "scheduled_days": payload.scheduled_days,
            "attendance_counts": counts, "payable_days": payable_days,
            "unpaid_absence_days": unpaid_absent, "estimated_gross_paise": gross,
            "estimated_gross_rupees": round(gross / 100, 2),
            "rule_note": "Prototype estimate only. Configure and validate employment, leave, overtime, deduction and legal rules before payroll use."
        }


@app.get("/api/reports/daily")
def daily_report(report_date: Optional[str] = None):
    report_date = report_date or date.today().isoformat()
    try:
        date.fromisoformat(report_date)
    except ValueError:
        raise HTTPException(400, "report_date must be YYYY-MM-DD")
    with db() as con:
        rows = con.execute(
            """SELECT p.*, o.order_no, o.total_paise, s.name AS staff_name
               FROM payments p JOIN orders o ON o.id=p.order_id
               LEFT JOIN staff s ON s.id=p.staff_id
               WHERE substr(p.created_at,1,10)=? AND p.status='confirmed'""",
            (report_date,),
        ).fetchall()
        total_received = sum(r["cash_received_paise"] for r in rows if r["method"] == "cash")
        change = sum(r["change_paise"] for r in rows if r["method"] == "cash")
        cash_sales = sum(r["amount_paise"] for r in rows if r["method"] == "cash")
        refunds = con.execute(
            "SELECT COALESCE(SUM(amount_paise),0) AS total FROM cash_movements WHERE movement_type='cash_refund' AND substr(created_at,1,10)=?",
            (report_date,),
        ).fetchone()["total"]
        discounts = con.execute(
            "SELECT COALESCE(SUM(discount_paise),0) AS total FROM orders WHERE substr(created_at,1,10)=?",
            (report_date,),
        ).fetchone()["total"]
        gross_sales = con.execute(
            "SELECT COALESCE(SUM(subtotal_paise),0) AS total FROM orders WHERE substr(created_at,1,10)=? AND status='paid'",
            (report_date,),
        ).fetchone()["total"]
        net_sales = con.execute(
            "SELECT COALESCE(SUM(total_paise),0) AS total FROM orders WHERE substr(created_at,1,10)=? AND status='paid'",
            (report_date,),
        ).fetchone()["total"]
        return {
            "date": report_date,
            "confirmed_cash_tendered_paise": total_received,
            "change_returned_paise": change,
            "net_cash_sales_before_cash_refunds_paise": cash_sales,
            "cash_refunds_paise": refunds,
            "discounts_applied_paise": discounts,
            "gross_sales_before_discounts_paise": gross_sales,
            "net_sales_after_discounts_paise": net_sales,
            "payment_count": len(rows),
            "staff_cash_summary": [
                {"staff_name": name,
                 "cash_received_paise": sum(r["cash_received_paise"] for r in rows if r["method"]=="cash" and r["staff_name"]==name),
                 "change_paise": sum(r["change_paise"] for r in rows if r["method"]=="cash" and r["staff_name"]==name)}
                for name in sorted({r["staff_name"] for r in rows if r["method"]=="cash" and r["staff_name"]})
            ],
            "note": "Prototype report. Opening float, cash additions/removals, non-cash provider reconciliation and refund workflow are not yet implemented."
        }


@app.get("/api/audit")
def get_audit(limit: int = 100):
    limit = max(1, min(limit, 500))
    with db() as con:
        return [rowdict(r) for r in con.execute(
            "SELECT * FROM audit_log ORDER BY id DESC LIMIT ?", (limit,)
        ).fetchall()]
