"""JINI MONTO TCP bridge: ESP32 TCP -> local FastAPI order API.

This is a local communication release, not a production security boundary.
It creates orders as awaiting_payment and never commands dispensing hardware.
"""
import asyncio
import hashlib
import hmac
import json
import os
import re
from datetime import datetime, timezone
from pathlib import Path
from urllib.error import HTTPError, URLError
from urllib.request import Request, urlopen

HOST = os.getenv("JINI_TCP_HOST", "192.168.1.16")
PORT = int(os.getenv("JINI_TCP_PORT", "5000"))
API_BASE = os.getenv("JINI_API_BASE", "http://127.0.0.1:8000")
ADMIN_TOKEN_PATH = Path(os.getenv(
    "JINI_ADMIN_TOKEN_FILE",
    str(Path(__file__).resolve().parent.parent / ".admin_token"),
))
MAX_LINE = 1024
LOG_PATH = Path(__file__).resolve().parent.parent / "esp32_api_orders.jsonl"
BRIDGE_KEY_PATH = Path(__file__).resolve().parent.parent / ".bridge_device_token"
BRIDGE_KEY_HEX = os.getenv("JINI_TCP_DEVICE_KEY", "").strip()
if not BRIDGE_KEY_HEX:
    try:
        BRIDGE_KEY_HEX = BRIDGE_KEY_PATH.read_text(encoding="utf-8").strip()
    except OSError as exc:
        raise RuntimeError("JINI TCP bridge HMAC key is missing") from exc
if not re.fullmatch(r"[0-9a-fA-F]{64}", BRIDGE_KEY_HEX):
    raise RuntimeError("JINI TCP bridge HMAC key must be 64 hexadecimal characters")
BRIDGE_KEY = bytes.fromhex(BRIDGE_KEY_HEX)
# Numeric item indexes must stay in the same order as the kiosk Product table.
# API prices are authoritative; the ESP32-reported total is diagnostic only.
MENU_INDEX_TO_CODE = {
    0: "CC-ICE-AMERICANO",
    1: "CC-COLD-FRAPPE",
    2: "CC-CARAMEL-FRAPPE",
    3: "CC-IRISH-FRAPPE",
    4: "CC-FRENCH-VANILLA-FRAPPE",
    5: "CC-HAZELNUT-FRAPPE",
    6: "CC-BISCOFF-FRAPPE",
    7: "CC-TIRAMISU-FRAPPE",
    8: "CC-CHOCO-ALMOND-FRAPPE",
    9: "CC-JAVA-CHIPS-FRAPPE",
    10: "CC-MOCHA-FRAPPE",
    11: "CC-COOKIES-CRUMBLE-FRAPPE",
    12: "CC-BROWNIE-FRAPPE",
    13: "CC-OREO-FRAPPE",
    14: "CC-BB-SPECIAL-FRAPPE",
    15: "SH-STRAWBERRY-SHAKE",
    16: "SH-OREO-SHAKE",
    17: "SH-HAZELNUT",
    18: "SH-KIT-KAT-SHAKE",
    19: "SH-TRIPLE-CHOCOLATE-SHAKE",
    20: "SH-RED-VELVET-FUDGE",
    21: "SH-MUD-PIE",
    22: "SH-CHOCO-ALMOND",
    23: "SH-TIRAMISU-TRUFFLE",
    24: "SH-CHOCO-HAZELNUT",
    25: "SH-BROWNIE",
    26: "SH-COOKIES-CRUMBLE",
    27: "SH-WHITE-CHOCOLATE-KITKAT",
    28: "SH-NUTELLA",
    29: "SH-OREO-STRAWBERRY",
    30: "SH-FERRERO-ROCHER",
    31: "SH-TIRAMISU-BROWNIE",
    32: "SH-MOCHA-BROWNIE",
    33: "SH-BB-SPECIAL",
    34: "SH-BISCOFF-DELIGHT",
    35: "HC-PREMIUM",
    36: "HC-HAZELNUT",
    37: "HC-IRISH",
    38: "HC-TIRAMISU",
    39: "HC-FRENCH-VANILLA",
    40: "HC-NUTELLA",
    41: "HF-ESPRESSO",
    42: "HF-AMERICANO",
    43: "HF-CAPPUCCINO",
    44: "HF-LATTE",
    45: "HF-MOCHA",
    46: "HF-IRISH-LATTE",
    47: "HF-HAZELNUT-LATTE",
    48: "HF-VANILLA-LATTE",
    49: "HF-TIRAMISU",
    50: "HF-BISCOFF-LATTE",
    51: "MO-MINT",
    52: "MO-STRAWBERRY",
    53: "MO-WATERMELON",
    54: "MO-KIWI",
    55: "MO-RED-BULL",
    56: "CH-MASALA",
    57: "CH-ADRAK-ELAICHI",
    58: "CH-TOAST-COMBO",
    59: "IT-LEMON",
    60: "IT-PEACH",
    61: "IT-STRAWBERRY",
    62: "IT-WATERMELON",
    63: "EX-CHEESE",
    64: "EX-MAYONNAISE",
}


