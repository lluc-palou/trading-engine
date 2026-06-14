"""
Hourly trading orchestrator — the main execution loop of the trading engine.

run_forever() blocks indefinitely, waking at H:03 UTC each hour (3 minutes after
each 1H candle close) to run one detection and execution cycle via run_once().

Each cycle follows this sequence:

    1. Fetch latest 1H candles from Bybit and compute indicators.
    2. If an active position is recorded in local state:
           a. Verify the position is still open on Bybit.
              - If NOT open: TP or SL was already triggered server-side.
                Clear local state and log the outcome.
           b. If OPEN and the hold-window deadline has passed:
                Cancel any residual TP/SL orders, execute a market close,
                clear state, and notify.
           c. If OPEN and deadline has not passed: log remaining time and exit.
    3. If no active position, run signal detection on the last closed candle.
    4. If a signal is active (entry bar == last bar):
           a. Fetch live wallet balance as the capital input for sizing.
           b. Compute sizing (Kelly fraction, notional, TP, SL, hold hours).
           c. Run all pre-trade risk guard checks.
           d. Place the market entry order with TP/SL bracket on Bybit.
           e. Persist the active position to state (including exit deadline).
           f. Notify and log.
"""

import logging
import sys
import time
from datetime import datetime, timedelta, timezone
from typing import Optional

from config import CAPITAL_FRACTION, LEVERAGE, SYMBOL, WAKEUP_OFFSET_MINUTES

from src.data.bybit import fetch_candles
from src.execution.orders import (
    calculate_qty_btc,
    cancel_all_active_orders,
    close_position_market,
    place_entry_order,
    set_leverage,
)
from src.execution.positions import get_open_position, get_wallet_balance
from src.execution.state import (
    clear_active_position,
    is_deadline_passed,
    load_active_position,
    save_active_position,
)
from src.notifications.base import BaseNotifier
from src.risk.guard import run_pre_trade_checks
from src.strategy.detector import detect
from src.strategy.indicators import compute_all
from src.strategy.sizing import compute_sizing

logger = logging.getLogger(__name__)


def setup_logging(log_file_path: Optional[str] = None) -> None:
    """
    Configures the root logger with a UTC-timestamped formatter.

    Attaches a StreamHandler (stdout) always, and a FileHandler when log_file_path
    is provided. Log level is INFO for both handlers.

    Args:
        log_file_path: Optional absolute path to the log file. When None, only
                       stdout logging is active.
    """
    log_format = "%(asctime)s UTC | %(levelname)-8s | %(message)s"
    formatter = logging.Formatter(fmt=log_format, datefmt="%Y-%m-%d %H:%M:%S")
    formatter.converter = time.gmtime  # force UTC timestamps in log lines

    root_logger = logging.getLogger()
    root_logger.setLevel(logging.INFO)

    # Stdout handler
    stream_handler = logging.StreamHandler(sys.stdout)
    stream_handler.setFormatter(formatter)
    root_logger.addHandler(stream_handler)

    # File handler
    if log_file_path:
        file_handler = logging.FileHandler(log_file_path, encoding="utf-8")
        file_handler.setFormatter(formatter)
        root_logger.addHandler(file_handler)


def _compute_next_wakeup() -> datetime:
    """
    Returns the next H:WAKEUP_OFFSET_MINUTES:00 UTC timestamp after now.

    If now is already past the current hour's wakeup point (e.g. it is H:05 and
    offset is 3), returns the next hour's wakeup time instead.

    Returns:
        UTC-aware datetime of the next scheduled wakeup.
    """
    now = datetime.now(tz=timezone.utc)
    candidate = now.replace(minute=WAKEUP_OFFSET_MINUTES, second=0, microsecond=0)
    if now >= candidate:
        candidate += timedelta(hours=1)
    return candidate


