"""
Data module — live Bybit V5 kline fetch for BTCUSDT linear perpetual.

fetch_candles() returns the last 1000 closed 1H candles, which is everything
the signal detection pipeline needs. Called once per hourly cycle.
"""

import logging
import time
from typing import List

import pandas as pd
import requests

from config import BYBIT_BASE_URL, CATEGORY, INTERVAL, INTERVAL_MS, SYMBOL

logger = logging.getLogger(__name__)

BYBIT_KLINES_URL: str = f"{BYBIT_BASE_URL}/v5/market/kline"

# Request one extra candle so the forming (unclosed) candle can be identified and dropped.
FETCH_LIMIT: int = 1001

# Bybit retCode for "too many visits" — transient rate-limit hit, safe to retry
_RATE_LIMIT_RET_CODE: int = 10006

# Retry policy for transient failures — this is a read-only GET, always safe to retry
_MAX_ATTEMPTS: int = 3
_RETRY_BACKOFF_SECONDS: tuple = (2, 5)

_BYBIT_COL_START_TIME: int = 0
_BYBIT_COL_OPEN: int = 1
_BYBIT_COL_HIGH: int = 2
_BYBIT_COL_LOW: int = 3
_BYBIT_COL_CLOSE: int = 4
_BYBIT_COL_VOLUME: int = 5

_OHLCV_COLUMNS: List[str] = ["open", "high", "low", "close", "volume"]


def _parse_kline_response(raw_list: List[List[str]]) -> pd.DataFrame:
    """
    Parses the raw Bybit kline list into a deduplicated, sorted OHLCV DataFrame.

    Bybit returns kline rows in descending order (newest first). This function
    reverses them to chronological order, converts timestamps to UTC-aware
    DatetimeIndex, and casts all OHLCV values to float64.

    Args:
        raw_list: List of kline rows from the Bybit V5 /market/kline endpoint.
                  Each row is [startTime, open, high, low, close, volume, turnover].

    Returns:
        DataFrame with columns [open, high, low, close, volume] and a UTC-aware
        DatetimeIndex named open_time. Empty DataFrame with correct columns if
        raw_list is empty.
    """
    if not raw_list:
        return pd.DataFrame(columns=_OHLCV_COLUMNS)

    chronological = list(reversed(raw_list))

    open_times = pd.to_datetime(
        [int(row[_BYBIT_COL_START_TIME]) for row in chronological],
        unit="ms",
        utc=True,
    )
    df = pd.DataFrame(
        {
            "open":   [float(row[_BYBIT_COL_OPEN])   for row in chronological],
            "high":   [float(row[_BYBIT_COL_HIGH])   for row in chronological],
            "low":    [float(row[_BYBIT_COL_LOW])    for row in chronological],
            "close":  [float(row[_BYBIT_COL_CLOSE])  for row in chronological],
            "volume": [float(row[_BYBIT_COL_VOLUME]) for row in chronological],
        },
        index=open_times,
    )
    df.index.name = "open_time"
    df = df[~df.index.duplicated(keep="last")].sort_index()
    return df


def fetch_candles(limit: int = FETCH_LIMIT, timeout: int = 10) -> pd.DataFrame:
    """
    Fetches the most recent closed 1H BTCUSDT candles from the Bybit V5 REST API.

    Requests `limit` candles from the linear perpetual endpoint, then drops the
    last row (the currently forming candle whose close time is in the future).
    All OHLCV columns are cast to float64. The returned index is a UTC-aware
    DatetimeIndex of candle open times.

    Args:
        limit:   Total candles to request before dropping the forming candle.
                 Callers receive limit-1 closed candles (1000 by default).
        timeout: HTTP request timeout in seconds.

    Returns:
        DataFrame with columns [open, high, low, close, volume] and a UTC-aware
        DatetimeIndex named open_time. Length is limit-1.

    Raises:
        requests.HTTPError: If the Bybit API returns a non-2xx status.
        requests.ConnectionError: On network failure.
        ValueError: If the Bybit API returns a non-zero retCode.
    """
    params = {
        "category": CATEGORY,
        "symbol":   SYMBOL,
        "interval": INTERVAL,
        "limit":    limit,
    }

    last_error: Exception = RuntimeError("unreachable")
    payload = None

    for attempt in range(1, _MAX_ATTEMPTS + 1):
        try:
            response = requests.get(BYBIT_KLINES_URL, params=params, timeout=timeout)
            response.raise_for_status()
            candidate = response.json()

            ret_code = candidate.get("retCode", -1)
            if ret_code == _RATE_LIMIT_RET_CODE and attempt < _MAX_ATTEMPTS:
                wait_seconds = _RETRY_BACKOFF_SECONDS[attempt - 1]
                logger.warning(
                    f"[fetch_candles] Bybit rate limit hit (retCode=10006). "
                    f"Retrying in {wait_seconds}s (attempt {attempt}/{_MAX_ATTEMPTS})."
                )
                time.sleep(wait_seconds)
                continue
            if ret_code != 0:
                raise ValueError(f"Bybit API error {ret_code}: {candidate.get('retMsg')}")

            payload = candidate
            break

        except (requests.ConnectionError, requests.Timeout) as network_error:
            last_error = network_error
            if attempt < _MAX_ATTEMPTS:
                wait_seconds = _RETRY_BACKOFF_SECONDS[attempt - 1]
                logger.warning(
                    f"[fetch_candles] Network error: {network_error}. "
                    f"Retrying in {wait_seconds}s (attempt {attempt}/{_MAX_ATTEMPTS})."
                )
                time.sleep(wait_seconds)
                continue
            raise

    if payload is None:
        raise last_error

    df = _parse_kline_response(payload["result"]["list"])

    # Drop the most recent row — it is the forming (unclosed) candle
    df = df.iloc[:-1]

    return df
