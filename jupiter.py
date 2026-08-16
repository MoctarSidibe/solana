"""Jupiter quote client for honest paper-fill pricing.

SunPark uses Jupiter only as a keyless pricing oracle for the paper account:
a real /v6/quote models price impact, slippage and the platform fee into the
effective fill price so paper PnL reflects reality. The swap endpoint needs a
wallet key to sign and is deliberately left as a dry-run stub behind
`DRY_RUN`; live execution stays disabled.
"""

import os

import requests
from dotenv import load_dotenv

load_dotenv()

WSOL_MINT = "So11111111111111111111111111111111111111112"
QUOTE_URL = os.getenv("SUNPARK_JUPITER_QUOTE_URL", "https://quote-api.jup.ag/v6/quote")
SLIPPAGE_BPS = int(os.getenv("SUNPARK_JUPITER_SLIPPAGE_BPS", "300"))
FEE_BPS = int(os.getenv("SUNPARK_JUPITER_FEE_BPS", "30"))
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


def quote_price(mint, side, amount, decimals=None, timeout=5):
    """Real Jupiter route quote -> effective fill price in SOL per token.

    side 'buy':  amount is SOL to spend, mint is the token bought.
    side 'sell': amount is whole token units, mint is the token sold.
    Returns a dict with price_sol/impact/fee or None on any failure.
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
    try:
        response = requests.get(QUOTE_URL, params=params, timeout=timeout)
        response.raise_for_status()
        data = response.json()
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
    except (requests.RequestException, ValueError, TypeError, KeyError):
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