def _handle_active_position(
    active_position: dict,
    notifier: BaseNotifier,
    dry_run: bool,
) -> None:
    """
    Manages an already-open position: reconciles with Bybit and handles exits.

    Checks whether the position is still live on Bybit (TP/SL may have fired),
    and whether the hold-window deadline has passed. Executes a market close if
    the deadline has expired.

    Args:
        active_position: Position state dict from load_active_position().
        notifier:        Notification backend for trade events.
        dry_run:         When True, logs actions without executing any orders.
    """
    direction = active_position["direction"]
    tier = active_position["tier"]
    qty_btc = active_position["qty_btc"]
    entry_price = active_position["entry_price"]
    deadline_str = active_position["exit_deadline_utc"]

    # Reconcile with Bybit — position may have been closed by TP or SL
    bybit_position = get_open_position(symbol=SYMBOL)

    if bybit_position is None:
        logger.info(
            f"[RECONCILE] Position {direction.upper()} T{tier} @ {entry_price:.2f} "
            f"no longer open on Bybit — TP or SL was triggered server-side."
        )
        notifier.send(
            "TP_OR_SL_HIT",
            f"{direction.upper()} T{tier} position closed by TP or SL (entry @ {entry_price:.2f}).",
        )
        if not dry_run:
            clear_active_position()
        return

    # Position is still open — check whether the hold-window deadline has passed
    if is_deadline_passed(active_position):
        logger.info(
            f"[DEADLINE] Hold window expired for {direction.upper()} T{tier} "
            f"(deadline {deadline_str}). Executing time-based market close."
        )
        if not dry_run:
            cancel_all_active_orders(symbol=SYMBOL)
            close_order_id = close_position_market(direction=direction, qty_btc=qty_btc)
            clear_active_position()
            logger.info(f"[TIME_EXIT] Close order placed. order_id={close_order_id}")
        else:
            logger.info("[DRY_RUN] Would cancel orders and close position at market.")

        notifier.send(
            "TIME_EXIT",
            f"{direction.upper()} T{tier} hold window expired — position closed at market.",
        )
        return

    # Position open and deadline not yet reached — nothing to do this cycle
    remaining = datetime.fromisoformat(deadline_str) - datetime.now(tz=timezone.utc)
    remaining_hours = remaining.total_seconds() / 3600.0
    logger.info(
        f"[HOLDING] {direction.upper()} T{tier} @ {entry_price:.2f} | "
        f"deadline in {remaining_hours:.1f}h ({deadline_str})"
    )


