"""Contract safety checks for SunPark (mint/freeze authority).

Background RPC enrichment following the metadata.py pattern: a persistent
`token_safety` cache with a pending -> ok/missing lifecycle, never blocking the
webhook request path. `selection_gate` hard-rejects `freezable` tokens and
rejects `mintable_after_migration` only contextually - bonding-curve mints
legitimately keep the mint authority before graduation, so that flag fires
only once `token_registry` marks the token as migrated.
"""

import base64
import struct

import requests
from dotenv import load_dotenv

from metadata import base58_decode, base58_encode, is_on_curve, RPC_URLS
from storage import append_activity, get_token_safety, save_token_safety

load_dotenv()

memory_cache = {}


def parse_mint_account(encoded):
    """Parse an SPL Mint account (COption layout; Token-2022 safe at base offsets)."""
    raw = base64.b64decode(encoded)
    if len(raw) < 46:
        raise ValueError("mint account too short")
    mint_flag = struct.unpack_from("<I", raw, 0)[0]
    mint_authority = base58_encode(raw[4:36]) if mint_flag else None
    decimals = raw[44]
    initialized = bool(raw[45])
    freeze_authority = None
    if len(raw) >= 82:
        freeze_flag = struct.unpack_from("<I", raw, 46)[0]
        if freeze_flag:
            freeze_authority = base58_encode(raw[50:82])
    return {
        "mint_authority": mint_authority,
        "mint_authority_is_pda": bool(mint_authority) and not is_on_curve(base58_decode(mint_authority)),
        "decimals": decimals,
        "initialized": initialized,
        "freeze_authority": freeze_authority,
        "freeze_authority_is_pda": bool(freeze_authority) and not is_on_curve(base58_decode(freeze_authority)),
    }


def resolve_token_safety(mint):
    if not mint:
        return {"mint": mint, "status": "invalid_mint"}
    cached = memory_cache.get(mint) or get_token_safety(mint)
    if cached:
        memory_cache[mint] = cached
        return cached
    payload = {
        "jsonrpc": "2.0", "id": 1, "method": "getAccountInfo",
        "params": [mint, {"encoding": "base64"}],
    }
    for url in RPC_URLS:
        try:
            response = requests.post(url, json=payload, timeout=2)
            response.raise_for_status()
            result = response.json()
            if result.get("error"):
                continue
            value = (result.get("result") or {}).get("value")
            data = value.get("data") if value else None
            if isinstance(data, list) and data:
                parsed = parse_mint_account(data[0])
                saved = {"mint": mint, "status": "ok", **parsed}
                save_token_safety(saved)
                memory_cache[mint] = saved
                return saved
            saved = {"mint": mint, "status": "missing"}
            save_token_safety(saved)
            memory_cache[mint] = saved
            return saved
        except (requests.RequestException, ValueError, TypeError, IndexError):
            continue
    saved = {"mint": mint, "status": "missing"}
    save_token_safety(saved)
    memory_cache[mint] = saved
    append_activity("warn", "safety", "RPC failed for mint", {"mint": mint[:16]})
    return saved


def cached_token_safety(mint):
    return memory_cache.get(mint) or get_token_safety(mint)
