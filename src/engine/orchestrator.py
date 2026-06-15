"""
Hourly trading orchestrator — the main execution loop of the trading engine.

run_forever() blocks indefinitely, waking at H:01 UTC each hour (1 minute after
each 1H candle close) to run one detection and execution cycle via run_once().

Each cycle follows this sequence:

    1. Fetch latest 1H candles from Bybit and compute indicators.
    2. If an active position is recorded in local state:
           a. Verify the position is still open on Bybit (live) or check price
              against TP/SL levels (paper). If closed: query fill, compute P&L,
              clear state, send TRADE_CLOSED.
           b. If OPEN and the hold-window deadline has passed: cancel brackets,
              close at market (live) or use current price (paper), clear state,
              send TRADE_CLOSED.
           c. If OPEN and deadline not passed: log remaining time, exit.
    3. If no active position, run signal detection on the last closed candle.
    4. If a signal is active (entry bar == last bar):
           a. Fetch live wallet balance (live) or use PAPER_CAPITAL_USDT (paper).
           b. Compute sizing (notional, TP, SL, hold hours).
           c. Run all pre-trade risk guard checks (skipped in paper mode).
           d. Write pending state (bybit_order_id="PENDING" or "PAPER") before
              placing the order, so the exit deadline survives a crash.
           e. Place the market entry order with TP/SL bracket (live only).
           f. Patch state with the real order ID (live) or "PAPER_TRADE" (paper).
           g. Send TRADE_OPENED notification.

Notifications are exactly three event types:
    TRADE_OPENED — fired once when the entry order lands on Bybit (or is
                   simulated in paper mode).
    TRADE_CLOSED — fired when the position closes (TP, SL, or time exit);
                   includes fill price, identified leg, and realised P&L.
    ERROR        — fired for guard blocks (with equity/drawdown context),
                   stale state, or unhandled exceptions.

Paper mode (--paper flag):
    Runs the full pipeline — detection, sizing, state management, notifications —
    but places no orders on Bybit. Uses PAPER_CAPITAL_USDT from config as the
    simulated account size. Notifications are prefixed with [PAPER]. TP/SL
    simulation is based on the last closed candle's price. Use this to validate
    the complete notification cycle and wakeup schedule before deploying capital.
"""

import logging
import logging.handlers
import sys
import time
from datetime import datetime, timedelta, timezone
from typing import Dict, Optional

from config import (
    CAPITAL_FRACTION,
    LEVERAGE,
    PAPER_CAPITAL_USDT,
    SYMBOL,
    WAKEUP_OFFSET_MINUTES,
)

from src.data.bybit import fetch_candles
from src.execution.orders import (
    calculate_qty_btc,
    cancel_all_active_orders,
    close_position_market,
    get_order_status,
    place_entry_order,
    set_leverage,
)
from src.execution.positions import (
    get_closing_execution,
    get_open_position,
    get_wallet_balance,
)
from src.execution.state import (
    clear_active_position,
    is_deadline_passed,
    load_active_position,
    patch_order_id,
    save_active_position,
)
from src.notifications.base import BaseNotifier
from src.risk.guard import load_peak_equity, run_pre_trade_checks
from src.strategy.detector import detect
from src.strategy.indicators import compute_all
from src.strategy.sizing import compute_sizing

logger = logging.getLogger(__name__)


# ── Logging setup ─────────────────────────────────────────────────────────────

def setup_logging(log_file_path: Optional[str] = None) -> None:
    """
    Configures the root logger with a UTC-timestamped formatter.

    Attaches a StreamHandler (stdout) always, and a TimedRotatingFileHandler
    when log_file_path is provided. Log files rotate at midnight UTC and are
    kept indefinitely — the log directory is the complete historical record.

    Args:
        log_file_path: Optional absolute path to the log file. When None, only
                       stdout logging is active.
    """
    log_format = "%(asctime)s UTC | %(levelname)-8s | %(message)s"
    formatter = logging.Formatter(fmt=log_format, datefmt="%Y-%m-%d %H:%M:%S")
    formatter.converter = time.gmtime

    root_logger = logging.getLogger()
    root_logger.setLevel(logging.INFO)

    stream_handler = logging.StreamHandler(sys.stdout)
    stream_handler.setFormatter(formatter)
    root_logger.addHandler(stream_handler)

    if log_file_path:
        file_handler = logging.handlers.TimedRotatingFileHandler(
            log_file_path,
            when="midnight",
            utc=True,
            backupCount=0,   # keep all rotated files — logs are the audit trail
            encoding="utf-8",
        )
        file_handler.setFormatter(formatter)
        root_logger.addHandler(file_handler)


