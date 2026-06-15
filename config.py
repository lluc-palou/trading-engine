"""
Trading engine configuration — environment-driven settings for the Bybit API,
instrument parameters, and execution controls.

All secrets are loaded from environment variables (BYBIT_API_KEY, BYBIT_API_SECRET).
A .env file at the project root is supported via python-dotenv and takes precedence
over system-level environment variables during local development.
"""

import os
from pathlib import Path

from dotenv import load_dotenv

# Load .env file from the project root when present
load_dotenv(Path(__file__).parent / ".env")

# ── Bybit API credentials ────────────────────────────────────────────────────
BYBIT_API_KEY: str = os.environ.get("BYBIT_API_KEY", "")
BYBIT_API_SECRET: str = os.environ.get("BYBIT_API_SECRET", "")
BYBIT_BASE_URL: str = "https://api.bybit.com"

# ── Telegram notifications ───────────────────────────────────────────────────
# Set both variables to enable Telegram alerts (TRADE_OPENED / TRADE_CLOSED / ERROR).
# If either is empty the engine falls back to the no-op StubNotifier silently.
TELEGRAM_BOT_TOKEN: str = os.environ.get("TELEGRAM_BOT_TOKEN", "")
TELEGRAM_CHAT_ID: str = os.environ.get("TELEGRAM_CHAT_ID", "")

# ── Instrument ───────────────────────────────────────────────────────────────
SYMBOL: str = "BTCUSDT"
CATEGORY: str = "linear"  # USDT-settled perpetual futures
INTERVAL: str = "60"      # Bybit interval notation for 1H (minutes as string)
INTERVAL_MS: int = 3_600_000  # 1 hour expressed in milliseconds

# ── Execution and risk controls ──────────────────────────────────────────────
# Exchange leverage applied to every order and set on the account.
LEVERAGE: int = 5

# Fraction of total wallet balance committed as margin per trade.
# At LEVERAGE=5, CAPITAL_FRACTION=1.0 posts the full account as margin and
# controls a 5× notional — the combination that targets P(ruin −20%) ≈ 10%.
CAPITAL_FRACTION: float = 1.0

MAX_ACTIVE_POSITIONS: int = 1  # One trade at a time — justified by signal frequency (~59/year)

# ── Paper mode ───────────────────────────────────────────────────────────────
# Simulated capital used when running with --paper (no real funds committed).
# Sizing and notifications reflect this amount; no orders are placed on Bybit.
PAPER_CAPITAL_USDT: float = 1_000.0

# ── Project paths ────────────────────────────────────────────────────────────
PROJECT_ROOT: Path = Path(__file__).parent
STATE_DIR: Path = PROJECT_ROOT / "state"
LOGS_DIR: Path = PROJECT_ROOT / "logs"

# Ensure runtime directories exist on first import
for _runtime_dir in (STATE_DIR, LOGS_DIR):
    _runtime_dir.mkdir(parents=True, exist_ok=True)

# ── Risk guard thresholds ────────────────────────────────────────────────────
# Minimum account equity to attempt a trade. Set to 0 — the real gate is the
# qty viability check in guard.py (_check_qty_viability), which blocks the order
# if the computed BTC quantity is below the exchange minimum step (0.001 BTC).
MIN_CAPITAL_USDT: float = 0.0

# Halt trading if equity drops more than this fraction from the recorded peak equity.
# At 35% drawdown the strategy edge is likely compromised or conditions have changed.
MAX_DRAWDOWN_PCT: float = 0.35

# SL must sit at least this far (as a fraction of entry price) above the estimated
# liquidation price to ensure the stop fires before the position is liquidated.
MIN_LIQUIDATION_BUFFER_PCT: float = 0.05

# ── Orchestrator timing ──────────────────────────────────────────────────────
# Run the detection cycle this many minutes after each hourly candle close (H:00 UTC).
# 1 minute is enough for Bybit to finalise the candle and minimises entry price drift.
WAKEUP_OFFSET_MINUTES: int = 1

# ── Capital state ────────────────────────────────────────────────────────────
CAPITAL_STATE_FILE: Path = STATE_DIR / "capital.json"
