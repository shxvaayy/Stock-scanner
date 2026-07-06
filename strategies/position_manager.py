"""Position manager — owns multi-entry / multi-exit accounting.

Every new strategy uses ScaledPosition for all entries, exits, profit ladder,
averaging, pyramiding. Strategies do NOT track qty, average cost, or
ladder state on their own — they query this module.

Profiles:
- "no_scaling"     : single entry, single exit ladder, no adds
- "profit_pyramid" : DEFAULT. Profit ladder + optional pyramid on fresh signal
- "averaging"      : Initial + up to 2 averages on loss + profit ladder
- "full"           : averaging + pyramiding
"""

from __future__ import annotations

import logging
from dataclasses import dataclass, field
from datetime import date, datetime, time, timedelta
from typing import Literal

log = logging.getLogger("autotheta.position_mgr")


@dataclass
class Entry:
    ts: datetime
    qty: int
    premium: float
    kind: Literal["initial", "average", "pyramid"] = "initial"


@dataclass
class Exit:
    ts: datetime
    qty: int
    premium: float
    reason: str


@dataclass
class ScalingConfig:
    """All scaling knobs in one place. Defaults are conservative."""
    profile: str = "profit_pyramid"
    # (premium_mult_vs_avg_cost, exit_fraction_of_remaining)
    profit_targets: list[tuple[float, float]] = field(
        default_factory=lambda: [(1.5, 0.33), (2.0, 0.33), (3.0, 1.0)]
    )
    # Structure-derived ABSOLUTE premium targets [(premium, exit_fraction)].
    # When set, takes precedence over the multiplicative profit_targets —
    # strategies compute these at entry as avg_cost + delta x distance to the
    # structural level (POC, opposite VA boundary, swept-level origin), which
    # keeps targets geometrically reachable intraday.
    target_premiums: list[tuple[float, float]] | None = None
    avg_triggers: list[float] = field(default_factory=lambda: [0.80, 0.65])
    avg_size_mults: list[float] = field(default_factory=lambda: [0.5, 0.5])
    max_total_size_mult: float = 2.0
    pyramid_after_target_idx: int = 1   # add after T2; -1 disables
    pyramid_size_mult: float = 0.5
    pyramid_requires_fresh_signal: bool = True
    max_pyramids: int = 1
    hard_stop_pct_from_avg: float = 0.45
    no_averaging_after_minutes: int = 60
    no_averaging_cutoff_time: time = time(14, 0)


