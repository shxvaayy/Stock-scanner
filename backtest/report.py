"""Backtest reporting: per-strategy train/test tables with exit-reason and
fee-share attribution.
"""

from __future__ import annotations

from datetime import date
from pathlib import Path

import pandas as pd

from backtest.records import TradeRecord

ROOT = Path(__file__).resolve().parent.parent
OUT_DIR = ROOT / "data" / "backtest_results"


def to_dataframe(trades: list[TradeRecord], split: date | None) -> pd.DataFrame:
    df = pd.DataFrame([t.__dict__ for t in trades])
    if df.empty:
        return df
    df["date"] = pd.to_datetime(df["date"]).dt.date
    if split:
        df["segment"] = df["date"].map(lambda d: "train" if d < split else "test")
    else:
        df["segment"] = "all"
    return df


def _max_drawdown(daily_pnl: pd.Series) -> float:
    eq = daily_pnl.cumsum()
    return float((eq - eq.cummax()).min()) if len(eq) else 0.0


def summarize(df: pd.DataFrame) -> pd.DataFrame:
    """Per (strategy, segment) summary table."""
    if df.empty:
        return pd.DataFrame()
    rows = []
    for (strat, seg), g in df.groupby(["strategy", "segment"]):
        daily = g.groupby("date")["net_pnl"].sum()
        gross_winners = g.loc[g["net_pnl"] > 0, "net_pnl"].sum()
        gross_losers = -g.loc[g["net_pnl"] < 0, "net_pnl"].sum()
        rows.append({
            "strategy": strat, "segment": seg,
            "trades": len(g),
            "win_rate": round((g["net_pnl"] > 0).mean(), 3),
            "gross": round(g["gross_pnl"].sum(), 0),
            "fees": round(g["fees"].sum(), 0),
            "net": round(g["net_pnl"].sum(), 0),
            "avg_trade": round(g["net_pnl"].mean(), 0),
            "profit_factor": round(gross_winners / gross_losers, 2) if gross_losers > 0 else float("inf"),
            "max_dd": round(_max_drawdown(daily), 0),
            "fee_share_of_gross": round(
                g["fees"].sum() / abs(g["gross_pnl"]).sum(), 3) if abs(g["gross_pnl"]).sum() > 0 else 0,
        })
    out = pd.DataFrame(rows).sort_values(["strategy", "segment"])
    return out


def exit_reason_attribution(df: pd.DataFrame) -> pd.DataFrame:
    if df.empty:
        return pd.DataFrame()
    return (df.groupby(["strategy", "segment", "reason"])
            .agg(n=("net_pnl", "count"), net=("net_pnl", "sum"))
            .round(0).reset_index())


def print_report(df: pd.DataFrame, label: str = "") -> None:
    print(f"\n{'=' * 90}")
    print(f"  BACKTEST REPORT {label}")
    print(f"{'=' * 90}")
    summary = summarize(df)
    if summary.empty:
        print("  no trades")
        return
    print(summary.to_string(index=False))
    print(f"\n  ── exit-reason attribution ──")
    print(exit_reason_attribution(df).to_string(index=False))
    by_seg = df.groupby("segment")["net_pnl"].sum().round(0)
    print(f"\n  ── combined net by segment ──")
    print(by_seg.to_string())
    # monthly stability
    m = df.copy()
    m["month"] = pd.to_datetime(m["date"].astype(str)).dt.to_period("M")
    pivot = m.pivot_table(index="month", columns="strategy", values="net_pnl",
                          aggfunc="sum").round(0)
    print(f"\n  ── monthly net P&L per strategy ──")
    print(pivot.to_string())


def save(df: pd.DataFrame, name: str) -> Path:
    OUT_DIR.mkdir(exist_ok=True)
    path = OUT_DIR / name
    df.to_csv(path, index=False)
    return path
