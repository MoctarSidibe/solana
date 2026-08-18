"""Jupiter quote client for honest paper-fill pricing.

SunPark uses Jupiter only as a keyless pricing oracle for the paper account:
a real quote models price impact, slippage and the platform fee into the
effective fill price so paper PnL reflects reality. The swap endpoint needs a
wallet key to sign and is deliberately left as a dry-run stub behind
`DRY_RUN`; live execution stays disabled.

Fallback chain: V6 (no fee) -> Swap V2 (5-10 bps) -> Lite (backup).
If all endpoints fail, the caller falls back to the rollup price.
"""

import os

import requests
from dotenv import load_dotenv

load_dotenv()

WSOL_MINT = "So11111111111111111111111111111111111111112"

QUOTE_URLS = [
    os.getenv("SUNPARK_JUPITER_QUOTE_URL", "https://quote-api.jup.ag/v6/quote"),
    os.getenv("SUNPARK_JUPITER_QUOTE_URL_2", "https://api.jup.ag/swap/v2/order"),
    os.getenv("SUNPARK_JUPITER_QUOTE_URL_3", "https://lite-api.jup.ag/v1/quote"),
]
SLIPPAGE_BPS = int(os.getenv("SUNPARK_JUPITER_SLIPPAGE_BPS", "3000"))
FEE_BPS = int(os.getenv("SUNPARK_JUPITER_FEE_BPS", "30"))
QUOTE_TIMEOUT = float(os.getenv("SUNPARK_JUPITER_TIMEOUT", "3"))
QUOTE_ENABLED = os.getenv("SUNPARK_JUPITER_QUOTE", "1") == "1"


def _token_decimals_from_route(route, mint):
    for swap in route.get("routePlan") or []:
        info = (swap.get("swapInfo") or {})
        for token in (info.get("inTokenInfo"), info.get("outTokenInfo")):
            if token and token.get("mint") == mint:
                try:
                    return int(token["decimals"])
                except (TypeError, ValueError):
                    return None
    return None


def _parse_quote_response(data, side, amount, decimals, mint):
    """Extract price_sol from any Jupiter quote response format."""
    out_amount = data.get("outAmount")
    if out_amount is None:
        return None
    impact = float(data.get("priceImpactPct") or 0.0)
    if side == "buy":
        token_decimals = _token_decimals_from_route(data, mint)
        token_decimals = token_decimals if token_decimals is not None else int(decimals or 6)
        out_tokens = int(out_amount) / (10 ** token_decimals)
        if out_tokens <= 0 or amount <= 0:
            return None
        price_sol = amount / out_tokens
    else:
        out_sol = int(out_amount) / 1e9
        if amount <= 0:
            return None
        price_sol = out_sol / amount
    return {
        "price_sol": price_sol,
        "price_impact_pct": impact,
        "fee_bps": FEE_BPS,
        "route_swaps": len(data.get("routePlan") or []),
    }


def quote_price(mint, side, amount, decimals=None, timeout=None):
    """Real Jupiter route quote -> effective fill price in SOL per token.

    side 'buy':  amount is SOL to spend, mint is the token bought.
    side 'sell': amount is whole token units, mint is the token sold.
    Tries each Jupiter endpoint in order; first success wins.
    Returns a dict with price_sol/impact/fee or None on all failures.
    """
    if not QUOTE_ENABLED:
        return None
    if side == "buy":
        params = {
            "inputMint": WSOL_MINT,
            "outputMint": mint,
            "amount": str(int(round(amount * 1e9))),
            "swapMode": "ExactIn",
            "slippageBps": str(SLIPPAGE_BPS),
        }
    else:
        token_decimals = int(decimals or 6)
        params = {
            "inputMint": mint,
            "outputMint": WSOL_MINT,
            "amount": str(int(round(amount * (10 ** token_decimals)))),
            "swapMode": "ExactIn",
            "slippageBps": str(SLIPPAGE_BPS),
        }
    timeout = timeout or QUOTE_TIMEOUT
    last_error = None
    for url in QUOTE_URLS:
        try:
            response = requests.get(url, params=params, timeout=timeout)
            response.raise_for_status()
            data = response.json()
            result = _parse_quote_response(data, side, amount, decimals, mint)
            if result is not None:
                return result
        except (requests.RequestException, ValueError, TypeError, KeyError) as error:
            last_error = error
            continue
    return None


def quote_buy_fill(mint, sol_in):
    return quote_price(mint, "buy", sol_in)


def quote_sell_fill(mint, size_sol, price_sol):
    if not price_sol or price_sol <= 0:
        return None
    token_units = size_sol / price_sol
    if token_units <= 0:
        return None
    try:
        from metadata import cached_token_metadata

        decimals = (cached_token_metadata(mint) or {}).get("decimals")
    except Exception:
        decimals = None
    return quote_price(mint, "sell", token_units, decimals)


def swap_transaction(mint, side, amount, decimals=None, dry_run=True):
    """Build a Jupiter swap. dry_run=True returns the quote only; live signing
    requires a wallet key and is deliberately unimplemented (DRY_RUN stays on)."""
    if dry_run:
        return quote_price(mint, side, amount, decimals)
    raise NotImplementedError("live Jupiter execution is disabled (DRY_RUN)")
