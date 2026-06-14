"""
Data module — live and historical Bybit V5 kline fetch for BTCUSDT linear perpetual.

fetch_candles()       — last ~1000 closed 1H candles (live signal detection)
fetch_full_history()  — full history from Bybit listing with local parquet cache;
                        first call fetches ~40k candles, subsequent calls only pull
                        new candles since the last cached timestamp.
"""

import pathlib
from typing import List

import pandas as pd
import requests

from config import (
    BYBIT_BASE_URL,
    BYBIT_ORIGIN_MS,
    CACHE_FILE,
    CATEGORY,
    INTERVAL,
    INTERVAL_MS,
    SYMBOL,
)

BYBIT_KLINES_URL: str = f"{BYBIT_BASE_URL}/v5/market/kline"

# Bybit returns at most 1000 candles per request; request one extra to identify
# the forming (unclosed) candle and drop it before returning to callers.
FETCH_LIMIT: int = 1001

# Bybit kline column order (index into each element of result["list"])
_BYBIT_COL_START_TIME: int = 0
_BYBIT_COL_OPEN: int = 1
_BYBIT_COL_HIGH: int = 2
_BYBIT_COL_LOW: int = 3
_BYBIT_COL_CLOSE: int = 4
_BYBIT_COL_VOLUME: int = 5

_OHLCV_COLUMNS: List[str] = ["open", "high", "low", "close", "volume"]


# ── Internal fetch primitive ─────────────────────────────────────────────────

def _parse_kline_response(raw_list: List[List[str]]) -> pd.DataFrame:
    """
    Parses the raw Bybit kline list into a deduplicated, sorted OHLCV DataFrame.

    Bybit returns kline rows in descending order (newest first). This function
    reverses them to chronological order, converts timestamps to UTC-aware
    DatetimeIndex, and casts all OHLCV values to float64.

    Args:
        raw_list: List of kline rows as returned by the Bybit V5 /market/kline
                  endpoint. Each row is [startTime, open, high, low, close, volume, turnover].

    Returns:
        DataFrame with columns [open, high, low, close, volume] and a UTC-aware
        DatetimeIndex named open_time. Empty DataFrame with correct columns if
        raw_list is empty.
    """
    if not raw_list:
        return pd.DataFrame(columns=_OHLCV_COLUMNS)

    # Bybit returns rows newest-first; reverse to chronological order
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


def _fetch_pages(start_ms: int, timeout: int = 10) -> pd.DataFrame:
    """
    Fetches all closed 1H BTCUSDT candles from `start_ms` up to (but not
    including) the currently forming candle, using paginated 1000-candle requests.

    Bybit kline pagination is driven by the `start` timestamp parameter.
    Each page returns up to 1000 rows from `start` forward; we advance `start`
    by one interval after the last candle of each batch until we reach the
    present hour.

    Args:
        start_ms: Start timestamp in milliseconds (inclusive). Candles with
                  open_time >= start_ms will be fetched.
        timeout:  HTTP request timeout in seconds.

    Returns:
        Deduplicated, sorted OHLCV DataFrame with a UTC-aware DatetimeIndex.
        The forming (currently open) candle is excluded. Returns an empty
        DataFrame with correct columns if no data is available.

    Raises:
        requests.HTTPError: On any non-2xx Bybit API response.
        ValueError: If the Bybit API returns a non-zero retCode.
    """
    now_ms = int(pd.Timestamp.now(tz="UTC").timestamp() * 1000)
    all_frames: List[pd.DataFrame] = []
    pos = start_ms

    while pos < now_ms:
        response = requests.get(
            BYBIT_KLINES_URL,
            params={
                "category": CATEGORY,
                "symbol":   SYMBOL,
                "interval": INTERVAL,
                "start":    pos,
                "limit":    1000,
            },
            timeout=timeout,
        )
        response.raise_for_status()

        payload = response.json()
        if payload.get("retCode", -1) != 0:
            raise ValueError(
                f"Bybit API error {payload.get('retCode')}: {payload.get('retMsg')}"
            )

        raw_list = payload["result"]["list"]
        if not raw_list:
            break

        batch = _parse_kline_response(raw_list)
        if batch.empty:
            break

        all_frames.append(batch)

        last_open_ms = int(batch.index[-1].timestamp() * 1000)

        # Stop when the last candle in this batch is the forming candle
        if last_open_ms >= now_ms - INTERVAL_MS:
            break

        pos = last_open_ms + INTERVAL_MS

    if not all_frames:
        return pd.DataFrame(columns=_OHLCV_COLUMNS)

    df = pd.concat(all_frames)
    df = df[~df.index.duplicated(keep="last")].sort_index()

    # Drop the forming (currently open) candle if present
    forming_open_ms = (now_ms // INTERVAL_MS) * INTERVAL_MS
    forming_time = pd.Timestamp(forming_open_ms, unit="ms", tz="UTC")
    df = df[df.index < forming_time]

    return df


# ── Public interface ─────────────────────────────────────────────────────────

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
    response = requests.get(
        BYBIT_KLINES_URL,
        params={
            "category": CATEGORY,
            "symbol":   SYMBOL,
            "interval": INTERVAL,
            "limit":    limit,
        },
        timeout=timeout,
    )
    response.raise_for_status()

    payload = response.json()
    if payload.get("retCode", -1) != 0:
        raise ValueError(
            f"Bybit API error {payload.get('retCode')}: {payload.get('retMsg')}"
        )

    df = _parse_kline_response(payload["result"]["list"])

    # Drop the most recent row — it is the forming (unclosed) candle
    df = df.iloc[:-1]

    return df


def fetch_full_history(timeout: int = 10) -> pd.DataFrame:
    """
    Returns the complete 1H BTCUSDT candle history from the Bybit linear perpetual
    listing date (2019-10-01) to the last closed candle, using a local parquet cache.

    First call:       fetches the full history across multiple paginated requests
                      (~40k candles) and writes the result to the parquet cache.
    Subsequent calls: reads the cache, fetches only candles since the last cached
                      timestamp, appends them, and overwrites the cache.

    Args:
        timeout: HTTP request timeout in seconds per paginated request.

    Returns:
        DataFrame with columns [open, high, low, close, volume] and a UTC-aware
        DatetimeIndex named open_time. All candles are closed (forming candle excluded).

    Raises:
        requests.HTTPError: On any non-2xx Bybit API response.
        ValueError: If the Bybit API returns a non-zero retCode.
    """
    cache_path = pathlib.Path(str(CACHE_FILE))

    if cache_path.exists():
        cached = pd.read_parquet(cache_path)
        # Fetch from one interval after the last cached closed candle
        fetch_start_ms = int(cached.index[-1].timestamp() * 1000) + INTERVAL_MS
    else:
        cached = pd.DataFrame(columns=_OHLCV_COLUMNS)
        fetch_start_ms = BYBIT_ORIGIN_MS

    df_new = _fetch_pages(fetch_start_ms, timeout=timeout)

    if df_new.empty:
        return cached

    if not cached.empty:
        df = pd.concat([cached, df_new])
        df = df[~df.index.duplicated(keep="last")].sort_index()
    else:
        df = df_new

    # Persist updated history to cache
    cache_path.parent.mkdir(parents=True, exist_ok=True)
    df.to_parquet(cache_path)

    return df
