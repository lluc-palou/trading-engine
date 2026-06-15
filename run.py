"""
Trading engine entry point — parses CLI arguments and starts the orchestrator loop.

Usage:
    python run.py                # Follow the H:03 UTC schedule (production mode)
    python run.py --now          # Run one cycle immediately, then follow the schedule
    python run.py --dry-run      # Detection and sizing only — no orders placed
    python run.py --now --dry-run

Notifications are sent via Telegram when TELEGRAM_BOT_TOKEN and TELEGRAM_CHAT_ID
are set in .env. If either is missing the engine runs silently with no notifications.

Logs are written to both stdout and logs/trading.log.
"""

import argparse
import sys
from pathlib import Path

# Make the project root importable from any working directory
sys.path.insert(0, str(Path(__file__).parent))

from config import LOGS_DIR, TELEGRAM_BOT_TOKEN, TELEGRAM_CHAT_ID
from src.engine.orchestrator import run_forever, run_once, setup_logging
from src.notifications.stub import StubNotifier
from src.notifications.telegram import build_notifier


def main() -> None:
    """
    Parses CLI arguments, selects the notification backend, and starts the orchestrator.

    Uses TelegramNotifier when TELEGRAM_BOT_TOKEN and TELEGRAM_CHAT_ID are set in
    the environment; falls back to StubNotifier (no-op) otherwise.
    """
    parser = argparse.ArgumentParser(
        description="Momentum-exhaustion-reversal trading engine — Bybit BTCUSDT perpetual."
    )
    parser.add_argument(
        "--now",
        action="store_true",
        help="Run one detection cycle immediately before entering the scheduled loop.",
    )
    parser.add_argument(
        "--dry-run",
        action="store_true",
        dest="dry_run",
        help="Run detection and sizing without placing any orders or writing state.",
    )
    args = parser.parse_args()

    log_file = str(LOGS_DIR / "trading.log")
    setup_logging(log_file_path=log_file)

    import logging
    logger = logging.getLogger(__name__)

    notifier = build_notifier(TELEGRAM_BOT_TOKEN, TELEGRAM_CHAT_ID)
    if notifier is None:
        notifier = StubNotifier()
        logger.info("[NOTIFIER] Telegram credentials not set — running without notifications.")
    else:
        logger.info("[NOTIFIER] Telegram notifications enabled.")

    if args.now:
        run_once(notifier=notifier, dry_run=args.dry_run)

    run_forever(notifier=notifier, dry_run=args.dry_run)


if __name__ == "__main__":
    main()
