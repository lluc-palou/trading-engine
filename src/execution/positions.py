"""
Position and account queries — wallet balance and open position inspection for the engine.

get_wallet_balance() is called before sizing each new trade to obtain the current
total USDT equity of the Bybit account, which serves as the capital input to
compute_sizing(). get_open_position() is called by the orchestrator to verify that
a position is still live on Bybit before attempting a time-based close, and to
reconcile local state when a TP or SL has already been triggered server-side.
"""

from typing import Dict, Optional

from config import CATEGORY, SYMBOL

from src.execution.client import signed_get


def get_wallet_balance(timeout: int = 10) -> float:
    """
    Returns the total USDT equity of the Bybit UNIFIED account.

    Total equity includes unrealised P&L from any open positions, making it the
    correct capital figure for sizing the next trade (it reflects the true current
    account value). When no position is open, total equity equals wallet balance.

    Args:
        timeout: HTTP request timeout in seconds.

    Returns:
        Total USDT equity as a float.

    Raises:
        ValueError: On Bybit API error or if the USDT coin is not found in the response.
        requests.HTTPError: On HTTP-level failure.
    """
    response = signed_get(
        "/v5/account/wallet-balance",
        params={"accountType": "UNIFIED", "coin": "USDT"},
        timeout=timeout,
    )

    account_list = response["result"]["list"]
    if not account_list:
        raise ValueError("Bybit wallet-balance response returned an empty account list.")

    # totalEquity is the aggregate USDT value of the full UNIFIED account
    total_equity_str = account_list[0].get("totalEquity", "")
    if not total_equity_str:
        raise ValueError("totalEquity field missing or empty in wallet-balance response.")

    return float(total_equity_str)


def get_open_position(symbol: str = SYMBOL, timeout: int = 10) -> Optional[Dict]:
    """
    Returns the current open position for the symbol, or None if no position exists.

    Queries the Bybit linear perpetual position list for the given symbol and returns
    the position detail dict if a non-zero size is found. A position with size "0"
    means no position is open (Bybit returns a zero-size entry rather than an empty list).

    Args:
        symbol:  Trading pair symbol (e.g. "BTCUSDT").
        timeout: HTTP request timeout in seconds.

    Returns:
        Position detail dict with keys including side ("Buy"/"Sell"/"None"),
        size (BTC quantity as string), avgPrice, unrealisedPnl, and markPrice.
        Returns None if no position is open.

    Raises:
        ValueError: On Bybit API error.
        requests.HTTPError: On HTTP-level failure.
    """
    response = signed_get(
        "/v5/position/list",
        params={"category": CATEGORY, "symbol": symbol},
        timeout=timeout,
    )

    position_list = response["result"]["list"]
    for position in position_list:
        # Bybit returns side="" and size="0" when no position is open
        if float(position.get("size", "0")) > 0:
            return position

    return None


def is_position_open(symbol: str = SYMBOL, timeout: int = 10) -> bool:
    """
    Returns True if there is an active open position for the symbol on Bybit.

    Convenience wrapper around get_open_position() for boolean checks in the
    orchestrator's pre-trade guard logic.

    Args:
        symbol:  Trading pair symbol.
        timeout: HTTP request timeout in seconds.

    Returns:
        True if a non-zero position exists, False otherwise.

    Raises:
        ValueError: On Bybit API error.
        requests.HTTPError: On HTTP-level failure.
    """
    return get_open_position(symbol=symbol, timeout=timeout) is not None