class ScaledPosition:
    """Owns ALL state for one trade: entries, exits, average cost, ladder.

    Strategies create one of these on entry signal, then query its decision
    methods on every tick / candle.
    """

    def __init__(self, strategy_name: str, trade_id: str, direction: str,
                 initial_premium: float, initial_qty: int,
                 invalidation_level: float | None,
                 invalidation_direction: str,  # "below" | "above"
                 scaling_config: ScalingConfig,
                 initial_entry_ts: datetime | None = None):
        self.strategy_name = strategy_name
        self.trade_id = trade_id
        self.direction = direction.lower()
        self.initial_premium = initial_premium
        self.initial_qty = initial_qty
        self.invalidation_level = invalidation_level
        self.invalidation_direction = invalidation_direction
        self.scaling_config = scaling_config
        self.entries: list[Entry] = []
        self.exits: list[Exit] = []
        self.targets_hit: set[int] = set()
        self.averages_done = 0
        self.pyramids_done = 0
        self.initial_entry_ts = initial_entry_ts or datetime.now()
        # Override path for RSI predictor (where invalidation is RSI-based, not price-based)
        self.invalidation_override = False

    # ─── State ───
    def add_entry(self, qty: int, premium: float, ts: datetime,
                  kind: Literal["initial", "average", "pyramid"] = "initial"):
        self.entries.append(Entry(ts=ts, qty=qty, premium=premium, kind=kind))
        if kind == "average":
            self.averages_done += 1
        elif kind == "pyramid":
            self.pyramids_done += 1

    def add_exit(self, qty: int, premium: float, ts: datetime, reason: str):
        self.exits.append(Exit(ts=ts, qty=qty, premium=premium, reason=reason))

    # ─── Properties ───
    @property
    def avg_cost(self) -> float:
        """Weighted average premium of all entries (entry-weighted, not net of exits)."""
        total_qty = sum(e.qty for e in self.entries)
        if total_qty == 0:
            return self.initial_premium
        cost = sum(e.qty * e.premium for e in self.entries)
        return cost / total_qty

    @property
    def net_qty(self) -> int:
        return sum(e.qty for e in self.entries) - sum(x.qty for x in self.exits)

    @property
    def total_size_mult(self) -> float:
        if self.initial_qty <= 0:
            return 0.0
        return sum(e.qty for e in self.entries) / self.initial_qty

    @property
    def is_fully_closed(self) -> bool:
        return self.net_qty == 0

    # ─── Decisions ───
    def _active_targets(self) -> list[tuple[float, float]]:
        """Ladder rungs as (absolute_premium, fraction)."""
        cfg = self.scaling_config
        if cfg.target_premiums:
            return cfg.target_premiums
        return [(self.avg_cost * mult, frac) for mult, frac in cfg.profit_targets]

    def should_take_profit(self, current_premium: float) -> tuple[int, int | None]:
        """Return (qty_to_exit, target_idx) for the next pending profit rung, or (0, None)."""
        if self.is_fully_closed:
            return 0, None
        cfg = self.scaling_config
        targets = self._active_targets()
        if cfg.profile == "no_scaling":
            # Single exit at the highest target only
            if targets:
                last_idx = len(targets) - 1
                if last_idx in self.targets_hit:
                    return 0, None
                level, frac = targets[last_idx]
                if current_premium >= level:
                    return self.net_qty, last_idx
            return 0, None
        # Multi-rung ladder
        for idx, (level, frac) in enumerate(targets):
            if idx in self.targets_hit:
                continue
            if current_premium >= level:
                qty_to_exit = max(int(round(self.net_qty * frac)), 1)
                qty_to_exit = min(qty_to_exit, self.net_qty)
                # Last rung always exits remaining qty
                if idx == len(targets) - 1 or frac >= 1.0:
                    qty_to_exit = self.net_qty
                return qty_to_exit, idx
            # don't fire later rungs before earlier ones
            return 0, None
        return 0, None

    def force_next_target(self) -> tuple[int, int | None]:
        """Strategy-specific accelerator (POC magnet, RSI extreme).

        Forces the next pending profit rung regardless of premium level.
        """
        if self.is_fully_closed:
            return 0, None
        targets = self._active_targets()
        for idx, (level, frac) in enumerate(targets):
            if idx in self.targets_hit:
                continue
            qty_to_exit = max(int(round(self.net_qty * frac)), 1)
            qty_to_exit = min(qty_to_exit, self.net_qty)
            if idx == len(targets) - 1 or frac >= 1.0:
                qty_to_exit = self.net_qty
            return qty_to_exit, idx
        return 0, None

    def should_average(self, current_premium: float, current_underlying: float,
                       now: datetime) -> int:
        """Decide whether to add an averaging entry. Returns qty to add (0 = skip).

        ALL guardrails must pass. Profile must be averaging|full.
        """
        cfg = self.scaling_config
        if cfg.profile not in {"averaging", "full"}:
            return 0
        if self.averages_done >= len(cfg.avg_triggers):
            return 0
        if self.total_size_mult >= cfg.max_total_size_mult - 1e-6:
            return 0
        # Time gates
        elapsed_min = (now - self.initial_entry_ts).total_seconds() / 60
        if elapsed_min > cfg.no_averaging_after_minutes:
            return 0
        cutoff = cfg.no_averaging_cutoff_time
        if isinstance(cutoff, time):
            now_t = now.timetz().replace(tzinfo=None) if now.tzinfo else now.time()
            if now_t >= cutoff:
                return 0
        # Invalidation check
        if self.is_invalidated(current_underlying):
            return 0
        # Premium trigger
        trigger = cfg.avg_triggers[self.averages_done]
        if current_premium > self.initial_premium * trigger:
            return 0
        # Size to add
        size_mult = cfg.avg_size_mults[self.averages_done]
        return max(int(self.initial_qty * size_mult), 1)

    def should_pyramid(self, current_premium: float, has_fresh_signal: bool) -> int:
        """Decide whether to add a pyramid entry. Returns qty to add (0 = skip)."""
        cfg = self.scaling_config
        if cfg.profile not in {"profit_pyramid", "full"}:
            return 0
        if cfg.pyramid_after_target_idx < 0:
            return 0
        if cfg.pyramid_after_target_idx not in self.targets_hit:
            return 0
        if self.pyramids_done >= cfg.max_pyramids:
            return 0
        if self.total_size_mult >= cfg.max_total_size_mult - 1e-6:
            return 0
        if cfg.pyramid_requires_fresh_signal and not has_fresh_signal:
            return 0
        return max(int(self.initial_qty * cfg.pyramid_size_mult), 1)

    def is_invalidated(self, current_underlying: float) -> bool:
        """Check whether the original setup-defining level has been broken."""
        if self.invalidation_override:
            return True
        if self.invalidation_level is None:
            return False
        if self.invalidation_direction == "below":
            # Bullish trade: invalidated if price goes below the level
            return current_underlying < self.invalidation_level
        elif self.invalidation_direction == "above":
            # Bearish trade: invalidated if price goes above the level
            return current_underlying > self.invalidation_level
        return False

    def hit_hard_stop(self, current_premium: float) -> bool:
        """45% below avg cost → exit everything."""
        if self.is_fully_closed:
            return False
        threshold = self.avg_cost * (1 - self.scaling_config.hard_stop_pct_from_avg)
        return current_premium <= threshold

    def __repr__(self) -> str:
        return (f"ScaledPosition({self.strategy_name} {self.direction} "
                f"qty={self.net_qty}/{self.initial_qty} "
                f"avg=₹{self.avg_cost:.2f} targets={sorted(self.targets_hit)} "
                f"avgs={self.averages_done} pyramids={self.pyramids_done})")


