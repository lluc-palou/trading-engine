"""
Signal detection — basin topology, divergence structure, and entry confirmation
for the momentum-exhaustion-reversal strategy.

Operates on a DataFrame that already carries wt1, wt2, and mfi columns (produced
by src/strategy/indicators.py). Ported from the original monitoring dashboard with
no changes to detection logic.

The live engine calls detect() at each hourly candle close to determine whether
a new tradeable signal has formed on the most recent closed candle.

Public interface:
    detect_all_signals(df)  →  list of signal dicts, chronological (oldest first)
    detect(df)              →  dict describing current state + recent signal history
"""

from typing import Dict, List, Optional, Set, Tuple

import numpy as np
import pandas as pd

# ── Detection parameters ─────────────────────────────────────────────────────
LOOKBACK_CANDLES: int = 120
MIN_WALL_CANDLES: int = 1
ENTRY_SEARCH_WINDOW: int = 80

WT_EXTREME: float = 53.0
WT_TIER2: float = 60.0
WT_TIER3: float = 75.0


# ── Basin construction ───────────────────────────────────────────────────────

def _label_connected_regions(mask: np.ndarray) -> Tuple[np.ndarray, int]:
    """
    Labels contiguous True regions in a boolean mask with sequential integers.

    Args:
        mask: Boolean array where True marks oscillator-below-zero positions.

    Returns:
        Tuple of (labeled array, number of distinct regions found).
    """
    labeled = np.zeros(len(mask), dtype=int)
    current_label = 0
    in_region = False
    for i, val in enumerate(mask):
        if val and not in_region:
            current_label += 1
            in_region = True
        elif not val:
            in_region = False
        if in_region:
            labeled[i] = current_label
    return labeled, current_label


def _build_basins(
    proximal: np.ndarray,
    distal: np.ndarray,
    wt1: np.ndarray,
    wt2: np.ndarray,
    price: np.ndarray,
    mfi: np.ndarray,
    direction: str,
    extreme_threshold: float,
) -> List[Dict]:
    """
    Constructs basin structures from the oscillator's negative regions.

    Adjacent regions separated by fewer than MIN_WALL_CANDLES confirmed wall
    candles are merged into a single basin. Each basin records its floor position
    (deepest distal extreme), whether it is closed (followed by a wall), and
    whether it qualifies as extreme (distal floor breaches extreme_threshold).

    Args:
        proximal:          The proximal oscillator line (max(wt1,wt2) for longs,
                           negated for shorts). Basin interiors are proximal < 0.
        distal:            The distal oscillator line (min for longs, negated for shorts).
        wt1:               Raw WT1 values.
        wt2:               Raw WT2 values.
        price:             Close price array aligned with the window.
        mfi:               Money Flow array aligned with the window.
        direction:         "long" or "short".
        extreme_threshold: Threshold below which the distal floor qualifies as extreme.

    Returns:
        List of basin dicts, each containing floor position, price/MFI at floor,
        region bounds, and extreme/closed flags.
    """
    n = len(proximal)
    labeled, n_regions = _label_connected_regions(proximal < 0)
    if n_regions == 0:
        return []

    raw_regions = [np.where(labeled == k)[0] for k in range(1, n_regions + 1)]

    merged: List[List[int]] = [raw_regions[0].tolist()]
    for region in raw_regions[1:]:
        prev_end = merged[-1][-1]
        cur_start = int(region[0])
        gap = proximal[prev_end + 1:cur_start]
        wall_width = int(np.sum(gap >= 0)) if len(gap) > 0 else 0
        if wall_width < MIN_WALL_CANDLES:
            merged[-1].extend(range(prev_end + 1, cur_start))
            merged[-1].extend(region.tolist())
        else:
            merged.append(region.tolist())

    basins: List[Dict] = []
    for idx_list in merged:
        idxs = np.array(idx_list)
        floor_pos = int(idxs[np.argmin(distal[idxs])])
        region_end = int(idxs[-1])

        wall_width = 0
        for offset in range(region_end + 1, n):
            if proximal[offset] >= 0:
                wall_width += 1
            else:
                break
        is_closed = wall_width >= MIN_WALL_CANDLES

        is_reversing = False
        if not is_closed and region_end == n - 1:
            wt_diff = wt1[idxs] - wt2[idxs]
            if direction == "long":
                cross = len(wt_diff) > 1 and bool(np.any(wt_diff[:-1] < 0) and wt_diff[-1] > 0)
            else:
                cross = len(wt_diff) > 1 and bool(np.any(wt_diff[:-1] > 0) and wt_diff[-1] < 0)
            if cross:
                lookback = min(3, len(idxs) - 1)
                is_reversing = bool(proximal[region_end] > proximal[region_end - lookback])

        basins.append({
            "floor_pos":      floor_pos,
            "floor_distal":   float(distal[floor_pos]),
            "price_at_floor": float(price[floor_pos]),
            "mfi_at_floor":   float(mfi[floor_pos]),
            "region_start":   int(idxs[0]),
            "region_end":     region_end,
            "valid":          is_closed or is_reversing,
            "extreme":        float(distal[floor_pos]) < extreme_threshold,
        })

    return basins


