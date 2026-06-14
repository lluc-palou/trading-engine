"""
Order management — entry, time-based close, and cancellation for BTCUSDT linear perpetual.

Every trade opened by the engine follows a three-leg exit structure:
    1. Take Profit — conditional order placed at entry, fires if price reaches tp_price.
    2. Stop Loss   — conditional order placed at entry, fires if price reaches sl_price.
    3. Hold window — if neither TP nor SL is triggered within hold_hours of entry,
                     the orchestrator calls close_position_market() to exit at market.

Leg 1 and 2 are handled natively by Bybit (takeProfit / stopLoss fields on the entry
order). Leg 3 is handled by the orchestrator checking the exit_deadline from state.py
and calling close_position_market() followed by cancel_all_active_orders() to clean up
any remaining conditional orders.
"""

import math
from typing import Dict

from config import CATEGORY, LEVERAGE, SYMBOL

from src.execution.client import signed_post, signed_get

# ── BTCUSDT instrument precision ─────────────────────────────────────────────
# Bybit BTCUSDT linear perpetual: qty step 0.001 BTC, price tick 0.10 USDT.
BTC_QTY_STEP: float = 0.001
BTC_PRICE_TICK: float = 0.1

# One-way position mode (positionIdx=0); the engine never uses hedge mode.
_POSITION_IDX: int = 0


def _direction_to_side(direction: str) -> str:
    """
    Converts strategy direction label to Bybit order side string.

    Args:
        direction: "long" or "short".

    Returns:
        "Buy" for long, "Sell" for short.
    """
    return "Buy" if direction == "long" else "Sell"


def _closing_side(direction: str) -> str:
    """
    Returns the opposite side needed to close an existing position.

    Args:
        direction: "long" or "short" of the position being closed.

    Returns:
        "Sell" to close a long, "Buy" to close a short.
    """
    return "Sell" if direction == "long" else "Buy"


def _round_price(price: float) -> str:
    """
    Rounds a price to BTCUSDT tick size and formats it as a string for the API.

    Args:
        price: Raw price in USDT.

    Returns:
        Price rounded to 1 decimal place, formatted as a string (e.g. "50750.5").
    """
    rounded = round(round(price / BTC_PRICE_TICK) * BTC_PRICE_TICK, 1)
    return f"{rounded:.1f}"


def calculate_qty_btc(position_notional: float, entry_price: float) -> float:
    """
    Calculates the BTC quantity for a position given the USDT notional and entry price.

    Rounds down to the nearest BTC_QTY_STEP to avoid exceeding the intended notional.
    Returns 0.0 if the notional is too small for the minimum order size.

    Args:
        position_notional: Total USDT notional exposure (margin × leverage).
        entry_price:       Entry price in USDT per BTC.

    Returns:
        BTC quantity rounded down to BTC_QTY_STEP precision, or 0.0 if below minimum.
    """
    raw_qty = position_notional / entry_price
    qty_stepped = math.floor(raw_qty / BTC_QTY_STEP) * BTC_QTY_STEP
    qty_stepped = round(qty_stepped, 3)

    if qty_stepped < BTC_QTY_STEP:
        return 0.0
    return qty_stepped


def set_leverage(symbol: str = SYMBOL, leverage: int = LEVERAGE) -> None:
    """
    Sets the leverage for both long and short sides of the symbol on Bybit.

    Should be called once at engine startup before any order is placed to ensure
    the account leverage matches the engine configuration. Safe to call repeatedly
    — Bybit returns retCode=0 even when leverage is already at the target value.

    Args:
        symbol:   Trading pair symbol (e.g. "BTCUSDT").
        leverage: Leverage multiplier to set (e.g. 5 for 5×).

    Raises:
        ValueError: If the Bybit API returns a non-zero retCode.
    """
    signed_post(
        "/v5/position/set-leverage",
        body={
            "category":    CATEGORY,
            "symbol":      symbol,
            "buyLeverage": str(leverage),
            "sellLeverage": str(leverage),
        },
    )