# ── Schedule ──────────────────────────────────────────────────────────────────

def _compute_next_wakeup() -> datetime:
    """
    Returns the next H:WAKEUP_OFFSET_MINUTES:00 UTC timestamp after now.

    If now is already past the current hour's wakeup point returns the next
    hour's wakeup time instead.

    Returns:
        UTC-aware datetime of the next scheduled wakeup.
    """
    now = datetime.now(tz=timezone.utc)
    candidate = now.replace(minute=WAKEUP_OFFSET_MINUTES, second=0, microsecond=0)
    if now >= candidate:
        candidate += timedelta(hours=1)
    return candidate


# ── Close-detail helpers ──────────────────────────────────────────────────────

def _query_tp_sl_close(pos: Dict) -> Optional[Dict]:
    """
    Queries execution history to identify which leg (TP or SL) fired and the fill price.

    Finds the closing execution after the entry timestamp, compares the fill price
    to the stored TP and SL reference levels (whichever is closer), and computes
    realised P&L.

    Args:
        pos: Active position dict from state.py.

    Returns:
        Dict with keys leg ("TP"/"SL"), fill_price, pnl_usdt, pnl_pct; or None on failure.
    """
    try:
        entry_dt = datetime.fromisoformat(pos["entry_time_utc"])
        entry_time_ms = int(entry_dt.timestamp() * 1000)

        execution = get_closing_execution(
            direction=pos["direction"],
            entry_time_ms=entry_time_ms,
        )
        if execution is None:
            logger.warning("[CLOSE_DETAILS] No closing execution found in recent history.")
            return None

        fill_price = float(execution["execPrice"])
        tp_dist = abs(fill_price - pos["tp_price"])
        sl_dist = abs(fill_price - pos["sl_price"])
        leg = "TP" if tp_dist < sl_dist else "SL"

        return {"leg": leg, "fill_price": fill_price, **_compute_pnl(pos, fill_price)}

    except Exception as exc:
        logger.warning(f"[CLOSE_DETAILS] Failed to query execution history: {exc}")
        return None


def _query_time_exit_fill(close_order_id: str) -> Optional[float]:
    """
    Returns the average fill price of a market close order placed by the engine.

    Args:
        close_order_id: Bybit order ID returned by close_position_market().

    Returns:
        Fill price in USDT, or None if the query fails or order not yet filled.
    """
    try:
        order = get_order_status(close_order_id)

        avg_price_str = order.get("avgPrice", "")
        if avg_price_str and float(avg_price_str) > 0:
            return float(avg_price_str)

        exec_value = float(order.get("cumExecValue", 0))
        exec_qty = float(order.get("cumExecQty", 0))
        if exec_qty > 0:
            return exec_value / exec_qty

    except Exception as exc:
        logger.warning(f"[CLOSE_FILL] Failed to query close order status: {exc}")

    return None


def _compute_pnl(pos: Dict, fill_price: float) -> Dict:
    """
    Computes realised P&L in USDT and as a percentage of margin.

    Args:
        pos:        Position state dict with direction, entry_price,
                    position_notional, and margin_usdt.
        fill_price: Actual (or simulated) close fill price in USDT.

    Returns:
        Dict with pnl_usdt and pnl_pct keys.
    """
    entry = pos["entry_price"]
    notional = pos["position_notional"]
    margin = pos["margin_usdt"]

    if pos["direction"] == "long":
        pnl_usdt = (fill_price - entry) / entry * notional
    else:
        pnl_usdt = (entry - fill_price) / entry * notional

    pnl_pct = pnl_usdt / margin * 100 if margin > 0 else 0.0
    return {"pnl_usdt": pnl_usdt, "pnl_pct": pnl_pct}


# ── Notification message builders ─────────────────────────────────────────────

def _paper_wrap(message: str) -> str:
    return f"<i>[PAPER MODE — no real funds]</i>\n{message}"


