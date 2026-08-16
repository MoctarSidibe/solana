import json
import os
import re
import threading
import time

import requests
import websocket
from dotenv import load_dotenv

from storage import append_activity, enqueue_candidate, update_ingress_stats

load_dotenv()

PROGRAMS = {
    "pump_fun": os.getenv(
        "PUMP_FUN_PROGRAM_ID",
        "6EF8rrecthR5Dkzon8Nwu78hRvfCKubJ14M5uBEwF6P",
    ),
    "raydium": os.getenv(
        "RAYDIUM_PROGRAM_ID",
        "675kPX9MHTjS2zt1qfr1NYHuzeLXfQM9H24wFSUt1Mp8",
    ),
}


def csv_env(name, defaults):
    value = os.getenv(name)
    if value:
        return [item.strip() for item in value.split(",") if item.strip()]
    return list(defaults)


WSS_URLS = csv_env(
    "SOLANA_WSS_URLS",
    [
        "wss://api.mainnet-beta.solana.com",
        "wss://solana-rpc.publicnode.com",
    ],
)
RPC_URLS = csv_env(
    "SOLANA_RPC_URLS",
    [
        os.getenv("SOLANA_RPC_URL"),
        "https://api.mainnet-beta.solana.com",
        "https://solana-rpc.publicnode.com",
    ],
)
RPC_URLS = [url for url in RPC_URLS if url]

seen_signatures = set()
seen_lock = threading.Lock()
stats = {"notifications": 0, "candidates": 0, "rpc_failures": 0}
stats_lock = threading.Lock()


def claim_signature(signature):
    with seen_lock:
        if signature in seen_signatures:
            return False
        seen_signatures.add(signature)
        if len(seen_signatures) > 100_000:
            seen_signatures.clear()
            seen_signatures.add(signature)
        return True


def release_signature(signature):
    with seen_lock:
        seen_signatures.discard(signature)


def rpc_get_transaction(signature):
    payload = {
        "jsonrpc": "2.0",
        "id": signature,
        "method": "getTransaction",
        "params": [
            signature,
            {"encoding": "jsonParsed", "maxSupportedTransactionVersion": 0},
        ],
    }
    for attempt in range(2):
        for url in RPC_URLS:
            try:
                response = requests.post(url, json=payload, timeout=10)
                response.raise_for_status()
                result = response.json().get("result")
                if result:
                    return result
            except requests.RequestException:
                continue
        if attempt == 0:
            time.sleep(0.2)
    with stats_lock:
        stats["rpc_failures"] += 1
    return None


def transaction_fee_payer(transaction):
    message = (transaction.get("transaction") or {}).get("message") or {}
    for account in message.get("accountKeys", []):
        if isinstance(account, dict) and account.get("signer"):
            return account.get("pubkey")
    return None


def account_keys(transaction):
    message = (transaction.get("transaction") or {}).get("message") or {}
    keys = []
    for account in message.get("accountKeys", []):
        if isinstance(account, dict):
            keys.append(account.get("pubkey"))
        else:
            keys.append(account)
    return keys


def token_transfers(transaction):
    meta = transaction.get("meta") or {}
    pre = {
        (item.get("owner"), item.get("mint")): int(
            (item.get("uiTokenAmount") or {}).get("amount", 0)
        )
        for item in meta.get("preTokenBalances", [])
    }
    post = {
        (item.get("owner"), item.get("mint")): int(
            (item.get("uiTokenAmount") or {}).get("amount", 0)
        )
        for item in meta.get("postTokenBalances", [])
    }
    changes = []
    for key in set(pre) | set(post):
        before = pre.get(key, 0)
        after = post.get(key, 0)
        if before != after and key[0] and key[1]:
            changes.append({"owner": key[0], "mint": key[1], "delta": after - before})

    transfers = []
    for mint in {item["mint"] for item in changes}:
        senders = [item for item in changes if item["mint"] == mint and item["delta"] < 0]
        receivers = [item for item in changes if item["mint"] == mint and item["delta"] > 0]
        for sender in senders:
            for receiver in receivers:
                amount = min(abs(sender["delta"]), receiver["delta"])
                if amount:
                    transfers.append(
                        {
                            "mint": mint,
                            "tokenAmount": str(amount),
                            "decimals": next(
                                (
                                    item.get("uiTokenAmount", {}).get("decimals")
                                    for item in meta.get("postTokenBalances", [])
                                    if item.get("mint") == mint
                                ),
                                None,
                            ),
                            "fromUserAccount": sender["owner"],
                            "toUserAccount": receiver["owner"],
                        }
                    )
    return transfers[:50]


def native_transfers(transaction):
    meta = transaction.get("meta") or {}
    before = meta.get("preBalances") or []
    after = meta.get("postBalances") or []
    keys = account_keys(transaction)
    decreases = []
    increases = []
    for index, (old, new) in enumerate(zip(before, after)):
        if new < old:
            decreases.append({"owner": keys[index], "amount": old - new})
        elif new > old:
            increases.append({"owner": keys[index], "amount": new - old})

    transfers = []
    for sender in decreases:
        for receiver in increases:
            amount = min(sender["amount"], receiver["amount"])
            if amount:
                transfers.append(
                    {
                        "amount": amount,
                        "fromUserAccount": sender["owner"],
                        "toUserAccount": receiver["owner"],
                    }
                )
    return transfers[:50]


