import json
import requests
from dotenv import load_dotenv
import os

load_dotenv()

DEEPSEEK_API_KEY = os.getenv("DEEPSEEK_API_KEY")
DEEPSEEK_URL = "https://api.deepseek.com/v1/chat/completions"


def analyze_with_deepseek(prompt, text, max_length=6000):
    """Send text to DeepSeek and get JSON back."""
    if DEEPSEEK_API_KEY is None:
        print("❌ DEEPSEEK_API_KEY not found.")
        return None
        
    headers = {
        "Authorization": f"Bearer {DEEPSEEK_API_KEY}",
        "Content-Type": "application/json"
    }
    
    payload = {
        "model": "deepseek-chat",
        "messages": [
            {"role": "system", "content": prompt},
            {"role": "user", "content": text[:max_length]}
        ],
        "temperature": 0.1,
        "response_format": {"type": "json_object"}
    }
    
    try:
        resp = requests.post(DEEPSEEK_URL, headers=headers, json=payload, timeout=20)
        if resp.status_code != 200:
            print(f"❌ DeepSeek HTTP {resp.status_code}: {resp.text[:300]}")
            return None
        data = resp.json()
        if "choices" not in data or not data["choices"]:
            print(f"❌ DeepSeek Error: unexpected response: {str(data)[:300]}")
            return None
        content = data["choices"][0]["message"]["content"]
        return json.loads(content)
    except Exception as e:
        print(f"❌ DeepSeek Error: {e}")
        return None


def judge_new_token(name, symbol, description=""):
    """Judge if a new token is worth buying."""
    prompt = """You are a Solana memecoin sniper.
    Analyze this new token. Return ONLY JSON:
    {
        "buy": true/false,
        "confidence": 0.0,
        "reason": "short explanation"
    }
    Consider: Is the name catchy? Is it a known meme? Does it look like a scam?"""
    
    text = f"Token Name: {name}\nSymbol: {symbol}\nDescription: {description}"
    return analyze_with_deepseek(prompt, text)


def judge_whale_trade(tx_data):
    """Judge if a whale trade is worth copying."""
    prompt = """You are a Solana on-chain analyst.
    Look at this transaction. Return ONLY JSON:
    {
        "signal": "BUY"/"SELL"/"IGNORE",
        "token_mint": "address_or_null",
        "confidence": 0.0,
        "reason": "short explanation"
    }
    Ignore wallet maintenance (burning dust, closing accounts)."""
    
    return analyze_with_deepseek(prompt, json.dumps(tx_data))


def judge_liquidity_event(pool_data):
    """Judge if a new LP pool is worth entering."""
    prompt = """You are a Solana liquidity analyst reading live on-chain flow stats for a new LP pool.
    Look at the initial liquidity and subsequent buy pressure. Return ONLY JSON:
    {
        "signal": "BUY"/"SELL"/"IGNORE",
        "token_mint": "address_or_null",
        "confidence": 0.0,
        "reason": "short explanation"
    }
    Consider:
    - Is the initial liquidity healthy (SOL floor)?
    - Is buy flow following the pool open or is it fading?
    - Distribution: unique buyers vs a single whale?
    - Red flags: no mint, tiny size, immediate sell pressure."""
    return analyze_with_deepseek(prompt, json.dumps(pool_data))


def judge_volume_spike(token_data):
    """Judge if a volume spike is a breakout or fake pump."""
    prompt = """You are a Solana momentum trader reading live on-chain flow stats for one token.
    Analyze the rolling volume, buy/sell pressure, and trader distribution. Return ONLY JSON:
    {
        "signal": "BUY"/"SELL"/"IGNORE",
        "token_mint": "address_or_null",
        "confidence": 0.0,
        "reason": "short explanation"
    }
    Consider:
    - Is volume organic (many unique buyers) or one wallet washing?
    - Buy/sell SOL ratio: is pressure positive or fading?
    - Age and initial liquidity: too fresh/no depth or healthy?
    - Red flags: few buyers, high sell pressure, no metadata."""
    return analyze_with_deepseek(prompt, json.dumps(token_data))


def judge_rugpull(dev_wallet_data):
    """Detect if a dev is dumping tokens."""
    prompt = """You are a Solana risk analyst.
    Look at this wallet activity. Return ONLY JSON:
    {
        "rugpull": true/false,
        "confidence": 0.0,
        "reason": "short explanation"
    }
    Consider: Is the dev selling large amounts suddenly?"""
    
    return analyze_with_deepseek(prompt, json.dumps(dev_wallet_data))