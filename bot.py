import os
import json
import requests
from dotenv import load_dotenv

# ----------------------------------------------
# CONFIGURATION
# ----------------------------------------------
load_dotenv()

DEEPSEEK_API_KEY = os.getenv("DEEPSEEK_API_KEY")
DEEPSEEK_URL = "https://api.deepseek.com/v1/chat/completions"

# Sniper Settings
DRY_RUN = True
POSITION_SIZE_SOL = 0.1  # How much SOL to spend per trade (~$15)
CONFIDENCE_THRESHOLD = 0.75  # Only buy if AI confidence is above this

# ----------------------------------------------
# DEEPSEEK AI BRAIN
# ----------------------------------------------
def judge_new_token(name, symbol, description=""):
    """Ask DeepSeek if this new token is worth buying."""
    if DEEPSEEK_API_KEY is None:
        print("❌ DEEPSEEK_API_KEY not found in .env file.")
        return None
        
    headers = {
        "Authorization": f"Bearer {DEEPSEEK_API_KEY}",
        "Content-Type": "application/json"
    }
    
    system_prompt = """You are a Solana memecoin sniper bot.
    Analyze this new token and decide if it's worth buying.
    Return ONLY JSON:
    {
        "buy": true/false,
        "confidence": 0.0,
        "reason": "short explanation"
    }
    Consider these factors:
    - Is the name catchy or memorable?
    - Is it based on a known meme or trend?
    - Does it look like a scam or rug pull?
    - Does the description seem genuine?
    """
    
    user_content = f"Token Name: {name}\nSymbol: {symbol}\nDescription: {description}"
    
    payload = {
        "model": "deepseek-chat",
        "messages": [
            {"role": "system", "content": system_prompt},
            {"role": "user", "content": user_content}
        ],
        "temperature": 0.1,
        "response_format": {"type": "json_object"}
    }
    
    try:
        resp = requests.post(DEEPSEEK_URL, headers=headers, json=payload, timeout=20)
        content = resp.json()["choices"][0]["message"]["content"]
        return json.loads(content)
    except Exception as e:
        print(f"❌ DeepSeek Error: {e}")
        return None


# ----------------------------------------------
# NEW TOKEN SNIPER
# ----------------------------------------------
def process_new_token(token_data):
    """Process a new token and decide whether to buy."""
    name = token_data.get("name", "Unknown")
    symbol = token_data.get("symbol", "?")
    description = token_data.get("description", "No description")
    mint_address = token_data.get("mint", "No address")
    
    print("\n" + "=" * 50)
    print(f"🆕 New Token Detected!")
    print(f"   Name: {name}")
    print(f"   Symbol: ${symbol}")
    print(f"   Mint: {mint_address}")
    print(f"   Description: {description[:60]}...")
    print("=" * 50)
    
    print("\n🤖 Asking DeepSeek for analysis...")
    result = judge_new_token(name, symbol, description)
    
    if result is None:
        print("❌ AI failed to return a result.")
        return
    
    confidence = result.get("confidence", 0)
    buy_signal = result.get("buy", False)
    reason = result.get("reason", "No reason given")
    
    print(f"\n📊 AI Analysis:")
    print(f"   Buy Signal: {buy_signal}")
    print(f"   Confidence: {confidence}")
    print(f"   Reason: {reason}")
    
    if buy_signal and confidence > CONFIDENCE_THRESHOLD:
        if DRY_RUN:
            print(f"\n🧪 [DRY RUN] Would buy ${symbol} with {POSITION_SIZE_SOL} SOL")
            print(f"   💰 Estimated cost: ${POSITION_SIZE_SOL * 150:.2f} (at $150/SOL)")
        else:
            print(f"\n✅ REAL BUY: Buying ${symbol} with {POSITION_SIZE_SOL} SOL")
            # Real buy logic goes here later
    else:
        print(f"\n⏭️ Skipped. Token did not pass the filter.")


# ----------------------------------------------
# TESTING
# ----------------------------------------------
if __name__ == "__main__":
    print("=" * 50)
    print("🤖 SunPark - New Token Sniper (Dry Run Mode)")
    print("=" * 50)
    
    # Test with a realistic meme token
    test_token = {
        "name": "Dog With Hat",
        "symbol": "DOGHAT",
        "description": "The famous dog with a hat meme, now on Solana. Community driven.",
        "mint": "7K8L9M0N1O2P3Q4R5S6T7U8V9W0X1Y2Z3A4B5C6D7"
    }
    process_new_token(test_token)
    
    # Test with an obvious scam
    print("\n\n" + "=" * 50)
    print("Second test: Obvious scam token")
    print("=" * 50)
    
    scam_token = {
        "name": "Definitely Not A Scam",
        "symbol": "SCAM",
        "description": "Trust me bro, this will 100x. Send all your SOL.",
        "mint": "SCAMSCAMSCAMSCAMSCAMSCAMSCAMSCAMSCAMSCAM"
    }
    process_new_token(scam_token)