def _find_divergence_structure(
    wt1: np.ndarray,
    wt2: np.ndarray,
    price: np.ndarray,
    mfi: np.ndarray,
    direction: str,
) -> Optional[Tuple[Dict, Dict]]:
    """
    Finds the first anchor-trigger divergence structure within a window slice.

    For longs: basins are negative regions of max(wt1, wt2); extreme when
    min(wt1, wt2) < -53. For shorts: the series are negated so the same basin
    logic applies; extreme when max(wt1, wt2) > +53.

    The anchor is the deepest extreme basin with MFI confirming direction.
    The trigger is the first subsequent shallower extreme where price extends
    (lower low for longs, higher high for shorts) and MFI flips sign.

    Args:
        wt1:       WT1 array for the current lookback window slice.
        wt2:       WT2 array for the current lookback window slice.
        price:     Close price array for the current lookback window slice.
        mfi:       Money Flow array for the current lookback window slice.
        direction: "long" or "short".

    Returns:
        Tuple of (anchor basin dict, trigger basin dict) if a valid divergence
        structure is found; None otherwise.
    """
    if direction == "long":
        proximal = np.maximum(wt1, wt2)
        distal = np.minimum(wt1, wt2)
    else:
        proximal = -np.minimum(wt1, wt2)
        distal = -np.maximum(wt1, wt2)

    extreme_threshold = -WT_EXTREME  # same for both directions; distal is negated for shorts

    basins = _build_basins(proximal, distal, wt1, wt2, price, mfi, direction, extreme_threshold)
    if not basins:
        return None

    extreme_valid = [b for b in basins if b["valid"] and b["extreme"]]
    anchor: Optional[Dict] = None
    trigger: Optional[Dict] = None

    for basin in extreme_valid:
        if anchor is None:
            mfi_ok = basin["mfi_at_floor"] > 0 if direction == "long" else basin["mfi_at_floor"] < 0
            if mfi_ok:
                anchor = basin
        elif basin["floor_distal"] < anchor["floor_distal"]:
            mfi_ok = basin["mfi_at_floor"] > 0 if direction == "long" else basin["mfi_at_floor"] < 0
            anchor = basin if mfi_ok else None
            trigger = None
        elif trigger is None:
            price_extreme = (
                basin["price_at_floor"] < anchor["price_at_floor"] if direction == "long"
                else basin["price_at_floor"] > anchor["price_at_floor"]
            )
            mfi_flip = (
                basin["mfi_at_floor"] < 0 if direction == "long"
                else basin["mfi_at_floor"] > 0
            )
            if price_extreme and mfi_flip:
                trigger = basin
                break

    if anchor is None or trigger is None:
        return None
    return anchor, trigger


def _find_entry_bar(
    wt1: np.ndarray,
    wt2: np.ndarray,
    trigger_abs_idx: int,
    direction: str,
    total: int,
) -> Optional[int]:
    """
    Finds the first candle after the trigger floor where WT1 crosses WT2 in
    the recovery direction (crossover-only entry mode).

    No threshold recovery is required: the basin already qualified as extreme,
    so the crossover occurs while both lines are still deep in the oversold or
    overbought zone, making it the earliest structurally valid entry point.

    Args:
        wt1:              Full WT1 array (absolute indices).
        wt2:              Full WT2 array (absolute indices).
        trigger_abs_idx:  Absolute index of the trigger basin floor.
        direction:        "long" or "short".
        total:            Total number of candles in the DataFrame.

    Returns:
        Absolute index of the entry candle, or None if no crossover is found
        within ENTRY_SEARCH_WINDOW candles of the trigger.
    """
    end = min(trigger_abs_idx + ENTRY_SEARCH_WINDOW, total)
    for i in range(trigger_abs_idx, end):
        w1, w2 = wt1[i], wt2[i]
        if direction == "long" and w1 > w2:
            return i
        if direction == "short" and w1 < w2:
            return i
    return None


def _classify_tier(anchor_floor_distal: float) -> Optional[int]:
    """
    Classifies a signal by anchor magnitude into Tier 2, Tier 3, or None (Tier 1, filtered).

    Args:
        anchor_floor_distal: The distal floor value of the anchor basin (negative for longs).

    Returns:
        3 if magnitude >= WT_TIER3, 2 if >= WT_TIER2, None otherwise (Tier 1 — excluded).
    """
    magnitude = abs(anchor_floor_distal)
    if magnitude >= WT_TIER3:
        return 3
    if magnitude >= WT_TIER2:
        return 2
    return None


# ── Public interface ─────────────────────────────────────────────────────────