def transaction_mints(transaction, program_name):
    meta = transaction.get("meta") or {}
    mints = []
    for balance in (meta.get("preTokenBalances", []) + meta.get("postTokenBalances", [])):
        mint = balance.get("mint")
        if mint and mint not in mints:
            mints.append(mint)
    if mints:
        return mints[:10]

    program_id = PROGRAMS[program_name]
    message = (transaction.get("transaction") or {}).get("message") or {}
    for instruction in message.get("instructions", []):
        if instruction.get("programId") == program_id:
            accounts = instruction.get("accounts") or []
            if accounts and isinstance(accounts[0], str):
                return accounts[:3]
    return []


def make_event(signature, slot, logs, program_name, transaction):
    source = "PUMP_FUN" if program_name == "pump_fun" else "RAYDIUM"
    log_text = " ".join(logs).lower()
    if program_name == "pump_fun":
        if "migrate" in log_text:
            event_type, category = "PUMP_MIGRATE", "liquidity"
        elif "buy" in log_text:
            event_type, category = "PUMP_BUY", "swap"
        elif "sell" in log_text:
            event_type, category = "PUMP_SELL", "swap"
        else:
            event_type, category = "PUMP_CREATE", "token_creation"
    else:
        if "ray_log:" in log_text:
            event_type, category = "RAYDIUM_SWAP", "swap"
        else:
            event_type, category = "RAYDIUM_LIQUIDITY", "liquidity"
    meta = transaction.get("meta") or {}
    mints = transaction_mints(transaction, program_name)
    return {
        "signature": signature,
        "slot": slot or transaction.get("slot"),
        "timestamp": transaction.get("blockTime"),
        "event_type": event_type,
        "source": source,
        "fee_payer": transaction_fee_payer(transaction),
        "token_transfers": token_transfers(transaction),
        "native_transfers": native_transfers(transaction),
        "token_mints": mints,
        "event_category": category,
        "raw": {
            "type": event_type,
            "source": source,
            "logs": logs[:100],
            "transaction": transaction,
            "transactionError": meta.get("err"),
        },
    }


def candidate_logs(logs, program_name):
    instruction_names = set(
        re.findall(r"instruction:\s*([a-z0-9_]+)", " ".join(logs).lower())
    )
    if program_name == "pump_fun":
        return bool(instruction_names.intersection({"create", "migrate"})) or any(
            "buy" in name or "sell" in name for name in instruction_names
        )
    return bool(
        instruction_names.intersection(
            {"initialize", "initialize2", "addliquidity", "remove_liquidity"}
        )
    ) or "ray_log:" in " ".join(logs).lower()


def handle_notification(notification, program_name):
    value = ((notification.get("params") or {}).get("result") or {}).get("value") or {}
    with stats_lock:
        stats["notifications"] += 1
    signature = value.get("signature")
    logs = value.get("logs") or []
    if not signature or value.get("err") or not candidate_logs(logs, program_name):
        return False

    if not claim_signature(signature):
        return False
    transaction = rpc_get_transaction(signature)
    if not transaction:
        release_signature(signature)
        return False
    event = make_event(
        signature,
        ((notification.get("params") or {}).get("result") or {}).get("context", {}).get("slot"),
        logs,
        program_name,
        transaction,
    )
    if event["event_category"] == "swap":
        sol_size = sum(item.get("amount", 0) for item in event["native_transfers"]) / 1_000_000_000
        if sol_size < 0.5:
            return False
    enqueued = enqueue_candidate(event)
    if enqueued:
        with stats_lock:
            stats["candidates"] += 1
    return enqueued


def listen(wss_url, program_name):
    program_id = PROGRAMS[program_name]
    request_id = 1
    backoff = 1
    while True:
        try:
            socket = websocket.create_connection(wss_url, timeout=30)
            socket.send(
                json.dumps(
                    {
                        "jsonrpc": "2.0",
                        "id": request_id,
                        "method": "logsSubscribe",
                        "params": [{"mentions": [program_id]}, {"commitment": "confirmed"}],
                    }
                )
            )
            confirmation = json.loads(socket.recv())
            if "result" not in confirmation:
                raise RuntimeError(f"subscription failed: {confirmation}")
            print(f"stream connected: {program_name} via {wss_url}")
            append_activity(
                "info",
                "stream",
                "subscription connected",
                {"program": program_name, "endpoint": wss_url},
            )
            backoff = 1
            while True:
                notification = json.loads(socket.recv())
                if notification.get("method") == "logsNotification":
                    handle_notification(notification, program_name)
        except Exception as error:
            print(f"stream disconnected ({program_name}, {wss_url}): {str(error)[:200]}")
            append_activity(
                "warn",
                "stream",
                "subscription disconnected",
                {"program": program_name, "endpoint": wss_url, "error": str(error)[:300]},
            )
            time.sleep(backoff)
            backoff = min(backoff * 2, 60)


def main():
    threads = []
    for wss_url in WSS_URLS:
        for program_name in PROGRAMS:
            thread = threading.Thread(
                target=listen,
                args=(wss_url, program_name),
                name=f"stream-{program_name}-{len(threads)}",
                daemon=True,
            )
            thread.start()
            threads.append(thread)
    print(f"stream started for {len(threads)} subscriptions")
    while True:
        time.sleep(60)
        with stats_lock:
            print(f"stream stats: {stats}")
            update_ingress_stats("stream", stats)
            append_activity("info", "stream", "periodic stream statistics", stats)


if __name__ == "__main__":
    main()