def api_json(path, method="GET", payload=None, timeout=4):
    data = None if payload is None else json.dumps(payload).encode("utf-8")
    headers = {"Accept": "application/json"}
    admin_token = os.getenv("JINI_ADMIN_TOKEN", "").strip()
    if not admin_token:
        try:
            admin_token = ADMIN_TOKEN_PATH.read_text(encoding="utf-8").strip()
        except OSError:
            admin_token = ""
    if admin_token:
        headers["X-JINI-Admin-Token"] = admin_token
    if data is not None:
        headers["Content-Type"] = "application/json"
    req = Request(API_BASE + path, data=data, headers=headers, method=method)
    try:
        with urlopen(req, timeout=timeout) as response:
            raw = response.read()
            return response.status, (json.loads(raw.decode("utf-8")) if raw else {})
    except HTTPError as e:
        body = e.read().decode("utf-8", errors="replace")
        try: parsed = json.loads(body)
        except Exception: parsed = {"detail": body}
        return e.code, parsed
    except (URLError, TimeoutError, OSError) as e:
        raise RuntimeError(f"API_UNREACHABLE: {e}") from e


def idempotency_key(device_id, sequence):
    return f"kiosk:{device_id}:{sequence}"


def verify_device_signature(message, signature):
    if not re.fullmatch(r"[0-9a-fA-F]{64}", signature):
        return False
    expected = hmac.new(BRIDGE_KEY, message.encode("utf-8"), hashlib.sha256).hexdigest()
    return hmac.compare_digest(expected, signature.lower())


def record_log(record):
    with LOG_PATH.open("a", encoding="utf-8") as f:
        f.write(json.dumps(record, ensure_ascii=False) + "\n")


