"""
Position sizing — fixed capital fraction applied to each trade, scaled by leverage.

Trade exposure per signal:
    margin_usdt       = capital × CAPITAL_FRACTION      (cash posted as margin)
    position_notional = margin_usdt × leverage          (total BTC exposure)

CAPITAL_FRACTION and leverage are both set in config.py and kept orthogonal:
CAPITAL_FRACTION controls how much of the wallet is committed per trade;
leverage controls the notional multiplier on that cash.

At CAPITAL_FRACTION=1.0 and leverage=5 the effective position is 5× capital,
placing P(ruin −20%) at approximately 10% based on the empirical signal
distribution (577 signals, 2017–2026, crossover-only entry mode).

Per-tier TP/SL percentages and hold windows come from the backtested
signal analysis and are independent of the sizing decision.
"""

from typing import Dict, Tuple

# ── Per-tier exit parameters: (tp_pct, sl_pct) ───────────────────────────────
# Percentages are price moves from entry; derived from backtested optimal exits.
_PARAMS: Dict[Tuple[str, int], Tuple[float, float]] = {
    ("short", 2): (0.015, 0.010),  # TP 1.5%, SL 1.0%
    ("short", 3): (0.012, 0.010),  # TP 1.2%, SL 1.0%
    ("long",  2): (0.015, 0.012),  # TP 1.5%, SL 1.2%
    ("long",  3): (0.010, 0.010),  # TP 1.0%, SL 1.0%
}

# ── Optimal holding periods in hours by (direction, tier) ────────────────────
OPTIMAL_HOLD_HOURS: Dict[Tuple[str, int], int] = {
    ("short", 2): 8,   # peak avg ret @ 8h  (+1.02%), win rate peak @ 6h (79.5%)
    ("short", 3): 24,  # both win rate (72.0%) and avg ret (+1.22%) peak at 24h
    ("long",  2): 12,  # win rate 77.3% @ 12h; avg ret continues to 24h — compromise
    ("long",  3): 18,  # win rate peaks at 18h (69.7%); avg ret peaks at 24h (+1.01%)
}


def get_exit_params(direction: str, tier: int) -> Tuple[float, float]:
    """
    Returns the (tp_pct, sl_pct) tuple for the given signal direction and tier.

    Args:
        direction: "long" or "short".
        tier:      2 or 3.

    Returns:
        Tuple of (take_profit_pct, stop_loss_pct) as decimal fractions (e.g. 0.015).
    """
    return _PARAMS[(direction, tier)]


def get_hold_hours(direction: str, tier: int) -> int:
    """
    Returns the optimal holding period in hours for the given signal type.

    Args:
        direction: "long" or "short".
        tier:      2 or 3.

    Returns:
        Integer number of hours to hold the position as a time-based backstop.
    """
    return OPTIMAL_HOLD_HOURS[(direction, tier)]


def compute_sizing(
    capital: float,
    direction: str,
    tier: int,
    entry_price: float,
    leverage: int,
    capital_fraction: float,
) -> Dict:
    """
    Computes all sizing and exit-price fields for a confirmed leveraged signal.

    Margin posted     = capital × capital_fraction.
    Position notional = margin × leverage.
    TP and SL prices are computed from entry_price and tier-specific percentages;
    they describe price movement and are independent of leverage.

    Args:
        capital:          Total USDT wallet balance available for trading.
        direction:        "long" or "short".
        tier:             2 or 3.
        entry_price:      Close price of the entry confirmation candle.
        leverage:         Leverage multiplier applied to the margin (e.g. 5 for 5×).
        capital_fraction: Fraction of capital committed as margin (e.g. 1.0 = 100%).

    Returns:
        Dict with keys:
            direction         trade direction (echoed)
            tier              signal tier (echoed)
            entry_price       entry price (echoed)
            tp_pct            take-profit percentage as decimal
            sl_pct            stop-loss percentage as decimal
            tp_price          absolute take-profit price level
            sl_price          absolute stop-loss price level
            hold_hours        optimal holding period in hours
            capital_fraction  fraction of capital posted as margin (echoed)
            margin_usdt       USDT posted as margin
            position_notional total USD notional exposure (margin × leverage)
            risk_amount_usdt  USDT at risk if stop-loss is hit
            leverage          leverage multiplier (echoed)
    """
    tp_pct, sl_pct = get_exit_params(direction, tier)
    hold_hours = get_hold_hours(direction, tier)

    margin_usdt = capital * capital_fraction
    position_notional = margin_usdt * leverage
    risk_amount_usdt = position_notional * sl_pct

    if direction == "long":
        tp_price = entry_price * (1.0 + tp_pct)
        sl_price = entry_price * (1.0 - sl_pct)
    else:
        tp_price = entry_price * (1.0 - tp_pct)
        sl_price = entry_price * (1.0 + sl_pct)

    return {
        "direction":         direction,
        "tier":              tier,
        "entry_price":       entry_price,
        "tp_pct":            tp_pct,
        "sl_pct":            sl_pct,
        "tp_price":          tp_price,
        "sl_price":          sl_price,
        "hold_hours":        hold_hours,
        "capital_fraction":  capital_fraction,
        "margin_usdt":       margin_usdt,
        "position_notional": position_notional,
        "risk_amount_usdt":  risk_amount_usdt,
        "leverage":          leverage,
    }
