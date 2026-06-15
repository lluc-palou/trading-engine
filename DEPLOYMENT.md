# Deployment Guide

## Prerequisites

- Ubuntu 24.04 LTS VM — 1 vCPU, 1 GB RAM minimum (Hetzner CX11 €4/mo, DigitalOcean Droplet $6/mo, AWS t3.micro $8/mo)
- Python 3.11+
- Git

---

## 1. Clone and install

```bash
git clone https://github.com/lluc-palou/trading-engine.git
cd trading-engine
python3 -m venv .venv
.venv/bin/pip install -r requirements.txt
```

---

## 2. Configure credentials

```bash
cp .env.example .env
nano .env
```

Fill in all four values:

```
BYBIT_API_KEY=...
BYBIT_API_SECRET=...
TELEGRAM_BOT_TOKEN=...
TELEGRAM_CHAT_ID=...
```

**Getting your Telegram chat ID:**
1. Create a bot via [@BotFather](https://t.me/BotFather) → `/newbot` → copy the token
2. Open your new bot in Telegram and send it any message
3. Open `https://api.telegram.org/bot<YOUR_TOKEN>/getUpdates` in a browser
4. Copy the `id` value inside `"chat"` from the response

**Bybit API keys:**
Create keys at Account → API Management. Required permissions: Read + Trade (Derivatives). IP whitelist your VM's IP for security.

---

## 3. Verify connectivity

Run one cycle immediately in dry-run mode — no orders placed, no notifications sent. Confirms API keys are valid and candles are loading:

```bash
.venv/bin/python run.py --now --dry-run
```

Expected output:
```
[ENGINE_START] ... mode=DRY_RUN
[CYCLE_START] ... mode=DRY_RUN
[DATA] 1000 candles loaded. Last closed: ...
[NO_SIGNAL] ... (or [SIGNAL] if one fires)
[DRY_RUN] Would place ... (only if signal)
```

---

## 4. Validate with paper mode

Run the full pipeline with simulated trades and real Telegram notifications. No orders are placed. Uses 1,000 USDT simulated capital:

```bash
.venv/bin/python run.py --paper
```

What to expect:
- Engine wakes at H:01 UTC every hour
- When a signal fires you receive a **TRADE OPENED** Telegram message (prefixed with `[PAPER MODE]`)
- The simulated position is tracked in `state/positions.json` across restarts
- If the candle close price hits the TP or SL level you receive a **TRADE CLOSED** message
- At the hold-window deadline you receive a **TRADE CLOSED — TIME EXIT** message

Run paper mode for at least one full trade cycle (up to 24h depending on tier) to confirm the entire notification flow works end-to-end before deploying capital.

---

## 5. Install as a systemd service

Edit the three lines in `trading-engine.service` to match your setup:

```ini
User=ubuntu
WorkingDirectory=/home/ubuntu/trading-engine
ExecStart=/home/ubuntu/trading-engine/.venv/bin/python run.py
```

For paper mode during initial deployment, change `ExecStart` to:
```ini
ExecStart=/home/ubuntu/trading-engine/.venv/bin/python run.py --paper
```

Install and start:

```bash
sudo cp trading-engine.service /etc/systemd/system/
sudo systemctl daemon-reload
sudo systemctl enable --now trading-engine
```

Confirm it is running:

```bash
sudo systemctl status trading-engine
```

---

## 6. Monitor

Follow live output:
```bash
journalctl -u trading-engine -f
```

Read the persistent log file (rotates daily at midnight UTC, all files kept):
```bash
tail -f logs/trading.log
```

Check active position state:
```bash
cat state/positions.json
```

Check peak equity (drawdown circuit breaker reference):
```bash
cat state/capital.json
```

---

## 7. Switch from paper to live

Once you are satisfied the system is operating correctly:

1. Fund your Bybit account with USDT
2. Edit the service file — change `ExecStart` back to:
   ```ini
   ExecStart=/home/ubuntu/trading-engine/.venv/bin/python run.py
   ```
3. Reinstall and restart:
   ```bash
   sudo cp trading-engine.service /etc/systemd/system/
   sudo systemctl daemon-reload
   sudo systemctl restart trading-engine
   ```

The first live trade notification will arrive without any `[PAPER MODE]` prefix.

---

## 8. Update after a code change

```bash
git pull
sudo systemctl restart trading-engine
```

---

## 9. Stop / uninstall

```bash
sudo systemctl stop trading-engine
sudo systemctl disable trading-engine
```
