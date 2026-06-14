"""
Active position state persistence — reads and writes state/positions.json.

The state file is the engine's source of truth for the active trade across process
restarts. It stores everything the orchestrator needs to manage the three-leg exit:
entry time, exit deadline (entry_time + hold_hours), TP/SL prices, and the Bybit
order ID. On each hourly tick the orchestrator reads this file to determine whether
a time-based close is due, even if the process restarted between ticks.

Schema of state/positions.json when a position is active:
{
    "active":            true,
    "direction":         "long" | "short",
    "tier":              2 | 3,
    "entry_time_utc":    "2026-06-14T12:00:00+00:00",
    "exit_deadline_utc": "2026-06-14T30:00:00+00:00",
    "hold_hours":        18,
    "entry_price":       50000.0,
    "tp_price":          50500.0,
    "sl_price":          49500.0,
    "bybit_order_id":    "1234567890123456789",
    "qty_btc":           0.295,
    "position_notional": 14750.0,
    "margin_usdt":       2950.0,
    "risk_amount_usdt":  147.5
}

When no position is active the file contains {"active": false}.
"""

import json
from datetime import datetime, timezone
from typing import Dict, Optional

from config import STATE_DIR

POSITIONS_FILE = STATE_DIR / "positions.json"


def load_active_position() -> Optional[Dict]:
    """
    Reads the state file and returns the active position dict, or None if inactive.

    Returns None both when the file does not exist and when its "active" field is
    False, treating both as a clean "no position" state.

    Returns:
        Position state dict if a trade is active; None otherwise.
    """
    if not POSITIONS_FILE.exists():
        return None

    with open(POSITIONS_FILE, "r", encoding="utf-8") as file_handle:
        state = json.load(file_handle)

    if not state.get("active", False):
        return None

    return state


def save_active_position(
    direction: str,
    tier: int,
    entry_time_utc: datetime,
    hold_hours: int,
    entry_price: float,
    tp_price: float,
    sl_price: float,
    bybit_order_id: str,
    qty_btc: float,
    position_notional: float,
    margin_usdt: float,
    risk_amount_usdt: float,
) -> None:
    """
    Writes a new active position to the state file, computing the exit deadline.

    The exit_deadline_utc is computed as entry_time_utc + hold_hours and stored
    as an ISO 8601 string with UTC offset, so it remains unambiguous across
    restarts and timezone changes.

    Args:
        direction:         "long" or "short".
        tier:              2 or 3.
        entry_time_utc:    UTC-aware datetime of the entry candle close.
        hold_hours:        Optimal holding period — deadline = entry + hold_hours.
        entry_price:       Actual market order fill price (or candle close proxy).
        tp_price:          Take-profit price level in USDT.
        sl_price:          Stop-loss price level in USDT.
        bybit_order_id:    Order ID returned by Bybit for the market entry order.
        qty_btc:           BTC position size placed.
        position_notional: Total USDT notional exposure of the trade.
        margin_usdt:       USDT posted as margin for this trade.
        risk_amount_usdt:  USDT at risk if the stop-loss is hit.
    """
    from datetime import timedelta

    exit_deadline_utc = entry_time_utc + timedelta(hours=hold_hours)

    state = {
        "active":            True,
        "direction":         direction,
        "tier":              tier,
        "entry_time_utc":    entry_time_utc.isoformat(),
        "exit_deadline_utc": exit_deadline_utc.isoformat(),
        "hold_hours":        hold_hours,
        "entry_price":       entry_price,
        "tp_price":          tp_price,
        "sl_price":          sl_price,
        "bybit_order_id":    bybit_order_id,
        "qty_btc":           qty_btc,
        "position_notional": position_notional,
        "margin_usdt":       margin_usdt,
        "risk_amount_usdt":  risk_amount_usdt,
    }

    POSITIONS_FILE.parent.mkdir(parents=True, exist_ok=True)
    with open(POSITIONS_FILE, "w", encoding="utf-8") as file_handle:
        json.dump(state, file_handle, indent=2)


def clear_active_position() -> None:
    """
    Marks the state file as inactive, recording the time the position was cleared.

    Does not delete the file — keeps a minimal record with the cleared_at timestamp
    for traceability. The next load_active_position() call will return None.
    """
    cleared_state = {
        "active":     False,
        "cleared_at": datetime.now(tz=timezone.utc).isoformat(),
    }

    POSITIONS_FILE.parent.mkdir(parents=True, exist_ok=True)
    with open(POSITIONS_FILE, "w", encoding="utf-8") as file_handle:
        json.dump(cleared_state, file_handle, indent=2)


def is_deadline_passed(position: Dict) -> bool:
    """
    Returns True if the hold-window exit deadline has been reached or exceeded.

    Compares the stored exit_deadline_utc against the current UTC time. A deadline
    that has already passed by any amount triggers the time-based exit.

    Args:
        position: Active position dict returned by load_active_position().

    Returns:
        True if current UTC time >= exit_deadline_utc; False otherwise.
    """
    deadline = datetime.fromisoformat(position["exit_deadline_utc"])
    return datetime.now(tz=timezone.utc) >= deadline
