"""
Stub notifier — no-op implementation of BaseNotifier used as the default backend.

Swap this for a real implementation (Telegram, email) in run.py once a notification
channel is configured. The stub silently discards all events so the orchestrator
runs cleanly without any external dependencies.
"""

from src.notifications.base import BaseNotifier


class StubNotifier(BaseNotifier):
    """
    No-op notifier that silently discards all events.

    Used as the default notification backend until a real channel is configured.
    """

    def send(self, event: str, message: str) -> None:
        """
        Accepts and discards the notification without any side effects.

        Args:
            event:   Event label (ignored).
            message: Event description (ignored).
        """
