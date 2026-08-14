import os
import json
import requests
from dotenv import load_dotenv

load_dotenv()

DEEPSEEK_API_KEY = os.getenv("DEEPSEEK_API_KEY")
DEEPSEEK_URL = "https://api.deepseek.com/v1/chat/completions"

# Multiple RPC endpoints - the bot will try them in order
RPC_ENDPOINTS = [
    os.getenv("SOLANA_RPC_URL"),  # Your Helius endpoint
    "https://api.mainnet-beta.solana.com",  # Public Solana endpoint
    "https://solana-api.projectserum.com",  # Backup public endpoint
    "https://rpc.ankr.com/solana",  # Ankr free endpoint
]

# Remove any None values from the list
RPC_ENDPOINTS = [url for url in RPC_ENDPOINTS if url is not None]

if not RPC_ENDPOINTS:
    raise ValueError("No RPC endpoints available. Check your .env file.")

current_rpc_index = 0

def rpc_request(method, params):
    """Send a JSON-RPC request to Solana, trying multiple endpoints if needed."""
    global current_rpc_index
    
    headers = {"Content-Type": "application/json"}
    payload = {
        "jsonrpc": "2.0",
        "id": 1,
        "method": method,
        "params": params
    }
    
    # Try each endpoint until one works
    for attempt in range(len(RPC_ENDPOINTS)):
        url = RPC_ENDPOINTS[current_rpc_index]
        
        try:
            print(f"  Trying RPC: {url[:50]}...")
            resp = requests.post(str(url), json=payload, headers=headers, timeout=10)
            data = resp.json()
            
            if "error" in data:
                print(f"  RPC Error: {data['error']}")
                # Move to next endpoint
                current_rpc_index = (current_rpc_index + 1) % len(RPC_ENDPOINTS)
                continue
                
            return data.get("result")
            
        except Exception as e:
            print(f"  Connection failed: {str(e)[:80]}")
            # Move to next endpoint
            current_rpc_index = (current_rpc_index + 1) % len(RPC_ENDPOINTS)
            continue
    
    print("❌ All RPC endpoints failed.")
    return None


def get_latest_transactions(wallet_address, limit=5):
    """Fetch recent transaction signatures for a wallet."""
    signatures = rpc_request("getSignaturesForAddress", [wallet_address, {"limit": limit}])
    if not signatures:
        print("No transactions found or invalid wallet.")
        return []
    return signatures


def get_transaction_details(signature):
    """Fetch the full details of a transaction."""
    tx_data = rpc_request("getTransaction", [
        signature,
        {"encoding": "jsonParsed", "maxSupportedTransactionVersion": 0}
    ])
    return tx_data


def analyze_transaction_with_ai(tx_data):
    """Send raw transaction data to DeepSeek to understand if it's a trading signal."""
    if DEEPSEEK_API_KEY is None:
        print("❌ DEEPSEEK_API_KEY not found in .env file.")
        return None
        
    headers = {
        "Authorization": f"Bearer {DEEPSEEK_API_KEY}",
        "Content-Type": "application/json"
    }
    
    system_prompt = """You are a Solana on-chain analyst.
    Look at the transaction data. 
    Determine if it involves a significant buy or sell of a Solana token.
    Ignore wallet maintenance (like closing accounts, burning dust, or claiming rent).
    Return ONLY JSON:
    {
        "signal": "BUY" / "SELL" / "IGNORE",
        "token_mint": "address_or_null",
        "confidence": 0.0,
        "reason": "short explanation"
    }"""
    
    tx_string = json.dumps(tx_data)
    
    payload = {
        "model": "deepseek-chat",
        "messages": [
            {"role": "system", "content": system_prompt},
            {"role": "user", "content": f"Transaction Data:\n{tx_string[:6000]}"}
        ],
        "temperature": 0.1,
        "response_format": {"type": "json_object"}
    }
    
    try:
        resp = requests.post(DEEPSEEK_URL, headers=headers, json=payload, timeout=20)
        content = resp.json()["choices"][0]["message"]["content"]
        return json.loads(content)
    except Exception as e:
        print(f"AI Error: {e}")
        return None


# ----------------------------------------------
# TEST: Run this file directly
# ----------------------------------------------
if __name__ == "__main__":
    print("=== ONCHAIN MODULE TEST ===\n")
    
    # Use a known active wallet
    test_wallet = "6dhTynDkYsVM7cbF7TKfC9DWB636TcEM935fq7JzL2ES"
    print(f"Checking transactions for: {test_wallet}\n")
    
    tx_list = get_latest_transactions(test_wallet)
    
    if tx_list:
        first_sig = tx_list[0]["signature"]
        print(f"\nFetching details for: {first_sig[:30]}...")
        details = get_transaction_details(first_sig)
        
        if details:
            print("\nAsking DeepSeek to analyze this transaction...")
            signal = analyze_transaction_with_ai(details)
            
            if signal:
                print("\n🎯 AI Analysis Result:")
                print(json.dumps(signal, indent=2))
            else:
                print("\n❌ Failed to analyze transaction.")
        else:
            print("\n❌ Failed to fetch transaction details.")
    else:
        print("\n❌ No transactions found.")
    
    print("\n=== TEST COMPLETE ===")