"""
Trading engine entry point — parses CLI arguments and starts the orchestrator loop.

Modes:
    (none)       Live trading — places real orders, full notifications.
    --dry-run    Detection and sizing only — no orders, no notifications, no state.
    --paper      Full pipeline with simulated trades — no real orders, full
                 notifications prefixed with [PAPER]. Use this to validate the
                 complete wakeup / signal / notification cycle before deploying capital.

Flags:
    --now        Run one detection cycle immediately before entering the scheduled loop.

Notifications are sent via Telegram when TELEGRAM_BOT_TOKEN and TELEGRAM_CHAT_ID
are set in .env. If either is missing the engine runs without notifications.

Logs are written to both stdout and logs/trading.log (daily rotation, kept forever).
"""

import argparse
import logging
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).parent))

from config import LOGS_DIR, TELEGRAM_BOT_TOKEN, TELEGRAM_CHAT_ID
from src.engine.orchestrator import run_forever, run_once, setup_logging
from src.notifications.stub import StubNotifier
from src.notifications.telegram import build_notifier

logger = logging.getLogger(__name__)


def main() -> None:
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
        help="Detection and sizing only — no orders, no state, no notifications.",
    )
    parser.add_argument(
        "--paper",
        action="store_true",
        dest="paper_mode",
        help=(
            "Full pipeline with simulated trades — no real orders placed. "
            "Uses PAPER_CAPITAL_USDT for sizing. All Telegram notifications fire "
            "with a [PAPER] prefix. Use to validate deployment before live capital."
        ),
    )
    args = parser.parse_args()

    if args.dry_run and args.paper_mode:
        print("Error: --dry-run and --paper are mutually exclusive.", file=sys.stderr)
        sys.exit(1)

    log_file = str(LOGS_DIR / "trading.log")
    setup_logging(log_file_path=log_file)

    notifier = build_notifier(TELEGRAM_BOT_TOKEN, TELEGRAM_CHAT_ID)
    if notifier is None:
        notifier = StubNotifier()
        logger.info("[NOTIFIER] Telegram credentials not set — running without notifications.")
    else:
        logger.info("[NOTIFIER] Telegram notifications enabled.")

    if args.now:
        run_once(notifier=notifier, dry_run=args.dry_run, paper_mode=args.paper_mode)

    run_forever(notifier=notifier, dry_run=args.dry_run, paper_mode=args.paper_mode)


if __name__ == "__main__":
    main()
