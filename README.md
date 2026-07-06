# Indian Market Scanner (NSE + BSE)

Full-market screener, live charts, top movers, news, AI analysis and a
paper-trade tracker — all local, in one dashboard.

> ⚠ Learning tool. Signals and rankings are hypotheses, not guarantees.
> Paper trade 30+ sessions and check the report before risking real money.

## Start

```bash
cd "/Volumes/SHIVAY DATA/stock-scanner"
./venv/bin/python app.py
```

Open **http://localhost:5050**

## Features

- **Full Indian market** — every listed NSE equity (~2050) + every active
  BSE-only equity (~2950), deduped by ISIN. Universe list refreshes weekly
  from official NSE/BSE sources.
- **Market status light** — pulsing green = open, amber = pre-open, red =
  closed/holiday. Verified against live data, not just the clock.
- **Stock search** — any listed company. Price, stats (RSI, volume ratio,
  SMA 20/50, avg range, 52w high/low), scanner verdict with a trade plan.
- **Charts** — 1D / 1W / 1M / 3M / 1Y / 5Y. Scroll to zoom, drag to pan,
  double-click to reset, crosshair tooltip. **1D auto-refreshes every minute
  while the market is open (LIVE badge).** Yahoo data is 1–15 min delayed.
- **Top 60 Market Movers** — scans the whole market and ranks by an activity
  score (volume surge, move size, RSI extremes, distance from 52w high/low,
  turnover). Takes ~5–10 min; cached 10 min. Ranking ≠ profit prediction.
- **📰 News** — latest headlines per stock (Yahoo Finance).
- **✨ AI Analysis** — Claude reads all indicators + news and writes a
  structured take (trend, momentum, levels, bull/bear case, verdict + risk).
  Needs `ANTHROPIC_API_KEY` (or an `ant auth login` profile); otherwise a
  rule-based analysis is generated locally.
- **⚙ Settings** — capital, risk/trade, stop loss, target editable in the UI.
- **Run Scan** — 9:15 momentum screen over the full market (liquidity-gated,
  turnover ≥ ₹5 cr) producing top 5 setups with entry/stop/target/qty.
- **Paper-Trade Report** — every signal evaluated against the actual session
  high/low (SL-first, costs deducted): win rate, net P&L, equity curve.

## CLI

```bash
./venv/bin/python scanner.py scan
./venv/bin/python scanner.py evaluate
```

## Auto-scan every trading day at 9:20 AM

```bash
(crontab -l 2>/dev/null; echo '20 9 * * 1-5 cd "/Volumes/SHIVAY DATA/stock-scanner" && ./venv/bin/python scanner.py scan >> scan.log 2>&1') | crontab -
```

## Files

- `scanner.py` — all logic (universe, scan, movers, charts, news, AI, evaluate)
- `app.py` — Flask server
- `static/index.html` — dashboard UI
- `signals_log.csv` — every signal and its outcome
- `equity_universe.json` — cached NSE+BSE symbol list (weekly)
- `top_movers.json` — cached movers scan (10 min)