def _fmt_trade_opened(
    direction: str,
    tier: int,
    entry_price: float,
    qty_btc: float,
    sizing: Dict,
    exit_deadline: datetime,
    paper_mode: bool = False,
) -> str:
    tp_sign = "+" if direction == "long" else "-"
    sl_sign = "-" if direction == "long" else "+"
    msg = (
        f"<b>TRADE OPENED</b>\n"
        f"{direction.upper()} T{tier} @ {entry_price:,.2f} USDT\n\n"
        f"Qty      : {qty_btc:.4f} BTC\n"
        f"Notional : {sizing['position_notional']:,.0f} USDT ({LEVERAGE}x)\n"
        f"Margin   : {sizing['margin_usdt']:,.0f} USDT\n"
        f"TP       : {sizing['tp_price']:,.2f} ({tp_sign}{sizing['tp_pct']*100:.1f}%)\n"
        f"SL       : {sizing['sl_price']:,.2f} ({sl_sign}{sizing['sl_pct']*100:.1f}%)\n"
        f"Window   : {sizing['hold_hours']}h — deadline "
        f"{exit_deadline.strftime('%Y-%m-%d %H:%M')} UTC"
    )
    return _paper_wrap(msg) if paper_mode else msg


def _fmt_trade_closed_tp_sl(pos: Dict, close: Optional[Dict], paper_mode: bool = False) -> str:
    opened = pos["entry_time_utc"][:16].replace("T", " ")

    if close:
        pnl_sign = "+" if close["pnl_usdt"] >= 0 else ""
        detail = (
            f"Leg  : <b>{close['leg']} hit</b>\n"
            f"Fill : {close['fill_price']:,.2f} USDT\n"
            f"P&L  : {pnl_sign}{close['pnl_usdt']:,.2f} USDT "
            f"({pnl_sign}{close['pnl_pct']:.1f}% on margin)"
        )
    else:
        detail = (
            f"Leg  : TP or SL (could not determine — check Bybit)\n"
            f"Fill : unavailable\n"
            f"P&L  : unavailable"
        )

    msg = (
        f"<b>TRADE CLOSED</b> — TP/SL\n"
        f"{pos['direction'].upper()} T{pos['tier']} | entry @ {pos['entry_price']:,.2f}\n\n"
        f"{detail}\n"
        f"TP ref : {pos['tp_price']:,.2f} | SL ref : {pos['sl_price']:,.2f}\n"
        f"Opened : {opened} UTC"
    )
    return _paper_wrap(msg) if paper_mode else msg


def _fmt_trade_closed_time(pos: Dict, fill_price: Optional[float], paper_mode: bool = False) -> str:
    opened = pos["entry_time_utc"][:16].replace("T", " ")

    if fill_price is not None:
        pnl = _compute_pnl(pos, fill_price)
        pnl_sign = "+" if pnl["pnl_usdt"] >= 0 else ""
        pnl_line = (
            f"Fill : {fill_price:,.2f} USDT\n"
            f"P&L  : {pnl_sign}{pnl['pnl_usdt']:,.2f} USDT "
            f"({pnl_sign}{pnl['pnl_pct']:.1f}% on margin)"
        )
    else:
        pnl_line = "Fill : unavailable — check Bybit"

    msg = (
        f"<b>TRADE CLOSED</b> — TIME EXIT\n"
        f"{pos['direction'].upper()} T{pos['tier']} | entry @ {pos['entry_price']:,.2f}\n\n"
        f"{pnl_line}\n"
        f"Window : {pos['hold_hours']}h expired\n"
        f"Opened : {opened} UTC"
    )
    return _paper_wrap(msg) if paper_mode else msg


def _fmt_error(
    label: str,
    detail: str,
    equity: Optional[float] = None,
    peak: Optional[float] = None,
) -> str:
    ts = datetime.now(tz=timezone.utc).strftime("%Y-%m-%d %H:%M")
    msg = f"<b>ENGINE ERROR</b> — {label}\n\n{detail}"

    if equity is not None and peak is not None and peak > 0:
        drawdown_pct = (peak - equity) / peak * 100
        msg += (
            f"\n\nEquity   : {equity:,.2f} USDT\n"
            f"Peak     : {peak:,.2f} USDT\n"
            f"Drawdown : {drawdown_pct:.1f}%"
        )

    return msg + f"\n\n{ts} UTC"


# ── Active position management ────────────────────────────────────────────────

