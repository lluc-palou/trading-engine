"""
Telegram notification backend — sends engine events to a Telegram chat.

Requires TELEGRAM_BOT_TOKEN and TELEGRAM_CHAT_ID to be set in the environment
(loaded via .env / config.py). Messages are sent as HTML-formatted text so
bold labels render cleanly on the Telegram mobile client.

Three event types are supported:
    TRADE_OPENED — a new position has been placed on Bybit.
    TRADE_CLOSED — the position has been closed (TP, SL, or time exit).
    ERROR        — anything unexpected: guard blocks, cycle errors, stale state.

Notification failures are logged but never raise — the engine must not crash
because a Telegram message failed to deliver.
"""

import json
import logging
import urllib.error
import urllib.request
from typing import Optional

from src.notifications.base import BaseNotifier

logger = logging.getLogger(__name__)

_TELEGRAM_API = "https://api.telegram.org/bot{token}/sendMessage"


class TelegramNotifier(BaseNotifier):
    """
    Sends trade lifecycle messages to a Telegram chat via the Bot API.

    Args:
        bot_token: Telegram bot token from BotFather (TELEGRAM_BOT_TOKEN).
        chat_id:   Numeric or @username chat ID to send messages to (TELEGRAM_CHAT_ID).
    """

    def __init__(self, bot_token: str, chat_id: str) -> None:
        self._url = _TELEGRAM_API.format(token=bot_token)
        self._chat_id = chat_id

    def send(self, event: str, message: str) -> None:
        """
        Posts a message to the configured Telegram chat.

        Args:
            event:   Event type string (TRADE_OPENED, TRADE_CLOSED, ERROR).
            message: Pre-formatted HTML message body built by the orchestrator.
        """
        payload = json.dumps(
            {
                "chat_id": self._chat_id,
                "text": message,
                "parse_mode": "HTML",
            }
        ).encode("utf-8")

        req = urllib.request.Request(
            url=self._url,
            data=payload,
            headers={"Content-Type": "application/json"},
            method="POST",
        )

        try:
            with urllib.request.urlopen(req, timeout=10) as resp:
                if resp.status != 200:
                    logger.warning(
                        f"[TELEGRAM] Unexpected HTTP {resp.status} for event={event}"
                    )
                else:
                    logger.debug(f"[TELEGRAM] Sent event={event}")
        except urllib.error.URLError as exc:
            logger.error(f"[TELEGRAM] Failed to deliver event={event}: {exc}")
        except Exception as exc:
            logger.error(f"[TELEGRAM] Unexpected error for event={event}: {exc}")


def build_notifier(
    bot_token: Optional[str], chat_id: Optional[str]
) -> Optional["TelegramNotifier"]:
    """
    Returns a TelegramNotifier when both credentials are provided, else None.

    Args:
        bot_token: Value of TELEGRAM_BOT_TOKEN from config.
        chat_id:   Value of TELEGRAM_CHAT_ID from config.

    Returns:
        TelegramNotifier instance, or None if either credential is missing.
    """
    if bot_token and chat_id:
        return TelegramNotifier(bot_token=bot_token, chat_id=chat_id)
    return None
