# Deployment Guide

## Prerequisites

- Hetzner account — [console.hetzner.cloud](https://console.hetzner.cloud)
- SSH key pair on your local machine
- Python 3.11+ and Git (installed automatically via apt on the VM)

> **Security note — read before doing anything else:**
> The single most important security control in this deployment is the **Bybit API key IP whitelist**.
> The `.env` file on the server holds your API keys. If the server were ever compromised, those keys
> could be used to trade your account. With an IP whitelist set to your VM's public IP, the keys are
> useless from any other machine — even if leaked. **Do not create Bybit API keys without setting the
> IP restriction.** You will need the VM's IP address first (step 0b), then create the keys (step 2).

---

## 0. Provision and harden the VM

### 0a. Generate an SSH key (local machine — skip if you already have one)

```bash
ssh-keygen -t ed25519 -C "trading-engine"
# Press Enter for default path (~/.ssh/id_ed25519), set a passphrase
cat ~/.ssh/id_ed25519.pub   # copy this output
```

### 0b. Create the server on Hetzner

1. Log in to [console.hetzner.cloud](https://console.hetzner.cloud) → **New Project** → name it `trading-engine`
2. Inside the project → **Add Server**
3. **Location**: any EU location (Nuremberg, Falkenstein, Helsinki)
4. **Image**: Ubuntu 24.04 LTS
5. **Type**: Shared CPU → **CX11** (2 vCPU, 2 GB RAM, 20 GB SSD — €4.51/mo)
6. **SSH Keys**: click Add SSH Key → paste your `id_ed25519.pub` content → name it
7. **Firewall**: create a new firewall named `trading-engine-fw`:
   - Inbound rule: TCP port 22 (SSH) — your local IP only if you have a static IP, otherwise `0.0.0.0/0`
   - Inbound rule: delete the default ICMP rule if present (optional, not critical)
   - Outbound: leave default (allow all — the engine needs HTTPS to Bybit and Telegram)
8. Leave everything else default → **Create & Buy**
9. Copy the server's public IP from the dashboard

### 0c. First login and system update

```bash
ssh root@<SERVER_IP>
apt update && apt upgrade -y
```

### 0d. Create a non-root user

```bash
adduser ubuntu               # set a strong password when prompted
usermod -aG sudo ubuntu

# Copy SSH keys from root to the new user
rsync --archive --chown=ubuntu:ubuntu ~/.ssh /home/ubuntu
```

### 0e. Harden SSH

```bash
nano /etc/ssh/sshd_config
```

Find and set (or add) these lines:
```
PermitRootLogin no
PasswordAuthentication no
PubkeyAuthentication yes
```

```bash
systemctl restart ssh
```

Open a **second terminal** and verify you can SSH as ubuntu before closing the root session:
```bash
# in the new terminal on your local machine
ssh ubuntu@<SERVER_IP>
```

### 0f. Set up UFW firewall

```bash
ufw default deny incoming
ufw default allow outgoing
ufw allow 22/tcp
ufw enable
ufw status
```

Expected output:
```
Status: active
To                Action    From
--                ------    ----
22/tcp            ALLOW     Anywhere
```

The engine makes only outbound HTTPS calls (Bybit + Telegram). No inbound ports are needed beyond SSH.

### 0g. Install Python and Git

Ubuntu 24.04 ships with Python 3.12. Verify:

```bash
python3 --version    # should print Python 3.12.x
sudo apt install -y git python3-venv python3-pip
```

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
chmod 600 .env       # restrict to owner-read only — no other user can read it
nano .env
```

Fill in all four values:

```
BYBIT_API_KEY=...
BYBIT_API_SECRET=...
TELEGRAM_BOT_TOKEN=...
TELEGRAM_CHAT_ID=...
```

**Bybit API keys:**
Account → API Management → Create New Key:
- Type: **System-generated**
- Permissions: **Read** + **Derivatives** (Trade) only — do NOT enable Wallet or Withdrawal
- IP Access: **Restrict to IP** → enter your VM's public IP
- Copy the key and secret immediately (secret shown only once)

The IP restriction is the most important security measure: even if the key is leaked, it cannot be used from any other machine.

**Getting your Telegram chat ID:**
1. Create a bot via [@BotFather](https://t.me/BotFather) → `/newbot` → copy the token
2. Open your new bot in Telegram and send it any message
3. Open `https://api.telegram.org/bot<YOUR_TOKEN>/getUpdates` in a browser
4. Copy the `id` value inside `"chat"` from the response

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