def _handle_active_position(
    active_position: Dict,
    notifier: BaseNotifier,
    dry_run: bool,
    paper_mode: bool = False,
    last_close_price: Optional[float] = None,
) -> None:
    """
    Manages an already-open position: reconciles with Bybit (live) or simulates
    TP/SL price checks (paper), and handles time-based exits.

    Args:
        active_position:  Position state dict from load_active_position().
        notifier:         Notification backend for trade events.
        dry_run:          When True, logs actions without executing any orders.
        paper_mode:       When True, skips all Bybit order/position API calls and
                          simulates TP/SL detection using the last candle close price.
        last_close_price: Close price of the most recent closed candle, used in
                          paper mode to check TP/SL and compute time-exit P&L.
    """
    direction = active_position["direction"]
    tier = active_position["tier"]
    qty_btc = active_position["qty_btc"]
    entry_price = active_position["entry_price"]
    deadline_str = active_position["exit_deadline_utc"]

    # ── Paper mode: simulate position management without Bybit API calls ──────
    if paper_mode:
        if last_close_price is not None:
            tp_price = active_position["tp_price"]
            sl_price = active_position["sl_price"]

            if direction == "long":
                tp_hit = last_close_price >= tp_price
                sl_hit = last_close_price <= sl_price
            else:
                tp_hit = last_close_price <= tp_price
                sl_hit = last_close_price >= sl_price

            if tp_hit or sl_hit:
                leg = "TP" if tp_hit else "SL"
                sim_fill = tp_price if tp_hit else sl_price
                close_details = {
                    "leg": leg,
                    "fill_price": sim_fill,
                    **_compute_pnl(active_position, sim_fill),
                }
                logger.info(
                    f"[PAPER_CLOSE] {direction.upper()} T{tier} simulated {leg} hit. "
                    f"fill={sim_fill:.2f}  P&L={close_details['pnl_usdt']:+.2f} USDT"
                )
                clear_active_position()
                notifier.send(
                    "TRADE_CLOSED",
                    _fmt_trade_closed_tp_sl(active_position, close_details, paper_mode=True),
                )
                return

        if is_deadline_passed(active_position):
            logger.info(
                f"[PAPER_DEADLINE] Hold window expired for {direction.upper()} T{tier}. "
                f"Simulating time-based close."
            )
            clear_active_position()
            notifier.send(
                "TRADE_CLOSED",
                _fmt_trade_closed_time(active_position, last_close_price, paper_mode=True),
            )
            return

        remaining = datetime.fromisoformat(deadline_str) - datetime.now(tz=timezone.utc)
        logger.info(
            f"[PAPER_HOLDING] {direction.upper()} T{tier} @ {entry_price:.2f} | "
            f"deadline in {remaining.total_seconds()/3600:.1f}h ({deadline_str})"
        )
        return

    # ── Live mode: reconcile with Bybit ───────────────────────────────────────
    bybit_position = get_open_position(symbol=SYMBOL)

    if bybit_position is None:
        close_details = _query_tp_sl_close(active_position)

        if close_details:
            logger.info(
                f"[RECONCILE] {direction.upper()} T{tier} @ {entry_price:.2f} "
                f"closed by {close_details['leg']}. "
                f"Fill={close_details['fill_price']:.2f}  "
                f"P&L={close_details['pnl_usdt']:+.2f} USDT "
                f"({close_details['pnl_pct']:+.1f}% on margin)"
            )
        else:
            logger.info(
                f"[RECONCILE] {direction.upper()} T{tier} @ {entry_price:.2f} "
                f"no longer open on Bybit — TP or SL triggered (fill unavailable)."
            )

        if not dry_run:
            clear_active_position()
        notifier.send("TRADE_CLOSED", _fmt_trade_closed_tp_sl(active_position, close_details))
        return

    if is_deadline_passed(active_position):
        logger.info(
            f"[DEADLINE] Hold window expired for {direction.upper()} T{tier} "
            f"(deadline {deadline_str}). Executing time-based market close."
        )

        fill_price = None
        if not dry_run:
            cancel_all_active_orders(symbol=SYMBOL)
            close_order_id = close_position_market(direction=direction, qty_btc=qty_btc)
            fill_price = _query_time_exit_fill(close_order_id)
            clear_active_position()

            if fill_price:
                pnl = _compute_pnl(active_position, fill_price)
                logger.info(
                    f"[TIME_EXIT] order_id={close_order_id}  "
                    f"fill={fill_price:.2f}  "
                    f"P&L={pnl['pnl_usdt']:+.2f} USDT ({pnl['pnl_pct']:+.1f}% on margin)"
                )
            else:
                logger.info(f"[TIME_EXIT] order_id={close_order_id}  fill=unavailable")
        else:
            logger.info("[DRY_RUN] Would cancel orders and close position at market.")

        notifier.send("TRADE_CLOSED", _fmt_trade_closed_time(active_position, fill_price))
        return

    remaining = datetime.fromisoformat(deadline_str) - datetime.now(tz=timezone.utc)
    logger.info(
        f"[HOLDING] {direction.upper()} T{tier} @ {entry_price:.2f} | "
        f"deadline in {remaining.total_seconds()/3600:.1f}h ({deadline_str})"
    )