async def handle_client(reader, writer):
    peer = writer.get_extra_info("peername") or ("unknown", 0)
    peer_ip = str(peer[0])
    print(f"ESP32 connected: {peer}", flush=True)
    try:
        while True:
            try:
                raw = await asyncio.wait_for(reader.readuntil(b"\n"), timeout=30)
            except asyncio.LimitOverrunError:
                writer.write(b"ERROR|LINE_TOO_LONG\n")
                await writer.drain()
                break
            except asyncio.TimeoutError:
                writer.write(b"ERROR|READ_TIMEOUT\n")
                await writer.drain()
                break
            except asyncio.IncompleteReadError as exc:
                if exc.partial:
                    writer.write(b"ERROR|INCOMPLETE_LINE\n")
                    await writer.drain()
                break
            if not raw:
                break
            if len(raw) > MAX_LINE:
                writer.write(b"ERROR|LINE_TOO_LONG\n"); await writer.drain(); break
            message = raw.decode("utf-8", errors="replace").strip()
            if message.startswith("PING|"):
                ping_parts = message.split("|")
                if (len(ping_parts) == 3
                        and re.fullmatch(r"[0-9a-f]{12}", ping_parts[1])
                        and verify_device_signature("|".join(ping_parts[:2]), ping_parts[2])):
                    writer.write(b"PONG\n"); await writer.drain()
                else:
                    writer.write(b"NACK|AUTH_FAILED\n"); await writer.drain()
                continue
            if not message.startswith("ORDER|"):
                writer.write(b"NACK|UNKNOWN_COMMAND\n"); await writer.drain(); continue
            parts = message.split("|", 5)
            if len(parts) != 6:
                writer.write(b"NACK|INVALID_FORMAT\n"); await writer.drain(); continue
            signed_body = "|".join(parts[:5])
            if not verify_device_signature(signed_body, parts[5]):
                writer.write(b"NACK|AUTH_FAILED\n"); await writer.drain(); continue
            try:
                device_id = parts[1].lower()
                sequence = int(parts[2])
                reported_total = int(parts[3])
                item_text = parts[4]
                if len(device_id) != 12 or any(c not in "0123456789abcdef" for c in device_id):
                    raise ValueError("invalid device id")
                if sequence < 1:
                    raise ValueError("invalid sequence")
                key = idempotency_key(device_id, sequence)
                if sequence > 0xFFFFFFFF:
                    raise ValueError("sequence is out of range")
                if reported_total < 1:
                    raise ValueError("invalid order total")
                quantities = {}
                for entry in item_text.split(","):
                    index_s, qty_s = entry.split(":", 1)
                    index, qty = int(index_s), int(qty_s)
                    if index not in MENU_INDEX_TO_CODE or not 1 <= qty <= 50:
                        raise ValueError("unsupported index or quantity")
                    quantities[index] = quantities.get(index, 0) + qty
                if not quantities or any(q > 50 for q in quantities.values()): raise ValueError("invalid quantities")
            except (ValueError, TypeError):
                writer.write(b"NACK|INVALID_ORDER\n"); await writer.drain(); continue

            try:
                items = [{"product_code": MENU_INDEX_TO_CODE[i], "quantity": q} for i, q in sorted(quantities.items())]
                note = f"ESP32 reported total={reported_total} rupees; server recalculates total."
                payload = {"source": "customer_kiosk", "items": items, "discount_paise": 0,
                           "customer_note": note, "idempotency_key": key}
                status, response = await asyncio.to_thread(api_json, "/api/orders", "POST", payload, 5)
                if status not in (200, 201) or not isinstance(response, dict) or not response.get("order_no"):
                    detail = str(response.get("detail", "API rejected order"))[:100]
                    print(f"API rejected device order {device_id}: HTTP {status}: {detail}", flush=True)
                    writer.write(b"NACK|API_REJECTED\n"); await writer.drain(); continue
                record = {"received_at": datetime.now(timezone.utc).isoformat(), "device_order_id": device_id,
                          "device_sequence": sequence,
                          "reported_total_rupees": reported_total, "api_order_no": response["order_no"],
                          "api_total_paise": response.get("total_paise"), "items": items,
                          "idempotency_key": key, "source": str(peer),
                          "status": response.get("status", "awaiting_payment"),
                          "idempotent_replay": response.get("idempotent_replay", False)}
                try:
                    record_log(record)
                except OSError as exc:
                    print(f"Order log unavailable; order is still safely recorded by API: {exc}", flush=True)
                print("API ORDER CREATED:", json.dumps(record, ensure_ascii=False), flush=True)
                writer.write(f"ACK|{sequence}|{response['order_no']}\n".encode()); await writer.drain()
            except RuntimeError as e:
                print(f"Order {device_id} not acknowledged: {e}", flush=True)
                writer.write(b"NACK|API_UNAVAILABLE\n"); await writer.drain()
    except (ConnectionError, asyncio.CancelledError):
        pass
    finally:
        writer.close()
        try: await writer.wait_closed()
        except ConnectionError: pass
        print(f"ESP32 disconnected: {peer}", flush=True)


async def main():
    status, health = await asyncio.to_thread(api_json, "/api/health")
    if status != 200:
        raise RuntimeError(f"JINI API is not healthy: HTTP {status} {health}")
    server = await asyncio.start_server(handle_client, HOST, PORT, limit=MAX_LINE)
    print(f"JINI MONTO API TCP bridge listening on {HOST}:{PORT}; API={API_BASE}", flush=True)
    print("Orders are created awaiting_payment. No payment gateway or dispensing is connected.", flush=True)
    async with server: await server.serve_forever()


if __name__ == "__main__":
    asyncio.run(main())
