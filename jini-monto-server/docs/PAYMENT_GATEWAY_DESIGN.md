# JINI MONTO Payment Gateway Design

Status: Razorpay sandbox integration code is present in the API and public kiosk page. Merchant credentials, public HTTPS deployment, durable production storage, and live payments are not configured. Payment confirmation does not initiate physical dispensing.

## Goal

Let a kiosk customer choose between **Pay at Counter** (cash) and **Pay Here with UPI** (phone-scanned QR), while keeping payment confirmation trustworthy and keeping payment secrets off the ESP32. The current project is a local prototype; live payment processing requires a production deployment and gateway merchant onboarding.

## Proposed customer flow

1. A customer web client (or ESP32 kiosk) sends an order to FastAPI. FastAPI remains the source of truth for menu prices and calculates the final INR amount. The current public GitHub Pages preview is not connected to this API.
2. The kiosk shows two payment choices: **Pay at Counter** and **Pay Here with UPI**.
3. If the customer chooses **Pay at Counter**, the order remains `awaiting_payment`. The kiosk shows the order number and amount; an employee collects cash and uses the existing cash-payment flow to record tender and any change. The kiosk must not display the order as paid until that cash confirmation is recorded.
4. If the customer chooses **Pay Here with UPI/card**, FastAPI creates a Razorpay Order for the exact order amount (integer paise, INR) and returns only the public Razorpay key ID, provider order ID, amount, currency, internal order reference, and a short-lived checkout context to the customer client. The client opens Razorpay Standard Checkout on the customer's phone. The ESP32 must not handle card data, UPI PINs, or Razorpay secrets. Confirm the merchant account's enabled payment methods and supported QR flow before promising a kiosk-displayed QR.
5. Razorpay returns a checkout signature which FastAPI verifies using the server-held key secret and the server's stored order ID. Razorpay also sends a signed webhook; the backend verifies `X-Razorpay-Signature` against the exact raw request bytes and a separate webhook secret, checks order ID, amount, currency, and captured state, then records the payment once. Duplicate webhook event IDs are safe to process.
6. The kiosk polls the order-status endpoint through the bridge. It displays **Paid** only after backend verification. A browser return or a QR scan by itself never marks an order paid.
7. For a delayed or missing webhook, the backend queries the provider status API before showing a final result. Failed, pending, expired, and paid attempts remain distinct in the audit trail.

## System shape

```text
ESP32-S3 kiosk  <->  Windows TCP bridge  <->  FastAPI + database
       |                                         |           ^
       | displays QR/status                      | HTTPS     | signed webhook
       v                                         v           |
Customer phone  --------------------------> Hosted gateway checkout
```

The FastAPI payment component should sit behind a stable public HTTPS address so the gateway can deliver webhooks. The current `localhost`/LAN-only server cannot receive production webhooks. A temporary HTTPS tunnel may be used for sandbox development only; production needs a managed public deployment with TLS, secrets management, durable database backups, monitoring, and an incident/reconciliation process. The ESP32 and bridge can continue using the local order/status path in the initial integration, but payment creation and webhook ownership must be reachable and dependable. Never expose TCP port 5000 to the public internet.

## Backend records and states

- Keep the existing `orders` record and server-calculated `total_paise` as the authoritative order and amount.
- Add `payment_attempts` linked to an order. Store provider name, provider order/payment IDs, amount in paise, INR currency, attempt status, creation/expiry/paid times, and safe diagnostic codes. Do not store card numbers, UPI PINs, or other payment credentials.
- Add a unique provider event identifier (or equivalent deduplication key) for webhook processing. Keep a minimal audit record of verification outcome and state changes.
- Suggested attempt states: `created`, `pending`, `paid`, `failed`, `expired`, `cancelled`, `refund_pending`, `refunded`, `review_required`.
- An order is `paid` only when one verified captured/successful payment matches the order's provider reference, amount, and INR currency. Failed or expired attempts do not change the order to paid.
- If a late success arrives after cancellation or another conflicting final state, flag it as `review_required` and surface it to the manager; do not silently trigger fulfilment.

## Manager and employee behavior

- The employee page keeps cash collection separate from online payment. Cash orders use denomination counts and change calculation; an employee cash confirmation cannot mark a UPI attempt as paid.
- The manager page must distinguish `cash` from `upi` in the order/payment record. It must not offer a manual “received” action for a gateway attempt.
- The manager page shows payment method, attempt state, provider reference, and timestamps. Any refund or exception action must be explicit and audited.
- Existing manual payment review remains available for its current prototype workflow but must be labeled manual and never treated as a gateway verification.
- A paid order is not an instruction to operate dispensing hardware. The current project has no dispensing integration.

## Reliability and safety rules

- Amount, currency, and internal order reference are created and checked by FastAPI, never accepted from the kiosk/browser as proof.
- Keep gateway secret keys on the server only, separate sandbox and production credentials, and never put secrets in firmware, HTML, logs, or the repository.
- Verify webhook signatures before parsing or applying payment state; make the handler idempotent.
- Use provider status API as a reconciliation fallback when a webhook is late. Treat `pending` as not paid.
- Prevent duplicate charges with idempotent payment-attempt creation and only one active attempt per order unless a deliberate retry is requested.
- Display an unambiguous timeout/failure path and allow a controlled retry without creating a second paid order.
- Add tests for valid/invalid signatures, duplicate webhooks, amount mismatch, late success, timeout, retries, and gateway outage before any live use.

## Build phases

1. Set `RAZORPAY_KEY_ID`, `RAZORPAY_KEY_SECRET`, and `RAZORPAY_WEBHOOK_SECRET` in the API host's secret store using Test Mode credentials first. Never paste secrets into chat, firmware, HTML, or Git.
2. Deploy the API behind stable public HTTPS with a durable database and backups; set the exact `JINI_CORS_ORIGINS` and set the kiosk page's `jini-api-base` metadata to the API origin.
3. Configure the Test Mode webhook at `/api/payments/razorpay/webhook` for `payment.captured` with the same webhook secret.
4. Exercise sandbox success, failure, abandonment, duplicate/late webhook, amount mismatch, replay, and timeout cases end to end before live mode.
5. Replace the prototype's per-process order throttle with durable distributed abuse controls; add payment reconciliation, alerting, and a restore rehearsal.
6. Complete merchant onboarding, security review, settlement/refund reconciliation, and physical dispensing safety review before considering live payments.

## Decisions needed

- Gateway provider: **Razorpay**, selected by the owner. Merchant account state and credentials are not configured in this project.
- Payment surface: the intended customer flow is Razorpay-hosted UPI/card checkout. A browser-based Standard Checkout is the starting implementation; a standalone dynamic QR flow must be confirmed against the Razorpay merchant account/product. A payment SDK or secret on the ESP32 is out of scope.
- Deployment: public HTTPS backend and webhook host remain undecided. The existing local-only PC server is not sufficient for production webhook delivery.

## Provider references

- Cashfree's official checkout guide describes server-side order creation, a payment session for checkout, server-side payment status lookup, and webhook notifications: <https://www.cashfree.com/devstudio/preview/pg/web/checkout?order_id=%7Border_id%7D>
- Cashfree's official webhook verification guide requires signature verification before processing events: <https://www.cashfree.com/devstudio/preview/pg/tools/webhookVerification>
- Razorpay's official Python integration guide describes server-side signature verification and using webhooks with API status checks for critical user-facing confirmation: <https://razorpay.com/docs/server-integration/python/test-app/>
