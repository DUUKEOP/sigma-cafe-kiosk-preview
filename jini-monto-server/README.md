# JINI MONTO Automated Café — Starter Application

This is a runnable **software-first prototype**, not a production-ready café/POS system.
It includes a FastAPI backend, SQLite database, browser UI, a Razorpay Standard Checkout
integration that can be configured for sandbox use, cash workflows, attendance examples,
basic salary estimates, and a daily cash report. Dispensing is simulated and does not control hardware.

## Requirements
- Python 3.10+
- Internet access for the first `pip install` (or preinstalled dependencies)

## Run on Windows (PowerShell)
```powershell
cd jini-monto-server
py -m venv .venv
.\.venv\Scripts\Activate.ps1
python -m pip install --upgrade pip
pip install -r requirements.txt
uvicorn app.main:app --host 127.0.0.1 --port 8000
```
Open http://127.0.0.1:8000

## Run on macOS/Linux
```bash
cd jini-monto-server
python3 -m venv .venv
source .venv/bin/activate
python -m pip install --upgrade pip
pip install -r requirements.txt
uvicorn app.main:app --host 127.0.0.1 --port 8000
```

## Included demo workflow
1. Create an order from the browser interface.
2. On the admin panel, enter denomination counts and confirm cash.
3. Mark attendance for demo staff and enter a monthly salary.
4. View daily summary and expected cash calculation.

## API docs
When running, open http://127.0.0.1:8000/docs

## Important limitations
- SQLite is used to make local testing simple. Move to PostgreSQL before multi-user production.
- Demo staff accounts are seeded. Manager API calls require a local admin token, but the prototype has no separate employee accounts or production identity system.
- Razorpay code requires merchant sandbox credentials and public HTTPS/webhook deployment before it can be exercised. Live payments, GST invoice compliance, cloud synchronization, payroll compliance, and hardware integration are not enabled here.
- The dispensing controller is simulated only. Never connect real loads using this prototype.
- Do not use real customer/staff data or real payments until authentication, authorization, privacy,
  backups, security review, tax rules and accounting workflows are implemented and tested.


## Communication release (2026-10)

This version connects the ESP32 TCP bridge to the local FastAPI order API.
Run API and bridge in separate terminals. The bridge uses menu indexes 0..64
from the active ESP32 menu and maps them to the matching API product codes.
The 65 items cover the eight categories shown in the kiosk. It ignores the
ESP32's total for billing; the API calculates price from its menu.
Orders appear as `awaiting_payment`. The Orders table includes local admin
buttons to mark payment received or not received. This is a manual prototype
review, not payment-gateway verification. No dispensing is connected.

On first API launch, a random local manager token is stored in `.admin_token`.
The dashboard prompts for this value and the bridge reads the same local file.
If you override it with `JINI_ADMIN_TOKEN`, set the same value for both processes.
Keep that file private. The token is sent over HTTP without TLS, so keep the API
and bridge on a trusted private network for development; do not expose either
to the public internet. Manager labels remain workflow descriptions, not
separate employee accounts. The kiosk now stores its sequence number in ESP32 NVS
and sends a stable device ID. The API stores an idempotency key and request
fingerprint, so retries return the original order and changed content under the
same key is rejected. Older firmware remains temporarily supported with a
weaker peer-address-based key until it is updated.

For the active kiosk, copy `SigmaCafe_LVGL_480x800/kiosk_config.h.example` to
`kiosk_config.h`, enter the Wi-Fi details and PC address, then configure a DHCP
reservation for that PC in the router. The local kiosk config is ignored by git.

Restart commands from the project folder:
- API: `python -m uvicorn app.main:app --host 127.0.0.1 --port 8000`
- Bridge: `python -m app.tcp_bridge`

The API binds to loopback; only the TCP bridge exposes an ESP32-facing socket,
bound by default to the kiosk PC's reserved Wi-Fi address `192.168.1.16:5000`.
The bridge accepts signed HMAC-SHA256 messages from the paired kiosk only.
Keep TCP port 5000 on the trusted store LAN; it is not a public internet service.

## Employee and manager workspaces

- `/employee` is for counter orders and cash collection. It can create staff-counter orders, view unpaid orders, and record cash received by an active employee.
- `/manager` is for payment review, order cancellation, attendance, payroll estimates, daily reports, and audit history. Manager API actions require a Manager PIN session or the recovery token in `.admin_token`.
- `/` is the workspace chooser.

Employee and manager tools use separate pages and the API keeps manager-only endpoints behind manager sign-in. This software prototype does not yet provide individual employee sign-in; employee operations are intended for a trusted store network only. Do not treat the employee name selector as authentication.

## Manager sign-in

- Use the optional 6–12 digit manager PIN at `http://localhost:8000/manager` on the Windows PC running the API. PIN sign-in is restricted to this local origin.
- The PIN is stored as a salted scrypt hash. Five incorrect attempts cause a five-minute lockout. Successful PIN sign-in creates an eight-hour manager session; **Lock Manager** revokes that session and hides manager data immediately.
- Keep `.admin_token` as the recovery credential. Use it to unlock Manager tools or set/change the PIN. Do not expose it or use this prototype over an untrusted network.
- Install dependencies after updating this project with `python -m pip install -r requirements.txt`, then restart the API.

## Razorpay Checkout (sandbox configuration)

The customer checkout in `../github-pages-kiosk/index.html` now creates an order through
FastAPI, opens Razorpay Standard Checkout, and asks the server to verify the checkout
signature. Only a captured payment verified by the server changes the order to `paid`.
Webhook handling verifies `X-Razorpay-Signature` over the raw request bytes and deduplicates
events by `X-Razorpay-Event-Id`. The server never receives card numbers or UPI PINs.

Configure these environment variables on the API host, using **Razorpay Test Mode** values
first: `RAZORPAY_KEY_ID`, `RAZORPAY_KEY_SECRET`, and `RAZORPAY_WEBHOOK_SECRET`. Set
`JINI_CORS_ORIGINS` to the exact HTTPS origin(s) hosting the kiosk page, comma-separated;
for the current GitHub Pages site this is `https://duukeop.github.io`. On the kiosk page,
set `<meta name="jini-api-base" content="https://YOUR-API-HOST">` to the API's HTTPS origin.
Register the webhook URL `https://YOUR-API-HOST/api/payments/razorpay/webhook` in Razorpay
Test Mode and subscribe to `payment.captured`; use the same webhook secret on the server.
The Razorpay key ID is public checkout configuration; the key secret and webhook secret
must remain server-only environment secrets and must never be committed or put in firmware.

The current local API binds to `127.0.0.1:8000`; Razorpay cannot deliver webhooks to it and
remote customers cannot reach it. Local checkout therefore remains unavailable until the
API is deployed behind public HTTPS and the kiosk's API base/CORS origin are configured.
The current SQLite database is not suitable for a multi-instance or ephemeral cloud host.
Before live mode, migrate to durable managed storage with backups, add durable abuse limits
and operational monitoring, configure merchant capture/UPI/card methods, and complete a
sandbox reconciliation review. The current public create-order throttle is per-process and
is only a development guard. Payment confirmation does not trigger real motor dispensing.
