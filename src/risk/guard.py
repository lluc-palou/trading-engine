"""
Pre-trade risk guard — validates conditions before any order is placed.

run_pre_trade_checks() is called by the orchestrator after a signal is detected
and before the entry order is submitted. It enforces four sequential gates:

    1. Capital floor     — account equity must exceed MIN_CAPITAL_USDT.
    2. Qty viability     — computed BTC quantity must be >= minimum order size.
    3. Drawdown breaker  — equity must not have fallen more than MAX_DRAWDOWN_PCT
                           from the recorded peak; halts the engine if breached.
    4. Liquidation buffer— SL price must sit at least MIN_LIQUIDATION_BUFFER_PCT
                           above the estimated liquidation price.

Peak equity is tracked in state/capital.json and updated after every successful
pre-trade check. The drawdown breaker fires if a losing streak has eroded the
account past the configured threshold, signalling that strategy conditions may
have changed and manual review is warranted.
"""

import json
from dataclasses import dataclass, field
from datetime import datetime, timezone
from typing import Dict, Optional

from config import (
    CAPITAL_STATE_FILE,
    LEVERAGE,
    MAX_DRAWDOWN_PCT,
    MIN_CAPITAL_USDT,
    MIN_LIQUIDATION_BUFFER_PCT,
)

from src.execution.orders import BTC_QTY_STEP


# ── Guard result ─────────────────────────────────────────────────────────────

@dataclass
class GuardResult:
    """
    Outcome of a pre-trade risk check sequence.

    Attributes:
        ok:     True if all checks passed and the trade may proceed.
        reason: Human-readable explanation when ok is False; empty string otherwise.
    """
    ok: bool
    reason: str = field(default="")


# ── Peak equity state ─────────────────────────────────────────────────────────

def load_peak_equity() -> Optional[float]:
    """
    Reads the recorded peak equity from the capital state file.

    Returns:
        Peak equity in USDT, or None if the file does not exist yet (first run).
    """
    if not CAPITAL_STATE_FILE.exists():
        return None

    with open(CAPITAL_STATE_FILE, "r", encoding="utf-8") as file_handle:
        data = json.load(file_handle)

    return float(data.get("peak_equity_usdt", 0.0)) or None


def update_peak_equity(current_equity: float) -> None:
    """
    Updates the capital state file if current_equity exceeds the stored peak.

    Called after every successful guard check so that new highs are always recorded.
    Creates the file on first call (first run with no prior trading history).

    Args:
        current_equity: Current total USDT equity of the account.
    """
    existing_peak = load_peak_equity() or 0.0
    new_peak = max(existing_peak, current_equity)

    capital_state = {
        "peak_equity_usdt": new_peak,
        "updated_at":       datetime.now(tz=timezone.utc).isoformat(),
    }

    CAPITAL_STATE_FILE.parent.mkdir(parents=True, exist_ok=True)
    with open(CAPITAL_STATE_FILE, "w", encoding="utf-8") as file_handle:
        json.dump(capital_state, file_handle, indent=2)


# ── Individual checks ─────────────────────────────────────────────────────────

def _check_capital_floor(capital: float) -> GuardResult:
    """
    Verifies account equity is above the minimum required to place a trade.

    Args:
        capital: Current USDT account equity.

    Returns:
        GuardResult with ok=False and a reason string if below the floor.
    """
    if capital < MIN_CAPITAL_USDT:
        return GuardResult(
            ok=False,
            reason=(
                f"Account equity {capital:.2f} USDT is below the minimum "
                f"required {MIN_CAPITAL_USDT:.2f} USDT."
            ),
        )
    return GuardResult(ok=True)


def _check_qty_viability(qty_btc: float) -> GuardResult:
    """
    Verifies the computed BTC quantity meets the exchange minimum order size.

    Args:
        qty_btc: BTC quantity from calculate_qty_btc() (may be 0.0 if too small).

    Returns:
        GuardResult with ok=False if qty is below the minimum step size.
    """
    if qty_btc < BTC_QTY_STEP:
        return GuardResult(
            ok=False,
            reason=(
                f"Computed position size {qty_btc:.3f} BTC is below the minimum "
                f"order size {BTC_QTY_STEP:.3f} BTC. Capital or leverage too low."
            ),
        )
    return GuardResult(ok=True)