def place_entry_order(
    direction: str,
    qty_btc: float,
    tp_price: float,
    sl_price: float,
    timeout: int = 10,
) -> str:
    """
    Places a market entry order with native TP and SL bracket on Bybit.

    The takeProfit and stopLoss fields are attached directly to the market order,
    so Bybit manages legs 1 and 2 of the exit structure server-side. The engine
    only needs to handle leg 3 (hold-window expiry) locally via the orchestrator.

    Args:
        direction: "long" or "short".
        qty_btc:   Position size in BTC (from calculate_qty_btc()).
        tp_price:  Take-profit price in USDT (absolute level, not percentage).
        sl_price:  Stop-loss price in USDT (absolute level, not percentage).
        timeout:   HTTP request timeout in seconds.

    Returns:
        Bybit order ID string for the placed market order.

    Raises:
        ValueError: If qty_btc is zero (notional too small) or on Bybit API error.
        requests.HTTPError: On HTTP-level failure.
    """
    if qty_btc <= 0.0:
        raise ValueError(
            f"Computed qty_btc={qty_btc} is below minimum order size ({BTC_QTY_STEP} BTC). "
            "Increase capital or reduce leverage."
        )

    response = signed_post(
        "/v5/order/create",
        body={
            "category":    CATEGORY,
            "symbol":      SYMBOL,
            "side":        _direction_to_side(direction),
            "orderType":   "Market",
            "qty":         f"{qty_btc:.3f}",
            "takeProfit":  _round_price(tp_price),
            "stopLoss":    _round_price(sl_price),
            "tpTriggerBy": "LastPrice",
            "slTriggerBy": "LastPrice",
            "tpslMode":    "Full",
            "positionIdx": _POSITION_IDX,
        },
        timeout=timeout,
    )
    return response["result"]["orderId"]


def close_position_market(
    direction: str,
    qty_btc: float,
    timeout: int = 10,
) -> str:
    """
    Closes an open position with a market order (hold-window expiry exit).

    Uses reduceOnly=True to guarantee this order can only reduce an existing
    position and cannot accidentally open a new one. Called by the orchestrator
    after cancel_all_active_orders() to ensure no conflicting TP/SL orders remain.

    Args:
        direction: "long" or "short" of the position being closed (not the order side).
        qty_btc:   Size of the position to close in BTC.
        timeout:   HTTP request timeout in seconds.

    Returns:
        Bybit order ID string for the placed market close order.

    Raises:
        ValueError: On Bybit API error.
        requests.HTTPError: On HTTP-level failure.
    """
    response = signed_post(
        "/v5/order/create",
        body={
            "category":    CATEGORY,
            "symbol":      SYMBOL,
            "side":        _closing_side(direction),
            "orderType":   "Market",
            "qty":         f"{qty_btc:.3f}",
            "reduceOnly":  True,
            "positionIdx": _POSITION_IDX,
        },
        timeout=timeout,
    )
    return response["result"]["orderId"]


def cancel_all_active_orders(symbol: str = SYMBOL, timeout: int = 10) -> None:
    """
    Cancels all active orders for the symbol (including any remaining TP/SL conditionals).

    Called before a market close to prevent residual TP/SL orders from re-opening
    a position after the hold-window exit has been executed.

    Args:
        symbol:  Trading pair symbol.
        timeout: HTTP request timeout in seconds.

    Raises:
        ValueError: On Bybit API error.
        requests.HTTPError: On HTTP-level failure.
    """
    signed_post(
        "/v5/order/cancel-all",
        body={
            "category": CATEGORY,
            "symbol":   symbol,
        },
        timeout=timeout,
    )


def get_order_status(order_id: str, timeout: int = 10) -> Dict:
    """
    Queries the current status of an order by its ID.

    Checks active orders first; falls back to order history if not found among
    active orders (covers filled, cancelled, or partially-filled states).

    Args:
        order_id: Bybit order ID string returned by place_entry_order().
        timeout:  HTTP request timeout in seconds.

    Returns:
        Order detail dict from Bybit with keys including orderId, orderStatus,
        avgPrice, cumExecQty, and cumExecValue.

    Raises:
        ValueError: If the order is not found in active orders or history,
                    or on Bybit API error.
        requests.HTTPError: On HTTP-level failure.
    """
    # Check active (open) orders first
    try:
        active_response = signed_get(
            "/v5/order/realtime",
            params={
                "category": CATEGORY,
                "symbol":   SYMBOL,
                "orderId":  order_id,
            },
            timeout=timeout,
        )
        orders = active_response["result"]["list"]
        if orders:
            return orders[0]
    except ValueError:
        pass

    # Fall back to order history for filled or cancelled orders
    history_response = signed_get(
        "/v5/order/history",
        params={
            "category": CATEGORY,
            "symbol":   SYMBOL,
            "orderId":  order_id,
        },
        timeout=timeout,
    )
    orders = history_response["result"]["list"]
    if not orders:
        raise ValueError(f"Order {order_id} not found in active orders or history.")
    return orders[0]
