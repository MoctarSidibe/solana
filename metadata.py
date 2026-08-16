import base64
import hashlib
import os
import struct

import requests
from dotenv import load_dotenv

from storage import get_token_meta, save_token_meta

load_dotenv()

METADATA_PROGRAM = "metaqbxxUerdq28cj1RbAWkYQm3ybzjb6a8bt518x1s"
BASE58 = "123456789ABCDEFGHJKLMNPQRSTUVWXYZabcdefghijkmnopqrstuvwxyz"
WSOL_MINT = "So11111111111111111111111111111111111111112"
USDC_MINT = "EPjFWdd5AufqSSqeM2qN1xzybapC8G4wEGGkZwyTDt1v"
RPC_URLS = [
    item.strip()
    for item in os.getenv("SOLANA_RPC_URLS", os.getenv("SOLANA_RPC_URL", "")).split(",")
    if item.strip()
]
RPC_URLS += [
    "https://api.mainnet-beta.solana.com",
    "https://solana-rpc.publicnode.com",
]
RPC_URLS = list(dict.fromkeys(RPC_URLS))[:3]
memory_cache = {}


def base58_decode(value):
    number = 0
    for character in value:
        number = number * 58 + BASE58.index(character)
    raw = number.to_bytes((number.bit_length() + 7) // 8, "big") if number else b""
    return b"\x00" * (len(value) - len(value.lstrip("1"))) + raw


def base58_encode(value):
    number = int.from_bytes(value, "big")
    result = ""
    while number:
        number, remainder = divmod(number, 58)
        result = BASE58[remainder] + result
    return "1" * (len(value) - len(value.lstrip(b"\x00"))) + (result or "")


def is_on_curve(public_key):
    # Solana PDAs must be off the Ed25519 curve.
    prime = 2**255 - 19
    d = (-121665 * pow(121666, prime - 2, prime)) % prime
    y = int.from_bytes(public_key, "little") & ((1 << 255) - 1)
    if y >= prime:
        return False
    xx = ((y * y - 1) * pow(d * y * y + 1, prime - 2, prime)) % prime
    x = pow(xx, (prime + 3) // 8, prime)
    if (x * x - xx) % prime:
        x = (x * pow(2, (prime - 1) // 4, prime)) % prime
    return (x * x - xx) % prime == 0


def metadata_address(mint):
    program_bytes = base58_decode(METADATA_PROGRAM)
    seeds = [b"metadata", program_bytes, base58_decode(mint)]
    for bump in range(255, -1, -1):
        digest = hashlib.sha256(
            b"".join(seeds + [bytes([bump]), program_bytes]) + b"ProgramDerivedAddress"
        ).digest()
        if not is_on_curve(digest):
            return base58_encode(digest)
    raise ValueError("unable to derive metadata PDA")


def read_string(data, offset, maximum):
    if offset + 4 > len(data):
        raise ValueError("metadata is truncated")
    length = struct.unpack_from("<I", data, offset)[0]
    offset += 4
    if length > maximum or offset + length > len(data):
        raise ValueError("metadata string is invalid")
    value = data[offset:offset + length].decode("utf-8", errors="replace").strip("\x00 ")
    return value, offset + length


def parse_metadata_account(encoded, mint):
    raw = base64.b64decode(encoded)
    if len(raw) < 65:
        raise ValueError("metadata account is too short")
    name, offset = read_string(raw, 65, 256)
    symbol, offset = read_string(raw, offset, 64)
    uri, _ = read_string(raw, offset, 512)
    return {"mint": mint, "name": name or None, "symbol": symbol or None, "uri": uri or None, "status": "ok"}


def resolve_token_metadata(mint):
    if not mint or mint in {WSOL_MINT, USDC_MINT}:
        return {"mint": mint, "status": "quote_asset"}
    cached = memory_cache.get(mint) or get_token_meta(mint)
    if cached:
        memory_cache[mint] = cached
        return cached
    try:
        address = metadata_address(mint)
    except (ValueError, IndexError):
        result = {"mint": mint, "status": "invalid_mint"}
        save_token_meta(result)
        memory_cache[mint] = result
        return result

    payload = {
        "jsonrpc": "2.0", "id": 1, "method": "getAccountInfo",
        "params": [address, {"encoding": "base64"}],
    }
    for url in RPC_URLS:
        try:
            response = requests.post(url, json=payload, timeout=2)
            response.raise_for_status()
            value = (response.json().get("result") or {}).get("value")
            data = value.get("data") if value else None
            if isinstance(data, list) and data:
                result = parse_metadata_account(data[0], mint)
                save_token_meta(result)
                memory_cache[mint] = result
                return result
        except (requests.RequestException, ValueError, TypeError, IndexError):
            continue
    result = {"mint": mint, "status": "missing"}
    save_token_meta(result)
    memory_cache[mint] = result
    return result


def cached_token_metadata(mint):
    return memory_cache.get(mint) or get_token_meta(mint)
