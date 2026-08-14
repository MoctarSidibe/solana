# 🌞 SunPark — Solana Sniper Bot Ecosystem

SunPark is an AI-powered Solana memecoin sniper ecosystem. It combines real-time
on-chain data with DeepSeek AI analysis to detect new token launches, whale
movements, liquidity events, and volume spikes — and decide whether they are
worth trading.

> ⚠️ **Disclaimer:** This software is for educational and research purposes.
> Trading memecoins is extremely risky. Use at your own risk.

---

## 🧠 What SunPark Does

- **Receives live Solana events** (new tokens, transfers, LP pools) via a Helius webhook.
- **Analyzes signals with DeepSeek AI** — judges whether a token is a gem or a rug pull.
- **Fetches on-chain data** through a resilient multi-RPC client with automatic failover.
- **Runs in DRY RUN mode by default** — it will *not* spend real SOL until you
  explicitly enable real trading.

---

## 📁 Project Structure

| File            | Purpose                                                                  |
|-----------------|--------------------------------------------------------------------------|
| `webhook.py`    | Flask webhook receiver. Listens for Helius events on port 5000 (`/webhook`). Prints token transfers, events, and SOL transfers. |
| `bot.py`        | New-token sniper. Detects new tokens and asks DeepSeek whether to buy. Runs in dry-run mode. |
| `brain.py`      | AI analysis module. Wraps DeepSeek calls: token judging, whale trade analysis, liquidity evaluation, volume spike detection, rugpull detection. |
| `onchain.py`    | Solana RPC client. Tries multiple RPC endpoints in order, fetches wallet transaction history, and feeds transaction data to the AI. |
| `.env`          | **Secrets — never commit this file.** Holds your `DEEPSEEK_API_KEY` and `SOLANA_RPC_URL`. |
| `.gitignore`    | Prevents secrets, logs, and binaries from being committed to Git. |
| `deploy.sh`     | One-shot deployment script for a Linux server (Contabo / VPS). |
| `sunpark.service`| systemd unit file so the bot runs as a background service. |

---

## 🚀 Local Setup (Windows / Mac / Linux)

### 1. Requirements

- Python 3.9+
- A Helius account (for the webhook) → https://helius.dev
- A DeepSeek API key → https://platform.deepseek.com
- (Optional) `cloudflared` if you want a public tunnel for the webhook

### 2. Install dependencies

```bash
pip install -r requirements.txt
```

> If you don't have a `requirements.txt` yet, install manually:
> ```bash
> pip install flask requests python-dotenv
> ```

### 3. Configure secrets

Create a `.env` file in the project root:

```env
DEEPSEEK_API_KEY=your_deepseek_api_key_here
SOLANA_RPC_URL=https://your-helius-endpoint.helius-rpc.com
```

> `.env` is ignored by Git. Your keys will never be committed or pushed.

### 4. Run the modules

```bash
# Test the AI brain (token analysis)
python brain.py

# Test the on-chain RPC + AI transaction analysis
python onchain.py

# Test the sniper bot (dry run — safe, no real SOL spent)
python bot.py

# Start the webhook receiver (listens on port 5000)
python webhook.py
```

### 5. Point Helius to your webhook

1. Go to your Helius dashboard → **Webhooks**.
2. Add a webhook with URL: `https://<your-host>/webhook`
   - Locally: use a tunnel (`cloudflared tunnel --url http://localhost:5000`)
   - On a server: `http://<server-ip>:5000/webhook` (or via a domain/SSL proxy)
3. Select the account/address types you want to watch.
4. Start receiving events — they will print in the terminal.

---

## 🖥️ Deploying to a Server (Contabo / any Ubuntu VPS)

### Option A — Automatic (recommended)

1. Push this repo to GitHub.
2. `ssh root@<your-server-ip>`
3. Create the `.env` file securely:

```bash
mkdir -p /root/sunpark
nano /root/sunpark/.env
# paste your keys, save & exit (Ctrl+X, Y, Enter)
```

4. Run the deployment script:

```bash
bash <(curl -sL https://raw.githubusercontent.com/MoctarSidibe/solana/main/deploy.sh)
```

### Option B — Manual

```bash
ssh root@<your-server-ip>
apt update && apt install -y git python3 python3-pip
mkdir -p /root/sunpark
cd /root/sunpark
git clone https://github.com/MoctarSidibe/solana.git .
pip3 install -r requirements.txt --break-system-packages
cp /root/sunpark/sunpark.service /etc/systemd/system/
systemctl daemon-reload
systemctl enable sunpark
systemctl start sunpark
systemctl status sunpark
```

### Managing the service

```bash
systemctl status sunpark     # check status
journalctl -u sunpark -f     # follow live logs
systemctl restart sunpark    # restart the bot
systemctl stop sunpark       # stop the bot
```

---

## 🔒 Security Notes

- **Never commit `.env`.** It contains API keys and (potentially) wallet secrets.
- Never share your DeepSeek key, Helius URL, or any private keys.
- The bot ships in **dry-run mode** (`DRY_RUN = True` in `bot.py`) so it can never
  accidentally spend real SOL until you flip that switch deliberately.

---

## 🛠️ Troubleshooting

| Problem                            | Fix                                                     |
|------------------------------------|---------------------------------------------------------|
| `DEEPSEEK_API_KEY not found`       | Make sure `.env` exists and the key is set correctly.  |
| `No RPC endpoints available`       | Check `SOLANA_RPC_URL` in `.env`.                       |
| Webhook not receiving events       | Confirm Helius webhook URL is publicly reachable.       |
| Service won't start on server      | Run `journalctl -u sunpark -n 50` to see the error.     |

---

## 📄 License

Private project. All rights reserved.