def detect_all_signals(df: pd.DataFrame) -> List[Dict]:
    """
    Scans the full indicator DataFrame for every valid Tier 2+ divergence signal.

    Uses entry-bar deduplication rather than a cooldown window, so all unique
    confirmed entries are surfaced regardless of spacing. When the oldest structure
    in a window resolves to an already-seen entry, the scan advances past that
    structure and searches for the next independent one in the remaining window slice.

    Args:
        df: DataFrame with columns [open, high, low, close, volume, wt1, wt2, mfi]
            and a UTC-aware DatetimeIndex.

    Returns:
        List of signal dicts ordered chronologically (oldest first). Each dict has:
            direction       "long" | "short"
            tier            2 | 3
            entry_time      pd.Timestamp (UTC-aware)
            entry_price     float
            entry_bar_idx   int  (absolute index into df)
            anchor_bar_idx  int
            trigger_bar_idx int
    """
    wt1_arr = df["wt1"].to_numpy()
    wt2_arr = df["wt2"].to_numpy()
    price_arr = df["close"].to_numpy()
    mfi_arr = df["mfi"].to_numpy()
    total = len(df)

    seen_entry_bars: Set[int] = set()
    signals: List[Dict] = []

    for bar in range(LOOKBACK_CANDLES, total):
        lb = bar - LOOKBACK_CANDLES

        for direction in ("long", "short"):
            advance = 0
            found_for_direction = False

            while advance < bar - lb:
                result = _find_divergence_structure(
                    wt1_arr[lb + advance:bar],
                    wt2_arr[lb + advance:bar],
                    price_arr[lb + advance:bar],
                    mfi_arr[lb + advance:bar],
                    direction,
                )
                if result is None:
                    break

                anchor, trigger = result
                anchor_abs = lb + advance + anchor["floor_pos"]
                trigger_abs = lb + advance + trigger["floor_pos"]

                entry_bar = _find_entry_bar(wt1_arr, wt2_arr, trigger_abs, direction, total)

                next_advance = advance + max(anchor["region_end"], trigger["region_end"]) + 1

                if entry_bar is None or entry_bar > bar:
                    advance = next_advance
                    continue

                if entry_bar in seen_entry_bars:
                    advance = next_advance
                    continue

                tier = _classify_tier(anchor["floor_distal"])
                if tier is None:
                    advance = next_advance
                    continue

                seen_entry_bars.add(entry_bar)
                signals.append({
                    "direction":       direction,
                    "tier":            tier,
                    "entry_time":      df.index[entry_bar],
                    "entry_price":     float(price_arr[entry_bar]),
                    "entry_bar_idx":   entry_bar,
                    "anchor_bar_idx":  anchor_abs,
                    "trigger_bar_idx": trigger_abs,
                })
                found_for_direction = True
                break

            if found_for_direction:
                break  # one signal per bar (first direction that fires)

    return signals


def detect(df: pd.DataFrame, n_recent: int = 10) -> Dict:
    """
    Runs full detection and returns current signal state plus recent signal history.

    Calls detect_all_signals() and determines whether the most recent signal fires
    on the last closed candle (active — tradeable this hour) or earlier (no active
    signal). Called by the orchestrator at each hourly candle close.

    Args:
        df:       DataFrame with columns [open, high, low, close, volume, wt1, wt2, mfi].
        n_recent: Number of recent historical signals to include in the return dict.

    Returns:
        Dict with keys:
            status          "active" | "none"
            direction       "long" | "short" | None
            tier            2 | 3 | None
            entry_price     float | None
            entry_time      pd.Timestamp | None
            anchor_bar_idx  int | None
            trigger_bar_idx int | None
            entry_bar_idx   int | None
            wt1_current     float
            wt2_current     float
            mfi_current     float
            recent_signals  list of up to n_recent signal dicts (newest first)
    """
    total = len(df)
    wt1_now = float(df["wt1"].iloc[-1])
    wt2_now = float(df["wt2"].iloc[-1])
    mfi_now = float(df["mfi"].iloc[-1])

    result: Dict = {
        "status":          "none",
        "direction":       None,
        "tier":            None,
        "entry_price":     None,
        "entry_time":      None,
        "anchor_bar_idx":  None,
        "trigger_bar_idx": None,
        "entry_bar_idx":   None,
        "wt1_current":     wt1_now,
        "wt2_current":     wt2_now,
        "mfi_current":     mfi_now,
        "recent_signals":  [],
    }

    if total < LOOKBACK_CANDLES:
        return result

    all_signals = detect_all_signals(df)
    result["recent_signals"] = list(reversed(all_signals[-n_recent:]))

    if not all_signals:
        return result

    latest = all_signals[-1]
    if latest["entry_bar_idx"] == total - 1:
        result.update({
            "status":          "active",
            "direction":       latest["direction"],
            "tier":            latest["tier"],
            "entry_price":     latest["entry_price"],
            "entry_time":      latest["entry_time"],
            "anchor_bar_idx":  latest["anchor_bar_idx"],
            "trigger_bar_idx": latest["trigger_bar_idx"],
            "entry_bar_idx":   latest["entry_bar_idx"],
        })

    return result