# ── Main cycle ────────────────────────────────────────────────────────────────

def run_once(
    notifier: BaseNotifier,
    dry_run: bool = False,
    paper_mode: bool = False,
) -> None:
    """
    Executes one full detection and execution cycle.

    Args:
        notifier:   Notification backend for trade lifecycle events.
        dry_run:    When True, runs detection and sizing but places no orders
                    and writes no state. No notifications sent.
        paper_mode: When True, runs the full pipeline including state writes and
                    notifications, but places no real orders. Uses PAPER_CAPITAL_USDT
                    for sizing. All notifications are prefixed with [PAPER].
    """
    cycle_start = datetime.now(tz=timezone.utc)
    mode_label = "PAPER" if paper_mode else ("DRY_RUN" if dry_run else "LIVE")
    logger.info(f"[CYCLE_START] {cycle_start.strftime('%Y-%m-%d %H:%M:%S')} UTC  mode={mode_label}")

    # Step 1: Fetch candles and compute indicators
    df_raw = fetch_candles()
    df = compute_all(df_raw)
    last_candle_time = df.index[-1]
    last_close_price = float(df["close"].iloc[-1])
    logger.info(f"[DATA] {len(df)} candles loaded. Last closed: {last_candle_time}  close={last_close_price:.2f}")

    # Step 2: Active position management
    active_position = load_active_position()
    if active_position is not None:
        _handle_active_position(
            active_position,
            notifier,
            dry_run,
            paper_mode=paper_mode,
            last_close_price=last_close_price,
        )
        return

    # Step 2b: Bybit-side position guard (live only — paper and dry-run never open positions)
    if not paper_mode and not dry_run and get_open_position(symbol=SYMBOL) is not None:
        logger.warning(
            "[POSITION_GUARD] Open position on Bybit but no local state found. "
            "Skipping new entry until the exchange position clears or is reconciled."
        )
        notifier.send(
            "ERROR",
            _fmt_error(
                "POSITION_GUARD",
                "Open position detected on Bybit without matching local state.\n"
                "New entry blocked — manual reconciliation required.",
            ),
        )
        return

    # Step 3: Signal detection on the last closed candle
    detection_result = detect(df)

    if detection_result["status"] != "active":
        logger.info(
            f"[NO_SIGNAL] No entry signal on last candle. "
            f"WT1={detection_result['wt1_current']:.2f}  "
            f"WT2={detection_result['wt2_current']:.2f}  "
            f"MFI={detection_result['mfi_current']:.4f}"
        )
        return

    direction = detection_result["direction"]
    tier = detection_result["tier"]
    entry_price = detection_result["entry_price"]
    entry_time = detection_result["entry_time"]

    logger.info(
        f"[SIGNAL] {direction.upper()} T{tier} detected on candle {entry_time} "
        f"| entry_price={entry_price:.2f}"
    )

    # Step 4: Determine capital and compute sizing
    if paper_mode:
        capital = PAPER_CAPITAL_USDT
        logger.info(f"[CAPITAL] Paper mode — using simulated capital: {capital:.2f} USDT")
    else:
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
        f"qty={qty_btc:.4f} BTC  "
        f"TP={sizing['tp_price']:.2f}  SL={sizing['sl_price']:.2f}  "
        f"hold={sizing['hold_hours']}h"
    )

    # Step 5: Pre-trade risk guard (skipped in paper mode — simulated capital always passes)
    if not paper_mode:
        guard_result = run_pre_trade_checks(
            capital=capital,
            qty_btc=qty_btc,
            sizing=sizing,
            leverage=LEVERAGE,
        )

        if not guard_result.ok:
            logger.warning(f"[GUARD_BLOCK] Trade blocked — {guard_result.reason}")
            peak = load_peak_equity()
            notifier.send(
                "ERROR",
                _fmt_error("GUARD_BLOCK", guard_result.reason, equity=capital, peak=peak),
            )
            return
    else:
        logger.info("[PAPER_GUARD] Risk guard skipped — paper mode.")

    if dry_run:
        exit_deadline_dry = entry_time.to_pydatetime() + timedelta(hours=sizing["hold_hours"])
        logger.info(
            f"[DRY_RUN] Would place {direction.upper()} T{tier} market order: "
            f"qty={qty_btc:.4f} BTC  TP={sizing['tp_price']:.2f}  SL={sizing['sl_price']:.2f}  "
            f"deadline={exit_deadline_dry.strftime('%Y-%m-%d %H:%M')} UTC"
        )
        return

    # Step 6: Write pending state before placing the order (or before simulating it).
    entry_time_dt = entry_time.to_pydatetime()
    exit_deadline = entry_time_dt + timedelta(hours=sizing["hold_hours"])

    if not paper_mode:
        set_leverage(symbol=SYMBOL, leverage=LEVERAGE)

    save_active_position(
        direction=direction,
        tier=tier,
        entry_time_utc=entry_time_dt,
        hold_hours=sizing["hold_hours"],
        entry_price=entry_price,
        tp_price=sizing["tp_price"],
        sl_price=sizing["sl_price"],
        bybit_order_id="PENDING" if not paper_mode else "PAPER",
        qty_btc=qty_btc,
        position_notional=sizing["position_notional"],
        margin_usdt=sizing["margin_usdt"],
        risk_amount_usdt=sizing["risk_amount_usdt"],
    )
    logger.info(
        f"[PRE_ORDER_STATE] {'Paper' if paper_mode else 'Pending'} state written. "
        f"Exit deadline: {exit_deadline.strftime('%Y-%m-%d %H:%M')} UTC"
    )

    # Step 7: Place order (live) or simulate (paper)
    if paper_mode:
        order_id = "PAPER_TRADE"
        logger.info(
            f"[PAPER_ENTRY] Simulated {direction.upper()} T{tier} order: "
            f"qty={qty_btc:.4f} BTC  TP={sizing['tp_price']:.2f}  SL={sizing['sl_price']:.2f}"
        )
    else:
        try:
            order_id = place_entry_order(
                direction=direction,
                qty_btc=qty_btc,
                tp_price=sizing["tp_price"],
                sl_price=sizing["sl_price"],
            )
        except Exception:
            clear_active_position()
            raise

        logger.info(
            f"[ENTRY] Order placed and state confirmed. "
            f"order_id={order_id}  "
            f"qty={qty_btc:.4f} BTC  TP={sizing['tp_price']:.2f}  SL={sizing['sl_price']:.2f}"
        )

    patch_order_id(order_id)

    notifier.send(
        "TRADE_OPENED",
        _fmt_trade_opened(
            direction=direction,
            tier=tier,
            entry_price=entry_price,
            qty_btc=qty_btc,
            sizing=sizing,
            exit_deadline=exit_deadline,
            paper_mode=paper_mode,
        ),
    )


def run_forever(
    notifier: BaseNotifier,
    dry_run: bool = False,
    paper_mode: bool = False,
) -> None:
    """
    Blocks indefinitely, running one detection cycle at H:01 UTC each hour.

    Errors inside run_once() are caught and logged without stopping the loop.

    Args:
        notifier:   Notification backend forwarded to each run_once() call.
        dry_run:    When True, forwarded to run_once() — no orders placed, no notifications.
        paper_mode: When True, forwarded to run_once() — full pipeline with simulated trades.
    """
    mode_label = "PAPER" if paper_mode else ("DRY_RUN" if dry_run else "LIVE")
    logger.info(
        f"[ENGINE_START] Trading engine started. "
        f"symbol={SYMBOL}  leverage={LEVERAGE}x  mode={mode_label}"
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
            run_once(notifier=notifier, dry_run=dry_run, paper_mode=paper_mode)
        except Exception as cycle_error:
            logger.error(
                f"[CYCLE_ERROR] Unhandled exception in run_once(): {cycle_error}",
                exc_info=True,
            )
            notifier.send("ERROR", _fmt_error("CYCLE_ERROR", str(cycle_error)))
