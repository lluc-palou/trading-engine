"""
Base notifier interface — abstract contract for all notification backends.

The orchestrator calls notifier.send(event, message) at key lifecycle points
(signal detected, trade opened, trade closed). Concrete implementations — Telegram,
email, or any other channel — subclass BaseNotifier and override send(). The engine
ships with StubNotifier (no-op) by default; swap it in run.py when a real backend
is configured.
"""

from abc import ABC, abstractmethod


class BaseNotifier(ABC):
    """
    Abstract base class for notification backends.

    Subclasses must implement send(). Failures inside send() should be caught and
    logged by the implementation itself; they must never propagate to the orchestrator
    since a notification failure should not interrupt the trading cycle.
    """

    @abstractmethod
    def send(self, event: str, message: str) -> None:
        """
        Sends a notification for the given trading event.

        Args:
            event:   Short label identifying the event type, e.g. "SIGNAL",
                     "ENTRY", "TP_HIT", "SL_HIT", "TIME_EXIT", "GUARD_BLOCK".
            message: Human-readable description of the event with relevant details.
        """
