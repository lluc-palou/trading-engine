"""
Indicator computation — Wave Trend Oscillator and Money Flow for the
momentum-exhaustion-reversal strategy.

Accepts a plain OHLCV DataFrame (columns: open, high, low, close, volume) and
returns indicator Series with the same index, ready for downstream signal detection.
Ported from the original monitoring dashboard with no changes to computation logic.
"""

import pandas as pd

# ── Wave Trend Oscillator parameters ────────────────────────────────────────
WT_CHANNEL_LENGTH: int = 9
WT_AVERAGE_LENGTH: int = 12
WT_MA_LENGTH: int = 3
WT_CONSTANT: float = 0.015

# ── Money Flow EMA smoothing ─────────────────────────────────────────────────
MFI_SMOOTHING_LENGTH: int = 3


def compute_wave_trend(df: pd.DataFrame) -> tuple[pd.Series, pd.Series]:
    """
    Computes the Wave Trend Oscillator (WT1, WT2) from an OHLCV DataFrame.

    WT1 normalises HLC3 deviation from its EMA by the EMA of mean absolute deviation,
    then applies a second EMA pass. WT2 is a 3-period SMA of WT1, acting as the
    signal line whose crossover with WT1 marks a momentum direction change.

    Args:
        df: DataFrame with columns [open, high, low, close, volume].

    Returns:
        Tuple of (wt1, wt2) Series indexed identically to df.
    """
    hlc3 = (df["high"] + df["low"] + df["close"]) / 3.0
    ema_hlc3 = hlc3.ewm(span=WT_CHANNEL_LENGTH, adjust=False).mean()
    mad = (hlc3 - ema_hlc3).abs().ewm(span=WT_AVERAGE_LENGTH, adjust=False).mean()
    channel_index = (hlc3 - ema_hlc3) / (WT_CONSTANT * mad)
    wt1 = channel_index.ewm(span=WT_CHANNEL_LENGTH, adjust=False).mean()
    wt2 = wt1.rolling(window=WT_MA_LENGTH).mean()
    return wt1, wt2


def compute_money_flow(df: pd.DataFrame) -> pd.Series:
    """
    Computes the signed Money Flow indicator from an OHLCV DataFrame.

    The signed body ratio (close - open) / (high - low) captures directional
    conviction per candle. Multiplied by volume and EMA-smoothed to reduce noise.

    Args:
        df: DataFrame with columns [open, high, low, close, volume].

    Returns:
        EMA-smoothed Money Flow Series indexed identically to df.
        Values are NaN where candle range (high - low) is zero.
    """
    candle_range = df["high"] - df["low"]
    signed_body = (df["close"] - df["open"]) / candle_range
    raw_mfi = signed_body * df["volume"]
    return raw_mfi.ewm(span=MFI_SMOOTHING_LENGTH, adjust=False).mean()


def compute_all(df: pd.DataFrame) -> pd.DataFrame:
    """
    Computes all indicators and attaches them as new columns on a copy of df.

    Args:
        df: OHLCV DataFrame with columns [open, high, low, close, volume].

    Returns:
        Copy of df with added columns: wt1, wt2, mfi.
    """
    out = df.copy()
    out["wt1"], out["wt2"] = compute_wave_trend(df)
    out["mfi"] = compute_money_flow(df)
    return out