def _check_drawdown_breaker(capital: float) -> GuardResult:
    """
    Fires the drawdown circuit breaker if equity has fallen below the peak threshold.

    Compares current equity against the stored peak. If the drawdown exceeds
    MAX_DRAWDOWN_PCT, trading is halted to prevent further losses during conditions
    that may indicate strategy edge degradation.

    Args:
        capital: Current USDT account equity.

    Returns:
        GuardResult with ok=False if the drawdown threshold is breached.
    """
    peak = load_peak_equity()
    if peak is None or peak <= 0.0:
        # No prior peak recorded — first trade cycle, skip this check
        return GuardResult(ok=True)

    drawdown_threshold = peak * (1.0 - MAX_DRAWDOWN_PCT)
    if capital < drawdown_threshold:
        drawdown_pct = (peak - capital) / peak * 100.0
        return GuardResult(
            ok=False,
            reason=(
                f"Drawdown circuit breaker triggered: equity {capital:.2f} USDT "
                f"is {drawdown_pct:.1f}% below peak {peak:.2f} USDT "
                f"(threshold {MAX_DRAWDOWN_PCT * 100:.0f}%). Manual review required."
            ),
        )
    return GuardResult(ok=True)


def _check_liquidation_buffer(sizing: Dict, leverage: int) -> GuardResult:
    """
    Verifies the SL price is safely above the estimated liquidation price.

    Liquidation is estimated as entry_price × (1 ∓ 1/leverage) — a simplified
    model valid for isolated margin. With 5× leverage, liquidation is ~20% from
    entry while SL sits at 1–1.2%, giving ~18–19% buffer. The check ensures no
    future leverage change inadvertently removes this safety margin.

    Args:
        sizing:   Dict returned by compute_sizing() containing direction, entry_price,
                  sl_price, tp_price.
        leverage: Leverage multiplier currently configured.

    Returns:
        GuardResult with ok=False if SL is within MIN_LIQUIDATION_BUFFER_PCT of liquidation.
    """
    direction = sizing["direction"]
    entry_price = sizing["entry_price"]
    sl_price = sizing["sl_price"]

    if direction == "long":
        estimated_liquidation = entry_price * (1.0 - 1.0 / leverage)
        # For longs: sl_price > liquidation (sl fires before liquidation)
        buffer = (sl_price - estimated_liquidation) / entry_price
    else:
        estimated_liquidation = entry_price * (1.0 + 1.0 / leverage)
        # For shorts: sl_price < liquidation
        buffer = (estimated_liquidation - sl_price) / entry_price

    if buffer < MIN_LIQUIDATION_BUFFER_PCT:
        return GuardResult(
            ok=False,
            reason=(
                f"SL price {sl_price:.2f} is too close to estimated liquidation "
                f"{estimated_liquidation:.2f} (buffer {buffer * 100:.1f}% < "
                f"minimum {MIN_LIQUIDATION_BUFFER_PCT * 100:.1f}%)."
            ),
        )
    return GuardResult(ok=True)


# ── Public interface ──────────────────────────────────────────────────────────

def run_pre_trade_checks(
    capital: float,
    qty_btc: float,
    sizing: Dict,
    leverage: int = LEVERAGE,
) -> GuardResult:
    """
    Runs all pre-trade risk checks in sequence, returning on first failure.

    If all checks pass, updates the peak equity record so new account highs
    are captured before the trade is placed.

    Args:
        capital:  Current USDT account equity from get_wallet_balance().
        qty_btc:  BTC quantity computed by calculate_qty_btc().
        sizing:   Dict from compute_sizing() — must include direction, entry_price,
                  sl_price, position_notional, margin_usdt, risk_amount_usdt.
        leverage: Leverage multiplier (defaults to config.LEVERAGE).

    Returns:
        GuardResult with ok=True if all checks pass; ok=False with a reason string
        identifying the first failed check.
    """
    # Gate 1: capital floor
    result = _check_capital_floor(capital)
    if not result.ok:
        return result

    # Gate 2: qty viability
    result = _check_qty_viability(qty_btc)
    if not result.ok:
        return result

    # Gate 3: drawdown circuit breaker
    result = _check_drawdown_breaker(capital)
    if not result.ok:
        return result

    # Gate 4: liquidation buffer
    result = _check_liquidation_buffer(sizing, leverage)
    if not result.ok:
        return result

    # All gates passed — record new peak if applicable
    update_peak_equity(capital)

    return GuardResult(ok=True)
