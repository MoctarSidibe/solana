import json
from flask import Flask, request, jsonify

app = Flask(__name__)

@app.route("/", methods=["POST"])
@app.route("/webhook", methods=["POST"])
def webhook():
    """Receive webhook events from Helius."""
    data = request.json

    if not data:
        return jsonify({"status": "error", "message": "No data"}), 400

    print("\n" + "=" * 60)
    print("📩 WEBHOOK RECEIVED")
    print("=" * 60)

    # Handle both dict and list payloads
    if isinstance(data, list):
        # If it's a list, process each item
        for item in data:
            process_single_event(item)
    else:
        # It's a single dict
        process_single_event(data)

    return jsonify({"status": "success"}), 200


def process_single_event(data):
    """Process a single Helius event (dict)."""
    # Token transfers
    token_transfers = data.get("tokenTransfers", [])
    if token_transfers:
        print(f"🔹 Token Transfers: {len(token_transfers)}")
        for transfer in token_transfers:
            print(f"   - Token: {transfer.get('tokenName', 'Unknown')}")
            print(f"     Symbol: {transfer.get('tokenSymbol', '?')}")
            print(f"     Mint: {transfer.get('mint', 'No mint')}")
            print(f"     From: {transfer.get('fromUserAccount', 'Unknown')}")
            print(f"     To: {transfer.get('toUserAccount', 'Unknown')}")

    # Events
    events = data.get("events", {})
    if events:
        print(f"🔹 Event Type: {events.get('type', 'Unknown')}")
        print(f"     Source: {events.get('source', 'Unknown')}")

    # Native SOL transfers
    native_transfers = data.get("nativeTransfers", [])
    if native_transfers:
        print(f"🔹 SOL Transfers: {len(native_transfers)}")
        for transfer in native_transfers:
            print(f"   - Amount: {transfer.get('amount', '?')} lamports")
            print(f"     From: {transfer.get('fromUserAccount', 'Unknown')}")
            print(f"     To: {transfer.get('toUserAccount', 'Unknown')}")

    # Print full payload for debugging (truncated)
    print("\n🔹 Full payload (first 800 chars):")
    print(json.dumps(data, indent=2)[:800])


if __name__ == "__main__":
    print("=" * 60)
    print("🔔 SunPark Webhook Receiver")
    print("=" * 60)
    print("✅ Listening on http://0.0.0.0:5000/")
    print("✅ Listening on http://0.0.0.0:5000/webhook")
    print("Waiting for Helius events...")
    app.run(host="0.0.0.0", port=5000, debug=False)