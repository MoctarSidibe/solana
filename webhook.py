import json
import os

from dotenv import load_dotenv
from flask import Flask, jsonify, request

from dashboard import (
    DASHBOARD_HTML,
    LOGS_HTML,
    edge_payload,
    enrichment_payload,
    logs_payload,
    paper_payload,
    picks_payload,
    status_payload,
    top_mints_payload,
)
from filters import categorize_event
from storage import append_activity, enqueue_candidate, get_db, utc_now

load_dotenv()

app = Flask(__name__)

HOST = os.getenv("SUNPARK_HOST", "127.0.0.1")
PORT = int(os.getenv("SUNPARK_PORT", "5010"))
AUTH_HEADER = os.getenv("HELIUS_AUTH_HEADER")
ADMIN_USER = os.getenv("SUNPARK_ADMIN_USER", "sunpark")
ADMIN_PASSWORD = os.getenv("SUNPARK_ADMIN_PASSWORD")


def is_authorized():
    """Require the exact secret configured in Helius authHeader."""
    return bool(AUTH_HEADER) and request.headers.get("Authorization") == AUTH_HEADER


def is_admin_authorized():
    credentials = request.authorization
    return bool(
        ADMIN_PASSWORD
        and credentials
        and credentials.username == ADMIN_USER
        and credentials.password == ADMIN_PASSWORD
    )


def admin_required():
    if not is_admin_authorized():
        return jsonify({"status": "error", "message": "Admin authentication required"}), 401, {
            "WWW-Authenticate": 'Basic realm="SunPark Monitor"'
        }
    return None


def normalize_event(data):
    """Keep a stable event envelope for future Helius/Yellowstone adapters."""
    events = data.get("events")
    if not isinstance(events, dict):
        events = {}

    event = {
        "signature": data.get("signature"),
        "slot": data.get("slot"),
        "timestamp": data.get("timestamp"),
        "event_type": data.get("type") or events.get("type"),
        "source": data.get("source") or events.get("source"),
        "fee_payer": data.get("feePayer"),
        "token_transfers": data.get("tokenTransfers", []),
        "native_transfers": data.get("nativeTransfers", []),
        "raw": data,
    }
    event["event_category"] = categorize_event(event)
    return event


def record_event(event):
    """Return False for a previously received signature."""
    signature = event.get("signature")
    if not signature:
        return True

    connection = get_db()
    try:
        cursor = connection.execute(
            "INSERT OR IGNORE INTO processed_events "
            "(signature, slot, event_type, received_at) VALUES (?, ?, ?, ?)",
            (
                signature,
                event.get("slot"),
                event.get("event_type"),
                utc_now(),
            ),
        )
        connection.commit()
        return cursor.rowcount == 1
    finally:
        connection.close()


def process_single_event(data):
    """Normalize and log one Helius event; AI processing comes later."""
    event = normalize_event(data)
    if not record_event(event):
        print(f"Duplicate event ignored: {event['signature']}")
        return {"status": "duplicate", "signature": event["signature"]}

    enqueue_candidate(event)

    print("\n" + "=" * 60)
    print("HELIUS EVENT RECEIVED")
    print("=" * 60)
    print(json.dumps({key: event[key] for key in event if key != "raw"}, indent=2))
    print("Raw payload (first 800 chars):")
    print(json.dumps(data, indent=2)[:800])
    return {"status": "accepted", "signature": event["signature"]}


@app.get("/health")
def health():
    return jsonify({"status": "ok", "service": "sunpark-webhook"})


@app.get("/sunpark/")
def dashboard():
    return DASHBOARD_HTML


@app.get("/sunpark/api/status")
def dashboard_status():
    return jsonify(status_payload())


@app.get("/sunpark/api/top_mints")
def dashboard_top_mints():
    return jsonify(top_mints_payload(request.args.get("limit", 12)))


@app.get("/sunpark/api/picks")
def dashboard_picks():
    return jsonify(picks_payload())


@app.get("/sunpark/api/paper")
def dashboard_paper():
    return jsonify(paper_payload())


@app.get("/sunpark/api/edge")
def dashboard_edge():
    return jsonify(edge_payload())


@app.get("/sunpark/api/enrichment")
def dashboard_enrichment():
    return jsonify(enrichment_payload())


@app.get("/sunpark/logs")
def activity_logs():
    return LOGS_HTML


@app.get("/sunpark/api/logs")
def activity_logs_api():
    return jsonify(
        logs_payload(
            request.args.get("limit", 150),
            request.args.get("level"),
            request.args.get("source"),
        )
    )


@app.post("/")
@app.post("/webhook")
def webhook():
    """Receive Helius events and acknowledge them without calling AI."""
    if not is_authorized():
        append_activity("warn", "webhook", "unauthorized request", {"path": request.path})
        return jsonify({"status": "error", "message": "Unauthorized"}), 401

    data = request.get_json(silent=True)
    if not isinstance(data, (dict, list)) or not data:
        append_activity("warn", "webhook", "invalid JSON payload", {"path": request.path})
        return jsonify({"status": "error", "message": "Invalid JSON payload"}), 400

    items = data if isinstance(data, list) else [data]
    append_activity("info", "webhook", "accepted webhook batch", {"count": len(items)})
    results = [process_single_event(item) for item in items if isinstance(item, dict)]
    return jsonify({"status": "success", "events": results}), 200


if __name__ == "__main__":
    from worker import start_worker

    start_worker()
    print(f"SunPark webhook listening on http://{HOST}:{PORT}")
    app.run(host=HOST, port=PORT, debug=False)
