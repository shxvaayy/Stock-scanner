"""Phase 0 probe: what historical data can Angel One actually serve?

Read-only. Answers the unknowns that decide the backtest data plan:
  1. Equity 1-min depth: does RELIANCE-EQ have 1-min data for June 2025?
  2. Nifty index (99926000) 1-min: available 12 months back? volume always 0?
  3. Nifty futures: how far back does each currently-listed FUTIDX token go?
  4. India VIX (99926017): daily history depth + does intraday 1-min exist?
  5. New watchlist symbol (JIOFIN-EQ): token lookup + 30-day 1-min fetch.
  6. Expired weekly option contracts: is any history queryable? (for S2)

Usage: python scripts/probe_history.py
"""

from __future__ import annotations

import json
import os
import sys
import time
from datetime import date, datetime
from pathlib import Path

import pandas as pd
import pyotp
from dotenv import load_dotenv
from SmartApi import SmartConnect

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))
load_dotenv(ROOT / ".env")

THROTTLE = 0.4


def fetch(api, exchange, token, interval, frm, to, label):
    """One getCandleData call; returns (n_rows, first_ts, last_ts, sample_volumes)."""
    time.sleep(THROTTLE)
    try:
        res = api.getCandleData({
            "exchange": exchange, "symboltoken": str(token),
            "interval": interval, "fromdate": frm, "todate": to,
        })
    except Exception as e:
        print(f"  [{label}] ERROR: {e}")
        return None
    data = (res or {}).get("data") or []
    if not data:
        msg = (res or {}).get("message", "no data")
        print(f"  [{label}] EMPTY ({msg})")
        return None
    vols = [row[5] for row in data[:5]]
    print(f"  [{label}] {len(data)} rows | {data[0][0]} .. {data[-1][0]} | first vols={vols}")
    return data


def main():
    api = SmartConnect(os.getenv("ANGEL_API_KEY"))
    totp = pyotp.TOTP(os.getenv("ANGEL_TOTP_SECRET")).now()
    sess = api.generateSession(os.getenv("ANGEL_CLIENT_ID"),
                               os.getenv("ANGEL_PASSWORD"), totp)
    if not sess or not sess.get("status"):
        print(f"login failed: {sess}")
        sys.exit(1)
    print("login ok\n")

    with open(ROOT / "data" / "instruments.json") as f:
        inst = pd.DataFrame(json.load(f))

    def eq_token(symbol):
        m = inst[(inst["symbol"] == symbol) & (inst["exch_seg"] == "NSE")]
        return str(m.iloc[0]["token"]) if not m.empty else None

    print("── 1. Equity 1-min depth (RELIANCE-EQ) ──")
    rel = eq_token("RELIANCE-EQ")
    print(f"  token={rel}")
    fetch(api, "NSE", rel, "ONE_MINUTE", "2025-06-02 09:15", "2025-07-01 15:30",
          "RELIANCE 1m Jun2025 (30d chunk)")
    fetch(api, "NSE", rel, "ONE_MINUTE", "2024-06-03 09:15", "2024-07-02 15:30",
          "RELIANCE 1m Jun2024 (2yr back)")

    print("\n── 2. Nifty index 1-min (99926000) ──")
    fetch(api, "NSE", "99926000", "ONE_MINUTE", "2025-06-02 09:15", "2025-07-01 15:30",
          "NIFTY idx 1m Jun2025")
    fetch(api, "NSE", "99926000", "ONE_MINUTE", "2026-06-08 09:15", "2026-06-09 15:30",
          "NIFTY idx 1m recent")

    print("\n── 3. Nifty futures depth per listed token ──")
    fut = inst[(inst["name"] == "NIFTY") & (inst["instrumenttype"] == "FUTIDX")
               & (inst["exch_seg"] == "NFO")].copy()
    fut["expiry_dt"] = pd.to_datetime(fut["expiry"], format="mixed", dayfirst=True,
                                      errors="coerce").dt.date
    fut = fut.sort_values("expiry_dt")
    print(f"  listed contracts: {[(r['symbol'], str(r['expiry_dt'])) for _, r in fut.iterrows()]}")
    for _, row in fut.iterrows():
        # ask for a deliberately ancient start; API returns whatever it has
        fetch(api, "NFO", row["token"], "ONE_DAY", "2025-01-01 09:15", "2026-06-09 15:30",
              f"FUT {row['symbol']} daily-depth")

    print("\n── 4. India VIX (99926017) ──")
    fetch(api, "NSE", "99926017", "ONE_DAY", "2025-06-01 09:15", "2026-06-09 15:30",
          "VIX daily 12mo")
    fetch(api, "NSE", "99926017", "ONE_MINUTE", "2026-06-02 09:15", "2026-06-02 15:30",
          "VIX 1m recent expiry-ish day")
    fetch(api, "NSE", "99926017", "ONE_MINUTE", "2025-07-01 09:15", "2025-07-01 15:30",
          "VIX 1m 11mo ago")

    print("\n── 5. New watchlist symbol (JIOFIN-EQ) ──")
    jio = eq_token("JIOFIN-EQ")
    print(f"  token={jio}")
    if jio:
        fetch(api, "NSE", jio, "ONE_MINUTE", "2025-06-02 09:15", "2025-07-01 15:30",
              "JIOFIN 1m Jun2025")
    for sym in ["JSWENERGY-EQ", "ADANIPOWER-EQ", "ETERNAL-EQ", "PNB-EQ",
                "BANKBARODA-EQ", "SAIL-EQ", "IRCON-EQ", "NAVA-EQ"]:
        t = eq_token(sym)
        print(f"  token {sym} = {t}")

    print("\n── 6. Expired weekly options (for S2 backtest) ──")
    opt = inst[(inst["name"] == "NIFTY") & (inst["instrumenttype"] == "OPTIDX")
               & (inst["exch_seg"] == "NFO")].copy()
    opt["expiry_dt"] = pd.to_datetime(opt["expiry"], format="mixed", dayfirst=True,
                                      errors="coerce").dt.date
    today = date(2026, 6, 10)
    expired = opt[opt["expiry_dt"] < today]
    live = opt[opt["expiry_dt"] >= today]
    print(f"  OPTIDX rows in master: total={len(opt)}, expired={len(expired)}, live={len(live)}")
    print(f"  expiry range in master: {opt['expiry_dt'].min()} .. {opt['expiry_dt'].max()}")
    if not expired.empty:
        row = expired.sort_values("expiry_dt").iloc[-1]
        d = row["expiry_dt"]
        print(f"  probing most recent expired contract {row['symbol']} (exp {d})")
        fetch(api, "NFO", row["token"], "ONE_MINUTE",
              f"{d} 09:15", f"{d} 15:30", f"expired {row['symbol']} 1m on expiry day")
    else:
        print("  no expired contracts in current master — S2 backtest must use synthesized premiums")

    print("\nprobe complete")


if __name__ == "__main__":
    main()