def reconstruct_from_journal(strategy_name: str, trade_id: str,
                             journal, scaling_config: ScalingConfig) -> ScaledPosition | None:
    """Rebuild a ScaledPosition from trade_journal rows on bot restart.

    Uses the existing trades + trade_exits tables. Returns None if not found.
    """
    if journal is None or not hasattr(journal, "conn"):
        return None
    row = journal.conn.execute(
        "SELECT side, quantity, entry_price, entry_time FROM trades WHERE trade_id=? AND strategy_name=?",
        (trade_id, strategy_name),
    ).fetchone()
    if not row:
        return None
    direction = "bullish" if row["side"].upper() == "BUY" else "bearish"
    initial_qty = int(row["quantity"])
    initial_premium = float(row["entry_price"])
    initial_ts = datetime.fromisoformat(row["entry_time"])

    pos = ScaledPosition(
        strategy_name=strategy_name, trade_id=trade_id, direction=direction,
        initial_premium=initial_premium, initial_qty=initial_qty,
        invalidation_level=None, invalidation_direction="below",
        scaling_config=scaling_config, initial_entry_ts=initial_ts,
    )
    pos.add_entry(initial_qty, initial_premium, initial_ts, kind="initial")

    # Replay any prior exits
    exits = journal.conn.execute(
        "SELECT exit_price, exit_quantity, exit_time, exit_reason "
        "FROM trade_exits WHERE trade_id=? ORDER BY exit_time",
        (trade_id,),
    ).fetchall()
    for ex in exits:
        pos.add_exit(int(ex["exit_quantity"]), float(ex["exit_price"]),
                     datetime.fromisoformat(ex["exit_time"]), ex["exit_reason"])
    return pos