def run_once(notifier: BaseNotifier, dry_run: bool = False) -> None:
    """
    Executes one full detection and execution cycle.

    Fetches candles, computes indicators, checks active position state,
    and opens a new trade if a signal is present and all risk gates pass.

    Args:
        notifier: Notification backend for trade lifecycle events.
        dry_run:  When True, runs detection and sizing but places no orders
                  and writes no state. Useful for live-testing without capital risk.
    """
    cycle_start = datetime.now(tz=timezone.utc)
    logger.info(f"[CYCLE_START] {cycle_start.strftime('%Y-%m-%d %H:%M:%S')} UTC")

    # Step 1: Fetch latest candles and compute indicators
    df_raw = fetch_candles()
    df = compute_all(df_raw)
    last_candle_time = df.index[-1]
    logger.info(f"[DATA] {len(df)} candles loaded. Last closed: {last_candle_time}")

    # Step 2: Active position management (check before looking for new signals)
    active_position = load_active_position()
    if active_position is not None:
        _handle_active_position(active_position, notifier, dry_run)
        return

    # Step 2b: Explicit Bybit-side guard — enforce MAX_ACTIVE_POSITIONS regardless
    # of local state. Catches stale state (e.g. deleted positions.json) and prevents
    # a second position from being opened when one is already live on the exchange.
    if get_open_position(symbol=SYMBOL) is not None:
        logger.warning(
            "[POSITION_GUARD] Open position detected on Bybit but no local state found. "
            "Skipping new signal until the exchange position is cleared or state is reconciled."
        )
        notifier.send(
            "GUARD_BLOCK",
            "Open position on Bybit without matching local state — new entry blocked. "
            "Manual reconciliation may be required.",
        )
        return

    # Step 3: No active position — run signal detection on the last closed candle
    detection_result = detect(df)

    if detection_result["status"] != "active":
        logger.info(
            f"[NO_SIGNAL] No entry signal on last candle. "
            f"WT1={detection_result['wt1_current']:.2f}  "
            f"WT2={detection_result['wt2_current']:.2f}  "
            f"MFI={detection_result['mfi_current']:.4f}"
        )
        return

    # Signal found on the last bar
    direction = detection_result["direction"]
    tier = detection_result["tier"]
    entry_price = detection_result["entry_price"]
    entry_time = detection_result["entry_time"]

    logger.info(
        f"[SIGNAL] {direction.upper()} T{tier} detected on candle {entry_time} "
        f"| entry_price={entry_price:.2f}"
    )
    notifier.send(
        "SIGNAL",
        f"Signal: {direction.upper()} Tier {tier} @ {entry_price:.2f} "
        f"(candle {entry_time.strftime('%Y-%m-%d %H:%M')} UTC)",
    )

    # Step 4: Fetch live capital and compute sizing
    capital = get_wallet_balance()
    logger.info(f"[CAPITAL] Wallet balance: {capital:.2f} USDT")

    sizing = compute_sizing(
        capital=capital,
        direction=direction,
        tier=tier,
        entry_price=entry_price,
        leverage=LEVERAGE,
        capital_fraction=CAPITAL_FRACTION,
    )
    qty_btc = calculate_qty_btc(
        position_notional=sizing["position_notional"],
        entry_price=entry_price,
    )

    logger.info(
        f"[SIZING] deployed={sizing['capital_fraction']*100:.0f}% of capital  "
        f"margin={sizing['margin_usdt']:.2f} USDT  "
        f"notional={sizing['position_notional']:.2f} USDT  "
        f"qty={qty_btc:.3f} BTC  "
        f"TP={sizing['tp_price']:.2f}  SL={sizing['sl_price']:.2f}  "
        f"hold={sizing['hold_hours']}h"
    )

    # Step 5: Run all pre-trade risk guard checks
    guard_result = run_pre_trade_checks(
        capital=capital,
        qty_btc=qty_btc,
        sizing=sizing,
        leverage=LEVERAGE,
    )

    if not guard_result.ok:
        logger.warning(f"[GUARD_BLOCK] Trade blocked — {guard_result.reason}")
        notifier.send("GUARD_BLOCK", f"Trade blocked: {guard_result.reason}")
        return

    if dry_run:
        logger.info(
            f"[DRY_RUN] Would place {direction.upper()} T{tier} market order: "
            f"qty={qty_btc:.3f} BTC  TP={sizing['tp_price']:.2f}  SL={sizing['sl_price']:.2f}"
        )
        return

    # Step 6: Set leverage and place the entry order
    set_leverage(symbol=SYMBOL, leverage=LEVERAGE)

    order_id = place_entry_order(
        direction=direction,
        qty_btc=qty_btc,
        tp_price=sizing["tp_price"],
        sl_price=sizing["sl_price"],
    )
    logger.info(f"[ENTRY] Market order placed. order_id={order_id}")

    # Step 7: Persist active position state with exit deadline
    save_active_position(
        direction=direction,
        tier=tier,
        entry_time_utc=entry_time.to_pydatetime(),
        hold_hours=sizing["hold_hours"],
        entry_price=entry_price,
        tp_price=sizing["tp_price"],
        sl_price=sizing["sl_price"],
        bybit_order_id=order_id,
        qty_btc=qty_btc,
        position_notional=sizing["position_notional"],
        margin_usdt=sizing["margin_usdt"],
        risk_amount_usdt=sizing["risk_amount_usdt"],
    )

    logger.info(
        f"[STATE_SAVED] Active position recorded. "
        f"Exit deadline: {entry_time.to_pydatetime() + timedelta(hours=sizing['hold_hours'])}"
    )

    notifier.send(
        "ENTRY",
        f"Trade opened: {direction.upper()} T{tier} | "
        f"qty={qty_btc:.3f} BTC @ ~{entry_price:.2f} | "
        f"TP={sizing['tp_price']:.2f}  SL={sizing['sl_price']:.2f}  "
        f"hold={sizing['hold_hours']}h",
    )


def run_forever(notifier: BaseNotifier, dry_run: bool = False) -> None:
    """
    Blocks indefinitely, running one detection cycle at H:03 UTC each hour.

    Errors inside run_once() are caught and logged without stopping the loop
    so a transient API failure or network blip does not bring the engine down.
    The loop always sleeps until the next scheduled wakeup, never hammers the
    exchange.

    Args:
        notifier: Notification backend forwarded to each run_once() call.
        dry_run:  When True, forwarded to run_once() — no orders are placed.
    """
    logger.info(
        f"[ENGINE_START] Trading engine started. "
        f"symbol={SYMBOL}  leverage={LEVERAGE}x  dry_run={dry_run}"
    )

    while True:
        next_wakeup = _compute_next_wakeup()
        sleep_seconds = (next_wakeup - datetime.now(tz=timezone.utc)).total_seconds()

        logger.info(
            f"[SLEEP] Next cycle at {next_wakeup.strftime('%Y-%m-%d %H:%M:%S')} UTC "
            f"({sleep_seconds / 60:.1f} min)"
        )
        time.sleep(max(sleep_seconds, 0))

        try:
            run_once(notifier=notifier, dry_run=dry_run)
        except Exception as cycle_error:
            logger.error(
                f"[CYCLE_ERROR] Unhandled exception in run_once(): {cycle_error}",
                exc_info=True,
            )
            notifier.send("CYCLE_ERROR", f"Engine cycle error: {cycle_error}")
