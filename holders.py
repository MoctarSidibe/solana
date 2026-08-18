"""Token holder concentration for SunPark (top-20 supply distribution).

Background RPC enrichment following the safety.py pattern: a persistent
`token_holders` cache with an ok/missing lifecycle, never blocking the webhook
request path. `selection_gate` uses the cached shares to hard-reject
`whale_heavy` (positions 2+3 combined - the top-1 account is normally the
bonding curve or AMM pool and is excluded to avoid pool noise) and `dev_heavy`
(the creator wallet holding a large supply share). Unknown/missing holder data
is never guessed into flags.
"""

import os

import requests
from dotenv import load_dotenv

from metadata import RPC_URLS, base58_encode
from storage import append_activity, get_token_holders, save_token_holders

load_dotenv()

memory_cache = {}

SUPPLY_FRACTION_TOLERANCE = 0.99


def _to_ui(amount, decimals):
    try:
        return float(amount) / (10 ** int(decimals or 0))
    except (TypeError, ValueError):
        return 0.0


def _fetch(method, params):
    payload = {"jsonrpc": "2.0", "id": 1, "method": method, "params": params}
    last = None
    for url in RPC_URLS:
        try:
            response = requests.post(url, json=payload, timeout=5)
            response.raise_for_status()
            result = response.json()
            last = result
            if result.get("error"):
                continue
            return result.get("result") or {}
        except (requests.RequestException, ValueError, TypeError):
            continue
    if last is not None:
        return last
    return None


def _token_account_owner(value):
    if not isinstance(value, dict) or not value.get("data"):
        return None
    data = value["data"]
    if isinstance(data, dict):
        parsed = (data.get("parsed") or {}).get("info") or {}
        return parsed.get("owner")
    if isinstance(data, list) and data:
        raw = data[0]
        if isinstance(raw, str):
            import base64

            try:
                decoded = base64.b64decode(raw)
                if len(decoded) >= 64:
                    return base58_encode(decoded[32:64])
            except Exception:
                return None
    return None


def resolve_token_holders(mint):
    if not mint:
        return {"mint": mint, "status": "invalid_mint"}
    cached = memory_cache.get(mint) or get_token_holders(mint)
    if cached:
        memory_cache[mint] = cached
        return cached

    supply = _fetch("getTokenSupply", [mint])
    largest = _fetch("getTokenLargestAccounts", [mint, {"commitment": "confirmed"}])
    if supply is None or largest is None:
        return {"mint": mint, "status": "missing", "transient": True}
    if supply.get("error") or largest.get("error"):
        saved = {"mint": mint, "status": "missing"}
        save_token_holders(saved)
        memory_cache[mint] = saved
        append_activity("warn", "holders", "RPC error saving missing", {"mint": mint[:16], "supply_err": bool(supply.get("error")), "largest_err": bool(largest.get("error"))})
        return saved

    accounts = (largest.get("value") or [])
    if not accounts:
        saved = {"mint": mint, "status": "missing"}
        save_token_holders(saved)
        memory_cache[mint] = saved
        return saved

    supply_value = supply.get("value") or {}
    decimals = supply_value.get("decimals") or 0
    supply_ui = _to_ui(supply_value.get("amount"), decimals)

    owners = []
    addresses = [account.get("address") for account in accounts if account.get("address")]
    if addresses:
        account_info = _fetch(
            "getMultipleAccounts",
            [addresses, {"encoding": "jsonParsed"}],
        )
        owner_map = {}
        if account_info and account_info.get("value"):
            for address, value in zip(addresses, account_info["value"]):
                owner = _token_account_owner(value)
                if owner:
                    owner_map[address] = owner
        for account in accounts:
            address = account.get("address")
            amount_ui = account.get("uiAmount")
            if amount_ui is None:
                amount_ui = _to_ui(account.get("amount"), account.get("decimals"))
            owners.append({"owner": owner_map.get(address), "share": amount_ui / supply_ui if supply_ui else 0.0})

    shares = [entry.get("share") or 0.0 for entry in owners]
    shares.sort(reverse=True)
    top1 = shares[0] if shares else 0.0
    top3 = sum(shares[:3]) if shares else 0.0
    whale_share = sum(shares[1:3]) if len(shares) >= 2 else 0.0
    top20 = sum(shares[:20]) if shares else 0.0

    saved = {
        "mint": mint,
        "status": "ok",
        "supply": round(supply_ui, 4),
        "total_assets": len(shares),
        "top1_share": round(top1, 4),
        "top3_share": round(top3, 4),
        "whale_share": round(whale_share, 4),
        "top20_share": round(top20, 4),
        "owners": owners,
    }
    save_token_holders(saved)
    memory_cache[mint] = saved
    return saved


def cached_token_holders(mint):
    return memory_cache.get(mint) or get_token_holders(mint)
